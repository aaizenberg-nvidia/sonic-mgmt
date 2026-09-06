import logging

import pytest

from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.switch_recovery import recover_dut_with_remote_reboot
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.nvos_constants.constants_nvos import SystemConsts, RebootConsts
from ngts.tests_nvos.system.reboot_telemetry_helpers import (
    REBOOT_REASON_SHOW_EXEMPTED_ERR_MSGS,
    RebootReasonCategory,
    assert_nvue_gnmi_counters_match,
    gnmi_client_for_dut,
    take_reboot_telemetry_snapshot,
    verify_reboot_telemetry_after_reboot,
)

logger = logging.getLogger()


@pytest.mark.dut_reboot
@pytest.mark.bmc
def test_system_power_button(engines, devices, topology_obj):
    """
    Test cover juliet power button functionality
        Test flow:
            1. simulate power button and check switch going down
            2. recover switch with remote reboot
            3. check reboot reason
    """
    system = System()
    expected_reason, expected_user = devices.dut.reboot_reason_dict[RebootConsts.POWER_BUTTON]
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)

    with allure.step('NVUE and gNMI reboot counters must match before power button test'):
        telemetry_before = take_reboot_telemetry_snapshot(system, gnmi_client)
        assert_nvue_gnmi_counters_match(telemetry_before)

    try:
        with allure.step('Simulate power button and check switch is down'):
            _simulate_power_button_press(engines)
            check_port_status_till_alive(False, engines.dut.ip, engines.dut.ssh_port)

    finally:
        with allure.step('Recover system with remote reboot'):
            recover_dut_with_remote_reboot(topology_obj, engines, 90)

        if not is_redmine_issue_active([4003176][0]):
            with allure.step("Verify NVUE and gNMI reboot telemetry after reboot"):
                reason_row = OutputParsingTool.parse_json_str_to_dictionary(
                    system.reboot.reason.show(exempted_err_msgs=REBOOT_REASON_SHOW_EXEMPTED_ERR_MSGS)
                ).get_returned_value()
                reason_val = reason_row.get("reason", "")
                if isinstance(reason_val, dict):
                    reason_val = reason_val.get("reason", "")
                telemetry_details = str(reason_val).strip()
                verify_reboot_telemetry_after_reboot(
                    snapshot_before=telemetry_before,
                    system=system,
                    gnmi_client=gnmi_client,
                    expected_category=RebootReasonCategory.USER_INITIATED,
                    expected_details=telemetry_details,
                    expected_user=expected_user,
                )

            ValidationTool.validate_reboot_reason_and_user(system, expected_reason, expected_user)


def _simulate_power_button_press(engines):
    engines.dut.run_cmd('sudo touch /var/run/hw-management/events/power_button')
    engines.dut.run_cmd('sudo chmod 777 /var/run/hw-management/events/power_button')
    engines.dut.run_cmd('sudo service power-mgmt restart')
    engines.dut.run_cmd('sudo echo 1 > /var/run/hw-management/events/power_button')
