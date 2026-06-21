"""
Constants for the gNMI-for-IB plane-port and extended-telemetry test suite.

Only the symbols needed by the currently-merged test cases are defined here;
this module grows one test at a time as each plane-port case passes review.

Sources:
- HLD: "gNMI for Infiniband Functional Specification" v1.0 (31-03-2026)
- Test plan: "Verification Test Plan for gNMI for InfiniBand - Plane-Port
  Persistence, and Extended IB Telemetry" v1.0 (26-05-2026)
"""

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
    ALL = [EFFECTIVE, SYMBOL, RAW]


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
    SPECIAL_DIAG = "special_diag"
    SKIP = "skip"
    ZERO = "zero"


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

# Link-down reason codes (PRM PUDE table). The reason STRINGS are reused from
# CODE_TO_DESCRIPTION in test_ib_interface_phy_detail.py (Rowaida R7) rather than
# duplicated here, so this stays a thin code -> shared-lookup indirection.
LINK_DOWN_CODE_ADMIN_DISABLE = 22   # CODE_TO_DESCRIPTION: "Down_by_management_command"
LINK_DOWN_CODE_CABLE_UNPLUGGED = 23  # CODE_TO_DESCRIPTION: "Cable_was_unplugged"


# Baseline directory + files for the section 7.1 backward-compat snapshot.
BASELINE_DIR_NAME = "baselines"
APORT_SCHEMA_BASELINE_FILE = "aport_schema_baseline.json"
APORT_SCHEMA_BASELINE_NVUE_FILE = "aport_schema_baseline.nvue.json"
