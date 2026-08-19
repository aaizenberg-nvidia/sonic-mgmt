from __future__ import annotations

from argparse import Namespace
import dataclasses
import datetime
import logging

from ngts.scripts.allure_reporter import ALLURE_PROJECT_ID_SUFFIX

logger = logging.getLogger(__name__)

BUG_URL_PREFIX: str = "https://redmine.mellanox.com/issues/"
ALLURE_DOCKER_SERVICE = "allure-docker-service"


class Args(Namespace):
    setup_name: str
    session_id: str
    target_version: str
    branch: str
    dut_hwsku: str
    allure_server_addr: str

    @property
    def allure_project_id(self) -> str:
        return self.setup_name.replace("_", "-").lower() + ALLURE_PROJECT_ID_SUFFIX

    @property
    def allure_server_base_url(self) -> str:
        return f"{self.allure_server_addr}/{ALLURE_DOCKER_SERVICE}"


@dataclasses.dataclass
class TestResult:
    suite_path: str
    test_name: str
    status: str
    duration: float
    test_url: str
    test_uid: str = ""

    def to_db_values(self, setup_name: str, session_id: str, dut_hwsku: str, branch: str, target_version: str) -> str:
        escaped_url = self.test_url.replace("'", "''") if self.test_url is not None else ""
        return (
            f"('{setup_name}', '{self.suite_path}', '{self.test_name}', '{self.status}', '{session_id}', "
            f"'{datetime.date.today()!s}', '{dut_hwsku}', '{escaped_url}', '{branch}', '{target_version}', "
            f"'{self.duration}')"
        )


@dataclasses.dataclass
class LABug:
    bug_id: str
    occurrences: int
    bug_handler_runtime_mins: float
    test_url: str

    def to_db_values(
        self, test_name: str, session_id: str, setup_name: str, target_version: str, branch: str
    ) -> str:
        escaped_test_name = test_name.replace("'", "''") if test_name else ""
        escaped_bug_url = self.test_url.replace("'", "''") if self.test_url else ""
        return (
            f"('{escaped_test_name}', '{escaped_bug_url}', '{self.bug_id}', '{BUG_URL_PREFIX}{self.bug_id}', '{self.occurrences}', "
            f"{self.bug_handler_runtime_mins}, '{branch}', '{session_id}', '{setup_name}', "
            f"'{target_version}', '{datetime.date.today()!s}')"
        )


@dataclasses.dataclass
class TestRuntime:
    test_name: str
    test_url: str
    test_runtime_mins: float
    setup_mins: float
    test_body_mins: float
    teardown_mins: float
    la_runtime_mins: float
    bugs: list[LABug] = dataclasses.field(default_factory=list)

    def to_db_values(self, session_id: str, setup_name: str, target_version: str, branch: str) -> str:
        escaped_test_name = self.test_name.replace("'", "''") if self.test_name else ""
        escaped_test_url = self.test_url.replace("'", "''") if self.test_url else ""
        return (
            f"('{escaped_test_name}', '{escaped_test_url}', '{setup_name}', '{target_version}', '{session_id}', "
            f"{self.test_runtime_mins}, {self.setup_mins}, {self.test_body_mins}, {self.teardown_mins}, "
            f"{self.la_runtime_mins}, '{branch}', '{datetime.date.today()!s}')"
        )


@dataclasses.dataclass
class Summary:
    report_url: str
    pass_rate: str = "N/A"
    executed: str = "N/A"
    passed: int = 0
    failed: int = 0
    la_failed: int = 0
    broken: int = 0
    unknown: int = 0
    skipped: int = 0

    def to_db_values(self, setup_name: str, target_version: str, session_id: str, dut_hwsku: str, branch: str) -> str:
        return (
            f"('{setup_name}', '{self.pass_rate}', '{target_version}', '{self.executed}', '{session_id}', "
            f"'{self.report_url}', '{datetime.date.today()!s}', '{dut_hwsku}', '{branch}')"
        )

    @staticmethod
    def summarize_test_run(tests: list[TestResult], report_url: str) -> Summary:
        summary = Summary(report_url=report_url)

        for t in tests:
            status = t.status
            if status == "passed":
                summary.passed += 1
            elif status == "LA_failed":
                summary.la_failed += 1
            elif status == "skipped":
                summary.skipped += 1
            elif status == "failed":
                summary.failed += 1
            elif status == "broken":
                summary.broken += 1
            else:
                summary.unknown += 1

        total_fail = summary.failed + summary.la_failed + summary.broken + summary.unknown
        total_count_for_pass_rate = summary.passed + total_fail

        summary.pass_rate = (summary.passed / total_count_for_pass_rate) * 100.0 if total_count_for_pass_rate > 0 else 0.0
        summary.executed = (
            f"{summary.passed}/{summary.passed + summary.failed + summary.la_failed} "
            f"({summary.broken + summary.unknown + summary.skipped})"
        )

        logger.info(f"Passed = {summary.passed}")
        logger.info(f"Failed = {summary.failed}")
        logger.info(f"LA Failed = {summary.la_failed}")
        logger.info(f"Broken = {summary.broken}")
        logger.info(f"Unknown = {summary.unknown}")
        logger.info(f"Skipped = {summary.skipped}")
        logger.info(f"Pass Rate = {summary.pass_rate}%")
        logger.info(f"Executed = {summary.executed}")

        return summary
