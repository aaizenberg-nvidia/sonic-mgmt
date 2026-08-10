import logging
import pytest
import random
import re

from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_constants.constants_nvos import PlatformConsts, HealthConsts
from retry import retry


logger = logging.getLogger()

OK = HealthConsts.OK
NOT_OK = HealthConsts.NOT_OK


@pytest.mark.platform
def test_platform_environment_bmc_leakage(engines, nv_command, devices):
    """
    Validate BMC Leakage sensor feature.
        Test flow:
            1. Validate system health is OK by default
            2. Verify default values and fields for platform environment leakage command
            3. Simulate leakage on 1 sensor
            4. Validate output, system health
            5. Return sensor to default value and validate
            6. Simulate leakage on all sensors and validate
            7. Reboot the system and check no leakages
    """
    try:
        with allure.step("Get leakage folder name"):
            leakage_folder_name = _get_leakage_folder_name(engines)

        with allure.step("Validate system health is OK"):
            retry_validate_health_fix_or_issue(nv_command.system, OK)

        with allure.step("Verify default fields and values"):
            leakage_output = OutputParsingTool.parse_json_str_to_dictionary(
                nv_command.platform.environment.leakage.show()).get_returned_value()

            with allure.step("Verify default fields"):
                ValidationTool.verify_all_fields_value_exist_in_output_dictionary(
                    leakage_output, devices.dut.list_of_leakages).verify_result()

            with allure.step("Verify default values"):
                ValidationTool.validate_fields_values_in_output(devices.dut.list_of_leakages,
                                                                PlatformConsts.LEAKAGE_DEFAULT_OUTPUT_VALUES,
                                                                leakage_output).verify_result()

        with allure.step("Simulate leakage on random sensor and validate output"):
            random_selected_leakage = random.choice(devices.dut.list_of_leakages)
            _simulate_leakage(engines, random_selected_leakage, PlatformConsts.LEAK_STATUS_LEAK)

            with allure.step("Validate output"):
                retry_validate_leakage_state(nv_command, random_selected_leakage,
                                             PlatformConsts.LEAKAGE_STATUS_LEAK)

            with allure.step("Validate system health"):
                retry_validate_health_fix_or_issue(nv_command.system, NOT_OK)
                retry_validate_leakage_state(nv_command, random_selected_leakage,
                                             PlatformConsts.LEAKAGE_STATUS_LEAK)
                health_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.health.show())\
                    .get_returned_value()
                ValidationTool.compare_values(health_output[HealthConsts.STATUS_LED],
                                              HealthConsts.LED_NOT_OK_STATUS).verify_result()
                retry_validate_health_history(nv_command, random_selected_leakage)

            with allure.step("Return leakage status to default"):
                _simulate_leakage(engines, random_selected_leakage, PlatformConsts.LEAK_STATUS_OK)
                retry_validate_leakage_state(nv_command, random_selected_leakage,
                                             PlatformConsts.LEAKAGE_STATUS_OK)

        with allure.step("Simulate leakage on all sensors and validate output"):
            _simulate_leakage(engines, devices.dut.list_of_leakages)

            with allure.step("Verify output of all sensors"):
                retry_validate_all_leakages(nv_command, devices.dut.list_of_leakages,
                                            PlatformConsts.LEAKAGE_ALL_SENSOR_NOT_OK)

    finally:
        _link_back_sysfs_files(engines, devices.dut.list_of_leakages, leakage_folder_name)
        retry_validate_health_fix_or_issue(nv_command.system, OK)
        retry_validate_all_leakages(nv_command, devices.dut.list_of_leakages,
                                    PlatformConsts.LEAKAGE_DEFAULT_OUTPUT_VALUES)


def _simulate_leakage(engines, leakage, leakage_status=PlatformConsts.LEAK_STATUS_LEAK):
    if isinstance(leakage, str):
        rewrite_files(engines, leakage, leakage_status)
    else:
        for name in leakage:
            rewrite_files(engines, name, leakage_status)


def rewrite_files(engines, name, leakage_status):
    leakage_file = convert_string(name)
    engines.dut.run_cmd("sudo sh -c 'unlink {0}{1} && echo {2} > {0}{1}'".format(PlatformConsts
                                                                                 .LEAKAGE_FILES_FOLDER,
                                                                                 leakage_file, leakage_status))


def _get_leakage_folder_name(engines):
    ls_output = engines.dut.run_cmd("ls -la {}".format(PlatformConsts.LEAKAGE_FILES_FOLDER))
    leakage_folder_name = re.search(r'hwmon/([^/]+)/leakage1', ls_output).group(1)
    return leakage_folder_name


def _link_back_sysfs_files(engines, leakage, leakage_folder_name):
    for name in leakage:
        leakage_file = convert_string(name)
        engines.dut.run_cmd("sudo sh -c 'rm {0}{1}'".format(PlatformConsts.LEAKAGE_FILES_FOLDER, leakage_file))
        engines.dut.run_cmd("sudo sh -c 'ln -s {2}{3}/{1} {0}{1}'".format(PlatformConsts.LEAKAGE_FILES_FOLDER,
                                                                          leakage_file, PlatformConsts.LEAKAGE_FILES_SYSFS_FOLDER, leakage_folder_name))


@retry(Exception, tries=15, delay=1)
def retry_validate_health_fix_or_issue(system, status):
    system.validate_health_status(status)


@retry(Exception, tries=5, delay=1)
def retry_validate_health_history(nv_command, random_selected_leakage):
    history_line = nv_command.system.health.history.search_line(line_to_search=random_selected_leakage)
    assert random_selected_leakage in history_line, 'Cant find leakage in health history'


@retry(Exception, tries=5, delay=1)
def retry_validate_leakage_state(nv_command, leakage_name, expected_state):
    """
    Validate leakage state with retry mechanism to allow time for state propagation.

    Args:
        nv_command: Command object for executing NVUE commands
        leakage_name: Name of the leakage sensor (e.g., 'LEAKAGE-1')
        expected_state: Expected state value (e.g., 'ok' or 'leak')
    """
    leakage_output = OutputParsingTool.parse_json_str_to_dictionary(
        nv_command.platform.environment.leakage.show()).get_returned_value()
    ValidationTool.compare_values(leakage_output[leakage_name]['state'],
                                  expected_state).verify_result()


@retry(Exception, tries=5, delay=1)
def retry_validate_all_leakages(nv_command, leakage_list, expected_values_dict):
    """
    Validate multiple leakage states with retry mechanism to allow time for state propagation.

    Args:
        nv_command: Command object for executing NVUE commands
        leakage_list: List of leakage sensor names
        expected_values_dict: Dictionary of expected field values
    """
    leakage_output = OutputParsingTool.parse_json_str_to_dictionary(
        nv_command.platform.environment.leakage.show()).get_returned_value()
    ValidationTool.validate_fields_values_in_output(leakage_list,
                                                    expected_values_dict,
                                                    leakage_output).verify_result()


def convert_string(input_string):
    parts = input_string.split('-')

    main_part = parts[0].lower() + parts[1]

    if len(parts) > 2:
        remaining_parts = '_'.join(parts[2:])
        result = f"{main_part}_{remaining_parts.lower()}"
    else:
        result = main_part.lower()

    return result
