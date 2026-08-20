from typing import Callable, Dict, Generator
from functools import partial
import contextlib
import logging
import random
import pytest
import time
import re
import string
from ngts.helpers.secure_boot_helper import SecureBootHelper
from ngts.tests_nvos.constants import MINUTE
from paramiko import SSHClient, AutoAddPolicy
from ngts.ngts_types import CleanUpT, EnginesT
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import switch_recovery
from ngts.tools.test_utils import nvos_general_utils
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool, wait_until_cli_is_up
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.tests_nvos.general.security.radius.constants import RadiusPhysicalServer
from ngts.tests_nvos.general.security.tacacs.constants import TacacsDockerServer1
from ngts.tests_nvos.general.security.test_aaa_ldap.ldap_servers_info import LdapServersP3
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.tests_nvos.general.security.security_test_tools.constants import AaaConsts, AddressingType, AuthConsts, AuthMedium, UserRole
from ngts.tests_nvos.general.security.security_test_tools.generic_remote_aaa_testing.constants import RemoteAaaType
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.RemoteAaaServerInfo import RemoteAaaServerInfo
from devts.infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine

logger = logging.getLogger(__name__)
DEFAULT_TEST_PASSWORD = 'Aa123456!'
USERNAME_LENGTH = 8
# ``_restore_default_passwords`` may run as a cleanup on a DUT that is still booting, and tries two
# passwords, so keep the per-attempt wait short enough to bound the total cleanup time.
CLI_UP_RETRIES_FOR_PASSWORD_RESTORE = 24
# Factory-reset boot is slower than the helper's 330s default, but ``timeout_system_is_ready`` is
# 20 minutes on Rosalind/Juliet. Using that full budget with no pytest timeout made a missing
# "System is ready" banner look like a hang. 10 minutes is enough for a real factory-reset boot
# and still fails in a bounded time if the banner never appears.
FACTORY_RESET_SERIAL_READY_TIMEOUT = 10 * MINUTE
LONG_PRESS_RESET_FACTORY_TIMEOUT = 20 * MINUTE


def _test_ssh_connection(engines: EnginesT, user: UserInfo) -> bool:
    try:
        with SSHClient() as ssh:
            ssh.set_missing_host_key_policy(AutoAddPolicy())
            ssh.connect(engines.dut.ip, username=user.username, password=user.password)
            # run a simple command to confirm the authenticated session is usable, not just that auth succeeded
            _, stdout, stderr = ssh.exec_command('id')
            id_output = stdout.read().decode().strip()
            exit_status = stdout.channel.recv_exit_status()
            err_output = stderr.read().decode().strip()
            success = exit_status == 0 and bool(id_output)
            if not success:
                logger.error(f"'id' command failed for user {user.username} on {engines.dut.ip}: "
                             f"exit_status={exit_status}, stdout={id_output!r}, stderr={err_output!r}")
    except Exception as e:
        logger.error(f"SSH connection to {engines.dut.ip} failed for user {user.username} with password {user.password}: {e}")
        success = False
    return success


def _set_user_password(engines: EnginesT, system: System, usr_name: str, usr_password: str) -> None:
    """
    Set a user password and saves it
    """
    with allure.step("Disable password policy"):
        system.security.password_hardening.set('state', AaaConsts.DISABLED, dut_engine=engines.dut, apply=True).get_returned_value()
    with allure.step(f"Set user: {usr_name} password: {usr_password}"):
        system.aaa.user.user_id[usr_name].set(AaaConsts.PASSWORD, usr_password, dut_engine=engines.dut, apply=True).get_returned_value()
    with allure.step("Enable password policy"):
        system.security.password_hardening.set('state', AaaConsts.ENABLED, dut_engine=engines.dut, apply=True).get_returned_value()
    NvueGeneralCli.save_config(engines.dut)


def _restore_default_passwords(engines: EnginesT, system: System) -> bool:
    """
    Put admin and monitor back on their default passwords and re-sync the SSH engine to them.

    Both the current engine password and the default are tried, because which one authenticates depends on
    whether a factory reset already reverted the DUT.

    :return: True once the defaults are applied. False when every attempt failed - the DUT password is then
             unknown, so the engine is deliberately left on the last password tried rather than claiming the
             default, which would hide the real state from the next test.
    """
    passwords_to_try = [engines.dut.password]
    if AaaConsts.ADMIN not in passwords_to_try:
        passwords_to_try.append(AaaConsts.ADMIN)
    for password_to_connect_with in passwords_to_try:
        try:
            engines.dut.password = password_to_connect_with
            wait_until_cli_is_up(engines.dut, retries=CLI_UP_RETRIES_FOR_PASSWORD_RESTORE)
            _set_user_password(engines, system, AaaConsts.ADMIN, AaaConsts.ADMIN)
            # admin answers to the default from here on, so re-sync before the next command authenticates
            engines.dut.password = AaaConsts.ADMIN
            _set_user_password(engines, system, AaaConsts.MONITOR, AaaConsts.MONITOR)
            return True
        except Exception as e:
            logger.warning(f'Could not restore default passwords using password "{password_to_connect_with}": {e}')
    logger.error('Failed to restore the default passwords. The DUT password is unknown, so the console and '
                 'any following test may fail to authenticate')
    return False


def _make_default_password_restorer(engines: EnginesT, system: System) -> Callable[[], None]:
    """
    Build a one-shot revert of the admin/monitor passwords to their defaults.

    Two call sites need the revert: the post-reboot phase, which must re-sync the engine before the later
    checkers run, and the registered cleanup, which covers a failure before that phase is reached.
    Whichever runs first does the work and the other becomes a no-op, so the happy path does not pay for it
    twice. A failed attempt is not marked done, so the cleanup still retries it.
    """
    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            logger.info('default passwords already restored - skipping')
            return
        restored = _restore_default_passwords(engines, system)

    return restore


def _admin_password_restore_check(engines: EnginesT, feature_enabled: bool, register_cleanup: CleanUpT):
    system = System()
    old_password = engines.dut.password
    new_password = DEFAULT_TEST_PASSWORD
    restore_default_passwords = _make_default_password_restorer(engines, system)
    with allure.step("Change admin password"):
        _set_user_password(engines, system, AaaConsts.ADMIN, new_password)
        engines.dut.password = new_password
        _set_user_password(engines, system, AaaConsts.MONITOR, new_password)
        # The DUT now holds a non-default password, saved to its configuration. Register the revert here so
        # it also runs when the test fails before the post-reboot phase below (the readiness wait after the
        # reboot is the usual place): a leaked test password locks the console out of every following test,
        # because the console credentials come from the Noga topology defaults and not from this engine.
        register_cleanup(restore_default_passwords)

    yield   # reboot

    admin_usr: UserInfo = UserInfo(AaaConsts.ADMIN, old_password if feature_enabled else new_password, AaaConsts.ADMIN)
    monitor_usr: UserInfo = UserInfo(AaaConsts.MONITOR, AaaConsts.MONITOR if feature_enabled else new_password, AaaConsts.MONITOR)
    try:
        with allure.step("Test ssh connection"):
            assert _test_ssh_connection(engines, admin_usr), f"SSH connection failed for admin user with password {admin_usr.password}"
            assert _test_ssh_connection(engines, monitor_usr), f"SSH connection failed for monitor user with password {monitor_usr.password}"
    finally:
        with allure.step("Restore admin password"):
            restore_default_passwords()

    yield    # to prevent StopIteration on the 2nd next() call


def _local_users_restore_check(engines: EnginesT, feature_enabled: bool):
    system = System()
    existing_users = [u for u, _ in system.aaa.user.parse_show().items()]
    # logical limit for admin user name, to avoid infinite while loop
    for i in range(1, 1000):
        admin_candidate = f"admin{i}"
        if admin_candidate not in existing_users:
            break
    random_user = ''.join(random.choices(string.ascii_lowercase, k=USERNAME_LENGTH))
    random_password = switch_recovery.generate_strong_password()
    new_admin: UserInfo = UserInfo(admin_candidate, random_password, AaaConsts.ADMIN)
    new_random: UserInfo = UserInfo(random_user, random_password, AaaConsts.MONITOR)
    with allure.step(f"Create local users: {new_admin.username} and {new_random.username}"):
        system.aaa.user.user_id[new_admin.username].set(AaaConsts.PASSWORD, new_admin.password, apply=True).verify_result()
        system.aaa.user.user_id[new_random.username].set(AaaConsts.PASSWORD, new_random.password, apply=True).verify_result()

    yield  # reboot

    try:
        with allure.step("Check local users after reboot"):
            users_after = [u for u, _ in system.aaa.user.parse_show().items()]
            if feature_enabled:
                assert admin_candidate not in users_after, f"{admin_candidate} should have been deleted after factory reset"
                assert random_user not in users_after, f"{random_user} should have been deleted after factory reset"
            else:
                assert admin_candidate in users_after, f"{admin_candidate} should exist after reboot"
                assert random_user in users_after, f"{random_user} should exist after reboot"
    finally:
        with allure.step("Remove local users"):
            system.aaa.user.user_id[new_admin.username].unset(apply=True).verify_result()
            system.aaa.user.user_id[new_random.username].unset(apply=True).verify_result()

    yield  # to prevent StopIteration on the 2nd next() call


def _aaa_method_keep_check(engines: EnginesT, auth_method: str):
    def _setup_tacacs() -> UserInfo:
        with allure.step('set tacacs server'):
            tac_server: RemoteAaaServerInfo = TacacsDockerServer1.SERVER_BY_ADDRESSING_TYPE[AddressingType.IPV4]
            tac_server.configure(engines)
            return tac_server.users_per_auth_medium[AuthMedium.SSH][UserRole.ADMIN][0]

    def _setup_ldap() -> UserInfo:
        with allure.step('set ldap server'):
            ldap_server: RemoteAaaServerInfo = LdapServersP3.LDAP3_SERVERS[AddressingType.IPV4]
            ldap_server.configure(engines)
            return ldap_server.users_per_auth_medium[AuthMedium.SSH][UserRole.ADMIN][0]

    def _setup_radius() -> UserInfo:
        with allure.step('set radius server'):
            rad_server: RemoteAaaServerInfo = RadiusPhysicalServer.SERVER_BY_ADDRESSING_TYPE[AddressingType.IPV4]
            rad_server.configure(engines)
            return rad_server.users_per_auth_medium[AuthMedium.SSH][UserRole.ADMIN][0]
    system = System()
    with allure.step("set AAA servers"):
        with allure.step(f'set AAA {auth_method} server'):
            aaa_user = {RemoteAaaType.TACACS: _setup_tacacs,
                        RemoteAaaType.LDAP: _setup_ldap,
                        RemoteAaaType.RADIUS: _setup_radius}[auth_method]()
            with allure.step('Set authentication order'):
                system.aaa.authentication.set(AuthConsts.ORDER, [auth_method, AuthConsts.LOCAL], apply=True).verify_result()
                if auth_method == RemoteAaaType.LDAP:
                    nvos_general_utils.wait_for_ldap_nvued_restart_workaround(None, engine_to_use=engines.dut)
                else:
                    time.sleep(3)
            with allure.step('enable failthrough'):
                System().aaa.authentication.set(AuthConsts.FAILTHROUGH, AaaConsts.ENABLED, apply=True).verify_result()

    yield   # reboot

    try:
        with allure.step(f'Verify AAA user access with {auth_method}'):
            assert _test_ssh_connection(engines, aaa_user), f"SSH connection failed for AAA user {aaa_user.username}"
    finally:
        with allure.step("Remove AAA users"):
            system.aaa.unset(op_param=auth_method, apply=True).verify_result()

    yield   # to prevent StopIteration on the 2nd next() call


def _register_cleanup(register_cleanup) -> str:
    """
    Register a cleanup function to restore the initial state of the feature.
    """
    with allure.step('Get current config'):
        current_config: Dict[str, str] = System().aaa.allow_reset_local_passwords.parse_show()
    # restore the initial state config after test is finished.
    register_cleanup(partial(System().aaa.allow_reset_local_passwords.set, 'state', current_config['state'], apply=True))
    return current_config['state']


def _create_serial_engine_with_dut_credentials(topology_obj, engines: EnginesT) -> PexpectSerialEngine:
    """
    Open a console session using the DUT credentials this session actually authenticates with.

    ``SecureBootHelper`` builds the serial engine from the Noga topology attributes, which hold the
    *default* console credentials. This test changes the admin password, so once it has run - or once any
    predecessor failed before reverting it - the console credentials no longer match the DUT and login
    fails with "Login incorrect" while SSH still works. The SSH engine tracks the working password, so
    reuse it for the console.
    """
    serial_engine: PexpectSerialEngine = SecureBootHelper.get_serial_engine_instance(topology_obj)
    serial_engine.username = engines.dut.username
    serial_engine.password = engines.dut.password
    serial_engine.create_serial_engine()
    return serial_engine


@contextlib.contextmanager
def _simulate_long_press(engines: EnginesT):
    try:
        with allure.step('Simulate long press'):
            # check if the long-reboot script is already modified
            if engines.dut.run_cmd('grep -q REMOVE-ME /usr/local/bin/long-reboot'):
                logger.info('Long reboot script is already modified')
            else:
                # find the line number of the return statement in the long-reboot script
                result = engines.dut.run_cmd(r'grep -n "def get_long_reboot_status\|return" /usr/local/bin/long-reboot')
                try:
                    return_line_number = int(re.search(r'def get_long_reboot_status.+$\s*(\d+)', result, flags=re.M).group(1))
                except Exception as e:
                    logger.error(f"Failed to find return line number in long-reboot script: {result}\n{e}")
                    raise e
                # modify the long-reboot script to return True
                cmd = r"sudo sed -i '%da\    return True, chassis.REBOOT_CAUSE_LONG_HARDWARE_BUTTON  # REMOVE-ME' /usr/local/bin/long-reboot"
                engines.dut.run_cmd(cmd % (return_line_number - 1))
        yield
    finally:
        with allure.step('Restore system'):
            # remove the REMOVE-ME line to restore the script to the original state
            engines.dut.run_cmd("sudo sed -i '/REMOVE-ME/d' /usr/local/bin/long-reboot")


ADMIN_PASSWORD = "Admin password"
AAA_METHOD_KEEP = "AAA method keep"
LOCAL_USERS_RESTORE = "Local users restore"


@pytest.mark.simx
@pytest.mark.timeout(LONG_PRESS_RESET_FACTORY_TIMEOUT, func_only=True)
@pytest.mark.parametrize('config_state, auth_method', [
    pytest.param(AaaConsts.DISABLED, random.choice(RemoteAaaType.ALL_TYPES), id=AaaConsts.DISABLED),
    pytest.param(AaaConsts.ENABLED, random.choice(RemoteAaaType.ALL_TYPES), id=AaaConsts.ENABLED),
])
def test_long_press_reset_factory(engines: EnginesT, register_cleanup, topology_obj,
                                  config_state: str, auth_method: str):
    """
    Test the factory reset flow when the long press is used to reset the system.
    If the feature is enabled, the admin and monitor users should be restored, configured AAA method should be kept
    and all local users should be deleted, include local admins such as admin1, admin2, etc.
    Test flow:
        1. Set the feature state to the desired state.
        2. Configure the AAA method and local users, change the admin and monitor passwords.
        3. Simulate the long press to reset the system.
        4. Check the system state after the reset.
        5. Restore the system to the initial state.
    """
    LONG_PRESS_FACTORY_RESET_CHECKERS: Dict[str, Generator[None, None, None]] = {
        ADMIN_PASSWORD: _admin_password_restore_check(engines, config_state == AaaConsts.ENABLED, register_cleanup),
        AAA_METHOD_KEEP: _aaa_method_keep_check(engines, auth_method),
        LOCAL_USERS_RESTORE: _local_users_restore_check(engines, config_state == AaaConsts.ENABLED),
    }
    checkers = LONG_PRESS_FACTORY_RESET_CHECKERS
    if not checkers:
        pytest.skip('test skipped: no checkers registered for this test')
    system = System()
    with allure.step('wait for the CLI to be ready'):
        # This test reboots the DUT, so a preceding case in the same run can still be initializing it.
        wait_until_cli_is_up(engines.dut)
    with allure.step('create serial engine'):
        engines.dut.disconnect()
        engines.dut.run_cmd('id')
        serial_engine: PexpectSerialEngine = _create_serial_engine_with_dut_credentials(topology_obj, engines)
    if _register_cleanup(register_cleanup) != config_state:
        with allure.step(f"Set feature state to {config_state}"):
            system.aaa.allow_reset_local_passwords.set('state', config_state, apply=True).get_returned_value()
    with _simulate_long_press(engines):
        with allure.step('checkers setup'):
            for name, checker in checkers.items():
                with allure.independent_step(f"check {name}"):
                    next(checker)
            NvueGeneralCli.save_config(engines.dut)
        with allure.step('Reboot the system'):
            engines.dut.disconnect()
            serial_engine.serial_engine.sendline('sudo reboot')
            # Longer than the helper's 330s default (too short for a factory-reset boot), shorter than
            # the 20-minute Rosalind/Juliet device budget (unbounded if the banner never prints).
            DutUtilsTool.wait_for_system_ready_in_serial(topology_obj, serial_engine,
                                                         FACTORY_RESET_SERIAL_READY_TIMEOUT)
        with allure.step('checkers cleanup'):
            for name, checker in checkers.items():
                with allure.independent_step(f"check {name}"):
                    next(checker)


@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_feature_default_config(engines: EnginesT, register_cleanup, DEFAULT_CONFIG=AaaConsts.ENABLED):
    """
    Validate the default configuration of the feature allowing reset of local passwords.
    Test flow:
        0. Wait for the CLI: the sibling long-press tests reboot the DUT, so it may still be initializing.
        1. Initialize the system with NVUE API:
            - Ensure the system is set to use the NVUE API.
        2. Unset and apply the 'allow_reset_local_passwords' feature:
            - Unset the feature to ensure it is not explicitly enabled.
        3. Get the new config:
            - Retrieve the new configuration of the 'allow_reset_local_passwords' feature.
        4. Assert the feature's default state:
            - Confirm that the feature is enabled by default, ensuring compliance with expected behavior.
    """
    with allure.step('wait for the CLI to be ready'):
        wait_until_cli_is_up(engines.dut)
    _register_cleanup(register_cleanup)
    system = System()
    with allure.step('Unset the feature'):
        system.aaa.allow_reset_local_passwords.unset(apply=True)
    with allure.step('Get new config'):
        new_config = system.aaa.allow_reset_local_passwords.parse_show()
        assert new_config['state'] == DEFAULT_CONFIG
