from enum import Enum


class StatsConsts:
    class State(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'

    # Sleep durations
    SLEEP_15_SECONDS = 15  # [sec]
    SLEEP_20_SECONDS = 20  # [sec]
    SLEEP_40_SECONDS = 40  # [sec]
    SLEEP_1_MINUTE = 60  # [sec]
    SLEEP_3_MINUTES = 180  # [sec]
    SLEEP_5_MINUTES = 300  # [sec]

    # Category configuration keys and defaults
    STATE = 'state'
    STATE_DEFAULT = State.ENABLED.value
    INTERVAL = 'interval'
    INTERVAL_DEFAULT = '5'  # [min]
    INTERVAL_MIN = '1'  # [min]
    HISTORY_DURATION = 'history-duration'
    HISTORY_DURATION_DEFAULT = '365'  # [days]
    HISTORY_DURATION_MIN = '1'  # [days]
    GENERATE_ALL_TIME_MAX = 2  # [sec]
    CATEGORY_STATE_DISABLED = {STATE: State.DISABLED.value}
    CATEGORY_MIN_DICT = {
        STATE: STATE_DEFAULT,
        INTERVAL: INTERVAL_MIN,
        HISTORY_DURATION: HISTORY_DURATION_MIN
    }
    CATEGORY_MIN_DISABLED_DICT = {
        STATE: State.DISABLED.value,
        INTERVAL: INTERVAL_MIN,
        HISTORY_DURATION: HISTORY_DURATION_MIN
    }

    # --- Counters Historical Analysis (new categories) ---
    INTERFACE = 'interface'
    PEER_PORT = 'peer-port'
    TRANSCEIVERS = 'transceivers'
    NEW_CATEGORIES = [INTERFACE, PEER_PORT, TRANSCEIVERS]

    # New-category defaults / ranges (per HLD: history-duration 1-7)
    # The default interval moved from 1 to 5 minutes; the minimum is what the
    # tests configure, so a wait for a sample stays inside a test's patience.
    NEW_INTERVAL_DEFAULT = '5'          # [min]
    NEW_HISTORY_DURATION_DEFAULT = '7'  # [days]
    NEW_HISTORY_DURATION_MIN = '1'      # [days]
    NEW_HISTORY_DURATION_MAX = '7'      # [days]
    NEW_INTERVAL_MIN = '1'              # [min]
    NEW_INTERVAL_MID = '720'            # [min]
    NEW_INTERVAL_MAX = '1440'           # [min]
    INVALID_NEW_HISTORY_DURATION_HIGH = 8   # above the new-category max (7)
    # 8 is inside the generic schema range (1-365), so the per-category limit is
    # only evaluated on apply, not on set.
    NEW_HISTORY_DURATION_APPLY_ERROR = 'Category {category}: history-duration must be between {min} and {max} days'

    # A category the platform supports can still have nothing to sample: a
    # transceiver cage with no module, a peer-port with no connected peer. The
    # category stays enabled and /var/stats/<category> is never created, and
    # generate is refused rather than producing an empty report. A platform that
    # cannot have the entity at all prunes the category instead.
    NO_SAMPLED_DATA_ERROR = 'does not have sampled data'

    # The peer-port stats category only has something to sample while the
    # peer-port telemetry feature is on, and the default platform config the
    # framework restores between tests leaves it off.
    # Matches the merged peer-port telemetry suite: keys are PEER_COUNTERS:<peer-id>,
    # and the colon keeps the count off anything that merely starts the same way.
    PEER_COUNTERS_KEY_PREFIX = 'PEER_COUNTERS:'
    PEER_PORT_COUNTER_KEYS_TIMEOUT = 180  # [sec]

    EXTERNAL_PATH = "/host/stats"
    # Counts every category's reports together, not per category.
    MAX_GENERATED_FILES = 3

    # Per category, newest first. Enforced as a file count, not a date window:
    # the HLD calls it "the last 3 days" and the two only agree without gaps.
    TECHSUPPORT_MAX_DAY_FILES = 3
    # More files than the cap, all inside a 7-day history-duration.
    TECHSUPPORT_SEED_FILE_AGES_DAYS = (2, 4, 6)
    TECHSUPPORT_SKIP_STATS_OPTION = 'skip-stats'
    TECHSUPPORT_SINCE_DATE_FORMAT = '%Y%m%d'

    NEW_CATEGORY_DEFAULT_DICT = {
        HISTORY_DURATION: NEW_HISTORY_DURATION_DEFAULT,
        INTERVAL: NEW_INTERVAL_DEFAULT,
        STATE: STATE_DEFAULT,
    }
    NEW_CATEGORY_DEFAULT_DISABLED_DICT = {
        HISTORY_DURATION: NEW_HISTORY_DURATION_DEFAULT,
        INTERVAL: NEW_INTERVAL_DEFAULT,
        STATE: State.DISABLED.value,
    }

    # Per-day internal file layout: /var/stats/<category>/<date>.csv (subdir, one file per day)
    # The daemon that samples the categories and writes the per-day files.
    SAMPLER_SERVICE = "stats-reportd"
    # CPU accounting for the sampler unit (cgroup v2 first, then v1)
    CGROUP_V2_CPU_STAT = "/sys/fs/cgroup/system.slice/{}.service/cpu.stat".format(SAMPLER_SERVICE)
    CGROUP_V1_CPUACCT = "/sys/fs/cgroup/cpuacct/system.slice/{}.service/cpuacct.usage".format(
        SAMPLER_SERVICE)

    INTERNAL_ROOT = "/var/stats"
    INTERNAL_CAT_DIR = "/var/stats/{category}"
    # Confirmed against the store on croc-61, juliet-144 and mamba-248 (-007..-009).
    PER_DAY_FILE_DATE_FORMAT = "%Y-%m-%d"
    PER_DAY_FILE_EXT = ".csv"

    # The new categories write one row per entity: column 0 is the Timestamp and
    # column 1 is the entity name (interface / peer-port / transceiver), so the
    # numeric counter values start at column 2.
    NEW_CATEGORY_FIRST_VALUE_COLUMN = 2

    # Columns of the new categories that hold a state enum instead of a number
    # (e.g. interface 'phy-manager-state' = RECEIVER_DETECT). Checked for
    # presence rather than numeric range. Add new ones here as they show up.
    NEW_CATEGORY_STATE_COLUMNS = {'phy-manager-state'}

    # peer-port carries a second identity column after the entity name: the switch
    # port the peer is attached to (e.g. 'sw50p1'), so its counters start one
    # column later than the other new categories.
    PEER_PORT_IDENTITY_COLUMNS = {'switch-port'}

    # File-size estimates (HLD 16.1: Rosalind 288 interfaces @ 1-min interval).
    # Sizing tests force interval 1 - the worst case, not the 5-minute default.
    PER_SAMPLE_KB = 82      # [KB] approximate size of one interface sample
    PER_DAY_MB_1MIN = 115   # [MB] approximate per-day file size at 1-min interval
    GZIP_RATIO = 3          # approximate gzip compression ratio

    # CPU limits (feature-owner measurement on rosalind-nvos-2182, 296 interface
    # ports, all categories @ 1-min interval, 5-min average window):
    #   single heaviest data-heavy category (interface) ~0.11% average
    #   all categories combined                          ~0.35% average
    # Limits kept above the measured worst case with headroom for per-tick peaks
    # (a few % on one core, averaged out over the window) so they still catch a
    # real regression.
    # Measured 2026-08-02 over the stats-reportd cgroup (both platforms agree
    # within a hundredth of a percent): mamba-248 read 0.052% with interface
    # alone, 0.073% with all new categories and 0.077% with every category at
    # interval 1; juliet-1221 sat slightly lower throughout. The limits allow for
    # a switch with roughly twice Mamba's port count plus per-tick peaks that the
    # window averages out.
    # All three gate against the feature's 1% budget, the way the RAM limits below
    # all gate against 200MB: the measured figures differ, the budget does not.
    CPU_LIMIT_SINGLE_PERCENT = 1.0    # measured 0.052% (interface)
    CPU_LIMIT_ALL_PERCENT = 1.0       # measured 0.073% (all new categories combined)
    CPU_MEASURE_WINDOW_SEC = 300      # 5-min average window (matches measurement)
    # Worst case: every category, legacy ones included, forced to interval 1.
    CPU_LIMIT_ALL_CATEGORIES_PERCENT = 1.0   # measured 0.077%

    # Resident memory of the stats-reportd unit (parent plus its worker). Measured
    # 75-77MB on both platforms in every configuration - it barely responds to how
    # many categories are enabled - and flat across an hour-long soak, so these
    # bound a leak rather than ordinary growth.
    RAM_LIMIT_SINGLE_MB = 200
    RAM_LIMIT_ALL_MB = 200
    RAM_LIMIT_ALL_CATEGORIES_MB = 200

    # Disk growth is extrapolated from a short window instead of sampling for a
    # whole day: measure /var/stats twice this far apart and project to 24h.
    DISK_GROWTH_WINDOW_SEC = 300
    SECONDS_PER_DAY = 86400
    BYTES_PER_MB = 1024 * 1024
    # Projected daily growth of the whole /var/stats tree with every category at
    # interval 1. Measured 74MB/day on mamba-248 (interface 49, transceivers 16,
    # peer-port 8, every legacy category under 0.4) and 36MB/day on juliet-1221,
    # which has neither peer-port nor transceiver modules; the limit leaves room
    # for a switch with roughly twice Mamba's port count.
    TOTAL_PER_DAY_MB = 300

    # Long-running / soak (short CI-safe default; raise for a real soak run)
    SCALE_SOAK_SECONDS = 3600       # TODO raise to 24-72h for a real soak run

    # Config-reflection (behavioral) test knobs
    SAMPLE_CADENCE_TOLERANCE_SEC = 15   # TODO confirm allowed sampling-cadence drift
    REFLECTION_INTERVAL_A = 1           # [min]
    REFLECTION_INTERVAL_B = 2           # [min]
    REFLECTION_HISTORY_DURATION = 2     # [days], inside the new-category range 1-7
    SEED_OLD_FILE_AGE_DAYS = 10         # older than the max history-duration (7), for prune test
    MERGE_SEED_FILE_AGES_DAYS = (1, 2)  # extra days to merge, within the history window
    HEALTH_SETTLE_TIMEOUT_SEC = 120     # how long a boot-time sensor reading may stay out of range

    # Log messages
    LOG_MSG_UNSET_STATS = "PATCH /nvue_v1/system/stats"
    LOG_MSG_SET_CATEGORY1 = "INFO stats-reportd: got config change "
    LOG_MSG_SET_CATEGORY2 = ": {'enabled': 'true', 'history_duration': '365', 'interval': '1'}"
    LOG_MSG_PATCH_CATEGORY = "PATCH /nvue_v1/system/stats/category/"
    LOG_MSG_ERROR_DB = "..."  # TODO: Update message (parameter not found in redis DB)...

    # Invalid/edge-case test inputs
    INVALID_CATEGORY_NAME = 'invalid_category_name'
    ALL_CATEGORIES = 'all'
    INVALID_STATE = 'invalid_state'
    INVALID_INTERVAL_LOW = 0
    INVALID_INTERVAL_HIGH = 1441
    INVALID_HISTORY_DURATION_LOW = 0
    INVALID_HISTORY_DURATION_HIGH = 366
    INVALID_FILE_NAME = 'file_not_exists.csv'
    INVALID_SHOW_CATEGORY = 'The requested item does not exist.'

    # File paths
    TEMP_PATH = '/auto/rdmzsysgwork/shared/test_utilities/tmp/5b5931e6aac04bd39499372ef73fbf31'
    INTERNAL_PATH = "/tmp"
    OLD_SAMPLES_PATH = "/auto/sw_system_project/NVOS_INFRA/verification/stats/old_samples/"
    BIG_FILE_PATH = "/auto/sw_system_project/NVOS_INFRA/verification/stats/big_file/"
    HUGE_FILE_PATH = "/auto/sw_system_project/NVOS_INFRA/verification/stats/huge_file/"
    NO_HEADER_FILE_PATH = "/auto/sw_system_project/NVOS_INFRA/verification/stats/no_header_file/"
    MAX_SIZE_FILE_PATH = "/auto/sw_system_project/NVOS_INFRA/verification/stats/max_size/"
    GENERATED_FILE_PATH = "/auto/sw_system_project/NVOS_INFRA/verification/stats/generated/"
    RESULTS_PATH = "/auto/sw_system_project/NVOS_INFRA/verification/stats/results/"
    INTERNAL_CAT_PATH = "/var/stats"
    TEMP_FOLDER = "/auto/sw_regression/system/NVOS/MARS/results/"

    # CSV header constants
    HEADER_HOSTNAME = "# Hostname:         "
    HEADER_GROUP = "# Statistic group:  "
    HEADER_TIME = "# Started sampling: "
    TIMESTAMP_FORMAT = "%b-%d %Y %H:%M:%S"
    SYSTEM_TIME_FORMAT = '%Y-%m-%d %H:%M:%S'
    MAX_ROWS_TO_SCAN = 300
    CONST_HEADER_ROWS = 9  # Number of header constant rows in the stats file's format: hostname, statistic group, time, columns ...
    BIG_FILE_NUM_OF_LINES = 600026
    # Just past the 600MB mark, where the single-file design used to truncate a
    # file into a <category>.csv.old and start over.
    HUGE_FILE_TARGET_BYTES = 620 * 1024 * 1024

    # Action constants
    GENERATE = 'generate'
    DELETE = 'delete'
    UPLOAD = 'upload'
    CLEAR = 'clear'

    # --- Data range constants per metric type ---

    # Temperature
    TEMP_MIN = 15  # [Celsius]
    TEMP_MAX = 90  # [Celsius]

    # Management interface
    MGMT_INT_MIN = 0  # [Bytes/sec]
    MGMT_INT_MAX = 10000  # [Bytes/sec]

    # Fan
    FAN_MIN = 0  # [%]
    FAN_MAX = 100  # [%]

    # Power supply
    PWR_PSU_VOLT_MIN = 0  # [V]
    PWR_PSU_VOLT_MAX = 300  # [V]
    PWR_PSU_CUR_MIN = 0  # [A]
    PWR_PSU_CUR_MAX = 100  # [A]

    # ASIC power
    ASIC_PWR_WATT_MIN = 0  # [Watt]
    ASIC_PWR_WATT_MAX = 600  # [Watt]

    # CPU
    CPU_FREE_RAM_MIN = 30  # [%]
    CPU_FREE_RAM_MAX = 100  # [%]
    CPU_UTIL_MIN = 0  # [%]
    CPU_UTIL_MAX = 60  # [%]
    CPU_REBOOT_CNT_MIN = 0
    CPU_REBOOT_CNT_MAX = 100

    # Disk
    DISK_FREE_SPACE_MIN = 30  # [%]
    DISK_FREE_SPACE_MAX = 99  # [%]
    DISK_RMN_LIFE_MIN = 70  # [%]
    DISK_RMN_LIFE_MAX = 100  # [%]
    DISK_FAIL_CNT_MIN = 0
    DISK_FAIL_CNT_MAX = 0
    DISK_TOTAL_LBA_RW_MIN = 10000
    DISK_TOTAL_LBA_RW_MAX = 4294967295

    # Voltage
    VOLTAGE_GENERAL_MIN = 0
    VOLTAGE_GENERAL_MAX = 100
    VOLTAGE_PSU_MIN = 0
    VOLTAGE_PSU_MAX = 300

    # Interface / peer-port counters (monotonic, unsigned 64-bit)
    COUNTER_MIN = 0
    COUNTER_MAX = 2 ** 64 - 1

    # Transceiver DOM-sensor ranges
    XCVR_TEMP_MIN = -50     # [Celsius]
    XCVR_TEMP_MAX = 120     # [Celsius]
    XCVR_VOLTAGE_MIN = 0    # [V]
    XCVR_VOLTAGE_MAX = 6    # [V]
    XCVR_POWER_MIN = 0      # [mW]
    XCVR_POWER_MAX = 20     # [mW]
    XCVR_BIAS_MIN = 0       # [mA]
    XCVR_BIAS_MAX = 500     # [mA] BlackMamba sw-port modules run up to ~295
    DOM_COMPARE_TOLERANCE = 5   # TODO confirm agreed DOM-sensor compare tolerance

    # Transceiver columns are per-lane and unit-suffixed ('rx-power3 [mW]',
    # 'tx-los7'), so they are matched by name pattern rather than index: one rule
    # covers every lane whatever the module's lane count, and the file stays valid
    # if columns are reordered. First match wins; anything unmatched (status,
    # error-status) falls through to the category's generic range.
    TRANSCEIVER_COLUMN_PATTERNS = (
        (r'-fault\d*$|-los\d*$|-cdr-lol\d*$', 'bool', None, None),
        (r'^temperature', 'numeric', XCVR_TEMP_MIN, XCVR_TEMP_MAX),
        (r'^voltage', 'numeric', XCVR_VOLTAGE_MIN, XCVR_VOLTAGE_MAX),
        (r'^(rx|tx)-power', 'numeric', XCVR_POWER_MIN, XCVR_POWER_MAX),
        (r'^tx-bias-current', 'numeric', XCVR_BIAS_MIN, XCVR_BIAS_MAX),
    )

    # --- Validation ---

    MIN_EXPECTED_SAMPLES = 3

    # Data-driven validation config per category.
    #
    # Rule types:
    #   'columns'            - list of (col_index, min, max, allow_na) for fixed-layout columns
    #   'uniform_range'      - single (min, max, allow_na) applied to all data columns (index 1+)
    #   'remaining_columns'  - range applied to a column slice (start/end, end=None means "to last")
    #   'tail_columns'       - range applied to the last N columns
    CATEGORY_VALIDATION_CONFIG = {
        'cpu': {
            'columns': [
                # (col_index, min_val, max_val, allow_na)
                (1, CPU_FREE_RAM_MIN, CPU_FREE_RAM_MAX, False),
                (2, CPU_UTIL_MIN, CPU_UTIL_MAX, False),
                (3, CPU_REBOOT_CNT_MIN, CPU_REBOOT_CNT_MAX, False),
            ],
            'remaining_columns': {
                'start': 4, 'end': None,
                'min': CPU_REBOOT_CNT_MIN, 'max': CPU_REBOOT_CNT_MAX, 'allow_na': False,
            },
        },
        'disk': {
            'columns': [
                (1, DISK_FREE_SPACE_MIN, DISK_FREE_SPACE_MAX, True),
                (2, DISK_RMN_LIFE_MIN, DISK_RMN_LIFE_MAX, True),
                (3, DISK_FAIL_CNT_MIN, DISK_FAIL_CNT_MAX, True),
                (4, DISK_FAIL_CNT_MIN, DISK_FAIL_CNT_MAX, True),
                (5, DISK_FAIL_CNT_MIN, DISK_FAIL_CNT_MAX, True),
                (6, DISK_TOTAL_LBA_RW_MIN, DISK_TOTAL_LBA_RW_MAX, True),
                (7, DISK_TOTAL_LBA_RW_MIN, DISK_TOTAL_LBA_RW_MAX, True),
            ],
        },
        'fan': {
            'uniform_range': {
                'start': 1, 'end': None,
                'min': FAN_MIN, 'max': FAN_MAX, 'allow_na': True,
            },
        },
        'temperature': {
            'uniform_range': {
                'start': 1, 'end': None,
                'min': TEMP_MIN, 'max': TEMP_MAX, 'allow_na': True,
            },
        },
        'mgmt-interface': {
            'uniform_range': {
                'start': 1, 'end': None,
                'min': MGMT_INT_MIN, 'max': MGMT_INT_MAX, 'allow_na': True,
            },
        },
        'power': {
            'columns': [
                (1, PWR_PSU_VOLT_MIN, PWR_PSU_VOLT_MAX, True),
                (2, PWR_PSU_VOLT_MIN, PWR_PSU_VOLT_MAX, False),
                (3, PWR_PSU_CUR_MIN, PWR_PSU_CUR_MAX, True),
                (4, PWR_PSU_CUR_MIN, PWR_PSU_CUR_MAX, False),
            ],
        },
        'asic-power': {
            'uniform_range': {
                'start': 1, 'end': None,
                'min': ASIC_PWR_WATT_MIN, 'max': ASIC_PWR_WATT_MAX, 'allow_na': True,
            },
        },
        'voltage': {
            'remaining_columns': {
                'start': 1, 'end': -2,
                'min': VOLTAGE_GENERAL_MIN, 'max': VOLTAGE_GENERAL_MAX, 'allow_na': True,
            },
            'tail_columns': {
                'count': 2,
                'min': VOLTAGE_PSU_MIN, 'max': VOLTAGE_PSU_MAX, 'allow_na': True,
            },
        },
        # Interface / peer-port: monotonic counters; absent counters recorded as N/A.
        # These categories are row-per-entity: column 0 is the Timestamp and column 1
        # is the entity name (e.g. "interface"), so the numeric range starts at 2.
        'interface': {
            'uniform_range': {
                'start': NEW_CATEGORY_FIRST_VALUE_COLUMN, 'end': None,
                'min': COUNTER_MIN, 'max': COUNTER_MAX, 'allow_na': True,
            },
            'non_numeric_columns': NEW_CATEGORY_STATE_COLUMNS,
            'row_per_entity': True,
        },
        'peer-port': {
            'uniform_range': {
                'start': NEW_CATEGORY_FIRST_VALUE_COLUMN, 'end': None,
                'min': COUNTER_MIN, 'max': COUNTER_MAX, 'allow_na': True,
            },
            'non_numeric_columns': NEW_CATEGORY_STATE_COLUMNS | PEER_PORT_IDENTITY_COLUMNS,
            'row_per_entity': True,
        },
        # Transceivers: DOM-sensor snapshots. Per-field ranges come from
        # TRANSCEIVER_COLUMN_PATTERNS; the generic range below only catches the
        # unmatched numeric columns (status, error-status).
        'transceivers': {
            'uniform_range': {
                'start': NEW_CATEGORY_FIRST_VALUE_COLUMN, 'end': None,
                'min': COUNTER_MIN, 'max': COUNTER_MAX, 'allow_na': True,
            },
            'column_patterns': TRANSCEIVER_COLUMN_PATTERNS,
            'non_numeric_columns': NEW_CATEGORY_STATE_COLUMNS,
            'row_per_entity': True,
        },
    }
