import logging
import pytest
import time

from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.nvos_constants.constants_nvos import DatabaseConst, SystemConsts, NvosConst, LogsSources
from devts.infra.tools.general_constants.constants import DefaultConnectionValues
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool, RebootParams
from ngts.nvos_tools.infra.SerialConsoleTool import SerialConsoleTool
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tests_nvos.constants import MINUTE

logger = logging.getLogger(__name__)

SYSTEM_STATE_DOWN_POLL_TIMEOUT = 30
SYSTEM_STATE_DOWN_POLL_INTERVAL = 2


@pytest.mark.check_log_size
@pytest.mark.check_disk_usage
@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.init_flow
@pytest.mark.dut_reboot
def test_system_ready_state_up(engines, devices, topology_obj, verify_no_kernel_errors):
    """
    Test flow:
        0. serial connection
        1. Run nv action reboot system (using engine.dut)
        2. validate Status = System is ready
        3. Run nv show fae system ready
        4. validate state = enabled
        5. Run nv action reboot system - will ends after catching "system is ready message" and CLI is up
        6. Run nv show system ready
        7. validate Status = System is ready
    """
    DutUtilsTool.wait_for_nvos_to_become_functional(engines.dut)
    topology_obj = topology_obj or TestToolkit.topology_obj

    with allure.step('reboot the system'):
        reload_cmd_set = "nv action reboot system"
        DutUtilsTool.reload(engine=engines.dut, device=devices.dut, command=reload_cmd_set, confirm=True,
                            reboot_params=RebootParams(should_wait_till_system_ready=False, topology_obj=topology_obj)
                            ).verify_result()

    # TODO - WA once "checkpoint # " prints are removed and the reboot is faster.
    devices.dut.sleep_after_system_reboot()

    with allure.step('reconnect to the switch'):
        serial_engine = connect_before_ssh(topology_obj, engines.dut)

    with allure.step('verify NVUE is not working before system is ready'):
        with allure.step("running nv show system command"):
            serial_engine.serial_engine.sendline('nv show system')
        with allure.step("verifying the output includes System is initializing!"):
            serial_engine.serial_engine.expect("System is initializing!", timeout=10)

    with allure.step('verify SYSTEM_READY|SYSTEM_STATE is not exist yet'):
        with allure.step("running sonic-db-cli to check existence of SYSTEM_READY|SYSTEM_STATE"):
            Tools.DatabaseTool.sonic_db_cli_hgetall_serial(engine=serial_engine.serial_engine, asic="",
                                                           db_name=DatabaseConst.STATE_DB_NAME,
                                                           table_name='\"SYSTEM_READY|SYSTEM_STATE\"')
        with allure.step("verifying the output includes (empty array)"):
            serial_engine.serial_engine.expect("{}", timeout=10)

    check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)
    ssh_connection = ConnectionTool.create_ssh_conn(engines.dut.ip, engines.dut.username, engines.dut.password).get_returned_value()

    with allure.step('wait until the system is ready'):
        DutUtilsTool.wait_for_nvos_to_become_functional(
            ssh_connection, cli_up_retries=devices.dut.cli_up_retries).verify_result()

    logs_to_find = ['Wait until the NOS signal we are ready to serve', 'System is ready to serve']
    System().log.verify_expected_logs(logs_to_find, logs_source=LogsSources.NVUED, engine=ssh_connection)

    with allure.step('check the system status in DB'):
        output = Tools.DatabaseTool.sonic_db_cli_hgetall(engine=ssh_connection, asic="",
                                                         db_name=DatabaseConst.STATE_DB_NAME,
                                                         table_name='\"SYSTEM_READY|SYSTEM_STATE\"')
        assert SystemConsts.STATUS_UP in output, "SYSTEM STATE SHOULD BE UP"

    with allure.step("verify the system is ready using nv show system"):
        system = System(None)
        Tools.ValidationTool.verify_field_value_in_output(Tools.OutputParsingTool.parse_json_str_to_dictionary(
            system.show()).verify_result(), SystemConsts.STATUS, SystemConsts.STATUS_DEFAULT_VALUE).verify_result()

    with allure.step("Validate services are active"):
        res_obj = devices.dut.verify_services(engines.dut)
        assert res_obj.result, res_obj.info

    with allure.step("Validate docker are up"):
        dockers = devices.dut.available_dockers
        res_obj = devices.dut.verify_dockers(engines.dut, dockers)
        assert res_obj.result, res_obj.info

    with allure.step("Validate all ports status is up"):
        res_obj = devices.dut.verify_ib_ports_state(engines.dut, NvosConst.PORT_STATUS_UP)
        assert res_obj.result, res_obj.info


@pytest.mark.check_log_size
@pytest.mark.check_disk_usage
@pytest.mark.timeout(25 * MINUTE, func_only=True)
@pytest.mark.init_flow
@pytest.mark.dut_reboot
@pytest.mark.disable_loganalyzer
def test_system_ready_state_down(engines, devices, topology_obj):
    """
    Test flow:
        0. serial connection
        1. Run nv action reboot system (using engine.dut)
        2. kill one of the dockers
        3. validate we can not run CLI and also the system status table is not exist
        4. verify expected logs after waiting 10 minutes
        5. start docker as a cleanup step
    """
    with allure.step('pick a docker to kill'):
        system = System(None)
        docker_to_kill = 'lldp.service'
        logger.info("after reboot we will stop {}".format(docker_to_kill))

    with allure.step('reboot the system'):
        reload_cmd_set = "nv action reboot system"
        DutUtilsTool.reload(engine=engines.dut, device=devices.dut, command=reload_cmd_set, confirm=True,
                            reboot_params=RebootParams(should_wait_till_system_ready=False)
                            ).verify_result()

    # TODO - WA once "checkpoint # " prints are removed and the reboot is faster.
    devices.dut.sleep_after_system_reboot()

    with allure.step('reconnect to the switch'):
        serial_engine = connect_before_ssh(topology_obj, engines.dut)

    with allure.step('kill service {}'.format(docker_to_kill)):
        serial_engine.serial_engine.sendline('sudo systemctl stop {}'.format(docker_to_kill))
        check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)
        ssh_connection = ConnectionTool.create_ssh_conn(engines.dut.ip, engines.dut.username, engines.dut.password).get_returned_value()

    try:
        with allure.step('Sleep 5 min'):
            time.sleep(300)

        with allure.step('check system status after killing swss docker'):
            with allure.step('verify SYSTEM_READY|SYSTEM_STATE is not exist yet'):
                output = Tools.DatabaseTool.sonic_db_cli_hgetall(engine=ssh_connection, asic="",
                                                                 db_name=DatabaseConst.STATE_DB_NAME,
                                                                 table_name='\"SYSTEM_READY|SYSTEM_STATE\"')
                assert not output or '{}' in output, "SYSTEM_READY state table should not be exist before system is ready"

            with allure.step('verify NVUE is not working before system is ready'):
                assert 'System is initializing!' in ssh_connection.run_cmd('nv show system'), \
                    "WE CAN NOT RUN NV COMMANDS BEFORE SYSTEM IS READY MESSAGE"

            with allure.step('Sleep 5 min'):
                time.sleep(300)

            logs_to_find = ['Wait until the NOS signal we are ready to serve']
            system.log.verify_expected_logs(logs_to_find, logs_source=LogsSources.NVUED, engine=ssh_connection)

            with allure.step(f'verify the system status is DOWN within {SYSTEM_STATE_DOWN_POLL_TIMEOUT}s'):
                output = _wait_for_system_state_down(ssh_connection)
                assert SystemConsts.STATUS_DOWN in output, (
                    f"SYSTEM STATE SHOULD BE DOWN within {SYSTEM_STATE_DOWN_POLL_TIMEOUT}s; last output: {output}"
                )

            with allure.step("verify we can run nvue command and the system status is not ok"):
                Tools.ValidationTool.verify_field_value_in_output(Tools.OutputParsingTool.parse_json_str_to_dictionary(
                    system.show()).verify_result(), SystemConsts.STATUS, SystemConsts.STATUS_NOT_OK).verify_result()

    finally:
        with allure.step('start docker {} as a cleanup step'.format(docker_to_kill)):
            engines.dut.run_cmd('sudo systemctl start {}'.format(docker_to_kill))
            system.reboot.action_reboot(engines.dut)


def _wait_for_system_state_down(ssh_connection):
    deadline = time.monotonic() + SYSTEM_STATE_DOWN_POLL_TIMEOUT
    while True:
        output = Tools.DatabaseTool.sonic_db_cli_hgetall(engine=ssh_connection, asic="",
                                                         db_name=DatabaseConst.STATE_DB_NAME,
                                                         table_name='\"SYSTEM_READY|SYSTEM_STATE\"')
        if SystemConsts.STATUS_DOWN in output:
            return output

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return output

        # Keep the grace period bounded so readiness delays beyond 30 seconds remain visible.
        time.sleep(min(SYSTEM_STATE_DOWN_POLL_INTERVAL, remaining))


def connect_before_ssh(topology_obj, engine):
    with allure.step('make serial connection with admin'):
        with allure.step('enter to serial context'):
            serial = SerialConsoleTool.get_serial_console_session(topology_obj)
        with allure.step('exit existing login'):
            SerialConsoleTool.exit_existing_login(serial)

        SerialConsoleTool.login_nos(serial_engine=serial, username=engine.username, password=engine.password, start_login_tries=10, handle_change_password_prompt=False)
        return serial
