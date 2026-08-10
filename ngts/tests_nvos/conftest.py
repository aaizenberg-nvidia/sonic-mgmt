from argparse import ArgumentTypeError
from typing import Dict, Optional
import concurrent.futures
from pathlib import Path
import datetime
import logging
import os
import random
import smtplib
import subprocess
import time
import json
from email.mime.text import MIMEText

import re

import requests_cache
import pexpect
import pytest
from dotted_dict import DottedDict
from retry import retry

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine
from devts.infra.tools.exceptions.setup_issue import SetupIssue
from devts.infra.tools.general_constants.air_constants import NvidiaAirConstants
from devts.infra.tools.linux_tools.linux_tools import scp_file
from ngts.helpers.object_filters import filter_objects
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.tools.mars_test_cases_results.Connect_to_MSSQL import ConnectMSSQL
from ngts.cli_wrappers.linux.linux_general_clis import LinuxGeneralCli
from ngts.cli_wrappers.nvue.nvue_base_clis import NvueBaseCli
from ngts.cli_wrappers.nvue.cumulus.cumulus_general_cli import CumulusGeneralCli
from ngts.cli_wrappers.openapi.openapi_command_builder import OpenApiRequest
from ngts.constants.constants import DbConstants, CliType, DebugKernelConsts, InfraConst, CoreDumpConsts
from ngts.nvos_constants.constants_nvos import ApiType, OperationTimeConsts, OutputFormat, NvosConst, TestConsts, \
    SyslogConsts, SystemConsts
from ngts.tests_nvos.platform.els_fiber_tuning.cpo_constants import CpoConsts
from ngts.nvos_tools.Devices.BaseDevice import BaseDevice
from ngts.nvos_tools.Devices.DeviceFactory import DeviceFactory
from ngts.nvos_tools.Devices.cpo.CpoTopology import is_cpo_capable
from ngts.nvos_tools.cli_coverage.nvue_cli_coverage import NVUECliCoverage
from ngts.nvos_tools.ib.opensm.OpenSmTool import OpenSmTool
from ngts.nvos_tools.infra import ExceptionTool
from ngts.nvos_tools.infra.CmdRunner import CmdRunner
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.DiskTool import DiskTool
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.nvos_tools.infra.NvCommand import NvCommand
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RegressionConfigurations import RegressionConfigurations
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.infra.SerialConsoleTool import SerialConsoleTool
from ngts.nvos_tools.infra.TrafficGeneratorTool import TrafficGeneratorTool
from ngts.nvos_tools.system.System import System
from ngts.scripts.code_coverage.code_coverage_consts import NvosConsts
from ngts.scripts.code_coverage.test_code_coverage import extract_python_coverage_for_nvos
from ngts.tests.nightly.logging.test_log_analyzer_errors_during_deploy_sonic import get_oldest_syslog_id, \
    get_new_start_string, insert_new_start_string
from ngts.tests_nvos.helpers.pytest_helpers import is_cur_test_has_marker, get_marker_arg_value, is_cur_test_passed
from ngts.tests_nvos.helpers.pytest_items_filters import run_nvos_pytest_items_modification
from ngts.tests_nvos.infra import nvos_hub as _nvos_hub
from ngts.tests_nvos.infra.nvos_hub import nvos_hub_ai_investigation  # noqa: F401
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_general_utils import is_ipv6_setup, wait_for_ldap_nvued_restart_workaround
from ngts.nvos_tools.infra.SecureBootTool import SecureBootTool
from ngts.tests_nvos.constants import PRODUCTION, DEVELOPMENT
from ngts.ngts_types import EnginesT
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.AirTool import get_internal_ip_for_oob_server
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.infra.IbRouterTool import IbRouterTool

logger = logging.getLogger()

EXPECTED_KERNEL_PATTERNS = [
    re.compile(r".*DPC: error containment capabilities:.*"),
    re.compile(r".*ib3: multicast join failed for.*, status -\d+"),
    re.compile(r".*RAS: Correctable Errors collector initialized.*"),
]

pytest_plugins = [
    "ngts.common.plugins.valgrind.plugin",
]


def pytest_configure(config):
    """
    Load Vault secrets early in pytest initialization for local (non-MARS) runs.
    This hook runs before session start and any fixtures.

    For MARS runs, secrets are already provided via environment variables.
    For local runs, we fetch secrets from Vault.

    This only runs when NVOS tests are being executed.
    """
    # Only run for NVOS tests - check if we're running tests from tests_nvos directory
    args = config.args if hasattr(config, 'args') else config.invocation_params.args
    if not args or not any('tests_nvos' in str(arg) for arg in args):
        logger.debug("Not running NVOS tests, skipping Vault secrets loading")
        return

    mars_key_id = config.getoption("--mars_key_id", default=None)
    session_id = config.getoption("--session_id", default=None)
    if mars_key_id or session_id:
        logger.info("MARS run detected - secrets already in environment, skipping Vault")
        return

    from ngts.nvos_tools.infra.VaultClient import VaultClient

    logger.info("Local run detected - loading secrets from Vault...")
    VaultClient.fetch_and_export_secrets()


def pytest_addoption(parser: pytest.Parser):
    """
    Parse NVOS pytest options
    :param parser: pytest build in
    """
    logger.info('Parsing NVOS pytest options')
    parser.addoption("--max_case_instances", action="store", type=int, default=None, help="Randomly select N test instances for each test function")
    parser.addoption('--release_name', action='store',
                     help='The name of the release to be tested. For example: 25.01.0630')
    parser.addoption("--restore_to_image",
                     action="store", default=None, help="restore image after error flow")
    parser.addoption("--traffic_available",
                     action="store", default='True', help="True to run traffic tests")
    parser.addoption("--tst_all_pwh_confs",
                     action="store", default='False', help="True to test functionality of all password hardening "
                                                           "configurations; False otherwise (only several random "
                                                           "configurations will be picked to testing)")
    parser.addoption("--disable_cli_coverage", action="store_true", default=False, help="Do not run cli coverage")
    parser.addoption("--security_post_checker", action="store_true", default=False, required=False,
                     help="Whether to run security post checker or not")
    parser.addoption("--check_output", action="store_true", default=False, help="Provide to check ib output")
    parser.addoption("--substrings_to_check", action="store", default=False, help="Provide which substrings to check")
    parser.addoption("--remote_test_path", action="store", default=None, help="Remote test path from MARS")
    parser.addoption("--skip_clear_config", action="store_true", default=False, help="skip the clear_config fixture at the end of the test")
    parser.addoption('--upgrade-matrix-json', type=_validate_matrix_arg, help='Path to matrix json file or json string')
    parser.addoption("--override-target-version", action="store_true", default=None,
                     help="Override the target version with the target from the upgrade downgrade matrix")
    parser.addoption(
        "--fixed-random-api",
        action="store",
        default=None,
        metavar="API",
        help="Pin random_api parametrization to NVUE or OpenApi (same strings as test ids). "
             "If unset, NVOS_FIXED_RANDOM_API is used. Default behavior is one random API per run.",
    )
    parser.addoption(
        "--nvos-hub-ai-investigation",
        action="store_true",
        default=False,
        help="Force-enable NVOS Hub AI-investigation auto-queue on test failures. "
             "ON by default only for MARS regression runs (REGRESSION_TYPE in "
             "RegressionType.mars_types(): regression, sonic_main, sonic_public, "
             "sonic_dpu_build); OFF for CI runs and local/manual runs. Pass this flag (or set "
             "env var NVOS_HUB_AI_INVESTIGATION to one of 1/true/yes/on) to opt in on any "
             "other run. To force it off anywhere (including a regression run) set "
             "NVOS_HUB_AI_INVESTIGATION to one of 0/false/no/off. When enabled, every failing "
             "test fires a best-effort POST to the dashboard and a deep investigation card is "
             "auto-generated; the Allure report gets a link to it.",
    )


def _resolve_fixed_random_api(config: pytest.Config) -> Optional[str]:
    """Pinned ApiType value for random_api, or None for default (random / collect-all)."""
    opt = config.getoption("--fixed-random-api")
    if opt is None:
        opt = os.environ.get("NVOS_FIXED_RANDOM_API")
    if not opt:
        return None
    opt_stripped = opt.strip()
    by_lower = {t.lower(): t for t in ApiType.ALL_TYPES}
    resolved = by_lower.get(opt_stripped.lower())
    if resolved is None:
        raise pytest.UsageError(
            f"--fixed-random-api / NVOS_FIXED_RANDOM_API must be one of {ApiType.ALL_TYPES}, got {opt_stripped!r}"
        )
    return resolved


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    run_nvos_pytest_items_modification(config, items)


def pytest_collection_finish(session: pytest.Session):
    """Fail explicitly when a MARS-managed run selects zero tests.

    Without this guard, an incorrect marker/keyword combination silently
    deselects every test and the session exits with code 0 (pass).
    """
    if session.items:
        return

    mars_key_id = session.config.getoption('--mars_key_id', default=None)
    session_id = session.config.getoption('--session_id', default=None)
    if not mars_key_id and not session_id:
        return

    markexpr = session.config.option.markexpr or ''
    keyword = session.config.option.keyword or ''

    filters = []
    if markexpr:
        filters.append(f"-m '{markexpr}'")
    if keyword:
        filters.append(f"-k '{keyword}'")
    filter_desc = ' '.join(filters) if filters else 'no marker/keyword filters'

    pytest.exit(
        f"CI/Regression run (session_id={session_id}) selected 0 tests with {filter_desc}. "
        "All collected tests were deselected — check marker and keyword filters.",
        returncode=pytest.ExitCode.USAGE_ERROR
    )


@pytest.fixture
def verify_no_kernel_errors(engines):
    yield
    with allure.step("Validate no error logs in kernel"):
        kernel_errors = engines.dut.run_cmd("sudo dmesg | grep -Ei 'error|fail|warning'")
        lines = kernel_errors.splitlines()

        unexpected_lines = [
            line for line in lines
            if not any(pattern.search(line) for pattern in EXPECTED_KERNEL_PATTERNS)
        ]

        filtered_logs = "\n".join(unexpected_lines)
        allure.attach("Filtered Kernel Errors", filtered_logs or "(no unexpected errors)")
        assert not filtered_logs.strip(), f"Unexpected error logs in kernel:\n{filtered_logs}"


@pytest.fixture(scope='function')
def show_platform_initial_state(engines):
    """
    For regression analysis, print the platform info before each test case.
    This helps to understand the initial state of the system (firmware versions, etc.).
    Add this fixture to platform firmware tests to see initial state.
    """
    with allure.step('Before test case: show platform firmware info'):
        platform = Platform()
        firmware_output = platform.firmware.show()
        logger.info(f"Platform firmware initial state:\n{firmware_output}")


@pytest.fixture(scope='session')
def update_platform_expected_values():
    """Update device-specific expected platform values before platform output validation."""
    with allure.step('Update platform expected values'):
        platform = Platform()
        output = OutputParsingTool.parse_show_output_to_dict(platform.show()).get_returned_value()
        TestToolkit.get_device().update_show_platform_output(output)


@pytest.fixture(autouse=True)
def cpov2_device_guard(request):
    """Self-skip guard for Gen2 CPO tests (marker `cpov2`).

    Skips any cpov2-marked test when the DUT device does not expose a Gen2 CPO
    topology (device.cpo, attached by PortiaCpoCapability), so cpov2 tests never
    fail on Taipan (Gen1) or non-CPO devices even if mis-selected. The `devices`
    fixture is requested lazily to keep this a no-op for non-cpov2 tests.
    """
    if request.node.get_closest_marker('cpov2') is not None:
        devices = request.getfixturevalue('devices')
        if not is_cpo_capable(devices.dut):
            pytest.skip('cpov2 test requires a Gen2 CPO-capable device (device.cpo is absent)')


@pytest.fixture(autouse=True)
def check_disk_usage(request, engines):
    marker_name = 'check_disk_usage'
    should_check = is_cur_test_has_marker(request, marker_name)
    if should_check:
        with allure.step("Get initial disk stats"):
            field_to_read = 'kB_wrtn'
            initial_output = OutputParsingTool.run_iostat_and_parse(engines.dut)
            device = next((devices for devices in initial_output.keys() if not devices.startswith('loop')), None)
            initial_kb = int(initial_output[device][field_to_read])

    yield

    if should_check:
        with allure.step("Fetching written data size"):
            final_output = OutputParsingTool.run_iostat_and_parse(engines.dut)
            final_kb = int(final_output[device][field_to_read])
            delta_kb = (final_kb - initial_kb)

        allure.attach('Written data size', f'before: {initial_kb}KB\nafter: {final_kb}KB\ntest added: {delta_kb}KB')

        if is_cur_test_passed(request):
            expected_threshold = get_marker_arg_value(request, marker_name, 'expect')
            if expected_threshold and isinstance(expected_threshold, int):
                with allure.step(f'make sure test addition is less than expected ({expected_threshold})'):
                    assert delta_kb <= expected_threshold, f"Wrote {delta_kb}KB (max {expected_threshold}KB allowed)"


@pytest.fixture(autouse=True)
def check_log_size(request, engines):
    def __get_syslog_file_size_kb(filename='syslog') -> int:
        return int(engines.dut.run_cmd(f'du -k /var/log/{filename} | cut -f1'))
    marker_name = 'check_log_size'
    should_check = is_cur_test_has_marker(request, marker_name)
    if should_check:
        with allure.step('get syslog size before (in KB)'):
            size_before = __get_syslog_file_size_kb()
    yield
    if should_check:
        with allure.step('get syslog size after (in KB)'):
            size_after = __get_syslog_file_size_kb()
            if size_after <= size_before:
                logging.info('log was rotated')
                size_after += __get_syslog_file_size_kb('syslog.1')
            test_addition_to_syslog = size_after - size_before
        allure.attach('syslog sizes', f'before: {size_before}KB\nafter: {size_after}KB\ntest added: {test_addition_to_syslog}KB')
        if is_cur_test_passed(request):
            expected_threshold = get_marker_arg_value(request, marker_name, 'expect')
            if expected_threshold and isinstance(expected_threshold, int):
                with allure.step(f'make sure test addition is less than expected ({expected_threshold})'):
                    assert test_addition_to_syslog <= expected_threshold, f'test added {test_addition_to_syslog}KB to syslog. allowed: {expected_threshold}'


@pytest.fixture(autouse=True)
def check_ib_output(request):
    """
    Method for getting check_ib_output and substrings_to_check from pytest arguments
    :param request: pytest builtin
    """
    if request.config.getoption("--check_output"):
        NvueBaseCli.check_output_strings = True
    if request.config.getoption("--substrings_to_check"):
        NvueBaseCli.sub_strings_to_search = request.config.getoption('--substrings_to_check').split(',')


@pytest.fixture(autouse=True)
def track_serial_console(request, topology_obj, engines, devices, is_air):
    """
    fixture to track serial console during test run,
        and if the test is failing, attach the serial console output to allure report (for better debug).

    This will apply for all test that has any of the defined interesting markers below.
    """
    interesting_markers = ['track_serial_console', 'reboot', 'factory_reset', 'reset_factory']
    should_track_serial_console = not is_air and any(is_cur_test_has_marker(request, marker) for marker in interesting_markers)

    if should_track_serial_console:
        with allure.step('start tracking serial console into file'):
            serial_log_file_path = '/tmp/serial.log'
            serial_connection_cmd = SerialConsoleTool.get_serial_console_connection_command(topology_obj)
            logging.info('connect to serial console and save output into a file')
            cmd = f'script -c "{serial_connection_cmd}" {serial_log_file_path}'
            child = pexpect.spawn(cmd)

    yield

    if should_track_serial_console:
        with allure.step('end serial console session'):
            for _ in range(3):
                child.sendcontrol('z')
                time.sleep(0.5)
            for _ in range(3):
                child.sendcontrol('d')
                time.sleep(0.5)
            # child.expect(pexpect.EOF)
            child.close()
        if request.node.rep_call.failed:
            try:
                with allure.step('take log file content'):
                    with open(serial_log_file_path, 'r', errors='replace') as file:
                        serial_log_content = file.read()
                with allure.step('attach content to allure'):
                    allure.attach('Serial Console log during test', serial_log_content)
            except Exception as e:
                err = f'failed to attach serial output from {serial_log_file_path} : {ExceptionTool.format_traceback()}'
                logging.warning(err)
                allure.attach('Attachment Failure', err)
        else:
            with allure.step('test passed. not attaching serial console log'):
                pass


def _reset_ansible_ssh_control_masters():
    """Close ansible's ssh ControlMaster sockets so the next ansible task reconnects.

    A reboot under test kills the remote sshd session, but ansible.cfg uses
    'ControlMaster=auto -o ControlPersist=7200s' with a ~35-minute ServerAlive window
    (ServerAliveInterval=30 * ServerAliveCountMax=70), so the local master socket keeps
    multiplexing to the dead session and every subsequent ansible task (loganalyzer
    analyze_logs, sysdumps, serial log analyzer) blocks until the pytest test timeout.
    Rebuilding the host object does NOT help: ControlMaster=auto re-attaches to the same
    dead master. We must drop the master itself.

    ansible's default ControlPath is a hash ('%C'), so we cannot map a socket back to a
    host; for a reboot test it is safe to close them all -- any unrelated master simply
    reconnects on next use. 'ssh -O exit' only talks to the local control socket (no
    network), so it cannot hang; we still bound it and fall back to removing the socket file.
    """
    cp_dir = os.path.join(os.environ.get("ANSIBLE_HOME", os.path.expanduser("~/.ansible")), "cp")
    try:
        sockets = [os.path.join(cp_dir, name) for name in os.listdir(cp_dir)]
    except OSError:
        sockets = []
    closed = 0
    for sock in sockets:
        closed_this = False
        try:
            res = subprocess.run(["ssh", "-O", "exit", "-o", "ControlPath={}".format(sock), "none"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            closed_this = res.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            closed_this = False
        if not closed_this:
            # ssh -O exit did not confirm a clean close (no/wedged master or timeout):
            # remove the socket file so a fresh master is created on the next connect.
            try:
                os.unlink(sock)
                closed_this = True
            except OSError:
                pass
        if closed_this:
            closed += 1
    logging.info("Reset %d ansible ssh ControlMaster socket(s) under %s after reboot", closed, cp_dir)


@pytest.fixture(autouse=True)
def reset_ansible_connection_after_reboot(loganalyzer):
    """Refresh the ansible ssh connection after any test that rebooted the DUT.

    The NVOS 'duthosts' ansible engines are session-scoped (built once and reused), so
    nothing re-establishes them after a per-test reboot. Combined with ansible's
    persistent ControlMaster (see _reset_ansible_ssh_control_masters), the first ansible
    consumer in teardown -- typically loganalyzer's analyze_logs, but also sysdumps /
    serial log analyzer -- blocks on the dead master until the whole-test pytest timeout
    fires (observed as 'Failed: Timeout >900.0s' on test_reboot_test).

    Detection is automatic and marker-free: every reboot/reset/install flow sets the
    process-wide flag pytest.dut_rebooted from DutUtilsTool (in wait_on_system_reboot and
    the readiness waits wait_for_nvos/cumulus_to_become_functional). Here we read it in
    teardown and, if a reboot happened, close the stale ControlMaster so the next ansible
    task reconnects fresh. New reboot tests are covered with no @pytest.mark.reboot to
    remember.

    Depends on 'loganalyzer' purely for ordering: teardown is LIFO, so by setting up
    after loganalyzer we guarantee this reset runs BEFORE loganalyzer's analyze_logs. The
    reset itself is not gated on loganalyzer being enabled.
    """
    yield

    if getattr(pytest, 'dut_rebooted', False):
        pytest.dut_rebooted = False
        with allure.step('Reset ansible ssh ControlMaster after reboot'):
            _reset_ansible_ssh_control_masters()


@pytest.fixture(scope='session')
def engines(topology_obj, devices, request):
    from ngts.nvos_tools.infra.CommandTracker import command_tracker

    engines_data = DottedDict()

    # Setup engines for all DUT players (dut, dut2, dut3, etc.)
    for player_name, player in filter_objects(topology_obj.players, host_type='dut', engine_type='ssh').items():
        engine = player['engine']
        engines_data[player_name] = engine
        device = devices[player_name]
        is_ipv6 = is_ipv6_setup(topology_obj, player_name)
        update_engine_dut_mgmt_port(topology_obj, engine, device, player_name, is_ipv6)
        logger.info(f'Updated engine management port for {player_name} (ipv6={is_ipv6})')

    # ha and hb are the traffic dockers
    if "ha" in topology_obj.players:
        engines_data.ha = topology_obj.players['ha']['engine']
        engines_data.ha_attr = topology_obj.players['ha']['attributes']
    if "hb" in topology_obj.players:
        engines_data.hb = topology_obj.players['hb']['engine']
        engines_data.hb_attr = topology_obj.players['hb']['attributes']

    # engines.hfnm refers to the VM connected to the FNM port if there is one, or to HA if there isn't
    if "hfnm" in topology_obj.players:
        engines_data.hfnm = topology_obj.players['hfnm']['engine']
        engines_data.hfnm_attr = topology_obj.players['hfnm']['attributes']
    elif "ha" in topology_obj.players:
        engines_data.hfnm = topology_obj.players['ha']['engine']
        engines_data.hfnm_attr = topology_obj.players['ha']['attributes']

    if "server" in topology_obj.players:
        engines_data.server = topology_obj.players['server']['engine']
    if "sonic-mgmt" in topology_obj.players:
        engines_data.sonic_mgmt = topology_obj.players['sonic-mgmt']['engine']

    if "oob-mgmt-server" in topology_obj.players:
        engines_data.oob_mgmt_server = topology_obj.players['oob-mgmt-server']['engine']
        engines_data.oob_mgmt_server.ip = get_internal_ip_for_oob_server(engines_data.oob_mgmt_server)

    TestToolkit.update_engines(engines_data)
    TestToolkit.update_topology_obj(topology_obj)

    # Enable monkey patching for new engines and wrap existing engines
    command_tracker.enable_monkey_patching()
    command_tracker.wrap_engines_recursively(engines_data)

    return engines_data


@pytest.fixture(scope='session', autouse=True)
def setup_cumulus_sudoers(topology_obj, engines):
    """
    Automatically setup sudoers for cumulus user if the device is running Cumulus Linux.
    This fixture runs once per session and modifies sudoers for all cumulus tests.
    """
    # Check if the device is a Cumulus switch by checking topology attributes
    is_cumulus_device = False
    try:
        # Check if 'is_cumulus' flag is set on the player (set in ngts/conftest.py)
        if topology_obj.players['dut'].get('is_cumulus'):
            is_cumulus_device = True
        else:
            # Fallback: check switch type from topology attributes
            switch_type = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific'].get('TYPE', '')
            from ngts.constants.constants import NvosCliTypes
            if switch_type == NvosCliTypes.CumulusCliType:
                is_cumulus_device = True
    except (KeyError, AttributeError) as e:
        logger.debug(f"Could not determine switch type from topology: {e}")

    if is_cumulus_device:
        try:
            logger.info("Detected Cumulus Linux device. Setting up sudoers for cumulus user...")
            cli_common = CumulusGeneralCli(engines.dut, engines.dut)
            cli_common.modify_sudoers_for_cumulus()
            logger.info("Successfully configured sudoers for cumulus user")
        except Exception as e:
            logger.warning(f"Failed to setup sudoers for cumulus user: {e}")
            # Don't fail the session if this fails, let individual tests handle it


@pytest.fixture(scope='session')
def dut_engines(engines):
    return filter_objects(engines, host_type='dut', engine_type='ssh')


@pytest.fixture(scope='session')
def single_switch(dut_engines):
    """
    Check if setup has only one switch
    """
    return len(dut_engines) == 1


@pytest.fixture(scope='function', autouse=True)
def auto_command_tracking_for_cli_coverage(request):
    """
    Automatically track commands for each test and attach results to Allure.
    This provides command tracking for both CLI coverage and general test insights.
    """
    from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
    import json

    # Start tracking commands for this test
    TestToolkit.start_command_tracking()

    try:
        yield
    finally:
        # Stop tracking and create Allure attachments
        TestToolkit.stop_command_tracking()

        # Get command execution data
        commands = TestToolkit.get_executed_commands()
        stats = TestToolkit.get_command_stats()

        if commands:
            # Create general command tracking report for ALL tests
            command_details = []
            nvue_commands = []

            for i, (cmd, response_time, status) in enumerate(commands, 1):
                cmd_data = {
                    "sequence": i,
                    "command": cmd,
                    "response_time_seconds": round(response_time, 3),
                    "status": status
                }
                command_details.append(cmd_data)

                # Separate NVUE commands for special handling
                if cmd.startswith('nv '):
                    nvue_commands.append(cmd_data)

            # Create summary
            summary = {
                "test_name": request.node.name,
                "total_commands": stats["total_commands"],
                "nvue_commands_count": len(nvue_commands),
                "total_execution_time_seconds": round(stats["total_time"], 3),
                "average_time_per_command_seconds": round(stats["average_time"], 3),
                "slowest_commands": [
                    {
                        "command": cmd,
                        "response_time_seconds": round(time_taken, 3),
                        "status": status
                    }
                    for cmd, time_taken, status in stats["slowest_commands"][:3]
                ]
            }

            # Combine data
            report_data = {
                "summary": summary,
                "command_details": command_details
            }

            # Attach to Allure
            with allure.step("Attach command execution data to Allure report"):
                allure.attach(
                    "Test Command Tracking (JSON)",
                    json.dumps(report_data, indent=2)
                )

                # Create readable summary
                text_summary = f"""
Command Execution Summary for {request.node.name}
{'=' * 50}
Total Commands: {stats["total_commands"]}
NVUE Commands: {len(nvue_commands)}
Total Time: {stats["total_time"]:.3f}s
Average Time: {stats["average_time"]:.3f}s

Top 3 Slowest Commands:
"""
                for i, (cmd, time_taken, status) in enumerate(stats["slowest_commands"][:3], 1):
                    text_summary += f"{i}. {cmd[:60]}{'...' if len(cmd) > 60 else ''} - {time_taken:.3f}s ({status})\n"

                text_summary += f"\nAll Commands (in execution order):\n"
                for i, (cmd, response_time, status) in enumerate(commands, 1):
                    text_summary += f"{i:3d}. [{response_time:6.3f}s] {cmd} ({status})\n"

                allure.attach(
                    "Test Command Summary",
                    text_summary
                )


def get_dut_hostname(engines):
    return engines.dut.run_cmd('hostname')


@pytest.fixture(scope='session')
def dut_hostname(engines):
    return get_dut_hostname(engines)


@pytest.fixture(scope='session')
def dut_ipv6_addr(engines, devices):
    dut_ipv6_addr = IpTool.get_dut_ipv6_addr_of_given_eth_interface_using_nv_cli(devices.dut.cur_mgmt_port_name, engines.dut)
    if not dut_ipv6_addr:
        dut_ipv6_addr = IpTool.get_player_ipv6_addr(engines.dut.ip, engines.dut)
    logging.info(f'dut ipv6 address: {dut_ipv6_addr}')
    return dut_ipv6_addr


@pytest.fixture(scope='session')
def sonic_mgmt_ipv6_addr(engines):
    if hasattr(engines.sonic_mgmt, 'switch_reachable_ip'):
        logging.info(f'sonic_mgmt ipv6 address (from switch_reachable_ip): {engines.sonic_mgmt.switch_reachable_ip}')
        return engines.sonic_mgmt.switch_reachable_ip
    sonic_mgmt_ipv6_addr = IpTool.get_player_ipv6_addr(engines.sonic_mgmt.ip, engines.sonic_mgmt)
    logging.info(f'sonic_mgmt ipv6 address: {sonic_mgmt_ipv6_addr}')
    return sonic_mgmt_ipv6_addr


@pytest.fixture(scope="function", autouse=True)
def uninstall_requests_cache():
    """
    Uninstall requests cache for all tests to prevent interference with OpenAPI calls.
    This is needed because NOGA functions enable global caching that affects all requests.
    """
    try:
        requests_cache.uninstall_cache()
        logger.info("Uninstalled requests cache for session")
    except Exception as e:
        ExceptionTool.log_exception(e, "Failed to uninstall requests cache")


def _apply_mgmt_port(dut_engine, dut_device, chosen_port, chosen_ip, chosen_ssh_port):
    """Apply chosen management port to engine and device."""
    dut_engine.ip = chosen_ip
    dut_engine.ssh_port = chosen_ssh_port
    dut_device.update_mgmt_port(chosen_port, chosen_ip)
    TestToolkit.update_dut_eth0_ip(chosen_ip)


def update_engine_dut_mgmt_port(topology, dut_engine: LinuxSshEngine, dut_device: BaseDevice,
                                player_name: str, is_ipv6_setup: bool = False):
    mgmt_ports = dut_device.get_mgmt_ports()
    if not mgmt_ports:
        raise ValueError(f"Device {type(dut_device).__name__} returned no mgmt ports — "
                         f"get_mgmt_ports() must return a non-empty list")

    specific = topology.players[player_name]['attributes'].noga_query_data['attributes']['Specific']
    if is_ipv6_setup:
        mgmt_ips = [specific.get('ipv6'), specific.get('ipv6_2')]
    else:
        mgmt_ips = [specific.get('ip_address'), specific.get('ip_address_2')]
    mgmt_ssh_ports = [dut_engine.ssh_port, specific.get('ssh_port_2')]

    available = [(ip, port) for ip, port in zip(mgmt_ips, mgmt_ssh_ports) if ip and port is not None]
    if not available:
        raise ValueError(f"No valid mgmt IP/port pairs for {player_name} "
                         f"(ipv6={is_ipv6_setup}, ips={mgmt_ips}, ports={mgmt_ssh_ports})")

    # Default to first available mgmt port
    chosen_port = mgmt_ports[0]
    chosen_ip, chosen_ssh_port = available[0]
    _apply_mgmt_port(dut_engine, dut_device, chosen_port, chosen_ip, chosen_ssh_port)

    if len(mgmt_ports) == 1:
        logger.info('Single mgmt port — dut engine ip: %s:%s', chosen_ip, chosen_ssh_port)
        _attach_mgmt_selection_to_allure(mgmt_ports, None, chosen_port, chosen_ip, chosen_ssh_port)
        return

    if len(available) != len(mgmt_ports):
        logger.info('Mgmt ip count (%d) != port count (%d) — keeping dut engine ip: %s:%s',
                    len(available), len(mgmt_ports), chosen_ip, chosen_ssh_port)
        _attach_mgmt_selection_to_allure(mgmt_ports, mgmt_ips, chosen_port, chosen_ip, chosen_ssh_port)
        return

    # Randomly select a management port for the session
    chosen_port = random.choice(mgmt_ports)
    chosen_ip, chosen_ssh_port = available[mgmt_ports.index(chosen_port)]
    _apply_mgmt_port(dut_engine, dut_device, chosen_port, chosen_ip, chosen_ssh_port)

    logger.info('Available mgmt ports: %s — ips: %s', mgmt_ports, [f'{ip}:{port}' for ip, port in available])
    logger.info('Chosen mgmt port: %s — %s:%s', chosen_port, chosen_ip, chosen_ssh_port)
    _attach_mgmt_selection_to_allure(mgmt_ports, mgmt_ips, chosen_port, chosen_ip, chosen_ssh_port)


def _attach_mgmt_selection_to_allure(available_ports_names, available_ports_ips, chosen_port_name,
                                     chosen_port_ip, chosen_port_ssh_port):
    attachment = (f'All ports: {available_ports_names} - {available_ports_ips}\n'
                  f'Chosen port: {chosen_port_name} - {chosen_port_ip}:{chosen_port_ssh_port}')
    allure.orig_allure.attach(attachment, 'dut_engine_mgmt_port_used_for_session',
                              allure.orig_allure.attachment_type.TEXT)


@pytest.fixture(scope="session")
def mst_device(request, engines):
    return ""


@pytest.fixture(scope='session')
def original_version(engines):
    version = System().version.get_nvos_image_version()
    return version


@pytest.fixture(scope='session', autouse=True)
def devices(topology_obj):
    devices = DeviceFactory.create_devices_object(topology_obj)
    TestToolkit.update_devices(devices)
    return devices


@pytest.fixture(scope='session', autouse=True)
def configure_opensm_multiplanar(devices):
    """
    Propagate the DUT's multi_planar attribute to OpenSmTool so that callers which
    do not explicitly pass `multiplanar=...` to start_open_sm still pick the
    multiplanar-capable binary (OPEN_SM_PATH) on Black Mamba / Taipan setups.
    """
    OpenSmTool.set_multi_planar(getattr(devices.dut, 'multi_planar', False))


@pytest.fixture(scope='session', autouse=True)
def update_open_api_port(topology_obj, devices, engines):
    """
    Update OpenAPI port for all DUTs in the topology.

    :param topology_obj: Topology object containing player information
    :param devices: Device objects for all DUTs
    :param engines: Engine objects for all DUTs
    """
    # Update OpenAPI port for each DUT player in topology
    for player_name, player in filter_objects(topology_obj.players, host_type='dut', engine_type='ssh').items():
        player_attrs = player['attributes']
        topology_conn = player_attrs.noga_query_data['attributes']['Topology Conn.']
        device = devices[player_name]
        open_api_port = topology_conn.get('OPEN_API_PORT', device.open_api_port)
        device.open_api_port = open_api_port
        engines[player_name].open_api_port = open_api_port
        # backwards compatibility, if instance has no attr like this, will retrieve from class level attr.
        ProxySshEngine.open_api_port = open_api_port


@pytest.fixture
def traffic_available(request):
    """
    True is traffic functionality is available for current setup
    :param request: pytest builtin
    :return: True/False
    """
    return bool(request.config.getoption('--traffic_available'))


@pytest.fixture(scope='function')
def serial_engine(topology_obj, devices):
    """
    :return: serial connection
    """
    return ConnectionTool.create_serial_connection(topology_obj, devices)


@pytest.fixture
def tst_all_pwh_confs(request):
    """
    True to test functionality of all password hardening configurations;
        False otherwise (only several random configurations will be picked to testing)
    :param request: pytest builtin
    :return: True/False
    """
    param_val = request.config.getoption('--tst_all_pwh_confs')
    return True if param_val == 'True' else False


@pytest.fixture
def start_sm(engines, devices, traffic_available):
    """
    Starts OpenSM
    """
    if traffic_available:
        RegressionConfigurations.configure_ports_to_legacy(engine=engines.dut, apply=True, throw_exception=False)
        result = OpenSmTool.start_open_sm(engines, multiplanar=devices.dut.multi_planar)
        if result is not None:
            result.ignore_result()
        if result is None or not result.result:
            with allure.step('open_sm failed to start (possibly due to #4088479), attempting to recover'):
                with allure.step('Rebooting all traffic VMs'):
                    executor = concurrent.futures.ThreadPoolExecutor()
                    tasks = []
                    if hasattr(engines, 'ha'):
                        tasks.append(executor.submit(engines.ha.reload, ['sudo reboot']))
                    if hasattr(engines, 'hb'):
                        tasks.append(executor.submit(engines.hb.reload, ['sudo reboot']))
                    if hasattr(engines, 'hfnm') and (not hasattr(engines, 'ha') or engines.ha.ip != engines.hfnm.ip):
                        tasks.append(executor.submit(engines.hfnm.reload, ['sudo reboot']))
                    for task in tasks:
                        try:
                            task.result(timeout=300)
                        except Exception:
                            ExceptionTool.log_traceback()
                            raise Exception('Failed to reboot traffic VMs, see traceback in logs')
                    time.sleep(5)

                with allure.step('Retrying to start open_sm'):
                    OpenSmTool.start_open_sm(engines, multiplanar=devices.dut.multi_planar).verify_result()
    else:
        raise SetupIssue("Traffic is not available on this setup")


@pytest.fixture
def stop_sm(engines, devices):
    """
    Stops OpenSM for the duration of the test, then restarts it after.
    """
    result = OpenSmTool.stop_open_sm(engines)
    if result is None or not result.result:
        logging.warning(f"Failed to stop openSM: {result.info if result else 'No result returned'}")

    yield  # Test runs here with SM stopped

    # Cleanup: restart OpenSM after test completes
    logging.info("Restarting OpenSM after test (stop_sm fixture cleanup)")
    restart_result = OpenSmTool.start_open_sm(engines, multiplanar=devices.dut.multi_planar)
    if restart_result is None or not restart_result.result:
        logging.error(f"Failed to restart OpenSM in stop_sm fixture cleanup: "
                      f"{restart_result.info if restart_result else 'No result returned'}")


@pytest.fixture(scope="session")
def release_name(request):
    """
    Method for getting release_name from pytest arguments
    :param request: pytest builtin
    :return: release_name
    """
    return request.config.getoption('--release_name')


@pytest.fixture(scope='function', autouse=True)
def branch_name(request):
    """
    Fixture that extracts branch name from --remote_test_path and sets it to TestToolkit.branch
    :param request: pytest builtin
    :return: branch_name
    """
    TestToolkit.branch = ""
    remote_test_path = request.config.getoption('--remote_test_path')
    logger.info(f"remote_test_path: {remote_test_path}")

    if remote_test_path:
        raw_branch = remote_test_path.split('/')[0]
        logger.info(f"raw_branch from remote_test_path: {raw_branch}")

        # Handle different branch name patterns:
        # 1. SONIC_CANONICAL-sonic-mgmt_develop.db.1 → develop
        # 2. SONIC_CANONICAL-sonic-mgmt_nvos_ver-25-02-5000.db → 25.02.5000

        if 'sonic-mgmt_' in raw_branch:
            # Extract everything after sonic-mgmt_
            after_prefix = raw_branch.split('sonic-mgmt_')[1]

            if after_prefix.startswith('develop'):
                branch_name = 'develop'
            elif after_prefix.startswith('nvos_ver-'):
                # Extract version like 25-02-5000 and convert to 25.02.5000
                version_part = after_prefix.replace('nvos_ver-', '').split('.')[0]  # "25-02-5000"
                branch_name = version_part.replace('-', '.')  # "25.02.5000"
            else:
                # Fallback: take everything before first dot
                branch_name = after_prefix.split('.')[0]
        else:
            # Fallback: use the raw branch name
            branch_name = raw_branch

        TestToolkit.branch = branch_name
        logger.info(f"branch is: {TestToolkit.branch}")

    return TestToolkit.branch


@pytest.fixture(scope='session')
def force_external_ips_in_air(pytestconfig, is_mars_run, is_ci_run):
    """Override for NVOS: automatically force external IPs for local (non-MARS, non-CI) runs.
    The --force_air_external_ips flag can still be used to force it explicitly."""
    force_external = pytestconfig.getoption('--force_air_external_ips', default=False)
    if not force_external and not is_mars_run and not is_ci_run:
        logger.info('Local NVOS run detected — automatically forcing external AIR IPs')
        force_external = True
    os.environ[NvidiaAirConstants.EXTERNAL_CONNECTION_MODE_ENV_VAR] = str(force_external).lower()


@pytest.fixture(scope='session', autouse=True)
def api_type():
    logger.info('updating API type to: ' + ApiType.NVUE)
    TestToolkit.update_apis(ApiType.NVUE)


@pytest.fixture(scope='session')
def cli_objects(topology_obj):
    cli_obj_data = DottedDict()
    cli_obj_data.dut = topology_obj.players['dut']['cli']
    if "ha" in topology_obj.players:
        cli_obj_data.ha = topology_obj.players['ha']['cli']
    if "hb" in topology_obj.players:
        cli_obj_data.hb = topology_obj.players['hb']['cli']
    return cli_obj_data


def check_switch_capacity(engine):
    try:
        logger.info("Check used capacity for /var/lib/python/coverage")
        engine.run_cmd("df -h /var/lib/python/coverage/")
        engine.run_cmd("du -h /var/lib/python/coverage")
        engine.run_cmd("du -h /sonic")
    except BaseException as ex:
        logger.warning(str(ex))


@pytest.fixture(scope='session')
def interfaces(topology_obj):
    interfaces_data = DottedDict()
    interfaces_data.ha_dut_1 = topology_obj.ports['ha-dut-1']
    interfaces_data.hb_dut_1 = topology_obj.ports['hb-dut-1']
    return interfaces_data


def clear_security_config(item):
    with allure.step("Clear security config"):
        TestToolkit.update_apis(ApiType.NVUE)

        try:
            local_dut_engine: ProxySshEngine = TestToolkit.engines.dut
            try:
                active_aaa_server = item.active_remote_aaa_server

                logging.info('Test configured aaa authentication. find remote admin user to use')
                remote_admin = [user for user in active_aaa_server.users if user.role == 'admin'][0]
                logging.info(f'Create engine with remote user: {remote_admin.username}')
                remote_admin_engine = ProxySshEngine(device_type=TestToolkit.get_engine().device_type,
                                                     ip=TestToolkit.get_engine().ip,
                                                     username=remote_admin.username,
                                                     password=remote_admin.password)

                logging.info('Clear authentication settings to allow local admin user engine continue')
                res = System().aaa.authentication.unset(op_param='order', apply=True, dut_engine=remote_admin_engine)
                assert 'verifyingreadying' in res.info, f'Expected to have "{"verifyingreadying"}" ' \
                    f'in output. Actual output: {res.info}'
            finally:
                item.active_remote_aaa_server = None
                wait_for_ldap_nvued_restart_workaround(item, engine_to_use=local_dut_engine)
        except Exception:
            local_dut_engine.disconnect()
            wait_for_ldap_nvued_restart_workaround(item, engine_to_use=local_dut_engine)

        # if isinstance(active_aaa_server, LdapServerInfo):
        #     logging.info('Remove LDAP users home directories')
        #     remote_usernames = [user.username for user in active_aaa_server.users]
        #     for username in remote_usernames:
        #         TestToolKit.get_engine().run_cmd(f'sudo rm -rf /home/{username}')


@pytest.fixture(scope="session")
def root_dir(request):
    return request.config.rootdir


@pytest.fixture(scope="session")
def default_config_yml_path(engines, devices, root_dir):
    return devices.dut.get_default_config_yml(engines.dut, root_dir)


def pytest_exception_interact(report):
    logging.error(f'----------- The test failed - an exception occurred: ----------- \n{report.longreprtext}')
    if TestToolkit.devices is not None:
        for dev_name, device in filter_objects(TestToolkit.devices, host_type='dut', engine_type='ssh').items():
            engine = TestToolkit.get_engine(dev_name)
            device.handle_exception(engine)


@pytest.fixture(scope="function")
def run_cli_coverage_flow(clear_config, request):
    yield

    try:
        item = request.node
        logging.info('------- Running CLI coverage -------')
        run_cli_coverage(item, item.keywords)
    except BaseException as err:
        logging.warning(f"CLI coverage flow failed- {err}")


def eth_handle_exception():
    logging.info("Handle eth exception")


@pytest.fixture(scope="function", autouse=True)
def list_of_executed_commands(engines, run_cli_coverage_flow, request):
    pytest.s_time = time.time()
    logging.info(f'------- TEST STARTED - {request.node.name} -------')
    if 'no_log_test_wrapper' not in request.keywords:
        try:
            SendCommandTool.execute_command(LinuxGeneralCli(engines.dut).clear_history)
        except BaseException as exc:
            logger.info(f"'history -c' failed - {exc}")

    yield

    try:
        with allure.step("List of executed commands"):
            commands_list = SendCommandTool.execute_command(
                LinuxGeneralCli(engines.dut).get_history).get_returned_value()
            allure.attach("List of commands", commands_list)

        with allure.step(f"Save list of commands to {SystemConsts.LIST_OF_COMMANDS_FILE_PATH}"):
            file_path = SystemConsts.LIST_OF_COMMANDS_FILE_PATH
            engines.dut.run_cmd(
                f"history | sudo tee {file_path} > /dev/null && "
                f"sudo sed -i '$d' {file_path} && "
                f"sudo sed -i 's/^ *//' {file_path}"
            )

        # Copy the commands file to local for bug handler access
        with allure.step("Copy commands file to local host"):
            from pathlib import Path

            # Create a local path for the commands file
            local_commands_dir = Path("/tmp/executed_commands")
            local_commands_dir.mkdir(exist_ok=True)
            local_file_path = local_commands_dir / "executed_commands.txt"

            # Copy from remote to local
            scp_file(engines.dut, file_path, str(local_file_path), download_from_remote=True)

    except BaseException as err:
        logging.warning(f"Failed to get list of executed commands - {err}")


@pytest.fixture(scope="function")
def clear_config(request, devices, engines, default_config_yml_path, root_dir, skip_clear_config, markers=None):
    yield

    TestToolkit.tested_api = ApiType.NVUE
    test_result = request.node.rep_call.outcome if hasattr(request.node, 'rep_call') else request.node.rep_setup.outcome
    logging.info(f"------- Test '{request.node.name}' {test_result} -------")

    try:
        should_skip = skip_clear_config or test_result == TestConsts.SKIPPED or is_cur_test_has_marker(request, 'skip_clear_config')
        if not should_skip:
            with allure.step(f"Clear config for test {request.node.name}"):
                """ if hasattr(item, 'active_remote_aaa_server') and item.active_remote_aaa_server:
                     clear_security_config(item)
                if hasattr(item, 'security_pexpect_ssh_session') and item.security_pexpect_ssh_session:
                    security_cleanup(item.security_pexpect_ssh_session)"""
                devices.dut.clear_config(engines.dut, markers, default_config_yml_path, root_dir)
        else:
            logging.info("skipping clear_config functionality")
    except Exception as err:
        logging.warning("Failed to clear config:" + str(err))
    finally:
        logging.info('Clear global OpenApi changeset and payload')
        OpenApiRequest.clear_changeset_and_payload()
        OpenApiRequest.update_client_certs_info(None)


@pytest.fixture(scope='function', autouse=True)
def teardown_collect_code_coverage(topology_obj, engines):
    yield
    if pytest.is_code_coverage:
        collect_coverage = False

        with allure.step("Check coverage folder capacity"):
            cli_obj = topology_obj.players['dut']['cli']
            try:
                capacity_percentage = DiskTool.get_path_available_capacity_percentage(engines.dut,
                                                                                      NvosConst.COVERAGE_PATH)
                logging.info(f"Coverage folder capacity: {capacity_percentage}%")
                collect_coverage = int(capacity_percentage) >= NvosConst.MAX_COVERAGE_PATH_CAPACITY_PERCENTAGE
            except BaseException:
                collect_coverage = True
                cli_obj.general.coverage_combine()

        if collect_coverage:
            with allure.step(f"Collect python coverage (folder capacity {capacity_percentage}%"):
                extract_python_coverage_for_nvos(dest=NvosConsts.DEST_PATH, engines=engines, cli_obj=cli_obj,
                                                 topology_obj=topology_obj)


@pytest.fixture(scope='function', autouse=True)
def debug_kernel_check(engines, test_name, setup_name, session_id):
    yield
    if pytest.is_debug_kernel:
        engines.dut.run_cmd("sudo dmesg | grep {}".format(DebugKernelConsts.KMEMLEAK))
        engines.dut.run_cmd("sudo echo scan | sudo tee {}".format(DebugKernelConsts.KMEMLEAK_PATH))
        mem_leaks_output = engines.dut.run_cmd("sudo cat {}".format(DebugKernelConsts.KMEMLEAK_PATH))
        if mem_leaks_output:
            logger.info("kernel memory leaks were found, will send mail with the leaks")
            context = f"Kernel memory leaks were found during test:{test_name}\n" \
                f"Setup: {setup_name}\n" \
                f"Session ID: {session_id}\n" \
                f"{mem_leaks_output}"
            try:
                s = smtplib.SMTP(InfraConst.NVIDIA_MAIL_SERVER)
                email_contents = MIMEText(context)
                email_contents['Subject'] = "debug kernel issue nvos"
                email_contents['To'] = "ncaro-org@exchange.nvidia.com"
                s.sendmail('noreply@debugkernel.com', email_contents['To'], email_contents.as_string())
                logger.info("Mail was sent to: {}".format(email_contents['To']))
            finally:
                s.quit()

            engines.dut.run_cmd("sudo echo clear | sudo tee {}".format(DebugKernelConsts.KMEMLEAK_PATH))


@pytest.fixture(autouse=True)
def skip_coredump_check(request):
    """
    Method for getting skip_coredump_check from pytest arguments
    :param request: pytest builtin
    """
    pytest.skip_coredump_check = request.config.getoption('--skip_coredump_check')


def _parse_coredump_ls_output(raw: str) -> list:
    """Drop bash prompt noise that mlx shells append after ``ls`` (e.g. cen* permission errors)."""
    names = []
    for line in (raw or '').splitlines():
        line = line.strip()
        if not line:
            continue
        if (
            line.startswith('sudo ') or
            line.startswith('-bash:') or
            line.startswith('rm:') or
            'cumulus@' in line or
            'Permission denied' in line or
            ':' in line
        ):
            continue
        names.append(line)
    return names


@pytest.fixture(scope='function', autouse=True)
def coredump_check(engines, test_name, setup_name, dumps_folder, session_id):
    yield
    if pytest.skip_coredump_check:
        logger.info('NVOS: Skip coredump check')
        return
    else:
        files = _parse_coredump_ls_output(
            engines.dut.run_cmd('sudo ls -1 %s 2>/dev/null' % (CoreDumpConsts.COREDUMP_PATH,), validate=False)
        )

        if not files:
            logger.info(f'No core dumps found in {pytest.test_name}')
        else:
            for file in files:
                file_path = os.path.join(CoreDumpConsts.COREDUMP_PATH, file)
                logger.info('Copy dump {} to log folder {}'.format(file_path, dumps_folder))
                dest_file = dumps_folder + '/' + file
                scp_file(engines.dut, file_path, dest_file, download_from_remote=True)
                os.chmod(dest_file, 0o655)
                logger.info('Dump file location: {}'.format(dest_file))
                logger.info('Delete coredump {} from the switch'.format(file_path))
                engines.dut.run_cmd(f"sudo rm -f {file_path}")
                logger.info("Core dump were found, will send mail with the leaks")
                context = f"Core dump were found during test:{test_name}\n" \
                    f"Setup: {setup_name}\n" \
                    f"Session ID: {session_id}\n" \
                    f"Test: {pytest.test_name}\n" \
                    f"Core dump file location: {dest_file}"
                try:
                    s = smtplib.SMTP(InfraConst.NVIDIA_MAIL_SERVER)
                    email_contents = MIMEText(context)
                    email_contents['Subject'] = "Core dump issue NVOS"
                    email_contents['To'] = ", ".join(['sviatoslavd@nvidia.com', 'ncaro-org@exchange.nvidia.com',
                                                      'yport@nvidia.com', 'nadeemn@nvidia.com'])
                    s.sendmail('noreply@nvidia.com', email_contents['To'], email_contents.as_string())
                    logger.info("Mail was sent to: {}".format(email_contents['To']))
                finally:
                    s.quit()
            pytest.fail(f"Coredump found and uploaded to {dest_file}")


_OPERATION_TIME_COLUMNS = (
    OperationTimeConsts.OPERATION_COL,
    OperationTimeConsts.PARAMS_COL,
    OperationTimeConsts.DURATION_COL,
    OperationTimeConsts.SETUP_COL,
    OperationTimeConsts.TYPE_COL,
    OperationTimeConsts.VERSION_COL,
    OperationTimeConsts.RELEASE_COL,
    OperationTimeConsts.SESSION_ID_COL,
    OperationTimeConsts.TEST_NAME_COL,
    OperationTimeConsts.DATE_COL,
)


@pytest.fixture(scope="session", autouse=True)
def insert_operation_time_to_db(setup_name, session_id, platform_params, topology_obj):
    """
    Collect per-test operation durations during the session, then INSERT them into
    the MSSQL operation_time table at session end (PowerBI feeds off this table).
    Tests append entries to pytest.operation_list via OperationTime.save_duration().
    """
    pytest.operation_list = []
    yield

    if not pytest.operation_list:
        logger.info("operation_time: no entries collected this session; nothing to upload")
        return

    with allure.step("Upload operation_time entries to MSSQL"):
        try:
            machine_type = platform_params['filtered_platform']
            with allure.step("Resolve image version and release name"):
                version = System().version.get_nvos_image_version()
                release_name = TestToolkit.version_to_release(version)
                logger.info("operation_time: version=%s release=%s entries=%d",
                            version, release_name, len(pytest.operation_list))

            skip_reason = _operation_time_skip_reason(release_name)
            if skip_reason:
                logger.info("operation_time: skipping upload (%s)", skip_reason)
                allure.attach("operation_time skip_reason", skip_reason)
                return

            insert_operation_duration_to_db(setup_name, machine_type, version, session_id, release_name)
        except Exception as err:
            logger.exception("operation_time: failed to save duration data: %s", err)
            raise


def _operation_time_skip_reason(release_name):
    """Return a human-readable reason the operation_time upload should be skipped, or '' to proceed."""
    if TestToolkit.is_special_run():
        return "special run (sanitizer/code-coverage/debug-kernel)"
    if not pytest.is_mars_run:
        return "not a MARS run"
    if pytest.is_ci_run:
        return "CI run"
    if not release_name:
        return "image version is not a release"
    return ""


@retry(Exception, tries=3, delay=3)
def insert_operation_duration_to_db(setup_name, machine_type, version, session_id, release_name):
    operations = pytest.operation_list
    today = datetime.date.today()
    columns = f"({', '.join(_OPERATION_TIME_COLUMNS)})"
    placeholders = "(" + ", ".join(["?"] * len(_OPERATION_TIME_COLUMNS)) + ")"
    query = f"INSERT operation_time {columns} values {placeholders}"
    rows = [
        (
            op[OperationTimeConsts.OPERATION_COL],
            op[OperationTimeConsts.PARAMS_COL],
            op[OperationTimeConsts.DURATION_COL],
            setup_name,
            machine_type,
            version,
            release_name,
            session_id,
            op[OperationTimeConsts.TEST_NAME_COL],
            today,
        )
        for op in operations
    ]

    connections_params = DbConstants.CREDENTIALS[CliType.NVUE]
    with allure.step(f"Connect to MSSQL ({connections_params['database']})"):
        mssql_connection_obj = ConnectMSSQL(**connections_params)
        mssql_connection_obj.connect_db()

    try:
        with allure.step(f"INSERT {len(operations)} rows into operation_time"):
            logger.info("operation_time: inserting %d entries (setup=%s session=%s release=%s)",
                        len(operations), setup_name, session_id, release_name)
            mssql_connection_obj.query_insert_many(query, rows)
            logger.info("operation_time: insert successful")
    finally:
        mssql_connection_obj.disconnect_db()


@pytest.fixture(autouse=True)
def disable_cli_coverage(request):
    """
    Method for getting disable_cli_coverage from pytest arguments
    :param request: pytest builtin
    """
    pytest.disable_cli_coverage = request.config.getoption('--disable_cli_coverage')


@pytest.fixture(autouse=True)
def skip_clear_config(request):
    """
    Method for getting skip_clear_config from pytest arguments
    :param request: pytest builtin
    """
    return request.config.getoption('--skip_clear_config')


def _api_type_for_cli_coverage(item):
    """
    API type used for this test run (NVUE vs OpenAPI).
    Use parametrization (test_api / random_api) when present. Relying only on
    TestToolkit.tested_api during teardown is unsafe: clear_config's finalizer sets it
    back to NVUE and may run before run_cli_coverage_flow, so OpenAPI cases could
    incorrectly trigger NVUE CLI coverage.
    """
    callspec = getattr(item, 'callspec', None)
    if callspec is not None:
        params = getattr(callspec, 'params', None) or {}
        if 'test_api' in params:
            return params['test_api']
        if 'random_api' in params:
            return params['random_api']
    return TestToolkit.tested_api


def run_cli_coverage(item, markers):
    try:
        api_type = _api_type_for_cli_coverage(item)
    except Exception as e:
        logging.warning(
            'Could not resolve API type from parametrization for CLI coverage (%s); skipping CLI coverage for this test', e)
        return

    if api_type == ApiType.NVUE and \
            'no_cli_coverage_run' not in markers and \
            not pytest.is_sanitizer and \
            pytest.is_mars_run and \
            not pytest.disable_cli_coverage and \
            not pytest.is_air:
        logging.info("API type is NVUE and is it not a sanitizer version, so CLI coverage script will run")
        NVUECliCoverage.run(item=item, start_time=pytest.s_time,
                            project=TestToolkit.devices.dut.cli_coverage_project_name, department='verification',
                            nvue_dir=TestToolkit.devices.dut.cli_coverage_path)
    else:
        logging.debug('Skipping CLI coverage: test API is %s', api_type)


@pytest.fixture(autouse=True)
def security_post_checker(request):
    """
    Method for getting security_post_checker from pytest arguments
    :param request: pytest builtin
    """
    if request.config.getoption("--security_post_checker"):
        logger.info('Security Post Checker')
        return True
    else:
        return False


@pytest.fixture(scope='session', autouse=True)
def store_and_manage_loganalyzer(request):
    ignore_failure = request.config.getoption("--ignore_la_failure")
    store_la_logs = request.config.getoption("--store_la_logs")
    if not ignore_failure:
        request.config.option.ignore_la_failure = True
    if not store_la_logs:
        request.config.option.store_la_logs = True


@pytest.fixture(scope='function', autouse=True)
def extend_log_analyzer_match_regex(loganalyzer):
    """
    Extend the loganalyzer match_regex list and ignore_regex list.
    """
    if loganalyzer:
        simplex_bidi_pattern = r"(?i:(NVL_(?:SIMPLEX|BIDI)\w*|\b(?:simplx|simplex|bidir|bidi|bidirectional)\b))"
        nvme_timeout_patterns = [
            r"(?i:(nvme\s+nvme\d+:\s*I/O\s+\d+\s+QID\s+\d+\s+timeout,\s*aborting))",
            r"(?i:(Device not ready; aborting reset, CSTS=0x1))",
            r"(?i:(I/O\s+\d+\s+QID\s+\d+\s+timeout,\s*reset controller))",
        ]
        for hostname in loganalyzer.keys():
            loganalyzer[hostname].ignore_regex.extend(list(pytest.dynamic_ignore_set))
            loganalyzer[hostname].match_regex.extend([
                "\\.*\\s+WARNING\\s+\\.*",
                "\\.*\\s+segfault\\s+\\.*",
                simplex_bidi_pattern,
                *nvme_timeout_patterns,
            ])


@pytest.fixture(scope='session', autouse=True)
def disable_loganalyzer_rotate_logs(request):
    request.config.option.loganalyzer_rotate_logs = False


@pytest.fixture(scope='function', autouse=True)
def initialize_testtoolkit_loganalyzer(loganalyzer):
    TestToolkit.loganalyzer_duts = loganalyzer


@pytest.fixture
def prepare_traffic(engines, setup_name):
    """
    - Bring up traffic containers in case are in down state.
    - Starts OpenSM
    """
    with allure.step('Prepare traffic containers...'):
        TrafficGeneratorTool.bring_up_traffic_containers(engines, setup_name)


@pytest.fixture
def output_format(test_api):
    return OutputFormat.auto if test_api == ApiType.NVUE else OutputFormat.json


@pytest.fixture(scope='session')
def target_version_realpath(target_version):
    assert target_version is not None, "No target image is specified"
    cmd_runner = CmdRunner()
    with allure.step('get real full path of target version'):
        target_version_path = cmd_runner.run_cmd(f'realpath {target_version}')
        logging.info(f'target version path: {target_version_path}')
    return target_version_path


@pytest.fixture(scope='session')
def base_version_realpath(base_version):
    assert base_version is not None, "No base image is specified"
    cmd_runner = CmdRunner()
    with allure.step('get real full path of target version'):
        base_version_path = cmd_runner.run_cmd(f'realpath {base_version}')
        logging.info(f'base version path: {base_version_path}')
    return base_version_path


@pytest.fixture(scope='session')
def downgrade_version_realpath(downgrade_version, base_version):
    version = downgrade_version or base_version
    if not version:
        raise SetupIssue('Must specify downgrade_version or base_version in command-line')
    cmd_runner = CmdRunner()
    with allure.step('get real full path of version'):
        version_path = cmd_runner.run_cmd(f'realpath {version}')
        logging.info(f'{version_path=}')
    return version_path


@pytest.fixture
def test_api(request):
    """This fixture runs the test twice (once for each api)."""
    if hasattr(request, 'param'):
        TestToolkit.tested_api = request.param
    return TestToolkit.tested_api


def pytest_generate_tests(metafunc: pytest.Metafunc):
    """
    This hook is called for every test function collected.
    It dynamically parametrizes any test that requests the `random_api` fixture.
    """
    if random_api.__name__ in metafunc.fixturenames:
        is_collecting = metafunc.config.getoption("--collect-only")
        test_name = metafunc.function.__name__
        fixed_api = _resolve_fixed_random_api(metafunc.config)

        if fixed_api is not None:
            param_list = [fixed_api]
            logging.warning(f"  -> random_api pinned to: {fixed_api} (--fixed-random-api / NVOS_FIXED_RANDOM_API)")
        elif is_collecting:
            param_list = ApiType.ALL_TYPES
            logging.warning(f"  -> [{test_name}] COLLECT MODE: Parametrizing with all values: {param_list}")
        else:
            random_api_choice = random.choice(ApiType.ALL_TYPES)
            logging.warning(f"  -> [{test_name}] Test run Selected API: {random_api_choice}")
            param_list = [random_api_choice]

        metafunc.parametrize('random_api', param_list, indirect=True)


@pytest.fixture()
def random_api(request: pytest.FixtureRequest):
    """Causes the test to run on a randomly-chosen API. The fixture also returns the name of the used API."""
    TestToolkit.tested_api = request.param
    return request.param


@pytest.fixture(scope='module')
def nv_command() -> NvCommand:
    return NvCommand()


@pytest.fixture(autouse=True)
def verify_result_objects():
    yield
    errors = [obj._get_fail_message() for obj in ResultObj._pop_all_instances() if not obj.result]
    if errors:
        raise Exception(f'There are {len(errors)} ResultObj instances that contain a failed result (see documentation '
                        f'of ResultObj class):\n\n' + ('\n' + '-' * 80 + '\n\n').join(errors))


@pytest.fixture(scope='session', autouse=True)
def update_fw_versions_json_file(fw_versions_json_file):
    logging.info(f'fw_versions_json_file path: {fw_versions_json_file}')
    BmcTool.set_fw_versions_json_file(fw_versions_json_file)
    return fw_versions_json_file


@pytest.fixture
def handle_la_marker_in_manufacture(engines, loganalyzer):
    """
    When the test ends, injects the log-analyzer test-start marker as the first line in the log.
    This is intended for tests that cause all log files to be deleted, e.g. by manufacture or factory-reset.
    This fixture calls the 'loganalyzer' fixture just to ensure that these 2 fixtures run in the correct order.
    """
    try:
        marker = engines.dut.run_cmd(r"grep -oE '\S+ \S+ start-LogAnalyzer-.*' " + SyslogConsts.SYSLOG_LOG_PATH,
                                     validate=True).splitlines()[-1]
        marker_find_exception = None
    except BaseException as e:
        marker_find_exception = e
        logger.warning("Failed to find LA start marker. LA will fail after the test finishes.")
    yield

    if marker_find_exception:
        raise marker_find_exception
    oldest_syslog_id = get_oldest_syslog_id(engines.dut)
    new_marker = get_new_start_string(engines.dut, oldest_syslog_id, marker)
    insert_new_start_string(engines.dut, oldest_syslog_id, new_marker)


def _validate_matrix_arg(matrix_arg: str) -> Optional[Dict]:
    """
    Validate matrix argument.
    If the argument is a path to a json file, return the json object.
    If the argument is a json string, return the json object.
    If the argument is not a valid json string, raise a ValueError.
    """
    if matrix_arg.endswith('.json'):
        path = Path(matrix_arg)
        if not path.exists():
            return

        with path.open() as f:
            matrix_arg = f.read()

    try:
        return json.loads(matrix_arg)
    except json.JSONDecodeError as e:
        raise ArgumentTypeError(f"Invalid JSON string for '{path}': {e}\n{matrix_arg}")


@pytest.fixture(scope='session')
def provisioning(engines: EnginesT) -> str:
    """ returns whether the system is dev or prod """
    return DEVELOPMENT if SecureBootTool.is_dev_system(engines.dut) else PRODUCTION


@pytest.fixture(scope='function')
def disable_els_init_state_for_taipan(engines, devices, nv_command):
    """
    Fixture to disable ELS init state before test and re-enable it after test.
    This fixture is used for Taipan devices only.
    """
    if devices.dut.switch_class != NvosConst.TAIPAN_SWITCH:
        yield
        return

    with allure.step("Disable ELS init state"):
        nv_command.fae.system.cpo.set(CpoConsts.ELS_INITIALIZATION_STATE, CpoConsts.State.DISABLED.value, apply=True).verify_result()
        NvueGeneralCli.save_config(engines.dut)

    yield

    with allure.step("Re-enable ELS init state"):
        nv_command.fae.system.cpo.set(CpoConsts.ELS_INITIALIZATION_STATE, CpoConsts.State.ENABLED.value, apply=True).verify_result()
        NvueGeneralCli.save_config(engines.dut)


@pytest.fixture(scope='session')
def ib_router(is_ib_router, engines):
    """
    Method for get ib_router value from pytest arguments and change profile on the switch if needed
    :param request: pytest builtin
    :return: True or False, if run is ib_router type
    """
    if is_ib_router:
        IbRouterTool.enable_ib_router_profile()
        IbRouterTool.configure_leaf_port_mapping(engines)
    return is_ib_router


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """
    - After the test *call* phase: store the real test outcome on the item.
    - After the *teardown* phase: if the test body passed but teardown failed
      due to loganalyzer, add a marker and (optionally) an Allure tag.

    All the logic is done *after* `yield` so we always work with TestReport.
    """

    # Let inner hooks (including Allure & others) run first
    outcome = yield
    result: pytest.TestReport = outcome.get_result()

    # ----- CALL PHASE: remember the real test outcome -----
    if call.when == "call":
        # result contains the real test outcome such as passed, failed, skipped, etc.
        logger.info(f"[###] Call phase: remember the real test outcome {result=}")
        item._test_call_result = result

    # ----- TEARDOWN PHASE: detect LA failure after a passed test -----
    elif call.when == "teardown":
        if not (call_result := getattr(item, "_test_call_result", None)):
            return

        logger.info(f"[###] Teardown phase: the test outcome {call_result=}")
        logger.info(f"[###] Teardown phase: the test NEW outcome {result=}")

        # Only care about: test body passed && teardown failed
        if call_result.passed and result.failed:
            # Robustly get longrepr text
            longrepr_text = getattr(result, "longreprtext", str(result.longrepr))
            logger.info(f"[###] Teardown phase: the longrepr text {longrepr_text=!r}")

            # if loganalyzer failed, we would have the string "/loganalyzer/" in the longrepr text
            if "/loganalyzer/" in longrepr_text:
                la_failed_marker = f"la_failed(outcome={call_result.outcome})"

                # 1) Add pytest marker so other plugins (e.g. ReportPortal) can see it
                item.add_marker(la_failed_marker)
                # 2) Tell Allure directly – this does NOT depend on marker collection
                allure.dynamic.tag(la_failed_marker)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    # NVOS Hub: PATCH each queued failure with the final Allure URL once the
    # upload completes. The autouse fixture is imported at module top.
    _nvos_hub.terminal_summary_impl(config)
