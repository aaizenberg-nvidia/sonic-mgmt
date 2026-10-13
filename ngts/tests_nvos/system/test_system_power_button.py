import logging

import pytest

from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.switch_recovery import recover_dut_with_remote_reboot
from ngts.nvos_constants.constants_nvos import RebootConsts
from ngts.tests_nvos.system.reboot_telemetry_helpers import (
    RebootReasonCategory,
    assert_nvue_gnmi_counters_match,
    gnmi_client_for_dut,
    take_reboot_telemetry_snapshot,
    verify_reboot_telemetry_after_reboot,
)

logger = logging.getLogger()

# HW-MGMT raises this edge for both the power button and Redfish. power-mgmt
# watches only this node and records reboot -H -r "Power Cycle" -u BMC
# (NVBug 6828687; the old events/power_button watcher was removed in 6093193).
GRACEFUL_PWR_OFF_EVENT = '/var/run/hw-management/events/graceful_pwr_off'
GRACEFUL_PWR_OFF_USER = 'BMC'


@pytest.mark.dut_reboot
@pytest.mark.bmc
def test_system_power_button(engines, devices, topology_obj):
    """
    Test cover power button functionality
        Test flow:
            1. inject the graceful_pwr_off event and check the switch goes down
            2. recover switch with remote reboot
            3. check reboot reason
    """
    system = System()
    expected_reason = RebootConsts.REBOOT_REASON_POWER_CYCLE
    expected_user = GRACEFUL_PWR_OFF_USER
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)

    with allure.step('NVUE and gNMI reboot counters must match before power button test'):
        telemetry_before = take_reboot_telemetry_snapshot(system, gnmi_client)
        assert_nvue_gnmi_counters_match(telemetry_before)

    try:
        with allure.step('Simulate power button and check switch is down'):
            _simulate_power_button_press(engines)
            check_port_status_till_alive(False, engines.dut.ip, engines.dut.ssh_port, tries=15, delay=2)

    finally:
        with allure.step('Recover system with remote reboot'):
            recover_dut_with_remote_reboot(topology_obj, engines, 90)

        if not is_redmine_issue_active([4003176][0]):
            with allure.step("Verify NVUE and gNMI reboot telemetry after reboot"):
                verify_reboot_telemetry_after_reboot(
                    snapshot_before=telemetry_before,
                    system=system,
                    gnmi_client=gnmi_client,
                    expected_category=RebootReasonCategory.USER_INITIATED,
                    expected_details=expected_reason,
                    expected_user=expected_user,
                )

            ValidationTool.validate_reboot_reason_and_user(system, expected_reason, expected_user)


def _simulate_power_button_press(engines):
    status = engines.dut.run_cmd('systemctl is-active power-mgmt').strip()
    assert status == 'active', f'power-mgmt is not active: {status!r}'
    # Write 0 first so a node that is already 1 still produces an IN_MODIFY edge.
    engines.dut.run_cmd(f"sudo sh -c 'echo 0 > {GRACEFUL_PWR_OFF_EVENT}'")
    engines.dut.run_cmd(f"sudo sh -c 'echo 1 > {GRACEFUL_PWR_OFF_EVENT}'")
