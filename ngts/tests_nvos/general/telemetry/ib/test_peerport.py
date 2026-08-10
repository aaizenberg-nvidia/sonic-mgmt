"""HCA peer-port ingestion tests: listing, ingestion, mapping, aggregation, resiliency."""

import logging
import time
from typing import Dict, List, Tuple

import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure

from ngts.tests_nvos.general.telemetry.ib import helpers as ibh
from ngts.tests_nvos.general.telemetry.ib.constants import (
    ALL_APIS,
    API_GNMIC,
    API_NVUE_CLI,
    GnmiTypeKind,
    GnmiYangPaths,
    NMXT_IB_SERVICE,
    NvuePaths,
    PEER_PORT_ADDITIVE_FIELDS,
    PEER_PORT_API_TO_DB_FIELD,
    PEER_PORT_BER_FIELDS,
    PEER_PORT_COUNTER_FIELDS,
    PEER_PORT_DB_ADDITIVE_FIELDS,
    PEER_PORT_LIST_SCHEMA_GROUPS,
    PEER_PORT_PLANE_FIELDS,
    PEER_PORT_PLR_FIELDS,
    PEER_PORT_SCHEMA_GROUPS,
    PEER_TELEMETRY_SAMPLING_SEC,
    PeerPortFields,
    PeerType,
    SAMPLING_JITTER_TOLERANCE_PCT,
)

logger = logging.getLogger(__name__)


PEER_PORT_ENABLE_GRACE_SEC = 10       # nmx-t-ib (re)start + IB discovery on enable
PEER_PORT_SETTLE_INTERVALS = 2        # sampling cycles allowed to reach the wanted state
PEER_PORT_INTERVAL_SETTLE_SEC = 10    # apply returns before the interval settles
# Cold reboot clears Redis; nmx-t-ib must rediscover every external HCA first.
PEER_PORT_COLD_BOOT_GRACE_SEC = 90
PEER_PORT_COLD_BOOT_SETTLE_INTERVALS = 12
# HCA peer-down -> row disappears within ~10s (event-driven); margin for jitter.
HCA_PEER_DOWN_DEADLINE_SEC = 20
# Row re-creation needs a full link retrain + SM re-discovery, far slower than removal.
HCA_PEER_RECOVERY_GRACE_SEC = 180
HCA_PEER_RECOVERY_SETTLE_INTERVALS = 4


def _peer_port_interval_sec(default: int = PEER_TELEMETRY_SAMPLING_SEC) -> int:
    """Operational peer-port sampling interval (seconds) from the live knob (else ``default``)."""
    try:
        shown = str(System().peer_port.parse_show().get("interval", "")).strip()
        return int(shown) if shown else default
    except Exception:  # noqa: BLE001
        return default


def _wait_for_peer_ports(engines, *, present: bool,
                         settle_intervals: int = PEER_PORT_SETTLE_INTERVALS) -> Dict[str, Dict[str, str]]:
    """Poll the live NVUE peer-port listing until it reaches the wanted state (bounded by live interval)."""
    interval = _peer_port_interval_sec()
    grace = PEER_PORT_ENABLE_GRACE_SEC if present else 0
    deadline = time.time() + grace + interval * max(1, settle_intervals)
    peers: Dict[str, Dict[str, str]] = ibh.nvue_show_peer_ports(engines)
    while bool(peers) != present and time.time() < deadline:
        time.sleep(min(interval, 2))
        peers = ibh.nvue_show_peer_ports(engines)
    return peers


def _enable_peer_port(engines) -> None:
    """Enable the peer-port feature knob and wait for the first ingest cycle."""
    System().peer_port.set(
        op_param_name=NvuePaths.KEY_STATE,
        op_param_value=NvuePaths.STATE_ENABLED,
        apply=True,
        ask_for_confirmation='-y',
    ).verify_result()
    _wait_for_peer_ports(engines, present=True)


def _enable_peer_port_for_hca(engines, **list_kwargs) -> Dict[str, Dict[str, str]]:
    """Enable peer-port and wait until the HCA peer-port rows are ingested (``list_kwargs`` forwarded)."""
    _enable_peer_port(engines)
    interval = _peer_port_interval_sec()
    deadline = time.time() + PEER_PORT_ENABLE_GRACE_SEC + interval * PEER_PORT_SETTLE_INTERVALS
    rows: Dict[str, Dict[str, str]] = ibh.list_hca_peer_rows(engines, **list_kwargs)
    while not rows and time.time() < deadline:
        time.sleep(min(interval, 2))
        rows = ibh.list_hca_peer_rows(engines, **list_kwargs)
    return rows


_XCSET_PARSE_FAIL_TOKENS = ("malformed row", "csv parse failed", "parse error", "parse failed")


def _count_xcset_parse_failures(engines) -> int:
    """Count CSV parse-failure lines in the peer-telemetry journal tail."""
    log = (ibh.read_gpu_telemetry_log(engines) or "").lower()
    return sum(1 for ln in log.splitlines() if any(tok in ln for tok in _XCSET_PARSE_FAIL_TOKENS))


def _wait_for_xcset_parse_failure(engines, baseline: int,
                                  settle_intervals: int = PEER_PORT_SETTLE_INTERVALS) -> bool:
    """Poll the peer-telemetry journal until a NEW CSV parse failure (past ``baseline``) is logged."""
    interval = _peer_port_interval_sec()
    deadline = time.time() + interval * max(1, settle_intervals)
    while time.time() < deadline:
        if _count_xcset_parse_failures(engines) > baseline:
            return True
        time.sleep(min(interval, 2))
    return False


def _require_hca_topology(setup_topology) -> List:
    """Return the connectivity-derived HCA peers, or FAIL with an actionable message."""
    hca = setup_topology.hca_peers()
    if not hca:
        pytest.fail(
            "WRONG LAB SETUP / TOPOLOGY: the connectivity JSON for "
            f"{setup_topology.setup_name!r} lists no HCA peers, so the HCA "
            "peer-port feature cannot be exercised here. Run on an HCA-connected "
            "setup (see test plan §3.1) whose connectivity file classifies its "
            "host peers as HCA."
        )
    return hca


def _require_external_hca_topology(setup_topology) -> List:
    """Return connectivity-derived *external* HCA peers (server cables), or FAIL."""
    hca = setup_topology.external_hca_peers()
    if not hca:
        pytest.fail(
            "WRONG LAB SETUP / TOPOLOGY: the connectivity JSON for "
            f"{setup_topology.setup_name!r} lists no external HCA cables "
            "(connected_to an ib*HCA* port), so the HCA peer-port listing "
            "cannot be exercised here. Run on a setup with at least one "
            "server HCA cabled to the fabric."
        )
    return hca


def _require_active_fabric_and_traffic(engines, request, setup_name):
    """FAIL (not skip) when HFNM/SM or the traffic harness are missing."""
    if not hasattr(engines, "hfnm"):
        pytest.fail(
            f"WRONG LAB SETUP: setup {setup_name!r} has no Host Fabric Node "
            "Manager (HFNM), so the Subnet Manager cannot be started to bring the "
            "fabric Active - this test cannot be exercised here. Run on an "
            "HFNM-capable setup with traffic hosts."
        )
    try:
        request.getfixturevalue("start_sm")
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            "WRONG LAB SETUP: Subnet Manager could not be started on setup "
            f"{setup_name!r} ({exc}); an active fabric is required here."
        )
    try:
        players = request.getfixturevalue("players")
        interfaces = request.getfixturevalue("interfaces")
    except (Exception, pytest.skip.Exception) as exc:  # noqa: BLE001 (Skipped is not an Exception)
        pytest.fail(
            f"WRONG LAB SETUP: traffic harness unavailable on setup {setup_name!r} "
            f"(no host<->DUT ethernet ports): {exc}."
        )
    return players, interfaces


def _assert_every_entry_references_an_aport(entries: Dict[str, Dict[str, str]]) -> None:
    missing = [pid for pid, f in entries.items() if not ibh.peer_entry_aport(f)]
    assert not missing, (
        f"Peer-port entries with no associated Aport reference: {missing!r}; "
        "every peer-port must reference its local Aport (test plan §5.5 step 2)"
    )


def _assert_peers_reference_known_aports(
    entries: Dict[str, Dict[str, str]], valid_aports: set, *, label: str = "peer"
) -> None:
    """Every entry's Aport reference resolves to a known local switch port."""
    valid_canon = {ibh.canonical_aport(a) for a in valid_aports}
    for pid, fields in entries.items():
        aport = ibh.peer_entry_aport(fields)
        assert aport, f"HCA {label} {pid!r} has no Aport reference"
        assert ibh.canonical_aport(aport) in valid_canon, (
            f"HCA {label} {pid!r} references unknown switch port {aport!r}; "
            f"known Aports include {sorted(valid_aports)!r}"
        )


def _assert_switch_ports_cover_external_hca(
    entries: Dict[str, Dict[str, str]], expected_ports: set
) -> None:
    """Every external-HCA switch port from connectivity carries an ingested HCA entry (JSON is a lower bound)."""
    live_ports = {ibh.peer_entry_aport(f) for f in entries.values()}
    live_canon = {ibh.canonical_aport(p) for p in live_ports}
    expected_canon = {ibh.canonical_aport(p) for p in expected_ports}
    missing_ports = expected_canon - live_canon
    assert not missing_ports, (
        "External HCA switch-port(s) from connectivity carry no ingested HCA entry: "
        f"{sorted(missing_ports)!r}; live {sorted(live_ports)!r}"
    )
    extra_ports = live_canon - expected_canon
    if extra_ports:
        logger.info(
            "Ingested HCA switch-port(s) beyond the connectivity JSON's external "
            "set (allowed - JSON is a lower bound): %r", sorted(extra_ports)
        )


def _assert_external_hca_neighbor_guids_present(
    live_guids: set,
    expected_guids: set,
    *,
    label: str,
    row_count: int,
) -> None:
    """Every external HCA neighbor GUID from the connectivity JSON is ingested (JSON is a lower bound)."""
    assert expected_guids, (
        "WRONG LAB SETUP / TOPOLOGY: the connectivity JSON lists external HCA "
        "cables but carries no neighbor_guid for them, so the ingested HCA set "
        f"cannot be reconciled by node GUID via {label}. Regenerate the "
        "connectivity JSON (test_and_update_connectivity_json) so each external "
        "HCA link records its neighbor_guid."
    )
    assert live_guids, f"No HCA node GUIDs via {label}"
    missing = expected_guids - live_guids
    assert not missing, (
        f"External HCA neighbor GUID(s) from connectivity not present via {label}: "
        f"{sorted(missing)!r}; live GUIDs {sorted(live_guids)!r}"
    )
    extra = live_guids - expected_guids
    if extra:
        logger.info(
            "Live HCA node GUID(s) via %s beyond the connectivity JSON's external "
            "set (allowed - JSON is a lower bound; faithfulness covered by "
            "test_hca_xcset_matches_system_db): %r", label, sorted(extra)
        )
    assert row_count >= len(expected_guids), (
        f"Expected at least {len(expected_guids)} aggregated HCA row(s) via {label} "
        f"(one per external host minimum), got {row_count}"
    )


def _assert_hca_ingestion_directly_on_dut(engines, setup_topology) -> None:
    """Guard: peer-port must surface only HCAs cabled DIRECTLY to this DUT (by owner node GUID), never downstream-switch HCAs."""
    dut_guids = setup_topology.dut_node_guids()
    owners = setup_topology.hca_owner_guids_by_neighbor()
    if not dut_guids or not owners:
        return
    # Wait out ingest lag so the guard never races an empty DB and passes vacuously.
    settle = PEER_PORT_ENABLE_GRACE_SEC + _peer_port_interval_sec() * PEER_PORT_SETTLE_INTERVALS
    hca_rows, _ = ibh.wait_for_hca_peer_rows(
        engines, tier=PeerPortFields.TIER_AGGREGATED, timeout_sec=settle
    )
    offenders = []
    for pid, row in hca_rows.items():
        node_guid = ibh.peer_entry_node_guid(row)
        owner = owners.get(node_guid, set())
        if owner and not (owner & dut_guids):
            offenders.append((pid, node_guid, sorted(owner)))
    assert not offenders, (
        "peer-port ingested HCA(s) that are NOT directly connected to this DUT: "
        "the connectivity JSON shows they are cabled to a downstream switch, so "
        "NVOS learned them transitively via ibdiagnet (product bug - remote HCAs "
        "must not appear on the DUT's peer-port surface). Offending rows "
        f"(peer_id, hca node_guid, owning switch node_guid): {offenders!r}. "
        f"DUT node GUID(s): {sorted(dut_guids)!r}"
    )


def _select_peer_port_targets(gnmi_client) -> List[Tuple[str, str]]:
    """Pick up to two live HCA peer-ports to sweep (fallback: first listed); FAILS if none exposed."""
    entries = ibh.gnmi_get_peer_port_list(gnmi_client)
    assert entries, (
        "No peer-ports exposed via gNMI under "
        f"{GnmiYangPaths.PEER_PORT_INTERFACES}. Enable the peer-port knob and "
        "confirm gpu-telemetry is running / the feature build is delivered."
    )
    hca = ibh.filter_peer_entries_by_type(entries, PeerType.HCA)
    targets = [("hca-peer", pid) for pid in sorted(hca)[:2]]
    if not targets:
        targets = [("peer", pid) for pid in sorted(entries)[:2]]
    return targets


# NVUE-surface peer-port tests (live `nv show peer-port`).
def _live_nvue_peers(engines) -> Dict[str, Dict[str, str]]:
    """Enable the knob and return the live NVUE peer listing; FAIL loud if empty."""
    _enable_peer_port(engines)
    peers = ibh.nvue_show_peer_ports(engines)
    assert peers, (
        "No peer-ports listed via NVUE (`nv show peer-port`). Enable the knob and "
        "confirm peers are attached and their links are Active (an SM must be running)."
    )
    return peers


# (family-id, leaf list, expected type, raw-subtree accessor name).
_NVUE_FAMILY_SPECS = [
    ("counters", PEER_PORT_COUNTER_FIELDS, GnmiTypeKind.COUNTER, "counters"),
    ("ber", PEER_PORT_BER_FIELDS, GnmiTypeKind.DECIMAL, "phy"),
    ("plr", PEER_PORT_PLR_FIELDS, GnmiTypeKind.UINT, "phy"),
]


@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.nvos_build
def test_peer_port_nvue_listing_and_identity(engines, devices, setup_topology):
    """Every NVUE-listed peer carries an identity, an associated switch port, and classifies as HCA."""
    peers = _live_nvue_peers(engines)
    problems: List[str] = []
    for pid, fields in sorted(peers.items()):
        if not ibh.peer_entry_identity(fields, fallback=""):
            problems.append(f"[{pid}] carries no identity (node-guid/port-guid)")
        if not ibh.peer_entry_aport(fields):
            problems.append(f"[{pid}] carries no associated-switch-port")
        if ibh.classify_peer_type(pid, fields) != PeerType.HCA:
            problems.append(f"[{pid}] peer-id does not classify as HCA")
    assert not problems, (
        "NVUE peer-port listing problems:\n  " + "\n  ".join(problems)
    )


@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.parametrize("family", _NVUE_FAMILY_SPECS, ids=lambda f: f[0])
def test_peer_port_nvue_fields_present_and_typed(engines, devices, setup_topology, family):
    """Each documented NVUE peer-port family is present and correctly typed on every live peer."""
    fam_id, fields, kind, subtree = family
    peers = _live_nvue_peers(engines)
    problems: List[str] = []

    for pid in sorted(peers):
        if subtree == "counters":
            sub = ibh.peer_port_counters(ibh.nvue_show_peer_port_raw(engines, pid))
        else:
            sub = ibh.peer_port_phy(ibh.nvue_peer_port_phy_raw(engines, pid))
        ibh.attach_dict(f"{fam_id} {pid}", sub)
        sub_ci = {k.lower(): v for k, v in sub.items()}
        for leaf in fields:
            if leaf.lower() not in sub_ci:
                problems.append(f"[{pid}] missing {fam_id} leaf {leaf!r}")
                continue
            err = ibh.gnmi_typecheck(kind, sub_ci[leaf.lower()])
            if err:
                problems.append(f"[{pid}] {fam_id} leaf {leaf!r}: {err}")

    assert not problems, (
        f"NVUE peer-port {fam_id} sweep found {len(problems)} problem(s):\n  " +
        "\n  ".join(problems)
    )


@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_peer_port_nvue_plane_mapping(engines, devices, setup_topology):
    """Each aggregated HCA peer fans out to per-plane rows with switch-ports under the parent's."""
    peers = _live_nvue_peers(engines)
    all_ids = set(peers)
    problems: List[str] = []

    for pid, fields in sorted(ibh.aggregated_peer_entries(peers).items()):
        peer_sw = ibh.peer_entry_aport(fields)
        if not peer_sw:
            problems.append(f"[{pid}] aggregated peer has no associated-switch-port")
            continue

        plane_ids = ibh.plane_peer_ids_for_parent(all_ids, pid)
        if not plane_ids:
            problems.append(f"[{pid}] has no per-plane peer rows in the listing")
            continue
        for plane_id in plane_ids:
            plane_sw = ibh.peer_entry_aport(peers.get(plane_id, {}))
            if not plane_sw:
                problems.append(f"[{pid}] plane peer {plane_id!r} has no associated-switch-port")
            elif not plane_sw.startswith(peer_sw):
                problems.append(
                    f"[{pid}] plane peer {plane_id!r} switch-port {plane_sw!r} is not under "
                    f"aggregated switch-port {peer_sw!r}"
                )

        raw = ibh.nvue_show_peer_port_raw(engines, pid)
        for plane_id, pf in sorted(ibh.peer_port_planes(raw).items()):
            for leaf in PEER_PORT_PLANE_FIELDS:
                if leaf not in pf:
                    problems.append(f"[{pid}] nested plane {plane_id} missing leaf {leaf!r}")
            plane_sw = str(pf.get("associated-switch-port", "")).strip()
            if peer_sw and plane_sw and not plane_sw.startswith(peer_sw):
                problems.append(
                    f"[{pid}] nested plane {plane_id} switch-port {plane_sw!r} is not under "
                    f"the peer switch-port {peer_sw!r}"
                )

    assert not problems, (
        "NVUE peer-port plane-mapping problems:\n  " + "\n  ".join(problems)
    )


@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_peer_port_nvue_knob_and_interval_lifecycle(engines, devices, setup_topology):
    """The peer-port knob round-trips: enable lists peers, interval is settable, disable drains."""
    system = System()
    peers = _live_nvue_peers(engines)
    assert peers, "No peers after enabling the knob"

    with allure.step("Polling interval is settable and reflected"):
        for interval in (10, 5):
            system.peer_port.set(
                op_param_name="interval", op_param_value=interval,
                apply=True, ask_for_confirmation='-y',
            ).verify_result()
            # apply returns before the value settles; poll the show until it converges.
            shown = ""
            deadline = time.time() + PEER_PORT_INTERVAL_SETTLE_SEC
            while time.time() < deadline:
                shown = str(system.peer_port.parse_show().get("interval", "")).strip()
                if shown == str(interval):
                    break
                time.sleep(1)
            assert shown == str(interval), (
                f"peer-port interval not applied: set {interval}, show reports {shown!r} "
                f"after {PEER_PORT_INTERVAL_SETTLE_SEC}s"
            )

    with allure.step("Disabling the knob drains the peer-port listing"):
        system.peer_port.unset(
            op_param=NvuePaths.KEY_STATE, apply=True, ask_for_confirmation='-y',
        ).verify_result()
        drained = _wait_for_peer_ports(engines, present=False)
        assert not drained, (
            f"peer-ports still listed after disabling the knob: {sorted(drained)!r}"
        )


# 5.6 test_peer_port_lists_hca (plan §5.5)
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.parametrize("api", ALL_APIS)
def test_peer_port_lists_hca(engines, devices, gnmi_client, setup_topology, api):
    """The peer-port view lists ingested HCA peers: classified HCA, Aport-anchored, DB-consistent."""
    _enable_peer_port(engines)
    _assert_hca_ingestion_directly_on_dut(engines, setup_topology)
    _require_external_hca_topology(setup_topology)
    expected_guids = setup_topology.external_hca_neighbor_guids()
    valid_aports = setup_topology.all_local_port_aliases()

    with allure.step(f"List peer-ports via {api}"):
        entries = ibh.list_peer_ports_via_api(api, engines, gnmi_client)
        ibh.attach_dict(f"peer-ports via {api}", entries)
        assert entries, (
            f"No peer-ports listed via {api}. The HCA peer-port feature build "
            "may not be delivered, or gpu-telemetry / NMX-T Lite is not running."
        )

    with allure.step("HCA peer-types are present, each with an Aport"):
        hca_live = ibh.filter_peer_entries_by_type(entries, PeerType.HCA)
        assert hca_live, f"No peer-type={PeerType.HCA} entries via {api}"
        _assert_every_entry_references_an_aport(entries)

    with allure.step("Aggregated HCA peers cover external hosts from connectivity JSON"):
        # Compare on the aggregated tier only; reconcile hosts by node GUID.
        hca_aggr = ibh.aggregated_peer_entries(hca_live)
        assert hca_aggr, f"No aggregated HCA peers via {api} (only per-plane members?)"
        _assert_external_hca_neighbor_guids_present(
            ibh.aggregated_hca_node_guids(hca_aggr),
            expected_guids,
            label=api,
            row_count=len(hca_aggr),
        )
        _assert_peers_reference_known_aports(hca_aggr, valid_aports)

    one_pid = sorted(hca_aggr)[0]
    with allure.step(f"Show one HCA peer ({one_pid}) and validate its detail"):
        detail = ibh.get_peer_port_via_api(api, engines, gnmi_client, one_pid)
        ibh.attach_dict(f"hca peer detail {one_pid}", detail)
        assert ibh.peer_entry_type(detail, one_pid) == PeerType.HCA, (
            f"Peer {one_pid} detail peer-type is {ibh.peer_entry_type(detail, one_pid)!r}, expected HCA"
        )
        assert ibh.peer_entry_identity(detail, fallback=""), (
            f"Peer {one_pid} detail carries no identity (e.g. node GUID)"
        )
        assert ibh.peer_entry_aport(detail), f"Peer {one_pid} detail has no Aport reference"
        assert any(f in detail for f in PEER_PORT_ADDITIVE_FIELDS), (
            f"Peer {one_pid} detail exposes no counters; keys={sorted(detail)!r}"
        )

    with allure.step(f"Compare {one_pid} counters via {api} against System DB"):
        db_row = ibh.read_peer_port_row(engines, one_pid)
        ibh.attach_dict(f"db row {one_pid}", db_row)
        assert db_row, f"No System DB row PEER_COUNTERS:{one_pid}"
        # Pair API leaf and DB SAI field through the API->DB field map.
        compared = ibh.assert_counters_within_tolerance(
            detail, db_row, PEER_PORT_API_TO_DB_FIELD, SAMPLING_JITTER_TOLERANCE_PCT,
            label=f"{api} vs DB for {one_pid}",
        )
        assert compared > 0, (
            f"Could not compare any counter for {one_pid} between {api} and DB "
            f"(api keys={sorted(detail)!r}, db keys={sorted(db_row)!r})"
        )


# 5.7 test_peer_port_api_parity_nvue_vs_gnmi (nice-to-have)
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_peer_port_api_parity_nvue_vs_gnmi(engines, devices, gnmi_client, setup_topology):
    """The same HCA peer is reported identically by NVUE and gNMI (peer-type, Aport, counters)."""
    _require_hca_topology(setup_topology)
    _enable_peer_port(engines)

    with allure.step("List peer-ports via NVUE and via gNMI"):
        nvue_entries = ibh.list_peer_ports_via_api(API_NVUE_CLI, engines, gnmi_client)
        gnmi_entries = ibh.list_peer_ports_via_api(API_GNMIC, engines, gnmi_client)
        # Compare like-for-like on the aggregated tier only.
        nvue_hca = ibh.aggregated_peer_entries(ibh.filter_peer_entries_by_type(nvue_entries, PeerType.HCA))
        gnmi_hca = ibh.aggregated_peer_entries(ibh.filter_peer_entries_by_type(gnmi_entries, PeerType.HCA))
        ibh.attach_dict("nvue hca", nvue_hca)
        ibh.attach_dict("gnmi hca", gnmi_hca)
        assert nvue_hca, "No HCA peers via NVUE"
        assert gnmi_hca, "No HCA peers via gNMI"

    with allure.step("NVUE and gNMI expose the same aggregated HCA peer ids"):
        nvue_only = sorted(set(nvue_hca) - set(gnmi_hca))
        gnmi_only = sorted(set(gnmi_hca) - set(nvue_hca))
        assert not nvue_only and not gnmi_only, (
            f"Aggregated HCA peer-id sets differ across surfaces: "
            f"NVUE-only={nvue_only!r}, gNMI-only={gnmi_only!r}"
        )

    with allure.step("Per-peer parity: Aport and counters match across surfaces"):
        for pid in sorted(nvue_hca):
            n_fields, g_fields = nvue_hca[pid], gnmi_hca[pid]
            assert ibh.peer_entry_aport(n_fields) == ibh.peer_entry_aport(g_fields), (
                f"HCA peer {pid!r} Aport differs: NVUE={ibh.peer_entry_aport(n_fields)!r}, "
                f"gNMI={ibh.peer_entry_aport(g_fields)!r}"
            )
            ibh.assert_counters_within_tolerance(
                n_fields, g_fields, PEER_PORT_ADDITIVE_FIELDS, SAMPLING_JITTER_TOLERANCE_PCT,
                label=f"NVUE vs gNMI for HCA peer {pid!r}",
            )


# 6.10 test_hca_xcset_ingestion (plan §6.2)
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.requires_hfnm
@pytest.mark.timeout(12 * MINUTE, func_only=True)
def test_hca_xcset_ingestion(engines, devices, setup_topology, setup_name, request):
    """gpu-telemetry writes each HCA as its own DB row; additive counters stay monotonic under traffic."""
    _enable_peer_port_for_hca(engines, tier=PeerPortFields.TIER_AGGREGATED)
    _assert_hca_ingestion_directly_on_dut(engines, setup_topology)
    _require_external_hca_topology(setup_topology)
    expected_guids = setup_topology.external_hca_neighbor_guids()
    expected_ports = setup_topology.external_hca_switch_port_aliases()
    valid_aports = setup_topology.all_local_port_aliases()

    with allure.step("List aggregated HCA peer-port rows in System DB"):
        # Ingestion identity is checked on the aggregated tier.
        hca_rows = ibh.list_hca_peer_rows(engines, tier=PeerPortFields.TIER_AGGREGATED)
        ibh.attach_dict("hca aggregated rows", hca_rows)
        assert hca_rows, (
            "No aggregated HCA peer-port rows (PEER_COUNTERS:<hca*> with "
            "peer_port_tier=aggregated) in System DB. The HCA feature build may "
            "not be delivered, or gpu-telemetry / NMX-T Lite (HCA xcset) is not "
            "running."
        )

    with allure.step("Ingested HCA rows cover external hosts from connectivity JSON"):
        _assert_external_hca_neighbor_guids_present(
            ibh.aggregated_hca_node_guids(hca_rows),
            expected_guids,
            label="System DB",
            row_count=len(hca_rows),
        )
        _assert_switch_ports_cover_external_hca(hca_rows, expected_ports)
        _assert_peers_reference_known_aports(hca_rows, valid_aports, label="row")

    one_pid = sorted(hca_rows)[0]
    with allure.step(f"Inspect one HCA peer-port row ({one_pid})"):
        row = hca_rows[one_pid]
        assert ibh.peer_entry_identity(row, fallback=""), f"HCA row {one_pid} carries no identity"
        assert ibh.peer_entry_type(row) == PeerType.HCA
        assert ibh.peer_row_aports(row), f"HCA row {one_pid} has no Aport reference"
        populated = [f for f in PEER_PORT_DB_ADDITIVE_FIELDS if str(row.get(f, "")).strip() not in ("", "0")]
        ibh.attach_dict(f"hca row {one_pid}", {"row": row, "populated_counters": populated})

    players, interfaces = _require_active_fabric_and_traffic(engines, request, setup_name)

    with allure.step("Snapshot the HCA row, drive an IB traffic burst, re-read"):
        initial = ibh.read_peer_port_row(engines, one_pid)
        Tools.TrafficGeneratorTool.send_ib_traffic(players, interfaces, setup_name, True).verify_result()
        time.sleep(PEER_TELEMETRY_SAMPLING_SEC)
        after = ibh.read_peer_port_row(engines, one_pid)
        ibh.attach_dict(f"hca {one_pid} pre/post traffic", {"initial": initial, "after": after})
        assert after, f"HCA row {one_pid} vanished after traffic"

    with allure.step("Additive counters are monotonic across the traffic burst"):
        ibh.assert_counters_monotonic(
            initial, after, PEER_PORT_DB_ADDITIVE_FIELDS,
            label=f"HCA {one_pid} across traffic",
        )


# 6.x test_hca_xcset_matches_system_db (ingestion fidelity: xcset <-> System DB)
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_hca_xcset_matches_system_db(engines, devices, setup_topology):
    """Every ingested DB row traces back to an xcset HCA with matching counters (plane-tier, DB subset of xcset)."""
    _require_hca_topology(setup_topology)
    _enable_peer_port_for_hca(engines, tier=PeerPortFields.TIER_PLANE)

    with allure.step("Snapshot plane-tier DB rows and xcset back-to-back"):
        plane_rows = ibh.list_hca_peer_rows(engines, tier=PeerPortFields.TIER_PLANE)
        xcset = ibh.read_hca_xcset(engines)
        ibh.attach_dict("nmx-t-for-ib hca xcset (by join key)", xcset)
        ibh.attach_dict("system db hca plane rows", plane_rows)
        assert xcset, (
            "NMX-T-for-IB served an empty HCA xcset. The HCA feature build may "
            "not be delivered, or NMX-T-for-IB is not serving the HCA report."
        )
        assert plane_rows, (
            "No per-plane HCA peer-port rows in System DB while NMX-T-for-IB is "
            "serving HCAs - peer-telemetry has not ingested the xcset."
        )

    with allure.step("Every ingested plane DB row is present in the xcset (joined on port_guid+port_num)"):
        db_by_key = {ibh.hca_xcset_join_key(row): (pid, row) for pid, row in plane_rows.items()}
        bad_keys = [pid for pid, row in plane_rows.items() if not ibh.hca_xcset_join_key(row)]
        assert not bad_keys, (
            f"System DB HCA row(s) missing port_guid/port_num; cannot join to xcset: {bad_keys!r}"
        )
        xcset_ids, db_ids = set(xcset), set(db_by_key)
        missing = db_ids - xcset_ids
        assert not missing, (
            "System DB ingested HCA peer(s) that NMX-T-for-IB does not serve "
            "(fabricated/stale ingestion): "
            f"{sorted((db_by_key[k][0], k) for k in missing)!r}"
        )
        source_only = xcset_ids - db_ids
        if source_only:
            logger.info(
                "xcset peers not aliased by NVOS (expected for FNM / front-panel-module "
                "/ non-swXpY ports): %r", sorted(source_only)
            )

    with allure.step("Per-plane additive counters match within sampling jitter"):
        compared = 0
        for key in sorted(db_ids):
            pid, db_row = db_by_key[key]
            compared += ibh.assert_counters_within_tolerance(
                db_row, xcset[key], PEER_PORT_DB_ADDITIVE_FIELDS, SAMPLING_JITTER_TOLERANCE_PCT,
                label=f"NMX-T-for-IB vs System DB for HCA plane {pid!r} ({key})",
            )
        assert compared > 0, (
            "No additive counters were comparable between the xcset and System DB. "
            "Check that NMXT_XCSET_TO_DB_FIELD lines the xcset columns up with the "
            "COUNTERS_DB SAI field names."
        )


# 6.11 test_hca_peerport_to_aport_mapping (plan §6.4)
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_hca_peerport_to_aport_mapping(engines, devices, gnmi_client, setup_topology):
    """Every HCA peer-port maps to a real local switch port matching external HCA cabling (by plane IB port)."""
    _enable_peer_port(engines)
    _assert_hca_ingestion_directly_on_dut(engines, setup_topology)
    _require_external_hca_topology(setup_topology)
    expected_guids = setup_topology.external_hca_neighbor_guids()
    expected_ports = setup_topology.external_hca_switch_port_aliases()
    valid_aports = setup_topology.all_local_port_aliases()

    with allure.step("List peer-ports via gNMI"):
        entries = ibh.gnmi_get_peer_port_list(gnmi_client)
        ibh.attach_dict("peer-ports via gnmi", entries)
        assert entries, "No peer-ports via gNMI (HCA feature build delivered?)"

    with allure.step("Each aggregated HCA peer references a valid local Aport"):
        # Aport-mapping check is on the aggregated tier only.
        hca_live = ibh.aggregated_peer_entries(
            ibh.filter_peer_entries_by_type(entries, PeerType.HCA))
        assert hca_live, f"No aggregated peer-type={PeerType.HCA} entries via gNMI"
        _assert_peers_reference_known_aports(hca_live, valid_aports)

    with allure.step("Switch ports carrying HCA peers match external HCA cabling"):
        _assert_external_hca_neighbor_guids_present(
            ibh.aggregated_hca_node_guids(hca_live),
            expected_guids,
            label="gNMI",
            row_count=len(hca_live),
        )
        _assert_switch_ports_cover_external_hca(hca_live, expected_ports)


# 6.12 test_hca_peerport_aggregation (plan §6.5)
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_hca_peerport_aggregation(engines, devices, gnmi_client, setup_topology):
    """Validate HCA parent/child aggregation: plane rows link to parent; counters sum when guids partition."""
    _require_hca_topology(setup_topology)
    _enable_peer_port_for_hca(engines, tier=PeerPortFields.TIER_PLANE)

    with allure.step("Find an aggregated HCA with >= 2 ingested plane rows"):
        plane_rows = ibh.list_hca_peer_rows(engines, tier=PeerPortFields.TIER_PLANE)
        assert plane_rows, "No per-plane HCA peer-port rows in System DB (feature build delivered?)"
        by_parent = {}
        for pid, row in plane_rows.items():
            by_parent.setdefault(ibh.peer_row_parent(row), []).append(pid)
        candidates = sorted(p for p, pids in by_parent.items() if p and len(pids) >= 2)
        planes_per_parent = {p: len(pids) for p, pids in by_parent.items()}
        assert candidates, (
            "No HCA has >= 2 ingested plane rows; aggregation needs such a setup "
            f"(test plan §6.5 precondition). planes-per-parent: {planes_per_parent!r}"
        )
        parent = candidates[0]
        member_pids = by_parent[parent]

    with allure.step(f"Aggregated row exists and plane rows link to {parent}"):
        plane_rows = ibh.list_hca_peer_rows(engines, tier=PeerPortFields.TIER_PLANE)
        aggregate = ibh.read_peer_port_row(engines, parent)
        ibh.attach_dict(f"aggregate {parent}", aggregate)
        assert aggregate, (
            f"No aggregated peer-port row PEER_COUNTERS:{parent} for a parent "
            f"referenced by plane rows {member_pids!r}."
        )
        assert ibh.peer_row_tier(aggregate) == PeerPortFields.TIER_AGGREGATED, (
            f"PEER_COUNTERS:{parent} is not tier=aggregated: {ibh.peer_row_tier(aggregate)!r}"
        )
        for pid in member_pids:
            row = plane_rows[pid]
            assert ibh.peer_row_tier(row) == PeerPortFields.TIER_PLANE, (
                f"Plane peer {pid!r} is not tier=plane: {ibh.peer_row_tier(row)!r}"
            )
            assert ibh.peer_row_parent(row) == parent, (
                f"Plane peer {pid!r} parent is {ibh.peer_row_parent(row)!r}, expected {parent!r}"
            )

    if not ibh.hca_aggregate_counters_partitioned_by_planes(aggregate, plane_rows, member_pids):
        logger.info(
            "Skipping counter roll-up check for %s: plane port guids differ from "
            "aggregate port guid (switch-leg plane rows on multiplanar mamba); "
            "parent/child linkage validated above.",
            parent,
        )
        return

    with allure.step("Sum per-plane additive counters and compare to the aggregate"):
        compared = 0
        for field in PEER_PORT_DB_ADDITIVE_FIELDS:
            member_vals = []
            for pid in member_pids:
                raw = plane_rows[pid].get(field)
                if raw is None:
                    continue
                try:
                    member_vals.append(ibh.parse_counter_value(raw))
                except (AssertionError, ValueError):
                    continue
            if not member_vals or field not in aggregate:
                continue
            try:
                agg_val = ibh.parse_counter_value(aggregate[field])
            except (AssertionError, ValueError):
                continue
            summed = sum(member_vals)
            assert ibh.values_within_tolerance(summed, agg_val, SAMPLING_JITTER_TOLERANCE_PCT), (
                f"HCA {parent} aggregate {field}={agg_val} != sum of "
                f"{len(member_pids)} plane rows ({summed})"
            )
            compared += 1
        assert compared > 0, (
            f"Could not compare any additive counter between {parent} and its "
            f"plane rows {member_pids!r} (agg keys={sorted(aggregate)!r})"
        )


# 6.13 test_hca_peerport_db_api_set_consistency (nice-to-have)
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_hca_peerport_db_api_set_consistency(engines, devices, gnmi_client, setup_topology):
    """The HCA peer set in System DB exactly matches gNMI, with unique keys (no duplicate ingestion)."""
    _require_hca_topology(setup_topology)
    _enable_peer_port_for_hca(engines)

    with allure.step("Collect HCA rows from System DB and HCA entries from gNMI"):
        db_rows = ibh.list_hca_peer_rows(engines)
        api_hca = ibh.filter_peer_entries_by_type(ibh.gnmi_get_peer_port_list(gnmi_client), PeerType.HCA)
        ibh.attach_dict("db hca rows", db_rows)
        ibh.attach_dict("api hca entries", api_hca)
        assert db_rows, "No HCA peer-port rows in System DB (HCA feature build delivered?)"
        assert api_hca, "No HCA peer-ports via gNMI"

    with allure.step("Peer-port DB keys are unique (no duplicate ingestion)"):
        pids = [ibh.peer_id_from_key(k) for k in ibh.list_peer_port_keys(engines)]
        dups = sorted({p for p in pids if pids.count(p) > 1})
        assert not dups, f"Duplicate PEER_COUNTERS keys ingested: {dups!r}"

    with allure.step("DB and gNMI expose the same HCA identity set"):
        db_idents = {ibh.peer_entry_identity(r, fallback=pid) for pid, r in db_rows.items()}
        api_idents = {ibh.peer_entry_identity(f, fallback=pid) for pid, f in api_hca.items()}
        missing_in_api = db_idents - api_idents
        orphan_in_api = api_idents - db_idents
        assert not missing_in_api, (
            f"HCA rows present in System DB but not exposed via gNMI: {sorted(missing_in_api)!r}"
        )
        assert not orphan_in_api, (
            f"HCA peers exposed via gNMI with no System DB row: {sorted(orphan_in_api)!r}"
        )


# 6.14 test_peer_telemetry_health_steady_state_healthy (nice-to-have)
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_peer_telemetry_health_steady_state_healthy(engines, devices, setup_topology):
    """In steady state, gpu-telemetry publishes PEER_PORT_TELEMETRY_HEALTH healthy in STATE_DB."""
    _require_hca_topology(setup_topology)
    _enable_peer_port(engines)

    with allure.step("Ensure a valid HCA xcset backend (clear leftover resiliency state)"):
        restored = ibh.ensure_valid_hca_xcset_backend(engines)
        ibh.attach_dict("xcset backend cleanup", {"restored": restored})

    with allure.step("Wait for PEER_PORT_TELEMETRY_HEALTH to settle healthy"):
        health, raw = ibh.wait_for_peer_telemetry_healthy(engines)
        ibh.attach_dict("peer telemetry health", {"classification": health, "raw": raw})
        assert raw, (
            "PEER_PORT_TELEMETRY_HEALTH is not present in STATE_DB; gpu-telemetry should "
            "publish an initial health state (HLD §6.1.2). Confirm the key with dev."
        )
        assert health == "healthy", (
            f"Steady-state PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}); expected healthy."
        )


def _is_gnmi_entry_absent(exc: BaseException) -> bool:
    """True when a gNMI read failed because the container/entry is not implemented
    for that peer (an FNM-connected HCA with no transceiver/component, or a plane leg
    whose link is down). Such a per-peer applicability gap is tolerated by the schema
    sweep rather than failed."""
    msg = str(exc).lower()
    return "unimplemented" in msg and "cannot find the entry" in msg


# 7.4 test_peer_port_gnmi_schema_paths_present_and_typed (nice-to-have)
# skip_clear_config: keep peer-port enabled across all variants; per-variant
# disable + cold re-ingest otherwise flakes on not-yet-populated leaves.
@pytest.mark.skip_clear_config
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(12 * MINUTE, func_only=True)
@pytest.mark.parametrize("group", PEER_PORT_SCHEMA_GROUPS, ids=lambda g: g.group_id)
def test_peer_port_gnmi_schema_paths_present_and_typed(engines, devices, gnmi_client, setup_topology, group):
    """Positive gNMI schema sweep of the nvidia-peer-port model: every documented leaf present and typed."""
    _enable_peer_port(engines)
    targets = _select_peer_port_targets(gnmi_client)
    problems: List[str] = []
    # `pending` leaves (known build gap): tolerated-missing but still type-checked.
    pending = {leaf.lower() for leaf in getattr(group, "pending", frozenset())}

    # A peer that doesn't implement this container (an FNM-connected HCA with no
    # transceiver/component, or a plane leg whose link is down) answers the subscribe
    # with gNMI Unimplemented "cannot find the entry" - a per-peer applicability gap,
    # not a schema violation. Tolerate it and count how many peers actually exposed it.
    exposed = 0
    with allure.step(f"Sweep peer-port container {group.group_id!r} across {len(targets)} peer(s)"):
        for role, pid in targets:
            prefix = group.prefix.format(name=pid)
            with allure.independent_step(f"Sweep {group.group_id} on {role} {pid} ({prefix})"):
                try:
                    payload = ibh.gnmi_get_flat(gnmi_client, prefix=prefix, path="")
                except AssertionError as exc:
                    if _is_gnmi_entry_absent(exc):
                        logger.info(
                            "[%s %s] container %s not implemented for this peer "
                            "(gNMI 'cannot find the entry'); tolerated as N/A",
                            role, pid, prefix,
                        )
                        continue
                    raise
                exposed += 1
                ibh.attach_dict(f"{group.group_id} {role} {pid}", payload)
                payload_ci = {k.lower(): v for k, v in payload.items()}
                for leaf, kind in group.leaves.items():
                    if leaf.lower() not in payload_ci:
                        if leaf.lower() in pending:
                            logger.info(
                                "[%s %s] pending leaf %r absent under %s (known build gap, tolerated)",
                                role, pid, leaf, prefix,
                            )
                            continue
                        problems.append(f"[{role} {pid}] missing leaf {leaf!r} under {prefix}")
                        continue
                    if leaf.lower() in pending:
                        logger.warning(
                            "[%s %s] pending leaf %r is now exposed under %s - drop it from "
                            "the group's `pending` set so the sweep enforces it again",
                            role, pid, leaf, prefix,
                        )
                    err = ibh.gnmi_typecheck(kind, payload_ci[leaf.lower()])
                    if err:
                        problems.append(f"[{role} {pid}] leaf {leaf!r}: {err}")

    if exposed == 0:
        pytest.skip(
            f"peer-port container {group.group_id!r} is not exposed by any peer on this "
            f"setup (FNM-only HCA or down plane legs); nothing to schema-check here."
        )
    assert not problems, (
        f"peer-port gNMI schema sweep for container {group.group_id!r} found "
        f"{len(problems)} problem(s):\n  " + "\n  ".join(problems)
    )


# 7.5 test_peer_port_gnmi_list_subtree_paths_present_and_typed (nice-to-have)
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(10 * MINUTE, func_only=True)
@pytest.mark.parametrize("group", PEER_PORT_LIST_SCHEMA_GROUPS, ids=lambda g: g.group_id)
def test_peer_port_gnmi_list_subtree_paths_present_and_typed(engines, devices, gnmi_client, setup_topology, group):
    """Positive schema sweep of the peer-port list subtrees: each exposed member's value leaf present and typed (empty tolerated)."""
    _enable_peer_port(engines)
    targets = _select_peer_port_targets(gnmi_client)
    problems: List[str] = []

    with allure.step(f"Sweep peer-port list {group.group_id!r} across {len(targets)} peer(s)"):
        for role, pid in targets:
            prefix = group.list_prefix.format(name=pid)
            with allure.independent_step(f"Sweep list {group.group_id} on {role} {pid} ({prefix})"):
                members = ibh.gnmi_list_members(gnmi_client, prefix)
                ibh.attach_dict(f"{group.group_id} {role} {pid}", members)
                if not members:
                    logger.info(
                        "Peer-port list %s on %s %s exposes no members - tolerated "
                        "(no oper-status leaf to force presence)", group.group_id, role, pid,
                    )
                    continue
                for member_key, leaves in members.items():
                    for leaf, kind in group.leaves.items():
                        present = next((v for k, v in leaves.items() if k.lower() == leaf.lower()), None)
                        if present is None:
                            problems.append(
                                f"[{role} {pid}] member [{member_key}] missing leaf {leaf!r} under {prefix}"
                            )
                            continue
                        err = ibh.gnmi_typecheck(kind, present)
                        if err:
                            problems.append(f"[{role} {pid}] member [{member_key}] leaf {leaf!r}: {err}")

    assert not problems, (
        f"peer-port gNMI list-subtree sweep for {group.group_id!r} found "
        f"{len(problems)} problem(s):\n  " + "\n  ".join(problems)
    )


# 9.2 test_hca_ingestion_persists_across_reboot (nice-to-have)
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_hca_ingestion_persists_across_reboot(engines, devices, gnmi_client, setup_topology, topology_obj):
    """The peer-port knob survives a reboot and gpu-telemetry re-ingests the HCA rows."""
    expected_hca = _require_hca_topology(setup_topology)
    system = System()

    with allure.step("Enable the peer-port knob and save config"):
        # Persistence is scoped to the aggregated tier (matching the rest of the suite).
        ibh.ensure_valid_hca_xcset_backend(engines)
        _enable_peer_port_for_hca(engines, tier=PeerPortFields.TIER_AGGREGATED)
        health, raw = ibh.wait_for_peer_telemetry_healthy(engines)
        ibh.attach_dict("peer telemetry health before reboot", {"classification": health, "raw": raw})
        assert health == "healthy", (
            f"PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}) before reboot; "
            "expected healthy so the baseline rows come from a live xcset feed"
        )
        NvueGeneralCli.save_config(engines.dut)

    with allure.step("Baseline aggregated HCA rows before reboot"):
        before = ibh.list_hca_peer_rows(engines, tier=PeerPortFields.TIER_AGGREGATED)
        ibh.attach_dict("hca rows before reboot", before)
        assert before, "No aggregated HCA peer-port rows before reboot (HCA feature build delivered?)"

    with allure.step("Reboot the system"):
        system.reboot.action_reboot(
            engine=engines.dut,
            device=devices.dut,
            topology_obj=topology_obj,
            should_wait_till_system_ready=True,
        )

    with allure.step("After boot: NMX-T-for-IB backend is active"):
        assert ibh.wait_for_nmxt_ib_active(engines, timeout_sec=PEER_PORT_COLD_BOOT_GRACE_SEC), (
            f"{NMXT_IB_SERVICE} did not reach active within {PEER_PORT_COLD_BOOT_GRACE_SEC}s "
            "after reboot; resiliency tests mask this unit and the mask persists across reboot"
        )

    with allure.step("After boot: peer-port knob is still enabled"):
        # Poll until the knob reads enabled (services settle a few seconds in).
        state = ""
        deadline = time.time() + PEER_PORT_ENABLE_GRACE_SEC + PEER_PORT_INTERVAL_SETTLE_SEC
        while time.time() < deadline:
            state = str(system.peer_port.parse_show().get(NvuePaths.KEY_STATE, "")).lower()
            if state == NvuePaths.STATE_ENABLED:
                break
            time.sleep(2)
        assert state == NvuePaths.STATE_ENABLED, (
            f"peer-port knob did not persist across reboot; expected 'enabled', got {state!r} "
            f"after {PEER_PORT_ENABLE_GRACE_SEC + PEER_PORT_INTERVAL_SETTLE_SEC}s"
        )

    with allure.step("After boot: re-apply peer-port enable to kick HCA ingestion"):
        # Re-apply replays the enable path (nmx-t-ib restart) a persisted knob skips.
        _enable_peer_port(engines)

    with allure.step("After boot: HCA ingestion resumes (aggregated rows repopulate)"):
        interval = _peer_port_interval_sec()
        timeout = PEER_PORT_COLD_BOOT_GRACE_SEC + interval * PEER_PORT_COLD_BOOT_SETTLE_INTERVALS
        after, missing = ibh.wait_for_hca_peer_rows(
            engines,
            tier=PeerPortFields.TIER_AGGREGATED,
            required=set(before),
            timeout_sec=timeout,
            poll_sec=min(interval, 5),
        )
        ibh.attach_dict("hca rows after reboot", after)
        health, raw = ibh.read_peer_telemetry_health(engines)
        ibh.attach_dict("peer telemetry health after reboot", {"classification": health, "raw": raw})
        assert not missing, (
            f"Aggregated HCA rows did not repopulate after reboot within {timeout}s; "
            f"still missing: {sorted(missing)!r}"
        )
        # Reconcile against resolved switch-port aliases (sw5p1), not ibdiagnet labels.
        expected_aports = setup_topology.external_hca_switch_port_aliases() or {
            p.aport for p in expected_hca
        }
        for pid, row in after.items():
            assert ibh.peer_row_aports(row) & expected_aports, (
                f"After reboot HCA row {pid!r} references unexpected Aport(s) "
                f"{sorted(ibh.peer_row_aports(row))!r}; expected one of "
                f"{sorted(expected_aports)!r}"
            )


# 10.5 test_hca_peerport_behavior_on_peer_down (plan §10.3)
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(12 * MINUTE, func_only=True)
def test_hca_peerport_behavior_on_peer_down(engines, gnmi_client, setup_topology):
    """A down HCA peer's row disappears within ~10s and reappears with the same Aport on recovery."""
    _require_hca_topology(setup_topology)
    _enable_peer_port_for_hca(engines)

    with allure.step("Pick a live HCA peer with a resolved switch port and snapshot its row"):
        hca_rows = ibh.list_hca_peer_rows(engines, tier=PeerPortFields.TIER_AGGREGATED)
        assert hca_rows, "No HCA peer-port rows in System DB (HCA feature build delivered?)"
        peer_id = ibh.pick_hca_peer_with_switch_port(engines, hca_rows)
        assert peer_id, (
            "No HCA peer has a resolved switch-port mapping (PEER_PORT_MAPPING "
            "switch_port_alias), so none can be driven down from the switch side. "
            f"Seen peers: {sorted(hca_rows)!r}"
        )
        snapshot = ibh.read_peer_port_row(engines, peer_id)
        ibh.attach_dict(f"snapshot {peer_id}", snapshot)
        assert snapshot, f"HCA row {peer_id} not readable"
        assert ibh.peer_entry_type(snapshot) == PeerType.HCA

    with allure.step(f"Bring HCA peer {peer_id} down"):
        ibh.simulate_hca_peer_down(engines, peer_id)

    try:
        with allure.step(f"Within ~{HCA_PEER_DOWN_DEADLINE_SEC}s the peer-port row disappears"):
            # A down peer has no row anywhere (DB, gNMI, NVUE); last peer -> "No data".
            deadline = time.time() + HCA_PEER_DOWN_DEADLINE_SEC
            after_row = ibh.read_peer_port_row(engines, peer_id)
            present = peer_id in ibh.gnmi_get_peer_port_list(gnmi_client)
            nvue_peers = ibh.nvue_show_peer_ports(engines)
            while (after_row or present or peer_id in nvue_peers) and time.time() < deadline:
                time.sleep(2)
                after_row = ibh.read_peer_port_row(engines, peer_id)
                present = peer_id in ibh.gnmi_get_peer_port_list(gnmi_client)
                nvue_peers = ibh.nvue_show_peer_ports(engines)
            ibh.attach_dict(f"after-down {peer_id}", {
                "present_in_gnmi": present,
                "listed_in_nvue": peer_id in nvue_peers,
                "db_row": after_row,
            })
            assert not after_row and not present and peer_id not in nvue_peers, (
                f"HCA peer {peer_id} went down but its peer-port row did not disappear "
                f"within {HCA_PEER_DOWN_DEADLINE_SEC}s (contract: no row for a down peer). "
                f"gnmi_present={present}, nvue_listed={peer_id in nvue_peers}, db_row={after_row!r}"
            )
            if not nvue_peers:
                assert ibh.nvue_peer_port_shows_no_data(engines), (
                    "All peer-ports are down but `nv show peer-port` did not render the "
                    "'No data' sentinel (checked case-insensitively)"
                )
    finally:
        with allure.step(f"Bring HCA peer {peer_id} back up"):
            ibh.restore_hca_peer(engines, peer_id)

    with allure.step("After recovery the row reappears with the same Aport association"):
        deadline = time.time() + HCA_PEER_RECOVERY_GRACE_SEC + _peer_port_interval_sec() * HCA_PEER_RECOVERY_SETTLE_INTERVALS
        restored = ibh.read_peer_port_row(engines, peer_id)
        while not restored and time.time() < deadline:
            time.sleep(min(_peer_port_interval_sec(), 5))
            restored = ibh.read_peer_port_row(engines, peer_id)
        ibh.attach_dict(f"restored {peer_id}", restored)
        assert restored, f"HCA peer {peer_id} row did not reappear after recovery"
        assert ibh.peer_entry_type(restored) == PeerType.HCA
        assert ibh.peer_entry_aport(restored) == ibh.peer_entry_aport(snapshot), (
            f"HCA peer {peer_id} Aport association changed after recovery: "
            f"{ibh.peer_entry_aport(snapshot)!r} -> {ibh.peer_entry_aport(restored)!r}"
        )


# 10.6 test_hca_xcset_unreachable_resiliency (plan §10.5)
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_hca_xcset_unreachable_resiliency(engines, devices, setup_topology):
    """On an NMX-T-for-IB outage: rows preserved, health -> degraded, recovery logged; health -> healthy on return."""
    _require_hca_topology(setup_topology)
    _enable_peer_port_for_hca(engines)

    with allure.step("Snapshot HCA rows and confirm health is healthy"):
        snapshot = ibh.list_hca_peer_rows(engines)
        assert snapshot, "No HCA peer-port rows in System DB (HCA feature build delivered?)"
        health, raw = ibh.read_peer_telemetry_health(engines)
        ibh.attach_dict("initial health", {"classification": health, "raw": raw})
        assert health == "healthy", (
            f"PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}) before the outage; "
            "expected healthy. Confirm the health key/values with dev (HLD §6.1.2)."
        )

    with allure.step("Make NMX-T Lite unreachable"):
        ibh.make_nmxt_lite_unreachable(engines)

    try:
        with allure.step("After >= 2 sampling cycles: rows preserved, health degraded, backoff logged"):
            time.sleep(2 * PEER_TELEMETRY_SAMPLING_SEC)
            during = ibh.list_hca_peer_rows(engines)
            ibh.attach_dict("rows during outage", during)
            # Retention: the row SET must match the pre-outage snapshot.
            assert set(during) == set(snapshot), (
                "HCA rows were added/dropped during the NMX-T Lite outage; FS §6.1.2 "
                "requires the last-good rows to be retained. "
                f"before={sorted(snapshot)!r}, during={sorted(during)!r}"
            )
            # Frozen within the outage: re-sample and require byte-identical rows.
            time.sleep(2 * PEER_TELEMETRY_SAMPLING_SEC)
            during_b = ibh.list_hca_peer_rows(engines)
            ibh.attach_dict("rows later in outage", during_b)
            drift = "were WIPED" if not during_b else "kept changing"
            assert during_b == during, (
                f"HCA rows {drift} across the NMX-T Lite outage window "
                f"(during={len(during)} rows, ~{2 * PEER_TELEMETRY_SAMPLING_SEC}s later="
                f"{len(during_b)} rows): with the backend unreachable the daemon must "
                "retain the last-good rows (stale but consistent, FS §6.1.2), not drop "
                "or keep updating them."
            )
            health, raw = ibh.read_peer_telemetry_health(engines)
            assert health == "degraded", (
                f"PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}) during the outage; expected degraded"
            )
            log = ibh.read_gpu_telemetry_log(engines)
            lowered = log.lower()
            assert "unreachable" in lowered or "nmx" in lowered, (
                "peer-telemetry log shows no NMX-T-for-IB unreachable error during the outage"
            )
            # Recovery action: auto-restart or retry/backoff wording.
            assert any(tok in lowered for tok in ("retry", "backoff", "restart")), (
                "peer-telemetry log shows no retry/backoff/restart recovery attempt during the outage"
            )
    finally:
        with allure.step("Restore NMX-T Lite"):
            ibh.restore_nmxt_lite(engines)

    with allure.step("After recovery: ingestion resumes and health is healthy again"):
        time.sleep(PEER_TELEMETRY_SAMPLING_SEC)
        health, raw = ibh.read_peer_telemetry_health(engines)
        assert health == "healthy", (
            f"PEER_PORT_TELEMETRY_HEALTH did not return to healthy after recovery: {health!r} (raw={raw!r})"
        )


# 10.7 test_hca_xcset_malformed_csv_resiliency (plan §10.6)
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_hca_xcset_malformed_csv_resiliency(engines, devices, setup_topology):
    """On a malformed xcset: rows frozen (no partial writes), parse error logged, health -> degraded; recovers on a good feed."""
    _require_hca_topology(setup_topology)
    _enable_peer_port_for_hca(engines)

    with allure.step("Ensure a valid HCA xcset backend (clear leftover resiliency state)"):
        restored = ibh.ensure_valid_hca_xcset_backend(engines)
        ibh.attach_dict("xcset backend cleanup", {"restored": restored})

    with allure.step("Snapshot HCA rows and confirm health is healthy"):
        snapshot = ibh.list_hca_peer_rows(engines)
        assert snapshot, "No HCA peer-port rows in System DB (HCA feature build delivered?)"
        health, raw = ibh.wait_for_peer_telemetry_healthy(engines)
        ibh.attach_dict("initial health", {"classification": health, "raw": raw})
        assert raw, (
            "PEER_PORT_TELEMETRY_HEALTH is not present in STATE_DB before injection; "
            "gpu-telemetry should publish an initial health state (FS §6.1.2)."
        )
        assert health == "healthy", (
            f"PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}) before injection; expected healthy"
        )
        parse_fail_baseline = _count_xcset_parse_failures(engines)

    with allure.step("Serve a malformed HCA xcset from NMX-T-for-IB"):
        ibh.inject_malformed_hca_xcset(engines)

    try:
        with allure.step("Wait for the malformed feed to take effect (a new CSV parse failure is logged)"):
            assert _wait_for_xcset_parse_failure(engines, parse_fail_baseline), (
                "No CSV parse-failure logged after serving a malformed xcset; the interceptor "
                "may not have taken over the telemetry socket (injection ineffective)"
            )

        with allure.step("During the malformed feed: rows are frozen (stale but consistent), none dropped"):
            # Baseline the freeze after the feed is unparseable; compare within the window.
            frozen_a = ibh.list_hca_peer_rows(engines)
            ibh.attach_dict("rows at start of malformed feed", frozen_a)
            assert set(frozen_a) == set(snapshot), (
                "HCA rows were added/dropped during the malformed feed; FS §6.1.2 requires the "
                f"last-good rows to be retained. before={sorted(snapshot)!r}, during={sorted(frozen_a)!r}"
            )
            time.sleep(2 * PEER_TELEMETRY_SAMPLING_SEC)
            frozen_b = ibh.list_hca_peer_rows(engines)
            ibh.attach_dict("rows later in malformed feed", frozen_b)
            assert frozen_b == frozen_a, (
                "HCA rows changed across the malformed-feed window; on a parse failure the daemon "
                "must not write the batch (stale but consistent / no partial writes, FS §6.1.2). "
                "Expected the journal's 'retaining last ... snapshot' behaviour to hold the rows steady."
            )

        with allure.step("Parse failure is logged"):
            log = ibh.read_gpu_telemetry_log(engines)
            assert any(tok in log.lower() for tok in ("malformed", "parse", "invalid")), (
                "peer-telemetry log shows no parse-failure error for the malformed xcset"
            )

        with allure.step("Health reflects the parse failure (FS §6.1.2: update health state -> degraded)"):
            health, raw = ibh.read_peer_telemetry_health(engines)
            assert health == "degraded", (
                f"PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}) during the malformed "
                "feed; FS §6.1.2 requires the daemon to update health state to degraded."
            )
    finally:
        with allure.step("Restore a valid HCA xcset"):
            ibh.restore_valid_hca_xcset(engines)

    with allure.step("After recovery: ingestion resumes and health is healthy again"):
        time.sleep(PEER_TELEMETRY_SAMPLING_SEC)
        during = ibh.list_hca_peer_rows(engines)
        assert during, "HCA rows did not resume after a valid xcset was restored"
        health, raw = ibh.read_peer_telemetry_health(engines)
        assert health == "healthy", (
            f"PEER_PORT_TELEMETRY_HEALTH did not return to healthy after recovery: {health!r} (raw={raw!r})"
        )


# 10.8 test_hca_ingestion_recovers_after_gpu_telemetry_restart (nice-to-have)
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(12 * MINUTE, func_only=True)
def test_hca_ingestion_recovers_after_gpu_telemetry_restart(engines, devices, setup_topology):
    """A gpu-telemetry restart loses no HCA ingestion: rows repopulate, counters do not reset, health -> healthy."""
    _require_hca_topology(setup_topology)
    _enable_peer_port_for_hca(engines)

    with allure.step("Baseline HCA rows before the restart"):
        before = ibh.list_hca_peer_rows(engines)
        ibh.attach_dict("hca rows before restart", before)
        assert before, "No HCA peer-port rows before restart (HCA feature build delivered?)"

    with allure.step("Restart gpu-telemetry"):
        ibh.restart_gpu_telemetry(engines)

    with allure.step("HCA rows repopulate after the daemon comes back"):
        after: Dict[str, Dict[str, str]] = {}
        for _ in range(6):
            after = ibh.list_hca_peer_rows(engines)
            if set(after) >= set(before):
                break
            time.sleep(PEER_TELEMETRY_SAMPLING_SEC)
        ibh.attach_dict("hca rows after restart", after)
        missing = set(before) - set(after)
        assert not missing, (
            f"HCA rows did not repopulate after gpu-telemetry restart; missing: {sorted(missing)!r}"
        )

    with allure.step("Let the repopulated counters settle before the monotonic check"):
        # Poll until every row's counters are back at/above baseline; a real reset never recovers.
        deadline = time.time() + PEER_PORT_ENABLE_GRACE_SEC + PEER_TELEMETRY_SAMPLING_SEC * 4
        while time.time() < deadline:
            if all(
                ibh.counters_nondecreasing(before[pid], after.get(pid, {}), PEER_PORT_DB_ADDITIVE_FIELDS)
                for pid in before if pid in after
            ):
                break
            time.sleep(min(PEER_TELEMETRY_SAMPLING_SEC, 5))
            after = ibh.list_hca_peer_rows(engines)
        ibh.attach_dict("hca rows after restart (settled)", after)

    with allure.step("Counters did not reset across the restart (System DB accumulators persist)"):
        for pid in before:
            if pid not in after:
                continue
            ibh.assert_counters_monotonic(
                before[pid], after[pid], PEER_PORT_DB_ADDITIVE_FIELDS,
                label=f"HCA {pid} across gpu-telemetry restart",
            )

    with allure.step("PEER_PORT_TELEMETRY_HEALTH is healthy again after recovery"):
        health, raw = ibh.read_peer_telemetry_health(engines)
        assert health == "healthy", (
            f"PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}) after gpu-telemetry restart; expected healthy"
        )
