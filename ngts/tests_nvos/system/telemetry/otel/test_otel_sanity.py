"""OTEL telemetry — sanity.

One short, deterministic test that proves the OTLP data path end to end: the DUT
is configured, the collector is up, and real metrics land on it.

It deliberately asserts nothing about *which* metrics arrive beyond the family
the bring-up enables. The strict checks — the metric-name set against
``metrics-classes.yaml``, the ``agent-mappings.yaml`` type/attribute contract,
the export toggle — live in :mod:`test_otel_metric_coverage` and
:mod:`test_otel_nvos_cli_and_functional`, and are what make those tests
expensive and build-sensitive. Keep this one cheap: a red result here should
mean OTEL export is broken, not that the catalog moved.

Runs on a single collector (no ``ha`` player needed) over insecure gRPC.
"""

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import ApiType, TelemetryConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.telemetry.otel.constants import (
    OtelCollectorConst,
    OtelSanityTestConst,
)
from ngts.tests_nvos.system.telemetry.otel.helpers import (
    assert_otlp_payload_valid,
    collect_otlp_metrics_window,
)
from ngts.tests_nvos.system.telemetry.otel.nvos_cli_helpers import (
    show_applied,
    verify_leaf,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.otel,
]

_ENABLED = TelemetryConsts.State.ENABLED.value


@pytest.mark.nvos_build
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_otel_sanity_metrics_reach_collector(engines, otel_suite_single, test_api, tmp_path):
    """The configured OTLP export delivers valid interface-stats metrics to the collector.

    The ``otel_suite_single`` bring-up already pre-cleans the DUT, starts the
    collector, applies one insecure gRPC destination with interface-stats enabled,
    and fails if the first artifact stays empty. This body re-reads the applied
    configuration, then collects its own window so the assertion is on a fresh
    artifact rather than the fixture's.
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    system = System()

    with allure.step(f"Applied configuration enables {OtelSanityTestConst.BASELINE_FAMILY}"):
        # Only the family export is asserted here: the master-enable leaf differs
        # per platform (NVOS ``export otlp state`` vs Cumulus ``telemetry state``).
        verify_leaf(
            show_applied(system.telemetry.interface_stats.export, dut),
            TelemetryConsts.STATE,
            _ENABLED,
        )

    with allure.step(f"Collect a {OtelSanityTestConst.COLLECTION_WINDOW_SEC}s OTLP window"):
        metric_names, docs, _timestamps = collect_otlp_metrics_window(
            otel_suite_single.primary,
            str(tmp_path),
            label=OtelSanityTestConst.COLLECTION_LABEL,
            max_sample_interval_sec=OtelCollectorConst.INTERFACE_STATS_SAMPLE_INTERVAL_SEC,
            wait_sec=OtelSanityTestConst.COLLECTION_WINDOW_SEC,
        )

    with allure.step("The collected artifact is valid OTLP with metric data"):
        assert_otlp_payload_valid(
            docs, collector_label=OtelSanityTestConst.COLLECTOR_LABEL
        )

    with allure.step(f"Exported metrics include the {OtelSanityTestConst.BASELINE_FAMILY} family"):
        family_names = sorted(
            name for name in metric_names
            if name.startswith(OtelSanityTestConst.BASELINE_METRIC_PREFIX)
        )
        assert family_names, (
            f"{OtelSanityTestConst.COLLECTOR_LABEL} received OTLP metrics but none from "
            f"{OtelSanityTestConst.BASELINE_FAMILY} (expected names starting with "
            f"{OtelSanityTestConst.BASELINE_METRIC_PREFIX!r}); "
            f"got {sorted(metric_names)}"
        )
