from __future__ import annotations

import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import openpyxl

from ngts.scripts.regression_mail.config import Settings
from ngts.scripts.regression_mail.grouping import build_deterministic_report
from ngts.scripts.regression_mail.mail import (
    ReportRenderer,
    _distinct_skip_conditions,
    _failure_table,
    _github_skip_rows,
    _hardware_rows,
    _internal_error_table,
    _section_title,
    _skip_table,
    _table,
)
from ngts.scripts.regression_mail.models import (
    DashboardAnalysis,
    DashboardSnapshot,
    GitResolution,
    PullRequest,
    RcStatus,
    ReportModel,
    ResultRow,
    RunRequest,
    SemanticGroup,
    SemanticReport,
    WorkbookSnapshot,
)
from ngts.scripts.regression_mail.normalization import (
    COMMENT_SEPARATOR,
    join_comment_blocks,
    normalize_text,
)
from ngts.scripts.regression_mail.pipeline import (
    PipelineDependencies,
    _apply_dashboard_status_warnings,
    _apply_failure_comment_policy,
    _build_setup_redactor,
    _classify_and_merge_failure_groups,
    _collect_internal_change_titles,
    _collect_mgmt_prs,
    _filter_ngts_internal_error_tests,
    _filter_closed_issue_skips,
    _merge_internal_error_groups,
    _prepare_redmine_comments,
    _review_failure_public_links,
    _review_redmine_links,
    _summarize_comments,
    apply_dashboard_analysis,
    run,
)
from ngts.scripts.regression_mail.redaction import SetupRedactor
from ngts.scripts.regression_mail.workbook import (
    ArtifactWriter,
    REQUIRED_HEADERS,
    WorkbookLoader,
)


VERSION = "202608_RC.17-deadbeef_Internal"


class _Workbook:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def load(self, path, version):
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot


class _Dashboard:
    def __init__(self, analyses=None):
        if analyses is None:
            analyses = [
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name="tests/a.py::test_a",
                    analysis="Existing engineer analysis on secret-setup-01",
                )
            ]
        self.analyses = list(analyses)

    def fetch(self, version):
        return DashboardSnapshot(
            coverage=91.2,
            pass_rate=98.4,
            analyses=self.analyses,
        )


class _GitHub:
    def fetch_rc_status(self, version):
        return RcStatus(
            image_branch="202608_RC",
            image_public_hash="deadbeef",
            image_pr_urls=[
                "https://github.com/sonic-net/sonic-buildimage/pull/9"
            ],
        )

    def get_image_prs(self, urls):
        return [
            PullRequest(
                url=urls[0],
                title="Fix image build",
                author="image-owner",
                state="closed",
            )
        ]

    def get_mgmt_prs(self, urls):
        return [
            PullRequest(
                url=urls[0],
                title="Fix mgmt test",
                author="zypgithub",
                state="open",
                labels=("Request for 202608 branch",),
            )
        ]

    def get_open_mgmt_prs_by_authors(self, authors):
        return [
            PullRequest(
                url="https://github.com/sonic-net/sonic-mgmt/pull/7",
                title="Fix mgmt test",
                author="zypgithub",
                state="open",
                labels=("Request for 202608 branch",),
            )
        ]

    def __getattr__(self, name):
        raise AssertionError("{} must not be called".format(name))


class _Git:
    def __init__(self, error=None):
        self.cleaned = False
        self.error = error

    def resolve(self, version):
        if self.error:
            raise self.error
        return GitResolution(
            internal_branch="develop-202608",
            public_hash="def",
            branch_cutoff_date="2026-01-01",
            additional_commit_hashes=["abc1234"],
        )

    def cleanup(self, resolution):
        self.cleaned = True


class _Unused:
    def __getattr__(self, name):
        raise AssertionError("{} must not be called".format(name))


class _Redmine:
    def __init__(self, error=None):
        self.error = error
        self.cutoffs = []

    def find_log_analyzer_errors(self, version, cutoff_date):
        self.cutoffs.append(cutoff_date)
        if self.error:
            raise self.error
        return []

    def describe_tickets(self, references):
        if self.error:
            raise self.error
        return {
            reference: (
                "[SONIC - Design] Bug SW #5154543: Internal issue"
                " | Assignee: Owner | Status: In Progress"
            )
            for reference in references
        }

    def inspect_tickets(self, references):
        if self.error:
            raise self.error
        return {
            reference: {
                "summary": (
                    "[SONIC - Design] Bug SW #5154543: Internal issue"
                    " | Assignee: Owner | Status: In Progress"
                ),
                "project": "SONIC - Design",
                "tracker": "Bug SW",
                "subject": "Internal issue",
                "evidence": "Internal issue",
                "github_issue_urls": [],
                "github_pr_urls": [],
                "is_test_issue": False,
            }
            for reference in references
        }


class _AI:
    def __init__(self, error=None):
        self.error = error
        self.batches = []
        self.error_batches = []

    def summarize(self, groups, redactor):
        self.batches.append(tuple(group.group_id for group in groups))
        if self.error:
            raise self.error
        return {
            group.group_id: "Generalized AI summary"
            for group in groups
        }

    def classify_failure_causes(self, groups, file_keys, redactor):
        if self.error:
            raise self.error
        return {
            group.group_id: "unique:" + group.group_id
            for group in groups
        }

    def review_redmine_links(self, contexts, redactor):
        if self.error:
            raise self.error
        return {
            url: "none"
            for url in contexts
        }

    def extract_error_logs(self, groups, redactor):
        self.error_batches.append(tuple(group.group_id for group in groups))
        if self.error:
            raise self.error
        return {
            group.group_id: (
                "ERR service: connection failed with 10.1.2.3"
            )
            for group in groups
        }


class _Artifacts:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.semantic = None

    def write_skips(self, workbook, semantic):
        self.semantic = semantic
        path = self.directory / "skips.json"
        path.write_text("{}\n", encoding="utf-8")
        return path


class _CapturingTransport:
    def __init__(self):
        self.messages = []

    def send(self, message, request):
        self.messages.append((message, request))


class _FailingTransport:
    def send(self, message, request):
        raise RuntimeError("SMTP unavailable")


class _BrokenRenderer:
    def render(self, report):
        raise RuntimeError("HTML failed")

    def minimal(self, report, error):
        raise RuntimeError("minimal renderer failed")


def _snapshot(path: Path) -> WorkbookSnapshot:
    row = ResultRow(
        record_id="failure-1",
        excel_row=2,
        session_id="s",
        mars_key_id="k",
        testbed="secret-setup-01",
        test_name="tests/a.py::test_a",
        sanitized_testname="tests/a.py::test_a",
        result="fail",
        message="failure",
        topology="t0",
        hwsku="sku",
        platform="SN5600",
        values={"ReportId": "report-uuid"},
    )
    return WorkbookSnapshot(
        source_path=path,
        sheet_name="community_tests",
        header_row=1,
        selected_rows=[row],
        result_counts={"pass": 0, "fail": 1, "skipped": 0},
        hardware_pairs=[("sku", "t0")],
        setup_platforms={"secret-setup-01": "SN5600"},
        setup_testbeds={"secret-setup-01": "SN5600-t0"},
        row_comments={
            "failure-1": "Existing engineer analysis on secret-setup-01"
        },
    )


def _settings(repo_root: Path) -> Settings:
    return Settings(
        smtp_host="smtp.invalid",
        smtp_port=25,
        sender="sender@nvidia.com",
        api_base_url="https://dashboard.invalid",
        github_token="",
        rc_status_repository="nvidia-sonic/sonic-buildimage",
        rc_status_ref="",
        rc_status_tag="",
        redmine_api_token="token",
        redmine_author_id=7680,
        http_timeout_seconds=30,
        github_authors=("zypgithub",),
        gerrit_username="",
        gerrit_api_key="",
        inference_api_key="inference-token",
        inference_base_url="https://inference-api.nvidia.com/v1",
        inference_model="openai/openai/gpt-5.6-sol",
        inference_batch_size=8,
        inference_timeout_seconds=120,
        repo_root=repo_root,
        log_path=None,
    )


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.request = RunRequest(
            excel_path=root / "results.xlsx",
            version=VERSION,
            to=("owner@nvidia.com",),
            cc=("reviewer@nvidia.com",),
        )
        self.settings = _settings(root)

    def test_table_palettes_match_reference_email(self) -> None:
        expected = {
            "hardware": ("#CAEDFB", "#F5FBFE"),
            "detail": ("#C0E6F5", "#F2F9FC"),
        }
        for palette, colors in expected.items():
            rendered = _table(
                ("First", "Second"),
                (("one", "two"), ("three", "four")),
                palette=palette,
            )
            for color in colors:
                self.assertIn("background-color:{}".format(color), rendered)
            self.assertNotIn("background-color:#0F9ED5", rendered)
            self.assertNotIn("background-color:#156082", rendered)

    def test_excel_metrics_count_only_issue_skips_in_coverage(self) -> None:
        base = _snapshot(self.request.excel_path).selected_rows[0]
        rows = [
            replace(base, record_id="pass-1", result="pass"),
            replace(base, record_id="pass-2", result="pass"),
            replace(base, record_id="fail-1", result="fail"),
            replace(
                base,
                record_id="github-skip",
                result="skipped",
                message=(
                    "Skipped for https://github.com/sonic-net/"
                    "sonic-mgmt/issues/123"
                ),
            ),
            replace(
                base,
                record_id="redmine-skip",
                result="skipped",
                message="Known failure tracked by RM ticket 5154543",
            ),
            replace(
                base,
                record_id="plain-skip",
                result="skipped",
                message="Topology is not supported",
            ),
        ]
        snapshot = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=rows,
            result_counts={"pass": 2, "fail": 1, "skipped": 3},
            hardware_pairs=[],
            issue_skip_tickets={
                "github-skip": [
                    "https://github.com/sonic-net/sonic-mgmt/issues/123"
                ],
                "redmine-skip": [
                    "https://redmine.mellanox.com/issues/5154543"
                ],
            },
            issue_skip_analysis_complete=True,
        )

        self.assertEqual(2, len(snapshot.issue_skipped))
        self.assertAlmostEqual(60.0, snapshot.coverage)
        self.assertAlmostEqual(2 / 3 * 100.0, snapshot.pass_rate)

    def test_coverage_is_unavailable_until_skip_analysis_completes(self) -> None:
        snapshot = _snapshot(self.request.excel_path)
        skipped = replace(
            snapshot.selected_rows[0],
            record_id="skip-unknown",
            result="skipped",
        )
        snapshot.selected_rows.append(skipped)
        snapshot.result_counts["skipped"] = 1

        self.assertIsNone(snapshot.coverage)

    def test_closed_ticket_before_case_start_is_not_counted_as_issue_skip(
        self,
    ) -> None:
        github_issue = "https://github.com/sonic-net/sonic-mgmt/issues/123"
        redmine_issue = "https://redmine.mellanox.com/issues/5154543"
        base = _snapshot(self.request.excel_path).selected_rows[0]
        skipped = [
            replace(
                base,
                record_id="github-skip",
                result="skipped",
                message=github_issue,
                values={"start": "2026-09-20T12:00:00Z"},
            ),
            replace(
                base,
                record_id="redmine-skip",
                result="skipped",
                message=redmine_issue,
                values={"start": "2026-09-20T12:00:00Z"},
            ),
        ]
        workbook = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=skipped,
            result_counts={"pass": 0, "fail": 0, "skipped": 2},
            hardware_pairs=[],
            issue_skip_tickets={
                "github-skip": [github_issue],
                "redmine-skip": [redmine_issue],
            },
            issue_skip_analysis_complete=True,
        )
        report = ReportModel(
            request=self.request,
            workbook=workbook,
            semantic=build_deterministic_report(workbook),
        )

        class GitHub:
            @staticmethod
            def inspect_issue_links(urls):
                return {
                    github_issue: {
                        "state": "closed",
                        "closed_at": "2026-09-19T12:00:00Z",
                    }
                }

        class Dependencies:
            github = GitHub()

        _filter_closed_issue_skips(
            report,
            Dependencies(),
            {
                redmine_issue: {
                    "is_closed": True,
                    "closed_on": "2026-09-19T12:00:00Z",
                }
            },
            self.settings,
        )

        self.assertEqual({}, workbook.issue_skip_tickets)
        self.assertEqual([], workbook.issue_skipped)

    def test_ticket_open_at_case_start_remains_issue_skip(self) -> None:
        issue = "https://github.com/sonic-net/sonic-mgmt/issues/123"
        base = _snapshot(self.request.excel_path).selected_rows[0]
        skipped = replace(
            base,
            record_id="github-skip",
            result="skipped",
            message=issue,
            values={"start": "2026-09-20T12:00:00Z"},
        )
        workbook = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=[skipped],
            result_counts={"pass": 0, "fail": 0, "skipped": 1},
            hardware_pairs=[],
            issue_skip_tickets={"github-skip": [issue]},
            issue_skip_analysis_complete=True,
        )
        report = ReportModel(
            request=self.request,
            workbook=workbook,
            semantic=build_deterministic_report(workbook),
        )

        class GitHub:
            @staticmethod
            def inspect_issue_links(urls):
                return {
                    issue: {
                        "state": "closed",
                        "closed_at": "2026-09-21T12:00:00Z",
                    }
                }

        class Dependencies:
            github = GitHub()

        _filter_closed_issue_skips(
            report,
            Dependencies(),
            {},
            self.settings,
        )

        self.assertEqual({"github-skip": [issue]}, workbook.issue_skip_tickets)
        self.assertEqual([skipped], workbook.issue_skipped)

    def test_missing_ticket_metadata_marks_coverage_incomplete(self) -> None:
        issue = "https://github.com/sonic-net/sonic-mgmt/issues/123"
        base = _snapshot(self.request.excel_path).selected_rows[0]
        skipped = replace(
            base,
            record_id="github-skip",
            result="skipped",
            message=issue,
            values={"start": "2026-09-20T12:00:00Z"},
        )
        workbook = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=[skipped],
            result_counts={"pass": 0, "fail": 0, "skipped": 1},
            hardware_pairs=[],
            issue_skip_tickets={"github-skip": [issue]},
            issue_skip_analysis_complete=True,
        )
        report = ReportModel(
            request=self.request,
            workbook=workbook,
            semantic=build_deterministic_report(workbook),
        )

        class GitHub:
            @staticmethod
            def inspect_issue_links(urls):
                return {}

        class Dependencies:
            github = GitHub()

        _filter_closed_issue_skips(
            report,
            Dependencies(),
            {},
            self.settings,
        )

        self.assertFalse(workbook.issue_skip_analysis_complete)
        self.assertIsNone(workbook.coverage)

    def test_table_keeps_first_column_at_fixed_width(self) -> None:
        rendered = _table(
            ("Test", "Details"),
            (("tests/a.py::test_with_a_long_name", "detail"),),
            first_column_width=420,
        )

        self.assertIn("table-layout:fixed", rendered)
        self.assertIn(
            "width:420px;min-width:420px;max-width:420px",
            rendered,
        )

    def test_analysis_table_keeps_wide_multiline_testbed_column(
        self,
    ) -> None:
        group = SemanticGroup(
            group_id="failure-1",
            member_ids=["failure-1"],
            test_display="tests/a.py::test_a",
            testbeds=["SN5600-t0", "SN4280-t1-lag"],
        )

        rendered = _failure_table([group])

        self.assertIn(
            "width:320px;min-width:320px;max-width:320px",
            rendered,
        )
        self.assertIn("SN5600-t0<br>SN4280-t1-lag", rendered)

    def test_failure_html_warning_stays_with_sorted_test_case(
        self,
    ) -> None:
        warning = SemanticGroup(
            group_id="warning",
            member_ids=["warning"],
            test_display="tests/z.py::test_z",
            testbeds=["SN5600-t0"],
            comments="Under investigation",
            internal_comments="not found in dashboard",
            internal_review_status="not_found",
            internal_review_warning=True,
        )
        normal = SemanticGroup(
            group_id="normal",
            member_ids=["normal"],
            test_display="tests/a.py::test_a",
            testbeds=["SN5600-t0"],
            comments="Known issue",
            internal_comments="Analyzed root cause",
        )

        rendered = _failure_table([warning, normal])
        normal_start = rendered.index("tests/a.py::test_a")
        warning_start = rendered.index("tests/z.py::test_z")
        normal_row = rendered[normal_start: rendered.index("</tr>", normal_start)]
        warning_row = rendered[
            warning_start: rendered.index("</tr>", warning_start)
        ]

        self.assertLess(normal_start, warning_start)
        self.assertNotIn("color:#c00000", normal_row)
        self.assertIn("color:#c00000", warning_row)
        self.assertIn("not found in dashboard", warning_row)

    def test_section_title_matches_reference_spacing_and_size(self) -> None:
        rendered = _section_title(
            "Results were uploaded for the following hwsku and topology:"
        )

        self.assertEqual(2, rendered.count("&nbsp;</p>"))
        self.assertIn("font-size:11pt", rendered)
        self.assertIn(
            "<strong>Results were uploaded for the following hwsku "
            "and topology:</strong>",
            rendered,
        )
        self.assertNotIn("<h2", rendered)

    def test_all_analysis_tables_use_github_detail_palette(self) -> None:
        group = SemanticGroup(
            group_id="skip-1",
            member_ids=["skip-1"],
            test_display="tests/a.py::test_a",
            testbeds=["SN5600-t0"],
            comments="Internal issue, WIP",
            internal_comments="internal",
        )
        groups = [group, replace(group, group_id="group-2")]
        analysis_tables = (
            _failure_table(groups),
            _skip_table(groups),
            _internal_error_table(groups),
        )
        github = _table(
            ("Github Issue", "Skip condition", "Related test case"),
            (("issue-1", "condition-1", "test-1"), ("issue-2", "condition-2", "test-2")),
            palette="detail",
        )

        for color in ("#C0E6F5", "#F2F9FC"):
            marker = "background-color:{}".format(color)
            for rendered in analysis_tables:
                self.assertIn(marker, rendered)
            self.assertIn(marker, github)
        self.assertNotIn("#156082", github)
        for rendered in analysis_tables:
            self.assertIn("color:#c00000", rendered)
        self.assertNotIn("#4F81BD", analysis_tables[0])
        self.assertLess(
            analysis_tables[1].index("Test case name"),
            analysis_tables[1].index("Skipped platform or topo"),
        )
        self.assertLess(
            analysis_tables[1].index("tests/a.py::test_a"),
            analysis_tables[1].index("SN5600-t0"),
        )

    def test_hardware_rows_are_sorted_case_insensitively_by_hwsku(self) -> None:
        workbook = _snapshot(self.request.excel_path)
        workbook.hardware_pairs = [
            ("zSku", "t0"),
            ("bSku", "t1"),
            ("ASku", "t2"),
        ]
        report = ReportModel(request=self.request, workbook=workbook)

        self.assertEqual(
            ["ASku", "bSku", "zSku"],
            [row[0] for row in _hardware_rows(report)],
        )

    def test_hardware_rows_leave_missing_report_id_blank(self) -> None:
        workbook = _snapshot(self.request.excel_path)
        workbook.selected_rows[0].values.clear()
        report = ReportModel(request=self.request, workbook=workbook)

        self.assertEqual(
            "",
            _hardware_rows(report)[0][2],
        )

    def test_redactor_prefers_dashboard_platform_to_workbook_setup_fallback(
        self,
    ) -> None:
        workbook = _snapshot(self.request.excel_path)
        workbook.setup_platforms = {"private-setup": ""}
        workbook.setup_testbeds = {"private-setup": "private-setup"}
        report = ReportModel(
            request=self.request,
            workbook=workbook,
            dashboard=DashboardSnapshot(
                setup_platforms={
                    "private-setup": "x86_64-nvidia_sn5600-r0",
                },
                setup_testbeds={
                    "private-setup": "x86_64-nvidia_sn5600-r0-t0",
                },
            ),
        )

        redactor = _build_setup_redactor(report)

        self.assertEqual(
            "x86_64-nvidia_sn5600-r0-t0",
            redactor.redact_testbed("private-setup"),
        )

    def test_missing_rc_status_is_visible_without_double_colon(self) -> None:
        report = ReportModel(request=self.request)
        rendered = ReportRenderer().render(report)

        self.assertIn(
            "Unavailable (RC_STATUS failed)",
            rendered.subject,
        )
        self.assertIn(
            "Additional PR(s) on top of the public hash: "
            "Unavailable (RC_STATUS failed):",
            rendered.plain,
        )
        self.assertNotIn("public hash: :", rendered.plain)

    def test_empty_internal_error_section_is_present_in_html_and_plain(
        self,
    ) -> None:
        rendered = ReportRenderer().render(
            ReportModel(request=self.request)
        )

        self.assertIn("Err msgs detected internally", rendered.html)
        self.assertIn("Err msgs detected internally", rendered.plain)

    def test_github_skip_conditions_flatten_duplicate_blocks(self) -> None:
        issue = "https://github.com/sonic-net/sonic-mgmt/issues/15942"
        condition = "'dualtor' in topo_name"
        source = COMMENT_SEPARATOR.join(
            (
                "{} and {}".format(issue, condition),
                condition,
                "{} and {}".format(issue, condition),
            )
        )
        row = ResultRow(
            record_id="skip-1",
            excel_row=2,
            session_id="s",
            mars_key_id="k",
            testbed="tb",
            test_name="tests/a.py::test_a",
            sanitized_testname="tests/a.py::test_a",
            result="skipped",
            message=source,
            topology="t0",
            hwsku="sku",
            values={"message": source},
        )
        workbook = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=[row],
            result_counts={},
            hardware_pairs=[],
        )

        github_rows = _github_skip_rows(workbook)

        self.assertEqual(1, len(github_rows))
        self.assertEqual(condition, github_rows[0][1])
        self.assertNotIn(COMMENT_SEPARATOR, github_rows[0][1])

    def test_github_skip_conditions_stay_with_their_message_issue(
        self,
    ) -> None:
        issue_a = "https://github.com/sonic-net/sonic-mgmt/issues/100"
        issue_b = "https://github.com/sonic-net/sonic-mgmt/issues/200"
        source = COMMENT_SEPARATOR.join(
            (
                "{} and topo_name == 't0'".format(issue_a),
                "{} and platform == 'x86_64'".format(issue_b),
            )
        )
        row = ResultRow(
            record_id="skip-1",
            excel_row=2,
            session_id="s",
            mars_key_id="k",
            testbed="tb",
            test_name="tests/a.py::test_a",
            sanitized_testname="tests/a.py::test_a",
            result="skipped",
            message=normalize_text(source),
            topology="t0",
            hwsku="sku",
            values={"message": source},
        )
        workbook = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=[row],
            result_counts={},
            hardware_pairs=[],
        )

        rows = {
            issue: condition
            for issue, condition, _tests in _github_skip_rows(workbook)
        }

        self.assertEqual(
            {
                issue_a: "topo_name == 't0'",
                issue_b: "platform == 'x86_64'",
            },
            rows,
        )

    def test_github_skip_rows_sort_by_related_test_case(self) -> None:
        rows = [
            ResultRow(
                record_id="skip-z",
                excel_row=2,
                session_id="s-z",
                mars_key_id="k-z",
                testbed="tb",
                test_name="tests/z.py::test_z",
                sanitized_testname="tests/z.py::test_z",
                result="skipped",
                message=(
                    "https://github.com/sonic-net/sonic-mgmt/issues/100"
                ),
                topology="t0",
                hwsku="sku",
            ),
            ResultRow(
                record_id="skip-a",
                excel_row=3,
                session_id="s-a",
                mars_key_id="k-a",
                testbed="tb",
                test_name="tests/a.py::test_a",
                sanitized_testname="tests/a.py::test_a",
                result="skipped",
                message=(
                    "https://github.com/sonic-net/sonic-mgmt/issues/200"
                ),
                topology="t0",
                hwsku="sku",
            ),
        ]
        workbook = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=rows,
            result_counts={},
            hardware_pairs=[],
        )

        github_rows = _github_skip_rows(workbook)

        self.assertEqual(
            ["tests/a.py::test_a", "tests/z.py::test_z"],
            [row[2] for row in github_rows],
        )

    def test_github_skip_conditions_remove_logically_redundant_rules(
        self,
    ) -> None:
        broad = "asic_type in ['broadcom', 'mellanox']"
        narrow = "'dualtor' in topo_name and {}".format(broad)

        self.assertEqual(
            [broad],
            _distinct_skip_conditions([narrow, broad]),
        )
        self.assertEqual(
            ["feature_enabled"],
            _distinct_skip_conditions(
                [
                    "(feature_enabled and extra_check) or feature_enabled",
                    "feature_enabled",
                ]
            ),
        )

    def test_github_skip_rows_ignore_yaml_scope_and_dedupe_redaction(
        self,
    ) -> None:
        issue = "https://github.com/sonic-net/sonic-mgmt/issues/15942"
        workbook = _snapshot(self.request.excel_path)
        workbook.public_issue_conditions = {
            issue: ["'dualtor' in topo_name"]
        }
        group = SemanticGroup(
            group_id="skip-1",
            member_ids=["skip-1"],
            test_display=(
                "hash/test_generic_hash.py::test_hash"
                "[warm-private-a-default, warm-private-b-default]"
            ),
            testbeds=["sku-t0"],
            ticket_refs=[issue],
        )

        rows = _github_skip_rows(
            workbook,
            [group],
            SetupRedactor(
                {
                    "private-a": "Unknown platform",
                    "private-b": "Unknown platform",
                }
            ),
        )

        self.assertEqual(1, len(rows))
        self.assertEqual(issue, rows[0][0])
        self.assertEqual(
            "Issue-only skip / no extra condition shown",
            rows[0][1],
        )
        self.assertEqual(
            "hash/test_generic_hash.py::test_hash"
            "[warm-private-a-default, warm-private-b-default]",
            rows[0][2],
        )

    def test_github_skip_rows_read_tickets_only_from_excel_message(
        self,
    ) -> None:
        message_issue = (
            "https://github.com/sonic-net/sonic-mgmt/issues/100"
        )
        comment_issue = (
            "https://github.com/sonic-net/sonic-mgmt/issues/200"
        )
        yaml_issue = (
            "https://github.com/sonic-net/sonic-mgmt/issues/300"
        )
        row = ResultRow(
            record_id="skip-1",
            excel_row=2,
            session_id="s",
            mars_key_id="k",
            testbed="tb",
            test_name="tests/a.py::test_skip",
            sanitized_testname="tests/a.py::test_skip",
            result="skipped",
            message=message_issue + " and topo_name == 't0'",
            topology="t0",
            hwsku="sku",
            values={"comments": comment_issue},
        )
        workbook = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=[row],
            result_counts={"pass": 0, "fail": 0, "skipped": 1},
            hardware_pairs=[("sku", "t0")],
            public_issue_conditions={yaml_issue: ["platform == 'x'"]},
        )
        semantic = build_deterministic_report(workbook)

        rows = _github_skip_rows(workbook, semantic.skip_groups)

        self.assertEqual(1, len(rows))
        self.assertEqual(message_issue, rows[0][0])
        self.assertEqual("topo_name == 't0'", rows[0][1])
        self.assertEqual("tests/a.py::test_skip", rows[0][2])

    def test_url_only_internal_comment_is_not_sent_to_ai(self) -> None:
        group = SemanticGroup(
            group_id="failure-1",
            member_ids=["failure-1"],
            test_display="tests/a.py::test_a",
            testbeds=["SN5600"],
            internal_comments="https://redmine.mellanox.com/issues/123",
            ticket_refs=["https://redmine.mellanox.com/issues/123"],
            redmine_urls=["https://redmine.mellanox.com/issues/123"],
        )
        report = ReportModel(
            request=self.request,
            semantic=SemanticReport(failure_groups=[group]),
        )

        class NoSummaryAI:
            @staticmethod
            def summarize(groups, redactor):
                raise AssertionError("URL-only comments must not reach AI")

        class Dependencies:
            ai = NoSummaryAI()

        _summarize_comments(
            report,
            Dependencies(),
            self.settings,
        )

        self.assertEqual("", group.comments)

    def test_failure_comment_policy_uses_fixed_priority_order(self) -> None:
        ticket_pr = "https://redmine.mellanox.com/issues/1001"
        ticket_issue = "https://redmine.mellanox.com/issues/1002"
        ticket_test = "https://redmine.mellanox.com/issues/1003"
        ticket_internal = "https://redmine.mellanox.com/issues/1004"
        groups = [
            SemanticGroup(
                group_id="under-investigation",
                member_ids=["under-investigation"],
                test_display="tests/a.py::test_a",
                testbeds=[],
                comments="old summary",
                redmine_urls=[ticket_pr],
                internal_review_status="not_found",
            ),
            SemanticGroup(
                group_id="fixed",
                member_ids=["fixed"],
                test_display="tests/b.py::test_b",
                testbeds=[],
                comments="old summary",
                redmine_urls=[ticket_pr],
            ),
            SemanticGroup(
                group_id="public-issue",
                member_ids=["public-issue"],
                test_display="tests/c.py::test_c",
                testbeds=[],
                comments="old summary",
                redmine_urls=[ticket_issue],
            ),
            SemanticGroup(
                group_id="test-issue",
                member_ids=["test-issue"],
                test_display="tests/d.py::test_d",
                testbeds=[],
                comments="old summary",
                redmine_urls=[ticket_test],
            ),
            SemanticGroup(
                group_id="internal-issue",
                member_ids=["internal-issue"],
                test_display="tests/e.py::test_e",
                testbeds=[],
                comments="old summary",
                redmine_urls=[ticket_internal],
            ),
            SemanticGroup(
                group_id="no-ticket",
                member_ids=["no-ticket"],
                test_display="tests/f.py::test_f",
                testbeds=[],
                comments="AI summary",
            ),
        ]
        semantic = SemanticReport(failure_groups=groups)
        issue_url = (
            "https://github.com/sonic-net/sonic-buildimage/issues/10"
        )
        pr_url = "https://github.com/sonic-net/sonic-buildimage/pull/20"
        contexts = {
            ticket_pr: {"is_test_issue": False},
            ticket_issue: {"is_test_issue": False},
            ticket_test: {"is_test_issue": True},
            ticket_internal: {"is_test_issue": False},
        }

        _apply_failure_comment_policy(
            semantic,
            contexts,
            {
                ticket_pr: "github_pr:" + pr_url,
                ticket_issue: "github_issue:" + issue_url,
                ticket_test: "none",
                ticket_internal: "none",
            },
        )

        self.assertEqual(
            [
                "Under investigation",
                "old summary\nProduction fix: " + pr_url,
                "old summary\nProduction bug: " + issue_url,
                "Internal test issue, WIP",
                "Internal issue, WIP",
                "AI summary",
            ],
            [group.comments for group in groups],
        )
        rendered = _failure_table(groups)
        self.assertIn('<a href="{}"'.format(issue_url), rendered)
        self.assertIn('<a href="{}"'.format(pr_url), rendered)

    def test_verification_summary_is_test_issue_when_inspection_unavailable(
        self,
    ) -> None:
        ticket = "https://redmine.mellanox.com/issues/5172208"
        group = SemanticGroup(
            group_id="verification",
            member_ids=["verification"],
            test_display="everflow/test_everflow_ipv6.py::test_mtu_drop",
            testbeds=["x86_64-nvidia_sn6600_ld-r0-t0"],
            redmine_urls=[ticket],
            internal_comments=(
                "[SONiC - Verification] Bug SW #5172208: "
                "MIRROR_EGRESS_ACTION test failure"
            ),
        )

        _apply_failure_comment_policy(
            SemanticReport(failure_groups=[group]),
            {},
            {},
        )

        self.assertEqual("Internal test issue, WIP", group.comments)

    def test_verification_summary_survives_metadata_unavailable_rendering(
        self,
    ) -> None:
        ticket = "https://redmine.mellanox.com/issues/5172208"
        group = SemanticGroup(
            group_id="verification-fallback",
            member_ids=["verification-fallback"],
            test_display="everflow/test_everflow_ipv6.py::test_mtu_drop",
            testbeds=["x86_64-nvidia_sn6600_ld-r0-t0"],
            redmine_urls=[ticket],
            ticket_refs=[ticket],
            internal_comments=(
                "[SONiC - Verification] Bug SW #5172208: test issue\n" +
                ticket
            ),
        )
        semantic = SemanticReport(failure_groups=[group])

        _prepare_redmine_comments(semantic, {})
        _apply_failure_comment_policy(semantic, {}, {})

        self.assertEqual("Internal test issue, WIP", group.comments)
        self.assertIn("[SONiC - Verification]", group.internal_comments)
        self.assertNotIn("[Unknown project]", group.internal_comments)

        group.comments = ""
        _apply_failure_comment_policy(
            semantic,
            {ticket: {"is_test_issue": False, "is_closed": False}},
            {},
        )
        self.assertEqual("Internal test issue, WIP", group.comments)

        group.comments = ""
        group.redmine_urls = []
        group.ticket_refs = []
        _apply_failure_comment_policy(semantic, {}, {})
        self.assertEqual("Internal test issue, WIP", group.comments)

    def test_mixed_ticket_group_with_verification_is_test_issue(
        self,
    ) -> None:
        test_ticket = "https://redmine.mellanox.com/issues/5172208"
        design_ticket = "https://redmine.mellanox.com/issues/5005586"
        group = SemanticGroup(
            group_id="mixed",
            member_ids=["mixed"],
            test_display="everflow/test_everflow_ipv6.py::test_mtu_drop",
            testbeds=["x86_64-nvidia_sn6600_ld-r0-t0"],
            redmine_urls=[test_ticket, design_ticket],
            internal_comments=(
                "[SONiC - Verification] Bug SW #5172208: test issue\n"
                "[SONIC - Design] Bug SW #5005586: product issue"
            ),
        )

        _apply_failure_comment_policy(
            SemanticReport(failure_groups=[group]),
            {
                test_ticket: {
                    "is_test_issue": True,
                    "is_closed": True,
                },
            },
            {},
        )

        self.assertEqual("Internal test issue, WIP", group.comments)

    def test_only_full_sonic_verification_marker_is_test_issue(
        self,
    ) -> None:
        ticket = "https://redmine.mellanox.com/issues/5216117"
        group = SemanticGroup(
            group_id="verification-project",
            member_ids=["verification-project"],
            test_display="vxlan/test_vxlan_ecmp.py",
            testbeds=[],
            redmine_urls=[ticket],
            ticket_refs=[ticket],
            internal_comments="Verification is still pending",
        )
        semantic = SemanticReport(failure_groups=[group])

        _apply_failure_comment_policy(
            semantic,
            {
                ticket: {
                    "project": "SONiC - Verification",
                    "is_test_issue": False,
                    "is_closed": False,
                },
            },
            {},
        )
        self.assertEqual("Internal test issue, WIP", group.comments)

        group.comments = ""
        _apply_failure_comment_policy(
            semantic,
            {
                ticket: {
                    "project": "SONiC - Design",
                    "summary": "Generic verification failure",
                    "is_test_issue": False,
                    "is_closed": False,
                },
            },
            {},
        )
        self.assertEqual("Internal issue, WIP", group.comments)

    def test_sonic_verification_skip_is_internal_test_issue(
        self,
    ) -> None:
        ticket = "https://redmine.mellanox.com/issues/5216117"
        group = SemanticGroup(
            group_id="verification-skip",
            member_ids=["verification-skip"],
            test_display="vxlan/test_vxlan_ecmp.py",
            testbeds=["x86_64-nvidia_sn5640-r0-t1"],
            redmine_urls=[ticket],
            ticket_refs=[ticket],
        )
        semantic = SemanticReport(skip_groups=[group])

        _prepare_redmine_comments(
            semantic,
            {
                ticket: (
                    "[SONiC - Verification] Bug SW #5216117: "
                    "test infrastructure issue"
                ),
            },
        )

        self.assertEqual("Internal test issue, WIP", group.comments)

    def test_failure_policy_handles_closed_rm_and_sensitive_terms(
        self,
    ) -> None:
        closed_ticket = "https://redmine.mellanox.com/issues/2001"
        closed_test_ticket = (
            "https://redmine.mellanox.com/issues/2002"
        )
        groups = [
            SemanticGroup(
                group_id="closed-internal",
                member_ids=["closed-internal"],
                test_display="tests/a.py::test_a",
                testbeds=[],
                redmine_urls=[closed_ticket],
            ),
            SemanticGroup(
                group_id="closed-test",
                member_ids=["closed-test"],
                test_display="tests/b.py::test_b",
                testbeds=[],
                redmine_urls=[closed_test_ticket],
            ),
            SemanticGroup(
                group_id="sensitive",
                member_ids=["sensitive"],
                test_display="tests/c.py::test_c",
                testbeds=[],
                comments="SAI profile mismatch",
                internal_comments="BIOS and FW evidence",
            ),
        ]

        _apply_failure_comment_policy(
            SemanticReport(failure_groups=groups),
            {
                closed_ticket: {
                    "is_test_issue": False,
                    "is_closed": True,
                },
                closed_test_ticket: {
                    "is_test_issue": True,
                    "is_closed": True,
                },
            },
            {},
        )

        self.assertEqual("Internal issue", groups[0].comments)
        self.assertEqual("Internal test issue", groups[1].comments)
        self.assertEqual("Internal issue, WIP", groups[2].comments)

    def test_dashboard_redmine_analysis_is_preserved_and_enriched(
        self,
    ) -> None:
        ticket = "https://redmine.mellanox.com/issues/4704448"
        analysis = (
            "DPU FPGA update times out when the DPUs are powered on."
        )
        summary = (
            "[HW Mgmt Pkg - Design] Bug SW #4704448: "
            "DPU FPGA burn takes longer than 30 minutes"
            " | Assignee: Owner | Status: Assigned"
        )
        group = SemanticGroup(
            group_id="failure-fwutil",
            member_ids=["failure-fwutil"],
            test_display=(
                "platform_tests/fwutil/test_fwutil.py::"
                "test_fwutil_update_current"
            ),
            testbeds=["SN4280"],
            internal_comments=COMMENT_SEPARATOR.join(
                (analysis, ticket, ticket)
            ),
            ticket_refs=[ticket],
            redmine_urls=[ticket],
        )

        _prepare_redmine_comments(
            SemanticReport(failure_groups=[group]),
            {ticket: summary},
        )

        self.assertEqual([ticket], group.redmine_urls)
        self.assertIn(analysis, group.internal_comments)
        self.assertEqual(1, group.internal_comments.count(summary))
        self.assertNotIn(ticket, group.internal_comments)

    def test_skip_internal_review_contains_only_ticket_summary(
        self,
    ) -> None:
        ticket = "https://redmine.mellanox.com/issues/5216117"
        analysis = "BFD must be enabled across all PTF ports."
        summary = (
            "[SONiC - Verification] Bug SW #5216117: "
            "enable BFD across all PTF ports"
        )
        group = SemanticGroup(
            group_id="skip-vxlan",
            member_ids=["skip-vxlan"],
            test_display="vxlan/test_vxlan_ecmp.py",
            testbeds=["SN5640"],
            internal_comments=COMMENT_SEPARATOR.join((analysis, ticket)),
            ticket_refs=[ticket],
            redmine_urls=[ticket],
        )

        _prepare_redmine_comments(
            SemanticReport(skip_groups=[group]),
            {ticket: summary},
        )

        self.assertEqual("Internal test issue, WIP", group.comments)
        self.assertEqual(summary, group.internal_comments)

    def test_failure_policy_uses_verified_direct_links(
        self,
    ) -> None:
        issue_url = (
            "https://github.com/sonic-net/sonic-buildimage/issues/10"
        )
        pr_url = "https://github.com/sonic-net/sonic-mgmt/pull/27108"
        groups = [
            SemanticGroup(
                group_id="direct-issue",
                member_ids=["direct-issue"],
                test_display="tests/a.py::test_a",
                testbeds=[],
                comments="AI summary",
            ),
            SemanticGroup(
                group_id="missing",
                member_ids=["missing"],
                test_display="tests/c.py::test_c",
                testbeds=[],
                comments="stale summary",
                internal_review_status="partial",
            ),
        ]

        _apply_failure_comment_policy(
            SemanticReport(failure_groups=groups),
            {},
            {},
            {
                "direct-issue": "github_issue:" + issue_url,
                "missing": "github_pr:" + pr_url,
            },
        )

        self.assertEqual(
            "AI summary\nProduction bug: " + issue_url,
            groups[0].comments,
        )
        self.assertEqual("Under investigation", groups[1].comments)

    def test_redmine_link_ai_compares_current_and_github_evidence(
        self,
    ) -> None:
        ticket = "https://redmine.mellanox.com/issues/1001"
        issue_url = (
            "https://github.com/sonic-net/sonic-buildimage/issues/10"
        )
        group = SemanticGroup(
            group_id="failure-1",
            member_ids=["failure-1"],
            test_display="tests/a.py::test_a",
            testbeds=[],
            comments="Generalized service startup failure",
            internal_comments="Dashboard: service exits during startup",
            cause_evidence="ERR service: startup failed",
            redmine_urls=[ticket],
            internal_review_status="has_analysis",
        )
        report = ReportModel(
            request=self.request,
            semantic=SemanticReport(failure_groups=[group]),
            redactor=SetupRedactor({}),
        )

        class GitHub:
            @staticmethod
            def inspect_issue_links(urls):
                self.assertEqual([issue_url], urls)
                return {
                    issue_url: {
                        "url": issue_url,
                        "kind": "issues",
                        "title": "Service exits during startup",
                        "body": "Startup fails while loading configuration.",
                        "state": "open",
                        "merged": False,
                        "labels": ["bug"],
                    }
                }

        class AI:
            @staticmethod
            def review_redmine_links(contexts, redactor):
                context = contexts[ticket]
                self.assertIn(
                    "ERR service: startup failed",
                    context["current_failure_evidence"],
                )
                self.assertEqual(
                    "Service exits during startup",
                    context["github_link_details"][issue_url]["title"],
                )
                return {ticket: "github_issue:" + issue_url}

        class Dependencies:
            github = GitHub()
            ai = AI()

        decisions = _review_redmine_links(
            report,
            Dependencies(),
            {
                ticket: {
                    "subject": "Internal service failure",
                    "evidence": "Related public issue " + issue_url,
                    "github_issue_urls": [issue_url],
                    "github_pr_urls": [],
                    "is_test_issue": False,
                }
            },
            self.settings,
        )
        _apply_failure_comment_policy(
            report.semantic,
            {ticket: {"is_test_issue": False}},
            decisions,
        )

        self.assertEqual(
            {ticket: "github_issue:" + issue_url},
            decisions,
        )
        self.assertEqual(
            "Generalized service startup failure\n"
            "Production bug: Service exits during startup | " + issue_url,
            group.comments,
        )

    def test_failure_comment_policy_labels_all_public_link_types(self) -> None:
        urls = (
            "https://github.com/sonic-net/sonic-mgmt/issues/1",
            "https://github.com/sonic-net/sonic-mgmt/pull/2",
            "https://github.com/sonic-net/sonic-buildimage/issues/3",
            "https://github.com/sonic-net/sonic-swss/pull/4",
        )
        groups = [
            SemanticGroup(
                group_id="link-{}".format(index),
                member_ids=["link-{}".format(index)],
                test_display="tests/a.py::test_a",
                testbeds=[],
            )
            for index in range(len(urls))
        ]
        decisions = {
            group.group_id: (
                "github_issue:" if "/issues/" in url else "github_pr:"
            ) + url
            for group, url in zip(groups, urls)
        }

        _apply_failure_comment_policy(
            SemanticReport(failure_groups=groups),
            {},
            {},
            public_link_decisions=decisions,
        )

        self.assertEqual(
            [
                "Test bug: " + urls[0],
                "Test fix: " + urls[1],
                "Production bug: " + urls[2],
                "Production fix: " + urls[3],
            ],
            [group.comments for group in groups],
        )

    def test_active_redmine_github_pr_is_selected_without_ai(
        self,
    ) -> None:
        ticket = "https://redmine.mellanox.com/issues/5100919"
        issue_url = (
            "https://github.com/sonic-net/sonic-buildimage/issues/28170"
        )
        pr_url = (
            "https://github.com/sonic-net/sonic-buildimage/pull/28827"
        )
        group = SemanticGroup(
            group_id="dhcp-autorestart",
            member_ids=["dhcp-autorestart"],
            test_display=(
                "autorestart/test_container_autorestart.py::"
                "test_containers_autorestart[dhcp_server]"
            ),
            testbeds=["SN4280"],
            internal_comments="Missing process-monitoring messages",
            ticket_refs=[ticket, issue_url],
            redmine_urls=[ticket],
        )
        report = ReportModel(
            request=self.request,
            semantic=SemanticReport(failure_groups=[group]),
            redactor=SetupRedactor({}),
        )

        class GitHub:
            @staticmethod
            def inspect_issue_links(urls):
                return {
                    url: {
                        "url": url,
                        "title": "Related rsyslog change",
                        "body": "Restores process-monitoring messages",
                    }
                    for url in urls
                }

        class AI:
            @staticmethod
            def review_redmine_links(contexts, redactor):
                raise AssertionError(
                    "A current active fix PR is authoritative"
                )

        class Dependencies:
            github = GitHub()
            ai = AI()

        decisions = _review_redmine_links(
            report,
            Dependencies(),
            {
                ticket: {
                    "subject": "Missing process-monitoring messages",
                    "evidence": "The current fix is " + pr_url,
                    "github_issue_urls": [issue_url],
                    "github_pr_urls": [pr_url],
                    "active_github_pr_urls": [pr_url],
                },
            },
            self.settings,
        )

        self.assertEqual(
            {ticket: "github_pr:" + pr_url},
            decisions,
        )

    def test_direct_issue_with_matching_test_name_is_deterministic(
        self,
    ) -> None:
        issue_url = (
            "https://github.com/sonic-net/sonic-mgmt/issues/23601"
        )
        group = SemanticGroup(
            group_id="dhcp-failure",
            member_ids=["dhcp-failure"],
            test_display=(
                "dhcp_relay/test_dhcp_relay_stress.py::"
                "test_dhcp_relay_restart_with_stress"
                "[sonic-relay-agent]"
            ),
            testbeds=["SN2700-t0"],
            internal_comments=issue_url,
            ticket_refs=[issue_url],
        )
        report = ReportModel(
            request=self.request,
            semantic=SemanticReport(failure_groups=[group]),
            redactor=SetupRedactor({}),
        )

        class GitHub:
            @staticmethod
            def inspect_issue_links(urls):
                return {
                    issue_url: {
                        "url": issue_url,
                        "kind": "issues",
                        "title": "Regression: test_dhcp_relay failed",
                        "body": (
                            "All test_dhcp_relay cases fail with "
                            "sonic-relay-agent."
                        ),
                        "state": "closed",
                    },
                }

        class AI:
            @staticmethod
            def review_public_links(contexts, redactor):
                raise AssertionError(
                    "Exact test evidence must not depend on AI"
                )

        class Dependencies:
            github = GitHub()
            ai = AI()

        decisions = _review_failure_public_links(
            report,
            Dependencies(),
            self.settings,
        )
        _apply_failure_comment_policy(
            report.semantic,
            {},
            {},
            public_link_decisions=decisions,
        )

        self.assertEqual(
            {"dhcp-failure": "github_issue:" + issue_url},
            decisions,
        )
        self.assertEqual(
            "Test bug: Regression: test_dhcp_relay failed | " + issue_url,
            group.comments,
        )

    def test_single_explicit_public_link_does_not_depend_on_ai_matching(
        self,
    ) -> None:
        issue_url = "https://github.com/sonic-net/sonic-mgmt/issues/27717"
        group = SemanticGroup(
            group_id="copp-failure",
            member_ids=["copp-failure"],
            test_display="copp/test_copp.py::TestCOPP",
            testbeds=["SN4280"],
            internal_comments="ptf issue\n" + issue_url,
            ticket_refs=[issue_url],
        )
        report = ReportModel(
            request=self.request,
            semantic=SemanticReport(failure_groups=[group]),
            redactor=SetupRedactor({}),
        )

        class GitHub:
            @staticmethod
            def inspect_issue_links(urls):
                return {
                    issue_url: {
                        "title": "Bug: nnpy connection timed out",
                        "body": "PTF packet injection intermittently times out.",
                    }
                }

        class AI:
            @staticmethod
            def review_public_links(contexts, redactor):
                raise AssertionError("an explicit singleton link is authoritative")

        class Dependencies:
            github = GitHub()
            ai = AI()

        decisions = _review_failure_public_links(
            report,
            Dependencies(),
            self.settings,
        )
        _apply_failure_comment_policy(
            report.semantic,
            {},
            {},
            public_link_decisions=decisions,
        )

        self.assertEqual(
            {"copp-failure": "github_issue:" + issue_url},
            decisions,
        )
        self.assertIn(issue_url, group.comments)

    def test_unconfigured_gerrit_title_lookup_is_skipped(self) -> None:
        change = (
            "https://git-nbu-sw.nvidia.com/r/c/switchx/sonic/"
            "sonic-mgmt/+/338605"
        )
        report = ReportModel(
            request=self.request,
            semantic=SemanticReport(failure_groups=[SemanticGroup(
                group_id="internal-change",
                member_ids=["internal-change"],
                test_display="tests/a.py::test_a",
                testbeds=[],
                internal_comments=change,
            )]),
        )

        class Gerrit:
            @staticmethod
            def describe_changes(urls):
                raise AssertionError("unconfigured Gerrit must not be called")

        class Dependencies:
            gerrit = Gerrit()

        _collect_internal_change_titles(
            report,
            Dependencies(),
            self.settings,
        )

        self.assertEqual([], report.errors)

    def test_all_testbed_cells_render_one_value_per_line(self) -> None:
        group = SemanticGroup(
            group_id="group-1",
            member_ids=["group-1"],
            test_display="tests/a.py::test_a",
            testbeds=["SN5600-t0", "SN4700-t1"],
            comments="comment",
            internal_comments="internal",
        )

        for rendered in (
            _failure_table([group]),
            _skip_table([group]),
            _internal_error_table([group]),
        ):
            self.assertIn("SN5600-t0<br>SN4700-t1", rendered)
            self.assertNotIn("SN5600-t0, SN4700-t1", rendered)

    def test_hardware_table_keeps_empty_report_id_column(
        self,
    ) -> None:
        workbook = _snapshot(self.request.excel_path)
        workbook.selected_rows = [
            replace(workbook.selected_rows[0], values={})
        ]
        report = ReportModel(
            request=self.request,
            workbook=workbook,
        )

        rendered = ReportRenderer().render(report)

        self.assertIn("ReportId", rendered.html)
        self.assertIn("ReportId", rendered.plain)
        self.assertEqual(
            [("sku", "t0", "")],
            _hardware_rows(report),
        )
        self.assertIn("HardwareSku", rendered.html)
        self.assertIn("Topology", rendered.html)

    def test_mgmt_prs_filter_github_candidates_by_release_and_draft(
        self,
    ) -> None:
        exact_url = "https://github.com/sonic-net/sonic-mgmt/pull/100"
        unrelated_url = (
            "https://github.com/sonic-net/sonic-mgmt/pull/101"
        )
        non_nvidia_url = (
            "https://github.com/sonic-net/sonic-mgmt/pull/102"
        )
        exact_pr = PullRequest(
            url=exact_url,
            title="GitHub candidate",
            author="zypgithub",
            state="open",
            labels=("Request for 202608 branch",),
        )
        unrelated_pr = PullRequest(
            url=unrelated_url,
            title="Candidate without target release label",
            author="zypgithub",
            state="open",
            labels=("Request for 202605 branch",),
        )
        non_nvidia_pr = PullRequest(
            url=non_nvidia_url,
            title="Target release candidate from another author",
            author="community-user",
            state="open",
            labels=("Request for 202608 branch",),
        )
        report = ReportModel(request=self.request)

        class GitHub:
            @staticmethod
            def get_open_mgmt_prs_by_authors(authors):
                self.assertEqual(["zypgithub"], authors)
                return [exact_pr, unrelated_pr, non_nvidia_pr]

        class Dependencies:
            github = GitHub()

        _collect_mgmt_prs(
            report,
            Dependencies(),
            self.settings,
        )

        self.assertEqual(
            [exact_url],
            [pr.url for pr in report.mgmt_prs],
        )

    def test_mgmt_prs_are_omitted_when_github_inventory_is_unavailable(
        self,
    ) -> None:
        report = ReportModel(request=self.request)

        class GitHub:
            @staticmethod
            def get_open_mgmt_prs_by_authors(authors):
                self.assertEqual(["zypgithub"], authors)
                raise RuntimeError("GitHub rate limit")

        class Dependencies:
            github = GitHub()

        _collect_mgmt_prs(
            report,
            Dependencies(),
            self.settings,
        )

        self.assertEqual([], report.mgmt_prs)
        self.assertEqual(
            ["GitHub open pull request inventory"],
            [error.stage for error in report.errors],
        )

    def test_analysis_tables_sort_groups_by_test_case(self) -> None:
        groups = [
            SemanticGroup(
                group_id="z-group",
                member_ids=["z-group"],
                test_display="tests/z.py::test_z",
                testbeds=["SN5600-t0"],
                comments="z comment",
                internal_comments="z internal",
            ),
            SemanticGroup(
                group_id="a-group",
                member_ids=["a-group"],
                test_display="tests/a.py::test_a",
                testbeds=["SN5600-t0"],
                comments="a comment",
                internal_comments="a internal",
            ),
        ]

        for rendered in (
            _failure_table(groups),
            _skip_table(groups),
            _internal_error_table(groups),
        ):
            self.assertLess(
                rendered.index("tests/a.py::test_a"),
                rendered.index("tests/z.py::test_z"),
            )

    def test_nested_internal_comment_blocks_are_deduplicated(self) -> None:
        known_issue = (
            "Known issue from RC 70. fixed- "
            "https://git-nbu-sw.nvidia.com/r/c/switchx/sonic/"
            "sonic-mgmt/+/344579"
        )
        ticket = "https://redmine.mellanox.com/issues/5202742"
        configuration = (
            "configuration issue - fixed in the config DB "
            "(remove debug feature that not supported)"
        )

        self.assertEqual(
            COMMENT_SEPARATOR.join((known_issue, ticket, configuration)),
            join_comment_blocks(
                (
                    COMMENT_SEPARATOR.join((known_issue, ticket)),
                    COMMENT_SEPARATOR.join((known_issue, configuration, ticket)),
                )
            ),
        )

    def test_internal_review_removes_separators_and_labels_links(self) -> None:
        test_issue = "https://github.com/sonic-net/sonic-mgmt/issues/123"
        production_fix = (
            "https://github.com/sonic-net/sonic-buildimage/pull/456"
        )
        group = SemanticGroup(
            group_id="links",
            member_ids=["links"],
            test_display="tests/a.py::test_a",
            testbeds=[],
            internal_comments=COMMENT_SEPARATOR.join(
                ("Root cause", test_issue, production_fix)
            ),
        )

        _prepare_redmine_comments(SemanticReport(failure_groups=[group]))

        self.assertNotIn(COMMENT_SEPARATOR, group.internal_comments)
        self.assertIn("Test bug: " + test_issue, group.internal_comments)
        self.assertIn(
            "Production fix: " + production_fix,
            group.internal_comments,
        )

    def test_failure_comments_put_one_sentence_before_classified_link(
        self,
    ) -> None:
        issue = "https://github.com/sonic-net/sonic-mgmt/issues/123"
        group = SemanticGroup(
            group_id="links",
            member_ids=["links"],
            test_display="tests/a.py::test_a",
            testbeds=[],
            comments=(
                "The invalid test expectation causes the failure. "
                "Investigation history should be omitted."
            ),
            internal_comments="Investigation history " + issue,
            link_titles={issue: "Fix invalid test expectation"},
        )
        semantic = SemanticReport(failure_groups=[group])

        _apply_failure_comment_policy(
            semantic,
            {},
            {},
            {"links": "github_issue:" + issue},
        )

        expected = "Test bug: Fix invalid test expectation | " + issue
        self.assertEqual(
            "The invalid test expectation causes the failure.\n" + expected,
            group.comments,
        )
        self.assertEqual(
            "The invalid test expectation causes the failure.\n" + expected,
            group.internal_comments,
        )

    def test_internal_links_are_hidden_from_public_comments_and_compacted(
        self,
    ) -> None:
        change = (
            "https://git-nbu-sw.nvidia.com/r/c/switchx/sonic/"
            "sonic-mgmt/+/348618"
        )
        group = SemanticGroup(
            group_id="internal-link",
            member_ids=["internal-link"],
            test_display="platform_tests/fwutil/test_fwutil.py",
            testbeds=[],
            comments="The power-cycle implementation uses the wrong backend.",
            internal_comments=(
                "348618: Use Redfish PowerCycle for BMC-based power cycle |\n" +
                "Test issue, fixed in 202605\n" + change
            ),
        )
        semantic = SemanticReport(failure_groups=[group])

        _apply_failure_comment_policy(semantic, {}, {})

        self.assertEqual(
            "The power-cycle implementation uses the wrong backend.",
            group.comments,
        )
        self.assertEqual(
            "The power-cycle implementation uses the wrong backend.\n"
            "Test fix: Use Redfish PowerCycle for BMC-based power cycle | " + change,
            group.internal_comments,
        )
        self.assertNotIn("git-nbu-sw", group.comments)

    def test_internal_review_deduplicates_same_links_in_different_order(
        self,
    ) -> None:
        first = "https://git-nbu-sw.nvidia.com/r/c/project/+/347432"
        second = "https://git-nbu-sw.nvidia.com/r/c/project/+/349054"
        group = SemanticGroup(
            group_id="reordered-links",
            member_ids=["reordered-links"],
            test_display="tests/a.py::test_a",
            testbeds=[],
            comments="The recovery retry logic is incomplete.",
            internal_comments=COMMENT_SEPARATOR.join(
                (
                    "fixing CR, rerun passed " + first + " | " + second,
                    "fixing CR, rerun passed " + second + " | " + first,
                )
            ),
        )

        _apply_failure_comment_policy(
            SemanticReport(failure_groups=[group]),
            {},
            {},
        )

        self.assertEqual(1, group.internal_comments.count(first))
        self.assertEqual(1, group.internal_comments.count(second))
        self.assertEqual(3, len(group.internal_comments.splitlines()))

    def _dependencies(
        self,
        transport,
        redmine=None,
        workbook=None,
        git=None,
        ai=None,
        dashboard=None,
    ):
        git = git or _Git()
        artifacts = _Artifacts(self.temp_dir.name)
        dependencies = PipelineDependencies(
            workbook=_Workbook(workbook or _snapshot(self.request.excel_path)),
            dashboard=dashboard or _Dashboard(),
            git=git,
            github=_GitHub(),
            redmine=redmine or _Redmine(),
            ai=ai or _AI(),
            artifacts=artifacts,
            renderer=ReportRenderer(),
            transport=transport,
        )
        return dependencies, git, artifacts

    def test_full_success_is_silent_and_delivers_without_attachment(self) -> None:
        transport = _CapturingTransport()
        dependencies, git, artifacts = self._dependencies(transport)
        stderr = io.StringIO()
        result = run(self.request, self.settings, dependencies, stderr=stderr)

        self.assertEqual(0, result)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(1, len(transport.messages))
        message = transport.messages[0][0]
        self.assertEqual("owner@nvidia.com", message["To"])
        self.assertEqual("reviewer@nvidia.com", message["Cc"])
        self.assertEqual([], list(message.iter_attachments()))
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("Fix image build", html)
        self.assertIn("Fix mgmt test", html)
        self.assertIn(
            '1. <a href="https://github.com/sonic-net/sonic-buildimage/pull/9">',
            html,
        )
        self.assertIn(
            '1. <a href="https://github.com/sonic-net/sonic-mgmt/pull/7">',
            html,
        )
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn(
            "1. https://github.com/sonic-net/sonic-buildimage/pull/9 - Fix image build",
            plain,
        )
        self.assertIn(
            "1. https://github.com/sonic-net/sonic-mgmt/pull/7 - Fix mgmt test",
            plain,
        )
        self.assertIn("SN5600-t0", html)
        self.assertIn("sonic-buildimage branch:", html)
        self.assertIn(
            '<strong style="color:#000000;font-weight:700">'
            "202608_RC</strong>",
            html,
        )
        self.assertNotIn("secret-setup-01", html)
        group = artifacts.semantic.failure_groups[0]
        self.assertEqual("Generalized AI summary", group.comments)
        self.assertEqual(
            "Generalized AI summary",
            group.internal_comments,
        )
        self.assertEqual(
            "Nvidia | Uploading test results to a Kusto/Azure Data | "
            "202608 | deadbeef | def",
            message["Subject"],
        )
        self.assertNotIn("Build Summary", html)
        self.assertNotIn("Hardware reports", html)
        self.assertIn("Hi All", html)
        self.assertIn("HardwareSku", html)
        self.assertIn("Topology", html)
        self.assertIn("ReportId", html)
        self.assertIn(">report-uuid</td>", html)
        self.assertIn("<th style=", html)
        self.assertIn('width="360"', html)
        self.assertIn("width:360px;min-width:360px", html)
        for color in (
            "#CAEDFB",
            "#F5FBFE",
            "#C0E6F5",
            "#F2F9FC",
        ):
            self.assertIn("background-color:{}".format(color), html)
        self.assertNotIn("background-color:#0F9ED5", html)
        self.assertNotIn("background-color:#156082", html)
        self.assertIn(">Testbed</th>", html)
        self.assertNotIn(">Platform</th>", html)
        self.assertIn("Comments for internal review", html)
        self.assertIn("color:#c00000", html)
        for value in (
            "deadbeef",
            "202608",
            "def",
            VERSION,
            "100%",
            "0%",
        ):
            self.assertIn(
                '<strong style="color:#000000;font-weight:700">' +
                value +
                "</strong>",
                html,
            )
        self.assertLess(
            html.index("Github issues that affects tests coverage:"),
            html.index("Err msgs detected internally:"),
        )
        self.assertTrue(git.cleaned)

    def test_unresolved_dashboard_analysis_is_refreshed_before_grouping(
        self,
    ) -> None:
        class RefreshingDashboard:
            def __init__(self):
                self.calls = 0

            def fetch(self, version):
                self.calls += 1
                analysis = (
                    ""
                    if self.calls == 1
                    else (
                        "Analysis became available on "
                        "refreshed-private-setup before rendering"
                    )
                )
                return DashboardSnapshot(
                    coverage=91.2,
                    pass_rate=98.4,
                    setup_platforms=(
                        {"refreshed-private-setup": "SN5600"}
                        if self.calls > 1
                        else {}
                    ),
                    analyses=[
                        DashboardAnalysis(
                            session_id="s",
                            key_id="k",
                            test_name="tests/a.py::test_a",
                            analysis=analysis,
                        )
                    ],
                )

        dashboard = RefreshingDashboard()
        transport = _CapturingTransport()
        dependencies, _, artifacts = self._dependencies(
            transport,
            dashboard=dashboard,
        )

        self.assertEqual(
            0,
            run(
                self.request,
                self.settings,
                dependencies,
                stderr=io.StringIO(),
            ),
        )

        self.assertEqual(2, dashboard.calls)
        group = artifacts.semantic.failure_groups[0]
        self.assertEqual(
            "Generalized AI summary",
            group.internal_comments,
        )
        self.assertNotIn(
            "refreshed-private-setup",
            transport.messages[0][0].as_string(),
        )
        self.assertNotIn(
            "not analyzed",
            transport.messages[0][0].as_string(),
        )

    def test_internal_error_messages_use_ai_extracted_logs(self) -> None:
        class ErrorRedmine(_Redmine):
            def find_log_analyzer_errors(self, version, cutoff_date):
                self.cutoffs.append(cutoff_date)
                return [
                    SemanticGroup(
                        group_id="redmine-1",
                        member_ids=["redmine-1"],
                        test_display="test_service",
                        testbeds=["secret-setup-01"],
                        comments=(
                            "2025 Jun 5 16:42:37 host-a ERR service: "
                            "connection failed with 10.1.2.3"
                        ),
                        internal_comments=(
                            "Redmine: "
                            "https://redmine.mellanox.com/issues/5154543"
                        ),
                        redmine_urls=[
                            "https://redmine.mellanox.com/issues/5154543"
                        ],
                    ),
                    SemanticGroup(
                        group_id="redmine-2",
                        member_ids=["redmine-2"],
                        test_display="test_service",
                        testbeds=["secret-setup-01"],
                        comments=(
                            "2025 Jun 5 16:42:38 host-b ERR other: "
                            "connection failed with 10.1.2.4"
                        ),
                        internal_comments=(
                            "Redmine: "
                            "https://redmine.mellanox.com/issues/5154544"
                        ),
                        redmine_urls=[
                            "https://redmine.mellanox.com/issues/5154544"
                        ],
                    ),
                ]

        transport = _CapturingTransport()

        class ErrorAI(_AI):
            def extract_error_logs(self, groups, redactor):
                self.error_batches.append(
                    tuple(group.group_id for group in groups)
                )
                return {
                    group.group_id: group.comments
                    for group in groups
                }

        ai = ErrorAI()
        dependencies, _, artifacts = self._dependencies(
            transport,
            redmine=ErrorRedmine(),
            ai=ai,
        )

        self.assertEqual(
            0,
            run(
                self.request,
                self.settings,
                dependencies,
                stderr=io.StringIO(),
            ),
        )

        groups_by_member = {
            member: group
            for group in artifacts.semantic.internal_error_groups
            for member in group.member_ids
        }
        self.assertEqual(
            (
                "2025 Jun 5 16:42:37 host-a ERR service: "
                "connection failed with 10.1.2.3"
            ),
            groups_by_member["redmine-1"].comments,
        )
        self.assertEqual(
            (
                "2025 Jun 5 16:42:38 host-b ERR other: "
                "connection failed with 10.1.2.4"
            ),
            groups_by_member["redmine-2"].comments,
        )
        self.assertEqual(2, len(artifacts.semantic.internal_error_groups))
        self.assertEqual(
            [("redmine-1", "redmine-2")],
            ai.error_batches,
        )

    def test_internal_error_fallback_is_isolated_per_group(self) -> None:
        class ErrorRedmine(_Redmine):
            def find_log_analyzer_errors(self, version, cutoff_date):
                return [
                    SemanticGroup(
                        group_id="redmine-1",
                        member_ids=["redmine-1"],
                        test_display="test_one",
                        testbeds=["sku-a"],
                        comments="ERR first: failed",
                    ),
                    SemanticGroup(
                        group_id="redmine-2",
                        member_ids=["redmine-2"],
                        test_display="test_two",
                        testbeds=["sku-b"],
                        comments="ERR second: retained",
                    ),
                ]

        class FailingErrorAI(_AI):
            def extract_error_logs(self, groups, redactor):
                raise RuntimeError("AI unavailable")

        def retain_one(value, group, redactor):
            if group.group_id == "redmine-1":
                raise ValueError("bad first group")
            return "ERR second: retained"

        transport = _CapturingTransport()
        dependencies, _, artifacts = self._dependencies(
            transport,
            redmine=ErrorRedmine(),
            ai=FailingErrorAI(),
        )
        with patch(
            "ngts.scripts.regression_mail.pipeline.deduplicate_error_logs",
            side_effect=retain_one,
        ):
            result = run(
                self.request,
                self.settings,
                dependencies,
                stderr=io.StringIO(),
            )

        self.assertNotEqual(6, result)
        by_member = {
            member: group
            for group in artifacts.semantic.internal_error_groups
            for member in group.member_ids
        }
        self.assertEqual("", by_member["redmine-1"].comments)
        self.assertEqual(
            "ERR second: retained",
            by_member["redmine-2"].comments,
        )

    def test_internal_errors_merge_same_problem_only_within_one_ticket(self) -> None:
        ticket_1 = "https://redmine.mellanox.com/issues/111"
        ticket_2 = "https://redmine.mellanox.com/issues/222"
        merged = _merge_internal_error_groups(
            [
                SemanticGroup(
                    group_id="error-1",
                    member_ids=["ticket-1"],
                    test_display="test_service[param-a]",
                    testbeds=["sku-a"],
                    comments="ERR service: first root cause",
                    ticket_refs=[ticket_1],
                    redmine_urls=[ticket_1],
                ),
                SemanticGroup(
                    group_id="error-2",
                    member_ids=["ticket-2"],
                    test_display="test_service[param-b]",
                    testbeds=["sku-b"],
                    comments="ERR service: second root cause",
                    ticket_refs=[ticket_2],
                    redmine_urls=[ticket_2],
                ),
                SemanticGroup(
                    group_id="error-3",
                    member_ids=["ticket-3"],
                    test_display="test_other",
                    testbeds=["sku-c"],
                    comments="ERR service: first root cause",
                    ticket_refs=[ticket_1],
                    redmine_urls=[ticket_1],
                ),
            ]
        )

        self.assertEqual(2, len(merged))
        first_cause = next(
            group
            for group in merged
            if group.comments == "ERR service: first root cause"
        )
        self.assertEqual(
            {
                "test_service[param-a]",
                "test_other",
            },
            set(first_cause.test_display.splitlines()),
        )
        self.assertEqual(
            {"sku-a", "sku-c"},
            set(first_cause.testbeds),
        )
        second_cause = next(
            group
            for group in merged
            if group.comments == "ERR service: second root cause"
        )
        self.assertEqual(
            "test_service[param-b]",
            second_cause.test_display,
        )

    def test_internal_error_table_filters_ngts_test_paths(self) -> None:
        root = Path(self.temp_dir.name)
        ngts_test = (
            root /
            "ngts/tests/nightly/logging" /
            "test_log_analyzer_errors_during_deploy_sonic.py"
        )
        ngts_test.parent.mkdir(parents=True)
        ngts_test.write_text(
            "def test_check_errors_in_log_during_deploy_sonic_image():\n"
            "    pass\n",
            encoding="utf-8",
        )
        public_test = root / "tests/platform_tests/test_public.py"
        public_test.parent.mkdir(parents=True)
        public_test.write_text(
            "def test_public():\n"
            "    pass\n",
            encoding="utf-8",
        )
        mixed = SemanticGroup(
            group_id="mixed",
            member_ids=["mixed"],
            test_display=(
                "ngts/tests/test_internal.py::test_private\n"
                "tests/platform_tests/test_public.py::test_public"
            ),
            testbeds=[],
        )
        ngts_only = SemanticGroup(
            group_id="ngts-only",
            member_ids=["ngts-only"],
            test_display=(
                "/root/mars/workspace/sonic-mgmt/ngts/"
                "tests/test_internal.py::test_private"
            ),
            testbeds=[],
        )
        bare_ngts = SemanticGroup(
            group_id="bare-ngts",
            member_ids=["bare-ngts"],
            test_display=(
                "test_check_errors_in_log_during_deploy_sonic_image"
                "[switch]\n"
                "test_log_analyzer_errors_during_deploy_sonic"
            ),
            testbeds=[],
        )
        bare_public = SemanticGroup(
            group_id="bare-public",
            member_ids=["bare-public"],
            test_display="test_public",
            testbeds=[],
        )

        filtered = _filter_ngts_internal_error_tests(
            [mixed, ngts_only, bare_ngts, bare_public],
            root,
        )

        self.assertEqual([mixed, bare_public], filtered)
        self.assertEqual(
            "tests/platform_tests/test_public.py::test_public",
            mixed.test_display,
        )

    def test_internal_errors_merge_all_evidence_from_same_ticket(self) -> None:
        ticket = "https://redmine.mellanox.com/issues/111"
        merged = _merge_internal_error_groups(
            [
                SemanticGroup(
                    group_id="error-1",
                    member_ids=["ticket-1"],
                    test_display="tests/a.py::TestA::test_one",
                    testbeds=["sku-a"],
                    comments="ERR service-a: connection refused",
                    ticket_refs=[ticket],
                    redmine_urls=[ticket],
                ),
                SemanticGroup(
                    group_id="error-2",
                    member_ids=["ticket-2"],
                    test_display="tests/b.py::TestB::test_two",
                    testbeds=["sku-b"],
                    comments="ERR service-b: process exited",
                    ticket_refs=[ticket],
                    redmine_urls=[ticket],
                ),
            ]
        )

        self.assertEqual(1, len(merged))
        self.assertEqual(
            (
                "tests/a.py::TestA::test_one\n"
                "tests/b.py::TestB::test_two"
            ),
            merged[0].test_display,
        )
        self.assertEqual(
            {
                "ERR service-a: connection refused",
                "ERR service-b: process exited",
            },
            set(merged[0].comments.splitlines()),
        )

    def test_internal_errors_without_tickets_merge_only_matching_causes(
        self,
    ) -> None:
        merged = _merge_internal_error_groups(
            [
                SemanticGroup(
                    group_id="error-1",
                    member_ids=["one"],
                    test_display="tests/a.py::test_one",
                    testbeds=["sku-a"],
                    comments="ERR service: connection failed with 10.1.2.3",
                ),
                SemanticGroup(
                    group_id="error-2",
                    member_ids=["two"],
                    test_display="tests/b.py::test_two",
                    testbeds=["sku-b"],
                    comments="ERR service: connection failed with 10.1.2.4",
                ),
                SemanticGroup(
                    group_id="error-3",
                    member_ids=["three"],
                    test_display="tests/c.py::test_three",
                    testbeds=["sku-c"],
                    comments="ERR service: permission denied",
                ),
            ]
        )

        self.assertEqual(2, len(merged))
        shared = next(
            group for group in merged if len(group.member_ids) == 2
        )
        self.assertEqual(
            "tests/a.py::test_one\ntests/b.py::test_two",
            shared.test_display,
        )

    def test_internal_error_multi_signature_group_does_not_bridge_causes(
        self,
    ) -> None:
        merged = _merge_internal_error_groups(
            [
                SemanticGroup(
                    group_id="error-1",
                    member_ids=["both"],
                    test_display="tests/a.py::test_both",
                    testbeds=["sku-a"],
                    comments=(
                        "ERR service: connection refused\n"
                        "ERR service: permission denied"
                    ),
                ),
                SemanticGroup(
                    group_id="error-2",
                    member_ids=["connection"],
                    test_display="tests/b.py::test_connection",
                    testbeds=["sku-b"],
                    comments="ERR service: connection refused",
                ),
                SemanticGroup(
                    group_id="error-3",
                    member_ids=["permission"],
                    test_display="tests/c.py::test_permission",
                    testbeds=["sku-c"],
                    comments="ERR service: permission denied",
                ),
            ]
        )

        self.assertEqual(3, len(merged))

    def test_internal_errors_do_not_merge_overlapping_patterns_across_tickets(
        self,
    ) -> None:
        ticket_1 = "https://redmine.mellanox.com/issues/111"
        ticket_2 = "https://redmine.mellanox.com/issues/222"
        merged = _merge_internal_error_groups(
            [
                SemanticGroup(
                    group_id="error-1",
                    member_ids=["ticket-1"],
                    test_display="test_one",
                    testbeds=["sku-a"],
                    comments=(
                        "ERR dockerd: connection failed with 10.1.2.3:49\n"
                        "ERR database: unable to connect to redis"
                    ),
                    ticket_refs=[ticket_1],
                    redmine_urls=[ticket_1],
                ),
                SemanticGroup(
                    group_id="error-2",
                    member_ids=["ticket-2"],
                    test_display="test_two",
                    testbeds=["sku-b"],
                    comments=(
                        "ERR dockerd: connection failed with 10.1.2.4:49"
                    ),
                    ticket_refs=[ticket_2],
                    redmine_urls=[ticket_2],
                ),
            ]
        )

        self.assertEqual(2, len(merged))
        self.assertEqual(
            {
                "ERR dockerd: connection failed with 10.1.2.3:49\n"
                "ERR database: unable to connect to redis",
                "ERR dockerd: connection failed with 10.1.2.4:49",
            },
            {group.comments for group in merged},
        )
        self.assertEqual(
            [{"ticket-1"}, {"ticket-2"}],
            sorted(
                (set(group.member_ids) for group in merged),
                key=lambda members: sorted(members),
            ),
        )

    def test_skip_sections_filter_redmine_and_github_cases(self) -> None:
        rows = []
        for record_id, test_name, message in (
            (
                "redmine-skip",
                "tests/a.py::test_redmine_skip",
                "https://redmine.mellanox.com/issues/5154543",
            ),
            (
                "github-skip",
                "tests/b.py::test_github_skip",
                "https://github.com/sonic-net/sonic-buildimage/issues/27006",
            ),
            (
                "plain-skip",
                "tests/c.py::test_plain_skip",
                "Skipped by a local condition",
            ),
        ):
            rows.append(
                ResultRow(
                    record_id=record_id,
                    excel_row=len(rows) + 2,
                    session_id=record_id,
                    mars_key_id=record_id,
                    testbed="tb",
                    test_name=test_name,
                    sanitized_testname=test_name,
                    result="skipped",
                    message=message,
                    topology="t0",
                    hwsku="sku",
                )
            )
        snapshot = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=rows,
            result_counts={"pass": 0, "fail": 0, "skipped": 3},
            hardware_pairs=[("sku", "t0")],
        )
        report = ReportModel(request=self.request)
        report.workbook = snapshot
        report.semantic = build_deterministic_report(snapshot)
        report.semantic.internal_error_groups = [
            SemanticGroup(
                group_id="redmine-1",
                member_ids=["redmine-1"],
                test_display="test_buffer",
                testbeds=["SN5600"],
                comments="Unexpected buffer error",
                internal_comments="https://redmine.mellanox.com/issues/1",
            )
        ]

        rendered = ReportRenderer().render(report)

        skipped_html = rendered.html.split(
            "<strong>The following tests were skipped over bugs found "
            "internally:</strong>",
            1,
        )[1].split(
            "<strong>Github issues that affects tests coverage:</strong>",
            1,
        )[0]
        github_html = rendered.html.split(
            "<strong>Github issues that affects tests coverage:</strong>",
            1,
        )[1].split(
            "<strong>Err msgs detected internally:</strong>",
            1,
        )[0]
        self.assertIn("test_redmine_skip", skipped_html)
        self.assertNotIn("test_github_skip", skipped_html)
        self.assertNotIn("test_plain_skip", skipped_html)
        self.assertIn("test_github_skip", github_html)
        self.assertNotIn("test_redmine_skip", github_html)
        self.assertNotIn("test_plain_skip", github_html)
        internal_html = rendered.html.split(
            "<strong>Err msgs detected internally:</strong>",
            1,
        )[1].split(
            "<strong>Additional PR(s) on top of the public hash: </strong>",
            1,
        )[0]
        self.assertIn("Err msgs", internal_html)
        self.assertIn("Testbeds", internal_html)
        self.assertIn("Test case", internal_html)
        self.assertIn("Unexpected buffer error", internal_html)
        self.assertIn("test_buffer", internal_html)
        self.assertNotIn("<h2>Summary</h2>", rendered.html)
        self.assertNotIn("<h2>Build Summary</h2>", rendered.html)
        self.assertNotIn("\nSummary:\n", rendered.plain)

    def test_same_failure_cause_merges_files_as_separate_lines(self) -> None:
        report = ReportModel(request=self.request)
        report.semantic = build_deterministic_report(
            WorkbookSnapshot(
                source_path=self.request.excel_path,
                sheet_name="community_tests",
                header_row=1,
                selected_rows=[
                    ResultRow(
                        record_id="failure-1",
                        excel_row=2,
                        session_id="session-1",
                        mars_key_id="key-1",
                        testbed="tb",
                        test_name="tests/a.py::test_a",
                        sanitized_testname="tests/a.py::test_a",
                        result="fail",
                        message="Shared cause",
                        topology="t0",
                        hwsku="sku",
                    ),
                    ResultRow(
                        record_id="failure-2",
                        excel_row=3,
                        session_id="session-2",
                        mars_key_id="key-2",
                        testbed="tb",
                        test_name="tests/b.py::test_b",
                        sanitized_testname="tests/b.py::test_b",
                        result="fail",
                        message="Shared cause",
                        topology="t0",
                        hwsku="sku",
                    ),
                ],
                result_counts={"pass": 0, "fail": 2, "skipped": 0},
                hardware_pairs=[("sku", "t0")],
            )
        )

        rendered = ReportRenderer().render(report)

        self.assertEqual(1, len(report.semantic.failure_groups))
        self.assertEqual(
            "tests/a.py::test_a\ntests/b.py::test_b",
            report.semantic.failure_groups[0].test_display,
        )
        self.assertIn("tests/a.py::test_a", rendered.html)
        self.assertIn("tests/b.py::test_b", rendered.html)
        self.assertIn(
            "tests/a.py::test_a<br>tests/b.py::test_b",
            rendered.html,
        )

    def test_ai_does_not_merge_analyzed_and_missing_dashboard_groups(
        self,
    ) -> None:
        rows = [
            ResultRow(
                record_id="failure-1",
                excel_row=2,
                session_id="session-1",
                mars_key_id="key-1",
                testbed="tb-a",
                test_name="tests/a.py::test_a",
                sanitized_testname="tests/a.py::test_a",
                result="fail",
                message="Failure A",
                topology="t0",
                hwsku="sku",
            ),
            ResultRow(
                record_id="failure-2",
                excel_row=3,
                session_id="session-2",
                mars_key_id="key-2",
                testbed="tb-b",
                test_name="tests/b.py::test_b",
                sanitized_testname="tests/b.py::test_b",
                result="fail",
                message="Failure B",
                topology="t1",
                hwsku="sku",
            ),
        ]
        workbook = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=rows,
            result_counts={"pass": 0, "fail": 2, "skipped": 0},
            hardware_pairs=[("sku", "t0"), ("sku", "t1")],
            row_comments={"failure-1": "Engineer root cause"},
            dashboard_statuses={
                "failure-1": "analyzed",
                "failure-2": "not_found",
            },
        )
        report = ReportModel(
            request=self.request,
            workbook=workbook,
            semantic=build_deterministic_report(workbook),
        )

        class CauseAI:
            @staticmethod
            def classify_failure_causes(groups, file_keys, redactor):
                return {
                    group.group_id: "same-ai-cause"
                    for group in groups
                }

        class Dependencies:
            ai = CauseAI()

        _classify_and_merge_failure_groups(
            report,
            Dependencies(),
            self.settings,
        )

        self.assertEqual(2, len(report.semantic.failure_groups))

    def test_ai_merges_only_semantically_matching_causes_in_one_file(self) -> None:
        rows = [
            ResultRow(
                record_id="failure-{}".format(index),
                excel_row=index + 1,
                session_id="session-{}".format(index),
                mars_key_id="key-{}".format(index),
                testbed="tb",
                test_name=test_name,
                sanitized_testname=test_name,
                result="fail",
                message=message,
                topology="t0",
                hwsku="sku",
            )
            for index, test_name, message in (
                (
                    1,
                    "tests/a.py::TestFeature::test_x",
                    "ERR service: connection failed with 10.1.2.3",
                ),
                (
                    2,
                    "tests/a.py::TestFeature::test_y",
                    "ERROR service: startup failed because connection refused",
                ),
                (
                    3,
                    "tests/a.py::TestFeature::test_z",
                    "ERR service: permission denied",
                ),
            )
        ]
        workbook = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=rows,
            result_counts={"pass": 0, "fail": 3, "skipped": 0},
            hardware_pairs=[("sku", "t0")],
            dashboard_statuses={
                row.record_id: "no_analysis"
                for row in rows
            },
        )
        report = ReportModel(request=self.request)
        report.workbook = workbook
        report.semantic = build_deterministic_report(workbook)

        class CauseAI:
            @staticmethod
            def classify_failure_causes(groups, file_keys, redactor):
                return {
                    group.group_id: (
                        "permission denied"
                        if "permission denied" in group.comments
                        else "service connection refused"
                    )
                    for group in groups
                }

        class Dependencies:
            ai = CauseAI()

        _classify_and_merge_failure_groups(
            report,
            Dependencies(),
            self.settings,
        )

        self.assertEqual(2, len(report.semantic.failure_groups))
        self.assertEqual(
            {
                "tests/a.py::TestFeature",
                "tests/a.py::TestFeature::test_z",
            },
            {
                group.test_display
                for group in report.semantic.failure_groups
            },
        )

    def test_ai_root_cause_merges_different_files_into_one_row(self) -> None:
        rows = [
            ResultRow(
                record_id="failure-a",
                excel_row=2,
                session_id="session-a",
                mars_key_id="key-a",
                testbed="tb-a",
                test_name="tests/a.py::TestA::test_one",
                sanitized_testname="tests/a.py::TestA::test_one",
                result="fail",
                message="Connection attempt timed out",
                topology="t0",
                hwsku="sku",
            ),
            ResultRow(
                record_id="failure-b",
                excel_row=3,
                session_id="session-b",
                mars_key_id="key-b",
                testbed="tb-b",
                test_name="tests/b.py::TestB::test_two",
                sanitized_testname="tests/b.py::TestB::test_two",
                result="fail",
                message="Peer did not respond before timeout",
                topology="t1",
                hwsku="sku",
            ),
        ]
        workbook = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=rows,
            result_counts={"pass": 0, "fail": 2, "skipped": 0},
            hardware_pairs=[],
        )
        report = ReportModel(request=self.request, workbook=workbook)
        report.semantic = build_deterministic_report(workbook)

        class CauseAI:
            @staticmethod
            def classify_failure_causes(groups, file_keys, redactor):
                self.assertEqual(
                    {"tests/a.py", "tests/b.py"},
                    set(file_keys.values()),
                )
                return {
                    group.group_id: "peer response timeout"
                    for group in groups
                }

        class Dependencies:
            ai = CauseAI()

        _classify_and_merge_failure_groups(
            report,
            Dependencies(),
            self.settings,
        )

        self.assertEqual(1, len(report.semantic.failure_groups))
        self.assertEqual(
            (
                "tests/a.py::TestA::test_one\n"
                "tests/b.py::TestB::test_two"
            ),
            report.semantic.failure_groups[0].test_display,
        )

    def test_ai_does_not_cross_file_merge_missing_dashboard_failures(
        self,
    ) -> None:
        rows = [
            ResultRow(
                record_id="failure-{}".format(index),
                excel_row=index + 1,
                session_id="session-{}".format(index),
                mars_key_id="key-{}".format(index),
                testbed="tb",
                test_name=test_name,
                sanitized_testname=test_name,
                result="fail",
                message=message,
                topology="t0",
                hwsku="sku",
            )
            for index, test_name, message in (
                (
                    1,
                    "tests/a.py::TestA::test_one",
                    "ARP neighbor remained unresolved",
                ),
                (
                    2,
                    "tests/b.py::TestB::test_two",
                    "BGP route was missing",
                ),
            )
        ]
        workbook = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=rows,
            result_counts={"pass": 0, "fail": 2, "skipped": 0},
            hardware_pairs=[],
            dashboard_statuses={
                row.record_id: "not_found"
                for row in rows
            },
        )
        report = ReportModel(request=self.request, workbook=workbook)
        report.semantic = build_deterministic_report(workbook)

        class CauseAI:
            @staticmethod
            def classify_failure_causes(groups, file_keys, redactor):
                return {
                    group.group_id: "same-ai-cause"
                    for group in groups
                }

        class Dependencies:
            ai = CauseAI()

        _classify_and_merge_failure_groups(
            report,
            Dependencies(),
            self.settings,
        )

        self.assertEqual(2, len(report.semantic.failure_groups))

    def test_ai_does_not_merge_distinct_redmine_tickets(self) -> None:
        rows = [
            ResultRow(
                record_id="failure-{}".format(index),
                excel_row=index + 1,
                session_id="session-{}".format(index),
                mars_key_id="key-{}".format(index),
                testbed="tb",
                test_name="tests/a.py::test_{}".format(index),
                sanitized_testname="tests/a.py::test_{}".format(index),
                result="fail",
                message="Symptom {} RM ticket {}".format(index, ticket),
                topology="t0",
                hwsku="sku",
            )
            for index, ticket in ((1, 1001), (2, 1002))
        ]
        workbook = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=rows,
            result_counts={"pass": 0, "fail": 2, "skipped": 0},
            hardware_pairs=[],
        )
        report = ReportModel(request=self.request, workbook=workbook)
        report.semantic = build_deterministic_report(workbook)

        class CauseAI:
            @staticmethod
            def classify_failure_causes(groups, file_keys, redactor):
                return {
                    group.group_id: "same-ai-cause"
                    for group in groups
                }

        class Dependencies:
            ai = CauseAI()

        _classify_and_merge_failure_groups(
            report,
            Dependencies(),
            self.settings,
        )

        self.assertEqual(2, len(report.semantic.failure_groups))

    def test_skip_mail_uses_wip_comment_and_ticket_for_internal_review(self) -> None:
        ticket = "https://redmine.mellanox.com/issues/5154543"
        row = ResultRow(
            record_id="skip-1",
            excel_row=2,
            session_id="session",
            mars_key_id="key",
            testbed="tb",
            test_name="tests/a.py::TestFeature::test_skip",
            sanitized_testname="tests/a.py::TestFeature::test_skip",
            result="skipped",
            message=ticket,
            topology="t0",
            hwsku="sku",
        )
        snapshot = WorkbookSnapshot(
            source_path=self.request.excel_path,
            sheet_name="community_tests",
            header_row=1,
            selected_rows=[row],
            result_counts={"pass": 0, "fail": 0, "skipped": 1},
            hardware_pairs=[],
        )
        transport = _CapturingTransport()

        dependencies, _, artifacts = self._dependencies(
            transport,
            redmine=_Redmine(),
            workbook=snapshot,
        )

        self.assertEqual(
            0,
            run(
                self.request,
                self.settings,
                dependencies,
                stderr=io.StringIO(),
            ),
        )

        group = artifacts.semantic.skip_groups[0]
        self.assertEqual("Internal issue, WIP", group.comments)
        self.assertEqual(
            "Production bug: [SONIC - Design] Bug SW #5154543: Internal issue"
            " | Assignee: Owner | Status: In Progress",
            group.internal_comments,
        )
        html = transport.messages[0][0].get_body(
            preferencelist=("html",)
        ).get_content()
        self.assertIn(
            "The following tests were skipped over bugs found internally:",
            html,
        )

    def test_real_workbook_pipeline_has_no_setup_leak(self) -> None:
        source = Path(self.temp_dir.name) / "real-results.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "community_tests"
        sheet.append(list(REQUIRED_HEADERS) + ["platform"])
        sheet.append(
            [
                "session",
                "key",
                "private-setup-01",
                "tests/a.py::TestFeature::test_a",
                "fail",
                "failure on private-setup-01",
                "t0",
                "sku",
                VERSION,
                "tests/a.py::TestFeature::test_a",
                "SN5600",
            ]
        )
        workbook.save(source)
        workbook.close()
        request = RunRequest(
            excel_path=source,
            version=VERSION,
            to=("owner@nvidia.com",),
        )
        transport = _CapturingTransport()
        artifacts = ArtifactWriter()
        dependencies = PipelineDependencies(
            workbook=WorkbookLoader(),
            dashboard=_Dashboard(),
            git=_Git(),
            github=_GitHub(),
            redmine=_Redmine(),
            ai=_AI(),
            artifacts=artifacts,
            renderer=ReportRenderer(),
            transport=transport,
        )

        result = run(
            request,
            self.settings,
            dependencies,
            stderr=io.StringIO(),
        )

        self.assertEqual(0, result)
        message = transport.messages[0][0]
        self.assertNotIn("private-setup-01", message.as_string())
        self.assertEqual([], list(message.iter_attachments()))
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("not found in dashboard", html)
        self.assertIn("SN5600-t0", html)

    def test_redmine_failure_sends_deterministic_degraded_message(self) -> None:
        transport = _CapturingTransport()
        dependencies, _, _ = self._dependencies(
            transport,
            redmine=_Redmine(RuntimeError("Redmine unavailable")),
        )
        stderr = io.StringIO()
        result = run(self.request, self.settings, dependencies, stderr=stderr)

        self.assertEqual(4, result)
        self.assertEqual(1, len(transport.messages))
        self.assertIn("degraded mode", stderr.getvalue())
        self.assertNotIn("[Degraded]", transport.messages[0][0]["Subject"])
        self.assertIn("stages=Redmine LogAnalyzer review", stderr.getvalue())
        html = transport.messages[0][0].get_body(
            preferencelist=("html",)
        ).get_content()
        self.assertIn("<strong>Generation Errors:</strong>", html)
        self.assertNotIn("<h2", html)

    def test_redmine_only_setup_name_is_added_to_redaction_deny_list(self) -> None:
        class RedmineWithPrivateSetup:
            @staticmethod
            def find_log_analyzer_errors(version, cutoff_date):
                return [
                    SemanticGroup(
                        group_id="internal-1",
                        member_ids=["ticket-1"],
                        test_display="tests/a.py::test_a",
                        testbeds=["redmine-private-setup"],
                        comments=(
                            "ERR service: failure on redmine-private-setup"
                        ),
                        internal_comments=(
                            "Observed on redmine-private-setup"
                        ),
                    )
                ]

        transport = _CapturingTransport()
        dependencies, _, artifacts = self._dependencies(
            transport,
            redmine=RedmineWithPrivateSetup(),
        )

        self.assertEqual(
            0,
            run(
                self.request,
                self.settings,
                dependencies,
                stderr=io.StringIO(),
            ),
        )

        html = transport.messages[0][0].get_body(
            preferencelist=("html",)
        ).get_content()
        self.assertIn("redmine-private-setup", html)
        self.assertNotIn("Unknown platform", html)
        self.assertEqual(
            ["redmine-private-setup"],
            artifacts.semantic.internal_error_groups[0].testbeds,
        )

    def test_ai_failure_keeps_draft_analysis_for_internal_review(self) -> None:
        transport = _CapturingTransport()
        snapshot = _snapshot(self.request.excel_path)
        snapshot.selected_rows = [
            replace(
                snapshot.selected_rows[0],
                message="failure RM ticket 5154543",
            )
        ]
        snapshot.row_comments["failure-1"] = (
            "Engineer analysis "
            "https://redmine.mellanox.com/issues/5154543"
        )
        dependencies, _, artifacts = self._dependencies(
            transport,
            workbook=snapshot,
            dashboard=_Dashboard(
                [
                    DashboardAnalysis(
                        session_id=snapshot.selected_rows[0].session_id,
                        key_id=snapshot.selected_rows[0].mars_key_id,
                        test_name=snapshot.selected_rows[0].test_name,
                        analysis="Engineer analysis",
                        redmine_url="https://redmine.mellanox.com/issues/5154543",
                    )
                ]
            ),
            ai=_AI(
                RuntimeError(
                    "Incorrect API key provided: inference-token"
                )
            ),
        )

        result = run(
            self.request,
            self.settings,
            dependencies,
            stderr=io.StringIO(),
        )

        self.assertEqual(4, result)
        group = artifacts.semantic.failure_groups[0]
        self.assertEqual("Internal issue, WIP", group.comments)
        self.assertIn(
            "Production bug: Internal issue | ",
            group.internal_comments,
        )
        self.assertIn(
            "https://redmine.mellanox.com/issues/5154543",
            group.internal_comments,
        )
        message = transport.messages[0][0]
        self.assertNotIn("inference-token", message.as_string())
        self.assertIn("5154543", message.as_string())

    def test_redmine_metadata_is_used_in_every_analysis_table(self) -> None:
        ticket = "https://redmine.mellanox.com/issues/5048947"
        summary = (
            "[SONIC - Design] Bug SW #5048947: [Community] CACL issue"
            " | Assignee: Owner | Status: In Progress"
        )
        groups = [
            SemanticGroup(
                group_id="failure-1",
                member_ids=["failure-1"],
                test_display="tests/a.py::test_failure",
                testbeds=["sku-t0"],
                internal_comments="Analysis\n" + ticket,
                redmine_urls=[ticket],
            ),
            SemanticGroup(
                group_id="skip-1",
                member_ids=["skip-1"],
                test_display="tests/a.py::test_skip",
                testbeds=["sku-t0"],
                internal_comments=ticket,
                redmine_urls=[ticket],
            ),
            SemanticGroup(
                group_id="error-1",
                member_ids=["error-1"],
                test_display="tests/a.py::test_error",
                testbeds=["sku-t0"],
                internal_comments="Redmine: " + ticket,
                redmine_urls=[ticket],
            ),
        ]
        semantic = SemanticReport(
            failure_groups=[groups[0]],
            skip_groups=[groups[1]],
            internal_error_groups=[groups[2]],
        )

        _prepare_redmine_comments(
            semantic,
            {ticket: summary},
        )

        for group in groups:
            self.assertIn(summary, group.internal_comments)
            self.assertNotIn(ticket, group.internal_comments)
        self.assertIn(
            (
                'href="https://redmine.mellanox.com/issues/5048947" '
                'style="color:#0563c1;text-decoration:underline">'
                "#5048947</a>"
            ),
            _failure_table([groups[0]]),
        )

    def test_redmine_replacement_deduplicates_identical_summaries(
        self,
    ) -> None:
        ticket = "https://redmine.mellanox.com/issues/5072475"
        summary = (
            "[SONIC - Design] Bug SW #5072475: Everflow issue"
            " | Assignee: Owner | Status: Assigned"
        )
        group = SemanticGroup(
            group_id="failure-1",
            member_ids=["failure-1"],
            test_display="tests/a.py::test_failure",
            testbeds=["sku-t0"],
            internal_comments=COMMENT_SEPARATOR.join(
                (ticket, "Redmine: " + ticket)
            ),
            redmine_urls=[ticket],
        )

        _prepare_redmine_comments(
            SemanticReport(failure_groups=[group]),
            {ticket: summary},
        )

        self.assertEqual(summary, group.internal_comments)

    def test_redmine_replacement_removes_prefixed_stale_summary(
        self,
    ) -> None:
        ticket = "https://redmine.mellanox.com/issues/5220971"
        stale = (
            "[SONIC - Design] Bug SW #5220971: old SFP subject"
            " | Assignee: Old Owner | Status: Assigned"
        )
        current = (
            "[SONIC - Design] Bug SW #5220971: current SFP subject"
            " | Assignee: Current Owner | Status: In Progress"
        )
        group = SemanticGroup(
            group_id="failure-sfp",
            member_ids=["failure-sfp"],
            test_display="platform_tests/api/test_sfp.py::test_reset",
            testbeds=["SN5600"],
            internal_comments=COMMENT_SEPARATOR.join(
                (
                    "Affected by reset test due to - " + stale,
                    ticket,
                )
            ),
            redmine_urls=[ticket],
        )

        _prepare_redmine_comments(
            SemanticReport(failure_groups=[group]),
            {ticket: current},
        )

        self.assertNotIn("old SFP subject", group.internal_comments)
        self.assertEqual(
            1,
            group.internal_comments.count("#5220971"),
        )
        self.assertIn("current SFP subject", group.internal_comments)
        self.assertIn(
            "Affected by reset test due to",
            group.internal_comments,
        )

    def test_redmine_replacement_prefers_current_conflicting_summary(
        self,
    ) -> None:
        ticket = "https://redmine.mellanox.com/issues/5225544"
        stale = (
            "[SONIC - Design] Bug SW #5225544: failed to install FW"
            " | Assignee: Old Owner | Status: Assigned"
        )
        current = (
            "[SONIC - Design] Bug SW #5225544: failed to change BIOS"
            " | Assignee: Current Owner | Status: Assigned"
        )
        group = SemanticGroup(
            group_id="failure-bios",
            member_ids=["failure-bios"],
            test_display="platform_tests/fwutil/test_fwutil.py",
            testbeds=["SN4280"],
            internal_comments=COMMENT_SEPARATOR.join((stale, ticket)),
            redmine_urls=[ticket],
        )

        _prepare_redmine_comments(
            SemanticReport(failure_groups=[group]),
            {ticket: current},
        )

        self.assertEqual(current, group.internal_comments)

    def test_redmine_metadata_replacement_respects_issue_id_boundaries(
        self,
    ) -> None:
        ticket_123 = "https://redmine.mellanox.com/issues/123"
        ticket_1234 = ticket_123 + "4"
        summary_123 = "[Project] Bug #123: first"
        summary_1234 = "[Project] Bug #1234: second"
        group = SemanticGroup(
            group_id="failure-1",
            member_ids=["failure-1"],
            test_display="tests/a.py::test_failure",
            testbeds=["sku-t0"],
            internal_comments="{}\n{}".format(ticket_123, ticket_1234),
            redmine_urls=[ticket_123, ticket_1234],
        )

        _prepare_redmine_comments(
            SemanticReport(failure_groups=[group]),
            {
                ticket_123: summary_123,
                ticket_1234: summary_1234,
            },
        )

        self.assertEqual(
            "{}\n{}".format(summary_123, summary_1234),
            group.internal_comments,
        )

    def test_redmine_metadata_failure_preserves_original_reference(self) -> None:
        ticket = "https://redmine.mellanox.com/issues/5048947"
        group = SemanticGroup(
            group_id="failure-1",
            member_ids=["failure-1"],
            test_display="tests/a.py::test_failure",
            testbeds=["sku-t0"],
            internal_comments=ticket,
            redmine_urls=[ticket],
        )
        semantic = SemanticReport(failure_groups=[group])

        _prepare_redmine_comments(semantic)

        self.assertEqual(ticket, group.internal_comments)

    def test_git_failure_does_not_skip_redmine_review(self) -> None:
        transport = _CapturingTransport()
        redmine = _Redmine()
        dependencies, _, _ = self._dependencies(
            transport,
            redmine=redmine,
            git=_Git(RuntimeError("Git unavailable")),
        )

        result = run(self.request, self.settings, dependencies, stderr=io.StringIO())

        self.assertEqual(3, result)
        self.assertEqual([""], redmine.cutoffs)
        self.assertEqual(1, len(transport.messages))

    def test_invalid_excel_still_sends_minimal_report(self) -> None:
        transport = _CapturingTransport()
        dependencies, _, _ = self._dependencies(
            transport,
            workbook=ValueError("no matching rows"),
        )
        stderr = io.StringIO()
        result = run(self.request, self.settings, dependencies, stderr=stderr)
        self.assertEqual(3, result)
        self.assertEqual(1, len(transport.messages))

    def test_smtp_failure_overrides_generation_status(self) -> None:
        dependencies, _, _ = self._dependencies(_FailingTransport())
        stderr = io.StringIO()
        result = run(self.request, self.settings, dependencies, stderr=stderr)
        self.assertEqual(6, result)
        self.assertIn("delivery failed", stderr.getvalue())

    def test_renderer_failure_uses_hardcoded_minimal_message(self) -> None:
        transport = _CapturingTransport()
        dependencies, _, _ = self._dependencies(transport)
        dependencies.renderer = _BrokenRenderer()
        stderr = io.StringIO()
        result = run(self.request, self.settings, dependencies, stderr=stderr)
        self.assertEqual(3, result)
        message = transport.messages[0][0]
        self.assertIn("Generation Errors", message.get_body(preferencelist=("plain",)).get_content())

    def test_dashboard_keeps_exact_analysis_and_redmine_url_internal(self) -> None:
        workbook = _snapshot(self.request.excel_path)
        dashboard = DashboardSnapshot(
            analyses=[
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name="tests/a.py::test_a",
                    analysis="Known product issue",
                    redmine_url="https://redmine.mellanox.com/issues/123",
                )
            ]
        )
        apply_dashboard_analysis(workbook, dashboard)
        self.assertEqual(
            "Known product issue\nhttps://redmine.mellanox.com/issues/123",
            workbook.row_comments["failure-1"],
        )

    def test_dashboard_ticket_without_analysis_still_protects_group(
        self,
    ) -> None:
        workbook = _snapshot(self.request.excel_path)
        ticket = "https://redmine.mellanox.com/issues/5154543"
        dashboard = DashboardSnapshot(
            analyses=[
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name="tests/a.py::test_a",
                    analysis="",
                    redmine_url=ticket,
                )
            ]
        )

        apply_dashboard_analysis(workbook, dashboard)
        semantic = build_deterministic_report(workbook)

        self.assertEqual(ticket, workbook.row_comments["failure-1"])
        self.assertEqual(
            "analyzed",
            workbook.dashboard_statuses["failure-1"],
        )
        self.assertEqual(
            [ticket],
            semantic.failure_groups[0].ticket_refs,
        )
        self.assertEqual(
            "",
            semantic.failure_groups[0].internal_review_status,
        )
        _apply_failure_comment_policy(
            semantic,
            {
                ticket: {
                    "is_test_issue": False,
                    "is_closed": False,
                },
            },
            {},
        )
        self.assertEqual(
            "Internal issue, WIP",
            semantic.failure_groups[0].comments,
        )

    def test_dashboard_does_not_match_other_parameter_variant(self) -> None:
        workbook = _snapshot(self.request.excel_path)
        workbook.selected_rows = [
            replace(
                workbook.selected_rows[0],
                test_name="tests/a.py::test_a[r-private-dut]",
                sanitized_testname="tests/a.py::test_a[r-private-dut]",
            )
        ]
        workbook.row_comments = {}
        dashboard = DashboardSnapshot(
            analyses=[
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name="tests/a.py::test_a[slm-1]",
                    analysis="Canonical engineer analysis",
                )
            ]
        )

        apply_dashboard_analysis(workbook, dashboard)

        self.assertEqual("", workbook.row_comments["failure-1"])
        self.assertEqual(
            "not_found",
            workbook.dashboard_statuses["failure-1"],
        )

    def test_dashboard_does_not_match_same_session_and_test_across_versions(
        self,
    ) -> None:
        workbook = _snapshot(self.request.excel_path)
        workbook.selected_rows[0] = replace(
            workbook.selected_rows[0],
            values={"os_version": "202605_RC.100-build_Internal"},
        )
        workbook.row_comments = {}
        dashboard = DashboardSnapshot(
            analyses=[
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name="tests/a.py::test_a",
                    analysis="Analysis from another version",
                    switch_version="202605_RC.101-build_Internal",
                )
            ]
        )

        apply_dashboard_analysis(workbook, dashboard)

        self.assertEqual("", workbook.row_comments["failure-1"])
        self.assertEqual("not_found", workbook.dashboard_statuses["failure-1"])

    def test_dashboard_matches_desensitized_setup_alias_by_platform_and_id(
        self,
    ) -> None:
        workbook = _snapshot(self.request.excel_path)
        first = replace(
            workbook.selected_rows[0],
            record_id="failure-dhcp",
            testbed="sn4280-1245-t1-28-lag",
            platform="x86_64-nvidia_sn4280-r0",
            test_name=(
                "tests/a.py::test_restart"
                "[sn4280-1245-None-dhcp_server]"
            ),
            sanitized_testname=(
                "tests/a.py::test_restart"
                "[switch-hostname-None-dhcp_server]"
            ),
        )
        second = replace(
            first,
            record_id="failure-eventd",
            excel_row=3,
            test_name=(
                "tests/a.py::test_restart"
                "[sn4280-1245-None-eventd]"
            ),
            sanitized_testname=(
                "tests/a.py::test_restart"
                "[switch-hostname-None-eventd]"
            ),
        )
        workbook.selected_rows = [first, second]
        workbook.setup_platforms = {
            "sn4280-1245-t1-28-lag": "x86_64-nvidia_sn4280-r0",
        }
        dashboard = DashboardSnapshot(
            setup_platforms={
                "bobcat-1245_setup": "x86_64-nvidia_sn4280-r0",
            },
            analyses=[
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name=(
                        "tests/a.py::test_restart"
                        "[bobcat-1245-None-eventd]"
                    ),
                    analysis="Eventd engineer analysis",
                ),
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name=(
                        "tests/a.py::test_restart"
                        "[bobcat-1245-None-dhcp_server]"
                    ),
                    analysis="DHCP engineer analysis",
                ),
            ],
        )

        apply_dashboard_analysis(workbook, dashboard)

        self.assertEqual(
            "DHCP engineer analysis",
            workbook.row_comments["failure-dhcp"],
        )
        self.assertEqual(
            "Eventd engineer analysis",
            workbook.row_comments["failure-eventd"],
        )
        self.assertEqual(
            {
                "failure-dhcp": "analyzed",
                "failure-eventd": "analyzed",
            },
            workbook.dashboard_statuses,
        )

    def test_dashboard_matches_prefixed_real_setup_alias(
        self,
    ) -> None:
        workbook = _snapshot(self.request.excel_path)
        row = replace(
            workbook.selected_rows[0],
            record_id="failure-sfp",
            session_id="11377379",
            mars_key_id="key-1",
            testbed="r-sn5640-12-ptf-any",
            platform="x86_64-nvidia_sn5640-r0",
            test_name=(
                "platform_tests/api/test_sfp.py::TestSfpApi::"
                "test_get_error_description[r-sn5640-12]"
            ),
            sanitized_testname=(
                "platform_tests/api/test_sfp.py::TestSfpApi::"
                "test_get_error_description[switch-hostname]"
            ),
        )
        workbook.selected_rows = [row]
        workbook.setup_platforms = {
            "r-sn5640-12-ptf-any": "x86_64-nvidia_sn5640-r0",
        }
        workbook.row_comments = {}
        dashboard = DashboardSnapshot(
            setup_platforms={
                "sonic_bison_r-bison-12": "x86_64-nvidia_sn5640-r0",
            },
            analyses=[
                DashboardAnalysis(
                    session_id="11377379",
                    key_id="key-1",
                    test_name=(
                        "platform_tests/api/test_sfp.py::TestSfpApi::"
                        "test_get_error_description[r-bison-12]"
                    ),
                    analysis="Known SFP reset issue",
                ),
            ],
        )

        apply_dashboard_analysis(workbook, dashboard)

        self.assertEqual(
            "Known SFP reset issue",
            workbook.row_comments["failure-sfp"],
        )
        self.assertEqual(
            "analyzed",
            workbook.dashboard_statuses["failure-sfp"],
        )

    def test_dashboard_matches_sanitized_composite_setup_parameter(
        self,
    ) -> None:
        workbook = _snapshot(self.request.excel_path)
        dual = replace(
            workbook.selected_rows[0],
            record_id="failure-dual",
            session_id="11377279",
            mars_key_id="key-dual",
            testbed="sonic-dual-tor-leopard-dualtor-aa-64-breakout",
            platform="x86_64-mlnx_msn4700-r0",
            test_name=(
                "platform_tests/test_advanced_reboot.py::"
                "test_warm_reboot[dual-r-msn4700-70]"
            ),
            sanitized_testname=(
                "platform_tests/test_advanced_reboot.py::"
                "test_warm_reboot[dual-switch-hostname]"
            ),
        )
        sad = replace(
            dual,
            record_id="failure-sad",
            mars_key_id="key-sad",
            test_name=(
                "platform_tests/test_advanced_reboot.py::"
                "test_warm_reboot_sad[sad_bgp-r-msn4700-70]"
            ),
            sanitized_testname=(
                "platform_tests/test_advanced_reboot.py::"
                "test_warm_reboot_sad[sad_bgp-switch-hostname]"
            ),
        )
        workbook.selected_rows = [dual, sad]
        workbook.row_comments = {}
        dashboard = DashboardSnapshot(
            setup_platforms={
                "r-leopard-70": "x86_64-mlnx_msn4700-r0",
                "r-leopard-72": "x86_64-mlnx_msn4700-r0",
            },
            analyses=[
                DashboardAnalysis(
                    session_id="11377279",
                    key_id="key-dual",
                    test_name=(
                        "platform_tests/test_advanced_reboot.py::"
                        "test_warm_reboot[dual-r-leopard-70]"
                    ),
                    analysis="DualToR analysis",
                ),
                DashboardAnalysis(
                    session_id="11377279",
                    key_id="key-sad",
                    test_name=(
                        "platform_tests/test_advanced_reboot.py::"
                        "test_warm_reboot_sad[sad_bgp-r-leopard-72]"
                    ),
                    analysis="BGP sad-path analysis",
                ),
                DashboardAnalysis(
                    session_id="11377279",
                    key_id="key-sad",
                    test_name=(
                        "platform_tests/test_advanced_reboot.py::"
                        "test_warm_reboot_sad[sad_lag-r-leopard-72]"
                    ),
                    analysis="Unrelated sad-path analysis",
                ),
            ],
        )

        apply_dashboard_analysis(workbook, dashboard)

        self.assertEqual(
            {
                "failure-dual": "DualToR analysis",
                "failure-sad": "BGP sad-path analysis",
            },
            workbook.row_comments,
        )
        self.assertEqual(
            {
                "failure-dual": "analyzed",
                "failure-sad": "analyzed",
            },
            workbook.dashboard_statuses,
        )

    def test_dashboard_exact_name_wins_over_broad_sanitized_candidates(
        self,
    ) -> None:
        workbook = _snapshot(self.request.excel_path)
        exact_name = (
            "platform_tests/test_advanced_reboot.py::"
            "test_warm_reboot[dual-r-msn4700-70]"
        )
        row = replace(
            workbook.selected_rows[0],
            test_name=exact_name,
            sanitized_testname=(
                "platform_tests/test_advanced_reboot.py::"
                "test_warm_reboot[dual-switch-hostname]"
            ),
        )
        workbook.selected_rows = [row]
        workbook.row_comments = {}
        dashboard = DashboardSnapshot(
            analyses=[
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name=exact_name,
                    analysis="Exact analysis",
                ),
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name=(
                        "platform_tests/test_advanced_reboot.py::"
                        "test_warm_reboot[dual-r-leopard-72]"
                    ),
                    analysis="Other setup analysis",
                ),
            ]
        )

        apply_dashboard_analysis(workbook, dashboard)

        self.assertEqual(
            "Exact analysis",
            workbook.row_comments["failure-1"],
        )
        self.assertEqual(
            "analyzed",
            workbook.dashboard_statuses["failure-1"],
        )

    def test_dashboard_uses_hostname_template_with_exact_identity(
        self,
    ) -> None:
        workbook = _snapshot(self.request.excel_path)
        workbook.selected_rows = [
            replace(
                workbook.selected_rows[0],
                test_name="tests/a.py::test_a[real-1]",
                sanitized_testname=(
                    "tests/a.py::test_a[switch-hostname]"
                ),
            )
        ]
        workbook.row_comments = {}
        dashboard = DashboardSnapshot(
            analyses=[
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name="tests/a.py::test_a[other-2]",
                    analysis="Other setup analysis",
                ),
            ]
        )

        apply_dashboard_analysis(workbook, dashboard)

        self.assertEqual(
            "Other setup analysis",
            workbook.row_comments["failure-1"],
        )
        self.assertEqual(
            "analyzed",
            workbook.dashboard_statuses["failure-1"],
        )

    def test_dashboard_placeholder_is_status_not_comment_text(self) -> None:
        workbook = _snapshot(self.request.excel_path)
        workbook.row_comments = {}
        dashboard = DashboardSnapshot(
            analyses=[
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name="tests/a.py::test_a",
                    analysis="not found in dashboard",
                )
            ]
        )

        apply_dashboard_analysis(workbook, dashboard)

        self.assertEqual("", workbook.row_comments["failure-1"])
        self.assertEqual(
            "not_found",
            workbook.dashboard_statuses["failure-1"],
        )

    def test_dashboard_refresh_does_not_downgrade_resolved_rows(self) -> None:
        workbook = _snapshot(self.request.excel_path)
        first = workbook.selected_rows[0]
        second = replace(
            first,
            record_id="failure-2",
            excel_row=3,
            session_id="s2",
            mars_key_id="k2",
            test_name="tests/b.py::test_b",
            sanitized_testname="tests/b.py::test_b",
        )
        workbook.selected_rows = [first, second]
        workbook.row_comments = {
            "failure-1": "Existing engineer analysis",
            "failure-2": "",
        }
        workbook.dashboard_statuses = {
            "failure-1": "analyzed",
            "failure-2": "not_found",
        }
        refreshed = DashboardSnapshot(
            analyses=[
                DashboardAnalysis(
                    session_id="s2",
                    key_id="k2",
                    test_name="tests/b.py::test_b",
                    analysis="New engineer analysis",
                )
            ]
        )

        apply_dashboard_analysis(
            workbook,
            refreshed,
            record_ids={"failure-2"},
        )

        self.assertEqual(
            "Existing engineer analysis",
            workbook.row_comments["failure-1"],
        )
        self.assertEqual(
            "analyzed",
            workbook.dashboard_statuses["failure-1"],
        )
        self.assertEqual(
            "New engineer analysis",
            workbook.row_comments["failure-2"],
        )

    def test_dashboard_rejects_ambiguous_canonical_analyses(self) -> None:
        workbook = _snapshot(self.request.excel_path)
        workbook.selected_rows = [
            replace(
                workbook.selected_rows[0],
                test_name="tests/a.py::test_a[excel-parameter]",
                sanitized_testname="tests/a.py::test_a[excel-parameter]",
            )
        ]
        workbook.row_comments = {}
        dashboard = DashboardSnapshot(
            analyses=[
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name="tests/a.py::test_a[dashboard-a]",
                    analysis="Analysis A",
                ),
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name="tests/a.py::test_a[dashboard-b]",
                    analysis="Analysis B",
                ),
            ]
        )

        apply_dashboard_analysis(workbook, dashboard)

        self.assertEqual("", workbook.row_comments["failure-1"])
        self.assertEqual(
            "not_found",
            workbook.dashboard_statuses["failure-1"],
        )

    def test_dashboard_does_not_merge_exact_analyses_from_other_versions(self) -> None:
        workbook = _snapshot(self.request.excel_path)
        workbook.row_comments = {}
        dashboard = DashboardSnapshot(
            analyses=[
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name="tests/a.py::test_a",
                    analysis="Analysis A",
                    redmine_url="https://redmine.mellanox.com/issues/111",
                    updated_at="2026-08-28 09:00:00",
                    switch_version="202605_RC.82-old_Internal",
                ),
                DashboardAnalysis(
                    session_id="s",
                    key_id="k",
                    test_name="tests/a.py::test_a",
                    analysis="Analysis B",
                    redmine_url="https://redmine.mellanox.com/issues/222",
                    updated_at="2026-08-28 10:00:00",
                    switch_version="202605_RC.83-new_Internal",
                ),
            ]
        )

        apply_dashboard_analysis(workbook, dashboard)

        self.assertEqual("", workbook.row_comments["failure-1"])
        self.assertEqual("not_found", workbook.dashboard_statuses["failure-1"])

    def test_dashboard_uses_latest_session_and_case_analysis(self) -> None:
        workbook = _snapshot(self.request.excel_path)
        workbook.row_comments = {}
        dashboard = DashboardSnapshot(
            analyses=[
                DashboardAnalysis(
                    session_id="s",
                    key_id="",
                    test_name="tests/a.py::test_a",
                    analysis="Old analysis",
                    updated_at="2026-08-27 10:00:00",
                ),
                DashboardAnalysis(
                    session_id="s",
                    key_id="",
                    test_name="tests/a.py::test_a",
                    analysis="Latest analysis",
                    updated_at="2026-08-28 10:00:00",
                ),
            ]
        )

        apply_dashboard_analysis(workbook, dashboard)

        comment = workbook.row_comments["failure-1"]
        self.assertLess(comment.index("Latest analysis"), comment.index("Old analysis"))
        self.assertEqual(
            "analyzed",
            workbook.dashboard_statuses["failure-1"],
        )

    def test_slm_setup_is_redacted_without_changing_r_prefixed_parameters(
        self,
    ) -> None:
        snapshot = _snapshot(self.request.excel_path)
        test_name = (
            "tests/a.py::test_a[r-sn5640-16-r-msn4700-01]"
        )
        snapshot.selected_rows = [
            replace(
                snapshot.selected_rows[0],
                test_name=test_name,
                sanitized_testname=test_name,
            )
        ]
        snapshot.row_comments = {}
        transport = _CapturingTransport()
        dependencies, _, _ = self._dependencies(
            transport,
            workbook=snapshot,
            dashboard=_Dashboard(
                [
                    DashboardAnalysis(
                        session_id="s",
                        key_id="k",
                        test_name=test_name,
                        analysis="disable unsupported service on slm-238",
                    )
                ]
            ),
        )

        self.assertEqual(
            0,
            run(
                self.request,
                self.settings,
                dependencies,
                stderr=io.StringIO(),
            ),
        )

        html = transport.messages[0][0].get_body(
            preferencelist=("html",)
        ).get_content()
        self.assertNotIn("slm-238", html)
        self.assertNotIn("Unknown platform", html)
        self.assertIn("r-sn5640-16-r-msn4700-01", html)

    def test_dashboard_record_without_analysis_is_red_internal_warning(self) -> None:
        snapshot = _snapshot(self.request.excel_path)
        snapshot.row_comments = {}
        transport = _CapturingTransport()
        dependencies, _, artifacts = self._dependencies(
            transport,
            workbook=snapshot,
            dashboard=_Dashboard(
                [
                    DashboardAnalysis(
                        session_id="s",
                        key_id="k",
                        test_name="tests/a.py::test_a",
                        analysis="",
                    )
                ]
            ),
        )

        result = run(
            self.request,
            self.settings,
            dependencies,
            stderr=io.StringIO(),
        )

        self.assertEqual(0, result)
        group = artifacts.semantic.failure_groups[0]
        self.assertEqual("Under investigation", group.comments)
        self.assertTrue(group.internal_review_warning)
        self.assertEqual("no_analysis", group.internal_review_status)
        self.assertEqual(
            "not analyzed",
            group.internal_comments,
        )
        html = transport.messages[0][0].get_body(
            preferencelist=("html",)
        ).get_content()
        self.assertRegex(
            html,
            (
                r'<span style="color:#c00000">not analyzed</span></td>'
            ),
        )

    def test_partial_dashboard_warning_does_not_color_analysis_red(
        self,
    ) -> None:
        warning = (
            "Dashboard analysis was not obtained for some grouped failure "
            "cases."
        )
        group = SemanticGroup(
            group_id="failure-1",
            member_ids=["failure-1"],
            test_display="tests/a.py::test_a",
            testbeds=["SN5600"],
            internal_comments=COMMENT_SEPARATOR.join(
                ["Useful dashboard analysis.", warning]
            ),
            internal_review_warning=True,
        )

        rendered = _failure_table([group])

        self.assertIn("Useful dashboard analysis.", rendered)
        self.assertNotIn(
            '<span style="color:#c00000">Useful dashboard analysis.</span>',
            rendered,
        )
        self.assertIn(
            '<span style="color:#c00000">{}</span>'.format(warning),
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
