

import pytest
import logging
import time
import re
from datetime import datetime

from ngts.constants.constants import BugHandlerConst
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_constants.constants_nvos import OperationTimeConsts, NvosConst, SystemConsts
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_constants.constants_nvos import RebootConsts
from devts.infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.system.reboot_telemetry_helpers import (
    REBOOT_REASON_SHOW_EXEMPTED_ERR_MSGS,
    RebootReasonCategory,
    assert_nvue_gnmi_counters_match,
    gnmi_client_for_dut,
    take_reboot_telemetry_snapshot,
    verify_reboot_telemetry_after_reboot,
)

logger = logging.getLogger(__name__)


@pytest.mark.disable_loganalyzer
@pytest.mark.dut_reboot
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_kernel_crash(engines, devices, topology_obj, random_api):
    """
        @summary: Test simulates kernal crash and verifies behavior

        Test flow:
        1. Simulate kernel crash
        2. Wait until system is ready
        3. Sleep until tech-support file is generated
        4. Verify in logs that kernel crash was detected and tech-support file was generated
        5. Get generated tech-support and verify it's birth-time was in the last 6 mins
        6. Get kdump files in tech-support
        7. Verify expected kdump files in tech-support
    """
    system = System()
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)

    with allure.step("NVUE and gNMI reboot counters must match before kernel crash"):
        telemetry_before = take_reboot_telemetry_snapshot(system, gnmi_client)
        assert_nvue_gnmi_counters_match(telemetry_before)

    with allure.step("Simulate kernel crash"):
        start_time = datetime.strptime(ClockTools.get_local_time_from_show_system_date_time_output(system.datetime.show()),
                                       BugHandlerConst.TIMESTAMP_FORMATS[4])
        serial_engine: PexpectSerialEngine = ConnectionTool.create_serial_connection(topology_obj, devices)
        serial_engine.run_cmd("echo 1 | sudo tee /proc/sys/kernel/sysrq")
        serial_engine.run_cmd("echo c | sudo tee /proc/sysrq-trigger")

    with allure.step("Wait for system is ready in serial"):
        DutUtilsTool.wait_on_system_reboot(engines.dut)
        time.sleep(10)

    with allure.step("Verify NVUE and gNMI reboot telemetry after kernel crash"):
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
            expected_category=RebootReasonCategory.CRITICAL_ERROR,
            expected_details=telemetry_details,
            expected_user=RebootConsts.REBOOT_USER_NA,
        )

    with allure.step("Verify in logs that kernel crash was detected and tech-support file will be generated"):
        log_message_list = [r"Kernel crashes detected",
                            r"System is ready to respond, will take tech support file.",
                            r"Generating system tech-support file, it might take a few minutes..."]
        system.log.verify_expected_logs_by_time(log_message_list, engines.dut, only_latest_log=False,
                                                start_time=start_time)

    with allure.step("Sleep until tech-support file in generated"):
        duration = devices.dut.expected_operation_durations.get(devices.dut.generate_tech_support)
        time.sleep(duration + 0.25 * MINUTE)

    with allure.step("Verify in logs that tech-support file generation is done"):
        log_message_list = [r"Generated tech-support"]
        system.log.verify_expected_logs_by_time(log_message_list, engines.dut, only_latest_log=False,
                                                start_time=start_time)

    with allure.step("Get generated tech-support and verify it was generated in the last 6 mins"):
        output_list = list(Tools.OutputParsingTool.parse_show_files_to_dict(
            system.techsupport.files.show()).get_returned_value().values())
        techsupport_file_path = output_list[0]
        techsupport_file_name = techsupport_file_path.split('/')[-1]
        system.techsupport.check_techsupport_file_age(engines.dut, system, techsupport_file_path, max_age_hours=0.1)

    with allure.step("Get expected kdump files names"):
        kdump_files_names_templates = ["dmesg.{}.gz", "kdump.{}", "kdump_lock.gz"]
        expected_patterns_list = []
        for template in kdump_files_names_templates:
            if '{}' in template:
                pattern = re.escape(template).replace(r'\{\}', NvosConst.TIMESTAMP_REGEX)
            else:
                pattern = re.escape(template)
            expected_patterns_list.append(re.compile(f"^{pattern}$"))

    with allure.step("Validate each expected file name and size"):
        system.techsupport.extract_techsupport_files(engines.dut, techsupport_file_name)
        techsupport_files_dict = system.techsupport.get_techsupport_files_names(engines.dut,
                                                                                {"kdump": expected_patterns_list})
        with allure.independent_step("Validate files names"):
            verify_techsupport_files_names(techsupport_files_dict["kdump"], expected_patterns_list)

        with allure.independent_step("Validate files sizes"):
            verify_techsupport_files_sizes(engines.dut, techsupport_file_name)

    # Cleanup: Remove kdump files and tech-support after validation
    with allure.step("Cleanup kdump files and tech-support after validation"):
        # Extract kdump timestamp from the validated files (e.g., "kdump.202512091341" -> "202512091341")
        kdump_timestamp = None
        for filename in techsupport_files_dict["kdump"]:
            if filename.startswith("kdump.") and not filename.endswith(".gz"):
                kdump_timestamp = filename.split(".")[1]
                break

        if not kdump_timestamp:
            logger.warning("Could not extract kdump timestamp, skipping kdump cleanup")
            kdump_path = None
        else:
            kdump_path = f"/var/crash/collected/{kdump_timestamp}"
            logger.info(f"Will cleanup kdump directory: {kdump_path}")

        # Measure sizes before cleanup
        kdump_size_before = int(engines.dut.run_cmd(
            f'sudo du -sm {kdump_path} 2>/dev/null | cut -f1 || echo "0"' if kdump_path else 'echo "0"',
            validate=False).strip() or 0)

        techsupport_size = int(engines.dut.run_cmd(
            f'sudo du -sm {techsupport_file_path} 2>/dev/null | cut -f1 || echo "0"',
            validate=False).strip() or 0)

        logger.info(f"Before cleanup - Kdump: {kdump_size_before} MB, Tech-support: {techsupport_size} MB")

        # Cleanup specific kdump directory for this test run only
        with allure.step("Cleanup kdump files from /var/crash/collected/"):
            if kdump_path:
                engines.dut.run_cmd(f'sudo rm -rf {kdump_path}', validate=False)
                logger.info(f"Deleted kdump from /var/crash/collected/{kdump_timestamp}/")

        # Cleanup kdump folder inside extracted tech-support
        with allure.step("Cleanup kdump folder from extracted tech-support"):
            if kdump_timestamp:
                extracted_dir = techsupport_file_name.replace('.tar.gz', "")
                extracted_techsupport_path = SystemConsts.TECHSUPPORT_FILES_PATH + extracted_dir
                kdump_in_techsupport = f"{extracted_techsupport_path}/kdump"

                # Delete the entire kdump folder (collected folder only exists after kernel crash)
                engines.dut.run_cmd(f'sudo rm -rf {kdump_in_techsupport}', validate=False)
                logger.info(f"Deleted kdump folder from tech-support: {kdump_in_techsupport}")

        # Delete the tech-support tar.gz file
        with allure.step("Cleanup tech-support archive file"):
            if system.techsupport.file_name:
                system.techsupport.files.file_name[system.techsupport.file_name].action_delete()
                logger.info(f"Deleted tech-support archive: {techsupport_file_path}")

        # Measure and report cleanup results
        total_freed = kdump_size_before + techsupport_size
        logger.info(f"Cleanup completed - Total space freed: {total_freed} MB")

        allure.attach("Cleanup Summary",
                      f"Kdump freed: {kdump_size_before} MB (timestamp: {kdump_timestamp})\n"
                      f"Tech-support: {techsupport_size} MB\nTotal: {total_freed} MB")


def verify_techsupport_files_names(techsupport_files_list, expected_patterns_list):
    files_search_errors = {}
    for expected_file_pattern in expected_patterns_list:
        if not any(expected_file_pattern.match(file) for file in techsupport_files_list):
            files_search_errors[expected_file_pattern] = f'file "{expected_file_pattern}" was not found'
    err = ',\n'.join(list(files_search_errors.values()))
    assert not files_search_errors, f"The following files weren't found:\n{err}"


def verify_techsupport_files_sizes(engine, techsupport_file_name):
    system = System()
    files_list = system.techsupport.get_techsupport_empty_files(engine, techsupport_file_name, "kdump")
    assert len(files_list) == 0, f"the next files are unexpectedly empty {files_list}"
