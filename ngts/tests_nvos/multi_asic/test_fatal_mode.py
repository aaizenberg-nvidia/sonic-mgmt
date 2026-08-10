import contextlib
import logging
import os
import re
import threading
import time
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from typing import Union

import pytest
from retry.api import retry_call

from ngts.nvos_constants.constants_nvos import ApiType, HealthConsts, NvosConst, ActionConsts, SystemConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.infra import ExceptionTool
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool, RebootParams, wait_until_cli_is_up, wait_on_systemctl_initialization
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.FilesTool import EngineFile
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.system.clock.ClockConsts import ClockConsts
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
# Reuse the canonical ASIC_HEALTH fatal-state helper from test_system_health.py so we
# don't carry a second copy that can silently diverge (Harel review #698). The helper
# takes ``engines`` there; we adapt via SimpleNamespace in _set_asic_health_fatal_state below.
from ngts.tests_nvos.system.test_system_health import (
    _set_asic_health_fatal_state as _shared_set_asic_health_fatal_state,
)
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.tests_nvos.multi_asic.fatal_dump_rotation_helpers import (
    FATAL_DUMP_ROTATION_CYCLES,
    assert_fatal_dump_rotation_inventory,
    cleanup_fatal_dump_tech_support,
    configure_fatal_dump_rotation_settings,
    prepare_fatal_dump_rotation_baseline,
    run_fatal_dump_rotation_cycle,
    verify_system_healthy_after_fatal_cycles,
    verify_tech_support_after_fatal_cycles,
)

logger = logging.getLogger()
fatal_event_timestamps = []

SETTINGS = {"clear_time": 2}
NUM_TRIES_TO_RECOVER_AT_TEARDOWN = 4
SAI_LOG_STRING = 'Health-Check: new failure'
SAI_LOG_TIME_WINDOW_SECONDS = 5
WAIT_BETWEEN_EVENTS_SECONDS = 20

FATAL_PROMPT = "[System_Fatal_State]"
FATAL_FILE = "/etc/system_fatal"
FATAL_REASON_FILE = "/host/fatal_reason"
FATAL_REASON_DUMP_FILE = "/dump/fatal_reason"
FATAL_REASON_FILE_CONTENT = 'Fatal health event'
FATAL_LOG_FILE = '/var/log/fatal.log'
FATAL_LOG_FILE_IN_DUMP = '/log/fatal.log.gz'
FATAL_EVENT_STRING = 'System fatal state detected'
# Health-event injection now goes through SAI sx_api_dbg_health_event_simulate.
# Requires SAI_SDK_HEALTH_EVENT_SIMULATION=1 in the running hwsku's sai.profile —
# top-level plus per-ASIC 0/sai.profile..N-1/sai.profile on multi-ASIC platforms.
# The active hwsku dir is resolved from syncd-ibv00's bind mount, not from a
# device-class constant, so the same code works on Crocodile QM3400 vs Q3200_RA
# and any future hwsku without further wiring.
# Mapping:
# - cause=5  SX_HEALTH_CAUSE_CATAS_E       - produces Fatal-severity event (triggers fatal mode)
# - cause=24 SX_HEALTH_CAUSE_FW_ASSERT_E   - produces Warning-severity test event
FATAL_HEALTH_EVENT_SIMULATION = {
    5: 5,
    "warning": 24,
}
FATAL_EVENT_IDS = (5,)
SAI_HEALTH_SIM_KEY = "SAI_SDK_HEALTH_EVENT_SIMULATION"
SAI_HEALTH_SIM_LINE = f"{SAI_HEALTH_SIM_KEY}=1"
SAI_SIM_NOT_ENABLED_MARKER = "Health event simulation not enabled at SDK init"
SAI_SIM_RC_RE = re.compile(r"sim_rc=(-?\d+)")

# Overlap: tech-support on primary SSH, fatal inject on secondary after delay.
TECH_SUPPORT_FATAL_INJECT_DELAY_SEC = 5
# Strictly below @pytest.mark.timeout(45 * MINUTE) so the join-timeout assertion path runs
# (and gives a useful failure message) before pytest kills the test outright.
TECH_SUPPORT_CONCURRENT_JOIN_TIMEOUT_SEC = 40 * MINUTE
# Single-ASIC overlap: cause 5 (CATAS) triggers fatal; inject count follows fae events-count.


def _overlap_fatal_inject_events(events_count: int) -> list:
    return [5] * events_count

#  FIXTURES AND TEARDOWN
################################################################################


def _resolve_active_hwsku_dir(engine) -> str:
    """Return the hwsku dir actually mounted by syncd-ibv00 at /usr/share/sonic/hwsku.
    Crocodile accepts both QM3400 and Q3200_RA hwskus; the platform/hwsku in
    use depends on the running image, so a hardcoded device-class path can be
    wrong. The running container's bind-mount source is the only ground truth
    for which file syncd actually reads. The mount source ends in /<asic_index>;
    strip it to get the hwsku dir."""
    fmt = ('{{range .Mounts}}{{if eq .Destination "/usr/share/sonic/hwsku"}}'
           '{{.Source}}{{end}}{{end}}')
    src = engine.run_cmd(f"docker inspect -f '{fmt}' syncd-ibv00").strip()
    assert src.startswith('/usr/share/sonic/device/'), (
        f"syncd-ibv00 hwsku mount source unexpected: {src!r}")
    return os.path.dirname(src)


def _list_sai_profile_files(engine, asic_amount: int) -> list:
    """Return the sai.profile files syncd actually reads: top-level + one per ASIC.
    Per-ASIC files are regenerated by SONiC at boot, so a `find` can race the
    generator and return only the top-level. Enumerate deterministically from
    the active hwsku dir (resolved from syncd-ibv00's bind mount) and the known
    asic_amount; assert each expected file exists so a missing per-ASIC file
    fails loudly at setup instead of silently breaking the simulate API later."""
    hwsku_dir = _resolve_active_hwsku_dir(engine)
    expected = [f"{hwsku_dir}/sai.profile"] + [
        f"{hwsku_dir}/{i}/sai.profile" for i in range(asic_amount)]
    paths_arg = ' '.join(f'"{p}"' for p in expected)
    out = engine.run_cmd(
        f'for p in {paths_arg}; do sudo test -f "$p" || echo "MISSING $p"; done')
    missing = [line.split(' ', 1)[1] for line in out.splitlines()
               if line.startswith('MISSING ')]
    assert not missing, (
        f"Expected sai.profile files missing on DUT (per-ASIC files are "
        f"generated at boot — check that the system finished init before the "
        f"fixture ran): {missing}")
    return expected


def _ensure_sim_enabled_content(original: str) -> str:
    """Return `original` with exactly one SAI_SDK_HEALTH_EVENT_SIMULATION=1 line:
    drop any existing SAI_SDK_HEALTH_EVENT_SIMULATION=* line(s) and append =1."""
    other_lines = [line for line in original.splitlines()
                   if not line.startswith(f"{SAI_HEALTH_SIM_KEY}=")]
    return "\n".join(other_lines + [SAI_HEALTH_SIM_LINE]) + "\n"


def _patch_sai_profile_handles(handles: list) -> int:
    """Ensure every handle's sai.profile contains SAI_SDK_HEALTH_EVENT_SIMULATION=1."""
    modified = 0
    for handle in handles:
        current = handle.get_content()
        new_content = _ensure_sim_enabled_content(current)
        if new_content != current:
            handle.replace_whole_content(new_content)
            modified += 1
    return modified


def _container_sai_profile_sim_line(engine, asic: int = 1) -> str:
    """Return grep output for the sim key inside syncd's mounted hwsku sai.profile."""
    container = f"syncd-ibv0{asic - 1}"
    return engine.run_cmd(
        f'docker exec {container} grep {SAI_HEALTH_SIM_KEY} /usr/share/sonic/hwsku/sai.profile 2>&1'
    ).strip()


def _sai_health_sim_probe(engine, asic: int = 1, retries: int = 18, delay_sec: int = 10) -> tuple:
    """Probe whether sx_api_dbg_health_event_simulate is enabled (sim_rc 0 = OK)."""
    container = f"syncd-ibv0{asic - 1}"
    cause = FATAL_HEALTH_EVENT_SIMULATION["warning"]
    py_script = (
        'from python_sdk_api.sx_api import *; '
        'rc, h = sx_api_open(None); '
        f'p = new_sx_dbg_health_event_simulate_params_t_p(); p.cause = {cause}; '
        'rc_sim = sx_api_dbg_health_event_simulate(h, SX_ACCESS_CMD_SET, p); '
        'delete_sx_dbg_health_event_simulate_params_t_p(p); '
        'sx_api_close(h); '
        "print('sim_rc=' + str(rc_sim))"
    )
    cmd = f'docker exec {container} python3 -c "{py_script}" 2>&1'
    output = ""
    for attempt in range(retries):
        output = engine.run_cmd(cmd)
        if "is not running" in output or "Error response from daemon" in output:
            logger.info(
                "SAI sim probe: %s not ready (attempt %s/%s), retrying in %ss",
                container, attempt + 1, retries, delay_sec)
            time.sleep(delay_sec)
            continue
        match = SAI_SIM_RC_RE.search(output)
        return (int(match.group(1)) if match else -1), output
    return -1, output


def _reboot_dut_for_sai_profile(engine) -> None:
    with allure.step("Reboot DUT so syncd reloads sai.profile with SAI_SDK_HEALTH_EVENT_SIMULATION"):
        System().reboot.action_reboot(engine, params='force').verify_result()
        System().validate_health_status(HealthConsts.OK)
        _reset_base_prompt(engine)


def _ensure_sai_health_simulation_active(engine, asic_amount: int, handles: list) -> None:
    """Per-ASIC sai.profile may be regenerated at boot without the sim flag; patch and reboot if needed."""
    with allure.step("Verify SAI health-event simulation is active in running syncd"):
        post_modified = _patch_sai_profile_handles(handles)
        rc_sim, probe_out = _sai_health_sim_probe(engine)
        if rc_sim == 0:
            return

        logger.info(
            "SAI sim probe sim_rc=%s (post_modified=%s); rebooting once to reload SDK from patched profiles",
            rc_sim, post_modified)
        _reboot_dut_for_sai_profile(engine)
        post_reboot_modified = _patch_sai_profile_handles(handles)
        rc_sim, probe_out = _sai_health_sim_probe(engine)
        container_profile = ""
        try:
            container_profile = _container_sai_profile_sim_line(engine)
        except Exception as exc:  # noqa: BLE001
            container_profile = f"(could not read container sai.profile: {exc})"

        assert rc_sim == 0, (
            f"{SAI_HEALTH_SIM_KEY}=1 is not active in running syncd (sim_rc={rc_sim}). "
            f"post_reboot_modified={post_reboot_modified}. "
            f"Container sai.profile grep: {container_profile!r}. "
            f"hwsku={_resolve_active_hwsku_dir(engine)!r}. "
            f"The image may not support health-event simulation on this build. "
            f"Probe output:\n{probe_out}")


def _revert_engine_file_safely(handle: EngineFile) -> None:
    """ExitStack callback: revert one handle, log + swallow on failure so other
    handles still get reverted (ExitStack already runs all callbacks, but we
    want a per-file warning rather than a single propagated exception)."""
    try:
        handle.revert_to_original()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to restore {handle.file_path}: {exc}")


@pytest.fixture(scope="session")
def enable_sai_health_event_simulation(engines, devices):
    """
    Enable SAI SDK health-event simulation in the active platform's sai.profile
    files so that sx_api_dbg_health_event_simulate (used by _simulate_event)
    takes effect. On multi-ASIC platforms this means top-level + every per-ASIC
    sai.profile. EngineFile is a context manager: ExitStack registers a safe
    revert per file as soon as the handle is constructed, so partial failures
    during setup still unwind cleanly, and at session teardown every file is
    restored to its snapshot. If setup rewrote any file, teardown reboots so
    the running SDK no longer has the simulation flag applied — otherwise the
    flag would leak into any later test in the session that reboots or reads
    sai.profile.

    Not autouse: fatal_mode_setup_and_teardown pulls this in for tests that
    actually inject health events; test_post_fatal_recovery skips it so the
    teardown-only test doesn't drag in a setup reboot.
    """
    engine = engines.dut
    sai_profiles = _list_sai_profile_files(engine, devices.dut.asic_amount)
    logger.info(f"sai.profile files to update: {sai_profiles}")

    with ExitStack() as stack:
        setup_modified_files = False
        with allure.step(f"Session setup: ensure {SAI_HEALTH_SIM_LINE} in active sai.profile files"):
            handles = []
            for p in sai_profiles:
                handle = EngineFile(engine, p)
                stack.callback(_revert_engine_file_safely, handle)
                handles.append(handle)
            modified_count = 0
            for handle in handles:
                new_content = _ensure_sim_enabled_content(handle.original_content)
                if new_content != handle.original_content:
                    handle.replace_whole_content(new_content)
                    modified_count += 1
            rc_before, _ = _sai_health_sim_probe(engine)
            need_reboot = modified_count > 0 or rc_before != 0
            if not need_reboot:
                logger.info(f"All {len(handles)} sai.profile files already have {SAI_HEALTH_SIM_LINE} "
                            "and SAI sim probe passed; skipping setup reboot")
            else:
                setup_modified_files = True
                logger.info(
                    f"SAI sim setup: modified_count={modified_count}, probe sim_rc={rc_before}; rebooting")
                with allure.step("Reboot DUT to apply SAI profile change"):
                    System().reboot.action_reboot(engine, params='force').verify_result()
                    System().validate_health_status(HealthConsts.OK)
                    _reset_base_prompt(engine)

            _ensure_sai_health_simulation_active(engine, devices.dut.asic_amount, handles)

        yield

        with allure.step("Session teardown: restore active sai.profile files to original content"):
            stack.close()  # run registered _revert_engine_file_safely callbacks inside this allure step
        if setup_modified_files:
            # Files are reverted on disk; reboot so the running SDK drops the simulation flag too,
            # otherwise it would leak into any later test in the session.
            with allure.step("Session teardown: reboot DUT so SAI sim flag is no longer active"):
                System().reboot.action_reboot(engine, params='force').verify_result()
                System().validate_health_status(HealthConsts.OK)
                _reset_base_prompt(engine)


@pytest.fixture
def events_count_setting():
    output = RandomizationTool.select_random_value([1, 2]).get_returned_value()
    allure.attach(f"events_count={output}")
    return output


@pytest.fixture(autouse=True)
def fatal_mode_setup_and_teardown(test_name, engines, events_count_setting, request):
    TestToolkit.tested_api = ApiType.OPENAPI
    if test_name == test_post_fatal_recovery.__name__:
        yield
        return

    # Pull in the session-scoped SAI sim setup only for tests that actually
    # inject health events. test_post_fatal_recovery returned above so it
    # never drags in the setup reboot.
    request.getfixturevalue('enable_sai_health_event_simulation')

    with allure.step(f"Asserting health-status is OK before starting {test_name}"):
        assert (OutputParsingTool.parse_json_str_to_dictionary(System().health.show()).get_returned_value()
                [HealthConsts.STATUS]) == HealthConsts.OK

    with allure.step("Clean accumulated sai-dfw dumps to keep tech-support generation fast"):
        # Each fatal event creates a ~25MB sai-dfw archive under sdk-{dumps,wrn-dumps}_dev*/.
        # Without cleanup, accumulated dumps from prior tests bloat tech-support to 200MB+
        # / 7+ minutes. Removing them before each test keeps tech-support around 2 minutes.
        engines.dut.run_cmd('sudo rm -f /var/log/mellanox/sdk-dumps_dev*/sai-dfw-*.tar.gz '
                            '/var/log/mellanox/sdk-wrn-dumps_dev*/sai-dfw-*.tar.gz')

    _set_settings(events_count=events_count_setting, **SETTINGS)
    yield

    with allure.step("Reverting fatal-mode settings"):
        Fae().system.fatal.unset()
        TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config(engines.dut, True)
        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)


@pytest.fixture(autouse=True)
def assert_fatal_logs(engines, request):
    """Runs at the end of each test to verify that all fatal events were logged to /var/log/fatal.log"""
    engine = engines.dut
    fatal_event_timestamps.clear()
    yield

    test_name = request.node.name
    if test_negative_fatal_flow_with_warnings.__name__ in test_name:
        logger.info("Fatal log check not needed for test_negative_fatal_flow_with_warnings as it doesn't create fatal events")
        return
    if test_all_asic_fatal_while_tech_support_generating.__name__ in test_name:
        logger.info(
            "Fatal log check skipped: STATE_DB ASIC_HEALTH path does not log "
            f'"{SAI_LOG_STRING}" lines to {FATAL_LOG_FILE}'
        )
        return

    with allure.step(f'Find lines for fatal events in {FATAL_LOG_FILE}'):
        engines.dut.engine.set_base_prompt('$')  # Let the engine find the current prompt
        log_lines = engine.run_cmd(f'grep -ah "{SAI_LOG_STRING}" {FATAL_LOG_FILE}*', validate=True).splitlines()
        # Derive system_year from the first event timestamp (format "YYYY-MM-DD HH:MM:SS")
        system_year = None
        if fatal_event_timestamps:
            try:
                system_year = datetime.strptime(fatal_event_timestamps[0], '%Y-%m-%d %H:%M:%S').year
            except ValueError:
                pass
        log_timestamps = []
        for line in reversed(log_lines):
            try:
                log_timestamps.append(ClockTools.get_datetime_of_system_log_line(line, system_year=system_year))
            except ValueError:
                logger.warning(f'Could not parse timestamp from fatal.log line: {line}')
        logger.info(f'Fatal event log-lines found at {log_timestamps}')

    with allure.step('Assert each event we simulated appears in the logs'):
        missing_timestamps = []
        for timestamp in fatal_event_timestamps:
            logger.info(f'Searching for fatal event log at {timestamp} (plus/minus {SAI_LOG_TIME_WINDOW_SECONDS} secs)')
            if not any(ClockTools.datetime_difference_in_seconds(timestamp, log_timestamp) < SAI_LOG_TIME_WINDOW_SECONDS
                       for log_timestamp in log_timestamps):
                missing_timestamps.append(timestamp)

        assert not missing_timestamps, (
            f'Fatal-events simulated at the following times are missing from {FATAL_LOG_FILE}:\n' +
            '\n'.join(x for x in missing_timestamps))


def test_post_fatal_recovery(engines):
    """
    Teardown called from .db file to make sure switch is not in fatal mode when the tests finish.
    """
    engines.dut.engine.set_base_prompt('$')  # Let the engine find the current prompt
    with allure.step("Check system health at end of test"):
        health_status = (OutputParsingTool.parse_json_str_to_dictionary(System().health.show())
                         .get_returned_value()[HealthConsts.STATUS])
        for i in range(NUM_TRIES_TO_RECOVER_AT_TEARDOWN):
            if health_status == HealthConsts.OK:
                break
            else:
                with allure.step(f"{health_status=}, will attempt to recover before moving-on to next test"):
                    try:
                        if health_status == HealthConsts.FATAL:
                            _manual_exit_fatal_mode(engines.dut)
                        elif health_status == HealthConsts.NOT_OK:
                            _manual_reboot(engines.dut)
                            _wait_to_exit_fatal()
                        health_status = (OutputParsingTool.parse_json_str_to_dictionary(System().health.show())
                                         .get_returned_value()[HealthConsts.STATUS])
                    except Exception:
                        if i == NUM_TRIES_TO_RECOVER_AT_TEARDOWN - 1:
                            raise

    engines.dut.engine.set_base_prompt('$')  # Let the engine find the current prompt


#  TESTS
################################################################################


@pytest.mark.checklist
@pytest.mark.fatal_mode
def test_fatal_flow_until_soft_reset(engines, devices, random_api, random_asic, events_count_setting):
    """
    Test that health-events trigger fatal mode properly, and that the system leaves fatal mode when everything is fine.
    """
    try:
        _trigger_soft_reset(False, random_asic, events_count_setting)

        with allure.step(f'Validate {FATAL_REASON_FILE} exists'):
            fatal_reason = engines.dut.run_cmd(f'cat {FATAL_REASON_FILE} && echo', validate=True).strip()
        with allure.step(f'Validate {FATAL_REASON_FILE} contents are: "{FATAL_REASON_FILE_CONTENT}"'):
            assert fatal_reason == FATAL_REASON_FILE_CONTENT
    finally:
        _wait_to_exit_fatal()


@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.checklist
@pytest.mark.fatal_mode
def test_fatal_flow_until_reboot(engines, devices, random_api, random_asic, test_name, events_count_setting):
    """
    Test repetitive health-events cause "soft reset" and then reboot. After everything is fine, leave fatal mode.
    Also generate tech-support and assert the dump contains /etc/system_fatal and fatal_reason files.
    """
    start_time = datetime.now()
    try:
        _set_settings(reboot_count=2, clear_time=4)  # longer clear-time so we have enough time to generate tech-support
        _trigger_soft_reset(False, random_asic, events_count_setting)

        with allure.step("Trigger switch reboot 1 or 2 times (randomly)"):
            repetitions = RandomizationTool.select_random_value([1, 2]).get_returned_value()
            for _ in range(repetitions):
                _trigger_reboot(random_asic, events_count_setting)

        start_time = datetime.now()
        _check_tech_support(engines.dut, test_name, repetitions)
    finally:
        elapsed = datetime.now() - start_time
        to_wait = 4 * 60 - elapsed.seconds
        logger.info(f'{start_time=}, {elapsed=}, {to_wait=}')
        if to_wait < 0:
            to_wait = 0
        _wait_to_exit_fatal(2 + to_wait // 60)


@pytest.mark.timeout(12 * MINUTE, func_only=True)
@pytest.mark.checklist
@pytest.mark.fatal_mode
def test_single_asic_fatal_while_tech_support_generating(engines, random_asic, test_name, events_count_setting):
    """
    Single-ASIC fatal during tech-support: SAI ``sx_api_dbg_health_event_simulate`` (cause 5) on one ASIC.

    After the first ASIC enters System-Fatal, further injections on other ASICs are not exercised here.
    """
    overlap_events = _overlap_fatal_inject_events(events_count_setting)

    def inject_worker(secondary):
        _simulate_events(overlap_events, random_asic, engine=secondary)

    _run_fatal_during_tech_support_overlap(
        engines,
        test_name,
        events_count_setting,
        overlap_description=(
            f"SAI health-event simulate on ASIC{random_asic} only "
            f"(events={overlap_events}, fae_events_count={events_count_setting})"
        ),
        inject_worker=inject_worker,
    )


@pytest.mark.timeout(15 * MINUTE, func_only=True)
@pytest.mark.checklist
@pytest.mark.fatal_mode
@pytest.mark.disable_loganalyzer
def test_all_asic_fatal_while_tech_support_generating(engines, test_name, events_count_setting):
    """
    All-ASIC fatal during tech-support: STATE_DB ``ASIC_HEALTH|STATE fatal_state=true`` (one write).

    Trigger: ``sonic-db-cli STATE_DB hset ASIC_HEALTH|STATE fatal_state true`` on a second SSH session.
    Recovery: ``fatal_state false`` (fire-and-forget) in test teardown — same pattern as
    ``test_asic_health_fatal_state_marks_all_unhealthy`` in ``test_system_health.py``.
    """
    def inject_worker(secondary):
        _set_asic_health_fatal_state(secondary, "true")

    _run_fatal_during_tech_support_overlap(
        engines,
        test_name,
        events_count_setting,
        overlap_description=(
            f"STATE_DB ASIC_HEALTH fatal_state=true (all ASICs unhealthy), "
            f"fae_events_count={events_count_setting}"
        ),
        inject_worker=inject_worker,
        recover_asic_health_fatal_state=True,
        overlap_tarball_fatal_via_state_db=True,
    )


@pytest.mark.timeout(45 * MINUTE, func_only=True)
@pytest.mark.checklist
@pytest.mark.fatal_mode
def test_repeated_fatal_fw_dumps_rotation_and_cleanup(
    engines, random_asic, test_name, events_count_setting,
):
    """
    Repeated FW health-check fatal events: verify ``sai-dfw`` dumps are created, rotated when at retention,
    tech-support still works, and the system leaves fatal cleanly.

    Each cycle injects two qualifying MFDEs (2× SAI cause=5).
    After recovery, checks NVOS functional and syncd/swss containers.

    Knobs: ``FATAL_DUMP_ROTATION_CYCLES`` (default 5), ``FATAL_DUMP_MAX_FILES`` (20),
    ``FATAL_DUMP_MAX_TOTAL_MB`` (600), ``FATAL_DUMP_GROWTH_WAIT_SEC`` (90),
    ``FATAL_DUMP_CLEAR_TIME_MIN`` (2), ``FATAL_DUMP_CLEAR_EXTRA_SEC`` (5),
    ``FATAL_DUMP_EVICTION_ASSERT_FROM_COUNT`` (3),
    ``FATAL_DUMP_REQUIRE_EVICTION_OBSERVED`` (false; set true on platforms with low dump cap).
    """
    engine = engines.dut
    tech_support_tar = None
    per_cycle_counts = []
    rotation_observed = False

    try:
        with allure.step("1. Baseline: clean sai-dfw dumps and verify SDK dump directories"):
            prepare_fatal_dump_rotation_baseline(engine)

        with allure.step("2. Configure FAE fatal-mode settings for repeated FW fatal events"):
            ctx = configure_fatal_dump_rotation_settings(events_count_setting)

        for cycle in range(1, FATAL_DUMP_ROTATION_CYCLES + 1):
            with allure.step(
                f"3.{cycle}. FW fatal cycle {cycle}/{FATAL_DUMP_ROTATION_CYCLES} "
                f"on ASIC{random_asic}"
            ):
                result = run_fatal_dump_rotation_cycle(
                    engine, random_asic, cycle, ctx["fw_inject_events"],
                )
                per_cycle_counts.append(result["summary"])
                rotation_observed = rotation_observed or result["rotation_observed"]

        with allure.step("4. Verify final sai-dfw inventory within retention and size budget"):
            assert_fatal_dump_rotation_inventory(engine, per_cycle_counts, rotation_observed)

        with allure.step("5. Verify tech-support succeeds after repeated fatal events"):
            tech_support_tar = verify_tech_support_after_fatal_cycles(engine, test_name)

        with allure.step("6. Verify system health is OK and not stuck in fatal"):
            verify_system_healthy_after_fatal_cycles()
    finally:
        with allure.step("7. Cleanup tech-support tarball"):
            cleanup_fatal_dump_tech_support(engine, tech_support_tar)


@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.checklist
@pytest.mark.fatal_mode
def test_fatal_flow_until_close_ports(engines, devices, random_api, random_asic, events_count_setting):
    """
    Test the full flow – repetitive health-events cause "soft reset", followed by reboot, followed by ports-close, and
    the system remains in this state.
    """
    _trigger_soft_reset(False, random_asic, events_count_setting)
    _trigger_reboot(random_asic, events_count_setting)

    with allure.step("Trigger final fatal-mode reboot after which ports are closed"):
        try:
            _trigger_reboot(random_asic, events_count_setting)
        except Exception as e:
            if 'Waiting for NVUE to become functional' in str(e):
                ExceptionTool.log_exception(e)
                with allure.step("Waiting extra 5 minutes for the system-ready timeout (necessary when swss service is not started)"):
                    _wait(5)
                    wait_until_cli_is_up(engines.dut)
                    wait_on_systemctl_initialization(engines.dut)
            else:
                raise

        _assert_system_fatal_mode(True)
        _assert_close_ports()

    with allure.step("Assert system remains in fatal mode with closed ports even after the clear-time"):
        _wait(1, 30)
        _assert_system_fatal_mode(True)
        _assert_close_ports()

    _manual_reboot(engines.dut)
    _assert_system_fatal_mode(False)
    # todo: assert ports are open


@pytest.mark.checklist
@pytest.mark.fatal_mode
def _test_negative_flow_time_window(engines, devices, random_asic):
    """
    Test that fatal-mode is triggered only when there were the right number of health-events in the proper time window.
    """
    TestToolkit.tested_api = ApiType.NVUE
    _set_settings(events_time=3, events_count=5)
    _simulate_events(1, random_asic)
    _wait(1, 15)
    _simulate_events(1, random_asic)
    _wait(1, 30)
    _simulate_events(3, random_asic)
    _assert_syncd_restart(expect_restart=False)
    _assert_system_fatal_mode(False, False)
    _trigger_soft_reset(False, random_asic, 1)
    _wait_to_exit_fatal()


@pytest.mark.checklist
@pytest.mark.fatal_mode
def test_negative_fatal_flow_with_warnings(engines, devices, random_api, random_asic):
    """Test that fatal-mode is not triggered by health events with irrelevant event_ids."""
    _simulate_events(["warning"], random_asic)
    _assert_syncd_restart(expect_restart=False)
    _assert_system_fatal_mode(False, False)


# todo: ask Ayal Lior for specs
@pytest.mark.checklist
@pytest.mark.fatal_mode
def _test_health_events_for_different_asics(engines, devices):
    """Test that fatal-mode is not triggered if the health events happen for different ASICs."""
    TestToolkit.tested_api = ApiType.NVUE
    _set_settings(events_count=3)
    asic1, asic2 = RandomizationTool.select_random_asics(devices.dut, how_many=2).get_returned_value()
    _simulate_events(2, asic1)
    _simulate_events(2, asic2)
    _assert_syncd_restart(expect_restart=False)
    _assert_system_fatal_mode(False, False)


# todo: skipped https://redmine.mellanox.com/issues/3866364
@pytest.mark.checklist
@pytest.mark.fatal_mode
def _test_disable_fatal_mode_reboot(engines, devices, random_asic):
    """Test that the "reboot-state" setting is working properly."""
    TestToolkit.tested_api = ApiType.OPENAPI
    _set_settings(reboot_state=NvosConst.DISABLED)

    with allure.step("Trigger fatal mode with no restart"):
        _simulate_events(2, random_asic)
        _assert_syncd_restart(expect_restart=False)
        _assert_system_fatal_mode(True, True)

    with allure.step("Simulate more events and assert still no restart"):
        _simulate_events(2, random_asic)
        _assert_syncd_restart(expect_restart=False)
        _assert_system_fatal_mode(True, False)

    _wait_to_exit_fatal()


# todo
@pytest.mark.parametrize('add_one_hour', [True, False])
@pytest.mark.checklist
@pytest.mark.fatal_mode
def _test_daylight_saving_time(engines, devices, random_asic, add_one_hour):
    """
    Test that moving the clock does not disturb the timekeeping of the fatal-mode feature.
    add_one_hour is True for simulating entering DST (adding 1 hour), False for exiting DST (moving back 1 hour).
    """
    TestToolkit.tested_api = ApiType.NVUE
    system = System()

    try:
        _set_dst(not add_one_hour, system)
        _set_settings(events_time=1)

        with allure.step(f"Simulate {'enter' if add_one_hour else 'exit'} DST between 2 health events"):
            _simulate_events(1, random_asic)
            _set_dst(add_one_hour, system)
            _trigger_soft_reset(False, random_asic, 1)

        with allure.step(f"Simulate {'exit' if add_one_hour else 'enter'} DST while waiting for fatal-mode timeout"):
            _set_dst(not add_one_hour, system)
            _assert_system_fatal_mode(True, False)
            _wait_to_exit_fatal()

    finally:
        system.datetime.unset(ClockConsts.TIMEZONE, apply=True)


# todo 5.12.	test_fail_to_start_once_after_firmware_burn
# todo 5.13.	test_burn_failure_and_manual_exit_from_fatal_mode
# todo 5.14.	test_exit_fatal_by_factory_reset
# todo 5.15.	test_exit_fatal_by_image_install


# HELPER FUNCTIONS
################################################################################


def _set_settings(reboot_state=None, reboot_count=None, events_time=None, events_count=None, clear_time=None):
    with allure.step("Change settings"):
        fae = Fae()
        for var in _set_settings.__code__.co_varnames[:_set_settings.__code__.co_argcount]:
            if eval(var) is not None:
                fae.system.fatal.set(var.replace('_', '-'), eval(var))
        TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config(TestToolkit.engines.dut, True)
        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(TestToolkit.engines.dut)


def _get_random_event_list(n: int) -> list:
    return RandomizationTool.select_random_values(list(FATAL_EVENT_IDS),
                                                  number_of_values_to_select=n,
                                                  allow_repetitions=True).get_returned_value()


def _trigger_soft_reset(already_in_fatal: bool, asic: int, number_of_events):
    # todo: remove already_in_fatal because if it's true then anyway we should _trigger_reboot instead.
    with allure.step(_trigger_soft_reset.__name__ +
                     ': Generating health-events to trigger fatal-mode "soft-reset" (syncd restart)'):
        _simulate_events(number_of_events, asic, not already_in_fatal)
        recent_events = _get_recent_events() if not already_in_fatal else None
        _assert_syncd_restart()
        _assert_system_fatal_mode(True, not already_in_fatal, recent_events=recent_events)
        _wait(0, 10)


def _trigger_reboot(asic: int, number_of_events):
    with allure.step(_trigger_reboot.__name__ + ': Generating health-events to trigger fatal-mode reboot'):
        _simulate_events(number_of_events, asic, False)
        _assert_reboot()
        _assert_system_fatal_mode(True)


def _thread_safe_allure_step(title):
    """allure.step that is a no-op on worker threads.

    Allure's step context is not thread-safe: spans opened on a non-main thread can be dropped
    or attached under the wrong parent. The outer "Concurrent: tech-support on primary SSH..."
    step on the main thread already frames worker activity; emit a plain log line instead so
    the report stays coherent.
    """
    if threading.current_thread() is threading.main_thread():
        return allure.step(title)
    logger.info("[%s] %s", threading.current_thread().name, title)
    return contextlib.nullcontext()


def _simulate_events(
    number_of_events: Union[int, list, tuple],
    asic: int,
    verify_non_fatal=True,
    engine=None,
):
    """
    Generates health-events relevant for fatal mode, on a given ASIC.
    number_of_events can instead contain an ordered list/tuple of the actual event_ids.
    If ``engine`` is given, all injections go through it (secondary SSH while the primary
    session is busy, e.g. tech-support ``nv action generate``); otherwise the default
    ``TestToolkit.engines.dut`` is used.
    todo If verify_non_fatal: after each event except the last, verify system is not in fatal mode.
    """
    len_events = number_of_events if isinstance(number_of_events, int) else len(number_of_events)
    with _thread_safe_allure_step(f"{_simulate_events.__name__}: Simulating {len_events} MFDEs on ASIC{asic}"):
        event_list = (
            list(number_of_events)
            if isinstance(number_of_events, (list, tuple))
            else _get_random_event_list(number_of_events)
        )
        for event_id in event_list:
            _simulate_event(event_id, asic, engine=engine)
            _wait(0, WAIT_BETWEEN_EVENTS_SECONDS)  # fatal doesn't work if we don't wait between events


def _simulate_event(event_id, asic, engine=None):
    """Simulates a health event on the given ASIC via SAI sx_api_dbg_health_event_simulate.

    ``engine`` defaults to ``TestToolkit.engines.dut`` so existing call sites are unchanged;
    pass a secondary SSH session to overlap with a long-running primary command.

    Asserts the SDK accepted the call: if the per-ASIC sai.profile is missing
    SAI_SDK_HEALTH_EVENT_SIMULATION=1, the SDK logs 'Health event simulation
    not enabled at SDK init' and returns non-zero — without this assert, the
    test silently no-ops the injection and only fails much later on a missing
    fatal-event log line, which obscures the root cause.
    """
    engine = engine if engine is not None else TestToolkit.engines.dut
    with _thread_safe_allure_step(f"{_simulate_event.__name__}({event_id=}, {asic=})"):
        cause = FATAL_HEALTH_EVENT_SIMULATION[event_id]
        container = f"syncd-ibv0{asic - 1}"
        # Inner script is wrapped in double-quotes by `docker exec ... python3 -c "..."`,
        # so any double-quote inside the script terminates the shell-level string.
        # Use single-quotes inside, and avoid f-strings (which require quotes) in
        # favor of explicit str() concatenation for the rc print.
        py_script = (
            'from python_sdk_api.sx_api import *; '
            'rc, h = sx_api_open(None); '
            f'p = new_sx_dbg_health_event_simulate_params_t_p(); p.cause = {cause}; '
            'rc_sim = sx_api_dbg_health_event_simulate(h, SX_ACCESS_CMD_SET, p); '
            'delete_sx_dbg_health_event_simulate_params_t_p(p); '
            'sx_api_close(h); '
            "print('sim_rc=' + str(rc_sim))"
        )
        cmd = f'docker exec {container} python3 -c "{py_script}" 2>&1'
        output = _send_command_timing(engine, cmd)
        assert SAI_SIM_NOT_ENABLED_MARKER not in output, (
            f"sx_api_dbg_health_event_simulate rejected the call on {container}: "
            f"SAI_SDK_HEALTH_EVENT_SIMULATION=1 missing from the sai.profile that "
            f"this syncd reads at init (active hwsku tree resolved at fixture setup). "
            f"Full output:\n{output}")
        match = SAI_SIM_RC_RE.search(output)
        assert match, (
            f"Could not parse sim_rc from sx_api_dbg_health_event_simulate output "
            f"on {container}; expected 'sim_rc=<int>'. Full output:\n{output}")
        rc_sim = int(match.group(1))
        assert rc_sim == 0, (
            f"sx_api_dbg_health_event_simulate(cause={cause}) returned rc={rc_sim} "
            f"on {container} (0=OK, 3=PARAM_ERROR, 13=unsupported). "
            f"Full output:\n{output}")
        # Capture dut_time AFTER the call returns. The sx_api call returns only after the
        # kernel has logged the "Health-Check: new failure" line, so this gives us a
        # timestamp very close to the log entry. Capturing before the call would leave
        # docker-exec startup overhead (~2-5s) outside the assert_fatal_logs ±5s window.
        date_json = (
            System().datetime.show()
            if engine is TestToolkit.engines.dut
            else engine.run_cmd("nv show system date-time -o json", validate=True)
        )
        dut_time = ClockTools.get_local_time_from_show_system_date_time_output(date_json)
        fatal_event_timestamps.append(dut_time)


def _set_asic_health_fatal_state(engine, value, fire_and_forget=False, timeout_sec=10):
    """Delegate to ``test_system_health._set_asic_health_fatal_state`` (Harel review #698).

    That helper expects ``engines`` (with a ``.dut`` attribute); here we usually have just
    one ``engine`` (the primary ``TestToolkit.engines.dut`` or a secondary SSH session
    created for the tech-support overlap). Wrap it in a minimal namespace so we can reuse
    the canonical helper without copying its body.
    """
    _shared_set_asic_health_fatal_state(
        SimpleNamespace(dut=engine), value,
        fire_and_forget=fire_and_forget, timeout_sec=timeout_sec,
    )


def _run_fatal_during_tech_support_overlap(
    engines,
    test_name,
    events_count_setting,
    overlap_description,
    inject_worker,
    recover_asic_health_fatal_state=False,
    overlap_tarball_fatal_via_state_db=False,
):
    """Shared tech-support + fatal overlap harness.

    ``inject_worker(secondary_ssh_engine)`` runs on a second SSH session after
    ``TECH_SUPPORT_FATAL_INJECT_DELAY_SEC`` while primary generates tech-support.

    When ``overlap_tarball_fatal_via_state_db`` is True (STATE_DB ``ASIC_HEALTH fatal_state``),
    tarball validation accepts ``etc/system_fatal`` / ``log/fatal.log.gz`` as well as ``dump/fatal_reason``.
    """
    fatal_confirmed = False
    exc_holder = {"tech": None, "inject": None}
    results = {}
    overlap_clear_time_min = 6

    def tech_support_worker():
        try:
            out, duration = System().techsupport.action_generate(engines.dut, test_name=test_name)
            results["duration"] = duration
            if isinstance(out, str) and out.endswith(".tar.gz"):
                results["folder"] = out
            else:
                exc_holder["tech"] = RuntimeError(
                    f"tech-support did not return a tarball path: {out!r} duration={duration}"
                )
        except Exception as e:
            exc_holder["tech"] = e
            logger.exception("tech-support worker failed")

    def inject_worker_thread():
        try:
            time.sleep(TECH_SUPPORT_FATAL_INJECT_DELAY_SEC)
            conn_result = ConnectionTool.create_ssh_conn(
                engines.dut.ip, engines.dut.username, engines.dut.password
            )
            conn_result.verify_result()
            secondary = conn_result.get_returned_value()
            try:
                inject_worker(secondary)
            finally:
                secondary.disconnect()
        except Exception as e:
            exc_holder["inject"] = e
            logger.exception("fatal inject worker failed")

    t_tech = None
    t_inj = None
    try:
        _set_settings(reboot_count=2, clear_time=overlap_clear_time_min)
        with allure.step(
            f"Concurrent: tech-support on primary SSH, fatal trigger after "
            f"{TECH_SUPPORT_FATAL_INJECT_DELAY_SEC}s on second SSH — {overlap_description}"
        ):
            t_tech = threading.Thread(target=tech_support_worker, name="techsupport-fatal-overlap", daemon=True)
            t_inj = threading.Thread(target=inject_worker_thread, name="fatal-inject-overlap", daemon=True)
            t_tech.start()
            t_inj.start()
            t_tech.join(timeout=TECH_SUPPORT_CONCURRENT_JOIN_TIMEOUT_SEC)
            t_inj.join(timeout=TECH_SUPPORT_CONCURRENT_JOIN_TIMEOUT_SEC)

        assert not t_tech.is_alive(), (
            f"tech-support thread still running after {TECH_SUPPORT_CONCURRENT_JOIN_TIMEOUT_SEC}s join timeout"
        )
        assert not t_inj.is_alive(), (
            f"fatal inject thread still running after {TECH_SUPPORT_CONCURRENT_JOIN_TIMEOUT_SEC}s join timeout"
        )
        assert exc_holder["inject"] is None, f"Fatal inject on secondary SSH failed: {exc_holder['inject']!r}"

        if exc_holder["tech"] is not None:
            pytest.fail(
                f"Tech-support generation failed while overlapping fatal trigger ({overlap_description}): "
                f"{exc_holder['tech']!r}"
            )

        tech_folder = results.get("folder")
        assert isinstance(tech_folder, str) and tech_folder.endswith(".tar.gz"), (
            f"Expected tech-support tarball path, got: {tech_folder!r} (duration={results.get('duration')})"
        )
        with allure.step("Verify tech-support tarball exists and is non-empty on DUT"):
            sz = _send_command_timing(engines.dut, f'stat -c %s "{tech_folder}"').strip()
            assert sz.isdigit() and int(sz) > 0, f"Tech-support tarball missing or empty: {tech_folder} stat={sz!r}"

        if overlap_tarball_fatal_via_state_db:
            _assert_overlap_tech_support_tarball_state_db_fatal(engines.dut, tech_folder)
        else:
            _assert_overlap_tech_support_tarball_lists_fatal_reason(engines.dut, tech_folder)

        with allure.step(
            "Verify live fatal mode (health, LED, ASIC health issue, prompt) — same as other fatal tests"
        ):
            retry_call(
                _assert_system_fatal_mode, [True],
                exceptions=AssertionError, tries=18, delay=10,
            )
            fatal_confirmed = True
    finally:
        if recover_asic_health_fatal_state:
            with allure.step("Recover ASIC_HEALTH fatal_state=false (fire-and-forget)"):
                _set_asic_health_fatal_state(engines.dut, "false", fire_and_forget=True)
        if t_tech is not None and t_tech.is_alive():
            logger.warning("tech-support thread still alive in finally; joining up to 300s")
            t_tech.join(timeout=300)
        if t_inj is not None and t_inj.is_alive():
            logger.warning("fatal inject thread still alive in finally; joining up to 300s")
            t_inj.join(timeout=300)
        health_status = OutputParsingTool.parse_json_str_to_dictionary(
            System().health.show()
        ).get_returned_value()[HealthConsts.STATUS]
        if recover_asic_health_fatal_state:
            if fatal_confirmed:
                with allure.step("Verify system left fatal after STATE_DB fatal_state=false"):
                    retry_call(
                        _assert_system_fatal_mode,
                        fargs=[False],
                        fkwargs={"state_just_changed": False},
                        exceptions=AssertionError,
                        tries=12,
                        delay=10,
                    )
            logger.info("%s: skip _wait_to_exit_fatal (STATE_DB ASIC_HEALTH recovery)", test_name)
        elif fatal_confirmed or health_status == HealthConsts.FATAL:
            exit_fatal_wait_min = overlap_clear_time_min + 1
            logger.info("%s: scheduling _wait_to_exit_fatal minutes=%s (fae clear-time=%s)",
                        test_name, exit_fatal_wait_min, overlap_clear_time_min)
            _wait_to_exit_fatal(exit_fatal_wait_min)
        else:
            logger.info("%s: skip _wait_to_exit_fatal (health=%s, fatal_confirmed=%s)",
                        test_name, health_status, fatal_confirmed)
        with allure.step("Cleanup tech-support tarball if recorded"):
            ts = System().techsupport
            if getattr(ts, "file_name", None):
                ts.files.file_name[ts.file_name].action_delete()


def _assert_overlap_tech_support_tarball_lists_fatal_reason(engine, tech_support_tar):
    """Minimal overlap check for SAI simulate inject: ``tar -tf`` shows ``dump/fatal_reason``."""
    with allure.step("Tech-support tarball lists dump/fatal_reason (overlap — SAI inject)"):
        cmd = f'tar -tf "{tech_support_tar}" 2>/dev/null | grep -m1 dump/fatal_reason'
        line = _send_command_timing(engine, cmd)
        assert "fatal_reason" in line, (
            f"Expected tarball member path containing dump/fatal_reason in {tech_support_tar!r}; got {line!r}"
        )


def _assert_overlap_tech_support_tarball_state_db_fatal(engine, tech_support_tar):
    """
    Overlap check for STATE_DB ``ASIC_HEALTH fatal_state``: tarball documents fatal capture.

    On some platforms ``dump/fatal_reason`` is not created; ``etc/system_fatal`` and
    ``log/fatal.log.gz`` still appear in tech-support.
    """
    markers = (
        ("dump/fatal_reason", "dump/fatal_reason"),
        ("etc/system_fatal", "etc/system_fatal"),
        ("log/fatal.log", "log/fatal.log"),
    )
    with allure.step(
        "Tech-support tarball documents fatal during overlap (STATE_DB — minimal)"
    ):
        found = []
        for label, pattern in markers:
            cmd = f'tar -tf "{tech_support_tar}" 2>/dev/null | grep -m1 {pattern}'
            line = _send_command_timing(engine, cmd)
            if line.strip():
                found.append(label)
                logger.info("Tech-support tarball marker %s: %s", label, line.strip())
        assert found, (
            f"Expected at least one fatal tarball marker in {tech_support_tar!r} "
            f"(tried {[m[0] for m in markers]}); found none"
        )


def _wait(minutes, seconds=0):
    logger.info(f"sleep {minutes}:{seconds:02} minutes")
    time.sleep(60 * minutes + seconds)


def _wait_to_exit_fatal(minutes=None):
    """Waits, then asserts system leaves fatal mode after desired time."""
    minutes = minutes or SETTINGS["clear_time"]
    with allure.step(_wait_to_exit_fatal.__name__):
        _wait(minutes, seconds=13)
        _assert_system_fatal_mode(False, state_just_changed=True)


def _manual_exit_fatal_mode(engine):
    """nv action resume fae system fatal monitor force. Verify the system reboots, exits fatal mode and opens ports."""
    with allure.step(f"{_manual_exit_fatal_mode.__name__}: Run FAE command to manually exit fatal mode and check that "
                     f"the em reboots to normal"):
        Fae().system.fatal.monitor.action_deprecated(ActionConsts.RESUME, param_name="force", expect_reboot=True,
                                                     output_format=None, dut_engine=engine, expected_output="Performing reboot"
                                                     ).verify_result()
        _wait(0, 20)
        _assert_system_fatal_mode(False)
        # todo assert ports are open


def _manual_reboot(engine):
    with allure.step(f"{_manual_reboot.__name__}: Perform reboot by user"):
        System().reboot.action_reboot(engine, params='force').verify_result()
        _reset_base_prompt(engine)


def _set_dst(enter: bool, system: System):
    """Enter or exit DST (plus/minus 1 hour in system clock)."""
    with allure.step(f"{_set_dst}: Simulate {'entering' if enter else 'exiting'} DST"):
        if enter:
            system.datetime.set(ClockConsts.TIMEZONE, ClockConsts.DST_TIMEZONE, apply=True)
        else:
            system.datetime.set(ClockConsts.TIMEZONE, ClockConsts.DEFAULT_TIMEZONE, apply=True)


def _get_recent_events():
    """Capture recent system events. Call right after event simulation, before syncd restart floods the event table."""
    with allure.step("Capture recent events before syncd restart"):
        return OutputParsingTool.parse_json_str_to_dictionary(
            System().events.show_events_last_recent_entries(SystemConsts.SYSTEM_RECENT_EVENT, '3')
        ).get_returned_value()


def _assert_system_fatal_mode(fatal: bool, state_just_changed=False, recent_events=None):
    """
    Asserts system health status, led, health-issue and fatal-mode prompt are in Fatal mode (or are OK if fatal==False).
    If state_just_changed then also assert a system event was raised.
    if fatal and state_just_changed then wait for the system to detect fatal mode.
    recent_events: pre-captured events to use for the event assertion (to avoid event flooding after syncd restart).
    """
    with allure.step(f"{_assert_system_fatal_mode.__name__}: Verify fatal mode is {fatal}"):
        system = System()
        engine = TestToolkit.engines.dut
        start_time = time.perf_counter()
        if fatal and state_just_changed:
            with allure.step("Check system health until it enters fatal mode"):
                health_dict = retry_call(_assert_health_fatal, [system, True],
                                         exceptions=AssertionError, tries=6, delay=3)
        else:
            with allure.step("Check system health status"):
                health_dict = retry_call(_assert_health_fatal, [system, fatal],
                                         exceptions=AssertionError, tries=6, delay=5)
        duration = time.perf_counter() - start_time
        expected_status = 'FATAL' if fatal else 'OK'
        OperationTime.save_manual_operation_duration_to_db(
            f'health_status_to_{expected_status}', duration, pytest.test_name)

        with allure.step("Assert LED color"):
            expected_color = HealthConsts.LED_NOT_OK_STATUS if fatal else HealthConsts.LED_OK_STATUS
            led_color = health_dict[HealthConsts.STATUS_LED]
            assert expected_color in led_color  # todo: sometimes after reboot it's amber_blink instead of amber

        if fatal:
            with allure.step("Assert fatal health issue"):
                actual_issue = (health_dict[HealthConsts.ISSUES]
                                .get(HealthConsts.ASIC_HEALTH_ISSUE, {})
                                .get(HealthConsts.ISSUE))
                assert actual_issue == HealthConsts.ASIC_HEALTH_ISSUE_FATAL, (
                    f"Expected issue '{HealthConsts.ASIC_HEALTH_ISSUE_FATAL}' but issues are "
                    f"{health_dict[HealthConsts.ISSUES]}")
        else:
            with allure.step("Assert no health issues"):
                assert not health_dict[HealthConsts.ISSUES], \
                    "Health issues exist:\n" + str(health_dict)

        with allure.step("Assert console prompt"):
            def _assert_prompt():
                prompt = DutUtilsTool.get_prompt(engine)
                assert prompt.strip(), f"Got empty/whitespace prompt: '{prompt}'"
                assert prompt.startswith(FATAL_PROMPT) == fatal, \
                    f"Prompt is '{prompt}' but it should {'' if fatal else 'not '} contain '{FATAL_PROMPT}'."
            retry_call(_assert_prompt, exceptions=AssertionError, tries=3, delay=2)

        if state_just_changed:
            with allure.step(f"Assert system-event was raised for {'entering' if fatal else 'exiting'} fatal mode"):
                event_text = ('' if fatal else 'Cleared: ') + FATAL_EVENT_STRING
                event_dict = recent_events or OutputParsingTool.parse_json_str_to_dictionary(
                    system.events.show_events_last_recent_entries(SystemConsts.SYSTEM_RECENT_EVENT, '3')
                ).get_returned_value()
                event_list = [x for x in event_dict.values() if x['text'] == event_text]
                if not event_list:
                    raise AssertionError('Expected system-event not found: ' + event_text)
                elif len(event_list) > 1:
                    raise AssertionError(f'Expected 1 event for {"entering" if fatal else "exiting"} fatal mode but '
                                         f'found multiple: {event_list}')

                expected_severity = 'CRITICAL' if fatal else 'INFORMATIONAL'
                assert event_list[0]['severity'] == expected_severity, (
                    f'system-event {expected_severity=} but actual event is {event_list[0]}'
                )


def _assert_health_fatal(system: System, expect_fatal: bool) -> dict:
    output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
    status = output[HealthConsts.STATUS]
    expected_status = HealthConsts.FATAL if expect_fatal else HealthConsts.OK
    assert status == expected_status
    return output


def _assert_syncd_restart(expect_restart=True):
    """
    If expect_restart: asserts that syncd & swss of all ASICs go down and back up. Give it 2 minutes. If not all of
    these dockers stopped and re-started then raise AssertionError.
    If not expect_restart: monitor these dockers for 15 seconds to assert they don't go down.
    """
    SLEEP_SECONDS = 5
    RETRIES = 24 if expect_restart else 4
    with allure.step(f"{_assert_syncd_restart.__name__}: Verify syncd and swss are {'' if expect_restart else 'not '}"
                     f"restarted"):
        engine = TestToolkit.engines.dut
        dockers = {f"{name}{asic}" for name in ('syncd-ibv0', 'swss-ibv0')
                   for asic in range(TestToolkit.devices.dut.asic_amount)}
        logger.info(f"Test will monitor the status of these dockers: {dockers}")
        dockers_went_down = set()
        for i in range(RETRIES):
            running_dockers = set(DutUtilsTool.get_running_dockers(engine)) & dockers
            logger.info(f"{i * SLEEP_SECONDS} seconds elapsed, the following relevant dockers are currently running: "
                        f"{running_dockers}")
            dockers_went_down |= (dockers - running_dockers)
            if expect_restart:
                if len(dockers_went_down) == len(running_dockers) == len(dockers):
                    return  # because all dockers have already went down and then went back up
            else:
                if dockers_went_down:
                    raise AssertionError(f"The following dockers went down: {dockers_went_down}")
            time.sleep(SLEEP_SECONDS)

        if expect_restart:
            raise AssertionError(
                f"The following dockers never went down: {dockers - dockers_went_down}\n"
                f"The following dockers did not come back up: {dockers - running_dockers}")
        else:
            return


def _assert_reboot():
    with allure.step(f"{_assert_reboot.__name__}: Verify switch is rebooted"):
        reboot_params = RebootParams(throw_exception_on_unhealthy=False)
        DutUtilsTool.wait_on_system_reboot(TestToolkit.engines.dut, reboot_params=reboot_params)
        _reset_base_prompt(TestToolkit.engines.dut)
        _wait(0, 15)


def _reset_base_prompt(engine):
    """Tells the engine that FATAL_PROMPT does not necessarily appear in the prompt."""
    engine.engine.base_prompt = engine.engine.base_prompt.replace(FATAL_PROMPT, '')
    logger.info(f'{engine.engine.base_prompt=}')


def _assert_close_ports():
    with allure.step(f"{_assert_close_ports.__name__}: Verify ports are close"):
        nv_interface = Interface(None)
        interface_output = OutputParsingTool.parse_json_str_to_dictionary(nv_interface.show()).get_returned_value()
        assert len(interface_output) < 5  # when ports are closed we should only have ['eth0', 'ib0', 'lo']


def _assert_system_fatal_file(count: int):
    """Verifies /etc/system_fatal exists with content `count`. If count==-1, verify file doesn't exist."""
    with allure.step(f"{_assert_system_fatal_file.__name__}: Verifying file {FATAL_FILE}"):
        cmd_output = _send_command_timing(TestToolkit.engines.dut, f"cat {FATAL_FILE}")
        actual_count = -1 if ('No such file or directory' in cmd_output) else int(cmd_output or 0)  # todo: or 0 ?
        assert actual_count == count, (
            f"File {FATAL_FILE} expected: {count if count >= 0 else 'does not exist'}, "
            f"actual: {actual_count if actual_count >= 0 else 'does not exist'}"
        )


def _check_tech_support(engine, test_name, num_reboots_done):
    with allure.step("Generate tech-support and validate contents"):
        tech_support_tar, _ = System().techsupport.action_generate(engine, test_name=test_name)
        with allure.independent_step("Verify " + FATAL_FILE):
            validate_file_in_tech_support(engine, tech_support_tar, FATAL_FILE, num_reboots_done)
        with allure.independent_step("Verify " + FATAL_REASON_FILE):
            validate_file_in_tech_support(engine, tech_support_tar, FATAL_REASON_DUMP_FILE, FATAL_REASON_FILE_CONTENT)
        with allure.independent_step("Verify fatal logs in dump"):
            path_in_dump = os.path.basename(tech_support_tar).replace(".tar.gz", FATAL_LOG_FILE_IN_DUMP)
            cmd = f"tar -tvz {path_in_dump} -f {tech_support_tar}"
            output = _send_command_timing(engine, cmd)
            assert int(output.split()[2]), f'Fatal-log file in dump is empty:\n{output}'


def validate_file_in_tech_support(engine, tar_path, file_path, expected_contents):
    fatal_file_path = os.path.basename(tar_path).replace(".tar.gz", file_path)
    cmd = f"tar -Ox {fatal_file_path} -f {tar_path} && echo"  # echo needed in case the file doesn't end with newline
    with allure.step("Verify contents"):
        fatal_file_contents = _send_command_timing(engine, cmd)
        logger.info(f"{fatal_file_contents=}")
        if isinstance(expected_contents, int):
            assert fatal_file_contents.isnumeric() and int(fatal_file_contents) == expected_contents
        else:
            assert fatal_file_contents == expected_contents


def _send_command_timing(engine, cmd):
    """
    Sends command and returns the output, without explicitly waiting for the shell prompt to appear. This is
    necessary when the engine is "confused" because the prompt was changed to [System_Fatal_State]
    """
    netmiko_engine = engine.engine
    logger.info(f"Running cmd: {cmd}")
    response = netmiko_engine.send_command_timing(cmd, cmd_verify=True, delay_factor=4).strip()
    response = netmiko_engine.strip_command(cmd, response)
    logger.info(response)
    return response
