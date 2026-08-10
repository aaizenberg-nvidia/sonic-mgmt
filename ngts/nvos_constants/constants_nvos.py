import json
import os
import re
from enum import Enum
from pathlib import Path
from typing import List, TypedDict
import re

from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import ADMIN


class DatabaseConst:
    APPL_DB_NAME = "APPL_DB"
    ASIC_DB_NAME = "ASIC_DB"
    COUNTERS_DB_NAME = "COUNTERS_DB"
    CONFIG_DB_NAME = "CONFIG_DB"
    STATE_DB_NAME = "STATE_DB"
    EVENT_DB_NAME = "EVENT_DB"

    APPL_DB_ID = 0
    ASIC_DB_ID = 1
    COUNTERS_DB_ID = 2
    CONFIG_DB_ID = 4
    STATE_DB_ID = 6

    REDIS_DB_NUM_TO_NAME = {0: APPL_DB_NAME, 1: ASIC_DB_NAME, 2: COUNTERS_DB_NAME, 4: CONFIG_DB_NAME, 6: STATE_DB_NAME}
    '''
     for each database we need:
         database id : id in redis
         database dict : includes all the possible tables and expected quantity of each table
         for example in config database we need a "IB_PORT" table for each port so possible quantities are 40 and 60

     '''
    APPL_DB_TABLES_DICT = {
        "IB_PORT_TABLE:Infiniband": [40, 64],
        "ALIAS_PORT_MAP": [40, 64],
        "IB_PORT_TABLE:Port": [2]
    }
    ASIC_DB_TABLES_DICT = {
        "ASIC_STATE:SAI_OBJECT_TYPE_PORT": [41, 65],
        "ASIC_STATE:SAI_OBJECT_TYPE_SWITCH": [1],
        "LANES": [1],
        "VIDCOUNTER": [1],
        "RIDTOVID": [1],
        "HIDDEN": [1],
        "COLDVIDS": [1]
    }
    COUNTERS_DB_TABLES_DICT = {
        "COUNTERS_PORT_NAME_MAP": [1],
        "COUNTERS:oid": [40, 64]
    }
    CONFIG_DB_TABLES_DICT = {
        "IB_PORT": [40, 64],
        "BREAKOUT_CFG": [40, 64],
        "FEATURE": [12],
        "CONFIG_DB_INITIALIZED": [1],
        "DEVICE_METADATA": [1],
        "XCVRD_LOG": [1],
        "VERSIONS": [1],
        "KDUMP": [1]
    }


class CumulusConsts:
    LINUX_BOOT_PATTERN = 'Debian GNU/Linux 12 .*'
    LOGIN_BOOT_PATTERN = 'cumulus login:.*'
    ETH_SWITCH_TYPE = "ETH"
    ROLE_SYSTEM_ADMIN = "system-admin"
    ROLE_NVUE_ADMIN = "nvue-admin"
    ROLE_NVUE_MONITOR = "nvue-monitor"
    TECHSUPPORT_FILES_PATH = "/var/support/"
    TECHSUPPORT_ACTION_DELETE_SUCCESS_MESSAGE = "File Delete Succeeded"
    TECHSUPPORT_THRESHOLD = 150
    TECHSUPPORT_FILE_NOT_FOUND_MESSAGE = "File not found: nonexist"
    NTP_SYNCHRONIZATION_MAX_TIME = 300
    NTP_LOGS_COMPONENT_ID = "nvue"
    LOG_MSG_SET_NTP = ["nv set system ntp server 10.80.74.100 state"]

    DISK_MODE_FULL_DISK = 'full-disk'
    DISK_MODE_USAGE = 'usage'
    DEFAULT_USER_CUMULUS = "cumulus"

    DNS_PRIORITIES = [10, 20, 30, 40]
    DNS_SERVER_IDS_V4_LIST = ["10.245.1.124", "10.245.1.121", "10.7.77.135"]
    DNS_SEARCHES_LIST = ["nvidia.com", "mellanox.com", "lab2300.nbulabs.nvidia.com", "mvlab.labs.mlnx"]
    DNS_SERVER_IDS_V6_LIST = ["2001:4860:4860::8888", "2606:4700:4700::1111", "fd00:2002:192:168::1"]
    DNS_DOMAIN_LIST = ["nvidia.com", "confluence", "redmine"]
    DNS_VRF = "vrf"
    DNS_SEARCH = "search"
    DNS_VRF_MGMT = "mgmt"
    DNS_DOMAIN = "domain"
    DNS_PRIORITY = "priority"
    DNS_VRF_LIST = ['default', 'RED']
    INTERFACE_IP_VRF = 'vrf'
    FETCH_SUCCESS_MESSAGE = "File has been successfully fetched"
    FETCH_ERROR_MESSAGE = "Failed to fetch file due to the following error"
    PLATFORM_ASIC_TEMPERATURE_FIELDS = ['current', 'min', 'max', 'crit', 'state']
    PLATFORM_ASIC_OUTPUT_FIELDS = {'temperature': PLATFORM_ASIC_TEMPERATURE_FIELDS}
    # Allowed values for ASIC temperature 'state' field
    PLATFORM_ASIC_TEMPERATURE_STATES = ('ok', 'high', 'low', 'critical')
    # Valid ranges for ASIC temperature numeric fields (min/max inclusive)
    PLATFORM_ASIC_TEMP_CURRENT_MIN = 0
    PLATFORM_ASIC_TEMP_CURRENT_MAX = 150
    PLATFORM_ASIC_TEMP_MIN_MIN = 0
    PLATFORM_ASIC_TEMP_MIN_MAX = 50
    PLATFORM_ASIC_TEMP_MAX_MIN = 50
    PLATFORM_ASIC_TEMP_MAX_MAX = 150
    PLATFORM_ASIC_TEMP_CRIT_MIN = 100
    PLATFORM_ASIC_TEMP_CRIT_MAX = 200
    PLATFORM_ASIC_RESOURCE_SECTIONS = ['global', 'acl']
    PLATFORM_ASIC_RESOURCE_ACL_TABLE_FIELDS = ['acl-18b', 'acl-36b', 'acl-54b', 'rule-count']
    PLATFORM_ASIC_RESOURCE_GLOBAL_REQUIRED_FIELDS = ['used']
    PLATFORM_ASIC_RESOURCE_GLOBAL_OPTIONAL_FIELDS = ['max', 'percentage']
    MIN_PSU_VPD_PATTERN = re.compile(r"MIN_PSU\s*[=:]\s*\d+")
    VRF_MGMT = "mgmt"

    CL_NFS_SERVER = "http://tetrabuild-1.mvlab.labs.mlnx"


class TestConsts:
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class NvosConst:
    IB_SWITCH_TYPE = "IB"
    NVL_SWITCH_TYPE = "NVL"
    JULIET_SWITCH = "JULIET"
    CROCODILE_SWITCH = "CROCODILE"
    BLACK_MAMBA_SWITCH = "BLACK_MAMBA"
    BLACK_MAMBA_DGX_SWITCH = "BLACK_MAMBA_DGX"
    TAIPAN_SWITCH = "TAIPAN"
    TAIPAN_SINGLE_ASIC_SWITCH = "TAIPAN_SINGLE_ASIC"
    GORILLA_SWITCH = "GORILLA"
    QTM2 = "Quantum2"
    QTM3 = "Quantum3"
    QTM4 = "Quantum4"
    NVL5 = 'NVLink-5 switch'
    NVL6 = 'NVLink-6 Switch'
    NVL7 = 'NVLink-7 Switch'
    NVL_ACCESS_PORTS_LIST = 'nvl_access_ports_list'
    NVL_TRUNK_PORTS_LIST = 'nvl_trunk_ports_list'
    ALL_NVL_PORTS_LIST = 'all_nvl_ports_list'
    QTM3_AND_NEWER = [QTM3, QTM4]
    DESCRIPTION = 'description'
    PORT_STATUS_UP = 'up'
    PORT_STATUS_DOWN = 'down'
    DOCKER_AUTO_RESTART = 'auto_restart'
    DOCKER_STATUS_ENABLED = 'enabled'
    DOCKER_STATUS_DISABLED = 'disabled'
    DOCKER_STATUS_UP = 'Up'
    SERVICE_STATUS_ACTIVE = 'active'
    NVUE_CLI = "NVUE"
    CUMULUS_SWITCH = "CUMULUS_SWITCH"
    ENABLED = 'enabled'
    DISABLED = 'disabled'
    NOT_AVAILABLE = 'N/A'
    ALL = 'all'
    DEFAULT_CONFIG_FILE_NAME = "nvos_default_config_IB.yml"
    DEFAULT_RESOURCES_PATH = Path('ngts/tools/test_utils/nvos_resources')
    DEFAULT_NVOS_CONFIG_PATH = DEFAULT_RESOURCES_PATH / "nvos_configuration_files"
    DEFAULT_CONFIG_PATH = "/tools/test_utils/nvos_resources/clear_config/"
    SCP_PATH_SWITCH = 'scp {path} {username}@{ip}:{dest_path}'
    PATH_TO_CONFIG_FILES_ON_DUT = "/host/config_files"
    PATH_TO_TMP_ON_DUT = "/tmp/"
    PATH_TO_FW_IMAGES = "/host/fw-images"
    PATH_TO_DPC_ON_DUT = "/etc/fae_platform_dpc"

    DOCKERS_LIST = ['pmon', 'syncd-ibv0', 'swss-ibv0', 'database']
    DOCKER_PER_ASIC_LIST = ['syncd-ibv0', 'swss-ibv0', 'database']
    NV_GNMI_DOCKER = 'nv-gnmi'
    NV_UMF_DOCKER = 'nv-umf'
    SYM_MGR_SERVICES = ['configmgrd.service', 'countermgrd.service', 'portsyncmgrd.service']
    PORT_STATUS_LABEL = 'admin_status'
    PORT_CONFIG_DB_TABLES_PREFIX = "IB_PORT"
    IMAGES_PATH_ON_SWITCH = "/tmp/temp_nvos.bin"
    FM_PATH_ON_SWITCH = "/tmp/temp_fw.bin"

    ROOT_USER = os.getenv("VM_USER")
    ROOT_PASSWORD = os.getenv("VM_PASSWORD")

    SONIC_MGMT = 'sonic_mgmt'
    FIT70 = 'fit70'

    OLD_PASS = os.getenv("NVU_SWITCH_PASSWORD")

    REBOOT_CMD_TO_RUN = "ipmitool -I lanplus -H {ip} -U {username} -P {password} chassis power cycle"

    SYS_RST_N_BIT_COMMAND = "sudo iorw -b 0x2519 -w -l1 -v 0xfe"

    # [0] syslog e.g. "Jan  1 12:34:56"; [1] space-separated e.g. "2026-02-18 12:34:56"; [2] ISO with T (optional .microseconds, optional Z or +HH:MM)
    DATE_TIME_REGEX = [
        r"\w{3}\s{1,2}\d{1,2} \d\d:\d\d:\d\d(?:.\d+)?",
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+\d{2}:\d{2})?",
    ]

    TIMESTAMP_REGEX = (
        r'(?P<year>\d{4})'
        r'(?P<month>1[0-2]|0?[1-9])'
        r'(?P<day>3[01]|[12][0-9]|0?[1-9])'
        r'(?P<hour>2[0-3]|1[0-9]|0?[0-9])'
        r'(?P<minute>[0-5][0-9]|[0-9])'
    )

    FW_DUMP_ME_SCRIPT_PATH = "/auto/sw_system_project/NVOS_INFRA/security/verification/fw_dump_me/sxd_api_crash_fw.py"
    DESTINATION_FW_SCRIPT_PATH = "/var/tmp/"
    SDK_DUMP_FOLDER = "/var/log/mellanox/sdk-dumps/"
    MARS_RESULTS_FOLDER = "/auto/sw_regression/system/NVOS/MARS/results/"
    MARS_DUMPS_FOLDER = "/auto/sw_regression/system/NVOS/MARS/dumps/"

    PATH_TO_IMAGES = "/host/nos-images"

    HOST_HA = 'ha'
    HOST_HA_ATTR = 'ha_attr'
    HOST_HB = 'hb'
    HOST_HB_ATTR = 'hb_attr'

    SERVERS_USER_NAME = os.getenv("TEST_SERVER_USER")

    SONIC_SERVICE_ACCOUNT = os.getenv("SONIC_SERVICE_ACCOUNT")
    SONIC_SERVICE_ACCOUNT_API_KEY = os.getenv("SONIC_SERVICE_ACCOUNT_API_KEY")

    SYSTEM = "system"
    INTERFACE = "interface"
    IB = "ib"
    SYSTEM_TIMEZONE = "timezone"
    SYSTEM_AAA = 'aaa'
    SYSTEM_AUTHENTICATION = 'authentication'
    SYSTEM_AUTHENTICATION_ORDER = 'order'
    SYSTEM_AAA_USER = 'user'
    SYSTEM_AAA_USER_ADMIN = 'admin'
    SYSTEM_AAA_USER_MONITOR = 'monitor'
    SYSTEM_AAA_USER_CUMULUS = 'cumulus'
    SYSTEM_AAA_CLASS = 'class'
    SYSTEM_AAA_ROLE = 'role'

    DEFAULT_CONFIG = {"system": {
        "aaa": {
            "authentication": {
                "restrictions": {
                    "fail-delay": 0,
                    "lockout-state": "disabled"
                }
            },
            "user": {
                "admin": {
                    "password": "*"
                }
            }
        },
        "security": {
            "password-hardening": {
                "state": "enabled"
            }
        },
        "timezone": "Asia/Jerusalem"
    }
    }
    DEFAULT_NVOS_IFACE_CONFIG = {
        "eth0": {
            "acl": {
                "ACL_MGMT_INBOUND_CP_DEFAULT": {
                    "inbound": {
                        "control-plane": {}
                    }
                },
                "ACL_MGMT_INBOUND_CP_DEFAULT_IPV6": {
                    "inbound": {
                        "control-plane": {}
                    }
                },
                "ACL_MGMT_INBOUND_DEFAULT": {
                    "inbound": {}
                },
                "ACL_MGMT_INBOUND_DEFAULT_IPV6": {
                    "inbound": {}
                },
                "ACL_MGMT_OUTBOUND_CP_DEFAULT": {
                    "outbound": {
                        "control-plane": {}
                    }
                },
                "ACL_MGMT_OUTBOUND_CP_DEFAULT_IPV6": {
                    "outbound": {
                        "control-plane": {}
                    }
                }
            },
            "type": "eth"
        },
        "lo": {
            "acl": {
                "ACL_LOOPBACK_INBOUND_CP_DEFAULT": {
                    "inbound": {
                        "control-plane": {}
                    }
                },
                "ACL_LOOPBACK_INBOUND_CP_DEFAULT_IPV6": {
                    "inbound": {
                        "control-plane": {}
                    }
                }
            },
            "type": "loopback"
        }
    }

    DEFAULT_CL_CONFIG = {
        "system": {
            "wjh": {
                "channel": {
                    "forwarding": {
                        "trigger": {
                            "l2": {},
                            "l3": {},
                            "tunnel": {}
                        }
                    },
                },
                "enable": "on"
            },
            "timezone": "Etc/UTC",
            "hostname": "cumulus",
            "reboot": {
                "mode": "cold"
            },
            "api": {
                "state": "enabled"
            },
            "control-plane": {
                "acl": {
                    "acl-default-dos": {
                        "inbound": {}
                    },
                    "acl-default-whitelist": {
                        "inbound": {}
                    }
                }
            },
            "config": {
                "auto-save": {
                    "state": "enabled"
                }
            },
            "ssh-server": {
                "state": "enabled"
            }
        }
    }

    DEFAULT_CL_IFACE_CONFIG = {
        "eth0": {
            "ip": {
                "address": {
                    "dhcp": {}
                },
                "vrf": "mgmt"
            },
            "type": "eth"
        }
    }

    ONIE_NOS_INSTALL_CMD = 'onie-nos-install'
    INSTALL_SUCCESS_PATTERN = 'Installed.*base image.*successfully'
    INSTALL_WGET_ERROR = "wget:.*"
    INSTALL_BOOT_PATTERN = "boot:"
    INSTALL_CUMULUS_SUCCESS_PATTERN = '.* login:'
    NVOS_INSTALL_TIMEOUT = 6 * 60  # 6 minutes
    GRUB_TIMEOUT = 60

    COVERAGE_PATH = "/var/lib/python/coverage"
    MAX_COVERAGE_PATH_CAPACITY_PERCENTAGE = 70

    NO_CONFIG_DIFF_APPLY_MSG = "config apply executed with no config diff"
    DECLINED_APPLY_MSG = 'Declined apply after warnings'
    Y_COMMAND_NOT_FOUND = 'y: command not found'
    FETCH_SUCCESS_MESSAGE = "File fetched successfully"
    FETCH_ERROR_MESSAGE = "Failed to create file"

    STATE = "state"
    EXPECTED_SELECTOR_DICTIONARY = {
        "filter": {
            "1": {
                "action": "exclude",
                "match": "a+"
            }
        }
    }


class TopologyConsts:
    MTL = "MTL"
    MTVR = "MTVR"
    CL = "CL"
    NVOS = "NVOS"
    SONIC = "SONIC"
    site_server_ip = {MTL: "10.237.22.60",
                      MTVR: "10.237.22.60",
                      CL: "10.188.20.9"}


class CertificateFiles:
    BUNDLE_FILE = 'bundle_uri'
    PUBLIC_KEY_FILE = 'uri-public-key'
    PRIVATE_KEY_FILE = 'uri-private-key'
    BUNDLE_CERTIFICATE_CURRENT_PASSWORD = 'Test_2108'
    URI_BUNDLE = 'uri-bundle'
    URI = 'uri'
    PUBLIC_PRIVATE = 'public_private'
    DATA = 'data'
    PASSPHRASE = "passphrase"
    CERTIFICATE_PATH = '/auto/sw_system_project/NVOS_INFRA/security/verification/cert_mgmt/'
    CERTIFICATE = "certificate"
    DEFAULT_CERTIFICATE = "self-signed"
    CA_CERTIFICATE = "ca-certificate"
    PATH_TO_CERTIFICATES = "/etc/ssl/certs/"


class UserRole:
    ADMIN = "admin"
    MONITOR = "monitor"
    # list of all roles
    ALL_ROLES = [ADMIN, MONITOR]


class ApiType:
    NVUE = "NVUE"
    OPENAPI = "OpenApi"
    # list of all api types
    ALL_TYPES = [NVUE, OPENAPI]


class TestFlowType:
    GOOD_FLOW = 'GoodFlow'
    BAD_FLOW = 'BadFlow'
    ALL_TYPES = [GOOD_FLOW, BAD_FLOW]


class RebootTestFlowType:
    WITH_SAVE = 'save'
    NO_SAVE = 'no-save'
    ALL_TYPES = [WITH_SAVE, NO_SAVE]


class CacertType:
    GLOBAL = 'global'
    EXTERNAL = 'external'
    ALL_TYPES = [GLOBAL, EXTERNAL]


class OutputFormat:
    auto = 'auto'
    json = 'json'
    yaml = 'yaml'


class ConfState:
    OPERATIONAL = 'operational'
    APPLIED = 'applied'
    STARTUP = 'startup'
    SAVED = 'saved'


class OpenApiReqType:
    GET = 'GET'
    PATCH = 'PATCH'
    DELETE = 'DELETE'
    APPLY = 'APPLY'
    ACTION = 'ACTION'


class OpenApiConfigVerifyConsts:
    """OpenAPI NVUE config verify (dry-run) only — revision state, modes, messages, pre-verify PATCH errors."""
    # Revision JSON `state` when dry-run validation finished successfully
    REVISION_STATE_DRY_RUN_COMPLETE = "dry_run_complete"
    # state-controls value for PATCH {"state": "apply", "state-controls": {"dry-run": ...}}
    DRY_RUN_MODE_BRIEF = "brief"
    DRY_RUN_MODE_VERBOSE = "verbose"
    # ResultObj.info / failure text from verify polling
    RESULT_INFO_VERIFY_DRY_RUN_OK = "Verify (dry-run) succeeded"
    RESULT_INFO_VERIFY_CONFIGS_FAILED_PREFIX = "Verify failed for these configs: "
    RESULT_INFO_VERIFY_FAILED_PREFIX = "Verify failed: "
    POLL_WAIT_MESSAGE = "Waiting for verify (dry-run) to complete"
    # text/plain PATCH failed (invalid nv lines, 400, etc.) — skip dry-run
    PATCH_BODY_ERROR_SUBSTRINGS = ("Error", "Request failed", "Bad Request")


class ActionType:
    BOOT_NEXT = '@boot-next'
    CLEANUP = '@cleanup'
    CLEAR = '@clear'
    DISCONNECT = '@disconnect'
    GENERATE = '@generate'
    INSTALL = '@install'
    REBOOT = '@reboot'
    RENEW = '@renew'
    RUN = '@run'
    ERASE = '@erase'
    ROTATE = '@rotate'
    TURNOFF = '@turn-off'
    TURNON = '@turn-on'
    UNINSTALL = '@uninstall'
    FETCH = '@fetch'
    DELETE = '@delete'
    RENAME = '@rename'
    UPLOAD = '@upload'
    RESET = '@reset'
    START = '@start'
    STOP = '@stop'
    UPDATE = '@update'
    RESTORE = '@restore'
    CHANGE = '@change'
    ENABLE = '@enable'
    DISABLE = '@disable'
    IMPORT = '@import'
    CREATE = '@create'
    POWER_CYCLE = '@power-cycle'


class ActionConsts:
    START = 'start'
    CLEANUP = "cleanup"
    RUN = "run"
    STOP = "stop"
    CHANGE = 'change'
    INSTALL = "install"
    UNINSTALL = "uninstall"
    BOOT_NEXT = "boot-next"
    GENERATE = "generate"
    FETCH = "fetch"
    ENABLE = 'enable'
    DISABLE = 'disable'
    DELETE = 'delete'
    CLEAR = 'clear'
    UPLOAD = 'upload'
    RENAME = 'rename'
    RESET = 'reset'
    RESUME = 'resume'
    UPDATE = 'update'
    POWER_CYCLE = 'power-cycle'
    REBOOT = 'reboot'
    RENEW = 'renew'
    RESTORE = 'restore'
    ROTATE = 'rotate'
    ACTIVATE = 'activate'
    ERASE = 'erase'


class ActionParamConsts:
    YES = 'yes'
    NO = 'no'
    ON = 'on'
    OFF = 'off'
    ENABLED = 'enabled'
    DISABLED = 'disabled'
    FORCE = 'force'
    IMMEDIATE = 'immediate'
    REBOOT = 'reboot'
    SKIP_REBOOT = 'skip-reboot'
    NEW_NAME = 'new-name'
    REMOTE_URL = 'remote-url'


class SystemConsts:
    MGMT2_HOSTNAME = "mgmt2"
    HOSTNAME = 'hostname'
    STATUS = 'status'
    STATE = 'state'
    MAC = 'mac'
    STATUS_DEFAULT_VALUE = 'System is ready'
    STATUS_NOT_OK = 'System is not ready'
    STATUS_UP = 'UP'
    STATUS_DOWN = 'DOWN'
    STATUS_FAILS = ''
    FAE_SYSTEM_STATE = 'state'
    FAE_SYSTEM_STATE_DEFAULT_VALUE = 'enabled'
    REBOOT = 'reboot'
    BUILD = 'build'
    PLATFORM = 'platform'
    PRODUCT_NAME = 'product-name'
    PRODUCT_RELEASE = 'product-release'
    MEMORY = 'memory'
    SWAP_MEMORY = 'swap-memory'
    SYSTEM_MEMORY = 'system-memory'
    UPTIME = 'uptime'
    TIMEZONE = 'timezone'
    DATE_TIME = 'date-time'
    VERSION = 'version'
    SECURITY = 'security'
    GPU_MAD_STATE_ENABLED = 'enabled'
    GPU_MAD_STATE_DISABLED = 'disabled'
    CONTACT = 'contact'
    LOCATION = 'location'
    TECHSUPPORT_FILES_PATH = '/host/dump/'
    LIST_OF_COMMANDS_FILE_PATH = '/var/tmp/list_of_executed_commands.txt'
    FILE_TRANSFER_PROTOCOLS = ['scp', 'sftp']
    TECHSUPPORT_SIZE_LIMIT = 75  # in MBs
    TECHSUPPORT_FILE_NOT_FOUND_MESSAGE = "is not a"

    PATH_KEY = 'path'
    LATEST_KEY = 'latest'

    MIN_SYSTEM_YEAR = '1970'
    MAX_SYSTEM_YEAR = '2035'
    MIN_SYSTEM_DATE = MIN_SYSTEM_YEAR + "-01-01"
    MAX_SYSTEM_DATE = MAX_SYSTEM_YEAR + "-12-31"
    MIN_SYSTEM_DATETIME = MIN_SYSTEM_DATE + " 00:00:00"
    MAX_SYSTEM_DATETIME = MAX_SYSTEM_DATE + " 23:59:59"

    NBU_NFS_SERVER = "https://nbu-mtr-nfs.nvidia.com"

    PRE_LOGIN_MESSAGE = 'pre-login'
    POST_LOGIN_MESSAGE = 'post-login'
    POST_LOGOUT_MESSAGE = 'post-logout'
    PRE_LOGIN_MESSAGE_DEFAULT_VALUE = "Welcome to NVIDIA Cumulus (R) Linux (R)"
    POST_LOGIN_MESSAGE_DEFAULT_VALUE = "Welcome to NVIDIA Cumulus (R) Linux (R)\nFor support and online technical documentation, visit https://www.nvidia.com/en-us/support\nThe registered trademark Linux (R) is used pursuant to a sublicense from LMI, the exclusive licensee of Linus Torvalds, owner of the mark on a world-wide basis.\n"
    POST_LOGOUT_MESSAGE_DEFAULT_VALUE = ""

    REBOOT_HISTORY = 'history'
    REBOOT_REASON = 'reason'
    REBOOT_REASON_REBOOT = 'reboot'
    REBOOT_REASON_POWER_LOSS = 'Power Loss'
    REBOOT_REASON_POWER_BUTTON = 'power button'

    VERSION_BUILD_DATE = 'build-date'
    VERSION_BUILD_ID = 'build-id'
    # VERSION_BUILT_BY = 'built-by'
    VERSION_IMAGE = 'image'
    VERSION_KERNEL = 'kernel'
    VERSION_ONIE = 'onie'
    VERSION_BASE_OS = 'base-os'
    VERSION_PRODUCT_RELEASE = 'product-release'
    SW_FIELD_NAMES = ('description', 'package', 'version')

    PROFILE_ADAPTIVE_ROUTING = 'adaptive-routing'
    PROFILE_ADAPTIVE_ROUTING_GROUPS = 'adaptive-routing-groups'
    PROFILE_ADAPTIVE_ROUTING_GROUPS_INDEX = 1
    PROFILE_BREAKOUT_MODE = 'breakout-mode'
    PROFILE_IB_ROUTING = 'ib-routing'
    PROFILE_NUMBER_OF_SWIDS = 'num-of-swids'
    PROFILE_OUTPUT_FIELDS = [PROFILE_ADAPTIVE_ROUTING, PROFILE_ADAPTIVE_ROUTING_GROUPS, PROFILE_BREAKOUT_MODE,
                             PROFILE_IB_ROUTING, PROFILE_NUMBER_OF_SWIDS]
    ADAPTIVE_ROUTING_DEFAULT_ADAPTIVE_GROUPS = 2048
    BREAKOUT_MODE_DEFAULT_ADAPTIVE_GROUPS = 1792
    DEFAULT_NUM_SWIDS = 1
    PROFILE_STATE_ENABLED = 'enabled'
    PROFILE_STATE_DISABLED = 'disabled'
    DEFAULT_SYSTEM_PROFILE_VALUES = [PROFILE_STATE_ENABLED, ADAPTIVE_ROUTING_DEFAULT_ADAPTIVE_GROUPS,
                                     PROFILE_STATE_DISABLED, PROFILE_STATE_DISABLED, DEFAULT_NUM_SWIDS]

    SNMP_REFRESH_INTERVAL = 'auto-refresh-interval'
    SNMP_LISTENING_ADDRESS = 'listening-address'
    SNMP_READONLY_COMMUNITY = 'readonly-community'
    SNMP_STATE = 'state'
    SNMP_SYSTEM_CONTACT = 'system-contact'
    SNMP_SYSTEM_LOCATION = 'system-location'
    SNMP_OUTPUT_FIELDS = [SNMP_LISTENING_ADDRESS, SNMP_READONLY_COMMUNITY, SNMP_STATE]
    SNMP_DEFAULT_STATE = 'disabled'
    SNMP_ENABLED_STATE = 'enabled'
    SNMP_DEFAULT_REFRESH_INTERVAL = 60
    SNMP_DEFAULT_LISTENING_ADDRESS = {}
    SNMP_DEFAULT_READONLY_COMMUNITY = {}
    SNMP_ENABLED_DEFAULT_LISTENING_ADDRESS = {'all': {'port': 161}}
    SNMP_DEFAULT_VALUES = [SNMP_DEFAULT_LISTENING_ADDRESS,
                           SNMP_DEFAULT_READONLY_COMMUNITY, SNMP_DEFAULT_STATE]

    SSH_CONFIG_PKA_ONLY = 'pka-only'
    SSH_CONFIG_AUTH_RETRIES = 'authentication-retries'
    SSH_CONFIG_INACTIVE_TIMEOUT = 'inactive-timeout'
    SSH_CONFIG_LOGIN_TIMEOUT = 'login-timeout'
    SSH_CONFIG_MAX_SESSIONS = 'max-sessions'
    SSH_CONFIG_PORT = 'port'
    SSH_CONFIG_OUTPUT_FIELDS = [SSH_CONFIG_AUTH_RETRIES, SSH_CONFIG_INACTIVE_TIMEOUT, SSH_CONFIG_LOGIN_TIMEOUT,
                                SSH_CONFIG_MAX_SESSIONS, SSH_CONFIG_PORT]
    SSH_CONFIG_DEFAULT_AUTH_RETRY = '6'
    SSH_CONFIG_DEFAULT_INACTIVE_TIMEOUT = '20'
    SSH_CONFIG_DEFAULT_LOGIN_TIMEOUT = '120'
    SSH_CONFIG_DEFAULT_MAX_SESSION = '100'
    SSH_CONFIG_DEFAULT_PORTS = {"22": {}}
    SSH_CONFIG_MIN_MAX_SESSION = 3
    SSH_CONFIG_MAX_MAX_SESSION = 100
    SSH_CONFIG_MIN_INACTIVE_TIMEOUT = 0
    SSH_CONFIG_MAX_INACTIVE_TIMEOUT = 35000
    SSH_CONFIG_DEFAULT_VALUES = [SSH_CONFIG_DEFAULT_AUTH_RETRY, SSH_CONFIG_DEFAULT_INACTIVE_TIMEOUT,
                                 SSH_CONFIG_DEFAULT_LOGIN_TIMEOUT, SSH_CONFIG_DEFAULT_MAX_SESSION,
                                 SSH_CONFIG_DEFAULT_PORTS]

    CLI_INACTIVE_TIMEOUT = 'inactive-timeout'
    SERIAL_CONSOLE_SYSRQ_CAPABILITIES = 'sysrq-capabilities'
    CLI_DEFAULT_INACTIVE_TIMEOUT = '20'
    SERIAL_CONSOLE_DEFAULT_SYSRQ_CAPABILITIES = 'disabled'
    SERIAL_CONSOLE_ENABLED_SYSRQ_CAPABILITIES = 'enabled'

    SERIAL_CONSOLE_CONNECTED_TO = 'connected-to'
    SERIAL_CONSOLE_OUTPUT_CPU = 'cpu'
    SERIAL_CONSOLE_OUTPUT_BMC = 'bmc'
    SERIAL_BMC_CONSOLE_OUTPUT_DEFAULT_FIELD = [SERIAL_CONSOLE_CONNECTED_TO]
    SERIAL_BMC_CONSOLE_OUTPUT_DEFAULT_VALUE = [SERIAL_CONSOLE_OUTPUT_CPU]
    SERIAL_BMC_ACTION_CHANGE_BMC = SERIAL_CONSOLE_CONNECTED_TO + " " + SERIAL_CONSOLE_OUTPUT_BMC
    SERIAL_BMC_ACTION_CHANGE_CPU = SERIAL_CONSOLE_CONNECTED_TO + " " + SERIAL_CONSOLE_OUTPUT_CPU
    SERIAL_CONSOLE_BAUD_RATE = "115200"

    HOSTNAME_DEFAULT_VALUE = 'nvos'
    POST_LOGOUT_MESSAGE_DEFAULT_VALUE = ""
    ACTIONS_GENERATE_SINCE = 'since'

    DEFAULT_USER_ADMIN = 'admin'
    DEFAULT_USER_CL = 'cumulus'
    DEFAULT_USER_MONITOR = 'monitor'
    USER_ROLE = 'role'
    USER_STATE = 'state'
    USER_FULL_NAME = 'full-name'
    USER_ADMIN_DEFAULT_FULL_NAME = 'System Administrator'
    USER_MONITOR_DEFAULT_FULL_NAME = 'System Monitor'
    USER_STATE_ENABLED = 'enabled'
    USER_STATE_DISABLED = 'disabled'
    USER_PASSWORD = 'password'
    USER_HASHED_PASSWORD = 'hashed-password'
    USER_SPIFFE_ID = 'spiffe-id'
    USER_PASSWORDS_DEFAULT_VALUE = '*'
    ROLE_LABEL = USER_ROLE
    ROLE_CONFIGURATOR = 'admin'
    ROLE_VIEWER = 'monitor'
    ROLE_GROUPS = 'groups'
    ROLE_PERMISSIONS = 'permissions'
    ROLE_CONFIGURATOR_DEFAULT_GROUPS = 'adm,docker,nvaction,nvapply,redis,sudo'
    ROLE_VIEWER_DEFAULT_GROUPS = 'adm,nvshow'
    USERNAME_MAX_LEN = 32
    USERNAME_PASSWORD_HARDENING_HISTORY_COUNT = 'history-cnt'
    USERNAME_PASSWORD_HARDENING_STATE = 'state'
    USERNAME_VALID_CHARACTERS = list(map(chr, range(65, 91))) + list(map(chr, range(97, 123)))
    USERNAME_INVALID_CHARACTERS = list(map(chr, range(48, 57)))
    USERNAME_PASSWORD_DIGITS_LABEL = 'digits-class'
    USERNAME_PASSWORD_DIGITS_LIST = list(map(chr, range(48, 57)))
    USERNAME_PASSWORD_LENGTH_LABEL = 'len-min'
    USERNAME_PASSWORD_LENGTH_DEFAULT = 8
    USERNAME_PASSWORD_LOWER_LABEL = 'lower-class'
    USERNAME_PASSWORD_LOWER_LIST = list(map(chr, range(97, 123)))
    USERNAME_PASSWORD_UPPER_LABEL = 'upper-class'
    USERNAME_PASSWORD_UPPER_LIST = list(map(chr, range(65, 91)))
    USERNAME_PASSWORD_SPECIAL_LABEL = 'special-class'
    USERNAME_PASSWORD_SPECIAL_LIST = "_#)(^"  # noqa: E402 "#$%&'()*+,-./:;<=>@[\]^_`{|}~"
    PASSWORD_HARDENING_DEFAULT = [USERNAME_PASSWORD_DIGITS_LABEL, USERNAME_PASSWORD_LOWER_LABEL,
                                  USERNAME_PASSWORD_UPPER_LABEL, USERNAME_PASSWORD_SPECIAL_LABEL]
    PASSWORD_HARDENING_RUNNING_PROCESSES = 'Running processes'
    PASSWORD_HARDENING_LABEL = 'password-hardening'

    PASSWORD_HARDENING_DICT = {
        USERNAME_PASSWORD_DIGITS_LABEL: USERNAME_PASSWORD_DIGITS_LIST,
        USERNAME_PASSWORD_LOWER_LABEL: USERNAME_PASSWORD_LOWER_LIST,
        USERNAME_PASSWORD_UPPER_LABEL: USERNAME_PASSWORD_UPPER_LIST,
        USERNAME_PASSWORD_SPECIAL_LABEL: USERNAME_PASSWORD_SPECIAL_LIST
    }

    SHOW_VALUE_YES = 'yes'
    SHOW_VALUE_NO = 'no'
    DHCP_SHOW_FIELDS = ['has-lease', 'is-running', 'set-hostname', 'state']
    DHCP_SHOW_DEFAULT_VALUES = [SHOW_VALUE_YES, SHOW_VALUE_YES, USER_STATE_ENABLED, USER_STATE_ENABLED]
    DH_CLIENT_CONF_FILE = "/etc/dhcp/dhclient.conf"

    MEMORY_PHYSICAL_KEY = 'physical'
    MEMORY_SWAP_KEY = 'swap'
    MEMORY_PERCENT_THRESH_MIN = 0.0
    MEMORY_PERCENT_THRESH_MAX = 70.0

    CPU_CORE_COUNT_KEY = 'core-count'
    CPU_MODEL_KEY = 'model'
    CPU_CORES = 'cores'
    CPU_LOAD_AVERAGE_KEY = 'load-average'
    CPU_UTILIZATION_KEY = 'utilization'
    CPU_TOTAL_UTILIZATION_KEY = 'total-utilization'
    CPU_PERCENT_THRESH_MIN = 0.0
    CPU_PERCENT_THRESH_MAX = 60.0

    LEGACY_HEALTH_STATUS = "health-status"  # as in 25.02.2500 branch
    HEALTH = "health"  # current

    EXTERNAL_API_STATE = 'state'
    EXTERNAL_API_STATE_ENABLED = 'enabled'
    EXTERNAL_API_STATE_DISABLED = 'disabled'
    EXTERNAL_API_LISTEN = 'listening-address'
    EXTERNAL_API_PORT = 'port'
    EXTERNAL_API_RULE = 'rule'
    EXTERNAL_API_STATE_DEFAULT = 'enabled'
    EXTERNAL_API_PORT_DEFAULT = '443'
    EXTERNAL_API_PORT_NON_DEFAULT = '442'
    EXTERNAL_API_LISTEN_DEFAULT = 'any'
    EXTERNAL_API_LISTEN_LOCALHOST = 'localhost'
    EXTERNAL_API_CONN_ACCEPTED = 'accepted'
    EXTERNAL_API_CONN_ACTIVE = 'active'
    EXTERNAL_API_CONN_HANDLED = 'handled'
    EXTERNAL_API_CONN_READING = 'reading'
    EXTERNAL_API_CONN_REQUEST = 'requests'
    EXTERNAL_API_CONN_WAITING = 'waiting'
    EXTERNAL_API_CONN_WRITING = 'writing'

    ZTP_SERVICE = 'service'
    ZTP_STATUS = 'status'
    ZTP_STATE = 'state'
    ZTP_CONFIG_SAVE = 'config-save'
    ZTP_OUTPUT_FIELDS = [ZTP_SERVICE, ZTP_STATUS, ZTP_CONFIG_SAVE]
    ZTP_DEFAULT_SERVICE = 'enabled'
    ZTP_DEFAULT_STATUS = 'discovering'
    ZTP_DEFAULT_CONFIG_SAVE = 'disabled'
    ZTP_DEFAULT_VALUES = [ZTP_DEFAULT_SERVICE, ZTP_DEFAULT_STATUS, ZTP_DEFAULT_CONFIG_SAVE]
    ZTP_CONFIG_SAVE_SERVICE = 'disabled'
    ZTP_CONFIG_SAVE_STATUS = 'aborted'
    ZTP_CONFIG_SAVE = 'disabled'
    ZTP_STATUS_ENABLED = 'enabled'
    ZTP_STATUS_FAILED = 'failed'
    ZTP_SERVICE_DISABLED = 'disabled'
    ZTP_SERVICE_ENABLED = 'enabled'
    ZTP_AFTER_CONFIG_SAVE_VALUES = [ZTP_CONFIG_SAVE_SERVICE, ZTP_CONFIG_SAVE_STATUS,
                                    ZTP_CONFIG_SAVE]
    ZTP_CONFIG_SAVE_VALUES = [ZTP_CONFIG_SAVE_SERVICE, ZTP_CONFIG_SAVE_STATUS,
                              ZTP_STATUS_ENABLED]
    ZTP_DEFAULT_LOG_FILE = '/var/log/ztp.log'
    DUMMY_JSON = 'dummy.json'
    POSITIVE_JSON = 'positive.json'
    NEGATIVE_PING_JSON = 'negative_ping.json'
    NEGATIVE_HALT_ON_FAILURE_JSON = 'negative_halt_on_failure.json'
    NEGATIVE_RESTART_ON_FAILURE_JSON = 'negative_restart_on_failure.json'
    IMAGE_JSON = 'uninstall.json'
    IMAGE_PROD_JSON = 'uninstall_prod.json'
    STARTUP_FILE_WRONG_IP = 'startup_wrong_ip.json'
    STARTUP_FILE_CLEAR_CONFIG_FALSE = 'startup_file_clear_config_false.json'
    STARTUP_FILE_CLEAR_CONFIG_TRUE = 'startup_file_clear_config.json'
    STARTUP_FILE_SAVE_CONFIG_TRUE = 'startup_file_config_save.json'
    STARTUP_FILE_INTERACTIVE_COMMANDS = 'startup_file_interactive.json'
    NMX_POSITIVE_JSON = 'ztp_nmx.json'
    NMX_NOT_EXIST_FILE_JSON = 'ztp_nmx_negative_wrong_file.json'
    NMX_BAD_COMMANDS = 'ztp_nmx_negative_bad_commands.json'
    CONNECTIVITY_IPV4_IPV6 = 'ping_ipv4_ipv6.json'
    NEGATIVE_CONNECTIVITY = 'negative_connectivity.json'
    COMPLEX = 'complex.json'
    COMPLEX_PROD = 'complex_prod.json'
    SCRIPT_POSITIVE = 'ztp_provisioning_positive.json'
    SCRIPT_NEGATIVE = 'ztp_provisioning_negative.json'
    SCRIPT_INTERACTIVE = 'ztp_provisioning_interactive_commands.json'
    SCRIPT_LOOP_TIMEOUT = 'ztp_provisioning_loop.json'
    SCRIPT_POSITIVE_PYTHON = 'ztp_provisioning_python.json'
    SCRIPT_BAD_FILE = 'ztp_provisioning_bad_file.json'
    SCRIPT_HASHED_PASSWORD = 'ztp_hashed_password.json'

    # ZTP provisioning-script "parameters"
    PROVISIONING_SCRIPT_STEP = '01-provisioning-script'
    SCRIPT_PARAMS_POSITIVE = 'ztp_provisioning_params_positive.json'
    SCRIPT_PARAMS_MAX = 'ztp_provisioning_params_max.json'
    SCRIPT_PARAMS_PYTHON_MIN = 'ztp_provisioning_params_python_min.json'
    SCRIPT_PARAMS_NO_KEY = 'ztp_provisioning_params_no_key.json'
    SCRIPT_PARAMS_EMPTY = 'ztp_provisioning_params_empty.json'
    SCRIPT_PARAMS_NOT_OBJECT = 'ztp_provisioning_params_not_object.json'
    SCRIPT_PARAMS_EMPTY_NAME = 'ztp_provisioning_params_empty_name.json'
    SCRIPT_PARAMS_INVALID_FLAG = 'ztp_provisioning_params_invalid_flag.json'
    SCRIPT_PARAMS_NON_SCALAR = 'ztp_provisioning_params_non_scalar.json'
    SCRIPT_PARAMS_TOO_MANY = 'ztp_provisioning_params_too_many.json'
    SCRIPT_PARAMS_WHITESPACE_NAME = 'ztp_provisioning_params_whitespace_name.json'  # "--  "
    SCRIPT_PARAMS_NUMERIC_NAME = 'ztp_provisioning_params_numeric_name.json'        # "--1"
    SCRIPT_PARAMS_SHORT_MULTICHAR = 'ztp_provisioning_params_short_multichar.json'  # "-ss"
    SCRIPT_PARAMS_NULL_VALUE = 'ztp_provisioning_params_null_value.json'            # {"--nullval": null}
    SCRIPT_PARAMS_INVALID_BOOL = 'ztp_provisioning_params_invalid_bool.json'        # {"--name": TRUE}
    ZTP_PARAMS_RECORDER_OUTPUT = '/tmp/ztp_params_recorder.out'
    ZTP_PARAMS_EXPECTED_MAX = 20
    PARAMS_LOG_PASSING_POSITIVE = ('passing 8 parameter(s): --nvos_image, --retries, --role, --s, --site, '
                                   '--verbose, -n, -s')
    PARAMS_LOG_PASSING_PYTHON_MIN = 'passing 2 parameter(s): --role, -n'
    PARAMS_LOG_PASSING_MAX = ('passing 20 parameter(s): --p01, --p02, --p03, --p04, --p05, --p06, --p07, --p08, '
                              '--p09, --p10, --p11, --p12, --p13, --p14, --p15, --p16, --p17, --p18, --p19, --p20')
    PARAMS_ERR_NOT_OBJECT = 'must be a JSON object of name/value pairs'
    PARAMS_ERR_EMPTY_NAME = 'Parameter name must be a non-empty string'
    PARAMS_ERR_INVALID_FLAG = 'is not a valid option flag'
    PARAMS_ERR_NON_SCALAR = 'is not a scalar value'
    PARAMS_ERR_TOO_MANY = 'Too many parameters (21); at most 20 are allowed'
    PARAMS_ERR_JSON_LOAD = 'ZTP JSON load failed'

    ZTP_STATUS_RUNNING = 'running'
    ZTP_STATUS_IN_PROGRESS = 'in-progress'
    ZTP_STATUS_SUCCESS = 'success'
    ZTP_STATUS_FAILED = 'failed'
    HTTP_SERVER = 'http://nbu-nfs.gtm.nvidia.com'
    VERIFICATION_ZTP_PATH = '/auto/sw_system_project/NVOS_INFRA/ztp/'
    SCRIPT_ASIC_DEBUG_CONFIG = 'positive_asic_debug_config.json'
    PASS_ASIC_DEBUG_CONFIG = 'pass-asic-debug.yaml'
    PASS_ASIC_DEBUG_CONFIG_ZTP = 'passing-asic-debug-config.yaml'
    NOT_YAML_ASIC_DEBUG_CONFIG = 'incorrect.txt'
    ASIC_DEBUG_CONFIG_20_PRMS = '20.yaml'
    ASIC_DEBUG_CONFIG_32_PRMS = '32_prms.yaml'
    ASIC_DEBUG_CONFIG_2K_BUFFER = '2k_buff.yaml'
    FAIL_ASIC_DEBUG_CONFIG_WRONG_REGISTER = 'failing-asic-debug-config-wrong-register.yaml'
    FAIL_ASIC_DEBUG_CONFIG_WRONG_VERSION = 'failing-asic-debug-config-wrong-version.yaml'
    YAML_WITHOUT_FIELDS = 'trash.yaml'
    STATUS_ASIC_DEBUG_CONFIG = 'status'
    SUCCESS_STATUS_ASIC_DEBUG_CONFIG = 'success'
    FAILED_STATUS_ASIC_DEBUG_CONFIG = 'failed'
    NA = 'N/A'
    STAGE_ASIC_DEBUG_CONFIG = 'stage'
    NEXT_ASIC_DEBUG_CONFIG = 'next'
    CURRENT_ASIC_DEBUG_CONFIG = 'current'
    ASIC_DEBUG_CONFIG_ERROR = 'asic-debug-config.service: Failed'
    VERIFICATION_ASIC_DEBUG_PATH = '/auto/sw_system_project/dev_test/asic_debug_config'
    ASIC_DEBUG_CONFIG_LOG_FILE = '/var/log/asic_debug_config.log'
    PGCB_PASS_CONFIG = 'pgcb_simple.yaml'
    SIMULATE_PGRSS_TRAP = 'simulate_pgrrs_trap.sh'

    SUCCESS_STATUS_DEBUG_CONFIG = 'success'
    FAILED_STATUS_DEBUG_CONFIG = 'failed'
    NA = 'N/A'
    STAGE_DEBUG_CONFIG = 'stage'
    NEXT_DEBUG_CONFIG = 'next'
    CURRENT_DEBUG_CONFIG = 'current'
    STATUS_DEBUG_CONFIG = 'status'
    NOT_SHELL_DEBUG_CONFIG = 'a.txt'
    PASS_CPU_DEBUG_CONFIG = 'pass.sh'
    FAIL_DEBUG_CONFIG = 'fail.sh'
    NOT_EXIST_DEBUG_CONFIG = 'aaa.sh'
    ERROR_NOT_SHELL_SCRIPT = 'is not a shell script'
    ERROR_FILE_NOT_EXIST = 'does not exist'
    CPU_DEBUG_LOG_GREP = '| grep cpu-debug-config'
    BMC_DEBUG_LOG_GREP = '| grep bmc-debug-config'
    PASS_FOLDER_CPU_DEBUG_CONFIG = 'pass_folder.sh'
    CPU_CONFIG_CREATED_FOLDER = '/tmp/cpu-config'
    VERIFICATION_CPU_DEBUG_PATH = '/auto/sw_system_project/dev_test/cpu_debug_config'
    VERIFICATION_BMC_DEBUG_PATH = '/auto/sw_system_project/dev_test/bmc_debug_config'
    DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES = {
        CURRENT_DEBUG_CONFIG: NA,
        NEXT_DEBUG_CONFIG: NA
    }

    PYTHON_PATH = 'PYTHONPATH=/ngts_venv/ /ngts_venv/bin/python'
    CONTAINER_BU_SCRIPT = '/devts/scripts/docker/containers_bringup.py'
    CONTAINER_BU_TEMPLATE = '{python_path} {container_bu_script} --setup_name {setup_name} --metrox2xc_setup'

    AUTO_SAVE_STATE = 'state'
    AUTO_SAVE_STATE_ENABLED = 'enabled'
    AUTO_SAVE_STATE_DISABLED = 'disabled'

    LLDP_INTERVAL = 'tx-interval'
    LLDP_MULTIPLIER = 'tx-hold-multiplier'
    LLDP_STATE = 'state'
    LLDP_IS_RUNNING = 'is-running'
    LLDP_NEIGHBOR = 'neighbor'
    LLDP_LLDP = 'lldp'

    GENERAL_TRANSCEIVER_FIRMWARE_FILES = "/auto/sw_system_project/NVOS_INFRA/verification_files/transceiver_fw"

    SSD_SPACE_TOTAL_SIZE = 'Size'
    SSD_SPACE_USED_SIZE = 'Used'
    SSD_SPACE_AVAILABLE_SIZE = 'Avail'
    SSD_SPACE_USAGE_PERCENTAGE = 'Use%'

    REBOOT_RESPONSE_MESSAGES = (
        "Performing reboot",
        "Performing warm reboot",
        "Disconnecting from NVOS, system is offline during reboot",
        "System will be rebooted",
        "System will power cycle in a few seconds",
        "Restarting system",
        "The system will reboot now!",
        "Rebooting System in cold mode",
        "System will power cycle in a few seconds",
        "Reboot service initiated successfully"
    )

    ACTION_INSTALL_SUCCESS_MESSAGES = (
        "is successfully installed",
    )

    LOCALHOST = "127.0.0.1"
    DUMMY_IMAGE = "dummy.bin"
    DUMMY_IMAGE_PATH = "/tmp/"

    SYSTEM_LAST_EVENT = 'last'
    SYSTEM_RECENT_EVENT = 'recent'

    DNS_SERVER = 'server'
    DNS_SERVER_IPV4 = "8.8.8.8"
    DNS_SERVER_IPV6 = "2001:4860:4860::8888"
    DNS_SERVER_LIST = [DNS_SERVER_IPV4, DNS_SERVER_IPV6]
    # list of all api types
    DNS_SERVER_IDS = {"ipv4": DNS_SERVER_IPV4, "ipv6": DNS_SERVER_IPV6}
    MAX_DNS_SERVERS = 3

    DUMMY_IMAGE_PATH = '/tmp/'
    DUMMY_IMAGE = 'dummy.bin'

    SYSTEMCTL_STATUS_CMD = "sudo systemctl is-system-running"

    NV_BRIDGE = 'nv-bridge'
    NV_BRIDGE_STATE = 'state'
    NV_BRIDGE_HEARTBEAT = 'heartbeat'
    NV_BRIDGE_HEALTH = 'health'
    NV_BRIDGE_HEALTH_REASON = 'health-reason'
    NV_BRIDGE_CONNECTION = 'connections'
    NV_BRIDGE_ENABLED = 'enabled'
    NV_BRIDGE_DISABLED = 'disabled'
    NV_BRIDGE_CLIENT_ADDRESS = 'client-address'
    NV_BRIDGE_SERVER_ADDRESS = 'server-address'
    NV_BRIDGE_CLIENT_ID = 'client-id'
    NV_BRIDGE_CLIENT_ACTIVE = 'active'
    NV_BRIDGE_HEALTH_OK = 'ok'
    NV_BRIDGE_HEALTH_NOT_OK = 'not ok'
    NV_BRIDGE_GREP = '| grep nv-bridge'
    NV_BRIDGE_NODE_IP = '127.0.0.1'
    NV_BRIDGE_NODE_NEGATIVE_IP = '1.1.1.1'
    NODE = 'node'
    NV_BRIDGE_NODE_SERVER = 'server'
    ACL_DEFAULT_WHITELIST = 'acl-default-whitelist'
    ACL_DEFAULT_WHITELIST_IPV6 = 'acl-default-whitelist-ipv6'
    NV_BRIDGE_IPV4_ACL_RULE = 260
    NV_BRIDGE_IPV6_ACL_RULE = 250
    NV_BRIDGE_PORT = 50052
    NV_BRIDGE_CLIENT_STATE = 'nv-bridge-state-report'

    CLUSTER_STATE_DISABLED = 'disabled'
    CLUSTER_STATE_ENABLED = 'enabled'
    CLUSTER_APP_STATE_UP = 'up'

    DISK: str = 'disk'
    GLOBAL = 'global'
    DNS = 'dns'
    FQDN = 'fqdn'

    class ApiConsts:
        class CompressionT:
            GZIP = 'gzip'

            @classmethod
            def all(cls):
                return [v for k, v in vars(cls).items() if k.isupper()]

        COMPRESSION = 'compression'


class EventConsts:
    TABLE_SIZE = 'table-size'
    TABLE_OCCUPANCY = 'table-occupancy'
    TABLE_SIZE_DEFAULT = 1000
    TABLE_SIZE_MAX = 10000

    # These are the json field names. In NVUE they are: Severity, Component, Description and Timestamp
    SEVERITY = 'severity'
    RESOURCE = 'resource'
    TEXT = 'text'
    TIME_CREATED = 'time-created'

    # wording pinned from live 25.03 devices (rosalind-mec-2164 / juliet-126)
    INTERFACE_UP_EVENT = 'Interface operational state is up'
    # 'time-created' format: '%Y-%m-%d %H:%M:%S' plus a trailing timezone name
    TIME_CREATED_FORMAT = '%Y-%m-%d %H:%M:%S'

    # severity levels
    INFORMATIONAL = 'INFORMATIONAL'
    MAJOR = 'MAJOR'


class DocumentsConsts:
    MIN_FILES_SIZE = 30000
    TYPE_EULA = 'EULA'
    TYPE_USER_MANUAL = 'User manual'
    TYPE_OPEN_SOURCE_LICENSES = 'Open source licenses'
    TYPE_RELEASE_NOTES = 'Release notes'


class IpConsts:
    MIN_IPV6_GROUP_VALUE = 0
    MAX_IPV6_GROUP_VALUE = 65535
    ARP_TIMEOUT = "arp-timeout"
    AUTOCONF = "autoconf"
    PYTHON_PATH = '/auto/app/Python-2.7.9/bin/python2.7'
    IP_LINK_SET_INTERFACE = 'sudo ip link set {interface} {state}'
    MAD_TEMPLATE = 'sudo {python_path} {nvmad_path}/nvmad.py --lid {lid} --mad MAD.GMP.VS.SwitchNetworkInfo --Ca {card} --modifier {modifier}'
    IPV4_PREFIX = 'MAD.GMP.VS.SwitchNetworkInfo.IPv4[0].ipv4'
    IPV4_NETMASK_PREFIX = 'MAD.GMP.VS.SwitchNetworkInfo.IPv4[0].netmask'
    IPV6_PREFIX = 'MAD.GMP.VS.SwitchNetworkInfo.IPv6[0].ipv6'
    IPV6_NETMASK_PREFIX = 'MAD.GMP.VS.SwitchNetworkInfo.IPv6[0].netmask'
    NUMBER_OF_ADDRESSES_IN_MAD_RESPONSE = 4
    IPV4 = 'ipv4'
    IPV4_NETMASK = 'ipv4_netmask'
    IPV6 = 'ipv6'
    IPV6_NETMASK = 'ipv6_netmask'
    ADDR = 'address'
    FUNC = 'function'
    HEX_TO_IPV4 = 'hex_to_ipv4'
    HEX_TO_IPV6 = 'hex_to_ipv6'
    HEX_PREFIX = '0x'
    PORT_STATE_UP = 'Up'
    PORT_STATE_DOWN = 'Down'
    IPV6_HEX_ZERO = '00000000000000000000000000000000'
    IPV6_ZERO = '0:0:0:0::0:0'
    MAD_DICT = {
        IPV4_PREFIX: {
            ADDR: IPV4,
            FUNC: HEX_TO_IPV4},
        IPV4_NETMASK_PREFIX: {
            ADDR: IPV4_NETMASK,
            FUNC: HEX_TO_IPV4},
        IPV6_PREFIX: {
            ADDR: IPV6,
            FUNC: HEX_TO_IPV6},
        IPV6_NETMASK_PREFIX: {
            ADDR: IPV6_NETMASK,
            FUNC: HEX_TO_IPV6}
    }


class ConfigConsts:
    HISTORY_APPLY_ID = 'apply-id'
    REVISION_ID = 'rev-id'
    REF = 'ref'
    HISTORY_USER = 'user'
    APPLY_YES = '-y'
    APPLY_ASSUME_YES = '--assume-yes'
    APPLY_ASSUME_NO = '--assume-no'
    APPLY_CONFIRM_NO = '--confirm-yes'
    APPLY_CONFIRM_YES = '--confirm-no'
    APPLY_CONFIRM_STATUS = '--confirm-status'
    CONFIG_LABELS = ['date', 'message', 'reason', 'type', 'user']

    EXPECTED_HISTORY_FIELDS = {
        'message': 'Config update by admin',
        'reason': 'Config update',
        'type': 'CLI',
        'user': 'admin'
    }


class PlatformConsts:
    PLATFORM_FW = "firmware"
    SYSTEM_TYPE = "system-type"
    ASIC_REVISION = "asic-revision"
    FW_PATH = "/auto/sw_system_project/MLNX_OS_INFRA/mlnx_os2/sx_mlnx_fw/"
    XDR_FW_PATH = "/auto/mswg/release/sx_mlnx_fw/{asic}/"
    PLATFORM_ENVIRONMENT = "environment"
    PLATFORM_HW = "hardware"
    PLATFORM_SW = "software"
    FW_ASIC = "ASIC"
    FW_ASIC_REVISION_VALUE = "0x00A0"
    FW_BIOS = "BIOS"
    FW_CPLD = "CPLD"
    FW_SSD = "SSD"
    FW_FPGA = "FPGA"
    FW_SMA = "SMA"
    FW_BMC = "BMC"
    FW_EROT = "EROT"
    FW_COMPONENTS = [FW_BMC, FW_EROT, FW_CPLD, FW_BIOS, FW_SMA]
    FW_FIELD_NAME_DICT = {"Actual FW": "actual-firmware"}
    FW_ACTUAL = "actual-firmware"
    FW_BACKGROUND_COPY_STATUS = 'background-copy-status'
    FW_DEBUG_TOKEN_STATUS = 'debug-token-status'
    FW_AP_BOOT_STATUS = 'ap-boot-status'
    FW_UPGRADE_STATUS = 'fw-upgrade-status'
    FW_UPGRADE_ERROR_MSG = 'fw-upgrade-error-msg'
    FW_PART_NUMBER = 'part-number'
    FW_AUTO_UPDATE = "auto-update"
    FW_SOURCE = "fw-source"
    FW_SOURCE_DEFAULT = "default"
    FW_SOURCE_CUSTOM = "custom"
    FW_SPECTRUM1 = "Spectrum"
    FW_SPECTRUM2 = "Spectrum-2"
    FW_SPECTRUM3 = "Spectrum-3"
    FW_SPECTRUM4 = "Spectrum-4"
    FW_SPECTRUM5 = "Spectrum-5"
    FW_SPECTRUM6 = "Spectrum-6"
    FW_SLOT_STATUS_ACTIVE = 'active'
    FW_SLOT_STATUS_INACTIVE = 'inactive'
    # Basic fields present in all firmware components
    FW_BASIC_FIELDS = [FW_ACTUAL, FW_PART_NUMBER, FW_SOURCE]
    # ERoT-specific additional fields (only for ERoT components)
    FW_EROT_ADDITIONAL_FIELDS = [FW_BACKGROUND_COPY_STATUS, FW_DEBUG_TOKEN_STATUS, FW_SLOT_STATUS_ACTIVE, FW_SLOT_STATUS_INACTIVE]
    # Legacy - all fields (for backward compatibility or ERoT components)
    FW_FIELDS = [FW_ACTUAL, FW_PART_NUMBER, FW_SOURCE, FW_BACKGROUND_COPY_STATUS, FW_DEBUG_TOKEN_STATUS, FW_SLOT_STATUS_ACTIVE, FW_SLOT_STATUS_INACTIVE]
    HARDWARE_TRANCEIVER_DIAGNOSTIC_STATUS = "diagnostics-status"
    HARDWARE_TRANCEIVER_NOT_EXIST = "Non present module"
    HARDWARE_TRANCEIVER_NOT_DDMI = "No Diagnostic Data Available. Module is not DDMI capable"
    HARDWARE_TRANCEIVER_DIAGNOSTIC_DATA_AVAILABLE = "Diagnostic Data Available"
    ENV_CPU = "CPU"
    ENV_FAN = "fan"
    ENV_LED = "led"
    ENV_UID = "UID"
    ENV_PSU_STATUS_LED = "PSU_STATUS"
    ENV_PSU = "psu"
    ENV_TEMP = 'temperature'
    ENV_COMP = [ENV_FAN, ENV_LED, ENV_PSU, ENV_TEMP]
    ENV_FAN_COMP = ["max-speed", "min-speed", "current-speed", "state"]
    ENV_LED_COLOR_LABEL = "color"
    ENV_LED_COLOR_GREEN = "green"
    ENV_LED_COLOR_RED = "red"
    ENV_LED_COLOR_BLUE = "blue"
    ENV_LED_COLOR_AMBER = "amber"
    ENV_LED_COLOR_AMBER_BLINK = "amber_blink"
    ENV_LED_TURN_OFF = "off"
    ENV_LED_TURN_ON = "on"
    ENV_LED_COLOR_OPTIONS = [ENV_LED_COLOR_GREEN, ENV_LED_COLOR_RED, ENV_LED_TURN_OFF,
                             ENV_LED_COLOR_BLUE, ENV_LED_COLOR_AMBER, ENV_LED_COLOR_AMBER_BLINK]
    ENV_PSU_PROP = ["capacity", "current", "power", "state", "voltage"]
    ENV_TEMP_CURR_PROP = "current"
    ENV_TEMP_STATE_PROP = "state"
    ENV_TEMP_STATE_OK = 'ok'
    ENV_TEMP_STATE_FAILED = 'failed'
    HW_COMP_SWITCH = "SWITCH"
    TRANSCEIVER_STATUS = "status"
    TRANSCEIVER_FAULT_CONDITION = "fault-condition"
    TRANSCEIVER_ERROR_STATUS = "error-status"
    TRANSCEIVER_ELS_OPER_STATE = "els-oper-state"
    TRANSCEIVER_ELS_OPER_STATE_LASER_ACTIVE = "Laser Active"
    TRANSCEIVER_ELS_OPER_STATE_LASER_DOWN = "Laser Down"
    TRANSCEIVER_PORT_MAPPING = "port-mapping"
    TRANSCEIVER_ELS_MAPPING = "els-mapping"
    TRANSCEIVER_OE_MAPPING = "oe-mapping"
    ENV_TEMP_TOLERANCE = 20  # [%]
    ENV_TEMP_MIN = 15  # [Celsius]
    ENV_TEMP_MAX = 90  # [Celsius]
    VOLTAGE_FILES_PATH = '/var/run/hw-management/ui/voltage'
    TEMPERATURE_FILES_PATH = '/var/run/hw-management/ui/temperature'
    HEALTH_STATUS_NOT_OK_EVENT = 'Health status is not ok'
    LEAKAGE_STATUS_OK = 'ok'
    LEAKAGE_STATUS_LEAK = 'leak'
    LEAK_STATUS_LEAK = '0'
    LEAK_STATUS_OK = '1'
    LEAKAGE_FILES_FOLDER = '/var/run/hw-management/system/'
    LEAKAGE_FILES_SYSFS_FOLDER = '/sys/devices/platform/mlxplat/mlxreg-io/hwmon/'
    LEAKAGE_DEFAULT_OUTPUT_VALUES = [{'state': 'ok'}]
    LEAKAGE_ALL_SENSOR_NOT_OK = [{'state': 'leak'}]
    LONG_TERM_AVERAGE = "long-term-average"
    SHORT_TERM_AVERAGE = "short-term-average"
    POWER_TELEMETRY_ASIC_OUTPUT_FIELDS = [LONG_TERM_AVERAGE, SHORT_TERM_AVERAGE]
    POWER_TELEMETRY_COUNTERS_FIELDS = ["long-power-avg-histogram-0", "long-power-avg-histogram-1",
                                       "long-power-avg-histogram-2", "long-power-avg-histogram-3",
                                       "long-power-avg-histogram-4", "long-power-avg-histogram-5",
                                       "long-power-avg-histogram-6", "long-power-avg-histogram-7",
                                       "long-power-avg-histogram-8", "long-power-avg-histogram-9",
                                       "long-power-avg-histogram-10", "long-power-avg-histogram-11",
                                       "long-power-avg-histogram-12", "long-power-avg-histogram-13",
                                       "long-power-avg-histogram-14", "long-power-avg-histogram-15",
                                       "no-shaper-bin", "power-shaper-histogram-0", "power-shaper-histogram-1",
                                       "power-shaper-histogram-2", "power-shaper-histogram-3",
                                       "power-shaper-histogram-4", "power-shaper-histogram-5",
                                       "power-shaper-histogram-6", "power-shaper-histogram-7",
                                       "power-shaper-histogram-8", "power-shaper-histogram-9",
                                       "power-shaper-histogram-10", "power-shaper-histogram-11",
                                       "power-shaper-histogram-12", "power-shaper-histogram-13",
                                       "power-shaper-histogram-14", "power-shaper-histogram-15",
                                       "short-power-avg-histogram-0", "short-power-avg-histogram-1",
                                       "short-power-avg-histogram-2", "short-power-avg-histogram-3",
                                       "short-power-avg-histogram-4", "short-power-avg-histogram-5",
                                       "short-power-avg-histogram-6", "short-power-avg-histogram-7",
                                       "short-power-avg-histogram-8", "short-power-avg-histogram-9",
                                       "short-power-avg-histogram-10", "short-power-avg-histogram-11",
                                       "short-power-avg-histogram-12", "short-power-avg-histogram-13",
                                       "short-power-avg-histogram-14", "short-power-avg-histogram-15"]

    POWER_TELEMETRY_COUNTERS_CHANGABLE_FIELDS = ['short-power-avg-histogram-0', 'long-power-avg-histogram-0']
    BMC_FIRMWARE_INVENTORY_LINK = '/UpdateService/FirmwareInventory'
    BMC_FIRMWARE_BMC_LINK = 'MGX_FW_BMC_0'
    BMC_FIRMWARE_EROT_LINK = 'MGX_FW_ERoT_BMC_0'
    EROT_BMC_PATH_NAME = 'EROT-BMC'
    EROT_CPU_PATH_NAME = 'EROT-CPU'
    EROT_FPGA_PATH_NAME = 'EROT-FPGA'
    EROT_ASIC1_PATH_NAME = 'EROT-ASIC1'
    EROT_ASIC2_PATH_NAME = 'EROT-ASIC2'
    BMC_INVENTORY_PATTERN = r'/redfish/v1/UpdateService/FirmwareInventory/([^"]+)'
    BMC_COMPONENT_VERSION_PATTERN = r'"Version":\s*"([^"]+)"'
    BMC_LOGIN = ADMIN
    BMC_INTERNAL_IP = '10.0.1.1'
    BMC_DEFAULT_ROOT_PASSWORD_AFTER_RESET_VIA_NOS = '0penBmc'

    PSU_STATE = 'state'
    PS_REDUNDANCY_POLICY = 'policy'
    PS_REDUNDANCY_MIN_REQ = 'min-required'
    PS_REDUNDANCY_NO = 'no-redundancy'
    PS_REDUNDANCY_PS = 'ps-redundant'
    PS_REDUNDANCY_GRID = 'grid-redundant'
    PS_REDUNDANCY_POLICY_TYPE = [PS_REDUNDANCY_NO, PS_REDUNDANCY_PS, PS_REDUNDANCY_GRID]
    PS_REDUNDANCY_POLICY_TYPE_DEF = PS_REDUNDANCY_GRID
    PS_REBOOT_PSU_SKIP_STR = "SSKIP="
    VOLTAGE_FILES_PATTERN = 'PMIC|PSU|PDB|HSC|HSCC|FAN'
    REMOVED = 'Removed'
    INSERTED = 'Inserted'
    TRANSCEIVER_CABLE_TYPE = 'cable-type'
    TRANSCEIVER_CABLE_OPTICAL_MODULE = 'Optical module'
    TRANSCEIVER_CABLE_COPPER_CABLE = 'Copper cable'
    EROTS_LIST = ['ERoT_BMC_0', 'ERoT_CPU_0', 'ERoT_FPGA_0', 'ERoT_NVSwitch_0', 'ERoT_NVSwitch_1']
    INV_STATE = 'state'
    INV_OK = 'ok'
    HARDWARE_VERSION = "hardware-version"
    ASIC_CONF_FILE_PATH = "/usr/share/sonic/device/x86_64-nvidia_{}-r0/asic.conf"
    INV_FAILED = 'failed'
    TIMEOUT_AFTER_BIOS_INSTALL = 720
    TIMEOUT_AFTER_FW_INSTALL = 1200
    DISK_SIZE = 'disk-size'
    MEMORY = 'memory'
    DPC = "dpc"
    ROSALIND_ASIC_REVISION_4 = "0x00A4"
    ROSALIND_ASIC_REVISION_BY_PART_NUMBER = {
        "699-23809-0600-EB1": FW_ASIC_REVISION_VALUE,
        "920-9K42W-00L6-GS0": FW_ASIC_REVISION_VALUE,
        "920-9K42W-00L6-TS0": FW_ASIC_REVISION_VALUE,
        "920-9K42W-00L6-TS1": FW_ASIC_REVISION_VALUE,
        "920-9K42W-00L6-TS2": FW_ASIC_REVISION_VALUE,
        "920-9K42W-1313-TS3": FW_ASIC_REVISION_VALUE,
        "920-9K42W-1313-TS4": FW_ASIC_REVISION_VALUE,
    }


class ChassisLocationConsts:
    TRAY_ID = 'tray-index'
    SLOT_NUM = 'slot-number'
    CHAS_SN = 'chassis-sn'
    TOPO_ID = 'topology-id'
    LOOP_CABLE = 'Loopback'
    ETF = "3-slot ETF Cartridge"
    OBERON_36 = "GB200 NVL36"
    OBERON_72 = "GB200 NVL72"
    NA = 'N/A'
    ALLOWED_TOPOLOGIES = [
        LOOP_CABLE,
        ETF,
        OBERON_36,
        OBERON_72,
    ]
    EXPECTED_STANDALONE_DICT = {TRAY_ID: '0',
                                SLOT_NUM: '0',
                                CHAS_SN: NA,
                                TOPO_ID: LOOP_CABLE}


class CableCartridgeConsts:
    # Keys in the cable cartridge output
    NAME = "Name"
    KEY_SLOT_ID = "slot-id"
    KEY_TRAY_ID = "tray-id"
    KEY_SERIAL = "serial-number"
    KEY_PART_NUMBER = "part-number"
    KEY_MANUFACTURING_DATE = "manufacture-date"
    LEFTMOST_CARTRIDGE = "cartridge1"
    ALLOWED_PART_NUMBERS = [
        "755-24972-0003-000",
        "HS-32836-001",
        "HS-32836-003",
        "HS-33868-002",
        "HS-33869-002",
        "HS-33886-002",
        "755-XXXXX-XXXX-XXX"
    ]
    # Error messages
    ERR_MISSING_KEY = "Missing key in cable cartridge data: {}"
    ERR_NULL_VALUE = "Key '{}' has a null or empty value"
    ERR_MISMATCH = "'{}' '{}' does not match the expected value '{}'"
    ERR_GENERAL_VALIDATION_FAILED = "Validation failed for key '{}'"
    ERR_TRAY_ID_OUT_OF_RANGE = "Tray ID '{}' is out of the valid range ({}-{})"
    # All expected keys for validation
    ALL_KEYS = {KEY_SLOT_ID, KEY_TRAY_ID, KEY_SERIAL, KEY_PART_NUMBER, KEY_MANUFACTURING_DATE}


class PowerCappingConsts:
    DEFAULT_PROFILE_ID = 'compute'
    POWER = 'power'
    ACTIVE = 'active'
    STATE = 'state'

    # Enum-based profile names for CLI
    class ProfileName(Enum):
        CUSTOM_PROFILE_1 = 'custom-profile-1'
        CUSTOM_PROFILE_2 = 'custom-profile-2'
        CUSTOM_PROFILE_3 = 'custom-profile-3'
        CUSTOM_PROFILE_4 = 'custom-profile-4'
        CUSTOM_PROFILE_5 = 'custom-profile-5'

    PROFILES = ["networking", "reduced-bandwidth", DEFAULT_PROFILE_ID]
    ENUM_PROFILES = [profile.value for profile in ProfileName]

    TEST_PROFILES = ["test_profile1", "test_profile2", "test_profile3", "test_profile4", "test_profile5", "test_profile6"]  # Define 1 extra profile
    ATTRIBUTES = [
        'power-allocation-1', 'power-allocation-2', 'max-integral-1', 'max-integral-2',
        'avg-p-wr-num-of-sampling-1', 'avg-p-wr-num-of-sampling-2', 'kp-factor-1',
        'kp-factor-2', 'ki-factor-1', 'ki-factor-2', 'kd-factor-1', 'kd-factor-2',
        'pid-up-date-num-of-sampling-1', 'pid-up-date-num-of-sampling-2'
    ]
    FACTOR_ATTRIBUTES = ['kp-factor-1', 'kp-factor-2', 'ki-factor-1', 'ki-factor-2',
                         'kd-factor-1', 'kd-factor-2']
    KP_FACTOR_ATTRIBUTES = ['kp-factor-1', 'kp-factor-2']
    KI_FACTOR_ATTRIBUTES = ['ki-factor-1', 'ki-factor-2']
    KD_FACTOR_ATTRIBUTES = ['kd-factor-1', 'kd-factor-2']
    AVG_PWR_SAMPLING_ATTRIBUTES = ['avg-p-wr-num-of-sampling-1', 'avg-p-wr-num-of-sampling-2']
    PID_UPDATE_SAMPLING_ATTRIBUTES = ['pid-up-date-num-of-sampling-1', 'pid-up-date-num-of-sampling-2']

    # New CLI limitations for power profile attributes
    POWER_ALLOCATION_ATTRIBUTES = ['power-allocation-1', 'power-allocation-2']
    MAX_INTEGRAL_ATTRIBUTES = ['max-integral-1', 'max-integral-2']

    # Attribute value ranges (based on observed SAI firmware constraints)
    # Ranges derived from actual hardware default profiles:
    # Profile 1: power=575/575/475, kp=25, ki=7, kd=0, max_integral=2250, avg_pwr_sampling=2, pid_update_sampling=2
    # Profile 2: power=450/575/380, kp=50, ki=25, kd=0, max_integral=585, avg_pwr_sampling=20, pid_update_sampling=4

    # Individual range constants
    POWER_ALLOCATION_MIN = 300
    POWER_ALLOCATION_MAX = 700
    MAX_INTEGRAL_MIN = 0
    MAX_INTEGRAL_MAX = 3000
    KP_FACTOR_MIN = 10
    KP_FACTOR_MAX = 100
    KI_FACTOR_MIN = 5
    KI_FACTOR_MAX = 30
    KD_FACTOR_MIN = 0
    KD_FACTOR_MAX = 10
    AVG_PWR_SAMPLING_MIN = 1
    AVG_PWR_SAMPLING_MAX = 30
    PID_UPDATE_SAMPLING_MIN = 1
    PID_UPDATE_SAMPLING_MAX = 10

    # Attribute ranges dictionary - references the constants above for easy maintenance
    ATTRIBUTE_RANGES = {
        'power-allocation-1': {'low': POWER_ALLOCATION_MIN, 'high': POWER_ALLOCATION_MAX},
        'power-allocation-2': {'low': POWER_ALLOCATION_MIN, 'high': POWER_ALLOCATION_MAX},
        'max-integral-1': {'low': MAX_INTEGRAL_MIN, 'high': MAX_INTEGRAL_MAX},
        'max-integral-2': {'low': MAX_INTEGRAL_MIN, 'high': MAX_INTEGRAL_MAX},
        'kp-factor-1': {'low': KP_FACTOR_MIN, 'high': KP_FACTOR_MAX},
        'kp-factor-2': {'low': KP_FACTOR_MIN, 'high': KP_FACTOR_MAX},
        'ki-factor-1': {'low': KI_FACTOR_MIN, 'high': KI_FACTOR_MAX},
        'ki-factor-2': {'low': KI_FACTOR_MIN, 'high': KI_FACTOR_MAX},
        'kd-factor-1': {'low': KD_FACTOR_MIN, 'high': KD_FACTOR_MAX},
        'kd-factor-2': {'low': KD_FACTOR_MIN, 'high': KD_FACTOR_MAX},
        'avg-p-wr-num-of-sampling-1': {'low': AVG_PWR_SAMPLING_MIN, 'high': AVG_PWR_SAMPLING_MAX},
        'avg-p-wr-num-of-sampling-2': {'low': AVG_PWR_SAMPLING_MIN, 'high': AVG_PWR_SAMPLING_MAX},
        'pid-up-date-num-of-sampling-1': {'low': PID_UPDATE_SAMPLING_MIN, 'high': PID_UPDATE_SAMPLING_MAX},
        'pid-up-date-num-of-sampling-2': {'low': PID_UPDATE_SAMPLING_MIN, 'high': PID_UPDATE_SAMPLING_MAX},
    }

    NUM_PROFILES_LIMIT = 5
    CHARS_LIMIT = 20
    ACTIVE_PROFILE = 'active-profile'

    class State(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'


class FansConsts:
    FORWARD_DIRECTION = 'B2F'
    BACKWARD_DIRECTION = 'F2B'
    ALL_DIRECTIONS = [FORWARD_DIRECTION, BACKWARD_DIRECTION]
    FEATURE_ENABLED = 'enabled'
    FEATURE_DISABLED = 'disabled'
    STATE_OK = 'ok'
    STATE_NOT_OK = 'Not OK'
    STATE_ABSENT = 'absent'
    FAN_DIRECTION_MISMATCH_ERR = "is not aligned with fan1 direction"
    FAN_DIRECTION_MISMATCH_ERR_CROC = "direction intake is not aligned"
    FAN_STATUS_LED = "FAN_STATUS"
    FAN_FAULT_FILE = "/var/run/hw-management/thermal/fan{}_fault"
    FAN_SPEED_OUT_OF_RANGE = "speed is out of range"
    FAN_NOT_WORKING = 'not working'
    FAN_LOW_SPEED_WARNING = 'low'
    HW_MANAGEMENT_TC_SERVICE = 'hw-management-tc.service'


class IbConsts:
    MAX_NODES = 'max-nodes'
    MAX_NODES_DEFAULT_VALUE = '2048'
    IS_RUNNING = 'is-running'
    IS_RUNNING_NO = 'no'
    IS_RUNNING_YES = 'yes'
    SM_STATE = 'state'
    SM_STATE_ENABLE = 'enabled'
    SM_STATE_DISABLE = 'disabled'
    SM_PRIORITY = 'sm-priority'
    SM_SL = 'sm-sl'
    PRIO_SL_DEFAULT_VALUE = '0'
    FILES = 'files'
    SIGNAL_DEGRADE_STATE = "state"
    SIGNAL_DEGRADE_ACTION = "action"
    SIGNAL_DEGRADE_STATE_ENABLED = "enabled"
    SIGNAL_DEGRADE_STATE_DISABLED = "disabled"
    SIGNAL_DEGRADE_ACTION_SHUTDOWN = "shutdown"
    SIGNAL_DEGRADE_ACTION_NO_SHUTDOWN = "no-shutdown"
    SIGNAL_DEGRADE = "signal-degrade"
    DEVICE_ASIC_PREFIX = 'ASIC'
    SWID = "SWID"
    IPOIB_INT = "ib{}"
    IPOIB_INT0 = IPOIB_INT.format(0)
    IPOIB_INT1 = IPOIB_INT.format(1)
    DEVICE_SYSTEM = 'SYSTEM'
    DEVICE_ASIC_TOP_FIELDS = ['ib-subnet', 'type']
    DEVICE_ASIC_SUBNET_KEY = 'ib-subnet'
    DEVICE_ASIC_DEFAULT_SUBNET = 'infiniband-default'
    DEVICE_ASIC_SUBNET_FIELDS = ['guid', 'lid']
    DEVICE_SYSTEM_LIST = ['guid']
    GUID_FORMAT = "[0-9a-f]{2}([:])[0-9a-f]{2}(\\1[0-9a-f]{2}){6}$"
    IBDIAGNET_PATH = '/var/tmp/ibdiagnet2'
    IBDIAGNET_ZIPPED_FOLDER_PATH = '/host/ibdiagnet'
    IBDIAGNET_COMMAND = 'ibdiagnet'
    IBDIAGNET_FILE_NAME = 'ibdiagnet2_output.tgz'
    IBDIAGNET_LOG_FINE_MIN_LINES = 50
    IBDIAGNET_PHY_INFO = '--get_phy_info'
    IBDIAGNET_CABLE_INFO = '--get_cable_info'
    IBDIAGNET_EXPECTED_FILES_LIST = ['ibdiagnet2.db_csv', 'ibdiagnet2.ibnetdiscover', 'ibdiagnet2.log',
                                     'ibdiagnet2.lst',
                                     'ibdiagnet2.net_dump', 'ibdiagnet2.nodes_info', 'ibdiagnet2.pkey', 'ibdiagnet2.pm',
                                     'ibdiagnet2.sm', 'ibdiagnet2.vports', 'ibdiagnet2.vports_pkey',
                                     'ibdiagnet2.debug', 'ibdiagnet2.net_dump_ext']
    IBDIAGNET_EXPECTED_MESSAGE = 'ibdiagnet output files were archived into ibdiagnet2_output.tgz'
    IB_INTERFACE_NAME_REGEX = "([a-zA-Z]+)(\d+)(p\d+)"  # noqa: E402
    IB_DEV_2_NET_DEV = 'ibdev2netdev'
    IBSWITCHES = 'ibswitches'
    BASE_LID = 'ibstat | grep "Base lid"'
    MAX_NUM_OF_BYTES = '8388608'
    IB_SEND_LAT_SERVER = ('ib_send_lat -F -s ' + MAX_NUM_OF_BYTES + ' -D ' +
                          '{traffic_duration}' + ' -d {ib_device} > ' + '{server_output}' + ' &')
    IB_SEND_LAT_CLIENT = ('ib_send_lat -F -s ' + MAX_NUM_OF_BYTES + ' -D ' +
                          '{traffic_duration}' + ' {server_ip} -d {ib_device} > ' + '{client_output}' + ' &')
    IB_SEND_LAT_ITERATION_TOLERANCE = 1  # Allowed difference between sent and received iterations
    GET_JOB_IB = 'jobs -l'
    HFNM = 'hfnm'


class PhyRoleConsts:
    PHY_ROLE: str = 'phy-role'

    class PhyRole(Enum):
        PRIMARY: str = 'primary'
        SECONDARY: str = 'secondary'
        FW_DEFAULT: str = 'fw-default'
        AUTO: str = 'auto'

        @classmethod
        def all(cls):
            return [member.value for member in cls]

        @classmethod
        def operational(cls):
            return [cls.PRIMARY.value, cls.SECONDARY.value]

    PHY_ROLE_OPERATIONAL_DEFAULT_VALUES = (PhyRole.PRIMARY, PhyRole.SECONDARY)
    PHY_ROLE_APPLIED_DEFAULT: str = PhyRole.FW_DEFAULT

    CONSTANT_ROLE: str = 'constant-role'

    class ConstantRole(Enum):
        ENABLED: str = 'enabled'
        DISABLED: str = 'disabled'

        @classmethod
        def all(cls):
            return [member.value for member in cls]

    CONSTANT_ROLE_DEFAULT: str = ConstantRole.ENABLED


class LowPowerConsts:
    PEC_DURATION: str = 'peq-duration'
    PEC_RECAL_PERIOD: str = 'peq-recal-period'
    PEC_RECAL_FORCE_PERIOD: str = 'peq-recal-force-period'
    FAE_ALL_PARAMS: list[str] = [PEC_DURATION, PEC_RECAL_PERIOD, PEC_RECAL_FORCE_PERIOD]

    class PecRecalPeriodForce(Enum):
        USE_AGREED_FUNCTION = 'use-agreed-function'
        FORCE_PERIOD = 'force-period'

        @classmethod
        def all(cls):
            return [member.value for member in cls]

    FAE_FIELDS = {
        PEC_DURATION: {"values": range(0, 65535), "default": 379},
        PEC_RECAL_PERIOD: {"values": range(0, 65535), "default": 8008},
        PEC_RECAL_FORCE_PERIOD: {"values": PecRecalPeriodForce.all(), "default": PecRecalPeriodForce.USE_AGREED_FUNCTION}
    }

    L1_TOTAL_ENTRIES: str = 'l1-total-entries'
    AVERAGE_LOCAL_FULL_BW_EXIT: str = 'average-local-full-bw-exit'
    ALL_COUNTERS: list[str] = [L1_TOTAL_ENTRIES, AVERAGE_LOCAL_FULL_BW_EXIT]


class LinkTrainingConsts:
    FEC_MEASURE_MODE: str = 'fec-measure-mode'

    class FecMeasureMode(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'
        FW_DEFAULT = 'fw-default'

        @classmethod
        def all(cls):
            return [member.value for member in cls]

        @classmethod
        def operational(cls):
            return [cls.ENABLED.value, cls.DISABLED.value]

    FEC_MEASURE_MODE_OPERATIONAL_DEFAULT = FecMeasureMode.ENABLED
    FEC_MEASURE_MODE_APPLIED_DEFAULT = FecMeasureMode.FW_DEFAULT

    FEC_MEASURE_FAIL_ACTION: str = 'fec-measure-fail-action'

    class FecMeasureFailAction(Enum):
        FW_DEFAULT = 'fw-default'
        FORCE_LINKUP = 'force-linkup'
        GOTO_POLLING = 'goto-polling'
        GOTO_DISABLE = 'goto-disable'

        @classmethod
        def all(cls):
            return [member.value for member in cls]

        @classmethod
        def operational(cls):
            return [cls.FORCE_LINKUP.value, cls.GOTO_POLLING.value, cls.GOTO_DISABLE.value]

    FEC_MEASURE_FAIL_ACTION_OPERATIONAL_DEFAULT = FecMeasureFailAction.FORCE_LINKUP
    FEC_MEASURE_FAIL_ACTION_APPLIED_DEFAULT = FecMeasureFailAction.FW_DEFAULT

    FAIL_ACTION_PRIORITY: dict[str, int] = {
        FecMeasureFailAction.FW_DEFAULT.value: 0,
        FecMeasureFailAction.FORCE_LINKUP.value: 1,
        FecMeasureFailAction.GOTO_POLLING.value: 2,
        FecMeasureFailAction.GOTO_DISABLE.value: 3,
    }

    FORCE_MAX_ITERATIONS: str = 'force-max-iterations'

    class ForceMaxIterations(Enum):
        DISABLED = 'disabled'
        ENABLED = 'enabled'

        @classmethod
        def all(cls):
            return [member.value for member in cls]

    FORCE_MAX_ITERATIONS_OPERATIONAL_DEFAULT = ForceMaxIterations.DISABLED
    FORCE_MAX_ITERATIONS_APPLIED_DEFAULT = ForceMaxIterations.DISABLED


class ImageConsts:
    NEXT_IMG = 'next'
    CURRENT_IMG = 'current'
    PARTITION1_IMG = 'partition1'
    PARTITION2_IMG = 'partition2'
    PARTITION = 'partition'
    BUILD_ID = 'build-id'
    OTHER = 'other'
    TYPE = 'type'
    ASIC = 'asic'
    SWID = 'swid'
    FW_ASIC = 'ASIC'
    FW_STABLE_VERSION = 'rel-31_2010_4100-004-EVB.mfa'
    XDR_FW_STABLE_VERSION = 'rel-35_2014_1452.mfa'
    SCP_PATH = 'scp://{}:{}@{}'.format(NvosConst.ROOT_USER, NvosConst.ROOT_PASSWORD,
                                       'fit70')
    SCP_PATH_SERVER = 'scp://{username}:{password}@{ip}{path}'
    NVOS_RELEASE_25_02_1000 = "25.02.1000"
    CL_RELEASE_5_16_0 = "5.16.0"
    CL_BASE_IMAGE_VERSION_TO_INSTALL = "cumulus-linux-mlx-amd64.bin"
    CL_BASE_IMAGE_VERSION_TO_INSTALL_PATH = "/devenv/release/{pre_release_name}/passed-mlx-amd64/build/images/{base_image}"


class RbacConsts:
    ALLOW = 'allow'
    DENY = 'deny'
    PERMISSION = 'permission'
    READ_ONLY = 'ro'
    READ_WRITE = 'rw'
    ACTION = 'act'
    ALL = 'all'
    PERMISSION_LEVELS = ["ro", "rw", "act", "all"]
    CONFLICT_ERR_MSG = 'Cannot have more allow permissions than parent'
    USED_ROLE_MSG = 'being modified is already part of'
    CLASS_LIMIT = 64
    ROLE = 'role'
    NMX_RBAC_FILE_USER_PATH = '/auto/sw_system_project/NVOS_INFRA/verification_files/nmx/rbac_user.yml'
    NMX_RBAC_FILE_SPIFFE_PATH = '/auto/sw_system_project/NVOS_INFRA/verification_files/nmx/rbac_spiffe.yml'
    NMX_RBAC_FILE_BAD_PATH = '/auto/sw_system_project/NVOS_INFRA/verification_files/nmx/rbac_bad.yml'
    RBAC_MODE_SPIFFE = 'spiffe'
    RBAC_MODE_USERNAME_PASSWORD = 'username-password'


class TcpDumpConsts:
    LLDP_CHASIS_ID = "chasis_id"
    LLDP_PORT_ID = "port_id"
    LLDP_TIME_TO_LIVE = "time_to_live"
    LLDP_SYSTEM_NAME = "system_name"
    LLDP_SYSTEM_DESCRIPTION = "system_description"
    LLDP_SYSTEM_CAPABILITIES = "system_capabilities"
    LLDP_ENABLED_CAPABILITIES = "enabled_capabilities"
    LLDP_IPV4 = "IPv4"
    LLDP_IPV6 = "IPv6"
    LLDP_PORT_DESCRIPTION = "port_description"


class NtpConsts:
    class Authentication(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'

    class Dhcp(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'
        DHCP_FOLDER = '/var/lib/dhcp/'

    class State(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'

    class Trusted(Enum):
        YES = 'yes'
        NO = 'no'

    class AssociationType(Enum):
        SERVER = 'server'
        PEER = 'peer'
        POOL = 'pool'

    class Status(Enum):
        SYNCHRONISED = 'synchronised'
        UNSYNCHRONISED = 'unsynchronised'

    class Vrf(Enum):
        DEFAULT = 'default'
        MGMT = 'mgmt'

    class Version(Enum):
        VERSION_3 = '3'
        VERSION_4 = '4'

    class Iburst(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'

    class KeyType(Enum):
        MD5 = 'md5'
        SHA1 = 'sha1'

    class Listen(Enum):
        ETH0 = 'eth0'
        ETH1 = 'eth1'

    AUTHENTICATION = 'authentication'
    DHCP = 'dhcp'
    LISTEN = 'listen'
    OFFSET = 'offset'
    REFERENCE = 'reference'
    SERVER = 'server'
    STATE = 'state'
    STATUS = 'status'
    SOURCE = 'source'
    VRF = 'vrf'
    KEY = 'key'
    VALUE = 'value'
    TYPE = 'type'
    RESOLVE_AS = 'resolve-as'
    ASSOCIATION_TYPE = 'association-type'
    IBURST = 'iburst'
    VERSION = 'version'
    TRUSTED = 'trusted'
    SERVER_ID = 'server-id'
    KEY_1 = '6'
    KEY_2 = '9876'
    KEY_VALUE = 'v1234'
    KEY1_VALUE = 'temp_value'
    KEY2_VALUE = 'temp_value123'
    # NBU lab DHCP-provided NTP servers (ib-01/ib-02.yk-il.nvidia.com), reachable across all lab networks
    # Can be verified via: nv show interface eth1 ipv4 dhcp-client lease
    SERVER1_IPV4 = '10.80.74.100'
    SERVER2_IPV4 = '10.80.74.101'
    HOSTNAME_SUFFIX = '.yk-il.nvidia.com'
    SERVER2_HOSTNAME = 'ib-02' + HOSTNAME_SUFFIX
    SERVER3_IPV4 = '10.7.77.136'
    SERVER4_IPV4 = 'time1.google.com'
    SERVER5_IPV4 = 'google.com'
    SERVER6_IPV4 = '0.cumulusnetworks.pool.ntp.org'
    SERVER7_IPV4 = '1.cumulusnetworks.pool.ntp.org'
    SERVER8_IPV4 = '2.cumulusnetworks.pool.ntp.org'
    SERVER9_IPV4 = '3.cumulusnetworks.pool.ntp.org'
    SERVER10_IPV4 = '4.cumulusnetworks.pool.ntp.org'
    DUMMY_SERVER1 = 'dummy.server1'
    DUMMY_SERVER2 = 'dummy.server2'
    DUMMY_SERVER3 = 'dummy.server3'
    DUMMY_SERVER4 = 'dummy.server4'
    DUMMY_SERVER5 = 'dummy.server5'
    DUMMY_SERVER6 = 'dummy.server6'
    DUMMY_SERVER7 = 'dummy.server7'
    DUMMY_SERVER8 = 'dummy.server8'
    SERVER_FAILED = 'DNS resolution failed'
    USER_CONFIG = 'user-config'
    MULTIPLE_SERVERS_NUMBER = 11
    CONFIG_TIME_DIFF_THRESHOLD = 2.5  # [sec]
    SHOW_TIME_DIFF_THRESHOLD = 0.5  # [sec]
    SYNCHRONIZE_NEW_LISTEN_TIME = 20  # [sec]
    SYNCHRONIZE_TIME = 80  # [sec]
    SYNCHRONIZATION_MAX_TIME = 100  # [sec]
    SYNCHRONIZATION_TIME_AFTER_REBOOT = 60  # [sec]
    CONFIG_TIME = 10  # [sec]
    NUMBER_OF_ITERATION = 5
    OLD_DATE = '2 OCT 2006 18:00:00'  # [Date and Time]
    NTP_MAX_DIFF_TIME = 180  # [sec]
    NTP_SERVER_FILES = "/auto/sw_system_project/NVOS_INFRA/verification/ntp/*"

    INVALID_STATE = 'enable1'
    INVALID_AUTHENTICATION = 'disable1'
    INVALID_LISTEN = 'invalid_eth'
    INVALID_DHCP = 'enabled1'
    INVALID_VRF = 'temp_str'
    INVALID_HIGHER_KEY = '65536'
    INVALID_LOWER_KEY = '0'
    INVALID_KEY_TYPE = '0'
    INVALID_KEY_TRUSTED = 'noo'
    INVALID_SERVER = '1234.1234'
    INVALID_SERVER_ASSOCIATION_TYPE = 'server1'
    INVALID_SERVER_STATE = 'disable2'
    INVALID_SERVER_HIGHER_KEY = '100000'
    INVALID_SERVER_LOWER_KEY = '-565'
    INVALID_SERVER_TRUSTED = 'server2'
    INVALID_SERVER_VERSION = '5'

    LOG_MSG_UNSET_NTP = "NtpCfg: Set global config: {'admin_state': 'disabled', 'authentication': 'disabled', " \
                        "'dhcp': 'disabled', 'server_role': 'disabled', 'src_intf': 'eth0', 'vrf': 'default'}"
    LOG_MSG_SERVER_CONFIG = f"servers: {{'{SERVER1_IPV4}': {{'admin_state': 'enabled', 'association_type': 'server', " \
        f"'iburst': 'off', 'resolve_as': '{SERVER1_IPV4}', 'trusted': 'no', 'version': '4'}}}}"
    LOG_MSG_SERVER_CONFIG_UPDATE = f"servers: {{'{SERVER1_IPV4}': {{'admin_state': 'disabled', " \
        f"'association_type': 'server', 'iburst': 'off', 'key': '6', " \
        f"'resolve_as': '{SERVER1_IPV4}', 'trusted': 'yes', 'version': '3'}}}}"
    LOG_MSG_SERVER_CONFIG_KEY = "NtpCfg: Set keys: {'6': {'trusted': 'yes', 'type': 'SHA1'}}"

    LOG_MSG_LIST = [LOG_MSG_UNSET_NTP, LOG_MSG_SERVER_CONFIG, LOG_MSG_SERVER_CONFIG_UPDATE, LOG_MSG_SERVER_CONFIG_KEY]
    #   LOG_MSG_SERVER_CONFIG_VRF = "..."  # Currently not supported

    NTP_DEFAULT_DICT = {
        AUTHENTICATION: Authentication.DISABLED.value,
        DHCP: Dhcp.ENABLED.value,
        LISTEN: {Listen.ETH0.value: {}},
        SERVER: {},
        STATE: State.ENABLED.value,
        STATUS: Status.UNSYNCHRONISED.value,
        VRF: Vrf.DEFAULT.value
    }
    SERVER_DEFAULT_VALUES_DICT = {
        IBURST: Iburst.DISABLED.value,
        ASSOCIATION_TYPE: AssociationType.SERVER.value,
        RESOLVE_AS: SERVER_FAILED,
        SOURCE: USER_CONFIG,
        STATE: State.ENABLED.value,
        TRUSTED: Trusted.NO.value,
        VERSION: Version.VERSION_4.value
    }
    SERVER1_DEFAULT_VALUES_DICT = {
        IBURST: Iburst.DISABLED.value,
        ASSOCIATION_TYPE: AssociationType.SERVER.value,
        RESOLVE_AS: SERVER1_IPV4,
        SOURCE: USER_CONFIG,
        STATE: State.ENABLED.value,
        TRUSTED: Trusted.NO.value,
        VERSION: Version.VERSION_4.value
    }
    SERVER2_DEFAULT_VALUES_DICT = {
        IBURST: Iburst.DISABLED.value,
        ASSOCIATION_TYPE: AssociationType.SERVER.value,
        RESOLVE_AS: SERVER2_IPV4,
        SOURCE: USER_CONFIG,
        STATE: State.ENABLED.value,
        TRUSTED: Trusted.NO.value,
        VERSION: Version.VERSION_4.value
    }
    MULTIPLE_SERVERS_DEFAULT_DICT = {
        SERVER1_IPV4: {},
        SERVER2_HOSTNAME: {},
        DUMMY_SERVER1: {},
        DUMMY_SERVER2: {},
        DUMMY_SERVER3: {},
        DUMMY_SERVER4: {},
        DUMMY_SERVER5: {},
        DUMMY_SERVER6: {},
        DUMMY_SERVER7: {},
        DUMMY_SERVER8: {},
    }
    MULTIPLE_SERVERS_CONFIG_DICT = {
        SERVER1_IPV4: SERVER1_DEFAULT_VALUES_DICT,
        SERVER2_HOSTNAME: SERVER2_DEFAULT_VALUES_DICT,
        DUMMY_SERVER1: SERVER_DEFAULT_VALUES_DICT,
        DUMMY_SERVER2: SERVER_DEFAULT_VALUES_DICT,
        DUMMY_SERVER3: SERVER_DEFAULT_VALUES_DICT,
        DUMMY_SERVER4: SERVER_DEFAULT_VALUES_DICT,
        DUMMY_SERVER5: SERVER_DEFAULT_VALUES_DICT,
        DUMMY_SERVER6: SERVER_DEFAULT_VALUES_DICT,
        DUMMY_SERVER7: SERVER_DEFAULT_VALUES_DICT,
        DUMMY_SERVER8: SERVER_DEFAULT_VALUES_DICT,
    }
    SERVER_NONE_DEFAULT_VALUES_DICT = {
        IBURST: Iburst.ENABLED.value,
        ASSOCIATION_TYPE: AssociationType.SERVER.value,
        KEY: KEY_1,
        RESOLVE_AS: SERVER1_IPV4,
        STATE: State.DISABLED.value,
        TRUSTED: Trusted.YES.value,
        VERSION: Version.VERSION_3.value
    }
    SERVER_DISABLED_DICT = {
        IBURST: Iburst.DISABLED.value,
        ASSOCIATION_TYPE: AssociationType.SERVER.value,
        KEY: KEY_1,
        RESOLVE_AS: SERVER1_IPV4,
        STATE: State.DISABLED.value,
        TRUSTED: Trusted.YES.value,
        VERSION: Version.VERSION_3.value
    }
    KEY_DEFAULT_DICT = {}
    KEY_CONFIGURED_DICT = {
        TRUSTED: Trusted.NO.value,
        TYPE: KeyType.MD5.value,
        VALUE: '*'
    }
    REFERENCE_DEFAULT_VALUE = 'local'
    EXPECTED_DURATION = 300
    CUMULUS_NTP_DEFAULT_DICT = {
        LISTEN: {Listen.ETH0.value: {}},
        SERVER: {},
        STATE: State.ENABLED.value,
        STATUS: Status.UNSYNCHRONISED.value,
        VRF: Vrf.MGMT.value
    }
    CUMULUS_EXPECTED_DURATION = 400
    CUMULUS_SERVER1_DEFAULT_VALUES_DICT = {
        IBURST: Iburst.ENABLED.value,
        ASSOCIATION_TYPE: AssociationType.SERVER.value,
        STATE: State.ENABLED.value,
        VERSION: Version.VERSION_4.value
    }
    CUMULUS_SERVER_NONE_DEFAULT_VALUES_DICT = {
        IBURST: Iburst.ENABLED.value,
        ASSOCIATION_TYPE: AssociationType.SERVER.value,
        STATE: State.DISABLED.value,
        VERSION: Version.VERSION_3.value
    }
    CUMULUS_MULTIPLE_SERVERS_CONFIG_DICT = {
        SERVER1_IPV4: CUMULUS_SERVER1_DEFAULT_VALUES_DICT,
        SERVER2_HOSTNAME: CUMULUS_SERVER1_DEFAULT_VALUES_DICT,
        DUMMY_SERVER1: CUMULUS_SERVER1_DEFAULT_VALUES_DICT,
        DUMMY_SERVER2: CUMULUS_SERVER1_DEFAULT_VALUES_DICT,
        DUMMY_SERVER3: CUMULUS_SERVER1_DEFAULT_VALUES_DICT,
        DUMMY_SERVER4: CUMULUS_SERVER1_DEFAULT_VALUES_DICT,
        DUMMY_SERVER5: CUMULUS_SERVER1_DEFAULT_VALUES_DICT,
        DUMMY_SERVER6: CUMULUS_SERVER1_DEFAULT_VALUES_DICT,
        DUMMY_SERVER7: CUMULUS_SERVER1_DEFAULT_VALUES_DICT,
        DUMMY_SERVER8: CUMULUS_SERVER1_DEFAULT_VALUES_DICT
    }


class TelemetryConsts:
    """Constants for `nv … system telemetry …` commands.

    Field names (constants ending without _* suffix) are exactly the kebab-case
    keys used both in the CLI command words and the `--output json` payloads.
    """

    # --- Field names (used as op_param_name in BaseComponent.set/unset, and as JSON keys) ---
    STATE = 'state'
    SAMPLE_INTERVAL = 'sample-interval'
    VRF = 'vrf'
    INSECURE = 'insecure'
    CERTIFICATE = 'certificate'
    CLIENT_CERTIFICATE = 'client-certificate'
    PORT = 'port'
    ENCODING = 'encoding'
    STATS_GROUP = 'stats-group'
    DESCRIPTION = 'description'

    # --- Top-level telemetry subtree node names (JSON keys when calling
    # `nv show system telemetry --output json`) ---
    EXPORT = 'export'
    OTLP = 'otlp'
    GRPC = 'grpc'
    HTTP = 'http'
    DESTINATION = 'destination'
    LABEL = 'label'
    INTERFACE_STATS = 'interface-stats'
    PEER_PORT_STATS = 'peer-port-stats'
    IB_ROUTER_STATS = 'ib-router-stats'
    PLATFORM_STATS = 'platform-stats'
    CLASS = 'class'
    PHY = 'phy'

    # Tuple of all stats subtree names. Useful for iterating over every family
    # (interface / peer-port / ib-router / platform) in tests or helpers.
    ALL_STATS_SUBTREES = (
        INTERFACE_STATS,
        PEER_PORT_STATS,
        IB_ROUTER_STATS,
        PLATFORM_STATS,
    )

    # --- Enums ---
    class State(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'

    class Encoding(Enum):
        PROTO = 'proto'
        JSON = 'json'

    # --- Platform-stats class names (each is a child of `platform-stats class`) ---
    PLATFORM_CLASS_CPU = 'cpu'
    PLATFORM_CLASS_DISK = 'disk'
    PLATFORM_CLASS_MEMORY = 'memory'
    PLATFORM_CLASS_HEALTH_INFO = 'health-info'
    PLATFORM_CLASS_ASIC_POWER = 'asic-power'
    PLATFORM_CLASS_PLATFORM_INFO = 'platform-info'
    PLATFORM_CLASS_ENVIRONMENT_SENSOR = 'environment-sensor'
    PLATFORM_CLASS_TRANSCEIVER_INFO = 'transceiver-info'

    PLATFORM_CLASSES = [
        PLATFORM_CLASS_CPU,
        PLATFORM_CLASS_DISK,
        PLATFORM_CLASS_MEMORY,
        PLATFORM_CLASS_HEALTH_INFO,
        PLATFORM_CLASS_ASIC_POWER,
        PLATFORM_CLASS_PLATFORM_INFO,
        PLATFORM_CLASS_ENVIRONMENT_SENSOR,
        PLATFORM_CLASS_TRANSCEIVER_INFO,
    ]

    # --- Numeric ranges (per the CLI grammar) ---
    SAMPLE_INTERVAL_MIN = 1
    SAMPLE_INTERVAL_MAX = 86400
    PLATFORM_SAMPLE_INTERVAL_MIN = 60
    PLATFORM_SAMPLE_INTERVAL_MAX = 86400
    PORT_MIN = 1
    PORT_MAX = 65535

    # --- Factory defaults (observed via `nv show system telemetry` on a fresh system) ---
    class Defaults:
        EXPORT_VRF = 'default'
        OTLP_STATE = 'disabled'
        GRPC_INSECURE = 'disabled'
        GRPC_PORT = 4317
        HTTP_INSECURE = 'disabled'
        HTTP_PORT = 4318
        HTTP_ENCODING = 'proto'

        # Default `state` for export and class.phy nodes of interface-stats / peer-port-stats /
        # ib-router-stats / platform-stats.export
        STATS_STATE = 'disabled'

        # Top-level sample-interval default for interface-stats / peer-port-stats / ib-router-stats
        STATS_SAMPLE_INTERVAL = 1

        # platform-stats has no top-level sample-interval; it lives under export
        PLATFORM_EXPORT_SAMPLE_INTERVAL = 60

        # platform-stats class categories (cpu, disk, memory, ...) default to enabled — this is the
        # one exception to the "everything starts disabled" rule
        PLATFORM_CLASS_STATE = 'enabled'


class RebootConsts:
    WAIT_TIME_BEFORE_REBOOT = 120
    HALT = "halt"
    COLD = "cold"
    IMMEDIATE = "immediate"
    POWER_CYCLE = "power-cycle"
    WARM = "warm"
    FAST = "fast"
    FACTORY_RESET = 'factory-reset'
    POWER_BUTTON = SystemConsts.REBOOT_REASON_POWER_BUTTON
    KERNEL_PANIC = 'Kernel Panic'
    PSU_OFF = "psu-off"
    REBOOT_REASON_POWER_CYCLE = 'Power Cycle'
    REBOOT_REASON_RESET_BIT = "System reset"
    REMOTE_REBOOT = "remote-reboot"
    INSTALL_FW = "install-fw"
    RESET_BIT = "reset_bit"

    DEFAULT_MODES = [POWER_CYCLE, HALT, COLD, IMMEDIATE]
    REBOOT_USER_NA = "N/A"
    REBOOT_USER_ADMIN = "admin"
    REBOOT_USER_SYSTEM = "system"

    POWER_CYCLE_NOT_SUPPORTED_ERR_MSG = "Power cycle mode is not supported on this system"


class PasswordHardeningConsts:
    # Field names
    DIGITS_CLASS = 'digits-class'
    EXPIRATION = 'expiration'
    EXPIRATION_WARNING = 'expiration-warning'
    HISTORY_CNT = 'history-cnt'
    LEN_MIN = 'len-min'
    LOWER_CLASS = 'lower-class'
    REJECT_USER_PASSW_MATCH = 'reject-user-passw-match'
    SPECIAL_CLASS = 'special-class'
    STATE = 'state'
    UPPER_CLASS = 'upper-class'

    class State(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'

    class DigitsClass(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'

    class LowerClass(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'

    class UpperClass(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'

    class SpecialClass(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'

    class RejectUserPasswMatch(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'

    # Default values
    PASSWORD_HARDENING_DEFAULT_DICT = {
        DIGITS_CLASS: DigitsClass.DISABLED.value,
        EXPIRATION: '99999',
        EXPIRATION_WARNING: '7',
        HISTORY_CNT: 0,
        LEN_MIN: 1,
        LOWER_CLASS: LowerClass.DISABLED.value,
        REJECT_USER_PASSW_MATCH: RejectUserPasswMatch.DISABLED.value,
        SPECIAL_CLASS: SpecialClass.DISABLED.value,
        STATE: State.DISABLED.value,
        UPPER_CLASS: UpperClass.DISABLED.value
    }


class SyslogConsts:
    FORMAT = 'format'
    FIREWAL_NAME = 'firewall-name'
    SEVERITY = 'severity'
    SERVER = 'server'
    SERVER_ID = 'server-id'
    SELECTOR = 'selector'
    SELECTOR_ID = 'selector-id'
    FILTER = 'filter'
    EXCLUDE = 'exclude'
    INCLUDE = 'include'
    PORT = 'port'
    DAEMON = 'daemon'
    RATE_LIMIT = 'rate-limit'
    INTERVAL = 'interval'
    BURST = 'burst'
    MATCH = 'match'
    ACTION = 'action'
    VRF = 'vrf'
    TCP = 'tcp'
    UDP = 'udp'
    DEFAULT_VRF = 'default'
    DEFAULT_PORT = 514
    DEFAULT_PROGRAM = 'switchd'
    DEFAULT_SELECTOR_NAME = 'selector-1'
    PROGRAM_NAME = 'program-name'
    FACILITY = 'facility'
    PROTOCOL = 'protocol'
    STANDARD = 'standard'
    WELF = 'welf'
    MODULE_LINE = "module(load=\"im{protocol}\")"
    PORT_LINE = "input(type=\"im{protocol}\" port=\"{port}\")"
    RSYSLOG_CONF_FILE = '/etc/rsyslog.conf'
    MULTIPLE_SERVERS_NUMBER = 10
    CONFIG_TIME_DIFF_THRESHOLD = 1.0  # [sec]
    SHOW_TIME_DIFF_THRESHOLD = 1.0  # [sec]
    NVUE_LOG_PATH = "/var/log/nv-cli.log"
    SYSLOG_LOG_PATH = "/var/log/syslog"


class ClusterAppsLogLevels:
    CRITICAL = 'critical'
    ERROR = 'error'
    WARNING = 'warn'
    NOTICE = 'notice'
    INFO = 'info'
    DEBUG = 'debug'


class ClusterApps:
    NMX_CONTROLLER = 'nmx-controller'
    NMX_TELEMETRY = 'nmx-telemetry'
    ALL_APPS = [NMX_CONTROLLER, NMX_TELEMETRY]


class DateTimeConsts:
    DATE_TIME = "date-time"
    LOCAL_TIME = "local-time"
    UNIVERSAL_TIME = "universal-time"
    RTC_TIME = "rtc-time"
    TIMEZONE = "timezone"
    SYSTEM_CLOCK_SYNCHRONIZED = "system-clock-synchronized"
    NTP_SERVICE = "ntp-service"
    RTC_IN_LOCAL_TZ = "rtc-in-local-tz"
    UNIX_TIME = "unix-time"

    class NtpService(Enum):
        ACTIVE = 'active'
        INACTIVE = 'inactive'

    class SystemClockSynchronized(Enum):
        YES = 'yes'
        NO = 'no'

    class RtcInLocalTZ(Enum):
        YES = 'yes'
        NO = 'no'


class ClusterConsts:
    APP_VERSION = 'app-ver'
    APP_NAME = 'app-name'
    NMX_CONTROLLER = ClusterApps.NMX_CONTROLLER
    NMX_TELEMETRY = ClusterApps.NMX_TELEMETRY
    NMX_CONTROLLER_PREFIX = 'nmx-c'
    NMX_TELEMETRY_PREFIX = 'nmx-t'
    NMX_RBAC_FILE = 'rbac-file'
    NMX_CONTROLLER_ENVOY_PORT = 9370
    NMX_TELEMETRY_ENVOY_PORT = 9351
    NMX_TELEMETRY_PROTO_PATH = '/auto/sw_system_project/NVOS_INFRA/verification_files/nmx/nmx-telemetry.proto'
    NMX_CONTROLLER_PROTO_PATH = '/auto/sw_system_project/NVOS_INFRA/verification_files/nmx/nmx-c.proto'
    TELEMETRY_SERVICES = ['nmx-connector', 'ib-telemetry']
    CONTROLLER_SERVICES = ['nmxc-sdn', 'nmxc-fib', 'redis']
    INITIAL_EXPECTED_APPS = [NMX_CONTROLLER, NMX_TELEMETRY]
    INITIAL_APPS_PATH = '/usr/local/cluster_pkgfiles/'
    INFRA_PACKAGES_PATH = '/host/cluster_infra/packages/'


class SyslogSeverityLevels:
    CRIT = 'crit'
    CRITICAL = 'critical'
    ERROR = 'error'
    WARN = 'warn'
    NOTICE = 'notice'
    INFO = 'info'
    DEBUG = 'debug'
    SEVERITY_LEVEL_LIST = [DEBUG, INFO, NOTICE, WARN, ERROR, CRITICAL]
    SEVERITY_LEVEL_DICT = {DEBUG: DEBUG,
                           # key : severity level to configure, value: priority level to send msg, and show commands
                           INFO: INFO,
                           NOTICE: NOTICE,
                           WARN: WARN,
                           ERROR: ERROR,
                           CRITICAL: CRIT}


class SdnCmdConsts:
    HELP = 'help'
    DOMAIN_STATE_INFO = 'DomainStateInfo'
    TOPOLOGY_INFO = 'TopologyInfo'
    SWITCH_NODE_INFO_LIST = 'SwitchNodeInfoList'
    COMPUTE_NODE_INFO_LIST = 'ComputeNodeInfoList'
    GPU_INFO_LIST = 'GpuInfoList'
    SET_STATIC_CONFIG = 'SetStaticConfig'
    STATIC_CONFIG = 'StaticConfig'
    FACTORY_RESET = 'FactoryReset'
    CREATE_PARTITION = 'CreatePartition'
    DELETE_PARTITION = 'DeletePartition'
    REMOVE_GPUS_FROM_PARTITION = 'RemoveGpusFromPartition'
    ADD_GPUS_TO_PARTITION = 'AddGpusToPartition'
    GET_COMMAND_LIST = [
        HELP, DOMAIN_STATE_INFO, TOPOLOGY_INFO, SWITCH_NODE_INFO_LIST,
        COMPUTE_NODE_INFO_LIST, GPU_INFO_LIST,
    ]
    SET_COMMAND_BASE_LIST = [SET_STATIC_CONFIG, FACTORY_RESET]
    SET_COMMAND_NON_STANDALONE_LIST = [SET_STATIC_CONFIG, FACTORY_RESET, CREATE_PARTITION, REMOVE_GPUS_FROM_PARTITION]


class Cpov2Consts:
    """Gen2 (Portia / QM5 / NVL7) CPO field names and constants.

    Field-name strings for `nv show platform cpo [cpoN]`,
    `nv show platform laser-source [elsN]` and `nv show interface swXpY cpo`.
    The expected topology (counts) lives in CpoTopology, not here.
    """
    # cpo / interface-cpo top-level fields
    STATUS = "status"
    ERROR_STATUS = "error-status"
    IDENTIFIER = "identifier"
    FW_VERSION = "fw-version"
    PARENT = "parent"
    # Mapping fields. The HLD spells these `associated-ports` /
    # `associated-laser-sources`; the shipped CLI drops the prefix.
    PORTS = "ports"
    LASER_SOURCES = "laser-sources"
    # OE membership has no mapping field of its own: the nested OE container
    # (OE below) carries both the names and their data, so its keys are the map.

    # `status` values: the CPO is not user-removable and
    # reports up/down (down when its ELS is removed; down with an empty
    # error-status is NOT unhealthy). The replaceable ELS keeps presence
    # semantics. Values compared case-insensitively until the CLI casing is
    # pinned on a DUT.
    CPO_STATUS_UP = "up"
    CPO_STATUS_DOWN = "down"
    ELS_STATUS_INSERTED = "Inserted"
    ELS_STATUS_REMOVED = "Removed"

    # sub-trees - these double as the CLI path segments of the drill-down
    # commands (`... cpo1 optical-engines oe1`, `... cpo1 channels channel-3`)
    THRESHOLDS = "thresholds"
    OE = "optical-engines"
    CHANNEL = "channels"
    LASER = "laser"

    # channel fields
    CH_RX_POWER = "rx-power"
    CH_TX_POWER = "tx-power"
    CH_RX_LOS = "rx-los"
    CH_TX_LOS = "tx-los"
    CH_TX_FAULT = "tx-fault"
    CH_LASER_SOURCE_INPUT_POWER = "laser-source-input-power"
    CH_FAULT_OPCODE = "advanced-fault-opcode"
    CH_DP_STATE = "dp-state"

    # optical-engine fields
    OE_SERIAL_NUMBER = "serial-number"
    OE_TEMPERATURE = "temperature"

    # laser-source (ELS) fields
    ELS_VENDOR_NAME = "vendor-name"
    ELS_VENDOR_PN = "vendor-pn"
    ELS_VENDOR_SN = "vendor-sn"
    ELS_VENDOR_REV = "vendor-rev"
    ELS_VENDOR_DATE_CODE = "vendor-date-code"
    ELS_POWER_CONSUMPTION = "power-consumption"
    ELS_ICC_CURRENT = "icc-current"

    # per-laser fields
    LASER_ENABLED = "enabled"
    LASER_OPER_STATUS = "oper-status"
    LASER_ERROR_STATUS = "error-status"
    LASER_RAMPING_STATUS = "ramping-status"
    LASER_POWER_RESTRICTION = "power-restriction"
    LASER_AGE = "laser-age"
    LASER_TARGET_OUTPUT_POWER = "target-output-power"
    LASER_MPD_CURRENT = "laser-mpd-current"
    LASER_BIAS_CURRENT = "laser-bias-current"
    LASER_TEC_CURRENT = "tec-current"
    LASER_TEC_VOLTAGE = "tec-voltage"
    LASER_TEMPERATURE = "laser-temperature"
    LASER_TX_POWER = "tx-power"
    LASER_HEALTH = "laser-health"
    TEC_HEALTH = "tec-health"
    FREQUENCY_ERROR = "frequency-error"

    # common leaf fields of measured values (power / current), per HLD samples
    DIAGNOSTICS_STATUS = "diagnostics-status"
    TEMPERATURE = "temperature"
    POWER = "power"
    ALARM_STATUS = "alarm-status"
    ALARM_SEVERITY = "alarm-severity"

    # thresholds: `nv show platform cpo <id>` uses THRESHOLDS (plural), keyed by
    # measured value (rx-power / tx-power / laser-source-input-power) with the
    # four bounds below nested under each.
    HIGH_ALARM = "high-alarm"
    LOW_ALARM = "low-alarm"
    HIGH_WARNING = "high-warning"
    LOW_WARNING = "low-warning"

    # `... laser-source <id>` uses THRESHOLD (singular), nested the other way
    # round: severity first (WARNING/ALARM) then tx-power-upper/lower (per HLD
    # samples). WARNING/ALARM are severity levels - distinct from the
    # ALARM_STATUS on/off flag of a measured value.
    WARNING = "warning"
    ALARM = "alarm"
    TX_POWER_UPPER = "tx-power-upper"
    TX_POWER_LOWER = "tx-power-lower"
    THRESHOLD = "threshold"

    # FAE: `nv action activate fae platform laser-source <els-id>` named params
    # (NVUE syntax keeps the 'laser'/'step' keywords, e.g. 'laser laser-4 step laser-up')
    STEP = "step"
    STEP_FIBER_CHECK = "fiber-check"
    STEP_LASER_TUNING = "laser-tuning"
    STEP_LASER_UP = "laser-up"
    STEP_LASER_FINE_TUNE = "laser-fine-tune"
    STEP_POWER_SETPOINT = "power-setpoint"
    # steps 1-3 run by default when no step is given; all 5 are valid step names
    DEFAULT_ACTIVATE_STEPS = [STEP_FIBER_CHECK, STEP_LASER_TUNING, STEP_LASER_UP]
    ALL_ACTIVATE_STEPS = DEFAULT_ACTIVATE_STEPS + [STEP_LASER_FINE_TUNE, STEP_POWER_SETPOINT]

    # FAE: `nv show/set/unset fae system cpo` fields (CLI uses 'z' spelling
    # els-initialization, unlike the HLD section headers)
    CPO_DUMP_STATE = "cpo-dump-state"
    ELS_INITIALIZATION = "els-initialization"
    ELS_INITIALIZATION_PER_LASER = "els-initialization-per-laser"
    INIT_FIBER_TUNING = "fiber-tuning"
    INIT_ERROR = "error"

    # system-health instance-name regexes (component types live in
    # HealthConsts.Component.CPO / Laser_Source). \Z-anchored because they are
    # consumed via re.match: 'cpo1-stale' must NOT pass as a valid instance name.
    CPO_INSTANCE_REGEX = r'cpo\d+\Z'
    ELS_INSTANCE_REGEX = r'els\d+\Z'

    # `nv show system events` wording (HLD Portia samples); the event's
    # Component column (JSON 'resource') carries the instance name (cpoN/elsN)
    CPO_INSERTED_EVENT = "CPO was inserted"
    CPO_EJECTED_EVENT = "CPO was ejected"
    LASER_SOURCE_INSERTED_EVENT = "Laser source was inserted"
    LASER_SOURCE_EJECTED_EVENT = "Laser source was ejected"
    HW_NOT_OK_EVENT_REGEX = r"HW Component health is not ok: .+"
    LASER_STATE_NOT_OK_EVENT_REGEX = r"ELS Operational State is not ok: Laser \d+ .+"
    # recovery events repeat the original text behind this prefix
    CLEARED_EVENT_PREFIX = "Cleared: "
    # temperature high/low events are Rel2B; no constant until wording lands


class HealthConsts:
    OK = "OK"
    NOT_OK = "Not OK"
    IGNORED = "Ignored"
    STATUS = "status"
    STATUS_LED = "status-led"
    LED_OK_STATUS = "green"
    LED_NOT_OK_STATUS = "amber"
    MONITOR_LIST = "monitor-list"
    HEALTH_FIRST_FILE = "health_history"
    HEALTH_SECOND_FILE = "health_history.1"
    HEALTH_MONITOR_CONFIG_FILE_PATH = "/usr/share/sonic/device/{}/system_health_monitoring_config.json"
    ISSUE = "issue"
    ISSUES = "issues"
    ASIC_HEALTH_ISSUE = "ASIC-HEALTH"
    SUMMARY_REGEX_OK = f"INFO {NvosConst.DATE_TIME_REGEX[0]} : Summary: {OK}"
    SUMMARY_REGEX_NOT_OK = f"ERROR {NvosConst.DATE_TIME_REGEX[0]} : Summary: {NOT_OK}"
    ADD_STATUS_TO_SUMMARY_REGEX = NvosConst.DATE_TIME_REGEX[0] + " : Summary:.*"
    HEALTH_ISSUE_REGEX = "ERROR {time_regex} : {component}: (?:is )?{issue}"
    HEALTH_FIX_REGEX = "INFO {time_regex} : Cleared: {component}: (?:is )?{issue}"
    SYSTEM_LOG_HEALTH_REGEX = '.* Health DB change cache.* new data.*\'summary\': \'{}\''
    SYSTEM_LOG_HEALTH_STATUS_REGEX = '.*text.*{}.*type-id.*{}'

    FATAL = "FATAL"
    ASIC_HEALTH_ISSUE_FATAL = "Switch ASIC in fatal state."
    # more constants found at test_fatal_mode.py

    class Component:
        FAN = "fan"
        ASIC = "asic"
        CPU = "cpu"
        PSU = "psu"
        Transceiver = "transceiver"
        Switch = "switch"
        Leakage_Sensor = "leakage-sensor"
        Software = "software"
        # Gen2 CPO health component types (Portia/QM5). COMPONENTS is the global
        # "known component" allowlist (health output validation); per-device
        # expectations come from device.health_components, so listing cpo /
        # laser-source here does not affect non-CPO devices.
        CPO = "cpo"
        Laser_Source = "laser-source"
        COMPONENTS = [FAN, ASIC, CPU, PSU, Transceiver, Switch, Leakage_Sensor, Software, CPO, Laser_Source]
        LAST_HEALTHY = 'last-unhealthy'
        UNHEALTHY_COUNT = 'unhealthy-count'
        STATE = 'state'
        HEALTHY = 'HEALTHY'
        UNHEALTHY = 'UNHEALTHY'
        INSTANCE = 'instance'


class OperationTimeConsts:
    """
    Constants for operation time tracking and database columns.

    Note: Operation duration thresholds have been moved to device classes
    (e.g., IbDevice.expected_operation_durations) for per-device customization.
    """
    OPERATION_COL = 'operation'
    PARAMS_COL = 'params'
    DURATION_COL = 'duration'
    SETUP_COL = 'setup_name'
    VERSION_COL = 'version'
    TYPE_COL = 'machine_type'
    RELEASE_COL = 'release'
    TEST_NAME_COL = 'test_name'
    SESSION_ID_COL = 'session_id'
    DATE_COL = 'date'

    # Operation labels used by save_duration / row-name lookups in expected_operation_durations.
    IMAGE_INSTALL = 'image install'
    IMAGE_UPGRADE = 'image upgrade'
    IMAGE_DOWNGRADE = 'image downgrade'
    IMAGE_INSTALL_OPERATION_ROW = 'image install (operation)'


class StatsConsts:
    class State(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'

    SLEEP_15_SECONDS = 15  # [sec]
    SLEEP_20_SECONDS = 20  # [sec]
    SLEEP_40_SECONDS = 40  # [sec]
    SLEEP_1_MINUTE = 60  # [sec]
    SLEEP_3_MINUTES = 180  # [sec]
    SLEEP_5_MINUTES = 300  # [sec]
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

    LOG_MSG_UNSET_STATS = "PATCH /nvue_v1/system/stats"
    LOG_MSG_SET_CATEGORY1 = "INFO stats-reportd: got config change "
    LOG_MSG_SET_CATEGORY2 = ": {'enabled': 'true', 'history_duration': '365', 'interval': '1'}"
    LOG_MSG_PATCH_CATEGORY = "PATCH /nvue_v1/system/stats/category/"

    LOG_MSG_ERROR_DB = "..."  # TODO: Update message (parameter not found in redis DB)...

    INVALID_CATEGORY_NAME = 'invalid_category_name'
    ALL_CATEGORIES = 'all'
    INVALID_STATE = 'invalid_state'
    INVALID_INTERVAL_LOW = 0
    INVALID_INTERVAL_HIGH = 1441
    INVALID_HISTORY_DURATION_LOW = 0
    INVALID_HISTORY_DURATION_HIGH = 366
    INVALID_FILE_NAME = 'file_not_exists.csv'
    INVALID_SHOW_CATEGORY = 'The requested item does not exist.'

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
    HEADER_HOSTNAME = "# Hostname:         "
    HEADER_GROUP = "# Statistic group:  "
    HEADER_TIME = "# Started sampling: "
    TIMESTAMP_FORMAT = "%b-%d %Y %H:%M:%S"
    SYSTEM_TIME_FORMAT = '%Y-%m-%d %H:%M:%S'
    MAX_ROWS_TO_SCAN = 300
    CONST_HEADER_ROWS = 8
    BIG_FILE_NUM_OF_LINES = 600026

    TEMP_MIN = 15  # [Celsius]
    TEMP_MAX = 90  # [Celsius]
    MGMT_INT_MIN = 0  # [Bytes/sec]
    MGMT_INT_MAX = 10000  # [Bytes/sec]
    FAN_MIN = 0  # [%]
    FAN_MAX = 100  # [%]
    PWR_PSU_VOLT_MIN = 0  # [V] TODO: Update
    PWR_PSU_VOLT_MAX = 300  # [V] TODO: Update
    PWR_PSU_CUR_MIN = 0  # [A] TODO: Update
    PWR_PSU_CUR_MAX = 100  # [A] TODO: Update
    ASIC_PWR_WATT_MIN = 400  # [Watt]
    ASIC_PWR_WATT_MAX = 600  # [Watt]
    CPU_FREE_RAM_MIN = 30  # [%]
    CPU_FREE_RAM_MAX = 100  # [%]
    CPU_UTIL_MIN = 0  # [%]
    CPU_UTIL_MAX = 60  # [%]
    CPU_REBOOT_CNT_MIN = 0
    CPU_REBOOT_CNT_MAX = 100
    DISK_FREE_SPACE_MIN = 30  # [%]
    DISK_FREE_SPACE_MAX = 99  # [%]
    DISK_RMN_LIFE_MIN = 70  # [%]
    DISK_RMN_LIFE_MAX = 100  # [%]
    DISK_FAIL_CNT_MIN = 0
    DISK_FAIL_CNT_MAX = 0
    DISK_TOTAL_LBA_RW_MIN = 10000
    DISK_TOTAL_LBA_RW_MAX = 4294967295
    VOLTAGE_GENERAL_MIN = 0
    VOLTAGE_GENERAL_MAX = 100
    VOLTAGE_PSU_MIN = 0
    VOLTAGE_PSU_MAX = 300

    GENERATE = 'generate'
    DELETE = 'delete'
    UPLOAD = 'upload'
    CLEAR = 'clear'

    # Operation labels used by save_duration / row-name lookups in expected_operation_durations.
    IMAGE_INSTALL = 'image install'
    IMAGE_UPGRADE = 'image upgrade'
    IMAGE_DOWNGRADE = 'image downgrade'
    IMAGE_INSTALL_OPERATION_ROW = 'image install (operation)'


class LinkDetectionConsts:
    PLATFORM_CAPABILITIES = "capabilities"
    PLATFORM_LINK_DETECTION = "link_detection"
    EMPTY_STRING = ""
    SPEED_WIDTH_MISMATCH = "speed_width_mismatch"
    NO_NEGOTIATION = "no_negotiation"
    PLANARIZED_MISMATCH = "planarized_mismatch"
    PLANARIZED = "planarized"
    NUM_OF_PLANES = "num_of_planes"
    SUPPORTED_WIDTH = "supported_width"
    SUPPORTED_SPEED = "supported_speed"
    CONNECTION_MODE_NDR = 'ndr'
    CONNECTION_MODE_XDR = 'xdr'
    CONNECTION_MODE = "connection-mode"

    FEC_MODE = "fec"
    FEC_MODE_AUTO = "auto"
    FEC_MODE_DOUBLE = "double-fec"
    FEC_MODE_QUAD = "quad-fec"
    FEC_MODE_OCTET = "octal-fec"
    FEC_MODE_DEFAULT = FEC_MODE_AUTO
    FEC_MODES = [FEC_MODE_AUTO, FEC_MODE_DOUBLE, FEC_MODE_QUAD, FEC_MODE_OCTET]


class MultiPlanarConsts:
    INTERNAL_PATH = "/tmp/"
    SIMULATION_PATH = "/auto/sw_system_project/NVOS_INFRA/verification/xdr/simulation/"
    A_PORT_SPLIT_SIMULATION_FILE = "split_sw10p1_aport.json"
    FNM_PORT_SPLIT_SIMULATION_FILE = 'fnm_split_platform.json'
    NVL_SIMULATION_FILE = 'nvl_platform.json'
    ORIGIN_FILE = "platform_origin.json"
    ORIGIN_FULL_PATH = SIMULATION_PATH + ORIGIN_FILE
    AGGREGATED_PORT_SIMULATION_FILE = "aggregated_port_platform.json"
    PLATFORM_FILE_FULL_PATH = "/usr/share/sonic/device/{}/platform.json"
    MULTI_PLANAR_KEYS = ['asic', 'parent-alias', 'parent-port', 'plane']

    PHYSICAL_STATE_PARAM = 'SAI_PORT_STAT_INFINIBAND_PHYSICAL_STATE'
    PHYSICAL_DISABLED = '0'  # disabled
    PHYSICAL_SLEEP = '1'  # sleep
    PHYSICAL_POLLING = '2'  # polling
    PHYSICAL_LINKUP = '3'  # linkup
    LOGICAL_STATE_PARAM = 'SAI_PORT_STAT_INFINIBAND_LOGICAL_STATE'
    LOGICAL_DOWN = '0'  # down
    LOGICAL_INIT = '1'  # init
    LOGICAL_ARMED = '2'  # armed
    LOGICAL_ACTIVE = '3'  # active
    SYNC_TIME = 5  # [sec]
    PHYSICAL_STATE_AGG_TABLE = [{"p1": 'PHYSICAL_DISABLED', "p2": 'PHYSICAL_DISABLED', "exp": 'PHYSICAL_DISABLED'},
                                {"p1": 'PHYSICAL_DISABLED', "p2": 'PHYSICAL_SLEEP', "exp": 'PHYSICAL_DISABLED'},
                                {"p1": 'PHYSICAL_DISABLED', "p2": 'PHYSICAL_POLLING', "exp": 'PHYSICAL_DISABLED'},
                                {"p1": 'PHYSICAL_DISABLED', "p2": 'PHYSICAL_LINKUP', "exp": 'PHYSICAL_DISABLED'},
                                {"p1": 'PHYSICAL_SLEEP', "p2": 'PHYSICAL_SLEEP', "exp": 'PHYSICAL_SLEEP'},
                                {"p1": 'PHYSICAL_SLEEP', "p2": 'PHYSICAL_POLLING', "exp": 'PHYSICAL_SLEEP'},
                                {"p1": 'PHYSICAL_SLEEP', "p2": 'PHYSICAL_LINKUP', "exp": 'PHYSICAL_SLEEP'},
                                {"p1": 'PHYSICAL_POLLING', "p2": 'PHYSICAL_POLLING', "exp": 'PHYSICAL_POLLING'},
                                {"p1": 'PHYSICAL_POLLING', "p2": 'PHYSICAL_LINKUP', "exp": 'PHYSICAL_POLLING'},
                                {"p1": 'PHYSICAL_LINKUP', "p2": 'PHYSICAL_LINKUP', "exp": 'PHYSICAL_LINKUP'}]
    LOGICAL_STATE_AGG_TABLE = [{"p1": 'LOGICAL_DOWN', "p2": 'LOGICAL_DOWN', "exp": 'LOGICAL_DOWN'},
                               {"p1": 'LOGICAL_DOWN', "p2": 'LOGICAL_INIT', "exp": 'LOGICAL_DOWN'},
                               {"p1": 'LOGICAL_DOWN', "p2": 'LOGICAL_ARMED', "exp": 'LOGICAL_DOWN'},
                               {"p1": 'LOGICAL_DOWN', "p2": 'LOGICAL_ACTIVE', "exp": 'LOGICAL_DOWN'},
                               {"p1": 'LOGICAL_INIT', "p2": 'LOGICAL_INIT', "exp": 'LOGICAL_INIT'},
                               {"p1": 'LOGICAL_INIT', "p2": 'LOGICAL_ARMED', "exp": 'LOGICAL_INIT'},
                               {"p1": 'LOGICAL_INIT', "p2": 'LOGICAL_ACTIVE', "exp": 'LOGICAL_INIT'},
                               {"p1": 'LOGICAL_ARMED', "p2": 'LOGICAL_ARMED', "exp": 'LOGICAL_ARMED'},
                               {"p1": 'LOGICAL_ARMED', "p2": 'LOGICAL_ACTIVE', "exp": 'LOGICAL_ARMED'},
                               {"p1": 'LOGICAL_ACTIVE', "p2": 'LOGICAL_ACTIVE', "exp": 'LOGICAL_ACTIVE'}]
    CONFIG_MANAGER_SERVICE = 'configmgrd'
    SERVICE_RECOVERY_MAX_TIME = 60  # [sec] TODO: update to accurate value
    PORT_DOWN_MAX_TIME = 3.0  # [sec]
    PORT_UP_MAX_TIME = 9.0  # [sec]
    PORT_UPDATE_TIME = 60  # [sec]
    DATABASE_TABLES = ['APPL_DB', 'ASIC_DB', 'COUNTERS_DB', 'COUNTERS_DB_1',
                       'COUNTERS_DB_2', 'CONFIG_DB', 'STATE_DB', 'FLEX_COUNTER_DB']
    LOG_MSG_UNSET_FAE_INTERFACE = "PATCH..."  # TODO: complete
    LOG_MSG_SET_FAE_INTERFACE = "PATCH /nvue_v1/interface/"
    LOG_MSG_ACTION_CLEAR_FAE_INTERFACE = 'Clearing counters for interface {port_name} for user admin'


class FastRecoveryConsts:
    STATE = 'state'
    STATE_ENABLED = 'enabled'
    STATE_DISABLED = 'disabled'
    STATE_DEFAULT = 'enabled'
    TRIGGER = 'trigger'
    TRIGGER_EVENT = 'event'
    TRIGGER_CREDIT_WATCHDOG = 'credit-watchdog'
    TRIGGER_EFFECTIVE_BER = 'effective-ber'
    TRIGGER_RAW_BER = 'raw-ber'
    TRIGGER_SYMBOL_BER = 'symbol-ber'
    TRIGGERS = [TRIGGER_CREDIT_WATCHDOG, TRIGGER_EFFECTIVE_BER, TRIGGER_RAW_BER, TRIGGER_SYMBOL_BER]
    SEVERITY_DEFAULT = 'error'
    SEVERITY_WARNING = 'warning'


class LogComponentsConsts:
    NVUE = 'nvue'
    SYMMETRY_MANAGER = 'symmetry-manager'
    GPU_TELEMETRY = 'gpu_telemetry'
    PEER_TELEMETRY = 'peer_telemetry'
    COMPONENTS_LIST = [
        NVUE,
        "orchagent",
        "portsyncd",
        "sai_api_port",
        "sai_api_switch",
        SYMMETRY_MANAGER,
        "syncd",
        GPU_TELEMETRY,
        PEER_TELEMETRY,
    ]
    CRITICAL = "critical"
    DEBUG = "debug"
    ERROR = "error"
    INFO = "info"
    NOTICE = "notice"
    WARN = "warn"
    ALERT = "alert"
    EMERGENCY = "emergency"
    LOG_LEVEL_LIST = [CRITICAL, DEBUG, ERROR, INFO, NOTICE, WARN]
    LOG_LEVEL_DEFAULT = NOTICE
    LEVEL = 'level'
    GPU_TELEMETRY = 'gpu_telemetry'
    NVUE_LOG = 'nvued.log'
    NVUE_CLI_LOG = 'nv-cli.log'
    FILE = 'file'
    SYSLOG = 'syslog'


class UfmMadConsts:
    class State(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'

    STATE = 'state'
    ADVERTISED_ADDRESSED = 'advertised-addresses'
    IPV4 = 'ipv4'
    IPV4_PREF = 'ipv4_0'
    IPV4_NETMASK = 'ipv4_netmask'
    IPV4_NETMASK_PREF = 'netmask_0'
    IPV6 = 'ipv6'
    IPV6_SLAAC = 'ipv6_slaac'
    IPV6_NETMASK = 'ipv6_netmask'
    IPV6_NETMASK_PREF = 'netmask['
    LAST_IP_INDEX = '[3]'
    ZEROS_IPV4 = '0.0.0.0/0'
    ZEROS_IPV6 = '0:0:0:0:0:0:0:0/0'
    STATIC_IPV4 = '10.10.10.10/10'
    STATIC_IPV6 = '10:10:10:10:10:10:10:10/10'
    MGMT_PORT0 = 'eth0'
    MGMT_PORT1 = 'eth1'
    IPV4_PREFIX = 'ipv4-prefix'
    IPV6_PREFIX = 'ipv6-prefix'
    UFM_MAD_TABLE_ETH_TEMPLATE = '\"UFM_MAD_TABLE|{port_name}\"'
    UFM_MAD_TABLE_GENERAL = '\"UFM_MAD_TABLE|general\"'
    NUMBER_OF_ADDRESSES_IN_MAD_RESPONSE = 4
    CONFIG_TIME = 100  # [sec]
    MST_DEV_NAME = '/dev/mst/mt54002_pciconf0'
    IBSNI_REGISTER = 'IBSNI'
    PMAOS_REGISTER = 'PMAOS'
    PREI_REGISTER = 'PREI'
    PAOS_REGISTER = 'PAOS'
    NVMAD_PATH = '/auto/sw_system_project/MLNX_OS_INFRA/mad_repository'
    MAD_NO_IPV4 = '0.0.0.0'
    MAD_NO_IPV6 = '0:0:0:0::0:0'


class DiskConsts:
    DEFAULT_PARTITION_NAME = 'sda'
    PARTITION_CAPACITY_LIMIT = 40  # Percent value
    MINIMUM_FREE_SPACE = 0.0  # Gigs


class BiosConsts:
    BIOS_START_REGEX = "American Megatrends"
    BIOS_PASSWORD_PROMPT = "Enter Password"
    CTRL_B = "\x02"
    ENTER = "\015"
    LEFT_ARROW = "\033[D"
    RIGHT_ARROW = "\033[C"
    DOWN_ARROW = "\033[B"
    UP_ARROW = "\033[A"
    F4 = "\033OS"
    ESC = "\033["
    BIOS_HOMEPAGE_TITLE = "BIOS Information"
    DEFAULT_BIOS_PASSWORD = "admin"
    INVALID_PASSWORD_PROMPT = "Invalid Password"
    CREATE_NEW_PASSWORD = "Create New Password"
    NVLINK_CREATE_NEW_PASSWORD = "Create New Administrator Password"
    ENTER_CURRENT_PASSWORD = "Enter Current Password"
    NVLINK_ENTER_CURRENT_PASSWORD = "Enter Current Administrator Password"
    CLEAR_OLD_PASSWORD = "Clear Old Password"
    NVLINK_CLEAR_OLD_PASSWORD = "Clear Old Administrator Password"
    ENABLED_SELECTED = "[1;37;47m[Enabled]"
    DISABLED_SELECTED = "[1;37;47m[Disabled]"
    KEY_STROKE_SLEEP = 0.5
    SELECTED_PAGE_REGEX = "\\x1b[[]0;34;47m ({}) \\x1b"
    SELECTED_LINE_REGEX = "\\x1b[[1];37;47m([^\\[\\]]*?)\\x1b"
    SELECTED_LINE_VAL_REGEX = "\\x1b[[1];37;47m[[]([^\\\\*?)[]]\\x1b"
    SELECTED_OPTION_LINE_REGEX = "\\x1b[[]1;37;40m([^\\[\\]]*?)\\x1b"
    BIOS_MENU_PAGES = ["Main", "Advanced", "Chipset", "Security", "Boot", "Save & Exit", "Event Logs"]
    NVLINK_BIOS_MENU_PAGES = ["Main", "Advanced", "Chipset", "Security", "Boot", "Save & Exit", "Event Logs", "Server Mgmt"]
    MISSING_PAGE_ERR = "Target page {} not found in the BIOS setting pages list {}"
    MAX_SELECTIONS_PER_PAGE = 30
    PEXPECT_TIMEOUT = 2


class AclConsts:
    ACTION = 'action'
    SET = 'set'
    ACTION_LOG_PREFIX = 'action_log_prefix'
    DSCP_SET_ACTION = 'dscp'
    DENY = 'deny'
    PERMIT = 'permit'
    LOG = 'log'
    LOG_PREFIX = 'log-prefix'
    MATCH = 'match'
    ACTION = 'action'
    IP = 'ip'
    MAC = 'mac'
    TYPE = 'type'
    RULE = 'rule'
    RULE_ID = 'rule_id'
    MATCH_IP = 'match_ip'
    SOURCE_IP = 'source-ip'
    DEST_IP = 'dest-ip'
    TCP = "tcp"
    DEST_PORT = "dest-port"
    TCP_SOURCE_PORT = 'tcp-source-port'
    UDP_SOURCE_PORT = 'udp-source-port'
    TCP_DEST_PORT = 'tcp-dest-port'
    UDP_DEST_PORT = 'udp-dest-port'
    FRAGMENT = 'fragment'
    ECN_FLAGS = 'ecn_flags'
    ECN_IP_ECT = 'ecn_ip-ect'
    IP_ECT = 'ip-ect'
    FLAGS = 'flags'
    TCP_FLAGS = 'tcp_flags'
    MASK = 'mask'
    TCP_MASK = 'tcp_mask'
    TCP_STATE = 'tcp_state'
    PROTOCOL = 'protocol'
    IP_PROTOCOL = 'ip_protocol'
    MAC_PROTOCOL = 'mac_protocol'
    ICMP = 'icmp'
    ICMPV6 = 'icmpv6'
    ECHO_REQUEST = 'echo-request'
    ICMP_TYPE = 'icmp-type'
    ICMPV6_TYPE = 'icmpv6-type'
    STATISTICS = 'statistics'
    INBOUND = 'inbound'
    OUTBOUND = 'outbound'
    CONTROL_PLANE = 'control-plane'
    REMARK = 'remark'
    MATCH_MAC = 'match_mac'
    SOURCE_MAC = 'source-mac'
    SOURCE_MAC_MASK = 'source-mac-mask'
    DEST_MAC = 'dest-mac'
    DEST_MAC_MASK = 'dest-mac-mask'
    MSS = 'mss'
    ALL_MSS_EXCEPT = 'all-mss-except'
    RECENT_LIST = 'recent-list'
    RECENT_LIST_NAME = 'recent_list_name'
    RECENT_LIST_UPDATE = 'update-interval'
    RECENT_LIST_HIT = 'hit-count'
    RECENT_LIST_ACTION = 'recent-list-action'
    HASHLIMIT_NAME = 'hash_name'
    HASHLIMIT_RATE = 'rate-above'
    HASHLIMIT_BURST = 'burst'
    HASHLIMIT_MODE = 'mode'
    HASHLIMIT_EXPIRE = 'expire'
    HASHLIMIT_DEST_MASK = 'destination-mask'
    HASHLIMIT_SRC_MASK = 'source-mask'
    DSCP = 'dscp'
    DEFAULT_ACLS = ["ACL_LOOPBACK_INBOUND_CP_DEFAULT", "ACL_LOOPBACK_INBOUND_CP_DEFAULT_IPV6",
                    "ACL_MGMT_INBOUND_CP_DEFAULT", "ACL_MGMT_INBOUND_CP_DEFAULT_IPV6",
                    "ACL_MGMT_INBOUND_DEFAULT", "ACL_MGMT_INBOUND_DEFAULT_IPV6",
                    "ACL_MGMT_OUTBOUND_CP_DEFAULT", "ACL_MGMT_OUTBOUND_CP_DEFAULT_IPV6"]
    # Old -> new ACL rename / split / merge map applied during ISSU from pre-rename images.
    # Splits (e.g. INBOUND_CP_DEFAULT) are resolved by rule "remark": rules whose remark starts with
    # "Whitelist-" go to acl-default-whitelist[-ipv6], the rest go to acl-default-dos[-ipv6].
    OLD_TO_NEW_ACL_RENAME_MAP = {
        "ACL_LOOPBACK_INBOUND_CP_DEFAULT": ["acl-default-loopback"],
        "ACL_LOOPBACK_INBOUND_CP_DEFAULT_IPV6": ["acl-default-loopback-ipv6"],
        "ACL_MGMT_INBOUND_CP_DEFAULT": ["acl-default-dos", "acl-default-whitelist"],
        "ACL_MGMT_INBOUND_CP_DEFAULT_IPV6": ["acl-default-dos-ipv6", "acl-default-whitelist-ipv6"],
        "ACL_MGMT_INBOUND_DEFAULT": ["acl-default-dos"],
        "ACL_MGMT_INBOUND_DEFAULT_IPV6": ["acl-default-dos-ipv6"],
        "ACL_MGMT_OUTBOUND_CP_DEFAULT": ["acl-default-outbound"],
        "ACL_MGMT_OUTBOUND_CP_DEFAULT_IPV6": ["acl-default-outbound-ipv6"],
    }
    # New default ACL structure after upgrade - replaces old DEFAULT_ACLS
    NEW_DEFAULT_ACLS = [
        'acl-default-loopback',
        'acl-default-loopback-ipv6',
        'acl-default-dos',
        'acl-default-whitelist',
        'acl-default-dos-ipv6',
        'acl-default-whitelist-ipv6',
        'acl-default-outbound',
        'acl-default-outbound-ipv6'
    ]

    # Expected rule counts for new ACLs after migration. Verified by comparing live ``nv show acl``
    # output between an OLD-CLI DUT and a NEW-CLI DUT.
    # The split predicate for ACL_MGMT_INBOUND_CP_DEFAULT[_IPV6] is exactly
    # ``rule.remark.startswith("Whitelist-")``: matching rules go to acl-default-whitelist[-ipv6],
    # everything else (including no-remark + action=permit ICMPv6 rules) goes to acl-default-dos[-ipv6].
    # Each acl-default-whitelist[-ipv6] also has +1 auto-injected terminator rule (action=deny,
    # match={}) appended at the end — not a migrated rule, generated by the new image.
    NEW_ACL_EXPECTED_RULE_COUNTS = {
        'acl-default-loopback': 1,                 # renamed from ACL_LOOPBACK_INBOUND_CP_DEFAULT (1 rule)
        'acl-default-loopback-ipv6': 1,            # renamed from ACL_LOOPBACK_INBOUND_CP_DEFAULT_IPV6 (1 rule)
        'acl-default-dos': 61,                     # 60 non-"Whitelist-" rules from ACL_MGMT_INBOUND_CP_DEFAULT + 1 from ACL_MGMT_INBOUND_DEFAULT
        'acl-default-dos-ipv6': 72,                # 71 non-"Whitelist-" rules from ACL_MGMT_INBOUND_CP_DEFAULT_IPV6 + 1 from ACL_MGMT_INBOUND_DEFAULT_IPV6
        'acl-default-whitelist': 25,               # 24 "Whitelist-*" rules from ACL_MGMT_INBOUND_CP_DEFAULT + 1 auto-injected deny-all terminator
        'acl-default-whitelist-ipv6': 24,          # 23 "Whitelist-*" rules from ACL_MGMT_INBOUND_CP_DEFAULT_IPV6 + 1 auto-injected deny-all terminator
        'acl-default-outbound': 2,                 # renamed from ACL_MGMT_OUTBOUND_CP_DEFAULT (2 rules)
        'acl-default-outbound-ipv6': 2,            # renamed from ACL_MGMT_OUTBOUND_CP_DEFAULT_IPV6 (2 rules)
    }

    # ACL Migration Reference Information:
    # How old default ACLs map to new default ACLs during upgrade. See OLD_TO_NEW_ACL_RENAME_MAP
    # above for the same data as a dict.
    #
    # OLD ACL NAME                          -> NEW ACL NAME              | MIGRATION TYPE
    # ----------------------------------------------------------------------------------
    # ACL_LOOPBACK_INBOUND_CP_DEFAULT       -> acl-default-loopback      | Renamed (interface-bound on lo)
    # ACL_LOOPBACK_INBOUND_CP_DEFAULT_IPV6  -> acl-default-loopback-ipv6 | Renamed (interface-bound on lo)
    # ACL_MGMT_INBOUND_CP_DEFAULT           -> acl-default-dos           | Split: rules whose remark does NOT start with "Whitelist-"
    # ACL_MGMT_INBOUND_CP_DEFAULT           -> acl-default-whitelist     | Split: rules whose remark starts with "Whitelist-"
    # ACL_MGMT_INBOUND_CP_DEFAULT_IPV6      -> acl-default-dos-ipv6      | Split: rules whose remark does NOT start with "Whitelist-"
    # ACL_MGMT_INBOUND_CP_DEFAULT_IPV6      -> acl-default-whitelist-ipv6| Split: rules whose remark starts with "Whitelist-"
    # ACL_MGMT_INBOUND_DEFAULT              -> acl-default-dos           | Merged into DOS ACL
    # ACL_MGMT_INBOUND_DEFAULT_IPV6         -> acl-default-dos-ipv6      | Merged into DOS ACL
    # ACL_MGMT_OUTBOUND_CP_DEFAULT          -> acl-default-outbound      | Renamed
    # ACL_MGMT_OUTBOUND_CP_DEFAULT_IPV6     -> acl-default-outbound-ipv6 | Renamed
    #
    # Migration Details:
    # - Interface-bound ACLs: Bound to specific interfaces (lo) post-rename.
    # - Whitelist ACLs receive a generated +1 deny-all terminator rule (action=deny, match={}) — not migrated.
    # - DOS ACLs do NOT receive any auto-injected rules.
    # - The split predicate is purely the remark prefix; action= is irrelevant for routing.


class PtpConsts:
    class TcState(Enum):
        ENABLED = 'enabled'
        DISABLED = 'disabled'
        INVALID = 'invalid'

    TC_STATE = 'tc'
    MTPCPC_REGISTER = 'MTPCPC'
    MTPCPC_INDEXES = '--indexes "lp_msb=0x0,local_port=0x0"'
    ING_CORRECTION_MSG_TYPE = 'ing_correction_message_type'
    EGR_CORRECTION_MSG_TYPE = 'egr_correction_message_type'
    REG_NA_VALUE = '0xffffffff'
    REG_DISABLE_VALUE = '0x00000000'
    REG_ENABLE_VALUE = '0x0000070f'
    DEFAULT_DICT = {
        ING_CORRECTION_MSG_TYPE: REG_NA_VALUE,
        EGR_CORRECTION_MSG_TYPE: REG_NA_VALUE
    }
    PTP_TABLE_TC = '\"PTP_TABLE|tc\"'


class IssuConsts:
    class IssuStatus(Enum):
        NO_ISSU = 'no-issu'
        IN_PROGRESS = 'in-progress'
        FAILED = 'failed'
        DONE = 'done'

    class IssuBase:
        LAST_GA = "Last_GA"
        LAST_FW = "Last_FW"
        # list of all ISSU base types
        ALL_TYPES = [LAST_GA, LAST_FW]

    ISSU_STATUS = 'issu-status'
    ISSU = 'issu'
    ISSU_SKIP_SM = 'issu skip-sm'
    ISSU_NO_REBOOT_PARAMS = {'reboot': 'no'}
    ISSU_INVALID_FLAG = 'issu invalid-flag'
    DB_REQUEST_ISSU = 'WARM_RESTART_TABLE|request-issu'
    DB_STATUS = 'status'
    OPENSM_RESPONSE_CLEAR = ''
    OPENSM_RESPONSE_NO = 'no'
    OPENSM_RESPONSE_YES = 'yes'
    OPENSM_RESPONSE_REQUESTING = 'requesting'
    OPENSM_RESPONSE_ABORT = 'abort'
    # DB_OPENSM_TIMEOUT = 'TBD'  # Currently not needed (constant 60 secs)
    OLD_IMAGE = "/auto/sw_system_release/nos/nvos/25.02.1930-007/amd64/dev/nvos-amd64-25.02.1930-007.bin"
    PYTHON_PATH = 'PYTHONPATH=/ngts_venv/ /ngts_venv/bin/python'
    PING_SERVER_SCRIPT = '/sonic-mgmt/ngts/tests_nvos/system/ping_server.py'
    CONTAINER_BU_TEMPLATE = '{python_path} {ping_server_script}'
    SERVER_SCRIPT = PYTHON_PATH + PING_SERVER_SCRIPT
    OPENSM_RESPONSE_TIMEOUT = '60'  # [sec]
    TRAFFIC_DURATION = '720'  # [sec]
    TRAFFIC_TIMEOUT = int(TRAFFIC_DURATION) + 10  # [sec]
    CPU_MAX_DOWNTIME = 120  # [sec]
    SERVER_OUTPUT = 'server_output.txt'
    CLIENT_OUTPUT = 'client_output.txt'
    ERROR_CONFIG_MUST_BE_SAVED = ('Error: Action failed with the following issue:\n'
                                  '  Configuration must be saved before performing ISSU')
    ERROR_SYSTEM_MUST_BE_REBOOTED = ('Error: Action failed with the following issue:\n'
                                     '  System must be rebooted during ISSU')
    ERROR_OPENSM_REACH_TIMEOUT = 'No permission to perform ISSU from the SM'
    ERROR_DOWNGRADE_NOT_ALLOWED = ('Error: Action failed with the following issue:\n'
                                   '  ISSU does not support downgrade')
    ERROR_INVALID_PARAM = "Error: Invalid parameter: 'invalid-flag'"
    LOG_MSG_LIST = ['issue: No permission to perform ISSU from the SM',
                    'issue: System must be rebooted during ISSU',
                    'issue: Configuration must be saved before performing ISSU']
    LOG_MSG_DOWNGRADE_NOT_SUPPORTED = 'issue: ISSU does not support downgrade'
    SNMP_READ_ONLY_COMMUNITY = 'qwerty12'
    REDUCED_TIMEOUT = '20'  # [sec]
    # Read timeout for ISSU operations (Netmiko send_command_timing)
    # Typical ISSU takes ~3 minutes, 600 seconds provides ~3x safety margin
    ISSU_READ_TIMEOUT = 600  # [sec] - 10 minutes to handle long ISSU operations


class RemarkableLogsConsts:
    REQUESTED = "requested"
    ERROR = "error"
    STORM = "storm"
    LOGS_CLEAN_TIME = "-logs-clean-time"
    LOGS_NUMBER = "-logs-number"
    LOGS_RATE = "-logs-rate"
    LOGS_TIME_WINDOW = "-logs-time-window"
    BOOTS_HISTORY = "boots-history"
    ERROR_LOGS_CLEAN_TIME = ERROR + LOGS_CLEAN_TIME
    ERROR_LOGS_NUMBER = ERROR + LOGS_NUMBER
    ERROR_LOGS_RATE = ERROR + LOGS_RATE
    ERROR_LOGS_TIME_WINDOW = ERROR + LOGS_TIME_WINDOW
    FIRST_SAVED_BOOT_LOGS = "first-saved-boot-logs"
    REQUESTED_BY_DAEMON_LOGS = "requested-by-daemon-logs"
    STATE = "state"
    STORM_LOGS_CLEAN_TIME = STORM + LOGS_CLEAN_TIME
    STORM_LOGS_NUMBER = STORM + LOGS_NUMBER
    STORM_LOGS_RATE = STORM + LOGS_RATE
    STORM_LOGS_TIME_WINDOW = STORM + LOGS_TIME_WINDOW
    REQUESTED_FILE_NAME = REQUESTED + '_'
    LOGS_PATH = "/var/log/"
    FIRST_BOOT_FOLDER_NAME = "remarkable_logs_first_boot"
    REMARKABLE_LOGS_FOLDER_NAME = "remarkable_logs_"
    FEATURE_EXPECTED_FIELDS = [BOOTS_HISTORY, ERROR_LOGS_CLEAN_TIME, ERROR_LOGS_NUMBER, ERROR_LOGS_RATE,
                               ERROR_LOGS_TIME_WINDOW, FIRST_SAVED_BOOT_LOGS, REQUESTED_BY_DAEMON_LOGS,
                               STATE, STORM_LOGS_CLEAN_TIME, STORM_LOGS_NUMBER,
                               STORM_LOGS_RATE, STORM_LOGS_TIME_WINDOW]


class LogsSources:
    SYSLOG = "syslog"
    NVUED = "nvued.log"
    USER = "user.log"
    AUTH = "auth.log"
    AUDIT = "audit.log"
    INSTALLER = "installer"
    PLATFORM_THERMAL = "platform-thermal"
    IFUPDOWN2 = "ifupdown2"
    NGINX = "nginx"
    DATAPATH = "datapath"
    ROUTING = "routing"
    FRR = "frr"
    LINKSTATE = "linkstate"
    SWITCHD = "switchd.log"

    @classmethod
    def all(cls):
        return [v for k, v in vars(cls).items() if k.isupper()]


class SecureConfig:
    """Secure configuration management for sensitive environment variables"""
    _instance = None
    _config = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._load_config()
        return cls._instance

    @classmethod
    def _load_config(cls):
        """Load configuration from secure source"""
        # Load from secure configuration file or environment
        # This is a placeholder - implement your preferred secure storage method
        config_path = os.path.join(os.path.dirname(__file__), 'secure_config.json')
        if os.path.exists(config_path):
            with open(config_path) as f:
                cls._config = json.load(f)

    @classmethod
    def get(cls, key, default=None):
        """Get a configuration value securely"""
        return cls._config.get(key, default)

    @classmethod
    def set(cls, key, value):
        """Set a configuration value securely"""
        cls._config[key] = value
        # Implement secure storage of the updated configuration
        config_path = os.path.join(os.path.dirname(__file__), 'secure_config.json')
        with open(config_path, 'w') as f:
            json.dump(cls._config, f, indent=4)


class SSDConsts:
    class DiskType(Enum):
        SDA = 'sda'
        NVME = 'nvme'

    SIZE_GB: str = 'size_gb'
    ADVERTISED_SIZE_GB: str = 'advertised_size_gb'
    SSD_PART_NUMBER: str = 'part-number'
    SSD_DUMP_FILENAME: str = 'ssd.dump'

    class SSDType(TypedDict):
        SIZE_GB: float
        ADVERTISED_SIZE_GB: float
        SSD_PART_NUMBER: str

    VIRTIUM_VTPM24CEXI08_BM110006: SSDType = {
        SIZE_GB: 74.5,
        ADVERTISED_SIZE_GB: 80.0,
        SSD_PART_NUMBER: 'Virtium VTPM24CEXI080-BM110006'
    }
    SFSA160GM2AK2TO_I_8C_22K_NVI: SSDType = {
        SIZE_GB: 149.1,
        ADVERTISED_SIZE_GB: 160.0,
        SSD_PART_NUMBER: 'SFSA160GM2AK2TO-I-8C-22K-NVI'
    }
    VTPM24GLXI160_BM11: SSDType = {
        SIZE_GB: 149.1,
        ADVERTISED_SIZE_GB: 160.0,
        SSD_PART_NUMBER: 'VTPM24GLXI160-BM11'
    }
    VTPM24GLXI160_BM12: SSDType = {
        SIZE_GB: 149.1,
        ADVERTISED_SIZE_GB: 160.0,
        SSD_PART_NUMBER: 'VTPM24GLXI160-BM12'
    }
    MD681GEEBC82: SSDType = {
        SIZE_GB: 74.5,  # OS-visible size for an "80 GB" SSD
        ADVERTISED_SIZE_GB: 80.0,
        SSD_PART_NUMBER: 'MD681GEEBC82',
    }
    MD681GEFBC82: SSDType = {
        SIZE_GB: 149.1,
        ADVERTISED_SIZE_GB: 160.0,
        SSD_PART_NUMBER: 'MD681GEFBC82',
    }


class ServiceConsts:
    NTP = 'ntp@mgmt'
    NTP_RESOURCE_LIMIT_CPU = 'cpu'
    NTP_RESOURCE_LIMIT_CPU_DEFAULT_VALUE = 10
    NTP_RESOURCE_LIMIT_MEMORY = 'memory'
    NTP_RESOURCE_LIMIT_MEMORY_DEFAULT_VALUE = "1000G"


class PackageConsts:
    REPO_ID = 'repository'
    REPO_DIST_ID = 'distribution'
    POOL_ID = 'pool'
    INSECURE = 'insecure'
    SOURCE = 'source'
    KEY = 'key'
    USE_VRF = 'use-vrf'
    SCOPE = 'scope'
    VRF = 'vrf'
    TRIES = 'tries'
    REMOTE_URL = 'remote-url'


class PeerPortConsts:
    GPU_TELEMETRY_SERVICE = "gpu-telemetry"
    NMX_T_LITE_SERVICE = "nmx-t-lite"
    PEER_PORT = "peer-port"
    STATE = "state"
    INTERVAL = "interval"
    STATE_ENABLED = "enabled"
    STATE_DISABLED = "disabled"
    INTERVAL_DEFAULT = 5
    INTERVAL_UNDERLIMIT = 0
    INTERVAL_OVERLIMIT = 3601
    INVALID_INTERVAL_ERR_MSG = "Valid range for interval is"
    LOWER_INTERVAL_ERR_MSG = "is less than the minimum of"
    HIGHER_INTERVAL_ERR_MSG = "is greater than the maximum of"
    SET_INVALID_INTERVAL_ERR_MSGS = {ApiType.NVUE: {"under_limit": INVALID_INTERVAL_ERR_MSG,
                                                    "over_limit": INVALID_INTERVAL_ERR_MSG},
                                     ApiType.OPENAPI: {"under_limit": LOWER_INTERVAL_ERR_MSG,
                                                       "over_limit": HIGHER_INTERVAL_ERR_MSG}}
    ASSOCIATED_SWITCH_PORT = "associated-switch-port"


class StoDebug:
    ENABLED = 'enabled'
    DISABLED = 'disabled'
    STATE = 'state'
    STO_EVENT_LOG = 'NVLink STO event'
    CONTAIN_DRAIN_EVENT = 'Contain_and_drain'
    DEBUG_STO_DUMP_FOLDER = '/var/log/mellanox/'
    STO_DEBUG_DUMPS = 'sdk-wrn-dumps'
