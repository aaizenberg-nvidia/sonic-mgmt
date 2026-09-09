"""NVOS OTEL telemetry — metric-name coverage on the default export VRF (insecure).

These are NVOS-native tests: they enable telemetry families and assert the exported
metric names match the on-switch ``metrics-classes.yaml`` (capability-gated).

:func:`test_otel_all_metrics_present` also implements test-plan item Section 5.1
``test_otel_basic_export_and_metric_inventory``. It is extended in place rather
than re-written under the Section 5 module because it already enabled every
family, collected a window and asserted the metric-name set strictly — the item's
remaining steps are payload validity, the agent-mapping contract, and the export
toggle.
"""

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import ApiType, TelemetryConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.telemetry.otel.agent_mappings import (
    expected_metrics_conform_to_agent_mappings,
)
from ngts.tests_nvos.system.telemetry.otel.constants import (
    OtelCollectorConst,
    OtelFunctionalTestConst,
)
from ngts.tests_nvos.system.telemetry.otel.helpers import (
    assert_metric_names_strict,
    assert_min_samples_in_window,
    assert_nvos_export_resumed,
    assert_nvos_export_stopped,
    assert_otlp_payload_valid,
    collect_metric_names_window,
    available_data_path_dest_types,
    collect_otlp_metrics_window,
    collection_window_for_samples,
    collector_receivers_for,
    enable_nvos_telemetry_families,
    expected_metric_names_from_dut,
    otlp_destination_id,
)
from ngts.tests_nvos.system.telemetry.otel.nvos_cli_helpers import (
    set_leaf,
    show_applied,
    unset_path,
    verify_leaf,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.otel,
]

_ENABLED = TelemetryConsts.State.ENABLED.value
_DISABLED = TelemetryConsts.State.DISABLED.value


@pytest.fixture(autouse=True)
def otel_coverage_nvue_default():
    """Default the API to NVUE here, restoring the previous value on the way out.

    Autouse function-scoped fixtures are set up before the requested ones, so the
    family enablement runs over NVUE while ``random_api`` (set up after this)
    still selects the API for the test body itself.
    """
    with TestToolkit.force_api(ApiType.NVUE):
        yield


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


def test_otel_all_metrics_present(
    engines, devices, otel_suite_single, is_ib_router, tmp_path, random_api
):
    """Section 5.1 - OTLP export works, the payload is valid, and the inventory matches.

    Enables every telemetry family against a single destination, collects a window
    long enough for two samples of the slowest class, and asserts the payload
    decodes, the metric-name set matches ``metrics-classes.yaml`` exactly, and every
    datapoint conforms to the ``agent-mappings.yaml`` type/attribute contract. Then
    toggles the export off and on, and finally unsets the export subtree.

    Takes the single-collector suite because the item's precondition is one
    destination: the two-collector suite additionally requires an ``ha`` player,
    so this test could not run on setups that have none, for no benefit here.

    The attribute check runs in both directions: no undeclared attribute, and no
    metric exporting fewer than its declared ``labels`` (the item's attribute-count
    requirement). Metric *type* is asserted as scalar-vs-histogram only — the shared
    validator deliberately treats ``gauge`` and ``sum`` as interchangeable, since the
    agent picks between them per metric.

    Both axes the plan randomizes are exercised: the transport (gRPC or http) and
    the destination id type, all naming the same running collector. The collector
    listens on IPv4 gRPC by default and is reconfigured for the run only when the
    draw needs more, so the suites that do not randomize keep their long-standing
    listener. The IPv6 address is read off the collector host's interface at run
    time, since the lab publishes no AAAA record for it, and IPv6 drops out of the
    pool on setups that do not route it between DUT and collector.
    """
    dut = engines.dut
    otlp = System().telemetry.export.otlp
    local_dir = str(tmp_path)
    collector_label = "primary (full export)"

    with allure.step("Select the OTLP transport and destination id type"):
        transport = RandomizationTool.select_random_value(
            list(OtelFunctionalTestConst.DATA_PATH_TRANSPORTS)
        ).get_returned_value()
        dest_type = RandomizationTool.select_random_value(
            list(available_data_path_dest_types(dut, otel_suite_single.primary))
        ).get_returned_value()

    with collector_receivers_for(
        otel_suite_single.primary, transport=transport, dest_type=dest_type
    ), otlp_destination_id(
        dut,
        otel_suite_single.primary_ip,
        dest_type,
        collector=otel_suite_single.primary,
    ) as destination_id:
        _assert_basic_export_and_metric_inventory(
            engines=engines,
            devices=devices,
            otel_suite_single=otel_suite_single,
            is_ib_router=is_ib_router,
            local_dir=local_dir,
            otlp=otlp,
            collector_label=collector_label,
            transport=transport,
            destination_id=destination_id,
        )


def _assert_basic_export_and_metric_inventory(
    *,
    engines,
    devices,
    otel_suite_single,
    is_ib_router,
    local_dir,
    otlp,
    collector_label,
    transport,
    destination_id,
):
    """Steps 1-8 of Section 5.1, against an already-resolved destination.

    Split out so the destination-id context manager (which may hold an
    ``/etc/hosts`` alias on the DUT) wraps the whole flow without indenting it.
    """
    dut = engines.dut

    with allure.step(
        f"Step 1 - enable every telemetry family and apply the {transport} "
        f"destination {destination_id}"
    ):
        enable_nvos_telemetry_families(
            dut,
            destination_id,
            TelemetryConsts.ALL_STATS_SUBTREES,
            collector_ips=(destination_id,),
            use_stats_group=False,
            transport=transport,
            clear_other_transports=True,
        )
        verify_leaf(show_applied(otlp, dut), TelemetryConsts.STATE, _ENABLED)

    max_sample_interval_sec = max(
        OtelCollectorConst.INTERFACE_STATS_SAMPLE_INTERVAL_SEC,
        OtelCollectorConst.PEER_PORT_STATS_SAMPLE_INTERVAL_SEC,
        OtelCollectorConst.IB_ROUTER_STATS_SAMPLE_INTERVAL_SEC,
        OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC,
    )
    two_sample_window_sec = collection_window_for_samples(
        max_sample_interval_sec, OtelFunctionalTestConst.MIN_SAMPLES_IN_WINDOW
    )

    with allure.step(
        f"Steps 2 and 5 - collect and decode OTLP batches over {two_sample_window_sec}s "
        f"({OtelFunctionalTestConst.MIN_SAMPLES_IN_WINDOW} samples of the slowest class)"
    ):
        actual_names, docs, timestamps = collect_otlp_metrics_window(
            otel_suite_single.primary,
            local_dir,
            label="primary-full",
            max_sample_interval_sec=max_sample_interval_sec,
            wait_sec=two_sample_window_sec,
        )

    with allure.step("Step 4 - the payload is valid OTLP with non-empty resourceMetrics"):
        assert_otlp_payload_valid(docs, collector_label=collector_label)
        assert_min_samples_in_window(
            timestamps,
            metric_name_prefix=OtelFunctionalTestConst.SLOWEST_FAMILY_METRIC_PREFIX,
            min_samples=OtelFunctionalTestConst.MIN_SAMPLES_IN_WINDOW,
            collector_label=collector_label,
        )

    with allure.step("Step 6 - no metric is missing and none is unexpected"):
        expected = expected_metric_names_from_dut(
            engines,
            local_dir,
            OtelCollectorConst.METRICS_CLASSES_FULL_EXPORT_GROUP_PREFIXES,
            file_name="metrics-classes-full.yaml",
            is_ib_router=is_ib_router,
            devices=devices,
        )
        assert expected, (
            f"No expected metric names after gating "
            f"(group prefixes={OtelCollectorConst.METRICS_CLASSES_FULL_EXPORT_GROUP_PREFIXES})."
        )
        assert_metric_names_strict(expected, actual_names, collector_label=collector_label)

    with allure.step(
        "Step 6 - every datapoint carries the declared type and its full declared "
        "attribute set (agent-mappings.yaml)"
    ):
        expected_metrics_conform_to_agent_mappings(
            engines,
            local_dir,
            docs,
            collector_label=collector_label,
            require_declared_attributes=True,
        )

    stop_window_sec = OtelCollectorConst.collection_window_sec(max_sample_interval_sec)

    with allure.step("Step 7 - disable the OTLP export and verify batches stop"):
        set_leaf(otlp, dut, TelemetryConsts.STATE, _DISABLED, apply=True)
        verify_leaf(show_applied(otlp, dut), TelemetryConsts.STATE, _DISABLED)
        assert_nvos_export_stopped(
            otel_suite_single.primary,
            local_dir,
            label="primary-export-disabled",
            wait_sec=stop_window_sec,
        )

    with allure.step("Step 7 - re-enable the OTLP export and verify batches resume"):
        set_leaf(otlp, dut, TelemetryConsts.STATE, _ENABLED, apply=True)
        verify_leaf(show_applied(otlp, dut), TelemetryConsts.STATE, _ENABLED)
        assert_nvos_export_resumed(
            otel_suite_single.primary,
            local_dir,
            label="primary-export-resumed",
            wait_sec=stop_window_sec,
        )

    with allure.step("Step 8 - unset the telemetry export subtree and apply"):
        unset_path(System().telemetry.export, dut, apply=True)
        verify_leaf(
            show_applied(otlp, dut),
            TelemetryConsts.STATE,
            TelemetryConsts.Defaults.OTLP_STATE,
        )
