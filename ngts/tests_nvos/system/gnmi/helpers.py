import logging
import os
import random
import re
import string
import subprocess
import threading
import time
import json
from typing import Optional, Tuple, List

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
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient, GnmicCmdBuilder
from ngts.tests_nvos.system.gnmi.constants import CERTIFICATE, GnmiServerStatus, GnmicErr
from ngts.tests_nvos.system.gnmi.constants import DUT_GNMI_CERTS_DIR, DOCKER_CERTS_DIR, GnmiMode, GrpcMsg, \
    SERVER_REFLECTION_SUBSCRIBE_RESPONSE
from ngts.tests_nvos.system.gnmi.constants import (
    FLOOD_COLLECT_GRACE_SEC, FLOOD_PROCESS_INIT_DELAY_SEC,
    PER_REQUEST_TIMEOUT_SEC, RECONNECT_CAPABILITIES_MAX_WAIT_SEC,
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


def gnmi_basic_flow(engines, mode='', ipv6=False, mgmt_port_name='eth0'):
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
    validate_gnmi_is_running_and_stream_updates(system, gnmi_server_obj, engines, target_ip, mode=mode)

    with allure.step('Disable gnmi'):
        gnmi_server_obj.disable_gnmi_server()
        validate_gnmi_disabled_and_not_running(gnmi_server_obj, engines)
        validate_gnmi_server_in_health_issues(system, expected_gnmi_health_issue=False)

    with allure.step('Enable gnmi'):
        gnmi_server_obj.enable_gnmi_server()
        validate_gnmi_is_running_and_stream_updates(system, gnmi_server_obj, engines, target_ip, mode=mode)


def validate_gnmi_is_running_and_stream_updates(system, gnmi_server_obj, engines, target_ip, mode='', username='',
                                                password=''):
    with allure.step('Validate gnmi is running and stream updates'):
        validate_gnmi_enabled_and_running(gnmi_server_obj, engines)
        validate_gnmi_server_in_health_issues(system, expected_gnmi_health_issue=False)
        port_description = Tools.RandomizationTool.get_random_string(7)
        change_port_description_and_validate_gnmi_updates(engines, port_description=port_description,
                                                          target_ip=target_ip, mode=mode, username=username,
                                                          password=password)


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
                                     mode='', username='', password=''):
    username = username or devices.dut.default_username
    password = password or devices.dut.default_password
    with allure.step("run gnmi-client and parse output"):
        sonic_mgmt_engine = engines.sonic_mgmt
        prefix_and_path = xpath.rsplit("/", 1)
        mode_flag = f"--mode {mode}" if mode else ''
        cmd = f"gnmic -a {target_ip} --port {target_port} --skip-verify subscribe --prefix '{prefix_and_path[0]}'" \
            f" --path '{prefix_and_path[1]}' --target nvos -u {username} " \
            f"-p {password} {mode_flag} --format flat"
        cmd = "timeout -s INT 4s " + cmd
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
    sample_flags = f"--stream-mode sample --sample-interval {sample_interval}" if sample_interval else ''
    path_flags = " ".join([f"--path '{path}'" for path in paths])
    return (
        f"gnmic -a {target_ip} --port {target_port} --skip-verify subscribe "
        f"--prefix '{prefix}' {path_flags} --target nvos -u {username} -p {password} "
        f"{mode_flag} {sample_flags} --format json"
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


def run_concurrent_multi_path_stream_subscribers(
        engines, devices, target_ip, prefix, paths, num_subscribers,
        window_sec=STREAM_SUBSCRIBE_WINDOW_SEC, target_port=GnmiConsts.GNMI_DEFAULT_PORT,
        username='', password='', sample_interval=None):
    """
    Open `num_subscribers` concurrent gnmic STREAM subscriptions on the same multi-path
    set, stream for `window_sec` seconds, and return a list of length `num_subscribers`
    where element i is the parsed JSON notifications observed by subscriber i.

    When `sample_interval` is set (e.g. '1s'), each subscriber streams in SAMPLE mode and
    receives a fresh result-set every interval, so a `window_sec` window yields multiple
    result-sets per subscriber instead of a single snapshot.
    """
    assert paths, "paths list cannot be empty"
    assert num_subscribers >= 1, f"num_subscribers must be >= 1; got {num_subscribers}"

    username = username or devices.dut.default_username
    password = password or devices.dut.default_password

    base_cmd = _compose_multi_path_subscribe_cmd(
        target_ip, prefix, paths, target_port, GnmiMode.STREAM, username, password,
        sample_interval=sample_interval)

    out_files = [f"/tmp/gnmi_multipath_sub_{i}.out" for i in range(num_subscribers)]
    cleanup = "rm -f /tmp/gnmi_multipath_sub_*.out"

    # Launch all subscribers as concurrent background jobs, each writing to its own file.
    # Each self-terminates after window_sec via `timeout -s INT`; `wait` blocks until all
    # have flushed their output files.
    launch_parts = [f"{cleanup} ;"]
    for out_file in out_files:
        launch_parts.append(f"timeout -s INT {window_sec}s {base_cmd} > {out_file} 2>&1 &")
    launch_parts.append("wait")
    launch_script = " ".join(launch_parts)

    sonic_mgmt_engine = engines.sonic_mgmt
    with allure.step(f"run {num_subscribers} concurrent multi-path STREAM subscribers for {window_sec}s"):
        sonic_mgmt_engine.run_cmd(launch_script)

    # Read each subscriber's output file on its own so the per-subscriber outputs are never
    # merged, then check for any gnmic failure and parse its notifications.
    per_subscriber = []
    for out_file in out_files:
        chunk = Tools.FilesTool.read_file_content(sonic_mgmt_engine, out_file, use_sudo=False).verify_result()
        assert not is_gnmi_failure(chunk), f"gnmic subscriber output {out_file} reported a failure:\n{chunk}"
        per_subscriber.append(_extract_json_objects_from_gnmic_output(chunk))

    sonic_mgmt_engine.run_cmd(cleanup)
    return per_subscriber


def _is_data_notification(obj):
    """Return True for a real gNMI data Notification (not a sync-response / timestamp-less control frame)."""
    return bool(obj.get("timestamp") is not None and not obj.get("sync-response"))


def count_result_sets(notifications):
    """Return the number of distinct streamed result-sets (one per gNMI Notification timestamp)."""
    timestamps = {obj.get("timestamp") for obj in notifications if _is_data_notification(obj)}
    return len(timestamps)


def _extract_leaves_from_update(update):
    """
    Return the set of leaf names contributed by a single gnmic `updates[]` entry.

    gnmic's JSON shape is not perfectly stable across versions, so we read leaves from
    every place a name can appear and let the caller union them:
      - "Path"  : capital-P; what current gnmic (--format json) emits, e.g. "in-octets"
                  or "counters/in-octets" depending on subscription prefix.
      - "path"  : lowercase variant emitted by some forks / older gnmic builds.
      - "values": dict whose keys are paths-relative-to-prefix; this is the form the
                  rest of the repo already consumes (see test_cpu_memory_monitoring.py).
    Final `rsplit("/", 1)[-1]` reduces "counters/in-octets" -> "in-octets" so the
    matcher works regardless of how deep the path was reported.
    """
    if not isinstance(update, dict):
        return set()

    candidates = {update.get("Path", ""), update.get("path", "")}
    values = update.get("values")
    if isinstance(values, dict):
        candidates.update(values.keys())

    leaves = {str(c).rsplit("/", 1)[-1] for c in candidates if c}
    leaves.discard("")
    return leaves


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


def change_port_description_and_validate_gnmi_updates(engines, port_description, target_ip, mode='', username='',
                                                      password=''):
    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).returned_value
    selected_port.interface.set(NvosConst.DESCRIPTION, port_description, apply=True).verify_result()
    selected_port.update_output_dictionary()
    verify_description_value(selected_port.show_output_dictionary, port_description)

    devices = TestToolkit.devices

    xpath = f'interfaces/interface[name={selected_port.name}]/state/description'
    logger.info(f"sleep {GnmiConsts.SLEEP_TIME_FOR_UPDATE} sec until we start validate the gnmi stream")
    time.sleep(GnmiConsts.SLEEP_TIME_FOR_UPDATE)
    gnmi_stream_updates = run_gnmi_client_and_parse_output(engines, devices, xpath, target_ip, mode=mode,
                                                           username=username, password=password)
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
            result = abs(int(gnmi_client_output) - int(redis_output))
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


def verify_gnmi_client_tools_installed(engines=None, verify_player=True):
    if verify_player:
        player = GnmiClient('', '', '', '', 10)
        with allure.step('verify gnmic installation on test player'):
            player.verify_gnmic_installation()
        with allure.step('verify grpcurl  installation on test player'):
            player.verify_grpcurl_installation()

    engines = engines or TestToolkit.engines
    if engines is not None and hasattr(engines, 'sonic_mgmt'):
        sonic_mgmt = engines.sonic_mgmt
        sonic_mgmt_client = GnmiClient('', '', '', '', 10, engine=sonic_mgmt)
        with allure.step(f'verify gnmic installation on sonic-mgmt engine {sonic_mgmt.ip}'):
            sonic_mgmt_client.verify_gnmic_installation()
        with allure.step(f'verify grpcurl installation on sonic-mgmt engine {sonic_mgmt.ip}'):
            sonic_mgmt_client.verify_grpcurl_installation()


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


def get_gnmic_engine(engines):
    """
    Engine on which gnmic should run for the rate-limit / load tests.

    Returns the sonic-mgmt engine when the topology defines it, so gnmic requests are generated
    from the sonic-mgmt host instead of the local test player. Falls back to None (local player)
    when no sonic-mgmt engine is available, so tests still run in topologies without one.
    """
    return getattr(engines, 'sonic_mgmt', None)


def is_gnmi_failure(err):
    """True if the given text indicates a failed gNMI request (e.g. rate limit, rpc error)."""
    return bool(err) and any(m in err for m in GnmicErr.ALL_ERRS)


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
    elapses, classifies each response, and prints aggregate counts. This pushes the request loop
    onto the engine (run as a single background process), matching how devts background validations
    generate load remotely instead of driving each request synchronously from the player.

    Output (on stdout) ends with two parseable lines::

        FLOOD_RESULT s=<success> f=<fail> rl=<rate_limit_fail> ov=<overload_fail>
        FLOOD_SAMPLE <first error sample, newlines flattened>

    A trap emits the counts even if the process is stopped (SIGINT/SIGTERM) before the deadline,
    so collection via either wait_process or stop_and_wait_process recovers the totals.
    """
    # All error markers come from the shared GnmicErr constants, so the remote classification stays
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
    engine=None,
):
    """
    Run ``num_clients`` Capabilities-flood loops concurrently for ``duration_sec``.

    Follows the devts background-validation model: each client is a single background process that
    loops gnmic on the target host (the sonic-mgmt ``engine`` when given, else the local player).
    Processes are started non-blocking (staggered), left to run for ``duration_sec``, then collected
    and parsed for per-client counts. Running the loop on the engine itself gives true concurrency
    on a single engine without per-request round-trips from the player.

    Returns ``(results, elapsed_sec, first_error)`` where ``results[i]`` is
    ``(success, fail, rate_limit_fail, overload_fail)`` and ``first_error`` may contain key ``"err"``.
    """
    # A separate GnmiClient per process gives each its own runner/connection. The gnmic command is
    # composed once from the first client (same composition a single GnmiClient call uses) and then
    # wrapped in the remote loop, so no throwaway client/connection is created just to build it.
    target_desc = f"engine {engine.ip}" if engine is not None else "local player"
    clients, procs = [], []
    loop_cmd = None
    with allure.step(f"Start {num_clients} background capabilities-flood loops on {target_desc}"):
        for _ in range(num_clients):
            client = GnmiClient(
                dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, username, password,
                cmd_time=cmd_timeout_sec, engine=engine,
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
    # NOTE: elapsed_sec is the player-side wait window, not the exact remote loop runtime; staggered
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
