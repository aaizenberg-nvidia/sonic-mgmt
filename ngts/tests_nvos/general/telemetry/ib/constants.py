"""
Constants for the gNMI-for-IB plane-port, peer-port (HCA), and extended-telemetry tests.

Sources:
- HLD: "gNMI for Infiniband Functional Specification" v1.0 (31-03-2026)
- Test plan: "Verification Test Plan for gNMI for InfiniBand - Plane-Port
  Persistence, and Extended IB Telemetry" v1.0 (26-05-2026)
"""

from collections import namedtuple
from enum import Enum

from ngts.tests_nvos.system.gnmi.constants import GnmiConstants


class PlanePortState(str, Enum):
    """NVUE plane-port knob states (HLD section 6.4)."""
    ENABLED = "enabled"
    DISABLED = "disabled"


class IfaceType:
    """Per-surface type-leaf mapping (NVOS schema, product-independent; test plan section 13.A)."""
    NVUE_APORT = "ib"
    NVUE_PLANE = "ibpp"
    GNMI_APORT = "infiniband"
    GNMI_PLANE = "infiniband-plane-port"


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
    _PEER_PREFIX = "peer-port/"
    PEER_PORT_INTERFACES = _PEER_PREFIX + INTERFACES
    PEER_PORT_BY_ID = _PEER_PREFIX + INTERFACES + "[name={pid}]"
    PEER_PORT_STATE = _PEER_PREFIX + STATE
    PEER_PORT_STATE_COUNTERS = _PEER_PREFIX + STATE_COUNTERS
    PEER_PORT_INFINIBAND_STATE = _PEER_PREFIX + INFINIBAND_STATE
    PEER_PORT_INFINIBAND_COUNTERS_PORT = _PEER_PREFIX + INFINIBAND_COUNTERS_PORT
    PEER_PORT_PHY_STATE = _PEER_PREFIX + PHY_STATE
    PEER_PORT_PHY_BER_STATE = _PEER_PREFIX + PHY_BER_STATE
    PEER_PORT_PHY_BER_MONITOR_STATE = _PEER_PREFIX + PHY_BER_MONITOR_STATE
    PEER_PORT_PHY_LINK_DOWN_INFO = _PEER_PREFIX + PHY_LINK_DOWN_INFO
    PEER_PORT_PHY_IB_PORT_STATISTICS_STATE = _PEER_PREFIX + PHY_IB_PORT_STATISTICS_STATE
    PEER_PORT_PHY_IB_PORT_ERRORS_STATE = _PEER_PREFIX + PHY_IB_PORT_ERRORS_STATE
    PEER_PORT_PHY_PLR_STATE = _PEER_PREFIX + PHY_PLR_STATE
    PEER_PORT_PHY_CHANNELS = _PEER_PREFIX + PHY_CHANNELS
    PEER_PORT_PHY_RS_HISTOGRAM = _PEER_PREFIX + PHY_RS_HISTOGRAM
    PEER_PORT_COMPONENTS = _PEER_PREFIX + "components/component"
    PEER_PORT_COMPONENT_STATE = PEER_PORT_COMPONENTS + "[name={name}]/state"
    PEER_PORT_COMPONENT_ASIC_STATE = PEER_PORT_COMPONENTS + "[name={name}]/asic/state"
    PEER_PORT_COMPONENT_TRANSCEIVER_STATE = PEER_PORT_COMPONENTS + "[name={name}]/transceiver/state"


class NvuePaths:
    """NVUE CLI / OpenAPI surface for the plane-port feature knob (HLD section 6.4)."""
    SYSTEM_PLANE_PORT = "/system/plane-port"
    KEY_STATE = "state"
    STATE_ENABLED = PlanePortState.ENABLED.value
    STATE_DISABLED = PlanePortState.DISABLED.value


# API surfaces used by parametrized section 5 tests. OTEL routes through a helper
# that pytest.skip()s for now (decision recorded in the plan).
API_NVUE_CLI = "nvue_cli"
API_GNMIC = "gnmic"
API_OTEL = "otel"
ALL_APIS = [API_NVUE_CLI, API_GNMIC]  # Todo: OTEL is not supported yet

# Sentinel marker for the OTEL skip path.
OTEL_PENDING_MSG = (
    "OTEL exposure pending - HLD test plan section 4 'Commands Reference' marks OTEL as TBD"
)

# Number of enable/disable cycles for the section 5.2 step-8 stress check.
PLANE_PORT_TOGGLE_CYCLES = 5

# Settle time (seconds) after toggling the plane-port knob before reading gNMI
# state; the gNMI server takes a moment to pick up the new config.
PLANE_PORT_TOGGLE_SETTLE_SEC = 8

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


# Extended congestion / BER / PFRN counter leaves expected on both Aport and
# plane-port rows (test plan section 5.5 / section 6.3). Section 5.4 uses this
# only as a soft "still exposed alongside the legacy set" check; the extended-
# counter CR reuses this same list.
EXTENDED_COUNTER_FIELDS = [
    "xmit-wait",
    BerFields.RAW,
    BerFields.EFFECTIVE,
    BerFields.SYMBOL,
    "pfrn-events",
]

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


# gNMI uses octets, NVUE uses bytes; comparisons skip names a surface lacks.
PEER_PORT_ADDITIVE_FIELDS = [
    "in-pkts",
    "out-pkts",
    "in-octets",
    "out-octets",
    "in-bytes",
    "out-bytes",
]


# Additive peer-port counters as the COUNTERS_DB row spells them (raw SAI names).
PEER_PORT_DB_ADDITIVE_FIELDS = [
    "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_PKTS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT",
]


# Canonical (API) additive counter -> COUNTERS_DB SAI field (bytes/octets share).
PEER_PORT_API_TO_DB_FIELD = {
    "in-pkts": "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT",
    "out-pkts": "SAI_PORT_STAT_INFINIBAND_IF_OUT_PKTS_EXT",
    "in-octets": "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT",
    "out-octets": "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT",
    "in-bytes": "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT",
    "out-bytes": "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT",
}


# Peer-port resiliency services: peer-telemetry (consumer) and nmx-t-ib (source).
PEER_TELEMETRY_SERVICE = "peer-telemetry.service"


NMXT_IB_SERVICE = "nmx-t-ib.service"


NMXT_IB_CONTAINER = "nmx-t-ib.telemetry.telemetry"


# NMX-T for IB serves the HCA cross-connect set (xcset) as CSV over a Unix socket.
NMXT_XCSET_SOCKET = "/var/run/nmx-t/ib/telemetry.sock"


NMXT_XCSET_ENDPOINT = "/csv/xcset/ib_nvos"


NMXT_CONTROL_SOCKET = "/var/run/nmx-t/ib/control.sock"


# xcset CSV counter column -> COUNTERS_DB SAI field (xcset already uses SAI names).
NMXT_XCSET_TO_DB_FIELD = {
    "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT": "SAI_PORT_STAT_INFINIBAND_IF_IN_PKTS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_PKTS_EXT": "SAI_PORT_STAT_INFINIBAND_IF_OUT_PKTS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT": "SAI_PORT_STAT_INFINIBAND_IF_IN_OCTETS_EXT",
    "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT": "SAI_PORT_STAT_INFINIBAND_IF_OUT_OCTETS_EXT",
}


# Sampling cycle of gpu-telemetry; resiliency tests wait a small multiple of this.
PEER_TELEMETRY_SAMPLING_SEC = 30


# Settle time after toggling PREI error injection on a plane-port, before the
# BER counters reflect the change. BER is sampled by sym-mgr over several link
# cycles, so we give it a few seconds to propagate to the telemetry rows.
BER_INJECT_SETTLE_SEC = 10


class GnmiTypeKind:
    """Coarse value-type buckets used by the §7.2 typing check."""
    COUNTER = "counter"   # YANG counter type - unsigned, monotonic
    UINT = "uint"         # unsigned integer
    DECIMAL = "decimal"   # non-negative real (BER, loss-percent)
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
SchemaGroup = namedtuple("SchemaGroup", ["group_id", "prefix", "leaves", "pending"], defaults=(frozenset(),))


_C = GnmiTypeKind.COUNTER


_U = GnmiTypeKind.UINT


_D = GnmiTypeKind.DECIMAL


_S = GnmiTypeKind.STRING


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
ListSchemaGroup = namedtuple(
    "ListSchemaGroup", ["group_id", "list_prefix", "leaves", "require_when_oper_up"]
)


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
            "peer-component": _S,
        },
        # TODO(peer-port): node-description not yet exposed over gNMI (gNMI-gap bug);
        # peer-component is outside the phase1-003 reduced drop (dev-confirmed,
        # off-spreadsheet) - comes in the later Arch-agreed xPath drop.
        pending=frozenset({"node-description", "peer-component"}),
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
            "down-blame": _U,
            "time-to-link-up-msec": _U,
        },
        # TODO(peer-port): these 3 phy/state leaves not yet implemented in the build.
        pending=frozenset({"time-since-last-clear-min", "down-blame", "time-to-link-up-msec"}),
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
        {
            "last-window-raw-ber": _D,
            "last-window-eff-ber": _D,
            "last-window-symbol-ber": _D,
            "max-raw-ber": _D,
            "max-eff-ber": _D,
            "max-symbol-ber": _D,
            "min-raw-ber": _D,
            "min-eff-ber": _D,
            "min-symbol-ber": _D,
            "num-of-raw-ber-alarms": _C,
            "num-of-eff-ber-alarms": _C,
            "num-of-symbol-ber-alarms": _C,
        },
        # TODO(peer-port): phy/ber-monitor/state not yet exposed over gNMI (gNMI-gap bug).
        pending=frozenset({
            "last-window-raw-ber", "last-window-eff-ber", "last-window-symbol-ber",
            "max-raw-ber", "max-eff-ber", "max-symbol-ber",
            "min-raw-ber", "min-eff-ber", "min-symbol-ber",
            "num-of-raw-ber-alarms", "num-of-eff-ber-alarms", "num-of-symbol-ber-alarms",
        }),
    ),
    SchemaGroup(
        "peer-port-phy-link-down-information",
        GnmiYangPaths.PEER_PORT_PHY_LINK_DOWN_INFO,
        {
            "total-events": _C,
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
        },
        # TODO(peer-port): the whole components/component/state container is outside
        # the phase1-003 reduced drop (Joe spreadsheet: mfg-name/part-no/serial-no/
        # firmware-version = NOT SUPPORTED IN REDUCED DROP; name/type/hardware-version
        # off-spreadsheet but the container is empty over gNMI on the reduced drop) -
        # comes in the later Arch-agreed xPath drop; drop from `pending` once exposed.
        pending=frozenset({
            "name", "type", "mfg-name", "part-no", "serial-no",
            "hardware-version", "firmware-version",
        }),
    ),
    SchemaGroup(
        "peer-port-component-asic-state",
        GnmiYangPaths.PEER_PORT_COMPONENT_ASIC_STATE,
        {
            "asic-temp": _D,
            "measured-freq-0": _U,
            "measured-freq-1": _U,
            "min-freq-0": _U,
            "min-freq-1": _U,
            "max-freq-0": _U,
            "max-freq-1": _U,
            "max-delta-freq-0": _U,
            "max-delta-freq-1": _U,
        },
        # TODO(peer-port): components/asic/state not yet exposed over gNMI (gNMI-gap bug).
        pending=frozenset({
            "asic-temp",
            "measured-freq-0", "measured-freq-1",
            "min-freq-0", "min-freq-1",
            "max-freq-0", "max-freq-1",
            "max-delta-freq-0", "max-delta-freq-1",
        }),
    ),
    SchemaGroup(
        "peer-port-component-transceiver",
        GnmiYangPaths.PEER_PORT_COMPONENT_TRANSCEIVER_STATE,
        {
            "vendor": _S,
            "vendor-part": _S,
            "vendor-rev": _S,
        },
        # TODO(peer-port): the whole transceiver subtree is outside the phase1-003
        # reduced drop (Joe spreadsheet: vendor-rev = NOT SUPPORTED IN REDUCED DROP, and the
        # entire transceiver container is dropped, so vendor/vendor-part go too) -
        # comes in the later Arch-agreed xPath drop; drop from `pending` once exposed.
        pending=frozenset({"vendor", "vendor-part", "vendor-rev"}),
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
]
