import copy
import os
import re
from datetime import datetime
from enum import StrEnum
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active

REGRESSION_TYPE_ENV_VAR = "REGRESSION_TYPE"
CLI_TYPE_CACHE_PATH = "/tmp/CLI_TYPE"


class RegressionType(StrEnum):
    """Regression test run types from REGRESSION_TYPE environment variable.
    This enum is used to categorize different types of regression tests based on their execution environment and purpose.

    Modes: local (unset), MARS (regression), CI (ci_types or relaxed_error_handling_types).
    """
    REGRESSION = "regression"
    SONIC_MAIN = "sonic_main"
    SONIC_PUBLIC = "sonic_public"
    SONIC_DPU_BUILD = "sonic_dpu_build"
    SONIC_CI = "sonic_ci"
    SONIC_DPU_CI = "sonic_dpu_ci"
    SONIC_CI_APP_EXTENSION = "sonic_ci_app_extension"
    SONIC_MAIN_NGCI_TEST_GZ = "sonic_main_ngci_test_gz"
    SONIC_CI_TEST_GZ = "sonic_ci_test_gz"
    NVOS_CI = "nvos_ci"
    SONIC_MGMT_CI = "sonic_mgmt_ci"

    @classmethod
    def ci_types(cls):
        """All CI project types (sonic_ci, sonic_dpu_ci, sonic_ci_app_extension,
        sonic_main_ngci_test_gz, sonic_ci_test_gz, nvos_ci, sonic_mgmt_ci)."""
        return {cls.SONIC_CI, cls.SONIC_DPU_CI, cls.SONIC_CI_APP_EXTENSION,
                cls.SONIC_MAIN_NGCI_TEST_GZ, cls.SONIC_CI_TEST_GZ,
                cls.NVOS_CI, cls.SONIC_MGMT_CI}

    @classmethod
    def relaxed_error_handling_types(cls):
        """CI projects where known issues (matched to bugs) pass the test.

        Unlike regular CI: fails on unknown errors only, passes on known (UPDATE decisions).
        Subset of ci_types: {sonic_ci, sonic_dpu_ci, sonic_ci_app_extension,
        sonic_main_ngci_test_gz, sonic_ci_test_gz}.
        """
        return {cls.SONIC_CI, cls.SONIC_DPU_CI, cls.SONIC_CI_APP_EXTENSION,
                cls.SONIC_MAIN_NGCI_TEST_GZ, cls.SONIC_CI_TEST_GZ}

    @classmethod
    def mars_types(cls):
        """Non-CI regression types (regression, sonic_main, sonic_public, sonic_dpu_build)."""
        return set(cls) - cls.ci_types()

    @classmethod
    def bug_create_types(cls):
        """Projects that create Redmine bugs on unknown log analyzer errors."""
        return {cls.REGRESSION, cls.SONIC_MAIN, cls.SONIC_PUBLIC, cls.SONIC_DPU_BUILD, cls.SONIC_MGMT_CI}


class PytestConst:
    run_config_only_arg = '--run_config_only'
    run_test_only_arg = '--run_test_only'
    run_cleanup_only_arg = '--run_cleanup_only'
    alluredir_arg = '--alluredir'
    disable_loganalyzer = '--disable_loganalyzer'
    disable_export_mars_cases_result = '--disable_exporting_results_to_mars_db'
    CUSTOM_SKIP_IF_DICT = 'custom_skip_if_dict'
    CUSTOM_TEST_SKIP_PLATFORM_TYPE = 'dynamic_tests_skip_platform_type'
    CUSTOM_TEST_SKIP_BRANCH_NAME = 'dynamic_tests_skip_branch_name'
    CUSTOM_TEST_SKIP_IMAGE_TYPE = "dynamic_tests_skip_image_type"
    LA_DYNAMIC_IGNORES_LIST = 'LA_DYNAMIC_IGNORES_LIST'
    GET_DUMP_AT_TEST_FALIURE = "GET_DUMP"
    IS_SANITIZER_IMAGE = 'IS_SANITIZER_IMAGE'
    DEPLOY_TEST_FILE_NAME = 'test_deploy_and_upgrade.py'


class SonicConst:
    PORT_SPLIT_NUM_1 = 1
    PORT_SPLIT_NUM_2 = 2
    PORT_SPLIT_NUM_3 = 3
    PORT_SPLIT_NUM_4 = 4
    PORT_SPLIT_NUM_8 = 8
    UNEVEN_PORT_SPLIT_NUMS = [PORT_SPLIT_NUM_3]
    PORT_LANE_NUM_1 = 1
    PORT_LANE_NUM_2 = 2
    PORT_LANE_NUM_4 = 4
    PORT_LANE_NUM_8 = 8
    FEC_RS_MODE = 'rs'
    FEC_FC_MODE = 'fc'
    FEC_NONE_MODE = 'none'
    FEC_AUTO_MODE = 'auto'
    FEC_MODE_LIST = [FEC_RS_MODE, FEC_FC_MODE, FEC_NONE_MODE]
    DOCKERS_LIST = ['swss', 'syncd', 'bgp', 'teamd', 'pmon', 'lldp', 'dhcp_relay', "gnmi",
                    'radv', 'eventd', 'database', 'snmp']
    DOCKERS_LIST_BF = ['swss', 'syncd', 'bgp', 'pmon', 'lldp', 'gnmi', 'eventd', 'database']
    DOCKERS_LIST_TOR = DOCKERS_LIST
    DOCKERS_LIST_LEAF = ['swss', 'syncd', 'bgp', 'teamd', 'pmon', 'lldp', 'radv', 'eventd', 'database', 'snmp', 'gnmi']

    DOCKERS_FANOUT = ['swss', 'gnmi', 'syncd', 'pmon', 'eventd', 'database', 'snmp']

    DAEMONS_DICT = {'swss': [], 'syncd': [], 'bgp': [],
                    'teamd': [], 'pmon': [], 'lldp': [], 'dhcp_relay': []}
    DAEMONS_DICT_BF = {'swss': [],
                       'syncd': [],
                       'bgp': [],
                       'pmon': ['pcied', 'rsyslogd', 'supervisor-proc-exit-listener', 'syseepromd', 'thermalctld',
                                'xcvrd'],
                       'lldp': []}

    # ASIC configuration
    DEFAULT_ASIC_COUNT = 1  # Default for single-ASIC platforms
    DEFAULT_TESTED_ASIC_INDEX = 0

    # Multi-ASIC Platform Configuration
    class MultiAsic:
        """Multi-ASIC platform specific constants"""

        # ASIC configuration path
        ASIC_CONF_PATH = "/usr/share/sonic/device/{PLATFORM}/asic.conf"

        # Retry configuration for multi-ASIC operations
        RETRY_COUNT = 30
        RETRY_DELAY = 10

        # Docker containers classification for multi-ASIC platforms
        # Global containers: run once per system
        # Per-ASIC containers: run once per ASIC namespace
        DOCKERS_GLOBAL = ['pmon', 'telemetry', 'snmp', 'radv', 'lldp', 'eventd', 'gnmi', 'database']

        DOCKERS_PER_ASIC = ['swss', 'syncd', 'bgp', 'teamd', 'database', 'lldp']  # ASIC-specific containers

    SONIC_CONFIG_FOLDER = '/etc/sonic/'
    PORT_CONFIG_INI = 'port_config.ini'
    CONFIG_DB_JSON = 'config_db.json'
    EXTENDED_CONFIG_DB_PATH = "extended_config_db.json"
    CONFIG_DB_JSON_PATH = SONIC_CONFIG_FOLDER + CONFIG_DB_JSON
    PLATFORM_FOLDER_PATH = "/usr/share/sonic/device/{PLATFORM}"
    PLATFORM_JSON_PATH = "/usr/share/sonic/device/{PLATFORM}/platform.json"
    PMON_DAEMON_CONTROL_JSON_PATH = "/usr/share/sonic/device/{PLATFORM}/pmon_daemon_control.json"
    SAI_PROFILE_FILE_PATH = "/usr/share/sonic/device/{PLATFORM}/{HWSKU}/sai.profile"
    COPP_CONFIG = 'copp_cfg.json'
    TIMEZONE = 'Asia/Jerusalem'
    BASIC_SESSION_INFO_FILE_NAME = "basic_session_info.json"

    # i.e, 2x400G[200G](4)+1x800G[400G](4) or 2x25G(2)+1x50G(2)
    BREAKOUT_MODE_WITH_DIFF_LANE_SUPPORTED_SPEEDS_REGEX = r"\dx\d+G(?:\[[\d+G,]+\])?\(\d+\)\+\dx\d+G(?:\[[\d+G,]+\])?\(\d+\)"

    # 1x100G[50G,25G,1G]
    BREAKOUT_MODE_WITH_ADDITIONAL_SUPPORTED_SPEEDS_REGEX = r"\dx\d+G\[[\d+G,]+\]|\dx\d+\[[\d,]+\]"
    BREAKOUT_MODE_WITHOUT_ADDITIONAL_SUPPORTED_SPEEDS_REGEX = r"\dx\d+G"  # 2x50G

    BREAKOUT_MODES_REGEX = "{}|{}|{}".format(BREAKOUT_MODE_WITH_DIFF_LANE_SUPPORTED_SPEEDS_REGEX,
                                             BREAKOUT_MODE_WITH_ADDITIONAL_SUPPORTED_SPEEDS_REGEX,
                                             BREAKOUT_MODE_WITHOUT_ADDITIONAL_SUPPORTED_SPEEDS_REGEX)

    MINIGRAPH_XML = 'minigraph.xml'
    MINIGRAPH_XML_PATH = SONIC_CONFIG_FOLDER + MINIGRAPH_XML
    SANITIZER_FOLDER_PATH = "/var/log/asan"
    CONFIG = '/usr/local/bin/config'
    SHOW = '/usr/local/bin/show'

    # Mellanox: command to get ASIC device (PCI) path on DUT
    ASIC_DETECT_GET_DEVICE_PATH_CMD = '/usr/bin/asic_detect/asic_detect.sh -p'

    TELEMETRY_PATH = "/etc/sonic/telemetry"
    TELEMETRY_SERVER_KEY = "/etc/sonic/telemetry/streamingtelemetryserver.key"
    TELEMETRY_SERVER_CER = "/etc/sonic/telemetry/streamingtelemetryserver.cer"
    TELEMETRY_DSMSROOT_KEY = "/etc/sonic/telemetry/dsmsroot.key"
    TELEMETRY_DSMSROOT_CER = "/etc/sonic/telemetry/dsmsroot.cer"

    RESOLV_CONF_NAME = 'resolv.conf'
    RESOLV_CONF_PATH = '/etc/' + RESOLV_CONF_NAME

    NVIDIA_LAB_DNS_FIRST = '10.211.0.163'
    NVIDIA_LAB_DNS_SECOND = '10.211.0.164'
    NVIDIA_LAB_DNS_THIRD = '10.222.36.70'
    NVIDIA_LAB_DNS_SEARCH = 'mtr.labs.mlnx labs.mlnx mlnx lab.mtl.com mtl.com'
    NVIDIA_AIR_DNS_FIRST = '8.8.8.8'
    NVIDIA_AIR_DNS_SECOND = '192.168.200.1'
    NTP_SERVERS = ['10.80.74.100', '10.245.1.123', '10.211.0.124']
    MIN_SHAPER_RATE_BPS = 25000000
    MAX_SHAPER_RATE_BPS = 0

    CONFIG_RELOAD_CMD = 'config reload -y'
    SONIC_CANONICAL_NOGA_GROUP = "SONiC_Canonical"
    SONIC_COMMUNITY_NOGA_GROUP = "SONiC_Community"
    SONIC_DPU_NOGA_GROUP = "SONiC_DPU"
    SONIC_NOGA_GROUPS = [SONIC_CANONICAL_NOGA_GROUP, SONIC_COMMUNITY_NOGA_GROUP, SONIC_DPU_NOGA_GROUP]
    T1_SKU_UPSTREAM_PORTS_SPEED = {
        "Mellanox-SN5600-C224O8": "400000",
        "Mellanox-SN5640-C448O16": "400000",
        "Mellanox-SN6600_LD-V448P16S2": "800000"
    }
    HWSKU_DOWNSTREAM_PORTS_SPEED = {
        "Mellanox-SN5600-C256S1": 100,
        "Mellanox-SN5600-C224O8": 100,
        "Mellanox-SN5640-C512S2": 100,
        "Mellanox-SN5640-C448O16": 100,
        "Mellanox-SN6600_LD-V512C2": 200,
        "Mellanox-SN6600_LD-V448P16S2": 200
    }
    NO_DYNAMIC_BUFFER_HWSKU_PREFIXES = ('MELLANOX-SN5640', 'MELLANOX-SN6600_LD')


class CliType:
    NVUE = 'NVUE'
    SONIC = 'Sonic'
    SHELL = 'SHELL'
    DVS = 'DVS'
    CUMULUS = 'CUMULUS'
    MLNX_OS = 'MLNX_OS'
    SKYNET = 'skynet'
    SONIC_SWITCH_TYPE = 'MLNX_SWITCH'
    CUMULUS_SWITCH_TYPE = "CUMULUS_SWITCH"
    DVS_SWITCH_TYPE = "DVS_SWITCH"
    NOS_TO_TYPE_DICT = {SONIC: SONIC_SWITCH_TYPE, NVUE: CUMULUS_SWITCH_TYPE, DVS: DVS_SWITCH_TYPE}


class FanoutConfigFile:
    SONIC = 'config_db.json'
    SONIC_CONFIG_PATH = '/etc/sonic/'
    ONYX = 'initial'
    ONYX_CONFIG_PATH = '/config/db/'
    TEMP_PATH = '/tmp/'


class DbConstants:
    METADATA_PATH = "/.autodirect/sw_regression/system/SONIC/MARS/metadata/"
    METADATA_PATH_NVOS = "/.autodirect/sw_regression/system/NVOS/MARS/metadata/"
    METADATA_DVS_PATH = "/auto/sw_regression/system/SDK_SWITCH/MARS/metadata/"

    CLI_TYPE_PATH_MAPPING = {CliType.SONIC: METADATA_PATH,
                             CliType.NVUE: METADATA_PATH_NVOS,
                             CliType.SHELL: METADATA_PATH,
                             CliType.MLNX_OS: METADATA_PATH_NVOS,
                             CliType.SKYNET: METADATA_PATH,
                             CliType.DVS: METADATA_DVS_PATH}
    CREDENTIALS = {CliType.SONIC: {'server': 'm-il-misql-01-prd.public.fbd32f5072b8.database.windows.net,3342', 'database': 'sonic_mars',
                                   'username': os.getenv("SONIC_SERVER_USER"),
                                   'password': os.getenv("SONIC_SERVER_PASSWORD")},
                   CliType.NVUE: {'server': 'm-il-misql-01-prd.public.fbd32f5072b8.database.windows.net,3342', 'database': "NVOS",
                                  'username': os.getenv("NVUE_SERVER_USER"),
                                  'password': os.getenv("NVUE_SERVER_PASSWORD")},
                   CliType.SKYNET: {'server': 'm-il-misql-01-prd.public.fbd32f5072b8.database.windows.net,3342', 'database': 'skynet',
                                    'username': os.getenv("SKYNET_SERVER_USER"),
                                    'password': os.getenv("SKYNET_SERVER_PASSWORD")}}

    MARS_RESPOND_TABLE = 'mars_respond'
    MARS_RESPOND_CI_TABLE = 'mars_respond_ci'


class InfraConst:
    DOCKER_EXEC_BASH_CMD = "docker exec -it {DOCKER} bash -c"
    CMD_GET_SDK_VERSION = 'sx_sdk --version'
    CMD_GET_SDK_VERSION_FROM_DOCKER = \
        "{DOCKER_EXEC_BASH_CMD} '{CMD_GET_SDK_VERSION}'".format(DOCKER_EXEC_BASH_CMD=DOCKER_EXEC_BASH_CMD,
                                                                CMD_GET_SDK_VERSION=CMD_GET_SDK_VERSION)
    SYNCD_DOCKER = 'syncd'
    SYNCD_IBV_DOCKER = 'syncd-ibv{:0>2}'
    NVIDIA_MAIL_SERVER = 'mail.nvidia.com'
    HTTP_SERVER = 'http://nbu-nfs.gtm'
    HTTTP_SERVER_FIT16 = 'http://r-fit16-clone.mtr.labs.mlnx'
    MARS_TOPO_FOLDER_PATH = '/auto/sw_regression/system/SONIC/MARS/conf/topo/'
    MARS_SETUPS_FOLDER_PATH = '/auto/sw_regression/system/SONIC/MARS/conf/setups/'
    MARS_CMIS_FOLDER_PATH = '/auto/sw_regression/system/SONIC/MARS/conf/cmis/'
    NVOS_REGRESSION_SHARED_RESULTS_DIR = '/auto/sw_regression/system/NVOS/MARS/results'
    REGRESSION_SHARED_RESULTS_DIR = '/auto/sw_regression/system/SONIC/MARS/results'
    RELEASE_RESULTS_DIR = '/auto/sw_regression/system/SONIC/release_results'
    HTTP_SERVER_MARS_TOPO_FOLDER_PATH = '{}{}'.format(HTTP_SERVER, MARS_TOPO_FOLDER_PATH)
    MYSQL_SERVER = '10.208.1.11'
    MYSQL_USER = 'sonic'
    MYSQL_PASSWORD = 'sonic11'
    MYSQL_DB = 'sonic'
    RC_SUCCESS = 0
    NGTS_PATH_PYTEST = '/ngts_venv/bin/pytest'
    SLEEP_BEFORE_RRBOOT = 5
    SLEEP_AFTER_RRBOOT = 35
    SLEEP_AFTER_WJH_INSTALLATION = 100
    IP = 'ip'
    IPV4 = "IPv4"
    IPV6 = "IPv6"
    MASK = 'mask'

    ALLURE_SERVER_IP = 'allure.nvidia.com'
    ALLURE_SERVER_PORT = ''
    ALLURE_SERVER_URL = 'https://{}'.format(ALLURE_SERVER_IP)
    ALLURE_REPORT_DIR = '/tmp/allure-results'
    CASE_INCLUDE_EMPTY_ALLURE_STEPS_TMPL = '/tmp/case_include_empty_allure_steps_{}.txt'
    ENV_SESSION_ID = 'SESSION_ID'
    ENV_LOG_FOLDER = 'LOG_FOLDER'
    CASES_DUMPS_DIR = 'cases_dumps'
    CASES_SYSLOG_DIR = 'cases_syslog'
    STM_IP = "10.209.104.53"
    SELECTED_DPUS = 'selected_dpus'

    LOOPBOX_LABEL = 'loopbox'


class LinuxConsts:
    CONF_FEC = "Configured FEC encodings"
    ACTIVE_FEC = "Active FEC encoding"
    FEC_AUTO_MODE = 'auto'
    error_exit_code = 1
    linux = 'linux'
    JERUSALEM_TIMEZONE = 'Asia/Jerusalem'
    ETC_UTC_TIMEZONE = 'Etc/UTC'


class TopologyConsts:
    engine = 'engine'
    players = 'players'
    ports = 'ports'
    interconnects = 'interconnects'
    ports_names = 'ports_names'


class ConfigDbJsonConst:
    PORT = 'PORT'
    ALIAS = 'alias'
    FEATURE = 'FEATURE'
    AUTO_TECHSUPPORT_FEATURE = 'AUTO_TECHSUPPORT_FEATURE'
    TELEMETRY = 'telemetry'
    GNMI = 'gnmi'
    LLDP = 'lldp'
    STATUS = 'status'
    STATE = 'state'
    SPEED = 'speed'
    ENABLED = 'enabled'
    DEVICE_METADATA = "DEVICE_METADATA"
    MGMT_INTERFACE = "MGMT_INTERFACE"
    INTERFACE = "INTERFACE"
    MGMT_INTERFACE_VALUE = '''{"eth0|%s/%s": {"gwaddr": "%s"}}'''
    MGMT_PORT = "MGMT_PORT"
    MGMT_PORT_VALUE = '''{"eth0":{"admin_status": "up", "alias": "eth0"}}'''
    LOCALHOST = "localhost"
    TYPE = 'type'
    TOR_ROUTER = 'ToRRouter'
    LEAF_ROUTER = 'LeafRouter'
    SONIC_HOST = 'SonicHost'
    HOSTNAME = "hostname"
    MAC = "mac"
    HWSKU = "hwsku"
    ADMIN_STATUS = "admin_status"
    DOCKER_ROUTING_CONFIG_MODE = "docker_routing_config_mode"
    SPLIT = "split"


class IpIfaceAddrConst:
    IPV4_ADDR_MASK_KEY = 'IPv4 address/mask'
    IPV6_ADDR_MASK_KEY = 'IPv6 address/mask'
    IPV4_MASK_24 = '24'


class AutonegCommandConstants:
    INTERFACE = "Interface"
    AUTONEG_MODE = "Auto-Neg Mode"
    SPEED = "Speed"
    ADV_SPEED = "Adv Speeds"
    TYPE = "Type"
    ADV_TYPES = "Adv Types"
    OPER = "Oper"
    ADMIN = "Admin"
    FEC = "FEC"
    FEC_OPER = "FEC Oper"
    FEC_ADMIN = "FEC Admin"
    WIDTH = "Width"
    CABLE_SPEED = "Supported Cable Speed"
    PART_NUMBER = "Vendor Part Number"
    REGEX_PARSE_EXPRESSION_FOR_MLXLINK = {
        ADMIN: (r"State\s*:\s*(\w*)", "Active", "up", "down", None),
        OPER: (r"Physical state\s*:\s*(.*)", "LinkUp|ENABLE", "up", "down", None),
        SPEED: (r"Speed\s*:\s*(?:BaseT|BaseTx)?(\d*(?:M|Mbps|G|GbE|Gbps))", None, None, None, None),
        WIDTH: (r"Width\s*:\s*(\d+)x", None, None, None, None),
        FEC: (r"FEC\s*:\s*(.*)", "No FEC", "none", None, None),
        AUTONEG_MODE: (r"Auto Negotiation\s*:\s*(\w*\s*-*\s*\d*\w_*\d*X*|ON)",
                       r"FORCE\s+-\s+\d+\w_*\d*X*|ON", "enabled", "disabled", "Force"),
        CABLE_SPEED: (r"Supported Cable Speed (?:\(Ext.\))?\s+:\s+(?:0x[0-9a-z]+\s+\(([\w.,]+)\)|N/A)",
                      None, None, None, None),
        PART_NUMBER: (r"Vendor Part Number\s*:\s*(\S+)", None, None, None, None)
    }
    PAM4_MIN_LANE_SPEED_MB = 50000


class DefaultCredentialConstants:
    OTHER_SONIC_USER = "admin"
    OTHER_SONIC_PASSWORD_LIST = ["password"]


class CometConsts:
    COMET_FILE_PATH = '/auto/sw/tools/comet/sonic/ver'
    FULL_COMMANDS_PATH = '/auto/sw/tools/comet/sonic/full_command_lists/'


class PlatformTypesConstants:
    FILTERED_PLATFORM_PANTHER = 'MSN2700'
    FILTERED_PLATFORM_ANACONDA = "MSN3700"
    FILTERED_PLATFORM_ANACONDA_C = "MSN3700C"
    FILTERED_PLATFORM_LIONFISH = "MSN3420"
    FILTERED_PLATFORM_BOBCAT = "SN4280"
    FILTERED_PLATFORM_LEOPARD = "MSN4700"
    FILTERED_PLATFORM_LEOPARD_DC = "SN4700D"
    FILTERED_PLATFORM_TIGON = "MSN4600C"

    FILTERED_PLATFORM_MOOSE = "SN5600"
    FILTERED_PLATFORM_BISON = "SN5640"
    FILTERED_PLATFORM_GAUR = "SN5610N"
    FILTERED_PLATFORM_SALAMANDRA = "SN6600"

    PLATFORM_ANACONDA = 'x86_64-mlnx_msn3700-r0'
    PLATFORM_ANACONDA_C = 'x86_64-mlnx_msn3700c-r0'
    PLATFORM_LEOPARD = 'x86_64-mlnx_msn4700-r0'
    PLATFORM_LEOPARD_DC = 'x86_64-nvidia_sn4700d-r0'
    PLATFORM_LIONFISH = 'x86_64-mlnx_msn3420-r0'
    PLATFORM_PANTHER = 'x86_64-mlnx_msn2700-r0'
    PLATFORM_PANTHER_A1 = 'x86_64-mlnx_msn2700a1-r0'
    PLATFORM_TIGON = 'x86_64-mlnx_msn4600c-r0'
    PLATFORM_MOOSE = 'x86_64-nvidia_sn5600-r0'
    PLATFORM_MOOSE_SIMX = 'x86_64-nvidia_sn5600_simx-r0'
    PLATFORM_BISON = 'x86_64-nvidia_sn5640-r0'
    PLATFORM_BISON_SIMX = 'x86_64-nvidia_sn5640_simx-r0'
    PLATFORM_GAUR = 'x86_64-nvidia_sn5610n-r0'
    PLATFORM_SALAMANDRA = 'x86_64-nvidia_sn6600-r0'
    PLATFORM_SALAMANDRA_SIMX = 'x86_64-nvidia_sn6600_simx-r0'
    PLATFORM_CHAMELEON_SINGLE_SIMX = 'x86_64-nvidia_sn5810_ld_simx-r0'
    PLATFORM_SALAMANDRA_LD = 'x86_64-nvidia_sn6600_ld-r0'
    PLATFORM_SALAMANDRA_LD_SIMX = 'x86_64-nvidia_sn6600_ld_simx-r0'
    PLATFORM_CHAMELEON_SINGLE_LD = 'x86_64-nvidia_sn6810_ld-r0'

    BISON_PLATFORMS = [PLATFORM_BISON, PLATFORM_BISON_SIMX]
    SALAMANDRA_PLATFORMS = [PLATFORM_SALAMANDRA, PLATFORM_SALAMANDRA_SIMX]
    SALAMANDRA_LD_PLATFORMS = [PLATFORM_SALAMANDRA_LD, PLATFORM_SALAMANDRA_LD_SIMX]
    LOGS_ON_TMPFS_PLATFORMS = [PLATFORM_PANTHER]


class InterfacesTypeConstants:
    RJ45 = 'RJ45'

    INTERFACE_TYPE_SUPPORTED_SPEEDS_SPC = {
        PlatformTypesConstants.FILTERED_PLATFORM_PANTHER: {
            "PANTHER_AOC": {
                SonicConst.PORT_LANE_NUM_1: {'SR': ['1G', '10G', '25G']},
                SonicConst.PORT_LANE_NUM_2: {'SR2': ['50G']},
                SonicConst.PORT_LANE_NUM_4: {'SR4': ['40G', '100G']}}
        },
        'default': {
            SonicConst.PORT_LANE_NUM_1: {'CR': ['1G', '10G', '25G']},
            SonicConst.PORT_LANE_NUM_2: {'CR2': ['50G']},
            SonicConst.PORT_LANE_NUM_4: {'CR4': ['40G', '100G']},
        }
    }
    INTERFACE_TYPE_SUPPORTED_SPEEDS_SPC2 = {
        PlatformTypesConstants.FILTERED_PLATFORM_LIONFISH: {
            SonicConst.PORT_LANE_NUM_1: {'CR': ['1G', '10G', '25G']},
            SonicConst.PORT_LANE_NUM_2: {'CR2': ['50G']},
            SonicConst.PORT_LANE_NUM_4: {'CR4': ['40G', '100G']}
        },
        PlatformTypesConstants.FILTERED_PLATFORM_ANACONDA_C: {
            SonicConst.PORT_LANE_NUM_1: {'CR': ['1G', '10G', '25G']},
            SonicConst.PORT_LANE_NUM_2: {'CR2': ['50G']},
            SonicConst.PORT_LANE_NUM_4: {'CR4': ['40G', '100G']}
        },
        PlatformTypesConstants.FILTERED_PLATFORM_ANACONDA: {
            SonicConst.PORT_LANE_NUM_1: {'CR': ['1G', '10G', '25G', '50G']},
            SonicConst.PORT_LANE_NUM_2: {'CR2': ['50G', '100G']},
            SonicConst.PORT_LANE_NUM_4: {'CR4': ['40G', '100G', '200G']}
        }
    }
    INTERFACE_TYPE_SUPPORTED_SPEEDS_SPC3 = {
        PlatformTypesConstants.FILTERED_PLATFORM_BOBCAT: {
            SonicConst.PORT_LANE_NUM_1: {'CR': ['1G', '10G', '25G', '50G']},
            SonicConst.PORT_LANE_NUM_2: {'CR2': ['50G', '100G']},
            SonicConst.PORT_LANE_NUM_4: {'CR4': ['40G', '100G', '200G', '400G']}
        },
        PlatformTypesConstants.FILTERED_PLATFORM_LEOPARD: {
            SonicConst.PORT_LANE_NUM_1: {'CR': ['1G', '10G', '25G', '50G']},
            SonicConst.PORT_LANE_NUM_2: {'CR2': ['50G', '100G']},
            SonicConst.PORT_LANE_NUM_4: {'CR4': ['40G', '100G', '200G', '400G']}
        },
        PlatformTypesConstants.FILTERED_PLATFORM_LEOPARD_DC: {
            SonicConst.PORT_LANE_NUM_1: {'CR': ['1G', '10G', '25G', '50G']},
            SonicConst.PORT_LANE_NUM_2: {'CR2': ['50G', '100G']},
            SonicConst.PORT_LANE_NUM_4: {'CR4': ['40G', '100G', '200G', '400G']}
        },
        PlatformTypesConstants.FILTERED_PLATFORM_TIGON: {
            SonicConst.PORT_LANE_NUM_1: {'CR': ['1G', '10G', '25G']},
            SonicConst.PORT_LANE_NUM_2: {'CR2': ['50G']},
            SonicConst.PORT_LANE_NUM_4: {'CR4': ['40G', '100G']}
        }

    }


class FecConstants:
    FEC_MODES_SPEED_SUPPORT_NOT_AVAILABLE = {}
    FEC_MODES_SPC_SPEED_SUPPORT = {
        SonicConst.FEC_FC_MODE: {
            SonicConst.PORT_SPLIT_NUM_1: {'10G': ['CR'],
                                          '25G': ['CR'],
                                          '40G': ['CR4'],
                                          '50G': ['CR2']
                                          },
            SonicConst.PORT_SPLIT_NUM_2: {'10G': ['CR'],
                                          '25G': ['CR'],
                                          '50G': ['CR2']
                                          },
            SonicConst.PORT_SPLIT_NUM_4: {'10G': ['CR'],
                                          '25G': ['CR'],
                                          '50G': ['CR2']
                                          }
        },
        SonicConst.FEC_RS_MODE: {
            SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                          '50G': ['CR2'],
                                          '100G': ['CR4']
                                          },
            SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                          '50G': ['CR2'],
                                          '100G': ['CR4']
                                          },
            SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                          '50G': ['CR2'],
                                          '100G': ['CR4']
                                          },
        },
        SonicConst.FEC_NONE_MODE: {
            SonicConst.PORT_SPLIT_NUM_1: {'1000M': ['CR'],
                                          '10G': ['CR'],
                                          '25G': ['CR'],
                                          '40G': ['CR4'],
                                          '50G': ['CR2'],
                                          '100G': ['CR4']
                                          },
            SonicConst.PORT_SPLIT_NUM_2: {'1000M': ['CR'],
                                          '10G': ['CR'],
                                          '25G': ['CR'],
                                          '50G': ['CR2'],
                                          '100G': ['CR4']
                                          },
            SonicConst.PORT_SPLIT_NUM_4: {'1000M': ['CR'],
                                          '10G': ['CR'],
                                          '25G': ['CR'],
                                          '50G': ['CR2'],
                                          '100G': ['CR4']
                                          }
        }
    }
    FEC_MODES_SPC2_SPEED_SUPPORT = {
        PlatformTypesConstants.FILTERED_PLATFORM_LIONFISH: {
            SonicConst.FEC_FC_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR']
                                              }
            },
            SonicConst.FEC_RS_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                              '50G': ['CR']
                                              }
            },
            SonicConst.FEC_NONE_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR']
                                              }
            }
        },
        PlatformTypesConstants.FILTERED_PLATFORM_ANACONDA_C: {
            SonicConst.FEC_FC_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                              },
            },
            SonicConst.FEC_RS_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4'],
                                              '200G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                              '50G': ['CR'],
                                              },
            },
            SonicConst.FEC_NONE_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR']
                                              }
            }
        },
        SonicConst.FEC_FC_MODE: {
            SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                          '50G': ['CR2']
                                          },
            SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                          '50G': ['CR2']
                                          },
            SonicConst.PORT_SPLIT_NUM_4: {
                '25G': ['CR'],
                '50G': ['CR2']
            }
        },
        SonicConst.FEC_RS_MODE: {
            SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                          '50G': ['CR2'],
                                          '100G': ['CR4']
                                          },
            SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                          '50G': ['CR2'],
                                          },
            SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                          '50G': ['CR'],
                                          },
        },
        SonicConst.FEC_NONE_MODE: {
            SonicConst.PORT_SPLIT_NUM_1: {'10G': ['CR'],
                                          '25G': ['CR'],
                                          '40G': ['CR4'],
                                          '50G': ['CR2'],
                                          '100G': ['CR4']
                                          },
            SonicConst.PORT_SPLIT_NUM_2: {'10G': ['CR'],
                                          '25G': ['CR'],
                                          '50G': ['CR2']
                                          },
            SonicConst.PORT_SPLIT_NUM_4: {'1G': ['CR'],
                                          '10G': ['CR'],
                                          '25G': ['CR'],
                                          '40G': ['CR']
                                          }
        }
    }
    FEC_MODES_SPC2_SPEED_SUPPORT[PlatformTypesConstants.FILTERED_PLATFORM_ANACONDA] = copy.deepcopy(FEC_MODES_SPC2_SPEED_SUPPORT[PlatformTypesConstants.FILTERED_PLATFORM_ANACONDA_C])
    FEC_MODES_SPC3_SPEED_SUPPORT = {
        PlatformTypesConstants.FILTERED_PLATFORM_LEOPARD: {
            SonicConst.FEC_FC_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              }
            },
            SonicConst.FEC_RS_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR'],
                                              '100G': ['CR2'],
                                              '200G': ['CR4'],
                                              '400G': ['CR8']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR'],
                                              '100G': ['CR2'],
                                              '200G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                              '50G': ['CR'],
                                              '100G': ['CR2']
                                              },
            },
            SonicConst.FEC_NONE_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'10G': ['CR'],
                                              '25G': ['CR'],
                                              '50G': ['CR2']
                                              }
            }
        },
        PlatformTypesConstants.FILTERED_PLATFORM_LEOPARD_DC: {
            SonicConst.FEC_FC_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              }
            },
            SonicConst.FEC_RS_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR'],
                                              '100G': ['CR2'],
                                              '200G': ['CR4'],
                                              '400G': ['CR8']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR'],
                                              '100G': ['CR2'],
                                              '200G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                              '50G': ['CR'],
                                              '100G': ['CR2']
                                              },
            },
            SonicConst.FEC_NONE_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'10G': ['CR'],
                                              '25G': ['CR'],
                                              '50G': ['CR2']
                                              }
            }
        },
        PlatformTypesConstants.FILTERED_PLATFORM_BOBCAT: {
            SonicConst.FEC_FC_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              }
            },
            SonicConst.FEC_RS_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR'],
                                              '100G': ['CR2'],
                                              '200G': ['CR4'],
                                              '400G': ['CR8']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR'],
                                              '100G': ['CR2'],
                                              '200G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                              '50G': ['CR'],
                                              '100G': ['CR2']
                                              },
            },
            SonicConst.FEC_NONE_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'10G': ['CR'],
                                              '25G': ['CR'],
                                              '50G': ['CR2']
                                              }
            }
        },
        PlatformTypesConstants.FILTERED_PLATFORM_TIGON: {
            SonicConst.FEC_FC_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR']
                                              }
            },
            SonicConst.FEC_RS_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4'],
                                              '200G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                              '50G': ['CR']
                                              }
            },
            SonicConst.FEC_NONE_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR']
                                              }
            }
        }
    }

    FEC_MODES_SPC4_SPEED_SUPPORT = {
        PlatformTypesConstants.FILTERED_PLATFORM_MOOSE: {
            SonicConst.FEC_FC_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {
                    '25G': ['CR'],
                    '50G': ['CR2']
                },
                SonicConst.PORT_SPLIT_NUM_2: {
                    '25G': ['CR'],
                    '50G': ['CR2']
                },
                SonicConst.PORT_SPLIT_NUM_4: {
                    '25G': ['CR'],
                    '50G': ['CR2']
                },
                SonicConst.PORT_SPLIT_NUM_8: {
                    '10G': ['CR'],
                    '25G': ['CR']
                }
            },
            SonicConst.FEC_RS_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR'],
                                              '200G': ['CR4'],
                                              '400G': ['CR4'],
                                              '800G': ['CR8'],
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR'],
                                              '200G': ['CR4'],
                                              '400G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR'],
                                              '200G': ['CR2'],
                                              },
                SonicConst.PORT_SPLIT_NUM_8: {'25G': ['CR'],
                                              '50G': ['CR'],
                                              '100G': ['CR']
                                              }
            },
            SonicConst.FEC_NONE_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_8: {'10G': ['CR'],
                                              '25G': ['CR']
                                              }
            }
        },
        PlatformTypesConstants.FILTERED_PLATFORM_GAUR: {
            SonicConst.FEC_FC_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {
                    '25G': ['CR'],
                    '50G': ['CR2']
                },
                SonicConst.PORT_SPLIT_NUM_2: {
                    '25G': ['CR'],
                    '50G': ['CR2']
                },
                SonicConst.PORT_SPLIT_NUM_4: {
                    '25G': ['CR'],
                    '50G': ['CR2']
                },
                SonicConst.PORT_SPLIT_NUM_8: {
                    '10G': ['CR'],
                    '25G': ['CR']
                }
            },
            SonicConst.FEC_RS_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR2'],
                                              '200G': ['CR4'],
                                              '400G': ['CR8'],
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR2'],
                                              '200G': ['CR4'],
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR2'],
                                              },
                SonicConst.PORT_SPLIT_NUM_8: {'25G': ['CR'],
                                              '50G': ['CR'],
                                              }
            },
            SonicConst.FEC_NONE_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_8: {'10G': ['CR'],
                                              '25G': ['CR']
                                              }
            }
        },
        PlatformTypesConstants.FILTERED_PLATFORM_BISON: {
            SonicConst.FEC_FC_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {
                    '25G': ['CR'],
                    '50G': ['CR2']
                },
                SonicConst.PORT_SPLIT_NUM_2: {
                    '25G': ['CR'],
                    '50G': ['CR2']
                },
                SonicConst.PORT_SPLIT_NUM_4: {
                    '25G': ['CR'],
                    '50G': ['CR2']
                },
                SonicConst.PORT_SPLIT_NUM_8: {
                    '10G': ['CR'],
                    '25G': ['CR']
                }
            },
            SonicConst.FEC_RS_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR2'],
                                              '200G': ['CR4'],
                                              '400G': ['CR8'],
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR2'],
                                              '200G': ['CR4'],
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'25G': ['CR'],
                                              '50G': ['CR2'],
                                              '100G': ['CR2'],
                                              },
                SonicConst.PORT_SPLIT_NUM_8: {'25G': ['CR'],
                                              '50G': ['CR'],
                                              }
            },
            SonicConst.FEC_NONE_MODE: {
                SonicConst.PORT_SPLIT_NUM_1: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_2: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2'],
                                              '100G': ['CR4']
                                              },
                SonicConst.PORT_SPLIT_NUM_4: {'1G': ['CR'],
                                              '10G': ['CR'],
                                              '25G': ['CR'],
                                              '40G': ['CR4'],
                                              '50G': ['CR2']
                                              },
                SonicConst.PORT_SPLIT_NUM_8: {'10G': ['CR'],
                                              '25G': ['CR']
                                              }
            }
        }
    }
    COPPER_TYPE_PREFIX = 'CR'
    OPTIC_TYPE_PREFIX = 'SR'
    # Dictionary mapping between part numbers to speeds they do not support
    CABLE_PART_NUMBER_UNSUPPORTED_SPEEDS = {'MFS1S00-V003E': '10G'}


FEC_MODES_TO_ETHTOOL = {
    SonicConst.FEC_FC_MODE: "baser",
    SonicConst.FEC_RS_MODE: SonicConst.FEC_RS_MODE,
    SonicConst.FEC_NONE_MODE: "off",
    LinuxConsts.FEC_AUTO_MODE: LinuxConsts.FEC_AUTO_MODE
}


class P4SamplingEntryConsts:
    ENTRY_PRIORITY_HEADERS = ['PRIO']
    COUNTER_PACKETS_HEADERS = ['Packets']
    COUNTER_BYTES_HEADERS = ['Bytes']
    FLOW_ENTRY_KEY_HEADERS = ['Key SIP', 'Key DIP', 'Key PROTO', 'Key L4 SPORT', 'Key L4 DPORT',
                              'Key Checksum Value/Mask']
    PORT_ENTRY_KEY_HEADERS = ['Key Port', 'Key Checksum Value/Mask']
    FLOW_ENTRY_ACTION_HEADERS = [
        'Action',
        'Action Mirror Port',
        'Action SMAC',
        'Action DMAC',
        'Action SIP',
        'Action DIP',
        'Action VLAN',
        'Action Is Trunc',
        'Action Trunc Size']

    PORT_ENTRY_ACTION_HEADERS = [
        'Action',
        'Action Mirror Port',
        'Action SMAC',
        'Action DMAC',
        'Action SIP',
        'Action DIP',
        'Action VLAN',
        'Action Is Trunc',
        'Action Trunc Size']
    dutha1_ip = '10.0.0.1'
    duthb1_ip = '50.0.0.1'
    dutha2_ip = '10.0.1.1'
    duthb2_ip = '50.0.1.1'

    hadut1_ip = "10.0.0.2"
    hbdut1_ip = "50.0.0.2"
    hadut2_ip = '10.0.1.2'
    hbdut2_ip = '50.0.1.2'

    hadut1_network = "10.0.0.0"
    hbdut1_network = "50.0.0.0"
    hadut2_network = '10.0.0.0'
    hbdut2_network = '50.0.1.0'


class P4SamplingConsts:
    APP_NAME = 'p4-sampling'
    REPOSITORY = 'urm.nvidia.com/sw-nbu-sws-sonic-docker/p4-sampling'
    VERSION = '0.2.0-012'
    UPGRADE_TARGET_VERSION = '0.2.0-19'
    CONTTROL_IN_PORT = 'control_in_port'
    PORT_TABLE_NAME = 'table_port_sampling'
    FLOW_TABLE_NAME = 'table_flow_sampling'
    ACTION_NAME = 'DoMirror'
    TRAFFIC_INTERVAL = 0.2
    COUNTER_REFRESH_INTERVAL = 10


class LoganalyzerConsts:
    LOG_FILE_NAME = "syslog"


class P4ExamplesConsts:
    REPO_NAME = "urm.nvidia.com/sw-nbu-sws-sonic-docker/p4-examples"
    APP_NAME = 'p4-examples'
    APP_VERSION = '0.5.0'
    NO_EXAMPLE = "NO_EXAMPLE"
    VXLAN_BM_FEATURE_NAME = "VXLAN_BM"
    VXLAN_BM_ENCAP_TABLE = "p4-vxlan-bm-overlay-router"
    VXLAN_BM_DECAP_TABLE = "p4-vxlan-bm-tenant-forward"
    GTP_PARSER_TABLE = "p4-gtp"
    GTP_PARSER_P4NSPECT_TABLE = "match_gtpv1"
    GTP_PARSER_FEATURE_NAME = "GTP"


class SflowConsts:
    SFLOW_FEATURE_NAME = "sflow"
    DEFAULT_UDP = 6343
    UDP_1 = 6555
    COLLECTOR_0 = "collector0"
    COLLECTOR_1 = "collector1"
    COLLECTOR_F = "collector2"
    COLLECTOR = {
        COLLECTOR_0: {
            'name': COLLECTOR_0,
            'ip': '50.0.0.2',
            'ipv6': '5000::2',
            'port': DEFAULT_UDP,
            'sample_file': "/tmp/collector_0.json"
        },
        COLLECTOR_1: {
            'name': COLLECTOR_1,
            'ip': '60.0.0.2',
            'ipv6': '6000::2',
            'port': UDP_1,
            'sample_file': "/tmp/collector_1.json"
        },
        COLLECTOR_F: {
            'name': COLLECTOR_F,
            'ip': '70.1.1.2'
        }
    }

    LOOPBACK_0 = "Loopback0"
    LOOPBACK_0_IP = "1000:aaaa:bbbb:cccc:dddd:eeee:2000:3000"
    HSFLOWD_AUTOGEN_FILE = '/etc/hsflowd.auto'
    VRF_DEFAULT = "default"
    AGENT_ID_DEFAULT = "default"
    SFLOW_UP = "up"
    SFLOW_DOWN = "down"
    SFLOW_TOOL_PRETTY = "/usr/local/bin/sflowtool -J -p "
    POLLING_INTERVAL_DEVIATION_TOLERANCE = 2
    SAMPLE_RATE_1 = 500
    SAMPLE_RATE_2 = 300
    SAMPLE_RATE_3 = 1000
    SEND_PACKET_NUM = 1024

    DUT_HA_1_IP = "30.0.0.1"
    DUT_HA_2_IP = "40.0.0.1"
    HA_DUT_1_IP = "30.0.0.2"
    HA_DUT_2_IP = "40.0.0.2"

    DUT_HB_1_IP = "50.0.0.1"
    DUT_HB_2_IP = "60.0.0.1"
    DUT_HB_1_IP_V6 = "5000::1"
    DUT_HB_2_IP_V6 = "6000::1"
    HB_DUT_1_IP = "50.0.0.2"
    HB_DUT_2_IP = "60.0.0.2"
    HB_DUT_1_IP_V6 = "5000::2"
    HB_DUT_2_IP_V6 = "6000::2"
    COLLECTOR_0_IP = HB_DUT_1_IP
    COLLECTOR_1_IP = HB_DUT_2_IP
    COLLECTOR_0_IP_V6 = HB_DUT_1_IP_V6
    COLLECTOR_1_IP_V6 = HB_DUT_2_IP_V6
    COLLECTOR_LIST = [COLLECTOR_0, COLLECTOR_1]
    MGMT_INTF = 'eth0'
    POLLING_INTERVAL_0 = 0
    POLLING_INTERVAL_1 = 5
    POLLING_INTERVAL_2 = 10
    POLLING_INTF_0_WAIT_TIME = 10
    DUMMY_SOURCE_LOOPBACK_IP = "1.1.1.6"
    LOOPBACK_DEST_PORT_IP = "2.2.2.1"
    DUMMY_SOURCE_LOOPBACK_MAC = "00:11:22:33:44:55"
    SFLOW_REDIS_GENERAL_TABLES = ["SFLOW", "SFLOW_SESSION"]
    SFLOW_GLOBAL_REDIS_KEY = "SFLOW|global"
    POLLING_INTERVAL_REDIS_SUBKEY = "polling_interval"
    SAMPLE_RATE_REDIS_SUBKEY = "sample_rate"
    SFLOW_SESSION_IFACE_KEY_FORMAT = "SFLOW_SESSION|{iface_name}"


class CounterpollConstants:
    COUNTERPOLL_SHOW = 'sudo counterpoll show'
    COUNTERPOLL_DISABLE = 'sudo counterpoll {} disable'
    COUNTERPOLL_ENABLE = 'sudo counterpoll {} enable'
    COUNTERPOLL_RESTORE = 'sudo counterpoll {} {}'
    COUNTERPOLL_INTERVAL_STR = 'sudo counterpoll {} interval {}'
    COUNTERPOLL_QUEST = 'sudo counterpoll --help'
    EXCLUDE_COUNTER_SUB_COMMAND = ['show', 'config-db', "flowcnt-trap", "flowcnt-route", "tunnel", "delay"]
    INTERVAL = 'Interval (in ms)'
    TYPE = 'Type'
    STATUS = 'Status'
    STDOUT = 'stdout'
    PG_DROP = 'pg-drop'
    PG_DROP_STAT_TYPE = 'PG_DROP_STAT'
    QUEUE_STAT_TYPE = 'QUEUE_STAT'
    QUEUE = 'queue'
    PORT_STAT_TYPE = 'PORT_STAT'
    PORT = 'port'
    PORT_BUFFER_DROP_TYPE = 'PORT_BUFFER_DROP'
    PORT_BUFFER_DROP = 'port-buffer-drop'
    RIF_STAT_TYPE = 'RIF_STAT'
    RIF = 'rif'
    WATERMARK = 'watermark'
    QUEUE_WATERMARK_STAT_TYPE = 'QUEUE_WATERMARK_STAT'
    PG_WATERMARK_STAT_TYPE = 'PG_WATERMARK_STAT'
    BUFFER_POOL_WATERMARK_STAT_TYPE = 'BUFFER_POOL_WATERMARK_STAT'
    ACL = 'acl'
    ACL_TYPE = 'ACL'
    TUNNEL_STAT = 'tunnel'
    TUNNEL_STAT_TYPE = 'TUNNEL_STAT'
    FLOW_CNT_TRAP_STAT = 'flowcnt-trap'
    FLOW_CNT_TRAP_STAT_TYPE = 'FLOW_CNT_TRAP_STAT'
    FLOW_CNT_ROUTE_STAT = 'flowcnt-route'
    FLOW_CNT_ROUTE_STAT_TYPE = 'FLOW_CNT_ROUTE_STAT'

    COUNTERPOLL_MAPPING = {PG_DROP_STAT_TYPE: PG_DROP,
                           QUEUE_STAT_TYPE: QUEUE,
                           PORT_STAT_TYPE: PORT,
                           PORT_BUFFER_DROP_TYPE: PORT_BUFFER_DROP,
                           RIF_STAT_TYPE: RIF,
                           BUFFER_POOL_WATERMARK_STAT_TYPE: WATERMARK,
                           QUEUE_WATERMARK_STAT_TYPE: WATERMARK,
                           PG_WATERMARK_STAT_TYPE: WATERMARK,
                           ACL_TYPE: ACL,
                           TUNNEL_STAT_TYPE: TUNNEL_STAT,
                           FLOW_CNT_TRAP_STAT_TYPE: FLOW_CNT_TRAP_STAT,
                           FLOW_CNT_ROUTE_STAT_TYPE: FLOW_CNT_ROUTE_STAT}
    COUNTERPOLL_INTERVAL = {WATERMARK: 10000,
                            PORT: 10000,
                            RIF: 10000,
                            PG_DROP: 10000,
                            QUEUE: 10000,
                            PORT_BUFFER_DROP: 10000,
                            ACL: 10000,
                            TUNNEL_STAT: 10000}
    SX_SDK = 'sx_sdk'
    COUNTERPOLL_CPU_USAGE_THRESHOLD = 10
    COUNTERPOLL_CPU_USAGE_THRESHOLD_SIMX = 60
    WATERMARK_INTERVAL_1 = 10000
    WATERMARK_INTERVAL_DEFAULT = 60000

    CPU_THRESHOLD_FOR_ORDINARY_PROCESS = 50
    CPU_THRESHOLD_FOR_HIGH_CONSUME_PROCESS = 90
    MEMORY_THRESHOLD = 50
    MEMORY_THRESHOLD_ASAN = 90
    MEMORY_THRESHOLD_TIGON = 60
    MEMORY_THRESHOLD_LIONFISH = 65
    CPU_HIGH_CONSUME_PERSIST_TIME_THRESHOLD = 8

    CPU_MEMORY_SAMPLE_INTERVAL_1 = 5
    CPU_MEMORY_SAMPLE_ITERATION_1 = 24
    CPU_MEMORY_SAMPLE_INTERVAL_2 = 1
    CPU_MEMORY_SAMPLE_ITERATION_2 = 60


class MarsConstants:
    SONIC_MARS_BASE_PATH = "/.autodirect/sw_regression/system/SONIC/MARS"

    SONIC_MGMT_DEVICE_ID = "SONIC_MGMT"
    NGTS_PATH_PYTEST = "/ngts_venv/bin/pytest"
    NGTS_PATH_PYTHON = "/ngts_venv/bin/python"
    TEST_SERVER_DEVICE_ID = "TEST_SERVER"
    NGTS_DEVICE_ID = "NGTS"
    DUT_DEVICE_ID = "DUT"
    FANOUT_DEVICE_ID = "FANOUT"
    SONIC_MGMT_DIR = '/root/mars/workspace/sonic-mgmt/'
    UPDATED_FW_TAR_PATH = 'tests/platform_tests/fwutil/firmware.json'
    HTTP_SERVER_NBU_NFS = 'http://nbu-nfs.gtm.nvidia.com'

    DOCKER_SONIC_MGMT_IMAGE_NAME = "docker-sonic-mgmt"
    DOCKER_NGTS_IMAGE_NAME = "docker-ngts"

    SONIC_MGMT_REPO_URL = "http://10.7.77.140:8080/switchx/sonic/sonic-mgmt"
    SONIC_MGMT_MOUNTPOINTS = {
        '/.autodirect/mswg/projects': '/.autodirect/mswg/projects',
        '/auto/sw_system_project': '/auto/sw_system_project',
        '/auto/sw_system_release': '/auto/sw_system_release',
        '/.autodirect/sw_system_release/': '/.autodirect/sw_system_release/',
        '/auto/sw_regression/system/SONIC': '/auto/sw_regression/system/SONIC/',
        '/.autodirect/sw_regression/system/SONIC': '/.autodirect/sw_regression/system/SONIC',
        '/workspace': '/workspace',
        '/.autodirect/LIT/SCRIPTS': '/.autodirect/LIT/SCRIPTS',
        '/.autodirect/LIT/LOGS/RR/': '/.autodirect/LIT/LOGS/RR/',
        '/.autodirect/sw/release/': '/.autodirect/sw/release/',
        '/auto/LIT/SCRIPTS/': '/auto/LIT/SCRIPTS/',
        '/auto/LIT/LOGS/RR/': '/auto/LIT/LOGS/RR/',
        '/auto/mswg/utils/bin/': '/auto/mswg/utils/bin/'
    }

    VER_SDK_PATH = "/opt/ver_sdk"
    EXTRA_PACKAGE_PATH_LIST = ["/usr/lib64/python2.7/site-packages"]

    TOPO_ARRAY = (
        "t0-56-po2vlan", "t0", "t0-16", "t1-lag", "t1-lag-c224o8", "t1-28-lag", "t1-32-lag", "ptf32",
        "t0-64", "t0-64-256", "t0-c256", "t1-64-lag", "t0-56", "t0-56-o8v48", "t0-120",
        "t0-256", "t1-56-lag", "t1-isolated-d32u1s2", "t1-isolated-d82u1", "t1-isolated-d224u8", "t1-isolated-d448u15-lag", "t0-isolated-d128u128s1",
        "t0-isolated-d16u16s1", "t0-isolated-d16u16s2", "t0-28", "dualtor", "dualtor-64", "t0-isolated-d256u256s2",
        "dualtor-aa", "t0-isolated-d2u254s1", "t1-isolated-d254u2s1", "dualtor-64-breakout",
        "dualtor-aa-64-breakout", "t0-88-o8c80", "ptp-256", "ptp-130", "t1-isolated-d28u1", 't1-isolated-d28u4',
        "t1-isolated-d56u2", "t0-isolated-d32u32s2", "t0-isolated-v6-d32u32s2", "t0-isolated-d2u254s2", "t0-isolated-d32u32s2-mix",
        "t1-isolated-v6-d56u1-lag", "t1-48-lag", "t1-isolated-d56u1-lag", "t0-isolated-d128u128s2", "t0-isolated-d2u510s2", "t1-isolated-d510u2",
        "bmc-dual-mgmt", "t1-isolated-d32u1s2", "t2-isolated-d128s2", "t0-d96-u30s2", "ptp-126", "t1-d118u8s2", "t1-d96u4"
    )
    TOPO_ARRAY_DUALTOR = ("dualtor", "dualtor-64", "dualtor-aa", "dualtor-64-breakout", "dualtor-aa-64-breakout")
    TOPO_ARRAY_HA = ("t1-smartswitch-ha",)

    REBOOT_TYPES = {
        "reboot": "reboot",
        "fast-reboot": "fast-reboot",
        "warm-reboot": "warm-reboot"
    }

    DOCKER_REGISTRY = "nbu-harbor.gtm.nvidia.com"
    AIR_DOCKER_REGISTRY = "harbor-mts.nvidia.com"

    DUT_LOG_BACKUP_PATH = "/.autodirect/sw_system_project/sonic/dut_logs"

    BRANCH_PTF_MAPPING = {'202012': '42007',
                          '202106': '42007',
                          'master': '1123724'
                          }
    DEFAULT_PTF_TAG = '1123724'


class AppExtensionInstallationConstants:
    WJH_APP_NAME = 'what-just-happened'
    LC_MANAGER = 'line-card-manager'
    DOAI = 'doai'
    APPLICATION_LIST = [
        P4SamplingConsts.APP_NAME,
        WJH_APP_NAME,
        LC_MANAGER,
        P4ExamplesConsts.APP_NAME,
        DOAI
    ]
    APP_EXTENSION_PROJECT_MAPPING = {'sonic-wjh': WJH_APP_NAME,
                                     'p4-sampling': P4SamplingConsts.APP_NAME,
                                     'sonic-lc-manager': LC_MANAGER,
                                     'p4-examples': P4ExamplesConsts.APP_NAME,
                                     'doai': DOAI}
    APPS_WHERE_SX_SDK_NOT_PRESENT = [P4SamplingConsts.APP_NAME, P4ExamplesConsts.APP_NAME, DOAI]


class PlayersAliases:
    Aliases_list = ['sl']
    SL = 'sl'
    # Multi-DUT setups name their extra switches dut2..dut9 in Noga (an NVLink full rack holds 9 switches)
    multi_duts_list = [f'dut{index}' for index in range(2, 10)]
    duts_list = ['dut', 'dut-b'] + multi_duts_list + ['left-tg', 'right-tg', 'dpu0', 'dpu1', 'dpu2', 'dpu3']
    fanouts_list = ['fanout']


class NvosCliTypes:
    NvueCliTypes = ["NVUE", "MLNX_OS"]
    CumulusCliType = "CUMULUS_SWITCH"


class DVSCliTypes:
    DVSCliTypes = ["DVS"]


class BluefieldConstants:
    BMC_USER = os.getenv("BMC_USER")
    BMC_PASS = os.getenv("BMC_PASSWORD")

    PXE_SERVER = 'r-fit16-clone'
    PXE_SERVER_CONFIGS_PATH = '/tftpboot/uefiboot/grub/'
    BF2_HWSKU = 'Nvidia-MBF2H536C'
    BF3_HWSKU = 'Nvidia-9009d3b600CVAA'
    BF2_GRUB_CFG = 'bluefield-2.grub.cfg'
    BF3_GRUB_CFG = 'bluefield-3.grub.cfg'
    GRUB_CFG_FILE_MAP = {BF2_HWSKU: BF2_GRUB_CFG, BF3_HWSKU: BF3_GRUB_CFG}

    BLUEFIELD_HWSKUS_LIST = [BF2_HWSKU, '{}-C2'.format(BF2_HWSKU),
                             "Nvidia-9009d3b600CVAA",
                             "Nvidia-9009d3b600CVAA-C1",
                             "Nvidia-9009d3b600CVAA-C2",
                             "Nvidia-9009d3b600SVAA",
                             "Nvidia-9009d3b600SVAA-C1",
                             "Nvidia-9009d3b600SVAA-C2"]
    BLUEFIELD_PORTS_LIST = ['Ethernet0', 'Ethernet4']
    DPU_SETUP_LIST = [
        "CI_sonic_BF3_1",
    ]


class PerformanceSetupConstants:
    HWSKU = 'Mellanox-SN5600-O128'


class SonicDeployConstants:
    DEFAULT_HWSKU_FILE_PATH = 'ansible/files/hwsku_vars/default_hwsku.json'
    PRODUCTION_DUTS = ["bobcat-10", "mtvr-moose-11"]
    DPU_DATA_INTERFACES = ['Ethernet224', 'Ethernet232', 'Ethernet240', 'Ethernet248']
    SINGLE_SERVICE_PORT_PLATFORMS = [PlatformTypesConstants.PLATFORM_MOOSE_SIMX]
    ADD_TOPO_TIMEOUT = 3600
    ADD_TOPO_TIMEOUT_FACTOR = 1
    ADD_TOPO_TIMEOUT_SCALE = 7200
    ADD_TOPO_TIMEOUT_BGP_SCALE = 14400
    REMOVE_TOPO_TIMEOUT = 600
    REMOVE_TOPO_TIMEOUT_SCALE = 3600
    SCALE_TOPOLOGIES_LIST = ['t0-isolated-d128u128s1', 't0-isolated-d128u128s2', 't1-isolated-d224u8', 't1-isolated-d448u15-lag',
                             't0-isolated-d2u510s2', 't1-isolated-d510u2', 't1-isolated-v6-d56u1-lag',
                             't0-isolated-d256u256s2', 't1-isolated-d508u1s2', 't2-isolated-d128s2']
    BGP_SCALE_TOPOLOGIES_LIST = ['t0-isolated-d2u510s2']
    SLM_GEN_MG_ALLOWLIST = ['slm-chipless-2700a1-146']
    CHIPLESS_SETUPS = ['sonic_salamandra_r-salamandra-01', 'sonic_slm-chipless-2700a1-146']

    # Top-to-bottom order of the ONIE grub sub-entries, confirmed on real HW
    ONIE_GRUB_ENTRY_DOWN_COUNT_FROM_TOP = {
        'install': 0,
        'rescue': 1,
        'uninstall': 2,
        'update_onie': 3,
        'embed_onie': 4,
    }
    CLEANUP_SCRIPT_TIMEOUT = 1200


class BmcDeployConstants:
    """
    Constants used for BMC SONiC image installation.

    Reference: https://nvidia.atlassian.net/wiki/spaces/SW/pages/3239673921/BMC+SONiC#2.-Image-installation
    """
    # Create TFTP + HTTP file server on the sonic-mgmt container
    BMC_TFTP_ROOT = "/srv/tftp"
    BMC_TFTP_PORT = 69
    BMC_HTTP_PORT = 8080
    BMC_TFTP_SERVER_SCRIPT = "bmc_tftp_server.py"  # bundled under ngts/common/
    FILE_SERVER_PROC_RETRIES = 10
    FILE_SERVER_PROC_INTERVAL = 1

    # File parameters (staged into BMC_TFTP_ROOT from the --base-version-bmc tar)
    BMC_FIT_FILE_NAME = "sonic_tftp_install.fit"
    BMC_EMMC_IMG_FILE_NAME = "sonic-aspeed-arm64-emmc.img.gz"

    # U-Boot parameters
    UBOOT_PROMPT = "=> "
    UBOOT_AUTOBOOT_HINT = "Hit any key to stop autoboot"
    UBOOT_LOAD_ADDR = "0x432000000"
    UBOOT_ENV_DEFAULT_CMD = "env default -f -a"
    UBOOT_DHCP_BOUND_MARKER = "DHCP client bound to address"
    UBOOT_BOOTCONF_DEFAULT = "sonic-ast2700-nvidia-spc6-bmc"
    UBOOT_BOOTARGS_TAILS = (
        'console=ttyS12,${{baudrate}}n8 earlycon=uart8250,mmio32,0x14c33b00 ',
        'root=/dev/ram0 rw ',
        'sonic_install.tftp_server=${{serverip}} ',
        'sonic_install.bmc_image={http_server}/{emmc_img} ',
    )

    # Marker parameter
    EMMC_WRITE_DONE_MARKER = "U-Boot environment updated"
    BMC_LOGIN_PROMPT = "login:"

    # Post-install SSH credentials (verify login + chrony clock sync).
    BMC_SONIC_OS_USERNAME = os.getenv("SONIC_SWITCH_USER")
    BMC_SONIC_OS_PASSWORD = os.getenv("SONIC_SWITCH_PASSWORD")

    # BMC DEVICE_METADATA
    BMC_DEVICE_METADATA_TYPE = "NetworkBmc"

    # DHCP recovery workaround (Redmine #5091238): after a reboot / power-cycle
    # the ftgmac100 NIC reports link up but no DHCP offer arrives, so eth0 never
    # gets a lease. A systemd timer periodically reloads the driver and re-runs
    # dhclient whenever eth0 has no IPv4 address (a oneshot at boot is not enough
    # - the IP can flap right after boot and the lease can drop at runtime). The
    # script and units are staged in ngts/common/ and installed during the BMC
    # deploy; the timer is what gets enabled.
    BMC_DHCP_WA_TIMER_NAME = "bmc-net-recover.timer"
    BMC_DHCP_WA_SCRIPT_SRC = "bmc_net_recover.sh"
    BMC_DHCP_WA_SERVICE_SRC = "bmc-net-recover.service"
    BMC_DHCP_WA_TIMER_SRC = "bmc-net-recover.timer"
    BMC_DHCP_WA_SCRIPT_DST = "/usr/local/bin/bmc_net_recover.sh"
    BMC_DHCP_WA_SERVICE_DST = "/etc/systemd/system/bmc-net-recover.service"
    BMC_DHCP_WA_TIMER_DST = "/etc/systemd/system/bmc-net-recover.timer"

    # AST HW types allowed to run a SONiC BMC image. AST2600 is
    # OpenBMC-only. AST2700-A1 is here temporarily; drop it once
    # AST2700-A1 is moved to OpenBMC-only.
    SONIC_BMC_SUPPORTED_HW_TYPES = ('AST2700-A1', 'AST2700-A2')
    if is_redmine_issue_active([5159518])[0]:
        SONIC_BMC_SUPPORTED_HW_TYPES += ('AST2700',)

    # BMC HWSKU
    BMC_HWSKU = "NVIDIA-AST2700-BMC"

    # Switch topo when deploy bmc setup
    SWITCH_DUAL_DEPLOY_TOPO = "ptp-64"

    # BMC UART
    BMC_UART_SEL_PATH = "/var/run/hw-management/system/uart_sel"
    BMC_UART_SEL_HOST = 2   # serial console connected to the host switch CPU
    BMC_UART_SEL_BMC = 0    # serial console connected to the BMC (default)
    BMC_POWERCTRL_SCRIPT = "/usr/bin/hw-management-bmc-powerctrl.sh"
    BMC_HOST_POWER_OFF_SETTLE = 30   # wait after power_off before power_on
    BMC_HOST_POWER_ON_SETTLE = 10    # wait after power_on before watching the host serial console

    # Timeouts (seconds).
    UBOOT_PROMPT_TIMEOUT = 120         # time to interrupt autoboot and reach U-Boot prompt
    DHCP_TIMEOUT = 60                  # one DHCP attempt timeout
    DHCP_RETRY_LIMIT = 3               # number of DHCP retries when the first attempt fails
    TFTP_DOWNLOAD_TIMEOUT = 900        # time to download the FIT image via TFTP
    TFTP_DOWNLOAD_RETRY_LIMIT = 3      # retries when TFTP fails (e.g. ARP retry exceeded)
    EMMC_WRITE_TIMEOUT = 1800          # time for the eMMC write to finish
    BMC_BOOT_TIMEOUT = 900             # time for the BMC to come up and present login prompt
    BMC_CHRONY_SETTLE_SECONDS = 5      # wait after starting chrony before forcing 'chronyc -a makestep'


class RebootTestConstants:
    DATAPLANE_TRAFFIC_RESULTS_FILE = '/tmp/reboot_dataplane_result.json'
    CONTROLPLANE_TRAFFIC_RESULTS_FILE = '/tmp/reboot_controlplane_result.json'
    IFACES_STATUS_FILE = '/tmp/reboot_ifaces_status.json'


class Sonic_Cache:
    VER_DB_PATH = "/auto/sw_system_release/sonic/verification_db"
    REDMINE_ISSUES_STATUS_CACHE = "/auto/sw_system_release/sonic/verification_db/redmine_issues.json"
    PROJECT_NAME = "switchx/sonic/sonic-mgmt"
    GERRIT_API_URL = "https://git-nbu-sw.nvidia.com/r"
    branches_url = "https://git-nbu-sw.nvidia.com/r/plugins/gitiles/switchx/sonic/sonic-mgmt/+/refs/heads"
    SONIC_MGMT_REDMINE_CACHE = "/root/mars/workspace/sonic-mgmt/ngts/scripts/redmine_cache/redmine_issues.json"
    gerrit_api_token = os.getenv("GERRIT_API_KEY")
    gerrit_username = os.getenv("SVC_SONIC_VER_BOT")


class VxlanConstants:
    HOST_HA = 'ha'
    HOST_HB = 'hb'
    PACKET_NUM_500 = 500
    PACKET_NUM_400 = 400
    PACKET_NUM_200 = 200
    PACKET_NUM_100 = 100
    PACKET_NUM_10 = 10
    PACKET_NUM_3 = 3
    PACKET_NUM_0 = 0
    STATIC_MAC_OPERATION_SET = 'SET'
    STATIC_MAC_OPERATION_DEL = 'DEL'
    MTU_1500 = '1500'
    MTU_9100 = '9100'
    MTU_1000 = '1000'
    JUMBO_PACKET_LEN = 9000
    NORMAL_PACKET_LEN = 1000
    PCAP_PATH = '/tmp/evpn_ecmp_{}.pcap'
    INTF_CNT_STAT_ITEMS = ['RX_DRP', 'RX_ERR', 'RX_OK', 'RX_OVR', 'TX_DRP', 'TX_ERR', 'TX_OK', 'TX_OVR']

    DUT_VNI_INTF_ADDRESS_TEMPLATE = '200.{}.0.1'
    VM_VNI_INTF_ADDRESS_TEMPLATE = '200.{}.0.2'
    BR_500100_IP = '100.0.0.2'
    BR_500200_IP = '200.0.0.2'
    IP_GENERATE_SEED = 201

    VETH_NAME_1 = 'tap_3333_1'
    VETH_PEER_NAME_1 = 'tap_3333_2'
    VETH_NAME_100 = 'tap_100_1'
    VETH_PEER_NAME_100 = 'tap_100_2'
    VETH_NAME_101 = 'tap_101_1'
    VETH_PEER_NAME_101 = 'tap_101_2'
    NAME_SPACE_1 = 'ns1'
    NAME_SPACE_100 = 'ns100'
    NAME_SPACE_101 = 'ns101'
    VETH_IP = '3.3.3.10'
    VETH_IP_ADDR_1 = '100.0.0.200'
    DST_IP_101_0_0_3 = '101.0.0.3'
    UNKNOWN_SRC_IP = '1.0.0.3'
    UNKNOWN_DST_IP = '101.0.0.100'

    BGP_SESSION_ID = 65000
    PREFIX_LENGTH = 24
    EVPN_ROUTE_MAP = 'EVPN-OUT'
    DENY = 'deny'
    PERMIT = 'permit'
    RD_20 = '20'
    RD_100 = '100'
    RD_101 = '101'
    RD_200 = '200'
    RD_3333 = '3333'
    VLAN_3 = 3
    VLAN_10 = 10
    VLAN_20 = 20
    VLAN_100 = 100
    VLAN_101 = 101
    VLAN_200 = 200
    VNI_12345 = 12345
    VNI_54321 = 54321
    VNI_50020 = 50020
    VNI_500100 = 500100
    VNI_500101 = 500101
    VNI_500200 = 500200
    VNI_3333 = 3333
    VNI_3333_IFACE = 'br_{}'.format(VNI_3333)
    HA_VXLAN_54321_IFACE = 'vtep_{}'.format(VNI_54321)
    VNI_50020_IFACE = 'br_{}'.format(VNI_50020)
    VNI_500100_IFACE = 'br_{}'.format(VNI_500100)
    VNI_500101_IFACE = 'br_{}'.format(VNI_500101)
    VNI_500200_IFACE = 'br_{}'.format(VNI_500200)

    EVPN_NVO = 'my-nvo'
    VTEP_NAME_DUT = 'vtep101032'
    VTEP_INTERFACE = 'Loopback0'
    SIMPLE_PACKET = 'Ether(src="{}",dst="{}")/IP(src="{}",dst="{}")/UDP()'
    SIMPLE_DOT1Q_PACKET = 'Ether(src="{}",dst="{}")/Dot1Q(vlan={})/IP(src="{}",dst="{}")/UDP()'
    ECMP_SIMPLE_PACKET = 'Ether(src="{}",dst="{}")/IP(src={},dst="{}")/UDP()'
    ECMP_SIMPLE_PACKET_V6 = 'Ether(src="{}",dst="{}")/IPv6(src={},dst="{}")/UDP()'
    ECMP_VARIABLE_LENGTH_PACKET = 'Ether(src="{}",dst="{}")/IP(src={},dst="{}")/UDP()/Raw(RandString(size={}))'
    ECMP_VARIABLE_LENGTH_PACKET_V6 = \
        'Ether(src="{}",dst="{}")/IPv6(src={},dst="{}")/UDP()/Raw(RandString(size={}))'
    SIMPLE_PACKET_FILTER = 'udp and dst host {}'
    # filter VXLAN packet with vni and encapsulated src IP
    TCPDUMP_VXLAN_SRC_IP_FILTER = 'port 4789 and ether[46:4]={} and ether[76:4]={}'
    # filter VXLAN packet with vni and encapsulated dst IP
    TCPDUMP_VXLAN_DST_IP_FILTER = 'port 4789 and ether[46:4]={} and ether[80:4]={}'
    TCPDUMP_VXLAN_DST_IPV6_FILTER = 'port 4789 and ether[46:4]={} and ether[88:4]={}'
    HEX_UNKNOWN_SRC_IP = '0x01000003'
    HEX_5_5_5_1 = '0x05050501'
    HEX_20_0_0_3 = '0x14000003'  # hex value for 20.0.0.3
    HEX_100_0_0_2 = '0x64000002'  # hex value for 100.0.0.2
    HEX_100_0_0_3 = '0x64000003'  # hex value for 100.0.0.3
    HEX_101_0_0_2 = '0x65000002'  # hex value for 101.0.0.2
    HEX_101_0_0_3 = '0x65000003'  # hex value for 101.0.0.3
    HEX_200_0_0_2 = '0xc8000002'  # hex value for 200.0.0.2
    HEX_255_255_255_255 = '0xffffffff'  # hex value for 255.255.255.255
    HEX_5000_2 = '0x50000000'  # hex value for 5000::2, scapy only support 4 byte size data matching
    HEX_500100 = '0x7a18400'
    HEX_500101 = '0x7a18500'
    HEX_500200 = '0x7a1e800'
    IP_HEX_MAP = {BR_500100_IP: HEX_100_0_0_2, BR_500200_IP: HEX_200_0_0_2}
    VNI_HEX_MAP = {VNI_500100: HEX_500100, VNI_500200: HEX_500200}
    STATIC_MAC_ADDR = '00-00-00-11-22-33'
    SOURCE_MAC_ADDR_1 = '00:11:22:33:44:55'
    UNKNOWN_UNICAST_MAC = '00:00:00:00:00:aa'
    UNKNOWN_UNICAST_MAC_1 = '00:00:00:99:99:99'
    BROADCAST_MAC = 'ff:ff:ff:ff:ff:ff'
    BROADCAST_IP = '255.255.255.255'
    ECMP_TRAFFIC_SRC_IP_LIST = ['3.3.3.3', '3.3.3.4', '3.3.3.5', '3.3.3.6']
    ECMP_TRAFFIC_SRC_IPV6_LIST = ['2000::3', '2000::4', '2000::5', '2000::6']
    BULK_ECMP_TRAFFIC_SRC_IP_LIST = eval("['3.3.3.' + str(i+1) for i in range(100)]")
    BULK_ECMP_TRAFFIC_SRC_IPV6_LIST = eval("['2000::' + str(i+1) for i in range(100)]")


class SanitizerConst:
    SENDER_MAIL = 'noreply@sanitizer.com'
    ASAN_APPS = ["what-just-happened"]
    # Extra wait (seconds) for ASAN/sanitizer reboot vs regular image (Redmine #5134692).
    SANITIZER_REBOOT_EXTRA_WAIT_SEC = 60

    NVOS_MAIL = 'nbu-system-sw-mlnxos20-ext@exchange.nvidia.com'
    SONIC_MAIL = "nbu-system-sw-sonic-ver@exchange.nvidia.com"
    CLI_TYPE_MAIL = \
        {"MLNX_OS": NVOS_MAIL,
         "NVUE": NVOS_MAIL,
         "Sonic": SONIC_MAIL}


class ResultUploaderConst:
    SONIC_CANONICAL_NOGA_GROUP = "SONiC_Canonical"
    SONIC_COMMUNITY_NOGA_GROUP = "SONiC_Community"
    SONIC_NOGA_GROUPS = [SONIC_CANONICAL_NOGA_GROUP, SONIC_COMMUNITY_NOGA_GROUP]
    XLSX_HEADER = ["session_id", "mars_key_id", "testbed", "test name", "start", "end", "result", "message",
                   "topology", "host", "asic", "platform", "hwsku", "os_version", "xml_path",
                   "was_xml_updated", "modify_xml", "sanitized_testname"]
    MSFT_XLSX_HEADER = ["testbed", "test name", "start", "end", "result", "message",
                        "topology", "host", "asic", "platform", "hwsku",
                        "os_version", "xml_path", "sanitized_testname"]
    SANITIZED_HOSTNAME = "switch-hostname"
    REMOVE_FAILED_TESTCASE = "REMOVE_FAILED_TESTCASE"
    DATABASE_NAME = "NvidiaTestData"
    UPLOADER_SCRIPT_RELATIVE_PATH = "test_reporting/report_uploader.py"
    COMMUNITY_DBS_RELATIVE_PATH = "sonic-tool/mars/dbs/community"
    MSFT_PLATFORMS = ["4600C", "4700", "2700"]
    # Setups whose Noga names do not follow the sonic_<codename>_<host> convention
    # and would be dropped by filter_canonical_setups. List them here to keep them
    NON_STANDARD_NAMED_SETUPS = ["sonic_slm-chipless-2700a1-146"]
    MARS_RELEASE = "/auto/sw_tools/Internal/MARS/mars_apps/RELEASE/4_3_3/bin/"
    QUERY_SESSIONS_SCRIPT = os.path.join(MARS_RELEASE, "save_delete_sessions_query.py")
    SAVE_SESSIONS_SCRIPT = os.path.join(MARS_RELEASE, "save_delete_sessions.py")
    SESSION_STARTED_BY_REGEX = ["mars_orch_sw-mars-orch@friday_canonical_full_regression_main_branch",
                                "mars_orch_sw-mars-orch@canonical_full_regression_main_branch",
                                "mars_orch_sw-mars-orch@Comex_Friday_Set",
                                "mars_orch_sw-mars-orch@Community_Comex_Regression",
                                "mars_orch_sw-mars-orch@Community_Regression_Dual_Tor",
                                "mars_orch_sw-mars-orch@Community_Regression",
                                "mars_orch_sw-mars-orch@Friday_Community_Regression"]
    HOST_INTERNAL_NAMES_LIST = ["panther", "lionfish", "tigon", "leopard",
                                "moose", "bobcat", "bison", "gaur", "salamandra"]

    HOST_INTERNAL_NAMES_MAP = {"panther": "msn2700",
                               "lionfish": "msn3420",
                               "tigon": "msn4600c",
                               "leopard": "msn4700",
                               "moose": "sn5600",
                               "bobcat": "sn4280",
                               "gaur": "sn5610n",
                               "bison": "sn5640",
                               "salamandra": "sn6600_ld"}


class BugHandlerConst:
    NGTS_PATH, path_suffix = os.path.abspath(__file__).split('constants/')
    CLI_TYPE_REDMINE_PROJECT = \
        {"MLNX_OS": "mlnxOS-Eth",
         "NVUE": "'NVOS - Design'",
         "Sonic": "SONiC-Design"}
    SONIC_BUG_HANDLER_CONF_FILE = os.path.join(NGTS_PATH, "helpers/bug_handler/sonic_bug_handler.conf")
    BUG_HANDLER_CONF_FILE = {"SONiC-Design": SONIC_BUG_HANDLER_CONF_FILE,
                             "SONiC-Verification": SONIC_BUG_HANDLER_CONF_FILE,
                             "'NVOS - Design'": os.path.join(NGTS_PATH, "helpers/bug_handler/nvos_bug_handler.conf")}
    BUG_HANDLER_PYTHON_PATH = "/mswg/projects/swvt/MARS/scripts/python37_wrapper.sh"
    BH_1_5_2_PATH = "/auto/sw_tools/Internal/BugHandling/RELEASES/1_5_2/bin/"
    BH_1_5_3_PATH = "/auto/sw_tools/Internal/BugHandling/RELEASES/1_5_3/bin/"
    BH_2026_1_1_PATH = "/auto/sw_tools/Internal/BugHandling/RELEASES/2026_1_1/bin/"
    BH_2026_6_PATH = "/auto/sw_tools/Internal/BugHandling/RELEASES/2026_6/bin/"
    BUG_HANDLER_PATH = {
        "SONiC-Design": BH_2026_6_PATH,
        "SONiC-Verification": BH_2026_6_PATH,
        "'NVOS - Design'": "/auto/sw_tools/Internal/BugHandling/RELEASES/2026_9/bin/",
        "default": BH_1_5_2_PATH
    }
    BUG_HANDLER_SCRIPT = {}
    BUG_HANDLER_UPLOAD_ATTACHMENT_SCRIPT = {}
    for key in BUG_HANDLER_PATH:
        BUG_HANDLER_SCRIPT[key] = os.path.join(BUG_HANDLER_PATH[key], "handle_bug.py")
        BUG_HANDLER_UPLOAD_ATTACHMENT_SCRIPT[key] = os.path.join(BUG_HANDLER_PATH[key], "upload_attachment_to_bug.py")
    BUG_HANDLER_SANITIZER_USER = "asan"
    BUG_HANDLER_LOG_ANALYZER_USER = "log_analyzer"
    SANITIZER_PARSED_DUMPS_FOLDER = "/tmp/parsed_sanitizer_dumps/"
    BUG_HANDLER_DECISION_UPDATE = "update"
    BUG_HANDLER_DECISION_SKIP = "skip"
    BUG_HANDLER_DECISION_CREATE = "create"
    BUG_HANDLER_DECISION_ABORT = "abort"
    BUG_HANDLER_DECISION_REOPEN = "reopen"
    NO_ACTION_MODE = "no action"
    UPDATE_ONLY = "update only"
    RC_ABORT = 2
    DECISION = "decision"
    NEW_BUGS = "new_bugs"
    EXISTING_BUGS = "existing_bugs"
    UPDATE_BUG = "update_bug"
    SKIP_UPDATE_BUG = "skip_update_bug"
    LA_ERROR = "la_error"
    BUG_HANDLER_FAILURE = "bug_handler_failure"
    LOG_ERRORS_DIR_PATH = "/tmp/loganalyzer/{hostname}"
    BUG_HANDLER_RC = "rc"
    BUG_HANDLER_STATUS = "status"
    BUG_HANDLER_ACTION = "action"
    BUG_HANDLER_BUG_ID = "bug_id"
    LA_BUG_HANDLER_ATTACHMENT_PREFIX = "la_bug_handler_"
    BUG_HANDLER_MESSAGES = "messages"
    LA_RM_ISSUES_DICT = "la_rm_issues_dict"
    BUG_HANDLER_FAILURE_EXCEPTION = "The log analyzer bug handler has failed"
    BUG_HANDLER_SUCCESS_ACTIONS_LIST = [BUG_HANDLER_DECISION_CREATE, BUG_HANDLER_DECISION_UPDATE,
                                        BUG_HANDLER_DECISION_SKIP]
    BUG_TITLE_LIMIT = 230
    BUG_HANDLER_SKIP_BRNACH = ['202305']
    TIMESTAMP_FORMATS = ["%b %d %H:%M:%S", "%Y %b %d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%b %d %H:%M:%S.%f",
                         "%Y-%m-%d %H:%M:%S"]
    TIMESTAMP_LENGTH = [len(datetime.now().strftime(format)) for format in TIMESTAMP_FORMATS]
    LOG_ERRORS_FILE_ROOT_ITEM = "log_errors"
    TAR_FILE_SIZE_RM_LIMIT = 102400000


class SerialLoggerConst:
    LOG_DIR = os.path.join(os.path.sep, ".autodirect", "sw_regression", "system", "NVOS", "MARS", "results",
                           "{setup_name}", "{session_id}", "serial_logs")
    ALL_PERM = 0o777
    DATETIME_FORMAT = BugHandlerConst.TIMESTAMP_FORMATS[0]
    START_SERIAL_LOGGING = "Start Session Logging"
    MANUFACTURE_STAGE = "Manufacture"
    UPGRADE_STAGE = "Upgrade"
    START_STAGE = "    ============== start {test} : {stage} stage ==============\n"
    END_STAGE = "    ============== end {test} : {stage} stage ==============\n"
    CMD_LINE_KEY = '--serial_logger'
    MODE_OFF = "off"
    MODE_STORE = "store"
    MODE_ANALYZE = "analyze"
    MODE_ANALYZE_AND_OPEN_BUGS = "analyze_and_open_bugs"
    CMD_LINE_VALUES = (MODE_OFF, MODE_STORE, MODE_ANALYZE, MODE_ANALYZE_AND_OPEN_BUGS)


class DebugKernelConsts:
    KMEMLEAK_PATH = '/sys/kernel/debug/kmemleak'
    KMEMLEAK = 'kmemleak'


class CoreDumpConsts:
    COREDUMP_PATH = '/var/lib/systemd/coredump'


class FatalStateConsts:
    FATAL_FILE = "/etc/system_fatal"
    FATAL_STATE_RETURN_CODE = 42


class GnmiConsts:
    GNMI_DOCKER = 'nv-gnmi'
    GNMI_SOCKET_PATH = '/var/run/nv-gnmi/gnmi.sock'
    GNMI_READY_STABILIZATION_SEC = 2
    GNMI_STATE_FIELD = 'state'
    GNMI_STATE_ENABLED = 'enabled'
    GNMI_STATE_DISABLED = 'disabled'
    GNMI_VERSION_FIELD = 'version'
    GNMI_DEFAULT_PORT = '9339'
    SLEEP_TIME_FOR_UPDATE = 40
    # Time to wait after a gNMI apply (set/unset with apply=True) before issuing
    # follow-up commands, so the server has time to pick up the new config.
    CONFIG_SETTLE_TIME_SEC = 6
    REDIS_CMD_KEY = 'redis_cmd'
    XPATH_KEY = 'xpath_gnmi_cmd'
    COMPARISON_KEY = 'comparison_dict'
    REDIS_CMD_DB_NAME = "redis_cmd_db_num"
    REDIS_CMD_TABLE_NAME = "redis_cmd_table"
    REDIS_CMD_PARAM = "redis_cmd_param"
    CPU_USAGE_MONITOR_INTERVAL = 10
    STREAM_EVENTS_INTERVAL = 2
    STREAM_PERFORMANCE_EVENTS_BATCH_SIZE = 50
    STREAM_PERFORMANCE_EVENTS_MAX_SIZE = 5000
    STREAM_PERFORMANCE_TOTAL_DURATION = 300
    ONIE_COMPONENT = 'ONIE'
    FW_VERSION = 'fw_version'
    PART_NUMBER = 'part_number'
    DESCRIPTION = 'description'
    SYSTEM_COMPONENTS = 'SYSTEM_COMPONENTS'
    LONG_TERM_AVERAGE_GNMI = "long-term-avg-power"
    SHORT_TERM_AVERAGE_GNMI = "short-term-avg-power"
    POWER_TELEMETRY_ASIC_GNMI_FIELDS = [LONG_TERM_AVERAGE_GNMI, SHORT_TERM_AVERAGE_GNMI]


class CableComplianceConst:
    EXTENDED_SPEC_COMPLIANCE_PREFIX = "Extended Specification Compliance"
    SPEC_COMPLIANCE_PREFIX = "Specification compliance"
    SFP_COMPLIANCE = "SFP+CableTechnology"
    UNDETECTED_EEPROM_MSG = "SFP EEPROM Not detected"
    UNDETECTED_RJ45_EEPROM_MSG = "SFP EEPROM is not applicable for RJ45 port"
    SUPPORTED_CABLE_DEFAULT = "Cable Always Supports Auto-Neg"
    DEFAULT_CABLE_COMPLIANCE_TUPLE = (SUPPORTED_CABLE_DEFAULT, SUPPORTED_CABLE_DEFAULT)
    SUPPORTED_SPECIFICATION_COMPLIANCE = {SPEC_COMPLIANCE_PREFIX: ["passive_copper_media_interface"],
                                          SFP_COMPLIANCE: ["Passive Cable"],
                                          # This following regex will match the pattern iff it
                                          # contains a iGBASE-?R and for each iGBASE-?R, ?=C
                                          EXTENDED_SPEC_COMPLIANCE_PREFIX: [r"^(?=.*\d+GBASE-CR)(?!\d+GBASE-[^C]R).*$"],
                                          SUPPORTED_CABLE_DEFAULT: [".*"]  # which match anything
                                          }
    UNSUPPORTED_SPECIFICATION_COMPLIANCE = {
        SPEC_COMPLIANCE_PREFIX: ["active_cable_media_interface", "sm_media_interface",
                                 "nm_850_media_interface"],
        EXTENDED_SPEC_COMPLIANCE_PREFIX: [r"\d+GBASE-DR", r"\d+GBASE-SR", r"AOC", r"Active Optical Cable"]}


class IndependentModuleConst:
    IM_SAI_ATTRIBUTE_NAME = "SAI_INDEPENDENT_MODULE_MODE"
    IM_CONTROL_FILE_PATH = "/sys/module/sx_core/asic0/module{PORT_NUMBER}/control"
    MEDIA_SETTINGS_FILE_NAME = "media_settings.json"
    OPTICS_SI_SETTINGS_FILE_NAME = "optics_si_settings.json"
    IM_INTERFACE_SETTINGS_FILE_PATH = "/usr/share/sonic/device/{PLATFORM}"
    PLATFORM_GENERATION = ['4280', '4700', '5600', '5610']
    AOC_VENDOR_PN = ['MMS1V00-WM', 'MMS4X00-NS', 'MFA7U10-H003', 'MMA4Z00-NS', '7123-G78-09', 'ALQA9N11ADLA1580',
                     'ALQA9N03ADLA1580', 'T-OH8CNT-NMT', 'MMS1X00-NS400', 'T-RH8ENH-NMT', '980-9IAU9-00XM0H']


class SSHConsts:
    SONIC_CREDS = {"username": os.getenv("SONIC_SWITCH_USER"), "password": os.getenv("SONIC_SWITCH_PASSWORD")}
    CL_CREDS = {"username": os.getenv("CUMULUS_SWITCH_USER"), "password": os.getenv("CUMULUS_SWITCH_PASSWORD")}
    DVS_CREDS = {"username": os.getenv("DVS_ROOT_USER"), "password": os.getenv("DVS_ROOT_PASSWORD")}
    SSH_CREDS_DICT = {"SONiC": SONIC_CREDS, "Cumulus": CL_CREDS, "DVS": DVS_CREDS}

    CLI_TYPE_TO_NOS = {
        CliType.NVUE: "Cumulus",
        CliType.SONIC: "SONiC",
        CliType.DVS: "DVS",
    }


class DoroceConsts:
    BUFFER_CONFIGURATIONS_DICT = {'lossless_double_ipool': ['egress_lossless_pool',
                                                            'egress_lossy_pool',
                                                            'ingress_lossless_pool',
                                                            'ingress_lossy_pool'],
                                  'lossless_single_ipool': ['egress_lossless_pool',
                                                            'egress_lossy_pool',
                                                            'ingress_lossless_pool'],
                                  'lossy_double_ipool': ['egress_lossy_pool',
                                                         'ingress_lossy_pool',
                                                         'roce_reserved_egress_pool',
                                                         'roce_reserved_ingress_pool']
                                  }
    ROCE_POOLS = 'roce_pools'
    NON_ROCE_POOLS = 'non_roce_pools'
    PERCENTAGE_POOLS_DICT = {'lossless_double_ipool': {ROCE_POOLS: ['ingress_lossless_pool'],
                                                       NON_ROCE_POOLS: ['ingress_lossy_pool',
                                                                        'egress_lossy_pool']},
                             'lossy_double_ipool': {ROCE_POOLS: ['roce_reserved_egress_pool',
                                                                 'roce_reserved_ingress_pool'],
                                                    NON_ROCE_POOLS: ['ingress_lossy_pool',
                                                                     'egress_lossy_pool']}}
    BUFFER_CONFIGURATIONS = list(BUFFER_CONFIGURATIONS_DICT.keys())
    ROCE_PG = 'PG3'
    NO_ROCE_PG = 'PG0'
    WATERMARK_THRESHOLD = '1000'
    ALLOWED_PERCENTAGE_DEVIATION = 2


FILE_INCLUDE_FAILED_SANITY_CHECKER_CASE = "/tmp/file_include_failed_sanity_checker_case.txt"
INSTALLED_DPUS = '/root/mars/workspace/sonic-mgmt/installed_dpus'


class WJHConsts:
    BUFFER_TABLE_SEPARATOR = "Buffer Info"
    ACL_TABLE_SEPARATOR = "Rules Info"
    RAW_PACKET_COUNT = 1
    AGG_PACKET_COUNT = 50
    AGGREGATE_FLAG = "--aggregate"
    WJH_POLL_CMD_PREFIX = "show what-just-happened poll"
    FORWARDING = "forwarding"
    BUFFER = "buffer"
    LAYER_1 = "layer-1"
    ACL = "acl"
    TCP_PROTO = 'tcp'
    UDP_PROTO = 'udp'
    IP_PROTO = 'ip'
    RAW_CHANNEL = 'raw'
    AGG_CHANNEL = 'aggregate'
    RAW_TABLE = 'raw'
    RAW_ACL_TABLE = 'raw_acl_buffer_info'
    AGG_TABLE = 'agg'
    AGG_ACL_TABLE = 'agg_acl_buffer_info'
    TESTED_SRC_MAC = '00:11:22:33:44:55'
    TESTED_SRC_IP = '40.0.0.6'
    TESTED_DST_IP = '40.0.0.7'
    NA = 'N/A'
    TCP_IPV4_PACKET_FORMAT = 'Ether(src="{SRC_MAC}", dst="{DST_MAC}")/IP(src="{SRC_IP}", dst="{DST_IP}")/TCP()'
    TCP_IPV6_PACKET_FORMAT = 'Ether(src="{SRC_MAC}", dst="{DST_MAC}")/IPv6(src="{SRC_IP}", dst="{DST_IP}")/TCP()'


class FanoutVersionConsts:
    EXPECTED_MLNX_VERSION = ["3.10.4206", "3.10.4302", "3.10.4602"]
    EXPECTED_SONIC_VERSION_LIST = ["202511_RC.129-48da86131_Internal", "202511_RC.150-cf87140a93_Internal", "202605_RC.76-88000f5c8c_Internal"]


class SerialConsts:
    PLATFORM_SERIAL_NUM_MAP = {
        "x86_64-mlnx_msn2700_simx-r0": "MT1706K08716",
        "x86_64-mlnx_msn4700_simx-r0": "MT2011X02163",
        "x86_64-nvidia_sn5600_simx-r0": "MT2315XZ04ZJ",
        "x86_64-nvidia_sn5640_simx-r0": "MT25116050YD",
        "x86_64-nvidia_sn5800_ld_simx-r0": "MT251160509J",
        "x86_64-nvidia_sn6600_simx-r0": "MT25116050YD",
        "x86_64-nvidia_sn6600_ld_simx-r0": "MT25116050YD",
        "x86_64-nvidia_sn6810_ld_simx-r0": "MT2607601EDV"
    }
    PLATFORM_MODEL_MAP = {
        "x86_64-mlnx_msn2700_simx-r0": "MSN2700-CS2FO",
        "x86_64-mlnx_msn4700_simx-r0": "MSN4700-WS2FO",
        "x86_64-nvidia_sn5600_simx-r0": "SN5600",
        "x86_64-nvidia_sn5640_simx-r0": "MSN5640",
        "x86_64-nvidia_sn5800_ld_simx-r0": "SN5800_LD",
        "x86_64-nvidia_sn6600_simx-r0": "SN6600_LD",
        "x86_64-nvidia_sn6600_ld_simx-r0": "SN6600_LD",
        "x86_64-nvidia_sn6810_ld_simx-r0": "SN6810_LD"
    }


class SimxCommunityConsts:
    PTF_IP = "192.168.200.11"
    SERVER_DOCKER_IP = "172.17.0.1"
    ANSIBLE_DIR = os.path.join(MarsConstants.SONIC_MGMT_DIR, "ansible")
    ANSIBLE_HWSKU_VARS_PATH = os.path.join(ANSIBLE_DIR, "files", "hwsku_vars")
    SIMX_COMMUNITY_FILES_PATH = os.path.join(ANSIBLE_HWSKU_VARS_PATH, "simx_community")
    COMMON_FILES_PATH = os.path.join(SIMX_COMMUNITY_FILES_PATH, "files")
    HYPERVISOR_FILE_NAME = "HYPERVISOR.j2"
    HOST_VARS_PATH = os.path.join(ANSIBLE_DIR, "host_vars")
    FILES_TO_TEMPLATE = {'sonic_nvidia_devices.j2', 'sonic_nvidia_links.j2', 'fanout_port_config.ini', 'lab.j2', 'testbed.j2', 'inventory.j2'}
    # HwSKU -> simx_community platform subdir when different from filtered_platform (e.g. V448P16S2 uses SN6600_LD)
    HWSKU_PLATFORM_DIR_OVERRIDE = {"Mellanox-SN6600-V448P16S2": "SN6600_LD"}


class TxSquelchConsts:
    SAI_TX_SQUELCH_MODE_KV = "SAI_KEY_TX_SQUELCH_MODE"

    SUPPORTED_HWSKUS = frozenset({
        "Mellanox-SN5640-C448O16",
        "Mellanox-SN5640-C512S2",
        "Mellanox-SN6600_LD-P128C2",
        "Mellanox-SN6600_LD-P64O128C2"
    })

    # KV mode values: 0=AUTO, 1=ENABLE, 2=DISABLE.
    TX_SQUELCH_MODE_AUTO = "0"
    TX_SQUELCH_MODE_ENABLE = "1"
    TX_SQUELCH_MODE_DISABLE = "2"
    ALL_TX_SQUELCH_MODES = [
        TX_SQUELCH_MODE_ENABLE,
        TX_SQUELCH_MODE_DISABLE,
        TX_SQUELCH_MODE_AUTO
    ]

    # Expected SDK string per KV mode.
    KV_TO_SQUELCH_STR = {
        TX_SQUELCH_MODE_ENABLE: "enabled",
        TX_SQUELCH_MODE_DISABLE: "disabled",
        TX_SQUELCH_MODE_AUTO: "enabled"
    }


class SonicMgmtChangeConstants:
    BRANCH_PREFIX = "branch_"
    TAG_PREFIX = "tag_"
