"""
Helpers for the gNMI-for-IB plane-port and extended-telemetry test suite.

Only the helpers needed by the currently-merged test cases live here; this
module grows one test at a time as each plane-port case passes review.

Current scope:
- plane-port knob read / set / unset via NVUE CLI;
- plane-port enumeration for an Aport;
- gNMI / NVUE / OTEL interface enumeration + per-interface subtree reads
  (OTEL is a pytest.skip stub for now);
- per-API type-leaf mapping + Allure attach helper.
"""

import ast
import json
import logging
import re
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.constants.constants import GnmiConsts
from ngts.helpers.object_filters import filter_objects
from ngts.nvos_constants.constants_nvos import DatabaseConst, UfmMadConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.nvos_tools.infra.IbInterfaceTool import IbInterfaceTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RegisterTool import RegisterTool
from ngts.nvos_tools.infra.RegressionConfigurations import PlanePortConnectivity
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
    COUNTER_SNAPSHOT_SETTLE_SEC,
    COUNTERMGRD_MAX_COUNTERS,
    COUNTERMGRD_SUM_COUNTERS,
    CONCAT_DELIMITER,
    CounterMgrdRule,
    EXPECTED_PLANE_PORT_DB_FIELDS,
    GNMI_REBOOT_READY_DELAY_SEC,
    GNMI_REBOOT_READY_TRIES,
    GnmiYangPaths,
    IfaceType,
    NvuePaths,
    OTEL_PENDING_MSG,
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
        result = System().plane_port.parse_show(dut_engine=engines.dut)
        _assert_plane_port_operation_ok(result, "nv show system plane-port")
        parsed = result.get_returned_value()
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


def resolve_engine_for_hostname(topology_obj, engines, hostname_hint: str) -> Optional[Any]:
    """Return an SSH engine for `dut` / `dut2` / ... when its hostname matches."""
    hint = PlanePortConnectivity.normalize_hostname(hostname_hint)
    if not hint:
        return None
    for player_name, player in filter_objects(
        topology_obj.players, host_type="dut", engine_type="ssh"
    ).items():
        try:
            player_name_value = player["attributes"].noga_query_data["attributes"]["Common"]["Name"]
        except (AttributeError, KeyError, TypeError):
            continue
        if PlanePortConnectivity.normalize_hostname(player_name_value) == hint:
            return engines[player_name]
    return None


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


def capture_gnmi_counter_snapshot(
    client: GnmiClient,
    port_name: str,
    sai_fields: Optional[List[str]] = None,
) -> Dict[str, int]:
    """
    Read countermgrd default-SUM + MAX fields for a port via gNMI counter leaves.

    Returns {SAI_PORT_STAT_*: int} for each leaf that is present and parseable.
    """
    fields = sai_fields if sai_fields is not None else COUNTERMGRD_SUM_COUNTERS
    sum_prefix = GnmiYangPaths.STATE_COUNTERS.format(name=port_name)
    sum_payload = gnmi_get_flat(client, prefix=sum_prefix, path="")
    out = _sai_snapshot_from_gnmi_sum_payload(sum_payload, fields)
    max_prefix = GnmiYangPaths.INFINIBAND_COUNTERS_PORT.format(name=port_name)
    max_payload = gnmi_get_flat(client, prefix=max_prefix, path="")
    out.update(_sai_snapshot_from_gnmi_max_payload(max_payload))
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
def quiesce_aport_for_counter_reads(
    aport: Port,
    engines=None,
    devices=None,
    setup_topology=None,
    topology_obj=None,
    require_oper_down: bool = True,
) -> Generator[None, None, None]:
    """
    Admin-down an Aport (and optionally its inter-switch partner) for counter reads.

    Waits for oper-down, then pauses briefly so gNMI Aport aggregation can catch
    up with plane-port sums. Restores admin-up in ``finally`` so assertion
    failures do not leave links down.
    """
    partner_engine = None
    partner_host = None
    partner_port = None
    partner_was_down = False

    if setup_topology is not None and topology_obj is not None and devices is not None:
        partner = setup_topology.inter_switch_partner(
            aport.name, dut_hostname(topology_obj, devices)
        )
        if partner is not None:
            partner_host, partner_port = partner
            partner_engine = resolve_engine_for_hostname(topology_obj, engines, partner_host)
            allure.attach(
                f"inter-switch partner for {aport.name}",
                f"host={partner_host!r} port={partner_port!r} "
                f"engine={'yes' if partner_engine is not None else 'no'}",
            )

    with allure.step(f"Admin-down Aport {aport.name} for counter snapshot"):
        if partner_port and partner_engine is None:
            pytest.skip(
                f"Connectivity lists inter-switch partner {partner_host!r} port "
                f"{partner_port!r} for {aport.name} but no matching SSH engine "
                "was found in topology (add dut2/dut3 to the setup players)."
            )

        aport.interface.link.state.set(
            op_param_name=NvosConsts.LINK_STATE_DOWN, apply=True, ask_for_confirmation=True
        ).verify_result()

        if partner_engine is not None and partner_port:
            with allure.step(
                f"Admin-down inter-switch partner {partner_port} on {partner_host} for {aport.name}"
            ):
                _set_link_state_on_engine(partner_engine, partner_port, NvosConsts.LINK_STATE_DOWN)
                partner_was_down = True

        _wait_for_admin_down_quiesce(
            aport,
            require_oper_down=require_oper_down,
            tries=(
                _QUIESCE_PARTNER_DOWN_OPER_TRIES
                if partner_was_down
                else _QUIESCE_ADMIN_DOWN_OPER_TRIES
            ),
            partner_port=partner_port,
            partner_quiesce_attempted=partner_was_down,
        )
    with allure.step(
        f"Wait {COUNTER_SNAPSHOT_SETTLE_SEC}s for counter aggregation to settle on {aport.name}"
    ):
        time.sleep(COUNTER_SNAPSHOT_SETTLE_SEC)
    try:
        yield
    finally:
        if partner_was_down and partner_engine is not None and partner_port:
            with allure.step(f"Admin-up partner {partner_port} (restore)"):
                try:
                    _set_link_state_on_engine(partner_engine, partner_port, NvosConsts.LINK_STATE_UP)
                    _wait_for_link_oper_on_engine(
                        partner_engine, partner_port, NvosConsts.LINK_STATE_UP,
                        tries=_QUIESCE_PARTNER_DOWN_OPER_TRIES,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("partner admin-up restore failed for %s: %s", partner_port, exc)
        with allure.step(f"Admin-up Aport {aport.name} (restore)"):
            try:
                aport.interface.link.state.set(
                    op_param_name=NvosConsts.LINK_STATE_UP, apply=True, ask_for_confirmation=True
                ).verify_result()
                Port.wait_for_port_state(aport, NvosConsts.LINK_STATE_UP)
            except Exception as exc:  # noqa: BLE001
                logger.warning("admin-up restore failed for %s: %s", aport.name, exc)


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
    raw = Tools.DatabaseTool.sonic_db_cli_hgetall(
        engine=engines.dut, asic="", db_name=db_name, table_name=key
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
