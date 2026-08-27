import logging
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest
import requests
from netmiko import ConnectHandler, NetmikoTimeoutException, ReadTimeout
from paramiko.ssh_exception import AuthenticationException
from retry.api import retry_call, retry

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.nvos_constants.constants_nvos import SystemConsts, HealthConsts, DatabaseConst, NvosConst, RebootConsts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime, SubDuration
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.DatabaseTool import DatabaseTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tests_nvos.general.post_upgrade_switch.constants import InstallSteps
from ngts.tests_nvos.general.post_upgrade_switch.install_steps_timer import InstallStepsTimer
from ngts.tools.test_utils import allure_utils as allure
from .ResultObj import ResultObj, IssueType
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active

logger = logging.getLogger(__name__)


@dataclass
class RebootParams:
    recovery_engine: Any = None
    topology_obj: Any = None
    wait_time_before_reboot: int = RebootConsts.WAIT_TIME_BEFORE_REBOOT
    system_is_ready_timeout: int = None
    track_boot_intervals: bool = False
    should_wait_till_system_ready: bool = True
    check_system_is_functional: bool = True
    throw_exception_on_unhealthy: bool = True


class DutUtilsTool:
    invalid_output_list = ['aborted', 'aborting', 'error']

    @staticmethod
    def reload(engine, device, command, confirm=False, reboot_params: Optional[RebootParams] = None):
        reboot_params = reboot_params or RebootParams()
        with allure.step(f'Run command "{command}" and wait for reboot to finish'):
            list_commands = [command, 'y'] if confirm else [command]
            op_start = time.perf_counter()
            output = device.reload_device(engine, list_commands)
            op_duration = time.perf_counter() - op_start
            OperationTime.record_sub_duration(SubDuration.OPERATION, op_duration)
            logger.info(output)
            output = output.replace('-bash: y: command not found', '')

            output_lower = output.lower()
            if ('action succeeded' in output_lower) and ('reboot skipped' in output_lower):
                res_obj = ResultObj(result=True, info=output, returned_value=output)
                res_obj.duration = {SubDuration.OPERATION: op_duration, SubDuration.REBOOT: 0.0}
                OperationTime.record_sub_duration(SubDuration.REBOOT, 0.0)
                return res_obj

            error_list = ['aborted', 'aborting', 'error: action failed', 'command not found']
            for error in error_list:
                if error in output_lower:
                    res_obj = ResultObj(result=False, info=output, returned_value=output)
                    res_obj.duration = {SubDuration.OPERATION: op_duration, SubDuration.REBOOT: 0.0}
                    OperationTime.record_sub_duration(SubDuration.REBOOT, 0.0)
                    return res_obj

            reboot_start = time.perf_counter()
            res_obj = DutUtilsTool.wait_on_system_reboot(engine, reboot_params, device,
                                                         verify_final_result=False, wait_for_nvos=True)
            reboot_duration = time.perf_counter() - reboot_start
            OperationTime.record_sub_duration(SubDuration.REBOOT, reboot_duration)
            if not reboot_params.should_wait_till_system_ready:
                time.sleep(40)
                res_obj.duration = {SubDuration.OPERATION: op_duration, SubDuration.REBOOT: reboot_duration}
                return res_obj

        res_obj.returned_value = output
        res_obj.duration = {SubDuration.OPERATION: op_duration, SubDuration.REBOOT: reboot_duration}
        return res_obj

    @staticmethod
    def check_ssh_for_authentication_error(engine, device):
        try:
            retry_call(engine.run_cmd, fargs=[''], tries=2, delay=3, logger=logger)
        except AuthenticationException as e:
            if engine.password == device.default_password:
                engine.password = NvosConst.OLD_PASS
            else:
                engine.password = device.default_password

    @staticmethod
    def reconnect_engine(engine, find_prompt_tries=5, find_prompt_delay=2):
        """
        Reconnect a disconnected engine by retrying run_cmd('') until the session is established.
        Use after disconnect() when subsequent test steps need the same engine session.
        """
        retry_call(engine.run_cmd, fargs=[''], tries=find_prompt_tries, delay=find_prompt_delay, logger=logger)

    @staticmethod
    def run_cmd_and_reconnect(engine, command, find_prompt_tries=5, find_prompt_delay=2):
        """
            this tool will help u to run commands that disconnect the admin

        :param engine:
        :param command:
        :param find_prompt_tries:
        :param find_prompt_delay:
        :return:
        """
        with allure.step('Run {} and reconnect'.format(command)):
            engine.send_config_set(command, exit_config_mode=False, cmd_verify=False, enter_config_mode=False)
            engine.disconnect()
            DutUtilsTool.reconnect_engine(engine, find_prompt_tries, find_prompt_delay)

            return ResultObj(result=True, info="Reconnected After Running {}".format(command))

    @staticmethod
    def wait_on_system_reboot(engine, reboot_params: Optional[RebootParams] = None, device=None, verify_final_result=True, wait_for_nvos=True):
        """
        Call this after an operation that should trigger a reboot. Will wait on the switch until it's functional.
        The RebootParams object can be used to control some parameters. If omitted, default values are used.
        """
        # A reboot tears down the remote sshd session; the session-scoped ansible
        # ControlMaster stays alive (ControlPersist) and would block the next ansible
        # task for minutes. Flag it so test teardown drops the stale master (see
        # reset_ansible_connection_after_reboot). Reset/install paths that bypass this
        # function are covered by the same flag set in the readiness waits below.
        pytest.dut_rebooted = True
        if not isinstance(reboot_params, RebootParams):
            reboot_params = RebootParams()
        with allure.step("Waiting for switch shutdown after reload command"):
            check_port_status_till_alive(
                False, engine.ip, engine.ssh_port,
                tries=reboot_params.wait_time_before_reboot / 2)  # divide by 2 because delay=2 seconds
            if reboot_params.track_boot_intervals:
                InstallStepsTimer.add_timestamp(InstallSteps.SHUT_DOWN)
            engine.disconnect()
            if not reboot_params.should_wait_till_system_ready:
                return ResultObj(result=True, info="system is not ready yet")

        with allure.step("Waiting for system to reboot and become available"):
            dut_engine: LinuxSshEngine = reboot_params.recovery_engine or engine

            # Attach to the serial console BEFORE the port-up wait. "System is ready" is a
            # one-shot banner printed *before* the mgmt IP/port becomes reachable sometimes, so attaching
            # serial only after the port-up wait races against (and loses to) it. Connecting now
            # lets the pexpect buffer accumulate the banner while we poll the port.
            serial_engine = None
            if wait_for_nvos and reboot_params.topology_obj:
                with allure.step('get serial engine (before port-up wait)'):
                    serial_engine = ConnectionTool.create_serial_engine(reboot_params.topology_obj,
                                                                        enter_serial_context=True)
            with allure.step("Waiting for switch to be ready"):
                with allure.step('wait for switch reachable/ping'):
                    check_port_status_till_alive(True, dut_engine.ip, dut_engine.ssh_port)
                if wait_for_nvos and reboot_params.topology_obj:
                    with allure.step('wait for System is ready in serial'):
                        if reboot_params.system_is_ready_timeout:
                            DutUtilsTool.wait_for_system_ready_in_serial(reboot_params.topology_obj,
                                                                         serial_engine=serial_engine,
                                                                         wait_timeout=reboot_params.system_is_ready_timeout)
                        elif device:
                            DutUtilsTool.wait_for_system_ready_in_serial(reboot_params.topology_obj,
                                                                         serial_engine=serial_engine,
                                                                         wait_timeout=device.timeout_system_is_ready)
                        else:
                            DutUtilsTool.wait_for_system_ready_in_serial(reboot_params.topology_obj,
                                                                         serial_engine=serial_engine)
                        if reboot_params.track_boot_intervals:
                            InstallStepsTimer.add_timestamp(InstallSteps.SYSTEM_IS_READY_AFTER_UPGRADE)
                if not wait_for_nvos:
                    with allure.step('wait for ssh'):
                        dut_engine.run_cmd('echo "SSH OK"')
                    return ResultObj(result=True, info="rebooted, ssh up, but system is not ready yet")
                if reboot_params.check_system_is_functional:
                    with allure.step('wait for os to be functional'):
                        if device:
                            result_obj = device.wait_for_os_to_become_functional(dut_engine)
                        else:
                            result_obj = DutUtilsTool.wait_for_nvos_to_become_functional(
                                dut_engine,
                                throw_exception_on_unhealthy=reboot_params.throw_exception_on_unhealthy)

                        if verify_final_result:
                            result_obj.verify_result()
                        return result_obj
                else:
                    return ResultObj(result=True, info="rebooted, System is ready, but postponing the check of system is functional")

    @staticmethod
    def wait_for_system_ready_in_serial(topology_obj, serial_engine: PexpectSerialEngine = None, wait_timeout=330):
        system_ready_pattern = ['System is ready', 'cumulus login:']
        with allure.step('get serial engine'):
            serial_engine: PexpectSerialEngine = serial_engine or ConnectionTool.create_serial_engine(topology_obj, enter_serial_context=True)
        with allure.step(f'wait for "{system_ready_pattern}". timeout: {wait_timeout}'):
            serial_engine.run_cmd('', system_ready_pattern, timeout=wait_timeout, send_without_enter=True)

    @staticmethod
    def wait_for_nvos_to_become_functional(engine, find_prompt_tries=60, find_prompt_delay=10, /, *,
                                           throw_exception_on_unhealthy: bool = True, cli_up_retries: int = 36):
        # Readiness choke point reached after any reboot/reset/install, including paths
        # that bypass wait_on_system_reboot (e.g. factory reset). Flag it so test teardown
        # drops the stale ansible ControlMaster (see reset_ansible_connection_after_reboot).
        pytest.dut_rebooted = True
        with allure.step('wait until the system is ready - check SYSTEM_STATE table'):
            with allure.step('wait for the system table to exist'):
                wait_for_system_table_to_exist(engine)

            output = ''
            try:
                with allure.step('check system state in redis'):
                    output = DatabaseTool.sonic_db_cli_hgetall(engine=engine, asic="",
                                                               db_name=DatabaseConst.STATE_DB_NAME,
                                                               table_name='\"SYSTEM_READY|SYSTEM_STATE\"')
                    assert SystemConsts.STATUS_DOWN not in output and '(empty array)' not in output
            except AssertionError:
                if SystemConsts.STATUS_DOWN in output:
                    return ResultObj(result=False, info="THE SYSTEM IS NOT OK", issue_type=IssueType.PossibleBug)

                if '(empty array)' in output:
                    return ResultObj(result=False, info="SYSTEM_READY|SYSTEM_STATE TABLE IS MISSED",
                                     issue_type=IssueType.PossibleBug)

            with allure.step('wait until the CLI is up'):
                wait_until_cli_is_up(engine, retries=cli_up_retries)

            try:
                with allure.step('Wait until systemctl status is "running"'):
                    wait_on_systemctl_initialization(engine)
            except BaseException as ex:
                logging.error("System is not ready according to systemctl status")
                engine.run_cmd("nv show system health")
                raise ex

            with allure.step('Validate system health status is OK'):
                from ngts.nvos_tools.system.System import System
                from ngts.nvos_constants.constants_nvos import HealthConsts

                System().validate_health_status(
                    HealthConsts.OK, dut_engine=engine, throw_exception=throw_exception_on_unhealthy
                )

            return ResultObj(result=True, info="System Is Ready", issue_type=IssueType.PossibleBug)

    @staticmethod
    def wait_for_cumulus_to_become_functional(engine, find_prompt_tries=60, find_prompt_delay=10):
        # Readiness choke point after reboot/reset/install on Cumulus/Eth DUTs (counterpart
        # of wait_for_nvos_to_become_functional). Flag it so teardown refreshes the ansible
        # connection (see reset_ansible_connection_after_reboot).
        pytest.dut_rebooted = True
        with allure.step('wait until the CLI is up'):
            wait_until_cli_is_up(engine)

        return ResultObj(result=True, info="System Is Ready", issue_type=IssueType.PossibleBug)

    @staticmethod
    def get_url(engine, command_opt='scp', file_full_path=''):
        if not engine or not engine.username:
            return ResultObj(result=False, info="No Engine")

        with allure.step('Trying to create url for {}'.format(engine.username)):

            with allure.step('check engine is reachable'):
                ssh_connection = ConnectionTool.create_ssh_conn(engine.ip, engine.username,
                                                                engine.password).verify_result()
                if not ssh_connection:
                    return ResultObj(result=False, info="{} is unreachable".format(engine.ip))

            with allure.step('generate url'):
                remote_url = '{}://{}:{}@{}{}'.format(command_opt, engine.username, engine.password, engine.ip,
                                                      file_full_path)

            return ResultObj(result=True, info=remote_url, returned_value=remote_url)

    @staticmethod
    def run_cmd_with_disconnect(engine, cmd, timeout=5):
        try:
            return engine.run_cmd(cmd, timeout=timeout, retry_run=False)
        except (socket.error, ReadTimeout, NetmikoTimeoutException) as e:
            logging.info('Got error: %s. Current engine was also disconnected', e)
            engine.disconnect()
            return "Action succeeded"

    @staticmethod
    def _nv_show_system_output_indicates_nvue_ready(output: str) -> bool:
        if not output or "CLI is unavailable" in output:
            return False
        if "System is ready" in output:
            return True
        try:
            out_obj = OutputParsingTool.parse_json_str_to_dictionary(output)
            if not out_obj.result or not isinstance(out_obj.returned_value, dict):
                return False
            health = out_obj.returned_value.get(SystemConsts.HEALTH)
            if isinstance(health, dict):
                st = health.get(HealthConsts.STATUS)
                if st in (HealthConsts.OK, SystemConsts.STATUS_DEFAULT_VALUE):
                    return True
        except (AttributeError, TypeError, KeyError, ValueError):
            pass
        return False

    @staticmethod
    def wait_for_nv_show_system_ready_on_ssh(engine: LinuxSshEngine, timeout_sec: int, poll_sec: int = 10) -> None:
        """
        Wait until `nv show system` shows NVUE is ready. Uses `retry_run=False` so ProxySshEngine
        does not sit in its internal pre-check loop.

        After a BMC-only reset the host may not reboot, so serial may never print a new
        \"System is ready\"; use this instead of `wait_for_system_ready_in_serial` in that case.
        """
        cmd = "nv show system -o json --color off"
        deadline = time.time() + timeout_sec
        last_out = ""
        while time.time() < deadline:
            try:
                last_out = engine.run_cmd(cmd, timeout=90, validate=False, retry_run=False)
            except (OSError, socket.error, ReadTimeout, NetmikoTimeoutException) as e:
                logger.warning(
                    "wait_for_nv_show_system_ready_on_ssh: %s; reconnecting and retrying.", e
                )
                try:
                    engine.disconnect()
                except OSError:
                    pass
                DutUtilsTool.reconnect_engine(engine, find_prompt_tries=5, find_prompt_delay=3)
                time.sleep(poll_sec)
                continue
            except Exception as e:
                logger.warning("wait_for_nv_show_system_ready_on_ssh: %s", e)
                time.sleep(poll_sec)
                continue
            if DutUtilsTool._nv_show_system_output_indicates_nvue_ready(last_out):
                return
            time.sleep(poll_sec)
        raise TimeoutError(
            f"Timed out after {timeout_sec}s waiting for NVUE readiness in `nv show system` output. "
            f"Last output (truncated): {last_out[:1200]!r}"
        )

    @staticmethod
    def get_engine_interface_name(engine, topology) -> str:
        specific: Dict[str, str] = \
            topology.players['dut']['attributes'].noga_query_data['attributes']['Specific']
        setup_mgmt_ips = [specific.get('ip_address'), specific.get('ip_address_2')]
        setup_mgmt_ipv6s = [specific.get('ipv6'), specific.get('ipv6_2')]
        interface = ''
        for index, (mgmt_ip, mgmt_ipv6) in enumerate(zip(setup_mgmt_ips, setup_mgmt_ipv6s)):
            if engine.ip in (mgmt_ip, mgmt_ipv6):
                interface = 'eth' + str(index)
        logger.info(f"engine interface name {interface}")
        return interface

    @staticmethod
    def get_prompt(engine: LinuxSshEngine) -> str:
        return engine.engine.find_prompt()

    @staticmethod
    def get_running_dockers(engine: LinuxSshEngine) -> List[str]:
        output = engine.run_cmd('docker ps --format \"table {{.Names}}\"', print_output=False)
        title, *dockers = output.splitlines()
        if title.strip().lower() != 'names':
            raise Exception("Got invalid response: " + output)
        return dockers

    @staticmethod
    def dut_psu_control(engines, topology_obj, skip_str='', psu_state='', dhcp_hostname='',):
        from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
        cli = NvueGeneralCli(engines.dut)
        with allure.step("Reboot {} the PSUs using reboot script".format(psu_state)):
            server_ip = cli.get_site_server_ip(topology_obj)
            ssh_conn = LinuxSshEngine(ip=server_ip, username=os.getenv("TEST_SERVER_USER"),
                                      password=os.getenv("TEST_SERVER_PASSWORD"))
            reboot_cmd = skip_str + '/.autodirect/mswg/utils/bin/rreboot ' + dhcp_hostname + ' ' + psu_state
            ssh_conn.run_cmd(reboot_cmd)
            time.sleep(2)

    @staticmethod
    def follow_journal_until_pattern(engine: LinuxSshEngine, service: str, patterns: list[str], timeout: int = 300,
                                     since_boot: bool = False):
        """
        Follow a systemd journal service and return as soon as any pattern matches.
        Raises TimeoutError if no pattern is matched within the given timeout.

        Uses ``timeout Xs journalctl -fu SERVICE | sed '/PATTERN/ q'`` which exits
        cleanly (rc 0) on match and with rc 143 on timeout.

        :param engine:   SSH engine (LinuxSshEngine / ProxySshEngine)
        :param service:  systemd service name (e.g. 'ztp')
        :param patterns: list of regex patterns to watch for
        :param timeout:  max seconds to wait (default 300)
        :param since_boot: if True, read from the start of the current boot (-b flag)
        :return: journal output up to and including the matched line
        :raises TimeoutError: if none of the patterns appeared within *timeout* seconds
        """
        sed_pattern = '|'.join(patterns)
        boot_flag = '-b --no-tail ' if since_boot else ''
        cmd = f"timeout {timeout}s journalctl {boot_flag}-fu {service} | sed -E '/{sed_pattern}/ q'"
        output = engine.run_cmd(cmd, validate=False, timeout=timeout + 10, retry_run=False)
        allure.attach(f'{service} journal output', output)
        rc = engine.run_cmd('echo $?', validate=False).strip()
        if rc == '0':
            for i, pattern in enumerate(patterns):
                if re.search(pattern, output):
                    return i, output
            else:
                raise RuntimeError(f'None of the expected patterns appeared in the journal, {output=}')

        raise TimeoutError(
            f'Journal follow for {service!r} timed out after {timeout}s without matching any of: {patterns}\n'
        )


def ping_device(ip_add):
    try:
        return _ping_device(ip_add)
    except BaseException as ex:
        logging.error(str(ex))
        logging.info(f"ip address {ip_add} is unreachable")
        return False


@retry(Exception, tries=5, delay=10)
def _ping_device(ip_add):
    with allure.step(f"Ping device ip {ip_add}"):
        cmd = f"ping -c 3 {ip_add}"
        logging.info(f"Running cmd: {cmd}")
        process = subprocess.Popen(cmd.split(), stdout=subprocess.PIPE)
        output, error = process.communicate()
        logging.info("output: " + str(output))
        logging.info("error: " + str(error))
        if " 0% packet loss" in str(output):
            logging.info("Reachable using ip address: " + ip_add)
            return True
        else:
            logging.error("Unreachable using ip address: " + ip_add)
            logging.info(f"ip address {ip_add} is unreachable")
            raise Exception(f"ip address {ip_add} is unreachable")


@retry(Exception, tries=48, delay=5)
def wait_for_system_table_to_exist(engine):
    output = DatabaseTool.sonic_db_cli_hgetall(engine=engine, asic="", db_name=DatabaseConst.STATE_DB_NAME,
                                               table_name='\"SYSTEM_READY|SYSTEM_STATE\"')
    if '(empty array)' in output:
        logger.info('Waiting to SYSTEM_STATUS table to be available')
        raise Exception("System is not ready yet")
    return True


def wait_until_cli_is_up(engine, retries=36):
    @retry(Exception, tries=retries, delay=5)
    def _wait():
        logger.info('Checking the status of nvued')
        output = DutUtilsTool.run_cmd_with_disconnect(engine, 'nv show system')
        if 'CLI is unavailable' in output:
            raise Exception("Waiting for NVUE to become functional")
    _wait()


@retry(Exception, tries=24, delay=5)
def wait_on_systemctl_initialization(engine):
    if not is_bug_active(4434382):
        output = DutUtilsTool.run_cmd_with_disconnect(engine, SystemConsts.SYSTEMCTL_STATUS_CMD)
        if "running" not in output:
            raise Exception("Waiting for systemctl to finish initializing")


def wait_for_specific_regex_in_logs(engine, regex, timeout=70):
    """

    :param engine:
    :param regex:
    :param timeout
    :return:
    """
    device = {'device_type': engine.device_type, 'host': engine.ip, 'username': engine.username,
              'password': engine.password, 'timeout': timeout}
    with allure.step(f"wait for {timeout} seconds to see '{regex}' in logs"):
        connection = ConnectHandler(**device)
        connection.send_command('nv show system log file follow', expect_string=regex, read_timeout=timeout)
        return


def run_ssh_command(command, ip_address, username, password):
    '''
    @Summary: This function inits ssh connection to the server, and runs commands on it.
              It will print the output of the command gradually (line by line).
    @param command: The command we want to run on the server using ssh connection, example: "ll"
    @return: full_output, which is the output of the command the was run.
    '''
    logger.info("Initializing connection to server {}".format(ip_address))

    ssh_command = [
        'sshpass', '-p', password,
        'ssh', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'StrictHostKeyChecking=no',
        '-o', 'TCPKeepAlive=yes', '-o', 'ServerAliveInterval=60', '-o', 'ConnectTimeout=30',
        '{}@{}'.format(username, ip_address), command
    ]

    try:
        output_lines = []
        # Run the SSH command and capture live output
        process = subprocess.Popen(
            ssh_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Redirect stderr to stdout
            bufsize=1,  # Line-buffered
            universal_newlines=True  # Use universal newlines to treat output as string
        )

        # Read output line by line
        while True:
            line = process.stdout.readline()
            if not line:
                break
            print(line.strip())
            output_lines.append(line)

        # Wait for the subprocess to finish and get the return code
        process.wait()
        full_output = ''.join(output_lines)
        return full_output

    except Exception as e:
        logger.error("An error occurred: {}".format(e))
        return None
