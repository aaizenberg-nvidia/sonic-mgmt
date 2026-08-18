import logging
import time

import pytest

from ngts.nvos_constants.constants_nvos import ApiType
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import RemarkableLogsConsts
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.DatabaseTool import DatabaseTool
from ngts.nvos_tools.infra.FilesTool import FilesTool
from ngts.nvos_constants.constants_nvos import DatabaseConst
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.constants import MINUTE


@pytest.mark.timeout(3 * MINUTE, func_only=True)
@pytest.mark.system
@pytest.mark.fae
@pytest.mark.simx
def test_system_remarkable_logs_default_values(random_api, engines, devices):
    """
    check the expected default values when the feature is enabled
        Test flow:
            1. run show fae system log remarkable-logs - save as remarkable_los_output
            2. validate all fields have expected values
            3. Run ls -l /var/log | grep remarkable
            5. validate at least [remarkable_logs_1, remarkable_logs_first_boot] exist
            6. verify hostname appending value is "Jaguar-NVOS"
            7. Run ls -l /var/log/remarkable_logs_first_boot
            8. verify at least [boot_log.1.gz] exist
            9. Run ls -l /var/log/remarkable_logs_1
            10. verify at least [boot_log.1.gz] exist
    """

    with allure.step('Test default values'):
        fae = Fae()

        with allure.independent_step('Test show command default values'):
            system_output = OutputParsingTool.parse_json_str_to_dictionary(fae.system.log.remarkable_logs.show()).get_returned_value()
            expected_values = ["3", "18000", "3", "300", "3600", "2", "3", "enabled", "20000", "3", "2000", "1800"]
            ValidationTool.validate_fields_values_in_output(output_dict=system_output, expected_fields=RemarkableLogsConsts.FEATURE_EXPECTED_FIELDS, expected_values=expected_values)

        with allure.step(f'check remarkable folder under {RemarkableLogsConsts.LOGS_PATH}'):
            FilesTool.validate_expected_files(engines.dut, RemarkableLogsConsts.LOGS_PATH, [f"{RemarkableLogsConsts.REMARKABLE_LOGS_FOLDER_NAME}1", RemarkableLogsConsts.FIRST_BOOT_FOLDER_NAME])

        with allure.step(f'check expected files under {RemarkableLogsConsts.LOGS_PATH}{RemarkableLogsConsts.FIRST_BOOT_FOLDER_NAME}'):
            FilesTool.validate_expected_files(engines.dut, f'{RemarkableLogsConsts.LOGS_PATH}{RemarkableLogsConsts.FIRST_BOOT_FOLDER_NAME}', ["boot_log.1.gz"])

        with allure.step(f'check expected files under {RemarkableLogsConsts.LOGS_PATH}{RemarkableLogsConsts.REMARKABLE_LOGS_FOLDER_NAME}1'):
            FilesTool.validate_expected_files(engines.dut, f'{RemarkableLogsConsts.LOGS_PATH}{RemarkableLogsConsts.REMARKABLE_LOGS_FOLDER_NAME}1', ["boot_log.1.gz"])


@pytest.mark.timeout(3 * MINUTE, func_only=True)
@pytest.mark.system
@pytest.mark.fae
@pytest.mark.simx
def test_system_remarkable_requested_logs(engines, devices):
    """
    Test Flow:
        1. set daemon logs and apply configuration
        2. run show fae system log remarkable-logs and validate output
        3. change via database the request flag
        4. run nv action rotate system log
        5. validate new file exist
        6. rerun steps 3-5 (5 times)
        7. change via database the request flag
        8. validate new file does not exist
    """
    fae = Fae()
    system = System()

    with allure.step(f"rotate logs and make sure demon can send request to save logs"):
        system.log.rotate_logs()
        system.log.rotate_logs()
        engines.dut.run_cmd("sudo ls /var/log/remarkable_logs_1/")

    try:
        with allure.step(f"configure requested by demon logs {RemarkableLogsConsts.REQUESTED_BY_DAEMON_LOGS}"):
            fae.system.log.remarkable_logs.set(op_param_name=f"{RemarkableLogsConsts.REQUESTED_BY_DAEMON_LOGS}", op_param_value=5, apply=True)

        with allure.step(f"verify show command"):
            system_output = OutputParsingTool.parse_json_str_to_dictionary(fae.system.log.remarkable_logs.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(output_dictionary=system_output, field_name=RemarkableLogsConsts.REQUESTED_BY_DAEMON_LOGS, expected_value="5")

        with allure.step("simulate daemon issue 5 times"):
            for i in range(5):
                with allure.step(f"loop iteration {i + 1}"):
                    with allure.step(f"simulate daemon issue"):
                        DatabaseTool.sonic_db_cli_hset(engine=engines.dut, asic="",
                                                       db_name=DatabaseConst.APPL_DB_NAME,
                                                       db_config="REMARKABLE_LOGS:save_requested",
                                                       param="save-logs",
                                                       value="True")

                    with allure.step(f"rotate logs"):
                        system.log.rotate_logs()

                    with allure.step(f'check expected files under {RemarkableLogsConsts.LOGS_PATH}{RemarkableLogsConsts.REMARKABLE_LOGS_FOLDER_NAME}1/'):
                        FilesTool.validate_expected_files(engines.dut, f'{RemarkableLogsConsts.LOGS_PATH}{RemarkableLogsConsts.REMARKABLE_LOGS_FOLDER_NAME}1',
                                                          [f"{RemarkableLogsConsts.REQUESTED_FILE_NAME}log.{i + 1}.gz"])

    finally:
        with allure.step("remove all new files"):
            engines.dut.run_cmd("sudo rm /var/log/remarkable_logs_1/*")
        with allure.step(f"rotate logs"):
            system.log.rotate_logs()
            system.log.rotate_logs()


@pytest.mark.timeout(3 * MINUTE, func_only=True)
@pytest.mark.system
@pytest.mark.fae
@pytest.mark.simx
def test_system_remarkable_logs_error(engines, devices):
    """
    check the expected flow after simulation errors/warnings
    """
    _test_remarkable_logs(engine=engines.dut, testing=RemarkableLogsConsts.ERROR, log_count=270, log_priority='local0.error',
                          threshold_file="remarkable_threshold_error_last_run", file_name="error_",
                          new_values=[2000, 5, 250, 500])


@pytest.mark.timeout(3 * MINUTE, func_only=True)
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.fae
def test_system_remarkable_logs_storm(engines, devices):
    """
    check the expected flow after simulation notices
    """
    _test_remarkable_logs(engine=engines.dut, testing=RemarkableLogsConsts.STORM, log_count=600, log_priority='local0.notice',
                          threshold_file="remarkable_threshold_notice_last_run", file_name="storm_prints_",
                          new_values=[2000, 5, 500, 500])


def _test_remarkable_logs(engine, testing, log_count, log_priority, threshold_file, file_name, new_values):
    """
    Test Flow:
        1. set rate, clean-time, logs-number and time-window for errors/storms
        2. apply configuration
        3. run show fae system log remarkable-logs - save as remarkable_los_output
        4. validate all fields have expected values
        5. exhaust first-saved-boot-logs rotations (boot window uses boot_log only)
        6. run error/storm script
        7. run nv action rotate system log
        8. validate new file exist
        9. rerun steps 6-8 (5 times)
        10. run error/storm script without clearing threshold
        11. validate new file does not exist
    :param testing:
    :param log_count:
    :param log_priority:
    :param threshold_file:
    :param file_name:
    :return:
    """
    fae = Fae()
    system = System()

    command = f"for i in {{1..{log_count}}}; do logger -p {log_priority} \"Welcome $i times\"; done"
    with allure.step(f"check current files in remarkable logs folder"):
        engine.run_cmd("cat /tmp/remarkable_threshold_error_last_run")
        engine.run_cmd("cat /tmp/remarkable_threshold_notice_last_run")
        engine.run_cmd("ls /var/log | grep remarkable_logs")
        engine.run_cmd("ls /var/log/remarkable_logs_1")

    with allure.step(f"configure rate, clean-time, logs-number and time-window for {testing}"):
        fae.system.log.remarkable_logs.set(op_param_name=f"{testing}{RemarkableLogsConsts.LOGS_CLEAN_TIME}", op_param_value=new_values[0]
                                           ).verify_result()
        fae.system.log.remarkable_logs.set(op_param_name=f"{testing}{RemarkableLogsConsts.LOGS_NUMBER}", op_param_value=new_values[1]
                                           ).verify_result()
        fae.system.log.remarkable_logs.set(op_param_name=f"{testing}{RemarkableLogsConsts.LOGS_RATE}", op_param_value=new_values[2]
                                           ).verify_result()
        fae.system.log.remarkable_logs.set(op_param_name=f"{testing}{RemarkableLogsConsts.LOGS_TIME_WINDOW}", op_param_value=new_values[3]
                                           ).verify_result()
        NvueGeneralCli.apply_config(engine, True)

    with allure.step("validate changes"):
        system_output = OutputParsingTool.parse_json_str_to_dictionary(
            fae.system.log.remarkable_logs.show()).get_returned_value()
        expected_fields = [f"{testing}{RemarkableLogsConsts.LOGS_CLEAN_TIME}",
                           f"{testing}{RemarkableLogsConsts.LOGS_NUMBER}", f"{testing}{RemarkableLogsConsts.LOGS_RATE}",
                           f"{testing}{RemarkableLogsConsts.LOGS_TIME_WINDOW}"]
        expected_values = new_values
        ValidationTool.validate_fields_values_in_output(output_dict=system_output, expected_fields=expected_fields,
                                                        expected_values=expected_values).verify_result()

    # HLD: first X rotations since boot are saved only as boot_log.X.gz (no storm/error duplicate).
    # Exhaust that window before expecting error_log / storm_prints_log filenames.
    with allure.step("exhaust first-saved-boot-logs rotations"):
        boot_rotations = int(system_output.get(RemarkableLogsConsts.FIRST_SAVED_BOOT_LOGS, "2"))
        for _ in range(boot_rotations):
            system.log.rotate_logs()
        time.sleep(8)  # allow rsyslog to settle after consecutive rotations
        engine.run_cmd("ls /var/log/remarkable_logs_1")

    try:
        with allure.step("simulate errors issue in logs 5 times"):
            for i in range(5):
                with allure.step(f"loop iteration {i + 1}"):
                    with allure.step(f"remove threshold files"):
                        engine.run_cmd(f"sudo rm /tmp/{threshold_file}")

                    with allure.step(f"simulate {testing} issue using {log_priority}"):
                        engine.run_cmd(command)

                    with allure.step(f"check threshold file"):
                        engine.run_cmd(f"cat /tmp/{threshold_file}")

                    with allure.step(f"rotate logs"):
                        system.log.rotate_logs()

                    with allure.step(f'check expected files under {RemarkableLogsConsts.LOGS_PATH}{RemarkableLogsConsts.REMARKABLE_LOGS_FOLDER_NAME}1/'):
                        FilesTool.validate_expected_files(engine, f'{RemarkableLogsConsts.LOGS_PATH}{RemarkableLogsConsts.REMARKABLE_LOGS_FOLDER_NAME}1',
                                                          [f"{file_name}log.{i + 1}.gz"])

        with allure.step("simulate errors issue without deleting threshold files"):
            with allure.step("delete the 5th file"):
                engine.run_cmd(f"sudo rm {RemarkableLogsConsts.LOGS_PATH}{RemarkableLogsConsts.REMARKABLE_LOGS_FOLDER_NAME}1/{file_name}log.5.gz")

            with allure.step(f"simulate {testing} issue using {log_priority}"):
                engine.run_cmd(command)

            with allure.step(f"rotate logs"):
                system.log.rotate_logs()

            with allure.step(f'check expected files under {RemarkableLogsConsts.LOGS_PATH}{RemarkableLogsConsts.REMARKABLE_LOGS_FOLDER_NAME}1/'):
                FilesTool.validate_expected_files(engine, f'{RemarkableLogsConsts.LOGS_PATH}{RemarkableLogsConsts.REMARKABLE_LOGS_FOLDER_NAME}1',
                                                  [f"{file_name}log.5.gz"], False)
    finally:
        with allure.step("remove all new files"):
            engine.run_cmd("sudo rm /var/log/remarkable_logs_1/*")
        with allure.step(f"rotate logs"):
            system.log.rotate_logs()
            system.log.rotate_logs()
