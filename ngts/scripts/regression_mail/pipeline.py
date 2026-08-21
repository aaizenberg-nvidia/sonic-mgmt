"""Synchronous, error-isolated regression mail workflow."""

from __future__ import annotations

import hashlib
import sys
import re
from collections import defaultdict
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, TextIO, Tuple

from ngts.scripts.regression_mail.ai import (
    AiCommentSummarizer,
    deduplicate_error_logs,
    error_log_signature,
    sanitize_ai_summary,
    sanitize_error_logs,
)
from ngts.scripts.regression_mail.config import Settings
from ngts.scripts.regression_mail.git_history import GitHistoryResolver, GitHubClient
from ngts.scripts.regression_mail.grouping import (
    build_deterministic_report,
    compact_test_names,
    compact_test_members,
)
from ngts.scripts.regression_mail.mail import (
    ReportRenderer,
    SmtpTransport,
    build_message,
    report_subject,
)
from ngts.scripts.regression_mail.models import (
    DashboardAnalysis,
    ExitCode,
    GenerationError,
    PullRequest,
    ReportModel,
    RenderedMessage,
    RunRequest,
    SemanticGroup,
    WorkbookSnapshot,
)
from ngts.scripts.regression_mail.normalization import (
    COMMENT_SEPARATOR,
    canonical_test_name,
    extract_internal_change_urls,
    extract_public_issue_urls,
    extract_public_pr_urls,
    extract_redmine_urls,
    join_distinct_analysis_blocks,
    join_comment_blocks,
    normalize_text,
    strip_redmine_references,
    unique_sorted,
)
from ngts.scripts.regression_mail.redaction import (
    SetupRedactor,
    extract_setup_names,
)
from ngts.scripts.regression_mail.redmine import RedmineClient
from ngts.scripts.regression_mail.regression_api import RegressionReportClient
from ngts.scripts.regression_mail.workbook import ArtifactWriter, WorkbookLoader


_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(authorization|api[-_ ]?key|token|password|secret)\b"
        r"(?:\s+[A-Za-z_-]+){0,3}\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)([?&](?:access_token|token|api_key|key)=)[^&\s]+"),
)
_WHITESPACE = re.compile(r"\s+")
_DASHBOARD_STATUS_MESSAGES = {
    "ambiguous": "Dashboard returned conflicting analysis records.",
    "unavailable": "Dashboard API data was not retrieved.",
    "not_found": "not found in dashboard",
    "no_analysis": "not analyzed",
    "partial": (
        "Dashboard analysis was not obtained for some grouped failure cases."
    ),
    "unknown": "Dashboard analysis status is unknown.",
}
_MISSING_DASHBOARD_STATUSES = {
    "ambiguous",
    "no_analysis",
    "not_found",
    "partial",
    "unavailable",
    "unknown",
}
_DASHBOARD_PLACEHOLDER_STATUSES = {
    "not analyzed": "no_analysis",
    "not found": "not_found",
    "not found in dashboard": "not_found",
}
_RELEASE_TRAIN = re.compile(r"(?<!\d)(\d{6})(?=_RC\b)", re.IGNORECASE)
_SENSITIVE_INTERNAL_TECHNOLOGY = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:SAI|BIOS|FW)(?![A-Za-z0-9])"
)
_VERIFICATION_PROJECT_MARKER = re.compile(
    r"(?i)\bSONiC\s*-\s*Verification\b"
)
_NGTS_TEST_PATH = re.compile(r"(?i)(?:^|[\\/])ngts[\\/]")
_TEST_FUNCTION_DEFINITION = re.compile(
    r"(?m)^[ \t]*(?:async[ \t]+)?def[ \t]+"
    r"(test_[A-Za-z][A-Za-z0-9_]*)[ \t]*\("
)


@dataclass
class PipelineDependencies:
    """Injectable boundaries for offline and live-sink testing."""

    workbook: Any
    dashboard: Any
    git: Any
    github: Any
    redmine: Any
    ai: Any
    artifacts: Any
    renderer: Any
    transport: Any
    mime_builder: Callable[..., EmailMessage] = build_message


def build_default_dependencies(settings: Settings) -> PipelineDependencies:
    github = GitHubClient(
        settings.github_token,
        timeout=settings.http_timeout_seconds,
        rc_status_repository=settings.rc_status_repository,
        rc_status_ref=settings.rc_status_ref,
        rc_status_tag=settings.rc_status_tag,
    )
    return PipelineDependencies(
        workbook=WorkbookLoader(),
        dashboard=RegressionReportClient(
            settings.api_base_url,
            timeout=settings.http_timeout_seconds,
        ),
        git=GitHistoryResolver(settings.repo_root),
        github=github,
        redmine=RedmineClient(
            settings.redmine_api_token,
            timeout=settings.http_timeout_seconds,
            author_id=settings.redmine_author_id,
        ),
        ai=AiCommentSummarizer(
            settings.inference_api_key,
            settings.inference_base_url,
            settings.inference_model,
            batch_size=settings.inference_batch_size,
            timeout=settings.inference_timeout_seconds,
        ),
        artifacts=ArtifactWriter(),
        renderer=ReportRenderer(),
        transport=SmtpTransport(settings.smtp_host, settings.smtp_port),
    )


def run(
    request: RunRequest,
    settings: Settings,
    dependencies: Optional[PipelineDependencies] = None,
    stderr: TextIO = sys.stderr,
) -> int:
    deps = dependencies or build_default_dependencies(settings)
    report = ReportModel(request=request)
    message: Optional[EmailMessage] = None

    try:
        report.workbook = _attempt(
            report,
            "Excel",
            ExitCode.INVALID_INPUT,
            "Excel-derived report sections are unavailable.",
            lambda: deps.workbook.load(request.excel_path, request.version),
            settings,
        )

        if report.workbook:
            report.dashboard = _attempt(
                report,
                "Regression Report API",
                ExitCode.EXTERNAL_SOURCE,
                "Coverage, pass rate, and engineer analysis are unavailable.",
                lambda: deps.dashboard.fetch(request.version),
                settings,
            )
            if report.dashboard:
                apply_dashboard_analysis(report.workbook, report.dashboard)
            else:
                mark_dashboard_unavailable(report.workbook)
            report.redactor = _build_setup_redactor(report)

        report.rc_status = _attempt(
            report,
            "RC_STATUS",
            ExitCode.EXTERNAL_SOURCE,
            "Image metadata and image pull requests are unavailable.",
            lambda: deps.github.fetch_rc_status(request.version),
            settings,
        )
        _collect_image_prs(report, deps, settings)
        report.git = _attempt(
            report,
            "Git history",
            ExitCode.INVALID_INPUT,
            "sonic-mgmt branch, public hash, and source-dependent analysis are unavailable.",
            lambda: deps.git.resolve(request.version),
            settings,
        )

        _collect_mgmt_prs(report, deps, settings)
        _refresh_dashboard_analysis_if_needed(
            report,
            deps,
            settings,
        )

        if report.workbook:
            report.semantic = build_deterministic_report(report.workbook)
            internal_errors = _attempt(
                report,
                "Redmine LogAnalyzer review",
                ExitCode.EXTERNAL_SOURCE,
                "Internally detected LogAnalyzer errors are unavailable.",
                lambda: deps.redmine.find_log_analyzer_errors(
                    request.version,
                    report.git.branch_cutoff_date if report.git else "",
                ),
                settings,
            )
            if internal_errors is not None:
                internal_errors = _filter_ngts_internal_error_tests(
                    internal_errors,
                    settings.repo_root,
                )
                report.semantic.internal_error_groups = internal_errors
                report.redactor = _build_setup_redactor(
                    report,
                    (
                        testbed
                        for group in internal_errors
                        for testbed in group.testbeds
                    ),
                )
            if report.redactor is not None:
                report.redactor.redact_semantic(report.semantic)
            ticket_urls = _redmine_ticket_urls(report.semantic)
            ticket_summaries: Mapping[str, str] = {}
            ticket_contexts: Mapping[str, Mapping[str, Any]] = {}
            if ticket_urls:
                inspect_tickets = getattr(
                    deps.redmine,
                    "inspect_tickets",
                    None,
                )
                if callable(inspect_tickets):
                    inspected = _attempt(
                        report,
                        "Redmine ticket details",
                        ExitCode.EXTERNAL_SOURCE,
                        (
                            "Redmine references retain their existing "
                            "evidence when metadata is unavailable."
                        ),
                        lambda: inspect_tickets(ticket_urls),
                        settings,
                    )
                    if isinstance(inspected, Mapping):
                        ticket_contexts = {
                            normalize_text(url): context
                            for url, context in inspected.items()
                            if normalize_text(url) and
                            isinstance(context, Mapping)
                        }
                        ticket_summaries = {
                            url: normalize_text(context.get("summary"))
                            for url, context in ticket_contexts.items()
                            if normalize_text(context.get("summary"))
                        }
                else:
                    described = _attempt(
                        report,
                        "Redmine ticket details",
                        ExitCode.EXTERNAL_SOURCE,
                        (
                            "Redmine references retain their existing "
                            "evidence when metadata is unavailable."
                        ),
                        lambda: deps.redmine.describe_tickets(ticket_urls),
                        settings,
                    )
                    if isinstance(described, Mapping):
                        ticket_summaries = described
            _classify_and_merge_failure_groups(report, deps, settings)
            _summarize_comments(report, deps, settings)
            _summarize_internal_errors(report, deps, settings)
            public_link_decisions = _review_failure_public_links(
                report,
                deps,
                settings,
            )
            _apply_dashboard_status_warnings(report.semantic)
            link_decisions = _review_redmine_links(
                report,
                deps,
                ticket_contexts,
                settings,
            )
            _prepare_redmine_comments(
                report.semantic,
                ticket_summaries,
                report.redactor,
            )
            _apply_failure_comment_policy(
                report.semantic,
                ticket_contexts,
                link_decisions,
                public_link_decisions,
            )

        if report.workbook and report.semantic:
            _attempt(
                report,
                "skips.json",
                ExitCode.INVALID_INPUT,
                "The skip mapping file was not updated.",
                lambda: deps.artifacts.write_skips(report.workbook, report.semantic),
                settings,
            )

        rendered = _render(report, deps, settings)
        message = _build_mime(report, rendered, deps, settings)
        message = _ensure_redacted_mime(report, message, deps, settings)
        if message is None:
            stderr.write("regression mail delivery failed: unable to construct minimal MIME message\n")
            return int(ExitCode.DELIVERY)

        try:
            deps.transport.send(message, request)
        except Exception as error:
            _write_diagnostic(settings, "SMTP", error)
            stderr.write(
                "regression mail delivery failed: {}\n".format(
                    sanitize_error(
                        error,
                        secrets=_setting_secrets(settings),
                    )
                )
            )
            return int(ExitCode.DELIVERY)

        if report.errors:
            failed_stages = ", ".join(error.stage for error in report.errors)
            stderr.write(
                "regression mail sent in degraded mode; {} stage(s) failed; "
                "exit={}; stages={}\n".format(
                    len(report.errors),
                    int(report.exit_code),
                    failed_stages,
                )
            )
        return int(report.exit_code)
    finally:
        if hasattr(deps.git, "cleanup"):
            deps.git.cleanup(report.git)


def _collect_mgmt_prs(
    report: ReportModel,
    deps: PipelineDependencies,
    settings: Settings,
) -> None:
    authors = list(settings.github_authors)
    candidates = (
        _attempt(
            report,
            "GitHub open pull request inventory",
            ExitCode.EXTERNAL_SOURCE,
            "Additional sonic-mgmt pull request candidates are unavailable.",
            lambda: deps.github.get_open_mgmt_prs_by_authors(authors),
            settings,
        )
        if authors
        else None
    )
    matched = (
        _eligible_mgmt_prs(
            candidates,
            authors,
            _target_release_label(report.request.version),
        )
        if candidates is not None
        else []
    )

    report.mgmt_prs = sorted(
        {
            pull_request.url: pull_request
            for pull_request in matched
        }.values(),
        key=lambda pull_request: pull_request.url,
    )


def _target_release_label(version: object) -> str:
    match = _RELEASE_TRAIN.search(str(version or ""))
    if not match:
        raise ValueError(
            "cannot derive sonic-mgmt target release label from {!r}".format(
                version
            )
        )
    return match.group(1)


def _eligible_mgmt_prs(
    pull_requests: Iterable[PullRequest],
    authors: Iterable[str],
    target_release: str,
) -> List[PullRequest]:
    author_keys = {
        normalize_text(author).casefold()
        for author in authors
        if normalize_text(author)
    }
    return [
        pull_request
        for pull_request in pull_requests
        if pull_request.author.casefold() in author_keys and
        pull_request.state.casefold() == "open" and
        not pull_request.draft and
        any(
            target_release in normalize_text(label)
            for label in pull_request.labels
        )
    ]


def _collect_image_prs(
    report: ReportModel,
    deps: PipelineDependencies,
    settings: Settings,
) -> None:
    if not report.rc_status or not report.rc_status.image_pr_urls:
        return
    validated = _attempt(
        report,
        "GitHub image pull request metadata",
        ExitCode.EXTERNAL_SOURCE,
        "Image pull request titles are unavailable.",
        lambda: deps.github.get_image_prs(report.rc_status.image_pr_urls),
        settings,
    )
    if validated is not None:
        report.image_prs = sorted(
            validated,
            key=lambda pull_request: pull_request.url,
        )


def _refresh_dashboard_analysis_if_needed(
    report: ReportModel,
    deps: PipelineDependencies,
    settings: Settings,
) -> None:
    if report.workbook is None or report.dashboard is None:
        return
    unresolved = {
        "no_analysis",
        "not_found",
    }
    unresolved_ids = {
        record_id
        for record_id, status
        in report.workbook.dashboard_statuses.items()
        if status in unresolved
    }
    if not unresolved_ids:
        return
    try:
        refreshed = deps.dashboard.fetch(report.request.version)
    except Exception as error:
        _write_diagnostic(settings, "Regression Report API refresh", error)
        return
    report.dashboard = refreshed
    apply_dashboard_analysis(
        report.workbook,
        refreshed,
        record_ids=unresolved_ids,
    )
    report.redactor = _build_setup_redactor(report)


def _build_setup_redactor(
    report: ReportModel,
    additional_testbeds: Iterable[str] = (),
) -> SetupRedactor:
    setup_platforms = dict(
        getattr(report.dashboard, "setup_platforms", {})
        if report.dashboard
        else {}
    )
    setup_testbeds = dict(
        getattr(report.dashboard, "setup_testbeds", {})
        if report.dashboard
        else {}
    )
    free_text: List[object] = []
    if report.workbook:
        workbook_platforms = dict(
            getattr(report.workbook, "setup_platforms", {})
        )
        workbook_testbeds = dict(
            getattr(report.workbook, "setup_testbeds", {})
        )
        for setup_name, platform in workbook_platforms.items():
            if normalize_text(platform):
                setup_platforms[setup_name] = platform
            else:
                setup_platforms.setdefault(setup_name, "")
        for setup_name, testbed in workbook_testbeds.items():
            if normalize_text(workbook_platforms.get(setup_name)):
                setup_testbeds[setup_name] = testbed
            elif not normalize_text(setup_testbeds.get(setup_name)):
                setup_testbeds[setup_name] = testbed
        free_text.extend(report.workbook.row_comments.values())
        free_text.extend(row.message for row in report.workbook.selected_rows)
        free_text.extend(row.test_name for row in report.workbook.selected_rows)
        free_text.extend(
            row.sanitized_testname
            for row in report.workbook.selected_rows
        )
    if report.semantic:
        for group in (
            list(report.semantic.failure_groups) +
            list(report.semantic.skip_groups) +
            list(report.semantic.internal_error_groups)
        ):
            free_text.extend(
                (
                    group.comments,
                    group.internal_comments,
                    group.cause_evidence,
                    group.test_display,
                )
            )
    for setup_name in extract_setup_names(free_text):
        setup_platforms.setdefault(setup_name, "")
        setup_testbeds.setdefault(setup_name, "")
    for testbed in additional_testbeds:
        setup_platforms.setdefault(testbed, "")
        setup_testbeds.setdefault(testbed, "")
    return SetupRedactor(setup_platforms, setup_testbeds)


def _summarize_comments(
    report: ReportModel,
    deps: PipelineDependencies,
    settings: Settings,
) -> None:
    if report.semantic is None:
        return
    all_groups = list(report.semantic.failure_groups)
    for group in all_groups:
        group.comments = ""
    groups = [
        group
        for group in all_groups
        if _has_summary_analysis(group.internal_comments)
    ]
    if not groups:
        return
    redactor = report.redactor or SetupRedactor({})
    fallbacks = _attempt(
        report,
        "Deterministic comment redaction",
        ExitCode.INVALID_INPUT,
        "Empty comments are used when a deterministic fallback cannot be sanitized.",
        lambda: {
            group.group_id: sanitize_ai_summary(
                group.internal_comments,
                group,
                redactor,
            )
            for group in groups
        },
        settings,
    ) or {group.group_id: "" for group in groups}

    def summarize() -> Dict[str, str]:
        result = deps.ai.summarize(groups, redactor)
        expected = {group.group_id for group in groups}
        if not isinstance(result, dict) or set(result) != expected:
            raise ValueError("AI response group IDs do not match the request")
        if any(not isinstance(result[group_id], str) for group_id in expected):
            raise ValueError("AI summaries must be strings")
        return {
            group.group_id: sanitize_ai_summary(
                result[group.group_id],
                group,
                redactor,
            )
            for group in groups
        }

    summaries = _attempt(
        report,
        "AI comment summary",
        ExitCode.EXTERNAL_SOURCE,
        "Deterministic desensitized dashboard analysis is used for Comments.",
        summarize,
        settings,
    )
    selected = summaries if summaries is not None else fallbacks
    for group in groups:
        summary = selected[group.group_id]
        group.comments = summary


def _classify_and_merge_failure_groups(
    report: ReportModel,
    deps: PipelineDependencies,
    settings: Settings,
) -> None:
    if report.semantic is None or report.workbook is None:
        return
    rows_by_id = {
        row.record_id: row
        for row in report.workbook.selected_rows
    }
    file_keys: Dict[str, str] = {}
    candidates: List[SemanticGroup] = []
    for group in report.semantic.failure_groups:
        if _failure_group_ticket_refs(group):
            continue
        files = {
            rows_by_id[member_id].test_name.split("::", 1)[0]
            for member_id in group.member_ids
            if member_id in rows_by_id
        }
        if not files:
            continue
        file_keys[group.group_id] = "\n".join(sorted(files))
        candidates.append(group)

    if len(candidates) < 2:
        return
    redactor = report.redactor or SetupRedactor({})

    def classify() -> Dict[str, str]:
        result = deps.ai.classify_failure_causes(
            candidates,
            file_keys,
            redactor,
        )
        expected = {group.group_id for group in candidates}
        if not isinstance(result, dict) or set(result) != expected:
            raise ValueError(
                "AI failure cause group IDs do not match the request"
            )
        if any(
            not isinstance(result[group_id], str) or
            not normalize_text(result[group_id])
            for group_id in expected
        ):
            raise ValueError("AI failure cause keys must be non-empty strings")
        return result

    cause_keys = _attempt(
        report,
        "AI failure cause classification",
        ExitCode.EXTERNAL_SOURCE,
        "Failure rows use conservative structural cause grouping.",
        classify,
        settings,
    )
    if cause_keys is None:
        return

    merge_buckets: Dict[Tuple[str, ...], List[SemanticGroup]] = {}
    for group in report.semantic.failure_groups:
        ticket_refs = _failure_group_ticket_refs(group)
        analysis_scope = _failure_group_analysis_scope(group)
        if ticket_refs:
            key = ("ticketed", analysis_scope, *ticket_refs)
        elif analysis_scope == "missing":
            cause_key = cause_keys.get(group.group_id, group.group_id)
            if normalize_text(cause_key).casefold().startswith(
                "unclassified:"
            ):
                cause_key = "unclassified:" + group.group_id
            key = (
                "unticketed-missing",
                file_keys.get(group.group_id, ""),
                normalize_text(cause_key).casefold(),
            )
        else:
            cause_key = cause_keys.get(group.group_id, group.group_id)
            if normalize_text(cause_key).casefold().startswith(
                "unclassified:"
            ):
                cause_key = "unclassified:" + group.group_id
            key = (
                "unticketed",
                analysis_scope,
                normalize_text(cause_key).casefold(),
            )
        merge_buckets.setdefault(key, []).append(group)

    merged: List[SemanticGroup] = []
    for sources in merge_buckets.values():
        if len(sources) == 1:
            merged.append(sources[0])
            continue
        member_ids = sorted({
            member_id
            for source in sources
            for member_id in source.member_ids
        })
        digest = hashlib.sha256(
            "\x1f".join(member_ids).encode("utf-8")
        ).hexdigest()[:16]
        merged.append(
            SemanticGroup(
                group_id="failure-ai-{}".format(digest),
                member_ids=member_ids,
                test_display=redactor.redact_test_display(
                    compact_test_members(
                        report.workbook,
                        member_ids,
                    )
                ),
                testbeds=sorted({
                    testbed
                    for source in sources
                    for testbed in source.testbeds
                }),
                comments=join_comment_blocks(
                    source.comments
                    for source in sources
                ),
                internal_comments=join_distinct_analysis_blocks(
                    source.internal_comments
                    for source in sources
                ),
                cause_evidence=join_comment_blocks(
                    source.cause_evidence
                    for source in sources
                ),
                ticket_refs=[],
                redmine_urls=sorted({
                    url
                    for source in sources
                    for url in source.redmine_urls
                }),
                internal_review_status=_merged_dashboard_status(
                    member_ids,
                    report.workbook,
                ),
            )
        )
    report.semantic.failure_groups = merged


def _has_summary_analysis(value: object) -> bool:
    text = strip_redmine_references(value)
    text = re.sub(r"https?://[^\s<>()]+", "", text, flags=re.IGNORECASE)
    text = text.replace(COMMENT_SEPARATOR, " ")
    text = text.replace("--------------------", " ")
    return bool(normalize_text(text.strip(" -;,")))


def _merged_dashboard_status(
    member_ids: Sequence[str],
    workbook: WorkbookSnapshot,
) -> str:
    statuses = {
        workbook.dashboard_statuses[member_id]
        for member_id in member_ids
        if member_id in workbook.dashboard_statuses
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


def _failure_group_ticket_refs(group: SemanticGroup) -> List[str]:
    values = (
        group.comments,
        group.internal_comments,
        group.cause_evidence,
        *group.ticket_refs,
        *group.redmine_urls,
    )
    parsed = {
        reference.casefold()
        for value in values
        for reference in (
            extract_redmine_urls(value) +
            extract_public_issue_urls(value) +
            extract_public_pr_urls(value) +
            extract_internal_change_urls(value)
        )
    }
    parsed.update(
        normalize_text(reference).casefold()
        for reference in group.ticket_refs
        if normalize_text(reference)
    )
    return sorted(parsed)


def _failure_group_analysis_scope(group: SemanticGroup) -> str:
    return (
        "analyzed"
        if not group.internal_review_status
        else "missing"
    )


def _dashboard_group_status(group: SemanticGroup) -> str:
    return normalize_text(group.internal_review_status) or "analyzed"


def _summarize_internal_errors(
    report: ReportModel,
    deps: PipelineDependencies,
    settings: Settings,
) -> None:
    if report.semantic is None:
        return
    groups = [
        group
        for group in report.semantic.internal_error_groups
        if normalize_text(group.comments)
    ]
    if not groups:
        return
    redactor = report.redactor or SetupRedactor({})
    fallbacks: Dict[str, str] = {}
    for group in groups:
        retained = _attempt(
            report,
            "Deterministic LogAnalyzer error retention ({})".format(
                group.group_id
            ),
            ExitCode.INVALID_INPUT,
            "Only this internal-error group's Err msgs are left empty.",
            lambda group=group: deduplicate_error_logs(
                group.comments,
                group,
                redactor,
            ),
            settings,
        )
        fallbacks[group.group_id] = retained or ""

    def extract() -> Dict[str, str]:
        result = deps.ai.extract_error_logs(groups, redactor)
        expected = {group.group_id for group in groups}
        if not isinstance(result, dict) or set(result) != expected:
            raise ValueError("AI response group IDs do not match the request")
        if any(not isinstance(result[group_id], str) for group_id in expected):
            raise ValueError("AI error logs must be strings")
        return {
            group.group_id: sanitize_error_logs(
                result[group.group_id],
                group,
                redactor,
            )
            for group in groups
        }

    extracted = _attempt(
        report,
        "AI LogAnalyzer sentence-pattern de-duplication",
        ExitCode.EXTERNAL_SOURCE,
        "First source ERR lines with deterministic de-duplication are used.",
        extract,
        settings,
    )
    selected = extracted if extracted is not None else fallbacks
    for group in groups:
        group.comments = selected[group.group_id]
    report.semantic.internal_error_groups = _merge_internal_error_groups(
        report.semantic.internal_error_groups
    )


def _filter_ngts_internal_error_tests(
    groups: Sequence[SemanticGroup],
    repo_root: Optional[Path] = None,
) -> List[SemanticGroup]:
    ngts_aliases = _exclusive_ngts_test_aliases(repo_root)
    filtered: List[SemanticGroup] = []
    for group in groups:
        tests = [
            line.strip()
            for line in str(group.test_display or "").splitlines()
            if line.strip()
        ]
        if not tests:
            filtered.append(group)
            continue
        retained = [
            test
            for test in tests
            if not (
                _NGTS_TEST_PATH.search(test) or
                _test_display_aliases(test) & ngts_aliases
            )
        ]
        if not retained:
            continue
        group.test_display = "\n".join(retained)
        filtered.append(group)
    return filtered


def _exclusive_ngts_test_aliases(
    repo_root: Optional[Path],
) -> set:
    if repo_root is None:
        return set()
    root = Path(repo_root)
    ngts_aliases = _test_aliases_under(root / "ngts")
    public_aliases = _test_aliases_under(root / "tests")
    return ngts_aliases - public_aliases


def _test_aliases_under(root: Path) -> set:
    aliases = set()
    if not root.is_dir():
        return aliases
    for path in root.rglob("test*.py"):
        aliases.add(path.stem.casefold())
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        aliases.update(
            match.group(1).casefold()
            for match in _TEST_FUNCTION_DEFINITION.finditer(source)
        )
    return aliases


def _test_display_aliases(value: object) -> set:
    text = normalize_text(value).replace("\\", "/")
    if not text:
        return set()
    aliases = set()
    parts = text.split("::")
    path = Path(parts[0])
    if path.suffix.casefold() == ".py":
        aliases.add(path.stem.casefold())
    for part in parts[1:] or parts:
        aliases.add(canonical_test_name(part).casefold())
    return {alias for alias in aliases if alias}


def _merge_internal_error_groups(
    groups: List[SemanticGroup],
) -> List[SemanticGroup]:
    signatures_by_index = [
        {
            normalize_text(error_log_signature(line)).casefold()
            for line in group.comments.splitlines()
            if normalize_text(error_log_signature(line))
        }
        for group in groups
    ]
    parents = list(range(len(groups)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = root(left)
        right_root = root(right)
        if left_root != right_root:
            parents[right_root] = left_root

    ticket_scopes = [
        _internal_error_ticket_scope(group)
        for group in groups
    ]
    problem_owner: Dict[Tuple[str, str], int] = {}
    for index, signatures in enumerate(signatures_by_index):
        ticket_scope = ticket_scopes[index]
        if ticket_scope:
            owner = problem_owner.setdefault(
                ("ticket", ticket_scope),
                index,
            )
            union(index, owner)
            continue
        if signatures:
            cause_scope = "\x1f".join(sorted(signatures))
            owner = problem_owner.setdefault(
                ("cause", cause_scope),
                index,
            )
            union(index, owner)

    indexes_by_problem: Dict[int, List[int]] = {}
    for index, _group in enumerate(groups):
        indexes_by_problem.setdefault(root(index), []).append(index)

    merged = []
    for component, source_indexes in indexes_by_problem.items():
        sources = [groups[index] for index in source_indexes]
        component_signatures = sorted({
            signature
            for index in source_indexes
            for signature in signatures_by_index[index]
        })
        ticket_scope = ticket_scopes[source_indexes[0]]
        if ticket_scope:
            problem_key = "ticket:" + ticket_scope
        elif component_signatures:
            problem_key = "cause:" + "\x1f".join(component_signatures)
        else:
            problem_key = "unknown:" + groups[component].group_id
        digest = hashlib.sha256(
            problem_key.encode("utf-8")
        ).hexdigest()[:16]
        patterns = []
        pattern_signatures = set()
        for source in sources:
            for line in source.comments.splitlines():
                pattern = normalize_text(line)
                signature = normalize_text(
                    error_log_signature(pattern)
                ).casefold()
                if (
                    pattern and
                    signature and
                    signature not in pattern_signatures
                ):
                    pattern_signatures.add(signature)
                    patterns.append(pattern)
        test_displays = compact_test_names(
            line
            for source in sources
            for line in source.test_display.splitlines()
            if normalize_text(line)
        )
        if not test_displays:
            test_displays.append("Unknown test case")
        merged.append(
            SemanticGroup(
                group_id="internal-error-{}".format(digest),
                member_ids=sorted({
                    member
                    for source in sources
                    for member in source.member_ids
                }),
                test_display="\n".join(test_displays),
                testbeds=sorted({
                    testbed
                    for source in sources
                    for testbed in source.testbeds
                }),
                comments="\n".join(patterns),
                internal_comments=join_comment_blocks(
                    source.internal_comments
                    for source in sources
                ),
                ticket_refs=sorted({
                    ticket
                    for source in sources
                    for ticket in source.ticket_refs
                }),
                redmine_urls=sorted({
                    url
                    for source in sources
                    for url in source.redmine_urls
                }),
            )
        )
    return merged


def _internal_error_ticket_scope(group: SemanticGroup) -> str:
    references = sorted({
        reference.casefold()
        for reference in list(group.ticket_refs) + list(group.redmine_urls)
        if normalize_text(reference)
    })
    return "\x1f".join(references)


def _apply_dashboard_status_warnings(semantic: Any) -> None:
    for group in semantic.failure_groups:
        warning = _DASHBOARD_STATUS_MESSAGES.get(
            group.internal_review_status,
            "",
        )
        if not warning:
            continue
        values = [
            value
            for value in (group.internal_comments, warning)
            if normalize_text(value)
        ]
        group.internal_comments = COMMENT_SEPARATOR.join(values)
        group.internal_review_warning = True


def _review_failure_public_links(
    report: ReportModel,
    deps: PipelineDependencies,
    settings: Settings,
) -> Dict[str, str]:
    if report.semantic is None:
        return {}
    contexts: Dict[str, Dict[str, Any]] = {}
    all_urls: List[str] = []
    for group in report.semantic.failure_groups:
        if _dashboard_group_status(group) in _MISSING_DASHBOARD_STATUSES:
            continue
        evidence_values = (
            group.comments,
            group.internal_comments,
            group.cause_evidence,
            *group.ticket_refs,
        )
        issue_urls = unique_sorted(
            url
            for value in evidence_values
            for url in extract_public_issue_urls(value)
        )
        pr_urls = unique_sorted(
            url
            for value in evidence_values
            for url in extract_public_pr_urls(value)
        )
        if not issue_urls and not pr_urls:
            continue
        contexts[group.group_id] = {
            "group": group,
            "subject": _first_analysis_line(group.internal_comments),
            "evidence": join_distinct_analysis_blocks(evidence_values),
            "current_failure_evidence": _failure_current_evidence(group),
            "github_issue_urls": issue_urls,
            "github_pr_urls": pr_urls,
        }
        all_urls.extend(issue_urls)
        all_urls.extend(pr_urls)
    if not contexts:
        return {}

    details: Mapping[str, Mapping[str, Any]] = {}
    inspector = getattr(deps.github, "inspect_issue_links", None)
    if callable(inspector):
        inspected = _attempt(
            report,
            "GitHub failure link metadata",
            ExitCode.EXTERNAL_SOURCE,
            "Some directly referenced GitHub links could not be semantically verified.",
            lambda: inspector(unique_sorted(all_urls)),
            settings,
        )
        details = inspected or {}
    for context in contexts.values():
        context["github_link_details"] = details

    deterministic: Dict[str, str] = {}
    ai_contexts: Dict[str, Dict[str, Any]] = {}
    for group_id, context in contexts.items():
        decision = _strong_direct_public_link_decision(
            context["group"],
            context["github_issue_urls"],
            context["github_pr_urls"],
            details,
        )
        if decision:
            deterministic[group_id] = decision
        else:
            ai_contexts[group_id] = context
    if not ai_contexts:
        return deterministic

    review_method = getattr(
        deps.ai,
        "review_public_links",
        getattr(deps.ai, "review_redmine_links", None),
    )
    if review_method is None:
        return deterministic

    def review() -> Dict[str, str]:
        result = review_method(ai_contexts, report.redactor)
        if not isinstance(result, Mapping) or set(result) != set(ai_contexts):
            raise ValueError(
                "AI failure link decisions do not match the request"
            )
        decisions = {
            group_id: normalize_text(result[group_id])
            for group_id in ai_contexts
        }
        for group_id, decision in decisions.items():
            allowed = {"none"}
            allowed.update(
                "github_issue:" + url
                for url in ai_contexts[group_id]["github_issue_urls"]
            )
            allowed.update(
                "github_pr:" + url
                for url in ai_contexts[group_id]["github_pr_urls"]
            )
            if decision not in allowed:
                raise ValueError(
                    "AI returned an invalid failure link decision"
                )
        return decisions

    reviewed = _attempt(
        report,
        "AI failure link review",
        ExitCode.EXTERNAL_SOURCE,
        "Ambiguous direct GitHub links were omitted from failure comments.",
        review,
        settings,
    )
    deterministic.update(reviewed or {})
    return deterministic


def _first_analysis_line(value: object) -> str:
    return next(
        (
            normalize_text(line)
            for line in str(value or "").splitlines()
            if normalize_text(line) and
            not extract_public_issue_urls(line) and
            not extract_public_pr_urls(line) and
            not extract_internal_change_urls(line)
        ),
        "",
    )


def _strong_direct_public_link_decision(
    group: SemanticGroup,
    issue_urls: Sequence[str],
    pr_urls: Sequence[str],
    details: Mapping[str, Mapping[str, Any]],
) -> str:
    tokens = _public_link_test_tokens(group.test_display)
    if not tokens:
        return ""
    for prefix, urls in (
        ("github_pr:", pr_urls),
        ("github_issue:", issue_urls),
    ):
        for url in urls:
            context = details.get(url)
            if not isinstance(context, Mapping):
                continue
            evidence = "{}\n{}".format(
                normalize_text(context.get("title")),
                normalize_text(context.get("body")),
            ).casefold()
            if any(token in evidence for token in tokens):
                return prefix + url
    return ""


def _public_link_test_tokens(value: object) -> set:
    tokens = set()
    for line in str(value or "").splitlines():
        canonical = canonical_test_name(line)
        if not canonical:
            continue
        path = canonical.split("::", 1)[0]
        stem = path.rsplit("/", 1)[-1]
        if stem.endswith(".py"):
            stem = stem[:-3]
        candidates = [stem]
        if "::" in canonical:
            candidates.append(canonical.rsplit("::", 1)[-1])
        for candidate in candidates:
            candidate = candidate.casefold()
            if candidate.startswith("test_") and len(candidate) >= 12:
                tokens.add(candidate)
                parts = candidate.split("_")
                if len(parts) >= 3:
                    tokens.add("_".join(parts[:3]))
    return tokens


def _failure_current_evidence(group: SemanticGroup) -> str:
    return join_distinct_analysis_blocks(
        value
        for value in (
            "Test case(s):\n" + group.test_display,
            (
                "Testbed(s):\n" + "\n".join(group.testbeds)
                if group.testbeds
                else ""
            ),
            group.cause_evidence,
            group.internal_comments,
            group.comments,
        )
        if normalize_text(value)
    )


def _redmine_ticket_urls(semantic: Any) -> List[str]:
    return sorted({
        url
        for groups in (
            semantic.failure_groups,
            semantic.skip_groups,
            semantic.internal_error_groups,
        )
        for group in groups
        for url in group.redmine_urls
        if normalize_text(url)
    })


def _review_redmine_links(
    report: ReportModel,
    deps: PipelineDependencies,
    ticket_contexts: Mapping[str, Mapping[str, Any]],
    settings: Settings,
) -> Dict[str, str]:
    if report.semantic is None:
        return {}
    groups_by_url: Dict[str, List[SemanticGroup]] = defaultdict(list)
    for group in report.semantic.failure_groups:
        if _dashboard_group_status(group) in _MISSING_DASHBOARD_STATUSES:
            continue
        for url in group.redmine_urls:
            groups_by_url[url].append(group)
    eligible_urls = {
        url
        for url in groups_by_url
        if url in ticket_contexts and
        (
            ticket_contexts[url].get("github_issue_urls") or
            ticket_contexts[url].get("github_pr_urls")
        )
    }
    candidate_urls = unique_sorted(
        candidate
        for url in eligible_urls
        for key in ("github_issue_urls", "github_pr_urls")
        for candidate in ticket_contexts[url].get(key, ())
    )
    if not candidate_urls:
        return {}
    inspector = getattr(deps.github, "inspect_issue_links", None)
    if not callable(inspector):
        return {}
    inspected = _attempt(
        report,
        "GitHub issue details",
        ExitCode.EXTERNAL_SOURCE,
        "Unverified GitHub links are not published in Failure Comments.",
        lambda: inspector(candidate_urls),
        settings,
    )
    github_details = (
        {
            normalize_text(url): context
            for url, context in inspected.items()
            if normalize_text(url) and isinstance(context, Mapping)
        }
        if isinstance(inspected, Mapping)
        else {}
    )
    contexts: Dict[str, Mapping[str, Any]] = {}
    for url in sorted(eligible_urls):
        context = dict(ticket_contexts[url])
        context["github_issue_urls"] = [
            candidate
            for candidate in context.get("github_issue_urls", ())
            if candidate in github_details
        ]
        context["github_pr_urls"] = [
            candidate
            for candidate in context.get("github_pr_urls", ())
            if candidate in github_details
        ]
        candidates = (
            list(context["github_issue_urls"]) +
            list(context["github_pr_urls"])
        )
        if not candidates:
            continue
        context["github_link_details"] = {
            candidate: github_details[candidate]
            for candidate in candidates
        }
        context["current_failure_evidence"] = (
            join_distinct_analysis_blocks(
                value
                for group in groups_by_url[url]
                for value in (
                    group.cause_evidence,
                    group.internal_comments,
                    group.comments,
                )
                if normalize_text(value)
            )
        )
        contexts[url] = context
    if not contexts:
        return {}
    decisions: Dict[str, str] = {}
    review_contexts: Dict[str, Mapping[str, Any]] = {}
    for url, context in contexts.items():
        active_prs = unique_sorted(
            candidate
            for candidate in context.get(
                "active_github_pr_urls",
                (),
            )
            if candidate in context.get("github_pr_urls", ())
        )
        if len(active_prs) == 1:
            decisions[url] = "github_pr:" + active_prs[0]
        else:
            review_contexts[url] = context
    if not review_contexts:
        return decisions
    reviewer = getattr(deps.ai, "review_redmine_links", None)
    if not callable(reviewer):
        return decisions
    redactor = report.redactor or SetupRedactor({})

    def review() -> Dict[str, str]:
        result = reviewer(review_contexts, redactor)
        if not isinstance(result, Mapping) or set(result) != set(
            review_contexts
        ):
            raise ValueError(
                "AI Redmine link decisions do not match the request"
            )
        decisions = {
            url: normalize_text(result[url])
            for url in review_contexts
        }
        for url, decision in decisions.items():
            allowed = {"none"}
            allowed.update(
                "github_issue:" + value
                for value in review_contexts[url].get(
                    "github_issue_urls",
                    (),
                )
            )
            allowed.update(
                "github_pr:" + value
                for value in review_contexts[url].get(
                    "github_pr_urls",
                    (),
                )
            )
            if decision not in allowed:
                raise ValueError(
                    "AI returned an invalid Redmine link decision"
                )
        return decisions

    reviewed = _attempt(
        report,
        "AI Redmine link review",
        ExitCode.EXTERNAL_SOURCE,
        "Failure Comments use the internal WIP fallback.",
        review,
        settings,
    )
    decisions.update(reviewed if isinstance(reviewed, dict) else {})
    return decisions


def _apply_failure_comment_policy(
    semantic: Any,
    ticket_contexts: Mapping[str, Mapping[str, Any]],
    link_decisions: Mapping[str, str],
    public_link_decisions: Optional[Mapping[str, str]] = None,
) -> None:
    public_link_decisions = public_link_decisions or {}
    for group in semantic.failure_groups:
        if _dashboard_group_status(group) in _MISSING_DASHBOARD_STATUSES:
            group.comments = "Under investigation"
            continue
        decisions = [
            normalize_text(link_decisions.get(url))
            for url in group.redmine_urls
        ]
        decisions.append(
            normalize_text(public_link_decisions.get(group.group_id))
        )
        pull_requests = unique_sorted(
            decision.split(":", 1)[1]
            for decision in decisions
            if decision.startswith("github_pr:")
        )
        if pull_requests:
            group.comments = "Could be fixed by: {}".format(
                "\n".join(pull_requests)
            )
            continue
        issues = unique_sorted(
            decision.split(":", 1)[1]
            for decision in decisions
            if decision.startswith("github_issue:")
        )
        if issues:
            group.comments = "\n".join(issues)
            continue
        if not group.redmine_urls:
            if _has_verification_ticket_summary(group):
                group.comments = "Internal test issue, WIP"
            elif _has_sensitive_internal_technology(group):
                group.comments = "Internal issue, WIP"
            continue
        issue_type = (
            "Internal test issue"
            if _failure_has_only_test_tickets(group, ticket_contexts)
            else "Internal issue"
        )
        group.comments = (
            issue_type
            if _all_failure_tickets_are_closed(group, ticket_contexts)
            else issue_type + ", WIP"
        )


def _failure_has_only_test_tickets(
    group: SemanticGroup,
    ticket_contexts: Mapping[str, Mapping[str, Any]],
) -> bool:
    if _has_verification_ticket_summary(group):
        return True
    contexts = [
        ticket_contexts[url]
        for url in group.redmine_urls
        if url in ticket_contexts
    ]
    if any(_is_verification_ticket_context(context) for context in contexts):
        return True
    return bool(contexts) and all(
        context.get("is_test_issue") is True
        for context in contexts
    )


def _has_verification_ticket_summary(group: SemanticGroup) -> bool:
    return bool(
        _VERIFICATION_PROJECT_MARKER.search(group.internal_comments)
    )


def _is_verification_ticket_context(
    context: Optional[Mapping[str, Any]],
) -> bool:
    if not isinstance(context, Mapping):
        return False
    project = normalize_text(context.get("project"))
    if project.casefold() == "sonic - verification":
        return True
    return any(
        _VERIFICATION_PROJECT_MARKER.search(
            normalize_text(context.get(key))
        )
        for key in ("title", "summary")
    )


def _all_failure_tickets_are_closed(
    group: SemanticGroup,
    ticket_contexts: Mapping[str, Mapping[str, Any]],
) -> bool:
    return bool(group.redmine_urls) and all(
        url in ticket_contexts and
        ticket_contexts[url].get("is_closed") is True
        for url in group.redmine_urls
    )


def _has_sensitive_internal_technology(group: SemanticGroup) -> bool:
    return any(
        _SENSITIVE_INTERNAL_TECHNOLOGY.search(str(value or ""))
        for value in (
            group.comments,
            group.internal_comments,
            group.cause_evidence,
        )
    )


def _prepare_redmine_comments(
    semantic: Any,
    ticket_summaries: Optional[Mapping[str, str]] = None,
    redactor: Optional[SetupRedactor] = None,
) -> None:
    summaries = {
        normalize_text(url).casefold(): normalize_text(summary)
        for url, summary in (ticket_summaries or {}).items()
        if normalize_text(url) and normalize_text(summary)
    }
    skip_group_ids = {
        id(group)
        for group in semantic.skip_groups
    }
    for group in (
        list(semantic.failure_groups) +
        list(semantic.skip_groups) +
        list(semantic.internal_error_groups)
    ):
        refreshed_urls = [
            url
            for url in group.redmine_urls
            if normalize_text(url).casefold() in summaries
        ]
        source_value = (
            join_comment_blocks(
                group.ticket_refs or group.redmine_urls
            )
            if id(group) in skip_group_ids
            else group.internal_comments
        )
        value = _strip_stale_redmine_summaries(
            source_value,
            refreshed_urls,
        )
        for url in sorted(group.redmine_urls, key=len, reverse=True):
            if normalize_text(url).casefold() not in summaries:
                continue
            display = _redmine_reference_display(
                url,
                summaries,
                redactor,
            )
            pattern = re.compile(
                r"(?i)(?:Redmine:\s*)?{}(?!\d)".format(
                    re.escape(url.rstrip("/"))
                )
            )
            value, replacements = pattern.subn(display, value)
            if not replacements and display not in value:
                value = join_comment_blocks((value, display))
        redacted = (
            redactor.redact(value)
            if redactor is not None
            else value
        )
        group.internal_comments = join_comment_blocks((redacted,))
    for group in semantic.skip_groups:
        group.comments = (
            "Internal test issue, WIP"
            if _has_verification_ticket_summary(group)
            else "Internal issue, WIP"
        )


def _strip_stale_redmine_summaries(
    value: object,
    redmine_urls: Sequence[str],
) -> str:
    text = str(value or "")
    issue_ids = {
        url.rstrip("/").rsplit("/", 1)[-1]
        for url in redmine_urls
        if extract_redmine_urls(url)
    }
    for issue_id in sorted(issue_ids, key=len, reverse=True):
        pattern = re.compile(
            r"(?i)(?:\[[^\]\r\n]{{1,160}}\]\s+)?"
            r"(?:Bug\s+SW|Feature|Issue)\s+#{}"
            r"\s*:[^\r\n]*".format(re.escape(issue_id))
        )
        text = pattern.sub("", text)
    return join_comment_blocks(
        block.strip(" -:;")
        for block in text.split(COMMENT_SEPARATOR)
        if normalize_text(block.strip(" -:;"))
    )


def _redmine_reference_display(
    url: str,
    summaries: Mapping[str, str],
    redactor: Optional[SetupRedactor],
) -> str:
    display = summaries.get(normalize_text(url).casefold(), "")
    if not display:
        display = normalize_text(url)
    return redactor.redact(display) if redactor is not None else display


def _render(
    report: ReportModel,
    deps: PipelineDependencies,
    settings: Settings,
) -> RenderedMessage:
    try:
        rendered = deps.renderer.render(report)
    except Exception as error:
        rendering_error = generation_error(
            "HTML rendering",
            error,
            "A minimal diagnostic message is used.",
            ExitCode.INVALID_INPUT,
            _setting_secrets(settings),
        )
        report.errors.append(rendering_error)
        _write_diagnostic(settings, "HTML rendering", error)
        try:
            rendered = deps.renderer.minimal(report, rendering_error)
        except Exception as minimal_error:
            _write_diagnostic(settings, "Minimal rendering", minimal_error)
            rendered = _hardcoded_minimal(report)
    return _redact_rendered(report, rendered)


def _build_mime(
    report: ReportModel,
    rendered: RenderedMessage,
    deps: PipelineDependencies,
    settings: Settings,
) -> Optional[EmailMessage]:
    try:
        return deps.mime_builder(
            rendered,
            report.request,
            settings.sender,
        )
    except Exception as error:
        mime_error = generation_error(
            "MIME construction",
            error,
            "A minimal diagnostic email is sent.",
            ExitCode.INVALID_INPUT,
            _setting_secrets(settings),
        )
        report.errors.append(mime_error)
        _write_diagnostic(settings, "MIME construction", error)
        try:
            try:
                minimal = deps.renderer.minimal(report, mime_error)
            except Exception as rendering_error:
                _write_diagnostic(settings, "Minimal rendering", rendering_error)
                minimal = _hardcoded_minimal(report)
            return deps.mime_builder(minimal, report.request, settings.sender)
        except Exception as minimal_error:
            _write_diagnostic(settings, "Minimal MIME", minimal_error)
            return None


def _redact_rendered(
    report: ReportModel,
    rendered: RenderedMessage,
) -> RenderedMessage:
    if report.redactor is None:
        return rendered
    return RenderedMessage(
        subject=report.redactor.redact(rendered.subject),
        plain=report.redactor.redact(rendered.plain),
        html=report.redactor.redact(rendered.html),
    )


def _ensure_redacted_mime(
    report: ReportModel,
    message: Optional[EmailMessage],
    deps: PipelineDependencies,
    settings: Settings,
) -> Optional[EmailMessage]:
    if message is None or report.redactor is None:
        return message
    try:
        report.redactor.assert_clean(_mime_text_values(message), "email message")
        return message
    except Exception as error:
        redaction_error = generation_error(
            "Setup redaction",
            error,
            "A minimal diagnostic email is sent.",
            ExitCode.INVALID_INPUT,
            _setting_secrets(settings),
        )
        report.errors.append(redaction_error)
        _write_diagnostic(settings, "Setup redaction", error)
        try:
            try:
                minimal = deps.renderer.minimal(report, redaction_error)
            except Exception as rendering_error:
                _write_diagnostic(settings, "Minimal redaction rendering", rendering_error)
                minimal = _hardcoded_minimal(report)
            minimal = _redact_rendered(report, minimal)
            fallback = deps.mime_builder(
                minimal,
                report.request,
                settings.sender,
            )
            report.redactor.assert_clean(
                _mime_text_values(fallback),
                "minimal email message",
            )
            return fallback
        except Exception as fallback_error:
            _write_diagnostic(settings, "Minimal redaction MIME", fallback_error)
            return None


def _mime_text_values(message: EmailMessage) -> List[str]:
    values = [
        "{}: {}".format(name, value)
        for name, value in message.items()
    ]
    for part in message.walk():
        filename = part.get_filename()
        if filename:
            values.append(filename)
        if part.get_content_maintype() == "text":
            try:
                values.append(part.get_content())
            except (KeyError, LookupError, TypeError, UnicodeError):
                payload = part.get_payload(decode=True)
                if payload:
                    values.append(payload.decode("utf-8", errors="replace"))
    return values


def _attempt(
    report: ReportModel,
    stage: str,
    exit_code: ExitCode,
    impact: str,
    operation: Callable[[], Any],
    settings: Settings,
) -> Any:
    try:
        return operation()
    except Exception as error:
        report.errors.append(
            generation_error(
                stage,
                error,
                impact,
                exit_code,
                _setting_secrets(settings),
            )
        )
        _write_diagnostic(settings, stage, error)
        return None


def _hardcoded_minimal(report: ReportModel) -> RenderedMessage:
    lines = [
        "SONiC Regression Report (degraded)",
        "Version: {}".format(report.request.version),
        "",
        "Generation Errors:",
    ]
    lines.extend(
        "- {}: {} Impact: {}".format(error.stage, error.message, error.impact)
        for error in report.errors
    )
    return RenderedMessage(
        subject=report_subject(report),
        plain="\n".join(lines) + "\n",
        html="",
    )


def apply_dashboard_analysis(
    workbook: Any,
    dashboard: Any,
    record_ids: Optional[Iterable[str]] = None,
) -> Dict[str, str]:
    """Join exact human analysis to Excel failures without cross-version guessing."""

    exact_index: Dict[
        Tuple[str, str, str],
        List[DashboardAnalysis],
    ] = defaultdict(list)
    identity_index: Dict[
        Tuple[str, str],
        List[DashboardAnalysis],
    ] = defaultdict(list)
    for analysis in dashboard.analyses:
        identity_key = (
            normalize_text(analysis.session_id),
            normalize_text(analysis.key_id),
        )
        identity_index[identity_key].append(analysis)
        exact_key = (
            identity_key[0],
            identity_key[1],
            normalize_text(analysis.test_name),
        )
        exact_index[exact_key].append(analysis)
    setup_aliases = _dashboard_setup_aliases(workbook, dashboard)
    aliased_index: Dict[
        Tuple[str, str, str],
        List[DashboardAnalysis],
    ] = defaultdict(list)
    if setup_aliases:
        for analysis in dashboard.analyses:
            aliased_name = _normalize_dashboard_join_name(
                analysis.test_name,
                setup_aliases,
            )
            aliased_key = (
                normalize_text(analysis.session_id),
                normalize_text(analysis.key_id),
                aliased_name,
            )
            aliased_index[aliased_key].append(analysis)

    selected_ids = set(record_ids) if record_ids is not None else None
    comments: Dict[str, str] = {}
    for row in workbook.failures:
        if selected_ids is not None and row.record_id not in selected_ids:
            continue
        identity_key = (
            normalize_text(row.session_id),
            normalize_text(row.mars_key_id),
        )
        candidates: List[DashboardAnalysis] = []
        for test_name in row.join_names:
            exact_key = (
                identity_key[0],
                identity_key[1],
                normalize_text(test_name),
            )
            for analysis in exact_index.get(exact_key, []):
                if analysis not in candidates:
                    candidates.append(analysis)
        if not candidates and setup_aliases:
            for test_name in row.join_names:
                aliased_key = (
                    identity_key[0],
                    identity_key[1],
                    _normalize_dashboard_join_name(
                        test_name,
                        setup_aliases,
                    ),
                )
                for analysis in aliased_index.get(aliased_key, []):
                    if analysis not in candidates:
                        candidates.append(analysis)
        if not candidates:
            candidates = [
                analysis
                for analysis in identity_index.get(identity_key, ())
                if _matches_sanitized_dashboard_name(
                    row.sanitized_testname,
                    analysis.test_name,
                )
            ]
        signatures = {
            (
                normalize_text(analysis.analysis),
                normalize_text(analysis.redmine_url),
            )
            for analysis in candidates
        }
        ambiguous = len(signatures) > 1
        matched = [] if ambiguous else candidates
        values: List[str] = []
        has_analysis = False
        has_ticket = False
        placeholder_statuses = set()
        for analysis in matched:
            value = normalize_text(analysis.analysis)
            placeholder_status = _dashboard_placeholder_status(value)
            if placeholder_status:
                placeholder_statuses.add(placeholder_status)
            elif value and value not in values:
                values.append(value)
                has_analysis = True
            redmine_url = normalize_text(analysis.redmine_url)
            if redmine_url and redmine_url not in values:
                values.append(redmine_url)
                has_ticket = True
        comments[row.record_id] = "\n".join(values)
        if ambiguous:
            workbook.dashboard_statuses[row.record_id] = "ambiguous"
        elif has_analysis and placeholder_statuses:
            workbook.dashboard_statuses[row.record_id] = "partial"
        elif has_analysis or has_ticket:
            workbook.dashboard_statuses[row.record_id] = "analyzed"
        elif matched:
            workbook.dashboard_statuses[row.record_id] = (
                "not_found"
                if "not_found" in placeholder_statuses
                else "no_analysis"
            )
        else:
            workbook.dashboard_statuses[row.record_id] = "not_found"
    workbook.row_comments.update(comments)
    return comments


def _dashboard_setup_aliases(
    workbook: Any,
    dashboard: Any,
) -> Dict[str, str]:
    markers_by_alias: Dict[str, set] = defaultdict(set)

    def add(alias: object, platform: object) -> None:
        normalized_alias = normalize_text(alias)
        marker = _setup_alias_marker(normalized_alias, platform)
        if normalized_alias and marker:
            markers_by_alias[normalized_alias.casefold()].add(marker)

    for source in (
        getattr(workbook, "setup_platforms", {}) or {},
        getattr(dashboard, "setup_platforms", {}) or {},
    ):
        for setup, platform in source.items():
            for alias in _setup_alias_candidates(setup):
                add(alias, platform)

    workbook_platforms = {
        normalize_text(setup).casefold(): normalize_text(platform)
        for setup, platform in (
            getattr(workbook, "setup_platforms", {}) or {}
        ).items()
        if normalize_text(setup) and normalize_text(platform)
    }
    for row in getattr(workbook, "failures", ()):
        platform = normalize_text(
            getattr(row, "platform", "")
        ) or workbook_platforms.get(
            normalize_text(getattr(row, "testbed", "")).casefold(),
            "",
        )
        if not platform:
            continue
        for test_name in row.join_names:
            for alias in _embedded_setup_aliases(
                row.testbed,
                test_name,
            ):
                add(alias, platform)

    return {
        alias: next(iter(markers))
        for alias, markers in markers_by_alias.items()
        if len(markers) == 1
    }


def _setup_alias_candidates(value: object) -> List[str]:
    setup = normalize_text(value)
    if not setup:
        return []
    candidates = [setup]
    for suffix in ("_setup", "-setup"):
        if setup.casefold().endswith(suffix):
            candidates.append(setup[:-len(suffix)])
    for candidate in tuple(candidates):
        embedded = re.match(
            r"(?i)^sonic_[a-z0-9]+_(.+)$",
            candidate,
        )
        if embedded:
            candidates.append(embedded.group(1))
    return unique_sorted(candidates)


def _embedded_setup_aliases(
    testbed: object,
    test_name: object,
) -> List[str]:
    normalized_testbed = normalize_text(testbed)
    normalized_name = normalize_text(test_name)
    parts = normalized_testbed.split("-")
    for end in range(len(parts), 0, -1):
        candidate = "-".join(parts[:end])
        if (
            _setup_alias_marker(candidate, "platform") and
            _contains_setup_alias(normalized_name, candidate)
        ):
            return [candidate]
    return []


def _setup_alias_marker(alias: object, platform: object) -> str:
    normalized_alias = normalize_text(alias)
    normalized_platform = normalize_text(platform).casefold()
    if not normalized_alias or not normalized_platform:
        return ""
    return "__setup_{}__".format(
        re.sub(r"[^a-z0-9]+", "_", normalized_platform).strip("_"),
    )


def _contains_setup_alias(value: str, alias: str) -> bool:
    return bool(
        re.search(
            r"(?<![A-Za-z0-9_]){}(?=$|[-,\]])".format(
                re.escape(alias)
            ),
            value,
            flags=re.IGNORECASE,
        )
    )


def _normalize_dashboard_join_name(
    value: object,
    aliases: Mapping[str, str],
) -> str:
    normalized = normalize_text(value)
    for alias in sorted(aliases, key=lambda item: (-len(item), item)):
        normalized = re.sub(
            r"(?<![A-Za-z0-9_]){}(?=$|[-,\]])".format(
                re.escape(alias)
            ),
            aliases[alias],
            normalized,
            flags=re.IGNORECASE,
        )
    return normalized


def _matches_sanitized_dashboard_name(
    sanitized_name: object,
    dashboard_name: object,
) -> bool:
    template = normalize_text(sanitized_name)
    candidate = normalize_text(dashboard_name)
    parts = re.split(
        r"(?i)switch-hostname",
        template,
    )
    if len(parts) == 1 or not candidate:
        return False
    pattern = r"[^,\[\]]+?".join(
        re.escape(part)
        for part in parts
    )
    return bool(
        re.fullmatch(
            pattern,
            candidate,
            flags=re.IGNORECASE,
        )
    )


def _dashboard_placeholder_status(value: object) -> str:
    normalized = normalize_text(value).casefold().rstrip(" .:;-")
    return _DASHBOARD_PLACEHOLDER_STATUSES.get(normalized, "")


def mark_dashboard_unavailable(workbook: Any) -> None:
    for row in workbook.failures:
        workbook.dashboard_statuses[row.record_id] = "unavailable"


def generation_error(
    stage: str,
    error: object,
    impact: str,
    exit_code: ExitCode,
    secrets: Tuple[str, ...] = (),
) -> GenerationError:
    return GenerationError(
        stage=stage,
        message=sanitize_error(error, secrets=secrets),
        impact=sanitize_error(impact, secrets=secrets),
        exit_code=exit_code,
    )


def sanitize_error(
    value: object,
    limit: int = 300,
    secrets: Tuple[str, ...] = (),
) -> str:
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)([?&]"):
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    if not text:
        return "unspecified error"
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def _write_diagnostic(settings: Settings, stage: str, error: object) -> None:
    if settings.log_path is None:
        return
    try:
        settings.log_path.parent.mkdir(parents=True, exist_ok=True)
        with settings.log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                "{}: {}\n".format(
                    stage,
                    sanitize_error(
                        error,
                        limit=2000,
                        secrets=_setting_secrets(settings),
                    ),
                )
            )
    except OSError:
        pass


def _setting_secrets(settings: Settings) -> Tuple[str, ...]:
    return tuple(
        value
        for value in (
            settings.github_token,
            settings.redmine_api_token,
            settings.inference_api_key,
        )
        if value
    )
