"""Cause-driven deterministic grouping and skip source analysis."""

from __future__ import annotations

import ast
import hashlib
import importlib
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from ngts.scripts.regression_mail.ai import error_log_signature
from ngts.scripts.regression_mail.models import ResultRow, SemanticGroup, SemanticReport, WorkbookSnapshot
from ngts.scripts.regression_mail.normalization import (
    canonical_test_name,
    extract_failure_message_redmine_urls,
    extract_internal_change_urls,
    extract_public_issue_urls,
    extract_redmine_issue_ids,
    extract_redmine_urls,
    join_distinct_analysis_blocks,
    join_comment_blocks,
    normalize_text,
    retain_redmine_references,
    sanitize_public_comment,
    strip_public_issue_urls,
    strip_redmine_references,
    unique_sorted,
)

_GENERIC_FAILURE_WRAPPER = re.compile(
    r"(?i)^(?:tests\.[A-Za-z0-9_.]+\.)?RunAnsibleModuleFail"
    r"(?:\s*:\s*.*)?$"
)


def build_deterministic_report(workbook: WorkbookSnapshot) -> SemanticReport:
    """Group exact matching evidence without inferring missing causes."""

    return SemanticReport(
        failure_groups=_cause_groups(workbook, "fail", "failure"),
        skip_groups=_cause_groups(workbook, "skipped", "skip"),
    )


def compact_test_members(
    workbook: WorkbookSnapshot,
    member_ids: Iterable[str],
) -> str:
    """Render parameter/class/file folding for explicit workbook members."""

    wanted = set(member_ids)
    rows = [
        row
        for row in workbook.selected_rows
        if row.record_id in wanted
    ]
    return "\n".join(compact_tests(rows, workbook.selected_rows))


def _cause_groups(
    workbook: WorkbookSnapshot,
    result: str,
    prefix: str,
) -> List[SemanticGroup]:
    rows = [row for row in workbook.selected_rows if row.result == result]
    if result in {"fail", "skipped"}:
        ticketed: DefaultDict[
            Tuple[Tuple[str, ...], str],
            List[ResultRow],
        ] = defaultdict(list)
        ticket_values: Dict[
            Tuple[Tuple[str, ...], str],
            Tuple[str, ...],
        ] = {}
        unticketed: List[ResultRow] = []
        for row in rows:
            ticket_refs = _row_ticket_refs(row, workbook)
            if ticket_refs:
                analysis_scope = (
                    _dashboard_analysis_scope(row, workbook)
                    if result == "fail"
                    else ""
                )
                ticket_key = (
                    tuple(
                        ticket_ref.casefold()
                        for ticket_ref in ticket_refs
                    ),
                    analysis_scope,
                )
                ticket_values.setdefault(
                    ticket_key,
                    tuple(ticket_refs),
                )
                ticketed[ticket_key].append(row)
            else:
                unticketed.append(row)
        scoped_components: List[
            Tuple[Tuple[str, ...], List[ResultRow]]
        ] = [
            (ticket_values[ticket_key], component)
            for ticket_key, component in ticketed.items()
        ]
        scoped_components.extend(
            ((), component)
            for component in _cause_components(
                unticketed,
                workbook,
                fold_unanalyzed_failures=(result == "fail"),
            )
        )
        scoped_components.sort(
            key=lambda item: (
                min(row.excel_row for row in item[1]),
                item[0],
                min(row.record_id for row in item[1]),
            )
        )
        return [
            _make_cause_group(
                prefix,
                component,
                workbook,
                ticket_scopes=ticket_scopes,
            )
            for ticket_scopes, component in scoped_components
        ]

    components = [
        component
        for _test_file_name, file_rows in sorted(
            _rows_by(rows, _test_file).items()
        )
        for component in _cause_components(
            file_rows,
            workbook,
            fold_unanalyzed_failures=(result == "fail"),
        )
    ]
    return [
        _make_cause_group(prefix, component, workbook)
        for component in components
    ]


def _row_ticket_refs(
    row: ResultRow,
    workbook: WorkbookSnapshot,
) -> List[str]:
    if row.result == "skipped":
        if workbook.issue_skip_analysis_complete:
            return unique_sorted(workbook.issue_skip_tickets.get(row.record_id, ()))
        return unique_sorted(
            extract_redmine_urls(row.message) +
            extract_public_issue_urls(row.message)
        )
    manual_values = _explicit_excel_comments(row)
    manual_values.append(workbook.row_comments.get(row.record_id, ""))
    redmine_urls = [
        issue
        for value in manual_values
        for issue in extract_redmine_urls(value)
    ]
    redmine_urls.extend(
        (
            extract_failure_message_redmine_urls(row.message)
            if row.result == "fail"
            else extract_redmine_urls(row.message)
        )
    )
    public_urls = [
        issue
        for value in _raw_causes(row, workbook)
        for issue in extract_public_issue_urls(value)
    ]
    internal_change_urls = [
        change
        for value in _raw_causes(row, workbook)
        for change in extract_internal_change_urls(value)
    ]
    return unique_sorted(
        redmine_urls + public_urls + internal_change_urls
    )


def _dashboard_analysis_scope(
    row: ResultRow,
    workbook: WorkbookSnapshot,
) -> str:
    return (
        "analyzed"
        if workbook.dashboard_statuses.get(row.record_id) == "analyzed"
        else "missing"
    )


def _cause_components(
    rows: Sequence[ResultRow],
    workbook: WorkbookSnapshot,
    fold_unanalyzed_failures: bool = False,
) -> List[List[ResultRow]]:
    parents = list(range(len(rows)))
    first_by_evidence: Dict[str, int] = {}
    plain_by_text: DefaultDict[str, List[int]] = defaultdict(list)
    ticketed_by_text: DefaultDict[Tuple[str, str], List[int]] = defaultdict(list)
    text_links = [
        _cause_text_links(row, workbook)
        for row in rows
    ]
    ticket_scopes_by_text: DefaultDict[str, Set[str]] = defaultdict(set)
    for links in text_links:
        for text, ticket_scope in links:
            if ticket_scope:
                ticket_scopes_by_text[text].add(ticket_scope)

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def merge(left: int, right: int) -> None:
        left_root = root(left)
        right_root = root(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for index, row in enumerate(rows):
        evidence_values = _cause_evidence(row, workbook)
        if (
            not evidence_values and
            fold_unanalyzed_failures and
            _parameter_value(row.test_name)
        ):
            canonical = canonical_test_name(row.test_name)
            if canonical:
                evidence_values.add(
                    "unexplained-parameterized-test:{}:{}".format(
                        _dashboard_analysis_scope(row, workbook),
                        canonical.casefold(),
                    )
                )
        for evidence in evidence_values:
            previous = first_by_evidence.setdefault(evidence, index)
            merge(index, previous)
        for text, ticket_scope in text_links[index]:
            known_scopes = ticket_scopes_by_text[text]
            if ticket_scope:
                if len(known_scopes) == 1:
                    for previous in plain_by_text[text]:
                        merge(index, previous)
                ticketed_by_text[(text, ticket_scope)].append(index)
            else:
                if len(known_scopes) == 1:
                    only_scope = next(iter(known_scopes))
                    for previous in ticketed_by_text[(text, only_scope)]:
                        merge(index, previous)
                plain_by_text[text].append(index)

    grouped: DefaultDict[int, List[ResultRow]] = defaultdict(list)
    for index, row in enumerate(rows):
        grouped[root(index)].append(row)
    return sorted(
        (sorted(group, key=lambda item: (item.excel_row, item.record_id))
         for group in grouped.values()),
        key=lambda group: (group[0].excel_row, group[0].record_id),
    )


def _cause_evidence(
    row: ResultRow,
    workbook: WorkbookSnapshot,
) -> Set[str]:
    raw_values = _grouping_causes(row, workbook)
    evidence = {
        "text:{}{}".format(
            "dashboard:{}:{}".format(
                _dashboard_analysis_scope(row, workbook),
                (
                    "file:{}:".format(
                        _test_file(row.test_name).casefold()
                    )
                    if _GENERIC_FAILURE_WRAPPER.fullmatch(normalized)
                    else ""
                ),
            )
            if row.result == "fail"
            else "",
            normalized.casefold(),
        )
        for normalized in (
            _semantic_cause_signature(value) for value in raw_values
        )
        if normalized
    }
    evidence.update(
        "ticket:" + reference.casefold()
        for reference in _row_ticket_refs(row, workbook)
    )
    return evidence


def _cause_text_links(
    row: ResultRow,
    workbook: WorkbookSnapshot,
) -> Set[Tuple[str, str]]:
    links = set()
    row_references = _row_ticket_refs(row, workbook)
    for value in _grouping_causes(row, workbook):
        normalized = _semantic_cause_signature(value)
        without_tickets = _comment_without_tickets(normalized)
        if without_tickets:
            issue_refs = [
                "redmine:" + issue_id
                for reference in row_references
                for issue_id in extract_redmine_issue_ids(reference)
            ]
            issue_refs.extend(
                reference
                for reference in row_references
                if extract_public_issue_urls(reference)
            )
            links.add(
                (
                    without_tickets.casefold(),
                    "\x1f".join(unique_sorted(issue_refs)).casefold(),
                )
            )
    return links


def _semantic_cause_signature(value: object) -> str:
    error_signature = error_log_signature(value)
    return error_signature or normalize_text(value)


def _grouping_causes(
    row: ResultRow,
    workbook: WorkbookSnapshot,
) -> List[str]:
    if row.result == "skipped":
        message = normalize_text(row.message)
        return [message] if message else []
    excel_comment = _excel_comment(row)
    if excel_comment:
        return [excel_comment]
    return unique_sorted(
        normalize_text(line)
        for line in str(workbook.row_comments.get(row.record_id, "")).splitlines()
        if normalize_text(line)
    )


def _raw_causes(
    row: ResultRow,
    workbook: WorkbookSnapshot,
) -> List[str]:
    if row.result == "skipped":
        message = normalize_text(row.message)
        return [message] if message else []
    excel_comment = _excel_comment(row)
    values = [excel_comment] if excel_comment else []
    values.extend(
        normalize_text(line)
        for line in str(workbook.row_comments.get(row.record_id, "")).splitlines()
        if normalize_text(line)
    )
    return unique_sorted(values)


def _excel_comment(row: ResultRow) -> str:
    comments = _explicit_excel_comments(row)
    if comments:
        return comments[0]
    return normalize_text(row.message)


def _explicit_excel_comments(row: ResultRow) -> List[str]:
    return [
        str(value).strip()
        for key, value in row.values.items()
        if normalize_text(key).lower().replace("_", " ")
        in {"comment", "comments"} and
        str(value or "").strip()
    ]


def _parameter_value(test_name: object) -> str:
    match = re.search(r"\[([^\[\]]+)\]$", normalize_text(test_name))
    return normalize_text(match.group(1)) if match else ""


def _make_cause_group(
    prefix: str,
    rows: Sequence[ResultRow],
    workbook: WorkbookSnapshot,
    ticket_scopes: Sequence[str] = (),
) -> SemanticGroup:
    member_ids = sorted(row.record_id for row in rows)
    digest_values = list(member_ids)
    digest_values.extend(
        "ticket:" + ticket_scope.casefold()
        for ticket_scope in ticket_scopes
    )
    digest = hashlib.sha256(
        "\x1f".join(digest_values).encode("utf-8")
    ).hexdigest()[:16]
    raw_values = unique_sorted(
        value
        for row in rows
        for value in _raw_causes(row, workbook)
    )
    tickets = (
        unique_sorted(ticket_scopes)
        if ticket_scopes
        else unique_sorted(
            issue
            for row in rows
            for issue in _row_ticket_refs(row, workbook)
        )
    )
    internal_source = (
        _dashboard_analysis(rows, workbook)
        if prefix == "failure"
        else raw_values
    )
    internal_tickets = (
        tickets
        if ticket_scopes
        else unique_sorted(
            url
            for value in internal_source
            for url in (
                extract_redmine_urls(value) +
                extract_public_issue_urls(value)
            )
        )
    )
    internal = unique_sorted(
        (
            []
            if prefix == "skip"
            else [
                _comment_without_tickets(value)
                for value in internal_source
            ]
        ) +
        internal_tickets +
        tickets
    )
    scoped_raw_values = [
        _value_for_ticket_scopes(value, ticket_scopes)
        for value in raw_values
    ]
    public = unique_sorted(
        sanitize_public_comment(value) for value in scoped_raw_values
    )
    cause_evidence = unique_sorted(
        sanitize_public_comment(value, limit=None)
        for value in scoped_raw_values
    )
    redmine_urls = unique_sorted(
        url for url in tickets if extract_redmine_urls(url)
    )
    return SemanticGroup(
        group_id="{}-{}".format(prefix, digest),
        member_ids=member_ids,
        test_display="\n".join(compact_tests(rows, workbook.selected_rows)),
        testbeds=unique_sorted(row.testbed for row in rows),
        comments=join_comment_blocks(public),
        internal_comments=(
            join_distinct_analysis_blocks(internal)
            if prefix == "failure"
            else join_comment_blocks(internal)
        ),
        cause_evidence=join_comment_blocks(cause_evidence),
        ticket_refs=tickets,
        redmine_urls=redmine_urls,
        internal_review_status=(
            _dashboard_group_status(rows, workbook)
            if prefix == "failure"
            else ""
        ),
    )


def _value_for_ticket_scopes(
    value: str,
    ticket_scopes: Sequence[str],
) -> str:
    if not ticket_scopes:
        return value
    allowed = {
        ticket_scope.casefold()
        for ticket_scope in ticket_scopes
    }
    text = value
    for public_issue in extract_public_issue_urls(text):
        if public_issue.casefold() not in allowed:
            text = text.replace(public_issue, "")
    return normalize_text(text)


def _dashboard_analysis(
    rows: Sequence[ResultRow],
    workbook: WorkbookSnapshot,
) -> List[str]:
    return unique_sorted(
        normalize_text(line)
        for row in rows
        for line in str(workbook.row_comments.get(row.record_id, "")).splitlines()
        if normalize_text(line)
    )


def _dashboard_group_status(
    rows: Sequence[ResultRow],
    workbook: WorkbookSnapshot,
) -> str:
    statuses = {
        workbook.dashboard_statuses[row.record_id]
        for row in rows
        if row.record_id in workbook.dashboard_statuses
    }
    if not statuses or statuses == {"analyzed"}:
        return ""
    if "unavailable" in statuses:
        return "unavailable"
    if statuses == {"no_analysis"}:
        return "no_analysis"
    if statuses == {"not_found"}:
        return "not_found"
    return "partial"


def _comment_without_tickets(value: str) -> str:
    text = strip_public_issue_urls(value)
    for url in (
        extract_redmine_urls(text) +
        extract_internal_change_urls(text)
    ):
        text = text.replace(url, "")
    return normalize_text(strip_redmine_references(text).strip(" -;,"))


def compact_tests(
    rows: Sequence[ResultRow],
    all_rows: Sequence[ResultRow],
) -> List[str]:
    """Return the report display names using the shared folding rules."""

    del all_rows
    return compact_test_names(row.test_name for row in rows)


def compact_test_names(test_names: Iterable[str]) -> List[str]:
    """Fold parameters, cases, classes, and files without crossing files."""
    names_by_file: DefaultDict[str, List[str]] = defaultdict(list)
    for value in test_names:
        name = normalize_text(value)
        if name:
            names_by_file[_display_test_file(name)].append(name)
    display: List[str] = []
    for test_file, file_names in sorted(names_by_file.items()):
        display.extend(_compact_file_test_names(test_file, file_names))
    return display


def _compact_file_test_names(
    test_file: str,
    test_names: Sequence[str],
) -> List[str]:
    names_by_test: DefaultDict[str, List[str]] = defaultdict(list)
    for name in test_names:
        names_by_test[canonical_test_name(name)].append(name)
    canonical_tests = sorted(names_by_test)
    if not canonical_tests:
        return []
    if len(canonical_tests) == 1:
        canonical = canonical_tests[0]
        return _compact_test_variants(canonical, names_by_test[canonical])
    if not test_file:
        return [
            display
            for canonical in canonical_tests
            for display in _compact_test_variants(
                canonical,
                names_by_test[canonical],
            )
        ]

    scopes = {
        _test_class(name) or test_file
        for name in test_names
    }
    if len(scopes) > 1:
        return [test_file] if test_file else canonical_tests
    scope = next(iter(scopes))
    if scope and scope != test_file:
        return [scope]
    return [test_file] if test_file else canonical_tests


def _compact_test_variants(
    canonical: str,
    variants: Sequence[str],
) -> List[str]:
    distinct = unique_sorted(variants)
    if len(distinct) > 1:
        return [canonical]
    return distinct


def _display_test_file(value: object) -> str:
    name = canonical_test_name(value)
    if "::" in name:
        return name.split("::", 1)[0]
    return name if name.endswith(".py") else ""


def _rows_by(
    rows: Iterable[ResultRow],
    key,
) -> Mapping[str, List[ResultRow]]:
    grouped: DefaultDict[str, List[ResultRow]] = defaultdict(list)
    for row in rows:
        grouped[key(row.test_name)].append(row)
    return grouped


def _test_file(value: object) -> str:
    return normalize_text(value).split("::", 1)[0]


def _test_class(value: object) -> str:
    parts = canonical_test_name(value).split("::")
    if (
        len(parts) < 3 or
        not parts[-2].startswith("Test") or
        not parts[-1].startswith("test")
    ):
        return ""
    return "::".join(parts[:-1])


@dataclass(frozen=True)
class _SkipSourceEvidence:
    text: str
    tickets: Tuple[str, ...] = ()
    condition_matched: bool = False
    scope: str = ""
    condition: str = ""
    ticket_conditions: Tuple[Tuple[str, str], ...] = ()


class SourceSkipAnalyzer:
    """Resolve issue-attributed skips from source conditions and Excel facts."""

    def enrich(
        self,
        report: SemanticReport,
        workbook: WorkbookSnapshot,
        source_root: Path,
    ) -> SemanticReport:
        rules = _load_condition_rules(source_root)
        issue_skip_tickets: Dict[str, List[str]] = {}
        public_issue_conditions: DefaultDict[str, List[str]] = defaultdict(list)
        for row in workbook.skipped:
            row_tickets = _evidence_ticket_refs(
                " ".join(_row_source_values(row))
            )
            if not row_tickets:
                continue
            evidence = _condition_rule_evidence(rules, row)
            matching_rules = [item for item in evidence if item.condition_matched]
            matching_tickets = {
                ticket.casefold()
                for item in matching_rules
                for ticket in item.tickets
            }
            tickets = [
                ticket
                for ticket in row_tickets
                if ticket.casefold() in matching_tickets
            ]
            if not evidence:
                tickets = row_tickets
            if not tickets:
                continue
            issue_skip_tickets[row.record_id] = tickets
            for item in matching_rules:
                conditions = dict(item.ticket_conditions)
                item_tickets = {value.casefold() for value in item.tickets}
                for ticket in tickets:
                    if (
                        not extract_public_issue_urls(ticket) or
                        ticket.casefold() not in item_tickets
                    ):
                        continue
                    condition = normalize_text(
                        conditions.get(ticket) or item.condition
                    )
                    condition = strip_public_issue_urls(condition)
                    if condition and condition not in public_issue_conditions[ticket]:
                        public_issue_conditions[ticket].append(condition)
        workbook.issue_skip_tickets = issue_skip_tickets
        workbook.public_issue_conditions = dict(public_issue_conditions)
        workbook.issue_skip_analysis_complete = True
        refreshed = build_deterministic_report(workbook)
        refreshed.internal_error_groups = report.internal_error_groups
        report.failure_groups = refreshed.failure_groups
        report.skip_groups = refreshed.skip_groups
        return report


def _source_evidence_applies(
    evidence: _SkipSourceEvidence,
    row: ResultRow,
    workbook: WorkbookSnapshot,
) -> bool:
    if not evidence.tickets or evidence.condition_matched:
        return True
    row_tickets = {
        value.casefold()
        for value in _row_ticket_refs(row, workbook)
    }
    return any(ticket.casefold() in row_tickets for ticket in evidence.tickets)


def _evidence_ticket_refs(value: object) -> List[str]:
    text = str(value or "")
    return unique_sorted(
        extract_redmine_urls(text) + extract_public_issue_urls(text)
    )


def _load_condition_rules(
    source_root: Path,
) -> List[Tuple[Path, str, Mapping[str, Any]]]:
    yaml = importlib.import_module("yaml")

    directory = (
        source_root /
        "tests" /
        "common" /
        "plugins" /
        "conditional_mark"
    )
    result: List[Tuple[Path, str, Mapping[str, Any]]] = []
    for path in sorted(directory.glob("tests_mark_conditions*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            continue
        for selector, details in payload.items():
            if isinstance(details, Mapping):
                result.append(
                    (
                        path.relative_to(source_root),
                        normalize_text(selector),
                        details,
                    )
                )
    return result


def _condition_rule_evidence(
    rules: Sequence[Tuple[Path, str, Mapping[str, Any]]],
    row: ResultRow,
) -> List[_SkipSourceEvidence]:
    candidates: DefaultDict[str, List[Tuple[int, _SkipSourceEvidence]]] = (
        defaultdict(list)
    )
    for display_path, selector, details in rules:
        if not _condition_selector_matches(selector, details, row.test_name):
            continue
        for mark_name in ("skip", "xfail"):
            mark = details.get(mark_name)
            if not isinstance(mark, Mapping):
                continue
            reason = normalize_text(mark.get("reason"))
            conditions_value = mark.get("conditions")
            if isinstance(conditions_value, Sequence) and not isinstance(
                conditions_value,
                (str, bytes),
            ):
                conditions = [
                    normalize_text(value)
                    for value in conditions_value
                    if normalize_text(value)
                ]
            else:
                condition = normalize_text(conditions_value)
                conditions = [condition] if condition else []
            logical_operator = normalize_text(
                mark.get("conditions_logical_operator") or "AND"
            ).upper()
            matches, active_conditions = _matching_conditions(
                conditions,
                logical_operator,
                row,
            )
            condition_tickets = _evidence_ticket_refs(" ".join(conditions))
            reason_tickets = _evidence_ticket_refs(reason)
            tickets = unique_sorted(condition_tickets + reason_tickets)
            row_tickets = {
                value.casefold()
                for value in _evidence_ticket_refs(
                    " ".join(_row_source_values(row))
                )
            }
            overlap = [
                ticket
                for ticket in _evidence_ticket_refs(
                    " ".join([reason] + conditions)
                )
                if ticket.casefold() in row_tickets
            ]
            if not matches and not overlap:
                continue
            if not condition_tickets and overlap:
                tickets = unique_sorted(overlap)
            if matches and condition_tickets:
                active_condition_tickets = _evidence_ticket_refs(
                    " ".join(active_conditions)
                )
                tickets = unique_sorted(
                    [
                        ticket
                        for ticket in reason_tickets
                        if ticket not in condition_tickets
                    ] + (
                        active_condition_tickets
                        if _tickets_are_required_for_conditions(
                            conditions,
                            logical_operator,
                            row,
                            condition_tickets,
                        )
                        else []
                    )
                )
            condition_text = " {} ".format(
                logical_operator.lower()
            ).join(active_conditions or conditions)
            ticket_conditions: List[Tuple[str, str]] = []
            selected_conditions = active_conditions or conditions
            if logical_operator == "OR":
                for selected_condition in selected_conditions:
                    ticket_conditions.extend(
                        (ticket, selected_condition)
                        for ticket in _evidence_ticket_refs(
                            selected_condition
                        )
                    )
            else:
                ticket_conditions.extend(
                    (ticket, condition_text)
                    for ticket in tickets
                    if ticket in condition_tickets
                )
            assigned_tickets = {
                ticket.casefold()
                for ticket, _condition in ticket_conditions
            }
            ticket_conditions.extend(
                (ticket, condition_text)
                for ticket in tickets
                if ticket.casefold() not in assigned_tickets
            )
            scoped_reason = _retain_ticket_refs(reason, tickets)
            text = "{} {} {} reason={!r}".format(
                display_path,
                selector,
                mark_name,
                scoped_reason,
            )
            if condition_text:
                text += " conditions={!r}".format(condition_text)
            candidates[mark_name].append(
                (
                    len(selector),
                    _SkipSourceEvidence(
                        text=text,
                        tickets=tuple(tickets),
                        condition_matched=matches,
                        scope=selector,
                        condition=condition_text,
                        ticket_conditions=tuple(ticket_conditions),
                    ),
                )
            )
    result: List[_SkipSourceEvidence] = []
    for values in candidates.values():
        longest = max(length for length, _evidence in values)
        result.extend(
            evidence
            for length, evidence in values
            if length == longest
        )
    return result


def _retain_ticket_refs(value: str, allowed: Sequence[str]) -> str:
    allowed_keys = {ticket.casefold() for ticket in allowed}
    text = retain_redmine_references(value, allowed)
    for ticket in _evidence_ticket_refs(value):
        if (
            extract_public_issue_urls(ticket) and
            ticket.casefold() not in allowed_keys
        ):
            text = text.replace(ticket, "")
    return normalize_text(text)


def _condition_selector_matches(
    selector: str,
    details: Mapping[str, Any],
    test_name: str,
) -> bool:
    nodeid = normalize_text(test_name).lstrip("/")
    variants = {nodeid}
    if nodeid.startswith("tests/"):
        variants.add(nodeid[len("tests/"):])
    else:
        variants.add("tests/" + nodeid)
    if details.get("regex") is True:
        try:
            return any(re.search(selector, value) for value in variants)
        except re.error:
            return False
    normalized_selector = selector.lstrip("/").rstrip("/")
    return any(
        value == normalized_selector or
        any(
            value.startswith(normalized_selector + separator)
            for separator in ("/", "::", "[")
        )
        for value in variants
    )


def _matching_conditions(
    conditions: Sequence[str],
    operator: str,
    row: ResultRow,
) -> Tuple[bool, List[str]]:
    if not conditions:
        return True, []
    evaluations = [
        (_active_condition(condition, row), condition)
        for condition in conditions
    ]
    if operator == "OR":
        active = [
            rendered
            for (matched, rendered), _condition in evaluations
            if matched is True
        ]
        return bool(active), active
    if all(result[0] is True for result, _condition in evaluations):
        return True, [result[1] for result, _condition in evaluations]
    return False, []


def _evaluate_condition(
    condition: str,
    row: ResultRow,
) -> Optional[bool]:
    return _active_condition(condition, row)[0]


def _active_condition(
    condition: str,
    row: ResultRow,
) -> Tuple[Optional[bool], str]:
    tickets = _evidence_ticket_refs(condition)
    placeholders = {
        ticket: "__skip_ticket_{}".format(index)
        for index, ticket in enumerate(tickets)
    }
    expression = condition
    for ticket, placeholder in placeholders.items():
        expression = expression.replace(ticket, placeholder)
    expression = re.sub(
        r"https?://[^\s'\"),]+",
        "True",
        expression,
    )
    try:
        parsed = ast.parse(expression, mode="eval")
        facts = {
            **_condition_facts(row),
            **{placeholder: True for placeholder in placeholders.values()},
        }
        value, active = _prune_condition_node(parsed.body, facts)
        if not value or active is None:
            return bool(value), condition
        if ast.dump(active) == ast.dump(parsed.body):
            return True, condition
        rendered = ast.unparse(active)
        for ticket, placeholder in placeholders.items():
            rendered = rendered.replace(placeholder, ticket)
        return True, rendered
    except (KeyError, SyntaxError, TypeError, ValueError):
        return None, condition


def _prune_condition_node(
    node: ast.AST,
    facts: Mapping[str, object],
) -> Tuple[bool, Optional[ast.AST]]:
    if isinstance(node, ast.BoolOp):
        evaluated = [
            _prune_condition_node(value, facts)
            for value in node.values
        ]
        if isinstance(node.op, ast.Or):
            active = [
                value
                for matched, value in evaluated
                if matched and value is not None
            ]
            if not active:
                return False, None
            return True, active[0] if len(active) == 1 else ast.BoolOp(node.op, active)
        if isinstance(node.op, ast.And):
            if not all(matched for matched, _value in evaluated):
                return False, None
            active = [value for _matched, value in evaluated if value is not None]
            return True, active[0] if len(active) == 1 else ast.BoolOp(node.op, active)
    return bool(_evaluate_condition_node(node, facts)), node


def _tickets_are_required_for_conditions(
    conditions: Sequence[str],
    operator: str,
    row: ResultRow,
    tickets: Sequence[str],
) -> bool:
    original, _active = _matching_conditions(conditions, operator, row)
    if not original:
        return False
    disabled = list(conditions)
    for ticket in tickets:
        disabled = [condition.replace(ticket, "False") for condition in disabled]
    still_matches, _active = _matching_conditions(disabled, operator, row)
    return not still_matches


def _evaluate_condition_node(
    node: ast.AST,
    facts: Mapping[str, object],
) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return facts[node.id]
    if isinstance(node, ast.Call):
        if node.keywords:
            raise ValueError("conditional-mark calls cannot use keywords")
        arguments = [
            _evaluate_condition_node(argument, facts)
            for argument in node.args
        ]
        if isinstance(node.func, ast.Name):
            functions = {
                "int": int,
                "len": len,
                "str": str,
            }
            function = functions.get(node.func.id)
            if function is None:
                raise ValueError("unsupported conditional-mark function")
            return function(*arguments)
        if isinstance(node.func, ast.Attribute):
            target = _evaluate_condition_node(node.func.value, facts)
            if not isinstance(target, str) or node.func.attr not in {
                "endswith",
                "lower",
                "split",
                "startswith",
                "strip",
                "upper",
            }:
                raise ValueError("unsupported conditional-mark method")
            return getattr(target, node.func.attr)(*arguments)
        raise ValueError("unsupported conditional-mark call")
    if isinstance(node, ast.Subscript):
        target = _evaluate_condition_node(node.value, facts)
        index = _evaluate_condition_node(node.slice, facts)
        if not isinstance(target, (list, tuple, str, Mapping)):
            raise ValueError("unsupported conditional-mark subscript")
        return target[index]  # type: ignore[index]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [
            _evaluate_condition_node(item, facts)
            for item in node.elts
        ]
        if isinstance(node, ast.List):
            return values
        if isinstance(node, ast.Tuple):
            return tuple(values)
        return set(values)
    if isinstance(node, ast.BoolOp):
        values = [
            bool(_evaluate_condition_node(item, facts))
            for item in node.values
        ]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
        raise ValueError("unsupported boolean operation")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not bool(_evaluate_condition_node(node.operand, facts))
    if isinstance(node, ast.Compare):
        left = _evaluate_condition_node(node.left, facts)
        for operator, comparator in zip(node.ops, node.comparators):
            right = _evaluate_condition_node(comparator, facts)
            if not _evaluate_comparison(operator, left, right):
                return False
            left = right
        return True
    raise ValueError("unsupported conditional-mark expression")


def _evaluate_comparison(
    operator: ast.cmpop,
    left: object,
    right: object,
) -> bool:
    if isinstance(operator, ast.Eq):
        return left == right
    if isinstance(operator, ast.NotEq):
        return left != right
    if isinstance(operator, ast.In):
        return left in right  # type: ignore[operator]
    if isinstance(operator, ast.NotIn):
        return left not in right  # type: ignore[operator]
    if isinstance(operator, ast.Is):
        return left is right
    if isinstance(operator, ast.IsNot):
        return left is not right
    if isinstance(operator, ast.Lt):
        return left < right  # type: ignore[operator]
    if isinstance(operator, ast.LtE):
        return left <= right  # type: ignore[operator]
    if isinstance(operator, ast.Gt):
        return left > right  # type: ignore[operator]
    if isinstance(operator, ast.GtE):
        return left >= right  # type: ignore[operator]
    raise ValueError("unsupported comparison")


def _condition_facts(row: ResultRow) -> Dict[str, object]:
    values = {
        normalize_text(key).casefold().replace(" ", "_"): value
        for key, value in row.values.items()
    }
    topology = normalize_text(row.topology)
    platform = normalize_text(row.platform) or normalize_text(values.get("platform"))
    hwsku = normalize_text(row.hwsku) or normalize_text(values.get("hwsku"))
    asic_type = normalize_text(values.get("asic_type"))
    if not asic_type and (
        "nvidia" in platform.casefold() or
        "mlnx" in platform.casefold() or
        "mellanox" in hwsku.casefold()
    ):
        asic_type = "mellanox"
    return {
        **values,
        "platform": platform,
        "hwsku": hwsku,
        "topo_name": topology or
        normalize_text(values.get("topo_name")),
        "topo_type": normalize_text(values.get("topo_type")) or
        topology.split("-", 1)[0],
        "testbed": normalize_text(row.testbed) or
        normalize_text(values.get("testbed")),
        "build_version": normalize_text(
            values.get("build_version") or
            values.get("version") or
            values.get("os_version")
        ),
        "asic_type": asic_type,
        "asic_gen": normalize_text(values.get("asic_gen")),
        "is_mellanox": asic_type == "mellanox",
        "is_nvidia": asic_type == "mellanox",
    }


def _row_source_values(row: ResultRow) -> List[str]:
    values = [normalize_text(row.message)]
    values.extend(
        normalize_text(value)
        for key, value in row.values.items()
        if normalize_text(key).casefold().replace("_", " ")
        in {"message", "comment", "comments"}
    )
    return [value for value in values if value]


def _group_targets(rows: Sequence[ResultRow]) -> Mapping[str, Sequence[str]]:
    targets: Dict[str, List[str]] = {}
    for row in rows:
        parts = normalize_text(row.test_name).split("::")
        if len(parts) < 2:
            continue
        test_file = parts[0]
        function = "::".join(parts[1:]).split("[", 1)[0]
        if not function.rsplit("::", 1)[-1].startswith("test_"):
            continue
        targets.setdefault(test_file, [])
        if function not in targets[test_file]:
            targets[test_file].append(function)
    return targets


def _resolve_test_file(source_root: Path, test_file: str) -> Optional[Path]:
    relative = Path(test_file.lstrip("/"))
    candidates = (
        source_root / relative,
        source_root / "tests" / relative,
        source_root / "ngts" / relative,
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(source_root)
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _function_skip_evidence(
    tree: ast.AST,
    source: str,
    functions: Sequence[str],
    display_path: Path,
) -> List[_SkipSourceEvidence]:
    wanted = set(functions)
    result: List[_SkipSourceEvidence] = []
    for qualified_name, node in _defined_functions(tree):
        if qualified_name not in wanted:
            continue
        for decorator in node.decorator_list:
            item = _decorator_evidence(decorator, source)
            if item:
                text = "{}:{} {} {}".format(
                    display_path,
                    getattr(decorator, "lineno", node.lineno),
                    qualified_name,
                    item,
                )
                result.append(
                    _SkipSourceEvidence(
                        text=text,
                        tickets=tuple(_evidence_ticket_refs(text)),
                    )
                )
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            if _call_name(child.func) != "pytest.skip":
                continue
            reason = _literal_argument(child, "reason", 0)
            if reason:
                text = "{}:{} {} pytest.skip reason={!r}".format(
                    display_path,
                    getattr(child, "lineno", node.lineno),
                    qualified_name,
                    reason,
                )
                result.append(
                    _SkipSourceEvidence(
                        text=text,
                        tickets=tuple(_evidence_ticket_refs(text)),
                    )
                )
    return result


def _defined_functions(
    tree: ast.AST,
    class_path: Tuple[str, ...] = (),
) -> Iterable[Tuple[str, ast.AST]]:
    for node in getattr(tree, "body", ()):
        if isinstance(node, ast.ClassDef):
            yield from _defined_functions(node, class_path + (node.name,))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield ("::".join(class_path + (node.name,)), node)


def _decorator_evidence(node: ast.AST, source: str) -> str:
    if not isinstance(node, ast.Call):
        return ""
    name = _call_name(node.func)
    if name not in {
        "pytest.mark.skip",
        "pytest.mark.skipif",
        "pytest.mark.xfail",
    }:
        return ""
    reason = _literal_argument(
        node,
        "reason",
        0 if name == "pytest.mark.skip" else -1,
    )
    if not reason:
        return ""
    parts = [name, "reason={!r}".format(reason)]
    if name == "pytest.mark.skipif" and node.args:
        source_segment = getattr(ast, "get_source_segment", None)
        condition = (
            source_segment(source, node.args[0])
            if source_segment is not None
            else None
        )
        if condition:
            parts.append("condition={}".format(normalize_text(condition)))
    return " ".join(parts)


def _call_name(node: ast.AST) -> str:
    parts: List[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _literal_argument(call: ast.Call, keyword: str, position: int) -> str:
    for item in call.keywords:
        if item.arg == keyword:
            return _literal_text(item.value)
    if position >= 0 and len(call.args) > position:
        return _literal_text(call.args[position])
    return ""


def _literal_text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return normalize_text(node.value)
    return ""


def _append(existing: str, addition: str) -> str:
    values = [value for value in (existing.strip(), addition.strip()) if value]
    return join_comment_blocks(values)
