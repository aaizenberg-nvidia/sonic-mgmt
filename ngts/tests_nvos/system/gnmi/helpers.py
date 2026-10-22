import logging
import math
import os
import random
import re
import string
import subprocess
import threading
import time
import json
from collections import defaultdict
from operator import itemgetter
from typing import NamedTuple, Optional, Tuple, List

from retry import retry

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.linux_tools.linux_tools import scp_file
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import HealthConsts, NvosConst, DatabaseConst, SystemConsts, TestFlowType
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.CmdRunner import CmdRunner
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.conftest import get_dut_hostname
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.certificate.helpers import import_certificates
from ngts.tests_nvos.general.security.helpers import setup_certs_for_tests, cleanup_certs_for_tests
from ngts.tests_nvos.general.security.mtls.generic_testing.constants import CA_CERTIFICATE
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.tests_nvos.helpers.general_helpers import run_cmd
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient, GnmicCmdBuilder, compose_subscribe_stream_flags
from ngts.tests_nvos.system.gnmi.constants import CERTIFICATE, GnmiServerStatus, GnmicErr
from ngts.tests_nvos.system.gnmi.constants import DUT_GNMI_CERTS_DIR, DOCKER_CERTS_DIR, GnmiMode, GrpcMsg, \
    SERVER_REFLECTION_SUBSCRIBE_RESPONSE
from ngts.tests_nvos.system.gnmi.constants import (
    FLOOD_COLLECT_GRACE_SEC, FLOOD_PROCESS_INIT_DELAY_SEC,
    HEARTBEAT_EPOCHS_TO_OBSERVE, HEARTBEAT_TOLERANCE_FACTOR, HEARTBEAT_WINDOW_SLACK_SEC,
    NANOSEC_PER_SEC, PER_REQUEST_TIMEOUT_SEC, RECONNECT_CAPABILITIES_MAX_WAIT_SEC,
    RECONNECT_CAPABILITIES_RETRY_INTERVAL_SEC, SAMPLE_ERROR_MAX_LEN,
    STREAM_SUBSCRIBE_WINDOW_SEC)
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


def validate_memory_and_cpu_utilization():
    system = System()
    output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.show("memory")).get_returned_value()
    memory_util = output_dictionary[SystemConsts.MEMORY_PHYSICAL_KEY]["utilization"]
    assert SystemConsts.MEMORY_PERCENT_THRESH_MIN < memory_util < SystemConsts.MEMORY_PERCENT_THRESH_MAX, "Physical utilization percentage is out of range"
    output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.show("cpu")).get_returned_value()
    cpu_utilization = output_dictionary[SystemConsts.CPU_TOTAL_UTILIZATION_KEY]
    logger.info(f"cpu utilization: {cpu_utilization}")
    assert cpu_utilization < SystemConsts.CPU_PERCENT_THRESH_MAX, "CPU utilization: {actual}% is higher than the maximum limit of: {expected}%" \
                                                                  "".format(actual=cpu_utilization,
                                                                            expected=SystemConsts.CPU_PERCENT_THRESH_MAX)


def validate_cpu_utilization_with_retry(check_individual_cores=False, max_retries=3, retry_interval=2):
    system = System()

    for attempt in range(max_retries):
        cpu_show = OutputParsingTool.parse_json_str_to_dictionary(system.show("cpu")).get_returned_value()

        if check_individual_cores:
            # Check each CPU core individually
            cpu_cores = cpu_show[SystemConsts.CPU_CORES].keys()
            high_cpu_cores = []

            for core in cpu_cores:
                cpu_utilization = cpu_show[SystemConsts.CPU_CORES][core][SystemConsts.CPU_UTILIZATION_KEY]
                if cpu_utilization >= SystemConsts.CPU_PERCENT_THRESH_MAX:
                    high_cpu_cores.append((core, cpu_utilization))

            if not high_cpu_cores:
                # All cores are within acceptable limits
                logger.info(f"All CPU cores are within acceptable limits on attempt {attempt + 1}")
                break
            elif attempt < max_retries - 1:
                # Some cores are still high, wait and retry
                logger.info(f"Attempt {attempt + 1}: High CPU cores detected: {high_cpu_cores}. Retrying in {retry_interval} seconds...")
                time.sleep(retry_interval)
            else:
                # Final attempt failed, report the issue
                high_cpu_details = ", ".join([f"{core}: {util}%" for core, util in high_cpu_cores])
                assert False, \
                    "CPU utilization spikes detected after {retries} attempts. High CPU cores: {cores}. " \
                    "This may indicate temporary system load or background processes.". \
                    format(retries=max_retries, cores=high_cpu_details)
        else:
            # Check total CPU utilization
            cpu_utilization = cpu_show[SystemConsts.CPU_TOTAL_UTILIZATION_KEY]
            logger.info(f"Total CPU utilization attempt {attempt + 1}: {cpu_utilization}%")

            if cpu_utilization < SystemConsts.CPU_PERCENT_THRESH_MAX:
                # CPU utilization is within acceptable limits
                break
            elif attempt < max_retries - 1:
                # CPU utilization is still high, wait and retry
                logger.info(f"Total CPU utilization {cpu_utilization}% exceeds threshold {SystemConsts.CPU_PERCENT_THRESH_MAX}%. Retrying in {retry_interval} seconds...")
                time.sleep(retry_interval)
            else:
                # Final attempt failed, report the issue
                assert False, "Total CPU utilization: {actual}% is higher than the maximum limit of: {expected}% after {retries} attempts. " \
                    "This may indicate temporary system load or background processes.". \
                    format(actual=cpu_utilization, expected=SystemConsts.CPU_PERCENT_THRESH_MAX, retries=max_retries)


def run_gnmi_client_in_the_background(target_ip, xpath, device):
    prefix_and_path = xpath.rsplit("/", 1)
    command = f"gnmic -a {target_ip} --port {GnmiConsts.GNMI_DEFAULT_PORT} --skip-verify subscribe " \
        f"--prefix '{prefix_and_path[0]}' --path '{prefix_and_path[1]}' --target nvos " \
        f"-u {device.default_username} -p {device.default_password} --format flat"
    # Use the subprocess.Popen function to run the command in the background
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               preexec_fn=os.setsid)
    return process


def gnmi_basic_flow(engines, mode='', ipv6=False, mgmt_port_name='eth0', timeout_sec=4):
    """
    Check gnmi basic flow: show command , disable and enable commands, validate stream updates to gnmi-client.
        Test flow:
            1. validate gnmi-server is running
            2. validate health status is OK
            3. change port description
            5. validate gnmi-server stream updates
            6. Disable gnmi-server
            7. validate gnmi-server is not running
            8. validate health status is OK
            9. enable gnmi-server
            10. validate gnmi-server is running
            11. validate gnmi-server stream updates
    """
    system = System()
    gnmi_server_obj = system.gnmi_server
    target_ip = Port(mgmt_port_name).interface.get_ipv6_address() if ipv6 else engines.dut.ip
    validate_gnmi_is_running_and_stream_updates(system, gnmi_server_obj, engines, target_ip, mode=mode,
                                                timeout_sec=timeout_sec)

    with allure.step('Disable gnmi'):
        gnmi_server_obj.disable_gnmi_server()
        validate_gnmi_disabled_and_not_running(gnmi_server_obj, engines)
        validate_gnmi_server_in_health_issues(system, expected_gnmi_health_issue=False)

    with allure.step('Enable gnmi'):
        gnmi_server_obj.enable_gnmi_server()
        validate_gnmi_is_running_and_stream_updates(system, gnmi_server_obj, engines, target_ip, mode=mode,
                                                    timeout_sec=timeout_sec)


def validate_gnmi_is_running_and_stream_updates(system, gnmi_server_obj, engines, target_ip, mode='', username='',
                                                password='', timeout_sec=4):
    with allure.step('Validate gnmi is running and stream updates'):
        validate_gnmi_enabled_and_running(gnmi_server_obj, engines)
        validate_gnmi_server_in_health_issues(system, expected_gnmi_health_issue=False)
        port_description = Tools.RandomizationTool.get_random_string(7)
        change_port_description_and_validate_gnmi_updates(engines, port_description=port_description,
                                                          target_ip=target_ip, mode=mode, username=username,
                                                          password=password, timeout_sec=timeout_sec)


@retry(Exception, tries=6, delay=2)
def validate_gnmi_server_docker_state(engines, should_run=True):
    """Assert the nv-gnmi container is running or stopped (docker ps only)."""
    cmd_output = engines.dut.run_cmd('docker ps |grep {}'.format(GnmiConsts.GNMI_DOCKER))
    should_run_str = '' if should_run else 'not'
    is_running_str = '' if cmd_output else 'not'
    assert bool(cmd_output) == should_run, f"The gnmi-server docker is {is_running_str} running, " \
        f"but we expect it {should_run_str} to run"


@retry(AssertionError, tries=5, delay=2)
def wait_for_gnmi_ready(engines, socket_path=GnmiConsts.GNMI_SOCKET_PATH):
    """
    Wait until the gNMI agent is ready after container start or node restart.

    Envoy may be up while the gNMI agent is still initializing; require the container
    (validate_gnmi_server_docker_state), the agent socket, then a short stabilization period.
    """
    validate_gnmi_server_docker_state(engines, should_run=True)
    socket_check = engines.dut.run_cmd(f"test -S {socket_path} && echo ok")
    assert "ok" in socket_check, f"gNMI socket {socket_path} does not exist"
    logger.info(f"gNMI container and socket ready; waiting {GnmiConsts.GNMI_READY_STABILIZATION_SEC}s to be sure gnmi is up fully")
    time.sleep(GnmiConsts.GNMI_READY_STABILIZATION_SEC)


def validate_show_gnmi(gnmi_server_obj, engines, gnmi_state=GnmiConsts.GNMI_STATE_ENABLED):
    gnmi_server_obj.compare_show_gnmi_output(
        expected={GnmiConsts.GNMI_STATE_FIELD: gnmi_state})
    should_run = gnmi_state == GnmiConsts.GNMI_STATE_ENABLED
    validate_gnmi_server_docker_state(engines, should_run=should_run)


def validate_gnmi_enabled_and_running(gnmi_server_obj, engines):
    validate_show_gnmi(gnmi_server_obj, engines, gnmi_state=GnmiConsts.GNMI_STATE_ENABLED)


def validate_gnmi_disabled_and_not_running(gnmi_server_obj, engines):
    validate_show_gnmi(gnmi_server_obj, engines, gnmi_state=GnmiConsts.GNMI_STATE_DISABLED)


def run_gnmi_client_and_parse_output(engines, devices, xpath, target_ip, target_port=GnmiConsts.GNMI_DEFAULT_PORT,
                                     mode='', username='', password='', timeout_sec=4):
    username = username or devices.dut.default_username
    password = password or devices.dut.default_password
    with allure.step("run gnmi-client and parse output"):
        sonic_mgmt_engine = engines.sonic_mgmt
        prefix_and_path = xpath.rsplit("/", 1)
        mode_flag = f"--mode {mode}" if mode else ''
        cmd = f"gnmic -a {target_ip} --port {target_port} --skip-verify subscribe --prefix '{prefix_and_path[0]}'" \
            f" --path '{prefix_and_path[1]}' --target nvos -u {username} " \
            f"-p {password} {mode_flag} --format flat"
        cmd = f"timeout -s INT {timeout_sec}s " + cmd
        logger.info(f"run on the sonic mgmt docker {sonic_mgmt_engine.ip}: {cmd}")
        if "poll" == mode:
            gnmi_client_output = sonic_mgmt_engine.run_cmd_set([cmd, '\n', '\n', '\x03', '\x03'],
                                                               patterns_list=["select target to poll:",
                                                                              "select subscription to poll:",
                                                                              "failed selecting target to poll:"])
            gnmi_client_output = re.findall(f"{re.escape(xpath)}:\\s+\\w+", gnmi_client_output)[0]
        elif "once" == mode:
            gnmi_client_output = sonic_mgmt_engine.run_cmd(cmd)
            gnmi_client_output = re.split(r"received\s+signal.*", gnmi_client_output, flags=re.IGNORECASE)[0]
            gnmi_client_output = re.sub(r'(\\["\\n]+|\s+)', '', gnmi_client_output.split(":")[-1])
        else:
            gnmi_client_output = sonic_mgmt_engine.run_cmd(cmd)
            gnmi_client_output = re.sub(r"\^C(.*\n.*)*", '', gnmi_client_output)
            gnmi_client_output = re.split(r"received\s+signal.*", gnmi_client_output, flags=re.IGNORECASE)[0]
            gnmi_client_output = re.sub(r'(\\["\\n]+|\s+)', '', gnmi_client_output.split(":")[-1])

        gnmi_updates_dict = {}
        for item in gnmi_client_output.split('\n'):
            if item.strip():
                item_as_list = item.split(":")
                key = re.sub(r"\s+\[|\]", '', item_as_list[0])
                value = re.sub(r"\s|\r|\"", '', item_as_list[-1])
                gnmi_updates_dict.update({key: value})
        return gnmi_updates_dict


def _extract_json_objects_from_gnmic_output(gnmi_output):
    """
    Extract JSON objects from gnmic output that may include prompt/log lines.
    """
    decoder = json.JSONDecoder()
    parsed_objects = []
    idx = 0
    while idx < len(gnmi_output):
        start = gnmi_output.find("{", idx)
        if start == -1:
            break
        try:
            obj, end = decoder.raw_decode(gnmi_output[start:])
            if isinstance(obj, dict):
                parsed_objects.append(obj)
            idx = start + end
        except ValueError:
            idx = start + 1
    return parsed_objects


def _compose_multi_path_subscribe_cmd(target_ip, prefix, paths, target_port, mode, username, password,
                                      sample_interval=None):
    """
    Build the gnmic multi-path subscribe command string (no timeout wrapper).

    When `sample_interval` is set (e.g. '1s'), a STREAM subscription is requested in SAMPLE
    stream-mode so the server emits a fresh result-set every interval rather than a single
    snapshot.
    """
    mode_flag = f"--mode {mode}" if mode else ''
    stream_flags = compose_subscribe_stream_flags(sample_interval=sample_interval).strip()
    path_flags = " ".join([f"--path '{path}'" for path in paths])
    return (
        f"gnmic -a {target_ip} --port {target_port} --skip-verify subscribe "
        f"--prefix '{prefix}' {path_flags} --target nvos -u {username} -p {password} "
        f"{mode_flag} {stream_flags} --format json"
    )


def run_gnmi_client_and_parse_multi_path_json_output(
        engines, devices, target_ip, prefix, paths, target_port=GnmiConsts.GNMI_DEFAULT_PORT,
        mode=GnmiMode.POLL, username='', password=''):
    """
    Run gnmic subscribe with multiple --path entries and parse --format json output.
    Returns a list of parsed JSON objects from mixed gnmic output.
    """
    assert paths, "paths list cannot be empty"

    username = username or devices.dut.default_username
    password = password or devices.dut.default_password

    with allure.step("run gnmi-client (multi-path json) and parse output"):
        sonic_mgmt_engine = engines.sonic_mgmt
        cmd = _compose_multi_path_subscribe_cmd(target_ip, prefix, paths, target_port, mode, username, password)
        cmd = f"timeout -s INT {STREAM_SUBSCRIBE_WINDOW_SEC}s " + cmd
        logger.info(f"run on the sonic mgmt docker {sonic_mgmt_engine.ip}: {cmd}")

        if mode == GnmiMode.POLL:
            gnmi_client_output = sonic_mgmt_engine.run_cmd_set(
                [cmd, '\n', '\n', '\x03', '\x03'],
                patterns_list=[
                    "select target to poll:",
                    "select subscription to poll:",
                    "failed selecting target to poll:",
                ],
            )
        else:
            gnmi_client_output = sonic_mgmt_engine.run_cmd(cmd)

        verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, gnmi_client_output)

        return _extract_json_objects_from_gnmic_output(gnmi_client_output)


class StreamSubscription(NamedTuple):
    """
    One gnmic STREAM subscription, as consumed by `run_stream_subscribers`.

    `label` names the subscription in failure messages, which matters for mixed-cost batches
    where "subscriber 9" alone does not say whether it was the heavy or a light one.
    """
    paths: List[str]
    prefix: str = ''
    stream_mode: Optional[str] = None
    sample_interval: Optional[str] = None
    heartbeat_interval: Optional[str] = None
    suppress_redundant: bool = False
    updates_only: bool = False
    label: str = ''


class SubscriberResult(NamedTuple):
    """
    What one gnmic STREAM subscription produced.

    `err` is kept alongside the parsed notifications so callers can tell a genuine heartbeat gap
    from a subscription the agent rejected outright (e.g. a sample interval below its minimum),
    which would otherwise look like a leaf that never streamed.

    `out` is the unparsed stdout. It is the only evidence left when a subscription yields no
    notifications *and* reports no recognized error, so it is kept rather than discarded after
    parsing.
    """
    notifications: list
    err: str
    out: str = ''
    label: str = ''

    def failed(self) -> bool:
        """True when either stream reports a failure - some gnmic paths report it on stdout."""
        return is_gnmi_failure(self.err) or is_gnmi_failure(self.out)

    def failure_text(self) -> str:
        """Whichever stream carries the failure, for reporting."""
        return self.err if is_gnmi_failure(self.err) else (self.out if is_gnmi_failure(self.out) else '')


def run_stream_subscribers(target_ip, subscriptions, window_sec, username, password,
                           target_port=GnmiConsts.GNMI_DEFAULT_PORT, mid_window_action=None,
                           action_at_sec=None):
    """
    Open one concurrent gnmic STREAM subscription per entry in `subscriptions`, stream for
    `window_sec` seconds, and return a list of SubscriberResult aligned with `subscriptions`.

    Every subscription is started before any waiting, so they are all alive simultaneously for the
    whole window. `mid_window_action` is a callable invoked `action_at_sec` into the window, which
    is how perturbation tests inject a change (link down, feature toggle, ...) while heartbeats are
    being observed.

    Failures are reported through each result rather than asserted here, so a caller can tell an
    agent-policy refusal (interval too low) apart from everything else and decide what each one
    means - see verify_subscribers_streamed.
    """
    assert subscriptions, 'subscriptions list cannot be empty'
    for subscription in subscriptions:
        assert subscription.paths, f'paths list cannot be empty for subscription {subscription.label!r}'
    assert action_at_sec is None or mid_window_action is not None, \
        'action_at_sec was given without a mid_window_action to run'

    # Drive gnmic through GnmiClient instead of hand-rolled gnmic strings. One client is built once
    # and reused for every subscription (gnmi guideline 1: verify_tools_installed=False). The output
    # is read once the sessions close, so spool_output keeps a long window over many paths from
    # filling the pipe buffer and blocking gnmic mid-stream.
    #
    # log_to_stderr is essential rather than cosmetic here: gnmic retries a rejected subscription
    # on its --retry timer and, with its logger off, writes nothing at all to either stream. A
    # refusal (bad path, expired account) would then be indistinguishable from a server that
    # accepted the subscription and stopped sending - i.e. it would read as a heartbeat failure.
    client = GnmiClient(target_ip, target_port, username, password, cmd_time=window_sec,
                        verify_tools_installed=False, print_outputs=False, spool_output=True)

    sessions = []
    with allure.step(f"start {len(subscriptions)} concurrent STREAM subscription(s)"):
        for subscription in subscriptions:
            _, _, _, proc = client.gnmic_subscribe_multi_path(
                prefix=subscription.prefix, paths=subscription.paths, mode=GnmiMode.STREAM,
                stream_mode=subscription.stream_mode, sample_interval=subscription.sample_interval,
                heartbeat_interval=subscription.heartbeat_interval,
                suppress_redundant=subscription.suppress_redundant,
                updates_only=subscription.updates_only, log_to_stderr=True,
                skip_cert_verify=True, keep_session_alive=True, wait_till_done=False)
            sessions.append(proc)

    _stream_for_window(window_sec, mid_window_action, action_at_sec)

    # Stop each subscription explicitly (gnmi guideline 6) and parse its own output, so the
    # per-subscription notifications are never merged. Both raw streams are kept on the result so
    # failure detection (gnmi guideline 3) can scan either one and callers can report them verbatim.
    results = []
    with allure.step("stop subscriptions and parse each one's notifications"):
        for idx, (subscription, proc) in enumerate(zip(subscriptions, sessions)):
            out, err = client.close_session_and_get_out_and_err(proc)
            results.append(SubscriberResult(
                notifications=_extract_json_objects_from_gnmic_output(out),
                err=err,
                out=out,
                label=subscription.label or f"subscriber {idx}",
            ))
    return results


def gnmi_once_notifications(client, path, prefix=''):
    """
    Data notifications from a single ONCE subscribe on `path`, and the raw streams behind them.

    Returns ``(notifications, out, err)``. Callers that only need "does this exist" want
    `gnmi_path_is_exposed`; this one is for callers that also need the leaf set the server
    enumerated, e.g. to size a subscription against what the DUT actually exposes.
    """
    out, err, _, _ = client.gnmic_subscribe_multi_path(
        prefix=prefix, paths=[path], mode=GnmiMode.ONCE, skip_cert_verify=True,
        wait_till_done=True)
    notifications = [obj for obj in _extract_json_objects_from_gnmic_output(out)
                     if _is_data_notification(obj)]
    return notifications, out, err


def gnmi_path_is_exposed(client, path, prefix=''):
    """
    True when a ONCE subscribe on `path` returns at least one data notification.

    Lets a test tell "this DUT does not model this subtree" (skip) apart from "the subtree is
    modelled but stopped streaming" (fail). Without that split, a platform-specific family like
    phy-diag/state would report a hard failure on every platform that does not expose it.

    Only a YANG-model rejection makes a path "not exposed". Any other failure - an expired
    account, a rate limit, a transport error - means the probe never learned whether the path
    exists, so it is raised rather than reported as absent: laundering those into a skip is how a
    broken switch reads as a platform that simply lacks the feature.
    """
    notifications, out, err = gnmi_once_notifications(client, path, prefix=prefix)
    if notifications:
        return True
    for stream in (err, out):
        if not is_gnmi_failure(stream):
            continue
        # Explicit raise rather than a bare assert (which python -O strips, as in
        # ib/helpers.py::_assert_gnmi_ok): stripping this one would fall through to "not exposed"
        # and skip the caller, which is the one outcome this predicate exists to prevent.
        if not is_path_not_modelled(stream):
            raise AssertionError(
                f"the ONCE probe of {path!r} was refused for a reason unrelated to the YANG "
                f"model, so whether this DUT exposes the path is unknown: {stream.strip()}")
        logger.info(f"ONCE probe of {path!r} was rejected as absent from the YANG model, treating "
                    f"it as not exposed")
        return False
    return False


def gnmi_service_refused_reason(client):
    """
    Why the gNMI server is not serving this client, or '' when it is.

    Meant to be called *after* a subscription delivered nothing, to tell "the server refused us"
    apart from "the server accepted us and then went quiet". gnmic retries a refused subscription
    on its own ``--retry`` timer (10s by default) and writes nothing to either stream unless
    ``--log`` is set, so a refusal is otherwise indistinguishable from silence - and a heartbeat
    test would report it as a leaf that stopped being resent.

    A per-module reachability check cannot cover this: the refusal can start mid-run, e.g. when the
    DUT admin password expires between one test and the next.
    """
    out, err = client.gnmic_capabilities(skip_cert_verify=True)
    if is_gnmi_failure(err):
        return err.strip()
    return out.strip() if is_gnmi_failure(out) else ''


def verify_subscribers_streamed(results, fail_on_rejected: bool = True):
    """
    Assert no subscription failed, and report which agent-policy rejections were seen.

    Returns the labels of the subscriptions whose sample interval the agent refused. A refusal is
    kept apart from the other failures because it is the one that could legitimately be a platform
    limit rather than a defect - but on nv-gnmi the intervals this suite uses are accepted, so
    `fail_on_rejected` fails on them too by default. Pass False against an agent that enforces a
    higher minimum, and skip on the returned labels instead; a refused subscription streams
    nothing, so leaving it unhandled reads as the server going quiet.
    """
    rejected = [result.label for result in results
                if is_sample_interval_rejected(result.out, result.err)]
    hard_failures = {
        result.label: result.failure_text() for result in results
        if result.failed() and result.label not in rejected
    }
    assert not hard_failures, f"gnmic subscription(s) failed: {hard_failures}"
    if fail_on_rejected:
        assert not rejected, (
            f"the gNMI agent refused the requested sample interval for {len(rejected)} of "
            f"{len(results)} subscription(s) {rejected}, so they streamed nothing; nv-gnmi is "
            f"expected to accept the intervals this suite uses"
        )
    return rejected


def _stream_for_window(window_sec, mid_window_action=None, action_at_sec=None):
    """Sleep out the streaming window, running `mid_window_action` partway through if given."""
    if mid_window_action is None:
        with allure.step(f"let subscriptions stream for {window_sec}s"):
            time.sleep(window_sec)
        return

    action_at_sec = action_at_sec if action_at_sec is not None else window_sec // 2
    assert 0 < action_at_sec < window_sec, (
        f"mid-window action must land inside the {window_sec}s window; got {action_at_sec}s")
    with allure.step(f"stream for {action_at_sec}s before the mid-window action"):
        time.sleep(action_at_sec)
    with allure.step("run the mid-window action while the subscriptions are streaming"):
        mid_window_action()
    with allure.step(f"keep streaming for the remaining {window_sec - action_at_sec}s"):
        time.sleep(window_sec - action_at_sec)


def run_concurrent_multi_path_stream_subscribers(
        devices, target_ip, prefix, paths, num_subscribers,
        window_sec=STREAM_SUBSCRIBE_WINDOW_SEC, target_port=GnmiConsts.GNMI_DEFAULT_PORT,
        username='', password='', sample_interval=None, heartbeat_interval=None,
        suppress_redundant: bool = False, fail_on_rejected: bool = True):
    """
    Open `num_subscribers` concurrent gnmic STREAM subscriptions on the same multi-path
    set and stream for `window_sec` seconds.

    Returns `(per_subscriber, rejected)`. `per_subscriber[i]` is the parsed JSON notifications
    observed by subscriber i. `rejected` names the subscribers the agent refused over
    sample-interval policy, and is empty unless `fail_on_rejected` is False - see
    verify_subscribers_streamed for what that flag decides.

    When `sample_interval` is set (e.g. '1s'), each subscriber streams in SAMPLE mode and
    receives a fresh result-set every interval, so a `window_sec` window yields multiple
    result-sets per subscriber instead of a single snapshot. `suppress_redundant` and
    `heartbeat_interval` add the suppress/heartbeat semantics on top.
    """
    assert num_subscribers >= 1, f"num_subscribers must be >= 1; got {num_subscribers}"

    subscription = StreamSubscription(
        paths=paths, prefix=prefix, sample_interval=sample_interval,
        heartbeat_interval=heartbeat_interval, suppress_redundant=suppress_redundant)
    results = run_stream_subscribers(
        target_ip=target_ip,
        subscriptions=[subscription._replace(label=f"subscriber {idx}")
                       for idx in range(num_subscribers)],
        window_sec=window_sec,
        username=username or devices.dut.default_username,
        password=password or devices.dut.default_password,
        target_port=target_port,
    )
    rejected = verify_subscribers_streamed(results, fail_on_rejected=fail_on_rejected)
    return [result.notifications for result in results], rejected


def _is_data_notification(obj):
    """Return True for a real gNMI data Notification (not a sync-response / timestamp-less control frame)."""
    return bool(obj.get("timestamp") is not None and not obj.get("sync-response"))


def count_result_sets(notifications):
    """Return the number of distinct streamed result-sets (one per gNMI Notification timestamp)."""
    timestamps = {obj.get("timestamp") for obj in notifications if _is_data_notification(obj)}
    return len(timestamps)


def _flatten_leaf_values(values, prefix=""):
    """Flatten a gnmic `values` mapping into ``{relative leaf path: value}``.

    Leaf subscriptions yield a flat ``{"in-octets": 1}`` mapping, but a subtree subscription
    (e.g. the whole ``phy-diag/state``) nests the leaves, so recurse. An empty dict is kept as
    a terminal leaf so a present-but-empty subtree is still reported.
    """
    if not isinstance(values, dict):
        return {prefix: values} if prefix else {}

    leaves = {}
    for key, value in values.items():
        child = f"{prefix}/{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            leaves |= _flatten_leaf_values(value, child)
        else:
            leaves[child] = value
    return leaves


def _extract_relative_paths_from_update(update):
    """
    Return the set of prefix-relative paths contributed by a single gnmic `updates[]` entry.

    gnmic's JSON shape is not perfectly stable across versions, so read from every place a
    path can appear and let the caller union them:
      - "Path"  : capital-P; what current gnmic (--format json) emits, e.g. "in-octets"
                  or "counters/in-octets" depending on subscription prefix.
      - "path"  : lowercase variant emitted by some forks / older gnmic builds.
      - "values": dict whose keys are paths-relative-to-prefix; this is the form the
                  rest of the repo already consumes (see test_cpu_memory_monitoring.py).
    """
    return set(_extract_relative_values_from_update(update))


def _extract_relative_values_from_update(update):
    """Path-only updates carry value None."""
    if not isinstance(update, dict):
        return {}

    # `values` is the authoritative prefix-relative mapping when present. Falling back to
    # Path/path only when it is absent avoids counting one leaf twice under two spellings
    # (e.g. Path "counters/in-octets" alongside values key "in-octets").
    leaves = {path.strip("/"): value for path, value in _flatten_leaf_values(update.get("values")).items()}
    if not leaves:
        leaves = {str(candidate).strip("/"): None for candidate in (update.get("Path"), update.get("path"))
                  if candidate}
    leaves.pop("", None)
    return leaves


def _extract_leaves_from_update(update):
    """
    Return the set of leaf names contributed by a single gnmic `updates[]` entry.

    Reduces "counters/in-octets" -> "in-octets" so the matcher works regardless of how deep
    the path was reported.
    """
    return {path.rsplit("/", 1)[-1] for path in _extract_relative_paths_from_update(update)}


def validate_notification_has_prefix_and_leaves(notifications, expected_prefix, expected_leaves):
    """
    Assert that a single gNMI Notification carries the expected prefix and contains all
    expected leaves in its `updates` array.

    Why this is enough to prove "same payload / same timestamp":
        In gNMI, `timestamp` and `prefix` are per-Notification fields, and all entries in
        `updates` share that timestamp/prefix by definition. Iterating one Notification at
        a time and only matching when ALL expected leaves appear in that single `updates`
        list guarantees the leaves were emitted together under one common prefix.

    Args:
        notifications:    list of parsed gnmic JSON Notification objects.
        expected_prefix:  prefix to match (matched via endswith to tolerate a leading "/").
        expected_leaves:  iterable of leaf names that must all appear in the same Notification.

    Raises:
        AssertionError: if no single Notification satisfies both the prefix and leaf set.
                        The message includes up to 5 near-misses (right prefix, wrong leaves)
                        to make debugging easy without spamming on unrelated notifications.
    """
    expected_leaves = set(expected_leaves)
    near_misses = []

    for obj in notifications:
        # Skip control frames (sync-response) and any malformed notification missing a timestamp.
        if not _is_data_notification(obj):
            continue

        prefix = obj.get("prefix", "")
        # endswith tolerates an optional leading "/" that some gnmic versions emit.
        if not prefix.strip().lstrip("/").endswith(expected_prefix):
            continue

        leaves = set()
        for update in obj.get("updates", []):
            leaves |= _extract_leaves_from_update(update)

        if expected_leaves.issubset(leaves):
            return

        near_misses.append(f"prefix={prefix}, leaves={sorted(leaves)}")

    raise AssertionError(
        f"Expected one Notification with prefix '{expected_prefix}' containing leaves "
        f"{sorted(expected_leaves)} in the same updates payload, but did not find it. "
        f"near_misses={near_misses[:5]}"
    )


# ---------------------------------------------------------------------------
# Per-leaf streaming continuity (suppress-redundant / heartbeat subscriptions)
# ---------------------------------------------------------------------------


def parse_duration_sec(duration):
    """
    Convert a gnmic duration ('2s', '500ms', '1m30s', or a plain number of seconds) to seconds.

    The heartbeat constants are gnmic duration strings because that is what goes on the command
    line, but the continuity math needs them as numbers.
    """
    if isinstance(duration, (int, float)):
        return float(duration)

    text = str(duration).strip()
    assert text, 'duration cannot be empty'
    units = {'ms': 0.001, 's': 1.0, 'm': 60.0, 'h': 3600.0}
    matches = re.findall(r'(\d+(?:\.\d+)?)(ms|s|m|h)?', text)
    total, matched_any = 0.0, False
    for value, unit in matches:
        if not value:
            continue
        matched_any = True
        total += float(value) * units[unit or 's']
    assert matched_any, f'unparseable duration: {duration!r}'
    return total


def heartbeat_window_sec(heartbeat_interval, epochs=HEARTBEAT_EPOCHS_TO_OBSERVE,
                         slack_sec=HEARTBEAT_WINDOW_SLACK_SEC):
    """
    Seconds a subscription must stream to observe `epochs` heartbeat epochs.

    The slack covers the server registering subscriptions serially, so the last subscriber of a
    concurrent batch only starts its first epoch some seconds after launch.
    """
    return int(parse_duration_sec(heartbeat_interval) * epochs) + slack_sec


def max_heartbeat_deliveries(window_sec, heartbeat_interval):
    """
    Most times a suppressed, unchanging leaf can legitimately arrive within `window_sec`.

    One initial sync plus one resend per heartbeat epoch, rounded up and with an extra epoch of
    slack so boundary timing does not flake. Exceeding this means the samples were never
    suppressed, so the run proves nothing about the heartbeat and must not be read as a pass.
    """
    epochs = math.ceil(window_sec / parse_duration_sec(heartbeat_interval))
    return epochs + 2


def verify_heartbeat_drove_delivery(timeline, window_sec, heartbeat_interval, context,
                                    leaf_filter=None, min_paced_share=1.0):
    """
    Assert the leaves in `timeline` arrived on heartbeat cadence, not on every sample.

    Continuity alone is also satisfied by a server that ignores suppress-redundant and delivers
    every sample, which would hide a broken heartbeat behind sheer volume. `leaf_filter` narrows
    the check to the leaves actually expected to be suppressed when the subscription mixes
    unchanging leaves with ones live traffic keeps moving.

    `min_paced_share` relaxes "every considered leaf" to "at least this share of them", for a
    subtree whose moving leaves cannot be named up front - a firmware-backed subtree exposes a
    different leaf set per platform, and its analog readings do change every sample. Suppression
    being off is a property of the whole subscription, so it is still caught: every leaf would
    then be sample-paced rather than a minority of them.
    """
    ceiling = max_heartbeat_deliveries(window_sec, heartbeat_interval)
    considered = {
        leaf: stamps for leaf, stamps in timeline.items()
        if leaf_filter is None or leaf_filter(leaf)
    }
    assert considered, (
        f'{context}: no unchanging leaves were streamed, so suppress-redundant was never '
        f'exercised and the heartbeat went untested'
    )
    oversupplied = {
        leaf: len(stamps) for leaf, stamps in considered.items() if len(stamps) > ceiling
    }
    paced_share = (len(considered) - len(oversupplied)) / len(considered)
    assert paced_share >= min_paced_share, (
        f'{context}: only {len(considered) - len(oversupplied)}/{len(considered)} leaves '
        f'({paced_share:.0%}, expected at least {min_paced_share:.0%}) stayed within the '
        f'{ceiling} deliveries a {heartbeat_interval} heartbeat allows over {window_sec}s, so '
        f'suppress-redundant did not suppress them and this run says nothing about the heartbeat: '
        f'{dict(list(oversupplied.items())[:5])}'
    )
    logger.info(
        '%s: %d/%d leaves delivered at heartbeat cadence (<=%d in %ds)',
        context, len(considered) - len(oversupplied), len(considered), ceiling, window_sec,
    )


def is_sample_interval_rejected(out, err):
    """True when the gNMI agent rejected the requested sample interval as below its minimum."""
    return any(GnmicErr.SAMPLE_INTERVAL_TOO_LOW in (stream or '') for stream in (out, err))


def build_leaf_value_timeline(notifications, include_deletes=True):
    """
    Map every delivered leaf to its time-ordered ``(timestamp_ns, value)`` deliveries; a gNMI delete has value None.

    Keys are absolute paths (``<prefix>/<relative path>``), so a wildcard subscription yields one
    entry per interface per leaf rather than collapsing them all onto one leaf name.
    """
    timeline = defaultdict(list)
    for obj in notifications:
        if not _is_data_notification(obj):
            continue
        timestamp = obj.get("timestamp")
        prefix = str(obj.get("prefix", "")).strip().strip("/")
        leaf_batches = [_extract_relative_values_from_update(update) for update in obj.get("updates", [])]
        if include_deletes:
            leaf_batches.append({str(path).strip("/"): None for path in obj.get("deletes", [])})
        for leaves in leaf_batches:
            for relative_path, value in leaves.items():
                leaf_path = f"{prefix}/{relative_path}" if prefix else relative_path
                timeline[leaf_path].append((timestamp, value))
    return {leaf: sorted(entries, key=itemgetter(0)) for leaf, entries in timeline.items()}


def build_leaf_timeline(notifications):
    """
    Map every delivered leaf to the sorted list of timestamps it was delivered at.

    This is what makes per-leaf analysis possible: ``count_result_sets`` counts distinct
    timestamps across the whole subscription and therefore cannot see one leaf going silent while
    its neighbours keep streaming.
    """
    return {leaf: [timestamp for timestamp, _ in entries]
            for leaf, entries in build_leaf_value_timeline(notifications, include_deletes=False).items()}


def timeline_end_ns(timeline):
    """Newest timestamp anywhere in the timeline, i.e. the last thing the server sent."""
    return max((timestamps[-1] for timestamps in timeline.values() if timestamps), default=None)


def stream_silence_before_window_end_sec(timeline, window_sec):
    """
    Seconds between the last notification in `timeline` and where its window was expected to end.

    The expected end is the timeline's own first timestamp plus `window_sec`, so the whole
    comparison stays on the server's clock (a DUT/player skew would otherwise read as silence).
    That also means it carries the subscription's registration delay - the first notification
    lands some time after launch, so the estimate runs that much past the real end, and any
    allowance made against this figure has to absorb it.
    """
    first_ns = min((timestamps[0] for timestamps in timeline.values() if timestamps), default=None)
    last_ns = timeline_end_ns(timeline)
    if first_ns is None or last_ns is None:
        return 0.0
    return (first_ns + int(window_sec * NANOSEC_PER_SEC) - last_ns) / NANOSEC_PER_SEC


def find_leaves_missing_from_timeline(timeline, expected_leaf_paths):
    """
    Expected leaves that never appeared, matched by suffix against the timeline keys.

    Callers pass prefix-relative paths (e.g. ``state/counters/in-octets``) because a wildcard
    subscription's absolute keys carry an interface name the caller cannot know up front.
    """
    return sorted(
        leaf for leaf in expected_leaf_paths
        if not any(key == leaf or key.endswith(f"/{leaf.strip('/')}") for key in timeline)
    )


def analyze_leaf_continuity(timeline, max_gap_sec, window_end_ns=None):
    """
    Report every leaf whose delivery cadence broke.

    A leaf breaks cadence when the gap between two consecutive deliveries, or the gap between its
    last delivery and `window_end_ns`, exceeds `max_gap_sec`. The trailing gap is what catches the
    initial-sync-then-silent shape: a leaf delivered once and never again has no
    consecutive-delivery gap at all, so an internal-gap-only check would pass it.

    `window_end_ns` defaults to the newest timestamp anywhere in `timeline`. Comparing against a
    server timestamp rather than the player's clock keeps the result correct when the two clocks
    are skewed.

    Returns ``{leaf path: {"deliveries", "max_gap_sec", "trailing_gap_sec"}}`` for violators only.
    """
    window_end_ns = window_end_ns if window_end_ns is not None else timeline_end_ns(timeline)
    violations = {}
    for leaf, timestamps in timeline.items():
        if not timestamps:
            continue
        gaps_sec = [
            (later - earlier) / NANOSEC_PER_SEC
            for earlier, later in zip(timestamps, timestamps[1:])
        ]
        max_gap_observed = max(gaps_sec, default=0.0)
        trailing_gap = (window_end_ns - timestamps[-1]) / NANOSEC_PER_SEC if window_end_ns else 0.0
        if max(max_gap_observed, trailing_gap) > max_gap_sec:
            violations[leaf] = {
                "deliveries": len(timestamps),
                "max_gap_sec": round(max_gap_observed, 3),
                "trailing_gap_sec": round(trailing_gap, 3),
            }
    return violations


def verify_leaf_heartbeat_continuity(notifications, heartbeat_interval, expected_leaf_paths=None,
                                     context='', tolerance=HEARTBEAT_TOLERANCE_FACTOR,
                                     min_deliveries=2, max_reported=20, window_sec=None,
                                     no_initial_sync=False):
    """
    Assert every leaf kept heartbeating over the collected notifications.

    Three things must hold, per leaf rather than in aggregate:
      1. every expected leaf appeared at all,
      2. each leaf was delivered at least `min_deliveries` times (so a resend actually repeated),
      3. no leaf was silent for longer than `tolerance` x the heartbeat interval.

    Those three are all measured inside the timeline, so they cannot see the subscription stopping
    as a whole: with every leaf's last delivery at the same moment, no leaf trails any other and
    each one's cadence looks intact right up to where the stream died. Pass `window_sec` (the
    window the caller streamed for) to also assert the stream reached the end of it.

    That last check infers where the window began from the first timestamp in the timeline, so an
    updates-only subscription needs `no_initial_sync`: with no sync to open the stream, its first
    delivery is a whole heartbeat late, and the inferred origin - and with it the silence figure -
    is off by that much. Without the flag the check would fail such a subscription for the
    registration delay it is supposed to tolerate.

    `heartbeat_interval` is a gnmic duration ('20s') or a number of seconds. `expected_leaf_paths`
    are prefix-relative paths; omit them when the subscription is a subtree whose exact leaf set
    is not known up front. Returns the built timeline so callers can assert further.
    """
    heartbeat_sec = parse_duration_sec(heartbeat_interval)
    max_gap_sec = heartbeat_sec * tolerance
    timeline = build_leaf_timeline(notifications)
    label = f" for {context}" if context else ''

    assert timeline, (
        f"no gNMI data notifications were collected{label}, so nothing can be concluded about "
        f"heartbeat continuity - the subscription never streamed"
    )

    if expected_leaf_paths:
        missing = find_leaves_missing_from_timeline(timeline, expected_leaf_paths)
        assert not missing, (
            f"{len(missing)} subscribed leaves were never delivered{label}: {missing[:max_reported]}"
        )

    silent_after_sync = sorted(leaf for leaf, stamps in timeline.items() if len(stamps) < min_deliveries)
    violations = analyze_leaf_continuity(timeline, max_gap_sec)
    attach_plain_summary(
        _format_continuity_summary(timeline, heartbeat_sec, max_gap_sec, silent_after_sync,
                                   violations, max_reported),
        f"heartbeat continuity{label}",
    )

    assert not silent_after_sync, (
        f"{len(silent_after_sync)}/{len(timeline)} leaves were delivered fewer than "
        f"{min_deliveries} times{label} - they synced once and then went silent instead of being "
        f"resent every {heartbeat_sec}s: {silent_after_sync[:max_reported]}"
    )
    assert not violations, (
        f"{len(violations)}/{len(timeline)} leaves missed a heartbeat epoch{label} (allowed gap "
        f"{max_gap_sec}s = {tolerance} x {heartbeat_sec}s): "
        f"{dict(list(violations.items())[:max_reported])}"
    )

    if window_sec is not None:
        # Never tighter than the window's own registration slack: the silence figure includes the
        # delay before this subscription's first notification, which is what that slack sizes.
        allowed_silence_sec = max(max_gap_sec, HEARTBEAT_WINDOW_SLACK_SEC)
        if no_initial_sync:
            allowed_silence_sec += heartbeat_sec
        silence_sec = stream_silence_before_window_end_sec(timeline, window_sec)
        assert silence_sec <= allowed_silence_sec, (
            f"the subscription{label} delivered its last notification {round(silence_sec, 1)}s "
            f"before the end of its {window_sec}s window (allowed {allowed_silence_sec}s), so "
            f"every leaf stopped together part-way through - the per-leaf checks above cannot see "
            f"that, because a leaf is only late relative to the others"
        )
    return timeline


def verify_leaves_not_resent(notifications, expected_leaf_paths=None, context='', max_reported=20):
    """
    Assert every leaf was delivered exactly once, i.e. suppress-redundant really suppressed.

    The negative control for the heartbeat tests: with suppression on and no heartbeat, an
    unchanged leaf must not be resent. Only meaningful for leaves that genuinely do not change
    (see HEARTBEAT_STATIC_INTERFACE_LEAVES) - a live counter changes every sample and is
    correctly resent.
    """
    timeline = build_leaf_timeline(notifications)
    label = f" for {context}" if context else ''

    assert timeline, (
        f"no gNMI data notifications were collected{label}, so suppression cannot be "
        f"distinguished from a subscription that never streamed"
    )
    if expected_leaf_paths:
        missing = find_leaves_missing_from_timeline(timeline, expected_leaf_paths)
        assert not missing, (
            f"{len(missing)} subscribed leaves were never delivered{label}, so their initial sync "
            f"is missing: {missing[:max_reported]}"
        )

    resent = {leaf: len(stamps) for leaf, stamps in timeline.items() if len(stamps) > 1}
    assert not resent, (
        f"{len(resent)}/{len(timeline)} unchanged leaves were resent{label} even though "
        f"suppress-redundant was set and no heartbeat was requested: "
        f"{dict(list(resent.items())[:max_reported])}"
    )
    return timeline


def _format_continuity_summary(timeline, heartbeat_sec, max_gap_sec, silent_after_sync, violations,
                               max_reported):
    """Human-readable per-leaf continuity summary for the Allure attachment."""
    delivery_counts = [len(stamps) for stamps in timeline.values()]
    lines = [
        f"leaves observed: {len(timeline)}",
        f"heartbeat: {heartbeat_sec}s (allowed gap {max_gap_sec}s)",
        f"deliveries per leaf: min={min(delivery_counts, default=0)}, "
        f"max={max(delivery_counts, default=0)}",
        f"leaves silent after initial sync: {len(silent_after_sync)}",
        f"leaves that missed a heartbeat epoch: {len(violations)}",
    ]
    if silent_after_sync:
        lines.append(f"silent leaves (first {max_reported}): {silent_after_sync[:max_reported]}")
    for leaf, detail in list(violations.items())[:max_reported]:
        lines.append(f"  {leaf}: {detail}")
    return "\n".join(lines)


def change_port_description_and_validate_gnmi_updates(engines, port_description, target_ip, mode='', username='',
                                                      password='', timeout_sec=4):
    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).returned_value
    selected_port.interface.set(NvosConst.DESCRIPTION, port_description, apply=True).verify_result()
    selected_port.update_output_dictionary()
    verify_description_value(selected_port.show_output_dictionary, port_description)

    devices = TestToolkit.devices

    xpath = f'interfaces/interface[name={selected_port.name}]/state/description'
    logger.info(f"sleep {GnmiConsts.SLEEP_TIME_FOR_UPDATE} sec until we start validate the gnmi stream")
    time.sleep(GnmiConsts.SLEEP_TIME_FOR_UPDATE)
    gnmi_stream_updates = run_gnmi_client_and_parse_output(engines, devices, xpath, target_ip, mode=mode,
                                                           username=username, password=password,
                                                           timeout_sec=timeout_sec)
    assert port_description in list(
        gnmi_stream_updates.values()), "we expect to see the new port description in the gnmi-client output but we didn't.\n" \
                                       f"port description: {port_description}\n" \
                                       f"but got: {list(gnmi_stream_updates.values())}"


@retry(Exception, tries=3, delay=3)
def validate_gnmi_server_in_health_issues(system, expected_gnmi_health_issue):
    health_output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
    health_issues = health_output.get(HealthConsts.ISSUES, {})
    issue_name = GnmiConsts.GNMI_DOCKER
    actual_present = issue_name in health_issues

    assert actual_present == expected_gnmi_health_issue, (
        f"Health issue presence mismatch for '{issue_name}': "
        f"expected={expected_gnmi_health_issue}, actual={actual_present}, "
        f"available_issues={list(health_issues.keys())}"
    )


def create_gnmi_and_redis_cmd_dict(redis_cmd_db_num, redis_cmd_table, redis_cmd_key, xpath_gnmi_cmd,
                                   comparison_dict=None):
    gnmi_cmd_dict = {GnmiConsts.REDIS_CMD_DB_NAME: DatabaseConst.REDIS_DB_NUM_TO_NAME[redis_cmd_db_num],
                     GnmiConsts.REDIS_CMD_TABLE_NAME: redis_cmd_table, GnmiConsts.REDIS_CMD_PARAM: redis_cmd_key,
                     GnmiConsts.XPATH_KEY: xpath_gnmi_cmd, GnmiConsts.COMPARISON_KEY: comparison_dict}
    return gnmi_cmd_dict


def get_infiniband_name_from_port_name(engine, port_name):
    output = Tools.DatabaseTool.sonic_db_cli_hget(engine=engine, asic="", db_name=DatabaseConst.APPL_DB_NAME,
                                                  db_config=f"\"ALIAS_PORT_MAP:{port_name}\"", param="name")
    # output = engine.run_cmd(f"redis-cli -n 0 HGET \"ALIAS_PORT_MAP:{port_name}\" \"name\"")
    infiniband_name = output.replace("\"", "")
    return infiniband_name


def get_port_oid_from_infiniband_port(engine, infiniband_port):
    output = Tools.DatabaseTool.sonic_db_cli_hget(engine=engine, asic="", db_name=DatabaseConst.COUNTERS_DB_NAME,
                                                  db_config="COUNTERS_PORT_NAME_MAP", param=str(infiniband_port))
    # output = engine.run_cmd(f"redis-cli -n 2 HGET \"COUNTERS_PORT_NAME_MAP\" \"{infiniband_port}\"")
    port_oid = output.replace("\"", "")
    return port_oid


def create_interface_state_commands_list(port_name, infiniband_name):
    state_xpath = "interfaces/interface[name={port_name}]/state/{field}"
    gnmi_list = [create_gnmi_and_redis_cmd_dict(4, f"IB_PORT|{infiniband_name}", "admin_status",
                                                state_xpath.format(port_name=port_name, field="admin-status")),
                 create_gnmi_and_redis_cmd_dict(4, f"IB_PORT|{infiniband_name}", "index",
                                                state_xpath.format(port_name=port_name, field="ifindex")),
                 create_gnmi_and_redis_cmd_dict(4, f"IB_PORT|{infiniband_name}", "description",
                                                state_xpath.format(port_name=port_name, field="description")),
                 create_gnmi_and_redis_cmd_dict(4, f"IB_PORT|{infiniband_name}", "admin_status",
                                                state_xpath.format(port_name=port_name, field="enabled"),
                                                comparison_dict={"up": "true", "down": "false"})]
    return gnmi_list


def create_platform_general_commands_list():
    usage_name = "USAGE"
    state_xpath = "platform-general/state/{field}"
    gnmi_list = [create_gnmi_and_redis_cmd_dict(6, f"DISK_INFO|{usage_name}", "disk_total_size",
                                                state_xpath.format(field="disk-total-size")),
                 create_gnmi_and_redis_cmd_dict(6, f"DISK_INFO|{usage_name}", "disk_usage",
                                                state_xpath.format(field="disk-used")),
                 create_gnmi_and_redis_cmd_dict(6, f"RAM_INFO|{usage_name}", "memory_total_size",
                                                state_xpath.format(field="memory-total-size")),
                 create_gnmi_and_redis_cmd_dict(6, f"RAM_INFO|{usage_name}", "memory_usage",
                                                state_xpath.format(field="memory-used")), ]
    return gnmi_list


def create_gnmi_counter_list(port_name, port_oid):
    state_xpath = "interfaces/interface[name={port_name}]/state/counters/{field}"
    gnmi_list = [create_gnmi_and_redis_cmd_dict(2, f"COUNTERS:{port_oid}", "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT",
                                                state_xpath.format(port_name=port_name, field="in-pkts")),
                 create_gnmi_and_redis_cmd_dict(2, f"COUNTERS:{port_oid}", "SAI_PORT_STAT_INFINIBAND_IF_OUT_PKTS_EXT",
                                                state_xpath.format(port_name=port_name, field="out-pkts")),
                 create_gnmi_and_redis_cmd_dict(2, f"COUNTERS:{port_oid}", "SAI_PORT_STAT_INFINIBAND_PC_ERR_RCV_F",
                                                state_xpath.format(port_name=port_name, field="in-errors")),
                 create_gnmi_and_redis_cmd_dict(2, f"COUNTERS:{port_oid}", "SAI_PORT_STAT_INFINIBAND_ERR_XMTCONSTR_F",
                                                state_xpath.format(port_name=port_name, field="out-errors")),
                 create_gnmi_and_redis_cmd_dict(2, f"COUNTERS:{port_oid}", "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT",
                                                state_xpath.format(port_name=port_name, field="in-octets")),
                 create_gnmi_and_redis_cmd_dict(2, f"COUNTERS:{port_oid}", "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT",
                                                state_xpath.format(port_name=port_name, field="out-octets")),
                 create_gnmi_and_redis_cmd_dict(2, f"COUNTERS:{port_oid}", "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT",
                                                state_xpath.format(port_name=port_name, field="in-pkts")),
                 create_gnmi_and_redis_cmd_dict(2, f"COUNTERS:{port_oid}", "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT",
                                                state_xpath.format(port_name=port_name, field="in-pkts")),
                 create_gnmi_and_redis_cmd_dict(2, f"COUNTERS:{port_oid}", "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT",
                                                state_xpath.format(port_name=port_name, field="in-pkts"))]
    return gnmi_list


def create_gnmi_infiniband_list(port_name, port_oid, infiniband_name):
    state_xpath = "interfaces/interface[name={port_name}]/infiniband/state/{field}"
    gnmi_list = [create_gnmi_and_redis_cmd_dict(2, f"COUNTERS:{port_oid}", "SAI_PORT_STAT_INFINIBAND_LOGICAL_STATE",
                                                state_xpath.format(port_name=port_name, field="logical-port-state"),
                                                comparison_dict={"1": "Down", "2": "Initialize", "3": "Armed",
                                                                 "4": "Active"}),
                 create_gnmi_and_redis_cmd_dict(2, f"COUNTERS:{port_oid}", "SAI_PORT_STAT_INFINIBAND_PHYSICAL_STATE",
                                                state_xpath.format(port_name=port_name, field="physical-port-state"),
                                                comparison_dict={"1": "Sleep", "2": "Polling", "3": "Disabled",
                                                                 "4": "PortConfigurationTraining", "5": "LINK_UP",
                                                                 "6": "LinkErrorRecovery", "7": "Phy Test",
                                                                 "8": "Disabled By Chassis Manager"}),
                 create_gnmi_and_redis_cmd_dict(6, f"IB_PORT_TABLE|{infiniband_name}", "speed_admin",
                                                state_xpath.format(port_name=port_name, field="supported-ib-speeds")),
                 create_gnmi_and_redis_cmd_dict(2, f"COUNTERS:{port_oid}", "SAI_PORT_STAT_INFINIBAND_SPEED_OPER",
                                                state_xpath.format(port_name=port_name, field="speed")),
                 create_gnmi_and_redis_cmd_dict(4, f"IB_PORT|{infiniband_name}", "auto_neg",
                                                state_xpath.format(port_name=port_name, field="speed-negotiate"),
                                                comparison_dict={'on': 'true', 'off': 'false'}),
                 create_gnmi_and_redis_cmd_dict(6, f"IB_PORT_TABLE|{infiniband_name}", "lanes_admin",
                                                state_xpath.format(port_name=port_name, field="supported-widths"),
                                                comparison_dict={"1": "1X", "2": "2X", "3": "1X_2X", "4": "4X",
                                                                 "5": "1X_4X", "6": "2X_4X", "7": "1X_2X_4X"}),
                 create_gnmi_and_redis_cmd_dict(6, f"IB_PORT_TABLE|{infiniband_name}", "mtu_max",
                                                state_xpath.format(port_name=port_name, field="max-supported-MTUs")),
                 create_gnmi_and_redis_cmd_dict(2, f"COUNTERS:{port_oid}", "SAI_PORT_STAT_INFINIBAND_MTU_OPER",
                                                state_xpath.format(port_name=port_name, field="mtu")),
                 create_gnmi_and_redis_cmd_dict(6, f"IB_PORT_TABLE|{infiniband_name}", "ib_subnet",
                                                state_xpath.format(port_name=port_name, field="ib-Subnet"),
                                                comparison_dict={"0": "infiniband-default", "1": "infiniband-1"}),
                 create_gnmi_and_redis_cmd_dict(6, f"IB_PORT_TABLE|{infiniband_name}", "vl_admin",
                                                state_xpath.format(port_name=port_name, field="vl-capabilities"),
                                                comparison_dict={"1": "VL0", "2": "VL0-VL1", "3": "VL0-VL2",
                                                                 "4": "VL0-VL3", "5": "VL0-VL4", "6": "VL0-VL5",
                                                                 "7": "VL0-VL6", "8": "VL0-VL7", "15": "VL0-VL14"})]
    return gnmi_list


@retry(AssertionError, tries=3, delay=10)
def validate_redis_cli_and_gnmi_commands_results(engines, devices, gnmi_list, allowed_range_in_bytes=None):
    client = GnmiClient(engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT,
                        devices.dut.default_username, devices.dut.default_password,
                        verify_tools_installed=True)
    for command in gnmi_list:
        prefix_and_path = command[GnmiConsts.XPATH_KEY].rsplit("/", 1)
        gnmi_client_output, gnmi_client_err, _, _ = client.gnmic_subscribe(prefix=prefix_and_path[0],
                                                                           path=prefix_and_path[1],
                                                                           mode='once', flat=True,
                                                                           skip_cert_verify=True)
        verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, gnmi_client_output, gnmi_client_err)
        gnmi_client_output = re.split(r"received\s+signal.*", gnmi_client_output, flags=re.IGNORECASE)[0]
        gnmi_client_output = re.sub(r'(\\["\\n]+|\s+)', '', gnmi_client_output.split(":")[-1])
        redis_output = Tools.DatabaseTool.sonic_db_cli_hget(engine=engines.dut, asic="",
                                                            db_name=command[GnmiConsts.REDIS_CMD_DB_NAME],
                                                            db_config=command[GnmiConsts.REDIS_CMD_TABLE_NAME],
                                                            param=command[GnmiConsts.REDIS_CMD_PARAM])
        if ',' in redis_output:
            redis_output = str(sorted(redis_output.split(',')))
            gnmi_client_output = str(sorted(gnmi_client_output.split(',')))
        if command[GnmiConsts.COMPARISON_KEY]:
            Tools.ValidationTool.compare_values(gnmi_client_output.lower(), command[GnmiConsts.COMPARISON_KEY][
                redis_output].lower()).verify_result()
        elif allowed_range_in_bytes is not None:
            # gNMI serializes uint64 byte-count leaves (disk-total-size, memory-used, ...)
            # in scientific notation (e.g. '1.55994918912e+11') for values >= ~1e11.
            # Product bug rejected as WontFix in Redmine #5073631, so accept float form here.
            result = abs(int(float(gnmi_client_output)) - int(redis_output))
            assert 0 <= result <= allowed_range_in_bytes, (
                f"gNMI output: {gnmi_client_output} is not within {allowed_range_in_bytes} to "
                f"redis output:{redis_output} for field: {prefix_and_path[1]}")
        else:
            Tools.ValidationTool.compare_values(gnmi_client_output.lower(), redis_output.lower()).verify_result()


def verify_description_value(output, expected_description):
    Tools.ValidationTool.verify_field_value_in_output(output, NvosConst.DESCRIPTION,
                                                      expected_description).verify_result()


def change_interface_description(selected_port, new_description: str = ''):
    rand_str = new_description or ''.join(random.choice(string.ascii_lowercase) for _ in range(20))
    selected_port.interface.set(NvosConst.DESCRIPTION, rand_str, apply=True).verify_result()
    wait_for_gnmi_to_update_data()
    return rand_str


def load_certificate_into_gnmi(engine: LinuxSshEngine, cert: CertInfo):
    with allure.step('make dedicated dir in switch'):
        engine.run_cmd(f'mkdir -p {DUT_GNMI_CERTS_DIR}')
    with allure.step('scp cert to switch'):
        with allure.step(f'copy {cert.private_filename}'):
            scp_file(engine, f'{cert.private}', f'{DUT_GNMI_CERTS_DIR}/{cert.private_filename}')
        with allure.step(f'copy {cert.public_filename}'):
            scp_file(engine, f'{cert.public}', f'{DUT_GNMI_CERTS_DIR}/{cert.public_filename}')
    with allure.step('copy cert into gnmi docker'):
        engine.run_cmd(
            f'docker cp {DUT_GNMI_CERTS_DIR}/{cert.private_filename} {GnmiConsts.GNMI_DOCKER}:{DOCKER_CERTS_DIR}/{cert.private_filename}')
        engine.run_cmd(
            f'docker cp {DUT_GNMI_CERTS_DIR}/{cert.public_filename} {GnmiConsts.GNMI_DOCKER}:{DOCKER_CERTS_DIR}/{cert.public_filename}')
    with allure.step('restart gnmi'):
        system = System()
        system.gnmi_server.disable_gnmi_server()
        system.gnmi_server.enable_gnmi_server()


def verify_msg_existence_in_out_or_err(msg: str, should_be_in: bool, out: str, err: str = None):
    msg_in_out = msg in out
    msg_in_err = msg in err if err else False
    assert (msg_in_out or msg_in_err) == should_be_in, ((f'"{msg}" unexpectedly was{" not" if should_be_in else ""} '
                                                         f'found in out{"/err" if err is not None else ""}.\nout: {out}') + (
        f'\nerr: {err}' if err is not None else ''))


def verify_msg_not_in_out_or_err(msg: str, out: str, err: str = None):
    verify_msg_existence_in_out_or_err(msg, False, out, err)


def verify_msg_in_out_or_err(msg: str, out: str, err: str = None):
    verify_msg_existence_in_out_or_err(msg, True, out, err)


def parse_gnmi_status(output):
    """
    Parse JSON output from 'nv show system gnmi-server status' (and variants).
    Returns a dict with GnmiServerStatus keys: total-active-subscriptions,
    received-subscription-requests, rejected-subscriptions, received-capabilities-requests, client (list).
    If the CLI returns a nested structure (e.g. {"status": {...}}), unwraps to the inner status dict.
    """
    result = OutputParsingTool.parse_json_str_to_dictionary(output)
    d = result.get_returned_value()
    if not isinstance(d, dict):
        return d
    # Unwrap if single top-level key contains the status (e.g. {"status": {...}})
    if len(d) == 1:
        inner = next(iter(d.values()))
        if isinstance(inner, dict) and (GnmiServerStatus.TOTAL_ACTIVE_SUBSCRIPTIONS in inner or GnmiServerStatus.CLIENT in inner):
            return inner
    return d


def get_gnmi_status_clients(status_dict):
    """
    Return a list of per-client status dicts from parsed gnmi-server status output.

    The ``client`` field may be absent, a list, or a dict keyed by client id (as returned
    by ``nv show system gnmi-server status`` when multiple clients are connected).
    """
    clients = status_dict.get(GnmiServerStatus.CLIENT)
    if clients is None:
        return []
    if isinstance(clients, dict):
        if not clients:
            return []
        return list(clients.values())
    if isinstance(clients, list):
        return clients
    return [clients]


def verify_gnmi_client(test_flow, server_host, server_port, username, password, skip_cert_verify: bool,
                       err_msg_to_check: str, port_to_change=None, cacert='', new_port_description_to_check=None,
                       client_cmd_time=None, debug_mode: bool = True):
    assert cacert or skip_cert_verify, 'given cacert can not be empty when skip_cert_verify is False'

    log_msg = (f'verify gnmi client with{"" if skip_cert_verify else "out"} skip-verify '
               f'and credentials: {username} / {password}')

    if port_to_change:
        selected_port = port_to_change
    else:
        with allure.step('randomize port to change description'):
            selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).returned_value

    if port_to_change and new_port_description_to_check:
        new_description = new_port_description_to_check
    else:
        with allure.step(f'change description of interface: "{selected_port.name}"'):
            new_description = change_interface_description(selected_port)

    with allure.step('create gnmi client'):
        client = GnmiClient(server_host, server_port, username, password, cacert=cacert, cmd_time=client_cmd_time or 15)
    if test_flow == TestFlowType.GOOD_FLOW:
        with allure.step(f'good-flow: {log_msg}'):
            with allure.step('verify using capabilities command'):
                out, err = client.gnmic_capabilities(skip_cert_verify=skip_cert_verify, debug_mode=debug_mode)
                verify_msg_not_in_out_or_err(err_msg_to_check, out, err)
            with allure.step('verify using subscribe command'):
                out, err = client.gnmic_subscribe_interface(GnmiMode.ONCE, selected_port.name,
                                                            skip_cert_verify=skip_cert_verify, debug_mode=debug_mode)
                verify_msg_in_out_or_err(new_description, out)
                verify_msg_not_in_out_or_err(err_msg_to_check, out, err)
            with allure.step('verify using reflection command'):
                services = [SERVER_REFLECTION_SUBSCRIBE_RESPONSE]
                verify_server_reflection(test_flow, client, skip_cert_verify, err_msg_to_check, services)
    else:
        with allure.step(f'bad-flow: {log_msg}'):
            with allure.step('verify using capabilities command'):
                out, err = client.gnmic_capabilities(skip_cert_verify=skip_cert_verify, debug_mode=debug_mode)
                verify_msg_in_out_or_err(err_msg_to_check, out, err)
            with allure.step('verify using subscribe command'):
                out, err = client.gnmic_subscribe_interface(GnmiMode.ONCE, selected_port.name,
                                                            skip_cert_verify=skip_cert_verify, debug_mode=debug_mode)
                verify_msg_not_in_out_or_err(new_description, out)
                verify_msg_in_out_or_err(err_msg_to_check, out, err)
            with allure.step('verify using reflection command'):
                verify_server_reflection(test_flow, client, skip_cert_verify, err_msg_to_check)


def run_gnmi_client_and_verify(addr: str, user: UserInfo, expect_success: bool, run_insecure: bool,
                               client_cacert: CertInfo = None, client_cert: CertInfo = None, timeout=None):
    # with allure.step('randomize port to change description'):
    #     selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).get_returned_value()
    with allure.step('build gnmic cmd'):
        mode = ''
        # gnmic = GnmicCmdBuilder(addr).user_creds(user.username, user.password).subscribe_interface_description(selected_port.name, mode).debug()
        gnmic = GnmicCmdBuilder(addr).user_creds(user.username, user.password).capabilities()
        if run_insecure:
            gnmic.skip_verify()
        if client_cert:
            gnmic.cert(client_cert.private, client_cert.public)
        if client_cacert:
            gnmic.ca(client_cacert.cacert)

    run_cmd_and_verify(gnmic.build(), client_cacert, client_cert, expect_success, timeout)


def build_gnmic_cmd_and_verify(host: str, creds: Optional[UserInfo], expect_success: bool, client_ca: Optional[CertInfo] = None, insecured: bool = False, client_cert: Optional[CertInfo] = None, revision_num: Optional[int] = None):

    with allure.step('build and verify gnmic client'):
        gnmic = GnmicCmdBuilder(host).capabilities()
        if creds:
            gnmic.user_creds(creds.username, creds.password)
        else:
            # WA gnmic does not support not providing user and password
            gnmic.user_creds('', '')
        if insecured:
            gnmic.skip_verify()
        if client_cert:
            gnmic.cert(client_cert.private, client_cert.public)
        if client_ca:
            gnmic.ca(client_ca.cacert)

        run_cmd_and_verify(gnmic.build(), client_ca, client_cert, expect_success)


def run_cmd_and_verify(cmd: str, client_cacert: CertInfo, client_cert: CertInfo, expect_success: bool, timeout=None) -> str:
    exc = ''
    gnmic_success = True
    output = ''
    try:
        if timeout:
            output = run_cmd(cmd, validate=True, timeout=timeout)
        else:
            output = run_cmd(cmd, validate=True)
        errs = GnmicErr.ALL_ERRS
        found_errs = [err for err in errs if err in output]
        assert not found_errs, f'gnmic got errors: {found_errs}'
    except Exception as e:
        gnmic_success = False
        exc = e
    assert gnmic_success == expect_success, (
        f'gnmic {"fail but expected success" if expect_success else "success but expected fail"}\n'
        f'client cert: {client_cert.name if client_cert else ""}\n'
        f'client ca: {client_cacert.cacert_name if client_cacert else ""}\n'
        f'out: {output}\n'
        f'exception: {exc}')
    return output


def verify_server_reflection(test_flow, client, skip_cert_verify, err_msg_to_check, services=None):
    out_reflect, err_reflect = client.grpcurl_describe(skip_cert_verify=skip_cert_verify)
    if test_flow == TestFlowType.GOOD_FLOW:
        verify_msg_in_out_or_err(GrpcMsg.MSG_SERVER_REFLECT, out_reflect)
        verify_msg_not_in_out_or_err(err_msg_to_check, out_reflect, err_reflect)
        for service in services:
            out_reflect, err_reflect = client.grpcurl_describe(service=service, skip_cert_verify=skip_cert_verify)
            verify_msg_in_out_or_err(GrpcMsg.ALL_MSGS[service], out_reflect)
            verify_msg_not_in_out_or_err(err_msg_to_check, out_reflect, err_reflect)
    else:
        verify_msg_not_in_out_or_err(GrpcMsg.MSG_SERVER_REFLECT, out_reflect)
        verify_msg_in_out_or_err(err_msg_to_check, out_reflect, err_reflect)


def get_scp_player(engines) -> LinuxSshEngine:
    return LinuxSshEngine('fit70', 'root', '3tango')
    return engines.sonic_mgmt
    # return LinuxSshEngine(ip='10.237.116.70', username='root', password='12345')
    # return LinuxSshEngine(ip='10.237.116.84', username='root', password='12345')
    # return LinuxSshEngine(ip='10.237.38.124', username='root', password='12345')
    # return LinuxSshEngine(ip='10.237.38.139', username='root', password='12345')


def verify_gnmi_client_tools_installed(verify_player=True):
    if verify_player:
        player = GnmiClient('', '', '', '', 10)
        with allure.step('verify gnmic installation on test player'):
            player.verify_gnmic_installation()
        with allure.step('verify grpcurl  installation on test player'):
            player.verify_grpcurl_installation()


def setup_gnmi_cert_checker(engines=None):
    engines = engines or TestToolkit.engines
    scp_player = get_scp_player(engines)
    dut_hostname = get_dut_hostname(engines)

    with allure.step('verify player has gnmi client tools'):
        verify_gnmi_client_tools_installed()
    with allure.step('prepare certs'):
        tmp_certs_dir, gnmi_certs = setup_gnmi_cert_tests(engines, dut_hostname, scp_player)
        cert = gnmi_certs[0]
    with allure.step(f'set certificate "{cert.name}" to gnmi'):
        System().gnmi_server.set(CERTIFICATE, cert.name, apply=True).verify_result()
    with allure.step('save config'):
        NvueGeneralCli.save_config(engines.dut)

    return tmp_certs_dir, gnmi_certs


def get_timestamp_of_first_gnmi_response(user: UserInfo, cert: CertInfo):
    client = GnmiClient(cert.dn or cert.ip, GnmiConsts.GNMI_DEFAULT_PORT, user.username, user.password,
                        cacert=cert.cacert)
    output = GnmicErr.CERT_VERIFY_FAIL
    while any(err_msg in output for err_msg in [GnmicErr.CERT_VERIFY_FAIL, 'Failed', 'failed']):
        time.sleep(0.5)
        out, err = client.grpcurl_describe(skip_cert_verify=False)
        output = out + err
    return time.time()


def get_timestamp_of_first_gnmi_response2(user: UserInfo, cert: CertInfo):
    current_file_path = os.path.realpath(__file__)
    current_dir = os.path.dirname(current_file_path)
    script_path = os.path.join(current_dir, 'grpcurl_in_loop.sh')
    script_path_absolute_path = os.path.abspath(script_path)

    cmd_runner = CmdRunner()
    cmd_runner.run_cmd_in_process(f'bash {script_path_absolute_path} {cert.cacert} {user.username} {user.password}')

    return time.time()


def setup_gnmi_cert_tests(engines, dut_hostname, scp_player, dut_ip=None, import_to_dut=True, import_cas=False) -> Tuple[str, List[CertInfo]]:
    return setup_certs_for_tests('gnmi', ['gnmi-cert1', 'gnmi-cert2', 'gnmi-cert3'], engines,
                                 dut_hostname, import_to_dut, scp_player, dut_ip, import_cas)


def cleanup_gnmi_cert_tests(tmp_certs_dir: str, certs: List[CertInfo], cas: List[CertInfo] = None):
    with allure.step('unset gnmi config'):
        gnmi = System().gnmi_server
        gnmi.unset(apply=True).verify_result()
    with allure.step('remove certs from dut and local'):
        cleanup_certs_for_tests(tmp_certs_dir, certs, cas)


def setup_gnmi_mtls_checker(engines=None):
    engines = engines or TestToolkit.engines
    scp_player = get_scp_player(engines)
    dut_hostname = get_dut_hostname(engines)
    system = System()

    with allure.step('verify player has gnmi client tools'):
        verify_gnmi_client_tools_installed()
    with allure.step('prepare certs'):
        tmp_certs_dir, certs = setup_gnmi_cert_tests(engines, dut_hostname, scp_player, None, False)
        server_cert: CertInfo = certs[0]
        server_ca: CertInfo = certs[1]
    with allure.step('import server ca/certs'):
        import_certificates(scp_player, engines.dut, [server_cert])
        import_certificates(scp_player, engines.dut, [server_ca], True)
    with allure.step(f'set cert: {server_cert.name}'):
        system.gnmi_server.set(CERTIFICATE, server_cert.name).verify_result()
    with allure.step(f'set ca: {server_ca.cacert_name}'):
        system.gnmi_server.mtls.set(CA_CERTIFICATE, server_ca.cacert_name, apply=True).verify_result()
    with allure.step('save config'):
        NvueGeneralCli.save_config(engines.dut)

    return tmp_certs_dir, server_cert, server_ca


def wait_for_gnmi_to_update_data():
    with allure.step(f'wait {GnmiConsts.SLEEP_TIME_FOR_UPDATE} seconds for gnmi to update with new data'):
        time.sleep(GnmiConsts.SLEEP_TIME_FOR_UPDATE)


def parse_gnmi_output(gnmi_out):
    """
    This differs from run_gnmi_client_and_parse_output - works also when path=''

    GNMI-output as an example:
    interfaces/interface[name=acp1]/phy-diag/state/raw-ber: 3E-10
    interfaces/interface[name=acp1]/phy-diag/state/eth-an-fsm-state: 0
    interfaces/interface[name=acp1]/phy-diag/state/last-logic-recovery-attempts: 0
    interfaces/interface[name=acp1]/phy-diag/state/psi-fsm-state: IDLE
    interfaces/interface[name=acp1]/phy-diag/state/successful-recovery-events: 0

    for the example, this function will return:
    {
        'raw-ber': '3E-10'
        'eth-an-fsm-state': '0'
        'last-logic-recovery-attempts': '0'
        'psi-fsm-state': 'IDLE'
        'successful-recovery-events': '0'
    }
    """
    res = {}
    try:
        gnmi_out_as_list = [line for line in gnmi_out.splitlines() if line.strip()]
        for line in gnmi_out_as_list:
            key_value_pair = line.split(": ")
            key = key_value_pair[0].split("/")[-1]
            value = key_value_pair[1]
            res[key] = value
        return res
    except Exception as e:
        logger.info("Got an exception while trying to parse GNMI output")
        logger.info(e)
        raise e


def is_gnmi_unavailable(err: str) -> bool:
    """True when the DUT gNMI server is down/unreachable (skip the test, do not false-pass)."""
    e = (err or "").lower()
    return (
        "connection refused" in e or
        "transport: error while dialing" in e or
        ("rpc error" in e and "unavailable" in e)
    )


def is_gnmi_failure(err):
    """True if the given text indicates a failed gNMI request (e.g. rate limit, rpc error)."""
    return bool(err) and any(m in err for m in GnmicErr.ALL_ERRS)


def is_path_not_modelled(text):
    """
    True when the given text is the agent rejecting a path as absent from its YANG model.

    Narrower than `is_gnmi_failure` on purpose: this is the only failure that means "this DUT does
    not have this path", as opposed to "this request did not get through".
    """
    return any(marker in (text or '')
               for marker in (GnmicErr.UNIMPLEMENTED, GnmicErr.PATH_NOT_IN_YANG))


def is_gnmi_rate_limit_error(err):
    """True if the given text indicates a rate-limit (local_rate_limited) error."""
    return bool(err) and GnmicErr.LOCAL_RATE_LIMITED in err


def is_gnmi_overload_error(err):
    """
    True if the given text indicates an expected overload/back-pressure error (rate limit, deadline
    exceeded, ...). These are transient while the server is under load and should be retried,
    not treated as hard failures. Backed by GnmicErr.OVERLOAD_ERRS, so newly recognized overload
    symptoms are picked up automatically without touching this function.
    """
    return bool(err) and any(marker in err for marker in GnmicErr.OVERLOAD_ERRS)


# gnmic normally writes failures to stderr, but some builds/paths surface the failure line on
# stdout. The pair-aware helpers below scan both streams so single-call classification matches the
# flood loop (which inspects merged 2>&1 output) and never miscounts a failed response as success.
def gnmi_response_failed(out, err):
    """True if either stdout or stderr shows a failed gNMI request."""
    return is_gnmi_failure(err) or is_gnmi_failure(out)


def gnmi_response_rate_limited(out, err):
    """True if either stdout or stderr shows a local_rate_limited error."""
    return is_gnmi_rate_limit_error(err) or is_gnmi_rate_limit_error(out)


def gnmi_response_overloaded(out, err):
    """True if either stdout or stderr shows an expected overload error (rate limit / deadline)."""
    return is_gnmi_overload_error(err) or is_gnmi_overload_error(out)


def attach_rate_limit_result(
    total_success, total_fail, sample_error, duration_sec, step_name, rate_limit_failures=None
):
    """Attach ramp summary (request rate, counts, rate-limit vs other failures, sample error) to Allure."""
    total = total_success + total_fail
    effective_rpm = total * (60.0 / duration_sec) if duration_sec else total
    summary = (
        f"Request rate: ~{effective_rpm:.1f} req/min\n"
        f"Total: {total} (success={total_success}, failed={total_fail})\n"
    )
    if rate_limit_failures is not None:
        other_failures = total_fail - rate_limit_failures
        summary += f"Of failures: rate_limit={rate_limit_failures}, other (e.g. timeout/network)={other_failures}\n"
    if sample_error:
        summary += f"Sample error:\n{sample_error}\n"
    # Project wrapper signature is attach(title, msg, ...): title first, body second.
    allure.attach(
        f"{step_name}: requests and output",
        summary,
        allure.orig_allure.attachment_type.TEXT,
    )


def attach_plain_summary(body: str, title: str):
    # Project wrapper signature is attach(title, msg, ...): title first, body second.
    allure.attach(title, body, allure.orig_allure.attachment_type.TEXT)


def capabilities_until_success_after_restart(client, step_label: str):
    """
    After gNMI comes back, attackers may have saturated it; retry capabilities while the only
    failure is an expected overload error (rate limit, deadline exceeded, ...) so the check
    validates reachability, not transient back-pressure. Any other failure is treated as real and
    fails immediately.

    Keeps retrying for up to RECONNECT_CAPABILITIES_MAX_WAIT_SEC (the per-minute rate-limit window),
    since the limiter can take up to a full minute for the overload errors to disappear once load
    drops.
    """
    with allure.step(
        f"{step_label}: Capabilities until success "
        f"(gnmic retries up to {RECONNECT_CAPABILITIES_MAX_WAIT_SEC}s)"
    ):
        last_err = ""
        attempt = 0
        deadline = time.time() + RECONNECT_CAPABILITIES_MAX_WAIT_SEC
        while True:
            attempt += 1
            out, err = client.gnmic_capabilities(skip_cert_verify=True, cmd_time=PER_REQUEST_TIMEOUT_SEC)
            if not gnmi_response_failed(out, err):
                if attempt > 1:
                    attach_plain_summary(
                        f"{step_label}: succeeded on attempt {attempt} after prior errors.",
                        "Reconnect capabilities retries",
                    )
                return
            last_err = err if is_gnmi_failure(err) else (out or err or "")
            # Keep waiting only for expected overload errors to clear, and only until the deadline;
            # any other failure (or a timeout of the wait window) is treated as a real failure.
            if gnmi_response_overloaded(out, err) and time.time() < deadline:
                time.sleep(RECONNECT_CAPABILITIES_RETRY_INTERVAL_SEC)
                continue
            break
        assert not is_gnmi_failure(last_err), f"Reconnect capabilities failed: {last_err}"


def build_capabilities_flood_loop_cmd(gnmic_cmd, duration_sec, per_request_timeout_sec):
    """
    Build a POSIX-sh one-liner that runs ``gnmic_cmd`` in a tight loop until ``duration_sec``
    elapses, classifies each response, and prints aggregate counts. The request loop runs as a single
    background process instead of driving each request synchronously from Python.

    Output (on stdout) ends with two parseable lines::

        FLOOD_RESULT s=<success> f=<fail> rl=<rate_limit_fail> ov=<overload_fail>
        FLOOD_SAMPLE <first error sample, newlines flattened>

    A trap emits the counts even if the process is stopped (SIGINT/SIGTERM) before the deadline,
    so collection via either wait_process or stop_and_wait_process recovers the totals.
    """
    # All error markers come from the shared GnmicErr constants, so the loop's classification stays
    # in sync with is_gnmi_failure / is_gnmi_rate_limit_error / is_gnmi_overload_error. The markers
    # are passed to grep -E, so they must remain ERE-safe (no special regex characters); they
    # currently are - add new markers with that constraint in mind.
    err_re = "|".join(GnmicErr.ALL_ERRS)
    overload_re = "|".join(GnmicErr.OVERLOAD_ERRS)
    rate_limit_marker = GnmicErr.LOCAL_RATE_LIMITED
    return (
        "s=0; f=0; rl=0; ov=0; sample=''; "
        "emit() { echo \"FLOOD_RESULT s=$s f=$f rl=$rl ov=$ov\"; echo \"FLOOD_SAMPLE $sample\"; }; "
        "trap 'emit; exit 0' INT TERM; "
        f"end=$(( $(date +%s) + {int(duration_sec)} )); "
        "while [ \"$(date +%s)\" -lt \"$end\" ]; do "
        f"o=$(timeout {int(per_request_timeout_sec)} {gnmic_cmd} 2>&1); "
        f"if printf '%s' \"$o\" | grep -qE '{err_re}'; then "
        "f=$((f+1)); "
        f"if printf '%s' \"$o\" | grep -q '{rate_limit_marker}'; then rl=$((rl+1)); fi; "
        f"if printf '%s' \"$o\" | grep -qE '{overload_re}'; then ov=$((ov+1)); fi; "
        "if [ -z \"$sample\" ]; then sample=$(printf '%s' \"$o\" | head -c 2000 | tr '\\n' ' '); fi; "
        "else s=$((s+1)); fi; "
        "done; emit"
    )


def parse_capabilities_flood_output(out):
    """Parse the FLOOD_RESULT / FLOOD_SAMPLE lines emitted by build_capabilities_flood_loop_cmd.

    Returns ``(success, fail, rate_limit_fail, overload_fail, sample_error)``. The last FLOOD_RESULT
    line wins.
    """
    success, fail, rate_limit_fail, overload_fail, sample = 0, 0, 0, 0, ""
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("FLOOD_RESULT"):
            m = re.search(r"s=(\d+)\s+f=(\d+)\s+rl=(\d+)\s+ov=(\d+)", line)
            if m:
                success, fail, rate_limit_fail, overload_fail = (
                    int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                )
        elif line.startswith("FLOOD_SAMPLE"):
            sample = line[len("FLOOD_SAMPLE"):].strip()
    return success, fail, rate_limit_fail, overload_fail, sample


def run_parallel_capabilities_flood(
    dut,
    username,
    password,
    *,
    duration_sec,
    num_clients,
    cmd_timeout_sec,
):
    """
    Run ``num_clients`` Capabilities-flood loops concurrently for ``duration_sec``.

    Follows the devts background-validation model: each client is a single background process that
    loops gnmic on the local player. Processes are started non-blocking (staggered), left to run for
    ``duration_sec``, then collected and parsed for per-client counts.

    Returns ``(results, elapsed_sec, first_error)`` where ``results[i]`` is
    ``(success, fail, rate_limit_fail, overload_fail)`` and ``first_error`` may contain key ``"err"``.
    """
    # A separate GnmiClient per process gives each its own runner/connection. The gnmic command is
    # composed once from the first client (same composition a single GnmiClient call uses) and then
    # wrapped in the loop, so no throwaway client/connection is created just to build it.
    clients, procs = [], []
    loop_cmd = None
    with allure.step(f"Start {num_clients} background capabilities-flood loops"):
        for _ in range(num_clients):
            client = GnmiClient(
                dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, username, password,
                cmd_time=cmd_timeout_sec,
            )
            if loop_cmd is None:
                gnmic_cmd = client.compose_gnmic_cmd("capabilities", skip_cert_verify=True)
                loop_cmd = build_capabilities_flood_loop_cmd(gnmic_cmd, duration_sec, cmd_timeout_sec)
            _, _, proc = client.cmd_runner.run_cmd_in_process(loop_cmd, keep_process_alive=True)
            clients.append(client)
            procs.append(proc)
            time.sleep(FLOOD_PROCESS_INIT_DELAY_SEC)

    start_time = time.time()
    with allure.step(f"Let flood run for {duration_sec}s"):
        time.sleep(duration_sec)
    # NOTE: elapsed_sec is the wait window, not the exact loop runtime; staggered
    # starts mean loops run marginally longer, so any RPM derived from it is biased slightly high.
    elapsed_sec = time.time() - start_time

    results = [None] * num_clients
    first_error = {}
    # Loops self-terminate at their own deadline; allow one extra per-request timeout (plus grace)
    # for the final in-flight gnmic call to finish and the counts to be printed before forcing stop.
    collect_timeout = cmd_timeout_sec + FLOOD_COLLECT_GRACE_SEC
    with allure.step("Collect and parse flood results"):
        for i, (client, proc) in enumerate(zip(clients, procs)):
            out, _ = client.cmd_runner.wait_cmd_process(proc, collect_timeout)
            success, fail, rate_limit_fail, overload_fail, sample = parse_capabilities_flood_output(out)
            results[i] = (success, fail, rate_limit_fail, overload_fail)
            if sample and "err" not in first_error:
                first_error["err"] = sample[:SAMPLE_ERROR_MAX_LEN]
    return results, elapsed_sec, first_error


def shutdown_threads(thread_list, first_timeout_sec, second_timeout_sec=5):
    """Join threads with two bounded attempts; return the list still alive.

    ``Thread.join(timeout=...)`` does not guarantee termination, so callers
    must check the returned list and fail the test (or take additional
    action) if any worker is still running — otherwise a stuck worker can
    leak into subsequent tests. A second bounded join gives slow-but-not-
    stuck workers a final chance to exit after a stop event is set.
    """
    for t in thread_list:
        t.join(timeout=first_timeout_sec)
    still_alive = [t for t in thread_list if t.is_alive()]
    if still_alive:
        for t in still_alive:
            t.join(timeout=second_timeout_sec)
        still_alive = [t for t in thread_list if t.is_alive()]
    return still_alive
