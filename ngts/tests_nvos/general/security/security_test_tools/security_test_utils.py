from datetime import datetime
from typing import Iterable
import subprocess
import logging
import shlex
import time
import pytz

from devts.infra.tools.validations.traffic_validations.ping import send as send_ping
from devts.infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine

from ngts.nvos_constants.constants_nvos import AclConsts, ApiType, TestFlowType, SystemConsts
from .tool_classes.AaaServerManager import AaaAccountingLogsFileContent, AaaServerManager
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from .tool_classes.RemoteAaaServerInfo import RemoteAaaServerInfo
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.tests_nvos.system.gnmi.constants import GnmiMode
from ngts.ngts_types import EnginesT, TopologyT, DevicesT
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils import nvos_general_utils
from ngts.nvos_tools.system.System import System
from ngts.constants.constants import GnmiConsts
from ngts.nvos_tools.infra.IpTool import IpTool
from .constants import AuthConsts, AuthMedium
from .tool_classes.UserInfo import UserInfo
from ngts.nvos_tools.acl.acl import Acl
from .constants import AaaConsts

from .tool_classes.AuthVerifier import AUTH_VERIFIERS

logger = logging.getLogger(__name__)


# Per-medium accounting baseline drain (see wait_for_accounting_logs_drained):
# upper bound to wait for the previous medium's late teardown records to flush, and the settle
# interval the (per-user) log file must stay empty for before we trust the baseline. The settle
# window must comfortably exceed the observed TACACS session-teardown write latency; a record
# arriving later than this after the final clear could still slip past the baseline (best-effort).
ACCOUNTING_DRAIN_TIMEOUT_SEC = 20
ACCOUNTING_DRAIN_SETTLE_SEC = 2


_cached_system = None


def _get_system():
    """Get a cached System object configured for NVUE API. Creates once, reuses thereafter."""
    global _cached_system
    if _cached_system is None:
        _cached_system = System(force_api=ApiType.NVUE)
    return _cached_system


def _local_user_exists(engines: EnginesT, username: str) -> bool:
    try:
        output = engines.dut.run_cmd(f'getent passwd {shlex.quote(username)}', validate=False)
    except Exception:
        return False
    return output.startswith(f'{username}:')


def _nvue_user_exists(engines: EnginesT, username: str) -> bool:
    try:
        output = engines.dut.run_cmd(f'nv show system aaa user {shlex.quote(username)} --output json', validate=False)
    except Exception:
        return False
    normalized_output = output.lower()
    return bool(output.strip()) and 'does not exist' not in normalized_output


def _attach_local_user_state(engines: EnginesT, username: str, title: str) -> None:
    try:
        local_output = engines.dut.run_cmd(f'getent passwd {shlex.quote(username)}', validate=False)
    except Exception as error:
        local_output = f'<failed to read getent passwd: {error}>'
    try:
        nvue_output = engines.dut.run_cmd(f'nv show system aaa user {shlex.quote(username)} --output json', validate=False)
    except Exception as error:
        nvue_output = f'<failed to read nv show user: {error}>'
    allure.attach(
        title,
        f'username: {username}\n'
        f'linux getent:\n{local_output or "<empty>"}\n\n'
        f'nvue show user:\n{nvue_output or "<empty>"}',
        log=False,
    )


def _remove_local_only_user(engines: EnginesT, username: str) -> None:
    quoted_username = shlex.quote(username)
    engines.dut.run_cmd(f"sudo userdel -r {quoted_username} || sudo userdel {quoted_username} || true", validate=False)


def cleanup_local_users(engines: EnginesT, users: Iterable) -> None:
    with allure.step('Cleanup local users'):
        for user in users:
            username = user.username if isinstance(user, UserInfo) else user[AaaConsts.USERNAME]
            with allure.step(f'Cleanup local user "{username}"'):
                if _nvue_user_exists(engines, username):
                    logger.info(f'Disconnect and delete NVUE-managed local user: {username}')
                    user_obj = System(force_api=ApiType.NVUE).aaa.user.user_id[username]
                    try:
                        user_obj.action_disconnect().get_returned_value(False)
                    except Exception as error:
                        logger.warning(f'Failed to disconnect user "{username}" before cleanup: {error}')
                    user_obj.unset(apply=True).verify_result()
                elif _local_user_exists(engines, username):
                    logger.warning(f'Removing leaked local-only user through Linux cleanup: {username}')
                    _attach_local_user_state(engines, username, f'LOCAL-ONLY USER CLEANUP: {username}')
                    _remove_local_only_user(engines, username)
                else:
                    logger.info(f'Local user already absent: {username}')
                    continue

                if _local_user_exists(engines, username):
                    _attach_local_user_state(engines, username, f'LOCAL USER CLEANUP FAILED: {username}')
                    raise AssertionError(f'Cleanup left local user "{username}" on the DUT')


def check_nslcd_service(engines: EnginesT):
    """
    @summary: Check the status of nslcd service, and restart it if needed (for next test cases):
    """
    exit_cmd = 'q'
    status_cmd = 'sudo service nslcd status'
    restart_cmd = 'sudo service nslcd restart'

    with allure.step('Check nslcd service status'):
        output = engines.dut.run_cmd_after_cmd([status_cmd, exit_cmd])
        if 'Active: failed' in output:
            logger.info('Service nslcd failed')
            with allure.step('Restart nslcd service'):
                engines.dut.run_cmd(restart_cmd)
            with allure.step('Check nslcd service status again'):
                output = engines.dut.run_cmd([status_cmd, exit_cmd])
                logger.info(f'Service nslcd is active: {"Active: active (running)" in output}')


def sleep_before_auth(sleep_time: int = 3):
    with allure.step(f'Wait {sleep_time} seconds before auth'):
        wait_time_before_auth_test = sleep_time
        logger.info(f'Wait {wait_time_before_auth_test} seconds')
        time.sleep(wait_time_before_auth_test)


def verify_auth_with_medium(medium: AuthMedium, user: UserInfo, expect_login_success: bool, verify_authorization: bool, engines: EnginesT,
                            topology_obj) -> None:
    with allure.step(f'Verify auth with medium: {medium}'):
        user_is_admin = user.role == AaaConsts.ADMIN
        with AUTH_VERIFIERS[medium](user.username, user.password, engines, topology_obj) as medium_obj:
            with allure.step(f'Verify authentication. Expect login success: {expect_login_success}'):
                medium_obj.verify_authentication(expect_login_success)

            if verify_authorization and expect_login_success:
                with allure.step(f'Verify authorization. Role: {user.role}'):
                    medium_obj.verify_authorization(user_is_admin=user_is_admin)


def clear_accounting_logs_on_servers(accounting_server_mngrs: list[AaaServerManager],
                                     retries: int = 3, retry_delay: float = 2):
    """Clear accounting log files on all servers, retrying if the TACACS daemon
    flushes buffered writes after the truncation."""
    with allure.step('Clear accounting logs on servers'):
        for mngr in accounting_server_mngrs:
            for attempt in range(retries):
                mngr.clear_accounting_logs()
                time.sleep(retry_delay)
                remaining = mngr.tail_accounting_logs()
                if not remaining.logs:
                    break
                logger.warning(
                    'Accounting log on %s not empty after clear (attempt %d/%d), retrying',
                    mngr.ip, attempt + 1, retries,
                )


def _prepare_accounting_logs_for_medium(accounting_server_mngrs: list[AaaServerManager], medium: str) -> None:
    settle_seconds = 1
    if medium == AuthMedium.OPENAPI:
        settle_seconds = 2

    with allure.step(f'Wait {settle_seconds} seconds for pending accounting logs before {medium}'):
        time.sleep(settle_seconds)
    clear_accounting_logs_on_servers(accounting_server_mngrs)

    if medium == AuthMedium.OPENAPI:
        with allure.step('Wait 1 second and clear accounting logs again before OpenApi'):
            time.sleep(1)
        clear_accounting_logs_on_servers(accounting_server_mngrs)


def wait_for_accounting_logs_drained(accounting_server_mngrs: list[AaaServerManager], grep: list[str],
                                     timeout_sec: float = ACCOUNTING_DRAIN_TIMEOUT_SEC,
                                     settle_sec: float = ACCOUNTING_DRAIN_SETTLE_SEC) -> bool:
    """
    @summary: Drain leftover accounting records (matching ``grep``) before taking a per-medium baseline.

        TACACS session-teardown records from the *previous* auth medium can be written with a delay.
        A fixed sleep before clearing the logs cannot guarantee they all landed, so a late record can
        arrive *after* clear+baseline and get wrongly counted against the next medium (e.g. OpenAPI,
        which must produce no logs). Instead of a fixed sleep, repeatedly clear the logs and verify
        nothing matching ``grep`` reappears within ``settle_sec``. Once the (per-user) logs stay empty
        for a full settle interval, the previous burst has drained and the baseline is trustworthy.

    @param accounting_server_mngrs: server managers whose accounting logs are drained
    @param grep: grep terms identifying the relevant records (e.g. [switch_hostname, client_username])
    @param timeout_sec: bounded upper limit to wait for the drain to complete (best-effort on timeout)
    @param settle_sec: interval the matching logs must stay empty after a clear to be considered drained
    @return: True if drained within the timeout, False if the timeout was reached (best-effort)
    """
    with allure.step(f'Drain leftover accounting logs (grep: {grep}, timeout: {timeout_sec}s)'):
        deadline = time.time() + timeout_sec
        while True:
            clear_accounting_logs_on_servers(accounting_server_mngrs)
            time.sleep(settle_sec)  # give any in-flight records from the previous medium time to land
            leftover = [mngr for mngr in accounting_server_mngrs
                        if mngr.tail_accounting_logs(grep=grep).logs]
            if not leftover:
                return True
            if time.time() >= deadline:
                logger.warning(f'Accounting logs did not drain within {timeout_sec}s; '
                               f'still seeing records on servers: {[mngr.ip for mngr in leftover]}')
                return False
            logger.info(f'Accounting logs still arriving on {[mngr.ip for mngr in leftover]}; clearing again')


def check_accounting(after_time: str, switch_hostname: str, client_username: str,
                     accounting_server_mngrs: list[AaaServerManager], expect_accounting_logs: list[bool]):
    with allure.step('Verify accounting logs on given servers'):
        for i, mngr in enumerate(accounting_server_mngrs):
            expect_logs = expect_accounting_logs[i]
            with allure.step(f'Check accounting on server: {mngr.ip} , Expect logs: {expect_logs}'):
                switch_hostname = IpTool.get_eth0_hostname(switch_hostname)
                accounting_logs: AaaAccountingLogsFileContent = mngr.tail_accounting_logs(
                    grep=[switch_hostname, client_username], after_time=after_time)
                assert bool(accounting_logs.logs) == expect_logs, \
                    f'There are {"no " if expect_logs else ""}accounting logs ' \
                    f'on server "{mngr.ip}" for user "{client_username}", ' \
                    f'while expected {"" if expect_logs else "not "}to have logs.\n' \
                    f'Actual raw content:\n{accounting_logs.raw_content}'


def verify_user_auth(engines: EnginesT, topology_obj: TopologyT, user: UserInfo, expect_login_success: bool = True,
                     verify_authorization: bool = True, skip_auth_mediums: list[str] = None,
                     accounting_servers: list[RemoteAaaServerInfo] = [], expect_accounting_logs: list[bool] = [],
                     switch_hostname: str = ''):
    """
    @summary: Verify authentication and authorization for the given user.
        Authentication will be verified via all possible mediums - SSH, OpenApi, rcon, SCP.
    @param engines: test engines object
    @param topology_obj: test topology object
    @param user: Details of the given user.
        User is a dictionary in the format:
        {
            username: str,
            password: str,
            role: admin/monitor (str)
        }
    @param expect_login_success: boolean flag, whether login expected to succeed (True) or fail (False).
        Default is True.
    @param verify_authorization: Whether to verify also authorization or not (authentication test only)
    @param skip_auth_mediums: auth mediums to skip from the test (optional)
    """
    assert len(accounting_servers) == len(expect_accounting_logs), \
        f'Arguments "accounting_servers" and "expect_accounting_logs" must be lists of the same length!\n' \
        f'Actual accounting_servers: {accounting_servers}\nActual expect_accounting_logs: {expect_accounting_logs}'

    should_check_accounting = bool(expect_accounting_logs)
    accounting_server_mngrs = [AaaServerManager(server.ipv4_addr, server.docker_name) for server in accounting_servers]
    if should_check_accounting:
        assert switch_hostname, f'Must give "switch_hostname" argument when should check accounting.\n' \
            f'Given hostname: {switch_hostname}'

    # resolve once (pure string op) and reuse for both the drain grep and the accounting check
    resolved_hostname = IpTool.get_eth0_hostname(switch_hostname) if should_check_accounting else switch_hostname

    with nvos_general_utils.loganalyzer_ignore(False and (not expect_login_success)):
        with allure.step(f'Verify auth: User: {user.username} , Password: {user.password} , Role: {user.role} , '
                         f'Expect login success: {expect_login_success}'):
            sleep_before_auth()

            # for ssh, openapi, rcon: test authentication, and then verify role by running show, set, unset commands
            for medium in AuthMedium.ALL_MEDIUMS:
                if skip_auth_mediums and medium in skip_auth_mediums:
                    continue

                if should_check_accounting:
                    # Drain (don't just sleep) so late teardown records from the previous medium can't
                    # leak past the baseline and be counted against this medium (e.g. OpenAPI expects none).
                    drained = wait_for_accounting_logs_drained(accounting_server_mngrs,
                                                               grep=[resolved_hostname, user.username])
                    if not drained:
                        # best-effort: not draining in time risks a flaky accounting result - make it visible
                        allure.orig_allure.attach(
                            f'user={user.username}, hostname={resolved_hostname}, medium={medium}',
                            'accounting_logs_not_drained_before_baseline',
                            allure.orig_allure.attachment_type.TEXT)
                time_at_server: str = datetime.now(pytz.utc).strftime('%b %d %H:%M:%S')  # servers have UTC timezone
                verify_auth_with_medium(medium, user, expect_login_success, verify_authorization, engines, topology_obj)

                if should_check_accounting:
                    time.sleep(2)  # allow TACACS daemon to flush accounting records
                    if medium == AuthMedium.OPENAPI:
                        check_accounting(time_at_server, switch_hostname, user.username, accounting_server_mngrs,
                                         [False for _ in expect_accounting_logs])
                    else:
                        check_accounting(time_at_server, switch_hostname, user.username, accounting_server_mngrs,
                                         expect_accounting_logs)

            logger.info('\n')


def verify_auth_mediums(
    test_flow: str,
    engines: EnginesT,
    topology_obj: TopologyT,
    remote_should_work: bool,
    local_should_work: bool,
    server: RemoteAaaServerInfo = None,
    remote_users_roles_to_check: list[str] = None,
    local_users: list[UserInfo] = None,
    verify_authorization: bool = True,
    skip_auth_mediums: list[str] = None,
    accounting_servers: list[RemoteAaaServerInfo] = [],
    expect_accounting_logs: list[bool] = [],
    switch_hostname: str = '',
) -> None:
    '''
    if should check accounting:
        accounting_preparations()

    for each medium:
        if good flow:
            if remote_should_work:
                (remote_should_work contains ROLES that should check)
                for each role in remote_should_work:
                    user = server.users_per_medium[medium][role]
                    verify_auth_with_medium(user, expect=is_good_flow)

        if bad flow:
            pass

        if should check accounting:
            accounting_check()
    '''
    is_good_flow = test_flow == TestFlowType.GOOD_FLOW

    assert len(accounting_servers) == len(expect_accounting_logs), \
        f'Arguments "accounting_servers" and "expect_accounting_logs" must be lists of the same length!\n' \
        f'Actual accounting_servers: {accounting_servers}\nActual expect_accounting_logs: {expect_accounting_logs}'

    should_check_accounting = bool(accounting_servers)
    accounting_server_mngrs = [AaaServerManager(server.ipv4_addr, server.docker_name) for server in accounting_servers]
    if should_check_accounting:
        assert switch_hostname, f'Must give "switch_hostname" argument when should check accounting.\n' \
            f'Given hostname: {switch_hostname}'

    auth_mediums = [medium for medium in AuthMedium.ALL_MEDIUMS if (not skip_auth_mediums) or (medium not in skip_auth_mediums)]
    with allure.step(f'verify auth through mediums: {auth_mediums}'):
        for medium in auth_mediums:
            with allure.independent_step(medium):
                # NOTE: unlike verify_user_auth, this path does not clear/drain accounting logs between
                # mediums (it relies on the per-medium 'after_time' baseline only). If a similar
                # late-record race surfaces here, mirror the wait_for_accounting_logs_drained() drain.
                sleep_before_auth()
                time_at_server: str = datetime.now(pytz.utc).strftime('%b %d %H:%M:%S')  # our AAA servers have UTC timezone

                if (is_good_flow == remote_should_work) and remote_users_roles_to_check:
                    with allure.independent_step(f'{test_flow} remote users check'):
                        for role in remote_users_roles_to_check:
                            with allure.independent_step(role):
                                user = server.users_per_auth_medium[medium][role][0]
                                with allure.step(f'{user.username} / {user.password} ({user.role}, {medium}) - {remote_should_work}'):
                                    verify_auth_with_medium(medium, user, remote_should_work, verify_authorization, engines, topology_obj)
                if (is_good_flow == local_should_work) and local_users:
                    with allure.independent_step(f'{test_flow} local users check'):
                        for user in local_users:
                            with allure.independent_step(f'{user.username} / {user.password} ({user.role}, {medium}) - {local_should_work}'):
                                verify_auth_with_medium(medium, user, local_should_work, verify_authorization, engines, topology_obj)

                if should_check_accounting:
                    with allure.step('check accounting'):
                        check_accounting(time_at_server, switch_hostname, user.username, accounting_server_mngrs,
                                         expect_accounting_logs)


def verify_users_auth(
    engines: EnginesT,
    topology_obj: TopologyT,
    users: list[UserInfo],
    expect_login_success: list[bool] = None,
    verify_authorization: bool = True,
    skip_auth_mediums: list[str] = None,
) -> None:
    """
    @summary: Verify authentication and authorization for the given users.
        Authentication will be verified via all possible mediums - SSH, OpenApi, rcon, SCP.
    @param engines: test engines object
    @param topology_obj: test topology object
    @param users: list of users to verify.
        User is a dictionary in the format:
        {
            username: str,
            password: str,
            role: admin/monitor (str)
        }
    @param expect_login_success: list of boolean flags, whether login expected to succeed (True) or fail (False).
        Default is True for all users.
    @param verify_authorization: Whether to verify also authorization or not (authentication test only)
    @param skip_auth_mediums: auth mediums to skip from the test (optional)
    """
    with allure.step('Verify users auth'):
        expect_login_success = [True] * len(users) if not expect_login_success else expect_login_success

        for i, user in enumerate(users):
            verify_user_auth(engines, topology_obj, user, expect_login_success[i], verify_authorization, skip_auth_mediums)


def validate_users_authorization_and_role(
    engines: EnginesT,
    users: list[UserInfo],
    login_should_succeed: bool = True,
    check_nslcd_if_login_failed: bool = False,
) -> None:
    """
    @summary:
        in this function we want to iterate on all users given and validate that access to switch
        and role as expected.
        We will restore the engine to default credentials afterwards
    """
    for user in users:
        username = user[AaaConsts.USERNAME]
        password = user[AaaConsts.PASSWORD]
        role = user[AaaConsts.ROLE]
        with allure.step(f"Check user: {username} , password: {password} , role: {role}"):
            with allure.step(f'Try login - expect: {"success" if login_should_succeed else "fail"}'):
                try:
                    new_engine = ProxySshEngine(device_type=engines.dut.device_type, ip=engines.dut.ip,
                                                username=username, password=password)
                    new_engine.run_cmd('')
                    # engines.dut.update_credentials(username=username, password=password)
                except Exception:
                    logger.info("Got an exception - can not connect to switch")
                    if check_nslcd_if_login_failed:
                        check_nslcd_service(engines)
                    assert not login_should_succeed, 'Login fail, expect success'
                    continue
                assert login_should_succeed, 'Login success, expect fail'

            SLEEP_BEFORE_EXECUTING_CMDS = 1
            with allure.step(f"Sleeping {SLEEP_BEFORE_EXECUTING_CMDS} secs before executing commands"):
                time.sleep(SLEEP_BEFORE_EXECUTING_CMDS)
                if role == AaaConsts.ADMIN:
                    with allure.step('FOR DEBUG - after login, run: sudo stat /var/log/audit.log'):
                        new_engine.run_cmd('sudo stat /var/log/audit.log')

            with allure.step("Running show command - expect: success"):
                system = _get_system()
                try:
                    system.version.show(dut_engine=new_engine)
                except Exception as ex:
                    logger.info("Got an exception - can not run show command")
                    raise ex

            is_admin = role == SystemConsts.DEFAULT_USER_ADMIN

            with allure.step(f'Run set command - expect: {"success" if is_admin else "fail"}'):
                system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value='"NVOS TESTS"',
                                   apply=is_admin, dut_engine=new_engine).verify_result(should_succeed=is_admin)

            with allure.step(f'Run unset command - expect: {"success" if is_admin else "fail"}'):
                system.message.unset(op_param=SystemConsts.PRE_LOGIN_MESSAGE, apply=is_admin,
                                     dut_engine=new_engine).verify_result(should_succeed=is_admin)


def find_server_admin_user(server_info: RemoteAaaServerInfo) -> UserInfo:
    with allure.step('Find server admin user'):
        admin_user = None
        for user in server_info[AuthConsts.USERS]:
            if user[AaaConsts.ROLE] == AaaConsts.ADMIN:
                admin_user = user
        assert admin_user, "Couldn't find admin user, check server configuration"
        return admin_user


def restore_original_engine_credentials(engines: EnginesT, devices: DevicesT) -> None:
    """
    @summary:
        in this fixture we will restore default credentials to dut engine
    """
    with allure.step('Restore original engine credentials'):
        logger.info("Restoring default credentials, and logging in to switch")
        engines.dut.update_credentials(username=devices.dut.default_username,
                                       password=devices.dut.default_password)


def validate_authentication_fail_with_credentials(engines: EnginesT, username: str, password: str) -> None:
    """
    @summary: in this helper function we want to validate authentication failure while using
    username and password credentials
    """
    with allure.step(f"Validating failed authentication with new credentials, username: {username}"):
        ConnectionTool.create_ssh_conn(engines.dut.ip, username=username, password=password).verify_result(
            should_succeed=False)


def validate_services_and_dockers_availability(engines: EnginesT, devices: DevicesT) -> None:
    """
    @summary: validate all services and dockers are up
    """
    with allure.step("validating all services and dockers are up"):
        devices.dut.verify_dockers(engines.dut).verify_result()
        devices.dut.verify_services(engines.dut).verify_result()


def configure_authentication(
    engines: EnginesT,
    devices: DevicesT,
    order: list[str] = None,
    failthrough: list[str] = None,
    fallback: list[str] = None,
    apply=False,
    dut_engine: ProxySshEngine = None,
) -> None:
    """
    @summary:
        Configure different authentication settings as given
    """
    if order == failthrough == fallback is None:
        return

    dut_engine = engines.dut if not dut_engine else dut_engine

    with allure.step('Configure authentication settings'):
        auth_obj = _get_system().aaa.authentication
        if order:
            logger.info(f'Set authentication order: {order}')
            auth_obj.set(AuthConsts.ORDER, order, dut_engine=dut_engine).verify_result()
        if failthrough:
            logger.info(f'Set authentication failthrough: {failthrough}')
            auth_obj.set(AuthConsts.FAILTHROUGH, failthrough, dut_engine=dut_engine).verify_result()
        # if fallback:
        #     logger.info(f'Set authentication fallback: {fallback}')
        #     auth_obj.set(AuthConsts.FALLBACK, fallback).verify_result()

    if apply:
        with allure.step('Apply settings'):
            SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config, dut_engine,
                                            True)

        if order:
            with allure.step('Validate that services and dockers are up'):
                DutUtilsTool.wait_for_nvos_to_become_functional(engines.dut)


def user_lists_difference(users_a: list[UserInfo], users_b: list[UserInfo]) -> list[UserInfo]:
    """
    @summary: Get the difference of the two given user lists.
        * Difference (like sets difference): A - B = all elements of A that are not in B.
        * Here, the elements are users (dictionaries {username: str, password: str, role: <admin, monitor>}),
            then the result will be all users of A, that don't have the same username as any user of B.
        @param users_a: users list A
    @param users_b: users list B
    @return: list of the difference.
    """
    with allure.step('Get users lists difference'):
        a_usernames = [user[AaaConsts.USERNAME] for user in users_a]
        b_usernames = [user[AaaConsts.USERNAME] for user in users_b]
        logger.info(f'A: {a_usernames}\nB: {b_usernames}')

        usernames_diff = list(set(a_usernames) - set(b_usernames))
        logger.info(f'Diff: {usernames_diff}')

        return [user for user in users_a if user[AaaConsts.USERNAME] in usernames_diff]


def mutual_users(users_a: list[UserInfo], users_b: list[UserInfo]) -> list[UserInfo]:
    """
    @summary: Get the mutual of the two given user lists.
    @param users_a: users list A
    @param users_b: users list B
    @return: list of the mutual users.
    """
    with allure.step('Get mutual users list'):
        a_usernames = [user[AaaConsts.USERNAME] for user in users_a]
        b_usernames = [user[AaaConsts.USERNAME] for user in users_b]
        logger.info(f'A: {a_usernames}\nB: {b_usernames}')

        mutual_usernames = list(set(a_usernames).intersection(set(b_usernames)))
        logger.info(f'Mutual users: {mutual_usernames}')

        return [user for user in users_a if user[AaaConsts.USERNAME] in mutual_usernames]


def set_local_users(engines: EnginesT, users: list[UserInfo], apply: bool = False) -> None:
    """
    @summary: Set the given users on local.
        * users should be a list of users.
        * a user should be a dictionary in the following format:
            {
                username: str ,
                password: str ,
                role: <admin, monitor>
            }
    @param engines: engines object
    @param users: users list (list of dictionaries)
    """
    staged_users_via_nvue = False
    with allure.step(f'Set {len(users)} local users'):
        for user in users:
            if isinstance(user, UserInfo):
                username = user.username
                password = user.password
                role = user.role
            else:
                username = user[AaaConsts.USERNAME]
                password = user[AaaConsts.PASSWORD]
                role = user[AaaConsts.ROLE]
            with allure.step(f'Set user "{username}" with role: {role}'):
                user_obj = _get_system().aaa.user.user_id[username]
                logger.info(f'Set user: {username} , password: {password}')
                user_obj.set(AaaConsts.PASSWORD, password).verify_result()
                logger.info(f'Set user: {username} , role: {role}')
                user_obj.set(AaaConsts.ROLE, role).verify_result()
                staged_users_via_nvue = True

    if apply and staged_users_via_nvue:
        with allure.step('Apply changes together'):
            # set_local_users configures users through NVUE objects, so apply via NVUE too.
            SendCommandTool.execute_command(TestToolkit.GeneralApi[ApiType.NVUE].apply_config, engines.dut, True).verify_result()


def check_ldap_user_with_getent_passwd(engine: ProxySshEngine, username: str, user_should_exist: bool) -> None:
    with allure.step('Get getent passwd output'):
        output = engine.run_cmd('getent passwd | grep ldap')
    with allure.step(f'Verify "{username}" {"exists" if user_should_exist else "does not exist"}'):
        err_msg = f'username "{username}" unexpectedly {"does not " if user_should_exist else ""}exist ' \
            f'in getent passwd output\ngetent passwd output: {output}\n'
        if not output:
            assert not user_should_exist, err_msg
        else:
            rows = output.split('\n')
            assert rows, f'Unknown error. Could not split output "{output}" to rows.\nActual split: {rows}'
            assert any(row.startswith(f'{username}:') for row in rows) == user_should_exist, err_msg


def check_ldap_user_groups_with_id(engine: ProxySshEngine, username: str, groupname: str,
                                   group_should_exist: bool, timeout: float = 0,
                                   poll_interval: float = 1) -> None:
    groups = [groupname] if isinstance(groupname, str) else groupname
    cmd = f'id {username}'
    deadline = time.monotonic() + timeout
    output = ''
    violating_groups = list(groups)

    with allure.step('Get id output'):
        while True:
            output = engine.run_cmd(cmd)
            if group_should_exist:
                violating_groups = [group for group in groups if f'({group})' not in output]
            else:
                violating_groups = [group for group in groups if f'({group})' in output]

            if not violating_groups:
                return

            if time.monotonic() >= deadline:
                timeout_note = (
                    f'timed out after {timeout}s waiting for LDAP user groups to reach expected state.\n'
                    if timeout > 0 else ''
                )
                raise AssertionError(
                    f'{timeout_note}'
                    f'some groups violating the expectations.\n'
                    f'groups that expected{"" if group_should_exist else " not"} '
                    f'to exist for user "{username}": {groups}\n'
                    f'violating groups: {violating_groups}\n'
                    f'cmd: {cmd}\n'
                    f'full output: {output}'
                )

            logging.info(
                'LDAP groups %s have not reached the expected state for user %s; retrying in %ss',
                violating_groups, username, poll_interval,
            )
            time.sleep(poll_interval)


# ==================== FIPS Mode Management Functions ====================

FIPS_KEX_ALGOS = {
    "diffie-hellman-group16-sha512",
    "diffie-hellman-group18-sha512",
    "diffie-hellman-group14-sha256"
}

FIPS_PUBKEY_ALGOS = {
    "ecdsa-sha2-nistp256-cert-v01@openssh.com",
    "ecdsa-sha2-nistp384-cert-v01@openssh.com",
    "ecdsa-sha2-nistp521-cert-v01@openssh.com",
    "rsa-sha2-512-cert-v01@openssh.com",
    "rsa-sha2-256-cert-v01@openssh.com",
    "rsa-sha2-512",
    "rsa-sha2-256",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521"
}


def configure_ssh_server_fips_algorithms(engines: EnginesT) -> None:
    """Configure SSH server algorithms required for FIPS mode"""
    with allure.step('Configure SSH server algorithms'):
        system = _get_system()

        # Check current configuration
        current_config = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.show()).get_returned_value()
        current_kex = current_config.get("kex-algorithms", [])
        current_pubkey = current_config.get("pubkey-accepted-algorithms", [])

        # Check if current configuration matches desired configuration (ignoring order)
        kex_match = set(current_kex) == FIPS_KEX_ALGOS
        pubkey_match = set(current_pubkey) == FIPS_PUBKEY_ALGOS

        # Only apply if different
        if not kex_match or not pubkey_match:
            logger.info("SSH algorithms need update for FIPS compliance")
            # Create space-separated strings from sets
            kex_algos = " ".join(FIPS_KEX_ALGOS)
            pubkey_algos = " ".join(FIPS_PUBKEY_ALGOS)

            system.ssh_server.set("kex-algorithms", kex_algos)
            system.ssh_server.set("pubkey-accepted-algorithms", pubkey_algos)
            NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')
        else:
            logger.info("SSH algorithms already configured for FIPS compliance")


def _apply_config_with_expected_disconnect(engine: ProxySshEngine, operation_name: str = "operation") -> str:
    """
    Apply config when we expect the session to disconnect (e.g., reboot, FIPS mode changes).
    Uses send_command with short timeout and catches expected exceptions.
    """
    with allure.step(f'Apply config with expected disconnect ({operation_name})'):
        try:
            # Save original timeout
            original_timeout = engine.engine.timeout if hasattr(engine.engine, 'timeout') else None

            # Set short timeout for expected disconnect
            if hasattr(engine.engine, 'timeout'):
                engine.engine.timeout = 5

            # Use netmiko's send_command directly
            output = engine.engine.send_command(
                'nv config apply -y',
                expect_string=r'(cumulus@.*:.*\$|applied)',  # Either prompt or success message
                max_loops=50,
                delay_factor=0.1
            )
            logger.info(f"Apply output: {output}")

            # Restore original timeout
            if original_timeout is not None and hasattr(engine.engine, 'timeout'):
                engine.engine.timeout = original_timeout

            return output
        except Exception as e:
            # Expected - connection will be lost during reboot/disconnect
            logger.info(f"Apply interrupted as expected during {operation_name}: {type(e).__name__}: {e}")

            # Restore original timeout even on exception
            try:
                if original_timeout is not None and hasattr(engine.engine, 'timeout'):
                    engine.engine.timeout = original_timeout
            except Exception:
                pass

            return "applied (disconnected as expected)"


def get_fips_state(engines: EnginesT) -> dict:
    """
    Get and parse current FIPS mode state.

    Args:
        engines: Test engines

    Returns:
        dict: Dictionary with 'operational', 'applied', and 'pending' keys (if exists)
              Example: {'operational': 'enabled', 'applied': 'enabled', 'pending': 'disabled'}
              Returns empty dict if parsing fails
    """
    with allure.step('Get FIPS state'):
        try:
            current_state = engines.dut.run_cmd("nv show system security fips")
            logger.debug(f"Raw FIPS state output:\n{current_state}")

            fips_state = {}
            lines = current_state.split('\n')

            for line in lines:
                # Skip empty lines
                if not line.strip():
                    continue

                # Check the data line that starts with "mode"
                if line.strip().startswith('mode'):
                    parts = line.split()
                    # Format: mode  operational  applied  [pending]
                    # Parts: [0]='mode', [1]=operational_value, [2]=applied_value, [3]=pending_value (optional)
                    if len(parts) >= 3:
                        fips_state['operational'] = parts[1]
                        fips_state['applied'] = parts[2]
                        if len(parts) >= 4:
                            fips_state['pending'] = parts[3]
                    break

            logger.info(f"Parsed FIPS state: {fips_state}")
            return fips_state

        except Exception as e:
            logger.warning(f"Failed to parse FIPS state: {e}")
            return {}


def switch_fips_mode(engines: EnginesT, on: bool = True, should_reboot: bool = True, expect_disconnect: bool = False) -> None:
    """
    Switch FIPS mode on or off with optional reboot.

    Args:
        engines: Test engines
        on: If True, enable FIPS mode. If False, disable FIPS mode.
        should_reboot: If True, apply config and reboot. If False, only set FIPS mode without applying.
    """
    mode_str = 'enabled' if on else 'disabled'
    action_str = 'Enable' if on else 'Disable'
    step_name = f'{action_str} FIPS mode' + (' with reboot' if should_reboot else ' (no reboot)')

    with allure.step(step_name):
        try:
            # Check if FIPS is already in desired state (check operational state)
            fips_state = get_fips_state(engines)
            current_operational = fips_state.get('operational', '')

            if current_operational == mode_str:
                logger.info(f"FIPS mode is already {mode_str} (operational)")
                return

            logger.info(f"FIPS mode not in desired state ({mode_str}). Current state: {fips_state}")

            # Configure FIPS-compliant SSH algorithms before enabling FIPS
            if on:
                configure_ssh_server_fips_algorithms(engines)

            # Set FIPS mode to desired state
            engines.dut.run_cmd(f"nv set system security fips mode {mode_str}")

            # Verify FIPS mode is in pending state before applying
            with allure.step(f'Verify FIPS mode change to {mode_str} is pending'):
                pending_state = get_fips_state(engines)
                if pending_state.get('pending') != mode_str:
                    logger.warning(f"FIPS mode may not be in pending state. State: {pending_state}")
                else:
                    logger.info(f"FIPS mode {action_str.lower()} is pending, ready to apply")

            if expect_disconnect:
                # Apply config without waiting for response (reboot will disconnect)
                _apply_config_with_expected_disconnect(engines.dut, f"FIPS {action_str.lower()}")
            else:
                NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')

            # If should_reboot is False, stop here without applying
            if not should_reboot:
                logger.info(f"FIPS mode set to {mode_str} (pending) without applying - reboot skipped")
                return

            # Reboot and wait for system to come back
            logger.info(f"FIPS mode {action_str.lower()} configuration applied, system should be rebooting...")
            _reboot_and_wait_for_system(engines)

            # Verify FIPS is in desired state after reboot
            new_state = get_fips_state(engines)
            if new_state.get('operational') != mode_str:
                raise Exception(f"FIPS mode not {mode_str} after reboot. Current state: {new_state}")

            logger.info(f"FIPS mode {mode_str} successfully after reboot")

        except Exception as e:
            logger.error(f"Failed to {action_str.lower()} FIPS mode: {str(e)}")
            raise


# Backward compatibility aliases
def enable_fips_mode(engines: EnginesT, should_reboot: bool = True, expect_disconnect: bool = False) -> None:
    """Enable FIPS mode (wrapper for switch_fips_mode)"""
    return switch_fips_mode(engines, on=True, should_reboot=should_reboot, expect_disconnect=expect_disconnect)


def disable_fips_mode(engines: EnginesT, should_reboot: bool = True, expect_disconnect: bool = False) -> None:
    """Disable FIPS mode (wrapper for switch_fips_mode)"""
    return switch_fips_mode(engines, on=False, should_reboot=should_reboot, expect_disconnect=expect_disconnect)


def _reboot_and_wait_for_system(engines: EnginesT, reboot_timeout: int = 300, system_ready_timeout: int = 600) -> None:
    """Reboot the system and wait for it to become functional"""

    with allure.step('Reboot system and wait for it to become ready'):
        try:
            # Execute reboot command
            with allure.step('Execute reboot command'):
                engines.dut.run_cmd("sudo reboot", timeout=10)

        except Exception as e:
            # Expected - connection will be lost during reboot
            logger.info(f"Connection lost during reboot (expected): {str(e)}")

        # Wait for system to go down
        with allure.step('Wait for system to go down'):
            time.sleep(30)  # Give time for reboot to start
            send_ping.ping_till_alive(should_be_alive=False, destination_host=engines.dut.ip, tries=reboot_timeout)

        # Wait for system to come back up
        with allure.step('Wait for system to come back up'):
            send_ping.ping_till_alive(should_be_alive=True, destination_host=engines.dut.ip, tries=system_ready_timeout)

        # Disconnect and reconnect to refresh connection
        with allure.step('Reconnect to system'):
            engines.dut.disconnect()
            time.sleep(10)  # Wait a bit before reconnecting

        # Wait for NVOS to become functional
        with allure.step('Wait for NVOS to become functional'):
            result = DutUtilsTool.wait_for_nvos_to_become_functional(engines.dut)
            if not result.result:
                raise Exception(f"System not ready after reboot: {result.info}")

        logger.info("System reboot completed and NVOS is functional")


def is_fips_enabled(engines: EnginesT) -> bool:
    """Check if FIPS mode is currently enabled (operational state)"""
    with allure.step('Check if FIPS is enabled'):
        try:
            fips_state = get_fips_state(engines)
            return fips_state.get('operational', '') == 'enabled'
        except Exception as e:
            logger.warning(f"Failed to check FIPS status: {str(e)}")
            return False


def get_fips_status(engines: EnginesT) -> dict:
    """Get current FIPS mode status (parsed dictionary)"""
    return get_fips_state(engines)


def change_max_files(engines: EnginesT, max_files: int = 65535) -> bool:
    """
    Edit /etc/security/limits.conf to set maximum file descriptor limits for all users.
    Also set the current session limits using ulimit.
    """
    with allure.step(f'Change max files limit to {max_files}'):
        try:
            # Create backup of original file
            engines.dut.run_cmd("sudo cp /etc/security/limits.conf /etc/security/limits.conf.backup")

            # Add new limits for all users
            limits_entry = f"* soft nofile {max_files}\n* hard nofile {max_files}\n"

            # Read file content using sudo cat
            current_content = engines.dut.run_cmd("sudo cat /etc/security/limits.conf")

            if limits_entry not in current_content:
                # Append the new limits to the file
                cmd = f'echo "{limits_entry}" | sudo tee -a /etc/security/limits.conf'
                engines.dut.run_cmd(cmd)
                logger.info(f"Limits entry added to /etc/security/limits.conf: {limits_entry}")

            # Set current session limits using ulimit
            engines.dut.run_cmd(f"ulimit -n {max_files}")  # Set soft limit
            engines.dut.run_cmd(f"ulimit -Hn {max_files}")  # Set hard limit

            return True
        except Exception as e:
            logger.error(f"Error setting file descriptor limits: {str(e)}")
            # Restore backup if operation failed
            engines.dut.run_cmd("sudo cp /etc/security/limits.conf.backup /etc/security/limits.conf")
            return False


def increase_pty_limit(engines: EnginesT, max_ptys: int = 65535) -> bool:
    """
    Increase the number of PTY devices available on the system.
    """
    with allure.step(f'Increase PTY limit to {max_ptys}'):
        try:
            # Check current PTY limit
            current_limit = engines.dut.run_cmd("cat /proc/sys/kernel/pty/max").strip()
            logger.info(f"Current PTY limit: {current_limit}")

            # Increase the limit
            engines.dut.run_cmd(f"echo {max_ptys} | sudo tee /proc/sys/kernel/pty/max")

            # Verify the new limit
            new_limit = engines.dut.run_cmd("cat /proc/sys/kernel/pty/max").strip()
            logger.info(f"New PTY limit: {new_limit}")

            # Make the change permanent by adding to sysctl.conf
            sysctl_entry = f"kernel.pty.max = {max_ptys}"

            # Read file content using sudo cat
            current_content = engines.dut.run_cmd("sudo cat /etc/sysctl.conf")

            if sysctl_entry not in current_content:
                engines.dut.run_cmd(f'echo "{sysctl_entry}" | sudo tee -a /etc/sysctl.conf')
                engines.dut.run_cmd("sudo sysctl -p")

            return True
        except Exception as e:
            logger.error(f"Error increasing PTY limit: {str(e)}")
            return False


# =============================================================================
# Log Management Functions
# =============================================================================

def rotate_logs(engines: EnginesT):
    """
    Reset/rotate the logs on the DUT.

    This function triggers log rotation on the device, which is useful
    for ensuring clean log state before running verification steps.

    Args:
        engines: The test engines object containing the DUT connection

    Returns:
        Result object from the log rotation operation

    Example:
        >>> rotate_logs(engines)
        >>> # Now logs are clean for verification
    """
    with allure.step('Rotate logs'):
        system = _get_system()
        result = system.log.rotate_logs()
        return result


# =============================================================================
# Service Management Functions
# =============================================================================

def run_nginx(engines: EnginesT) -> None:
    """
    Verify and start the nginx service on the DUT if not running.

    This is needed for REST API/OpenAPI testing that requires nginx.

    Args:
        engines: The test engines object containing the DUT connection

    Example:
        >>> run_nginx(engines)
        >>> # nginx is now running and ready for API requests
    """
    with allure.step('Verify Nginx runs before test'):
        nginx_status = engines.dut.run_cmd('systemctl status nginx')
        if 'active (running)' not in nginx_status:
            engines.dut.run_cmd('sudo systemctl start nginx')
            logger.info("Started nginx service")
        else:
            logger.info("Nginx already running")


# =============================================================================
# SSH Key Management Functions
# =============================================================================

def add_ssh_key_to_localhost(engines: EnginesT, username: str) -> bool:
    """
    Generate an SSH key pair on the DUT and copy it to localhost for passwordless authentication.

    This function:
    1. Generates an SSH key pair on the DUT if it doesn't exist
    2. Copies the public key to localhost for the specified user
    3. Sets appropriate permissions on the SSH directory and files

    Args:
        engines: The test engines object containing the DUT connection information
        username: Username to add the SSH key for

    Returns:
        True if successful, False otherwise

    Example:
        >>> success = add_ssh_key_to_localhost(engines, "admin")
        >>> if success:
        ...     print("SSH key added successfully")
    """
    with allure.step(f'Add SSH key for user "{username}" to localhost'):
        try:
            # Check if .ssh directory exists, create if not
            engines.dut.run_cmd(f'sudo mkdir -p /home/{username}/.ssh')
            engines.dut.run_cmd(f'sudo chown {username}:{username} /home/{username}/.ssh')
            engines.dut.run_cmd(f'sudo chmod 700 /home/{username}/.ssh')

            # Check if key already exists
            key_exists = engines.dut.run_cmd(
                f'test -f /home/{username}/.ssh/id_rsa && echo "exists" || echo "not exists"'
            )

            if 'exists' not in key_exists:
                # Generate SSH key pair
                engines.dut.run_cmd(
                    f'sudo -u {username} ssh-keygen -t rsa -b 2048 -f /home/{username}/.ssh/id_rsa -N ""'
                )
                engines.dut.run_cmd(f'sudo chown {username}:{username} /home/{username}/.ssh/id_rsa*')
                engines.dut.run_cmd(f'sudo chmod 600 /home/{username}/.ssh/id_rsa')
                engines.dut.run_cmd(f'sudo chmod 644 /home/{username}/.ssh/id_rsa.pub')

            # Get the public key
            public_key = engines.dut.run_cmd(f'cat /home/{username}/.ssh/id_rsa.pub')

            # Add the key to localhost's authorized_keys (root)
            engines.dut.run_cmd('sudo mkdir -p /root/.ssh')
            engines.dut.run_cmd('sudo touch /root/.ssh/authorized_keys')
            engines.dut.run_cmd('sudo chmod 700 /root/.ssh')
            engines.dut.run_cmd('sudo chmod 600 /root/.ssh/authorized_keys')

            # Check if key already in authorized_keys to avoid duplicates
            key_check = engines.dut.run_cmd(
                f'grep -F "{public_key.strip()}" /root/.ssh/authorized_keys || echo "not found"'
            )

            if 'not found' in key_check:
                engines.dut.run_cmd(f'echo "{public_key}" | sudo tee -a /root/.ssh/authorized_keys')

            # Also add to the user's own authorized_keys for self-connection
            engines.dut.run_cmd(f'sudo touch /home/{username}/.ssh/authorized_keys')
            engines.dut.run_cmd(f'sudo chown {username}:{username} /home/{username}/.ssh/authorized_keys')
            engines.dut.run_cmd(f'sudo chmod 600 /home/{username}/.ssh/authorized_keys')

            key_check = engines.dut.run_cmd(
                f'grep -F "{public_key.strip()}" /home/{username}/.ssh/authorized_keys || echo "not found"'
            )

            if 'not found' in key_check:
                engines.dut.run_cmd(f'echo "{public_key}" | sudo tee -a /home/{username}/.ssh/authorized_keys')

            logger.info(f"SSH key for user {username} successfully added to localhost")
            return True

        except Exception as e:
            logger.error(f"Failed to add SSH key for user {username}: {str(e)}")
            return False


# =============================================================================
# SSH Configuration Functions
# =============================================================================

def change_ssh_limits(
    engines: EnginesT,
    max_sessions: int = 100,
    max_unauthenticated: int = 500,
    throttle_percent: int = 30,
    throttle_start: int = 500,
    additional_ports: list = None,
) -> None:
    """
    Change the SSH session limits in the sshd_config.

    Args:
        engines: The test engines object
        max_sessions: Maximum sessions per connection (default: 100)
        max_unauthenticated: Maximum unauthenticated session count (default: 500)
        throttle_percent: Throttle percent for unauthenticated (default: 30)
        throttle_start: Throttle start for unauthenticated (default: 500)
        additional_ports: List of additional ports to add (default: None)

    Example:
        >>> change_ssh_limits(engines, additional_ports=[40, 41])
    """
    with allure.step('Modifying SSH session limits'):
        # Add ACL rules for additional ports
        if additional_ports:
            for idx, port in enumerate(additional_ports, start=6):
                add_ssh_port_acl(engines, port, str(idx))

        system = _get_system()
        system.ssh_server.set("max-sessions-per-connection", max_sessions)
        system.ssh_server.set("max-unauthenticated", f'"session-count" {max_unauthenticated}')
        system.ssh_server.set("max-unauthenticated", f'"throttle-percent" {throttle_percent}')
        system.ssh_server.set("max-unauthenticated", f'"throttle-start" {throttle_start}')

        # Build port string
        ports = "22"
        if additional_ports:
            ports = ",".join(["22"] + [str(p) for p in additional_ports])

        system.ssh_server.set("port", ports, apply=True, ask_for_confirmation='-y')


def add_ssh_port_acl(engines: EnginesT, port: int, rule_id: str) -> None:
    """
    Add an ACL rule to allow traffic on a new SSH port.

    Args:
        engines: The test engines object
        port: The port to add to the ACL
        rule_id: The rule ID for the ACL entry

    Example:
        >>> add_ssh_port_acl(engines, 40, '6')
    """
    with allure.step(f'Add SSH port {port} to ACL'):
        acl = Acl()
        acl_rule = acl.acl_id["acl-default-whitelist"].rule.rule_id[rule_id]
        acl_rule.match.ip.set_protocol(AclConsts.TCP)
        acl_rule.match.ip.tcp.set("dest-port", port)
        acl_rule.match.ip.set("connection-state", "new")
        acl_rule.match.ip.set("connection-state", "established")
        acl_rule.action.set('permit', apply=True)

# =============================================================================
# VRF Configuration Functions
# =============================================================================


def configure_non_default_vrf(
    engines: EnginesT,
    vrf_name: str = 'RED',
    interface: str = 'swp1',
    ip_address: str = '192.168.100.1/24',
) -> str:
    """
    Configure a non-default VRF for testing multi-VRF connections.

    Args:
        engines: Test engines
        vrf_name: Name of the VRF to create
        interface: Interface to add to the VRF
        ip_address: IP address to assign to the interface

    Returns:
        str: The VRF name for use with ip vrf exec commands
    """
    with allure.step(f'Configure non-default VRF "{vrf_name}"'):
        # Create VRF using NVUE CLI commands
        # Step 1: Create the VRF
        engines.dut.run_cmd(f"nv set vrf {vrf_name}")
        logger.info(f"Created VRF {vrf_name}")

        # Step 2: Set interface type to swp (switch port)
        engines.dut.run_cmd(f"nv set interface {interface} type swp")

        # Step 3: Assign interface to VRF (correct syntax: NOT under 'ip')
        engines.dut.run_cmd(f"nv set interface {interface} vrf {vrf_name}")
        logger.info(f"Assigned interface {interface} to VRF {vrf_name}")

        # Step 4: Set IP address on the interface
        engines.dut.run_cmd(f"nv set interface {interface} ip address {ip_address}")
        logger.info(f"Set IP address {ip_address} on interface {interface}")

        # Step 5: Apply the NVUE configuration
        NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')
        logger.info("Applied VRF configuration")

        # Step 5b: Save the configuration to persist across reboots
        # This is important because FIPS enable triggers a reboot
        engines.dut.run_cmd("nv config save -y", validate=False)
        logger.info("Saved VRF configuration to persist across reboots")

        # Step 6: Verify VRF configuration
        vrf_show_output = engines.dut.run_cmd(f"nv show vrf {vrf_name}", validate=False)
        logger.info(f"VRF {vrf_name} configuration:\n{vrf_show_output}")

        logger.info(f"Configured VRF {vrf_name} on {interface} with IP {ip_address}")
        return vrf_name


# =============================================================================
# gNMI Configuration Functions
# =============================================================================

def ensure_gnmic_installed() -> None:
    """
    Ensure gnmic is installed on the local system (test runner).
    """
    with allure.step('Ensure gnmic is installed'):
        # Check if gnmic is installed
        try:
            # Try to run gnmic version
            subprocess.run("gnmic version", shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            logger.info("gnmic is already installed")
            return
        except Exception:
            logger.info("gnmic not found or not working, attempting installation...")

        try:
            logger.info("Installing gnmic using installation script...")
            # Use curl to install
            # We pipe to sudo bash to ensure we have permissions to write to /usr/local/bin
            install_cmd = "curl -sL https://get-gnmic.openconfig.net | sudo bash"
            subprocess.run(install_cmd, shell=True, check=True)

            logger.info("gnmic installed successfully")
        except Exception as e:
            logger.warning(f"Failed to install gnmic via script: {e}. Trying alternative method...")
            try:
                # Alternative method: download binary directly
                logger.info("Downloading gnmic binary...")
                subprocess.run("wget https://github.com/karimra/gnmic/releases/latest/download/gnmic-linux-amd64.tar.gz", shell=True, check=True)
                subprocess.run("tar -xzf gnmic-linux-amd64.tar.gz", shell=True, check=True)
                subprocess.run("sudo mv gnmic /usr/local/bin/", shell=True, check=True)
                subprocess.run("rm gnmic-linux-amd64.tar.gz", shell=True, check=False)
                logger.info("gnmic installed successfully via binary download")
            except Exception as e2:
                logger.error(f"Failed to install gnmic: {e2}")
                raise Exception("Could not install gnmic required for test12")


def get_gnmi_subscription_count(engines: EnginesT) -> int:
    """
    Get the number of active gNMI subscriptions.

    Args:
        engines: Test engines

    Returns:
        int: Number of active subscriptions
    """
    with allure.step('Get gNMI subscription count'):
        system = _get_system()
        try:
            # Use system object to show status
            # This executes 'nv show system gnmi-server status' (plus json format by default in NvueGeneralCli/BaseComponent)
            output = system.gnmi_server.show("status", dut_engine=engines.dut)
            logger.info(f"gNMI server status output: {output}")
            # Parse JSON output
            status = OutputParsingTool.parse_json_str_to_dictionary(output).get_returned_value()
            return status.get('total-active-subscriptions', 0)

        except Exception as e:
            logger.warning(f"Failed to get gNMI server status via object: {e}")
            # Fallback to SS command if NVUE fails or output is invalid
            tcp_output = engines.dut.run_cmd("ss -tn 'sport = :9339' | grep ESTAB || true")
            return len([line for line in tcp_output.strip().split('\n') if 'ESTAB' in line]) if tcp_output.strip() else 0


def verify_gnmi_connections_active(engines: EnginesT, expected_subscriptions: int = 1) -> int:
    """
    Verify that gNMI subscriptions are active using 'nv show system gnmi-server status'.
    """
    with allure.step(f'Verify gNMI subscriptions are active (expected: {expected_subscriptions})'):
        active_subscriptions = get_gnmi_subscription_count(engines)
        logger.info(f"Found {active_subscriptions} active gNMI subscriptions")

        assert active_subscriptions >= expected_subscriptions, \
            f"Expected at least {expected_subscriptions} gNMI subscriptions, but found {active_subscriptions}"

        return active_subscriptions


def verify_gnmi_connections_closed(engines: EnginesT) -> None:
    """
    Verify that gNMI subscriptions are closed using 'nv show system gnmi-server status'.
    """
    with allure.step('Verify gNMI subscriptions are closed'):
        active_subscriptions = get_gnmi_subscription_count(engines)
        logger.info(f"Found {active_subscriptions} active gNMI subscriptions")

        assert active_subscriptions == 0, \
            f"Expected no gNMI subscriptions, but found {active_subscriptions} active subscriptions"


def enable_gnmi_server_with_cert(engines: EnginesT, cert_name: str = "gnmi-server-cert") -> str:
    """
    Enable GNMI server with a self-signed certificate and nginx authenticator.

    Args:
        engines: Test engines
        cert_name: Name for the certificate

    Returns:
        str: The certificate name used
    """
    with allure.step(f'Enable GNMI server with certificate "{cert_name}"'):
        # gNMI requires TLS certificates - username/password alone won't work
        # We need to import or generate a certificate first
        system = _get_system()

        logger.info("Setting up self-signed certificate for gNMI server")

        # Create self-signed certificate using OpenSSL on the DUT
        # Use user home directory and SCP to avoid file permission issues with nv action import
        # which can fail with file:// scheme due to PrivateTmp or restricted permissions
        username = engines.dut.username
        password = engines.dut.password
        cert_path = f"/home/{username}/gnmi-cert.pem"
        key_path = f"/home/{username}/gnmi-key.pem"

        # Clean up any previous files
        engines.dut.run_cmd(f"rm -f {cert_path} {key_path}")

        # Delete any existing certificate with the same name before importing
        # This prevents "certificate already exists" errors
        engines.dut.run_cmd(f"sudo nv action delete system security certificate {cert_name}", validate=False)

        engines.dut.run_cmd(
            f"openssl req -x509 -newkey rsa:4096 -keyout {key_path} "
            f"-out {cert_path} -days 365 -nodes "
            f"-subj '/CN={engines.dut.ip}/O=Test/C=US'"
        )

        # Ensure gNMI authentication service is running on port 54321
        # This is required because gNMI Envoy proxy sends auth requests to localhost:54321
        with allure.step('Configure Nginx authentication for gNMI'):
            gnmi_auth_config = '''server {
  listen localhost:54321;
  server_tokens off;
  auth_pam               "nvueapi";
  auth_pam_service_name  "nvueapi";
  location /authenticate {
    allow ::1;
    allow 127.0.0.1;
    deny  all;
    add_header Content-Length 0;
    return 204 "";
  }
}'''
            engines.dut.run_cmd(f"echo '{gnmi_auth_config}' | sudo tee /etc/nginx/auth/nginx_auth_gnmi.conf > /dev/null")
            engines.dut.run_cmd("sudo systemctl restart nginx-authenticator")
            time.sleep(2)  # Wait for nginx to restart
            logger.info("gNMI authentication service configured on port 54321")

        # Import the certificate into NVUE using SCP (pointing to localhost)
        # This bypasses filesystem permission issues seen with file://
        with allure.step('Import certificate to NVUE'):
            import_cmd = (
                f"sudo nv action import system security certificate {cert_name} "
                f"uri-private-key 'scp://{username}:{password}@127.0.0.1:{key_path}' "
                f"uri-public-key 'scp://{username}:{password}@127.0.0.1:{cert_path}'"
            )
            engines.dut.run_cmd(import_cmd)

            # Clean up cert files
            engines.dut.run_cmd(f"rm -f {cert_path} {key_path}")

        # Bind certificate to gNMI server
        with allure.step('Enable gNMI server in NVUE'):
            system.gnmi_server.set('certificate', cert_name, dut_engine=engines.dut, apply=False)
            system.gnmi_server.set('listening-address', '0.0.0.0', dut_engine=engines.dut, apply=False)
            system.gnmi_server.set('port', 9339, dut_engine=engines.dut, apply=False)
            system.gnmi_server.set('state', 'enabled', dut_engine=engines.dut, apply=False)

            NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')

        # Wait for GNMI server to start
        time.sleep(5)

        # Verify gNMI server is listening
        gnmi_status = engines.dut.run_cmd("ss -tlnp | grep 9339")
        logger.info(f"GNMI server listening on port 9339: {gnmi_status}")

        logger.info("GNMI server enabled with certificate")
        return cert_name


def create_gnmi_client(engines: EnginesT, user: str, password: str) -> GnmiClient:
    """
    Create a GNMI client instance.

    Args:
        engines: Test engines
        user: Username for GNMI connection
        password: Password for GNMI connection

    Returns:
        GnmiClient: The initialized GNMI client
    """
    with allure.step(f'Create GNMI client for user "{user}"'):
        gnmi_client = GnmiClient(
            engines.dut.ip,
            GnmiConsts.GNMI_DEFAULT_PORT,
            user,
            password,
            cmd_time=30  # Longer timeout for streaming
        )
        return gnmi_client


def create_gnmi_subscription(
    gnmi_client: GnmiClient,
    user: str,
    streaming: bool = True,
    path: str = '/',
    prefix: str = '',
) -> subprocess.Popen:
    """
    Create a GNMI subscription using an existing client.

    Args:
        gnmi_client: The GnmiClient instance
        user: Username (for logging)
        streaming: Whether to use streaming mode (default: True)
        path: Path to subscribe to (default: '/')
        prefix: Prefix for subscription (default: '')

    Returns:
        subprocess.Popen: The subscription process
    """
    mode_str = "streaming" if streaming else "once"
    with allure.step(f'Start GNMI subscription ({mode_str} mode) for user "{user}"'):
        mode = GnmiMode.STREAM if streaming else GnmiMode.ONCE

        # Start GNMI subscription
        # With skip_cert_verify=True, the client will skip certificate validation
        # but still use TLS encryption and username/password authentication
        _, _, _, gnmi_subscription_process = gnmi_client.gnmic_subscribe(
            prefix=prefix,
            path=path,
            mode=mode,
            flat=True,
            skip_cert_verify=True,
            keep_session_alive=streaming
        )

        # Give subscription time to establish
        time.sleep(10)  # Increased wait time for connection to establish

        # Check if the gnmic process is still running
        if gnmi_subscription_process.poll() is not None:
            # Process terminated - get error output
            stdout, stderr = gnmi_subscription_process.communicate()
            logger.error(f"gNMI subscription failed to start. stdout: {stdout}, stderr: {stderr}")
            raise Exception("gNMI subscription process terminated immediately. Check gNMI server configuration.")

        logger.info(f"GNMI subscription started for user {user}")
        return gnmi_subscription_process


def create_gnmi_subscription_session(
    engines: EnginesT,
    user: str,
    password: str,
    streaming: bool = True,
) -> tuple[GnmiClient, subprocess.Popen]:
    """
    Create a GNMI subscription session (Client + Subscription).
    Wrapper that uses create_gnmi_client and create_gnmi_subscription.

    Args:
        engines: Test engines
        user: Username for GNMI connection
        password: Password for GNMI connection
        streaming: Whether to use streaming mode (default: True)

    Returns:
        tuple: (gnmi_client, gnmi_subscription_process)
    """
    with allure.step('Create GNMI subscription session'):
        gnmi_client = create_gnmi_client(engines, user, password)
        gnmi_subscription_process = create_gnmi_subscription(gnmi_client, user, streaming=streaming)
        return gnmi_client, gnmi_subscription_process


def disable_gnmi_server_and_cleanup(engines: EnginesT, cert_name: str = "gnmi-server-cert") -> None:
    """
    Disable GNMI server and cleanup certificates.

    Args:
        engines: Test engines
        cert_name: Name of the certificate to clean up
    """
    with allure.step('Cleanup: Disable GNMI server and delete certificate'):
        try:
            system = _get_system()
            system.gnmi_server.set('state', 'disabled', dut_engine=engines.dut, apply=False)
            system.gnmi_server.unset('certificate', dut_engine=engines.dut, apply=False).ignore_result()
            NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')

            # Delete the certificate after disabling the gNMI server
            engines.dut.run_cmd(f"sudo nv action delete system security certificate {cert_name}", validate=False)

            # Remove the gNMI auth nginx config we added
            engines.dut.run_cmd("sudo rm -f /etc/nginx/auth/nginx_auth_gnmi.conf", validate=False)
            engines.dut.run_cmd("sudo systemctl restart nginx-authenticator", validate=False)
            logger.info("GNMI server disabled and certificate deleted")
        except Exception as e:
            logger.debug(f"Cleanup failed: {e}")
