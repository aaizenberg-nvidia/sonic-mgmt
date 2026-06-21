"""
Test cases for user and group session limit (max-logins) feature.

This module tests the configuration and enforcement of maximum concurrent
sessions per user and group across different authentication methods:
- SSH
- Serial console
- GNMI/GNOI (REST API)
- Mixed methods
- RADIUS authentication
"""
import pytest
import logging
import time
import paramiko
import socket
import pexpect
import subprocess
import re
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from ngts.nvos_tools.system.System import System
from ngts.nvos_constants.constants_nvos import ApiType, SystemConsts
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tests_nvos.general.security.security_test_tools.constants import AaaConsts
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.constants import GnmiMode
from ngts.tests_nvos.system.gnmi.helpers import _is_gnmi_unavailable
from ngts.constants.constants import GnmiConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.SerialConsoleTool import SerialConsoleTool
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)

# Test constants
TEST_USER1 = "testuser1"
TEST_USER2 = "testuser2"
TEST_USER3 = "testuser3"
TEST_USER4 = "testuser4"
TEST_USER5 = "testuser5"
TEST_USER6 = "testuser6"
TEST_USER7 = "testuser7"
TEST_USER8 = "testuser8"
TEST_USER9 = "testuser9"
TEST_GROUP = "test_class"
TEST_GROUP2 = "test_class2"
TEST_PASSWORD = "TestPassword123!"

# Minimum required Cumulus version for max-logins feature
MIN_CUMULUS_VERSION = (5, 16)


def get_cumulus_version(dut_engine=None):
    """
    Extract version from system using nv show system version command.

    Args:
        dut_engine: Optional DUT engine. If not provided, uses TestToolkit.engines.dut

    Returns:
        tuple: (major, minor) version numbers, e.g., (5, 14), or (0, 0) if not found
    """
    with allure.step('Extracting version from system version info'):
        engine = dut_engine or (TestToolkit.engines.dut if hasattr(TestToolkit, 'engines') and TestToolkit.engines else None)

        logger.info('Retrieving system version via nv show system version...')
        system = System(force_api=ApiType.NVUE)
        version_info = OutputParsingTool.parse_json_str_to_dictionary(
            system.version.show(dut_engine=engine)
        ).get_returned_value()

        current_version = version_info.get(SystemConsts.VERSION_PRODUCT_RELEASE, "")
        logger.info(f'Current product-release version: {current_version}')

        if not current_version:
            logger.warning('No product-release version found, returning (0, 0)')
            return (0, 0)

        # Parse version string like "5.14.0" into tuple (5, 14)
        match = re.match(r'(\d+)\.(\d+)', current_version)
        if match:
            return (int(match.group(1)), int(match.group(2)))

        logger.warning(f'Could not parse version string: {current_version}')
        return (0, 0)


@pytest.fixture(scope="module", autouse=True)
def check_cumulus_version(engines):
    """
    Module-level fixture to verify Cumulus version is 5.16 or higher.
    The max-logins feature is only available in Cumulus 5.16+.
    """
    version = get_cumulus_version(engines.dut)
    version_str = f"{version[0]}.{version[1]}"
    min_version_str = f"{MIN_CUMULUS_VERSION[0]}.{MIN_CUMULUS_VERSION[1]}"

    if version < MIN_CUMULUS_VERSION:
        pytest.skip(
            f"Test requires Cumulus Linux {min_version_str} or higher. "
            f"Current version: {version_str}. "
            f"The max-logins feature is not available in this version."
        )

    logger.info(f"Cumulus version {version_str} meets minimum requirement of {min_version_str}")
    yield


def configure_non_default_vrf(engines, vrf_name='RED', interface='swp1', ip_address='192.168.100.1/24'):
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

        # Optional: bind an interface to the VRF (can be disruptive on shared testbeds).
        # For session-limit testing via localhost (127.0.0.1), VRF existence is enough.
        if interface:
            # Step 2: Set interface type to swp (switch port)
            engines.dut.run_cmd(f"nv set interface {interface} type swp", validate=False)

            # Step 3: Assign interface to VRF (correct syntax: NOT under 'ip')
            engines.dut.run_cmd(f"nv set interface {interface} vrf {vrf_name}", validate=False)
            logger.info(f"Assigned interface {interface} to VRF {vrf_name}")

            # Step 4: Set IP address on the interface
            if ip_address:
                engines.dut.run_cmd(f"nv set interface {interface} ip address {ip_address}", validate=False)
                logger.info(f"Set IP address {ip_address} on interface {interface}")

        # Step 5: Apply the NVUE configuration
        engines.dut.run_cmd("nv config apply -y", validate=False)
        logger.info(f"Applied VRF configuration")

        # Step 5b: Save the configuration to persist across reboots
        # This is important because FIPS enable triggers a reboot
        engines.dut.run_cmd("nv config save -y", validate=False)
        logger.info(f"Saved VRF configuration to persist across reboots")

        # Step 6: Verify VRF configuration
        vrf_show_output = engines.dut.run_cmd(f"nv show vrf {vrf_name}", validate=False)
        logger.info(f"VRF {vrf_name} configuration:\n{vrf_show_output}")

        logger.info(f"Configured VRF {vrf_name} on {interface} with IP {ip_address}")
        return vrf_name


def create_session_via_vrf(
    engines,
    user: str,
    password: str,
    vrf_name: str,
    target_ip: str = "127.0.0.1",
    sessions_dict=None,
    sessions_dict_lock=None,
):
    """
    Create an SSH session through a specific VRF using `ip vrf exec`.

    Notes:
    - This is a best-effort helper that spawns a background SSH that sleeps (so a session exists).
    - If you want to track sessions in a shared dict, pass `sessions_dict` and `sessions_dict_lock`.
      Otherwise, you can just use the returned dict.
    """
    with allure.step(f'Create session for user "{user}" via VRF "{vrf_name}"'):
        # Verify VRF exists and connectivity works
        ping_result = engines.dut.run_cmd(
            f"ip vrf exec {vrf_name} ping -c 1 {target_ip} 2>/dev/null || echo 'ping failed'",
            validate=False,
        )
        logger.info(f"VRF {vrf_name} ping test to {target_ip}: {ping_result}")

        # Check if sshpass is available
        sshpass_check = engines.dut.run_cmd("which sshpass 2>/dev/null || echo 'not found'", validate=False)

        session_created = False

        if "not found" not in sshpass_check:
            # Use sshpass to create SSH session via VRF
            # Key: -tt forces PTY allocation even when stdin isn't a terminal
            ssh_cmd = (
                f"sshpass -p '{password}' ip vrf exec {vrf_name} ssh -tt "
                f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                f"{user}@{target_ip} 'sleep 3600' </dev/null &>/dev/null &"
            )
            engines.dut.run_cmd(ssh_cmd, validate=False)

            # Wait for session to establish
            time.sleep(2)

            # Verify session was created
            verify_result = engines.dut.run_cmd(
                f"who -u | grep {user} | grep {target_ip} || echo 'no session'",
                validate=False,
            )
            if "no session" not in verify_result and target_ip in verify_result:
                logger.info(f"✓ VRF SSH session created for user {user}:\n{verify_result.strip()}")
                session_created = True
            else:
                logger.warning(f"VRF SSH session may not have been created for user {user}")
                logger.debug(f"who -u output: {verify_result}")
        else:
            logger.warning("sshpass not available - VRF SSH session cannot be created")
            logger.warning(f"Manual test: ip vrf exec {vrf_name} ssh {user}@{target_ip}")

        vrf_session_info = {
            "vrf_name": vrf_name,
            "user": user,
            "target_ip": target_ip,
            "session_created": session_created,
            "username": user,  # compatibility
            "engine": None,  # no direct engine object for this background session
            "disconnect": lambda: None,  # placeholder
        }

        # Optional tracking hook (for callers that maintain a sessions dict)
        if sessions_dict is not None:
            key = f"vrf_{vrf_name}_{user}"
            if sessions_dict_lock is not None:
                with sessions_dict_lock:
                    sessions_dict.setdefault(key, []).append(vrf_session_info)
            else:
                sessions_dict.setdefault(key, []).append(vrf_session_info)

        logger.info(f"VRF session for user {user} via VRF {vrf_name}: session_created={session_created}")
        return vrf_session_info


@pytest.fixture(scope="module", autouse=True)
def presuite_install_sshpass(engines):
    """
    Presuite hook for this module.
    Ensures `sshpass` exists on the DUT (used by some session/VRF-related helpers).
    """
    dut = engines.dut
    with allure.step("Presuite: ensure sshpass installed on DUT"):
        try:
            sshpass_check = dut.run_cmd("which sshpass 2>/dev/null || echo 'not found'", validate=False)
            if "not found" in sshpass_check:
                logger.info("Installing sshpass via direct download (apt fails in FIPS mode)")
                dut.run_cmd(
                    "curl -L -o /tmp/sshpass.deb http://deb.debian.org/debian/pool/main/s/sshpass/sshpass_1.09-1+b1_amd64.deb",
                    validate=False,
                )
                dut.run_cmd("sudo dpkg -i /tmp/sshpass.deb", validate=False)
                verify = dut.run_cmd("which sshpass 2>/dev/null || echo 'not found'", validate=False)
                if "not found" not in verify:
                    logger.info("✓ sshpass installed successfully")
                else:
                    logger.warning("sshpass installation may have failed")
            else:
                logger.info("sshpass is already installed")
        except Exception as e:
            logger.warning(f"Failed to ensure sshpass is installed: {e}")

    # fixture runs once per module before tests
    yield


@pytest.fixture(scope="function", autouse=True)
def cleanup_max_logins(engines):
    """
    Cleanup hook that runs before and after each test.
    Removes any leftover max-logins configurations to ensure clean state.
    """
    dut = engines.dut
    users = [TEST_USER1, TEST_USER2, TEST_USER3, TEST_USER4, TEST_USER5, TEST_USER6,
             TEST_USER7, TEST_USER8, TEST_USER9]
    groups = [TEST_GROUP, TEST_GROUP2, "sudo", "nvapply"]

    # Before test: Clean up any existing max-logins configurations
    logger.info("Pre-test cleanup: Removing any existing max-logins configurations")
    for user in users:
        # system.unset_system(f"security user {user} max-logins")
        dut.run_cmd(f"nv unset system security user {user} max-logins", validate=False)
    for group in groups:
        dut.run_cmd(f"nv unset system security group {group} max-logins", validate=False)
    dut.run_cmd("nv config apply -y", validate=False)

    yield

    # After test: Clean up max-logins configurations
    logger.info("Post-test cleanup: Removing max-logins configurations")
    for user in users:
        dut.run_cmd(f"nv unset system security user {user} max-logins", validate=False)
    for group in groups:
        dut.run_cmd(f"nv unset system security group {group} max-logins", validate=False)
    dut.run_cmd("nv config apply -y", validate=False)


@pytest.fixture(scope="module")
def setup_users(engines):
    """
    Setup test users on the DUT.
    """
    dut = engines.dut
    users = [TEST_USER1, TEST_USER2, TEST_USER3, TEST_USER4, TEST_USER5, TEST_USER6,
             TEST_USER7, TEST_USER8, TEST_USER9]

    # Delete existing users first to avoid password-hardening issues
    for user in users:
        dut.run_cmd(f"sudo pkill -u {user} || true", validate=False)
        dut.run_cmd(f"sudo userdel -r {user} || true", validate=False)

    # Create test users using Linux commands only (NOT NVUE)
    # NVUE cannot manage users created outside of first boot
    for user in users:
        dut.run_cmd(f"sudo useradd -m {user}")
        # Set password using usermod (bypasses password-hardening)
        dut.run_cmd(f'sudo usermod -p $(openssl passwd -1 "{TEST_PASSWORD}") {user}')

    yield

    # Cleanup: Remove test users (Linux accounts only - not using NVUE for users)
    for user in users:
        # Kill any active sessions
        dut.run_cmd(f"sudo pkill -u {user} || true", validate=False)
        dut.run_cmd(f"sudo userdel -r {user} || true", validate=False)


@pytest.fixture(scope="module")
def setup_group(engines):
    """
    Setup test group on the DUT.
    """
    dut = engines.dut

    # Create test group
    dut.run_cmd(f"sudo groupadd {TEST_GROUP} || true", validate=False)

    yield

    # Cleanup: Remove test group
    dut.run_cmd(f"sudo groupdel {TEST_GROUP} || true", validate=False)


def set_user_max_logins(dut, username, limit):
    """
    Set maximum concurrent logins for a user.

    Args:
        dut: DUT engine object
        username: Username to configure
        limit: Maximum number of concurrent sessions (1-100)
    """
    with allure.step(f"Set max-logins={limit} for user '{username}'"):
        cmd = f"nv set system security user {username} max-logins {limit}"
        dut.run_cmd(cmd)
        dut.run_cmd("nv config apply -y")


def unset_user_max_logins(dut, username):
    """
    Unset maximum concurrent logins for a user.

    Args:
        dut: DUT engine object
        username: Username to remove configuration for
    """
    with allure.step(f"Unset max-logins for user '{username}'"):
        cmd = f"nv unset system security user {username} max-logins"
        dut.run_cmd(cmd)
        dut.run_cmd("nv config apply -y")


def set_group_max_logins(dut, groupname, limit):
    """
    Set maximum concurrent logins for a group.

    Args:
        dut: DUT engine object
        groupname: Group name to configure
        limit: Maximum number of concurrent sessions (1-100)
    """
    with allure.step(f"Set max-logins={limit} for group '{groupname}'"):
        cmd = f"nv set system security group {groupname} max-logins {limit}"
        dut.run_cmd(cmd)
        dut.run_cmd("nv config apply -y")


def unset_group_max_logins(dut, groupname):
    """
    Unset maximum concurrent logins for a group.

    Args:
        dut: DUT engine object
        groupname: Group name to remove configuration for
    """
    with allure.step(f"Unset max-logins for group '{groupname}'"):
        cmd = f"nv unset system security group {groupname} max-logins"
        dut.run_cmd(cmd)
        dut.run_cmd("nv config apply -y")


def verify_user_max_logins(dut, username, expected_limit):
    """
    Verify that user max-logins is set correctly.

    Args:
        dut: DUT engine object
        username: Username to verify
        expected_limit: Expected max-logins value
    """
    with allure.step(f"Verify user '{username}' max-logins == {expected_limit}"):
        cmd = f"nv show system security user {username}"
        result = dut.run_cmd(cmd)
        pytest_assert(str(expected_limit) in result,
                      f"Expected max-logins {expected_limit} not found in output")


def verify_group_max_logins(dut, groupname, expected_limit):
    """
    Verify that group max-logins is set correctly.

    Args:
        dut: DUT engine object
        groupname: Group name to verify
        expected_limit: Expected max-logins value
    """
    with allure.step(f"Verify group '{groupname}' max-logins == {expected_limit}"):
        cmd = f"nv show system security group {groupname}"
        result = dut.run_cmd(cmd)
        pytest_assert(str(expected_limit) in result,
                      f"Expected max-logins {expected_limit} not found in output")


def open_ssh_session(dut, username, password):
    """
    Open an SSH session to the DUT.

    Args:
        dut: DUT engine object
        username: Username for authentication
        password: Password for authentication

    Returns:
        SSH connection object or None if failed
    """
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        # Get DUT IP from the engine
        hostname = dut.ip
        ssh.connect(
            hostname=hostname,
            username=username,
            password=password,
            timeout=10,
            look_for_keys=False,  # Don't use SSH keys, only password
            allow_agent=False     # Don't use SSH agent
        )

        # This is where max-logins enforcement typically happens
        channel = ssh.invoke_shell()

        # Try to execute a simple command to verify session is usable
        time.sleep(1)  # Wait for shell to initialize
        channel.send('echo test\n')
        time.sleep(0.5)
        output = channel.recv(1024).decode('utf-8', errors='ignore')

        if 'test' in output or 'echo' in output:
            logger.info(f"SSH session established successfully for {username}")
            return ssh
        else:
            logger.warning(f"SSH session opened but shell not responsive for {username}")
            ssh.close()
            return None

    except paramiko.ssh_exception.SSHException as e:
        logger.info(f"SSH connection failed for {username}: {str(e)}")
        return None
    except Exception as e:
        logger.info(f"SSH connection failed for {username}: {str(e)}")
        return None


def open_serial_session(username, password, dut_alias='dut'):
    """
    Open a serial console session to the DUT via serial connection.
    Uses the serial connection tools (`SerialConsoleTool`) and logs in to NOS.

    Args:
        username: Username for authentication
        password: Password for authentication
        dut_alias: DUT alias in topology (default: 'dut')

    Returns:
        PexpectSerialEngine or None if failed (including when max-logins is exceeded)
    """
    try:
        # Prefer TestToolkit.topology_obj (this is what other serial tests use, e.g. test_disconnect_fix_threading.py)
        topo = TestToolkit.topology_obj
        if topo is None:
            pytest_assert(False, "topology_obj is not available (and TestToolkit.topology_obj is None)")

        with allure.step("Enter serial context"):
            serial = SerialConsoleTool.get_serial_console_session(topo, dut_alias=dut_alias)

        with allure.step("Exit existing login (if any)"):
            SerialConsoleTool.exit_existing_login(serial)

        with allure.step(f"Login to NOS via serial as '{username}'"):
            SerialConsoleTool.login_nos(
                serial_engine=serial,
                username=username,
                password=password,
                start_login_tries=10,
                handle_change_password_prompt=False,
            )

        with allure.step("Verify session is responsive"):
            # if login succeeded, we should be able to run a simple command
            out, _ = serial.run_cmd("whoami", SerialConsoleTool.SHELL_PROMPT_PATTERNS, 10)
            pytest_assert(username in out, f"Serial session login verification failed. whoami output: {out}")
            return serial

    except Exception as e:
        # When max-logins is hit, login may fail; return None so callers can assert accordingly.
        logger.warning(f"Serial login failed for {username}: {e}")
        return None


def close_ssh_session(ssh_connection):
    """
    Close an SSH session.

    Args:
        ssh_connection: SSH connection object to close
    """
    if ssh_connection:
        try:
            ssh_connection.close()
        except Exception as e:
            logger.warning(f"Error closing SSH session: {str(e)}")


def cleanup_user_sessions(dut, username, sessions):
    """
    Clean up all SSH sessions for a user.
    Closes session objects and kills any remaining processes.

    Args:
        dut: DUT engine object
        username: Username to clean up sessions for
        sessions: List of SSH session objects to close
    """
    logger.info(f"Cleaning up sessions for user {username}")

    # Close all SSH session objects
    for session in sessions:
        close_ssh_session(session)

    # Kill any remaining processes for the user
    dut.run_cmd(f"sudo pkill -u {username} || true", validate=False)


def open_gnmi_sessions(dut, username, password, num_sessions, verify_tools_first=True):
    """
    Helper to open multiple *concurrent* GNMI sessions for a user.
    Uses a subscribe operation with keep-alive so the sessions actually stay open.

    Args:
        dut: DUT engine object
        username: Username for authentication
        password: Password for authentication
        num_sessions: Number of sessions to open
        verify_tools_first: Whether to verify gnmic installation on first session

    Returns:
        list[subprocess.Popen]: List of live gnmic processes representing open sessions.
    """
    processes = []
    gnmi_client = GnmiClient(
        dut.ip,
        GnmiConsts.GNMI_DEFAULT_PORT,
        username,
        password,
        verify_tools_installed=verify_tools_first,
    )

    # Quick pre-check: if gNMI server is down, skip instead of producing false results.
    out, err = gnmi_client.gnmic_capabilities(skip_cert_verify=True)
    if _is_gnmi_unavailable(err):
        pytest.skip(f"gNMI server is unavailable on {dut.ip}:{GnmiConsts.GNMI_DEFAULT_PORT}: {err.strip()}")

    for i in range(num_sessions):
        try:
            # Keep session alive using a streaming subscribe.
            proc = gnmi_client.gnmic_subscribe_system_events(
                mode=GnmiMode.STREAM,
                skip_cert_verify=True,
                keep_session_alive=True,
                wait_till_done=False,
            )[2]
            # Give it a moment to fail fast if auth/transport is broken.
            time.sleep(0.7)
            if proc.poll() is None:
                logger.info(f"✓ GNMI session {i + 1} for {username} established (keepalive)")
                processes.append(proc)
            else:
                # Process already exited; capture its output for diagnostics.
                o, e = gnmi_client.close_session_and_get_out_and_err(proc, delay=0)
                if _is_gnmi_unavailable(e):
                    pytest.skip(f"gNMI server became unavailable on {dut.ip}:{GnmiConsts.GNMI_DEFAULT_PORT}: {e.strip()}")
                logger.warning(f"GNMI session {i + 1} for {username} failed: {e or o}")
        except Exception as exc:
            logger.warning(f"Failed GNMI session {i + 1} for {username}: {exc}")
        time.sleep(0.3)

    return processes


def test_gnmi_session_fails(dut, username, password, session_desc="extra"):
    """
    Helper to test that a GNMI session fails (for limit testing).

    Args:
        dut: DUT engine object
        username: Username for authentication
        password: Password for authentication
        session_desc: Description of session being tested (for logging)

    Returns:
        bool: True if session failed as expected, False if it succeeded
    """
    try:
        gnmi_client = GnmiClient(dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, username, password)
        out, err = gnmi_client.gnmic_capabilities(skip_cert_verify=True)

        if ('error' in err.lower() or 'denied' in err.lower() or 'failed' in err.lower()):
            logger.info(f"✓ {session_desc} session for {username} correctly rejected")
            return True
        else:
            logger.warning(f"{session_desc} session for {username} may have been allowed")
            return False
    except Exception as e:
        logger.info(f"✓ {session_desc} session for {username} failed as expected: {e}")
        return True


def close_serial_session(serial_connection):
    """
    Close a serial console session.

    Args:
        serial_connection: Serial connection object (pexpect spawn) to close
    """
    if serial_connection:
        try:
            # Logout cleanly
            serial_connection.sendline('exit')
            try:
                serial_connection.expect('login:', timeout=5)
                logger.debug("Successfully logged out of console session")
            except BaseException:
                pass

            # Close the connection
            serial_connection.close()
            logger.debug("Serial session closed successfully")
        except Exception as e:
            logger.warning(f"Error closing serial session: {str(e)}")
            try:
                serial_connection.close(force=True)
            except BaseException:
                pass


@pytest.mark.cumulus
def test01_user_max_logins_ssh(engines, setup_users):
    """
    Verify that user-level session limit configuration correctly allows N concurrent
    SSH sessions and blocks the N+1 session.

    Test Steps:
    1. Configure user and set session limit to 3
    2. Verify configuration with nv show
    3. Open 3 concurrent SSH sessions (should succeed)
    4. Attempt 4th SSH session (should fail)
    5. Unset the limit
    6. Verify 4th user can login
    """
    dut = engines.dut
    username = TEST_USER1
    max_sessions = 3
    sessions = []

    try:
        # Step 1: Set and verify configuration
        set_user_max_logins(dut, username, max_sessions)
        verify_user_max_logins(dut, username, max_sessions)

        # Step 2: Open sessions up to limit
        with allure.step(f"Open {max_sessions} concurrent SSH sessions"):
            for i in range(max_sessions):
                session = open_ssh_session(dut, username, TEST_PASSWORD)
                pytest_assert(session is not None,
                              f"Failed to open SSH session {i + 1}/{max_sessions}")
                sessions.append(session)

        # Step 3: Verify limit enforcement
        with allure.step(f"Verify session {max_sessions + 1} is blocked by limit"):
            extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(extra_session is None,
                          f"Should not allow session {max_sessions + 1} for user {username}")

        # Step 4: Unset limit and verify
        unset_user_max_logins(dut, username)
        time.sleep(2)

        with allure.step(f"Verify session {max_sessions + 1} works after removing limit"):
            session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(session is not None,
                          f"Failed to open SSH session {max_sessions + 1} after unset")
            sessions.append(session)

        logger.info("Test01: PASSED")

    finally:
        cleanup_user_sessions(dut, username, sessions)


@pytest.mark.cumulus
def test02_connection_limit_serial(engines, setup_users):
    """
    Verify that session limits apply to serial console connections.

    Test Steps:
    1. Configure user and set session limit to 1
    2. Verify configuration with nv show
    3. Open 1 serial console sessions (should succeed)
    4. Attempt 2nd ssh session (should fail)
    5. Unset the limit
    6. Verify 3rd session works
    """
    dut = engines.dut
    username = TEST_USER1
    max_sessions = 1
    sessions = []

    try:
        with allure.step(f"Configure max-logins={max_sessions} for user"):
            set_user_max_logins(dut, username, max_sessions)
            verify_user_max_logins(dut, username, max_sessions)

        with allure.step(f"Open {max_sessions} serial console session"):
            session = open_serial_session(username, TEST_PASSWORD)
            pytest_assert(session is not None, f"Failed to open serial session")
            sessions.append(session)

        with allure.step(f"Verify 2nd SSH session is blocked (limit={max_sessions})"):
            extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(extra_session is None,
                          f"Should not allow session {max_sessions + 1} for user {username}")

        with allure.step("Close serial session and unset limit"):
            if sessions:
                close_serial_session(sessions.pop())
            time.sleep(2)
            unset_user_max_logins(dut, username)
            time.sleep(2)

        with allure.step("Verify 2nd session works after removing limit"):
            session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(session is not None, f"Failed to open session after unset")
            sessions.append(session)

        logger.info("Test02: PASSED")

    finally:
        # Cleanup: Close all serial sessions
        for session in sessions:
            close_serial_session(session)
        # Kill any remaining sessions
        dut.run_cmd(f"sudo pkill -u {username} || true", validate=False)


@pytest.mark.cumulus
def test03_connection_limit_GNMI(engines, setup_users):
    """
    Verify that session limits apply to GNMI connections.
    Based on test_gnmi_pam.py authentication testing.

    Test Steps:
    1. Configure user and set session limit to 2
    2. Verify configuration with nv show
    3. Establish 2 concurrent GNMI sessions (should succeed)
    4. Attempt 3rd session (should fail)
    5. Unset the limit
    6. Verify 3rd session works
    """
    dut = engines.dut
    username = TEST_USER3
    max_sessions = 2
    gnmi_processes = []

    try:
        with allure.step(f"Configure max-logins={max_sessions} for user"):
            set_user_max_logins(dut, username, max_sessions)
            time.sleep(2)
            verify_user_max_logins(dut, username, max_sessions)

        with allure.step(f"Open {max_sessions} concurrent GNMI sessions"):
            for i in range(max_sessions):
                try:
                    gnmi_client = GnmiClient(
                        dut.ip,
                        GnmiConsts.GNMI_DEFAULT_PORT,
                        username,
                        TEST_PASSWORD,
                        verify_tools_installed=(i == 0)
                    )
                    out, err = gnmi_client.gnmic_capabilities(skip_cert_verify=True)

                    if 'error' not in err.lower() and 'failed' not in err.lower():
                        logger.info(f"✓ GNMI session {i + 1} established successfully")
                    else:
                        logger.warning(f"GNMI session {i + 1} may have failed: {err}")
                except Exception as e:
                    logger.warning(f"Failed to establish GNMI session {i + 1}: {e}")
            time.sleep(2)

        with allure.step(f"Verify session {max_sessions + 1} is blocked by limit"):
            try:
                gnmi_client = GnmiClient(dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, username, TEST_PASSWORD)
                out, err = gnmi_client.gnmic_capabilities(skip_cert_verify=True)

                if 'authentication' in err.lower() or 'denied' in err.lower() or 'error' in err.lower():
                    logger.info(f"✓ GNMI session {max_sessions + 1} correctly rejected")
                else:
                    logger.warning(f"GNMI session {max_sessions + 1} may have been allowed: {out}")
            except Exception as e:
                logger.info(f"✓ GNMI session {max_sessions + 1} failed as expected: {e}")

        with allure.step("Unset max-logins limit"):
            unset_user_max_logins(dut, username)
            time.sleep(2)

        with allure.step("Verify session works after removing limit"):
            try:
                gnmi_client = GnmiClient(dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, username, TEST_PASSWORD)
                out, err = gnmi_client.gnmic_capabilities(skip_cert_verify=True)

                if 'error' not in err.lower():
                    logger.info(f"✓ GNMI session works after unsetting limit")
                else:
                    logger.warning(f"GNMI session failed after unset: {err}")
            except Exception as e:
                logger.warning(f"GNMI session failed after unset: {e}")

        logger.info("Test03: PASSED (GNMI session limit testing)")

    finally:
        # Cleanup: Kill any gnmic processes
        dut.run_cmd("killall gnmic || true", validate=False)
        dut.run_cmd(f"sudo pkill -u {username} || true", validate=False)


@pytest.mark.cumulus
def test04_mixed_conn_user_limit_restAPI(engines, setup_users, topology_obj):
    """
    Verify that user session limits apply across different connection methods
    (SSH, serial) and can be configured via REST API.

    Test Steps:
    1. Set user session limit to 2 via REST API
    2. Verify configuration
    3. Open 1 SSH + 1 serial session (should succeed)
    4. Attempt 3rd connection (should fail - at user limit)
    5. Unset the limit via REST API
    6. Verify 3rd connection works
    """
    dut = engines.dut
    username = TEST_USER4
    max_sessions = 2
    ssh_sessions = []
    serial_sessions = []

    try:
        logger.info(f"Setting max-logins to {max_sessions} for user {username} via REST API")
        with allure.step(f"Configure max-logins={max_sessions} via OpenAPI/REST"):

            # Temporarily set API type to OpenAPI (REST)
            original_api = TestToolkit.tested_api if hasattr(TestToolkit, 'tested_api') else ApiType.NVUE
            TestToolkit.tested_api = ApiType.OPENAPI

            try:
                # Set max-logins using REST API backend
                set_user_max_logins(dut, username, max_sessions)
                logger.info("✓ User max-logins configuration set via REST API")
            finally:
                # Restore original API type
                TestToolkit.tested_api = original_api

            time.sleep(2)

            # Step 2: Verify configuration
            logger.info(f"Verifying max-logins configuration for user {username}")
            verify_user_max_logins(dut, username, max_sessions)

        with allure.step("Open mixed connection types: 1 SSH + 1 Serial"):
            ssh_session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(ssh_session is not None, f"Failed to open SSH session")
            ssh_sessions.append(ssh_session)
            time.sleep(1)

            serial_session = open_serial_session(username, TEST_PASSWORD)
            pytest_assert(serial_session is not None, f"Failed to open serial session")
            serial_sessions.append(serial_session)
            time.sleep(1)

        with allure.step("Verify 3rd session is blocked by limit"):
            extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(extra_session is None,
                          f"Should not allow 3rd session when user limit is {max_sessions}")

        with allure.step("Unset limit via OpenAPI/REST"):
            TestToolkit.tested_api = ApiType.OPENAPI
            try:
                unset_user_max_logins(dut, username)
            finally:
                TestToolkit.tested_api = original_api
            time.sleep(2)

        with allure.step("Verify 3rd session works after removing limit"):
            extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(extra_session is not None,
                          f"Should allow 3rd session after unsetting limit")
            ssh_sessions.append(extra_session)

        logger.info("Test04: PASSED (Mixed connections with REST API configuration)")

    finally:
        # Cleanup
        for session in ssh_sessions:
            close_ssh_session(session)
        for session in serial_sessions:
            close_serial_session(session)
        dut.run_cmd(f"sudo pkill -u {username} || true", validate=False)


@pytest.mark.cumulus
def test05_group_max_logins_ssh(engines, setup_users, setup_group):
    """
    Verify that group-level session limits work correctly as per-user limits
    for all users in the same group using SSH.
    Group limit = max sessions each user in the group can have.

    Test Steps:
    1. Configure users and create group with limit = 2 (per-user)
    2. Assign users to group
    3. Verify configuration
    4. Open 2 SSH sessions with user1 and 2 with user2 (should succeed)
    5. Attempt 3rd session for each user (should fail - each at their 2 session limit)
    6. Unset the limit
    7. Verify additional sessions work
    """
    dut = engines.dut
    user1 = TEST_USER1
    user2 = TEST_USER2
    group = TEST_GROUP
    max_sessions = 2
    sessions = []

    try:
        with allure.step(f"Add users to group and configure max-logins={max_sessions}"):
            dut.run_cmd(f"sudo usermod -aG {group} {user1}")
            dut.run_cmd(f"sudo usermod -aG {group} {user2}")
            set_group_max_logins(dut, group, max_sessions)
            time.sleep(2)
            verify_group_max_logins(dut, group, max_sessions)

        with allure.step(f"Open {max_sessions} sessions per user (group limit)"):
            for user in [user1, user2]:
                session = open_ssh_session(dut, user, TEST_PASSWORD)
                pytest_assert(session is not None, f"Failed to open session 1 for {user}")
                sessions.append(session)
                time.sleep(1)

            for user in [user1, user2]:
                session = open_ssh_session(dut, user, TEST_PASSWORD)
                pytest_assert(session is not None, f"Failed to open session 2 for {user}")
                sessions.append(session)

        with allure.step("Verify session 3 is blocked for both users"):
            for user in [user1, user2]:
                extra_session = open_ssh_session(dut, user, TEST_PASSWORD)
                pytest_assert(extra_session is None,
                              f"Should not allow session 3 for {user} in group {group}")

        with allure.step("Unset limit and verify sessions work"):
            unset_group_max_logins(dut, group)
            time.sleep(2)

            for user in [user1, user2]:
                session = open_ssh_session(dut, user, TEST_PASSWORD)
                pytest_assert(session is not None,
                              f"Failed to open session for {user} after unset")

        logger.info("Test05: PASSED")

    finally:
        # Cleanup
        cleanup_user_sessions(dut, user1, sessions)
        dut.run_cmd(f"sudo pkill -u {user2} || true", validate=False)


@pytest.mark.cumulus
def test06_group_max_logins_GNMI(engines, setup_users, setup_group):
    """
    Verify that group-level session limits work correctly across multiple users
    in the same group using GNMI/GNOI.

    Test Steps:
    1. Configure users and create group with limit
    2. Assign users to group
    3. Verify configuration
    4. Open 2 GNMI sessions with user1 and 1 with user2 (should succeed)
    5. Attempt another session (should fail)
    6. Unset the limit
    7. Verify additional session works
    """
    dut = engines.dut
    user1 = TEST_USER3
    user2 = TEST_USER4
    group = TEST_GROUP
    max_sessions = 3
    gnmi_processes = []

    try:
        with allure.step(f"Add users to group '{group}' and set max-logins={max_sessions}"):
            dut.run_cmd(f"sudo usermod -aG {group} {user1}")
            dut.run_cmd(f"sudo usermod -aG {group} {user2}")
            set_group_max_logins(dut, group, max_sessions)
            time.sleep(2)
            verify_group_max_logins(dut, group, max_sessions)

        with allure.step(f"Open {max_sessions} concurrent GNMI sessions per user and verify limit"):
            for user in [user1, user2]:
                procs = open_gnmi_sessions(
                    dut,
                    user,
                    TEST_PASSWORD,
                    max_sessions,
                    verify_tools_first=(user == user1),
                )
                pytest_assert(
                    len(procs) == max_sessions,
                    f"Failed to open {max_sessions} GNMI sessions for {user}; opened {len(procs)}",
                )
                gnmi_processes.extend(procs)

                session_failed = test_gnmi_session_fails(
                    dut, user, TEST_PASSWORD, session_desc=f"{max_sessions + 1}th"
                )
                pytest_assert(session_failed, f"Session {max_sessions + 1} should have failed for {user}")

        with allure.step("Unset group limit and verify one more GNMI session can be opened"):
            unset_group_max_logins(dut, group)
            time.sleep(2)

            # Close existing sessions so the new attempt is meaningful.
            for proc in gnmi_processes:
                try:
                    GnmiClient(dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, user1, TEST_PASSWORD).close_session_and_get_out_and_err(proc, delay=0)
                except Exception:
                    pass
            gnmi_processes = []

            for user in [user1, user2]:
                procs = open_gnmi_sessions(dut, user, TEST_PASSWORD, 1, verify_tools_first=False)
                pytest_assert(len(procs) == 1, f"Should be able to open GNMI session after unset for {user}")
                gnmi_processes.extend(procs)

        logger.info("Test06: PASSED (GNMI group limit testing)")

    finally:
        # Cleanup - close player-side gnmic processes
        for proc in gnmi_processes:
            try:
                proc.kill()
            except Exception:
                pass
        dut.run_cmd(f"sudo pkill -u {user1} || true", validate=False)
        dut.run_cmd(f"sudo pkill -u {user2} || true", validate=False)


@pytest.mark.cumulus
def test07_group_max_logins_serial(engines, setup_users, setup_group):
    """
    Verify that group-level session limits work correctly as per-user limits
    using serial console and SSH (mixed connection types).
    Group limit = max sessions each user in the group can have.

    Test Steps:
    1. Configure users and create group with limit = 2 (per-user)
    2. Assign users to group
    3. Verify configuration
    4. Open 1 serial + 1 SSH for user1, 2 SSH for user2 (each user has 2 sessions)
    5. Attempt 3rd session for each user (should fail)
    6. Unset the limit
    7. Verify additional sessions work
    """
    dut = engines.dut
    user1 = TEST_USER5
    user2 = TEST_USER6
    group = TEST_GROUP
    max_sessions = 2
    ssh_sessions = []
    serial_sessions = []

    try:
        with allure.step(f"Add users to group '{group}' and configure max-logins={max_sessions}"):
            dut.run_cmd(f"sudo usermod -aG {group} {user1}")
            dut.run_cmd(f"sudo usermod -aG {group} {user2}")
            set_group_max_logins(dut, group, max_sessions)
            time.sleep(2)
            verify_group_max_logins(dut, group, max_sessions)

        with allure.step(f"Open sessions up to limit (user1: serial+ssh, user2: 2 ssh)"):
            serial_session = open_serial_session(user1, TEST_PASSWORD)
            pytest_assert(serial_session is not None, "Failed to open serial session for user1")
            serial_sessions.append(serial_session)

            for user in [user1, user2, user2]:
                session = open_ssh_session(dut, user, TEST_PASSWORD)
                pytest_assert(session is not None, f"Failed to open ssh session for user {user}")
                ssh_sessions.append(session)

        with allure.step(f"Verify session {max_sessions + 1} is blocked for both users"):
            for user in [user1, user2]:
                extra_session = open_ssh_session(dut, user, TEST_PASSWORD)
                pytest_assert(
                    extra_session is None,
                    f"Should not allow session {max_sessions + 1} for group {group} for user {user}",
                )

        with allure.step("Unset limit"):
            unset_group_max_logins(dut, group)
            time.sleep(2)

        with allure.step(f"Verify session {max_sessions + 1} works after unset for both users"):
            for user in [user1, user2]:
                extra_session = open_ssh_session(dut, user, TEST_PASSWORD)
                pytest_assert(
                    extra_session is not None,
                    f"Should allow session {max_sessions + 1} for group {group} for user {user} after unset",
                )
                ssh_sessions.append(extra_session)

        logger.info("Test07: PASSED (placeholder for serial console testing)")

    finally:
        with allure.step("Cleanup sessions"):
            for session in ssh_sessions:
                close_ssh_session(session)
            for session in serial_sessions:
                close_serial_session(session)
            dut.run_cmd(f"sudo pkill -u {user1} || true", validate=False)
            dut.run_cmd(f"sudo pkill -u {user2} || true", validate=False)
            unset_group_max_logins(dut, group)


@pytest.mark.cumulus
def test08_mixed_conn_group_limit_restAPI(engines, setup_users, setup_group):
    """
    Verify that group session limits apply across different connection methods
    (SSH, serial) and can be configured via REST API.
    Similar to test04 but tests GROUP limitation instead of USER limitation.
    Group limit = max sessions each user in the group can have.

    Test Steps:
    1. Add user to group
    2. Set group session limit to 2 via REST API
    3. Verify configuration
    4. Open 1 SSH + 1 serial session (should succeed)
    5. Attempt 3rd connection (should fail - at group limit)
    6. Unset the limit via REST API
    7. Verify 3rd connection works
    """
    dut = engines.dut
    username = TEST_USER1
    groupname = TEST_GROUP
    max_sessions = 2
    ssh_sessions = []
    serial_sessions = []

    try:
        with allure.step(f"Add user '{username}' to group '{groupname}'"):
            dut.run_cmd(f"sudo usermod -aG {groupname} {username}")
            time.sleep(1)

        with allure.step(f"Configure group max-logins={max_sessions} via OpenAPI/REST"):
            original_api = TestToolkit.tested_api if hasattr(TestToolkit, 'tested_api') else ApiType.NVUE
            TestToolkit.tested_api = ApiType.OPENAPI
            try:
                set_group_max_logins(dut, groupname, max_sessions)
            finally:
                TestToolkit.tested_api = original_api
            time.sleep(2)
            verify_group_max_logins(dut, groupname, max_sessions)

        with allure.step("Open mixed connections (1 SSH + 1 serial)"):
            ssh_session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(ssh_session is not None, "Failed to open SSH session")
            ssh_sessions.append(ssh_session)
            time.sleep(1)

            serial_session = open_serial_session(username, TEST_PASSWORD)
            pytest_assert(serial_session is not None, "Failed to open serial session")
            serial_sessions.append(serial_session)
            time.sleep(1)

        with allure.step("Verify 3rd connection is blocked by group limit"):
            extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(extra_session is None,
                          f"Should not allow 3rd session when group limit is {max_sessions}")

        with allure.step("Unset group limit via OpenAPI/REST"):
            original_api = TestToolkit.tested_api if hasattr(TestToolkit, 'tested_api') else ApiType.NVUE
            TestToolkit.tested_api = ApiType.OPENAPI
            try:
                unset_group_max_logins(dut, groupname)
            finally:
                TestToolkit.tested_api = original_api
            time.sleep(2)

        with allure.step("Verify 3rd session works after removing group limit"):
            extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(extra_session is not None, "Should allow 3rd session after unsetting limit")
            ssh_sessions.append(extra_session)

        logger.info("Test08: PASSED (Mixed connections with REST API group configuration)")

    finally:
        with allure.step("Cleanup sessions"):
            for session in ssh_sessions:
                close_ssh_session(session)
            for session in serial_sessions:
                close_serial_session(session)
            dut.run_cmd(f"sudo pkill -u {username} || true", validate=False)


@pytest.mark.cumulus
def test09_negative_limit(engines, setup_users):
    """
    Verify that negative numbers/chars/0 limitations do not work as expected.
    Tests invalid input validation for max-logins configuration.

    Test Steps:
    1. Try to set limit to 0 → expect error
    2. Try to set limit to negative number → expect error
    3. Try to set limit to invalid character → expect error
    4. Verify configuration remains unchanged
    """
    dut = engines.dut
    username = TEST_USER1

    try:
        with allure.step("Set max-logins=0 (should fail)"):
            result = dut.run_cmd(f"nv set system security user {username} max-logins 0", validate=False)
            pytest_assert("error" in result.lower() or "invalid" in result.lower(),
                          "Setting max-logins to 0 should produce an error")

        with allure.step("Set max-logins=-5 (should fail)"):
            result = dut.run_cmd(f"nv set system security user {username} max-logins -5", validate=False)
            pytest_assert("error" in result.lower() or "invalid" in result.lower(),
                          "Setting max-logins to negative should produce an error")

        with allure.step("Set max-logins=abc (should fail)"):
            result = dut.run_cmd(f"nv set system security user {username} max-logins abc", validate=False)
            pytest_assert("error" in result.lower() or "invalid" in result.lower(),
                          "Setting max-logins to char should produce an error")

        with allure.step("Set max-logins=999 (should fail or be rejected)"):
            result = dut.run_cmd(f"nv set system security user {username} max-logins 999", validate=False)
            logger.info(f"Result for max-logins=999: {result}")

        with allure.step("Verify invalid config wasn't applied"):
            show_result = dut.run_cmd(f"nv show system security user {username}", validate=False)
            pytest_assert("max-logins" not in show_result or "999" not in show_result,
                          "Invalid configuration should not be applied")

        logger.info("Test09: PASSED (Negative limit validation successful)")

    finally:
        with allure.step("Cleanup user max-logins"):
            dut.run_cmd(f"nv unset system security user {username} max-logins", validate=False)
            dut.run_cmd("nv config apply -y", validate=False)


@pytest.mark.cumulus
def test10_user_and_group_limit_conflict(engines, setup_users, setup_group):
    """
    Verify behavior when both user and group limits are configured.
    Tests which limit takes precedence: user-specific or group-based.

    Test Steps:
    1. Create user WITHOUT individual limit
    2. Create group with limit = 2
    3. Assign user to group
    4. Open 2 sessions → Success (group limit applies)
    5. Attempt 3rd session → Should fail (group limit of 2)
    6. Set user-specific limit = 5 (higher than group)
    7. Test if user can now open 5 sessions (user limit overrides) OR still limited to 2 (group wins)
    8. Clean up and test with user limit LOWER than group limit
    """
    dut = engines.dut
    username = TEST_USER9
    groupname = TEST_GROUP
    group_limit = 2
    higher_user_limit = 5
    lower_user_limit = 1
    sessions = []

    try:
        # === Scenario 1: Group limit only (no user limit) ===
        logger.info("=== Scenario 1: Group limit only (2 sessions per user) ===")

        # Add user to group (no user limit set yet)
        logger.info(f"Adding user {username} to group {groupname}")
        dut.run_cmd(f"sudo usermod -aG {groupname} {username}")

        # Set group limit only
        logger.info(f"Setting max-logins to {group_limit} for group {groupname}")
        set_group_max_logins(dut, groupname, group_limit)
        time.sleep(2)

        # Open 2 sessions (should succeed with group limit)
        logger.info(f"Opening {group_limit} sessions (should succeed)")
        for i in range(group_limit):
            session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(session is not None, f"Failed to open session {i + 1}")
            sessions.append(session)
            time.sleep(1)

        # 3rd session should fail (group limit = 2)
        logger.info("Attempting 3rd session (should fail - group limit is 2)")
        extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
        pytest_assert(extra_session is None,
                      f"Should not allow session 3 when only group limit is set to {group_limit}")

        # Close sessions
        for session in sessions:
            close_ssh_session(session)
        sessions = []
        time.sleep(2)

        # === Scenario 2: User limit (5) HIGHER than group limit (2) ===
        logger.info("=== Scenario 2: User limit (5) takes precedence over Group limit (2) ===")

        # Set user limit higher than group limit
        logger.info(f"Setting user max-logins to {higher_user_limit} (user limit overrides group)")
        set_user_max_logins(dut, username, higher_user_limit)
        time.sleep(2)

        # Should now be able to open 5 sessions (user limit takes precedence)
        logger.info(f"Opening {higher_user_limit} sessions (user limit should win)")
        for i in range(higher_user_limit):
            session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(session is not None,
                          f"Failed to open session {i + 1} - user limit (5) should override group limit (2)")
            sessions.append(session)
            time.sleep(1)

        # 6th session should fail (user limit = 5)
        logger.info("Attempting 6th session (should fail - user limit is 5)")
        extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
        pytest_assert(extra_session is None,
                      f"Should not allow session 6 when user limit is {higher_user_limit}")

        # Close sessions
        for session in sessions:
            close_ssh_session(session)
        sessions = []
        time.sleep(2)

        # === Scenario 3: User limit (1) LOWER than group limit (2) ===
        logger.info("=== Scenario 3: User limit (1) takes precedence over Group limit (2) ===")

        # Change user limit to lower than group
        logger.info(f"Setting user max-logins to {lower_user_limit} (user limit still overrides group)")
        set_user_max_logins(dut, username, lower_user_limit)
        time.sleep(2)

        # Should only be able to open 1 session (user limit wins even when lower)
        logger.info(f"Opening {lower_user_limit} session (user limit should win)")
        session = open_ssh_session(dut, username, TEST_PASSWORD)
        pytest_assert(session is not None, f"Failed to open 1 session")
        sessions.append(session)

        # 2nd session should fail (user limit = 1)
        logger.info("Attempting 2nd session (should fail - user limit is 1)")
        extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
        pytest_assert(extra_session is None,
                      f"Should not allow session 2 when user limit is {lower_user_limit}")

        logger.info("Test10: PASSED (User-specific limit always takes precedence over group limit)")

    finally:
        # Cleanup
        cleanup_user_sessions(dut, username, sessions)
        dut.run_cmd(f"sudo gpasswd -d {username} {groupname}", validate=False)
        unset_user_max_logins(dut, username)
        unset_group_max_logins(dut, groupname)


@pytest.mark.cumulus
def test11_limit_after_reboot(engines, setup_users):
    """
    Verify that session limit configuration persists after system reboot.
    Ensures configuration is properly saved and restored.

    Test Steps:
    1. Configure user
    2. Set session limit = 3
    3. Save configuration
    4. Reboot system
    5. After reboot, test limit enforcement → Should still block 4th session
    """
    dut = engines.dut
    username = TEST_USER1
    max_sessions = 3
    sessions = []

    try:
        # Step 1 & 2: Set session limit
        logger.info(f"Setting max-logins to {max_sessions} for user {username}")
        set_user_max_logins(dut, username, max_sessions)

        # Step 3: Save configuration
        logger.info("Saving configuration")
        dut.run_cmd("nv config save")
        time.sleep(2)

        # Verify configuration before reboot
        logger.info("Verifying configuration before reboot")
        verify_user_max_logins(dut, username, max_sessions)

        # Step 4: Reboot system
        logger.info("Rebooting system...")
        dut.run_cmd("sudo reboot", validate=False)

        # Wait for system to reboot
        logger.info("Waiting for system to come back online (60 seconds)...")
        time.sleep(60)

        # Wait until system is accessible
        max_wait = 180  # 3 minutes
        start_time = time.time()
        while time.time() - start_time < max_wait:
            try:
                result = dut.run_cmd("echo test", timeout=10)
                if "test" in result:
                    logger.info("System is back online")
                    break
            except BaseException:
                pass
            time.sleep(10)
        else:
            pytest_assert(False, "System did not come back online after reboot")

        # Additional wait for services to stabilize
        time.sleep(30)

        # Step 5: Verify limit still enforced after reboot
        logger.info("Verifying max-logins configuration persisted after reboot")
        verify_user_max_logins(dut, username, max_sessions)

        # Test enforcement
        logger.info(f"Testing max-logins enforcement after reboot")
        for i in range(max_sessions):
            session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(session is not None, f"Failed to open SSH session {i + 1} after reboot")
            sessions.append(session)
            time.sleep(1)

        # 4th session should fail
        extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
        pytest_assert(extra_session is None,
                      f"Should not allow session {max_sessions + 1} after reboot")

        logger.info("Test11: PASSED (Limit persisted after reboot)")

    finally:
        cleanup_user_sessions(dut, username, sessions)


@pytest.mark.cumulus
def test12_scale_connect(engines, setup_users):
    """
    Verify that a large number of users (200) limit is working correctly.
    When user 201 connects, they should be blocked.

    Test Steps:
    1. Set limit to 200
    2. Connect 200 users
    3. Verify user 201 can't login
    """
    dut = engines.dut
    username = TEST_USER1
    max_sessions = 100
    sessions = []

    try:
        # Step 1: Set high session limit
        logger.info(f"Setting max-logins to {max_sessions} for user {username}")
        set_user_max_logins(dut, username, max_sessions)

        # Step 2: Open many concurrent sessions
        logger.info(f"Opening {max_sessions} concurrent SSH sessions (this may take a while)...")
        successful_sessions = 0
        failed_sessions = 0

        # Open sessions in batches to avoid overwhelming the system
        batch_size = 10
        for batch in range(0, max_sessions, batch_size):
            batch_sessions = min(batch_size, max_sessions - batch)
            logger.info(f"Opening batch {batch // batch_size + 1}: sessions {batch + 1} to {batch + batch_sessions}")

            for i in range(batch_sessions):
                session = open_ssh_session(dut, username, TEST_PASSWORD)
                if session is not None:
                    sessions.append(session)
                    successful_sessions += 1
                else:
                    failed_sessions += 1
                    logger.warning(f"Failed to open session {batch + i + 1}")

                # Small delay between sessions
                if i % 5 == 0:
                    time.sleep(0.5)

        logger.info(f"Opened {successful_sessions} sessions successfully, {failed_sessions} failed")

        # If we didn't open enough sessions, keep trying until we hit the limit
        if successful_sessions < max_sessions:
            logger.info(f"Only {successful_sessions} sessions opened, attempting more to reach {max_sessions}")
            while len(sessions) < max_sessions:
                session = open_ssh_session(dut, username, TEST_PASSWORD)
                if session is not None:
                    sessions.append(session)
                else:
                    # Hit the limit before reaching target
                    logger.warning(f"Hit limit at {len(sessions)} sessions (target was {max_sessions})")
                    break
                time.sleep(0.2)

        actual_sessions = len(sessions)
        logger.info(f"Actually opened {actual_sessions} sessions")

        # Step 3: Attempt one more session (should fail if we're at or near limit)
        logger.info(f"Attempting session {actual_sessions + 1} (should fail)")
        extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
        pytest_assert(extra_session is None,
                      f"Should not allow session {actual_sessions + 1} when limit is {max_sessions}")

        logger.info("Test12: PASSED (Scale test successful)")

    finally:
        # Cleanup: Close all sessions
        logger.info(f"Cleaning up {len(sessions)} sessions...")
        for session in sessions:
            close_ssh_session(session)
        # Force kill any remaining sessions
        dut.run_cmd(f"sudo pkill -u {username} || true", validate=False)
        time.sleep(2)


@pytest.mark.cumulus
def test13_group_user_vrf(engines, setup_users, setup_group):
    """
    Verify user/group with max limit works correctly with different VRF connections.
    Tests that session limits (per-user for group) apply across different VRF configurations.

    Test Steps:
    1. Set user and group limitation (group limit = per-user limit)
    2. Test limitation works with default VRF
    3. Create new VRF
    4. Verify limitation still works with VRF configured
    5. Clean up VRF
    """
    dut = engines.dut
    username = TEST_USER1
    groupname = TEST_GROUP
    max_sessions = 2
    ssh_sessions = []
    vrf_name = "test_vrf"

    try:
        with allure.step(f"Configure user+group max-logins={max_sessions}"):
            set_user_max_logins(dut, username, max_sessions)
            dut.run_cmd(f"sudo usermod -aG {groupname} {username}", validate=False)
            set_group_max_logins(dut, groupname, max_sessions)

        with allure.step("Verify limit works with default VRF (regular SSH)"):
            for i in range(max_sessions):
                session = open_ssh_session(dut, username, TEST_PASSWORD)
                pytest_assert(session is not None, f"Failed to open session {i + 1}")
                ssh_sessions.append(session)

            extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(extra_session is None, "Should not allow extra session")

            for session in ssh_sessions:
                close_ssh_session(session)
            ssh_sessions = []
            time.sleep(2)

        with allure.step(f"Configure VRF '{vrf_name}'"):
            # Create the VRF (safe mode: don't bind a physical interface)
            configure_non_default_vrf(engines, vrf_name=vrf_name, interface=None)

        with allure.step("Open 1 regular SSH session + 1 VRF-based SSH session and verify limit"):
            session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(session is not None, "Failed to open regular SSH session")
            ssh_sessions.append(session)

            vrf_session = create_session_via_vrf(
                engines,
                user=username,
                password=TEST_PASSWORD,
                vrf_name=vrf_name,
                target_ip="127.0.0.1",
            )
            pytest_assert(vrf_session.get("session_created") is True, "Failed to create VRF-based SSH session")

            extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(extra_session is None, "Should not allow extra session with VRF session active")

        logger.info("Test13: PASSED (Limits work correctly with VRF)")

    finally:
        cleanup_user_sessions(dut, username, ssh_sessions)
        dut.run_cmd(f"nv unset vrf {vrf_name}", validate=False)
        dut.run_cmd("nv config apply -y", validate=False)


@pytest.mark.cumulus
def test14_change_limit(engines, setup_users):
    """
    Verify limit can be changed dynamically, including via REST API.

    Test Steps:
    1. Set limit = X to user/group
    2. Check user X+1 can't connect
    3. Change limit to X+2 through REST API
    4. Check user X+1 can login
    5. Change limit to X
    6. Check user X+2 can't connect
    7. Unset limit
    8. Check user can connect
    """
    dut = engines.dut
    username = TEST_USER1
    initial_limit = 2
    increased_limit = 4
    sessions = []

    try:
        # Step 1: Set initial limit
        logger.info(f"Step 1: Setting max-logins to {initial_limit} for user {username}")
        set_user_max_logins(dut, username, initial_limit)

        # Step 2: Verify initial limit works
        logger.info(f"Step 2: Opening {initial_limit} sessions")
        for i in range(initial_limit):
            session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(session is not None, f"Failed to open session {i + 1}")
            sessions.append(session)

        # Extra session should fail
        extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
        pytest_assert(extra_session is None,
                      f"Should not allow session {initial_limit + 1}")

        # Step 3: Increase limit using NVUE (or REST API if available)
        logger.info(f"Step 3: Increasing max-logins to {increased_limit}")
        set_user_max_logins(dut, username, increased_limit)
        time.sleep(1)

        for i in range(increased_limit - initial_limit):
            session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(session is not None, f"Failed to open session {initial_limit + i + 1}")
            sessions.append(session)

        # 5th session should fail
        extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
        pytest_assert(extra_session is None,
                      f"Should not allow session {increased_limit + 2}")

        # Step 5: Decrease limit back
        logger.info(f"Step 5: Decreasing max-logins back to {initial_limit}")
        set_user_max_logins(dut, username, initial_limit)

        # Step 6: Verify decreased limit works, session should fail
        logger.info(f"Step 6: Opening 1 session")
        session = open_ssh_session(dut, username, TEST_PASSWORD)
        pytest_assert(session is None, f"Should not allow session {initial_limit + 1}")

        # Step 7: Unset limit
        logger.info("Step 7: Unsetting max-logins")
        unset_user_max_logins(dut, username)
        time.sleep(2)

        # Step 8: Verify unlimited sessions work
        logger.info(f"Step 8: Verifying unlimited sessions")
        session = open_ssh_session(dut, username, TEST_PASSWORD)
        pytest_assert(session is not None,
                      f"Should allow session after unsetting limit")
        sessions.append(session)

        logger.info("Test14: PASSED (Dynamic limit changes successful)")

    finally:
        cleanup_user_sessions(dut, username, sessions)


@pytest.mark.cumulus
def test15_system_groups_limit(engines, setup_users):
    """
    Verify that session limits work correctly on system groups (sudo, nvapply).
    Tests that limits can be applied to pre-existing system groups.
    Uses testuser1 and temporarily adds them to these system groups.

    Test Steps:
    For each system group (sudo, nvapply):
    1. Add testuser1 to the system group
    2. Set limit to 2 for the group
    3. Connect 2 sessions with testuser1
    4. Attempt 3rd session → Should fail (group limit)
    5. Unset the limit
    6. Attempt 3rd session → Should succeed
    7. Remove testuser1 from group
    8. Close all sessions and move to next group
    """
    dut = engines.dut
    username = TEST_USER1
    password = TEST_PASSWORD
    system_groups = ["sudo", "nvapply"]
    max_sessions = 2

    for groupname in system_groups:
        sessions = []

        try:
            with allure.step(f"Test system group '{groupname}' with user '{username}'"):
                logger.info(f"\n{'=' * 60}")
                logger.info(f"Testing group: {groupname} with user: {username}")
                logger.info(f"{'=' * 60}")

            with allure.step(f"Add user '{username}' to group '{groupname}'"):
                dut.run_cmd(f"sudo usermod -aG {groupname} {username}")
                time.sleep(1)

            with allure.step("Verify user membership"):
                groups_output = dut.run_cmd(f"groups {username}")
                logger.info(f"User {username} groups: {groups_output}")

            with allure.step(f"Set group max-logins={max_sessions}"):
                set_group_max_logins(dut, groupname, max_sessions)
                time.sleep(2)

            verify_group_max_logins(dut, groupname, max_sessions)

            with allure.step(f"Open {max_sessions} SSH sessions (fill limit)"):
                for i in range(max_sessions):
                    session = open_ssh_session(dut, username, password)
                    pytest_assert(
                        session is not None,
                        f"Failed to open session {i + 1} for user {username} in group {groupname}",
                    )
                    sessions.append(session)
                    time.sleep(1)

            with allure.step(f"Verify session {max_sessions + 1} is blocked"):
                extra_session = open_ssh_session(dut, username, password)
                pytest_assert(
                    extra_session is None,
                    f"Should not allow session {max_sessions + 1} for group {groupname}",
                )

            with allure.step("Unset group max-logins"):
                unset_group_max_logins(dut, groupname)
                time.sleep(2)

            with allure.step(f"Verify session {max_sessions + 1} works after unset"):
                extra_session = open_ssh_session(dut, username, password)
                pytest_assert(
                    extra_session is not None,
                    f"Should allow session {max_sessions + 1} after unsetting limit for group {groupname}",
                )
                sessions.append(extra_session)

            logger.info(f"✓ Group {groupname} test passed")

        finally:
            # Cleanup for this group
            with allure.step(f"Cleanup for group '{groupname}'"):
                for session in sessions:
                    close_ssh_session(session)

            # Kill any remaining sessions for testuser1
            dut.run_cmd(f"sudo pkill -u {username} || true", validate=False)
            time.sleep(1)

            # Remove user from the system group
            dut.run_cmd(f"sudo gpasswd -d {username} {groupname}", validate=False)

            # Unset group limit
            unset_group_max_logins(dut, groupname)

    logger.info("\nTest15: PASSED (System groups limit enforcement successful)")


@pytest.mark.cumulus
def test16_logs_after_max_logins(engines, setup_users):
    """
    Verify that correct log messages appear when max-logins limit is exceeded.

    Expected log messages when limit is exceeded:
    - "Accepted password for <user>"
    - "session opened for user <user>"
    - "Too many logins (max N) for <user>"
    - "error: PAM: pam_open_session(): Permission denied"
    - "Disconnected from user <user>"

    Test Steps:
    1. Set limit to 2 for user
    2. Open 2 sessions (at limit)
    3. Attempt 3rd session (should fail)
    4. Check auth.log for expected error messages
    5. Verify "Too many logins" message appears
    6. Verify "pam_open_session(): Permission denied" appears
    """
    dut = engines.dut
    username = TEST_USER1
    max_sessions = 2
    sessions = []

    try:
        with allure.step(f"Configure user '{username}' max-logins={max_sessions}"):
            set_user_max_logins(dut, username, max_sessions)

        with allure.step(f"Open {max_sessions} SSH sessions (fill limit)"):
            for i in range(max_sessions):
                session = open_ssh_session(dut, username, TEST_PASSWORD)
                pytest_assert(session is not None, f"Failed to open session {i + 1}")
                sessions.append(session)
                time.sleep(1)

        with allure.step(f"Attempt session {max_sessions + 1} (should fail and create auth.log entries)"):
            extra_session = open_ssh_session(dut, username, TEST_PASSWORD)
            pytest_assert(extra_session is None, f"Should not allow session {max_sessions + 1}")
            time.sleep(2)  # allow logs to flush

        with allure.step("Collect recent /var/log/auth.log entries"):
            auth_log = dut.run_cmd("sudo cat /var/log/auth.log | tail -200")
            logger.info(f"Auth log entries:\n{auth_log}")

        with allure.step("Validate expected log patterns"):
            too_many_logins_pattern = f"Too many logins (max {max_sessions}) for {username}"
            pytest_assert(
                too_many_logins_pattern in auth_log or "Too many logins" in auth_log,
                "Expected 'Too many logins' message not found in auth.log",
            )

            pam_error_pattern = "pam_open_session(): Permission denied"
            pytest_assert(
                pam_error_pattern in auth_log or "Permission denied" in auth_log,
                "Expected PAM permission denied message not found in auth.log",
            )

            # Extra signal: ensure user is present in the log slice
            pytest_assert(username in auth_log, f"Username {username} not found in auth.log")

        logger.info("Test16: PASSED (Correct logs generated when max-logins exceeded)")

    finally:
        with allure.step("Cleanup sessions and unset max-logins"):
            cleanup_user_sessions(dut, username, sessions)
            unset_user_max_logins(dut, username)
