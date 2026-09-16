"""OTEL/gNMI telemetry coexistence (SSIM ``test_telemetry_coexistence.py``).

Runs on Cumulus and NVOS. Cumulus-only gNMI models (BGP/LLDP/system-CP/storage) are
marked ``cumulus_only``; dual-platform checks use ``suite.is_nvos`` in collect/validate
(same pattern as ``test_otel_split_pipelines.py``).
"""

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.system.telemetry.otel.cumulus import cache as telemetryCache
from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import (
    require_cumulus_at_least,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.coexistence import (
    COEXISTENCE_NVOS_INTERFACE_STATS_MAX_GAP,
    coexistence_otel_exclude_metrics_for_dut,
    collect_otel_all_stats_to_cache,
    gnmi_coexistence_cache_ready,
    skip_unless_gnmi_coexistence,
    validate_chassis_platform_hw_info,
    validate_coexistence_otel_metrics,
    validate_coexistence_otel_time_gaps,
    validate_cpu_platform_hw_info,
    validate_component_storage_coexistence,
    validate_component_system_otel_time_gaps,
    validate_interface_broadcast_pkts_otel_cli,
    validate_interface_multicast_pkts_otel_cli,
    validate_interface_no_buffer_uc_dropped_pkts_otel_cli,
    validate_interface_stats_time_gap,
    validate_otel_metric_attribute_filtering,
    validate_otel_persistence_time_gaps,
    validate_interface_transmit_pkts_otel_cli,
    validate_interface_wred_dropped_pkts_otel_cli,
    validate_interface_in_errors_otel_cli,
    validate_psu_platform_hw_info,
    validate_system_out_packets_coexistence,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.gnmi_coexistence import (
    COMPONENT_GNMI_ATTRS,
    INTERFACE_QOS_GNMI_ATTRS,
    SMOKE_LLDP_ATTRS,
    SYSTEM_GNMI_ATTRS,
    assert_gnmi_timing_pass,
    build_smoke_component_expected_xpaths,
    build_smoke_interface_expected_xpaths,
    build_smoke_system_expected_xpaths,
    discover_asic_model,
    discover_lldp_neighbor_context,
    discover_storage_component_name,
    run_and_validate_interface_on_change_subscription,
    validate_component_top_level_subscription,
    validate_gnmi_sample_interval_per_timestamp,
    validate_interface_top_level_subscription,
    validate_qos_prefix_subscription,
    validate_system_top_level_subscription,
    validate_top_level_subscription,
    validate_xpath_coverage_per_timestamp,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import (
    require_cumulus_at_least,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.lab_topology import (
    discover_swp_interfaces_on_dut,
    resolve_cumulus_lab_interfaces_on_dut,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.qos_xpaths import (
    generate_qos_container_xpaths,
    generate_qos_xpaths,
    qos_unsupported_xpath_substrings,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import get_dut_hostname

pytestmark = [
    pytest.mark.cumulus,
    pytest.mark.otel,
    pytest.mark.co_existence,
]


# --- Test_Telemetry_Coexistence_Smoke (OtelMgmtVrfWithTLSConfig / mgmt secured) ---

@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_smoke_01_validate_otel_metrics_collection(
    engines, devices, coexistence_smoke_cache, otel_suite_mgmt_secured, is_ib_router, test_api
):
    """SSIM ``Test_Telemetry_Coexistence_Smoke::test_01_validate_otel_metrics_collection``."""
    TestToolkit.tested_api = test_api
    validate_coexistence_otel_metrics(
        engines.dut,
        secured=True,
        is_nvos=otel_suite_mgmt_secured.is_nvos,
        engines=engines,
        devices=devices,
        cur_dir=coexistence_smoke_cache,
        is_ib_router=is_ib_router,
        destination_ip=otel_suite_mgmt_secured.primary_ip,
    )


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_smoke_02_gnmi_sample_interval_from_collection(
    engines, coexistence_smoke_cache, test_api
):
    """SSIM smoke test_02 — gNMI BGP/LLDP per-timestamp sample interval adherence."""
    TestToolkit.tested_api = test_api
    skip_unless_gnmi_coexistence("gnmi_timing", context="smoke BGP/LLDP sample interval")
    timing = telemetryCache.get_data("gnmi_timing")
    assert isinstance(timing, dict), "GNMI timing data missing"
    if gnmi_coexistence_cache_ready("gnmi-series-bgp"):
        assert timing.get("bgp") is not None, "Missing GNMI timing entry for BGP"
        validate_gnmi_sample_interval_per_timestamp(
            models=("bgp",),
            sample_interval=4,
            tolerance=0.25,
        )
    if gnmi_coexistence_cache_ready("gnmi-series-lldp"):
        assert timing.get("lldp") is not None, "Missing GNMI timing entry for LLDP"
        validate_gnmi_sample_interval_per_timestamp(
            models=("lldp",),
            sample_interval=5,
            tolerance=0.25,
        )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_smoke_03_validate_otel_time_gaps(
    engines, coexistence_smoke_cache, otel_suite_mgmt_secured, test_api
):
    """SSIM ``Test_Telemetry_Coexistence_Smoke::test_03_validate_otel_time_gaps``."""
    TestToolkit.tested_api = test_api
    validate_coexistence_otel_time_gaps(is_nvos=otel_suite_mgmt_secured.is_nvos)


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_smoke_04_gnmi_lldp_xpath_coverage_per_timestamp_strict(
    engines, coexistence_smoke_cache, test_api
):
    """SSIM smoke test_04 — strict LLDP XPath coverage per timestamp."""
    TestToolkit.tested_api = test_api
    skip_unless_gnmi_coexistence(
        "gnmi-series-lldp",
        context="smoke strict LLDP XPath coverage",
    )
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut)
    lldp_ctx = discover_lldp_neighbor_context(engines.dut, preferred_iface=lab.test_iface)
    assert lldp_ctx is not None, "LLDP neighbor context missing on mlx lab"
    validate_xpath_coverage_per_timestamp(
        model="lldp",
        attrs=SMOKE_LLDP_ATTRS,
        populate_kwargs=lldp_ctx,
        series_key="gnmi-series-lldp",
        group_size=1,
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_smoke_07_interface_top_level_subscription(
    engines, coexistence_smoke_cache, test_api
):
    """SSIM3 ``Test_Telemetry_Coexistence_Smoke::test_07_interface_top_level_subscription``.

    SSIM3 subscribes to the single interface top-level path
    (``/interfaces/interface[name=<iface>]``) and strictly verifies, per timestamp, that
    every modeled interface leaf (full ``interface_xpath`` set + phy-histogram buckets) is
    streamed and that no unexpected leaf appears (noise-filtered). This test reproduces that
    against the smoke ``gnmi-series-interface`` collected from the same top-level subscription.
    """
    TestToolkit.tested_api = test_api
    skip_unless_gnmi_coexistence(
        "gnmi-series-interface",
        context="smoke interface top-level subscription",
    )
    with allure.step("Resolve lab interface and build SSIM3 interface_xpath expected set"):
        lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut)
        expected_xpaths, _top_level = build_smoke_interface_expected_xpaths(
            engines.dut, lab.test_iface
        )
    with allure.step("Validate interface top-level subscription per-timestamp XPath coverage"):
        validate_interface_top_level_subscription(
            expected_xpaths=expected_xpaths,
            series_key="gnmi-series-interface",
        )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_smoke_08_system_top_level_subscription(
    engines, coexistence_smoke_cache, test_api
):
    """SSIM3 ``Test_Telemetry_Coexistence_Smoke::test_08_system_top_level_subscription``.

    SSIM3 subscribes to the single system top-level path (``/system``) and strictly verifies,
    per timestamp, that every modeled system leaf (full ``system_xpath`` set — CPU cores /
    mount points / process PIDs / control-plane-traffic / general) is streamed and that no
    unexpected leaf appears (noise-filtered), skipping first-timestamp rate metrics. This test
    reproduces that against the smoke ``gnmi-series-system`` collected from the same
    subscription.
    """
    TestToolkit.tested_api = test_api
    skip_unless_gnmi_coexistence(
        "gnmi-series-system",
        context="smoke system top-level subscription",
    )
    with allure.step("Build SSIM3 system_xpath expected set from discovered context"):
        expected_xpaths, _top_level = build_smoke_system_expected_xpaths(engines.dut)
    with allure.step("Validate system top-level subscription per-timestamp XPath coverage"):
        validate_system_top_level_subscription(
            expected_xpaths=expected_xpaths,
            dut=engines.dut,
            series_key="gnmi-series-system",
        )


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_smoke_09_components_top_level_subscription(
    engines, coexistence_smoke_cache, test_api
):
    """SSIM3 ``Test_Telemetry_Coexistence_Smoke::test_09_components_top_level_subscription``.

    SSIM3 subscribes to the single components top-level path (``/components``) and strictly
    verifies, per timestamp, that every modeled component leaf (full ``components_xpath`` set —
    PSU / FAN / TEMP / TRANSCEIVER / ASIC / CHASSIS / STORAGE / CPU / LEAKAGE, expanded per
    discovered component) is streamed and that no unexpected leaf appears (noise-filtered,
    integrated-circuit dropped). This test reproduces that against the smoke
    ``gnmi-series-component`` collected from the same subscription.
    """
    TestToolkit.tested_api = test_api
    skip_unless_gnmi_coexistence(
        "gnmi-series-component",
        context="smoke components top-level subscription",
    )
    with allure.step("Build SSIM3 components_xpath expected set from discovered components"):
        expected_xpaths, _top_level = build_smoke_component_expected_xpaths(engines.dut)
    with allure.step("Validate components top-level subscription per-timestamp XPath coverage"):
        validate_component_top_level_subscription(
            expected_xpaths=expected_xpaths,
            dut=engines.dut,
            series_key="gnmi-series-component",
        )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_12_qos_prefix_level_subscription(
    engines, coexistence_interface_qos_cache, test_api
):
    """Subscribe at supported QoS prefixes for the DUT-facing interface."""
    TestToolkit.tested_api = test_api
    coexistence_interface_qos_cache.ensure_collected()
    skip_unless_gnmi_coexistence(
        'gnmi-series-qos-prefix',
        context='QoS prefix-level subscription',
    )

    with allure.step('Resolve DUT-facing interface and supported QoS prefixes'):
        lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut)
        asic_model = discover_asic_model(engines.dut)
        prefixes = generate_qos_container_xpaths(lab.test_iface)
        expected_xpaths = generate_qos_xpaths(
            lab.test_iface,
            include_containers=False,
            asic_model=asic_model,
        )
        allure.attach(lab.test_iface, 'qos_dut_facing_interface')
        allure.attach(str(asic_model), 'qos_dut_asic_model')
        allure.attach('\n'.join(prefixes), 'qos_subscription_prefixes')
        allure.attach('\n'.join(expected_xpaths), 'qos_expected_xpaths')

    with allure.step('Validate QoS prefix-level subscription coverage'):
        validate_qos_prefix_subscription(
            prefixes,
            series_key='gnmi-series-qos-prefix',
            asic_model=asic_model,
        )

    with allure.step('Validate complete and strictly scoped QoS XPath coverage'):
        validate_top_level_subscription(
            expected_xpaths=expected_xpaths,
            series_key='gnmi-series-qos-prefix',
            model_label='qos',
            noise_substrings=qos_unsupported_xpath_substrings(asic_model),
        )

    with allure.step('Validate QoS 15-second sample interval'):
        assert_gnmi_timing_pass(
            'qos-prefix',
            context='QoS 15-second sample interval',
        )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_13_collect_otel_data_persistence(
    engines, coexistence_otel_persistence_cache, test_api
):
    """Verify fresh OTEL collection after earlier OTEL and gNMI activity."""
    TestToolkit.tested_api = test_api
    coexistence_otel_persistence_cache.ensure_collected()

    validate_coexistence_otel_metrics(
        engines.dut,
        secured=False,
        exclude_metrics=coexistence_otel_exclude_metrics_for_dut(
            engines.dut,
            secured=False,
            include_dot1x=True,
        ),
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_14_collect_otel_data_persistence_time_gaps(
    coexistence_otel_persistence_cache, test_api
):
    """Validate OTEL delivery frequency using the data cached by test_13."""
    TestToolkit.tested_api = test_api
    # Reuse the module persistence cache (collected by test_13 when both run).
    coexistence_otel_persistence_cache.ensure_collected()
    validate_otel_persistence_time_gaps()


# --- Test_Telemetry_Interface_QoS_Validation_Coexistence (split pipeline mgmt) ---


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_01_gnmi_interface_xpath_coverage_per_timestamp(
    engines, coexistence_interface_qos_cache, test_api
):
    """SSIM Interface/QoS test_01 — gNMI interface XPath coverage."""
    TestToolkit.tested_api = test_api
    coexistence_interface_qos_cache.ensure_collected()
    with allure.step('Verify gNMI interface cache is populated'):
        skip_unless_gnmi_coexistence(
            "gnmi-series-interface",
            context="Interface/QoS XPath coverage",
        )
    with allure.step('Resolve lab interface for xpath population'):
        lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut)
        allure.attach(lab.test_iface, 'test_iface')
    with allure.step('Validate interface XPath coverage per timestamp (group_size=2)'):
        validate_xpath_coverage_per_timestamp(
            model="interface",
            attrs=INTERFACE_QOS_GNMI_ATTRS,
            populate_kwargs={"interface": lab.test_iface},
            series_key="gnmi-series-interface",
            group_size=2,
        )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_02_validate_otel_metrics_collection(
    engines, devices, coexistence_interface_qos_cache, otel_suite_mgmt, is_ib_router, test_api
):
    """SSIM ``Test_Telemetry_Interface_QoS_Validation_Coexistence::test_02``."""
    TestToolkit.tested_api = test_api
    coexistence_interface_qos_cache.ensure_collected()
    validate_coexistence_otel_metrics(
        engines.dut,
        secured=False,
        is_nvos=otel_suite_mgmt.is_nvos,
        engines=engines,
        devices=devices,
        cur_dir=coexistence_interface_qos_cache.cur_dir,
        is_ib_router=is_ib_router,
        destination_ip=otel_suite_mgmt.primary_ip,
        use_stats_group=True,
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_03_gnmi_interface_sample_interval_adherence(
    engines, coexistence_interface_qos_cache, test_api
):
    """SSIM Interface/QoS test_03 — gNMI interface sample-interval adherence."""
    TestToolkit.tested_api = test_api
    coexistence_interface_qos_cache.ensure_collected()
    skip_unless_gnmi_coexistence("gnmi-series-interface", context="Interface sample interval")
    assert_gnmi_timing_pass('interface', context='Interface sample interval')


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_04_otel_interface_stats_time_gap_analysis(
    engines, coexistence_interface_qos_cache, otel_suite_mgmt, test_api
):
    """SSIM Interface/QoS test_04 — interface_stats_gap threshold."""
    TestToolkit.tested_api = test_api
    coexistence_interface_qos_cache.ensure_collected()
    max_gap = (
        COEXISTENCE_NVOS_INTERFACE_STATS_MAX_GAP
        if otel_suite_mgmt.is_nvos
        else 4.0
    )
    validate_interface_stats_time_gap(max_gap=max_gap)


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_05_interface_in_broadcast_pkts(
    engines, coexistence_interface_qos_cache, otel_suite_mgmt, test_api
):
    """SSIM Interface/QoS test_05 — OTEL vs CLI for in_broadcast_pkts (GNMI portion skipped on mlx)."""
    TestToolkit.tested_api = test_api
    coexistence_interface_qos_cache.ensure_collected()
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    swps = discover_swp_interfaces_on_dut(engines.dut)
    iface = lab.test_iface if lab.test_iface in swps else (swps[0] if swps else lab.test_iface)
    validate_interface_broadcast_pkts_otel_cli(
        engines.dut, interface=iface, is_nvos=otel_suite_mgmt.is_nvos
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_06_validate_otel_metric_attribute_filtering(
    engines, coexistence_attribute_filtering_cache, test_api
):
    """SSIM Interface/QoS ``test_06_validate_otel_metric_attribute_filtering``.

    After unsetting interface-stats pg/tc {1,3,5,7} and sp {1,3,7}, the exported tc/sp/pg
    metrics must drop the unset attribute values and keep the surviving ones.
    """
    TestToolkit.tested_api = test_api
    with allure.step("Validating tc attribute filtering"):
        validate_otel_metric_attribute_filtering(
            metric_name="nvswitch_interface_tc_tx_octet",
            disallowed_values={"tc": {1, 3, 5, 7}},
            required_values={"tc": {0, 2, 4, 6}},
        )
    with allure.step("Validating sp attribute filtering"):
        validate_otel_metric_attribute_filtering(
            metric_name="nvswitch_interface_sp_rx_pause",
            disallowed_values={"sp": {1, 3, 4, 5, 6, 7}},
            required_values={"sp": {0, 2}},
        )
    with allure.step("Validating pg attribute filtering"):
        validate_otel_metric_attribute_filtering(
            metric_name="nvswitch_interface_pg_rx_frames",
            disallowed_values={"pg": {1, 3, 5, 7}},
            required_values={"pg": {0, 2, 4, 6}},
        )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_07_interface_in_multicast_pkts(
    engines, coexistence_interface_qos_cache, test_api
):
    """SSIM Interface/QoS test_07 — OTEL vs CLI for in_multicast_pkts (GNMI portion skipped on mlx)."""
    TestToolkit.tested_api = test_api
    coexistence_interface_qos_cache.ensure_collected()
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    swps = discover_swp_interfaces_on_dut(engines.dut)
    iface = lab.test_iface if lab.test_iface in swps else (swps[0] if swps else lab.test_iface)
    validate_interface_multicast_pkts_otel_cli(engines.dut, interface=iface)


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_08_interface_no_buffer_uc_dropped_pkts(
    engines, coexistence_interface_qos_cache, test_api
):
    """SSIM Interface/QoS test_08 — OTEL vs CLI for no_buffer_uc_dropped_pkts (GNMI portion skipped on mlx)."""
    TestToolkit.tested_api = test_api
    coexistence_interface_qos_cache.ensure_collected()
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    swps = discover_swp_interfaces_on_dut(engines.dut)
    iface = lab.test_iface if lab.test_iface in swps else (swps[0] if swps else lab.test_iface)
    validate_interface_no_buffer_uc_dropped_pkts_otel_cli(
        engines.dut, interface=iface, traffic_class="7"
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_09_interface_wred_dropped_pkts(
    engines, coexistence_interface_qos_cache, test_api
):
    """SSIM Interface/QoS test_09 — OTEL vs CLI for wred_dropped_pkts (GNMI portion skipped on mlx)."""
    TestToolkit.tested_api = test_api
    coexistence_interface_qos_cache.ensure_collected()
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    swps = discover_swp_interfaces_on_dut(engines.dut)
    iface = lab.test_iface if lab.test_iface in swps else (swps[0] if swps else lab.test_iface)
    validate_interface_wred_dropped_pkts_otel_cli(
        engines.dut, interface=iface, traffic_class="7"
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_10_interface_transmit_pkts(
    engines, coexistence_interface_qos_cache, test_api
):
    """SSIM Interface/QoS test_10 — OTEL vs CLI for transmit_pkts (GNMI portion skipped on mlx)."""
    TestToolkit.tested_api = test_api
    coexistence_interface_qos_cache.ensure_collected()
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    swps = discover_swp_interfaces_on_dut(engines.dut)
    iface = lab.test_iface if lab.test_iface in swps else (swps[0] if swps else lab.test_iface)
    validate_interface_transmit_pkts_otel_cli(
        engines.dut, interface=iface, traffic_class="7"
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_11_interface_in_errors(
    engines, coexistence_interface_qos_cache, test_api
):
    """SSIM Interface/QoS test_11 — OTEL vs CLI for in_errors (GNMI portion skipped on mlx)."""
    TestToolkit.tested_api = test_api
    coexistence_interface_qos_cache.ensure_collected()
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    swps = discover_swp_interfaces_on_dut(engines.dut)
    iface = lab.test_iface if lab.test_iface in swps else (swps[0] if swps else lab.test_iface)
    validate_interface_in_errors_otel_cli(engines.dut, interface=iface)


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_12_qos_transmit_pkts_queue0(
    engines, coexistence_interface_qos_cache, test_api
):
    """SSIM Interface/QoS test_12 — OTEL vs CLI ``tx-frames`` for queue-0.

    Reuses the suite cache. Unlike queue-7, queue-0 usually cannot use a dedicated
    ``cli-time-series-qos-q0`` series, so tolerance is 25%.
    """
    TestToolkit.tested_api = test_api
    coexistence_interface_qos_cache.ensure_collected()
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    swps = discover_swp_interfaces_on_dut(engines.dut)
    iface = lab.test_iface if lab.test_iface in swps else (swps[0] if swps else lab.test_iface)
    validate_interface_transmit_pkts_otel_cli(
        engines.dut,
        interface=iface,
        traffic_class="0",
        tolerance_percent=25.0,
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_13_on_change_subscription_interface(
    engines, coexistence_interface_qos_cache, test_api
):
    """SSIM Interface/QoS test_13 — gNMI dial-out on-change for interface description."""
    TestToolkit.tested_api = test_api
    coexistence_interface_qos_cache.ensure_collected()
    lab = resolve_cumulus_lab_interfaces_on_dut(engines.dut, get_dut_hostname(engines.dut))
    swps = discover_swp_interfaces_on_dut(engines.dut)
    iface = lab.test_iface if lab.test_iface in swps else (swps[0] if swps else lab.test_iface)
    assert engines.sonic_mgmt is not None, "sonic_mgmt required for gNMI dial-out on-change"
    # Release the deferred suite session: it keeps gnmi-server/grpc-tunnel bound to
    # ``gnmic_ca``, which blocks the on-change run from re-importing that CA.
    coexistence_interface_qos_cache.teardown()
    run_and_validate_interface_on_change_subscription(
        engines.dut,
        engines.sonic_mgmt,
        interface=iface,
        is_nvos=coexistence_interface_qos_cache.is_nvos,
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_interface_qos_14_collect_otel_all_stats_to_cache(
    engines, coexistence_interface_qos_cache, test_api
):
    """SSIM Interface/QoS test_14 — fresh OTEL-only all-stats collect into cache.

    Clears the suite cache, runs a dedicated OTEL-only all-stats collection (no
    gNMI), then validates presence / no extras and attribute coverage.
    """
    TestToolkit.tested_api = test_api
    bundle = coexistence_interface_qos_cache
    with allure.step("Clear cache and collect fresh OTEL-only all-stats window"):
        collect_otel_all_stats_to_cache(
            engines.dut,
            bundle.collector,
            bundle.cur_dir,
            collector_ips=bundle.collector_ips,
        )
    with allure.step(
        "Validate metric presence (expected present, no extras) "
            "and attribute/label coverage"):
        validate_coexistence_otel_metrics(engines.dut, secured=False)

# --- Test_Telemetry_Component_System_Validation_Coexistence ---


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_component_system_01_gnmi_system_xpath_coverage_per_timestamp(
    engines, coexistence_component_system_cache, test_api
):
    """SSIM Component/System test_01 — gNMI system XPath coverage."""
    TestToolkit.tested_api = test_api
    skip_unless_gnmi_coexistence(
        "gnmi-series-system",
        context="Component/System system XPath coverage",
    )
    validate_xpath_coverage_per_timestamp(
        model="system",
        attrs=SYSTEM_GNMI_ATTRS,
        populate_kwargs={"mount_point": "/"},
        series_key="gnmi-series-system",
        group_size=2,
    )


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_component_system_02_gnmi_component_xpath_coverage_per_timestamp(
    engines, coexistence_component_system_cache, test_api
):
    """SSIM Component/System test_02 — gNMI component XPath coverage."""
    TestToolkit.tested_api = test_api
    skip_unless_gnmi_coexistence(
        "gnmi-series-component",
        context="Component/System component XPath coverage",
    )
    component_name = discover_storage_component_name(engines.dut)
    validate_xpath_coverage_per_timestamp(
        model="component",
        attrs=COMPONENT_GNMI_ATTRS,
        populate_kwargs={"component_name": component_name},
        series_key="gnmi-series-component",
        group_size=2,
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_component_system_03_validate_otel_metrics_collection(
    engines, devices, coexistence_component_system_cache, otel_suite_mgmt, is_ib_router, test_api
):
    """SSIM ``Test_Telemetry_Component_System_Validation_Coexistence::test_03``."""
    TestToolkit.tested_api = test_api
    validate_coexistence_otel_metrics(
        engines.dut,
        secured=False,
        is_nvos=otel_suite_mgmt.is_nvos,
        engines=engines,
        devices=devices,
        cur_dir=coexistence_component_system_cache,
        is_ib_router=is_ib_router,
        destination_ip=otel_suite_mgmt.primary_ip,
        use_stats_group=True,
        exclude_metrics=(
            None
            if otel_suite_mgmt.is_nvos
            else coexistence_otel_exclude_metrics_for_dut(
                engines.dut,
                secured=False,
                include_dot1x=True,
            )
        ),
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_09_validate_telemetry_platform_hw_info_cpu(
    engines, coexistence_component_system_cache, test_api
):
    """SSIM Component/System test_09 — CPU platform hw identity across gNMI, CLI and OTEL.

    The suite cache holds the parallel collection: OTEL stats, the dedicated CPU gNMI
    component subscription (``json_ietf``, 5s sample) and the ``cpu-cli`` component payload.
    Five fields are validated, gNMI paths relative to ``/components/component[name=CPU]``:
      - cpu_serial_no → state/serial-no
      - cpu_part_no → state/part-no
      - cpu_model_name → state/model-name
      - cpu_hardware_version → state/hardware-version
      - cpu_type → state/type

    Asserts the ``cpu-cli`` cache exists, then compares gNMI vs CLI, gNMI vs OTEL and OTEL
    ``nvswitch_platform_info_hw_details`` (``type=CPU``, ``name=CPU``) vs CLI per field. The CLI
    leg reads the NVUE platform component view of the CPU (the board model reported by gNMI and
    OTEL), and skips fields NVUE does not report. Requires Cumulus >= 5.17.
    """
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.17.0")
    with allure.step("Validate CPU platform hw info (gNMI + OTEL vs CLI)"):
        validate_cpu_platform_hw_info()


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_10_validate_telemetry_platform_hw_info_psu(
    engines, coexistence_component_system_cache, test_api
):
    """SSIM Component/System test_10 — PSU platform hw identity across gNMI, CLI and OTEL.

    One randomly chosen present PSU is collected in parallel with OTEL and a dedicated PSU
    gNMI component subscription (``json_ietf``, 5s sample) into the ``psu-cli`` cache. The
    subscription omits the component list key and the sample is resolved back to the chosen PSU by
    normalized name, because the DUT spells it ``PSU 1`` where NVUE inventory keys it ``PSU1``.
    Five fields are validated, gNMI paths relative to ``/components/component[name=<psu>]``:
      - psu_serial_no → state/serial-no
      - psu_part_no → state/part-no
      - psu_model_name → state/model-name
      - psu_hardware_version → state/hardware-version
      - psu_type → state/type

    Asserts the ``psu-cli`` cache exists, then compares gNMI vs CLI, gNMI vs OTEL and OTEL
    ``nvswitch_platform_info_hw_details`` (``type=PSU|POWER_SUPPLY``, ``name=<psu>``) vs CLI.
    Requires Cumulus >= 5.17.
    """
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.17.0")
    with allure.step("Validate PSU platform hw info (gNMI + OTEL vs CLI)"):
        validate_psu_platform_hw_info()


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_component_system_04_gnmi_sample_interval_adherence(
    engines, coexistence_component_system_cache, test_api
):
    """SSIM Component/System test_04 — gNMI per-timestamp sample interval (system + component)."""
    TestToolkit.tested_api = test_api
    with allure.step("Determine gNMI series available for sample-interval check"):
        models = []
        if gnmi_coexistence_cache_ready("gnmi-series-system"):
            models.append("system")
        if gnmi_coexistence_cache_ready("gnmi-series-component"):
            models.append("component")
        if not models:
            pytest.skip("No system/component gNMI series collected on this lab")
    with allure.step("Validate gNMI per-timestamp sample interval (system + component)"):
        validate_gnmi_sample_interval_per_timestamp(
            models=tuple(models),
            sample_interval=5,
            tolerance=0.10,
            cache_key="gnmi_interval_validation_component_system",
        )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_component_system_05_otel_time_gap_analysis(
    engines, coexistence_component_system_cache, test_api
):
    """SSIM Component/System test_05 — OTEL control-plane / disk metric time-gap analysis."""
    TestToolkit.tested_api = test_api
    with allure.step("Validate component/system OTEL metric time-gap analysis"):
        validate_component_system_otel_time_gaps()


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_component_system_06_system_out_packets_validation(
    engines, coexistence_component_system_cache, test_api
):
    """SSIM Component/System test_06 — system control-plane out-packets (OTEL vs CLI).

    These counters are Linux netstat IP InDelivers, exported over OTEL as
    ``node_netstat_Ip_InDelivers`` / ``node_netstat_Ip6_InDelivers`` and read via
    ``nstat``. No gNMI/NVUE control-plane-traffic path exists on Cumulus, so only the
    OTEL-vs-CLI leg is validated. node_netstat export requires Cumulus >= 5.17.
    """
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.17.0")
    with allure.step("Validate system out-packet counters (OTEL vs CLI)"):
        validate_system_out_packets_coexistence(engines.dut)


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_component_system_07_component_storage_bytes_time_validation(
    engines, coexistence_component_system_cache, test_api
):
    """SSIM Component/System test_07 — storage read-bytes / write-seconds (gNMI present + CLI cross-check)."""
    TestToolkit.tested_api = test_api
    with allure.step("Validate component storage counters (gNMI present + CLI cross-check)"):
        validate_component_storage_coexistence(engines.dut)


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_component_system_08_validate_telemetry_platform_hw_info_chassis(
    engines, coexistence_component_system_cache, test_api
):
    """SSIM Component/System test_08 — CHASSIS ``nvswitch_platform_info_hw_details`` OTEL vs CLI."""
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.17.0")
    with allure.step("Validate CHASSIS platform hw info (OTEL vs CLI)"):
        validate_chassis_platform_hw_info(engines.dut)
