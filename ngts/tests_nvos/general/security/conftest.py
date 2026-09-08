import logging
import pexpect
import pytest
import time

from devts.infra.tools.general_constants.constants import DefaultConnectionValues
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.SshCmdBuilder import SshPassCmdBuilder
from .security_test_tools.constants import AaaConsts, AuthConsts
from .security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.nvos_tools.infra.SecureBootTool import SecureBootTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.infra.PexpectTool import PexpectTool
from ngts.ngts_types import EnginesT, DevicesT, TopologyT
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.Devices.EthDevice import EthSwitch  # temporary, needed until nv unification RM 3735390.
from .security_test_tools import security_test_utils
from ngts.tools.test_utils import switch_recovery
from ngts.nvos_tools.system.System import System
from ngts.tools.allure_report import allure_set_environment

logger = logging.getLogger(__name__)


def security_cleanup(ssh_session: PexpectTool) -> bool:
    success = False
    if not ssh_session or not isinstance(ssh_session, PexpectTool):
        return success
    with allure.step('Security cleanup'):
        with allure.step('check session still connected to switch'):
            session_is_live = False
            ssh_session.sendline('nv show system')

            while True:
                try:
                    i = ssh_session.expect(DefaultConnectionValues.DEFAULT_PROMPTS, timeout=15)
                    if i < len(DefaultConnectionValues.DEFAULT_PROMPTS) and ('product-name' in ssh_session.last_output):
                        session_is_live = True
                        logger.info("Session is live")
                        break

                except pexpect.exceptions.TIMEOUT:
                    logger.info("No more output detected due to timeout.")
                    break

        if session_is_live:
            with allure.step('unset authentication config to allow local connection'):
                cmds = TestToolkit.devices.dut.aaa_cleanup_cmds
                expect_timeout = 60
                ssh_session.sendline(' ; '.join(cmds))
                i = ssh_session.expect(DefaultConnectionValues.DEFAULT_PROMPTS, timeout=expect_timeout,
                                       raise_exception_for_timeout=False)
                assert i != PexpectTool.TIMEOUT, f'security cleanup failed: expect prompt after apply failed: exceeded expect timeout: {expect_timeout} seconds'
                success = i < len(DefaultConnectionValues.DEFAULT_PROMPTS) and any(
                    msg in ssh_session.last_output for msg in ['applied', 'config apply executed with no config diff'])
    return success


@pytest.fixture(autouse=True)
def check_ssh_connections(engines: EnginesT):
    # check number of SSH connections before test
    who_output = engines.dut.run_cmd("who")
    result = engines.dut.run_cmd("who | wc -l")
    logger.debug(f"Number of SSH connections before test: {result.strip()}")
    logger.debug("who output before test:\n%s", who_output)

    yield

    # check number of SSH connections after test
    who_output = engines.dut.run_cmd("who")
    result = engines.dut.run_cmd("who | wc -l")
    logger.debug(f"Number of SSH connections after test: {result.strip()}")
    logger.debug("who output after test:\n%s", who_output)


@pytest.fixture()
def cleanup_after_aaa(topology_obj: TopologyT, engines: EnginesT, request: pytest.FixtureRequest, devices: DevicesT):
    dut: LinuxSshEngine = engines.dut

    with allure.step('ssh the switch with long logout time'):
        # ssh_cmd = f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=60 -o ServerAliveCountMax=5 {dut.username}@{dut.ip}'
        sshpass_cmd = SshPassCmdBuilder(dut.username, dut.password, dut.ip).set_ssn().set_long_lasting_session().build()
        ssh_session = PexpectTool(sshpass_cmd)
        # ssh_session.expect('[Pp]assword:')
        i = ssh_session.expect(DefaultConnectionValues.DEFAULT_PROMPTS)
        assert i < len(DefaultConnectionValues.DEFAULT_PROMPTS), 'could not ssh the switch'
    with allure.step('update pytest item for the cleanup stage'):
        item = request.node
        item.security_pexpect_ssh_session = ssh_session

    try:
        yield
    finally:
        try:
            skip_rr = security_cleanup(ssh_session)

            if engines and topology_obj and not skip_rr:
                with allure.step('try recover with remote reboot'):
                    switch_recovery.recover_dut_with_remote_reboot(topology_obj, engines)  # TODO: there was another clear config (try without for now)
        finally:
            ssh_session.close()
            request.node.security_pexpect_ssh_session = None


def create_ssh_login_engine(dut_ip: str, username: str, port: int = 22, custom_ssh_options: str = None) -> pexpect.spawn:
    '''
    @summary: in this function we want to create ssh connection to device,
    ssh connection means that only executing the command:
    'ssh {-o OPTIONS} -l {username} {dut_ip}'
    without entering password!
    :param dut_ip: device IP
    :param username: username initiating the ssh connection
    :param port: connection port, by default 22
    :return: pexpect python module with ssh connection command executed
    '''
    ssh_options = custom_ssh_options if custom_ssh_options is not None else DefaultConnectionValues.BASIC_SSH_CONNECTION_OPTIONS
    _ssh_command = f'ssh {ssh_options} -p {port} -l {username} {dut_ip}'
    # connect to device
    child = pexpect.spawn(_ssh_command, env={'TERM': 'dumb'}, timeout=10)
    return child


def ssh_to_device_and_retrieve_raw_login_ssh_notification(
    dut_ip: str,
    username: str = None,
    password: str = None,
    port: int = 22,
) -> str:
    '''
    @summary: in this function we create ssh connection
    and return the raw output after connecting to device
    '''
    notification_login_message = ''
    device = TestToolkit.devices.dut
    if not username or not password:
        username = device.default_username
        password = device.default_password

    with allure.step("Connection to dut device with SSH"):
        logger.info("Connection to dut device with SSH")
        # connecting using pexpect
        try:
            child = create_ssh_login_engine(dut_ip, username, port)
            respond = child.expect([DefaultConnectionValues.PASSWORD_REGEX, '~'])
            if respond == 0:
                notification_login_message += child.before.decode('utf-8')
                child.sendline(password)
                child.expect(DefaultConnectionValues.DEFAULT_PROMPTS[0])

            # convert output to decode
            notification_login_message += child.before.decode('utf-8')
            # close connection
        finally:
            child.close()
        return notification_login_message


@pytest.fixture(scope='function')
def post_test_remote_reboot(topology_obj: TopologyT):
    '''
    @summary: perform remote reboot from the physical server using the noga remote reboot command,
    usually the command should be like this: '/auto/mswg/utils/bin/rreboot <ip|hostname>'
    after the test is done as a part of cleanup
    '''
    yield

    logger.info("Performing remote reboot to switch")
    cmd = topology_obj.players['dut_serial']['attributes'].noga_query_data['attributes']['Specific'][
        'remote_reboot']
    assert cmd, "Reboot command is empty"
    topology_obj.players['server']['engine'].run_cmd(cmd)
    SLEEP_AFTER_REBOOT = 60
    logger.info(f"Sleeping {SLEEP_AFTER_REBOOT} secs after reboot")
    time.sleep(SLEEP_AFTER_REBOOT)
    # verify dockers are up
    logger.info("Verifying that dockers are up")
    TestToolkit.engines.dut.disconnect()
    nvue_cli = NvueGeneralCli(TestToolkit.engines.dut, TestToolkit.devices.dut)
    nvue_cli.verify_dockers_are_up()


@pytest.fixture(scope='function')
def is_secure_boot_enabled(engines: EnginesT):
    if SecureBootTool.is_secure_boot_disabled(engines.dut):
        logger.warning("The test is skipped - secure boot is disabled")
        pytest.skip("The test is skipped - secure boot is disabled")


def _publish_system_version_to_allure_env(config: pytest.Config, system: System) -> None:
    """
    Publish the version running on the switch to the allure report environment tab (environment.properties).

    environment.properties is a line based 'key=value' file, so only a single line value may be reported.
    Reporting metadata must never fail a test, hence a failure here is only logged.

    :param config: pytest config object, which holds the allure environment entries.
    :param system: System component to read the version from.
    """
    try:
        allure_set_environment(config, 'system_version', system.version.get_running_version())
    except Exception:
        logger.warning('Could not publish the system version to the allure environment', exc_info=True)


@pytest.fixture(scope='module', autouse=True)
def show_sys_version(engines: EnginesT, pytestconfig: pytest.Config):
    """
    For regression analysis, show the system info (and version) before each test case/file,
    and report the running version in the allure environment tab
    """
    with allure.step('Before test case: show system info'):
        system = System()
        if isinstance(TestToolkit.devices.dut, EthSwitch):  # temporary, needed until nv unification RM 3735390.
            attachment = '\n'.join(
                [system.show(), engines.dut.run_cmd('cat /etc/image-release'), NvueGeneralCli.show_config(engines.dut)])
        else:
            attachment = '\n'.join([system.show(), system.version.show(), NvueGeneralCli.show_config(engines.dut)])
        allure.orig_allure.attach(attachment, 'system_version_and_conf', allure.orig_allure.attachment_type.TEXT)
        _publish_system_version_to_allure_env(pytestconfig, system)


@pytest.fixture(scope='function')
def local_adminuser(engines: EnginesT, devices: DevicesT) -> UserInfo:
    adminrole = devices.dut.aaa_admin_role
    adminuser = UserInfo(username=AaaConsts.LOCALADMIN, password=switch_recovery.generate_strong_password(), role=adminrole)
    logger.info(f'Local admin user for test: "{adminuser.username}", "{adminuser.password}"')
    security_test_utils.set_local_users(engines, [adminuser], apply=True)
    try:
        yield adminuser
    finally:
        security_test_utils.cleanup_local_users(engines, [adminuser])


@pytest.fixture(scope="session", autouse=False)
def prepare_scp(engines: EnginesT, devices: DevicesT):
    """
    @summary: Ensure SCP test files exist on the switch for verification.
    Checks if files exist and only creates them if missing.
    """
    admin_monitor_mutual_group = "adm"
    admins_group = devices.dut.get_admins_group()

    # Check and prepare directory for admin users only
    result = engines.dut.run_cmd(f"test -f {AuthConsts.SWITCH_ADMIN_SCP_DOWNLOAD_TEST_FILE} && echo exists")
    if "exists" not in result:
        logger.info("Prepare directory for admin users only")
        engines.dut.run_cmd(f"mkdir -p {AuthConsts.SWITCH_ADMINS_DIR}")
        engines.dut.run_cmd(f'echo "SCP test content" > {AuthConsts.SWITCH_ADMIN_SCP_DOWNLOAD_TEST_FILE}')
        engines.dut.run_cmd(f"chgrp -R {admins_group} {AuthConsts.SWITCH_ADMINS_DIR}")
        engines.dut.run_cmd(f"chmod -R 770 {AuthConsts.SWITCH_ADMINS_DIR}")

    # Check and prepare non-privileged directory
    result = engines.dut.run_cmd(f"test -f {AuthConsts.SWITCH_MONITOR_SCP_DOWNLOAD_TEST_FILE} && echo exists")
    if "exists" not in result:
        logger.info("Prepare non-privileged directory")
        engines.dut.run_cmd(f"mkdir -p {AuthConsts.SWITCH_MONITORS_DIR}")
        engines.dut.run_cmd(f'echo "SCP test content" > {AuthConsts.SWITCH_MONITOR_SCP_DOWNLOAD_TEST_FILE}')
        engines.dut.run_cmd(f"sudo chgrp -R {admin_monitor_mutual_group} {AuthConsts.SWITCH_MONITORS_DIR}")
        engines.dut.run_cmd(f"chmod -R 770 {AuthConsts.SWITCH_MONITORS_DIR}")

    yield


@pytest.fixture(scope='session')
def switch_hostname(engines: EnginesT):
    return OutputParsingTool.parse_json_str_to_dictionary(System().show()).get_returned_value()[SystemConsts.HOSTNAME]

# @pytest.fixture(scope='function')
# def disable_remote_auth_after_test(engines):
#     """
#     @summary: disable remote authentication after test
#         * aaa tests should update SecurityTestToolKit.active_remote_server each time configuring a remote server.
#     """
#     yield
#
#     active_remote_server = SecurityTestToolKit.active_remote_server
#     if not active_remote_server:
#         return
#
#     active_admin_user = [user for user in active_remote_server.users if user.role == AaaConsts.ADMIN][0]
#     orig_username, orig_password = engines.dut.username, engines.dut.password
#     engines.dut.update_credentials(username=active_admin_user.username, password=active_admin_user.password)
#
#     System().aaa.unset(apply=True)
#     if isinstance(active_remote_server, LdapServerInfo):
#         DutUtilsTool.wait_for_nvos_to_become_functional(TestToolkit.engines.dut).verify_result()
#
#     engines.dut.update_credentials(username=orig_username, password=orig_password)
#
#     TestToolkit.active_remote_auth_server = None
