import logging
import time
from datetime import datetime

import pytest

from ngts.constants.constants import BugHandlerConst
from ngts.nvos_constants.constants_nvos import HealthConsts, RebootConsts, SystemConsts
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.general.kernel_crash_helpers import (
    LANDMAN_ROW27_FMEA,
    cleanup_kernel_crash_stress,
    get_kdump_expected_patterns,
    precheck_kdump_for_stress,
    prepare_kernel_crash_stress_baseline,
    run_kernel_crash_stress_cycle,
    verify_system_healthy_after_kernel_crash_stress,
    verify_tech_support_after_kernel_crash_stress,
    verify_techsupport_files_names,
    verify_techsupport_files_sizes,
)
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
from ngts.tests_nvos.system.reboot_telemetry_helpers import (
    REBOOT_REASON_SHOW_EXEMPTED_ERR_MSGS,
    RebootReasonCategory,
    assert_nvue_gnmi_counters_match,
    gnmi_client_for_dut,
    take_reboot_telemetry_snapshot,
    verify_reboot_telemetry_after_reboot,
)
from ngts.tools.test_utils import allure_utils as allure
try:
    from devts.infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine
except ModuleNotFoundError:
    from infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine

logger = logging.getLogger()


@pytest.mark.disable_loganalyzer
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
        expected_patterns_list = get_kdump_expected_patterns()

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


@pytest.mark.disable_loganalyzer
@pytest.mark.timeout(90 * MINUTE, func_only=True)
def test_repeated_kernel_crash_core_dump_retention(
    engines, devices, topology_obj, random_api, test_name,
):
    """
    Landman row 27 — stress multiple core dumps (kernel crashes).

    Primary: SW-006 (panic/reboot/recovery/health), SW-026 (kdump capture/cleanup/bounded growth).
    Secondary: SYS-010 (SSD usable for logs/tech-support). Proxy: SYS-012 (resilience only).

    Proves: repeated sysrq panics; new kdump dir each cycle under ``/var/crash/collected/``;
    SSD/artifact bounds; manual tech-support after stress; verified cleanup.
    Does **not** assert ``kdump_count <= num_dumps``: CONFIG_DB ``num_dumps`` is admin policy;
    NVOS prunes old artifacts on tech-support (e.g. >7 days), not per-cycle kdump rotation.

    Knobs: ``KERNEL_CRASH_STRESS_CYCLES`` (0 = ``KERNEL_CRASH_STRESS_CYCLES_DEFAULT``, default 4).

    Note: OpenAPI ``/system/reboot`` history index ``1`` is the *most recent* panic, not pytest cycle
    number. Auto tech-support can take several minutes; polling uses live syslog plus ``/host/dump/``.
    """
    pytest.skip_coredump_check = True
    allure.attach("landman_row27_failure_modes", "\n".join(f"{k}: {v}" for k, v in LANDMAN_ROW27_FMEA.items()))
    system = System()
    engine = engines.dut
    serial_engine: PexpectSerialEngine = ConnectionTool.create_serial_connection(
        topology_obj, devices
    )
    per_cycle = []
    seen_kdump_dirs = set()
    tech_support_tar = None

    try:
        with allure.step("1. Precheck: kdump enabled and ready"):
            ctx = precheck_kdump_for_stress(engine)

        with allure.step("2. Baseline: SSD free space before repeated kernel crashes"):
            prepare_kernel_crash_stress_baseline(engine)

        for cycle in range(1, ctx["stress_cycles"] + 1):
            with allure.step(
                f"3.{cycle}. Kernel crash cycle {cycle}/{ctx['stress_cycles']}: "
                "sysrq-trigger → reboot → kdump capture"
            ):
                result = run_kernel_crash_stress_cycle(
                    system,
                    engines,
                    engine,
                    topology_obj,
                    devices,
                    serial_engine,
                    cycle,
                    ctx["stress_cycles"],
                    seen_kdump_dirs,
                )
                per_cycle.append(result["summary"])
                seen_kdump_dirs = result["seen_kdump_dirs"]
                serial_engine = result["serial_engine"]

        with allure.step("4. Verify system health is OK after repeated kernel crashes"):
            verify_system_healthy_after_kernel_crash_stress(system, ctx["stress_cycles"])

        with allure.step("5. Verify tech-support succeeds after repeated kernel crashes"):
            tech_support_tar = verify_tech_support_after_kernel_crash_stress(
                system, engine, test_name
            )

        allure.attach("kernel_crash_stress_summary", str(per_cycle))

    finally:
        with allure.step("6. Cleanup crash artifacts and tech-support"):
            cleanup_kernel_crash_stress(system, engine, tech_support_tar)
