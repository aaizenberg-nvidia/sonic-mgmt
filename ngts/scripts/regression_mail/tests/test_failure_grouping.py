from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ngts.scripts.regression_mail.grouping import (
    _dashboard_group_status,
    build_deterministic_report,
)
from ngts.scripts.regression_mail.models import ResultRow, WorkbookSnapshot
from ngts.scripts.regression_mail.normalization import COMMENT_SEPARATOR
from ngts.scripts.regression_mail.tests.helpers import result_row


def _row(
    record_id: str,
    name: str,
    result: str,
    testbed: str,
    comment: str = "",
) -> ResultRow:
    return result_row(
        record_id,
        name,
        result,
        testbed=testbed,
        message=comment,
        values={"comments": comment} if comment else {},
    )


def _report(rows, comments, statuses=None):
    with tempfile.TemporaryDirectory() as directory:
        workbook = WorkbookSnapshot(
            source_path=Path(directory) / "results.xlsx",
            sheet_name="community_tests",
            header_row=1,
            selected_rows=list(rows),
            result_counts={},
            hardware_pairs=[],
            row_comments=dict(comments),
            dashboard_statuses=dict(statuses or {}),
        )
        return build_deterministic_report(workbook)


class FailureGroupingTest(unittest.TestCase):
    def test_failure_hex_dump_offset_is_not_a_redmine_ticket(self) -> None:
        message = (
            "0040  52 6D E0 05 FE 0E 52 6D  ........Rm....Rm\n"
            "0050  E0 05 08 00 45 00 00 56"
        )
        row = result_row(
            "row-1",
            "fib/test_fib.py::test_vxlan_hash[ipv6-ipv4]",
            message=message,
            values={"message": message},
        )

        report = _report([row], {}, {"row-1": "no_analysis"})

        self.assertEqual([], report.failure_groups[0].ticket_refs)
        self.assertEqual([], report.failure_groups[0].redmine_urls)
        self.assertEqual("", report.failure_groups[0].internal_comments)

    def test_failure_rm_register_offset_cannot_cross_line_boundary(
        self,
    ) -> None:
        message = "register RM\n100000  52 6D E0 05 FE 0E"
        row = result_row(
            "row-1",
            "fib/test_fib.py::test_vxlan_hash[ipv6-ipv4]",
            message=message,
            values={
                "message": message,
                "comments": "register RM\n0050  E0 05 08 00",
            },
        )

        report = _report([row], {}, {"row-1": "no_analysis"})

        self.assertEqual([], report.failure_groups[0].ticket_refs)
        self.assertEqual([], report.failure_groups[0].redmine_urls)

    def test_failure_message_keeps_explicit_rm_ticket_reference(self) -> None:
        message = "Failure is tracked by RM ticket 123"
        row = result_row(
            "row-1",
            "tests/a.py::test_failure",
            message=message,
            values={"message": message},
        )

        report = _report([row], {})

        self.assertEqual(
            ["https://redmine.mellanox.com/issues/123"],
            report.failure_groups[0].ticket_refs,
        )

    def test_generic_failure_wrapper_does_not_merge_across_files(self) -> None:
        for wrapper in (
            "RunAnsibleModuleFail: run module config_facts rc = 1",
            "tests.common.errors.RunAnsibleModuleFail: "
            "run module config_facts rc = 1",
        ):
            with self.subTest(wrapper=wrapper):
                report = _report(
                    [
                        _row(
                            "row-1",
                            "tests/a.py::test_one",
                            "fail",
                            "tb-a",
                            wrapper,
                        ),
                        _row(
                            "row-2",
                            "tests/b.py::test_two",
                            "fail",
                            "tb-b",
                            wrapper,
                        ),
                    ],
                    {},
                )

                self.assertEqual(2, len(report.failure_groups))

    def test_failure_groups_do_not_mix_analyzed_and_missing_dashboard_rows(
        self,
    ) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_one",
                    "fail",
                    "tb-a",
                    "Shared failure text",
                ),
                _row(
                    "row-2",
                    "tests/b.py::test_two",
                    "fail",
                    "tb-b",
                    "Shared failure text",
                ),
            ],
            {"row-1": "Engineer analysis"},
            {"row-1": "analyzed", "row-2": "not_found"},
        )

        self.assertEqual(2, len(report.failure_groups))
        self.assertEqual(
            {"", "not_found"},
            {
                group.internal_review_status
                for group in report.failure_groups
            },
        )

    def test_different_internal_changes_are_different_root_causes(self) -> None:
        base = (
            "https://git-nbu-sw.nvidia.com/r/c/switchx/sonic/"
            "sonic-mgmt/+/"
        )
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_one",
                    "fail",
                    "tb-a",
                    "Fixed by " + base + "344987",
                ),
                _row(
                    "row-2",
                    "tests/b.py::test_two",
                    "fail",
                    "tb-b",
                    "Needs revert " + base + "344447",
                ),
            ],
            {},
            {"row-1": "analyzed", "row-2": "analyzed"},
        )

        self.assertEqual(2, len(report.failure_groups))
        self.assertEqual(
            [[base + "344987"], [base + "344447"]],
            [group.ticket_refs for group in report.failure_groups],
        )

    def test_multiple_references_on_same_rows_create_one_group(self) -> None:
        issue = "https://github.com/sonic-net/sonic-mgmt/issues/21571"
        change = (
            "https://git-nbu-sw.nvidia.com/r/c/switchx/sonic/"
            "sonic-mgmt/+/345028"
        )
        report = _report(
            [
                _row(
                    "row-1",
                    "everflow/test_everflow_ipv6.py::test_one",
                    "fail",
                    "tb-a",
                    "Known issue {} fixed by {}".format(issue, change),
                ),
                _row(
                    "row-2",
                    "everflow/test_everflow_ipv6.py::test_two",
                    "fail",
                    "tb-a",
                    "Known issue {} fixed by {}".format(issue, change),
                ),
            ],
            {},
            {"row-1": "analyzed", "row-2": "analyzed"},
        )

        self.assertEqual(1, len(report.failure_groups))
        group = report.failure_groups[0]
        self.assertEqual(["row-1", "row-2"], group.member_ids)
        self.assertEqual({issue, change}, set(group.ticket_refs))

    def test_skip_source_comments_do_not_override_excel_message(self) -> None:
        wrapper = "Skipped by conditional mark"
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_one",
                    "skipped",
                    "tb-a",
                    wrapper,
                ),
                _row(
                    "row-2",
                    "tests/b.py::test_two",
                    "skipped",
                    "tb-b",
                    wrapper,
                ),
            ],
            {
                "row-1": "Source evidence: reason A",
                "row-2": "Source evidence: reason B",
            },
        )

        self.assertEqual(1, len(report.skip_groups))
        self.assertEqual([], report.skip_groups[0].ticket_refs)

    def test_skip_ticket_is_read_only_from_excel_message(self) -> None:
        ticket = "https://redmine.mellanox.com/issues/5154543"
        row = result_row(
            "row-1",
            "tests/a.py::test_skip",
            result="skipped",
            message="Skipped by topology",
            values={
                "message": "Skipped by topology",
                "comments": ticket,
            },
        )

        report = _report(
            [row],
            {"row-1": "Source evidence: " + ticket},
        )

        self.assertEqual([], report.skip_groups[0].ticket_refs)
        self.assertEqual([], report.skip_groups[0].redmine_urls)

    def test_skip_condition_is_not_added_to_internal_comments(
        self,
    ) -> None:
        issue = "https://github.com/sonic-net/sonic-mgmt/issues/123"
        ticket = "https://redmine.mellanox.com/issues/5154543"
        message = (
            "(is_multi_asic == True) or "
            "(platform not in ['x86_64-nvidia_sn5640-r0'] and "
            "asic_type not in ['marvell-teralynx']) or {} and "
            "topo_name in ['t1-isolated'] RM ticket 5154543"
        ).format(issue)
        report = _report(
            [
                result_row(
                    "row-1",
                    "vxlan/test_vxlan_ecmp.py",
                    result="skipped",
                    message=message,
                ),
                result_row(
                    "row-2",
                    "vxlan/test_vxlan_underlay_ecmp.py",
                    result="skipped",
                    message=(
                        "(is_multi_asic == True) or "
                        "platform not in "
                        "['x86_64-nvidia_sn5640-r0'] or {} and "
                        "topo_name in ['t1-isolated'] "
                        "RM ticket 5154543"
                    ).format(issue),
                ),
                result_row(
                    "row-3",
                    "vxlan/test_vxlan_bfd_tsa.py",
                    result="skipped",
                    message=(
                        "{} and topo_name in ['t1-isolated'] "
                        "RM ticket 5154543"
                    ).format(issue),
                ),
            ],
            {},
        )

        group = report.skip_groups[0]
        self.assertNotIn("is_multi_asic", group.internal_comments)
        self.assertNotIn("topo_name", group.internal_comments)
        self.assertIn(issue, group.internal_comments)
        self.assertIn(ticket, group.internal_comments)
        self.assertIn(ticket, group.redmine_urls)

    def test_all_failed_parameters_with_same_cause_show_test_name(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_x[a]",
                    "fail",
                    "tb-1",
                    "Same cause",
                ),
                _row(
                    "row-2",
                    "tests/a.py::test_x[b]",
                    "fail",
                    "tb-2",
                    "Same cause",
                ),
                _row(
                    "row-3",
                    "tests/a.py::test_x[c]",
                    "fail",
                    "tb-3",
                    "Same cause",
                ),
            ],
            {},
        )

        self.assertEqual(1, len(report.failure_groups))
        group = report.failure_groups[0]
        self.assertEqual(
            "tests/a.py::test_x",
            group.test_display,
        )
        self.assertEqual("Same cause", group.comments)

    def test_partial_failed_parameters_with_same_cause_list_parameters(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_x[mode-a]",
                    "fail",
                    "tb-1",
                    "Same cause",
                ),
                _row(
                    "row-2",
                    "tests/a.py::test_x[mode-b]",
                    "fail",
                    "tb-2",
                    "Same cause",
                ),
                _row(
                    "row-3",
                    "tests/a.py::test_x[mode-c]",
                    "pass",
                    "tb-3",
                ),
            ],
            {},
        )

        self.assertEqual(1, len(report.failure_groups))
        self.assertEqual(
            "tests/a.py::test_x",
            report.failure_groups[0].test_display,
        )

    def test_mixed_parameterized_and_unparameterized_cases_remain_visible(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_x",
                    "fail",
                    "tb-1",
                    "Same cause",
                ),
                _row(
                    "row-2",
                    "tests/a.py::test_x[mode-a]",
                    "fail",
                    "tb-2",
                    "Same cause",
                ),
                _row(
                    "row-3",
                    "tests/a.py::test_x[mode-b]",
                    "pass",
                    "tb-3",
                ),
            ],
            {},
        )

        self.assertEqual(
            "tests/a.py::test_x",
            report.failure_groups[0].test_display,
        )

    def test_all_cases_in_file_with_same_cause_show_file(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_x",
                    "fail",
                    "tb-1",
                    "Same cause",
                ),
                _row(
                    "row-2",
                    "tests/a.py::test_y",
                    "fail",
                    "tb-2",
                    "Same cause",
                ),
            ],
            {},
        )

        self.assertEqual(1, len(report.failure_groups))
        self.assertEqual("tests/a.py", report.failure_groups[0].test_display)

    def test_error_logs_with_only_runtime_values_share_one_cause(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::TestFeature::test_x",
                    "fail",
                    "tb-1",
                    (
                        "2026 Jun 5 16:42:37 host-a ERR service: "
                        "returned value 268435456 from 10.1.2.3"
                    ),
                ),
                _row(
                    "row-2",
                    "tests/a.py::TestFeature::test_y",
                    "fail",
                    "tb-2",
                    (
                        "2026 Jun 5 16:43:38 host-b ERR service: "
                        "returned value 268435457 from 10.1.2.4"
                    ),
                ),
            ],
            {},
        )

        self.assertEqual(1, len(report.failure_groups))
        self.assertEqual(
            "tests/a.py::TestFeature",
            report.failure_groups[0].test_display,
        )

    def test_multiple_cases_in_class_with_same_cause_show_class(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::TestFeature::test_x[mode-a]",
                    "fail",
                    "tb-1",
                    "Same cause",
                ),
                _row(
                    "row-2",
                    "tests/a.py::TestFeature::test_y",
                    "fail",
                    "tb-2",
                    "Same cause",
                ),
                _row(
                    "row-3",
                    "tests/a.py::TestOther::test_z",
                    "pass",
                    "tb-3",
                ),
            ],
            {},
        )

        self.assertEqual(
            "tests/a.py::TestFeature",
            report.failure_groups[0].test_display,
        )

    def test_class_folding_does_not_merge_different_causes(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::TestFeature::test_x",
                    "fail",
                    "tb-1",
                    "Cause A",
                ),
                _row(
                    "row-2",
                    "tests/a.py::TestFeature::test_y",
                    "fail",
                    "tb-2",
                    "Cause B",
                ),
            ],
            {},
        )

        self.assertEqual(2, len(report.failure_groups))
        self.assertEqual(
            {
                "tests/a.py::TestFeature::test_x",
                "tests/a.py::TestFeature::test_y",
            },
            {group.test_display for group in report.failure_groups},
        )

    def test_file_folding_takes_precedence_over_class_folding(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::TestOne::test_x",
                    "fail",
                    "tb-1",
                    "Same cause",
                ),
                _row(
                    "row-2",
                    "tests/a.py::TestOne::test_y",
                    "fail",
                    "tb-2",
                    "Same cause",
                ),
                _row(
                    "row-3",
                    "tests/a.py::TestTwo::test_z",
                    "fail",
                    "tb-3",
                    "Same cause",
                ),
            ],
            {},
        )

        self.assertEqual("tests/a.py", report.failure_groups[0].test_display)

    def test_skip_uses_same_file_and_partial_parameter_folding(self) -> None:
        file_report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_x",
                    "skipped",
                    "tb-1",
                    "Same skip",
                ),
                _row(
                    "row-2",
                    "tests/a.py::test_y",
                    "skipped",
                    "tb-2",
                    "Same skip",
                ),
            ],
            {},
        )
        partial_report = _report(
            [
                _row(
                    "row-1",
                    "tests/b.py::test_x[a]",
                    "skipped",
                    "tb-1",
                    "Same skip",
                ),
                _row(
                    "row-2",
                    "tests/b.py::test_x[b]",
                    "skipped",
                    "tb-2",
                    "Same skip",
                ),
                _row("row-3", "tests/b.py::test_x[c]", "pass", "tb-3"),
            ],
            {},
        )

        self.assertEqual("tests/a.py", file_report.skip_groups[0].test_display)
        self.assertEqual(
            "tests/b.py::test_x",
            partial_report.skip_groups[0].test_display,
        )

    def test_skip_uses_class_folding(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::TestSkip::test_x",
                    "skipped",
                    "tb-1",
                    "Same skip",
                ),
                _row(
                    "row-2",
                    "tests/a.py::TestSkip::test_y",
                    "skipped",
                    "tb-2",
                    "Same skip",
                ),
                _row(
                    "row-3",
                    "tests/a.py::TestOther::test_z",
                    "pass",
                    "tb-3",
                ),
            ],
            {},
        )

        self.assertEqual(
            "tests/a.py::TestSkip",
            report.skip_groups[0].test_display,
        )

    def test_internal_skip_ticket_folds_parameters_to_test_case(self) -> None:
        ticket = "https://redmine.mellanox.com/issues/5154543"
        report = _report(
            [
                _row(
                    "row-1",
                    "hash/test_generic_hash.py::test_reboot"
                    "[CRC_CCITT-VLAN_ID-ipv4-None-None-fast]",
                    "skipped",
                    "tb-1",
                    "Fast reboot is unsupported " + ticket,
                ),
                _row(
                    "row-2",
                    "hash/test_generic_hash.py::test_reboot"
                    "[CRC_CCITT-VLAN_ID-ipv4-None-None-warm]",
                    "skipped",
                    "tb-2",
                    "Warm reboot is unsupported " + ticket,
                ),
            ],
            {},
        )

        self.assertEqual(1, len(report.skip_groups))
        group = report.skip_groups[0]
        self.assertEqual(
            "hash/test_generic_hash.py::test_reboot",
            group.test_display,
        )
        self.assertEqual(["tb-1", "tb-2"], group.testbeds)
        self.assertEqual([ticket], group.redmine_urls)

    def test_ticket_scope_does_not_absorb_plain_text_cause(
        self,
    ) -> None:
        ticket = "https://redmine.mellanox.com/issues/123"
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_a",
                    "fail",
                    "tb-1",
                    "Shared cause",
                ),
                _row(
                    "row-2",
                    "tests/b.py::test_b",
                    "fail",
                    "tb-2",
                    "Shared cause " + ticket,
                ),
                _row(
                    "row-3",
                    "tests/c.py::test_c",
                    "fail",
                    "tb-3",
                    "Different wording " + ticket,
                ),
            ],
            {},
        )

        self.assertEqual(2, len(report.failure_groups))
        self.assertEqual(
            {
                "tests/a.py::test_a",
                "tests/b.py::test_b\ntests/c.py::test_c",
            },
            {group.test_display for group in report.failure_groups},
        )
        self.assertEqual(
            1,
            sum(ticket in group.redmine_urls for group in report.failure_groups),
        )

    def test_skip_root_cause_merges_across_files_without_crossing_causes(
        self,
    ) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::TestA::test_one",
                    "skipped",
                    "tb-1",
                    "Shared skip cause",
                ),
                _row(
                    "row-2",
                    "tests/b.py::TestB::test_two",
                    "skipped",
                    "tb-2",
                    "Shared skip cause",
                ),
                _row(
                    "row-3",
                    "tests/c.py::TestC::test_three",
                    "skipped",
                    "tb-3",
                    "Different skip cause",
                ),
            ],
            {},
        )

        self.assertEqual(2, len(report.skip_groups))
        shared = next(
            group for group in report.skip_groups
            if set(group.member_ids) == {"row-1", "row-2"}
        )
        self.assertEqual(
            (
                "tests/a.py::TestA::test_one\n"
                "tests/b.py::TestB::test_two"
            ),
            shared.test_display,
        )

    def test_excel_and_dashboard_text_or_github_ticket_can_match(self) -> None:
        issue = "https://github.com/sonic-net/sonic-mgmt/issues/9"
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_a",
                    "fail",
                    "tb-1",
                    "Dashboard cause",
                ),
                _row(
                    "row-2",
                    "tests/b.py::test_b",
                    "fail",
                    "tb-2",
                ),
                _row(
                    "row-3",
                    "tests/c.py::test_c",
                    "fail",
                    "tb-3",
                    issue,
                ),
            ],
            {
                "row-2": "Dashboard cause\n" + issue,
            },
        )

        self.assertEqual(2, len(report.failure_groups))
        self.assertEqual(
            1,
            sum(
                group.internal_comments.count(issue)
                for group in report.failure_groups
            ),
        )

    def test_same_ticket_uses_file_local_parameter_class_and_file_folding(
        self,
    ) -> None:
        ticket = "https://redmine.mellanox.com/issues/123"
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::TestOne::test_x[a]",
                    "fail",
                    "tb-1",
                    "Cause A " + ticket,
                ),
                _row(
                    "row-2",
                    "tests/a.py::TestOne::test_x[b]",
                    "fail",
                    "tb-2",
                    "Cause B " + ticket,
                ),
                _row(
                    "row-3",
                    "tests/b.py::TestTwo::test_y",
                    "fail",
                    "tb-3",
                    "Cause C " + ticket,
                ),
                _row(
                    "row-4",
                    "tests/b.py::TestTwo::test_z",
                    "fail",
                    "tb-4",
                    "Cause D " + ticket,
                ),
            ],
            {},
        )

        self.assertEqual(1, len(report.failure_groups))
        group = report.failure_groups[0]
        self.assertEqual(
            (
                "tests/a.py::TestOne::test_x\n"
                "tests/b.py::TestTwo"
            ),
            group.test_display,
        )
        self.assertEqual([ticket], group.ticket_refs)
        self.assertEqual([ticket], group.redmine_urls)

    def test_cross_file_root_cause_folds_each_file_independently(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::TestOne::test_x",
                    "fail",
                    "tb-1",
                    "Shared root cause",
                ),
                _row(
                    "row-2",
                    "tests/a.py::TestOne::test_y",
                    "fail",
                    "tb-2",
                    "Shared root cause",
                ),
                _row(
                    "row-3",
                    "tests/b.py::TestTwo::test_x",
                    "fail",
                    "tb-3",
                    "Shared root cause",
                ),
                _row(
                    "row-4",
                    "tests/b.py::TestThree::test_y",
                    "fail",
                    "tb-4",
                    "Shared root cause",
                ),
            ],
            {},
        )

        self.assertEqual(1, len(report.failure_groups))
        self.assertEqual(
            "tests/a.py::TestOne\ntests/b.py",
            report.failure_groups[0].test_display,
        )

    def test_shared_dashboard_analysis_does_not_merge_different_excel_causes(
        self,
    ) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_a",
                    "fail",
                    "tb-1",
                    "Cause A",
                ),
                _row(
                    "row-2",
                    "tests/b.py::test_b",
                    "fail",
                    "tb-2",
                    "Cause B",
                ),
            ],
            {
                "row-1": "Shared dashboard analysis",
                "row-2": "Shared dashboard analysis",
            },
        )

        self.assertEqual(2, len(report.failure_groups))

    def test_empty_parameterized_causes_merge_but_different_causes_do_not(
        self,
    ) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_x[a]",
                    "fail",
                    "tb-1",
                    "Cause A",
                ),
                _row(
                    "row-2",
                    "tests/a.py::test_x[b]",
                    "fail",
                    "tb-2",
                    "Cause B",
                ),
                _row("row-3", "tests/a.py::test_x[c]", "fail", "tb-3"),
                _row("row-4", "tests/a.py::test_x[d]", "fail", "tb-4"),
            ],
            {},
        )

        self.assertEqual(3, len(report.failure_groups))
        self.assertIn(
            "tests/a.py::test_x",
            {group.test_display for group in report.failure_groups},
        )

    def test_empty_dashboard_parameters_merge_like_iface_namingmode(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "iface_namingmode/test_iface_namingmode.py::"
                    "TestShowInterfaces::test_show_interfaces_portchannel"
                    "[alias-msn4700-1288]",
                    "fail",
                    "tb-1",
                ),
                _row(
                    "row-2",
                    "iface_namingmode/test_iface_namingmode.py::"
                    "TestShowInterfaces::test_show_interfaces_portchannel"
                    "[default-msn4700-1288]",
                    "fail",
                    "tb-2",
                ),
            ],
            {},
        )

        self.assertEqual(1, len(report.failure_groups))
        self.assertEqual(
            "iface_namingmode/test_iface_namingmode.py::"
            "TestShowInterfaces::test_show_interfaces_portchannel",
            report.failure_groups[0].test_display,
        )

    def test_dashboard_status_does_not_split_same_root_cause(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_x[a]",
                    "fail",
                    "tb-1",
                    "Same cause",
                ),
                _row(
                    "row-2",
                    "tests/a.py::test_x[b]",
                    "fail",
                    "tb-2",
                    "Same cause",
                ),
            ],
            {},
            {
                "row-1": "no_analysis",
                "row-2": "not_found",
            },
        )

        self.assertEqual(1, len(report.failure_groups))
        self.assertEqual(
            "partial",
            report.failure_groups[0].internal_review_status,
        )

    def test_mixed_dashboard_status_is_reported_as_partial(self) -> None:
        rows = [
            _row(
                "row-1",
                "tests/a.py::test_x[a]",
                "fail",
                "tb-1",
            ),
            _row(
                "row-2",
                "tests/a.py::test_x[b]",
                "fail",
                "tb-2",
            ),
        ]
        workbook = WorkbookSnapshot(
            source_path=Path("/tmp/results.xlsx"),
            sheet_name="community_tests",
            header_row=1,
            selected_rows=rows,
            result_counts={},
            hardware_pairs=[],
            dashboard_statuses={
                "row-1": "no_analysis",
                "row-2": "not_found",
            },
        )

        self.assertEqual(
            "partial",
            _dashboard_group_status(rows, workbook),
        )

    def test_no_analysis_rows_keep_different_causes_separate(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "acl/test_acl.py::TestBasicAcl::test_source_ip[a]",
                    "fail",
                    "tb-1",
                    "Hidden Excel cause A",
                ),
                _row(
                    "row-2",
                    "acl/test_acl.py::TestIncrementalAcl::test_dest_ip[b]",
                    "fail",
                    "tb-2",
                    "Hidden Excel cause B",
                ),
                _row(
                    "row-3",
                    "acl/test_acl.py::TestAclWithReboot::test_protocol[c]",
                    "fail",
                    "tb-3",
                    "Hidden Excel cause C",
                ),
            ],
            {},
            {
                "row-1": "no_analysis",
                "row-2": "no_analysis",
                "row-3": "no_analysis",
            },
        )

        self.assertEqual(3, len(report.failure_groups))
        self.assertEqual(
            {
                "acl/test_acl.py::TestBasicAcl::test_source_ip[a]",
                "acl/test_acl.py::TestIncrementalAcl::test_dest_ip[b]",
                "acl/test_acl.py::TestAclWithReboot::test_protocol[c]",
            },
            {
                group.test_display
                for group in report.failure_groups
            },
        )
        self.assertEqual(
            {"no_analysis"},
            {
                group.internal_review_status
                for group in report.failure_groups
            },
        )

    def test_different_tickets_with_same_public_text_are_not_merged(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::TestFeature::test_x",
                    "fail",
                    "tb-1",
                    "Known issue https://redmine.mellanox.com/issues/111",
                ),
                _row(
                    "row-2",
                    "tests/a.py::TestFeature::test_y",
                    "fail",
                    "tb-2",
                    "Known issue https://redmine.mellanox.com/issues/222",
                ),
            ],
            {},
        )

        self.assertEqual(2, len(report.failure_groups))

    def test_plain_row_does_not_bridge_different_redmine_tickets(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::TestFeature::test_x",
                    "fail",
                    "tb-1",
                    "Known issue https://redmine.mellanox.com/issues/111",
                ),
                _row(
                    "row-2",
                    "tests/a.py::TestFeature::test_y",
                    "fail",
                    "tb-2",
                    "Known issue",
                ),
                _row(
                    "row-3",
                    "tests/a.py::TestFeature::test_z",
                    "fail",
                    "tb-3",
                    "Known issue https://redmine.mellanox.com/issues/222",
                ),
            ],
            {},
        )

        self.assertEqual(3, len(report.failure_groups))

    def test_ticket_bundles_do_not_duplicate_shared_test_rows(self) -> None:
        ticket_1 = "https://redmine.mellanox.com/issues/111"
        ticket_2 = "https://redmine.mellanox.com/issues/222"
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::TestFeature::test_x",
                    "fail",
                    "tb-1",
                    "Known issues {} {}".format(ticket_1, ticket_2),
                ),
                _row(
                    "row-2",
                    "tests/b.py::test_y",
                    "fail",
                    "tb-2",
                    "Related to " + ticket_1,
                ),
            ],
            {},
        )

        self.assertEqual(2, len(report.failure_groups))
        self.assertEqual(
            2,
            len({group.group_id for group in report.failure_groups}),
        )
        by_members = {
            tuple(group.member_ids): group
            for group in report.failure_groups
        }
        self.assertEqual(
            "tests/a.py::TestFeature::test_x",
            by_members[("row-1",)].test_display,
        )
        self.assertEqual(
            {ticket_1, ticket_2},
            set(by_members[("row-1",)].ticket_refs),
        )
        self.assertEqual(
            "tests/b.py::test_y",
            by_members[("row-2",)].test_display,
        )
        self.assertEqual(
            [ticket_1],
            by_members[("row-2",)].ticket_refs,
        )
        self.assertEqual(
            {"row-1", "row-2"},
            {
                member_id
                for group in report.failure_groups
                for member_id in group.member_ids
            },
        )
        self.assertEqual(
            2,
            sum(
                len(group.member_ids)
                for group in report.failure_groups
            ),
        )

    def test_redmine_url_variants_use_one_issue_identity(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::TestFeature::test_x",
                    "fail",
                    "tb-1",
                    "Cause A https://redmine.mellanox.com/issues/111",
                ),
                _row(
                    "row-2",
                    "tests/a.py::TestFeature::test_y",
                    "fail",
                    "tb-2",
                    "Cause B redmine.mellanox.com/issues/111",
                ),
            ],
            {},
        )

        self.assertEqual(1, len(report.failure_groups))
        self.assertEqual(
            ["https://redmine.mellanox.com/issues/111"],
            report.failure_groups[0].redmine_urls,
        )

    def test_merged_comments_use_visible_block_separators(self) -> None:
        ticket = "https://redmine.mellanox.com/issues/123"
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_a",
                    "fail",
                    "tb-1",
                    "Cause A " + ticket,
                ),
                _row(
                    "row-2",
                    "tests/a.py::test_b",
                    "fail",
                    "tb-2",
                    "Cause B " + ticket,
                ),
            ],
            {
                "row-1": "Analysis A " + ticket,
                "row-2": "Analysis B " + ticket,
            },
        )

        group = report.failure_groups[0]
        self.assertEqual(
            COMMENT_SEPARATOR.join(
                ("Analysis A", "Analysis B", "Cause A", "Cause B")
            ),
            group.comments,
        )
        self.assertEqual(
            2,
            group.internal_comments.count(COMMENT_SEPARATOR),
        )

    def test_failure_internal_comments_use_dashboard_analysis_not_error(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_a",
                    "fail",
                    "tb-1",
                    "AssertionError: raw failure output",
                ),
            ],
            {"row-1": "Engineer root-cause analysis"},
        )

        group = report.failure_groups[0]
        self.assertEqual("Engineer root-cause analysis", group.internal_comments)
        self.assertNotIn("AssertionError", group.internal_comments)

    def test_failure_without_dashboard_analysis_has_empty_internal_comments(self) -> None:
        report = _report(
            [
                _row(
                    "row-1",
                    "tests/a.py::test_a",
                    "fail",
                    "tb-1",
                    "AssertionError: raw failure output",
                ),
            ],
            {},
        )

        self.assertEqual("", report.failure_groups[0].internal_comments)


if __name__ == "__main__":
    unittest.main()
