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

import json
import logging
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.constants.constants import GnmiConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.constants import GnmiMode, GnmicErr
from ngts.tests_nvos.system.gnmi.helpers import (
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
    GnmiYangPaths,
    IfaceType,
    NvuePaths,
    OTEL_PENDING_MSG,
    PlanePortState,
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
