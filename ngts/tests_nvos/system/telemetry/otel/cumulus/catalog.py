"""Version-gated OTLP supported-metric catalog for Cumulus lab DUTs.

Cumulus/NVOS lab DUTs typically do not ship ``metrics-classes.yaml`` like NVOS images.
Tests derive expected metric names from this catalog (by ``python3-nvue`` package
version) instead of SCP from the switch.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ngts.tests_nvos.system.telemetry.otel.cumulus.metric_catalog import (
    supported_metrics as metrics,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.metric_catalog import (
    supported_metrics_parity as metrics_parity,
)

logger = logging.getLogger(__name__)

# ``python3-nvue`` Debian package version → TelemetryOtlp profile id (515/516/517/518…).
_NVUE_PKG_TO_PROFILE: Tuple[Tuple[Tuple[int, ...], str], ...] = (
    ((1, 14, 0), "518"),
    ((1, 13, 0), "517"),
    ((1, 12, 0), "516"),
    ((1, 11, 0), "515"),
    ((1, 10, 0), "514"),
    ((1, 9, 0), "513"),
    ((1, 8, 0), "512"),
    ((1, 7, 0), "511"),
    ((1, 6, 0), "510"),
    ((0, 0, 0), "510"),
)

_SKIP_BUCKETS = frozenset({"ALLOWED_EXTRAS"})


class _TelemetryOtlpBase:
    def __init__(self) -> None:
        self.supported_metrics: Dict[str, List[str]] = {
            "PH1_ADD_STAT": metrics.PH1_ADD_STAT,
            "PH1_DOT3_STAT": metrics.PH1_DOT3_STAT,
            "PH1_INT_STAT": metrics.PH1_INT_STAT,
            "PH1_HIST": metrics.PH1_HIST,
        }


class _TelemetryOtlp511(_TelemetryOtlpBase):
    def __init__(self) -> None:
        super().__init__()
        self.supported_metrics.update(
            {
                "PH2_ADD_INT_STAT": metrics.PH2_ADD_INT_STAT,
                "PH2_SW_PRIO_STATS": metrics.PH2_SW_PRIO_STATS,
                "PH2_CP_STATS": metrics.PH2_CP_STATS,
                "PH2_INT_CAR_CHG": metrics.PH2_INT_CAR_CHG,
                "PH2_INT_DISC_STATS": metrics.PH2_INT_DISC_STATS,
                "PH2_INT_ETHER_STATS": metrics.PH2_INT_ETHER_STATS,
                "PH2_PKT_DIST_STATS": metrics.PH2_PKT_DIST_STATS,
                "PH2_PLAT_ENV": metrics.PH2_PLAT_ENV,
                "PH2_NODE_CPU": metrics.PH2_NODE_CPU,
                "PH2_NODE_MEM": metrics.PH2_NODE_MEM,
                "PH2_NODE_DISK": metrics.PH2_NODE_DISK,
                "PH2_NODE_FILE": metrics.PH2_NODE_FILE,
                "PH2_HIST": metrics.PH2_HIST,
                "ALLOWED_EXTRAS": metrics.ALLOWED_EXTRAS,
            }
        )


class _TelemetryOtlp512(_TelemetryOtlp511):
    def __init__(self) -> None:
        super().__init__()
        self.supported_metrics.update(
            {
                "PH3_ROUTING_STATS": metrics.PH3_ROUTING_STATS,
                "PH3_BUFF_STATS": metrics.PH3_BUFF_STATS,
                "PH3_PHY_STATS": metrics.PH3_PHY_STATS,
                "PH3_EXTRAS": metrics.PH3_EXTRAS,
            }
        )


class _TelemetryOtlp513(_TelemetryOtlp512):
    def __init__(self) -> None:
        super().__init__()
        if "PH3_ROUTING_STATS" in self.supported_metrics:
            del self.supported_metrics["PH3_ROUTING_STATS"]
        self.supported_metrics.update(
            {
                "PH4_EXTRAS": metrics.PH4_EXTRAS,
                "PH4_TRANSCEIVER_INFO_ALL": metrics.PH4_TRANSCEIVER_INFO_ALL,
                "PH4_SOFTWARE_STATS": metrics.PH4_SOFTWARE_STATS,
                "PH4_LLDP_STATS": metrics.PH4_LLDP_STATS,
                "PH5_ADDITIONS": metrics.PH5_ADDITIONS,
            }
        )


class _TelemetryOtlp514(_TelemetryOtlp513):
    def __init__(self) -> None:
        super().__init__()
        self.supported_metrics.update(
            {
                "PH5_ROUTING_STATS": metrics.PH5_ROUTING_STATS,
            }
        )


class _TelemetryOtlp515:
    def __init__(self) -> None:
        self.supported_metrics = {
            "PH1_INT_STAT": metrics_parity.PH1_INT_STAT,
            "PH1_HIST": metrics_parity.PH1_HIST,
            "PH1_DOT3_STAT": metrics_parity.PH1_DOT3_STAT,
            "PH1_ADD_STAT": metrics_parity.PH1_ADD_STAT,
            "PH2_HIST": metrics_parity.PH2_HIST,
            "PH2_ADD_INT_STAT": metrics_parity.PH2_ADD_INT_STAT,
            "PH2_INT_CAR_CHG": metrics_parity.PH2_INT_CAR_CHG,
            "PH2_INT_DISC_STATS": metrics_parity.PH2_INT_DISC_STATS,
            "PH2_INT_ETHER_STATS": metrics_parity.PH2_INT_ETHER_STATS,
            "PH2_PKT_DIST_STATS": metrics_parity.PH2_PKT_DIST_STATS,
            "PH2_PLAT_ENV": metrics_parity.PH2_PLAT_ENV,
            "PH2_NODE_CPU": metrics_parity.PH2_NODE_CPU,
            "PH2_NODE_DISK": metrics_parity.PH2_NODE_DISK,
            "PH2_NODE_FILE": metrics_parity.PH2_NODE_FILE,
            "PH2_NODE_MEM": metrics_parity.PH2_NODE_MEM,
            "PH2_SW_PRIO_STATS": metrics_parity.PH2_SW_PRIO_STATS,
            "PH2_CP_STATS": metrics_parity.PH2_CP_STATS,
            "ALLOWED_EXTRAS": metrics_parity.ALLOWED_EXTRAS,
            "PH3_BUFF_STATS": metrics_parity.PH3_BUFF_STATS,
            "PH3_PHY_STATS": metrics_parity.PH3_PHY_STATS,
            "PH3_EXTRAS": metrics_parity.PH3_EXTRAS,
            "PH4_EXTRAS": metrics_parity.PH4_EXTRAS,
            "PH4_TRANSCEIVER_INFO_ALL": metrics_parity.PH4_TRANSCEIVER_INFO_ALL,
            "PH4_SOFTWARE_STATS": metrics_parity.PH4_SOFTWARE_STATS,
            "PH4_LLDP_STATS": metrics_parity.PH4_LLDP_STATS,
            "PH5_ADDITIONS": metrics_parity.PH5_ADDITIONS,
            "CL_514_AI_ETHERNET_STATS": metrics_parity.CL_514_AI_ETHERNET_STATS,
            "PH6_ADDITIONAL_NODE_STATS": metrics_parity.PH6_ADDITIONAL_NODE_STATS,
            "CL_515_ROUTING_STATS": metrics_parity.CL_515_ROUTING_STATS,
            "CL_515_ADDITIONAL_INT_STATS": metrics_parity.CL_515_ADDITIONAL_INT_STATS,
            "PH6_ACL_STATS": metrics_parity.PH6_ACL_STATS,
            "CL_515_AI_ETHERNET_STATS": metrics_parity.CL_515_AI_ETHERNET_STATS,
        }


class _TelemetryOtlp516(_TelemetryOtlp515):
    def __init__(self) -> None:
        super().__init__()
        self.supported_metrics.update(
            {
                "PH6_DOT1X_STATS": metrics_parity.PH6_DOT1X_STATS,
                "CL_516_QOS_BUFFER_METRICS": metrics_parity.CL_516_QOS_BUFFER_METRICS,
            }
        )


class _TelemetryOtlp517(_TelemetryOtlp516):
    def __init__(self) -> None:
        super().__init__()
        metric_to_remove = "nvswitch_interface_shared_buffer_port_tc_watermark_recorded_max_bytes"
        ph5_additions = list(self.supported_metrics.get("PH5_ADDITIONS", []))
        if metric_to_remove in ph5_additions:
            ph5_additions.remove(metric_to_remove)
            self.supported_metrics["PH5_ADDITIONS"] = ph5_additions
        self.supported_metrics.update(
            {
                "CL_517_ADDITIONAL_PLATFORM_STATS": metrics_parity.CL_517_ADDITIONAL_PLATFORM_STATS,
                "CL_517_CP_NETSTAT_METRICS": metrics_parity.CL_517_CP_NETSTAT_METRICS,
                "CL_517_ADDITIONAL": metrics_parity.CL_517_ADDITIONAL,
                "CL_517_ADDITIONAL_PHY": metrics_parity.CL_517_ADDITIONAL_PHY,
                "CL_517_LINK_DEBOUNCE_STATS": metrics_parity.CL_517_LINK_DEBOUNCE_STATS,
            }
        )


class _TelemetryOtlp518(_TelemetryOtlp517):
    def __init__(self) -> None:
        super().__init__()
        self.supported_metrics.update(
            {
                "CL_518_PLATFORM_ASIC_RESOURCE_STATS": (
                    metrics_parity.CL_518_PLATFORM_ASIC_RESOURCE_STATS
                ),
                "CL_518_PLATFORM_PARTITION_STATS": (
                    metrics_parity.CL_518_PLATFORM_PARTITION_STATS
                ),
                "CL_518_PLATFORM_POWER_SUPPLY_STATS": (
                    metrics_parity.CL_518_PLATFORM_POWER_SUPPLY_STATS
                ),
            }
        )


_PROFILE_CLASSES = {
    "510": _TelemetryOtlpBase,
    "511": _TelemetryOtlp511,
    "512": _TelemetryOtlp512,
    "513": _TelemetryOtlp513,
    "514": _TelemetryOtlp514,
    "515": _TelemetryOtlp515,
    "516": _TelemetryOtlp516,
    "517": _TelemetryOtlp517,
    "518": _TelemetryOtlp518,
}


def _parse_version_tuple(version: str) -> Tuple[int, ...]:
    parts = re.findall(r"\d+", version)
    if not parts:
        return (0, 0, 0)
    return tuple(int(p) for p in parts[:3]) + (0,) * max(0, 3 - len(parts[:3]))


def profile_for_nvue_pkg_version(version: str) -> str:
    """Map ``python3-nvue`` package version to a TelemetryOtlp profile id."""
    ver_tuple = _parse_version_tuple(version)
    for threshold, profile in _NVUE_PKG_TO_PROFILE:
        if ver_tuple >= threshold:
            return profile
    return "510"


def _clean_nvue_cmd_output(raw: str) -> str:
    """Strip prompts/echo from ``dut.run_cmd`` output and keep payload lines."""
    if not raw:
        return ""
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw)
    kept: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith("$") or stripped.endswith("#"):
            continue
        if stripped.startswith("nv ") and "Error:" not in stripped:
            if not re.search(r"\d+\.\d+\.\d+", stripped):
                continue
        kept.append(stripped)
    return "\n".join(kept)


def detect_nvue_python_pkg_version(dut) -> Optional[str]:
    """Best-effort read of ``python3-nvue`` package version from the DUT."""
    probes = (
        "dpkg -s python3-nvue 2>/dev/null | awk '/^Version:/ {print $2; exit}'",
        "rpm -q --queryformat '%{VERSION}' python3-nvue 2>/dev/null",
        "dpkg-query -W -f='${Version}' python3-nvue 2>/dev/null",
    )
    for cmd in probes:
        raw = dut.run_cmd(cmd, validate=False, print_output=False) or ""
        # Version is often on the same line as the shell prompt; search full raw first.
        match = re.search(r"(\d+\.\d+\.\d+)", raw)
        if match:
            return match.group(1)
        text = _clean_nvue_cmd_output(raw)
        match = re.search(r"(\d+\.\d+\.\d+)", text)
        if match:
            return match.group(1)
    return None


def resolve_supported_metrics_profile(dut, *, test01: bool = False) -> str:
    """Map DUT ``python3-nvue`` version to TelemetryOtlp profile id."""
    pkg_version = detect_nvue_python_pkg_version(dut)
    if pkg_version:
        profile = profile_for_nvue_pkg_version(pkg_version)
        logger.info(
            "OTEL supported_metrics profile=%s (python3-nvue=%s)",
            profile,
            pkg_version,
        )
        return profile
    fallback = "517" if test01 else "510"
    logger.warning(
        "python3-nvue version not detected from DUT; using supported_metrics profile=%s",
        fallback,
    )
    return fallback


def supported_metrics_for_dut(dut, *, profile_override: Optional[str] = None) -> Dict[str, List[str]]:
    """Return version-gated ``supported_metrics`` dict for this DUT."""
    profile = profile_override or resolve_supported_metrics_profile(dut)
    otlp = _PROFILE_CLASSES[profile]()
    return dict(otlp.supported_metrics)


def supported_metrics_for_test01_validation(dut) -> Dict[str, List[str]]:
    """Supported metrics for test01 validation (515+ catalog, no capability gating)."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst

    profile = resolve_supported_metrics_profile(dut, test01=True)
    full = supported_metrics_for_dut(dut, profile_override=profile)
    allowed = set(CumulusOtelConst.TEST01_VALIDATION_BUCKETS)
    skip = set(CumulusOtelConst.TEST01_VALIDATION_SKIP_BUCKETS)
    return {
        bucket: names
        for bucket, names in full.items()
        if bucket in allowed and bucket not in skip
    }


def supported_metrics_for_secured_validation(dut) -> Dict[str, List[str]]:
    """Full version-gated catalog for secured OTLP collection (SSIM ``supported_metrics``)."""
    profile = resolve_supported_metrics_profile(dut, test01=True)
    return supported_metrics_for_dut(dut, profile_override=profile)


def supported_metrics_for_mgmt_vrf_secured_validation(dut) -> Dict[str, List[str]]:
    """Secured mgmt VRF catalog aligned with ``OtelMgmtVrfWithTLSConfig`` (no routing/dot1x/debounce)."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst

    profile = resolve_supported_metrics_profile(dut, test01=True)
    full = supported_metrics_for_dut(dut, profile_override=profile)
    skip = set(CumulusOtelConst.SECURED_MGMT_VALIDATION_SKIP_BUCKETS)
    return {bucket: names for bucket, names in full.items() if bucket not in skip}


def flatten_supported_metrics(
    catalog: Dict[str, List[str]],
    *,
    skip_buckets: Iterable[str] = _SKIP_BUCKETS,
) -> Set[str]:
    """Flatten catalog buckets to a set of metric names."""
    skip = set(skip_buckets)
    names: Set[str] = set()
    for bucket, entries in catalog.items():
        if bucket in skip:
            continue
        names.update(entries)
    return names
