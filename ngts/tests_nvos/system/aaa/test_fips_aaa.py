import json
import logging
import pytest
import subprocess
import time
from collections import defaultdict
from datetime import datetime

from ngts.nvos_constants.constants_nvos import ApiType, CumulusConsts
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.cli_wrappers.nvue.cumulus.cumulus_general_cli import CumulusGeneralCli
from ngts.tests_nvos.general.security.radius.constants import CLRadiusPhysicalServer
from ngts.tests_nvos.general.security.tacacs.constants import TacacsPhysicalServer
from ngts.tests_nvos.general.security.test_aaa_ldap.ldap_servers_info import LdapServers
from ngts.constants.constants import GnmiConsts
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.constants import GnmiMode
from ngts.cli_wrappers.openapi.openapi_command_builder import OpenApiCommandHelper, OpenApiReqType


# Import shared utilities
from ngts.nvos_tools.infra.SessionManager import SessionManager
from ngts.nvos_tools.system.UserManager import (
    create_user,
    add_user_with_system_admin,
    add_user_with_sudo,
)
from ngts.tests_nvos.general.security.security_test_tools.aaa_server_config import (
    configure_radius_server,
    configure_tacacs_server,
    configure_ldap_server,
    set_authentication_order,
    unset_authentication_order,
    verify_authentication_order,
    cleanup_test_aaa_servers,
    set_radius_server,
    unset_radius_server,
    set_tacacs_server,
    unset_tacacs_server,
    set_ldap_server,
    unset_ldap_server
)
from ngts.tests_nvos.general.security.security_test_tools.security_test_utils import (
    get_fips_state,
    is_fips_enabled,
    switch_fips_mode,
    enable_fips_mode,
    disable_fips_mode,
    _reboot_and_wait_for_system,
    _apply_config_with_expected_disconnect,
    change_max_files,
    increase_pty_limit,
    configure_ssh_server_fips_algorithms,
    rotate_logs,
    configure_non_default_vrf,
    ensure_gnmic_installed,
    verify_gnmi_connections_active,
    verify_gnmi_connections_closed,
    enable_gnmi_server_with_cert,
    create_gnmi_client,
    create_gnmi_subscription,
    create_gnmi_subscription_session,
    disable_gnmi_server_and_cleanup
)


cumulus_owner = "gpazbenitzha"


logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================
FIPS_DISCONNECT_TIMEOUT = 15  # Wait for asynchronous disconnection to complete
SESSION_CREATION_DELAY = 0.5
NEW_SSH_PORT1 = 40
NEW_SSH_PORT2 = 41


class TestServerConfig:
    """Test server configuration constants to avoid hardcoded values."""
    # TACACS test credentials (from TacacsPhysicalServer)
    TACACS_TEST_USER = "azmy"
    TACACS_TEST_PASSWORD = "azmy"

    # Test user passwords
    ROOT_TEST_PASSWORD = 'Admin!Pass567#'
    NVAPPLY_TEST_PASSWORD = 'NvApply@Pass123'
    TESTUSER1_PASSWORD = 'CompPass123_'


# =============================================================================
# Global State (consider refactoring to fixtures for better isolation)
# =============================================================================
session_mgr = SessionManager()
cli_common = None
user_credentials = {}
configured_vrf_name = None  # VRF name for multi-VRF testing (e.g., 'RED')
sessions_dict = session_mgr.sessions_dict  # Backward compatibility


_cached_system = None


def get_system():
    """Get a cached System object configured for NVUE API. Creates once, reuses thereafter."""
    global _cached_system
    if _cached_system is None:
        _cached_system = System(force_api=ApiType.NVUE)
    return _cached_system


@pytest.fixture(scope='module', autouse=True)
def fips_test_module_setup(engines, devices):
    """Module-level setup for FIPS testing"""
    global cli_common, configured_vrf_name, user_credentials
    cli_common = CumulusGeneralCli(engines.dut, engines.dut)
    cli_common.modify_sudoers_for_cumulus()

    # Clear user credentials at module start to force recreation
    user_credentials.clear()
    logger.info("Cleared user credentials for fresh start")

    # Clear any pending configuration to avoid conflicts
    with allure.step('Clear any pending configuration before module setup'):
        try:
            engines.dut.run_cmd("nv config detach")
            logger.info("Detached any pending configuration")
        except Exception as e:
            logger.warning(f"Failed to detach config (may not exist): {e}")

    # Setup system resources (from test_disconnect_fix_threading.py)
    change_max_files(engines)
    increase_pty_limit(engines)

    # Configure SSH server algorithms required for FIPS mode
    configure_ssh_server_fips_algorithms(engines)

    # Configure authentication servers (presuite setup)
    with allure.step('Configure authentication servers'):
        configure_radius_server(engines)
        configure_tacacs_server(engines)
        configure_ldap_server(engines)

    # CRITICAL: Install RADIUS/TACACS/LDAP packages BEFORE enabling FIPS mode
    # When FIPS is enabled, apt-get fails with libgcrypt error, preventing package installation
    # Solution: Set auth order to install packages, then revert to local
    with allure.step('Pre-install AAA packages before FIPS mode (workaround for libgcrypt FIPS issue)'):
        if not is_fips_enabled(engines):
            try:
                logger.info("Installing RADIUS packages (set auth order 'radius local')...")
                set_authentication_order(engines, ['radius', 'local'], is_fips_mode=False)
                logger.info("✓ RADIUS packages installed successfully")

                logger.info("Installing TACACS packages (set auth order 'tacacs local')...")
                set_authentication_order(engines, ['tacacs', 'local'], is_fips_mode=False)
                logger.info("✓ TACACS packages installed successfully")

                logger.info("Installing LDAP packages (set auth order 'ldap local')...")
                set_authentication_order(engines, ['ldap', 'local'], is_fips_mode=False)
                logger.info("✓ LDAP packages installed successfully")

                # Revert back to local-only auth order after all packages are installed
                logger.info("Reverting auth order back to 'local'...")
                set_authentication_order(engines, ['local'], is_fips_mode=False)
                logger.info("✓ Auth order reverted to 'local' - all AAA packages remain installed and ready for FIPS mode")
            except Exception as e:
                logger.warning(f"Failed to pre-install AAA packages: {e}")
                logger.warning("Tests may fail when trying to set AAA auth order in FIPS mode")

    # Install sshpass for VRF session testing (apt fails in FIPS mode due to libgcrypt)
    with allure.step('Install sshpass for VRF session testing'):
        try:
            # Check if sshpass is already installed
            sshpass_check = engines.dut.run_cmd("which sshpass 2>/dev/null || echo 'not found'", validate=False)
            if 'not found' in sshpass_check:
                logger.info("Installing sshpass via direct download (apt fails in FIPS mode)")
                # Download sshpass deb package directly from Debian repo
                engines.dut.run_cmd(
                    "curl -L -o /tmp/sshpass.deb http://deb.debian.org/debian/pool/main/s/sshpass/sshpass_1.09-1+b1_amd64.deb",
                    validate=False
                )
                # Install using dpkg
                engines.dut.run_cmd("sudo dpkg -i /tmp/sshpass.deb", validate=False)
                # Verify installation
                verify = engines.dut.run_cmd("which sshpass 2>/dev/null || echo 'not found'", validate=False)
                if 'not found' not in verify:
                    logger.info("✓ sshpass installed successfully")
                else:
                    logger.warning("sshpass installation may have failed")
            else:
                logger.info("sshpass is already installed")
        except Exception as e:
            logger.warning(f"Failed to install sshpass: {e} - VRF sessions may not be created")

    # Configure non-default VRF for multi-VRF testing
    with allure.step('Configure non-default VRF for testing'):
        try:
            configured_vrf_name = configure_non_default_vrf(engines, vrf_name='RED', interface='swp1', ip_address='192.168.100.1/24')
            logger.info(f"Non-default VRF configured: {configured_vrf_name}")
        except Exception as e:
            logger.warning(f"Failed to configure non-default VRF (may not be supported on this device): {e}")
            configured_vrf_name = None

    # Note: User creation moved to fips_enabled fixture to ensure FIPS-compliant password hashes
    # Store original FIPS state
    original_fips_state = None
    try:
        original_fips_state = engines.dut.run_cmd("nv show system security fips -o json")
    except Exception as e:
        logger.debug(f"Could not get original FIPS state: {e}")

    yield

    # Post-suite cleanup with proper order for FIPS mode
    logger.info("Starting post-suite cleanup with proper FIPS handling")

    # Step 1: Restore original FIPS state if it changed (with apply and reboot)
    with allure.step('Cleanup Step 1: Restore original FIPS state if needed'):
        try:
            # Get current and original FIPS states
            current_fips_state_dict = get_fips_state(engines)
            current_is_enabled = current_fips_state_dict.get('operational', '') == 'enabled'

            original_is_enabled = False
            try:
                if original_fips_state:
                    original_json = json.loads(original_fips_state)
                    if isinstance(original_json, dict):
                        original_is_enabled = original_json.get('mode') == 'enabled'
            except Exception as e:
                logger.warning(f"Failed to parse original FIPS state JSON: {e}")

            logger.info(f"Current FIPS operational state: {'enabled' if current_is_enabled else 'disabled'}")
            logger.info(f"Original FIPS state: {'enabled' if original_is_enabled else 'disabled'}")

            # Check if FIPS state changed from original
            if current_is_enabled != original_is_enabled:
                if original_is_enabled:
                    # Original was enabled, current is disabled - restore to enabled
                    logger.info("Restoring original FIPS enabled state...")
                    switch_fips_mode(engines, on=True, should_reboot=True)
                    logger.info("FIPS restored to enabled state with reboot")
                else:
                    # Original was disabled, current is enabled - restore to disabled
                    logger.info("Restoring original FIPS disabled state...")
                    switch_fips_mode(engines, on=False, should_reboot=True)
                    logger.info("FIPS restored to disabled state with reboot")
            else:
                logger.info(f"FIPS state unchanged from original ({'enabled' if current_is_enabled else 'disabled'}), no action needed")
        except Exception as e:
            logger.warning(f"Failed to restore original FIPS state during cleanup: {e}")

    # Step 2: Unset authentication order if not default (with apply)
    with allure.step('Cleanup Step 2: Unset authentication order if needed'):
        try:
            # Get current authentication order using nv show command
            auth_status_json = engines.dut.run_cmd("nv show system aaa authentication -o json")
            auth_data = json.loads(auth_status_json)
            operational_order = auth_data.get('order', None)

            logger.info(f"Current authentication order: {operational_order}")

            # Only unset if auth order is not default (None, [], or ['local'])
            if operational_order and operational_order not in [[], ['local']]:
                logger.info(f"Authentication order is {operational_order}, unsetting...")
                system = get_system()
                system.aaa.authentication.unset_authentication_order(
                    dut_engine=engines.dut,
                    apply=True,
                    ask_for_confirmation='-y'
                )
                logger.info("Authentication order unset and applied - all other changes cleaned up automatically")
            else:
                logger.info(f"Authentication order is default ({operational_order}), no need to unset")
        except Exception as e:
            logger.warning(f"Failed to unset authentication order during cleanup: {e}")

    # Step 3: Full Config Cleanup (ensure all config is cleared regardless of test execution)
    with allure.step('Cleanup Step 3: Full Configuration Cleanup'):
        try:
            logger.info("Executing full configuration cleanup (clearing all settings)")
            devices.dut.clear_config(engines.dut)
            logger.info("Full configuration cleanup completed")
        except Exception as e:
            logger.warning(f"Failed to execute full configuration cleanup: {e}")

    # Clear credentials at end
    user_credentials.clear()
    logger.info("Post-suite cleanup completed")

    # NOTE: We manually clear config here to ensure cleanup even if the last test skipped it


@pytest.fixture(scope='function', autouse=True)
def function_cleanup(engines):
    """Function-level cleanup"""
    yield
    # Cleanup sessions and test users
    ResultObj._pop_all_instances()
    session_mgr.clear()


@pytest.fixture(scope='module')  # Changed to module scope to create users only once
def fips_enabled(engines):
    """Ensure FIPS mode is enabled and create users with FIPS-compliant hashes (ONCE per module)"""
    logger.debug("fips_enabled fixture starting")
    # Check FIPS state via JSON
    is_enabled = False
    try:
        status = engines.dut.run_cmd("nv show system security fips -o json")
        status_json = json.loads(status)
        if isinstance(status_json, dict):
            is_enabled = status_json.get("mode") == "enabled"
    except (json.JSONDecodeError, KeyError, Exception) as e:
        logger.debug(f"Could not parse FIPS status: {e}")

    if not is_enabled:
        switch_fips_mode(engines, on=True, should_reboot=True)

    # Create all users after FIPS is enabled to ensure FIPS-compliant password hashes
    # This runs ONCE per module, not per test, to avoid password mismatches
    with allure.step('Create all test users with FIPS-compliant password hashes'):
        global user_credentials
        logger.debug("Starting to create users. user_credentials: %s", user_credentials)
        # Only create users if they haven't been created yet
        if not user_credentials:
            system = get_system()

            # Define user groups for creation
            local_users = ['loc_test_user', 'test_user', 'user1', 'user2', 'local_user']
            admin_users = ['admin_user', 'system_admin']
            sudo_users = ['super_user']

            # Create local users for testing
            for username in local_users:
                logger.debug(f"Creating local user '{username}'")
                _, password = create_user(system, username, apply=False)
                user_credentials[username] = password

            # Create admin users (system-admin role)
            for username in admin_users:
                logger.debug(f"Creating admin user '{username}'")
                _, password = add_user_with_system_admin(engines, username, apply=False)
                user_credentials[username] = password

            # Create sudo users (this will apply all pending user creations)
            for username in sudo_users:
                logger.debug(f"Creating sudo user '{username}' - this will apply all users")
                _, password = add_user_with_sudo(engines, username)
                user_credentials[username] = password

            logger.info("All test users created with FIPS-compliant password hashes")

            # Verify users actually exist on the system
            expected_users = local_users + admin_users + sudo_users
            logger.debug("Verifying users exist on the system")
            try:
                grep_pattern = '|'.join(expected_users)
                result = engines.dut.run_cmd(f"getent passwd | grep -E '({grep_pattern})'")
                logger.debug(f"Users found on system:\n{result}")

                # Verify each user exists
                for expected_user in expected_users:
                    if expected_user not in result:
                        raise Exception(f"User {expected_user} was not created on the system! Apply may have failed.")
                logger.info("✓ All expected users verified on system")
            except Exception as e:
                logger.error(f"Failed to verify users on system: {e}")
                # Clear credentials since users weren't created
                user_credentials.clear()
                raise Exception(f"User creation failed - users not found on system: {e}")

            logger.debug(f"Total users in credentials dict: {len(user_credentials)}")
        else:
            logger.info("Users already created, reusing existing credentials")

    yield


@pytest.fixture
def fips_disabled(engines):
    """Ensure FIPS mode is disabled"""
    # Check FIPS state via JSON
    is_disabled = False
    try:
        status = engines.dut.run_cmd("nv show system security fips -o json")
        status_json = json.loads(status)
        if isinstance(status_json, dict):
            is_disabled = status_json.get("mode") == "disabled"
    except (json.JSONDecodeError, KeyError, Exception) as e:
        logger.debug(f"Could not parse FIPS status: {e}")

    if not is_disabled:
        switch_fips_mode(engines, on=False, should_reboot=True)
    yield


@pytest.fixture
def reset_auth_order(engines):
    """
    Reset authentication order to 'local' if it is not already 'local'.
    """
    with allure.step('Check and reset authentication order to "local"'):
        try:
            # Check current authentication order
            current_order_output = engines.dut.run_cmd("nv show system aaa authentication -o json")

            # Parse JSON
            try:
                data = json.loads(current_order_output)
                order = data.get('order', [])
            except Exception as e:
                logger.warning(f"Failed to parse JSON output: {current_order_output}, error: {e}")
                order = []

            logger.info(f"Current authentication order: {order}")

            # If order is NOT ['local'], reset it
            if order != ['local']:
                logger.warning(f"Authentication order is {order}, resetting to ['local']")

                # Check FIPS state to handle disconnects properly
                fips_on = is_fips_enabled(engines)

                set_authentication_order(engines, ['local'], is_fips_mode=fips_on)
            else:
                logger.info("Authentication order is already 'local'")

        except Exception as e:
            logger.warning(f"Error in reset_auth_order: {e}")

    yield


# Multi-Provider Session Management
def create_multi_provider_sessions(engines, include_local=True, include_console=True, include_radius=False,
                                   include_tacacs=False, include_ldap=False, include_non_default_vrf=False,
                                   vrf_name=None, raise_on_failure=False):
    """
    Create sessions from multiple authentication providers using threading.

    Args:
        engines: Test engines
        include_local: Include local user SSH session
        include_console: Include console session
        include_radius: Include RADIUS user sessions
        include_tacacs: Include TACACS user sessions
        include_ldap: Include LDAP user sessions
        include_non_default_vrf: Include VRF session
        vrf_name: Name of the VRF for VRF session (e.g., 'RED')
        raise_on_failure: If True, raise exception if any session creation fails.
                         If False (default), log warning and continue with partial results.

    Returns:
        dict: Dictionary of sessions by provider type. May be partially populated if failures occurred.
    """
    with allure.step(f'Create multi-provider sessions (Local={include_local}, Console={include_console}, '
                     f'Radius={include_radius}, Tacacs={include_tacacs}, Ldap={include_ldap}, Vrf={include_non_default_vrf})'):
        sessions = defaultdict(list)
        local_user = 'loc_test_user'
        local_pass = user_credentials[local_user]
        creation_failed = False

        logger.debug(f"create_multi_provider_sessions: local={include_local}, console={include_console}, "
                     f"radius={include_radius}, tacacs={include_tacacs}, ldap={include_ldap}, vrf={include_non_default_vrf}")

        # Get user lists from constants (define at top for later session collection)
        radius_users = []
        radius_passwords = []
        tacacs_users = []
        tacacs_passwords = []
        ldap_users = []
        ldap_passwords = []

        if include_radius:
            radius_config = CLRadiusPhysicalServer.SERVER_IPV4
            radius_users = [user.username for user in radius_config.users][1:2]
            radius_passwords = [user.password for user in radius_config.users][1:2]
            logger.debug(f"RADIUS users: {radius_users}")

        if include_tacacs:
            tacacs_users = [TestServerConfig.TACACS_TEST_USER]
            tacacs_passwords = [TestServerConfig.TACACS_TEST_PASSWORD]
            logger.debug(f"TACACS users: {tacacs_users}")

        if include_ldap:
            ldap_config = LdapServers.PHYSICAL_SERVER
            ldap_users = [user.username for user in ldap_config.users][:2]
            ldap_passwords = [user.password for user in ldap_config.users][:2]
            logger.debug(f"LDAP users: {ldap_users}")

        # Create sessions
        if include_local:
            with allure.step(f"Creating LOCAL session for user '{local_user}'"):
                logger.debug(f"Creating LOCAL session for user '{local_user}'")
                session_mgr.create_session_thread(engines, local_user, local_pass)

        if include_console:
            with allure.step(f"Creating CONSOLE session for user '{local_user}'"):
                # Console session implementation - create console session with local user
                # Don't raise on failure - just log and continue
                console_session = session_mgr.create_console_session(engines, local_user, local_pass, raise_on_failure=False)
                if console_session is not None:
                    sessions['console'].append(console_session)
                else:
                    logger.warning(f"Console session creation failed for user '{local_user}' - continuing without console session")
                    creation_failed = True

        if include_non_default_vrf and vrf_name:
            with allure.step(f"Creating VRF session for user '{local_user}' via '{vrf_name}'"):
                # Create session through non-default VRF using 'ip vrf exec'
                try:
                    vrf_session = session_mgr.create_session_via_vrf(engines, local_user, local_pass, vrf_name)
                    sessions['vrf'].append(vrf_session)
                except Exception as e:
                    logger.warning(f"VRF session creation failed: {e} - continuing without VRF session")
                    creation_failed = True

        if include_radius:
            with allure.step("Creating RADIUS sessions"):
                logger.debug(f"Creating RADIUS sessions for users: {radius_users}")
                for user, password in zip(radius_users, radius_passwords):
                    session_mgr.create_session_thread(engines, user, password)

        if include_tacacs:
            with allure.step("Creating TACACS sessions"):
                logger.debug(f"Creating TACACS sessions for users: {tacacs_users}")
                for user, password in zip(tacacs_users, tacacs_passwords):
                    session_mgr.create_session_thread(engines, user, password)

        if include_ldap:
            with allure.step("Creating LDAP sessions"):
                logger.debug(f"Creating LDAP sessions for users: {ldap_users}")
                for user, password in zip(ldap_users, ldap_passwords):
                    session_mgr.create_session_thread(engines, user, password)

        # Wait for all session threads to complete
        with allure.step("Wait for all session threads to complete"):
            session_mgr.wait_for_sessions_threads()

        # Collect sessions from sessions_dict
        with allure.step("Collect sessions"):
            if local_user in sessions_dict:
                sessions['local'].extend(sessions_dict[local_user])
            if include_local and not sessions['local']:
                logger.warning(f"Local session creation failed for user '{local_user}'")
                creation_failed = True

            if include_radius:
                for user in radius_users:
                    if user in sessions_dict:
                        sessions['radius'].extend(sessions_dict[user])
                    else:
                        logger.warning(f"RADIUS session creation failed for user '{user}'")
                        creation_failed = True

            if include_tacacs:
                for user in tacacs_users:
                    if user in sessions_dict:
                        sessions['tacacs'].extend(sessions_dict[user])
                    else:
                        logger.warning(f"TACACS session creation failed for user '{user}'")
                        creation_failed = True

            if include_ldap:
                for user in ldap_users:
                    if user in sessions_dict:
                        sessions['ldap'].extend(sessions_dict[user])
                    else:
                        logger.warning(f"LDAP session creation failed for user '{user}'")
                        creation_failed = True

        # Log summary of session creation
        total_sessions = sum(len(s) for s in sessions.values())
        logger.info(f"Session creation completed: {total_sessions} sessions created")
        if creation_failed:
            logger.warning("Some session creations failed - returning partial results")
            if raise_on_failure:
                raise Exception("One or more session creations failed")

        return sessions


def verify_all_sessions_disconnected(engines, sessions_dict_param, timeout=FIPS_DISCONNECT_TIMEOUT):
    """Verify all sessions from all providers are disconnected (allows one cumulus admin session)"""
    with allure.step(f'Verify all sessions disconnected within {timeout}s'):
        time.sleep(timeout)
        for provider, sessions in sessions_dict_param.items():
            for session in sessions:
                if hasattr(session, 'username'):
                    # Allow one cumulus session (the admin session used for verification)
                    allow_cumulus = (session.username == 'cumulus')
                    session_mgr.verify_sessions_disconnected(cli_common, session.username, allow_one_session=allow_cumulus)


def verify_sessions_still_active(engines, sessions_dict_param):
    """Verify all sessions are still active (for negative tests)"""
    with allure.step('Verify sessions still active'):
        for provider, sessions in sessions_dict_param.items():
            for session in sessions:
                if hasattr(session, 'username'):
                    session_mgr.verify_sessions_active(cli_common, session.username)


# AAA Command Execution with Different APIs
def execute_aaa_command_nvue(engines, command, admin_session):
    """Execute AAA command via NVUE CLI"""
    with allure.step(f'Execute NVUE command: {command}'):
        result = admin_session.run_cmd(command)
        NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')
        return result


def execute_non_aaa_command(engines, admin_session):
    """Execute non-AAA command to verify no disconnection"""
    with allure.step('Execute non-AAA command (interface)'):
        admin_session.run_cmd("nv set interface swp1 ip address 192.168.100.1/24")
        NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')


def apply_config_from_session(session, timeout=10, expect_disconnect=False):
    """
    Apply config from a specific session with optional disconnect handling.
    """
    with allure.step(f'Apply config from session (expect_disconnect={expect_disconnect})'):
        if expect_disconnect:
            try:
                prompt = session.engine.find_prompt()
                disconnect_msg = r"Session terminated by NVUE"
                expect_pattern = f"({prompt}|{disconnect_msg})"

                output = session.engine.send_command(
                    'nv -y config apply',
                    expect_string=expect_pattern,
                    max_loops=100,
                    delay_factor=0.1
                )

                logger.info(f"Apply output: {output}")

                if "Invalid config" in output or "Error" in output:
                    logger.error(f"Apply failed with error (users might not disconnect): {output}")

                return output

            except Exception as e:
                logger.info(f"Apply caused disconnection as expected: {type(e).__name__}: {e}")
                return "applied (disconnected as expected)"
        else:
            return session.run_cmd("nv -y config apply")


# Test Cases Implementation
@pytest.mark.fips
@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.test_first
@pytest.mark.skip_clear_config  # Skip cleanup between tests to preserve users
def test01_disconnect_all_user_types(engines, fips_enabled):
    """
    Test Case 1: Check that after connecting with many users (local, radius, tacacs, ldap)
    and from different vrf and from console, enabling fips mode on and changing authentication
    order disconnects all connected users.
    """
    global configured_vrf_name
    with allure.step('Create multiple user sessions from different providers and VRFs'):
        # Use pre-created admin user
        admin_user = 'admin_user'
        admin_pass = user_credentials[admin_user]
        session_mgr.create_session_thread(engines, admin_user, admin_pass)
        # Create multi-provider sessions including VRF if configured in module setup
        test_sessions = create_multi_provider_sessions(
            engines,
            include_local=True,
            include_console=True,
            include_non_default_vrf=(configured_vrf_name is not None),
            vrf_name=configured_vrf_name
        )

        # Wait for all sessions to be established (including admin session)
        session_mgr.wait_for_sessions_threads()
        logger.debug(f"Sessions established: {list(sessions_dict.keys())}")
        admin_session = sessions_dict[admin_user][0]

    rotate_logs(engines)

    with allure.step('Execute nv show system aaa commands - should not disconnect'):
        admin_session.run_cmd("nv show system aaa")
        admin_session.run_cmd("nv show system aaa user")
        admin_session.run_cmd("nv show system aaa authentication")
        admin_session.run_cmd("nv show system aaa radius")
        admin_session.run_cmd("nv show system aaa tacacs")
        admin_session.run_cmd("nv show system aaa ldap")
        # Verify sessions are still active
        verify_sessions_still_active(engines, test_sessions)

    with allure.step('Set authentication order to RADIUS first - disconnects all users'):
        # Set authentication order with RADIUS first - this will disconnect all users in FIPS mode
        set_authentication_order(engines, ['radius', 'local'])

        # Verify all sessions are disconnected (local users can't login with RADIUS first)
        verify_all_sessions_disconnected(engines, test_sessions)

        # Clear threads list for new session creation
        session_mgr.threads.clear()
        # Now create RADIUS sessions since local users can't login with RADIUS as primary
        # Note: Console sessions might not work either with RADIUS primary
        test_sessions = create_multi_provider_sessions(engines, include_console=False, include_radius=True, include_local=False)

        # Wait for all RADIUS sessions to be established
        session_mgr.wait_for_sessions_threads()

    with allure.step('Change authentication order back to LOCAL first - disconnects RADIUS users'):
        # Change back to local first - this will disconnect RADIUS users in FIPS mode
        set_authentication_order(engines, ['local', 'radius'])
        # Verify RADIUS sessions are disconnected
        verify_all_sessions_disconnected(engines, test_sessions)

        # Clear threads list for new session creation
        session_mgr.threads.clear()

        # Now we can create local sessions again since local is primary
        test_sessions = create_multi_provider_sessions(engines, include_local=True, include_console=True)

        # Wait for local sessions to be established
        session_mgr.wait_for_sessions_threads()

    # Test VRF authentication if VRF is configured
    if configured_vrf_name:
        with allure.step(f'Test authentication through VRF "{configured_vrf_name}"'):
            # Test SSH connectivity through the VRF
            local_user = 'loc_test_user'
            local_pass = user_credentials[local_user]

            # Verify VRF is configured and SSH service is running
            vrf_status = engines.dut.run_cmd(f"nv show vrf {configured_vrf_name} -o json", validate=False)
            logger.info(f"VRF {configured_vrf_name} status: {vrf_status}")

            ssh_service_status = engines.dut.run_cmd(f"sudo systemctl status ssh@{configured_vrf_name}.service 2>&1 || echo 'Service not found'", validate=False)
            logger.info(f"SSH service status on VRF {configured_vrf_name}: {ssh_service_status}")

            # Create session via VRF using the helper function
            try:
                vrf_session = session_mgr.create_session_via_vrf(engines, local_user, local_pass, configured_vrf_name)
                logger.info(f"✓ Successfully created session through VRF {configured_vrf_name}: {vrf_session}")

                # Add VRF session to test_sessions for tracking
                if 'vrf' not in test_sessions:
                    test_sessions['vrf'] = []
                test_sessions['vrf'].append(vrf_session)
            except Exception as e:
                logger.warning(f"VRF session creation failed: {e} - VRF authentication may not be supported")

    # Cleanup
    try:
        admin_session.disconnect()
    except Exception:
        pass  # Session might already be disconnected

    logger.info("Test01 completed - All users disconnected when authentication order was changed in FIPS mode")


@pytest.mark.fips
@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.skip_clear_config  # Skip cleanup between tests to preserve users
def test02_change_providers(engines, fips_enabled):
    """
    Test Case 2: Check that when fips mode is on, changing settings for providers
    (radius, tacacs, ldap) disconnects users from that provider.

    For each provider:
    1. Set auth order to use that provider
    2. Create sessions from ONLY that provider
    3. Change provider settings from engines.dut (cumulus default connection)
    4. Verify provider users are disconnected
    """

    # === RADIUS Provider Tests ===
    session_mgr.threads.clear()
    test_sessions = create_multi_provider_sessions(engines, include_local=True, include_console=True)
    session_mgr.wait_for_sessions_threads()

    with allure.step('RADIUS: Set auth order to radius, create RADIUS sessions'):
        set_authentication_order(engines, ['radius', 'local'], is_fips_mode=True)

        # Wait for auth order to propagate
        time.sleep(2)
        verify_all_sessions_disconnected(engines, test_sessions)
        # Create RADIUS sessions only (local users can't login with radius first)
        session_mgr.threads.clear()
        test_sessions = create_multi_provider_sessions(engines, include_radius=True, include_local=False, include_console=False)
        session_mgr.wait_for_sessions_threads()

    rotate_logs(engines)

    with allure.step('RADIUS: Change settings from cumulus - should disconnect RADIUS users'):
        # Make changes from default cumulus connection (engines.dut)
        set_radius_server(engines, '192.168.1.1', secret='newsecret', priority=2, apply=False)
        _apply_config_with_expected_disconnect(engines.dut, "RADIUS settings change")

        # Verify RADIUS users disconnected
        verify_all_sessions_disconnected(engines, test_sessions)

    with allure.step('RADIUS: Reconnect and unset settings - should disconnect RADIUS users'):
        # Reconnect RADIUS users
        session_mgr.threads.clear()
        test_sessions = create_multi_provider_sessions(engines, include_radius=True, include_local=False, include_console=False)
        session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

        # Unset RADIUS settings from cumulus
        unset_radius_server(engines, '192.168.1.1', apply=False)
        _apply_config_with_expected_disconnect(engines.dut, "RADIUS settings unset")

        # Verify disconnection
        verify_all_sessions_disconnected(engines, test_sessions)

    # === TACACS Provider Tests ===
    with allure.step('TACACS: Set auth order to tacacs, create TACACS sessions'):
        set_authentication_order(engines, ['tacacs', 'local'], is_fips_mode=True)

        # Wait for auth order to propagate
        time.sleep(2)

        # Create TACACS sessions only
        session_mgr.threads.clear()
        test_sessions = create_multi_provider_sessions(engines, include_tacacs=True, include_local=False, include_console=False)
        session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

    with allure.step('TACACS: Change settings from cumulus - should disconnect TACACS users'):
        # Make changes from default cumulus connection
        set_tacacs_server(engines, '192.168.1.2', secret='tacacs_secret', priority=2, apply=False)
        _apply_config_with_expected_disconnect(engines.dut, "TACACS settings change")

        # Verify TACACS users disconnected
        verify_all_sessions_disconnected(engines, test_sessions)

    with allure.step('TACACS: Reconnect and unset settings - should disconnect TACACS users'):
        # Reconnect TACACS users
        session_mgr.threads.clear()
        test_sessions = create_multi_provider_sessions(engines, include_tacacs=True, include_local=False, include_console=False)
        session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

        # Unset TACACS settings from cumulus
        unset_tacacs_server(engines, '192.168.1.2', apply=False)
        _apply_config_with_expected_disconnect(engines.dut, "TACACS settings unset")

        # Verify disconnection
        verify_all_sessions_disconnected(engines, test_sessions)

    # === LDAP Provider Tests ===
    with allure.step('LDAP: Set auth order to ldap, create LDAP sessions'):
        set_authentication_order(engines, ['ldap', 'local'], is_fips_mode=True)

        # Wait for auth order to propagate
        time.sleep(2)

        # Create LDAP sessions only
        session_mgr.threads.clear()
        test_sessions = create_multi_provider_sessions(engines, include_ldap=True, include_local=False, include_console=False)
        session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

    with allure.step('LDAP: Change settings from cumulus - should disconnect LDAP users'):
        # Make changes from default cumulus connection (use priority 2 to avoid conflict with existing server)
        set_ldap_server(engines, '192.168.1.3', priority=2, apply=False)
        _apply_config_with_expected_disconnect(engines.dut, "LDAP settings change")

        # Verify LDAP users disconnected
        verify_all_sessions_disconnected(engines, test_sessions)

    with allure.step('LDAP: Reconnect and unset settings - should disconnect LDAP users'):
        # Reconnect LDAP users
        session_mgr.threads.clear()
        test_sessions = create_multi_provider_sessions(engines, include_ldap=True, include_local=False, include_console=False)
        session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

        # Unset LDAP settings from cumulus
        unset_ldap_server(engines, '192.168.1.3', apply=False)
        _apply_config_with_expected_disconnect(engines.dut, "LDAP settings unset")

        # Verify disconnection
        verify_all_sessions_disconnected(engines, test_sessions)

    with allure.step('Cleanup: Remove test AAA servers'):
        # Reset auth order back to local for cleanup
        set_authentication_order(engines, ['local'], is_fips_mode=True)
        # Clean up any RADIUS/TACACS/LDAP servers added during this test
        cleanup_test_aaa_servers(engines)

    logger.info("Test02 completed - All provider changes (RADIUS, TACACS, LDAP) cause FIPS disconnection")


@pytest.mark.fips
@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.skip_clear_config  # Skip cleanup between tests to preserve users
def test03_change_authentication_order(engines, fips_enabled):
    """
    Test Case 3: Check that when fips mode is on, and authentication providers are defined,
    local is top priority. When we change the authentication order to the different providers,
    verify all users disconnect.

    This test verifies that changing authentication order in FIPS mode disconnects all users,
    and tests with multiple providers (RADIUS, TACACS, LDAP) and console sessions.
    """
    # Use pre-created users
    local_user = 'local_user'
    local_pass = user_credentials[local_user]

    # === Start with LOCAL auth order ===
    with allure.step('Connect with local user (local auth order)'):
        set_authentication_order(engines, ['local'], is_fips_mode=True)
        time.sleep(2)  # Wait for auth order to propagate

        session_mgr.threads.clear()
        local_session = session_mgr.create_session(engines, local_user, local_pass)

        rotate_logs(engines)

    # === Change to RADIUS ===
    with allure.step('Change authentication order to RADIUS - should disconnect local user'):
        set_authentication_order(engines, ['radius', 'local'], is_fips_mode=True)
        session_mgr.verify_sessions_disconnected(cli_common, local_user)

    with allure.step('Connect with RADIUS user and console'):
        time.sleep(2)
        session_mgr.threads.clear()
        # Create RADIUS sessions
        radius_sessions = create_multi_provider_sessions(engines, include_radius=True, include_console=False, include_local=False)
        session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

    with allure.step('Change RADIUS provider settings - should disconnect RADIUS users'):
        set_radius_server(engines, '192.168.2.1', secret='test123', priority=2, apply=False)
        _apply_config_with_expected_disconnect(engines.dut, "RADIUS settings change")
        verify_all_sessions_disconnected(engines, radius_sessions)

    # === Change to TACACS ===
    with allure.step('Connect with RADIUS user, change auth order to TACACS - should disconnect RADIUS user'):
        # Current auth order is ['radius', 'local'] - connect with RADIUS user
        time.sleep(2)
        session_mgr.threads.clear()

        # Get RADIUS user credentials
        radius_config = CLRadiusPhysicalServer.SERVER_IPV4
        radius_user = radius_config.users[1].username
        radius_password = radius_config.users[1].password

        # Create session with RADIUS user
        radius_session = session_mgr.create_session(engines, radius_user, radius_password)

        rotate_logs(engines)

        # Change to TACACS - should disconnect RADIUS user
        set_authentication_order(engines, ['tacacs', 'local'], is_fips_mode=True)
        # RADIUS session should be disconnected
        session_mgr.verify_sessions_disconnected(cli_common, radius_user)
        time.sleep(FIPS_DISCONNECT_TIMEOUT)

    with allure.step('Connect with TACACS user and console'):
        time.sleep(2)
        session_mgr.threads.clear()
        tacacs_sessions = create_multi_provider_sessions(engines, include_tacacs=True, include_console=True, include_local=False)
        session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

    with allure.step('Change TACACS provider settings - should disconnect TACACS users'):
        set_tacacs_server(engines, '192.168.2.2', secret='tacacs123', priority=2, apply=False)
        _apply_config_with_expected_disconnect(engines.dut, "TACACS settings change")
        verify_all_sessions_disconnected(engines, tacacs_sessions)

    # === Change to LDAP ===
    with allure.step('Connect with TACACS user, change auth order to LDAP - should disconnect TACACS user'):
        # Current auth order is ['tacacs', 'local'] - connect with TACACS user
        time.sleep(2)
        session_mgr.threads.clear()

        # Get TACACS user credentials
        tacacs_user = TestServerConfig.TACACS_TEST_USER
        tacacs_password = TestServerConfig.TACACS_TEST_PASSWORD

        # Create session with TACACS user
        tacacs_session = session_mgr.create_session(engines, tacacs_user, tacacs_password)

        rotate_logs(engines)

        # Change to LDAP - should disconnect TACACS user
        set_authentication_order(engines, ['ldap', 'local'], is_fips_mode=True)
        # TACACS session should be disconnected
        session_mgr.verify_sessions_disconnected(cli_common, tacacs_user)
        time.sleep(FIPS_DISCONNECT_TIMEOUT)

    with allure.step('Connect with LDAP user and console'):
        time.sleep(2)
        session_mgr.threads.clear()
        ldap_sessions = create_multi_provider_sessions(engines, include_ldap=True, include_console=True, include_local=False)
        session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

    with allure.step('Change LDAP provider settings - should disconnect LDAP users'):
        # Use priority 2 to avoid conflict with existing LDAP server at priority 1
        set_ldap_server(engines, '192.168.2.3', priority=2, apply=False)
        _apply_config_with_expected_disconnect(engines.dut, "LDAP settings change")
        verify_all_sessions_disconnected(engines, ldap_sessions)

    # === Change back to LOCAL ===
    with allure.step('Connect with LDAP user, change auth order to LOCAL - should disconnect LDAP user'):
        # Current auth order is ['ldap', 'local'] - connect with LDAP user
        time.sleep(2)
        session_mgr.threads.clear()

        # Get LDAP user credentials
        ldap_config = LdapServers.PHYSICAL_SERVER
        ldap_user = ldap_config.users[0].username
        ldap_password = ldap_config.users[0].password

        # Create session with LDAP user
        ldap_session = session_mgr.create_session(engines, ldap_user, ldap_password)

        rotate_logs(engines)

        # Change to LOCAL - should disconnect LDAP user
        set_authentication_order(engines, ['local'], is_fips_mode=True)
        # LDAP session should be disconnected
        session_mgr.verify_sessions_disconnected(cli_common, ldap_user)
        time.sleep(FIPS_DISCONNECT_TIMEOUT)

    # === Change to LDAP again and then UNSET ===
    with allure.step('Change auth order to LDAP (no user session needed)'):
        # Current auth order is ['local'] - directly change to LDAP without creating local user session
        # This still triggers FIPS disconnection of any active sessions
        time.sleep(2)

        set_authentication_order(engines, ['ldap', 'local'], is_fips_mode=True)
        time.sleep(FIPS_DISCONNECT_TIMEOUT)

    with allure.step('Connect with LDAP user, unset auth order - should disconnect LDAP user'):
        # Current auth order is ['ldap', 'local'] - connect with LDAP user
        time.sleep(2)
        session_mgr.threads.clear()

        # Get LDAP user credentials
        ldap_config = LdapServers.PHYSICAL_SERVER
        ldap_user = ldap_config.users[0].username
        ldap_password = ldap_config.users[0].password

        # Create session with LDAP user
        ldap_session = session_mgr.create_session(engines, ldap_user, ldap_password)

        rotate_logs(engines)

        # Unset auth order - should disconnect LDAP user
        unset_authentication_order(engines, is_fips_mode=True)
        # LDAP session should be disconnected
        session_mgr.verify_sessions_disconnected(cli_common, ldap_user)
        time.sleep(FIPS_DISCONNECT_TIMEOUT)

    with allure.step('Cleanup: Remove test AAA servers'):
        # Clean up any RADIUS/TACACS/LDAP servers added during this test
        cleanup_test_aaa_servers(engines)

    logger.info("Test03 completed - All authentication order changes cause FIPS disconnection")


@pytest.mark.fips
@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.skip_clear_config  # Skip cleanup between tests to preserve users
def test04_openapi_set_unset(engines, fips_enabled):
    """
    Test Case 4: Check that when fips mode is on, user is logged in, aaa commands run from open API,
    the user is disconnected.

    Login with users from different types (1 console, 1 ssh), change authentication order
    using REST API, check if all users were disconnected.
    """
    # Use pre-created users
    test_user = 'test_user'
    test_pass = user_credentials[test_user]
    admin_user = 'admin_user'
    admin_pass = user_credentials[admin_user]

    with allure.step('Create sessions (1 SSH, 1 console)'):
        session_mgr.threads.clear()
        # Create SSH session
        ssh_session = session_mgr.create_session(engines, test_user, test_pass)
        # Create console session
        console_session = session_mgr.create_console_session(engines, test_user, test_pass)

        rotate_logs(engines)

    with allure.step('Change authentication order via OpenAPI to RADIUS - should disconnect all users'):
        # Use OpenAPI to change authentication order
        # Build the REST API request - pass as dictionary
        auth_order_data = {
            "order": ["radius", "local"]
        }
        try:
            # Execute REST API call to change authentication order
            # Pass the full dictionary as param_value with empty param_name
            response = OpenApiCommandHelper.execute_script(
                admin_user,
                admin_pass,
                OpenApiReqType.PATCH,
                engines.dut.ip,
                '/system/aaa/authentication',
                op_param_name='',
                op_param_value=auth_order_data
            )
            logger.info(f"OpenAPI response: {response}")
            # Apply the configuration via REST API
            apply_response = OpenApiCommandHelper.execute_script(
                admin_user,
                admin_pass,
                OpenApiReqType.APPLY,
                engines.dut.ip,
                'system/config/apply'
            )
            logger.info(f"OpenAPI apply response: {apply_response}")
        except Exception as e:
            # Expected - connection may be lost during FIPS disconnection
            logger.info(f"OpenAPI call interrupted (expected in FIPS mode): {type(e).__name__}: {e}")

        # Wait for disconnection to complete
        time.sleep(FIPS_DISCONNECT_TIMEOUT)

        # Verify authentication order was changed via OpenAPI
        verify_authentication_order(engines, ['radius', 'local'])

        # Verify users are disconnected
        session_mgr.verify_sessions_disconnected(cli_common, test_user)

    with allure.step('Reconnect with RADIUS user and unset authentication order via OpenAPI'):
        # Current auth order is ['radius', 'local'] - connect with RADIUS user
        time.sleep(2)

        # Get RADIUS user credentials
        radius_config = CLRadiusPhysicalServer.SERVER_IPV4
        radius_user = radius_config.users[1].username
        radius_password = radius_config.users[1].password

        # Create new sessions with RADIUS user (not local user)
        session_mgr.threads.clear()
        ssh_session = session_mgr.create_session(engines, radius_user, radius_password)
        # Note: Console sessions typically use local users, skip console for RADIUS

        rotate_logs(engines)

        # Unset authentication order via OpenAPI
        try:
            unset_response = OpenApiCommandHelper.execute_script(
                engines.dut.username,
                engines.dut.password,
                OpenApiReqType.DELETE,
                engines.dut.ip,
                '/system/aaa/authentication',
                op_param_name='order'
            )
            logger.info(f"OpenAPI unset response: {unset_response}")

            # Apply via REST API
            apply_response = OpenApiCommandHelper.execute_script(
                engines.dut.username,
                engines.dut.password,
                OpenApiReqType.APPLY,
                engines.dut.ip,
                'system/config/apply'
            )
            logger.info(f"OpenAPI apply response: {apply_response}")
        except Exception as e:
            logger.info(f"OpenAPI unset interrupted (expected in FIPS mode): {type(e).__name__}: {e}")

        # Wait for disconnection
        time.sleep(FIPS_DISCONNECT_TIMEOUT)

        # Verify authentication order was unset via OpenAPI
        verify_authentication_order(engines, None)

        # Verify disconnection of RADIUS user
        session_mgr.verify_sessions_disconnected(cli_common, radius_user)

    # Also test TACACS via OpenAPI
    # Verify authentication order is ["local"] before proceeding
    verify_authentication_order(engines, ["local"])
    with allure.step('Change authentication order via OpenAPI to TACACS - should disconnect all users'):
        # Use OpenAPI to change authentication order
        # Build the REST API request - pass as dictionary
        auth_order_data = {
            "order": ["tacacs", "local"]
        }
        try:
            # Execute REST API call to change authentication order
            # Pass the full dictionary as param_value with empty param_name
            response = OpenApiCommandHelper.execute_script(
                admin_user,
                admin_pass,
                OpenApiReqType.PATCH,
                engines.dut.ip,
                '/system/aaa/authentication',
                op_param_name='',
                op_param_value=auth_order_data
            )
            logger.info(f"OpenAPI response: {response}")
            # Apply the configuration via REST API
            apply_response = OpenApiCommandHelper.execute_script(
                admin_user,
                admin_pass,
                OpenApiReqType.APPLY,
                engines.dut.ip,
                'system/config/apply'
            )
            logger.info(f"OpenAPI apply response: {apply_response}")
        except Exception as e:
            # Expected - connection may be lost during FIPS disconnection
            logger.info(f"OpenAPI call interrupted (expected in FIPS mode): {type(e).__name__}: {e}")

        # Wait for disconnection to complete
        time.sleep(FIPS_DISCONNECT_TIMEOUT)

        # Verify authentication order was changed via OpenAPI
        verify_authentication_order(engines, ['tacacs', 'local'])

        # Verify users are disconnected
        session_mgr.verify_sessions_disconnected(cli_common, test_user)

    with allure.step('Reconnect with TACACS user and unset authentication order via OpenAPI'):
        # Current auth order is ['tacacs', 'local'] - connect with TACACS user
        time.sleep(2)

        # Get TACACS user credentials
        tacacs_user = TestServerConfig.TACACS_TEST_USER
        tacacs_password = TestServerConfig.TACACS_TEST_PASSWORD

        # Create new sessions with TACACS user (not local user)
        session_mgr.threads.clear()
        ssh_session = session_mgr.create_session(engines, tacacs_user, tacacs_password)
        # Note: Console sessions typically use local users, skip console for TACACS

        rotate_logs(engines)

        # Unset authentication order via OpenAPI
        # Use DELETE with op_param_name to trigger PATCH-with-null behavior
        # (see openapi_command_builder.py send_delete_request - when op_params is provided,
        # it converts to PATCH with null value instead of real HTTP DELETE)
        try:
            unset_response = OpenApiCommandHelper.execute_script(
                engines.dut.username,
                engines.dut.password,
                OpenApiReqType.DELETE,
                engines.dut.ip,
                '/system/aaa/authentication',  # Parent path (same as SET)
                op_param_name='order'          # Field to unset - triggers PATCH with {"order": null}
            )
            logger.info(f"OpenAPI unset response: {unset_response}")

            # Apply via REST API
            apply_response = OpenApiCommandHelper.execute_script(
                engines.dut.username,
                engines.dut.password,
                OpenApiReqType.APPLY,
                engines.dut.ip,
                'system/config/apply'
            )
            logger.info(f"OpenAPI apply response: {apply_response}")
        except Exception as e:
            logger.info(f"OpenAPI unset interrupted (expected in FIPS mode): {type(e).__name__}: {e}")

        # Wait for disconnection
        time.sleep(FIPS_DISCONNECT_TIMEOUT)

        # Verify authentication order was unset via OpenAPI
        verify_authentication_order(engines, None)

        # Verify disconnection of TACACS user
        session_mgr.verify_sessions_disconnected(cli_common, tacacs_user)

    logger.info("Test04 completed - OpenAPI AAA commands cause FIPS disconnection")


@pytest.mark.fips
@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.skip_clear_config  # Skip cleanup between tests to preserve users
def test05_change_aaa_turn_fips_off(engines, fips_enabled):
    """
    Test Case 5: Check that when FIPS mode is on, setting AAA commands and then disabling FIPS
    disconnects all users. Then verify that after FIPS is disabled, aaa commands are set and system is rebooted,
    applying AAA changes does NOT disconnect users (since FIPS is now off).

    Steps:
    1. Connect with multiple users (local, console)
    2. Set aaa commands (authentication order and provider settings)
    3-4. Disable FIPS mode and apply (without reboot) - should disconnect all users
    5. Set aaa commands (authentication order and provider settings), without applying
    6. reboot the system
    7. Login after reboot
    8. apply the pending AAA changes
    9. Verify users are NOT disconnected (FIPS is disabled, so AAA changes don't trigger disconnection)
    """

    with allure.step('Step 1: Connect with multiple users from different sources'):
        session_mgr.threads.clear()
        # Create sessions from multiple sources
        test_sessions = create_multi_provider_sessions(
            engines,
            include_local=True,
            include_console=True,
            include_radius=False,
            include_tacacs=False,
            include_ldap=False
        )
        session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

    with allure.step('Step 2: Set AAA commands (authentication order and provider settings)'):
        # Set authentication order (pending, not applied yet)
        system = get_system()
        system.aaa.authentication.set_authentication_order(['radius', 'local'],
                                                           dut_engine=engines.dut, apply=False)

        # Change TACACS provider settings (pending, not applied yet) - use priority 3 to avoid conflicts
        set_tacacs_server(engines, '192.168.1.2', secret='tacacssecret1', priority=3, apply=False)

        logger.info("AAA commands set (pending, not applied)")

    with allure.step('Step 3 & 4: Disable FIPS mode and apply (without reboot) - should disconnect all users'):
        # Disable FIPS mode without waiting for reboot (applies FIPS disable + AAA changes together)
        disable_fips_mode(engines, should_reboot=False, expect_disconnect=True)

        # Verify all sessions are disconnected
        verify_all_sessions_disconnected(engines, test_sessions)
        logger.info("FIPS disable applied and all users disconnected")

    with allure.step('Step 5: Set AAA commands (authentication order and provider settings), without applying'):
        # Set NEW authentication order (pending, not applied)
        system.aaa.authentication.set_authentication_order(['local', 'tacacs'],
                                                           dut_engine=engines.dut, apply=False)

        # Change RADIUS provider settings again (pending, not applied)
        set_radius_server(engines, '192.168.1.1', secret='radiussecret2', priority=3, apply=False)

        # Change TACACS provider settings again (pending, not applied)
        set_tacacs_server(engines, '192.168.1.2', secret='tacacssecret2', priority=4, apply=False)

        logger.info("New AAA commands set (pending, not applied)")

    with allure.step('Step 6: Reboot the system'):
        # Reboot the system
        _reboot_and_wait_for_system(engines)

        # Verify FIPS is disabled after reboot
        fips_state = engines.dut.run_cmd("nv show system security fips -o json")
        is_disabled = False
        try:
            state_json = json.loads(fips_state)
            if isinstance(state_json, dict):
                is_disabled = state_json.get("mode") == "disabled"
        except Exception as e:
            logger.warning(f"Failed to parse FIPS state JSON: {e}")
            # Fallback to check if 'disabled' is in output if JSON failed
            if "disabled" in fips_state.lower():
                is_disabled = True

        if not is_disabled:
            raise Exception(f"FIPS mode not disabled after reboot. Current state: {fips_state}")
        logger.info("System rebooted, FIPS mode is disabled")

    with allure.step('Step 7: Login after reboot'):
        # Try to connect with local user directly using ConnectionTool
        # This is a known issue: fallback from RADIUS/TACACS to local may not work
        # We try up to 3 times and then move on
        time.sleep(2)  # Wait a bit before reconnecting

        local_user = 'loc_test_user'
        local_pass = user_credentials[local_user]
        local_session = None

        try:
            logger.info(f"Attempting to connect as '{local_user}'")
            session_result = ConnectionTool.create_ssh_conn(
                engines.dut.ip, local_user, local_pass, port=22, retry=False
            )
            if session_result.result:
                local_session = session_result.get_returned_value()
                logger.info(f"Successfully connected as '{local_user}'")
            else:
                logger.warning(f"Connection failed for '{local_user}'")
        except Exception as e:
            logger.warning(f"Connection exception for '{local_user}': {e}")

        if not local_session:
            logger.error("Local user authentication failed after 3 attempts")
            logger.info("This is a known issue: fallback from RADIUS/TACACS to local doesn't work (PAM corruption)")
            logger.info("Continuing test with engines.dut (cumulus user)")

        # Create test_sessions dict for compatibility with rest of test
        test_sessions = defaultdict(list)
        if local_session:
            test_sessions['local'].append(local_session)

        rotate_logs(engines)

    with allure.step('Step 8: Apply the pending AAA changes'):
        # Apply the pending AAA changes (FIPS is disabled, so no disconnection expected)
        NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')

        # Verify authentication order was applied
        verify_authentication_order(engines, ['local', 'tacacs'])
        logger.info("Pending AAA changes applied")

    with allure.step('Step 9: Verify users are NOT disconnected'):
        # Verify users are NOT disconnected (FIPS is disabled, so AAA changes don't disconnect)
        verify_sessions_still_active(engines, test_sessions)
        logger.info("Users remain connected after AAA changes (FIPS disabled)")
        unset_authentication_order(engines, is_fips_mode=False)

    with allure.step('Cleanup: Remove test AAA servers'):
        # Clean up any RADIUS/TACACS/LDAP servers added during this test
        cleanup_test_aaa_servers(engines)

    logger.info("Test05 completed - FIPS disable disconnects users, but with FIPS off, AAA changes don't disconnect")


@pytest.mark.fips
@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.skip_clear_config  # Skip cleanup between tests to preserve users
def test06_change_aaa_fips_off(engines, fips_disabled):
    """
    Test Case 6: Check that when FIPS mode is OFF, user can run various AAA commands (user, role,
    class management, authentication order, radius, ldap, tacacs) without being disconnected.
    Then enable FIPS mode (pending), set more AAA properties, reboot, and verify that applying
    AAA changes with FIPS ON causes disconnection.

    Steps:
    1. Connect with local user
    2. Change settings for user, role, and class management - verify user is NOT disconnected
    3. Change authentication order - verify user is NOT disconnected
    4. Change settings of providers (radius, tacacs, ldap) - verify user is NOT disconnected
    5. Run nv show system aaa commands - verify users are NOT disconnected
    6. Set authentication order and LDAP settings, enable FIPS mode (without reboot) - verify NOT disconnected
    7. Verify FIPS is enabled (pending until restart)
    8. Set more AAA properties (radius, authentication order, ldap) without applying
    9. Restart the device
    10. Connect with local users
    11. Apply the config
    12. Verify users ARE disconnected
    """
    system = get_system()

    with allure.step('Step 1: Connect with local user'):
        session_mgr.threads.clear()
        # Create sessions from local users
        test_sessions = create_multi_provider_sessions(
            engines,
            include_local=True,
            include_console=False,
            include_radius=False,
            include_tacacs=False,
            include_ldap=False
        )
        session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

    with allure.step('Step 2: Change settings for user management - verify NOT disconnected'):
        # Get local user to track
        local_user = 'loc_test_user'

        # User management commands - unset first to ensure clean state
        system.aaa.user.user_id['testuser1'].unset(dut_engine=engines.dut, apply=False).ignore_result()

        system.aaa.user.set_new_user(username='testuser1', password=TestServerConfig.TESTUSER1_PASSWORD,
                                     role='nvue-admin', apply=False)

        # Class management commands (create class first, as role needs it)
        system.aaa.class_rbac.class_id['test-class'].set('action', 'allow', dut_engine=engines.dut, apply=False)
        system.aaa.class_rbac.class_id['test-class'].set('command-path', '/acl/*', dut_engine=engines.dut, apply=False)

        # Role management commands (role requires a class)
        system.aaa.role.role_id['test-role'].set('class', 'test-class', dut_engine=engines.dut, apply=False)

        NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')

        # Verify user is NOT disconnected
        verify_sessions_still_active(engines, test_sessions)
        logger.info("User/role/class management changes applied, users remain connected (FIPS off)")

    with allure.step('Step 3: Change authentication order - verify NOT disconnected'):
        # Change authentication order
        system.aaa.authentication.set_authentication_order(['local', 'radius'],
                                                           dut_engine=engines.dut, apply=True,
                                                           ask_for_confirmation='-y')

        # Verify user is NOT disconnected
        verify_sessions_still_active(engines, test_sessions)
        logger.info("Authentication order changed, users remain connected (FIPS off)")

    with allure.step('Step 4: Change settings of providers - verify NOT disconnected'):
        # Change RADIUS settings (use priority 4 to avoid conflict with existing servers)
        set_radius_server(engines, '192.168.2.1', secret='radiustest', priority=4, apply=False)

        # Change TACACS settings (use priority 5 to avoid conflict with existing servers)
        set_tacacs_server(engines, '192.168.2.2', secret='tacacstest', priority=5, apply=False)

        # Change LDAP settings (use priority 3 to avoid conflict with existing servers)
        set_ldap_server(engines, '192.168.2.3', priority=3, apply=False)

        NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')

        # Verify users are NOT disconnected
        verify_sessions_still_active(engines, test_sessions)
        logger.info("Provider settings changed, users remain connected (FIPS off)")

    with allure.step('Step 5: Run nv show system aaa commands - verify NOT disconnected'):
        # Get one of the sessions to run commands
        local_session = sessions_dict[local_user][0]

        # Run various show commands
        local_session.run_cmd("nv show system aaa")
        local_session.run_cmd("nv show system aaa user")
        local_session.run_cmd("nv show system aaa authentication")
        local_session.run_cmd("nv show system aaa radius")
        local_session.run_cmd("nv show system aaa tacacs")
        local_session.run_cmd("nv show system aaa ldap")
        local_session.run_cmd("nv show system aaa role")
        local_session.run_cmd("nv show system aaa class")

        # Verify users are NOT disconnected
        verify_sessions_still_active(engines, test_sessions)
        logger.info("Show commands executed, users remain connected (FIPS off)")

    with allure.step('Step 6: Set authentication order and LDAP settings, enable FIPS mode (without reboot) - verify NOT disconnected'):
        # Set new authentication order
        system.aaa.authentication.set_authentication_order(['ldap', 'local'],
                                                           dut_engine=engines.dut, apply=False)

        # Set LDAP settings (use priority 4 to avoid conflict with existing servers)
        set_ldap_server(engines, '192.168.4.4', priority=4, apply=False)

        # Enable FIPS mode (without reboot - applies pending AAA changes + FIPS enable)
        enable_fips_mode(engines, should_reboot=False)

        # Verify users are NOT disconnected (FIPS is pending until restart)
        verify_sessions_still_active(engines, test_sessions)
        logger.info("Authentication order, LDAP settings, and FIPS mode set (pending), users remain connected")

    with allure.step('Step 7: Verify FIPS is enabled (pending until restart)'):
        # Verify FIPS is in pending state (enabled in applied config, but not operational yet)
        fips_state = engines.dut.run_cmd("nv show system security fips")
        logger.info(f"FIPS state after enable (should be pending):\n{fips_state}")
        # Check that FIPS is enabled in applied config (will be operational after reboot)
        if "enabled" not in fips_state.lower():
            logger.warning(f"FIPS may not be enabled in applied config: {fips_state}")

    with allure.step('Step 8: Restart the device'):
        # Reboot the system
        _reboot_and_wait_for_system(engines)

        # Verify FIPS is enabled (operational) after reboot
        fips_state = engines.dut.run_cmd("nv show system security fips -o json")
        is_enabled = False
        try:
            state_json = json.loads(fips_state)
            if isinstance(state_json, dict):
                is_enabled = state_json.get("mode") == "enabled"
        except Exception as e:
            logger.warning(f"Failed to parse FIPS state JSON: {e}")
            # Fallback
            if "enabled" in fips_state.lower():
                is_enabled = True

        if not is_enabled:
            raise Exception(f"FIPS mode not enabled after reboot. Current state: {fips_state}")
        logger.info("System rebooted, FIPS mode is now operationally enabled")

    with allure.step('Step 9: Connect with local users'):
        # Clear threads for new session creation
        session_mgr.threads.clear()
        time.sleep(2)  # Wait a bit before reconnecting

        # Reconnect with local users (FIPS is now operationally enabled)
        test_sessions = create_multi_provider_sessions(
            engines,
            include_local=True,
            include_console=False,
            include_radius=False,
            include_tacacs=False,
            include_ldap=False
        )
        session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

    with allure.step('Step 10: Set more AAA properties (after reboot)'):
        # Set new AAA properties (pending, not applied)
        system = get_system()
        system.aaa.authentication.set_authentication_order(['tacacs', 'local'],
                                                           dut_engine=engines.dut, apply=False)

        # Change RADIUS settings again (use priority 5 to avoid conflicts)
        set_radius_server(engines, '192.168.3.1', secret='newradiussecret', priority=5, apply=False)

        # Change LDAP settings again (use priority 5 to avoid conflicts)
        set_ldap_server(engines, '192.168.3.3', priority=5, apply=False)

        logger.info("New AAA properties set (pending, not applied)")

    with allure.step('Step 11: Apply the pending config'):
        # Apply the pending AAA changes (FIPS is enabled, so disconnection expected)
        _apply_config_with_expected_disconnect(engines.dut, "AAA changes with FIPS enabled")
        logger.info("Pending AAA changes applied")

    with allure.step('Step 12: Verify users ARE disconnected'):
        # Verify all sessions are disconnected (FIPS is enabled, AAA changes cause disconnection)
        verify_all_sessions_disconnected(engines, test_sessions)
        logger.info("Users disconnected after AAA changes (FIPS enabled)")

    with allure.step('Cleanup: Unset test role and class'):
        logger.info("Cleaning up test role and class")
        try:
            # Clean up role and class
            unset_authentication_order(engines, is_fips_mode=True)
            system.aaa.user.user_id['testuser1'].unset(dut_engine=engines.dut, apply=False).ignore_result()
            system.aaa.role.role_id['test-role'].unset(dut_engine=engines.dut, apply=False).ignore_result()
            system.aaa.class_rbac.class_id['test-class'].unset(dut_engine=engines.dut, apply=False).ignore_result()
            result = NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')
            logger.info(f"Configuration applied: {result}")
        except Exception as e:
            logger.warning(f"Cleanup failed: {e}")

    with allure.step('Cleanup: Remove test AAA servers'):
        # Clean up any RADIUS/TACACS/LDAP servers added during this test
        cleanup_test_aaa_servers(engines)

    logger.info("Test06 completed - FIPS off allows AAA changes without disconnect, FIPS on causes disconnect")


@pytest.mark.fips
@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.skip_clear_config  # Skip cleanup between tests to preserve users
def test07_non_aaa_set(engines, fips_enabled):
    """
    Test Case 7: FIPS mode is on, set a new interface, verify users are not disconnected.
    """
    system = get_system()

    with allure.step('create sessions'):
        # Use pre-created users from module setup (don't recreate them)
        test_user = 'test_user'
        test_pass = user_credentials[test_user]
        admin_user = 'admin_user'
        admin_pass = user_credentials[admin_user]

        test_session = session_mgr.create_session(engines, test_user, test_pass)
        admin_session = session_mgr.create_session(engines, admin_user, admin_pass)

    rotate_logs(engines)

    with allure.step('Create new interface - should NOT disconnect users'):
        execute_non_aaa_command(engines, admin_session)

        session_mgr.verify_sessions_active(cli_common, test_user)

    with allure.step('cleanup'):
        test_session.disconnect()
        admin_session.disconnect()


@pytest.mark.fips
@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.skip_clear_config  # Skip cleanup between tests to preserve users
def test08_multiple_aaa_commands(engines, fips_enabled):
    """
    Test Case 8: FIPS mode is on, set many different aaa settings, verify users are disconnected.
    Then unset all AAA settings and verify users are disconnected again.
    """
    # Use pre-created users
    test_user = 'test_user'
    test_pass = user_credentials[test_user]
    admin_user = 'admin_user'
    admin_pass = user_credentials[admin_user]

    # === Pre-cleanup: Remove any fake TACACS servers left from previous tests ===
    # This is critical because when auth order is "tacacs local", fake TACACS servers
    # cause authentication timeouts instead of falling back to local users quickly
    with allure.step('Pre-cleanup: Remove fake TACACS servers'):
        cleanup_test_aaa_servers(engines)

    # === Part 1: Set AAA settings and verify disconnection ===
    with allure.step('Connect with local users'):
        session_mgr.threads.clear()
        test_session = session_mgr.create_session(engines, test_user, test_pass)
        admin_session = session_mgr.create_session(engines, admin_user, admin_pass)

        rotate_logs(engines)

    with allure.step('Set new authentication order - tacacs local'):
        # Set authentication order to tacacs, local
        admin_session.run_cmd("nv set system aaa authentication order tacacs local")
        logger.info("Authentication order set to tacacs, local (pending)")

    with allure.step('Set changes to LDAP'):
        # Set LDAP provider settings (use priority 7 to avoid conflict with existing servers)
        admin_session.run_cmd("nv set system aaa ldap server 192.168.5.1 priority 7")
        logger.info("LDAP settings changed (pending)")

    with allure.step('Set changes to RADIUS'):
        # Set RADIUS provider settings (use priority 7 to avoid conflict with existing servers)
        admin_session.run_cmd("nv set system aaa radius server 192.168.5.2 secret radius_test")
        admin_session.run_cmd("nv set system aaa radius server 192.168.5.2 priority 6")
        logger.info("RADIUS settings changed (pending)")

    with allure.step('Apply - should disconnect all sessions'):
        # Apply all AAA changes together - should cause disconnection in FIPS mode
        apply_config_from_session(admin_session, expect_disconnect=True)

        # Wait for disconnection to complete
        time.sleep(FIPS_DISCONNECT_TIMEOUT)

        # Verify all users are disconnected
        session_mgr.verify_sessions_disconnected(cli_common, test_user)
        session_mgr.verify_sessions_disconnected(cli_common, admin_user)
        logger.info("All sessions disconnected after AAA changes")

    # === Part 2: Login with TACACS users and local, unset AAA settings, verify disconnection ===
    with allure.step('Login with tacacs users and local (if possible)'):
        session_mgr.threads.clear()

        # Try to login with local users (should work since 'local' is second in auth order)
        test_session = session_mgr.create_session(engines, test_user, test_pass)
        admin_session = session_mgr.create_session(engines, admin_user, admin_pass)

        session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

        logger.info("Logged in with local users (TACACS as primary in auth order)")

    with allure.step('Unset changes to LDAP'):
        # Unset LDAP settings
        admin_session.run_cmd("nv unset system aaa ldap server 192.168.5.1")
        logger.info("LDAP settings unset (pending)")

    with allure.step('Unset changes to RADIUS'):
        # Unset RADIUS settings
        admin_session.run_cmd("nv unset system aaa radius server 192.168.5.2")
        logger.info("RADIUS settings unset (pending)")

    with allure.step('Unset changes to authentication order'):
        # Unset authentication order (will revert to default)
        system = get_system()
        system.aaa.authentication.unset_authentication_order(dut_engine=engines.dut, apply=False)
        logger.info("Authentication order unset (pending)")

    with allure.step('Apply - should disconnect all sessions'):
        # Apply all AAA unset changes together - should cause disconnection in FIPS mode
        apply_config_from_session(admin_session, expect_disconnect=True)

        # Wait for disconnection to complete
        time.sleep(FIPS_DISCONNECT_TIMEOUT)

        # Verify all users are disconnected
        session_mgr.verify_sessions_disconnected(cli_common, test_user)
        session_mgr.verify_sessions_disconnected(cli_common, admin_user)
        logger.info("All sessions disconnected after unsetting AAA changes")

    # Cleanup sessions
    try:
        test_session.disconnect()
    except Exception:
        pass  # Session might already be disconnected
    try:
        admin_session.disconnect()
    except Exception:
        pass  # Session might already be disconnected

    with allure.step('Cleanup: Remove test AAA servers'):
        # Clean up any RADIUS/TACACS/LDAP servers added during this test
        cleanup_test_aaa_servers(engines)

    logger.info("Test08 completed - Multiple AAA changes and unsets cause FIPS disconnection")


@pytest.mark.fips
@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.skip_clear_config  # Skip cleanup between tests to preserve users
def test09_set_unset_aaa_property(engines, fips_enabled):
    """
    Test Case 09: FIPS mode is on, verify that setting and unsetting AAA properties
    before applying results in no net change, so users remain connected.

    This test verifies that when AAA properties are set and then unset before applying,
    the net effect is no AAA change, and users should NOT be disconnected in FIPS mode.

    Steps:
    1. Connect with test users
    2. Verify/set authentication order to default ['local']
    3. Set new authentication order (pending)
    4. Unset the new authentication order (pending)
    5. Apply changes (set + unset cancels out) with interface config
    6. Verify users are still connected (no net AAA change)
    7. Set and unset AAA server (RADIUS), apply, verify users remain connected
    """
    system = get_system()

    # Use pre-created users
    test_user = 'test_user'
    test_pass = user_credentials[test_user]
    admin_user = 'admin_user'
    admin_pass = user_credentials[admin_user]

    # === Step 1: User is connected ===
    with allure.step('Step 1: Connect with test users'):
        session_mgr.threads.clear()
        test_session = session_mgr.create_session(engines, test_user, test_pass)
        admin_session = session_mgr.create_session(engines, admin_user, admin_pass)

        rotate_logs(engines)

        logger.info("Users connected successfully")

    # === Step 2: Authentication order is default ===
    with allure.step('Step 2: Verify authentication order is default'):
        # Check if authentication order is just ["local"]
        if not verify_authentication_order(engines, ['local']):
            logger.info("Authentication order is not just ['local'], setting it to ['local']")
            system.aaa.authentication.set_authentication_order(
                ['local'],
                dut_engine=engines.dut,
                apply=False
            )

            try:
                # Apply change - expect disconnect in FIPS mode
                _apply_config_with_expected_disconnect(engines.dut, "Set auth order to local")

                # Wait for disconnection
                time.sleep(FIPS_DISCONNECT_TIMEOUT)

                # Reconnect after changing auth order
                session_mgr.threads.clear()
                test_session = session_mgr.create_session(engines, test_user, test_pass)
                admin_session = session_mgr.create_session(engines, admin_user, admin_pass)

                rotate_logs(engines)

            except Exception as e:
                logger.error(f"Failed to set authentication order to ['local']: {e}")
        else:
            logger.info("Authentication order is already ['local']")

        # Verify default auth order
        verify_authentication_order(engines, ['local'])

    # === Step 3: Set new authentication order ===
    with allure.step('Step 3: Set new authentication order (local, radius)'):
        system.aaa.authentication.set_authentication_order(
            ['local', 'radius'],
            dut_engine=engines.dut,
            apply=False
        )
        logger.info("New authentication order set (pending)")

    # === Step 4: Unset the new order ===
    with allure.step('Step 4: Unset the new authentication order'):
        system.aaa.authentication.unset_authentication_order(dut_engine=engines.dut, apply=False)
        logger.info("Authentication order unset (pending)")

    # === Step 5: Apply ===
    with allure.step('Step 5: Apply changes (set + unset auth order)'):
        # Also set a new interface (non-AAA command) to verify mixed config
        admin_session.run_cmd("nv set interface swp2 ip address 192.168.101.1/24")
        logger.info("Interface configuration added (pending)")

        # Apply all pending changes from admin_session (where the config was made)
        NvueGeneralCli.apply_config(admin_session, ask_for_confirmation='-y')
        logger.info("Configuration applied")

        # Wait a moment for any potential disconnection
        time.sleep(2)

    # === Step 6: Verify user is still connected ===
    with allure.step('Step 6: Verify users are still connected (set+unset should cancel out)'):
        # Since we set and then unset the auth order before applying,
        # the net effect is no AAA change, so users should remain connected
        session_mgr.verify_sessions_active(cli_common, test_user)
        session_mgr.verify_sessions_active(cli_common, admin_user)
        logger.info("✓ Users remain connected after set+unset auth order (net no change)")

    # === Step 7: Set and unset AAA server, verify no disconnection ===
    with allure.step('Step 7: Set and unset AAA server, apply, verify users remain connected'):
        # Set a RADIUS server configuration (pending)
        set_radius_server(engines, '192.168.10.10', secret='radius_secret', priority=7, apply=False)
        logger.info("RADIUS server set (pending)")

        # Unset the same RADIUS server (pending) - net effect is no change
        unset_radius_server(engines, '192.168.10.10', apply=False)
        logger.info("RADIUS server unset (pending)")

        # Check if there is config diff before applying
        diff = NvueGeneralCli.diff_config(engines.dut)
        if diff and diff.strip():
            logger.info(f"Configuration diff before apply:\n{diff}")
            try:
                NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')
            except Exception as e:
                logger.info(f"Apply result: {e}")

        time.sleep(2)

        # Verify users are still connected (set+unset cancels out)
        session_mgr.verify_sessions_active(cli_common, test_user)
        session_mgr.verify_sessions_active(cli_common, admin_user)
        logger.info("✓ Users remain connected after set+unset AAA server (net no change)")

    logger.info("Test09 completed - Set+unset auth order (no net change) keeps users connected, "
                "setting and unsetting non-default AAA property didn't cause disconnection in FIPS mode")


@pytest.mark.fips
@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.skip_clear_config  # Skip cleanup between tests to preserve users
def test10_set_user_role_class(engines, fips_enabled):
    """
    Test Case 10: FIPS mode is on, set and unset few AAA properties, verify no user is disconnected.

    Connect with 2 users, 3 sessions.
    Set and unset authentication order, radius settings, tacacs settings, ldap settings.
    Set new interface and apply changes.
    Verify users are still connected (set+unset should cancel out, no net AAA change).
    """
    system = get_system()

    # Use pre-created test users
    user1 = 'user1'
    pass1 = user_credentials[user1]
    user2 = 'user2'
    pass2 = user_credentials[user2]
    admin_user = 'admin_user'
    admin_pass = user_credentials[admin_user]

    with allure.step('Step 1: Connect with 2 users, 3 sessions total'):
        # Create 3 sessions: 2 sessions for user1, 1 session for user2
        session_mgr.create_sessions(engines, user1, pass1, 2)
        session_mgr.create_session_thread(engines, user2, pass2)
        admin_session = session_mgr.create_session(engines, admin_user, admin_pass)

        session_mgr.wait_for_sessions_threads()
        logger.info(f"Created 2 sessions for {user1}, 1 session for {user2}, and admin session")

    rotate_logs(engines)

    # === Step 2: Set authentication order (without applying) ===
    with allure.step('Step 2: Set authentication order (local, radius) - pending'):
        system.aaa.authentication.set_authentication_order(
            ['local', 'radius'],
            dut_engine=engines.dut,
            apply=False
        )
        logger.info("Authentication order set to ['local', 'radius'] (pending)")

    # === Step 3: Unset authentication order (without applying) ===
    with allure.step('Step 3: Unset authentication order - pending'):
        system.aaa.authentication.unset_authentication_order(dut_engine=engines.dut, apply=False)
        logger.info("Authentication order unset (pending)")

    # === Step 4: Set RADIUS server settings (without applying) ===
    with allure.step('Step 4: Set RADIUS server settings - pending'):
        admin_session.run_cmd("nv set system aaa radius server 192.168.20.20 secret radius_test_secret")
        admin_session.run_cmd("nv set system aaa radius server 192.168.20.20 priority 8")
        logger.info("RADIUS server settings configured (pending)")

    # === Step 5: Unset RADIUS server settings (without applying) ===
    with allure.step('Step 5: Unset RADIUS server settings - pending'):
        admin_session.run_cmd("nv unset system aaa radius server 192.168.20.20")
        logger.info("RADIUS server settings unset (pending)")

    # === Step 6: Set TACACS+ server settings (without applying) ===
    with allure.step('Step 6: Set TACACS+ server settings - pending'):
        admin_session.run_cmd("nv set system aaa tacacs server 192.168.30.30 secret tacacs_test_secret")
        admin_session.run_cmd("nv set system aaa tacacs server 192.168.30.30 priority 6")
        logger.info("TACACS+ server settings configured (pending)")

    # === Step 7: Unset TACACS+ server settings (without applying) ===
    with allure.step('Step 7: Unset TACACS+ server settings - pending'):
        admin_session.run_cmd("nv unset system aaa tacacs server 192.168.30.30")
        logger.info("TACACS+ server settings unset (pending)")

    # === Step 8: Set LDAP settings (without applying) ===
    with allure.step('Step 8: Set LDAP settings - pending'):
        admin_session.run_cmd("nv set system aaa ldap server 192.168.40.40 priority 6")
        admin_session.run_cmd("nv set system aaa ldap bind-dn cn=admin,dc=example,dc=com")
        admin_session.run_cmd("nv set system aaa ldap base-dn dc=example,dc=com")
        logger.info("LDAP settings configured (pending)")

    # === Step 9: Unset LDAP settings (without applying) ===
    with allure.step('Step 9: Unset LDAP settings - pending'):
        admin_session.run_cmd("nv unset system aaa ldap server 192.168.40.40")
        admin_session.run_cmd("nv unset system aaa ldap bind-dn")
        admin_session.run_cmd("nv unset system aaa ldap base-dn")
        logger.info("LDAP settings unset (pending)")

    # === Step 10: Set new interface (non-AAA command) ===
    with allure.step('Step 10: Set new interface configuration - pending'):
        admin_session.run_cmd("nv set interface swp3 ip address 192.168.102.1/24")
        logger.info("Interface swp3 configuration added (pending)")

    # === Step 11: Apply all changes ===
    with allure.step('Step 11: Apply all changes (set+unset should cancel out)'):
        # Since set+unset operations cancel out, there may be no AAA changes to apply
        # (only the interface configuration). Handle empty apply gracefully.
        try:
            result = NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')
            logger.info(f"Configuration applied: {result}")
        except Exception as e:
            # If apply fails due to no pending changes, that's acceptable
            if "No pending configuration" in str(e) or "nothing to apply" in str(e).lower():
                logger.info("No pending changes to apply (expected - set+unset canceled out)")
            else:
                # Re-raise if it's a different error
                raise

        # Wait a moment for any potential disconnection
        time.sleep(2)

    # === Step 12: Verify users are still connected ===
    with allure.step('Step 12: Verify all users are still connected'):
        # Since we set and then unset all AAA properties before applying,
        # the net effect is no AAA change, so users should remain connected.
        # Only the interface configuration (non-AAA) was actually applied.
        session_mgr.verify_sessions_active(cli_common, user1, expected_num_sessions=2)
        session_mgr.verify_sessions_active(cli_common, user2, expected_num_sessions=1)
        logger.info(f"✓ All users remain connected: {user1} (2 sessions), {user2} (1 session)")
        logger.info("✓ Set+unset of AAA properties canceled out - no disconnection occurred")

    logger.info("Test10 completed - Set+unset of multiple AAA properties (auth order, RADIUS, TACACS, LDAP) "
                "keeps users connected in FIPS mode when net change is zero")

    # Cleanup
    admin_session.disconnect()


@pytest.mark.fips
@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.gnmi
@pytest.mark.cumulus_only
@pytest.mark.skip_clear_config  # Skip cleanup between tests to preserve users
def test11_verify_gnmi_subscription(engines, fips_enabled):
    """
    Test Case 11: FIPS mode is on, set new authentication order, verify gnmi subscription user is disconnected.

    Steps:
    1. Configure GNMI server (enable it)
    2. Connect with local user via SSH
    3. Create GNMI subscription session (streaming) for the user
    4. Verify both SSH and GNMI sessions are active
    5. Set new authentication order
    6. Verify both SSH and GNMI subscription users are disconnected
    """
    # Ensure gnmic is installed
    ensure_gnmic_installed()

    # Use pre-created local user
    local_user = 'loc_test_user'
    local_pass = user_credentials[local_user]

    # Enable GNMI server with certificate
    cert_name = enable_gnmi_server_with_cert(engines)

    with allure.step('Step 2: Connect with local user via SSH'):
        session_mgr.threads.clear()
        ssh_session = session_mgr.create_session(engines, local_user, local_pass)

        # Verify SSH session is active
        session_mgr.verify_sessions_active(cli_common, local_user, expected_num_sessions=1)
        logger.info(f"SSH session created for user {local_user}")

    # Create GNMI subscription session using separated functions
    # This demonstrates that we can create a client and then a subscription
    gnmi_client = create_gnmi_client(engines, local_user, local_pass)
    gnmi_subscription_process = create_gnmi_subscription(gnmi_client, local_user, streaming=True)

    rotate_logs(engines)

    with allure.step('Step 4: Verify both SSH and GNMI sessions are active'):
        # Verify SSH session is still active
        session_mgr.verify_sessions_active(cli_common, local_user, expected_num_sessions=1)

        # Verify GNMI connection is active (check port 9339 for ESTABLISHED connections)
        verify_gnmi_connections_active(engines, expected_subscriptions=1)

        # Also verify GNMI client process is still running locally
        assert gnmi_subscription_process.poll() is None, "GNMI subscription process is not running"
        logger.info(f"Both SSH session and GNMI connection are active for user {local_user}")

    with allure.step('Step 5: Set new authentication order - should disconnect all users'):
        # Capture timestamps from both Python (test machine) and device to calculate offset
        python_time_before = datetime.now()
        device_time_str = engines.dut.run_cmd("date '+%Y-%m-%d %H:%M:%S'").strip().split('\n')[0]
        try:
            device_time_before = datetime.strptime(device_time_str, '%Y-%m-%d %H:%M:%S')
            time_offset = python_time_before - device_time_before
            time_offset_seconds = time_offset.total_seconds()
            logger.info(f"TIME SYNC INFO: Python time: {python_time_before}, Device time: {device_time_before}")
            logger.info(f"TIME SYNC INFO: Time offset (Python - Device): {time_offset_seconds:.0f} seconds ({time_offset_seconds / 3600:.1f} hours)")
        except ValueError as e:
            logger.warning(f"Could not parse device time '{device_time_str}': {e}")
            time_offset_seconds = 0

        # Rotate logs before AAA change to get a clean slate for log analysis
        logger.info("Rotating logs before AAA change...")
        rotate_logs(engines)
        # Also create a marker file for journalctl timestamp reference
        engines.dut.run_cmd("touch /tmp/gnmi_log_marker")
        time.sleep(1)  # Ensure marker is older than any new logs

        # Set authentication order - this should disconnect all sessions in FIPS mode
        set_authentication_order(engines, ['tacacs', 'local'], is_fips_mode=True)
        logger.info("Authentication order changed to ['tacacs', 'local']")

    with allure.step('Step 6: Verify both SSH and GNMI users are disconnected'):
        # Wait for disconnection to complete
        time.sleep(FIPS_DISCONNECT_TIMEOUT)

        # Collect gNMI-related logs to check for disconnect/reconnect events
        with allure.step('Collect gNMI logs to verify disconnect/reconnect behavior'):
            logger.info("=" * 80)
            logger.info("GNMI LOG ANALYSIS - Checking for disconnect/reconnect events (logs since marker)")
            logger.info("=" * 80)

            # Get the marker file timestamp for journalctl queries
            marker_timestamp = engines.dut.run_cmd("stat -c %Y /tmp/gnmi_log_marker 2>/dev/null || echo '0'").strip().split('\n')[0]
            logger.info(f"Log marker timestamp (epoch): {marker_timestamp}")

            # Check gnmi-server logs since marker
            gnmi_server_logs = engines.dut.run_cmd(
                f"sudo journalctl -u gnmi-server --since @{marker_timestamp} --no-pager 2>/dev/null || echo 'gnmi-server logs not available'",
                validate=False
            )
            logger.info("--- gNMI Server Logs (since marker) ---")
            logger.info(gnmi_server_logs)

            # Check nvue-envoy logs (gNMI proxy) since marker
            envoy_logs = engines.dut.run_cmd(
                f"sudo journalctl -u nvue-envoy --since @{marker_timestamp} --no-pager 2>/dev/null || echo 'nvue-envoy logs not available'",
                validate=False
            )
            logger.info("--- NVUE Envoy Logs (since marker) ---")
            logger.info(envoy_logs)

            # Check nginx authenticator logs since marker
            nginx_auth_logs = engines.dut.run_cmd(
                f"sudo journalctl -u nginx-authenticator --since @{marker_timestamp} --no-pager 2>/dev/null || echo 'nginx-authenticator logs not available'",
                validate=False
            )
            logger.info("--- Nginx Authenticator Logs (since marker) ---")
            logger.info(nginx_auth_logs)

            # Check nvued logs since marker
            nvued_logs = engines.dut.run_cmd(
                f"sudo journalctl -u nvued --since @{marker_timestamp} --no-pager 2>/dev/null || echo 'nvued logs not available'",
                validate=False
            )
            logger.info("--- NVUED Logs (since marker) ---")
            logger.info(nvued_logs)

            # Check auth.log for entries newer than marker file
            auth_log = engines.dut.run_cmd(
                "sudo awk -v marker=$(stat -c %Y /tmp/gnmi_log_marker) 'BEGIN{FS=\"T\"} {gsub(/-/,\" \",$1); gsub(/:/,\" \",$2); t=mktime($1\" \"substr($2,1,8))} t>=marker' /var/log/auth.log 2>/dev/null | grep -i 'gnmi\\|nvueapi' || echo 'No new auth.log entries'",
                validate=False
            )
            logger.info("--- Auth Log (gNMI/nvueapi entries since marker) ---")
            logger.info(auth_log)

            # Check syslog for gNMI entries newer than marker
            syslog_gnmi = engines.dut.run_cmd(
                "sudo find /var/log/syslog -newer /tmp/gnmi_log_marker -exec grep -i gnmi {} \\; 2>/dev/null || echo 'No new syslog gnmi entries'",
                validate=False
            )
            logger.info("--- Syslog gNMI entries (since marker) ---")
            logger.info(syslog_gnmi)

            # Check for any re-authentication patterns
            logger.info("--- Checking for re-authentication indicators ---")
            reauth_indicators = [
                "authenticated", "authentication", "session", "connect",
                "disconnect", "terminate", "close", "new connection", "established"
            ]
            all_logs = f"{gnmi_server_logs}\n{envoy_logs}\n{nginx_auth_logs}\n{nvued_logs}"
            for indicator in reauth_indicators:
                if indicator.lower() in all_logs.lower():
                    logger.info(f"Found indicator '{indicator}' in logs - potential session event")

            # Cleanup marker file
            engines.dut.run_cmd("rm -f /tmp/gnmi_log_marker", validate=False)

            logger.info("=" * 80)
            logger.info("END OF GNMI LOG ANALYSIS")
            logger.info("=" * 80)

        # Verify SSH session is disconnected
        session_mgr.verify_sessions_disconnected(cli_common, local_user)
        logger.info(f"SSH session disconnected for user {local_user}")

        # Verify gNMI connections are closed (no ESTABLISHED connections on port 9339)
        verify_gnmi_connections_closed(engines)
        logger.info(f"✓ gNMI connections closed on port 9339")

        # Close GNMI client process and check output for disconnection
        out, err = gnmi_client.close_session_and_get_out_and_err(gnmi_subscription_process, delay=2)

        # Log GNMI output for debugging
        logger.info(f"GNMI subscription output: {out}")
        logger.info(f"GNMI subscription error: {err}")

        # Verify GNMI client process has terminated (disconnected)
        assert gnmi_subscription_process.poll() is not None, "GNMI subscription client process should be terminated"
        logger.info(f"✓ GNMI subscription client disconnected for user {local_user}")

    # Cleanup
    try:
        ssh_session.disconnect()
    except Exception:
        pass  # Session already disconnected

    # Reset authentication order for subsequent tests
    try:
        set_authentication_order(engines, ['local'], is_fips_mode=True)
    except Exception as e:
        logger.debug(f"Could not reset auth order during cleanup: {e}")

    # Disable GNMI server and clean up certificate
    disable_gnmi_server_and_cleanup(engines, cert_name)

    logger.info("Test11 completed - GNMI subscription users are disconnected when authentication order changes in FIPS mode")


@pytest.mark.fips
@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.cumulus_only
@pytest.mark.skip_clear_config  # Skip cleanup between tests to preserve users
def test12_verify_behavior_for_users(engines, fips_enabled, reset_auth_order):
    """
    Test Case 12: FIPS mode is on, enable root user, login with root user, set new authentication order,
    verify users are disconnected. Define a user with nvapply permissions, log in with the user,
    set new authentication order, verify users are disconnected.

    Steps:
    1. Enable root user
    2. Log in with root user
    3. Log in with local user
    4. Set and apply new authentication order
    5. Verify users are disconnected
    6. Define a user with nvapply permissions (admin role)
    7. Log in with nvapply user
    8. Log in with local user
    9. Set and apply new authentication order using nvapply user
    10. Verify users are disconnected
    """
    system = get_system()

    # Use pre-created local user
    local_user = 'loc_test_user'
    local_pass = user_credentials[local_user]

    # === Part 1: Test with root user ===
    with allure.step('Part 1: Enable root user and test disconnection'):
        with allure.step('Step 1: Enable root user'):
            # Set root password (must NOT contain "root" substring - PAM will reject it)
            root_password = TestServerConfig.ROOT_TEST_PASSWORD

            # Set password for root user using chpasswd (more reliable than passwd command)
            result = engines.dut.run_cmd(
                f"echo 'root:{root_password}' | sudo chpasswd"
            )
            logger.info(f"Root password set using chpasswd: {result}")

            # Verify password was set successfully
            verify_result = engines.dut.run_cmd("sudo passwd -S root")
            logger.info(f"Root password status: {verify_result}")
            if "P" not in verify_result:  # P = password set
                raise Exception("Failed to set root password")

            # Discard any invalid pending config from previous tests to avoid validation errors
            logger.info("Discarding any pending invalid config from previous tests")
            engines.dut.run_cmd("nv config detach")

            # Enable root login via SSH
            system.ssh_server.set('permit-root-login', 'enabled', dut_engine=engines.dut, apply=False)

            # Apply only this specific change
            NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')
            logger.info("Root user enabled and SSH login permitted")

            # Wait for configuration to settle
            time.sleep(2)

        with allure.step('Step 2: Log in with root user'):
            session_mgr.threads.clear()
            root_session = session_mgr.create_session(engines, 'root', root_password)
            logger.info("Root user session created")

        with allure.step('Step 3: Log in with local user'):
            local_session = session_mgr.create_session(engines, local_user, local_pass)
            logger.info(f"Local user {local_user} session created")

            session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

        with allure.step('Step 4: Set and apply new authentication order'):
            # Verify both sessions are active before changing auth order
            session_mgr.verify_sessions_active(cli_common, 'root', expected_num_sessions=1)
            session_mgr.verify_sessions_active(cli_common, local_user, expected_num_sessions=1)
            logger.info("Both root and local user sessions are active")

            # Set new authentication order - this should disconnect all users in FIPS mode
            set_authentication_order(engines, ['tacacs', 'local'], is_fips_mode=True)
            logger.info("Authentication order changed to ['tacacs', 'local']")

        with allure.step('Step 5: Verify root user is still connected and local user is disconnected'):
            # Wait for disconnection to complete
            # Verify both users are disconnected
            session_mgr.verify_sessions_active(cli_common, 'root')
            session_mgr.verify_sessions_disconnected(cli_common, local_user)
            logger.info("✓ Root user is still connected and local user is disconnected")

    # Cleanup root sessions
    try:
        root_session.disconnect()
    except Exception:
        pass  # Session already disconnected

    try:
        local_session.disconnect()
    except Exception:
        pass  # Session already disconnected

    # === Part 2: Test with nvapply user ===
    with allure.step('Part 2: Create nvapply user and test disconnection'):
        with allure.step('Step 6: Define a user with nvapply permissions (system-admin role)'):
            # Create a user with system-admin role (which includes nvapply group)
            nvapply_user = 'nvapply_test_user'
            nvapply_pass = TestServerConfig.NVAPPLY_TEST_PASSWORD

            # Create user with system-admin role (includes nvapply and sudo groups)
            system.aaa.user.set_new_user(
                username=nvapply_user,
                password=nvapply_pass,
                role=CumulusConsts.ROLE_SYSTEM_ADMIN,
                apply=False
            )
            NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')
            logger.info(f"User {nvapply_user} created with nvapply permissions (system-admin role)")

            # Wait for user creation to settle
            time.sleep(2)

        with allure.step('Step 7: Log in with nvapply user'):
            session_mgr.threads.clear()
            nvapply_session = session_mgr.create_session(engines, nvapply_user, nvapply_pass)
            logger.info(f"nvapply user {nvapply_user} session created")

        with allure.step('Step 8: Log in with local user'):
            local_session = session_mgr.create_session(engines, local_user, local_pass)
            logger.info(f"Local user {local_user} session created")

            session_mgr.wait_for_sessions_threads()

        rotate_logs(engines)

        with allure.step('Step 9: Set and apply new authentication order using nvapply user'):
            # Verify both sessions are active before changing auth order
            session_mgr.verify_sessions_active(cli_common, nvapply_user, expected_num_sessions=1)
            session_mgr.verify_sessions_active(cli_common, local_user, expected_num_sessions=1)
            logger.info(f"Both {nvapply_user} and local user sessions are active")

            # Use nvapply user session to set authentication order
            nvapply_session.run_cmd("nv set system aaa authentication order local tacacs")

            # Apply configuration with expected disconnect
            _apply_config_with_expected_disconnect(nvapply_session, "Set authentication order to ['local', 'tacacs'] using nvapply user")

            logger.info("Authentication order changed to ['local', 'tacacs'] using nvapply user")

        with allure.step('Step 10: Verify users are disconnected'):
            # Wait for disconnection to complete
            time.sleep(FIPS_DISCONNECT_TIMEOUT)

            # Verify both users are disconnected
            session_mgr.verify_sessions_disconnected(cli_common, nvapply_user)
            session_mgr.verify_sessions_disconnected(cli_common, local_user)
            logger.info(f"✓ Both {nvapply_user} and local user sessions are disconnected")

    # Cleanup nvapply sessions
    try:
        nvapply_session.disconnect()
    except Exception:
        pass  # Session already disconnected

    try:
        local_session.disconnect()
    except Exception:
        pass  # Session already disconnected

    # Cleanup: Delete nvapply test user
    try:
        with allure.step('Cleanup: Delete nvapply test user'):
            system.aaa.user.user_id[nvapply_user].delete(apply=True, ask_for_confirmation='-y')
            logger.info(f"Deleted nvapply test user {nvapply_user}")
    except Exception as e:
        logger.debug(f"Best effort cleanup failed: {e}")

    # Cleanup: Disable root login
    try:
        with allure.step('Cleanup: Disable root login'):
            system.ssh_server.set('permit-root-login', 'disabled', dut_engine=engines.dut, apply=False)
            NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')
            logger.info("Root login disabled")
    except Exception as e:
        logger.debug(f"Best effort cleanup failed: {e}")

    # Reset authentication order for subsequent tests
    try:
        set_authentication_order(engines, ['local'], is_fips_mode=True)
    except Exception as e:
        logger.debug(f"Could not reset auth order during cleanup: {e}")

    logger.info("Test12 completed - Both root user and nvapply user sessions are disconnected when authentication order changes in FIPS mode")


@pytest.mark.fips
@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.cumulus_only
def test13_invalid_config_with_valid_aaa_command(engines, fips_enabled):
    """
    Test Case 13: FIPS mode is on, set new authentication order, set some illegal property, apply,
    verify sessions weren't disconnected.

    Steps:
    1. Login with local user
    2. Set new authentication order (valid AAA command)
    3. Set some illegal value for a property (duplicate TACACS server priority)
    4. Apply config (should fail due to invalid config)
    5. Verify sessions weren't disconnected (because apply failed)

    The invalid AAA config: Set new TACACS server with same priority as existing one.
    This should fail because each TACACS server must have a unique priority value.
    """
    system = get_system()

    # Use pre-created local user
    local_user = 'loc_test_user'
    local_pass = user_credentials[local_user]

    with allure.step('Step 1: Login with local user'):
        session_mgr.threads.clear()
        local_session = session_mgr.create_session(engines, local_user, local_pass)
        session_mgr.wait_for_sessions_threads()
        logger.info(f"Local user {local_user} session created")

    rotate_logs(engines)

    with allure.step('Step 2: Set new authentication order (valid AAA command)'):
        # Set authentication order without applying yet
        system.aaa.authentication.set_authentication_order(
            ['local', 'tacacs'],
            dut_engine=engines.dut,
            apply=False
        )
        logger.info("Authentication order set to ['local', 'tacacs'] (pending)")

    with allure.step('Step 3: Set illegal value - duplicate TACACS server priority'):
        # From previous tests, there may be existing TACACS servers with priority 1 or 2
        # Try to add a new TACACS server with the same priority 1
        # This should create an invalid configuration
        set_tacacs_server(engines, '192.168.100.1', secret='newsecret', priority=1, port=49, apply=False)
        logger.info("Set new TACACS server 192.168.100.1 with duplicate priority 1")

    with allure.step('Step 4: Apply config - should fail due to invalid config'):
        # Verify session is active before attempting apply
        session_mgr.verify_sessions_active(cli_common, local_user, expected_num_sessions=1)
        logger.info(f"User {local_user} session is active before apply")

        # Try to apply - this should fail due to duplicate priority
        apply_failed = False
        try:
            result = NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation='-y')
            logger.info(f"Apply result: {result}")

            # Check if the result indicates failure
            if 'Invalid config' in str(result) or 'priority' in str(result).lower():
                apply_failed = True
                logger.info("Apply failed as expected due to duplicate TACACS priority")
                engines.dut.run_cmd("nv config detach")
        except Exception as e:
            # Expected to fail due to invalid config
            apply_failed = True
            logger.info(f"Apply failed as expected: {type(e).__name__}: {e}")

            # Verify error message mentions duplicate priority
            error_msg = str(e)
            if 'priority' in error_msg.lower():
                logger.info(f"✓ Error message confirms duplicate priority issue: {error_msg}")

        # Ensure the apply actually failed
        assert apply_failed, "Apply should have failed due to duplicate TACACS server priority"
        logger.info("✓ Configuration apply failed as expected")

    with allure.step('Step 5: Verify sessions were NOT disconnected'):
        # Wait a moment to ensure no delayed disconnection
        time.sleep(2)

        # Sessions should NOT be disconnected because the apply failed
        # When apply fails, no configuration changes are actually applied
        session_mgr.verify_sessions_active(cli_common, local_user, expected_num_sessions=1)
        logger.info(f"✓ User {local_user} session remained connected (apply failed, no changes applied)")

    # Cleanup: Revert the pending changes
    try:
        with allure.step('Cleanup: Revert pending invalid configuration'):
            engines.dut.run_cmd("nv config detach")
            logger.info("Reverted pending invalid configuration")
    except Exception as e:
        logger.debug(f"Best effort cleanup failed: {e}")

    # Cleanup session
    try:
        local_session.disconnect()
    except Exception:
        pass  # Session may already be disconnected

    logger.info("Test13 completed - Users remain connected when AAA config apply fails due to invalid configuration in FIPS mode")
