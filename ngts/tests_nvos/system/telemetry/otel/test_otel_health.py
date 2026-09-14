import pytest

import ngts.tools.test_utils.allure_utils as allure

from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
    restart_nvtelemetry,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import (
    apply_otel_mgmt_vrf_no_tls_telemetry_config,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.telemetry_health import (
    assert_otlp_session_established,
    cleanup_otlp_export_session,
    ensure_mgmt_vrf_secured_otlp_session,
    restart_otlp_collector_and_verify_health,
    start_otlp_related_systemd_units,
    stop_otlp_related_systemd_units,
    verify_export_destinations_connectivity,
    verify_export_destinations_health,
    verify_otel_health_services,
    verify_otelcol_server_active,
    verify_otlp_client_active,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.otel,
    pytest.mark.cumulus_only,
]


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test01_otel_telemetry_health_mgmt_vrf_insecure(
    engines, otel_suite_mgmt, test_api,
):
    """Cumulus lab: OTEL telemetry health on mgmt VRF without TLS (SSIM health test01).

    Applies ``OtelMgmtVrfNoTLSConfig``-equivalent NVUE, restarts the OTLP session,
    then validates ``nv show system telemetry health`` service status, export
    connectivity, and counter flow. SSIM Scapy traffic (leaf2/leaf3) is omitted —
    NGTS mlx lab has no multi-leaf topology.
    """
    TestToolkit.tested_api = test_api
    vrf = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT
    collector_ips = otel_suite_mgmt.collector_ips

    with allure.step("Apply mgmt VRF insecure telemetry and restart OTLP session"):
        apply_otel_mgmt_vrf_no_tls_telemetry_config(engines.dut, collector_ips=collector_ips)
        cleanup_otlp_export_session(engines.dut, otel_suite_mgmt.primary, vrf=vrf)

    with allure.step("Verify OTLP session established"):
        assert_otlp_session_established(otel_suite_mgmt.primary)

    with allure.step("Verify OTEL health services are active"):
        verify_otel_health_services(
            engines.dut,
            services=CumulusOtelConst.OTEL_HEALTH_SERVICES_MGMT_INSECURE,
            max_attempts=30,
        )

    with allure.step("Verify export destination connectivity and counter flow"):
        verify_export_destinations_health(engines.dut, max_connectivity_attempts=40)


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test02_restart_otlp_services_telemetry_health_mgmt_vrf_insecure(
    engines, otel_suite_mgmt, test_api,
):
    """Cumulus lab: telemetry health recovery after OTLP systemd restart (SSIM health test02).

    Stops prometheus / nv-telemetry / asic-monitor units, restarts them, then re-validates
    ``nv show system telemetry health``.
    """
    TestToolkit.tested_api = test_api
    collector_ips = otel_suite_mgmt.collector_ips

    with allure.step("Ensure mgmt VRF insecure telemetry is applied"):
        apply_otel_mgmt_vrf_no_tls_telemetry_config(engines.dut, collector_ips=collector_ips)

    with allure.step("Verify initial OTLP session"):
        assert_otlp_session_established(otel_suite_mgmt.primary)

    stop_otlp_related_systemd_units(engines.dut)
    start_otlp_related_systemd_units(engines.dut)

    with allure.step("Verify OTEL health services after restart"):
        verify_otel_health_services(engines.dut)

    with allure.step("Verify export destination connectivity and counter flow"):
        verify_export_destinations_health(engines.dut)


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test01_otel_telemetry_health_mgmt_vrf_secured(
    engines, otel_suite_mgmt_secured, test_api,
):
    """Cumulus lab: OTEL health on mgmt VRF with TLS (SSIM health test01)."""
    TestToolkit.tested_api = test_api
    vrf = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT

    restart_nvtelemetry(engines.dut, vrf)
    assert_otlp_session_established(otel_suite_mgmt_secured.primary)
    verify_otlp_client_active(engines.dut, vrf)
    verify_otelcol_server_active(otel_suite_mgmt_secured.primary)
    verify_otel_health_services(engines.dut)
    verify_export_destinations_connectivity(
        engines.dut,
        check_drop_counter=True,
    )


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test02_restart_otlp_server_telemetry_health_mgmt_vrf_secured(
    engines, otel_suite_mgmt_secured, test_api,
):
    """Cumulus lab: health recovery after OTLP collector restart — mgmt VRF TLS (SSIM health test02)."""
    TestToolkit.tested_api = test_api

    ensure_mgmt_vrf_secured_otlp_session(
        engines.dut,
        otel_suite_mgmt_secured.primary,
        collector_ip=otel_suite_mgmt_secured.primary_ip,
    )
    restart_otlp_collector_and_verify_health(
        engines.dut,
        otel_suite_mgmt_secured.primary,
        verify_counter_flow_after=True,
    )
