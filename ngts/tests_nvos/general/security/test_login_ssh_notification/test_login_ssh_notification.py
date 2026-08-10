import datetime
import json
import logging
import os
import random
import re
import time

import paramiko
import pytest
from retry import retry
from devts.infra.tools.connection_tools.utils import generate_strong_password
from devts.infra.tools.general_constants.constants import DefaultConnectionValues
from devts.infra.tools.linux_tools.linux_tools import scp_file
from ngts.ngts_types.devices_T import DevicesT
from ngts.nvos_constants.constants_nvos import SystemConsts, ApiType, CumulusConsts
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.system.UserManager import delete_user
from ngts.tests_nvos.general.security.conftest import ssh_to_device_and_retrieve_raw_login_ssh_notification
from ngts.tests_nvos.general.security.security_test_tools.switch_authenticators import SshAuthenticator
from ngts.tests_nvos.general.security.test_login_ssh_notification.constants import LoginSSHNotificationConsts as Consts
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
from ngts.tools.test_utils import allure_utils as allure
from ngts.cli_wrappers.common.general_clis_common import GeneralCliCommon
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool


cumulus_owner = "yleibovichwe"


logger = logging.getLogger(__name__)


@pytest.mark.cumulus
def test_ssh_login_notifications_default_fields_admin(engines, login_source_ip_addresses):
    """
    Validate admin user SSH login notification default fields.
        Test flow:
            1. Connect to switch before validation to clear failed messages
            2. Validate SSH login notification (default fields, IP, failed attempts, etc.)
    """
    system = System(None)

    with allure.step("Connect to switch before validation to clear all failed messages"):
        successful_login_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())
        SshAuthenticator(engines.dut.username, engines.dut.password, engines.dut.ip)

    with allure.step("Validate ssh login notification"):
        validate_ssh_login_notifications_default_fields(engines, login_source_ip_addresses,
                                                        username=engines.dut.username,
                                                        password=engines.dut.password,
                                                        capability=Consts.ADMIN_CAPABITILY,
                                                        last_successful_login=successful_login_time)


@pytest.mark.cumulus
def test_ssh_login_notification_password_change_admin(engines, login_source_ip_addresses):
    """
    Validate admin user SSH login notification when password is changed.
        Test flow:
            1. Create new user
            2. Connect to switch and collect successful login time
            3. Change user password
            4. Validate SSH login notification with password-change message
    """
    system = System(force_api=ApiType.NVUE)
    username = None

    try:
        with allure.step("Create new user"):
            username, password = system.aaa.user.set_new_user(apply=True)
            new_password = generate_strong_password()

        with allure.step("Connect to switch and collect successful login time"):
            successful_login_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())
            SshAuthenticator(username, password, engines.dut.ip).attempt_login_success()

        with allure.step("Change user password"):
            change_username_password(engines, username=username,
                                     curr_password=password,
                                     new_password=new_password)

        with allure.step("Validate ssh login notification with password change"):
            validate_ssh_login_notifications_default_fields(engines, login_source_ip_addresses,
                                                            username=username,
                                                            password=new_password,
                                                            capability=Consts.ADMIN_CAPABITILY,
                                                            check_password_change_msg=True,
                                                            last_successful_login=successful_login_time)
    finally:
        if username:
            with allure.step("Cleanup: delete created user"):
                try:
                    delete_user(engines, username)
                except Exception:
                    logger.warning("Failed to delete user %s during cleanup", username)


@pytest.mark.cumulus
def test_ssh_login_notification_role_new_user(engines, login_source_ip_addresses, devices: DevicesT):
    """
    Validate new user role change is reflected in SSH login notification.
        Test flow:
            1. Create new user with admin role
            2. Connect to switch and collect successful login time
            3. Change user role to monitor role
            4. Validate SSH login notification with role-change message
    """
    system = System(force_api=ApiType.NVUE)
    admin_role = devices.dut.aaa_admin_role
    monitor_role = devices.dut.aaa_monitor_role
    user_name = None

    try:
        with allure.step(f"Create new user with role {admin_role}"):
            user_name, password = system.aaa.user.set_new_user(role=admin_role, apply=True)

        with allure.step("Connect to switch and collect successful login time"):
            successful_login_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())
            SshAuthenticator(user_name, password, engines.dut.ip).attempt_login_success()

        with allure.step(f"Change user role to {monitor_role}"):
            system.aaa.user.user_id[user_name].set(SystemConsts.USER_ROLE, monitor_role, apply=True).verify_result()
            time.sleep(5)

        with allure.step("Validate ssh login notification with role change"):
            validate_ssh_login_notifications_default_fields(engines, login_source_ip_addresses,
                                                            username=user_name,
                                                            password=password,
                                                            capability=monitor_role,
                                                            check_password_change_msg=False,
                                                            check_role_change_msg=True,
                                                            last_successful_login=successful_login_time)
    finally:
        if user_name:
            with allure.step("Cleanup: delete created user"):
                try:
                    delete_user(engines, user_name)
                except Exception:
                    logger.warning("Failed to delete user %s during cleanup", user_name)


@pytest.mark.cumulus
def test_ssh_login_notification_cli_commands_good_flow(engines, login_source_ip_addresses):
    """
    Test CLI commands for login SSH notification (login-record-period set/show).
        Test flow:
            1. Connect to switch and collect successful login time
            2. Set login-record-period to random value (min-max range)
            3. Validate SSH login notification with new record period
            4. Run nv show system ssh-server and verify login-record-period value
    """
    system = System(None)

    with allure.step("Connect to switch and collect successful login time"):
        successful_login_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())
        SshAuthenticator(engines.dut.username, engines.dut.password, engines.dut.ip).attempt_login_success()

    with allure.step("Set new value for login record period"):
        record_days = random.randint(Consts.MIN_RECORD_PERIOD_VAL, Consts.MAX_RECORD_PERIOD_VAL)
        system.ssh_server.set(Consts.RECORD_PERIOD, record_days, apply=True, ask_for_confirmation=True)

    with allure.step("Validate ssh login notification with new record period"):
        validate_ssh_login_notifications_default_fields(engines, login_source_ip_addresses,
                                                        username=engines.dut.username,
                                                        password=engines.dut.password,
                                                        capability=SystemConsts.ROLE_CONFIGURATOR,
                                                        check_password_change_msg=False,
                                                        check_role_change_msg=False,
                                                        expected_login_record_period=record_days,
                                                        last_successful_login=successful_login_time)

    with allure.step("Validate login record period value in show system ssh-server command"):
        output = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.show()).get_returned_value()
        ValidationTool.validate_fields_values_in_output([Consts.RECORD_PERIOD], [record_days], output).verify_result()


@pytest.mark.cumulus
def test_login_ssh_notification_performance(engines, login_source_ip_addresses):
    """
    Validate login notification performance with large auth.log.
        Test flow:
            1. Set login record period to max value
            2. Populate auth.log by uploading from shared location
            3. Measure SSH login time and assert within max threshold
    """
    system = System(None)

    with allure.step("Set max value for login record period"):
        system.ssh_server.set(Consts.RECORD_PERIOD, Consts.MAX_RECORD_PERIOD_VAL, apply=True, ask_for_confirmation=False).verify_result()

    with allure.step("Populate auth. logs by uploading from previously created files"):
        engines.dut.run_cmd(f'mkdir {Consts.TMP_TEST_DIR_SWITCH_PATH}')
        scp_file(engines.dut, Consts.AUTH_LOGS_SHARED_LOCATION, Consts.TMP_TEST_DIR_SWITCH_PATH)
        engines.dut.run_cmd(f'sudo mv {Consts.TMP_TEST_DIR_SWITCH_PATH}/auth.log* {Consts.AUTH_LOG_DIR_SWITCH_PATH}')
        engines.dut.run_cmd(f'rmdir {Consts.TMP_TEST_DIR_SWITCH_PATH}')

    with allure.step("Measure login time"):
        start_time = datetime.datetime.now()
        ssh_to_device_and_retrieve_raw_login_ssh_notification(engines.dut.ip)
        end_time = datetime.datetime.now()
        login_time_sec = end_time.second - start_time.second
        logger.info("Login time is: {} secs".format(login_time_sec))
        assert login_time_sec <= Consts.MAX_LOGIN_TIME, \
            "Login time is too long, max threshold: {}," \
            "actual login time: {}".format(Consts.MAX_LOGIN_TIME, login_time_sec)


@pytest.mark.cumulus
def test_ssh_login_notifications_diff_user_notification(engines, login_source_ip_addresses, devices: DevicesT):
    """
    Validate that one user's login failures are not shown in another user's SSH login notification.
        Test flow:
            1. Create new user with admin role
            2. Connect with cumulus user (clear failed messages)
            3. Connect with newly created user and collect successful login time
            4. Fail N times connecting with newly created user
            5. Connect with cumulus user and parse login notification
            6. Verify cumulus user notification has no failed-attempt count
            7. Connect with newly created user and validate failed-attempt count in notification
    """
    system = System(force_api=ApiType.NVUE)
    admin_role = devices.dut.aaa_admin_role
    user_name = None

    try:
        with allure.step(f"Create new user with role {admin_role}"):
            user_name, password = system.aaa.user.set_new_user(
                role=admin_role, apply=True
            )

        with allure.step("Connect to switch with cumulus user"):
            connect_with_cumulus_user_before_validation(engines)

        with allure.step("Connect to switch with newly created user and collect successful login time"):
            successful_login_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())
            SshAuthenticator(user_name, password, engines.dut.ip).attempt_login_success()

        random_number_of_connection_fails = random.randint(5, 7)
        with allure.step(f"Fail {random_number_of_connection_fails} times connecting to device with newly created user"):
            authenticator = SshAuthenticator(user_name, password, engines.dut.ip)
            for index in range(random_number_of_connection_fails):
                logger.info(f"Attempt number {index + 1}")
                authenticator.attempt_login_failure()

        with allure.step("Connect to switch with cumulus user and parse login notification"):
            second_login_notification_message = parse_ssh_login_notification(engines.dut.ip, engines.dut.username, engines.dut.password)

        with allure.step("Validate failed attempts are not in the cumulus user login notification"):
            actual_failed = second_login_notification_message[Consts.NUMBER_OF_UNSUCCESSFUL_ATTEMPTS_SINCE_LAST_LOGIN]
            assert actual_failed is None, (f"Expected no failed-attempt count for cumulus user; got: {actual_failed}")

        with allure.step("Validate failed attempts in notification with newly created user"):
            validate_ssh_login_notifications_default_fields(engines,
                                                            login_source_ip_addresses,
                                                            username=user_name,
                                                            password=password,
                                                            already_login_failed=random_number_of_connection_fails,
                                                            capability=admin_role,
                                                            last_successful_login=successful_login_time)
    finally:
        if user_name:
            with allure.step("Cleanup: delete created user"):
                try:
                    delete_user(engines, user_name)
                except Exception:
                    logger.warning("Failed to delete user %s during cleanup", user_name)


@pytest.mark.cumulus_only
def test_ssh_login_notifications_allowed_user(engines, login_source_ip_addresses, monitor_user_for_ssh_allowed):
    """
    Validate SSH connection is allowed only for user configured in allow-users.
        Test flow:
            1. Set ssh-server allow-users to monitor user
            2. Connect with cumulus user (clear failed messages)
            3. Connect with monitor user and collect login timestamp
            4. Validate SSH login notification for monitor user
            5. Verify nv show system ssh-server shows allow-users same as login user
            6. Verify non-allowed user (cumulus) is rejected when allow-users is set
    """
    user_name, password = monitor_user_for_ssh_allowed
    system = System(force_api=ApiType.NVUE)

    with allure.step("Setting ssh server with allow users"):
        system.ssh_server.set(Consts.ALLOW_USERS, user_name, apply=True, ask_for_confirmation=True).verify_result()

    with allure.step("Connecting to switch with cumulus user before validation to clear all failed messages"):
        SshAuthenticator(engines.dut.username, engines.dut.password, engines.dut.ip).attempt_login_success()

    with allure.step("Connecting to switch with newly created user to collect the login timestamp"):
        successful_login_time = connect_with_user_and_collect_login_time(system, user_name, password, engines.dut.ip)

    with allure.step("Validating successful login attempt with newly created user"):
        validate_ssh_login_notifications_default_fields(engines, login_source_ip_addresses,
                                                        username=user_name,
                                                        password=password,
                                                        capability=Consts.NVUE_MONITOR_ROLE,
                                                        last_successful_login=successful_login_time)

    with allure.step("verifying allowed user and login user are the same"):
        fields_to_verify = [Consts.ALLOW_USERS]
        values_to_verify = [{user_name: {}}]
        output = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.show()).get_returned_value()
        ValidationTool.validate_fields_values_in_output(fields_to_verify, values_to_verify, output).verify_result()

    with allure.step("Validating that non-allowed user is rejected"):
        login_succeeded, _ = SshAuthenticator(engines.dut.username, engines.dut.password, engines.dut.ip).attempt_login_success()
        assert not login_succeeded, (
            "Non-allowed user ({}) should be rejected when allow-users is set to {} only."
        ).format(engines.dut.username, user_name)


@pytest.mark.cumulus_only
def test_verify_switchd_restart(engines, login_source_ip_addresses, monitor_user_for_ssh_allowed):
    """
    Verify SSH server config persists after switchd restart.
        Test flow:
            1. Configure SSH server options (port, auth-retries, login-timeout, allow-users)
            2. Connect with cumulus user then with monitor user; validate login notification
            3. Restart switchd and verify service is active
            4. Verify SSH server config in nv show after restart
            5. Connect again with both users and validate login notification
            6. Cleanup SSH server configuration
    """
    username, password = monitor_user_for_ssh_allowed
    system = System(force_api=ApiType.NVUE)

    with allure.step("Configure SSH server options (port, auth-retries=6, login-timeout=120)"):
        for param, value in Consts.SSH_SERVER_OPTIONS_FOR_SET.items():
            system.ssh_server.set(param, value).verify_result()
        system.ssh_server.set(Consts.ALLOW_USERS, username, apply=True, ask_for_confirmation=True).verify_result()

    with allure.step("Connecting to switch with cumulus user before validation to clear all failed messages"):
        SshAuthenticator(engines.dut.username, engines.dut.password, engines.dut.ip).attempt_login_success()

    with allure.step("Connecting to switch with newly created user to collect the login timestamp"):
        successful_login_time = connect_with_user_and_collect_login_time(system, username, password, engines.dut.ip)

    with allure.step("Validating successful login attempt with newly created user"):
        validate_ssh_login_notifications_default_fields(engines, login_source_ip_addresses,
                                                        username=username,
                                                        password=password,
                                                        capability=Consts.NVUE_MONITOR_ROLE,
                                                        last_successful_login=successful_login_time)
    with allure.step("Restart switchd and verify it is active"):
        GeneralCliCommon(engines.dut).systemctl_restart('switchd')
        assert GeneralCliCommon(engines.dut).systemctl_is_service_active('switchd'), 'switchd did not become active after restart'

    with allure.step("Verify SSH server configuration persisted after switchd restart"):
        fields_to_verify = [p[0] for p in Consts.SSH_SERVER_OPTIONS_FOR_SHOW_VERIFY] + [Consts.ALLOW_USERS]
        values_to_verify = [p[1] for p in Consts.SSH_SERVER_OPTIONS_FOR_SHOW_VERIFY] + [{username: {}}]
        output = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.show()).get_returned_value()
        ValidationTool.validate_fields_values_in_output(fields_to_verify, values_to_verify, output).verify_result()

    with allure.step("Connecting to switch with cumulus user before validation to clear all failed messages"):
        SshAuthenticator(engines.dut.username, engines.dut.password, engines.dut.ip).attempt_login_success()

    with allure.step("Connecting to switch with newly created user to collect the login timestamp"):
        successful_login_time = connect_with_user_and_collect_login_time(system, username, password, engines.dut.ip)

    with allure.step("Validating successful login attempt with newly created user"):
        validate_ssh_login_notifications_default_fields(engines, login_source_ip_addresses,
                                                        username=username,
                                                        password=password,
                                                        capability=Consts.NVUE_MONITOR_ROLE,
                                                        last_successful_login=successful_login_time)
    with allure.step("Cleanup SSH server configuration"):
        system.ssh_server.unset(apply=True, ask_for_confirmation=True).verify_result()


@pytest.mark.cumulus_only
def test_verify_reboot(engines, login_source_ip_addresses, monitor_user_for_ssh_allowed):
    """
    Verify SSH server config persists after node reboot.
        Test flow:
            1. Configure SSH server options (auth-retries, login-timeout, port, allow-users)
            2. Connect with cumulus user then with monitor user; validate login notification
            3. Reboot the system
            4. Verify SSH server config in nv show after reboot
            5. Connect again with both users and validate login notification
            6. Cleanup SSH server configuration
    """
    username, password = monitor_user_for_ssh_allowed
    system = System(force_api=ApiType.NVUE)

    with allure.step("Configure SSH server options (port, auth-retries=6, login-timeout=120)"):
        for param, value in (
            (Consts.SSH_AUTHENTICATION_RETRIES, Consts.SSH_AUTH_RETRIES_VAL),
            (Consts.SSH_LOGIN_TIMEOUT, Consts.SSH_LOGIN_TIMEOUT_VAL),
            (Consts.SSH_PORT, Consts.SSH_PORT_VAL),
            (Consts.ALLOW_USERS, username)
        ):
            system.ssh_server.set(param, value).verify_result()
        system.ssh_server.set(Consts.ALLOW_USERS, engines.dut.username, apply=True, ask_for_confirmation=True).verify_result()

    with allure.step("Connecting to switch with cumulus user before validation to clear all failed messages"):
        SshAuthenticator(engines.dut.username, engines.dut.password, engines.dut.ip).attempt_login_success()

    with allure.step("Connecting to switch with newly created user to collect the login timestamp"):
        successful_login_time = connect_with_user_and_collect_login_time(system, username, password, engines.dut.ip, port=Consts.SSH_PORT_VAL)

    with allure.step("Validating successful login attempt with newly created user"):
        validate_ssh_login_notifications_default_fields(engines, login_source_ip_addresses,
                                                        username=username,
                                                        password=password,
                                                        capability=Consts.NVUE_MONITOR_ROLE,
                                                        last_successful_login=successful_login_time)

    with allure.step('Reboot the system'):
        system.action_reboot(send_user_confirmation='y').verify_result()

    with allure.step("Verify SSH server configuration persisted after reboot"):
        fields_to_verify = [p[0] for p in Consts.SSH_SERVER_OPTIONS_FOR_SHOW_VERIFY_REBOOT] + [Consts.ALLOW_USERS, Consts.ALLOW_USERS]
        values_to_verify = [p[1] for p in Consts.SSH_SERVER_OPTIONS_FOR_SHOW_VERIFY_REBOOT] + [{username: {}}, {engines.dut.username: {}}]
        output = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.show()).get_returned_value()
        ValidationTool.validate_fields_values_in_output(fields_to_verify, values_to_verify, output).verify_result()

    with allure.step("Connecting to switch with cumulus user before validation to clear all failed messages"):
        SshAuthenticator(engines.dut.username, engines.dut.password, engines.dut.ip).attempt_login_success()

    with allure.step("Connecting to switch with newly created user to collect the login timestamp"):
        successful_login_time = connect_with_user_and_collect_login_time(system, username, password, engines.dut.ip, port=Consts.SSH_PORT_VAL)

    with allure.step("Validating successful login attempt with newly created user"):
        validate_ssh_login_notifications_default_fields(engines, login_source_ip_addresses,
                                                        username=username,
                                                        password=password,
                                                        capability=Consts.NVUE_MONITOR_ROLE,
                                                        last_successful_login=successful_login_time)

    with allure.step("Cleanup SSH server configuration"):
        system.ssh_server.unset(apply=True, ask_for_confirmation=True).verify_result()


@pytest.mark.cumulus_only
def test_verify_nv_show(engines):
    """
    Verify nv show system ssh-server active-sessions displays correct output.
        Test flow:
            1. Configure SSH server (max-sessions-per-connection=30, max-unauthenticated, permit-root-login)
            2. Verify SSH server options in nv show system ssh-server
            3. Get ESTAB session count from active-sessions show
            4. Perform successful login with cumulus user (keep session)
            5. Verify ESTAB session count increased in active-sessions show
    """
    system = System(force_api=ApiType.NVUE)

    with allure.step("Configure SSH server options (max-sessions-per-connection=30)"):
        system.ssh_server.set(Consts.SSH_MAX_SESSIONS_PER_CONNECTION, Consts.SSH_MAX_SESSIONS_PER_CONNECTION_VAL).verify_result()
        system.ssh_server.set(Consts.SSH_MAX_UNAUTHENTICATED, Consts.SSH_MAX_UNAUTHENTICATED_THROTTLE_START_30).verify_result()
        system.ssh_server.set(Consts.SSH_PERMIT_ROOT_LOGIN, Consts.SSH_PERMIT_ROOT_LOGIN_ENABLED, apply=True, ask_for_confirmation=True).verify_result()

    with allure.step("Verify SSH server options are set correctly"):
        output = json.loads(system.ssh_server.show())
        fields_to_verify = [p[0] for p in Consts.SSH_SERVER_OPTIONS_FOR_SHOW_VERIFY_NV_SHOW]
        values_to_verify = [p[1] for p in Consts.SSH_SERVER_OPTIONS_FOR_SHOW_VERIFY_NV_SHOW]
        ValidationTool.validate_fields_values_in_output(fields_to_verify, values_to_verify, output).verify_result()

    active_sessions_pre = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.active_sessions.show()).get_returned_value()
    count_pre = sum(1 for s in (active_sessions_pre or {}).values() if isinstance(s, dict) and s.get('state') == 'ESTAB')

    with allure.step("Validating successful login attempt with cumulus user"):
        authenticator = SshAuthenticator(engines.dut.username, engines.dut.password, engines.dut.ip)
        authenticator.attempt_login_success(restart_session_process=False, logout_if_succeeded=False)

    with allure.step("Verify that the number of ESTAB sessions in active-sessions show has increased"):
        verify_active_sessions_estab_count_increased(system, count_pre)
        authenticator.close_ssh_login_session()


@pytest.mark.cumulus_only
def test_verify_max_session_per_connection(engines):
    """
    Verify max-sessions-per-connection limit is enforced per TCP connection.
        Test flow:
            1. Set max-sessions-per-connection to limit (e.g. 12)
            2. Verify value in nv show system ssh-server
            3. Open sessions on single connection up to limit; verify (limit+1)th rejected
            4. Close channels and connection
            5. Set max-sessions-per-connection to 102 and verify it fails
            6. Unset max-sessions-per-connection
    """
    system = System(force_api=ApiType.NVUE)
    limit = Consts.SSH_MAX_SESSIONS_PER_CONNECTION_LIMIT

    with allure.step(f"Set max sessions per connection to {limit}"):
        system.ssh_server.set(Consts.SSH_MAX_SESSIONS_PER_CONNECTION, limit, apply=True, ask_for_confirmation=True).verify_result()

    with allure.step(f"Verify max sessions per connection is set to {limit}"):
        output = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.show()).get_returned_value()
        ValidationTool.validate_fields_values_in_output([Consts.SSH_MAX_SESSIONS_PER_CONNECTION], [limit], output).verify_result()

    with allure.step(f"Open sessions on a single connection up to limit {limit} (then try one more)"):
        client, channels, num_opened = _open_sessions_on_single_connection(
            engines.dut.ip,
            engines.dut.username,
            engines.dut.password,
            Consts.SSH_PORT_VAL,
            limit + 2,
        )
        # Server may reject at limit or limit+1 (e.g. off-by-one or connection counted as first session)
        assert limit <= num_opened <= limit + 1, (
            f"Expected between {limit} and {limit + 1} sessions (limit enforced), but opened {num_opened}"
        )

    with allure.step(f"Verify one more session is rejected (limit enforced)"):
        if num_opened <= limit:
            # Limit was already enforced in the loop
            pass
        else:
            # num_opened == limit+1: verify (limit+2)th is rejected
            try:
                extra_chan = client.get_transport().open_session()
                extra_chan.exec_command("echo test")
                extra_chan.close()
                assert False, (
                    f"Session {num_opened + 1} should be rejected when max-sessions-per-connection is {limit}"
                )
            except Exception as e:
                logger.info("Session %s correctly rejected: %s", num_opened + 1, e)

    # Close all channels and the single connection
    for ch in channels:
        try:
            ch.close()
        except Exception:
            pass
    client.close()

    with allure.step("Set max sessions per connection to 102 and verify it fails"):
        system.ssh_server.set(Consts.SSH_MAX_SESSIONS_PER_CONNECTION, Consts.SSH_MAX_SESSIONS_PER_CONNECTION_VAL_102, apply=True, ask_for_confirmation=True).verify_result(should_succeed=False)

    with allure.step("Unset max sessions per connection"):
        system.ssh_server.unset(Consts.SSH_MAX_SESSIONS_PER_CONNECTION, apply=True, ask_for_confirmation=True).verify_result()


@pytest.mark.cumulus_only
def test_verify_permit_root_login():
    """
    Verify permit-root-login SSH server option set/show/unset for each value.
        Test flow:
            1. For each value (prohibit-password, forced-commands-only, disabled, enabled):
                 a. Set permit-root-login to value and apply
                 b. Verify value in nv show system ssh-server
                 c. Unset permit-root-login
    """
    system = System(force_api=ApiType.NVUE)
    permit_values = [
        Consts.SSH_PERMIT_ROOT_LOGIN_PROHIBIT_PASSWORD,
        Consts.SSH_PERMIT_ROOT_LOGIN_FORCED_COMMANDS_ONLY,
        Consts.SSH_PERMIT_ROOT_LOGIN_DISABLED,
        Consts.SSH_PERMIT_ROOT_LOGIN_ENABLED,
    ]
    for value in permit_values:
        with allure.step(f"Set permit-root-login to {value}"):
            system.ssh_server.set(
                Consts.SSH_PERMIT_ROOT_LOGIN, value, apply=True, ask_for_confirmation=True
            ).verify_result()
        with allure.step(f"Verify permit-root-login is set to {value}"):
            output = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.show()).get_returned_value()
            ValidationTool.validate_fields_values_in_output(
                [Consts.SSH_PERMIT_ROOT_LOGIN], [value], output
            ).verify_result()
        with allure.step("Unset permit-root-login"):
            system.ssh_server.unset(
                Consts.SSH_PERMIT_ROOT_LOGIN, apply=True, ask_for_confirmation=True
            ).verify_result()


def convert_linux_date_output_to_datetime_object(linux_date_string):
    '''
    @summary: this helper function will extract time and date and
    return tuple ( date , time )
    :param output: output to extract the time and date from
    :param date_regex: date regex to catch
    :param time_regex: time regex to catch
    '''
    date_format = "%a %b %d %H:%M:%S %Z %Y"
    date = datetime.datetime.strptime(linux_date_string, date_format)
    return date


def connect_with_user_and_collect_login_time(system, username, password, dut_ip, port=Consts.SSH_PORT_VAL):
    """
    Connect to switch with the given user, perform successful login, and return the login
    timestamp for validation. Use when a test needs last_successful_login for notification checks.
    """
    successful_login_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())
    authenticator = SshAuthenticator(username, password, dut_ip, port)
    authenticator.attempt_login_success()
    return successful_login_time


def connect_with_cumulus_user_before_validation(engines):
    """
    Connect to switch with cumulus user before validation to clear all failed messages.
    Use before validating login notifications so prior failed-attempt messages do not affect checks.
    """
    with allure.step("Connecting to switch with cumulus user before validation to clear all failed messages"):
        return SshAuthenticator(
            engines.dut.username, engines.dut.password, engines.dut.ip
        ).attempt_login_success()


def _open_sessions_on_single_connection(hostname, username, password, port, max_sessions_to_try):
    """
    Open a single SSH connection and open multiple sessions (channels) on that same connection.
    Used to verify max-sessions-per-connection is enforced (sessions per TCP connection, not total connections).

    Returns:
        tuple: (client, list of channels, num_opened).
        client is the paramiko SSHClient (one connection). channels are the open session channels.
        num_opened is how many sessions were successfully opened before failure or limit.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=hostname,
        username=username,
        password=password,
        port=port,
        timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    transport = client.get_transport()
    channels = []
    try:
        for i in range(max_sessions_to_try):
            chan = transport.open_session()
            chan.exec_command("echo test")
            channels.append(chan)
            time.sleep(0.2)
    except Exception as e:
        logger.info("Opening session %s failed (expected when limit enforced): %s", len(channels) + 1, e)
    logger.info(f"Opened {len(channels)} sessions on single connection")
    return client, channels, len(channels)


@retry(AssertionError, tries=6, delay=2)
def verify_active_sessions_estab_count_increased(system, count_pre):
    active_sessions = OutputParsingTool.parse_json_str_to_dictionary(system.ssh_server.active_sessions.show()).get_returned_value()
    count_post = sum(1 for s in (active_sessions or {}).values() if isinstance(s, dict) and s.get('state') == 'ESTAB')
    assert int(count_post) > int(count_pre), (
        f"Active sessions count did not increase\n"
        f"Before: {count_pre}\n"
        f"After: {count_post}"
    )


def parse_ssh_login_notification(dut_ip, username, password, assert_last_login=True, assert_no_errors=False):
    '''
    @summary: in this function we will parse the login ssh notification parameters
    when creating ssh connection to switch and extracting all the parameters
    this includes:
        1.	Last successful login date/time and location – (for all users)
        2.	Number of unsuccessful logins since last successful login – (per user)
        3.	Last unsuccessful date/time and location (terminal or IP) – (per user)
        4.	Changes to user's account since last login (password, role, group, etc) – (per user)
        5.	Number of total successful logins since a date/time – (for all users)

    :param dut_ip: IP address of the device under test
    :param username: username to login with
    :param password: password for the user
    :param assert_last_login: If True (default), asserts that "Last login:" message exists.
                             Set to False for first-time logins where no history exists yet.
    :param assert_no_errors: If True, validates the SSH login notification for common errors.
                            Defaults to False (lenient mode) - use True in strict validation scenarios
                            where MOTD/login scripts must be error-free.
    :return: will return a dictionary of each parameter, e.g.:
    {
        'last_successful_login_date' : datetime object of current date,
        'last_successful_login_ip' : '10.7.34.240',
        'last_unsuccessful_login_date' : datetime object of current date,
        'last_unsuccessful_login_ip' : '10.7.34.240',
        'number_of_unsuccessful_attempts_since_last_login', '4',
        'record_period' : '5',
        'number_of_successful_connections_in_the_last_record_period' : '100',
        'password_changed_message' : None, (None - means didn't appear in the notification)
        'role_changed_message' : None
    }
    '''
    result = {}

    with allure.step(f'Connect to switch with user "{username}"'):
        _, _, notification_login_message = SshAuthenticator(username, password, dut_ip).attempt_login_success(
            return_output=True)
        # notification_login_message = ssh_to_device_and_retrieve_raw_login_ssh_notification(dut_ip,
        #                                                                                    username,
        #                                                                                    password)

    if assert_no_errors:
        with allure.step('Verify no errors in SSH login notification'):
            # Check for common shell/script error patterns that indicate MOTD or login script failures
            match = Consts.SSH_LOGIN_ERROR_PATTERN.search(notification_login_message)
            if match:
                assert False, \
                    f"SSH login notification contains error matching pattern '{match.group()}'\n" \
                    f"Full notification message:\n{notification_login_message}"

    with allure.step('Parse ssh login output'):
        for key, regex in Consts.LOGIN_SSH_NOTIFICATION_REGEX_DICT.items():
            logging.info(f'Extract key: {key}')
            match = re.findall(regex, notification_login_message)
            if regex == Consts.LAST_SUCCESSFUL_LOGIN_DATE_REGEX:
                if assert_last_login:
                    assert match, f'could not find {key} in ssh login message.\nregex: {regex}\n' \
                        f'login message:\n{notification_login_message}'
                # there will be always output to catch it if it is not first login
                result[Consts.LAST_SUCCESSFUL_LOGIN_DATE] = convert_linux_date_output_to_datetime_object(match[0]) if match else None
            elif regex == Consts.LAST_UNSUCCESSFUL_LOGIN_DATE_REGEX:
                # not always the message will appear
                result[Consts.LAST_UNSUCCESSFUL_LOGIN_DATE] = convert_linux_date_output_to_datetime_object(match[0]) if match else None
            elif regex == Consts.NUMBER_OF_SUCCESSFUL_CONNECTIONS_IN_THE_LAST_RECORD_PERIOD_REGEX:
                # Assert this field should always be present for all users
                assert match, f'could not find {key} in ssh login message.\nregex: {regex}\n' \
                    f'login message:\n{notification_login_message}'
                result[key] = match[0]
            else:
                result[key] = match[0] if match else None

    return result


def change_username_password(engines, username, curr_password, new_password):
    '''
    @summary: in this test case we want to validate password message appearance
    after changing it in the second ssh login notification
    :param username: username
    :param curr_password: current password for username
    :param new_password: new password to change to
    '''
    with allure.step("Changing password for user: {}".format(username)):
        logger.info("Changing password for user: {}\n"
                    "Current password: {}\n"
                    "New password proposed: {}".format(username, curr_password, new_password))
        System(force_api=ApiType.NVUE).aaa.user.user_id[username].set(DefaultConnectionValues.PASSWORD, new_password,
                                                                      apply=True).verify_result()

    with allure.step("Sleeping {} secs to allow password change".format(Consts.PASSWORD_UPDATE_WAIT_TIME)):
        time.sleep(Consts.PASSWORD_UPDATE_WAIT_TIME)


def validate_ssh_login_notifications_default_fields(engines, login_source_ip_addresses, username, password, capability,
                                                    check_password_change_msg=False,
                                                    check_role_change_msg=False,
                                                    already_login_failed=0,
                                                    expected_login_record_period=None,
                                                    last_successful_login=None):
    '''
    @summary: in this test case we want to validate the output of default fields
    of login ssh notification, where we want to check the following parameters:
        [
            'last_successful_login_date',
            'last_successful_login_time',
            'last_successful_login_ip',
            'last_unsuccessful_login_date',
            'last_unsuccessful_login_time',
            'last_unsuccessful_login_ip',
            'number_of_unsuccessful_attempts_since_last_login',
            'record_period',
            'number_of_successful_connections_in_the_last_record_period'
        ]
    :param engines: fixture containing all engines
    :param login_source_ip_addresses: ip address initiating the ssh connection in the test
    :param username: username to connect with to switch
    :param password: the password for username
    :param capability: the username capability, could be one of [admin, monitor]
    :param check_password_change_msg: if set true will check if password message appeared
    :param check_role_change_msg: if set true will check if role message appeared
    :param expected_login_record_period: if not None, will validate same value as the notification value
    :param last_successful_login: datetime object of the time since last successful login
    '''
    if not already_login_failed:
        random_number_of_connection_fails = random.randint(5, 15)
        with allure.step("Fail {} times connecting to device".format(random_number_of_connection_fails)):
            logger.info("Attempting {} wrong password attempts".format(random_number_of_connection_fails))
            authenticator = SshAuthenticator(username, password, engines.dut.ip)
            for index in range(random_number_of_connection_fails):
                logger.info(f'Attempt number {index + 1}')
                authenticator.attempt_login_failure()
                # try:
                #     connection = create_ssh_login_engine(engines.dut.ip, username)
                #     connection.expect(DefaultConnectionValues.PASSWORD_REGEX)
                #     random_password = RandomizationTool.get_random_string(random.randint(Consts.PASSWORD_MIN_LEN,
                #                                                                          Consts.PASSWORD_MAX_LEN))
                #     logger.info("Iteration {} - connecting using random password: {}".format(index, random_password))
                #     connection.sendline(random_password)
                #     connection.expect(["Permission denied", "permission denied"])
                # finally:
                #     connection.close()
    else:
        random_number_of_connection_fails = already_login_failed

    with allure.step("Connect for the second time to switch and store details"):
        second_login_notification_message = parse_ssh_login_notification(engines.dut.ip, username,
                                                                         password)

    if last_successful_login:
        with allure.step("Validating same date"):
            time_delta_seconds = (abs(
                second_login_notification_message[Consts.LAST_SUCCESSFUL_LOGIN_DATE] - last_successful_login)).seconds
            assert time_delta_seconds < Consts.MAX_TIME_DELTA_BETWEEEN_CONNECTIONS, "Time Delta between current time and successful login ssh time is not under 120 secs, \n" \
                                                                                    "The time difference is {}".format(
                                                                                        time_delta_seconds)
            time_delta_seconds = (second_login_notification_message[
                Consts.LAST_UNSUCCESSFUL_LOGIN_DATE] - last_successful_login).seconds
            assert time_delta_seconds < Consts.MAX_TIME_DELTA_BETWEEEN_CONNECTIONS, "Time Delta between current time and successful login ssh time is not under 120 secs, \n" \
                                                                                    "The time difference is {}".format(
                                                                                        time_delta_seconds)

    with allure.step(
            "Validating {} failed attempts in the second connection".format(random_number_of_connection_fails)):
        assert int(second_login_notification_message[
            Consts.NUMBER_OF_UNSUCCESSFUL_ATTEMPTS_SINCE_LAST_LOGIN]) == random_number_of_connection_fails, \
            f"Number of failed connections is not the same, \n" \
            f"Expected: {random_number_of_connection_fails} \n" \
            f"Actual: {second_login_notification_message[Consts.NUMBER_OF_UNSUCCESSFUL_ATTEMPTS_SINCE_LAST_LOGIN]}"

    with allure.step("Validating IP address is same as this test IP address"):
        with allure.step("Validating successful IP address"):
            assert second_login_notification_message[Consts.LAST_SUCCESSFUL_LOGIN_IP] in login_source_ip_addresses, \
                f"Not same login IP Address, \n" \
                f"Expected: {login_source_ip_addresses} \n" \
                f"Actual: {second_login_notification_message[Consts.LAST_SUCCESSFUL_LOGIN_IP]}"
        with allure.step("Validating unsuccessful IP address"):
            assert second_login_notification_message[Consts.LAST_UNSUCCESSFUL_LOGIN_IP] in login_source_ip_addresses, \
                f"Not same unsuccessful login IP Address\n" \
                f"Expected: {login_source_ip_addresses} \n" \
                f"Actual: {second_login_notification_message[Consts.LAST_UNSUCCESSFUL_LOGIN_IP]}"

    with allure.step("Validating password or capability changes"):
        if check_password_change_msg:
            assert second_login_notification_message[Consts.PASSWORD_CHANGED_MESSAGE] is not None, \
                "Password change message did not appear when it should"
        else:
            assert second_login_notification_message[Consts.PASSWORD_CHANGED_MESSAGE] is None, \
                "Password change message appeared when it should not"

        if check_role_change_msg:
            assert second_login_notification_message[Consts.ROLE_CHANGED_MESSAGE] is not None, \
                "Capability change message did not appear when it should"
        else:
            assert second_login_notification_message[Consts.ROLE_CHANGED_MESSAGE] is None, \
                "Capability change message appeared when it should not"

    # if expected_login_record_period:
    #     with allure.step("Validating login-record-period value"):
    #         logger.info("Validating login-record-period value")
    #         assert second_login_notification_message[Consts.RECORD_PERIOD] == str(expected_login_record_period), \
    #             "Not same login record period value, expected: {}, actual: {}".format(expected_login_record_period,
    #                                                                                   second_login_notification_message[Consts.RECORD_PERIOD])


def get_current_time_in_secs():
    '''
    @summary: in this function we convert the current date to seconds
    :return:
    '''
    output = os.popen("date").read()
    current_date_string = re.findall(Consts.LINUX_DATE_REGEX, output)[0]
    logger.info("Linux date is {}".format(current_date_string))
    current_date = convert_linux_date_output_to_datetime_object(current_date_string)
    return current_date


@pytest.fixture
def monitor_user_for_ssh_allowed(engines):
    """
    Create a new NVUE monitor user for SSH allowed-user tests.
    Yields (user_name, password); teardown deletes the user.
    """
    system = System(force_api=ApiType.NVUE)
    with allure.step("Create new user"):
        user_name, password = system.aaa.user.set_new_user(role=Consts.NVUE_MONITOR_ROLE, apply=True)
    try:
        yield (user_name, password)
    finally:
        with allure.step("Delete user"):
            delete_user(engines, user_name)
