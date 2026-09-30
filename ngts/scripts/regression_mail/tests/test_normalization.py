from __future__ import annotations

import unittest

from ngts.scripts.regression_mail.normalization import (
    COMMENT_SEPARATOR,
    extract_failure_message_redmine_urls,
    extract_redmine_urls,
    expand_explicit_public_work_item_ids,
    join_distinct_analysis_blocks,
    join_comment_blocks,
    ordered_unique,
    sanitize_public_comment,
    strip_public_issue_urls,
)


class NormalizationTest(unittest.TestCase):
    def test_expands_only_explicit_github_ids(self) -> None:
        self.assertEqual(
            [
                "https://github.com/sonic-net/sonic-buildimage/issues/29696",
                "https://github.com/sonic-net/sonic-mgmt/issues/27805",
                "https://github.com/sonic-net/sonic-mgmt/pull/27847",
            ],
            expand_explicit_public_work_item_ids(
                "sonic-buildimage issue #29696, sonic-mgmt PR #27847, GH#27805"
            ),
        )
        self.assertEqual([], expand_explicit_public_work_item_ids("known issue"))

    def test_ordered_unique_preserves_input_without_normalizing(self) -> None:
        self.assertEqual(
            ["B", "A", " A "],
            ordered_unique(["B", "", "A", "B", " A "]),
        )

    def test_join_comment_blocks_flattens_and_deduplicates(self) -> None:
        self.assertEqual(
            COMMENT_SEPARATOR.join(("first", "second", "third")),
            join_comment_blocks(
                (
                    COMMENT_SEPARATOR.join((" first ", "second")),
                    COMMENT_SEPARATOR.join(("first", " third ")),
                )
            ),
        )

    def test_failure_message_redmine_parser_rejects_hex_dump_offset(self) -> None:
        source = (
            "0040  52 6D E0 05 FE 0E 52 6D  ........Rm....Rm\n"
            "0050  E0 05 08 00 45 00 00 56"
        )

        self.assertEqual([], extract_failure_message_redmine_urls(source))
        self.assertEqual(
            ["https://redmine.mellanox.com/issues/123"],
            extract_failure_message_redmine_urls(
                source + "\nRoot cause tracked by RM ticket 123"
            ),
        )
        self.assertEqual(
            ["https://redmine.mellanox.com/issues/5154543"],
            extract_failure_message_redmine_urls(
                "Known failure (RM 5154543)"
            ),
        )

    def test_rich_redmine_summary_exposes_ticket_url(self) -> None:
        self.assertEqual(
            ["https://redmine.mellanox.com/issues/5100919"],
            extract_redmine_urls(
                "[SONIC - Design] Bug SW #5100919: Missing messages"
            ),
        )

    def test_distinct_analysis_blocks_preserve_different_operations(self) -> None:
        first = (
            "Cleanup operation create failed because the component exists."
        )
        second = (
            "Cleanup operation delete failed because the component exists."
        )

        self.assertEqual(
            COMMENT_SEPARATOR.join((first, second)),
            join_distinct_analysis_blocks((first, second)),
        )

    def test_distinct_analysis_blocks_keep_richer_duplicate_once(self) -> None:
        repeated = (
            "On the same hardware and topology, queued BMP updates temporarily "
            "increase bgpd memory and exceed the fixed threshold."
        )
        richer = repeated + " Tracking continues in the BMP email thread."

        self.assertEqual(
            richer,
            join_distinct_analysis_blocks((repeated, richer)),
        )

    def test_distinct_analysis_blocks_ignore_link_order(self) -> None:
        first_url = "https://git-nbu-sw.nvidia.com/r/c/project/+/123"
        second_url = "https://git-nbu-sw.nvidia.com/r/c/project/+/456"

        self.assertEqual(
            first_url + " | " + second_url,
            join_distinct_analysis_blocks(
                (
                    first_url + " | " + second_url,
                    second_url + " | " + first_url,
                )
            ),
        )

    def test_distinct_analysis_blocks_keep_different_link_sets(self) -> None:
        first_url = "https://git-nbu-sw.nvidia.com/r/c/project/+/123"
        second_url = "https://git-nbu-sw.nvidia.com/r/c/project/+/456"

        self.assertEqual(
            COMMENT_SEPARATOR.join(
                ("Fix is tracked by " + first_url, "Fix is tracked by " + second_url)
            ),
            join_distinct_analysis_blocks(
                ("Fix is tracked by " + first_url, "Fix is tracked by " + second_url)
            ),
        )

    def test_public_issue_condition_removes_dangling_boolean_syntax(self) -> None:
        url = "https://github.com/sonic-net/sonic-mgmt/issues/123"
        self.assertEqual(
            "asic_type == 'mellanox'",
            strip_public_issue_urls(
                "asic_type == 'mellanox' or ({} or and )".format(url)
            ),
        )
        self.assertEqual(
            (
                "(is_multi_asic == True) or "
                "topo_name in ['t1-isolated']"
            ),
            strip_public_issue_urls(
                "(is_multi_asic == True) or {} and "
                "topo_name in ['t1-isolated']".format(url)
            ),
        )
        self.assertEqual("", strip_public_issue_urls(url + " or "))

    def test_public_issue_condition_handles_prefix_issue_ids(self) -> None:
        issue_123 = "https://github.com/sonic-net/sonic-mgmt/issues/123"
        issue_1234 = issue_123 + "4"

        self.assertEqual(
            "topo_name == 't0'",
            strip_public_issue_urls(
                "{} or ({} and topo_name == 't0')".format(
                    issue_123,
                    issue_1234,
                )
            ),
        )

    def test_nvidia_sonic_public_url_is_retained_in_public_comment(self) -> None:
        url = "https://github.com/nvidia-sonic/sonic-mgmt/issues/123"
        self.assertEqual(url, sanitize_public_comment(url))


if __name__ == "__main__":
    unittest.main()
