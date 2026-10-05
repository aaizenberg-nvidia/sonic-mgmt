"""Helpers for the gNMI-for-IB plane-port, peer-port (HCA), and extended-telemetry tests."""

import ast
import base64
import csv
import io
import json
import logging
import re
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, FrozenSet, Generator, List, Optional, Tuple

import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.cli_wrappers.nvue.nvue_system_clis import NvueSystemCli
from ngts.cli_wrappers.openapi.openapi_system_clis import OpenApiSystemCli
from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import ApiType, DatabaseConst, PlatformConsts, UfmMadConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.IbInterfaceTool import IbInterfaceTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RegisterTool import RegisterTool
from ngts.nvos_tools.infra.ResultObj import IssueType, ResultObj
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.constants import GnmiMode, GnmicErr
from ngts.tests_nvos.system.gnmi.helpers import (
    get_infiniband_name_from_port_name,
    get_port_oid_from_infiniband_port,
    is_gnmi_failure,
    is_gnmi_unavailable,
    parse_gnmi_output,
    verify_msg_not_in_out_or_err,
)
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_general_utils import loganalyzer_ignore

from ngts.tests_nvos.general.telemetry.ib.constants import (
    ALL_APIS,
    API_GNMIC,
    API_NVUE_CLI,
    API_OTEL,
    ASIC_COMPONENT_TYPE,
    BER_COMPONENT_LEAF_MARKERS,
    BER_DB_FIELD_MARKER,
    BER_INJECT_SETTLE_SEC,
    BER_RATIO_CEILING,
    BER_RAW_COMPONENT_MARKERS,
    COMPONENT_REF_POLL_SEC,
    COMPONENT_REF_WAIT_SEC,
    CONCAT_DELIMITER,
    COUNTER_SNAPSHOT_SETTLE_SEC,
    COUNTERMGRD_MAX_COUNTERS,
    COUNTERMGRD_SUM_COUNTERS,
    CounterMgrdRule,
    DECIMAL_COMPARE_EPSILON,
    EXPECTED_PLANE_PORT_DB_FIELDS,
    EXTENDED_COUNTERS,
    GNMI_REBOOT_READY_DELAY_SEC,
    GNMI_REBOOT_READY_TRIES,
    GnmiTypeKind,
    GnmiYangPaths,
    IB_OTEL_CURRENT_SAMPLE_SLACK_SEC,
    IfaceType,
    LINK_RECOVERY_POLL_SEC,
    LINK_RECOVERY_TIMEOUT_SEC,
    LINK_STATE_RECOVERY_LEAVES,
    LOCAL_PORT_OTEL_LINK_DOWN_INFO,
    LOCAL_PORT_OTEL_METRIC_BY_DB_FIELD,
    LOCAL_PORT_OTEL_TYPE,
    LocalPortFamily,
    LocalPortNvueSub,
    NMXT_CONNECTION_ERROR_TOKEN,
    NMXT_CONNECTOR_SOCKET,
    NMXT_CONTROL_SOCKET,
    NMXT_IB_CONTAINER,
    NMXT_IB_SERVICE,
    NMXT_XCSET_ENDPOINT,
    NMXT_XCSET_SOCKET,
    NMXT_XCSET_TO_DB_FIELD,
    NvueShowNode,
    NvuePaths,
    OPTICAL_POWER_DBM_TOLERANCE,
    OPTICAL_POWER_DBM_UNIT,
    OPTICAL_POWER_MW_UNIT,
    OTEL_LINK_DOWN_LOCAL_REASON_LABEL,
    OtelGroup,
    OtelLabel,
    PEER_ASIC_HOST_NAME_CANDIDATES,
    PEER_PLATFORM_CLI_LEAF_NAMES,
    PEER_PLATFORM_CLI_POWER_CONTAINERS,
    PEER_PLATFORM_CLI_POWER_LEAF,
    PEER_PLATFORM_CLI_UNIT_LEAVES,
    PEER_PLATFORM_RESOURCE_PATH,
    PEER_PORT_BER_MAX_RATIO,
    PEER_PORT_BER_ZERO_FLOOR,
    PEER_PORT_COUNTERS_LINK_TO_DB_FIELD,
    PEER_PORT_COUNTERS_TO_DB_FIELD,
    PEER_PORT_DIRECTION_NA,
    PEER_PORT_HCA_OMITTED_LEAVES,
    PEER_PORT_HISTOGRAM_BUCKET_COUNT,
    PEER_PORT_HISTOGRAM_DB_FMT,
    PEER_PORT_HISTOGRAM_LEAF,
    PEER_PORT_LANE_COUNT,
    PEER_PORT_LANE_LEAF_SPECS,
    PEER_PORT_LANE_LEAVES,
    PEER_PORT_LANES_DB_FIELD,
    PEER_PORT_OTEL_ADDITIVE_LEAVES,
    PEER_PORT_OTEL_LANE_LEAVES,
    PEER_PORT_OTEL_LEAVES_BY_NVUE_SUB,
    PEER_PORT_OTEL_METRIC_BY_DB_FIELD,
    PEER_PORT_OTEL_NODE_INFO,
    PEER_PORT_OTEL_PLR_BW_LOSS,
    PEER_PORT_OTEL_PLR_CODES_LOSS,
    PEER_PORT_OTEL_RS_HISTOGRAM,
    PEER_PORT_OTEL_UNEXPORTED_NVUE_SUBS,
    PEER_PORT_PHY_DETAIL_TO_DB_FIELD,
    PEER_PORT_PHY_HEALTH_BER_TO_DB_FIELD,
    PEER_PORT_PHY_HEALTH_TO_DB_FIELD,
    PEER_PORT_PLANE_ROOT_PENDING_LEAVES,
    PEER_PORT_PLR_BW_LOSS_DIVISOR,
    PEER_PORT_PLR_BW_LOSS_LEAF,
    PEER_PORT_PLR_CODES_LOSS_LEAF,
    PEER_PORT_NVUE_ROOT_EXTENDED_LEAVES,
    PEER_PORT_SPEED_DB_FIELD,
    PEER_PORT_SPEED_LEAF,
    PEER_PORT_SPEED_MBPS_PER_GBPS,
    PEER_PORT_WIDTH_LEAF,
    PEER_TELEMETRY_SAMPLING_SEC,
    PEER_TELEMETRY_SERVICE,
    PEER_TYPE_NODE_INFO_FIELDS,
    PeerComponentNaming,
    PeerComponentXcsetColumn,
    PeerPlatformKind,
    PeerPlatformNode,
    PeerPortComponentRef,
    PeerPortContainer,
    PeerPortDbPair,
    PeerPortFields,
    PeerPortNvueSub,
    PeerPortTargetRole,
    PeerTelemetryHealth,
    PeerType,
    PlanePortState,
    PLANEPORT_SUM_AGGREGATION_MIN_DELTA,
    SAI_TO_GNMI_MAX_COUNTER_LEAF,
    SAI_TO_GNMI_STATE_COUNTER_LEAF,
    SAI_TO_NVUE_COUNTER_LEAF,
    SAI_TO_NVUE_MAX_COUNTER_LEAF,
    SAMPLING_JITTER_TOLERANCE_PCT,
    SystemDbCli,
    TRANSCEIVER_DETAIL_VIEW,
    XCSET_EXTENDED_COLUMN_MARKERS,
)
from ngts.tests_nvos.general.telemetry.ib import otel_helpers as ibo

logger = logging.getLogger(__name__)


# ============================================================================
# Lab prerequisites
# ============================================================================


def report_wrong_lab(message: str, skip: bool) -> None:
    """
    Fail on an absent lab prerequisite, or skip when the caller is non-gating extra coverage.

    The peer-port and plane-port suites proper fail: a missing HCA, Subnet Manager or plane-port
    there means the feature under test was never exercised, so a skip would green the run. The
    gNMI subscribe-heartbeat cases layer extra coverage on top of those suites and skip instead,
    because they need a fuller lab (external HCA cables, an SM, live planes) than the surface they
    extend and are not meant to hold back a setup that cannot host them.
    """
    if skip:
        pytest.skip(f"{message} Skipped rather than failed: this case is additional gNMI "
                    f"subscribe-heartbeat coverage and is not mandatory on a setup that cannot "
                    f"host it.")
    pytest.fail(message)


# ============================================================================
# Plane-port knob configuration (NVUE)
# ============================================================================


def _assert_plane_port_operation_ok(result: ResultObj, operation: str) -> None:
    """Raise when the plane-port NVUE resource is missing or the call failed."""
    if result.result:
        return
    detail = result.info or "<no details>"
    raise AssertionError(
        f"Plane-port feature is not available on this build - {operation} failed "
        "(load a plane-port-capable build before running this suite).\n"
        f"DUT response:\n{detail}"
    )


def _coerce_plane_port_state(state) -> PlanePortState:
    if isinstance(state, PlanePortState):
        return state
    try:
        return PlanePortState(str(state).lower())
    except ValueError as exc:
        raise ValueError(f"Unsupported plane-port state: {state!r}") from exc


def set_plane_port_state(engines, state, apply: bool = True, save: bool = False) -> None:
    """Configure plane-port state via ``System().plane_port`` (optionally apply/save)."""
    knob_state = _coerce_plane_port_state(state)
    with allure.step(f"Set plane-port state to {knob_state.value!r}"):
        result = System().plane_port.set(
            op_param_name=NvuePaths.KEY_STATE,
            op_param_value=knob_state.value,
            apply=apply,
            ask_for_confirmation="-y" if apply else False,
            dut_engine=engines.dut,
        )
        _assert_plane_port_operation_ok(result, "set plane-port state")
    if apply:
        time.sleep(GnmiConsts.CONFIG_SETTLE_TIME_SEC)
    if save:
        with allure.step("Save config"):
            NvueGeneralCli.save_config(engines.dut)


def unset_plane_port_state(engines, apply: bool = True) -> None:
    """Unset plane-port state (returns to default = disabled) via ``System().plane_port``."""
    with allure.step("Unset plane-port state"):
        result = System().plane_port.unset(
            op_param=NvuePaths.KEY_STATE,
            apply=apply,
            ask_for_confirmation="-y" if apply else False,
            dut_engine=engines.dut,
        )
        _assert_plane_port_operation_ok(result, "unset plane-port state")
    if apply:
        time.sleep(GnmiConsts.CONFIG_SETTLE_TIME_SEC)


def get_plane_port_state(engines) -> str:
    """Return the plane-port ``state`` leaf ('enabled' / 'disabled')."""
    with allure.step("Read plane-port state via NVUE"):
        # Fetch the ResultObj (not the parsed dict) so a missing/failed
        # plane-port resource is reported via _assert_plane_port_operation_ok
        # with a helpful message instead of raising deep inside the parser.
        result = System().plane_port.show(
            dut_engine=engines.dut, should_succeed=False, if_returned_value=False
        )
        _assert_plane_port_operation_ok(result, "nv show system plane-port")
        parsed = OutputParsingTool.parse_json_str_to_dictionary(
            result.get_returned_value()
        ).get_returned_value()
        return str(parsed.get(NvuePaths.KEY_STATE, "")).lower()


# ============================================================================
# gNMI read
# ============================================================================


def _assert_gnmi_ok(out: str, err: str, context: str) -> None:
    """Raise AssertionError if a gnmic call failed (checks both stderr and stdout)."""
    # Explicit raise (not a bare assert, which python -O can strip). gnmic reports
    # most failures on stderr, but some surface as a leading "error:" on stdout.
    out_starts_with_error = (out or "").strip().lower().startswith("error")
    if is_gnmi_failure(err) or out_starts_with_error:
        raise AssertionError(
            f"gnmic call failed ({context}):\n  stderr: {err}\n  stdout: {out}"
        )


def gnmi_get_interface_subtree(client: GnmiClient, name: str) -> Dict[str, str]:
    """
    Full interface subtree under `interfaces/interface[name=<name>]`, returned
    as a flat {leaf: value} dict.
    """
    out, err = client.gnmic_subscribe_interface(
        mode=GnmiMode.ONCE,
        interface_name=name,
        skip_cert_verify=True,
        wait_till_done=True,
        interface_path="",
    )
    verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, out, err)
    _assert_gnmi_ok(out, err, f"interface subtree for {name!r}")
    return parse_gnmi_output(out)


def wait_link_state_recovered(client, ports, pre, *,
                              timeout_sec=LINK_RECOVERY_TIMEOUT_SEC,
                              poll_sec=LINK_RECOVERY_POLL_SEC):
    """Poll each port until its LINK_STATE_RECOVERY_LEAVES match the ``pre`` snapshot.

    Returns ``(recovered, port_name, leaf, pre_value, post_value)``: on success
    ``recovered`` is True and the rest are None; otherwise they describe the
    first leaf that never returned to its pre-value before the timeout.
    """
    # A re-enabled / plugged-back IB link transits PORT_CONFIGURATION_TRAINING
    # before LINK_UP, so a single immediate read races the retrain - poll instead.
    deadline = time.time() + timeout_sec
    while True:
        mismatch = None
        for port in ports:
            payload = gnmi_get_interface_subtree(client, port.name)
            pre_payload = pre[port.name]
            for leaf_name in LINK_STATE_RECOVERY_LEAVES:
                if leaf_name in pre_payload and leaf_name in payload:
                    if payload[leaf_name] != pre_payload[leaf_name]:
                        mismatch = (False, port.name, leaf_name,
                                    pre_payload[leaf_name], payload[leaf_name])
                        break
            if mismatch:
                break
        if mismatch is None:
            return (True, None, None, None, None)
        if time.time() >= deadline:
            return mismatch
        time.sleep(poll_sec)


def gnmi_get_flat(client: GnmiClient, prefix: str, path: str = "") -> Dict[str, str]:
    """
    Subscribe ONCE to (prefix, path) in flat mode and return the parsed
    {leaf_name: value} dict.
    """
    out, err, _, _ = client.gnmic_subscribe(
        prefix=prefix,
        path=path,
        mode=GnmiMode.ONCE,
        flat=True,
        skip_cert_verify=True,
        wait_till_done=True,
    )
    verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, out, err)
    _assert_gnmi_ok(out, err, f"subscribe prefix={prefix!r} path={path!r}")
    return parse_gnmi_output(out)


def gnmi_get_raw_lines(client: GnmiClient, prefix: str, path: str = "") -> List[str]:
    """
    Return the non-empty lines of a gnmic flat subscribe output. Useful when
    parse_gnmi_output() collapses path information we still need (e.g. for
    "leaf is absent under prefix" checks).
    """
    out, err, _, _ = client.gnmic_subscribe(
        prefix=prefix,
        path=path,
        mode=GnmiMode.ONCE,
        flat=True,
        skip_cert_verify=True,
        wait_till_done=True,
    )
    verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, out, err)
    _assert_gnmi_ok(out, err, f"subscribe prefix={prefix!r} path={path!r}")
    return [line for line in out.splitlines() if line.strip()]


# One `[key=value]` element of a gNMI list path.
_GNMI_LIST_KEY_RE = re.compile(r"\[[^\]=]+=([^\]]+)\]")


def _split_gnmic_flat_line(line: str) -> Optional[Tuple[str, str]]:
    """Split one gnmic flat line into ``(path, value)``, or None when it carries no separator.

    Different gnmic versions separate the two with ``: ``, ``=`` or a bare ``:``. The bare
    colon is tried last so a value that itself contains one is not split on it. A separator
    inside a list predicate such as ``[name=acp1]`` belongs to the path, so only one outside
    square brackets splits the line.
    """
    for separator in (": ", "=", ":"):
        index = _index_outside_brackets(line, separator)
        if index >= 0:
            return line[:index].strip(), line[index + len(separator):].strip()
    return None


def _index_outside_brackets(text: str, separator: str) -> int:
    """Index of the first ``separator`` that is not inside ``[...]``, or -1."""
    depth = 0
    for index, char in enumerate(text):
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        elif depth == 0 and text.startswith(separator, index):
            return index
    return -1


def relative_leaf_path(path: str, prefix: str) -> str:
    """A leaf's path relative to `prefix`, keeping the containers between the two.

    Falls back to the bare leaf name when the prefix does not appear in the path, so a
    rendering difference degrades to the `gnmi_get_flat` behavior rather than dropping the leaf.
    """
    head = prefix.strip("/")
    text = path.strip("/")
    if head and head in text:
        return text.split(head, 1)[1].strip("/")
    return text.rsplit("/", 1)[-1]


def gnmi_get_flat_relative(client: GnmiClient, prefix: str, path: str = "") -> Dict[str, str]:
    """Flat read of (prefix, path) keyed by each leaf's path RELATIVE to `prefix`.

    `gnmi_get_flat` keys by the last path segment alone, which collapses distinct readings
    that end in the same name - ``media-snr/instant`` and ``host-snr/instant`` would both
    arrive as ``instant``. Use this wherever a container nests its samples.
    """
    lines = gnmi_get_raw_lines(client, prefix=prefix, path=path)
    result: Dict[str, str] = {}
    unparseable: List[str] = []
    for line in lines:
        parsed = _split_gnmic_flat_line(line)
        if parsed is None:
            unparseable.append(line)
            continue
        leaf_path, value = parsed
        result[relative_leaf_path(leaf_path, prefix)] = value
    if unparseable:
        # Logged rather than raised: gnmic can emit banner//progress lines that carry no leaf.
        # A dropped leaf would otherwise resurface later as a confusing "missing leaf".
        logger.warning("Ignored %d unparseable gnmic line(s) under %s: %r",
                       len(unparseable), prefix, unparseable)
    if lines and not result:
        raise AssertionError(
            f"gnmic returned {len(lines)} line(s) under {prefix!r} but none carried a leaf "
            f"value; output was: {lines!r}"
        )
    return result


def parse_counter_value(raw: str) -> int:
    """Strip stringification artifacts and coerce a gNMI counter leaf to int."""
    if raw is None:
        raise AssertionError("Counter raw value is None")
    cleaned = str(raw).strip().strip('"').strip()
    if cleaned == "":
        raise AssertionError("Counter raw value is empty")
    try:
        return int(cleaned)
    except ValueError:
        # Scientific notation (e.g. 2.634624e+06): Decimal, not float, to keep
        # integer precision past 2**53.
        try:
            return int(Decimal(cleaned))
        except (InvalidOperation, ValueError) as exc:
            raise AssertionError(
                f"Counter raw value {raw!r} is not a valid integer/decimal: {exc}"
            ) from exc


# ============================================================================
# NVUE read
# ============================================================================


def nvue_show_interface_json(engines, name: str) -> Dict:
    """Return `nv show interface <name>` as a dict via the framework wrapper."""
    raw = Port.show_interface(dut_engine=engines.dut, port_names=name)
    # Empty output means the command produced nothing (e.g. the interface is not
    # currently shown); surface that with the interface name instead of letting
    # the JSON parser raise a context-free "Expecting value" error.
    if not raw or not raw.strip():
        raise AssertionError(
            f"NVUE 'nv show interface {name}' returned empty output - the interface "
            "is not currently shown (hidden/absent), not a parseable JSON payload."
        )
    return OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()


def nvue_show_all_interfaces_json(engines) -> Dict:
    """Return `nv show interface` (all interfaces) as a dict via the framework wrapper."""
    raw = Port.show_interface(dut_engine=engines.dut)
    return OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
        raw,
        require_non_empty=True,
    ).get_returned_value()


# ============================================================================
# Cross-API interface enumeration
# ============================================================================


def otel_interface_types(snap: ibo.OtelSnapshot) -> Dict[str, str]:
    """``{interface: type}`` from the current `nvswitch_interface_type` series.

    An interface exported under more than one type at once renders as their comma-joined list,
    so the caller's type comparison reports it instead of picking one.
    """
    types: Dict[str, set] = {}
    for series in snap.current(LOCAL_PORT_OTEL_TYPE):
        name = series.labels.get(OtelLabel.INTERFACE)
        if name:
            types.setdefault(name, set()).add(series.labels.get(OtelLabel.TYPE, ""))
    return {name: ",".join(sorted(values)) for name, values in types.items()}


def otel_interface_metric_names(snap: ibo.OtelSnapshot, name: str) -> set:
    """Every metric with at least one current series of ``name``."""
    return {metric for metric in snap.names
            if snap.current(metric, **{OtelLabel.INTERFACE: name})}


def otel_interface_metrics(snap: ibo.OtelSnapshot, name: str) -> Dict[str, Any]:
    """``{metric: latest value}`` of every current single-series metric of ``name``."""
    out: Dict[str, Any] = {}
    for metric in sorted(snap.names):
        series = snap.current(metric, **{OtelLabel.INTERFACE: name})
        if len(series) == 1:
            out[metric] = series[0].latest
    return out


def list_interfaces_via_api(api: str, engines, gnmi_client: GnmiClient) -> Dict[str, str]:
    """
    Enumerate interface names + their type leaf across APIs.

    Returns {interface_name: type_string} so the caller can filter by
    Aport vs plane-port without re-implementing parsing per API.

    The `type` for gnmic is the gNMI `state/type` leaf (e.g. "infiniband",
    "infiniband-plane-port"); for NVUE it is the JSON top-level "type" field
    (e.g. "ib", "ibpp"); for OTEL it is the `type` label of `nvswitch_interface_type`,
    read from a fresh collection window.
    """
    assert api in ALL_APIS, f"Unsupported api: {api!r}"

    if api == API_NVUE_CLI:
        body = nvue_show_all_interfaces_json(engines)
        return {name: str(b.get("type", "")) for name, b in (body or {}).items() if isinstance(b, dict)}

    if api == API_GNMIC:
        lines = gnmi_get_raw_lines(gnmi_client, GnmiYangPaths.INTERFACES, path="state/type")
        result: Dict[str, str] = {}
        for line in lines:
            match = re.search(r"interface\[name=([^\]]+)\][^:]*:\s*(.+)", line)
            if match:
                result[match.group(1)] = match.group(2).strip()
        return result

    if api == API_OTEL:
        return otel_interface_types(ibo.active_surface().snapshot("interface-types", fresh=True))

    raise ValueError(api)


def expected_type_leaf(api: str, is_plane: bool) -> str:
    """Return the expected type-leaf value for a given API surface."""
    if api == API_NVUE_CLI:
        return IfaceType.NVUE_PLANE if is_plane else IfaceType.NVUE_APORT
    if api == API_GNMIC:
        return IfaceType.GNMI_PLANE if is_plane else IfaceType.GNMI_APORT
    if api == API_OTEL:
        return IfaceType.OTEL_PLANE if is_plane else IfaceType.OTEL_APORT
    raise ValueError(api)


def attach_dict(name: str, payload: Dict) -> None:
    """Attach a dict to Allure as JSON for post-mortem inspection."""
    try:
        allure.attach(name, json.dumps(payload, indent=2, default=str), allure.orig_allure.attachment_type.JSON)
    except Exception:  # noqa: BLE001
        # The allure wrapper signature varies across versions; fall back to plain text.
        allure.attach(name, json.dumps(payload, indent=2, default=str))


# ============================================================================
# Port-name classification / Aport enumeration
# ============================================================================


def is_plane_port_name(name: str) -> bool:
    """Return True if `name` matches the plane-port suffix convention (`...plN`)."""
    return bool(re.search(r"pl\d+$", name))


def connectivity_label_to_nvue(label: str) -> str:
    """Map an ibdiagnet aggregated label to its live NVUE interface name.

    ibdiagnet enumerates a switch cage's IB port from 0 (e.g. ``sw122p0``)
    while NVUE labels the same port as port 1 (``sw122p1``); the aport number
    is identical. Convert a trailing ``p0`` to ``p1`` for switch labels; HCA
    labels (``ibB...``) and already-``p1`` labels are returned unchanged.
    """
    if not label or not label.startswith("sw"):
        return label
    return re.sub(r"p0$", "p1", label)


def filter_aport_names(names: List[str]) -> List[str]:
    """Return only IB Aport names (sw...p..., excluding plane-ports and mgmt)."""
    out = []
    for n in names:
        if not n:
            continue
        if is_plane_port_name(n):
            continue
        if not re.match(r"^sw[A-Za-z]?\d+p\d+$", n):
            continue
        out.append(n)
    return out


def filter_fabric_port_names(names: List[str]) -> List[str]:
    """Return planarized FNM fabric-manager port names (e.g. ``fnm1``).

    Only the primary fabric-node-manager port (``fnm<N>``) is split into
    ``fnm<N>plK`` plane-port rows in COUNTERS_DB; per-ASIC fabric ports
    (``fnmaXpY``) and plane-ports themselves are excluded.
    """
    out = []
    for n in names:
        if not n:
            continue
        if is_plane_port_name(n):
            continue
        if not re.match(r"^fnm\d+$", n, re.IGNORECASE):
            continue
        out.append(n)
    return out


# ============================================================================
# Host / engine resolution (multi-switch topologies)
# ============================================================================


def dut_hostname(topology_obj, devices) -> str:
    """Best-effort DUT hostname from NOGA attributes."""
    try:
        return str(
            topology_obj.players["dut"]["attributes"]
            .noga_query_data["attributes"]["Common"]["Name"]
        )
    except (AttributeError, KeyError, TypeError):
        return str(getattr(devices.dut, "hostname", "") or "")


def is_gnmi_server_unavailable(err: Optional[str]) -> bool:
    """True when the DUT gNMI server is down/unreachable (skip, do not false-pass)."""
    return is_gnmi_unavailable(err)


def wait_for_gnmi_reachable(
    gnmi_client: GnmiClient,
    tries: int = GNMI_REBOOT_READY_TRIES,
    delay: int = GNMI_REBOOT_READY_DELAY_SEC,
) -> bool:
    """Wait (bounded) for the DUT gNMI server to accept a Capabilities request.

    nv-gnmi can still be starting right after a reboot, so this polls gnmic
    Capabilities via the shared ``ValidationTool.retry_until_valid`` retry loop.
    Returns True once the server is reachable (Capabilities succeeded, or failed
    with a non-"server down" error that the caller's own gNMI check will surface),
    or False if it stayed unavailable for the whole window.
    """
    def _probe() -> None:
        _, err = gnmi_client.gnmic_capabilities(skip_cert_verify=True)
        if is_gnmi_server_unavailable(err):
            raise AssertionError(f"gNMI server still unavailable: {(err or '').strip()}")

    try:
        Tools.ValidationTool.retry_until_valid(
            _probe,
            tries=tries,
            delay=delay,
            description=f"Wait up to ~{tries * delay}s for gNMI to be reachable",
        )
        return True
    except Exception:  # noqa: BLE001 - exhausted wait means still unavailable
        return False


# ============================================================================
# Counter snapshots per API (gNMI / NVUE / OTEL)
# ============================================================================


def _sai_snapshot_from_gnmi_sum_payload(
    payload: Dict[str, str],
    sai_fields: List[str],
) -> Dict[str, int]:
    """Map a flat gNMI ``state/counters`` subscribe to {SAI_PORT_STAT_*: int}."""
    out: Dict[str, int] = {}
    for sai_field in sai_fields:
        leaf = SAI_TO_GNMI_STATE_COUNTER_LEAF.get(sai_field)
        if leaf is None:
            continue
        raw = payload.get(leaf)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            out[sai_field] = parse_counter_value(raw)
        except (AssertionError, ValueError):
            continue
    return out


def _sai_snapshot_from_gnmi_max_payload(payload: Dict[str, str]) -> Dict[str, int]:
    """Map a flat gNMI infiniband/port counters subscribe to MAX SAI fields."""
    out: Dict[str, int] = {}
    for sai_field in COUNTERMGRD_MAX_COUNTERS:
        leaf = SAI_TO_GNMI_MAX_COUNTER_LEAF.get(sai_field)
        if leaf is None:
            continue
        raw = payload.get(leaf)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            out[sai_field] = parse_counter_value(raw)
        except (AssertionError, ValueError):
            continue
    return out


_GNMI_IFACE_NAME_RE = re.compile(r"interface\[name=([^\]]+)\]")


def _parse_gnmi_flat_per_interface(out: str, path_contains: str) -> Dict[str, Dict[str, str]]:
    """Parse a wildcard flat subscribe into {interface_name: {leaf: value}}."""
    per_port: Dict[str, Dict[str, str]] = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or ": " not in line:
            continue
        path, _, value = line.partition(": ")
        if path_contains not in path:
            continue
        match = _GNMI_IFACE_NAME_RE.search(path)
        if not match:
            continue
        name = match.group(1)
        leaf = path.split("/")[-1]
        per_port.setdefault(name, {})[leaf] = value
    return per_port


def capture_gnmi_aggregation_snapshot_window(
    client: GnmiClient,
    aport_name: str,
    plane_ports: List[Port],
    sai_fields: Optional[List[str]] = None,
) -> Dict[str, Dict[str, int]]:
    """
    Read Aport + plane-port counters in one wildcard ``subscribe --mode once``.

    A single snapshot over ``interface[name=*]`` keeps every port on the same
    sampling instant, then the Aport + plane names are filtered out (section 6.3).
    """
    fields = sai_fields if sai_fields is not None else COUNTERMGRD_SUM_COUNTERS
    port_names = [aport_name] + [p.name for p in plane_ports]
    wildcard = GnmiYangPaths.INTERFACE_BY_NAME.format(name="*")
    sum_path = f"{wildcard}/state/counters"
    max_path = f"{wildcard}/infiniband/state/counters/port"
    with allure.step(
        f"Read gNMI counters for {aport_name} + {len(plane_ports)} plane-port(s) "
        "in one subscribe-once snapshot"
    ):
        out, err, _, _ = client.gnmic_subscribe_multi_path(
            prefix="",
            paths=[sum_path, max_path],
            mode=GnmiMode.ONCE,
            flat=True,
            skip_cert_verify=True,
            wait_till_done=True,
        )
        verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, out, err)
        _assert_gnmi_ok(out, err, f"wildcard counter subscribe for {aport_name!r}")
        sum_per_port = _parse_gnmi_flat_per_interface(out, "]/state/counters/")
        max_per_port = _parse_gnmi_flat_per_interface(out, "/infiniband/state/counters/port/")
        readings: Dict[str, Dict[str, int]] = {}
        for name in port_names:
            snap = _sai_snapshot_from_gnmi_sum_payload(sum_per_port.get(name, {}), fields)
            snap.update(_sai_snapshot_from_gnmi_max_payload(max_per_port.get(name, {})))
            readings[name] = snap
        return readings


def _nvue_fetch_interface_counters_json(engines, name: str) -> Dict:
    """Run `nv show interface <name> counters --output json` (no Allure step)."""
    cmd = f"nv show interface {name} counters --output json"
    raw = engines.dut.run_cmd(cmd)
    parsed = OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()
    return parsed if isinstance(parsed, dict) else {}


def _sai_snapshot_from_nvue_counters(
    counters: Optional[Dict],
    sai_fields: Optional[List[str]] = None,
) -> Dict[str, int]:
    """Map NVUE ``counters`` JSON to {SAI_PORT_STAT_*: int}."""
    fields = sai_fields if sai_fields is not None else COUNTERMGRD_SUM_COUNTERS
    if not isinstance(counters, dict):
        return {}
    out: Dict[str, int] = {}
    for sai_field in fields:
        leaf = SAI_TO_NVUE_COUNTER_LEAF.get(sai_field)
        if leaf is None:
            continue
        raw = counters.get(leaf)
        if raw is None or str(raw).strip().lower() in ("", "n/a"):
            continue
        try:
            out[sai_field] = parse_counter_value(raw)
        except (AssertionError, ValueError):
            continue
    for sai_field in COUNTERMGRD_MAX_COUNTERS:
        leaf = SAI_TO_NVUE_MAX_COUNTER_LEAF.get(sai_field)
        if leaf is None:
            continue
        raw = counters.get(leaf)
        if raw is None or str(raw).strip().lower() in ("", "n/a"):
            continue
        try:
            out[sai_field] = parse_counter_value(raw)
        except (AssertionError, ValueError):
            continue
    return out


def read_nvue_aggregation_snapshot_window(
    engines,
    aport_name: str,
    plane_ports: List[Port],
    sai_fields: Optional[List[str]] = None,
) -> Dict[str, Dict[str, int]]:
    """
    Read Aport + plane-port countermgrd fields via per-port ``counters -o json``.

    Bulk ``nv show interface -o json`` omits counter leaves; reading each port's
    counters keeps the NVUE aggregation check in one tight window (section 6.3).
    """
    port_names = [aport_name] + [p.name for p in plane_ports]
    with allure.step(
        "Read NVUE counters for "
        f"{aport_name} + {len(plane_ports)} plane-port(s) (no inter-port sleep)"
    ):
        readings: Dict[str, Dict[str, int]] = {}
        for port_name in port_names:
            counters = _nvue_fetch_interface_counters_json(engines, port_name)
            readings[port_name] = _sai_snapshot_from_nvue_counters(counters, sai_fields)
        return readings


def _otel_point_near(points: Dict[int, float], ts_ns: int) -> Optional[float]:
    """The value sampled closest to ``ts_ns``, if within the same export cycle."""
    if not points:
        return None
    nearest = min(points, key=lambda ts: abs(ts - ts_ns))
    if abs(nearest - ts_ns) > IB_OTEL_CURRENT_SAMPLE_SLACK_SEC * 1_000_000_000:
        return None
    return points[nearest]


def read_otel_aggregation_snapshot_window(
    aport_name: str,
    plane_ports: List[Port],
    sai_fields: Optional[List[str]] = None,
) -> Dict[str, Dict[str, int]]:
    """
    Read Aport + plane-port countermgrd fields from one OTEL collection window.

    Every port of one export cycle is sampled together, so each field is read at the Aport's
    newest sample and each plane at its point of that same cycle (section 6.3). Same
    {SAI_PORT_STAT_*: int} shape as the gNMI / NVUE windows.
    """
    fields = list(sai_fields if sai_fields is not None else COUNTERMGRD_SUM_COUNTERS)
    fields += [f for f in COUNTERMGRD_MAX_COUNTERS if f not in fields]
    port_names = [aport_name] + [p.name for p in plane_ports]
    with allure.step(
        f"Read OTEL counters for {aport_name} + {len(plane_ports)} plane-port(s) "
        "from one export cycle"
    ):
        snap = ibo.active_surface().snapshot("aggregation", fresh=True)
        readings: Dict[str, Dict[str, int]] = {name: {} for name in port_names}
        for sai_field in fields:
            per_port = snap.per_key_points(
                LOCAL_PORT_OTEL_METRIC_BY_DB_FIELD[sai_field], OtelLabel.INTERFACE)
            aport_points = per_port.get(aport_name, {})
            if not aport_points:
                continue
            cycle_ts = max(aport_points)
            for name in port_names:
                value = _otel_point_near(per_port.get(name, {}), cycle_ts)
                if value is not None:
                    readings[name][sai_field] = int(value)
        return readings


def read_counter_snapshot_window(
    aport_name: str,
    plane_ports: List[Port],
    snapshot_for_port: Optional[Callable[[str], Dict[str, int]]] = None,
    inter_plane_sleep_sec: float = 0.1,
    snapshot_all_ports: Optional[
        Callable[[str, List[Port]], Dict[str, Dict[str, int]]]
    ] = None,
) -> Dict[str, Dict[str, int]]:
    """
    Read Aport and each plane-port within one sampling window.

    ``snapshot_all_ports`` captures every port in one consistent snapshot
    (Redis EVAL / wildcard gNMI subscribe-once) so all counters share the same
    sampling instant. When omitted, ``snapshot_for_port`` is read per port -
    typically ``functools.partial(capture_*_snapshot, ...)``.
    """
    if snapshot_all_ports is not None:
        return snapshot_all_ports(aport_name, plane_ports)
    assert snapshot_for_port is not None, (
        "read_counter_snapshot_window requires snapshot_for_port or snapshot_all_ports"
    )
    readings: Dict[str, Dict[str, int]] = {
        aport_name: snapshot_for_port(aport_name),
    }
    for plane in plane_ports:
        readings[plane.name] = snapshot_for_port(plane.name)
        time.sleep(inter_plane_sleep_sec)
    return readings


def assert_counter_snapshot_window_nonempty(
    readings: Dict[str, Dict[str, int]],
    aport_name: str,
    api_label: str,
    expected_sai_fields: Optional[List[str]] = None,
) -> None:
    """Fail when required aggregation counters are missing on the Aport for this API."""
    sum_fields = expected_sai_fields if expected_sai_fields is not None else COUNTERMGRD_SUM_COUNTERS
    aport_row = readings.get(aport_name, {})
    missing = [f for f in sum_fields if f not in aport_row]
    assert not missing, (
        f"Aport {aport_name} missing countermgrd SUM counters via {api_label}: {missing!r}; "
        f"got keys {sorted(aport_row.keys())!r}"
    )


# ============================================================================
# Admin-down counter-freeze window (section 6.3)
# ============================================================================


# Short oper-state poll after admin-down; partner-down may need a few more tries.
_QUIESCE_ADMIN_DOWN_OPER_TRIES = 3
_QUIESCE_PARTNER_DOWN_OPER_TRIES = 8


def _link_oper_state_on_engine(engine, port_name: str) -> str:
    raw = engine.run_cmd(f"nv show interface {port_name} link -o json")
    parsed = OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()
    link_state = parsed.get(IbInterfaceConsts.LINK_STATE, parsed)
    if isinstance(link_state, dict):
        for key in ("operational", IbInterfaceConsts.LINK_STATE, "state"):
            if key in link_state:
                return str(link_state[key]).lower()
    return str(parsed.get("operational", "")).lower()


def _set_link_state_on_engine(engine, port_name: str, state: str, apply: bool = True) -> None:
    engine.run_cmd(f"nv set interface {port_name} link state {state}")
    if apply:
        NvueGeneralCli.apply_config(engine=engine, option='-y')


def _wait_for_link_oper_on_engine(engine, port_name: str, expected_state: str, tries: int) -> bool:
    expected = expected_state.lower()
    for _ in range(tries):
        if _link_oper_state_on_engine(engine, port_name) == expected:
            return True
        time.sleep(1)
    return False


def _aport_link_oper_state(aport: Port) -> str:
    link_row = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
        aport.interface.link.show()
    ).get_returned_value()
    if IbInterfaceConsts.LINK_STATE not in link_row:
        raise AssertionError(f"link row missing link-state: {link_row!r}")
    return link_row[IbInterfaceConsts.LINK_STATE]


def _wait_for_admin_down_quiesce(
    aport: Port,
    require_oper_down: bool = True,
    tries: int = _QUIESCE_ADMIN_DOWN_OPER_TRIES,
    partner_port: Optional[str] = None,
    partner_quiesce_attempted: bool = False,
) -> None:
    """Wait for DUT oper-down after admin-down (and optional partner admin-down)."""
    try:
        Port.wait_for_port_state(aport, NvosConsts.LINK_STATE_DOWN, tries=tries)
        return
    except Exception:  # noqa: BLE001
        if _aport_link_oper_state(aport) == NvosConsts.LINK_STATE_DOWN:
            return

    if not require_oper_down:
        note = (
            f"Aport {aport.name} oper state stayed up after admin-down "
            "(and partner down when available); proceeding with counter snapshot"
        )
        logger.warning(note)
        allure.attach(f"quiesce not oper-down {aport.name}", note)
        return

    if partner_quiesce_attempted:
        pytest.fail(
            f"Aport {aport.name} oper state stayed up after admin-down on DUT and "
            f"partner {partner_port!r}; counter snapshots would drift."
        )

    pytest.skip(
        f"Aport {aport.name} stayed oper-up after admin-down with no inter-switch "
        "partner in connectivity (loopback/internal link). section 6.3 requires a "
        "quiescent counter window - pick an inter-switch Aport or refresh "
        "connectivity JSON and ensure dut2/dut3 is in topology."
    )


@contextmanager
def quiesce_aport_via_loopback_partner(
    aport: Port,
    peer_port_name: str,
    engines,
    require_oper_down: bool = True,
) -> Generator[None, None, None]:
    """Admin-down a loopback Aport *and* its same-switch peer so the IB link
    truly goes operationally down, then restore both ends in ``finally``.

    An IB link only drops operationally when *both* ends are admin-disabled.
    On a single-switch loopback bench there is no inter-switch dut2/dut3
    partner, so the "other end" of `aport` is `peer_port_name` on the same
    DUT. Both ends are driven through ``engines.dut``.
    """
    with allure.step(f"Admin-down loopback Aport {aport.name} + peer {peer_port_name}"):
        aport.interface.link.state.set(
            op_param_name=NvosConsts.LINK_STATE_DOWN, apply=True, ask_for_confirmation=True
        ).verify_result()
        _set_link_state_on_engine(engines.dut, peer_port_name, NvosConsts.LINK_STATE_DOWN)
        _wait_for_admin_down_quiesce(
            aport,
            require_oper_down=require_oper_down,
            tries=_QUIESCE_PARTNER_DOWN_OPER_TRIES,
            partner_port=peer_port_name,
            partner_quiesce_attempted=True,
        )
    with allure.step(f"Wait {COUNTER_SNAPSHOT_SETTLE_SEC}s for state to settle on {aport.name}"):
        time.sleep(COUNTER_SNAPSHOT_SETTLE_SEC)
    try:
        yield
    finally:
        with allure.step(f"Admin-up loopback peer {peer_port_name} (restore)"):
            try:
                _set_link_state_on_engine(engines.dut, peer_port_name, NvosConsts.LINK_STATE_UP)
                _wait_for_link_oper_on_engine(
                    engines.dut, peer_port_name, NvosConsts.LINK_STATE_UP,
                    tries=_QUIESCE_PARTNER_DOWN_OPER_TRIES,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("loopback peer admin-up restore failed for %s: %s", peer_port_name, exc)
        with allure.step(f"Admin-up Aport {aport.name} (restore)"):
            try:
                aport.interface.link.state.set(
                    op_param_name=NvosConsts.LINK_STATE_UP, apply=True, ask_for_confirmation=True
                ).verify_result()
                Port.wait_for_port_state(aport, NvosConsts.LINK_STATE_UP)
            except Exception as exc:  # noqa: BLE001
                logger.warning("loopback Aport admin-up restore failed for %s: %s", aport.name, exc)


def try_drive_ib_traffic_burst(
    players,
    interfaces,
    setup_name,
    settle_sec: int = 2,
) -> Tuple[bool, str]:
    """
    Drive a small IB traffic burst, returning ``(True, "")`` on success or
    ``(False, reason)`` when the TG IB link is down / traffic could not be
    generated. The failed ResultObj is consumed so it does not surface as a
    teardown failure, letting callers fall back to quiescent counters.
    """
    result = Tools.TrafficGeneratorTool.send_ib_traffic(
        players, interfaces, setup_name, True
    )
    if not result.result:
        reason = result.info
        result.ignore_result()
        return False, reason
    result.ignore_result()
    time.sleep(settle_sec)
    return True, ""


# ============================================================================
# DB lookups (Aport / plane-port OID resolution, row hgetall)
# ============================================================================


def get_aport_oid(engines, port_name: str) -> str:
    """Resolve an Aport's COUNTERS_DB OID via the existing system/gnmi helpers."""
    ib_name = get_infiniband_name_from_port_name(engines.dut, port_name)
    return get_port_oid_from_infiniband_port(engines.dut, ib_name)


# Memoized ``plane-ports`` map of an Aport, keyed by (engine, Aport). The no-arg
# bulk ``nv show fae interface`` only exposes link+type, so plane-ports/key detail
# must come from the per-Aport ``nv show fae interface <aport>`` form. Caching per
# Aport keeps it at one show per active Aport, never one per plane.
_FAE_APORT_PLAN_PORTS_CACHE: Dict[Tuple[int, str], Dict[str, dict]] = {}


def _fae_aport_plan_ports(engine, aport_name: str) -> Dict[str, dict]:
    """
    Return one Aport's ``plane-ports`` map from ``nv show fae interface <aport>``,
    memoized per (engine, Aport).

    ``Port.show_interface`` returns the raw JSON string (no error-keyword
    ResultObj), and the single parser ResultObj is consumed here, so a missing
    Aport yields an empty map without leaking a failed ResultObj into the
    teardown verifier.
    """
    cache_key = (id(engine), aport_name)
    cached = _FAE_APORT_PLAN_PORTS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    raw = Port.show_interface(fae_param="fae", port_names=aport_name, dut_engine=engine)
    res = OutputParsingTool.parse_show_interface_output_to_dictionary(raw)
    if not res.result:
        res.ignore_result()
        plan_ports: Dict[str, dict] = {}
    else:
        parsed = res.get_returned_value()
        plan_ports = parsed.get("plane-ports", {}) if isinstance(parsed, dict) else {}
        if not isinstance(plan_ports, dict):
            plan_ports = {}
    _FAE_APORT_PLAN_PORTS_CACHE[cache_key] = plan_ports
    return plan_ports


def get_plane_port_oid(engines, plane_port_name: str) -> str:
    """
    Resolve a plane-port OID into the full ``oid:0x...`` COUNTERS_DB key suffix,
    mirroring ``get_port_oid`` in
    ``ngts/tests_nvos/interfaces/test_ib_show_interface.py``.
    """
    aport_name = re.sub(r"pl\d+$", "", plane_port_name)
    plane_suffix = plane_port_name[len(aport_name):]  # e.g. "pl1"

    # First, try the plane-port name directly in COUNTERS_PORT_NAME_MAP (builds
    # that key the map by the user-facing NVUE plane name).
    try:
        direct = Tools.DatabaseTool.sonic_db_cli_hget(
            engine=engines.dut, asic="",
            db_name=DatabaseConst.COUNTERS_DB_NAME,
            db_config=SystemDbCli.COUNTERS_PORT_NAME_MAP,
            param=str(plane_port_name),
        )
        direct = (direct or "").replace('"', "").strip()
        if direct.startswith("oid:"):
            return direct
    except Exception:  # noqa: BLE001
        pass

    # Primary path: COUNTERS_PORT_NAME_MAP is keyed by the *InfiniBand* name
    # (e.g. "Infiniband56pl1"). Translate the Aport portion to its IB name and
    # append the plane suffix.
    try:
        ib_aport = get_infiniband_name_from_port_name(engines.dut, aport_name)
        if ib_aport:
            ib_plane = f"{ib_aport}{plane_suffix}"
            mapped = Tools.DatabaseTool.sonic_db_cli_hget(
                engine=engines.dut, asic="",
                db_name=DatabaseConst.COUNTERS_DB_NAME,
                db_config=SystemDbCli.COUNTERS_PORT_NAME_MAP,
                param=ib_plane,
            )
            mapped = (mapped or "").replace('"', "").strip()
            if mapped.startswith("oid:"):
                return mapped
    except Exception:  # noqa: BLE001
        pass

    # Fallback: resolve via the FAE plane-ports "key" from the per-Aport
    # `nv show fae interface <aport>` form (the no-arg bulk lacks plane-ports),
    # cached per Aport so a multi-plane caller hits one show per Aport, not per
    # plane. A miss is just "not in the map" - no failed ResultObj leaking to
    # teardown.
    plan_ports = _fae_aport_plan_ports(engines.dut, aport_name)
    plane_entry = plan_ports.get(plane_port_name, {})
    port_key_with_suffix = plane_entry.get("key", "") if isinstance(plane_entry, dict) else ""
    if not port_key_with_suffix:
        raise AssertionError(
            f"No FAE plane-ports key for {plane_port_name}; "
            f"Aport {aport_name!r} plane-ports keys={list(plan_ports.keys())}"
        )
    # FAE's 'key' carries a 3-char tail we strip; the trimmed value is the
    # IB-internal port name fed into COUNTERS_PORT_NAME_MAP.
    if len(port_key_with_suffix) <= 3:
        raise AssertionError(f"unexpected FAE plane-ports key, too short to trim: {port_key_with_suffix!r}")
    port_key = port_key_with_suffix[:-3]
    output = Tools.DatabaseTool.sonic_db_cli_hget(
        engine=engines.dut, asic="",
        db_name=DatabaseConst.COUNTERS_DB_NAME,
        db_config=SystemDbCli.COUNTERS_PORT_NAME_MAP,
        param=port_key,
    )
    return (output or "").replace('"', "").strip()


def list_plane_port_keys(engines) -> List[str]:
    """
    Return the COUNTERS_DB keys of the plane-port rows, deduplicated.

    Plane-port rows are enumerated from the live ``COUNTERS_PORT_NAME_MAP`` (every
    entry whose port name carries a ``plN`` suffix), so the count reflects the
    rows sym-mgr actually wrote rather than a brute-forced ``swXp0plY`` candidate
    set probed one OID at a time.
    """
    name_map = db_hgetall(
        engines, SystemDbCli.COUNTERS_DB, SystemDbCli.COUNTERS_PORT_NAME_MAP
    )
    keys: List[str] = []
    for port_name, oid in name_map.items():
        if not is_plane_port_name(port_name):
            continue
        oid = (oid or "").replace('"', "").strip()
        if not oid.startswith("oid:"):
            continue
        full_key = SystemDbCli.COUNTERS_OID_KEY_FMT.format(oid=oid)
        if full_key not in keys:
            keys.append(full_key)
    return keys


def db_hgetall(engines, db_name: str, key: str) -> Dict[str, str]:
    """
    Read a Redis hash via the shared ``Tools.DatabaseTool.sonic_db_cli_hgetall``
    and parse it into a dict (Rowaida R2: reuse the existing reader, no new one).

    ``sonic-db-cli hgetall`` prints a Python dict repr on a single line (single-
    quoted), so ``ast.literal_eval`` is tried first; JSON and line-pair splitting
    are kept only as defensive fallbacks for other emitters.
    """
    # STATE_DB/CONFIG_DB keys carry a '|' separator, which the shell reads as a pipe. The
    # shared reader inserts the table name unquoted, so callers must pre-quote it (matching
    # the UfmMadConsts.*_TEMPLATE convention); otherwise HGETALL on e.g.
    # "PEER_PORT_TELEMETRY_HEALTH|global" silently returns nothing.
    raw = Tools.DatabaseTool.sonic_db_cli_hgetall(
        engine=engines.dut, asic="", db_name=db_name, table_name=f'"{key}"'
    )

    if raw and "{" in raw and "}" in raw:
        snippet = raw[raw.index("{"): raw.rindex("}") + 1]
        try:
            parsed = ast.literal_eval(snippet)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except (ValueError, SyntaxError):
            logger.debug("hgetall literal_eval failed; trying json (key=%s)", key)

    try:
        parsed = OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except Exception:  # noqa: BLE001
        logger.debug("hgetall json parse failed; falling back to line split (key=%s)", key)

    out: Dict[str, str] = {}
    lines = [line for line in (raw or "").splitlines() if line.strip()]
    for i in range(0, len(lines) - 1, 2):
        out[lines[i].strip().strip('"')] = lines[i + 1].strip().strip('"')
    return out


def _sai_snapshot_from_db_row(
    row: Dict[str, str],
    sai_fields: Optional[List[str]] = None,
) -> Dict[str, int]:
    """Map a COUNTERS_DB hash row to {SAI_PORT_STAT_*: int}."""
    fields = sai_fields if sai_fields is not None else EXPECTED_PLANE_PORT_DB_FIELDS
    out: Dict[str, int] = {}
    for sai_field in fields:
        raw = row.get(sai_field)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            out[sai_field] = parse_counter_value(raw)
        except (AssertionError, ValueError):
            continue
    return out


def _resolve_counter_oid(engines, port_name: str) -> str:
    """Resolve a port's ``oid:0x...`` COUNTERS_DB key suffix from the name maps."""
    if is_plane_port_name(port_name):
        oid = get_plane_port_oid(engines, port_name)
    else:
        oid = get_aport_oid(engines, port_name)
    oid = (oid or "").replace('"', "").strip()
    assert oid.startswith("oid:"), (
        f"Could not resolve COUNTERS_DB OID for {port_name!r}: got {oid!r}"
    )
    return oid


# Atomic multi-hash read: one EVAL returns every COUNTERS row at a single
# instant as tab-joined "key\tfield\tvalue..." records, one per line.
_DB_SNAPSHOT_LUA = (
    "local r={} for i=1,#KEYS do "
    "local h=redis.call('HGETALL',KEYS[i]) r[i]=KEYS[i] "
    "for j=1,#h do r[i]=r[i]..'\\t'..h[j] end end "
    "return table.concat(r,'\\n')"
)


def capture_counters_db_aggregation_snapshot_window(
    engines,
    aport_name: str,
    plane_ports: List[Port],
    sai_fields: Optional[List[str]] = None,
) -> Dict[str, Dict[str, int]]:
    """
    Read Aport + plane-port COUNTERS_DB rows in one atomic Redis EVAL snapshot.

    OIDs resolve first from the static name maps (no counter skew), then every
    ``COUNTERS:oid:*`` row is read in a single round-trip so all counters share
    the same sampling instant (section 6.2).
    """
    port_names = [aport_name] + [p.name for p in plane_ports]
    with allure.step(
        f"Read COUNTERS_DB rows for {aport_name} + {len(plane_ports)} plane-port(s) "
        "in one Redis snapshot"
    ):
        key_to_port: Dict[str, str] = {}
        for port_name in port_names:
            oid = _resolve_counter_oid(engines, port_name)
            key_to_port[SystemDbCli.COUNTERS_OID_KEY_FMT.format(oid=oid)] = port_name
        raw = Tools.DatabaseTool.redis_cli_eval(
            engine=engines.dut,
            db_num=DatabaseConst.COUNTERS_DB_ID,
            script=_DB_SNAPSHOT_LUA,
            keys=list(key_to_port.keys()),
        )
        readings: Dict[str, Dict[str, int]] = {name: {} for name in port_names}
        for line in (raw or "").splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            key = parts[0].strip().strip('"')
            port_name = key_to_port.get(key)
            if port_name is None:
                continue
            row: Dict[str, str] = {}
            fields = parts[1:]
            for i in range(0, len(fields) - 1, 2):
                row[fields[i]] = fields[i + 1]
            readings[port_name] = _sai_snapshot_from_db_row(row, sai_fields)
        return readings


def otel_port_db_parity_problems(
    engines,
    aport_name: str,
    plane_ports: List[Port],
    sai_fields: List[str],
    label: str,
) -> List[str]:
    """Problems where an OTEL port metric is not a value its COUNTERS_DB row held in the window.

    The rows are read before and after one fresh collection window, so each exported value must
    sit within (for a counter) or equal (for a state) what the DB carried around it.
    """
    port_names = [aport_name] + [p.name for p in plane_ports]
    before = capture_counters_db_aggregation_snapshot_window(engines, aport_name, plane_ports, sai_fields)
    snap = ibo.active_surface().snapshot(label, fresh=True)
    after = capture_counters_db_aggregation_snapshot_window(engines, aport_name, plane_ports, sai_fields)
    attach_dict(f"COUNTERS_DB around OTEL window {label}", {"before": before, "after": after})
    problems: List[str] = []
    for name in port_names:
        problems += ibo.db_parity_problems(
            snap, name, before.get(name, {}), after.get(name, {}),
            LOCAL_PORT_OTEL_METRIC_BY_DB_FIELD, sai_fields,
        )
    return problems


def otel_link_down_reasons(snap: ibo.OtelSnapshot, port_name: str) -> List[str]:
    """The local link-down reasons the port currently exports over OTEL."""
    return [
        series.labels.get(OTEL_LINK_DOWN_LOCAL_REASON_LABEL, "")
        for series in snap.current(LOCAL_PORT_OTEL_LINK_DOWN_INFO,
                                   **{OtelLabel.INTERFACE: port_name})
    ]


# ============================================================================
# countermgrd aggregation rules (SUM / MAX / MIN / CONCAT)
# ============================================================================


def aggregate_max(values: List[str]) -> int:
    """Plain integer max (xmit-wait etc.)."""
    coerced = [int(v) for v in values if str(v).strip() != ""]
    if not coerced:
        raise AssertionError("aggregate_max called with no usable values")
    return max(coerced)


def aggregate_float_max(values: List[str]) -> Tuple[str, float]:
    """Pick the per-plane BER string whose float value is largest; raise if none parse."""
    best_str: Optional[str] = None
    best_val: Optional[float] = None
    for v in values:
        if not v:
            continue
        try:
            f = float(v)
        except ValueError:
            logger.debug("aggregate_float_max: skipping non-parseable value %r", v)
            continue
        if best_val is None or f > best_val:
            best_val = f
            best_str = v
    if best_str is None or best_val is None:
        raise AssertionError(f"aggregate_float_max: no parseable values in {values!r}")
    return best_str, best_val


def aggregate_min(values: List[str], rank: Optional[Dict[str, int]] = None) -> str:
    """
    countermgrd MIN rule. Numeric values use ``min(int)``; enum/string values use
    ``rank`` (unknown ranks sort last). Without a rank, non-numeric values must
    already agree, otherwise the min is undefined.
    """
    usable = [str(v).strip() for v in values if v is not None and str(v).strip() != ""]
    if not usable:
        raise AssertionError("aggregate_min: no usable values")
    try:
        return str(min(int(v) for v in usable))
    except ValueError:
        pass
    if rank:
        return min(usable, key=lambda v: rank.get(v, 1 << 30))
    distinct = set(usable)
    if len(distinct) == 1:
        return usable[0]
    raise AssertionError(
        f"aggregate_min: cannot order non-numeric values without a rank map: {distinct!r}"
    )


def aggregate_concat(values: List[str], delimiter: str = CONCAT_DELIMITER) -> str:
    """countermgrd CONCAT rule: append unique plane tokens joined by ``delimiter``."""
    tokens: List[str] = []
    for v in values:
        if v is None:
            continue
        for part in str(v).split(delimiter):
            part = part.strip()
            if part and part not in tokens:
                tokens.append(part)
    return delimiter.join(tokens)


def _scalar_equal(actual: str, expected: str, value_type: str) -> bool:
    if value_type == "int":
        try:
            return parse_counter_value(actual) == parse_counter_value(expected)
        except (AssertionError, ValueError):
            return False
    return str(actual).strip() == str(expected).strip()


def _concat_is_superset(aport_value: str, plane_values: List[str], delimiter: str = CONCAT_DELIMITER) -> bool:
    """True if every plane token appears in the Aport's concatenated value."""
    aport_tokens = {t.strip() for t in str(aport_value).split(delimiter) if t.strip()}
    plane_tokens = {
        t.strip()
        for v in plane_values if v is not None
        for t in str(v).split(delimiter) if t.strip()
    }
    return plane_tokens.issubset(aport_tokens)


def values_within_tolerance(measured: float, expected: float, tolerance_pct: float) -> bool:
    """Standard +/- N% comparison used for aggregation reads across sampling windows."""
    if expected == 0:
        return abs(measured) <= max(1.0, tolerance_pct)
    return abs(measured - expected) <= tolerance_pct * abs(expected)


def assert_rule_aggregation(
    rule: str,
    sai_key: str,
    aport_value: Optional[str],
    plane_values: List[Optional[str]],
    api_label: str,
    *,
    value_type: str = "int",
    rank: Optional[Dict[str, int]] = None,
    tolerance_pct: float = SAMPLING_JITTER_TOLERANCE_PCT,
) -> Dict[str, Any]:
    """
    Assert the Aport value follows ``rule`` over the plane values for ``sai_key``.

    Returns a detail dict (for Allure attach). Raises AssertionError on mismatch
    or when a value that should be present is missing (hard-fail policy).
    """
    present = [v for v in plane_values if v is not None and str(v).strip() != ""]
    assert present, (
        f"{api_label}: no plane values exposed for {sai_key} (rule={rule}); "
        "cannot verify aggregation"
    )
    assert aport_value is not None and str(aport_value).strip() != "", (
        f"{api_label}: Aport missing {sai_key} (rule={rule}) but planes expose it: {present!r}"
    )

    if rule == CounterMgrdRule.MIN:
        expected: Any = aggregate_min(present, rank=rank)
        ok = _scalar_equal(aport_value, expected, value_type)
    elif rule == CounterMgrdRule.MAX:
        if value_type == "float":
            _, expected = aggregate_float_max(present)
            ok = values_within_tolerance(float(aport_value), float(expected), tolerance_pct)
        else:
            expected = aggregate_max(present)
            ok = parse_counter_value(aport_value) == expected
    elif rule == CounterMgrdRule.CONCAT:
        expected = aggregate_concat(present)
        ok = _concat_is_superset(aport_value, present)
    else:
        raise ValueError(f"assert_rule_aggregation does not handle rule {rule!r}")

    detail = {
        "rule": rule,
        "sai_key": sai_key,
        "api": api_label,
        "value_type": value_type,
        "aport": aport_value,
        "planes": present,
        "expected": expected,
    }
    assert ok, (
        f"{api_label} {rule} aggregation mismatch on {sai_key}: "
        f"aport={aport_value!r}, expected={expected!r}, planes={present!r}"
    )
    return detail


def aggregation_allowed_delta(expected: float) -> float:
    """Allowed |measured - expected| for plane-port SUM aggregation (section 6.2 / 6.3).

    Absolute cap only (no percentage slack) so a fixed sum mismatch fails
    regardless of counter scale. gNMI/NVUE/Redis share this helper.
    """
    del expected
    return float(PLANEPORT_SUM_AGGREGATION_MIN_DELTA)


def values_within_aggregation_tolerance(measured: float, expected: float) -> bool:
    """Tight SUM aggregation check: Aport vs sum(plane-ports)."""
    return abs(measured - expected) <= aggregation_allowed_delta(expected)


# ============================================================================
# Peer link-loss simulation (test plan section 10.2 / section 10.4)
# ============================================================================


def resolve_module_handle(engines, devices, port: Port) -> Tuple[str, str]:
    """Resolve (mst_dev_name, module_index) for a Port for simulate_*_module_event.

    ``module_index`` follows the rule used by test_platform_transceiver: the
    front-panel label number (1-based) minus 1, taken modulo the per-ASIC
    ``module_offset`` when the device defines one. The label number comes from
    ``Port.parse_port_name`` (robust for ``swA13p2`` / ``sw1p1pl3`` naming).
    """
    mst_dev_name = IbInterfaceTool.get_mst_dev_name(engine=engines.dut, port_name=port.name)
    try:
        _, label_number, _, _, _ = Port.parse_port_name(port.name)
    except ValueError as exc:
        raise AssertionError(f"Cannot resolve module index for {port.name}: {exc}") from exc
    module_index = int(label_number) - 1
    offset = getattr(devices.dut, "module_offset", None)
    if offset:
        module_index %= offset
    return mst_dev_name, str(module_index)


# Firmware rejects PMAOS module-register access on a secondary ASIC device:
#   -E- Failed to send access register: Register Access not supported by secondary
# On planarized multi-ASIC Aports the resolved primary-asic-device can be a
# secondary for module-register access, so the PMAOS unplug/plug simulation
# cannot run on that port; gate on it instead of hard-failing the test.
PMAOS_SECONDARY_REJECTION = "not supported by secondary"

# A PMAOS read (-g) succeeds even on devices/modules whose firmware rejects the
# unplug/plug write (-s). The rejection only surfaces on the write, as one of
# these markers, so the probe-by-read cannot predict it and the simulation must
# gate on the failed write instead.
PMAOS_WRITE_REJECTION_MARKERS = (
    PMAOS_SECONDARY_REJECTION,
    "configuration is rejected",
    # Some HW firmware rejects the PMAOS module-register write with:
    #   -E- Failed to send access register: ... Register access bad parameter
    "register access bad parameter",
    "failed to send access register",
)


def _pmaos_write_rejected(text: str) -> bool:
    """True when a PMAOS write failure carries a known firmware-rejection marker."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in PMAOS_WRITE_REJECTION_MARKERS)


def pmaos_simulation_available(engines, mst_dev_name: str, module_index: str) -> bool:
    """Return True when the MST device accepts PMAOS module-register access.

    Reads PMAOS with validation disabled (a firmware rejection must not raise) and
    reports whether the secondary-ASIC rejection marker is absent from the output.
    """
    out = RegisterTool.get_mst_register_value(
        engines.dut,
        mst_dev_name,
        UfmMadConsts.PMAOS_REGISTER,
        additional_params=f"-i slot_index=0,module={module_index}",
        validate=False,
    )
    available = PMAOS_SECONDARY_REJECTION not in (out or "")
    if not available:
        logger.info(
            "PMAOS module simulation unavailable on %s (module=%s): device is a "
            "secondary ASIC for module-register access", mst_dev_name, module_index
        )
    return available


def simulate_peer_unplug(engines, devices, port: Port, settle_sec: int = 8) -> None:
    """Use PMAOS register simulation to mark the module as unplugged.

    Skips cleanly when the resolved MST device is a secondary ASIC that rejects
    PMAOS module-register access (planarized multi-ASIC Aport), so the suite
    reports a skip rather than a firmware-rejection failure.
    """
    mst_dev_name, module_index = resolve_module_handle(engines, devices, port)
    if not pmaos_simulation_available(engines, mst_dev_name, module_index):
        pytest.skip(
            f"PMAOS unplug simulation unavailable for {port.name}: MST device "
            f"{mst_dev_name!r} rejects module-register access on a secondary ASIC "
            "(planarized multi-ASIC Aport)."
        )
    try:
        IbInterfaceTool.simulate_unplug_module_event(
            engine=engines.dut,
            device=devices.dut,
            module_index=module_index,
            mst_dev_name=mst_dev_name,
            sleep=settle_sec,
        )
    except Exception as exc:  # noqa: BLE001
        if _pmaos_write_rejected(str(exc)):
            pytest.skip(
                f"PMAOS unplug simulation rejected by firmware for {port.name} "
                f"(MST device {mst_dev_name!r}, module {module_index}): {exc}"
            )
        raise


def simulate_peer_plug_in(engines, devices, port: Port, settle_sec: int = 50) -> None:
    """Reverse of ``simulate_peer_unplug``.

    No-op when PMAOS module-register access is unavailable on the resolved device,
    so cleanup after a skipped unplug never raises.
    """
    mst_dev_name, module_index = resolve_module_handle(engines, devices, port)
    if not pmaos_simulation_available(engines, mst_dev_name, module_index):
        logger.warning(
            "Skipping PMAOS plug-in for %s: MST device %r rejects module-register "
            "access on a secondary ASIC", port.name, mst_dev_name
        )
        return
    try:
        IbInterfaceTool.simulate_plugin_module_event(
            engine=engines.dut,
            device=devices.dut,
            module_index=module_index,
            mst_dev_name=mst_dev_name,
            sleep=settle_sec,
        )
    except Exception as exc:  # noqa: BLE001
        if _pmaos_write_rejected(str(exc)):
            logger.warning(
                "PMAOS plug-in for %s rejected by firmware (module %s): %s",
                port.name, module_index, exc
            )
            return
        raise


# ============================================================================
# Peer-port / HCA / nmxt-ib telemetry helpers (ported from change 336026)
# ============================================================================


# Aports whose HCA peer was administratively downed by simulate_hca_peer_down,
# so restore_hca_peer can bring the original switch port back up.
_HCA_DOWNED_APORTS: Dict[str, str] = {}

# DUT-side record (one Aport alias per line) of ports we admin-downed, so the
# safety teardown can bring them back even after a hard abort that skips the
# test's own finally (the in-process dict above does not survive that).
_HCA_DOWNED_APORTS_FILE = "/tmp/peerport_downed_aports"


# Top-level `nv show peer-port` listing resource (distinct from System().peer_port).
_PEER_PORT_RESOURCE = BaseComponent(
    parent=None,
    api={ApiType.NVUE: NvueSystemCli, ApiType.OPENAPI: OpenApiSystemCli},
    path='/peer-port',
)


def canonical_aport(name: str) -> str:
    """Canonical switch-Aport key for cross-source compare; bridges the ibdiagnet p0 <-> NVOS p1 off-by-one while keeping distinct ports distinct."""
    return re.sub(r"p0$", "p1", str(name).strip().lower())


def gnmi_list_members(client: GnmiClient, list_prefix: str,
                      relative: bool = False) -> Dict[str, Dict[str, str]]:
    """Enumerate a gNMI list under `list_prefix` as ``{member_key: {leaf: value}}``.

    With `relative`, each leaf keeps the containers below its list member instead of just the
    last segment, so members that nest several samples under one leaf name stay distinguishable.
    """
    members: Dict[str, Dict[str, str]] = {}
    for line in gnmi_get_raw_lines(client, prefix=list_prefix, path=""):
        parsed = _split_gnmic_flat_line(line)
        if parsed is None:
            continue
        path, value = parsed
        keys = list(_GNMI_LIST_KEY_RE.finditer(path))
        if not keys:
            continue
        leaf = path[keys[-1].end():].strip("/") if relative else path.rsplit("/", 1)[-1]
        if not leaf:
            continue
        members.setdefault(keys[-1].group(1), {})[leaf] = value
    return members


def _flatten_dict(body: Dict, prefix: str = "") -> Dict[str, str]:
    """Flatten nested NVUE JSON to {leaf: value} (leaf segment only, stringified) for diffing vs gnmic flat."""
    out: Dict[str, str] = {}
    if not isinstance(body, dict):
        return out
    for k, v in body.items():
        if isinstance(v, dict):
            out.update(_flatten_dict(v, prefix=k))
        else:
            out[str(k)] = "" if v is None else str(v)
    return out


# Peer-port (GPU + HCA) read + classification.
def _parse_peer_port_flat_lines(lines: List[str]) -> Dict[str, Dict[str, str]]:
    """Parse gnmic flat peer-port lines into ``{peer_id: {leaf: value}}``."""
    result: Dict[str, Dict[str, str]] = {}
    for line in lines:
        match = re.search(r"interface\[name=([^\]]+)\]/(.+?):\s*(.*)$", line)
        if not match:
            continue
        pid, path, value = match.group(1), match.group(2), match.group(3)
        leaf = path.rsplit("/", 1)[-1].strip()
        result.setdefault(pid, {})[leaf] = value.strip()
    return result


def gnmi_get_peer_port_list(client: GnmiClient) -> Dict[str, Dict[str, str]]:
    """Return ``{peer_id: {leaf: value}}`` for the whole peer-port subtree."""
    lines = gnmi_get_raw_lines(client, GnmiYangPaths.PEER_PORT_INTERFACES, path="")
    return _parse_peer_port_flat_lines(lines)


def gnmi_get_peer_port(client: GnmiClient, peer_id: str) -> Dict[str, str]:
    """Return a single peer-port entry as a flat ``{leaf: value}`` dict."""
    lines = gnmi_get_raw_lines(client, GnmiYangPaths.PEER_PORT_BY_ID.format(pid=peer_id), path="")
    parsed = _parse_peer_port_flat_lines(lines)
    out = parsed.get(peer_id, {})
    # GET-by-id often omits identity-name leaves; inject the requested id.
    if out and not any(out.get(k) for k in ("peer-port-name", "peer_port_name", "peer-component",
                                            "peer_component", PeerPortFields.HCA_ALIAS_FIELD)):
        out.setdefault("peer-port-name", peer_id)
    return out


def nvue_show_peer_ports(engines) -> Dict[str, Dict[str, str]]:
    """`nv show peer-port` via the wrapper; returns ``{peer_id: {leaf: value}}`` (flattened)."""
    body = _PEER_PORT_RESOURCE.parse_show(dut_engine=engines.dut)
    out: Dict[str, Dict[str, str]] = {}
    for pid, entry in (body or {}).items():
        out[str(pid)] = _flatten_dict(entry) if isinstance(entry, dict) else {}
    return out


def nvue_peer_port_shows_no_data(engines) -> bool:
    """True if ``nv show peer-port`` renders the "No data" sentinel (all peers down)."""
    out = engines.dut.run_cmd("nv show peer-port") or ""
    return "no data" in out.lower()


def nvue_peer_port_show(engines, peer_id: str, sub: str = "") -> Dict:
    """`nv show peer-port <id> [sub]` returning the nested JSON as-is (not flattened)."""
    op_param = f"{peer_id} {sub}".strip()
    body = _PEER_PORT_RESOURCE.parse_show(op_param=op_param, dut_engine=engines.dut)
    return body if isinstance(body, dict) else {}


def nvue_show_peer_port(engines, peer_id: str) -> Dict[str, str]:
    """`nv show peer-port <id>` via the framework wrapper; returns a flat dict."""
    return _flatten_dict(nvue_peer_port_show(engines, peer_id))


def peer_port_show_containers(body: Dict) -> Dict[str, Dict]:
    """Child containers of a peer-port show payload as ``{name: sub-payload}``."""
    return {str(k): v for k, v in (body or {}).items() if isinstance(v, dict)}


def peer_port_show_leaves(body: Dict) -> Dict[str, str]:
    """Direct (non-container) leaves of a peer-port show payload, stringified (null -> "")."""
    return {str(k): "" if v is None else str(v)
            for k, v in (body or {}).items() if not isinstance(v, dict)}


def select_nvue_peer_port_targets(engines) -> List[Tuple[str, str]]:
    """Pick one aggregated HCA peer (preferring a switch-cabled one) plus one of its plane rows."""
    peers = nvue_show_peer_ports(engines)
    assert peers, (
        "No peer-ports listed via NVUE (`nv show peer-port`). Enable the knob and confirm "
        "peers are attached and their links are Active (an SM must be running)."
    )
    aggregated = aggregated_peer_entries(filter_peer_entries_by_type(peers, PeerType.HCA))
    assert aggregated, f"No aggregated peer-type={PeerType.HCA} entries via NVUE"
    # Prefer a peer cabled to a switch port: an FNM-connected HCA only gains its peer
    # transceiver / asic containers in a later drop.
    switch_cabled = sorted(pid for pid, fields in aggregated.items()
                           if peer_entry_aport(fields).lower().startswith("sw"))
    parent = (switch_cabled or sorted(aggregated))[0]
    targets = [(PeerPortTargetRole.AGGREGATED, parent)]
    plane_ids = plane_peer_ids_for_parent(set(peers), parent)
    if plane_ids:
        targets.append((PeerPortTargetRole.PLANE, plane_ids[0]))
    return targets


def leaf_problems(leaves: Dict[str, Any], expected: Dict[str, str],
                  allow_not_available: bool = False,
                  tolerated_missing: FrozenSet[str] = frozenset(),
                  where: str = "", require_value: bool = False) -> List[str]:
    """Missing / mistyped leaves against an ``{leaf: GnmiTypeKind}`` expectation.

    Shared by the NVUE show-surface walk and the gNMI schema sweeps: both need the same
    case-insensitive presence check, the same known-gap tolerance and the same typecheck.
    A blank value is tolerated unless ``require_value`` is set, which reports it as a problem.
    A tolerated leaf that has started reporting is logged as a warning rather than as a
    problem, so the tolerance is noticed once the build delivers the leaf.
    """
    problems: List[str] = []
    leaves_ci = {k.lower(): v for k, v in leaves.items()}
    tolerated_ci = {leaf.lower() for leaf in tolerated_missing}
    location = f" under {where}" if where else ""
    for leaf, kind in expected.items():
        if leaf.lower() not in leaves_ci:
            if leaf.lower() in tolerated_ci:
                logger.info("Tolerated leaf %r is absent%s (known gap)", leaf, location)
            else:
                problems.append(f"missing leaf {leaf!r}{location}")
            continue
        if leaf.lower() in tolerated_ci:
            logger.warning(
                "Tolerated leaf %r is now reported%s - drop it from the tolerated set so "
                "the check enforces it again", leaf, location,
            )
        value = leaves_ci[leaf.lower()]
        if allow_not_available and str(value).strip().lower() == PEER_PORT_DIRECTION_NA:
            continue
        if require_value and str(value).strip().strip('"').strip() in ("", "None"):
            problems.append(f"leaf {leaf!r}: reported with no value ({value!r})")
            continue
        err = gnmi_typecheck(kind, value)
        if err:
            problems.append(f"leaf {leaf!r}: {err}")
    return problems


def _peer_port_lane_problems(lane_body: Dict) -> List[str]:
    """Per-lane problems under `link phy health`; a lane past the operational width may be null."""
    lanes = peer_port_show_containers(lane_body)
    if not lanes:
        return [f"missing '{PeerPortContainer.LANE}' members"]
    problems: List[str] = []
    populated = 0
    for lane_id, member in sorted(lanes.items()):
        leaves = peer_port_show_leaves(member)
        problems += [f"lane {lane_id} {problem}"
                     for problem in leaf_problems(leaves, PEER_PORT_LANE_LEAVES)]
        if any(leaves.get(leaf, "").strip() for leaf in PEER_PORT_LANE_LEAVES):
            populated += 1
    if not populated:
        problems.append(
            f"every '{PeerPortContainer.LANE}' member is empty; the operational lanes must report"
        )
    return problems


def _peer_port_histogram_problems(buckets_body: Dict) -> List[str]:
    """Per-bucket problems under `link phy health histogram`."""
    buckets = peer_port_show_containers(buckets_body)
    problems: List[str] = []
    missing = [str(b) for b in range(PEER_PORT_HISTOGRAM_BUCKET_COUNT) if str(b) not in buckets]
    if missing:
        problems.append(f"histogram missing bucket(s) {missing!r}")
    for bucket_id, member in sorted(buckets.items()):
        problems += [
            f"histogram bucket {bucket_id} {problem}"
            for problem in leaf_problems(
                peer_port_show_leaves(member), {PEER_PORT_HISTOGRAM_LEAF: GnmiTypeKind.COUNTER})
        ]
    return problems


def peer_port_show_problems(body: Dict, node: NvueShowNode,
                            role: str = PeerPortTargetRole.AGGREGATED) -> List[str]:
    """Contract problems for one `nv show peer-port <id> <sub>` payload (empty list when clean).

    The peer's tier labels every problem and every tolerated-leaf log, because a leaf that is
    absent on a plane row while the aggregated row reports it is the interesting case.

    ``node.pending`` names leaves whose tier the HLD leaves open, so one row may lack them; the
    caller proves across the listing that some tier carries them.
    """
    containers = peer_port_show_containers(body)
    tolerated = frozenset(node.pending)
    if role == PeerPortTargetRole.PLANE and node.sub == PeerPortNvueSub.ROOT:
        tolerated |= PEER_PORT_PLANE_ROOT_PENDING_LEAVES
    problems = [f"missing child container {name!r}"
                for name in sorted(node.containers) if name not in containers]
    problems += leaf_problems(peer_port_show_leaves(body), node.leaves,
                              tolerated_missing=tolerated, where=role)
    for name in sorted(node.containers):
        if name not in containers:
            continue
        problems += [
            f"member {name!r} {problem}"
            for problem in leaf_problems(
                peer_port_show_leaves(containers[name]), node.member_leaves,
                allow_not_available=True, where=role)
        ]
    # The keyed lists have no sub-command of their own, so each is verified together with
    # the node that carries it.
    if node.sub == PeerPortNvueSub.LINK_PHY_HEALTH:
        problems += _peer_port_lane_problems(containers.get(PeerPortContainer.LANE, {}))
    if node.sub == PeerPortNvueSub.LINK_PHY_HEALTH_HISTOGRAM:
        problems += _peer_port_histogram_problems(
            containers.get(PeerPortContainer.HISTOGRAM_BUCKETS, {}))
    return problems


def _peer_port_lane_db_pairs(lane_body: Dict) -> List[PeerPortDbPair]:
    """Per-lane NVUE leaves paired with their COUNTERS_DB lane fields."""
    lanes = peer_port_show_containers(lane_body)
    pairs: List[PeerPortDbPair] = []
    for lane in range(PEER_PORT_LANE_COUNT):
        leaves = peer_port_show_leaves(lanes.get(str(lane), {}))
        for leaf, (kind, db_fmt) in PEER_PORT_LANE_LEAF_SPECS.items():
            pairs.append(PeerPortDbPair(f"lane {lane} {leaf}", leaves.get(leaf),
                                        db_fmt.format(lane=lane), kind))
    return pairs


def _peer_port_histogram_db_pairs(buckets_body: Dict) -> List[PeerPortDbPair]:
    """Per-bucket histogram counts paired with their COUNTERS_DB RS-histogram fields."""
    buckets = peer_port_show_containers(buckets_body)
    pairs: List[PeerPortDbPair] = []
    for bucket in range(PEER_PORT_HISTOGRAM_BUCKET_COUNT):
        value = peer_port_show_leaves(buckets.get(str(bucket), {})).get(PEER_PORT_HISTOGRAM_LEAF)
        pairs.append(PeerPortDbPair(f"histogram bucket {bucket}", value,
                                    PEER_PORT_HISTOGRAM_DB_FMT.format(bucket=bucket),
                                    GnmiTypeKind.COUNTER))
    return pairs


def peer_port_db_comparison_pairs(engines, peer_id: str) -> List[PeerPortDbPair]:
    """Every NVUE peer-port leaf that has a COUNTERS_DB field, paired with that field."""
    pairs: List[PeerPortDbPair] = []
    for sub, field_map in (
        (PeerPortNvueSub.COUNTERS, PEER_PORT_COUNTERS_TO_DB_FIELD),
        (PeerPortNvueSub.COUNTERS_LINK, PEER_PORT_COUNTERS_LINK_TO_DB_FIELD),
        (PeerPortNvueSub.LINK_PHY_DETAIL, PEER_PORT_PHY_DETAIL_TO_DB_FIELD),
    ):
        leaves = peer_port_show_leaves(nvue_peer_port_show(engines, peer_id, sub))
        pairs += [PeerPortDbPair(f"{sub} {leaf}", leaves.get(leaf), db_field, GnmiTypeKind.COUNTER)
                  for leaf, db_field in field_map.items()]

    health = nvue_peer_port_show(engines, peer_id, PeerPortNvueSub.LINK_PHY_HEALTH)
    health_leaves = peer_port_show_leaves(health)
    for field_map, kind in ((PEER_PORT_PHY_HEALTH_TO_DB_FIELD, GnmiTypeKind.COUNTER),
                            (PEER_PORT_PHY_HEALTH_BER_TO_DB_FIELD, GnmiTypeKind.DECIMAL)):
        pairs += [PeerPortDbPair(f"{PeerPortNvueSub.LINK_PHY_HEALTH} {leaf}",
                                 health_leaves.get(leaf), db_field, kind)
                  for leaf, db_field in field_map.items()]
    pairs += _peer_port_lane_db_pairs(
        peer_port_show_containers(health).get(PeerPortContainer.LANE, {}))

    histogram = nvue_peer_port_show(engines, peer_id, PeerPortNvueSub.LINK_PHY_HEALTH_HISTOGRAM)
    pairs += _peer_port_histogram_db_pairs(
        peer_port_show_containers(histogram).get(PeerPortContainer.HISTOGRAM_BUCKETS, {}))
    return pairs


def _peer_port_ber_problem(pair: PeerPortDbPair, db_raw: str) -> Optional[str]:
    """Problem comparing one BER pair numerically, or None when the two surfaces agree."""
    def parse(raw) -> Optional[Decimal]:
        try:
            value = Decimal(str(raw).strip())
        except InvalidOperation:
            return None
        return value if value.is_finite() else None

    cli_val, db_val = parse(pair.cli_value), parse(db_raw)
    if cli_val is None or db_val is None:
        label, raw = ((pair.label, pair.cli_value) if cli_val is None
                      else (pair.db_field, db_raw))
        return f"{label}={raw!r} is not a finite decimal"
    shown = f"{pair.label}={pair.cli_value!r} against {pair.db_field}={db_raw!r}"
    zeros = [abs(value) <= PEER_PORT_BER_ZERO_FLOOR for value in (cli_val, db_val)]
    if all(zeros):
        return None
    if any(zeros):
        return f"{shown}: the two surfaces disagree on whether the link has any errors"
    hi, lo = max(abs(cli_val), abs(db_val)), min(abs(cli_val), abs(db_val))
    if hi / lo > PEER_PORT_BER_MAX_RATIO:
        return f"{shown}: more than a decade apart"
    return None


def peer_port_db_pair_problems(
    pairs: List[PeerPortDbPair],
    db_row: Dict[str, str],
    tolerance_pct: float = SAMPLING_JITTER_TOLERANCE_PCT,
    surface: str = "NVUE",
) -> Tuple[List[str], int]:
    """Problems and compared-count for API-vs-DB pairs (absent on both sides is consistent)."""
    problems: List[str] = []
    compared = 0
    for pair in pairs:
        cli_present = pair.cli_value is not None and str(pair.cli_value).strip() != ""
        db_raw = db_row.get(pair.db_field)
        db_present = db_raw is not None and str(db_raw).strip() != ""
        if not cli_present and not db_present:
            # A lane past the operational width: neither surface reports it.
            continue
        if not cli_present:
            problems.append(f"{pair.label}: absent in {surface} while {pair.db_field}={db_raw!r}")
            continue
        if not db_present:
            problems.append(f"{pair.label}={pair.cli_value!r} has no {pair.db_field} in the DB row")
            continue
        if pair.kind == GnmiTypeKind.DECIMAL:
            # A BER is a ratio rather than a counter, so it is compared as a parsed decimal
            # within a decade instead of within the counter sampling tolerance.
            problem = _peer_port_ber_problem(pair, db_raw)
            if problem:
                problems.append(problem)
            compared += 1
            continue
        try:
            cli_val = parse_counter_value(pair.cli_value)
            db_val = parse_counter_value(db_raw)
        except (AssertionError, ValueError) as exc:
            problems.append(f"{pair.label}: unparseable counter ({surface}={pair.cli_value!r}, "
                            f"{pair.db_field}={db_raw!r}): {exc}")
            continue
        if not values_within_tolerance(cli_val, db_val, tolerance_pct):
            problems.append(f"{pair.label}={cli_val} differs from {pair.db_field}={db_val} "
                            f"beyond the {tolerance_pct:.0%} sampling tolerance")
        compared += 1
    return problems, compared


def peer_port_link_db_problems(link_body: Dict, db_row: Dict[str, str]) -> List[str]:
    """Problems comparing `link` speed / width against the DB operational speed and lane count."""
    leaves = peer_port_show_leaves(link_body)
    problems: List[str] = []
    for leaf, db_field, per_unit in (
        (PEER_PORT_SPEED_LEAF, PEER_PORT_SPEED_DB_FIELD, PEER_PORT_SPEED_MBPS_PER_GBPS),
        (PEER_PORT_WIDTH_LEAF, PEER_PORT_LANES_DB_FIELD, 1),
    ):
        shown = leaves.get(leaf, "").strip()
        digits = re.search(r"\d+", shown)
        if not digits:
            problems.append(f"link {leaf}={shown!r} carries no numeric value")
            continue
        try:
            db_val = parse_counter_value(db_row.get(db_field))
        except (AssertionError, ValueError):
            problems.append(f"link {leaf}={shown!r} has no usable {db_field} in the DB row "
                            f"({db_row.get(db_field)!r})")
            continue
        expected = db_val // per_unit
        if int(digits.group()) != expected:
            problems.append(f"link {leaf}={shown!r} does not match {db_field}={db_val} "
                            f"(expected {expected})")
    return problems


def peer_port_plr_bw_loss_problems(detail_body: Dict) -> List[str]:
    """Problems when `plr-bw-loss-percent` does not match `plr-codes-loss` / the documented divisor."""
    leaves = peer_port_show_leaves(detail_body)
    shown = leaves.get(PEER_PORT_PLR_BW_LOSS_LEAF, "").strip()
    codes_loss = leaves.get(PEER_PORT_PLR_CODES_LOSS_LEAF, "").strip()
    if not shown or not codes_loss:
        return [f"phy detail is missing {PEER_PORT_PLR_BW_LOSS_LEAF!r} / "
                f"{PEER_PORT_PLR_CODES_LOSS_LEAF!r} (got {shown!r} / {codes_loss!r})"]
    try:
        measured = float(shown)
        expected = float(codes_loss) / PEER_PORT_PLR_BW_LOSS_DIVISOR
    except ValueError as exc:
        return [f"{PEER_PORT_PLR_BW_LOSS_LEAF} / {PEER_PORT_PLR_CODES_LOSS_LEAF} unparseable "
                f"({shown!r} / {codes_loss!r}): {exc}"]
    if not values_within_tolerance(measured, expected, SAMPLING_JITTER_TOLERANCE_PCT):
        return [f"{PEER_PORT_PLR_BW_LOSS_LEAF}={measured} != {PEER_PORT_PLR_CODES_LOSS_LEAF} "
                f"({codes_loss}) / {PEER_PORT_PLR_BW_LOSS_DIVISOR:.0e} = {expected}"]
    return []


def otel_peer_port_rows(snap: ibo.OtelSnapshot) -> Dict[str, Dict[str, str]]:
    """``{peer_id: {leaf: value}}`` of every peer exporting `nvswitch_peer_port_node_info`.

    A row carries the identity labels under their NVUE and gNMI leaf names and the additive
    counters, the same shape as the NVUE / gNMI listings.
    """
    rows: Dict[str, Dict[str, str]] = {}
    for peer_id in sorted(snap.keys(PEER_PORT_OTEL_NODE_INFO, OtelLabel.INTERFACE)):
        row = dict(ibo.peer_identity_leaf_values(snap, peer_id))
        for leaf, spec in PEER_PORT_OTEL_ADDITIVE_LEAVES.items():
            value = snap.value(spec, OtelLabel.INTERFACE, peer_id)
            if value is not None:
                row[leaf] = str(int(value))
        rows[peer_id] = row
    return rows


def _otel_rendering(value: Any) -> str:
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def otel_leaf_readings(snap: ibo.OtelSnapshot, group: OtelGroup, key: str,
                       member: Optional[str] = None) -> Dict[str, List[str]]:
    """``{leaf: [rendering, ...]}`` of every leaf of ``group`` the entity currently exports."""
    readings: Dict[str, List[str]] = {}
    for leaf, spec in group.leaves.items():
        values = snap.leaf_values(spec, group.key_label, key, group.member_label, member)
        if values:
            readings[leaf] = [_otel_rendering(v) for v in values]
    return readings


def otel_leaf_parity_problems(snap: ibo.OtelSnapshot, group: OtelGroup, key: str,
                              expected: Dict[str, str], shown: Dict[str, str], other_label: str,
                              member: Optional[str] = None,
                              single_series: bool = False) -> Tuple[List[str], int]:
    """Leaves ``other_label`` shows for ``key`` must read the same over OTEL.

    Compared with the same kind-aware rule as the CLI and gNMI parity; a leaf fanned out over
    several series (one per direction) agrees when any of them does. With ``single_series`` the
    leaf must be exported by exactly one current series, so a correct duplicate cannot hide a
    wrong or stale one. Returns ``(problems, compared)``.
    """
    readings = otel_leaf_readings(snap, group, key, member)
    where = f"{key} {group.member_label}={member}" if member is not None else key
    problems: List[str] = []
    compared = 0
    for leaf, kind in sorted(expected.items()):
        if component_source_absent(shown.get(leaf)):
            continue
        spec = group.leaves.get(leaf)
        if spec is None:
            problems.append(f"{where}: {leaf} has no OTEL counterpart defined")
            continue
        if leaf not in readings:
            problems.append(f"{where}: {leaf} is shown over {other_label} ({shown[leaf]!r}) but "
                            f"not exported over OTEL ({spec.metric})")
            continue
        compared += 1
        if single_series and len(readings[leaf]) != 1:
            problems.append(f"{where}: leaf {leaf!r} is exported by {len(readings[leaf])} "
                            f"series ({spec.metric}), expected exactly one: {readings[leaf]!r}")
            continue
        if not any(_leaf_values_agree(kind, shown[leaf], value) for value in readings[leaf]):
            problems.append(f"{where}: leaf {leaf!r} differs: {other_label}={shown[leaf]!r}, "
                            f"{API_OTEL}={readings[leaf]!r}")
    return problems, compared


def otel_member_labels(snap: ibo.OtelSnapshot, group: OtelGroup, key: str) -> Dict[str, str]:
    """``{member index: member label value}`` over every leaf of a list group."""
    labels: Dict[str, str] = {}
    for spec in group.leaves.values():
        for member in snap.members(spec, group.key_label, key, group.member_label):
            labels[list_member_index(member)] = member
    return labels


def peer_port_otel_show_problems(snap: ibo.OtelSnapshot, peer_id: str, node: NvueShowNode,
                                 body: Dict, role: str = PeerPortTargetRole.AGGREGATED) -> List[str]:
    """Problems when the OTEL export of a peer lacks what `nv show peer-port <id> <sub>` shows.

    Every leaf the sub-command documents must be exported; the ones whose tier the HLD leaves
    open are required only when the CLI shows them on this row. The lanes and histogram buckets
    must match the CLI one for one.
    """
    if node.sub in PEER_PORT_OTEL_UNEXPORTED_NVUE_SUBS:
        return []
    table = PEER_PORT_OTEL_LEAVES_BY_NVUE_SUB.get(node.sub)
    if table is None:
        return [f"{peer_id}: `{node.sub}` has no OTEL counterpart defined"]
    tolerated = frozenset(node.pending)
    if role == PeerPortTargetRole.PLANE and node.sub == PeerPortNvueSub.ROOT:
        tolerated |= PEER_PORT_PLANE_ROOT_PENDING_LEAVES
    shown = peer_port_show_leaves(body)
    leaves = [leaf for leaf in table
              if leaf not in tolerated or not component_source_absent(shown.get(leaf))]
    problems = ibo.group_problems(snap, OtelGroup(OtelLabel.INTERFACE, table), peer_id,
                                  leaves=leaves)
    containers = peer_port_show_containers(body)

    if node.sub == PeerPortNvueSub.LINK_PHY_HEALTH:
        lane_group = OtelGroup(OtelLabel.INTERFACE, PEER_PORT_OTEL_LANE_LEAVES, OtelLabel.LANE)
        cli_lane_members = peer_port_show_containers(
            containers.get(PeerPortContainer.LANE, {}))
        cli_lanes = sorted(list_member_index(k) for k, member in cli_lane_members.items()
                           if any(peer_port_show_leaves(member).values()))
        otel_lanes = otel_member_labels(snap, lane_group, peer_id)
        if len(otel_lanes) != len(cli_lanes):
            problems.append(f"{peer_id}: OTEL exports {len(otel_lanes)} lane(s) "
                            f"{sorted(otel_lanes)} but the CLI shows {len(cli_lanes)} populated "
                            f"lane(s) {cli_lanes}; lanes past the operational width are null on "
                            f"the CLI and not exported, and the lane numbering is not compared")
        for _, lane in sorted(otel_lanes.items()):
            problems += ibo.group_problems(snap, lane_group, peer_id, members=[lane])

    if node.sub == PeerPortNvueSub.LINK_PHY_HEALTH_HISTOGRAM:
        cli_buckets = peer_port_show_containers(
            containers.get(PeerPortContainer.HISTOGRAM_BUCKETS, {}))
        series = snap.current(PEER_PORT_OTEL_RS_HISTOGRAM, **{OtelLabel.INTERFACE: peer_id})
        if len(series) != 1:
            problems.append(f"{peer_id}: {PEER_PORT_OTEL_RS_HISTOGRAM} has {len(series)} current "
                            f"series (expected 1)")
        else:
            # An OTLP histogram carries one more bucket than explicit bounds (the overflow
            # bucket), so the CLI buckets are compared with the explicit bounds.
            bounds = series[0].explicit_bounds
            if len(series[0].bucket_counts) != len(bounds) + 1:
                problems.append(f"{peer_id}: {PEER_PORT_OTEL_RS_HISTOGRAM} has "
                                f"{len(series[0].bucket_counts)} bucket(s) for {len(bounds)} "
                                f"explicit bound(s); an OTLP histogram needs bounds + 1")
            if len(bounds) != len(cli_buckets):
                problems.append(f"{peer_id}: {PEER_PORT_OTEL_RS_HISTOGRAM} has {len(bounds)} "
                                f"explicit bound(s) {bounds} but the CLI shows "
                                f"{len(cli_buckets)} bucket(s)")
    return problems


def otel_peer_port_db_problems(engines, peer_id: str, label: str) -> Tuple[List[str], int]:
    """Problems where the OTEL export of a peer disagrees with its COUNTERS_DB rows.

    The PEER_COUNTERS row is read before and after one fresh collection window, so a sampled
    value must be one the DB held in that window; the identity labels must equal the
    PEER_COUNTERS / PEER_PORT_MAPPING fields they are read from. Returns
    ``(problems, compared)``.
    """
    before = read_peer_port_row(engines, peer_id)
    snap = ibo.active_surface().snapshot(label, fresh=True)
    after = read_peer_port_row(engines, peer_id)
    mapping = read_peer_port_mapping(engines, peer_id)
    attach_dict(f"{peer_id} DB rows around OTEL window {label}",
                {"before": before, "after": after, "mapping": mapping})
    fields = [f for f in PEER_PORT_OTEL_METRIC_BY_DB_FIELD if f in before and f in after]
    problems = ibo.db_parity_problems(snap, peer_id, before, after,
                                      PEER_PORT_OTEL_METRIC_BY_DB_FIELD, fields)
    problems += ibo.peer_identity_problems(snap, peer_id, {**mapping, **after})
    return problems, len(fields)


def otel_peer_port_plr_bw_loss_problems(snap: ibo.OtelSnapshot, peer_id: str) -> List[str]:
    """The exported PLR bandwidth loss must be derived from the exported PLR codes loss."""
    body = {}
    for leaf, spec in ((PEER_PORT_PLR_BW_LOSS_LEAF, PEER_PORT_OTEL_PLR_BW_LOSS),
                       (PEER_PORT_PLR_CODES_LOSS_LEAF, PEER_PORT_OTEL_PLR_CODES_LOSS)):
        value = snap.value(spec, OtelLabel.INTERFACE, peer_id)
        if value is not None:
            body[leaf] = _otel_rendering(value)
    return [f"{peer_id} over OTEL: {problem}" for problem in peer_port_plr_bw_loss_problems(body)]


def list_peer_ports_via_api(api: str, engines, gnmi_client: GnmiClient) -> Dict[str, Dict[str, str]]:
    """Enumerate peer-ports across APIs as ``{peer_id: {leaf: value}}``."""
    assert api in ALL_APIS, f"Unsupported api: {api!r}"
    if api == API_NVUE_CLI:
        return nvue_show_peer_ports(engines)
    if api == API_GNMIC:
        return gnmi_get_peer_port_list(gnmi_client)
    if api == API_OTEL:
        return otel_peer_port_rows(ibo.active_surface().snapshot("peer-ports", fresh=True))
    raise ValueError(api)


def get_peer_port_via_api(api: str, engines, gnmi_client: GnmiClient, peer_id: str) -> Dict[str, str]:
    """Read a single peer-port entry across APIs as a flat dict."""
    assert api in ALL_APIS, f"Unsupported api: {api!r}"
    if api == API_NVUE_CLI:
        return nvue_show_peer_port(engines, peer_id)
    if api == API_GNMIC:
        return gnmi_get_peer_port(gnmi_client, peer_id)
    if api == API_OTEL:
        rows = otel_peer_port_rows(ibo.active_surface().snapshot(f"peer-port-{peer_id}", fresh=True))
        return rows.get(peer_id, {})
    raise ValueError(api)


def classify_peer_type(peer_id: str, fields: Optional[Dict[str, str]] = None) -> str:
    """Return 'GPU'/'HCA'/'' for a peer-port (by id-prefix; explicit peer-type wins)."""
    if fields:
        explicit = str(fields.get(PeerPortFields.PEER_TYPE, "")).strip().upper()
        if explicit in (PeerType.GPU, PeerType.HCA):
            return explicit
    pid = str(peer_id).strip().lower()
    for prefix, ptype in PeerType.ID_PREFIXES.items():
        if pid.startswith(prefix):
            return ptype
    return ""


def peer_entry_type(fields: Dict[str, str], peer_id: str = "") -> str:
    """Classify a peer-port entry as 'GPU'/'HCA' ('' if undecidable), via name/id-prefix."""
    explicit = str(fields.get(PeerPortFields.PEER_TYPE, "")).strip().upper()
    if explicit in (PeerType.GPU, PeerType.HCA):
        return explicit
    for key in ("name", "peer_port_name", "peer-port-name", PeerPortFields.HCA_ALIAS_FIELD,
                "peer_component", "peer-component"):
        ptype = classify_peer_type(str(fields.get(key, "")))
        if ptype:
            return ptype
    return classify_peer_type(peer_id, fields) if peer_id else ""


def peer_entry_aport(fields: Dict[str, str]) -> str:
    """Return the Aport a peer-port entry is associated with (first candidate hit)."""
    for cand in PeerPortFields.APORT_REF_CANDIDATES:
        if fields.get(cand):
            return str(fields[cand]).strip()
    return ""


def peer_row_aports(fields: Dict[str, str]) -> set:
    """Every aport identifier a DB/API entry exposes (sw-alias and IB-name)."""
    out = set()
    for cand in (PeerPortFields.ASSOCIATED_SWITCH_PORT, PeerPortFields.SWITCH_PORT_ALIAS_FIELD,
                 PeerPortFields.APORT_NAME_FIELD):
        val = str(fields.get(cand, "")).strip()
        if val:
            out.add(val)
    return out


def peer_row_tier(fields: Dict[str, str]) -> str:
    """Return the peer-port tier ('plane'/'aggregated'/'') of a DB row."""
    return str(fields.get(PeerPortFields.TIER_FIELD, "")).strip().lower()


def peer_row_parent(fields: Dict[str, str]) -> str:
    """Return the parent (aggregated) peer-id a plane DB row rolls up into ('' if none)."""
    return str(fields.get(PeerPortFields.PARENT_FIELD, "")).strip()


def hca_aggregate_counters_partitioned_by_planes(
    aggregate: Dict[str, str],
    plane_rows: Dict[str, Dict[str, str]],
    member_pids: List[str],
) -> bool:
    """True when plane DB rows share the aggregate's ``port_guid`` (counters then sum)."""
    agg_guid = str(aggregate.get("port_guid") or "").strip().lower()
    if not agg_guid:
        return False
    for pid in member_pids:
        row = plane_rows.get(pid, {})
        plane_guid = str(row.get("port_guid") or "").strip().lower()
        if plane_guid != agg_guid:
            return False
    return True


def peer_entry_identity(fields: Dict[str, str], fallback: str = "") -> str:
    """Return the identity (e.g. node GUID) of a peer-port entry (first candidate hit)."""
    for cand in PeerPortFields.IDENTITY_CANDIDATES:
        if fields.get(cand):
            return str(fields[cand]).strip()
    return fallback


def peer_entry_node_guid(fields: Dict[str, str]) -> str:
    """Return the HCA node GUID of a peer-port entry ('' if absent)."""
    for cand in ("node-guid", "node_guid"):
        if fields.get(cand):
            return str(fields[cand]).strip().lower()
    return ""


def aggregated_hca_node_guids(entries: Dict[str, Dict[str, str]]) -> set:
    """Unique lowercase node GUIDs across aggregated HCA peer-port entries."""
    return {g for g in (peer_entry_node_guid(f) for f in entries.values()) if g}


def filter_peer_entries_by_type(entries: Dict[str, Dict[str, str]], peer_type: str) -> Dict[str, Dict[str, str]]:
    """Sub-select peer entries whose classified type equals `peer_type` (by id-prefix or field)."""
    want = peer_type.strip().upper()
    return {pid: f for pid, f in entries.items() if classify_peer_type(pid, f) == want}


def aggregated_peer_entries(entries: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    """Keep only aggregated peer entries, dropping per-plane (``...plN``) members."""
    return {pid: f for pid, f in entries.items() if not is_plane_port_name(pid)}


def plane_peer_ids_for_parent(all_peer_ids, parent_id: str) -> List[str]:
    """Return sorted plane-tier peer ids that belong to an aggregated parent (``hca5p1`` -> ``hca5p1pl1``)."""
    prefix = f"{parent_id}pl"
    return sorted(
        str(pid) for pid in all_peer_ids
        if str(pid).startswith(prefix) and is_plane_port_name(str(pid))
    )


def list_peer_port_keys(engines) -> List[str]:
    """Return COUNTERS_DB keys for ingested peer-ports (``PEER_COUNTERS:*``)."""
    out = Tools.DatabaseTool.sonic_db_cli_get_keys(
        engine=engines.dut,
        asic="",
        db_name=SystemDbCli.COUNTERS_DB,
        grep_str=SystemDbCli.PEER_PORT_KEY_PREFIX,
    )
    return [line.strip() for line in (out or "").splitlines() if line.strip()]


def peer_id_from_key(key: str) -> str:
    """Strip the ``PEER_COUNTERS:`` prefix off a Redis key."""
    return key.split(SystemDbCli.PEER_PORT_KEY_PREFIX, 1)[-1].strip().strip('"')


def read_peer_port_row(engines, peer_id: str) -> Dict[str, str]:
    """hgetall the COUNTERS_DB ``PEER_COUNTERS`` row for one peer-port."""
    key = SystemDbCli.PEER_PORT_COUNTERS_KEY_FMT.format(peer_id=peer_id)
    return db_hgetall(engines, SystemDbCli.COUNTERS_DB, key)


def read_peer_port_mapping(engines, peer_id: str) -> Dict[str, str]:
    """hgetall the COUNTERS_DB ``PEER_PORT_MAPPING`` row for one peer-port."""
    key = SystemDbCli.PEER_PORT_MAPPING_KEY_FMT.format(peer_id=peer_id)
    return db_hgetall(engines, SystemDbCli.COUNTERS_DB, key)


def list_hca_peer_rows(engines, tier: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """Return ``{peer_id: row}`` for HCA peers, merging PEER_COUNTERS + PEER_PORT_MAPPING (optional tier filter)."""
    want_tier = tier.strip().lower() if tier else None
    out: Dict[str, Dict[str, str]] = {}
    for key in list_peer_port_keys(engines):
        pid = peer_id_from_key(key)
        row = db_hgetall(engines, SystemDbCli.COUNTERS_DB, key)
        if classify_peer_type(pid, row) != PeerType.HCA:
            continue
        if want_tier and peer_row_tier(row) != want_tier:
            continue
        mapping = read_peer_port_mapping(engines, pid)
        out[pid] = {**mapping, **row}
    return out


def list_peer_telemetry_health_keys(engines) -> List[str]:
    out = Tools.DatabaseTool.sonic_db_cli_get_keys(
        engine=engines.dut,
        asic="",
        db_name=SystemDbCli.STATE_DB,
        grep_str=SystemDbCli.PEER_TELEMETRY_HEALTH_KEY_GREP,
    )
    return [line.strip() for line in (out or "").splitlines() if line.strip()]


def classify_peer_health(raw: Dict[str, str]) -> str:
    """Classify a PEER_PORT_TELEMETRY_HEALTH row as 'healthy'/'degraded'/'unknown'."""
    if PeerTelemetryHealth.HEALTH_FIELD in raw:
        vals = [raw[PeerTelemetryHealth.HEALTH_FIELD]]
    else:
        vals = list(raw.values())
    text = " ".join(str(v).strip().lower() for v in vals)
    if any(token in text for token in PeerTelemetryHealth.DEGRADED_VALUES):
        return PeerTelemetryHealth.DEGRADED
    tokens = set(text.split())
    if any(h in tokens or h == text.strip() for h in PeerTelemetryHealth.HEALTHY_VALUES):
        return PeerTelemetryHealth.HEALTHY
    return PeerTelemetryHealth.UNKNOWN


def read_peer_telemetry_health(engines) -> Tuple[str, Dict[str, str]]:
    """Read gpu-telemetry health from STATE_DB; returns ``(classification, raw_row)``."""
    keys = list_peer_telemetry_health_keys(engines) or [SystemDbCli.PEER_TELEMETRY_HEALTH_KEY]
    raw: Dict[str, str] = {}
    for key in keys:
        raw.update(db_hgetall(engines, SystemDbCli.STATE_DB, key))
    return classify_peer_health(raw), raw


def read_gpu_telemetry_log(engines, tail: int = 400, since: Optional[str] = None) -> str:
    """Return peer-telemetry (consumer) journal lines.

    With ``since`` (a ``journalctl``-compatible timestamp) only entries from that moment on
    are returned - use it to scope a search to a specific window and skip stale history.
    Otherwise the last ``tail`` lines are returned.
    """
    if since:
        cmd = f"sudo journalctl -u {PEER_TELEMETRY_SERVICE} --no-pager --since '{since}' 2>&1"
    else:
        cmd = f"sudo journalctl -u {PEER_TELEMETRY_SERVICE} --no-pager -n {tail} 2>&1"
    return engines.dut.run_cmd(cmd) or ""


def dut_journal_since_marker(engines) -> str:
    """DUT-local timestamp (``YYYY-MM-DD HH:MM:SS``) usable as a ``journalctl --since`` boundary."""
    return (engines.dut.run_cmd("date '+%Y-%m-%d %H:%M:%S'") or "").strip()


def restart_gpu_telemetry(engines, settle_sec: int = 15) -> None:
    """Restart the peer-telemetry (consumer) service and settle (drives §10.8)."""
    with allure.step(f"Restart the {PEER_TELEMETRY_SERVICE} service"):
        engines.dut.run_cmd(f"sudo systemctl restart {PEER_TELEMETRY_SERVICE}")
    time.sleep(settle_sec)


def hca_peer_switch_aport(engines, peer) -> str:
    """Resolve the switch Aport alias (e.g. ``swA2p1``) an HCA peer is cabled to ('' if unresolved)."""
    peer_id = str(getattr(peer, "peer_id", peer))
    mapping = read_peer_port_mapping(engines, peer_id)
    alias = str(mapping.get(PeerPortFields.SWITCH_PORT_ALIAS_FIELD, "")).strip()
    if not alias:
        rows = list_hca_peer_rows(engines)
        alias = next(
            (a for a in sorted(peer_row_aports(rows.get(peer_id, {}))) if str(a).lower().startswith("sw")),
            "",
        )
    return alias


def pick_hca_peer_with_switch_port(engines, hca_rows: Dict[str, Dict[str, str]]) -> str:
    """First HCA peer-id whose PEER_PORT_MAPPING resolves a switch Aport alias ('' if none)."""
    return next((pid for pid in sorted(hca_rows) if hca_peer_switch_aport(engines, pid)), "")


def _set_switch_port_link(engines, aport: str, *, up: bool) -> None:
    """`nv set interface <aport> link state up|down` + apply, then let it settle."""
    state = "up" if up else "down"
    with allure.step(f"nv set interface {aport} link state {state}"):
        engines.dut.run_cmd(f"nv set interface {aport} link state {state}")
        NvueGeneralCli.apply_config(engine=engines.dut, option='-y')
    time.sleep(BER_INJECT_SETTLE_SEC)


def simulate_hca_peer_down(engines, peer) -> None:
    """Bring an HCA peer down by admin-downing its switch-side Aport (alias cached)."""
    peer_id = str(getattr(peer, "peer_id", peer))
    aport = hca_peer_switch_aport(engines, peer)
    assert aport, f"Could not resolve switch Aport alias for HCA peer {peer_id!r}"
    # Aport aliases are DB-derived and always [A-Za-z0-9]+; guard the shell boundary so
    # a malformed alias fails loudly here rather than corrupting the marker file / sed
    # cleanup (mirrors the validation in restore_downed_hca_aports).
    assert re.fullmatch(r"[A-Za-z0-9]+", aport), \
        f"Unexpected switch Aport alias {aport!r} for HCA peer {peer_id!r}"
    _HCA_DOWNED_APORTS[peer_id] = aport
    # Owner-only marker so the crash-recovery list can't be read/tampered with by
    # another user on a shared DUT. Perms are best-effort (umask on create + chmod,
    # both non-fatal via ';'): recording the aport (the final echo) must always run
    # so the teardown can restore the port, even if a stale file blocks the chmod.
    engines.dut.run_cmd(
        f"( umask 077; touch {_HCA_DOWNED_APORTS_FILE} ) 2>/dev/null; "
        f"chmod 600 {_HCA_DOWNED_APORTS_FILE} 2>/dev/null; "
        f"echo {aport} >> {_HCA_DOWNED_APORTS_FILE}"
    )
    _set_switch_port_link(engines, aport, up=False)


def restore_hca_peer(engines, peer) -> None:
    """Counterpart of `simulate_hca_peer_down`: bring the switch Aport back up so the peer returns."""
    peer_id = str(getattr(peer, "peer_id", peer))
    aport = _HCA_DOWNED_APORTS.pop(peer_id, None) or hca_peer_switch_aport(engines, peer)
    assert aport, f"Could not resolve switch Aport alias to restore HCA peer {peer_id!r}"
    # Guard the shell/sed boundary: an [A-Za-z0-9]+ alias cannot carry regex/shell
    # metacharacters, so the sed '/^{aport}$/d' cleanup stays safe and exact.
    assert re.fullmatch(r"[A-Za-z0-9]+", aport), \
        f"Unexpected switch Aport alias {aport!r} to restore HCA peer {peer_id!r}"
    _set_switch_port_link(engines, aport, up=True)
    engines.dut.run_cmd(f"sed -i '/^{aport}$/d' {_HCA_DOWNED_APORTS_FILE} 2>/dev/null || true")


def restore_downed_hca_aports(engines) -> None:
    """Bring back any switch Aports left admin-down by an aborted simulate_hca_peer_down (reads the DUT-side marker; only touches ports we recorded)."""
    listing = engines.dut.run_cmd(f"cat {_HCA_DOWNED_APORTS_FILE} 2>/dev/null") or ""
    aports = [ln.strip() for ln in listing.splitlines()
              if re.fullmatch(r"[A-Za-z0-9]+", ln.strip())]
    if not aports:
        return
    with allure.step(f"Restore Aports left admin-down: {aports}"):
        for aport in aports:
            _set_switch_port_link(engines, aport, up=True)
        engines.dut.run_cmd(f"rm -f {_HCA_DOWNED_APORTS_FILE}")
    _HCA_DOWNED_APORTS.clear()


def make_nmxt_lite_unreachable(engines) -> None:
    """Make NMX-T-for-IB unreachable to peer-telemetry by masking + stopping the unit."""
    with allure.step(f"Mask + stop {NMXT_IB_SERVICE} (defeats the peer-telemetry watchdog)"):
        engines.dut.run_cmd(f"sudo systemctl mask {NMXT_IB_SERVICE}")
        engines.dut.run_cmd(f"sudo systemctl stop {NMXT_IB_SERVICE} 2>/dev/null")
        engines.dut.run_cmd(f"sudo docker stop {NMXT_IB_CONTAINER} 2>/dev/null")


def restore_nmxt_lite(engines) -> None:
    """Counterpart of `make_nmxt_lite_unreachable`: unmask and start the backend."""
    with allure.step(f"Unmask + start {NMXT_IB_SERVICE}"):
        engines.dut.run_cmd(f"sudo systemctl unmask {NMXT_IB_SERVICE}")
        engines.dut.run_cmd(f"sudo systemctl start {NMXT_IB_SERVICE}")


# HTTP-over-unix-socket server impersonating NMX-T-for-IB (fronts telemetry +
# control sockets) plus a bare listener on the gRPC connector socket. Without a
# live connector the daemon times out and wipes the last-good rows, so we only
# need to accept and hold the connection open (no gRPC responses - dev-confirmed,
# Joe). Argv: ctrl_sock tele_sock csv connector_sock.
_NMXT_FAKE_SERVER = r'''
import os, sys, socket, threading, time
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, UnixStreamServer

CTRL, TELE, CSV, CONN = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
HEALTH = b'{"message":"OK","status":0}'
with open(CSV, "rb") as fh:
    BODY = fh.read()

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a):
        pass
    def _w(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path == "/healthcheck":
            self._w(HEALTH, "application/json")
        elif self.path.startswith("/csv/"):
            self._w(BODY, "text/csv")
            sys.stderr.write("xcset-served\n")
            sys.stderr.flush()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

class S(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    def get_request(self):
        req, _ = super().get_request()
        return req, ("local", 0)

def serve(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    srv = S(path, H)
    os.chmod(path, 0o666)
    srv.serve_forever()

def serve_connector(path):
    # gRPC connector: the daemon only needs a live peer to avoid the connect
    # timeout that would wipe the rows, so accept and hold connections open
    # without ever replying. Each connection is held on its own thread and
    # closed once the peer goes away, so reconnect churn can't leak FDs.
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    os.chmod(path, 0o666)
    sock.listen(128)

    def hold(conn):
        try:
            while conn.recv(4096):
                pass  # drain and ignore - never reply
        except OSError:
            pass
        finally:
            conn.close()

    while True:
        conn, _ = sock.accept()
        threading.Thread(target=hold, args=(conn,), daemon=True).start()

for _p in (CTRL, TELE):
    threading.Thread(target=serve, args=(_p,), daemon=True).start()
threading.Thread(target=serve_connector, args=(CONN,), daemon=True).start()
time.sleep(3600)
'''


_NMXT_FAKE_SCRIPT_PATH = "/tmp/peerport_nmxt_fake.py"


_NMXT_FAKE_CSV_PATH = "/tmp/peerport_xcset_fake.csv"


_NMXT_FAKE_PID_PATH = "/tmp/peerport_nmxt_fake.pid"


_NMXT_FAKE_LOG_PATH = "/tmp/peerport_nmxt_fake.log"


# Structurally broken xcset (header advertises 6 columns; rows do not match).
_MALFORMED_XCSET = (
    "timestamp,node_guid,port_guid,port_num,aport,device_id\n"
    "NOT_A_TIMESTAMP,0xbad,0xbad,1\n"
    "garbage,,,,,extra,columns,@@@\n"
)


def _put_remote_file(engines, remote_path: str, contents: str) -> None:
    """Write `contents` to `remote_path` on the DUT via base64."""
    blob = base64.b64encode(contents.encode()).decode()
    engines.dut.run_cmd(f"echo {blob} | base64 -d | sudo tee {remote_path} >/dev/null")


def inject_malformed_hca_xcset(engines) -> None:
    """Serve a malformed HCA xcset (via the interceptor) so peer-telemetry hits the CSV parse-failure path."""
    inject_hca_xcset(engines, _MALFORMED_XCSET)


def restore_valid_hca_xcset(engines) -> None:
    """Tear down the interceptor and bring the real NMX-T-for-IB backend back."""
    with allure.step("Stop the interceptor and restore the real backend"):
        engines.dut.run_cmd(
            f"sudo bash -c 'kill $(cat {_NMXT_FAKE_PID_PATH} 2>/dev/null) 2>/dev/null; "
            "pkill -f \"[p]eerport_nmxt_fake.py\" 2>/dev/null; true'"
        )
        engines.dut.run_cmd(
            f"sudo rm -f {NMXT_XCSET_SOCKET} {NMXT_CONTROL_SOCKET} {NMXT_CONNECTOR_SOCKET}"
        )
        engines.dut.run_cmd(
            f"sudo rm -f {_NMXT_FAKE_CSV_PATH} {_NMXT_FAKE_PID_PATH} "
            f"{_NMXT_FAKE_SCRIPT_PATH} {_NMXT_FAKE_LOG_PATH}"
        )
        engines.dut.run_cmd(f"sudo systemctl unmask {NMXT_IB_SERVICE}")
        # restart, not start: we just removed the sockets, and `start` is a no-op when the
        # service is already running - which would leave the backend with no live sockets.
        engines.dut.run_cmd(f"sudo systemctl restart {NMXT_IB_SERVICE}")


def hca_xcset_interceptor_active(engines) -> bool:
    """True only when the fake NMX-T interceptor is genuinely active: a live fake process or a masked real backend.

    Stale ``/tmp`` artifacts alone do NOT count - they intercept nothing, and treating them as
    active would trigger restore_valid_hca_xcset against a healthy backend (which is destructive).
    """
    # The bracketed first char keeps the pattern from matching pgrep's own shell command line.
    if "yes" in (engines.dut.run_cmd(
            "pgrep -f '[p]eerport_nmxt_fake.py' >/dev/null 2>&1 && echo yes || echo no") or ""):
        return True
    masked = (engines.dut.run_cmd(f"systemctl is-enabled {NMXT_IB_SERVICE} 2>&1") or "").lower()
    return "masked" in masked


def ensure_valid_hca_xcset_backend(engines) -> bool:
    """Restore the real NMX-T-for-IB backend if an interceptor is active; True when restored."""
    if not hca_xcset_interceptor_active(engines):
        return False
    restore_valid_hca_xcset(engines)
    return True


def wait_for_peer_telemetry_healthy(
    engines,
    timeout_sec: Optional[int] = None,
) -> Tuple[str, Dict[str, str]]:
    """Poll PEER_PORT_TELEMETRY_HEALTH until healthy or ``timeout_sec`` elapses.

    A cold enable warms up through a transient (``CSV parse failed`` -> ``telemetry_status=-1``
    -> ``OK``) before health settles, so the default bound is sized for that warm-up (returns
    as soon as healthy - typically well under a minute).
    """
    bound = timeout_sec or (10 + PEER_TELEMETRY_SAMPLING_SEC * 5)
    deadline = time.time() + bound
    health, raw = read_peer_telemetry_health(engines)
    while health != PeerTelemetryHealth.HEALTHY and time.time() < deadline:
        time.sleep(min(5, max(1, bound // 10)))
        health, raw = read_peer_telemetry_health(engines)
    return health, raw


def wait_for_nmxt_ib_active(engines, timeout_sec: int = 120) -> bool:
    """Wait until ``nmx-t-ib.service`` is active, unmasking/starting it if left masked."""
    ensure_valid_hca_xcset_backend(engines)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        active = (engines.dut.run_cmd(f"systemctl is-active {NMXT_IB_SERVICE} 2>&1") or "").strip()
        if active == "active":
            return True
        enabled = (engines.dut.run_cmd(f"systemctl is-enabled {NMXT_IB_SERVICE} 2>&1") or "").lower()
        if "masked" in enabled:
            engines.dut.run_cmd(f"sudo systemctl unmask {NMXT_IB_SERVICE}")
        engines.dut.run_cmd(f"sudo systemctl start {NMXT_IB_SERVICE} 2>/dev/null")
        time.sleep(5)
    return False


def wait_for_nmxt_backend_reachable(engines, timeout_sec: int = 120, quiet_sec: int = 10) -> bool:
    """Wait until nmx-t-ib is up and peer-telemetry has stopped logging backend connection errors.

    ``systemctl start nmx-t-ib`` returns before the unit's sockets accept connections, and until
    they do the daemon keeps logging ``Connection error: GET ... on /var/run/nmx-t/ib/*.sock`` at
    error level. Callers restoring the backend use this to hold a LogAnalyzer ignore window open
    until the recovery is genuinely quiet, so those expected errors are not attributed to the
    post-recovery validation. Returns False if the unit does not reach active or the journal
    does not go quiet within ``timeout_sec`` total (the whole call is bounded by it).
    """
    deadline = time.time() + timeout_sec
    if not wait_for_nmxt_ib_active(engines, timeout_sec=timeout_sec):
        return False
    # Only probe while a FULL quiet window still fits: a truncated one would read a few
    # error-free seconds as quiet and let the caller close its ignore window too early.
    while deadline - time.time() >= quiet_sec:
        since = dut_journal_since_marker(engines)
        time.sleep(quiet_sec)
        log = (read_gpu_telemetry_log(engines, since=since) or "").lower()
        if NMXT_CONNECTION_ERROR_TOKEN not in log:
            return True
    return False


def wait_for_hca_peer_rows(
    engines,
    *,
    tier: Optional[str] = None,
    required: Optional[set] = None,
    timeout_sec: Optional[int] = None,
    poll_sec: Optional[int] = None,
) -> Tuple[Dict[str, Dict[str, str]], set]:
    """Poll COUNTERS_DB until ``required`` HCA peer ids are present; returns ``(rows, still_missing)``."""
    want = set(required) if required is not None else None
    cadence = poll_sec or min(PEER_TELEMETRY_SAMPLING_SEC, 5)
    bound = timeout_sec or (10 + PEER_TELEMETRY_SAMPLING_SEC * 2)
    deadline = time.time() + bound
    rows: Dict[str, Dict[str, str]] = {}
    missing: set = set()
    while time.time() < deadline:
        rows = list_hca_peer_rows(engines, tier=tier)
        if want is None:
            if rows:
                return rows, set()
        else:
            missing = want - set(rows)
            if not missing:
                return rows, set()
        time.sleep(cadence)
    rows = list_hca_peer_rows(engines, tier=tier)
    missing = (want - set(rows)) if want is not None else set()
    return rows, missing


def _split_xcset_csv(raw: str) -> Tuple[List[str], List[str], List[List[str]]]:
    """Split an xcset response into ``(preamble_lines, header, data_rows)``."""
    lines = (raw or "").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("timestamp,")), None)
    if start is None:
        return [], [], []
    rows = [r for r in csv.reader(io.StringIO("\n".join(lines[start:]))) if r]
    if not rows:
        return lines[:start], [], []
    return lines[:start], rows[0], rows[1:]


def _render_xcset_csv(preamble: List[str], header: List[str], rows: List[List[str]]) -> str:
    """Re-render an xcset response from the parts `_split_xcset_csv` produced."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return "\n".join(preamble + [buffer.getvalue().rstrip("\n")]) + "\n"


def _parse_xcset_csv(raw: str) -> List[Dict[str, str]]:
    """Parse the NMX-T-for-IB CSV into column->value dicts (skips noise/short rows)."""
    _, header, rows = _split_xcset_csv(raw)
    return [dict(zip(header, row)) for row in rows if len(row) == len(header)]


def _is_hca_xcset_row(row: Dict[str, str]) -> bool:
    """True for an HCA peer row, excluding the switch's own aggregation-node ports."""
    if "aggregation node" in (row.get("node_description") or "").lower():
        return False
    device = (row.get("device_id") or "").strip()
    return bool(device) and device.upper() not in ("UNKNOWN", "N/A")


def hca_xcset_join_key(fields: Dict[str, str]) -> str:
    """Join key (port_guid[:port_num]) shared by xcset CSV rows and ingested DB rows."""
    guid = str(fields.get("port_guid") or "").strip().lower()
    if not guid:
        return ""
    for num_field in ("port_num", "hca_port_index", "hca_ib_port"):
        num = str(fields.get(num_field) or "").strip()
        if num:
            return f"{guid}:{num}"
    return guid


def read_hca_xcset_raw(engines) -> str:
    """The raw xcset response, exactly as NMX-T-for-IB serves it over its Unix socket."""
    cmd = f"sudo curl -s --unix-socket {NMXT_XCSET_SOCKET} http://localhost{NMXT_XCSET_ENDPOINT}"
    return engines.dut.run_cmd(cmd) or ""


def read_hca_xcset(engines, attempts: int = 3) -> Dict[str, Dict[str, str]]:
    """Read the NMX-T-for-IB xcset as ``{join_key: {sai_field: value}}`` (HCA rows only, counters summed per join key)."""
    out: Dict[str, Dict[str, str]] = {}
    for attempt in range(max(1, attempts)):
        raw = read_hca_xcset_raw(engines)
        out = {}
        for row in _parse_xcset_csv(raw):
            if not _is_hca_xcset_row(row):
                continue
            key = hca_xcset_join_key(row)
            if not key:
                continue
            mapped = {db_field: row[col]
                      for col, db_field in NMXT_XCSET_TO_DB_FIELD.items() if col in row}
            if key not in out:
                out[key] = mapped
                continue
            # Sum additive counters across plane rows sharing a join key.
            for field, val in mapped.items():
                out[key][field] = str(_safe_counter_sum(out[key].get(field), val))
        if out or attempt == max(1, attempts) - 1:
            return out
        time.sleep(PEER_TELEMETRY_SAMPLING_SEC)
    return out


def gnmi_typecheck(kind: str, value) -> str:
    """Return an error string if `value` violates the declared `kind`, else "" (empty tolerated)."""
    text = "" if value is None else str(value).strip().strip('"').strip()
    if text == "":
        return ""
    if kind == GnmiTypeKind.OPTICAL_POWER:
        return "" if optical_power_readings(text) else (
            f"expected a number or '<mW> mW / <dBm> dBm' ({kind}); got {value!r}")
    if kind in (GnmiTypeKind.COUNTER, GnmiTypeKind.UINT, GnmiTypeKind.SAMPLED_UINT,
                GnmiTypeKind.DECIMAL):
        want_int = kind in (GnmiTypeKind.COUNTER, GnmiTypeKind.UINT, GnmiTypeKind.SAMPLED_UINT)
        noun = "unsigned integer" if want_int else "decimal"
        for token in text.split("/"):
            token = token.strip()
            if token == "":
                continue
            try:
                fval = float(token)
            except ValueError:
                return f"expected {noun} ({kind}); got {value!r}"
            if fval < 0:
                return f"{kind} is negative: {value!r}"
            if want_int and not fval.is_integer():
                return f"expected integer ({kind}); got {value!r}"
        return ""
    if kind == GnmiTypeKind.BOOL:
        return "" if text.lower() in ("true", "false", "0", "1") else f"expected boolean; got {value!r}"
    return ""  # STRING / enum / identity: any non-empty value is acceptable


def _safe_counter_sum(acc, val) -> int:
    """Add a counter value onto an accumulator, treating unparseable parts as 0."""
    total = 0
    for piece in (acc, val):
        try:
            total += parse_counter_value(piece)
        except (AssertionError, ValueError):
            continue
    return total


def assert_counters_within_tolerance(a_row, b_row, fields, tolerance_pct: float, label: str) -> int:
    """Assert shared additive counters agree within tolerance_pct; returns count compared."""
    pairs = fields.items() if isinstance(fields, dict) else ((f, f) for f in fields)
    compared = 0
    for a_key, b_key in pairs:
        if a_key not in a_row or b_key not in b_row:
            continue
        try:
            a_val = parse_counter_value(a_row[a_key])
            b_val = parse_counter_value(b_row[b_key])
        except (AssertionError, ValueError):
            continue
        assert values_within_tolerance(a_val, b_val, tolerance_pct), (
            f"{label}: counter {a_key!r} differs beyond tolerance: {a_val} vs {b_val}"
        )
        compared += 1
    return compared


def counters_nondecreasing(before_row, after_row, fields) -> bool:
    """Non-asserting twin of `assert_counters_monotonic` (True when no shared counter decreased)."""
    for field in fields:
        if field not in before_row or field not in after_row:
            continue
        try:
            pre = parse_counter_value(before_row[field])
            post = parse_counter_value(after_row[field])
        except (AssertionError, ValueError):
            continue
        if post < pre:
            return False
    return True


def assert_counters_monotonic(before_row, after_row, fields, label: str) -> int:
    """Assert no shared additive counter decreased between two readings; returns count compared."""
    compared = 0
    for field in fields:
        if field not in before_row or field not in after_row:
            continue
        try:
            pre = parse_counter_value(before_row[field])
            post = parse_counter_value(after_row[field])
        except (AssertionError, ValueError):
            continue
        assert post >= pre, (
            f"{label}: counter {field!r} regressed: pre={pre}, post={post}"
        )
        compared += 1
    return compared


# ============================================================================
# Extended drop: peer component naming (HLD section 4.2)
# ============================================================================


def component_source_absent(value) -> bool:
    """True when a component-naming source identifier names nothing (missing, empty or N/A)."""
    return str(value or "").strip().lower() in PeerComponentNaming.ABSENT_VALUES


def sanitize_component_part(value) -> str:
    """One part of a component name: drop a leading 0x, replace every disallowed character."""
    text = str(value or "").strip()
    if text.lower().startswith(PeerComponentNaming.HEX_PREFIX):
        text = text[len(PeerComponentNaming.HEX_PREFIX):]
    return PeerComponentNaming.ALLOWED_CHARS_RE.sub(
        PeerComponentNaming.SANITIZE_REPLACEMENT, text)


def expected_peer_transceiver_name(module_serial, sys_image_guid=None) -> str:
    """Component name a peer module must carry, or "" when the serial names nothing.

    ``xcvr-<serial>``, extended with the chassis GUID when the xcset carries one, so the same
    cable serial seen on two chassis stays two components.
    """
    if component_source_absent(module_serial):
        return ""
    name = PeerComponentNaming.TRANSCEIVER_PREFIX + sanitize_component_part(module_serial)
    if not component_source_absent(sys_image_guid):
        name += "-" + sanitize_component_part(sys_image_guid)
    return name


def expected_peer_asic_name(node_guid) -> str:
    """Component name a peer ASIC must carry, or "" when the node GUID names nothing."""
    if component_source_absent(node_guid):
        return ""
    return PeerComponentNaming.ASIC_PREFIX + sanitize_component_part(node_guid)


def plane_peer_entries(entries: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    """Keep only per-plane (``...plN``) peer entries, dropping their aggregated parents."""
    return {pid: f for pid, f in entries.items() if is_plane_port_name(pid)}


def most_specific_peer_entries(entries: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    """One tier per peer port: its plane rows when it has any, otherwise the port row itself.

    The HLD does not say which tier carries the extended leaves, and a peer cabled on a single
    plane is listed with no plane rows at all, so keeping only the plane tier would drop it.
    """
    planes = plane_peer_entries(entries)
    out = dict(planes)
    for pid, fields in aggregated_peer_entries(entries).items():
        if not plane_peer_ids_for_parent(planes, pid):
            out[pid] = fields
    return out


def foreign_node_info_fields(fields: Dict[str, str], peer_type: str) -> List[str]:
    """Node-info leaves on a peer row that carry another peer type's data (SR-6).

    gpu_node_info and hca_node_info are separate metrics, so a row must not be padded with the
    other type's values. Only a populated leaf counts: if the two peer types share one YANG
    container, every row renders the other type's leaf names as empty placeholders, and
    failing on that would flag correct behaviour. A placeholder holding an actual value is the
    padding the requirement rules out.
    """
    foreign = set().union(*(names for kind, names in PEER_TYPE_NODE_INFO_FIELDS.items()
                            if kind != peer_type))
    return sorted(name for name, value in (fields or {}).items()
                  if str(name).lower() in foreign and str(value).strip())


def foreign_node_info_problems(entries: Dict[str, Dict[str, str]]) -> List[str]:
    """Peer rows carrying another peer type's node-info fields (SR-6)."""
    problems = []
    enforceable = 0
    for pid, fields in sorted(entries.items()):
        peer_type = classify_peer_type(pid, fields)
        if not peer_type:
            continue
        # A type whose own node info nobody else defines has no foreign set to match.
        if any(names for kind, names in PEER_TYPE_NODE_INFO_FIELDS.items() if kind != peer_type):
            enforceable += 1
        strays = foreign_node_info_fields(fields, peer_type)
        if strays:
            problems.append(
                f"[{pid}] is a {peer_type} row but carries {len(strays)} field(s) of the other "
                f"peer type: {strays!r}"
            )
    logger.info("SR-6 examined %d of %d peer row(s) for foreign node info; the rest are of a "
                "peer type whose counterpart has no node-info leaf in the HLD",
                enforceable, len(entries or {}))
    return problems


def extended_identity_tier_problems(hca_entries: Dict[str, Dict[str, str]]) -> List[str]:
    """Extended leaves that no tier of the `nv show peer-port <id>` HCA rows carries (HLD 5.2).

    The HLD leaves open whether the aggregated or the plane row carries port-number, so it
    passes when every row of one tier publishes it. A component reference is named by the rows
    that have a cable or a chip behind them, which an FNM-connected HCA does not, so each
    reference must be named by at least one row.
    """
    plane = plane_peer_entries(hca_entries)
    tiers = {
        PeerPortTargetRole.PLANE: plane,
        PeerPortTargetRole.AGGREGATED: {pid: fields for pid, fields in hca_entries.items()
                                        if pid not in plane},
    }
    nvue_refs = set(PeerPortComponentRef.NVUE_LEAF.values())
    problems = hca_omitted_leaf_problems(hca_entries)
    for leaf in sorted(PEER_PORT_NVUE_ROOT_EXTENDED_LEAVES):
        if leaf in nvue_refs:
            if not any(str(fields.get(leaf) or "").strip() for fields in hca_entries.values()):
                problems.append(f"no HCA row names a {leaf!r} component")
            continue
        missing = {role: sorted(pid for pid, fields in rows.items() if leaf not in fields)
                   for role, rows in tiers.items() if rows}
        if all(missing.values()):
            problems.append(f"leaf {leaf!r} is not published on every row of any tier; rows "
                            f"lacking it per tier: {missing!r}")
    return problems


def hca_omitted_leaf_problems(hca_entries: Dict[str, Dict[str, str]]) -> List[str]:
    """HCA peer-port rows that publish a leaf the HLD omits for an HCA (tray-id, SR-6)."""
    return [f"HCA row {pid} publishes {leaf!r} = {fields[leaf]!r}, which the HLD omits for an "
            f"HCA peer-port (published for a GPU peer-port only)"
            for pid, fields in sorted(hca_entries.items())
            for leaf in sorted(PEER_PORT_HCA_OMITTED_LEAVES) if leaf in fields]


def peer_component_refs(fields: Dict[str, str], nvue: bool = False) -> Dict[str, str]:
    """The component names a peer-port row points at, keyed by the gNMI reference leaf.

    `nvue` reads a `nv show peer-port <id>` row, which names the chip reference `asic`.
    """
    refs = {}
    for leaf in PeerPortComponentRef.ALL:
        shown = PeerPortComponentRef.NVUE_LEAF[leaf] if nvue else leaf
        value = str(fields.get(shown) or "").strip()
        if value:
            refs[leaf] = value
    return refs


# ============================================================================
# Extended drop: `nv show peer-platform` surface (HLD section 5.3)
# ============================================================================


# Top-level `nv show peer-platform` resource - the peer-side twin of `nv show platform`,
# addressed by component name rather than by peer-port id.
_PEER_PLATFORM_RESOURCE = BaseComponent(
    parent=None,
    api={ApiType.NVUE: NvueSystemCli, ApiType.OPENAPI: OpenApiSystemCli},
    path=PEER_PLATFORM_RESOURCE_PATH,
)


def parse_show_json(resource: BaseComponent, op_param: str, engines, label: str) -> Dict:
    """`nv show` one resource as nested JSON, failing the case when it carries no JSON object.

    Every command tree the extended drop adds goes through here, so a command the build does
    not resolve fails with its own output as the evidence rather than as a JSON parse error.
    """
    shown = resource.show(op_param=op_param, dut_engine=engines.dut,
                          if_returned_value=False).ignore_result()
    ResultObj(shown.result, f"`{label}` failed:\n{shown.info}",
              issue_type=IssueType.PossibleBug).verify_result()
    parsed = OutputParsingTool.parse_json_str_to_dictionary(shown.returned_value).ignore_result()
    body = parsed.returned_value if parsed.result else None
    ResultObj(isinstance(body, dict),
              f"`{label}` returned no JSON object:\n{shown.returned_value}",
              issue_type=IssueType.PossibleBug).verify_result()
    return body


_CLI_UNIT_SUFFIX = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s+[A-Za-z%]+\s*$")


def peer_platform_cli_to_canonical(body):
    """Re-spell a `nv show peer-platform` payload onto the names the HLD and gNMI use.

    The CLI follows the local platform show: the module type is `identifier` (gNMI `form-factor`),
    each channel power is a nested `rx-power` / `tx-power` container whose `power` leaf reads
    "<mW> mW / <dBm> dBm" (gNMI `input-power` / `output-power`), and temperature, laser
    temperature and supply voltage carry their unit ("63.00 C", "3.28 V"). Everything else is
    left as the CLI printed it. A power container without a `power` leaf is kept as printed, so
    the missing leaf shows up in the parity check.
    """
    if not isinstance(body, dict):
        return body
    out = {}
    for key, value in body.items():
        if (key in PEER_PLATFORM_CLI_POWER_CONTAINERS and isinstance(value, dict) and
                PEER_PLATFORM_CLI_POWER_LEAF in value):
            out[PEER_PLATFORM_CLI_POWER_CONTAINERS[key]] = value[PEER_PLATFORM_CLI_POWER_LEAF]
            continue
        if isinstance(value, dict):
            out[key] = peer_platform_cli_to_canonical(value)
            continue
        key = PEER_PLATFORM_CLI_LEAF_NAMES.get(key, key)
        if key in PEER_PLATFORM_CLI_UNIT_LEAVES:
            match = _CLI_UNIT_SUFFIX.match(str(value))
            value = match.group(1) if match else value
        out[key] = value
    return out


def nvue_peer_platform_show(engines, kind: str, name: str, sub: str = "") -> Dict:
    """`nv show peer-platform <kind> <name> [sub]` as nested JSON, on the HLD leaf names."""
    op_param = " ".join(part for part in (kind, name, sub) if part)
    return peer_platform_cli_to_canonical(
        parse_show_json(_PEER_PLATFORM_RESOURCE, op_param, engines,
                        f"nv show peer-platform {op_param}"))


def hca_peer_component_names(engines, client: GnmiClient) -> Tuple[str, Dict[str, str]]:
    """``(peer_id, {PeerPlatformKind: component name})`` for one switch-cabled HCA neighbor.

    Any HCA row will do: what 5.7 opens is the module and the ASIC by name, not the port they
    were read off. The names are taken from the gNMI peer-port state the naming rule is defined
    on; whether `nv show peer-port <peer>` renders the same references is the listing test's
    check. Returns ``("", {})`` when no row names a component, which callers fail.
    """
    # A row is listed before its component references are published, so right after the knob is
    # enabled the references can be absent for up to a couple of telemetry cycles. Wait for them
    # instead of failing on the first read.
    deadline = time.time() + COMPONENT_REF_WAIT_SEC
    while True:
        candidates = most_specific_peer_entries(
            filter_peer_entries_by_type(nvue_show_peer_ports(engines), PeerType.HCA))
        states = gnmi_get_peer_port_list(client)
        for pid in sorted(candidates):
            refs = peer_component_refs(states.get(pid, {}))
            if refs:
                return pid, {PeerPlatformKind.BY_COMPONENT_REF[leaf]: name
                             for leaf, name in refs.items()}
        if time.time() >= deadline:
            return "", {}
        logger.info("No HCA row names a component yet; checking again in %ds",
                    COMPONENT_REF_POLL_SEC)
        time.sleep(COMPONENT_REF_POLL_SEC)


def remap_leaf_keys(leaves: Dict[str, str], key_map: Dict[str, str]) -> Dict[str, str]:
    """Re-key leaf readings through `key_map`, leaving keys the map does not name as they are."""
    return {key_map.get(key, key): value for key, value in leaves.items()}


def merge_gnmi_containers(client: GnmiClient, prefixes, name: str,
                          path_prefix: str = "") -> Dict[str, str]:
    """Read several gNMI containers into one leaf table, refusing to hide a name collision.

    A node or family may span containers while the reader keys by leaf name, so the same leaf
    served under two prefixes would silently overwrite itself and the comparison would run
    against whichever container happened to be read last. Identical values are harmless;
    conflicting ones mean the merged table cannot be trusted.
    """
    leaves: Dict[str, str] = {}
    origin: Dict[str, str] = {}
    for prefix_template in prefixes:
        path = path_prefix + prefix_template.format(name=name)
        for leaf, value in gnmi_get_flat_relative(client, prefix=path).items():
            if leaf in leaves and leaves[leaf] != value:
                raise AssertionError(
                    f"gNMI leaf {leaf!r} is served by both {origin[leaf]} and {path} with "
                    f"different values ({leaves[leaf]!r} vs {value!r}); the containers "
                    f"collide, so the merged leaf table would be read from whichever was "
                    f"fetched last"
                )
            leaves[leaf] = value
            origin[leaf] = path
    return leaves


def gnmi_peer_platform_leaves(client: GnmiClient, node: PeerPlatformNode,
                              name: str) -> Dict[str, str]:
    """A peer-platform node's leaves over gNMI, re-keyed onto the CLI leaf names."""
    return remap_leaf_keys(merge_gnmi_containers(client, node.gnmi_prefixes, name),
                           node.gnmi_key_map)


def gnmi_peer_platform_members(client: GnmiClient, node: PeerPlatformNode,
                               name: str) -> Dict[str, Dict[str, str]]:
    """A peer-platform node's list members over gNMI, each re-keyed onto the CLI leaf names.

    Members are merged across the node's prefixes by member key, because the receive-side
    readings and the transmit-side indications of one channel live in two sibling lists while
    the CLI renders them as one channel entry.
    """
    members: Dict[str, Dict[str, str]] = {}
    for prefix_template in node.gnmi_prefixes:
        for key, leaves in gnmi_list_members(
                client, prefix_template.format(name=name), relative=True).items():
            members.setdefault(key, {}).update(remap_leaf_keys(leaves, node.gnmi_key_map))
    return members


def list_member_index(key: str) -> str:
    """The index a list member is keyed by: `channel-3` on the CLI and `3` over gNMI both give `3`.

    The HLD writes the CLI key as `<ch>` without fixing its spelling, so members are joined on
    the number alone.
    """
    match = re.search(r"(\d+)$", str(key))
    return match.group(1) if match else str(key)


def shift_member_index_to_match(reference: Dict[str, Any], members: Dict[str, Any]) -> Dict[str, Any]:
    """``members`` renumbered by a constant offset when that makes its indexes equal ``reference``'s.

    Meant for a list one surface numbers from 0 and another from 1. Returned unchanged when no
    constant offset lines the two up, so any other mismatch keeps being compared as it is.
    """
    try:
        want = sorted(int(key) for key in reference)
        have = sorted(int(key) for key in members)
    except ValueError:
        return members
    if not want or len(want) != len(have):
        return members
    offset = want[0] - have[0]
    if offset == 0 or [index + offset for index in have] != want:
        return members
    return {str(int(key) + offset): member for key, member in members.items()}


def xcset_row_for_db_row(db_row: Dict[str, str],
                         indexed_rows: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """The raw xcset row behind one ingested peer-port row ({} when the two cannot be joined)."""
    key = hca_xcset_join_key(db_row)
    return indexed_rows.get(key, {}) if key else {}


def xcset_host_name(xcset_row: Dict[str, str]) -> str:
    """The remote host an xcset row names: its host_name column, else the host word of node_description."""
    named = str(xcset_row.get(PeerComponentXcsetColumn.HOST_NAME) or "").strip()
    if named:
        return named
    description = str(xcset_row.get(PeerComponentXcsetColumn.NODE_DESCRIPTION) or "").split()
    return description[0] if description else ""


def peer_asic_host_name(leaves: Dict[str, str]) -> str:
    """The remote host name off a peer ASIC payload, under either accepted spelling."""
    for candidate in PEER_ASIC_HOST_NAME_CANDIDATES:
        value = str(leaves.get(candidate) or "").strip()
        if value:
            return value
    return ""


def peer_platform_show_problems(body: Dict, node: PeerPlatformNode) -> List[str]:
    """Contract problems for one `nv show peer-platform ...` payload (empty list when clean)."""
    problems = leaf_problems(peer_port_show_leaves(body), node.leaves, where=node.node_id)
    if not node.member_leaves:
        return problems
    members = peer_port_show_containers(body)
    if not members:
        return problems + [f"{node.node_id} exposes no members to check"]
    for member_key, member in sorted(members.items()):
        problems += [
            f"member [{member_key}] {problem}"
            for problem in leaf_problems(peer_port_show_leaves(member), node.member_leaves,
                                         where=node.node_id)
        ]
    return problems


# ============================================================================
# Extended drop: raw xcset columns and serving a rewritten xcset
# ============================================================================


def hca_xcset_rows_from_raw(raw: str) -> List[Dict[str, str]]:
    """Every HCA xcset row in `raw` as ``{column: value}``, keeping all columns.

    Split from `read_hca_xcset_rows` so a caller that rewrites the CSV parses the very text it
    is about to rewrite; re-reading the socket could return a different header.
    """
    return [row for row in _parse_xcset_csv(raw) if _is_hca_xcset_row(row)]


def read_hca_xcset_rows(engines) -> List[Dict[str, str]]:
    """Every HCA xcset row as ``{column: value}``, keeping all columns (not just the counters)."""
    return hca_xcset_rows_from_raw(read_hca_xcset_raw(engines))


def xcset_rows_by_join_key(rows: List[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    """Index raw xcset rows by the join key they share with the ingested System DB rows."""
    indexed: Dict[str, Dict[str, str]] = {}
    for row in rows:
        key = hca_xcset_join_key(row)
        if key:
            indexed.setdefault(key, row)
    return indexed


def xcset_header(raw: str) -> List[str]:
    """Column names the served xcset carries, so a caller can tell a rewrite is possible."""
    _, header, _ = _split_xcset_csv(raw)
    return header


def _split_xcset_csv_for_rewrite(raw: str, column: str, verb: str):
    """`_split_xcset_csv`, but refusing a response the column cannot be located in."""
    preamble, header, rows = _split_xcset_csv(raw)
    if not header:
        raise AssertionError(
            f"the xcset response carries no CSV header, so column {column!r} cannot be "
            f"{verb}; NMX-T-for-IB served: {raw!r}"
        )
    if column not in header:
        raise AssertionError(
            f"xcset column {column!r} is not served, so it cannot be {verb}; "
            f"header is {header!r}"
        )
    return preamble, header, rows


def xcset_csv_drop_column(raw: str, column: str) -> str:
    """The xcset CSV with `column` removed from the header and from every row.

    This is how a column NMX-T does not serve is simulated: the field must then be omitted
    from the published row rather than reported as zero (SR-7).
    """
    preamble, header, rows = _split_xcset_csv_for_rewrite(raw, column, "removed")
    keep = [index for index, name in enumerate(header) if name != column]
    return _render_xcset_csv(
        preamble,
        [header[index] for index in keep],
        [[row[index] for index in keep if index < len(row)] for row in rows],
    )


def xcset_csv_blank_column(raw: str, column: str, join_key: str = "") -> str:
    """The xcset CSV with `column` emptied - on the row matching `join_key`, or on every row.

    Keeping the column but clearing its value is the "telemetry does not name it" case: no
    component may be created, while the peer's own row must still be published.
    """
    preamble, header, rows = _split_xcset_csv_for_rewrite(raw, column, "blanked")
    index = header.index(column)
    blanked = []
    for row in rows:
        if index < len(row) and (not join_key or hca_xcset_join_key(dict(zip(header, row))) == join_key):
            row = list(row)
            row[index] = ""
        blanked.append(row)
    return _render_xcset_csv(preamble, header, blanked)


def droppable_xcset_column(xcset_row: Dict[str, str]) -> str:
    """An extended xcset column this row actually carries a value for, or "" when it has none.

    Picked by marker rather than by a fixed name: which columns NMX-T serves per cable type is
    still open, and a column that is empty or zero already proves nothing when removed. The
    columns that name a component are excluded -- removing one of those is the separate SR-7
    scenario where the component correctly goes away, which this check would misread as a loss.
    """
    for column, value in sorted(xcset_row.items()):
        if column.lower() in PeerComponentXcsetColumn.ALL:
            continue
        if (any(marker in column.lower() for marker in XCSET_EXTENDED_COLUMN_MARKERS) and
                not _xcset_value_is_empty(value)):
            return column
    return ""


def _xcset_value_is_empty(value) -> bool:
    """True for a blank, a zero, or the firmware's "no errors" BER sentinel (e.g. ``1.5e-254``).

    Removing a column that carries such a value proves nothing, because the DB shows the same
    zero-like value whether the column is served or not.
    """
    text = str(value).strip()
    if text in ("", "0"):
        return True
    try:
        return abs(Decimal(text)) <= PEER_PORT_BER_ZERO_FLOOR
    except InvalidOperation:
        return False


def missing_xcset_column_problems(before: Dict[str, str], after: Dict[str, str], column: str,
                                  refs_before: Dict[str, str],
                                  refs_after: Dict[str, str]) -> List[str]:
    """SR-7 problems seen after a column was removed from the served xcset.

    Which DB field a given column feeds is not published, so the two COUNTERS_DB snapshots are
    compared instead of a name map: a field that went from a real value to zero is exactly the
    violation SR-7 names, and the row itself must survive either way. The component references
    are passed in separately because they are NVUE leaves, absent from the DB row.
    """
    if not after:
        return [f"the whole peer-port row disappeared when column {column!r} was removed; "
                f"SR-7 requires the row to survive with a single leaf omitted"]
    problems = [
        f"field {field!r} was {value!r} and now reads 0 with column {column!r} removed; a "
        f"column NMX-T does not serve must omit its leaf, not report it as zero"
        for field, value in sorted(before.items())
        if str(value).strip() not in ("", "0") and str(after.get(field, "")).strip() == "0"
    ]
    problems += [
        f"component reference {leaf!r} was dropped when column {column!r} was removed; that "
        f"column names no component, so only the leaf it feeds may go"
        for leaf in sorted(set(refs_before) - set(refs_after))
    ]
    return problems


def select_droppable_xcset_column(engines, xcset_rows: List[Dict[str, str]]) -> Tuple[str, str]:
    """``(column, peer_id)`` for an extended xcset column that feeds a live peer-port row."""
    rows = most_specific_peer_entries(list_hca_peer_rows(engines))
    indexed = xcset_rows_by_join_key(xcset_rows)
    for pid, db_row in sorted(rows.items()):
        column = droppable_xcset_column(indexed.get(hca_xcset_join_key(db_row), {}))
        if column:
            return column, pid
    return "", ""


def component_naming_problems(pid: str, xcset_row: Dict[str, str],
                              refs: Dict[str, str]) -> List[str]:
    """Where a peer-port's component names disagree with the naming rule (HLD Section 4.2)."""
    expected = {
        PeerPortComponentRef.TRANSCEIVER: expected_peer_transceiver_name(
            xcset_row.get(PeerComponentXcsetColumn.MODULE_SERIAL),
            xcset_row.get(PeerComponentXcsetColumn.SYS_IMAGE_GUID),
        ),
        PeerPortComponentRef.INTEGRATED_CIRCUIT: expected_peer_asic_name(
            xcset_row.get(PeerComponentXcsetColumn.NODE_GUID)),
    }
    problems = []
    for leaf, want in expected.items():
        got = refs.get(leaf, "")
        if not want:
            if got:
                problems.append(
                    f"[{pid}] names a {leaf} component {got!r} although the xcset carries no "
                    f"identifier to build it from; no component may be invented"
                )
            continue
        if not got:
            problems.append(f"[{pid}] carries no {leaf} name although the xcset names one "
                            f"(expected {want!r})")
        elif got != want:
            problems.append(f"[{pid}] {leaf} is {got!r}, but the naming rule gives {want!r}")
    return problems


def shared_module_problems(peers: Dict[str, Dict[str, str]],
                           xcset_by_key: Dict[str, Dict[str, str]],
                           db_rows: Dict[str, Dict[str, str]]) -> List[str]:
    """Peers on one physical module that do not point at the same transceiver name (SR-4).

    A module is its serial plus the system image GUID, because the component name appends the
    GUID so the same serial on two chassis stays two components.
    """
    by_module: Dict[Tuple[str, str], Dict[str, str]] = {}
    for pid, fields in sorted(peers.items()):
        xcset_row = xcset_row_for_db_row(db_rows.get(pid, {}), xcset_by_key)
        serial = str(xcset_row.get(PeerComponentXcsetColumn.MODULE_SERIAL) or "").strip()
        guid = str(xcset_row.get(PeerComponentXcsetColumn.SYS_IMAGE_GUID) or "").strip()
        name = peer_component_refs(fields).get(PeerPortComponentRef.TRANSCEIVER, "")
        if serial and name:
            by_module.setdefault((serial, guid), {})[pid] = name
    return [
        f"peers sharing module serial {serial!r} (system image GUID {guid!r}) point at "
        f"different transceiver names: {names!r}"
        for (serial, guid), names in sorted(by_module.items()) if len(set(names.values())) > 1
    ]


def resolve_component_targets(client: GnmiClient, ref_leaf: str) -> List[Tuple[str, str]]:
    """Component names to sweep: every `ref_leaf` value a gNMI-listed peer-port points at.

    The whole listing is read rather than a sample of peers, because which tier carries the
    reference is not in the HLD. Ports sharing one module share one component name, so the
    names are de-duplicated and the peer they were read off is kept only to label the sweep.
    """
    resolved: Dict[str, str] = {}
    for pid, fields in sorted(gnmi_get_peer_port_list(client).items()):
        name = str(fields.get(ref_leaf) or "").strip()
        if name:
            resolved.setdefault(name, f"via {pid}")
    return [(via, name) for name, via in sorted(resolved.items())]


def inject_hca_xcset(engines, csv_text: str) -> None:
    """Serve `csv_text` as the ib_nvos xcset, replacing the real NMX-T-for-IB backend."""
    with allure.step("Ship the replacement xcset + interceptor to the DUT"):
        _put_remote_file(engines, _NMXT_FAKE_CSV_PATH, csv_text)
        _put_remote_file(engines, _NMXT_FAKE_SCRIPT_PATH, _NMXT_FAKE_SERVER)

    with allure.step(f"Mask + stop {NMXT_IB_SERVICE} and start the interceptor"):
        engines.dut.run_cmd(f"sudo systemctl mask {NMXT_IB_SERVICE}")
        engines.dut.run_cmd(f"sudo systemctl stop {NMXT_IB_SERVICE} 2>/dev/null")
        engines.dut.run_cmd(f"sudo docker stop {NMXT_IB_CONTAINER} 2>/dev/null")
        engines.dut.run_cmd("sudo mkdir -p /var/run/nmx-t/ib")
        time.sleep(3)  # let the real daemon release its sockets
        engines.dut.run_cmd(
            f"sudo bash -c 'nohup python3 {_NMXT_FAKE_SCRIPT_PATH} {NMXT_CONTROL_SOCKET} "
            f"{NMXT_XCSET_SOCKET} {_NMXT_FAKE_CSV_PATH} {NMXT_CONNECTOR_SOCKET} "
            f">{_NMXT_FAKE_LOG_PATH} 2>&1 < /dev/null & echo $! > {_NMXT_FAKE_PID_PATH}'"
        )


def wait_for_fake_xcset_fetches(engines, fetches: int,
                                timeout_sec: int = 10 + PEER_TELEMETRY_SAMPLING_SEC * 3) -> int:
    """How many times peer-telemetry fetched the served xcset, waiting for `fetches` of them.

    The swap is an outage, after which peer-telemetry retries on its health-check cadence
    rather than the sampling interval, so a fixed sleep can end before it reads the rewritten
    xcset even once.
    """
    deadline = time.time() + timeout_sec
    served = 0
    while True:
        out = engines.dut.run_cmd(
            f"sudo grep -c xcset-served {_NMXT_FAKE_LOG_PATH} 2>/dev/null; true") or ""
        found = re.search(r"\d+", out)
        served = int(found.group()) if found else 0
        if served >= fetches or time.time() >= deadline:
            return served
        time.sleep(5)


@contextmanager
def serving_hca_xcset(engines, csv_text: str) -> Generator[None, None, None]:
    """Serve a rewritten xcset for the duration of the block, then restore the real backend.

    Each swap is a deliberate NMX-T outage, during which peer-telemetry logs connection errors
    at error level (HLD section 6), so LogAnalyzer is paused across the two swaps but not while
    the block runs. The block is entered only after two fetches of the rewritten xcset: the
    second one starts after the first was written through.
    """
    with loganalyzer_ignore():
        inject_hca_xcset(engines, csv_text)
        served = wait_for_fake_xcset_fetches(engines, fetches=2)
    try:
        ResultObj(
            served >= 2,
            f"peer-telemetry fetched the rewritten xcset {served} time(s) after NMX-T-for-IB "
            f"came back; it must resume polling once the backend answers again (HLD section 6)",
            issue_type=IssueType.PossibleBug,
        ).verify_result()
        yield
    finally:
        with loganalyzer_ignore():
            restore_valid_hca_xcset(engines)
            backend_quiet = wait_for_nmxt_backend_reachable(engines)
    # Checked after the finally so that a failure inside the block is not replaced by this one.
    ResultObj(
        backend_quiet,
        "nmx-t-ib did not come back and stop logging backend connection errors after the "
        "rewritten xcset was withdrawn",
        issue_type=IssueType.PossibleBug,
    ).verify_result()


# ============================================================================
# Extended drop: the local switch half (HLD section 5.1)
# ============================================================================


def is_gnmi_entry_absent(text: str) -> bool:
    """True when a gNMI read failed because the container / entry is not implemented there.

    An FNM-connected HCA with no peer components, or a plane leg whose link is down, answers
    with Unimplemented "cannot find the entry" - a per-target applicability gap rather than a
    schema violation.
    """
    message = str(text or "").lower()
    return "unimplemented" in message and "cannot find the entry" in message


def select_link_up_port() -> Tuple[Optional[Port], str]:
    """A randomly chosen link-up switch Port, or ``(None, reason)`` when the DUT has none.

    Returns the reason rather than skipping, so a caller can fail, skip or degrade one step
    according to how central a live link is to what it checks.
    """
    port_result = Tools.RandomizationTool.select_random_port(
        requested_ports_state=NvosConsts.LINK_STATE_UP)
    if not port_result.result:
        port_result.ignore_result()
        return None, str(port_result.info)
    return port_result.get_returned_value(), ""


def require_link_up_port(purpose: str, skip: bool) -> Port:
    """A link-up switch Port, or end the case: these leaves only populate on a live link.

    `skip` carries the same meaning as in `report_wrong_lab`: the telemetry suites proper fail,
    because a DUT with no live link never exercises the leaves under test, while the
    subscribe-heartbeat cases layered on top skip instead.
    """
    port, reason = select_link_up_port()
    if port is None:
        report_wrong_lab(
            f"WRONG LAB SETUP: no link-up port available to {purpose} on this DUT. The "
            f"extended PHY and platform leaves are only populated while a port is link-up: "
            f"{reason}",
            skip=skip,
        )
    return port


def ber_ratio_scale_problem(leaf: str, value, where: str) -> str:
    """Empty string unless an error-rate leaf sits outside the 0..1 ratio it is published as.

    A surface that scaled the rate to a percentage would report up to 100, which is exactly the
    scaling difference SR-2 rules out between the local port and the peer-port.
    """
    try:
        ratio = Decimal(str(value).strip().strip('"'))
    except (InvalidOperation, ValueError):
        return ""
    if ratio.is_nan() or Decimal(0) <= ratio <= BER_RATIO_CEILING:
        return ""
    return (f"error-rate leaf {leaf!r} reads {value!r} on the {where}, outside the 0..1 ratio "
            f"an error rate is published as, which is a scaling difference")


def phy_surface_symmetry_problems(expected: Dict[str, str], peer_leaves: Dict[str, str],
                                  local_leaves: Dict[str, str]) -> Tuple[List[str], int]:
    """Where the peer-port and local-port PHY containers disagree (SR-2); ``(problems, shared)``.

    Names: the two read the same YANG container, so a leaf one side publishes must be published
    on the other under the same name.
    Scale: every error rate both sides publish must be a ratio on both, which is what a
    percent-versus-ratio difference would break.
    """
    problems: List[str] = []
    peer_present = {leaf for leaf in expected if leaf in peer_leaves}
    local_present = {leaf for leaf in expected if leaf in local_leaves}
    problems += [f"leaf {leaf!r} is published on the local port but not on the peer-port"
                 for leaf in sorted(local_present - peer_present)]
    problems += [f"leaf {leaf!r} is published on the peer-port but not on the local port"
                 for leaf in sorted(peer_present - local_present)]
    shared = sorted(peer_present & local_present)
    for leaf in shared:
        if expected[leaf] != GnmiTypeKind.DECIMAL:
            continue
        for where, leaves in (("peer-port", peer_leaves), ("local port", local_leaves)):
            problem = ber_ratio_scale_problem(leaf, leaves[leaf], where)
            if problem:
                problems.append(problem)
    return problems, len(shared)


def gnmi_family_leaves(client: GnmiClient, name: str, family: LocalPortFamily,
                       path_prefix: str = "") -> Dict[str, str]:
    """One field family over gNMI, merged across the containers the family spans.

    `path_prefix` selects the tree: "" reads the switch's own port, while
    ``GnmiYangPaths.PEER_PORT_PREFIX`` reads the peer-port twin of the same containers - which
    is what makes the local-versus-peer symmetry check table-driven.
    """
    return merge_gnmi_containers(client, family.gnmi_prefixes, name, path_prefix=path_prefix)


def flatten_nvue_leaves(body: Dict, prefix: str = "") -> Dict[str, str]:
    """Flatten nested NVUE JSON to ``{container/.../leaf: value}`` (null renders as "")."""
    flat: Dict[str, str] = {}
    for key, value in (body or {}).items():
        path = f"{prefix}/{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(flatten_nvue_leaves(value, path))
        else:
            flat[path] = "" if value is None else str(value)
    return flat


def leaf_lookup(flat: Dict[str, str]) -> Dict[str, str]:
    """Address each flattened leaf by its full path and by every unambiguous suffix of it.

    The CLI and gNMI do not agree on how many wrapper containers a leaf sits under, so a
    gNMI-relative key like ``raw-ber/alarm-count`` has to resolve against a CLI payload that
    nests it one level deeper. A suffix more than one leaf would claim is dropped, so an
    ambiguous short name never resolves to an arbitrary reading.
    """
    lookup: Dict[str, str] = dict(flat)
    claims: Dict[str, List[str]] = {}
    for path, value in flat.items():
        parts = path.split("/")
        for start in range(1, len(parts)):
            claims.setdefault("/".join(parts[start:]), []).append(value)
    for suffix, values in claims.items():
        if len(values) == 1 and suffix not in lookup:
            lookup[suffix] = values[0]
    return lookup


def nvue_local_sub_leaves(engines, port: Port, sub: str) -> Dict[str, str]:
    """`nv show interface <port> <sub>` flattened and addressable by every unambiguous suffix."""
    phy = port.interface.link.phy
    readers = {
        LocalPortNvueSub.LINK_PHY: phy,
        LocalPortNvueSub.LINK_PHY_DETAIL: phy.detail,
        LocalPortNvueSub.LINK_PHY_HEALTH: phy.health,
        LocalPortNvueSub.LINK_PHY_BER_MONITOR: phy.ber_monitor,
        LocalPortNvueSub.COUNTERS: port.interface.counters,
    }
    body = parse_show_json(readers[sub], "", engines, f"nv show interface {port.name} {sub}")
    return leaf_lookup(flatten_nvue_leaves(body))


def nvue_local_family_leaves(engines, port: Port, family: LocalPortFamily) -> Dict[str, str]:
    """One local-port field family over the CLI, addressable by the family's gNMI leaf keys."""
    return nvue_local_sub_leaves(engines, port, family.nvue_sub)


def read_local_port_counters_row(engines, port_name: str) -> Dict[str, str]:
    """hgetall the COUNTERS_DB row of one local switch port ({} when its OID does not resolve)."""
    oid = (get_aport_oid(engines, port_name) or "").replace('"', "").strip()
    if not oid.startswith(SystemDbCli.COUNTERS_OID_PREFIX):
        logger.info("Port %s resolved to OID %r, which is not a COUNTERS_DB key", port_name, oid)
        return {}
    return db_hgetall(engines, SystemDbCli.COUNTERS_DB,
                      SystemDbCli.COUNTERS_OID_KEY_FMT.format(oid=oid))


def read_port_counters_row(engines, port_name: str) -> Dict[str, str]:
    """hgetall the COUNTERS_DB row of an Aport or a plane-port (fails when its OID does not resolve)."""
    return db_hgetall(engines, SystemDbCli.COUNTERS_DB,
                      SystemDbCli.COUNTERS_OID_KEY_FMT.format(oid=_resolve_counter_oid(engines, port_name)))


def _db_field_by_leaf_tokens(db_row: Dict[str, str], leaf: str) -> List[str]:
    """Row fields whose name carries every dash-separated token of ``leaf``, upper-cased."""
    tokens = leaf.upper().split("-")
    return sorted(field for field in db_row if all(token in field for token in tokens))


def extended_counter_db_fields(db_row: Dict[str, str], port_name: str,
                               is_plane: bool) -> Tuple[Dict[str, str], List[str]]:
    """``{leaf: db_field}`` of every EXTENDED_COUNTERS entry the port must expose, and the problems.

    An Aport must carry every extended counter; a plane-port must carry the per-plane ones and
    is compared on the others only where its DB row carries them.
    """
    fields: Dict[str, str] = {}
    problems: List[str] = []
    for counter in EXTENDED_COUNTERS:
        required = counter.per_plane or not is_plane
        if counter.db_field:
            candidates = [counter.db_field] if counter.db_field in db_row else []
        else:
            candidates = _db_field_by_leaf_tokens(db_row, counter.leaf)
        if len(candidates) > 1:
            problems.append(f"{port_name}: {counter.leaf} matches more than one COUNTERS_DB "
                            f"field: {candidates}")
        elif candidates and str(db_row[candidates[0]]).strip() != "":
            fields[counter.leaf] = candidates[0]
        elif required:
            problems.append(f"{port_name}: the COUNTERS_DB row carries no "
                            f"{counter.db_field or counter.leaf + ' field'}")
    return fields, problems


def _extended_counter_api_values(engines, gnmi_client: GnmiClient, api: str, port: Port,
                                 leaves) -> Dict[str, Optional[str]]:
    """``{leaf: value}`` of the given extended counters over ``api`` (None when not exposed)."""
    readings: Dict[str, Dict[str, str]] = {}
    values: Dict[str, Optional[str]] = {}
    for counter in EXTENDED_COUNTERS:
        if counter.leaf not in leaves:
            continue
        if api == API_NVUE_CLI:
            source, leaf = counter.nvue_sub, counter.nvue_leaf
            if source not in readings:
                readings[source] = nvue_local_sub_leaves(engines, port, source)
        else:
            source, leaf = counter.gnmi_prefix, counter.leaf
            if source not in readings:
                readings[source] = gnmi_get_flat(gnmi_client, source.format(name=port.name))
        values[counter.leaf] = readings[source].get(leaf)
    attach_dict(f"{api} extended counters {port.name}", values)
    return values


def extended_counter_db_problems(engines, gnmi_client: GnmiClient, api: str, port: Port,
                                 is_plane: bool) -> List[str]:
    """Problems where an extended counter of ``port`` is missing over ``api`` or disagrees with the DB.

    The DB row read first decides which counters apply. NVUE and gNMI are then compared to the
    row read right after them, a counter within the sampling tolerance and a BER within a
    decade; OTEL is compared to the rows read around one fresh collection window.
    """
    by_leaf = {counter.leaf: counter for counter in EXTENDED_COUNTERS}
    before = read_port_counters_row(engines, port.name)
    fields, problems = extended_counter_db_fields(before, port.name, is_plane)
    if api == API_OTEL:
        snap = ibo.active_surface().snapshot(f"extended-counters-{port.name}", fresh=True)
        after = read_port_counters_row(engines, port.name)
        attach_dict(f"COUNTERS_DB {port.name} around OTEL window", {"before": before, "after": after})
        metric_by_field = {field: by_leaf[leaf].otel_metric for leaf, field in fields.items()}
        return problems + ibo.db_parity_problems(snap, port.name, before, after, metric_by_field,
                                                 list(fields.values()))

    api_values = _extended_counter_api_values(engines, gnmi_client, api, port, fields)
    db_row = read_port_counters_row(engines, port.name)
    attach_dict(f"COUNTERS_DB {port.name}", db_row)
    pairs = [PeerPortDbPair(f"{port.name} {api} {leaf}", api_values[leaf], field, by_leaf[leaf].kind)
             for leaf, field in fields.items()]
    pair_problems, _ = peer_port_db_pair_problems(pairs, db_row, surface=api)
    return problems + pair_problems


def gnmi_missing_extended_counters(gnmi_client: GnmiClient, port_name: str) -> List[str]:
    """Extended counter leaves an Aport does not expose over gNMI."""
    readings = {prefix: gnmi_get_flat(gnmi_client, prefix.format(name=port_name))
                for prefix in {counter.gnmi_prefix for counter in EXTENDED_COUNTERS}}
    return [counter.leaf for counter in EXTENDED_COUNTERS
            if str(readings[counter.gnmi_prefix].get(counter.leaf, "")).strip() == ""]


def local_transceiver_inventory(engines) -> Dict[str, Dict]:
    """`nv show platform transceiver --view=detail`, keyed by module name.

    The detail view rather than the brief one: the identity leaves this suite compares
    (vendor revision, supported cable length) are rendered there only. Read once per test and
    passed around, so one CLI call serves both the module choice and every leaf read.
    """
    inventory = parse_show_json(Platform().transceiver, TRANSCEIVER_DETAIL_VIEW, engines,
                                f"nv show platform transceiver {TRANSCEIVER_DETAIL_VIEW}")
    return {name: row for name, row in inventory.items() if isinstance(row, dict)}


def fitted_transceiver_names(inventory: Dict[str, Dict]) -> List[str]:
    """The modules the platform reports as fitted, out of every cage the inventory lists.

    An empty cage is listed too and publishes no identity leaf on either surface, so choosing
    one would leave the comparison with nothing to compare. A cage the platform gives a cable
    type is one that holds a module, whatever type that module turns out to be.
    """
    return sorted(name for name, row in inventory.items()
                  if str(row.get(PlatformConsts.TRANSCEIVER_CABLE_TYPE) or "").strip())


def local_transceiver_leaves(inventory: Dict[str, Dict], name: str) -> Dict[str, str]:
    """One module's detail row, addressable by the gNMI transceiver leaf keys."""
    return leaf_lookup(flatten_nvue_leaves(inventory.get(name, {})))


def diagnostic_transceiver_names(inventory: Dict[str, Dict], fitted: List[str]) -> List[str]:
    """The fitted modules that carry diagnostic data, the only ones with per-channel readings.

    A passive copper cable reports itself not DDMI capable and has no channel to sample.
    """
    return [name for name in fitted
            if str(inventory[name].get(PlatformConsts.HARDWARE_TRANCEIVER_DIAGNOSTIC_STATUS) or
                   "").strip() != PlatformConsts.HARDWARE_TRANCEIVER_NOT_DDMI]


def gnmi_local_asic_names(client: GnmiClient) -> List[str]:
    """Names of the switch chips in the gNMI components tree.

    Read from gNMI because the bare `nv show platform asic` lists no chip on NVOS; only the
    per-chip commands resolve.
    """
    types = gnmi_list_members(client, GnmiYangPaths.COMPONENT_TYPES)
    return sorted(name for name, leaves in types.items()
                  if leaves.get("type") == ASIC_COMPONENT_TYPE)


def local_asic_frequency_leaves(engines, asic_name: str) -> Dict[str, str]:
    """`nv show platform asic <asic> frequency`, flattened to the gNMI frequency leaf keys."""
    return leaf_lookup(flatten_nvue_leaves(parse_show_json(
        Platform().asic.asic_id[asic_name].frequency, "", engines,
        f"nv show platform asic {asic_name} frequency")))


def gnmi_local_component_names(client: GnmiClient) -> List[str]:
    """Component names the switch's own components tree exposes over gNMI."""
    return sorted(gnmi_list_members(client, GnmiYangPaths.COMPONENTS, relative=True))


def _normalized_bool(value: str) -> str:
    """Canonical spelling of a boolean leaf, so true and 1 compare equal across surfaces.

    Deliberately the same vocabulary `gnmi_typecheck` accepts for a BOOL: a spelling this
    folded away but the type check rejects would be normalized into a pass by one half of the
    suite and flagged by the other.
    """
    text = str(value).strip().strip('"').lower()
    if text in ("true", "1"):
        return "true"
    if text in ("false", "0"):
        return "false"
    return text


def _decimals_agree(left: float, right: float) -> bool:
    """Relative +/- jitter comparison for a sampled decimal, with no absolute counter floor.

    Error rates are ratios bounded at 1, so the shared `values_within_tolerance` -- which
    degrades to an absolute tolerance of 1.0 once either side reads zero -- would accept any
    reading against a zero. Only far-tail noise below DECIMAL_COMPARE_EPSILON is absorbed here.
    """
    scale = max(abs(left), abs(right))
    if scale <= DECIMAL_COMPARE_EPSILON:
        return True
    return abs(left - right) <= max(SAMPLING_JITTER_TOLERANCE_PCT * scale,
                                    DECIMAL_COMPARE_EPSILON)


def optical_power_readings(text: str) -> Dict[str, float]:
    """An optical power as ``{unit: value}``; empty when `text` is neither rendering.

    gNMI publishes a bare number whose unit the HLD does not state, keyed "" here. nv show
    renders ``"<mW> mW / <dBm> dBm"``, and a milliwatt reading can never be negative.
    """
    text = str(text).strip().strip('"').strip()
    try:
        return {"": float(text)}
    except ValueError:
        pass
    readings: Dict[str, float] = {}
    for part in text.split("/"):
        tokens = part.split()
        if len(tokens) != 2:
            return {}
        number, unit = tokens[0], tokens[1].lower()
        if unit not in (OPTICAL_POWER_MW_UNIT, OPTICAL_POWER_DBM_UNIT) or unit in readings:
            return {}
        try:
            readings[unit] = float(number)
        except ValueError:
            return {}
    if set(readings) != {OPTICAL_POWER_MW_UNIT, OPTICAL_POWER_DBM_UNIT}:
        return {}
    return readings if readings[OPTICAL_POWER_MW_UNIT] >= 0 else {}


def _optical_powers_agree(left: str, right: str) -> bool:
    """True when two renderings of one optical power carry the same reading.

    A bare number matches either unit of the two-unit rendering, since the HLD names the
    reading but not its unit. Milliwatts compare within the relative sampling jitter, dBm
    within an absolute band.
    """
    left_by_unit, right_by_unit = optical_power_readings(left), optical_power_readings(right)
    if not left_by_unit or not right_by_unit:
        return False

    def agree(unit: str, a: float, b: float) -> bool:
        if unit == OPTICAL_POWER_DBM_UNIT:
            return abs(a - b) <= OPTICAL_POWER_DBM_TOLERANCE
        return _decimals_agree(a, b)

    for left_unit, a in left_by_unit.items():
        for right_unit, b in right_by_unit.items():
            if left_unit and right_unit and left_unit != right_unit:
                continue
            if agree(left_unit or right_unit, a, b):
                return True
    return False


def _leaf_values_agree(kind: str, left: str, right: str) -> bool:
    """True when two surfaces report the same reading for a leaf of the given kind.

    Only values sampled from the hardware -- accumulating counters, ratios and measured
    readings -- get the sampling-jitter tolerance. A code, a setting or a duration is a single
    published value, so any difference there is a real mismatch.
    """
    left_text, right_text = str(left).strip().strip('"'), str(right).strip().strip('"')
    if kind == GnmiTypeKind.BOOL:
        return _normalized_bool(left_text) == _normalized_bool(right_text)
    if kind == GnmiTypeKind.OPTICAL_POWER:
        return _optical_powers_agree(left_text, right_text)
    if kind in (GnmiTypeKind.COUNTER, GnmiTypeKind.SAMPLED_UINT, GnmiTypeKind.DECIMAL):
        try:
            if kind == GnmiTypeKind.DECIMAL:
                return _decimals_agree(float(left_text), float(right_text))
            return values_within_tolerance(float(left_text), float(right_text),
                                           SAMPLING_JITTER_TOLERANCE_PCT)
        except (TypeError, ValueError):
            return left_text.lower() == right_text.lower()
    if kind == GnmiTypeKind.UINT:
        # "1000" and "1000.0" are the same integer rendered differently.
        try:
            return float(left_text) == float(right_text)
        except (TypeError, ValueError):
            return left_text.lower() == right_text.lower()
    return left_text.lower() == right_text.lower()


def leaf_parity_problems(expected: Dict[str, str], left: Dict[str, str], right: Dict[str, str],
                         left_label: str, right_label: str) -> Tuple[List[str], int]:
    """Disagreements over the leaves both surfaces expose; returns ``(problems, compared)``.

    A leaf only one surface carries is not a disagreement - that is the presence check's job,
    and the two surfaces deliberately differ on a few leaves (local SNR is gNMI-only).
    """
    problems: List[str] = []
    compared = 0
    for leaf, kind in sorted(expected.items()):
        if leaf not in left or leaf not in right:
            continue
        compared += 1
        if not _leaf_values_agree(kind, left[leaf], right[leaf]):
            problems.append(
                f"leaf {leaf!r} differs: {left_label}={left[leaf]!r}, "
                f"{right_label}={right[leaf]!r}"
            )
    return problems, compared


def ber_raw_component_db_fields(db_row: Dict[str, str]) -> Dict[str, str]:
    """COUNTERS_DB fields holding a raw BER component (coefficient / magnitude) the switch polled.

    Matched by name rather than by a hardcoded field: the HLD does not publish the spellings,
    and the pair is what R-NVOS-3 requires to be combined before publication.
    """
    return {
        field: value for field, value in (db_row or {}).items()
        if BER_DB_FIELD_MARKER in field.upper() and
        any(marker in field.lower() for marker in BER_RAW_COMPONENT_MARKERS)
    }


def ber_component_leaf_problems(leaves, where: str) -> List[str]:
    """Published leaf names that expose a raw BER component instead of the combined rate."""
    return [
        f"leaf {leaf!r} publishes a raw BER component on {where}: R-NVOS-3 requires the "
        f"coefficient and magnitude to be combined into a single value"
        for leaf in sorted(leaves)
        if any(marker in str(leaf).lower() for marker in BER_COMPONENT_LEAF_MARKERS)
    ]
