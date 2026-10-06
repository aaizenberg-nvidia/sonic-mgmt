import logging
import os
from enum import StrEnum
from typing import Dict, List, Optional
from dataclasses import dataclass, field, fields
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from ngts.constants.constants import NvosCliTypes, DVSCliTypes, BugHandlerConst
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.helpers.performance.performance_recovery import get_performance_session_state

logger = logging.getLogger()


class ValidationConsts:
    IGNORE_COUNTER_LIST = "ignore_counter_list"
    SPEED = "speed"
    VALIDATION_KEY = "validation_key"
    PORT_GROUPS = "port_groups"
    # Metadata block of TrafficValidator.json. TrafficValidator fills the DUT-side
    # fields (speed, chip_type, sdk_version, collectors_list); the run identity is
    # merged in on ingest from ValidatorRunIdentity.
    METADATA = "metadata"
    TX_RX_PER_PORT_GROUP = "tx_rx_per_port_group"
    SHARED_BUFFER_SIZE = "shared_buffer_size"
    COLLECTORS_LIST = "collectors_list"
    TC_DATAFRAME = "tc_dataframe"
    PG_HEADROOM_DATAFRAME = "pg_headroom_dataframe"
    PG_BUFFER_DATAFRAME = "pg_buffer_dataframe"
    PER_PORT_TC_STATS = "per_port_tc_stats"
    PER_PORT_PG_BUFFER_STATS = "per_port_pg_buffer_stats"
    PER_PORT_PG_HEADROOM_STATS = "per_port_pg_headroom_stats"
    TC_PG_SAMPLES = "TC_PG_samples"
    TC_SAMPLES = "TC_samples"
    TC_NAME = "tc"
    PG_NAME = "pg"
    PG_HEADROOM = "pg_headroom"
    PG_BUFFER = "pg_buffer"
    OCC_AVG = "occAvg"
    OCC_99 = "occ99"
    OCC_MAX = "occMax"
    MAX_WATERMARK = "maxWatermark"
    JSON_METRIC_AVG = "avg"
    JSON_METRIC_P99 = "percentile_99"
    JSON_METRIC_MAX = "max"
    JSON_METRIC_WATERMARK = "max_watermark"
    TX_RATE = "txRate"
    RX_RATE = "rxRate"
    TC_LATENCY_SAMPLES = "TC_latency_samples"
    TC_LATENCY_DATAFRAME = "tc_latency_dataframe"
    TC_AVG_LATENCY = "tcAvgLatency"
    TC_MAX_LATENCY = "tcMaxLatency"
    TC_99_LATENCY = "tc99Latency"
    DATAFRAME_KEYS_DICT = {
        TC_LATENCY_DATAFRAME: [TC_AVG_LATENCY, TC_MAX_LATENCY, TC_99_LATENCY],
        TC_DATAFRAME: [OCC_AVG, OCC_99, OCC_MAX, MAX_WATERMARK],
        PG_HEADROOM_DATAFRAME: [OCC_AVG, OCC_99, OCC_MAX, MAX_WATERMARK],
        PG_BUFFER_DATAFRAME: [OCC_AVG, OCC_99, OCC_MAX, MAX_WATERMARK]
    }
    COUNTERS_SAMPLES = "Counters_samples"
    SAMPLES_PARAMS = "sample_params"
    COUNTERS_DATAFRAME = "counters_dataframe"
    BW_SAMPLES = "Bandwidth_samples"
    BW_DATAFRAME = "bandwidth_dataframe"
    BW_STATS = 'bw_stats'
    TX_BW_AVG = 'tx_avg'
    RX_BW_AVG = 'rx_avg'
    TX_RATE_MIN = 'min_tx_bw'
    RX_RATE_MIN = 'min_rx_bw'
    TX = 'tx'
    RX = 'rx'
    PORT = "port"
    QUEUE = "queue"
    ACL = "acl"
    OS_PORT_NAME = "osPortName"
    OS_PORT_ALIAS = "osPortAlias"
    OS_PORTS_NAME_MAPPING_DATAFRAME = "osPortsNameMappingDataframe"
    UNTRIMMED_PERCENTAGE = "untrimmedPercentage"
    TRIMMING_PERCENTAGE = "trimmingPercentage"
    DROPPED_WITHOUT_TRIMMING_PERCENTAGE = "droppedWithoutTrimmingPercentage"
    DROPPED_WITHOUT_TRIMMING = "droppedWithoutTrimmingPackets"
    UNTRIMMED_BYTES_PERCENTAGE = "untrimmedBytesPercentage"
    TRIMMING_BYTES_PERCENTAGE = "trimmingBytesPercentage"
    TRIMMING_PPS = "trimmingPacketsPerSecond"
    POWER_SAMPLES = "Power_samples"
    TEMPERATURE_SAMPLES = "Temperature_samples"
    TEMPERATURE = "temperature"
    SENSORS_OUTPUT = 'sensors_output'


class PerfConsts:
    # Performance Setup Aliases
    RESERVED_BUFFER_SIZE_WITH_NO_DROPS = [54]
    LEFT_TG_ALIAS = "left-tg"
    RIGHT_TG_ALIAS = "right-tg"
    TG_REGEX = r"(left|right)-tg"
    TG_ALIAS_LIST = [LEFT_TG_ALIAS, RIGHT_TG_ALIAS]
    DUT_ALIAS = "dut"
    PERF_SETUP_PLAYERS_ALIASES = [LEFT_TG_ALIAS, DUT_ALIAS, RIGHT_TG_ALIAS]
    PERF_SETUP_TG_ALIASES = [LEFT_TG_ALIAS, RIGHT_TG_ALIAS]
    PERF_SETUP_DUT_ALIASES = [DUT_ALIAS]
    ECN_CAPABLE_TRANSPORT = 1
    # Sample Parameters
    # DISABLE_HFT_ON_SPC6: temporary workaround for RM #5165344. Remove once resolved.
    DISABLE_HFT_ENV_VAR = "DISABLE_HFT"
    DISABLE_HFT = "True" if is_redmine_issue_active([5165344])[0] else "False"
    SAMPLES_PARAMS = {
        "SAMPLE_DURATION": 30,
        "BW_SAMPLE_DELAY": 5,
        "TC_SAMPLE_DELAY": 5,
        "COUNTERS_SAMPLE_DELAY": 5,
        "CLEAR_COUNTERS": "True",
        "COLLECT_TC_LATENCY": "False",
        DISABLE_HFT_ENV_VAR: DISABLE_HFT,
    }
    COLLECT_TC_LATENCY_ENV_VAR = "COLLECT_TC_LATENCY"
    SENSORS_CMD_ENV_VAR = "SENSORS_CMD"
    DEFAULT_SENSORS_CMD = r"sensors *-i2c-5-*"
    SPC6_SENSORS_CMD = "sensors"
    CLEAR_COUNTERS_ENV_VAR = "CLEAR_COUNTERS"
    SHAPER_VALUE_ENV_VAR = "SHAPER_VALUE"
    SEND_FWS_ENV = "SEND_FWS"
    BURST_CONFIG_FILE_PATH_ENV = "BURST_CONFIG_FILE_PATH"
    SRV6_SONIC_OS_SHAPER_VALUE = 0.99
    SRV6_NVUE_OS_SHAPER_VALUE = 0.96
    DVS_SHAPER_VALUE = 0.975
    OCC_AVG_TH = 400
    TC_NUM = 7

    # Thresholds
    OCC_TH_DICT = {ValidationConsts.OCC_AVG: OCC_AVG_TH}
    TEMPERATURE_TH = 105
    LOW_AR_THRESHOLD = 190
    LOW_AR_THRESHOLD_SPC6 = 280
    MED_AR_THRESHOLD = 800
    MED_AR_THRESHOLD_SPC6 = 800
    HIGH_AR_THRESHOLD = 2000
    # SPC4 AR does not recover from repeated link toggles with hi=2000: TC occupancy stays
    # above it and TX is stuck at ~88% (test_ar_perf_link_flap[port_repeated_toggle]).
    HIGH_AR_THRESHOLD_SPC4 = 1200
    HIGH_AR_THRESHOLD_SPC6 = 1200
    # Chip types not listed fall back to HIGH_AR_THRESHOLD (see get_high_ar_threshold).
    HIGH_AR_THRESHOLD_BY_CHIP = {
        "SPC4": HIGH_AR_THRESHOLD_SPC4,
        "SPC6": HIGH_AR_THRESHOLD_SPC6
    }
    # AR extended grading adds congestion thresholds 4-7 (port grades up to 7) on top of
    # lo/med/hi. SPC6 is the first generation to support it - add newer generations here
    # as they arrive (see get_ar_extended_grading_conf_args).
    AR_EXTENDED_GRADING_CHIP_TYPES = ("SPC6",)
    AR_EXTENDED_THRESHOLD_COUNT = 4
    # Extended-grading profile (7 grades): 200, 383, 567, 750, 933, 1117, 1300.
    LOW_AR_THRESHOLD_EXTENDED_SPC6 = 200
    MED_AR_THRESHOLD_EXTENDED_SPC6 = 383
    HIGH_AR_THRESHOLD_EXTENDED_SPC6 = 567
    AR_EXTENDED_THRESHOLDS_SPC6 = (750, 933, 1117, 1300)
    ECMP_SIZE_MAX = 4096
    DEFAULT_FAIRNESS_THRESHOLD = 0.05
    DEFAULT_TC_OCC_FAIRNESS_THRESHOLD = 0.05

    # CLI Types
    NON_SONIC_CLI_TYPE = NvosCliTypes.NvueCliTypes + DVSCliTypes.DVSCliTypes

    # Paths and Directories

    DVS_RUN_TEST_PATH = "/root/sys_sdk/sx_sdk_py_tests/tests/run_tests.py"
    # The runner's per-invocation cable table runs mlxcables on every port (~10-18s) and is never consumed here
    DVS_RUN_TEST_CMD = f"{DVS_RUN_TEST_PATH} --skip-cable-info"
    PORTS_JSON_FILENAME = "ports.json"
    PORTS_JSON_PATH = f"/{PORTS_JSON_FILENAME}"
    PERF_SYS_SDK_CHANGE_FILE = "/tmp/perf_sys_sdk_change"
    DEFAULT_PERF_TEMPLATES_DIR = "performance_config_templates"
    CONFIG_FILES_DIR = os.path.join(BugHandlerConst.NGTS_PATH, 'tests/performance/config_files')
    REQUIRMENTS_DIR = os.path.join(BugHandlerConst.NGTS_PATH, 'performance_tests/')
    SDK_DEB_DIR_TEMPLATE = "/auto/sw_system_release/sx_sdk_eth/sx_sdk_eth-{SDK_VERSION}/DEBS/6.1.0-29-2-amd64/"
    SDK_VERSION_PATH = "/auto/sw_system_release/sx_sdk_eth/"
    DVS_LATEST_VERSION_FILE = "dvs_latest.properties"
    SDK_INSTALL_PATH = "/auto/mswg/projects/sx_mlnx_os/sx_fit_regression/libs/scripts/install_sdk_wrapper.py"
    SDK_DUMP_FILE_SYSTEM = '/var/log/sdk_dbg'
    SHARED_SDK_DUMPS_DIR = "/auto/sw_system_project/switch_performance/results/dumps"
    CLEAN_SWITCH_PATH = "/auto/mswg/projects/sx_mlnx_os/sx_fit_regression/libs/scripts/sx_sdk_clean_logs.py"
    SPC6_DVS_CUSTOM_CONFIG_FILE = "/usr/share/dvs_manager_eth_spectrum6_bu_128ports_4x_2bonus.xml"
    FW_BURN_PATH = "/auto/mswg/projects/sx_mlnx_os/sx_fit_regression/libs/scripts/sdk_fw_burn.py"
    LATEST_SDK_DEB_DIR_TEMPLATE = "/auto/sw_system_release/sx_sdk_eth/lastrc_{SDK_BRANCH}/DEBS/"
    SYNCD_PIP_INDEX_URL = 'https://urm.nvidia.com/artifactory/api/pypi/nv-shared-pypi/simple'

    # File Names
    REQUIRMENTS_FILE = 'requirements.txt'
    FW_VERSION_FILE = "FW.txt"

    # Export Python Path
    EXPORT_PYTHONPATH = (
        'export SDK_ROOT=/root/sys_sdk; '
        'export APP_LIB_PATH=$SDK_ROOT/applibs; '
        'export SDK_INCLUDE=$SDK_ROOT/applibs; '
        'export SXD_INCLUDE=$SDK_ROOT/sxd_libs; '
        'export PYVER=$(python3 -c "import sys; print(chr(46).join(map(str, sys.version_info[:2])))"); '
        'export INCLUDES="-I$SDK_ROOT/applibs/include '
        '-I$SDK_ROOT/sxd_libs/include '
        '-I$SDK_ROOT/sx_complib/include '
        '-I$SDK_ROOT/sx_gen_utils/include '
        '-I/usr/include/python$PYVER '
        '-I/usr/lib/python${PYVER}/config-${PYVER}-x86_64-linux-gnu/"; '
        'export LD_LIBRARY_PATH=$SDK_ROOT/applibs/lib:$SDK_ROOT/sxd_libs/lib:$SDK_ROOT/sx_complib/lib:$LD_LIBRARY_PATH; '
        'export PYTHONPATH=/root/sys_sdk/sx_sdk_py_tests/:'
        '/root/sys_sdk/sx_sdk_py_tests/tests/:'
        '/root/sys_sdk/sx_sdk_py_tests/tools/bpf_api_tracer/:'
        '/root/sys_sdk/sx_sdk_py_tests/libs/swig/:'
        '/root/sys_sdk/sx_sdk_py_tests/tests/traffic_tests/vlan_bridge/'
    )

    # Traffic Generator and Validator Constants
    DVS_TG_NAME = "GenericTrafficGenerator"
    DVS_TG_VALIDATOR_NAME = "TrafficValidator"
    DVS_TG_MLOOP_CONFIGURATION = "ConfigureMloopOnTG"
    DVS_TG_REMOVE_MLOOP_CONFIGURATION = "RemoveMloopOnTG"
    DVS_GET_PORTS = "GetPorts"
    DVS_UNSPLIT_ALL_PORTS = "UnsplitAllPorts"
    DVS_DYNAMIC_CONF_PREFIX = "DynamicConfiguration"
    DVS_CREATE_INCREMENTAL_DIPS = "MultiDipsTG"
    DVS_MODIFY_PG_BUFFER_CONNECTED_PORTS = "ModifyPGBufferConnectedPorts"

    CL_ROCE_LOSSLESS_DEFAULT_TC = 96
    DVS_LOSSLESS_TC = 26
    DVS_LOSSY_TC = 34
    DVS_CONTROL_TC = 48
    # ECN field codepoints (RFC 3168), the low 2 bits of the IPv6 Traffic Class /
    # IPv4 TOS byte. RoCE traffic is ECN-capable, so dscp_to_tc() callers building
    # RoCE streams pass ECN_ECT0 rather than a bare 2.
    ECN_NOT_ECT = 0
    ECN_ECT1 = 1
    ECN_ECT0 = 2
    ECN_CE = 3

    # Configuration Parameters
    PORT_GROUPS = "port_groups"
    AR_ALGORITHM = "ar_algorithm"
    AR_ALGORITHM_ENV_VAR = "AR_ALGORITHM"
    ECMP_CONFIGURATIONS = "ecmp_configurations"
    TRAFFIC_PATTERN = "traffic_pattern"
    BW_THRESHOLD = "bw_threshold"
    ECMP_TYPE_AR = "ar"
    ECMP_TYPE_STATIC = "static"

    LEAF_PORTS_GROUP = "leaf_ports_group"
    SUPER_SPINE_PORTS_GROUP = "super_spine_ports_group"
    LEFT_SPLIT_PORTS = "left_split_ports"
    RIGHT_SPLIT_PORTS = "right_split_ports"

    # Configuration Files
    AR_PERF_CONFIG_FOLDER = 'config_files'
    SDK_TEST_CONF = "sdk_test_conf"
    CUSTOM_IBM_PROFILE_JSON = 'ibm_profile.json'
    IBM_CUSTOM_PROFILE_NAME = 'ibm_profile'
    DISABLE_MAC_SCRIPT = "disable_mac_learn.py"
    LB_FILTER_SCRIPT = "api_for_filter.py"
    SDK_DUMP_REMOTE_BASENAME = 'sdkdump'
    GZIP_MAGIC_BYTES = b'\x1f\x8b'
    LB_SCRIPT_TG = "run_lb_script.sh"
    IP_NEIGH_SCRIPT = "config_ip_neigh.sh"
    TRAFFIC_SENDER_SCRIPT_TG = "traffic_generator.py"
    PACKETS_AGING_SCRIPT = "packets_aging.py"
    CONFIG_FILES_LIST_LEFT_TG = [DISABLE_MAC_SCRIPT, LB_FILTER_SCRIPT, LB_SCRIPT_TG]
    CONFIG_FILES_LIST_RIGHT_TG = [DISABLE_MAC_SCRIPT, LB_FILTER_SCRIPT, LB_SCRIPT_TG]
    CONFIG_FILES_DICT = {
        LEFT_TG_ALIAS: CONFIG_FILES_LIST_LEFT_TG,
        RIGHT_TG_ALIAS: CONFIG_FILES_LIST_RIGHT_TG
    }
    TRAFFIC_VALIDATION_JSON_PATH = "TRAFFIC_VALIDATION_JSON_PATH"
    # Sample Times
    DEFAULT_SAMPLE_TIME_IN_SEC = 20
    EXTENDED_SAMPLE_TIME_IN_SEC = 60
    PACKET_SIZE_4K = 4096
    PACKET_SIZE_256 = 256
    # Packet Sizes and Utilization Thresholds
    PACKET_SIZE_LIST = [PACKET_SIZE_4K]
    TG_TX_UTIL_TH = 95
    ROCE_ACK_SIZE = 64
    RTT_PROB_SIZE = 138
    RTT_PROB_RESPONSE_SIZE = 118
    CNP_SIZE = 116
    NACK_SIZE = 102
    SACK_SIZE = 170
    GFP_CONTROL_SIZE = 138

    # Indexes
    VALUE_INDEX = 0
    TIMESTAMP_INDEX = 1

    # Log Ports
    HEX_BASE = 16
    LOG_PORT_LEFT_TG = 0x10001
    LOG_PORT_RIGHT_TG = 0x10081
    LOG_PORTS_DICT = {LEFT_TG_ALIAS: LOG_PORT_LEFT_TG, RIGHT_TG_ALIAS: LOG_PORT_RIGHT_TG}

    # IP Neighbors
    L_IP_NEIGH = "10.10.10.10"
    R_IP_NEIGH = "20.20.20.20"

    # Supported Reboot Types
    PERF_SUPPORTED_REBOOT_TYPES = ['reboot', 'config reload -y']

    # Sleep Time
    SLEEP_TIME_BEFORE_SAMPLE = 15

    # Images and Grub Paths
    SONIC_GA_IMAGE = "/auto/sw_system_release/sonic/202311/202311/dev/sonic-mellanox.bin"
    SONIC_DVS_GRUB_PATH = 'host'
    GRUB_PATH_DICT = {
        "SONiC": SONIC_DVS_GRUB_PATH,
        "Cumulus": 'boot',
        "DVS": SONIC_DVS_GRUB_PATH
    }
    SDK_DEB_FILE_TEMPLATE = "sys-sdk-git_1.mlnx.{SDK_VERSION}_amd64.deb"
    LATEST_SDK_DEB_FILE_TEMPLATE = "sys-sdk-git_1.mlnx.*_amd64.deb"
    PERF_SYS_SDK_TAR_FILE_TEMPLATE = "perf_sys_sdk_{SDK_BRANCH}.db.1.tgz"
    PERF_SYS_SDK_TAR_FILE_PATH = "/.autodirect/sw_regression/system/SDK_SWITCH/MARS/tarball_files/"

    # Miscellaneous
    USED_SITE = "MTL"
    DVS_CLI_TYPE = "DVS"
    DVS_WELCOME_MESSAGE = "Welcome to the NVIDIA Switch Development System"
    ROCE_PORT = 4791
    UDP_SOURCE_PORT = 2001
    TCP_SOURCE_PORT = 2001
    UDP_DOURCE_PORT = 80
    TCP_DOURCE_PORT = 80
    ADAPTIVE_ROUTING_ENABLED = 1
    ADAPTIVE_ROUTING_DISABLED = 0
    IP_PROTOCOL_UDP = "UDP"
    IP_PROTOCOL_TCP = "TCP"
    # Single-family selectors for traffic_helpers.generate_ip_address_list, which also accepts
    # "dual" to return both families at once.
    IP_MODE_V4 = "v4"
    IP_MODE_V6 = "v6"

    # Counters
    COUNTERS = [
        "if_out_discards",
        "a_mac_control_frames_transmitted",
        "a_mac_control_frames_received",
        "a_pause_mac_ctrl_frames_transmitted",
        "a_pause_mac_ctrl_frames_received"
    ]
    ECN_COUNTERS = [f'tx_ecn_marked_tc_{tc}' for tc in range(TC_NUM)]
    TC_BUFFER_DISCARDS_COUNTERS = [f'tx_no_buffer_discard_uc_tc_{tc}' for tc in range(TC_NUM)]
    TC_WRED_DISCARDS_COUNTERS = [f'tx_wred_discard_tc_{tc}' for tc in range(TC_NUM)]
    TOTAL_COUNTERS = [
        'ingress_policy_engine',
        'ingress_vlan_membership',
        'ingress_tag_frame_type',
        'egress_vlan_membership',
        'loopback_filter',
        'egress_general',
        'egress_hoq',
        'port_isolation',
        'egress_policy_engine',
        'ingress_tx_link_down',
        'egress_stp_filter',
        'egress_hoq_stall',
        'egress_sll',
        'ingress_discard_all',
        'egress_link_down',
        'ingress_no_tx',
        'a_alignment_errors',
        'a_frame_check_sequence_errors',
        'a_frame_too_long_errors',
        'a_in_range_length_errors',
        'a_symbol_error_during_carrier',
        'a_unsupported_opcodes_received',
        'a_in_range_length_errors',
        'a_mac_control_frames_transmitted',
        'a_mac_control_frames_received',
        'a_pause_mac_ctrl_frames_transmitted',
        'a_pause_mac_ctrl_frames_received',
        'if_in_discards',
        'if_in_errors',
        'if_out_discards',
        'if_out_errors',
        'ether_stats_crc_align_errors',
        'ether_stats_drop_events',
        'dot3stats_alignment_errors',
        'dot3stats_carrier_sense_errors',
        'dot3stats_fcs_errors',
        'dot3stats_frame_too_longs',
        'dot3stats_sqe_test_errors',
        'dot3stats_symbol_errors',
        'dot3stats_internal_mac_transmit_errors',
        'dot3stats_internal_mac_receive_errors',
        'port_rx_fcs_errors',
        'port_rx_no_buffer',
        'port_rx_other_errors',
        'port_tx_errors',
    ] + ECN_COUNTERS + TC_BUFFER_DISCARDS_COUNTERS + TC_WRED_DISCARDS_COUNTERS

    # Timeouts
    TIMEOUT_FOR_NEXTHOP_RESOLUTION = 180
    # Post-reboot settle time before ONIE/install steps; used for multi-NOS uninstall (onie-select -k).
    # Unknown chip types fall back to TIMEOUT_FOR_UNINSTALL_MODE_DEFAULT (see general_clis_common).
    TIMEOUT_FOR_UNINSTALL_MODE_DEFAULT = 900
    TIMEOUT_FOR_UNINSTALL_MODE = {
        "SPC3": 900,
        "SPC4": 900,
        "SPC5": 480,
        "SPC6": 480
    }
    TIMEOUT_FOR_INSTALL_MODE = 120
    IMAGE_SETTLING_TIME = 30
    MAX_CELLS_RANGE_FOR_BINARY_SEARCH = 100

    @staticmethod
    def get_high_ar_threshold(chip_type):
        """Return the legacy AR high-congestion threshold (``congestion_thresh_hi``) for a chip type.

        Args:
            chip_type: ASIC generation of the DUT (e.g. "SPC4", "SPC6").

        Returns:
            The threshold in cells.
        """
        return PerfConsts.HIGH_AR_THRESHOLD_BY_CHIP.get(chip_type, PerfConsts.HIGH_AR_THRESHOLD)

    @staticmethod
    def get_ar_extended_grading_conf_args(chip_type, extended_thresholds=None):
        """Build the AR extended-grading entries of a spcx_ra/AR ``conf_args`` dict.

        Extended grading adds congestion thresholds 4-7 on top of lo/med/hi, giving port
        grades up to 7. It exists only on the chip types listed in
        ``AR_EXTENDED_GRADING_CHIP_TYPES``, so it is left off on older generations even
        when thresholds are supplied - the DUT then keeps the legacy lo/med/hi grading.

        Also gated on the ``--ar_extended_thresholds`` pytest flag (mirrored on
        ``get_performance_session_state().ar_extended_thresholds``): the legacy 3-grade
        lo/med/hi thresholds are the default, and extended thresholds are only honored
        when the flag is passed, regardless of what the caller supplies.

        Args:
            chip_type: ASIC generation of the DUT (e.g. "SPC4", "SPC6").
            extended_thresholds: Ordered thresholds 4-7 in cells. Passing None (the
                default) disables extended grading, which keeps the rendered DUT config
                identical to the legacy one.

        Returns:
            The ``conf_args`` entries to merge in: always ``extended_grading``, plus
            ``congestion_thresh_extended_4``..``_7`` when extended grading is on.

        Raises:
            ValueError: if ``extended_thresholds`` does not hold exactly four values.
        """
        if not get_performance_session_state().ar_extended_thresholds:
            extended_thresholds = None
        if extended_thresholds is None:
            return {"extended_grading": False}
        extended_thresholds = list(extended_thresholds)
        if len(extended_thresholds) != PerfConsts.AR_EXTENDED_THRESHOLD_COUNT:
            raise ValueError(f"extended_thresholds must hold exactly "
                             f"{PerfConsts.AR_EXTENDED_THRESHOLD_COUNT} values (thresholds 4-7), "
                             f"got {extended_thresholds}")
        if chip_type not in PerfConsts.AR_EXTENDED_GRADING_CHIP_TYPES:
            logger.info(f"AR extended grading is not supported on {chip_type} "
                        f"(supported: {PerfConsts.AR_EXTENDED_GRADING_CHIP_TYPES}) - "
                        f"keeping legacy lo/med/hi grading")
            return {"extended_grading": False}
        return {
            "extended_grading": True,
            **{f"congestion_thresh_extended_{index}": threshold
               for index, threshold in enumerate(extended_thresholds, start=4)},
        }


@dataclass
class BwFairnessThreshold:
    tx: Optional[float] = PerfConsts.DEFAULT_FAIRNESS_THRESHOLD
    rx: Optional[float] = PerfConsts.DEFAULT_FAIRNESS_THRESHOLD

    @classmethod
    def _get_bw_fairness_threshold(cls, bw_threshold: Optional[dict]) -> 'BwFairnessThreshold':
        """
        Build a BwFairnessThreshold from a single-port-group bw_threshold dict.

        A direction is set to None (disabled) only when its value in the dict is explicitly None.
        Falls back to a default BwFairnessThreshold when bw_threshold is not a dict.
        """
        if not isinstance(bw_threshold, dict):
            logger.info(f"_get_bw_fairness_threshold: bw_threshold is not a dict (got: {bw_threshold=} of type={type(bw_threshold).__name__!r}), returning default BwFairnessThreshold")
            return cls()
        result = cls()
        for group_threshold in bw_threshold.values():
            if not isinstance(group_threshold, dict):
                continue
            for direction in (ValidationConsts.TX, ValidationConsts.RX):
                if direction in group_threshold and group_threshold[direction] is None:
                    logger.info(f"bw_threshold has {direction}=None — setting bw_fairness_threshold.{direction} to None")
                    setattr(result, direction, None)
        return result

    @classmethod
    def get_bw_fairness_threshold_per_port_group(cls, bw_threshold: Optional[dict]) -> 'Optional[Dict[str, BwFairnessThreshold]]':
        """
        Build a per-port-group fairness threshold mapping from a bw_threshold dict.

        Returns None when bw_threshold is None, signaling that fairness validation should be skipped.
        When bw_threshold is a non-dict non-None value, returns {"default": <default BwFairnessThreshold>}.
        When bw_threshold is a dict keyed by port group names, each group gets its own
        BwFairnessThreshold where a direction is None (disabled) if that group's value
        for that direction is explicitly None.
        """
        if bw_threshold is None:
            logger.info("get_bw_fairness_threshold_per_port_group: bw_threshold is None, returning None")
            return None
        if not isinstance(bw_threshold, dict):
            logger.info(
                f"get_bw_fairness_threshold_per_port_group: bw_threshold is not a dict "
                f"(got: {bw_threshold=} of type={type(bw_threshold).__name__!r}), returning default for 'default' group"
            )
            return {"default": cls._get_bw_fairness_threshold(bw_threshold)}
        return {
            port_group: cls._get_bw_fairness_threshold({port_group: group_threshold})
            for port_group, group_threshold in bw_threshold.items()
        }

    def override_per_port_group(
        self, bw_fairness_threshold: 'Optional[Dict[str, BwFairnessThreshold]]'
    ) -> 'Optional[Dict[str, BwFairnessThreshold]]':
        """Apply this instance as a threshold override across all port groups.

        Directions that are already disabled (None) in a port group are preserved.
        """
        if bw_fairness_threshold is None:
            return None
        for threshold_by_direction in bw_fairness_threshold.values():
            if threshold_by_direction.tx is not None:
                threshold_by_direction.tx = self.tx
            if threshold_by_direction.rx is not None:
                threshold_by_direction.rx = self.rx
        return bw_fairness_threshold


class MultiNosSharedData:
    DEFAULT_SHARED_JSON = "shared_communication.json"
    ALIBABA_ACL_DUMP_PATH = 'alibaba_acl_path'
    ALIBABA_ACL_DUMP_NAME = 'alibaba_acl_name'


@dataclass
class OccupancyMetrics:
    """Per-metric variance thresholds for a single buffer type."""
    avg: float = PerfConsts.DEFAULT_TC_OCC_FAIRNESS_THRESHOLD
    percentile_99: float = PerfConsts.DEFAULT_TC_OCC_FAIRNESS_THRESHOLD
    max: float = PerfConsts.DEFAULT_TC_OCC_FAIRNESS_THRESHOLD
    max_watermark: float = PerfConsts.DEFAULT_TC_OCC_FAIRNESS_THRESHOLD


@dataclass
class OccupancyVarianceConfig:
    """
    Per-buffer-type variance thresholds for TC occupancy fairness validation.

    Each field holds an OccupancyMetrics instance with per-metric thresholds for that buffer type.
    Setting a field to None disables variance validation entirely for that buffer type.

    Attributes:
        tc (OccupancyMetrics, optional): Variance thresholds for TC buffer occupancy.
            None means TC occupancy variance will not be validated.
        pg_headroom (OccupancyMetrics, optional): Variance thresholds for PG headroom occupancy.
            None means PG headroom occupancy variance will not be validated.
        pg_buffer (OccupancyMetrics, optional): Variance thresholds for PG buffer occupancy.
            None means PG buffer occupancy variance will not be validated.
    """
    tc: Optional[OccupancyMetrics] = field(default_factory=OccupancyMetrics)
    pg_headroom: Optional[OccupancyMetrics] = field(default_factory=OccupancyMetrics)
    pg_buffer: Optional[OccupancyMetrics] = field(default_factory=OccupancyMetrics)

    _occupancy_metrics_field_by_occ_key = {
        ValidationConsts.OCC_AVG: ValidationConsts.JSON_METRIC_AVG,
        ValidationConsts.OCC_99: ValidationConsts.JSON_METRIC_P99,
        ValidationConsts.OCC_MAX: ValidationConsts.JSON_METRIC_MAX,
        ValidationConsts.MAX_WATERMARK: ValidationConsts.JSON_METRIC_WATERMARK,
    }

    def get_variance(self, buffer_type: str, occ_key: str) -> Optional[float]:
        """
        Return the variance threshold for a given buffer type and occupancy metric key.

        Args:
            buffer_type: One of the dataclass fields ('tc', 'pg_headroom', 'pg_buffer').
            occ_key: An occupancy metric key (e.g. ValidationConsts.OCC_AVG).

        Returns:
            The variance threshold as a float, or None if variance validation is disabled
            for the given buffer type (i.e. the field is set to None).

        Raises:
            ValueError: If buffer_type is not a recognized field or occ_key is not a
                known occupancy metric key. The exception message includes the allowed values.
        """
        try:
            metrics: Optional[OccupancyMetrics] = getattr(self, buffer_type)
        except AttributeError as attr_error:
            allowed = [f.name for f in fields(self) if not f.name.startswith('_')]
            raise AttributeError(
                f"{attr_error}.\nUnknown buffer_type '{buffer_type}'. Allowed values: {allowed}"
            )
        if metrics is None:
            logging.getLogger(__name__).debug(
                f"get_variance: buffer_type='{buffer_type}' is None — variance validation disabled for this buffer type"
            )
            return None
        field_name = self._occupancy_metrics_field_by_occ_key.get(occ_key)
        if field_name is None:
            allowed_keys = list(self._occupancy_metrics_field_by_occ_key.keys())
            raise ValueError(
                f"Unknown occ_key '{occ_key}'. Allowed values: {allowed_keys}"
            )
        return getattr(metrics, field_name)

    @classmethod
    def from_tc_occ_threshold(cls, tc_occ_threshold) -> Optional[Dict[str, 'OccupancyVarianceConfig']]:
        """
        Derive a per-port-group variance config dict from a tc_occ_threshold value.

        - None               → None (disables tc_occ_fairness validation)
        - {port_group: dict} → {port_group: OccupancyVarianceConfig()} per port group
        - {occ_key: value}   → {"default": OccupancyVarianceConfig()} applied to all port groups
        - non-dict           → raises ValueError
        """
        logger.debug(f"from_tc_occ_threshold: input tc_occ_threshold={tc_occ_threshold!r}")
        if tc_occ_threshold is None:
            logger.debug("from_tc_occ_threshold: tc_occ_threshold is None — disabling tc_occ_fairness validation, returning None")
            return None
        if not isinstance(tc_occ_threshold, dict):
            raise ValueError(
                f"tc_occ_threshold must be a dict or None, got {type(tc_occ_threshold).__name__!r}: {tc_occ_threshold!r}"
            )
        # Peek at the first value to detect the dict shape:
        # - values are dicts  → per-port-group config: {port_group: {occ_key: value, ...}, ...}
        # - values are scalars → flat/global config:    {occ_key: value, ...}
        if isinstance(next(iter(tc_occ_threshold.values()), None), dict):
            result = {pg: cls() for pg in tc_occ_threshold}
            logger.debug(f"from_tc_occ_threshold: detected per-port-group shape — returning per-port-group config: {list(result.keys())}")
            return result
        logger.debug("from_tc_occ_threshold: detected flat/global shape — returning {'default': OccupancyVarianceConfig()} applied to all port groups")
        return {"default": cls()}


@dataclass
class LossyLosslessTrafficHandler:
    """
    Data class to encapsulate configuration details for traffic setup.
    """
    neigh_ip: str
    neigh_mask: str
    neigh_mac: str
    nexthop_ip: str
    ports_list: List[int]
    ecmp_type: str


class Cl_Consts:
    CL_LOG_PORT_FILE_PATH = os.path.join(BugHandlerConst.NGTS_PATH, 'scripts/')
    CL_LOG_PORT_FILE = 'log_port_cumulus.py'
    CL_LOG_BW_FILE = 'collect_bw_cumulus.py'
    AR_PROFILES = {
        "SPC4": "profile-2",
    }
    BONUS_PORTS = {
        'Spectrum-3': [],
        'Spectrum-4': ['swp65'],
        'Spectrum-5': ['swp65', 'swp66'],
        'Spectrum-6': ['swp65s0', 'swp65s1']
    }
    CL_HOME_DIR = "/home/cumulus"
    CL_SDK_CLEAR_ALL_COUNTERS_FORCE_CMD = "sudo sx_api_port_counter_clear_all.py --force"
    CL_PYTHON_PATH = "/home/cumulus/sdk_env/bin/python3.11"
    CL_GA_IMAGE = "/auto/sw_system_project/NVOS_INFRA/cumulus_images/GA/5.10/cumulus-linux-mlx-amd64.bin.devsigned"
    COMMON_IP_PREFIX_LEFT = "130"
    COMMON_IP_PREFIX_RIGHT = "110"
    SRV6_SPEED_BY_CHIP_TYPE = {
        "SPC4": "200000000",
        "SPC5": "100000000",
        "SPC6": "200000000"
    }
    SRV6_SPLIT_LEFT_BY_CHIP_TYPE = {
        "SPC4": 4,
        "SPC5": 8,
        "SPC6": 8,
    }
    SRV6_SPLIT_RIGHT_BY_CHIP_TYPE = {
        "SPC4": 4,
        "SPC5": 8,
        "SPC6": 8,
    }
    SDK_DEB_DIR_TEMPLATE = os.path.join(PerfConsts.SDK_VERSION_PATH, "sx_sdk_eth-{SDK_VERSION}/DEBS/6.1.0-29-2-amd64/")


class ArAlgorithm(StrEnum):
    """AR (adaptive-routing) port-selection algorithms for the spcx_ra scenario.

    Members map to the exact strings consumed downstream by perf_sys_sdk
    (spcx_basic_ra_dut_config / spcx_ra_tg -> ar_profile_create ``port_select_mode``).
    Round-Robin and Semi-Round-Robin require ASIC generation SPC6+; Random-From-Best
    runs on SPC2+.
    """
    RANDOM_FROM_BEST = "random_from_best"
    SEMI_ROUND_ROBIN = "semi_round_robin"
    ROUND_ROBIN = "round_robin"

    @classmethod
    def from_env(cls) -> Optional["ArAlgorithm"]:
        """Return the algorithm requested through the ``AR_ALGORITHM`` env variable.

        Lets a run override the algorithm without touching the test code, e.g.
        ``AR_ALGORITHM=semi_round_robin pytest ... test_spcx_ra.py``.

        Returns:
            The matching member, or ``None`` when the variable is unset or empty
            (the caller then falls back to its own default).

        Raises:
            ValueError: The variable holds a value that is not a supported algorithm.
        """
        requested = os.getenv(PerfConsts.AR_ALGORITHM_ENV_VAR, "").strip().lower()
        if not requested:
            return None
        try:
            return cls(requested)
        except ValueError:
            raise ValueError(
                f"Invalid {PerfConsts.AR_ALGORITHM_ENV_VAR}='{requested}'. "
                f"Supported values: {', '.join(member.value for member in cls)}") from None


class SPCXRAConsts:
    DUT_TX_UTIL_IBM_BW_TH = 0.96
    DUT_TX_UTIL_IBM_BW_TH_SPC4_SPC5 = 0.959

    @staticmethod
    def get_min_line_rate_bw_threshold_ibm(chip_type):
        """
        Return minimum line rate IBM BW threshold: 0.959 for SPC4/SPC5 when issue 4848895 is active, 0.96 otherwise.
        HW Bug #4848895: AR unfairness while sending similar packets SPC4/SPC5.

        Args:
            chip_type (str): Chip type

        Returns:
            float: Minimum line rate IBM BW threshold
        """
        if chip_type in ("SPC4", "SPC5") and is_redmine_issue_active([4848895])[0]:
            return SPCXRAConsts.DUT_TX_UTIL_IBM_BW_TH_SPC4_SPC5
        return SPCXRAConsts.DUT_TX_UTIL_IBM_BW_TH

    DUT_TX_UTIL_AUTO_TH_DICT = {
        "left_ports": {ValidationConsts.TX: 0.92, ValidationConsts.RX: 0.92},
        "right_ports": {ValidationConsts.TX: 0.92, ValidationConsts.RX: 0.92}
    }
    DUT_TX_UTIL_IBM_TH_DICT = {
        "left_ports": {ValidationConsts.TX: DUT_TX_UTIL_IBM_BW_TH, ValidationConsts.RX: DUT_TX_UTIL_IBM_BW_TH},
        "right_ports": {ValidationConsts.TX: DUT_TX_UTIL_IBM_BW_TH, ValidationConsts.RX: DUT_TX_UTIL_IBM_BW_TH}
    }
    if is_redmine_issue_active([4667031])[0]:
        DUT_TX_UTIL_AUTO_TH_DICT[ValidationConsts.VALIDATION_KEY] = (ValidationConsts.TX_BW_AVG, ValidationConsts.RX_BW_AVG)
        DUT_TX_UTIL_IBM_TH_DICT[ValidationConsts.VALIDATION_KEY] = (ValidationConsts.TX_BW_AVG, ValidationConsts.RX_BW_AVG)

# Per-chip traffic-packet counts, indexed by chip_type.
    # SPC6 lanes are 200G (vs 100G on SPC4/SPC5), so SPC6 uses 1.5x the
    # SPC4/SPC5 packet count at the same per-port speed.
    # All callers must index by chip_type, e.g. PACKET_NUM_800G_x1[chip_type].
    PACKET_NUM_200G_x4 = {"SPC4": 6, "SPC5": 6, "SPC6": 9}
    PACKET_NUM_400G_x2 = {"SPC4": 8, "SPC5": 8, "SPC6": 12}
    PACKET_NUM_800G_x1 = {"SPC4": 20, "SPC5": 20, "SPC6": 30}
    PACKET_NUM_800G_x2 = PACKET_NUM_800G_x1
    PACKET_NUM_800G_x1_WITH_INCREMENTAL_DIPS = {"SPC4": 72, "SPC5": 72, "SPC6": 108}

    # Downstream (spine -> host) destination IPs for the leaf scenario, one address per DUT right port.
    # These mirror the DVS reference literals in
    # performance_config_templates/spcx_ra/dvs/dut.jinja ("dip_left_to_right" and the base of
    # "right_side_ports_to_ip_dict"); keep both in sync if either side moves.
    # The range sits outside every subnet the SPCX-RA topology advertises, so a destination without a
    # per-port route is dropped by the DUT instead of silently falling back to the BGP ECMP group.
    LEAF_DIP_BASE_IPV4 = "10.0.1.0"
    LEAF_DIP_BASE_IPV6 = "192:168:5:1:1:1:2:0"
    LEAF_DIP_STEP = 1

    # PFC port buffer in bytes, per chip. 363000 is the Spectrum-4 / SN5600 figure from the
    # Spectrum-X v2.2 deployment guide and is what SPC6 runs with. SPC4 and SPC5 use 20200 because
    # they reject 363000: their cells are 192 bytes and the buffer caps at 258432 bytes, exactly
    # 1346 cells, so even SPC6's 1417 cells does not fit. Both are placeholders pending the QoS PFC
    # values the architecture team is confirming for those generations. A chip left out of this map
    # renders without "port-buffer" at all and takes whatever NVUE derives instead.
    PFC_PORT_BUFFER_BY_CHIP = {"SPC6": 363000, "SPC5": 20200, "SPC4": 20200}

    # Ceiling on the packets resident in the loop at once, (ports x packets-per-port). Only a
    # backstop: the schedule is sized from chip_dict's calibrated per-port count, which is what
    # tracked the healthy runs - 30 packets at 800G, 12 at 400G, 9 at 200G. 4096 is the level the
    # pre-schedule pattern staged at 4 lanes/sub-port, and that ran at 0.655 of line rate with
    # egress occupancy nineteen times its threshold, so treat it as a level reached in practice
    # rather than one known to be healthy.
    LEAF_RESIDENT_PACKET_BUDGET = 4096
    # Largest fraction of the destination list a single port may be assigned. Below 1 so that
    # per-port destination sets can actually differ - at the full list every port would be
    # identical and the schedule would degenerate back into a synchronised sweep.
    LEAF_PORT_SET_DIVISOR = 2
    # Fixed seed for the per-port destination draw, so a throughput change between two runs is a
    # real regression rather than a different random sample.
    LEAF_SCHEDULE_SEED = "spcx_ra_leaf"

    # Wait for the leaf host routes to reach the routing table before traffic starts. Neighbour
    # resolution is a separate event, so waiting on it alone lets traffic begin while the routes
    # those neighbours serve are still missing.
    LEAF_ROUTE_TIMEOUT = 120
    LEAF_ROUTE_POLL_INTERVAL = 5
    # The route wait above reads the kernel table; switchd offloads to the ASIC afterwards with no
    # readiness signal to poll, so traffic must not start the moment the kernel routes appear.
    LEAF_FORWARDING_SETTLE_SECONDS = 60
    # Settle time after traffic starts, before the traffic validator takes its first reading. The
    # leaf pattern opens with a burst as the queues fill, and the validator's first counters sample
    # is a read-and-clear of everything since traffic began, so waiting here puts the whole burst
    # into the sample the leaf validation already discards instead of leaving its tail to land in
    # the second one. Traffic keeps running throughout, so the measured load is unchanged.
    LEAF_TRAFFIC_SETTLE_SECONDS = 60
    # Port readiness budgets for the leaf re-apply only. "nv config replace" bounces every link, and
    # a host route stays out of the kernel while the interface behind its nexthop is down, so the
    # leaf run waits far longer than the shared defaults for links to train. Admin state follows the
    # configuration within seconds, so a port still admin down well before the oper budget expires
    # is a configuration problem rather than a slow link, and is worth failing on sooner.
    LEAF_PORT_ADMIN_UP_TIMEOUT = 180
    LEAF_PORT_OPER_UP_TIMEOUT = 180


class MongoDbConsts:
    PERF_MONGO_DB_FILENAME = "perf_res.db"
    PERF_MONGO_DB_RESULTS_PATH = os.path.join(PerfConsts.REQUIRMENTS_DIR, PERF_MONGO_DB_FILENAME)
    PORT_GROUP_NAME = "portGroupName"
    PORT_GROUP_DF = "portGroupDataframe"
    BW_COUTERS_DATA = "bandwidthCountersData"
    TC_DATA = "tcData"
    PG_DATA = "pgData"
    TC_LATENCY_DATA = "tcLatencyData"
    TEMP_DATA = "temperatureData"
    TEST_NAME = "testName"
    DUT_SYSTEM_INFORMATION = "dutSystemInformation"
    MARS_SESSION_ID = "marsSessionId"
    SETUP_NAME = "setupName"
    OS_VERSION = "osVersion"
    TEST_WORKLOAD = "testWorkload"
    TEST_TRAFFIC_TYPE = "testTrafficType"
    INGRESS_PORT_SEQUENCE = "ingressPortSequence"
    TIME_STAMP = "timeStamp"
    TIME_REGEX_FORMAT = "%d-%m-%Y %H:%M:%S"
    TIME_REGEX_FORMAT_FOR_MONGO_DB = "%d-%m-%Y_%H-%M-%S"

    POWER_TOTAL = "powerTotal"
    POWER_BY_COLLECTORS = "powerByCollectors"
    ALLURE_URL = "allureUrl"
    TEST_RESULT = "result"
    VALIDATOR_RESULTS = "validatorResults"
    TRIMMED_UNTRIMMED_DROPPED_PERCENTAGES = "trimmedUntrimmedDroppedPercentages"
    CONF_NAME = "configurationName"
    TEST_STATE = "testState"
    STATE_INFO = "stateInfo"
    COLLECTION = ":COLLECTION:SwitchPerformanceCollection\n"
    CRITERIA = ":CRITERIA_FIELD:testType\n"
    MONGO_DB_DICT_PATH = "/auto/sw_system_project/switch_performance/results/mongodb/"
    MONGO_DB_UPLOADS = os.path.join(MONGO_DB_DICT_PATH, "for_upload/")
    MONGO_DB_ERRORS = os.path.join(MONGO_DB_DICT_PATH, "errors/")
    MONGO_DB_SANDBOX_TESTS = os.path.join(MONGO_DB_DICT_PATH, "Sandbox_testing/")
    MONGO_DB_SANDBOX_TESTING_COMMAND = os.path.join(MONGO_DB_DICT_PATH, "initiate_sandbox")
    MONGO_DB_SANDBOX_TESTING_TIMEOUT = 300


class PortMappingOptionsConsts:
    """
    This class contains the port mapping options for the performance tests

    Port Mapping Table:
    +----------+-------------------+-------------------------+----------------------+------------------------------------------+-----------------------------+-------------------+
    | Ingress  | Option 1:         | Option 2:               | Option 3:            | Option 4:                                | Option 5:                   | Option 6:         |
    |          | 4 ports in        | 2 in same 8x,           | Each in              | 2 in same 8x, 1 in another 8x,           | 3 in same 8x,               | Sequential        |
    |          | same 8x           | 2 in other 8x           | different 8x         | last one in another 8x                   | 1 in another 8x             | M-to-1 mapping    |
    +----------+-------------------+-------------------------+----------------------+------------------------------------------+-----------------------------+-------------------+
    |    0     |        0          |           0             |          0           |                 0                        |              0              |         0         |
    |    1     |        1          |           0             |          8           |                 0                        |              0              |         0         |
    |    2     |        2          |           8             |         16           |                 8                        |              1              |         1         |
    |    3     |        3          |           8             |         24           |                16                        |              8              |         1         |
    |    4     |        0          |           1             |          0           |                 1                        |              1              |         2         |
    |    5     |        1          |           1             |          8           |                 1                        |              2              |         2         |
    |    6     |        2          |           9             |         16           |                 8                        |              2              |         3         |
    |    7     |        3          |           9             |         24           |                16                        |              8              |         3         |
    |    8     |        4          |           2             |          1           |                 2                        |              3              |         4         |
    |    9     |        5          |           2             |          9           |                 2                        |              3              |         4         |
    |   10     |        6          |          10             |         17           |                 9                        |              4              |         5         |
    |   11     |        7          |          10             |         25           |                17                        |              9              |         5         |
    |   12     |        4          |           3             |          1           |                 3                        |              4              |         6         |
    |   13     |        5          |           3             |          9           |                 3                        |              5              |         6         |
    |   14     |        6          |          11             |         17           |                 9                        |              5              |         7         |
    |   15     |        7          |          11             |         25           |                17                        |              9              |         7         |
    +----------+-------------------+-------------------------+----------------------+------------------------------------------+-----------------------------+-------------------+

    """
    EGRESS_SAME_8X_OPTION = 1
    EGRESS_2_IN_SAME_8X_2_IN_OTHER_8X_OPTION = 2
    EGRESS_DIFFERENT_8X_OPTION = 3
    EGRESS_2_IN_SAME_8X_1_IN_OTHER_8X_LAST_IN_OTHER_8X_OPTION = 4
    EGRESS_3_IN_SAME_8X_1_IN_OTHER_8X_OPTION = 5
    EGRESS_SEQUENTIAL_OPTION = 6


EGRESS_PORT_GROUP_NAME = "egress_ports"
INGRESS_PORT_GROUP_NAME = "ingress_ports"


@dataclass
class MinPacketSizeConfig:
    """Minimum SRv6 bisection data packet params for one ASIC generation.

    The minimum differs per ASIC generation, so it is looked up by chip type
    rather than hardcoded.

    Attributes:
        packet_size: MRC data packet size (bytes) at the minimum.
        mrc_num_packets: MRC data packets per stream at the minimum.
        retransmit_num_packets: MRC retransmission packets per stream at the minimum.
    """
    packet_size: int
    mrc_num_packets: int
    retransmit_num_packets: int


class MRCConsts:
    # Cumulus SRv6 scheduler configuration:
    # - Select one entry from CUMULUS_DWRR_PROFILES with CUMULUS_DWRR_PROFILE.
    # - A TC listed in "weights" is configured in DWRR mode; its value becomes NVUE bw-percent.
    # - A TC listed in "strict_tcs" is configured in strict mode (no DWRR weight).
    # - To change a TC from strict to DWRR, remove it from "strict_tcs" and add it to "weights".
    # - To change a TC from DWRR to strict, remove it from "weights" and add it to "strict_tcs".
    # - Weights must be integers from 1 through 100 and their configured total cannot exceed 100.
    CUMULUS_DWRR_PROFILE = "default"
    CUMULUS_DWRR_PROFILES = {
        "sonic": {
            "weights": {1: 8, 2: 18, 3: 22, 4: 22},
            "strict_tcs": []
        },
        "custom": {
            "weights": {1: 8, 2: 18, 3: 22, 4: 48},
            "strict_tcs": []
        },
        "default": {
            "weights": {},
            "strict_tcs": []
        }
    }
    # Per-TC egress lossy-buffer shared-alpha overrides (NVUE values such as "alpha_8").
    # Empty dict preserves lossy-multi-tc defaults for all TCs. Keys are TC ids 0-7.
    # Example — override MRC/trimming TCs 1-4:
    # Empty dict preserves lossy-multi-tc defaults for all TCs. Example override:
    #   CUMULUS_EGRESS_LOSSY_SHARED_ALPHA_BY_TC = {1: "alpha_1_4", 2: "alpha_1_4", 3: "alpha_1_4", 4: "alpha_4"}
    CUMULUS_EGRESS_LOSSY_SHARED_ALPHA_BY_TC = {}
    # Deprecated: use CUMULUS_EGRESS_LOSSY_SHARED_ALPHA_BY_TC[4] instead. When set and TC4 is
    # absent from the dict, applies only to TC4 (backward compatible).
    CUMULUS_TC4_SHARED_ALPHA = None
    # Per-chip / per-TC egress lossy-buffer reservation in cells. Empty outer/inner dicts preserve
    # the profile defaults. Values are converted to bytes using the ASIC cell size.
    # Example for SPC6 only: {"SPC6": {4: 50}} renders reserved: 12800
    # (50 cells * 256 bytes/cell). SPC4/SPC5 stay unchanged when omitted.
    CUMULUS_EGRESS_LOSSY_RESERVED_CELLS_BY_CHIP_TYPE = {}

    @staticmethod
    def get_cumulus_egress_lossy_shared_alpha_by_tc():
        """Return sorted TC→alpha map for Cumulus egress-lossy-buffer overrides.

        Merges CUMULUS_EGRESS_LOSSY_SHARED_ALPHA_BY_TC with legacy CUMULUS_TC4_SHARED_ALPHA
        (legacy applies only when TC4 is not already in the dict). Entries with None values
        are omitted.

        Returns:
            dict: Mapping of traffic class id to NVUE shared-alpha string, or empty dict.
        """
        shared_alpha_by_tc = {
            tc: alpha for tc, alpha in (MRCConsts.CUMULUS_EGRESS_LOSSY_SHARED_ALPHA_BY_TC or {}).items()
            if alpha is not None
        }
        if MRCConsts.CUMULUS_TC4_SHARED_ALPHA is not None:
            shared_alpha_by_tc.setdefault(4, MRCConsts.CUMULUS_TC4_SHARED_ALPHA)
        return dict(sorted(shared_alpha_by_tc.items()))

    @staticmethod
    def get_cumulus_egress_lossy_reserved_cells_by_tc(chip_type):
        """Return per-TC reservation cells for the given chip type.

        Args:
            chip_type: ASIC generation, such as ``SPC5`` or ``SPC6``.

        Returns:
            dict: Mapping of traffic class id to reserved cells, or an empty dict.
        """
        reserved_by_chip = MRCConsts.CUMULUS_EGRESS_LOSSY_RESERVED_CELLS_BY_CHIP_TYPE or {}
        return dict(reserved_by_chip.get(chip_type) or {})

    @staticmethod
    def get_cumulus_egress_lossy_reserved_bytes_by_tc(chip_type):
        """Convert configured per-TC reservation cells to NVUE bytes for a chip.

        Args:
            chip_type: ASIC generation, such as ``SPC5`` or ``SPC6``.

        Returns:
            dict: Mapping of traffic class id to reserved bytes, or an empty dict.

        Raises:
            ValueError: If reservations are configured for an unsupported chip type.
        """
        reserved_cells_by_tc = MRCConsts.get_cumulus_egress_lossy_reserved_cells_by_tc(chip_type)
        if not reserved_cells_by_tc:
            return {}
        cell_size = MRCConsts.BUFFER_CELL_SIZE_BY_CHIP_TYPE.get(chip_type)
        if cell_size is None:
            raise ValueError(f"No buffer cell size configured for chip type '{chip_type}'")
        return dict(sorted((tc, cells * cell_size) for tc, cells in reserved_cells_by_tc.items()))

    DOWNLINKS = "Downlinks"
    UPLINKS = "Uplinks"
    T1_MANY_TO_FEW_INGRESS_PORTS_NUM_BY_CHIP_TYPE = {
        "SPC4": 256,
        "SPC5": 448,
        "SPC6": 448
    }
    T0_DOWNLINKS_LIST = []
    T0_UPLINKS_LIST = []
    T0_DOWNLINKS_LIST.extend(list(range(64)))
    T0_DOWNLINKS_LIST.extend(list(range(192, 320)))
    T0_DOWNLINKS_LIST.extend(list(range(448, 512)))
    T0_UPLINKS_LIST.extend(list(range(64, 192)))
    T0_UPLINKS_LIST.extend(list(range(320, 448)))
    T0_UPSTREAM_DOWNSTREAM_PORT_GROUPS_DICT = {DOWNLINKS: [f"Ethernet{index}" for index in T0_DOWNLINKS_LIST],
                                               UPLINKS: [f"Ethernet{index}" for index in T0_UPLINKS_LIST]}
    EIGHT_X_SKIP_GAP = 8
    FOUR_X_SKIP_GAP = 4
    TRIMMING_COUNTERPOLL_LIST = [ValidationConsts.PORT, ValidationConsts.QUEUE, ValidationConsts.ACL]
    TRIMMING_COUNTERPOLL_INTERVAL = 1000
    LATENCY_ALLOWED_DEVIATION = 59
    HEADROOM_ALLOWED_DEVIATION = 0.15
    # Floor for the ingress PG deviation band, in buffer cells. HEADROOM_ALLOWED_DEVIATION is
    # a percentage, so its width shrinks with the reference and collapses on a near-empty
    # buffer. SPC4 and SPC5 keep the shared pool loaded at 51 and 26 cells, where 15% is a
    # stable band. SPC6 absorbs the same traffic in its reserved headroom, leaving a shared
    # pool of 0.2 to 0.56 cells that has deviated from the reference by 1.7% to 80.4% in both
    # directions across three sessions. The floor binds below a reference of about 6.7 cells
    # and the percentage takes over above it, so the band is never narrower than either rule
    # alone. On a near-empty buffer the lower limit lands below zero, which leaves the check
    # effectively one sided; that is the only meaningful direction there, since a lower
    # shared pool reading just means fewer bursts were sampled.
    MIN_PG_OCC_BAND_CELLS = 1
    # Allowed deviation of the total ingress PG occAvg from the IPv6 reference.
    # Kept separate from HEADROOM_ALLOWED_DEVIATION so the per-frame and the total
    # comparisons can be tuned independently, even though both start at 15%.
    PG_TOTAL_OCC_ALLOWED_DEVIATION = 0.15
    MRC_DATA_PACKET_SIZE = 4234
    EGRESS_PORT_GROUP_NAME = EGRESS_PORT_GROUP_NAME
    INGRESS_PORT_GROUP_NAME = INGRESS_PORT_GROUP_NAME
    BISECTION_DOWNSTREAM_PORT_GROUP_NAME = "bisection_downstream"
    BISECTION_UPSTREAM_PORT_GROUP_NAME = "bisection_upstream"
    LEAF_ROUND_ROBIN_UPSTREAM_PORT_GROUP_NAME = "round_robin_upstream"
    LEAF_ROUND_ROBIN_DOWNSTREAM_PORT_GROUP_NAME = "round_robin_downstream"
    SPINE_ROUND_ROBIN_DOWNSTREAM_PORT_GROUP_1_NAME = "round_robin_downstream_group_1"
    SPINE_ROUND_ROBIN_DOWNSTREAM_PORT_GROUP_2_NAME = "round_robin_downstream_group_2"
    MAX_INCAST_WITHOUT_TRIMMING_DROP = 10
    INCAST_VALUE_WITH_TRIMMING_DROP = MAX_INCAST_WITHOUT_TRIMMING_DROP + 1
    DWRR_WEIGHTS_CONFIG = "DEFAULT"
    TC_1_2_3_ALPHA = 1 / 128
    MIN_INGRESS_PORTS_NUM = 4
    MAX_INGRESS_PORTS_NUM = 5
    INGRESS_PORT_NUMBER_LIST = list(range(MIN_INGRESS_PORTS_NUM, MAX_INGRESS_PORTS_NUM))
    ACL_MODES = [False, True]
    PORTS_NUM_LIST = [120, 240, 360, 448, 512]
    M_LIST = list(range(2, MAX_INGRESS_PORTS_NUM))
    OPTION_LIST = [PortMappingOptionsConsts.EGRESS_SAME_8X_OPTION]  # can also include 2, 3, 4, 5
    T1_MANY_TO_FEW_MIN_INGRESS_PORTS_NUM = 6
    T1_MANY_TO_FEW_MAX_INGRESS_PORTS_NUM = 7
    T1_MANY_TO_FEW_INGRESS_PORT_NUMBER_LIST = list(range(T1_MANY_TO_FEW_MIN_INGRESS_PORTS_NUM, T1_MANY_TO_FEW_MAX_INGRESS_PORTS_NUM))
    HWSKU_BY_CHIP_TYPE = {
        "SPC4": {"leaf": "Mellanox-SN5600-C256S1",
                 "spine": "Mellanox-SN5600-C224O8"},
        "SPC5": {"leaf": "Mellanox-SN5640-C512S2",
                 "spine": "Mellanox-SN5640-C448O16"},
        "SPC6": {"leaf": "Mellanox-SN6600_LD-V512C2",
                 "spine": "Mellanox-SN6600_LD-V448P16S2"},
    }
    HWSKU_SWITCH_TYPE = {
        "Mellanox-SN5600-C256S1": 'ToRRouter',
        "Mellanox-SN5600-C224O8": 'LeafRouter',
        "Mellanox-SN5640-C512S2": 'ToRRouter',
        "Mellanox-SN5640-C448O16": 'LeafRouter',
        "Mellanox-SN6600_LD-V512C2": 'ToRRouter',
        "Mellanox-SN6600_LD-V448P16S2": 'LeafRouter'
    }
    UPSTREAM_DOWNSTREAM_NUM_OF_PORTS_BY_CHIP_TYPE = {
        "SPC4": 128,
        "SPC5": 180,
        "SPC6": 180
    }
    VICTIM_PORTS_NUM = 90
    LEAF_ROUND_ROBIN_PORTS_NUM_BY_CHIP_TYPE = {
        "SPC4": {'group_size': 16, 'group_num': 8},
        "SPC5": {'group_size': 10, 'group_num': 18},
        "SPC6": {'group_size': 10, 'group_num': 18}
    }
    SPINE_ROUND_ROBIN_PORTS_NUM_BY_CHIP_TYPE = {
        "SPC4": {'group_size': 16, 'group_num': 7},
        "SPC5": {'group_size': 14, 'group_num': 16},
        "SPC6": {'group_size': 14, 'group_num': 16}
    }
    TRAFFIC_TYPE_IPV6 = "IPv6"
    TRAFFIC_TYPE_SRV6 = "SRv6"
    WEEKEND_TRAFFIC_TYPE_LIST = [TRAFFIC_TYPE_IPV6, TRAFFIC_TYPE_SRV6]
    WEEKDAY_TRAFFIC_TYPE_LIST = [TRAFFIC_TYPE_SRV6]
    # In case of running weekend regression, this list can be changed with 'if' statement checking os.environ.get('SKIP_WEEKEND_CASES') == 'yes'
    REGRESSION_TRAFFIC_TYPE_LIST = WEEKEND_TRAFFIC_TYPE_LIST
    INGRESS_PORT_SEQUENCE_CONSECUTIVE = 'consecutive'
    INGRESS_PORT_SEQUENCE_NON_CONSECUTIVE = 'non_consecutive'
    INGRESS_PORT_SEQUENCE = [INGRESS_PORT_SEQUENCE_NON_CONSECUTIVE]
    DUT_TX_UTIL_TH = 0.95 if is_redmine_issue_active([4667031])[0] else 0.98
    # Many to one SRv6 DUT TX Util threshold was lowered to 0.73 because of bug #4527474
    TRIMMING_SRV6_DUT_TX_UTIL_TH = 0.73
    CL_TRIMMING_DUT_TX_UTIL_TH = 0.5
    # SPC6 trimming DUT TX Util threshold was lowered to 0.85 because of bug #5239628
    SPC6_TRIMMING_DUT_TX_UTIL_TH = 0.85
    BUFFER_CELL_SIZE = 192
    BUFFER_CELL_SIZE_SPC6 = 256
    BUFFER_CELL_SIZE_BY_CHIP_TYPE = {
        "SPC4": BUFFER_CELL_SIZE,
        "SPC5": BUFFER_CELL_SIZE,
        "SPC6": BUFFER_CELL_SIZE_SPC6
    }
    SPC5_POOL_SIZE_MB = 105
    SPC5_POOL_SIZE_BYTES = SPC5_POOL_SIZE_MB * 1000 * 1000
    SPC4_POOL_SIZE_BYTES = 158230080
    SPC5_POOL_SIZE_CELLS = SPC5_POOL_SIZE_BYTES / BUFFER_CELL_SIZE
    SPC4_POOL_SIZE_CELLS = SPC4_POOL_SIZE_BYTES / BUFFER_CELL_SIZE
    SPC6_POOL_SIZE_MB = 187
    SPC6_POOL_SIZE_BYTES = SPC6_POOL_SIZE_MB * 1000 * 1000
    SPC6_POOL_SIZE_CELLS = SPC6_POOL_SIZE_BYTES / BUFFER_CELL_SIZE_SPC6
    POOL_SIZE_CELLS_BY_CHIP_TYPE = {
        "SPC5": SPC5_POOL_SIZE_CELLS,
        "SPC4": SPC4_POOL_SIZE_CELLS,
        "SPC6": SPC6_POOL_SIZE_CELLS
    }
    FULL_MRC_DATA_PACKET_SIZE_BY_CHIP_TYPE = {
        "SPC4": 23,
        "SPC5": 23,
        "SPC6": 29
    }
    HALF_MRC_DATA_PACKET_SIZE_BY_CHIP_TYPE = {
        chip: size / 2 for chip, size in FULL_MRC_DATA_PACKET_SIZE_BY_CHIP_TYPE.items()
    }
    MANY_TO_ONE_MAX_QUEUE_BUILDUP_BY_CHIP_TYPE = {
        chip: size * 260 for chip, size in FULL_MRC_DATA_PACKET_SIZE_BY_CHIP_TYPE.items()
    }
    MAX_PACKETS_IN_QUEUE_LEAF = 55
    MAX_PACKETS_IN_QUEUE_SPINE = 65
    MANY_TO_FEW_MAX_QUEUE_BUILDUP_LEAF = (MAX_PACKETS_IN_QUEUE_LEAF * MRC_DATA_PACKET_SIZE) / BUFFER_CELL_SIZE
    MANY_TO_FEW_MAX_QUEUE_BUILDUP_SPINE = (MAX_PACKETS_IN_QUEUE_SPINE * MRC_DATA_PACKET_SIZE) / BUFFER_CELL_SIZE

    MANY_TO_ONE_TRAFFIC_TC_OCC_TH_BY_CHIP_TYPE = {
        chip: {
            EGRESS_PORT_GROUP_NAME: {ValidationConsts.MAX_WATERMARK: max_queue_buildup},
            INGRESS_PORT_GROUP_NAME: {},
        }
        for chip, max_queue_buildup in MANY_TO_ONE_MAX_QUEUE_BUILDUP_BY_CHIP_TYPE.items()
    }
    SPINE_MANY_TO_FEW_TRAFFIC_TC_OCC_TH = {EGRESS_PORT_GROUP_NAME: {ValidationConsts.MAX_WATERMARK: MANY_TO_FEW_MAX_QUEUE_BUILDUP_SPINE},
                                           INGRESS_PORT_GROUP_NAME: {}}
    LEAF_MANY_TO_FEW_TRAFFIC_TC_OCC_TH = {EGRESS_PORT_GROUP_NAME: {ValidationConsts.MAX_WATERMARK: MANY_TO_FEW_MAX_QUEUE_BUILDUP_LEAF},
                                          INGRESS_PORT_GROUP_NAME: {}}
    OCC_TH_DICT_BY_CHIP_TYPE = {
        chip: {
            ValidationConsts.OCC_AVG: size / 2,
            ValidationConsts.OCC_99: size,
        }
        for chip, size in FULL_MRC_DATA_PACKET_SIZE_BY_CHIP_TYPE.items()
    }
    VICTIM_FLOW_OCC_AVG_TH_BY_CHIP_TYPE = {
        "SPC4": 13,
        "SPC5": 13,
        "SPC6": 16,
    }
    MAX_WATERMARK_BY_ALPHA_TOLERANCE = 0.1
    ECN_COUNTERS = [f'tx_ecn_marked_tc_{tc}' for tc in range(PerfConsts.TC_NUM)]
    COUNTERS_WITH_ECN = PerfConsts.COUNTERS + ECN_COUNTERS
    MRC1_DSCP = 1
    MRC1_RTT_DSCP = 2
    MRC2_DSCP = 3
    MRC2_RTT_DSCP = 4
    MRC1_RETRANSMISSION_DSCP = 5
    MRC2_RETRANSMISSION_DSCP = 6
    CNP_DSCP = 32
    SACK_DSCP = 36
    NACK_DSCP = 33
    PROBE_ACK_DSCP = 31
    ROCE_ACK_DSCP = 30
    MRC_TRIMMED_DSCP = 11
    GFP_CONTROL_DSCP = 17
    GFP_DATA_DSCP = 41
    MRC_TRIMMED_TC = 4
    OPT_TS = 'OPT_TS'
    OPT_TS_DEFAULT = 256
    OPT_TS_DEFAULT_SPC6 = 256
    MINIMAL_TRIM_SIZE = 256
    MAX_TRIM_SIZE_CHECKING_RANGE = 512
    TRIMMING_TC = '4'
    MRC1_DATA_TC = '1'
    MRC2_DATA_TC = '2'
    MRC_RETRANSMISSION_TC = '3'
    PG_LIST = [0]
    # Ingress PG frames summed into the total occupancy comparison. A priority group is
    # split between a reserved headroom and a dynamically borrowed shared pool, and chips
    # divide the same load between them differently: SPC4 keeps most of it in the shared
    # pool while SPC6 absorbs it in the headroom. Summing both makes the comparison
    # independent of that split. Requires the HFT collector, which is the only path that
    # populates the headroom frame.
    PG_TOTAL_OCC_FRAMES = [ValidationConsts.PG_HEADROOM_DATAFRAME, ValidationConsts.PG_BUFFER_DATAFRAME]
    TRIMMING_ELEGABLE_QUEUE_NUM = [int(MRC1_DATA_TC), int(MRC2_DATA_TC), int(MRC_RETRANSMISSION_TC)]
    TRIMMING_DATA_QUEUE_NUM = [int(MRC1_DATA_TC), int(MRC2_DATA_TC)]
    MRC_RETRANSMISSION_TC_NUM = [int(MRC_RETRANSMISSION_TC)]
    MRC_RETRANSMISSION_TC_OCC_AVG_THRESHOLD = 44
    TRIMMING_QUEUE_NUM = [int(TRIMMING_TC)]
    MRC_CONTROL_TC = '4'
    GFP_DATA_TC = '5'

    @staticmethod
    def get_opt_ts_default(chip_type):
        """Return the default optimal trimming size for the ASIC generation."""
        return MRCConsts.OPT_TS_DEFAULT_SPC6 if chip_type == "SPC6" else MRCConsts.OPT_TS_DEFAULT

    WORKLOAD_1_TC_LIST = [int(MRC1_DATA_TC), int(MRC2_DATA_TC), int(MRC_RETRANSMISSION_TC), int(TRIMMING_TC)]
    WORKLOAD_2_TC_LIST = [int(MRC1_DATA_TC), int(MRC2_DATA_TC), int(MRC_RETRANSMISSION_TC), int(TRIMMING_TC), int(GFP_DATA_TC)]

    ETS_TC_LIST = [int(MRC1_DATA_TC), int(MRC2_DATA_TC), int(TRIMMING_TC)]
    ETS_VICTEM_FLOW_TC_LIST = [int(MRC1_DATA_TC), int(MRC2_DATA_TC)]
    MRC_DATA_ONLY_WORKLOAD_TC_LIST = [int(MRC2_DATA_TC)]
    WORKLOAD1_NAME = 'workload_1'
    WORKLOAD2_NAME = 'workload_2'
    MRC1_DATA_ONLY_WORKLOAD_NAME = 'mrc1_data_only'
    MRC2_DATA_ONLY_WORKLOAD_NAME = 'mrc2_data_only'
    MRC_REGRESSION_WORKLOADS_LIST = [WORKLOAD1_NAME]
    SHAPER_VALUE = 0.975
    BEFORE_TEST_SHAPER_VALUE = 0.8
    SHAPER_VALUE_AFTER_TEST = 1.0
    TC_OCC_ALLOWED_DEVIATION = 0.15
    TC_OCC_ALLOWED_DEVIATION_BISECTION = 1

    # Expected SRv6 egress TC occAvg overhead over the IPv6 reference, in buffer cells.
    # SRv6 uN costs an extra SID lookup and destination rewrite, which lengthens buffer
    # residence (~45 ns/packet on SPC4). Measured +2.08..+2.66 cells over 352 regression
    # observations on SPC4/NVUE. SPC5 and SPC6 overheads fall inside the +-1 cell tolerance,
    # so they stay at 0 and their comparison band is unchanged.
    # SPC4 has no SONiC coverage: get_srv6_tests_skip_condition skips SPC4 under SonicCli.
    # Re-validate the SPC4 value if that skip is ever removed.
    BISECTION_SRV6_OCC_OVERHEAD_BY_CHIP_TYPE = {
        "SPC4": 2.4,
        "SPC5": 0,
        "SPC6": 0,
    }

    # MRC1 + MRC2 data packets per stream for many-to-one SRv6 tests (workload_1 default is 6).
    MANY_TO_ONE_MRC_NUM_PACKETS_BY_CHIP_TYPE = {
        "SPC6": 15,
    }

    @staticmethod
    def get_many_to_one_mrc_num_packets(chip_type):
        """Return MRC data packet count for many-to-one, or None to use workload_1 default (6)."""
        return MRCConsts.MANY_TO_ONE_MRC_NUM_PACKETS_BY_CHIP_TYPE.get(chip_type)

    # Drop Over Max constants per chip type
    DROP_OVER_MAX_BY_CHIP_TYPE = {
        "SPC5": {
            "packet_size": 3072,
            "mrc_num_packets": 10,
            "num_dummy_acls": 25,
        },
        # TODO: confirm correct Drop Over Max params for SPC6 with arch (seeded from SPC5)
        "SPC6": {
            "packet_size": 3072,
            "mrc_num_packets": 10,
            "num_dummy_acls": 25,
        },
    }

    # Minimum bisection data packet params per chip type and per traffic type.
    # Only SRv6 traffic has a measured minimum; a chip type or traffic type absent here is skipped by the test.
    MIN_PACKET_SIZE_BY_CHIP_TYPE = {
        "SPC5": {
            TRAFFIC_TYPE_SRV6: MinPacketSizeConfig(packet_size=462, mrc_num_packets=66, retransmit_num_packets=13),
        },
        "SPC6": {
            TRAFFIC_TYPE_SRV6: MinPacketSizeConfig(packet_size=1496, mrc_num_packets=23, retransmit_num_packets=4),
        },
    }


class NoSrhPerfConsts:
    """Constants for SRv6 no-SRH shift-left performance tests (Feature #4894969)."""

    # Full supported packet-size sweep. Default parametrization in
    # test_srv6_no_srh_perf.py runs only the per-chip minimum for the
    # all-ports scenario (see ALL_PORTS_MIN_PACKET_SIZE_BY_CHIP_TYPE);
    # override CHOSEN_PACKET_SIZES_ENTROPY_COMPARISON in that file to sweep
    # this full list (or any custom subset).
    PACKET_SIZE_SWEEP_LIST = [370, 512, 1024, 2048, 4096]

    # Per-chip minimum packet size for the all-ports bisection scenario.
    # SPC6 (200G/lane) needs 1024B to sustain line rate on all-ports.
    ALL_PORTS_MIN_PACKET_SIZE_BY_CHIP_TYPE = {"SPC4": 512, "SPC5": 512, "SPC6": 1024}

    ENTROPY_HIGH = "high_entropy"
    ENTROPY_LOW = "low_entropy"

    TRAFFIC_TYPE_SRV6_NO_SRH = "SRv6_no_SRH"

    PORT_SPLITS_BY_CHIP_TYPE = {
        "SPC4": [(2, 2), (4, 4)],
        "SPC5": [(1, 1), (2, 2), (4, 4)],
        "SPC6": [(1, 1), (4, 4)],
    }

    BW_EQUIVALENCE_TOLERANCE = 0.02

    SUPPORTED_CHIP_TYPES = ["SPC4", "SPC5", "SPC6"]

    NO_SRH_DATA_DSCP = 0

    NO_SRH_NUM_DATA_PACKETS = 10

    NO_SRH_PACKETS_PER_DIP_SCALING_EXPONENT = 0.55

    # Faster ports need more in-flight packets to reach line rate. Packets per DIP are also scaled by
    # (port speed / port speed of the chip's lowest split) ** this exponent.
    NO_SRH_PACKETS_PER_DIP_SPEED_EXPONENT = 0.6

    # Keys are lanes per port; the largest key is the cage width in lanes.
    # On Cumulus it sets ports per cage (cage width // lanes per port), so it must match the platform.
    SONIC_BREAKOUT_MODE_BY_CHIP_AND_SPLIT = {
        "SPC4": {1: "8x100G[50G]", 2: "4x200G[100G]", 4: "2x400G[200G]", 8: "1x800G[400G]"},
        "SPC5": {1: "8x100G[50G]", 2: "4x200G[100G]", 4: "2x400G[200G]", 8: "1x400G"},
        "SPC6": {1: "8x200G[100G]", 2: "4x400G[200G]", 4: "2x800G[400G]", 8: "1x800G"},
    }


class PowerConsts:
    CONTROLLER_REGEX = r'\w*\d*-i2c-\d*-\d*\w*'
    POWER_SUPPLY = "powerSupply"
    POWER_SUPPLY_ADDRESS = "address"
    POWER_VOLTAGE = "voltage"
    POWER_CURRENT = "currentAmp"
    POWER_WATT = "powerWatt"
    TOTAL_POWER = "Total Power"
    POWER_DOMAIN = "domain"


class RailDomain:
    """Electrical domain a power rail belongs to.

    Separates ASIC (silicon) rails from Switch/Board rails so reports and power
    totals never mix or double-count the two domains.
    """
    ASIC = "ASIC"
    BOARD = "Board"


@dataclass(frozen=True)
class RailDef:
    """Declarative definition of a single power rail (one controller output).

    Single source of truth for a rail's identity, intended to replace the
    scattered address dicts, label-inference markers and threshold regexes.
    Kept chip-agnostic so other chip types (SPC4/SPC5/...) can adopt the same
    model in future refactors.

    Attributes:
        canonical_name (str): Org-aligned rail name (e.g. ``"VDD_Tile"``, ``"HVDD"``).
        domain (str): Rail domain, one of :class:`RailDomain` (``ASIC``/``Board``).
        address (str): I2C device address in ``0x..`` form (e.g. ``"0x60"``).
        buses (Tuple[int, ...]): I2C bus numbers (branches) the address is valid
            on. An address can repeat across branches, so the bus distinguishes
            the branch and separates ASIC buses from board buses that reuse the
            same address.
        power_threshold_watt (Optional[float]): Explicit canonical-GROUP budget
            in watts (used as-is), or ``None`` when the group budget is instead
            derived from ``per_leg_power_threshold_watt``. Used for Arch-given
            group budgets and SPC6 placeholders (see ``is_placeholder_threshold``).
        per_leg_power_threshold_watt (Optional[float]): Legacy PER-LEG cap in
            watts. When set, the canonical-group budget is derived as this value
            times ``active_legs``, summed across every controller sharing the
            canonical name. Preserves the legacy per-leg validation intent while
            enforcing at the group level (SPC4/SPC5).
        active_legs (int): Number of real output legs this controller contributes
            to its canonical group (dual-output rails = 2; single-output rails
            such as VCORE MAIN / VDDSCC = 1). Only used together with
            ``per_leg_power_threshold_watt``.
        label_regex (Optional[str]): Optional token pattern for descriptive/bench
            ``sensors`` dumps (e.g. the Arch stress-tool ``STRESS_<RAIL>`` labels).
            Address is the primary key; this is a fallback for label-only dumps.
        description (str): One-line responsibility summary (feeds the rail wiki).
        is_placeholder_threshold (bool): ``True`` while ``power_threshold_watt``
            is a placeholder that is not yet validated, awaiting Arch calibration.
        is_upstream_feed (bool): ``True`` for an upstream input converter (e.g. a
            PDB CONV / HSC) whose output feeds downstream leaf rails. Such rails
            are excluded from domain power totals to avoid double-counting.
    """
    canonical_name: str
    domain: str
    address: str
    buses: Tuple[int, ...]
    power_threshold_watt: Optional[float] = None
    per_leg_power_threshold_watt: Optional[float] = None
    active_legs: int = 2
    label_regex: Optional[str] = None
    description: str = ""
    is_placeholder_threshold: bool = False
    is_upstream_feed: bool = False


class RailModel:
    """Chip-type to declarative rail table (single source of truth for identity).

    New chip types are added by defining their rail list and registering it in
    :attr:`RAILS_BY_CHIP_TYPE`; parsing, naming, grouping and thresholds can then
    all be derived generically from these definitions.
    """

    # SPC6 (Salamandra) exposes a single ASIC. Its BGA dynamic/controlled rails
    # are driven by MPS ``M29816`` controllers on two i2c branches:
    #   branch 1 = i2c-15, branch 2 = i2c-16 (confirmed against a live dump).
    # At the canonical level an address maps to the same rail on both branches
    # (e.g. 0x63/0x64 => DVDD), so the branch is only needed to (a) place the
    # branch-unique rails (VDD_main on 16, HVDD on 15) and (b) separate ASIC
    # buses from board buses that reuse the same address (e.g. 0x60 on i2c-6/7
    # is a board PDB converter, not VDD_main).
    #
    # NOTE: the ASIC controllers on these buses also drive the front-panel OSFP
    # optics supply (0x68/0x69, see below). Rail domain (ASIC vs Board) is
    # therefore taken per-rail from this table, never inferred from the bus.
    SPC6_ASIC_BUSES = (15, 16)

    # ``power_threshold_watt`` below is a rail-GROUP budget (the sum of all the
    # rail's tile legs), taken from the Arch SPC6 power design at the worst/max
    # column across voltage-temp corners. Still placeholders pending Arch
    # sign-off; two known caveats: (1) HVDD design max (~61 W) is below an
    # observed run (~65 W) - open with Arch; (2) "DVDD CPO" (a separate ~160 W
    # rail in the design) is not modeled yet (i2c address TBD).
    SPC6_RAILS = [
        RailDef(
            canonical_name="VDD_main",
            domain=RailDomain.ASIC,
            address="0x60",
            buses=(16,),
            power_threshold_watt=540,
            label_regex=r"_STRESS_VDD_rail\d+",
            description="Main core supply (VDD_M) feeding the ASIC core.",
        ),
        RailDef(
            canonical_name="VDD_Tile",
            domain=RailDomain.ASIC,
            address="0x61",
            buses=SPC6_ASIC_BUSES,
            power_threshold_watt=276,
            label_regex=r"_STRESS_VDD_T\d+_rail\d+",
            description="Per-tile core supply (VDD_Tx) for the ASIC tiles.",
        ),
        RailDef(
            canonical_name="VDD_Tile",
            domain=RailDomain.ASIC,
            address="0x62",
            buses=SPC6_ASIC_BUSES,
            power_threshold_watt=276,
            label_regex=r"_STRESS_VDD_T\d+_rail\d+",
            description="Per-tile core supply (VDD_Tx) for the ASIC tiles.",
        ),
        RailDef(
            canonical_name="DVDD",
            domain=RailDomain.ASIC,
            address="0x63",
            buses=SPC6_ASIC_BUSES,
            power_threshold_watt=188,
            label_regex=r"_STRESS_DVDD_T\d+_rail\d+",
            description="Digital core supply (DVDD) for the ASIC tiles.",
        ),
        RailDef(
            canonical_name="DVDD",
            domain=RailDomain.ASIC,
            address="0x64",
            buses=SPC6_ASIC_BUSES,
            power_threshold_watt=188,
            label_regex=r"_STRESS_DVDD_T\d+_rail\d+",
            description="Digital core supply (DVDD) for the ASIC tiles.",
        ),
        RailDef(
            canonical_name="AVDD",
            domain=RailDomain.ASIC,
            address="0x65",
            buses=SPC6_ASIC_BUSES,
            power_threshold_watt=185,
            label_regex=r"_STRESS_AVDD_T\d+_rail\d+",
            description="Analog supply (AVDD) for the ASIC tiles.",
        ),
        RailDef(
            canonical_name="AVDD",
            domain=RailDomain.ASIC,
            address="0x66",
            buses=SPC6_ASIC_BUSES,
            power_threshold_watt=185,
            label_regex=r"_STRESS_AVDD_T\d+_rail\d+",
            description="Analog supply (AVDD) for the ASIC tiles.",
        ),
        RailDef(
            canonical_name="VDDHBID",
            domain=RailDomain.ASIC,
            address="0x67",
            buses=SPC6_ASIC_BUSES,
            power_threshold_watt=220,
            label_regex=r"_STRESS_VDDHBID_T\d+(?:_T\d+)?_rail\d+",
            description="HBM/interface supply (VDDHBID) shared across tile pairs.",
        ),
        RailDef(
            canonical_name="HVDD",
            domain=RailDomain.ASIC,
            address="0x6a",
            buses=(15,),
            power_threshold_watt=72,
            label_regex=r"_STRESS_HVDD_T\d+(?:_\d+)?_rail\d+",
            description="Fixed 1.2 V high-voltage supply (HVDD) for tile groups.",
        ),
        RailDef(
            canonical_name="OSFP",
            domain=RailDomain.BOARD,
            address="0x68",
            buses=SPC6_ASIC_BUSES,
            power_threshold_watt=None,
            label_regex=r"OSFP",
            description="Front-panel OSFP optics/PHY supply (OSFP x16).",
        ),
        RailDef(
            canonical_name="OSFP",
            domain=RailDomain.BOARD,
            address="0x69",
            buses=SPC6_ASIC_BUSES,
            power_threshold_watt=None,
            label_regex=r"OSFP",
            description="Front-panel OSFP optics/PHY supply (OSFP x16).",
        ),
        # --- SPC6 Board / upstream rails (domain=Board) -------------------
        # Not ASIC silicon; modeled for canonical naming + clean domain totals.
        # Bus scoping is REQUIRED: 0x60 is VDD_main on i2c-16 (ASIC) but a PDB
        # brick on i2c-6/7 (board). PDB/HSC are 48V->12V upstream feeds (their
        # output equals all downstream power), so is_upstream_feed excludes them
        # from domain totals to avoid double-counting the leaf rails they supply.
        # NOTE: on Sonic the HSC yields dataframe rows (input-side power); on DVS
        # it produced none. is_upstream_feed excludes it from domain totals in
        # either case, so it never double-counts the leaf rails it supplies.
        RailDef(
            canonical_name="PDB Converter",
            domain=RailDomain.BOARD,
            address="0x60",
            buses=(6, 7),
            power_threshold_watt=None,
            label_regex=r"PDB-\d+\s+CONV",
            description="PDB 48V->12V input converter (upstream feed).",
            is_upstream_feed=True,
        ),
        RailDef(
            canonical_name="PDB HSC",
            domain=RailDomain.BOARD,
            address="0x12",
            buses=(6, 7),
            power_threshold_watt=None,
            label_regex=r"PDB-\d+\s+HSC",
            description="PDB hot-swap controller input stage (upstream feed).",
            is_upstream_feed=True,
        ),
        RailDef(
            canonical_name="DDR (VDD_MEM)",
            domain=RailDomain.BOARD,
            address="0x6a",
            buses=(5,),
            power_threshold_watt=None,
            label_regex=r"DDR PMIC",
            description="DDR memory supply (VDD_MEM / VDD_MEMIO).",
        ),
        RailDef(
            canonical_name="CPU PMIC (VDDCR)",
            domain=RailDomain.BOARD,
            address="0x69",
            buses=(5,),
            power_threshold_watt=None,
            label_regex=r"CPU PMIC",
            description="Host CPU core/SoC supply (VDDCR_CPU / VDDCR_SOC).",
        ),
    ]

    # --- Legacy ASIC chips (SPC3/4/5) -------------------------------------
    # These keep their EXISTING addresses; only the label format is cleaned
    # (encapsulated detail moved to `description`). All rails are ASIC-domain.
    # `buses=()` means "match on address regardless of i2c bus" (single
    # controller bank, unlike SPC6's dual branch).
    #
    # Per Arch, threshold validation is CANONICAL-GROUP level for every chip
    # (sum every leg sharing a canonical rail name). The legacy POWER_TH_PER_ASIC
    # values are PER-LEG caps (each dataframe row is one leg / vout channel), so
    # the group budget is derived as per_leg_power_threshold_watt * active_legs,
    # summed across a rail's controllers. This reproduces the legacy HVDD group
    # aggregate (e.g. SPC5 222*2=444, SPC4 124*2=248) and stays above the observed
    # measurements. Dual-output rails have active_legs=2; single-output rails
    # (VCORE MAIN, VDDSCC) active_legs=1.
    SPC4_RAILS = [
        # HVDD: only controller 0x6c is populated on these boards (2 legs, ~110 W
        # each => group 124*2=248, matching the legacy HVDD_TILES_TH aggregate and
        # the Arch spec HVDD total of 221 W). The legacy map also listed 0x61
        # (HVDD_T47) but it never appears in live dumps.
        # TODO(Arch): confirm 0x61 is unpopulated on all SPC4 board variants.
        RailDef(canonical_name="HVDD", domain=RailDomain.ASIC, address="0x6c", buses=(),
                per_leg_power_threshold_watt=124, active_legs=2,
                description="HVDD tiles (HVDD_T03 + HVDD_T47), 2 legs."),
        RailDef(canonical_name="VCORE MAIN", domain=RailDomain.ASIC, address="0x62", buses=(),
                per_leg_power_threshold_watt=345, active_legs=1,
                description="Main core supply (VDD_M), single leg."),
        RailDef(canonical_name="VCORE TILES", domain=RailDomain.ASIC, address="0x63", buses=(),
                per_leg_power_threshold_watt=17, active_legs=2, description="Core tiles 0 & 1 (VDD_Tx)."),
        RailDef(canonical_name="VCORE TILES", domain=RailDomain.ASIC, address="0x64", buses=(),
                per_leg_power_threshold_watt=17, active_legs=2, description="Core tiles 2 & 3 (VDD_Tx)."),
        RailDef(canonical_name="VCORE TILES", domain=RailDomain.ASIC, address="0x65", buses=(),
                per_leg_power_threshold_watt=17, active_legs=2, description="Core tiles 4 & 5 (VDD_Tx)."),
        RailDef(canonical_name="VCORE TILES", domain=RailDomain.ASIC, address="0x66", buses=(),
                per_leg_power_threshold_watt=17, active_legs=2, description="Core tiles 6 & 7 (VDD_Tx)."),
        RailDef(canonical_name="DVDD", domain=RailDomain.ASIC, address="0x67", buses=(),
                per_leg_power_threshold_watt=18.13, active_legs=2, description="Digital tiles 0 & 1 (DVDD_Tx)."),
        RailDef(canonical_name="DVDD", domain=RailDomain.ASIC, address="0x68", buses=(),
                per_leg_power_threshold_watt=18.13, active_legs=2, description="Digital tiles 2 & 3 (DVDD_Tx)."),
        RailDef(canonical_name="DVDD", domain=RailDomain.ASIC, address="0x69", buses=(),
                per_leg_power_threshold_watt=18.13, active_legs=2, description="Digital tiles 4 & 5 (DVDD_Tx)."),
        RailDef(canonical_name="DVDD", domain=RailDomain.ASIC, address="0x6a", buses=(),
                per_leg_power_threshold_watt=18.13, active_legs=2, description="Digital tiles 6 & 7 (DVDD_Tx)."),
        RailDef(canonical_name="VDDSCC", domain=RailDomain.ASIC, address="0x6e", buses=(),
                per_leg_power_threshold_watt=47, active_legs=1, description="SerDes/SCC supply (VDDSCC), single leg."),
    ]

    # SPC5: same per-leg * active_legs derivation. HVDD is a single controller
    # (0x6c) driving 2 legs (HVDD_T03 + HVDD_T47) => 222*2=444.
    SPC5_RAILS = [
        RailDef(canonical_name="HVDD", domain=RailDomain.ASIC, address="0x6c", buses=(),
                per_leg_power_threshold_watt=222, active_legs=2,
                description="HVDD tiles (HVDD_T03 + HVDD_T47), 2 legs."),
        RailDef(canonical_name="VCORE MAIN", domain=RailDomain.ASIC, address="0x62", buses=(),
                per_leg_power_threshold_watt=310, active_legs=1,
                description="Main core supply (VDD_M), single leg."),
        RailDef(canonical_name="VCORE TILES", domain=RailDomain.ASIC, address="0x63", buses=(),
                per_leg_power_threshold_watt=28.5, active_legs=2, description="Core tiles 0 & 1 (VDD_Tx)."),
        RailDef(canonical_name="VCORE TILES", domain=RailDomain.ASIC, address="0x64", buses=(),
                per_leg_power_threshold_watt=28.5, active_legs=2, description="Core tiles 2 & 3 (VDD_Tx)."),
        RailDef(canonical_name="VCORE TILES", domain=RailDomain.ASIC, address="0x65", buses=(),
                per_leg_power_threshold_watt=28.5, active_legs=2, description="Core tiles 4 & 5 (VDD_Tx)."),
        RailDef(canonical_name="VCORE TILES", domain=RailDomain.ASIC, address="0x66", buses=(),
                per_leg_power_threshold_watt=28.5, active_legs=2, description="Core tiles 6 & 7 (VDD_Tx)."),
        RailDef(canonical_name="DVDD", domain=RailDomain.ASIC, address="0x67", buses=(),
                per_leg_power_threshold_watt=30.75, active_legs=2, description="Digital tiles 0 & 1 (DVDD_Tx)."),
        RailDef(canonical_name="DVDD", domain=RailDomain.ASIC, address="0x68", buses=(),
                per_leg_power_threshold_watt=30.75, active_legs=2, description="Digital tiles 2 & 3 (DVDD_Tx)."),
        RailDef(canonical_name="DVDD", domain=RailDomain.ASIC, address="0x69", buses=(),
                per_leg_power_threshold_watt=30.75, active_legs=2, description="Digital tiles 4 & 5 (DVDD_Tx)."),
        RailDef(canonical_name="DVDD", domain=RailDomain.ASIC, address="0x6a", buses=(),
                per_leg_power_threshold_watt=30.75, active_legs=2, description="Digital tiles 6 & 7 (DVDD_Tx)."),
        RailDef(canonical_name="VDDSCC", domain=RailDomain.ASIC, address="0x6e", buses=(),
                per_leg_power_threshold_watt=48, active_legs=1, description="SerDes/SCC supply (VDDSCC), single leg."),
    ]

    # SPC3 has no power thresholds today (power validation stays disabled). Kept
    # for naming parity in the unified resolver; thresholds intentionally None.
    SPC3_RAILS = [
        RailDef(canonical_name="VCORE MAIN", domain=RailDomain.ASIC, address="0x62", buses=(),
                description="Main core supply."),
        RailDef(canonical_name="1.8V_MAIN & 1.2V_MAIN", domain=RailDomain.ASIC, address="0x64", buses=(),
                description="1.8V/1.2V main rails."),
        RailDef(canonical_name="VCORE & 1.8V_Tile", domain=RailDomain.ASIC, address="0x66", buses=(),
                description="Core + 1.8V tile rail."),
        RailDef(canonical_name="VCORE & 1.8V_Tile", domain=RailDomain.ASIC, address="0x68", buses=(),
                description="Core + 1.8V tile rail."),
        RailDef(canonical_name="VCORE & 1.8V_Tile", domain=RailDomain.ASIC, address="0x6a", buses=(),
                description="Core + 1.8V tile rail."),
        RailDef(canonical_name="VCORE & 1.8V_Tile", domain=RailDomain.ASIC, address="0x6c", buses=(),
                description="Core + 1.8V tile rail."),
        RailDef(canonical_name="VCORE & 1.8V_Tile", domain=RailDomain.ASIC, address="0x6e", buses=(),
                description="Core + 1.8V tile rail."),
    ]

    RAILS_BY_CHIP_TYPE = {
        "SPC3": SPC3_RAILS,
        "SPC4": SPC4_RAILS,
        "SPC5": SPC5_RAILS,
        "SPC6": SPC6_RAILS,
    }

    # ASIC-domain total power budget (watts) per chip type, compared against the
    # summed ASIC-domain rails in _validate_asic_total_power. Chips absent here
    # (e.g. SPC3) have no total check, so their power validation stays disabled.
    ASIC_TOTAL_POWER_TH_WATT = {
        "SPC4": 754,
        "SPC5": 811,
        "SPC6": 1500,
    }

    @classmethod
    def rails_for_chip_type(cls, chip_type: str) -> List[RailDef]:
        """Return the declarative rail list for a chip type.

        Args:
            chip_type (str): Chip identifier such as ``"SPC6"``.

        Returns:
            List[RailDef]: Rail definitions for the chip type, or an empty list
            when the chip type has not been migrated to the declarative model.
        """
        return cls.RAILS_BY_CHIP_TYPE.get(chip_type, [])

    @classmethod
    def asic_total_threshold(cls, chip_type: str) -> Optional[float]:
        """Return the ASIC-domain total power budget (watts) for a chip type.

        Args:
            chip_type (str): Chip identifier such as ``"SPC6"``.

        Returns:
            Optional[float]: Total budget in watts, or ``None`` when the chip has
            no total check (power validation stays disabled, e.g. SPC3).
        """
        return cls.ASIC_TOTAL_POWER_TH_WATT.get(chip_type)

    @classmethod
    def get_rail_def(cls, chip_type: str, bus: Optional[int], address: Optional[str]) -> Optional[RailDef]:
        """Return the rail definition matching an ``(i2c bus, address)`` pair.

        For chips whose addresses repeat across branches (e.g. SPC6), the rail's
        ``buses`` tuple is required and both bus and address must match. For
        single-bank legacy chips whose rails set ``buses=()``, matching is
        address-only (bus is ignored).

        Args:
            chip_type (str): Chip identifier such as ``"SPC6"``.
            bus (Optional[int]): I2C bus number (branch) the chip sits on.
            address (Optional[str]): I2C device address in ``0x..`` form.

        Returns:
            Optional[RailDef]: The matching rail definition, or ``None`` when the
            address (and bus, when constrained) is unknown.
        """
        if not address:
            return None
        for rail in cls.rails_for_chip_type(chip_type):
            if rail.address == address and (not rail.buses or bus in rail.buses):
                return rail
        return None

    @classmethod
    def power_thresholds_by_rail(cls, chip_type: str) -> Dict[str, float]:
        """Return the canonical-group power budget per rail for a chip type.

        Two modes are supported so chip types can be mixed cleanly:

        * Per-leg rails (SPC4/SPC5): the group budget is derived as
          ``per_leg_power_threshold_watt * active_legs``, SUMMED across every
          controller sharing the canonical name.
        * Explicit rails (SPC6 placeholders / Arch-given group budgets): the
          ``power_threshold_watt`` value is taken as the whole-group budget
          (once per canonical name, not summed).

        Args:
            chip_type (str): Chip identifier such as ``"SPC6"``.

        Returns:
            Dict[str, float]: Canonical-group power budgets keyed by canonical name.
        """
        derived: Dict[str, float] = {}
        explicit: Dict[str, float] = {}
        for rail in cls.rails_for_chip_type(chip_type):
            if rail.per_leg_power_threshold_watt is not None:
                contribution = rail.per_leg_power_threshold_watt * rail.active_legs
                derived[rail.canonical_name] = derived.get(rail.canonical_name, 0.0) + contribution
            elif rail.power_threshold_watt is not None:
                explicit[rail.canonical_name] = rail.power_threshold_watt
        return {**explicit, **derived}

    @classmethod
    def asic_buses(cls, chip_type: str) -> set:
        """Return the set of i2c buses carrying ASIC-domain rails for a chip type.

        Derived from the rail table so callers never hardcode bus numbers. Used
        to decide whether an unmapped controller sits on an ASIC branch (worth a
        coverage warning) versus a board branch (intentionally not modeled).
        Legacy single-bank chips whose rails set ``buses=()`` yield an empty set.

        Args:
            chip_type (str): Chip identifier such as ``"SPC6"``.

        Returns:
            set: I2C bus numbers used by the chip's ASIC-domain rails.
        """
        return {bus for rail in cls.rails_for_chip_type(chip_type)
                if rail.domain == RailDomain.ASIC for bus in rail.buses}

    @classmethod
    def asic_canonical_names(cls, chip_type: str) -> set:
        """Return the set of ASIC-domain canonical rail names for a chip type.

        Used to classify a report row's domain from its name alone (rows not in
        this set are treated as Board/Switch).

        Args:
            chip_type (str): Chip identifier such as ``"SPC6"``.

        Returns:
            set: Canonical names whose domain is :attr:`RailDomain.ASIC`.
        """
        return {rail.canonical_name for rail in cls.rails_for_chip_type(chip_type)
                if rail.domain == RailDomain.ASIC}

    @classmethod
    def upstream_feed_names(cls, chip_type: str) -> set:
        """Return the canonical names of upstream feed rails for a chip type.

        Upstream feeds (e.g. PDB CONV / HSC) are excluded from domain power
        totals to avoid double-counting the downstream leaf rails they supply.

        Args:
            chip_type (str): Chip identifier such as ``"SPC6"``.

        Returns:
            set: Canonical names whose ``is_upstream_feed`` flag is ``True``.
        """
        return {rail.canonical_name for rail in cls.rails_for_chip_type(chip_type)
                if rail.is_upstream_feed}
