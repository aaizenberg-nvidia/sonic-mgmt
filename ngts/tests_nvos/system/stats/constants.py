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
    }
