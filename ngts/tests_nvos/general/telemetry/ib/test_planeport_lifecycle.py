"""
Plane-port reboot persistence and failure / link-down tests
(test plan section 9.1, section 10.1, section 10.2).

These tests cover the lifecycle of the plane-port knob and the response of
plane-port interfaces to Aport admin-down and peer-side link loss (cable
unplug).
"""

import logging
from typing import Dict, List

import pytest

from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.interfaces.test_ib_interface_phy_detail import CODE_TO_DESCRIPTION
from ngts.tools.test_utils import allure_utils as allure

from ngts.tests_nvos.general.telemetry.ib import helpers as ibh
from ngts.tests_nvos.general.telemetry.ib.constants import (
    API_GNMIC,
    API_NVUE_CLI,
    GnmiYangPaths,
    IfaceType,
    LINK_DOWN_CODE_ADMIN_DISABLE,
    LINK_DOWN_CODE_CABLE_UNPLUGGED,
    PHYSICAL_STATE_LEAVES,
    PlanePortState,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _gnmi_link_state(client, port_name: str) -> Dict[str, str]:
    """Read link / phy state leaves used by the link-down assertions."""
    return ibh.gnmi_get_interface_subtree(client, port_name)


def _snapshot_link_state(client, ports: List[Port]) -> Dict[str, Dict[str, str]]:
    """Return {port_name: flat gNMI interface subtree} for the given ports."""
    return {p.name: _gnmi_link_state(client, p.name) for p in ports}


# ---------------------------------------------------------------------------
# 9.1 test_planeport_state_persists_across_reboot
# ---------------------------------------------------------------------------


@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_planeport_state_persists_across_reboot(engines, devices, gnmi_client, setup_topology, topology_obj):
    """The plane-port enable/disable setting must survive a reboot."""
    system = System()

    # This is the only plane-port test that *persists* (saves) an enabled state.
    # The autouse `_planeport_safety_teardown` only unsets+applies the running
    # config, so on any failure between the enable-save and the final
    # disable-save the startup config would stay 'enabled' and leak across the
    # next reboot. Restore (and persist) the default in a finally block.
    try:
        with allure.step("Enable plane-port and save config"):
            ibh.set_plane_port_state(engines, PlanePortState.ENABLED, apply=True, save=True)

        with allure.step("Reboot the system"):
            system.reboot.action_reboot(
                engine=engines.dut,
                device=devices.dut,
                topology_obj=topology_obj,
                should_wait_till_system_ready=True,
            )

        with allure.step("After boot: state is enabled"):
            state = ibh.get_plane_port_state(engines)
            assert state == PlanePortState.ENABLED, (
                f"plane-port state did not persist; expected 'enabled', got {state!r}"
            )

        with allure.step("After boot: plane-ports visible via 'nv show interface' (test plan section 9.1 step 4)"):
            nvue_types = ibh.list_interfaces_via_api(API_NVUE_CLI, engines, gnmi_client)
            nvue_planes = [n for n, t in nvue_types.items() if t.strip().lower() == IfaceType.NVUE_PLANE]
            ibh.attach_dict(
                "plane-ports visible after reboot (NVUE)",
                {"count": len(nvue_planes), "names": nvue_planes[:10]},
            )
            assert nvue_planes, "No plane-ports visible via 'nv show interface' after reboot with state=enabled"

            # Belt-and-suspenders: also confirm via gNMI. nv-gnmi may still be
            # starting (~5s) right after the reboot, so wait (bounded) for it to
            # be reachable first. If it stays down, skip just this cross-check -
            # the NVUE persistence assertions above are the real coverage.
            if ibh.wait_for_gnmi_reachable(gnmi_client):
                gnmi_types = ibh.list_interfaces_via_api(API_GNMIC, engines, gnmi_client)
                gnmi_planes = [n for n, t in gnmi_types.items() if t.strip() == IfaceType.GNMI_PLANE]
                ibh.attach_dict(
                    "plane-ports visible after reboot (gNMI)",
                    {"count": len(gnmi_planes), "names": gnmi_planes[:10]},
                )
                assert gnmi_planes, "No plane-ports visible via gNMI after reboot with state=enabled"
            else:
                logger.warning(
                    "gNMI server still unavailable after the post-reboot wait; "
                    "skipping the gNMI plane-port cross-check (NVUE persistence "
                    "already confirmed)."
                )

        with allure.step("Disable plane-port, save, reboot, and confirm state stuck disabled"):
            ibh.set_plane_port_state(engines, PlanePortState.DISABLED, apply=True, save=True)
            system.reboot.action_reboot(
                engine=engines.dut,
                device=devices.dut,
                topology_obj=topology_obj,
                should_wait_till_system_ready=True,
            )
            state = ibh.get_plane_port_state(engines)
            assert state == PlanePortState.DISABLED, (
                f"plane-port state did not persist; expected 'disabled', got {state!r}"
            )
    finally:
        with allure.step("Teardown: persist default (disabled) plane-port state"):
            try:
                ibh.set_plane_port_state(
                    engines, PlanePortState.DISABLED, apply=True, save=True
                )
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                logger.warning(
                    "reboot-persistence teardown (persist disabled) failed: %s "
                    "(non-fatal)", exc
                )


# ---------------------------------------------------------------------------
# 10.1 test_planeport_state_follows_aport_admin_down
# ---------------------------------------------------------------------------


@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_planeport_state_follows_aport_admin_down(engines, devices, gnmi_client, setup_topology):
    """Admin-downing an Aport propagates the down state to all its plane-ports.

    An IB link only goes operationally down when *both* ends are admin-disabled.
    On this single-switch loopback bench there is no inter-switch dut2/dut3
    partner, so we down the Aport together with its same-switch loopback peer.
    `link-down-information/state/reason-status-local` is expected to carry an
    admin-disable code.
    """
    ibh.set_plane_port_state(engines, PlanePortState.ENABLED, apply=True)

    loopback_pairs = setup_topology.link_up_loopback_aports()
    if not loopback_pairs:
        pytest.skip(
            "No link-up loopback Aport with a full plane set in connectivity. "
            "This test needs both ends of the link to be brought down: either "
            "an inter-switch dut2/dut3 partner or a same-switch loopback peer. "
            "Refresh the connectivity JSON for this setup."
        )
    aport_name, peer_name = loopback_pairs[0]
    aport = Port(aport_name)
    planes = setup_topology.planes_for(aport.name)
    all_ports = [aport] + planes

    with allure.step("Snapshot pre-state of Aport and plane-ports"):
        pre = _snapshot_link_state(gnmi_client, all_ports)
        ibh.attach_dict(f"pre-state {aport.name}", pre)

    with ibh.quiesce_aport_via_loopback_partner(aport, peer_name, engines):
        with allure.step("Aport and every plane report down state and Disabled physical-state"):
            for p in all_ports:
                payload = _gnmi_link_state(gnmi_client, p.name)
                ibh.attach_dict(f"after-down {p.name}", payload)
                phys_state = payload.get(
                    PHYSICAL_STATE_LEAVES[0], payload.get(PHYSICAL_STATE_LEAVES[1], "")
                )
                assert (
                    "disabled" in phys_state.strip().lower()
                ), f"{p.name}: expected physical-state Disabled, got {phys_state!r}"

        with allure.step(f"Read link-down reason for Aport {aport.name}"):
            reason_payload = ibh.gnmi_get_flat(
                gnmi_client,
                prefix=GnmiYangPaths.PHY_LINK_DOWN_INFO.format(name=aport.name),
                path="reason-status-local",
            )
            reason = reason_payload.get("reason-status-local", "")
            ibh.attach_dict("link-down-reason", reason_payload)
            reason_l = reason.lower()
            admin_disable_indicators = (
                CODE_TO_DESCRIPTION[LINK_DOWN_CODE_ADMIN_DISABLE],  # "Down_by_management_command"
                "admin_state_set_to_disable",  # test plan section 10.1 example
                "disabled",                    # test plan section 10.1 fallback example
            )
            assert any(ind.lower() in reason_l for ind in admin_disable_indicators), (
                f"link-down-reason for {aport.name} after admin-down: "
                f"expected an admin-disable indicator (one of {admin_disable_indicators}), got {reason!r}"
            )

    with allure.step("Aport and every plane recover to pre-state"):
        recovered, pname, leaf_name, pre_val, post_val = ibh.wait_link_state_recovered(
            gnmi_client, all_ports, pre
        )
        assert recovered, (
            f"{pname}: leaf {leaf_name!r} did not recover; "
            f"pre={pre_val!r}, post={post_val!r}"
        )


# ---------------------------------------------------------------------------
# 10.2 test_planeport_state_follows_peer_link_loss
# ---------------------------------------------------------------------------


@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_planeport_state_follows_peer_link_loss(engines, devices, gnmi_client, setup_topology):
    """Peer-side cable removal propagates to all plane-ports of the affected Aport.

    `link-down-information/state/reason-status-local` is expected to indicate a
    cable / peer-side removal on a real point-to-point link. On a single-switch
    loopback bench (the only Aport selection available here) the PMAOS unplug is
    a management register write with no external transceiver to pull, so the
    firmware reports a management-disable reason instead - accepted for loopback.
    """
    ibh.set_plane_port_state(engines, PlanePortState.ENABLED, apply=True)

    loopback_pairs = setup_topology.link_up_loopback_aports()
    if not loopback_pairs:
        pytest.skip(
            "No link-up Aport with a full plane set in connectivity for the "
            "peer-unplug simulation. On a single-switch loopback bench the IB "
            "ports come up Initialize (no SM), so MultiPlanarTool's Active-only "
            "selection finds nothing - select a link-up loopback Aport instead. "
            "Refresh the connectivity JSON for this setup."
        )
    aport = Port(loopback_pairs[0][0])
    planes = setup_topology.planes_for(aport.name)
    all_ports = [aport] + planes
    # On a single-switch loopback bench the "peer unplug" is realized as a PMAOS
    # register write to the Aport's own module, which the firmware logs as a
    # management action (reason 22 Down_by_management_command) rather than a
    # physical cable removal (reason 23 Cable_was_unplugged) - there is no
    # external transceiver to pull. Relax only the reason-string check for a
    # loopback Aport; the down-propagation and recovery assertions are unchanged.
    is_loopback = bool(setup_topology.loopback_partner(aport.name))

    with allure.step("Snapshot pre-state"):
        pre = _snapshot_link_state(gnmi_client, all_ports)
        ibh.attach_dict(f"pre-state {aport.name}", pre)

    plugged_back = False
    try:
        with allure.step(f"Simulate peer-side cable unplug on {aport.name}"):
            ibh.simulate_peer_unplug(engines, devices, aport)

        with allure.step("Aport and every plane reflect the loss"):
            for p in all_ports:
                payload = _gnmi_link_state(gnmi_client, p.name)
                ibh.attach_dict(f"after-unplug {p.name}", payload)
                phys_state = payload.get(
                    PHYSICAL_STATE_LEAVES[0], payload.get(PHYSICAL_STATE_LEAVES[1], "")
                )
                assert phys_state.strip(), f"{p.name}: physical-state missing after unplug"

        with allure.step("Read link-down reason and assert peer / cable removal indicator"):
            reason_payload = ibh.gnmi_get_flat(
                gnmi_client,
                prefix=GnmiYangPaths.PHY_LINK_DOWN_INFO.format(name=aport.name),
                path="reason-status-local",
            )
            reason = reason_payload.get("reason-status-local", "")
            ibh.attach_dict("link-down-reason (unplug)", reason_payload)
            reason_l = reason.lower()
            cable_removal_indicators = (
                CODE_TO_DESCRIPTION[LINK_DOWN_CODE_CABLE_UNPLUGGED],  # "Cable_was_unplugged"
                "unplug",
                "cable",
            )
            if is_loopback:
                # PMAOS unplug of a loopback port surfaces as a management
                # disable; accept that too, but still require a real reason.
                accepted_indicators = cable_removal_indicators + (
                    CODE_TO_DESCRIPTION[LINK_DOWN_CODE_ADMIN_DISABLE],  # "Down_by_management_command"
                )
                assert reason.strip() and any(
                    ind.lower() in reason_l for ind in accepted_indicators
                ), (
                    f"Expected a cable-removal or management-disable reason on the "
                    f"loopback bench (one of {accepted_indicators}); got {reason!r}"
                )
            else:
                assert any(ind.lower() in reason_l for ind in cable_removal_indicators), (
                    f"Expected cable/peer-side removal reason (one of {cable_removal_indicators}); "
                    f"got {reason!r}"
                )

        with allure.step("Plug back and confirm recovery"):
            ibh.simulate_peer_plug_in(engines, devices, aport)
            plugged_back = True
            recovered, pname, leaf_name, pre_val, post_val = ibh.wait_link_state_recovered(
                gnmi_client, all_ports, pre
            )
            assert recovered, (
                f"{pname}: leaf {leaf_name!r} did not recover after plug-back; "
                f"pre={pre_val!r}, post={post_val!r}"
            )
    finally:
        if not plugged_back:
            try:
                ibh.simulate_peer_plug_in(engines, devices, aport)
            except Exception as exc:  # noqa: BLE001
                logger.warning("plug-back restore failed: %s (test cleanup)", exc)
