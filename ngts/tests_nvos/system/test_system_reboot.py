import logging
import random
import re
import time

import pytest

from retry import retry
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import ApiType, RebootConsts, SystemConsts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool, RebootParams, ping_device
from ngts.nvos_tools.system.System import System
from ngts.nvos_constants.constants_nvos import ActionConsts
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.system.reboot_telemetry_helpers import (
    REBOOT_REASON_SHOW_EXEMPTED_ERR_MSGS,
    RebootReasonCategory,
    assert_nvue_gnmi_counters_match,
    gnmi_client_for_dut,
    take_reboot_telemetry_snapshot,
    verify_reboot_telemetry_after_reboot,
)
from ngts.tools.test_utils import allure_utils as allure
from retry.api import retry_call
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active

logger = logging.getLogger(__name__)
REBOOT_OUTPUT_FIELDS = {"gentime", "reason", "reason-type", "user"}


def _verify_exact_reboot_fields(output, output_name):
    output_fields = set(output.keys())
    missing_fields = REBOOT_OUTPUT_FIELDS - output_fields
    extra_fields = output_fields - REBOOT_OUTPUT_FIELDS
    assert output_fields == REBOOT_OUTPUT_FIELDS, (
        f"Unexpected fields in '{output_name}' output. "
        f"Missing fields: {missing_fields or 'none'}; extra fields: {extra_fields or 'none'}"
    )


@pytest.mark.dut_reboot
@pytest.mark.usefixtures("disable_els_init_state_for_taipan")
@pytest.mark.system
def test_system_reset_bit(engines, devices, topology_obj, test_name):
    """
    Test flow:
        1. Clear system events to avoid stale reboot entries.
        2. Trigger reboot by writing the system reset bit (linux iorw command).
        3. Wait for the system to be ready on serial, then for NVOS to be functional.
        4. Verify reboot show/reason/history output shape and reboot cause (reason/user).
        5. Validate reboot reason and user against device expectations.
        6. Assert reboot duration is within the expected range for reset-bit reboot.
    """
    if not devices.dut.sys_rst_n_bit_command:
        pytest.skip("System reset bit trigger not supported on this platform")

    system = System(None)
    expected_reason, expected_user, expected_reason_type = devices.dut.reboot_reason_dict[RebootConsts.RESET_BIT]

    try:
        with allure.step('Clear system events to remove older reboot system events'):
            system.events.action(ActionConsts.CLEAR)

        start_time = time.perf_counter()

        with allure.step('Run linux command to trigger system reset via reset bit'):
            DutUtilsTool.run_cmd_with_disconnect(engines.dut, devices.dut.sys_rst_n_bit_command, timeout=5)

        with allure.step('Wait for system to be ready in serial'):
            DutUtilsTool.wait_for_system_ready_in_serial(topology_obj, wait_timeout=devices.dut.timeout_system_is_ready)

        with allure.step(f"wait for system to become functional"):
            DutUtilsTool.wait_for_nvos_to_become_functional(engines.dut).verify_result()

        duration = time.perf_counter() - start_time

        with allure.step("Check system reboot output"):
            output = OutputParsingTool.parse_json_str_to_dictionary(system.reboot.show()).get_returned_value()
            assert "reason" in output.keys(), "'reason' not in the output"

            with allure.independent_step("Check system reboot reason output"):
                output = OutputParsingTool.parse_json_str_to_dictionary(system.reboot.reason.show()).get_returned_value()
                ValidationTool.verify_all_fields_value_exist_in_output_dictionary(
                    output, ["gentime", "reason", "reason-type", "user"]).verify_result()

            with allure.independent_step("Check system reboot history output"):
                output = OutputParsingTool.parse_json_str_to_dictionary(system.reboot.history.show()).get_returned_value()
                if output and len(output.keys()) > 0:
                    ValidationTool.verify_all_fields_value_exist_in_output_dictionary(
                        output[list(output.keys())[0]], ["gentime", "reason", "reason-type", "user"]).verify_result()

            with allure.independent_step("Check reboot cause"):
                output = OutputParsingTool.parse_json_str_to_dictionary(system.reboot.reason.show()).get_returned_value()
                assert expected_reason in output["reason"], f"{expected_reason} not found in show reboot output"
                assert expected_user in output["user"], f"Reboot user is {output['user']} instead of '{expected_user}'"
                assert expected_reason_type == output["reason-type"], (
                    f"Reboot reason-type is {output['reason-type']} instead of '{expected_reason_type}'")

            with allure.independent_step("Validate reboot reason and user"):
                ValidationTool.validate_reboot_reason_and_user(system, expected_reason, expected_user)

            with allure.independent_step("Verify reboot time is within expected range"):
                OperationTime.verify_operation_time(duration, "reset bit", devices).verify_result()

    finally:
        if not ping_device(engines.dut.ip):
            logger.info("system is off and will now remote reboot the switch")
            NvueGeneralCli(TestToolkit.engines.dut).remote_reboot_nvue(topology_obj)


@pytest.mark.usefixtures("disable_els_init_state_for_taipan")
@pytest.mark.check_log_size
@pytest.mark.dut_reboot
@pytest.mark.check_disk_usage
@pytest.mark.system
@pytest.mark.nvos_build
def test_reboot_command(engines, devices, test_name, topology_obj):
    """
    Test flow:
        1. run nv action reboot system
    """
    system = System(None)
    expected_reason, expected_user = devices.dut.reboot_reason_dict[RebootConsts.COLD]
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)

    with allure.step('Clear system events to remove older reboot system events'):
        system.events.action(ActionConsts.CLEAR)

    with allure.step('NVUE and gNMI reboot counters must match before reboot'):
        telemetry_before = take_reboot_telemetry_snapshot(system, gnmi_client)
        assert_nvue_gnmi_counters_match(telemetry_before)

    with allure.step('Run nv action reboot system and wait for system to be ready in serial'):
        result_obj, duration = OperationTime.save_duration('reboot', '', test_name, system.reboot.action_reboot,
                                                           topology_obj=topology_obj, check_system_is_functional=False)

    with allure.step("wait for system to become functional"):
        DutUtilsTool.wait_for_nvos_to_become_functional(engines.dut).verify_result()

    with allure.step("Check system reboot output"):
        output = OutputParsingTool.parse_json_str_to_dictionary(system.reboot.show()).get_returned_value()
        assert "reason" in output.keys(), "'reason' not in the output"

        with allure.independent_step("Check system reboot reason output"):
            output = OutputParsingTool.parse_json_str_to_dictionary(system.reboot.reason.show()).get_returned_value()
            _verify_exact_reboot_fields(output, "system reboot reason")
            ValidationTool.verify_all_fields_value_exist_in_output_dictionary(output, REBOOT_OUTPUT_FIELDS).verify_result()

        with allure.independent_step("Verify NVUE and gNMI reboot telemetry after reboot"):
            verify_reboot_telemetry_after_reboot(
                snapshot_before=telemetry_before,
                system=system,
                gnmi_client=gnmi_client,
                expected_category=RebootReasonCategory.USER_INITIATED,
                expected_details=expected_reason,
                expected_user=expected_user,
            )

        with allure.independent_step("Check system reboot history output"):
            output = OutputParsingTool.parse_json_str_to_dictionary(
                system.reboot.history.show(exempted_err_msgs=REBOOT_REASON_SHOW_EXEMPTED_ERR_MSGS)
            ).get_returned_value()
            if output and len(output.keys()) > 0:
                first_entry = output[list(output.keys())[0]]
                required_fields = {"gentime", "reason", "reason-type", "user"}
                projected_entry = {field: first_entry.get(field) for field in required_fields}
                ValidationTool.verify_all_fields_value_exist_in_output_dictionary(
                    projected_entry, required_fields
                ).verify_result()

        with allure.independent_step("Validate reboot reason and user"):
            ValidationTool.validate_reboot_reason_and_user(system, expected_reason, expected_user)

        with allure.independent_step("Verify reboot time is within expected range"):
            OperationTime.verify_operation_time(duration, devices.dut.reboot_type, devices).verify_result()


@pytest.mark.dut_reboot
@pytest.mark.usefixtures("disable_els_init_state_for_taipan")
@pytest.mark.system
def test_reboot_command_force(engines, devices, test_name, random_api, topology_obj):
    """
    Test flow:
        1. run nv action reboot system force
    """
    system = System(None)
    with allure.step('Run nv action reboot system mode force'):
        result_obj, duration = OperationTime.save_duration('reboot', '', test_name,
                                                           system.reboot.action_reboot, params='force',
                                                           topology_obj=topology_obj)
        OperationTime.verify_operation_time(duration, devices.dut.reboot_type, devices).verify_result()


@pytest.mark.system
@pytest.mark.dut_reboot
def test_reboot_command_bad_flow(engines, devices):
    """
    Test flow:
        1. run nv action reboot system --type fast
        2. expected message: not supported for IB
        3. run nv action reboot system --type warm
        4. expected message: not supported for IB
    """
    substring = 'Error: Invalid parameter'
    invalid_command = 'Error: Invalid Command:'
    err_message = 'Reboot types should not be supported in NVOS'

    with allure.step('Run nv action reboot system type fast'):
        list_commands = ['nv action reboot system type fast', 'y']
        output = engines.dut.send_config_set(list_commands, exit_config_mode=False, cmd_verify=False,
                                             enter_config_mode=False)
        ValidationTool.verify_substring_in_output(output, substring, err_message, True)

    with allure.step('Run nv action reboot system type warm'):
        list_commands = ['nv action reboot system type warm', 'y']
        output = engines.dut.send_config_set(list_commands, exit_config_mode=False, cmd_verify=False,
                                             enter_config_mode=False)
        ValidationTool.verify_substring_in_output(output, substring, err_message, True)

    with allure.step("test non-existing mode"):
        output = engines.dut.run_cmd('nv action reboot system mode non existing mode')
        ValidationTool.verify_expected_output(output, invalid_command, True)

        with allure.step("test pruned warm mode"):
            output = engines.dut.run_cmd('nv action reboot system mode warm')
            ValidationTool.verify_expected_output(output, invalid_command, True)

    if RebootConsts.POWER_CYCLE not in devices.dut.supported_commands:
        with (allure.step("action power-cycle not supported. Test negative flow")):
            output = engines.dut.run_cmd('nv action reboot system mode power-cycle')
            ValidationTool.verify_expected_output(output, RebootConsts.POWER_CYCLE_NOT_SUPPORTED_ERR_MSG, True)


@pytest.mark.dut_reboot
@pytest.mark.usefixtures("disable_els_init_state_for_taipan")
@pytest.mark.system
@pytest.mark.parametrize('mode', RebootConsts.DEFAULT_MODES)
def test_reboot_mode(engines, devices, topology_obj, mode, random_api, test_name, verify_no_kernel_errors):
    if mode == RebootConsts.POWER_CYCLE and mode not in devices.dut.supported_commands:
        pytest.skip(f"{mode} not supported")
    system = System()
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)

    try:
        with allure.step('Clear system events to remove older reboot system events'):
            system.events.action(ActionConsts.CLEAR)

        with allure.step('NVUE and gNMI reboot counters must match before reboot'):
            telemetry_before = take_reboot_telemetry_snapshot(system, gnmi_client)
            assert_nvue_gnmi_counters_match(telemetry_before)

        result_obj = _reboot_system_by_mode(engines, devices, test_name, topology_obj, mode)
        result_obj.verify_result()

        expected_reason, expected_user = devices.dut.reboot_reason_dict[mode]

        with allure.step("Verify NVUE and gNMI reboot telemetry after reboot"):
            telemetry_category = (
                RebootReasonCategory.POWER_FAILURE
                if expected_reason == SystemConsts.REBOOT_REASON_POWER_LOSS
                else RebootReasonCategory.USER_INITIATED
            )
            verify_reboot_telemetry_after_reboot(
                snapshot_before=telemetry_before,
                system=system,
                gnmi_client=gnmi_client,
                expected_category=telemetry_category,
                expected_details=expected_reason,
                expected_user=expected_user,
            )

        with allure.step("Validate reboot reason and user"):
            ValidationTool.validate_reboot_reason_and_user(system, expected_reason, expected_user)

        with allure.step("Verify reboot time is within expected range"):
            OperationTime.verify_operation_time(result_obj.duration, mode, devices).verify_result()

    finally:
        if not ping_device(engines.dut.ip):
            logger.info("system is off and will now remote reboot the switch")
            NvueGeneralCli(TestToolkit.engines.dut).remote_reboot_nvue(topology_obj)


@pytest.mark.dut_reboot
@pytest.mark.usefixtures("disable_els_init_state_for_taipan")
@pytest.mark.system
@pytest.mark.timeout(10 * MINUTE)
def test_reboot_via_remote_reboot(engines, devices, topology_obj):
    """
    Test flow:
        1. run remote reboot script to turn electrical source power off and on
        2. Validate reboot reason in system events
    """
    system = System()
    expected_reason, expected_user = devices.dut.reboot_reason_dict[RebootConsts.REMOTE_REBOOT]
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)

    with allure.step('Clear system events to remove older reboot system events'):
        system.events.action(ActionConsts.CLEAR)

    with allure.step('NVUE and gNMI reboot counters must match before remote reboot'):
        telemetry_before = take_reboot_telemetry_snapshot(system, gnmi_client)
        assert_nvue_gnmi_counters_match(telemetry_before)

    with allure.step("Get name from NOGA"):
        noga_query_data = topology_obj.players['dut']['attributes'].noga_query_data['attributes']
        dhcp_hostname = noga_query_data['Common']['Name'] or noga_query_data['Specific']['dhcp_hostname']

    with allure.step("Sync filesystem to ensure LogAnalyzer marker is persisted to disk before power cycle"):
        engines.dut.run_cmd("sync")

    with allure.step("Reboot the system using remote reboot"):
        DutUtilsTool.dut_psu_control(engines, topology_obj, dhcp_hostname=dhcp_hostname)

    reboot_params = RebootParams(topology_obj=topology_obj)
    res_obj = DutUtilsTool.wait_on_system_reboot(engines.dut, reboot_params=reboot_params, device=devices.dut, verify_final_result=False)
    assert res_obj.result, 'System reboot failed'

    with allure.step("Verify NVUE and gNMI reboot telemetry after remote reboot"):
        telemetry_category = (
            RebootReasonCategory.POWER_FAILURE
            if expected_reason == SystemConsts.REBOOT_REASON_POWER_LOSS
            else RebootReasonCategory.USER_INITIATED
        )
        verify_reboot_telemetry_after_reboot(
            snapshot_before=telemetry_before,
            system=system,
            gnmi_client=gnmi_client,
            expected_category=telemetry_category,
            expected_details=expected_reason,
            expected_user=expected_user,
        )

    ValidationTool.validate_reboot_reason_and_user(system, expected_reason, expected_user)


def _reboot_system_by_mode(engines, devices, test_name, topology_obj, mode):
    system = System()
    dhcp_hostname = ''

    if mode == RebootConsts.HALT:
        noga_data = topology_obj.players['dut']['attributes'].noga_query_data['attributes']
        dhcp_hostname = noga_data['Common']['Name'] or noga_data['Specific']['dhcp_hostname']

    # Run reboot
    with allure.step(f"Rebooting system with mode: {mode}"):
        reboot_params = RebootParams(topology_obj=topology_obj)
        reboot_params.should_wait_till_system_ready = mode != RebootConsts.HALT
        reboot_result_obj, _ = OperationTime.save_duration(f"reboot {mode}", '', test_name,
                                                           system.action_reboot,
                                                           additional_params={'mode': mode},
                                                           reboot_params=reboot_params)
        time.sleep(10)

    # PSU recovery for halt
    if mode == RebootConsts.HALT:
        with allure.step("Power the system back on via PSU"):
            DutUtilsTool.dut_psu_control(engines, topology_obj, dhcp_hostname=dhcp_hostname)

        with allure.step("Wait for system to be ready"):
            result_obj = DutUtilsTool.wait_on_system_reboot(engines.dut, device=devices.dut, verify_final_result=False)
            assert result_obj.result, f"System did not come back after reboot mode {mode}"

    return reboot_result_obj


@pytest.mark.platform
def test_lspci_width(engines, devices):
    """
    The purpose of this function is to check if 2 parameters (LnkSta, LnkCap) are valid.
    """
    with allure.step("Running lspci command to find Infiniband controllers"):
        lines_devices = engines.dut.run_cmd("sudo lspci | grep 'Infiniband'")
        actual_devices = lines_devices.split("\n")

    with allure.step("Validating the number of detected devices"):
        assert len(actual_devices) == devices.dut.asic_amount, \
            f"Actual number of devices: {len(actual_devices)}, Expected: {devices.dut.asic_amount}"

    for device in actual_devices:
        device_num_str = device.split(" ")[0]

        with allure.step(f"Fetching LnkCap for device {device_num_str}"):
            bash_cmd_cap = f"sudo lspci -vv -s {device_num_str} | grep LnkCap:"
            line_cap = engines.dut.run_cmd(bash_cmd_cap)
            cap_arr = line_cap.split(' ')

        with allure.step(f"Fetching LnkSta for device {device_num_str}"):
            bash_cmd_sta = f"sudo lspci -vv -s {device_num_str} | grep LnkSta:"
            line_sta = engines.dut.run_cmd(bash_cmd_sta)
            sta_arr = line_sta.split(' ')

        with allure.step(f"Validating LnkCap and LnkSta for device {device_num_str}"):
            validate_lspci_status(engines, cap_arr, sta_arr, line_sta, line_cap)


def validate_lspci_status(engines, cap_arr, sta_arr, line_sta, line_cap):
    with allure.step("Checking if cap_arr and sta_arr are non-empty"):
        assert sta_arr, f"sta_arr is empty: {sta_arr}"
        assert cap_arr, f"cap_arr is empty: {cap_arr}"

    with allure.step("Validating speed in LnkCap and LnkSta"):
        number_gts_sta = get_number_gts(sta_arr, "")
        number_gts_cap = get_number_gts(cap_arr, "")
        assert number_gts_sta, f"The string number_gts_sta is empty: {sta_arr}"
        assert number_gts_cap, f"The string number_gts_cap is empty: {cap_arr}"
        assert number_gts_cap in number_gts_sta, \
            f"Speed NUMBER GT/s mismatch: LnkCap={number_gts_cap}, LnkSta={number_gts_sta}"

    with allure.step("Validating Width values in LnkCap and LnkSta"):
        x_number_cap = get_x_number(cap_arr, "")
        assert x_number_cap, f"Width x<Number> not found in line_cap: {line_cap}, cap_arr: {cap_arr}"
        x_number_sta = get_x_number(sta_arr, "")
        assert x_number_sta, "Width x<Number> not found in LnkSta"
        assert x_number_cap == x_number_sta, \
            f"Width mismatch: LnkCap={x_number_cap}, LnkSta={x_number_sta}"


def get_number_gts(arr, number_gts):
    """
    Return GT/s number.
    """
    with allure.step("Extracting GT/s value from array"):
        for word in arr:
            if "GT/s" in word:
                number_gts = word[:-1] if word.endswith(',') else word
                return number_gts


def get_x_number(arr, x_number):
    """
    Return x number.
    """
    with allure.step("Extracting Width x<number> from array"):
        for i in range(len(arr)):
            if "Width" in arr[i]:
                x_number = arr[i + 1]
                x_number = x_number[:-1] if x_number.endswith(',') else x_number
                return x_number


@pytest.mark.system
def test_show_system_reboot_history_filter(engines, random_api):
    """
    Verify `nv show system reboot history --filter <field>=<value>` (NVUE)
    and the equivalent `?filter=<field>%3d<value>` REST query (OpenAPI).

    flow:
    1. Run show reboot history without filter - capture full output
    2. Pick a random entry and a random non-gentime field/value
       (gentime contains spaces, which complicates --filter quoting on NVUE)
    3. Build expected dict (entries where field==value), run with the filter, verify match
    4. Run with an empty filter - verify it equals the unfiltered output
    5. Run with a valid field but a value that does not match any entry - verify {}
    6. Run with a filter field that does not exist - verify error message
       (NVUE: stderr "No match found for filter depth of N."; OpenAPI: HTTP 404
       body with the same "No match found for filter depth of N." string)
    """
    system = System()

    with allure.step('Run show reboot history without filter'):
        output_dict = OutputParsingTool.parse_json_str_to_dictionary(
            system.reboot.history.show(exempted_err_msgs=REBOOT_REASON_SHOW_EXEMPTED_ERR_MSGS)
        ).get_returned_value()
        if not output_dict:
            pytest.skip("reboot history is empty - nothing to filter")

    with allure.step('Select a random filter field and value (skip gentime - has spaces)'):
        random_key = RandomizationTool.select_random_value(list(output_dict.keys())).get_returned_value()
        filter_name = RandomizationTool.select_random_value(
            list(output_dict[random_key].keys()), forbidden_values=['gentime']
        ).get_returned_value()
        value = output_dict[random_key][filter_name]

    with allure.step('Build expected filtered dict from full output'):
        filtered_expected = {
            k: v for k, v in output_dict.items() if v.get(filter_name) == value
        }

    with allure.step('Verify filter behaviors'):
        with allure.independent_step(f'Filter reboot history by {filter_name}={value}'):
            filtered_raw = system.reboot.history.filter(
                filter_name=filter_name, value=value,
                exempted_err_msgs=REBOOT_REASON_SHOW_EXEMPTED_ERR_MSGS,
            ).get_returned_value()
            output_dict_filtered = OutputParsingTool.parse_json_str_to_dictionary(filtered_raw).get_returned_value()
            assert len(output_dict_filtered) == len(filtered_expected), (
                f"filter result size mismatch: filtered={len(output_dict_filtered)} "
                f"expected={len(filtered_expected)} (filter {filter_name}={value!r})"
            )
            ValidationTool.compare_nested_dictionary_content(
                output_dict_filtered, filtered_expected
            ).verify_result()

        with allure.independent_step('Empty filter must return the full output'):
            empty_filter_raw = system.reboot.history.filter(
                exempted_err_msgs=REBOOT_REASON_SHOW_EXEMPTED_ERR_MSGS,
            ).get_returned_value()
            output_dict_empty_filter = OutputParsingTool.parse_json_str_to_dictionary(empty_filter_raw).get_returned_value()
            ValidationTool.compare_nested_dictionary_content(
                output_dict_empty_filter, output_dict
            ).verify_result()

        with allure.independent_step('Existing field with non-matching value must return {}'):
            no_match_raw = system.reboot.history.filter(
                filter_name=filter_name, value='__no_such_value__',
                exempted_err_msgs=REBOOT_REASON_SHOW_EXEMPTED_ERR_MSGS,
            ).get_returned_value()
            empty_output = OutputParsingTool.parse_json_str_to_dictionary(no_match_raw).get_returned_value()
            assert empty_output == {}, (
                f"expected empty dict for non-matching filter value, got {empty_output!r}"
            )

        with allure.independent_step('Non-existing filter field must return an error'):
            out = system.reboot.history.filter(
                filter_name='__no_such_field__', value='x',
            ).verify_result(False)
            assert re.search(r'No match found for filter depth of \d+\.', out), (
                f"expected 'No match found for filter depth of N.' message, got {out!r}"
            )
