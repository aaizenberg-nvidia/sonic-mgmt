from ngts.nvos_constants.constants_nvos import ClusterAppsLogLevels, NvosConst
import re
from typing import Dict, List, Tuple


class ClusterConsts:
    NMX_CONTROLLER = 'nmx-controller'
    NMX_TELEMETRY = 'nmx-telemetry'
    NMX_CONTROLLER_ENVOY_PORT = 9370
    NV_BRIDGE_PORT = 50052  # nv-bridge port for cluster communication
    INITIAL_EXPECTED_APPS = [NMX_CONTROLLER, NMX_TELEMETRY]
    START_APP_WHILE_CLUSTER_DISABLED_ERR_MSG = 'Output was expected to contain:\nAction succeeded\nBut the output is:\nAction executing ...\nError: Action failed with the following issue:\n  cluster is not enabled'
    TELEMETRY_SERVICES = ['nmx-telemetry']
    CONTROLLER_SERVICES = ['nmxc-rel', 'redis']
    ClusterAppsLogLevelsList = [ClusterAppsLogLevels.DEBUG, ClusterAppsLogLevels.INFO, ClusterAppsLogLevels.NOTICE, ClusterAppsLogLevels.WARNING, ClusterAppsLogLevels.ERROR, ClusterAppsLogLevels.CRITICAL]
    NMX_CONTROLLER_CONFIG_FILE_TYPES = ['fm_config', 'sm_config', 'rdm_config', 'chassis_mapping']
    NMX_CONTROLLER_STATE_FILE_TYPES = ['topology', 'partition-report', 'nv-bridge-state-report']
    NMX_TELEMETRY_CONFIG_FILE_TYPES = ['telemetry']  # Once added make sure to adjust CONFIG_FILES_CHANGE
    NMX_TELEMETRY_STATE_FILE_TYPES = []
    CONTROLLER_AND_TELEMETRY_CONFIG_FILES = NMX_CONTROLLER_CONFIG_FILE_TYPES + NMX_TELEMETRY_CONFIG_FILE_TYPES
    CONTROLLER_AND_TELEMETRY_STATE_FILES = NMX_CONTROLLER_STATE_FILE_TYPES + NMX_TELEMETRY_STATE_FILE_TYPES
    MAP_CONFIG_FILE_TYPE_TO_APP = {}
    MAP_CONFIG_FILE_TYPE_TO_APP.update({file_type: 'nmx-controller' for file_type in ['fm_config', 'sm_config', 'rdm_config', 'chassis_mapping']})
    MAP_CONFIG_FILE_TYPE_TO_APP.update({file_type: 'nmx-telemetry' for file_type in ['telemetry']})
    MAP_STATE_FILE_TYPE_TO_APP = {}
    MAP_STATE_FILE_TYPE_TO_APP.update({file_type: 'nmx-controller' for file_type in ['topology', 'partition-report', 'nv-bridge-state-report']})
    MAP_STATE_FILE_TYPE_TO_APP.update({file_type: 'nmx-telemetry' for file_type in []})
    NMX_LOG_MESSAGES_TAGS = ['nmxc-sm', 'nmxc-fm', 'nmxc-fib', 'nmxc-gw_api', 'nmxc-rest', 'nmxc-config_daemon']
    INITIAL_CONFIGURATIONS_PATH = '/auto/sw_system_project/NVOS_INFRA/verification_files/cluster/uploaded_control_plane_files'
    UNDEFINED_STATE = 'undefined'
    CONTROL_PLANE_STATE_CONFIGURED = 'CONTROL_PLANE_STATE_CONFIGURED'
    UNDEFINED_STATE_ERR_MSG_NVUE = "'undefined' is not one of ['enabled', 'disabled']"
    UNDEFINED_STATE_ERR_MSG_OPENAPI = 'Error: Request failed. Details: Error: \'undefined\' is not one of [\'enabled\', \'disabled\', None]'
    UNDEFINED_STATE_DICT = {'NVUE': UNDEFINED_STATE_ERR_MSG_NVUE, 'OpenApi': UNDEFINED_STATE_ERR_MSG_OPENAPI}
    CLUSTER_NOT_ENABLED_ERR = 'cluster is not enabled'

    RESET_FACTORY_CLUSTER_DISABLED_NVUE = 'Error: Action failed with the following issue:\n  cluster is not enabled'

    RESET_FACTORY_NMX_CONN_DISABLED_NVUE = 'Error: Action failed with the following issue:\n  gRPC connection is down'

    RESET_FACTORY_CLUSTER_DISABLED_OPENAPI = 'action_error: cluster is not enabled'

    RESET_FACTORY_NMX_CONN_DISABLED_OPENAPI = 'action_error: gRPC connection is down'

    RESET_FACTORY_CLUSTER_DISABLED = {'NVUE': RESET_FACTORY_CLUSTER_DISABLED_NVUE, 'OpenApi': RESET_FACTORY_CLUSTER_DISABLED_OPENAPI}
    RESET_FACTORY_NMX_CONN_DISABLED = {'NVUE': RESET_FACTORY_NMX_CONN_DISABLED_NVUE, 'OpenApi': RESET_FACTORY_NMX_CONN_DISABLED_OPENAPI}

    STATE_UNKNOWN = 'unknown'
    NMXC_CONN = 'nmxc-conn'
    NMXC_CONN_STATE_PER_CLUSTER_STATE = {NvosConst.ENABLED: 'up', NvosConst.DISABLED: 'down'}
    WAIT_FOR_APPS_RUNNING = 50  # Reduce to 15 once bug is fixed [NVOS - Design] Bug SW #4099507: [Non-Functional ] [NVL5 - JULIET - NMX] | nmxc-conn takes too long to be in "up" state | Assignee: Or Farfara | Status: Opened on other team
    UNDEFINED_STATE_ERR_MSG = 'Error: At state: \'undefined\' is not one of [\'enabled\', \'disabled\']'
    DEFAULT_LOG_LEVEL = 'notice'
    UNDEFINED_LOG_LEVEL = "'undefined' is not one of ['critical', 'error', 'warn', 'notice', 'info', 'debug']"
    SLEEP_AFTER_LOG_ROTATE = 20
    PARTITIONS_NAMES = ['test_partition1', 'test_partition2', 'test_partition3']
    RESILIENCY_MODES = ['adaptive_bandwidth', 'full_bandwidth', 'user_action_required']
    CONFIDENTIAL_COMPUTE = [True, False]
    DEFAULT_PARTITION = 1
    APP_VERSION = 'app-ver'
    APP_NAME = 'app-name'
    NMX_CONTROLLER_PREFIX = 'nmx-c'
    NMX_TELEMETRY_PREFIX = 'nmx-t'
    INITIAL_APPS_PATH = '/usr/local/cluster_pkgfiles/'
    INFRA_PACKAGES_PATH = '/host/cluster_infra/packages/'
    # Valid rdm_config test content (approved by the nmx-c team; changes multicast rounding mode,
    # so it is limited to upgrade/reset flows). nmx-c rejects installing EMPTY sdn config files
    # (NVBug 6447101), so the rdm/chassis test edits below must inject real content.
    RDM_TEST_CONFIG_LINES = ['[rdm]',
                             'rdm_rm_native_8_bit 1',
                             'rdm_rm_mixed_8_bit 1',
                             'rdm_rm_native_16_bit 1',
                             'rdm_rm_mixed_16_bit 1',
                             'rdm_rm_deterministic_mode 1']
    # chassis_mapping line format: 'chassisId<index> <13-char REAL chassis S/N>' - the S/N must be
    # the system's real one (nv show platform chassis-location), otherwise GFM never goes operational.
    # Call sites must format with chassis_sn=ClusterTools.get_chassis_sn().
    CONFIG_FILES_CHANGE = {'sm_config': "true",
                           'fm_config': "sudo sed -i \"/^GFM_WAIT_TIMEOUT_SEC=/c\\GFM_WAIT_TIMEOUT_SEC=350\" {file_path}",
                           # guarded append: at most once, and never onto a file that already carries [rdm]
                           'rdm_config': "sudo bash -c 'grep -q \"^\\[rdm\\]\" {file_path} || printf \"" + '\\n'.join(RDM_TEST_CONFIG_LINES) + "\\n\" >> {file_path}'",
                           # append only when the file has no chassisId entries (standalone generates it
                           # empty - NVBug 6447101); on real clusters the generated mapping already has
                           # chassisId1..N lines and a duplicate chassisId1 would corrupt the mapping
                           'chassis_mapping': "sudo bash -c 'grep -q \"^chassisId\" {file_path} || echo \"chassisId1 {chassis_sn}\" >> {file_path}'",
                           'telemetry': "true"}
    EXPECTED_LINE_TO_BE_PRESERVED_AFTER_UPGRADE = {'sm_config': "",
                                                   'fm_config': "GFM_WAIT_TIMEOUT_SEC=350",
                                                   'rdm_config': RDM_TEST_CONFIG_LINES[-1],
                                                   'chassis_mapping': "chassisId1",
                                                   'telemetry': ""}
    CONFIG_FILES_CONTENT_CHANGE = {
        'sm_config': lambda content: content,
        'fm_config': lambda content: re.sub(r'^GFM_WAIT_TIMEOUT_SEC=.*$', 'GFM_WAIT_TIMEOUT_SEC=350', content, flags=re.MULTILINE),
        # newline-neutral: mirrors the cat output of the appended file (no leading blank line when the
        # initial content is empty, no trailing newline - netmiko strips the trailing prompt line)
        'rdm_config': lambda content: ((content + '\n') if content.strip() else '') + '\n'.join(ClusterConsts.RDM_TEST_CONFIG_LINES),
        # chassis_mapping expected content needs the runtime chassis S/N; the content verifier
        # currently skips chassis_mapping, so the transformation stays identity.
        'chassis_mapping': lambda content: content,
        'telemetry': lambda content: content
    }
    NMX_CONTROLLER_CONFIG_CHASSIS_MAPPING = 'chassis_mapping'
    PARTITION_TYPES = ['location_based', 'gpuuid_based']
    EMPTY_PARTITION_ID = '10'
    EMPTY_PARTITION_NAME = "empty_partition"
    CREATED_PARTITION_NAME = "user_partition"
    MIN_MCAST = 0
    MAX_MCAST = 0  # Change to 1024 once bug is closed.
    PROTOCOL_RSYSLOG = "rsyslog"
    PROTOCOL_ELK = "elk"
    PROTOCOL_SPLUNK = "splunk"
    NMXC_LOG_STREAM_PROTOCOLS = [PROTOCOL_RSYSLOG, PROTOCOL_ELK, PROTOCOL_SPLUNK]
    NMXC_LOG_STREAM_DEFAULT_PORT = "514"
    NMXC_LOG_STREAM_PORT = {PROTOCOL_RSYSLOG: "514", PROTOCOL_ELK: "6001", PROTOCOL_SPLUNK: "8088"}
    CONTROLLER_LOG_STREAM_CONFIG_FILE = "rsyslog.conf"
    CONTROLLER_LOG_STREAM_SERVICE = "rsyslog"

    # maintenance state constants
    MAINTENANCE_STATE = 'maintenance-state'
    MAINTENANCE_STATE_OPTIONS = ['up', 'diag', 'down']

    # DSCP Marking constants
    DSCP_MARKING_FIELD = 'dscp-marking'
    DSCP_DEFAULT_VALUE = 46
    DSCP_MIN_VALUE = 0
    DSCP_MAX_VALUE = 63

    # DSCP enum mapping: name -> (numeric_value, description)
    DSCP_ENUM_MAP: Dict[str, Tuple[int, str]] = {
        'be': (0, 'Best Effort'),
        'cs1': (8, 'Class Selector 1'),
        'af11': (10, 'Assured Forwarding 11'),
        'af12': (12, 'Assured Forwarding 12'),
        'af13': (14, 'Assured Forwarding 13'),
        'cs2': (16, 'Class Selector 2'),
        'af21': (18, 'Assured Forwarding 21'),
        'af22': (20, 'Assured Forwarding 22'),
        'af23': (22, 'Assured Forwarding 23'),
        'cs3': (24, 'Class Selector 3'),
        'af31': (26, 'Assured Forwarding 31'),
        'af32': (28, 'Assured Forwarding 32'),
        'af33': (30, 'Assured Forwarding 33'),
        'cs4': (32, 'Class Selector 4'),
        'af41': (34, 'Assured Forwarding 41'),
        'af42': (36, 'Assured Forwarding 42'),
        'af43': (38, 'Assured Forwarding 43'),
        'cs5': (40, 'Class Selector 5'),
        'ef': (46, 'Expedited Forwarding'),
        'cs6': (48, 'Class Selector 6'),
        'cs7': (56, 'Class Selector 7'),
    }

    # Reverse mapping: numeric_value -> enum_name
    DSCP_NUMERIC_TO_ENUM: Dict[int, str] = {v[0]: k for k, v in DSCP_ENUM_MAP.items()}

    # List of all enum names
    DSCP_ENUM_NAMES: List[str] = list(DSCP_ENUM_MAP.keys())


class NmxTelemetryConsts:
    # docker name
    NMX_TELEMETRY_DOCKER_NAME = "nmx-t.telemetry.telemetry"

    # health
    STATUS_OK = 'ok'
    STATUS_NOT_OK = 'not ok'
    STATUS_HEALTHY = 'Healthy'

    # messages
    NMX_T_AGENT_STOPPED_MESSAGE = "Telemetry agent stopped successfully"
    NMX_T_AGENT_STARTED_MESSAGE = "Telemetry agent started successfully"
    CHANGE_AGENT_CONFIG_ERR_MESSAGE = "Cannot change telemetry agent configuration when cluster state is enabled"

    AGENTS = 'agents'


class AnsiblePlaybooksConsts:
    """
    Constants for NEW nvidia.nvlink and nvidia_internal.nvlink Ansible collections.

    Architecture:
        - Playbook-centric (each playbook defines what components it needs)
        - No YAML config file manipulation - all params via -e
        - Clean separation of concerns

    Migration: Replaces old NVlinkClusterManagement collection approach
    """

    # =========================================================================
    # Ansible Server Configuration
    # =========================================================================
    USER = 'user'
    PASS = 'pass'
    ANSIBLE_MACHINES = ['10.237.246.70']
    ANSIBLE_MACHINES_CREDENTIALS = {
        '10.237.246.70': {USER: NvosConst.ROOT_USER, PASS: NvosConst.ROOT_PASSWORD}
    }

    # =========================================================================
    # Ansible Collection Paths (NEW)
    # =========================================================================
    BASE_ANSIBLE_PATH = '/root/.ansible/collections/ansible_collections/'
    PATH_TO_NVIDIA_NVLINK = f'{BASE_ANSIBLE_PATH}nvidia/nvlink/'
    PATH_TO_NVIDIA_INTERNAL_NVLINK = f'{BASE_ANSIBLE_PATH}nvidia_internal/nvlink/'

    # =========================================================================
    # Default Inventory Path
    # =========================================================================
    DEFAULT_INVENTORY_PATH = '/auto/sw_system_project/NVOS_INFRA/verification_files/inventory_files/israel-cluster.yml'

    # =========================================================================
    # Playbook Keys (for type-safe playbook identification)
    # =========================================================================
    SOFTWARE_INSTALL = 'SOFTWARE_INSTALL'
    FIRMWARE_BMC = 'FIRMWARE_BMC'
    FIRMWARE_CPLD = 'FIRMWARE_CPLD'
    FIRMWARE_HMC = 'FIRMWARE_HMC'
    TESTS_INSTALL_CUDA = 'TESTS_INSTALL_CUDA'
    INSTALL_CUDA_TOOLKIT = 'INSTALL_CUDA_TOOLKIT'
    STATUS_HEALTH = 'STATUS_HEALTH'
    SOFTWARE_CONFIGURE_SWITCH = 'SOFTWARE_CONFIGURE_SWITCH'
    TESTS_EXECUTE = 'TESTS_EXECUTE'
    TESTS_EXECUTE_P2P = 'TESTS_EXECUTE_P2P'

    # =========================================================================
    # Component Keys (from JSON file)
    # =========================================================================
    RM_VERSION = 'rm_version'
    IMEX_VERSION = 'imex_version'  # NEW: IMEX is now separate
    NVFWUPD = 'nvfwupd'  # Firmware update tool
    COMPUTE_BMC = 'compute_bmc'
    COMPUTE_CPLD = 'compute_cpld'
    COMPUTE_HMC = 'compute_hmc'
    CUDA_PACKAGE = 'cuda_package'
    CUDA_TOOLKIT = 'cuda_toolkit'  # CUDA toolkit offline repo

    # For backward compatibility with old code
    COMPONENTS = [RM_VERSION, COMPUTE_BMC, COMPUTE_CPLD, COMPUTE_HMC, CUDA_PACKAGE]

    # Old config file mappings (no longer used, but kept for reference)
    CONFIG_FILE = '~/.ansible/user_config_file.yml'
    BUILD_VERSION = 'build_version'
    COMPUTE_NODE_FW_PATH = 'compute_node_fw_path'
    CUDA_AARCH64_DOWNLOAD_URL = 'cuda_aarch64_download_url'
    CONFIG_FILE_UPDATE_PER_COMPONENT = {
        RM_VERSION: BUILD_VERSION,
        COMPUTE_BMC: COMPUTE_NODE_FW_PATH,
        COMPUTE_CPLD: COMPUTE_NODE_FW_PATH,
        COMPUTE_HMC: COMPUTE_NODE_FW_PATH,
        CUDA_PACKAGE: CUDA_AARCH64_DOWNLOAD_URL
    }
    PLAYBOOKS_ARGUMENTS = {
        RM_VERSION: '-vvv',
        COMPUTE_BMC: '-vvv',
        COMPUTE_CPLD: '-vvv',
        COMPUTE_HMC: '-vvv',
        CUDA_PACKAGE: "--skip-tags 'cuda_rm_assert' -vvv"
    }
    PLAYBOOKS_NAMES = {
        RM_VERSION: 'provision_compute_node_software_nvl5.yml',
        COMPUTE_BMC: 'provision_compute_node_firmware_bmc.yml',
        COMPUTE_CPLD: 'provision_compute_node_firmware_cpld.yml',
        COMPUTE_HMC: 'provision_compute_node_firmware_hmc.yml',
        CUDA_PACKAGE: "install_cuda_tests.yml"
    }

    # =========================================================================
    # Playbook Definitions (NEW: Playbook-centric approach)
    # =========================================================================

    # Each playbook entry contains:
    #   'name': playbook filename
    #   'collection_path': path to collection
    #   'component_param_mapping': list of dicts showing EXPLICIT bond between:
    #       - 'component': JSON key (e.g., 'rm_version')
    #       - 'provisioning': which section to extract from ('prod' or 'dev')
    #       - 'param': parameter name for -e flag
    #   'extra_args': additional arguments (like --skip-tags)

    # Per-setup playbook filenames (same collection_path and component_param_mapping as PLAYBOOKS base entries).
    # Keys: ALIGNMENT_PLAYBOOKS_ORDER setup names. Inner keys: playbook_key when filename differs from default.
    PLAYBOOK_FILENAME_PER_SETUP: Dict[str, Dict[str, str]] = {
        'NVOS_juliet_10_7_148_148': {
            FIRMWARE_BMC: 'firmware_install_compute_bmc.yml',
        },
        'NVOS_sws_rtf2_rosalind_198': {
            FIRMWARE_BMC: 'bmc_powerctrl_firmware_install_compute_bmc.yml',
        },
    }

    PLAYBOOKS = {
        SOFTWARE_INSTALL: {
            'name': 'software_install_run_compute.yml',
            'collection_path': PATH_TO_NVIDIA_NVLINK,
            'component_param_mapping': [
                # RM driver from dev section
                {'component': RM_VERSION, 'provisioning': 'dev', 'param': 'rm_run_path'},
                # IMEX from dev section
                {'component': IMEX_VERSION, 'provisioning': 'dev', 'param': 'imex_run_path'}
            ],
            'extra_args': ''
        },
        FIRMWARE_BMC: {
            'name': 'firmware_install_compute_bmc.yml',
            'collection_path': PATH_TO_NVIDIA_INTERNAL_NVLINK,
            'component_param_mapping': [
                # Tool path from prod section (shared, but needs to be in JSON)
                {'component': NVFWUPD, 'provisioning': 'prod', 'param': 'nvfwupd_cli_path'},
                # Prod-signed BMC from prod section → compute_bmc_prod_signed_fwpkg_path
                {'component': COMPUTE_BMC, 'provisioning': 'prod', 'param': 'compute_bmc_prod_signed_fwpkg_path'},
                # Debug-signed BMC from dev section → compute_bmc_debug_signed_fwpkg_path
                {'component': COMPUTE_BMC, 'provisioning': 'dev', 'param': 'compute_bmc_debug_signed_fwpkg_path'}
            ],
            'extra_args': '-e force_update=true'
        },
        FIRMWARE_CPLD: {
            'name': 'firmware_install_compute_cpld.yml',
            'collection_path': PATH_TO_NVIDIA_INTERNAL_NVLINK,
            'component_param_mapping': [
                # Tool path from prod section (shared, but needs to be in JSON)
                {'component': NVFWUPD, 'provisioning': 'prod', 'param': 'nvfwupd_cli_path'},
                # Prod-signed CPLD from prod section → compute_cpld_prod_signed_fwpkg_path
                {'component': COMPUTE_CPLD, 'provisioning': 'prod', 'param': 'compute_cpld_prod_signed_fwpkg_path'},
                # Debug-signed CPLD from dev section → compute_cpld_debug_signed_fwpkg_path
                {'component': COMPUTE_CPLD, 'provisioning': 'dev', 'param': 'compute_cpld_debug_signed_fwpkg_path'}
            ],
            'extra_args': '-e force_update=true'
        },
        FIRMWARE_HMC: {
            'name': 'firmware_install_compute_hmc.yml',
            'collection_path': PATH_TO_NVIDIA_INTERNAL_NVLINK,
            'component_param_mapping': [
                # Tool path from prod section (shared, but needs to be in JSON)
                {'component': NVFWUPD, 'provisioning': 'prod', 'param': 'nvfwupd_cli_path'},
                # Prod-signed HMC from prod section → compute_hmc_prod_signed_fwpkg_path
                {'component': COMPUTE_HMC, 'provisioning': 'prod', 'param': 'compute_hmc_prod_signed_fwpkg_path'},
                # Debug-signed HMC from dev section → compute_hmc_debug_signed_fwpkg_path
                {'component': COMPUTE_HMC, 'provisioning': 'dev', 'param': 'compute_hmc_debug_signed_fwpkg_path'}
            ],
            'extra_args': '-e force_update=true'
        },
        TESTS_INSTALL_CUDA: {
            'name': 'tests_install_cuda.yml',
            'collection_path': PATH_TO_NVIDIA_INTERNAL_NVLINK,
            'component_param_mapping': [
                # NOTE: Provisioning section will be dynamically set based on system type
                # For now using 'dev' - could be made dynamic based on switch BIOS version
                {'component': CUDA_PACKAGE, 'provisioning': 'dev', 'param': 'cuda_aarch64_tar_path'}
            ],
            'extra_args': '-e fail_on_rm_mismatch=false'
        },
        INSTALL_CUDA_TOOLKIT: {
            'name': 'install_cuda_toolkit_offline_repo.yml',
            'collection_path': PATH_TO_NVIDIA_NVLINK,
            'component_param_mapping': [
                # CUDA toolkit offline repo from dev section
                {'component': CUDA_TOOLKIT, 'provisioning': 'dev', 'param': 'cuda_toolkit_offline_repo_path'}
            ],
            'extra_args': ''
        },
        TESTS_EXECUTE: {
            'name': 'tests_execute_cuda.yml',
            'collection_path': PATH_TO_NVIDIA_INTERNAL_NVLINK,
            'component_param_mapping': [],  # No components - just runs tests
            'extra_args': ''  # Runs all tests
        },
        TESTS_EXECUTE_P2P: {
            'name': 'tests_execute_cuda.yml',
            'collection_path': PATH_TO_NVIDIA_INTERNAL_NVLINK,
            'component_param_mapping': [],  # No components - just runs tests
            'extra_args': '-e \'{"cuda_test_list":["p2p_bandwidth"]}\''  # Runs only p2p_bandwidth test
        },
        SOFTWARE_CONFIGURE_SWITCH: {
            'name': 'software_configure_switch.yml',
            'collection_path': PATH_TO_NVIDIA_NVLINK,
            'component_param_mapping': [],  # No components - just checks status
            'extra_args': ''  # No extra args for health check
        },
        STATUS_HEALTH: {
            'name': 'status_health_check.yml',
            'collection_path': PATH_TO_NVIDIA_NVLINK,
            'component_param_mapping': [],  # No components - just checks status
            'extra_args': ''  # No extra args for health check
        }
    }

    # =========================================================================
    # Execution Order for Full Rack Alignment
    # =========================================================================
    ALIGNMENT_PLAYBOOKS_ORDER = {"NVOS_juliet_10_7_148_148": [
        SOFTWARE_INSTALL,  # RM + IMEX
        FIRMWARE_BMC,  # BMC firmware
        FIRMWARE_CPLD,  # CPLD firmware
        FIRMWARE_HMC,  # HMC firmware
        TESTS_INSTALL_CUDA,  # Install CUDA tests
        INSTALL_CUDA_TOOLKIT,  # Install CUDA toolkit
        STATUS_HEALTH  # Health check
    ],
        "NVOS_sws_rtf2_rosalind_198": [
        SOFTWARE_INSTALL,  # RM + IMEX
        FIRMWARE_BMC,  # BMC firmware
        FIRMWARE_HMC,  # HMC firmware
        TESTS_INSTALL_CUDA,  # Install CUDA tests
        INSTALL_CUDA_TOOLKIT,  # Install CUDA toolkit
        STATUS_HEALTH  # Health check
    ]}
    # NOTE: Traffic tests run separately via test_cluster_traffic.py

    # =========================================================================
    # Download Configuration
    # =========================================================================
    # NOTE: Download logic is now AUTO-DETECTED based on path!
    # - Paths starting with http:// or https:// → downloaded to /tmp
    # - Other paths → used directly as local files

    DOWNLOAD_TEMP_DIR = '/tmp'
    DOWNLOAD_TIMEOUT_SECONDS = 1800  # 30 minutes
    DOWNLOAD_MAX_RETRIES = 3
    DOWNLOAD_RETRY_DELAY_SECONDS = 10

    # =========================================================================
    # Helper Methods
    # =========================================================================

    @classmethod
    def _playbook_filename(cls, playbook_info, playbook_key, setup_name):
        """Resolve playbook YAML filename, including per-setup overrides."""
        name = playbook_info['name']
        per_setup = cls.PLAYBOOK_FILENAME_PER_SETUP.get(setup_name, {})
        name = per_setup.get(playbook_key, name)
        return name

    @classmethod
    def get_playbook_command(cls, playbook_key, inventory_path, component_paths_dict, setup_name):
        """
        Build complete playbook command.

        NOTE: Playbooks are directly executable (have shebang), so NO 'ansible-playbook' prefix!

        Args:
            playbook_key: Key from PLAYBOOKS dict (e.g., 'SOFTWARE_INSTALL')
            inventory_path: Path to inventory file
            component_paths_dict: Dict of {param_name: file_path}
                Example: {'rm_run_path': '/tmp/driver.run', 'imex_run_path': '/tmp/imex.run'}
            setup_name: Optional lab/setup id (e.g. NVOS_sws_rtf2_rosalind_198) for playbook filename overrides

        Returns:
            Complete command string ready to execute

        Example:
            >>> get_playbook_command('SOFTWARE_INSTALL', '/path/inv.yml',
            ...     {'rm_run_path': '/tmp/driver.run', 'imex_run_path': '/tmp/imex.run'})
            '/root/.../software_install_run_compute.yml -i /path/inv.yml -e "rm_run_path=/tmp/driver.run imex_run_path=/tmp/imex.run" -vvv'
        """
        playbook_info = cls.PLAYBOOKS[playbook_key]
        filename = cls._playbook_filename(playbook_info, playbook_key, setup_name)

        # Build full playbook path (playbooks are directly executable)
        playbook_path = f"{playbook_info['collection_path']}playbooks/{filename}"

        # Start command
        cmd = f"ansible-playbook {playbook_path} -i {inventory_path}"

        # Add parameters if any
        if component_paths_dict:
            param_pairs = [f"{param}={path}" for param, path in component_paths_dict.items()]
            params_str = " ".join(param_pairs)
            cmd += f' -e "{params_str}"'

        # Add extra args
        if playbook_info['extra_args']:
            cmd += f" {playbook_info['extra_args']}"

        return cmd

    @classmethod
    def get_component_mappings(cls, playbook_key):
        """
        Get component-to-param mappings for a playbook.

        This makes it easy to iterate and extract the right components
        from the right provisioning sections.

        Args:
            playbook_key: Key from PLAYBOOKS dict

        Returns:
            List of dicts with 'component', 'provisioning', and 'param' keys

        Example:
            >>> mappings = get_component_mappings('FIRMWARE_HMC')
            >>> for m in mappings:
            ...     print(f"{m['param']} ← json['{m['provisioning']}']['{m['component']}']")
            compute_hmc_prod_signed_fwpkg ← json['prod']['compute_hmc']
            compute_hmc_debug_signed_fwpkg ← json['dev']['compute_hmc']
        """
        return cls.PLAYBOOKS[playbook_key]['component_param_mapping']
