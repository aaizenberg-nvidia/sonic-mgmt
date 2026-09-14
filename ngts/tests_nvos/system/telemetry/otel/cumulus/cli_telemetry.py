"""CLI snapshots for Cumulus mgmt VRF OTEL tests (interface, histogram, control-plane)."""

from __future__ import annotations

import logging
import re
import shlex
from typing import Any, Dict, List

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import (
    collect_cli_platform_environment_stats,
    get_plat_env_fan_stats_with_cli,
    get_plat_env_psu_stats_with_cli,
    get_plat_env_temp_stats_with_cli,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.lab_topology import is_front_panel_port

logger = logging.getLogger(__name__)

_GLOBAL_PATTERN = re.compile(r"^\s*(\w+):\s+(\d+)")
_GROUP_PATTERN = re.compile(
    r"^\s*Group\s+(\d+):\s+ToCpuPacket:\s+(\d+)\s+ToCpuByte:\s+(\d+)\s+Events:\s+(\d+)\s+Drops:\s+(\d+)"
)
_TRAP_PATTERN1 = re.compile(
    r"^\s*Trap\s+(\d+) \(Group (\d+)\):\s+ToCpuPacket:\s+(\d+)\s+ToCpuByte:\s+(\d+)"
)
_TRAP_PATTERN2 = re.compile(
    r"^\s*Trap\s+(\d+) \(Group (\d+)\):\s+Events:\s+(\d+)\s+Drops:\s+(\d+)"
)


def _parse_json_text(raw: str) -> Any:
    """Parse JSON text from non-``nv show`` sources (e.g. histogram snapshot files)."""
    result = OutputParsingTool.parse_show_output_to_dict(raw)
    if not result.result:
        result.ignore_result()
        raise ValueError(result.info or "failed to parse JSON text")
    return result.get_returned_value()


def _interface_qos_node(iface: str, counter_path: str) -> BaseComponent:
    iface_node = Interface(None, port_name=iface)
    return BaseComponent(iface_node.counters, path=f'/qos/{counter_path}')


def list_swp_interfaces(dut) -> List[str]:
    """Return sorted front-panel port names (Cumulus ``swp*`` / NVOS ``sw*p*``)."""
    try:
        parsed = Interface(None).parse_show(dut_engine=dut)
        intf_table = parsed.get("interface", parsed)
        names = sorted(name for name in intf_table if is_front_panel_port(name))
        if names:
            return names
    except Exception as exc:  # noqa: BLE001
        logger.warning("nv show interface parse failed (%s); falling back to sysfs grep", exc)
    lines = dut.run_cmd(
        "ls /sys/class/net | grep '^sw'",
        validate=False,
        print_output=False,
    )
    return sorted(
        line.strip() for line in lines.splitlines() if is_front_panel_port(line.strip())
    )


def get_ingress_interface_stats_with_cli(dut, *, required: bool = False) -> Dict[str, Any]:
    """Per-interface ingress-buffer-stats (test02a)."""
    with allure.step("CLI: ingress-buffer-stats"):
        ingress_data: Dict[str, Any] = {}
        for intf in list_swp_interfaces(dut):
            try:
                ingress_data[intf] = _interface_qos_node(
                    intf, "ingress-buffer-stats"
                ).parse_show(dut_engine=dut)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ingress-buffer-stats parse failed for %s: %s", intf, exc)
                if required:
                    pytest.fail(f"ingress-buffer-stats failed for {intf}: {exc}")
        return ingress_data


def get_egress_interface_stats_with_cli(dut, *, required: bool = False) -> Dict[str, Any]:
    """Per-interface egress-queue-stats (test02b)."""
    with allure.step("CLI: egress-queue-stats"):
        egress_data: Dict[str, Any] = {}
        for intf in list_swp_interfaces(dut):
            try:
                egress_data[intf] = _interface_qos_node(
                    intf, "egress-queue-stats"
                ).parse_show(dut_engine=dut)
            except Exception as exc:  # noqa: BLE001
                logger.warning("egress-queue-stats parse failed for %s: %s", intf, exc)
                if required:
                    pytest.fail(f"egress-queue-stats failed for {intf}: {exc}")
        return egress_data


def _histogram_snapshot_from_parsed(parsed: Any) -> Dict[str, Any]:
    """Return ``parsed`` when it is a histogram snapshot dict, else ``{}``."""
    if isinstance(parsed, dict) and "histogram_counter_info" in parsed:
        return parsed
    return {}


def _find_histogram_snapshot_path(dut) -> str:
    """Locate newest valid histogram snapshot under ``/var/run/cumulus``.

    SSIM ``otelClientOps.get_histogram_snapshot`` uses ``ls -rt | tail -1`` on the
    directory listing. Some DUTs also have unrelated ``histogram_stats_*`` files that
    grep-match but are not JSON snapshots; walk newest-first and validate JSON keys.
    """
    # Prefer files that actually contain histogram JSON (skip histogram_stats_* noise).
    discover_cmd = (
        "for f in $(ls -1rt /var/run/cumulus 2>/dev/null); do "
        'p="/var/run/cumulus/$f"; '
        '[ -f "$p" ] || continue; '
        'grep -q histogram_counter_info "$p" 2>/dev/null && echo "$p" && break; '
        "done"
    )
    path = dut.run_cmd(discover_cmd, validate=False, print_output=False).strip()
    if path:
        try:
            raw = dut.run_cmd(f"cat {shlex.quote(path)}", validate=False, print_output=False)
            parsed = _parse_json_text(raw)
        except ValueError:
            parsed = None
        else:
            if _histogram_snapshot_from_parsed(parsed):
                return path

    names = dut.run_cmd(
        "ls -1rt /var/run/cumulus 2>/dev/null",
        validate=False,
        print_output=False,
    ).splitlines()
    for name in (n.strip() for n in names if n.strip()):
        full_path = name if name.startswith("/") else f"/var/run/cumulus/{name}"
        try:
            raw = dut.run_cmd(f"cat {shlex.quote(full_path)}", validate=False, print_output=False)
            parsed = _parse_json_text(raw)
        except ValueError:
            continue
        if _histogram_snapshot_from_parsed(parsed):
            return full_path

    listing = dut.run_cmd(
        "ls -la /var/run/cumulus 2>/dev/null | head -20",
        validate=False,
        print_output=False,
    ).strip()
    logger.warning(
        "No histogram snapshot under /var/run/cumulus (listing head):\n%s",
        listing or "(empty)",
    )
    return ""


def get_histogram_snapshot(dut, *, required: bool = False) -> Dict[str, Any]:
    """Latest histogram snapshot JSON under ``/var/run/cumulus`` (test03a-d).

    When ``required`` is false, returns ``{}`` if no snapshot exists (test05 and other
    tests that only need platform/interface CLI can still proceed).
    """
    with allure.step("CLI: histogram snapshot"):
        path = _find_histogram_snapshot_path(dut)
        if not path:
            if required:
                pytest.fail("No histogram snapshot file under /var/run/cumulus")
            logger.warning(
                "Histogram snapshot not found; hist_snap will be empty "
                "(test03a-d validations will skip)"
            )
            return {}

        full_path = path if path.startswith("/") else f"/var/run/cumulus/{path}"
        logger.info("Using histogram snapshot file: %s", full_path)
        try:
            raw = dut.run_cmd(f"cat {shlex.quote(full_path)}", validate=False, print_output=False)
            parsed = _parse_json_text(raw)
        except ValueError as exc:
            if required:
                pytest.fail(f"Histogram snapshot {full_path!r} is not valid JSON: {exc}")
            logger.warning("Histogram snapshot %s is not valid JSON: %s", full_path, exc)
            return {}

        snap = _histogram_snapshot_from_parsed(parsed)
        if not snap:
            if required:
                pytest.fail(
                    f"File {full_path!r} is not a histogram snapshot "
                    f"(missing histogram_counter_info); "
                    f"keys={list(parsed.keys()) if isinstance(parsed, dict) else type(parsed)}"
                )
            logger.warning(
                "File %s is not a histogram snapshot (missing histogram_counter_info)",
                full_path,
            )
            return {}
        return snap


def get_cp_stat_sx_api_cli(dut) -> Dict[str, Any]:
    """Parse ``sx_api_host_ifc_counters_get.py`` output (test04)."""
    with allure.step("CLI: sx_api_host_ifc_counters_get"):
        try:
            host_ifc_ctrs = dut.run_cmd(
                "sudo sx_api_host_ifc_counters_get.py",
                validate=False,
                print_output=False,
            )
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"sx_api_host_ifc_counters_get.py failed: {exc}")

        cp_ctrs_dict: Dict[str, Any] = {
            "Global": {},
            "Group Counters": {},
            "Trap Counters": {},
        }
        cp_global_ctrs_dict: Dict[str, int] = {}
        cp_group_ctrs_dict: Dict[str, Any] = {}
        cp_trap_ctrs_dict: Dict[str, Any] = {}
        current_section = None

        for line in host_ifc_ctrs.strip().split("\n"):
            if line == "Global":
                current_section = "Global"
                continue
            if line == "Group Counters":
                current_section = "Group Counters"
                continue
            if line == "Trap Counters":
                current_section = "Trap Counters"
                continue

            if current_section == "Global":
                match = _GLOBAL_PATTERN.match(line)
                if match:
                    key, value = match.groups()
                    cp_global_ctrs_dict[key.strip()] = int(value)
            elif current_section == "Group Counters":
                match = _GROUP_PATTERN.match(line)
                if match:
                    group_id, to_cpu_packet, to_cpu_byte, events, drops = match.groups()
                    cp_group_ctrs_dict[f"Group {group_id.zfill(2)}"] = {
                        "ToCpuPacket": int(to_cpu_packet),
                        "ToCpuByte": int(to_cpu_byte),
                        "Events": int(events),
                        "Drops": int(drops),
                    }
            elif current_section == "Trap Counters":
                match = _TRAP_PATTERN1.match(line)
                if match:
                    trap_id, group_id, to_cpu_packet, to_cpu_byte = match.groups()
                    key = f"Trap {trap_id.zfill(2)} (Group {group_id.zfill(2)})"
                    cp_trap_ctrs_dict.setdefault(key, {}).update(
                        {"ToCpuPacket": int(to_cpu_packet), "ToCpuByte": int(to_cpu_byte)}
                    )
                else:
                    match = _TRAP_PATTERN2.match(line)
                    if match:
                        trap_id, group_id, events, drops = match.groups()
                        key = f"Trap {trap_id.zfill(2)} (Group {group_id.zfill(2)})"
                        cp_trap_ctrs_dict.setdefault(key, {}).update(
                            {"Events": int(events), "Drops": int(drops)}
                        )

        cp_ctrs_dict["Global"] = cp_global_ctrs_dict
        cp_ctrs_dict["Group Counters"] = cp_group_ctrs_dict
        cp_ctrs_dict["Trap Counters"] = cp_trap_ctrs_dict
        return cp_ctrs_dict


def collect_cli_platform_only(dut) -> Dict[str, Any]:
    """Platform environment CLI only (test05); avoids slow/noisy per-interface nv show."""
    platform = collect_cli_platform_environment_stats(dut)
    return {
        "egress_stats": {},
        "ingress_stats": {},
        "hist_snap": {},
        "cp_stats": {},
        "plat_env_temp": platform.get("plat_env_temp") or get_plat_env_temp_stats_with_cli(dut),
        "plat_env_fan": platform.get("plat_env_fan") or get_plat_env_fan_stats_with_cli(dut),
        "plat_env_psu": platform.get("plat_env_psu") or get_plat_env_psu_stats_with_cli(dut),
    }


def collect_cli_mgmt_vrf_session_data(
    dut,
    *,
    cp_stats_pre: Dict[str, Any] | None = None,
    require_hist_snap: bool = False,
    include_interface_stats: bool = True,
    include_cp_stats: bool = True,
) -> Dict[str, Any]:
    """Post-export CLI payloads stored under telemetry cache key ``cli``.

    Histogram snapshot collection is best-effort unless ``require_hist_snap`` is true
    (test03a-d need a non-empty ``hist_snap`` at validation time).
    """
    platform = collect_cli_platform_environment_stats(dut)
    payload: Dict[str, Any] = {
        "plat_env_temp": platform.get("plat_env_temp") or get_plat_env_temp_stats_with_cli(dut),
        "plat_env_fan": platform.get("plat_env_fan") or get_plat_env_fan_stats_with_cli(dut),
        "plat_env_psu": platform.get("plat_env_psu") or get_plat_env_psu_stats_with_cli(dut),
    }
    if include_cp_stats:
        cp_stats_pre = cp_stats_pre if cp_stats_pre is not None else get_cp_stat_sx_api_cli(dut)
        payload["cp_stats"] = {"pre": cp_stats_pre, "post": get_cp_stat_sx_api_cli(dut)}
    else:
        payload["cp_stats"] = {}
    if include_interface_stats:
        payload["ingress_stats"] = get_ingress_interface_stats_with_cli(dut)
        payload["egress_stats"] = get_egress_interface_stats_with_cli(dut)
    else:
        payload["ingress_stats"] = {}
        payload["egress_stats"] = {}
    payload["hist_snap"] = get_histogram_snapshot(dut, required=require_hist_snap)
    return payload


def collect_control_plane_stats_only(
    dut,
    *,
    cp_stats_pre: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Collect control plane stats CLI (test04)."""
    with allure.step("CLI: control-plane stats collection"):
        cp_stats_pre = cp_stats_pre if cp_stats_pre is not None else get_cp_stat_sx_api_cli(dut)
        cp_stats_post = get_cp_stat_sx_api_cli(dut)
        return {"cp_stats": {"pre": cp_stats_pre, "post": cp_stats_post}}
