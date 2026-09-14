"""NVOS OTEL telemetry — metric coverage and Cumulus lab OTLP tests."""

import time

import pytest

import ngts.tools.test_utils.allure_utils as allure

from ngts.nvos_constants.constants_nvos import ApiType, TelemetryConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.system.telemetry.otel.constants import OtelCollectorConst
from ngts.tests_nvos.system.telemetry.otel.helpers import (
    assert_metric_names_strict,
    collect_metric_names_window,
    enable_nvos_telemetry_families,
    expected_metric_names_from_dut,
    log_otel_metric_inventory,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus import cache as telemetryCache
from ngts.tests_nvos.system.telemetry.otel.cumulus.catalog import (
    flatten_supported_metrics,
    supported_metrics_for_mgmt_vrf_secured_validation,
    supported_metrics_for_secured_validation,
    supported_metrics_for_test01_validation,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
    collect_and_cache_mgmt_vrf_otel_session,
    collect_and_cache_secured_otel_session,
    ensure_mgmt_vrf_otel_cli_cache,
    get_dut_hostname,
    log_collector_export_metrics,
    cumulus_otel_artifact_path,
    restart_nvtelemetry,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus import cache as telemetryCache
from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import require_cumulus_at_least
from ngts.tests_nvos.system.telemetry.otel.cumulus.lab_topology import resolve_cumulus_lab_interfaces_on_dut
from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import (
    apply_otel_mgmt_vrf_no_tls_telemetry_config,
    dut_root_on_nvme_storage,
    show_platform_stats_class_transceiver_info,
    test01_expected_metric_names_from_catalog,
    validate_test01_collected_metrics,
    validate_transceiver_info_applied,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.telemetry_health import (
    assert_otlp_session_established,
    cleanup_otlp_export_session,
    restart_otlp_collector_and_verify_health,
    start_otlp_related_systemd_units,
    stop_otlp_related_systemd_units,
    verify_export_destinations_connectivity,
    verify_export_destinations_health,
    verify_otel_health_services,
    verify_otelcol_server_active,
    verify_otlp_client_active,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.tls import assert_otlp_grpc_certificate_applied
from ngts.tests_nvos.system.telemetry.otel.cumulus.validations import OtelDataValidations

pytestmark = [
    pytest.mark.system,
    pytest.mark.otel,
]


# def test_otel_platform_stats_metrics_present(engines, devices, otel_suite, is_ib_router, tmp_path):
#     """Enable platform-stats only and assert expected metric names appear on both collectors."""
#     enable_nvos_telemetry_families(
#         engines.dut, otel_suite.primary_ip, [TelemetryConsts.PLATFORM_STATS]
#     )

#     expected = expected_metric_names_from_dut(
#         engines,
#         str(tmp_path),
#         OtelCollectorConst.METRICS_CLASSES_PLATFORM_STATS_GROUP_PREFIXES,
#         file_name="metrics-classes-platform.yaml",
#         is_ib_router=is_ib_router,
#         devices=devices,
#     )
#     assert expected, (
#         f"No expected platform-stats metric names after gating "
#         f"(group prefixes={OtelCollectorConst.METRICS_CLASSES_PLATFORM_STATS_GROUP_PREFIXES})."
#     )

#     primary_names = collect_metric_names_window(
#         otel_suite.primary,
#         str(tmp_path),
#         label="primary-platform",
#         max_sample_interval_sec=OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC,
#     )
#     secondary_names = collect_metric_names_window(
#         otel_suite.secondary,
#         str(tmp_path),
#         label="secondary-platform",
#         max_sample_interval_sec=OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC,
#     )

#     assert_metric_names_strict(expected, primary_names, collector_label="primary (platform-stats)")
#     assert_metric_names_strict(expected, secondary_names, collector_label="secondary (platform-stats)")


# def test_otel_all_metrics_present(engines, devices, otel_suite, is_ib_router, tmp_path):
#     """Enable every telemetry family and assert expected metric names appear on the primary collector."""
#     enable_nvos_telemetry_families(
#         engines.dut, otel_suite.primary_ip, TelemetryConsts.ALL_STATS_SUBTREES
#     )

#     expected = expected_metric_names_from_dut(
#         engines,
#         str(tmp_path),
#         OtelCollectorConst.METRICS_CLASSES_FULL_EXPORT_GROUP_PREFIXES,
#         file_name="metrics-classes-full.yaml",
#         is_ib_router=is_ib_router,
#         devices=devices,
#     )
#     assert expected, (
#         f"No expected metric names after gating "
#         f"(group prefixes={OtelCollectorConst.METRICS_CLASSES_FULL_EXPORT_GROUP_PREFIXES})."
#     )

#     max_sample_interval_sec = max(
#         OtelCollectorConst.INTERFACE_STATS_SAMPLE_INTERVAL_SEC,
#         OtelCollectorConst.PEER_PORT_STATS_SAMPLE_INTERVAL_SEC,
#         OtelCollectorConst.IB_ROUTER_STATS_SAMPLE_INTERVAL_SEC,
#         OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC,
#     )
#     actual_names = collect_metric_names_window(
#         otel_suite.primary,
#         str(tmp_path),
#         label="primary-full",
#         max_sample_interval_sec=max_sample_interval_sec,
#     )

#     assert_metric_names_strict(expected, actual_names, collector_label="primary (full export)")


# @pytest.mark.cumulus
# @pytest.mark.parametrize("test_api", [ApiType.NVUE])
# def test01_otel_metrics_collection_mgmt_vrf_insecure(
#     engines, otel_suite_mgmt, otel_telemetry_cache, test_api, tmp_path
# ):
#     """Cumulus lab: OTLP metric collection on mgmt VRF without TLS (test01)."""
#     TestToolkit.tested_api = test_api
#     telemetryCache.clear_data()
#     cur_dir = str(tmp_path)

#     collect_and_cache_mgmt_vrf_otel_session(
#         engines.dut,
#         otel_suite_mgmt.primary,
#         cur_dir,
#         collector_ips=(otel_suite_mgmt.primary_ip, otel_suite_mgmt.secondary_ip),
#     )
#     otel_data_val = OtelDataValidations()
#     hostname = get_dut_hostname(engines.dut)

#     metrics_timestamps = telemetryCache.get_data("otel")["metrics_timestamps"]
#     log_collector_export_metrics(
#         cumulus_otel_artifact_path(cur_dir),
#         collector_label="primary (test01 stats-group export)",
#         parsed_metrics_timestamps=metrics_timestamps,
#     )

#     exclude_metrics = list(CumulusOtelConst.TEST01_EXCLUDE_METRICS)
#     if dut_root_on_nvme_storage(engines.dut):
#         exclude_metrics.extend(CumulusOtelConst.TEST01_EXCLUDE_METRICS_NVME)

#     total_metrics = supported_metrics_for_test01_validation(engines.dut)
#     assert total_metrics, "supported_metrics catalog is empty for this DUT"

#     catalog_flat = flatten_supported_metrics(total_metrics, skip_buckets=())
#     expected_flat = test01_expected_metric_names_from_catalog(
#         total_metrics, exclude_metrics=exclude_metrics
#     )
#     log_otel_metric_inventory(
#         set(metrics_timestamps.keys()),
#         collector_label="primary (test01 stats-group export)",
#         group_name_prefixes=CumulusOtelConst.TEST01_METRICS_GROUP_PREFIXES,
#         expected=expected_flat,
#         extra_ignore=set(exclude_metrics),
#     )

#     with allure.step("Validate collected metrics (test01)"):
#         validate_test01_collected_metrics(
#             metrics_timestamps,
#             catalog_flat=catalog_flat,
#             exclude_metrics=exclude_metrics,
#             group_prefixes=CumulusOtelConst.TEST01_METRICS_GROUP_PREFIXES,
#         )

#     with allure.step("Validate collected metrics attributes count (test01)"):
#         otel_data_val.validate_collected_metrics_attributes_count(
#             metrics_timestamps,
#             hostname,
#             total_metrics=total_metrics,
#             exclude_metrics=exclude_metrics,
#         )


# @pytest.mark.cumulus
# @pytest.mark.parametrize("test_api", [ApiType.NVUE])
# def test05_otel_platform_stats_validation(
#     engines, otel_suite_mgmt, otel_telemetry_cache, test_api, tmp_path
# ):
#     """Cumulus lab: validate OTLP platform environment metrics vs CLI (test05).

#     Compares platform temp/psu telemetry against ``Platform().environment`` snapshots.
#     Reuses telemetry cache from test01 when present; otherwise runs the mgmt VRF
#     collection session.
#     """
#     TestToolkit.tested_api = test_api
#     cur_dir = str(tmp_path)

#     ensure_mgmt_vrf_otel_cli_cache(
#         engines,
#         otel_suite_mgmt,
#         cur_dir,
#         collector_ips=(otel_suite_mgmt.primary_ip, otel_suite_mgmt.secondary_ip),
#         cli_platform_only=True,
#     )

#     otel_platform_stats = telemetryCache.get_data("otel").get("platform_stats")
#     cli_data = telemetryCache.get_data("cli")

#     otel_data_val = OtelDataValidations()
#     with allure.step("Validate platform stats OTEL vs CLI (test05)"):
#         otel_data_val.plat_stats_data_validation(
#             otel_platform_stats,
#             cli_data.get("plat_env_temp"),
#             cli_data.get("plat_env_psu"),
#             cli_data.get("plat_env_fan"),
#         )


# @pytest.mark.cumulus
# @pytest.mark.parametrize("test_api", [ApiType.NVUE])
# def test_nv_show_system_telemetry_platform_stats_class_transceiver_info_validation(
#     engines, otel_suite_mgmt, test_api,
# ):
#     """Cumulus lab: validate transceiver-info platform-stats class (SSIM otel_show).

#     ``nv show system telemetry platform-stats class transceiver-info --applied``
#     ``nv show system telemetry stats-group sg_01 platform-stats class transceiver-info --applied``
#     """
#     TestToolkit.tested_api = test_api

#     apply_otel_mgmt_vrf_no_tls_telemetry_config(
#         engines.dut,
#         collector_ips=(otel_suite_mgmt.primary_ip, otel_suite_mgmt.secondary_ip),
#     )

#     root_out = show_platform_stats_class_transceiver_info(engines.dut)
#     with allure.step("Validate root transceiver-info (applied)"):
#         validate_transceiver_info_applied(
#             root_out,
#             context="nv show system telemetry platform-stats class transceiver-info",
#         )

#     sg_out = show_platform_stats_class_transceiver_info(
#         engines.dut,
#         stats_group_id=CumulusOtelConst.TEST01_STATS_GROUP_ID,
#     )
#     with allure.step(
#         f"Validate stats-group {CumulusOtelConst.TEST01_STATS_GROUP_ID} "
#         "transceiver-info (applied)"
#     ):
#         validate_transceiver_info_applied(
#             sg_out,
#             context=(
#                 f"nv show system telemetry stats-group "
#                 f"{CumulusOtelConst.TEST01_STATS_GROUP_ID} "
#                 "platform-stats class transceiver-info"
#             ),
#         )


# @pytest.mark.cumulus
# @pytest.mark.parametrize("test_api", [ApiType.NVUE])
# def test01_otel_telemetry_health_mgmt_vrf_insecure(
#     engines, otel_suite_mgmt, test_api,
# ):
#     """Cumulus lab: OTEL telemetry health on mgmt VRF without TLS (SSIM health test01).

#     Applies ``OtelMgmtVrfNoTLSConfig``-equivalent NVUE, restarts the OTLP session,
#     then validates ``nv show system telemetry health`` service status, export
#     connectivity, and counter flow. SSIM Scapy traffic (leaf2/leaf3) is omitted —
#     NGTS mlx lab has no multi-leaf topology.
#     """
#     TestToolkit.tested_api = test_api
#     vrf = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT
#     collector_ips = (otel_suite_mgmt.primary_ip, otel_suite_mgmt.secondary_ip)

#     with allure.step("Apply mgmt VRF insecure telemetry and restart OTLP session"):
#         apply_otel_mgmt_vrf_no_tls_telemetry_config(engines.dut, collector_ips=collector_ips)
#         cleanup_otlp_export_session(engines.dut, otel_suite_mgmt.primary, vrf=vrf)

#     with allure.step("Verify OTLP session established"):
#         assert_otlp_session_established(otel_suite_mgmt.primary)

#     with allure.step("Verify OTEL health services are active"):
#         verify_otel_health_services(
#             engines.dut,
#             services=CumulusOtelConst.OTEL_HEALTH_SERVICES_MGMT_INSECURE,
#             max_attempts=30,
#         )

#     with allure.step("Verify export destination connectivity and counter flow"):
#         verify_export_destinations_health(engines.dut, max_connectivity_attempts=40)


# @pytest.mark.cumulus
# @pytest.mark.parametrize("test_api", [ApiType.NVUE])
# def test02_restart_otlp_services_telemetry_health_mgmt_vrf_insecure(
#     engines, otel_suite_mgmt, test_api,
# ):
#     """Cumulus lab: telemetry health recovery after OTLP systemd restart (SSIM health test02).

#     Stops prometheus / nv-telemetry / asic-monitor units, restarts them, then re-validates
#     ``nv show system telemetry health``.
#     """
#     TestToolkit.tested_api = test_api
#     collector_ips = (otel_suite_mgmt.primary_ip, otel_suite_mgmt.secondary_ip)

#     with allure.step("Ensure mgmt VRF insecure telemetry is applied"):
#         apply_otel_mgmt_vrf_no_tls_telemetry_config(engines.dut, collector_ips=collector_ips)

#     with allure.step("Verify initial OTLP session"):
#         assert_otlp_session_established(otel_suite_mgmt.primary)

#     stop_otlp_related_systemd_units(engines.dut)
#     start_otlp_related_systemd_units(engines.dut)

#     with allure.step("Verify OTEL health services after restart"):
#         verify_otel_health_services(engines.dut)

#     with allure.step("Verify export destination connectivity and counter flow"):
#         verify_export_destinations_health(engines.dut)


# @pytest.mark.cumulus
# @pytest.mark.parametrize("test_api", [ApiType.NVUE])
# def test01_otel_data_collection_mgmt_vrf_secured(
#     engines, otel_suite_mgmt_secured, otel_telemetry_cache, test_api, tmp_path
# ):
#     """Cumulus lab: OTLP metric collection on mgmt VRF with TLS (SSIM otlp test01)."""
#     TestToolkit.tested_api = test_api
#     telemetryCache.clear_data()
#     cur_dir = str(tmp_path)
#     vrf = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT

#     otel_payload = collect_and_cache_secured_otel_session(
#         engines.dut,
#         otel_suite_mgmt_secured.primary,
#         cur_dir,
#         vrf=vrf,
#         wait_sec=CumulusOtelConst.SECURED_COLLECTION_WAIT_MGMT_VRF_SEC,
#         cleanup_session=True,
#     )
#     assert_otlp_grpc_certificate_applied(
#         engines.dut,
#         destination_id=otel_suite_mgmt_secured.primary_ip,
#     )

#     metrics_timestamps = otel_payload["metrics_timestamps"]
#     hostname = get_dut_hostname(engines.dut)
#     exclude_metrics = secured_exclude_metrics(engines.dut, no_routing=True)
#     total_metrics = supported_metrics_for_mgmt_vrf_secured_validation(engines.dut)

#     with allure.step("Validate collected metrics (mgmt VRF secured)"):
#         OtelDataValidations().validate_collected_metrics(
#             metrics_timestamps,
#             hostname,
#             total_metrics=total_metrics,
#             exclude_metrics=exclude_metrics,
#         )

# @pytest.mark.cumulus
# @pytest.mark.parametrize("test_api", [ApiType.NVUE])
# def test01_otel_telemetry_health_mgmt_vrf_secured(
#     engines, otel_suite_mgmt_secured, test_api,
# ):
#     """Cumulus lab: OTEL health on mgmt VRF with TLS (SSIM health test01)."""
#     TestToolkit.tested_api = test_api
#     vrf = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT

#     restart_nvtelemetry(engines.dut, vrf)
#     assert_otlp_session_established(otel_suite_mgmt_secured.primary)
#     verify_otlp_client_active(engines.dut, vrf)
#     verify_otelcol_server_active(otel_suite_mgmt_secured.primary)
#     verify_otel_health_services(engines.dut)
#     verify_export_destinations_connectivity(
#         engines.dut,
#         check_drop_counter=True,
#     )


# @pytest.mark.cumulus
# @pytest.mark.parametrize("test_api", [ApiType.NVUE])
# def test02_restart_otlp_server_telemetry_health_mgmt_vrf_secured(
#     engines, otel_suite_mgmt_secured, test_api,
# ):
#     """Cumulus lab: health recovery after OTLP collector restart — mgmt VRF TLS (SSIM health test02)."""
#     TestToolkit.tested_api = test_api

#     assert_otlp_session_established(otel_suite_mgmt_secured.primary)
#     restart_otlp_collector_and_verify_health(
#         engines.dut,
#         otel_suite_mgmt_secured.primary,
#         verify_counter_flow_after=True,
#     )


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test02a_otel_interface_pg_rx_validation(
    engines, otel_mgmt_insecure_validation_cache, test_api
):
    """SSIM ``Test_Otel_Mgmt_Vrf_Insecure::test02a`` — pg_rx_frames vs CLI."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.10.0")
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    otel_data_val = OtelDataValidations()
    otel_data_val.validate_interface_pg_rx(
        telemetryCache.get_data("otel").get("intf_stats"),
        telemetryCache.get_data("cli").get("ingress_stats"),
        lab,
    )


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test02b_otel_interface_tc_tx_validation(
    engines, otel_mgmt_insecure_validation_cache, test_api
):
    """SSIM ``Test_Otel_Mgmt_Vrf_Insecure::test02b`` — tc_tx_frames vs CLI."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.10.0")
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    otel_data_val = OtelDataValidations()
    otel_data_val.validate_interface_tc_tx(
        telemetryCache.get_data("otel").get("intf_stats"),
        telemetryCache.get_data("cli").get("egress_stats"),
        lab,
    )


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test03a_otel_histogram_counter_validation(
    engines, otel_mgmt_insecure_validation_cache, test_api
):
    """SSIM ``Test_Otel_Mgmt_Vrf_Insecure::test03a`` — histogram counter vs snapshot."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.10.0")
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    otel_data_val = OtelDataValidations()
    otel_data_val.validate_histogram_counter(
        telemetryCache.get_data("otel").get("histograms"),
        telemetryCache.get_data("cli").get("hist_snap"),
        lab,
    )


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test03b_otel_histogram_ingress_buffer_validation(
    engines, otel_mgmt_insecure_validation_cache, test_api
):
    """SSIM ``Test_Otel_Mgmt_Vrf_Insecure::test03b`` — ingress buffer histogram."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.10.0")
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    otel_data_val = OtelDataValidations()
    otel_data_val.validate_histogram_ingress_buffer(
        telemetryCache.get_data("otel").get("histograms"),
        telemetryCache.get_data("cli").get("hist_snap"),
        lab,
    )


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test03c_otel_histogram_egress_buffer_validation(
    engines, otel_mgmt_insecure_validation_cache, test_api
):
    """SSIM ``Test_Otel_Mgmt_Vrf_Insecure::test03c`` — egress buffer histogram."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.10.0")
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    otel_data_val = OtelDataValidations()
    otel_data_val.validate_histogram_egress_buffer(
        telemetryCache.get_data("otel").get("histograms"),
        telemetryCache.get_data("cli").get("hist_snap"),
        lab,
    )


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test03d_otel_histogram_latency_validation(
    engines, otel_mgmt_insecure_validation_cache, test_api
):
    """SSIM ``Test_Otel_Mgmt_Vrf_Insecure::test03d`` — latency histogram buckets."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.11.0")
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    otel_data_val = OtelDataValidations()
    otel_data_val.validate_histogram_latency(
        telemetryCache.get_data("otel").get("histograms"),
        telemetryCache.get_data("cli").get("hist_snap"),
        lab,
    )


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test03e_otel_histogram_structure_validation(
    engines, otel_mgmt_insecure_validation_cache, test_api
):
    """SSIM ``Test_Otel_Mgmt_Vrf_Insecure::test03e`` — histogram OTLP structure."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.10.0")
    hostname = get_dut_hostname(engines.dut)
    otel_data_val = OtelDataValidations()
    otel_data_val.validate_histogram_structure(
        telemetryCache.get_data("otel").get("hist_list"),
        hostname=hostname,
    )
