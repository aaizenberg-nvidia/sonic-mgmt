"""Split-pipeline OTLP stats-group configuration (SSIM ``OtelSplitPipelineMgmtVrf*``)."""

from __future__ import annotations

import logging
import time
from typing import List, Tuple
import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import ConfState, TelemetryConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
    ensure_asic_monitor_running,
    get_dut_hostname,
    restart_asic_monitor,
    restart_nvtelemetry,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import (
    _bind_stats_group_on_otlp_destinations,
    _enable_family_classes,
    _enable_histogram_root,
    _enable_simple_telemetry_export,
    _enable_stats_group_simple_export,
    _enable_stats_group_software_stats_systemd,
    _stats_group_subtree,
    apply_staged_candidate,
    enable_cumulus_lab_interface_histogram,
)
from ngts.tests_nvos.system.telemetry.otel.helpers import (
    _set_stats_sample_interval,
    _stats_node,
    _telemetry_stats_parent,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.sample_interval import (
    verify_metrics_sample_interval_server1,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.telemetry_health import (
    assert_otlp_session_established,
    cleanup_otlp_export_session,
    verify_otelcol_server_active,
    verify_otlp_client_active,
)
from ngts.tests_nvos.system.telemetry.otel.otel_collector import OtelCollector

logger = logging.getLogger(__name__)

_ENABLED = TelemetryConsts.State.ENABLED.value
_STATS_GROUP = CumulusOtelConst.SPLIT_PIPELINE_STATS_GROUP_ID


def _nv_apply(dut) -> None:
    """Apply the candidate staged by the component model, then let services settle.

    Histogram and stats-group sets go through ``BaseComponent.set``, which follows
    ``TestToolkit.tested_api``. Applying with raw ``nv config apply`` would commit
    the NVUE candidate and leave an OpenAPI revision unapplied.
    """
    apply_staged_candidate(dut)
    time.sleep(10)


_SPLIT_PIPELINE_PLATFORM_CLASSES = (
    TelemetryConsts.PLATFORM_CLASS_CPU,
    TelemetryConsts.PLATFORM_CLASS_DISK,
    TelemetryConsts.PLATFORM_CLASS_ENVIRONMENT_SENSOR,
    CumulusOtelConst.STATS_GROUP_PLATFORM_CLASS_FILE_SYSTEM,
    TelemetryConsts.PLATFORM_CLASS_MEMORY,
)
_SPLIT_PIPELINE_PLATFORM_CLASS_INTERVALS = {
    cls: 70 for cls in _SPLIT_PIPELINE_PLATFORM_CLASSES
}

_NVOS_SPLIT_PIPELINE_PLATFORM_CLASSES = (
    TelemetryConsts.PLATFORM_CLASS_CPU,
    TelemetryConsts.PLATFORM_CLASS_DISK,
    TelemetryConsts.PLATFORM_CLASS_MEMORY,
    TelemetryConsts.PLATFORM_CLASS_ENVIRONMENT_SENSOR,
)
_NVOS_SPLIT_PIPELINE_PLATFORM_CLASS_INTERVALS = {
    cls: 70 for cls in _NVOS_SPLIT_PIPELINE_PLATFORM_CLASSES
}


def _try_stats_group_export_enabled(system: System, dut, stats_group_id: str, path: str) -> None:
    """Best-effort enable export under a stats-group subtree (optional Cumulus knobs)."""
    result = _stats_group_subtree(system, stats_group_id, path).set(
        TelemetryConsts.STATE, _ENABLED, dut_engine=dut
    )
    if result.result:
        result.verify_result()
    else:
        result.ignore_result()
        logger.info(
            "Skipping stats-group %s export enable under %s (NVUE rejected set)",
            stats_group_id,
            path,
        )


def apply_split_pipeline_test01_stats_group(
    dut,
    primary_collector_ip: str,
    *,
    stats_group_id: str = _STATS_GROUP,
    include_router_lldp: bool = False,
) -> None:
    """Apply SSIM split test01 ``test_01`` stats-group + root intervals on the DUT."""
    system = System()
    sg = stats_group_id
    with allure.step(f"Apply split-pipeline stats-group {sg} (mgmt VRF)"):
        _telemetry_stats_parent(system, sg)
        _set_stats_sample_interval(
            system, dut, TelemetryConsts.INTERFACE_STATS, 10
        )
        _set_stats_sample_interval(
            system, dut, TelemetryConsts.PLATFORM_STATS, 60
        )
        _enable_simple_telemetry_export(system, dut, "control-plane-stats", 10)

        _stats_group_subtree(system, sg, "/histogram/export").set(
            TelemetryConsts.STATE, _ENABLED, dut_engine=dut
        ).verify_result()
        _enable_stats_group_simple_export(
            system, dut, sg, "control-plane-stats", 15
        )
        _enable_stats_group_simple_export(system, dut, sg, "buffer-stats", 15)
        _enable_stats_group_simple_export(
            system, dut, sg, TelemetryConsts.INTERFACE_STATS, 15
        )
        _stats_node(
            system, TelemetryConsts.PLATFORM_STATS, stats_group_id=sg
        ).export.set(TelemetryConsts.STATE, _ENABLED, dut_engine=dut).verify_result()
        _enable_family_classes(
            system,
            dut,
            TelemetryConsts.PLATFORM_STATS,
            stats_group_id=sg,
            platform_classes=_SPLIT_PIPELINE_PLATFORM_CLASSES,
            platform_class_intervals=_SPLIT_PIPELINE_PLATFORM_CLASS_INTERVALS,
        )
        _bind_stats_group_on_otlp_destinations(
            system, dut, [primary_collector_ip], sg
        )

        if include_router_lldp:
            _try_stats_group_export_enabled(
                system, dut, sg, "/router/export"
            )
            _enable_stats_group_simple_export(system, dut, sg, "lldp", 30)
            _enable_family_classes(
                system,
                dut,
                TelemetryConsts.PLATFORM_STATS,
                stats_group_id=sg,
                platform_classes=(TelemetryConsts.PLATFORM_CLASS_TRANSCEIVER_INFO,),
                platform_class_intervals={
                    TelemetryConsts.PLATFORM_CLASS_TRANSCEIVER_INFO: 60
                },
            )
            _enable_stats_group_software_stats_systemd(system, dut, sg, 70)

        system.telemetry.export.otlp.set(
            TelemetryConsts.STATE,
            _ENABLED,
            apply=False,
            dut_engine=dut,
        )
        _nv_apply(dut)


def apply_split_pipeline_test01_stats_group_nvos(
    dut,
    primary_collector_ip: str,
    *,
    export_vrf: str,
    stats_group_id: str = _STATS_GROUP,
) -> None:
    """NVOS split-pipeline stats-group ``test_01`` (interface + platform only).

    NVOS has no histogram / buffer-stats / control-plane-stats / lldp / software-stats
    telemetry subtrees, so this stages only the split-pipeline families NVOS supports:
    root interface/platform intervals that differ from the ``test_01`` stats-group
    bound to the primary collector. Intervals here must match the ``expected_gaps``
    the test passes to :func:`run_split_pipeline_test01_verification`.
    """
    system = System()
    sg = stats_group_id
    with allure.step(
        f"Apply NVOS split-pipeline stats-group {sg} (vrf={export_vrf})"
    ):
        _telemetry_stats_parent(system, sg)

        _stats_node(system, TelemetryConsts.INTERFACE_STATS).export.set(
            TelemetryConsts.STATE, _ENABLED, dut_engine=dut
        ).verify_result()
        _set_stats_sample_interval(
            system, dut, TelemetryConsts.INTERFACE_STATS, 10
        )
        _stats_node(system, TelemetryConsts.PLATFORM_STATS).export.set(
            TelemetryConsts.STATE, _ENABLED, dut_engine=dut
        ).verify_result()
        _set_stats_sample_interval(
            system, dut, TelemetryConsts.PLATFORM_STATS, 60
        )

        _stats_node(
            system, TelemetryConsts.INTERFACE_STATS, stats_group_id=sg
        ).export.set(TelemetryConsts.STATE, _ENABLED, dut_engine=dut).verify_result()
        _set_stats_sample_interval(
            system, dut, TelemetryConsts.INTERFACE_STATS, 15, stats_group_id=sg
        )
        _stats_node(
            system, TelemetryConsts.PLATFORM_STATS, stats_group_id=sg
        ).export.set(TelemetryConsts.STATE, _ENABLED, dut_engine=dut).verify_result()
        _enable_family_classes(
            system,
            dut,
            TelemetryConsts.PLATFORM_STATS,
            stats_group_id=sg,
            platform_classes=_NVOS_SPLIT_PIPELINE_PLATFORM_CLASSES,
            platform_class_intervals=_NVOS_SPLIT_PIPELINE_PLATFORM_CLASS_INTERVALS,
        )
        _bind_stats_group_on_otlp_destinations(
            system, dut, [primary_collector_ip], sg
        )

        system.telemetry.export.otlp.set(
            TelemetryConsts.STATE,
            _ENABLED,
            apply=False,
            dut_engine=dut,
        )
        _nv_apply(dut)


def validate_split_pipeline_stats_group_field_unsets(
    dut,
    *,
    stats_group_id: str = _STATS_GROUP,
) -> None:
    """Port SSIM insecure split-pipeline test02 unset/show coverage.

    Each field is unset, applied, and immediately read from the applied datastore.
    The show is intentionally retained even where NVUE renders a schema default after
    unset; the SSIM assertion is that every unset/show pair completes successfully.
    """
    from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import (
        get_cumulus_version_tuple,
    )

    pairs: List[Tuple[str, str]] = [
        ("buffer-stats/export", "state"),
        ("buffer-stats", "sample-interval"),
        ("control-plane-stats/export", "state"),
        ("control-plane-stats", "sample-interval"),
        ("histogram/export", "state"),
        ("interface-stats/export", "state"),
        ("interface-stats", "sample-interval"),
        ("platform-stats/export", "state"),
    ]
    # get_cumulus_version_tuple is (major, minor) only; (5, 17) means the 5.17 family and later.
    if get_cumulus_version_tuple(dut) >= (5, 17):
        pairs.extend(
            [
                ("interface-stats/class/debounce", "state"),
                ("interface-stats/class/debounce", "sample-interval"),
            ]
        )
    for platform_class in (
        "cpu",
        "disk",
        "memory",
        "file-system",
        "environment-sensor",
    ):
        pairs.extend(
            [
                (f"platform-stats/class/{platform_class}", "state"),
                (f"platform-stats/class/{platform_class}", "sample-interval"),
            ]
        )

    failures = []
    system = System()
    for path, key in pairs:
        label = f"stats-group {stats_group_id} {path} {key}"
        try:
            node = BaseComponent(
                system.telemetry,
                path=f"/stats-group/{stats_group_id}/{path}",
            )
            node.unset(
                key, apply=True, ask_for_confirmation=True, dut_engine=dut
            ).verify_result()
            output = node.parse_show(rev=ConfState.APPLIED, dut_engine=dut)
            logger.info("%s after unset: %s", label, output)
        except Exception as exc:  # noqa: BLE001 - aggregate all CLI coverage failures
            logger.exception("nv unset/show %s failed", label)
            failures.append(f"{label}: {exc}")

    assert not failures, (
        f"split-pipeline unset/show validations failed ({len(failures)}/{len(pairs)}):\n" +
        "\n".join(failures)
    )


def prepare_split_pipeline_insecure_pre_run(
    dut,
    *,
    vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT,
) -> None:
    hostname = get_dut_hostname(dut)
    with allure.step("Split-pipeline insecure pre-run (histogram baseline + service restart)"):
        system = System()
        _enable_histogram_root(system, dut)
        enable_cumulus_lab_interface_histogram(dut, hostname)
        _nv_apply(dut)
        restart_nvtelemetry(dut, vrf)
        restart_asic_monitor(dut, vrf)
        ensure_asic_monitor_running(dut, vrf)


def prepare_split_pipeline_secured_pre_run(
    dut,
    collector: OtelCollector,
    vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT,
) -> None:
    """SSIM ``Test_Otel_Split_Mgmt_Vrf_Secured.pre_run_hook`` on mlx lab (no routing)."""
    with allure.step("Split-pipeline secured pre-run (restart OTLP session)"):
        cleanup_otlp_export_session(dut, collector, vrf=vrf)
        assert_otlp_session_established(collector)
        verify_otlp_client_active(dut, vrf)
        verify_otelcol_server_active(collector)


def run_split_pipeline_test01_verification(
    dut,
    collector: OtelCollector,
    cur_dir: str,
    *,
    vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT,
    expected_gaps: dict | None = None,
    prepare_session: bool = True,
    telemetry_unit: str | None = None,
    family_map: list | None = None,
) -> None:
    """Shared test01 body: sample-interval validation for stats-group ``test_01``.

    ``telemetry_unit`` overrides the systemd unit used by collection (NVOS passes
    ``nv-telemetry.service``); defaults to the Cumulus per-VRF ``nv-telemetry@<vrf>``.
    ``family_map`` overrides the metric-prefix -> gap-key pairs (NVOS metric names).
    """
    hostname = get_dut_hostname(dut)
    expected = expected_gaps or CumulusOtelConst.SPLIT_PIPELINE_TEST01_EXPECTED_GAPS
    verify_metrics_sample_interval_server1(
        dut,
        collector,
        hostname,
        cur_dir,
        vrf,
        expected,
        prepare_session=prepare_session,
        telemetry_unit=telemetry_unit,
        family_map=family_map,
    )


def apply_split_pipeline_mgmt_vrf_secured_base(
    dut,
    collector_ip: str,
    hostname: str,
) -> None:
    enable_cumulus_lab_interface_histogram(dut, hostname)
    apply_split_pipeline_test01_stats_group(
        dut, collector_ip, include_router_lldp=False
    )


def apply_split_pipeline_mgmt_vrf_insecure_base(
    dut,
    collector_ip: str,
    hostname: str,
) -> None:
    enable_cumulus_lab_interface_histogram(dut, hostname)
    vrf = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT
    restart_nvtelemetry(dut, vrf)
    restart_asic_monitor(dut, vrf)
