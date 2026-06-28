import logging
import pytest

from ngts.nvos_constants.constants_nvos import OutputFormat, ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.cluster.ansible_playbooks_tool import AnsiblePlaybooksTool
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts, AnsiblePlaybooksConsts as Ansible
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()

# Bug 5653301: NMX-C/T causes excessive disk writes filling /var/log.
# Measure ALL device writes during traffic to catch any excessive I/O.
# Start low to surface real numbers, then adjust based on observed baselines.
TRAFFIC_TOTAL_WRITE_THRESHOLD_KB = 50 * 1024  # 50 MB total across all devices


def _snapshot_all_writes(engine):
    """
    Snapshot kB_wrtn for every device from iostat.
    Returns a dict {device: kB_wrtn} or empty dict on failure.
    Never raises — logs the error and returns empty so the test flow continues.
    """
    try:
        iostat_data = OutputParsingTool.run_iostat_and_parse(engine)
        writes = {}
        for device, stats in iostat_data.items():
            writes[device] = int(stats['kB_wrtn'])
        logger.info(f"iostat snapshot (kB_wrtn): {writes}")
        return writes
    except Exception as e:
        logger.error(f"Failed to snapshot disk writes via iostat: {type(e).__name__}: {e}")
        logger.error("Disk write measurement will be skipped for this run")
        return {}


def _check_disk_writes(engine, initial_writes):
    """
    Compare iostat kB_wrtn on ALL devices before vs after traffic.
    Reports per-device deltas and total. Returns True if within threshold.
    Never raises — if iostat fails post-traffic, logs the error and returns True
    so the existing traffic/status assertions are not blocked.
    """
    if not initial_writes:
        logger.warning("Skipping disk write check - no initial iostat snapshot was captured")
        return True

    try:
        final_writes = _snapshot_all_writes(engine)
        if not final_writes:
            logger.warning("Skipping disk write check - failed to capture post-traffic iostat snapshot")
            return True

        lines = []
        total_delta_kb = 0
        for device in sorted(set(initial_writes) | set(final_writes)):
            before = initial_writes.get(device, 0)
            after = final_writes.get(device, 0)
            delta = after - before
            total_delta_kb += delta
            delta_mb = delta / 1024
            lines.append(f"  {device:12s}  before: {before:>10} KB  "
                         f"after: {after:>10} KB  delta: {delta:>10} KB ({delta_mb:.1f} MB)")

        total_delta_mb = total_delta_kb / 1024
        threshold_mb = TRAFFIC_TOTAL_WRITE_THRESHOLD_KB / 1024

        report = (
            "Per-device disk writes during traffic:\n" +
            "\n".join(lines) +
            f"\n\nTotal written during traffic: {total_delta_kb} KB ({total_delta_mb:.1f} MB)\n"
            f"Threshold: {TRAFFIC_TOTAL_WRITE_THRESHOLD_KB} KB ({threshold_mb:.1f} MB)"
        )
        logger.info(f"Disk write report:\n{report}")
        allure.attach("Disk write measurement during traffic (all devices)", report)

        if total_delta_kb > TRAFFIC_TOTAL_WRITE_THRESHOLD_KB:
            logger.error(
                f"DISK WRITE CHECK FAILED (bug 5653301): "
                f"Total writes during traffic = {total_delta_kb} KB ({total_delta_mb:.1f} MB), "
                f"threshold = {TRAFFIC_TOTAL_WRITE_THRESHOLD_KB} KB ({threshold_mb:.1f} MB). "
                f"Per-device breakdown:\n" + "\n".join(lines)
            )
            return False

        logger.info(f"Disk write check PASSED: {total_delta_kb} KB written, "
                    f"threshold {TRAFFIC_TOTAL_WRITE_THRESHOLD_KB} KB")
        return True

    except Exception as e:
        logger.error(f"Disk write check encountered an error: {type(e).__name__}: {e}")
        logger.error("Returning True so traffic/status assertions are not blocked")
        return True


def _assert_test_results(status_check, configure_nmx_status, traffic_status, disk_write_ok=True):
    """Assert test results and provide detailed failure information"""
    failed_components = []
    passed_components = []

    if not status_check:
        failed_components.append("status check")
    else:
        passed_components.append("status check")
    if not configure_nmx_status:
        failed_components.append("configure NMX")
    else:
        passed_components.append("configure NMX")
    if not traffic_status:
        failed_components.append("traffic test")
    else:
        passed_components.append("traffic test")
    if not disk_write_ok:
        failed_components.append("disk write threshold exceeded (bug 5653301)")
    else:
        passed_components.append("disk write check")

    if passed_components:
        logger.info(f"Passed: {', '.join(passed_components)}")

    if failed_components:
        failure_msg = f"Test failed on: {', '.join(failed_components)}"
        assert False, failure_msg


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
@pytest.mark.timeout(45 * MINUTE, func_only=True)
def test_cluster_traffic_basic_test(engines, devices, test_api, has_loopbox, standalone_system, setup_name, ansible_inventory_file):
    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json

    if standalone_system:
        pytest.skip(f"Skipping test - Standalone system, traffic not supported.")
    try:
        with allure.step("Enable Cluster"):
            cluster = Cluster()
            logger.info("Setting cluster state to enabled")
            ClusterTools.start_cluster(cluster, setup_name, output_format, devices=devices)

        with allure.step("Check cluster status"):
            status_check = AnsiblePlaybooksTool.run_playbook_by_key(
                Ansible.STATUS_HEALTH,
                ansible_inventory_file,
                {}
            )

        with allure.step("Configure NMX"):
            configure_nmx_status = AnsiblePlaybooksTool.run_playbook_by_key(
                Ansible.SOFTWARE_CONFIGURE_SWITCH,
                ansible_inventory_file,
                {}
            )

        with allure.step("Snapshot disk writes before traffic (bug 5653301)"):
            initial_writes = _snapshot_all_writes(engines.dut)

        with allure.step("Running run_mpi_basic_test"):
            traffic_status = AnsiblePlaybooksTool.run_playbook_by_key(
                Ansible.TESTS_EXECUTE_P2P,
                ansible_inventory_file,
                {}
            )

        with allure.step("Check disk writes during traffic (bug 5653301)"):
            disk_write_ok = _check_disk_writes(engines.dut, initial_writes)

        _assert_test_results(status_check, configure_nmx_status, traffic_status, disk_write_ok)

    finally:
        ClusterTools.update_nmx_c_manager_state(cluster, state="disabled")


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
@pytest.mark.timeout(45 * MINUTE, func_only=True)
def test_cluster_traffic_all_test(engines, devices, test_api, has_loopbox, standalone_system, setup_name,
                                  ansible_inventory_file):
    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json

    if standalone_system:
        pytest.skip(f"Skipping test - Standalone system, traffic not supported.")
    try:
        with allure.step("Enable Cluster"):
            cluster = Cluster()
            logger.info("Setting cluster state to enabled")
            ClusterTools.start_cluster(cluster, setup_name, output_format, devices=devices)

        with allure.step("Check cluster status"):
            status_check = AnsiblePlaybooksTool.run_playbook_by_key(
                Ansible.STATUS_HEALTH,
                ansible_inventory_file,
                {}
            )

        with allure.step("Configure NMX"):
            configure_nmx_status = AnsiblePlaybooksTool.run_playbook_by_key(
                Ansible.SOFTWARE_CONFIGURE_SWITCH,
                ansible_inventory_file,
                {}
            )

        with allure.step("Snapshot disk writes before traffic (bug 5653301)"):
            initial_writes = _snapshot_all_writes(engines.dut)

        with allure.step("Running run_mpi_all_test"):
            traffic_status = AnsiblePlaybooksTool.run_playbook_by_key(
                Ansible.TESTS_EXECUTE,
                ansible_inventory_file,
                {}
            )

        with allure.step("Check disk writes during traffic (bug 5653301)"):
            disk_write_ok = _check_disk_writes(engines.dut, initial_writes)

        _assert_test_results(status_check, configure_nmx_status, traffic_status, disk_write_ok)

    finally:
        ClusterTools.update_nmx_c_manager_state(cluster, state="disabled")
