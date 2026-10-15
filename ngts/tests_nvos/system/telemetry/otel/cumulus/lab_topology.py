"""Cumulus lab interface names and OTEL attribute labels for mgmt VRF tests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional, Tuple

import pytest

from ngts.nvos_constants.constants_nvos import OutputFormat
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst

# (test_iface, tx_iface, rx_iface) — tried in order when probing the DUT.
_INTERFACE_TRIPLETS: Tuple[Tuple[str, str, str], ...] = (
    ("swp1s0", "swp1s1", "swp1s2"),  # mlx-3700 / split-port spine
    ("swp61s0", "swp61s1", "swp61s2"),  # mlx-5
    ("swp1", "swp2", "swp3"),  # flat front-panel naming (e.g. SN5600 lab)
)

# Front-panel data ports across platforms:
#   Cumulus: ``swp1``, ``swp1s0``, ``swp61s0``
#   NVOS:    ``swA1p1``, ``sw44p1``
_FRONT_PANEL_RE = re.compile(r"^(?:swp\d|sw[A-Za-z]*\d+p\d)")
_SWP_NAME_RE = re.compile(r"^(?:swp\d|sw[A-Za-z]*\d+p\d)\S*", re.MULTILINE)


def is_front_panel_port(name: str) -> bool:
    """True for Cumulus ``swp*`` and NVOS ``sw<plane><id>p<port>`` ports."""
    return bool(_FRONT_PANEL_RE.match(str(name)))


@dataclass(frozen=True)
class CumulusLabInterfaces:
    tx_iface: str
    rx_iface: str
    test_iface: str
    labels: Tuple[str, ...]


def _swp_sort_key(name: str) -> Tuple[int, ...]:
    """Natural order: swp2 before swp10; swp1s1 before swp1s10 when present."""
    nums = [int(x) for x in re.findall(r"\d+", name)]
    return tuple(nums) if nums else (0,)


def _interface_show_text(dut, iface: str = "", *, output_format: str = OutputFormat.json) -> str:
    """``nv show interface [iface]`` via the interface component (NVUE or OpenAPI)."""
    node = Interface(None, port_name=iface) if iface else Interface(None)
    try:
        result = node.show(
            dut_engine=dut, output_format=output_format, if_returned_value=False
        )
    except Exception:  # noqa: BLE001 - discovery must not fail the caller
        return ""
    result.ignore_result()
    raw = result.returned_value
    if isinstance(raw, (dict, list)):
        return json.dumps(raw)
    if raw:
        return str(raw)
    return str(result.info or "")


def _interface_show_dict(text: str):
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        parsed = OutputParsingTool.parse_json_str_to_dictionary(text)
        parsed.ignore_result()
        payload = parsed.returned_value if parsed.result else None
    return payload if isinstance(payload, dict) else None


def dut_interface_exists(dut, iface: str) -> bool:
    """True when ``nv show interface <iface>`` returns a real interface object."""
    out = _interface_show_text(dut, iface)
    lowered = out.lower()
    if "does not exist" in lowered or "invalid command" in lowered:
        return False
    if "error:" in lowered:
        return False
    return "oper-status" in lowered or f'"{iface}"' in out


def discover_swp_interfaces_on_dut(dut) -> Tuple[str, ...]:
    """Return sorted front-panel port names (Cumulus ``swp*`` / NVOS ``sw*p*``)."""
    out = _interface_show_text(dut)
    if "error:" not in out.lower() and "does not exist" not in out.lower():
        payload = _interface_show_dict(out)
        if isinstance(payload, dict):
            names = [k for k in payload if is_front_panel_port(k)]
            if names:
                return tuple(sorted(names, key=_swp_sort_key))

    table = _interface_show_text(dut, output_format=OutputFormat.auto)
    names = sorted(
        {m.group(0) for m in _SWP_NAME_RE.finditer(table)},
        key=_swp_sort_key,
    )
    return tuple(names)


def resolve_cumulus_lab_interfaces(hostname: str) -> Optional[CumulusLabInterfaces]:
    """Map DUT hostname to traffic/histogram interfaces (Cumulus mlx lab naming).

    Returns ``None`` when the hostname is unknown (e.g. default ``cumulus``) so
    callers can probe the DUT.
    """
    host = (hostname or "").lower()
    if any(tag in host for tag in ("mlx-3", "mlx-4", "mlx-3700", "3700-225")):
        return CumulusLabInterfaces(
            tx_iface="swp1s1",
            rx_iface="swp1s2",
            test_iface="swp1s0",
            labels=CumulusOtelConst.INTF_LABELS,
        )
    if "mlx-5" in host:
        return CumulusLabInterfaces(
            tx_iface="swp61s1",
            rx_iface="swp61s2",
            test_iface="swp61s0",
            labels=CumulusOtelConst.INTF_LABELS,
        )
    return None


def resolve_cumulus_lab_interfaces_on_dut(dut, hostname: str = "") -> CumulusLabInterfaces:
    """Resolve lab interfaces from hostname or by probing ``nv show interface``."""
    host = (hostname or "").strip()
    if host:
        lab = resolve_cumulus_lab_interfaces(host)
        if lab is not None:
            return lab

    for test_iface, tx_iface, rx_iface in _INTERFACE_TRIPLETS:
        if dut_interface_exists(dut, test_iface):
            return CumulusLabInterfaces(
                test_iface=test_iface,
                tx_iface=tx_iface,
                rx_iface=rx_iface,
                labels=CumulusOtelConst.INTF_LABELS,
            )

    swps = discover_swp_interfaces_on_dut(dut)
    if swps:
        test_iface = swps[0]
        tx_iface = swps[1] if len(swps) > 1 else swps[0]
        rx_iface = swps[2] if len(swps) > 2 else swps[0]
        return CumulusLabInterfaces(
            test_iface=test_iface,
            tx_iface=tx_iface,
            rx_iface=rx_iface,
            labels=CumulusOtelConst.INTF_LABELS,
        )

    pytest.fail(
        "Could not resolve Cumulus lab swp interfaces "
        f"(hostname={hostname!r}); nv show interface has no swp* ports"
    )


def histogram_interfaces_on_dut(dut, hostname: str = "") -> Tuple[str, ...]:
    """Minimal swp set for histogram / ``histogram-export-service`` on single-DUT lab.

    SSIM ``OtelMgmtVrfNoTLSConfig`` enables histogram on every spine ``swp`` in a
    multi-node topo. NGTS health test01 only needs a few ports (same three used for
    interface up/down in SSIM health). Full-fabric parity is not required here.
    """
    lab = resolve_cumulus_lab_interfaces_on_dut(dut, hostname)
    return tuple(dict.fromkeys((lab.test_iface, lab.tx_iface, lab.rx_iface)))
