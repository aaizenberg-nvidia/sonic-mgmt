from __future__ import annotations

import requests

from ngts.scripts.pbi.models import TestResult


# **********************************************************************************************************************
# Allure Helper Functions
# **********************************************************************************************************************
def fetch_test_json(test: TestResult, base_url: str) -> dict:
    test_resp = requests.get(f"{base_url}/data/test-cases/{test.test_uid}.json")
    test_resp.raise_for_status()
    return test_resp.json()


def _step_duration_ms(step: dict | None) -> int:
    if not step:
        return 0
    return (step.get("time") or {}).get("duration", 0)


def get_stage_runtime_mins(test: TestResult, base_url: str) -> tuple[float, float, float]:
    """Returns (setup, test_body, teardown) in minutes: beforeStages, testStage, afterStages."""
    test_json = fetch_test_json(test, base_url)

    before_ms = sum(_step_duration_ms(s) for s in (test_json.get("beforeStages") or []))
    test_stage = test_json.get("testStage") or {}
    body_ms = sum(_step_duration_ms(s) for s in (test_stage.get("steps") or []))
    after_ms = sum(_step_duration_ms(s) for s in (test_json.get("afterStages") or []))

    return before_ms / 60000.0, body_ms / 60000.0, after_ms / 60000.0


def get_allure_step_runtime_mins(test: TestResult, base_url: str, stage: str, step_name: str | None = None) -> float:
    test_json = fetch_test_json(test, base_url)

    steps = test_json.get(stage) or []
    if step_name is None:
        total_ms = sum(_step_duration_ms(s) for s in steps)
        return total_ms / 60000.0

    for step in steps:
        name_lower = (step.get("name") or "").lower()
        if step_name.lower() in name_lower:
            return _step_duration_ms(step) / 60000.0
    return 0.0


def parse_allure_step(test: TestResult, base_url: str, stage: str, step_name: str) -> dict | None:
    test_json = fetch_test_json(test, base_url)

    parsed_step = None
    for curr_stage in test_json.get(stage) or []:
        stage_name = (curr_stage.get("name") or "").lower()
        if step_name in stage_name:
            if curr_stage.get("attachments"):
                parsed_step = curr_stage.get("attachments")[0]
            break
    return parsed_step


def fetch_attachment(base_url: str, source: str) -> str:
    r = requests.get(f"{base_url}/data/attachments/{source}")
    r.raise_for_status()
    return r.text
