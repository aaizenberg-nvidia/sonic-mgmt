import logging

import pytest

from ngts.constants.constants import GnmiConsts
from ngts.ngts_types import DevicesT
from ngts.nvos_constants.constants_nvos import DatabaseConst, PlatformConsts, PowerCappingConsts
from ngts.nvos_tools.Devices.IbDevice import RosalindSwitch
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.helpers import run_gnmi_client_and_parse_output
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.mark.platform
@pytest.mark.power_telemetry
def test_platform_asic_power_telemetry_default_fields_values(random_api, devices, engines, nv_command):
    """
    Validate ASIC Power Telemetry feature.
        Test flow:
            1. Validate nv show platform asic output
            2. Validate nv show platform asic ASIC output
            3. Validate nv show platform asic ASIC power counters output
            4. Validate GNMI output
    """

    with allure.step("Check gnmi is installed and running"):
        GnmiClient(engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, devices.dut.default_username,
                   devices.dut.default_password, verify_tools_installed=True)

    with allure.step("Check nv show platform asic"):
        asic_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.platform.asic.show()).get_returned_value()

        with allure.step("Verify default fields"):
            ValidationTool.verify_field_exist_in_json_output(asic_output,
                                                             PlatformConsts.POWER_TELEMETRY_ASIC_OUTPUT_FIELDS + devices.dut.asic_numbers).verify_result()

        with allure.step("Check only positive integers inside"):
            for asic, values in asic_output.items():
                power_values = values.get(PowerCappingConsts.POWER, {})
                for key in PlatformConsts.POWER_TELEMETRY_ASIC_OUTPUT_FIELDS:
                    value = power_values.get(key)
                    assert isinstance(value, str), f"Error: {key} in {asic} is not a string"
                    assert value.isdigit(), f"Error: {key} in {asic} is not a numeric string"
                    assert int(value) > 0, f"Error: {key} in {asic} is not a positive integer"

    with allure.step("Check nv show platform asic ASIC"):
        for asic_num in devices.dut.asic_numbers:
            asic_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.platform.asic.show(asic_num)).get_returned_value()

            with allure.step("Verify default fields"):
                ValidationTool.verify_field_exist_in_json_output(asic_output,
                                                                 PlatformConsts.POWER_TELEMETRY_ASIC_OUTPUT_FIELDS).verify_result()

            with allure.step("Check only positive integers inside"):
                for key in PlatformConsts.POWER_TELEMETRY_ASIC_OUTPUT_FIELDS:
                    value = asic_output['power'][key]
                    assert isinstance(value, str), f"Error: {key} in {asic} is not a string"
                    assert value.isdigit(), f"Error: {key} in {asic} is not a numeric string"
                    assert int(value) > 0, f"Error: {key} in {asic} is not a positive integer"

    with allure.step("Check nv show platform asic ASIC power counters"):
        for asic_num in devices.dut.asic_numbers:
            asic_output_power_counters = OutputParsingTool.parse_json_str_to_dictionary(nv_command.platform.asic.show(asic_num + ' power counters')).get_returned_value()

            with allure.step("Verify default fields"):
                ValidationTool.verify_field_exist_in_json_output(asic_output_power_counters,
                                                                 PlatformConsts.POWER_TELEMETRY_COUNTERS_FIELDS).verify_result()

            with allure.step("Check only positive integers inside"):
                for key, value in asic_output_power_counters.items():
                    assert isinstance(value, str), f"Error: {key} is not a string"
                    assert value.isdigit(), f"Error: {key} is not a numeric string"
                    assert int(value) >= 0, f"Error: {key} is not a non-negative integer"

    with allure.step("Check GNMI"):
        for asic_num in devices.dut.asic_numbers:
            with allure.step("Subscribe to the gnmi server and check system version"):
                for key in GnmiConsts.POWER_TELEMETRY_ASIC_GNMI_FIELDS:
                    xpath = f"/components/component[name={asic_num}]/asic/state/{key}"
                    gnmi_stream_updates = run_gnmi_client_and_parse_output(engines, devices, xpath, engines.dut.ip, mode='once')
                    gnmi_stream_updates_value = list(gnmi_stream_updates.values())[0]
                    assert gnmi_stream_updates_value.isdigit(), f"Error: {gnmi_stream_updates_value} is not a numeric string"
                    assert int(gnmi_stream_updates_value) >= 0, f"Error: {gnmi_stream_updates_value} is not a non-negative integer"


@pytest.mark.dut_reboot
@pytest.mark.platform
@pytest.mark.power_telemetry
def test_platform_asic_power_telemetry_counters_updates(engines, random_api, devices: DevicesT, nv_command, wrong_shunt_resistor_system):
    """
    Validate ASIC Power Telemetry feature counters updates.
        Test flow:
            1. Validate telemetry counters changes every 5 seconds
            2. Validate values from NVUE command and GNMI
            3. Validate reboot and check counters reset
    """
    if wrong_shunt_resistor_system:
        pytest.skip('system has wrong shunt resistor - part number is 692-9K33R-00MV-JQS, causing unexpected behavior like wrong power reads - https://nvbugspro.nvidia.com/bug/5242705')

    with allure.step("Check gnmi is installed and running"):
        GnmiClient(engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, devices.dut.default_username,
                   devices.dut.default_password, verify_tools_installed=True)

    with allure.step("Get random ASIC"):
        random_asic = RandomizationTool.select_random_value(devices.dut.asic_numbers).get_returned_value()

        if _check_hw_mgmt_rev(engines) and not isinstance(devices.dut, RosalindSwitch):
            with allure.step("Get output from NVUE counters command, wait 5 seconds, check counters changed"):
                with allure.step("Get output from NVUE command before reboot"):
                    counters_before_sleep = _get_power_temetry_counters(nv_command.platform, random_asic)

                with allure.step('Stress the system for 5 seconds'):
                    for _ in range(5):
                        nv_command.platform.firmware.show()

                with allure.step("Get output from NVUE command after reboot"):
                    counters_after_sleep = _get_power_temetry_counters(nv_command.platform, random_asic)

                with allure.step('Compare values'):
                    for key in PlatformConsts.POWER_TELEMETRY_COUNTERS_CHANGABLE_FIELDS:
                        value_counters_before_sleep = int(counters_before_sleep[key])
                        value_counters_after_sleep = int(counters_after_sleep[key])
                        assert value_counters_after_sleep - value_counters_before_sleep != 0, f"Error: {key} did not change"

        with allure.step("Get output from NVUE command, GNMI and compare"):
            for index, key in enumerate(PlatformConsts.POWER_TELEMETRY_ASIC_OUTPUT_FIELDS):
                with allure.step("Get output from NVUE command"):
                    asic_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.platform.asic.show(random_asic)).get_returned_value()

                with allure.step("Get output from GNMI"):
                    xpath = f"/components/component[name={random_asic}]/asic/state/{GnmiConsts.POWER_TELEMETRY_ASIC_GNMI_FIELDS[index]}"
                    gnmi_stream_updates = run_gnmi_client_and_parse_output(engines, devices, xpath, engines.dut.ip, mode='once')
                    gnmi_stream_updates_value = list(gnmi_stream_updates.values())[0]

                with allure.step("Compare both values"):
                    assert abs(int(gnmi_stream_updates_value) - int(asic_output['power'][key])) <= 5, f"Values {gnmi_stream_updates_value} and {asic_output['power'][key]} differ by more than 5"

        with allure.step("Get output from NVUE counters command, reboot switch, check counters reset"):
            with allure.step("Get output from NVUE command before reboot"):
                counters_before_reboot = _get_power_temetry_counters(nv_command.platform, random_asic)

            with allure.step('Reboot the system'):
                nv_command.system.action_reboot('force').verify_result()

            with allure.step("Get output from NVUE command after reboot"):
                counters_after_reboot = _get_power_temetry_counters(nv_command.platform, random_asic)

            with allure.step('Compare values'):
                for key in PlatformConsts.POWER_TELEMETRY_COUNTERS_CHANGABLE_FIELDS:
                    value_counters_before_reboot = int(counters_before_reboot[key])
                    value_counters_after_reboot = int(counters_after_reboot[key])
                    assert value_counters_after_reboot < value_counters_before_reboot, f"Error: {key} did not reset"


@pytest.mark.platform
@pytest.mark.power_telemetry
def test_negative_platform_asic_power_telemetry_not_supported(engines, devices, topology_obj, random_api, nv_command):
    """
    Validate ASIC Power Telemetry feature not working on not supported systems.
        Test flow:
            1. Validate nv show platform asic command
            2. Validate nv show platform asic ASIC command
    """

    with allure.step("Check nv show platform asic, on not gb300 device"):
        asic_output = nv_command.platform.asic.show(should_succeed=False)
        assert 'Invalid keywords found' not in asic_output, 'Command running on not supported device'

    with allure.step("Check nv show platform asic ASIC, on not supported systems"):
        for asic_num in devices.dut.asic_numbers:
            asic_output = nv_command.platform.asic.show(asic_num, should_succeed=False)
            assert 'Invalid keywords found' not in asic_output, 'Command running on not supported device'


def _get_power_temetry_counters(platform_obj, random_asic):
    asic_output = OutputParsingTool.parse_json_str_to_dictionary(platform_obj.asic.show(random_asic + ' power counters')).get_returned_value()

    return {key: asic_output[key] for key in PlatformConsts.POWER_TELEMETRY_COUNTERS_CHANGABLE_FIELDS if key in asic_output}


def _check_hw_mgmt_rev(engines):
    """
        This function check if we have hw-mgmt-rev which support power-capping feature
        """
    try:
        curr_value = Tools.DatabaseTool.sonic_db_cli_hget(engine=engines.dut, asic="",
                                                          db_name=DatabaseConst.CONFIG_DB_NAME,
                                                          db_config='DEVICE_METADATA|localhost', param='hw-mgmt-rev')
        value = int(curr_value)
        hex_val = hex(value)[2:]
        last_digit = int(hex_val[-1], 16)
        return last_digit >= 5

    except Exception as e:
        logger.info(f"Error occurred: {e}")
        return False
