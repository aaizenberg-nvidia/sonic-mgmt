import os
import re


class HealthEventConst:
    DEFAULT_SEVERITY = 'fatal'
    SEVERITY_FATAL = 'fatal'
    SEVERITY_WARNING = 'warning'
    SEVERITY_LIST = ['fatal', 'error', 'warning']
    CATEGORY_FIRMWARE = 'firmware'
    CATEGORY_NONE = 'none'
    CATEGORY_POSITIVE = ['firmware', 'software', 'asic_hw', 'cpu_hw']
    CATEGORY_NEGATIVE = ['ABC', '0']
    MAX_EVENTS_NUM = 5
    MAX_EVENTS_NUM_DEFAULT = 0
    MAX_EVENTS_NUM_ELIMINATE_THRESHOLD = 10
    MAX_EVENTS_NUM_POSITIVE = ['0', '10000']
    MAX_EVENTS_NUM_NEGATIVE = ['1.0', '-1', 'unlimited']
    DEFAULT_FW_HEALTH_EVENT_ID = 1
    FW_EVENTS_DICT = {"FW_FATAL_EVENT": 1, "FW_WARN_EVENT": 2, "PLL_LOCK_EVENT": 3}
    BASE_DIR = os.path.dirname(os.path.realpath(__file__))
    FILES_DIR = os.path.join(BASE_DIR, 'files')
    GENERATE_EVENTS_SCRIPT = 'generate-events.py'
    GENERATE_EVENTS_SCRIPT_DEST_FOLDER = '/tmp'
    EVENT_PATTERN = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\w+)\s+(\w+)')
    HEALTH_EVENT_DUMP_FILE = 'asic.sdk.health.event'
    ELIMINATE_EVENTS_SCRIPT = '/usr/share/swss/eliminate_events.lua'
    BASE_COMMAND = 'sudo config asic-sdk-health-event suppress fatal'
    SCALE_EVENTS_NUM = 100
    PARAMETERS = {
        'category-list': {
            'positive': CATEGORY_POSITIVE,
            'negative': CATEGORY_NEGATIVE
        },
        'max-events': {
            'positive': MAX_EVENTS_NUM_POSITIVE,
            'negative': MAX_EVENTS_NUM_NEGATIVE
        }
    }
