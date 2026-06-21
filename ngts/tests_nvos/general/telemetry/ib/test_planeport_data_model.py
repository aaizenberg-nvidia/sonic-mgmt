"""
Plane-port functional / data-modeling tests (test plan section 6.1, section 6.2, section 6.3, section 6.9, section 7.1).

Validates that sym-mgr writes per-plane rows in System DB alongside Aport
rows (HLD section 6.2.1), that the Aport's additive counters equal the sum across
its planes in COUNTERS_DB (section 6.2) and over gNMI + NVUE CLI (section 6.3), that every
leaf aggregated on the Aport is also addressable per-plane (test plan section 6.9),
and that the Aport gNMI schema has not regressed (test plan section 7.1).
"""

import json
import logging
import time
from functools import partial
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import pytest

from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import (
    IbInterfaceConsts,
    NvosConsts,
)
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure

from ngts.tests_nvos.general.telemetry.ib import helpers as ibh
from ngts.tests_nvos.general.telemetry.ib.constants import (
    API_LABEL_GNMI,
    API_LABEL_NVUE,
    API_LABEL_OTEL,
    APORT_SCHEMA_BASELINE_FILE,
    APORT_SCHEMA_BASELINE_NVUE_FILE,
    BASELINE_DIR_NAME,
    COUNTERMGRD_MAX_COUNTERS,
    COUNTERMGRD_SUM_COUNTERS,
    CounterMgrdRule,
    EXPECTED_PLANE_PORT_DB_FIELDS,
    GnmiYangPaths,
    PlanePortState,
    PLANEPORT_SUM_AGGREGATION_MIN_DELTA,
    PLANEPORT_SUM_AGGREGATION_TOLERANCE_PCT,
    SAI_TO_GNMI_STATE_COUNTER_LEAF,
    SystemDbCli,
)
from ngts.tests_nvos.system.gnmi.constants import (
    MAX_GNMI_SUBSCRIBERS,
    STREAM_SUBSCRIBE_SAMPLE_INTERVAL,
    STREAM_SUBSCRIBE_WINDOW_SEC,
)
from ngts.tests_nvos.system.gnmi.helpers import (
    count_result_sets,
    run_concurrent_multi_path_stream_subscribers,
    validate_notification_has_prefix_and_leaves,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers local to this file
# ---------------------------------------------------------------------------


_AGGREGATION_LOGICAL_STATES = frozenset({
    IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE,
    IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_INITIALIZE,
})


def _select_aport_for_aggregation(devices, setup_topology, topology_obj) -> Port:
    """Pick a link-up IB Aport; prefer inter-switch ports when topology resolves a partner."""
    with allure.step("Select aggregated port on DUT"):
        iface_map = OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
            Port.show_interface()
        ).verify_result()

        dut_host = ibh.dut_hostname(topology_obj, devices)
        candidates: List[str] = []
        inter_switch: List[str] = []

        for port_name, details in iface_map.items():
            if details.get(IbInterfaceConsts.TYPE) != IbInterfaceConsts.IB_PORT_TYPE:
                continue
            if details.get(IbInterfaceConsts.LINK_STATE) != NvosConsts.LINK_STATE_UP:
                continue
            logical = details.get(IbInterfaceConsts.LINK_LOGICAL_PORT_STATE)
            if logical not in _AGGREGATION_LOGICAL_STATES:
                continue
            candidates.append(port_name)
            if setup_topology.inter_switch_partner(port_name, dut_host) is not None:
                inter_switch.append(port_name)

        if not candidates:
            pytest.skip(
                "No IB Aport with admin-up and logical-state Active/Initialize on DUT. "
                "Ensure SM is running and plane-ports are enabled, then retry."
            )

        pool = inter_switch if inter_switch else candidates
        chosen_name = Tools.RandomizationTool.select_random_value(pool).get_returned_value()
        logger.info(
            "Selected Aport %s (pool=%d inter-switch=%d candidates=%s)",
            chosen_name,
            len(pool),
            len(inter_switch),
            candidates,
        )
        allure.attach(
            "aport selection",
            f"chosen={chosen_name}\npool={pool}\ncandidates={candidates}",
        )
        return Port(chosen_name)


def _assert_planeport_to_aport_aggregation(
    readings: Dict[str, Dict[str, int]],
    aport: Port,
    planes: List[Port],
    api_label: str,
) -> None:
    """countermgrd SUM and MAX plane-port -> Aport rules (section 6.3 / section 6.7)."""
    _assert_planeport_to_aport_sum_aggregation(readings, aport, planes, api_label)
    _assert_planeport_to_aport_max_aggregation(readings, aport, planes, api_label)


def _assert_planeport_to_aport_sum_aggregation(
    readings: Dict[str, Dict[str, int]],
    aport: Port,
    planes: List[Port],
    api_label: str,
) -> None:
    """Sum of per-plane countermgrd fields must match the Aport row (default-SUM)."""
    aport_row = readings[aport.name]
    for sai_field in COUNTERMGRD_SUM_COUNTERS:
        with allure.independent_step(f"{api_label} SUM {aport.name} {sai_field}"):
            aport_val = aport_row.get(sai_field)
            assert aport_val is not None, (
                f"{api_label}: Aport {aport.name} missing SUM counter {sai_field}"
            )
            plane_values = []
            for plane in planes:
                plane_val = readings[plane.name].get(sai_field)
                assert plane_val is not None, (
                    f"{api_label}: plane-port {plane.name} missing SUM counter {sai_field}"
                )
                plane_values.append(plane_val)
            plane_sum = sum(plane_values)
            allowed_delta = ibh.aggregation_allowed_delta(aport_val)
            delta = plane_sum - aport_val
            ibh.attach_dict(
                f"{api_label} agg check {aport.name} {sai_field}",
                {
                    "aggregation": "sum",
                    "aport": aport_val,
                    "plane_values": plane_values,
                    "sum": plane_sum,
                    "expected": aport_val,
                    "measured": plane_sum,
                    "delta": delta,
                    "allowed_delta": allowed_delta,
                    "tolerance_pct": PLANEPORT_SUM_AGGREGATION_TOLERANCE_PCT,
                    "tolerance_min_delta": PLANEPORT_SUM_AGGREGATION_MIN_DELTA,
                },
            )
            assert ibh.values_within_aggregation_tolerance(
                measured=plane_sum,
                expected=aport_val,
            ), (
                f"{api_label} SUM aggregation mismatch on Aport {aport.name} field {sai_field}: "
                f"expected aport={aport_val}, measured sum(planes)={plane_sum}, "
                f"delta={delta}, allowed_delta={allowed_delta}"
            )


def _assert_planeport_to_aport_max_aggregation(
    readings: Dict[str, Dict[str, int]],
    aport: Port,
    planes: List[Port],
    api_label: str,
) -> None:
    """Aport countermgrd MAX fields must equal max(plane-ports) (e.g. xmit-wait / out-wait)."""
    aport_row = readings[aport.name]
    for sai_field in COUNTERMGRD_MAX_COUNTERS:
        with allure.independent_step(f"{api_label} MAX {aport.name} {sai_field}"):
            aport_val = aport_row.get(sai_field)
            assert aport_val is not None, (
                f"{api_label}: Aport {aport.name} missing MAX counter {sai_field}"
            )
            plane_values = []
            for plane in planes:
                plane_val = readings[plane.name].get(sai_field)
                assert plane_val is not None, (
                    f"{api_label}: plane-port {plane.name} missing MAX counter {sai_field}"
                )
                plane_values.append(plane_val)
            plane_max = max(plane_values)
            ibh.attach_dict(
                f"{api_label} agg check {aport.name} {sai_field}",
                {
                    "aggregation": "max",
                    "aport": aport_val,
                    "plane_values": plane_values,
                    "max": plane_max,
                    "expected": aport_val,
                    "measured": plane_max,
                    "delta": plane_max - aport_val,
                },
            )
            ibh.assert_rule_aggregation(
                CounterMgrdRule.MAX, sai_field, aport_val, plane_values,
                api_label=api_label, value_type="int",
            )


def _aggregation_capture_fns(
    gnmi_client,
    engines,
) -> List[
    Tuple[
        str,
        Optional[Callable[[str], Dict[str, int]]],
        bool,
        Optional[Callable[[str, List[Port]], Dict[str, Dict[str, int]]]],
    ]
]:
    """
    (api_label, snapshot_for_port, may_skip, read_snapshot_window) for section 6.3.

    gNMI uses per-port capture with batched subscribes (2/port). NVUE uses
    per-port ``counters -o json`` reads in one window (no inter-port sleep).
    OTEL uses may_skip=True so pytest.skip in capture_otel_counter_snapshot
    does not abort gNMI/NVUE.
    """
    return [
        (
            API_LABEL_GNMI,
            partial(ibh.capture_gnmi_counter_snapshot, gnmi_client),
            False,
            None,
        ),
        (
            API_LABEL_NVUE,
            None,
            False,
            partial(ibh.read_nvue_aggregation_snapshot_window, engines),
        ),
        (
            API_LABEL_OTEL,
            partial(ibh.capture_otel_counter_snapshot, engines),
            True,
            None,
        ),
    ]


def _run_planeport_aggregation_phase(
    phase_title: str,
    aport: Port,
    planes: List[Port],
    api_label: str,
    may_skip: bool = False,
    snapshot_for_port: Optional[Callable[[str], Dict[str, int]]] = None,
    read_snapshot_window: Optional[
        Callable[[str, List[Port]], Dict[str, Dict[str, int]]]
    ] = None,
) -> None:
    """
    Read counter window and run shared SUM/MAX asserts for one API surface.

    Uses independent_step so a gNMI failure still runs NVUE (and OTEL stub)
    in the same phase; failures roll up when the parent aggregation step ends.
    """
    if read_snapshot_window is not None:
        assert snapshot_for_port is None, (
            f"{api_label}: pass read_snapshot_window or snapshot_for_port, not both"
        )
    else:
        assert snapshot_for_port is not None, (
            f"{api_label}: snapshot_for_port required when read_snapshot_window is None"
        )

    with allure.independent_step(f"{api_label} aggregation check {phase_title}"):
        with allure.step(f"{api_label} read counter snapshot window {phase_title}"):
            try:
                if read_snapshot_window is not None:
                    readings = read_snapshot_window(aport.name, planes)
                else:
                    readings = ibh.read_counter_snapshot_window(
                        aport.name,
                        planes,
                        snapshot_for_port,
                        inter_plane_sleep_sec=0,
                    )
            except pytest.skip.Exception:
                if may_skip:
                    logger.info(
                        "%s aggregation %s skipped (OTEL not exposed yet)",
                        api_label,
                        phase_title,
                    )
                    return
                raise

        with allure.independent_step(f"{api_label} verify snapshot window nonempty {phase_title}"):
            ibh.assert_counter_snapshot_window_nonempty(readings, aport.name, api_label)

        with allure.independent_step(f"{api_label} verify plane-port to Aport aggregation {phase_title}"):
            _assert_planeport_to_aport_aggregation(readings, aport, planes, api_label)


def _run_planeport_aggregation_phases(
    phase_title: str,
    aport: Port,
    planes: List[Port],
    gnmi_client,
    engines,
    devices,
    setup_topology,
    topology_obj,
) -> None:
    """
    Close port(s) for frozen reads, then gNMI, NVUE, and OTEL stub asserts.

    Admin-down freezes the DUT Aport and the inter-switch partner when
    connectivity JSON resolves one; restores both in ``finally``.
    """
    with allure.step(f"Admin-down {aport.name} for counter reads ({phase_title})"):
        with ibh.quiesce_aport_for_counter_reads(
            aport,
            engines=engines,
            devices=devices,
            setup_topology=setup_topology,
            topology_obj=topology_obj,
            require_oper_down=True,
        ):
            for api_label, capture_fn, may_skip, window_fn in _aggregation_capture_fns(
                gnmi_client, engines
            ):
                _run_planeport_aggregation_phase(
                    phase_title,
                    aport,
                    planes,
                    api_label,
                    may_skip=may_skip,
                    snapshot_for_port=capture_fn,
                    read_snapshot_window=window_fn,
                )


# ---------------------------------------------------------------------------
# 6.1 test_planeport_redis_rows_present
# ---------------------------------------------------------------------------


@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_planeport_redis_rows_present(engines, devices, setup_topology):
    """
    With the knob enabled, System DB must hold one COUNTERS:* row per
    plane-port, keyed by plane-port name (test plan section 6.1). Each row must
    expose the per-plane additive counter fields.
    """
    ibh.set_plane_port_state(engines, PlanePortState.ENABLED, apply=True)

    expected_count = setup_topology.expected_plane_count()
    assert expected_count > 0, (
        "expected_plane_count() is 0 - either the connectivity JSON is empty or "
        "device.num_of_plane_ports is unset; cannot validate per-plane rows"
    )

    with allure.step("List plane-port COUNTERS keys in System DB"):
        # Pass the expected plane-port names so list_plane_port_keys can also
        # use the OID-resolved EXISTS path (proven by
        # ngts/tests_nvos/interfaces/test_ib_show_interface.get_port_oid) in
        # case the new HLD section 6.2.1 name-keyed rows are not present yet.
        expected_planes = [p.name for aport in setup_topology.all_aports()
                           for p in setup_topology.planes_for(aport)]
        keys = ibh.list_plane_port_keys(engines, planes=expected_planes)
        ibh.attach_dict(
            "plane-port db keys",
            {"keys": keys, "expected_count": expected_count, "expected_planes": expected_planes},
        )

    assert len(keys) == expected_count, (
        f"Plane-port row count mismatch: got {len(keys)} keys, expected {expected_count}"
    )

    with allure.step("Inspect one plane-port row to confirm field set"):
        sample_aport = setup_topology.all_aports()[0]
        sample_plane = setup_topology.planes_for(sample_aport)[0].name
        oid = ibh.get_plane_port_oid(engines, sample_plane)
        row = ibh.db_hgetall(
            engines,
            SystemDbCli.COUNTERS_DB,
            SystemDbCli.COUNTERS_OID_KEY_FMT.format(oid=oid),
        )
        ibh.attach_dict(f"plane-port row {sample_plane}", row)

    missing_fields = [f for f in EXPECTED_PLANE_PORT_DB_FIELDS if f not in row]
    assert not missing_fields, (
        f"Plane-port row {sample_plane} (oid={oid}) is missing expected fields: {missing_fields!r}"
    )


# ---------------------------------------------------------------------------
# 6.2 test_planeport_to_aport_aggregation (COUNTERS_DB - Ori)
# ---------------------------------------------------------------------------


def _capture_counter_snapshot(engines, port_name: str) -> Dict[str, int]:
    """Read additive counter fields for a port from COUNTERS_DB via OID."""
    if ibh.is_plane_port_name(port_name):
        oid = ibh.get_plane_port_oid(engines, port_name)
    else:
        oid = ibh.get_aport_oid(engines, port_name)
    row = ibh.db_hgetall(
        engines,
        SystemDbCli.COUNTERS_DB,
        SystemDbCli.COUNTERS_OID_KEY_FMT.format(oid=oid),
    )
    out: Dict[str, int] = {}
    for sai_field in EXPECTED_PLANE_PORT_DB_FIELDS:
        raw = row.get(sai_field)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            out[sai_field] = ibh.parse_counter_value(raw)
        except (AssertionError, ValueError):
            continue
    return out


@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.requires_hfnm
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_planeport_to_aport_aggregation(
    engines, devices, setup_topology, topology_obj, players, setup_name, request
):
    """Sum of per-plane additive counters equals the Aport counter (within tolerance), re-checked after a small IB traffic burst."""
    if not hasattr(engines, "hfnm"):
        pytest.skip(
            f"REQUIRES HFNM-CAPABLE SETUP: setup {setup_name!r} has no Host "
            "Fabric Node Manager, so the Subnet Manager cannot be started to "
            "bring plane-ports Active. This is a lab-capability gap, not a "
            "product pass - run on an HFNM-capable setup for section 6.2 coverage."
        )
    try:
        request.getfixturevalue("start_sm")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            "REQUIRES HFNM-CAPABLE SETUP: Subnet Manager could not be started "
            f"on setup {setup_name!r}: {exc}"
        )

    ibh.set_plane_port_state(engines, PlanePortState.ENABLED, apply=True)

    aport = _select_aport_for_aggregation(devices, setup_topology, topology_obj)
    planes = setup_topology.planes_for(aport.name)
    assert planes, f"No plane-ports could be enumerated for Aport {aport.name}"

    _read_window = partial(
        ibh.read_counter_snapshot_window,
        aport.name,
        planes,
        partial(_capture_counter_snapshot, engines),
    )

    def _assert_aggregation(readings: Dict[str, Dict[str, int]]) -> None:
        aport_row = readings[aport.name]
        for sai_field, aport_val in aport_row.items():
            with allure.independent_step(
                f"COUNTERS_DB SUM {aport.name} {sai_field}"
            ):
                plane_values = [readings[p.name].get(sai_field) for p in planes]
                plane_values = [v for v in plane_values if v is not None]
                if not plane_values:
                    continue
                plane_sum = sum(plane_values)
                allowed_delta = ibh.aggregation_allowed_delta(aport_val)
                delta = plane_sum - aport_val
                ibh.attach_dict(
                    f"agg check {aport.name} {sai_field}",
                    {
                        "aport": aport_val,
                        "plane_values": plane_values,
                        "sum": plane_sum,
                        "delta": delta,
                        "allowed_delta": allowed_delta,
                        "tolerance_pct": PLANEPORT_SUM_AGGREGATION_TOLERANCE_PCT,
                        "tolerance_min_delta": PLANEPORT_SUM_AGGREGATION_MIN_DELTA,
                    },
                )
                assert ibh.values_within_aggregation_tolerance(
                    measured=plane_sum,
                    expected=aport_val,
                ), (
                    f"Aggregation mismatch on Aport {aport.name} field {sai_field}: "
                    f"sum(planes)={plane_sum}, aport={aport_val}, "
                    f"delta={delta}, allowed_delta={allowed_delta}"
                )

    with allure.step("Aggregation check on quiescent system"):
        _assert_aggregation(_read_window())

    with allure.step("Drive a small IB traffic burst and re-check aggregation"):
        try:
            interfaces = request.getfixturevalue("interfaces")
        except Exception as exc:  # noqa: BLE001
            pytest.skip(
                f"Traffic harness unavailable on setup {setup_name!r} (no host<->DUT "
                f"ethernet ports): {exc}. Quiescent aggregation check already passed."
            )
        Tools.TrafficGeneratorTool.send_ib_traffic(
            players, interfaces, setup_name, True
        ).verify_result()
        time.sleep(2)
        with allure.step("Aggregation check after IB traffic burst"):
            _assert_aggregation(_read_window())


# ---------------------------------------------------------------------------
# 6.3 test_gnmi_nvue_planeport_to_aport_aggregation (gNMI + NVUE CLI)
# ---------------------------------------------------------------------------


@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.requires_hfnm
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_gnmi_nvue_planeport_to_aport_aggregation(
    engines, devices, gnmi_client, setup_topology, topology_obj, players, setup_name, request
):
    """
    Plane-port -> Aport counter aggregation on gNMI and NVUE CLI (test plan
    section 6.3 / R-NVOS-1). Flow: pick Aport -> drive IB traffic (link up) ->
    admin-down DUT Aport (and inter-switch partner when connectivity resolves)
    to freeze counters -> read gNMI/NVUE in one window and assert SUM+MAX.
    Missing leaves on either API is treated as a product bug.
    """
    with allure.step("gNMI and NVUE plane-port to Aport counter aggregation"):
        with allure.independent_step("Verify HFNM-capable setup"):
            if not hasattr(engines, "hfnm"):
                pytest.skip(
                    f"REQUIRES HFNM-CAPABLE SETUP: setup {setup_name!r} has no Host "
                    "Fabric Node Manager, so the Subnet Manager cannot be started to "
                    "bring plane-ports Active. Run on an HFNM-capable setup for "
                    "section 6.3 coverage."
                )

        with allure.independent_step("Start Subnet Manager"):
            try:
                request.getfixturevalue("start_sm")
            except Exception as exc:  # noqa: BLE001
                pytest.skip(
                    "REQUIRES HFNM-CAPABLE SETUP: Subnet Manager could not be started "
                    f"on setup {setup_name!r}: {exc}"
                )

        with allure.independent_step("Enable plane-port operational state"):
            ibh.set_plane_port_state(engines, PlanePortState.ENABLED, apply=True)

        with allure.independent_step("Verify gNMI server is available"):
            _, gnmi_cap_err = gnmi_client.gnmic_capabilities(skip_cert_verify=True)
            if ibh.is_gnmi_server_unavailable(gnmi_cap_err):
                pytest.skip(
                    f"gNMI server is unavailable on {engines.dut.ip}: "
                    f"{gnmi_cap_err.strip()}"
                )

        with allure.step(
            "Select aggregated port on DUT and enumerate plane-ports"
        ):
            aport = _select_aport_for_aggregation(
                devices, setup_topology, topology_obj
            )
            planes = setup_topology.planes_for(aport.name)
            assert planes, (
                f"No plane-ports could be enumerated for Aport {aport.name}"
            )

        with allure.step("Counter aggregation checks (gNMI, NVUE, OTEL stub)"):
            phase_title = "after IB traffic warm-up"
            with allure.step("Drive IB traffic while link is up (freeze after admin-down)"):
                try:
                    interfaces = request.getfixturevalue("interfaces")
                except Exception as exc:  # noqa: BLE001
                    phase_title = "without traffic harness (quiescent counters only)"
                    logger.warning(
                        "Traffic harness unavailable on setup %r: %s - continuing with "
                        "admin-down snapshot only.",
                        setup_name,
                        exc,
                    )
                else:
                    Tools.TrafficGeneratorTool.send_ib_traffic(
                        players, interfaces, setup_name, True
                    ).verify_result()
                    time.sleep(2)

            _run_planeport_aggregation_phases(
                phase_title,
                aport,
                planes,
                gnmi_client,
                engines,
                devices,
                setup_topology,
                topology_obj,
            )


# ---------------------------------------------------------------------------
# 6.9 test_planeport_path_coverage_for_all_aggregated_aport_leaves
# ---------------------------------------------------------------------------


def _leaf_set(payload: Dict[str, str]) -> Set[str]:
    return {k for k, v in (payload or {}).items() if v is not None}


@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(8 * MINUTE, func_only=True)
def test_planeport_path_coverage_for_all_aggregated_aport_leaves(
    engines, devices, gnmi_client, setup_topology, topology_obj
):
    """
    Every leaf currently aggregated on the Aport row must also be addressable
    on every plane-port row (schema-completeness check).
    """
    ibh.set_plane_port_state(engines, PlanePortState.ENABLED, apply=True)
    aport = _select_aport_for_aggregation(devices, setup_topology, topology_obj)
    planes = setup_topology.planes_for(aport.name)

    with allure.step(f"Read Aport subtree {aport.name}"):
        aport_payload = ibh.gnmi_get_interface_subtree(gnmi_client, aport.name)
        aport_leaves = _leaf_set(aport_payload)
        ibh.attach_dict(f"aport leaves {aport.name}", {"leaves": sorted(aport_leaves)})

    assert aport_leaves, f"Aport {aport.name} returned no leaves over gNMI"

    with allure.step(f"Verify every plane subtree is a superset of Aport {aport.name} leaves"):
        for plane in planes:
            with allure.independent_step(f"Plane {plane.name} subtree is a superset of Aport leaves"):
                plane_payload = ibh.gnmi_get_interface_subtree(gnmi_client, plane.name)
                plane_leaves = _leaf_set(plane_payload)
                # Every Aport leaf must also exist on the plane subtree.
                Tools.ValidationTool.validate_subset_in_superset(
                    subset=aport_leaves, superset=plane_leaves
                ).verify_result()
                for k in plane_leaves:
                    val = plane_payload.get(k)
                    assert val is not None, (
                        f"Plane-port {plane.name} leaf {k!r} returned None - typing/format violation"
                    )


# ---------------------------------------------------------------------------
# 6.9 test_gnmi_subscribe_many_planeport_paths (multi-path subscribe load)
# ---------------------------------------------------------------------------


@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(8 * MINUTE, func_only=True)
def test_gnmi_subscribe_many_planeport_paths(
    engines, devices, gnmi_client, setup_topology, topology_obj
):
    """
    gNMI subscribe load: open MAX_GNMI_SUBSCRIBERS concurrent SAMPLE-mode STREAM subscribers
    on the full per-plane counter-leaf path set of one Aport (plane-port enabled), stream for
    STREAM_SUBSCRIBE_WINDOW_SEC seconds at a one-second sample interval, and assert each
    subscriber both receives every subscribed path and observes more than one result-set
    (roughly one per second). Path/field-name and result-set coverage only - counter values
    are not asserted.
    """
    # Enable plane-port so each plane exposes its own counter paths
    ibh.set_plane_port_state(engines, PlanePortState.ENABLED, apply=True)

    # Select a link-up Aport and enumerate its plane-ports
    aport = _select_aport_for_aggregation(devices, setup_topology, topology_obj)
    planes = setup_topology.planes_for(aport.name)
    assert planes, f"No plane-ports enumerated for Aport {aport.name}"

    # Build the full per-plane counter-leaf path set to subscribe to
    leaves = sorted(set(SAI_TO_GNMI_STATE_COUNTER_LEAF.values()))
    paths = [
        GnmiYangPaths.STATE_COUNTER_FIELD.format(name=plane.name, field=leaf)
        for plane in planes
        for leaf in leaves
    ]
    ibh.attach_dict(
        "subscribe load",
        {
            "aport": aport.name,
            "planes": [p.name for p in planes],
            "leaves": leaves,
            "paths_per_subscriber": len(paths),
            "subscribers": MAX_GNMI_SUBSCRIBERS,
            "window_sec": STREAM_SUBSCRIBE_WINDOW_SEC,
            "sample_interval": STREAM_SUBSCRIBE_SAMPLE_INTERVAL,
        },
    )
    assert len(paths) == len(planes) * len(leaves), (
        f"path build mismatch: {len(paths)} != {len(planes)} planes * {len(leaves)} leaves"
    )

    # Open MAX_GNMI_SUBSCRIBERS concurrent SAMPLE-mode STREAM subscribers on the full set so
    # each one streams for STREAM_SUBSCRIBE_WINDOW_SEC and gets a result-set every second
    with allure.step(
        f"Open {MAX_GNMI_SUBSCRIBERS} concurrent STREAM subscribers on {len(paths)} paths "
        f"for {STREAM_SUBSCRIBE_WINDOW_SEC}s at {STREAM_SUBSCRIBE_SAMPLE_INTERVAL} sample interval"
    ):
        per_subscriber = run_concurrent_multi_path_stream_subscribers(
            engines=engines,
            devices=devices,
            target_ip=engines.dut.ip,
            prefix="",
            paths=paths,
            num_subscribers=MAX_GNMI_SUBSCRIBERS,
            window_sec=STREAM_SUBSCRIBE_WINDOW_SEC,
            username=gnmi_client.username,
            password=gnmi_client.password,
            sample_interval=STREAM_SUBSCRIBE_SAMPLE_INTERVAL,
        )

    # Every subscriber session must have returned a result set
    assert len(per_subscriber) == MAX_GNMI_SUBSCRIBERS, (
        f"expected {MAX_GNMI_SUBSCRIBERS} subscriber result-sets, got {len(per_subscriber)}"
    )

    # Assert each subscriber received every subscribed path (field names only)
    with allure.step("Verify every subscriber received all subscribed plane-port paths"):
        for sub_idx, notifications in enumerate(per_subscriber):
            for plane in planes:
                with allure.independent_step(
                    f"subscriber {sub_idx}: plane {plane.name} has all subscribed counter leaves"
                ):
                    validate_notification_has_prefix_and_leaves(
                        notifications=notifications,
                        expected_prefix=GnmiYangPaths.STATE_COUNTERS.format(name=plane.name),
                        expected_leaves=leaves,
                    )

    # Assert the subscription actually streamed: each subscriber must observe more than one
    # result-set over the window (SAMPLE mode at a one-second interval yields ~window_sec)
    result_set_counts = [count_result_sets(notifications) for notifications in per_subscriber]
    ibh.attach_dict(
        "result-sets per subscriber",
        {f"subscriber_{idx}": count for idx, count in enumerate(result_set_counts)},
    )
    with allure.step("Verify every subscriber received more than one result-set"):
        for sub_idx, count in enumerate(result_set_counts):
            with allure.independent_step(
                f"subscriber {sub_idx}: received {count} result-sets over {STREAM_SUBSCRIBE_WINDOW_SEC}s"
            ):
                assert count > 1, (
                    f"subscriber {sub_idx} expected more than one streamed result-set over "
                    f"{STREAM_SUBSCRIBE_WINDOW_SEC}s at {STREAM_SUBSCRIBE_SAMPLE_INTERVAL} sample "
                    f"interval, got {count}"
                )


# ---------------------------------------------------------------------------
# 7.1 test_aport_backward_compat_schema_unchanged
# ---------------------------------------------------------------------------


def _baselines_dir() -> Path:
    return Path(__file__).resolve().parent / BASELINE_DIR_NAME


def _flatten_nvue_payload(payload, prefix: str = "") -> Set[str]:
    """
    Flatten a deeply-nested NVUE JSON payload into a set of leaf paths
    (``a/b/c`` form). Empty dicts (e.g. ``link.diagnostics: {}``) are kept
    as terminal leaves so the test still detects them being added/removed.
    Lists are unsupported in the NVUE output we baseline against and would
    only appear inside opaque value strings, so we keep them as leaves.
    """
    leaves: Set[str] = set()
    if isinstance(payload, dict):
        if not payload:
            leaves.add(prefix) if prefix else None
            return leaves
        for key, value in payload.items():
            child_prefix = f"{prefix}/{key}" if prefix else str(key)
            leaves |= _flatten_nvue_payload(value, child_prefix)
        return leaves
    leaves.add(prefix)
    return leaves


def _platform_baseline_keys(device) -> List[str]:
    """
    Ordered, most-specific-first platform keys used to look up a product-pinned
    baseline override (e.g. ``blackmamba`` then ``qtm3``).

    The default capture is product-independent (the NVUE Aport leaf schema is
    the same across the QTM3 family), so this only matters when someone pins a
    product-specific file; otherwise the generic baseline is used.
    """
    keys: List[str] = []
    if device is None:
        return keys
    cls = type(device).__name__  # e.g. 'BlackMambaSwitch', 'CrocodileSwitch'
    product = cls[:-len("Switch")].lower() if cls.endswith("Switch") else cls.lower()
    if product:
        keys.append(product)
    asic = getattr(device, "asic_type", None)
    if asic:
        keys.append(str(asic).lower())
    return keys


def _candidate_baseline_files(base_filename: str, device) -> List[str]:
    """
    Product-specific baseline filenames first, then the generic base file.

    ``aport_schema_baseline.nvue.json`` + product ``blackmamba``
        -> ``aport_schema_baseline.blackmamba.nvue.json``
    """
    stem, _, rest = base_filename.partition(".")
    names = [
        (f"{stem}.{key}.{rest}" if rest else f"{stem}.{key}")
        for key in _platform_baseline_keys(device)
    ]
    names.append(base_filename)
    return names


def _load_baseline(device=None) -> Dict[str, object]:
    """
    Load the pinned Aport schema baseline.

    For each format we look for a product-specific override first
    (``aport_schema_baseline.<product>[.nvue].json``) and fall back to the
    generic, product-independent baseline. Two formats are supported, tried in
    this order:

      1. ``aport_schema_baseline.json`` -- the canonical gNMI baseline
         produced by `gnmic get --path '/interfaces/interface[name=<aport>]'`
         on a pre-feature build. Stored as a flat dict whose top-level
         ``leaves`` key holds the list of leaf paths (Test Plan section 7.1 (a)).

      2. ``aport_schema_baseline.nvue.json`` -- fallback NVUE deep view
         (`nv show interface <aport> -o json` on a pre-feature build).
         The nested JSON is flattened to leaf paths at load time
         (Test Plan section 7.1 (c)).

    Returns a dict::

        {"source": "gnmi" | "nvue", "leaves": [...], "aport": ..., "file": ...}
    """
    baselines = _baselines_dir()

    for fname in _candidate_baseline_files(APORT_SCHEMA_BASELINE_FILE, device):
        gnmi_path = baselines / fname
        if not gnmi_path.exists():
            continue
        try:
            data = json.loads(gnmi_path.read_text())
            leaves = sorted(data.get("leaves", []))
            if leaves:
                return {"source": "gnmi", "leaves": leaves,
                        "aport": data.get("aport"), "file": fname}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not parse gNMI baseline %s: %s", gnmi_path, exc)

    for fname in _candidate_baseline_files(APORT_SCHEMA_BASELINE_NVUE_FILE, device):
        nvue_path = baselines / fname
        if not nvue_path.exists():
            continue
        try:
            data = json.loads(nvue_path.read_text())
            # Strip metadata (``_``-prefixed); remaining top-level key is the
            # (illustrative) Aport name.
            ports = {k: v for k, v in data.items() if not k.startswith("_")}
            if len(ports) != 1:
                logger.warning(
                    "NVUE baseline %s must contain exactly one Aport; got %r",
                    nvue_path, list(ports.keys()),
                )
                continue
            aport_name, aport_payload = next(iter(ports.items()))
            leaves = sorted(_flatten_nvue_payload(aport_payload))
            if leaves:
                return {"source": "nvue", "leaves": leaves,
                        "aport": aport_name, "file": fname}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not parse NVUE baseline %s: %s", nvue_path, exc)

    return {}


def _read_live_nvue_schema(aport_name: str) -> Set[str]:
    """
    Read the live `nv show interface <aport> -o json` payload and flatten
    it to a leaf-path set, matching the NVUE baseline format.
    """
    raw = Port(name=aport_name).interface.show()
    payload = OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()
    return _flatten_nvue_payload(payload)


@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_aport_backward_compat_schema_unchanged(
    engines, devices, gnmi_client, setup_topology, topology_obj
):
    """
    Aport schema (leaf-name set) must not regress relative to the pinned
    baseline after the plane-port feature is enabled.

    Allowed differences vs baseline: numeric counter values, time-since-clear,
    BER readings, net-new leaves. Disallowed: removed leaves, renamed leaves.

    The baseline lives at ``baselines/aport_schema_baseline[.nvue].json``
    alongside this test. When the schema legitimately changes (new release),
    update the baseline file in the same commit that exposes the new leaves.
    """
    ibh.set_plane_port_state(engines, PlanePortState.ENABLED, apply=True)
    aport = _select_aport_for_aggregation(devices, setup_topology, topology_obj)

    baseline = _load_baseline(devices.dut)
    assert baseline, (
        f"Aport schema baseline not found in {_baselines_dir()}; "
        f"expected one of {APORT_SCHEMA_BASELINE_FILE!r} (gNMI) or "
        f"{APORT_SCHEMA_BASELINE_NVUE_FILE!r} (NVUE), optionally with a "
        f"product suffix (tried: {_platform_baseline_keys(devices.dut)!r})."
    )
    baseline_leaves = sorted(baseline["leaves"])
    baseline_source = baseline["source"]
    ibh.attach_dict(
        "baseline",
        {"source": baseline_source, "file": baseline.get("file"),
         "aport": baseline.get("aport"), "leaves": baseline_leaves},
    )

    with allure.step(f"Read current Aport subtree from {aport.name} via {baseline_source}"):
        if baseline_source == "gnmi":
            current_payload = ibh.gnmi_get_interface_subtree(gnmi_client, aport.name)
            current_leaves = set(current_payload.keys())
        else:
            current_leaves = _read_live_nvue_schema(aport.name)
        ibh.attach_dict(
            f"current schema {aport.name}",
            {"leaves": sorted(current_leaves)},
        )

    with allure.step("Diff current vs baseline"):
        removed = sorted(set(baseline_leaves) - current_leaves)
        added = sorted(current_leaves - set(baseline_leaves))
        ibh.attach_dict("schema diff", {"removed": removed, "added": added})

    # Net-new leaves are allowed - the new release surfaces new fields - so only
    # the regression direction is blocked: baseline leaves must remain a subset
    # of the current schema (Test Plan section 7.1 step 3).
    Tools.ValidationTool.validate_subset_in_superset(
        subset=baseline_leaves, superset=current_leaves
    ).verify_result()
