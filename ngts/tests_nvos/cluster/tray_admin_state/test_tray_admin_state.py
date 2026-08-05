import logging
import random

import pytest

from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.Devices.IbDevice import JulietSwitch, RosalindSwitch
from ngts.tests_nvos.cluster.tray_admin_state.helpers import (
    DEFAULT_TRAY_STATE,
    VALID_TRAY_STATES,
    delete_sdn_partitions_before_compute_tray_test,
    ensure_cluster_enabled,
    get_acp_ports_for_tray,
    get_all_trays,
    get_tray,
    is_compute_tray,
    is_rtf_compute_tray_scenario,
    pick_two_trays_for_independence_test,
    reset_all_compute_node_gpus,
    restore_tray_cleanup,
    verify_affected_ports_state,
    verify_tray_state,
)

logger = logging.getLogger()


# ---------------------------------------------------------------------------
#  4.1 test_tray_show_cmd
# ---------------------------------------------------------------------------


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_tray_show_cmd(engines, devices, random_api, cluster_and_sdn, chassis_info,
                       expected_tray_count, tray_topology_config, setup_name,
                       standalone_system, has_loopbox):
    """
    Test Objective:
    Verify that the SDN tray show commands return correct output format, that all trays
    are displayed with their maintenance-state field, and that showing a single tray
    matches the aggregated output.

    Tray count is checked against RegressionConfigurations.tray_topology for this
    setup_name. Wrong count (e.g. 27 vs 3 on RTF) skips with a hint — not a hard fail
    on misconfigured MNNVL_TOPOLOGY / cluster view.

    Precondition: Cluster enabled, gRPC connection to NMX-C is up.
    """
    cluster, sdn = cluster_and_sdn
    ensure_cluster_enabled(cluster, setup_name, devices)

    with allure.step("Run 'nv show sdn trays' and validate output"):
        trays_output = get_all_trays(sdn)
        tray_ids = list(trays_output.keys())
        assert len(tray_ids) > 0, "No trays shown in 'nv show sdn trays'"

        for tray_id in tray_ids:
            assert ClusterConsts.MAINTENANCE_STATE in trays_output[tray_id], (
                f"Tray {tray_id} missing '{ClusterConsts.MAINTENANCE_STATE}' field")
            state = trays_output[tray_id][ClusterConsts.MAINTENANCE_STATE]
            assert state in VALID_TRAY_STATES, (
                f"Tray {tray_id} has invalid maintenance-state: {state}")

    with allure.step(f"Verify number of trays matches expected setup size ({expected_tray_count})"):
        if len(tray_ids) != expected_tray_count:
            pytest.skip(
                f"Tray count mismatch: expected {expected_tray_count} from lab config "
                f"(switch_nodes={tray_topology_config['switch_nodes']}, "
                f"compute_nodes={tray_topology_config['compute_nodes']}), "
                f"got {len(tray_ids)}: {tray_ids}. Check MNNVL_TOPOLOGY / cluster discovery — "
                f"not failing on environment mismatch.")

    with allure.step("Verify local switch slot-number appears in tray output"):
        local_slot = chassis_info.get('slot-number')
        assert local_slot is not None, "Could not get slot-number from platform chassis-location"
        assert str(local_slot) in tray_ids, (
            f"Local slot {local_slot} not found in tray list {tray_ids}")

    with allure.step("Verify 'nv show sdn trays <slot-id>' matches aggregated output"):
        for tray_id in tray_ids:
            individual_output = get_tray(sdn, tray_id)
            assert ClusterConsts.MAINTENANCE_STATE in individual_output, (
                f"Individual show for tray {tray_id} missing maintenance-state")

            actual_state = individual_output[ClusterConsts.MAINTENANCE_STATE]
            expected_state = trays_output[tray_id][ClusterConsts.MAINTENANCE_STATE]
            assert actual_state == expected_state, (
                f"Tray {tray_id}: individual show state ({actual_state}) != "
                f"aggregated show state ({expected_state})")

    with allure.step("Verify all trays default state is 'up'"):
        for tray_id in tray_ids:
            state = trays_output[tray_id][ClusterConsts.MAINTENANCE_STATE]
            assert state == DEFAULT_TRAY_STATE, (
                f"Tray {tray_id} expected default state '{DEFAULT_TRAY_STATE}', got '{state}'")


# ---------------------------------------------------------------------------
#  4.2 test_tray_change_and_verify_state
# ---------------------------------------------------------------------------


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(25 * MINUTE, func_only=True)
def test_tray_change_and_verify_state(engines, devices, random_api, cluster_and_sdn,
                                      chassis_info, tray_topology_config, setup_name,
                                      standalone_system, has_loopbox):
    """
    Test Objective:
    Verify that the tray maintenance state can be updated to each valid value (up, down, diag)
    and restored to default. After each change, verify the state is reflected in
    nv show sdn trays and port state on the switch (link state, link maintenance-state,
    and Summary column "Maintenance State" in nv show interface).
    Also verify behaviour when local port admin state is down.

    Tray selection (tray_topology_config):
    - Any tray from 'nv show sdn trays' (switch or compute), including the lone switch on RTF.
    - compute_nodes==0 (simx): any tray; no NMX topology gpuTopoInfo parsing.

    RTF compute tray precondition:
    - Delete all SDN partitions (e.g. default 32766) before maintenance-state changes.

    After tray 'down' (switch or compute):
    - Run nvidia-smi -r on all hosts in compute_nodes_per_system before diag/up/restore.

    Port verification (after settle: 60s if bug 5065628 open, else 7s):
    - Local switch tray: all acp ports.
    - compute_nodes==0: all local acps (no topology dig).
    - Compute tray with GPUs in topology: acp list from gpuTopoInfo; missing GPU data -> skip.

    Precondition: Cluster enabled, gRPC connection to NMX-C is up.
    """
    cluster, sdn = cluster_and_sdn
    ensure_cluster_enabled(cluster, setup_name, devices)

    trays_output = get_all_trays(sdn)
    tray_ids = list(trays_output.keys())
    if not tray_ids:
        pytest.skip("No trays in 'nv show sdn trays' — enable cluster and check fm_config")

    local_switch_slot = str(chassis_info.get('slot-number'))
    selected_tray = random.choice(tray_ids)
    is_switch_tray = (selected_tray == local_switch_slot)
    tray_type = "switch" if is_switch_tray else "compute"
    logger.info(
        "Selected tray %s (%s) for state change tests (local switch slot = %s)",
        selected_tray, tray_type, local_switch_slot)

    partitions_deleted = is_rtf_compute_tray_scenario(
        selected_tray, local_switch_slot, tray_topology_config)
    if partitions_deleted:
        delete_sdn_partitions_before_compute_tray_test(sdn)

    affected_acps = get_acp_ports_for_tray(
        engines, sdn, devices, selected_tray, local_switch_slot, tray_topology_config)
    logger.info(
        "%s acp ports should be affected by tray %s state changes",
        len(affected_acps), selected_tray)

    compute_tray_under_test = is_compute_tray(
        selected_tray, local_switch_slot, tray_topology_config)

    # Pick one port for the admin-state override test (single-port test at the end)
    override_test_port = Port(random.choice(affected_acps))

    # GPU reset in finally only if we entered 'down' and have not yet recovered from it.
    needs_gpu_reset = False
    try:
        # Compute tray about to go 'down': the GPUs still hold NVLink resources, so NMX-C
        # rejects the maintenance change with NMX_ST_RESOURCE_IN_USE. Reset the GPUs first
        # to free those resources before requesting 'down'.
        # compute_tray_under_test only: NMX_ST_RESOURCE_IN_USE is for compute trays on
        # 'down'; switch trays are not subject to that error.
        if compute_tray_under_test:
            reset_all_compute_node_gpus(setup_name, cluster)

        # -- Set tray to DOWN: ports should go down, maintenance-state should be 'down' --
        with allure.step(f"Update tray {selected_tray} ({tray_type}) to 'down'"):
            sdn.trays.action_update_maintenance_state(
                tray_id=selected_tray, maintenance_state='down').verify_result()
        needs_gpu_reset = True
        verify_tray_state(sdn, selected_tray, 'down')
        with allure.step(f"Verify all {len(affected_acps)} affected ports: link=down, maintenance=down"):
            verify_affected_ports_state(affected_acps, expected_link_state='down', expected_maintenance_state='down')
        with allure.step(f"Update tray {selected_tray} ({tray_type}) to 'diag'"):
            sdn.trays.action_update_maintenance_state(
                tray_id=selected_tray, maintenance_state='diag').verify_result()
        # 'down' severs all inband paths to GPUs; reset is needed to go back to 'up'.
        reset_all_compute_node_gpus(setup_name, cluster)
        needs_gpu_reset = False
        verify_tray_state(sdn, selected_tray, 'diag')
        with allure.step(f"Verify all {len(affected_acps)} affected ports: link=up, maintenance=diag"):
            verify_affected_ports_state(affected_acps, expected_link_state='up', expected_maintenance_state='diag')

        # -- Set tray to UP: ports should remain up, maintenance-state should be 'up' --
        with allure.step(f"Update tray {selected_tray} ({tray_type}) to 'up'"):
            sdn.trays.action_update_maintenance_state(
                tray_id=selected_tray, maintenance_state='up').verify_result()
        verify_tray_state(sdn, selected_tray, 'up')
        with allure.step(f"Verify all {len(affected_acps)} affected ports: link=up, maintenance=up"):
            verify_affected_ports_state(affected_acps, expected_link_state='up', expected_maintenance_state='up')

        # -- Random non-default state, then restore to default --
        random_state = random.choice(['down', 'diag'])
        with allure.step(f"Set tray {selected_tray} to '{random_state}' then restore"):
            # Same as the first 'down': free GPU NVLink resources before a compute tray
            # goes 'down', else NMX-C returns NMX_ST_RESOURCE_IN_USE.
            # compute_tray_under_test only: that error is for compute trays on 'down'/'diag'.
            if compute_tray_under_test and random_state == 'down':
                reset_all_compute_node_gpus(setup_name, cluster)
            sdn.trays.action_update_maintenance_state(
                tray_id=selected_tray, maintenance_state=random_state).verify_result()
            if random_state == 'down':
                needs_gpu_reset = True
            verify_tray_state(sdn, selected_tray, random_state)

            sdn.trays.action_restore_maintenance_state(tray_id=selected_tray).verify_result()
            verify_tray_state(sdn, selected_tray, DEFAULT_TRAY_STATE)
            # 'down' severs all inband paths to GPUs; reset is needed to go back to 'up'.
            if random_state == 'down':
                reset_all_compute_node_gpus(setup_name, cluster)
                needs_gpu_reset = False
        with allure.step(f"Verify all {len(affected_acps)} affected ports: link=up, maintenance=up after restore"):
            verify_affected_ports_state(affected_acps, expected_link_state='up', expected_maintenance_state='up')

        # -- Verify port admin-state DOWN overrides tray diag --
        # Per HLD: if a port's local admin state is down, tray diag should NOT bring it up.
        # This is a single-port test since it requires per-port config changes.
        with allure.step("Verify local port admin-state 'down' overrides tray 'diag'"):
            logger.info(f"Port for admin-state override test: {override_test_port.name}")

            with allure.step(f"Set port {override_test_port.name} link state to down"):
                override_test_port.interface.link.set(op_param_name='state', op_param_value='down', apply=True)
            Port.wait_for_port_state(override_test_port, expected_state='down')

            with allure.step(f"Set tray {selected_tray} to diag"):
                sdn.trays.action_update_maintenance_state(
                    tray_id=selected_tray, maintenance_state='diag').verify_result()

            with allure.step(f"Verify port {override_test_port.name} stays down (admin-state overrides tray diag)"):
                Port.wait_for_port_state(override_test_port, expected_state='down')

            with allure.step("Cleanup: unset port link state and restore tray"):
                override_test_port.interface.link.unset(op_param='state', apply=True)
                sdn.trays.action_restore_maintenance_state(tray_id=selected_tray).verify_result()

    finally:
        restore_tray_cleanup(sdn, selected_tray, setup_name, cluster, reset_gpu=needs_gpu_reset)
        # We destructively deleted the default SDN partition (32766) as a precondition.
        # Restore it the same way the dedicated partition test does: an SDN factory reset
        # makes NMX-C recreate the default partition (test_cluster_partition.py:86-101).
        if partitions_deleted:
            with allure.step("Restore deleted SDN partition(s) via SDN factory reset"):
                ClusterTools.reset_sdn_factory_default_and_wait_for_restart(sdn, cluster)


# ---------------------------------------------------------------------------
#  4.3 test_tray_multi_tray_independence
# ---------------------------------------------------------------------------


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_tray_multi_tray_independence(engines, devices, random_api, cluster_and_sdn,
                                      chassis_info, tray_topology_config, setup_name,
                                      standalone_system, has_loopbox):
    """
    Test Objective:
    Verify that setting maintenance state on one tray does not affect other trays.

    On single-switch setups (RTF), both trays are compute trays — the lone switch is never used.

    Precondition: Cluster enabled, setup has >= 2 eligible trays.
    """
    cluster, sdn = cluster_and_sdn
    ensure_cluster_enabled(cluster, setup_name, devices)

    trays_output = get_all_trays(sdn)
    tray_ids = list(trays_output.keys())
    local_switch_slot = str(chassis_info.get('slot-number'))
    tray_a, tray_b = pick_two_trays_for_independence_test(
        tray_ids, local_switch_slot, tray_topology_config)
    state_a = random.choice(VALID_TRAY_STATES)
    state_b = random.choice(VALID_TRAY_STATES)
    logger.info(f"Tray A={tray_a} -> {state_a}, Tray B={tray_b} -> {state_b}")

    # Free GPU NVLink resources before a tray goes 'down', else NMX-C may return
    # NMX_ST_RESOURCE_IN_USE. (reset_all_compute_node_gpus resets all compute nodes.)
    if state_a == 'down' or state_b == 'down':
        reset_all_compute_node_gpus(setup_name, cluster)

    try:
        local_switch_slot = str(chassis_info.get('slot-number'))
        partitions_deleted_a = is_rtf_compute_tray_scenario(tray_a, local_switch_slot, tray_topology_config)
        partitions_deleted_b = is_rtf_compute_tray_scenario(tray_b, local_switch_slot, tray_topology_config)
        if partitions_deleted_a or partitions_deleted_b:
            delete_sdn_partitions_before_compute_tray_test(sdn)
        with allure.step(f"Set tray {tray_a} to '{state_a}' and tray {tray_b} to '{state_b}'"):
            sdn.trays.action_update_maintenance_state(
                tray_id=tray_a, maintenance_state=state_a).verify_result()
            sdn.trays.action_update_maintenance_state(
                tray_id=tray_b, maintenance_state=state_b).verify_result()

        with allure.step("Verify each tray has its own independent state"):
            verify_tray_state(sdn, tray_a, state_a)
            verify_tray_state(sdn, tray_b, state_b)

    finally:
        with allure.step("Restore both trays"):
            reset_gpu = (state_a == 'down' or state_b == 'down')
            try:
                restore_tray_cleanup(sdn, tray_a, setup_name, cluster)
            finally:
                restore_tray_cleanup(sdn, tray_b, setup_name, cluster, reset_gpu=reset_gpu)

        with allure.step("Verify both trays restored to default"):
            verify_tray_state(sdn, tray_a, DEFAULT_TRAY_STATE)
            verify_tray_state(sdn, tray_b, DEFAULT_TRAY_STATE)


# ---------------------------------------------------------------------------
#  6.1 test_tray_bad_params
# ---------------------------------------------------------------------------


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_tray_bad_params(engines, devices, random_api, cluster_and_sdn, chassis_info,
                         setup_name, standalone_system, has_loopbox):
    """
    Test Objective:
    Verify that invalid parameters are rejected by the tray maintenance state commands.

    Precondition: Cluster enabled, gRPC connection to NMX-C is up.
    """
    cluster, sdn = cluster_and_sdn
    ensure_cluster_enabled(cluster, setup_name, devices)
    random_string = RandomizationTool.get_random_string(length=10)

    with allure.step("Show trays with invalid slot-id"):
        sdn.trays.show(random_string, should_succeed=False, exempted_err_msgs=["is not a", "Error", "Invalid"])

    with allure.step("Update tray with valid slot-id but invalid maintenance-state value"):
        trays_output = get_all_trays(sdn)
        valid_tray_id = random.choice(list(trays_output.keys()))
        result = sdn.trays.action_update_maintenance_state(
            tray_id=valid_tray_id, maintenance_state=random_string)
        result.verify_result(should_succeed=False, expected_value=["Error", "Invalid Command"])

    with allure.step("Update tray with non-existent slot-id"):
        # A valid-format but non-existent slot-id is rejected by NMX-C
        result = sdn.trays.action_update_maintenance_state(
            tray_id='999', maintenance_state='down')
        expected_value = ["Error", "Action failed"]
        if random_api == ApiType.OPENAPI:
            expected_value = ["action_error"]
        result.verify_result(should_succeed=False, expected_value=expected_value)


# ---------------------------------------------------------------------------
#  6.2 test_tray_rejected_without_cluster
# ---------------------------------------------------------------------------


@pytest.mark.nmx
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_tray_rejected_without_cluster(random_api, standalone_system):
    """
    Test Objective:
    Verify that tray admin state commands are rejected when cluster is not enabled.

    Precondition: Cluster is disabled on the switch.
    """
    if standalone_system:
        pytest.skip("Tray admin state tests require non-standalone setup")

    cluster = Cluster()
    sdn = Sdn()
    with allure.step("Ensure cluster is disabled"):
        cluster.unset(apply=True)
        ClusterTools.wait_for_apps_to_be_in_wanted_state(
            cluster, cluster_expected_state='disabled', nmx_c_expected_state='down')

    with allure.step("Verify 'nv show sdn trays' shows empty output with cluster disabled"):
        output = sdn.trays.show()
        assert output.strip() == '{}', (
            f"Expected output to be empty, got: {output}")

    with allure.step("Verify update maintenance-state fails with cluster disabled"):
        random_state = random.choice(VALID_TRAY_STATES)
        output = sdn.trays.action_update_maintenance_state(
            tray_id='9', maintenance_state=random_state)
        output.verify_result(should_succeed=False, expected_value=ClusterConsts.CLUSTER_NOT_ENABLED_ERR)

    with allure.step("Verify restore maintenance-state fails with cluster disabled"):
        output = sdn.trays.action_restore_maintenance_state(tray_id='9')
        output.verify_result(should_succeed=False, expected_value=ClusterConsts.CLUSTER_NOT_ENABLED_ERR)


# ---------------------------------------------------------------------------
#  6.3 test_tray_not_supported_on_juliet
# ---------------------------------------------------------------------------


@pytest.mark.nmx
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_tray_not_supported_on_juliet(engines, devices, random_api, setup_name,
                                      standalone_system, has_loopbox):
    """
    Test Objective:
    Verify that tray admin state commands are not supported on GB200/Juliet systems.
    Only VR200/Rosalind and later are supported.

    Precondition: Cluster enabled. Device under test is Juliet (non-Rosalind).
    """
    if isinstance(devices.dut, RosalindSwitch):
        pytest.skip("This test targets Juliet (non-Rosalind) systems only")
    if not isinstance(devices.dut, JulietSwitch):
        pytest.skip("This test targets Juliet systems only")

    cluster = Cluster()
    sdn = Sdn()
    ensure_cluster_enabled(cluster, setup_name, devices)

    with allure.step("Verify 'nv show sdn trays' not supported on Juliet"):
        output = sdn.trays.show(should_succeed=False)
        logger.info(f"Show trays on Juliet: {output}")

    with allure.step("Verify update tray state not supported on Juliet"):
        output = sdn.trays.action_update_maintenance_state(
            tray_id='9', maintenance_state='up').verify_result(should_succeed=False)
        logger.info(f"Update tray on Juliet: {output}")
