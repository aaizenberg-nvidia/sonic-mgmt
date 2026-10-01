import logging
import random
import re
import time

import pytest
from retry.api import retry_call

from ngts.nvos_constants.constants_nvos import ApiType, OutputFormat
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RegressionConfigurations import Configurations
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()

VALID_TRAY_STATES = ClusterConsts.MAINTENANCE_STATE_OPTIONS
DEFAULT_TRAY_STATE = 'up'

# Bug 5065628: interface Summary maintenance-state propagation is slow while open.
TRAY_MAINTENANCE_SUMMARY_BUG_ID = 5065628
TRAY_PORT_STATE_SETTLE_SECONDS = 7
TRAY_PORT_STATE_SETTLE_SECONDS_BUG_OPEN = 60

# Port-verify retry budget for verify_affected_ports_state (each attempt = 5s sleep
# + show time, ~9-13s wall). LTX-forced: with force-max-iterations armed every retrain
# walks all LTX iterations — 60 attempts ≈ 9-13 min, above the product's forced
# link-up budget (InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED = 375s)
# plus mass-retrain convergence margin (~5-6 min measured on RTF for a full tray).
TRAY_PORT_VERIFY_TRIES = 15
TRAY_PORT_VERIFY_TRIES_LTX_FORCED = 60
# Retries poll per-port instead of dumping the full interface state once this few
# ports remain mismatched — a full 288-port dump every 5s floods the log.
PER_PORT_POLL_MAX_PORTS = 10

# Link-up snapshot stability (get_acp_ports_for_tray): links may still be TRAINING
# when the test starts (cluster just enabled; a forced-LTX mass bring-up takes
# ~5-6 min), so sample the link-up set until identical on 3 consecutive samples.
SNAPSHOT_STABLE_SAMPLES = 3
SNAPSHOT_SAMPLE_DELAY_SECONDS = 15
SNAPSHOT_MAX_SAMPLES = 40

# Matches "Maintenance State: Up" in nv show interface Summary column (table output).
INTERFACE_SUMMARY_MAINTENANCE_RE = re.compile(
    r'Maintenance State:\s*(\S+)', re.IGNORECASE)
INTERFACE_TABLE_LINE_RE = re.compile(r'^(acp\d+)\s+', re.IGNORECASE)


def _normalize_maintenance_state(state):
    """Normalize maintenance-state / Summary values for comparison (e.g. Up -> up)."""
    if state is None:
        return 'unknown'
    return str(state).strip().lower()


def _maintenance_state_from_interface_summary(summary_value):
    """Extract maintenance state from interface summary text or dict field."""
    if summary_value is None:
        return None
    if isinstance(summary_value, dict):
        for key in (ClusterConsts.MAINTENANCE_STATE, 'maintenance-state', 'Maintenance State'):
            if key in summary_value:
                return _normalize_maintenance_state(summary_value[key])
        return None
    match = INTERFACE_SUMMARY_MAINTENANCE_RE.search(str(summary_value))
    if match:
        return _normalize_maintenance_state(match.group(1))
    return None


def _show_interface_table(interface_component):
    """'nv show interface' TABLE view. Only the NVUE CLI renders a table (REST has
    no such view), so pin the API for this read regardless of the tested API."""
    saved_api = TestToolkit.tested_api
    TestToolkit.tested_api = ApiType.NVUE
    try:
        return interface_component.show(output_format=OutputFormat.auto)
    finally:
        TestToolkit.tested_api = saved_api


def _parse_interface_table_summary_maintenance(table_output):
    """
    Parse maintenance state from the Summary column in 'nv show interface' table output.

    Example line:
      acp1  up  328G  256  nvl  ...  Maintenance State:  Up
    """
    summary_by_port = {}
    for line in table_output.splitlines():
        if not INTERFACE_TABLE_LINE_RE.match(line):
            continue
        port_name = INTERFACE_TABLE_LINE_RE.match(line).group(1)
        maint = _maintenance_state_from_interface_summary(line)
        if maint is not None:
            summary_by_port[port_name] = maint
    return summary_by_port


def eligible_trays_for_maintenance_change(tray_ids, local_switch_slot, tray_topology_config):
    """
    Tray IDs safe to use for maintenance-state up/down/diag tests.

    Product decision (2026-08-05): compute-tray admin state is not released to
    customers — SWITCH trays only (RN note in 0.6, blocked in the NMX-C API in 0.8).
    Compute trays must never be selected for maintenance-state changes.

    Labs with compute trays (compute_nodes > 0): use the local switch tray. (Since the
    6623822 fix the trays view lists compute trays too, marked maintenance-state 'N/A' —
    we still restrict to the local slot because only its port impact is verifiable
    on the local switch; relax to all non-'N/A' listed trays when a multi-switch-tray
    setup with remote-impact verification exists.)

    Switch-only labs (compute_nodes==0): every tray in the list is a switch tray;
    any of them is fair game. (Note: the only such setup configured today,
    NVOS_2_rosalind-simx, is classified standalone and skips the suite at
    cluster_and_sdn — this branch waits for a real multi-switch-tray setup.)
    """
    local_switch_slot = str(local_switch_slot)
    compute_nodes = tray_topology_config.get('compute_nodes', 0)

    if compute_nodes > 0:
        eligible = [t for t in tray_ids if str(t) == local_switch_slot]
        if not eligible:
            pytest.skip(
                f"Local switch slot {local_switch_slot} not in tray list {tray_ids} — "
                f"no switch tray available for maintenance tests (compute trays are "
                f"excluded per the switch-tray-only product decision). Check cluster / fm_config.")
        logger.info(
            "Lab has compute trays: maintenance tests use the local switch tray only %s "
            "(compute-tray admin state is not released to customers)", eligible)
        return eligible

    logger.info(
        "Switch-only setup (compute_nodes=0) — any tray may be used for maintenance tests")
    return list(tray_ids)


def pick_tray_for_maintenance_change(tray_ids, local_switch_slot, tray_topology_config):
    """Random tray ID subject to eligible_trays_for_maintenance_change rules."""
    eligible = eligible_trays_for_maintenance_change(
        tray_ids, local_switch_slot, tray_topology_config)
    return random.choice(eligible)


def pick_two_trays_for_independence_test(tray_ids, local_switch_slot, tray_topology_config):
    """Pick two distinct switch trays; labs with compute trays have only one known
    switch tray (the local slot), so this skips there — see eligible_trays_for_maintenance_change."""
    eligible = eligible_trays_for_maintenance_change(
        tray_ids, local_switch_slot, tray_topology_config)
    if len(eligible) < 2:
        pytest.skip(
            f"Need >= 2 eligible trays for independence test, got {len(eligible)}: {eligible}")
    return random.sample(eligible, 2)


def get_tray_port_state_settle_seconds():
    """Wait after tray maintenance change before nv show interface checks."""
    if is_bug_active(TRAY_MAINTENANCE_SUMMARY_BUG_ID):
        logger.info(
            "Bug %s is active — using %ss settle before interface checks",
            TRAY_MAINTENANCE_SUMMARY_BUG_ID, TRAY_PORT_STATE_SETTLE_SECONDS_BUG_OPEN)
        return TRAY_PORT_STATE_SETTLE_SECONDS_BUG_OPEN
    return TRAY_PORT_STATE_SETTLE_SECONDS


def wait_for_nmx_apps_healthy_after_gpu_reset(cluster):
    """
    After a compute GPU reset (nvidia-smi -r), the NVLink topology re-converges and
    NMX briefly drops. Wait until it recovers before touching tray/port state again:
    nmx-controller 'ok', nmx-telemetry 'ok', and the nmx-c connection 'up'.
    """
    with allure.step(
            "Wait for NMX to recover after GPU reset "
            "(nmx-controller ok, nmx-telemetry ok, nmx-c up)"):
        ClusterTools.wait_for_app_healthy(cluster, ClusterConsts.NMX_CONTROLLER)
        ClusterTools.wait_for_app_healthy(cluster, ClusterConsts.NMX_TELEMETRY)
        ClusterTools.wait_for_apps_to_be_in_wanted_state(
            cluster, cluster_expected_state='enabled', nmx_c_expected_state='up')


def reset_all_compute_node_gpus(setup_name, cluster=None):
    """
    Reset GPUs on every host listed in RegressionConfigurations.compute_nodes_per_system.

    Required BY DESIGN when a tray (including the switch tray) leaves 'down'
    (down->diag or down->up): the 'down' admin-disabled the GPU side of the links,
    and GPU-facing acps stay Down/Polling until the peer GPUs are reset with
    nvidia-smi -r (RM 5189276 was closed as not-a-bug — recovery without a GPU
    reset is unsupported per the architecture spec). Loopback-cabled acps retrain
    on their own. No-op on setups without compute node entries.

    If `cluster` is provided, waits for NMX (controller/telemetry/nmx-c) to recover
    after the reset before returning.
    """
    nodes = Configurations.compute_nodes_per_system.get(setup_name)
    if not nodes:
        logger.warning(
            "setup '%s' has no compute_nodes_per_system — skip GPU reset", setup_name)
        return
    with allure.step(
            f"Reset GPUs on all compute nodes ({len(nodes)} host(s), nvidia-smi -r)"):
        logger.info(
            "Resetting GPUs (tray leaving 'down') on: %s",
            [n['ip_address'] for n in nodes])
        ClusterTools.reboot_compute_nodes_gpus(setup_name)
    if cluster is not None:
        wait_for_nmx_apps_healthy_after_gpu_reset(cluster)


def get_compute_tray_slots(sdn):
    """
    Slot ids of compute trays, taken from the trays view.

    Since the 6623822 fix (0.8 >= 25.03.0722) 'nv show sdn trays' lists compute trays
    again, with maintenance-state 'N/A' (their admin state is not supported), so the
    view itself names them — no partition parsing needed (and this keeps working when
    partitions are deleted).
    """
    return {
        tray_id for tray_id, tray_data in get_all_trays(sdn).items()
        if tray_data.get(ClusterConsts.MAINTENANCE_STATE) == ClusterConsts.COMPUTE_TRAY_MAINTENANCE_STATE}


def get_all_trays(sdn):
    """Return parsed output of 'nv show sdn trays' as dict."""
    return OutputParsingTool.parse_show_output_to_dict(
        sdn.trays.show()
    ).get_returned_value()


def get_tray(sdn, tray_id):
    """Return parsed output of 'nv show sdn trays <tray_id>' as dict."""
    return OutputParsingTool.parse_show_output_to_dict(
        sdn.trays.tray[tray_id].show()
    ).get_returned_value()


def verify_tray_state(sdn, tray_id, expected_state):
    """Verify tray state in both individual and aggregated show output."""
    with allure.step(f"Verify tray {tray_id} maintenance-state == {expected_state}"):
        tray_output = get_tray(sdn, tray_id)
        actual = tray_output.get(ClusterConsts.MAINTENANCE_STATE)
        assert actual == expected_state, (
            f"Tray {tray_id}: expected {expected_state}, got {actual}")

        all_trays = get_all_trays(sdn)
        actual_in_all = all_trays[str(tray_id)][ClusterConsts.MAINTENANCE_STATE]
        assert actual_in_all == expected_state, (
            f"Tray {tray_id} in aggregated output: expected {expected_state}, got {actual_in_all}")


def restore_tray_cleanup(sdn, tray_id):
    """Restore tray to default state. Raises on failure to prevent polluting next tests."""
    sdn.trays.action_restore_maintenance_state(tray_id=tray_id).verify_result()


def wait_for_tray_port_state_settle(expected_maintenance_state):
    """Allow tray change to propagate before nv show interface checks."""
    settle_secs = get_tray_port_state_settle_seconds()
    with allure.step(
            f"Wait {settle_secs}s for port maintenance/link state to settle "
            f"(tray maintenance-state={expected_maintenance_state})"):
        logger.info(
            "Waiting %ss for port maintenance-state and link state to settle after tray change "
            "(expected maintenance-state=%s) before nv show interface checks",
            settle_secs, expected_maintenance_state)
        time.sleep(settle_secs)


def _port_mismatch(acp_name, port_data, expected_link_state, expected_maint_norm):
    """Mismatch description for one port's JSON data ({'link': {...}}), or None if it matches."""
    if not port_data:
        return f"{acp_name}: not found in interface output"
    link_data = port_data.get('link', {})
    actual_link = link_data.get('state', 'unknown')
    if isinstance(actual_link, dict):
        actual_link = next(iter(actual_link.keys()), 'unknown')
    actual_maint = _normalize_maintenance_state(
        link_data.get(ClusterConsts.MAINTENANCE_STATE, 'unknown'))
    errors = []
    if actual_link != expected_link_state:
        errors.append(f"state={actual_link} (expected {expected_link_state})")
    if actual_maint != expected_maint_norm:
        errors.append(f"maintenance-state={actual_maint} (expected {expected_maint_norm})")
    return f"{acp_name}: {', '.join(errors)}" if errors else None


def verify_affected_ports_state(affected_acps, expected_link_state, expected_maintenance_state,
                                tries=TRAY_PORT_VERIFY_TRIES):
    """
    Verify that ALL affected acp ports have the expected link state AND maintenance-state.

    `tries`: retry budget (x5s delay + show time). Use TRAY_PORT_VERIFY_TRIES_LTX_FORCED
    when force-max-iterations is armed — forced retrains take ~5-6 min instead of ~1 min.

    The relationship between tray state and port states:
        tray 'up'   -> link.state = 'up',   maintenance-state = 'up'
        tray 'down' -> link.state = 'down',  maintenance-state = 'down'
        tray 'diag' -> link.state = 'up',   maintenance-state = 'diag'

    Log-volume policy: retries re-check ONLY the still-mismatched ports (per-port
    shows once <= PER_PORT_POLL_MAX_PORTS remain) instead of dumping the full
    interface state every 5s, and the table Summary column ('Maintenance State: X')
    is verified once after the link/maintenance states converge.
    """
    expected_maint_norm = _normalize_maintenance_state(expected_maintenance_state)
    wait_for_tray_port_state_settle(expected_maintenance_state)
    interface_component = Interface(parent_obj=None)
    logger.info(
        "Verifying %s ports: link=%s, maintenance-state=%s (up to %s tries x 5s)",
        len(affected_acps), expected_link_state, expected_maint_norm, tries)

    pending = list(affected_acps)
    mismatch_info = {}
    for attempt in range(1, tries + 1):
        if attempt > 1:
            time.sleep(5)
        if len(pending) <= PER_PORT_POLL_MAX_PORTS:
            polled = {
                acp_name: OutputParsingTool.parse_show_output_to_dict(
                    interface_component.show(op_param=acp_name, output_format=OutputFormat.json)
                ).get_returned_value()
                for acp_name in pending}
        else:
            polled = OutputParsingTool.parse_show_output_to_dict(
                interface_component.show(output_format=OutputFormat.json)).get_returned_value()
        mismatch_info = {}
        for acp_name in pending:
            error = _port_mismatch(acp_name, polled.get(acp_name),
                                   expected_link_state, expected_maint_norm)
            if error:
                mismatch_info[acp_name] = error
        pending = list(mismatch_info.keys())
        if not pending:
            break
        logger.warning(
            "%s/%s ports mismatch (expected link=%s, maintenance=%s), attempt %s/%s: %s%s",
            len(pending), len(affected_acps), expected_link_state, expected_maint_norm,
            attempt, tries, ', '.join(pending[:10]), ' ...' if len(pending) > 10 else '')

    assert not pending, (
        f"{len(pending)}/{len(affected_acps)} ports mismatch after {tries} tries "
        f"(expected link={expected_link_state}, maintenance={expected_maint_norm}):\n" +
        "\n".join(list(mismatch_info.values())[:20]) +
        (f"\n... and {len(pending) - 20} more" if len(pending) > 20 else ""))

    def _check_summary_column():
        table_output = _show_interface_table(interface_component)
        summary_by_port = _parse_interface_table_summary_maintenance(table_output)
        summary_mismatch = [
            acp_name for acp_name in affected_acps
            if summary_by_port.get(acp_name) != expected_maint_norm]
        assert not summary_mismatch, (
            f"{len(summary_mismatch)}/{len(affected_acps)} ports Summary 'Maintenance "
            f"State' != {expected_maint_norm}: {summary_mismatch[:20]}")

    retry_call(_check_summary_column, exceptions=AssertionError, tries=3, delay=5)
    logger.info(
        "All %s ports confirmed: link=%s, maintenance-state=%s (incl. Summary column)",
        len(affected_acps), expected_link_state, expected_maint_norm)


def get_acp_ports_for_tray(devices, target_slot_id, local_switch_slot):
    """
    Snapshot the acp ports to verify when a (switch) tray's maintenance-state changes.

    Only switch trays may be targeted (compute-tray admin state is not released to
    customers). A switch-tray change affects the cabled acp ports; acps without a
    peer stay link 'down' regardless of the tray state, so only ports that are
    link 'up' before the change are verified. Links may still be TRAINING when the
    test starts (the cluster was just enabled, and with force-max-iterations armed
    a mass bring-up takes ~5-6 min), so the link-up set is sampled until it is
    stable before snapshotting. Must be called BEFORE the first tray change, while
    the tray is still in the default 'up' state.
    """
    model_acps = set(devices.dut.nvl_access_ports_list)
    interface_component = Interface(parent_obj=None)

    def _link_up_acps():
        table_output = _show_interface_table(interface_component)
        up_acps = []
        for line in table_output.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] in model_acps and fields[1] == 'up':
                up_acps.append(fields[0])
        return up_acps

    with allure.step("Snapshot link-up acp ports (wait until the link-up set is stable)"):
        linked_acps = _link_up_acps()
        stable = 0
        for _ in range(SNAPSHOT_MAX_SAMPLES):
            if linked_acps and stable >= SNAPSHOT_STABLE_SAMPLES - 1:
                break
            time.sleep(SNAPSHOT_SAMPLE_DELAY_SECONDS)
            current = _link_up_acps()
            stable = stable + 1 if (current and set(current) == set(linked_acps)) else 0
            logger.info("Link-up acps: %s/%s (stable samples: %s)",
                        len(current), len(model_acps), stable)
            linked_acps = current
        else:
            logger.warning(
                "Link-up set did not stabilize after %s samples — snapshotting anyway",
                SNAPSHOT_MAX_SAMPLES)
    if not linked_acps:
        pytest.skip(
            f"No link-up acp ports before the tray {target_slot_id} maintenance test "
            f"(local switch slot {local_switch_slot}) — cannot verify tray-driven "
            f"port state; check links/cluster.")
    logger.info(
        "Tray %s is a switch tray (local switch slot %s) — verifying the %s/%s acp "
        "ports that are link-up and stable before the change",
        target_slot_id, local_switch_slot, len(linked_acps), len(model_acps))
    return sorted(linked_acps, key=lambda acp: int(acp[3:]))


def ensure_cluster_enabled(cluster, setup_name, devices):
    """
    Enable cluster and wait for NMX-C after @disabled_access_ports leaves cluster disabled.
    """
    with allure.step("Enable cluster"):
        ClusterTools.start_cluster(cluster, setup_name, OutputFormat.json, devices=devices)
        ClusterTools.wait_until_app_expected_status(cluster, ClusterConsts.NMX_CONTROLLER, 'ok')
