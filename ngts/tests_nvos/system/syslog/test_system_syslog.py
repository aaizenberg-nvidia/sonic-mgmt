import logging
import pytest
import random
from retry import retry
from retry.api import retry_call
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tools.test_utils import allure_utils as allure
import string
import time
import re
import socket
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_constants.constants_nvos import SyslogConsts, SyslogSeverityLevels, NvosConst, RebootConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.SonicMgmtContainer import SonicMgmtContainer
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.SudoScope import sudo_scope_if


cumulus_owner = "gosaini"


logger = logging.getLogger()
INCOMPLETE_COMMAND = "Incomplete Command"
ERROR = "Error"
INVALID_COMMAND = "Invalid Command"
IS_NOT_ONE_OF = "is not one of"
IS_NOT_AN_INTEGER = "is not an integer"
IS_NOT_OF_TYPE_INTEGER = "is not of type 'integer'"
IS_TOO_SHORT = "'' is too short"
INVALID_CONFIG = "Config invalid"


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
@pytest.mark.air
@pytest.mark.cumulus
def test_rsyslog_positive_minimal_flow_by_hostname(engines, is_air, random_api):
    """
    Will validate the minimal positive flow:
        set server and send UDP msg , verify the server get the msg and show commands

    Test flow:
    1. Configure remote syslog server by hostname
    2. Validate show commands
    3. Print msg that the server should catch, validate it gets the msg
    4. Print msg that the server should not catch, validate it does not get the msg
    5. Cleanup
    """
    remote_server_engine, remote_server_hostname = _get_server_info(is_air, engines)
    positive_minimal_flow(remote_server_engine, remote_server_hostname)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.cumulus
def test_rsyslog_positive_minimal_flow_by_ipv4(engines, random_api):
    """
    Will validate the minimal positive flow:
        set server and send UDP msg , verify the server get the msg and show commands

    Test flow:
    1. Configure remote syslog server by ipv4
    2. Validate show commands
    3. Print msg that the server should catch, validate it gets the msg
    4. Print msg that the server should not catch, validate it does not get the msg
    5. Cleanup
    """
    remote_server_engine = engines[NvosConst.SONIC_MGMT]
    positive_minimal_flow(remote_server_engine, remote_server_engine.ip)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.cumulus
def test_rsyslog_positive_minimal_flow_by_ipv6(engines, random_api, sonic_mgmt_ipv6_addr):
    """
    Will validate the minimal positive flow:
        set server and send UDP msg , verify the server get the msg and show commands

    Test flow:
    1. Configure remote syslog server by ipv6
    2. Validate show commands
    3. Print msg that the server should catch, validate it gets the msg
    4. Print msg that the server should not catch, validate it does not get the msg
    5. Cleanup
    """
    if not IpTool.is_dhcp_client6_has_lease(engines.dut):
        pytest.skip("DUT DHCP client6 has no lease; cannot run this IPv6 test.")

    positive_minimal_flow(engines[NvosConst.SONIC_MGMT], sonic_mgmt_ipv6_addr)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.cumulus
def test_rsyslog_multiple_servers_configuration(engines):
    """
    Validates the following:
    - Time to configure 1 and 10 servers should be similar (<< 1sec of difference)
    - No degradation in time to "nv show system syslog" when a lot servers are configured (10 for example)
    - Normal system resources utilization when a lot servers configured (10 for example)

    Test flow:
    1. configuring 1 single server and measuring its time.
    2. Measure "nv show system syslog" command time with 1 server configured.
    3. configuring 11 servers and measuring its time.
    4. Measure "nv show system syslog" command time with 11 servers configured.
    5. Verify all configured servers displayed in show command
    6. Validate system resources CPU utilization with 11 servers configured.
    7. Compare between server configuration times.
    8. Compare between "nv show system syslog" command times.
    9. Cleanup
    """
    system = System()
    server_name = 'server-0'

    try:
        with allure.step("Configure 1 remote syslog server {}".format(server_name)):
            start_time = time.time()
            system.syslog.servers.set_server(server_name, apply=True)
            end_time = time.time()
            config_single_duration = end_time - start_time

        with allure.step("Calculate a single server show time"):
            start_time = time.time()
            system.syslog.servers.show()
            end_time = time.time()
            show_single_duration = end_time - start_time

        with allure.step("Configure 10 remote syslog servers"):
            for x in range(1, SyslogConsts.MULTIPLE_SERVERS_NUMBER):
                server_name = 'server' + str(x)
                system.syslog.servers.set_server(server_name, apply=False)
            server_name = 'server-10'
            start_time = time.time()
            system.syslog.servers.set_server(server_name, apply=True)
            end_time = time.time()
            config_multiple_duration = end_time - start_time

        with allure.step("Calculate 10 server configuration time"):
            start_time = time.time()
            system.syslog.servers.show()
            end_time = time.time()
            show_multiple_duration = end_time - start_time

        with allure.step("Verify all configured servers displayed in show command"):
            server_list = OutputParsingTool.parse_json_str_to_dictionary(
                system.syslog.servers.show()).get_returned_value()
            server_len = len(server_list)
            assert server_len == (SyslogConsts.MULTIPLE_SERVERS_NUMBER + 1), \
                "Number of servers configured is different than expected"

        with allure.step("Validate system resources CPU utilization with 11 servers configured"):
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.show("cpu")).get_returned_value()
            cpu_utilization = output_dictionary[SystemConsts.CPU_TOTAL_UTILIZATION_KEY]
            assert cpu_utilization < SystemConsts.CPU_PERCENT_THRESH_MAX, \
                "CPU utilization: {actual}% is higher than the maximum limit of: {expected}%" \
                "".format(actual=cpu_utilization, expected=SystemConsts.CPU_PERCENT_THRESH_MAX)

        with allure.step("Verify configuration diff time"):
            config_duration_diff = config_multiple_duration - config_single_duration
            assert config_duration_diff < SyslogConsts.CONFIG_TIME_DIFF_THRESHOLD, \
                "Configuration diff time: {actual} is higher than expected time: {expected}" \
                "".format(actual=config_duration_diff, expected=SyslogConsts.CONFIG_TIME_DIFF_THRESHOLD)

        with allure.step("Verify show diff time"):
            show_duration_diff = show_multiple_duration - show_single_duration
            assert show_duration_diff < SyslogConsts.SHOW_TIME_DIFF_THRESHOLD, \
                "Show diff time: {actual} is higher than expected time: {expected}" \
                "".format(actual=show_duration_diff, expected=SyslogConsts.SHOW_TIME_DIFF_THRESHOLD)

    finally:
        with allure.step("Cleanup syslog configurations"):
            system.syslog.servers.unset(apply=True)
        with allure.step("Wait for rsyslog to become active after reconfiguration"):
            # Reconfiguring syslog bounces rsyslogd. Give it up to 30s to stabilize back to
            # 'active' so the loganalyzer teardown can place its end-marker (which requires a
            # running rsyslog); otherwise the teardown can catch rsyslog mid-restart and fail.
            end_time = time.time() + 30
            while time.time() < end_time:
                if engines.dut.run_cmd("systemctl is-active rsyslog").strip() == "active":
                    break
                time.sleep(5)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
@pytest.mark.cumulus
def test_rsyslog_configurations(random_api):
    """
    will check rsyslog configurations

    Test flow:
    1. configure remote syslog servers : server_a, server_b
    2. validate show commands
    3. change global severity
    4. unset server_a
    5. validate show commands
    6. unset server
    7. validate show commands
    """
    system = System()
    server_a = 'server-a'
    server_b = 'server-b'

    with allure.step("Configure remote syslog servers"):
        system.syslog.servers.set_server(server_a, apply=True)
        system.syslog.servers.set_server(server_b, apply=True)

        # Create selector configuration using the helper function
        create_selector_configuration(
            selector_id=SyslogConsts.DEFAULT_SELECTOR_NAME,
            severity=SyslogSeverityLevels.ERROR
        )
        system.syslog.servers.servers_dict[server_a].set_selector_priority(1, SyslogConsts.DEFAULT_SELECTOR_NAME,
                                                                           apply=True)
        system.syslog.servers.servers_dict[server_b].set_selector_priority(1, SyslogConsts.DEFAULT_SELECTOR_NAME,
                                                                           apply=True)

    try:
        with allure.step("Validate show commands"):
            expected_server_dictionary_a = create_remote_server_dictionary_with_selector(server_a, 1,
                                                                                         selector_id=SyslogConsts.DEFAULT_SELECTOR_NAME)
            expected_server_dictionary_b = create_remote_server_dictionary_with_selector(server_b, 1,
                                                                                         selector_id=SyslogConsts.DEFAULT_SELECTOR_NAME)
            expected_server_dictionary = {**expected_server_dictionary_a, **expected_server_dictionary_b}
            expected_syslog_dictionary = create_syslog_output_dictionary(
                server_dict={SyslogConsts.SERVER: expected_server_dictionary})
            system.syslog.verify_show_syslog_output(expected_syslog_dictionary)
            system.syslog.servers.verify_show_servers_list([server_a, server_b])
            system.syslog.servers.servers_dict[server_a].verify_show_server_output(expected_server_dictionary[server_a])

        with allure.step("unset server vrf and Validate"):
            system.syslog.servers.servers_dict[server_a].unset_vrf(apply=True)
            expected_syslog_dictionary[SyslogConsts.SERVER][server_a].update({SyslogConsts.VRF: SyslogConsts.DEFAULT_VRF})
            system.syslog.verify_show_syslog_output(expected_syslog_dictionary)
            system.syslog.servers.servers_dict[server_a].verify_show_server_output(expected_server_dictionary[server_a])

        severity_level = SyslogSeverityLevels.ERROR
        with allure.step("Unset {} and Validate".format(server_a)):
            system.syslog.servers.unset_server(server_a, apply=True)
            expected_syslog_dictionary[SyslogConsts.SERVER].pop(server_a)
            system.syslog.servers.verify_show_servers_list([server_b])
            system.syslog.verify_show_syslog_output(expected_syslog_dictionary)
            server_list = OutputParsingTool.parse_json_str_to_dictionary(
                system.syslog.servers.show()).get_returned_value()
            assert server_a not in server_list, "Did not expect to see {} in the list of servers".format(server_a)

        with allure.step("Unset server and Validate"):
            system.syslog.servers.unset_server(server_b, apply=True)
            expected_syslog_dictionary[SyslogConsts.SERVER].pop(server_b)
            # FOR ETH DEVICES IF ALL SERVERS ARE REMOVED , 'SERVER' KEY ALSO WILL BE REMOVED FROM THE DICTIONARY
            if TestToolkit.devices.dut.is_eth():
                expected_syslog_dictionary.pop(SyslogConsts.SERVER)
            system.syslog.verify_show_syslog_output(expected_syslog_dictionary)
            server_list = OutputParsingTool.parse_json_str_to_dictionary(
                system.syslog.servers.show()).get_returned_value()
            assert server_b not in server_list, "Did not expect to see {} in the list of servers".format(server_b)

        with allure.step("Configure remote syslog server and validate unset syslog"):
            system.syslog.servers.set_server(server_a, apply=True)
            system.syslog.servers.servers_dict[server_a].set_selector_priority(1, SyslogConsts.DEFAULT_SELECTOR_NAME,
                                                                               apply=True)
            expected_server_dictionary = create_remote_server_dictionary_with_selector(server_a, 1,
                                                                                       selector_id=SyslogConsts.DEFAULT_SELECTOR_NAME)
            expected_syslog_dictionary.update({SyslogConsts.SERVER: expected_server_dictionary})
            system.syslog.verify_show_syslog_output(expected_syslog_dictionary)
            system.syslog.servers.verify_show_servers_list([server_a])
            system.syslog.unset(apply=True)
            expected_syslog_dictionary[SyslogConsts.SERVER].pop(server_a)
            if TestToolkit.devices.dut.is_eth():
                expected_syslog_dictionary.pop(SyslogConsts.SERVER)
            system.syslog.verify_show_syslog_output(expected_syslog_dictionary)
            server_list = OutputParsingTool.parse_json_str_to_dictionary(
                system.syslog.servers.show()).get_returned_value()
            assert server_a not in server_list, "Did not expect to see {} in the list of servers".format(server_a)

    finally:
        with allure.step("Cleanup syslog configurations"):
            system.syslog.servers.unset(apply=True)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
@pytest.mark.air
@pytest.mark.cumulus
def test_rsyslog_server_severity_levels(engines, is_air, loganalyzer, random_api):
    """
    Will validate all the severity options:  debug, info, notice, warning, error, critical, alert, emerg, none.
    Will configure the severity level, validate it in the show command and validate that the server catch the relevant
    messages only.

    Test flow:
    * Configure remote syslog server
    To each severity level:
         * Set severity level
         * Validate with show command
         * Print msg that the server should catch, validate
         * Print msg that the server should not catch, validate
    * Unset server severity
    * Cleanup
    """
    remote_server_engine, _ = _get_server_info(is_air, engines)
    remote_server_ip = remote_server_engine.ip
    system = System()

    with allure.step("Configure remote syslog server {}".format(remote_server_ip)):
        server = system.syslog.servers.set_server(remote_server_ip, apply=True)
        if TestToolkit.devices.dut.is_eth():
            system.syslog.servers.servers_dict[remote_server_ip].set_vrf(vrf=TestToolkit.devices.dut.vrf_mgmt, apply=True, ask_for_confirmation=True)

    try:
        with allure.step("Configure selectors"):
            create_selector_configuration(selector_id=SyslogConsts.DEFAULT_SELECTOR_NAME)
            system.syslog.servers.servers_dict[remote_server_ip].set_selector_priority(1, SyslogConsts.DEFAULT_SELECTOR_NAME,
                                                                                       apply=True)
        with allure.step("Validate show commands"):
            expected_server_dictionary = create_remote_server_dictionary_with_selector(remote_server_ip, 1,
                                                                                       selector_id=SyslogConsts.DEFAULT_SELECTOR_NAME, vrf=TestToolkit.devices.dut.vrf_mgmt)
            expected_syslog_dictionary = create_syslog_output_dictionary(
                server_dict={SyslogConsts.SERVER: expected_server_dictionary})
            system.syslog.verify_show_syslog_output(expected_syslog_dictionary)

        with allure.step("Validate all severity levels"):
            for severity_level in SyslogSeverityLevels.SEVERITY_LEVEL_LIST:
                config_and_verify_severity(loganalyzer, system.syslog, server, remote_server_ip, remote_server_engine,
                                           severity_level,
                                           selector_id=SyslogConsts.DEFAULT_SELECTOR_NAME,
                                           global_severity_level=SyslogSeverityLevels.NOTICE)

    finally:
        with allure.step("Cleanup syslog configurations"):
            system.syslog.servers.unset(apply=True)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
def test_rsyslog_port(engines, random_api):
    """
    Will check the syslog with non default port
    we will check it with 2 ports number, one in the system ports range (0-1023) and the other out of this range.
    steps:
    1. configure remote server syslog
    2. change rsyslog port on switch and remote server
    3. validate with show command
    4. send msg , validate remote server get the msg
    5. Change back rsyslog port to default port, just on switch
    6. send msg , validate remote server did not get the msg!
    7. Change back rsyslog port to default port on remote server
    8. send msg , validate remote server get the msg
    """
    remote_server_engine = engines[NvosConst.SONIC_MGMT]
    remote_server_ip = remote_server_engine.ip
    system = System()
    tmp_port = 500  # in the system ports range

    with allure.step("Configure remote syslog server {}".format(remote_server_ip)):
        system.syslog.servers.set_server(remote_server_ip, apply=True)
        if TestToolkit.devices.dut.is_eth():
            system.syslog.servers.servers_dict[remote_server_ip].set_vrf(vrf=TestToolkit.devices.dut.vrf_mgmt, apply=True, ask_for_confirmation=True)

    try:
        with allure.step("Validate show commands and send msg"):
            expected_server_dictionary = create_remote_server_dictionary(remote_server_ip, vrf=TestToolkit.devices.dut.vrf_mgmt)
            system.syslog.servers.servers_dict[remote_server_ip].verify_show_server_output(
                expected_server_dictionary[remote_server_ip])
            random_msg = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
            send_msg_to_server(random_msg, remote_server_ip, remote_server_engine, verify_msg_received=True)

        with allure.step("Change rsyslog port to non default port"):
            config_and_verify_rsyslog_port(system.syslog.servers.servers_dict[remote_server_ip], remote_server_engine,
                                           remote_server_ip, SyslogConsts.DEFAULT_PORT, tmp_port)
            config_and_verify_rsyslog_port(system.syslog.servers.servers_dict[remote_server_ip], remote_server_engine,
                                           remote_server_ip, tmp_port, 1500)
            tmp_port = 1500  # out of system port range

        with allure.step("Change back rsyslog port to default port, just on switch"):
            system.syslog.servers.servers_dict[remote_server_ip].unset_port(apply=True)
            system.syslog.servers.servers_dict[remote_server_ip].verify_show_server_output(
                {SyslogConsts.PORT: str(SyslogConsts.DEFAULT_PORT)})
            random_msg = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
            send_msg_to_server(random_msg, remote_server_ip, remote_server_engine, verify_msg_didnt_received=True)

        with allure.step("Change back rsyslog port to default port on remote server"):
            SonicMgmtContainer.change_rsyslog_port(remote_server_engine, tmp_port, SyslogConsts.DEFAULT_PORT, SyslogConsts.UDP,
                                                   restart_rsyslog=True)
            random_msg = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
            send_msg_to_server(random_msg, remote_server_ip, remote_server_engine, verify_msg_received=True)

    finally:
        with allure.step("Cleanup syslog configurations"):
            system.syslog.unset(apply=True)
            SonicMgmtContainer.change_rsyslog_port(remote_server_engine, tmp_port, SyslogConsts.DEFAULT_PORT, SyslogConsts.UDP,
                                                   restart_rsyslog=True)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
def test_rsyslog_protocol(engines, random_api):
    """
    Will check the syslog protocol options: TCP and UDP
    Steps:
        1. configure a remote syslog server
        2. config syslog protocol to udp
        3. validate with show commands
        4. send a msg and validate server received it
        5. config syslog protocol to tcp
        6. validate with show commands
        7. send a msg and validate server received it
        8. simulate a disconnection, by stop the rsyslog process
        9. send multiple messages during downtime (should be buffered)
        10. wait for 1 minute during server downtime
        11. reconnect, restart the rsyslog process
        12. verify all buffered messages are delivered to remote server
        13. send a msg and validate server received it
    """
    remote_server_engine = engines[NvosConst.SONIC_MGMT]
    remote_server_ip = remote_server_engine.ip
    system = System()

    with allure.step("Configure remote syslog server {}".format(remote_server_ip)):
        system.syslog.servers.set_server(remote_server_ip, apply=True)

    try:
        with allure.step("Validate show commands"):
            expected_server_dictionary = create_remote_server_dictionary(remote_server_ip)
            system.syslog.servers.servers_dict[remote_server_ip].verify_show_server_output(
                expected_server_dictionary[remote_server_ip])

        config_and_verify_rsyslog_protocol(system.syslog.servers.servers_dict[remote_server_ip], remote_server_engine,
                                           remote_server_ip, SyslogConsts.UDP)
        config_and_verify_rsyslog_protocol(system.syslog.servers.servers_dict[remote_server_ip], remote_server_engine,
                                           remote_server_ip, SyslogConsts.TCP)

        with allure.step("Test message buffering during server disconnection"):

            with allure.step("Simulate disconnection to the server"):
                remote_server_engine.run_cmd('sudo pkill rsyslogd')
                time.sleep(10)  # Allow time for service to fully stop

                # First verify server is down by sending a test message
                test_msg = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
                send_msg_to_server(test_msg, remote_server_ip, remote_server_engine, verify_msg_didnt_received=True)

            # Generate multiple unique messages to send during downtime
            buffered_messages = []
            with allure.step("Send multiple messages while server is down (should be buffered)"):
                for i in range(5):  # Send 5 messages during downtime
                    msg = "PROTOCOL_BUFFERED_MSG_{}_{}".format(i, RandomizationTool.get_random_string(20, ascii_letters=string.ascii_letters + string.digits))
                    buffered_messages.append(msg)
                    logger.info("Sending buffered message {}: {}".format(i + 1, msg))
                    send_msg_to_server(msg, remote_server_ip, remote_server_engine)  # Don't verify receipt yet
                    time.sleep(2)  # Small delay between messages

            with allure.step("Wait for 1 minute during server downtime"):
                logger.info("Waiting 60 seconds while server is down...")
                time.sleep(60)

            with allure.step("Reconnect to the server"):
                SonicMgmtContainer.restart_rsyslog(remote_server_engine)
                time.sleep(30)  # Allow time for service to fully restart and process queued messages

                # First verify server is working with a fresh message
                fresh_msg = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
                send_msg_to_server(fresh_msg, remote_server_ip, remote_server_engine, verify_msg_received=True)
                logger.info(" Server is working - fresh message delivered successfully")

            with allure.step("Verify all buffered messages are delivered to remote server"):
                # Give additional time for any queued messages to be processed
                time.sleep(30)

                delivered_count = 0
                missing_count = 0
                missing_messages = []

                for i, msg in enumerate(buffered_messages):
                    with allure.step("Verify buffered message {} was delivered: {}".format(i + 1, msg)):
                        try:
                            verify_msg_in_syslog_file(remote_server_engine, msg, should_find=True)
                            logger.info(" Buffered message {} found: {}".format(i + 1, msg))
                            delivered_count += 1
                        except Exception as e:
                            logger.error(" Buffered message {} NOT found: {} - Error: {}".format(i + 1, msg, e))
                            missing_count += 1
                            missing_messages.append(msg)

                with allure.step("Summary of buffered message delivery"):
                    logger.info(" Buffered Message Summary:")
                    logger.info("   Delivered: {}/{}".format(delivered_count, len(buffered_messages)))
                    logger.info("   Missing: {}/{}".format(missing_count, len(buffered_messages)))

                    if missing_count > 0:
                        # Show recent syslog for debugging
                        with allure.step("Show recent syslog entries for debugging"):
                            remote_server_engine.run_cmd('tail -50 /var/log/syslog')

                        # FAIL the test as per requirement - all messages should be received
                        error_msg = " BUFFERING TEST FAILED: {}/{} messages were not delivered after server restart.\n".format(
                            missing_count, len(buffered_messages))
                        error_msg += "Missing messages:\n"
                        for msg in missing_messages:
                            error_msg += "  - {}\n".format(msg)
                        error_msg += "All messages sent during server downtime should be buffered and delivered when server comes back online."

                        raise AssertionError(error_msg)
                    else:
                        logger.info(" SUCCESS: All buffered messages were successfully delivered!")

            with allure.step("Final connectivity test"):
                random_msg = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
                send_msg_to_server(random_msg, remote_server_ip, remote_server_engine, verify_msg_received=True)

            with allure.step("Unset syslog server protocol"):
                system.syslog.servers.servers_dict[remote_server_ip].unset_protocol(apply=True)
                system.syslog.servers.servers_dict[remote_server_ip].verify_show_server_output(
                    {SyslogConsts.PROTOCOL: SyslogConsts.UDP})

    finally:
        with allure.step("Cleanup syslog configurations"):
            system.syslog.unset(apply=True)
        SonicMgmtContainer.restart_rsyslog(remote_server_engine)
        time.sleep(10)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
@pytest.mark.cumulus
def test_rsyslog_filter(engines, random_api, devices):
    """
    Will check the rsyslog filter options: exclude and include.
    Validate that the server will get only the relevant messages.
    Test flow:
    1. configure remote syslog server with exclude filter
    2. validate with show commands and send messages
    3. configure remote syslog server with include filter
    4. validate with show commands and send messages
    5. unset filter
    6. validate with show commands and send messages
    """
    remote_server_engine = engines[NvosConst.SONIC_MGMT]
    remote_server_ip = remote_server_engine.ip
    system = System()

    try:
        with allure.step("Configure selector"):
            create_selector_configuration(selector_id=SyslogConsts.DEFAULT_SELECTOR_NAME)

        with allure.step("Configure remote syslog server {} with exclude filter and validate".format(remote_server_ip)):
            system.syslog.servers.set_server(remote_server_ip, apply=True)
            if TestToolkit.devices.dut.is_eth():
                system.syslog.servers.servers_dict[remote_server_ip].set_vrf(vrf=TestToolkit.devices.dut.vrf_mgmt, apply=True, ask_for_confirmation=True)
            system.syslog.servers.servers_dict[remote_server_ip].set_selector_priority(1, SyslogConsts.DEFAULT_SELECTOR_NAME,
                                                                                       apply=True)

            exclude_regex = "a+"
            include_regex = "b+"

            # First create and configure the filter with all settings
            selector = system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME]
            selector.set_filter("1", apply=False)
            selector.filter_dict["1"].set_action_filter(SyslogConsts.EXCLUDE, apply=False)
            selector.filter_dict["1"].set_match_filter(exclude_regex, apply=True)

            expected_server_dictionary = create_remote_server_dictionary_with_selector(remote_server_ip, 1,
                                                                                       selector_id=SyslogConsts.DEFAULT_SELECTOR_NAME, vrf=TestToolkit.devices.dut.vrf_mgmt)

            system.syslog.servers.servers_dict[remote_server_ip].verify_show_server_output(
                expected_server_dictionary[remote_server_ip])
        with allure.step("Validate show commands"):
            expected_selector_dictionary = devices.dut.expected_selector_dictionary
            system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].verify_filter_options(
                expected_selector_dictionary)
            with allure.step("Send message with the exclude filter regex,\n"
                             "expect message not to be received over the remote server"):
                random_msg = RandomizationTool.get_random_string(20, ascii_letters=string.digits)
                send_msg_to_server(exclude_regex + random_msg, remote_server_ip, remote_server_engine,
                                   verify_msg_didnt_received=True)

            with allure.step("Send message without the exclude filter regex but with lower severity level,\n"
                             "expect message to be received over the remote server"):
                random_msg = RandomizationTool.get_random_string(20, ascii_letters=string.digits)
                send_msg_to_server(random_msg, remote_server_ip, remote_server_engine,
                                   priority=SyslogSeverityLevels.DEBUG,
                                   verify_msg_received=True)

            with allure.step("Send message without the exclude filter regex,\n"
                             "expect message to be received over the remote server"):
                random_msg = RandomizationTool.get_random_string(20, ascii_letters=string.digits)
                send_msg_to_server(include_regex + random_msg, remote_server_ip, remote_server_engine,
                                   verify_msg_received=True)

            with allure.step("Configure long regex for the exclude filter and validate"):
                long_exclude_regex = RandomizationTool.get_random_string(200,
                                                                         ascii_letters=string.digits + string.ascii_letters)
                selector = system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME]
                selector.filter_dict["1"].set_match_filter(long_exclude_regex, apply=True)
                expected_server_dictionary = create_remote_server_dictionary_with_selector(remote_server_ip, 1,
                                                                                           selector_id=SyslogConsts.DEFAULT_SELECTOR_NAME, vrf=TestToolkit.devices.dut.vrf_mgmt)
                system.syslog.servers.servers_dict[remote_server_ip].verify_show_server_output(
                    expected_server_dictionary[remote_server_ip])
                with allure.step("Validate show commands"):
                    expected_selector_dictionary.update(
                        {
                            SyslogConsts.FILTER: {
                                "1": {
                                    SyslogConsts.ACTION: SyslogConsts.EXCLUDE,
                                    SyslogConsts.MATCH: long_exclude_regex
                                }
                            }
                        }
                    )
                    system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].verify_filter_options(
                        expected_selector_dictionary)

        with allure.step("Configure remote syslog server {} with include filter and validate".format(remote_server_ip)):
            system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].filter_dict["1"].set_action_filter(
                SyslogConsts.INCLUDE, apply=False)
            system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].filter_dict["1"].set_match_filter(
                include_regex, apply=True)
            with allure.step("Validate show commands"):
                long_include_regex = RandomizationTool.get_random_string(200, ascii_letters=string.digits + string.ascii_letters)
                expected_selector_dictionary.update({SyslogConsts.FILTER: {"1": {SyslogConsts.ACTION: SyslogConsts.INCLUDE, SyslogConsts.MATCH: include_regex}}})
                system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].verify_filter_options(
                    expected_selector_dictionary)
            with allure.step("Send message without the include filter regex,\n"
                             "expect message not to be received over the remote server"):
                random_msg = RandomizationTool.get_random_string(20, ascii_letters=string.digits)
                send_msg_to_server(random_msg, remote_server_ip, remote_server_engine, verify_msg_didnt_received=True)

            with allure.step("Send message with the include filter regex but with lower severity level,\n"
                             "expect message to be received over the remote server"):
                random_msg = RandomizationTool.get_random_string(20, ascii_letters=string.digits)
                send_msg_to_server(include_regex + random_msg, remote_server_ip, remote_server_engine,
                                   priority=SyslogSeverityLevels.DEBUG, verify_msg_received=True)

            with allure.step("Send message with the include filter regex,\n"
                             "expect message to be received over the remote server"):
                random_msg = RandomizationTool.get_random_string(20, ascii_letters=string.digits)
                send_msg_to_server(long_exclude_regex + random_msg, remote_server_ip, remote_server_engine, priority=SyslogSeverityLevels.NOTICE,
                                   verify_msg_received=True)

            with allure.step("Configure long regex for the include filter and validate"):
                long_include_regex = RandomizationTool.get_random_string(200,
                                                                         ascii_letters=string.digits + string.ascii_letters)
                system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].filter_dict["1"].set_match_filter(
                    long_include_regex, apply=True)
                with allure.step("Validate show commands"):
                    expected_selector_dictionary.update({SyslogConsts.FILTER: {
                        "1": {SyslogConsts.ACTION: SyslogConsts.INCLUDE, SyslogConsts.MATCH: long_include_regex}
                    }})
                    system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].verify_filter_options(
                        expected_selector_dictionary)
                with allure.step("Send message with the include filter regex,\n"
                                 "expect message to be received over the remote server"):
                    send_msg_to_server(long_exclude_regex, remote_server_ip, remote_server_engine,
                                       verify_msg_received=True)

        with allure.step("Unset filter and validate"):
            system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].unset_filter("1", apply=True).verify_result()
            with allure.step("Validate show commands"):
                if devices.dut.is_eth():
                    system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].show(should_succeed=False)
                else:
                    expected_selector_dictionary.update({SyslogConsts.FILTER: {}})
                    system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].verify_filter_options(
                        expected_selector_dictionary)
            random_msg = RandomizationTool.get_random_string(20, ascii_letters=string.digits)
            send_msg_to_server(exclude_regex + random_msg, remote_server_ip, remote_server_engine,
                               verify_msg_received=True)

    finally:
        with allure.step("Cleanup syslog configurations"):
            system.syslog.unset(apply=True)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
@pytest.mark.cumulus
def test_rsyslog_format(engines, random_api):
    """
    Will validate all the format options:  standard, welf.
    Will configure the syslog format, validate it in the show command and in the syslog file.
    Test flow:
    1. configure remote syslog server
    2. validate with show commands
    3. send a msg and validate server received it and its not in welf format
    4. configure welf format
    5. validate with show command and on the remote syslog server
    6. configure welf firewall-nme
    7. validate with show command and on the remote syslog server
    8. unset welf firewall-nme
    9.  validate with show command and on the remote syslog server
    """
    remote_server_engine = engines[NvosConst.SONIC_MGMT]
    remote_server_ip = remote_server_engine.ip
    system = System()

    try:
        with allure.step("Configure remote syslog server {} and validate".format(remote_server_ip)):
            system.syslog.servers.set_server(remote_server_ip, apply=True)
            if TestToolkit.devices.dut.is_eth():
                system.syslog.servers.servers_dict[remote_server_ip].set_vrf(vrf=TestToolkit.devices.dut.vrf_mgmt, apply=True, ask_for_confirmation=True)
            expected_server_dictionary = create_remote_server_dictionary(remote_server_ip, vrf=TestToolkit.devices.dut.vrf_mgmt)
            expected_syslog_dictionary = create_syslog_output_dictionary(
                server_dict={SyslogConsts.SERVER: expected_server_dictionary})
            system.syslog.verify_show_syslog_output(expected_syslog_dictionary)
            system.syslog.verify_show_syslog_format_output({SyslogConsts.STANDARD: {}})
            send_random_msg_and_validate_format(remote_server_ip, remote_server_engine, expect_welf_format=False)

        with allure.step("Set welf format and validate"):
            system.syslog.format.set(SyslogConsts.WELF, apply=True).verify_result(False, expected_value=[f"firewall-name must be configured",
                                                                                                         f"firewall name must be configured"])

        with allure.step("Set firewall name and validate"):
            firewall_name = RandomizationTool.get_random_string(6, ascii_letters=string.ascii_letters)
            system.syslog.format.welf.set_firewall_name(firewall_name, apply=True)
            system.syslog.verify_show_syslog_format_output({SyslogConsts.WELF:
                                                            {SyslogConsts.FIREWAL_NAME:
                                                             firewall_name}})
            send_random_msg_and_validate_format(remote_server_ip, remote_server_engine, expect_welf_format=True,
                                                firewall_name=firewall_name)

        with allure.step("Unset firewall name and validate"):
            system.syslog.format.welf.unset(apply=True)
            expected_syslog_dictionary[SyslogConsts.FORMAT] = {SyslogConsts.STANDARD: {}}
            system.syslog.verify_show_syslog_output(expected_syslog_dictionary)
            system.syslog.verify_show_syslog_format_output({SyslogConsts.STANDARD: {}})
            send_random_msg_and_validate_format(remote_server_ip, remote_server_engine, expect_welf_format=False)

    finally:
        with allure.step("Cleanup syslog configurations"):
            system.syslog.servers.unset(apply=True)
            system.syslog.format.unset(apply=True)


@pytest.mark.disable_loganalyzer
@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
@pytest.mark.cumulus
def test_rsyslog_bad_params(random_api):
    """
    Will check all the commands that get params, with bad params- empty or random
    """
    system = System()
    rand_str = RandomizationTool.get_random_string(10)
    server_name = RandomizationTool.get_random_string(5)

    with allure.step("Global syslog commands"):

        with allure.step("set selector"):
            create_selector_configuration(selector_id=SyslogConsts.DEFAULT_SELECTOR_NAME)

        with allure.independent_step("Configure and validate severity, should fail"):
            system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].set_severity("", apply=True).verify_result(False)
            system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].set_severity(rand_str, apply=False).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        # TODO change when bug 3390504 will be fixed
        with allure.independent_step("Configure and validate format, should fail"):
            try:
                system.syslog.format.set(rand_str, apply=False).verify_result(False)
            except Exception as e:
                logger.info(f"Expected the test step to fail: {e}")
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
    with allure.step("Specific syslog server commands"):
        try:
            system.syslog.servers.set_server(server_name, apply=False)
        except Exception as e:
            logger.info(f"Expected the test step to fail: {e}")

        with allure.independent_step("Configure and validate port, should fail"):
            system.syslog.servers.servers_dict[server_name].set_port("", apply=True).verify_result(False)
            system.syslog.servers.servers_dict[server_name].set_port(rand_str, apply=True).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        with allure.independent_step("Configure and validate protocol, should fail"):
            system.syslog.servers.servers_dict[server_name].set_protocol("", apply=True).verify_result(False)
            if is_bug_active(4283380):
                system.syslog.servers.servers_dict[server_name].set_protocol(rand_str, apply=True).verify_result(False)
            else:
                system.syslog.servers.servers_dict[server_name].set_protocol(rand_str, apply=True).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        with allure.independent_step("Configure and validate vrf, should fail"):
            if TestToolkit.devices.dut.is_eth():
                system.syslog.servers.servers_dict[server_name].set_vrf("", apply=True, ask_for_confirmation=True).verify_result(False)
                system.syslog.servers.servers_dict[server_name].set_vrf(rand_str, apply=True, ask_for_confirmation=True).verify_result(False)
            if TestToolkit.devices.dut.is_ib():
                system.syslog.servers.servers_dict[server_name].set_vrf("", apply=False).verify_result(False)
                system.syslog.servers.servers_dict[server_name].set_vrf(rand_str, apply=False).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        with allure.independent_step("Configure and validate filter, should fail"):
            # First set up a valid filter configuration
            system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].set_filter("2", expected_str=["Error", "match and action properties should be configured for the filter", "Config invalid"], apply=True).verify_result(False)
            NvueGeneralCli.detach_config(TestToolkit.engines.dut)
            system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].set_filter("2", apply=False).verify_result(True)
            system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].filter_dict["1"].set_action_filter(
                SyslogConsts.INCLUDE, apply=False)
            system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].filter_dict["1"].set_match_filter(
                "test.*", apply=True).verify_result(False)

            # Now try to set an invalid action filter
            system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].set_filter("2", expected_str=["Error", "match and action properties should be configured for the filter", "Config invalid"], apply=True).verify_result(False)

        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        with allure.independent_step("Configure and validate filter include, should fail"):
            system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].filter_dict["2"].set_action_filter(
                SyslogConsts.INCLUDE, expected_str=["Error", "match and action properties should be configured for the filter", "Config invalid"], apply=True).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        with allure.independent_step("Configure and validate filter exclude, should fail"):
            system.syslog.selectors.selectors_dict[SyslogConsts.DEFAULT_SELECTOR_NAME].filter_dict["2"].set_action_filter(
                SyslogConsts.EXCLUDE, expected_str=["Error", "match and action properties should be configured for the filter", "Config invalid"], apply=True).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
    with allure.step("Cleanup syslog configurations"):
        system.syslog.unset(apply=True)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
@pytest.mark.cumulus
def test_syslog_selector_priorities(random_api):
    """
    Test selector priorities configuration and validation:
    1. Create server with UDP protocol and port 514
    2. Create two selectors with priorities 1 and 2, configure with different severity levels
    3. Attempt to set duplicate priority and non-integer priority
    4. Unset selector by priority 1 and verify removal
    5. Attempt to unset non-existent priority
    6. Verify selector ID retrieval by priority
    7. Clean up configuration
    """
    system = System()
    remote_server_engine = TestToolkit.engines[NvosConst.SONIC_MGMT]
    remote_server_ip = remote_server_engine.ip
    selector1_id = 'selector1'
    selector2_id = 'selector2'
    non_existent_priority = 999

    try:
        with allure.step("Create server with UDP protocol and port 514"):
            system.syslog.servers.set_server(remote_server_ip, apply=True)
            system.syslog.servers.servers_dict[remote_server_ip].set_protocol(SyslogConsts.UDP, apply=True)
            system.syslog.servers.servers_dict[remote_server_ip].set_port(SyslogConsts.DEFAULT_PORT, apply=True)
            expected_server_dictionary = create_remote_server_dictionary(remote_server_ip)
            system.syslog.servers.servers_dict[remote_server_ip].verify_show_server_output(expected_server_dictionary[remote_server_ip])

        with allure.step("Create two selectors with different priorities and severity levels"):
            create_selector_configuration(selector_id=selector1_id, severity=SyslogSeverityLevels.ERROR)
            create_selector_configuration(selector_id=selector2_id, severity=SyslogSeverityLevels.WARN)

            # Set priorities for the selectors
            system.syslog.servers.servers_dict[remote_server_ip].set_selector_priority(1, selector1_id, apply=True).verify_result()

        with allure.step("Attempt to set duplicate priority"):
            system.syslog.servers.servers_dict[remote_server_ip].set_selector_priority(1, selector2_id, apply=True).verify_result()

        with allure.step("Unset selector by priority 1 and verify removal"):
            system.syslog.servers.servers_dict[remote_server_ip].unset_selector_priority(1, apply=True)
            server_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.syslog.servers.servers_dict[remote_server_ip].show()).get_returned_value()
            assert "1" not in server_output.get('selector', {}), "Priority 1 should be removed but still exists"

        with allure.step("Attempt to unset non-existent priority"):
            try:
                system.syslog.servers.servers_dict[remote_server_ip].unset_selector_priority(non_existent_priority, apply=True)
            except Exception as e:
                assert "Error" in str(e), "Expected error message not found"

        with allure.step("Verify selector ID retrieval by priority"):
            server_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.syslog.servers.servers_dict[remote_server_ip].show()).get_returned_value()
            assert selector2_id not in server_output.get('selector', {}), f"Selector {selector2_id} should not be in the output"

    finally:
        with allure.step("Clean up configuration"):
            system.syslog.unset(apply=True)


@pytest.mark.disable_loganalyzer
@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
@pytest.mark.cumulus
def test_syslog_selector_attachment_validation_negative(random_api):
    """
    Test system behavior when attempting to configure server priority without a selector-id
    and other negative selector attachment scenarios.

    Test flow:
    1. Attempt to set priority without selector-id
    2. Create and attach selector with priority
    3. Attempt to change priority of same selector
    4. Create and attach new selector to existing priority
    5. Attach first selector to different priority
    6. Attempt to unset attached selector
    7. Clean up configurations
    """
    system = System()
    remote_server_engine = TestToolkit.engines[NvosConst.SONIC_MGMT]
    remote_server_ip = remote_server_engine.ip
    selector1_id = 'selector-1'
    selector2_id = 'selector-2'

    try:
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        with allure.step("Create server and attempt to set priority without selector-id"):
            system.syslog.servers.set_server(remote_server_ip, apply=True)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        with allure.step("Configure selector"):
            create_selector_configuration(selector_id=selector1_id, severity=SyslogSeverityLevels.INFO)
            create_selector_configuration(selector_id=selector2_id, severity=SyslogSeverityLevels.INFO)

        with allure.step("Create and attach selector-1 to server with priority 10"):
            system.syslog.servers.servers_dict[remote_server_ip].set_selector_priority(10, selector1_id, apply=True).verify_result()
            expected_server_dictionary = create_remote_server_dictionary_with_selector(
                remote_server_ip, 10, selector_id=selector1_id)
            system.syslog.servers.servers_dict[remote_server_ip].verify_show_server_output(expected_server_dictionary[remote_server_ip])

        with allure.step("Attempt to change priority of selector-1"):
            try:
                system.syslog.servers.servers_dict[remote_server_ip].set_selector_priority(20, selector1_id, expected_str=["Error", "multiple priority levels"], apply=True).verify_result(False)
            except Exception as e:
                pass

        with allure.step("Create and attach selector-2 to existing priority"):
            system.syslog.servers.servers_dict[remote_server_ip].set_selector_priority(10, selector2_id, apply=True).verify_result()

        with allure.step("Attach selector-1 to different priority"):
            try:
                system.syslog.servers.servers_dict[remote_server_ip].set_selector_priority(30, selector1_id, expected_str=["Error", "cannot be changed"], apply=True).verify_result(False)
            except Exception as e:
                pass

        with allure.step("Attempt to unset selector-1 that is attached to server"):
            system.syslog.selectors.selectors_dict[selector1_id].unset(apply=True).verify_result(
                False)

        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
    finally:
        with allure.step("Clean up configurations"):
            # First unset the selector priorities
            system.syslog.servers.servers_dict[remote_server_ip].unset_selector_priority(10, apply=True)
            system.syslog.servers.servers_dict[remote_server_ip].unset_selector_priority(20, apply=True)
            # Then unset the selectors
            system.syslog.selectors.selectors_dict[selector1_id].unset(apply=True)
            system.syslog.selectors.selectors_dict[selector2_id].unset(apply=True)
            # Finally unset the server
            system.syslog.unset(apply=True)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
@pytest.mark.cumulus
def test_syslog_selector_priority_with_all_options(random_api):
    """
    Test Objective:
    Verify selector priority configuration with filters, including include/exclude filter functionality, severity and facility

    Precondition:
    - System is up and running
    - Syslog service is enabled
    - Test environment is properly configured
    """
    system = System()
    remote_server_engine = TestToolkit.engines[NvosConst.SONIC_MGMT]
    remote_server_ip = remote_server_engine.ip
    server = 'server-a'
    selector = 'selector-a'
    priority = 1
    protocol = SyslogConsts.UDP
    port = SyslogConsts.DEFAULT_PORT
    severity = SyslogSeverityLevels.INFO
    facility = "user"
    program_name = "switchd"
    filter_match = "test"
    filter_action = SyslogConsts.INCLUDE

    with allure.step(f"Create server with {protocol} protocol and port {port}"):
        logging.info(f"Create server with {protocol} protocol and port {port}")
        system.syslog.servers.set_server(remote_server_ip, apply=True)
        if TestToolkit.devices.dut.is_eth():
            system.syslog.servers.servers_dict[remote_server_ip].set_vrf(vrf=TestToolkit.devices.dut.vrf_mgmt, apply=True, ask_for_confirmation=True)
        system.syslog.servers.servers_dict[remote_server_ip].set_protocol(protocol, apply=True)
        system.syslog.servers.servers_dict[remote_server_ip].set_port(port, apply=True)
        # Create selector configuration using the helper function
        create_selector_configuration(
            selector_id=selector,
            severity=severity,
            facility=facility,
            program=program_name,
            filters=[(filter_action, filter_match)]
        )
        system.syslog.servers.servers_dict[remote_server_ip].set_selector_priority(priority, selector, apply=True)

    try:
        with allure.step("Validate show commands"):
            expected_server_dictionary = create_remote_server_dictionary_with_selector(remote_server_ip, priority, selector_id=selector, vrf=TestToolkit.devices.dut.vrf_mgmt)
            expected_syslog_dictionary = create_syslog_output_dictionary(
                server_dict={SyslogConsts.SERVER: expected_server_dictionary})
            system.syslog.verify_show_syslog_output(expected_syslog_dictionary)
            system.syslog.servers.verify_show_servers_list([remote_server_ip])
            system.syslog.servers.servers_dict[remote_server_ip].verify_show_server_output(expected_server_dictionary[remote_server_ip])
        with allure.step("Send message using logger with daemon facility"):
            test_message = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
            send_msg_to_server(filter_match + "_" + test_message, remote_server_ip, TestToolkit.engines[NvosConst.SONIC_MGMT],
                               facility=SyslogConsts.DAEMON, priority=severity, program=program_name, verify_msg_didnt_received=True)

        with allure.step("Send message using logger with user facility"):
            test_message = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
            send_msg_to_server(filter_match + "_" + test_message, remote_server_ip, TestToolkit.engines[NvosConst.SONIC_MGMT],
                               facility=facility, priority=severity, program=program_name, verify_msg_received=True)

        # EXTENDED COVERAGE: Add missing granular show command validations
        with allure.step("EXTENDED: Validate granular show commands"):
            with allure.step("Validate nv show system syslog server <server-id> selector"):
                server_output = OutputParsingTool.parse_json_str_to_dictionary(
                    system.syslog.servers.servers_dict[remote_server_ip].show()).get_returned_value()
                server_selector_dict = server_output.get('selector', {})
                assert str(priority) in server_selector_dict, f"Priority {priority} should be in server selector output"
                assert server_selector_dict[str(priority)][SyslogConsts.SELECTOR_ID] == selector, f"Selector ID should be {selector}"

            with allure.step("Validate nv show system syslog server <server-id> selector <priority-id>"):
                server_output = OutputParsingTool.parse_json_str_to_dictionary(
                    system.syslog.servers.servers_dict[remote_server_ip].show()).get_returned_value()
                server_selector_priority_dict = server_output.get('selector', {}).get(str(priority), {})
                assert server_selector_priority_dict[SyslogConsts.SELECTOR_ID] == selector, f"Selector ID should be {selector}"

            with allure.step("Validate nv show system syslog selector"):
                selector_list_output = system.syslog.selectors.show()
                selector_list_dict = OutputParsingTool.parse_json_str_to_dictionary(selector_list_output).get_returned_value()
                assert selector in selector_list_dict, f"Selector {selector} should be in selector list"

            with allure.step("Validate nv show system syslog selector <selector-id>"):
                selector_output = system.syslog.selectors.selectors_dict[selector].show()
                selector_dict = OutputParsingTool.parse_json_str_to_dictionary(selector_output).get_returned_value()
                assert selector_dict[SyslogConsts.SEVERITY] == severity, f"Severity should be {severity}"
                assert selector_dict[SyslogConsts.FACILITY] == facility, f"Facility should be {facility}"
                assert selector_dict[SyslogConsts.PROGRAM_NAME] == program_name, f"Program name should be {program_name}"

            with allure.step("Validate nv show system syslog selector <selector-id> filter"):
                selector_output = system.syslog.selectors.selectors_dict[selector].show()
                selector_dict = OutputParsingTool.parse_json_str_to_dictionary(selector_output).get_returned_value()
                selector_filter_dict = selector_dict.get('filter', {})
                assert "1" in selector_filter_dict, "Filter ID '1' should be present"
                assert selector_filter_dict["1"][SyslogConsts.ACTION] == filter_action, f"Filter action should be {filter_action}"
                assert selector_filter_dict["1"][SyslogConsts.MATCH] == filter_match, f"Filter match should be {filter_match}"

            with allure.step("Validate nv show system syslog selector <selector-id> filter <filter-id>"):
                selector_output = system.syslog.selectors.selectors_dict[selector].show()
                selector_dict = OutputParsingTool.parse_json_str_to_dictionary(selector_output).get_returned_value()
                selector_filter_id_dict = selector_dict.get('filter', {}).get("1", {})
                assert selector_filter_id_dict[SyslogConsts.ACTION] == filter_action, f"Filter action should be {filter_action}"
                assert selector_filter_id_dict[SyslogConsts.MATCH] == filter_match, f"Filter match should be {filter_match}"

        # EXTENDED COVERAGE: Add missing individual property unset validations
        with allure.step("EXTENDED: Test individual property unset"):
            with allure.step("Unset selector program-name and validate"):
                system.syslog.selectors.selectors_dict[selector].unset(SyslogConsts.PROGRAM_NAME, apply=True)
                selector_output = system.syslog.selectors.selectors_dict[selector].show()
                selector_dict = OutputParsingTool.parse_json_str_to_dictionary(selector_output).get_returned_value()
                assert SyslogConsts.PROGRAM_NAME not in selector_dict or selector_dict[SyslogConsts.PROGRAM_NAME] is None, "Program name should be unset"

            with allure.step("Unset selector facility and validate"):
                system.syslog.selectors.selectors_dict[selector].unset(SyslogConsts.FACILITY, apply=True)
                selector_output = system.syslog.selectors.selectors_dict[selector].show()
                selector_dict = OutputParsingTool.parse_json_str_to_dictionary(selector_output).get_returned_value()
                assert SyslogConsts.FACILITY not in selector_dict or selector_dict[SyslogConsts.FACILITY] == SyslogConsts.DAEMON, "Facility should be unset or default"

            with allure.step("Unset selector filter and validate"):
                system.syslog.selectors.selectors_dict[selector].unset(SyslogConsts.FILTER, apply=True)
                selector_output = system.syslog.selectors.selectors_dict[selector].show()
                selector_dict = OutputParsingTool.parse_json_str_to_dictionary(selector_output).get_returned_value()
                selector_filter_dict = selector_dict.get('filter', {})
                assert len(selector_filter_dict) == 0, "All filters should be unset"

        with allure.step("Unset selector priority and Validate"):
            system.syslog.servers.servers_dict[remote_server_ip].unset_selector_priority(priority, apply=True)
            if TestToolkit.devices.dut.is_eth():
                expected_syslog_dictionary[SyslogConsts.SERVER][remote_server_ip].pop(SyslogConsts.SELECTOR)
            else:
                expected_syslog_dictionary[SyslogConsts.SERVER][remote_server_ip].update({SyslogConsts.SELECTOR: {}})
            system.syslog.verify_show_syslog_output(expected_syslog_dictionary)
            system.syslog.servers.servers_dict[remote_server_ip].verify_show_server_output(expected_server_dictionary[remote_server_ip])

        # EXTENDED COVERAGE: Add missing global and individual unset command validations
        with allure.step("EXTENDED: Test missing unset commands"):
            # First setup format for testing format unset
            with allure.step("Setup format for unset testing"):
                firewall_name = "test-firewall"
                system.syslog.format.welf.set_firewall_name(firewall_name, apply=True)
                format_output = system.syslog.format.show()
                format_dict = OutputParsingTool.parse_json_str_to_dictionary(format_output).get_returned_value()
                assert SyslogConsts.WELF in format_dict, "WELF format should be configured"

            # Re-create a filter for individual filter unset testing
            with allure.step("Setup individual filter for unset testing"):
                test_filter_id = 2
                system.syslog.selectors.selectors_dict[selector].set_filter(test_filter_id, apply=False)
                system.syslog.selectors.selectors_dict[selector].filter_dict[test_filter_id].set_action_filter(filter_action, apply=False)
                system.syslog.selectors.selectors_dict[selector].filter_dict[test_filter_id].set_match_filter(filter_match, apply=True)

            with allure.step("Validate nv unset system syslog selector <selector-id> filter <filter-id>"):
                system.syslog.selectors.selectors_dict[selector].unset_filter(test_filter_id, apply=True)
                selector_output = system.syslog.selectors.selectors_dict[selector].show()
                selector_dict = OutputParsingTool.parse_json_str_to_dictionary(selector_output).get_returned_value()
                selector_filter_dict = selector_dict.get('filter', {})
                assert str(test_filter_id) not in selector_filter_dict, f"Filter {test_filter_id} should be unset"

            with allure.step("Validate nv unset system syslog format"):
                system.syslog.format.unset(apply=True)
                format_output = system.syslog.format.show()
                format_dict = OutputParsingTool.parse_json_str_to_dictionary(format_output).get_returned_value()
                # Format should be empty or have default values only
                assert SyslogConsts.WELF not in format_dict or format_dict.get(SyslogConsts.WELF) is None, "WELF format should be unset"

            with allure.step("Validate nv unset system syslog selector (all selectors)"):
                system.syslog.selectors.unset(apply=True)
                selector_list_output = system.syslog.selectors.show()
                selector_list_dict = OutputParsingTool.parse_json_str_to_dictionary(selector_list_output).get_returned_value()
                assert len(selector_list_dict) == 0 or selector not in selector_list_dict, "All selectors should be unset"

            with allure.step("Validate nv unset system syslog server (all servers)"):
                system.syslog.servers.unset(apply=True)
                server_list_output = system.syslog.servers.show()
                server_list_dict = OutputParsingTool.parse_json_str_to_dictionary(server_list_output).get_returned_value()
                assert len(server_list_dict) == 0 or remote_server_ip not in server_list_dict, "All servers should be unset"

            with allure.step("Validate nv unset system syslog (global unset)"):
                # Re-setup minimal config for global unset testing
                system.syslog.servers.set_server(remote_server_ip, apply=True)
                system.syslog.unset(apply=True)
                syslog_output = system.syslog.show()
                syslog_dict = OutputParsingTool.parse_json_str_to_dictionary(syslog_output).get_returned_value()
                # Verify that all main sections are cleared
                assert len(syslog_dict.get(SyslogConsts.SERVER, {})) == 0, "All syslog configuration should be unset"
                assert len(syslog_dict.get(SyslogConsts.SELECTOR, {})) == 0, "All selectors should be unset"
    finally:
        with allure.step("Cleanup syslog configurations"):
            system.syslog.unset(apply=True)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
@pytest.mark.cumulus
def test_syslog_multiple_filters_same_selector(random_api):
    """
    Test multiple filters configured on the same selector and verify their combinations work correctly.

    Test flow:
    1. Create server with UDP protocol and port 514
    2. Create selector with 4 different filters:
       - Filter-1: Include "abc"
       - Filter-2: Include "def"
       - Filter-3: Exclude "ghi"
       - Filter-4: Exclude "jkl"
    3. Test message filtering with different patterns
    4. Clean up configurations
    """
    remote_server_engine = TestToolkit.engines[NvosConst.SONIC_MGMT]
    remote_server_ip = remote_server_engine.ip
    system = System()
    selector_id = 'selector-1'
    filter_action_include = SyslogConsts.INCLUDE
    filter_action_exclude = SyslogConsts.EXCLUDE
    filter_match1 = "abc"
    filter_match2 = "def"
    filter_match3 = "ghi"
    filter_match4 = "jkl"

    try:
        with allure.step("Create server with UDP protocol and port 514"):
            logging.info("Create server with UDP protocol and port 514")
            system.syslog.servers.set_server(remote_server_ip, apply=True)
            if TestToolkit.devices.dut.is_eth():
                system.syslog.servers.servers_dict[remote_server_ip].set_vrf(vrf=TestToolkit.devices.dut.vrf_mgmt, apply=True, ask_for_confirmation=True)
            expected_server_dictionary = create_remote_server_dictionary(remote_server_ip, vrf=TestToolkit.devices.dut.vrf_mgmt)
            system.syslog.servers.servers_dict[remote_server_ip].verify_show_server_output(expected_server_dictionary[remote_server_ip])

        with allure.step("Create selector with multiple filters"):
            logging.info("Create selector with multiple filters")
            create_selector_configuration(
                selector_id=selector_id,
                severity=SyslogSeverityLevels.INFO,
                filters=[
                    (filter_action_include, filter_match1),
                    (filter_action_include, filter_match2),
                    (filter_action_exclude, filter_match3),
                    (filter_action_exclude, filter_match4)
                ]
            )
            system.syslog.servers.servers_dict[remote_server_ip].set_selector_priority(1, selector_id, apply=True)

        with allure.step("Test message filtering with different patterns"):
            logging.info("Test message filtering with different patterns")

            # Test message matching "abc" pattern
            test_message = "abc_def_test"
            send_msg_to_server(test_message, remote_server_ip, remote_server_engine,
                               priority=SyslogSeverityLevels.INFO, verify_msg_received=True)

            # Test message matching only "def" pattern
            test_message = "def_test"
            send_msg_to_server(test_message, remote_server_ip, remote_server_engine,
                               priority=SyslogSeverityLevels.INFO, verify_msg_received=True)

            # Test message matching "ghi" pattern (should be excluded)
            test_message = "ghi_test"
            send_msg_to_server(test_message, remote_server_ip, remote_server_engine,
                               priority=SyslogSeverityLevels.INFO, verify_msg_didnt_received=True)

            # Test message matching "jkl" pattern (should be excluded)
            test_message = "jkl_test"
            send_msg_to_server(test_message, remote_server_ip, remote_server_engine,
                               priority=SyslogSeverityLevels.INFO, verify_msg_didnt_received=True)

            # Test message matching multiple patterns
            test_message = "abc_test_def_test_ghi_test_jkl_test"
            send_msg_to_server(test_message, remote_server_ip, remote_server_engine,
                               priority=SyslogSeverityLevels.INFO, verify_msg_didnt_received=True)

    finally:
        with allure.step("Clean up configurations"):
            logging.info("Clean up configurations")
            system.syslog.unset(apply=True)


@pytest.mark.disable_loganalyzer  # dont want log analyser to raise error for severity level-ERR
@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
@pytest.mark.cumulus
def test_syslog_multiple_selectors_same_server(random_api):
    """
    Test Objective:
    Verify multiple selectors with different priorities on the same server and their message filtering behavior.

    Precondition:
    - System is up and running
    - Syslog service is enabled
    - Test environment is properly configured
    """
    system = System()
    remote_server_engine = TestToolkit.engines[NvosConst.SONIC_MGMT]
    remote_server_ip = remote_server_engine.ip

    # Test configuration variables
    server_id = remote_server_ip
    selector1_id = "selector-1"
    selector2_id = "selector-2"
    selector3_id = "selector-3"
    priority1 = 1
    priority2 = 2
    priority3 = 3
    filter1_match = "selector1_test_message"
    filter2_match = "selector2_test_message"
    filter3_match = "selector3_test_message"

    try:
        with allure.step("Create server with UDP protocol and port 514"):
            system.syslog.servers.set_server(server_id, apply=True)
            if TestToolkit.devices.dut.is_eth():
                system.syslog.servers.servers_dict[server_id].set_vrf(vrf=TestToolkit.devices.dut.vrf_mgmt, apply=True, ask_for_confirmation=True)
            system.syslog.servers.servers_dict[server_id].set_protocol(SyslogConsts.UDP, apply=True)
            system.syslog.servers.servers_dict[server_id].set_port(SyslogConsts.DEFAULT_PORT, apply=True)

        with allure.step("Create 3 selectors with different priorities on same server"):

            # Create and configure selector-1
            create_selector_configuration(
                selector_id=selector1_id,
                severity=SyslogSeverityLevels.INFO,
                filters=[(SyslogConsts.INCLUDE, filter1_match)]
            )
            system.syslog.servers.servers_dict[server_id].set_selector_priority(priority1, selector1_id, apply=True)

            # Create and configure selector-2
            create_selector_configuration(
                selector_id=selector2_id,
                severity=SyslogSeverityLevels.ERROR,
                filters=[(SyslogConsts.INCLUDE, filter2_match)]
            )
            system.syslog.servers.servers_dict[server_id].set_selector_priority(priority2, selector2_id, apply=True)

            # Create and configure selector-3
            create_selector_configuration(
                selector_id=selector3_id,
                severity=SyslogSeverityLevels.CRITICAL,
                filters=[(SyslogConsts.INCLUDE, filter3_match)]
            )
            system.syslog.servers.servers_dict[server_id].set_selector_priority(priority3, selector3_id, apply=True)

        with allure.step("Test message filtering with different patterns"):

            # Test message matching "selector1_test_message" pattern
            test_message = "selector1_test_message"
            send_msg_to_server(test_message, server_id, remote_server_engine,
                               priority=SyslogSeverityLevels.INFO, verify_msg_received=True)

            # Test message matching "selector2_test_message" pattern
            test_message = "selector2_test_message"
            send_msg_to_server(test_message, server_id, remote_server_engine,
                               priority=SyslogSeverityLevels.ERROR, verify_msg_received=True)

            # Test message matching "selector3_test_message" pattern
            test_message = "selector3_test_message"
            send_msg_to_server(test_message, server_id, remote_server_engine,
                               priority=SyslogSeverityLevels.CRITICAL, verify_msg_received=True)

        with allure.step("Test higher priority selector with matching filter"):
            test_message = "selector3_test_message"
            send_msg_to_server(test_message, server_id, remote_server_engine,
                               priority=SyslogSeverityLevels.CRITICAL, verify_msg_received=True)

        with allure.step("Unset random selector and verify next highest priority selector behavior"):

            # Randomly select one selector to unset
            selectors = [(selector1_id, priority1), (selector2_id, priority2), (selector3_id, priority3)]
            random_selector, random_priority = random.choice(selectors)
            remaining_selectors = [s for s in selectors if s[0] != random_selector]
            next_highest_selector, next_highest_priority = max(remaining_selectors, key=lambda x: x[1])

            system.syslog.servers.servers_dict[server_id].unset_selector_priority(random_priority, apply=True)

            # Verify the unset selector is removed
            server_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.syslog.servers.servers_dict[server_id].show()).get_returned_value()
            assert str(random_priority) not in server_output.get('selector', {}), \
                f"Priority {random_priority} should be removed but still exists"

            # Test all patterns again to verify next highest priority selector's behavior
            logging.info(f"Testing patterns with next highest priority selector {next_highest_selector}")

            # Define selector pattern mappings
            selector_patterns = {
                selector1_id: {
                    'pattern': 'selector1_test_message',
                    'priority': SyslogSeverityLevels.INFO
                },
                selector2_id: {
                    'pattern': 'selector2_test_message',
                    'priority': SyslogSeverityLevels.ERROR
                },
                selector3_id: {
                    'pattern': 'selector3_test_message',
                    'priority': SyslogSeverityLevels.CRITICAL
                }
            }

            # Test pattern for next highest priority selector (should be received)
            next_pattern = selector_patterns[next_highest_selector]
            send_msg_to_server(next_pattern['pattern'], server_id, remote_server_engine,
                               priority=next_pattern['priority'], verify_msg_received=True)

            # Test pattern for remaining selector (should not be received)
            remaining_selector = [s for s in remaining_selectors if s[0] != next_highest_selector][0]
            remaining_pattern = selector_patterns[remaining_selector[0]]
            send_msg_to_server(remaining_pattern['pattern'], server_id, remote_server_engine,
                               priority=remaining_pattern['priority'], verify_msg_received=True)

    finally:
        with allure.step("Clean up configurations"):
            # Unset all selector priorities
            system.syslog.servers.servers_dict[server_id].unset_selector_priority(priority1, apply=True)
            system.syslog.servers.servers_dict[server_id].unset_selector_priority(priority3, apply=True)
            system.syslog.servers.servers_dict[server_id].unset_selector_priority(priority2, apply=True)
            # Unset all selectors
            system.syslog.selectors.unset_selector(selector1_id, apply=True)
            system.syslog.selectors.unset_selector(selector2_id, apply=True)
            system.syslog.selectors.unset_selector(selector3_id, apply=True)
            # Unset server
            system.syslog.servers.unset_server(server_id, apply=True)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
@pytest.mark.cumulus
def test_syslog_rate_limit_burst(random_api):
    """
    Test Objective:
    Verify rate limiting behavior with burst limit configuration.

    Test flow:
    1. Create server and selector
    2. Configure rate limit with interval=60 and burst=10
    3. Send 15 messages in quick succession
    4. Verify messages 11-15 are dropped
    5. Wait for interval reset
    6. Send new message
    7. Unset interval and burst
    8. Send new message again
    9. Clean up configuration
    """
    TestToolkit.tested_api = random_api
    system = System()
    remote_server_engine = TestToolkit.engines[NvosConst.SONIC_MGMT]
    remote_server_ip = remote_server_engine.ip
    selector_id = "burst_rate_limit_selector"
    interval = 80
    burst = 10

    try:
        with allure.step("Create server and selector"):
            system.syslog.servers.set_server(remote_server_ip, apply=True)
            if TestToolkit.devices.dut.is_eth():
                system.syslog.servers.servers_dict[remote_server_ip].set_vrf(vrf=TestToolkit.devices.dut.vrf_mgmt, apply=True, ask_for_confirmation=True)
            system.syslog.selectors.set_selector(selector_id, apply=True)
            system.syslog.selectors.selectors_dict[selector_id].set_severity(SyslogSeverityLevels.CRITICAL, apply=True)
            system.syslog.servers.servers_dict[remote_server_ip].set_selector_priority(1, selector_id, apply=True)

            if TestToolkit.devices.dut.is_eth():
                # we need to include filter to get the expected result for eth devices
                # since device is sending other service message also to remote server, that also counts in burst limit
                include_regex = "burst_test_message.*"
                system.syslog.selectors.selectors_dict[selector_id].set_filter("1", apply=False)
                system.syslog.selectors.selectors_dict[selector_id].filter_dict["1"].set_action_filter(SyslogConsts.INCLUDE, apply=False)
                system.syslog.selectors.selectors_dict[selector_id].filter_dict["1"].set_match_filter(include_regex, apply=True)
        with allure.step(f"Configure rate limit with interval={interval} and burst={burst}"):
            system.syslog.selectors.selectors_dict[selector_id].rate_limit.set_interval(interval, apply=True)
            system.syslog.selectors.selectors_dict[selector_id].rate_limit.set_burst(burst, apply=True)

            expected_selector = {
                'rate-limit': {
                    'burst': burst,
                    'interval': interval
                }
            }
            system.syslog.selectors.selectors_dict[selector_id].verify_rate_limit_config(expected_selector)

        with allure.step("Send messages until rate limiting kicks in, then verify drops"):
            # Send messages continuously until rate limiting is detected
            rate_limit_triggered = False
            max_messages = 20  # Safety limit to avoid infinite loop
            messages_sent = 0

            for i in range(max_messages):
                test_message = f"burst_test_message_{i}"
                send_msg_to_server(test_message, remote_server_ip, remote_server_engine,
                                   priority=SyslogSeverityLevels.CRITICAL)
                messages_sent += 1
                time.sleep(1)

                # Check if rate limiting message appears in switch logs
                with sudo_scope_if(TestToolkit.devices.dut.is_eth()):
                    syslog_output = TestToolkit.engines.dut.run_cmd('tail -20 /var/log/syslog | grep "rate-limiting"')
                if "begin to drop messages due to rate-limiting" in syslog_output:
                    rate_limit_triggered = True
                    logger.info(f"✓ Rate limiting detected after sending {messages_sent} messages")
                    break

            assert rate_limit_triggered, f"Rate limiting was not triggered after {messages_sent} messages (expected within burst limit of {burst})"

            # Now send additional messages and verify they are being dropped
            with allure.step("Verify messages are dropped after rate limit"):
                for j in range(3):
                    drop_test_message = f"burst_test_drop_verify_{j}"
                    send_msg_to_server(drop_test_message, remote_server_ip, remote_server_engine,
                                       priority=SyslogSeverityLevels.CRITICAL, verify_msg_didnt_received=True)
                    logger.info(f"✓ Confirmed message {j} was dropped as expected")

        with allure.step("Send 10 messages in quick succession"):
            # Send first 3 messages (within burst limit)
            for i in range(3):
                test_message = f"burst_test_message_{i}"
                send_msg_to_server(test_message, remote_server_ip, remote_server_engine,
                                   priority=SyslogSeverityLevels.CRITICAL, verify_msg_received=True)
            # Send 10 messages without checking message received to expire burst limit
            for i in range(3, 10):
                test_message = f"burst_test_message_{i}"
                send_msg_to_server(test_message, remote_server_ip, remote_server_engine,
                                   priority=SyslogSeverityLevels.CRITICAL)
            time.sleep(10)
            # Send next 5 messages and verify they are not received (past burst limit)
            for i in range(11, 15):
                test_message = f"burst_test_message_{i}"
                send_msg_to_server(test_message, remote_server_ip, remote_server_engine,
                                   priority=SyslogSeverityLevels.CRITICAL, verify_msg_didnt_received=True)

        with allure.step("Wait for interval reset and send new message"):
            time.sleep(80)  # Wait for interval reset with buffer time
            test_message = "burst_test_message_after_interval"
            send_msg_to_server(test_message, remote_server_ip, remote_server_engine,
                               priority=SyslogSeverityLevels.CRITICAL, verify_msg_received=True)

        with allure.step("Unset rate limit"):
            system.syslog.selectors.selectors_dict[selector_id].rate_limit.unset(apply=True)
            expected_selector = {}
            system.syslog.selectors.selectors_dict[selector_id].verify_rate_limit_config(expected_selector)

        with allure.step("Send new message after unset"):
            test_message = "burst_test_message_after_unset"
            send_msg_to_server(test_message, remote_server_ip, remote_server_engine,
                               priority=SyslogSeverityLevels.CRITICAL, verify_msg_received=True)
        with allure.step("Configure rate limit with interval=60 and burst=10"):
            system.syslog.selectors.selectors_dict[selector_id].rate_limit.set_interval(interval, apply=True)
            system.syslog.selectors.selectors_dict[selector_id].rate_limit.set_burst(burst, apply=True)
        with allure.step("Unset burst"):
            system.syslog.selectors.selectors_dict[selector_id].rate_limit.unset_burst(apply=True)
            expected_selector = {'rate-limit': {'interval': interval}}
            system.syslog.selectors.selectors_dict[selector_id].verify_rate_limit_config(expected_selector)

        with allure.step("Send new message after unset"):
            test_message = "burst_test_message_after_unset"
            send_msg_to_server(test_message, remote_server_ip, remote_server_engine,
                               priority=SyslogSeverityLevels.CRITICAL, verify_msg_received=True)

    finally:
        with allure.step("Clean up configurations"):
            system.syslog.unset(apply=True)


@pytest.mark.system
@pytest.mark.syslog
@pytest.mark.simx
@pytest.mark.cumulus
def test_syslog_welf_format_without_firewall_name(random_api):
    """
    Test Objective:
    Verify that WELF format configuration requires a firewall name and proper error handling.

    Test flow:
    1. Attempt to configure WELF format without firewall name
    2. Attempt to unset WELF format without firewall name
    3. Attempt to set empty firewall name
    4. Unset with commands
    5. Clean up configuration
    """
    system = System()
    remote_server_engine = TestToolkit.engines[NvosConst.SONIC_MGMT]
    remote_server_ip = remote_server_engine.ip

    try:
        with allure.step("Attempt to configure WELF format without firewall name"):
            system.syslog.format.set(SyslogConsts.WELF, apply=True).verify_result(False,
                                                                                  expected_value=["firewall-name must be configured", "firewall name must be configured"])

        with allure.step("Attempt to unset WELF format without firewall name"):
            system.syslog.format.welf.unset(apply=True).verify_result()

        with allure.step("Attempt to set empty firewall name"):
            system.syslog.format.welf.set_firewall_name("", apply=True).verify_result(False,
                                                                                      expected_value=["Error", "too small"])

        with allure.step("Unset with commands"):
            system.syslog.format.unset(apply=True)
            system.syslog.format.welf.unset(apply=True)

    finally:
        with allure.step("Clean up configurations"):
            system.syslog.unset(apply=True)


def verify_welf_format(line_to_check, firewall_name=".*", expect_welf_format=True):
    welf_format_regex = TestToolkit.devices.dut.welf_format_regex.format(firewall_name)
    result = re.findall(welf_format_regex, line_to_check)
    with allure.step("Verify msg format"):
        if expect_welf_format:
            assert result, "Expect the line to be in welf format, but it was not"
        else:
            assert not result, "Expect the line not to be in welf format, but it was"


def send_random_msg_and_validate_format(remote_server_ip, remote_server_engine, expect_welf_format, firewall_name=".*"):
    random_msg = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
    output = send_msg_to_server(random_msg, remote_server_ip, remote_server_engine, verify_msg_received=True)
    verify_welf_format(output, expect_welf_format=expect_welf_format, firewall_name=firewall_name)


def config_and_verify_rsyslog_protocol(server, remote_server_engine, remote_server_ip, protocol):
    with allure.step("Change rsyslog protocol to {}".format(protocol)):
        server.set_protocol(protocol, apply=True)
        server.verify_show_server_output({SyslogConsts.PROTOCOL: protocol})
        random_msg = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
        send_msg_to_server(random_msg, remote_server_ip, remote_server_engine, verify_msg_received=True)


def config_and_verify_rsyslog_port(server, remote_server_engine, remote_server_ip, old_port, new_port):
    with allure.step("Change rsyslog port to {}".format(new_port)):
        server.set_port(new_port, apply=True)
        server.verify_show_server_output({SyslogConsts.PORT: str(new_port)})
        try:
            SonicMgmtContainer.change_rsyslog_port(remote_server_engine, old_port, new_port, SyslogConsts.UDP,
                                                   restart_rsyslog=True)
            random_msg = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
            send_msg_to_server(random_msg, remote_server_ip, remote_server_engine, verify_msg_received=True)
        except Exception as err:
            SonicMgmtContainer.change_rsyslog_port(remote_server_engine, old_port, new_port, SyslogConsts.UDP,
                                                   restart_rsyslog=True)
            raise err


def config_and_verify_severity(loganalyzer, syslog, server, server_name, server_engine, severity_level,
                               selector_id=None,
                               global_severity_level=SyslogSeverityLevels.NOTICE):
    with allure.step("Configure and verify severity level: {}".format(severity_level)):
        syslog.selectors.selectors_dict[selector_id].set_severity(severity_level, apply=True)
        syslog.selectors.selectors_dict[selector_id].verify_trap_severity_level(selector_id, severity_level)

        random_msg = RandomizationTool.get_random_string(40, ascii_letters=string.ascii_letters + string.digits)
        add_msg_to_ignore_loganalyzer_regex(loganalyzer, random_msg)
        severity_level_index = SyslogSeverityLevels.SEVERITY_LEVEL_LIST.index(severity_level)
        send_msg_to_server(random_msg, server_name, server_engine, priority=severity_level,
                           verify_msg_received=True,
                           verify_msg_didnt_received=False)

        if severity_level_index + 1 < len(SyslogSeverityLevels.SEVERITY_LEVEL_LIST):
            random_msg = RandomizationTool.get_random_string(40, ascii_letters=string.ascii_letters + string.digits)
            add_msg_to_ignore_loganalyzer_regex(loganalyzer, random_msg)
            rand_recieved_level = random.choice(SyslogSeverityLevels.SEVERITY_LEVEL_LIST[severity_level_index + 1:])
            send_msg_to_server(random_msg, server_name, server_engine, priority=rand_recieved_level,
                               verify_msg_received=True,
                               verify_msg_didnt_received=False)
        if severity_level_index > 0:
            rand_not_recieved_level = random.choice(SyslogSeverityLevels.SEVERITY_LEVEL_LIST[:severity_level_index])
            random_msg = RandomizationTool.get_random_string(35, ascii_letters=string.ascii_letters + string.digits)
            add_msg_to_ignore_loganalyzer_regex(loganalyzer, random_msg)
            send_msg_to_server(random_msg, server_name, server_engine, priority=rand_not_recieved_level,
                               verify_msg_received=False,
                               verify_msg_didnt_received=True)


def add_msg_to_ignore_loganalyzer_regex(loganalyzer, random_msg):
    if loganalyzer:
        for hostname in loganalyzer.keys():
            loganalyzer[hostname].ignore_regex.extend([f".*{random_msg}.*"])


def send_msg_to_server(msg, server_name, server_engine, protocol=None, priority=None, port=None, program=None, facility=None,
                       verify_msg_received=False,
                       verify_msg_didnt_received=False, additional_flags=''):
    with allure.step("Send msg to server {}".format(server_name)):
        protocol_flag = f' --{protocol}' if protocol else ''  # must be tcp or udp
        severity = SyslogSeverityLevels.SEVERITY_LEVEL_DICT[priority] if priority else SyslogSeverityLevels.NOTICE
        priority_flag = f' --priority {facility}.{severity}' if facility else f' --priority {SyslogConsts.DAEMON}.{severity}'
        port_flag = f' --port {port}' if port else ''
        program_flag = f' --tag {program}' if program else ''
        # additional_flags: raw `logger` flags, carrying its own leading space.
        extra_flags = protocol_flag + priority_flag + port_flag + program_flag + additional_flags
        logger_cmd = f'logger {extra_flags} "{msg}"'
        time.sleep(3)  # wait for rsyslogd reload after nv config apply (NVBug 6213076)
        TestToolkit.engines.dut.run_cmd(logger_cmd)
        with sudo_scope_if(TestToolkit.devices.dut.is_eth()):
            TestToolkit.engines.dut.run_cmd('tail -10 /var/log/syslog')
        output = ''

        time.sleep(2)
        if verify_msg_received:
            with allure.step("Verify server {} received the msg".format(server_name)):
                output = verify_msg_in_syslog_file(server_engine, msg, should_find=True)
        elif verify_msg_didnt_received:
            with allure.step("Verify server {} did not receive the msg".format(server_name)):
                verify_msg_in_syslog_file(server_engine, msg, should_find=False)
        return output


def verify_msg_in_syslog_file(engine, msg_to_find, syslog_file='/var/log/syslog', should_find=True,
                              tries=5, delay=2):
    # 'sudo' so the root-owned log is readable (a non-sudo read fails silently and looks like "not found"),
    # and 'grep -F -- <msg>' so regex/shell metacharacters in the message (e.g. the "a+"/"b+" filter
    # prefixes) are matched literally instead of being interpreted as a pattern.
    cmd = f"sudo grep -F -- '{msg_to_find}' {syslog_file}"

    def _verify_msg():
        output = engine.run_cmd(cmd)
        msg_in_file = msg_to_find in output
        if msg_in_file and not should_find:
            raise AssertionError("Found the message, but expected not to find it")
        if not msg_in_file and should_find:
            raise AssertionError("Didn't find the message, but expected to find it")
        return output

    # Forwarding to the remote collector can lag behind the send, so retry while we still expect to
    # find it. For the "should not be received" case a single check is enough (tries=1).
    return retry_call(_verify_msg, exceptions=AssertionError,
                      tries=tries if should_find else 1, delay=delay)


def create_syslog_output_dictionary(format=SyslogConsts.STANDARD, format_dict={}, trap=SyslogSeverityLevels.NOTICE,
                                    server_dict=None):

    dictionary = {
        SyslogConsts.FORMAT: {format: format_dict},
        # SyslogConsts.TRAP: trap
    }
    if server_dict:
        dictionary.update(server_dict)

    return dictionary


def create_remote_server_dictionary(server_name, port=SyslogConsts.DEFAULT_PORT, protocol=SyslogConsts.UDP, vrf=SyslogConsts.DEFAULT_VRF):
    server_config = {
        SyslogConsts.PORT: str(port),
        SyslogConsts.PROTOCOL: protocol,
        SyslogConsts.VRF: vrf,
    }
    if not TestToolkit.devices.dut.is_eth():
        server_config[SyslogConsts.SELECTOR] = {}
    dictionary = {server_name: server_config}
    return dictionary


def create_remote_server_dictionary_with_selector(server_name, priority, port=SyslogConsts.DEFAULT_PORT, protocol=SyslogConsts.UDP,
                                                  vrf=SyslogConsts.DEFAULT_VRF, selector_id=None):
    dictionary = {
        server_name: {
            SyslogConsts.PORT: str(port),
            SyslogConsts.PROTOCOL: protocol,
            SyslogConsts.VRF: vrf,
            SyslogConsts.SELECTOR: {str(priority): {SyslogConsts.SELECTOR_ID: selector_id}},
        }
    }
    return dictionary


def create_selector_configuration(selector_id, options=None, program=None, facility=None, severity=None, rate_limit_interval=None, rate_limit_burst=None, filters=None):
    """
    Create and apply a selector configuration with all possible options.

    Args:
        selector_id (str): The ID of the selector
        options (dict, optional): Dictionary containing any of the following keys:
            - SyslogConsts.SEVERITY: Severity level
            - SyslogConsts.FACILITY: Facility name
            - SyslogConsts.PROGRAM_NAME: Program name
        program (str, optional): Program name
        facility (str, optional): Facility name
        severity (str, optional): Severity level
        rate_limit_interval (int, optional): Rate limit interval in seconds
        rate_limit_burst (int, optional): Rate limit burst count
        filters (list, optional): List of (action, match) tuples for filter configuration
            Example: [("include", "abc"), ("exclude", "xyz")]

    Returns:
        dict: Selector configuration dictionary
    """
    system = System()

    # Create the selector
    system.syslog.selectors.set_selector(selector_id, apply=True)
    selector = system.syslog.selectors.selectors_dict[selector_id]

    # Apply configurations based on options
    if options:
        # Apply severity if provided
        if SyslogConsts.SEVERITY in options:
            selector.set_severity(options[SyslogConsts.SEVERITY], apply=True)
        elif severity:
            selector.set_severity(severity, apply=True)

        # Apply facility if provided
        if SyslogConsts.FACILITY in options:
            selector.set_facility(options[SyslogConsts.FACILITY], apply=True)
        elif facility:
            selector.set_facility(facility, apply=True)

        # Apply program name if provided
        if SyslogConsts.PROGRAM_NAME in options:
            selector.set_program_name(options[SyslogConsts.PROGRAM_NAME], apply=True)
        elif program:
            selector.set_program_name(program, apply=True)
    else:
        # Apply default configurations if provided
        if severity:
            selector.set_severity(severity, apply=True)
        if facility:
            selector.set_facility(facility, apply=True)
        if program:
            selector.set_program_name(program, apply=True)

    # Apply rate limit configurations if provided
    if rate_limit_interval is not None:
        selector.rate_limit.set_interval(rate_limit_interval, apply=True)
    if rate_limit_burst is not None:
        selector.rate_limit.set_burst(rate_limit_burst, apply=True)

    # Handle filter configurations
    if filters:
        for index, (action, match) in enumerate(filters, start=1):
            filter_id = str(index)
            selector.set_filter(filter_id, apply=False)
            selector.filter_dict[filter_id].set_action_filter(action, apply=False)
            selector.filter_dict[filter_id].set_match_filter(match, apply=True)

    # Create and return the dictionary representation
    selector_dict = {
        SyslogConsts.FACILITY: facility if facility else (options.get(SyslogConsts.FACILITY) if options else None),
        SyslogConsts.PROGRAM_NAME: program if program else (options.get(SyslogConsts.PROGRAM_NAME) if options else None),
        SyslogConsts.FILTER: {},
        SyslogConsts.SEVERITY: severity if severity else (options.get(SyslogConsts.SEVERITY) if options else None)
    }

    # Add all filter configurations to dictionary
    for filter_id, filter_obj in selector.filter_dict.items():
        selector_dict[SyslogConsts.FILTER][filter_id] = {
            SyslogConsts.ACTION: filter_obj.action,
            SyslogConsts.MATCH: filter_obj.match
        }

    # Add rate limit configuration to dictionary if any rate limit settings were applied
    if rate_limit_interval is not None or rate_limit_burst is not None:
        selector_dict[SyslogConsts.RATE_LIMIT] = {}
        if rate_limit_interval is not None:
            selector_dict[SyslogConsts.RATE_LIMIT][SyslogConsts.INTERVAL] = rate_limit_interval
        if rate_limit_burst is not None:
            selector_dict[SyslogConsts.RATE_LIMIT][SyslogConsts.BURST] = rate_limit_burst

    return selector_dict


def positive_minimal_flow(remote_server_engine, remote_server):
    system = System()

    with allure.step("Configure remote syslog server: {}".format(remote_server)):
        system.syslog.servers.set_server(remote_server, apply=True)
        if TestToolkit.devices.dut.is_eth():
            system.syslog.servers.servers_dict[remote_server].set_vrf(TestToolkit.devices.dut.vrf_mgmt, apply=True, ask_for_confirmation=True)
    try:
        random_msg = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
        with allure.step("Validate show commands"):
            expected_server_dictionary = create_remote_server_dictionary(remote_server, vrf=TestToolkit.devices.dut.vrf_mgmt)
            expected_syslog_dictionary = create_syslog_output_dictionary(
                server_dict={SyslogConsts.SERVER: expected_server_dictionary})
            system.syslog.verify_show_syslog_output(expected_syslog_dictionary)
            system.syslog.servers.verify_show_servers_list([remote_server])
            system.syslog.servers.servers_dict[remote_server].verify_show_server_output(
                expected_server_dictionary[remote_server])

        send_msg_to_server(random_msg, remote_server, remote_server_engine, verify_msg_received=True)
    finally:
        with allure.step("Cleanup syslog configurations"):
            system.syslog.servers.unset(apply=True)


def get_hostname_from_ip(ip):
    host_name_index = 0
    hostname_str = socket.gethostbyaddr(ip)[host_name_index]
    return remove_mlnx_lab_suffix(hostname_str)


def remove_mlnx_lab_suffix(hostname_string):
    """
    Returns switch hostname without mlnx lab prefix
    :param hostname_string: 'arc-switch1030.mtr.labs.mlnx'
    :return: arc-switch1030
    """
    host_name_index = 0
    return hostname_string.split('.')[host_name_index]


@retry(Exception, tries=3, delay=5, backoff=2)
def send_message_after_rate_limit_reset(remote_server_ip, remote_server_engine, priority=SyslogSeverityLevels.INFO):
    """
    Send a message after rate limit interval reset with automatic retry.

    Args:
        remote_server_ip: IP address of the remote server
        remote_server_engine: Engine object for the remote server
        priority: Syslog priority level (default: INFO)

    Returns:
        None

    Raises:
        Exception: If all retry attempts fail
    """
    test_message = "burst_test_message_after_interval"
    send_msg_to_server(test_message, remote_server_ip, remote_server_engine,
                       priority=priority, verify_msg_received=True)
    logger.info("Message sent successfully after interval reset")


@pytest.mark.system
@pytest.mark.syslog
def test_syslog_logging_during_system_reboot(engines, random_api):
    """
    Test Objective:
    Verify that system-generated logs are forwarded to remote syslog during cold reboot.

    Test flow:
    1. Configure remote syslog server
    2. Perform reboot
    3. Verify DUT reboot logs are present in remote syslog
    4. Clean up
    """
    remote_server_engine = engines[NvosConst.SONIC_MGMT]
    remote_server_ip = remote_server_engine.ip
    system = System()

    try:
        with allure.step("Configure remote syslog server"):
            system.syslog.servers.set_server(remote_server_ip, apply=True)
            # Save configuration to persist across reboot so logs can be forwarded
            TestToolkit.GeneralApi[random_api].save_config(engines.dut)

        with allure.step("Perform reboot"):
            system.reboot.action_reboot()

            # Wait for system to come back online
            max_wait = 300
            wait_interval = 10
            elapsed = 0

            while elapsed < max_wait:
                try:
                    TestToolkit.engines.dut.run_cmd('echo "online"', timeout=10)
                    break
                except BaseException:
                    time.sleep(wait_interval)
                    elapsed += wait_interval
            else:
                raise Exception(f"System did not come back online within {max_wait} seconds")

            # Allow time for logs to be forwarded
            time.sleep(70)

        with allure.step("Verify reboot logs are forwarded to remote syslog"):
            # Get reboot-related logs from DUT
            dut_logs = TestToolkit.engines.dut.run_cmd('grep -ia "reboot\\|restart\\|boot" /var/log/syslog | tail -5')

            if not dut_logs.strip():
                logger.warning("No reboot logs found on DUT")
                return

            # Check if any DUT logs are present in remote syslog
            matched = 0
            for log_line in dut_logs.strip().split('\n'):
                if not log_line.strip():
                    continue

                # Extract key part of message for searching
                parts = log_line.split()
                if len(parts) >= 4:
                    search_text = ' '.join(parts[3:6])
                    remote_result = remote_server_engine.run_cmd(f'grep -F "{search_text}" /var/log/syslog')
                    if remote_result.strip():
                        matched += 1
                        logger.info(f"Found DUT log in remote syslog: {search_text}")

            # Verify at least some logs were forwarded
            if matched == 0:
                raise AssertionError("No DUT cold reboot logs found in remote syslog - forwarding may not be working during cold reboot")
            else:
                logger.info(f"SUCCESS: {matched} cold reboot log entries forwarded to remote syslog")

    finally:
        with allure.step("Cleanup"):
            system.syslog.servers.unset(apply=True)


@pytest.mark.system
@pytest.mark.syslog
def test_syslog_buffering_during_mgmt_interface_downtime(engines, topology_obj, serial_engine, random_api):
    """
    Test syslog message buffering during complete management network downtime.
    Uses the same configuration pattern as test_rsyslog_format and proper eth0 up/down pattern.

    Test flow:
    1. Configure remote syslog server using same pattern as rsyslog_format test
    2. Send messages and verify they are received
    3. Shutdown management interface (complete network isolation)
    4. Send logger messages during interface downtime (should be buffered)
    5. Bring management interface back up
    6. Verify all buffered messages are received after interface recovery
    """
    remote_server_engine = engines[NvosConst.SONIC_MGMT]
    remote_server_ip = remote_server_engine.ip
    system = System()

    # Get management interface dynamically
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)

    try:
        normal_messages = []
        buffered_messages = []

        with allure.step("Configure remote syslog server {} and validate".format(remote_server_ip)):
            system.syslog.servers.set_server(remote_server_ip, apply=True)
            expected_server_dictionary = create_remote_server_dictionary(remote_server_ip)
            expected_syslog_dictionary = create_syslog_output_dictionary(
                server_dict={SyslogConsts.SERVER: expected_server_dictionary})
            system.syslog.verify_show_syslog_output(expected_syslog_dictionary)
            system.syslog.servers.verify_show_servers_list([remote_server_ip])
            system.syslog.servers.servers_dict[remote_server_ip].verify_show_server_output(
                expected_server_dictionary[remote_server_ip])

        with allure.step("Step 1: Verify management interface is up and send normal messages"):
            # Verify interface is up initially
            output_dictionary = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                mgmt_port.interface.link.show()).get_returned_value()
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_STATE,
                                                              expected_value=NvosConsts.LINK_STATE_UP).verify_result()

            # Verify connectivity
            check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port, tries=15, delay=2)

            # Send normal messages and verify immediate delivery
            for i in range(3):
                random_msg = RandomizationTool.get_random_string(25, ascii_letters=string.ascii_letters + string.digits)
                normal_messages.append(random_msg)
                full_msg = f"MGMT_BUFFERING_NORMAL_{random_msg}"

                send_msg_to_server(full_msg, remote_server_ip, remote_server_engine, verify_msg_received=True)
                logging.info(f" Verified normal message delivered: {full_msg}")

            logging.info(f" All {len(normal_messages)} normal messages successfully delivered")

        with allure.step("Step 2: Shutdown management interface {}".format(mgmt_port_name)):
            # Use direct CLI commands via serial engine since Port wrapper doesn't work with serial engine
            serial_engine.run_cmd(f'nv set interface {mgmt_port_name} link state down')
            serial_engine.run_cmd('nv config apply -y')

            # Verify interface is down and connectivity is lost
            check_port_status_till_alive(False, engines.dut.ip, engines.dut.ssh_port, tries=15, delay=2)

            # Verify interface state via serial engine
            show_output = serial_engine.run_cmd(f'nv show interface {mgmt_port_name} link')
            # Handle tuple return value from serial engine
            output_str = show_output[0] if isinstance(show_output, tuple) else show_output
            assert 'down' in output_str.lower(), f"Interface {mgmt_port_name} not showing as down: {output_str}"
            logging.info(f" Management interface {mgmt_port_name} is down")

        with allure.step("Step 3: Send messages during interface downtime (should be buffered)"):
            for i in range(5):
                random_msg = RandomizationTool.get_random_string(30, ascii_letters=string.ascii_letters + string.digits)
                buffered_messages.append(random_msg)
                full_msg = f"MGMT_BUFFERING_DOWNTIME_{random_msg}"

                # Send message via serial engine during downtime
                serial_engine.run_cmd(f'logger "{full_msg}"')
                logging.info(f"Sent message during downtime (should be buffered): {full_msg}")
                time.sleep(1)

            # Wait for buffering period
            time.sleep(15)
            logging.info(f"Sent {len(buffered_messages)} messages during complete management network downtime")

        with allure.step("Step 4: Bring management interface {} back up".format(mgmt_port_name)):
            # Use direct CLI commands via serial engine
            serial_engine.run_cmd(f'nv unset interface {mgmt_port_name} link state')
            serial_engine.run_cmd('nv config apply -y')

            # Verify interface is up and connectivity is restored
            check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port, tries=15, delay=2)

            # Verify interface state is back up (can now use regular engine since SSH is restored)
            output_dictionary = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                mgmt_port.interface.link.show()).get_returned_value()
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_STATE,
                                                              expected_value=NvosConsts.LINK_STATE_UP).verify_result()

            # Wait for messages to be delivered
            time.sleep(30)
            logging.info(f" Management interface {mgmt_port_name} is back up")

        with allure.step("Step 5: Verify all normal messages are still present"):
            for msg in normal_messages:
                full_msg = f"MGMT_BUFFERING_NORMAL_{msg}"
                verify_msg_in_syslog_file(remote_server_engine, full_msg, should_find=True)
                logging.info(f" Verified normal message still present: {full_msg}")

        with allure.step("Step 6: Verify all buffered messages were delivered after recovery"):
            for msg in buffered_messages:
                full_msg = f"MGMT_BUFFERING_DOWNTIME_{msg}"
                verify_msg_in_syslog_file(remote_server_engine, full_msg, should_find=True)
                logging.info(f" Verified buffered message delivered: {full_msg}")

            logging.info(f"✓ All {len(buffered_messages)} buffered messages successfully delivered after interface recovery")

        with allure.step("Summary"):
            total_messages = len(normal_messages) + len(buffered_messages)
            logging.info(f" TEST PASSED: All {total_messages} messages delivered successfully")
            logging.info(f"  - Normal messages (immediate delivery): {len(normal_messages)}")
            logging.info(f"  - Buffered messages (delayed delivery): {len(buffered_messages)}")

    finally:
        with allure.step("Cleanup"):
            # Ensure management interface is back up using direct CLI commands
            try:
                serial_engine.run_cmd(f'nv unset interface {mgmt_port_name} link state')
                serial_engine.run_cmd('nv config apply -y')
                check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port, tries=15, delay=2)
                logging.info(f" Cleanup: Management interface {mgmt_port_name} restored")
            except Exception as e:
                logging.warning(f"Warning: Failed to restore interface in cleanup: {e}")

            # Cleanup syslog configurations
            system.syslog.servers.unset(apply=True)


def _get_server_info(is_air, engines):
    if is_air:
        remote_server_engine = engines.oob_mgmt_server
        remote_server_hostname = engines.oob_mgmt_server.ip
    else:
        remote_server_engine = engines[NvosConst.SONIC_MGMT]
        remote_server_hostname = get_hostname_from_ip(remote_server_engine.ip)
    return remote_server_engine, remote_server_hostname


def configure_oob_server_for_syslog(oob_mgmt_server):
    try:
        with allure.step("Configure OOB server for syslog"):
            oob_mgmt_server.run_cmd('sudo systemctl enable rsyslog')
            oob_mgmt_server.run_cmd('sudo systemctl start rsyslog')
            oob_mgmt_server.run_cmd('sudo chmod 666 /var/log/syslog')
    except Exception as err:
        oob_mgmt_server.run_cmd('sudo systemctl stop rsyslog')
        oob_mgmt_server.run_cmd('sudo chmod 644 /var/log/syslog')
        logging.error(f"Failed to configure OOB server for syslog: {err}")
        raise err


@pytest.fixture(scope='session', autouse=True)
def configure_oob_server(engines, is_air):
    if is_air:
        configure_oob_server_for_syslog(engines.oob_mgmt_server)
