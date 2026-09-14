"""Dual-platform (NVOS + Cumulus) flows for the ported Cumulus OTEL tests.

These helpers let a single test body run on both platforms: the test calls one
``run_*`` flow and all platform divergence lives here, driven by
``suite.is_nvos`` / ``suite.export_vrf`` / ``suite.telemetry_unit`` from
:class:`~ngts.tests_nvos.system.telemetry.otel.helpers.OtelSuiteContext`.

Platform differences handled here:

* Collection: Cumulus uses the per-VRF stop/copy/restart workflow and validates
  against the maintained catalog; NVOS collects a window and validates strictly
  against the on-switch ``metrics-classes.yaml`` plus the UMF ``agent-mappings.yaml``
  contract (see :mod:`agent_mappings`).
* Export VRF: Cumulus uses ``mgmt``; NVOS has no ``mgmt`` VRF and uses ``default``.
* Telemetry unit: Cumulus ``nv-telemetry@<vrf>``; NVOS ``nv-telemetry.service``.
* Telemetry master enable: Cumulus ``telemetry state``; NVOS ``export otlp state``
  (see :func:`~ngts.tests_nvos.system.telemetry.otel.helpers.stage_telemetry_master_state`
  and :func:`~ngts.tests_nvos.system.telemetry.otel.helpers.apply_telemetry_configuration`).
* transceiver-info: both platforms validate root + ``stats-group sg_01`` show output.
"""

import logging
from typing import List

import ngts.tools.test_utils.allure_utils as allure

from ngts.nvos_constants.constants_nvos import TelemetryConsts
from ngts.tests_nvos.system.telemetry.otel.agent_mappings import (
    expected_metrics_conform_to_agent_mappings,
)
from ngts.tests_nvos.system.telemetry.otel.constants import OtelCollectorConst
from ngts.tests_nvos.system.telemetry.otel.helpers import (
    OtelSuiteContext,
    TelemetryEnablement,
    assert_nvos_metric_names_for_enablement,
    build_nvos_enablement,
    collect_otlp_metrics_window,
    enable_nvos_telemetry_families,
    log_otel_metric_inventory,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus import cache as telemetryCache
from ngts.tests_nvos.system.telemetry.otel.cumulus.catalog import (
    flatten_supported_metrics,
    supported_metrics_for_mgmt_vrf_secured_validation,
    supported_metrics_for_test01_validation,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
    collect_and_cache_mgmt_vrf_otel_session,
    collect_and_cache_secured_otel_session,
    cumulus_otel_artifact_path,
    ensure_control_plane_stats_otel_cli_cache,
    ensure_platform_stats_otel_cli_cache,
    get_dut_hostname,
    log_collector_export_metrics,
    validate_cached_control_plane_stats_against_cli,
    validate_cached_platform_stats_against_cli,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import (
    apply_simple_export_show_telemetry_config,
    apply_software_stats_show_telemetry_config,
    apply_transceiver_info_show_telemetry_config,
    dut_root_on_nvme_storage,
    secured_exclude_metrics,
    show_platform_stats_class_transceiver_info,
    show_system_telemetry_simple_export,
    test01_expected_metric_names_from_catalog,
    validate_simple_export_applied,
    validate_software_stats_applied,
    validate_test01_collected_metrics,
    validate_transceiver_info_applied,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.tls import (
    assert_otlp_grpc_certificate_applied,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.validations import OtelDataValidations

logger = logging.getLogger(__name__)


def _max_family_sample_interval_sec() -> int:
    return max(
        OtelCollectorConst.INTERFACE_STATS_SAMPLE_INTERVAL_SEC,
        OtelCollectorConst.PEER_PORT_STATS_SAMPLE_INTERVAL_SEC,
        OtelCollectorConst.IB_ROUTER_STATS_SAMPLE_INTERVAL_SEC,
        OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC,
    )


# --- NVOS strict collection (metrics-classes.yaml + agent-mappings.yaml) ------


def _nvos_collect_strict_and_mappings(
    engines,
    devices,
    suite: OtelSuiteContext,
    is_ib_router: bool,
    cur_dir: str,
    *,
    label: str,
    enablement: TelemetryEnablement,
    destination_ip: str,
) -> None:
    """Collect a window and validate names (strict) + agent-mappings contract (NVOS).

    The expected set is derived from the test-declared ``enablement`` for
    ``destination_ip`` (class-aware, stats-group override aware), not the full
    family catalog, so a stats-group that enables a class subset no longer reports
    every un-enabled class as missing.
    """
    names, docs, _timestamps = collect_otlp_metrics_window(
        suite.primary,
        cur_dir,
        label=label,
        max_sample_interval_sec=_max_family_sample_interval_sec(),
    )
    assert_nvos_metric_names_for_enablement(
        engines,
        devices,
        cur_dir,
        actual_metric_names=names,
        enablement=enablement,
        destination_ip=destination_ip,
        is_ib_router=is_ib_router,
        collector_label=label,
    )
    with allure.step("Validate OTLP datapoints conform to agent-mappings (NVOS)"):
        expected_metrics_conform_to_agent_mappings(
            engines, cur_dir, docs, collector_label=label
        )


# --- test01 insecure collection (mgmt VRF) ------------------------------------


def _cumulus_insecure_collection(engines, suite: OtelSuiteContext, cur_dir: str) -> None:
    telemetryCache.clear_data()
    collect_and_cache_mgmt_vrf_otel_session(
        engines.dut,
        suite.primary,
        cur_dir,
        collector_ips=suite.collector_ips,
    )
    otel_data_val = OtelDataValidations()
    hostname = get_dut_hostname(engines.dut)

    metrics_timestamps = telemetryCache.get_data("otel")["metrics_timestamps"]
    log_collector_export_metrics(
        cumulus_otel_artifact_path(cur_dir),
        collector_label="primary (test01 stats-group export)",
        parsed_metrics_timestamps=metrics_timestamps,
    )

    exclude_metrics = list(CumulusOtelConst.TEST01_EXCLUDE_METRICS)
    if dut_root_on_nvme_storage(engines.dut):
        exclude_metrics.extend(CumulusOtelConst.TEST01_EXCLUDE_METRICS_NVME)

    total_metrics = supported_metrics_for_test01_validation(engines.dut)
    assert total_metrics, "supported_metrics catalog is empty for this DUT"

    catalog_flat = flatten_supported_metrics(total_metrics, skip_buckets=())
    expected_flat = test01_expected_metric_names_from_catalog(
        total_metrics, exclude_metrics=exclude_metrics
    )
    log_otel_metric_inventory(
        set(metrics_timestamps.keys()),
        collector_label="primary (test01 stats-group export)",
        group_name_prefixes=CumulusOtelConst.TEST01_METRICS_GROUP_PREFIXES,
        expected=expected_flat,
        extra_ignore=set(exclude_metrics),
    )

    with allure.step("Validate collected metrics (test01)"):
        validate_test01_collected_metrics(
            metrics_timestamps,
            catalog_flat=catalog_flat,
            exclude_metrics=exclude_metrics,
            group_prefixes=CumulusOtelConst.TEST01_METRICS_GROUP_PREFIXES,
        )

    with allure.step("Validate collected metrics attributes count (test01)"):
        otel_data_val.validate_collected_metrics_attributes_count(
            metrics_timestamps,
            hostname,
            total_metrics=total_metrics,
            exclude_metrics=exclude_metrics,
        )


def run_insecure_collection_and_validate(
    engines, devices, suite: OtelSuiteContext, is_ib_router: bool, cur_dir: str
) -> None:
    """Ported test01 (insecure mgmt VRF) collection + validation for both platforms."""
    if suite.is_nvos:
        enablement = enable_nvos_telemetry_families(
            engines.dut,
            suite.primary_ip,
            TelemetryConsts.ALL_STATS_SUBTREES,
            export_vrf=suite.export_vrf,
            collector_ips=suite.collector_ips,
        )
        _nvos_collect_strict_and_mappings(
            engines, devices, suite, is_ib_router, cur_dir,
            label="primary-insecure-full",
            enablement=enablement,
            destination_ip=suite.primary_ip,
        )
    else:
        _cumulus_insecure_collection(engines, suite, cur_dir)


# --- test01 secured collection (TLS) ------------------------------------------


def _cumulus_secured_collection(engines, suite: OtelSuiteContext, cur_dir: str) -> None:
    telemetryCache.clear_data()
    vrf = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT

    otel_payload = collect_and_cache_secured_otel_session(
        engines.dut,
        suite.primary,
        cur_dir,
        vrf=vrf,
        wait_sec=CumulusOtelConst.SECURED_COLLECTION_WAIT_MGMT_VRF_SEC,
        cleanup_session=True,
    )
    assert_otlp_grpc_certificate_applied(
        engines.dut,
        destination_id=suite.primary_ip,
    )

    metrics_timestamps = otel_payload["metrics_timestamps"]
    hostname = get_dut_hostname(engines.dut)
    exclude_metrics = secured_exclude_metrics(engines.dut, no_routing=True)
    total_metrics = supported_metrics_for_mgmt_vrf_secured_validation(engines.dut)

    with allure.step("Validate collected metrics (mgmt VRF secured)"):
        OtelDataValidations().validate_collected_metrics(
            metrics_timestamps,
            hostname,
            total_metrics=total_metrics,
            exclude_metrics=exclude_metrics,
        )


def run_secured_collection_and_validate(
    engines, devices, suite: OtelSuiteContext, is_ib_router: bool, cur_dir: str
) -> None:
    """Ported secured test01 (TLS) collection + validation for both platforms."""
    if suite.is_nvos:
        assert_otlp_grpc_certificate_applied(
            engines.dut, destination_id=suite.primary_ip
        )
        # The secured suite fixture enabled root families only (no stats-group),
        # so declare the matching root-only enablement for the strict expected set.
        enablement = build_nvos_enablement(TelemetryConsts.ALL_STATS_SUBTREES)
        _nvos_collect_strict_and_mappings(
            engines, devices, suite, is_ib_router, cur_dir,
            label="primary-secured-full",
            enablement=enablement,
            destination_ip=suite.primary_ip,
        )
    else:
        _cumulus_secured_collection(engines, suite, cur_dir)


# --- test05 platform-stats vs CLI ---------------------------------------------


def run_platform_stats_validation(
    engines, devices, suite: OtelSuiteContext, cur_dir: str
) -> None:
    """Ported test05 platform environment OTEL-vs-CLI validation for both platforms."""
    collector_ips = suite.collector_ips
    if suite.is_nvos:
        enable_nvos_telemetry_families(
            engines.dut,
            suite.primary_ip,
            [TelemetryConsts.PLATFORM_STATS],
            export_vrf=suite.export_vrf,
            collector_ips=collector_ips,
        )

    ensure_platform_stats_otel_cli_cache(
        engines,
        suite,
        cur_dir,
        collector_ips=collector_ips,
        vrf=suite.export_vrf,
        apply_telemetry_config=not suite.is_nvos,
        telemetry_unit=suite.telemetry_unit,
    )
    validate_cached_platform_stats_against_cli(devices)


# --- transceiver-info platform-stats class ------------------------------------


def run_transceiver_info_validation(
    engines, devices, suite: OtelSuiteContext
) -> None:
    """Ported transceiver-info platform-stats class validation for both platforms."""
    apply_transceiver_info_show_telemetry_config(
        engines.dut,
        collector_ips=suite.collector_ips,
        is_nvos=suite.is_nvos,
    )
    root_out = show_platform_stats_class_transceiver_info(engines.dut)
    with allure.step("Validate root transceiver-info (applied)"):
        validate_transceiver_info_applied(
            root_out,
            context="nv show system telemetry platform-stats class transceiver-info",
        )

    sg_out = show_platform_stats_class_transceiver_info(
        engines.dut,
        stats_group_id=CumulusOtelConst.TEST01_STATS_GROUP_ID,
    )
    with allure.step(
        f"Validate stats-group {CumulusOtelConst.TEST01_STATS_GROUP_ID} "
        "transceiver-info (applied)"
    ):
        validate_transceiver_info_applied(
            sg_out,
            context=(
                f"nv show system telemetry stats-group "
                f"{CumulusOtelConst.TEST01_STATS_GROUP_ID} "
                "platform-stats class transceiver-info"
            ),
        )


def run_software_stats_validation(
    engines, devices, suite: OtelSuiteContext
) -> None:
    """Ported ``software-stats`` systemd show validation for both platforms.

    Applies the ``custom`` systemd unit profile at the telemetry root and under
    ``stats-group sg_01``, then validates the ``--applied`` show output (config →
    show parity), mirroring SSIM
    ``test_nv_show_system_telemetry_software_stats_validation``.
    """
    del devices
    apply_software_stats_show_telemetry_config(
        engines.dut,
        collector_ips=suite.collector_ips,
        is_nvos=suite.is_nvos,
    )
    with allure.step("Validate software-stats systemd show output (applied)"):
        validate_software_stats_applied(
            engines.dut,
            stats_group_id=CumulusOtelConst.TEST01_STATS_GROUP_ID,
        )


def _run_simple_export_show_validation(
    engines,
    suite: OtelSuiteContext,
    *,
    subtree: str,
    root_interval: int,
    stats_group_interval: int,
) -> None:
    """Apply + validate a simple-export subtree show at root and under ``stats-group sg_01``.

    Shared flow for SSIM ``otel_show`` single-``export`` families (``lldp``,
    ``adaptive-routing-stats``): validates ``<subtree>`` (+ ``export``) at the telemetry
    root and under the stats-group with ``--applied`` (config -> show parity).
    """
    stats_group_id = CumulusOtelConst.TEST01_STATS_GROUP_ID
    apply_simple_export_show_telemetry_config(
        engines.dut,
        collector_ips=suite.collector_ips,
        subtree=subtree,
        root_sample_interval=root_interval,
        stats_group_sample_interval=stats_group_interval,
        stats_group_id=stats_group_id,
        is_nvos=suite.is_nvos,
    )
    with allure.step(f"Validate root {subtree} show output (applied)"):
        validate_simple_export_applied(
            show_system_telemetry_simple_export(engines.dut, subtree),
            context=f"nv show system telemetry {subtree}",
            expected_interval=root_interval,
        )
        validate_simple_export_applied(
            show_system_telemetry_simple_export(engines.dut, subtree, export_node=True),
            context=f"nv show system telemetry {subtree} export",
            export_node=True,
        )
    with allure.step(
        f"Validate stats-group {stats_group_id} {subtree} show output (applied)"
    ):
        validate_simple_export_applied(
            show_system_telemetry_simple_export(
                engines.dut, subtree, stats_group_id=stats_group_id
            ),
            context=f"nv show system telemetry stats-group {stats_group_id} {subtree}",
            expected_interval=stats_group_interval,
        )
        validate_simple_export_applied(
            show_system_telemetry_simple_export(
                engines.dut, subtree, export_node=True, stats_group_id=stats_group_id
            ),
            context=(
                f"nv show system telemetry stats-group {stats_group_id} "
                f"{subtree} export"
            ),
            export_node=True,
        )


def run_adaptive_routing_stats_show_validation(
    engines, devices, suite: OtelSuiteContext
) -> None:
    """Ported SSIM ``test16a`` adaptive-routing-stats show validation (root + stats-group sg_01)."""
    del devices
    _run_simple_export_show_validation(
        engines,
        suite,
        subtree="adaptive-routing-stats",
        root_interval=CumulusOtelConst.TEST01_ROOT_SAMPLE_INTERVALS[
            "adaptive-routing-stats"
        ],
        stats_group_interval=CumulusOtelConst.TEST01_STATS_GROUP_SAMPLE_INTERVALS[
            "adaptive-routing-stats"
        ],
    )


def run_lldp_show_validation(engines, devices, suite: OtelSuiteContext) -> None:
    """Ported SSIM ``test19a`` lldp show validation (root + stats-group sg_01)."""
    del devices
    _run_simple_export_show_validation(
        engines,
        suite,
        subtree="lldp",
        root_interval=CumulusOtelConst.TEST01_ROOT_SAMPLE_INTERVALS["lldp"],
        stats_group_interval=CumulusOtelConst.TEST01_STATS_GROUP_SAMPLE_INTERVALS[
            "lldp"
        ],
    )

# --- test04 control-plane stats vs CLI ---------------------------------------------


def run_control_plane_stats_data_validation(
    engines, devices, suite: OtelSuiteContext, cur_dir: str
) -> None:
    """Ported test04 control-plane counters OTEL-vs-CLI validation.

    Cumulus-lab test (the ``sx_api`` CLI reference is Cumulus-only); the NVOS branch
    is kept for structural parity with :func:`run_platform_stats_validation` but the
    caller marks test04 ``cumulus_only``.
    """
    collector_ips = suite.collector_ips
    ensure_control_plane_stats_otel_cli_cache(
        engines,
        suite,
        cur_dir,
        collector_ips=collector_ips,
        vrf=suite.export_vrf,
        apply_telemetry_config=not suite.is_nvos,
        telemetry_unit=suite.telemetry_unit,
    )
    validate_cached_control_plane_stats_against_cli(devices)
