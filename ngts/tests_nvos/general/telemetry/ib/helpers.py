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
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.cli_wrappers.nvue.nvue_system_clis import NvueSystemCli
from ngts.cli_wrappers.openapi.openapi_system_clis import OpenApiSystemCli
from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import ApiType, DatabaseConst, UfmMadConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.IbInterfaceTool import IbInterfaceTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RegisterTool import RegisterTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.constants import GnmiMode, GnmicErr
from ngts.tests_nvos.system.gnmi.helpers import (
    _is_gnmi_unavailable,
    get_infiniband_name_from_port_name,
    get_port_oid_from_infiniband_port,
    is_gnmi_failure,
    parse_gnmi_output,
    verify_msg_not_in_out_or_err,
)
from ngts.tools.test_utils import allure_utils as allure

from ngts.tests_nvos.general.telemetry.ib.constants import (
    ALL_APIS,
    API_GNMIC,
    API_NVUE_CLI,
    API_OTEL,
    BER_INJECT_SETTLE_SEC,
    CONCAT_DELIMITER,
    COUNTER_SNAPSHOT_SETTLE_SEC,
    COUNTERMGRD_MAX_COUNTERS,
    COUNTERMGRD_SUM_COUNTERS,
    CounterMgrdRule,
    EXPECTED_PLANE_PORT_DB_FIELDS,
    GNMI_REBOOT_READY_DELAY_SEC,
    GNMI_REBOOT_READY_TRIES,
    GnmiTypeKind,
    GnmiYangPaths,
    IfaceType,
    LINK_RECOVERY_POLL_SEC,
    LINK_RECOVERY_TIMEOUT_SEC,
    LINK_STATE_RECOVERY_LEAVES,
    NMXT_CONTROL_SOCKET,
    NMXT_IB_CONTAINER,
    NMXT_IB_SERVICE,
    NMXT_XCSET_ENDPOINT,
    NMXT_XCSET_SOCKET,
    NMXT_XCSET_TO_DB_FIELD,
    NvuePaths,
    OTEL_PENDING_MSG,
    PEER_TELEMETRY_SAMPLING_SEC,
    PEER_TELEMETRY_SERVICE,
    PeerPortFields,
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
)

logger = logging.getLogger(__name__)


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


def pull_otel_metric(name: str, leaf: Optional[str] = None) -> Dict[str, str]:
    """
    OTEL collector access is not wired up in ngts yet (test plan section 4: "OTEL
    not yet exposed"). The plan calls for parametrizing over OTEL and
    routing through this single function so that, when OTEL lands, only this
    helper needs to change.
    """
    pytest.skip(f"{OTEL_PENDING_MSG} (interface={name}, leaf={leaf})")


def list_interfaces_via_api(api: str, engines, gnmi_client: GnmiClient) -> Dict[str, str]:
    """
    Enumerate interface names + their type leaf across APIs.

    Returns {interface_name: type_string} so the caller can filter by
    Aport vs plane-port without re-implementing parsing per API.

    The `type` for gnmic is the gNMI `state/type` leaf (e.g. "infiniband",
    "infiniband-plane-port"); for NVUE it is the JSON top-level "type" field
    (e.g. "ib", "ibpp").
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
        pull_otel_metric("<list>", "type")
        return {}  # unreachable; pytest.skip raises

    raise ValueError(api)


def expected_type_leaf(api: str, is_plane: bool) -> str:
    """Return the expected type-leaf value for a given API surface."""
    if api == API_NVUE_CLI:
        return IfaceType.NVUE_PLANE if is_plane else IfaceType.NVUE_APORT
    if api == API_GNMIC:
        return IfaceType.GNMI_PLANE if is_plane else IfaceType.GNMI_APORT
    return "<otel>"


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
    return _is_gnmi_unavailable(err)


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


def capture_otel_counter_snapshot(
    engines,
    port_name: str,
    sai_fields: Optional[List[str]] = None,
) -> Dict[str, int]:
    """
    Read countermgrd fields via OTEL (same {SAI_PORT_STAT_*: int} shape as gNMI/NVUE).

    Not exposed yet - pytest.skip until the OTEL maps and collector wiring land.
    """
    del engines, sai_fields
    pytest.skip(f"{OTEL_PENDING_MSG} (port={port_name})")


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


# Memoized ``plan-ports`` map of an Aport, keyed by (engine, Aport). The no-arg
# bulk ``nv show fae interface`` only exposes link+type, so plan-ports/key detail
# must come from the per-Aport ``nv show fae interface <aport>`` form. Caching per
# Aport keeps it at one show per active Aport, never one per plane.
_FAE_APORT_PLAN_PORTS_CACHE: Dict[Tuple[int, str], Dict[str, dict]] = {}


def _fae_aport_plan_ports(engine, aport_name: str) -> Dict[str, dict]:
    """
    Return one Aport's ``plan-ports`` map from ``nv show fae interface <aport>``,
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
        plan_ports = parsed.get("plan-ports", {}) if isinstance(parsed, dict) else {}
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

    # Fallback: resolve via the FAE plan-ports "key" from the per-Aport
    # `nv show fae interface <aport>` form (the no-arg bulk lacks plan-ports),
    # cached per Aport so a multi-plane caller hits one show per Aport, not per
    # plane. A miss is just "not in the map" - no failed ResultObj leaking to
    # teardown.
    plan_ports = _fae_aport_plan_ports(engines.dut, aport_name)
    plane_entry = plan_ports.get(plane_port_name, {})
    port_key_with_suffix = plane_entry.get("key", "") if isinstance(plane_entry, dict) else ""
    if not port_key_with_suffix:
        raise AssertionError(
            f"No FAE plan-ports key for {plane_port_name}; "
            f"Aport {aport_name!r} plan-ports keys={list(plan_ports.keys())}"
        )
    # FAE's 'key' carries a 3-char tail we strip; the trimmed value is the
    # IB-internal port name fed into COUNTERS_PORT_NAME_MAP.
    if len(port_key_with_suffix) <= 3:
        raise AssertionError(f"unexpected FAE plan-ports key, too short to trim: {port_key_with_suffix!r}")
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


def gnmi_list_members(client: GnmiClient, list_prefix: str) -> Dict[str, Dict[str, str]]:
    """Enumerate a gNMI list under `list_prefix` as ``{member_key: {leaf: value}}``."""
    members: Dict[str, Dict[str, str]] = {}
    for line in gnmi_get_raw_lines(client, prefix=list_prefix, path=""):
        if ": " not in line:
            continue
        path, _, value = line.partition(": ")
        keys = re.findall(r"\[[^\]=]+=([^\]]+)\]", path)
        if not keys:
            continue
        members.setdefault(keys[-1], {})[path.rsplit("/", 1)[-1]] = value.strip()
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


def nvue_show_peer_port(engines, peer_id: str) -> Dict[str, str]:
    """`nv show peer-port <id>` via the framework wrapper; returns a flat dict."""
    body = _PEER_PORT_RESOURCE.parse_show(op_param=peer_id, dut_engine=engines.dut)
    return _flatten_dict(body) if isinstance(body, dict) else {}


def nvue_show_peer_port_raw(engines, peer_id: str) -> Dict:
    """`nv show peer-port <id>` returning the nested JSON as-is (not flattened)."""
    body = _PEER_PORT_RESOURCE.parse_show(op_param=peer_id, dut_engine=engines.dut)
    return body if isinstance(body, dict) else {}


def peer_port_counters(entry_raw: Dict) -> Dict[str, str]:
    """Flat ``{leaf: value}`` of a peer-port ``counters`` subtree from a raw nv-show dict."""
    return _flatten_dict(entry_raw.get("counters", {})) if isinstance(entry_raw, dict) else {}


def nvue_peer_port_phy_raw(engines, peer_id: str) -> Dict:
    """`nv show peer-port <id> link phy` JSON (BER ``health`` + PLR ``detail`` subtree)."""
    body = _PEER_PORT_RESOURCE.parse_show(op_param=f"{peer_id} link phy", dut_engine=engines.dut)
    return body if isinstance(body, dict) else {}


def peer_port_phy(phy_raw: Dict) -> Dict[str, str]:
    """Flat ``{leaf: value}`` of a peer-port ``link phy`` subtree (BER ``health`` + PLR ``detail``)."""
    return _flatten_dict(phy_raw) if isinstance(phy_raw, dict) else {}


def peer_port_planes(entry_raw: Dict) -> Dict[str, Dict[str, str]]:
    """``{plane_id: {leaf: value}}`` of a peer-port ``plane-ports`` map from a raw nv-show dict."""
    planes = {}
    if isinstance(entry_raw, dict):
        planes = entry_raw.get("plane-ports") or entry_raw.get("plane_ports") or {}
    out: Dict[str, Dict[str, str]] = {}
    for plane_id, body in (planes or {}).items():
        out[str(plane_id)] = _flatten_dict(body) if isinstance(body, dict) else {}
    return out


def list_peer_ports_via_api(api: str, engines, gnmi_client: GnmiClient) -> Dict[str, Dict[str, str]]:
    """Enumerate peer-ports across APIs as ``{peer_id: {leaf: value}}`` (OTEL excluded today)."""
    assert api in ALL_APIS, f"Unsupported api: {api!r}"
    if api == API_NVUE_CLI:
        return nvue_show_peer_ports(engines)
    if api == API_GNMIC:
        return gnmi_get_peer_port_list(gnmi_client)
    if api == API_OTEL:
        pull_otel_metric("<peer-port-list>", "peer-type")
        return {}  # unreachable; pull_otel_metric raises
    raise ValueError(api)


def get_peer_port_via_api(api: str, engines, gnmi_client: GnmiClient, peer_id: str) -> Dict[str, str]:
    """Read a single peer-port entry across APIs as a flat dict."""
    assert api in ALL_APIS, f"Unsupported api: {api!r}"
    if api == API_NVUE_CLI:
        return nvue_show_peer_port(engines, peer_id)
    if api == API_GNMIC:
        return gnmi_get_peer_port(gnmi_client, peer_id)
    if api == API_OTEL:
        pull_otel_metric(peer_id, "peer-type")
        return {}  # unreachable
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
    for key in ("peer_port_name", "peer-port-name", PeerPortFields.HCA_ALIAS_FIELD,
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
        return "degraded"
    tokens = set(text.split())
    if any(h in tokens or h == text.strip() for h in PeerTelemetryHealth.HEALTHY_VALUES):
        return "healthy"
    return "unknown"


def read_peer_telemetry_health(engines) -> Tuple[str, Dict[str, str]]:
    """Read gpu-telemetry health from STATE_DB; returns ``(classification, raw_row)``."""
    keys = list_peer_telemetry_health_keys(engines) or [SystemDbCli.PEER_TELEMETRY_HEALTH_KEY]
    raw: Dict[str, str] = {}
    for key in keys:
        raw.update(db_hgetall(engines, SystemDbCli.STATE_DB, key))
    return classify_peer_health(raw), raw


def read_gpu_telemetry_log(engines, tail: int = 400) -> str:
    """Return the last `tail` lines of the peer-telemetry (consumer) journal."""
    cmd = f"sudo journalctl -u {PEER_TELEMETRY_SERVICE} --no-pager -n {tail} 2>&1"
    return engines.dut.run_cmd(cmd) or ""


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
# control sockets). Argv: ctrl_sock tele_sock csv.
_NMXT_FAKE_SERVER = r'''
import os, sys, threading, time
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, UnixStreamServer

CTRL, TELE, CSV = sys.argv[1], sys.argv[2], sys.argv[3]
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

for _p in (CTRL, TELE):
    threading.Thread(target=serve, args=(_p,), daemon=True).start()
time.sleep(3600)
'''


_NMXT_FAKE_SCRIPT_PATH = "/tmp/peerport_nmxt_fake.py"


_NMXT_FAKE_CSV_PATH = "/tmp/peerport_xcset_bad.csv"


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
    with allure.step("Ship the malformed xcset + interceptor to the DUT"):
        _put_remote_file(engines, _NMXT_FAKE_CSV_PATH, _MALFORMED_XCSET)
        _put_remote_file(engines, _NMXT_FAKE_SCRIPT_PATH, _NMXT_FAKE_SERVER)

    with allure.step(f"Mask + stop {NMXT_IB_SERVICE} and start the interceptor"):
        engines.dut.run_cmd(f"sudo systemctl mask {NMXT_IB_SERVICE}")
        engines.dut.run_cmd(f"sudo systemctl stop {NMXT_IB_SERVICE} 2>/dev/null")
        engines.dut.run_cmd(f"sudo docker stop {NMXT_IB_CONTAINER} 2>/dev/null")
        engines.dut.run_cmd("sudo mkdir -p /var/run/nmx-t/ib")
        time.sleep(3)  # let the real daemon release its sockets
        engines.dut.run_cmd(
            f"sudo bash -c 'nohup python3 {_NMXT_FAKE_SCRIPT_PATH} {NMXT_CONTROL_SOCKET} "
            f"{NMXT_XCSET_SOCKET} {_NMXT_FAKE_CSV_PATH} >{_NMXT_FAKE_LOG_PATH} 2>&1 "
            f"< /dev/null & echo $! > {_NMXT_FAKE_PID_PATH}'"
        )


def restore_valid_hca_xcset(engines) -> None:
    """Tear down the interceptor and bring the real NMX-T-for-IB backend back."""
    with allure.step("Stop the interceptor and restore the real backend"):
        engines.dut.run_cmd(
            f"sudo bash -c 'kill $(cat {_NMXT_FAKE_PID_PATH} 2>/dev/null) 2>/dev/null; "
            "pkill -f \"[p]eerport_nmxt_fake.py\" 2>/dev/null; true'"
        )
        engines.dut.run_cmd(f"sudo rm -f {NMXT_XCSET_SOCKET} {NMXT_CONTROL_SOCKET}")
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
    while health != "healthy" and time.time() < deadline:
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


def _parse_xcset_csv(raw: str) -> List[Dict[str, str]]:
    """Parse the NMX-T-for-IB CSV into column->value dicts (skips noise/short rows)."""
    lines = (raw or "").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("timestamp,")), None)
    if start is None:
        return []
    rows = [r for r in csv.reader(io.StringIO("\n".join(lines[start:]))) if r]
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, r)) for r in rows[1:] if len(r) == len(header)]


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


def read_hca_xcset(engines, attempts: int = 3) -> Dict[str, Dict[str, str]]:
    """Read the NMX-T-for-IB xcset as ``{join_key: {sai_field: value}}`` (HCA rows only, counters summed per join key)."""
    cmd = f"sudo curl -s --unix-socket {NMXT_XCSET_SOCKET} http://localhost{NMXT_XCSET_ENDPOINT}"
    out: Dict[str, Dict[str, str]] = {}
    for attempt in range(max(1, attempts)):
        raw = engines.dut.run_cmd(cmd) or ""
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
    if kind in (GnmiTypeKind.COUNTER, GnmiTypeKind.UINT, GnmiTypeKind.DECIMAL):
        want_int = kind in (GnmiTypeKind.COUNTER, GnmiTypeKind.UINT)
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
