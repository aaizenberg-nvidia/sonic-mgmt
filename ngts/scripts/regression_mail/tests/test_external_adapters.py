from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from ngts.scripts.regression_mail.ai import (
    AiCommentSummarizer,
    generalize_error_logs,
    sanitize_error_logs,
)
from ngts.scripts.regression_mail.git_history import GitHubClient
from ngts.scripts.regression_mail.models import PullRequest, SemanticGroup
from ngts.scripts.regression_mail.normalization import (
    extract_redmine_urls,
)
from ngts.scripts.regression_mail.redaction import (
    SetupRedactor,
    extract_setup_names,
)
from ngts.scripts.regression_mail.regression_api import (
    RegressionReportClient,
    _extract_coverage,
    _extract_pass_rate,
    _setup_maps,
)
from ngts.scripts.regression_mail.tests.helpers import (
    HttpResponse as _Response,
    ScriptedOpenAIClient,
    semantic_group,
)


class ExternalAdapterTest(unittest.TestCase):
    def test_redmine_reference_requires_explicit_redmine_prefix(self) -> None:
        self.assertEqual(
            ["https://redmine.mellanox.com/issues/123"],
            extract_redmine_urls("RM ticket 123"),
        )
        self.assertEqual([], extract_redmine_urls("GitHub issue 123"))
        self.assertEqual([], extract_redmine_urls("generic ticket 456"))

    def test_redaction_deduplicates_parameters_that_become_identical(self) -> None:
        redactor = SetupRedactor(
            {
                "private-a": "Unknown platform",
                "private-b": "Unknown platform",
                "private-c": "Unknown platform",
            }
        )

        self.assertEqual(
            (
                "tests/a.py::test_reboot"
                "[warm-private-a-default, warm-private-b-default, "
                "warm-private-c-default]"
            ),
            redactor.redact_test_display(
                "tests/a.py::test_reboot"
                "[warm-private-a-default, warm-private-b-default, "
                "warm-private-c-default]"
            ),
        )

    def test_redaction_preserves_explicit_redmine_system_type(self) -> None:
        redactor = SetupRedactor(
            {"ACS-MSN2700": "Unknown platform"},
            {"ACS-MSN2700": "Unknown platform"},
        )

        self.assertEqual(
            "ACS-MSN2700",
            redactor.redact_testbed("ACS-MSN2700"),
        )
        self.assertEqual(
            "",
            redactor.redact_testbed("Unknown platform"),
        )

    def test_ai_summaries_are_batched_strict_and_desensitized(self) -> None:
        def response(**kwargs):
            payload = json.loads(kwargs["input"][1]["content"])
            return json.dumps({
                item["group_id"]: (
                    "Failure on secret-setup; Redmine ticket 5154543 "
                    "redmine.mellanox.com/issues/5154543 #5154543"
                )
                for item in payload
            })

        client = ScriptedOpenAIClient(response)
        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            batch_size=2,
            client_factory=lambda **kwargs: client,
        )
        groups = [
            semantic_group(
                group_id="group-{}".format(index),
                member_ids=["row-{}".format(index)],
                test_display="tests/a.py::test_{}".format(index),
                testbeds=["SN5600"],
                comments="public",
                internal_comments=(
                    "Failure on secret-setup "
                    "https://redmine.mellanox.com/issues/5154543"
                ),
                redmine_urls=[
                    "https://redmine.mellanox.com/issues/5154543"
                ],
            )
            for index in range(3)
        ]

        summaries = summarizer.summarize(
            groups,
            SetupRedactor({"secret-setup": "SN5600"}),
        )

        self.assertEqual(2, len(client.calls))
        self.assertTrue(
            all(call["max_output_tokens"] == 1024 for call in client.calls)
        )
        self.assertTrue(all("secret-setup" not in value for value in summaries.values()))
        self.assertTrue(all("5154543" not in value for value in summaries.values()))
        self.assertTrue(all("redmine.mellanox.com" not in value for value in summaries.values()))

    def test_ai_client_retries_transient_service_failures(self) -> None:
        captured = {}
        client = ScriptedOpenAIClient(
            lambda **kwargs: json.dumps({"group": "summary"})
        )

        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: captured.update(kwargs) or client,
        )
        summarizer.summarize(
            [semantic_group(group_id="group")],
            SetupRedactor({}),
        )

        self.assertEqual(5, captured["max_retries"])

    def test_ai_reviews_only_supplied_redmine_github_candidates(self) -> None:
        issue_url = (
            "https://github.com/sonic-net/sonic-buildimage/issues/123"
        )
        pr_url = (
            "https://github.com/sonic-net/sonic-buildimage/pull/456"
        )

        def response(**kwargs):
            payload = json.loads(kwargs["input"][1]["content"])
            self.assertIn(
                "concrete causal chain",
                kwargs["input"][0]["content"],
            )
            self.assertNotIn("secret-setup", kwargs["input"][1]["content"])
            self.assertNotIn("5154543", kwargs["input"][1]["content"])
            return json.dumps({
                item["ticket_id"]: "github_pr:" + pr_url
                for item in payload
            })

        client = ScriptedOpenAIClient(response)
        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: client,
        )
        ticket = "https://redmine.mellanox.com/issues/5154543"
        no_link_ticket = "https://redmine.mellanox.com/issues/5154544"

        decisions = summarizer.review_redmine_links(
            {
                ticket: {
                    "subject": "RM ticket 5154543 on secret-setup",
                    "evidence": (
                        "Failure on secret-setup; Redmine #5154543. " +
                        issue_url +
                        " fixed by " +
                        pr_url
                    ),
                    "current_failure_evidence": (
                        "ERR service failed on secret-setup"
                    ),
                    "github_issue_urls": [issue_url],
                    "github_pr_urls": [pr_url],
                    "github_link_details": {
                        issue_url: {
                            "kind": "issues",
                            "title": "Service failure",
                            "body": "The service fails during startup",
                            "state": "open",
                            "labels": ["bug"],
                        },
                        pr_url: {
                            "kind": "pull",
                            "title": "Fix service startup",
                            "body": "Fixes the same startup failure",
                            "state": "open",
                            "merged": False,
                            "labels": [],
                        },
                    },
                },
                no_link_ticket: {
                    "subject": "Internal issue",
                    "evidence": "No public links",
                    "github_issue_urls": [],
                    "github_pr_urls": [],
                },
            },
            SetupRedactor({"secret-setup": "SN5600"}),
        )

        self.assertEqual(
            {
                ticket: "github_pr:" + pr_url,
                no_link_ticket: "none",
            },
            decisions,
        )
        self.assertEqual(1, len(client.calls))

    def test_ai_rejects_extra_or_missing_group_ids(self) -> None:
        response = SimpleNamespace(output_text='{"unexpected": "summary"}')
        client = SimpleNamespace(
            responses=SimpleNamespace(create=lambda **kwargs: response)
        )
        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: client,
        )
        group = SemanticGroup(
            group_id="expected",
            member_ids=["row"],
            test_display="tests/a.py::test_a",
            testbeds=["SN5600"],
            comments="public",
            internal_comments="internal",
        )

        with self.assertRaisesRegex(ValueError, "group IDs"):
            summarizer.summarize([group], SetupRedactor({}))

    def test_ai_classifies_semantic_failure_causes_across_files(self) -> None:
        calls = []

        class Responses:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                payload = json.loads(kwargs["input"][1]["content"])
                if "evidence_id" in payload[0]:
                    return SimpleNamespace(
                        output_text=json.dumps({
                            item["evidence_id"]: (
                                "service connection refused"
                                if item["evidence_id"].startswith(
                                    ("group-1:", "group-2:")
                                )
                                else "permission denied"
                            )
                            for item in payload
                        })
                    )
                return SimpleNamespace(
                    output_text=json.dumps({
                        item["group_id"]: (
                            "service connection refused"
                            if item["group_id"] in {"group-1", "group-2"}
                            else "permission denied"
                        )
                        for item in payload
                    })
                )

        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: SimpleNamespace(
                responses=Responses()
            ),
        )
        groups = [
            SemanticGroup(
                group_id="group-{}".format(index),
                member_ids=["row-{}".format(index)],
                test_display="tests/a.py::test_{}".format(index),
                testbeds=[],
                comments=comment,
            )
            for index, comment in (
                (1, "ERR service: connection failed"),
                (2, "ERROR service: connection refused"),
                (3, "ERR service: permission denied"),
            )
        ]

        result = summarizer.classify_failure_causes(
            groups,
            {
                group.group_id: "tests/{}.py".format(group.group_id)
                for group in groups
            },
            SetupRedactor({}),
        )

        self.assertEqual(
            result["group-1"],
            result["group-2"],
        )
        self.assertNotEqual(
            result["group-1"],
            result["group-3"],
        )
        self.assertIn(
            "same byte-identical key",
            calls[1]["input"][0]["content"],
        )
        self.assertEqual(
            {
                "tests/group-1.py",
                "tests/group-2.py",
                "tests/group-3.py",
            },
            {
                item["python_file"]
                for item in json.loads(calls[1]["input"][1]["content"])
            },
        )
        self.assertEqual(2, len(calls))

    def test_ai_retries_failure_evidence_with_non_string_values(self) -> None:
        calls = []
        evidence_attempts = 0

        class Responses:
            @staticmethod
            def create(**kwargs):
                nonlocal evidence_attempts
                calls.append(kwargs)
                payload = json.loads(kwargs["input"][1]["content"])
                if "evidence_id" in payload[0]:
                    evidence_attempts += 1
                    if evidence_attempts == 1:
                        return SimpleNamespace(
                            output_text=json.dumps({
                                item["evidence_id"]: ["connection refused"]
                                for item in payload
                            })
                        )
                    return SimpleNamespace(
                        output_text=json.dumps({
                            item["evidence_id"]: "connection refused"
                            for item in payload
                        })
                    )
                return SimpleNamespace(
                    output_text=json.dumps({
                        item["group_id"]: "service connection refused"
                        for item in payload
                    })
                )

        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: SimpleNamespace(
                responses=Responses()
            ),
        )
        group = SemanticGroup(
            group_id="group-1",
            member_ids=["row-1"],
            test_display="tests/a.py::test_a",
            testbeds=[],
            comments="ERR service: failed with connection refused",
        )

        result = summarizer.classify_failure_causes(
            [group],
            {"group-1": "tests/a.py"},
            SetupRedactor({}),
        )

        self.assertEqual("service connection refused", result["group-1"])
        self.assertEqual(3, len(calls))
        self.assertIn(
            "previous response violated the JSON schema",
            calls[1]["input"][0]["content"],
        )

    def test_ai_keeps_insufficient_failure_evidence_unclassified(self) -> None:
        calls = []

        class Responses:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                payload = json.loads(kwargs["input"][1]["content"])
                self.assertIn("evidence_id", payload[0])
                return SimpleNamespace(output_text=json.dumps({
                    item["evidence_id"]: "insufficient evidence"
                    for item in payload
                }))

        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: SimpleNamespace(
                responses=Responses()
            ),
        )
        group = semantic_group(
            "group-1",
            comments="tests.common.errors.RunAnsibleModuleFail: rc = 1",
        )

        result = summarizer.classify_failure_causes(
            [group],
            {"group-1": "tests/a.py"},
            SetupRedactor({}),
        )

        self.assertEqual("unclassified:group-1", result["group-1"])
        self.assertEqual(1, len(calls))

    def test_matching_internal_change_is_a_strong_failure_cause(self) -> None:
        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("AI must not be called for a strong cause")
            ),
        )
        groups = [
            SemanticGroup(
                group_id="group-{}".format(index),
                member_ids=["row-{}".format(index)],
                test_display="tests/a.py::test_{}".format(index),
                testbeds=[],
                comments=comment,
                internal_comments=(
                    "test issue - https://git-nbu-sw.nvidia.com/r/c/"
                    "switchx/sonic/sonic-mgmt/+/344636"
                ),
            )
            for index, comment in (
                (1, "ERR service: first symptom"),
                (2, "ERROR service: second symptom"),
            )
        ]

        result = summarizer.classify_failure_causes(
            groups,
            {group.group_id: "tests/a.py" for group in groups},
            SetupRedactor({}),
        )

        self.assertEqual(
            result["group-1"],
            result["group-2"],
        )

    def test_redmine_reference_is_a_strong_failure_cause_boundary(self) -> None:
        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("AI must not be called for a strong cause")
            ),
        )
        groups = [
            SemanticGroup(
                group_id="group-{}".format(index),
                member_ids=["row-{}".format(index)],
                test_display="tests/a.py::test_{}".format(index),
                testbeds=[],
                comments="Similar public symptom",
                redmine_urls=[
                    "https://redmine.mellanox.com/issues/{}".format(ticket)
                ],
            )
            for index, ticket in ((1, 1001), (2, 1002))
        ]

        result = summarizer.classify_failure_causes(
            groups,
            {group.group_id: "tests/a.py" for group in groups},
            SetupRedactor({}),
        )

        self.assertNotEqual(result["group-1"], result["group-2"])

    def test_failure_ai_receives_untruncated_cause_evidence(self) -> None:
        payloads = []

        class Responses:
            @staticmethod
            def create(**kwargs):
                payload = json.loads(kwargs["input"][1]["content"])
                payloads.append(payload)
                if "evidence_id" in payload[0]:
                    return SimpleNamespace(output_text=json.dumps({
                        item["evidence_id"]: "terminal marker"
                        for item in payload
                    }))
                return SimpleNamespace(output_text=json.dumps({
                    item["group_id"]: "terminal marker"
                    for item in payload
                }))

        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: SimpleNamespace(
                responses=Responses()
            ),
        )
        group = SemanticGroup(
            group_id="group-1",
            member_ids=["row-1"],
            test_display="tests/a.py::test_a",
            testbeds=[],
            comments="short display...",
            cause_evidence=(
                "ERR service: failure " +
                ("detail " * 100) +
                "TERMINAL_MARKER"
            ),
        )

        summarizer.classify_failure_causes(
            [group],
            {"group-1": "tests/a.py"},
            SetupRedactor({}),
        )

        self.assertTrue(any(
            "TERMINAL_MARKER" in item.get("evidence", "")
            for payload in payloads
            for item in payload
        ))

    def test_ai_deduplicates_patterns_and_keeps_first_source_line(self) -> None:
        calls = []

        class Responses:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                payload = json.loads(kwargs["input"][1]["content"])
                result = {
                    item["line_id"]: item["line_id"]
                    for item in payload
                }
                return SimpleNamespace(output_text=json.dumps(result))

        client = SimpleNamespace(responses=Responses())
        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: client,
        )
        group = SemanticGroup(
            group_id="redmine-1",
            member_ids=["redmine-1"],
            test_display="test_service",
            testbeds=["SN5600"],
            comments=(
                "ERR dockerd: connection failed with 10.1.2.3:49\n"
                "ERR swss#orchagent: :- queryCapability: returned value "
                "268435456 is not allowed on ATTRIBUTE\n"
                "ERR swss#orchagent: :- queryCapability: returned value "
                "268435457 is not allowed on ATTRIBUTE"
            ),
            internal_comments=(
                "Redmine: https://redmine.mellanox.com/issues/5154543"
            ),
            redmine_urls=[
                "https://redmine.mellanox.com/issues/5154543"
            ],
        )

        result = summarizer.extract_error_logs(
            [group],
            SetupRedactor({}),
        )

        self.assertEqual(
            "ERR dockerd: "
            "connection failed with 10.1.2.3:49\n"
            "ERR swss#orchagent: :- queryCapability: returned value "
            "268435456 is not allowed on ATTRIBUTE",
            result["redmine-1"],
        )
        self.assertEqual(1, len(calls))

    def test_ai_keeps_every_distinct_error_pattern(self) -> None:
        patterns = [
            "ERR service: {} failure".format(name)
            for name in (
                "alpha",
                "bravo",
                "charlie",
                "delta",
                "echo",
                "foxtrot",
                "golf",
                "hotel",
                "india",
            )
        ]

        class Responses:
            @staticmethod
            def create(**kwargs):
                payload = json.loads(kwargs["input"][1]["content"])
                if "line_id" not in payload[0]:
                    result = {
                        item["group_id"]: "\n".join(patterns)
                        for item in payload
                    }
                else:
                    result = {
                        item["line_id"]: item["line_id"]
                        for item in payload
                    }
                return SimpleNamespace(output_text=json.dumps(result))

        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: SimpleNamespace(
                responses=Responses()
            ),
        )
        group = SemanticGroup(
            group_id="group-1",
            member_ids=["row-1"],
            test_display="test_1",
            testbeds=[],
            comments="\n".join(patterns),
        )

        result = summarizer.extract_error_logs(
            [group],
            SetupRedactor({}),
        )

        self.assertEqual(patterns, result["group-1"].splitlines())

    def test_ai_keeps_first_source_line_for_repeated_value_template(self) -> None:
        selected = [
            "ERR service: {} failure".format(name)
            for name in (
                "alpha",
                "bravo",
                "charlie",
                "delta",
                "echo",
                "foxtrot",
                "golf",
                "hotel",
            )
        ]

        class Responses:
            @staticmethod
            def create(**kwargs):
                payload = json.loads(kwargs["input"][1]["content"])
                if "line_id" not in payload[0]:
                    result = {
                        item["group_id"]: "\n".join(selected)
                        for item in payload
                    }
                else:
                    result = {
                        item["line_id"]: item["line_id"]
                        for item in payload
                    }
                return SimpleNamespace(output_text=json.dumps(result))

        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: SimpleNamespace(
                responses=Responses()
            ),
        )
        group = SemanticGroup(
            group_id="group-1",
            member_ids=["row-1"],
            test_display="test_1",
            testbeds=[],
            comments=(
                "ERR swss#orchagent: returned value 268435456 is not allowed\n"
                "ERR swss#orchagent: returned value 268435457 is not allowed"
            ),
        )

        result = summarizer.extract_error_logs(
            [group],
            SetupRedactor({}),
        )

        self.assertEqual(
            "ERR swss#orchagent: returned value 268435456 is not allowed",
            result["group-1"],
        )

    def test_deterministic_error_generalization_handles_trace_markers(self) -> None:
        value = (
            "ERR service: failed 10.1.2.3#012"
            "  File \"/usr/bin/service\", line 246, in <module>#012"
            "RuntimeError: unable to connect 10.1.2.4\n"
            "ERR service: failed 10.1.2.3#012"
            "  File \"/usr/bin/service\", line 246, in <module>#012"
            "RuntimeError: unable to connect 10.1.2.4"
        )

        self.assertEqual(
            "ERR service: failed x.x.x.x#012"
            " File \"/usr/bin/service\", line 246, in <module>#012"
            "RuntimeError: unable to connect x.x.x.x",
            generalize_error_logs(value),
        )

    def test_error_generalization_rejects_non_log_error_fragments(self) -> None:
        self.assertEqual(
            "ERR service: actual failure",
            generalize_error_logs(
                "ERR err/reports/123/index.html\n"
                "ERROR code 15\n"
                "ERROR 15\n"
                "ERR service: actual failure Expected behavior"
            ),
        )

    def test_error_log_sanitizer_preserves_non_setup_values(self) -> None:
        group = SemanticGroup(
            group_id="group-1",
            member_ids=["row-1"],
            test_display="test_1",
            testbeds=[],
            comments=(
                "ERR service: failure fixed by "
                "https://git-nbu-sw.nvidia.com/r/c/switchx/sonic/"
                "sonic-mgmt/+/344636"
            ),
        )

        self.assertEqual(
            (
                "ERR service: failure fixed by "
                "https://git-nbu-sw.nvidia.com/r/c/switchx/sonic/"
                "sonic-mgmt/+/344636"
            ),
            sanitize_error_logs(
                group.comments,
                group,
                SetupRedactor({}),
            ),
        )

    def test_error_log_sanitizer_redacts_only_discovered_setup_name(self) -> None:
        group = SemanticGroup(
            group_id="group-1",
            member_ids=["row-1"],
            test_display="test_1",
            testbeds=[],
        )
        source = (
            "ERR service: failed on slm-238 with 10.1.2.3:49, "
            "returned value 268435456"
        )
        setups = extract_setup_names(
            [source, "parameter r-sn5640-16 r-msn4700-01"]
        )

        self.assertEqual(["slm-238"], setups)
        self.assertEqual(
            (
                "ERR service: failed on slm-238 with 10.1.2.3:49, "
                "returned value 268435456"
            ),
            sanitize_error_logs(
                source,
                group,
                SetupRedactor({"slm-238": "Unknown platform"}),
            ),
        )

    def test_error_log_sanitizer_rejects_display_placeholders(self) -> None:
        group = SemanticGroup(
            group_id="group-1",
            member_ids=["row-1"],
            test_display="test_1",
            testbeds=[],
        )

        with self.assertRaisesRegex(ValueError, "display placeholder"):
            sanitize_error_logs(
                "ERR service: connection failed with x.x.x.x:<Port>",
                group,
                SetupRedactor({}),
            )

    def test_ai_rejects_forward_error_representative(self) -> None:
        class Responses:
            @staticmethod
            def create(**kwargs):
                payload = json.loads(kwargs["input"][1]["content"])
                return SimpleNamespace(output_text=json.dumps({
                    item["line_id"]: payload[-1]["line_id"]
                    for item in payload
                }))

        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: SimpleNamespace(
                responses=Responses()
            ),
        )
        group = SemanticGroup(
            group_id="group-1",
            member_ids=["row-1"],
            test_display="test_1",
            testbeds=[],
            comments=(
                "ERR service: first failure\n"
                "ERR service: second different failure"
            ),
        )

        with self.assertRaisesRegex(ValueError, "must not point forward"):
            summarizer.extract_error_logs(
                [group],
                SetupRedactor({}),
            )

    def test_ai_rejects_representative_from_another_ticket_group(self) -> None:
        class Responses:
            @staticmethod
            def create(**kwargs):
                payload = json.loads(kwargs["input"][1]["content"])
                first = payload[0]["line_id"]
                return SimpleNamespace(output_text=json.dumps({
                    item["line_id"]: (
                        first
                        if index
                        else item["line_id"]
                    )
                    for index, item in enumerate(payload)
                }))

        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: SimpleNamespace(
                responses=Responses()
            ),
        )
        groups = [
            SemanticGroup(
                group_id="redmine-{}".format(index),
                member_ids=["ticket-{}".format(index)],
                test_display="test_{}".format(index),
                testbeds=[],
                comments="ERR service: connection failed",
            )
            for index in (1, 2)
        ]

        with self.assertRaisesRegex(ValueError, "one ticket group"):
            summarizer.extract_error_logs(
                groups,
                SetupRedactor({}),
            )

    def test_ai_cannot_append_prose_to_an_error_sentence(self) -> None:
        class Responses:
            @staticmethod
            def create(**kwargs):
                payload = json.loads(kwargs["input"][1]["content"])
                if "line_id" not in payload[0]:
                    result = {
                        item["group_id"]: (
                            "ERR service: actual failure Expected behavior"
                        )
                        for item in payload
                    }
                else:
                    result = {
                        item["line_id"]: item["line_id"]
                        for item in payload
                    }
                return SimpleNamespace(output_text=json.dumps(result))

        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: SimpleNamespace(
                responses=Responses()
            ),
        )
        group = SemanticGroup(
            group_id="group-1",
            member_ids=["row-1"],
            test_display="test_1",
            testbeds=[],
            comments="ERR service: actual failure",
        )

        result = summarizer.extract_error_logs(
            [group],
            SetupRedactor({}),
        )

        self.assertEqual(
            "ERR service: actual failure",
            result["group-1"],
        )

    def test_ai_keeps_process_variants_separate_between_ticket_groups(self) -> None:
        class Responses:
            @staticmethod
            def create(**kwargs):
                payload = json.loads(kwargs["input"][1]["content"])
                if "line_id" not in payload[0]:
                    result = {
                        item["group_id"]: item["candidate_error_logs"]
                        for item in payload
                    }
                else:
                    result = {
                        item["line_id"]: item["line_id"]
                        for item in payload
                    }
                return SimpleNamespace(output_text=json.dumps(result))

        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: SimpleNamespace(
                responses=Responses()
            ),
        )
        groups = [
            SemanticGroup(
                group_id="group-{}".format(index),
                member_ids=["row-{}".format(index)],
                test_display="test_{}".format(index),
                testbeds=[],
                comments="ERR {}: connection failed".format(process),
            )
            for index, process in enumerate(("dockerd", "systemctl"))
        ]

        result = summarizer.extract_error_logs(
            groups,
            SetupRedactor({}),
        )

        self.assertEqual(
            {
                "ERR dockerd: connection failed",
                "ERR systemctl: connection failed",
            },
            set(result.values()),
        )

    def test_ai_preserves_a_constant_error_process_name(self) -> None:
        class Responses:
            @staticmethod
            def create(**kwargs):
                payload = json.loads(kwargs["input"][1]["content"])
                if "line_id" not in payload[0]:
                    result = {
                        item["group_id"]: item["candidate_error_logs"]
                        for item in payload
                    }
                else:
                    result = {
                        item["line_id"]: item["line_id"]
                        for item in payload
                    }
                return SimpleNamespace(output_text=json.dumps(result))

        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: SimpleNamespace(
                responses=Responses()
            ),
        )
        group = SemanticGroup(
            group_id="group-1",
            member_ids=["row-1"],
            test_display="test_1",
            testbeds=[],
            comments=(
                "ERR swss#orchagent: returned value 268435456 "
                "is not allowed"
            ),
        )

        result = summarizer.extract_error_logs(
            [group],
            SetupRedactor({}),
        )

        self.assertEqual(
            (
                "ERR swss#orchagent: returned value 268435456 "
                "is not allowed"
            ),
            result["group-1"],
        )

    def test_error_log_sensitive_identifiers_use_typed_placeholders(self) -> None:
        value = (
            "ERR service: peer 2001:db8::1 mac 00:11:22:33:44:55 "
            "request 123e4567-e89b-12d3-a456-426614174000 "
            "owner user@nvidia.com"
        )

        self.assertEqual(
            "ERR service: peer <IPv6> mac <MAC> request <ID> owner <User>",
            generalize_error_logs(value),
        )

    def test_returned_value_variants_keep_sentence_and_use_placeholder(
        self,
    ) -> None:
        value = "\n".join(
            "ERR swss#orchagent: :- queryAttributeEnumValuesCapability: "
            "returned value {} is not allowed on "
            "SAI_HOSTIF_TRAP_ATTR_TRAP_TYPE".format(number)
            for number in (
                268435456,
                268435457,
                268435463,
                268435464,
            )
        )

        self.assertEqual(
            "ERR swss#orchagent: :- queryAttributeEnumValuesCapability: "
            "returned value <ReturnedValue> is not allowed on "
            "SAI_HOSTIF_TRAP_ATTR_TRAP_TYPE",
            generalize_error_logs(value),
        )

    def test_error_logs_use_context_specific_placeholders(self) -> None:
        value = "\n".join(
            (
                "ERR systemctl: tac_connect_single: connection to "
                "10.1.2.3:49 failed: Network is unreachable",
                "ERR swss#orchagent: :- createEntry: Failed to start "
                "PFC Watchdog on port Ethernet109",
                "ERR syncd#SDK: [SAI_PORT.ERR] client_pid=20, "
                "./src/mlnx_sai_port.c[11151]- mlnx_lag_by_log_id: "
                "Failed lookup port config for lag by log id 42",
                "ERR dhcp_server#rsyslogd: omrelp: could not connect to "
                "remote server, librelp error 100 [v8.2504.0 try "
                "https://www.rsyslog.com/e/2353 ]",
            )
        )

        self.assertEqual(
            "\n".join(
                (
                    "ERR systemctl: tac_connect_single: connection to "
                    "x.x.x.x:<Port> failed: Network is unreachable",
                    "ERR swss#orchagent: :- createEntry: Failed to start "
                    "PFC Watchdog on port <Interface>",
                    "ERR syncd#SDK: [SAI_PORT.ERR] client_pid=<PID>, "
                    "./src/mlnx_sai_port.c[<Line>]- mlnx_lag_by_log_id: "
                    "Failed lookup port config for lag by log id <LAG_ID>",
                    "ERR dhcp_server#rsyslogd: omrelp: could not connect to "
                    "remote server, librelp error <ErrorCode> [<Version> try "
                    "https://www.rsyslog.com/e/<ErrorCode> ]",
                )
            ),
            generalize_error_logs(value),
        )

    def test_ai_rejects_duplicate_json_group_id(self) -> None:
        response = SimpleNamespace(
            output_text='{"expected": "first", "expected": "second"}'
        )
        client = SimpleNamespace(
            responses=SimpleNamespace(create=lambda **kwargs: response)
        )
        summarizer = AiCommentSummarizer(
            "secret",
            "https://inference-api.nvidia.com/v1",
            "openai/openai/gpt-5.6-sol",
            client_factory=lambda **kwargs: client,
        )
        group = SemanticGroup(
            group_id="expected",
            member_ids=["row"],
            test_display="tests/a.py::test_a",
            testbeds=["SN5600"],
            comments="public",
            internal_comments="internal",
        )

        with self.assertRaisesRegex(ValueError, "strict JSON"):
            summarizer.summarize([group], SetupRedactor({}))

    def test_testbed_redaction_preserves_topology_suffix(self) -> None:
        redactor = SetupRedactor(
            {"arc-switch1025_setup": "x86_64-mlnx_msn2700-r0"},
            {"arc-switch1025_setup": "x86_64-mlnx_msn2700-r0"},
        )

        self.assertEqual(
            "x86_64-mlnx_msn2700-r0-t1-lag",
            redactor.redact_testbed("arc-switch1025-t1-lag"),
        )
        simx_redactor = SetupRedactor(
            {
                "sonic_simx_r-moose-simx-34":
                    "x86_64-nvidia_sn5600_simx-r0"
            },
            {
                "sonic_simx_r-moose-simx-34":
                    "x86_64-nvidia_sn5600_simx-r0"
            },
        )
        self.assertEqual(
            "x86_64-nvidia_sn5600_simx-r0-ptf-any",
            simx_redactor.redact_testbed(
                "r-moose-simx-34-ptf-any"
            ),
        )
        self.assertEqual(
            "r-panther-23_setup",
            simx_redactor.redact_testbed("r-panther-23_setup"),
        )
        self.assertEqual(
            "x86_64-nvidia_sn5640-r0",
            simx_redactor.redact_testbed("x86_64-nvidia_sn5640-r0"),
        )

    def test_dashboard_metrics_follow_contract_denominator(self) -> None:
        self.assertEqual(
            75.0,
            _extract_pass_rate(
                {
                    "groups": [
                        {
                            "passed": 6,
                            "failed": 2,
                            "skipped": 100,
                            "rmSkipped": 50,
                        },
                        {"passed": 3, "failed": 1},
                    ]
                }
            ),
        )
        self.assertEqual(
            90.0,
            _extract_pass_rate(
                {
                    "groups": {
                        "Canonical": {"passed": 6, "failed": 1},
                        "Community": {"passed": 3, "failed": 0},
                    }
                }
            ),
        )
        self.assertEqual(
            88.5,
            _extract_coverage(
                {"coverage": {"totals": {"coverage": "88.5"}}}
            ),
        )
        self.assertEqual(
            96.5,
            _extract_coverage({"totals": {"coverage": "96.5"}}),
        )

    def test_dashboard_builds_testbed_mapping_from_collection(self) -> None:
        setup_platforms, setup_testbeds = _setup_maps(
            [
                {
                    "setupName": "private-setup",
                    "platform": "x86_64-platform-r0",
                    "topology": "",
                },
                {
                    "setupName": "private-setup",
                    "platform": "x86_64-platform-r0",
                    "topology": "t1-lag",
                },
            ]
        )

        self.assertEqual(
            "x86_64-platform-r0",
            setup_platforms["private-setup"],
        )
        self.assertEqual(
            "x86_64-platform-r0-t1-lag",
            setup_testbeds["private-setup"],
        )

    def test_dashboard_keeps_setup_when_platform_is_missing(self) -> None:
        setup_platforms, setup_testbeds = _setup_maps(
            [
                {
                    "setupName": "private-setup",
                    "platform": "Unknown platform",
                    "topology": "t1-lag",
                },
            ]
        )

        self.assertEqual("", setup_platforms["private-setup"])
        self.assertEqual(
            "private-setup",
            setup_testbeds["private-setup"],
        )

    def test_dashboard_rejects_duplicate_analysis_keys(self) -> None:
        collection = {"records": [{"id": "1"}]}
        overlay = {
            "records": [
                {"recordKey": "mars-1", "analysis": "first"},
                {"recordKey": "mars-1", "analysis": "second"},
            ]
        }

        with self.assertRaisesRegex(ValueError, "duplicate recordKey"):
            RegressionReportClient._join_analyses(collection, overlay)

    def test_dashboard_preserves_records_without_engineer_analysis(self) -> None:
        collection = {
            "records": [
                {
                    "id": "1",
                    "sessionId": "session",
                    "keyId": "key",
                    "name": "tests/a.py::test_a[param]",
                }
            ]
        }

        analyses = RegressionReportClient._join_analyses(
            collection,
            {"records": []},
        )

        self.assertEqual(1, len(analyses))
        self.assertEqual("", analyses[0].analysis)
        self.assertEqual("session", analyses[0].session_id)

    def test_dashboard_metrics_use_sonic_prefixed_exact_version(self) -> None:
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if "/api/failure-analysis?" in request.full_url:
                payload = {"rows": []}
            elif "/api/snapshot-summary?" in request.full_url:
                payload = {"groups": [{"passed": 9, "failed": 1}]}
            elif "/api/coverage?" in request.full_url:
                payload = {"totals": {"coverage": 96.5}}
            else:
                raise AssertionError(request.full_url)
            return _Response(json.dumps(payload).encode())

        snapshot = RegressionReportClient(
            "https://dashboard.invalid",
            opener=opener,
        ).fetch("202605_RC.70-deadbeef_Internal")

        self.assertEqual(96.5, snapshot.coverage)
        self.assertEqual(90.0, snapshot.pass_rate)
        metric_calls = [
            call
            for call in calls
            if "snapshot-summary" in call or "/coverage?" in call
        ]
        self.assertTrue(
            all(
                "version=SONiC.202605_RC.70-deadbeef_Internal" in call
                for call in metric_calls
            )
        )

    def test_dashboard_filters_overlay_by_exact_version(self) -> None:
        analyses = RegressionReportClient._analyses_from_records(
            [
                {
                    "switchVersion": "SONiC.202605_RC.83-deadbeef_Internal",
                    "marsSessionId": "123",
                    "testName": "tests/a.py::test_a[param]",
                    "analysis": "analysis",
                    "verdict": "verdict",
                    "rmUrl": "https://redmine.mellanox.com/issues/1",
                    "updatedAt": "2026-08-28 10:00:00",
                },
                {
                    "switchVersion": "SONiC.202605_RC.84-cafebabe_Internal",
                    "marsSessionId": "456",
                    "testName": "tests/b.py::test_b[param]",
                },
            ],
        )

        self.assertEqual(2, len(analyses))
        self.assertEqual("123", analyses[0].session_id)
        self.assertEqual("verdict", analyses[0].verdict)
        self.assertEqual("2026-08-28 10:00:00", analyses[0].updated_at)
        self.assertEqual(
            "202605_RC.83-deadbeef_Internal",
            analyses[0].switch_version,
        )

    def test_dashboard_rejects_invalid_count_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "not an integer"):
            _extract_pass_rate({"groups": [{"passed": "unknown", "failed": 1}]})

    def test_github_uses_vault_token_for_exact_pr(self) -> None:
        def opener(request, timeout):
            self.assertEqual("Bearer secret", request.headers["Authorization"])
            self.assertTrue(request.full_url.endswith("/pulls/7"))
            payload = {
                "html_url": "https://github.com/sonic-net/sonic-mgmt/pull/7",
                "title": "Fix deterministic report",
                "state": "open",
                "draft": False,
                "labels": [
                    {"name": "Request for 202605 branch"},
                    {"name": "Tested for 202605 branch"},
                ],
                "user": {"login": "zypgithub"},
                "base": {"ref": "202605"},
            }
            return _Response(json.dumps(payload).encode())

        result = GitHubClient(token="secret", opener=opener).get_mgmt_prs(
            ["https://github.com/sonic-net/sonic-mgmt/pull/7"]
        )

        self.assertEqual(1, len(result))
        self.assertEqual(
            "https://github.com/sonic-net/sonic-mgmt/pull/7",
            result[0].url,
        )
        self.assertFalse(result[0].draft)
        self.assertEqual(
            (
                "Request for 202605 branch",
                "Tested for 202605 branch",
            ),
            result[0].labels,
        )

    def test_github_fetches_image_pr_title(self) -> None:
        def opener(request, timeout):
            payload = {
                "html_url": "https://github.com/sonic-net/sonic-buildimage/pull/9",
                "title": "Fix image build",
                "state": "closed",
                "user": {"login": "image-owner"},
            }
            return _Response(json.dumps(payload).encode())

        result = GitHubClient(opener=opener).get_image_prs(
            ["https://github.com/sonic-net/sonic-buildimage/pull/9"]
        )

        self.assertEqual("Fix image build", result[0].title)

    def test_github_fetches_exact_issue_evidence_for_ai_review(self) -> None:
        issue_url = (
            "https://github.com/sonic-net/sonic-buildimage/issues/19"
        )

        def opener(request, timeout):
            self.assertTrue(request.full_url.endswith(
                "/repos/sonic-net/sonic-buildimage/issues/19"
            ))
            return _Response(json.dumps({
                "html_url": issue_url,
                "title": "Service startup failure",
                "body": "The service exits while loading configuration.",
                "state": "open",
                "labels": [{"name": "bug"}],
            }).encode())

        details = GitHubClient(opener=opener).inspect_issue_links(
            [issue_url]
        )

        self.assertEqual("Service startup failure", details[issue_url]["title"])
        self.assertEqual(["bug"], details[issue_url]["labels"])
        self.assertFalse(details[issue_url]["merged"])

    def test_rc_status_uses_release_tag_when_ref_is_empty(self) -> None:
        markdown = """
| Field | Value |
| --- | --- |
| Tag | 202605_RC.70 |
| RC branch | 202605_RC |
| Upstream base | deadbeef |
https://github.com/sonic-net/sonic-buildimage/pull/123
"""

        def opener(request, timeout):
            self.assertEqual("Bearer secret", request.headers["Authorization"])
            self.assertIn(
                "ref=202605_RC.70",
                request.full_url,
            )
            payload = {
                "content": base64.b64encode(markdown.encode()).decode()
            }
            return _Response(json.dumps(payload).encode())

        result = GitHubClient(token="secret", opener=opener).fetch_rc_status(
            "202605_RC.70-deadbeef_Internal"
        )
        self.assertEqual("202605_RC", result.image_branch)
        self.assertEqual("deadbeef", result.image_public_hash)
        self.assertEqual(
            ["https://github.com/sonic-net/sonic-buildimage/pull/123"],
            result.image_pr_urls,
        )

    def test_rc_status_supports_matching_repository_and_ref_override(self) -> None:
        markdown = """
- **Tag:** `202605_RC.70`
- **RC branch:** `202605_RC`
- **Upstream base:** `deadbeef`
"""

        def opener(request, timeout):
            self.assertIn(
                "/repos/zypgithub/sonic-mgmt/contents/RC_STATUS.md",
                request.full_url,
            )
            self.assertIn("ref=demo-rc70", request.full_url)
            return _Response(
                json.dumps(
                    {
                        "content": base64.b64encode(markdown.encode()).decode(),
                    }
                ).encode()
            )

        result = GitHubClient(
            opener=opener,
            rc_status_repository="zypgithub/sonic-mgmt",
            rc_status_ref="demo-rc70",
            rc_status_tag="202605_RC.70",
        ).fetch_rc_status("202605_RC.70-deadbeef_Internal")

        self.assertEqual("deadbeef", result.image_public_hash)

    def test_rc_status_rejects_cross_release_tag_override(self) -> None:
        client = GitHubClient(
            rc_status_repository="zypgithub/sonic-mgmt",
            rc_status_ref="demo-rc70",
            rc_status_tag="202605_RC.70",
            opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("mismatched override must fail before I/O")
            ),
        )

        with self.assertRaisesRegex(
            ValueError,
            "does not match requested release",
        ):
            client.fetch_rc_status(
                "202605_RC.76-deadbeef_Internal"
            )

    def test_github_lists_open_mgmt_prs_by_author(self) -> None:
        def opener(request, timeout):
            self.assertIn(
                "/repos/sonic-net/sonic-mgmt/pulls?state=open&per_page=100&page=1",
                request.full_url,
            )
            self.assertEqual("Bearer token", request.headers["Authorization"])
            return _Response(
                json.dumps(
                    [
                        {
                            "html_url": "https://github.com/sonic-net/sonic-mgmt/pull/123",
                            "title": "Fix regression",
                            "state": "open",
                            "draft": False,
                            "labels": [
                                {"name": "Request for 202605 branch"}
                            ],
                            "user": {"login": "zypgithub"},
                        },
                        {
                            "html_url": "https://github.com/sonic-net/sonic-mgmt/pull/456",
                            "title": "Other change",
                            "state": "open",
                            "user": {"login": "someone-else"},
                        },
                        {
                            "html_url": "https://github.com/sonic-net/sonic-mgmt/pull/789",
                            "title": "Draft regression fix",
                            "state": "open",
                            "draft": True,
                            "labels": [
                                {"name": "Request for 202605 branch"}
                            ],
                            "user": {"login": "ZypGitHub"},
                        },
                    ]
                ).encode()
            )

        result = GitHubClient(
            token="token",
            opener=opener,
        ).get_open_mgmt_prs_by_authors(["ZypGitHub"])

        self.assertEqual(
            [
                "https://github.com/sonic-net/sonic-mgmt/pull/123",
                "https://github.com/sonic-net/sonic-mgmt/pull/789",
            ],
            [pull_request.url for pull_request in result],
        )
        self.assertFalse(result[0].draft)
        self.assertTrue(result[1].draft)
        self.assertEqual(
            ("Request for 202605 branch",),
            result[0].labels,
        )


if __name__ == "__main__":
    unittest.main()
