"""Parse OTLP collector artifacts and validate Cumulus telemetry exports."""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple, Union

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.tests_nvos.system.telemetry.otel.cumulus.comparison_logging import log_otel_cli_comparison
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.lab_topology import CumulusLabInterfaces
from ngts.tests_nvos.system.telemetry.otel.metrics_parser import (
    metric_timestamps_from_docs,
    prepare_attributes_key,
)

logger = logging.getLogger(__name__)

AttributeKey = Union[Tuple[Any, ...], str]

# Cumulus renamed env PSU OTEL series: ``..._psu_*`` → ``..._power_supply_*``.
# Canonicalize to the legacy ``psu`` form so catalogs and dual-publish windows compare cleanly.
_ENV_PSU_METRIC_PREFIX = "nvswitch_platform_environment_psu_"
_ENV_POWER_SUPPLY_METRIC_PREFIX = "nvswitch_platform_environment_power_supply_"


def _canonical_env_psu_metric_name(name: str) -> str:
    if name.startswith(_ENV_POWER_SUPPLY_METRIC_PREFIX):
        return _ENV_PSU_METRIC_PREFIX + name[len(_ENV_POWER_SUPPLY_METRIC_PREFIX):]
    return name


def _resolve_labeled_intf_attr_key(
    series: Dict[AttributeKey, Any],
    iface: str,
    *dim_tokens: str,
    labels: Tuple[str, ...] = (),
) -> Optional[AttributeKey]:
    """Match OTEL interface metric keys when OTLP attribute order differs from the test tuple."""
    exact = ("interface", iface) + dim_tokens + labels
    if exact in series:
        return exact

    candidates: List[AttributeKey] = []
    for key in series:
        if not isinstance(key, tuple) or len(key) < 2 or key[1] != iface:
            continue
        if not all(token in key for token in dim_tokens):
            continue
        if labels and not all(label in key for label in labels):
            continue
        candidates.append(key)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def _score(key: AttributeKey) -> Tuple[int, int]:
        label_hits = sum(1 for label in labels if label in key) if labels else 0
        dim_hits = sum(1 for token in dim_tokens if token in key)
        return (label_hits, dim_hits)

    return max(candidates, key=_score)


def _plat_numeric(value: Any) -> float:
    """Coerce Platform/OTEL environment values (CLI JSON often uses strings)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or text.upper() == "N/A":
            raise ValueError(f"non-numeric platform value: {value!r}")
        return float(text)
    raise TypeError(f"unsupported platform numeric type: {type(value)!r}")


def _plat_rounded_equal(otel_val: Any, cli_val: Any) -> bool:
    return round(_plat_numeric(otel_val)) == round(_plat_numeric(cli_val))


# SW #5082293: NVOS (umf-agent) and Cumulus ("Telemetry Proxy") emit OTLP differently.
# NVOS batches multiple resourceMetrics per export line and uses asInt for integer
# metrics, whereas this parser historically read only resourceMetrics[0]/scopeMetrics[0]
# and assumed asDouble. These two helpers make value extraction tolerant of both shapes.
# Once SW #5082293 aligns the NVOS export with Cumulus, these can be simplified.
def _iter_metrics(parsed_objects: List[Dict[str, Any]]):
    """Yield every metric object across all resourceMetrics/scopeMetrics (SW #5082293)."""
    for obj in parsed_objects:
        for rm in obj.get("resourceMetrics", []) or []:
            for sm in rm.get("scopeMetrics", []) or []:
                for metric in sm.get("metrics", []) or []:
                    yield metric


def _dp_number(dp: Dict[str, Any]) -> int:
    """Datapoint numeric value as int; tolerates asDouble (Cumulus) + asInt (NVOS). SW #5082293."""
    if "asDouble" in dp:
        return int(dp["asDouble"])
    if "asInt" in dp:
        return int(dp["asInt"])
    raise KeyError("datapoint has neither asDouble nor asInt")


class OtelDataValidations:
    """Parse OTLP JSON and build metric structures for Cumulus validations."""

    def __init__(self) -> None:
        self.parsed_objects: List[Dict[str, Any]] = []

    def parse_disjoint_json(self, file_path, hostname):
        """
        file_path: location of exported otel data as json file
        Returns: parsed_objects as a list of json objects, where each object is dictionary
        """
        expanded_path = os.path.expanduser(file_path)
        self.parsed_objects = []
        with open(expanded_path, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if line and hostname in line:
                    try:
                        self.parsed_objects.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        logger.debug("Skipping non-JSON line in %s: %s", file_path, exc)
        return self.parsed_objects

    def prepare_attributes_key(self, attributes: List[Dict[str, Any]]) -> AttributeKey:
        return prepare_attributes_key(attributes)

    def metricTimestamps(self, parsed_objects):
        """
        parsed_objects: List of json objects, where each object is dictionary
        return type: dict of dict as value list -> {k1: {k2: [t1, t2..]}}
                    k1 = metric name
                    k2 = tuple of attributes such as interface name, priority-group, traffic-class etc.
                    v2 = list of timestamp in unixNano [t1, t2.. ]
        """
        self.metric_timestamps = metric_timestamps_from_docs(parsed_objects)
        return self.metric_timestamps

    def metricPlatformStatsValues(self, parsed_objects):
        """
        parsed_objects: List of json objects, where each object is dictionary
        return type: dict of dict as value list -> {k1: {k2: [v1, v2..]}}
                    k1 = metric name
                    k2 = tuple of attributes such as interface name, priority-group, traffic-class etc.
                    v2 = list of values that are not zero [v1, v2.. ]
        """
        self.metric_platform_stats_values = defaultdict(lambda: defaultdict(list))
        # SW #5082293: iterate all resourceMetrics/scopeMetrics (NVOS batches several per line).
        for m in _iter_metrics(parsed_objects):
            if "nvswitch_platform" in m["name"]:
                if "gauge" in m:
                    mtype = "gauge"
                elif "sum" in m:
                    mtype = "sum"
                else:
                    logger.error("Unknown data type in platform stats: %s", m["name"])
                    continue
                for dp in m[mtype]["dataPoints"]:
                    attrs = dp.get("attributes", [])
                    attribute_key = self.prepare_attributes_key(attrs) if attrs else "No Attr"
                    self.metric_platform_stats_values[m["name"]][attribute_key].append(
                        _dp_number(dp)  # SW #5082293: asInt on NVOS, asDouble on Cumulus
                    )
        return self.metric_platform_stats_values

    def metricIntfStatsValues(self, parsed_objects):
        """
        parsed_objects: List of json objects, where each object is dictionary
        return type: dict of dict as value list -> {k1: {k2: [v1, v2..]}}
                    k1 = metric name
                    k2 = tuple of attributes such as interface name, priority-group, traffic-class etc.
                    v2 = list of values that are not zero [v1, v2.. ]
        """
        self.metric_intf_stats_values = defaultdict(lambda: defaultdict(list))
        # SW #5082293: iterate all resourceMetrics/scopeMetrics (NVOS batches several per line).
        for m in _iter_metrics(parsed_objects):
            if (
                "nvswitch_interface_shared_buffer" in m["name"] or
                "nvswitch_interface_headroom" in m["name"] or
                "nvswitch_interface" not in m["name"]
            ):
                continue
            if "gauge" in m:
                mtype = "gauge"
            elif "sum" in m:
                mtype = "sum"
            elif "histogram" in m:
                # SW #5082293: histograms (e.g. NVOS nvswitch_interface_phy_stats_rs_hist)
                # are not scalar interface counters; they are collected by
                # metricHistogramValues. Skipping here avoids int() on the
                # bucketCounts/explicitBounds lists (NVOS omits count on idle datapoints).
                # Cumulus interface-stats are gauge/sum only, so this path is NVOS-only.
                continue
            else:
                logger.error("Unknown data type in interface-stats: %s", m["name"])
                continue
            for dp in m[mtype]["dataPoints"]:
                if "attributes" in dp:
                    attribute_key = self.prepare_attributes_key(dp["attributes"])
                else:
                    attribute_key = "No Attr"
                if "asDouble" in dp or "asInt" in dp:
                    # SW #5082293: NVOS uses asInt where Cumulus uses asDouble.
                    self.metric_intf_stats_values[m["name"]][attribute_key].append(
                        _dp_number(dp)
                    )
                else:
                    pytest.fail(
                        f"No valid value in interface-stats metric {m['name']} "
                        f"datapoint: {dp}"
                    )
        return self.metric_intf_stats_values

    def metricControlPlaneStatsValues(self, parsed_objects):
        """
        parsed_objects: List of json objects, where each object is dictionary
        return type: dict of dict as value list -> {k1: {k2: [v1, v2..]}}
                    k1 = metric name
                    k2 = tuple of attributes such as interface name, priority-group, traffic-class etc.
                    v2 = list of values that are not zero [v1, v2.. ]
        """
        self.metric_control_plane_stats_values = defaultdict(lambda: defaultdict(list))
        # SW #5082293: iterate all resourceMetrics/scopeMetrics (NVOS batches several per line).
        for m in _iter_metrics(parsed_objects):
            if "nvswitch_control" in m["name"]:
                if "gauge" in m:
                    mtype = "gauge"
                elif "sum" in m:
                    mtype = "sum"
                else:
                    logger.error("Unknown data type in control-plane stats: %s", m["name"])
                    continue
                for dp in m[mtype]["dataPoints"]:
                    if "flags" in dp:
                        val = dp["flags"]
                    elif "asDouble" in dp:
                        val = dp["asDouble"]
                    elif "asInt" in dp:
                        val = dp["asInt"]  # SW #5082293: NVOS uses asInt
                    else:
                        logger.error("Datapoint has no value: %s", dp)
                        continue
                    if "attributes" in dp:
                        attribute_key = self.prepare_attributes_key(dp["attributes"])
                    else:
                        attribute_key = "No Attr"
                    self.metric_control_plane_stats_values[m["name"]][attribute_key].append(
                        int(val)
                    )
        return self.metric_control_plane_stats_values

    def metricHistogramValues(self, parsed_objects):
        """
        parsed_objects: List of json objects, where each object is dictionary
        return type: dict of dict as value list -> {k1: {k2: [v1, v2..]}}
                    k1 = metric name
                    k2 = tuple of attributes such as interface name, priority-group, traffic-class etc.
                    v2 = list of values that are not zero [v1, v2.. ]
        """
        self.metric_histogram_values = defaultdict(lambda: defaultdict(dict))
        # SW #5082293: iterate all resourceMetrics/scopeMetrics (NVOS batches several per line).
        for m in _iter_metrics(parsed_objects):
            if "histogram" in m:
                for dp in m["histogram"]["dataPoints"]:
                    if "attributes" in dp:
                        attribute_key = self.prepare_attributes_key(dp["attributes"])
                        self.metric_histogram_values[m["name"]][attribute_key].update(
                            {"bucketCounts": dp["bucketCounts"]}
                        )
                        self.metric_histogram_values[m["name"]][attribute_key].update(
                            {"explicitBounds": dp["explicitBounds"]}
                        )
        return self.metric_histogram_values

    def getHistogramList(self, parsed_objects, count=2000):
        """
        parsed_objects: List of json objects, where each object is dictionary
        count: int representing how many objects to write
        """
        hist_obj_list = []
        for i, obj in enumerate(parsed_objects):
            if i <= count:
                # SW #5082293: Cumulus put "asic-monitor" at resource attribute index [3];
                # NVOS resources have a different (and shorter) attribute list, so scan all
                # resourceMetrics/resource attributes for the marker instead of indexing.
                if any(
                    "asic-monitor" in str(attr.get("value", {}).get("stringValue", ""))
                    for rm in obj.get("resourceMetrics", []) or []
                    for attr in rm.get("resource", {}).get("attributes", []) or []
                ):
                    hist_obj_list.append(obj)
        return hist_obj_list

    def validate_collected_metrics(
        self,
        metrics_data: Dict[str, Any],
        hostname: str,
        total_metrics: Optional[Dict[str, List[str]]] = None,
        exclude_metrics: Optional[List[str]] = None,
    ) -> None:
        """Strict equality of collected vs expected metric names (SSIM secured test01)."""
        del hostname
        total_metrics = total_metrics or {}
        exclude_set = set(exclude_metrics or [])
        expected = [
            met for val in total_metrics.values() for met in val if met not in exclude_set
        ]
        if not metrics_data:
            pytest.fail("No metrics timestamps data collected.")
        collected = [name for name in metrics_data if name not in exclude_set]
        expected_set = {_canonical_env_psu_metric_name(met) for met in expected}
        collected_set = {_canonical_env_psu_metric_name(name) for name in collected}
        if collected_set != expected_set:
            extra = sorted(collected_set - expected_set)
            missing = sorted(expected_set - collected_set)
            pytest.fail(
                f"{len(extra)} more than expected metrics collected. Extra: {extra[:20]}"
                f"{' ...' if len(extra) > 20 else ''}. "
                f"{len(missing)} missing metrics. Missing: {missing[:20]}"
                f"{' ...' if len(missing) > 20 else ''}."
            )
        logger.info("Secured metrics validation passed: %d metrics", len(collected_set))

    def validate_collected_metrics_attributes_count(
        self,
        metrics_data: Dict[str, Any],
        hostname: str,
        total_metrics: Optional[Dict[str, List[str]]] = None,
        exclude_metrics: Optional[List[str]] = None,
    ) -> None:
        """Validate metric attribute tuple counts (mlx-3/4/5 lab topologies)."""
        if not any(tag in hostname for tag in ("mlx-3", "mlx-4", "mlx-5")):
            logger.info(
                "Skipping attribute-count validation on hostname=%s (mlx-3/4/5 lab only)",
                hostname,
            )
            return
        total_metrics = total_metrics or {}
        exclude_metrics = set(exclude_metrics or [])
        logger.info(
            "Validating metric attribute tuple counts (test01, hostname=%s)",
            hostname,
        )

        def _expect_count(metric_name: str, expected: int, *, bucket: str) -> None:
            if metric_name in exclude_metrics or metric_name not in metrics_data:
                return
            actual = len(metrics_data[metric_name])
            if actual != expected:
                pytest.fail(
                    f"Not all attribute tuples found for {metric_name} "
                    f"(bucket={bucket}): expected {expected}, got {actual}"
                )

        if "PH1_INT_STAT" in total_metrics:
            for met in total_metrics["PH1_INT_STAT"]:
                _expect_count(met, 24, bucket="PH1_INT_STAT")
        if "PH1_HIST" in total_metrics:
            for met in total_metrics["PH1_HIST"]:
                _expect_count(met, 24, bucket="PH1_HIST")
        if "PH1_ADD_STAT" in total_metrics:
            _expect_count("nvswitch_interface_oper_state", 3, bucket="PH1_ADD_STAT")
            _expect_count(
                "nvswitch_interface_performance_marked_packets", 6, bucket="PH1_ADD_STAT"
            )
        if "PH2_ADD_INT_STAT" in total_metrics:
            for met in total_metrics["PH2_ADD_INT_STAT"]:
                _expect_count(met, 3, bucket="PH2_ADD_INT_STAT")
        if "PH2_INT_CAR_CHG" in total_metrics:
            expected = None
            if "mlx-3" in hostname:
                expected = 35
            elif "mlx-4" in hostname:
                expected = 35
            elif "mlx-5" in hostname:
                expected = 71
            if expected is not None:
                for met in total_metrics["PH2_INT_CAR_CHG"]:
                    _expect_count(met, expected, bucket="PH2_INT_CAR_CHG")
        if "PH2_INT_DISC_STATS" in total_metrics:
            for met in total_metrics["PH2_INT_DISC_STATS"]:
                _expect_count(met, 3, bucket="PH2_INT_DISC_STATS")
        if "PH2_INT_ETHER_STATS" in total_metrics:
            for met in total_metrics["PH2_INT_ETHER_STATS"]:
                _expect_count(met, 3, bucket="PH2_INT_ETHER_STATS")
        if "PH2_SW_PRIO_STATS" in total_metrics:
            for met in total_metrics["PH2_SW_PRIO_STATS"]:
                _expect_count(met, 15, bucket="PH2_SW_PRIO_STATS")
        if "PH2_PKT_DIST_STATS" in total_metrics:
            for met in total_metrics["PH2_PKT_DIST_STATS"]:
                _expect_count(met, 3, bucket="PH2_PKT_DIST_STATS")
        if "PH2_HIST" in total_metrics:
            _expect_count("nvswitch_histogram_interface_latency", 24, bucket="PH2_HIST")
            _expect_count("nvswitch_histogram_interface_counter", 6, bucket="PH2_HIST")
        if "PH3_ROUTING_STATS" in total_metrics:
            for met in total_metrics["PH3_ROUTING_STATS"]:
                _expect_count(met, 52, bucket="PH3_ROUTING_STATS")
        if "PH4_ROUTING_STATS" in total_metrics or "PH5_ROUTING_STATS" in total_metrics:
            routing_bucket = (
                "PH5_ROUTING_STATS"
                if "PH5_ROUTING_STATS" in total_metrics
                else "PH4_ROUTING_STATS"
            )
            for met in total_metrics[routing_bucket]:
                if met == "nvrouting_rib_count":
                    _expect_count(met, 728, bucket=routing_bucket)
                elif met == "nvrouting_rib_nhg_count":
                    _expect_count(met, 1, bucket=routing_bucket)
                else:
                    _expect_count(met, 52, bucket=routing_bucket)

    @staticmethod
    def get_percentage_deviation(cli_data: float, otel_data: float) -> float:
        """Percent difference between CLI reference and OTEL sample."""
        if cli_data == 0:
            return 0.0 if otel_data == 0 else 100.0
        return abs(cli_data - otel_data) / cli_data * 100

    def validate_interface_pg_rx(
        self,
        otel_intf_stats: Dict[str, Any],
        cli_ingress_stats: Dict[str, Any],
        lab_ifaces: CumulusLabInterfaces,
        *,
        max_deviation_pct: float = 25,
    ) -> None:
        """test02a: ``nvswitch_interface_pg_rx_frames`` vs ingress-buffer-stats CLI."""
        if not otel_intf_stats:
            logger.info("No intf_stats in cache; skipping test02a")
            return
        metric = "nvswitch_interface_pg_rx_frames"
        if metric not in otel_intf_stats:
            pytest.fail(f"{metric} not present in OTEL intf_stats export")

        attr_key = _resolve_labeled_intf_attr_key(
            otel_intf_stats[metric],
            lab_ifaces.rx_iface,
            "pg",
            "0",
            labels=lab_ifaces.labels,
        )
        if attr_key is None:
            expected = ("interface", lab_ifaces.rx_iface, "pg", "0") + lab_ifaces.labels
            pytest.fail(f"{metric} missing attribute key for {lab_ifaces.rx_iface!r} (expected {expected!r})")

        d_otel = round(mean(otel_intf_stats[metric][attr_key]))
        ingress = cli_ingress_stats.get(lab_ifaces.rx_iface, {})
        pg0 = ingress.get("0", ingress) if isinstance(ingress, dict) else {}
        d_cli = _plat_numeric(pg0.get("rx-frames", 0))

        with allure.step(f"Validate {metric} vs CLI"):
            deviation = self.get_percentage_deviation(d_cli, d_otel)
            log_otel_cli_comparison(
                metric,
                otel_value=d_otel,
                reference_value=d_cli,
                reference_label="CLI",
                deviation_pct=deviation,
                max_deviation_pct=max_deviation_pct,
                attr_key=attr_key,
                passed=deviation <= max_deviation_pct,
            )
            if deviation > max_deviation_pct:
                pytest.fail(
                    f"{metric} validation failed: otel={d_otel}, cli={d_cli}, "
                    f"deviation={deviation:.1f}% (max {max_deviation_pct}%)"
                )

    def validate_interface_tc_tx(
        self,
        otel_intf_stats: Dict[str, Any],
        cli_egress_stats: Dict[str, Any],
        lab_ifaces: CumulusLabInterfaces,
        *,
        max_deviation_pct: float = 30,
    ) -> None:
        """test02b: ``nvswitch_interface_tc_tx_frames`` vs egress-queue-stats CLI."""
        if not otel_intf_stats:
            logger.info("No intf_stats in cache; skipping test02b")
            return
        metric = "nvswitch_interface_tc_tx_frames"
        if metric not in otel_intf_stats:
            pytest.fail(f"{metric} not present in OTEL intf_stats export")

        attr_key = _resolve_labeled_intf_attr_key(
            otel_intf_stats[metric],
            lab_ifaces.tx_iface,
            "tc",
            "0",
            labels=lab_ifaces.labels,
        )
        if attr_key is None:
            expected = ("interface", lab_ifaces.tx_iface, "tc", "0") + lab_ifaces.labels
            pytest.fail(f"{metric} missing attribute key for {lab_ifaces.tx_iface!r} (expected {expected!r})")

        d_otel = round(mean(otel_intf_stats[metric][attr_key]))
        egress = cli_egress_stats.get(lab_ifaces.tx_iface, {})
        tc0 = egress.get("0", egress) if isinstance(egress, dict) else {}
        d_cli = _plat_numeric(tc0.get("tx-frames", 0))

        with allure.step(f"Validate {metric} vs CLI"):
            deviation = self.get_percentage_deviation(d_cli, d_otel)
            log_otel_cli_comparison(
                metric,
                otel_value=d_otel,
                reference_value=d_cli,
                reference_label="CLI",
                deviation_pct=deviation,
                max_deviation_pct=max_deviation_pct,
                attr_key=attr_key,
                passed=deviation <= max_deviation_pct,
            )
            if deviation > max_deviation_pct:
                pytest.fail(
                    f"{metric} validation failed: otel={d_otel}, cli={d_cli}, "
                    f"deviation={deviation:.1f}% (max {max_deviation_pct}%)"
                )

    def validate_histogram_counter(
        self,
        otel_histograms: Dict[str, Any],
        hist_snap: Dict[str, Any],
        lab_ifaces: CumulusLabInterfaces,
        *,
        max_deviation_pct: float = 10,
    ) -> None:
        """test03a: first bucket of ``nvswitch_histogram_interface_counter`` vs snapshot."""
        if not otel_histograms or not hist_snap:
            logger.info("No histograms or hist_snap; skipping test03a")
            return
        metric = "nvswitch_histogram_interface_counter"
        snap_iface = hist_snap.get("histogram_counter_info", {}).get(lab_ifaces.test_iface, {})
        crc = snap_iface.get("crc", {}) if isinstance(snap_iface, dict) else {}
        data = crc.get("data", {}) if isinstance(crc, dict) else {}
        if not data:
            pytest.fail("histogram_counter_info snapshot missing crc data")
        snap_first_bucket = sorted(data.keys())[0]
        d_snap = _plat_numeric(data[snap_first_bucket])

        attr_key = _resolve_labeled_intf_attr_key(
            otel_histograms[metric],
            lab_ifaces.test_iface,
            "type",
            "crc",
            labels=lab_ifaces.labels,
        )
        if attr_key is None:
            expected = ("interface", lab_ifaces.test_iface, "type", "crc") + lab_ifaces.labels
            pytest.fail(f"{metric} missing attribute key for {lab_ifaces.test_iface!r} (expected {expected!r})")
        buckets = otel_histograms[metric][attr_key]["bucketCounts"]
        d_otel = int(buckets[0])

        with allure.step("Validate nvswitch_histogram_interface_counter vs snapshot"):
            deviation = self.get_percentage_deviation(d_snap, d_otel)
            log_otel_cli_comparison(
                metric,
                otel_value=d_otel,
                reference_value=d_snap,
                reference_label="snapshot",
                deviation_pct=deviation,
                max_deviation_pct=max_deviation_pct,
                attr_key=attr_key,
                passed=deviation <= max_deviation_pct,
            )
            if deviation > max_deviation_pct:
                pytest.fail(
                    f"{metric} validation failed: otel={d_otel}, snapshot={d_snap}, "
                    f"deviation={deviation:.1f}%"
                )

    def validate_histogram_ingress_buffer(
        self,
        otel_histograms: Dict[str, Any],
        hist_snap: Dict[str, Any],
        lab_ifaces: CumulusLabInterfaces,
        *,
        max_deviation_pct: float = 10,
    ) -> None:
        """test03b: ``nvswitch_histogram_interface_ingress_buffer`` vs snapshot."""
        if not otel_histograms or not hist_snap:
            logger.info("No histograms or hist_snap; skipping test03b")
            return
        metric = "nvswitch_histogram_interface_ingress_buffer"
        pg_info = hist_snap.get("histogram_pg_info", {}).get(lab_ifaces.test_iface, {})
        pg0 = pg_info.get("0", {}) if isinstance(pg_info, dict) else {}
        data = pg0.get("data", {}) if isinstance(pg0, dict) else {}
        if not data:
            pytest.fail("histogram_pg_info snapshot missing pg 0 data")
        snap_first_bucket = sorted(data.keys())[0]
        d_snap = _plat_numeric(data[snap_first_bucket])

        attr_key = _resolve_labeled_intf_attr_key(
            otel_histograms[metric],
            lab_ifaces.test_iface,
            "pg",
            "0",
            labels=lab_ifaces.labels,
        )
        if attr_key is None:
            expected = ("interface", lab_ifaces.test_iface, "pg", "0") + lab_ifaces.labels
            pytest.fail(f"{metric} missing attribute key for {lab_ifaces.test_iface!r} (expected {expected!r})")
        d_otel = int(otel_histograms[metric][attr_key]["bucketCounts"][0])

        with allure.step("Validate nvswitch_histogram_interface_ingress_buffer vs snapshot"):
            deviation = self.get_percentage_deviation(d_snap, d_otel)
            log_otel_cli_comparison(
                metric,
                otel_value=d_otel,
                reference_value=d_snap,
                reference_label="snapshot",
                deviation_pct=deviation,
                max_deviation_pct=max_deviation_pct,
                attr_key=attr_key,
                passed=deviation <= max_deviation_pct,
            )
            if deviation > max_deviation_pct:
                pytest.fail(
                    f"{metric} validation failed: otel={d_otel}, snapshot={d_snap}, "
                    f"deviation={deviation:.1f}%"
                )

    def validate_histogram_egress_buffer(
        self,
        otel_histograms: Dict[str, Any],
        hist_snap: Dict[str, Any],
        lab_ifaces: CumulusLabInterfaces,
        *,
        max_deviation_pct: float = 10,
    ) -> None:
        """test03c: ``nvswitch_histogram_interface_egress_buffer`` vs snapshot."""
        if not otel_histograms or not hist_snap:
            logger.info("No histograms or hist_snap; skipping test03c")
            return
        metric = "nvswitch_histogram_interface_egress_buffer"
        tc_info = hist_snap.get("histogram_tc_info", {}).get(lab_ifaces.test_iface, {})
        tc0 = tc_info.get("0", {}) if isinstance(tc_info, dict) else {}
        data = tc0.get("data", {}) if isinstance(tc0, dict) else {}
        if not data:
            pytest.fail("histogram_tc_info snapshot missing tc 0 data")
        snap_first_bucket = sorted(data.keys())[0]
        d_snap = _plat_numeric(data[snap_first_bucket])

        attr_key = _resolve_labeled_intf_attr_key(
            otel_histograms[metric],
            lab_ifaces.test_iface,
            "tc",
            "0",
            labels=lab_ifaces.labels,
        )
        if attr_key is None:
            expected = ("interface", lab_ifaces.test_iface, "tc", "0") + lab_ifaces.labels
            pytest.fail(f"{metric} missing attribute key for {lab_ifaces.test_iface!r} (expected {expected!r})")
        d_otel = int(otel_histograms[metric][attr_key]["bucketCounts"][0])

        with allure.step("Validate nvswitch_histogram_interface_egress_buffer vs snapshot"):
            deviation = self.get_percentage_deviation(d_snap, d_otel)
            log_otel_cli_comparison(
                metric,
                otel_value=d_otel,
                reference_value=d_snap,
                reference_label="snapshot",
                deviation_pct=deviation,
                max_deviation_pct=max_deviation_pct,
                attr_key=attr_key,
                passed=deviation <= max_deviation_pct,
            )
            if deviation > max_deviation_pct:
                pytest.fail(
                    f"{metric} validation failed: otel={d_otel}, snapshot={d_snap}, "
                    f"deviation={deviation:.1f}%"
                )

    def validate_histogram_latency(
        self,
        otel_histograms: Dict[str, Any],
        hist_snap: Dict[str, Any],
        lab_ifaces: CumulusLabInterfaces,
        *,
        max_deviation_pct: float = 10,
    ) -> None:
        """test03d: all buckets of ``nvswitch_histogram_interface_latency`` vs snapshot."""
        if not otel_histograms or not hist_snap:
            logger.info("No histograms or hist_snap; skipping test03d")
            return
        metric = "nvswitch_histogram_interface_latency"
        lat_info = hist_snap.get("histogram_latency_info", {}).get(lab_ifaces.test_iface, {})
        tc0 = lat_info.get("0", {}) if isinstance(lat_info, dict) else {}
        data = tc0.get("data", {}) if isinstance(tc0, dict) else {}
        if not data:
            pytest.fail("histogram_latency_info snapshot missing tc 0 data")

        attr_key = _resolve_labeled_intf_attr_key(
            otel_histograms[metric],
            lab_ifaces.test_iface,
            "tc",
            "0",
            labels=lab_ifaces.labels,
        )
        if attr_key is None:
            expected = ("interface", lab_ifaces.test_iface, "tc", "0") + lab_ifaces.labels
            pytest.fail(f"{metric} missing attribute key for {lab_ifaces.test_iface!r} (expected {expected!r})")
        snap_list = list(data.values())
        otel_list = [int(x) for x in otel_histograms[metric][attr_key]["bucketCounts"]]

        with allure.step("Validate nvswitch_histogram_interface_latency vs snapshot"):
            if len(snap_list) != len(otel_list):
                pytest.fail(
                    f"{metric} bucket count mismatch: snapshot={len(snap_list)} otel={len(otel_list)}"
                )
            for idx, (d_snap, d_otel) in enumerate(zip(snap_list, otel_list)):
                d_snap_num = _plat_numeric(d_snap)
                if d_snap_num == 0 and d_otel == 0:
                    logger.info(
                        "OTEL vs snapshot [PASS] %s bucket=%d: otel=0 snapshot=0 (skipped)",
                        metric,
                        idx,
                    )
                    continue
                deviation = self.get_percentage_deviation(d_snap_num, d_otel)
                log_otel_cli_comparison(
                    f"{metric} bucket={idx}",
                    otel_value=d_otel,
                    reference_value=d_snap_num,
                    reference_label="snapshot",
                    deviation_pct=deviation,
                    max_deviation_pct=max_deviation_pct,
                    attr_key=attr_key,
                    passed=deviation <= max_deviation_pct,
                )
                if deviation > max_deviation_pct:
                    pytest.fail(
                        f"{metric} bucket {idx} failed: otel={d_otel}, snapshot={d_snap_num}, "
                        f"deviation={deviation:.1f}%"
                    )

    def validate_histogram_structure(
        self, histogram_obj_list: List[Dict[str, Any]], *, hostname: str = ""
    ) -> None:
        """test03e: histogram resource attributes and datapoint schema."""
        if not histogram_obj_list:
            logger.info("No hist_list in cache; skipping test03e")
            return
        logger.info(
            "Histogram structure validation: %d asic-monitor OTLP object(s), hostname=%r",
            len(histogram_obj_list),
            hostname or "(not checked)",
        )
        with allure.step("Validate histogram resource attributes"):
            self.validate_histogram_resource_attributes(histogram_obj_list, hostname=hostname)
        with allure.step("Validate histogram metric datapoints"):
            self.validate_histogram_metrics_datapoints(histogram_obj_list)

    def validate_histogram_resource_attributes(
        self, histogram_obj_list: List[Dict[str, Any]], *, hostname: str = ""
    ) -> None:
        """Histogram OTLP objects must include asic-monitor resource and host identity."""
        for hist_obj in histogram_obj_list:
            attrs = hist_obj["resourceMetrics"][0]["resource"]["attributes"]
            string_values = [
                attr.get("value", {}).get("stringValue", "")
                for attr in attrs
                if isinstance(attr.get("value"), dict)
            ]
            if not any("asic-monitor" in value for value in string_values):
                pytest.fail("Histogram resource missing asic-monitor identifier")
            if hostname and not any(hostname in value for value in string_values):
                pytest.fail(
                    f"Histogram resource host name does not include DUT hostname {hostname!r}"
                )

    def validate_histogram_metrics_datapoints(self, histogram_obj_list: List[Dict[str, Any]]) -> None:
        """Validate allowed histogram metric names, units, and bucket layout."""
        allowed = (
            "nvswitch_histogram_interface_egress_buffer",
            "nvswitch_histogram_interface_ingress_buffer",
            "nvswitch_histogram_interface_latency",
            "nvswitch_histogram_interface_counter",
        )
        for hist_obj in histogram_obj_list:
            for metric in hist_obj["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]:
                name = metric["name"]
                if name not in allowed:
                    pytest.fail(f"Invalid histogram metric name found: {name}")
                if name in (
                    "nvswitch_histogram_interface_egress_buffer",
                    "nvswitch_histogram_interface_ingress_buffer",
                ) and metric.get("unit") != "bytes":
                    pytest.fail(f"histogram metric unit {name}: {metric.get('unit')}")
                if name == "nvswitch_histogram_interface_counter" and metric.get("unit") != "counter":
                    pytest.fail(f"histogram metric unit {name}: {metric.get('unit')}")
                if name == "nvswitch_histogram_interface_latency" and metric.get("unit") != "packets":
                    pytest.fail(f"histogram metric unit {name}: {metric.get('unit')}")
                for dp in metric["histogram"]["dataPoints"]:
                    if len(dp["bucketCounts"]) != 10:
                        pytest.fail(f"Invalid bucketCounts length in {name}: {dp['bucketCounts']}")
                    if len(dp["explicitBounds"]) != 9:
                        pytest.fail(f"Invalid explicitBounds length in {name}: {dp['explicitBounds']}")

    def cp_stats_data_validation(
        self,
        otel_cp_stats: Dict[str, Any],
        cli_cp_stats: Dict[str, Any],
    ) -> None:
        """test04: global control-plane counters (OTEL samples bounded by CLI pre/post)."""
        if not otel_cp_stats or not cli_cp_stats:
            logger.info("No cp_stats in cache; skipping test04")
            return
        cli_pre = cli_cp_stats.get("pre") or {}
        cli_post = cli_cp_stats.get("post") or {}
        if not cli_pre or not cli_post:
            pytest.fail("cp_stats cache must contain pre and post CLI snapshots")

        checks = (
            (
                "nvswitch_control_plane_rx_buffer_drops",
                "ToCpuBufferDrop",
                "rx_buffer_drops",
            ),
            ("nvswitch_control_plane_rx_bytes", "ToCpuByte", "rx_bytes"),
            ("nvswitch_control_plane_rx_packets", "ToCpuPacket", "rx_packets"),
            ("nvswitch_control_plane_tx_bytes", "FromCpuControlByte", "tx_bytes"),
            ("nvswitch_control_plane_tx_packets", "FromCpuControlPacket", "tx_packets"),
        )
        global_pre = cli_pre.get("Global", {})
        global_post = cli_post.get("Global", {})

        for metric_name, cli_key, label in checks:
            if metric_name not in otel_cp_stats:
                logger.warning("Skipping %s — not in OTEL export", metric_name)
                continue
            otel_series = next(iter(otel_cp_stats[metric_name].values()))
            d_otel_pre = otel_series[0]
            d_otel_post = otel_series[-1]
            d_cli_pre = global_pre.get(cli_key)
            d_cli_post = global_post.get(cli_key)
            if d_cli_pre is None or d_cli_post is None:
                pytest.fail(f"CLI Global missing {cli_key} for {label}")

            with allure.step(f"Validate {metric_name}"):
                passed = d_otel_pre >= d_cli_pre and d_otel_post <= d_cli_post
                logger.info(
                    "OTEL vs CLI [%s] %s (%s): pre otel=%s cli=%s | post otel=%s cli=%s",
                    "PASS" if passed else "FAIL",
                    metric_name,
                    label,
                    d_otel_pre,
                    d_cli_pre,
                    d_otel_post,
                    d_cli_post,
                )
                if not passed:
                    pytest.fail(
                        f"{metric_name} out of CLI bounds: "
                        f"otel_pre={d_otel_pre} cli_pre={d_cli_pre} "
                        f"otel_post={d_otel_post} cli_post={d_cli_post}"
                    )

    def traps_data_validation(
        self,
        otel_data: Dict[str, Any],
        cli_data_pre: Dict[str, Any],
        cli_data_post: Dict[str, Any],
    ) -> None:
        """test04: per-trap control-plane counters (OTEL samples bounded by CLI pre/post).

        The OTEL keys carry extra labels (``group_name``, ``trap_name``) beyond the
        CLI-provided group / trap_id, so :meth:`_find_otel_key_by_group_and_trap`
        resolves the matching series. Mismatches are logged (not failed): these
        counters are volatile between the pre/post CLI reads, matching SSIM behaviour.
        """
        logger.info("Running cp_trap_stats data validation")
        trap_pattern = re.compile(r"Trap\s+(\d+) \(Group (\d+)\)")
        with allure.step("Validate control-plane trap counters"):
            for trap_key in cli_data_pre.keys():
                match = trap_pattern.match(trap_key)
                if not match:
                    continue
                trap_id, group_id = match.groups()
                trap_id, group_id = str(int(trap_id)), str(int(group_id))
                control_plane_trap_pre = cli_data_pre[trap_key]
                control_plane_trap_post = cli_data_post[trap_key]
                for trap_counter in control_plane_trap_pre.keys():
                    d_cli_pre = control_plane_trap_pre[trap_counter]
                    d_cli_post = control_plane_trap_post[trap_counter]
                    otel_metric_name = CumulusOtelConst.TRAPS_CONVERSION_CLI_TO_OTEL.get(trap_counter)
                    if otel_metric_name is None or otel_metric_name not in otel_data:
                        continue
                    otel_metric_data = otel_data[otel_metric_name]
                    otel_key = self._find_otel_key_by_group_and_trap(
                        otel_metric_data, group_id, trap_id
                    )
                    if otel_key is None:
                        logger.error(
                            "No OTEL data found for trap %s (group=%s, trap_id=%s)",
                            trap_key,
                            group_id,
                            trap_id,
                        )
                        continue
                    d_otel = otel_metric_data[otel_key]
                    d_otel_pre = d_otel[0]
                    d_otel_post = d_otel[-1]
                    if d_otel_pre < d_cli_pre or d_otel_post > d_cli_post:
                        logger.error(
                            "Data validation for cp_trap_stats %s failed for trap %s, "
                            "otel: %s, cli: %s",
                            trap_counter,
                            trap_key,
                            d_otel_post,
                            d_cli_post,
                        )

    def trap_groups_data_validation(
        self,
        otel_data: Dict[str, Any],
        cli_data_pre: Dict[str, Any],
        cli_data_post: Dict[str, Any],
    ) -> None:
        """test04: per-trap-group control-plane counters (bytes / packets / violations).

        Compares the "Group Counters" CLI section against the exported group metrics;
        an OTEL series absent for a group is treated as 0. Mismatches are logged (not
        failed), mirroring SSIM, since trap-group counters move between the CLI reads.
        """
        otel_cp_stats_bytes = otel_data.get(
            "nvswitch_control_plane_trap_group_rx_bytes", {}
        )
        otel_cp_stats_pkts = otel_data.get(
            "nvswitch_control_plane_trap_group_rx_packets", {}
        )
        otel_cp_stats_violated_pkts = otel_data.get(
            "nvswitch_control_plane_trap_group_pkt_violations", {}
        )

        with allure.step("Validate control-plane trap-group counters"):
            for group_key, group_data in cli_data_pre.items():
                group_id = str(int(group_key.split()[1]))
                to_cpu_bytes_pre = group_data["ToCpuByte"]
                to_cpu_pkts_pre = group_data["ToCpuPacket"]
                drops_pre = group_data["Drops"]
                group_data_post = cli_data_post[group_key]
                to_cpu_bytes_post = group_data_post["ToCpuByte"]
                to_cpu_pkts_post = group_data_post["ToCpuPacket"]
                drops_post = group_data_post["Drops"]

                logger.info(
                    "Group ID: %s, pre: to-cpu-bytes=%s to-cpu-pkts=%s violated-pkts=%s | "
                    "post: to-cpu-bytes=%s to-cpu-pkts=%s violated-pkts=%s",
                    group_id,
                    to_cpu_bytes_pre,
                    to_cpu_pkts_pre,
                    drops_pre,
                    to_cpu_bytes_post,
                    to_cpu_pkts_post,
                    drops_post,
                )

                otel_bytes_pre, otel_bytes_post = self._otel_group_bounds(
                    otel_cp_stats_bytes, group_id
                )
                otel_pkts_pre, otel_pkts_post = self._otel_group_bounds(
                    otel_cp_stats_pkts, group_id
                )
                otel_violated_pre, otel_violated_post = self._otel_group_bounds(
                    otel_cp_stats_violated_pkts, group_id
                )

                comparisons = zip(
                    [to_cpu_bytes_pre, to_cpu_pkts_pre, drops_pre],
                    [to_cpu_bytes_post, to_cpu_pkts_post, drops_post],
                    [otel_bytes_pre, otel_pkts_pre, otel_violated_pre],
                    [otel_bytes_post, otel_pkts_post, otel_violated_post],
                    ["bytes", "packets", "violated packets"],
                )
                for d_cli_pre, d_cli_post, d_otel_pre, d_otel_post, kind in comparisons:
                    if d_otel_pre < d_cli_pre or d_otel_post > d_cli_post:
                        logger.error(
                            "%s mismatch: CLI reported %s %s but metrics endpoint "
                            "reported %s %s for group %s",
                            kind,
                            d_cli_post,
                            kind,
                            d_otel_post,
                            kind,
                            group_id,
                        )
                logger.info(
                    "Validated trap group %s bytes=%s packets=%s violated packets=%s",
                    group_id,
                    to_cpu_bytes_post,
                    to_cpu_pkts_post,
                    drops_post,
                )

    def _otel_group_bounds(
        self, otel_metric_data: Dict[Any, Any], group_id: str
    ) -> Tuple[int, int]:
        """Return (pre, post) OTEL samples for ``group_id``; (0, 0) when absent."""
        otel_key = self._find_otel_key_by_group(otel_metric_data, group_id)
        if otel_key is None:
            return 0, 0
        series = otel_metric_data[otel_key]
        return series[0], series[-1]

    @staticmethod
    def _find_otel_key_by_group_and_trap(
        otel_metric_data: Dict[Any, Any], group_id: str, trap_id: str
    ) -> Optional[Tuple[Any, ...]]:
        """Find the OTEL attribute key matching both ``group_id`` and ``trap_id``.

        OTEL keys are flat label tuples like
        ``('group', '0', 'group_name', 'catch_all', 'trap_id', '321', 'trap_name', ...)``.
        """
        for key in otel_metric_data.keys():
            key_dict = dict(zip(key[::2], key[1::2]))
            if key_dict.get("group") == group_id and key_dict.get("trap_id") == trap_id:
                return key
        return None

    @staticmethod
    def _find_otel_key_by_group(
        otel_metric_data: Dict[Any, Any], group_id: str
    ) -> Optional[Tuple[Any, ...]]:
        """Find the OTEL attribute key matching ``group_id`` (ignoring extra labels)."""
        for key in otel_metric_data.keys():
            key_dict = dict(zip(key[::2], key[1::2]))
            if key_dict.get("group") == group_id:
                return key
        return None

    def plat_stats_data_validation(
        self,
        metrics_platstats: Dict[str, Any],
        cli_plat_env_temp: Dict[str, Any],
        cli_plat_env_psu: Dict[str, Any],
        cli_plat_env_fan: Dict[str, Any],
        *,
        platform_profile: str = "cumulus",
    ) -> None:
        """Compare OTEL platform environment metrics against CLI snapshots.

        ``platform_profile`` selects platform behavior. NVOS exports the same
        ``nvswitch_platform_environment_*`` metric names as Cumulus, so the
        comparison is shared. On ``nvos`` an exported sensor whose CLI snapshot is
        missing is collected and the test fails once at the end with the full list
        (instead of Cumulus's fail-fast ``KeyError``), so every gap is reported in
        a single run rather than silently passing.
        """
        del cli_plat_env_fan  # fan validation blocked (redmine 4134074)

        is_nvos = platform_profile == "nvos"
        state_map = {0: "absent", 1: "ok", 2: "failed", 3: "bad"}
        if not metrics_platstats:
            logger.info("No platform_stats in telemetry cache; skipping test05 validation")
            return

        cli_plat_env_temp = cli_plat_env_temp or {}
        cli_plat_env_psu = cli_plat_env_psu or {}

        logger.info(
            "Platform stats validation (%s): otel metrics=%d temp_cli_sensors=%d psu_cli=%d",
            platform_profile,
            len(metrics_platstats),
            len(cli_plat_env_temp),
            len(cli_plat_env_psu),
        )

        # OTEL sensors we couldn't match to a CLI snapshot. On NVOS we keep going so
        # the whole gap is reported at once (see the aggregated check at the end);
        # Cumulus keeps failing fast via KeyError.
        missing_cli: set = set()

        def _temp_cli(sensor: AttributeKey) -> Optional[Dict[str, Any]]:
            """CLI temp snapshot for ``sensor``; None (recorded) when absent on NVOS."""
            key = str(sensor[1]).replace(" ", "-")
            if key in cli_plat_env_temp:
                return cli_plat_env_temp[key]
            if is_nvos:
                missing_cli.add(f"temp sensor {key!r}")
                return None
            raise KeyError(key)

        def _psu_cli(psu_id: Any) -> Optional[Dict[str, Any]]:
            """CLI psu snapshot for ``psu_id``; None (recorded) when absent on NVOS.

            The OTLP psu name comes through as ``PSU 3`` while the CLI keys it as
            ``PSU3``, so fall back to a space-stripped lookup (mirrors the temp
            sensor normalization above).
            """
            for key in (psu_id, str(psu_id).replace(" ", "")):
                if key in cli_plat_env_psu:
                    return cli_plat_env_psu[key]
            if is_nvos:
                missing_cli.add(f"psu {psu_id!r}")
                return None
            raise KeyError(psu_id)

        def _psu_metric_series(leaf: str) -> Tuple[str, Dict[AttributeKey, Any]]:
            """Prefer renamed ``power_supply_<leaf>`` series, else legacy ``psu_<leaf>``."""
            new_name = _ENV_POWER_SUPPLY_METRIC_PREFIX + leaf
            old_name = _ENV_PSU_METRIC_PREFIX + leaf
            if new_name in metrics_platstats and metrics_platstats[new_name]:
                return new_name, metrics_platstats[new_name]
            return old_name, metrics_platstats.get(old_name, {})

        with allure.step("Validate nvswitch_platform_environment_temp_state"):
            for sensor in metrics_platstats.get(
                "nvswitch_platform_environment_temp_state", {}
            ):
                cli = _temp_cli(sensor)
                if cli is None:
                    continue
                d_otel = metrics_platstats["nvswitch_platform_environment_temp_state"][sensor][-1]
                d_cli = cli["state"]
                otel_state = state_map[d_otel]
                passed = otel_state == d_cli
                log_otel_cli_comparison(
                    "nvswitch_platform_environment_temp_state",
                    otel_value=otel_state,
                    reference_value=d_cli,
                    attr_key=sensor,
                    passed=passed,
                )
                if not passed:
                    pytest.fail(
                        f"Data validation for nvswitch_platform_environment_temp_state failed, "
                        f"otel: {otel_state}, cli: {d_cli}"
                    )

        with allure.step("Validate nvswitch_platform_environment_temp_min"):
            for sensor in metrics_platstats.get("nvswitch_platform_environment_temp_min", {}):
                cli = _temp_cli(sensor)
                if cli is None:
                    continue
                if cli["state"] == "ok":
                    d_otel = metrics_platstats["nvswitch_platform_environment_temp_min"][sensor][-1]
                    d_cli = cli["min"]
                    passed = _plat_rounded_equal(d_otel, d_cli)
                    log_otel_cli_comparison(
                        "nvswitch_platform_environment_temp_min",
                        otel_value=d_otel,
                        reference_value=d_cli,
                        attr_key=sensor,
                        passed=passed,
                    )
                    if not passed:
                        pytest.fail(
                            f"Data validation for nvswitch_platform_environment_temp_min failed, "
                            f"otel: {d_otel}, cli: {d_cli}"
                        )

        with allure.step("Validate nvswitch_platform_environment_temp_max"):
            logger.info("Running nvswitch_platform_environment_temp_max data validation")
            for sensor in metrics_platstats.get("nvswitch_platform_environment_temp_max", {}):
                cli = _temp_cli(sensor)
                if cli is None:
                    continue
                if cli["state"] == "ok":
                    d_otel = metrics_platstats["nvswitch_platform_environment_temp_max"][sensor][-1]
                    d_cli = cli["max"]
                    if not _plat_rounded_equal(d_otel, d_cli):
                        pytest.fail(
                            f"Data validation for nvswitch_platform_environment_temp_max failed, "
                            f"otel: {d_otel}, cli: {d_cli}"
                        )

        with allure.step("Validate nvswitch_platform_environment_temp_current"):
            for sensor in metrics_platstats.get(
                "nvswitch_platform_environment_temp_current", {}
            ):
                cli = _temp_cli(sensor)
                if cli is None:
                    continue
                if cli["state"] == "ok":
                    d_otel = _plat_numeric(
                        metrics_platstats["nvswitch_platform_environment_temp_current"][sensor][
                            -1
                        ]
                    )
                    d_cli = _plat_numeric(cli["current"])
                    deviation = self.get_percentage_deviation(round(d_cli), round(d_otel))
                    log_otel_cli_comparison(
                        "nvswitch_platform_environment_temp_current",
                        otel_value=d_otel,
                        reference_value=d_cli,
                        deviation_pct=deviation,
                        max_deviation_pct=30,
                        attr_key=sensor,
                        passed=deviation <= 30,
                    )
                    if deviation > 30:
                        pytest.fail(
                            f"Data validation for nvswitch_platform_environment_temp_current failed, "
                            f"otel: {d_otel}, cli: {d_cli}"
                        )

        with allure.step("Validate nvswitch_platform_environment_temp_crit"):
            logger.info("Running nvswitch_platform_environment_temp_crit data validation")
            for sensor in metrics_platstats.get("nvswitch_platform_environment_temp_crit", {}):
                cli = _temp_cli(sensor)
                if cli is None:
                    continue
                if cli["state"] == "ok":
                    d_otel = metrics_platstats["nvswitch_platform_environment_temp_crit"][sensor][-1]
                    d_cli = cli["crit"]
                    if not _plat_rounded_equal(d_otel, d_cli):
                        pytest.fail(
                            f"Data validation for nvswitch_platform_environment_temp_crit failed, "
                            f"otel: {d_otel}, cli: {d_cli}"
                        )

        with allure.step("Validate nvswitch_platform_environment_psu_state"):
            metric_name, series = _psu_metric_series("state")
            for sensor in series:
                cli = _psu_cli(sensor[-1])
                if cli is None:
                    continue
                d_otel = series[sensor][-1]
                d_cli = cli["state"]
                otel_state = state_map[d_otel]
                passed = otel_state == d_cli
                log_otel_cli_comparison(
                    metric_name,
                    otel_value=otel_state,
                    reference_value=d_cli,
                    attr_key=sensor,
                    passed=passed,
                )
                if not passed:
                    pytest.fail(
                        f"Data validation for {metric_name} failed, "
                        f"otel: {otel_state}, cli: {d_cli}"
                    )

        with allure.step("Validate nvswitch_platform_environment_psu_voltage"):
            metric_name, series = _psu_metric_series("voltage")
            logger.info("Running %s data validation", metric_name)
            for sensor in series:
                cli = _psu_cli(sensor[-1])
                if cli is None:
                    continue
                if cli["state"] == "ok":
                    d_otel = _plat_numeric(series[sensor][-1])
                    d_cli = _plat_numeric(cli["voltage"])
                    if self.get_percentage_deviation(round(d_cli), round(d_otel)) > 35:
                        pytest.fail(
                            f"Data validation for {metric_name} failed, "
                            f"otel: {d_otel}, cli: {d_cli}"
                        )

        with allure.step("Validate nvswitch_platform_environment_psu_power"):
            metric_name, series = _psu_metric_series("power")
            logger.info("Running %s data validation", metric_name)
            for sensor in series:
                cli = _psu_cli(sensor[-1])
                if cli is None:
                    continue
                if cli["state"] == "ok":
                    d_otel = _plat_numeric(series[sensor][-1])
                    d_cli = _plat_numeric(cli["power"])
                    if self.get_percentage_deviation(round(d_cli), round(d_otel)) > 20:
                        pytest.fail(
                            f"Data validation for {metric_name} failed, "
                            f"otel: {d_otel}, cli: {d_cli}"
                        )

        with allure.step("Validate nvswitch_platform_environment_psu_current"):
            metric_name, series = _psu_metric_series("current")
            logger.info("Running %s data validation", metric_name)
            for sensor in series:
                cli = _psu_cli(sensor[-1])
                if cli is None:
                    continue
                if cli["state"] == "ok":
                    d_otel = _plat_numeric(series[sensor][-1])
                    d_cli = _plat_numeric(cli["current"])
                    if self.get_percentage_deviation(round(d_cli), round(d_otel)) > 35:
                        pytest.fail(
                            f"Data validation for {metric_name} failed, "
                            f"otel: {d_otel}, cli: {d_cli}"
                        )

        with allure.step("Validate nvswitch_platform_environment_psu_capacity"):
            metric_name, series = _psu_metric_series("capacity")
            logger.info("Running %s data validation", metric_name)
            for sensor in series:
                cli = _psu_cli(sensor[-1])
                if cli is None:
                    continue
                if cli["state"] == "ok":
                    d_otel = _plat_numeric(series[sensor][-1])
                    d_cli = _plat_numeric(cli["capacity"])
                    if not _plat_rounded_equal(d_otel, d_cli):
                        pytest.fail(
                            f"Data validation for {metric_name} failed, "
                            f"otel: {d_otel}, cli: {d_cli}"
                        )

        if missing_cli:
            pytest.fail(
                "Platform stats: exported OTEL sensors have no matching CLI snapshot: " +
                ", ".join(sorted(missing_cli))
            )
