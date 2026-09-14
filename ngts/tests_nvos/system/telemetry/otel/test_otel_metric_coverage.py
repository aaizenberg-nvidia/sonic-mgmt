"""NVOS OTEL telemetry — metric-name coverage on the default export VRF (insecure).

These are NVOS-native tests: they enable telemetry families and assert the exported
metric names match the on-switch ``metrics-classes.yaml`` (capability-gated).
"""

import pytest

from ngts.nvos_constants.constants_nvos import TelemetryConsts
from ngts.tests_nvos.system.telemetry.otel.constants import OtelCollectorConst
from ngts.tests_nvos.system.telemetry.otel.helpers import (
    assert_metric_names_strict,
    collect_metric_names_window,
    enable_nvos_telemetry_families,
    expected_metric_names_from_dut,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.otel,
]


@pytest.mark.multiple_otlp_collectors
def test_otel_platform_stats_metrics_present(engines, devices, otel_suite_dual, is_ib_router, tmp_path):
    """Enable platform-stats only and assert expected metric names appear on both collectors."""
    enable_nvos_telemetry_families(
        engines.dut,
        otel_suite_dual.primary_ip,
        [TelemetryConsts.PLATFORM_STATS],
        collector_ips=otel_suite_dual.collector_ips,
        use_stats_group=False,
    )

    expected = expected_metric_names_from_dut(
        engines,
        str(tmp_path),
        OtelCollectorConst.METRICS_CLASSES_PLATFORM_STATS_GROUP_PREFIXES,
        file_name="metrics-classes-platform.yaml",
        is_ib_router=is_ib_router,
        devices=devices,
    )
    assert expected, (
        f"No expected platform-stats metric names after gating "
        f"(group prefixes={OtelCollectorConst.METRICS_CLASSES_PLATFORM_STATS_GROUP_PREFIXES})."
    )

    primary_names = collect_metric_names_window(
        otel_suite_dual.primary,
        str(tmp_path),
        label="primary-platform",
        max_sample_interval_sec=OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC,
    )
    secondary_names = collect_metric_names_window(
        otel_suite_dual.secondary,
        str(tmp_path),
        label="secondary-platform",
        max_sample_interval_sec=OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC,
    )

    assert_metric_names_strict(expected, primary_names, collector_label="primary (platform-stats)")
    assert_metric_names_strict(expected, secondary_names, collector_label="secondary (platform-stats)")


def test_otel_all_metrics_present(engines, devices, otel_suite, is_ib_router, tmp_path):
    """Enable every telemetry family and assert expected metric names appear on the primary collector."""
    enable_nvos_telemetry_families(
        engines.dut,
        otel_suite.primary_ip,
        TelemetryConsts.ALL_STATS_SUBTREES,
        collector_ips=otel_suite.collector_ips,
        use_stats_group=False,
    )

    expected = expected_metric_names_from_dut(
        engines,
        str(tmp_path),
        OtelCollectorConst.METRICS_CLASSES_FULL_EXPORT_GROUP_PREFIXES,
        file_name="metrics-classes-full.yaml",
        is_ib_router=is_ib_router,
        devices=devices,
    )
    assert expected, (
        f"No expected metric names after gating "
        f"(group prefixes={OtelCollectorConst.METRICS_CLASSES_FULL_EXPORT_GROUP_PREFIXES})."
    )

    max_sample_interval_sec = max(
        OtelCollectorConst.INTERFACE_STATS_SAMPLE_INTERVAL_SEC,
        OtelCollectorConst.PEER_PORT_STATS_SAMPLE_INTERVAL_SEC,
        OtelCollectorConst.IB_ROUTER_STATS_SAMPLE_INTERVAL_SEC,
        OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC,
    )
    actual_names = collect_metric_names_window(
        otel_suite.primary,
        str(tmp_path),
        label="primary-full",
        max_sample_interval_sec=max_sample_interval_sec,
    )

    assert_metric_names_strict(expected, actual_names, collector_label="primary (full export)")
