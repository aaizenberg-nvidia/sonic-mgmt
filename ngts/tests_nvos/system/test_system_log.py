import logging
import math
import random
import re
import time
import json
import pytest
from typing import List
from datetime import datetime
from retry import retry

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import LogComponentsConsts, SyslogConsts, LogsSources
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
from ngts.constants.constants import BugHandlerConst
from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.FilesTool import FilesTool
from ngts.nvos_tools.infra.DutUtilsTool import wait_for_specific_regex_in_logs
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.infra.SudoScope import sudo_scope_if


cumulus_owner = "gosaini"


logger = logging.getLogger()


@pytest.mark.system
@pytest.mark.log
@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
@pytest.mark.cumulus
def test_show_log(engines, devices):
    """
    Write to log file on switch, run nv show system log command and verify system/image are exist
    command: nv show system log

    Test flow:
        1. Rotate logs
        2. Run show system image
        3. Run nv show system log
        4. Check if we have in the logs 'regular_log' message
    """
    with allure.step("Create System object"):
        system = System(None)
        platform = Platform()

    with allure.step("Rotate logs"):
        system.log.rotate_logs()

    firmware_log_marker = (
        devices.dut.system_log_value
        if devices.dut.system_log_value != "system log"
        else "part-number"
    )
    with allure.step(f"Verify {LogsSources.NVUED} includes the expected logs"):
        with allure.independent_step(
                f"Run show platform command to view platform firmware; verify in {LogsSources.NVUED}"):
            platform.firmware.show()
            with sudo_scope_if(devices.dut.is_eth()):
                system.log.verify_expected_logs(
                    logs_to_find=[firmware_log_marker],
                    logs_source=LogsSources.NVUED,
                    engine=engines.dut,
                    only_latest_log=True)

        with allure.independent_step(
                f'Run show system image and verify "system/image" in the {LogsSources.NVUED} logs'):
            system.image.show()
            with sudo_scope_if(devices.dut.is_eth()):
                system.log.verify_expected_logs(
                    logs_to_find=["system/image"], logs_source=LogsSources.NVUED,
                    engine=engines.dut, only_latest_log=True)


@pytest.mark.system
@pytest.mark.log
@pytest.mark.simx
def test_show_log_continues(engines):
    """
    Write to log file on switch, run nv show system log command and verify system/image are exist
    command: nv show system log --view follow

    Test flow:
        1. Rotate logs
        2. Run show system image
        3. Run nv show system log --view follow
        4. Check if we have in the logs 'regular_log' message
    """
    with allure.step("Create System object"):
        system = System()
        platform = Platform()

    with allure.step("Rotate logs"):
        system.log.rotate_logs()

    with allure.step("Run nv show system log command --view follow to view system logs"):
        wait_for_specific_regex_in_logs(engines.dut, "nvued\\:    INFO", timeout=5)

    with allure.step("Run show command to view platform firmware"):
        platform.firmware.show()

    with allure.step(f"Verify part-number in {LogsSources.NVUED}"):
        system.log.verify_expected_logs(
            logs_to_find=["part-number"],
            logs_source=LogsSources.NVUED,
            engine=engines.dut,
            only_latest_log=True)


@pytest.mark.system
@pytest.mark.log
@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
@pytest.mark.cumulus
def test_show_log_files(engines, devices):
    """
    Check all fields in files commands, write to log check it exist in show files command

    Test flow:
        1. Run nv show system log files command and validate fields
        2. Rotate logs
        3. Run show system image
        4. Check if we have in the logs 'system/image' message
    """
    with allure.step("Create System object"):
        system = System(None)
        platform = Platform()

    with allure.step("Run nv show system log file list command and validate fields"):
        show_output = system.log.file.show(op_param='list')
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()

        with allure.step("Validate all expected fields in show output"):
            ValidationTool.verify_field_exist_in_json_output(output_dictionary,
                                                             [LogComponentsConsts.SYSLOG]).verify_result()
            logging.info("All expected fields were found")

    with allure.step("Rotate logs"):
        system.log.rotate_logs()

    with allure.step("Run show command to view platform firmware"):
        platform.firmware.show()

    firmware_log_marker = (
        devices.dut.system_log_value
        if devices.dut.system_log_value != "system log"
        else "part-number"
    )
    with allure.step(f"Verify firmware marker in {LogsSources.NVUED}"):
        with sudo_scope_if(devices.dut.is_eth()):
            system.log.verify_expected_logs(
                logs_to_find=[firmware_log_marker],
                logs_source=LogsSources.NVUED,
                engine=engines.dut,
                only_latest_log=True)


@pytest.mark.system
@pytest.mark.log
@pytest.mark.simx
def test_log_files_rotation_default_fields(engines):
    """
    Check all fields and default values exist in nv show system log files rotation
    command: nv show system log files rotation
    Prune temp for NVOS - component

    Test flow:
        1. Verify all fields in command
        2. Verify all default values
    """
    with allure.step("Create System object"):
        system = System(None)

    with allure.step('Test set log rotation default_fields'):
        with allure.independent_step(f"Test rotate log files on {LogComponentsConsts.SYSLOG}"):
            _log_files_rotation_default_fields(system.log, "20", "10.0")
        # component = get_random_component(system)
        # with allure.independent_step(f"Test rotate log files on {component.component_name}"):
        #     _log_files_rotation_default_fields(component, "20", "10.0")


def _log_files_rotation_default_fields(system_log_obj, default_max_number, default_size):
    with allure.step("Run nv show system log rotation command and validate fields"):
        logging.info("Run nv show system log rotation command and validate fields")
        show_output = system_log_obj.rotation.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()

        with allure.step("Validate all expected fields in show output"):
            ValidationTool.verify_field_exist_in_json_output(output_dictionary,
                                                             ["frequency", 'max-number', 'size']).verify_result()
            logging.info("All expected fields were found")

    with allure.step("Verify default values"):
        ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                    field_name="frequency", expected_value="daily").verify_result()

        ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                    field_name='max-number',
                                                    expected_value=default_max_number).verify_result()

        ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                    field_name='size', expected_value=default_size).verify_result()


@pytest.mark.system
@pytest.mark.log
@pytest.mark.simx
def test_log_files_set_unset_log_rotation_frequency(engines):
    """
    Check set unset for files rotation parameters
    command: nv set/unset system log rotation command
    Prune temp for NVOS - component

    Test flow:
        1. Get default values for rotation frequency
        2. Negative testing for frequency
        3. Validate set, unset for frequency
    """
    with allure.step("Create System object"):
        system = System(None)

    with allure.step('Test set log rotation frequency'):
        with allure.independent_step(f"Test rotate log files on {LogComponentsConsts.SYSLOG}"):
            _log_files_set_unset_log_rotation_frequency(engines, system.log)
        # component = get_random_component(system)
        # with allure.independent_step(f"Test rotate log files on {component.component_name}"):
        #     _log_files_set_unset_log_rotation_frequency(engines, component)


def _log_files_set_unset_log_rotation_frequency(engines, system_log_obj):
    list_with_possible_frequency = ['daily', 'weekly', 'monthly', 'yearly']

    with allure.step("Run nv show system log rotation command"):
        logging.info("Run nv show system log rotation command")
        show_output = system_log_obj.rotation.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()

    with allure.step("Get default values for rotation frequency"):
        logging.info("Get default values for rotation frequency")
        show_output = system_log_obj.rotation.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        default_frequency = output_dictionary['frequency']

    with allure.step('Negative validation for frequency with invalid value'):
        system_log_obj.rotation.set('frequency', 'invalid').verify_result(False)

    with allure.step("Validate configure all possible frequency"):
        logging.info("Validate configure all possible frequency")
        for frequency in list_with_possible_frequency:
            system_log_obj.rotation.set('frequency', frequency)
            NvueGeneralCli.apply_config(engines.dut)
            show_output = system_log_obj.rotation.show()
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
            ValidationTool.verify_field_value_in_output(output_dictionary, 'frequency', frequency).verify_result()

    with allure.step("Validate unset frequency"):
        logging.info("Validate unset frequency")
        system_log_obj.rotation.unset('frequency')
        NvueGeneralCli.apply_config(engines.dut)
        show_output = system_log_obj.rotation.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        ValidationTool.verify_field_value_in_output(output_dictionary, 'frequency', default_frequency).verify_result()


@pytest.mark.system
@pytest.mark.log
@pytest.mark.simx
def test_log_files_set_unset_log_rotation_size_disk_percentage(engines):
    """
    Check set unset for files rotation file size and disk percentage
    command: nv set/unset system log rotation size and sick-percentage
    Prune temp for NVOS - component

    Test flow:
        1. Negative testing for log file size
        2. Set, unset for log file size
        3. Positive and negative testing for disk percentage parameter
    """
    with allure.step("Create System object"):
        system = System(None)
    with allure.step('Test set log rotation size disk percentage'):
        with allure.independent_step(f"Test rotate log files on {LogComponentsConsts.SYSLOG}"):
            _log_files_set_unset_log_rotation_size_disk_percentage(engines, system.log)
        # component = get_random_component(system)
        # with allure.independent_step(f"Test rotate log files on {component.component_name}"):
        #     _log_files_set_unset_log_rotation_size_disk_percentage(engines, component)


def _log_files_set_unset_log_rotation_size_disk_percentage(engines, system_log_obj):
    with allure.step("Negative validation for rotation size"):
        logging.info("Negative validation for rotation size")
        show_output = system_log_obj.rotation.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        default_size = output_dictionary['size']
        system_log_obj.rotation.set('size', '0.0001').verify_result(False)
        system_log_obj.rotation.set('size', '3500.01').verify_result(False)

    with allure.step("Validate all possible rotation size"):
        logging.info("Validate all possible rotation size")
        system_log_obj.rotation.set('size', '0.001')
        NvueGeneralCli.apply_config(engines.dut)
        show_output = system_log_obj.rotation.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        ValidationTool.verify_field_value_in_output(output_dictionary, 'size', '0.001').verify_result()

        log_rotation_size = '3499.0'
        system_log_obj.rotation.set('size', log_rotation_size)
        NvueGeneralCli.apply_config(engines.dut)
        show_output = system_log_obj.rotation.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        ValidationTool.verify_field_value_in_output(output_dictionary, 'size', log_rotation_size).verify_result()
        system_log_obj.rotation.unset(op_param='size')

    with allure.step("Negative validation disk percentage configuration"):
        logging.info("Negative validation disk percentage configuration")
        system_log_obj.rotation.set('disk-percentage', '0.0001').verify_result(False)
        system_log_obj.rotation.set('disk-percentage', '100.001').verify_result(False)

    with allure.step("Validate disk percentage configuration"):
        disk_percentage_cases = [
            ('50.0', '1750.0', None),       # (percentage, expected_size, label) - None means use default label
            ('0.001', '0.035', 'lowest'),
            ('100.0', '3500.0', 'highest'),
        ]
        for pct, expected_size, label in disk_percentage_cases:
            desc = "Validate disk percentage configuration" + (f" with {label} value" if label else "")
            logging.info(desc)
            system_log_obj.rotation.set('disk-percentage', pct, apply=True)

            def _verify_disk_percentage(exp_size=expected_size, exp_pct=pct):
                show_output = system_log_obj.rotation.show()
                output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
                ValidationTool.verify_field_value_in_output(output_dictionary, 'size', exp_size).verify_result()
                ValidationTool.verify_field_value_in_output(output_dictionary, 'disk-percentage', exp_pct).verify_result()

            ValidationTool.retry_until_valid(_verify_disk_percentage,
                                             description=f"Waiting for size to reflect disk-percentage {pct}%")

    with allure.step("Validate unset log rotation"):
        logging.info("Validate unset log rotation")
        system_log_obj.rotation.unset()
        NvueGeneralCli.apply_config(engines.dut)
        show_output = system_log_obj.rotation.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        ValidationTool.verify_field_value_in_output(output_dictionary, 'size', default_size).verify_result()


@pytest.mark.system
@pytest.mark.log
@pytest.mark.simx
def test_log_files_set_unset_log_rotation_max_number(engines):
    """
    Check set unset for files rotation max-number
    command: nv set/unset system log rotation max-number

    Test flow:
        1. Negative validation for max-number
        2. Set possible value to max-number
        1. Log rotation max-number testing with files
        2. Unset log rotation and check default parameters
    """
    with allure.step("Create System object"):
        system = System(None)

    marker = TestToolkit.get_loganalyzer_marker(engines.dut)

    _log_files_set_unset_log_rotation_max_number(engines, system.log, "syslog")

    TestToolkit.add_loganalyzer_marker(engines.dut, marker)


def _log_files_set_unset_log_rotation_max_number(engines, system_log_obj, log_name_prefix):
    with allure.step("Negative validation for log rotation max-number"):
        logging.info("Negative validation for log rotation max-number")
        show_output = system_log_obj.rotation.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        default_max_number = output_dictionary['max-number']
        system_log_obj.rotation.set('max-number', '0.001').verify_result(False)
        result_obj = system_log_obj.rotation.set('max-number', '9999999')
        assert 'Valid range for max-number is 0 - 999999' in result_obj.get_info(False), "Set of invalid max-number should fail"

    with allure.step("Validate set max-number 5"):
        logging.info("Validate set max-number 5")
        system_log_obj.rotation.set('max-number', '5')
        NvueGeneralCli.apply_config(engines.dut)
        show_output = system_log_obj.rotation.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        ValidationTool.verify_field_value_in_output(output_dictionary, 'max-number', '5').verify_result()

        logging.info("Rotate log 5 times to check functionality of max-number")
        for i in range(0, 5):
            system_log_obj.rotate_logs()
            system_log_obj.write_to_log()

        logging.info("Check we have 5 log files")
        show_output = system_log_obj.file.show(op_param='list')
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        assert len(output_dictionary.keys()) >= 6, f"expected 5 log files, but there are {len(output_dictionary.keys())} instead"
        assert list(output_dictionary.keys())[-1] == f'{log_name_prefix}.5.gz', f"log file name does not match the expected format, the expected is {log_name_prefix}.5.gz and the current name is {list(output_dictionary.keys())[-1]}"

    with allure.step("Validate set max-number 1"):
        logging.info("Validate set max-number 1")
        system_log_obj.rotation.set('max-number', '1')
        NvueGeneralCli.apply_config(engines.dut)
        show_output = system_log_obj.rotation.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        ValidationTool.verify_field_value_in_output(output_dictionary, 'max-number', '1').verify_result()

        logging.info("Rotate log 1 time to check functionality of max-number")
        system_log_obj.rotate_logs()
        system_log_obj.write_to_log()

        logging.info("Check we have 1 log files")
        show_output = system_log_obj.file.show(op_param='list')
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        assert len(output_dictionary.keys()) >= 2, f"expected 1 log file, but there are {len(output_dictionary.keys())} instead"
        assert list(output_dictionary.keys())[-1] == f'{log_name_prefix}.1', \
            f"log file name does not match the expected format, the expected is {log_name_prefix}.1 and the current name is {list(output_dictionary.keys())[-1]}"

    with allure.step("Validate unset log rotation"):
        logging.info("Validate unset log rotation")
        system_log_obj.rotation.unset('max-number')
        NvueGeneralCli.apply_config(engines.dut)
        show_output = system_log_obj.rotation.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        ValidationTool.verify_field_value_in_output(output_dictionary, 'max-number', default_max_number).verify_result()


@pytest.mark.system
@pytest.mark.log
@pytest.mark.simx
def test_log_files_rotation_force(engines):
    """
    Check version on switch, rotate logs, check that it doesn't exist in the logs
    command: nv action system log rotation force

    Test flow:
        1. Run show system image
        2. Rotate logs
        3. Run nv show system log
        4. Check if we have in the logs 'regular_log' message
    """
    with allure.step("Create System object"):
        system = System(None)

    with allure.step("Run show command to view system image"):
        logging.info("Run show command to view system image")
        system.image.show()

    with allure.step("Rotate logs"):
        logging.info("Rotate logs")
        system.log.rotate_logs()

    with allure.step("Run nv show system log command follow to view system logs"):
        logging.info("Run nv show system log command follow to view system logs")
        show_output = system.log.file.show_log(exit_cmd='q')

    with allure.step('Verify updated “system/image” in the logs as expected'):
        logging.info('Verify updated “system/image” in the logs as expected')
        ValidationTool.verify_expected_output(show_output, 'system/image').verify_result(False)


@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.system
@pytest.mark.log
@pytest.mark.simx
def test_log_components(engines):
    """
    Check all fields in components command, check all components can be set/unset with all log levels

    Test flow:
        1. Run show system log component and validate all fields
        2. Run nv show system component and check default log levels for all components
        3. Run nv set/unset for all components with all log levels and validate
    """

    default_log_level_nvue = "info"
    list_with_all_components = LogComponentsConsts.COMPONENTS_LIST
    list_with_all_log_levels = LogComponentsConsts.LOG_LEVEL_LIST
    with allure.step("Create System object"):
        system = System(None)

    with allure.step("Run show command to view all log components"):
        show_output = system.log.component.show()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()

        with allure.step("Validate all expected fields in show component output"):
            ValidationTool.verify_field_exist_in_json_output(output_dictionary,
                                                             list_with_all_components).verify_result()
            logging.info("All expected fields were found")

    with allure.step("Validate default log levels for all components"):
        logging.info("Validate default log levels for all components")
        for component_name in list_with_all_components:
            show_output = system.log.component.component_id[component_name].show()
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
            default_log_level = LogComponentsConsts.NOTICE
            if component_name in (LogComponentsConsts.NVUE, LogComponentsConsts.SYMMETRY_MANAGER):
                default_log_level = default_log_level_nvue

            with allure.step("Validate component {component} with default log level {level}"
                             .format(component=component_name, level=default_log_level)):
                ValidationTool.verify_field_value_in_output(output_dictionary, LogComponentsConsts.LEVEL,
                                                            default_log_level).verify_result()
                logging.info("All expected components were with default log levels")

    with allure.step("Rotate logs"):
        logging.info("Rotate logs")
        system.log.rotate_logs()

    with allure.step("Validate all log levels (with random.choice) can be applied for all components"):
        for component_name in list_with_all_components:
            log_level = random.choice(list_with_all_log_levels)
            if component_name == LogComponentsConsts.NVUE and log_level == LogComponentsConsts.NOTICE:
                continue
            system.log.component.component_id[component_name].level.set(log_level, apply=True).verify_result()
            show_output = system.log.component.show()
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
            ValidationTool.verify_field_value_in_output(output_dictionary[component_name], LogComponentsConsts.LEVEL,
                                                        log_level).verify_result()
            if component_name == LogComponentsConsts.NVUE and log_level is list_with_all_log_levels[-1]:
                system.log.component.component_id[component_name].level.set(default_log_level_nvue, apply=True).verify_result()
            else:
                system.log.component.component_id[component_name].level.unset(apply=True).verify_result()
                show_output = system.log.component.show()
                output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
                ValidationTool.verify_field_value_in_output(
                    output_dictionary[component_name], LogComponentsConsts.LEVEL,
                    default_log_level_nvue if component_name in (LogComponentsConsts.NVUE, LogComponentsConsts.SYMMETRY_MANAGER)
                    else LogComponentsConsts.NOTICE).verify_result()

    with allure.step("Unset log components"):
        logging.info("Unset log components")
        system.log.component.unset(apply=True).verify_result()


@pytest.mark.system
@pytest.mark.log
def test_upload_log_files(engines, topology_obj):
    """
    Check uploading log files to shared location and validate
    command: nv action upload system log file

    Test flow:
        1. Upload log file to shared location
        2. Validate it uploaded
        3. Delete from shared location
        4. Check if in history login and password hided
    """
    with allure.step("Create System object"):
        system = System(None)
        system.log.rotate_logs()
    with allure.step('Test upload log files'):
        with allure.independent_step(f"Test upload log files on {LogComponentsConsts.SYSLOG}"):
            _upload_log_files(topology_obj, system.log)
        component = system.log.component.component_id[LogComponentsConsts.NVUE]  # for now only nvue component has logs
        with allure.independent_step(f"Test upload log files on {component.get_resource_basename()}"):
            _upload_log_files(topology_obj, component)


def _upload_log_files(topology_obj, system_log_obj):
    player = topology_obj.players['sonic-mgmt']['engine']

    with allure.step("Get and upload log file"):
        show_output = system_log_obj.file.show(op_param='list')
        log_files_dict = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
        log_file = list(log_files_dict.keys())[-1]
        upload_path = 'scp://{}:{}@{}/root/{}'.format(player.username, player.password, player.ip, log_file)
        system_log_obj.file.file_id[log_file].action_upload(upload_path=upload_path)

    with allure.step("Check if file uploaded and delete it from player"):
        assert player.run_cmd(cmd='ls -la | grep {}'.format(log_file)), "it appears the file does not exist in the target path"
        player.run_cmd(cmd='rm -f {}'.format(log_file))

    with allure.step("Run nv show system log command to check if command with password hidden"):
        show_output = system_log_obj.file.show_log(exit_cmd='q')
        ValidationTool.verify_expected_output(show_output, upload_path).verify_result(False)


@pytest.mark.system
@pytest.mark.log
@pytest.mark.simx
@pytest.mark.disable_loganalyzer
@pytest.mark.cumulus
def test_delete_log_files(engines, topology_obj):
    """
    Check uploading log files to shared location and validate
    command: nv action upload system log file

    Test flow:
        1. Upload log file to shared location
        2. Validate it uploaded
        3. Delete from shared location
        4. Check if in history login and password hided
    """
    with allure.step("Create System object"):
        system = System(None)
        system.log.rotate_logs()

    with allure.step('Test delete log files'):
        with allure.independent_step(f"Test delete log files on {LogComponentsConsts.SYSLOG}"):
            _delete_log_files(engines, system.log, file_name=LogComponentsConsts.SYSLOG)
        component = system.log.component.component_id[LogComponentsConsts.NVUE]  # for now only nvue component has logs

        with allure.independent_step(f"Test delete log files on {component.get_resource_basename()}"):
            _delete_log_files(engines, component, file_name=LogComponentsConsts.NVUE_LOG)


def _delete_log_files(engines, system_log_obj, file_name):
    """
    Check user can delete debug-log files
    command: nv action delete system log file

    Test flow:
        1. Rotate log to create log files and check created
        2. Get all log files and check we can delete it
        3. Check we didn't delete all log files
        4. Run show system image
        5. Check it exist in log
    """
    system = System()
    platform = Platform()
    with allure.step("Rotate log 5 times to create log files"):
        for i in range(0, 5):
            system_log_obj.rotate_logs()

        with allure.step("Check we have 5 log files"):
            show_output = system_log_obj.file.show(op_param='list')
            log_files_dict = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
            assert len(log_files_dict.values()) >= 6, "Not all 5 log files were created"

    with allure.step("Delete all log files and validate"):

        with allure.step("Get current size of " + file_name):
            output = engines.dut.run_cmd("stat /var/log/{} | grep Size".format(file_name))
            assert output, f"Can't find {file_name} file"
            prev_log_size = output.split()[1]

        logs_names_to_delete = list(log_files_dict.keys())
        logs_names_to_delete.remove(file_name)
        if LogComponentsConsts.NVUE_CLI_LOG in logs_names_to_delete:
            logs_names_to_delete.remove(LogComponentsConsts.NVUE_CLI_LOG)
        left_files = logs_names_to_delete.copy()

        for log_file in logs_names_to_delete:

            with allure.step("Delete {} file".format(log_file)):
                system_log_obj.file.file_id[log_file].action_delete()
                left_files.remove(log_file)

                with allure.step("Verify only {} was deleted".format(log_file)):
                    output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system_log_obj.file.show(op_param='list')) \
                        .get_returned_value()

                    with allure.step("Verify {} was deleted".format(log_file)):
                        assert log_file not in output_dictionary.keys(), log_file + " was not actually deleted"

                    with allure.step("Verify other files were not deleted"):
                        if left_files:
                            ValidationTool.verify_field_exist_in_json_output(output_dictionary,
                                                                             left_files).verify_result()
        if file_name == LogComponentsConsts.SYSLOG:
            with allure.step("Verify syslog file was deleted and a new one was created"):
                with allure.step("Save log analyzer marker before deleting the log file"):
                    marker = TestToolkit.get_loganalyzer_marker(engines.dut, use_sudo=TestToolkit.is_eth_dut())

                with allure.step(f"Delete {file_name} log file"):
                    system_log_obj.file.file_id[file_name].action_delete()
                    if TestToolkit.is_eth_dut():
                        engines.dut.run_cmd("sudo systemctl restart rsyslog", validate=False)
                    with allure.step("Add log analyzer marker for the new log file"):
                        TestToolkit.add_loganalyzer_marker(engines.dut, marker)
                    output = engines.dut.run_cmd("stat /var/log/{} | grep Size".format(file_name))
                    assert output, f"Can't find {file_name} file"
                    curr_log_size = int(output.split()[1])
                    # Only require new file smaller when old file was large; if old was tiny, new file
                    # can be larger after restart/markers/logging.
                    assert curr_log_size < int(prev_log_size) or int(prev_log_size) < 500, (
                        f"{file_name} file probably was not deleted (curr={curr_log_size}, prev={prev_log_size})"
                    )

            with allure.step("Rotate logs"):
                system_log_obj.rotate_logs()

            with allure.step("Run show command to view platform firmware"):
                platform.firmware.show()

            firmware_log_marker = (
                TestToolkit.devices.dut.system_log_value
                if TestToolkit.devices.dut.system_log_value != "system log"
                else "part-number"
            )
            with allure.step(f"Verify firmware marker in {LogsSources.NVUED}"):
                with sudo_scope_if(TestToolkit.is_eth_dut()):
                    system.log.verify_expected_logs(
                        logs_to_find=[firmware_log_marker],
                        logs_source=LogsSources.NVUED,
                        engine=engines.dut,
                        only_latest_log=True)

            with allure.step("Run show command to view system image"):
                start_time = datetime.strptime(ClockTools.get_local_time_from_show_system_date_time_output(system.datetime.show()),
                                               BugHandlerConst.TIMESTAMP_FORMATS[4])
                system.image.show()

            with allure.step("Run nv show system log command follow to view system logs"):
                with sudo_scope_if(TestToolkit.is_eth_dut()):
                    output = system_log_obj.file.show_log(exit_cmd='q')
                last_line = output.splitlines()[-4]
                date_match = re.search(fr'{TestToolkit.devices.dut.system_log_timestamp_regex}', last_line)
                date_str = TestToolkit.devices.dut.get_system_log_date_str_for_parsing(date_match.group())
                parsed_time = datetime.strptime(date_str, BugHandlerConst.TIMESTAMP_FORMATS[3])
                parsed_time = parsed_time.replace(year=datetime.now().year)
                assert start_time <= parsed_time, "Syslog file does not contain new data, system might be"


@pytest.mark.check_log_size(expect=10000)
@pytest.mark.system
@pytest.mark.log
@pytest.mark.simx
def test_log_idle(engines):
    """
    Verify system generates minimal logs during idle state.

    Test flow:
        1. Check IPv6 status and configure DHCP pattern if needed
        2. Renew DHCP on all eth interfaces
        3. Rotate logs and record initial size
        4. Wait 10 minutes (idle period)
        5. Verify no unexpected log entries
        6. Verify log file size growth is within threshold
    """
    # Maximum allowed syslog growth during idle period (in bytes)
    expected_file_size_diff = 9000
    # Duration to wait in idle state
    idle_duration_sec = 10 * MINUTE
    # Time to wait for interface lock release after DHCP action (in seconds)
    interface_lock_release_time = 5
    # Time to wait for DHCP configuration to stabilize (in seconds)
    dhcp_stabilization_time = 60

    # DHCP solicit pattern for when IPv6 is not working
    dhcp_solicit_pattern = (
        r'\w{3}\s+\d+\s+\d+:\d+:[\d.]+\s+[\w-]+\s+INFO dhclient\[\d+\]: XMT: Solicit on eth\d+, interval \d+ms\.[\n]?',
        30
    )

    list_of_expected_patterns: List[tuple[str, int | None]] = [
        (r'\w{3}\s+\d+\s+\d+\:\d+\:[\d\.]+\s+[\w\-]+\s+INFO\s*.+\s*(Starting system activity accounting tool|Starting Rotate log files\.\.\.|logrotate\.service: Succeeded\.|Finished Rotate log files\.)[^\n]*[\n]?', 4),
        (r'\w{3}\s+\d+\s+\d+\:\d+\:[\d\.]+\s+[\w\-]+\s+INFO\s*.+\s*(sysstat-collect\.service: Succeeded|Finished system activity accounting tool\.)[^\n]*[\n]?', 2),
        (r'\w{3}\s+\d+\s+\d+\:\d+\:[\d\.]+\s+[\w\-]+\s+INFO nvued\s*.+\s*(log\s*rotate|Log\s*rotate)[^\n]*[\n]?', 3),
        (r'\w{3}\s+\d+\s+\d+\:\d+\:[\d\.]+\s+[\w\-]+\s+INFO nvued.+Ran Job running ActionKey\(\'@rotate\', \'/system/log\', \(\), \d+, \(\)\)[^\n]*[\n]?', 1),
        (r'\w{3}\s+\d+\s+\d+\:\d+\:[\d\.]+\s+[\w\-]+\s+INFO nvued.+\"GET /nvue_v1/action/\d+ HTTP/1\.1\" 200 -[^\n]*[\n]?', None),
        (r'\w{3}\s+\d+\s+\d+\:\d+\:[\d\.]+\s+[\w\-]+\s+INFO .+ Backup logs.*[\n]?', None),
        (r'\w{3}\s+\d+\s+\d+\:\d+\:[\d\.]+\s+[\w\-]+\s+DEBUG nvued.+[\n]?', None),
        (r'\w{3}\s+\d+\s+\d+\:\d+\:[\d\.]+\s+[\w\-]+\s+INFO systemd\[\d+\]\: sysstat-collect.service: Deactivated successfully.*[\n]?', 1),
        (r'\w{3}\s+\d+\s+\d+\:\d+\:[\d\.]+\s+[\w\-]+\s+INFO systemd\[\d+\]\: logrotate.service: Deactivated successfully.*[\n]?', 1),
        (r'\w{3}\s+\d+\s+\d+\:\d+\:[\d\.]+\s+[\w\-]+\s+INFO systemd.*Deactivated successfully.*[\n]?', 10),
        # healthd runs approximately every 3 seconds, calculate expected iterations
        (r'\w{3}\s+\d+\s+\d+\:\d+\:[\d\.]+\s+[\w\-]+\s+INFO healthd\[\d+\]\: System health takes [\d\.]+ seconds for one iteration[\n]?', math.ceil(idle_duration_sec / 3))
    ]

    with allure.step("Create System object"):
        system = System(None)

    with allure.step("Check IPv6 status and add DHCP pattern if not working"):
        interface = Interface(None)
        eth_port_list = [Port(interface_name) for interface_name in re.findall(r'eth\d+', interface.show())]
        for port in eth_port_list:
            ipv6_output = port.interface.ipv6.get_primary_ip_address()
            logger.info(f"IPv6 output from get_primary_ip_address: {ipv6_output}")
            if not ipv6_output:
                # If IPv6 is not working, add DHCP solicit pattern to expected logs
                # [NVOS - Design] Bug SW #4715286: [Non-Functional] [Syslog] | unexpected logs found during idle
                logger.info(f"DEBUG: get_primary_ip_address returned empty for port {port.name}")
                logger.info(f"IPv6 not working, adding DHCP solicit pattern to expected logs")
                list_of_expected_patterns.append(dhcp_solicit_pattern)
                break

    with allure.step("Renew DHCP"):
        for port in eth_port_list:
            port.interface.ipv4.action_renew_dhcp_client().verify_result()
            time.sleep(interface_lock_release_time)
            port.interface.ipv6.action_renew_dhcp_client().verify_result()
            time.sleep(interface_lock_release_time)
        time.sleep(dhcp_stabilization_time)

    with allure.step("Rotate the log"):
        system.log.rotate_logs()
        syslog_size_before_idle = FilesTool.get_file_size_in_bytes(engines.dut, SyslogConsts.SYSLOG_LOG_PATH)

    with allure.step("Do nothing for 10 min"):
        time.sleep(idle_duration_sec)

    with allure.step("Verify log file content and size"):
        max_unexpected_lines_to_show = 50

        with allure.independent_step("Verify log file size"):
            syslog_size_after_idle = FilesTool.get_file_size_in_bytes(engines.dut, SyslogConsts.SYSLOG_LOG_PATH)
            assert syslog_size_before_idle >= 0, f"Failed to get syslog size before idle: {syslog_size_before_idle}"
            assert syslog_size_after_idle >= 0, f"Failed to get syslog size after idle: {syslog_size_after_idle}"
            assert syslog_size_after_idle - syslog_size_before_idle <= expected_file_size_diff, \
                f"The size of the log file is more than expected threshold, before: {syslog_size_before_idle}b, " \
                f"after: {syslog_size_after_idle}b, expected_threshold value: {expected_file_size_diff}"

        syslog_idle_output: str = engines.dut.run_cmd(f'cat {SyslogConsts.SYSLOG_LOG_PATH}')
        logs_after_check: str = syslog_idle_output

        # First pass: check pattern counts and collect failures
        pattern_count_failures = []
        for pattern, max_count in list_of_expected_patterns:
            if max_count is not None:
                actual_count = len(re.findall(pattern, logs_after_check))
                if actual_count > max_count:
                    pattern_count_failures.append(f'Pattern "{pattern}" matched {actual_count} times, maximum allowed {max_count}')
            logs_after_check = re.sub(pattern, '', logs_after_check)

        with allure.independent_step("Verify expected pattern counts"):
            assert not pattern_count_failures, '\n'.join(pattern_count_failures)

        with allure.independent_step("Verify no unexpected log lines"):
            unexpected_lines = [line for line in logs_after_check.splitlines() if line.strip()]
            if unexpected_lines:
                lines_to_show = unexpected_lines[:max_unexpected_lines_to_show]
                logger.info("=" * 80)
                logger.info(f"UNEXPECTED LOG LINES FOUND DURING IDLE ({len(unexpected_lines)} total, showing first {len(lines_to_show)}):")
                logger.info("=" * 80)
                for i, line in enumerate(lines_to_show, 1):
                    logger.info(f"  [{i}] {line}")
                if len(unexpected_lines) > max_unexpected_lines_to_show:
                    logger.info(f"  ... and {len(unexpected_lines) - max_unexpected_lines_to_show} more lines")
                logger.info("=" * 80)
            assert len(unexpected_lines) == 0, \
                f'Found {len(unexpected_lines)} unexpected log lines during idle (showing first {min(len(unexpected_lines), max_unexpected_lines_to_show)}):\n' + \
                '\n'.join(f'  [{i}] {line}' for i, line in enumerate(unexpected_lines[:max_unexpected_lines_to_show], 1))


def get_random_component(system):
    show_output = system.log.component.show()
    output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
    component_name = random.choice(list(output_dictionary.keys()))
    return system.log.component.component_id[component_name]


@retry(Exception, tries=2, delay=2)
def _validate_system_log_rotation_output(system_log_obj, attribute, expected_value):
    show_output = system_log_obj.rotation.show()
    output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
    ValidationTool.verify_field_value_in_output(output_dictionary, attribute, expected_value).verify_result()
