"""OTLP export sample-interval gap analysis (SSIM ``timegap_analysis_v2`` parity)."""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict, deque
from statistics import mean
from typing import Any, Dict, Optional

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
    collect_telemetry_data,
    cumulus_otel_artifact_path,
    truncate_collector_export,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.telemetry_health import (
    cleanup_otlp_export_session,
)
from ngts.tests_nvos.system.telemetry.otel.otel_collector import OtelCollector

logger = logging.getLogger(__name__)

_TOLERANCE_PCT = 0.25


def _parse_cumulus_version_tuple(version_text: str) -> tuple[int, int]:
    match = re.search(r"(\d+)\.(\d+)", version_text or "")
    if match:
        return int(match.group(1)), int(match.group(2))
    return 0, 0


def get_dut_os_info(dut) -> str:
    """Best-effort OS string for gap-analysis gating (SSIM ``get_os_info``)."""
    for cmd in (
        "cat /etc/image-release 2>/dev/null",
        "cat /etc/os-release 2>/dev/null",
    ):
        out = dut.run_cmd(cmd, validate=False, print_output=False)
        if out.strip():
            return out
    from ngts.nvos_tools.system.System import System
    from ngts.tests_nvos.system.telemetry.otel.helpers import component_show_text

    return component_show_text(System().version, dut)


def _node_netstat_sample_interval_supported(os_info: str) -> bool:
    text = (os_info or "").lower()
    match = re.search(r"(\d+)\.(\d+)", text)
    if not match:
        return False
    major, minor = int(match.group(1)), int(match.group(2))
    return (major, minor) >= (5, 17)


def _expected_sample_intervals_for_os(
    expected_result: Dict[str, float], os_info: str
) -> Dict[str, float]:
    out = dict(expected_result)
    if not _node_netstat_sample_interval_supported(os_info):
        out.pop("netstats_gap", None)
    return out


def timegap_analysis_v2(
    otlp_output_filepath: str,
    hostname: str,
    *,
    max_objects: int = 20000,
    expected: Optional[Dict[str, float]] = None,
    tolerance_pct: float = _TOLERANCE_PCT,
    os_info: str = "",
    family_map: Optional[list] = None,
) -> Dict[str, float]:
    """Streaming gap analysis (SSIM ``test_telemetry_split_pipelines.timegap_analysis_v2``).

    ``family_map`` overrides the ``(metric-name-prefix, gap-key)`` pairs scanned for
    gaps. Defaults to the Cumulus/node-exporter prefixes; NVOS passes its unified
    ``nvswitch_platform_*`` / IB ``nvswitch_interface_if`` prefixes instead.
    """

    def _hosts_in_obj(obj: Dict[str, Any]) -> list[str]:
        hosts = []
        for rm in obj.get("resourceMetrics", []) or []:
            attrs = (rm.get("resource", {}) or {}).get("attributes", []) or []
            for attr in attrs:
                key = attr.get("key")
                if key in ("host.name", "hostname"):
                    val = attr.get("value", {}) or {}
                    hn = val.get("stringValue") or val.get("intValue") or val.get("doubleValue")
                    if hn:
                        hosts.append(str(hn))
        return hosts

    def _prepare_attributes_key(attributes: list) -> tuple | str:
        if not attributes:
            return "no_attr"
        attrs_key = []
        for attr in attributes:
            for value in attr.values():
                if isinstance(value, dict):
                    if "arrayValue" in value:
                        try:
                            label = value["arrayValue"]["values"][0]["stringValue"]
                            attrs_key.append(label)
                        except (KeyError, IndexError, TypeError):
                            continue
                    else:
                        attrs_key.extend(str(v) for v in value.values())
                else:
                    attrs_key.append(str(value))
        return tuple(attrs_key)

    def _optimized_get_timestamp_gaps(
        metrics_timestamps: dict, met_type: str = "nvswitch_interface", n: float = 0.5
    ) -> dict:
        longer: dict = {}
        for met_name, attr_tstamps in metrics_timestamps.items():
            if met_type not in met_name:
                continue
            if met_type.startswith("nvswitch_interface") and (
                "shared_buffer" in met_name or
                "headroom_buffer" in met_name or
                "headroom_pool" in met_name
            ):
                continue
            gaps = []
            for tstamps in attr_tstamps.values():
                if not tstamps or len(tstamps) < 2:
                    continue
                for i in range(1, len(tstamps)):
                    gap = (tstamps[i] - tstamps[i - 1]) / 1e9
                    if gap >= n:
                        gaps.append(gap)
            if gaps:
                longer[met_name] = gaps
        return longer

    def _optimized_mean_of_rolling_avg(
        metric_timestamp_gaps: dict, roll_win: int = 1024
    ) -> dict:
        if not metric_timestamp_gaps:
            return {}
        roll_avg: dict = {}
        curr: deque = deque(maxlen=roll_win)
        for met_name, gaps in metric_timestamp_gaps.items():
            if not gaps:
                continue
            curr.clear()
            for gap in gaps:
                curr.append(gap)
                roll_avg.setdefault(met_name, []).append(sum(curr) / len(curr))
        return {name: mean(values) for name, values in roll_avg.items() if values}

    metric_timestamps: dict = defaultdict(lambda: defaultdict(list))
    count = 0
    expanded = os.path.expanduser(otlp_output_filepath)
    with open(expanded, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            count += 1
            obj_hosts = _hosts_in_obj(obj)
            if hostname and obj_hosts:
                if all(hostname not in host for host in obj_hosts):
                    continue
            elif hostname and hostname not in line:
                continue
            for resource_metric in obj.get("resourceMetrics", []) or []:
                for scope_metric in resource_metric.get("scopeMetrics", []) or []:
                    for metric in scope_metric.get("metrics", []) or []:
                        name = metric.get("name", "")
                        if not name:
                            continue
                        for mtype in ("gauge", "histogram", "sum"):
                            if mtype not in metric:
                                continue
                            for dp in metric[mtype].get("dataPoints", []) or []:
                                ts = dp.get("timeUnixNano")
                                if not ts:
                                    continue
                                key = _prepare_attributes_key(dp.get("attributes", []) or [])
                                metric_timestamps[name][key].append(int(ts))
                            break
            if count >= max_objects:
                break

    if family_map is not None:
        families = list(family_map)
    else:
        families = [
            ("nvswitch_histogram", "histogram_gap"),
            ("nvswitch_interface_ether", "interface_stats_gap"),
            ("nvswitch_control", "control_stats_gap"),
        ]
        if _node_netstat_sample_interval_supported(os_info):
            families.append(("node_netstat", "netstats_gap"))
        families.extend(
            [
                ("node_cpu", "platf_cpu_gap"),
                ("node_memory", "platf_mem_gap"),
                ("node_filesystem", "platf_file_gap"),
                ("nvswitch_platform_environment", "platf_envir_gap"),
                ("node_disk", "platf_disk_gap"),
                ("nvswitch_platform_transceiver", "platf_transceiver_gap"),
                ("shared_buffer", "buffer_gap"),
                ("nvswitch_interface_phy", "phy_gap"),
                ("phy_stats", "phy_gap"),
                ("nvrouting", "frr_gap"),
                ("nvswitch_systemd", "systemd_gap"),
                ("nvswitch_ar", "ar_gap"),
                ("nvswitch_lldp", "lldp_gap"),
            ]
        )

    result: Dict[str, float] = {}
    for met_type, key in families:
        gaps = _optimized_get_timestamp_gaps(metric_timestamps, met_type=met_type)
        if gaps:
            rolling = _optimized_mean_of_rolling_avg(gaps)
            val = mean(rolling.values()) if rolling else 0.0
        else:
            val = 0.0
        if key in result and result[key] != 0 and val == 0:
            continue
        result[key] = val
        logger.info(
            "sample-interval gap %s (%s): %.6f",
            key,
            met_type,
            val,
        )
        if expected is not None and key in expected and gaps:
            exp = expected[key]
            tol = exp * tolerance_pct
            lower, upper = exp - tol, exp + tol
            if rolling:
                outliers = [
                    (name, value)
                    for name, value in rolling.items()
                    if value < lower or value > upper
                ]
                if outliers:
                    logger.warning(
                        "metrics outside expected interval for %s (~%s +/- %s): %s",
                        key,
                        exp,
                        tol,
                        outliers[:5],
                    )

    logger.info("timegap_analysis_v2: %d unique metric names", len(metric_timestamps))
    return result


def check_metrics_within_range(
    expected: Dict[str, float], actual: Dict[str, float]
) -> None:
    """Assert rolling-mean gaps are within ±25% of expected (SSIM ``check_metrics_within_range``)."""
    failures = []
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if actual_value is None:
            failures.append(f"Metric '{key}' is missing from actual values.")
            continue
        tolerance = _TOLERANCE_PCT * expected_value
        if abs(actual_value - expected_value) > tolerance:
            failures.append(
                f"Metric '{key}' actual={actual_value}, expected {expected_value} "
                f"+/- {tolerance}"
            )
        else:
            logger.info(
                "Metric '%s' actual=%s within expected %s +/- %s",
                key,
                actual_value,
                expected_value,
                tolerance,
            )
    if failures:
        pytest.fail("\n".join(failures))


def verify_metrics_sample_interval_server1(
    dut,
    collector: OtelCollector,
    hostname: str,
    cur_dir: str,
    vrf: str,
    expected_result: Dict[str, float],
    *,
    timeout_sec: int = CumulusOtelConst.SPLIT_PIPELINE_COLLECT_WAIT_SEC,
    prepare_session: bool = True,
    telemetry_unit: Optional[str] = None,
    family_map: Optional[list] = None,
) -> None:
    """Collect OTLP export and validate sample-interval gaps (SSIM server1 helper).

    ``telemetry_unit`` overrides the systemd unit for stop/copy/restart collection
    (NVOS passes ``nv-telemetry.service``); defaults to Cumulus ``nv-telemetry@<vrf>``.
    ``family_map`` overrides the metric-prefix -> gap-key pairs (NVOS metric names).
    """
    os_info = get_dut_os_info(dut)
    expected = _expected_sample_intervals_for_os(expected_result, os_info)

    if prepare_session:
        cleanup_otlp_export_session(dut, collector, vrf=vrf)

    truncate_collector_export(collector)
    with allure.step(f"Collect split-pipeline OTLP export ({timeout_sec}s)"):
        collect_telemetry_data(
            dut, collector, vrf, timeout_sec, None, cur_dir,
            telemetry_unit=telemetry_unit,
        )

    artifact = cumulus_otel_artifact_path(cur_dir)
    with allure.step("Analyze metric sample intervals (timegap v2)"):
        actual = timegap_analysis_v2(
            artifact,
            hostname,
            expected=expected,
            tolerance_pct=_TOLERANCE_PCT,
            os_info=os_info,
            family_map=family_map,
        )
        try:
            allure.attach(
                "sample-interval gaps",
                json.dumps({"expected": expected, "actual": actual}, indent=2),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Allure attach of sample-interval gaps failed: %s", exc)
        check_metrics_within_range(expected, actual)

    if os.path.isfile(artifact):
        open(artifact, "w").close()
