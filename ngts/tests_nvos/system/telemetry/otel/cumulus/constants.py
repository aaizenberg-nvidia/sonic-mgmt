"""Constants for Cumulus OTLP tests (stats-group, catalog buckets, collection window)."""

from typing import Dict, Optional, Tuple

from ngts.nvos_constants.constants_nvos import TelemetryConsts
from ngts.tests_nvos.system.telemetry.otel.constants import OtelCollectorConst


class CumulusOtelConst:
    """Cumulus lab mgmt VRF insecure OTLP (test01) configuration."""

    TELEMETRY_EXPORT_VRF_MGMT = "mgmt"
    # Stats-group name for mgmt VRF insecure topology.
    TEST01_STATS_GROUP_ID = "sg_01"

    # CLI control-plane trap/group counter name -> exported OTEL metric name (test04).
    TRAPS_CONVERSION_CLI_TO_OTEL: Dict[str, str] = {
        "ToCpuPacket": "nvswitch_control_plane_trap_rx_packets",
        "ToCpuByte": "nvswitch_control_plane_trap_rx_bytes",
        "Events": "nvswitch_control_plane_trap_rx_event_count",
        "Drops": "nvswitch_control_plane_trap_rx_drop",
    }

    STATS_GROUP_SUPPORTED_FAMILIES = (
        TelemetryConsts.INTERFACE_STATS,
        TelemetryConsts.PLATFORM_STATS,
    )
    ROOT_STATS_FAMILIES = (
        TelemetryConsts.INTERFACE_STATS,
        TelemetryConsts.PLATFORM_STATS,
    )
    CUMULUS_ROOT_STATS_FAMILIES = ROOT_STATS_FAMILIES
    # ``stats-group sg_01`` simple-export subtrees (``mgmt VRF insecure topology``).
    # ``adaptive-routing-stats`` is root-only on many Cumulus NVUE builds (not under stats-group).
    TEST01_STATS_GROUP_SIMPLE_EXPORTS: Tuple[str, ...] = (
        "ai-ethernet-stats",
        "lldp",
        "control-plane-stats",
        "buffer-stats",
    )

    TEST01_ROOT_SIMPLE_EXPORTS: Tuple[str, ...] = (
        "lldp",
        "buffer-stats",
        "control-plane-stats",
    )
    # Root adaptive-routing; absent on some lab NVUE builds.
    TEST01_ROOT_OPTIONAL_SIMPLE_EXPORTS: Tuple[str, ...] = ("adaptive-routing-stats",)
    TEST01_ROOT_STATS_FAMILIES: Tuple[str, ...] = (
        TelemetryConsts.INTERFACE_STATS,
        TelemetryConsts.PLATFORM_STATS,
    )

    # Interval-map key for the software-stats systemd sample-interval (not an NVUE
    # node name; the ``software-stats`` sample-interval lives under ``systemd``).
    SOFTWARE_STATS_SYSTEMD_INTERVAL_KEY = "software-stats-systemd"

    TEST01_ROOT_SAMPLE_INTERVALS: Dict[str, int] = {
        TelemetryConsts.INTERFACE_STATS: 3,
        TelemetryConsts.PLATFORM_STATS: 61,
        "lldp": 20,
        "buffer-stats": 60,
        "control-plane-stats": 2,
        "adaptive-routing-stats": 70,
        SOFTWARE_STATS_SYSTEMD_INTERVAL_KEY: 60,
    }

    # Sample intervals under ``stats-group sg_01`` (``mgmt VRF insecure topology``).
    TEST01_STATS_GROUP_SAMPLE_INTERVALS: Dict[str, int] = {
        "ai-ethernet-stats": 70,
        "lldp": 60,
        "control-plane-stats": 60,
        "buffer-stats": 60,
        "adaptive-routing-stats": 60,
        TelemetryConsts.INTERFACE_STATS: 3,
        TelemetryConsts.PLATFORM_STATS: 61,
        SOFTWARE_STATS_SYSTEMD_INTERVAL_KEY: 60,
    }

    TEST01_HISTOGRAM_BUFFER_SIZES: Dict[str, int] = {
        "egress-buffer": 9_830_400,
        "ingress-buffer": 2_457_600,
    }

    TEST01_SYSTEMD_UNIT_NAMES: Tuple[str, ...] = (
        "nv-telemetry@default",
        "asic-monitor@default",
        "prometheus-sdk-stats",
        "prometheus-node-exporter",
        "nginx",
        "switchd",
        "frr",
        "nvued",
    )

    # When ``export vrf`` is mgmt, histogram uses ``asic-monitor@mgmt`` (not @default).
    TEST01_SYSTEMD_UNIT_NAMES_MGMT_VRF: Tuple[str, ...] = (
        "nv-telemetry@mgmt",
        "asic-monitor@mgmt",
        "prometheus-sdk-stats",
        "prometheus-node-exporter",
        "nginx",
        "switchd",
        "frr",
        "nvued",
    )

    # Built-in ``default`` systemd unit-profile services validated by SSIM
    # ``test_nv_show_system_telemetry_software_stats_validation`` (Cumulus-only; the
    # list is image/platform specific, so it is presence-checked, not set-compared).
    TEST01_SYSTEMD_DEFAULT_UNIT_NAMES: Tuple[str, ...] = (
        "asic-monitor.service",
        "frr.service",
        "hostapd.service",
        "netq-agent.service",
        "netqd.service",
        "nginx.service",
        "ntpsec.service",
        "nv-telemetry.service",
        "nvued.service",
        "prometheus-node-exporter.service",
        "prometheus-sdk-stats.service",
        "ptp4l.service",
        "snmpd.service",
        "switchd.service",
        "sx_sdk.service",
        "wd_keepalive.service",
    )

    # software-stats subtree node / field names (NVUE command words and JSON keys).
    SOFTWARE_STATS = "software-stats"
    SYSTEMD = "systemd"
    UNIT_PROFILE = "unit-profile"
    UNIT = "unit"
    PROCESS_LEVEL = "process-level"
    ACTIVE_PROFILE = "active-profile"
    # Built-in software-stats systemd unit-profile ids.
    UNIT_PROFILE_CUSTOM = "custom"
    UNIT_PROFILE_DEFAULT = "default"

    # Sorted keys expected under ``software-stats systemd`` in the ``--applied`` show.
    SOFTWARE_STATS_SYSTEMD_KEYS: Tuple[str, ...] = (
        "active-profile",
        "export",
        "process-level",
        "sample-interval",
        "unit-profile",
    )

    TEST01_METRICS_GROUP_PREFIXES: Tuple[str, ...] = (
        TelemetryConsts.INTERFACE_STATS,
        TelemetryConsts.PLATFORM_STATS,
        "lldp",
        "buffer-stats",
        "control-plane-stats",
        "adaptive-routing-stats",
        "ai-ethernet-stats",
        "software-stats",
        "histogram",
    )

    TEST01_COLLECTION_WAIT_SEC = 177

    # test01 — not applicable on
    # this topology or absent features on the lab DUT.
    TEST01_EXCLUDE_METRICS: Tuple[str, ...] = (
        "nvswitch_srv6_in_pkts",
        "nvswitch_srv6_no_sid_drops",
        "nvswitch_ar_congestion_changes",
        "nvswitch_qos_trimmed_unicast_pkts",
        "nvswitch_interface_trimmed_unicast_pkts",
        "nvswitch_interface_trimmed_tx_unicast_pkts",
        "nvswitch_interface_tc_trimmed_unicast_pkts",
        "nvswitch_acl_interface_matched_pkts",
        "nvswitch_acl_interface_matched_bytes",
        "nvswitch_acl_set_ipv4_info",
        "nvswitch_acl_set_ipv6_info",
        "nvswitch_acl_set_l2_info",
        "nvswitch_acl_set_l4_info",
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
        "scrape_duration_seconds",
        "scrape_samples_post_metric_relabeling",
        "scrape_samples_scraped",
        "scrape_series_added",
        "up",
    )

    # When root is on NVMe, NVUE/OTLP does not expose ATA-only disk metrics.
    TEST01_EXCLUDE_METRICS_NVME: Tuple[str, ...] = (
        "node_disk_ata_rotation_rate_rpm",
        "node_disk_ata_write_cache",
        "node_disk_ata_write_cache_enabled",
        "node_cpu_core_throttles_total",
        "node_cpu_package_throttles_total",
    )

    TEST01_VALIDATION_BUCKETS: Tuple[str, ...] = (
        "PH1_INT_STAT",
        "PH1_HIST",
        "PH1_DOT3_STAT",
        "PH1_ADD_STAT",
        "PH2_HIST",
        "PH2_ADD_INT_STAT",
        "PH2_INT_CAR_CHG",
        "PH2_INT_DISC_STATS",
        "PH2_INT_ETHER_STATS",
        "PH2_PKT_DIST_STATS",
        "PH2_PLAT_ENV",
        "PH2_NODE_CPU",
        "PH2_NODE_DISK",
        "PH2_NODE_FILE",
        "PH2_NODE_MEM",
        "PH2_SW_PRIO_STATS",
        "PH2_CP_STATS",
        "ALLOWED_EXTRAS",
        "PH3_BUFF_STATS",
        "PH3_PHY_STATS",
        "PH3_EXTRAS",
        "PH4_EXTRAS",
        "PH4_TRANSCEIVER_INFO_ALL",
        "PH4_SOFTWARE_STATS",
        "PH4_LLDP_STATS",
        "PH5_ADDITIONS",
        "CL_514_AI_ETHERNET_STATS",
        "PH6_ADDITIONAL_NODE_STATS",
        "CL_515_ADDITIONAL_INT_STATS",
        "CL_515_AI_ETHERNET_STATS",
        "CL_516_QOS_BUFFER_METRICS",
        "PH6_DOT1X_STATS",
        "CL_517_ADDITIONAL_PLATFORM_STATS",
        "CL_517_CP_NETSTAT_METRICS",
        "CL_517_ADDITIONAL",
        "CL_517_ADDITIONAL_PHY",
        "CL_517_LINK_DEBOUNCE_STATS",
        "CL_518_PLATFORM_ASIC_RESOURCE_STATS",
        "CL_518_PLATFORM_PARTITION_STATS",
        "CL_518_PLATFORM_POWER_SUPPLY_STATS",
    )
    TEST01_VALIDATION_SKIP_BUCKETS: Tuple[str, ...] = (
        "PH3_ROUTING_STATS",
        "PH5_ROUTING_STATS",
        "CL_515_ROUTING_STATS",
        "PH6_ACL_STATS",
    )
    # ``OtelMgmtVrfWithTLSConfig`` does not enable dot1x-stats or link-debounce telemetry.
    SECURED_MGMT_VALIDATION_SKIP_BUCKETS: Tuple[str, ...] = (
        *TEST01_VALIDATION_SKIP_BUCKETS,
        "PH6_DOT1X_STATS",
        "CL_517_LINK_DEBOUNCE_STATS",
    )
    # Metrics that need SSIM spine↔leaf fabric (LLDP neighbors, optics, RS-FEC).
    SECURED_MGMT_LAB_TOPOLOGY_EXCLUDE_METRICS: Tuple[str, ...] = (
        "nvswitch_interface_phy_rs_fec_histogram",
        "nvswitch_lldp_neighbor_management_address_info",
        "nvswitch_platform_transceiver_ethernet_pmd",
    )
    # Control-plane trap-group counters need trap traffic (SSIM spine has BGP fabric).
    SECURED_MGMT_CP_TRAP_GROUP_EXCLUDE_METRICS: Tuple[str, ...] = (
        "nvswitch_control_plane_trap_group_pkt_violations",
        "nvswitch_control_plane_trap_group_rx_bytes",
        "nvswitch_control_plane_trap_group_rx_packets",
    )

    STATS_GROUP_PLATFORM_CLASS_FILE_SYSTEM = "file-system"
    PLATFORM_CLASS_ASIC_RESOURCE = "asic-resource"
    STATS_GROUP_PLATFORM_CLASS_ASIC_RESOURCE_SAMPLE_INTERVAL = 10
    STATS_GROUP_PLATFORM_CLASSES = (
        TelemetryConsts.PLATFORM_CLASS_CPU,
        TelemetryConsts.PLATFORM_CLASS_DISK,
        TelemetryConsts.PLATFORM_CLASS_MEMORY,
        STATS_GROUP_PLATFORM_CLASS_FILE_SYSTEM,
        TelemetryConsts.PLATFORM_CLASS_PLATFORM_INFO,
        TelemetryConsts.PLATFORM_CLASS_ENVIRONMENT_SENSOR,
        TelemetryConsts.PLATFORM_CLASS_TRANSCEIVER_INFO,
    )
    # test15b (CL 5.12–5.13): ``platform-info`` is not restored after class unset.
    TEST15B_STATS_GROUP_PLATFORM_CLASSES: Tuple[str, ...] = (
        TelemetryConsts.PLATFORM_CLASS_CPU,
        TelemetryConsts.PLATFORM_CLASS_DISK,
        TelemetryConsts.PLATFORM_CLASS_ENVIRONMENT_SENSOR,
        STATS_GROUP_PLATFORM_CLASS_FILE_SYSTEM,
        TelemetryConsts.PLATFORM_CLASS_MEMORY,
        TelemetryConsts.PLATFORM_CLASS_TRANSCEIVER_INFO,
    )
    # ``sg_01`` platform classes in ``mgmt VRF insecure topology`` (not disk/memory/file-system).
    TEST01_STATS_GROUP_PLATFORM_CLASSES: Tuple[str, ...] = (
        TelemetryConsts.PLATFORM_CLASS_CPU,
        TelemetryConsts.PLATFORM_CLASS_ENVIRONMENT_SENSOR,
        TelemetryConsts.PLATFORM_CLASS_TRANSCEIVER_INFO,
        TelemetryConsts.PLATFORM_CLASS_PLATFORM_INFO,
    )
    TEST01_STATS_GROUP_PLATFORM_CLASS_INTERVALS: Dict[str, int] = {
        TelemetryConsts.PLATFORM_CLASS_CPU: 69,
        TelemetryConsts.PLATFORM_CLASS_ENVIRONMENT_SENSOR: 64,
        TelemetryConsts.PLATFORM_CLASS_TRANSCEIVER_INFO: 62,
        TelemetryConsts.PLATFORM_CLASS_PLATFORM_INFO: 63,
    }
    # Root platform-stats class intervals (``OtelMgmtVrfNoTLSConfig`` / SSIM test01).
    TEST01_ROOT_PLATFORM_CLASS_INTERVALS: Dict[str, int] = {
        TelemetryConsts.PLATFORM_CLASS_CPU: 69,
        TelemetryConsts.PLATFORM_CLASS_ENVIRONMENT_SENSOR: 64,
        TelemetryConsts.PLATFORM_CLASS_TRANSCEIVER_INFO: 62,
        TelemetryConsts.PLATFORM_CLASS_PLATFORM_INFO: 63,
    }
    ROOT_PLATFORM_CLASSES = STATS_GROUP_PLATFORM_CLASSES
    CUMULUS_ROOT_PLATFORM_CLASSES = ROOT_PLATFORM_CLASSES

    # Reuse parent collector timeouts for fetch after stop/copy.
    ARTIFACT_TIMEOUT_SEC = OtelCollectorConst.ARTIFACT_TIMEOUT_SEC

    # OTEL attribute label pairs on mgmt VRF insecure stats-group export (test02–03).
    # Telemetry health services (``nv show system telemetry health``); subset checked per build.
    OTEL_HEALTH_SERVICES: Tuple[str, ...] = (
        "nv-telemetry-service",
        "histogram-export-service",
        "platform-stats-service",
        # "routing-telemetry-service",
        "control-plane-stats-service",
        "software-systemd-stats-service",
        "ai-ethernet-stats-service",
        "lldp-stats-service",
        "interface-stats-service",
        "buffer-stats-service",
        "sdk-stats-service",
    )

    # ``OtelMgmtVrfNoTLSConfig`` health (SSIM OTEL514) without router telemetry (disabled on mlx lab).
    OTEL_HEALTH_SERVICES_MGMT_INSECURE: Tuple[str, ...] = (
        "nv-telemetry-service",
        "histogram-export-service",
        "platform-stats-service",
        "control-plane-stats-service",
        "software-systemd-stats-service",
        "ai-ethernet-stats-service",
        "lldp-stats-service",
        "interface-stats-service",
        "buffer-stats-service",
    )

    OTEL_HEALTH_POST_CLEANUP_SETTLE_SEC = 30
    # Brief pause after coexistence module teardown before secured SCP / gNMI setup.
    COEXISTENCE_MODULE_TRANSITION_SETTLE_SEC = 5

    OTEL_SYSTEMD_STOP_UNITS: Tuple[Tuple[str, Optional[str]], ...] = (
        ("prometheus-sdk-stats", None),
        ("prometheus-node-exporter.service", "platform-stats-service"),
        ("asic-monitor@mgmt.service", "histogram-export-service"),
        ("nv-telemetry@mgmt.service", "nv-telemetry-service"),
    )
    OTEL_SYSTEMD_START_UNITS: Tuple[str, ...] = (
        "prometheus-sdk-stats",
        "prometheus-node-exporter.service",
        "asic-monitor@mgmt.service",
        "nv-telemetry@mgmt.service",
    )
    OTEL_SERVICES_RESTART_SETTLE_SEC = 20

    TELEMETRY_EXPORT_VRF_DEFAULT = "default"

    # TLS / secured OTLP (OtelDefaultVrfWithTLSConfig / OtelMgmtVrfWithTLSConfig).
    OTEL_TLS_CA_NAME = "ca"
    OTEL_TLS_CERT_DIR = "/tmp/otel-tls"
    OTEL_TLS_CA_CRT = f"{OTEL_TLS_CERT_DIR}/ca.crt"
    OTEL_TLS_SERVER_CRT = f"{OTEL_TLS_CERT_DIR}/otelc.crt"
    OTEL_TLS_SERVER_KEY = f"{OTEL_TLS_CERT_DIR}/otelc.key"
    OTEL_TLS_DUT_CA_STAGING = "/tmp/ca.crt"
    OTEL_TLS_DUT_CA_KEY_STAGING = "/tmp/ca.key"

    SECURED_COLLECTION_WAIT_DEFAULT_VRF_SEC = 177
    SECURED_COLLECTION_WAIT_MGMT_VRF_SEC = 175
    SECURED_NVTELEMETRY_RESTART_SETTLE_SEC = 30

    SECURED_ROOT_SAMPLE_INTERVALS: Dict[str, int] = {
        TelemetryConsts.INTERFACE_STATS: 2,
        TelemetryConsts.PLATFORM_STATS: 60,
        "lldp": 20,
        "buffer-stats": 60,
        "control-plane-stats": 2,
        "adaptive-routing-stats": 70,
        SOFTWARE_STATS_SYSTEMD_INTERVAL_KEY: 60,
    }

    # Rib counters may still appear briefly when router export is disabled on lab DUTs.
    SECURED_NO_ROUTING_EXCLUDE_METRICS: Tuple[str, ...] = (
        "nvrouting_rib_count",
        "nvrouting_rib_nhg_count",
        "nvrouting_rib_total_count",
    )

    SECURED_EXCLUDE_METRICS: Tuple[str, ...] = (
        "nvswitch_srv6_in_pkts",
        "nvswitch_srv6_no_sid_drops",
        "nvswitch_ar_congestion_changes",
        "nvswitch_qos_trimmed_unicast_pkts",
        "nvswitch_interface_trimmed_unicast_pkts",
        "nvswitch_interface_trimmed_tx_unicast_pkts",
        "nvswitch_interface_tc_trimmed_unicast_pkts",
        "nvswitch_acl_interface_matched_pkts",
        "nvswitch_acl_interface_matched_bytes",
        "nvswitch_acl_set_ipv4_info",
        "nvswitch_acl_set_ipv6_info",
        "nvswitch_acl_set_l2_info",
        "nvswitch_acl_set_l4_info",
    )

    OTEL_SYSTEMD_STOP_UNITS_DEFAULT: Tuple[Tuple[str, Optional[str]], ...] = (
        ("prometheus-sdk-stats", None),
        ("prometheus-node-exporter.service", "platform-stats-service"),
        ("asic-monitor@default.service", "histogram-export-service"),
        ("nv-telemetry@default.service", "nv-telemetry-service"),
    )
    OTEL_SYSTEMD_START_UNITS_DEFAULT: Tuple[str, ...] = (
        "prometheus-sdk-stats",
        "prometheus-node-exporter.service",
        "asic-monitor@default.service",
        "nv-telemetry@default.service",
    )

    # Split-pipeline stats-group (``OtelSplitPipelineMgmtVrf*`` SSIM test01).
    SPLIT_PIPELINE_STATS_GROUP_ID = "test_01"
    SPLIT_PIPELINE_COLLECT_WAIT_SEC = 180
    SPLIT_PIPELINE_TEST01_EXPECTED_GAPS: Dict[str, float] = {
        "histogram_gap": 1.2,
        "interface_stats_gap": 15,
        "control_stats_gap": 15,
        "platf_cpu_gap": 70,
        "platf_mem_gap": 70,
        "platf_file_gap": 70,
        "platf_envir_gap": 70,
        "platf_disk_gap": 70,
        "buffer_gap": 15,
    }
    # NVOS split-pipeline / coexistence Interface-QoS: only interface + platform families
    # (no histogram / buffer / control-plane / lldp). Shared by
    # ``test_otel_split_pipelines`` and coexistence collectors.
    NVOS_SPLIT_FAMILY_MAP: Tuple[Tuple[str, str], ...] = (
        ("nvswitch_interface_if", "interface_stats_gap"),
        ("nvswitch_platform_cpu", "platf_cpu_gap"),
        ("nvswitch_platform_memory", "platf_mem_gap"),
        ("nvswitch_platform_disk", "platf_disk_gap"),
        ("nvswitch_platform_environment", "platf_envir_gap"),
    )
    NVOS_SPLIT_EXPECTED_GAPS: Dict[str, float] = {
        "interface_stats_gap": 15.0,
        "platf_cpu_gap": 70.0,
        "platf_mem_gap": 70.0,
        "platf_disk_gap": 70.0,
        "platf_envir_gap": 70.0,
    }

    # System telemetry labels for ``Test_Otel_Mgmt_Vrf_Insecure_CLI_Coverage`` (SSIM topo).
    DEVICE_LABELS: Tuple[Tuple[str, str], ...] = (
        ("device-l1", "Device Label-1"),
        ("device-l2", "Device Label-2"),
        ("device-l3", "Device Label-3"),
        ("device-l4", "Device Label-4"),
        ("device-l5", "Device Label-5"),
        ("device-l6", "Device Label-6"),
        ("device-l7", "Device Label-7"),
        ("device-l8", "Device Label-8"),
        ("device-l9", "Device Label-9"),
        ("device-l10", "Device Label-10"),
    )

    INTF_LABELS: Tuple[str, ...] = (
        "mgmtVrfNoTls-l1",
        "Management VRF Insecure Label-1",
        "mgmtVrfNoTls-l10",
        "Management VRF Insecure Label-10",
        "mgmtVrfNoTls-l2",
        "Management VRF Insecure Label-2",
        "mgmtVrfNoTls-l3",
        "Management VRF Insecure Label-3",
        "mgmtVrfNoTls-l4",
        "Management VRF Insecure Label-4",
        "mgmtVrfNoTls-l5",
        "Management VRF Insecure Label-5",
        "mgmtVrfNoTls-l6",
        "Management VRF Insecure Label-6",
        "mgmtVrfNoTls-l7",
        "Management VRF Insecure Label-7",
        "mgmtVrfNoTls-l8",
        "Management VRF Insecure Label-8",
        "mgmtVrfNoTls-l9",
        "Management VRF Insecure Label-9",
    )
