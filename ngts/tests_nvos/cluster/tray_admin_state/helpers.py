import json
import logging
import random
import re
import time

import pytest
from retry import retry

from ngts.nvos_constants.constants_nvos import OutputFormat
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

# NMX topology JSON keys
TOPO_DEVICE_INFO = 'deviceTopoInfo'
TOPO_GPU_INFO = 'gpuTopoInfo'
TOPO_SWITCH_INFO = 'switchTopoInfo'
TOPO_LOC = 'loc'
TOPO_LOCATION = 'location'
TOPO_SLOT_ID = 'slotId'
TOPO_DEVICE_UID = 'deviceUid'
TOPO_DEVICE_ID = 'deviceId'
TOPO_PORT_INFO = 'portTopoInfo'
TOPO_PORT_TYPE = 'portType'
TOPO_PORT_TYPE_ACCESS = 'NMX_PORT_TYPE_SWITCH_ACCESS'
TOPO_PEER_DEVICE_UID = 'peerPortDeviceUid'
TOPO_SYSTEM_PORT_NUM = 'systemPortNum'
TOPO_NUM_PORTS = 'numPorts'
NMX_TOPOLOGY_FILE_TYPE = 'topology'

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

    RTF-style (switch_nodes==1): never toggle the only switch tray — lab has a single
    switch; taking it down kills the DUT under test. Use compute trays only.

    Simx / switch-only (compute_nodes==0): no compute GPUs; any tray in the list is
    fair game (typically other switch trays in the cluster view).
    """
    local_switch_slot = str(local_switch_slot)
    compute_nodes = tray_topology_config.get('compute_nodes', 0)
    switch_nodes = tray_topology_config.get('switch_nodes', 0)

    if compute_nodes > 0 and switch_nodes == 1:
        eligible = [t for t in tray_ids if str(t) != local_switch_slot]
        if not eligible:
            pytest.skip(
                f"No compute trays available for maintenance tests (local switch slot "
                f"{local_switch_slot}, all trays {tray_ids}). Check cluster / fm_config.")
        logger.info(
            "Single-switch setup: maintenance tests will use compute trays only %s "
            "(excluding switch slot %s)", eligible, local_switch_slot)
        return eligible

    logger.info(
        "Setup has compute_nodes=%s switch_nodes=%s — any tray may be used for maintenance tests",
        compute_nodes, switch_nodes)
    return list(tray_ids)


def pick_two_trays_for_independence_test(tray_ids, local_switch_slot, tray_topology_config):
    """Pick two distinct trays; on single-switch + compute setups, both are compute trays."""
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


def is_compute_tray(tray_id, local_switch_slot, tray_topology_config):
    """Remote compute tray (not the local switch slot) when the lab has compute nodes."""
    if tray_topology_config.get('compute_nodes', 0) == 0:
        return False
    return str(tray_id) != str(local_switch_slot)


def is_rtf_compute_tray_scenario(tray_id, local_switch_slot, tray_topology_config):
    """
    RTF-style lab: single switch + compute trays — compute-tray maintenance tests must
    delete SDN partition(s) first (e.g. default partition 32766 with GPUs).
    """
    compute_nodes = tray_topology_config.get('compute_nodes', 0)
    switch_nodes = tray_topology_config.get('switch_nodes', 0)
    return (
        compute_nodes > 0 and
        switch_nodes == 1 and
        str(tray_id) != str(local_switch_slot)
    )


def delete_sdn_partitions_before_compute_tray_test(sdn, excluded_partitions=[]):
    """
    Delete all SDN partitions before compute-tray maintenance-state changes.

    Lab flow: `nv action delete sdn partition <id>` on default partition (e.g. 32766)
    so tray maintenance can be applied to compute nodes without partition binding.
    """
    with allure.step("Delete SDN partition(s) before compute-tray maintenance test"):
        partitions = OutputParsingTool.parse_show_output_to_dict(
            sdn.partition.show()).get_returned_value()
        if not partitions:
            logger.info("No SDN partitions present — nothing to delete")
            return
        for partition_id in list(partitions.keys()):
            if partition_id in excluded_partitions:
                continue
            partition_name = partitions[partition_id].get('name', partition_id)
            logger.info("Deleting partition %s (%s)", partition_id, partition_name)
            sdn.partition.partition_id[str(partition_id)].action_delete_partition().verify_result()
        remaining = OutputParsingTool.parse_show_output_to_dict(
            sdn.partition.show()).get_returned_value()
        if not excluded_partitions:
            assert not remaining, (
                f"Expected all SDN partitions deleted before compute-tray test, still have: {remaining}")
        # NMX-C unbinds GPU/switch-port resources from a deleted partition asynchronously:
        # 'nv show sdn partition' reports it gone before the topology releases the resources,
        # so the next tray maintenance-state change can hit NMX_ST_RESOURCE_IN_USE. Let it settle.
        time.sleep(5)


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

    After tray maintenance-state 'down' (switch or compute tray), reset is required when
    leaving 'down'.

    Why (switch tray): multi-switch designs assume at least one switch tray remains active
    so inband connectivity to all GPUs is preserved. RTF has a *single* switch tray, so
    taking it 'down' severs every plane to the compute tray(s) at once (same ASICs).

    Why (compute tray): putting the compute tray 'down' similarly cuts the inband path to
    its GPUs — there is no alternate plane to send Hello / recover those links without a
    GPU reset after leaving 'down'.

    If `cluster` is provided, waits for NMX (controller/telemetry/nmx-c) to recover after
    the reset before returning.

    Per-tray → compute mapping is not implemented; all configured compute nodes are reset.
    """
    nodes = Configurations.compute_nodes_per_system.get(setup_name)
    if not nodes:
        logger.warning(
            "setup '%s' has no compute_nodes_per_system — skip GPU reset; "
            "add compute IP entries in RegressionConfigurations.py",
            setup_name)
        return
    with allure.step(
            f"Reset GPUs on all compute nodes ({len(nodes)} host(s), nvidia-smi -r)"):
        logger.info(
            "Resetting GPUs (tray leaving 'down') on: %s",
            [n['ip_address'] for n in nodes])
        ClusterTools.reboot_compute_nodes_gpus(setup_name)
    if cluster is not None:
        wait_for_nmx_apps_healthy_after_gpu_reset(cluster)


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


def restore_tray_cleanup(sdn, tray_id, setup_name, cluster, restore=True, reset_gpu=False):
    """
    Optionally restore tray to default and/or reset compute GPUs (cleanup helper).

    restore: if True, run action_restore_maintenance_state (skip when factory reset
    already returned the tray to default).
    reset_gpu: if True, reset compute GPUs after restore. Moving from down to up is not
    enough — 'down' severs inband paths and nvidia-smi -r is needed so links recover:

    - Switch tray (RTF): design assume at least one switch tray remains active for
      inband connectivity to all GPUs. This setup has a *single* switch tray, so every
      plane to the compute tray(s) traverses the same ASICs and is severed at once —
      NMX fails with "failed to find an active link to send Hello Message".
    - Compute tray: 'down' cuts the inband path to that tray's GPUs; there is no
      alternate way to send Hello / recover those links without a GPU reset.
    """
    if restore:
        with allure.step(f"Restore tray {tray_id} maintenance state to default"):
            sdn.trays.action_restore_maintenance_state(tray_id=tray_id).verify_result()
    if reset_gpu:
        with allure.step(f"Reset compute GPUs after tray {tray_id} restore"):
            reset_all_compute_node_gpus(setup_name, cluster)


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


def verify_affected_ports_state(affected_acps, expected_link_state, expected_maintenance_state):
    """
    Verify that ALL affected acp ports have the expected link state AND maintenance-state.

    Checks per port:
    - link.state: the physical port state ('up' or 'down') from 'nv show interface -o json'
    - link.maintenance-state: tray maintenance-state ('up', 'down', or 'diag') from JSON
    - Summary column: 'Maintenance State: <state>' from 'nv show interface' table output

    The relationship between tray state and port states:
        tray 'up'   -> link.state = 'up',   maintenance-state = 'up'
        tray 'down' -> link.state = 'down',  maintenance-state = 'down'
        tray 'diag' -> link.state = 'up',   maintenance-state = 'diag'

    Waits get_tray_port_state_settle_seconds() after the tray change, then runs 'nv show interface'
    (json + table) once per retry. Uses retry if states are still converging.
    """
    expected_maint_norm = _normalize_maintenance_state(expected_maintenance_state)
    wait_for_tray_port_state_settle(expected_maintenance_state)

    @retry(AssertionError, tries=15, delay=5)
    def _check_all_ports():
        interface_component = Interface(parent_obj=None)
        all_interfaces = OutputParsingTool.parse_show_output_to_dict(
            interface_component.show(output_format=OutputFormat.json)
        ).get_returned_value()
        table_output = interface_component.show(output_format=OutputFormat.auto)
        summary_by_port = _parse_interface_table_summary_maintenance(table_output)

        mismatched = []
        for acp_name in affected_acps:
            if acp_name not in all_interfaces:
                mismatched.append(f"{acp_name}: not found in interface output")
                continue
            link_data = all_interfaces[acp_name].get('link', {})
            actual_link = link_data.get('state', 'unknown')
            if isinstance(actual_link, dict):
                actual_link = next(iter(actual_link.keys()), 'unknown')
            actual_maint = _normalize_maintenance_state(
                link_data.get(ClusterConsts.MAINTENANCE_STATE, 'unknown'))

            errors = []
            if actual_link != expected_link_state:
                errors.append(f"state={actual_link} (expected {expected_link_state})")
            if actual_maint != expected_maint_norm:
                errors.append(
                    f"maintenance-state={actual_maint} (expected {expected_maint_norm})")

            summary_maint = _maintenance_state_from_interface_summary(
                all_interfaces[acp_name].get('summary'))
            if summary_maint is None:
                summary_maint = summary_by_port.get(acp_name)
            if summary_maint is None:
                errors.append("summary Maintenance State not found")
            elif summary_maint != expected_maint_norm:
                errors.append(
                    f"summary Maintenance State={summary_maint} "
                    f"(expected {expected_maint_norm})")

            if errors:
                mismatched.append(f"{acp_name}: {', '.join(errors)}")

        assert not mismatched, (
            f"{len(mismatched)}/{len(affected_acps)} ports mismatch "
            f"(expected link={expected_link_state}, maintenance={expected_maint_norm}):\n" +
            "\n".join(mismatched[:20]) +
            (f"\n... and {len(mismatched) - 20} more" if len(mismatched) > 20 else ""))

    logger.info(
        "Verifying %s ports: link=%s, maintenance-state=%s (json link + Summary column)",
        len(affected_acps), expected_link_state, expected_maint_norm)
    _check_all_ports()
    logger.info(
        "All %s ports confirmed: link=%s, maintenance-state=%s",
        len(affected_acps), expected_link_state, expected_maint_norm)


def get_acp_ports_for_tray(engines, sdn, devices, target_slot_id, local_switch_slot,
                           tray_topology_config):
    """
    Determine which acp ports should be affected when a tray's maintenance-state changes.

    Setup policy (tray_topology_config from RegressionConfigurations.tray_topology):
    - Local switch tray: all local acp ports (no topology file).
    - compute_nodes==0: no gpuTopoInfo in lab; do not parse topology — use all local acps.
    - compute_nodes>0, remote compute tray: parse topology gpuTopoInfo; if missing, skip
      (do not fail) — common when FM exports switch-only topology.
    - switch_nodes>1: only switchTopoInfo for the local switch slotId (multi-switch).

    The NMX topology file (JSON) has a flat list under 'deviceTopoInfo'.
    Each entry is either a GPU chip or a switch ASIC:

        deviceTopoInfo: [
            { "gpuTopoInfo":    { ... } },   <-- GPU chip in a compute tray
            { "switchTopoInfo": { ... } },   <-- Switch ASIC in the switch tray
            ...
        ]

    GPU entries tell us which compute tray each GPU chip belongs to:
        gpuTopoInfo.loc.location.slotId  -->  the tray ID (e.g. "1" or "2")
        gpuTopoInfo.deviceUid            -->  unique ID of this GPU chip

    Switch entries contain the actual port connectivity:
        switchTopoInfo.deviceId          -->  ASIC number (1=U1, 2=U2, 3=U3, 4=U4)
        switchTopoInfo.numPorts          -->  ports per ASIC (e.g. 72), read dynamically
        switchTopoInfo.portTopoInfo[]    -->  list of switch ports on this ASIC

    Each switch port entry has:
        portType          -->  "NMX_PORT_TYPE_SWITCH_ACCESS" for access ports
        systemPortNum     -->  1 to numPorts, the port number within this ASIC
        peerPortDeviceUid -->  the deviceUid of the GPU chip on the OTHER end

    Algorithm (for compute tray):
        1. From all gpuTopoInfo entries, build:  gpu_deviceUid -> slotId
        2. Read numPorts from the first switchTopoInfo entry (ports per ASIC)
        3. For each switch ASIC's access port, look up peerPortDeviceUid in the GPU map
        4. If the peer GPU's slotId matches our target tray -> this port is affected
        5. Translate to acp name:  acp_number = (deviceId - 1) * numPorts + systemPortNum

    Concrete example from the RTF2 setup (1 switch, 2 compute trays, numPorts=72):
        - Target tray: slotId=2 (compute tray with 2 GPU chips)
        - GPU chips in slot 2 have deviceUid "15046..." and "17640..."
        - Switch ASIC U4 (deviceId=4), port systemPortNum=1, peerPortDeviceUid="15046..."
          -> peer is in slotId=2 -> acp_number = (4-1)*72 + 1 = 217 -> acp217 will go down
        - Switch ASIC U1 (deviceId=1), port systemPortNum=1, peerPortDeviceUid="15046..."
          -> peer is in slotId=2 -> acp_number = (1-1)*72 + 1 = 1 -> acp1 will go down
        - Result: 18 ports per ASIC x 4 ASICs = 72 acp ports affected for slot 2

    For the switch tray (target_slot_id == local_switch_slot):
        All acp ports go down (taken from device model). No topology parsing needed.

    Returns: sorted list of acp port names (e.g. ['acp1', 'acp2', 'acp73', ...])
    """
    target_slot_id = str(target_slot_id)
    local_switch_slot = str(local_switch_slot)
    compute_nodes = tray_topology_config.get('compute_nodes', 0)
    switch_nodes = tray_topology_config.get('switch_nodes', 0)
    all_acps = list(devices.dut.nvl_access_ports_list)

    # --- Case 1: Local switch tray — all acp ports ---
    if target_slot_id == local_switch_slot:
        logger.info(
            "Tray %s is the local switch tray — all %s acp ports should be affected",
            target_slot_id, len(all_acps))
        return all_acps

    # --- Case 2: No compute in lab model (e.g. simx) — skip topology; use all local acps ---
    if compute_nodes == 0:
        logger.info(
            "Tray %s: compute_nodes=0 in lab config — not parsing topology; "
            "using all %s local acp ports for interface checks", target_slot_id, len(all_acps))
        return all_acps

    # --- Case 3: Compute tray — parse topology when gpuTopoInfo is expected ---
    logger.info(
        "Tray %s is a compute tray — parsing topology to find connected acp ports", target_slot_id)

    with allure.step("Generate NMX topology file"):
        sdn.state.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[NMX_TOPOLOGY_FILE_TYPE].action_generate_sdn()

    with allure.step("Get topology file path"):
        files_output = OutputParsingTool.parse_show_output_to_dict(
            sdn.state.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[NMX_TOPOLOGY_FILE_TYPE].files.show()
        ).get_returned_value()
        # Get the most recent topology file path
        topology_file_path = list(files_output.values())[-1]['path']
        logger.info(f"Topology file: {topology_file_path}")

    with allure.step("Read and parse topology JSON"):
        raw_json = engines.dut.run_cmd(f"sudo cat {topology_file_path}")
        topology = json.loads(raw_json)

    # Step 1: Build a map of GPU deviceUid -> slotId
    # Each gpuTopoInfo entry is one GPU chip. Multiple GPU chips can share
    # the same slotId (they're in the same compute tray).
    # The tray id from 'nv show sdn trays' equals the device slotId (verified against
    # real RTF topology: switch slotId=3 -> tray "3", GPU slotId=1/2 -> tray "1"/"2").
    # trayIndex is a separate enumeration (0-based) and does NOT match the tray id, so
    # we map and match strictly on slotId.
    gpu_uid_to_slot = {}
    for device_entry in topology[TOPO_DEVICE_INFO]:
        if TOPO_GPU_INFO in device_entry:
            gpu = device_entry[TOPO_GPU_INFO]
            gpu_uid = gpu[TOPO_DEVICE_UID]
            slot_id = str(gpu[TOPO_LOC][TOPO_LOCATION][TOPO_SLOT_ID])
            gpu_uid_to_slot[gpu_uid] = slot_id
            logger.info(f"  GPU chip {TOPO_DEVICE_UID}={gpu_uid} -> {TOPO_SLOT_ID}={slot_id}")

    if not gpu_uid_to_slot:
        pytest.skip(
            f"No gpuTopoInfo in NMX topology file — cannot map compute tray {target_slot_id} "
            f"to acp ports (FM often exports switch-only topology until compute is discovered). "
            f"Skip port checks; fix cluster/fm_config or run on a setup with GPU entries.")

    # Step 2: For each switch ASIC, find access ports whose peer is in the target compute tray.
    # Each switchTopoInfo entry is one switch ASIC (U1, U2, U3, or U4).
    # The deviceId tells us which ASIC: 1=U1, 2=U2, 3=U3, 4=U4.
    # numPorts (read from topology) tells us how many ports per ASIC.
    # The acp formula: acp_number = (deviceId - 1) * numPorts + systemPortNum
    affected_acps = []
    ports_per_asic = None
    for device_entry in topology[TOPO_DEVICE_INFO]:
        if TOPO_SWITCH_INFO in device_entry:
            switch = device_entry[TOPO_SWITCH_INFO]
            if switch_nodes > 1:
                switch_slot = str(
                    switch.get(TOPO_LOC, {}).get(TOPO_LOCATION, {}).get(TOPO_SLOT_ID, ''))
                if switch_slot and switch_slot != local_switch_slot:
                    logger.info(
                        "  Skipping switch ASIC on remote slot %s (local=%s)",
                        switch_slot, local_switch_slot)
                    continue
            asic_device_id = switch[TOPO_DEVICE_ID]
            asic_description = switch.get('description', '')

            if ports_per_asic is None:
                ports_per_asic = int(switch[TOPO_NUM_PORTS])
                logger.info(f"  Ports per ASIC (from topology): {ports_per_asic}")

            logger.info(f"  Scanning switch ASIC {TOPO_DEVICE_ID}={asic_device_id} ({asic_description})")

            for port in switch[TOPO_PORT_INFO]:
                if port[TOPO_PORT_TYPE] != TOPO_PORT_TYPE_ACCESS:
                    continue

                peer_uid = port[TOPO_PEER_DEVICE_UID]
                system_port_num = int(port[TOPO_SYSTEM_PORT_NUM])

                # Look up which compute tray this port's peer GPU belongs to.
                peer_slot = gpu_uid_to_slot.get(peer_uid)
                if peer_slot == target_slot_id:
                    acp_number = (asic_device_id - 1) * ports_per_asic + system_port_num
                    affected_acps.append(f'acp{acp_number}')

    if ports_per_asic is None:
        pytest.skip(
            f"No switchTopoInfo for local slot {local_switch_slot} in topology — "
            f"cannot map acp ports for compute tray {target_slot_id}")

    affected_acps.sort(key=lambda x: int(x.replace('acp', '')))
    if not affected_acps:
        pytest.skip(
            f"Topology has gpuTopoInfo but no access ports map to compute tray {target_slot_id} — "
            f"skip rather than fail (peer map / slotId mismatch).")
    logger.info(f"Tray {target_slot_id}: {len(affected_acps)} acp ports should be affected: {affected_acps}")
    return affected_acps


def ensure_cluster_enabled(cluster, setup_name, devices):
    """
    Enable cluster and wait for NMX-C after @disabled_access_ports leaves cluster disabled.
    """
    with allure.step("Enable cluster"):
        ClusterTools.start_cluster(cluster, setup_name, OutputFormat.json, devices=devices)
        ClusterTools.wait_until_app_expected_status(cluster, ClusterConsts.NMX_CONTROLLER, 'ok')
