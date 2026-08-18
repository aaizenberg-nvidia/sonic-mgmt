from __future__ import annotations

from collections import defaultdict
import json
import logging

from ngts.constants.constants import BugHandlerConst
from ngts.scripts.pbi import allure_helpers
from ngts.scripts.pbi.models import LABug, TestRuntime, TestResult

logger = logging.getLogger(__name__)


# **********************************************************************************************************************
# Log Analyzer Helper Functions
# **********************************************************************************************************************
def parse_test_runtimes(tests: list[TestResult], base_url: str) -> list[TestRuntime]:
    result: list[TestRuntime] = []

    for test in tests:
        setup_mins, test_body_mins, teardown_mins = allure_helpers.get_stage_runtime_mins(test, base_url)
        loganalyzer_runtime_mins = allure_helpers.get_allure_step_runtime_mins(test, base_url, "afterStages", "loganalyzer::0")

        bugs: list[LABug] = []
        handler_results = _collect_bug_handler_attachments(test, base_url)
        if handler_results:
            bugs_occurrences: defaultdict[str, int] = defaultdict(int)
            bug_handler_runtime_mins: defaultdict[str, float] = defaultdict(float)

            for entry in handler_results:
                bug_id = str(entry.get("bug_id", "")).strip()
                if not bug_id:
                    continue
                bugs_occurrences[bug_id] += 1
                bug_handler_runtime_mins[bug_id] += float(entry.get("runtime_mins", 0))

            bugs = [
                LABug(
                    bug_id=bug_id,
                    occurrences=occurrences,
                    bug_handler_runtime_mins=bug_handler_runtime_mins.get(bug_id),
                    test_url=test.test_url,
                )
                for bug_id, occurrences in bugs_occurrences.items()
            ]
            logger.info(
                "Parsed %d LA bug(s) for %s from %d attachment(s)",
                len(bugs),
                test.test_name,
                len(handler_results),
            )

        result.append(
            TestRuntime(
                test_name=test.test_name,
                test_url=test.test_url,
                test_runtime_mins=setup_mins + test_body_mins + teardown_mins,
                setup_mins=setup_mins,
                test_body_mins=test_body_mins,
                teardown_mins=teardown_mins,
                la_runtime_mins=loganalyzer_runtime_mins,
                bugs=bugs,
            )
        )

    return result


def _collect_bug_handler_attachments(test: TestResult, base_url: str) -> list[dict]:
    test_json = allure_helpers.fetch_test_json(test, base_url)

    out: list[dict] = []
    for stage in test_json.get("afterStages") or []:
        if "loganalyzer" not in (stage.get("name") or "").lower():
            continue
        _parse_bug_handler_attachments(stage.get("attachments") or [], base_url, out)
        _collect_bug_handler_attachments_from_steps(stage.get("steps") or [], base_url, out)
    return out


def _parse_bug_handler_attachments(attachments: list, base_url: str, out: list[dict]) -> None:
    for att in attachments:
        name = att.get("name") or ""
        if not name.startswith(BugHandlerConst.LA_BUG_HANDLER_ATTACHMENT_PREFIX):
            continue
        try:
            payload = json.loads(allure_helpers.fetch_attachment(base_url, att["source"]))
        except json.JSONDecodeError:
            logger.warning("Failed to parse LA bug handler attachment %s", name)
            continue
        bug_id = str(payload.get("bug_id", "") or "").strip() if isinstance(payload, dict) else ""
        if bug_id:
            payload["bug_id"] = bug_id
            out.append(payload)


def _collect_bug_handler_attachments_from_steps(steps: list, base_url: str, out: list[dict]) -> None:
    for step in steps:
        _parse_bug_handler_attachments(step.get("attachments") or [], base_url, out)
        _collect_bug_handler_attachments_from_steps(step.get("steps") or [], base_url, out)
