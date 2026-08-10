from enum import Enum, StrEnum


class InternalNvosConsts:
    # Output dictionary
    OPERATIONAL_INDEX = 0
    APPLIED_INDEX = 1
    DEFAULT_TIMEOUT = 120  # Non-ACP ports (FNM/SW)
    NVL5_ACP_LINK_UP_TIMEOUT_LTX_ENABLED = 60  # NVL5 ACP ports with LTX enabled
    NVL5_ACP_LINK_UP_TIMEOUT_LTX_DISABLED = 30  # NVL5 ACP ports with LTX (fec-measure-mode) disabled
    NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED = 375  # NVL6 ACP ports with fec-measure-mode enabled
    NVL6_ACP_LINK_UP_TIMEOUT_LTX_DISABLED = 90  # NVL6 ACP ports with fec-measure-mode disabled
    # TODO: interim value - update after verifying link-up time on a real Portia (NVL7) system
    NVL7_SW_LINK_UP_TIMEOUT = 2 * 60  # NVL7 (Portia) CPO sw ports
    ACP_PORT_GOES_UP = "acp port goes up"
    IB_TRAFFIC_SENDER_INTERFACE = "h1p1"
    IB_TRAFFIC_RECEIVER_INTERFACE = "h2p1"
    IB_TRAFFIC_LAT_TYPE = "ib_send_lat"
    IB_TRAFFIC_IPOIB_TYPE = "ping_over_ib"


class NvosConsts:
    LINK_STATE_UP = "up"
    LINK_STATE_DOWN = "down"
    LINK_STATE_ALL_TYPES = [LINK_STATE_UP, LINK_STATE_DOWN]
    LINK_LOG_STATE_ACTIVE = "Active"
    LINK_LOG_STATE_INITIALIZE = "Initialize"


class NvlInterfaceConsts:
    NVL_PORT_TYPE = "nvl"
    ACP_PORT_TYPE = "acp"
    SW_INTERFACE_TYPE = "sw"  # trunk/switch ports (interface_type for select_random_port)
    TRUNK_PORT_TYPE = "trunk"  # port type label (trunk vs access)
    ACCESS_PORT_TYPE = "access"


class InterfaceConsts:
    DESCRIPTION = "description"


class IbInterfaceConsts:
    INTERFACE_NAME = "name"
    DESCRIPTION = "description"
    ARPTIMEOUT = "arp-timeout"
    AUTOCONFIG = "autoconf"
    DHCP_STATE = "state"
    INTERFACE_STATE = "interface-state"
    UP_ONCE = "up-once"
    DHCP_SET_HOSTNAME = "set-hostname"
    TYPE = "type"
    LINK = "link"
    PHY_DIAG = "phy-diag"
    PHY_DETAIL = "phy-detail"
    NVL = "nvl"
    IP = "ip"
    IPV4 = "ipv4"
    IPV6 = "ipv6"
    VRF = "vrf"
    LLDP = "lldp"
    IFINDEX = "ifindex"
    LINK_LOGICAL_PORT_STATE = "logical-state"
    LINK_LOGICAL_PORT_STATE_ACTIVE = "Active"
    LINK_LOGICAL_PORT_STATE_INITIALIZE = "Initialize"
    LINK_LOGICAL_PORT_STATE_UP = "Up"
    LINK_LOGICAL_PORT_STATE_DOWN = "Down"
    LINK_PHYSICAL_PORT_STATE = "physical-state"
    LINK_PHYSICAL_PORT_STATE_LINK_UP = "LinkUp"
    LINK_PHYSICAL_PORT_STATE_DISABLED = "Disabled"
    LINK_PHYSICAL_PORT_STATE_POLLING = "Polling"
    LINK_PHYSICAL_PORT_STATE_CONFIGURATION_TRAINING = "PortConfigurationTraining"
    LINK_ADMIN_STATUS = "admin-status"
    LINK_OPER_STATUS = "oper-status"
    LINK_STATE = "state"
    LINK_CONNECTION_MODE = "connection-mode"
    XDR = "xdr"
    NDR = "ndr"
    SDR = "sdr"
    LINK_AUTO_NEG_ON = "enabled"
    LINK_AUTO_NEG_OFF = "disabled"
    LINK_DIAGNOSTICS = "diagnostics"
    LINK_DIAGNOSTICS_UNPLUGGED_PORT = {"1024": {"status": "Cable is unplugged"}}
    LINK_DIAGNOSTICS_CLOSED_BY_COMMAND_PORT = {"1": {"status": "Closed by command"}}
    LINK_DIAGNOSTICS_WITHOUT_ISSUE_PORT = {"0": {"status": "No issue was observed"}}
    LINK_DIAGNOSTICS_NEGOTIATION_FAILURE_PORT = {"2": {"status": "Negotiation failure"}}
    LINK_DIAGNOSTICS_SIGNAL_NOT_DETECTED = {"57": {"status": "signal not detected"}}
    LINK_BREAKOUT = "breakout"
    LINK_STATS_LINK_DOWNED = "carrier-down-count"
    LINK_IB_SPEED = "ib-speed"
    LINK_SUPPORTED_IB_SPEEDS = "supported-ib-speed"
    LINK_SPEED = "speed"
    LINK_MAC = "mac"
    LINK_DUPLEX = "duplex"
    LINK_AUTO_NEGOTIATE = "auto-negotiate"
    LINK_SUPPORTED_SPEEDS = "supported-speed"
    LINK_SUPPORTED_LANES = "supported-lanes"
    LINK_LANES = "lanes"
    LINK_MAX_SUPPORTED_MTU = "max-supported-mtu"
    LINK_MTU = "mtu"
    LINK_VL_ADMIN_CAPABILITIES = "vl-capabilities"
    LINK_OPERATIONAL_VLS = "op-vls"
    LINK_TO_LINK_UP = "time-to-link-up"
    LINK_IB_SUBNET = "ib-subnet"
    LINK_STATS = "counters"
    LINK_STATS_CARRIER_TRANSITION = "carrier-transitions"
    LINK_STATS_IN_BYTES = "in-bytes"
    LINK_STATS_IN_DROPS = "in-drops"
    LINK_STATS_IN_ERRORS = "in-errors"

    # IB counters sub-options
    COUNTERS_ERRORS = "errors"
    COUNTERS_DROPS = "drops"
    COUNTERS_FAST_RECOVERY = "fast-recovery"

    LINK_STATS_IN_SYMBOL_ERRORS = "in-symbol-errors"
    LINK_STATS_IN_PKTS = "in-pkts"
    LINK_STATS_UNICAST_IN_PKTS = "unicast-in-pkts"
    LINK_STATS_MULTICAST_IN_PKTS = "multicast-in-pkts"
    LINK_STATS_OUT_BYTES = "out-bytes"
    LINK_STATS_OUT_DROPS = "out-drops"
    LINK_STATS_OUT_ERRORS = "out-errors"
    LINK_STATS_OUT_PKTS = "out-pkts"
    LINK_STATS_UNICAST_OUT_PKTS = "unicast-out-pkts"
    LINK_STATS_MULTICAST_OUT_PKTS = "multicast-out-pkts"
    LINK_STATS_OUT_WAIT = "out-wait"
    MAX_BYTE_COUNTER_AFTER_CLEAR = 4000
    MAX_PKT_COUNTER_AFTER_CLEAR = 15
    LINK_STATS_RCV_ICRC_ERRORS = "rcv-icrc-errors"
    LINK_STATS_TX_PARITY_ERRORS = "tx-parity-errors"
    LINK_PLR_RCV_CODES_ERRORS = "plr-rcv-codes-err"
    # Full QTM3 counter fields list (used for indexed access in IbDevice.py)
    LINK_STATS_QNT3 = [
        "link-error-recovery",
        "link-downed",
        "port-rcv-remote-physical-errors",
        "port-rcv-switch-relay-errors",
        "port-rcv-constraint-errors",
        "local-link-integrity-errors",
        "qp1-drops",
        "buffer-overrun-errors",
        LINK_STATS_RCV_ICRC_ERRORS,
        LINK_STATS_TX_PARITY_ERRORS,
    ]

    # QTM3 fields split by location in the counters JSON structure (for validation):
    # Fields at top level of counters output
    LINK_STATS_QNT3_TOP_LEVEL = ["buffer-overrun-errors"]
    # Fields under 'link' dictionary (note: no 'link-' prefix in actual JSON)
    LINK_STATS_QNT3_UNDER_LINK = [
        "error-recovery",
        "port-rcv-remote-physical-errors",
        "port-rcv-switch-relay-errors",
        "port-rcv-constraint-errors",
        "local-integrity-errors",
    ]
    LINK_PHY_RAW_ERRORS = [
        "phy-raw-errors-lane0",
        "phy-raw-errors-lane1",
        "phy-raw-errors-lane2",
        "phy-raw-errors-lane3",
        "phy-raw-errors-lane4",
        "phy-raw-errors-lane5",
        "phy-raw-errors-lane6",
        "phy-raw-errors-lane7",
    ]
    LINK_PHY_RAW_BER = [
        "raw-ber-lane0",
        "raw-ber-lane1",
        "raw-ber-lane2",
        "raw-ber-lane3",
        "raw-ber-lane4",
        "raw-ber-lane5",
        "raw-ber-lane6",
        "raw-ber-lane7",
    ]
    LINK_BREAKOUT_NDR = "2x-ndr"
    LINK_BREAKOUT_HDR = "2x-hdr"
    LINK_BREAKOUT_XDR = "2x-xdr"
    LINK_ROUND_TRIP_LATENCY = "round-trip-latency"
    ASIC = "asic"
    PRIMARY_ASIC = "primary-asic"
    PRIMARY_ASIC_DEVICE = "primary-asic-device"
    LOCAL_PORT = "local-port"
    IP_VRF = "vrf"
    IP_ADDRESS = "address"
    IP_GATEWAY = "gateway"
    IP_DHCP = "dhcp-client"
    IP_DHCP6 = "dhcp-client6"
    NAME = "name"
    IB_PORT_TYPE = "ib"
    FNM_PORT_TYPE = "fnm"
    LOOPBACK_PORT_TYPE = "loopback"
    ETH_PORT_TYPE = "eth"
    MTU_VALUES = [256, 512, 1024, 2048, 4096]
    DEFAULT_MTU = 4096
    XDR_SLOW_SPEED = "200G"
    SPEED_LIST = {"xdr": "800G", "ndr": "400G", "hdr": "200G", "edr": "100G", "fdr": "56G", "qdr": "40G", "sdr": "10G"}
    SUPPORTED_LANES = ["1X", "1X,2X", "1X,4X", "1X,2X,4X"]
    DEFAULT_LANES = "1X,2X,4X"
    SPLIT_PORT_DEFAULT_LANES = "4X"
    SPLIT_PORT_CHILD_DEFAULT_LANES = "2X"
    SPLIT_PORT_DEFAULT_MTU = 4096
    SPLIT_PORT_DEFAULT_VLS = "VL0-VL1"
    SUPPORTED_VLS = ["VL0", "VL0-VL1", "VL0-VL3", "VL0-VL7"]
    DEFAULT_VLS = "VL0-VL7"
    IB0_LINK_MTU_DEFAULT_VALUE = 2044
    IB0_IP_ARP_DEFAULT_VALUE = 1800
    IB0_IP_AUTOCONF_DEFAULT_VALUE = "disabled"
    IB0_DHCP_STATE_DEFAULT_VALUE = "disabled"
    MAX_COUNTERS_AFTER_CLEAR = 700
    PLANARIZED_PORTS = "planarized-ports"
    PC_VL15_DROPPED_F = "SAI_PORT_STAT_INFINIBAND_PC_VL15_DROPPED_F"
    RCV_DISCARD_EXTERNAL_CONTAIN = "SAI_PORT_STAT_INFINIBAND_RCV_DISCARD_EXTERNAL_CONTAIN"
    TOTAL_IN_DROPS = "PORT_STAT_INFINIBAND_TOTAL_IN_DROPS"
    PC_XMT_DISCARDS_F = "SAI_PORT_STAT_INFINIBAND_PC_XMT_DISCARDS_F"
    XMT_DISCARD_EXTERNAL_CONTAIN = "SAI_PORT_STAT_INFINIBAND_XMT_DISCARD_EXTERNAL_CONTAIN"
    TOTAL_OUT_DROPS = "PORT_STAT_INFINIBAND_TOTAL_OUT_DROPS"
    DEFAULT_SWID = "infiniband-default"

    # IB counters expected fields list (defined at end to reference constants defined above)
    IB_COUNTERS_EXPECTED_OPTIONS = [COUNTERS_ERRORS, COUNTERS_DROPS, COUNTERS_FAST_RECOVERY]
    NVL_COUNTERS_FIELDS = [LINK, NVL]
    NVL_COUNTERS_LINK_FIELDS = [COUNTERS_ERRORS, COUNTERS_DROPS]

    # Non-IB interfaces forbidden options (should not have these sub-options under counters)
    # Note: 'nvl' is from NvlInterfaceConsts.NVL_PORT_TYPE, included as string to avoid cross-class reference
    NON_IB_COUNTERS_FORBIDDEN_OPTIONS = [IB_PORT_TYPE, "nvl", LINK]


class AutoNegotiateConsts:
    class State(Enum):
        ENABLED = "enabled"
        DISABLED = "disabled"


class MloopConsts:
    """FAE per-interface mloop knob: nv set fae interface <port> link mloop <mode>."""

    class Mode(StrEnum):
        PHY = "phy"  # PHY-level loopback (was system-wide "enabled")
        LOGICAL = "logical"  # LLU2LLU loopback
        DISABLED = "disabled"


class PhyRecoveryConsts:
    STEP_1 = "step-1"
    STEP_2 = "step-2"

    ENABLED = "enabled"
    DISABLED = "disabled"
    FW_DEFAULT = "fw-default"
    AUTO = "auto"
    FULL_DUPLEX = "full-duplex"

    # NVL6 attributes
    LINK_DOWN_TIMEOUT = "link-down-timeout"
    RECOVERY_SUPPORTED = "recovery-supported"
    RECOVERY_STATUS = "recovery-status"
    RECOVERY_NEGATIVE_TYPE = "recovery-neg-type"
    RECOVERY_NEG_TYPE_FORCE_PEER = "force-peer"
    RECOVERY_ENTRY_REASON = "recovery-entry-reason"
    PRESENT_MODE = "preset-mode"
    PEQ_NUMBER_OF_RETRY_PRESET1 = "peq-number-of-retry-preset1"
    PEQ_NUMBER_OF_RETRY_PRESET2 = "peq-number-of-retry-preset2"
    PEQ_NUMBER_OF_RETRY_PRESET3 = "peq-number-of-retry-preset3"
    STATE_60_TIMEOUT = "state-60-timeout"
    STATE_61_TIMEOUT = "state-61-timeout"
    STATE_62_TIMEOUT = "state-62-timeout"
    STATE_65_TO_66_TIME_PRESET1 = "state-65-to-66-time-preset1"
    STATE_65_TO_66_TIME_PRESET2 = "state-65-to-66-time-preset2"
    STATE_65_TO_66_TIME_PRESET3 = "state-65-to-66-time-preset3"
    STATE_66_TO_67_TIME_PRESET1 = "state-66-to-67-time-preset1"
    STATE_66_TO_67_TIME_PRESET2 = "state-66-to-67-time-preset2"
    STATE_66_TO_67_TIME_PRESET3 = "state-66-to-67-time-preset3"
    STATE_67_TO_68_TIME_PRESET1 = "state-67-to-68-time-preset1"
    STATE_67_TO_68_TIME_PRESET2 = "state-67-to-68-time-preset2"
    STATE_67_TO_68_TIME_PRESET3 = "state-67-to-68-time-preset3"
    STATE_60_TO_LINKUP_TIMEOUT = "state-60-to-linkup-timeout"
    UNINTENTIONAL_LINK_DOWN_EVENTS = "unintentional-link-down-events"
    INTENTIONAL_LINK_DOWN_EVENTS = "intentional-link-down-events"
    TOTAL_SUCCESSFUL_RECOVERY_EVENTS = "total-successful-recovery-events"
    SUCCESSFUL_RECOVERY_EVENTS = "successful-recovery-events"
    TIME_IN_LAST_LOGIC_RECOVERY_EVENT = "time-in-last-logic-recovery-event"
    TIME_IN_LAST_SERDES_EQ_RECOVERY_EVENT = "time-in-last-serdes-eq-recovery-event"
    TIME_SINCE_LAST_RECOVERY = "time-since-last-recovery"
    LAST_LOGIC_RECOVERY_ATTEMPTS = "last-logic-recovery-attempts"
    LAST_SERDES_EQ_RECOVERY_ATTEMPTS = "last-serdes-eq-recovery-attempts"
    TIME_BETWEEN_LAST_TWO_RECOVERIES = "time-between-last-two-recoveries"
    LAST_RS_FEC_UNCORRECTABLE_DURING_RECOVERY = "last-rs-fec-uncorrectable-during-recovery"
    TOTAL_RS_FEC_UNCORRECTABLE_DURING_RECOVERY = "total-rs-fec-uncorrectable-during-recovery"
    LAST_SUCCESSFUL_RECOVERY_TIME = "last-successful-recovery-time"
    TOTAL_SUCCESSFUL_RECOVERY_TIME = "total-successful-recovery-time"
    LAST_SUCCESSFUL_RECOVERY_STEP_ATTEMPTS = "last-successful-recovery-step-attempts"

    immutable_attributes = [RECOVERY_SUPPORTED, RECOVERY_ENTRY_REASON]

    phy_recovery_attributes_options = {
        LINK_DOWN_TIMEOUT: list(range(0, 65536)),
        RECOVERY_STATUS: [ENABLED, DISABLED],
        RECOVERY_NEGATIVE_TYPE: ["auto", "force-peer", "ignore-negotiation"],
        PRESENT_MODE: ["auto", "peq-only", "cdr-toggle", "full-duplex", "logic-lock-only", "skip-step"],
        PEQ_NUMBER_OF_RETRY_PRESET1: list(range(0, 32)),
        PEQ_NUMBER_OF_RETRY_PRESET2: list(range(0, 32)),
        PEQ_NUMBER_OF_RETRY_PRESET3: list(range(0, 32)),
        STATE_60_TIMEOUT: list(range(0, 65536)),
        STATE_61_TIMEOUT: list(range(0, 65536)),
        STATE_62_TIMEOUT: list(range(0, 65536)),
        STATE_65_TO_66_TIME_PRESET1: list(range(0, 65536)),
        STATE_65_TO_66_TIME_PRESET2: list(range(0, 65536)),
        STATE_65_TO_66_TIME_PRESET3: list(range(0, 65536)),
        STATE_66_TO_67_TIME_PRESET1: list(range(0, 65536)),
        STATE_66_TO_67_TIME_PRESET2: list(range(0, 65536)),
        STATE_66_TO_67_TIME_PRESET3: list(range(0, 65536)),
        STATE_67_TO_68_TIME_PRESET1: list(range(0, 65536)),
        STATE_67_TO_68_TIME_PRESET2: list(range(0, 65536)),
        STATE_67_TO_68_TIME_PRESET3: list(range(0, 65536)),
        STATE_60_TO_LINKUP_TIMEOUT: list(range(0, 65536)),
    }

    phy_recovery_mutable_attributes = [
        PEQ_NUMBER_OF_RETRY_PRESET3,
        STATE_60_TIMEOUT,
        STATE_61_TIMEOUT,
        STATE_62_TIMEOUT,
        STATE_65_TO_66_TIME_PRESET3,
        STATE_66_TO_67_TIME_PRESET3,
        STATE_67_TO_68_TIME_PRESET3,
    ]

    class SerdesEQMode(Enum):
        ENABLED = "enabled"
        DISABLED = "disabled"
        FW_DEFAULT = "fw-default"

    class LogicRelockMode(Enum):
        ENABLED = "enabled"
        DISABLED = "disabled"
        FW_DEFAULT = "fw-default"

    class SerdesEQ:
        MODE = "serdes-eq-mode"
        TIMEOUT = "serdes-eq-timeout"
        SETTING_PREFIX = "serdes-eq"

    class LogicRelock:
        MODE = "logic-relock-mode"
        TIMEOUT = "logic-relock-timeout"
        SETTING_PREFIX = "logic-relock"

    DEFAULT_PHY_RECOVERY_DICT = {
        SerdesEQ.MODE: SerdesEQMode.DISABLED.value,
        SerdesEQ.TIMEOUT: str(0),
    }

    NVL5_MODES = [ENABLED, DISABLED, FW_DEFAULT]
    # NVL7 CPO trunk ports reuse NVL6_MODES as-is (the NVL6 recovery-status / link-down-timeout
    # mode+timeout semantics are inherited, see HLD "Subsystem Requirements Overview").
    NVL6_MODES = [ENABLED, DISABLED, AUTO]

    # ===================================================================================
    # NVL7 CPO trunk-port PHY recovery (extends NVL5/NVL6 phy-recovery)
    # HLD: HLD_NVOS_CPO_PHY_RECOVERY_NVL7 - "NVUE CLI - New configuration leaves" (Table 4)
    # These 5 leaves + the "counters debug" node exist ONLY on NVL7 CPO trunk ports (sw*),
    # and are pruned (absent/not settable) on access ports (acp*) and on NVL5/NVL6/non-NVLink.
    # ===================================================================================
    RECOVERY_POLICY_CONFIG = 'recovery-policy-config'
    RECOVERY_TX_RE_ITERATION = 'recovery-tx-re-iteration'
    RECOVERY_TX_TOGGLE_DELAY = 'recovery-tx-toggle-delay'
    RECOVERY_TX_TOGGLE_TIME = 'recovery-tx-toggle-time'
    RECOVERY_RX_ALGO_TIME = 'recovery-rx-algo-time'

    class RecoveryPolicyConfig:
        """Values for the ``recovery-policy-config`` leaf (HLD Table 4)."""
        FW_DEFAULT = 'fw-default'
        EXPLICIT_CONFIG_CONTROLS = 'explicit-config-controls'
        RECOVERY_OFF = 'recovery-off'
        ROBUST_OPTIMIZED = 'robust-optimized'
        GRADUAL_RECOVERY = 'gradual-recovery'

        ALL = [FW_DEFAULT, EXPLICIT_CONFIG_CONTROLS, RECOVERY_OFF, ROBUST_OPTIMIZED, GRADUAL_RECOVERY]

    class RecoveryTxReIteration:
        """Values for the ``recovery-tx-re-iteration`` leaf (HLD Table 4)."""
        FW_DEFAULT = 'fw-default'
        ENABLED = 'enabled'
        DISABLED = 'disabled'

        ALL = [FW_DEFAULT, ENABLED, DISABLED]

    # Numeric leaves accept int msec 0..255 (HLD Table 4).
    # Per dev (2026-07-17): NVL7 show echoes the CONFIGURED value LITERALLY (timer 0 shows as 0,
    # fw-default shows as "fw-default"); there is NO FW-resolved display (that HLD line was stale).
    NVL7_TIMER_MIN = 0
    NVL7_TIMER_MAX = 255
    NVL7_TIMER_LEAVES = [RECOVERY_TX_TOGGLE_DELAY, RECOVERY_TX_TOGGLE_TIME, RECOVERY_RX_ALGO_TIME]

    # The 5 new NVL7 CPO leaves (order matches HLD Table 4).
    NVL7_NEW_LEAVES = [
        RECOVERY_POLICY_CONFIG,
        RECOVERY_TX_TOGGLE_DELAY,
        RECOVERY_TX_TOGGLE_TIME,
        RECOVERY_RX_ALGO_TIME,
        RECOVERY_TX_RE_ITERATION,
    ]

    # Literal default show values for the 5 new leaves (confirmed by dev 2026-07-17).
    NVL7_NEW_LEAF_DEFAULTS = {
        RECOVERY_POLICY_CONFIG: RecoveryPolicyConfig.FW_DEFAULT,
        RECOVERY_TX_RE_ITERATION: RecoveryTxReIteration.FW_DEFAULT,
        RECOVERY_TX_TOGGLE_DELAY: 0,
        RECOVERY_TX_TOGGLE_TIME: 0,
        RECOVERY_RX_ALGO_TIME: 0,
    }

    # New FAE-only debug step-counters (read-only).
    # HLD: "counters debug leaf" column (Table 7) / object model "+--ro debug".
    # nv show fae interface <intf> counters debug
    TIME_IN_LAST_STEP_1 = 'time-in-last-step-1'
    TIME_IN_LAST_STEP_2 = 'time-in-last-step-2'
    TOTAL_TIME_IN_STEP_1 = 'total-time-in-step-1'
    TOTAL_TIME_IN_STEP_2 = 'total-time-in-step-2'
    TOTAL_STEP_1_COUNT = 'total-step-1-count'
    TOTAL_STEP_2_COUNT = 'total-step-2-count'

    NVL7_DEBUG_COUNTERS = [
        TIME_IN_LAST_STEP_1,
        TIME_IN_LAST_STEP_2,
        TOTAL_TIME_IN_STEP_1,
        TOTAL_TIME_IN_STEP_2,
        TOTAL_STEP_1_COUNT,
        TOTAL_STEP_2_COUNT,
    ]
    # Debug step-counters are all 0 before any recovery has been triggered / after clear.
    NVL7_DEFAULT_DEBUG_COUNTERS = {counter: 0 for counter in NVL7_DEBUG_COUNTERS}

    # ``counters debug`` node path (for building the FAE-only counters debug command).
    COUNTERS_DEBUG = 'debug'

    # ---- FAE command prefixes (for device unsupported_commands_list pruning entries) ----
    # NOTE: {port} is a literal template filled later by the device layer, so it is escaped
    # as {{port}} inside the f-string comprehension (class-body comprehensions cannot read
    # sibling class attributes in their body, hence the prefix is inlined).
    SHOW_FAE_COUNTERS_DEBUG_COMMAND = "nv show fae interface {port} counters debug"

    # Set commands for the 5 new leaves - added to unsupported_commands_list on
    # NVL5 / NVL6 / IB so pruning tests and bad-flow gating skip them there.
    # Each carries a VALID sample value: a value-less set errors on ANY device as merely
    # incomplete, so only a valued set (accepted where exposed, rejected where pruned)
    # actually proves the leaf is pruned.
    NVL7_NEW_LEAF_SAMPLE_VALUES = {
        RECOVERY_POLICY_CONFIG: RecoveryPolicyConfig.ROBUST_OPTIMIZED,
        RECOVERY_TX_TOGGLE_DELAY: 1,
        RECOVERY_TX_TOGGLE_TIME: 1,
        RECOVERY_RX_ALGO_TIME: 1,
        RECOVERY_TX_RE_ITERATION: RecoveryTxReIteration.ENABLED,
    }
    NVL7_NEW_LEAF_SET_COMMANDS = [
        f"nv set fae interface {{port}} link phy-recovery {leaf} {value}"
        for leaf, value in NVL7_NEW_LEAF_SAMPLE_VALUES.items()
    ]
    # Full pruning command list (5 set-leaf commands + counters debug show) for off-NVL7 devices.
    NVL7_PRUNED_COMMANDS = NVL7_NEW_LEAF_SET_COMMANDS + [SHOW_FAE_COUNTERS_DEBUG_COMMAND]

    # ---- NVL7 new-leaf bad-flow / pruning expected-error fragments (any-of match) ----
    # Only schema-shaped fragments (avoid over-broad bare words like 'Error'/'Invalid' that match
    # almost anything). Tentative wording per dev (2026-07-17), Omer to post the exact strings:
    #   NVUE   -> "Invalid config" + an unsupported-parameter mention
    #   OpenAPI-> HTTP 422 "is not valid under any of the given schemas"
    # Keep these any-of fragments until Omer confirms, then tighten.
    NVL7_ERR_ENUM_INVALID = ["is not one of"]
    NVL7_ERR_NUMERIC_OVER_MAX = ["Valid range", "greater than the maximum"]
    NVL7_ERR_NUMERIC_NEGATIVE = ["Valid range", "less than the minimum"]
    NVL7_ERR_NUMERIC_NON_NUMERIC = ["is not of type", "is not one of"]
    NVL7_ERR_PRUNED_SET = ["is not one of", "does not exist"]


class TxBwLossMonitorConsts:
    """Constants for tx-bandwidth-loss-monitor (zombie link) feature."""

    # Field names in show output
    STATE: str = "state"
    MONITOR_STATUS: str = "monitor-status"

    class State(Enum):
        ENABLED: str = "enabled"
        DISABLED: str = "disabled"
        FW_DEFAULT: str = "fw-default"

        @classmethod
        def all(cls):
            return [member.value for member in cls]

    class MonitorStatus(Enum):
        NORMAL: str = "normal"
        NA: str = "N/A"
        WARNING: str = "warning"
        ALARM: str = "alarm"

        @classmethod
        def all(cls):
            return [member.value for member in cls]

    # Default show values (port up, no config applied)
    DEFAULT_OPER_STATE: str = State.ENABLED.value
    DEFAULT_APPLIED_STATE: str = State.FW_DEFAULT.value
    DEFAULT_MONITOR_STATUS: str = MonitorStatus.NORMAL.value

    # Link-down diagnostics opcode for BW-loss threshold exceeded
    BW_LOSS_DIAG_CODE: str = "45"
    BW_LOSS_DIAG_STATUS: str = "BW_loss_threshold_exceeded"
    # Same opcode as it appears in NVOS ``nv show interface <p> link phy detail``
    # ``linkdown-reason-status-local`` field (uppercase-underscored).
    NVOS_LINKDOWN_STATUS_BW_LOSS: str = "BW_LOSS_THRESHOLD_EXCEEDED"

    # NVOS phy-detail fields used by the injection test verification.
    NVOS_PHY_DETAIL_LINKDOWN_CODE_LOCAL: str = "linkdown-reason-code-local"
    NVOS_PHY_DETAIL_LINKDOWN_STATUS_LOCAL: str = "linkdown-reason-status-local"
    NVOS_PHY_DETAIL_UNINTENTIONAL_LINK_DOWN: str = "unintentional-link-down-events"
    # ``link phy detail`` returns per-plane values joined with this separator
    # (e.g. ``"45/23/23/23"``). First slot = most-recent link-down event.
    NVOS_PHY_DETAIL_PLANE_SPLITTER: str = "/"

    # Expected error fragment for invalid state input
    ERR_MSG_INVALID_STATE: str = "is not one of"


class FWRecoveryConsts:
    # Define constants for recovery event fields
    TOTAL_SUCCESSFUL_RECOVERY_EVENTS = "total-successful-recovery-events"
    TIME_IN_LAST_LOGIC_RECOVERY_EVENT = "time-in-last-logic-recovery-event"
    TIME_IN_LAST_SERDES_EQ_RECOVERY_EVENT = "time-in-last-serdes-eq-recovery-event"
    TIME_SINCE_LAST_RECOVERY = "time-since-last-recovery"
    LAST_LOGIC_RECOVERY_ATTEMPTS = "last-logic-recovery-attempts"
    LAST_SERDES_EQ_RECOVERY_ATTEMPTS = "last-serdes-eq-recovery-attempts"
    TIME_BETWEEN_LAST_TWO_RECOVERIES = "time-between-last-two-recoveries"

    ENABLED = "enabled"
    DISABLED = "disabled"
    FW_DEFAULT = "fw-default"

    # Default expected values
    DEFAULT_FW_RECOVERY_COUNTERS = {
        TOTAL_SUCCESSFUL_RECOVERY_EVENTS: 0,
        TIME_IN_LAST_LOGIC_RECOVERY_EVENT: 0,
        TIME_IN_LAST_SERDES_EQ_RECOVERY_EVENT: 0,
        TIME_SINCE_LAST_RECOVERY: 0,
        LAST_LOGIC_RECOVERY_ATTEMPTS: 0,
        LAST_SERDES_EQ_RECOVERY_ATTEMPTS: 0,
        TIME_BETWEEN_LAST_TWO_RECOVERIES: 0,
    }

    class SerdesEQMode(Enum):
        ENABLED = "enabled"
        DISABLED = "disabled"
        FW_DEFAULT = "fw-default"

    class LogicRelockMode(Enum):
        ENABLED = "enabled"
        DISABLED = "disabled"
        FW_DEFAULT = "fw-default"

    class SerdesEQ:
        MODE = "serdes-eq-mode"
        TIMEOUT = "serdes-eq-timeout"
        SETTING_PREFIX = "serdes-eq"

    class LogicRelock:
        MODE = "logic-relock-mode"
        TIMEOUT = "logic-relock-timeout"
        SETTING_PREFIX = "logic-relock"

    DEFAULT_PHY_RECOVERY_DICT = {
        SerdesEQ.MODE: SerdesEQMode.ENABLED.value,
        SerdesEQ.TIMEOUT: str(100),
    }

    MODES = [SerdesEQMode.DISABLED.value, SerdesEQMode.FW_DEFAULT.value, SerdesEQMode.ENABLED.value]

    # Go once action constants
    GO_ONCE_INVALID_PORT_TYPES = ["eth0", "eth1", "fnm1", "lo"]
    GO_ONCE_LOG_MESSAGE = "Got phy recovery event for port"


class PhyDiagConsts:
    """Constants for the phy team's ``phy_diag.py`` register-access tool.

    Used by :class:`ngts.nvos_tools.infra.PhyDiagTool.PhyDiagTool` to drive
    BW-loss-monitor injection (PPBMP + PTER) on Mellanox switches.
    """

    # Canonical NFS source path on the sonic-mgmt host. Not mounted on NVOS
    # DUTs, so :meth:`PhyDiagTool.ensure_deployed` SCPs the package from
    # here to the DUT before the test runs.
    PHY_DIAG_SOURCE_DIR: str = "/auto/mswg/release/fwshared/phy/utils/phy_tools/phy_tools_last_stable/phy_package"
    # Deployed location on the DUT (where the SCP push lands). The phy_diag
    # entry-point lives under ``phy_diag/phy_diag.py`` inside the package.
    PHY_DIAG_DUT_DIR: str = "/tmp/phy_package"
    PHY_DIAG_BIN: str = f"{PHY_DIAG_DUT_DIR}/phy_diag/phy_diag.py"

    # Register names as phy_diag.py knows them. Note these differ from the
    # bare ``mlxreg --reg_name`` form — phy_diag's ADB exposes specialized
    # variants (e.g. ``PPBMP_BW_LOSS_MONITOR_PARAMETERS`` instead of
    # ``PPBMP``, ``PTER_PHY_REG`` instead of ``PTER``).
    REG_PPBMC: str = "PPBMC"
    REG_PPBMP_BW_LOSS: str = "PPBMP_BW_LOSS_MONITOR_PARAMETERS"
    REG_PTER_PHY: str = "PTER_PHY_REG"
    REG_PDDR_LINK_DOWN_INFO: str = "PDDR_LINK_DOWN_INFO"

    # PPBMC ``monitor_cntl`` bitmask — bit 5 (``0x20``) enables the literal
    # ``Tx_BW_loss`` monitor type (phy_diag decodes it as enum ``Tx_BW_loss``).
    # The FW default is ``0x24`` (bits 5+2 -- general BER monitoring) which
    # does NOT classify our injection as a BW-loss event on the local port.
    # We override to pure ``0x20`` so the local linkdown-reason-code is 45.
    PPBMC_MONITOR_CNTL_TX_BW_LOSS: int = 0x20

    # PPBMP ``monitor_group`` enum values discovered on Quantum3 (mt54004)
    # firmware 35.2016.4994-002. Groups 0/1/2/5 are BER source enums and
    # FW-locked for writes (``th_cap_exp_max=0``). Group 8 (``Tx_BW_loss``)
    # is the actual zombie-link group and is writable (``time_window_set_cap=1``).
    PPBMP_MONITOR_GROUP_RAW_BER_RS: int = 0
    PPBMP_MONITOR_GROUP_RAW_BER_FC: int = 1
    PPBMP_MONITOR_GROUP_EFFECTIVE_BER: int = 2
    PPBMP_MONITOR_GROUP_SYMBOL_BER: int = 5
    PPBMP_MONITOR_GROUP_TX_BW_LOSS: int = 8

    # PPBMP knobs for the "trip-immediately" injection setup.
    PPBMP_BW_LOSS_THRESHOLD_LOW: int = 1
    PPBMP_TIME_WINDOW_DEFAULT: int = 100
    PPBMP_TIME_WINDOW_W_EN: int = 1

    # PTER ``error_type_admin`` enum (also reported as ``error_type_cap`` bitmap).
    PTER_ERROR_TYPE_NONE: int = 0
    PTER_ERROR_TYPE_RAW_BER: int = 1
    PTER_ERROR_TYPE_EFFECTIVE_BER: int = 2
    PTER_ERROR_TYPE_SYMBOL_ERRORS: int = 4

    # PTER BER injection — verified-working values on Taipan Quantum3 FW
    # 35.2016.4994-002 (matches the wiki's NVL6 recipe). Higher values
    # (e.g. 15/15) are FW-rejected on some builds.
    PTER_BER_MANTISSA_DEFAULT: int = 1
    PTER_BER_EXP_DEFAULT: int = 4
    PTER_INJECTION_TIME_MAX: int = 0xFFFF

    # Regex matching the PTER response row that indicates the firmware armed
    # the injection (``error_type_oper`` field is 1). Whitespace-tolerant so
    # padding differences in phy_diag's table formatting don't break detection.
    # If absent the FW silently rejected (engineering-FW-only path) — caller
    # should skip rather than fail.
    PTER_ARMED_REGEX: str = r"error_type_oper\s*\|\s*1\b"

    # PDDR page selector for link-down info (the page that exposes
    # ``local_reason_opcode = 45 BW_loss_threshold_exceeded``).
    PDDR_PAGE_LINK_DOWN_INFO: int = 6


class DataBaseNames:
    CONFIG_DB = "ConfigDb"
    STATE_DB = "StateDb"


class DelayedRecovery:
    DELAYED_RECOVERY_STATE = "state"
    DELAYED_RECOVERY_LOSS_TH = "fec-plr-align-loss-th"
    DELAYED_RECOVERY_RETRY_TH = "plr-retry-th"
    DELAYED_RECOVERY_STATE_FORCE = "peer-state-force"
    DELAYED_RECOVERY_LOSS_TH_FORCE = "peer-fec-plr-align-loss-th-force"
    DELAYED_RECOVERY_RETRY_TH_FORCE = "peer-plr-retry-th-force"
    DELAYED_RECOVERY_DEFAULT_STATE = "disabled"
    DELAYED_RECOVERY_DEFAULT_LOSS_TH = 126
    DELAYED_RECOVERY_DEFAULT_RETRY_TH = 32
    DELAYED_RECOVERY_DEFAULT_FORCE_STATE = "enabled"
    DELAYED_RECOVERY_DEFAULT_FORCE_LOSS_TH = "disabled"
    DELAYED_RECOVERY_DEFAULT_FORCE_RETRY_TH = "disabled"
    DELAYED_RECOVERY_DEFAULT_APPLIED_STATE = "fw-default"
    DELAYED_RECOVERY_DEFAULT_APPLIED_LOSS_TH = 0
    DELAYED_RECOVERY_DEFAULT_APPLIED_RETRY_TH = 0


class PhyHealthConsts:
    """Constants for phy health related fields and values."""

    # Field names for phy health output
    EFFECTIVE_BER = "effective-ber"
    EFFECTIVE_ERRORS = "effective-errors"
    LANE = "lane"
    PHY_RECEIVED_BITS = "phy-received-bits"
    RAW_BER = "raw-ber"
    SYMBOL_BER = "symbol-ber"
    SYMBOL_ERRORS = "symbol-errors"
    TIME_SINCE_LAST_CLEAR_MIN = "time-since-last-clear-min"

    # Lane field names
    PHY_RAW_ERRORS = "phy-raw-errors"
    LANE_RAW_BER = "raw-ber"

    # Histogram field names
    RS_FEC_CORRECTED_ERRORS = "rs-fec-corrected-errors"
    COUNT = "count"

    # Expected values
    EXPECTED_BER_FORMAT = "0E-0"
    EXPECTED_BIN_COUNT = 16

    # List of all expected fields in phy health output
    EXPECTED_FIELDS = [
        EFFECTIVE_BER,
        EFFECTIVE_ERRORS,
        LANE,
        PHY_RECEIVED_BITS,
        RAW_BER,
        SYMBOL_BER,
        SYMBOL_ERRORS,
        TIME_SINCE_LAST_CLEAR_MIN,
    ]

    # List of expected fields in each lane
    EXPECTED_LANE_FIELDS = [PHY_RAW_ERRORS, LANE_RAW_BER]


class PhyDetailConsts:
    """Constants for phy detail attribute types per ASIC generation."""

    # PHY detail attribute types for QTM3 ASICs (includes NVL5)
    ATTR_TYPES_QTM3 = {
        "pd-link-width-enabled": "sai_uint8_t",
        "pd-link-speed-enabled": "sai_u32_list_t",
        "phy-hst-link-width-enabled": "sai_uint8_t",
        "phy-hst-link-speed-enabled": "sai_u32_list_t",
        "phy-manager-link-width-enabled": "sai_uint32_t",
        "phy-manager-link-proto-enabled": "sai_u32_list_t",
        "core-to-phy-link-width-enabled": "sai_uint32_t",
        "core-to-phy-link-proto-enabled": "sai_u32_list_t",
        "cable-proto-cap-ext": "sai_s32_list_t",
    }

    # PHY detail attribute types for QTM4 and newer ASICs
    ATTR_TYPES_QTM4_AND_NEWER = {
        "pd-link-width-enabled": "sai_uint8_t",
        "pd-link-speed-enabled": "sai_uint32_t",
        "phy-hst-link-width-enabled": "sai_uint8_t",
        "phy-hst-link-speed-enabled": "sai_uint32_t",
        "phy-manager-link-width-enabled": "sai_uint8_t",
        "phy-manager-link-proto-enabled": "sai_uint32_t",
        "core-to-phy-link-width-enabled": "sai_uint8_t",
        "core-to-phy-link-proto-enabled": "sai_uint32_t",
        "cable-proto-cap-ext": "sai_uint32_t",
    }

    # These attributes now return scalar values on QTM4+ after product fix (no longer null/absent)
    QTM4_NON_EXISTENT_ATTRS = []
