"""HCA peer-port ingestion tests: listing, ingestion, mapping, aggregation, resiliency."""

import logging
import time
from typing import Dict, List, Tuple

import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.ResultObj import IssueType, ResultObj
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.system.gnmi.constants import (
    HEARTBEAT_INTERVAL,
    HEARTBEAT_INTERVAL_SHORT,
    HEARTBEAT_PERTURBATION_AT_SEC,
    HEARTBEAT_SAMPLE_INTERVAL,
    HEARTBEAT_STATIC_INTERFACE_LEAVES,
    GnmiStreamMode,
)
from ngts.tests_nvos.system.gnmi.helpers import (
    StreamSubscription,
    get_gnmic_engine,
    heartbeat_window_sec,
    run_stream_subscribers,
    verify_heartbeat_drove_delivery,
    verify_leaf_heartbeat_continuity,
    verify_subscribers_streamed,
)
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_general_utils import loganalyzer_ignore

from ngts.tests_nvos.general.telemetry.ib import helpers as ibh
from ngts.tests_nvos.general.telemetry.ib import otel_helpers as ibo
from ngts.tests_nvos.general.telemetry.ib.constants import (
    ALL_APIS,
    API_GNMIC,
    API_NVUE_CLI,
    API_OTEL,
    BER_MONITOR_LEAVES,
    GNMI_PEER_LIST_POLL_SEC,
    GnmiTypeKind,
    GnmiYangPaths,
    LOCAL_PORT_FAMILIES,
    LocalPortFamilyId,
    NMXT_IB_SERVICE,
    NvuePaths,
    OTEL_LINK_HEALTH_STATES,
    OtelGroup,
    OtelLabel,
    PEER_ASIC_HOST_NAME_CANDIDATES,
    PEER_PLATFORM_OTEL_EXTRA_GROUPS,
    PEER_PLATFORM_OTEL_GROUPS,
    PEER_PLATFORM_SHOW_NODES,
    PEER_PORT_ADDITIVE_FIELDS,
    PEER_PORT_API_TO_DB_FIELD,
    PEER_PORT_DB_ADDITIVE_FIELDS,
    PEER_PORT_GNMI_ADDITIVE_FIELDS,
    PEER_PORT_GNMI_STATIC_LEAVES,
    PEER_PORT_LIST_SCHEMA_GROUPS,
    PEER_PORT_NVUE_ROOT_EXTENDED_LEAVES,
    PEER_PORT_NVUE_SHOW_NODES,
    PEER_PORT_NVUE_TO_GNMI_ROOT_LEAF,
    PEER_PORT_OTEL_ADDITIVE_LEAVES,
    PEER_PORT_OTEL_BER_MONITOR_LEAVES,
    PEER_PORT_OTEL_IDENTITY_DB_FIELD,
    PEER_PORT_OTEL_IDENTITY_LEAVES,
    PEER_PORT_OTEL_LINK_HEALTH,
    PEER_PORT_OTEL_LIST_GROUPS,
    PEER_PORT_OTEL_METRIC_BY_DB_FIELD,
    PEER_PORT_OTEL_NODE_INFO,
    PEER_PORT_OTEL_SCHEMA_GROUPS,
    PEER_PORT_OTEL_UNEXPORTED_ROOT_LEAVES,
    PEER_PORT_OTEL_UNEXPORTED_SCHEMA_LEAVES,
    PEER_PORT_ROOT_EXTENDED_LEAVES,
    PEER_PORT_ROOT_TIER_UNSPECIFIED_LEAVES,
    PEER_PORT_SCHEMA_GROUPS,
    PEER_TELEMETRY_SAMPLING_SEC,
    PeerComponentXcsetColumn,
    PeerPlatformKind,
    PeerPlatformSub,
    PeerPortComponentRef,
    PeerPortFields,
    PeerPortNvueSub,
    PeerTelemetryHealth,
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


def _wait_for_gnmi_peer_ports(gnmi_client) -> Dict[str, Dict[str, str]]:
    """The gNMI peer-port list, asked again until it is non-empty or the settle time is over.

    NVUE lists the rows before gNMI serves them, so the first read after the knob is enabled can
    be empty. The settle time is the one the DB gets.
    """
    deadline = time.time() + PEER_PORT_ENABLE_GRACE_SEC + (
        _peer_port_interval_sec() * PEER_PORT_SETTLE_INTERVALS)
    entries = ibh.gnmi_get_peer_port_list(gnmi_client)
    while not entries and time.time() < deadline:
        logger.info("gNMI lists no peer-port yet; checking again in %ds", GNMI_PEER_LIST_POLL_SEC)
        time.sleep(GNMI_PEER_LIST_POLL_SEC)
        entries = ibh.gnmi_get_peer_port_list(gnmi_client)
    return entries


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


def _recycle_peer_port_for_hca(engines, **list_kwargs) -> Dict[str, Dict[str, str]]:
    """Enable peer-port from a disabled state (disabling first if needed) and return the HCA rows.

    The daemon latches its 'backend unrecoverable' declaration for the lifetime of its
    monitor thread: once declared, neither the backend coming back nor a later outage
    re-arms it. Only the enable path, which starts the monitor thread, clears the latch.
    Tests that assert on the declaration must therefore recycle the knob instead of
    inheriting an enabled feature from whatever ran before them.
    """
    system = System()
    state = str(system.peer_port.parse_show().get(NvuePaths.KEY_STATE, "")).lower()
    if state == NvuePaths.STATE_ENABLED:
        with allure.step("Disable peer-port so the enable below restarts the daemon's monitor thread"):
            system.peer_port.unset(
                op_param=NvuePaths.KEY_STATE, apply=True, ask_for_confirmation='-y',
            ).verify_result()
            _wait_for_peer_ports(engines, present=False)
    return _enable_peer_port_for_hca(engines, **list_kwargs)


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


# NMX-T-for-IB outage: the daemon retries the backend a few times, then declares it
# unrecoverable and keeps serving the last-good snapshot, flagged stale. Key off the log
# wording rather than the attempt count so the check survives the count changing.
NMXT_UNRECOVERABLE_TOKEN = "nmx-t backend unrecoverable after"
NMXT_UNRECOVERABLE_DEADLINE_SEC = 3 * MINUTE


def _nmxt_unrecoverable_logged(engines, since: str) -> bool:
    """True once peer-telemetry declares the backend unrecoverable in the journal after ``since``."""
    log = (ibh.read_gpu_telemetry_log(engines, since=since) or "").lower()
    return NMXT_UNRECOVERABLE_TOKEN in log


def _wait_for_nmxt_unrecoverable(engines, since: str,
                                 deadline_sec: int = NMXT_UNRECOVERABLE_DEADLINE_SEC) -> bool:
    """Poll until a 'backend unrecoverable' declaration is logged after ``since``.

    Scoping the search to the outage window keeps a stale line from an earlier outage - the
    journal spans prior runs - from satisfying the wait too early, without the tail-scrolling
    fragility of counting occurrences in a fixed-size tail.
    """
    interval = _peer_port_interval_sec()
    deadline = time.time() + deadline_sec
    while time.time() < deadline:
        if _nmxt_unrecoverable_logged(engines, since):
            return True
        time.sleep(min(interval, 5))
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


def _require_listed_hca_peers(engines) -> Dict[str, Dict[str, str]]:
    """The HCA peers `nv show peer-port` lists once the feature is enabled, failing on none.

    For cases that inspect what the switch reports about an HCA peer, not whether every cabled
    HCA is discovered: discovery is checked against the connectivity file by
    test_peer_port_lists_hca, so choosing the peer from the listing does not weaken these.
    """
    peers = ibh.filter_peer_entries_by_type(ibh.nvue_show_peer_ports(engines), PeerType.HCA)
    ibh.attach_dict("nv show peer-port HCA peers", peers)
    ResultObj(
        bool(peers),
        "Peer-port is enabled but `nv show peer-port` lists no HCA peer, so there is no "
        "peer to read the extended leaves from",
        issue_type=IssueType.PossibleBug,
    ).verify_result()
    return peers


def _require_external_hca_topology(setup_topology, skip_on_wrong_lab: bool = False) -> List:
    """
    Return connectivity-derived *external* HCA peers (server cables), or report the lab.

    ``skip_on_wrong_lab`` is set only by the gNMI subscribe-heartbeat cases, which are extra
    coverage rather than gating - see ``ibh.report_wrong_lab``.
    """
    hca = setup_topology.external_hca_peers()
    if not hca:
        ibh.report_wrong_lab(
            "WRONG LAB SETUP / TOPOLOGY: the connectivity JSON for "
            f"{setup_topology.setup_name!r} lists no external HCA cables "
            "(connected_to an ib*HCA* port), so the HCA peer-port listing "
            "cannot be exercised here. Run on a setup with at least one "
            "server HCA cabled to the fabric.",
            skip_on_wrong_lab,
        )
    return hca


def _require_active_fabric(engines, request, setup_name, skip_on_wrong_lab: bool = False):
    """
    Bring the fabric Active by starting the Subnet Manager, or report the lab.

    An IB port stays in Initialize until an SM configures it, and NVOS only discovers peer HCAs
    over an Active fabric - so without this the peer-port surface is simply empty, which reads as
    a missing feature rather than a missing SM.

    ``skip_on_wrong_lab`` is set only by the gNMI subscribe-heartbeat cases, which are extra
    coverage rather than gating - see ``ibh.report_wrong_lab``.
    """
    if not hasattr(engines, "hfnm"):
        ibh.report_wrong_lab(
            f"WRONG LAB SETUP: setup {setup_name!r} has no Host Fabric Node "
            "Manager (HFNM), so the Subnet Manager cannot be started to bring the "
            "fabric Active - this test cannot be exercised here. Run on an "
            "HFNM-capable setup with traffic hosts.",
            skip_on_wrong_lab,
        )
    try:
        request.getfixturevalue("start_sm")
    except Exception as exc:  # noqa: BLE001
        ibh.report_wrong_lab(
            "WRONG LAB SETUP: Subnet Manager could not be started on setup "
            f"{setup_name!r} ({exc}); an active fabric is required here.",
            skip_on_wrong_lab,
        )


def _require_active_fabric_and_traffic(engines, request, setup_name):
    """FAIL (not skip) when HFNM/SM or the traffic harness are missing."""
    _require_active_fabric(engines, request, setup_name)
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


def _select_peer_port_targets(gnmi_client, skip_on_wrong_lab: bool = False) -> List[Tuple[str, str]]:
    """
    Pick up to two live HCA peer-ports to sweep (fallback: first listed), or report the lab.

    ``skip_on_wrong_lab`` is set only by the gNMI subscribe-heartbeat cases, which are extra
    coverage rather than gating - see ``ibh.report_wrong_lab``.
    """
    entries = _wait_for_gnmi_peer_ports(gnmi_client)
    if not entries:
        ibh.report_wrong_lab(
            "No peer-ports exposed via gNMI under "
            f"{GnmiYangPaths.PEER_PORT_INTERFACES}. Enable the peer-port knob and "
            "confirm gpu-telemetry is running / the feature build is delivered.",
            skip_on_wrong_lab,
        )
    hca = ibh.filter_peer_entries_by_type(entries, PeerType.HCA)
    targets = [("hca-peer", pid) for pid in sorted(hca)[:2]]
    if not targets:
        targets = [("peer", pid) for pid in sorted(entries)[:2]]
    return targets


def _peer_port_schema_targets(gnmi_client, group) -> List[Tuple[str, str]]:
    """What a schema group's `{name}` is filled with: peer-port ids, or the components they name."""
    if not group.component_ref:
        return _select_peer_port_targets(gnmi_client)
    targets = ibh.resolve_component_targets(gnmi_client, group.component_ref)
    ResultObj(
        bool(targets),
        f"No gNMI-listed peer-port names a {group.component_ref!r} component, so the "
        f"{group.group_id!r} container has no component to sweep (SR-4)",
        issue_type=IssueType.PossibleBug,
    ).verify_result()
    return targets


# NVUE-surface peer-port tests (live `nv show peer-port`).
_OTEL_IDENTITY_GROUP = OtelGroup(OtelLabel.INTERFACE, PEER_PORT_OTEL_IDENTITY_LEAVES)


def _otel_peer_rows(label: str) -> Dict[str, Dict[str, str]]:
    """``{peer_id: {leaf: value}}`` of one fresh OTEL window, the shape of the NVUE listing."""
    rows = ibh.otel_peer_port_rows(ibo.active_surface().snapshot(label, fresh=True))
    ibh.attach_dict(f"peer-ports via {API_OTEL} ({label})", rows)
    return rows


def _otel_hca_rows(label: str) -> Dict[str, Dict[str, str]]:
    return ibh.filter_peer_entries_by_type(_otel_peer_rows(label), PeerType.HCA)


def _verify_otel_peer_identity(peers: Dict[str, Dict[str, str]],
                               detailed: Dict[str, Dict[str, str]]) -> None:
    """OTEL exports exactly the NVUE-listed peers, each with the identity NVUE shows (SR-1)."""
    snap = ibo.active_surface().snapshot("peer-identity", fresh=True)
    rows = ibh.otel_peer_port_rows(snap)
    ibh.attach_dict(f"peer-ports via {API_OTEL}", rows)
    problems = [f"[{pid}] is listed on NVUE but exports no {PEER_PORT_OTEL_NODE_INFO}"
                for pid in sorted(set(peers) - set(rows))]
    problems += [f"[{pid}] is exported over OTEL but not listed on NVUE"
                 for pid in sorted(set(rows) - set(peers))]
    for pid in sorted(set(peers) & set(rows)):
        shown = ibh.remap_leaf_keys({**peers[pid], **detailed.get(pid, {})},
                                    PEER_PORT_NVUE_TO_GNMI_ROOT_LEAF)
        leaves = [leaf for leaf in PEER_PORT_OTEL_IDENTITY_LEAVES
                  if leaf not in PEER_PORT_OTEL_UNEXPORTED_ROOT_LEAVES and
                  not ibh.component_source_absent(shown.get(leaf))]
        problems += ibo.cross_surface_problems(snap, _OTEL_IDENTITY_GROUP, pid, shown,
                                               API_NVUE_CLI, leaves)
    ResultObj(
        not problems,
        f"The OTEL peer-port export disagrees with the NVUE listing in {len(problems)} "
        f"place(s); both publish the same neighbors with the same identity (SR-1):\n  " +
        "\n  ".join(problems),
        issue_type=IssueType.PossibleBug,
    ).verify_result()


def _live_nvue_peers(engines) -> Dict[str, Dict[str, str]]:
    """Enable the knob and return the live NVUE peer listing; FAIL loud if empty."""
    _enable_peer_port(engines)
    peers = ibh.nvue_show_peer_ports(engines)
    assert peers, (
        "No peer-ports listed via NVUE (`nv show peer-port`). Enable the knob and "
        "confirm peers are attached and their links are Active (an SM must be running)."
    )
    return peers


def _verify_extended_peer_identity(engines, peers: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    """Verify port-number and the component names a peer-port points at, and no tray-id.

    The HLD does not say which tier carries these, so a single row may lack one; the listing as
    a whole must still carry each (see `extended_identity_tier_problems`). A GPU row has no
    cable or chip of its own, so only HCA rows are examined, and tray-id is published for a GPU
    peer-port only (SR-6). Each row is read with `nv show peer-port <peer>`, the command HLD
    section 5.2 renders these leaves on; the bare listing is a summary that does not carry them.
    Returns those per-peer shows.
    """
    candidates = {pid: ibh.nvue_show_peer_port(engines, pid)
                  for pid in ibh.filter_peer_entries_by_type(peers, PeerType.HCA)}
    component_names: Dict[str, str] = {}

    with allure.step(f"Extended identity on {len(candidates)} HCA row(s)"):
        problems = ibh.extended_identity_tier_problems(candidates)
        for pid, fields in sorted(candidates.items()):
            problems += ibh.leaf_problems(
                fields, PEER_PORT_NVUE_ROOT_EXTENDED_LEAVES,
                tolerated_missing=PEER_PORT_ROOT_TIER_UNSPECIFIED_LEAVES, where=pid,
            )
            for leaf, name in ibh.peer_component_refs(fields, nvue=True).items():
                component_names.setdefault(name, leaf)
        ibh.attach_dict("named peer components", component_names)
        ResultObj(
            not problems,
            f"Extended peer identity has {len(problems)} problem(s). Every HCA neighbor must "
            f"report its port number as a whole number, name the cable and the chip it belongs "
            f"to (`transceiver`, `asic`) and omit tray-id (HLD Section 5.2, SR-4, SR-6):\n  " +
            "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step(f"Resolve {len(component_names)} named component(s) via nv show peer-platform"):
        unresolved = []
        for name, leaf in sorted(component_names.items()):
            kind = PeerPlatformKind.BY_COMPONENT_REF[leaf]
            if not ibh.nvue_peer_platform_show(engines, kind, name):
                unresolved.append(f"`nv show peer-platform {kind} {name}` resolved to nothing")
        ResultObj(
            not unresolved,
            f"{len(unresolved)} component name(s) published on a peer-port do not resolve "
            f"under nv show peer-platform (SR-4):\n  " +
            "\n  ".join(unresolved),
            issue_type=IssueType.PossibleBug,
        ).verify_result()
    return candidates


@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.nvos_build
def test_peer_port_nvue_listing_and_identity(engines, devices, setup_topology, ib_otel, random_api):
    """Every NVUE-listed peer carries an identity, an associated switch port, and classifies as HCA.

    OTEL exports the same peers with the same identity.
    """
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

    detailed = _verify_extended_peer_identity(engines, peers)

    with allure.step("OTEL exports the same peers with the same identity"):
        _verify_otel_peer_identity(peers, detailed)


@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_peer_port_nvue_show_surface(engines, devices, setup_topology, ib_otel, random_api):
    """Every `nv show peer-port <id>` sub-command exposes its documented containers and typed leaves.

    Each node is read with its own sub-command rather than by flattening a parent payload, so a
    sub-command that does not resolve is a failure instead of an incidentally-covered leaf. The
    OTEL export of each peer carries the same leaves, lanes and histogram buckets.
    """
    _enable_peer_port(engines)
    targets = ibh.select_nvue_peer_port_targets(engines)
    bodies: Dict[Tuple[str, str], Dict] = {}

    with allure.step(f"Walk the peer-port show surface on {len(targets)} peer(s)"):
        for role, pid in targets:
            for node in PEER_PORT_NVUE_SHOW_NODES:
                cmd = f"nv show peer-port {pid} {node.sub}".strip()
                with allure.independent_step(f"Verify {role} `{cmd}`"):
                    body = ibh.nvue_peer_port_show(engines, pid, node.sub)
                    bodies[(pid, node.sub)] = body
                    ibh.attach_dict(cmd, body)
                    ResultObj(
                        bool(body),
                        f"`{cmd}` returned no data",
                        issue_type=IssueType.PossibleBug,
                    ).verify_result()
                    problems = ibh.peer_port_show_problems(body, node, role=role)
                    ResultObj(
                        not problems,
                        f"`{cmd}` has {len(problems)} contract problem(s):\n  " +
                        "\n  ".join(problems),
                        issue_type=IssueType.PossibleBug,
                    ).verify_result()

    snap = ibo.active_surface().snapshot("peer-show-surface", fresh=True)
    with allure.step(f"The OTEL export carries the same surface on {len(targets)} peer(s)"):
        for role, pid in targets:
            for node in PEER_PORT_NVUE_SHOW_NODES:
                cmd = f"nv show peer-port {pid} {node.sub}".strip()
                with allure.independent_step(f"Verify {role} `{cmd}` over OTEL"):
                    problems = ibh.peer_port_otel_show_problems(
                        snap, pid, node, bodies.get((pid, node.sub), {}), role=role)
                    ResultObj(
                        not problems,
                        f"The OTEL export of {pid} lacks {len(problems)} part(s) of `{cmd}`:\n  " +
                        "\n  ".join(problems),
                        issue_type=IssueType.PossibleBug,
                    ).verify_result()


@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_peer_port_nvue_matches_system_db(engines, devices, setup_topology, ib_otel):
    """Every NVUE and OTEL peer-port leaf backed by COUNTERS_DB agrees with the ingested row."""
    _enable_peer_port(engines)
    targets = ibh.select_nvue_peer_port_targets(engines)

    for role, pid in targets:
        with allure.step(f"Compare {role} {pid} NVUE leaves against its COUNTERS_DB row"):
            db_row = ibh.read_peer_port_row(engines, pid)
            ibh.attach_dict(f"COUNTERS_DB row {pid}", db_row)
            ResultObj(
                bool(db_row),
                f"No COUNTERS_DB PEER_COUNTERS row for peer-port {pid!r}, so nothing to compare",
                issue_type=IssueType.PossibleBug,
            ).verify_result()

            pairs = ibh.peer_port_db_comparison_pairs(engines, pid)
            ibh.attach_dict(f"{pid} NVUE-to-DB pairs",
                            {pair.label: [pair.cli_value, pair.db_field] for pair in pairs})
            problems, compared = ibh.peer_port_db_pair_problems(pairs, db_row)

            with allure.independent_step(f"Verify {compared} DB-backed leaf value(s) of {pid}"):
                ResultObj(
                    not problems,
                    f"{pid} disagrees with its COUNTERS_DB row in {len(problems)} place(s):\n  " +
                    "\n  ".join(problems),
                    issue_type=IssueType.PossibleBug,
                ).verify_result()
            with allure.independent_step(f"Verify {pid} exposes DB-backed leaves at all"):
                ResultObj(
                    compared > 0,
                    f"{pid} exposed no DB-backed leaf out of {len(pairs)} mapped one(s)",
                    issue_type=IssueType.PossibleBug,
                ).verify_result()
            with allure.independent_step(f"Verify {pid} link speed and width match the DB row"):
                link_problems = ibh.peer_port_link_db_problems(
                    ibh.nvue_peer_port_show(engines, pid, PeerPortNvueSub.LINK), db_row)
                ResultObj(
                    not link_problems,
                    f"{pid} link attributes disagree with the DB row:\n  " +
                    "\n  ".join(link_problems),
                    issue_type=IssueType.PossibleBug,
                ).verify_result()
            with allure.independent_step(f"Verify {pid} derives plr-bw-loss-percent from plr-codes-loss"):
                plr_problems = ibh.peer_port_plr_bw_loss_problems(
                    ibh.nvue_peer_port_show(engines, pid, PeerPortNvueSub.LINK_PHY_DETAIL))
                ResultObj(
                    not plr_problems,
                    f"{pid} PLR bandwidth-loss derivation problems:\n  " +
                    "\n  ".join(plr_problems),
                    issue_type=IssueType.PossibleBug,
                ).verify_result()
            with allure.independent_step(f"Verify {pid} over OTEL agrees with its DB rows"):
                label = f"peer-db-{pid}"
                otel_problems, otel_compared = ibh.otel_peer_port_db_problems(engines, pid, label)
                otel_problems += ibh.otel_peer_port_plr_bw_loss_problems(
                    ibo.active_surface().snapshot(label), pid)
                if not otel_compared:
                    otel_problems.append(f"{pid}: its DB row carries no field with an OTEL metric")
                ResultObj(
                    not otel_problems,
                    f"{pid} over OTEL disagrees with its COUNTERS_DB rows in "
                    f"{len(otel_problems)} place(s):\n  " + "\n  ".join(otel_problems),
                    issue_type=IssueType.PossibleBug,
                ).verify_result()


def _plane_mapping_problems(peers: Dict[str, Dict[str, str]]) -> List[str]:
    """Aggregated peers without plane rows, or plane rows outside the parent's switch-port."""
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
    return problems


@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_peer_port_nvue_plane_mapping(engines, devices, setup_topology, ib_otel):
    """Each aggregated HCA peer fans out to per-plane rows with switch-ports under the parent's.

    Plane rows are siblings of the aggregated row rather than a nested subtree (by design), so
    the fan-out is verified across the listing, on NVUE and on the OTEL switch_info.
    """
    peers = _live_nvue_peers(engines)
    problems = _plane_mapping_problems(peers)
    assert not problems, (
        "NVUE peer-port plane-mapping problems:\n  " + "\n  ".join(problems)
    )

    with allure.step("OTEL exports the same fan-out"):
        otel_peers = _otel_peer_rows("peer-plane-mapping")
        otel_problems = _plane_mapping_problems(otel_peers)
        if set(otel_peers) != set(peers):
            otel_problems.append(
                f"OTEL exports peers {sorted(otel_peers)!r} but NVUE lists {sorted(peers)!r}")
        assert not otel_problems, (
            "OTEL peer-port plane-mapping problems:\n  " + "\n  ".join(otel_problems)
        )


@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_peer_port_nvue_knob_and_interval_lifecycle(engines, devices, setup_topology, ib_otel):
    """The peer-port knob round-trips: enable lists peers, interval is settable, disable drains NVUE and OTEL."""
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

    with allure.step("Disabling the knob stops the OTEL peer-port export"):
        exported = _otel_peer_rows("peer-port-disabled")
        assert not exported, (
            f"peer-ports still exported over OTEL after disabling the knob: {sorted(exported)!r}"
        )


# 5.6 test_peer_port_lists_hca (plan §5.5)
@pytest.mark.gnmi
@pytest.mark.otel
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

    with allure.step("GPU and HCA node info stay separate; neither row is dropped or padded"):
        gpu_live = ibh.filter_peer_entries_by_type(entries, PeerType.GPU)
        if not gpu_live:
            logger.info(
                "No GPU rows via %s, so the GPU half of SR-6 is vacuous here; the HCA half "
                "still runs against %d HCA row(s)", api, len(hca_live),
            )
        mixed = ibh.foreign_node_info_problems(entries)
        ResultObj(
            not mixed,
            f"Peer rows via {api} mix GPU and HCA node info in {len(mixed)} place(s). Each "
            f"entry must be listed in full with only its own type's node info, and must not be "
            f"padded with the other type's fields (SR-6):\n  " + "\n  ".join(mixed),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

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
    # OTEL values are sampled across a collection window, so they are bracketed by DB reads.
    db_before = ibh.read_peer_port_row(engines, one_pid) if api == API_OTEL else {}
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
        if api == API_OTEL:
            fields = [f for f in dict.fromkeys(PEER_PORT_API_TO_DB_FIELD[leaf]
                                               for leaf in PEER_PORT_ADDITIVE_FIELDS)
                      if f in db_before and f in db_row]
            problems = ibo.db_parity_problems(
                ibo.active_surface().snapshot(f"peer-port-{one_pid}"), one_pid, db_before, db_row,
                PEER_PORT_OTEL_METRIC_BY_DB_FIELD, fields)
            assert not problems, (
                f"{api} counters of {one_pid} are not values its DB row held:\n  " +
                "\n  ".join(problems)
            )
            compared = len(fields)
        else:
            # Pair API leaf and DB SAI field through the API->DB field map.
            compared = ibh.assert_counters_within_tolerance(
                detail, db_row, PEER_PORT_API_TO_DB_FIELD, SAMPLING_JITTER_TOLERANCE_PCT,
                label=f"{api} vs DB for {one_pid}",
            )
        assert compared > 0, (
            f"Could not compare any counter for {one_pid} between {api} and DB "
            f"(api keys={sorted(detail)!r}, db keys={sorted(db_row)!r})"
        )


def _verify_extended_leaf_parity(engines, gnmi_client, nvue_entries, gnmi_entries) -> None:
    """Compare the extended peer leaves across NVUE and gNMI, and against a local switch port.

    SR-1 is the cross-surface half: the same neighbor's new component-reference and
    error-rate-monitor leaves must read the same on both interfaces.
    SR-2 is the local half: a peer-port and the switch's own port read the same YANG
    containers, so the leaf names must coincide and neither side may rescale the error rates.
    Both halves are driven off the same leaf tables as the peer-port schema sweep, so a leaf
    added there is compared here without touching this test.
    """
    rows_nvue = ibh.most_specific_peer_entries(
        ibh.filter_peer_entries_by_type(nvue_entries, PeerType.HCA))
    rows_gnmi = ibh.most_specific_peer_entries(
        ibh.filter_peer_entries_by_type(gnmi_entries, PeerType.HCA))
    shared_ids = sorted(set(rows_nvue) & set(rows_gnmi))
    ResultObj(
        bool(shared_ids),
        f"No HCA row is listed on both surfaces, so the extended leaves cannot be compared "
        f"(NVUE={sorted(rows_nvue)!r}, gNMI={sorted(rows_gnmi)!r})",
        issue_type=IssueType.PossibleBug,
    ).verify_result()
    pid = shared_ids[0]

    with allure.step(f"Extended leaves of {pid} agree across NVUE and gNMI"):
        # The listing is a summary; HLD section 5.2 renders these leaves on the per-peer show,
        # where the chip reference keeps its CLI name `asic`.
        problems, compared = ibh.leaf_parity_problems(
            PEER_PORT_ROOT_EXTENDED_LEAVES,
            ibh.remap_leaf_keys(ibh.nvue_show_peer_port(engines, pid),
                                PEER_PORT_NVUE_TO_GNMI_ROOT_LEAF),
            rows_gnmi[pid], API_NVUE_CLI, API_GNMIC,
        )
        nvue_ber = ibh.leaf_lookup(ibh.flatten_nvue_leaves(
            ibh.nvue_peer_port_show(engines, pid, PeerPortNvueSub.LINK_PHY_BER_MONITOR)))
        gnmi_ber = ibh.gnmi_get_flat_relative(
            gnmi_client, prefix=GnmiYangPaths.PEER_PORT_PHY_BER_MONITOR_STATE.format(name=pid))
        ibh.attach_dict(f"{pid} ber-monitor across surfaces",
                        {API_NVUE_CLI: nvue_ber, API_GNMIC: gnmi_ber})
        ber_problems, ber_compared = ibh.leaf_parity_problems(
            BER_MONITOR_LEAVES, nvue_ber, gnmi_ber, API_NVUE_CLI, API_GNMIC)
        problems += ber_problems
        compared += ber_compared
        logger.info("Compared %d extended leaf value(s) of %s across both surfaces", compared, pid)
        ResultObj(
            not problems,
            f"{pid} reports {len(problems)} extended leaf value(s) differently on NVUE and "
            f"gNMI; only the encoding may differ (SR-1):\n  " + "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step("No HCA peer-port state publishes tray-id over gNMI (SR-6)"):
        problems = ibh.hca_omitted_leaf_problems(rows_gnmi)
        ResultObj(
            not problems,
            f"{len(problems)} HCA peer-port(s) publish a GPU-only leaf over gNMI (HLD Section "
            f"5.2, SR-6):\n  " + "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    local_port = ibh.require_link_up_port("compare the peer-port PHY against", skip=False)

    # The routing / pFRN counters are a local-only family, so SR-2 covers the PHY and
    # error-rate containers the two trees genuinely share.
    with allure.step(f"Local-versus-peer PHY symmetry on {pid} and {local_port.name} (SR-2)"):
        for family in [f for f in LOCAL_PORT_FAMILIES
                       if f.family_id != LocalPortFamilyId.ROUTING]:
            with allure.independent_step(
                    f"Verify {family.family_id} leaf names match on {pid} and {local_port.name}"):
                local_leaves = ibh.gnmi_family_leaves(gnmi_client, local_port.name, family)
                peer_leaves = ibh.gnmi_family_leaves(
                    gnmi_client, pid, family, path_prefix=GnmiYangPaths.PEER_PORT_PREFIX)
                ibh.attach_dict(f"{family.family_id} peer vs local",
                                {"peer-port": peer_leaves, "local port": local_leaves})
                problems, shared = ibh.phy_surface_symmetry_problems(
                    family.leaves, peer_leaves, local_leaves)
                logger.info("%s: %d leaf name(s) shared by the peer-port and the local port",
                            family.family_id, shared)
                ResultObj(
                    not problems,
                    f"The {family.family_id} container differs between peer-port {pid} and local "
                    f"port {local_port.name} in {len(problems)} place(s); both read the same YANG "
                    f"container, so names and units must match (SR-2):\n  " + "\n  ".join(problems),
                    issue_type=IssueType.PossibleBug,
                ).verify_result()


def _verify_otel_parity(engines, nvue_entries, nvue_hca) -> None:
    """OTEL reports the same aggregated HCA peers as NVUE: Aport, counters and extended leaves."""
    snap = ibo.active_surface().snapshot("peer-parity", fresh=True)
    otel_entries = ibh.otel_peer_port_rows(snap)
    otel_hca = ibh.aggregated_peer_entries(ibh.filter_peer_entries_by_type(otel_entries, PeerType.HCA))
    ibh.attach_dict("otel hca", otel_hca)

    with allure.step("NVUE and OTEL expose the same aggregated HCA peer ids"):
        nvue_only = sorted(set(nvue_hca) - set(otel_hca))
        otel_only = sorted(set(otel_hca) - set(nvue_hca))
        assert not nvue_only and not otel_only, (
            f"Aggregated HCA peer-id sets differ across surfaces: "
            f"NVUE-only={nvue_only!r}, OTEL-only={otel_only!r}"
        )

    with allure.step("Per-peer parity: Aport and counters match across NVUE and OTEL"):
        for pid in sorted(nvue_hca):
            n_fields, o_fields = nvue_hca[pid], otel_hca[pid]
            assert ibh.peer_entry_aport(n_fields) == ibh.peer_entry_aport(o_fields), (
                f"HCA peer {pid!r} Aport differs: NVUE={ibh.peer_entry_aport(n_fields)!r}, "
                f"OTEL={ibh.peer_entry_aport(o_fields)!r}"
            )
            compared = ibh.assert_counters_within_tolerance(
                n_fields, o_fields, PEER_PORT_ADDITIVE_FIELDS, SAMPLING_JITTER_TOLERANCE_PCT,
                label=f"NVUE vs OTEL for HCA peer {pid!r}",
            )
            assert compared > 0, (
                f"No additive counter of HCA peer {pid!r} is shown on NVUE and exported over "
                f"OTEL (NVUE keys={sorted(n_fields)!r}, OTEL keys={sorted(o_fields)!r})"
            )

    rows_nvue = ibh.most_specific_peer_entries(
        ibh.filter_peer_entries_by_type(nvue_entries, PeerType.HCA))
    shared_ids = sorted(set(rows_nvue) & set(otel_entries))
    ResultObj(
        bool(shared_ids),
        f"No HCA row is listed on NVUE and exported over OTEL, so the extended leaves cannot be "
        f"compared (NVUE={sorted(rows_nvue)!r}, OTEL={sorted(otel_entries)!r})",
        issue_type=IssueType.PossibleBug,
    ).verify_result()
    pid = shared_ids[0]
    with allure.step(f"Extended leaves of {pid} agree across NVUE and OTEL"):
        problems, compared = ibh.otel_leaf_parity_problems(
            snap, _OTEL_IDENTITY_GROUP, pid, PEER_PORT_ROOT_EXTENDED_LEAVES,
            ibh.nvue_show_peer_port(engines, pid), API_NVUE_CLI)
        nvue_ber = ibh.leaf_lookup(ibh.flatten_nvue_leaves(
            ibh.nvue_peer_port_show(engines, pid, PeerPortNvueSub.LINK_PHY_BER_MONITOR)))
        ber_problems, ber_compared = ibh.otel_leaf_parity_problems(
            snap, OtelGroup(OtelLabel.INTERFACE, PEER_PORT_OTEL_BER_MONITOR_LEAVES), pid,
            BER_MONITOR_LEAVES, nvue_ber, API_NVUE_CLI)
        problems += ber_problems
        compared += ber_compared
        logger.info("Compared %d extended leaf value(s) of %s across NVUE and OTEL", compared, pid)
        ResultObj(
            not problems and compared > 0,
            f"{pid} reports {len(problems)} extended leaf value(s) differently on NVUE and "
            f"OTEL (compared {compared}); only the encoding may differ (SR-1):\n  " +
            "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()


# 5.7 test_peer_port_api_parity_nvue_vs_gnmi (nice-to-have)
@pytest.mark.gnmi
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_peer_port_api_parity_nvue_vs_gnmi(engines, devices, gnmi_client, ib_otel):
    """The same HCA peer is reported identically by NVUE, gNMI and OTEL (peer-type, Aport, counters)."""
    _enable_peer_port(engines)
    _require_listed_hca_peers(engines)

    with allure.step("List peer-ports via NVUE and via gNMI"):
        nvue_entries = ibh.list_peer_ports_via_api(API_NVUE_CLI, engines, gnmi_client)
        _wait_for_gnmi_peer_ports(gnmi_client)
        gnmi_entries = ibh.list_peer_ports_via_api(API_GNMIC, engines, gnmi_client)
        # Compare like-for-like on the aggregated tier only.
        nvue_hca = ibh.aggregated_peer_entries(ibh.filter_peer_entries_by_type(nvue_entries, PeerType.HCA))
        gnmi_hca = ibh.aggregated_peer_entries(ibh.filter_peer_entries_by_type(gnmi_entries, PeerType.HCA))
        ibh.attach_dict("nvue hca", nvue_hca)
        ibh.attach_dict("gnmi hca", gnmi_hca)
        assert nvue_hca, "No HCA peers via NVUE"
        assert gnmi_hca, "No HCA peers via gNMI"
        # The NVUE listing is a summary table (identity columns only); the counters are on the
        # per-peer show.
        nvue_hca = {pid: {**fields, **ibh.nvue_show_peer_port(engines, pid)}
                    for pid, fields in nvue_hca.items()}
        ibh.attach_dict("nvue hca with per-peer counters", nvue_hca)

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
            compared = ibh.assert_counters_within_tolerance(
                n_fields, g_fields, PEER_PORT_ADDITIVE_FIELDS, SAMPLING_JITTER_TOLERANCE_PCT,
                label=f"NVUE vs gNMI for HCA peer {pid!r}",
            )
            assert compared > 0, (
                f"No additive counter of HCA peer {pid!r} is shown on NVUE and served over gNMI "
                f"(NVUE keys={sorted(n_fields)!r}, gNMI keys={sorted(g_fields)!r})"
            )

    _verify_extended_leaf_parity(engines, gnmi_client, nvue_entries, gnmi_entries)
    _verify_otel_parity(engines, nvue_entries, nvue_hca)


def _verify_peer_asic_host_name(engines, topology_obj, devices, pid, leaves) -> None:
    """The host name on a peer ASIC is the remote host from the xcset, never this switch (SR-5)."""
    reported = ibh.peer_asic_host_name(leaves)
    ResultObj(
        bool(reported),
        f"The peer ASIC payload carries no host name under any accepted spelling "
        f"{PEER_ASIC_HOST_NAME_CANDIDATES!r} (SR-5)",
        issue_type=IssueType.PossibleBug,
    ).verify_result()

    xcset_row = ibh.xcset_row_for_db_row(
        ibh.read_peer_port_row(engines, pid),
        ibh.xcset_rows_by_join_key(ibh.read_hca_xcset_rows(engines)),
    )
    expected = ibh.xcset_host_name(xcset_row)
    switch_host = ibh.dut_hostname(topology_obj, devices)
    ibh.attach_dict("peer ASIC host name", {"reported": reported, "xcset": expected,
                                            "xcset row found": bool(xcset_row),
                                            "this switch": switch_host})

    problems = []
    if switch_host and reported.lower() == switch_host.lower():
        problems.append(f"the peer ASIC reports this switch's own hostname {reported!r}; it "
                        f"must report the remote host")
    if not xcset_row:
        problems.append(f"no xcset row joins to {pid}, so the reported host name "
                        f"{reported!r} has nothing to be compared against")
    elif not expected:
        problems.append(f"the xcset row behind {pid} names no host, in "
                        f"{PeerComponentXcsetColumn.HOST_NAME!r} or "
                        f"{PeerComponentXcsetColumn.NODE_DESCRIPTION!r}, to compare the reported "
                        f"host name {reported!r} against")
    elif reported.lower() != expected.lower():
        problems.append(f"the peer ASIC reports host name {reported!r} while the xcset row "
                        f"for {pid} says {expected!r}")
    ResultObj(
        not problems,
        "Peer ASIC host name problems (SR-5):\n  " + "\n  ".join(problems),
        issue_type=IssueType.PossibleBug,
    ).verify_result()


def _verify_peer_platform_otel(node, name: str, body: Dict) -> None:
    """The component exports the node's leaves over OTEL, reading the same as the CLI (SR-1)."""
    snap = ibo.active_surface().snapshot("peer-platform", platform=True)
    groups = [PEER_PLATFORM_OTEL_GROUPS[node.node_id]] + [
        PEER_PLATFORM_OTEL_GROUPS[extra] for extra in PEER_PLATFORM_OTEL_EXTRA_GROUPS.get(node.node_id, ())
    ]
    problems: List[str] = []
    compared = 0
    if node.member_leaves:
        cli_members = {ibh.list_member_index(key): ibh.peer_port_show_leaves(member)
                       for key, member in ibh.peer_port_show_containers(body).items()}
        for group in groups:
            otel_members = ibh.otel_member_labels(snap, group, name)
            missing = sorted(set(cli_members) - set(otel_members))
            if missing:
                problems.append(f"{name}: OTEL exports no {group.member_label} {missing} of the "
                                f"CLI's {sorted(cli_members)}")
            expected = {leaf: kind for leaf, kind in node.member_leaves.items()
                        if leaf in group.leaves}
            for index, cli_member in sorted(cli_members.items()):
                if index not in otel_members:
                    continue
                member_problems, member_compared = ibh.otel_leaf_parity_problems(
                    snap, group, name, expected, cli_member, API_NVUE_CLI,
                    member=otel_members[index])
                problems += member_problems
                compared += member_compared
    else:
        problems, compared = ibh.otel_leaf_parity_problems(
            snap, groups[0], name, node.leaves, ibh.peer_port_show_leaves(body), API_NVUE_CLI)
    logger.info("Compared %d %s leaf value(s) across the CLI and OTEL", compared, node.node_id)
    ResultObj(
        not problems and compared > 0,
        f"Component {name} exports {len(problems)} {node.node_id} leaf value(s) over OTEL that "
        f"differ from or are missing against the CLI (compared {compared}); only the encoding "
        f"may differ, and sampled values may differ within sampling jitter (SR-1):\n  " +
        "\n  ".join(problems),
        issue_type=IssueType.PossibleBug,
    ).verify_result()


# 5.7 test_peer_platform_transceiver_and_asic_show
@pytest.mark.gnmi
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.platform
@pytest.mark.transceiver
@pytest.mark.multiplanar
@pytest.mark.nvos_build
@pytest.mark.timeout(12 * MINUTE, func_only=True)
@pytest.mark.parametrize("node", PEER_PLATFORM_SHOW_NODES, ids=lambda n: n.node_id)
def test_peer_platform_transceiver_and_asic_show(engines, devices, gnmi_client, topology_obj, node, ib_otel,
                                                 random_api):
    """A neighbor's cable and chip open by name and report their identity, readings and alarms.

    The CLI, gNMI and OTEL agree on them.
    """
    _enable_peer_port(engines)
    _require_listed_hca_peers(engines)

    with allure.step("Take the component names one HCA neighbor points at"):
        pid, names = ibh.hca_peer_component_names(engines, gnmi_client)
        ibh.attach_dict("peer component names", {"peer": pid, "components": names})
        ResultObj(
            node.kind in names,
            f"No HCA row names a {node.kind!r} component, so the {node.node_id!r} "
            f"sub-command has nothing to open (SR-4)",
            issue_type=IssueType.PossibleBug,
        ).verify_result()
        name = names[node.kind]

    cmd = f"nv show peer-platform {node.kind} {name} {node.sub}".strip()
    with allure.step(f"Verify `{cmd}`"):
        body = ibh.nvue_peer_platform_show(engines, node.kind, name, node.sub)
        ibh.attach_dict(cmd, body)
        problems = ibh.peer_platform_show_problems(body, node)
        ResultObj(
            not problems,
            f"`{cmd}` has {len(problems)} contract problem(s):\n  " + "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    if node.member_leaves:
        with allure.step(f"Open each member with `{cmd} <ch>`"):
            problems = []
            for member_key in sorted(ibh.peer_port_show_containers(body)):
                shown = ibh.nvue_peer_platform_show(engines, node.kind, name,
                                                    f"{node.sub} {member_key}")
                problems += [f"`{cmd} {member_key}` {problem}" for problem in ibh.leaf_problems(
                    ibh.peer_port_show_leaves(shown), node.member_leaves, where=node.node_id)]
            ResultObj(
                not problems,
                f"{len(problems)} problem(s) opening single members of `{cmd}` (HLD section "
                f"5.3):\n  " + "\n  ".join(problems),
                issue_type=IssueType.PossibleBug,
            ).verify_result()

    # A node either renders one flat payload or a keyed list of members, never both.
    gnmi_members = (ibh.gnmi_peer_platform_members(gnmi_client, node, name)
                    if node.member_leaves else {})
    gnmi_leaves = ({} if node.member_leaves
                   else ibh.gnmi_peer_platform_leaves(gnmi_client, node, name))

    with allure.step(f"Verify the same {node.node_id} leaves over gNMI"):
        if node.member_leaves:
            ibh.attach_dict(f"gnmi {node.node_id} members", gnmi_members)
            problems = [] if gnmi_members else [f"gNMI exposes no {node.node_id} member"]
            problems += [
                f"member [{member_key}] {problem}"
                for member_key, member in sorted(gnmi_members.items())
                for problem in ibh.leaf_problems(member, node.member_leaves, where=name)
            ]
        else:
            ibh.attach_dict(f"gnmi {node.node_id}", gnmi_leaves)
            problems = ibh.leaf_problems(gnmi_leaves, node.leaves, where=name)
        ResultObj(
            not problems,
            f"The gNMI {node.node_id} leaves of component {name} have {len(problems)} "
            f"problem(s):\n  " + "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    if node.kind == PeerPlatformKind.ASIC and node.sub == PeerPlatformSub.ROOT:
        with allure.step("The peer ASIC names the remote host, not this switch"):
            _verify_peer_asic_host_name(engines, topology_obj, devices, pid,
                                        ibh.leaf_lookup(ibh.flatten_nvue_leaves(body)))

    if not body:
        return
    with allure.step(f"The CLI and gNMI agree on the {node.node_id} leaves"):
        if node.member_leaves:
            cli_members = {ibh.list_member_index(key): ibh.peer_port_show_leaves(member)
                           for key, member in ibh.peer_port_show_containers(body).items()}
            gnmi_by_index = {ibh.list_member_index(key): member
                             for key, member in gnmi_members.items()}
            problems, compared = [], 0
            if set(cli_members) != set(gnmi_by_index):
                problems.append(f"the CLI lists member(s) {sorted(cli_members)!r} while gNMI "
                                f"lists {sorted(gnmi_by_index)!r}")
                # Report a pure 0-based/1-based numbering gap once, then compare the members
                # side by side so a real value difference is not buried under it.
                gnmi_by_index = ibh.shift_member_index_to_match(cli_members, gnmi_by_index)
            for member_key, cli_member in sorted(cli_members.items()):
                member_problems, member_compared = ibh.leaf_parity_problems(
                    node.member_leaves, cli_member, gnmi_by_index.get(member_key, {}),
                    API_NVUE_CLI, API_GNMIC)
                problems += [f"member [{member_key}] {problem}" for problem in member_problems]
                compared += member_compared
        else:
            problems, compared = ibh.leaf_parity_problems(
                node.leaves, ibh.peer_port_show_leaves(body), gnmi_leaves,
                API_NVUE_CLI, API_GNMIC)
        logger.info("Compared %d %s leaf value(s) across both surfaces", compared, node.node_id)
        ResultObj(
            not problems,
            f"Component {name} reports {len(problems)} {node.node_id} leaf value(s) "
            f"differently on the CLI and over gNMI; only the encoding may differ, and sampled "
            f"values may differ within sampling jitter (SR-1):\n  " + "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step(f"The CLI and OTEL agree on the {node.node_id} leaves"):
        _verify_peer_platform_otel(node, name, body)


# 6.10 test_hca_xcset_ingestion (plan §6.2)
@pytest.mark.gnmi
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.requires_hfnm
@pytest.mark.timeout(14 * MINUTE, func_only=True)
def test_hca_xcset_ingestion(engines, devices, setup_topology, setup_name, request, ib_otel):
    """gpu-telemetry writes each HCA as its own DB row; additive counters stay monotonic under traffic.

    OTEL exports every ingested row, and its counters stay monotonic across the same traffic.
    """
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

    with allure.step("OTEL exports every ingested aggregated HCA row"):
        otel_initial = _otel_hca_rows("pre-traffic")
        missing = sorted(set(hca_rows) - set(otel_initial))
        assert not missing, (
            f"Ingested aggregated HCA row(s) not exported over OTEL: {missing!r}"
        )

    with allure.step("Snapshot the HCA row, drive an IB traffic burst, re-read"):
        initial = ibh.read_peer_port_row(engines, one_pid)
        Tools.TrafficGeneratorTool.send_ib_traffic(players, interfaces, setup_name, True).verify_result()
        time.sleep(PEER_TELEMETRY_SAMPLING_SEC)
        after = ibh.read_peer_port_row(engines, one_pid)
        ibh.attach_dict(f"hca {one_pid} pre/post traffic", {"initial": initial, "after": after})
        assert after, f"HCA row {one_pid} vanished after traffic"
        otel_after = _otel_hca_rows("post-traffic")

    with allure.step("Additive counters are monotonic across the traffic burst"):
        ibh.assert_counters_monotonic(
            initial, after, PEER_PORT_DB_ADDITIVE_FIELDS,
            label=f"HCA {one_pid} across traffic",
        )

    with allure.step("OTEL additive counters are monotonic across the traffic burst"):
        assert one_pid in otel_after, f"HCA peer {one_pid} is no longer exported over OTEL after traffic"
        compared = ibh.assert_counters_monotonic(
            otel_initial[one_pid], otel_after[one_pid], list(PEER_PORT_OTEL_ADDITIVE_LEAVES),
            label=f"HCA {one_pid} over OTEL across traffic",
        )
        assert compared > 0, f"HCA peer {one_pid} exports no additive counter over OTEL"


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
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_hca_peerport_to_aport_mapping(engines, devices, gnmi_client, setup_topology, ib_otel):
    """Every HCA peer-port maps to a real local switch port matching external HCA cabling (by plane IB port).

    Checked on gNMI and on the OTEL switch_info.
    """
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

    with allure.step("OTEL: aggregated HCA peers reference valid Aports matching external cabling"):
        otel_live = ibh.aggregated_peer_entries(_otel_hca_rows("peer-aport-mapping"))
        assert otel_live, f"No aggregated peer-type={PeerType.HCA} peers exported over OTEL"
        _assert_peers_reference_known_aports(otel_live, valid_aports)
        _assert_external_hca_neighbor_guids_present(
            ibh.aggregated_hca_node_guids(otel_live),
            expected_guids,
            label=API_OTEL,
            row_count=len(otel_live),
        )
        _assert_switch_ports_cover_external_hca(otel_live, expected_ports)


# 6.12 test_hca_peerport_aggregation (plan §6.5)
@pytest.mark.gnmi
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_hca_peerport_aggregation(engines, devices, gnmi_client, setup_topology, ib_otel):
    """Validate HCA parent/child aggregation: plane rows link to parent; counters sum when guids partition.

    OTEL exports the aggregate and its plane rows, with the same roll-up.
    """
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

    with allure.step(f"OTEL exports {parent} and its plane rows"):
        otel_rows = _otel_peer_rows("peer-aggregation")
        missing = [pid for pid in [parent, *member_pids] if pid not in otel_rows]
        assert not missing, (
            f"HCA aggregate {parent} / plane row(s) not exported over OTEL: {missing!r}"
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

    with allure.step("OTEL: the aggregate's additive counters are the sum of its plane rows"):
        compared = 0
        for leaf in PEER_PORT_OTEL_ADDITIVE_LEAVES:
            member_vals = [otel_rows[pid].get(leaf) for pid in member_pids]
            agg_val = otel_rows[parent].get(leaf)
            if agg_val is None or None in member_vals:
                continue
            summed = sum(int(v) for v in member_vals)
            assert ibh.values_within_tolerance(summed, int(agg_val), SAMPLING_JITTER_TOLERANCE_PCT), (
                f"HCA {parent} aggregate {leaf}={agg_val} over OTEL != sum of "
                f"{len(member_pids)} plane rows ({summed})"
            )
            compared += 1
        assert compared > 0, (
            f"No additive counter is exported over OTEL on both {parent} and every plane row "
            f"{member_pids!r} (agg keys={sorted(otel_rows[parent])!r})"
        )


# 6.13 test_hca_peerport_db_api_set_consistency (nice-to-have)
@pytest.mark.gnmi
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_hca_peerport_db_api_set_consistency(engines, devices, gnmi_client, setup_topology, ib_otel):
    """The HCA peer set in System DB exactly matches gNMI and OTEL, with unique keys (no duplicate ingestion)."""
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
        db_identities = {ibh.peer_entry_identity(r, fallback=pid) for pid, r in db_rows.items()}
        api_identities = {ibh.peer_entry_identity(f, fallback=pid) for pid, f in api_hca.items()}
        missing_in_api = db_identities - api_identities
        orphan_in_api = api_identities - db_identities
        assert not missing_in_api, (
            f"HCA rows present in System DB but not exposed via gNMI: {sorted(missing_in_api)!r}"
        )
        assert not orphan_in_api, (
            f"HCA peers exposed via gNMI with no System DB row: {sorted(orphan_in_api)!r}"
        )

    with allure.step("DB and OTEL expose the same HCA identity set"):
        otel_hca = _otel_hca_rows("peer-set")
        otel_identities = {ibh.peer_entry_identity(f, fallback=pid) for pid, f in otel_hca.items()}
        assert db_identities == otel_identities, (
            f"HCA identity sets differ between System DB and OTEL: "
            f"DB-only={sorted(db_identities - otel_identities)!r}, "
            f"OTEL-only={sorted(otel_identities - db_identities)!r}"
        )


# 6.14 test_peer_telemetry_health_steady_state_healthy (nice-to-have)
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_peer_telemetry_health_steady_state_healthy(engines, devices, setup_topology, ib_otel):
    """In steady state, gpu-telemetry publishes PEER_PORT_TELEMETRY_HEALTH healthy in STATE_DB.

    Every ingested HCA peer then exports a defined link health over OTEL.
    """
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
        assert health == PeerTelemetryHealth.HEALTHY, (
            f"Steady-state PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}); expected healthy."
        )

    with allure.step(f"OTEL: every ingested HCA peer exports {PEER_PORT_OTEL_LINK_HEALTH}"):
        rows = ibh.list_hca_peer_rows(engines)
        assert rows, "No HCA peer-port rows in System DB (HCA feature build delivered?)"
        snap = ibo.active_surface().snapshot("peer-health", fresh=True)
        problems = []
        for pid in sorted(rows):
            series = snap.current(PEER_PORT_OTEL_LINK_HEALTH, **{OtelLabel.INTERFACE: pid})
            if len(series) != 1:
                problems.append(f"{pid}: {len(series)} current series (expected 1)")
            elif series[0].latest not in OTEL_LINK_HEALTH_STATES:
                problems.append(f"{pid}: {series[0].latest} is not one of {OTEL_LINK_HEALTH_STATES}")
        assert not problems, (
            f"{PEER_PORT_OTEL_LINK_HEALTH} problems on {len(problems)} peer(s):\n  " +
            "\n  ".join(problems)
        )


# 7.4 test_peer_port_gnmi_schema_paths_present_and_typed (nice-to-have)
# skip_clear_config: keep peer-port enabled across all variants; per-variant
# disable + cold re-ingest otherwise flakes on not-yet-populated leaves.
@pytest.mark.skip_clear_config
@pytest.mark.gnmi
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(14 * MINUTE, func_only=True)
@pytest.mark.parametrize("group", PEER_PORT_SCHEMA_GROUPS, ids=lambda g: g.group_id)
def test_peer_port_gnmi_schema_paths_present_and_typed(engines, devices, gnmi_client, setup_topology, group,
                                                       ib_otel):
    """Positive gNMI schema sweep of the nvidia-peer-port model: every documented leaf present and typed.

    Every peer exposing the container over gNMI exports the same leaves over OTEL.
    """
    _enable_peer_port(engines)
    targets = _peer_port_schema_targets(gnmi_client, group)
    problems: List[str] = []
    exposed_payloads: List[Tuple[str, str, Dict[str, str]]] = []

    # A peer that doesn't implement a per-port container (a plane leg whose link is down)
    # answers the subscribe with gNMI Unimplemented "cannot find the entry" - a per-peer
    # applicability gap, not a schema violation. Tolerate it and count how many peers exposed
    # it. A component container is addressed by a name a peer-port published, so it must exist.
    exposed = 0
    with allure.step(f"Sweep peer-port container {group.group_id!r} across {len(targets)} target(s)"):
        for role, pid in targets:
            prefix = group.prefix.format(name=pid)
            with allure.independent_step(f"Sweep {group.group_id} on {role} {pid} ({prefix})"):
                read = ibh.gnmi_get_flat_relative if group.relative_leaves else ibh.gnmi_get_flat
                try:
                    payload = read(gnmi_client, prefix=prefix, path="")
                except AssertionError as exc:
                    if not group.component_ref and ibh.is_gnmi_entry_absent(str(exc)):
                        logger.info(
                            "[%s %s] container %s not implemented for this peer "
                            "(gNMI 'cannot find the entry'); tolerated as N/A",
                            role, pid, prefix,
                        )
                        continue
                    raise
                exposed += 1
                exposed_payloads.append((role, pid, payload))
                ibh.attach_dict(f"{group.group_id} {role} {pid}", payload)
                # `pending` leaves (known build gap) are tolerated-missing but still typed.
                problems += [
                    f"[{role} {pid}] {problem}"
                    for problem in ibh.leaf_problems(
                        payload, group.leaves, tolerated_missing=group.pending, where=prefix)
                ]

    otel_group = PEER_PORT_OTEL_SCHEMA_GROUPS.get(group.group_id)
    if otel_group is None:
        problems.append(f"container {group.group_id!r} has no OTEL rendering defined")
    elif exposed_payloads:
        with allure.step(f"The same {group.group_id!r} leaves are exported over OTEL"):
            snap = ibo.active_surface().snapshot(
                "peer-schema", platform=bool(group.component_ref), fresh=True)
            for role, pid, payload in exposed_payloads:
                # A pending leaf is required over OTEL only where gNMI already carries it and
                # the leaf has an OTEL rendering.
                unexported = PEER_PORT_OTEL_UNEXPORTED_SCHEMA_LEAVES.get(group.group_id, frozenset())
                leaves = [leaf for leaf in group.leaves
                          if leaf not in unexported and
                          (leaf not in group.pending or
                           (leaf in payload and leaf in otel_group.leaves))]
                problems += [f"[{role} {pid}] OTEL: {problem}"
                             for problem in ibo.group_problems(snap, otel_group, pid, leaves=leaves)]

    if exposed == 0:
        pytest.skip(
            f"peer-port container {group.group_id!r} is not exposed by any peer on this "
            f"setup (FNM-only HCA or down plane legs); nothing to schema-check here."
        )
    assert not problems, (
        f"peer-port gNMI schema sweep for container {group.group_id!r} found "
        f"{len(problems)} problem(s):\n  " + "\n  ".join(problems)
    )


# 6.6, last step: a column NMX-T does not serve omits one leaf, not the row (SR-7).
#
# Its own case rather than a step of the parameterized sweep, because it swaps out the NMX-T
# backend - as a step it would tear the backend down and rebuild it once per schema group.
def _otel_missing_column_problems(snap: ibo.OtelSnapshot, pid: str,
                                  before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """The peer is still exported, and nothing its now-missing DB fields fed is (SR-7)."""
    dropped = sorted({f for f, v in before.items() if str(v).strip()} -
                     {f for f, v in after.items() if str(v).strip()})
    problems = []
    if not snap.current(PEER_PORT_OTEL_NODE_INFO, **{OtelLabel.INTERFACE: pid}):
        problems.append(f"{pid} is no longer exported over OTEL ({PEER_PORT_OTEL_NODE_INFO}); "
                        f"only the leaf the column fed may go")
    identity = ibo.peer_identity(snap, pid)
    label_by_field = {db_field: label for label, db_field in PEER_PORT_OTEL_IDENTITY_DB_FIELD.items()}
    for db_field in dropped:
        metric = PEER_PORT_OTEL_METRIC_BY_DB_FIELD.get(db_field)
        if metric and snap.current(metric, **{OtelLabel.INTERFACE: pid}):
            problems.append(f"{pid}: {metric} is still exported although DB {db_field} is gone")
        label = label_by_field.get(db_field)
        if label and not ibh.component_source_absent(identity.get(label)):
            problems.append(f"{pid}: identity label {label}={identity[label]!r} is still exported "
                            f"although DB {db_field} is gone")
    return problems


@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(12 * MINUTE, func_only=True)
def test_peer_port_missing_xcset_column_omits_one_leaf(engines, devices, gnmi_client, ib_otel):
    """A column absent from the xcset omits its leaf; the peer-port row and its components survive.

    The OTEL export drops the same leaf and keeps the peer.
    """
    _enable_peer_port_for_hca(engines)
    _require_listed_hca_peers(engines)

    with allure.step("Read the xcset NMX-T serves and pick an extended column to remove"):
        raw = ibh.read_hca_xcset_raw(engines)
        assert raw, (
            "NMX-T-for-IB served an empty HCA xcset, so there is no sample to rewrite. "
            "Confirm NMX-T-for-IB is serving the HCA report."
        )
        column, pid = ibh.select_droppable_xcset_column(
            engines, ibh.hca_xcset_rows_from_raw(raw))
        ResultObj(
            bool(column),
            "The served xcset carries no extended column with a value on any ingested HCA "
            "row, so no column can be removed to exercise SR-7",
            issue_type=IssueType.PossibleBug,
        ).verify_result()
        logger.info("Removing column %r, which feeds peer-port %s", column, pid)

    with allure.step(f"Serve the xcset without {column!r} and re-read {pid}"):
        before = ibh.read_peer_port_row(engines, pid)
        refs_before = ibh.peer_component_refs(ibh.gnmi_get_peer_port(gnmi_client, pid))
        assert before, f"No COUNTERS_DB PEER_COUNTERS row for {pid} before the column was removed"
        with ibh.serving_hca_xcset(engines, ibh.xcset_csv_drop_column(raw, column)):
            time.sleep(_peer_port_interval_sec() * PEER_PORT_SETTLE_INTERVALS)
            after = ibh.read_peer_port_row(engines, pid)
            refs_after = ibh.peer_component_refs(ibh.gnmi_get_peer_port(gnmi_client, pid))
            otel_snap = ibo.active_surface().snapshot("xcset-column-dropped", fresh=True)
        ibh.attach_dict(f"{pid} with and without {column}",
                        {"before": before, "after": after,
                         "refs before": refs_before, "refs after": refs_after})

    with allure.step(f"The {pid} row survives and no field is reported as zero instead"):
        problems = ibh.missing_xcset_column_problems(before, after, column,
                                                     refs_before, refs_after)
        if column in before and column in after:
            problems.append(f"COUNTERS_DB still holds {column}={after[column]!r} on {pid} after "
                            f"the column was removed from the served xcset; SR-7 requires the "
                            f"leaf to be omitted, not to keep its last value")
        elif before == after:
            problems.append(f"removing {column!r} changed no field of {pid}, although the "
                            f"column was selected for feeding it")
        ResultObj(
            not problems,
            f"Removing xcset column {column!r} broke {len(problems)} SR-7 expectation(s):\n  " +
            "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step(f"OTEL keeps exporting {pid} and drops only what {column!r} fed"):
        problems = _otel_missing_column_problems(otel_snap, pid, before, after)
        ResultObj(
            not problems,
            f"Removing xcset column {column!r} broke {len(problems)} SR-7 expectation(s) over "
            f"OTEL:\n  " + "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()


def _verify_unnamed_sources_invent_nothing(engines, gnmi_client, raw, peers, db_rows,
                                           xcset_by_key) -> None:
    """Blanking an identifier column drops that component only; the peer's row survives (SR-7).

    No lab has a peer whose serial or node GUID is genuinely missing, so the condition is
    produced by serving an xcset with the column cleared on one row.
    """
    target = next((pid for pid in sorted(peers)
                   if ibh.xcset_row_for_db_row(db_rows.get(pid, {}), xcset_by_key)), "")
    ResultObj(
        bool(target),
        "No listed HCA row joins to an xcset row, so no identifier can be blanked (SR-7)",
        issue_type=IssueType.PossibleBug,
    ).verify_result()
    join_key = ibh.hca_xcset_join_key(db_rows[target])

    with allure.step(f"An identifier-less source on {target} invents no component (SR-7)"):
        for column, ref_leaf in ((PeerComponentXcsetColumn.MODULE_SERIAL,
                                  PeerPortComponentRef.TRANSCEIVER),
                                 (PeerComponentXcsetColumn.NODE_GUID,
                                  PeerPortComponentRef.INTEGRATED_CIRCUIT)):
            with allure.independent_step(f"Blank {column} on {target} and re-read it"):
                ResultObj(
                    column in ibh.xcset_header(raw),
                    f"NMX-T serves no {column!r} column, which the {ref_leaf} component is "
                    f"named from (SR-4)",
                    issue_type=IssueType.PossibleBug,
                ).verify_result()
                named_before = ibh.peer_component_refs(peers[target]).get(ref_leaf, "")
                with ibh.serving_hca_xcset(
                        engines, ibh.xcset_csv_blank_column(raw, column, join_key)):
                    time.sleep(_peer_port_interval_sec() * PEER_PORT_SETTLE_INTERVALS)
                    row = ibh.read_peer_port_row(engines, target)
                    refs = ibh.peer_component_refs(ibh.gnmi_get_peer_port(gnmi_client, target))
                    listed = set(ibh.gnmi_list_members(
                        gnmi_client, GnmiYangPaths.PEER_PORT_COMPONENTS, relative=True))
                ibh.attach_dict(f"{target} with {column} blanked",
                                {"row": row, "refs": refs, "components": sorted(listed)})

                problems = []
                if not row:
                    problems.append(
                        f"the {target} row disappeared when {column} was cleared; the peer's own "
                        f"row must still be listed"
                    )
                if refs.get(ref_leaf):
                    problems.append(
                        f"{target} still names a {ref_leaf} component {refs[ref_leaf]!r} with "
                        f"{column} cleared; no component may be created without an identifier"
                    )
                # Peers sharing one module share one name, so the component may legitimately
                # survive on someone else's reference; only an orphan must disappear.
                still_referenced = any(
                    named_before and ibh.peer_component_refs(other).get(ref_leaf) == named_before
                    for pid, other in peers.items() if pid != target
                )
                if named_before and not still_referenced and named_before in listed:
                    problems.append(
                        f"component {named_before!r} is still listed under "
                        f"{GnmiYangPaths.PEER_PORT_COMPONENTS} although {column} was cleared and "
                        f"no other peer references it"
                    )
                ResultObj(
                    not problems,
                    f"Clearing {column} broke {len(problems)} SR-7 expectation(s):\n  " +
                    "\n  ".join(problems),
                    issue_type=IssueType.PossibleBug,
                ).verify_result()


def _otel_peer_component_names(snap: ibo.OtelSnapshot) -> set:
    """Names of the peer transceivers and ASICs the OTEL window exports."""
    names = set()
    for node_id in ("transceiver-identity", "asic"):
        for spec in PEER_PLATFORM_OTEL_GROUPS[node_id].leaves.values():
            names |= snap.keys(spec.metric, OtelLabel.NAME)
    return names


# 6.9 test_peer_component_naming_and_linkage
@pytest.mark.gnmi
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.transceiver
@pytest.mark.multiplanar
@pytest.mark.nvos_build
@pytest.mark.timeout(14 * MINUTE, func_only=True)
def test_peer_component_naming_and_linkage(engines, devices, gnmi_client, ib_otel):
    """Peer cables and chips are named per the rule, linked from the peer-port, never invented.

    OTEL names the same components on each peer and exports every one of them.
    """
    _enable_peer_port_for_hca(engines)
    listed_hca = _require_listed_hca_peers(engines)

    with allure.step("Read the xcset identifiers and the gNMI state behind every HCA row"):
        raw = ibh.read_hca_xcset_raw(engines)
        xcset_by_key = ibh.xcset_rows_by_join_key(ibh.hca_xcset_rows_from_raw(raw))
        db_rows = ibh.list_hca_peer_rows(engines)
        # The references are read from the gNMI state the naming rule is defined on; whether
        # `nv show peer-port <peer>` renders them identically is the listing test's check.
        states = ibh.gnmi_get_peer_port_list(gnmi_client)
        peers = {pid: states.get(pid, {})
                 for pid in ibh.most_specific_peer_entries(listed_hca)}
        ibh.attach_dict("xcset rows by join key", xcset_by_key)
        ibh.attach_dict("gnmi state of the hca rows", peers)
        assert xcset_by_key, (
            "NMX-T-for-IB served no HCA xcset row, so there are no identifiers to name a "
            "component from. Confirm NMX-T-for-IB is serving the HCA report."
        )

    with allure.step(f"Component names on {len(peers)} peer(s) follow the naming rule"):
        problems = []
        named = 0
        for pid, fields in sorted(peers.items()):
            xcset_row = ibh.xcset_row_for_db_row(db_rows.get(pid, {}), xcset_by_key)
            if not xcset_row:
                problems.append(f"[{pid}] joins to no xcset row, so its component names have "
                                f"no identifiers to be checked against")
                continue
            refs = ibh.peer_component_refs(fields)
            named += bool(refs)
            problems += ibh.component_naming_problems(pid, xcset_row, refs)
        if not named:
            problems.append("no ingested HCA row names a transceiver or an "
                            "integrated-circuit component")
        ResultObj(
            not problems,
            f"Peer component naming has {len(problems)} problem(s). A name is "
            f"xcvr-<serial>[-<sys-image-guid>] or asic-<node-guid>, with a leading 0x dropped "
            f"and every other character replaced (SR-4):\n  " + "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step("Every named component resolves in the gNMI component tree"):
        listed = set(ibh.gnmi_list_members(
            gnmi_client, GnmiYangPaths.PEER_PORT_COMPONENTS, relative=True))
        wanted = {name for fields in peers.values()
                  for name in ibh.peer_component_refs(fields).values()}
        ibh.attach_dict("peer components", {"listed": sorted(listed), "named": sorted(wanted)})
        unlisted = sorted(wanted - listed)
        ResultObj(
            not unlisted,
            f"{len(unlisted)} component name(s) are published on a peer-port but absent from "
            f"{GnmiYangPaths.PEER_PORT_COMPONENTS} (SR-4): {unlisted!r}",
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step("Peers sharing one physical module share one transceiver name"):
        shared_problems = ibh.shared_module_problems(peers, xcset_by_key, db_rows)
        ResultObj(
            not shared_problems,
            "Ports on one module must share one component name (SR-4):\n  " +
            "\n  ".join(shared_problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step("OTEL names the same components and exports each of them"):
        snap = ibo.active_surface().snapshot("peer-components", platform=True, fresh=True)
        otel_problems = []
        for pid, fields in sorted(peers.items()):
            refs = ibh.peer_component_refs(fields)
            otel_refs = ibh.peer_component_refs(ibo.peer_identity_leaf_values(snap, pid))
            if refs != otel_refs:
                otel_problems.append(f"[{pid}] names {otel_refs!r} over OTEL but {refs!r} over gNMI")
        exported = _otel_peer_component_names(snap)
        ibh.attach_dict("peer components over OTEL", {"exported": sorted(exported),
                                                      "named": sorted(wanted)})
        otel_problems += [f"component {name!r} is named on a peer-port but not exported over OTEL"
                          for name in sorted(wanted - exported)]
        ResultObj(
            not otel_problems,
            f"OTEL peer component linkage has {len(otel_problems)} problem(s) (SR-4):\n  " +
            "\n  ".join(otel_problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    _verify_unnamed_sources_invent_nothing(engines, gnmi_client, raw, peers, db_rows, xcset_by_key)


def _otel_list_group_problems(group, listed: List[Tuple[str, str, Dict]]) -> List[str]:
    """Each gNMI list is exported over OTEL with the same members (histogram: the same buckets)."""
    otel_group = PEER_PORT_OTEL_LIST_GROUPS.get(group.group_id)
    if otel_group is None:
        return [f"list {group.group_id!r} has no OTEL rendering defined"]
    snap = ibo.active_surface().snapshot("peer-list-schema", platform=bool(group.component_ref),
                                         fresh=True)
    problems = []
    for role, name, members in listed:
        where = f"[{role} {name}] OTEL:"
        if otel_group.member_label is None:
            for leaf, spec in otel_group.leaves.items():
                series = snap.current(spec.metric, **{otel_group.key_label: name})
                if len(series) != 1:
                    problems.append(f"{where} {leaf} ({spec.metric}) has {len(series)} current "
                                    f"series (expected 1)")
                elif len(series[0].bucket_counts) != len(members):
                    problems.append(f"{where} {leaf} ({spec.metric}) has "
                                    f"{len(series[0].bucket_counts)} bucket(s) but gNMI lists "
                                    f"{len(members)} member(s)")
            continue
        otel_members = ibh.otel_member_labels(snap, otel_group, name)
        gnmi_members = {ibh.list_member_index(key) for key in members}
        if set(otel_members) != gnmi_members:
            problems.append(f"{where} exports {otel_group.member_label} {sorted(otel_members)} but "
                            f"gNMI lists {sorted(gnmi_members)}")
        for _, member in sorted(otel_members.items()):
            problems += [f"{where} {problem}" for problem in
                         ibo.group_problems(snap, otel_group, name, members=[member])]
    return problems


# 7.5 test_peer_port_gnmi_list_subtree_paths_present_and_typed (nice-to-have)
@pytest.mark.gnmi
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(12 * MINUTE, func_only=True)
@pytest.mark.parametrize("group", PEER_PORT_LIST_SCHEMA_GROUPS, ids=lambda g: g.group_id)
def test_peer_port_gnmi_list_subtree_paths_present_and_typed(engines, devices, gnmi_client, setup_topology, group,
                                                             ib_otel):
    """Positive schema sweep of the peer-port list subtrees: each exposed member's value leaf present and typed (empty tolerated).

    Every list gNMI exposes is exported over OTEL with the same members.
    """
    _enable_peer_port(engines)
    targets = _peer_port_schema_targets(gnmi_client, group)
    problems: List[str] = []
    listed: List[Tuple[str, str, Dict]] = []

    with allure.step(f"Sweep peer-port list {group.group_id!r} across {len(targets)} target(s)"):
        for role, name in targets:
            prefix = group.list_prefix.format(name=name)
            with allure.independent_step(f"Sweep list {group.group_id} on {role} {name} ({prefix})"):
                # Members that nest the same leaf name under two parents must be keyed by the
                # path relative to the member, or the readings collapse onto each other.
                members = ibh.gnmi_list_members(gnmi_client, prefix,
                                                relative=group.relative_leaves)
                ibh.attach_dict(f"{group.group_id} {role} {name}", members)
                if not members and group.component_ref:
                    problems.append(f"[{role} {name}] a fitted module exposes no "
                                    f"{group.group_id} member")
                    continue
                if not members:
                    logger.info(
                        "Peer-port list %s on %s %s exposes no members - tolerated "
                        "(no oper-status leaf to force presence)", group.group_id, role, name,
                    )
                    continue
                listed.append((role, name, members))
                for member_key, leaves in members.items():
                    problems += [
                        f"[{role} {name}] member [{member_key}] {problem}"
                        for problem in ibh.leaf_problems(leaves, group.leaves, where=prefix)
                    ]

    if listed:
        with allure.step(f"The same {group.group_id!r} members are exported over OTEL"):
            problems += _otel_list_group_problems(group, listed)

    assert not problems, (
        f"peer-port gNMI list-subtree sweep for {group.group_id!r} found "
        f"{len(problems)} problem(s):\n  " + "\n  ".join(problems)
    )


# ---------------------------------------------------------------------------
# gNMI subscribe heartbeat on the peer-port surface (Redmine 5230671 item 1)
# ---------------------------------------------------------------------------


def _require_link_up_port(purpose: str):
    """A link-up switch Port, or skip because the lab cannot host this extra coverage."""
    return ibh.require_link_up_port(purpose, skip=True)


def _peer_port_counter_paths(targets: List[Tuple[str, str]]) -> List[str]:
    """Absolute gNMI paths of the additive counters of each selected peer-port."""
    return [
        f"{GnmiYangPaths.PEER_PORT_STATE_COUNTERS.format(name=pid)}/{field}"
        for _, pid in targets
        for field in PEER_PORT_GNMI_ADDITIVE_FIELDS
    ]


def _peer_port_static_paths(targets: List[Tuple[str, str]]) -> List[str]:
    """Absolute gNMI paths of the unchanging identity leaves of each selected peer-port."""
    return [
        f"{GnmiYangPaths.PEER_PORT_STATE.format(name=pid)}/{leaf}"
        for _, pid in targets
        for leaf in PEER_PORT_GNMI_STATIC_LEAVES
    ]


@pytest.mark.skip_clear_config
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_peer_port_gnmi_counters_heartbeat_under_suppress_redundant(
    engines, devices, gnmi_client, setup_topology, request, setup_name
):
    """
    Peer-port identity leaves must keep being resent while suppress-redundant is on.

    Redmine 5230671 item 1 on the peer-port subtree. The subscription deliberately targets the
    identity leaves rather than the additive counters: gpu-telemetry re-ingests the counters every
    PEER_TELEMETRY_SAMPLING_SEC, which is well under any heartbeat, so a counter is always
    delivered by a change before its heartbeat comes due and the heartbeat is never exercised. The
    identity leaves never change while the peer is present, so suppress-redundant drops every
    sample of them and only the heartbeat can deliver them - which is what makes a dropped
    heartbeat observable here at all.

    Extra coverage layered on the peer-port suite, so an unmet lab prerequisite skips rather than
    fails: this case needs an external HCA cable and a running SM on top of what the surface it
    extends requires.
    """
    _require_external_hca_topology(setup_topology, skip_on_wrong_lab=True)
    _require_active_fabric(engines, request, setup_name, skip_on_wrong_lab=True)
    _enable_peer_port(engines)
    targets = _select_peer_port_targets(gnmi_client, skip_on_wrong_lab=True)
    paths = _peer_port_static_paths(targets)
    window_sec = heartbeat_window_sec(HEARTBEAT_INTERVAL)
    subscription = StreamSubscription(
        paths=paths,
        stream_mode=GnmiStreamMode.SAMPLE,
        sample_interval=HEARTBEAT_SAMPLE_INTERVAL,
        suppress_redundant=True,
        heartbeat_interval=HEARTBEAT_INTERVAL,
        label="peer-port identity leaves",
    )
    ibh.attach_dict(
        "peer-port heartbeat subscription",
        {
            "peers": [pid for _, pid in targets],
            "paths": len(paths),
            "sample_interval": HEARTBEAT_SAMPLE_INTERVAL,
            "heartbeat_interval": HEARTBEAT_INTERVAL,
            "peer_sampling_interval_sec": _peer_port_interval_sec(),
            "window_sec": window_sec,
        },
    )

    with allure.step(
        f"Stream {len(paths)} peer-port identity leaves with suppress-redundant and a "
        f"{HEARTBEAT_INTERVAL} heartbeat for {window_sec}s"
    ):
        results = run_stream_subscribers(
            target_ip=engines.dut.ip,
            subscriptions=[subscription],
            window_sec=window_sec,
            username=gnmi_client.username,
            password=gnmi_client.password,
            engine=get_gnmic_engine(engines),
        )
        verify_subscribers_streamed(results)

    # Expected leaves are the subscribed paths themselves, so the check is per peer and per leaf:
    # a leaf arriving for one peer must not cover for the same leaf going silent on another, and a
    # peer dropping out of the subscription surfaces as all of its leaves never being delivered.
    with allure.step(f"Verify every identity leaf of all {len(targets)} subscribed peer(s) kept being resent"):
        timeline = verify_leaf_heartbeat_continuity(
            results[0].notifications,
            HEARTBEAT_INTERVAL,
            expected_leaf_paths=paths,
            window_sec=window_sec,
            context="peer-port identity leaves",
        )

    with allure.step(f"Verify delivery was driven by the {HEARTBEAT_INTERVAL} heartbeat, not the "
                     f"{HEARTBEAT_SAMPLE_INTERVAL} sample interval"):
        verify_heartbeat_drove_delivery(
            timeline, window_sec, HEARTBEAT_INTERVAL, context="peer-port identity leaves",
        )


@pytest.mark.skip_clear_config
@pytest.mark.gnmi
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(12 * MINUTE, func_only=True)
def test_peer_port_heartbeat_survives_peer_port_disable(
    engines, devices, gnmi_client, setup_topology, request, setup_name
):
    """
    Disabling peer-port mid-subscription must not silence the rest of the subscription.

    Disabling the knob removes the whole peer-port subtree while a subscription is streaming it -
    the bluntest available way to kill leaves under a live RPC, which is the failure shape
    Redmine 5230671 item 1 is about: leaves going silent under a subscription that stays up. The
    switch-side leaves in the same subscription must keep heartbeating throughout, and the RPC
    itself must not abort when part of its path set disappears.

    Extra coverage layered on the peer-port suite, so an unmet lab prerequisite skips rather than
    fails: this case needs an external HCA cable, a running SM and a live switch-side link on top
    of what the surface it extends requires.
    """
    _require_external_hca_topology(setup_topology, skip_on_wrong_lab=True)
    _require_active_fabric(engines, request, setup_name, skip_on_wrong_lab=True)
    _enable_peer_port(engines)
    targets = _select_peer_port_targets(gnmi_client, skip_on_wrong_lab=True)
    switch_port = _require_link_up_port('watch across the peer-port disable')
    switch_paths = [
        f"{GnmiYangPaths.INTERFACE_BY_NAME.format(name=switch_port.name)}/{leaf}"
        for leaf in HEARTBEAT_STATIC_INTERFACE_LEAVES
    ]
    paths = switch_paths + _peer_port_counter_paths(targets)
    window_sec = heartbeat_window_sec(HEARTBEAT_INTERVAL_SHORT, epochs=5)
    subscription = StreamSubscription(
        paths=paths,
        stream_mode=GnmiStreamMode.SAMPLE,
        sample_interval=HEARTBEAT_SAMPLE_INTERVAL,
        suppress_redundant=True,
        heartbeat_interval=HEARTBEAT_INTERVAL_SHORT,
        label="switch-side + peer-port leaves across a peer-port disable",
    )
    ibh.attach_dict(
        "peer-port disable perturbation",
        {
            "switch_port": switch_port.name,
            "switch_paths": len(switch_paths),
            "peer_paths": len(paths) - len(switch_paths),
            "heartbeat_interval": HEARTBEAT_INTERVAL_SHORT,
            "window_sec": window_sec,
            "disable_at_sec": HEARTBEAT_PERTURBATION_AT_SEC,
        },
    )

    def _disable_peer_port():
        System().peer_port.unset(
            op_param=NvuePaths.KEY_STATE, apply=True, ask_for_confirmation='-y',
        ).verify_result()

    with allure.step(
        f"Stream {len(paths)} leaves for {window_sec}s, disabling peer-port after "
        f"{HEARTBEAT_PERTURBATION_AT_SEC}s"
    ):
        try:
            results = run_stream_subscribers(
                target_ip=engines.dut.ip,
                subscriptions=[subscription],
                window_sec=window_sec,
                username=gnmi_client.username,
                password=gnmi_client.password,
                mid_window_action=_disable_peer_port,
                action_at_sec=HEARTBEAT_PERTURBATION_AT_SEC,
                engine=get_gnmic_engine(engines),
            )
        finally:
            with allure.step("Re-enable peer-port"):
                _enable_peer_port(engines)
        verify_subscribers_streamed(results)

    # The peer-port leaves are expected to stop: the feature that produced them was turned off.
    # What must not happen is the switch-side leaves stopping along with them.
    with allure.step(
        f"Verify {switch_port.name}'s leaves kept heartbeating across the peer-port disable"
    ):
        switch_side = [
            notification for notification in results[0].notifications
            if GnmiYangPaths.PEER_PORT_PREFIX not in str(notification.get("prefix", ""))
        ]
        verify_leaf_heartbeat_continuity(
            switch_side,
            HEARTBEAT_INTERVAL_SHORT,
            expected_leaf_paths=list(HEARTBEAT_STATIC_INTERFACE_LEAVES),
            window_sec=window_sec,
            context=f"{switch_port.name} while the peer-port subtree was removed",
        )


# 9.2 test_hca_ingestion_persists_across_reboot (nice-to-have)
@pytest.mark.gnmi
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(17 * MINUTE, func_only=True)
def test_hca_ingestion_persists_across_reboot(engines, devices, gnmi_client, setup_topology, topology_obj,
                                              ib_otel):
    """The peer-port knob survives a reboot and gpu-telemetry re-ingests the HCA rows, exported over OTEL."""
    expected_hca = _require_hca_topology(setup_topology)
    system = System()

    with allure.step("Enable the peer-port knob and save config"):
        # Persistence is scoped to the aggregated tier (matching the rest of the suite).
        ibh.ensure_valid_hca_xcset_backend(engines)
        _enable_peer_port_for_hca(engines, tier=PeerPortFields.TIER_AGGREGATED)
        health, raw = ibh.wait_for_peer_telemetry_healthy(engines)
        ibh.attach_dict("peer telemetry health before reboot", {"classification": health, "raw": raw})
        assert health == PeerTelemetryHealth.HEALTHY, (
            f"PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}) before reboot; "
            "expected healthy so the baseline rows come from a live xcset feed"
        )
        NvueGeneralCli.save_config(engines.dut)

    with allure.step("Baseline aggregated HCA rows before reboot"):
        before = ibh.list_hca_peer_rows(engines, tier=PeerPortFields.TIER_AGGREGATED)
        ibh.attach_dict("hca rows before reboot", before)
        assert before, "No aggregated HCA peer-port rows before reboot (HCA feature build delivered?)"
        missing = sorted(set(before) - set(_otel_hca_rows("before-reboot")))
        assert not missing, f"Aggregated HCA row(s) not exported over OTEL before reboot: {missing!r}"

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

    with allure.step("After boot: OTEL exports the re-ingested HCA rows"):
        # The OTEL export was applied, not saved, so the reboot dropped it.
        ib_otel.invalidate(reconfigure=True)
        missing = sorted(set(before) - set(_otel_hca_rows("after-reboot")))
        assert not missing, f"Re-ingested HCA row(s) not exported over OTEL after reboot: {missing!r}"


# 10.5 test_hca_peerport_behavior_on_peer_down (plan §10.3)
@pytest.mark.gnmi
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(14 * MINUTE, func_only=True)
def test_hca_peerport_behavior_on_peer_down(engines, gnmi_client, setup_topology, ib_otel):
    """A down HCA peer's row disappears within ~10s and reappears with the same Aport on recovery.

    OTEL stops exporting the peer while it is down and exports it again on recovery.
    """
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

        with allure.step(f"OTEL stops exporting {peer_id}"):
            snap = ibo.active_surface().snapshot("peer-down", fresh=True)
            still = snap.current(PEER_PORT_OTEL_NODE_INFO, **{OtelLabel.INTERFACE: peer_id})
            assert not still, (
                f"HCA peer {peer_id} is down but is still exported over OTEL "
                f"({PEER_PORT_OTEL_NODE_INFO}: {[s.labels for s in still]!r})"
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

    with allure.step(f"OTEL exports {peer_id} again with the same Aport"):
        otel_row = _otel_peer_rows("peer-restored").get(peer_id, {})
        assert otel_row, f"HCA peer {peer_id} is not exported over OTEL after recovery"
        assert ibh.peer_entry_aport(otel_row) == ibh.peer_entry_aport(snapshot), (
            f"HCA peer {peer_id} Aport over OTEL after recovery is "
            f"{ibh.peer_entry_aport(otel_row)!r}, was {ibh.peer_entry_aport(snapshot)!r}"
        )


# 10.6 test_hca_xcset_unreachable_resiliency (plan §10.5)
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(17 * MINUTE, func_only=True)
def test_hca_xcset_unreachable_resiliency(engines, devices, setup_topology, ib_otel):
    """NMX-T-for-IB outage: last-good rows retained while the backend is down, refreshed on its return.

    OTEL keeps exporting the retained rows through the outage and the full set after it.
    """
    _require_hca_topology(setup_topology)
    _recycle_peer_port_for_hca(engines)

    with allure.step("Snapshot HCA rows and confirm health is healthy"):
        snapshot = ibh.list_hca_peer_rows(engines)
        assert snapshot, "No HCA peer-port rows in System DB (HCA feature build delivered?)"
        health, raw = ibh.read_peer_telemetry_health(engines)
        ibh.attach_dict("initial health", {"classification": health, "raw": raw})
        assert health == PeerTelemetryHealth.HEALTHY, (
            f"PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}) before the outage; "
            "expected healthy. Confirm the health key/values with dev (HLD §6.1.2)."
        )

    # Negative flow: while NMX-T-for-IB is down the daemon retries the backend and logs the
    # outage; once the failure is deemed persistent it logs "nmx-t backend unrecoverable
    # after ..." (kept at error level by design) and goes on serving the last-good snapshot
    # flagged stale. Suppress LogAnalyzer over the whole deliberate outage window;
    # LogAnalyzer stays active for the pre-outage setup above.
    with loganalyzer_ignore():
        with allure.step("Make NMX-T Lite unreachable"):
            # Mark the outage start so the unrecoverable-declaration search is scoped to this
            # window and can't be satisfied by a stale line from an earlier outage.
            outage_since = ibh.dut_journal_since_marker(engines)
            ibh.make_nmxt_lite_unreachable(engines)

        try:
            with allure.step("While the outage is still transient: last-good rows retained, health degraded"):
                time.sleep(PEER_TELEMETRY_SAMPLING_SEC)
                # Sample rows, health and log before asserting: the three checks are
                # independent facets of the same state, so one failing must not stop the
                # others from being reported.
                during = ibh.list_hca_peer_rows(engines)
                ibh.attach_dict("rows during transient outage", during)
                health, raw = ibh.read_peer_telemetry_health(engines)
                log = ibh.read_gpu_telemetry_log(engines)
                lowered = log.lower()

                with allure.independent_step("Last-good rows are retained while the outage is transient"):
                    # Retention while transient: the row SET must still match the pre-outage snapshot.
                    assert set(during) == set(snapshot), (
                        "HCA rows were added/dropped while the NMX-T Lite outage was still transient; "
                        "the last-good rows must be retained until the failure is deemed persistent. "
                        f"before={sorted(snapshot)!r}, during={sorted(during)!r}"
                    )
                with allure.independent_step("Health is degraded while the outage is transient"):
                    assert health == PeerTelemetryHealth.DEGRADED, (
                        f"PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}) during the outage; expected degraded"
                    )
                with allure.independent_step("The outage is reported in the peer-telemetry log"):
                    assert "unreachable" in lowered or "nmx" in lowered, (
                        "peer-telemetry log shows no NMX-T-for-IB unreachable error during the outage"
                    )

            with allure.step("Once the failure is deemed persistent: last-good rows are still retained"):
                # The daemon gives up restarting the backend but keeps the last-good snapshot
                # rather than blanking the listing, so consumers keep reading data flagged
                # stale. Key off the log wording (robust to the restart-attempt count
                # changing), then give the rows two more cycles to prove they are not drained.
                assert _wait_for_nmxt_unrecoverable(engines, outage_since), (
                    "peer-telemetry never logged 'nmx-t backend unrecoverable after ...' during the "
                    f"outage (waited {NMXT_UNRECOVERABLE_DEADLINE_SEC}s); expected the daemon to declare "
                    "the NMX-T-for-IB failure persistent"
                )
                time.sleep(2 * PEER_TELEMETRY_SAMPLING_SEC)
                # Retention and health are independent facets of the same post-declaration
                # state, so sample both before asserting and report them separately.
                remaining = ibh.list_hca_peer_rows(engines)
                ibh.attach_dict("rows after persistence declared", remaining)
                health, raw = ibh.read_peer_telemetry_health(engines)

                with allure.independent_step("Last-good rows are retained after persistence is declared"):
                    assert set(remaining) == set(snapshot), (
                        "HCA rows changed after the NMX-T-for-IB failure was declared unrecoverable; "
                        "the last-good snapshot must be retained for the whole outage. "
                        f"before={sorted(snapshot)!r}, remaining={sorted(remaining)!r}"
                    )
                with allure.independent_step("Health is still degraded after persistence is declared"):
                    assert health == PeerTelemetryHealth.DEGRADED, (
                        f"PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}) after the failure was "
                        "declared unrecoverable; expected degraded while the backend is still down"
                    )
                with allure.independent_step("OTEL keeps exporting the last-good rows"):
                    otel_rows = _otel_hca_rows("xcset-outage")
                    assert set(otel_rows) == set(snapshot), (
                        "The OTEL HCA peer set changed during the NMX-T-for-IB outage; the last-good "
                        f"rows must stay exported. before={sorted(snapshot)!r}, "
                        f"otel={sorted(otel_rows)!r}"
                    )
        finally:
            with allure.step("Restore NMX-T Lite"):
                ibh.restore_nmxt_lite(engines)
                # `systemctl start` returns before nmx-t-ib accepts connections, and the daemon
                # keeps logging backend connection errors until it does. Stay inside the ignore
                # window until the journal goes quiet, otherwise those tail-end outage errors
                # land on the post-recovery validation and fail LogAnalyzer in teardown.
                backend_quiet = ibh.wait_for_nmxt_backend_reachable(engines)
                if not backend_quiet:
                    ibh.attach_dict("journal tail while still not quiet",
                                    {"peer_telemetry": ibh.read_gpu_telemetry_log(engines)})

    # Verified here rather than in the finally above: raising from a finally would replace a
    # failure raised by the outage checks and hide the real cause. Reaching this line means
    # those checks passed, so a still-noisy backend is the only thing left to report.
    assert backend_quiet, (
        "nmx-t-ib did not come back and stop logging backend connection errors after "
        "restore; the LogAnalyzer ignore window has now closed, so those expected outage "
        "errors will also be attributed to the post-recovery validation"
    )

    # LogAnalyzer is re-enabled here (outside the ignore): recovery is quiet by this point, so
    # any unexpected error log during the post-restore validation is a real regression to catch.
    with allure.step("After recovery: ingestion resumes and health is healthy again"):
        # Wait for the full pre-outage peer set to return, not just any row - a partial
        # re-ingestion would otherwise pass the "restored on return" claim. Size the
        # deadline off the live sampling interval, but never below the constant so a
        # smaller configured interval can't starve the nmx-t-ib restart + rediscovery.
        interval = max(_peer_port_interval_sec(), PEER_TELEMETRY_SAMPLING_SEC)
        resumed, still_missing = ibh.wait_for_hca_peer_rows(
            engines, required=set(snapshot),
            timeout_sec=PEER_PORT_ENABLE_GRACE_SEC + 2 * interval,
        )
        ibh.attach_dict("rows after recovery", resumed)
        # Poll for health rather than a one-shot read: the classification can lag row
        # reappearance and pass through transient states during the daemon's warm-up.
        health, raw = ibh.wait_for_peer_telemetry_healthy(engines)
        assert not still_missing, (
            "HCA rows did not fully resume after NMX-T Lite was restored; "
            f"missing peers={sorted(still_missing)!r}, snapshot={sorted(snapshot)!r}"
        )
        assert health == PeerTelemetryHealth.HEALTHY, (
            f"PEER_PORT_TELEMETRY_HEALTH did not return to healthy after recovery: {health!r} (raw={raw!r})"
        )

    with allure.step("After recovery: OTEL exports the full pre-outage peer set"):
        missing = sorted(set(snapshot) - set(_otel_hca_rows("xcset-restored")))
        assert not missing, f"HCA row(s) not exported over OTEL after NMX-T Lite was restored: {missing!r}"


# 10.7 test_hca_xcset_malformed_csv_resiliency (plan §10.6)
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(17 * MINUTE, func_only=True)
def test_hca_xcset_malformed_csv_resiliency(engines, devices, setup_topology, ib_otel):
    """On a malformed xcset: rows frozen (no partial writes), parse error logged, health -> degraded; recovers on a good feed.

    OTEL keeps exporting the frozen rows with their frozen counters.
    """
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
        assert health == PeerTelemetryHealth.HEALTHY, (
            f"PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}) before injection; expected healthy"
        )
        parse_fail_baseline = _count_xcset_parse_failures(engines)

    # Negative flow: the malformed feed makes the daemon log expected parse-failure
    # warnings (Redmine 5159311). Ignore them for LogAnalyzer over the deliberate
    # malformed-feed window; LogAnalyzer stays active for the pre-injection setup above.
    with loganalyzer_ignore():
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

            with allure.step("During the malformed feed: OTEL exports the frozen rows and counters"):
                snap = ibo.active_surface().snapshot("xcset-malformed", fresh=True)
                otel_rows = ibh.filter_peer_entries_by_type(ibh.otel_peer_port_rows(snap), PeerType.HCA)
                assert set(otel_rows) == set(frozen_a), (
                    "The OTEL HCA peer set differs from the frozen DB rows during the malformed feed: "
                    f"db={sorted(frozen_a)!r}, otel={sorted(otel_rows)!r}"
                )
                problems = []
                for pid, row in sorted(frozen_a.items()):
                    fields = [f for f in PEER_PORT_DB_ADDITIVE_FIELDS if f in row]
                    problems += ibo.db_parity_problems(snap, pid, row, frozen_b.get(pid, {}),
                                                       PEER_PORT_OTEL_METRIC_BY_DB_FIELD, fields)
                assert not problems, (
                    "OTEL counters moved away from the frozen DB rows during the malformed feed:\n  " +
                    "\n  ".join(problems)
                )

            with allure.step("Parse failure is logged"):
                log = ibh.read_gpu_telemetry_log(engines)
                assert any(tok in log.lower() for tok in ("malformed", "parse", "invalid")), (
                    "peer-telemetry log shows no parse-failure error for the malformed xcset"
                )

            with allure.step("Health reflects the parse failure (FS §6.1.2: update health state -> degraded)"):
                health, raw = ibh.read_peer_telemetry_health(engines)
                assert health == PeerTelemetryHealth.DEGRADED, (
                    f"PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}) during the malformed "
                    "feed; FS §6.1.2 requires the daemon to update health state to degraded."
                )
        finally:
            with allure.step("Restore a valid HCA xcset"):
                ibh.restore_valid_hca_xcset(engines)

    # LogAnalyzer is re-enabled here (outside the ignore): recovery should be clean, so any
    # unexpected error log during the post-restore validation is a real regression to catch.
    with allure.step("After recovery: ingestion resumes and health is healthy again"):
        time.sleep(PEER_TELEMETRY_SAMPLING_SEC)
        during = ibh.list_hca_peer_rows(engines)
        assert during, "HCA rows did not resume after a valid xcset was restored"
        # Poll for health rather than a one-shot read: the classification can lag row
        # reappearance and pass through transient states during the daemon's warm-up.
        health, raw = ibh.wait_for_peer_telemetry_healthy(engines)
        assert health == PeerTelemetryHealth.HEALTHY, (
            f"PEER_PORT_TELEMETRY_HEALTH did not return to healthy after recovery: {health!r} (raw={raw!r})"
        )


# 10.8 test_hca_ingestion_recovers_after_gpu_telemetry_restart (nice-to-have)
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.timeout(14 * MINUTE, func_only=True)
def test_hca_ingestion_recovers_after_gpu_telemetry_restart(engines, devices, setup_topology, ib_otel):
    """A gpu-telemetry restart loses no HCA ingestion: rows repopulate, counters do not reset, health -> healthy.

    The same holds for the OTEL export.
    """
    _require_hca_topology(setup_topology)
    _enable_peer_port_for_hca(engines)

    with allure.step("Baseline HCA rows before the restart"):
        before = ibh.list_hca_peer_rows(engines)
        ibh.attach_dict("hca rows before restart", before)
        assert before, "No HCA peer-port rows before restart (HCA feature build delivered?)"
        otel_before = _otel_hca_rows("before-restart")

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
        assert health == PeerTelemetryHealth.HEALTHY, (
            f"PEER_PORT_TELEMETRY_HEALTH is {health!r} (raw={raw!r}) after gpu-telemetry restart; expected healthy"
        )

    with allure.step("OTEL exports every row again, with counters that did not reset"):
        otel_after = _otel_hca_rows("after-restart")
        missing = sorted(set(before) - set(otel_after))
        assert not missing, f"HCA row(s) not exported over OTEL after gpu-telemetry restart: {missing!r}"
        for pid in sorted(set(otel_before) & set(otel_after)):
            ibh.assert_counters_monotonic(
                otel_before[pid], otel_after[pid], list(PEER_PORT_OTEL_ADDITIVE_LEAVES),
                label=f"HCA {pid} over OTEL across gpu-telemetry restart",
            )
