import logging
import re
import time

import pytest
from retry import retry

from ngts.nvos_constants.constants_nvos import HealthConsts, SyslogConsts, SystemConsts
from ngts.nvos_tools.infra.DutUtilsTool import wait_for_specific_regex_in_logs
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)

paths_order = ['/host/nos-images/', '/etc/fae_platform_firmware/ssd/', '/host/fw-images/cpld/', '/host/fw-images/bios/',
               '/host/fw-images/fpga/', '/host/fw-images/bmc/', '/host/fw-images/erot/', '/host/fw-images/transceiver/',
               '/host/stats/', '/var/stats/', '/host/dump/', '/var/core/', '/host/fw-images/asic/']


@pytest.mark.system
def test_ssd_cleanup_before_adding_files(engines, devices):
    """
    :summary:
    Test Flow:
        1. get ssd usage
        2. verify it's more threshold (5G)
        3. run nv action run fae system ssd-cleanup
        4. get ssd usage and verify it's still the same as before cleanup
        5. check logs - nothing has been deleted

    :param engines:
    :param devices:
    :return:
    """
    fae = Fae()
    ssd_usage_before_cleanup = _get_df_output(engines.dut)

    with allure.step("check usage is under threshold and the status is ok"):
        assert ssd_usage_before_cleanup[SystemConsts.SSD_SPACE_AVAILABLE_SIZE] >= 5, "can not complete the test, the SSD usage more than expected {}".format(ssd_usage_before_cleanup[SystemConsts.SSD_SPACE_USED_SIZE])

    with allure.step("try to cleanup SSD and verify nothing has been deleted"):
        fae.system.ssd_cleanup(expected_str='Action succeeded')
        ssd_usage_after_cleanup = _get_df_output(engines.dut)

        with allure.step("check usage is under threshold and the status is ok"):
            assert ssd_usage_after_cleanup == ssd_usage_before_cleanup, "at least one of the fields has been change, the usage before cleanup is {}, the usage after cleanup is {}".format(ssd_usage_before_cleanup, ssd_usage_after_cleanup)

        with allure.step("check no files deleted"):
            deleted_list = _get_deleted_files_list_from_logs(engines.dut)
            assert len(deleted_list) == 0, "no files should be deleted but the cleanup script deleted {}".format(deleted_list)


@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.system
@pytest.mark.checklist
def test_ssd_cleanup_positive_flow(engines, devices):
    """
    :summary:
    Test Flow:
        1. add files until warning threshold (5G)
        1.5 wait 10-11 min
        2. verify health issue
        3. Run  nv action run fae system ssd-cleanup
        4. verify deleted files order
        5. verify health is ok
        6. add files until auto cleanup (3.5G)
        7. Run nv show system events and verify cleanup step
        7.5 search "INFO ssd_cleanup: SSD Cleanup Started" in the logs
        8. verify health
        9. verify deleting order

    :param engines:
    :param devices:
    :return:
    """
    with allure.step("create fae and system"):
        fae = Fae()
        system = System()

    MONIT_UPDATE_TIME = 180
    with allure.step("save all system events before testing"):
        system_events_before_testing = OutputParsingTool.parse_json_str_to_dictionary(system.events.show()).verify_result()

    _delete_all_files(engines.dut)
    file_path = '/etc/monit/conf.d/sonic-host'
    old_line = 'if status == 1 for 10 times within 20 cycles then alert repeat every 1 cycles'
    new_line = 'if status == 1 for 2 times within 20 cycles then alert repeat every 1 cycles'
    _change_monit_and_reload(engines.dut, old_line, new_line, file_path)

    try:
        system_health_status = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).verify_result()[HealthConsts.STATUS] == HealthConsts.NOT_OK
        df_output = _get_df_output(engines.dut)
        with allure.step("add file to reach usage threshold {}".format(5.1)):
            engines.dut.run_cmd(f"sudo fallocate -l {df_output[SystemConsts.SSD_SPACE_AVAILABLE_SIZE] - 5.1}G {paths_order[-1]}/big_file")

        files_to_delete = _add_files(engines.dut, 4, df_output[SystemConsts.SSD_SPACE_AVAILABLE_SIZE])

        with allure.step("check health status is not ok"):
            with allure.step(f"health issue will be reported after {MONIT_UPDATE_TIME} seconds"):
                time.sleep(MONIT_UPDATE_TIME)

            _check_disk_issue_with_retry(system, no_disk_issue=False)

            with allure.step("check system events - two events expected "):
                events_dict = OutputParsingTool.parse_json_str_to_dictionary(system.events.show()).verify_result()
                _verify_system_event(system_events_before_testing, events_dict, False, system_health_status)

        with allure.step("try to cleanup and verify health status and deleted files after it"):
            with allure.step("Rotate logs"):
                system.log.rotate_logs()

            with allure.step("cleanup SSD"):
                fae.system.ssd_cleanup(expected_str='Action succeeded')

            with allure.step("check deleted files and the deleting order"):
                verify_deleted_folders_list(engines.dut, files_to_delete[:-2])

            with allure.independent_step("check no disk issue"):
                with allure.step(f"waiting for {MONIT_UPDATE_TIME} seconds, for monit and healthD"):
                    time.sleep(MONIT_UPDATE_TIME)
                _check_disk_issue(system)

            with allure.independent_step("check system events - two events expected "):
                events_dict = OutputParsingTool.parse_json_str_to_dictionary(system.events.show()).verify_result()
                _verify_system_event(system_events_before_testing, events_dict, True, system_health_status)

        df_output = _get_df_output(engines.dut)
        file_name = 'Big_file'
        engines.dut.run_cmd('sudo fallocate -l {size}G /{path}/{file}'.format(size=df_output[SystemConsts.SSD_SPACE_AVAILABLE_SIZE] - 2, path=paths_order[0], file=file_name))

        with allure.step("check auto cleanup step"):
            with allure.step("check SSD Cleanup Started in the logs"):
                wait_for_specific_regex_in_logs(engines.dut, "ssd_cleanup: SSD Cleanup Done", timeout=120)

            with allure.step("Verify that deleted files are completely removed"):
                verify_deleted_folders_list(engines.dut, [file_name])
                assert "No such file or directory" in engines.dut.run_cmd(f"cat {paths_order[0]}/{files_to_delete[0]}"), f"{files_to_delete[0]} should be deleted"

            with allure.step("check no disk issue"):
                with allure.step(f"waiting for {MONIT_UPDATE_TIME} seconds, for monit and healthD"):
                    time.sleep(MONIT_UPDATE_TIME)
                _check_disk_issue(system)
    finally:
        _delete_all_files(engines.dut)
        _change_monit_and_reload(engines.dut, new_line, old_line, file_path)


def _check_disk_issue_with_retry(system: System, /, *, no_disk_issue: bool = True, tries: int = 6, delay: int = 10):
    """
    Check disk issue with retry.

    Args:
        system: System object.
        system_events_before_testing: System events before testing.
        system_health_status: System health status.

    Keyword Args:
        no_disk_issue: Whether to expect disk issue.
        tries: Number of tries.
        delay: Delay between tries.

    Returns: None.
    """
    @retry(AssertionError, tries=tries, delay=delay)
    def _check_disk_issue_with_retry_inner():
        with allure.step("check disk issue"):
            _check_disk_issue(system, no_disk_issue)

    return _check_disk_issue_with_retry_inner()


@pytest.mark.dut_reboot
@pytest.mark.system
def test_ssd_cleanup_reboot_with_high_ssd_usage(engines, devices):
    """
    Test Flow:
        1. add one file to get 99% usage
        2. run nv action system reboot
        3. check health
        4. check serial logs

    :param engines:
    :param devices:
    :return:
    """

    system = System()
    _delete_all_files(engines.dut)
    df_output = _get_df_output(engines.dut)
    path = '/host/nos-images/'
    file_name = 'new_file'
    file_size = df_output[SystemConsts.SSD_SPACE_AVAILABLE_SIZE] - 2

    try:
        engines.dut.run_cmd('sudo fallocate -l {size}G /{path}/{file}'.format(size=file_size, path=path, file=file_name))

        with allure.step("Rotate logs"):
            logger.info("Listing all current log files:")
            engines.dut.run_cmd("ls -la /var/log")
            system.log.rotate_logs()

        with allure.step('Reboot the system'):
            system.reboot.action_reboot()

        with allure.step("check deleted files and the deleting order"):
            verify_deleted_folders_list(engines.dut, [file_name])

        with allure.step("check no disk issue"):
            _check_disk_issue(system)

        with allure.step("check ssd-cleanup deleted the {file}".format(file=file_name)):
            assert file_name not in engines.dut.run_cmd(f'ls {path}')
    finally:
        with allure.step(f"cleanup step - delete {file_name}"):
            engines.dut.run_cmd('sudo rm -f /{path}/{file}'.format(path=path, file=file_name))


def _add_files(engine, usage_threshold, available_space):
    """
    :summary:
        the method will add 2 files for each path in paths_order all same size
    :param engine:
    :param usage_threshold: available space after adding files
    :param available_space: available space before adding files
    :return: list of added files (with the adding order)
    """

    with allure.step("create new files to reach usage threshold {}".format(usage_threshold)):
        added_files_list = []

        size_each_file = 500 / (2 * len(paths_order) - 1)
        file_size = f"{size_each_file}M"

        for i, path in enumerate(paths_order):
            file_name = f"new_file{i}"
            file_path = path + file_name

            with allure.step(f"adding {file_path} {file_path}.1 of size {file_size}"):
                engine.run_cmd(f"sudo fallocate -l {file_size} {file_path}")
                added_files_list.append(file_name)
                engine.run_cmd(f"sudo fallocate -l {file_size} {file_path}.1")
                added_files_list.append(file_name + '.1')

        logger.info(f"the current df command output is : {engine.run_cmd('df -h')}")
        return added_files_list


def _get_df_output(engine):
    """
    :summary:
        run command and parse it and return expected info
    :return:
    """
    with allure.step("run df command and parse the output into a dictionary"):
        df_output = engine.run_cmd('df -h | grep root-overlay')
        parts = df_output.split()
        result = {
            SystemConsts.SSD_SPACE_TOTAL_SIZE: float(parts[1][:-1]),
            SystemConsts.SSD_SPACE_USED_SIZE: float(parts[2][:-1]),
            SystemConsts.SSD_SPACE_AVAILABLE_SIZE: float(parts[3][:-1]),
            SystemConsts.SSD_SPACE_USAGE_PERCENTAGE: float(parts[4][:-1])
        }

        return result


def verify_deleted_folders_list(engine, files_to_delete):
    """
    :param engine:
    :param files_to_delete:
    :return:
    """
    with allure.step('compare the expected list to the deleted files list'):
        deleted_list = _get_deleted_files_list_from_logs(engine)
        it1 = iter(files_to_delete)
        for item in deleted_list:
            if item == next(it1, None):
                continue
        assert next(it1, None) is None, f"we expected to delete the files with this order: {files_to_delete}, but the deleting was in this order: {deleted_list}"


def _get_deleted_files_list_from_logs(engine):
    """
    :summary:
        checking the system logs file to know the deleted files and the deleting order
    :param engine:
    :return: deleted files list
    """
    with allure.step('Get the list of deleted files from logs'):
        from_line = "ssd_cleanup: SSD Cleanup Started"
        to_line = "ssd_cleanup: SSD Cleanup Done"
        cmd = f"tac {SyslogConsts.SYSLOG_LOG_PATH} | awk '/{to_line}/ {{p=1}} p; /{from_line}/ {{exit}}' | tac"
        logs_output = engine.run_cmd(cmd).splitlines()
        file_names = [line.split()[-1] for line in logs_output if "ssd_cleanup: Deleting" in line]

    return file_names


def _change_monit_and_reload(engine, old_line, new_line, file_path):
    """
    :summary:

    :param engine:
    :return:
    """
    with allure.step('Change monit and reload service'):
        engine.run_cmd(f"sudo sed -i 's/{old_line}/{new_line}/' {file_path}")
        engine.run_cmd('sudo monit reload')
        _wait_until_monit_is_running(engine)


def _delete_all_files(engine):
    """
    :summary:

    :param engine:
    :return:
    """
    with allure.step('Delete all files under this list of paths: {}'.format(paths_order)):
        for path in paths_order:
            logger.info(f"Deleting files under {path}")
            engine.run_cmd(f"sudo rm -rf {path}")
            cmd = f"sudo bash -c 'if [ ! -d '{path}' ]; then mkdir -p '{path}'; fi'"
            engine.run_cmd(cmd)
            logger.info(f"done with {path}")


def _check_disk_issue(system, no_disk_issue=True):
    """
    :summary:

    :param system:
    :param no_disk_issue:
    :return:
    """
    issue = {
        'Disk space': {
            "issue": "Status is not ok"
        }
    }
    health_dict = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).verify_result()
    temp = all(item in health_dict[HealthConsts.ISSUES].items() for item in issue.items())

    assert (no_disk_issue ^ temp), "we{}expect disk issue, but the health output is {}".format(' do not ' if no_disk_issue else ' ', health_dict)


def get_status_of_program(output, program_name):
    pattern = rf"Program '{program_name}'\s*[\s\S]*?status\s*([^\n]*)"
    match = re.search(pattern, output)
    if match:
        return match.group(1).strip()
    else:
        return None


@retry(Exception, tries=12, delay=10)
def _wait_until_monit_is_running(engine):
    """

    :param engine:
    :return:
    """
    with allure.step("check monit status"):
        output = engine.run_cmd('sudo monit status')
        monit_status = get_status_of_program(output, "root-overlay")
        if "Status ok" not in monit_status:
            raise Exception("Waiting for monit to finish initializing")


def _verify_system_event(events_dict_before_testing, events_dict_after_testing, is_ok, system_health_status):
    """

    :param events_dict_before_testing:
    :param events_dict_after_testing:
    :param is_ok:
    :param system_health_status:
    :return:
    """
    with allure.step("create expected events"):
        expected_disk_issue_event = {'resource': 'Disk space', 'severity': 'INFORMATIONAL' if is_ok else 'WARNING', 'text': 'Service goes back to normal' if is_ok else 'Disk space Status is not ok'}
        expected_health_issue_event = {'resource': 'System', 'severity': 'INFORMATIONAL' if is_ok else 'WARNING', 'text': 'Health status is ok' if is_ok else 'Health status is not ok'}

    with allure.step("get all events that happened during testing"):
        events_output = {key: value for key, value in events_dict_after_testing.items() if
                         key not in events_dict_before_testing}
        for event in events_output.values():
            event.pop('time-created')

    logger.info("the new events: {}".format(events_output.values()))

    with allure.step("verify health event"):
        assert expected_health_issue_event in events_output.values() or system_health_status, f"can not find an expected event: {expected_health_issue_event}"

    with allure.step("verify disk issue event"):
        assert expected_disk_issue_event in events_output.values(), f"can not find an expected event: {expected_disk_issue_event}"
