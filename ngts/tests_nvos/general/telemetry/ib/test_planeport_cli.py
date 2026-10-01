"""
Plane-port CLI / NVUE configuration tests (test plan section 5.1 - section 5.4).

These tests drive the new NVUE knob `nv set system plane-port state ...`
and validate the user-facing surface across NVUE, gNMI, and OTEL.

Covered:
- 5.1 test_planeport_state_default_disabled
- 5.2 test_planeport_state_enable_disable
- 5.3 test_planeport_type_leaf_and_api_parity
- 5.4 test_aport_counters_unchanged_with_planeport_enabled
"""

import logging
import time
from typing import Dict, List

import pytest

from ngts.cli_wrappers.common.general_clis_common import GeneralCliCommon
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.MultiPlanarTool import MultiPlanarTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure

from ngts.tests_nvos.general.telemetry.ib import helpers as ibh
from ngts.tests_nvos.general.telemetry.ib import umf_churn_helpers as umf_churn
from ngts.tests_nvos.general.telemetry.ib.constants import (
    ALL_APIS,
    API_GNMIC,
    API_NVUE_CLI,
    EXTENDED_COUNTER_FIELDS,
    GNMI_PACKET_OCTET_LEAVES,
    GnmiYangPaths,
    IfaceType,
    NVBUG_6152697,
    PlanePortState,
    PLANE_PORT_TOGGLE_CYCLES,
    PLANE_PORT_TOGGLE_SETTLE_SEC,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local utilities (tiny enough not to live in helpers.py)
# ---------------------------------------------------------------------------

# Telemetry containers that must survive plane-port toggling untouched.
_RESTART_MONITORED_CONTAINERS = ("sym-mgr", "gpu-telemetry")


def _assert_no_plane_ports(api: str, iface_types: Dict[str, str]) -> None:
    """Per `api`, assert no interface is reported as a plane-port type."""
    plane_type = ibh.expected_type_leaf(api, is_plane=True).lower()
    present_types = {str(t).strip().lower() for t in iface_types.values()}
    # validate_set_disjoint (not validate_subset_in_superset(should_be_included=False)):
    # the latter logs "Validation failed: subset not fully included" even on the
    # expected pass, since its message ignores should_be_included.
    ValidationTool.validate_set_disjoint({plane_type}, present_types).verify_result()


def _assert_some_plane_ports(api: str, iface_types: Dict[str, str]) -> None:
    """Per `api`, assert at least one interface is reported as a plane-port type."""
    plane_type = ibh.expected_type_leaf(api, is_plane=True).lower()
    present_types = {str(t).strip().lower() for t in iface_types.values()}
    ValidationTool.validate_subset_in_superset(
        {plane_type}, present_types, should_be_included=True
    ).verify_result()


def _present_plane_port_names(api: str, iface_types: Dict[str, str]) -> List[str]:
    """Names from the listing whose type is the plane-port type for `api`."""
    plane_type = ibh.expected_type_leaf(api, is_plane=True).lower()
    return [name for name, t in iface_types.items() if str(t).strip().lower() == plane_type]


def _select_link_up_aport(devices, setup_topology) -> Port:
    """Pick a link-up aggregated port (Aport) that has plane-ports."""
    # Fail (not skip) with an explicit message when no Active Aport exists - the IB
    # links are likely INI/Down (SM not initialized). Non-HFNM setups are skipped
    # earlier by the requires_hfnm guard, so a failure here is a real SM/link issue.
    with allure.step("Select a link-up aggregated port via MultiPlanarTool"):
        try:
            fae = MultiPlanarTool.select_random_aggregated_port(devices.dut, setup_topology.setup_name)
        except Exception as exc:  # noqa: BLE001 - normalize to a clear, actionable failure
            raise AssertionError(
                f"No Active aggregated port on setup {setup_topology.setup_name!r}: the IB "
                "links are not Active (likely INI/Down). The Subnet Manager must be running "
                "and managing this subnet (start_sm on the HFNM, multiplanar opensm) before "
                f"the plane-port link-up tests can run.\nSelector error: {exc}"
            ) from exc
        assert fae is not None, (
            f"No Active aggregated port on setup {setup_topology.setup_name!r}: the IB links "
            "are not Active (likely INI/Down - Subnet Manager has not initialized this subnet)."
        )
        return Port(fae.port.name)


# ---------------------------------------------------------------------------
# 5.1 test_planeport_state_default_disabled
# ---------------------------------------------------------------------------


@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.nvos_build
@pytest.mark.parametrize("api", ALL_APIS)
def test_planeport_state_default_disabled(engines, gnmi_client, api):
    """
    On a fresh DUT the plane-port knob must be disabled and the API must not
    expose any interface with `type` = plane-port.
    """
    with allure.step("Read the observed plane-port knob state before normalizing"):
        # Capture (and attach) the raw state on entry so a genuine default
        # regression is still visible in the log/allure report even though we
        # normalize below to stay robust against state leaked from prior tests.
        observed_state = ibh.get_plane_port_state(engines)
        ibh.attach_dict("plane-port show (observed on entry)", {"state": observed_state})
        if observed_state != PlanePortState.DISABLED:
            logger.warning(
                "plane-port knob was %r (not the default 'disabled') on entry - likely "
                "leaked from a prior test; normalizing to default before assertion",
                observed_state,
            )

    with allure.step("Normalize to default by unsetting the plane-port knob"):
        ibh.unset_plane_port_state(engines, apply=True)

    with allure.step("Default plane-port knob state must be 'disabled'"):
        state = ibh.get_plane_port_state(engines)
        ibh.attach_dict("plane-port show", {"state": state})
        assert state == PlanePortState.DISABLED, (
            f"Default plane-port state must be 'disabled'; got {state!r}"
        )

    with allure.step(f"List all interfaces via {api}"):
        iface_types = ibh.list_interfaces_via_api(api, engines, gnmi_client)

    with allure.step(f"Assert no plane-port entries are exposed via {api}"):
        _assert_no_plane_ports(api, iface_types)


# ---------------------------------------------------------------------------
# 5.2 test_planeport_state_enable_disable
# ---------------------------------------------------------------------------


def _enable_and_assert_visible(api: str, engines, gnmi_client) -> Dict[str, str]:
    ibh.set_plane_port_state(engines, PlanePortState.ENABLED, apply=True)
    state = ibh.get_plane_port_state(engines)
    assert state == PlanePortState.ENABLED, f"state after enable should be 'enabled'; got {state!r}"
    iface_types = ibh.list_interfaces_via_api(api, engines, gnmi_client)
    _assert_some_plane_ports(api, iface_types)
    return iface_types


def _disable_and_assert_hidden(api: str, engines, gnmi_client) -> None:
    ibh.unset_plane_port_state(engines, apply=True)
    state = ibh.get_plane_port_state(engines)
    assert state == PlanePortState.DISABLED, f"state after unset should be 'disabled'; got {state!r}"
    iface_types = ibh.list_interfaces_via_api(api, engines, gnmi_client)
    _assert_no_plane_ports(api, iface_types)


@pytest.mark.requires_hfnm
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(8 * MINUTE, func_only=True)
@pytest.mark.parametrize("api", ALL_APIS)
def test_planeport_state_enable_disable(engines, devices, gnmi_client, setup_topology, start_sm, api):
    """
    Toggling the plane-port knob must immediately change the visibility of
    plane-port interfaces, and the toggle must be stable across the
    enable/disable stress cycles (test plan section 5.2 step 8).

    Also asserts NVBug 6152697 / UMF !195: no alias Data-index-out-of-range
    ERROR during plane-port enable/disable churn, and post-settle Aport
    ALIAS_PORT_MAP rows stay consistent.
    """
    general_cli = GeneralCliCommon(engines.dut)
    umf_marker = umf_churn.place_umf_churn_marker(engines, f"planeport-enable-disable-{api}")
    # sym-mgr / gpu-telemetry only exist on cluster-telemetry platforms
    # (Crocodile / DGX). On a plain multi-planar switch they are absent, so
    # monitor only the containers actually present and skip the restart check
    # when none exist rather than failing on `docker inspect`.
    present_containers = set(
        engines.dut.run_cmd("docker ps -a --format '{{.Names}}'").split()
    )
    monitored_containers = tuple(
        c for c in _RESTART_MONITORED_CONTAINERS if c in present_containers
    )
    restart_before = (
        general_cli.get_container_restart_counts(monitored_containers)
        if monitored_containers else {}
    )

    with allure.step(f"Enable plane-port and verify visibility via {api}"):
        iface_types = _enable_and_assert_visible(api, engines, gnmi_client)

    with allure.step("Show one plane-port to confirm the instance is addressable"):
        aport = _select_link_up_aport(devices, setup_topology)
        plane_ports = setup_topology.planes_for(aport.name)
        assert plane_ports, f"No plane-ports enumerated for {aport.name} (num_of_plane_ports=0?)"
        # The enumerated plane names are synthesized (`f"{aport}pl{i}"`); only
        # those that are actually addressable in the enabled listing can be
        # shown, so pick the first enumerated plane that is present rather than
        # blindly trusting plane_ports[0].
        present_planes = set(_present_plane_port_names(api, iface_types))
        first_plane = next((p for p in plane_ports if p.name in present_planes), None)
        assert first_plane is not None, (
            f"None of the planes enumerated for {aport.name} are present in the "
            f"enabled {api} listing.\n"
            f"  enumerated: {[p.name for p in plane_ports]!r}\n"
            f"  available plane-ports (first 20): {sorted(present_planes)[:20]!r}"
        )
        if api == API_GNMIC:
            payload = ibh.gnmi_get_interface_subtree(gnmi_client, first_plane.name)
            assert payload, f"gnmic returned no leaves for plane-port {first_plane.name}"
            ibh.attach_dict(f"gnmi plane-port {first_plane.name}", payload)
        elif api == API_NVUE_CLI:
            payload = ibh.nvue_show_interface_json(engines, first_plane.name)
            assert payload, f"nv show interface {first_plane.name} returned empty payload"
            ibh.attach_dict(f"nvue plane-port {first_plane.name}", payload)
        else:
            ibh.pull_otel_metric(first_plane.name, leaf="state/type")

    with allure.step(f"Disable plane-port and verify hidden via {api}"):
        _disable_and_assert_hidden(api, engines, gnmi_client)

    with allure.step(f"Get the plane-port via nv show interface {first_plane.name} expects no/empty entry"):
        # After disable the plane-port is hidden, so `nv show interface <plane>`
        # legitimately either returns an empty payload OR errors with non-JSON
        # output (which makes nvue_show_interface_json raise). Both are
        # acceptable; only a non-empty, parseable payload means it is still
        # visible, which is a failure.
        try:
            raw = ibh.nvue_show_interface_json(engines, first_plane.name)
        except Exception:  # noqa: BLE001 - hidden plane-port -> non-JSON/err output is expected
            logger.info("NVUE returned a non-JSON error for the (now hidden) plane-port; that is acceptable")
        else:
            assert not raw or raw == {}, (
                f"Plane-port {first_plane.name} still visible via NVUE after disable: {raw!r}"
            )

    with allure.step(f"Stress: {PLANE_PORT_TOGGLE_CYCLES} enable/disable cycles via {api}"):
        for cycle in range(1, PLANE_PORT_TOGGLE_CYCLES + 1):
            with allure.independent_step(f"cycle {cycle}: enable"):
                _enable_and_assert_visible(api, engines, gnmi_client)
            with allure.independent_step(f"cycle {cycle}: disable"):
                _disable_and_assert_hidden(api, engines, gnmi_client)

    with allure.step("Verify sym-mgr / gpu-telemetry containers did not restart"):
        # Compare RestartCount before vs after the toggle stress: a daemon that
        # crashed and was restarted increments this counter even if it is already
        # "Up" again by the time we look.
        if not monitored_containers:
            logger.info(
                "No monitored telemetry containers (%s) present on this DUT; "
                "skipping restart check", ", ".join(_RESTART_MONITORED_CONTAINERS)
            )
        else:
            restart_after = general_cli.get_container_restart_counts(monitored_containers)
            ibh.attach_dict("docker restart counts", {"before": restart_before, "after": restart_after})
            for name, before in restart_before.items():
                after = restart_after.get(name)
                assert after is not None, (
                    f"{name} container disappeared during toggle stress (before RestartCount={before})"
                )
                assert after == before, (
                    f"{name} restarted during plane-port toggle stress: "
                    f"RestartCount {before} -> {after}"
                )

    with allure.step(
        f"NVBug {NVBUG_6152697} / UMF !195: no alias Data-index-out-of-range ERROR during plane-port churn"
    ):
        umf_churn.assert_no_alias_data_index_errors(engines, umf_marker)
        # Sample steady Aports (always present); plane aliases disappear when the
        # knob ends disabled after the stress loop.
        aport_names = [
            ibh.connectivity_label_to_nvue(n)
            for n in setup_topology.all_planarized_ports()
        ]
        aport_sample = umf_churn.sample_port_names(aport_names or [aport.name])
        umf_churn.assert_alias_port_map_consistent(engines, aport_sample)


# ---------------------------------------------------------------------------
# 5.3 test_planeport_type_leaf_and_api_parity
# ---------------------------------------------------------------------------


@pytest.mark.requires_hfnm
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_planeport_type_leaf_and_api_parity(engines, devices, gnmi_client, setup_topology, start_sm):
    """
    With the knob enabled, the per-surface type-leaf mapping must hold:

      NVUE / OpenAPI top-level `type`: ib (Aport), ibpp (plane-port)
      gNMI state/type leaf           : infiniband (Aport), infiniband-plane-port (plane-port)
    """
    ibh.set_plane_port_state(engines, PlanePortState.ENABLED, apply=True)

    aport = _select_link_up_aport(devices, setup_topology)
    plane = setup_topology.planes_for(aport.name)[0]

    with allure.step("NVUE: show interface and verify type column for Aport and plane"):
        nvue_all = ibh.nvue_show_all_interfaces_json(engines)
        aport_body = nvue_all.get(aport.name, {})
        plane_body = nvue_all.get(plane.name, {})
        assert str(aport_body.get("type", "")).lower() == IfaceType.NVUE_APORT
        assert str(plane_body.get("type", "")).lower() == IfaceType.NVUE_PLANE

    with allure.step("NVUE: show one Aport and one plane individually (JSON top-level type)"):
        aport_json = ibh.nvue_show_interface_json(engines, aport.name)
        plane_json = ibh.nvue_show_interface_json(engines, plane.name)
        assert str(aport_json.get("type", "")).lower() == IfaceType.NVUE_APORT
        assert str(plane_json.get("type", "")).lower() == IfaceType.NVUE_PLANE

    with allure.step("gNMI: get full Aport subtree and check both type AND counters present"):
        # Test plan section 5.3 step 5: "gNMI state/type leaf = 'infiniband'; counters present"
        aport_subtree = ibh.gnmi_get_interface_subtree(gnmi_client, aport.name)
        ibh.attach_dict(f"gnmi subtree {aport.name}", aport_subtree)
        assert (
            aport_subtree.get("type", "").strip() == IfaceType.GNMI_APORT
        ), f"gNMI type for Aport {aport.name} should be {IfaceType.GNMI_APORT!r}; got type={aport_subtree.get('type')!r}"
        aport_counter_keys = [k for k in aport_subtree.keys()
                              if "counter" in k.lower() or k in GNMI_PACKET_OCTET_LEAVES]
        assert aport_counter_keys, (
            f"gNMI Aport {aport.name} returned no counter leaves; "
            f"available keys (first 30): {sorted(aport_subtree.keys())[:30]!r}"
        )

    with allure.step("gNMI: get full plane-port subtree and check both type AND counters present"):
        # Test plan section 5.3 step 6: "gNMI state/type leaf = 'infiniband-plane-port'; counters present"
        plane_subtree = ibh.gnmi_get_interface_subtree(gnmi_client, plane.name)
        ibh.attach_dict(f"gnmi subtree {plane.name}", plane_subtree)
        assert (
            plane_subtree.get("type", "").strip() == IfaceType.GNMI_PLANE
        ), f"gNMI type for plane-port {plane.name} should be {IfaceType.GNMI_PLANE!r}; got type={plane_subtree.get('type')!r}"
        plane_counter_keys = [k for k in plane_subtree.keys()
                              if "counter" in k.lower() or k in GNMI_PACKET_OCTET_LEAVES]
        assert plane_counter_keys, (
            f"gNMI plane-port {plane.name} returned no counter leaves; "
            f"available keys (first 30): {sorted(plane_subtree.keys())[:30]!r}"
        )

    with allure.step("OTEL: confirm placeholder path skips cleanly"):
        try:
            ibh.pull_otel_metric(aport.name, leaf="type")
        except pytest.skip.Exception:
            logger.info("OTEL skip is the expected outcome until OTEL is exposed")


# ---------------------------------------------------------------------------
# 5.4 test_aport_counters_unchanged_with_planeport_enabled
# ---------------------------------------------------------------------------


def _read_aport_counter_set(gnmi_client, aport_name: str) -> Dict[str, str]:
    """Read the per-Aport counter subtree as a flat dict."""
    return ibh.gnmi_get_flat(
        gnmi_client,
        prefix=GnmiYangPaths.STATE_COUNTERS.format(name=aport_name),
        path="",
    )


def _counter_keys(payload: Dict[str, str]) -> List[str]:
    return sorted(payload.keys())


@pytest.mark.requires_hfnm
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(8 * MINUTE, func_only=True)
def test_aport_counters_unchanged_with_planeport_enabled(engines, devices, gnmi_client, setup_topology, start_sm):
    """
    Enabling the plane-port knob must NOT change the existing Aport counter
    schema or the live values that users were already consuming. We:
      1) baseline the Aport counter set with the knob disabled,
      2) enable the knob,
      3) confirm the same counter keys are present,
      4) confirm the counter values are non-decreasing over time (no reset).
    """
    aport = _select_link_up_aport(devices, setup_topology)

    with allure.step("Ensure knob starts disabled (baseline)"):
        ibh.unset_plane_port_state(engines, apply=True)

    with allure.step("Baseline read of Aport counters with knob disabled"):
        baseline = _read_aport_counter_set(gnmi_client, aport.name)
        baseline_keys = _counter_keys(baseline)
        ibh.attach_dict(f"baseline counters {aport.name}", baseline)
        assert baseline_keys, f"Aport {aport.name} returned no counter leaves at baseline"

    with allure.step("Enable plane-port knob"):
        ibh.set_plane_port_state(engines, PlanePortState.ENABLED, apply=True)
        time.sleep(PLANE_PORT_TOGGLE_SETTLE_SEC)

    with allure.step("Re-read Aport counters with knob enabled"):
        after = _read_aport_counter_set(gnmi_client, aport.name)
        after_keys = _counter_keys(after)
        ibh.attach_dict(f"after-enable counters {aport.name}", after)

    with allure.step("Counter key set must be unchanged"):
        missing = set(baseline_keys) - set(after_keys)
        assert not missing, (
            f"Counter leaves disappeared after enabling plane-port knob: {sorted(missing)!r}"
        )

    with allure.step("Counter values must be monotonic (no reset due to enable)"):
        for field in baseline_keys:
            if field not in after:
                continue
            try:
                base_val = ibh.parse_counter_value(baseline[field])
                after_val = ibh.parse_counter_value(after[field])
            except (AssertionError, ValueError):
                logger.debug("Skipping non-numeric counter %s (base=%r after=%r)",
                             field, baseline.get(field), after.get(field))
                continue
            assert after_val >= base_val, (
                f"Counter {field!r} on Aport {aport.name} regressed after enabling plane-port "
                f"knob: baseline={base_val}, after={after_val}"
            )

    with allure.step("Extended counter fields are exposed alongside the legacy set"):
        # Soft check: extended counters may be present or absent depending on
        # the build, but they must not displace the baseline set.
        extra_extended = [f for f in EXTENDED_COUNTER_FIELDS if f in after]
        ibh.attach_dict("extended counters now visible", {"present": extra_extended})
