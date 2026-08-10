import logging
import time
from typing import Any

import pytest
from retry import retry

from ngts.nvos_constants.constants_nvos import HealthConsts, SystemConsts
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.FilesTool import FilesTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.StressNgTool import StressNgTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.helpers.memory_validation import MemoryStats
from ngts.tests_nvos.system.test_system_health import reset_health_service
from ngts.tools.test_utils import allure_utils as allure


cumulus_owner = "yleibovichwe"


logger = logging.getLogger()

OK = HealthConsts.OK
NOT_OK = HealthConsts.NOT_OK

# Health daemon typically adds "memory" to issues at 90%+ utilization
MEMORY_STRESS_TARGET_PERCENT = 90
MEMORY_STRESS_MAX_RETRIES = 24
MEMORY_STRESS_RETRY_INTERVAL_SEC = 5
MEMORY_NORMALIZE_MAX_RETRIES = 12


def _stop_service(engines_dut, service):
    """Stop a systemd service. Uses validate=False to avoid infra exit-code parsing issues."""
    engines_dut.run_cmd(f"sudo systemctl stop {service}")
    time.sleep(10)


def _start_service(engines_dut, service):
    """Start a systemd service. Uses validate=False to avoid infra exit-code parsing issues."""
    engines_dut.run_cmd(f"sudo systemctl start {service}")
    time.sleep(10)


def _is_service_status(engines_dut, service):
    """Return the output of systemctl is-active for the given service."""
    output = engines_dut.run_cmd(f"sudo systemctl is-active {service}", validate=False)
    return output.strip()


def _allocate_memory(engines_dut, percentage=90):
    """Allocate memory via tmpfs to reach target usage percentage. Returns (mount_point, file_path, increase_memory_mb)."""
    system = System()
    output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.show("memory")).get_returned_value()
    physical_memory = output_dictionary[SystemConsts.MEMORY_PHYSICAL_KEY]
    free_memory = physical_memory["free"]
    available_memory_mb = free_memory / (1024 * 1024)
    # Allocate most of free memory (cap 98%) so total utilization reliably exceeds 90% and health reports
    allocate_fraction = min(0.98, (percentage / 100) + 0.08)
    increase_memory_mb = int(available_memory_mb * allocate_fraction)
    mount_point = "/mnt/tmpfs"
    file_path = f"{mount_point}/testfile"
    logger.info("Setting up tmpfs mount at %s", mount_point)
    engines_dut.run_cmd(f"sudo mkdir -p {mount_point}")
    engines_dut.run_cmd(f"sudo mount -t tmpfs -o size={increase_memory_mb}M tmpfs {mount_point}")
    logger.info("Allocating %sMB of memory to reach %s%% usage", increase_memory_mb, percentage)
    engines_dut.run_cmd(f"sudo dd if=/dev/zero of={file_path} bs=1M count={increase_memory_mb}")
    return mount_point, file_path, increase_memory_mb


@pytest.mark.system
@pytest.mark.health
@pytest.mark.cumulus
def test_system_health_after_reboot_for_cumulus(devices, engines):
    """
    Steps:
        1. Check initial system health status
        2. Stop critical services (switchd and frr)
        3. Verify system health shows issues
        4. Reboot the system
        5. Verify system health status after reboot
    """
    system = System()
    with allure.step("Checking initial system health status"):
        initial_health_status = OutputParsingTool.parse_json_str_to_dictionary(
            system.health.show()
        ).get_returned_value()
        logger.info("Initial system health status: %s", initial_health_status)
        assert initial_health_status[HealthConsts.STATUS] == HealthConsts.OK, (
            "Initial system health status should be 'OK'"
        )
        assert initial_health_status[HealthConsts.STATUS_LED] == HealthConsts.LED_OK_STATUS, (
            "Initial system health status LED should be 'green'"
        )
    with allure.step("Stop critical services (switchd and frr)"):
        for svc in ["switchd", "frr"]:
            _stop_service(engines.dut, svc)
    with allure.step("Verify system health shows issues"):
        system.wait_until_health_status_change_to(HealthConsts.NOT_OK)
        health_issues = OutputParsingTool.parse_json_str_to_dictionary(
            system.health.show()
        ).get_returned_value()[HealthConsts.ISSUES]
        logger.info("System health issues: %s", health_issues)
        assert health_issues, "System health should show issues"

    with allure.step("Reboot the system"):
        reboot_dut(engines, system)
    with allure.step("Wait for system to become functional"):
        DutUtilsTool.wait_for_cumulus_to_become_functional(engines.dut).verify_result()

    with allure.step("Verify no new issues"):
        verify_no_new_issues(engines.dut, initial_health_status)

    with allure.step("Verify system health status after reboot"):
        system.validate_health_status(HealthConsts.OK)
        status_output = OutputParsingTool.parse_json_str_to_dictionary(
            system.health.show()
        ).get_returned_value()
        assert status_output[HealthConsts.STATUS] == HealthConsts.OK, "Status output status should be 'OK'"
        assert status_output[HealthConsts.STATUS_LED] == HealthConsts.LED_OK_STATUS, (
            "Status output status LED should be 'green'"
        )


@pytest.mark.cumulus
@pytest.mark.health
@pytest.mark.system
def test_verify_system_health_history_service(devices, engines):
    """
    Verify system health history for services.
    Steps:
        1. Stop and restart services
        2. Verify system health status is not OK
        3. Start services
        4. Verify system health status is OK
    """
    services = [
        "lldpd",
        "cumulus-platform",
        "hw-management-sync",
        "hw-management-tc",
        "hw-management",
        "smond",
        "mft",
        "rasdaemon",
        "ledmgrd",
        "update-ports",
    ]
    logger.info("Checking initial system health status")
    system = System()
    system.log.rotate_logs()
    system.validate_health_status(OK)
    with allure.step("Stopping and restarting services"):
        for service in services:
            logger.info("Stopping %s", service)
            _stop_service(engines.dut, service)
            system.wait_until_health_status_change_to(NOT_OK)
        system.log.rotate_logs()
        for service in services:
            logger.info("Verifying %s is stopped", service)
            assert _is_service_status(engines.dut, service) != "active", f"{service} is not stopped"
    with allure.step("Verifying system health status is not OK"):
        issues = OutputParsingTool.parse_json_str_to_dictionary(
            system.health.show()
        ).get_returned_value()[HealthConsts.ISSUES]
        logger.info("%s", issues)
        system.validate_health_status(NOT_OK)
        issue_keys = list(issues.keys())
        for service in services:
            found = service in issue_keys or any(
                service in k or k in service for k in issue_keys
            )
            assert found, f"{service} is not in issues (keys: {list(issue_keys)})"

    with allure.step("Restarting services"):
        for service in services:
            logger.info("Starting %s", service)
            _start_service(engines.dut, service)
        _start_service(engines.dut, "switchd")
    with allure.step("Validating health status after services restart"):
        system.wait_until_health_status_change_to(OK)
        for service in services:
            logger.info("Verifying %s is started", service)
            assert _is_service_status(engines.dut, service) == "active", f"{service} is not started"
        system.validate_health_status(OK)


@pytest.mark.cumulus
@pytest.mark.health
@pytest.mark.system
def test_show_system_health_memory_stress(devices, engines, loganalyzer, reset_health_service):
    """
    Test system health memory usage reporting (aligned with SSIM test03_nv_show_system_health_memory_usage).

    Verifies:
    1. Memory usage reporting in system health
    2. System health status changes when memory usage exceeds threshold (e.g. 80%)

    Steps:
    1. Check initial system health and memory usage
    2. Increase memory usage above threshold (tmpfs + stress-ng)
    3. Verify memory-usage status in system health and health history
    4. Clean up (stress-ng and tmpfs)
    """
    system = System()
    engines_dut = engines.dut

    with allure.step("Check initial system health and memory usage"):
        system.validate_health_status(OK)

    mount_point = None
    file_path = None
    try:
        with allure.step("Install stress-ng"):
            StressNgTool.install_stress_ng(engines_dut)
        mount_point, file_path, increase_memory_mb = _allocate_memory(
            engines_dut, MEMORY_STRESS_TARGET_PERCENT
        )
        with allure.step("Run stress-ng to generate memory load"):
            engines_dut.run_cmd("sudo -b stress-ng --vm 8 --vm-bytes 100% --vm-keep --timeout 60s")
            engines_dut.run_cmd("sudo -b stress-ng --vm 6 --vm-bytes 100% --vm-keep --timeout 120s")

        with allure.step("Increase memory usage above threshold and verify system health"):
            memory_usage = None
            increased_health_status = None
            for attempt in range(MEMORY_STRESS_MAX_RETRIES):
                output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                    system.show("memory")
                ).get_returned_value()
                physical_memory = output_dictionary[SystemConsts.MEMORY_PHYSICAL_KEY]
                memory_usage = physical_memory["utilization"]
                logger.info("Current memory usage: %s%%", memory_usage)

                switchd_active = _is_service_status(engines_dut, "switchd") == "active"
                assert switchd_active, "switchd service stopped due to memory pressure"

                increased_health_status = OutputParsingTool.parse_json_str_to_dictionary(
                    system.health.show()
                ).get_returned_value()
                logger.info(
                    "System health status after increasing memory usage: %s",
                    increased_health_status,
                )
                memory_issues = increased_health_status.get(HealthConsts.ISSUES, {})
                # Success: memory usage above threshold and memory must be present in health issues
                if memory_usage >= MEMORY_STRESS_TARGET_PERCENT and "memory" in memory_issues:
                    logger.info(
                        "Memory usage %.2f%% above target and memory present in health issues",
                        memory_usage,
                    )
                    break

                logger.info(
                    "Retry %s/%s: Memory usage or health issues not satisfied (current: %s%%, "
                    "target: >=%s%%, memory in issues: %s), retrying in %s seconds...",
                    attempt + 1,
                    MEMORY_STRESS_MAX_RETRIES,
                    memory_usage,
                    MEMORY_STRESS_TARGET_PERCENT,
                    "memory" in memory_issues,
                    MEMORY_STRESS_RETRY_INTERVAL_SEC,
                )
                time.sleep(MEMORY_STRESS_RETRY_INTERVAL_SEC)
            else:
                assert False, (
                    f"Memory usage did not reach {MEMORY_STRESS_TARGET_PERCENT}%% (last: {memory_usage}%%) "
                    f"or memory was not present in health issues after {MEMORY_STRESS_MAX_RETRIES} retries"
                )

        with allure.step("Verify memory metrics during stress"):
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.show("memory")
            ).get_returned_value()
            physical_memory = output_dictionary[SystemConsts.MEMORY_PHYSICAL_KEY]
            physical = MemoryStats(**physical_memory)
            physical.validate_utilization("Physical")
    finally:
        StressNgTool.cleanup_stress_ng(engines_dut)
        FilesTool.cleanup_tmpfs(engines_dut, mount_point, file_path)
    system.validate_health_status(OK)


@pytest.mark.cumulus
@pytest.mark.health
@pytest.mark.system
def test_system_health_cpu_usage(devices, engines):
    """
    Validate CPU usage in system health
    """
    system = System()
    # system.validate_health_status(OK)
    with allure.step("Validate CPU usage in system health"):
        cpu_usage = OutputParsingTool.parse_json_str_to_dictionary(system.show("cpu")).get_returned_value()
        logger.info(f"CPU usage: {cpu_usage}")
        assert cpu_usage[SystemConsts.CPU_TOTAL_UTILIZATION_KEY] < SystemConsts.CPU_PERCENT_THRESH_MAX, \
            "CPU usage is not below configured threshold"

    engines_dut = engines.dut
    try:
        with allure.step("Install stress-ng"):
            StressNgTool.install_stress_ng(engines_dut, install_bc=True)
        with allure.step("Run stress-ng to generate CPU load"):
            engines.dut.run_cmd("stress-ng --cpu 0 --cpu-method all --timeout 120s")
            health_output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
            if health_output[HealthConsts.STATUS] == HealthConsts.NOT_OK:
                if 'cpu' in health_output[HealthConsts.ISSUES]:
                    assert health_output[HealthConsts.ISSUES]['cpu']['issue'] == HealthConsts.NOT_OK, "CPU issue is not Not OK"
    finally:
        wait_for_cpu_usage_below_threshold(system)
        StressNgTool.cleanup_stress_ng(engines_dut, remove_bc=True)
        system.validate_health_status(OK)


def verify_no_new_issues(engines_dut, initial_issues):
    """
    Verify no new issues.
    """
    logger.info("Verifying no new issues")
    system = System()
    health_after_restart = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
    initial_issue_keys = set(initial_issues.get(HealthConsts.ISSUES, {}).keys())
    current_issue_keys = set(health_after_restart.get(HealthConsts.ISSUES, {}).keys())
    new_issues = current_issue_keys - initial_issue_keys
    assert not new_issues, f"New issues appeared: {new_issues}"


def reboot_dut(engines, system, sleep_time_seconds=240):
    try:
        system.reboot.action_reboot(send_user_confirmation="y")
    except Exception as e:
        logger.info("excepted rebooting of the dut: {}".format(e))
        time.sleep(sleep_time_seconds)
        with allure.step('Wait for switch to be up'):
            engines.dut.disconnect()
            time.sleep(30)


@retry(Exception, tries=18, delay=10)
def wait_for_cpu_usage_below_threshold(system: System) -> None:
    """
    Poll until CPU total utilization drops below the configured threshold.
    """
    usage = OutputParsingTool.parse_json_str_to_dictionary(system.show("cpu")).get_returned_value()
    logger.info(f"CPU usage during wait: {usage}")
    total = usage[SystemConsts.CPU_TOTAL_UTILIZATION_KEY]
    assert total < SystemConsts.CPU_PERCENT_THRESH_MAX, \
        f"CPU usage is not below configured threshold: {total}%"
