from datetime import datetime, timedelta
import base64
import logging
import random
import pytest
import shlex
import time
import os

import pandas as pd

from devts.infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine
from ngts.ngts_types import DevicesT, EnginesT

from ngts.nvos_constants.constants_nvos import ApiType, DatabaseConst, NvosConst, LogsSources
from ngts.tests_nvos.system.stats.constants import StatsConsts
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.DatabaseTool import DatabaseTool
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure
from retry import retry

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def cleanup_verification_test(engines):
    """Remove leftover verification_test file from /var/stats before each test."""
    engines.dut.run_cmd("sudo rm -rf /var/stats/verification_test")
    yield


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simxl
def test_system_stats_configuration(engines: EnginesT, devices: DevicesT, random_api: str):
    """
    validate:
    - Enable/Disable stats feature
    - Show system stats commands
    - Update stats category configuration
    - System stats actions (clear, delete)
    - Sampling interval and cache duration mechanism
    - New file (internal) is generated after clear command.

    Test flow:
    1. Disable system stats feature
    2. Disable all categories stats
    3. Clear all system stats and delete stats files
    4. Check internal and external files
    5. Select a random category and unset its configuration
    6. Update general configuration
    7. Update category configuration
    8. Wait 1 minute
    9. Check no files created when feature state is disabled
    10.	Enable feature and disable category
    11.	Wait 1 minute
    12.	Check no files created when feature state is disabled
    13.	Update general configuration
    14.	Enable category
    15.	Wait 2 minutes
    16.	Check internal and external files
    17.	Wait another 1 minute
    18.	Check internal and external files
    """

    system = System(devices_dut=devices.dut)
    engine = engines.dut
    category_list = devices.dut.category_list
    category_disabled_dict = devices.dut.category_disabled_dict
    category_list_default = devices.dut.category_list_default_dict

    try:
        with allure.step("Set system stats feature to default"):
            system.stats.unset(apply=True).verify_result()

        with allure.step("Disable system stats feature"):
            system.stats.set(op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.DISABLED.value,
                             apply=True).verify_result()
            stats_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.show()).get_returned_value()
            assert stats_show[StatsConsts.STATE] == StatsConsts.State.DISABLED.value, \
                "stats state parameter is expected to be 'disabled'"

        with allure.step("Disable all categories stats"):
            for name in category_list:
                system.stats.category.categoryName[name].set(
                    op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.DISABLED.value).verify_result()
            SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].
                                            apply_config, TestToolkit.engines.dut, False).verify_result()
            stats_category_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.category.show()). \
                get_returned_value()
            ValidationTool.compare_dictionary_content(stats_category_show, category_disabled_dict).verify_result()

        with allure.step("Clear all system stats and delete stats files"):
            clear_all_internal_and_external_files(engine, system, category_list)

        with allure.step("Check both internal and external paths"):
            output = engine.run_cmd("ls /var/stats")
            assert not output or "No such file or directory" in output, "Category internal files were not cleared"
            stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()). \
                get_returned_value()
            assert not stats_files_show, "External stats files should not exist"

        with allure.step("Select a random category and unset its configuration"):
            name = RandomizationTool.select_random_value(category_list).get_returned_value()
            system.stats.category.categoryName[name].unset(apply=True).verify_result()
            stats_category_show = OutputParsingTool.parse_json_str_to_dictionary(
                system.stats.category.categoryName[name].show()).get_returned_value()
            ValidationTool.compare_dictionary_content(stats_category_show, category_list_default[name]). \
                verify_result()

        with allure.step("Update cache duration to 1 minute"):
            set_cache_duration(engine, 1)

        with allure.step("Update category configuration"):
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.HISTORY_DURATION, op_param_value=int(StatsConsts.HISTORY_DURATION_MIN)).\
                verify_result()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.INTERVAL, op_param_value=int(StatsConsts.INTERVAL_MIN), apply=True).\
                verify_result()
            stats_category_show = OutputParsingTool.parse_json_str_to_dictionary(
                system.stats.category.categoryName[name].show()).get_returned_value()
            ValidationTool.compare_dictionary_content(stats_category_show, StatsConsts.CATEGORY_MIN_DICT).\
                verify_result()

        with allure.step("Restart process..."):
            engine.run_cmd("sudo systemctl restart stats-reportd")

        with allure.step("Check no files created when feature state is disabled"):
            output = engine.run_cmd("ls /var/stats")
            assert not output or "No such file or directory" in output, "Category internal files were not cleared"
            stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()). \
                get_returned_value()
            assert not stats_files_show, "External stats files should not exist"

        with allure.step("Enable feature and disable category"):
            system.stats.set(op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.ENABLED.value). \
                verify_result()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.DISABLED.value, apply=True). \
                verify_result()
            stats_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.show()).get_returned_value()
            stats_category_show = OutputParsingTool.parse_json_str_to_dictionary(
                system.stats.category.categoryName[name].show()).get_returned_value()
            assert stats_show[StatsConsts.STATE] == StatsConsts.State.ENABLED.value, \
                "stats state parameter is expected to be 'enabled'"
            assert stats_category_show[StatsConsts.STATE] == StatsConsts.State.DISABLED.value, \
                "stats state parameter is expected to be 'disabled'"

        with allure.step("Wait 15 seconds..."):
            time.sleep(StatsConsts.SLEEP_15_SECONDS)

        with allure.step("Check no files created when category state is disabled"):
            output = engine.run_cmd("ls /var/stats")
            assert not output or "No such file or directory" in output, "Category internal files were not cleared"
            stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()). \
                get_returned_value()
            assert not stats_files_show, "External stats files should not exist"

        with allure.step("Enable category"):
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.ENABLED.value, apply=True). \
                verify_result()
            stats_category_show = OutputParsingTool.parse_json_str_to_dictionary(
                system.stats.category.categoryName[name].show()).get_returned_value()
            assert stats_category_show[StatsConsts.STATE] == StatsConsts.State.ENABLED.value, \
                "stats state parameter is expected to be 'enabled'"

        with allure.step("Wait another 1 minute..."):
            time.sleep(StatsConsts.SLEEP_1_MINUTE)

        with allure.step("Check both internal and external paths"):
            output = engine.run_cmd("ls /var/stats")
            assert output == name + '.csv', "Category internal file does not exist, or not the only one that exists"
            stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()). \
                get_returned_value()
            assert not stats_files_show, "External stats files should not exist"

    finally:
        set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
def test_system_stats_generation(engines: EnginesT, devices: DevicesT, random_api: str):
    """
    validate:
    - Enable/Disable stats feature
    - Show system stats commands
    - Update stats category configuration
    - System stats actions (generate and upload)
    - External file - name, header and content

    Test flow:
    1. Set stats feature to default
    2. Update general configuration
    3. Update all categories stats states
    4. Wait 3 minutes
    5. Generate system stats category
    6. Upload stats file to URL
    7. Delete stats file
    8. Clear system stats specific category
    9. Wait 3 minutes
    10.	Check internal and external files
    11.	Validate sample timestamps
    """

    system = System(devices_dut=devices.dut)
    engine = engines.dut
    player = engines['sonic_mgmt']
    category_list = devices.dut.category_list
    category_list_default = devices.dut.category_list_default_dict

    try:

        with allure.step("Clear all system stats and delete stats files"):
            clear_all_internal_and_external_files(engine, system, category_list)

        with allure.step("Set Stats feature to default"):
            system.stats.unset(op_param=StatsConsts.STATE, apply=True).verify_result()
            stats_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.show()).get_returned_value()
            assert stats_show[StatsConsts.STATE] == StatsConsts.State.ENABLED.value, \
                "stats state parameter is expected to be 'enabled'"

        with allure.step("Update cache duration to 3 minutes"):
            set_cache_duration(engine, 3)

        with allure.step("Update all categories interval values to minimum and states to enable"):
            configure_all_categories(system, category_list,
                                     interval=StatsConsts.INTERVAL_MIN,
                                     state=StatsConsts.State.ENABLED.value)

        with allure.step("Restart process and check internal path"):
            engine.run_cmd("sudo systemctl restart stats-reportd")
            check_category_internal_files_exist(engine, category_list)

        with allure.step("Generate system stats category"):
            name = RandomizationTool.select_random_value(category_list).get_returned_value()
            system.stats.category.categoryName[name].action_general(StatsConsts.GENERATE).verify_result()
            stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()).\
                get_returned_value()
            file_name = list(stats_files_show)[0]
            assert len(stats_files_show) == 1, "Expected only 1 file"
            assert name in file_name, "Expected category file does not exist"

        with allure.step("Upload stats file to URL"):
            validate_upload_stats_file(engines, system, file_name, True)

        with allure.step("Validate show file"):
            show_output = system.stats.files.file_name[file_name].show_file(exit_cmd='q')
            if 'NVUE' == TestToolkit.tested_api:
                assert name in show_output, "show file is missing category name"

        with allure.step("Delete stats external file"):
            system.stats.files.file_name[file_name].action_delete().verify_result()
            output = engine.run_cmd("ls /var/stats")
            assert name in output, "Category internal file not exists"
            stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()).\
                get_returned_value()
            assert not stats_files_show, "External stats files should not exist"

        with allure.step("Clear system stats specific category"):
            clear_time = datetime.now()
            system.stats.category.categoryName[name].action_general(StatsConsts.CLEAR).verify_result()
            output = engine.run_cmd("ls /var/stats")
            assert not output or name not in output, "Category internal file was not cleared"

        with allure.step("Wait another 3 minutes..."):
            time.sleep(StatsConsts.SLEEP_3_MINUTES)

        with allure.step("Check both internal and external paths"):
            output = engine.run_cmd("ls /var/stats")
            assert name in output, "Category internal file does not exist, or not the only one that exists"
            stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()).\
                get_returned_value()
            assert not stats_files_show, "External stats files should not exist"

        with allure.step("Generate and upload stats file to URL"):
            system.stats.category.categoryName[name].action_general(StatsConsts.GENERATE).verify_result()
            stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()).\
                get_returned_value()
            file_name = list(stats_files_show)[0]
            validate_upload_stats_file(engines, system, file_name, False)

        with allure.step("Validate samples timestamps is older than clear time"):
            validate_external_file_timestamps(file_name, clear_time)

        with allure.step("Delete uploaded file"):
            player.run_cmd(cmd='rm -f {}{}'.format(NvosConst.MARS_RESULTS_FOLDER, file_name))

        with allure.step("Select a random category and set its configuration to minimum values"):
            name = RandomizationTool.select_random_value(category_list).get_returned_value()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.INTERVAL, op_param_value=int(StatsConsts.INTERVAL_MIN)).verify_result()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.DISABLED.value).verify_result()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.HISTORY_DURATION,
                op_param_value=int(StatsConsts.HISTORY_DURATION_MIN), apply=True).verify_result()
            stats_category_show = OutputParsingTool.parse_json_str_to_dictionary(
                system.stats.category.categoryName[name].show()).get_returned_value()
            ValidationTool.compare_dictionary_content(stats_category_show, StatsConsts.CATEGORY_MIN_DISABLED_DICT).\
                verify_result()

        with allure.step("Verify unset each category parameter configuration"):
            system.stats.category.categoryName[name].unset(op_param=StatsConsts.INTERVAL).verify_result()
            system.stats.category.categoryName[name].unset(op_param=StatsConsts.HISTORY_DURATION).verify_result()
            system.stats.category.categoryName[name].unset(op_param=StatsConsts.STATE, apply=True).verify_result()

            stats_category_show = OutputParsingTool.parse_json_str_to_dictionary(
                system.stats.category.categoryName[name].show()).get_returned_value()
            ValidationTool.compare_dictionary_content(stats_category_show, category_list_default[name]).\
                verify_result()

        with allure.step("Select a random category and set its configuration to minimum values"):
            name = RandomizationTool.select_random_value(category_list).get_returned_value()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.INTERVAL, op_param_value=int(StatsConsts.INTERVAL_MIN)).verify_result()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.DISABLED.value).verify_result()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.HISTORY_DURATION,
                op_param_value=int(StatsConsts.HISTORY_DURATION_MIN), apply=True).verify_result()
            stats_category_show = OutputParsingTool.parse_json_str_to_dictionary(
                system.stats.category.categoryName[name].show()).get_returned_value()
            ValidationTool.compare_dictionary_content(stats_category_show, StatsConsts.CATEGORY_MIN_DISABLED_DICT).\
                verify_result()

        with allure.step("Verify unset category configuration"):
            system.stats.category.categoryName[name].unset(apply=True).verify_result()

    finally:
        set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.dut_reboot
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_system_stats_performance(engines: EnginesT, devices: DevicesT, test_api: str):
    """
    validate:
    - Generate all system stats command
    - Time to generate "all" categories < 2 secs
    - Cleanup mechanism (after reboot)

    Test flow:
    1. Set stats feature to default
    2. Update general configuration
    3. Select a random category
    4. Update selected category stats config.
    5. Create category internal file with old samples
    6. Restart stats process
    7. Wait 1 minute
    8. Verify cleanup after reboot
    9.Update all categories stats states
    10.	Wait 1 minute
    11.	Generate all system files and verify action time
    12.	Upload stats file to URL
    13.	Validate file content
    """

    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    category_list = devices.dut.category_list
    category_disabled_dict = devices.dut.category_disabled_dict
    player_engine = engines['sonic_mgmt']

    try:
        with allure.step("Set Stats feature to default"):
            system.stats.unset(apply=True).verify_result()
            stats_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.show()).get_returned_value()
            assert stats_show[StatsConsts.STATE] == StatsConsts.State.ENABLED.value, \
                "stats state parameter is expected to be 'enabled'"

        with allure.step("Clear all system stats and delete stats files"):
            clear_all_internal_and_external_files(engine, system, category_list)

        with allure.step("Update cache duration to 1 minute"):
            set_cache_duration(engine, 1)

        with allure.step("Select a random category"):
            name = RandomizationTool.select_random_value(list(category_disabled_dict.keys())). \
                get_returned_value()

        with allure.step("Update selected category stats config"):
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.INTERVAL, op_param_value=int(StatsConsts.INTERVAL_MIN)).verify_result()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.HISTORY_DURATION, op_param_value=int(StatsConsts.HISTORY_DURATION_MIN)).\
                verify_result()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.ENABLED.value, apply=True).\
                verify_result()
            SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].
                                            save_config, TestToolkit.engines.dut).verify_result()

        with allure.step("Create category internal file with old samples"):
            create_old_samples_internal_file(engine, name)

        with allure.step("Perform system reboot"):
            system.reboot.action_reboot(params='force').verify_result()

        with allure.step("Wait 20 seconds..."):
            time.sleep(StatsConsts.SLEEP_20_SECONDS)

        with allure.step("Check internal files were created"):
            check_category_internal_files_exist(engine, category_list)

        with allure.step("Clear all external files"):
            engine.run_cmd("sudo rm -f /host/stats/*.csv")

        with allure.step("Generate and upload stats file to URL"):
            system.stats.category.categoryName[name].action_general(StatsConsts.GENERATE).verify_result()
            current_time = datetime.now()
            history_time = current_time - timedelta(days=int(StatsConsts.HISTORY_DURATION_MIN))
            stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()).\
                get_returned_value()
            file_name = list(stats_files_show)[0]
            validate_upload_stats_file(engines, system, file_name, False)

        with allure.step("Verify cleanup after restart process"):
            validate_external_file_timestamps(file_name, history_time)

        with allure.step("Delete uploaded file"):
            player_engine.run_cmd(cmd='rm -f {}{}'.format(NvosConst.MARS_RESULTS_FOLDER, file_name))

        with allure.step("Clear all external files"):
            engine.run_cmd("sudo rm -f /host/stats/*.csv")

        with allure.step("Generate all system files and verify action time"):
            start_time = time.time()
            system.stats.action_general(StatsConsts.GENERATE).verify_result()
            end_time = time.time()
            diff_time = end_time - start_time
            stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()).\
                get_returned_value()
            file_name = list(stats_files_show)[0]
            assert len(stats_files_show) == 1, "Expected only 1 file"
            assert 'tar' in file_name, "tar file does not exist"
            assert diff_time < StatsConsts.GENERATE_ALL_TIME_MAX, "Generate all time is higher than allowed"

        with allure.step("Upload stats file to URL"):
            validate_upload_stats_file(engines, system, file_name, False)

        with allure.step("Extract the file and check that all categories file exist"):
            # engine.run_cmd('sudo tar -xf ' + NvosConst.MARS_RESULTS_FOLDER + file_name + ' -C /tmp')
            # folder_name = techsupport.replace('.tar.gz', "")
            # output = engine.run_cmd('ls ' + folder_name + '/stats')

            engine.run_cmd("sudo tar -xf {}{}".format(NvosConst.MARS_RESULTS_FOLDER, file_name))
            file_split = file_name.split("_")
            report_path = os.path.splitext('report_{}_{}'.format(file_split[-2], file_split[-1]))[0]
            _ = engine.run_cmd("ls {}{}".format(NvosConst.MARS_RESULTS_FOLDER, report_path))
            # for name in category_list:
            #     assert name in output, f"{name} external file is missing in tar file"

        with allure.step("Delete uploaded file"):
            player_engine.run_cmd(cmd='rm -f {}{}'.format(NvosConst.MARS_RESULTS_FOLDER, file_name))

    finally:
        set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.dut_reboot
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_stats_reliability(engines: EnginesT, devices: DevicesT, test_api: str):
    """
    validate:
    - Configuration of the feature persists through upgrade and reboot
    - Configuring/return to default the feature in the loop should be stable
    - No unexpected behavior (access violation, leak etc.) when processing malformed input
        (e.g. malformed/missing config in DB)

    Test flow:
    1. Set stats feature to default
    2. Update general configuration
    3. Update all categories stats states
    4. Wait 1 minute
    5. Check internal path
    6. Perform system reboot
    7. Check feature and categories config.
    8. Check internal path
    9. Configuring the feature to default in the loop
    10.	Remove sampled data from DB
    """

    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    category_list = devices.dut.category_list
    try:
        with allure.step("Set Stats feature to default"):
            system.stats.unset(apply=True).verify_result()

        with allure.step("Update cache duration to 1 minute"):
            set_cache_duration(engine, 1)

        with allure.step("Update all categories stats states"):
            configure_all_categories(system, category_list,
                                     state=StatsConsts.State.ENABLED.value,
                                     interval=StatsConsts.INTERVAL_MIN,
                                     history_duration=StatsConsts.HISTORY_DURATION_MIN)
            SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].
                                            save_config, TestToolkit.engines.dut).verify_result()

        with allure.step("restart stats service and check internal path"):
            engine.run_cmd("sudo systemctl restart stats-reportd")
            with allure.step("Wait 15 seconds..."):
                time.sleep(StatsConsts.SLEEP_15_SECONDS)
            # Remove stale rotated <cat>.csv.old files: they survive the stats-reportd restart
            # and are legitimate rotation artifacts, but an unlisted <cat>.csv.old (fan/power/
            # temperature/... rotate depending on timing) trips the "extra files" assertion.
            # Clear them so the check validates only the freshly-regenerated base <cat>.csv files.
            engine.run_cmd("sudo rm -f /var/stats/*.old")
            check_category_internal_files_exist(engine, category_list)

        with allure.step("Perform system reboot"):
            system.reboot.action_reboot(params='force').verify_result()

        with allure.step("Check feature and categories configurations"):
            stats_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.show()).get_returned_value()
            assert stats_show[StatsConsts.STATE] == StatsConsts.State.ENABLED.value, \
                "stats state parameter is expected to be 'enabled'"
            for name in category_list:
                stats_category_show = OutputParsingTool.parse_json_str_to_dictionary(
                    system.stats.category.categoryName[name].show()).get_returned_value()
                ValidationTool.compare_dictionary_content(stats_category_show, StatsConsts.CATEGORY_MIN_DICT).\
                    verify_result()

        with allure.step("Check internal path"):
            check_category_internal_files_exist(engine, category_list)

        with allure.step("Configuring the feature to default in the loop"):
            for x in range(10):
                system.stats.unset(op_param=StatsConsts.STATE, apply=True).verify_result()
            stats_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.show()).get_returned_value()
            assert stats_show[StatsConsts.STATE] == StatsConsts.State.ENABLED.value, \
                "stats state parameter is expected to be 'enabled'"

        with allure.step("Remove sampled data from DB"):
            # TODO: delete a sampled parameter from DB
            stats_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.show()).get_returned_value()
            assert stats_show[StatsConsts.STATE] == StatsConsts.State.ENABLED.value, \
                "stats state parameter is expected to be 'enabled'"
            system.log.rotate_logs()
            # Exempt 'Not Found': benign PCIe/pmon messages in log (e.g. "PCIe Device: ... Not Found") are not failures.
            _ = system.log.file.show_log(exit_cmd='q', exempted_err_msgs=('Not Found',))
            # TODO: update StatsConsts.LOG_MSG_ERROR_DB
            # ValidationTool.verify_expected_output(show_output, StatsConsts.LOG_MSG_ERROR_DB).verify_result()

    finally:
        set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_system_stats_log(engines: EnginesT, devices: DevicesT, test_api: str):
    """
    validate:
    - Configuring commands are logged to system log

    Test flow:
    1. Unset stats feature state and check log file
    2. Set category stats configuration and check log file
    """

    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    category_list = devices.dut.category_list

    ssh_connection = ConnectionTool.create_ssh_conn(engines.dut.ip, engines.dut.username,
                                                    engines.dut.password).get_returned_value()

    try:
        with allure.step("Unset stats feature state and check log file"):
            system.log.rotate_logs()
            system.stats.unset(op_param=StatsConsts.STATE, apply=True).verify_result()

        with allure.step("Set category stats configuration and check log file"):
            name = RandomizationTool.select_random_value(category_list).get_returned_value()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.INTERVAL, op_param_value=int(StatsConsts.INTERVAL_MIN)).verify_result()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.HISTORY_DURATION, op_param_value=int(StatsConsts.HISTORY_DURATION_DEFAULT)).\
                verify_result()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.ENABLED.value).verify_result()
            SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].
                                            apply_config, TestToolkit.engines.dut, False).verify_result()

        with allure.step("Validate commands exist in system log"):
            log_message_list = [StatsConsts.LOG_MSG_UNSET_STATS, StatsConsts.LOG_MSG_PATCH_CATEGORY + name]
            system.log.verify_expected_logs(log_message_list, logs_source=LogsSources.NVUED, engine=ssh_connection)

        with allure.step("Validate stats files in tech support file"):
            stats_files = list(engines.dut.run_cmd("ls /var/stats").split())
            validate_stats_files_exist_in_techsupport(system, engines.dut, stats_files)

    finally:
        set_system_stats_to_default(engines.dut, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_validate_tech_support_with_max_size(engines: EnginesT, devices: DevicesT, test_api: str):
    """
    validate:
    - Validate creating tech support when all stats files are full (reached max size)

    Test flow:
    1. Disable stats feature
    2. Replace all stats categories internal files with max sized files
    3. Run tech-support and validate files.
    """

    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    player_engine = engines['sonic_mgmt']
    category_list = devices.dut.category_list

    try:
        with allure.step("Disable system stats feature"):
            system.stats.set(op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.DISABLED.value,
                             apply=True).verify_result()

        with allure.step("Clear all internal files"):
            engines.dut.run_cmd("sudo rm -f /var/stats/*")

        with allure.step("Replace all category internal files with a max sized files"):
            for category in category_list:
                file_name = category + '.csv'
                file_path = StatsConsts.MAX_SIZE_FILE_PATH + file_name
                player_engine.upload_file_using_scp(dest_username=devices.dut.default_username,
                                                    dest_password=devices.dut.default_password,
                                                    dest_folder=StatsConsts.INTERNAL_PATH,
                                                    dest_ip=engines.dut.ip,
                                                    local_file_path=file_path)
                engines.dut.run_cmd("sudo cp /tmp/{} /var/stats".format(file_name))

        with allure.step("Validate stats files in tech support file"):
            stats_files = list(engines.dut.run_cmd("ls /var/stats").split())
            validate_stats_files_exist_in_techsupport(system, engines.dut, stats_files)

    finally:
        with allure.step("Clear all internal files and set to all default"):
            engines.dut.run_cmd("sudo rm -f /var/stats/*")
            set_system_stats_to_default(engines.dut, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_system_stats_invalid_values(engines: EnginesT, devices: DevicesT, test_api: str):
    """
    validate:
    - Check all the commands that get param with invalid values

    Test flow:
    1. nv set system stats category <invalid category-name> state <state>
    2. nv set system stats category <category-name> state <invalid state>
    3. nv set system stats category <category-name> interval <less than 1>
    4. nv set system stats category <category-name> interval <higher than 1440>
    5. nv set system stats category <category-name> history-duration <less than 1>
    6. nv set system stats category <category-name> history-duration <higher than 365>
    7. nv show system stats category <invalid category-name>
    8. nv clear system stats <invalid category-name>
    9. nv action delete system stats files <file does not exist>
    10. nv action upload system stats files <file does not exist> <remote-url>
    11. nv action upload system stats files <file-name> <invalid remote-url>
    12. nv action generate system stats <invalid category-name>
    """

    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    category_list = devices.dut.category_list
    player = engines['sonic_mgmt']
    invalid_remote_url = 'scp://{}:{}{}/tmp/'.format(player.username, player.password, player.ip)
    valid_remote_url = 'scp://{}:{}@{}/tmp/'.format(player.username, player.password, player.ip)

    try:
        with allure.step("Validate set system stats unknown category"):
            system.stats.category.categoryName[StatsConsts.INVALID_CATEGORY_NAME].set(
                op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.DISABLED.value, apply=True).\
                verify_result(should_succeed=False)

        with allure.step("Validate set system stats category invalid state"):
            name = RandomizationTool.select_random_value(category_list).get_returned_value()
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.INVALID_STATE).\
                verify_result(should_succeed=False)

        with allure.step("Validate set system stats category invalid low interval"):
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.INTERVAL, op_param_value=StatsConsts.INVALID_INTERVAL_LOW).\
                verify_result(should_succeed=False)

        with allure.step("Validate set system stats category invalid high interval"):
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.INTERVAL, op_param_value=StatsConsts.INVALID_INTERVAL_HIGH).\
                verify_result(should_succeed=False)

        with allure.step("Validate set system stats category invalid low history-duration"):
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.HISTORY_DURATION, op_param_value=StatsConsts.INVALID_HISTORY_DURATION_LOW).\
                verify_result(should_succeed=False)

        with allure.step("Validate set system stats category invalid high history-duration"):
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.HISTORY_DURATION, op_param_value=StatsConsts.INVALID_HISTORY_DURATION_HIGH).\
                verify_result(should_succeed=False)

        with allure.step("Validate show system stats invalid category"):
            system.stats.category.categoryName[StatsConsts.INVALID_CATEGORY_NAME].show(should_succeed=False)

        with allure.step("Validate clear system stats invalid category"):
            system.stats.category.categoryName[StatsConsts.INVALID_CATEGORY_NAME].action_general(StatsConsts.CLEAR).\
                verify_result(should_succeed=False)

        with allure.step("Validate delete system stats file not exists"):
            system.stats.files.file_name[StatsConsts.INVALID_FILE_NAME].action_delete().verify_result(should_succeed=False)

        with allure.step("Validate upload system stats file not exists"):
            system.stats.files.file_name[StatsConsts.INVALID_FILE_NAME].action_upload(valid_remote_url
                                                                                      ).verify_result(should_succeed=False)

        with allure.step("Validate upload system stats file to invalid URL"):
            file_name = 'stats_cpu_gorilla-154_20230702_145940.csv'
            file_path = StatsConsts.GENERATED_FILE_PATH + file_name
            player.upload_file_using_scp(dest_username=devices.dut.default_username,
                                         dest_password=devices.dut.default_password,
                                         dest_folder=StatsConsts.INTERNAL_PATH,
                                         dest_ip=engines.dut.ip,
                                         local_file_path=file_path)
            engine.run_cmd("sudo cp /tmp/{} /host/stats".format(file_name))
            system.stats.files.file_name[file_name].action_upload(invalid_remote_url).verify_result(should_succeed=False)

        with allure.step("Validate generate system stats invalid category"):
            system.stats.category.categoryName[StatsConsts.INVALID_CATEGORY_NAME].action_general(StatsConsts.GENERATE).\
                verify_result(should_succeed=False)

    finally:
        engine.run_cmd("sudo rm -f /host/stats/*")
        SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].
                                        detach_config, TestToolkit.engines.dut).verify_result()
        set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
def test_system_stats_big_files(engines: EnginesT, devices: DevicesT, random_api: str):
    """
    validate:
    - Append works on a big file (600K samples)
    - Clear works on a big file (600K samples)
    - Corrupted file - bigger than 600MB
    - Corrupted file - without a header

    Test flow:
    1. unset system stats feature
    2. set fan category history duration and interval to min values
    3. Update general configuration - cache_duration to min value
    4. Restart stats service to apply changes immediately
    5. Replace internal file with a big file
    6. Wait interval time and validate new samples are written to file
    7. Restart stats service and validate clear works on a big file
    8. Replace category internal file with a corrupted file (no header) and validate creation of a new file
    9. Replace category internal file with a corrupted file (over 600MB) and validate creation of a new file
    """

    system = System(devices_dut=devices.dut)
    engine = engines.dut
    player_engine: ProxySshEngine = engines['sonic_mgmt']

    try:
        with allure.step("Set system stats feature to default"):
            system.stats.unset(apply=True).verify_result()

        with allure.step("set fan category history duration and interval to min values"):
            system.stats.category.categoryName['disk'].set(
                op_param_name=StatsConsts.HISTORY_DURATION, op_param_value=int(StatsConsts.HISTORY_DURATION_MIN)).\
                verify_result()
            system.stats.category.categoryName['disk'].set(
                op_param_name=StatsConsts.INTERVAL, op_param_value=int(StatsConsts.INTERVAL_MIN), apply=True).\
                verify_result()

        with allure.step("Update cache duration to 1 minute"):
            set_cache_duration(engine, 1)

        with allure.step("Restart process..."):
            engine.run_cmd("sudo systemctl restart stats-reportd")

        with allure.step("Replace internal file with a big file"):
            file_name = 'disk.csv'
            file_path = StatsConsts.BIG_FILE_PATH + file_name
            player_engine.upload_file_using_scp(dest_username=devices.dut.default_username,
                                                dest_password=devices.dut.default_password,
                                                dest_folder=StatsConsts.INTERNAL_PATH,
                                                dest_ip=engines.dut.ip,
                                                local_file_path=file_path)
            engine.run_cmd("sudo cp /tmp/{} /var/stats".format(file_name))

        with allure.step("Wait 1 min..."):
            time.sleep(StatsConsts.SLEEP_1_MINUTE)

        with allure.step("Validate appending big file"):
            validate_number_of_lines_in_external_file(engines, system, 'disk', StatsConsts.BIG_FILE_NUM_OF_LINES,
                                                      StatsConsts.BIG_FILE_NUM_OF_LINES + 3)

        with allure.step("Delete uploaded file"):
            engine.run_cmd(cmd='rm -f {}'.format(file_path))

        with allure.step("Restart process..."):
            engine.run_cmd("sudo systemctl restart stats-reportd")

        with allure.step("Wait 15 seconds..."):
            time.sleep(StatsConsts.SLEEP_15_SECONDS)

        with allure.step("Validate clearing big file"):
            validate_number_of_lines_in_external_file(engines, system, 'disk',
                                                      devices.dut.stats_disk_header_num_of_lines,
                                                      devices.dut.stats_disk_header_num_of_lines + 30)

        with allure.step("Delete uploaded file"):
            engine.run_cmd(cmd='rm -f {}'.format(file_path))

        with allure.step("Replace internal file with file without header"):
            file_name = 'cpu.csv'
            file_path = StatsConsts.NO_HEADER_FILE_PATH + file_name
            player_engine.upload_file_using_scp(dest_username=devices.dut.default_username,
                                                dest_password=devices.dut.default_password,
                                                dest_folder=StatsConsts.INTERNAL_PATH,
                                                dest_ip=engines.dut.ip,
                                                local_file_path=file_path)
            engine.run_cmd("sudo cp /tmp/{} /var/stats".format(file_name))

        with allure.step("Restart process..."):
            engine.run_cmd("sudo systemctl restart stats-reportd")

        with allure.step("Wait 15 seconds..."):
            time.sleep(StatsConsts.SLEEP_15_SECONDS)

        with allure.step("Validate creating new category file when header is corrupted"):
            validate_number_of_lines_in_external_file(engines, system, 'cpu',
                                                      devices.dut.stats_cpu_header_num_of_lines,
                                                      devices.dut.stats_cpu_header_num_of_lines + 40)

        with allure.step("Delete uploaded file"):
            engine.run_cmd(cmd='rm -f {}'.format(file_path))

        with allure.step("Replace internal file with a huge file"):
            file_name = 'temperature.csv'
            generate_huge_stats_file(engine, 'temperature')

        with allure.step("Restart process..."):
            engine.run_cmd("sudo systemctl restart stats-reportd")

        with allure.step("Wait for stats-reportd to finish cleaning up the huge file"):
            wait_for_stats_file_cleanup(engine, 'temperature')

        with allure.step("Get Temperature header number of lines"):
            temperature_header_num_of_lines = get_header_number_of_lines(engines, 'temperature')
            assert temperature_header_num_of_lines >= devices.dut.stats_temperature_header_num_of_lines, \
                (f"temperature header number of lines ({temperature_header_num_of_lines}) is lower than the "
                 f"expected minimum number of lines: {devices.dut.stats_temperature_header_num_of_lines}")

        with allure.step("Validate creating new category file when file size is over 600MB"):
            validate_number_of_lines_in_external_file(engines, system, 'temperature',
                                                      temperature_header_num_of_lines,
                                                      temperature_header_num_of_lines + 100)

    finally:
        set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_validate_category_file_values(engines: EnginesT, devices: DevicesT, test_api: str):
    """
    validate:
    - Category file values are within the valid range
    - Samples timestamps match the configured interval

    Test flow:
    1. unset system stats feature
    2. set random category history duration and interval to min values
    3. Update general configuration - cache_duration to min value
    4. clear all internal and external category files
    5. Restart stats service to apply changes immediately
    6. Wait 5 minutes
    7. Generate and upload the chosen category file
    8. Validate file values are within range
    9. Validate samples timestamps match the configured interval
    10. Restore default values
    """

    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    player = engines['sonic_mgmt']
    category_list = devices.dut.category_list

    try:
        with allure.step("Set system stats feature to default"):
            system.stats.unset(apply=True).verify_result()

        with allure.step("Set all categories history duration and interval values to minimum"):
            configure_all_categories(system, category_list,
                                     history_duration=StatsConsts.HISTORY_DURATION_MIN,
                                     interval=StatsConsts.INTERVAL_MIN)

        with allure.step("Update cache duration to 1 minute"):
            set_cache_duration(engine, 1)

        with allure.step("Clear all system stats and delete stats files"):
            system_show = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
            start_time = datetime.strptime(system_show['date-time']['local-time'], StatsConsts.SYSTEM_TIME_FORMAT)
            # Remove mgmt port number if present in hostname
            hostname = system_show['hostname'].replace('-mgmt2', '')
            clear_all_internal_and_external_files(engine, system, category_list)

        with allure.step("Restart process..."):
            engine.run_cmd("sudo systemctl restart stats-reportd")

        with allure.step("Wait 5 min..."):
            time.sleep(StatsConsts.SLEEP_5_MINUTES)

        with allure.step("Generate system stats category"):
            for name in category_list:
                system.stats.category.categoryName[name].action_general(StatsConsts.GENERATE).verify_result()
            stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()).\
                get_returned_value()

            for file_name in stats_files_show:
                with allure.independent_step(f"testing {file_name} file"):
                    with allure.step("Upload stats file to URL"):
                        validate_upload_stats_file(engines, system, file_name, False)

                    with allure.step("Validate external file header"):
                        name = file_name.split('_')[1]
                        file_path = NvosConst.MARS_RESULTS_FOLDER + file_name
                        end_time = start_time + timedelta(minutes=6)
                        validate_external_file_header_and_data(name, file_path, hostname, start_time, end_time)

                    with allure.step("Delete uploaded file"):
                        player.run_cmd(cmd='rm -f {}'.format(file_path))

    finally:
        set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_system_stats_on_skynet(test_api: str):
    """
    (Dedicated test will be added later to skynet to check some of the functionalities once a month)

    Test flow:
    1. ...
    """

    TestToolkit.tested_api = test_api


# ---------------------------------------------
def set_system_stats_to_default(engine: ProxySshEngine, system: System) -> None:
    with allure.step("Update Stats feature to default"):
        system.stats.unset(apply=True).verify_result()

    with allure.step("Update cache general configuration to default"):
        set_cache_duration(engine, 10)
        set_cache_duration(engine, 1)


def set_cache_duration(engine, minutes):
    """Set the stats cache duration in CONFIG_DB."""
    DatabaseTool.sonic_db_cli_hset(
        engine, "", db_name=DatabaseConst.CONFIG_DB_NAME,
        db_config="STATS_CONFIG|GENERAL",
        param="cache_duration", value=str(minutes))


def configure_all_categories(system, category_list, interval=None,
                             history_duration=None, state=None, apply=True):
    """Configure all categories with the given parameters and optionally apply."""
    for name in category_list:
        if state is not None:
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.STATE,
                op_param_value=state).verify_result()
        if interval is not None:
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.INTERVAL,
                op_param_value=int(interval)).verify_result()
        if history_duration is not None:
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.HISTORY_DURATION,
                op_param_value=int(history_duration)).verify_result()
    if apply:
        SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].
                                        apply_config, TestToolkit.engines.dut, False).verify_result()


def clear_all_internal_and_external_files(engine: ProxySshEngine, system: System, category_list: list[str]) -> None:
    system.stats.action_general(StatsConsts.CLEAR).verify_result()
    stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()).get_returned_value()
    if stats_files_show != "":
        for file in stats_files_show.keys():
            system.stats.files.file_name[file].action_delete().verify_result()
    # clear old files if exist
    engine.run_cmd("sudo rm -f /var/stats/*.old")


def check_category_internal_files_exist(engine: ProxySshEngine, category_list: list[str], retries: int = 10, interval: int = 3) -> None:
    expected_files = [f"{cat}.csv" for cat in category_list]
    start_time = time.perf_counter()
    for attempt in range(1, retries + 1):
        output = engine.run_cmd("ls /var/stats")
        output_list = list(filter(None, output.strip().split()))
        missing = [file for file in expected_files if file not in output_list]
        if not missing:
            duration = time.perf_counter() - start_time
            logger.info("All {} category files generated in {:.1f}s ({} attempts)".format(
                len(expected_files), duration, attempt))
            OperationTime.save_manual_operation_duration_to_db(
                'stats category files generation', duration, pytest.test_name)
            break
        logger.info("Attempt {}/{}: still missing {} files: {}".format(attempt, retries, len(missing), missing))
        time.sleep(interval)
    else:
        duration = time.perf_counter() - start_time
        assert False, "After {:.1f}s ({} retries), still missing files: {}".format(duration, retries, missing)
    extra = [file for file in output_list if file not in expected_files]

    for allowed_extra_file in ("mgmt-interface.csv.old", "asic-power.csv.old", "verification_test"):
        if allowed_extra_file in extra:
            extra.remove(allowed_extra_file)

    assert not extra, f"Unexpected extra files found: {extra}"


def create_old_samples_internal_file(engine: ProxySshEngine, name: str, num_samples: int = 5) -> None:
    """Seed the internal /var/stats/<name>.csv with old-dated samples that match the DUT's
    own column schema, so the retention/cleanup check has stale rows to purge.

    The schema is derived from the DUT's current internal file (its header is written as soon
    as the category is enabled) rather than uploaded from a static fixture whose column layout
    drifts per-platform (e.g. a 2-PSU jaguar file vs an 8-PSU switch) and produces a ragged CSV
    that crashes the timestamp parser. Only the timestamp column matters to the cleanup check,
    so data columns get a benign placeholder; the timestamps are set far in the past.
    """
    internal_file = "/var/stats/{}.csv".format(name)

    # Restart stats-reportd so it re-initializes and writes the category header promptly
    # (~5s); otherwise the internal file only appears after a full sampling interval (up to
    # 1 min) and the read below would race ahead of it.
    engine.run_cmd("sudo systemctl restart stats-reportd")
    header_block = ""
    for _ in range(10):
        header_block = engine.run_cmd("sudo cat {} 2>/dev/null".format(internal_file))
        if header_block.strip():
            break
        time.sleep(3)
    assert header_block.strip(), \
        "internal stats file {} was not created after restarting stats-reportd".format(internal_file)

    # Keep the comment block (incl. its blank lines) up to and including the CSV column-header
    # line — the first non-comment, non-blank line (e.g. "Timestamp,PSU 1 Voltage [V],...").
    header_lines = []
    column_header = None
    for line in header_block.splitlines():
        header_lines.append(line)
        if line.startswith('#') or not line.strip():
            continue
        column_header = line
        break
    assert column_header, "no CSV column header found in {}".format(internal_file)
    num_columns = len(column_header.split(','))

    # Build old-dated rows matching the schema: past timestamp + placeholder data columns.
    old_base = datetime(2023, 5, 10, 16, 23, 18)
    placeholder = ','.join(['0'] * (num_columns - 1))
    old_rows = [
        "{},{}".format((old_base + timedelta(minutes=i)).strftime(StatsConsts.TIMESTAMP_FORMAT),
                       placeholder)
        for i in range(num_samples)
    ]

    content = '\n'.join(header_lines + old_rows) + '\n'
    # base64 round-trip avoids all shell quoting/newline issues when writing the file back.
    encoded = base64.b64encode(content.encode()).decode()
    engine.run_cmd("echo '{}' | base64 -d | sudo tee {} > /dev/null".format(encoded, internal_file))


def validate_upload_stats_file(engines: EnginesT, system: System, file: str, delete: bool = True) -> None:
    """
    validate upload stats file with scp and sftp
    """
    upload_protocols = ['scp', 'sftp']
    player = engines['sonic_mgmt']
    dest_path = NvosConst.MARS_RESULTS_FOLDER

    with allure.step("Upload stats file to player {} with the next protocols : {}".format(player.ip, upload_protocols)):
        for protocol in upload_protocols:
            with allure.step("Upload stats file to player with {} protocol".format(protocol)):
                upload_path = 'scp://{}:{}@{}{}'.format(player.username, player.password, player.ip, dest_path)
                system.stats.files.file_name[file].action_upload(upload_path).verify_result()

            with allure.step("Validate file was uploaded to player"):
                assert player.run_cmd(cmd='ls {} | grep {}'.format(dest_path, file)), \
                    "Did not find the file with ls cmd"

            if delete:
                with allure.step("Delete uploaded file"):
                    player.run_cmd(cmd='rm -f {}{}'.format(dest_path, file))


def parse_stats_csv(file_path):
    """Parse a stats CSV file, returning header metadata and a pandas DataFrame.

    Returns:
        dict with keys:
            'hostname'    — hostname from the file header (with -mgmt2 stripped)
            'group'       — category/group name
            'export_time' — the "Started sampling" timestamp
            'column_descriptions' — list of column names from "# Column N:" lines
            'df'          — DataFrame with all data rows (all values as strings)
    """
    metadata = {'column_descriptions': []}
    with open(file_path, 'r') as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith(StatsConsts.HEADER_HOSTNAME):
                metadata['hostname'] = stripped.replace('-mgmt2', '')
            elif stripped.startswith(StatsConsts.HEADER_GROUP):
                metadata['group'] = stripped.replace(StatsConsts.HEADER_GROUP, '')
            elif stripped.startswith(StatsConsts.HEADER_TIME):
                metadata['export_time'] = datetime.strptime(
                    stripped.replace(StatsConsts.HEADER_TIME, ''), StatsConsts.TIMESTAMP_FORMAT)
            elif stripped.startswith('# Column'):
                metadata['column_descriptions'].append(stripped.split(': ')[-1])
            elif not stripped.startswith('#') and stripped:
                break

    df = pd.read_csv(file_path, comment='#', keep_default_na=False, dtype=str)
    metadata['df'] = df
    return metadata


def assert_value_in_range(col_name, value, min_val, max_val, sample_num, category, allow_na=True):
    """Assert that a CSV cell value falls within [min_val, max_val].

    Args:
        allow_na: If True, 'N/A' values are silently accepted.
                  If False, 'N/A' values cause an assertion failure.
    """
    if value == 'N/A':
        assert allow_na, (
            f"{category} '{col_name}' has N/A at sample #{sample_num} (not permitted)")
        return
    assert min_val <= float(value) <= max_val, (
        f"{category} '{col_name}' = {value} not in [{min_val}, {max_val}] at sample #{sample_num}")


def _validate_row_from_config(config, col_names, row_values, num_of_columns, sample_num, category):
    """Apply validation rules from a category config dict to a single data row."""
    if 'columns' in config:
        for col_idx, min_val, max_val, allow_na in config['columns']:
            if col_idx < num_of_columns:
                assert_value_in_range(
                    col_names[col_idx], row_values[col_idx],
                    min_val, max_val, sample_num, category, allow_na)

    if 'uniform_range' in config:
        r = config['uniform_range']
        start = r['start']
        end = num_of_columns if r['end'] is None else r['end']
        for col_idx in range(start, end):
            assert_value_in_range(
                col_names[col_idx], row_values[col_idx],
                r['min'], r['max'], sample_num, category, r['allow_na'])

    if 'remaining_columns' in config:
        r = config['remaining_columns']
        start = r['start']
        end = num_of_columns if r['end'] is None else (num_of_columns + r['end'] if r['end'] < 0 else r['end'])
        for col_idx in range(start, end):
            assert_value_in_range(
                col_names[col_idx], row_values[col_idx],
                r['min'], r['max'], sample_num, category, r['allow_na'])

    if 'tail_columns' in config:
        t = config['tail_columns']
        for col_idx in range(num_of_columns - t['count'], num_of_columns):
            assert_value_in_range(
                col_names[col_idx], row_values[col_idx],
                t['min'], t['max'], sample_num, category, t['allow_na'])


def validate_external_file_header_and_data(name, file_path, hostname, start_time, end_time):
    with allure.step(f"validate external file header and data - {file_path}"):
        meta = parse_stats_csv(file_path)
        df = meta['df']

        with allure.step("Validate header metadata"):
            assert meta['hostname'] == StatsConsts.HEADER_HOSTNAME + hostname, \
                f"unexpected hostname: {meta['hostname']} vs {hostname}"
            assert meta['group'] == name, \
                f"unexpected group: {meta['group']} vs {name}"

            if start_time and end_time:
                assert start_time < meta['export_time'] < end_time, \
                    f"Started sampling time {meta['export_time']} not between {start_time} and {end_time}"

        with allure.step("Validate column descriptions match DataFrame columns"):
            assert meta['column_descriptions'] == list(df.columns), \
                "mismatch between # Column descriptions and CSV header columns"

        with allure.step("Validate data is not empty"):
            assert len(df) > 0, f"No data rows in {name} stats file"

        config = StatsConsts.CATEGORY_VALIDATION_CONFIG.get(name)
        assert config is not None, \
            f"Unknown stats category '{name}' — no validation config. " \
            f"Known: {list(StatsConsts.CATEGORY_VALIDATION_CONFIG.keys())}"

        export_time = meta['export_time']
        prev_sample_time = export_time - timedelta(minutes=int(StatsConsts.INTERVAL_MIN))
        col_names = list(df.columns)
        num_of_columns = len(col_names)

        with allure.step(f"Validate {name} data rows ({len(df)} samples)"):
            for row_idx, row in df.iterrows():
                sample_num = row_idx + 1
                row_values = list(row)

                assert len(row_values) == num_of_columns, \
                    f"Row {sample_num} has {len(row_values)} values, expected {num_of_columns}"

                prev_sample_time = check_sample_timestamp(row_values, prev_sample_time, name)
                _validate_row_from_config(config, col_names, row_values,
                                          num_of_columns, sample_num, name)

        with allure.step("Validate minimum sample count"):
            assert len(df) >= StatsConsts.MIN_EXPECTED_SAMPLES, \
                f"{name}: only {len(df)} samples collected, expected >= {StatsConsts.MIN_EXPECTED_SAMPLES}"

        with allure.step("Validate timestamps are strictly increasing"):
            timestamps = df.iloc[:, 0].tolist()
            for i in range(1, len(timestamps)):
                t_prev = datetime.strptime(timestamps[i - 1], StatsConsts.TIMESTAMP_FORMAT)
                t_curr = datetime.strptime(timestamps[i], StatsConsts.TIMESTAMP_FORMAT)
                assert t_curr > t_prev, \
                    f"{name}: timestamp at row {i + 1} ({t_curr}) is not after row {i} ({t_prev})"


def validate_external_file_timestamps(file_name: str, clear_time: datetime) -> None:
    full_path = NvosConst.MARS_RESULTS_FOLDER + file_name
    meta = parse_stats_csv(full_path)
    df = meta['df']

    assert not df.empty, f"No data rows found in {file_name}"

    for row_idx, row in df.iterrows():
        sample_time = datetime.strptime(row.iloc[0], StatsConsts.TIMESTAMP_FORMAT)
        assert sample_time > clear_time, \
            f"Sample at row {row_idx + 1} has timestamp {sample_time} earlier than clear time {clear_time}"


def validate_number_of_lines_in_external_file(engines: EnginesT, system: System, cat_name: str, min_lines: int, max_lines: int) -> None:
    player = engines['sonic_mgmt']

    with allure.step("Clear external files"):
        engines.dut.run_cmd("sudo rm -f /host/stats/*.csv")

    with allure.step("Generate system stats category"):
        system.stats.category.categoryName[cat_name].action_general(StatsConsts.GENERATE).verify_result()
        stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()). \
            get_returned_value()
        file_name = list(stats_files_show)[0]

    with allure.step("Upload stats file to URL"):
        validate_upload_stats_file(engines, system, file_name, False)

    with allure.step("Validate number of lines in file"):
        full_path = NvosConst.MARS_RESULTS_FOLDER + file_name
        with open(full_path, 'r') as f:
            num_of_lines = sum(1 for _ in f)
        assert min_lines < num_of_lines < max_lines, \
            f"Number of lines: {num_of_lines} is not as expected: {min_lines}-{max_lines}"

    with allure.step("Delete uploaded file"):
        player.run_cmd(cmd='rm -f {}'.format(full_path))


def get_stats_file_line_count(engine: ProxySshEngine, file_path: str) -> int:
    """Return the number of lines in a DUT stats file, or -1 if the file is absent."""
    quoted_path = shlex.quote(file_path)
    output: str = engine.run_cmd(f"if [ -e {quoted_path} ]; then wc -l < {quoted_path}; else echo -1; fi")
    return int(output.strip().splitlines()[-1])


def validate_stats_files_exist_in_techsupport(system: System, engine: ProxySshEngine, stats_files: list[str]) -> None:
    """
    generate techsupport and validate stats files exist in the stats dir
    """
    try:
        tech_support_folder, duration = system.techsupport.action_generate(engine=engine)
        logger.info("The techsupport file name is : " + tech_support_folder)
        system.techsupport.extract_techsupport_files(engine)
        techsupport_files_list = system.techsupport.get_techsupport_files_list(engine, 'stats')
        for stat_file in stats_files:
            assert "{}.gz".format(stat_file) in techsupport_files_list, \
                "Expect to have {} file, in the tech support stats files {}".format(stat_file, techsupport_files_list)

    finally:
        system.techsupport.cleanup(engine)
        if system.techsupport.file_name:
            system.techsupport.files.file_name[system.techsupport.file_name].action_delete()


def check_sample_timestamp(row, prev_sample_time, category):
    timestamp_str = row[0] if isinstance(row, list) else row.iloc[0]
    sample_time = datetime.strptime(timestamp_str, StatsConsts.TIMESTAMP_FORMAT)
    expected_time = prev_sample_time + timedelta(minutes=int(StatsConsts.INTERVAL_MIN))
    time_low_thresh = expected_time - timedelta(seconds=5)
    time_high_thresh = expected_time + timedelta(seconds=5)
    assert time_low_thresh < sample_time < time_high_thresh, \
        f"{category} timestamp {sample_time} is too far from expected {expected_time}"
    return sample_time


def wait_for_stats_file_cleanup(engine, category, max_size_kb=1024):
    """Wait for stats-reportd to finish cleaning up a huge internal file.

    Polls the file size of /var/stats/{category}.csv until it drops below max_size_kb,
    indicating that stats-reportd has completed the cleanup and created a new file.
    """
    @retry(AssertionError, tries=24, delay=5)
    def _wait():
        output = engine.run_cmd(f"stat --format=%s /var/stats/{category}.csv 2>/dev/null || echo 0")
        file_size_bytes = int(output.strip())
        file_size_kb = file_size_bytes / 1024
        assert file_size_kb < max_size_kb, \
            f"stats-reportd still cleaning up {category}.csv (size: {file_size_kb:.0f}KB, waiting for < {max_size_kb}KB)"

    _wait()


def generate_huge_stats_file(engine, category, num_lines=7000000):
    """Generate a huge stats file (>600MB) on the device using its own header.

    Uses the device's current /var/stats/{category}.csv header so the file
    has the correct platform-specific header regardless of device type.
    Replicates a real data line 7M times to exceed the 600MB threshold.
    """
    stats_file = f"/var/stats/{category}.csv"
    engine.run_cmd(
        f"sudo bash -c '"
        f"grep \"^#\" {stats_file} > /tmp/{category}_huge.csv && "
        f"grep -v \"^#\" {stats_file} | head -1 >> /tmp/{category}_huge.csv && "
        f"DATA=$(grep -v \"^#\" {stats_file} | tail -1) && "
        f"yes \"$DATA\" | head -n {num_lines} >> /tmp/{category}_huge.csv && "
        f"mv /tmp/{category}_huge.csv {stats_file}'"
    )


def get_header_number_of_lines(engines: EnginesT, category: str) -> int:
    with allure.step(f"Get {category} header number of lines"):
        num_of_lines = int(engines.dut.run_cmd(f"grep -c '^#' /var/stats/{category}.csv")) + 2

    return num_of_lines


def wait_for_stats_file_to_shrink(engine: ProxySshEngine, category: str, max_size_kb: int = 1024) -> int:
    """Wait for stats-reportd to finish cleaning up a huge internal file.

    Polls the file size of /var/stats/{category}.csv until it drops below max_size_kb,
    indicating that stats-reportd has completed the cleanup and created a new file.
    """
    counter = 0

    @retry(AssertionError, tries=24, delay=5)  # ~2 min timeout
    def _wait():
        nonlocal counter
        counter += 1

        with allure.step(f"Check {category!r} stats file shrink: attempt {counter}"):
            output = engine.run_cmd(f"stat --format=%s /var/stats/{category}.csv 2>/dev/null || echo 0")
            file_size_bytes = int(output.strip())
            file_size_kb = file_size_bytes / 1024
            assert file_size_kb < max_size_kb, \
                f"stats-reportd still cleaning up {category}.csv (size: {file_size_kb:.0f}KB, waiting for < {max_size_kb}KB)"

    _wait()
