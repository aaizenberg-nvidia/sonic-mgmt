"""Domain models shared by the regression mail workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


class ExitCode(IntEnum):
    """Public process exit codes."""

    SUCCESS = 0
    INVALID_INPUT = 3
    EXTERNAL_SOURCE = 4
    DELIVERY = 6


@dataclass(frozen=True)
class RunRequest:
    """Validated command-line request."""

    excel_path: Path
    version: str
    to: Sequence[str]
    cc: Sequence[str] = ()


@dataclass(frozen=True)
class GenerationError:
    """A sanitized error that can be rendered in the generated email."""

    stage: str
    message: str
    impact: str
    exit_code: ExitCode


@dataclass(frozen=True)
class ResultRow:
    """A selected workbook result row."""

    record_id: str
    excel_row: int
    session_id: str
    mars_key_id: str
    testbed: str
    test_name: str
    sanitized_testname: str
    result: str
    message: str
    topology: str
    hwsku: str
    platform: str = ""
    values: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def join_names(self) -> Sequence[str]:
        names = [self.test_name]
        if self.sanitized_testname and self.sanitized_testname not in names:
            names.append(self.sanitized_testname)
        return names

    @property
    def version(self) -> str:
        return str(self.values.get("os_version") or "")


@dataclass
class WorkbookSnapshot:
    """Read-only projection of the selected workbook version."""

    source_path: Path
    sheet_name: str
    header_row: int
    selected_rows: List[ResultRow]
    result_counts: Dict[str, int]
    hardware_pairs: List[Sequence[str]]
    versions: List[str] = field(default_factory=list)
    setup_platforms: Dict[str, str] = field(default_factory=dict)
    setup_testbeds: Dict[str, str] = field(default_factory=dict)
    row_comments: Dict[str, str] = field(default_factory=dict)
    public_issue_conditions: Dict[str, List[str]] = field(
        default_factory=dict
    )
    issue_skip_tickets: Dict[str, List[str]] = field(default_factory=dict)
    issue_skip_analysis_complete: bool = False
    dashboard_statuses: Dict[str, str] = field(default_factory=dict)
    record_excel_rows: Dict[str, List[int]] = field(default_factory=dict, repr=False)

    @property
    def failures(self) -> List[ResultRow]:
        return [row for row in self.selected_rows if row.result == "fail"]

    @property
    def skipped(self) -> List[ResultRow]:
        return [row for row in self.selected_rows if row.result == "skipped"]

    @property
    def issue_skipped(self) -> List[ResultRow]:
        if not self.issue_skip_analysis_complete:
            return []
        return [
            row
            for row in self.skipped
            if self.issue_skip_tickets.get(row.record_id)
        ]

    @property
    def coverage(self) -> Optional[float]:
        if self.skipped and not self.issue_skip_analysis_complete:
            return None
        executed = self.result_counts.get("pass", 0) + self.result_counts.get("fail", 0)
        denominator = executed + len(self.issue_skipped)
        return (executed / denominator * 100.0) if denominator else None

    @property
    def pass_rate(self) -> Optional[float]:
        passed = self.result_counts.get("pass", 0)
        executed = passed + self.result_counts.get("fail", 0)
        return (passed / executed * 100.0) if executed else None


@dataclass(frozen=True)
class DashboardAnalysis:
    """Human analysis returned by the regression dashboard."""

    session_id: str
    key_id: str
    test_name: str
    analysis: str
    redmine_url: str = ""
    verdict: str = ""
    updated_at: str = ""
    switch_version: str = ""


@dataclass
class DashboardSnapshot:
    """Dashboard summary and failure-analysis overlay."""

    coverage: Optional[float] = None
    pass_rate: Optional[float] = None
    analyses: List[DashboardAnalysis] = field(default_factory=list)
    setup_platforms: Dict[str, str] = field(default_factory=dict)
    setup_testbeds: Dict[str, str] = field(default_factory=dict)


@dataclass
class RcStatus:
    """Metadata parsed from an exact RC_STATUS.md document."""

    image_branch: str = ""
    image_public_hash: str = ""
    image_pr_urls: List[str] = field(default_factory=list)


@dataclass
class GitResolution:
    """Internal/public sonic-mgmt history resolution."""

    internal_branch: str = ""
    public_hash: str = ""
    source_root: Optional[Path] = None
    additional_commit_hashes: List[str] = field(default_factory=list)
    branch_cutoff_date: str = ""
    temporary_refs: List[str] = field(default_factory=list, repr=False)


@dataclass(frozen=True)
class PullRequest:
    """Validated public GitHub pull request."""

    url: str
    title: str = ""
    author: str = ""
    state: str = ""
    draft: bool = False
    labels: Tuple[str, ...] = ()


@dataclass
class SemanticGroup:
    """Deterministically derived rendered report row."""

    group_id: str
    member_ids: List[str]
    test_display: str
    testbeds: List[str]
    comments: str = ""
    internal_comments: str = ""
    cause_evidence: str = ""
    ticket_refs: List[str] = field(default_factory=list)
    redmine_urls: List[str] = field(default_factory=list)
    internal_review_status: str = ""
    internal_review_warning: bool = False
    link_titles: Dict[str, str] = field(default_factory=dict)


@dataclass
class SemanticReport:
    """Deterministically derived report content."""

    failure_groups: List[SemanticGroup] = field(default_factory=list)
    skip_groups: List[SemanticGroup] = field(default_factory=list)
    internal_error_groups: List[SemanticGroup] = field(default_factory=list)


@dataclass
class ReportModel:
    """Complete best-effort input to rendering and delivery."""

    request: RunRequest
    workbook: Optional[WorkbookSnapshot] = None
    dashboard: Optional[DashboardSnapshot] = None
    rc_status: Optional[RcStatus] = None
    git: Optional[GitResolution] = None
    semantic: Optional[SemanticReport] = None
    image_prs: List[PullRequest] = field(default_factory=list)
    mgmt_prs: List[PullRequest] = field(default_factory=list)
    errors: List[GenerationError] = field(default_factory=list)
    redactor: Optional[Any] = field(default=None, repr=False)

    @property
    def exit_code(self) -> ExitCode:
        if not self.errors:
            return ExitCode.SUCCESS
        return max(error.exit_code for error in self.errors)


@dataclass(frozen=True)
class RenderedMessage:
    """Rendered message bodies before MIME construction."""

    subject: str
    plain: str
    html: str
