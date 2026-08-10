import re
import time
import pytest
import logging
from ngts.nvos_tools.system.System import System
from datetime import datetime
from retry.api import retry_call
from ngts.tests_nvos.general.security.conftest import create_ssh_login_engine
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from devts.infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine
from ngts.nvos_constants.constants_nvos import SystemConsts, NvosConst
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from devts.infra.tools.general_constants.constants import DefaultConnectionValues
from ngts.tools.test_utils import allure_utils as allure


cumulus_owner = "yleibovichwe"


logger = logging.getLogger(__name__)


@pytest.mark.checklist
@pytest.mark.ssh_config
def test_set_max_cli_session(engines, devices):
    """
    Test flow:
        1. run show system ssh and check default fields and values
        2. set max cli session to 98, check functionality of it, cpu and memory utilization
        3. unset system ssh-config max cli session and verify

    """
    system = System()

    with allure.step('Show ssh and verify default values'):
        ssh_output = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.show()).get_returned_value()

        with allure.step("Verify default values"):
            ValidationTool.validate_fields_values_in_output(SystemConsts.SSH_CONFIG_OUTPUT_FIELDS,
                                                            SystemConsts.SSH_CONFIG_DEFAULT_VALUES,
                                                            ssh_output).verify_result()

        with allure.step("Validate login max cli session"):
            system.ssh_server.set(SystemConsts.SSH_CONFIG_MAX_SESSIONS, '98', apply=True,
                                  ask_for_confirmation=True).verify_result()
            ssh_output = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(ssh_output, SystemConsts.SSH_CONFIG_MAX_SESSIONS, '98')\
                .verify_result()

    with allure.step("Open more than 98 cli's session and verify result"):
        connection_list = []
        try:
            for conn_no in range(100):
                try:
                    logger.info("Creating connection number: {}".format(conn_no + 1))
                    connection = create_ssh_login_engine(engines.dut.ip, username=DefaultConnectionValues.DEFAULT_USER,
                                                         port=22)
                    connection_list.append(connection)
                    respond = connection.expect([DefaultConnectionValues.PASSWORD_REGEX, '~'])
                    if respond == 0:
                        connection.sendline(devices.dut.default_password)
                        connection.expect(DefaultConnectionValues.DEFAULT_PROMPTS[0])
                except Exception as err:
                    logger.info("Failed in creating connection {}, Error: {}".format(conn_no + 1, err))
                    break

            with allure.step("Validate no of users is 98"):
                connection_list[conn_no - 1].sendline('w')
                connection_list[conn_no - 1].expect('98 users')

            with allure.step("Validate system resources CPU utilization with 98 cli session configured"):
                logging.info("Validate system resources CPU utilization with 98 cli session configured")
                output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                    system.show("cpu")).get_returned_value()
                cpu_utilization = output_dictionary[SystemConsts.CPU_TOTAL_UTILIZATION_KEY]
                assert cpu_utilization < SystemConsts.CPU_PERCENT_THRESH_MAX, \
                    "CPU utilization: {actual}% is higher than the maximum limit of: {expected}%" \
                    "".format(actual=cpu_utilization, expected=SystemConsts.CPU_PERCENT_THRESH_MAX)

            with allure.step("Validate system memory utilization with 98 cli session configured"):
                logging.info("Validate system memory  utilization with 98 cli session configured")
                output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                    system.show("memory")).get_returned_value()
                utilization = output_dictionary[SystemConsts.MEMORY_PHYSICAL_KEY]["utilization"]
                assert SystemConsts.MEMORY_PERCENT_THRESH_MIN < utilization < \
                    SystemConsts.MEMORY_PERCENT_THRESH_MAX, "Physical utilization percentage is out of range"

        finally:
            with allure.step("Close all opened before cli sessions"):
                logging.info("Close all opened before cli sessions")
                for connection in connection_list:
                    connection.close()

    with allure.step("Negative validation for set command"):
        logger.info("Negative validation for set command")
        system.ssh_server.set(SystemConsts.SSH_CONFIG_MAX_SESSIONS, 101, apply=True,
                              ask_for_confirmation=True).verify_result(False)
        system.ssh_server.set(SystemConsts.SSH_CONFIG_MAX_SESSIONS, 1, apply=True,
                              ask_for_confirmation=True).verify_result(False)
        system.ssh_server.set(SystemConsts.SSH_CONFIG_MAX_SESSIONS, 'aaa', apply=True,
                              ask_for_confirmation=True).verify_result(False)

    with allure.step("Unset max cli session and validate"):
        logger.info("Unset max cli session and validate")
        system.ssh_server.unset(apply=True, ask_for_confirmation=True).verify_result()
        ssh_output = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.show()).get_returned_value()

        with allure.step("Verify default values"):
            ValidationTool.validate_fields_values_in_output(SystemConsts.SSH_CONFIG_OUTPUT_FIELDS,
                                                            SystemConsts.SSH_CONFIG_DEFAULT_VALUES,
                                                            ssh_output).verify_result()


@pytest.mark.checklist
@pytest.mark.ssh_config
def test_set_inactivity_timeout(engines, devices, topology_obj, test_name):
    """
    Test flow:
        1. run show system serial-console and check default fields and values
        2. change inactive timeout to 1 and check it changed
        3. check before create new serial and ssh conn that we have only 2 sessions on the switch by w command
        4. create one serial and ssh session and check that after 60 seconds it will be disconnected
        5. do the unset and check it returned to default
    """
    system = System()

    with allure.step("Verify default values"):
        serial_output = OutputParsingTool.parse_json_str_to_dictionary(system.system_cli.show())\
            .get_returned_value()
        logging.info(serial_output)
        ValidationTool.verify_field_value_in_output(serial_output, SystemConsts.CLI_INACTIVE_TIMEOUT, SystemConsts.CLI_DEFAULT_INACTIVE_TIMEOUT).verify_result()

    with allure.step("Change inactivity timeout for ssh and serial and validate"):
        logger.info("Change inactivity timeout for ssh and serial and validate")
        system.ssh_server.set(SystemConsts.SSH_CONFIG_INACTIVE_TIMEOUT, '1', apply=True,
                              ask_for_confirmation=True).verify_result()
        system.system_cli.set(SystemConsts.CLI_INACTIVE_TIMEOUT, '1', apply=True,
                              ask_for_confirmation=True).verify_result()
        ssh_output = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(ssh_output, SystemConsts.SSH_CONFIG_INACTIVE_TIMEOUT, '1') \
            .verify_result()
        serial_output = OutputParsingTool.parse_json_str_to_dictionary(system.system_cli.show()) \
            .get_returned_value()
        ValidationTool.verify_field_value_in_output(serial_output, SystemConsts.CLI_INACTIVE_TIMEOUT, '1').verify_result()

    with allure.step("Open new ssh and serial connection"):
        logger.info("Open new ssh and serial connection")

        with allure.step("Get number of current users"):
            users_num = _get_users_num(engines)

        connection = None
        try:
            logger.info("Create serial engine")
            serial_engine = ConnectionTool.create_serial_connection(topology_obj=topology_obj, devices=devices,
                                                                    force_new_login=True)
            logger.info("Create ssh engine")
            connection = create_ssh_login_engine(engines.dut.ip, username=DefaultConnectionValues.DEFAULT_USER,
                                                 port=22)
            respond = connection.expect([DefaultConnectionValues.PASSWORD_REGEX, '~'])
            if respond == 0:
                connection.sendline(devices.dut.default_password)
                connection.expect(DefaultConnectionValues.DEFAULT_PROMPTS[0])

            with allure.step("Verify we have 2 new users connected"):
                assert users_num == _get_users_num(engines) - 2, f"Current users number is not as expected.\nExpected: {users_num + 2}\nGot: {_get_users_num(engines)}"

            with allure.step("Wait 60 seconds"):
                start_time = time.time()
                time.sleep(60)

            with allure.step("Verify 2 users disconnected by inactivity timeout"):
                retry_call(_assert_number_of_users_disconnected, [engines, users_num], exceptions=AssertionError, tries=6, delay=5)
                end_time = time.time()
                total_time = end_time - start_time
                logger.info(f"Users disconnection by inactivity timeout duration - {total_time} seconds")
                OperationTime.verify_operation_time(total_time, "users disconnection by inactivity timeout", devices).verify_result()

        except BaseException as ex:
            raise Exception("Failed on {}".format(str(ex)))
        finally:
            if connection:
                connection.close()
            system.ssh_server.unset(apply=True, ask_for_confirmation=True).verify_result()
            system.serial_console.unset(apply=True, ask_for_confirmation=True).verify_result()
            system.system_cli.unset(apply=True, ask_for_confirmation=True).verify_result()
            ssh_output = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(ssh_output, SystemConsts.SSH_CONFIG_INACTIVE_TIMEOUT, SystemConsts.SSH_CONFIG_DEFAULT_INACTIVE_TIMEOUT)\
                .verify_result()
            serial_output = OutputParsingTool.parse_json_str_to_dictionary(system.system_cli.show()) \
                .get_returned_value()
            logging.info(serial_output)
            ValidationTool.verify_field_value_in_output(serial_output, SystemConsts.CLI_INACTIVE_TIMEOUT, SystemConsts.CLI_DEFAULT_INACTIVE_TIMEOUT).verify_result()


@pytest.mark.checklist
@pytest.mark.ssh_config
@pytest.mark.cumulus
def test_set_sysrq_capabilities(engines):
    """
    Test flow:
        1. run show system serial-console and check default fields and values
        2. change inactive sysrq to enabled and check it changed
        3. unset and check it returned to default
    """
    system = System()

    with allure.step("Verify default values"):
        serial_output = OutputParsingTool.parse_json_str_to_dictionary(system.serial_console.show())\
            .get_returned_value()
        ValidationTool.verify_field_value_in_output(serial_output, SystemConsts.SERIAL_CONSOLE_SYSRQ_CAPABILITIES,
                                                    SystemConsts.SERIAL_CONSOLE_DEFAULT_SYSRQ_CAPABILITIES
                                                    ).verify_result()

    def _assert_sysrq_kernel_value(expected_val):
        """Fetch sysrq kernel param and assert it matches expected value."""
        output = engines.dut.run_cmd('cat /proc/sys/kernel/sysrq')
        assert not output or expected_val in output, \
            f"Kernel sysrq value is '{output}', expected '{expected_val}'"

    with allure.step("Verify kernel sysrq is disabled by default"):
        ValidationTool.retry_until_valid(
            lambda: _assert_sysrq_kernel_value("0"),
            description="Verify kernel sysrq is disabled")

    with allure.step("Change sysrq to enabled"):
        logger.info("Change sysrq to enabled")
        system.serial_console.set(SystemConsts.SERIAL_CONSOLE_SYSRQ_CAPABILITIES,
                                  SystemConsts.SERIAL_CONSOLE_ENABLED_SYSRQ_CAPABILITIES, apply=True,
                                  ask_for_confirmation=True).verify_result()

    with allure.step("Verify sysrq is enabled"):
        ValidationTool.retry_until_valid(
            lambda: _assert_sysrq_kernel_value("1"),
            description="Verify kernel sysrq is enabled")
        serial_output = OutputParsingTool.parse_json_str_to_dictionary(system.serial_console.show())\
            .get_returned_value()
        ValidationTool.verify_field_value_in_output(serial_output, SystemConsts.SERIAL_CONSOLE_SYSRQ_CAPABILITIES,
                                                    SystemConsts.SERIAL_CONSOLE_ENABLED_SYSRQ_CAPABILITIES
                                                    ).verify_result()

    with allure.step("Verify default values after unset"):
        system.serial_console.unset(apply=True, ask_for_confirmation=True).verify_result()
        ValidationTool.retry_until_valid(
            lambda: _assert_sysrq_kernel_value("0"),
            description="Verify kernel sysrq is disabled after unset")
        serial_output = OutputParsingTool.parse_json_str_to_dictionary(system.serial_console.show())\
            .get_returned_value()
        ValidationTool.verify_field_value_in_output(serial_output, SystemConsts.SERIAL_CONSOLE_SYSRQ_CAPABILITIES,
                                                    SystemConsts.SERIAL_CONSOLE_DEFAULT_SYSRQ_CAPABILITIES
                                                    ).verify_result()


def _get_users_num(engines):
    users_num = 0
    output = engines.dut.run_cmd("w")
    pattern = r"(\d+)\s+users?"
    match = re.search(pattern, output)
    if match:
        users_num = int(match.group(1))
    return users_num


def _assert_number_of_users_disconnected(engines, users_num):
    current_users = _get_users_num(engines)
    output = ValidationTool.compare_values(current_users, users_num).get_returned_value()
    return output
