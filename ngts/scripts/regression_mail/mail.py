"""Report rendering, MIME construction, and STARTTLS SMTP delivery."""

from __future__ import annotations

import ast
import html
import re
import smtplib
import ssl
from collections import defaultdict
from email.message import EmailMessage
from typing import DefaultDict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ngts.scripts.regression_mail.grouping import (
    compact_test_names,
    compact_tests,
)
from ngts.scripts.regression_mail.models import (
    GenerationError,
    RenderedMessage,
    ReportModel,
    ResultRow,
    RunRequest,
    SemanticGroup,
    WorkbookSnapshot,
)
from ngts.scripts.regression_mail.normalization import (
    COMMENT_SEPARATOR,
    extract_public_issue_urls,
    normalize_text,
    strip_public_issue_urls,
)

_TABLE_PALETTES = {
    "hardware": ("#0F9ED5", "#CAEDFB", "#FFFFFF", "#FFFFFF"),
    "detail": ("#156082", "#83CCEB", "#C0E6F5", "#FFFFFF"),
}
_TABLE_FONTS = {
    "hardware": ("Arial, sans-serif", "Calibri, Arial, sans-serif"),
    "detail": (
        "'Aptos Narrow', Aptos, Arial, sans-serif",
        "'Aptos Narrow', Aptos, Arial, sans-serif",
    ),
}
_DASHBOARD_WARNING_TEXTS = (
    "Dashboard API data was not retrieved.",
    "not found in dashboard",
    "not analyzed",
    "Dashboard analysis was not obtained for some grouped failure cases.",
)
_RICH_REDMINE_ID = re.compile(
    r"(\[[^\]\r\n]{1,120}\]\s+[^#\r\n]{1,100})#(\d+)(?=:)"
)
_PUBLIC_GITHUB_URL = re.compile(
    r"https://github\.com/(?:sonic-net|nvidia-sonic)/"
    r"[A-Za-z0-9_.-]+/(?:issues|pull)/\d+",
    re.IGNORECASE,
)


class ReportRenderer:
    """Render the best available report without consulting external services."""

    def render(self, report: ReportModel) -> RenderedMessage:
        return RenderedMessage(
            subject=report_subject(report),
            plain=self._plain(report),
            html=self._html(report),
        )

    def minimal(
        self,
        report: ReportModel,
        rendering_error: GenerationError,
    ) -> RenderedMessage:
        errors = list(report.errors)
        if rendering_error not in errors:
            errors.append(rendering_error)
        lines = [
            "SONiC Regression Report (degraded)",
            "Version: {}".format(report.request.version),
            "",
            "Generation Errors:",
        ]
        lines.extend(
            "- {}: {} Impact: {}".format(
                error.stage,
                error.message,
                error.impact,
            )
            for error in errors
        )
        return RenderedMessage(
            subject=report_subject(report),
            plain="\n".join(lines) + "\n",
            html="",
        )

    def _plain(self, report: ReportModel) -> str:
        lines = [
            "Hi All",
            "",
            "We have uploaded results into the NvidiaTestData database "
            "for the image hash {}".format(
                _image_public_hash(report)
            ),
            "sonic-buildimage branch: {}".format(
                _image_branch(report)
            ),
            "sonic-mgmt public hash ({}): {}".format(
                _release_train(report.request.version),
                report.git.public_hash if report.git else "",
            ),
            "In the NvidiaTestData DB the versions are: {}".format(
                report.request.version
            ),
            "",
            "Coverage: {} (coverage reduced due to some test case "
            "skipped by known issues)".format(
                _percent(report.dashboard.coverage)
                if report.dashboard
                else ""
            ),
            "Pass rate: {}".format(
                _percent(report.dashboard.pass_rate)
                if report.dashboard
                else ""
            ),
        ]
        if report.errors:
            lines.extend(("", "Generation Errors:"))
            lines.extend(
                "- {}: {} Impact: {}".format(
                    error.stage,
                    error.message,
                    error.impact,
                )
                for error in report.errors
            )
        lines.extend(
            _plain_table(
                "Results were uploaded for the following hwsku and topology",
                ("HardwareSku", "Topology", "ReportId"),
                _hardware_rows(report),
            )
        )
        semantic = report.semantic
        if semantic:
            lines.extend(
                _plain_groups(
                    "Failure Analysis",
                    semantic.failure_groups,
                    ("Test", "Testbed", "Comments", "Comments for internal review"),
                )
            )
            lines.extend(
                _plain_skip_groups(
                    "The following tests were skipped over bugs found internally",
                    _redmine_skip_groups(semantic.skip_groups),
                )
            )
        lines.extend(
            _plain_table(
                "Github issues that affects tests coverage",
                ("Github Issue", "Skip condition", "Related test case"),
                _github_skip_rows(
                    report.workbook,
                    semantic.skip_groups if semantic else (),
                    report.redactor,
                )
                if report.workbook
                else [],
            )
        )
        lines.extend(
            _plain_internal_errors(
                semantic.internal_error_groups if semantic else ()
            )
        )
        image_pr_rows = [
            (pr.url, pr.title, pr.author, pr.state)
            for pr in report.image_prs
        ]
        if not image_pr_rows and report.rc_status:
            image_pr_rows = [
                (url, "", "", "")
                for url in report.rc_status.image_pr_urls
            ]
        lines.extend(
            _plain_prs(
                _image_pr_title(report),
                image_pr_rows,
            )
        )
        lines.extend(
            _plain_prs(
                _mgmt_pr_title(report),
                [
                    (pr.url, pr.title, pr.author, pr.state)
                    for pr in report.mgmt_prs
                ],
            )
        )
        lines.extend(("", "Regards,"))
        return "\n".join(lines) + "\n"

    def _html(self, report: ReportModel) -> str:
        body: List[str] = [
            '<div style="font-family:Aptos,Calibri,Arial,sans-serif;'
            'font-size:11pt;line-height:1.25;color:#000000;'
            'max-width:1200px">',
            "<p>Hi All</p>",
            "<p>We have uploaded results into the NvidiaTestData database "
            "for the image hash {}<br>"
            "sonic-buildimage branch: {}<br>"
            "sonic-mgmt public hash ({}): {}<br>"
            "In the NvidiaTestData DB the versions are: {}</p>".format(
                _bold_value(
                    _image_public_hash(report)
                ),
                _bold_value(_image_branch(report)),
                _bold_value(_release_train(report.request.version)),
                _bold_value(report.git.public_hash if report.git else ""),
                _bold_value(report.request.version),
            ),
        ]
        if report.errors:
            body.append(
                '<div style="border:2px solid #b91c1c;'
                'background:#fef2f2;padding:12px">'
                "{}</div>".format(
                    _section_title("Generation Errors:") +
                    _error_list(report.errors)
                )
            )

        body.append(
            "<p>Coverage: {} (coverage reduced due to some test case "
            "skipped by known issues)<br>Pass rate: {}</p>".format(
                _bold_value(
                    _percent(report.dashboard.coverage)
                    if report.dashboard
                    else ""
                ),
                _bold_value(
                    _percent(report.dashboard.pass_rate)
                    if report.dashboard
                    else ""
                )
            )
        )
        body.extend(
            (
                _section_title(
                    "Results were uploaded for the following hwsku and "
                    "topology:"
                ),
                _table(
                    ("HardwareSku", "Topology", "ReportId"),
                    _hardware_rows(report),
                    palette="hardware",
                ),
            )
        )

        semantic = report.semantic
        if semantic:
            body.extend(
                (
                    _section_title("Failure Analysis:"),
                    _failure_table(semantic.failure_groups),
                    _section_title(
                        "The following tests were skipped over bugs found "
                        "internally:"
                    ),
                    _skip_table(_redmine_skip_groups(semantic.skip_groups)),
                )
            )

        coverage_rows = (
            _github_skip_rows(
                report.workbook,
                semantic.skip_groups if semantic else (),
                report.redactor,
            )
            if report.workbook
            else []
        )
        image_pr_rows = [
            (pr.url, pr.title, pr.author, pr.state)
            for pr in report.image_prs
        ]
        if not image_pr_rows and report.rc_status:
            image_pr_rows = [
                (url, "", "", "")
                for url in report.rc_status.image_pr_urls
            ]
        body.extend(
            (
                _section_title(
                    "Github issues that affects tests coverage:"
                ),
                _table(
                    ("Github Issue", "Skip condition", "Related test case"),
                    coverage_rows,
                    palette="detail",
                ),
                _section_title("Err msgs detected internally:"),
                _internal_error_table(
                    semantic.internal_error_groups if semantic else []
                ),
                _section_title(_image_pr_title(report)),
                _pr_list(image_pr_rows),
                _section_title(_mgmt_pr_title(report)),
                _pr_list(
                    [
                        (pr.url, pr.title, pr.author, pr.state)
                        for pr in report.mgmt_prs
                    ]
                ),
            )
        )
        body.extend(("<p>Regards,</p>", "</div>"))
        return "".join(body)


def _plain_groups(
    title: str,
    groups: Sequence[SemanticGroup],
    headers: Sequence[str],
) -> List[str]:
    return _plain_table(
        title,
        headers,
        _failure_rows(groups),
    )


def _plain_skip_groups(
    title: str,
    groups: Sequence[SemanticGroup],
) -> List[str]:
    return _plain_table(
        title,
        (
            "Test case name",
            "Skipped platform or topo",
            "Comments",
            "Comments for internal review",
        ),
        _skip_rows(groups),
    )


def _plain_table(
    title: str,
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
) -> List[str]:
    values = list(rows)
    lines = ["", "{}:".format(title), " | ".join(headers)]
    if not values:
        lines.append("(none)")
        return lines
    lines.extend(
        " | ".join(str(value or "") for value in row)
        for row in values
    )
    return lines


def _redmine_skip_groups(
    groups: Sequence[SemanticGroup],
) -> List[SemanticGroup]:
    return [group for group in groups if group.redmine_urls]


def _plain_internal_errors(
    groups: Sequence[SemanticGroup],
) -> List[str]:
    return _plain_table(
        "Err msgs detected internally",
        (
            "Err msgs",
            "Testbeds",
            "Test case",
            "Comments for internal review",
        ),
        _internal_error_rows(groups),
    )


def _github_skip_rows(
    workbook: WorkbookSnapshot,
    skip_groups: Sequence[SemanticGroup] = (),
    redactor: Optional[object] = None,
) -> List[Sequence[object]]:
    rows_by_url: DefaultDict[str, List[ResultRow]] = defaultdict(list)
    conditions_by_url: DefaultDict[str, List[str]] = defaultdict(list)
    tests_by_url: DefaultDict[str, List[str]] = defaultdict(list)
    for row in workbook.skipped:
        source = str(row.values.get("message") or row.message or "")
        source_urls = extract_public_issue_urls(source)
        for url in source_urls:
            if all(
                item.record_id != row.record_id
                for item in rows_by_url[url]
            ):
                rows_by_url[url].append(row)
        for block in source.split(COMMENT_SEPARATOR):
            block_urls = extract_public_issue_urls(block)
            targets = source_urls if len(source_urls) == 1 else block_urls
            condition = strip_public_issue_urls(block)
            for url in targets:
                if condition and condition not in conditions_by_url[url]:
                    conditions_by_url[url].append(condition)
    for group in skip_groups:
        urls = extract_public_issue_urls(" ".join(group.ticket_refs))
        for url in urls:
            tests_by_url[url].extend(group.test_display.splitlines())

    result: List[Sequence[object]] = []
    urls = set(rows_by_url) | set(tests_by_url) | set(conditions_by_url)
    for url in sorted(urls):
        if tests_by_url[url]:
            tests = "\n".join(compact_test_names(tests_by_url[url]))
        else:
            tests = "\n".join(
                compact_tests(rows_by_url[url], workbook.selected_rows)
            )
        redact_test_display = getattr(
            redactor,
            "redact_test_display",
            None,
        )
        if callable(redact_test_display):
            tests = redact_test_display(tests)
        conditions = _distinct_skip_conditions(conditions_by_url[url])
        condition = (
            "\n".join(conditions)
            if conditions
            else "Issue-only skip / no extra condition shown"
        )
        result.append((url, condition, tests))
    return sorted(
        result,
        key=lambda row: (
            not bool(normalize_text(row[2])),
            normalize_text(row[2]).casefold(),
            normalize_text(row[0]).casefold(),
        ),
    )


def _distinct_skip_conditions(values: Iterable[object]) -> List[str]:
    conditions = []
    for value in values:
        condition = normalize_text(value).strip(" -;,")
        condition = re.sub(
            r"(?i)^(?:and|or)\s+|\s+(?:and|or)$",
            "",
            condition,
        )
        if condition and condition not in conditions:
            conditions.append(condition)

    result = []
    parsed = [_condition_dnf(condition) for condition in conditions]
    for index, condition in enumerate(conditions):
        redundant = False
        for other_index, other in enumerate(conditions):
            if index == other_index:
                continue
            covered = _condition_is_covered(
                condition,
                parsed[index],
                other,
                parsed[other_index],
            )
            if not covered:
                continue
            reverse = _condition_is_covered(
                other,
                parsed[other_index],
                condition,
                parsed[index],
            )
            if not reverse or (len(condition), index) > (
                len(other),
                other_index,
            ):
                redundant = True
                break
        if not redundant:
            result.append(condition)
    return result


def _condition_dnf(value: str) -> Optional[List[frozenset[str]]]:
    try:
        expression = ast.parse(value, mode="eval").body
        return _expression_dnf(expression)
    except (SyntaxError, ValueError):
        return None


def _expression_dnf(node: ast.AST) -> List[frozenset[str]]:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        terms = [
            term
            for child in node.values
            for term in _expression_dnf(child)
        ]
        if len(terms) > 64:
            raise ValueError("condition is too complex")
        return terms
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        terms = [frozenset()]
        for child in node.values:
            child_terms = _expression_dnf(child)
            terms = [
                left | right
                for left in terms
                for right in child_terms
            ]
            if len(terms) > 64:
                raise ValueError("condition is too complex")
        return terms
    return [
        frozenset(
            {
                ast.dump(
                    node,
                    annotate_fields=False,
                    include_attributes=False,
                )
            }
        )
    ]


def _condition_is_covered(
    candidate: str,
    candidate_dnf: Optional[List[frozenset[str]]],
    alternative: str,
    alternative_dnf: Optional[List[frozenset[str]]],
) -> bool:
    if candidate_dnf is not None and alternative_dnf is not None:
        return all(
            any(other_term.issubset(term) for other_term in alternative_dnf)
            for term in candidate_dnf
        )
    candidate_key = re.sub(r"[\s()]+", "", candidate).casefold()
    alternative_key = re.sub(r"[\s()]+", "", alternative).casefold()
    return (
        candidate_key == alternative_key or
        alternative_key.startswith(candidate_key + "or")
    )


def _plain_prs(
    title: str,
    rows: Sequence[Sequence[object]],
) -> List[str]:
    normalized_title = title.rstrip()
    lines = [
        "",
        (
            normalized_title
            if normalized_title.endswith(":")
            else normalized_title + ":"
        ),
    ]
    if not rows:
        lines.append("(none)")
        return lines
    for row in rows:
        url, pr_title = row[0], row[1]
        lines.append("{} - {}".format(url, pr_title))
    return lines


def _failure_table(groups: Sequence[SemanticGroup]) -> str:
    ordered_groups = _groups_by_test_case(groups)
    html_cells = {
        (index, 3): _failure_internal_comment_html(group)
        for index, group in enumerate(ordered_groups)
        if group.internal_review_warning
    }
    return _table(
        (
            "Test",
            "Testbed",
            "Comments",
            "Comments for internal review",
        ),
        _failure_rows(ordered_groups),
        red_headers=(3,),
        html_cells=html_cells,
        column_widths={0: 360, 1: 320, 2: 360, 3: 520},
        palette="detail",
    )


def _skip_table(groups: Sequence[SemanticGroup]) -> str:
    return _table(
        (
            "Test case name",
            "Skipped platform or topo",
            "Comments",
            "Comments for internal review",
        ),
        _skip_rows(groups),
        red_headers=(3,),
        column_widths={0: 360, 1: 320, 2: 360, 3: 520},
        palette="detail",
    )


def _internal_error_table(groups: Sequence[SemanticGroup]) -> str:
    return _table(
        (
            "Err msgs",
            "Testbeds",
            "Test case",
            "Comments for internal review",
        ),
        _internal_error_rows(groups),
        red_headers=(3,),
        column_widths={0: 520, 1: 320, 2: 360, 3: 520},
        palette="detail",
    )


def _failure_rows(
    groups: Sequence[SemanticGroup],
) -> List[Sequence[object]]:
    return [
        (
            group.test_display,
            "\n".join(group.testbeds),
            group.comments,
            group.internal_comments,
        )
        for group in _groups_by_test_case(groups)
    ]


def _skip_rows(
    groups: Sequence[SemanticGroup],
) -> List[Sequence[object]]:
    return [
        (
            group.test_display,
            "\n".join(group.testbeds),
            group.comments,
            group.internal_comments,
        )
        for group in _groups_by_test_case(groups)
    ]


def _internal_error_rows(
    groups: Sequence[SemanticGroup],
) -> List[Sequence[object]]:
    return [
        (
            group.comments,
            "\n".join(group.testbeds),
            group.test_display,
            group.internal_comments,
        )
        for group in _groups_by_test_case(groups)
    ]


def _groups_by_test_case(
    groups: Sequence[SemanticGroup],
) -> List[SemanticGroup]:
    return sorted(
        groups,
        key=lambda group: (
            not bool(normalize_text(group.test_display)),
            normalize_text(group.test_display).casefold(),
            group.group_id,
        ),
    )


def _pr_list(rows: Sequence[Sequence[object]]) -> str:
    if not rows:
        return "<p>No data available</p>"
    return "<p>{}</p>".format(
        "<br>".join(
            '<a href="{url}">{url}</a> - {title}'.format(
                url=html.escape(str(row[0] or ""), quote=True),
                title=_e(row[1]),
            )
            for row in rows
        )
    )


def _error_list(errors: Sequence[GenerationError]) -> str:
    return "<ul>{}</ul>".format(
        "".join(
            "<li><strong>{}</strong>: {} "
            "<em>Impact: {}</em></li>".format(
                _e(error.stage),
                _e(error.message),
                _e(error.impact),
            )
            for error in errors
        )
    )


def _table(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    red_headers: Sequence[int] = (),
    red_cells: Iterable[Sequence[int]] = (),
    html_cells: Optional[Mapping[Tuple[int, int], str]] = None,
    first_column_width: int = 360,
    column_widths: Optional[Mapping[int, int]] = None,
    palette: str = "detail",
) -> str:
    row_values = list(rows)
    try:
        header_color, primary_color, alternate_color, header_text_color = (
            _TABLE_PALETTES[palette]
        )
        header_font, body_font = _TABLE_FONTS[palette]
    except KeyError as error:
        raise ValueError("unknown table palette: {}".format(palette)) from error
    red_cell_indexes = {
        (int(value[0]), int(value[1]))
        for value in red_cells
    }
    html_cell_values = dict(html_cells or {})
    widths = {0: first_column_width}
    widths.update({
        int(index): int(width)
        for index, width in (column_widths or {}).items()
        if int(width) > 0
    })

    def width_attribute(index: int) -> str:
        width = widths.get(index)
        return ' width="{}"'.format(width) if width else ""

    def width_style(index: int) -> str:
        width = widths.get(index)
        if not width:
            return ""
        return (
            ";width:{0}px;min-width:{0}px;max-width:{0}px;"
            "word-break:break-word"
        ).format(width)

    base_style = (
        "border:1px solid #bbb;padding:6px;text-align:left;"
        "vertical-align:top"
    )
    header_style = "{};font-family:{};font-size:11pt".format(
        base_style,
        header_font,
    )
    body_style = "{};font-family:{};font-size:11pt;color:#000000".format(
        base_style,
        body_font,
    )
    header_html = "".join(
        '<th{} style="{}{}{}">{}</th>'.format(
            width_attribute(index),
            header_style,
            width_style(index),
            ";background-color:{};color:{};font-weight:bold{}".format(
                header_color,
                header_text_color,
                ";color:#c00000" if index in red_headers else "",
            ),
            _e(value),
        )
        for index, value in enumerate(headers)
    )
    if not row_values:
        body_html = (
            '<tr><td style="{};background-color:{}" colspan="{}">'
            "No data available</td></tr>"
        ).format(body_style, primary_color, len(headers))
    else:
        body_html = "".join(
            "<tr>{}</tr>".format(
                "".join(
                    '<td{} style="{}{}{}">{}</td>'.format(
                        width_attribute(column_index),
                        body_style,
                        width_style(column_index),
                        ";background-color:{}{}".format(
                            primary_color
                            if row_index % 2 == 0
                            else alternate_color,
                            ";color:#c00000"
                            if (row_index, column_index) in red_cell_indexes
                            else "",
                        ),
                        html_cell_values.get(
                            (row_index, column_index),
                            _e(value),
                        ),
                    )
                    for column_index, value in enumerate(row)
                )
            )
            for row_index, row in enumerate(row_values)
        )
    table_width = max(1000, sum(widths.values()))
    column_html = "".join(
        '<col width="{0}" style="width:{0}px;min-width:{0}px;'
        'max-width:{0}px">'.format(widths[index])
        if index in widths
        else "<col>"
        for index in range(len(headers))
    )
    return (
        '<table role="presentation" cellspacing="0" cellpadding="0" '
        'width="{0}" style="border-collapse:collapse;width:{0}px;'
        'min-width:{0}px;max-width:{0}px;table-layout:fixed;'
        'margin-bottom:16px"><colgroup>{1}</colgroup>'
        "<thead><tr>{2}</tr></thead><tbody>{3}</tbody></table>".format(
            table_width,
            column_html,
            header_html,
            body_html,
        )
    )


def _section_title(value: object) -> str:
    blank = (
        '<p style="margin:0;font-family:Aptos,Calibri,Arial,sans-serif;'
        'font-size:11pt;line-height:1.25">&nbsp;</p>'
    )
    title = (
        '<p style="margin:0 0 8px 0;font-family:Aptos,Calibri,Arial,'
        'sans-serif;font-size:11pt;line-height:1.25;color:#000000">'
        "<strong>{}</strong></p>"
    ).format(_e(value))
    return blank + blank + title


def report_subject(report: ReportModel) -> str:
    return (
        "Nvidia | Uploading test results to a Kusto/Azure Data | "
        "{} | {} | {}"
    ).format(
        _release_train(report.request.version),
        _image_public_hash(report),
        report.git.public_hash if report.git else "",
    )


def _image_pr_title(report: ReportModel) -> str:
    value = _image_public_hash(report)
    return "Additional PR(s) on top of the public hash: {}".format(value)


def _mgmt_pr_title(report: ReportModel) -> str:
    value = report.git.public_hash if report.git else ""
    return (
        "Additional PR(s) on top of the sonic-mgmt {} public hash: {}"
    ).format(_release_train(report.request.version), value)


def _release_train(version: object) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?=_RC\b)", str(version or ""))
    return match.group(1) if match else ""


def _image_branch(report: ReportModel) -> str:
    if report.rc_status and report.rc_status.image_branch:
        return report.rc_status.image_branch
    return _release_train(report.request.version)


def _image_public_hash(report: ReportModel) -> str:
    if report.rc_status and report.rc_status.image_public_hash:
        return report.rc_status.image_public_hash
    return "Unavailable (RC_STATUS failed)"


def _percent(value: object) -> str:
    if value is None:
        return ""
    formatted = "{:.2f}".format(float(value)).rstrip("0").rstrip(".")
    return "{}%".format(formatted)


def _hardware_rows(report: ReportModel) -> List[Sequence[object]]:
    if not report.workbook:
        return []
    report_ids: DefaultDict[Tuple[str, str], List[str]] = defaultdict(list)
    for row in report.workbook.selected_rows:
        pair = (row.hwsku, row.topology)
        report_id = next(
            (
                normalize_text(value)
                for key, value in row.values.items()
                if normalize_text(key).lower().replace("_", " ")
                in {"report id", "reportid"} and
                normalize_text(value)
            ),
            "",
        )
        if report_id and report_id not in report_ids[pair]:
            report_ids[pair].append(report_id)
    return [
        (hardware_sku, topology, report_id)
        for hardware_sku, topology in sorted(
            report.workbook.hardware_pairs,
            key=lambda pair: (
                normalize_text(pair[0]).casefold(),
                normalize_text(pair[1]).casefold(),
            ),
        )
        for report_id in (
            sorted(
                report_ids[(hardware_sku, topology)],
                key=str.casefold,
            ) or
            [""]
        )
    ]


def _e(value: object) -> str:
    text = str(value or "")
    result = []
    offset = 0
    for match in _RICH_REDMINE_ID.finditer(text):
        result.append(_link_public_github_urls(text[offset:match.start()]))
        result.append(html.escape(match.group(1), quote=True))
        issue_id = match.group(2)
        result.append(
            '<a href="https://redmine.mellanox.com/issues/{0}" '
            'style="color:#0563c1;text-decoration:underline">#{0}</a>'.format(
                issue_id
            )
        )
        offset = match.end()
    result.append(_link_public_github_urls(text[offset:]))
    return "".join(result).replace("\n", "<br>")


def _link_public_github_urls(value: str) -> str:
    result = []
    offset = 0
    for match in _PUBLIC_GITHUB_URL.finditer(value):
        result.append(html.escape(value[offset:match.start()], quote=True))
        url = match.group(0)
        escaped = html.escape(url, quote=True)
        result.append(
            '<a href="{0}" style="color:#0563c1;'
            'text-decoration:underline">{0}</a>'.format(escaped)
        )
        offset = match.end()
    result.append(html.escape(value[offset:], quote=True))
    return "".join(result)


def _failure_internal_comment_html(group: SemanticGroup) -> str:
    rendered = _e(group.internal_comments)
    for warning in _DASHBOARD_WARNING_TEXTS:
        escaped = _e(warning)
        rendered = rendered.replace(
            escaped,
            '<span style="color:#c00000">{}</span>'.format(escaped),
        )
    return rendered


def _bold_value(value: object) -> str:
    return '<strong style="color:#000000;font-weight:700">{}</strong>'.format(
        _e(value)
    )


def build_message(
    rendered: RenderedMessage,
    request: RunRequest,
    sender: str,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = rendered.subject
    message["From"] = sender
    message["To"] = ", ".join(request.to)
    if request.cc:
        message["Cc"] = ", ".join(request.cc)
    message.set_content(rendered.plain)
    if rendered.html:
        message.add_alternative(rendered.html, subtype="html")
    return message


class SmtpTransport:
    """Deliver a prepared message without silently downgrading TLS."""

    def __init__(self, host: str, port: int, timeout: int = 120):
        self.host = host
        self.port = port
        self.timeout = timeout

    def send(self, message: EmailMessage, request: RunRequest) -> None:
        recipients = list(request.to) + [address for address in request.cc if address not in request.to]
        server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        try:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            refused = server.send_message(message, to_addrs=recipients)
        finally:
            server.close()
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
