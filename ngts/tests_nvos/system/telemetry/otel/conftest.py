"""Pytest plumbing for the NVOS OTEL telemetry test suite.

Platform note: never call ``system.telemetry.set(state)`` or ``export.otlp.set``
directly for master enable — use :func:`~ngts.tests_nvos.system.telemetry.otel.helpers.stage_telemetry_master_state`
and :func:`~ngts.tests_nvos.system.telemetry.otel.helpers.apply_telemetry_configuration`
with ``suite.is_nvos`` (Cumulus: ``telemetry state``; NVOS: ``export otlp state``).
"""

import logging

import pytest

from ngts.nvos_constants.constants_nvos import ApiType, TelemetryConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.system.telemetry.otel.cumulus import cache as telemetry_cache
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.helpers import (
    OtelSuiteContext,
    is_nvos_dut,
    setup_otel_suite,
    setup_otel_suite_secured,
    teardown_otel_suite,
)
from ngts.tests_nvos.system.telemetry.otel.syslog_constants import SyslogOtelConst

logger = logging.getLogger(__name__)


def _ensure_nvue_api() -> None:
    """Module fixtures run before function-scoped ``test_api``; pin NVUE for setup."""
    TestToolkit.tested_api = ApiType.NVUE


@pytest.fixture(scope="module")
def otel_telemetry_cache():
    """Shared telemetry cache for Cumulus OTLP tests (test01/test05)."""
    telemetry_cache.clear_data()
    yield telemetry_cache
    telemetry_cache.clear_data()


@pytest.fixture(scope="module")
def otel_suite(engines, devices) -> OtelSuiteContext:
    """OTEL collector + DUT OTLP, one destination (default export VRF)."""
    _ensure_nvue_api()
    suite = setup_otel_suite(engines, is_nvos=is_nvos_dut(devices))
    try:
        yield suite
    finally:
        teardown_otel_suite(engines, suite)


@pytest.fixture(scope="module")
def otel_suite_dual(engines, devices) -> OtelSuiteContext:
    """OTEL collectors on sonic-mgmt + HA, with both OTLP destinations configured.

    Only for tests marked ``multiple_otlp_collectors`` — those that read metrics off
    the second collector. Skips when the topology has no ``ha`` player, since most
    setups have no host for a second collector.
    """
    _ensure_nvue_api()
    suite = setup_otel_suite(engines, is_nvos=is_nvos_dut(devices), use_secondary=True)
    try:
        yield suite
    finally:
        teardown_otel_suite(engines, suite)


@pytest.fixture(scope="module")
def otel_suite_single(engines, devices) -> OtelSuiteContext:
    """Bring up a single-collector OTEL suite: primary collector + one OTLP destination, no HA player."""
    _ensure_nvue_api()
    suite = setup_otel_suite(engines, is_nvos=is_nvos_dut(devices), use_secondary=False)
    try:
        yield suite
    finally:
        teardown_otel_suite(engines, suite)


@pytest.fixture(scope="module")
def otel_suite_mgmt(engines, devices) -> OtelSuiteContext:
    """OTEL insecure suite for the ported mgmt-VRF tests.

    Cumulus exports over the ``mgmt`` VRF (sonic-mgmt / HA reachability). NVOS has
    no ``mgmt`` VRF, so it falls back to the ``default`` export VRF.
    """
    _ensure_nvue_api()
    is_nvos = is_nvos_dut(devices)
    export_vrf = (
        TelemetryConsts.Defaults.EXPORT_VRF
        if is_nvos
        else CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT
    )
    suite = setup_otel_suite(engines, export_vrf=export_vrf, is_nvos=is_nvos)
    try:
        yield suite
    finally:
        teardown_otel_suite(engines, suite)


@pytest.fixture(scope="module")
def otel_suite_secured(engines, devices) -> OtelSuiteContext:
    """OTEL suite with TLS on default export VRF (SSIM ``OtelDefaultVrfWithTLSConfig``)."""
    _ensure_nvue_api()
    suite = setup_otel_suite_secured(
        engines,
        export_vrf=CumulusOtelConst.TELEMETRY_EXPORT_VRF_DEFAULT,
        default_vrf_grpc_certificate=True,
        is_nvos=is_nvos_dut(devices),
    )
    try:
        yield suite
    finally:
        teardown_otel_suite(engines, suite)


@pytest.fixture(scope="module")
def otel_suite_single_secured(engines, devices) -> OtelSuiteContext:
    """Single-collector TLS OTLP suite (primary collector only, no HA player).

    Used only by the syslog secured test: enable a trimmed, universally-supported
    metric family to open the TLS session (avoids the ib-router-stats unmapped-metric
    subscription storm that destabilizes the pipeline on IB builds).
    """
    _ensure_nvue_api()
    suite = setup_otel_suite_secured(
        engines,
        export_vrf=CumulusOtelConst.TELEMETRY_EXPORT_VRF_DEFAULT,
        default_vrf_grpc_certificate=True,
        is_nvos=is_nvos_dut(devices),
        use_secondary=False,
        nvos_families=SyslogOtelConst.SECURED_SESSION_FAMILIES,
    )
    try:
        yield suite
    finally:
        teardown_otel_suite(engines, suite)


@pytest.fixture(scope="module")
def otel_suite_mgmt_secured(engines, devices) -> OtelSuiteContext:
    """OTEL TLS suite for the ported secured mgmt-VRF tests.

    Cumulus uses the ``mgmt`` VRF with a destination certificate. NVOS has no
    ``mgmt`` VRF, so it exports over the ``default`` VRF with a gRPC-level
    certificate (same posture as :func:`otel_suite_secured`).
    """
    _ensure_nvue_api()
    from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
        settle_dut_ssh_after_module_transition,
    )

    settle_dut_ssh_after_module_transition(engines.dut)
    is_nvos = is_nvos_dut(devices)
    if is_nvos:
        suite = setup_otel_suite_secured(
            engines,
            export_vrf=TelemetryConsts.Defaults.EXPORT_VRF,
            default_vrf_grpc_certificate=True,
            is_nvos=True,
        )
    else:
        suite = setup_otel_suite_secured(
            engines,
            export_vrf=CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT,
            default_vrf_grpc_certificate=False,
            enable_routing=False,
            enable_interface_histogram=True,
            is_nvos=False,
        )
    try:
        yield suite
    finally:
        teardown_otel_suite(engines, suite)


@pytest.fixture(scope="module")
def otel_mgmt_insecure_validation_cache(engines, otel_suite_mgmt, tmp_path_factory):
    """``Test_Otel_Mgmt_Vrf_Insecure`` shared OTLP + CLI cache (test02a–test03e)."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
        collect_and_cache_mgmt_vrf_insecure_validation_session,
    )

    cur_dir = str(tmp_path_factory.mktemp("otel_mgmt_insecure_validation"))
    collector_ips = otel_suite_mgmt.collector_ips
    collect_and_cache_mgmt_vrf_insecure_validation_session(
        engines.dut,
        otel_suite_mgmt.primary,
        cur_dir,
        collector_ips=collector_ips,
    )
    yield cur_dir


def _teardown_deferred_gnmi_session(dut, gnmi_session) -> None:
    """SSIM ``post_suite_hook`` parity: remove grpc-tunnel after coexistence tests."""
    if gnmi_session is None:
        return
    from ngts.tests_nvos.system.telemetry.otel.cumulus.gnmi_coexistence import (
        teardown_gnmi_coexistence_session,
    )

    teardown_gnmi_coexistence_session(dut, gnmi_session)


@pytest.fixture(scope="module")
def coexistence_smoke_cache(engines, otel_suite_mgmt_secured, tmp_path_factory):
    """``Test_Telemetry_Coexistence_Smoke`` OTEL cache (secured; NVOS skips gNMI)."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.coexistence import (
        collect_coexistence_smoke_cache,
    )

    suite = otel_suite_mgmt_secured
    cur_dir = str(tmp_path_factory.mktemp("coexistence_smoke"))
    collect_result = collect_coexistence_smoke_cache(
        engines.dut,
        suite.primary,
        cur_dir,
        sonic_mgmt=engines.sonic_mgmt,
        vrf=suite.export_vrf,
        defer_gnmi_teardown=True,
        is_nvos=suite.is_nvos,
        telemetry_unit=suite.telemetry_unit,
    )
    yield cur_dir
    _teardown_deferred_gnmi_session(engines.dut, collect_result.gnmi_session)


@pytest.fixture(scope="module")
def coexistence_interface_qos_cache(engines, otel_suite_mgmt, tmp_path_factory):
    """Lazy Interface/QoS bundle; call ``ensure_collected()`` from test body for Allure nesting."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.coexistence import (
        CoexistenceInterfaceQosBundle,
    )

    suite = otel_suite_mgmt
    bundle = CoexistenceInterfaceQosBundle(
        cur_dir=str(tmp_path_factory.mktemp("coexistence_interface_qos")),
        dut=engines.dut,
        collector=suite.primary,
        sonic_mgmt=engines.sonic_mgmt,
        collector_ips=suite.collector_ips,
        is_nvos=suite.is_nvos,
        export_vrf=suite.export_vrf,
        telemetry_unit=suite.telemetry_unit,
    )
    yield bundle
    bundle.teardown()


@pytest.fixture(scope="module")
def coexistence_component_system_cache(engines, otel_suite_mgmt, tmp_path_factory):
    """``Test_Telemetry_Component_System_Validation_Coexistence`` OTEL cache."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.coexistence import (
        collect_coexistence_component_system_cache,
    )

    from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
        settle_dut_ssh_after_module_transition,
    )

    suite = otel_suite_mgmt
    settle_dut_ssh_after_module_transition(engines.dut)
    cur_dir = str(tmp_path_factory.mktemp("coexistence_component_system"))
    collector_ips = suite.collector_ips
    collect_result = collect_coexistence_component_system_cache(
        engines.dut,
        suite.primary,
        cur_dir,
        collector_ips=collector_ips,
        sonic_mgmt=engines.sonic_mgmt,
        defer_gnmi_teardown=True,
        is_nvos=suite.is_nvos,
        export_vrf=suite.export_vrf,
        telemetry_unit=suite.telemetry_unit,
    )
    yield cur_dir
    _teardown_deferred_gnmi_session(engines.dut, collect_result.gnmi_session)


@pytest.fixture(scope="module")
def coexistence_attribute_filtering_cache(engines, otel_suite_mgmt, tmp_path_factory):
    """SSIM ``test_06_validate_otel_metric_attribute_filtering`` OTEL cache (unset pg/tc/sp)."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.coexistence import (
        collect_coexistence_attribute_filtering_cache,
    )
    from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
        settle_dut_ssh_after_module_transition,
    )

    settle_dut_ssh_after_module_transition(engines.dut)
    cur_dir = str(tmp_path_factory.mktemp("coexistence_attribute_filtering"))
    collector_ips = otel_suite_mgmt.collector_ips
    collect_coexistence_attribute_filtering_cache(
        engines.dut,
        otel_suite_mgmt.primary,
        cur_dir,
        collector_ips=collector_ips,
    )
    yield cur_dir


@pytest.fixture(scope="module")
def coexistence_otel_persistence_cache(engines, otel_suite_mgmt, tmp_path_factory):
    """Lazy OTEL persistence bundle shared by tests 13 and 14."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.coexistence import (
        CoexistenceOtelPersistenceBundle,
    )

    return CoexistenceOtelPersistenceBundle(
        cur_dir=str(tmp_path_factory.mktemp("coexistence_otel_persistence")),
        dut=engines.dut,
        collector=otel_suite_mgmt.primary,
        collector_ips=otel_suite_mgmt.collector_ips,
        vrf=otel_suite_mgmt.export_vrf,
    )


@pytest.fixture(scope="module")
def otel_cli_coverage_baseline(engines, otel_suite_mgmt) -> OtelSuiteContext:
    """``OtelMgmtVrfNoTLSConfig`` NVUE baseline for unset CLI coverage tests."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import (
        apply_otel_cli_coverage_baseline,
        cleanup_otel_cli_coverage_labels,
    )

    _ensure_nvue_api()
    apply_otel_cli_coverage_baseline(
        engines.dut, otel_suite_mgmt.collector_ips, suite=otel_suite_mgmt
    )
    try:
        yield otel_suite_mgmt
    finally:
        _ensure_nvue_api()
        cleanup_otel_cli_coverage_labels(engines.dut)
