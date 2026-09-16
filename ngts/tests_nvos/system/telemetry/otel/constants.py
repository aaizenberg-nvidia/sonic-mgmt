"""Constants for the NVOS OTEL telemetry test suite (insecure OTLP gRPC, file exporter).

Generic telemetry constants (subtree names, default values, platform-stats class
names, etc.) live in :mod:`ngts.nvos_constants.constants_nvos.TelemetryConsts`.
This module holds only the OTel-collector-specific test choices: binary names,
install paths/URLs, timeouts, the minimal collector YAML, and capability /
metric-name gating used by the OTel coverage tests.
"""

import enum
from typing import Dict, Tuple
from ngts.nvos_constants.constants_nvos import TelemetryConsts


class OtelCapability:
    """Capabilities emitted by ``helpers.dut_capabilities``."""

    HAS_IB_ROUTER_PROFILE = "has-ib-router-profile"
    SUPPORTS_PEER_PORT_STATS = "supports-peer-port-stats"
    HAS_CONNECTED_TRANSCEIVERS = "has-connected-transceivers"
    # NVLink-only platform metrics (absent on IB / Crocodile-class systems).
    SUPPORTS_NVLINK_PLATFORM_METRICS = "supports-nvlink-platform-metrics"
    # PDB (power distribution board) metrics, advertised by ``platform.json``.
    SUPPORTS_SYSTEM_POWER = "supports-system-power"
    # PSU metrics, one series per ``PSU_INFO`` row. Derived from the device model's
    # ``psu_list``, which ``NoPSUCapability`` clears on PSU-less systems (Black
    # Mamba DGX, Rosalind); those have no rows, so none of the series are exported.
    HAS_PSU = "has-psu"


class OtelCliTestConst:
    """Test inputs for the NVOS OTEL CLI configuration tests (test plan Section 4).

    Holds only values the tests *choose* (ids, non-default ports, staged sample
    intervals). Product defaults asserted after ``unset`` come from
    :class:`~ngts.nvos_constants.constants_nvos.TelemetryConsts.Defaults`.
    """

    # Sections 4.1 / 4.2 run once per OTLP transport.
    ALL_TRANSPORTS: Tuple[str, ...] = (TelemetryConsts.GRPC, TelemetryConsts.HTTP)

    # `encoding` exists only on the http transport (Section 4.1 steps 10-13).
    TRANSPORT_WITH_ENCODING = TelemetryConsts.HTTP

    # Per-transport factory default, so a parametrized test can assert the right one.
    DEFAULT_INSECURE_BY_TRANSPORT: Dict[str, str] = {
        TelemetryConsts.GRPC: TelemetryConsts.Defaults.GRPC_INSECURE,
        TelemetryConsts.HTTP: TelemetryConsts.Defaults.HTTP_INSECURE,
    }

    # --- Section 4.2: destinations ------------------------------------------
    # The destinations are configured and shown, never contacted, so any
    # syntactically valid id of the right address family works.
    DEST_TYPE_IPV4 = "ipv4"
    DEST_TYPE_IPV6 = "ipv6"
    DEST_TYPE_HOSTNAME = "hostname"
    ALL_DEST_TYPES: Tuple[str, ...] = (DEST_TYPE_IPV4, DEST_TYPE_IPV6, DEST_TYPE_HOSTNAME)
    DEST_ID_BY_TYPE: Dict[str, str] = {
        DEST_TYPE_IPV4: "10.210.25.111",
        DEST_TYPE_IPV6: "fc00::111",
        DEST_TYPE_HOSTNAME: "otel-cli-collector",
    }

    # Non-default per-destination port (both transports default to 4317/4318).
    NON_DEFAULT_DESTINATION_PORT = 15317

    # --- Section 4.3: labels and metric classes -----------------------------
    # Ids distinct from the Cumulus baseline labels so the two suites never
    # collide on a shared setup. Descriptions must be single CLI tokens: the NVUE
    # `set` wrapper interpolates the value into the command without quoting, so a
    # value containing spaces is parsed as extra parameters and rejected.
    SYSTEM_LABEL_ID = "nvos-plan-site"
    SYSTEM_LABEL_DESCRIPTION = "nvos-plan-host"
    INTERFACE_LABEL_ID = "nvos-plan-role"
    INTERFACE_LABEL_DESCRIPTION = "nvos-plan-spine"

    # Metric families Section 4.3 is parametrized over.
    ALL_METRIC_CLASSES: Tuple[str, ...] = TelemetryConsts.ALL_STATS_SUBTREES

    # Families exposing `class phy` (Section 4.3 steps 3 / 7 / 8). ib-router-stats
    # has no `class` node; platform-stats has categories, which the plan excludes.
    METRIC_CLASSES_WITH_PHY: Tuple[str, ...] = (
        TelemetryConsts.INTERFACE_STATS,
        TelemetryConsts.PEER_PORT_STATS,
    )

    # Staged sample interval. platform-stats enforces a 60s floor, so it gets its
    # own value; the other families accept the shorter one.
    STAGED_SAMPLE_INTERVAL_SEC = 30
    STAGED_PLATFORM_SAMPLE_INTERVAL_SEC = 120

    # --- Section 4.4: stats groups ------------------------------------------
    STATS_GROUP_1 = "nvos-plan-sg-one"
    STATS_GROUP_2 = "nvos-plan-sg-two"


class OtelFunctionalTestConst:
    """Test inputs for the NVOS OTEL functional tests (test plan Section 5).

    Section 5 exercises the data plane, so unlike Section 4 these values size
    collection windows and bound value comparisons.
    """

    # 5.1 step 2: the window must cover "at least two samples for the max sample
    # interval of enabled classes", so the slowest enabled class must emit twice.
    MIN_SAMPLES_IN_WINDOW = 2

    # --- Randomized axes on the data path ------------------------------------
    # 5.1 and 5.2 both export to "a single gRPC/http destination (chosen
    # randomly)". Unlike Section 4, which only configures a destination, these
    # items push real payloads through it, so the collector must actually receive
    # on the chosen transport — see ``collector_receivers_for``.
    DATA_PATH_TRANSPORTS: Tuple[str, ...] = OtelCliTestConst.ALL_TRANSPORTS

    # 5.1 additionally randomizes dest_id_type over {ipv4, ipv6, hostname}, with
    # "external OTEL server reachable over IPv4, IPv6 and hostname" as its stated
    # precondition. All three point at the same running collector. The IPv6
    # address is read off the collector host's interface at run time rather than
    # taken from DNS: the lab publishes no AAAA record for these hosts even where
    # their management interface carries a ULA the DUT routes to. Not every setup
    # routes IPv6 between DUT and collector, so the usable subset is probed per
    # run by ``available_data_path_dest_types``.
    DATA_PATH_DEST_TYPES: Tuple[str, ...] = (
        OtelCliTestConst.DEST_TYPE_IPV4,
        OtelCliTestConst.DEST_TYPE_IPV6,
        OtelCliTestConst.DEST_TYPE_HOSTNAME,
    )

    # Name written into the DUT's ``/etc/hosts`` when the collector's own DNS name
    # is not resolvable from the DUT, so the hostname destination type always has
    # a real name to resolve.
    COLLECTOR_HOST_ALIAS = "otel-collector-primary"

    # Probes used to prove a destination is reachable before it is configured. Two
    # is enough to tell "routed" from "not routed" without padding every run.
    DEST_PING_COUNT = 2

    # 5.2 step 1: "start OTLP capture for at least three sample intervals". With
    # interface-stats at its 10s default this is a 30s span, so the suite's 70s
    # floor already covers it; platform-stats (60s) is what makes the window long.
    MIN_SAMPLE_INTERVALS = 3

    # 5.2: the family disabled mid-capture (step 4) and the family whose *sub*-class
    # is disabled afterwards (step 5). Two different families, as the plan requires.
    DISABLED_FAMILY = TelemetryConsts.PLATFORM_STATS
    DISABLED_SUB_CLASS_FAMILY = TelemetryConsts.INTERFACE_STATS
    DISABLED_SUB_CLASS = TelemetryConsts.PHY

    # 5.3: the single metric compared across the OTEL and gNMI paths. NVOS exports
    # IB/if counters (no ether-stats catalog), so this is the one interface counter
    # both paths expose — the same pairing the ported coexistence validators use
    # (``cumulus/coexistence.py`` ``INTERFACE_GNMI_TO_OTEL_METRIC_NVOS``).
    SHARED_METRIC_OTEL_NAME = "nvswitch_interface_if_in_broadcast_pkts"
    SHARED_METRIC_GNMI_LEAF = "in-broadcast-pkts"
    SHARED_METRIC_GNMI_PREFIX_FMT = "interfaces/interface[name={interface}]/state/counters"

    # OTLP attribute naming the interface a datapoint belongs to.
    INTERFACE_ATTRIBUTE_KEY = "interface"

    # OTLP prefix of the slowest family (platform-stats, 60s), used to prove a
    # window really spanned several sample intervals.
    SLOWEST_FAMILY_METRIC_PREFIX = "nvswitch_platform_"

    # OTLP prefix of the family 5.2 keeps enabled, whose values must stay sane
    # across both disables.
    KEPT_FAMILY_METRIC_PREFIX = "nvswitch_interface_"

    # 5.3 step 4: "within the expected sampling tolerance". The two paths sample
    # independently, so a counter read over gNMI and over OTLP in the same window
    # can legitimately differ by the traffic seen between the two samples.
    SHARED_METRIC_TOLERANCE_PERCENT = 20.0

    # gnmic budget for a subscription that has to outlive the OTLP window.
    GNMI_CLIENT_CMD_TIME_SEC = 60
    GNMI_STREAM_SETTLE_SEC = 5


class OtelCollectorLabel(enum.Enum):
    """Identifies which of the two OTel collector instances in the suite this is.

    Both instances are identical OTel collectors; the label only selects per-instance
    constants (``OtelCollectorConst.{PRIMARY,SECONDARY}_*``) and which ``engines.*``
    host the collector runs on (see ``OtelCollectorConst.HOST_ATTR_BY_LABEL``).
    """

    PRIMARY = "primary"
    SECONDARY = "secondary"


# Default: one gRPC receiver on the IPv4 wildcard, which is what every suite in
# this tree exports over and what collectors here have always listened on.
#
# The http receiver and the dual-stack bind are opt-in per test rather than
# defaults, so a suite that does not randomize its transport or destination keeps
# byte-identical collector configuration. Tests that do randomize reconfigure
# their collector through ``collector_receivers_for`` and restore it afterwards.
# Module-level so the config builders below can default to it; re-exported as
# ``OtelCollectorConst.OTLP_GRPC_BIND_ADDR`` for callers outside this module.
_OTLP_GRPC_BIND_ADDR = "0.0.0.0"
_OTLP_DUAL_STACK_BIND_ADDR = "[::]"


def _endpoint(bind_addr: str, port: int) -> str:
    """``host:port``, quoted only where YAML needs it.

    A bare ``[::]:4317`` parses as a nested sequence rather than a string, so the
    dual-stack address has to be quoted. The IPv4 default is left bare, keeping
    the config of every collector that does not opt into dual-stack identical to
    what it has always been.
    """
    endpoint = f"{bind_addr}:{port}"
    return f'"{endpoint}"' if bind_addr.startswith("[") else endpoint


def _otel_collector_config_yaml(
    otlp_port: int,
    file_exporter_path: str,
    max_megabytes: int,
    max_backups: int,
    bind_addr: str = _OTLP_GRPC_BIND_ADDR,
    http_port: int = TelemetryConsts.Defaults.HTTP_PORT,
    include_http: bool = False,
) -> str:
    """OTEL collector config: OTLP receivers in, file exporter out (with rotation).

    gRPC only by default, which is what every suite here exports over. The test
    plan also randomizes onto http (5.1, 5.2, 5.4) and enumerates both (5.5,
    5.6, 5.8); those items switch their collector over via ``include_http``
    rather than every collector carrying a receiver it never serves.
    """
    protocols = f"""      grpc:
        endpoint: {_endpoint(bind_addr, otlp_port)}"""
    if include_http:
        protocols += f"""
      http:
        endpoint: {_endpoint(bind_addr, http_port)}"""
    return f"""receivers:
  otlp:
    protocols:
{protocols}
processors: {{}}
exporters:
  file:
    path: {file_exporter_path}
    rotation:
      max_megabytes: {max_megabytes}
      max_backups: {max_backups}
service:
  telemetry:
    metrics:
      level: none
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [file]
"""


def _otel_collector_config_yaml_tls(
    otlp_port: int,
    file_exporter_path: str,
    max_megabytes: int,
    max_backups: int,
    *,
    cert_file: str,
    key_file: str,
    bind_addr: str = "0.0.0.0",
) -> str:
    """OTEL collector config with TLS on the OTLP gRPC receiver."""
    endpoint = f"{bind_addr}:{otlp_port}"
    return f"""receivers:
  otlp:
    protocols:
      grpc:
        endpoint: {endpoint}
        tls:
          cert_file: {cert_file}
          key_file: {key_file}
processors: {{}}
exporters:
  file:
    path: {file_exporter_path}
    rotation:
      max_megabytes: {max_megabytes}
      max_backups: {max_backups}
service:
  telemetry:
    metrics:
      level: none
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [file]
"""


class OtelCollectorConst:
    BINARY_NAME = "otelcol-contrib"

    OTLP_GRPC_PORT = 4317
    OTLP_HTTP_PORT = TelemetryConsts.Defaults.HTTP_PORT
    OTLP_GRPC_BIND_ADDR = _OTLP_GRPC_BIND_ADDR
    # Only for tests that export to an IPv6 destination; see ``_OTLP_GRPC_BIND_ADDR``.
    OTLP_DUAL_STACK_BIND_ADDR = _OTLP_DUAL_STACK_BIND_ADDR

    # Receiver port per OTLP transport. Only the receivers a config declares are
    # up (see ``_otel_collector_config_yaml``); this says which port a given
    # export should be pointed at.
    OTLP_PORT_BY_TRANSPORT: Dict[str, int] = {
        TelemetryConsts.GRPC: OTLP_GRPC_PORT,
        TelemetryConsts.HTTP: OTLP_HTTP_PORT,
    }

    # Both collectors install identically: download the upstream `.tar.gz`, drop the
    # `otelcol-contrib` binary in `/usr/local/bin`, and run it as a `nohup` foreground
    # process with a log file (identified later via ``pgrep -f`` on the binary + config
    # signature). One pinned version for both roles to avoid skew.
    OTEL_COLLECTOR_VERSION = "0.130.0"
    OTEL_COLLECTOR_CONTRIB_GITHUB_RELEASES_BASE = (
        "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download"
    )

    OTEL_TLS_CA_NAME = "ca"

    # Per-label host attribute on the ``engines`` fixture. PRIMARY runs on the sonic-mgmt
    # container (per-DUT); SECONDARY runs on the HA player (per-testbed).
    HOST_ATTR_BY_LABEL: Dict[OtelCollectorLabel, str] = {
        OtelCollectorLabel.PRIMARY: "sonic_mgmt",
        OtelCollectorLabel.SECONDARY: "ha",
    }

    # Primary collector on engines.sonic_mgmt.
    BASE_PATH = "/etc/otelcol"
    PRIMARY_CONFIG_PATH = f'{BASE_PATH}/primary-config.yaml'
    PRIMARY_OUTPUT_JSON_PATH = f'{BASE_PATH}/primary-test.json'
    PRIMARY_OUTPUT_JSON_ROTATED_GLOB = f'{BASE_PATH}/primary-test-*.json'
    PRIMARY_STAGED_OUTPUT_JSON_PATH = "/tmp/otel-artifacts/otel-primary-out.json"
    PRIMARY_LOG_PATH = "/tmp/otelcol-primary.log"
    PRIMARY_FILE_EXPORT_MAX_MB = 600
    PRIMARY_FILE_EXPORT_MAX_BACKUPS = 1

    # Secondary collector on engines.ha (per-testbed; fails if HA is absent).
    SECONDARY_CONFIG_PATH = f'{BASE_PATH}/secondary-config.yaml'
    SECONDARY_OUTPUT_JSON_PATH = f'{BASE_PATH}/secondary-test.json'
    SECONDARY_OUTPUT_JSON_ROTATED_GLOB = f'{BASE_PATH}/secondary-test-*.json'
    SECONDARY_STAGED_OUTPUT_JSON_PATH = "/tmp/otel-artifacts/otel-secondary-out.json"
    SECONDARY_LOG_PATH = "/tmp/otelcol-secondary.log"
    SECONDARY_FILE_EXPORT_MAX_MB = 600
    SECONDARY_FILE_EXPORT_MAX_BACKUPS = 1

    # Per-label file-exporter rotation knobs, so a config built for a given collector always
    # follows that collector's PRIMARY_*/SECONDARY_* values instead of hard-coding one label.
    FILE_EXPORT_MAX_MB_BY_LABEL: Dict[OtelCollectorLabel, int] = {
        OtelCollectorLabel.PRIMARY: PRIMARY_FILE_EXPORT_MAX_MB,
        OtelCollectorLabel.SECONDARY: SECONDARY_FILE_EXPORT_MAX_MB,
    }
    FILE_EXPORT_MAX_BACKUPS_BY_LABEL: Dict[OtelCollectorLabel, int] = {
        OtelCollectorLabel.PRIMARY: PRIMARY_FILE_EXPORT_MAX_BACKUPS,
        OtelCollectorLabel.SECONDARY: SECONDARY_FILE_EXPORT_MAX_BACKUPS,
    }

    PRIMARY_CONFIG_YAML = _otel_collector_config_yaml(
        OTLP_GRPC_PORT,
        PRIMARY_OUTPUT_JSON_PATH,
        PRIMARY_FILE_EXPORT_MAX_MB,
        PRIMARY_FILE_EXPORT_MAX_BACKUPS,
        bind_addr=OTLP_GRPC_BIND_ADDR,
    )
    SECONDARY_CONFIG_YAML = _otel_collector_config_yaml(
        OTLP_GRPC_PORT,
        SECONDARY_OUTPUT_JSON_PATH,
        SECONDARY_FILE_EXPORT_MAX_MB,
        SECONDARY_FILE_EXPORT_MAX_BACKUPS,
        bind_addr=OTLP_GRPC_BIND_ADDR,
    )

    # Telemetry subtree names (interface-stats / peer-port-stats / ib-router-stats /
    # platform-stats) and platform-stats class names live in TelemetryConsts as the
    # single source of truth; consumers should reference them via
    # ``TelemetryConsts.{INTERFACE_STATS, PEER_PORT_STATS, IB_ROUTER_STATS,
    # PLATFORM_STATS}``, ``TelemetryConsts.ALL_STATS_SUBTREES`` and
    # ``TelemetryConsts.PLATFORM_CLASSES`` rather than re-declaring the strings here.

    # NVUE telemetry sample intervals. `platform-stats export sample-interval` is restricted
    # to [60, 86400]; the other groups have no enforced lower bound, so we keep them at 10s.
    INTERFACE_STATS_SAMPLE_INTERVAL_SEC = 10
    PEER_PORT_STATS_SAMPLE_INTERVAL_SEC = 10
    IB_ROUTER_STATS_SAMPLE_INTERVAL_SEC = 10
    PLATFORM_STATS_SAMPLE_INTERVAL_SEC = 60

    # Floor of 70s ensures a platform-stats sample (60s + warm-up) always fits in the wait.
    OTEL_COLLECTION_WARM_UP_SEC = 10
    OTEL_COLLECTION_MIN_WAIT_SEC = 70

    @staticmethod
    def collection_window_sec(max_sample_interval_sec: int) -> int:
        return max(
            max_sample_interval_sec + OtelCollectorConst.OTEL_COLLECTION_WARM_UP_SEC,
            OtelCollectorConst.OTEL_COLLECTION_MIN_WAIT_SEC,
        )

    @staticmethod
    def artifact_poll_timeout_sec(max_sample_interval_sec: int) -> int:
        """Poll budget for a non-empty file-exporter artifact.

        Used after :meth:`collection_window_sec` sleep (or includes that window when no
        prior sleep). Cumulus/mgmt OTLP can deliver the first non-empty batch slightly
        after the nominal collection window; logs showed ~62s poll after a 70s sleep
        with ``ARTIFACT_TIMEOUT_SEC=60`` alone.
        """
        return (
            OtelCollectorConst.collection_window_sec(max_sample_interval_sec) +
            OtelCollectorConst.ARTIFACT_TIMEOUT_SEC
        )

    START_TIMEOUT_SEC = 90
    START_RETRY_INTERVAL_SEC = 3
    ARTIFACT_TIMEOUT_SEC = 60

    # Present in OTLP export via prometheus-node-exporter / scrape pipeline; not NVUE
    # ``nvswitch_*`` catalog metrics. Ignored when YAML is absent on the DUT.
    OTEL_PROMETHEUS_SIDECAR_NAME_PREFIXES: Tuple[str, ...] = (
        "node_",
        "scrape_",
        "process_",
        "promhttp_",
        "go_",
    )
    OTEL_PROMETHEUS_SIDECAR_EXACT_NAMES: Tuple[str, ...] = ("up",)

    # Source of truth on the DUT for the expected OTLP metric names.
    METRICS_CLASSES_PATH_ON_SWITCH = "/etc/nv-umf-manager/metrics-classes.yaml"
    METRICS_CLASSES_STAGED_ON_DUT = "/tmp/ngts-metrics-classes.yaml"
    METRICS_CLASSES_CANDIDATE_PATHS = (
        METRICS_CLASSES_PATH_ON_SWITCH,
        "/usr/share/nv-umf-manager/metrics-classes.yaml",
        "/usr/lib/nv-umf-manager/metrics-classes.yaml",
        "/opt/mellanox/nv-umf-manager/metrics-classes.yaml",
    )

    # NVOS-only: UMF agent mapping contract (metric type + attribute labels per metric).
    # The NVOS equivalent of the Cumulus attribute-count / value-type checks.
    AGENT_MAPPINGS_PATH_ON_SWITCH = (
        "/etc/nv-umf-agent/mappings/umf-agent/agent-mappings.yaml"
    )
    AGENT_MAPPINGS_STAGED_ON_DUT = "/tmp/ngts-agent-mappings.yaml"
    AGENT_MAPPINGS_CANDIDATE_PATHS = (AGENT_MAPPINGS_PATH_ON_SWITCH,)

    # `metrics-classes.yaml` group-name prefixes used for ``startswith`` matching.
    # Every family name is also the prefix of its subgroups: ``interface-stats`` covers
    # ``interface-stats-phy``, ``peer-port-stats`` covers ``peer-port-stats-phy``, and
    # ``platform-stats`` covers all ``platform-stats-*`` subgroups (cpu / disk / memory /
    # health-info / asic-power / platform-info / environment-sensor / transceiver-info).
    METRICS_CLASSES_PLATFORM_STATS_GROUP_PREFIXES = (TelemetryConsts.PLATFORM_STATS,)
    METRICS_CLASSES_FULL_EXPORT_GROUP_PREFIXES = (
        TelemetryConsts.INTERFACE_STATS,
        TelemetryConsts.PEER_PORT_STATS,
        TelemetryConsts.PLATFORM_STATS,
        TelemetryConsts.IB_ROUTER_STATS,
    )

    # --- Class-aware expected-set derivation (test-declared enablement model) ----
    # Sentinel for the always-on "base" class of a family (the non-phy / non-
    # categorized metrics emitted whenever that family's export is enabled).
    TELEMETRY_BASE_CLASS = "__base__"

    # Exact ``metrics-classes.yaml`` group name(s) per (family, class-token), where
    # class-token is ``TELEMETRY_BASE_CLASS`` or ``TelemetryConsts.PHY``. Platform-stats
    # classes are resolved dynamically via ``METRICS_CLASSES_PLATFORM_GROUP_FMT`` below.
    METRICS_CLASSES_GROUP_BY_FAMILY_CLASS: Dict[Tuple[str, str], Tuple[str, ...]] = {
        (TelemetryConsts.INTERFACE_STATS, TELEMETRY_BASE_CLASS): ("interface-stats",),
        (TelemetryConsts.INTERFACE_STATS, TelemetryConsts.PHY): ("interface-stats-phy",),
        (TelemetryConsts.PEER_PORT_STATS, TELEMETRY_BASE_CLASS): ("peer-port-stats",),
        (TelemetryConsts.PEER_PORT_STATS, TelemetryConsts.PHY): ("peer-port-stats-phy",),
        (TelemetryConsts.IB_ROUTER_STATS, TELEMETRY_BASE_CLASS): ("ib-router-stats",),
    }

    # platform-stats class category -> exact YAML group name (e.g. ``cpu`` ->
    # ``platform-stats-cpu``).
    METRICS_CLASSES_PLATFORM_GROUP_FMT = "platform-stats-{cls}"

    # When the capability is ABSENT on the DUT, expected names starting with any of the listed
    # prefixes are removed from the *expected* set before strict equality.
    OTLP_METRIC_NAME_PREFIXES_GATED_BY_CAPABILITY: Dict[str, Tuple[str, ...]] = {
        OtelCapability.HAS_CONNECTED_TRANSCEIVERS: ("nvswitch_platform_transceiver_",),
        OtelCapability.HAS_IB_ROUTER_PROFILE: ("nvswitch_ib_router_",),
        OtelCapability.SUPPORTS_PEER_PORT_STATS: ("nvswitch_peer_port_",),
        OtelCapability.SUPPORTS_SYSTEM_POWER: ("nvswitch_platform_environment_pdb_",),
        # Both spellings: Cumulus renamed the family to ``power_supply_*`` and
        # ``validations.py`` still maps it back to the legacy ``psu_*``.
        OtelCapability.HAS_PSU: (
            "nvswitch_platform_environment_power_supply_",
            "nvswitch_platform_environment_psu_",
        ),
    }

    # ``platform.json`` capability flags probed on the DUT. ``system-power-supported``
    # is absent (not ``no``) on systems without a PDB, so only the literal
    # ``yes`` counts as supported.
    PLATFORM_JSON_PATH_FMT = "/usr/share/sonic/device/{platform}/platform.json"
    PLATFORM_JSON_PLATFORM_CMD = "sonic-cfggen -H -v DEVICE_METADATA.localhost.platform"
    PLATFORM_JSON_CAPABILITIES_KEY = "capabilities"
    PLATFORM_JSON_SYSTEM_POWER_KEY = "system-power-supported"
    PLATFORM_JSON_CAPABILITY_SUPPORTED_VALUE = "yes"

    # NVOS exports these even when the ib-routing profile is disabled; do not gate them out.
    OTLP_IB_ROUTER_METRICS_ALWAYS_PRESENT: Tuple[str, ...] = (
        "nvswitch_ib_router_enabled",
        "nvswitch_ib_router_swid_count",
    )

    # Exact metric names removed from expected when the linked capability is absent.
    OTLP_METRIC_NAMES_GATED_BY_CAPABILITY: Dict[str, Tuple[str, ...]] = {
        OtelCapability.SUPPORTS_NVLINK_PLATFORM_METRICS: (
            "nvswitch_platform_asic_avg_power",
            "nvswitch_platform_environment_leak_sensor_status",
        ),
    }
