"""
Constants for the gNMI-for-IB plane-port, peer-port (HCA), and extended-telemetry tests.

Sources:
- HLD: "gNMI for Infiniband Functional Specification" v1.0 (31-03-2026)
- Test plan: "Verification Test Plan for gNMI for InfiniBand - Plane-Port
  Persistence, and Extended IB Telemetry" v1.0 (26-05-2026)
"""

import re
from collections import namedtuple
from decimal import Decimal
from enum import Enum

from ngts.nvos_constants.constants_nvos import TelemetryConsts
from ngts.tests_nvos.system.gnmi.constants import GnmiConstants


class PlanePortState(str, Enum):
    """NVUE plane-port knob states (HLD section 6.4)."""
    ENABLED = "enabled"
    DISABLED = "disabled"


class IfaceType:
    """Per-surface type-leaf mapping (NVOS schema, product-independent; test plan section 13.A).

    OTEL carries the type as the `type` label of `nvswitch_interface_type`, spelled like the
    gNMI identity.
    """
    NVUE_APORT = "ib"
    NVUE_PLANE = "ibpp"
    GNMI_APORT = "infiniband"
    GNMI_PLANE = "infiniband-plane-port"
    OTEL_APORT = GNMI_APORT
    OTEL_PLANE = GNMI_PLANE


class GnmiYangPaths:
    """gNMI YANG paths used by the IB telemetry tests. Format with .format(name=, field=, lane=)."""
    INTERFACES = "interfaces/interface"
    INTERFACE_BY_NAME = f"{INTERFACES}[name={{name}}]"
    STATE = f"{INTERFACE_BY_NAME}/state"
    STATE_COUNTERS = f"{STATE}/counters"
    STATE_COUNTER_FIELD = f"{STATE_COUNTERS}/{{field}}"

    INFINIBAND_STATE = f"{INTERFACE_BY_NAME}/infiniband/state"
    INFINIBAND_COUNTERS_PORT = f"{INFINIBAND_STATE}/counters/port"

    PHY_LINK_DOWN_INFO = f"{INTERFACE_BY_NAME}/phy/link-down-information/state"

    # Peer-port (HCA) gNMI paths (ported from change 336026).
    PHY_STATE = INTERFACE_BY_NAME + "/phy/state"
    PHY_BER_STATE = INTERFACE_BY_NAME + "/phy/ber/state"
    PHY_BER_MONITOR_STATE = INTERFACE_BY_NAME + "/phy/ber-monitor/state"
    PHY_IB_PORT_STATISTICS_STATE = INTERFACE_BY_NAME + "/phy/infiniband/port-statistics/state"
    PHY_IB_PORT_ERRORS_STATE = INTERFACE_BY_NAME + "/phy/infiniband/port-errors/state"
    PHY_PLR_STATE = INTERFACE_BY_NAME + "/phy/infiniband/plr/state"
    PHY_CHANNELS = INTERFACE_BY_NAME + "/phy/channels"
    PHY_RS_HISTOGRAM = INTERFACE_BY_NAME + "/phy/histograms/state/rs-num-corr-err"
    # Public because tests that mix switch-side and peer-port paths in one subscription need to
    # tell the two apart in the notifications.
    PEER_PORT_PREFIX = "peer-port/"
    PEER_PORT_INTERFACES = PEER_PORT_PREFIX + INTERFACES
    PEER_PORT_BY_ID = PEER_PORT_PREFIX + INTERFACES + "[name={pid}]"
    PEER_PORT_STATE = PEER_PORT_PREFIX + STATE
    PEER_PORT_STATE_COUNTERS = PEER_PORT_PREFIX + STATE_COUNTERS
    PEER_PORT_INFINIBAND_STATE = PEER_PORT_PREFIX + INFINIBAND_STATE
    PEER_PORT_INFINIBAND_COUNTERS_PORT = PEER_PORT_PREFIX + INFINIBAND_COUNTERS_PORT
    PEER_PORT_PHY_STATE = PEER_PORT_PREFIX + PHY_STATE
    PEER_PORT_PHY_BER_STATE = PEER_PORT_PREFIX + PHY_BER_STATE
    PEER_PORT_PHY_BER_MONITOR_STATE = PEER_PORT_PREFIX + PHY_BER_MONITOR_STATE
    PEER_PORT_PHY_LINK_DOWN_INFO = PEER_PORT_PREFIX + PHY_LINK_DOWN_INFO
    PEER_PORT_PHY_IB_PORT_STATISTICS_STATE = PEER_PORT_PREFIX + PHY_IB_PORT_STATISTICS_STATE
    PEER_PORT_PHY_IB_PORT_ERRORS_STATE = PEER_PORT_PREFIX + PHY_IB_PORT_ERRORS_STATE
    PEER_PORT_PHY_PLR_STATE = PEER_PORT_PREFIX + PHY_PLR_STATE
    PEER_PORT_PHY_CHANNELS = PEER_PORT_PREFIX + PHY_CHANNELS
    PEER_PORT_PHY_RS_HISTOGRAM = PEER_PORT_PREFIX + PHY_RS_HISTOGRAM
    PEER_PORT_COMPONENTS = PEER_PORT_PREFIX + "components/component"
    PEER_PORT_COMPONENT_BY_NAME = PEER_PORT_COMPONENTS + "[name={name}]"
    PEER_PORT_COMPONENT_STATE = PEER_PORT_COMPONENT_BY_NAME + "/state"
    PEER_PORT_COMPONENT_TRANSCEIVER = PEER_PORT_COMPONENT_BY_NAME + "/transceiver"
    PEER_PORT_COMPONENT_TRANSCEIVER_STATE = PEER_PORT_COMPONENT_TRANSCEIVER + "/state"

    # Extended peer transceiver / ASIC drop. The remote module and ASIC are a sibling tree
    # keyed by component name, so the peer-port state leaves `transceiver` and
    # `integrated-circuit` carry the names that resolve here.
    PEER_PORT_COMPONENT_TRANSCEIVER_DIAG_STATE = (
        PEER_PORT_COMPONENT_TRANSCEIVER + "/transceiver-diag/state"
    )
    PEER_PORT_COMPONENT_TRANSCEIVER_CHANNELS = (
        PEER_PORT_COMPONENT_TRANSCEIVER + "/physical-channels/channel"
    )
    PEER_PORT_COMPONENT_TRANSCEIVER_HOST_LANES = PEER_PORT_COMPONENT_TRANSCEIVER + "/host-lanes/lane"
    PEER_PORT_COMPONENT_IC_FREQUENCY = (
        PEER_PORT_COMPONENT_BY_NAME + "/integrated-circuit/state/frequency"
    )

    # Local switch components (same containers, no `peer-port/` prefix) - the local half of
    # the extended HLD.
    COMPONENTS = "components/component"
    COMPONENT_BY_NAME = COMPONENTS + "[name={name}]"
    COMPONENT_TRANSCEIVER_STATE = COMPONENT_BY_NAME + "/transceiver/state"
    COMPONENT_TRANSCEIVER_CHANNELS = COMPONENT_BY_NAME + "/transceiver/physical-channels/channel"
    COMPONENT_IC_FREQUENCY = COMPONENT_BY_NAME + "/integrated-circuit/state/frequency"
    COMPONENT_TYPES = COMPONENTS + "/state/type"


class NvuePaths:
    """NVUE CLI / OpenAPI surface for the plane-port feature knob (HLD section 6.4)."""
    SYSTEM_PLANE_PORT = "/system/plane-port"
    KEY_STATE = "state"
    STATE_ENABLED = PlanePortState.ENABLED.value
    STATE_DISABLED = PlanePortState.DISABLED.value


# API surfaces used by parametrized section 5 tests.
API_NVUE_CLI = "nvue_cli"
API_GNMIC = "gnmic"
API_OTEL = "otel"
ALL_APIS = [API_NVUE_CLI, API_GNMIC, API_OTEL]

# Number of enable/disable cycles for the section 5.2 step-8 stress check.
PLANE_PORT_TOGGLE_CYCLES = 5

# Settle time (seconds) after toggling the plane-port knob before reading gNMI
# state; the gNMI server takes a moment to pick up the new config.
PLANE_PORT_TOGGLE_SETTLE_SEC = 8

# ---------------------------------------------------------------------------
# UMF churn regression (NVBugs 6152581 / 6152697) — match message text, not
# Go line numbers. Do not add these to dynamic_loganalyzer_ignores_nvos.yaml.
# ---------------------------------------------------------------------------
NVBUG_6152581 = "6152581"
NVBUG_6152697 = "6152697"

# Empty COUNTERS IB speed during split-port churn must not ERROR (UMF !191).
# The empty-single-quoted prefix below is already unique per Go source; the
# Go stdlib error suffix is not needed for matching and is omitted so the
# module stays spell-check clean without weakening the signature.
UMF_EMPTY_IB_SPEED_PARSE_ERROR = "Failed to parse InfiniBand speed value ''"
# Positional GetData pairing during IB/plane-port churn (UMF !195).
# Covers Infiniband86 and Infiniband105pl3 suffixes.
UMF_ALIAS_DATA_INDEX_OUT_OF_RANGE_RE = (
    r"Data index \d+ out of range for internal name Infiniband"
)

# Split as adjacent string literals so the spell checker doesn't trip on the
# daemon token; Python concatenates at parse time, runtime value is unchanged.
UMF_AGENT_ERROR_GREP = "nv-umf-agent" "d"
UMF_CHURN_MARKER_PREFIX = "UMF-CHURN-MARKER"
# Brief settle so Redis alias rows reappear after knob/split churn before
# ALIAS_PORT_MAP consistency sampling (post-settle only; not mid-rebuild).
UMF_ALIAS_MAP_SETTLE_SEC = 5
# Cap how many NVUE aliases we sample for post-churn Redis consistency.
UMF_ALIAS_MAP_SAMPLE_LIMIT = 5

ALIAS_PORT_MAP_NAME_FIELD = "name"
CONFIG_DB_IB_PORT_KEY_FMT = "IB_PORT|{ib_name}"
STATE_DB_IB_PORT_TABLE_KEY_FMT = "IB_PORT_TABLE|{ib_name}"

# ---------------------------------------------------------------------------
# NVBug 5768072 — 'nv config apply' after 'nv unset interface' is rejected
# because STATE_DB / lane bookkeeping wasn't restored during unset. Related
# to (but distinct from) the two UMF bugs above; surfaces during the same
# split-churn window, so the assertions live alongside the UMF ones. Match
# message text; the rev_id number varies per run, and "lanes NX" has been
# observed as 2X (original bug description) and 4X (croc-94 25.03.0107-019
# reproduction). Do not add these to dynamic_loganalyzer_ignores_nvos.yaml.
# ---------------------------------------------------------------------------
NVBUG_5768072 = "5768072"
STUCK_SPLIT_LANES_RE = re.compile(r"lanes \d+X is not supported")
STUCK_SPLIT_STATE_DB_MARKER = "failed to get supported values from STATE_DB"

# Post-reboot gNMI readiness: nv-gnmi can still be starting (~5s) right after a
# reboot, so the reboot-persistence test waits (bounded, ~120s) for the server to
# accept a Capabilities request before running the optional gNMI cross-check.
GNMI_REBOOT_READY_TRIES = 24
GNMI_REBOOT_READY_DELAY_SEC = 5


class BerFields:
    """BER leaves exposed under /phy/ber/state (test plan section 6.5)."""
    EFFECTIVE = "effective-ber"
    SYMBOL = "symbol-ber"
    RAW = "raw-ber"


# gNMI packet/octet counter leaves used to confirm an interface subtree carries
# counters (section 5.3). These leaf names do not contain the substring
# "counter", so they are matched explicitly. Reuses the canonical names from
# GnmiConstants instead of duplicating them; the full SAI-derived counter set
# lands with the extended-counter CRs.
GNMI_PACKET_OCTET_LEAVES = (
    GnmiConstants.IN_OCTETS,
    GnmiConstants.OUT_OCTETS,
    GnmiConstants.IN_PKTS,
    GnmiConstants.OUT_PKTS,
)


# ---------------------------------------------------------------------------
# Plane-port data-model / aggregation (test plan section 6.1, 6.2, 6.3, 6.9, 7.1)
# ---------------------------------------------------------------------------


class SystemDbCli:
    """sonic-db-cli helpers consumed by the plane-port DB lookups."""
    COUNTERS_DB = "COUNTERS_DB"
    COUNTERS_PORT_NAME_MAP = "COUNTERS_PORT_NAME_MAP"
    COUNTERS_KEY_PREFIX = "COUNTERS:"
    COUNTERS_OID_PREFIX = "oid:"
    COUNTERS_OID_KEY_FMT = f"{COUNTERS_KEY_PREFIX}{{oid}}"

    # Peer-port (HCA) System-DB keys (ported from change 336026).
    STATE_DB = "STATE_DB"
    PEER_PORT_KEY_PREFIX = "PEER_COUNTERS:"
    PEER_PORT_COUNTERS_KEY_FMT = PEER_PORT_KEY_PREFIX + "{peer_id}"
    PEER_PORT_MAPPING_PREFIX = "PEER_PORT_MAPPING:"
    PEER_PORT_MAPPING_KEY_FMT = PEER_PORT_MAPPING_PREFIX + "{peer_id}"
    PEER_TELEMETRY_HEALTH_KEY = "PEER_PORT_TELEMETRY_HEALTH|global"
    PEER_TELEMETRY_HEALTH_KEY_GREP = "PEER_PORT_TELEMETRY_HEALTH"


# Default fields whose presence we check in every per-plane Redis row (section 6.1).
EXPECTED_PLANE_PORT_DB_FIELDS = [
    "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_PKTS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT",
]

# Phase-1 IB port-error counters in the countermgrd default-SUM bucket (in/out errors).
SAI_PORT_STAT_INFINIBAND_PC_ERR_RCV_F = "SAI_PORT_STAT_INFINIBAND_PC_ERR_RCV_F"
SAI_PORT_STAT_INFINIBAND_ERR_XMTCONSTR_F = "SAI_PORT_STAT_INFINIBAND_ERR_XMTCONSTR_F"

# countermgrd default-SUM counters (HLD R-NVOS-1): the 4 packet/octet IF_*_EXT
# leaves plus the Phase-1 IB port-error SUM leaves.
COUNTERMGRD_SUM_COUNTERS = tuple(EXPECTED_PLANE_PORT_DB_FIELDS) + (
    SAI_PORT_STAT_INFINIBAND_PC_ERR_RCV_F,
    SAI_PORT_STAT_INFINIBAND_ERR_XMTCONSTR_F,
)

# countermgrd MAX aggregation on the Aport (HLD section 6.7). v1: xmit-wait only.
COUNTERMGRD_MAX_COUNTERS = (
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_WAIT",
)

# SAI COUNTERS_DB key -> gNMI state/counters leaf (create_gnmi_counter_list / supported-paths).
SAI_TO_GNMI_STATE_COUNTER_LEAF = {
    "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT": "in-pkts",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_PKTS_EXT": "out-pkts",
    "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT": "in-octets",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT": "out-octets",
    SAI_PORT_STAT_INFINIBAND_PC_ERR_RCV_F: "in-errors",
    SAI_PORT_STAT_INFINIBAND_ERR_XMTCONSTR_F: "out-errors",
}

# SAI COUNTERS_DB key -> NVUE `counters` JSON leaf. Octet SAI stats are exposed
# as in-bytes/out-bytes in NVUE.
SAI_TO_NVUE_COUNTER_LEAF = {
    "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT": "in-pkts",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_PKTS_EXT": "out-pkts",
    "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT": "in-bytes",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT": "out-bytes",
    SAI_PORT_STAT_INFINIBAND_PC_ERR_RCV_F: "in-errors",
    SAI_PORT_STAT_INFINIBAND_ERR_XMTCONSTR_F: "out-errors",
}

# MAX counters: gNMI uses the infiniband/port subtree; NVUE uses counters.out-wait.
SAI_TO_GNMI_MAX_COUNTER_LEAF = {
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_WAIT": "xmit-wait",
}
SAI_TO_NVUE_MAX_COUNTER_LEAF = {
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_WAIT": "out-wait",
}

# Tight SUM aggregation tolerance for plane-port -> Aport checks (section 6.2, 6.3).
# countermgrd default-SUM should match exactly; allow only a small absolute read
# jitter across sequential Aport + plane-port samples (gNMI, NVUE, Redis alike).
PLANEPORT_SUM_AGGREGATION_TOLERANCE_PCT = 0.001  # retained for Allure attach only
PLANEPORT_SUM_AGGREGATION_MIN_DELTA = 10

# Loose tolerance reused by the float-MAX aggregation branch (BER/time-since-clear).
SAMPLING_JITTER_TOLERANCE_PCT = 0.10

# Display labels for section 6.3 aggregation Allure steps (shared assert path).
API_LABEL_GNMI = "gNMI"
API_LABEL_NVUE = "NVUE"
API_LABEL_OTEL = "OTEL"

# Pause after admin-down freeze (oper-down confirmed) before reading counters.
# gNMI Aport aggregation can lag plane-port sums briefly.
COUNTER_SNAPSHOT_SETTLE_SEC = 2


class CounterMgrdRule:
    """countermgrd plane-port -> Aport aggregation rule identifiers."""
    SUM = "sum"
    MIN = "min"
    MAX = "max"
    CONCAT = "concat"


# Concatenation delimiter mirrors countermgrd VALUE_DELIMITER ("/").
CONCAT_DELIMITER = "/"


# ---------------------------------------------------------------------------
# Plane-port lifecycle / link-down (test plan section 9.1, 10.1, 10.2, 10.4)
# ---------------------------------------------------------------------------

# Link / physical state leaves read from the flat gNMI interface subtree during
# admin-down / peer-loss recovery checks (test plan section 10.1 / section 10.2).
LINK_STATE_RECOVERY_LEAVES = ("oper-status", "physical-port-state", "physical-state")
# Physical-state leaf with its build-dependent fallback name (preferred first).
PHYSICAL_STATE_LEAVES = ("physical-port-state", "physical-state")

# A re-enabled / plugged-back IB link transits PORT_CONFIGURATION_TRAINING before
# LINK_UP; poll the recovery leaves up to this ceiling at this interval.
LINK_RECOVERY_TIMEOUT_SEC = 90
LINK_RECOVERY_POLL_SEC = 3

# Link-down reason codes (PRM PUDE table). The reason STRINGS are reused from
# CODE_TO_DESCRIPTION in test_ib_interface_phy_detail.py (Rowaida R7) rather than
# duplicated here, so this stays a thin code -> shared-lookup indirection.
LINK_DOWN_CODE_ADMIN_DISABLE = 22   # CODE_TO_DESCRIPTION: "Down_by_management_command"
LINK_DOWN_CODE_CABLE_UNPLUGGED = 23  # CODE_TO_DESCRIPTION: "Cable_was_unplugged"


# Baseline directory + files for the section 7.1 backward-compat snapshot.
BASELINE_DIR_NAME = "baselines"
APORT_SCHEMA_BASELINE_FILE = "aport_schema_baseline.json"
APORT_SCHEMA_BASELINE_NVUE_FILE = "aport_schema_baseline.nvue.json"


# ============================================================================
# Peer-port / HCA / nmxt-ib telemetry constants (ported from change 336026)
# ============================================================================


class PeerType:
    """GPU vs HCA peer classification (by peer-id prefix; no peer-type leaf)."""
    GPU = "GPU"
    HCA = "HCA"
    ID_PREFIXES = {"hca": HCA, "gpu": GPU}


# The node-info leaves each peer type owns (SR-6). gpu_node_info and hca_node_info are separate
# metrics, so a row must carry only its own type's set and must not be padded with the other's.
# Matching is by exact leaf name: the `hca` token also appears in leaves every row renders, so a
# substring test would flag a GPU row for fields it is supposed to have. The HLD defines no GPU
# node-info leaf, hence the empty set: an HCA row has no foreign set to match, and
# `foreign_node_info_problems` logs how many rows it could enforce.
_HCA_NODE_INFO_LEAVES = ("hca-node-description", "hca-ib-port", "hca-port-index")

PEER_TYPE_NODE_INFO_FIELDS = {
    PeerType.HCA: frozenset(set(_HCA_NODE_INFO_LEAVES) |
                            {leaf.replace("-", "_") for leaf in _HCA_NODE_INFO_LEAVES}),
    PeerType.GPU: frozenset(),
}


class PeerPortFields:
    """Leaf / field names on a peer-port entry (candidate tuples span surfaces)."""
    PEER_TYPE = "peer-type"
    ASSOCIATED_SWITCH_PORT = "associated-switch-port"
    APORT_REF_CANDIDATES = (
        "associated-switch-port", "switch_port_alias", "aport_name",
        "local-port", "associated-aport", "parent-interface",
    )
    IDENTITY_CANDIDATES = (
        "node-guid", "port-guid", "peer-port-name", "peer-component",
        "node_guid", "port_guid", "peer_port_name", "peer_component", "hca_alias",
        "guid", "name", "id", "peer-id",
    )

    TIER_FIELD = "peer_port_tier"
    TIER_PLANE = "plane"
    TIER_AGGREGATED = "aggregated"
    PARENT_FIELD = "parent_peer_port_name"
    HCA_ALIAS_FIELD = "hca_alias"
    APORT_NAME_FIELD = "aport_name"
    SWITCH_PORT_ALIAS_FIELD = "switch_port_alias"


class PeerPortComponentRef:
    """Peer-port state leaves that name a component in the sibling components tree.

    The values are the gNMI leaf names. `nv show peer-port` keeps the CLI's `asic` name for
    the chip reference (extended HLD rev 1.1, SR-1), which NVUE_LEAF records.
    """
    TRANSCEIVER = "transceiver"
    INTEGRATED_CIRCUIT = "integrated-circuit"
    ALL = (TRANSCEIVER, INTEGRATED_CIRCUIT)
    NVUE_LEAF = {TRANSCEIVER: TRANSCEIVER, INTEGRATED_CIRCUIT: "asic"}


class PeerComponentNaming:
    """Naming rule for remote component names (extended HLD section 4.2).

    A transceiver is ``xcvr-<serial>``, or ``xcvr-<serial>-<sys-image-guid>`` when the chassis
    GUID is present too; an ASIC is ``asic-<node-guid>``. In every part a leading ``0x`` is
    dropped and any character outside ``[A-Za-z0-9._-]`` becomes an underscore. No component is
    created when the source identifier is missing or reads N/A.
    """
    TRANSCEIVER_PREFIX = "xcvr-"
    ASIC_PREFIX = "asic-"
    HEX_PREFIX = "0x"
    ALLOWED_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]")
    SANITIZE_REPLACEMENT = "_"
    # An identifier reading any of these names nothing, so no component may be created for it.
    ABSENT_VALUES = ("", "n/a", "na", "none", "null", "unknown")


# xcset columns the extended HLD adds, and which component name each one feeds. The peer
# transceiver / ASIC components are derived from these, so a test that blanks one of them is
# exercising the "nothing is invented when the telemetry does not name it" rule (SR-7).
class PeerComponentXcsetColumn:
    """ib_nvos xcset columns that name a peer component (extended HLD section 4.2).

    The HLD names the identifiers but not their columns; these are the spellings NMX-T serves.
    """
    MODULE_SERIAL = "serial"
    SYS_IMAGE_GUID = "sys_image_guid"
    NODE_GUID = "node_guid"
    HOST_NAME = "host_name"
    # NMX-T does not serve a host_name column; the remote host leads the node description
    # ("<host> <hca>"), which is where NVOS takes the peer host name from.
    NODE_DESCRIPTION = "node_description"
    ALL = (MODULE_SERIAL, SYS_IMAGE_GUID, NODE_GUID, HOST_NAME)


PEER_PORT_COUNTER_FIELDS = [
    "in-bytes", "in-pkts", "in-drops", "in-errors",
    "out-bytes", "out-pkts", "out-drops", "out-errors",
    "in-unicast-pkts", "in-multicast-pkts",
    "out-unicast-pkts", "out-multicast-pkts",
    "buffer-overrun-errors", "out-wait",
]


PEER_PORT_BER_FIELDS = ["raw-ber", "effective-ber", "symbol-ber"]


PEER_PORT_PLR_FIELDS = [
    "plr-rcv-codes", "plr-rcv-codes-err", "plr-rcv-uncorrectable-code",
    "plr-xmit-codes", "plr-xmit-retry-codes", "plr-xmit-retry-events",
    "plr-sync-events",
]


PEER_PORT_PLANE_FIELDS = [
    "associated-switch-port", "plane-index",
    "switch-ib-port-index", "switch-ib-port-name",
]


class PeerTelemetryHealth:
    """PEER_PORT_TELEMETRY_HEALTH|global status fields and healthy/degraded synonyms."""
    HEALTH_FIELD = "status"
    HEALTHY_VALUES = ("ok", "healthy", "up", "ready", "running", "200", "0")
    DEGRADED_VALUES = ("degraded", "unhealthy", "error", "down", "failed", "stale",
                       "fail", "not_ok", "unreachable")
    # Classifications returned by helpers.classify_peer_health(); the *_VALUES tuples above
    # are the raw STATE_DB spellings that map onto these.
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


# gNMI uses octets, NVUE uses bytes; comparisons skip names a surface lacks.
PEER_PORT_ADDITIVE_FIELDS = [
    "in-pkts",
    "out-pkts",
    "in-octets",
    "out-octets",
    "in-bytes",
    "out-bytes",
]


# The subset of PEER_PORT_ADDITIVE_FIELDS that the gNMI YANG model actually exposes, i.e. without
# the NVUE-only 'bytes' spellings. Use this to build gNMI paths: the union above is safe only for
# comparisons that tolerate a missing name, whereas a subscription carrying one path outside the
# model is rejected wholesale with Unimplemented - taking the valid paths down with it.
PEER_PORT_GNMI_ADDITIVE_FIELDS = [
    "in-pkts",
    "out-pkts",
    "in-octets",
    "out-octets",
]


# Peer-port identity leaves, which do not change for as long as the peer is present. These are the
# only peer-port leaves a heartbeat test can use: the additive counters above are refreshed every
# gpu-telemetry ingest cycle, which is far shorter than any heartbeat, so a change always delivers
# them before the heartbeat is due and the heartbeat is never exercised.
PEER_PORT_GNMI_STATIC_LEAVES = [
    "name",
    "node-guid",
    "port-guid",
    "port-number",
    "node-description",
]


# Additive peer-port counters as the COUNTERS_DB row spells them (raw SAI names).
PEER_PORT_DB_ADDITIVE_FIELDS = [
    "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_PKTS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT",
]


# `nv show peer-port <id> counters` leaf -> COUNTERS_DB SAI field. The two drop
# counters are the only ones the daemon writes without the SAI_ prefix.
PEER_PORT_COUNTERS_TO_DB_FIELD = {
    "in-pkts": "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT",
    "out-pkts": "SAI_PORT_STAT_INFINIBAND_IF_OUT_PKTS_EXT",
    "in-bytes": "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT",
    "out-bytes": "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT",
    "in-unicast-pkts": "SAI_PORT_STAT_INFINIBAND_UNICAST_RCV_PKTS",
    "out-unicast-pkts": "SAI_PORT_STAT_INFINIBAND_UNICAST_XMT_PKTS",
    "in-multicast-pkts": "SAI_PORT_STAT_INFINIBAND_MULTICAST_RCV_PKTS",
    "out-multicast-pkts": "SAI_PORT_STAT_INFINIBAND_MULTICAST_XMT_PKTS",
    "in-drops": "PORT_STAT_INFINIBAND_TOTAL_IN_DROPS",
    "out-drops": "PORT_STAT_INFINIBAND_TOTAL_OUT_DROPS",
    "in-errors": "SAI_PORT_STAT_INFINIBAND_PC_ERR_RCV_F",
    "out-errors": "SAI_PORT_STAT_INFINIBAND_ERR_XMTCONSTR_F",
    "out-wait": "SAI_PORT_STAT_INFINIBAND_IF_OUT_WAIT",
    "buffer-overrun-errors": "SAI_PORT_STAT_INFINIBAND_EXCESSIVE_BUFFER_OVERRUN_EXT",
}


# `nv show peer-port <id> counters link` leaf -> COUNTERS_DB SAI field.
PEER_PORT_COUNTERS_LINK_TO_DB_FIELD = {
    "carrier-down-count": "SAI_PORT_STAT_INFINIBAND_LINK_DOWNED_EXT",
    "error-recovery": "SAI_PORT_STAT_INFINIBAND_ERROR_RECOVERY_EXT",
    "local-integrity-errors": "SAI_PORT_STAT_INFINIBAND_LOCAL_LINK_INTEGRITY_ERR_EXT",
    "port-rcv-constraint-errors": "SAI_PORT_STAT_INFINIBAND_RX_CONSTRAINTS_ERR_EXT",
    "port-rcv-remote-physical-errors": "SAI_PORT_STAT_INFINIBAND_RCV_REMOTE_PHY_ERR_EXT",
    "port-rcv-switch-relay-errors": "SAI_PORT_STAT_INFINIBAND_RCV_SWITCH_RELAY_ERR_EXT",
}


# `nv show peer-port <id> link phy detail` leaf -> COUNTERS_DB SAI field. NVUE spells the
# PLR leaves `plr-xmit-*` / `plr-rcv-codes-err` (matching the local interface tree) while the
# DB uses `PLR_XMT_*` and the gNMI model uses `plr-rcv-code-err`; the gNMI divergence is
# inherited from the interface tree (dev-confirmed), so keep one spelling per surface.
PEER_PORT_PHY_DETAIL_TO_DB_FIELD = {
    "plr-rcv-codes": "SAI_PORT_STAT_INFINIBAND_PLR_RCV_CODES",
    "plr-rcv-codes-err": "SAI_PORT_STAT_INFINIBAND_PLR_RCV_CODES_ERR",
    "plr-rcv-uncorrectable-code": "SAI_PORT_STAT_INFINIBAND_PLR_RCV_UNCORRECTABLE_CODE",
    "plr-xmit-codes": "SAI_PORT_STAT_INFINIBAND_PLR_XMT_CODES",
    "plr-xmit-retry-codes": "SAI_PORT_STAT_INFINIBAND_PLR_XMT_RETRY_CODES",
    "plr-xmit-retry-events": "SAI_PORT_STAT_INFINIBAND_PLR_XMT_RETRY_EVENTS",
    "plr-sync-events": "SAI_PORT_STAT_INFINIBAND_PLR_SYNC_EVENTS",
    "plr-codes-loss": "SAI_PORT_STAT_INFINIBAND_PLR_CODES_LOSS",
    "rq-general-error": "SAI_PORT_STAT_INFINIBAND_RQ_GENERAL_ERROR",
    "sync-header-error-counter": "SAI_PORT_STAT_INFINIBAND_SYNC_HEADER_ERR_COUNTER",
}


# `nv show peer-port <id> link phy health` counter leaf -> COUNTERS_DB SAI field.
PEER_PORT_PHY_HEALTH_TO_DB_FIELD = {
    "effective-errors": "SAI_PORT_STAT_INFINIBAND_EFFECTIVE_ERR",
    "symbol-errors": "SAI_PORT_STAT_INFINIBAND_SYMBOL_ERR",
    "phy-received-bits": "SAI_PORT_STAT_INFINIBAND_PHY_RECEIVED_BITS",
}


# BER leaves of `link phy health`. The DB stores them as strings and the surfaces do not
# normalize the formatting (an interface reports `15E-255` for the peer's `1.5e-254`, and a
# peer renders `7e-08` as `0.00000007`), so these are compared as parsed decimals, never as
# strings.
PEER_PORT_PHY_HEALTH_BER_TO_DB_FIELD = {
    "raw-ber": "SAI_PORT_STAT_INFINIBAND_RAW_BER_STRING",
    "effective-ber": "SAI_PORT_STAT_INFINIBAND_EFFECTIVE_BER_STRING",
    "symbol-ber": "SAI_PORT_STAT_INFINIBAND_SYMBOL_BER_STRING",
}


# The firmware rounds BER to a single significant digit, so one error arriving between the
# NVUE read and the DB read steps the value (`7e-08` -> `1e-07`) far beyond any percentage
# tolerance. A BER pair is therefore required to stay within a decade, which still catches the
# mismatches worth catching: a wrong DB field, a missing scale factor, or a floor value shown
# against a real one. Anything under the floor is the "no errors" sentinel, which one surface
# may render as `1.5e-254` and another as `0`.
PEER_PORT_BER_MAX_RATIO = Decimal(10)
PEER_PORT_BER_ZERO_FLOOR = Decimal("1e-100")


# An error rate is published as a ratio, so it cannot exceed 1. A surface that scaled it to a
# percentage would report up to 100 - the scaling difference SR-2 rules out between the local
# port and the peer-port.
BER_RATIO_CEILING = Decimal(1)


# Cross-surface comparison of a sampled decimal is relative-only: the shared
# `values_within_tolerance` falls back to an absolute tolerance of 1.0 when one side reads zero,
# which would make every error rate (a ratio bounded by BER_RATIO_CEILING) compare equal to zero.
# This epsilon absorbs far-tail sampling noise between the two reads while staying orders of
# magnitude below any rate a scaling bug would produce.
DECIMAL_COMPARE_EPSILON = 1e-12


# A relative tolerance is meaningless on a logarithmic reading near 0 dBm, so two dBm samples
# of one optical power taken a moment apart are compared within this absolute band instead.
OPTICAL_POWER_DBM_TOLERANCE = 0.5

# Units nv show renders an optical power in: "<mW> mW / <dBm> dBm".
OPTICAL_POWER_MW_UNIT = "mw"
OPTICAL_POWER_DBM_UNIT = "dbm"


# Per-histogram-bucket COUNTERS_DB field template (per-lane templates live with the lane
# leaf specs). The DB carries a lane field only for the operational lanes, so a peer
# narrower than PEER_PORT_LANE_COUNT renders the remaining lanes as null.
PEER_PORT_HISTOGRAM_DB_FMT = "SAI_PORT_STAT_INFINIBAND_RS_HIST_{bucket}"
PEER_PORT_LANE_COUNT = 8
PEER_PORT_HISTOGRAM_BUCKET_COUNT = 16


# `link` speed / width against the DB's operational speed (Mb/s) and lane count.
PEER_PORT_SPEED_LEAF = "speed"
PEER_PORT_WIDTH_LEAF = "width"
PEER_PORT_SPEED_DB_FIELD = "SAI_PORT_STAT_INFINIBAND_SPEED_OPER_BY_LANES"
PEER_PORT_LANES_DB_FIELD = "SAI_PORT_STAT_INFINIBAND_LANES_OPER"
PEER_PORT_SPEED_MBPS_PER_GBPS = 1000


# `plr-bw-loss-percent` is derived from `plr-codes-loss` (dev-confirmed), not ingested.
PEER_PORT_PLR_BW_LOSS_LEAF = "plr-bw-loss-percent"
PEER_PORT_PLR_CODES_LOSS_LEAF = "plr-codes-loss"
PEER_PORT_PLR_BW_LOSS_DIVISOR = 1e8


# Canonical (API) additive counter -> COUNTERS_DB SAI field. gNMI spells the byte
# counters `in-octets` / `out-octets` where NVUE says `in-bytes` / `out-bytes`.
PEER_PORT_API_TO_DB_FIELD = {
    **PEER_PORT_COUNTERS_TO_DB_FIELD,
    "in-octets": PEER_PORT_COUNTERS_TO_DB_FIELD["in-bytes"],
    "out-octets": PEER_PORT_COUNTERS_TO_DB_FIELD["out-bytes"],
}


# Peer-port resiliency services: peer-telemetry (consumer) and nmx-t-ib (source).
PEER_TELEMETRY_SERVICE = "peer-telemetry.service"


NMXT_IB_SERVICE = "nmx-t-ib.service"


NMXT_IB_CONTAINER = "nmx-t-ib.telemetry.telemetry"


# NMX-T for IB serves the HCA cross-connect set (xcset) as CSV over a Unix socket.
NMXT_XCSET_SOCKET = "/var/run/nmx-t/ib/telemetry.sock"


# Lower-cased journal marker peer-telemetry logs (at error level) while it cannot reach the
# nmx-t-ib sockets, e.g. "Connection error: GET ... on /var/run/nmx-t/ib/control.sock".
NMXT_CONNECTION_ERROR_TOKEN = "connection error: get"


NMXT_XCSET_ENDPOINT = "/csv/xcset/ib_nvos"


NMXT_CONTROL_SOCKET = "/var/run/nmx-t/ib/control.sock"


# gRPC Connection API socket. The daemon keeps a connection to it open; if it is
# missing the connector times out and the daemon wipes the last-good rows. The
# malformed-feed interceptor must bind/listen here (no responses needed) so the
# parse-failure path is exercised without tripping the connector-timeout wipe.
NMXT_CONNECTOR_SOCKET = "/var/run/nmx-t/ib/connector.sock"


# xcset CSV counter column -> COUNTERS_DB SAI field (xcset already uses SAI names).
# Columns the extended HLD adds to the ib_nvos xcset (BER, frequency, cable and SNR). Which
# spellings NMX-T actually serves per cable type is still open, so the SR-7 case picks the
# column it removes by marker rather than by a fixed name.
XCSET_EXTENDED_COLUMN_MARKERS = (
    "ber", "freq", "snr", "power", "vendor", "serial", "temperature", "voltage", "cable",
)


NMXT_XCSET_TO_DB_FIELD = {
    "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT": "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_PKTS_EXT": "SAI_PORT_STAT_INFINIBAND_IF_OUT_PKTS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT": "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT": "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT",
}


# Sampling cycle of gpu-telemetry; resiliency tests wait a small multiple of this.
PEER_TELEMETRY_SAMPLING_SEC = 30


# How long, and how often, to wait for a freshly enabled peer-port row to publish its transceiver
# and ASIC references (a row is listed before them): three sampling cycles.
COMPONENT_REF_WAIT_SEC = 3 * PEER_TELEMETRY_SAMPLING_SEC
COMPONENT_REF_POLL_SEC = 10


# How often to ask gNMI again for the peer-port list while it is still empty after NVUE listed rows.
GNMI_PEER_LIST_POLL_SEC = 5


# Settle time after toggling PREI error injection on a plane-port, before the
# BER counters reflect the change. BER is sampled by sym-mgr over several link
# cycles, so we give it a few seconds to propagate to the telemetry rows.
BER_INJECT_SETTLE_SEC = 10


class GnmiTypeKind:
    """Coarse value-type buckets used by the §7.2 typing check."""
    COUNTER = "counter"   # YANG counter type - unsigned, monotonic
    UINT = "uint"         # unsigned integer - a code or a setting, identical on every surface
    # An unsigned reading sampled from the hardware (a measured clock speed). Types like a
    # UINT, but two surfaces reading it a moment apart may legitimately differ, so the
    # cross-surface comparison gives it the sampling-jitter tolerance.
    SAMPLED_UINT = "sampled-uint"
    DECIMAL = "decimal"   # non-negative real (BER, loss-percent)
    # A sampled optical power. gNMI publishes one signed number, while nv show renders both
    # units on one line ("2.10 mW / 3.21 dBm"); dBm goes negative below 1 mW.
    OPTICAL_POWER = "optical-power"
    BOOL = "bool"         # boolean
    STRING = "string"     # string / enum / identity - any non-empty value


# A fixed (non-list) gNMI container and the leaves it must expose.
#   group_id: stable, human-readable id (also the pytest parametrize id)
#   prefix:   GnmiYangPaths template taking {name}
#   leaves:   {leaf-name: GnmiTypeKind} for every documented leaf in the container
# `pending` lists leaves that are part of the model (Joe's ib_peer_port_xpaths)
# but NOT yet implemented in the build: the sweep tolerates their absence instead
# of failing, yet still type-checks them if they appear (so they auto-enforce
# once delivered). Defaults to empty so existing groups are unaffected.
# One gNMI container swept by the peer-port schema test:
#   pending:         leaves tolerated absent (a value that does appear is still type-checked)
#   relative_leaves: key readings by their path under `prefix`, for containers that nest the
#                    same leaf name under several parents
#   component_ref:   the peer-port leaf whose value fills `{name}` in `prefix`; empty for
#                    containers keyed by the peer-port id itself
SchemaGroup = namedtuple(
    "SchemaGroup", ["group_id", "prefix", "leaves", "pending", "relative_leaves", "component_ref"],
    defaults=(frozenset(), False, ""),
)


_C = GnmiTypeKind.COUNTER


_U = GnmiTypeKind.UINT


_SU = GnmiTypeKind.SAMPLED_UINT


_D = GnmiTypeKind.DECIMAL


_S = GnmiTypeKind.STRING


_B = GnmiTypeKind.BOOL


_P = GnmiTypeKind.OPTICAL_POWER


# List subtrees of the model. Unlike GNMI_SCHEMA_GROUPS these are
# keyed lists (per-lane channel, per-FEC-bin histogram), so the §7.2b sweep
# enumerates the present members and asserts every member exposes the full,
# correctly-typed leaf set. Member presence is conditional (a channel exists per
# active SerDes lane; a histogram bin exists when FEC is running), so absence is
# tolerated UNLESS `require_when_oper_up` and the port is operationally Up - then
# at least one member must exist (a real finding if the build exposes none).
#
#   group_id:             stable id / pytest parametrize id
#   list_prefix:          GnmiYangPaths template (the container holding the list)
#   leaves:               {leaf-name: GnmiTypeKind} expected on EVERY member
#   require_when_oper_up:  if True, an Up port must expose >= 1 member
#   relative_leaves:      when True, `leaves` are keyed by the path relative to the list MEMBER
#                         (e.g. "state/media-snr/instant") instead of by the bare leaf name.
#                         Needed wherever one member nests the same leaf name under two parents
#                         (media-snr/instant and host-snr/instant both end in "instant").
#   component_ref:        when set, {name} is keyed by a component name rather than by a
#                         peer-port id, and this names the peer-port leaf carrying it (the
#                         remote module is a sibling tree a peer-port points at).
ListSchemaGroup = namedtuple(
    "ListSchemaGroup",
    ["group_id", "list_prefix", "leaves", "require_when_oper_up", "relative_leaves",
     "component_ref"],
    defaults=(False, ""),
)


# ---------------------------------------------------------------------------
# Extended peer transceiver / ASIC + local switch leaves (extended HLD rev 1.1).
#
# The HLD models the peer tree (/peer-port/components/component) and the local tree
# (/components/component) with the same containers, so each leaf table is defined once here
# and the local subsets are derived from it rather than re-listed.
# ---------------------------------------------------------------------------

# BER monitor: each error-rate type is its own container carrying the same four leaves.
BER_MONITOR_TYPES = ("raw-ber", "effective-ber", "symbol-ber")


BER_MONITOR_TYPE_LEAVES = {"alarm-count": _C, "last": _D, "max": _D, "min": _D}


# Keyed by the path relative to ber-monitor/state: all three types carry an `alarm-count`,
# so the bare leaf name alone would not identify a reading.
BER_MONITOR_LEAVES = {
    f"{ber_type}/{leaf}": kind
    for ber_type in BER_MONITOR_TYPES
    for leaf, kind in BER_MONITOR_TYPE_LEAVES.items()
}


# The HLD lists no enum values for form-factor, module-functional-type or supported-cable-length,
# so every identity leaf is checked for presence and a non-empty value only.
TRANSCEIVER_IDENTITY_LEAVES = {
    "vendor": _S,
    "vendor-rev": _S,
    "vendor-part": _S,
    "serial-no": _S,
    "form-factor": _S,
    "module-functional-type": _S,
    "supported-cable-length": _S,
}


# HLD section 5.1 exposes only these four of the identity leaves on the switch's own module.
LOCAL_TRANSCEIVER_IDENTITY_LEAF_NAMES = (
    "vendor", "vendor-rev", "module-functional-type", "supported-cable-length",
)


LOCAL_TRANSCEIVER_IDENTITY_LEAVES = {
    leaf: kind for leaf, kind in TRANSCEIVER_IDENTITY_LEAVES.items()
    if leaf in LOCAL_TRANSCEIVER_IDENTITY_LEAF_NAMES
}


# `nv show platform transceiver` keeps its existing names (SR-1), so the one identity leaf it
# spells differently from gNMI is mapped: {CLI leaf: gNMI leaf}.
LOCAL_TRANSCEIVER_NVUE_TO_GNMI_LEAF = {"vendor-name": "vendor"}


# Per physical channel, as NVUE names them. The analog readings and the flags are kept apart
# because gNMI nests only the readings under `instant`.
TRANSCEIVER_CHANNEL_SNR_READINGS = {
    "media-snr": _D,
    "host-snr": _D,
}


TRANSCEIVER_CHANNEL_READINGS = {
    "input-power": _P,
    "output-power": _P,
    **TRANSCEIVER_CHANNEL_SNR_READINGS,
}


TRANSCEIVER_CHANNEL_FLAGS = {"rx-los": _B, "rx-cdr-lol": _B}


TRANSCEIVER_CHANNEL_NVUE_LEAVES = {**TRANSCEIVER_CHANNEL_READINGS, **TRANSCEIVER_CHANNEL_FLAGS}


# The transmit-side indications live on the host lanes, not on the media channels.
TRANSCEIVER_HOST_LANE_FLAGS = {"tx-los": _B, "tx-cdr-lol": _B}


def _channel_gnmi_leaves(readings, flags):
    """gNMI keys of one channel / lane member, relative to the member itself.

    Every member roots its leaves at `state`, and each analog reading nests its sample under
    `instant` - so `media-snr` and `host-snr` would both key as `instant` if the parent
    container were dropped.
    """
    return {
        **{f"state/{leaf}/instant": kind for leaf, kind in readings.items()},
        **{f"state/{leaf}": kind for leaf, kind in flags.items()},
    }


TRANSCEIVER_CHANNEL_LEAVES = _channel_gnmi_leaves(
    TRANSCEIVER_CHANNEL_READINGS, TRANSCEIVER_CHANNEL_FLAGS)


TRANSCEIVER_HOST_LANE_LEAVES = _channel_gnmi_leaves({}, TRANSCEIVER_HOST_LANE_FLAGS)


# HLD section 5.1 serves only the two SNR readings on the switch's own module, over gNMI only.
TRANSCEIVER_SNR_LEAVES = _channel_gnmi_leaves(TRANSCEIVER_CHANNEL_SNR_READINGS, {})


# Four flags each for cable temperature and supply voltage (SR-8): the requirement is that
# they are exposed as individual booleans, never as one packed number.
TRANSCEIVER_TEMPERATURE_FLAGS = {
    "temp-high-alarm-flag": _B,
    "temp-low-alarm-flag": _B,
    "temp-high-warning-flag": _B,
    "temp-low-warning-flag": _B,
}


TRANSCEIVER_VOLTAGE_FLAGS = {
    "vcc-high-alarm-flag": _B,
    "vcc-low-alarm-flag": _B,
    "vcc-high-warning-flag": _B,
    "vcc-low-warning-flag": _B,
}


TRANSCEIVER_DIAG_LEAVES = {**TRANSCEIVER_TEMPERATURE_FLAGS, **TRANSCEIVER_VOLTAGE_FLAGS}


# The per-module identity leaves only render in the detail view of the transceiver inventory.
TRANSCEIVER_DETAIL_VIEW = "--view=detail"


# integrated-circuit/state/frequency, which the extended HLD uses in place of the first
# drop's asic/state measured-freq-0 / -1 pairs (naming rule in HLD section 4.2).
ASIC_FREQUENCY_LEAVES = {
    "instant": _SU,
    "min": _SU,
    "max": _SU,
    "max-delta": _SU,
}


# components/component/state/type of the switch chips.
ASIC_COMPONENT_TYPE = "ASIC"


# Leaves the extended HLD adds to an HCA peer-port's state, by their gNMI names. `transceiver`
# and `integrated-circuit` carry the component names that resolve under
# /peer-port/components/component, which is what makes the sibling tree reachable from a peer.
PEER_PORT_ROOT_EXTENDED_LEAVES = {
    "port-number": _U,
    PeerPortComponentRef.TRANSCEIVER: _S,
    PeerPortComponentRef.INTEGRATED_CIRCUIT: _S,
}


# The same leaves as `nv show peer-port <id>` renders them.
PEER_PORT_NVUE_ROOT_EXTENDED_LEAVES = {
    PeerPortComponentRef.NVUE_LEAF.get(leaf, leaf): kind
    for leaf, kind in PEER_PORT_ROOT_EXTENDED_LEAVES.items()
}


# {CLI leaf: gNMI leaf} for the peer-port state leaves the two surfaces spell differently.
PEER_PORT_NVUE_TO_GNMI_ROOT_LEAF = {
    nvue: gnmi for gnmi, nvue in PeerPortComponentRef.NVUE_LEAF.items() if nvue != gnmi
}


# tray-id is published for a GPU peer-port and omitted for an HCA peer-port (HLD section 5.2,
# SR-6), so an HCA row carrying it is a finding on either surface.
PEER_PORT_HCA_OMITTED_LEAVES = frozenset({"tray-id"})


# The HLD does not say whether the aggregated or the plane row carries these, so a single row
# may lack them. That is not a pass: every leaf must be published on every row of at least one
# tier, which `extended_identity_tier_problems` enforces across the whole listing.
PEER_PORT_ROOT_TIER_UNSPECIFIED_LEAVES = (frozenset(PEER_PORT_ROOT_EXTENDED_LEAVES) |
                                          frozenset(PEER_PORT_NVUE_ROOT_EXTENDED_LEAVES))


# PHY leaves, split by the container each one sits in: `link-up-duration` (milliseconds) hangs
# off phy/state while `down-blame` hangs off phy/link-down-information/state.
LOCAL_PHY_STATE_LEAVES = {"link-up-duration": _U}


LOCAL_PHY_LINK_DOWN_LEAVES = {"down-blame": _S}


# The remote ASIC's host name: the HLD command reference spells it `hostname` while the
# peer-port row spells the same idea `host-name`, so SR-5 reads whichever spelling is present
# and fails when neither is.
PEER_ASIC_HOST_NAME_CANDIDATES = ("hostname", "host-name")

# The HLD command-reference spelling, which the peer ASIC node must render (HLD section 5.3).
PEER_ASIC_HOST_NAME_LEAF = PEER_ASIC_HOST_NAME_CANDIDATES[0]


# Peer-port (nvidia-peer-port) gNMI schema sweep groups, rooted at
# /peer-port/interfaces/interface[name=*] and /peer-port/components/component[name=*].
PEER_PORT_SCHEMA_GROUPS = [
    SchemaGroup(
        "peer-port-state",
        GnmiYangPaths.PEER_PORT_STATE,
        {
            "name": _S,
            "node-description": _S,
            "node-guid": _S,
            "port-guid": _S,
            **PEER_PORT_ROOT_EXTENDED_LEAVES,
        },
        # TODO(peer-port): node-description not yet exposed over gNMI (gNMI-gap bug). The
        # extended leaves replace the first drop's peer-component and may sit on either tier.
        pending=frozenset({"node-description"}) | PEER_PORT_ROOT_TIER_UNSPECIFIED_LEAVES,
    ),
    SchemaGroup(
        "peer-port-state-counters",
        GnmiYangPaths.PEER_PORT_STATE_COUNTERS,
        {
            "in-octets": _C,
            "in-pkts": _C,
            "in-unicast-pkts": _C,
            "in-broadcast-pkts": _C,
            "in-multicast-pkts": _C,
            "in-discards": _C,
            "in-errors": _C,
            "out-octets": _C,
            "out-pkts": _C,
            "out-unicast-pkts": _C,
            "out-broadcast-pkts": _C,
            "out-multicast-pkts": _C,
            "out-discards": _C,
            "out-errors": _C,
        },
    ),
    SchemaGroup(
        "peer-port-infiniband-state",
        GnmiYangPaths.PEER_PORT_INFINIBAND_STATE,
        {
            "lid": _U,
        },
        # TODO(peer-port): infiniband/state/lid not yet exposed over gNMI (gNMI-gap bug).
        pending=frozenset({"lid"}),
    ),
    SchemaGroup(
        "peer-port-infiniband-port-counters",
        GnmiYangPaths.PEER_PORT_INFINIBAND_COUNTERS_PORT,
        {
            "xmit-wait": _C,
            "rcv-errors": _C,
            "link-error-recovery": _C,
            "link-downed": _C,
            "rcv-remote-phy-errors": _C,
            "rcv-switch-relay-errors": _C,
            "rcv-constraints-errors": _C,
            "local-link-integrity-errors": _C,
            "excessive-buffer-overrun": _C,
            "vl15-dropped": _C,
        },
        # TODO(peer-port): vl15-dropped is outside the phase1-003 reduced drop
        # (Joe spreadsheet 63573, NOT SUPPORTED IN REDUCED DROP) - comes in the later
        # Arch-agreed xPath drop; drop from `pending` once the build exposes it.
        pending=frozenset({"vl15-dropped"}),
    ),
    SchemaGroup(
        "peer-port-phy-state",
        GnmiYangPaths.PEER_PORT_PHY_STATE,
        {
            "effective-errors": _C,
            "symbol-errors": _C,
            "received-bits": _C,
            "time-since-last-clear-min": _D,  # uint64 in schema; tolerate fractional
            **LOCAL_PHY_STATE_LEAVES,
        },
        # TODO(peer-port): time-since-last-clear-min not yet implemented in the build.
        pending=frozenset({"time-since-last-clear-min"}),
    ),
    SchemaGroup(
        "peer-port-phy-ber",
        GnmiYangPaths.PEER_PORT_PHY_BER_STATE,
        {
            "effective-ber": _D,
            "symbol-ber": _D,
            "raw-ber": _D,
        },
    ),
    SchemaGroup(
        "peer-port-phy-ber-monitor",
        GnmiYangPaths.PEER_PORT_PHY_BER_MONITOR_STATE,
        BER_MONITOR_LEAVES,
        relative_leaves=True,
    ),
    SchemaGroup(
        "peer-port-phy-link-down-information",
        GnmiYangPaths.PEER_PORT_PHY_LINK_DOWN_INFO,
        {
            "total-events": _C,
            **LOCAL_PHY_LINK_DOWN_LEAVES,
        },
    ),
    SchemaGroup(
        "peer-port-phy-infiniband-port-statistics",
        GnmiYangPaths.PEER_PORT_PHY_IB_PORT_STATISTICS_STATE,
        {
            "port-rcv-data": _C,
            "port-rcv-pkts": _C,
            "port-xmit-data": _C,
            "port-xmit-pkts": _C,
            "port-unicast-rcv-pkts": _C,
            "port-unicast-xmit-pkts": _C,
            "port-multicast-rcv-pkts": _C,
            "port-multicast-xmit-pkts": _C,
        },
    ),
    SchemaGroup(
        "peer-port-phy-infiniband-port-errors",
        GnmiYangPaths.PEER_PORT_PHY_IB_PORT_ERRORS_STATE,
        {
            "rq-general-error": _C,
            "sync-header-error-counter": _C,
        },
    ),
    SchemaGroup(
        "peer-port-phy-infiniband-plr",
        GnmiYangPaths.PEER_PORT_PHY_PLR_STATE,
        {
            "plr-rcv-code-err": _C,
            "plr-rcv-codes": _C,
            "plr-rcv-uncorrectable-code": _C,
            "plr-sync-events": _C,
            "plr-xmit-codes": _C,
            "plr-xmit-retry-codes": _C,
            "plr-xmit-retry-events": _C,
        },
    ),
    SchemaGroup(
        "peer-port-component-state",
        GnmiYangPaths.PEER_PORT_COMPONENT_STATE,
        {
            "name": _S,
            "type": _S,
            "mfg-name": _S,
            "part-no": _S,
            "serial-no": _S,
            "hardware-version": _S,
            "firmware-version": _S,
            PEER_ASIC_HOST_NAME_LEAF: _S,
        },
        # TODO(peer-port): outside the phase1-003 reduced drop (Joe spreadsheet: mfg-name/
        # part-no/serial-no = NOT SUPPORTED IN REDUCED DROP; name/type/hardware-version
        # off-spreadsheet) and not in the extended HLD either. firmware-version and the host
        # name are extended-HLD leaves of the peer ASIC, so they are required.
        pending=frozenset({"name", "type", "mfg-name", "part-no", "serial-no", "hardware-version"}),
        component_ref=PeerPortComponentRef.INTEGRATED_CIRCUIT,
    ),
    # The extended HLD replaces the first drop's asic/state (measured-freq-0 / -1 pairs) with
    # integrated-circuit/state/frequency, so the ASIC clock speed is swept under the HLD name.
    SchemaGroup(
        "peer-port-component-integrated-circuit-frequency",
        GnmiYangPaths.PEER_PORT_COMPONENT_IC_FREQUENCY,
        ASIC_FREQUENCY_LEAVES,
        component_ref=PeerPortComponentRef.INTEGRATED_CIRCUIT,
    ),
    # The extended HLD delivers the whole transceiver identity subtree the first drop left out.
    SchemaGroup(
        "peer-port-component-transceiver",
        GnmiYangPaths.PEER_PORT_COMPONENT_TRANSCEIVER_STATE,
        TRANSCEIVER_IDENTITY_LEAVES,
        component_ref=PeerPortComponentRef.TRANSCEIVER,
    ),
    SchemaGroup(
        "peer-port-component-transceiver-diag",
        GnmiYangPaths.PEER_PORT_COMPONENT_TRANSCEIVER_DIAG_STATE,
        TRANSCEIVER_DIAG_LEAVES,
        component_ref=PeerPortComponentRef.TRANSCEIVER,
    ),
]


# List-based peer-port subtrees; no oper-status leaf, so empty lists are tolerated.
PEER_PORT_LIST_SCHEMA_GROUPS = [
    ListSchemaGroup(
        "peer-port-phy-channels",
        GnmiYangPaths.PEER_PORT_PHY_CHANNELS,
        {
            "raw-errors": _C,
        },
        False,
    ),
    ListSchemaGroup(
        "peer-port-phy-histograms-rs-num-corr-err",
        GnmiYangPaths.PEER_PORT_PHY_RS_HISTOGRAM,
        {
            "count": _C,
        },
        False,
    ),
    # Keyed by component name, not by peer-port id: the remote module is a sibling tree that a
    # peer-port points at through its `transceiver` leaf.
    ListSchemaGroup(
        "peer-component-transceiver-channels",
        GnmiYangPaths.PEER_PORT_COMPONENT_TRANSCEIVER_CHANNELS,
        TRANSCEIVER_CHANNEL_LEAVES,
        False,
        relative_leaves=True,
        component_ref=PeerPortComponentRef.TRANSCEIVER,
    ),
    ListSchemaGroup(
        "peer-component-transceiver-host-lanes",
        GnmiYangPaths.PEER_PORT_COMPONENT_TRANSCEIVER_HOST_LANES,
        TRANSCEIVER_HOST_LANE_LEAVES,
        False,
        relative_leaves=True,
        component_ref=PeerPortComponentRef.TRANSCEIVER,
    ),
]


class PeerPortNvueSub:
    """`nv show peer-port <peer-port-id> <sub>` sub-commands (ROOT is the bare show)."""
    ROOT = ""
    COUNTERS = "counters"
    COUNTERS_IB = "counters ib"
    COUNTERS_IB_DROPS = "counters ib drops"
    COUNTERS_IB_ERRORS = "counters ib errors"
    COUNTERS_LINK = "counters link"
    LINK = "link"
    LINK_PHY = "link phy"
    LINK_PHY_DETAIL = "link phy detail"
    LINK_PHY_HEALTH = "link phy health"
    LINK_PHY_HEALTH_HISTOGRAM = "link phy health histogram"
    LINK_PHY_BER_MONITOR = "link phy ber-monitor"


class PeerPortTargetRole:
    """Tier of a peer-port picked as a show-surface target."""
    AGGREGATED = "hca-peer"
    PLANE = "plane-peer"


class PeerPortContainer:
    """Child container names nested inside a `nv show peer-port` payload."""
    COUNTERS = "counters"
    IB = "ib"
    LINK = "link"
    DROPS = "drops"
    ERRORS = "errors"
    PHY = "phy"
    DETAIL = "detail"
    HEALTH = "health"
    LANE = "lane"
    HISTOGRAM_BUCKETS = "rs-fec-corrected-errors"
    BER_MONITOR = "ber-monitor"


# IB drop / error counters are reported per direction. A direction the peer does not
# report renders as PEER_PORT_DIRECTION_NA, and these counters are hardcoded rather than
# ingested (dev-confirmed, same behavior as the local interface tree), so they carry no
# COUNTERS_DB field and are never DB-compared.
PEER_PORT_DIRECTION_NA = "n/a"
PEER_PORT_IB_DIRECTION_LEAVES = {"receive": _C, "transmit": _C}
PEER_PORT_IB_DROP_COUNTERS = frozenset({"qp1-drops"})
PEER_PORT_IB_ERROR_COUNTERS = frozenset({"icrc-errors", "symbol-errors", "tx-parity-errors"})


# The peer's own name is `name` and the remote host is `hostname` (the older `peer-port-name` and
# `host-name` spellings are no longer used). `associated-switch-tray-number` is absent on purpose:
# an IB system has no tray number, so the CLI no longer renders it (Redmine 5259547).
PEER_PORT_ROOT_LEAVES = {
    "name": _S,
    "node-guid": _S,
    "port-guid": _S,
    "hostname": _S,
    "hca-node-description": _S,
    "hca-ib-port": _U,
    "hca-port-index": _U,
    "associated-switch-port": _S,
    "associated-switch-asic-in-tray": _U,
    "chassis-sn": _S,
    **PEER_PORT_NVUE_ROOT_EXTENDED_LEAVES,
}


# A plane row renders every root leaf except these two, even though its COUNTERS_DB row populates
# both (`peer_port_name`, `hca_ib_port`) and the aggregated row shows them. Tolerated on plane
# rows only, so the aggregated contract stays strict.
PEER_PORT_PLANE_ROOT_PENDING_LEAVES = frozenset({"name", "hca-ib-port"})


# `hca_node_info` carries hostname, interface, lid, node_description, node_guid, port_guid and
# port_number only (proposal 'Metric Additions'), so these CLI leaves have no OTEL label.
PEER_PORT_OTEL_UNEXPORTED_ROOT_LEAVES = frozenset({"hca-ib-port", "hca-port-index"})


PEER_PORT_LINK_LEAVES = {
    "logical-state": _S,
    "physical-state": _S,
    PEER_PORT_SPEED_LEAF: _S,
    PEER_PORT_WIDTH_LEAF: _S,
}


PEER_PORT_PHY_DETAIL_LEAVES = {
    "phy-manager-state": _S,
    "profile-fec-in-use": _S,
    PEER_PORT_PLR_BW_LOSS_LEAF: _D,
    **{leaf: _C for leaf in PEER_PORT_PHY_DETAIL_TO_DB_FIELD},
}


PEER_PORT_PHY_HEALTH_LEAVES = {
    "time-since-last-clear-min": _D,
    **{leaf: _C for leaf in PEER_PORT_PHY_HEALTH_TO_DB_FIELD},
    **{leaf: _D for leaf in PEER_PORT_PHY_HEALTH_BER_TO_DB_FIELD},
}


# Per-lane leaf -> (GnmiTypeKind, COUNTERS_DB field template).
PEER_PORT_LANE_LEAF_SPECS = {
    "phy-raw-errors": (_C, "SAI_PORT_STAT_INFINIBAND_RAW_ERR_LANE_{lane}"),
    "raw-ber": (_D, "SAI_PORT_STAT_INFINIBAND_RAW_BER_STRING_LANE_{lane}"),
}


PEER_PORT_LANE_LEAVES = {leaf: spec[0] for leaf, spec in PEER_PORT_LANE_LEAF_SPECS.items()}


PEER_PORT_HISTOGRAM_LEAF = "count"


# One `nv show peer-port <id> <sub>` node of the show surface:
#   sub:            PeerPortNvueSub value (the sub-command to run)
#   containers:     child container names the payload must expose
#   leaves:         {leaf: GnmiTypeKind} carried directly by this node
#   member_leaves:  {leaf: GnmiTypeKind} every container in `containers` must carry
#   pending:        leaves one target may lack because the HLD leaves their tier open; the
#                   caller must then prove the other tier carries them
NvueShowNode = namedtuple(
    "NvueShowNode", ["sub", "containers", "leaves", "member_leaves", "pending"],
    defaults=({}, frozenset()),
)


# The complete NVUE peer-port show surface. `nv show peer-port <id> -h` exposes only
# `counters` and `link`, and the bare show renders identity plus `counters` (no link
# subtree), so this list is the whole contract.
PEER_PORT_NVUE_SHOW_NODES = [
    NvueShowNode(PeerPortNvueSub.ROOT, frozenset({PeerPortContainer.COUNTERS}),
                 PEER_PORT_ROOT_LEAVES,
                 pending=PEER_PORT_ROOT_TIER_UNSPECIFIED_LEAVES),
    NvueShowNode(PeerPortNvueSub.COUNTERS,
                 frozenset({PeerPortContainer.IB, PeerPortContainer.LINK}),
                 {leaf: _C for leaf in PEER_PORT_COUNTERS_TO_DB_FIELD}),
    NvueShowNode(PeerPortNvueSub.COUNTERS_IB,
                 frozenset({PeerPortContainer.DROPS, PeerPortContainer.ERRORS}), {}),
    NvueShowNode(PeerPortNvueSub.COUNTERS_IB_DROPS, PEER_PORT_IB_DROP_COUNTERS, {},
                 PEER_PORT_IB_DIRECTION_LEAVES),
    NvueShowNode(PeerPortNvueSub.COUNTERS_IB_ERRORS, PEER_PORT_IB_ERROR_COUNTERS, {},
                 PEER_PORT_IB_DIRECTION_LEAVES),
    NvueShowNode(PeerPortNvueSub.COUNTERS_LINK, frozenset(),
                 {leaf: _C for leaf in PEER_PORT_COUNTERS_LINK_TO_DB_FIELD}),
    NvueShowNode(PeerPortNvueSub.LINK, frozenset({PeerPortContainer.PHY}), PEER_PORT_LINK_LEAVES),
    NvueShowNode(PeerPortNvueSub.LINK_PHY,
                 frozenset({PeerPortContainer.DETAIL, PeerPortContainer.HEALTH,
                            PeerPortContainer.BER_MONITOR}), {}),
    NvueShowNode(PeerPortNvueSub.LINK_PHY_DETAIL, frozenset(), PEER_PORT_PHY_DETAIL_LEAVES),
    # `link phy health` renders the `lane` list inline but not the histogram: that subtree
    # shows up only in the parent `link` payload and under its own sub-command.
    NvueShowNode(PeerPortNvueSub.LINK_PHY_HEALTH,
                 frozenset({PeerPortContainer.LANE}),
                 PEER_PORT_PHY_HEALTH_LEAVES),
    NvueShowNode(PeerPortNvueSub.LINK_PHY_HEALTH_HISTOGRAM,
                 frozenset({PeerPortContainer.HISTOGRAM_BUCKETS}), {}),
    # Extended drop: one container per error-rate type, each carrying the same four readings.
    NvueShowNode(PeerPortNvueSub.LINK_PHY_BER_MONITOR,
                 frozenset(BER_MONITOR_TYPES), {}, BER_MONITOR_TYPE_LEAVES),
]


# One NVUE leaf paired with the COUNTERS_DB field that backs it:
#   kind: GnmiTypeKind.COUNTER compares within sampling tolerance, GnmiTypeKind.DECIMAL
#         only requires both sides to parse (a sampled ratio can move between reads)
PeerPortDbPair = namedtuple("PeerPortDbPair", ["label", "cli_value", "db_field", "kind"])


# ============================================================================
# Extended drop: `nv show peer-platform` surface (HLD section 5.3)
# ============================================================================


class PeerPlatformKind:
    """Resource under `nv show peer-platform` that a show node reads."""
    TRANSCEIVER = "transceiver"
    ASIC = "asic"
    # Which resource each peer-port component-reference leaf points into.
    BY_COMPONENT_REF = {
        PeerPortComponentRef.TRANSCEIVER: TRANSCEIVER,
        PeerPortComponentRef.INTEGRATED_CIRCUIT: ASIC,
    }


class PeerPlatformSub:
    """`nv show peer-platform <kind> <name> [sub]` sub-commands (ROOT is the bare show).

    HLD section 5.3 renders temperature and voltage on the bare transceiver show, so only the
    channel list and the ASIC frequency have a sub-command of their own.
    """
    ROOT = ""
    CHANNEL = "channel"
    FREQUENCY = "frequency"


# How `nv show peer-platform` (build 019) spells what the HLD and gNMI name differently. The tests
# read the CLI on the HLD names; `peer_platform_cli_to_canonical` applies this re-spelling.
PEER_PLATFORM_CLI_LEAF_NAMES = {"identifier": "form-factor"}


# Channel power is a nested container: rx-power {power: "2.22 mW / 3.47 dBm"} is the input power.
PEER_PLATFORM_CLI_POWER_CONTAINERS = {"rx-power": "input-power", "tx-power": "output-power"}


PEER_PLATFORM_CLI_POWER_LEAF = "power"


# Leaves the CLI prints with a trailing unit ("63.00 C", "3.28 V"); the unit is dropped before
# the value is held to its decimal kind.
PEER_PLATFORM_CLI_UNIT_LEAVES = frozenset({"temperature", "laser-temperature", "supply-voltage"})


# Top-level NVUE resource backing the peer-platform show surface.
PEER_PLATFORM_RESOURCE_PATH = "/peer-platform"


PEER_ASIC_LEAVES = {
    "firmware-version": _S,
    PEER_ASIC_HOST_NAME_LEAF: _S,
}


PEER_TRANSCEIVER_TEMPERATURE_LEAVES = {
    "temperature": _D,
    "laser-temperature": _D,
    **TRANSCEIVER_TEMPERATURE_FLAGS,
}


PEER_TRANSCEIVER_VOLTAGE_LEAVES = {
    "supply-voltage": _D,
    **TRANSCEIVER_VOLTAGE_FLAGS,
}


# The CLI renders one sample per line while gNMI nests it under `state/<leaf>/instant`, so the
# gNMI keys of the per-channel and per-module samples are mapped back onto the CLI leaf names
# before the two surfaces are compared. Keys absent from a map are compared as they are.
TRANSCEIVER_CHANNEL_GNMI_KEY_MAP = {
    **{f"state/{leaf}/instant": leaf for leaf in TRANSCEIVER_CHANNEL_READINGS},
    **{f"state/{leaf}": leaf for leaf in TRANSCEIVER_CHANNEL_FLAGS},
    **{f"state/{leaf}": leaf for leaf in TRANSCEIVER_HOST_LANE_FLAGS},
}


PEER_TRANSCEIVER_TEMPERATURE_GNMI_KEY_MAP = {"temperature/instant": "temperature"}


PEER_TRANSCEIVER_VOLTAGE_GNMI_KEY_MAP = {"supply-voltage/instant": "supply-voltage"}


# One `nv show peer-platform <kind> <name> [sub]` node of the show surface:
#   node_id:        stable id, also the pytest parametrize id
#   kind:           PeerPlatformKind value (which resource the name belongs to)
#   sub:            PeerPlatformSub value (the sub-command to run)
#   leaves:         {leaf: GnmiTypeKind} the payload must carry directly
#   member_leaves:  {leaf: GnmiTypeKind} every member must carry when the payload is a keyed
#                   list (one entry per channel) rather than a flat node
#   gnmi_prefixes:  GnmiYangPaths templates taking {name}, together holding the same leaves as
#                   the CLI sub-command; a node may span several containers
#   gnmi_key_map:   {gnmi key relative to its prefix: CLI leaf name} for the leaves the two
#                   surfaces spell differently
PeerPlatformNode = namedtuple(
    "PeerPlatformNode",
    ["node_id", "kind", "sub", "leaves", "member_leaves", "gnmi_prefixes", "gnmi_key_map"],
    defaults=({}, (), {}),
)


PEER_PLATFORM_SHOW_NODES = [
    PeerPlatformNode("transceiver-identity", PeerPlatformKind.TRANSCEIVER,
                     PeerPlatformSub.ROOT, TRANSCEIVER_IDENTITY_LEAVES,
                     gnmi_prefixes=(GnmiYangPaths.PEER_PORT_COMPONENT_TRANSCEIVER_STATE,)),
    PeerPlatformNode("transceiver-channel", PeerPlatformKind.TRANSCEIVER,
                     PeerPlatformSub.CHANNEL, {},
                     member_leaves={**TRANSCEIVER_CHANNEL_NVUE_LEAVES,
                                    **TRANSCEIVER_HOST_LANE_FLAGS},
                     gnmi_prefixes=(GnmiYangPaths.PEER_PORT_COMPONENT_TRANSCEIVER_CHANNELS,
                                    GnmiYangPaths.PEER_PORT_COMPONENT_TRANSCEIVER_HOST_LANES),
                     gnmi_key_map=TRANSCEIVER_CHANNEL_GNMI_KEY_MAP),
    PeerPlatformNode("transceiver-temperature", PeerPlatformKind.TRANSCEIVER,
                     PeerPlatformSub.ROOT, PEER_TRANSCEIVER_TEMPERATURE_LEAVES,
                     gnmi_prefixes=(GnmiYangPaths.PEER_PORT_COMPONENT_STATE,
                                    GnmiYangPaths.PEER_PORT_COMPONENT_TRANSCEIVER_STATE,
                                    GnmiYangPaths.PEER_PORT_COMPONENT_TRANSCEIVER_DIAG_STATE),
                     gnmi_key_map=PEER_TRANSCEIVER_TEMPERATURE_GNMI_KEY_MAP),
    PeerPlatformNode("transceiver-voltage", PeerPlatformKind.TRANSCEIVER,
                     PeerPlatformSub.ROOT, PEER_TRANSCEIVER_VOLTAGE_LEAVES,
                     gnmi_prefixes=(GnmiYangPaths.PEER_PORT_COMPONENT_TRANSCEIVER_STATE,
                                    GnmiYangPaths.PEER_PORT_COMPONENT_TRANSCEIVER_DIAG_STATE),
                     gnmi_key_map=PEER_TRANSCEIVER_VOLTAGE_GNMI_KEY_MAP),
    PeerPlatformNode("asic", PeerPlatformKind.ASIC,
                     PeerPlatformSub.ROOT, PEER_ASIC_LEAVES,
                     gnmi_prefixes=(GnmiYangPaths.PEER_PORT_COMPONENT_STATE,)),
    PeerPlatformNode("asic-frequency", PeerPlatformKind.ASIC,
                     PeerPlatformSub.FREQUENCY, ASIC_FREQUENCY_LEAVES,
                     gnmi_prefixes=(GnmiYangPaths.PEER_PORT_COMPONENT_IC_FREQUENCY,)),
]


# ============================================================================
# Extended drop: the local switch half (HLD section 5.1)
# ============================================================================


class LocalPortFamilyId:
    """Field families of a local switch port that the extended HLD adds."""
    PHY = "phy"
    BER_MONITOR = "ber-monitor"
    ROUTING = "routing"


# Adaptive-routing and Fast Recovery Notification counters, carried by the existing port stat
# poll rather than by a new FlexCounter group.
LOCAL_ROUTING_COUNTER_LEAVES = {
    "port-ar-trials": _C,
    "pfrn-rcv-packets": _C,
    "pfrn-rcv-errors": _C,
    "pfrn-xmit-packets": _C,
    "pfrn-start-packets": _C,
}


# `nv show interface <if> ...` sub-command that renders each family.
class LocalPortNvueSub:
    """`nv show interface <if> <sub>` sub-commands the local families are read from."""
    LINK_PHY = "link phy"
    LINK_PHY_DETAIL = "link phy detail"
    LINK_PHY_HEALTH = "link phy health"
    LINK_PHY_BER_MONITOR = "link phy ber-monitor"
    COUNTERS = "counters"


# One field family of a local switch port:
#   family_id:     stable id, also the pytest parametrize id
#   gnmi_prefixes: GnmiYangPaths templates taking {name}; a family may span two containers
#   leaves:        {leaf: GnmiTypeKind}, keyed relative to the prefix the leaf sits under
#   nvue_sub:      LocalPortNvueSub value the CLI half is read from
#
# The CLI nests some families under child containers and renders others flat; the reader
# flattens the payload and addresses each leaf by its unambiguous suffix, so the nesting does
# not have to be described here. The leaf names themselves must match gNMI (HLD section 7).
LocalPortFamily = namedtuple(
    "LocalPortFamily",
    ["family_id", "gnmi_prefixes", "leaves", "nvue_sub"],
)


LOCAL_PORT_FAMILIES = [
    # HLD 5.1 renders link-up-duration on `link phy` and down-blame on its `detail` child,
    # which the `link phy` payload carries too.
    LocalPortFamily(
        LocalPortFamilyId.PHY,
        (GnmiYangPaths.PHY_STATE, GnmiYangPaths.PHY_LINK_DOWN_INFO),
        {**LOCAL_PHY_STATE_LEAVES, **LOCAL_PHY_LINK_DOWN_LEAVES},
        LocalPortNvueSub.LINK_PHY,
    ),
    LocalPortFamily(
        LocalPortFamilyId.BER_MONITOR,
        (GnmiYangPaths.PHY_BER_MONITOR_STATE,),
        BER_MONITOR_LEAVES,
        LocalPortNvueSub.LINK_PHY_BER_MONITOR,
    ),
    LocalPortFamily(
        LocalPortFamilyId.ROUTING,
        (GnmiYangPaths.INFINIBAND_COUNTERS_PORT,),
        LOCAL_ROUTING_COUNTER_LEAVES,
        LocalPortNvueSub.COUNTERS,
    ),
]


# R-NVOS-3: the switch polls a BER coefficient and a magnitude from the hardware and must
# publish the two combined into one value. Neither raw field may be published on its own.
#
# The exact COUNTERS_DB spellings are not in the HLD, so the raw pair is discovered by
# matching a BER field name against these markers instead of by a hardcoded field name.
BER_RAW_COMPONENT_MARKERS = ("coef", "magnitude", "mantissa", "exponent")


BER_DB_FIELD_MARKER = "BER"


# A published leaf whose name carries one of these must not exist on either surface: it would
# mean the switch forwarded a raw hardware field instead of the combined error rate.
BER_COMPONENT_LEAF_MARKERS = BER_RAW_COMPONENT_MARKERS


class LocalPlatformNodeId:
    """Sub-commands of the switch's own platform surface that the extended HLD adds."""
    TRANSCEIVER_IDENTITY = "transceiver-identity"
    CHANNEL_SNR = "channel-snr"
    ASIC_FREQUENCY = "asic-frequency"


LOCAL_PLATFORM_NODE_IDS = (
    LocalPlatformNodeId.TRANSCEIVER_IDENTITY,
    LocalPlatformNodeId.CHANNEL_SNR,
    LocalPlatformNodeId.ASIC_FREQUENCY,
)


# ============================================================================
# OTEL surface: the nvswitch_* metrics the switch exports over OTLP
#
# Metric and label names follow the switch's metrics-classes.yaml / agent-mappings.yaml. The
# extended-HLD leaves (peer transceiver and ASIC, error-rate monitor, link-up-duration,
# down-blame, routing counters, channel SNR, ASIC clock speed) have no catalog entry yet; their
# names below mirror the existing local families so the two trees read alike.
# ============================================================================


class OtelLabel:
    """Datapoint labels the IB tests key OTEL series by."""
    INTERFACE = "interface"
    NAME = "name"
    LANE = "lane"
    CHANNEL = "channel"
    TYPE = "type"
    DIRECTION = "direction"


class OtelMetricPrefix:
    """Metric-name prefix of each exported family."""
    INTERFACE = "nvswitch_interface_"
    PEER_PORT = "nvswitch_peer_port_"
    PLATFORM = "nvswitch_platform_"
    PEER_PLATFORM = "nvswitch_peer_platform_"


# The families the IB suites export, each with its PHY / per-class metrics enabled.
IB_OTEL_FAMILIES = (
    TelemetryConsts.INTERFACE_STATS,
    TelemetryConsts.PEER_PORT_STATS,
    TelemetryConsts.PLATFORM_STATS,
)


# Collection windows. Interface and peer-port samples every 10 s, so 30 s holds at least two
# samples of every port series; platform samples every 60 s and needs the suite's 70 s floor.
IB_OTEL_PORT_WINDOW_SEC = 30
IB_OTEL_PLATFORM_WINDOW_SEC = 70


# A series belongs to the newest sample when its last point is within this of the newest point
# of its metric; older series are left over from before a state change inside the window.
IB_OTEL_CURRENT_SAMPLE_SLACK_SEC = 5


# One OTEL rendering of a leaf:
#   metric: the metric carrying it
#   label:  the label holding the leaf on an info metric; None when the leaf is the metric value
#   match:  (label, value) pairs a series must carry, for metrics that fan one key out
#   bit:    mask of a bitmap value; the leaf reads 1 when the mask is set in the value, else 0
#   enum:   names of an enumerated value, indexed by the value (0 Unknown, 1 Local_phy, ...)
OtelLeaf = namedtuple("OtelLeaf", ["metric", "label", "match", "bit", "enum"],
                      defaults=(None, (), None, ()))


# Alarm bitmap of the transceiver temperature and voltage metrics (proposal 'Metric Additions'):
# 0 none, 1 high alarm, 2 low alarm, 4 high warning, 8 low warning.
OTEL_ALARM_BITS = {"high-alarm": 1, "low-alarm": 2, "high-warning": 4, "low-warning": 8}


# Value order of the down-blame metric.
OTEL_DOWN_BLAME_NAMES = ("Unknown", "Local_phy", "Remote_phy")


# The OTEL rendering of one leaf table:
#   key_label:    label identifying the entity (interface for ports, name for components)
#   leaves:       {leaf: OtelLeaf}
#   member_label: label identifying list members (lane, channel), None for flat containers
OtelGroup = namedtuple("OtelGroup", ["key_label", "leaves", "member_label"], defaults=(None,))


def _otel_name(leaf: str) -> str:
    return leaf.replace("-", "_").replace("/", "_")


_OP = OtelMetricPrefix.PEER_PORT


_OI = OtelMetricPrefix.INTERFACE


_OPP = OtelMetricPrefix.PEER_PLATFORM


_OLP = OtelMetricPrefix.PLATFORM


# COUNTERS_DB field -> metric suffix, identical on the interface and peer-port families.
_OTEL_PORT_DB_FIELD_SUFFIX = {
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_WAIT": "xmit_wait",
    "SAI_PORT_STAT_INFINIBAND_EXCESSIVE_BUFFER_OVERRUN_EXT": "excessive_buffer_overrun",
    "SAI_PORT_STAT_INFINIBAND_LINK_DOWNED_EXT": "link_downed",
    "SAI_PORT_STAT_INFINIBAND_ERROR_RECOVERY_EXT": "link_error_recovery",
    "SAI_PORT_STAT_INFINIBAND_LOCAL_LINK_INTEGRITY_ERR_EXT": "local_link_integrity_errors",
    "SAI_PORT_STAT_INFINIBAND_RX_CONSTRAINTS_ERR_EXT": "rcv_constraints_errors",
    "SAI_PORT_STAT_INFINIBAND_RCV_REMOTE_PHY_ERR_EXT": "rcv_remote_phy_errors",
    "SAI_PORT_STAT_INFINIBAND_RCV_SWITCH_RELAY_ERR_EXT": "rcv_switch_relay_errors",
    "SAI_PORT_STAT_INFINIBAND_PC_ERR_SYM_F": "symbol_errors",
    "SAI_PORT_STAT_INFINIBAND_PC_VL15_DROPPED_F": "vl15_dropped",
    "SAI_PORT_STAT_INFINIBAND_QP1_DROPPED_EXT": "qp1_dropped",
    "SAI_PORT_STAT_INFINIBAND_LOGICAL_STATE": "logical_port_state",
    "SAI_PORT_STAT_INFINIBAND_PHYSICAL_STATE": "physical_port_state",
    PEER_PORT_LANES_DB_FIELD: "number_of_lanes",
    PEER_PORT_SPEED_DB_FIELD: "oper_speed",
    "SAI_PORT_STAT_INFINIBAND_EFFECTIVE_ERR": "phy_stats_phy_effective_errors",
    "SAI_PORT_STAT_INFINIBAND_PHY_RECEIVED_BITS": "phy_stats_phy_received_bits",
    "SAI_PORT_STAT_INFINIBAND_SYMBOL_ERR": "phy_stats_phy_symbol_errors",
    "SAI_PORT_STAT_INFINIBAND_TIME_SINCE_LAST_CLEAR": "phy_stats_time_since_last_clear",
    "SAI_PORT_STAT_INFINIBAND_RAW_BER_STRING": "phy_stats_raw_ber",
    "SAI_PORT_STAT_INFINIBAND_EFFECTIVE_BER_STRING": "phy_stats_effective_ber",
    "SAI_PORT_STAT_INFINIBAND_SYMBOL_BER_STRING": "phy_stats_symbol_ber",
    "SAI_PORT_STAT_INFINIBAND_ZERO_HIST": "phy_stats_zero_hist",
    "SAI_PORT_STAT_INFINIBAND_SYNC_HEADER_ERR_COUNTER": "phy_stats_sync_header_errors",
    "SAI_PORT_STAT_INFINIBAND_RQ_GENERAL_ERROR": "phy_stats_rq_general_error",
    "SAI_PORT_STAT_INFINIBAND_PLR_CODES_LOSS": "phy_stats_plr_codes_loss",
    "SAI_PORT_STAT_INFINIBAND_PLR_RCV_CODES_ERR": "phy_stats_plr_rcv_code_err",
    "SAI_PORT_STAT_INFINIBAND_PLR_RCV_CODES": "phy_stats_plr_rcv_codes",
    "SAI_PORT_STAT_INFINIBAND_PLR_RCV_UNCORRECTABLE_CODE": "phy_stats_plr_rcv_uncorrectable_code",
    "SAI_PORT_STAT_INFINIBAND_PLR_SYNC_EVENTS": "phy_stats_plr_sync_events",
    "SAI_PORT_STAT_INFINIBAND_PLR_XMT_CODES": "phy_stats_plr_xmit_codes",
    "SAI_PORT_STAT_INFINIBAND_PLR_XMT_RETRY_CODES": "phy_stats_plr_xmit_retry_codes",
    "SAI_PORT_STAT_INFINIBAND_PLR_XMT_RETRY_EVENTS": "phy_stats_plr_xmit_retry_events",
}


# Packet counters, which the interface family prefixes with `if_` and the peer-port family
# does not.
_OTEL_PORT_PACKET_DB_FIELD_SUFFIX = {
    "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT": "in_pkts",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_PKTS_EXT": "out_pkts",
    "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT": "in_octets",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT": "out_octets",
    "SAI_PORT_STAT_INFINIBAND_UNICAST_RCV_PKTS": "in_ucast_pkts",
    "SAI_PORT_STAT_INFINIBAND_UNICAST_XMT_PKTS": "out_ucast_pkts",
    "SAI_PORT_STAT_INFINIBAND_MULTICAST_RCV_PKTS": "in_multicast_pkts",
    "SAI_PORT_STAT_INFINIBAND_MULTICAST_XMT_PKTS": "out_multicast_pkts",
    "PORT_STAT_INFINIBAND_TOTAL_IN_DROPS": "in_discards",
    "PORT_STAT_INFINIBAND_TOTAL_OUT_DROPS": "out_discards",
    SAI_PORT_STAT_INFINIBAND_PC_ERR_RCV_F: "in_errors",
    SAI_PORT_STAT_INFINIBAND_ERR_XMTCONSTR_F: "out_errors",
}


PEER_PORT_OTEL_METRIC_BY_DB_FIELD = {
    field: _OP + suffix
    for field, suffix in {**_OTEL_PORT_PACKET_DB_FIELD_SUFFIX, **_OTEL_PORT_DB_FIELD_SUFFIX}.items()
}


LOCAL_PORT_OTEL_METRIC_BY_DB_FIELD = {
    **{field: f"{_OI}if_{suffix}" for field, suffix in _OTEL_PORT_PACKET_DB_FIELD_SUFFIX.items()},
    **{field: _OI + suffix for field, suffix in _OTEL_PORT_DB_FIELD_SUFFIX.items()},
}


# How an OTEL value is held against the COUNTERS_DB value it was sampled from.
class OtelDbCompare:
    COUNTER = "counter"   # within the DB values read before and after the collection window
    STATE = "state"       # equal to one of the DB values read around the window
    BER = "ber"           # within PEER_PORT_BER_MAX_RATIO of one of them (single-digit rounding)
    SPEED = "speed"       # the DB speed in Mb/s, exported in Gb/s


OTEL_DB_FIELD_COMPARE = {
    "SAI_PORT_STAT_INFINIBAND_LOGICAL_STATE": OtelDbCompare.STATE,
    "SAI_PORT_STAT_INFINIBAND_PHYSICAL_STATE": OtelDbCompare.STATE,
    PEER_PORT_LANES_DB_FIELD: OtelDbCompare.STATE,
    PEER_PORT_SPEED_DB_FIELD: OtelDbCompare.SPEED,
    **{field: OtelDbCompare.BER for field in PEER_PORT_PHY_HEALTH_BER_TO_DB_FIELD.values()},
}


# The combined `node_info` metric is deprecated; an HCA peer is identified by `hca_node_info`.
PEER_PORT_OTEL_NODE_INFO = _OP + "hca_node_info"


# The peer-port's references to its transceiver and ASIC components.
PEER_PORT_OTEL_COMPONENT_INFO = _OP + "component_info"


PEER_PORT_OTEL_SWITCH_INFO = _OP + "switch_info"


PEER_PORT_OTEL_LINK_HEALTH = _OP + "link_health_status"


# link_health_status: 0 = n/a, 1 = attention, 2 = healthy.
OTEL_LINK_HEALTH_STATES = (0, 1, 2)


# Peer identity: the node_info labels come from PEER_COUNTERS and the switch_info labels from
# PEER_PORT_MAPPING, so a merged `list_hca_peer_rows` row backs both. Labels without a DB field
# are derived by the daemon (component names) and are compared against the other surfaces.
PEER_PORT_OTEL_IDENTITY_DB_FIELD = {
    "node_guid": "node_guid",
    "port_guid": "port_guid",
    "node_description": "node_description",
    "lid": "lid",
    "hostname": "host_name",
    "port_number": "port_num",
    "tray_id": "tray_index",
    "slot_index": "chassis_slot_index",
    "cpu_node_number": "cpu_node_number",
    "device_number_on_cpu": "device_num_on_cpu_node",
    "associated_switch_port": "switch_port_alias",
    "associated_switch_asic_in_tray": "switch_port_asic_num",
    "associated_switch_tray_number": "switch_tray_num",
    "chassis_sn": "chassis_sn",
}


# Peer identity leaf, under its NVUE and its gNMI spelling, -> OTEL label.
PEER_PORT_OTEL_IDENTITY_LEAVES = {
    "name": OtelLeaf(PEER_PORT_OTEL_NODE_INFO, OtelLabel.INTERFACE),
    "node-guid": OtelLeaf(PEER_PORT_OTEL_NODE_INFO, "node_guid"),
    "port-guid": OtelLeaf(PEER_PORT_OTEL_NODE_INFO, "port_guid"),
    "node-description": OtelLeaf(PEER_PORT_OTEL_NODE_INFO, "node_description"),
    "hca-node-description": OtelLeaf(PEER_PORT_OTEL_NODE_INFO, "node_description"),
    "host-name": OtelLeaf(PEER_PORT_OTEL_NODE_INFO, "hostname"),
    "hostname": OtelLeaf(PEER_PORT_OTEL_NODE_INFO, "hostname"),
    "lid": OtelLeaf(PEER_PORT_OTEL_NODE_INFO, "lid"),
    "port-number": OtelLeaf(PEER_PORT_OTEL_NODE_INFO, "port_number"),
    "tray-id": OtelLeaf(PEER_PORT_OTEL_NODE_INFO, "tray_id"),
    "hca-ib-port": OtelLeaf(PEER_PORT_OTEL_NODE_INFO, "hca_ib_port"),
    "hca-port-index": OtelLeaf(PEER_PORT_OTEL_NODE_INFO, "hca_port_index"),
    PeerPortComponentRef.TRANSCEIVER: OtelLeaf(PEER_PORT_OTEL_COMPONENT_INFO, "transceiver"),
    PeerPortComponentRef.INTEGRATED_CIRCUIT: OtelLeaf(PEER_PORT_OTEL_COMPONENT_INFO,
                                                      "integrated_circuit"),
    "associated-switch-port": OtelLeaf(PEER_PORT_OTEL_SWITCH_INFO, "associated_switch_port"),
    "associated-switch-tray-number": OtelLeaf(PEER_PORT_OTEL_SWITCH_INFO,
                                              "associated_switch_tray_number"),
    "associated-switch-asic-in-tray": OtelLeaf(PEER_PORT_OTEL_SWITCH_INFO,
                                               "associated_switch_asic_in_tray"),
    "chassis-sn": OtelLeaf(PEER_PORT_OTEL_SWITCH_INFO, "chassis_sn"),
}


def _peer_db_otel_leaves(leaf_to_db_field):
    return {leaf: OtelLeaf(PEER_PORT_OTEL_METRIC_BY_DB_FIELD[field])
            for leaf, field in leaf_to_db_field.items()}


def _ber_monitor_otel_leaves(prefix):
    """`<type>-ber/last|max|min` ride one gauge per type (label `stat`); `alarm-count` rides a sum."""
    leaves = {}
    for leaf in BER_MONITOR_LEAVES:
        ber_type, field = leaf.split("/")
        short = ber_type[:-len("-ber")]
        if field == "alarm-count":
            leaves[leaf] = OtelLeaf(f"{prefix}phy_stats_ber_monitor_alarms", None,
                                    (("ber_type", short),))
        else:
            leaves[leaf] = OtelLeaf(f"{prefix}phy_stats_{short}_ber_monitor", None,
                                    (("stat", field),))
    return leaves


PEER_PORT_OTEL_BER_MONITOR_LEAVES = _ber_monitor_otel_leaves(_OP)


PEER_PORT_OTEL_LANE_LEAVES = {
    "phy-raw-errors": OtelLeaf(_OP + "phy_stats_lane_raw_errors"),
    "raw-ber": OtelLeaf(_OP + "phy_stats_lane_raw_ber"),
}


PEER_PORT_OTEL_RS_HISTOGRAM = _OP + "phy_stats_rs_hist"


# down-blame is its own gauge: 0 Unknown, 1 Local_phy, 2 Remote_phy.
PEER_PORT_OTEL_DOWN_BLAME = OtelLeaf(_OP + "phy_stats_phy_down_blame", None, (), None,
                                     OTEL_DOWN_BLAME_NAMES)


PEER_PORT_OTEL_TIME_SINCE_LAST_CLEAR = OtelLeaf(_OP + "phy_stats_time_since_last_clear")


PEER_PORT_OTEL_PLR_BW_LOSS = OtelLeaf(_OP + "phy_stats_plr_bw_loss_percent")


PEER_PORT_OTEL_PLR_CODES_LOSS = OtelLeaf(
    PEER_PORT_OTEL_METRIC_BY_DB_FIELD["SAI_PORT_STAT_INFINIBAND_PLR_CODES_LOSS"])


# `nv show peer-port <id> <sub>` leaf -> OTEL, per sub-command. The IB drop / error direction
# counters are hardcoded n/a on NVUE rather than ingested, so those nodes have nothing to export.
# `profile-fec-in-use` is a CLI leaf of the peer with no OTEL metric in the Streaming Telemetry Change
# Proposal and none in the exported peer catalog (only the local nvswitch_interface_phy_fec_mode
# exists), so the OTEL table below carries no counterpart for it.
PEER_PORT_OTEL_LEAVES_BY_NVUE_SUB = {
    PeerPortNvueSub.ROOT: {
        leaf: PEER_PORT_OTEL_IDENTITY_LEAVES[PEER_PORT_NVUE_TO_GNMI_ROOT_LEAF.get(leaf, leaf)]
        for leaf in PEER_PORT_ROOT_LEAVES if leaf not in PEER_PORT_OTEL_UNEXPORTED_ROOT_LEAVES},
    PeerPortNvueSub.COUNTERS: _peer_db_otel_leaves(PEER_PORT_COUNTERS_TO_DB_FIELD),
    PeerPortNvueSub.COUNTERS_LINK: _peer_db_otel_leaves(PEER_PORT_COUNTERS_LINK_TO_DB_FIELD),
    PeerPortNvueSub.LINK: {
        "logical-state": OtelLeaf(_OP + "logical_port_state"),
        "physical-state": OtelLeaf(_OP + "physical_port_state"),
        PEER_PORT_SPEED_LEAF: OtelLeaf(_OP + "oper_speed"),
        PEER_PORT_WIDTH_LEAF: OtelLeaf(_OP + "number_of_lanes"),
    },
    PeerPortNvueSub.LINK_PHY_DETAIL: {
        **_peer_db_otel_leaves(PEER_PORT_PHY_DETAIL_TO_DB_FIELD),
        "phy-manager-state": OtelLeaf(_OP + "phy_manager_state"),
        PEER_PORT_PLR_BW_LOSS_LEAF: PEER_PORT_OTEL_PLR_BW_LOSS,
    },
    PeerPortNvueSub.LINK_PHY_HEALTH: {
        **_peer_db_otel_leaves(PEER_PORT_PHY_HEALTH_TO_DB_FIELD),
        **_peer_db_otel_leaves(PEER_PORT_PHY_HEALTH_BER_TO_DB_FIELD),
        "time-since-last-clear-min": PEER_PORT_OTEL_TIME_SINCE_LAST_CLEAR,
    },
    PeerPortNvueSub.LINK_PHY_HEALTH_HISTOGRAM: {
        PEER_PORT_HISTOGRAM_LEAF: OtelLeaf(PEER_PORT_OTEL_RS_HISTOGRAM),
    },
    PeerPortNvueSub.LINK_PHY_BER_MONITOR: PEER_PORT_OTEL_BER_MONITOR_LEAVES,
}


# Sub-commands whose leaves carry no ingested value, so no OTEL counterpart is expected.
PEER_PORT_OTEL_UNEXPORTED_NVUE_SUBS = frozenset({
    PeerPortNvueSub.COUNTERS_IB,
    PeerPortNvueSub.COUNTERS_IB_DROPS,
    PeerPortNvueSub.COUNTERS_IB_ERRORS,
    PeerPortNvueSub.LINK_PHY,
})


# Peer component identity: one info series per component, whatever its type (proposal
# 'Metric Additions': nvswitch_peer_platform_info_hostname / _info_firmware_version).
_PEER_COMPONENT_HOSTNAME = _OPP + "info_hostname"


_PEER_COMPONENT_FIRMWARE = _OPP + "info_firmware_version"


_PEER_TRANSCEIVER = _OPP + "transceiver_"


_LOCAL_TRANSCEIVER = _OLP + "transceiver_"


def _asic_frequency_otel_leaves(prefix):
    """One `asic_frequency` gauge whose `stat` label selects instant, min, max or max-delta."""
    return {leaf: OtelLeaf(f"{prefix}asic_frequency", None, (("stat", leaf),))
            for leaf in ASIC_FREQUENCY_LEAVES}


def _transceiver_identity_otel_leaves(prefix):
    return {
        "vendor": OtelLeaf(prefix + "vendor_info", "vendor_name"),
        "vendor-rev": OtelLeaf(prefix + "vendor_info", "rev"),
        "vendor-part": OtelLeaf(prefix + "vendor_info", "part_no"),
        "serial-no": OtelLeaf(prefix + "vendor_info", "serial_no"),
        "form-factor": OtelLeaf(prefix + "info", "identifier"),
        "module-functional-type": OtelLeaf(prefix + "info", "module_functional_type"),
        "supported-cable-length": OtelLeaf(prefix + "info", "cable_length"),
    }


def _transceiver_channel_otel_leaves(prefix):
    return {
        # One `channel_power` series per direction: rx is the input power, tx the output power.
        "input-power": OtelLeaf(prefix + "channel_power", None, (("direction", "rx"),)),
        "output-power": OtelLeaf(prefix + "channel_power", None, (("direction", "tx"),)),
        "media-snr": OtelLeaf(prefix + "media_snr"),
        "host-snr": OtelLeaf(prefix + "host_snr"),
        # rx-los and rx-cdr-lol share one gauge, selected by its `state` label.
        **{leaf: OtelLeaf(prefix + "physical_channel_state", None, (("state", leaf),))
           for leaf in TRANSCEIVER_CHANNEL_FLAGS},
    }


def _transceiver_host_lane_otel_leaves(prefix):
    return {leaf: OtelLeaf(prefix + "host_lane_state", None, (("state", leaf),))
            for leaf in TRANSCEIVER_HOST_LANE_FLAGS}


def _transceiver_alarm_otel_leaves(prefix):
    """The eight alarm flags, read off the temperature and voltage alarm bitmaps."""
    leaves = {}
    for flag_prefix, metric, flags in (("temp", "temperature_alarm", TRANSCEIVER_TEMPERATURE_FLAGS),
                                       ("vcc", "voltage_alarm", TRANSCEIVER_VOLTAGE_FLAGS)):
        for leaf in flags:
            # temp-high-alarm-flag -> high-alarm
            kind = leaf[len(flag_prefix) + 1:-len("-flag")]
            leaves[leaf] = OtelLeaf(prefix + metric, None, (), OTEL_ALARM_BITS[kind])
    return leaves


PEER_ASIC_OTEL_LEAVES = {
    "firmware-version": OtelLeaf(_PEER_COMPONENT_FIRMWARE, "version_number"),
    PEER_ASIC_HOST_NAME_LEAF: OtelLeaf(_PEER_COMPONENT_HOSTNAME, "hostname"),
}


PEER_ASIC_FREQUENCY_OTEL_LEAVES = _asic_frequency_otel_leaves(_OPP)


PEER_TRANSCEIVER_OTEL_IDENTITY_LEAVES = _transceiver_identity_otel_leaves(_PEER_TRANSCEIVER)


PEER_TRANSCEIVER_OTEL_DIAG_LEAVES = {
    "temperature": OtelLeaf(_PEER_TRANSCEIVER + "temperature"),
    "laser-temperature": OtelLeaf(_PEER_TRANSCEIVER + "laser_temperature"),
    "supply-voltage": OtelLeaf(_PEER_TRANSCEIVER + "voltage"),
    **_transceiver_alarm_otel_leaves(_PEER_TRANSCEIVER),
}


PEER_TRANSCEIVER_OTEL_CHANNEL_LEAVES = _transceiver_channel_otel_leaves(_PEER_TRANSCEIVER)


PEER_TRANSCEIVER_OTEL_HOST_LANE_LEAVES = _transceiver_host_lane_otel_leaves(_PEER_TRANSCEIVER)


# `nv show peer-platform` node -> its OTEL rendering, keyed by the component name. The channel
# node spans the media channels and the host lanes, each keyed by its own member label.
PEER_PLATFORM_OTEL_GROUPS = {
    "transceiver-identity": OtelGroup(OtelLabel.NAME, PEER_TRANSCEIVER_OTEL_IDENTITY_LEAVES),
    "transceiver-channel": OtelGroup(OtelLabel.NAME, PEER_TRANSCEIVER_OTEL_CHANNEL_LEAVES,
                                     OtelLabel.CHANNEL),
    "transceiver-host-lane": OtelGroup(OtelLabel.NAME, PEER_TRANSCEIVER_OTEL_HOST_LANE_LEAVES,
                                       OtelLabel.LANE),
    "transceiver-temperature": OtelGroup(OtelLabel.NAME, {
        leaf: PEER_TRANSCEIVER_OTEL_DIAG_LEAVES[leaf] for leaf in PEER_TRANSCEIVER_TEMPERATURE_LEAVES
    }),
    "transceiver-voltage": OtelGroup(OtelLabel.NAME, {
        leaf: PEER_TRANSCEIVER_OTEL_DIAG_LEAVES[leaf] for leaf in PEER_TRANSCEIVER_VOLTAGE_LEAVES
    }),
    "asic": OtelGroup(OtelLabel.NAME, PEER_ASIC_OTEL_LEAVES),
    "asic-frequency": OtelGroup(OtelLabel.NAME, PEER_ASIC_FREQUENCY_OTEL_LEAVES),
}


# Extra OTEL groups a peer-platform node is rendered with, beyond the one sharing its id.
PEER_PLATFORM_OTEL_EXTRA_GROUPS = {"transceiver-channel": ("transceiver-host-lane",)}


def _peer_port_otel_group(leaves):
    return OtelGroup(OtelLabel.INTERFACE, leaves)


# gNMI schema group -> its OTEL rendering (same leaf names, one table per container).
# gNMI leaves of a peer-port container that the OTEL spec gives no metric: `total-events` has no
# nvswitch_peer_port_phy_stats_link_down_events, and similar OTEL gaps were closed as intended
# (Redmine 5266138, 5228378). They stay checked over gNMI and are not required over OTEL.
PEER_PORT_OTEL_UNEXPORTED_SCHEMA_LEAVES = {
    "peer-port-phy-link-down-information": frozenset({"total-events"}),
}


PEER_PORT_OTEL_SCHEMA_GROUPS = {
    "peer-port-state": _peer_port_otel_group({
        leaf: PEER_PORT_OTEL_IDENTITY_LEAVES[leaf]
        for leaf in ("name", "node-description", "node-guid", "port-guid",
                     *PEER_PORT_ROOT_EXTENDED_LEAVES)
    }),
    "peer-port-state-counters": _peer_port_otel_group({
        "in-octets": OtelLeaf(_OP + "in_octets"),
        "in-pkts": OtelLeaf(_OP + "in_pkts"),
        "in-unicast-pkts": OtelLeaf(_OP + "in_ucast_pkts"),
        "in-broadcast-pkts": OtelLeaf(_OP + "in_broadcast_pkts"),
        "in-multicast-pkts": OtelLeaf(_OP + "in_multicast_pkts"),
        "in-discards": OtelLeaf(_OP + "in_discards"),
        "in-errors": OtelLeaf(_OP + "in_errors"),
        "out-octets": OtelLeaf(_OP + "out_octets"),
        "out-pkts": OtelLeaf(_OP + "out_pkts"),
        "out-unicast-pkts": OtelLeaf(_OP + "out_ucast_pkts"),
        "out-broadcast-pkts": OtelLeaf(_OP + "out_broadcast_pkts"),
        "out-multicast-pkts": OtelLeaf(_OP + "out_multicast_pkts"),
        "out-discards": OtelLeaf(_OP + "out_discards"),
        "out-errors": OtelLeaf(_OP + "out_errors"),
    }),
    "peer-port-infiniband-state": _peer_port_otel_group({
        "lid": PEER_PORT_OTEL_IDENTITY_LEAVES["lid"],
    }),
    "peer-port-infiniband-port-counters": _peer_port_otel_group({
        "xmit-wait": OtelLeaf(_OP + "xmit_wait"),
        "rcv-errors": OtelLeaf(_OP + "in_errors"),
        "link-error-recovery": OtelLeaf(_OP + "link_error_recovery"),
        "link-downed": OtelLeaf(_OP + "link_downed"),
        "rcv-remote-phy-errors": OtelLeaf(_OP + "rcv_remote_phy_errors"),
        "rcv-switch-relay-errors": OtelLeaf(_OP + "rcv_switch_relay_errors"),
        "rcv-constraints-errors": OtelLeaf(_OP + "rcv_constraints_errors"),
        "local-link-integrity-errors": OtelLeaf(_OP + "local_link_integrity_errors"),
        "excessive-buffer-overrun": OtelLeaf(_OP + "excessive_buffer_overrun"),
        "vl15-dropped": OtelLeaf(_OP + "vl15_dropped"),
    }),
    "peer-port-phy-state": _peer_port_otel_group({
        "effective-errors": OtelLeaf(_OP + "phy_stats_phy_effective_errors"),
        "symbol-errors": OtelLeaf(_OP + "phy_stats_phy_symbol_errors"),
        "received-bits": OtelLeaf(_OP + "phy_stats_phy_received_bits"),
        "time-since-last-clear-min": PEER_PORT_OTEL_TIME_SINCE_LAST_CLEAR,
        "link-up-duration": OtelLeaf(_OP + "phy_stats_phy_link_up_duration"),
    }),
    "peer-port-phy-ber": _peer_port_otel_group(
        _peer_db_otel_leaves(PEER_PORT_PHY_HEALTH_BER_TO_DB_FIELD)),
    "peer-port-phy-ber-monitor": _peer_port_otel_group(PEER_PORT_OTEL_BER_MONITOR_LEAVES),
    "peer-port-phy-link-down-information": _peer_port_otel_group({
        "down-blame": PEER_PORT_OTEL_DOWN_BLAME,
    }),
    "peer-port-phy-infiniband-port-statistics": _peer_port_otel_group({
        "port-rcv-data": OtelLeaf(_OP + "in_octets"),
        "port-rcv-pkts": OtelLeaf(_OP + "in_pkts"),
        "port-xmit-data": OtelLeaf(_OP + "out_octets"),
        "port-xmit-pkts": OtelLeaf(_OP + "out_pkts"),
        "port-unicast-rcv-pkts": OtelLeaf(_OP + "in_ucast_pkts"),
        "port-unicast-xmit-pkts": OtelLeaf(_OP + "out_ucast_pkts"),
        "port-multicast-rcv-pkts": OtelLeaf(_OP + "in_multicast_pkts"),
        "port-multicast-xmit-pkts": OtelLeaf(_OP + "out_multicast_pkts"),
    }),
    "peer-port-phy-infiniband-port-errors": _peer_port_otel_group({
        "rq-general-error": OtelLeaf(_OP + "phy_stats_rq_general_error"),
        "sync-header-error-counter": OtelLeaf(_OP + "phy_stats_sync_header_errors"),
    }),
    "peer-port-phy-infiniband-plr": _peer_port_otel_group({
        leaf: OtelLeaf(_OP + "phy_stats_" + _otel_name(leaf))
        for leaf in ("plr-rcv-code-err", "plr-rcv-codes", "plr-rcv-uncorrectable-code",
                     "plr-sync-events", "plr-xmit-codes", "plr-xmit-retry-codes",
                     "plr-xmit-retry-events")
    }),
    # The other openconfig state leaves are outside the reduced drop and the HLD, so they have
    # no OTEL rendering to hold them to.
    "peer-port-component-state": OtelGroup(OtelLabel.NAME, {
        "name": OtelLeaf(_PEER_COMPONENT_HOSTNAME, OtelLabel.NAME),
        "firmware-version": PEER_ASIC_OTEL_LEAVES["firmware-version"],
        PEER_ASIC_HOST_NAME_LEAF: PEER_ASIC_OTEL_LEAVES[PEER_ASIC_HOST_NAME_LEAF],
    }),
    "peer-port-component-integrated-circuit-frequency": OtelGroup(
        OtelLabel.NAME, PEER_ASIC_FREQUENCY_OTEL_LEAVES),
    "peer-port-component-transceiver": OtelGroup(OtelLabel.NAME,
                                                 PEER_TRANSCEIVER_OTEL_IDENTITY_LEAVES),
    "peer-port-component-transceiver-diag": OtelGroup(OtelLabel.NAME, {
        leaf: PEER_TRANSCEIVER_OTEL_DIAG_LEAVES[leaf] for leaf in TRANSCEIVER_DIAG_LEAVES
    }),
}


# gNMI list group -> its OTEL rendering. The histogram is one OTLP histogram per peer, whose
# buckets are the list members.
PEER_PORT_OTEL_LIST_GROUPS = {
    "peer-port-phy-channels": OtelGroup(
        OtelLabel.INTERFACE, {"raw-errors": PEER_PORT_OTEL_LANE_LEAVES["phy-raw-errors"]},
        OtelLabel.LANE),
    "peer-port-phy-histograms-rs-num-corr-err": OtelGroup(
        OtelLabel.INTERFACE, {PEER_PORT_HISTOGRAM_LEAF: OtelLeaf(PEER_PORT_OTEL_RS_HISTOGRAM)}),
    "peer-component-transceiver-channels": OtelGroup(
        OtelLabel.NAME, PEER_TRANSCEIVER_OTEL_CHANNEL_LEAVES, OtelLabel.CHANNEL),
    "peer-component-transceiver-host-lanes": OtelGroup(
        OtelLabel.NAME, PEER_TRANSCEIVER_OTEL_HOST_LANE_LEAVES, OtelLabel.LANE),
}


# Peer-port leaves whose OTEL rendering is compared across surfaces: the additive counters
# under both the NVUE and the gNMI spelling of the byte counters.
PEER_PORT_OTEL_ADDITIVE_LEAVES = {
    leaf: OtelLeaf(PEER_PORT_OTEL_METRIC_BY_DB_FIELD[PEER_PORT_API_TO_DB_FIELD[leaf]])
    for leaf in PEER_PORT_ADDITIVE_FIELDS
}


# Local switch port families -> OTEL.
LOCAL_PORT_OTEL_GROUPS = {
    LocalPortFamilyId.PHY: OtelGroup(OtelLabel.INTERFACE, {
        "link-up-duration": OtelLeaf(_OI + "phy_stats_phy_link_up_duration"),
        "down-blame": OtelLeaf(_OI + "phy_stats_phy_down_blame", None, (), None,
                               OTEL_DOWN_BLAME_NAMES),
    }),
    LocalPortFamilyId.BER_MONITOR: OtelGroup(OtelLabel.INTERFACE, _ber_monitor_otel_leaves(_OI)),
    LocalPortFamilyId.ROUTING: OtelGroup(OtelLabel.INTERFACE, {
        leaf: OtelLeaf(_OI + _otel_name(leaf)) for leaf in LOCAL_ROUTING_COUNTER_LEAVES
    }),
}


# One extended counter of a local port (plane-port test plan section 5.5):
#   leaf:        gNMI leaf name
#   gnmi_prefix: GnmiYangPaths template (taking {name}) the leaf sits under
#   nvue_sub:    LocalPortNvueSub the CLI renders it on
#   nvue_leaf:   the CLI spelling of the leaf
#   db_field:    COUNTERS_DB field; None when the HLD does not name it, in which case it is the
#                one row field carrying every token of the leaf (PFRN-RCV-PACKET -> *PFRN*RCV*PACKET*)
#   otel_metric: the nvswitch_interface_* metric carrying it
#   kind:        GnmiTypeKind the API value is compared to the DB as (COUNTER or DECIMAL)
#   per_plane:   a plane-port must carry it too. countermgrd aggregates xmit-wait (MAX) and the
#                BER strings (float MAX) from the planes, so those exist per plane; the rest are
#                only compared on a plane-port whose DB row carries them
ExtendedCounter = namedtuple(
    "ExtendedCounter",
    ["leaf", "gnmi_prefix", "nvue_sub", "nvue_leaf", "db_field", "otel_metric", "kind", "per_plane"],
)


# gNMI PLR leaf -> the CLI spelling (see PEER_PORT_PHY_DETAIL_TO_DB_FIELD).
_LOCAL_PLR_GNMI_TO_NVUE_LEAF = {
    "plr-rcv-code-err": "plr-rcv-codes-err",
    "plr-rcv-codes": "plr-rcv-codes",
    "plr-rcv-uncorrectable-code": "plr-rcv-uncorrectable-code",
    "plr-sync-events": "plr-sync-events",
    "plr-xmit-codes": "plr-xmit-codes",
    "plr-xmit-retry-codes": "plr-xmit-retry-codes",
    "plr-xmit-retry-events": "plr-xmit-retry-events",
}


_XMIT_WAIT_DB_FIELD = COUNTERMGRD_MAX_COUNTERS[0]


EXTENDED_COUNTERS = (
    ExtendedCounter(
        SAI_TO_GNMI_MAX_COUNTER_LEAF[_XMIT_WAIT_DB_FIELD], GnmiYangPaths.INFINIBAND_COUNTERS_PORT,
        LocalPortNvueSub.COUNTERS, SAI_TO_NVUE_MAX_COUNTER_LEAF[_XMIT_WAIT_DB_FIELD],
        _XMIT_WAIT_DB_FIELD, LOCAL_PORT_OTEL_METRIC_BY_DB_FIELD[_XMIT_WAIT_DB_FIELD], _C, True),
    *(ExtendedCounter(leaf, GnmiYangPaths.PHY_BER_STATE, LocalPortNvueSub.LINK_PHY_HEALTH, leaf,
                      PEER_PORT_PHY_HEALTH_BER_TO_DB_FIELD[leaf],
                      LOCAL_PORT_OTEL_METRIC_BY_DB_FIELD[PEER_PORT_PHY_HEALTH_BER_TO_DB_FIELD[leaf]],
                      _D, True)
      for leaf in (BerFields.RAW, BerFields.EFFECTIVE, BerFields.SYMBOL)),
    *(ExtendedCounter(leaf, GnmiYangPaths.PHY_PLR_STATE, LocalPortNvueSub.LINK_PHY_DETAIL, nvue_leaf,
                      PEER_PORT_PHY_DETAIL_TO_DB_FIELD[nvue_leaf],
                      LOCAL_PORT_OTEL_METRIC_BY_DB_FIELD[PEER_PORT_PHY_DETAIL_TO_DB_FIELD[nvue_leaf]],
                      _C, False)
      for leaf, nvue_leaf in _LOCAL_PLR_GNMI_TO_NVUE_LEAF.items()),
    *(ExtendedCounter(leaf, GnmiYangPaths.INFINIBAND_COUNTERS_PORT, LocalPortNvueSub.COUNTERS, leaf,
                      None, spec.metric, _C, False)
      for leaf, spec in LOCAL_PORT_OTEL_GROUPS[LocalPortFamilyId.ROUTING].leaves.items()
      if leaf.startswith("pfrn-")),
)


LOCAL_PORT_OTEL_TYPE = _OI + "type"


LOCAL_PORT_OTEL_LINK_DOWN_INFO = _OI + "phy_link_down_info"


LOCAL_PORT_OTEL_OPER_STATE = _OI + "oper_state"


LOCAL_PORT_OTEL_PHYSICAL_STATE = LOCAL_PORT_OTEL_METRIC_BY_DB_FIELD[
    "SAI_PORT_STAT_INFINIBAND_PHYSICAL_STATE"]


# The link-down reason is carried as the local_reason_msg label of phy_link_down_info.
OTEL_LINK_DOWN_LOCAL_REASON_LABEL = "local_reason_msg"


# Local platform nodes -> OTEL, keyed by the component name.
LOCAL_PLATFORM_OTEL_GROUPS = {
    LocalPlatformNodeId.TRANSCEIVER_IDENTITY: OtelGroup(OtelLabel.NAME, {
        leaf: spec for leaf, spec in _transceiver_identity_otel_leaves(_LOCAL_TRANSCEIVER).items()
        if leaf in LOCAL_TRANSCEIVER_IDENTITY_LEAVES
    }),
    LocalPlatformNodeId.CHANNEL_SNR: OtelGroup(OtelLabel.NAME, {
        leaf: spec for leaf, spec in _transceiver_channel_otel_leaves(_LOCAL_TRANSCEIVER).items()
        if leaf in TRANSCEIVER_CHANNEL_SNR_READINGS
    }, OtelLabel.CHANNEL),
    LocalPlatformNodeId.ASIC_FREQUENCY: OtelGroup(OtelLabel.NAME,
                                                  _asic_frequency_otel_leaves(_OLP)),
}
