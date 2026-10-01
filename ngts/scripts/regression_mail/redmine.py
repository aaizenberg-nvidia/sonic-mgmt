"""Deterministic Redmine REST review for LogAnalyzer-created SONiC bugs."""

from __future__ import annotations

import html
import json
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ngts.scripts.regression_mail.ai import (
    error_log_signature,
    extract_error_log_lines,
)
from ngts.scripts.regression_mail.models import SemanticGroup
from ngts.scripts.regression_mail.normalization import (
    canonical_test_name,
    extract_public_issue_urls,
    extract_redmine_issue_ids,
    normalize_text,
    unique_sorted,
)


_PROJECT = "sonic-design"
_BASE_URL = "https://redmine.mellanox.com"
_PROJECT_ID = 9712
_TRACKER_ID = 28
_AUTHOR_ID = 7680
_WONT_FIX_STATUS_ID = 11
_TEST = re.compile(r"\btest_[A-Za-z][A-Za-z0-9_]*(?:\[[^\]\r\n]+\])?")
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_BREAK = re.compile(r"(?i)<(?:br\s*/?|/p|/div)>")
_MAX_ERROR_CANDIDATE_LINES = 500
_MAX_ERROR_CANDIDATE_CHARS = 64000
_MAX_ERROR_CANDIDATE_LINE_CHARS = 8000
_ERROR_CANDIDATE_TAIL_LINES = 50
_ERROR_CANDIDATE_TAIL_CHARS = 16000
_REPRODUCTION = re.compile(
    r"(?i)\b(?:reproduc(?:e|ed|ible|ing)?|detected|observed|occurred|"
    r"seen|tested|version\s+is)\b"
)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PUBLIC_GITHUB_PR = re.compile(
    r"https://github\.com/(?:sonic-net|nvidia-sonic)/"
    r"[A-Za-z0-9_.-]+/pull/\d+",
    re.IGNORECASE,
)
_TEST_ISSUE_SUBJECT_MARKER = re.compile(
    r"(?i)(?:\[(?:test|tests|testing|test[- ]?infra)\]|"
    r"^\s*(?:test|testing)\s+(?:issue|bug)\b)"
)
_VERIFICATION_PROJECT_MARKER = re.compile(
    r"(?i)\bSONiC\s*-\s*Verification\b"
)


class RedmineClient:
    """Query a narrowly filtered ticket set and extract explicit occurrences."""

    def __init__(
        self,
        api_token: str,
        timeout: int = 60,
        opener: Callable[..., Any] = urlopen,
        page_size: int = 100,
        author_id: int = _AUTHOR_ID,
    ):
        self.api_token = api_token.strip()
        self.timeout = timeout
        self.opener = opener
        self.page_size = page_size
        if author_id <= 0:
            raise ValueError("Redmine author ID must be positive")
        self.author_id = author_id

    def find_log_analyzer_errors(
        self,
        version: str,
        cutoff_date: str,
    ) -> List[SemanticGroup]:
        if not self.api_token:
            raise RuntimeError("REDMINE_API_TOKEN is not configured")
        if cutoff_date and not _DATE.fullmatch(cutoff_date):
            raise ValueError("Redmine cutoff date must use YYYY-MM-DD")
        current_release = _release_version(version)
        effective_cutoff = cutoff_date or "{}-{}-01".format(
            current_release[:4],
            current_release[4:6],
        )

        open_candidates = self._list_candidates("o", effective_cutoff)
        wont_fix_candidates = self._list_candidates(
            str(_WONT_FIX_STATUS_ID),
            effective_cutoff,
        )
        open_ids = {_ticket_id(ticket) for ticket in open_candidates}
        candidates = _deduplicate_tickets(
            list(open_candidates) + list(wont_fix_candidates)
        )

        groups: List[SemanticGroup] = []
        for candidate in candidates:
            ticket_id = _ticket_id(candidate)
            ticket = self._get_ticket(ticket_id)
            status = ticket.get("status") or {}
            status_id = int(status.get("id") or 0)
            if ticket_id not in open_ids and status_id != _WONT_FIX_STATUS_ID:
                continue
            group = _ticket_group(ticket, current_release)
            if group is not None:
                groups.append(group)
        return sorted(groups, key=lambda group: group.group_id)

    def describe_tickets(
        self,
        references: Iterable[str],
    ) -> Dict[str, str]:
        """Return stable, human-readable summaries for referenced tickets."""
        if not self.api_token:
            raise RuntimeError("REDMINE_API_TOKEN is not configured")
        ticket_ids = unique_sorted(
            ticket_id
            for reference in references
            for ticket_id in extract_redmine_issue_ids(reference)
        )
        summaries: Dict[str, str] = {}
        errors: List[Exception] = []
        for ticket_id in ticket_ids:
            try:
                summaries[
                    "{}/issues/{}".format(_BASE_URL, ticket_id)
                ] = _ticket_summary(self._get_issue(int(ticket_id)))
            except Exception as error:
                errors.append(error)
        if ticket_ids and not summaries and errors:
            raise errors[0]
        return summaries

    def inspect_tickets(
        self,
        references: Iterable[str],
    ) -> Dict[str, Mapping[str, Any]]:
        """Return bounded ticket evidence and public GitHub candidates."""

        if not self.api_token:
            raise RuntimeError("REDMINE_API_TOKEN is not configured")
        ticket_ids = unique_sorted(
            ticket_id
            for reference in references
            for ticket_id in extract_redmine_issue_ids(reference)
        )
        contexts: Dict[str, Mapping[str, Any]] = {}
        errors: List[Exception] = []
        for ticket_id in ticket_ids:
            try:
                ticket = self._get_issue(
                    int(ticket_id),
                    include_journals=True,
                )
                url = "{}/issues/{}".format(_BASE_URL, ticket_id)
                contexts[url] = _ticket_context(ticket)
            except Exception as error:
                errors.append(error)
        if ticket_ids and not contexts and errors:
            raise errors[0]
        return contexts

    def _list_candidates(
        self,
        status_id: str,
        cutoff_date: str,
    ) -> Sequence[Mapping[str, Any]]:
        result: List[Mapping[str, Any]] = []
        offset = 0
        while True:
            params = {
                "tracker_id": str(_TRACKER_ID),
                "author_id": str(self.author_id),
                "status_id": status_id,
                "sort": "updated_on:desc",
                "limit": str(self.page_size),
                "offset": str(offset),
            }
            if cutoff_date:
                params["updated_on"] = ">=" + cutoff_date
            payload = self._get_json(
                "/projects/{}/issues.json?{}".format(
                    _PROJECT,
                    urlencode(params),
                )
            )
            issues = payload.get("issues") or []
            if not isinstance(issues, list):
                raise ValueError("Redmine issue list is not an array")
            page = [ticket for ticket in issues if isinstance(ticket, Mapping)]
            result.extend(page)
            offset += len(page)
            total = int(payload.get("total_count") or len(result))
            if not page or offset >= total:
                break
            if offset >= 1000:
                raise RuntimeError(
                    "Redmine candidate query exceeded the 1000-ticket safety limit"
                )
        return result

    def _get_ticket(self, ticket_id: int) -> Mapping[str, Any]:
        ticket = self._get_issue(ticket_id, include_journals=True)
        project = ticket.get("project") or {}
        tracker = ticket.get("tracker") or {}
        author = ticket.get("author") or {}
        if (
            int(project.get("id") or 0) != _PROJECT_ID or
            int(tracker.get("id") or 0) != _TRACKER_ID or
            int(author.get("id") or 0) != self.author_id
        ):
            raise ValueError(
                "Redmine issue {} escaped the structured LogAnalyzer filters".format(
                    ticket_id
                )
            )
        return ticket

    def _get_issue(
        self,
        ticket_id: int,
        include_journals: bool = False,
    ) -> Mapping[str, Any]:
        payload = self._get_json(
            "/issues/{}.json{}".format(
                ticket_id,
                "?include=journals" if include_journals else "",
            )
        )
        ticket = payload.get("issue")
        if not isinstance(ticket, Mapping):
            raise ValueError(
                "Redmine issue {} response is invalid".format(ticket_id)
            )
        return ticket

    def _get_json(self, path: str) -> Mapping[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "sonic-regression-mail/1",
            "X-Redmine-API-Key": self.api_token,
        }
        request = Request(_BASE_URL + path, headers=headers)
        with self.opener(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("Redmine returned a non-object JSON response")
        return payload


def _ticket_group(
    ticket: Mapping[str, Any],
    current_release: str,
) -> Optional[SemanticGroup]:
    ticket_id = _ticket_id(ticket)
    raw_description = ticket.get("description")
    description = _plain(raw_description)
    detected_version = _custom_field(ticket, "Detected In Version")
    evidence = ""
    occurrence_text = ""
    occurrence_source: object = ""
    if _contains_release(detected_version, current_release) or _contains_release(
        description,
        current_release,
    ):
        occurrence_text = description
        occurrence_source = raw_description
        evidence = "initial ticket"
    else:
        for journal in ticket.get("journals") or []:
            if not isinstance(journal, Mapping):
                continue
            raw_notes = journal.get("notes")
            notes = _plain(raw_notes)
            if (
                notes and
                _contains_release(notes, current_release) and
                _REPRODUCTION.search(notes)
            ):
                occurrence_text = notes
                occurrence_source = raw_notes
                evidence = "journal {}".format(journal.get("id") or "")
                break
    if not occurrence_text:
        return None

    tests = _fold_tests(_TEST.findall(occurrence_text))
    testbeds = (
        _extract_system_types(occurrence_text) or
        _extract_testbeds(occurrence_text)
    )
    url = "{}/issues/{}".format(_BASE_URL, ticket_id)
    # Keep the error signature and the occurrence metadata from the same
    # source. Mixing an RC journal's test/session with the ticket's original
    # description can make an old symptom look like the current occurrence.
    error_logs = _error_log_candidates(occurrence_source)
    internal = [
        "Redmine: {}".format(url),
        "Version evidence: {} ({})".format(current_release, evidence),
    ]
    if detected_version:
        internal.append("Detected In Version: {}".format(detected_version))
    return SemanticGroup(
        group_id="redmine-{}".format(ticket_id),
        member_ids=["redmine-{}-current-branch".format(ticket_id)],
        test_display="\n".join(tests),
        testbeds=testbeds,
        comments=error_logs,
        internal_comments="\n".join(value for value in internal if value),
        ticket_refs=[url],
        redmine_urls=[url],
    )


def _ticket_summary(ticket: Mapping[str, Any]) -> str:
    ticket_id = _ticket_id(ticket)
    project = normalize_text((ticket.get("project") or {}).get("name"))
    tracker = normalize_text((ticket.get("tracker") or {}).get("name"))
    subject = normalize_text(ticket.get("subject"))
    assignee = normalize_text((ticket.get("assigned_to") or {}).get("name"))
    status = normalize_text((ticket.get("status") or {}).get("name"))
    return "[{}] {} #{}: {} | Assignee: {} | Status: {}".format(
        project or "Unknown project",
        tracker or "Issue",
        ticket_id,
        subject or "(no subject)",
        assignee or "Unassigned",
        status or "Unknown",
    )


def _ticket_context(ticket: Mapping[str, Any]) -> Mapping[str, Any]:
    project = normalize_text((ticket.get("project") or {}).get("name"))
    tracker = normalize_text((ticket.get("tracker") or {}).get("name"))
    subject = normalize_text(ticket.get("subject"))
    status = ticket.get("status") or {}
    status_name = normalize_text(status.get("name"))
    evidence_values = [
        subject,
        _plain_multiline(ticket.get("description")),
    ]
    evidence_values.extend(
        _plain_multiline(journal.get("notes"))
        for journal in ticket.get("journals") or []
        if isinstance(journal, Mapping) and journal.get("notes")
    )
    evidence_values.extend(
        "{}: {}".format(
            normalize_text(field.get("name")),
            normalize_text(field.get("value")),
        )
        for field in ticket.get("custom_fields") or []
        if isinstance(field, Mapping) and
        (field.get("name") or field.get("value"))
    )
    active_development_items = _active_development_items(ticket)
    evidence_values.extend(active_development_items)
    evidence = "\n".join(
        value for value in evidence_values if normalize_text(value)
    )
    return {
        "summary": _ticket_summary(ticket),
        "project": project,
        "tracker": tracker,
        "subject": subject,
        "status": status_name,
        "is_closed": _ticket_status_is_closed(status),
        "closed_on": normalize_text(ticket.get("closed_on")),
        "evidence": _bounded_ticket_evidence(evidence),
        "github_issue_urls": extract_public_issue_urls(evidence),
        "github_pr_urls": unique_sorted(
            match.group(0)
            for match in _PUBLIC_GITHUB_PR.finditer(evidence)
        ),
        "active_github_pr_urls": unique_sorted(
            match.group(0)
            for value in active_development_items
            for match in _PUBLIC_GITHUB_PR.finditer(value)
        ),
        "is_test_issue": bool(
            project.casefold() == "sonic - verification" or
            _VERIFICATION_PROJECT_MARKER.search(subject) or
            re.search(r"(?i)\btest(?:ing)?\b", tracker) or
            _TEST_ISSUE_SUBJECT_MARKER.search(subject)
        ),
    }


def _active_development_items(
    ticket: Mapping[str, Any],
) -> List[str]:
    active: Dict[str, str] = {}
    for journal in ticket.get("journals") or []:
        if not isinstance(journal, Mapping):
            continue
        for detail in journal.get("details") or []:
            if (
                not isinstance(detail, Mapping) or
                normalize_text(detail.get("property")).casefold() !=
                "development_item"
            ):
                continue
            item_id = normalize_text(detail.get("name"))
            if not item_id:
                continue
            new_value = normalize_text(detail.get("new_value"))
            if new_value:
                active[item_id] = new_value
            else:
                active.pop(item_id, None)
    return unique_sorted(active.values())


def _ticket_status_is_closed(status: Mapping[str, Any]) -> bool:
    if status.get("is_closed") is True:
        return True
    name = normalize_text(status.get("name")).casefold()
    if name in {
        "closed",
        "done",
        "fixed",
        "rejected",
        "resolved",
        "won't fix",
        "wont fix",
    }:
        return True
    return bool(
        re.match(
            r"^(?:closed|done|fixed|rejected|resolved)"
            r"(?:\b|\s*[\[(])",
            name,
        )
    )


def _bounded_ticket_evidence(value: str, limit: int = 16000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return "{}\n...\n{}".format(text[:half], text[-half:])


def _release_version(version: str) -> str:
    match = re.search(
        r"(?i)(?<![A-Za-z0-9])(\d{6}_RC\.\d+)(?=[._-]|\b)",
        version,
    )
    if not match:
        raise ValueError(
            "cannot derive the SONiC RC version from {!r}".format(version)
        )
    return match.group(1).upper()


def _contains_release(value: object, release: str) -> bool:
    pattern = re.compile(
        r"(?i)(?<![A-Za-z0-9]){}(?=[._-]|\b)".format(
            re.escape(release)
        )
    )
    return bool(pattern.search(str(value or "")))


def _custom_field(ticket: Mapping[str, Any], name: str) -> str:
    for field in ticket.get("custom_fields") or []:
        if not isinstance(field, Mapping):
            continue
        if normalize_text(field.get("name")).lower() == name.lower():
            return normalize_text(field.get("value"))
    return ""


def _plain(value: object) -> str:
    text = str(value or "")
    text = _HTML_TAG.sub(" ", text)
    text = html.unescape(text)
    return normalize_text(text)


def _plain_multiline(value: object) -> str:
    text = str(value or "")
    text = _HTML_BREAK.sub("\n", text)
    text = _HTML_TAG.sub(" ", text)
    text = html.unescape(text)
    return "\n".join(
        normalize_text(line)
        for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        if normalize_text(line)
    )


def _error_log_candidates(occurrence_source: object) -> str:
    source = _plain_multiline(occurrence_source)
    result: List[str] = []
    seen = set()
    for line in extract_error_log_lines(source).splitlines():
        if len(line) > _MAX_ERROR_CANDIDATE_LINE_CHARS:
            line = line[:_MAX_ERROR_CANDIDATE_LINE_CHARS - 3].rstrip() + "..."
        key = normalize_text(error_log_signature(line)).casefold()
        if not key:
            key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(line)
    if (
        len(result) <= _MAX_ERROR_CANDIDATE_LINES and
        sum(len(line) + 1 for line in result) <=
        _MAX_ERROR_CANDIDATE_CHARS
    ):
        return "\n".join(result)

    tail: List[str] = []
    tail_chars = 0
    for line in reversed(result):
        added = len(line) + 1
        if (
            len(tail) >= _ERROR_CANDIDATE_TAIL_LINES or
            tail_chars + added > _ERROR_CANDIDATE_TAIL_CHARS
        ):
            break
        tail.append(line)
        tail_chars += added
    tail.reverse()
    head: List[str] = []
    head_limit = max(0, _MAX_ERROR_CANDIDATE_CHARS - tail_chars)
    head_count = _MAX_ERROR_CANDIDATE_LINES - len(tail)
    head_chars = 0
    head_source = result[:-len(tail)] if tail else result
    for line in head_source:
        added = len(line) + 1
        if len(head) >= head_count or head_chars + added > head_limit:
            break
        head.append(line)
        head_chars += added
    return "\n".join(head + tail)


def _extract_testbeds(value: str) -> List[str]:
    patterns = (
        re.compile(r"(?i)\bTestbed\s+name\s*:\s*([A-Za-z0-9_.-]+)"),
        re.compile(r"(?i)(?:^|\s)--testbed(?:=|\s+)([A-Za-z0-9_.-]+)"),
        re.compile(r"(?i)(?:^|\s)--setup_name(?:=|\s+)([A-Za-z0-9_.-]+)"),
    )
    values: List[str] = []
    for pattern in patterns:
        values.extend(match.group(1) for match in pattern.finditer(value))
    return unique_sorted(values)


def _extract_system_types(value: str) -> List[str]:
    pattern = re.compile(
        r"(?i)\bSystem\s+type\s*:\s*([A-Za-z0-9_.-]+)"
    )
    return unique_sorted(
        match.group(1)
        for match in pattern.finditer(value)
    )


def _fold_tests(values: Iterable[str]) -> List[str]:
    variants: Dict[str, Set[str]] = {}
    for value in values:
        canonical = canonical_test_name(value)
        if not canonical:
            continue
        variants.setdefault(canonical, set()).add(value)
    return [
        canonical + "[*]" if len(items) > 1 else sorted(items)[0]
        for canonical, items in sorted(variants.items())
    ]


def _ticket_id(ticket: Mapping[str, Any]) -> int:
    value = int(ticket.get("id") or 0)
    if value <= 0:
        raise ValueError("Redmine ticket does not contain a valid id")
    return value


def _deduplicate_tickets(
    tickets: Sequence[Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    result: Dict[int, Mapping[str, Any]] = {}
    for ticket in tickets:
        result[_ticket_id(ticket)] = ticket
    return [result[ticket_id] for ticket_id in sorted(result)]
