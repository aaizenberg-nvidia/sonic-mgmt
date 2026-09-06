import logging
import os
import random
import shlex
import signal
import subprocess
import threading
import time
from subprocess import Popen
from typing import Dict, List, NamedTuple, Optional, Tuple

import pytest

from ngts.constants.constants import GnmiConsts
from ngts.nvos_tools.infra.CmdRunner import CmdRunner
from ngts.nvos_constants.constants_nvos import TestFlowType
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.conftest import cleanup_after_aaa
from ngts.tests_nvos.general.security.conftest import local_adminuser
from ngts.tests_nvos.general.security.radius.constants import RadiusVmServer
from ngts.tests_nvos.general.security.security_test_tools.constants import AuthConsts, AaaConsts, AddressingType
from ngts.tests_nvos.general.security.security_test_tools.generic_remote_aaa_testing.constants import (
    RemoteAaaType, ValidValues)
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.RemoteAaaServerInfo import RemoteAaaServerInfo
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.tests_nvos.general.security.tacacs.constants import TacacsDockerServer0
from ngts.tests_nvos.general.security.test_aaa_ldap.ldap_servers_info import LdapServersP3
from ngts.tests_nvos.general.security.api_server.mtls.spiffe_id.constants import SecurityMode
from ngts.tests_nvos.general.security.api_server.mtls.spiffe_id.test_api_mtls_spiffe_id import (
    TestSetup, setup_test, spiffe_setup_users)
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.gnmi_server.mtls.spiffe_id.conftest import cleanup_spiffe_gnmi
from ngts.tests_nvos.general.security.gnmi_server.mtls.spiffe_id.test_gnmi_server_spiffe_id import setup_gnmi_security_mode
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient, GnmicCmdBuilder
from ngts.tests_nvos.system.gnmi.constants import GnmiMode, GNMI_INVALID_AUTH_CLIENT_PASSWORD, GNMI_INVALID_AUTH_CLIENT_USERNAME, \
    GNMI_PAM_BAD_PLAIN_LAUNCH_JOIN_SLACK_SEC, GNMI_PAM_BAD_PLAIN_READY_WAIT_SEC, GNMI_PAM_BAD_PLAIN_THREAD_MIN_BEFORE_GOOD, \
    GNMI_PAM_DUT_TCP_WAIT_POLL_SEC, GNMI_PAM_DUT_TCP_WAIT_TIMEOUT_SEC, \
    GNMI_PAM_GNMIC_TERMINATE_TIMEOUT_SEC, \
    GNMI_PAM_SPIFFE_CAPABILITIES_MAX_ATTEMPTS, GNMI_PAM_SPIFFE_CAPABILITIES_MAX_WAIT_SEC, \
    GNMI_PAM_SPIFFE_CAPABILITIES_RETRY_INTERVAL_SEC, GNMI_PAM_SPIFFE_CAPABILITIES_TRANSIENT_RETRY_SLEEP_SEC, \
    MAX_GNMI_BAD_SUBSCRIBERS, MAX_GNMI_SUBSCRIBERS, GnmicErr
from ngts.tests_nvos.system.gnmi.helpers import verify_gnmi_client, change_interface_description, \
    verify_msg_in_out_or_err, verify_msg_not_in_out_or_err, gnmi_response_overloaded
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_general_utils import wait_for_ldap_nvued_restart_workaround
from ngts.tools.test_utils.switch_recovery import generate_strong_password

logger = logging.getLogger(__name__)


def _count_established_gnmi_tcp_on_dut(dut_engine, port: str = GnmiConsts.GNMI_DEFAULT_PORT) -> int:
    """Count established TCP sockets on the DUT bound to the gNMI listener port (``ss`` local ``sport``)."""
    out = dut_engine.run_cmd(
        f"ss -Htn state established '( sport = :{port} )' | wc -l")
    line = (out or '').strip().splitlines()[-1] if (out or '').strip() else ''
    digits = ''.join(ch for ch in line if ch.isdigit())
    return int(digits) if digits else 0


def _wait_until_at_least_one_gnmi_tcp_connection_closed_on_dut(
        dut_engine,
        timeout_sec: int = GNMI_PAM_DUT_TCP_WAIT_TIMEOUT_SEC,
        poll_sec: float = GNMI_PAM_DUT_TCP_WAIT_POLL_SEC,
) -> None:
    """
    Poll the DUT until ``ss`` shows at least one fewer established connection on ``sport = :9339`` than a
    prior peak, and that peak was at least ``MAX_GNMI_SUBSCRIBERS``.

    When only the test's invalid ``gnmic`` clients use that listener, a peak of ``MAX_GNMI_SUBSCRIBERS``
    and a drop by one corresponds to one fewer bad-client connection; other baseline connections widen
    the peak without changing the ``current <= peak - 1`` condition.
    """
    deadline = time.monotonic() + timeout_sec
    peak = 0
    prev_n = None
    while time.monotonic() < deadline:
        n = _count_established_gnmi_tcp_on_dut(dut_engine)
        if n != prev_n:
            with allure.step(f'established TCP on DUT gNMI port: {n}'):
                pass
            prev_n = n
        peak = max(peak, n)
        if peak >= MAX_GNMI_SUBSCRIBERS and n <= peak - 1:
            with allure.step(
                    f'at least one gNMI TCP connection closed (peak={peak}, current={n}), proceeding'):
                pass
            return
        time.sleep(poll_sec)
    pytest.fail(
        f'timed out after {timeout_sec}s waiting for established gNMI TCP on DUT to drop from a peak of at '
        f'least {MAX_GNMI_SUBSCRIBERS} (last count={_count_established_gnmi_tcp_on_dut(dut_engine)}, peak={peak})')


def _verify_capabilities_failed_when_slots_busy(out: str, err: str) -> None:
    """Valid client may see slot exhaustion or RPC failure depending on gnmi-server behavior."""
    text = (out or '') + (err or '')
    for msg in (
            GnmicErr.NO_SUBSCRIBER_SLOT_AVAILABLE,
            GnmicErr.REQUEST_FAILED,
            GnmicErr.RPC_ERROR,
            GnmicErr.AUTH_FAIL,
            GnmicErr.HANDSHAKE_FAIL,
            GnmicErr.RCV_ERROR,
            GnmicErr.AUTH_SERVICE_UNAVAILABLE,
    ):
        if msg in text:
            return
    pytest.fail(f'expected a gnmi failure indicating overload or RPC error; out={out!r} err={err!r}')


def _start_invalid_gnmi_stream_clients_background(
        dut_ip: str,
        interface_name: str,
        started_clients_evt: Optional[threading.Event] = None,
        started_clients_threshold: int = 1,
        inter_client_delay_sec: float = 0.0,
        max_subscribers: int = MAX_GNMI_SUBSCRIBERS,
        processes: Optional[List[Popen]] = None,
        gnmi_client: Optional[GnmiClient] = None,
) -> Tuple[List[Popen], GnmiClient]:
    """Run ``max_subscribers`` long-lived gnmic subscribe (stream) sessions with bad creds (background).

    ``GnmiMode.ONCE`` is not valid here: ``gnmic_subscribe_interface_and_keep_session_alive`` passes
    ``keep_session_alive=True``, and ``GnmiClient.gnmic_subscribe`` only allows STREAM or POLL in that case.

    Appends each ``Popen`` to ``processes`` as it starts (so teardown can kill partial launches).
    Returns ``(processes, gnmi_client)`` — callers must keep ``gnmi_client`` alive until after
    ``_terminate_gnmic_processes``; otherwise ``CmdRunner.__del__`` kills the subscribers early.
    """
    invalid_client = gnmi_client or GnmiClient(
        dut_ip, GnmiConsts.GNMI_DEFAULT_PORT, GNMI_INVALID_AUTH_CLIENT_USERNAME, GNMI_INVALID_AUTH_CLIENT_PASSWORD)
    processes = processes if processes is not None else []
    started_clients_threshold = max(1, min(started_clients_threshold, max_subscribers))
    for i in range(max_subscribers):
        with allure.step(f'run invalid gnmi client #{i}'):
            processes.append(
                invalid_client.gnmic_subscribe_interface_and_keep_session_alive(
                    GnmiMode.STREAM, interface_name, skip_cert_verify=True))
            if (
                    started_clients_evt is not None and
                    not started_clients_evt.is_set() and
                    (i + 1) >= started_clients_threshold
            ):
                started_clients_evt.set()
            if inter_client_delay_sec > 0:
                time.sleep(inter_client_delay_sec)
    return processes, invalid_client


class PlainBadGnmiBackground(NamedTuple):
    """Background plain-auth invalid STREAM clients: thread handle, gating event, join budget, and Popen list."""

    thread: threading.Thread
    started_evt: threading.Event
    join_timeout_sec: float
    min_before_good: int
    max_bad_subscribers: int
    processes: List[Popen]
    # Keep GnmiClient (and its CmdRunner) alive until after explicit process termination.
    gnmi_client: GnmiClient


def _plain_bad_gnmi_clients_background_thread(
        dut_ip: str,
        interface_name: str,
        max_bad_subscribers: int = MAX_GNMI_BAD_SUBSCRIBERS,
) -> PlainBadGnmiBackground:
    """
    Launch ``max_bad_subscribers`` invalid plain ``gnmic`` STREAM clients on a daemon thread.

    Sets ``started_evt`` after ``min_before_good`` clients have started, where
    ``min_before_good = min(GNMI_PAM_BAD_PLAIN_THREAD_MIN_BEFORE_GOOD, max_bad_subscribers)``.
    ``join_timeout_sec`` allows time to spawn all subscriber processes.
    """
    processes: List[Popen] = []
    started_evt = threading.Event()
    min_before_good = min(GNMI_PAM_BAD_PLAIN_THREAD_MIN_BEFORE_GOOD, max_bad_subscribers)
    join_timeout_sec = float(GNMI_PAM_BAD_PLAIN_LAUNCH_JOIN_SLACK_SEC) + float(max_bad_subscribers)
    # Create the client in the parent so its CmdRunner outlives the launcher thread; otherwise
    # CmdRunner.__del__ kills every subscriber as soon as the thread function returns.
    gnmi_client = GnmiClient(
        dut_ip, GnmiConsts.GNMI_DEFAULT_PORT, GNMI_INVALID_AUTH_CLIENT_USERNAME, GNMI_INVALID_AUTH_CLIENT_PASSWORD)

    def _launch_bad_clients() -> None:
        try:
            _start_invalid_gnmi_stream_clients_background(
                dut_ip,
                interface_name,
                started_clients_evt=started_evt,
                started_clients_threshold=min_before_good,
                inter_client_delay_sec=0.0,
                max_subscribers=max_bad_subscribers,
                processes=processes,
                gnmi_client=gnmi_client,
            )
        except Exception as error:
            # Leave started_evt alone so wait_ready fails if the gate never tripped; partial
            # Popen handles remain in ``processes`` for ``_plain_bad_gnmi_background_cleanup``.
            logger.warning('plain bad gnmi launcher stopped early: %s', error)

    thread = threading.Thread(target=_launch_bad_clients, daemon=True)
    return PlainBadGnmiBackground(
        thread, started_evt, join_timeout_sec, min_before_good, max_bad_subscribers, processes, gnmi_client)


def _plain_bad_gnmi_background_start_and_wait_ready(
        bad_bg: PlainBadGnmiBackground,
        wait_sec: float = GNMI_PAM_BAD_PLAIN_READY_WAIT_SEC,
) -> None:
    bad_bg.thread.start()
    assert bad_bg.started_evt.wait(timeout=wait_sec), (
        f'timed out waiting for first {bad_bg.min_before_good} bad gnmi clients to start'
    )


def _wait_join_plain_bad_clients_thread(bad_clients_thread: threading.Thread, join_timeout_sec: float) -> None:
    bad_clients_thread.join(timeout=join_timeout_sec)
    assert not bad_clients_thread.is_alive(), (
        f'bad clients launch thread did not finish in {join_timeout_sec}s '
        f'(join_timeout_sec budget exhausted)'
    )


def _plain_bad_gnmi_background_join_and_assert_done(bad_bg: PlainBadGnmiBackground) -> None:
    _wait_join_plain_bad_clients_thread(bad_bg.thread, bad_bg.join_timeout_sec)
    assert len(bad_bg.processes) == bad_bg.max_bad_subscribers


def _plain_bad_gnmi_background_cleanup(bad_bg: PlainBadGnmiBackground) -> None:
    """Wait for the launcher to stop adding clients, then terminate everything it started."""
    bad_bg.thread.join(timeout=bad_bg.join_timeout_sec)
    if bad_bg.thread.is_alive():
        logger.warning('plain bad gnmi launcher still alive after %ss; terminating known processes',
                       bad_bg.join_timeout_sec)
    _terminate_gnmic_processes(bad_bg.processes)


def _terminate_gnmic_processes(
        processes: List[Popen],
        timeout_sec: float = GNMI_PAM_GNMIC_TERMINATE_TIMEOUT_SEC,
) -> None:
    """Best-effort SIGTERM→SIGKILL termination of only the gnmic processes started by the current test."""
    for process in list(processes):
        try:
            if process.poll() is not None:
                continue
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                continue
            try:
                process.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=timeout_sec)
                except subprocess.TimeoutExpired:
                    logger.warning('gnmic process pid=%s did not exit after SIGKILL', process.pid)
        except Exception as error:
            logger.warning('Failed to terminate test gnmic process pid=%s: %s', getattr(process, 'pid', '?'), error)


def _start_invalid_gnmi_mtls_stream_clients_background(
        dut_ip: str,
        interface_name: str,
        tls_ca: CertInfo,
        client_cert: CertInfo,
        max_subscribers: int = MAX_GNMI_BAD_SUBSCRIBERS,
        processes: Optional[List[Popen]] = None,
) -> Tuple[List[Popen], CmdRunner]:
    """Long-lived STREAM subscribers with valid mTLS but invalid username/password (background).

    Appends each ``Popen`` to ``processes`` as it starts (so teardown can kill partial launches).
    Returns ``(processes, cmd_runner)`` — callers must keep ``cmd_runner`` alive until after
    ``_terminate_gnmic_processes``; otherwise ``CmdRunner.__del__`` kills every live process at GC.
    """
    cmd_runner = CmdRunner('gnmi_mtls_invalid_stream_clients', 5, True)
    processes = processes if processes is not None else []
    for i in range(max_subscribers):
        gnmic_cmd = (
            GnmicCmdBuilder(host=dut_ip, port=int(GnmiConsts.GNMI_DEFAULT_PORT))
            .user_creds(GNMI_INVALID_AUTH_CLIENT_USERNAME, GNMI_INVALID_AUTH_CLIENT_PASSWORD)
            .ca(tls_ca.cacert)
            .cert(client_cert.private, client_cert.public)
            .subscribe_interface_description(interface_name, GnmiMode.STREAM)
            .format_flat()
            .build()
        )
        with allure.step(f'run invalid gnmi mTLS client #{i}: {gnmic_cmd}'):
            _, _, proc = cmd_runner.run_cmd_in_process(gnmic_cmd, keep_process_alive=True)
            processes.append(proc)
    return processes, cmd_runner


def _gnmic_capabilities_mtls_spiffe_argv(dut_ip: str, tls_ca: CertInfo, client_cert: CertInfo) -> List[str]:
    """Argv list for ``gnmic capabilities`` (no shell); keeps empty ``-u`` / ``-p`` without shlex ambiguity."""
    port = str(GnmiConsts.GNMI_DEFAULT_PORT)
    return [
        'gnmic', '-a', dut_ip, '--port', port,
        '-u', '', '-p', '',
        '--tls-ca', tls_ca.cacert,
        '--tls-key', client_cert.private,
        '--tls-cert', client_cert.public,
        'capabilities',
    ]


def _gnmic_capabilities_mtls_spiffe_success(
        dut_ip: str,
        tls_ca: CertInfo,
        client_cert: CertInfo,
        timeout_sec: int = GNMI_PAM_SPIFFE_CAPABILITIES_MAX_WAIT_SEC,
        max_attempts: int = GNMI_PAM_SPIFFE_CAPABILITIES_MAX_ATTEMPTS,
) -> None:
    """
    Run ``gnmic capabilities`` with mTLS SPIFFE and tolerate a small number of transient races.

    Retries transport races (EOF / Unavailable / refused) and brief overload back-pressure for at most
    ``max_attempts`` (bounded wall ``timeout_sec``). Bound is intentional: these DDoS/slot tests prove
    SPIFFE clients are not starved — an unbounded "eventually succeeds" window would invert that.
    """
    gnmic_argv = _gnmic_capabilities_mtls_spiffe_argv(dut_ip, tls_ca, client_cert)
    gnmic_cmd_log = ' '.join(shlex.quote(a) for a in gnmic_argv)
    transport_transient_markers = (
        'error reading server preface: EOF',
        'rpc error: code = Unavailable',
        'connection error: desc =',
        'connect: connection refused',
    )
    start = time.monotonic()
    attempt = 0
    out, err = '', ''
    result = None
    while attempt < max_attempts:
        attempt += 1
        remaining_sec = timeout_sec - (time.monotonic() - start)
        if remaining_sec <= 0:
            break
        with allure.step(f'run gnmic capabilities mTLS SPIFFE (attempt {attempt}/{max_attempts}): {gnmic_cmd_log}'):
            result = subprocess.run(
                gnmic_argv,
                capture_output=True,
                text=True,
                timeout=max(1, int(min(remaining_sec, 15))),
            )
        out, err = result.stdout, result.stderr
        if result.returncode == 0:
            break
        combined = (out or '') + (err or '')
        is_transport_transient = any(marker in combined for marker in transport_transient_markers)
        is_overload = gnmi_response_overloaded(out, err)
        if not (is_transport_transient or is_overload):
            break
        if attempt >= max_attempts:
            break
        sleep_sec = (
            GNMI_PAM_SPIFFE_CAPABILITIES_RETRY_INTERVAL_SEC if is_overload
            else GNMI_PAM_SPIFFE_CAPABILITIES_TRANSIENT_RETRY_SLEEP_SEC
        )
        time.sleep(min(sleep_sec, max(0.0, remaining_sec)))

    rc = result.returncode if result is not None else 'n/a'
    assert result is not None and result.returncode == 0, (
        f'gnmic capabilities failed after {attempt} attempt(s) (max {max_attempts}) '
        f'rc={rc} out={out!r} err={err!r}'
    )
    # Strict: success output must not contain any gnmic failure signature (including literal ``rpc error``).
    for err_msg in GnmicErr.ALL_ERRS:
        with allure.independent_step(f'verify no error msg: "{err_msg}"'):
            verify_msg_not_in_out_or_err(err_msg, out, err)


@pytest.fixture()
def aaa_users(engines, cleanup_after_aaa) -> Dict[str, UserInfo]:
    with allure.step('set AAA servers'):
        # Copy each server and pin a unique explicit priority: the shared class-level objects
        # would otherwise be mutated across tests, and letting the DUT auto-assign priorities
        # makes collisions ("priority 1 must be unique") depend on the random addressing type.
        with allure.step('set tacacs server'):
            tac_server: RemoteAaaServerInfo = TacacsDockerServer0.SERVER_BY_ADDRESSING_TYPE[
                random.choice(AddressingType.ALL_TYPES)].copy()
            tac_server.priority = ValidValues.PRIORITY[0]
            tac_server.configure(engines, set_explicit_priority=True)
        with allure.step('set ldap server'):
            ldap_server: RemoteAaaServerInfo = LdapServersP3.LDAP3_SERVERS[
                random.choice(AddressingType.ALL_TYPES)].copy()
            ldap_server.priority = ValidValues.PRIORITY[1]
            ldap_server.configure(engines, set_explicit_priority=True)
        with allure.step('set radius server'):
            rad_server: RemoteAaaServerInfo = RadiusVmServer.SERVER_BY_ADDRESSING_TYPE[
                random.choice([AddressingType.IPV4, AddressingType.DN])].copy()
            rad_server.priority = ValidValues.PRIORITY[2]
            rad_server.configure(engines, set_explicit_priority=True)
        with allure.step('enable failthrough'):
            System().aaa.authentication.set(AuthConsts.FAILTHROUGH, AaaConsts.ENABLED, apply=True).verify_result()
    return {RemoteAaaType.TACACS: tac_server.users[0], RemoteAaaType.LDAP: ldap_server.users[0],
            RemoteAaaType.RADIUS: rad_server.users[0], }  # servers config cleared in clear_conf hook func


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
@pytest.mark.parametrize('addressing_type', [AddressingType.IPV4, AddressingType.IPV6])
def test_gnmi_authentication(test_flow, addressing_type, engines, local_adminuser, aaa_users, dut_ipv6_addr):
    """
    verify that gnmi clients must be properly authenticated to subscribe and get updates

    1. set local-user/AAA-method
    2. good-flow: subscribe with valid user credentials
        bad-flow: subscribe with invalid credentials
    3. change port description
    4. good-flow: expect valid user client gets update
        bad-flow: expect invalid user client doesn't get update
    """
    host_address = dut_ipv6_addr if addressing_type == AddressingType.IPV6 else engines.dut.ip

    if addressing_type == AddressingType.IPV6 and not IpTool.is_routable_ipv6(host_address):
        pytest.skip("This setup has only link-local ipv6 address, to run this test need global or unique local")

    system = System()
    auth = system.aaa.authentication
    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).returned_value
    with allure.step(f'change description of interface: "{selected_port.name}"'):
        new_description = change_interface_description(selected_port)
    with allure.step('test with all auth methods'):
        for auth_method in ['default (user admin)', AuthConsts.LOCAL] + RemoteAaaType.ALL_TYPES:
            with allure.independent_step(f'test with auth method: {auth_method}'):
                user = UserInfo(engines.dut.username, engines.dut.password,
                                'admin') if auth_method == 'default' else local_adminuser
                if auth_method in RemoteAaaType.ALL_TYPES:
                    user = aaa_users[auth_method]
                    with allure.step(f'enable {auth_method} authentication'):
                        auth.set(AuthConsts.ORDER, [auth_method, AuthConsts.LOCAL], apply=True).verify_result()
                        if auth_method == RemoteAaaType.LDAP:
                            wait_for_ldap_nvued_restart_workaround(None, engine_to_use=engines.dut)
                        else:
                            time.sleep(3)
                verify_gnmi_client(test_flow, host_address, GnmiConsts.GNMI_DEFAULT_PORT, user.username,
                                   user.password if test_flow == TestFlowType.GOOD_FLOW else 'abcde', True,
                                   GnmicErr.AUTH_FAIL, selected_port, new_port_description_to_check=new_description,
                                   client_cmd_time=20)


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
def test_gnmi_auth_change_local_user_password(test_flow, engines, local_adminuser):
    """
    verify that gnmi properly authenticates local user after password change

    1. change local user's password
    2. good-flow: run client request using new password - expect success
        bad-flow: run client request using old password - expect fail
    """
    with allure.step(f'change password for local user "{local_adminuser.username}"'):
        new_password = generate_strong_password()
        old_password = local_adminuser.password
        System().aaa.user.user_id[local_adminuser.username].set('password', new_password, apply=True).verify_result()
        local_adminuser.password = new_password

    verify_gnmi_client(test_flow, engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, local_adminuser.username,
                       new_password if test_flow == TestFlowType.GOOD_FLOW else old_password, True, GnmicErr.AUTH_FAIL)


@pytest.mark.system
@pytest.mark.gnmi
def test_gnmi_auth_after_remove_local_user(engines, local_adminuser):
    """
    verify that after removing a local user, client cannot request using the credentials of that user

    1. remove local user
    2. run client request using credentials of removed user - expect fail
    """
    with allure.step(f'remove local user "{local_adminuser.username}"'):
        System().aaa.user.user_id[local_adminuser.username].unset(apply=True).verify_result()

    verify_gnmi_client(TestFlowType.BAD_FLOW, engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, local_adminuser.username,
                       local_adminuser.password, True, GnmicErr.AUTH_FAIL, client_cmd_time=20)


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.parametrize('test_flow', TestFlowType.ALL_TYPES)
def test_gnmi_auth_failthrough(test_flow, engines, local_adminuser, aaa_users):
    """
    verify that gnmi client authentication also takes under count the failthrough mechanism properly

    1. configure some auth order with 2 methods (local + remote AAA)
    2. good-flow: enable failthrough
        bad-flow: disable failthrough
    3. run client using credentials of 2nd auth method user
    4. good-flow: expect success
        bad-flow: expect fail
    """
    aaa_users[AuthConsts.LOCAL] = local_adminuser

    rand_aaa_method = random.choice(RemoteAaaType.ALL_TYPES)
    auth_methods = [AuthConsts.LOCAL, rand_aaa_method]
    random.shuffle(auth_methods)

    order = auth_methods
    method2 = auth_methods[1]
    failthrough = AaaConsts.ENABLED if test_flow == TestFlowType.GOOD_FLOW else AaaConsts.DISABLED

    with allure.step(f'set auth order: {order}'):
        system = System()
        system.aaa.authentication.set(AuthConsts.ORDER, order).verify_result()
    with allure.step(f'set failthrough: {failthrough}'):
        system.aaa.authentication.set(AuthConsts.FAILTHROUGH, failthrough, apply=True).verify_result()
        if rand_aaa_method == RemoteAaaType.LDAP:
            wait_for_ldap_nvued_restart_workaround(None, engine_to_use=engines.dut)
        else:
            time.sleep(3)

    user = aaa_users[method2]
    verify_gnmi_client(test_flow, engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, user.username, user.password, True,
                       GnmicErr.AUTH_FAIL)


@pytest.mark.system
@pytest.mark.gnmi
def test_gnmi_auth_existing_streamed_session(engines, local_adminuser):
    """
    verify that when client establishes streamed grpc session with gnmi, any change to user
        (password change, user remove, etc.) doesn't affect/terminate the existing session

    1. set up streamed gnmi session - subscribe to port description
    2. change port description to X
    3. change client user password
    4. change port description to Y
    5. remove the user
    6. change port description to Z
    7. set the user again from scratch
    8. change port description to W
    9. verify that the client received all the port description changes in the existing streaming session
    """
    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).returned_value
    new_descriptions: List[str] = []

    with allure.step('set up streamed gnmi session - subscribe client to port description'):
        client = GnmiClient(engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, local_adminuser.username,
                            local_adminuser.password)
        session = client.gnmic_subscribe_interface_and_keep_session_alive(GnmiMode.STREAM, selected_port.name,
                                                                          skip_cert_verify=True)
    with allure.step('change port description'):
        new_descriptions.append(change_interface_description(selected_port))
    with allure.step(f'change password of user "{local_adminuser.username}"'):
        user_obj = System().aaa.user.user_id[local_adminuser.username]
        user_obj.set('password', generate_strong_password(), apply=True).verify_result()
    with allure.step('change port description'):
        new_descriptions.append(change_interface_description(selected_port))
    with allure.step(f'remove user "{local_adminuser.username}"'):
        user_obj.unset(apply=True).verify_result()
    with allure.step('change port description'):
        new_descriptions.append(change_interface_description(selected_port))
    with allure.step(f'recreate the user "{local_adminuser.username}"'):
        local_adminuser.password = generate_strong_password()
        user_obj.set('password', local_adminuser.password, apply=True).verify_result()
    with allure.step('change port description'):
        new_descriptions.append(change_interface_description(selected_port))
    with allure.step('verify that client received all new descriptions in the existing streaming session'):
        out, err = client.close_session_and_get_out_and_err(session)
        verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, out, err)
        for new_description in new_descriptions:
            with allure.independent_step(f'check that "{new_description}" was streamed'):
                verify_msg_in_out_or_err(new_description, out)


@pytest.mark.system
@pytest.mark.gnmi
def test_gnmi_auth_failing_clients_ddos(engines, local_adminuser):
    """
    verify that gnmi accepts a valid client after at least one failing-auth TCP session has closed on the DUT

    1. run MAX_GNMI_SUBSCRIBERS gnmi clients with bad credentials (in bg/processes)
    2. wait on the DUT until ``ss`` shows at least one established gNMI TCP connection closed (count drops
       by at least one from a peak of at least MAX_GNMI_SUBSCRIBERS)
    3. run gnmic capabilities with valid credentials once
    4. expect success (no ``GnmicErr`` substrings in output)
    """
    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).returned_value
    invalid_clients: List[Popen] = []

    try:
        with allure.step(f'run {MAX_GNMI_SUBSCRIBERS} gnmi clients with bad creds in background'):
            # Hold the GnmiClient for the whole test: CmdRunner.__del__ kills its live processes.
            invalid_clients, _gnmi_client_owner = _start_invalid_gnmi_stream_clients_background(
                engines.dut.ip, selected_port.name, max_subscribers=MAX_GNMI_SUBSCRIBERS,
                processes=invalid_clients)
        with allure.step(
                'wait on DUT until at least one established TCP connection on gNMI port has closed '
                f'(from a peak of at least {MAX_GNMI_SUBSCRIBERS})'):
            _wait_until_at_least_one_gnmi_tcp_connection_closed_on_dut(engines.dut)
        with allure.step('run gnmi client with valid creds'):
            out, err = GnmiClient(engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, local_adminuser.username,
                                  local_adminuser.password).gnmic_capabilities(skip_cert_verify=True,
                                                                               wait_till_done=True)
        with allure.step('expect success'):
            for err_msg in GnmicErr.ALL_ERRS:
                with allure.independent_step(f'verify no error msg: "{err_msg}"'):
                    verify_msg_not_in_out_or_err(err_msg, out, err)
    finally:
        _terminate_gnmic_processes(invalid_clients)


@pytest.mark.system
@pytest.mark.gnmi
def test_gnmi_auth_failing_clients_exhaust_slots_valid_client_rejected(engines, local_adminuser):
    """
    verify that while all subscriber slots are consumed by failing-auth gnmi clients, a valid client cannot connect

    1. start MAX_GNMI_BAD_SUBSCRIBERS bad-credential STREAM clients on a background thread.
    2. after a gated number of bad clients have started, run valid gnmic capabilities (overlaps remaining launches).
    3. join the launcher thread, then expect capabilities failure (overload / RPC).
    """
    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).returned_value

    bad_bg = _plain_bad_gnmi_clients_background_thread(
        engines.dut.ip, selected_port.name, max_bad_subscribers=MAX_GNMI_BAD_SUBSCRIBERS)
    try:
        with allure.step('start bad creds clients in background thread'):
            _plain_bad_gnmi_background_start_and_wait_ready(bad_bg)
        with allure.step('run gnmi client with valid creds while bad clients are launching/running'):
            out, err = GnmiClient(engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, local_adminuser.username,
                                  local_adminuser.password).gnmic_capabilities(skip_cert_verify=True,
                                                                               wait_till_done=True)
        with allure.step('expect failure'):
            _verify_capabilities_failed_when_slots_busy(out, err)
        with allure.step('wait for bad clients launch thread to finish'):
            _plain_bad_gnmi_background_join_and_assert_done(bad_bg)
    finally:
        _plain_bad_gnmi_background_cleanup(bad_bg)


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.security
@pytest.mark.mtls
def test_gnmi_auth_failing_plain_clients_ddos_mtls_spiffe_valid_client_succeeds(
        engines, dut_hostname, scp_player):
    """
    Verify that with gNMI ``mTLS`` + SPIFFE enabled, a valid SPIFFE client can run ``capabilities`` while
    ``MAX_GNMI_BAD_SUBSCRIBERS`` background clients repeatedly attempt subscribe with invalid username/password
    using the plain auth helper.

    Steps:
    1. Build SPIFFE test setup and configure gNMI security mode to ``mTLS``.
    2. Start ``MAX_GNMI_BAD_SUBSCRIBERS`` invalid plain ``gnmic`` clients on a background thread (gated start count).
    3. Run SPIFFE ``gnmic capabilities`` while bad clients still launch; expect success.
    4. Join launcher thread; cleanup SPIFFE/mTLS (including AAA users) in outer ``finally``.
    """
    setup: Optional[TestSetup] = None
    try:
        setup = setup_test(dut_hostname, engines, scp_player, cert_name_prefix='gnmi-pam-mtls-ddos')
        selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).returned_value
        bad_bg = _plain_bad_gnmi_clients_background_thread(
            engines.dut.ip, selected_port.name, max_bad_subscribers=MAX_GNMI_BAD_SUBSCRIBERS)
        with allure.step('configure gNMI mTLS + SPIFFE (users, certs, gnmi-server)'):
            setup_gnmi_security_mode(SecurityMode.MTLS, setup.server_cert, setup.server_ca)
        try:
            with allure.step(
                    f'start {MAX_GNMI_BAD_SUBSCRIBERS} invalid plain gnmi clients in background thread '
                    f'(after {bad_bg.min_before_good} started, run foreground request)'):
                _plain_bad_gnmi_background_start_and_wait_ready(bad_bg)
            with allure.step('run one valid SPIFFE gnmic capabilities request while bad clients launch'):
                _gnmic_capabilities_mtls_spiffe_success(
                    engines.dut.ip,
                    setup.server_cert,
                    setup.cert_spif_of_user1_1,
                )
            with allure.step('wait for bad clients launch thread to finish'):
                _plain_bad_gnmi_background_join_and_assert_done(bad_bg)
        finally:
            _plain_bad_gnmi_background_cleanup(bad_bg)
    finally:
        cleanup_spiffe_gnmi(spiffe_setup_users(setup))


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.security
@pytest.mark.mtls
def test_gnmi_auth_failing_clients_exhaust_slots_mtls_spiffe_valid_client_succeeds(
        engines, dut_hostname, scp_player):
    """
    Verify that with gNMI ``mTLS`` + SPIFFE auth enabled, a valid SPIFFE client is still accepted even when
    ``MAX_GNMI_BAD_SUBSCRIBERS`` background clients continuously attempt subscribe with wrong username/password.

    Rationale:
    - In plain PAM-only flow (``test_gnmi_auth_failing_clients_exhaust_slots_valid_client_rejected``), many bad
      clients may consume subscriber slots and a valid PAM client can be rejected.
    - In mTLS + SPIFFE flow, we expect the valid SPIFFE client to complete ``capabilities`` successfully.

    Steps:
    1. Build SPIFFE test setup (users, SPIFFE mappings, server cert, client certs/CA import).
    2. Configure gNMI server security mode to ``mTLS`` using the generated server cert and client CA.
    3. Pick a random interface path for long-lived subscribe sessions.
    4. Start ``MAX_GNMI_BAD_SUBSCRIBERS`` background STREAM clients with:
       - valid mTLS material (trusted CA + client cert/key),
       - intentionally wrong username/password.
    5. Start a foreground ``gnmic capabilities`` request using:
       - trusted server CA,
       - SPIFFE-bound client cert (no password auth expected).
    6. Verify the foreground request returns success and does not contain any ``GnmicErr`` signatures.
    7. Always run SPIFFE/mTLS cleanup (including AAA users) in ``finally`` to leave the DUT state clean.
    """
    setup: Optional[TestSetup] = None
    invalid_clients: List[Popen] = []
    try:
        setup = setup_test(dut_hostname, engines, scp_player, cert_name_prefix='gnmi-pam-mtls-slots')
        selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).returned_value
        with allure.step('configure gNMI mTLS + SPIFFE (users, certs, gnmi-server)'):
            setup_gnmi_security_mode(SecurityMode.MTLS, setup.server_cert, setup.server_ca)
        with allure.step(
                f'run {MAX_GNMI_BAD_SUBSCRIBERS} gnmic STREAM clients: valid mTLS, invalid user/password (background)'):
            # Hold the CmdRunner for the whole test: its __del__ kills every live process.
            invalid_clients, _mtls_cmd_runner = _start_invalid_gnmi_mtls_stream_clients_background(
                engines.dut.ip,
                selected_port.name,
                setup.server_cert,
                setup.cert_no_spif,
                max_subscribers=MAX_GNMI_BAD_SUBSCRIBERS,
                processes=invalid_clients,
            )
        with allure.step('run gnmic capabilities as SPIFFE user1 (no password) while background clients run'):
            _gnmic_capabilities_mtls_spiffe_success(
                engines.dut.ip,
                setup.server_cert,
                setup.cert_spif_of_user1_1,
            )
    finally:
        try:
            _terminate_gnmic_processes(invalid_clients)
        finally:
            cleanup_spiffe_gnmi(spiffe_setup_users(setup))
