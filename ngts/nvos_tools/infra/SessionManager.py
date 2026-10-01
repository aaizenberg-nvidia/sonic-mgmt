"""
Session Manager Module

This module provides centralized SSH/Console session management with threading support.
It handles session creation, tracking, and verification for test automation.

Usage:
    from ngts.nvos_tools.infra.SessionManager import SessionManager

    # Create a session manager instance
    session_mgr = SessionManager()

    # Create sessions
    session_mgr.create_session(engines, user, password)
    session_mgr.create_sessions(engines, user, password, count=3)

    # Wait for threaded sessions to complete
    session_mgr.wait_for_sessions_threads()

    # Verify session states
    session_mgr.verify_sessions_disconnected(cli_common, user)
    session_mgr.verify_sessions_active(cli_common, user)

    # Cleanup
    session_mgr.clear()
"""

import logging
import time
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Optional, List, Dict, Any

from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.SerialConsoleTool import SerialConsoleTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)

# Default timeout constants for session creation
DEFAULT_ATTEMPT_TIMEOUT = 20.0  # Max seconds per connection attempt
DEFAULT_TOTAL_TIMEOUT = 60.0    # Max seconds for all attempts combined
DEFAULT_MAX_RETRIES = 5         # Max number of retry attempts


class SessionManager:
    """
    Centralized session management class for SSH and console sessions.

    Provides thread-safe session creation, tracking, and verification
    capabilities for test automation.

    Attributes:
        threads: List of active session creation threads
        sessions_dict: Dictionary mapping usernames to their session objects
        sessions_dict_lock: Thread lock for safe session dictionary access
    """

    def __init__(self):
        """Initialize the SessionManager with empty session tracking."""
        self.threads: List[threading.Thread] = []
        self.sessions_dict: Dict[str, List[Any]] = defaultdict(list)
        self.sessions_dict_lock = threading.Lock()

    def create_session(self, engines, user: str, password: str, port: int = 22,
                       max_retries: int = DEFAULT_MAX_RETRIES,
                       attempt_timeout: float = DEFAULT_ATTEMPT_TIMEOUT,
                       total_timeout: float = DEFAULT_TOTAL_TIMEOUT) -> Optional[Any]:
        """
        Create a single SSH session for a user with retry mechanism and timeouts.

        Args:
            engines: The test engines object containing the DUT connection information
            user: Username to create the session for
            password: Password for the user
            port: SSH port to connect to (default: 22)
            max_retries: Maximum number of connection retry attempts (default: 5)
            attempt_timeout: Maximum seconds for each connection attempt (default: 20)
            total_timeout: Maximum total seconds for all attempts combined (default: 60)

        Returns:
            Session object if successful, None if all retries failed

        Raises:
            Exception: If all retry attempts are exhausted or total timeout exceeded

        Example:
            >>> session = session_mgr.create_session(engines, "admin", "password123")
            >>> session.run_cmd("show version")
        """
        with allure.step(f'Create session for user "{user}"'):
            start_time = time.time()
            attempt = 0
            last_error = None

            logger.info(f"Attempting to create SSH session for user '{user}' to {engines.dut.ip}:{port}")
            logger.info(f"Limits: max_retries={max_retries}, attempt_timeout={attempt_timeout}s, total_timeout={total_timeout}s")

            while attempt < max_retries:
                # Check total timeout before each attempt
                elapsed = time.time() - start_time
                if elapsed >= total_timeout:
                    logger.error(f"Total timeout of {total_timeout}s exceeded for user '{user}' after {attempt} attempts ({elapsed:.1f}s elapsed)")
                    raise TimeoutError(f"Total timeout of {total_timeout}s exceeded for user {user} after {attempt} attempts")

                attempt += 1
                remaining_time = total_timeout - elapsed
                # Use the smaller of attempt_timeout or remaining total time
                effective_timeout = min(attempt_timeout, remaining_time)

                logger.debug(f"Connection attempt {attempt}/{max_retries} for user '{user}' (timeout: {effective_timeout:.1f}s)")

                try:
                    # Use ThreadPoolExecutor to enforce timeout on each attempt
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(
                            ConnectionTool.create_ssh_conn,
                            engines.dut.ip, user, password, port, False  # retry=False
                        )
                        try:
                            session_result = future.result(timeout=effective_timeout)
                        except FuturesTimeoutError:
                            logger.warning(f"Attempt {attempt}/{max_retries} timed out after {effective_timeout:.1f}s for user '{user}'")
                            last_error = TimeoutError(f"Connection attempt timed out after {effective_timeout}s")
                            time.sleep(0.5)  # Brief delay before retry
                            continue

                    if session_result.result:
                        session = session_result.get_returned_value()
                        with self.sessions_dict_lock:
                            self.sessions_dict[user].append(session)
                        total_elapsed = time.time() - start_time
                        logger.info(f"Session successfully created for user '{user}' on attempt {attempt} ({total_elapsed:.1f}s total)")
                        return session
                    else:
                        logger.warning(f"Connection failed for user '{user}' on attempt {attempt}/{max_retries} - session_result.result was False")
                        last_error = Exception("Connection failed - session_result.result was False")

                except FuturesTimeoutError:
                    logger.warning(f"Attempt {attempt}/{max_retries} timed out after {effective_timeout:.1f}s for user '{user}'")
                    last_error = TimeoutError(f"Connection attempt timed out after {effective_timeout}s")
                except Exception as e:
                    logger.warning(f"Authentication failed for user {user}, attempt {attempt}/{max_retries}: {str(e)}")
                    last_error = e

                # Brief delay between retries (but don't delay if we're out of attempts)
                if attempt < max_retries:
                    time.sleep(1)

            # All retries exhausted
            total_elapsed = time.time() - start_time
            logger.error(f"All {max_retries} retries exhausted for user '{user}' ({total_elapsed:.1f}s total)")
            if last_error:
                raise last_error
            raise Exception(f"Failed to create session for user {user} after {max_retries} attempts")

    def create_session_thread(self, engines, user: str, password: str, port: int = 22,
                              max_retries: int = DEFAULT_MAX_RETRIES,
                              attempt_timeout: float = DEFAULT_ATTEMPT_TIMEOUT,
                              total_timeout: float = DEFAULT_TOTAL_TIMEOUT) -> threading.Thread:
        """
        Create a session in a separate thread for parallel session creation.

        Args:
            engines: The test engines object containing the DUT connection information
            user: Username to create the session for
            password: Password for the user
            port: SSH port to connect to (default: 22)
            max_retries: Maximum number of connection retry attempts (default: 5)
            attempt_timeout: Maximum seconds for each connection attempt (default: 20)
            total_timeout: Maximum total seconds for all attempts combined (default: 60)

        Returns:
            The started Thread object

        Example:
            >>> session_mgr.create_session_thread(engines, "user1", "pass1")
            >>> session_mgr.create_session_thread(engines, "user2", "pass2")
            >>> session_mgr.wait_for_sessions_threads()
        """
        with allure.step(f'Start session creation thread for user "{user}"'):
            thread = threading.Thread(
                target=self.create_session,
                args=(engines, user, password, port, max_retries, attempt_timeout, total_timeout)
            )
            self.threads.append(thread)
            thread.start()
            return thread

    def wait_for_sessions_threads(self, timeout: Optional[float] = None) -> None:
        """
        Wait for all session creation threads to complete.

        Args:
            timeout: Maximum time to wait for each thread (None = wait indefinitely)

        Example:
            >>> session_mgr.create_session_thread(engines, "user1", "pass1")
            >>> session_mgr.create_session_thread(engines, "user2", "pass2")
            >>> session_mgr.wait_for_sessions_threads()
        """
        with allure.step('Wait for session threads'):
            for thread in self.threads:
                thread.join(timeout=timeout)

    def create_sessions(self, engines, user: str, password: str, count: int,
                        port: int = 22, sleep: float = 0,
                        max_retries: int = DEFAULT_MAX_RETRIES,
                        attempt_timeout: float = DEFAULT_ATTEMPT_TIMEOUT,
                        total_timeout: float = DEFAULT_TOTAL_TIMEOUT) -> None:
        """
        Create multiple sessions for a user using threaded session creation.

        Args:
            engines: The test engines object containing the DUT connection information
            user: Username to create the sessions for
            password: Password for the user
            count: Number of sessions to create
            port: SSH port to connect to (default: 22)
            sleep: Delay in seconds between starting each session thread (default: 0)
            max_retries: Maximum number of connection retry attempts per session (default: 5)
            attempt_timeout: Maximum seconds for each connection attempt (default: 20)
            total_timeout: Maximum total seconds for all attempts combined per session (default: 60)

        Example:
            >>> session_mgr.create_sessions(engines, "admin", "password123", count=3)
            >>> session_mgr.wait_for_sessions_threads()
            >>> print(len(session_mgr.sessions_dict["admin"]))  # 3
        """
        with allure.step(f'Create {count} sessions for user "{user}"'):
            for i in range(count):
                self.create_session_thread(engines, user, password, port, max_retries, attempt_timeout, total_timeout)
                if sleep > 0:
                    time.sleep(sleep)

    def create_console_session(self, engines, username: str, password: str,
                               raise_on_failure: bool = False) -> Optional[Any]:
        """
        Create a console session using SerialConsoleTool.

        Args:
            engines: Test engines
            username: Username for console login
            password: Password for console login
            raise_on_failure: If True, raise exception on failure. If False, return None.

        Returns:
            Session object on success, None on failure (if raise_on_failure=False)

        Example:
            >>> console = session_mgr.create_console_session(engines, "admin", "password")
            >>> if console:
            ...     console.run_cmd("show version")
        """
        with allure.step(f'Create console session for user "{username}"'):
            try:
                topology_obj = TestToolkit.topology_obj

                # Get serial console session
                with allure.step('Enter serial context'):
                    serial = SerialConsoleTool.get_serial_console_session(topology_obj)

                # Exit any existing login
                with allure.step('Exit existing login'):
                    SerialConsoleTool.exit_existing_login(serial)

                # Login with the specified user credentials
                SerialConsoleTool.login_nos(
                    serial_engine=serial,
                    username=username,
                    password=password,
                    start_login_tries=3,
                    handle_change_password_prompt=False
                )

                # Track the session
                with self.sessions_dict_lock:
                    self.sessions_dict[username].append(serial)

                return serial

            except Exception as e:
                logger.error(f"Failed to create console session for user {username}: {str(e)}")
                if raise_on_failure:
                    raise
                return None

    def create_session_via_vrf(self, engines, user: str, password: str, vrf_name: str,
                               target_ip: str = '127.0.0.1') -> Dict[str, Any]:
        """
        Create SSH session through a specific VRF using 'ip vrf exec'.

        This executes SSH from within the VRF context on the DUT.

        Args:
            engines: Test engines
            user: Username for SSH connection
            password: Password for SSH connection
            vrf_name: Name of the VRF to execute SSH from
            target_ip: Target IP to SSH to (default: 127.0.0.1)

        Returns:
            dict: Session info with VRF context details
        """
        with allure.step(f'Create session for user "{user}" via VRF "{vrf_name}"'):
            # Verify VRF exists and connectivity works
            ping_result = engines.dut.run_cmd(f"ip vrf exec {vrf_name} ping -c 1 {target_ip} 2>/dev/null || echo 'ping failed'", validate=False)
            logger.info(f"VRF {vrf_name} ping test to {target_ip}: {ping_result}")

            # Check if sshpass is available
            sshpass_check = engines.dut.run_cmd("which sshpass 2>/dev/null || echo 'not found'", validate=False)

            session_created = False

            if 'not found' not in sshpass_check:
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

                # Verify session was created - check who -u for 127.0.0.1 session
                verify_result = engines.dut.run_cmd(f"who -u | grep {user} | grep {target_ip} || echo 'no session'", validate=False)
                if 'no session' not in verify_result and target_ip in verify_result:
                    logger.info(f"✓ VRF SSH session created for user {user}:\n{verify_result.strip()}")
                    session_created = True
                else:
                    logger.warning(f"VRF SSH session may not have been created for user {user}")
                    logger.debug(f"who -u output: {verify_result}")
            else:
                logger.warning(f"sshpass not available - VRF SSH session cannot be created")
                logger.warning(f"Manual test: ip vrf exec {vrf_name} ssh {user}@{target_ip}")

            # Create session info object for tracking
            vrf_session_info = {
                'vrf_name': vrf_name,
                'user': user,
                'target_ip': target_ip,
                'session_created': session_created,
                'username': user,  # For compatibility with verify functions
                'engine': None,    # No direct engine object for VRF session
                'disconnect': lambda: None  # Dummy disconnect method
            }

            # Store in sessions_dict for tracking
            with self.sessions_dict_lock:
                self.sessions_dict[f"vrf_{vrf_name}_{user}"].append(vrf_session_info)

            logger.info(f"VRF session for user {user} via VRF {vrf_name}: session_created={session_created}")
            return vrf_session_info

    def verify_sessions_disconnected(self, cli_common, user: str,
                                     allow_one_session: bool = False) -> None:
        """
        Verify that a user has no active sessions on the system.

        Args:
            cli_common: CumulusGeneralCli instance for running commands
            user: Username to check for active sessions
            allow_one_session: If True, allows one session (useful for admin verification sessions)

        Raises:
            AssertionError: If the user is found to have active sessions beyond allowed

        Example:
            >>> session_mgr.verify_sessions_disconnected(cli_common, "user1")
        """
        time.sleep(1)  # Brief wait for session cleanup
        with allure.step(f'Verify sessions disconnected for user "{user}"'):
            current_sessions = cli_common.who('-u')
            logger.debug(f"Current sessions on system:\n{current_sessions}")

            if allow_one_session:
                # Count sessions for this user
                user_sessions = [
                    line for line in current_sessions.split('\n')
                    if user in line and 'pts/' in line
                ]
                assert len(user_sessions) <= 1, \
                    f"User {user} has {len(user_sessions)} sessions (expected 0 or 1)"
            else:
                assert user not in current_sessions, f"User {user} is still active"

    def verify_sessions_active(self, cli_common, user: str,
                               expected_num_sessions: int = 1) -> None:
        """
        Verify that a user has active sessions on the system.

        Args:
            cli_common: CumulusGeneralCli instance for running commands
            user: Username to check for active sessions
            expected_num_sessions: Expected number of active sessions (default: 1)

        Raises:
            AssertionError: If the user doesn't have the expected number of sessions

        Example:
            >>> session_mgr.verify_sessions_active(cli_common, "user1", expected_num_sessions=3)
        """
        with allure.step(f'Verify sessions active for user "{user}"'):
            current_sessions = cli_common.who('-u')
            logger.debug(f"Current sessions on system:\n{current_sessions}")

            if expected_num_sessions == 1:
                assert user in current_sessions, f"User {user} is not active"
            else:
                actual_count = current_sessions.count(user)
                assert actual_count == expected_num_sessions, \
                    f"User {user} has {actual_count} sessions, expected {expected_num_sessions}"

    def verify_sessions_state(self, cli_common,
                              users_sessions_should_be_closed: List[Any] = None,
                              users_sessions_should_be_active: List[Any] = None,
                              wait_time: float = 5) -> None:
        """
        Verify the state of multiple user sessions in the system.

        Args:
            cli_common: CumulusGeneralCli instance for running commands
            users_sessions_should_be_closed: List of session objects that should be closed
            users_sessions_should_be_active: List of session objects that should be active
            wait_time: Time to wait before checking (default: 5 seconds)

        Raises:
            AssertionError: If any session's state does not match the expected state

        Example:
            >>> session_mgr.verify_sessions_state(
            ...     cli_common,
            ...     users_sessions_should_be_closed=[session1, session2],
            ...     users_sessions_should_be_active=[session3]
            ... )
        """
        with allure.step('Verify sessions state'):
            users_sessions_should_be_closed = users_sessions_should_be_closed or []
            users_sessions_should_be_active = users_sessions_should_be_active or []

            time.sleep(wait_time)
            current_sessions = cli_common.who('-u')

            for session in users_sessions_should_be_closed:
                assert session.username not in current_sessions, \
                    f"{session.username} session is still active"

            for session in users_sessions_should_be_active:
                assert session.username in current_sessions, \
                    f"{session.username} session is not active"

    def reset_channels(self) -> None:
        """
        Reset/read all channels on active sessions to clear any pending output.

        This is useful before verification steps to ensure clean state.

        Example:
            >>> session_mgr.reset_channels()
        """
        with allure.step('Reset channels'):
            all_sessions = []
            for sessions in self.sessions_dict.values():
                all_sessions.extend(sessions)

            for session in all_sessions:
                try:
                    session.engine.read_channel()
                except Exception as e:
                    logger.debug(f"Could not reset channel for session: {e}")

    def get_sessions(self, user: str) -> List[Any]:
        """
        Get all sessions for a specific user.

        Args:
            user: Username to get sessions for

        Returns:
            List of session objects for the user

        Example:
            >>> sessions = session_mgr.get_sessions("admin")
            >>> for s in sessions:
            ...     s.run_cmd("show version")
        """
        with self.sessions_dict_lock:
            return list(self.sessions_dict.get(user, []))

    def get_all_sessions(self) -> Dict[str, List[Any]]:
        """
        Get all sessions dictionary.

        Returns:
            Dictionary mapping usernames to their session objects
        """
        with self.sessions_dict_lock:
            return dict(self.sessions_dict)

    def disconnect_all_sessions(self) -> None:
        """
        Disconnect all tracked sessions.

        Example:
            >>> session_mgr.disconnect_all_sessions()
        """
        with allure.step('Disconnect all sessions'):
            with self.sessions_dict_lock:
                for user, sessions in self.sessions_dict.items():
                    for session in sessions:
                        try:
                            session.disconnect()
                        except Exception as e:
                            logger.debug(f"Could not disconnect session for {user}: {e}")

    def clear(self) -> None:
        """
        Clear all session tracking data and thread lists.

        This should be called at the end of each test to reset state.

        Example:
            >>> session_mgr.clear()
        """
        with allure.step('Clear all sessions'):
            self.disconnect_all_sessions()
            with self.sessions_dict_lock:
                self.sessions_dict.clear()
            self.threads.clear()


# Global instance for backward compatibility with existing test code
# Tests can either use this global instance or create their own SessionManager
_global_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """
    Get the global SessionManager instance, creating it if necessary.

    Returns:
        The global SessionManager instance
    """
    global _global_session_manager
    if _global_session_manager is None:
        _global_session_manager = SessionManager()
    return _global_session_manager


def reset_global_session_manager() -> None:
    """
    Reset the global SessionManager instance.

    This clears all sessions and creates a fresh instance.
    """
    global _global_session_manager
    if _global_session_manager is not None:
        _global_session_manager.clear()
    _global_session_manager = SessionManager()
