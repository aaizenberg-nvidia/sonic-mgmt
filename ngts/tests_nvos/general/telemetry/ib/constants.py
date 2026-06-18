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
