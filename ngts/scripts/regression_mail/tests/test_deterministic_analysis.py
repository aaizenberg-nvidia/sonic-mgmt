from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ngts.scripts.regression_mail.grouping import (
    SourceSkipAnalyzer,
    _condition_selector_matches,
    _evaluate_condition,
    _retain_ticket_refs,
    build_deterministic_report,
)
from ngts.scripts.regression_mail.models import ResultRow, WorkbookSnapshot
from ngts.scripts.regression_mail.normalization import sanitize_public_comment
from ngts.scripts.regression_mail.redmine import (
    RedmineClient,
    _error_log_candidates,
    _ticket_context,
    _ticket_group,
    _ticket_status_is_closed,
)
from ngts.scripts.regression_mail.tests.helpers import HttpResponse as _Response


VERSION = "202605_RC.70-deadbeef_Internal"


def _skip_snapshot(
    path: Path,
    test_name: str = "tests/a.py::test_feature[param]",
) -> WorkbookSnapshot:
    row = ResultRow(
        record_id="skip-1",
        excel_row=2,
        session_id="s",
        mars_key_id="k",
        testbed="tb",
        test_name=test_name,
        sanitized_testname=test_name,
        result="skipped",
        message="",
        topology="t0",
        hwsku="sku",
    )
    return WorkbookSnapshot(
        source_path=path,
        sheet_name="community_tests",
        header_row=1,
        selected_rows=[row],
        result_counts={"pass": 0, "fail": 0, "skipped": 1},
        hardware_pairs=[],
    )


class DeterministicAnalysisTest(unittest.TestCase):
    def test_condition_selector_respects_nodeid_boundaries(self) -> None:
        self.assertTrue(
            _condition_selector_matches(
                "tests/a.py::test_foo",
                {},
                "tests/a.py::test_foo[param]",
            )
        )
        self.assertFalse(
            _condition_selector_matches(
                "tests/a.py::test_foo",
                {},
                "tests/a.py::test_foobar[param]",
            )
        )
        self.assertFalse(
            _condition_selector_matches(
                "acl",
                {},
                "acl2/test_acl.py::test_rule",
            )
        )

    def test_condition_evaluator_supports_safe_real_yaml_operations(
        self,
    ) -> None:
        row = replace(
            _skip_snapshot(Path("results.xlsx")).selected_rows[0],
            platform="x86_64-nvidia_sn6600-r0",
            topology="t0-64",
            values={"num_asics": "2"},
        )

        self.assertTrue(
            _evaluate_condition(
                "platform.startswith('x86_64')",
                row,
            )
        )
        self.assertTrue(
            _evaluate_condition("int(num_asics) >= 2", row)
        )
        self.assertTrue(
            _evaluate_condition("topo_name.split('-')[0] == 't0'", row)
        )

    def test_retain_ticket_refs_removes_disallowed_rm_shorthand(self) -> None:
        result = _retain_ticket_refs(
            "Blocked by RM 111 and RM 222",
            ["https://redmine.mellanox.com/issues/111"],
        )

        self.assertIn("RM 111", result)
        self.assertNotIn("RM 222", result)

    def test_public_comment_keeps_public_pr_and_removes_internal_urls(self) -> None:
        value = sanitize_public_comment(
            "See https://redmine.mellanox.com/issues/123 and "
            "https://allure.nvidia.com/report/7; fixed by "
            "https://github.com/sonic-net/sonic-mgmt/pull/99"
        )
        self.assertEqual(
            "See and fixed by "
            "https://github.com/sonic-net/sonic-mgmt/pull/99",
            value,
        )

    def test_static_skip_analysis_is_not_used_for_ticket_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_path = root / "tests" / "a.py"
            test_path.parent.mkdir()
            test_path.write_text(
                """
import pytest

@pytest.mark.skipif(True, reason="feature is unsupported")
def test_feature():
    pass

def test_other():
    pytest.skip(dynamic_reason)
""",
                encoding="utf-8",
            )
            workbook = _skip_snapshot(root / "results.xlsx")
            semantic = build_deterministic_report(workbook)
            SourceSkipAnalyzer().enrich(semantic, workbook, root)

            internal = semantic.skip_groups[0].internal_comments
            self.assertEqual("", internal)
            self.assertEqual({}, workbook.row_comments)

    def test_static_skip_analysis_does_not_override_excel_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_path = root / "tests" / "a.py"
            test_path.parent.mkdir()
            test_path.write_text(
                """
import pytest

class TestA:
    @pytest.mark.skip(reason="reason A")
    def test_feature(self):
        pass

class TestB:
    @pytest.mark.skip(reason="reason B")
    def test_feature(self):
        pass
""",
                encoding="utf-8",
            )
            workbook = _skip_snapshot(
                root / "results.xlsx",
                "tests/a.py::TestA::test_feature[param]",
            )
            semantic = build_deterministic_report(workbook)
            SourceSkipAnalyzer().enrich(semantic, workbook, root)

            internal = semantic.skip_groups[0].internal_comments
            self.assertEqual("", internal)

    def test_source_ticket_is_applied_only_to_matching_skip_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_path = root / "tests" / "hash" / "test_generic_hash.py"
            test_path.parent.mkdir(parents=True)
            test_path.write_text(
                """
import pytest

def test_reboot(reboot_type, rand_selected_dut):
    if reboot_type in ("warm", "fast") and "sn6600_ld" in rand_selected_dut.facts.get("platform", ""):
        pytest.skip("warm/fast reboot is not supported on sn6600_ld (RM 5154543)")
""",
                encoding="utf-8",
            )
            rows = [
                ResultRow(
                    record_id="warm",
                    excel_row=2,
                    session_id="s",
                    mars_key_id="warm",
                    testbed="tb-warm",
                    test_name="hash/test_generic_hash.py::test_reboot[warm]",
                    sanitized_testname="",
                    result="skipped",
                    message=(
                        "warm/fast reboot is not supported on sn6600_ld "
                        "(RM 5154543)"
                    ),
                    topology="t1",
                    hwsku="sku",
                    platform="x86_64-nvidia_sn6600_ld-r0",
                ),
                ResultRow(
                    record_id="fast",
                    excel_row=3,
                    session_id="s",
                    mars_key_id="fast",
                    testbed="tb-fast",
                    test_name="hash/test_generic_hash.py::test_reboot[fast]",
                    sanitized_testname="",
                    result="skipped",
                    message=(
                        "warm/fast reboot is not supported on sn6600_ld "
                        "(RM 5154543)"
                    ),
                    topology="t1",
                    hwsku="sku",
                    platform="x86_64-nvidia_sn6600_ld-r0",
                ),
                ResultRow(
                    record_id="cold",
                    excel_row=4,
                    session_id="s",
                    mars_key_id="cold",
                    testbed="tb-cold",
                    test_name="hash/test_generic_hash.py::test_reboot[cold]",
                    sanitized_testname="",
                    result="skipped",
                    message="INNER_ETHERTYPE is unsupported",
                    topology="t1",
                    hwsku="sku",
                    platform="x86_64-nvidia_sn6600_ld-r0",
                ),
            ]
            workbook = WorkbookSnapshot(
                source_path=root / "results.xlsx",
                sheet_name="community_tests",
                header_row=1,
                selected_rows=rows,
                result_counts={"pass": 0, "fail": 0, "skipped": 3},
                hardware_pairs=[],
            )
            semantic = build_deterministic_report(workbook)
            SourceSkipAnalyzer().enrich(semantic, workbook, root)

            ticket = "https://redmine.mellanox.com/issues/5154543"
            ticket_groups = [
                group
                for group in semantic.skip_groups
                if ticket in group.redmine_urls
            ]
            self.assertEqual(1, len(ticket_groups))
            self.assertEqual(
                {"warm", "fast"},
                set(ticket_groups[0].member_ids),
            )
            cold_group = next(
                group
                for group in semantic.skip_groups
                if "cold" in group.member_ids
            )
            self.assertEqual([], cold_group.redmine_urls)
            self.assertNotIn(ticket, cold_group.internal_comments)

    def test_condition_yaml_ticket_is_ignored_for_skip_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_path = root / "tests" / "a.py"
            test_path.parent.mkdir()
            test_path.write_text(
                "def test_feature():\n    pass\n",
                encoding="utf-8",
            )
            condition_dir = (
                root /
                "tests" /
                "common" /
                "plugins" /
                "conditional_mark"
            )
            condition_dir.mkdir(parents=True)
            condition_file = condition_dir / "tests_mark_conditions.yaml"
            condition_file.write_text(
                """
tests/a.py:
  skip:
    reason: "Platform bug"
    conditions:
      - "https://redmine.mellanox.com/issues/123 and 'sn6600' in platform"
""",
                encoding="utf-8",
            )
            workbook = _skip_snapshot(
                root / "results.xlsx",
                "tests/a.py::test_feature[param]",
            )
            workbook.selected_rows[0] = replace(
                workbook.selected_rows[0],
                platform="x86_64-nvidia_sn6600-r0",
                message="Skipped by conditional mark",
            )
            semantic = build_deterministic_report(workbook)

            SourceSkipAnalyzer().enrich(semantic, workbook, root)

            self.assertEqual(1, len(semantic.skip_groups))
            self.assertEqual([], semantic.skip_groups[0].redmine_urls)
            self.assertEqual(
                "tests/a.py::test_feature[param]",
                semantic.skip_groups[0].test_display,
            )
            self.assertEqual(
                "",
                semantic.skip_groups[0].internal_comments,
            )

    def test_condition_yaml_does_not_fold_skip_cases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_path = root / "tests" / "qos" / "test_qos.py"
            test_path.parent.mkdir(parents=True)
            test_path.write_text(
                "class TestQos:\n"
                "    def testQosSaiDwrr(self): pass\n"
                "    def testQosSaiLossy(self): pass\n",
                encoding="utf-8",
            )
            condition_dir = (
                root / "tests" / "common" / "plugins" / "conditional_mark"
            )
            condition_dir.mkdir(parents=True)
            (condition_dir / "tests_mark_conditions.yaml").write_text(
                """
qos/test_qos.py::TestQos::testQosSaiDwrr:
  skip:
    reason: "Shared shaper issue"
    conditions:
      - "https://redmine.mellanox.com/issues/5167432"
qos/test_qos.py::TestQos::testQosSaiLossy:
  skip:
    reason: "Shared shaper issue"
    conditions:
      - "https://redmine.mellanox.com/issues/5167432"
""",
                encoding="utf-8",
            )
            workbook = _skip_snapshot(
                root / "results.xlsx",
                "qos/test_qos.py::TestQos::testQosSaiDwrr[param]",
            )
            workbook.selected_rows.append(
                replace(
                    workbook.selected_rows[0],
                    record_id="skip-2",
                    mars_key_id="k-2",
                    test_name=(
                        "qos/test_qos.py::TestQos::testQosSaiLossy[param]"
                    ),
                    sanitized_testname=(
                        "qos/test_qos.py::TestQos::testQosSaiLossy[param]"
                    ),
                    excel_row=3,
                )
            )
            semantic = build_deterministic_report(workbook)

            SourceSkipAnalyzer().enrich(semantic, workbook, root)

            self.assertEqual(2, len(semantic.skip_groups))
            self.assertEqual(
                {
                    "qos/test_qos.py::TestQos::testQosSaiDwrr[param]",
                    "qos/test_qos.py::TestQos::testQosSaiLossy[param]",
                },
                {
                    group.test_display
                    for group in semantic.skip_groups
                },
            )

    def test_condition_yaml_does_not_add_public_issue_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_path = root / "tests" / "hft" / "test_hft.py"
            test_path.parent.mkdir(parents=True)
            test_path.write_text(
                "def test_a(): pass\ndef test_b(): pass\n",
                encoding="utf-8",
            )
            condition_dir = (
                root / "tests" / "common" / "plugins" / "conditional_mark"
            )
            condition_dir.mkdir(parents=True)
            (condition_dir / "tests_mark_conditions.yaml").write_text(
                """
hft/test_hft.py::test_a:
  skip:
    reason: "https://github.com/sonic-net/sonic-mgmt/issues/25812"
    conditions:
      - "topo_name == 't0'"
hft/test_hft.py::test_b:
  skip:
    reason: "https://github.com/sonic-net/sonic-mgmt/issues/25812"
    conditions:
      - "topo_name == 't0'"
""",
                encoding="utf-8",
            )
            workbook = _skip_snapshot(
                root / "results.xlsx",
                "hft/test_hft.py::test_a[param]",
            )
            workbook.selected_rows.append(
                replace(
                    workbook.selected_rows[0],
                    record_id="skip-2",
                    mars_key_id="k-2",
                    test_name="hft/test_hft.py::test_b[param]",
                    sanitized_testname="hft/test_hft.py::test_b[param]",
                    excel_row=3,
                )
            )
            semantic = build_deterministic_report(workbook)

            SourceSkipAnalyzer().enrich(semantic, workbook, root)

            self.assertEqual(2, len(semantic.skip_groups))
            self.assertEqual(
                {
                    "hft/test_hft.py::test_a[param]",
                    "hft/test_hft.py::test_b[param]",
                },
                {
                    group.test_display
                    for group in semantic.skip_groups
                },
            )
            self.assertEqual({}, workbook.public_issue_conditions)

    def test_condition_yaml_or_does_not_project_issue_branches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            test_path = root / "tests" / "a.py"
            test_path.parent.mkdir(parents=True)
            test_path.write_text(
                "def test_feature(): pass\n",
                encoding="utf-8",
            )
            condition_dir = (
                root / "tests" / "common" / "plugins" / "conditional_mark"
            )
            condition_dir.mkdir(parents=True)
            issue_123 = (
                "https://github.com/sonic-net/sonic-mgmt/issues/123"
            )
            issue_1234 = issue_123 + "4"
            (condition_dir / "tests_mark_conditions.yaml").write_text(
                """
tests/a.py:
  skip:
    reason: "Platform-specific public issues"
    conditions_logical_operator: OR
    conditions:
      - "{} and topo_name == 't0'"
      - "{} and topo_name == 'dualtor'"
""".format(issue_123, issue_1234),
                encoding="utf-8",
            )
            workbook = _skip_snapshot(root / "results.xlsx")
            semantic = build_deterministic_report(workbook)

            SourceSkipAnalyzer().enrich(semantic, workbook, root)

            self.assertEqual({}, workbook.public_issue_conditions)

    def test_static_skip_analysis_rejects_paths_outside_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "repo"
            source_root.mkdir()
            (root / "outside.py").write_text(
                """
import pytest

@pytest.mark.skip(reason="outside")
def test_feature():
    pass
""",
                encoding="utf-8",
            )
            workbook = _skip_snapshot(
                root / "results.xlsx",
                "../outside.py::test_feature[param]",
            )
            semantic = build_deterministic_report(workbook)
            SourceSkipAnalyzer().enrich(
                semantic,
                workbook,
                source_root,
            )

            self.assertNotIn("outside", semantic.skip_groups[0].internal_comments)

    def test_redmine_error_candidates_do_not_drop_late_distinct_pattern(
        self,
    ) -> None:
        lines = [
            "ERR service: failure type {} {}".format(
                index,
                "x" * 100,
            )
            for index in range(300)
        ]
        lines.append("ERR final-service: terminal distinct failure")

        result = _error_log_candidates("\n".join(lines))

        self.assertIn(
            "ERR final-service: terminal distinct failure",
            result.splitlines(),
        )
        self.assertLessEqual(len(result), 64000)

    def test_redmine_ticket_summary_includes_owner_and_status(self) -> None:
        def opener(request, timeout):
            del timeout
            self.assertTrue(
                request.full_url.endswith("/issues/5048947.json")
            )
            payload = {
                "issue": {
                    "id": 5048947,
                    "project": {"name": "SONIC - Design"},
                    "tracker": {"name": "Bug SW"},
                    "subject": (
                        "[Community][caclmgrd] iptables rules are removed"
                    ),
                    "assigned_to": {"name": "Hemanth Kumar Tirupati"},
                    "status": {"name": "In Progress"},
                }
            }
            return _Response(json.dumps(payload).encode())

        summary = RedmineClient(
            "token",
            opener=opener,
        ).describe_tickets(
            ["https://redmine.mellanox.com/issues/5048947"]
        )

        self.assertEqual(
            (
                "[SONIC - Design] Bug SW #5048947: "
                "[Community][caclmgrd] iptables rules are removed"
                " | Assignee: Hemanth Kumar Tirupati"
                " | Status: In Progress"
            ),
            summary["https://redmine.mellanox.com/issues/5048947"],
        )

    def test_redmine_ticket_summary_keeps_successful_partial_results(
        self,
    ) -> None:
        def opener(request, timeout):
            del timeout
            if request.full_url.endswith("/issues/111.json"):
                raise RuntimeError("ticket 111 is unavailable")
            payload = {
                "issue": {
                    "id": 222,
                    "project": {"name": "SONIC - Design"},
                    "tracker": {"name": "Bug SW"},
                    "subject": "Available ticket",
                    "assigned_to": {"name": "Owner"},
                    "status": {"name": "In Progress"},
                }
            }
            return _Response(json.dumps(payload).encode())

        summaries = RedmineClient(
            "token",
            opener=opener,
        ).describe_tickets(
            [
                "https://redmine.mellanox.com/issues/111",
                "https://redmine.mellanox.com/issues/222",
            ]
        )

        self.assertEqual(
            ["https://redmine.mellanox.com/issues/222"],
            list(summaries),
        )
        self.assertIn("Available ticket", next(iter(summaries.values())))

    def test_redmine_ticket_context_extracts_public_link_candidates(
        self,
    ) -> None:
        issue_url = (
            "https://github.com/sonic-net/sonic-buildimage/issues/123"
        )
        pr_url = (
            "https://github.com/sonic-net/sonic-buildimage/pull/456"
        )
        removed_pr_url = (
            "https://github.com/sonic-net/sonic-buildimage/pull/455"
        )

        def opener(request, timeout):
            self.assertTrue(request.full_url.endswith(
                "/issues/5048947.json?include=journals"
            ))
            payload = {
                "issue": {
                    "id": 5048947,
                    "project": {"name": "SONIC - Design"},
                    "tracker": {"name": "Bug SW"},
                    "subject": "[Test] Validate feature behavior",
                    "description": "Related issue: " + issue_url,
                    "journals": [
                        {"notes": "A proposed fix is under review"},
                        {
                            "details": [
                                {
                                    "property": "development_item",
                                    "name": "active",
                                    "new_value": pr_url,
                                },
                                {
                                    "property": "development_item",
                                    "name": "removed",
                                    "new_value": removed_pr_url,
                                },
                            ],
                        },
                        {
                            "details": [
                                {
                                    "property": "development_item",
                                    "name": "removed",
                                    "old_value": removed_pr_url,
                                    "new_value": None,
                                },
                            ],
                        },
                    ],
                    "status": {"name": "In Progress"},
                    "assigned_to": {"name": "Owner"},
                }
            }
            return _Response(json.dumps(payload).encode())

        contexts = RedmineClient(
            "token",
            opener=opener,
        ).inspect_tickets(
            ["https://redmine.mellanox.com/issues/5048947"]
        )

        context = contexts[
            "https://redmine.mellanox.com/issues/5048947"
        ]
        self.assertEqual([issue_url], context["github_issue_urls"])
        self.assertEqual([pr_url], context["github_pr_urls"])
        self.assertEqual([pr_url], context["active_github_pr_urls"])
        self.assertNotIn(removed_pr_url, context["evidence"])
        self.assertTrue(context["is_test_issue"])
        self.assertEqual("In Progress", context["status"])
        self.assertFalse(context["is_closed"])
        self.assertIn("proposed fix", context["evidence"])

    def test_redmine_filters_and_extracts_current_branch_occurrences(self) -> None:
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            self.assertEqual(
                "token",
                request.headers["X-redmine-api-key"],
            )
            if "/projects/sonic-design/issues.json" in request.full_url:
                ticket_ids = (
                    [2]
                    if "status_id=11" in request.full_url
                    else [1, 3]
                )
                payload = {
                    "issues": [
                        {"id": ticket_id}
                        for ticket_id in ticket_ids
                    ],
                    "total_count": len(ticket_ids),
                }
            elif request.full_url.endswith(
                "/issues/1.json?include=journals"
            ):
                payload = {
                    "issue": _ticket(
                        1,
                        status_id=12,
                        detected=VERSION,
                        description=(
                            "Log Analyzer detected log errors during test: "
                            "test_acl[a] Testbed name: tb-current\n"
                            "2025 Jun 5 16:42:37.333005 tb-current "
                            "ERR dockerd: connection failed with 10.1.2.3:49\n"
                            "2025 Jun 5 16:42:38.444006 other-host "
                            "ERR dockerd: connection failed with 10.1.2.4:49"
                        ),
                    )
                }
            elif request.full_url.endswith(
                "/issues/2.json?include=journals"
            ):
                ticket = _ticket(
                    2,
                    status_id=11,
                    detected="202604_RC.1-old_Internal",
                    description=(
                        "test_old Testbed name: tb-old\n"
                        "2025 Jun 5 16:42:37 host-old ERR dockerd: "
                        "tac_connect_single: connection failed with "
                        "10.1.2.3:49: Transport endpoint is not connected\n"
                        "2025 Jun 5 16:42:38 host-old ERR dockerd: "
                        "tac_connect_single: connection failed with "
                        "10.1.2.4:49: Transport endpoint is not connected\n"
                        "2025 Jun 5 16:42:39 host-old ERR dockerd: "
                        "nss_tacplus: failed to connect TACACS+ server "
                        "10.1.2.3:49, ret=-9: Transport endpoint is not "
                        "connected\n"
                        "2025 Jun 5 16:42:40 host-old ERR database: "
                        "RuntimeError: Unable to connect to redis#012"
                        "  File \"/usr/bin/listener\", line 246, in "
                        "&lt;module&gt;"
                        "#012RuntimeError: Unable to connect to redis"
                    ),
                )
                ticket["journals"] = [
                    {
                        "id": 99,
                        "notes": (
                            "Reproduced on 202605_RC.70-new_Internal "
                            "during test_new[b] --testbed tb-new\n"
                            "2025 Jun 5 16:42:39 tb-new ERR journalctl: "
                            "wrapper output that must not replace the "
                            "ticket description"
                        ),
                    }
                ]
                payload = {"issue": ticket}
            elif request.full_url.endswith(
                "/issues/3.json?include=journals"
            ):
                payload = {
                    "issue": _ticket(
                        3,
                        status_id=12,
                        detected="202605_RC.71-new_Internal",
                        description=(
                            "Log Analyzer detected log errors during test: "
                            "test_wrong_rc Testbed name: tb-wrong"
                        ),
                    )
                }
            else:
                raise AssertionError(request.full_url)
            return _Response(json.dumps(payload).encode())

        groups = RedmineClient(
            "token",
            opener=opener,
        ).find_log_analyzer_errors(VERSION, "2026-01-01")

        self.assertEqual(2, len(groups))
        self.assertEqual("test_acl[a]", groups[0].test_display)
        self.assertEqual(["tb-current"], groups[0].testbeds)
        self.assertEqual(
            "2025 Jun 5 16:42:37.333005 tb-current ERR dockerd: "
            "connection failed with 10.1.2.3:49",
            groups[0].comments,
        )
        self.assertEqual("test_new[b]", groups[1].test_display)
        self.assertEqual(["tb-new"], groups[1].testbeds)
        self.assertNotIn("[log_analyzer]", groups[1].internal_comments)
        self.assertIn(
            "Redmine: https://redmine.mellanox.com/issues/2",
            groups[1].internal_comments,
        )
        self.assertEqual(
            "2025 Jun 5 16:42:39 tb-new ERR journalctl: "
            "wrapper output that must not replace the ticket description",
            groups[1].comments,
        )
        self.assertNotIn("host-old", groups[1].comments)
        self.assertTrue(
            all("test_wrong_rc" not in group.test_display for group in groups)
        )
        self.assertTrue(
            all("tracker_id=28" in call for call in calls[:2])
        )
        self.assertTrue(
            all("author_id=7680" in call for call in calls[:2])
        )
        self.assertTrue(
            all("updated_on=%3E%3D2026-01-01" in call for call in calls[:2])
        )

    def test_redmine_closed_status_recognizes_real_status_names(self) -> None:
        for status in (
            "Closed",
            "Closed (Rejected)",
            "Fixed",
            "Resolved",
            "Won't fix",
        ):
            with self.subTest(status=status):
                self.assertTrue(
                    _ticket_status_is_closed({"name": status})
                )
        self.assertFalse(
            _ticket_status_is_closed({"name": "In Progress"})
        )

    def test_verification_project_ticket_is_a_test_issue(self) -> None:
        ticket = _ticket(
            5216117,
            status_id=12,
            detected=VERSION,
            description="VXLAN test failure",
        )
        ticket["project"] = {
            "id": 9567,
            "name": "SONIC - Verification",
        }
        ticket["subject"] = (
            "[202605] [scale topo] test_vxlan_ecmp needs BFD"
        )

        self.assertTrue(_ticket_context(ticket)["is_test_issue"])

    def test_redmine_occurrence_uses_explicit_system_type(self) -> None:
        ticket = _ticket(
            5211704,
            status_id=12,
            detected=VERSION,
            description=(
                "Log Analyzer detected log errors during test: "
                "test_core_functionality_with_reboot "
                "Testbed name: sonic_simx_r-panther-simx-23 "
                "System type: ACS-MSN2700"
            ),
        )

        group = _ticket_group(ticket, "202605_RC.70")

        self.assertIsNotNone(group)
        self.assertEqual(["ACS-MSN2700"], group.testbeds)


def _ticket(
    ticket_id: int,
    status_id: int,
    detected: str,
    description: str,
):
    return {
        "id": ticket_id,
        "project": {"id": 9712, "name": "SONIC - Design"},
        "tracker": {"id": 28, "name": "Bug SW"},
        "author": {"id": 7680, "name": "LogAnalyzer User"},
        "status": {
            "id": status_id,
            "name": "Won't fix" if status_id == 11 else "Assigned",
        },
        "subject": "[log_analyzer] error {}".format(ticket_id),
        "description": description,
        "custom_fields": [
            {
                "name": "Detected In Version",
                "value": detected,
            }
        ],
        "journals": [],
    }


if __name__ == "__main__":
    unittest.main()
