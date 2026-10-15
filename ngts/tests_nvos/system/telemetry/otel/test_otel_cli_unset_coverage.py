"""NV unset CLI coverage (SSIM ``Test_Otel_Mgmt_Vrf_Insecure_CLI_Coverage``).

Runs on NVOS and Cumulus. Histogram / interface-stats-buffer unsets are Cumulus-only
(no NVOS equivalent) and stay marked ``cumulus_only``.

The module fixture applies the interface histogram, labels, and system telemetry
tree once and records that NVUE revision. System tests roll back to it with one
``nv config apply``. Interface tests do not rebuild that tree.
"""

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import ApiType, TelemetryConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import (
    applied_ids,
    apply_unset_changes,
    discard_pending_nvue_revision,
    expected_interface_labels_after_unset,
    expected_stats_group_platform_stats_classes_after_unset_pre114,
    expected_stats_group_platform_stats_classes_after_unset_test15b,
    expected_stats_group_platform_stats_classes_after_unset_v114,
    get_cumulus_version_tuple,
    get_interface_telemetry_labels_applied,
    get_system_interface_stats_classes_applied,
    get_system_interface_stats_egress_buffer_applied,
    get_system_interface_stats_egress_tc_applied,
    get_system_interface_stats_ingress_buffer_applied,
    get_system_interface_stats_switch_priority_applied,
    expected_system_labels_after_unset,
    get_interface_histogram_counter_applied,
    get_interface_histogram_egress_tc_applied,
    get_interface_histogram_ingress_pg_applied,
    get_interface_histogram_latency_tc_applied,
    get_interface_telemetry_labels_applied,
    get_system_interface_stats_class_phy_applied,
    get_system_interface_stats_egress_tc_applied,
    get_system_interface_stats_ingress_pg_applied,
    get_system_otlp_destination_applied,
    get_system_platform_stats_class_applied,
    get_system_platform_stats_class_platform_info_applied,
    get_system_platform_stats_class_transceiver_info_applied,
    get_system_platform_stats_classes_applied,
    get_system_platform_stats_export_applied,
    get_system_telemetry_adaptive_routing_stats_applied,
    get_system_telemetry_adaptive_routing_stats_export_applied,
    get_system_telemetry_buffer_stats_applied,
    get_system_telemetry_buffer_stats_export_applied,
    get_system_telemetry_control_plane_stats_applied,
    get_system_telemetry_control_plane_stats_export_applied,
    get_system_telemetry_control_plane_stats_sample_interval_applied,
    get_system_telemetry_export_otlp_applied,
    get_system_telemetry_export_otlp_grpc_applied,
    get_system_telemetry_histogram_applied,
    get_system_telemetry_histogram_export_applied,
    get_system_telemetry_interface_stats_applied,
    get_system_telemetry_interface_stats_export_applied,
    get_system_telemetry_interface_stats_sample_interval_applied,
    get_system_telemetry_lldp_applied,
    get_system_telemetry_lldp_export_applied,
    get_system_telemetry_labels_applied,
    get_system_telemetry_platform_stats_applied,
    get_system_telemetry_platform_stats_export_applied,
    get_system_telemetry_software_stats_applied,
    get_system_telemetry_software_stats_systemd_applied,
    get_system_telemetry_software_stats_systemd_export_applied,
    get_system_telemetry_software_stats_systemd_unit_profile_applied,
    get_system_telemetry_stats_group_buffer_stats_export_applied,
    get_system_telemetry_stats_group_router_applied,
    get_system_telemetry_stats_group_router_export_applied,
    get_system_telemetry_stats_groups_applied,
    platform_stats_classes_for_unset,
    require_cumulus_at_least,
    require_cumulus_at_most,
    require_cumulus_equal,
    restore_otel_cli_coverage_golden,
    set_system_telemetry_histogram,
    swp_interfaces_for_unset,
    unset_interface_histogram_counter,
    unset_interface_histogram_egress_tc,
    unset_interface_histogram_ingress_pg,
    unset_interface_histogram_latency_tc,
    unset_interface_label,
    unset_system_interface_stats_class_phy,
    unset_system_interface_stats_classes,
    unset_system_interface_stats_egress_buffer,
    unset_system_interface_stats_ingress_buffer,
    unset_system_interface_stats_switch_priority,
    unset_system_otlp_destination_port,
    unset_system_otlp_destination_stats_group,
    unset_system_platform_stats_class,
    unset_system_platform_stats_class_platform_info,
    unset_system_platform_stats_class_transceiver_info,
    unset_system_platform_stats_classes,
    unset_system_platform_stats_export,
    unset_system_telemetry_adaptive_routing_stats,
    unset_system_telemetry_adaptive_routing_stats_export,
    unset_system_telemetry_buffer_stats,
    unset_system_telemetry_buffer_stats_export,
    unset_system_telemetry_control_plane_stats,
    unset_system_telemetry_control_plane_stats_export,
    unset_system_telemetry_control_plane_stats_sample_interval,
    unset_system_telemetry_export_otlp,
    unset_system_telemetry_export_otlp_grpc,
    unset_system_telemetry_histogram,
    unset_system_telemetry_histogram_export,
    unset_system_telemetry_interface_stats,
    unset_system_telemetry_interface_stats_egress_tc,
    unset_system_telemetry_interface_stats_export,
    unset_system_telemetry_interface_stats_ingress_pg,
    unset_system_telemetry_interface_stats_sample_interval,
    unset_system_telemetry_label,
    unset_system_telemetry_lldp,
    unset_system_telemetry_lldp_export,
    unset_system_telemetry_platform_stats,
    unset_system_telemetry_platform_stats_export,
    unset_system_telemetry_software_stats,
    unset_system_telemetry_software_stats_systemd,
    unset_system_telemetry_software_stats_systemd_export,
    unset_system_telemetry_software_stats_systemd_unit_profile,
    unset_system_telemetry_software_stats_systemd_unit_profile_unit,
    unset_system_telemetry_stats_group,
    unset_system_telemetry_stats_group_buffer_stats_export,
    unset_system_telemetry_stats_group_router,
    unset_system_telemetry_stats_group_router_export,
    validate_unset_stats_group_platform_stats,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.otel,
    pytest.mark.otel_unset,
    pytest.mark.skip_clear_config,
]


@ pytest.fixture(autouse=True)
def _require_cli_coverage_baseline(otel_cli_coverage_baseline):
    """Module baseline from ``OtelMgmtVrfNoTLSConfig`` (see conftest)."""
    return otel_cli_coverage_baseline


def _unset_test_skipped(item) -> bool:
    """True when setup or the test body skipped (version gate or marker).

    ``ngts/conftest.py`` stores ``rep_setup`` / ``rep_call`` before fixture teardown.
    """
    for phase in ("setup", "call"):
        report = getattr(item, f"rep_{phase}", None)
        if report is not None and report.skipped:
            return True
    return False


@pytest.fixture(autouse=True)
def _restore_cli_baseline_after_unset_tests(request, engines):
    """Roll a system unset back to the module golden revision.

    The baseline is applied once. Replaying it after every test (interface
    histogram, labels, OTLP, and the telemetry tree) is what added about an hour.

    Interface tests (``otel_unset_interface_baseline``) only drop a pending
    candidate. test01a, test01b, and test01c/test02 unset disjoint interface
    nodes, and no system test reads that tree back.

    A skipped test has not unset anything, so it does not pay ``nv config apply``.
    ``skip_clear_config`` means the last system-test rollback is what leaves a
    known telemetry tree; module teardown then removes the coverage labels.
    """
    yield
    if _unset_test_skipped(request.node) or request.node.get_closest_marker(
        "otel_unset_interface_baseline"
    ):
        discard_pending_nvue_revision(engines.dut)
        return
    restore_otel_cli_coverage_golden(engines.dut)


@pytest.mark.cumulus_only
@pytest.mark.otel_unset_interface_baseline
def test01a_otlp_interface_unset_cli_validations(engines, random_api):
    """SSIM test01a: unset interface histogram ingress PG and egress TC."""
    require_cumulus_at_least(engines.dut, "5.10.0")
    ifaces = swp_interfaces_for_unset(engines.dut)
    for iface in ifaces:
        for pg in ("0", "1", "2"):
            unset_interface_histogram_ingress_pg(engines.dut, iface, pg)
        for tc in ("0", "1", "6", "7"):
            unset_interface_histogram_egress_tc(engines.dut, iface, tc)
    apply_unset_changes(engines.dut)

    # Baseline configures priority-group 0-7 and egress traffic-class 0-7 per interface.
    for iface in ifaces:
        pgs = applied_ids(get_interface_histogram_ingress_pg_applied(engines.dut, iface))
        assert pgs == {"3", "4", "5", "6", "7"}, (
            f"nv unset interface {iface} telemetry histogram ingress-buffer "
            f"priority-group failed: {sorted(pgs)}"
        )
        tcs = applied_ids(get_interface_histogram_egress_tc_applied(engines.dut, iface))
        assert tcs == {"2", "3", "4", "5"}, (
            f"nv unset interface {iface} telemetry histogram egress-buffer "
            f"traffic-class failed: {sorted(tcs)}"
        )


@pytest.mark.cumulus_only
@pytest.mark.otel_unset_interface_baseline
def test01b_otlp_interface_unset_cli_validations(engines, random_api):
    """SSIM test01b: unset interface histogram latency TC and counter."""
    require_cumulus_at_least(engines.dut, "5.11.0")
    ifaces = swp_interfaces_for_unset(engines.dut)
    for iface in ifaces:
        for tc in ("0", "1", "2", "3", "4", "7"):
            unset_interface_histogram_latency_tc(engines.dut, iface, tc)
        unset_interface_histogram_counter(engines.dut, iface, "rx-byte")
    apply_unset_changes(engines.dut)

    # Baseline configures latency traffic-class 0-7 and counters rx-byte + crc.
    for iface in ifaces:
        tcs = applied_ids(get_interface_histogram_latency_tc_applied(engines.dut, iface))
        assert tcs == {"5", "6"}, (
            f"nv unset interface {iface} telemetry histogram latency traffic-class "
            f"failed: {sorted(tcs)}"
        )
        counters = applied_ids(get_interface_histogram_counter_applied(engines.dut, iface))
        assert counters == {"crc"}, (
            f"nv unset interface {iface} telemetry histogram counter counter-type "
            f"failed: {sorted(counters)}"
        )


@pytest.mark.otel_unset_interface_baseline
def test01c_otlp_interface_unset_cli_validations(engines, random_api):
    """SSIM test01c: unset interface telemetry labels l1–l4."""
    require_cumulus_at_least(engines.dut, "5.12.0")
    labels = ("mgmtVrfNoTls-l1", "mgmtVrfNoTls-l2", "mgmtVrfNoTls-l3", "mgmtVrfNoTls-l4")
    ifaces = swp_interfaces_for_unset(engines.dut)
    for iface in ifaces:
        for label in labels:
            unset_interface_label(engines.dut, iface, label)
    apply_unset_changes(engines.dut)

    expected = expected_interface_labels_after_unset(labels)
    for iface in ifaces:
        out = get_interface_telemetry_labels_applied(engines.dut, iface)
        assert out == expected, (
            f"nv unset interface {iface} telemetry label failed: {out!r}"
        )


@pytest.mark.otel_unset_interface_baseline
def test02_otlp_interface_unset_cli_validations(engines, random_api):
    """SSIM test02: unset label description then label; verify l5–l10 remain."""
    require_cumulus_at_least(engines.dut, "5.12.0")
    labels = ("mgmtVrfNoTls-l1", "mgmtVrfNoTls-l2", "mgmtVrfNoTls-l3", "mgmtVrfNoTls-l4")
    ifaces = swp_interfaces_for_unset(engines.dut)
    for iface in ifaces:
        for label in labels:
            unset_interface_label(engines.dut, iface, label, key="description")
            unset_interface_label(engines.dut, iface, label)
    apply_unset_changes(engines.dut)

    expected = {
        f"mgmtVrfNoTls-l{i}": {"description": f"Management VRF Insecure Label-{i}"}
        for i in range(5, 11)
    }
    out = get_interface_telemetry_labels_applied(engines.dut, ifaces[0])
    assert out == expected, "nv unset interface telemetry label failed"


@pytest.mark.cumulus_only
def test03a_otlp_system_unset_cli_validations(engines, otel_cli_coverage_baseline, random_api):
    """SSIM test03a: unset system interface-stats TC/PG and the last destination's port."""
    require_cumulus_at_least(engines.dut, "5.10.0")
    unset_tc = ("0", "1", "4")
    for tc in unset_tc:
        unset_system_telemetry_interface_stats_egress_tc(engines.dut, tc)
        apply_unset_changes(engines.dut)
        out = get_system_interface_stats_egress_tc_applied(engines.dut)
        assert str(tc) not in out, (
            "nv unset system telemetry interface-stats egress-buffer traffic-class failed"
        )
    destination_id = otel_cli_coverage_baseline.collector_ips[-1]
    unset_system_otlp_destination_port(engines.dut, destination_id)
    unset_pg = ("0", "5", "6", "7")
    for pg in unset_pg:
        unset_system_telemetry_interface_stats_ingress_pg(engines.dut, pg)
    apply_unset_changes(engines.dut)

    remaining_pg = applied_ids(get_system_interface_stats_ingress_pg_applied(engines.dut))
    assert not remaining_pg & set(unset_pg), (
        "nv unset system telemetry interface-stats ingress-buffer priority-group "
        f"failed: {sorted(remaining_pg)}"
    )
    # The baseline binds the last destination to the default gRPC port, so the unset shows up
    # as the port being the default rather than as a change of value.
    out = get_system_otlp_destination_applied(engines.dut, destination_id)
    port = out.get(TelemetryConsts.PORT)
    assert port in (None, TelemetryConsts.Defaults.GRPC_PORT), (
        f"nv unset system telemetry export otlp grpc destination {destination_id} port "
        f"failed: {port!r}"
    )


def test03b_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test03b: unset system telemetry labels device-l1..l4."""
    require_cumulus_at_least(engines.dut, "5.11.0")
    labels = ("device-l1", "device-l2", "device-l3", "device-l4")
    for label in labels:
        unset_system_telemetry_label(engines.dut, label)
    apply_unset_changes(engines.dut)
    out = get_system_telemetry_labels_applied(engines.dut)
    assert out == expected_system_labels_after_unset(labels), (
        f"nv unset system telemetry label failed: {out!r}"
    )


def test03c_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test03c: unset interface-stats class phy state; unset device labels."""
    require_cumulus_at_least(engines.dut, "5.13.0")
    labels = ("device-l1", "device-l2", "device-l3", "device-l4")
    unset_system_interface_stats_class_phy(engines.dut, key="state", value="enabled")
    for label in labels:
        unset_system_telemetry_label(engines.dut, label)
    apply_unset_changes(engines.dut)
    out = get_system_interface_stats_class_phy_applied(engines.dut)
    assert out.get(TelemetryConsts.STATE) == TelemetryConsts.Defaults.STATS_STATE, (
        f"nv unset system telemetry interface-stats class phy state failed: {out!r}"
    )
    out = get_system_telemetry_labels_applied(engines.dut)
    assert out == expected_system_labels_after_unset(labels), (
        f"nv unset system telemetry label failed: {out!r}"
    )


def test04a_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test04a: unset system telemetry interface-stats class phy."""
    require_cumulus_at_least(engines.dut, "5.13.0")
    unset_system_interface_stats_class_phy(engines.dut)
    apply_unset_changes(engines.dut)
    out = get_system_interface_stats_class_phy_applied(engines.dut)
    assert out.get(TelemetryConsts.STATE) == TelemetryConsts.Defaults.STATS_STATE, (
        f"nv unset system telemetry interface-stats class phy failed: {out!r}"
    )


def test04b_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test04b: unset system telemetry label description then label."""
    require_cumulus_at_least(engines.dut, "5.12.0")
    labels = ("device-l1", "device-l2", "device-l3", "device-l4")
    for label in labels:
        unset_system_telemetry_label(engines.dut, label, key="description")
        unset_system_telemetry_label(engines.dut, label)
    apply_unset_changes(engines.dut)
    out = get_system_telemetry_labels_applied(engines.dut)
    assert out == expected_system_labels_after_unset(labels), (
        f"nv unset system telemetry label description then label failed: {out!r}"
    )


def test05_otlp_system_unset_cli_validations(engines, otel_cli_coverage_baseline, random_api):
    """SSIM test05: unset platform-stats classes (per-platform class set)."""
    require_cumulus_at_least(engines.dut, "5.11.0")
    platform_classes = platform_stats_classes_for_unset(
        otel_cli_coverage_baseline.is_nvos
    )
    for platform_class in platform_classes:
        unset_system_platform_stats_class(
            engines.dut, platform_class, key="sample-interval", value=""
        )
        unset_system_platform_stats_class(
            engines.dut, platform_class, key="state", value=""
        )
    apply_unset_changes(engines.dut)

    # Platform-stats classes are the one family whose state defaults to enabled.
    for platform_class in platform_classes:
        out = get_system_platform_stats_class_applied(engines.dut, platform_class)
        assert out.get(TelemetryConsts.STATE) == TelemetryConsts.Defaults.PLATFORM_CLASS_STATE, (
            "nv unset system telemetry platform-stats class "
            f"{platform_class} state failed: {out!r}"
        )


def test05a_otlp_system_unset_cli_validations(engines, otel_cli_coverage_baseline, random_api):
    """SSIM test05a: unset platform-info sample-interval, state, then class.

    NVOS keeps the platform-stats sample-interval default on ``platform-stats export``,
    not per class, so unset of ``sample-interval`` leaves nothing rendered on the class
    (vs the rendered ``sample-interval 60`` on Cumulus). Unset of ``state`` reverts to the
    default ``enabled`` on both platforms, so the fully-unset class still renders
    ``state enabled`` (NVOS) or ``sample-interval 60`` + ``state enabled`` (Cumulus).
    """
    is_nvos = otel_cli_coverage_baseline.is_nvos
    require_cumulus_at_least(engines.dut, "5.14.0")
    unset_system_platform_stats_class_platform_info(
        engines.dut, key="sample-interval", value=""
    )
    apply_unset_changes(engines.dut)
    out = get_system_platform_stats_class_platform_info_applied(engines.dut)
    if is_nvos:
        assert "sample-interval" not in out, (
            "nv unset system telemetry platform-stats class platform-info sample-interval failed"
        )
    else:
        assert out.get("sample-interval") == 60, (
            "nv unset system telemetry platform-stats class platform-info sample-interval failed"
        )
    unset_system_platform_stats_class_platform_info(engines.dut, key="state", value="")
    apply_unset_changes(engines.dut)
    out = get_system_platform_stats_class_platform_info_applied(engines.dut)
    assert out.get("state") == "enabled", (
        "nv unset system telemetry platform-stats class platform-info state failed"
    )
    unset_system_platform_stats_class_platform_info(engines.dut)
    apply_unset_changes(engines.dut)
    out = get_system_platform_stats_class_platform_info_applied(engines.dut)
    expected = {"state": "enabled"} if is_nvos else {"sample-interval": 60, "state": "enabled"}
    assert out == expected, (
        "nv unset system telemetry platform-stats class platform-info failed"
    )


def test06a_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test06a: unset system telemetry interface-stats export state.

    ``nv unset system telemetry interface-stats export state`` → applied state disabled.
    """
    require_cumulus_at_least(engines.dut, "5.10.0")
    unset_system_telemetry_interface_stats_export(engines.dut, key="state", value="")
    apply_unset_changes(engines.dut)
    out = get_system_telemetry_interface_stats_export_applied(engines.dut)
    assert out.get("state") == "disabled", (
        "nv unset system telemetry interface-stats export state failed"
    )


# NVOS has no control-plane-stats family (absent from TelemetryConsts.ALL_STATS_SUBTREES).
@pytest.mark.cumulus_only
def test06b_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test06b: unset system telemetry control-plane-stats export state.

    ``nv unset system telemetry control-plane-stats export state`` → applied state disabled.

    Cumulus-only: control-plane-stats has no NVOS equivalent (not in ALL_STATS_SUBTREES).
    """
    require_cumulus_at_least(engines.dut, "5.11.0")
    unset_system_telemetry_control_plane_stats_export(engines.dut, key="state", value="")
    apply_unset_changes(engines.dut)
    out = get_system_telemetry_control_plane_stats_export_applied(engines.dut)
    assert out.get("state") == "disabled", (
        "nv unset system telemetry control-plane-stats export state failed"
    )


# NVOS has no buffer-stats family (absent from TelemetryConsts.ALL_STATS_SUBTREES).
@pytest.mark.cumulus_only
def test06c_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test06c: unset system telemetry buffer-stats export state and sample-interval.

    ``nv unset system telemetry buffer-stats export state`` → applied state disabled.
    ``nv unset system telemetry buffer-stats sample-interval`` → applied sample-interval 1.

    Cumulus-only: buffer-stats has no NVOS equivalent (not in ALL_STATS_SUBTREES).
    """
    require_cumulus_at_least(engines.dut, "5.12.0")
    unset_system_telemetry_buffer_stats_export(engines.dut, key="state", value="")
    apply_unset_changes(engines.dut)
    out = get_system_telemetry_buffer_stats_export_applied(engines.dut)
    assert out.get("state") == "disabled", (
        "nv unset system telemetry buffer-stats export state failed"
    )

    unset_system_telemetry_buffer_stats(engines.dut, key="sample-interval", value="")
    apply_unset_changes(engines.dut)
    out = get_system_telemetry_buffer_stats_applied(engines.dut)
    assert out.get("sample-interval") == 1, (
        "nv unset system telemetry buffer-stats sample-interval failed"
    )


def _unset_platform_stats_root_and_verify(engines, *, include_platform_info: bool) -> None:
    unset_system_platform_stats_export(engines.dut, key="state")
    apply_unset_changes(engines.dut)
    out = get_system_platform_stats_export_applied(engines.dut)
    assert out.get("state") == "disabled", (
        "nv unset system telemetry platform-stats export state failed"
    )

    unset_system_platform_stats_export(engines.dut, key="sample-interval")
    apply_unset_changes(engines.dut)
    out = get_system_platform_stats_export_applied(engines.dut)
    assert out.get("sample-interval") == 60, (
        "nv unset system telemetry platform-stats export sample-interval failed"
    )

    unset_system_platform_stats_classes(engines.dut)
    apply_unset_changes(engines.dut)
    out = get_system_platform_stats_classes_applied(engines.dut)
    expected_classes = (
        "cpu",
        "disk",
        "environment-sensor",
        "file-system",
        "memory",
        "transceiver-info",
    )
    if include_platform_info:
        expected_classes = expected_classes[:-1] + ("platform-info", "transceiver-info")
    expected = {
        platform_class: {"sample-interval": 60, "state": "enabled"}
        for platform_class in expected_classes
    }
    # CL 5.18+ schema default: asic-resource class (sample-interval 10, not 60).
    if get_cumulus_version_tuple(engines.dut) >= (5, 18):
        expected["asic-resource"] = {"sample-interval": 10, "state": "enabled"}
    assert out == expected, (
        "nv unset system telemetry platform-stats class failed: "
        f"actual={out}, expected={expected}"
    )


@pytest.mark.cumulus_only
@ pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test07_otlp_system_unset_cli_validations(engines, test_api):
    """SSIM test07: unset every configured platform-stats subclass."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.11.0")
    with allure.step("Unset every configured platform-stats subclass"):
        for platform_class in ("cpu", "disk", "environment-sensor", "file-system", "memory"):
            unset_system_platform_stats_class(engines.dut, platform_class)
        apply_unset_changes(engines.dut)


@pytest.mark.cumulus_only
@ pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test08_otlp_system_unset_cli_validations(engines, test_api):
    """SSIM test08 (CL 5.11–5.13): unset platform-stats export and class roots."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.11.0")
    require_cumulus_at_most(engines.dut, "5.13.0")
    with allure.step("Unset platform-stats export and class roots"):
        _unset_platform_stats_root_and_verify(engines, include_platform_info=False)


@pytest.mark.cumulus_only
@ pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test08a_otlp_system_unset_cli_validations(engines, test_api):
    """SSIM test08a (CL 5.14+): platform-info is part of default class output."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.14.0")
    with allure.step("Unset platform-stats export and class roots with platform-info"):
        _unset_platform_stats_root_and_verify(engines, include_platform_info=True)


@pytest.mark.cumulus_only
@ pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test09a_otlp_system_unset_cli_validations(engines, test_api):
    """SSIM test09a: unset interface-stats ingress/egress buffer roots."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.10.0")
    with allure.step("Unset interface-stats ingress/egress buffer roots"):
        unset_system_interface_stats_ingress_buffer(engines.dut)
        unset_system_interface_stats_egress_buffer(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify ingress/egress buffer roots are disabled"):
        assert get_system_interface_stats_ingress_buffer_applied(engines.dut) == {
            "priority-group": {}
        }, "nv unset system telemetry interface-stats ingress-buffer failed"
        assert get_system_interface_stats_egress_buffer_applied(engines.dut) == {
            "traffic-class": {}
        }, "nv unset system telemetry interface-stats egress-buffer failed"


@pytest.mark.cumulus_only
@ pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test09b_otlp_system_unset_cli_validations(engines, test_api):
    """SSIM test09b: unset interface-stats switch-priority."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.11.0")
    with allure.step("Unset interface-stats switch-priority"):
        unset_system_interface_stats_switch_priority(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify switch-priority is disabled"):
        assert get_system_interface_stats_switch_priority_applied(engines.dut) == {}, (
            "nv unset system telemetry interface-stats switch-priority failed"
        )


@pytest.mark.cumulus_only
@ pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test09c_otlp_system_unset_cli_validations(engines, test_api):
    """SSIM test09c: unset interface-stats classes and verify version defaults."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.12.0")
    with allure.step("Unset interface-stats classes"):
        unset_system_interface_stats_classes(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify interface-stats classes are disabled"):
        expected = {"phy": {"state": "disabled"}}
        require_debounce = get_cumulus_version_tuple(engines.dut) >= (5, 17)
        if require_debounce:
            expected["debounce"] = {"sample-interval": 10, "state": "disabled"}
        assert get_system_interface_stats_classes_applied(engines.dut) == expected, (
            "nv unset system telemetry interface-stats class failed "
            f"(debounce_default={require_debounce})"
        )


def test10a_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test10a: unset system telemetry interface-stats export (reverts to disabled)."""
    require_cumulus_at_least(engines.dut, "5.10.0")
    with allure.step("Unset system telemetry interface-stats export"):
        unset_system_telemetry_interface_stats_export(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify interface-stats export is disabled"):
        out = get_system_telemetry_interface_stats_export_applied(engines.dut)
        assert out.get("state") == "disabled", (
            "nv unset system telemetry interface-stats export failed"
        )


# NVOS has no control-plane-stats family (absent from TelemetryConsts.ALL_STATS_SUBTREES).
@pytest.mark.cumulus_only
def test10b_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test10b: unset system telemetry platform-stats + control-plane-stats export.

    Cumulus-only: control-plane-stats has no NVOS equivalent (not in ALL_STATS_SUBTREES).
    """
    require_cumulus_at_least(engines.dut, "5.11.0")
    with allure.step("Unset system telemetry platform-stats export"):
        unset_system_telemetry_platform_stats_export(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify platform-stats export is disabled"):
        out = get_system_telemetry_platform_stats_export_applied(engines.dut)
        assert out.get("state") == "disabled", (
            "nv unset system telemetry platform-stats export failed"
        )
    with allure.step("Unset system telemetry control-plane-stats export"):
        unset_system_telemetry_control_plane_stats_export(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify control-plane-stats export is disabled"):
        out = get_system_telemetry_control_plane_stats_export_applied(engines.dut)
        assert out.get("state") == "disabled", (
            "nv unset system telemetry control-plane-stats export failed"
        )


# NVOS has no buffer-stats family (absent from TelemetryConsts.ALL_STATS_SUBTREES).
@pytest.mark.cumulus_only
def test10c_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test10c: unset system telemetry buffer-stats export.

    Cumulus-only: buffer-stats has no NVOS equivalent (not in ALL_STATS_SUBTREES).
    """
    require_cumulus_at_least(engines.dut, "5.12.0")
    with allure.step("Unset system telemetry buffer-stats export"):
        unset_system_telemetry_buffer_stats_export(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify buffer-stats export is disabled"):
        out = get_system_telemetry_buffer_stats_export_applied(engines.dut)
        assert out.get("state") == "disabled", (
            "nv unset system telemetry buffer-stats export failed"
        )


def test11a_otlp_interface_unset_cli_validations(engines, otel_cli_coverage_baseline, random_api):
    """
    SSIM test11a: unset system telemetry interface-stats sample-interval
    nv unset system telemetry interface-stats sample-interval
    """
    require_cumulus_at_least(engines.dut, "5.10.0")
    with allure.step("Unset system telemetry interface-stats sample-interval"):
        unset_system_telemetry_interface_stats_sample_interval(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify interface-stats sample-interval is 1"):
        out = get_system_telemetry_interface_stats_sample_interval_applied(engines.dut)
        assert out['sample-interval'] == 1, (
            "nv unset system telemetry interface-stats sample-interval failed"
        )


@pytest.mark.cumulus_only
def test11b_otlp_system_unset_cli_validations(engines, otel_cli_coverage_baseline, random_api):
    """
    SSIM test11b: unset system telemetry control-plane-stats sample-interval
    nv unset system telemetry control-plane-stats sample-interval
    """
    require_cumulus_at_least(engines.dut, "5.11.0")
    with allure.step("Unset system telemetry control-plane-stats sample-interval"):
        unset_system_telemetry_control_plane_stats_sample_interval(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify control-plane-stats sample-interval is 1"):
        out = get_system_telemetry_control_plane_stats_sample_interval_applied(engines.dut)
        assert out['sample-interval'] == 1, (
            "nv unset system telemetry control-plane-stats sample-interval failed"
        )


def test12a_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test12a: unset system telemetry interface-stats (export reverts to disabled)."""
    require_cumulus_at_least(engines.dut, "5.10.0")
    with allure.step("Unset system telemetry interface-stats"):
        unset_system_telemetry_interface_stats(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify interface-stats export is disabled"):
        out = get_system_telemetry_interface_stats_applied(engines.dut)
        assert out.get("export", {}).get("state") == "disabled", (
            "nv unset system telemetry interface-stats failed"
        )


# NVOS has no control-plane-stats family (absent from TelemetryConsts.ALL_STATS_SUBTREES).
@pytest.mark.cumulus_only
def test12b_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test12b: unset control-plane-stats and platform-stats (export reverts to disabled).

    Cumulus note: control-plane-stats has no NVOS equivalent (not in ALL_STATS_SUBTREES).
    """
    require_cumulus_at_least(engines.dut, "5.11.0")
    with allure.step("Unset system telemetry control-plane-stats"):
        unset_system_telemetry_control_plane_stats(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify control-plane-stats export is disabled"):
        out = get_system_telemetry_control_plane_stats_applied(engines.dut)
        assert out.get("export", {}).get("state") == "disabled", (
            "nv unset system telemetry control-plane-stats failed"
        )
    with allure.step("Unset system telemetry platform-stats"):
        unset_system_telemetry_platform_stats(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify platform-stats export is disabled"):
        out = get_system_telemetry_platform_stats_applied(engines.dut)
        assert out.get("export", {}).get("state") == "disabled", (
            "nv unset system telemetry platform-stats failed"
        )


@pytest.mark.cumulus_only
def test12c_otlp_system_unset_cli_validations(engines, otel_cli_coverage_baseline, random_api):
    """
    SSIM test12c: unset system telemetry buffer-stats.
    nv unset system telemetry buffer-stats
    """
    require_cumulus_at_least(engines.dut, "5.12.0")
    with allure.step("Unset system telemetry buffer-stats"):
        unset_system_telemetry_buffer_stats(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify buffer-stats export is disabled"):
        out = get_system_telemetry_buffer_stats_applied(engines.dut)
        assert out['export']['state'] == 'disabled', (
            "nv unset system telemetry buffer-stats failed"
        )


def test13a_otlp_system_unset_cli_validations(engines, otel_cli_coverage_baseline, random_api):
    """
    SSIM test13a: unset system telemetry export otlp grpc
    nv unset system telemetry export otlp grpc
    """
    require_cumulus_at_least(engines.dut, "5.10.0")
    with allure.step("Unset system telemetry export otlp grpc"):
        unset_system_telemetry_export_otlp_grpc(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify export otlp grpc is disabled"):
        out = get_system_telemetry_export_otlp_grpc_applied(engines.dut)
        assert out == {'destination': {}, 'insecure': 'disabled', 'port': 4317}, (
            "nv unset system telemetry export otlp grpc failed"
        )


def test13b_otlp_system_unset_cli_validations(engines, otel_cli_coverage_baseline, random_api):
    """SSIM test13b: unset system telemetry export otlp state
    nv unset system telemetry export otlp state
    """
    require_cumulus_at_least(engines.dut, "5.13.0")
    with allure.step("Unset system telemetry export otlp state"):
        unset_system_telemetry_export_otlp(engines.dut, key="state", value="")
        apply_unset_changes(engines.dut)
    with allure.step("Verify export otlp state is disabled"):
        out = get_system_telemetry_export_otlp_applied(engines.dut)
        assert out.get(TelemetryConsts.STATE) == TelemetryConsts.Defaults.OTLP_STATE, (
            "nv unset system telemetry export otlp state failed"
        )


@pytest.mark.cumulus_only
def test14_otlp_system_unset_cli_validations(engines, otel_cli_coverage_baseline, random_api):
    """
    SSIM test14: unset software-stats and verify systemd export is disabled.
    nv unset system telemetry software-stats
    """
    require_cumulus_at_least(engines.dut, "5.13.0")
    with allure.step("Unset system telemetry software-stats"):
        unset_system_telemetry_software_stats(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify systemd export is disabled"):
        out = get_system_telemetry_software_stats_applied(engines.dut)
        assert out.get("systemd", {}).get("export", {}).get("state") == "disabled", (
            "nv unset system telemetry software-stats failed: "
            f"actual={out!r}, expected systemd.export.state='disabled'"
        )


# Pins a bounded Cumulus release window (5.12-5.13); NVOS has no analogue.
@pytest.mark.cumulus_only
def test15b_otlp_system_unset_cli_validations(engines, otel_cli_coverage_baseline, random_api):
    """SSIM test15b: unset stats-group platform-stats class and export sample-interval.
    nv unset system telemetry stats-group <stats-group-id> platform-stats class
    nv unset system telemetry stats-group <stats-group-id> platform-stats export sample-interval
    """
    require_cumulus_at_least(engines.dut, "5.12.0", "5.13.0")
    with allure.step("Unset stats-group platform-stats class and export sample-interval"):
        validate_unset_stats_group_platform_stats(
            engines.dut,
            CumulusOtelConst.TEST01_STATS_GROUP_ID,
            expected_stats_group_platform_stats_classes_after_unset_test15b(),
        )


# Pins a bounded Cumulus release window (5.14-5.17); NVOS has no analogue.
@pytest.mark.cumulus_only
def test15c_otlp_system_unset_cli_validations(engines, otel_cli_coverage_baseline, random_api):
    """SSIM test15c: unset stats-group platform-stats on CL 5.14–5.17 (pre-NVUE 1.14).

    nv unset system telemetry stats-group <stats-group-id> platform-stats class
    nv unset system telemetry stats-group <stats-group-id> platform-stats export sample-interval
    """
    require_cumulus_at_least(engines.dut, "5.14.0", "5.17.0")
    with allure.step("Unset stats-group platform-stats class and export sample-interval"):
        validate_unset_stats_group_platform_stats(
            engines.dut,
            CumulusOtelConst.TEST01_STATS_GROUP_ID,
            expected_stats_group_platform_stats_classes_after_unset_pre114(),
        )


def test15c_otlp_system_unset_cli_validations_v518(
    engines, otel_cli_coverage_baseline, random_api
):
    """SSIM test15c v518: unset stats-group platform-stats on CL 5.18+ (NVUE 1.14+).

    Same coverage as test15c, but expects the additional ``asic-resource`` row restored
    when the class is unset. NVOS has no ``asic-resource`` class, so it is not expected there.
    """
    require_cumulus_at_least(engines.dut, "5.18.0")
    with allure.step("Unset stats-group platform-stats class and export sample-interval"):
        validate_unset_stats_group_platform_stats(
            engines.dut,
            CumulusOtelConst.TEST01_STATS_GROUP_ID,
            expected_stats_group_platform_stats_classes_after_unset_v114(),)


@pytest.mark.cumulus_only
def test16a_otlp_system_unset_cli_validations(
    engines, otel_cli_coverage_baseline, random_api
):
    """SSIM test16a: unset stats-group router export and sample-interval.

    nv unset system telemetry stats-group <stats-group-id> router export
    nv unset system telemetry stats-group <stats-group-id> router sample-interval
    """
    require_cumulus_at_least(engines.dut, "5.12.0")
    stats_group_id = CumulusOtelConst.TEST01_STATS_GROUP_ID
    with allure.step("Unset stats-group router export and sample-interval"):

        unset_system_telemetry_stats_group_router_export(engines.dut, stats_group_id)
        apply_unset_changes(engines.dut)
    with allure.step("Verify router export is disabled"):
        out = get_system_telemetry_stats_group_router_export_applied(
            engines.dut, stats_group_id
        )
        assert out.get(TelemetryConsts.STATE) == "disabled", (
            "nv unset system telemetry stats-group "
            f"{stats_group_id} router export failed: {out!r}"
        )

    with allure.step("Unset stats-group router"):
        unset_system_telemetry_stats_group_router(engines.dut, stats_group_id)
        apply_unset_changes(engines.dut)
    with allure.step("Verify router sample-interval is 30"):
        out = get_system_telemetry_stats_group_router_applied(
            engines.dut, stats_group_id
        )
        assert out.get(TelemetryConsts.SAMPLE_INTERVAL) == 30, (
            "nv unset system telemetry stats-group "
            f"{stats_group_id} router sample-interval failed: {out!r}"
        )


@pytest.mark.cumulus_only
def test16b_otlp_system_unset_cli_validations(
    engines, otel_cli_coverage_baseline, random_api
):
    """SSIM test16b: unset stats-group buffer-stats export.

    nv unset system telemetry stats-group <stats-group-id> buffer-stats export
    """
    require_cumulus_at_least(engines.dut, "5.12.0")
    stats_group_id = CumulusOtelConst.TEST01_STATS_GROUP_ID
    with allure.step("Unset stats-group buffer-stats export"):
        unset_system_telemetry_stats_group_buffer_stats_export(
            engines.dut, stats_group_id
        )
        apply_unset_changes(engines.dut)
    with allure.step("Verify buffer-stats export is disabled"):
        out = get_system_telemetry_stats_group_buffer_stats_export_applied(
            engines.dut, stats_group_id
        )
        assert out.get(TelemetryConsts.STATE) == "disabled", (
            "nv unset system telemetry stats-group "
            f"{stats_group_id} buffer-stats export failed: {out!r}"
        )


@pytest.mark.cumulus_only
@ pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test15d_otlp_system_unset_stats_group_cli_validations(engines, test_api):
    """SSIM test15a stats-group delete: unbind OTLP destination stats-group, then unset sg_01.

    Distinct from ``test15a_otlp_system_unset_cli_validations``, which only unbinds the
    destination. test13a clears gRPC, and the golden-revision rollback puts it back
    before the next test.
    nv unset system telemetry export otlp grpc destination <destination-id> stats-group
    nv unset system telemetry stats-group <stats-group-id>
    Each system test rolls back to the module golden revision, so removing ``sg_01``
    does not leak. This test still runs after test15b/test15c to match SSIM order.
    """
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.12.0")
    destinations = get_system_telemetry_export_otlp_grpc_applied(engines.dut).get(
        "destination", {}
    )
    destination_ids = list(destinations) if isinstance(destinations, dict) else []
    stats_group_id = CumulusOtelConst.TEST01_STATS_GROUP_ID
    with allure.step("Unset OTLP destination stats-group binding and stats-group sg_01"):
        for destination_id in destination_ids:
            unset_system_otlp_destination_stats_group(engines.dut, destination_id)
        apply_unset_changes(engines.dut)
    with allure.step("Verify OTLP destination stats-group binding is disabled"):
        for destination_id in destination_ids:
            out = get_system_otlp_destination_applied(engines.dut, destination_id)
            assert not out.get(TelemetryConsts.STATS_GROUP), (
                "nv unset system telemetry export otlp grpc destination "
                f"{destination_id} stats-group failed"
            )
    with allure.step("Unset stats-group sg_01"):
        unset_system_telemetry_stats_group(engines.dut, stats_group_id)
        apply_unset_changes(engines.dut)
    with allure.step("Verify stats-group sg_01 is disabled"):
        out = get_system_telemetry_stats_groups_applied(engines.dut)
        assert stats_group_id not in out, (
            f"nv unset system telemetry stats-group {stats_group_id} failed"
        )


@pytest.mark.cumulus_only
def test17a_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test17a: unset system telemetry software-stats systemd (whole subtree).

    Cumulus-only: software-stats systemd has no NVOS equivalent.
    """
    require_cumulus_at_least(engines.dut, "5.13.0")
    with allure.step("Unset system telemetry software-stats systemd"):
        unset_system_telemetry_software_stats_systemd(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify systemd export is disabled"):
        out = get_system_telemetry_software_stats_systemd_applied(engines.dut)
        assert out.get("export", {}).get("state") == "disabled", (
            "nv unset system telemetry software-stats systemd failed"
        )


@pytest.mark.cumulus_only
def test17b_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test17b: unset software-stats systemd export/active-profile/process-level/
    sample-interval/unit-profile (each reverts to its default).

    Cumulus-only: software-stats systemd has no NVOS equivalent.
    """
    require_cumulus_at_least(engines.dut, "5.13.0")

    with allure.step("Unset system telemetry software-stats systemd export"):
        unset_system_telemetry_software_stats_systemd_export(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify systemd export is disabled"):
        out = get_system_telemetry_software_stats_systemd_export_applied(engines.dut)
        assert out.get("state") == "disabled", (
            "nv unset system telemetry software-stats systemd export failed"
        )

    with allure.step("Unset system telemetry software-stats systemd active-profile"):
        unset_system_telemetry_software_stats_systemd(engines.dut, key="active-profile")
        apply_unset_changes(engines.dut)
    with allure.step("Verify active-profile is default"):
        out = get_system_telemetry_software_stats_systemd_applied(engines.dut)
        assert out.get("active-profile") == "default", (
            "nv unset system telemetry software-stats systemd active-profile failed"
        )

    with allure.step("Unset system telemetry software-stats systemd process-level"):
        unset_system_telemetry_software_stats_systemd(engines.dut, key="process-level")
        apply_unset_changes(engines.dut)
    with allure.step("Verify process-level is disabled"):
        out = get_system_telemetry_software_stats_systemd_applied(engines.dut)
        assert out.get("process-level") == "disabled", (
            "nv unset system telemetry software-stats systemd process-level failed"
        )

    with allure.step("Unset system telemetry software-stats systemd sample-interval"):
        unset_system_telemetry_software_stats_systemd(engines.dut, key="sample-interval")
        apply_unset_changes(engines.dut)
    with allure.step("Verify sample-interval is 60"):
        out = get_system_telemetry_software_stats_systemd_applied(engines.dut)
        assert out.get("sample-interval") == 60, (
            "nv unset system telemetry software-stats systemd sample-interval failed"
        )

    with allure.step("Unset system telemetry software-stats systemd unit-profile"):
        unset_system_telemetry_software_stats_systemd_unit_profile(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify unit-profile is default"):
        out = get_system_telemetry_software_stats_systemd_unit_profile_applied(engines.dut)
        assert "custom" not in out and "default" in out, (
            "nv unset system telemetry software-stats systemd unit-profile failed"
        )


@pytest.mark.cumulus_only
def test17c_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test17c: unset software-stats systemd export state and unit-profile <id>.

    Cumulus-only: software-stats systemd has no NVOS equivalent.
    """
    require_cumulus_at_least(engines.dut, "5.13.0")

    with allure.step("Unset system telemetry software-stats systemd export state"):
        unset_system_telemetry_software_stats_systemd_export(engines.dut, key="state")
        apply_unset_changes(engines.dut)
    with allure.step("Verify systemd export state is disabled"):
        out = get_system_telemetry_software_stats_systemd_export_applied(engines.dut)
        assert out.get("state") == "disabled", (
            "nv unset system telemetry software-stats systemd export state failed"
        )

    with allure.step("Unset system telemetry software-stats systemd active-profile"):
        unset_system_telemetry_software_stats_systemd(engines.dut, key="active-profile")
        apply_unset_changes(engines.dut)
    with allure.step("Verify active-profile is default"):
        out = get_system_telemetry_software_stats_systemd_applied(engines.dut)
        assert out.get("active-profile") == "default", (
            "nv unset system telemetry software-stats systemd active-profile failed"
        )
    with allure.step("Unset system telemetry software-stats systemd unit-profile"):
        unset_system_telemetry_software_stats_systemd_unit_profile(
            engines.dut, unit_profile_id="custom"
        )
        apply_unset_changes(engines.dut)
    with allure.step("Verify unit-profile is default"):
        out = get_system_telemetry_software_stats_systemd_unit_profile_applied(engines.dut)
        assert "custom" not in out, (
            "nv unset system telemetry software-stats systemd unit-profile custom failed")


@pytest.mark.cumulus_only
def test17d_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test17d: unset software-stats systemd unit-profile <id> unit.

    Cumulus-only: software-stats systemd has no NVOS equivalent.
    """
    require_cumulus_at_least(engines.dut, "5.13.0")
    with allure.step("Unset system telemetry software-stats systemd unit-profile unit"):
        unset_system_telemetry_software_stats_systemd_unit_profile_unit(
            engines.dut, unit_profile_id="custom"
        )
        apply_unset_changes(engines.dut)
    with allure.step("Verify unit-profile unit is disabled"):
        out = get_system_telemetry_software_stats_systemd_unit_profile_applied(engines.dut)
        assert not out.get("custom", {}).get("unit"), (
            "nv unset system telemetry software-stats systemd unit-profile custom unit failed"
        )


@pytest.mark.cumulus_only
def test18a_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test18a: unset system telemetry platform-stats class transceiver-info.

    Cumulus-only: asserts the Cumulus default ``sample-interval 60`` after full class unset.
    """
    require_cumulus_equal(engines.dut, "5.13.0")
    with allure.step("Unset system telemetry platform-stats class transceiver-info"):
        unset_system_platform_stats_class_transceiver_info(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify sample-interval is 60"):
        out = get_system_platform_stats_class_transceiver_info_applied(engines.dut)
        assert out.get("sample-interval") == 60, (
            "nv unset system telemetry platform-stats class transceiver-info sample-interval failed")


def test18b_otlp_system_unset_cli_validations(engines, otel_cli_coverage_baseline, random_api):
    """SSIM test18b: unset transceiver-info sample-interval and state."""
    is_nvos = otel_cli_coverage_baseline.is_nvos
    require_cumulus_at_least(engines.dut, "5.13.0")
    unset_system_platform_stats_class_transceiver_info(
        engines.dut, key="sample-interval", value=""
    )
    unset_system_platform_stats_class_transceiver_info(engines.dut, key="state", value="")
    apply_unset_changes(engines.dut)
    out = get_system_platform_stats_class_transceiver_info_applied(engines.dut)
    expected = {"state": "enabled"} if is_nvos else {"sample-interval": 60, "state": "enabled"}
    assert out == expected, (
        "nv unset system telemetry platform-stats class transceiver-info sample-interval got failed")


@pytest.mark.cumulus_only
@ pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test19b_otlp_system_unset_cli_validations(engines, test_api):
    """SSIM test19b: unset system telemetry lldp (reverts to disabled + interval 5).

    Cumulus-only: lldp telemetry subtree has no NVOS equivalent.
    """
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.13.0")
    with allure.step("Unset system telemetry lldp"):
        unset_system_telemetry_lldp(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify lldp export is disabled and sample-interval is 5"):
        out = get_system_telemetry_lldp_applied(engines.dut)
        assert out == {"export": {"state": "disabled"}, "sample-interval": 5}, (
            "nv unset system telemetry lldp failed")


@pytest.mark.cumulus_only
@ pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test19c_otlp_system_unset_cli_validations(engines, test_api):
    """SSIM test19c: unset lldp export and sample-interval.

    Cumulus-only: lldp telemetry subtree has no NVOS equivalent.
    """
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.13.0")
    with allure.step("Unset system telemetry lldp export and sample-interval"):
        unset_system_telemetry_lldp_export(engines.dut)
        unset_system_telemetry_lldp(engines.dut, key="sample-interval")
        apply_unset_changes(engines.dut)
    with allure.step("Verify lldp export is disabled and sample-interval is 5"):
        out = get_system_telemetry_lldp_applied(engines.dut)
        assert out.get("export", {}).get("state") == "disabled", (
            "nv unset system telemetry lldp export failed"
        )
        assert out.get("sample-interval") == 5, (
            "nv unset system telemetry lldp sample-interval failed")


@pytest.mark.cumulus_only
@ pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test19d_otlp_system_unset_cli_validations(engines, test_api):
    """SSIM test19d: unset lldp export state → disabled.

    Cumulus-only: lldp telemetry subtree has no NVOS equivalent.
    """
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.13.0")
    with allure.step("Unset system telemetry lldp export state"):
        unset_system_telemetry_lldp_export(engines.dut, key="state")
        apply_unset_changes(engines.dut)
    with allure.step("Verify lldp export state is disabled"):
        out = get_system_telemetry_lldp_export_applied(engines.dut)
        assert out.get("state") == "disabled", (
            "nv unset system telemetry lldp export state failed")


@pytest.mark.cumulus_only
def test20a_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test20a: unset system telemetry adaptive-routing-stats."""
    require_cumulus_equal(engines.dut, "5.13.0")
    with allure.step("Unset system telemetry adaptive-routing-stats"):
        unset_system_telemetry_adaptive_routing_stats(engines.dut)
        apply_unset_changes(engines.dut)
    with allure.step("Verify adaptive-routing-stats export is disabled and sample-interval is 1"):
        out = get_system_telemetry_adaptive_routing_stats_applied(engines.dut)
        assert out.get("export", {}).get("state") == "disabled" and out.get("sample-interval") == 1, (
            "nv unset system telemetry adaptive-routing-stats got failed")


@pytest.mark.cumulus_only
def test20b_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test20b: unset adaptive-routing-stats export and sample-interval."""
    require_cumulus_equal(engines.dut, "5.13.0")
    with allure.step("Unset system telemetry adaptive-routing-stats export and sample-interval"):
        unset_system_telemetry_adaptive_routing_stats_export(engines.dut)
        unset_system_telemetry_adaptive_routing_stats(engines.dut, key="sample-interval")
        apply_unset_changes(engines.dut)
    with allure.step("Verify adaptive-routing-stats export is disabled and sample-interval is 1"):
        out = get_system_telemetry_adaptive_routing_stats_applied(engines.dut)
        assert out.get("export", {}).get("state") == "disabled", (
            "nv unset system telemetry adaptive-routing-stats export got failed")
        assert out.get("sample-interval") == 1, (
            "nv unset system telemetry adaptive-routing-stats sample-interval got failed")


@pytest.mark.cumulus_only
@ pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test20c_otlp_system_unset_cli_validations(engines, test_api):
    """SSIM test20c: unset adaptive-routing-stats export state → disabled.

    Cumulus-only / 5.13 family (major.minor): adaptive-routing-stats is not present on later NVUE.
    """
    TestToolkit.tested_api = test_api
    require_cumulus_equal(engines.dut, "5.13.0")
    with allure.step("Unset system telemetry adaptive-routing-stats export state"):
        unset_system_telemetry_adaptive_routing_stats_export(engines.dut, key="state")
        apply_unset_changes(engines.dut)
    with allure.step("Verify adaptive-routing-stats export state is disabled"):
        out = get_system_telemetry_adaptive_routing_stats_export_applied(engines.dut)
        assert out.get("state") == "disabled", (
            "nv unset system telemetry adaptive-routing-stats export state failed")


@pytest.mark.cumulus_only
def test21a_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test21a: set histogram temporality cumulative, then unset reverts to delta.

    Cumulus-only: system telemetry histogram has no NVOS equivalent.
    """
    require_cumulus_at_least(engines.dut, "5.13.0")
    with allure.step("Set system telemetry histogram temporality to cumulative"):
        set_system_telemetry_histogram(engines.dut, "temporality", "cumulative")
        apply_unset_changes(engines.dut)
    with allure.step("Verify histogram temporality is cumulative"):
        out = get_system_telemetry_histogram_applied(engines.dut)
        assert out.get("temporality") == "cumulative", (
            "nv set system telemetry histogram temporality failed"
        )
    with allure.step("Unset system telemetry histogram temporality"):
        unset_system_telemetry_histogram(engines.dut, key="temporality")
        apply_unset_changes(engines.dut)
    with allure.step("Verify histogram temporality is delta"):
        out = get_system_telemetry_histogram_applied(engines.dut)
        assert out.get("temporality") == "delta", (
            "nv unset system telemetry histogram temporality failed"
        )


@pytest.mark.cumulus_only
def test21b_otlp_system_unset_cli_validations(engines, random_api):
    """SSIM test21b: unset histogram export state, then unset histogram export.

    Cumulus-only: system telemetry histogram has no NVOS equivalent.
    """
    require_cumulus_at_least(engines.dut, "5.10.0")
    with allure.step("Unset system telemetry histogram export state"):
        unset_system_telemetry_histogram_export(engines.dut, key="state")
        apply_unset_changes(engines.dut)
    with allure.step("Verify histogram export state is disabled"):
        out = get_system_telemetry_histogram_export_applied(engines.dut)
        assert out.get("state") == "disabled", (
            "nv unset system telemetry histogram export state failed"
        )


def test15a_otlp_system_unset_cli_validations(engines, otel_cli_coverage_baseline, random_api):
    """SSIM test15a destination unbind: unset the OTLP destination stats-group binding.

    Distinct from ``test15d_otlp_system_unset_stats_group_cli_validations``, which also
    deletes stats-group ``sg_01``.
    nv unset system telemetry export otlp grpc destination <destination-id> stats-group

    test13a unsets the whole OTLP gRPC subtree. The module golden revision is applied
    again before this test, so the destinations are already back.
    """
    require_cumulus_at_least(engines.dut, "5.12.0")
    baseline = otel_cli_coverage_baseline
    with allure.step("Unset OTLP destination stats-group"):
        for destination_id in baseline.collector_ips:
            unset_system_otlp_destination_stats_group(engines.dut, destination_id)
        apply_unset_changes(engines.dut)
    with allure.step("Verify OTLP destination stats-group is unbound"):
        for destination_id in baseline.collector_ips:
            out = get_system_otlp_destination_applied(engines.dut, destination_id)
            assert not out.get(TelemetryConsts.STATS_GROUP), (
                "nv unset system telemetry export otlp grpc destination "
                f"{destination_id} stats-group failed"
            )
