import logging
import random
import shlex
import socket
import string
import subprocess
import time

try:
    from netmiko.ssh_exception import NetmikoAuthenticationException  # type: ignore
except ImportError:
    from netmiko.exceptions import NetmikoAuthenticationException

from devts.infra.tools.validations.traffic_validations.ping import send as send_ping
from devts.infra.tools.general_constants.constants import DefaultConnectionValues
from devts.infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine

from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.Devices.BaseDevice import BaseDevice
from ngts.nvos_tools.infra.PexpectTool import PexpectTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.ngts_types import TopologyT, EnginesT

logger = logging.getLogger(__name__)


def recover_dut_with_remote_reboot(topology_obj: TopologyT, engines: EnginesT, wait_after_rr: int = None, device: BaseDevice = None) -> None:
    with allure.step('Execute remote reboot'):
        NvueGeneralCli(engines.dut).remote_reboot_nvue(topology_obj)
    if wait_after_rr:
        with allure.step(f'sleep {wait_after_rr} seconds'):
            time.sleep(wait_after_rr)
    with allure.step('Ping switch until shutting down'):
        send_ping.ping_till_alive(should_be_alive=False, destination_host=engines.dut.ip)
    with allure.step('Wait for switch to be up'):
        DutUtilsTool.wait_for_system_ready_in_serial(topology_obj)
        engines.dut.disconnect()
        DutUtilsTool.wait_for_nvos_to_become_functional(engines.dut).verify_result()


def generate_strong_password(n: int = 10) -> str:
    assert n >= 10, f'Given argument "n" - {n} - is too small. Must be at least 10'

    lower_len: int = random.randint(1, 9)
    upper_len: int = n - lower_len

    lowers_str: str = RandomizationTool.get_random_string(length=lower_len)
    uppers_str: str = RandomizationTool.get_random_string(length=upper_len, ascii_letters=string.ascii_uppercase)
    rand_num_str: str = str(random.randint(0, 999))

    return lowers_str + '_' + uppers_str + rand_num_str


def new_handle_change_password_prompt(engine: ProxySshEngine):
    SUCCESS, FAIL = 'success', 'fail'

    ip, port, username, orig_password = engine.ip, engine.ssh_port, engine.username, engine.password
    # _ssh_command = DefaultConnectionValues.SSH_CMD.copy() + ['-p', str(port)] + ['-l', username, ip]
    # ssh_cmd = ' '.join(_ssh_command)
    ssh_cmd = f'ssh -tt -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o GSSAPIAuthentication=no -o ' \
        f'PubkeyAuthentication=no -p {port} -l {username} {ip}'
    with allure.step('SSH the switch'):
        pexpect_tool = PexpectTool(spawn_cmd=ssh_cmd)
    with allure.step('Expect password prompt'):
        expect = ['New password:', 'password:'] + DefaultConnectionValues.DEFAULT_PROMPTS
        res = pexpect_tool.expect(expect, raise_exception_for_timeout=False)
        if res not in range(len(expect)):
            return FAIL
        if res > 1:
            return SUCCESS
    if res == 1:
        with allure.step('Enter orig password'):
            pexpect_tool.sendline(orig_password)
        with allure.step('Expect new password prompt'):
            expect = ['New password:'] + DefaultConnectionValues.DEFAULT_PROMPTS
            res = pexpect_tool.expect(expect, raise_exception_for_timeout=False)
            if res not in range(len(expect)):
                return FAIL
            if res > 0:
                return SUCCESS
    if res == 0:
        with allure.step('Generate strong password'):
            new_password = generate_strong_password()
        with allure.step(f'Enter "{new_password}" as new password'):
            pexpect_tool.sendline(new_password)
        with allure.step('Expect retype new password prompt'):
            expect = 'Retype new password:'
            res = pexpect_tool.expect(expect, raise_exception_for_timeout=False)
            if res != 0:
                return FAIL
        with allure.step(f'Enter "{new_password}" again and finish'):
            pexpect_tool.sendline(new_password)
        with allure.step('Expect default prompt after login'):
            expect = DefaultConnectionValues.DEFAULT_PROMPTS
            res = pexpect_tool.expect(expect, raise_exception_for_timeout=False)
            if res not in range(len(expect)):
                return FAIL
        with allure.step('Reset password'):
            reset_cmd = f'nv set sys se p st dis ; nv c a ; nv se sys a u {username} pa {orig_password} ; nv c a ; nv ' \
                f'set sys se p st en; nv c a ; nv c save '
            pexpect_tool.sendline(reset_cmd)
        with allure.step('Expect default prompt after login and finish'):
            expect = DefaultConnectionValues.DEFAULT_PROMPTS
            res = pexpect_tool.expect(expect, raise_exception_for_timeout=False)
            if res not in range(len(expect)):
                return FAIL
            return SUCCESS if (pexpect_tool.expect('.*', raise_exception_for_timeout=False) == 0) else FAIL


def check_switch_connectivity(topology_obj: TopologyT, engines: EnginesT, *, allow_dut_reboot: bool = True) -> bool:
    NETMIKO_ERR, SOCKET_ERR, SUCCESS = 'Netmiko', 'Socket', 'success'

    def check_connectivity():
        try:
            with allure.step('Check connectivity of local engine with dummy show command'):
                logger.info(System().aaa.show())
                return SUCCESS
        except NetmikoAuthenticationException as e:
            logger.exception(f'Netmiko Exception:\n{e}')
            return NETMIKO_ERR
        except socket.error as e:
            logger.exception(f'Socket Error:\n{e}')
            return SOCKET_ERR
        finally:
            pass

    res = check_connectivity()
    if res == SUCCESS:
        return
    with allure.step('Disconnect dut engine and try again'):
        engines.dut.disconnect()
        res = check_connectivity()
        if res == SUCCESS:
            return
    with allure.step(f'Got {res} error. Try recover with new change password handling'):
        res = new_handle_change_password_prompt(engines.dut)
        if res == SUCCESS:
            res = check_connectivity()
            if res == SUCCESS:
                return

    if not allow_dut_reboot:
        logger.error("DUT connectivity could not be restored without a reboot")
        return False

    with allure.step(f'Got {res} error. Try recover with remote reboot to dut'):
        recover_dut_with_remote_reboot(topology_obj, engines)


def remote_reboot_dut(topology_obj):
    with allure.step("Remote reboot DUT"):
        cmd = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific']['remote_reboot']
        cmd = cmd.replace('/auto', '/.autodirect')
        logging.info(f"Running cmd: {cmd}")
        p = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            p.communicate(timeout=60)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            logger.debug('Process is not responding. Sending SIGKILL.')
            p.kill()
            std_out, std_err = p.communicate()
            rc = p.returncode
            std_out = str(std_out.decode('utf-8') or '')
            std_err = str(std_err.decode('utf-8') or '')
            logging.info(f"std_out = {std_out}, std_err = {std_err}")
        return rc == 0
