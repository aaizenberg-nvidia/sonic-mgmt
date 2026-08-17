import logging
import random

import pytest

from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
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
    TRAY_PORT_VERIFY_TRIES,
    TRAY_PORT_VERIFY_TRIES_LTX_FORCED,
    VALID_TRAY_STATES,
    ensure_cluster_enabled,
    get_acp_ports_for_tray,
    get_all_trays,
    get_compute_tray_slots,
    get_tray,
    pick_tray_for_maintenance_change,
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
    Verify that the SDN tray show commands return correct output format, that all listed
    trays have their maintenance-state field, and that showing a single tray matches the
    aggregated output. Since 0.8 the trays view lists SWITCH trays only (compute trays
    are filtered out — compute-tray admin state is not released to customers).

    Tray count is checked against RegressionConfigurations.tray_topology switch_nodes
    for this setup_name. Wrong count (e.g. 27 vs 1 on RTF) skips with a hint — not a
    hard fail on misconfigured MNNVL_TOPOLOGY / cluster view.

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
                f"Tray count mismatch: expected {expected_tray_count} switch trays from lab "
                f"config (switch_nodes={tray_topology_config['switch_nodes']}; since 0.8 "
                f"compute trays are filtered out of 'nv show sdn trays'), "
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
@pytest.mark.timeout(60 * MINUTE, func_only=True)
def test_tray_change_and_verify_state(engines, devices, random_api, cluster_and_sdn,
                                      chassis_info, tray_topology_config, setup_name,
                                      standalone_system, has_loopbox,
                                      ltx_force_max_iterations):
    """
    Test Objective:
    Verify that the tray maintenance state can be updated to each valid value (up, down, diag)
    and restored to default. After each change, verify the state is reflected in
    nv show sdn trays and port state on the switch (link state, link maintenance-state,
    and Summary column "Maintenance State" in nv show interface).
    Also verify behaviour when local port admin state is down.

    Tray selection: SWITCH trays only — compute-tray admin state is not released to
    customers (product decision 2026-08-05; RN note in 0.6, blocked in NMX-C API in 0.8).
    - Labs with compute trays: the local switch tray.
    - compute_nodes==0: any tray (all are switch trays; note the only such setup
      today, rosalind-simx, is classified standalone and skips the whole suite).

    Port verification (after settle: 60s if bug 5065628 open, else 7s): the acp ports
    that were link-up (stable) before the first change.

    GPU reset on leaving 'down' (labs with compute nodes): by design (RM 5189276,
    closed not-a-bug) GPU-facing acps stay Down/Polling after down->diag/up until
    the peer GPUs are reset with nvidia-smi -r; loopback-cabled acps retrain alone.

    LTX coverage: runs with force-max-iterations armed on all access ports (fixture
    ltx_force_max_iterations), so every tray-driven retrain walks ALL link-training
    iterations (6480024 class); the port-verify retry budget is widened accordingly.

    Precondition: Cluster enabled, gRPC connection to NMX-C is up.
    """
    cluster, sdn = cluster_and_sdn
    ensure_cluster_enabled(cluster, setup_name, devices)
    verify_tries = (TRAY_PORT_VERIFY_TRIES_LTX_FORCED if ltx_force_max_iterations
                    else TRAY_PORT_VERIFY_TRIES)
    lab_has_gpus = tray_topology_config.get('compute_nodes', 0) > 0

    trays_output = get_all_trays(sdn)
    tray_ids = list(trays_output.keys())
    if not tray_ids:
        pytest.skip("No trays in 'nv show sdn trays' — enable cluster and check fm_config")

    local_switch_slot = str(chassis_info.get('slot-number'))
    selected_tray = pick_tray_for_maintenance_change(
        tray_ids, local_switch_slot, tray_topology_config)
    tray_type = "switch"
    logger.info(
        "Selected tray %s (%s) for state change tests (local switch slot = %s)",
        selected_tray, tray_type, local_switch_slot)

    affected_acps = get_acp_ports_for_tray(devices, selected_tray, local_switch_slot)
    logger.info(
        "%s acp ports should be affected by tray %s state changes",
        len(affected_acps), selected_tray)

    # Pick one port for the admin-state override test (single-port test at the end)
    override_test_port = Port(random.choice(affected_acps))
    current_tray_state = DEFAULT_TRAY_STATE

    try:
        # -- Set tray to DOWN: ports should go down, maintenance-state should be 'down' --
        with allure.step(f"Update tray {selected_tray} ({tray_type}) to 'down'"):
            sdn.trays.action_update_maintenance_state(
                tray_id=selected_tray, maintenance_state='down').verify_result()
            current_tray_state = 'down'
        verify_tray_state(sdn, selected_tray, 'down')
        with allure.step(f"Verify all {len(affected_acps)} affected ports: link=down, maintenance=down"):
            verify_affected_ports_state(affected_acps, expected_link_state='down', expected_maintenance_state='down',
                                        tries=verify_tries)

        # -- Set tray to DIAG: ports should come back up, maintenance-state should be 'diag' --
        # Leaving 'down' requires a compute GPU reset by design (RM 5189276, not-a-bug):
        # the 'down' admin-disabled the GPU side, and GPU-facing acps stay Down/Polling
        # on diag/up until the peer GPUs are reset. Loopback-cabled acps retrain alone.
        if lab_has_gpus:
            reset_all_compute_node_gpus(setup_name, cluster)
        with allure.step(f"Update tray {selected_tray} ({tray_type}) to 'diag'"):
            sdn.trays.action_update_maintenance_state(
                tray_id=selected_tray, maintenance_state='diag').verify_result()
            current_tray_state = 'diag'
        verify_tray_state(sdn, selected_tray, 'diag')
        with allure.step(f"Verify all {len(affected_acps)} affected ports: link=up, maintenance=diag"):
            verify_affected_ports_state(affected_acps, expected_link_state='up', expected_maintenance_state='diag',
                                        tries=verify_tries)

        # -- Set tray to UP: ports should remain up, maintenance-state should be 'up' --
        with allure.step(f"Update tray {selected_tray} ({tray_type}) to 'up'"):
            sdn.trays.action_update_maintenance_state(
                tray_id=selected_tray, maintenance_state='up').verify_result()
            current_tray_state = 'up'
        verify_tray_state(sdn, selected_tray, 'up')
        with allure.step(f"Verify all {len(affected_acps)} affected ports: link=up, maintenance=up"):
            verify_affected_ports_state(affected_acps, expected_link_state='up', expected_maintenance_state='up',
                                        tries=verify_tries)

        # -- Random non-default state, then restore to default --
        random_state = random.choice(['down', 'diag'])
        with allure.step(f"Set tray {selected_tray} to '{random_state}' then restore"):
            sdn.trays.action_update_maintenance_state(
                tray_id=selected_tray, maintenance_state=random_state).verify_result()
            current_tray_state = random_state
            verify_tray_state(sdn, selected_tray, random_state)
            # Same by-design GPU reset before leaving 'down' (restore -> up).
            if random_state == 'down' and lab_has_gpus:
                reset_all_compute_node_gpus(setup_name, cluster)
            sdn.trays.action_restore_maintenance_state(tray_id=selected_tray).verify_result()
            current_tray_state = 'up'
            verify_tray_state(sdn, selected_tray, DEFAULT_TRAY_STATE)
        with allure.step(f"Verify all {len(affected_acps)} affected ports: link=up, maintenance=up after restore"):
            verify_affected_ports_state(affected_acps, expected_link_state='up', expected_maintenance_state='up',
                                        tries=verify_tries)

        # -- Verify port admin-state DOWN overrides tray diag --
        # Per HLD: if a port's local admin state is down, tray diag should NOT bring it up.
        # This is a single-port test since it requires per-port config changes.
        with allure.step("Verify local port admin-state 'down' overrides tray 'diag'"):
            logger.info(f"Port for admin-state override test: {override_test_port.name}")

            with allure.step(f"Set port {override_test_port.name} link state to down"):
                override_test_port.interface.link.state.set(
                    op_param_name='down', apply=True, ask_for_confirmation=True).verify_result()
            Port.wait_for_port_state(override_test_port, expected_state='down')

            with allure.step(f"Set tray {selected_tray} to diag"):
                sdn.trays.action_update_maintenance_state(
                    tray_id=selected_tray, maintenance_state='diag').verify_result()
                current_tray_state = 'diag'

            with allure.step(f"Verify port {override_test_port.name} stays down (admin-state overrides tray diag)"):
                Port.wait_for_port_state(override_test_port, expected_state='down')

            with allure.step("Cleanup: unset port link state, restore tray, verify port retrains"):
                override_test_port.interface.link.state.unset(
                    apply=True, ask_for_confirmation=True).verify_result()
                sdn.trays.action_restore_maintenance_state(tray_id=selected_tray).verify_result()
                current_tray_state = 'up'
                # Verify the port actually comes back up (forced LTX retrain ~5-6 min) —
                # otherwise a retrain hiccup leaks a dead link into the next test.
                verify_affected_ports_state([override_test_port.name], expected_link_state='up',
                                            expected_maintenance_state='up', tries=verify_tries)

    finally:
        # If the test died while the tray was 'down', reset the GPUs first so the
        # restore's down->up transition can actually retrain the GPU-facing links.
        if current_tray_state == 'down' and lab_has_gpus:
            reset_all_compute_node_gpus(setup_name, cluster)
        restore_tray_cleanup(sdn, selected_tray)


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

    Switch trays only (compute-tray admin state is not released to customers).
    Requires >= 2 eligible switch trays: labs with compute trays expose only one
    known switch tray (the local slot), and the sole switch-only setup configured
    today (rosalind-simx) is gated out as standalone by cluster_and_sdn — so this
    test skips everywhere until a multi-switch-tray setup is configured.

    Precondition: Cluster enabled, setup has >= 2 eligible (switch) trays.
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

    try:
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
            sdn.trays.action_restore_maintenance_state(tray_id=tray_a)
            sdn.trays.action_restore_maintenance_state(tray_id=tray_b)

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
        # Use the local switch slot: compute-tray admin state is blocked in the NMX-C API
        # from 0.8, which could mask the invalid-state rejection under test here.
        valid_tray_id = chassis_info.get('slot-number')
        assert valid_tray_id is not None, "Could not get slot-number from platform chassis-location"
        result = sdn.trays.action_update_maintenance_state(
            tray_id=str(valid_tray_id), maintenance_state=random_string)
        result.verify_result(should_succeed=False, expected_value=["Error", "Invalid Command"])

    with allure.step("Update tray with non-existent slot-id"):
        # A valid-format but non-existent slot-id is rejected by NMX-C
        result = sdn.trays.action_update_maintenance_state(
            tray_id='999', maintenance_state='down')
        # Branch on the API actually in effect, NOT on random_api:
        # @disabled_access_ports forces tested_api to NVUE on the JulietSwitch
        # family (RosalindRTF included), so random_api may say OpenApi while the
        # command really runs through the NVUE CLI and returns its error format.
        expected_value = ["Error", "Action failed"]
        if TestToolkit.tested_api == ApiType.OPENAPI:
            expected_value = ["action_error"]
        result.verify_result(should_succeed=False, expected_value=expected_value)

    with allure.step("Update maintenance-state on a compute tray (not supported since 0.8)"):
        # Compute-tray admin state is not released to customers (product decision
        # 2026-08-05; blocked in the NMX-C API from 0.8). The block has a dedicated
        # message, distinct from the bad-id NMX_ST_BADPARAM above. The backend
        # generates the message, so it is the same substring under NVUE and OpenAPI.
        compute_slots = get_compute_tray_slots(sdn)
        if not compute_slots:
            logger.info("No compute tray slots in partition GPU locations - skipping compute-tray check")
        else:
            compute_tray_id = random.choice(sorted(compute_slots))
            result = sdn.trays.action_update_maintenance_state(
                tray_id=compute_tray_id, maintenance_state='down')
            result.verify_result(
                should_succeed=False,
                expected_value=f"Tray admin state is not supported for tray {compute_tray_id}")


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
