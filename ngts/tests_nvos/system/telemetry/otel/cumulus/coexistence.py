"""Coexistence telemetry helpers (SSIM ``test_telemetry_coexistence.py`` mlx-lab parity)."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.tests_nvos.system.telemetry.otel.cumulus import cache as telemetryCache
from ngts.tests_nvos.system.telemetry.otel.cumulus.catalog import (
    supported_metrics_for_mgmt_vrf_secured_validation,
    supported_metrics_for_test01_validation,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
    collect_and_cache_secured_otel_session,
    collect_cli_mgmt_vrf_session_data,
    collect_telemetry_data,
    cumulus_otel_artifact_path,
    get_cp_stat_sx_api_cli,
    get_dut_hostname,
    parse_telemetry_and_cache,
    settle_dut_ssh_after_module_transition,
    truncate_collector_export,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.gnmi_coexistence import (
    COMPONENT_GNMI_ATTRS,
    CPU_COMPONENT_GNMI_SERIES_KEY,
    CPU_COMPONENT_NAME,
    CPU_PLATFORM_HW_GNMI_ATTRS,
    PSU_COMPONENT_GNMI_SERIES_KEY,
    PSU_PLATFORM_HW_GNMI_ATTRS,
    PSU_STATE_LEAF_BY_ATTR,
    _component_xpath,
    build_component_system_gnmi_jobs,
    build_interface_qos_gnmi_jobs,
    build_smoke_gnmi_jobs,
    discover_storage_component_name,
    get_selected_psu_component_name,
    gnmi_component_names_in_series,
    latest_gnmi_component_state_leaf,
    latest_gnmi_leaf_for_xpath,
    latest_gnmi_numeric_for_xpath,
    normalize_component_name,
    run_gnmi_with_parallel_work,
    teardown_gnmi_coexistence_session,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import dut_root_on_nvme_storage
from ngts.tests_nvos.system.telemetry.otel.cumulus.sample_interval import (
    check_metrics_within_range,
    get_dut_os_info,
    timegap_analysis_v2,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.catalog import detect_nvue_python_pkg_version
from ngts.tests_nvos.system.telemetry.otel.cumulus.split_pipeline import (
    prepare_split_pipeline_insecure_pre_run,
)
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tests_nvos.system.telemetry.otel.cumulus.validations import OtelDataValidations
from ngts.tests_nvos.system.telemetry.otel.otel_collector import OtelCollector

logger = logging.getLogger(__name__)


@dataclass
class CoexistenceCollectResult:
    """OTEL cache payload plus optional deferred gNMI session for module teardown."""

    otel_payload: Dict[str, Any]
    gnmi_session: Any = None


@dataclass
class CoexistenceInterfaceQosBundle:
    """Per-test collector: run ``ensure_collected()`` from the test body for a fresh window."""

    cur_dir: str
    dut: Any
    collector: OtelCollector
    sonic_mgmt: Any
    collector_ips: Tuple[str, ...]
    is_nvos: bool = False
    export_vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT
    telemetry_unit: Optional[str] = None
    _collected: bool = False
    _gnmi_session: Any = None

    def ensure_collected(self, *, wait_sec: Optional[int] = None) -> None:
        """Collect a fresh OTEL+gNMI window for the calling test (no cache reuse)."""
        if wait_sec is None:
            wait_sec = COEXISTENCE_INTERFACE_QOS_COLLECT_WAIT_SEC
        self.teardown()
        with allure.step('Collect fresh Interface/QoS coexistence OTEL+gNMI cache'):
            result = collect_coexistence_interface_qos_cache(
                self.dut,
                self.collector,
                self.cur_dir,
                collector_ips=self.collector_ips,
                sonic_mgmt=self.sonic_mgmt,
                wait_sec=wait_sec,
                defer_gnmi_teardown=True,
                is_nvos=self.is_nvos,
                export_vrf=self.export_vrf,
                telemetry_unit=self.telemetry_unit,
            )
            self._gnmi_session = result.gnmi_session
            self._collected = True

    def teardown(self) -> None:
        if not self._collected or self._gnmi_session is None:
            return
        with allure.step('Teardown deferred Interface/QoS gNMI coexistence session'):
            teardown_gnmi_coexistence_session(self.dut, self._gnmi_session)
        self._gnmi_session = None
        self._collected = False


@dataclass
class CoexistenceOtelPersistenceBundle:
    """Per-test OTEL persistence collector for tests 13 and 14."""

    cur_dir: str
    dut: Any
    collector: OtelCollector
    collector_ips: Tuple[str, ...]
    vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT

    def ensure_collected(self, *, wait_sec: Optional[int] = None) -> None:
        """Collect a fresh OTEL persistence window for the calling test (no cache reuse)."""
        if wait_sec is None:
            wait_sec = COEXISTENCE_OTEL_PERSISTENCE_COLLECT_WAIT_SEC
        with allure.step('Collect fresh OTEL persistence cache'):
            collect_otel_persistence_cache(
                self.dut,
                self.collector,
                self.cur_dir,
                collector_ips=self.collector_ips,
                vrf=self.vrf,
                wait_sec=wait_sec,
            )


# SSIM ``Test_Telemetry_Coexistence_Smoke`` / Interface QoS shared excludes.
COEXISTENCE_OTEL_EXCLUDE_BASE: Tuple[str, ...] = (
    "nvswitch_ar_congestion_changes",
    "nvswitch_srv6_in_pkts",
    "nvswitch_srv6_no_sid_drops",
    "nvswitch_qos_trimmed_unicast_pkts",
    "nvswitch_interface_trimmed_unicast_pkts",
    "nvswitch_interface_trimmed_tx_unicast_pkts",
    "nvswitch_interface_tc_trimmed_unicast_pkts",
    "nvswitch_acl_set_ipv4_info",
    "nvswitch_acl_set_ipv6_info",
    "nvswitch_acl_set_l4_info",
    "nvswitch_acl_set_l2_info",
    "nvswitch_acl_interface_matched_pkts",
    "nvswitch_acl_interface_matched_bytes",
    "nvswitch_dot1x_interface_info",
    "nvswitch_dot1x_ipv6_profile_info",
    "nvswitch_dot1x_ipv6_profile_property_info",
    "nvswitch_dot1x_ipv6_profile_summary",
    "nvswitch_dot1x_radius_client_info",
    "nvswitch_dot1x_radius_server_info",
    "nvswitch_dot1x_reauth_timeouts",
    "nvswitch_dot1x_supplicant_eapol_counters",
    "nvswitch_dot1x_supplicant_status",
    "nvswitch_dot1x_supplicant_summary",
    "nvswitch_dot1x_system_info",
    "nvswitch_platform_asic_resource_free",
    "nvswitch_platform_asic_resource_high_watermark",
    "nvswitch_platform_asic_resource_high_watermark_timestamp",
    "nvswitch_platform_asic_resource_max_limit",
    "nvswitch_platform_asic_resource_used",
    "scrape_duration_seconds",
    "scrape_samples_post_metric_relabeling",
    "scrape_samples_scraped",
    "scrape_series_added",
    "up",
)

COEXISTENCE_OTEL_EXCLUDE_BGP_PEER: Tuple[str, ...] = (
    "nvrouting_bgp_peer_as",
    "nvrouting_bgp_peer_fsm_established_transitions",
    "nvrouting_bgp_peer_graceful_shutdown",
    "nvrouting_bgp_peer_info",
    "nvrouting_bgp_peer_last_established",
    "nvrouting_bgp_peer_local_as",
    "nvrouting_bgp_peer_rib_adj_in",
    "nvrouting_bgp_peer_rib_adj_in_installed",
    "nvrouting_bgp_peer_rib_adj_out_advertised",
    "nvrouting_bgp_peer_rx_updates",
    "nvrouting_bgp_peer_socket_in_queue",
    "nvrouting_bgp_peer_socket_out_queue",
    "nvrouting_bgp_peer_state",
    "nvrouting_bgp_peer_total_msgs_recvd",
    "nvrouting_bgp_peer_total_msgs_sent",
    "nvrouting_bgp_peer_tx_updates",
)

COEXISTENCE_SMOKE_COLLECT_WAIT_SEC = 150
COEXISTENCE_INTERFACE_QOS_COLLECT_WAIT_SEC = 120
COEXISTENCE_OTEL_PERSISTENCE_COLLECT_WAIT_SEC = 120

# SSIM smoke ``test_03_validate_otel_time_gaps`` / persistence ``test_14`` expected gaps
# (NGTS ``timegap_analysis_v2`` keys). Secured mgmt mlx-lab:
# ``SECURED_ROOT_SAMPLE_INTERVALS`` + no spine fabric (AR/FRR absent).
COEXISTENCE_SMOKE_TIME_GAPS: Dict[str, float] = {
    "histogram_gap": 1.1,
    "interface_stats_gap": 2.0,
    "control_stats_gap": 2.0,
    "platf_cpu_gap": 60.0,
    "platf_mem_gap": 60.0,
    "platf_file_gap": 60.0,
    "platf_envir_gap": 60.0,
    "platf_disk_gap": 60.0,
    "buffer_gap": 60.0,
    "phy_gap": 2.0,
    "lldp_gap": 20.0,
    "systemd_gap": 60.0,
}

# Gaps skipped when ``timegap_analysis_v2`` returns 0 (family not exported on mlx lab).
COEXISTENCE_SMOKE_SKIP_ZERO_GAPS: Tuple[str, ...] = (
    "frr_gap",
    "ar_gap",
)

INTERFACE_GNMI_TO_OTEL_METRIC: Dict[str, str] = {
    "in_broadcast_pkts": "nvswitch_interface_ether_stats_broadcast_pkts",
    "in_multicast_pkts": "nvswitch_interface_ether_stats_multicast_pkts",
    "in_octets": "nvswitch_interface_ether_stats_octets",
    "in_errors": "nvswitch_interface_if_in_errors",
    "no_buffer_uc_dropped_pkts": "nvswitch_interface_tc_tx_no_buffer_discard_uc",
    "wred_dropped_pkts": "nvswitch_interface_tc_tx_wred_discard",
    "transmit_pkts": "nvswitch_interface_tc_tx_frames",
}

# NVOS OTEL names (IB/if counters; no ether_stats catalog).
INTERFACE_GNMI_TO_OTEL_METRIC_NVOS: Dict[str, str] = {
    "in_broadcast_pkts": "nvswitch_interface_if_in_broadcast_pkts",
}

# Align with ``apply_split_pipeline_test01_stats_group_nvos`` / split-pipeline gaps.
# Family map + Interface/QoS expected gaps live on CumulusOtelConst (shared with
# ``test_otel_split_pipelines``). Smoke gaps differ (shorter collect window).
COEXISTENCE_NVOS_SMOKE_TIME_GAPS: Dict[str, float] = {
    "interface_stats_gap": 10.0,
    "platf_cpu_gap": 60.0,
    "platf_mem_gap": 60.0,
    "platf_disk_gap": 60.0,
    "platf_envir_gap": 60.0,
}
# Platform-stats classes a given NVOS platform may not export at all; a zero/absent gap
# for these is tolerated. ``interface_stats_gap`` is deliberately absent from this tuple:
# it must always stream, and keeping it in the expected set is what stops
# ``check_metrics_within_range`` from being called with an empty (no-op) expectation.
COEXISTENCE_NVOS_OPTIONAL_TIME_GAPS: Tuple[str, ...] = (
    "platf_cpu_gap",
    "platf_mem_gap",
    "platf_disk_gap",
    "platf_envir_gap",
)
COEXISTENCE_NVOS_INTERFACE_STATS_MAX_GAP = 15.0
COEXISTENCE_NVOS_STATS_GROUP_ID = CumulusOtelConst.SPLIT_PIPELINE_STATS_GROUP_ID
INTERFACE_QOS_NO_BUFFER_UC_CLI_FIELD = "tx-uc-buffer-discards"
INTERFACE_QOS_NO_BUFFER_UC_DEFAULT_TC = "7"
INTERFACE_QOS_WRED_CLI_FIELD = "tx-wred-discards"
INTERFACE_QOS_WRED_DEFAULT_TC = "7"
INTERFACE_QOS_TRANSMIT_PKTS_CLI_FIELD = "tx-frames"
INTERFACE_QOS_TRANSMIT_PKTS_DEFAULT_TC = "7"


# SSIM ``test_06_validate_otel_metric_attribute_filtering`` — pg/tc/sp values unset from the
# interface-stats baseline (``_apply_coexistence_split_pipeline_root_config`` enables pg/tc 0-7
# and sp 0,1,2,3,7). After unset the surviving values are pg/tc {0,2,4,6} and sp {0,2}.
COEXISTENCE_ATTR_FILTER_UNSET_PGS: Tuple[str, ...] = ("1", "3", "5", "7")
COEXISTENCE_ATTR_FILTER_UNSET_TCS: Tuple[str, ...] = ("1", "3", "5", "7")
COEXISTENCE_ATTR_FILTER_UNSET_SPS: Tuple[str, ...] = ("1", "3", "7")
COEXISTENCE_ATTR_FILTER_SETTLE_SEC = 10
# Dedicated OTEL cache key for the attribute-filtering suite. Kept distinct from the generic
# ``otel`` key so this module-scoped collection neither reads nor overwrites other suites' OTEL
# payloads (the substring ``otel`` still routes it to the OTEL container in ``cache._container_for_key``).
COEXISTENCE_ATTR_FILTER_CACHE_KEY = "otel_attr_filter"


COEXISTENCE_DOT1X_EXCLUDE_METRICS: Tuple[str, ...] = (
    "nvswitch_dot1x_interface_info",
    "nvswitch_dot1x_ipv6_profile_info",
    "nvswitch_dot1x_ipv6_profile_property_info",
    "nvswitch_dot1x_ipv6_profile_summary",
    "nvswitch_dot1x_radius_client_info",
    "nvswitch_dot1x_radius_server_info",
    "nvswitch_dot1x_reauth_timeouts",
    "nvswitch_dot1x_supplicant_eapol_counters",
    "nvswitch_dot1x_supplicant_status",
    "nvswitch_dot1x_supplicant_summary",
    "nvswitch_dot1x_system_info",
)


def _dedupe_metric_names(names: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for name in names:
        if name not in seen:
            out.append(name)
            seen.add(name)
    return out


def coexistence_otel_exclude_metrics(*, include_bgp_peer: bool = True) -> List[str]:
    """Deduped exclude list for Interface/QoS and Component/System suites."""
    combined: List[str] = list(COEXISTENCE_OTEL_EXCLUDE_BASE)
    if include_bgp_peer:
        combined.extend(COEXISTENCE_OTEL_EXCLUDE_BGP_PEER)
    return _dedupe_metric_names(combined)


def coexistence_otel_exclude_metrics_for_dut(
    dut,
    *,
    secured: bool = False,
    include_bgp_peer: bool = True,
    include_dot1x: bool = False,
) -> List[str]:
    """SSIM coexistence excludes plus mlx-lab gaps (no spine↔leaf fabric / trap traffic)."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import secured_exclude_metrics

    combined: List[str] = list(coexistence_otel_exclude_metrics(include_bgp_peer=include_bgp_peer))
    if secured:
        combined.extend(secured_exclude_metrics(dut, no_routing=True))
    else:
        combined.extend(CumulusOtelConst.SECURED_NO_ROUTING_EXCLUDE_METRICS)
        combined.extend(CumulusOtelConst.SECURED_MGMT_LAB_TOPOLOGY_EXCLUDE_METRICS)
        combined.extend(CumulusOtelConst.SECURED_MGMT_CP_TRAP_GROUP_EXCLUDE_METRICS)
    if include_dot1x:
        combined.extend(COEXISTENCE_DOT1X_EXCLUDE_METRICS)
    return _dedupe_metric_names(combined)


def _append_nvme_excludes(exclude: List[str], dut) -> List[str]:
    if dut_root_on_nvme_storage(dut):
        exclude.extend(
            [
                "node_disk_ata_rotation_rate_rpm",
                "node_disk_ata_write_cache",
                "node_disk_ata_write_cache_enabled",
                "node_cpu_core_throttles_total",
                "node_cpu_package_throttles_total",
            ]
        )
    return exclude


def gnmi_coexistence_cache_ready(series_key: str) -> bool:
    """True when gNMI series data was populated (SSIM ``collect_telemetry`` gnmi_jobs)."""
    try:
        series = telemetryCache.get_data(series_key)
    except KeyError:
        return False
    return isinstance(series, dict) and bool(series)


def skip_unless_gnmi_coexistence(series_key: str, *, context: str) -> None:
    if not gnmi_coexistence_cache_ready(series_key):
        pytest.skip(
            'gNMI coexistence cache %r missing for %s '
            '(no grpc-tunnel subscription data: check LLDP/BGP neighbor or gnmic on sonic-mgmt)'
            % (series_key, context)
        )


def assert_telemetry_cache_has_data(
    cache_keys: Sequence[Tuple[str, str]],
    *,
    context: str = '',
) -> None:
    """SSIM ``assert_telemetry_cache_has_data`` parity — hard-fail when cache keys are empty."""
    missing = []
    for label, key in cache_keys:
        try:
            data = telemetryCache.get_data(key)
        except KeyError:
            data = None
        if not data:
            missing.append('%s (%s)' % (label, key))
    if missing:
        prefix = 'telemetry cache missing data'
        if context:
            prefix += ' [%s]' % context
        pytest.fail('%s: %s' % (prefix, '; '.join(missing)))


def validate_coexistence_otel_metrics(
    dut,
    *,
    secured: bool = False,
    exclude_metrics: Optional[Iterable[str]] = None,
    is_nvos: bool = False,
    engines=None,
    devices=None,
    cur_dir: Optional[str] = None,
    is_ib_router: bool = False,
    destination_ip: Optional[str] = None,
    use_stats_group: bool = False,
) -> None:
    """SSIM ``test_*_validate_otel_metrics_collection`` parity (Cumulus catalog / NVOS YAML)."""
    otel_data = telemetryCache.get_data("otel")
    metrics_timestamps = (otel_data or {}).get("metrics_timestamps")
    assert metrics_timestamps is not None, "metrics_timestamps missing from OTEL cache"

    if is_nvos:
        _validate_coexistence_otel_metrics_nvos(
            metrics_timestamps,
            engines=engines,
            devices=devices,
            cur_dir=cur_dir or "",
            secured=secured,
            is_ib_router=is_ib_router,
            destination_ip=destination_ip,
            use_stats_group=use_stats_group,
        )
        return

    hostname = get_dut_hostname(dut)
    excludes = list(
        exclude_metrics
        if exclude_metrics is not None
        else coexistence_otel_exclude_metrics_for_dut(dut, secured=secured)
    )
    excludes = _append_nvme_excludes(excludes, dut)

    if secured:
        total_metrics = supported_metrics_for_mgmt_vrf_secured_validation(dut)
    else:
        total_metrics = supported_metrics_for_test01_validation(dut)

    otel_data_val = OtelDataValidations()
    with allure.step("Validate collected metrics (coexistence)"):
        otel_data_val.validate_collected_metrics(
            metrics_timestamps,
            hostname,
            total_metrics=total_metrics,
            exclude_metrics=excludes,
        )
        otel_data_val.validate_collected_metrics_attributes_count(
            metrics_timestamps,
            hostname,
            total_metrics=total_metrics,
            exclude_metrics=excludes,
        )


def _validate_coexistence_otel_metrics_nvos(
    metrics_timestamps: Dict[str, Any],
    *,
    engines,
    devices,
    cur_dir: str,
    secured: bool,
    is_ib_router: bool,
    destination_ip: Optional[str],
    use_stats_group: bool,
) -> None:
    """Strict metric-name equality from metrics-classes.yaml + declared enablement."""
    from ngts.nvos_constants.constants_nvos import TelemetryConsts
    from ngts.tests_nvos.system.telemetry.otel.helpers import (
        assert_nvos_metric_names_for_enablement,
        build_nvos_enablement,
    )

    assert engines is not None and destination_ip, (
        "NVOS coexistence metric validation requires engines + destination_ip"
    )
    families = (
        TelemetryConsts.INTERFACE_STATS,
        TelemetryConsts.PLATFORM_STATS,
    )
    if secured and not use_stats_group:
        # Secured smoke: root families only (matches otel_suite_mgmt_secured fixture).
        enablement = build_nvos_enablement(TelemetryConsts.ALL_STATS_SUBTREES)
    else:
        enablement = build_nvos_enablement(
            families,
            collector_ips=(destination_ip,),
            stats_group_id=COEXISTENCE_NVOS_STATS_GROUP_ID,
            use_stats_group=use_stats_group,
        )
    assert_nvos_metric_names_for_enablement(
        engines,
        devices,
        cur_dir,
        actual_metric_names=metrics_timestamps.keys(),
        enablement=enablement,
        destination_ip=destination_ip,
        is_ib_router=is_ib_router,
        collector_label="coexistence-nvos",
    )


def collect_otel_all_stats_to_cache(
    dut,
    collector: OtelCollector,
    cur_dir: str,
    *,
    collector_ips: Tuple[str, str],
    wait_sec: int = COEXISTENCE_INTERFACE_QOS_COLLECT_WAIT_SEC,
) -> Dict[str, Any]:
    """Clear cache and collect a fresh OTEL-only all-stats window (no gNMI).

    Used by ``test_14_collect_otel_all_stats_to_cache`` so validation runs against a
    dedicated collect, not a reused coexistence OTEL+gNMI cache.
    """
    with allure.step("Clear prior telemetry cache"):
        telemetryCache.clear_data()

    vrf = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT
    with allure.step("Prepare all-stats OTEL export (mgmt VRF, insecure)"):
        from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import (
            _restore_otlp_mgmt_insecure_destinations,
        )

        prepare_split_pipeline_insecure_pre_run(dut)
        _restore_otlp_mgmt_insecure_destinations(dut, collector_ips, export_vrf=vrf)
        _apply_coexistence_split_pipeline_root_config(dut)
        _enable_interface_qos_coexistence_exports(dut)
        _ensure_insecure_otlp_session_after_config(dut, collector, vrf=vrf)

    collect_dir = os.path.join(cur_dir, "all_stats_otel")
    os.makedirs(collect_dir, exist_ok=True)
    with allure.step(
        "Collect fresh OTEL-only all-stats window (vrf=%s, wait_sec=%d)"
        % (vrf, wait_sec)
    ):
        truncate_collector_export(collector)
        collect_telemetry_data(dut, collector, vrf, wait_sec, None, collect_dir)

    artifact_path = cumulus_otel_artifact_path(collect_dir)
    hostname = get_dut_hostname(dut)
    payload = parse_telemetry_and_cache(
        OtelDataValidations(),
        artifact_path=artifact_path,
        hostname=hostname,
        include_time_gaps=True,
        dut=dut,
    )
    metrics_timestamps = payload.get("metrics_timestamps")
    assert isinstance(metrics_timestamps, dict) and metrics_timestamps, (
        "Fresh OTEL all-stats collection produced no metrics_timestamps data"
    )
    return payload


def validate_coexistence_otel_time_gaps(
    expected: Optional[Dict[str, float]] = None,
    *,
    is_nvos: bool = False,
) -> None:
    """SSIM smoke ``test_03_validate_otel_time_gaps`` using cached ``time_gaps``."""
    otel_data = telemetryCache.get_data("otel")
    assert isinstance(otel_data, dict) and "time_gaps" in otel_data, (
        "OTEL time_gaps missing in cache; collection must set include_time_gaps=True"
    )
    actual = otel_data["time_gaps"]
    if expected is not None:
        exp = dict(expected)
    elif is_nvos:
        exp = dict(
            otel_data.get("coexistence_expected_gaps") or
            COEXISTENCE_NVOS_SMOKE_TIME_GAPS
        )
    else:
        exp = dict(COEXISTENCE_SMOKE_TIME_GAPS)
    # Drop only the families allowed to be absent on this platform, so a DUT that stopped
    # exporting everything still fails instead of validating an empty expectation.
    optional_gaps = (
        COEXISTENCE_NVOS_OPTIONAL_TIME_GAPS if is_nvos else COEXISTENCE_SMOKE_SKIP_ZERO_GAPS
    )
    for key in optional_gaps:
        if actual.get(key, 0.0) == 0.0:
            exp.pop(key, None)
    with allure.step("Validate OTEL sample-interval time gaps (coexistence)"):
        check_metrics_within_range(exp, actual)


def validate_otel_persistence_time_gaps() -> None:
    """Validate time gaps cached by ``test_13`` using ``COEXISTENCE_SMOKE_TIME_GAPS``."""
    try:
        otel_data = telemetryCache.get_data("otel")
    except KeyError:
        otel_data = None
    assert isinstance(otel_data, dict) and "time_gaps" in otel_data, (
        "OTEL data missing; run metrics collection test first"
    )
    time_gaps = otel_data["time_gaps"]
    assert isinstance(time_gaps, dict), (
        "OTEL time_gaps must be a dictionary; run metrics collection test first"
    )

    exp = dict(COEXISTENCE_SMOKE_TIME_GAPS)
    for key in COEXISTENCE_SMOKE_SKIP_ZERO_GAPS:
        if time_gaps.get(key) == 0.0:
            exp.pop(key, None)
    with allure.step("Validate cached OTEL persistence time gaps (primary collector)"):
        check_metrics_within_range(exp, time_gaps)


def collect_otel_persistence_cache(
    dut,
    collector: OtelCollector,
    cur_dir: str,
    *,
    collector_ips: Tuple[str, ...],
    vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT,
    wait_sec: int = COEXISTENCE_OTEL_PERSISTENCE_COLLECT_WAIT_SEC,
) -> Dict[str, Any]:
    """Collect a fresh OTEL window after earlier coexistence activity.

    Uses mgmt VRF insecure OTLP (mlx lab reachability to sonic-mgmt). Clears the
    in-memory cache deliberately so this is a new collection window, not reuse of
    earlier coexistence results.
    """
    with allure.step('Clear prior telemetry results'):
        telemetryCache.clear_data()

    with allure.step('Prepare mgmt VRF insecure OTEL export for persistence collection'):
        prepare_split_pipeline_insecure_pre_run(dut)
        _apply_coexistence_persistence_root_config(dut, collector_ips)
        _enable_interface_qos_coexistence_exports(dut)
        _ensure_insecure_otlp_session_after_config(
            dut, collector, vrf=vrf
        )

    with allure.step(
        'Collect fresh OTEL persistence window (vrf=%s, wait_sec=%d)'
        % (vrf, wait_sec)
    ):
        truncate_collector_export(collector)
        collect_telemetry_data(
            dut,
            collector,
            vrf,
            wait_sec,
            None,
            cur_dir,
        )

    artifact_path = cumulus_otel_artifact_path(cur_dir)
    hostname = get_dut_hostname(dut)
    payload = parse_telemetry_and_cache(
        OtelDataValidations(),
        artifact_path=artifact_path,
        hostname=hostname,
        include_time_gaps=True,
        dut=dut,
        attach_debug=True,
    )
    metrics_timestamps = payload.get('metrics_timestamps')
    assert isinstance(metrics_timestamps, dict) and metrics_timestamps, (
        'Fresh OTEL persistence collection produced no metrics_timestamps data'
    )
    assert isinstance(payload.get('time_gaps'), dict), (
        'Fresh OTEL persistence collection produced no time_gaps data'
    )
    return payload


def validate_interface_stats_time_gap(*, max_gap: float = 4.0) -> None:
    """SSIM Interface/QoS ``test_04_otel_interface_stats_time_gap_analysis``."""
    time_gaps = telemetryCache.get_data("otel").get("time_gaps")
    assert time_gaps is not None, "time_gaps not available in OTEL cache"
    gap = time_gaps.get("interface_stats_gap")
    assert gap is not None, "interface_stats_gap not present in time_gaps"
    assert gap <= max_gap, f"Time gap analysis failed for Interface metrics: {gap}"


# NVUE ``nv show interface <iface> counters`` leaf names per counter. These are the
# per-interface ether counter totals that match the OTEL ``ether_stats`` semantics.
# Octets is reported as ``in-bytes`` in the CLI (OpenConfig ``in-octets`` alias). First
# present leaf wins.
_INTERFACE_ETHER_CLI_LEAVES: Dict[str, Tuple[str, ...]] = {
    "in_broadcast_pkts": ("in-broadcast-pkts", "in-broadcast"),
    "in_multicast_pkts": ("in-multicast-pkts", "in-multicast"),
    "in_octets": ("in-octets", "in-bytes"),
}


def _interface_ether_counter_cli_value(
    dut, interface: str, counter: str
) -> Tuple[Optional[int], str, bool]:
    """CLI counter total from ``nv show interface <iface> counters``.

    Reads the per-interface ether counters live and deep-searches for the counter's leaf
    (see ``_INTERFACE_ETHER_CLI_LEAVES``). This is the correct CLI counterpart for the
    ``nvswitch_interface_ether_stats_*`` OTEL metrics; the cached ingress-buffer-stats
    snapshot only carries a subset (``rx-frames``/``rx-broadcast``).

    Returns ``(value, reason, fatal)``. On success ``value`` is the integer counter,
    ``reason`` is empty and ``fatal`` is ``False``. On failure ``value`` is ``None`` and
    ``reason`` names the specific cause; ``fatal`` distinguishes an actionable regression
    (``True`` — bad counter mapping, ``nv show`` command failure, unexpected parsed type,
    or non-numeric leaf value) from a benign data-absence (``False`` — the counter leaf is
    simply not present in the CLI output). Callers should ``fail`` on ``fatal`` and only
    ``skip`` otherwise, so CLI execution/parse regressions are not hidden behind a skip.
    """
    leaves = _INTERFACE_ETHER_CLI_LEAVES.get(counter)
    if not leaves:
        return None, f"no CLI leaf mapping for counter {counter!r}", True
    from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
    try:
        parsed = Interface(None, port_name=interface).counters.parse_show(dut_engine=dut)
    except Exception as exc:  # noqa: BLE001
        logger.warning("nv show interface %s counters failed: %s", interface, exc)
        return None, f"nv show interface {interface} counters command failed: {exc}", True
    if not isinstance(parsed, (dict, list)):
        return None, (
            f"nv show interface {interface} counters returned unexpected type "
            f"{type(parsed).__name__}"
        ), True

    def _find(obj, key):
        if isinstance(obj, dict):
            if key in obj and not isinstance(obj[key], (dict, list)):
                return obj[key]
            for value in obj.values():
                found = _find(value, key)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = _find(item, key)
                if found is not None:
                    return found
        return None

    for leaf in leaves:
        raw = _find(parsed, leaf)
        if raw is not None:
            try:
                return int(raw), "", False
            except (TypeError, ValueError):
                try:
                    return int(float(raw)), "", False
                except (TypeError, ValueError):
                    return None, (
                        f"CLI leaf {leaf!r} value {raw!r} is non-numeric on {interface}"
                    ), True
    available = sorted(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__
    logger.warning(
        "interface %s counters present but none of %s found; available keys=%s",
        interface, leaves, available,
    )
    return None, (
        f"no matching CLI leaf {leaves} in nv show interface {interface} counters"
    ), False


def _validate_interface_counter_otel_cli(
    dut,
    *,
    counter: str,
    interface: str,
    tolerance_percent: float = 20.0,
) -> None:
    """OTEL vs CLI for an interface ``ether_stats`` counter (SSIM OTEL portion only).

    Shared implementation for the per-counter wrappers (``in_multicast_pkts``,
    ``in_octets``). The SSIM GNMI-vs-CLI portion is skipped: it relies on
    ``populate_gnmi_xpaths`` / gnmi xpath model objects and BGP/LLDP-style show
    collectors that are not available in the NGTS framework here (same limitation
    that makes ``test_05`` OTEL-only). The OTEL value is the mean of the cached samples
    for ``interface``; the CLI value is read from ``nv show interface <iface> counters``
    (``_INTERFACE_ETHER_CLI_LEAVES``), which is the correct per-interface ether counter
    total matching the OTEL ``ether_stats`` semantics. When that leaf is unavailable the
    test skips — we deliberately do not fall back to the ingress-buffer-stats pg0
    snapshot, whose per-priority-group ``rx-*`` counters are a different physical quantity
    and would produce a misleading comparison.
    """
    metric = INTERFACE_GNMI_TO_OTEL_METRIC[counter]
    otel_intf_stats = telemetryCache.get_data("otel").get("intf_stats") or {}

    otel_data_val = OtelDataValidations()
    if not otel_intf_stats:
        pytest.skip("intf_stats missing; interface OTEL export not present in cache")

    if metric not in otel_intf_stats:
        pytest.skip(f"{metric} not in OTEL export on mlx lab")

    series = otel_intf_stats[metric]
    matched_key = _matching_otel_series_key(series, interface=interface)
    if matched_key is None:
        pytest.skip(f"{metric} has no samples for interface {interface!r}")

    d_otel = round(mean(series[matched_key]))

    # CLI reference: the per-interface ether counter total from
    # ``nv show interface <iface> counters`` (matches the OTEL ether_stats semantics).
    # No pg0 ingress-buffer-stats fallback: those per-priority-group ``rx-*`` counters
    # are a different physical quantity and would yield a misleading comparison.
    d_cli, cli_reason, cli_fatal = _interface_ether_counter_cli_value(dut, interface, counter)
    if d_cli is None:
        if cli_fatal:
            pytest.fail(
                f"CLI counter read failed for {counter!r} on {interface}: {cli_reason}"
            )
        pytest.skip(
            f"No CLI counter value for {counter!r} on {interface}: {cli_reason}"
        )

    deviation = otel_data_val.get_percentage_deviation(d_cli, d_otel)
    if deviation > tolerance_percent:
        pytest.fail(
            f"{metric} OTEL vs CLI failed: otel={d_otel}, cli={d_cli}, "
            f"deviation={deviation:.1f}% (max {tolerance_percent}%)"
        )


def validate_interface_broadcast_pkts_otel_cli(
    dut,
    *,
    interface: str,
    tolerance_percent: float = 20.0,
    is_nvos: bool = False,
) -> None:
    """OTEL vs CLI for ``in_broadcast_pkts`` (SSIM test_05 OTEL portion only)."""
    metric_map = (
        INTERFACE_GNMI_TO_OTEL_METRIC_NVOS if is_nvos else INTERFACE_GNMI_TO_OTEL_METRIC
    )
    metric = metric_map["in_broadcast_pkts"]
    otel_intf_stats = telemetryCache.get_data("otel").get("intf_stats") or {}
    cli_ingress = telemetryCache.get_data("cli").get("ingress_stats") or {}

    otel_data_val = OtelDataValidations()
    if not otel_intf_stats:
        pytest.skip("intf_stats missing; interface OTEL export not present in cache")

    if metric not in otel_intf_stats:
        pytest.skip(f"{metric} not in OTEL export")

    series = otel_intf_stats[metric]
    matched_key = _matching_otel_series_key(series, interface=interface)
    if matched_key is None:
        pytest.skip(f"{metric} has no samples for interface {interface!r}")

    d_otel = round(mean(series[matched_key]))
    ingress = cli_ingress.get(interface, {})
    pg0 = ingress.get("0", ingress) if isinstance(ingress, dict) else {}
    d_cli = int(pg0.get("rx-broadcast", pg0.get("broadcast", 0)) or 0)

    deviation = otel_data_val.get_percentage_deviation(d_cli, d_otel)
    if deviation > tolerance_percent:
        pytest.fail(
            f"{metric} OTEL vs CLI failed: otel={d_otel}, cli={d_cli}, "
            f"deviation={deviation:.1f}% (max {tolerance_percent}%)"
        )


def _attr_tuple_to_pairs(attrs_tuple: Any) -> Dict[str, Any]:
    """Convert an OTLP attribute key tuple to a ``{key: value}`` dict.

    NGTS builds metric attribute keys as flat tuples of alternating key/value strings,
    e.g. ``('interface', 'swp1s0', 'pg', '0', 'direction', 'rx')`` (see
    :func:`ngts.tests_nvos.system.telemetry.otel.metrics_parser.prepare_attributes_key`).
    Numeric-looking values are normalized to ``int`` to match the SSIM comparison.
    """
    pairs: Dict[str, Any] = {}
    vals = list(attrs_tuple) if isinstance(attrs_tuple, (list, tuple)) else []
    i = 0
    while i + 1 < len(vals):
        key = vals[i]
        val = vals[i + 1]
        if isinstance(val, str) and val.isdigit():
            val = int(val)
        pairs[str(key)] = val
        i += 2
    return pairs


def _matching_otel_series_key(
    series: Any,
    *,
    interface: str,
    traffic_class: Optional[str] = None,
) -> Any:
    """Return the first OTEL series key for ``interface`` (and optional ``tc``)."""
    for key in series:
        kv = _attr_tuple_to_pairs(key)
        if str(kv.get("interface")) != str(interface):
            continue
        if traffic_class is not None and str(kv.get("tc")) != str(traffic_class):
            continue
        return key
    return None


def validate_otel_metric_attribute_filtering(
    *,
    metric_name: str,
    cache_key: str = COEXISTENCE_ATTR_FILTER_CACHE_KEY,
    disallowed_values: Optional[Dict[str, Iterable[Any]]] = None,
    required_values: Optional[Dict[str, Iterable[Any]]] = None,
) -> None:
    """SSIM ``validate_otel_metric_attribute_filtering`` — pg/sp/tc datapoint filtering.

    After ``nv unset system telemetry interface-stats {ingress-buffer priority-group |
    egress-buffer traffic-class | switch-priority} <value>``, the exported OTEL metric must
    no longer carry the unset attribute values and must still carry the surviving ones.

    Skips (mlx lab friendliness) when the metric is not exported at all; otherwise asserts:
      - no datapoint carries a ``disallowed_values`` pg/sp/tc value, and
      - at least one datapoint exists for every ``required_values`` pg/sp/tc value.
    """
    otel_obj = telemetryCache.get_data_optional(cache_key) or {}
    metrics_timestamps = otel_obj.get("metrics_timestamps") or {}
    assert metrics_timestamps, f"No metrics_timestamps found in cache '{cache_key}'"
    if metric_name not in metrics_timestamps:
        pytest.skip(f"Metric '{metric_name}' not present in OTEL export on this lab")

    seen_attr_tuples = list((metrics_timestamps[metric_name] or {}).keys())
    disallowed_values = disallowed_values or {}
    required_values = required_values or {}
    failures: List[str] = []

    for attrs in seen_attr_tuples:
        kv = _attr_tuple_to_pairs(attrs)
        for key_name in ("pg", "sp", "tc"):
            if key_name not in disallowed_values:
                continue
            disset = {str(x) for x in (disallowed_values.get(key_name) or [])}
            val = kv.get(key_name)
            if str(val) in disset:
                failures.append(
                    f"{metric_name}: found disallowed {key_name}={val} in attributes {kv}"
                )

    for key_name, must_vals in required_values.items():
        must_set = {str(x) for x in (must_vals or [])}
        present = {
            str(_attr_tuple_to_pairs(attrs).get(key_name))
            for attrs in seen_attr_tuples
            if key_name in _attr_tuple_to_pairs(attrs)
        }
        missing = sorted(v for v in must_set if v not in present)
        if missing:
            failures.append(
                f"{metric_name}: required {key_name} values not found: {missing}; "
                f"present={sorted(present)}"
            )

    with allure.step(f"Validate OTEL attribute filtering for '{metric_name}'"):
        assert not failures, "\n".join(failures)
    logger.info("Attribute filtering passed for '%s'", metric_name)
# ---------------------------------------------------------------------------
# SSIM ``Test_Telemetry_Component_System_Validation_Coexistence`` (test_04..08)
# ---------------------------------------------------------------------------


# SSIM component/system ``test_05`` expected OTEL sample-interval gaps mapped to NGTS
# ``timegap_analysis_v2`` keys. Intervals match the split-pipeline root config applied by
# ``_apply_coexistence_split_pipeline_root_config`` (interface-stats=3, control=2, cpu=70,
# mem=75, file=72, environment=65, disk=74). ``histogram_gap`` is omitted: NVUE has no
# histogram export sample-interval, and asic-monitor OTEL cadence on mlx is ~1.1s or ~2s.
COMPONENT_SYSTEM_TIME_GAPS: Dict[str, float] = {
    "interface_stats_gap": 3.0,
    "control_stats_gap": 2.0,
    "platf_cpu_gap": 70.0,
    "platf_mem_gap": 75.0,
    "platf_file_gap": 72.0,
    "platf_envir_gap": 65.0,
    "platf_disk_gap": 74.0,
    "buffer_gap": 1.0,
    "phy_gap": 3.0,
    "lldp_gap": 5.0,
    "systemd_gap": 60.0,
}


def validate_component_system_otel_time_gaps() -> None:
    """SSIM component/system ``test_05`` OTEL time-gap analysis using cached ``time_gaps``.

    Families that are not exported on the mlx coexistence lab report a 0 gap; those keys are
    dropped (NGTS infra limitation) and only exported families are validated within ±25%.
    """
    with allure.step("Resolve exported component/system OTEL time-gap families"):
        otel_data = telemetryCache.get_data("otel")
        assert isinstance(otel_data, dict) and "time_gaps" in otel_data, (
            "OTEL time_gaps missing in cache; collection must set include_time_gaps=True"
        )
        actual = otel_data["time_gaps"]
        expected = {key: val for key, val in COMPONENT_SYSTEM_TIME_GAPS.items() if actual.get(key)}
        if not expected:
            pytest.skip("No component/system OTEL time-gap families exported on this lab")
    with allure.step("Validate component/system OTEL sample-interval time gaps"):
        check_metrics_within_range(expected, actual)


def _compare_counter_gnmi_cli(
    gnmi_value: float, cli_value: Optional[float], tolerance_percent: float
) -> Optional[bool]:
    """Compare a monotonic gNMI counter against a later CLI read.

    Returns ``None`` when the CLI value is unavailable (best-effort leg skipped), ``True`` when
    the values agree (CLI read after the gNMI window may legitimately be higher for a counter),
    otherwise ``False``.
    """
    if cli_value is None:
        return None
    if cli_value >= gnmi_value:
        return True
    if cli_value == 0:
        return gnmi_value == 0
    deviation = OtelDataValidations().get_percentage_deviation(cli_value, gnmi_value)
    return deviation <= tolerance_percent


# SSIM system_gnmi_to_otel_metric / system_counter_cli_key: on Cumulus these
# control-plane counters are Linux kernel netstat values, exported over OTEL as
# node_exporter ``node_netstat_*`` and read via ``nstat``. There is no gNMI /
# NVUE control-plane-traffic path for them on this platform.
# attr -> (OTEL metric, nstat CLI key)
SYSTEM_NETSTAT_SOURCES = {
    "cp_out_ipv4_pkts": ("node_netstat_Ip_InDelivers", "IpInDelivers"),
    "cp_out_ipv6_pkts": ("node_netstat_Ip6_InDelivers", "Ip6InDelivers"),
}


def _control_plane_traffic_counters_cli(dut) -> Dict[str, float]:
    """SSIM ``get_control_plane_traffic_counters``: flat kernel counters via ``nstat -az``.

    ``-a`` dumps absolute values and ``-z`` avoids resetting the history file, so the
    returned counts are cumulative and comparable to the OTEL counter snapshot.
    """
    out = dut.run_cmd("nstat -az 2>/dev/null", validate=False, print_output=False)
    counters: Dict[str, float] = {}
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("-").isdigit():
            counters[parts[0]] = float(parts[1])
    return counters


def _otel_netstat_latest(netstat_map: Dict[str, Any], metric: str) -> Optional[float]:
    """Latest unlabeled datapoint for ``metric`` in parsed ``netstat_stats``.

    These counters are compared to unlabeled ``nstat`` totals, so only the ``No Attr``
    series is used. Other attribute keys are not consulted.
    """
    per_metric = (netstat_map or {}).get(metric) or {}
    if not isinstance(per_metric, dict):
        return None
    series = per_metric.get("No Attr")
    if not (isinstance(series, list) and series):
        return None
    try:
        return float(series[-1])
    except (TypeError, ValueError):
        return None


def validate_system_out_packets_coexistence(dut, *, tolerance_percent: float = 10.0) -> None:
    """SSIM component/system ``test_06`` system control-plane out-packets (OTEL vs CLI).

    On Cumulus these ``cp_out_ipv*_pkts`` counters are Linux netstat IP InDelivers, which
    are exported over OTEL as ``node_netstat_Ip_InDelivers`` / ``node_netstat_Ip6_InDelivers``
    and read via ``nstat -az`` (``IpInDelivers`` / ``Ip6InDelivers``). There is no gNMI or
    NVUE control-plane-traffic path for them on this platform, so this validates the OTEL vs
    CLI leg only (SSIM ``validate_system_otel_cli_from_cache``).

    These are monotonic cumulative counters and the CLI read happens *after* the cached OTEL
    datapoint, so ``cli >= otel`` is expected and passes; the comparison is directional via
    :func:`_compare_counter_gnmi_cli` (only a CLI value below the OTEL snapshot beyond
    ``tolerance_percent`` fails) so normal counter growth under traffic does not flap the test.
    """
    otel = telemetryCache.get_data("otel") or {}
    netstat = otel.get("netstat_stats") or {}
    with allure.step("Read CLI control-plane traffic counters (nstat -az)"):
        cli_map = _control_plane_traffic_counters_cli(dut)
    failures: List[str] = []
    for attr, (metric, cli_key) in SYSTEM_NETSTAT_SOURCES.items():
        with allure.step("Validate system %s (OTEL %s vs CLI %s)" % (attr, metric, cli_key)):
            otel_val = _otel_netstat_latest(netstat, metric)
            cli_val = cli_map.get(cli_key)
            if otel_val is None:
                failures.append("%s: OTEL metric %s missing from netstat_stats" % (attr, metric))
                continue
            if cli_val is None:
                failures.append("%s: CLI counter %s missing from nstat -az" % (attr, cli_key))
                continue
            # Monotonic cumulative counter: the CLI read happens after the OTEL snapshot, so
            # cli >= otel is the normal case and must PASS. Only a CLI value that is *below* the
            # OTEL snapshot beyond tolerance (counter regressed / OTEL over-reported) is a failure.
            ok = _compare_counter_gnmi_cli(otel_val, cli_val, tolerance_percent)
            allure.attach(
                attr,
                "otel=%s cli=%s tol%%=%s ok=%s" % (otel_val, cli_val, tolerance_percent, ok),
            )
            if ok is False:
                failures.append(
                    "%s: OTEL=%s vs CLI=%s — CLI below OTEL beyond tol%%=%s"
                    % (attr, otel_val, cli_val, tolerance_percent)
                )
    if failures:
        pytest.fail("; ".join(failures))


def _parse_proc_diskstats(dut) -> Dict[str, Dict[str, float]]:
    """``/proc/diskstats`` device -> storage read-bytes / write-seconds."""
    out = dut.run_cmd("cat /proc/diskstats 2>/dev/null", validate=False, print_output=False)
    parsed: Dict[str, Dict[str, float]] = {}
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) < 11:
            continue
        name = parts[2]
        try:
            sectors_read = int(parts[5])
            ms_writing = int(parts[10])
        except ValueError:
            continue
        parsed[name] = {
            "storage_read_bytes": float(sectors_read * 512),
            "storage_write_seconds": float(ms_writing) / 1000.0,
        }
    return parsed


def _is_whole_disk_device(name: str) -> bool:
    """Skip partitions (``sda1``, ``nvme0n1p1``) when falling back to a kernel disk."""
    if name.startswith("nvme"):
        return "p" not in name
    if name.startswith(("sd", "vd")):
        return bool(name) and name[-1].isalpha()
    return False


def _resolve_diskstats_device(devices: Sequence[str], component_name: str) -> Optional[str]:
    """Map a gNMI/NVUE component name onto a ``/proc/diskstats`` device.

    Exact match first, then case-insensitive / substring, then the first whole
    ``nvme*`` / ``sd*`` / ``vd*`` disk. Inventory labels like ``Storage`` do not
    match a kernel row on their own.
    """
    if component_name in devices:
        return component_name
    low = component_name.lower()
    for name in devices:
        if name.lower() == low:
            return name
    for name in devices:
        nlow = name.lower()
        if nlow in low or low in nlow:
            return name
    whole = [name for name in devices if _is_whole_disk_device(name)]
    for prefix in ("nvme", "sd", "vd"):
        for name in whole:
            if name.startswith(prefix):
                return name
    return whole[0] if whole else None


def _disk_stats_cli(dut, device: str) -> Tuple[Dict[str, float], Optional[str]]:
    """Storage counters from ``/proc/diskstats`` (SSIM ``get_disk_stats``).

    Returns ``(counters, resolved_kernel_device)``. ``resolved_kernel_device`` is
    None when diskstats has no usable whole-disk row.
    """
    parsed = _parse_proc_diskstats(dut)
    resolved = _resolve_diskstats_device(tuple(parsed), device)
    if resolved is None:
        return {}, None
    return parsed[resolved], resolved


def validate_component_storage_coexistence(dut, *, tolerance_percent: float = 20.0) -> None:
    """SSIM component/system ``test_07`` storage counters (gNMI presence + CLI cross-check).

    OTEL-vs-CLI uses ``node_disk_*``, which the NGTS ``otel`` cache does not parse; that
    leg is skipped. CLI reads ``/proc/diskstats`` after mapping the gNMI component name
    onto a kernel device (exact / substring / first whole ``nvme*`` or ``sd*`` disk).
    gNMI presence and CLI comparisons are counted separately so a missing diskstats
    row cannot pass as a completed CLI cross-check.
    """
    skip_unless_gnmi_coexistence("gnmi-series-component", context="component storage GNMI vs CLI")
    with allure.step("Discover storage component and read CLI disk stats"):
        series = telemetryCache.get_data("gnmi-series-component")
        component_name = discover_storage_component_name(dut)
        cli_map, cli_device = _disk_stats_cli(dut, component_name)
        allure.attach(
            "component=%s diskstats_device=%s" % (component_name, cli_device),
            "storage_cli_device",
        )
    failures: List[str] = []
    gnmi_present = 0
    cli_compared = 0
    for attr in COMPONENT_GNMI_ATTRS:
        xpath = _component_xpath(attr, component_name=component_name)
        gnmi_value = latest_gnmi_numeric_for_xpath(series, xpath)
        with allure.step("Validate component %s (gNMI present + CLI cross-check)" % attr):
            if gnmi_value is None:
                failures.append("%s: not exported over gNMI (xpath=%s)" % (attr, xpath))
                continue
            gnmi_present += 1
            cli_value = cli_map.get(attr)
            ok = _compare_counter_gnmi_cli(gnmi_value, cli_value, tolerance_percent)
            allure.attach(
                attr,
                "gnmi=%s cli=%s component=%s diskstats=%s ok=%s"
                % (gnmi_value, cli_value, component_name, cli_device, ok),
            )
            if ok is None:
                continue
            cli_compared += 1
            if ok is False:
                failures.append(
                    "%s: gNMI=%s vs CLI=%s beyond %s%%" % (attr, gnmi_value, cli_value, tolerance_percent)
                )
    allure.attach(
        "framework_limitation",
        "OTEL vs CLI skipped: node_disk_* not parsed into NGTS otel cache",
    )
    if gnmi_present == 0:
        pytest.skip("No component storage counters present in gNMI series")
    if cli_compared == 0:
        pytest.skip(
            "gNMI storage counters present for component %r but /proc/diskstats has no "
            "usable disk row (resolved %r); CLI cross-check did not run"
            % (component_name, cli_device)
        )
    if failures:
        pytest.fail("; ".join(failures))


def _chassis_cli_data(dut) -> Dict[str, Any]:
    """CHASSIS CLI payload from ``decode-syseeprom`` / ``dmidecode`` (SSIM ``get_chassis_cli_data``)."""
    data: Dict[str, Any] = {"chassis_type": "CHASSIS"}
    raw = dut.run_cmd(
        "sudo decode-syseeprom --json 2>/dev/null",
        validate=False,
        print_output=False,
    )
    tlv: Dict[str, Any] = {}
    try:
        payload = json.loads(raw) if raw and raw.strip().startswith("{") else {}
        tlv = payload.get("tlv", {}) if isinstance(payload, dict) else {}
    except (json.JSONDecodeError, TypeError):
        tlv = {}

    def _tlv_value(key: str) -> Optional[str]:
        entry = tlv.get(key, {}) if isinstance(tlv, dict) else {}
        return entry.get("value") if isinstance(entry, dict) else None

    data["chassis_serial_no"] = _tlv_value("Serial Number")
    data["chassis_part_no"] = _tlv_value("Part Number")
    data["chassis_model_name"] = _tlv_value("Product Name")
    hw = dut.run_cmd(
        "sudo dmidecode -s chassis-version 2>/dev/null",
        validate=False,
        print_output=False,
    )
    data["chassis_hardware_version"] = (hw or "").strip() or None
    return data


def _validate_platform_hw_info_otel_cli(
    *,
    expected_type: str,
    expected_name: str,
    cli_by_otel_field: Dict[str, Any],
) -> None:
    """Shared ``nvswitch_platform_info_hw_details`` OTEL-vs-CLI comparison for a component.

    Locates the metric attribute-set for ``(type=expected_type, name=expected_name)``, asserts the
    presence flag is ``1``, then compares each OTEL hw-detail attribute against the CLI value keyed
    by the same OTEL field name (``serial_no`` / ``part_no`` / ``model_name`` / ``hardware_version``
    / ``type``). Fields with a missing OTEL attribute or CLI value are skipped (best-effort leg).
    """
    otel_data = telemetryCache.get_data("otel") or {}
    platform_stats = (otel_data or {}).get("platform_stats") or {}
    metric_map = platform_stats.get("nvswitch_platform_info_hw_details") or {}
    if not isinstance(metric_map, dict) or not metric_map:
        pytest.skip("nvswitch_platform_info_hw_details not exported on this DUT")
    target_attrs: Dict[str, Any] = {}
    target_key = None
    for attr_key in metric_map:
        if not isinstance(attr_key, tuple):
            continue
        attrs = {
            str(attr_key[i]): attr_key[i + 1] for i in range(0, len(attr_key) - 1, 2)
        }
        if str(attrs.get("type")) == expected_type and str(attrs.get("name")) == expected_name:
            target_key = attr_key
            target_attrs = attrs
            break
    assert target_key is not None, (
        "%s attribute set (name=%s) not found in nvswitch_platform_info_hw_details"
        % (expected_type, expected_name)
    )

    values = metric_map[target_key]
    d_otel = values[-1] if isinstance(values, list) and values else values
    assert d_otel == 1, (
        "nvswitch_platform_info_hw_details %s value=%s (expected 1)" % (expected_type, d_otel)
    )

    failures: List[str] = []
    with allure.step("Validate %s OTEL platform-info hw details vs CLI" % expected_type):
        for otel_field, cli_value in cli_by_otel_field.items():
            otel_value = target_attrs.get(otel_field)
            if otel_value is None or cli_value in (None, ""):
                allure.attach(otel_field, "otel=%s cli=%s (skipped)" % (otel_value, cli_value))
                continue
            if str(otel_value) != str(cli_value):
                failures.append("%s: OTEL=%s CLI=%s" % (otel_field, otel_value, cli_value))
    if failures:
        pytest.fail("%s platform hw info mismatch: %s" % (expected_type, "; ".join(failures)))


def validate_chassis_platform_hw_info(dut) -> None:
    """SSIM component/system ``test_08`` CHASSIS ``nvswitch_platform_info_hw_details`` OTEL vs CLI.

    The chassis gNMI leg is not collected by the coexistence jobs (storage-only), so only the
    OTEL platform-info-hw-details vs CLI comparison is performed (NGTS infra scope).
    """
    with allure.step("Collect CHASSIS CLI hw details (decode-syseeprom / dmidecode)"):
        cli = _chassis_cli_data(dut)
    _validate_platform_hw_info_otel_cli(
        expected_type="CHASSIS",
        expected_name="CHASSIS",
        cli_by_otel_field={
            "serial_no": cli.get("chassis_serial_no"),
            "part_no": cli.get("chassis_part_no"),
            "model_name": cli.get("chassis_model_name"),
            "hardware_version": cli.get("chassis_hardware_version"),
            "type": cli.get("chassis_type"),
        },
    )


def _cli_interface_in_errors(dut, interface: str) -> int:
    """Return ``in-errors`` from ``nv show interface <iface> counters``.

    Parse failures fail the test; a genuine zero counter is returned only when
    the field is present in the parsed payload.
    """
    from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface

    try:
        counters = Interface(None, port_name=interface).counters.parse_show(
            dut_engine=dut
        )
    except Exception as exc:  # noqa: BLE001
        pytest.fail("CLI in-errors parse failed for %s: %s" % (interface, exc))
    if not isinstance(counters, dict):
        pytest.fail(
            "CLI in-errors parse for %s did not return a dict: %r" % (interface, counters)
        )
    for field in ("in-errors", "rx-errors", "errors"):
        if field in counters:
            return int(counters.get(field) or 0)
    link = counters.get("link")
    if isinstance(link, dict):
        for field in ("in-errors", "rx-errors", "errors"):
            if field in link:
                return int(link.get(field) or 0)
    pytest.fail(
        "CLI in-errors field missing for %s in counters payload: %s"
        % (interface, sorted(counters.keys()) if isinstance(counters, dict) else counters)
    )


def validate_interface_in_errors_otel_cli(
    dut,
    *,
    interface: str,
    tolerance_percent: float = 20.0,
) -> None:
    """OTEL vs CLI for ``in_errors`` (SSIM test_11 OTEL portion only)."""
    metric = INTERFACE_GNMI_TO_OTEL_METRIC["in_errors"]
    otel_intf_stats = telemetryCache.get_data("otel").get("intf_stats") or {}

    otel_data_val = OtelDataValidations()
    if not otel_intf_stats:
        pytest.skip("intf_stats missing; interface OTEL export not present in cache")

    if metric not in otel_intf_stats:
        pytest.skip(f"{metric} not in OTEL export on mlx lab")

    series = otel_intf_stats[metric]
    matched_key = _matching_otel_series_key(series, interface=interface)
    if matched_key is None:
        pytest.skip(f"{metric} has no samples for interface {interface!r}")

    d_otel = round(mean(series[matched_key]))
    d_cli = _cli_interface_in_errors(dut, interface)

    with allure.step(f"Validate {metric} OTEL vs CLI in-errors"):
        allure.attach(
            "interface=%s otel=%s cli=%s" % (interface, d_otel, d_cli),
            "interface_in_errors_comparison",
        )
        deviation = otel_data_val.get_percentage_deviation(d_cli, d_otel)
        if deviation > tolerance_percent:
            pytest.fail(
                f"{metric} OTEL vs CLI failed: otel={d_otel}, cli={d_cli}, "
                f"deviation={deviation:.1f}% (max {tolerance_percent}%)"
            )


def validate_interface_transmit_pkts_otel_cli(
    dut,
    *,
    interface: str,
    traffic_class: str = INTERFACE_QOS_TRANSMIT_PKTS_DEFAULT_TC,
    tolerance_percent: float = 20.0,
) -> None:
    """OTEL vs CLI for ``transmit_pkts`` / ``tx-frames`` (SSIM test_10 / test_12)."""
    metric = INTERFACE_GNMI_TO_OTEL_METRIC["transmit_pkts"]
    otel_intf_stats = telemetryCache.get_data("otel").get("intf_stats") or {}
    cli_egress = telemetryCache.get_data("cli").get("egress_stats") or {}

    otel_data_val = OtelDataValidations()
    if not otel_intf_stats:
        pytest.skip("intf_stats missing; interface OTEL export not present in cache")

    if metric not in otel_intf_stats:
        pytest.skip(f"{metric} not in OTEL export on mlx lab")

    series = otel_intf_stats[metric]
    matched_key = _matching_otel_series_key(
        series, interface=interface, traffic_class=traffic_class
    )
    if matched_key is None:
        pytest.skip(
            f"{metric} has no samples for interface {interface!r} tc={traffic_class!r}"
        )

    d_otel = round(mean(series[matched_key]))
    egress = cli_egress.get(interface, {})
    tc_stats = egress.get(traffic_class, egress) if isinstance(egress, dict) else {}
    if not isinstance(tc_stats, dict):
        tc_stats = {}
    d_cli = int(
        tc_stats.get(
            INTERFACE_QOS_TRANSMIT_PKTS_CLI_FIELD,
            tc_stats.get("tx_frames", 0),
        ) or
        0
    )

    with allure.step(
        f"Validate {metric} OTEL vs CLI egress-queue tx-frames (tc={traffic_class})"
    ):
        allure.attach(
            "interface=%s tc=%s otel=%s cli=%s"
            % (interface, traffic_class, d_otel, d_cli),
            "qos_transmit_pkts_comparison",
        )
        deviation = otel_data_val.get_percentage_deviation(d_cli, d_otel)
        if deviation > tolerance_percent:
            pytest.fail(
                f"{metric} OTEL vs CLI failed: otel={d_otel}, cli={d_cli}, "
                f"deviation={deviation:.1f}% (max {tolerance_percent}%)"
            )


# ---------------------------------------------------------------------------
# SSIM ``Test_Telemetry_Component_System_Validation_Coexistence`` platform hw info
# (MR 344332 chassis pattern; test_09 CPU; test_10 PSU)
# ---------------------------------------------------------------------------

# SSIM test_09 CLI component job (``component_type='CPU'``) cache key.
CPU_CLI_CACHE_KEY = "cpu-cli"

# SSIM test_10 CLI component job (``component_type='PSU'``) cache key.
PSU_CLI_CACHE_KEY = "psu-cli"
# OTEL ``nvswitch_platform_info_hw_details`` type labels seen for a PSU sample.
_PSU_OTEL_TYPE_CANDIDATES: Tuple[str, ...] = ("PSU", "POWER_SUPPLY")

# NVUE platform component/inventory field names per SSIM ``cpu_*`` field.
_CPU_CLI_NVUE_FIELD_MAP: Dict[str, Tuple[str, ...]] = {
    "cpu_serial_no": ("serial-no", "serial-number", "serial"),
    "cpu_part_no": ("part-no", "part-number"),
    "cpu_model_name": ("model-name", "model"),
    "cpu_hardware_version": ("hardware-version", "hw-revision"),
    "cpu_type": ("type",),
}

# NVUE platform component/inventory field names per SSIM ``psu_*`` field.
_PSU_CLI_NVUE_FIELD_MAP: Dict[str, Tuple[str, ...]] = {
    "psu_serial_no": ("serial-no", "serial-number", "serial"),
    "psu_part_no": ("part-no", "part-number"),
    "psu_model_name": ("model-name", "model"),
    "psu_hardware_version": ("hardware-version", "hw-revision"),
    "psu_type": ("type",),
}

_CLI_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "-",
        "n/a",
        "na",
        "not specified",
        "none",
        "unknown",
        "to be filled by o.e.m.",
        "not provided",
        "not available",
        "not present",
    }
)


def _dmi_first_processor_fields(raw: str) -> Dict[str, str]:
    """Parse the first Processor Information block from ``dmidecode -t processor``."""
    fields: Dict[str, str] = {}
    in_processor = False
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Processor Information"):
            if fields:
                break
            in_processor = True
            continue
        if not in_processor:
            continue
        if stripped.startswith("Handle ") and fields:
            break
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        if key not in ("Version", "Serial Number", "Part Number"):
            continue
        if val.lower() in _CLI_PLACEHOLDER_VALUES:
            continue
        fields.setdefault(key, val)
    return fields


def _platform_hw_info_otel_attrs(
    *,
    expected_type: str,
    expected_name: str,
    expected_types: Optional[Sequence[str]] = None,
    match_name_normalized: bool = False,
) -> Dict[str, Any]:
    """``nvswitch_platform_info_hw_details`` attribute set for one component.

    Locates the metric attribute-set for ``(type in expected_types, name=expected_name)`` and
    asserts the presence flag is ``1``, skipping when the metric is not exported by the DUT.
    ``match_name_normalized`` compares names ignoring case/spacing, since the OTEL label may read
    ``PSU 1`` where NVUE inventory keys it ``PSU1``.
    """
    type_candidates = tuple(expected_types) if expected_types else (expected_type,)
    otel_data = telemetryCache.get_data("otel") or {}
    platform_stats = (otel_data or {}).get("platform_stats") or {}
    metric_map = platform_stats.get("nvswitch_platform_info_hw_details") or {}
    if not isinstance(metric_map, dict) or not metric_map:
        pytest.skip("nvswitch_platform_info_hw_details not exported on this DUT")
    target_attrs: Dict[str, Any] = {}
    target_key = None
    for attr_key in metric_map:
        if not isinstance(attr_key, tuple):
            continue
        attrs = _attr_tuple_to_pairs(attr_key)
        name = str(attrs.get("name"))
        if match_name_normalized:
            if normalize_component_name(name) != normalize_component_name(expected_name):
                continue
        elif name != expected_name:
            continue
        if str(attrs.get("type")) not in type_candidates:
            continue
        target_key = attr_key
        target_attrs = attrs
        break
    assert target_key is not None, (
        "%s attribute set (name=%s, types=%s) not found in nvswitch_platform_info_hw_details"
        % (expected_type, expected_name, list(type_candidates))
    )

    values = metric_map[target_key]
    d_otel = values[-1] if isinstance(values, list) and values else values
    assert d_otel == 1, (
        "nvswitch_platform_info_hw_details %s value=%s (expected 1)" % (expected_type, d_otel)
    )
    return target_attrs


def _validate_platform_hw_info_otel_cli(
    *,
    expected_type: str,
    expected_name: str,
    cli_by_otel_field: Dict[str, Any],
    expected_types: Optional[Sequence[str]] = None,
    match_name_normalized: bool = False,
) -> None:
    """Shared ``nvswitch_platform_info_hw_details`` OTEL-vs-CLI comparison for a component.

    Compares each OTEL hw-detail attribute against the CLI value keyed by the same OTEL field
    name (``serial_no`` / ``part_no`` / ``model_name`` / ``hardware_version`` / ``type``). Fields
    with a missing OTEL attribute or CLI value are skipped (best-effort leg).
    """
    target_attrs = _platform_hw_info_otel_attrs(
        expected_type=expected_type,
        expected_name=expected_name,
        expected_types=expected_types,
        match_name_normalized=match_name_normalized,
    )
    failures: List[str] = []
    with allure.step("Validate %s OTEL platform-info hw details vs CLI" % expected_type):
        for otel_field, cli_value in cli_by_otel_field.items():
            otel_value = target_attrs.get(otel_field)
            if otel_value is None or cli_value in (None, ""):
                allure.attach(
                    otel_field,
                    "otel=%s cli=%s (skipped)" % (otel_value, cli_value),
                )
                continue
            if str(otel_value) != str(cli_value):
                failures.append("%s: OTEL=%s CLI=%s" % (otel_field, otel_value, cli_value))
        if failures:
            pytest.fail(
                "%s OTEL vs CLI platform hw info mismatch: %s"
                % (expected_type, "; ".join(failures))
            )


def _first_present_value(payload: Dict[str, Any], keys: Sequence[str]) -> Optional[str]:
    """First non-placeholder value in ``payload`` for any of ``keys``."""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in _CLI_PLACEHOLDER_VALUES:
            return text
    return None


def _flatten_cli_leaves(payload: Any, depth: int = 0) -> Dict[str, str]:
    """Leaf ``key -> value`` map of a NVUE JSON entry; first occurrence of a key wins."""
    leaves: Dict[str, str] = {}
    if not isinstance(payload, dict) or depth > 4:
        return leaves
    for key, value in payload.items():
        if isinstance(value, dict):
            for nested_key, nested_value in _flatten_cli_leaves(value, depth + 1).items():
                leaves.setdefault(nested_key, nested_value)
        elif value is not None and not isinstance(value, (list, tuple)):
            leaves.setdefault(str(key), str(value))
    return leaves


def _find_cpu_component_entry(payload: Any, depth: int = 0) -> Dict[str, Any]:
    """CPU component sub-dict inside a NVUE platform inventory/component payload.

    Only entries keyed by a ``CPU*`` component name are matched, except at the top level where a
    component-specific show command (``nv show ... CPU``) returns the entry itself.
    """
    if not isinstance(payload, dict) or depth > 4:
        return {}
    for key, entry in payload.items():
        if isinstance(entry, dict) and str(key).strip().upper().startswith("CPU"):
            return entry
    if depth == 0 and any(
        cli_key in payload
        for cli_keys in _CPU_CLI_NVUE_FIELD_MAP.values()
        for cli_key in cli_keys
    ):
        return payload
    for entry in payload.values():
        found = _find_cpu_component_entry(entry, depth + 1)
        if found:
            return found
    return {}


def _platform_inventory_parse_show(dut, op_param: str = "") -> Any:
    """``nv show platform inventory [op_param]`` via ``Platform().inventory.parse_show``."""
    try:
        return Platform().inventory.parse_show(op_param=op_param, dut_engine=dut)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Platform().inventory.parse_show(%r) failed: %s", op_param, exc
        )
        return None


def _nvue_cpu_component_payload(dut) -> Tuple[Dict[str, Any], str]:
    """CPU entry from NVUE platform inventory (``Platform().inventory``)."""
    attempts = (
        (CPU_COMPONENT_NAME, "platform.inventory.parse_show(%s)" % CPU_COMPONENT_NAME),
        ("", "platform.inventory.parse_show()"),
    )
    for op_param, source in attempts:
        payload = _platform_inventory_parse_show(dut, op_param)
        if not isinstance(payload, dict) or not payload:
            continue
        entry = _find_cpu_component_entry(payload)
        if entry:
            return entry, source
    return {}, ""


def collect_cpu_cli_component_data(dut) -> Dict[str, Any]:
    """SSIM test_09 CLI component job (``component_type='CPU'``) → ``cpu-cli`` payload.

    The NVUE platform component/inventory tree is the only comparison source: gNMI and OTEL
    export the platform component view of the CPU (board model), which is a different value
    than the processor identity ``dmidecode`` reports. ``dmidecode`` output is attached for
    context only and never compared.
    """
    with allure.step("CLI: collect CPU platform component hw details (component_type=CPU)"):
        nvue, source_cmd = _nvue_cpu_component_payload(dut)
        leaves = _flatten_cli_leaves(nvue)
        data: Dict[str, Any] = {
            field_key: _first_present_value(leaves, cli_keys)
            for field_key, cli_keys in _CPU_CLI_NVUE_FIELD_MAP.items()
        }
        if not data.get("cpu_type"):
            data["cpu_type"] = CPU_COMPONENT_NAME
        data["cli_source"] = source_cmd
        dmi_raw = dut.run_cmd(
            "sudo dmidecode -t processor 2>/dev/null",
            validate=False,
            print_output=False,
        )
        allure.attach(
            "cpu_cli_payload",
            "source=%s nvue_leaves=%s dmidecode=%s"
            % (source_cmd or "<none>", leaves, _dmi_first_processor_fields(dmi_raw or "")),
        )
        return data


def _cpu_cli_cache_payload() -> Dict[str, Any]:
    """SSIM test_09 step 2 — assert the ``cpu-cli`` payload was collected."""
    assert_telemetry_cache_has_data(
        [("CPU CLI", CPU_CLI_CACHE_KEY)],
        context="CPU platform hw info",
    )
    payload = telemetryCache.get_data(CPU_CLI_CACHE_KEY)
    assert isinstance(payload, dict), (
        "%s cache payload is not a dict: %r" % (CPU_CLI_CACHE_KEY, payload)
    )
    return payload


def _validate_cpu_platform_hw_info_gnmi_cli(cli: Dict[str, Any]) -> None:
    """SSIM test_09 gNMI vs CLI for CPU OpenConfig component state leaves."""
    skip_unless_gnmi_coexistence(
        CPU_COMPONENT_GNMI_SERIES_KEY,
        context="CPU platform hw info gNMI vs CLI",
    )
    series = telemetryCache.get_data(CPU_COMPONENT_GNMI_SERIES_KEY)
    failures: List[str] = []
    exported = 0
    compared = 0
    with allure.step("Validate CPU gNMI state leaves vs CLI"):
        for attr in CPU_PLATFORM_HW_GNMI_ATTRS:
            xpath = _component_xpath(attr, component_name=CPU_COMPONENT_NAME)
            gnmi_value = latest_gnmi_leaf_for_xpath(series, xpath)
            cli_value = cli.get(attr)
            if gnmi_value is None:
                failures.append("%s: not exported over gNMI (xpath=%s)" % (attr, xpath))
                continue
            exported += 1
            if cli_value in (None, ""):
                allure.attach(
                    attr,
                    "gnmi=%s cli=%s xpath=%s (cli skipped)" % (gnmi_value, cli_value, xpath),
                )
                continue
            compared += 1
            if str(gnmi_value) != str(cli_value):
                failures.append(
                    "%s: gNMI=%s CLI=%s (xpath=%s)" % (attr, gnmi_value, cli_value, xpath)
                )
            else:
                allure.attach(
                    attr,
                    "gnmi=%s cli=%s xpath=%s" % (gnmi_value, cli_value, xpath),
                )
        if exported == 0:
            pytest.skip("CPU gNMI vs CLI: no platform hw-info leaves present in gNMI component series")
        if failures:
            pytest.fail("CPU gNMI vs CLI mismatch: %s" % ("; ".join(failures),))
    if compared == 0:
        logger.warning(
            'No CPU hw-info field is reported by the NVUE platform CLI (source=%r); '
            'gNMI vs CLI leg had nothing to compare',
            cli.get("cli_source") or "<none>",
        )


def _validate_cpu_platform_hw_info_gnmi_otel() -> None:
    """gNMI vs OTEL for the CPU hw-info leaves — both export the platform component view."""
    skip_unless_gnmi_coexistence(
        CPU_COMPONENT_GNMI_SERIES_KEY,
        context="CPU platform hw info gNMI vs OTEL",
    )
    otel_attrs = _platform_hw_info_otel_attrs(expected_type="CPU", expected_name=CPU_COMPONENT_NAME)
    series = telemetryCache.get_data(CPU_COMPONENT_GNMI_SERIES_KEY)
    failures: List[str] = []
    with allure.step("Validate CPU gNMI state leaves vs OTEL platform-info hw details"):
        for attr in CPU_PLATFORM_HW_GNMI_ATTRS:
            otel_field = attr[len("cpu_"):]
            otel_value = otel_attrs.get(otel_field)
            gnmi_value = latest_gnmi_leaf_for_xpath(
                series,
                _component_xpath(attr, component_name=CPU_COMPONENT_NAME),
            )
            if otel_value is None or gnmi_value is None:
                allure.attach(attr, "gnmi=%s otel=%s (skipped)" % (gnmi_value, otel_value))
                continue
            if str(gnmi_value) != str(otel_value):
                failures.append("%s: gNMI=%s OTEL=%s" % (attr, gnmi_value, otel_value))
            else:
                allure.attach(attr, "gnmi=%s otel=%s" % (gnmi_value, otel_value))
        if failures:
            pytest.fail(
                "CPU gNMI vs OTEL platform hw info mismatch: %s" % ("; ".join(failures),)
            )


def validate_cpu_platform_hw_info() -> None:
    """SSIM component/system ``test_09`` CPU platform hw info (gNMI + OTEL vs CLI).

    Uses the suite cache populated during collection: the ``cpu-cli`` component payload,
    the dedicated ``gnmi-series-component-cpu`` subscription (five ``state`` leaves), and
    OTEL ``nvswitch_platform_info_hw_details`` for ``type=CPU`` / ``name=CPU``.
    """
    cli = _cpu_cli_cache_payload()
    _validate_cpu_platform_hw_info_gnmi_cli(cli)
    _validate_cpu_platform_hw_info_gnmi_otel()
    _validate_platform_hw_info_otel_cli(
        expected_type="CPU",
        expected_name="CPU",
        cli_by_otel_field={
            "serial_no": cli.get("cpu_serial_no"),
            "part_no": cli.get("cpu_part_no"),
            "model_name": cli.get("cpu_model_name"),
            "hardware_version": cli.get("cpu_hardware_version"),
            "type": cli.get("cpu_type"),
        },
    )


def _find_named_component_entry(
    payload: Any,
    *,
    component_name: str,
    depth: int = 0,
) -> Dict[str, Any]:
    """Named component sub-dict inside a NVUE platform inventory/component payload."""
    if not isinstance(payload, dict) or depth > 4:
        return {}
    target = component_name.strip().upper()
    for key, entry in payload.items():
        if isinstance(entry, dict) and str(key).strip().upper() == target:
            return entry
    if depth == 0 and any(
        cli_key in payload
        for cli_keys in _PSU_CLI_NVUE_FIELD_MAP.values()
        for cli_key in cli_keys
    ):
        return payload
    for entry in payload.values():
        found = _find_named_component_entry(
            entry, component_name=component_name, depth=depth + 1
        )
        if found:
            return found
    return {}


def _nvue_psu_component_payload(dut, psu_name: str) -> Tuple[Dict[str, Any], str]:
    """PSU entry from NVUE platform inventory (``Platform().inventory``)."""
    attempts = (
        (psu_name, "platform.inventory.parse_show(%s)" % psu_name),
        ("", "platform.inventory.parse_show()"),
    )
    for op_param, source in attempts:
        payload = _platform_inventory_parse_show(dut, op_param)
        if not isinstance(payload, dict) or not payload:
            continue
        entry = _find_named_component_entry(payload, component_name=psu_name)
        if entry:
            return entry, source
    return {}, ""


def collect_psu_cli_component_data(dut, psu_name: Optional[str] = None) -> Dict[str, Any]:
    """SSIM test_10 CLI component job (``component_type='PSU'``) → ``psu-cli`` payload.

    Collects the five inventory fields for the same PSU name used by the dedicated gNMI
    ``component-psu`` subscription (chosen once during job build).
    """
    name = psu_name or get_selected_psu_component_name()
    with allure.step(
        "CLI: collect PSU platform component hw details (component_type=PSU, name=%s)"
        % (name or "<none>",)
    ):
        if not name:
            allure.attach("psu_cli_payload", "no present PSU selected")
            return {"psu_name": None}
        nvue, source_cmd = _nvue_psu_component_payload(dut, name)
        leaves = _flatten_cli_leaves(nvue)
        data: Dict[str, Any] = {
            field_key: _first_present_value(leaves, cli_keys)
            for field_key, cli_keys in _PSU_CLI_NVUE_FIELD_MAP.items()
        }
        if not data.get("psu_type"):
            data["psu_type"] = "PSU"
        data["psu_name"] = name
        data["cli_source"] = source_cmd
        allure.attach(
            "psu_cli_payload",
            "psu=%s source=%s nvue_leaves=%s"
            % (name, source_cmd or "<none>", leaves),
        )
        return data


def _psu_cli_cache_payload() -> Dict[str, Any]:
    """SSIM test_10 — assert the ``psu-cli`` payload was collected for a chosen PSU."""
    assert_telemetry_cache_has_data(
        [("PSU CLI", PSU_CLI_CACHE_KEY)],
        context="PSU platform hw info",
    )
    payload = telemetryCache.get_data(PSU_CLI_CACHE_KEY)
    assert isinstance(payload, dict), (
        "%s cache payload is not a dict: %r" % (PSU_CLI_CACHE_KEY, payload)
    )
    if not payload.get("psu_name"):
        pytest.skip("No present PSU inventory component on this DUT")
    return payload


def _psu_gnmi_series() -> Dict[str, Any]:
    """The ``component-psu`` gNMI series, skipping when the DUT exported no PSU components."""
    skip_unless_gnmi_coexistence(
        PSU_COMPONENT_GNMI_SERIES_KEY,
        context="PSU platform hw info",
    )
    return telemetryCache.get_data(PSU_COMPONENT_GNMI_SERIES_KEY)


def _validate_psu_platform_hw_info_gnmi_cli(cli: Dict[str, Any]) -> None:
    """SSIM test_10 gNMI vs CLI for the chosen PSU OpenConfig component state leaves."""
    psu_name = str(cli.get("psu_name") or "")
    series = _psu_gnmi_series()
    failures: List[str] = []
    exported = 0
    compared = 0
    with allure.step("Validate PSU gNMI state leaves vs CLI (name=%s)" % psu_name):
        for attr in PSU_PLATFORM_HW_GNMI_ATTRS:
            leaf = PSU_STATE_LEAF_BY_ATTR[attr]
            gnmi_value, xpath = latest_gnmi_component_state_leaf(
                series, component_name=psu_name, leaf=leaf
            )
            cli_value = cli.get(attr)
            if gnmi_value is None:
                failures.append(
                    "%s: not exported over gNMI (component=%s leaf=%s)" % (attr, psu_name, leaf)
                )
                continue
            exported += 1
            if cli_value in (None, ""):
                allure.attach(
                    attr,
                    "gnmi=%s cli=%s xpath=%s (cli skipped)" % (gnmi_value, cli_value, xpath),
                )
                continue
            compared += 1
            if str(gnmi_value) != str(cli_value):
                failures.append(
                    "%s: gNMI=%s CLI=%s (xpath=%s)" % (attr, gnmi_value, cli_value, xpath)
                )
            else:
                allure.attach(
                    attr,
                    "gnmi=%s cli=%s xpath=%s" % (gnmi_value, cli_value, xpath),
                )
        if exported == 0:
            pytest.skip(
                "PSU gNMI vs CLI: %r has no hw-info state leaves in the gNMI component "
                "series (components seen: %s)"
                % (psu_name, gnmi_component_names_in_series(series))
            )
        if failures:
            pytest.fail("PSU gNMI vs CLI mismatch: %s" % ("; ".join(failures),))
    if compared == 0:
        logger.warning(
            "No PSU hw-info field is reported by the NVUE platform CLI (psu=%r source=%r); "
            "gNMI vs CLI leg had nothing to compare",
            psu_name,
            cli.get("cli_source") or "<none>",
        )


def _validate_psu_platform_hw_info_gnmi_otel(cli: Dict[str, Any]) -> None:
    """gNMI vs OTEL for the chosen PSU hw-info leaves."""
    psu_name = str(cli.get("psu_name") or "")
    series = _psu_gnmi_series()
    otel_attrs = _platform_hw_info_otel_attrs(
        expected_type="PSU",
        expected_name=psu_name,
        expected_types=_PSU_OTEL_TYPE_CANDIDATES,
        match_name_normalized=True,
    )
    failures: List[str] = []
    with allure.step(
        "Validate PSU gNMI state leaves vs OTEL platform-info hw details (name=%s)"
        % psu_name
    ):
        for attr in PSU_PLATFORM_HW_GNMI_ATTRS:
            otel_field = attr[len("psu_"):]
            otel_value = otel_attrs.get(otel_field)
            gnmi_value, _ = latest_gnmi_component_state_leaf(
                series,
                component_name=psu_name,
                leaf=PSU_STATE_LEAF_BY_ATTR[attr],
            )
            if otel_value is None or gnmi_value is None:
                allure.attach(attr, "gnmi=%s otel=%s (skipped)" % (gnmi_value, otel_value))
                continue
            if str(gnmi_value) != str(otel_value):
                failures.append("%s: gNMI=%s OTEL=%s" % (attr, gnmi_value, otel_value))
            else:
                allure.attach(attr, "gnmi=%s otel=%s" % (gnmi_value, otel_value))
        if failures:
            pytest.fail(
                "PSU gNMI vs OTEL platform hw info mismatch: %s" % ("; ".join(failures),)
            )


def validate_psu_platform_hw_info() -> None:
    """SSIM component/system ``test_10`` PSU platform hw info (gNMI + OTEL vs CLI).

    Uses the suite cache populated during collection: the ``psu-cli`` component payload for one
    randomly chosen present PSU, the dedicated ``gnmi-series-component-psu`` subscription (five
    ``state`` leaves), and OTEL ``nvswitch_platform_info_hw_details`` for that PSU name.
    """
    cli = _psu_cli_cache_payload()
    _validate_psu_platform_hw_info_gnmi_cli(cli)
    _validate_psu_platform_hw_info_gnmi_otel(cli)
    _validate_platform_hw_info_otel_cli(
        expected_type="PSU",
        expected_name=str(cli.get("psu_name")),
        expected_types=_PSU_OTEL_TYPE_CANDIDATES,
        match_name_normalized=True,
        cli_by_otel_field={
            "serial_no": cli.get("psu_serial_no"),
            "part_no": cli.get("psu_part_no"),
            "model_name": cli.get("psu_model_name"),
            "hardware_version": cli.get("psu_hardware_version"),
            "type": cli.get("psu_type"),
        },
    )


def validate_interface_multicast_pkts_otel_cli(
    dut,
    *,
    interface: str,
    tolerance_percent: float = 20.0,
) -> None:
    """OTEL vs CLI for ``in_multicast_pkts`` (SSIM test_06 OTEL portion only)."""
    _validate_interface_counter_otel_cli(
        dut,
        counter="in_multicast_pkts",
        interface=interface,
        tolerance_percent=tolerance_percent,
    )


def validate_interface_octets_otel_cli(
    dut,
    *,
    interface: str,
    tolerance_percent: float = 20.0,
) -> None:
    """OTEL vs CLI for ``in_octets`` (SSIM test_07 OTEL portion only)."""
    _validate_interface_counter_otel_cli(
        dut,
        counter="in_octets",
        interface=interface,
        tolerance_percent=tolerance_percent,
    )


def validate_interface_no_buffer_uc_dropped_pkts_otel_cli(
    dut,
    *,
    interface: str,
    traffic_class: str = INTERFACE_QOS_NO_BUFFER_UC_DEFAULT_TC,
    tolerance_percent: float = 20.0,
) -> None:
    """OTEL vs CLI for ``no_buffer_uc_dropped_pkts`` (SSIM test_08 OTEL portion only)."""
    metric = INTERFACE_GNMI_TO_OTEL_METRIC["no_buffer_uc_dropped_pkts"]
    otel_intf_stats = telemetryCache.get_data("otel").get("intf_stats") or {}
    cli_egress = telemetryCache.get_data("cli").get("egress_stats") or {}

    otel_data_val = OtelDataValidations()
    if not otel_intf_stats:
        pytest.skip("intf_stats missing; interface OTEL export not present in cache")

    if metric not in otel_intf_stats:
        pytest.skip(f"{metric} not in OTEL export on mlx lab")

    series = otel_intf_stats[metric]
    matched_key = _matching_otel_series_key(
        series, interface=interface, traffic_class=traffic_class
    )
    if matched_key is None:
        pytest.skip(
            f"{metric} has no samples for interface {interface!r} tc={traffic_class!r}"
        )

    d_otel = round(mean(series[matched_key]))
    egress = cli_egress.get(interface, {})
    tc_stats = egress.get(traffic_class, egress) if isinstance(egress, dict) else {}
    if not isinstance(tc_stats, dict):
        tc_stats = {}
    d_cli = int(
        tc_stats.get(
            INTERFACE_QOS_NO_BUFFER_UC_CLI_FIELD,
            tc_stats.get("tx_no_buffer_discard_uc", 0),
        ) or
        0
    )

    deviation = otel_data_val.get_percentage_deviation(d_cli, d_otel)
    if deviation > tolerance_percent:
        pytest.fail(
            f"{metric} OTEL vs CLI failed: otel={d_otel}, cli={d_cli}, "
            f"deviation={deviation:.1f}% (max {tolerance_percent}%)"
        )


def validate_interface_wred_dropped_pkts_otel_cli(
    dut,
    *,
    interface: str,
    traffic_class: str = INTERFACE_QOS_WRED_DEFAULT_TC,
    tolerance_percent: float = 20.0,
) -> None:
    """OTEL vs CLI for ``wred_dropped_pkts`` (SSIM test_09 OTEL portion only)."""
    metric = INTERFACE_GNMI_TO_OTEL_METRIC["wred_dropped_pkts"]
    otel_intf_stats = telemetryCache.get_data("otel").get("intf_stats") or {}
    cli_egress = telemetryCache.get_data("cli").get("egress_stats") or {}

    otel_data_val = OtelDataValidations()
    if not otel_intf_stats:
        pytest.skip("intf_stats missing; interface OTEL export not present in cache")

    if metric not in otel_intf_stats:
        pytest.skip(f"{metric} not in OTEL export on mlx lab")

    series = otel_intf_stats[metric]
    matched_key = _matching_otel_series_key(
        series, interface=interface, traffic_class=traffic_class
    )
    if matched_key is None:
        pytest.skip(
            f"{metric} has no samples for interface {interface!r} tc={traffic_class!r}"
        )

    d_otel = round(mean(series[matched_key]))
    egress = cli_egress.get(interface, {})
    tc_stats = egress.get(traffic_class, egress) if isinstance(egress, dict) else {}
    if not isinstance(tc_stats, dict):
        tc_stats = {}
    d_cli = int(
        tc_stats.get(
            INTERFACE_QOS_WRED_CLI_FIELD,
            tc_stats.get("tx_wred_discard", 0),
        ) or
        0
    )

    deviation = otel_data_val.get_percentage_deviation(d_cli, d_otel)
    if deviation > tolerance_percent:
        pytest.fail(
            f"{metric} OTEL vs CLI failed: otel={d_otel}, cli={d_cli}, "
            f"deviation={deviation:.1f}% (max {tolerance_percent}%)"
        )


def _nvue_version_gte(dut, version: str) -> bool:
    pkg_version = detect_nvue_python_pkg_version(dut)
    if not pkg_version:
        return False
    try:
        return tuple(int(part) for part in pkg_version.split(".")) >= tuple(
            int(part) for part in version.split(".")
        )
    except ValueError:
        return False


def _apply_coexistence_split_pipeline_root_config(dut) -> None:
    with allure.step("Apply coexistence split-pipeline root OTLP (SSIM OtelSplitPipelineMgmtVrfNoTLS)"):
        cmds = [
            "nv set system telemetry interface-stats export state enabled",
            "nv set system telemetry interface-stats sample-interval 3",
            "nv set system telemetry control-plane-stats export state enabled",
            "nv set system telemetry control-plane-stats sample-interval 2",
            "nv set system telemetry platform-stats export state enabled",
            "nv set system telemetry platform-stats export sample-interval 61",
            "nv set system telemetry platform-stats class cpu state enabled",
            "nv set system telemetry platform-stats class cpu sample-interval 70",
            "nv set system telemetry platform-stats class disk state enabled",
            "nv set system telemetry platform-stats class disk sample-interval 74",
            "nv set system telemetry platform-stats class environment-sensor state enabled",
            "nv set system telemetry platform-stats class environment-sensor sample-interval 65",
            "nv set system telemetry platform-stats class file-system state enabled",
            "nv set system telemetry platform-stats class file-system sample-interval 72",
            "nv set system telemetry platform-stats class memory state enabled",
            "nv set system telemetry platform-stats class memory sample-interval 75",
        ]
        for pg in range(8):
            cmds.append(
                "nv set system telemetry interface-stats ingress-buffer priority-group %d" % pg
            )
        for tc in range(8):
            cmds.append(
                "nv set system telemetry interface-stats egress-buffer traffic-class %d" % tc
            )
        for sp in ("0", "1", "2", "3", "7"):
            cmds.append("nv set system telemetry interface-stats switch-priority %s" % sp)
        if _nvue_version_gte(dut, "1.13.0"):
            cmds.append("nv set system telemetry interface-stats class debounce state enabled")
        for cmd in cmds:
            dut.run_cmd(cmd, validate=False)
        dut.run_cmd("nv config apply -y", validate=True)


def _enable_nvos_coexistence_phy(dut, *, stats_group_id: str) -> None:
    """Enable interface-stats class phy at root and under stats-group (validator expects PHY)."""
    from ngts.nvos_constants.constants_nvos import TelemetryConsts
    from ngts.nvos_tools.system.System import System
    from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import _enable_family_classes
    from ngts.tests_nvos.system.telemetry.otel.helpers import apply_telemetry_configuration

    system = System()
    with allure.step(
        "Enable NVOS interface-stats class phy (root + stats-group %s)" % (stats_group_id,)
    ):
        _enable_family_classes(
            system, dut, TelemetryConsts.INTERFACE_STATS, stats_group_id=None
        )
        _enable_family_classes(
            system,
            dut,
            TelemetryConsts.INTERFACE_STATS,
            stats_group_id=stats_group_id,
            enable_phy_under_stats_group=True,
        )
        apply_telemetry_configuration(system, dut, is_nvos=True)


def _apply_coexistence_root_telemetry_config(
    dut,
    collector_ips: Tuple[str, ...],
    *,
    is_nvos: bool,
    export_vrf: str,
) -> None:
    if is_nvos:
        from ngts.tests_nvos.system.telemetry.otel.cumulus.split_pipeline import (
            apply_split_pipeline_test01_stats_group_nvos,
        )

        with allure.step("Apply NVOS split-pipeline test_01 stats-group (coexistence)"):
            apply_split_pipeline_test01_stats_group_nvos(
                dut,
                collector_ips[0],
                export_vrf=export_vrf,
                stats_group_id=COEXISTENCE_NVOS_STATS_GROUP_ID,
            )
            _enable_nvos_coexistence_phy(
                dut, stats_group_id=COEXISTENCE_NVOS_STATS_GROUP_ID
            )
    else:
        _apply_coexistence_split_pipeline_root_config(dut)


def _apply_coexistence_persistence_root_config(
    dut,
    collector_ips: Tuple[str, ...],
) -> None:
    """Root OTLP intervals aligned with ``COEXISTENCE_SMOKE_TIME_GAPS`` / secured smoke."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import (
        _restore_otlp_mgmt_insecure_destinations,
    )

    with allure.step(
        "Apply coexistence persistence root OTLP "
        "(interface=2s, control=2s, buffer=60s, lldp=20s, platform≈60s)"
    ):
        _restore_otlp_mgmt_insecure_destinations(dut, collector_ips)
        cmds = [
            "nv set system telemetry interface-stats export state enabled",
            "nv set system telemetry interface-stats sample-interval 2",
            "nv set system telemetry control-plane-stats export state enabled",
            "nv set system telemetry control-plane-stats sample-interval 2",
            "nv set system telemetry buffer-stats export state enabled",
            "nv set system telemetry buffer-stats sample-interval 60",
            "nv set system telemetry lldp export state enabled",
            "nv set system telemetry lldp sample-interval 20",
            "nv set system telemetry platform-stats export state enabled",
            "nv set system telemetry platform-stats export sample-interval 60",
            "nv set system telemetry platform-stats class cpu state enabled",
            "nv set system telemetry platform-stats class cpu sample-interval 60",
            "nv set system telemetry platform-stats class disk state enabled",
            "nv set system telemetry platform-stats class disk sample-interval 60",
            "nv set system telemetry platform-stats class environment-sensor state enabled",
            "nv set system telemetry platform-stats class environment-sensor sample-interval 60",
            "nv set system telemetry platform-stats class file-system state enabled",
            "nv set system telemetry platform-stats class file-system sample-interval 60",
            "nv set system telemetry platform-stats class memory state enabled",
            "nv set system telemetry platform-stats class memory sample-interval 60",
            "nv set system telemetry interface-stats class phy state enabled",
            "nv set system telemetry interface-stats class phy sample-interval 2",
            "nv set system telemetry software-stats systemd export state enabled",
            "nv set system telemetry software-stats systemd sample-interval 60",
            "nv set system telemetry router export state enabled",
            "nv set system telemetry router bgp export state enabled",
            "nv set system telemetry router rib export state enabled",
        ]
        for pg in range(8):
            cmds.append(
                "nv set system telemetry interface-stats ingress-buffer priority-group %d" % pg
            )
        for tc in range(8):
            cmds.append(
                "nv set system telemetry interface-stats egress-buffer traffic-class %d" % tc
            )
        for sp in ("0", "1", "2", "3", "7"):
            cmds.append("nv set system telemetry interface-stats switch-priority %s" % sp)
        if _nvue_version_gte(dut, "1.13.0"):
            cmds.append("nv set system telemetry interface-stats class debounce state enabled")
        for cmd in cmds:
            dut.run_cmd(cmd, validate=False)
        dut.run_cmd("nv config apply -y", validate=True)


def _prepare_coexistence_gnmi_dialout(dut) -> None:
    """Release stale dial-out security before a fresh gNMI coexistence session."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.gnmi_dialout_mtls import (
        _release_stale_gnmi_dialout_security,
    )

    with allure.step('Prepare gNMI dial-out security (coexistence)'):
        _release_stale_gnmi_dialout_security(dut)
        settle_dut_ssh_after_module_transition(dut, settle_sec=3)


def _ensure_insecure_otlp_session_after_config(
    dut,
    collector: OtelCollector,
    *,
    vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT,
    telemetry_unit: Optional[str] = None,
    is_nvos: bool = False,
) -> None:
    """Re-bind insecure OTLP after secured smoke or ``clear_config``."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.telemetry_health import (
        assert_otlp_session_established,
        verify_otlp_client_active,
    )
    from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
        restart_asic_monitor,
        restart_nvtelemetry,
    )

    with allure.step("Ensure insecure OTLP session (post-NVUE apply)"):
        truncate_collector_export(collector)
        collector.ensure_running(install_if_missing=False)
        restart_nvtelemetry(dut, vrf, unit=telemetry_unit)
        if not is_nvos:
            restart_asic_monitor(dut, vrf)
        verify_otlp_client_active(
            dut, vrf, is_nvos=is_nvos, telemetry_unit=telemetry_unit
        )
        assert_otlp_session_established(collector)


def _enable_interface_qos_coexistence_exports(dut) -> None:
    """NVUE exports enabled in SSIM ``build_interface_telemetry_cache`` (no BGP peers on mlx)."""
    cmds = [
        "nv set system telemetry router export state enabled",
        "nv set system telemetry acl-stats export state enabled",
        "nv set system telemetry acl-stats class acl-set state enabled",
        "nv set system telemetry lldp export state enabled",
        "nv set system telemetry ai-ethernet-stats export state enabled",
        "nv set system telemetry interface-stats class phy state enabled",
        "nv set system telemetry software-stats systemd export state enabled",
        "nv set system telemetry software-stats systemd process-level enabled",
        "nv set system telemetry router bgp export state enabled",
        "nv set system telemetry router rib export state enabled",
        "nv set system telemetry buffer-stats export state enabled",
        "nv set system telemetry platform-stats export state enabled",
    ]
    with allure.step("Enable coexistence telemetry exports (Interface/QoS)"):
        for cmd in cmds:
            dut.run_cmd(cmd, validate=False)
        dut.run_cmd("nv config apply -y", validate=True)


def _prepare_coexistence_insecure_otel(
    dut,
    collector: OtelCollector,
    collector_ips: Tuple[str, ...],
    *,
    is_nvos: bool,
    export_vrf: str,
    telemetry_unit: Optional[str] = None,
) -> None:
    """Shared Cumulus/NVOS insecure coexistence prep (same step order; platform bodies differ)."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import (
        _restore_otlp_mgmt_insecure_destinations,
    )

    with allure.step("Prepare coexistence insecure OTEL (shared Cumulus/NVOS steps)"):
        _restore_otlp_mgmt_insecure_destinations(
            dut, collector_ips, export_vrf=export_vrf, is_nvos=is_nvos
        )
        if is_nvos:
            # NVOS has no histogram subtree, so there is no insecure pre-run to stage.
            logger.info("NVOS: skipping Cumulus histogram / split-pipeline insecure pre-run")
        else:
            prepare_split_pipeline_insecure_pre_run(dut)
        _apply_coexistence_root_telemetry_config(
            dut, collector_ips, is_nvos=is_nvos, export_vrf=export_vrf
        )
        if is_nvos:
            # lldp / acl-stats / router / buffer-stats / software-stats are Cumulus-only subtrees.
            logger.info("NVOS: skipping Cumulus-only coexistence telemetry exports")
        else:
            _enable_interface_qos_coexistence_exports(dut)
        _ensure_insecure_otlp_session_after_config(
            dut,
            collector,
            vrf=export_vrf,
            telemetry_unit=telemetry_unit,
            is_nvos=is_nvos,
        )


def collect_coexistence_smoke_cache(
    dut,
    collector: OtelCollector,
    cur_dir: str,
    *,
    sonic_mgmt=None,
    vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT,
    wait_sec: int = COEXISTENCE_SMOKE_COLLECT_WAIT_SEC,
    cleanup_session: bool = True,
    defer_gnmi_teardown: bool = False,
    is_nvos: bool = False,
    telemetry_unit: Optional[str] = None,
) -> CoexistenceCollectResult:
    """``Test_Telemetry_Coexistence_Smoke`` OTEL (+ gNMI on Cumulus only).

    Cumulus: BGP/LLDP gNMI jobs + secured catalog families.
    NVOS: OTEL only (smoke_01/03); BGP/LLDP gNMI tests are cumulus_only.
    """
    telemetryCache.clear_data()
    if is_nvos:
        gnmi_jobs: List[Any] = []
    else:
        gnmi_jobs = build_smoke_gnmi_jobs(dut, duration_sec=wait_sec)
    if gnmi_jobs and sonic_mgmt is not None:
        _prepare_coexistence_gnmi_dialout(dut)

    def _collect_otlp() -> Dict[str, Any]:
        return collect_and_cache_secured_otel_session(
            dut,
            collector,
            cur_dir,
            vrf=vrf,
            wait_sec=wait_sec,
            cleanup_session=cleanup_session,
            telemetry_unit=telemetry_unit,
            is_nvos=is_nvos,
        )

    gnmi_session = None
    if gnmi_jobs and sonic_mgmt is not None:
        otel_holder: Dict[str, Any] = {}

        def _otlp_and_store():
            otel_holder['payload'] = _collect_otlp()

        gnmi_session = run_gnmi_with_parallel_work(
            dut,
            sonic_mgmt,
            gnmi_jobs,
            _otlp_and_store,
            defer_teardown=defer_gnmi_teardown,
            use_mtls=True,
        )
        otel_payload = otel_holder['payload']
    else:
        if gnmi_jobs and sonic_mgmt is None:
            logger.warning('sonic_mgmt not provided; skipping smoke gNMI collection')
        otel_payload = _collect_otlp()

    artifact_path = cumulus_otel_artifact_path(cur_dir)
    hostname = get_dut_hostname(dut)
    family_map = list(CumulusOtelConst.NVOS_SPLIT_FAMILY_MAP) if is_nvos else None
    otel_payload["time_gaps"] = timegap_analysis_v2(
        artifact_path,
        hostname,
        os_info=get_dut_os_info(dut),
        family_map=family_map,
    )
    if is_nvos:
        otel_payload["coexistence_expected_gaps"] = dict(COEXISTENCE_NVOS_SMOKE_TIME_GAPS)
    telemetryCache.add_data("otel", otel_payload)
    return CoexistenceCollectResult(otel_payload=otel_payload, gnmi_session=gnmi_session)


def collect_coexistence_interface_qos_cache(
    dut,
    collector: OtelCollector,
    cur_dir: str,
    *,
    collector_ips: Tuple[str, ...],
    sonic_mgmt=None,
    wait_sec: int = COEXISTENCE_INTERFACE_QOS_COLLECT_WAIT_SEC,
    defer_gnmi_teardown: bool = False,
    is_nvos: bool = False,
    export_vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT,
    telemetry_unit: Optional[str] = None,
) -> CoexistenceCollectResult:
    """``Test_Telemetry_Interface_QoS_Validation_Coexistence`` OTEL + gNMI cache."""
    telemetryCache.clear_data()
    vrf = export_vrf
    _prepare_coexistence_insecure_otel(
        dut,
        collector,
        collector_ips,
        is_nvos=is_nvos,
        export_vrf=vrf,
        telemetry_unit=telemetry_unit,
    )

    cp_stats_pre = get_cp_stat_sx_api_cli(dut)
    gnmi_jobs = build_interface_qos_gnmi_jobs(dut, duration_sec=wait_sec, is_nvos=is_nvos)
    # NVOS: plain dial-out (addr+port). Cumulus: SSIM mTLS dial-out.
    use_mtls = not is_nvos
    if gnmi_jobs and sonic_mgmt is not None:
        _prepare_coexistence_gnmi_dialout(dut)
    otel_holder: Dict[str, Any] = {}
    family_map = list(CumulusOtelConst.NVOS_SPLIT_FAMILY_MAP) if is_nvos else None

    def _collect_otlp_and_cli():
        with allure.step('Collect OTLP export and CLI baseline (parallel with gNMI)'):
            truncate_collector_export(collector)
            collect_telemetry_data(
                dut, collector, vrf, wait_sec, None, cur_dir, telemetry_unit=telemetry_unit
            )
            artifact_path = cumulus_otel_artifact_path(cur_dir)
            hostname = get_dut_hostname(dut)
            otel_holder['payload'] = parse_telemetry_and_cache(
                OtelDataValidations(),
                artifact_path=artifact_path,
                hostname=hostname,
                include_time_gaps=True,
                dut=dut,
                family_map=family_map,
            )
            if is_nvos:
                otel_holder['payload']['coexistence_expected_gaps'] = dict(
                    CumulusOtelConst.NVOS_SPLIT_EXPECTED_GAPS
                )
                telemetryCache.add_data('otel', otel_holder['payload'])
            otel_holder['cli'] = collect_cli_mgmt_vrf_session_data(dut, cp_stats_pre=cp_stats_pre)

    gnmi_session = None
    collect_result = None
    try:
        if gnmi_jobs and sonic_mgmt is not None:
            with allure.step(
                'Run gNMI dial-out + OTLP in parallel (wait_sec=%d, jobs=%d, use_mtls=%s)'
                % (wait_sec, len(gnmi_jobs), use_mtls)
            ):
                gnmi_session = run_gnmi_with_parallel_work(
                    dut,
                    sonic_mgmt,
                    gnmi_jobs,
                    _collect_otlp_and_cli,
                    defer_teardown=defer_gnmi_teardown,
                    use_mtls=use_mtls,
                    is_nvos=is_nvos,
                )
        else:
            _collect_otlp_and_cli()

        with allure.step('Assert telemetry cache has OTEL + gNMI interface series'):
            telemetryCache.add_data('cli', otel_holder['cli'])
            assert_telemetry_cache_has_data(
                [
                    ('OTEL', 'otel'),
                    ('GNMI interface series', 'gnmi-series-interface'),
                ],
                context='Interface/QoS coexistence',
            )
        collect_result = CoexistenceCollectResult(
            otel_payload=otel_holder['payload'],
            gnmi_session=gnmi_session,
        )
        return collect_result
    finally:
        if collect_result is None and gnmi_session is not None and defer_gnmi_teardown:
            teardown_gnmi_coexistence_session(dut, gnmi_session)


def collect_coexistence_component_system_cache(
    dut,
    collector: OtelCollector,
    cur_dir: str,
    *,
    collector_ips: Tuple[str, ...],
    sonic_mgmt=None,
    wait_sec: int = COEXISTENCE_INTERFACE_QOS_COLLECT_WAIT_SEC,
    defer_gnmi_teardown: bool = False,
    is_nvos: bool = False,
    export_vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT,
    telemetry_unit: Optional[str] = None,
) -> CoexistenceCollectResult:
    """``Test_Telemetry_Component_System_Validation_Coexistence`` OTEL + gNMI cache."""
    telemetryCache.clear_data()
    vrf = export_vrf
    settle_dut_ssh_after_module_transition(dut)
    _prepare_coexistence_insecure_otel(
        dut,
        collector,
        collector_ips,
        is_nvos=is_nvos,
        export_vrf=vrf,
        telemetry_unit=telemetry_unit,
    )

    cp_stats_pre = get_cp_stat_sx_api_cli(dut)
    if is_nvos:
        gnmi_jobs: List[Any] = []
    else:
        gnmi_jobs = build_component_system_gnmi_jobs(dut, duration_sec=wait_sec)
    if gnmi_jobs and sonic_mgmt is not None:
        _prepare_coexistence_gnmi_dialout(dut)
    otel_holder: Dict[str, Any] = {}
    family_map = list(CumulusOtelConst.NVOS_SPLIT_FAMILY_MAP) if is_nvos else None

    def _collect_otlp_and_cli():
        truncate_collector_export(collector)
        collect_telemetry_data(
            dut, collector, vrf, wait_sec, None, cur_dir, telemetry_unit=telemetry_unit
        )
        artifact_path = cumulus_otel_artifact_path(cur_dir)
        hostname = get_dut_hostname(dut)
        otel_holder['payload'] = parse_telemetry_and_cache(
            OtelDataValidations(),
            artifact_path=artifact_path,
            hostname=hostname,
            include_time_gaps=True,
            dut=dut,
            family_map=family_map,
        )
        if is_nvos:
            otel_holder['payload']['coexistence_expected_gaps'] = dict(
                CumulusOtelConst.NVOS_SPLIT_EXPECTED_GAPS
            )
            telemetryCache.add_data('otel', otel_holder['payload'])
        otel_holder['cli'] = collect_cli_mgmt_vrf_session_data(dut, cp_stats_pre=cp_stats_pre)
        # SSIM test_09 CLI component job (component_type='CPU'), collected in the same window.
        otel_holder['cpu_cli'] = collect_cpu_cli_component_data(dut)
        # SSIM test_10 CLI component job (component_type='PSU'), same chosen PSU as gNMI job.
        otel_holder['psu_cli'] = collect_psu_cli_component_data(dut)

    gnmi_session = None
    if gnmi_jobs and sonic_mgmt is not None:
        gnmi_session = run_gnmi_with_parallel_work(
            dut,
            sonic_mgmt,
            gnmi_jobs,
            _collect_otlp_and_cli,
            defer_teardown=defer_gnmi_teardown,
            use_mtls=True,
        )
    else:
        _collect_otlp_and_cli()

    telemetryCache.add_data('cli', otel_holder['cli'])
    telemetryCache.add_data(CPU_CLI_CACHE_KEY, otel_holder['cpu_cli'])
    telemetryCache.add_data(PSU_CLI_CACHE_KEY, otel_holder['psu_cli'])
    return CoexistenceCollectResult(
        otel_payload=otel_holder['payload'],
        gnmi_session=gnmi_session,
    )


def _unset_interface_stats_attribute_filters(dut) -> None:
    """SSIM ``test_06`` pg/tc/sp unsets on the interface-stats baseline."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import (
        apply_unset_changes,
        unset_system_telemetry_interface_stats_egress_tc,
        unset_system_telemetry_interface_stats_ingress_pg,
        unset_system_telemetry_interface_stats_switch_priority,
    )

    with allure.step("Unset interface-stats pg/tc/sp attribute filters (SSIM test_06)"):
        for pg in COEXISTENCE_ATTR_FILTER_UNSET_PGS:
            unset_system_telemetry_interface_stats_ingress_pg(dut, pg)
        for tc in COEXISTENCE_ATTR_FILTER_UNSET_TCS:
            unset_system_telemetry_interface_stats_egress_tc(dut, tc)
        for sp in COEXISTENCE_ATTR_FILTER_UNSET_SPS:
            unset_system_telemetry_interface_stats_switch_priority(dut, sp)
        apply_unset_changes(dut)


def collect_coexistence_attribute_filtering_cache(
    dut,
    collector: OtelCollector,
    cur_dir: str,
    *,
    collector_ips: Tuple[str, ...],
    wait_sec: int = COEXISTENCE_INTERFACE_QOS_COLLECT_WAIT_SEC,
    settle_sec: int = COEXISTENCE_ATTR_FILTER_SETTLE_SEC,
) -> Dict[str, Any]:
    """SSIM ``test_06_validate_otel_metric_attribute_filtering`` OTEL cache.

    Rebuilds the Interface/QoS interface-stats baseline (pg/tc 0-7, sp 0,1,2,3,7), unsets the
    pg/tc/sp values under test, re-collects the OTLP export and caches ``metrics_timestamps``
    under the dedicated ``COEXISTENCE_ATTR_FILTER_CACHE_KEY`` for
    :func:`validate_otel_metric_attribute_filtering`. Deliberately does NOT clear the shared
    telemetry cache: this fixture is module-scoped and a global clear would wipe other suites'
    cached payloads, coupling otherwise-independent tests.
    """
    from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import (
        _restore_otlp_mgmt_insecure_destinations,
    )

    vrf = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT
    settle_dut_ssh_after_module_transition(dut)
    with allure.step('Prepare interface-stats baseline and unset pg/tc/sp (SSIM test_06)'):
        _restore_otlp_mgmt_insecure_destinations(dut, collector_ips, export_vrf=vrf)
        prepare_split_pipeline_insecure_pre_run(dut)
        _apply_coexistence_split_pipeline_root_config(dut)
        _enable_interface_qos_coexistence_exports(dut)
        _unset_interface_stats_attribute_filters(dut)
        _ensure_insecure_otlp_session_after_config(dut, collector, vrf=vrf)

    with allure.step('Settle before OTLP re-collection (%ds)' % settle_sec):
        time.sleep(settle_sec)

    with allure.step('Collect OTLP export after attribute-filter unset (wait_sec=%d)' % wait_sec):
        truncate_collector_export(collector)
        collect_telemetry_data(dut, collector, vrf, wait_sec, None, cur_dir)
        artifact_path = cumulus_otel_artifact_path(cur_dir)
        hostname = get_dut_hostname(dut)
        otel_payload = parse_telemetry_and_cache(
            OtelDataValidations(),
            artifact_path=artifact_path,
            hostname=hostname,
            cache_key=COEXISTENCE_ATTR_FILTER_CACHE_KEY,
            include_time_gaps=True,
            dut=dut,
        )
    return otel_payload
