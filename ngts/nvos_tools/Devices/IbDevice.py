import logging
import math
import os
import time
from collections import namedtuple

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.linux_tools.linux_tools import scp_file

from ngts.nvos_constants.constants_nvos import (
    ActionConsts,
    CableCartridgeConsts,
    ChassisLocationConsts,
    DatabaseConst,
    DocumentsConsts,
    FansConsts,
    HealthConsts,
    IbConsts,
    ImageConsts,
    LogComponentsConsts,
    MultiPlanarConsts,
    NvosConst,
    OperationTimeConsts,
    PlatformConsts,
    RebootConsts,
    SSDConsts,
    SyslogConsts,
    SystemConsts,
    TcpDumpConsts,
)
from ngts.nvos_tools.Devices.BaseDevice import BaseSwitch
from ngts.nvos_tools.Devices.SwitchCapabilities import (
    CpoCapability,
    NoPSUCapability,
    PortiaCpoCapability,
    SwitchCapabilityHandler,
)
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import (
    IbInterfaceConsts,
    InternalNvosConsts,
    PhyRecoveryConsts,
)
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.FilesTool import FilesTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.TrafficValidatorTool import TrafficErrorCounters
from ngts.nvos_tools.infra.ValidationTool import ExpectedString
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.Spdm import SPDMComponents
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.constants import (
    FW_COMPONENT_ASIC,
    FW_COMPONENT_BIOS,
    FW_COMPONENT_BMC,
    FW_COMPONENT_CPLD,
    FW_COMPONENT_EROT,
    FW_COMPONENT_FPGA,
    FW_COMPONENT_SMA,
    FW_COMPONENT_SSD,
    MINUTE,
)
from ngts.tests_nvos.general.post_upgrade_switch.constants import InstallSteps
from ngts.tests_nvos.general.security.nmx_cert.constants import (
    ALTERNATE_CERTIFICATE,
    CA_CERTIFICATE,
    CERTIFICATE,
    CRL,
    ENCRYPTION,
    ENDPOINT,
    PROTOCOL,
    REMOTE_URL,
    Defaults,
)
from ngts.tests_nvos.general.security.security_test_tools.constants import AaaConsts
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.system.gnmi.constants import GnmiConstants
from ngts.tests_nvos.system.stats.constants import StatsConsts
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_config_utils import clear_conf
from ngts.tools.test_utils.nvos_general_utils import get_version_info

logger = logging.getLogger(__name__)


class IbSwitch(BaseSwitch):
    ErotFirmwareImagesTestConsts = namedtuple('ErotFirmwareImagesTestConsts',
                                              ('previous_image_path', 'current_image_path', 'version_names'))

    def __init__(self, asic_amount, switch_type=NvosConst.IB_SWITCH_TYPE, switch_class=NvosConst.IB_SWITCH_TYPE):
        super().__init__(switch_type=switch_type, asic_amount=asic_amount, switch_class=switch_class)
        self.port_type = self.switch_type.lower()
        self.supports_mtu_testing = True
        self.single_fw_pkg_supported = False
        self.documents_path = None
        self.documents_files = None
        self._init_sensors_dict()
        self._init_gnmi_consts()
        self.open_api_port = "443"
        self.default_password = os.environ["NVU_SWITCH_NEW_PASSWORD"]
        self.default_username = os.environ["NVU_SWITCH_USER"]
        self.prev_default_password = os.environ["NVU_SWITCH_PASSWORD"]
        self._init_ib_speeds()
        self._init_interfaces_ib_lanes()
        self._init_eth0_speeds()
        self._init_eth0_duplex()
        self.init_documents_consts()
        self.init_cli_coverage_prop("nvos")
        self._init_interface_lists()
        self._init_interface_attributes_mapping_dict()
        self._init_link_error_counters()

    def _init_expected_operation_durations(self):
        super()._init_expected_operation_durations()
        self.expected_operation_durations.update({
            # The synchronous "nv action install" portion (before reboot/recovery).
            OperationTimeConsts.IMAGE_INSTALL_OPERATION_ROW: 120,
        })

    def get_health_issue_dict_fan_and_psu(self, psu_display_name, fan_display_name):
        """Return expected health issues dict for simulated fan+PSU fault (IB)."""
        return {
            psu_display_name: ["missing or not available", "missing - Unpopulated PSU slot"],
            fan_display_name: "not working",
        }

    def _relevant_config_filename_by_version(self, version: str) -> str:
        global_build = self._version_to_global_build(version)
        if global_build:
            relevant_ga_num = math.ceil(global_build / 1000) * 1000
            return f'nvos_config_ga_{relevant_ga_num}.yml'
        return super()._relevant_config_filename_by_version(version)

    def get_test_config_file_by_version(self, version: str):
        config_file_path, config_filename = super().get_test_config_file_by_version(version)
        if os.path.isfile(config_file_path):
            return config_file_path, config_filename

        global_build = self._version_to_global_build(version)
        if not global_build:
            raise FileNotFoundError(f"Test config file not found: {config_file_path}")

        ngts_path = os.path.join(os.path.abspath(__file__).split('ngts', 1)[0], 'ngts')
        resources_dir = os.path.join(ngts_path, 'tools', 'test_utils', 'nvos_resources')

        import glob
        ga_files = glob.glob(os.path.join(resources_dir, 'nvos_config_ga_*.yml'))
        ga_numbers = []
        for f in ga_files:
            ga_str = os.path.basename(f).replace('nvos_config_ga_', '').replace('.yml', '')
            if ga_str.isdigit():
                ga_numbers.append(int(ga_str))

        if not ga_numbers:
            raise FileNotFoundError(f"No GA config files found in {resources_dir}")

        # Round up global_build to the next GA boundary (multiple of 1000),
        # then pick the lowest available GA config at or above that boundary.
        # Falls back to the highest available GA if none are higher.
        # Examples (available GAs: [7000, 8000]):
        #   version=25.03.0106-035 -> global_build=8106 -> target_ga=9000 -> no match -> closest_ga=8000 (fallback)
        #   version=25.02.7001     -> global_build=7001 -> target_ga=8000 -> closest_ga=8000
        #   version=25.02.7000     -> global_build=7000 -> target_ga=8000 -> closest_ga=8000
        target_ga = (global_build // 1000 + 1) * 1000
        higher_or_equal = [ga for ga in ga_numbers if ga >= target_ga]
        closest_ga = min(higher_or_equal) if higher_or_equal else max(ga_numbers)
        config_filename = f'nvos_config_ga_{closest_ga}.yml'
        config_file_path = os.path.join(resources_dir, config_filename)
        logging.info(f'No exact GA config for global_build={global_build}, using closest: {config_filename}')
        return config_file_path, config_filename

    def get_lldp_port_name_from_dump(self, lldp_dict):
        """Return the port name from parsed LLDP dump; ib uses Port ID TLV."""
        return lldp_dict[TcpDumpConsts.LLDP_PORT_ID]

    def get_upload_prefix(self):
        return f"://{NvosConst.ROOT_USER}:{NvosConst.ROOT_PASSWORD}@{NvosConst.FIT70}/tmp/"

    def get_default_password_by_version(self, version: str):
        version_num, _ = get_version_info(version)
        if self.prev_default_password and version_num:
            logger.info(f'detected version: {version_num}')
            if version_num.startswith('25.01.') and int(version_num.split('.')[-1]) <= 3000:
                logger.info('using prev default password')
                return self.prev_default_password
        logger.info('using regular default password')
        return self.default_password

    def get_voltage_sensors(self, dut_engine=None):
        return Tools.FilesTool.get_subfiles_list(engine=dut_engine, folder_path=PlatformConsts.VOLTAGE_FILES_PATH,
                                                 subfiles_pattern=PlatformConsts.VOLTAGE_FILES_PATTERN)

    def get_default_nvue_config(self, dut_engine=None):
        return {**NvosConst.DEFAULT_CONFIG, "interface": NvosConst.DEFAULT_NVOS_IFACE_CONFIG}

    def show_setup_versions(self, dut_engine: LinuxSshEngine):
        outputs = {
            'system version': dut_engine.run_cmd('nv show system version', timeout=30),
            'platform firmware': dut_engine.run_cmd('nv show platform firmware', timeout=30),
            'fae platform firmware': dut_engine.run_cmd('nv show fae platform firmware', timeout=30),
        }
        res = [f'{title.upper()}:\n{output}\n' for title, output in outputs.items()]
        return '\n'.join(res)

    def verify_ib_ports_state(self, dut_engine, expected_port_state):
        logger.info(f"number of ports: {self.ib_ports_num}")
        output_dict = OutputParsingTool.parse_json_str_to_dictionary(
            Port.show_interface(dut_engine, '--applied')).returned_value
        err_msg = ""
        for key, value in output_dict.items():
            if value[IbInterfaceConsts.TYPE] == IbInterfaceConsts.IB_PORT_TYPE and expected_port_state not in \
                    value[IbInterfaceConsts.LINK][IbInterfaceConsts.DHCP_STATE].keys():
                err_msg += f"{key} state is {value[IbInterfaceConsts.LINK][IbInterfaceConsts.DHCP_STATE].keys()}"

        return ResultObj(False, err_msg) if err_msg else ResultObj(True, "", "")

    def clear_config(self, dut_engine, markers=None, default_yml_path=None, root_dir="", version=""):
        clear_conf(dut_engine, self, default_yml_path, root_dir, version=version)

    def handle_exception(self, dut_engine):
        try:
            logger.info("Handle ib exception")
            dut_engine.run_cmd("docker ps")
            dut_engine.run_cmd("systemctl --type=service")
            dut_engine.run_cmd("nv show system version")
        except BaseException as err:
            logger.warning(err)

    # Subclasses that should use the version-based GA config for clear_config set this to True
    use_ga_default_config = False

    def get_default_config_yml(self, engine, root_dir, version=""):
        try:
            with allure.step("Get default yml file"):
                if version and self.use_ga_default_config:
                    config_file_path, default_config_name = self.get_test_config_file_by_version(version)
                    src_path = config_file_path
                else:
                    default_config_name = NvosConst.DEFAULT_CONFIG_FILE_NAME
                    path_to_config_ymls = f"{root_dir}/{NvosConst.DEFAULT_CONFIG_PATH}"

                    logger.info(f"eth0_ip:{TestToolkit.dut_eth0_ip}")
                    logger.info(f"engine.ip:{engine.ip}")
                    logger.info(f"switch_class:{self.switch_class}")

                    for file_name in os.listdir(path_to_config_ymls):
                        if self.switch_class in file_name:
                            default_config_name = file_name
                        if (TestToolkit.dut_eth0_ip and TestToolkit.dut_eth0_ip in file_name) or (engine.ip in file_name):
                            default_config_name = file_name
                            break
                    src_path = f"{path_to_config_ymls}{default_config_name}"

            yml_file_path = f"{NvosConst.PATH_TO_CONFIG_FILES_ON_DUT}/{default_config_name}"

            if FilesTool.file_exists(engine, yml_file_path):
                logger.info(f"Config file {yml_file_path} already exists on the switch")
            else:
                with allure.step(f"Copy {default_config_name} to the switch"):
                    scp_file(player=engine,
                             src_path=src_path,
                             dst_path=NvosConst.PATH_TO_TMP_ON_DUT,
                             download_from_remote=False, print_output=True)

                tmp_file_path = f"{NvosConst.PATH_TO_TMP_ON_DUT}/{default_config_name}"

                with allure.step(f"Copy {tmp_file_path} to {yml_file_path}"):
                    engine.run_cmd(f"sudo cp {tmp_file_path} {yml_file_path}")

            logger.info(f"Using default yml file: {yml_file_path}")
            return yml_file_path

        except BaseException as ex:
            print(f"SCP command failed - error output: {ex}")
            return ""

    @staticmethod
    def check_fec_capability():
        platform = Platform()
        platform_output = OutputParsingTool.parse_json_str_to_dictionary(platform.show()).get_returned_value()
        asic_model = platform_output['asic-model']
        if "NVLink-6" in asic_model:
            # Platform is FEC capable
            return True
        else:
            # Platform is FEC incapable
            return False

    def _init_ib_speeds(self):
        self.invalid_ib_speeds = {'qdr': '40G'}
        self.supported_ib_speeds = ('hdr', 'edr', 'fdr', 'sdr', 'ndr')
        # Note: FNM speeds set in subclasses that have FNM ports

    def _init_interfaces_ib_lanes(self):
        """Initialize lane configurations for IB ports. Override in subclasses for FNM-specific lanes."""
        self.supported_lanes = '4X'  # Default for regular IB ports
        # Note: FNM lanes set in subclasses that have FNM ports

    def _init_eth0_speeds(self):
        self.supported_eth0_speeds = ['100M', '1G']

    def _init_eth0_duplex(self):
        self.supported_eth0_duplex = ['half', 'full']

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2",
                         "FAN5/1", "FAN5/2", "FAN6/1", "FAN6/2"]

    def _init_led_list(self):
        self.led_list = ['FAN1', 'FAN2', 'FAN3', 'FAN4', 'FAN5', 'FAN6', "PSU_STATUS", "STATUS", "UID"]

    def _init_system_lists(self):
        super()._init_system_lists()
        self.user_fields = ['admin', 'monitor']
        self.log_components_list = LogComponentsConsts.COMPONENTS_LIST

    def _init_security_lists(self):
        super()._init_security_lists()
        self.kex_algorithms = ['curve25519-sha256', 'curve25519-sha256@libssh.org', 'diffie-hellman-group16-sha512',
                               'diffie-hellman-group18-sha512', 'diffie-hellman-group14-sha256', 'kex-strict-s-v00@openssh.com']
        self.aaa_cleanup_cmds = ['nv config detach', 'nv unset system aaa authentication order',
                                 'nv unset system aaa authentication failthrough', 'nv config apply -y']

    def _init_password_hardening_lists(self):
        self.aaa_admin_role = 'admin'
        self.aaa_monitor_role = 'monitor'
        self.local_test_users = [{AaaConsts.USERNAME: AaaConsts.LOCALADMIN,
                                  AaaConsts.PASSWORD: AaaConsts.STRONG_PASSWORD,
                                  AaaConsts.ROLE: self.aaa_admin_role},
                                 {AaaConsts.USERNAME: AaaConsts.LOCALMONITOR,
                                  AaaConsts.PASSWORD: AaaConsts.STRONG_PASSWORD,
                                  AaaConsts.ROLE: self.aaa_monitor_role}]

    def _init_available_databases(self):
        super()._init_available_databases()
        self.available_databases.update(
            {DatabaseConst.APPL_DB_NAME: DatabaseConst.APPL_DB_ID,
             DatabaseConst.ASIC_DB_NAME: DatabaseConst.ASIC_DB_ID,
             # DatabaseConst.COUNTERS_DB_NAME: DatabaseConst.COUNTERS_DB_ID, - disabled for now
             DatabaseConst.CONFIG_DB_NAME: DatabaseConst.CONFIG_DB_ID,
             DatabaseConst.STATE_DB_NAME: DatabaseConst.STATE_DB_ID
             })

        self.available_tables['database'] = {
            DatabaseConst.APPL_DB_ID:
            {"ALIAS_PORT_MAP": self.get_ib_ports_num()},
            DatabaseConst.ASIC_DB_ID:
            {"ASIC_STATE:SAI_OBJECT_TYPE_PORT": self.get_ib_ports_num() + 1,
             "ASIC_STATE:SAI_OBJECT_TYPE_SWITCH": 1,
             "LANES": 1,
             "VIDCOUNTER": 1,
             "RIDTOVID": 1,
             "HIDDEN": 1,
             "COLDVIDS": 1},
            DatabaseConst.COUNTERS_DB_ID:
            {"COUNTERS_PORT_NAME_MAP": 1,
             "COUNTERS:oid": self.get_ib_ports_num()},
            DatabaseConst.CONFIG_DB_ID:
            {"IB_PORT": self.get_ib_ports_num(),
             "FEATURE": 11,
             "CONFIG_DB_INITIALIZED": 1,
             "DEVICE_METADATA": 1,
             "VERSIONS": 1,
             "KDUMP": 1}
        }
        self.available_tables['database'][DatabaseConst.ASIC_DB_ID].update(
            {"ASIC_STATE:SAI_OBJECT_TYPE_PORT": self.get_ib_ports_num(),
             "ASIC_STATE:SAI_OBJECT_TYPE_SWITCH": 0,
             "LANES": 0,
             "VIDCOUNTER": 0,
             "RIDTOVID": 0,
             "HIDDEN": 0,
             "COLDVIDS": 0})

        self.available_tables_per_asic = {
            DatabaseConst.APPL_DB_ID:
                {"ALIAS_PORT_MAP": self.get_ib_ports_num()},
            DatabaseConst.ASIC_DB_ID:
                {"ASIC_STATE:SAI_OBJECT_TYPE_PORT": self.get_ib_ports_num() / 2 + 1,
                 "LANES": 1,
                 "VIDCOUNTER": 1,
                 "RIDTOVID": 1,
                 "HIDDEN": 1,
                 "COLDVIDS": 1},
            DatabaseConst.COUNTERS_DB_ID:
                {"COUNTERS_PORT_NAME_MAP": 1,
                 "COUNTERS:oid": self.get_ib_ports_num() / 2},
            DatabaseConst.CONFIG_DB_ID:
                {"IB_PORT": self.get_ib_ports_num() / 2,
                 "FEATURE": 6,
                 "CONFIG_DB_INITIALIZED": 1,
                 "DEVICE_METADATA": 1,
                 "VERSIONS": 0,
                 "KDUMP": 0}
        }
        self.available_tables.update({'database0': self.available_tables_per_asic})

    def _init_services(self):
        super()._init_services()
        self.available_services.extend((
            'docker.service', 'database.service', 'hw-management.service', 'hw-management-tc.service', 'config-setup.service', 'ntpsec.service', 'hostname-config.service', 'ntp-config.service',
            'rsyslog-config.service', 'procdockerstatsd.service',
            'configmgrd.service', 'countermgrd.service', 'portsyncmgrd.service'
        ))
        for deamon in NvosConst.DOCKER_PER_ASIC_LIST:
            for asic_num in range(0, self.asic_amount):
                self.available_services.append(f'{deamon}@{asic_num}.service')

    def _init_dependent_services(self):
        super()._init_dependent_services()
        self.dependent_services.append(NvosConst.SYM_MGR_SERVICES)

    def _init_gnmi_consts(self):
        self.version_xpath = 'platform-general/versions/state/nos-version'
        self.bios_xpath = "platform-general/versions/state/fw-version-bios"
        self.cpld1_xpath = 'platform-general/versions/fw-versions-cpld/fw-version-cpld[id=1]/state/fw-version'
        self.cpld2_xpath = 'platform-general/versions/fw-versions-cpld/fw-version-cpld[id=1]/state/fw-version'
        self.cpld3_xpath = 'platform-general/versions/fw-versions-cpld/fw-version-cpld[id=3]/state/fw-version'
        self.cpld4_xpath = 'platform-general/versions/fw-versions-cpld/fw-version-cpld[id=4]/state/fw-version'
        self.components_gnmi_xpath = [self.bios_xpath, self.cpld1_xpath, self.cpld2_xpath,
                                      self.cpld3_xpath, self.cpld4_xpath]

    def _init_dockers(self):
        super()._init_dockers()
        self.available_dockers.extend(('database', NvosConst.NV_GNMI_DOCKER, NvosConst.NV_UMF_DOCKER))  # TODO: Add lldp container check
        for deamon in NvosConst.DOCKER_PER_ASIC_LIST:
            for asic_num in range(0, self.asic_amount):
                self.available_dockers.append(f"{deamon}{asic_num}")

    def _init_constants(self):
        super()._init_constants()
        self.full_version_pattern = r'^nvos-\d{2}\.\d{2}\.\d{4}(-\d{3})?$'
        self.version_number_pattern = r'\d{2}\.\d{2}\.\d{4}'
        self.health_monitor_config_file_path = ""
        self.platform_file_path = ""
        self.primary_asic = f"{IbConsts.DEVICE_ASIC_PREFIX}1"
        self.primary_swid = f"{IbConsts.SWID}0"
        self.primary_ipoib_interface = IbConsts.IPOIB_INT.format(self.asic_amount - 1)
        self.multi_asic_system = False
        self.multi_planar = False
        self.port_range_group_separator = ''
        self.login_pattern = NvosConst.INSTALL_SUCCESS_PATTERN
        self.install_patterns = {self.login_pattern: 0, "NOS install successful": 1}
        self.ib_host_player = 'ha'
        self.install_success_patterns = list(self.install_patterns.keys())
        self.mst_dev_name = ('/dev/mst/mt54002_pciconf0')
        self.category_list = ['temperature', 'cpu', 'disk', 'power', 'fan', 'mgmt-interface', 'voltage']
        self.category_disk_interval_default = '30'
        self.category_temperature_interval_default = '1'
        self.system_profile_default_values = ['enabled', '2048', 'disabled', 'disabled', '1']
        self.fw_versions_json_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/crocodile_versions.json"
        self.erot_fw_image_info = self.ErotFirmwareImagesTestConsts(
            current_image_path='auto/sw_system_release/erot/juliet/01.03.0202.000/sign/n04/dev/cec1736-ecfw-01.03.0202.0000-n04-dev-initial.bin',
            previous_image_path='auto/sw_system_release/erot/juliet/01.03.0183.000/sign/n04/dev/cec1736-ecfw-01.03.0183.0000-n04-dev-initial.bin',
            version_names={'cec1736-ecfw-01.03.0196.0001-n04-dev-initial.fwpkg': '01.03.0196.0001_n04',
                           'cec1736-ecfw-01.03.0202.0000-n04-dev-initial.fwpkg': '01.03.0202.0000_n04'})
        self.ztp_prod_json = 'uninstall_prod.json'
        self.ztp_dev_json = 'uninstall.json'
        self.ztp_complex_prod_json = 'complex_prod.json'
        self.ztp_complex_dev_json = 'complex.json'
        self.ib_host_player = 'ha'
        self.list_of_leakages = []

        # Techsupport constants for IB devices
        self.techsupport_files_path = SystemConsts.TECHSUPPORT_FILES_PATH
        self.techsupport_upload_success_message = 'File upload successfully'
        self.techsupport_delete_success_message = 'File delete successfully'
        self.techsupport_file_not_found_message = SystemConsts.TECHSUPPORT_FILE_NOT_FOUND_MESSAGE
        # Note: techsupport_threshold is set after _init_expected_operation_durations() is called

        self.techsupport_dump_empty_files_to_ignore = ['queue.counters_2', 'queue.counters_1.0', 'swapon', 'queue.counters_1',
                                                       'queue.counters_2.0', 'queue.counters_1.1', 'queue.counters_2.1',
                                                       'queue.counters_1.3', 'queue.counters_1.2', 'queue.counters_2.3',
                                                       'queue.counters_2.2', 'rsyslog.conf', 'verification_test',
                                                       'verification_test.gz', 'cpu-debug-config', 'bmc-debug-config', 'asic-debug-config',
                                                       'system.profile']
        self.techsupport_etc_empty_files_to_ignore = ['ifstatelock', '.lock', 'base', 'tail', 'installed', 'rules.v4',
                                                      'rules.v6', 'gnmi-server_reconcile', 'lsb_release', 'usr.sbin.haveged',
                                                      'nvidia_modprobe', '.placeholder', '.pwd.lock',
                                                      'verification_test', 'opasswd.old', 'opasswd', 'sbin.dhclient', 'reload.lock',
                                                      'empty.sh', 'nv-bridge_reconcile', 'gpu_telemetry_enable',
                                                      'peer_telemetry_enable', '10-ztp-log-forwarding.conf',
                                                      'event_db.json', 'accept_eula_version']
        self.techsupport_cluster_empty_files_to_ignore = ['redis.log', 'config_storage.json', 'user_config_changed',
                                                          'nvlink_domain_telemetry.csv']
        self.techsupport_hw_mgmt_empty_files_to_ignore = ['hw-management-fixup.sh', 'hw-management-bmc-fixup.sh']
        self.techsupport_skynet_hw_mgmt_empty_files_to_ignore = ['udev_events.log']
        # System message constants for IB devices - set after pre/post login messages are initialized
        self.default_user_name = SystemConsts.DEFAULT_USER_ADMIN
        # System image constants for IB devices
        self.nfs_server = SystemConsts.NBU_NFS_SERVER
        self.upload_path = f'://{NvosConst.ROOT_USER}:{NvosConst.ROOT_PASSWORD}@{NvosConst.FIT70}/tmp/'
        self.default_username = SystemConsts.DEFAULT_USER_ADMIN
        self.release_name = ImageConsts.NVOS_RELEASE_25_02_1000
        self.list_of_leakages = []

        self.reboot_reason_dict = {
            RebootConsts.HALT: (SystemConsts.REBOOT_REASON_POWER_LOSS, RebootConsts.REBOOT_USER_ADMIN),
            RebootConsts.COLD: ("reboot", RebootConsts.REBOOT_USER_ADMIN),
            RebootConsts.IMMEDIATE: ("reboot", RebootConsts.REBOOT_USER_ADMIN),
            RebootConsts.FACTORY_RESET: ("reboot", RebootConsts.REBOOT_USER_SYSTEM),
            RebootConsts.POWER_BUTTON: (SystemConsts.REBOOT_REASON_POWER_BUTTON, RebootConsts.REBOOT_USER_NA),
            RebootConsts.PSU_OFF: (SystemConsts.REBOOT_REASON_POWER_LOSS, RebootConsts.REBOOT_USER_NA),
            RebootConsts.REMOTE_REBOOT: (SystemConsts.REBOOT_REASON_POWER_LOSS, RebootConsts.REBOOT_USER_NA),
            RebootConsts.INSTALL_FW: ("reboot", RebootConsts.REBOOT_USER_ADMIN),
            RebootConsts.RESET_BIT: (RebootConsts.REBOOT_REASON_RESET_BIT, RebootConsts.REBOOT_USER_NA,
                                     RebootConsts.REBOOT_REASON_TYPE_CRITICAL_ERROR)
        }
        # Reset-bit reboot trigger, shared by all IB switches (Taipan, Crocodile, ...).
        # Subclasses can override or clear it if their platform doesn't support it.
        self.sys_rst_n_bit_command = NvosConst.SYS_RST_N_BIT_COMMAND

        self.category_default_disabled_dict = {
            StatsConsts.HISTORY_DURATION: StatsConsts.HISTORY_DURATION_DEFAULT,
            StatsConsts.INTERVAL: StatsConsts.INTERVAL_DEFAULT,
            StatsConsts.STATE: StatsConsts.State.DISABLED.value
        }
        self.category_default_dict = {
            StatsConsts.HISTORY_DURATION: StatsConsts.HISTORY_DURATION_DEFAULT,
            StatsConsts.INTERVAL: StatsConsts.INTERVAL_DEFAULT,
            StatsConsts.STATE: StatsConsts.STATE_DEFAULT
        }
        self.category_disk_default_dict = {
            StatsConsts.HISTORY_DURATION: StatsConsts.HISTORY_DURATION_DEFAULT,
            StatsConsts.INTERVAL: self.category_disk_interval_default,
            StatsConsts.STATE: StatsConsts.STATE_DEFAULT
        }
        self.category_disk_default_disable_dict = {
            StatsConsts.HISTORY_DURATION: StatsConsts.HISTORY_DURATION_DEFAULT,
            StatsConsts.INTERVAL: self.category_disk_interval_default,
            StatsConsts.STATE: StatsConsts.State.DISABLED.value
        }
        self.category_temperature_default_dict = {
            StatsConsts.HISTORY_DURATION: StatsConsts.HISTORY_DURATION_DEFAULT,
            StatsConsts.INTERVAL: self.category_temperature_interval_default,
            StatsConsts.STATE: StatsConsts.STATE_DEFAULT
        }
        self.category_temperature_default_disable_dict = {
            StatsConsts.HISTORY_DURATION: StatsConsts.HISTORY_DURATION_DEFAULT,
            StatsConsts.INTERVAL: self.category_temperature_interval_default,
            StatsConsts.STATE: StatsConsts.State.DISABLED.value
        }
        self.category_disabled_dict = {
            self.category_list[0]: self.category_temperature_default_disable_dict,
            self.category_list[1]: self.category_default_disabled_dict,
            self.category_list[2]: self.category_disk_default_disable_dict,
            self.category_list[3]: self.category_default_disabled_dict,
            self.category_list[4]: self.category_default_disabled_dict,
            self.category_list[5]: self.category_default_disabled_dict,
            self.category_list[6]: self.category_default_disabled_dict
        }
        self.category_list_default_dict = {
            self.category_list[0]: self.category_temperature_default_dict,
            self.category_list[1]: self.category_default_dict,
            self.category_list[2]: self.category_disk_default_dict,
            self.category_list[3]: self.category_default_dict,
            self.category_list[4]: self.category_default_dict,
            self.category_list[5]: self.category_default_dict,
            self.category_list[6]: self.category_default_dict
        }

        # Counters Historical Analysis new categories (interface / peer-port /
        # transceivers), shared by all IB and NVL switches. Kept in a separate list
        # so the existing stats suite (which iterates category_list) is unaffected;
        # the new suite discovers the categories actually present at runtime and
        # skips the ones a given platform prunes.
        self.new_category_list = [StatsConsts.INTERFACE, StatsConsts.PEER_PORT, StatsConsts.TRANSCEIVERS]
        self.new_category_list_default_dict = {
            name: StatsConsts.NEW_CATEGORY_DEFAULT_DICT for name in self.new_category_list
        }
        self.new_category_disabled_dict = {
            name: StatsConsts.NEW_CATEGORY_DEFAULT_DISABLED_DICT for name in self.new_category_list
        }

        # NVUE logic-relock defaults (override per switch class, e.g. BlackMamba)
        self.default_phy_recovery_dict = {
            PhyRecoveryConsts.LogicRelock.MODE: PhyRecoveryConsts.DISABLED,
            PhyRecoveryConsts.LogicRelock.TIMEOUT: str(0),
        }

        self.asic0 = 'asic0'
        self.asic1 = 'asic1'
        self.asic_numbers = [f"ASIC{i}" for i in range(1, self.asic_amount + 1)]
        self.counters_db_name = 'COUNTERS_DB'

        # Default firmware components list for IB switches (can be overridden by subclasses)
        self.components_list = [FW_COMPONENT_CPLD,
                                FW_COMPONENT_BIOS,
                                FW_COMPONENT_SSD,
                                FW_COMPONENT_ASIC]

        # Expected ACL rule counts after migration (device-specific, can be overridden)
        self.expected_acl_rule_counts = {
            'acl-default-loopback': 1,
            'acl-default-loopback-ipv6': 1,
            'acl-default-dos': 61,
            'acl-default-dos-ipv6': 72,
            'acl-default-whitelist': 26,
            'acl-default-whitelist-ipv6': 25,
            'acl-default-outbound': 2,
            'acl-default-outbound-ipv6': 2
        }

        self.voltage_sensors = ["PMIC-1-12V-ASIC-VCORE-In-1", "PMIC-1-ASIC-VCORE-Out-1", "PMIC-2-12V-ASIC-HVDD-DVDD-In-1",
                                "PMIC-2-ASIC-DVDD-WEST-Out-2", "PMIC-2-ASIC-HVDD-WEST-Out-1", "PMIC-3-12V-ASIC-HVDD-DVDD-In-1",
                                "PMIC-3-ASIC-DVDD-EAST-Out-2", "PMIC-3-ASIC-HVDD-EAST-Out-1", "PMIC-4-3.3V-OSFP-P01-P08-Out-1",
                                "PMIC-4-3.3V-OSFP-P09-P16-Out-2", "PMIC-4-12V-PORTS-WEST-In-1", "PMIC-5-3.3V-OSFP-P17-P24-Out-1",
                                "PMIC-5-3.3V-OSFP-P25-P32-Out-2", "PMIC-5-12V-PORTS-EAST-In-1", "PMIC-6-13V5-COMEX-VDD-In-1",
                                "PMIC-6-COMEX-VCCSA-Out-2", "PMIC-6-COMEX-VCORE-Out-1", "PSU-1-12V-Out", "PSU-2-12V-Out"]

        self.device_list = [IbConsts.DEVICE_ASIC_PREFIX + str(index) for index in range(1, self.asic_amount + 1)]
        self.device_list.append(IbConsts.DEVICE_SYSTEM)

        dump_files_to_replace_for_each_asic = ['docker.swss-ibv0{}.log', 'saidump{}']
        dump_files_to_add_for_each_asic = ['APPL_DB.json.{}', 'ASIC_DB.json.{}', 'CONFIG_DB.json.{}',
                                           'STATE_DB.json.{}', 'FLEX_COUNTER_DB.json.{}', 'COUNTERS_DB.json.{}',
                                           'COUNTERS_DB_1.json.{}', 'COUNTERS_DB_2.json.{}']

        for dump_file in dump_files_to_replace_for_each_asic:
            self.constants.dump_files.remove(dump_file.format(''))
            for asic_num in range(0, self.asic_amount):
                self.constants.dump_files.append(dump_file.format(asic_num))

        self.constants.firmware.append('transceiver')

        for dump_file in dump_files_to_add_for_each_asic:
            for asic_num in range(0, self.asic_amount):
                self.constants.dump_files.append(dump_file.format(asic_num))

        self.pre_login_message = "NVOS switch"
        self.post_login_message = "\n \u2588\u2588\u2588\u2557   \u2588\u2588\u2557\u2588\u2588\u2557   " \
                                  "\u2588\u2588\u2557 \u2588\u2588\u2588\u2588\u2588\u2588\u2557 " \
                                  "\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557\n \u2588\u2588\u2588\u2588" \
                                  "\u2557  \u2588\u2588\u2551\u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588" \
                                  "\u2554\u2550\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2554\u2550\u2550" \
                                  "\u2550\u2550\u255d\n \u2588\u2588\u2554\u2588\u2588\u2557 \u2588\u2588" \
                                  "\u2551\u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2551   " \
                                  "\u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557\n " \
                                  "\u2588\u2588\u2551\u255a\u2588\u2588\u2557\u2588\u2588\u2551\u255a\u2588" \
                                  "\u2588\u2557 \u2588\u2588\u2554\u255d\u2588\u2588\u2551   \u2588\u2588\u2551" \
                                  "\u255a\u2550\u2550\u2550\u2550\u2588\u2588\u2551\n \u2588\u2588\u2551 \u255a" \
                                  "\u2588\u2588\u2588\u2588\u2551 \u255a\u2588\u2588\u2588\u2588\u2554\u255d " \
                                  "\u255a\u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2588" \
                                  "\u2588\u2588\u2588\u2588\u2551\n \u255a\u2550\u255d  \u255a\u2550\u2550" \
                                  "\u2550\u255d  \u255a\u2550\u2550\u2550\u255d   \u255a\u2550\u2550\u2550" \
                                  "\u2550\u2550\u255d \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u255d\n"

        # Set system message default and test values
        self.post_logout_message = SystemConsts.POST_LOGOUT_MESSAGE_DEFAULT_VALUE

        self.ssd_image_per_ssd_model = {
            'StorFly VSFBM4XC016G-MLX2':
                BaseSwitch.SsdImageConsts(
                    file='/auto/sw_system_project/NVOS_INFRA/verification_files/ssd_fw/virtium_ssd_fw_pkg.pkg',
                    current_version='0202-000', alternate_version='0202-002'),
        }
        self.module_offset = None  # Should be overridden in child if used for module mapping
        if not hasattr(self, 'expected_operation_durations'):
            self.expected_operation_durations = {}
        # Note: _init_expected_operation_durations() is called by BaseSwitch._init_platform_lists()
        self.techsupport_threshold = self.expected_operation_durations.get(self.generate_tech_support)
        self.unsupported_commands_list = [
            "nv show platform environment leakage",
            "nv show platform firmware BMC",
            "nv show platform firmware FPGA",
            "nv show platform firmware EROT",
            "nv show platform firmware EROT-ASIC1",
            "nv show platform firmware EROT-ASIC2",
            "nv show platform firmware EROT-BMC",
            "nv show platform firmware EROT-CPU",
            "nv show platform firmware EROT-FPGA",
            "nv show platform boot-policy",
            "nv show platform cable-cartridge",
            "nv show platform chassis-location",
            "nv show cluster",
            "nv show sdn",
            "nv sh fae interface swA10p1 link link-training",
            "nv show interface swA10p1 link plr",
            # IB (croc+mamba) uses logic-relock-*, NOT serdes-eq-* for PHY recovery
            "nv set fae interface {port} link phy-recovery serdes-eq-mode",
            "nv set fae interface {port} link phy-recovery serdes-eq-timeout",
            "nv set fae interface {port} link link-training",
            # Rosalind-only PHY recovery commands (not supported on croc+mamba)
            "nv set fae interface {port} link phy-role",
            "nv set fae interface {port} link constant-role",
            "nv set fae interface {port} link phy-recovery link-down-timeout",
            "nv set fae interface {port} link phy-recovery recovery-neg-type",
            "nv set fae interface {port} link phy-recovery recovery-status",
            "nv set fae interface {port} link phy-recovery step-1",
            "nv set fae interface {port} link phy-recovery step-2",
            "nv show fae interface {port} link low-power",
            "nv set fae interface {port} link low-power state",
            "nv unset fae interface {port} link low-power state",
            "nv show fae platform bkv",
            "nv show fae system sto-debug"
        ]
        # NVL7 CPO trunk-port PHY-recovery leaves + counters debug: pruned everywhere except CPO
        # NVL7 (removed from the list in PortiaCpoSwitch; PortiaSimx/NPO deliberately keeps them pruned).
        self.unsupported_commands_list.extend(PhyRecoveryConsts.NVL7_PRUNED_COMMANDS)

        self.memory_size: list[float] = [15.0]
        self.supported_disk_list: list[SSDConsts.SSDType] = [
            SSDConsts.SFSA160GM2AK2TO_I_8C_22K_NVI,
            SSDConsts.VIRTIUM_VTPM24CEXI08_BM110006
        ]
        self.fetch_success_message = NvosConst.FETCH_SUCCESS_MESSAGE
        self.fetch_error_message = NvosConst.FETCH_ERROR_MESSAGE
        self.ask_for_confirmation = False
        self.system_default_value_dict = {
            SystemConsts.HOSTNAME: SystemConsts.HOSTNAME_DEFAULT_VALUE,
            SystemConsts.CONTACT: None,
            SystemConsts.LOCATION: None
        }
        self.expected_selector_dictionary = NvosConst.EXPECTED_SELECTOR_DICTIONARY
        self.welf_format_regex = "id=firewall time=\".*\" fw=\"{}\" pri=\\d msg=\".*\""
        self.vrf_mgmt = SyslogConsts.DEFAULT_VRF

        # Initialize link error counters for traffic validation
        self.system_log_value = "part-number"
        self.system_log_timestamp_regex = NvosConst.DATE_TIME_REGEX[0]

    def _init_link_error_counters(self):
        """
        Initialize link error counters to check after traffic tests.

        Override in subclasses to add platform-specific counters by extending the dict.

        Structure:
            traffic_error_counters = {
                'link': [...],       # Counters under 'nv show interface <port> counters link'
                'top_level': [...],  # Counters under 'nv show interface <port> counters'
            }
        """
        # Use defaults from TrafficErrorCounters helper
        self.traffic_error_counters = TrafficErrorCounters.get_default()

    def get_base_image(self):
        release_name = ImageConsts.NVOS_RELEASE_25_02_1000
        return ImageConsts.BASE_IMAGE_VERSION_TO_INSTALL.format(
            pre_release_name=release_name
        )

    def get_base_image_path(self, image_file):
        release_name = ImageConsts.NVOS_RELEASE_25_02_1000
        return ImageConsts.BASE_IMAGE_VERSION_TO_INSTALL_PATH.format(
            pre_release_name=release_name,
            base_image=image_file
        )

    def sleep_after_system_reboot(self):
        pass

    def init_documents_consts(self):
        self.documents_files = {
            DocumentsConsts.TYPE_EULA: "NVOS_EULA.pdf",
            DocumentsConsts.TYPE_RELEASE_NOTES: f"NVOS_{self.switch_type}_Release_Notes.pdf",
            DocumentsConsts.TYPE_USER_MANUAL: f"NVOS_{self.switch_type}_User_Manual.pdf",
            DocumentsConsts.TYPE_OPEN_SOURCE_LICENSES: "Open_Source_Licenses.txt"}
        self.documents_path = {DocumentsConsts.TYPE_EULA:
                               f"/usr/share/nginx/html/system_documents/eula/{self.documents_files[DocumentsConsts.TYPE_EULA]}",
                               DocumentsConsts.TYPE_RELEASE_NOTES:
                               f"/usr/share/nginx/html/system_documents/release_notes/{self.documents_files[DocumentsConsts.TYPE_RELEASE_NOTES]}",
                               DocumentsConsts.TYPE_USER_MANUAL:
                               f"/usr/share/nginx/html/system_documents/user_manual/{self.documents_files[DocumentsConsts.TYPE_USER_MANUAL]}",
                               DocumentsConsts.TYPE_OPEN_SOURCE_LICENSES:
                               f"/usr/share/nginx/html/system_documents/open_source_licenses/"
                               f"{self.documents_files[DocumentsConsts.TYPE_OPEN_SOURCE_LICENSES]}"}

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors += ["CPU-Core-2-Temp", "CPU-Core-3-Temp", "PCH-Temp", "PSU-2-Temp", "SODIMM-1-Temp"]

    def _init_sensors_dict(self):
        self.sensors_dict = {"VOLTAGE": self.voltage_sensors,
                             "TEMPERATURE": self.temperature_sensors}

    def _init_interface_lists(self):
        self.ib_ports_num = 64
        self.mgmt_ports = ['eth0']
        self.plane_port_list = ['pl1', 'pl2']
        self.default_aggregated_port = 'sw32p1'
        self.default_loopback_ports = ['sw31p1', 'sw31p2']
        self.loop_back_to_ports = {
            'sw31p1': 'sw32p1pl1',
            'sw31p2': 'sw32p1pl2'
        }
        self.default_port = 'sw1p1'
        self.aggregated_port_list = ['sw1p1', 'sw2p1', 'sw32p1']  # total 3 ports
        self.fnm_port_list = ['fnm1']
        self.aggregated_split_port_list = ['sw10p1']
        self.fnm_external_port_list = ['fnm1']
        self.fnm_external_child_port = 'fnm1s1'
        self.interface_active_internal_fnm_ports = {}
        self.child_aggregated_port = 'swA11p1s1'
        self.num_of_plane_ports = 4
        self.num_of_fnm_plane_ports = 2
        self.network_ports = ['eth0', 'ib0', 'lo']  # total 3 ports
        self.fnm_link_speed = '400G'
        self.fnm_port_type = 'fnm'
        self.object_numbers = {  # TBD - update values
            'sw1p1': {
                'plane1': 'COUNTERS:oid:0x100000000001f',
                'plane2': 'COUNTERS:oid:0x100000000001f'
            },
            'sw2p1': {
                'plane1': 'COUNTERS:oid:0x100000000001f',
                'plane2': 'COUNTERS:oid:0x100000000001f'
            },
            'sw32p1': {
                'plane1': 'COUNTERS:oid:0x100000000001f',
                'plane2': 'COUNTERS:oid:0x100000000001f'
            }
        }

    def _init_interface_attributes_mapping_dict(self):
        self.interface_attributes_mapping_dict = {
            IbInterfaceConsts.LINK_AUTO_NEGOTIATE: GnmiConstants.SPEED_NEGOTIATE,
            IbInterfaceConsts.LINK_SPEED: GnmiConstants.SPEED,
            # counters
            IbInterfaceConsts.LINK_STATS_IN_BYTES: GnmiConstants.IN_OCTETS,
            IbInterfaceConsts.LINK_STATS_IN_PKTS: GnmiConstants.IN_PKTS,
            IbInterfaceConsts.LINK_STATS_IN_DROPS: GnmiConstants.IN_DISCARDS,
            IbInterfaceConsts.LINK_STATS_IN_ERRORS: GnmiConstants.IN_ERRORS,
            IbInterfaceConsts.LINK_STATS_OUT_BYTES: GnmiConstants.OUT_OCTETS,
            IbInterfaceConsts.LINK_STATS_OUT_PKTS: GnmiConstants.OUT_PKTS,
            IbInterfaceConsts.LINK_STATS_OUT_DROPS: GnmiConstants.OUT_DISCARDS,
            IbInterfaceConsts.LINK_STATS_OUT_ERRORS: GnmiConstants.OUT_ERRORS,
            IbInterfaceConsts.LINK_STATS_IN_SYMBOL_ERRORS: GnmiConstants.SYMBOL_ERROR_COUNTER,
            IbInterfaceConsts.LINK_STATS_OUT_WAIT: GnmiConstants.XMIT_WAIT,
            IbInterfaceConsts.LINK_STATS_QNT3[0]: GnmiConstants.LINK_ERROR_RECOVERY,
            IbInterfaceConsts.LINK_STATS_QNT3[1]: GnmiConstants.LINK_DOWNED,
            IbInterfaceConsts.LINK_STATS_QNT3[2]: GnmiConstants.RCV_REMOTE_PHY_ERRORS,
            IbInterfaceConsts.LINK_STATS_QNT3[3]: GnmiConstants.RCV_SWITCH_RELAY_ERRORS,
            IbInterfaceConsts.LINK_STATS_QNT3[4]: GnmiConstants.RCV_CONSTRAINTS_ERRORS,
            IbInterfaceConsts.LINK_STATS_QNT3[5]: GnmiConstants.LOCAL_LINK_INTEGRITY_ERRORS,
            IbInterfaceConsts.LINK_STATS_QNT3[6]: GnmiConstants.QP1_DROPPED,
            IbInterfaceConsts.LINK_STATS_QNT3[7]: GnmiConstants.PORT_BUFFER_OVERRUN_ERRORS,
            # IbInterfaceConsts.LINK_STATS_QNT3[8]: '', #TODO: check if attributes exist in gnmi output in different names, otherwise delete
            # IbInterfaceConsts.LINK_STATS_QNT3[9]: '', #TODO: check if attributes exist in gnmi output in different names, otherwise delete
            IbInterfaceConsts.LINK_PLR_RCV_CODES_ERRORS: GnmiConstants.LINK_PLR_RCV_CODE_ERRORS,
            IbInterfaceConsts.LINK_STATS_UNICAST_IN_PKTS: GnmiConstants.IN_UNICAST_PKTS,
            IbInterfaceConsts.LINK_STATS_UNICAST_OUT_PKTS: GnmiConstants.OUT_UNICAST_PKTS,
            IbInterfaceConsts.LINK_STATS_MULTICAST_IN_PKTS: GnmiConstants.IN_MULTICAST_PKTS,
            IbInterfaceConsts.LINK_STATS_MULTICAST_OUT_PKTS: GnmiConstants.OUT_MULTICAST_PKTS,
            ##
            IbInterfaceConsts.LINK_MAX_SUPPORTED_MTU: GnmiConstants.MAX_SUPPORTED_MTUS,
            IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE: GnmiConstants.PHYSICAL_PORT_STATE,
            IbInterfaceConsts.LINK_LOGICAL_PORT_STATE: GnmiConstants.LOGICAL_PORT_STATE,
            IbInterfaceConsts.LINK_SUPPORTED_LANES: GnmiConstants.SUPPORTED_WIDTHS,
            IbInterfaceConsts.LINK_VL_ADMIN_CAPABILITIES: GnmiConstants.VL_CAPABILITIES,
            IbInterfaceConsts.LINK_PHY_RAW_ERRORS[0]: GnmiConstants.PHY_RAW_ERRORS[0],
            IbInterfaceConsts.LINK_PHY_RAW_ERRORS[1]: GnmiConstants.PHY_RAW_ERRORS[1],
            IbInterfaceConsts.LINK_PHY_RAW_ERRORS[2]: GnmiConstants.PHY_RAW_ERRORS[2],
            IbInterfaceConsts.LINK_PHY_RAW_ERRORS[3]: GnmiConstants.PHY_RAW_ERRORS[3],
            IbInterfaceConsts.LINK_PHY_RAW_ERRORS[4]: GnmiConstants.PHY_RAW_ERRORS[4],
            IbInterfaceConsts.LINK_PHY_RAW_ERRORS[5]: GnmiConstants.PHY_RAW_ERRORS[5],
            IbInterfaceConsts.LINK_PHY_RAW_ERRORS[6]: GnmiConstants.PHY_RAW_ERRORS[6],
            IbInterfaceConsts.LINK_PHY_RAW_ERRORS[7]: GnmiConstants.PHY_RAW_ERRORS[7],
            IbInterfaceConsts.LINK_PHY_RAW_BER[0]: GnmiConstants.PHY_RAW_BER[0],
            IbInterfaceConsts.LINK_PHY_RAW_BER[1]: GnmiConstants.PHY_RAW_BER[1],
            IbInterfaceConsts.LINK_PHY_RAW_BER[2]: GnmiConstants.PHY_RAW_BER[2],
            IbInterfaceConsts.LINK_PHY_RAW_BER[3]: GnmiConstants.PHY_RAW_BER[3],
            IbInterfaceConsts.LINK_PHY_RAW_BER[4]: GnmiConstants.PHY_RAW_BER[4],
            IbInterfaceConsts.LINK_PHY_RAW_BER[5]: GnmiConstants.PHY_RAW_BER[5],
            IbInterfaceConsts.LINK_PHY_RAW_BER[6]: GnmiConstants.PHY_RAW_BER[6],
            IbInterfaceConsts.LINK_PHY_RAW_BER[7]: GnmiConstants.PHY_RAW_BER[7]
        }

    def wait_for_os_to_become_functional(self, engine, find_prompt_tries=60, find_prompt_delay=10):
        return DutUtilsTool.wait_for_nvos_to_become_functional(engine, cli_up_retries=self.cli_up_retries)

    def recover_after_fw_crash(self, engine, system):
        """Recover the system after a FW crash event.

        On standard IB systems, docker restart handles recovery without reboot,
        so we manually reboot to restore a clean state.
        Subclasses (e.g., TaipanSwitch) override this for platform-specific behavior.
        """
        with allure.step('Reboot system after FW crash'):
            system.reboot.action_reboot(params='force').verify_result()

    def reload_device(self, engine, cmd_list, validate=False, enter_config_mode=False):
        if any("action install platform firmware" in cmd for cmd in cmd_list):
            return engine.send_config_set(cmd_list, exit_config_mode=False, cmd_verify=False,
                                          enter_config_mode=enter_config_mode, read_timeout=900)
        else:
            return engine.send_config_set(cmd_list, exit_config_mode=False, cmd_verify=False,
                                          enter_config_mode=enter_config_mode)

    def get_bios_file_name(self):
        return self.current_bios_version_path.split('/')[-1]

    def setup_base_aaa_config(self, dut_engine: LinuxSshEngine):
        pass

    def cleanup_base_aaa_config(self, dut_engine: LinuxSshEngine):
        pass

    @classmethod
    def get_lane_bmap(cls, port: Port) -> str:
        """
        Calculates the lane-bmap as it would appear in the output of sx_api_ports_mapping_dump.py
        Note: Aggregated ports have no bmap so this function fails for these.

        Returns:
            str: lane-bmap as a string representing a hex number, e.g. on Crocodile: Port('swB11p2pl4') --> '0x18'
        """
        # todo cls.validate_port_name(port)
        lane_bmap = cls._get_lane_bmap(port)
        lane_bmap = f'0x{lane_bmap:0>2X}'
        logger.info(f"{port.name=} ==> {lane_bmap=}")
        return lane_bmap

    @classmethod
    def _get_lane_bmap(cls, port) -> int:
        raise NotImplementedError

# -------------------------- Gorilla Switch ----------------------------


class GorillaSwitch(IbSwitch):

    def __init__(self, asic_amount=1):
        super().__init__(asic_amount=asic_amount, switch_class=NvosConst.GORILLA_SWITCH)

    def _init_constants(self):
        IbSwitch._init_constants(self)
        self.core_count = 4
        self.asic_type = NvosConst.QTM2
        self.split_ports_supported = True
        self.profile_change_supported = True
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-mlnx_mqm9700-r0")
        self.platform_file_path = MultiPlanarConsts.PLATFORM_FILE_FULL_PATH.format("x86_64-mlnx_mqm9700-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "MQM9700",
            "asic-model": self.asic_type,
        })
        self.asic_version = BaseSwitch.AsicImageConsts(
            version="31_2014_0902-024",
            filename="fw-QTM2-rel-31_2014_0902-024.mfa"
        )
        self.previous_cpld_version = BaseSwitch.CpldImageConsts(
            burn_image_path="/auto/sw_system_project/NVOS_INFRA/verification_files/cpld_fw/FUI000258_BURN_Gorilla_MNG_CPLD000232_REV0700_CPLD000324_REV0300_CPLD000268_REV0700_IPN.vme",
            refresh_image_path="/auto/sw_system_project/NVOS_INFRA/verification_files/cpld_fw/FUI000258_REFRESH_Gorilla_MNG_CPLD000232_REV0700_CPLD000324_REV0300_CPLD000268_REV0700.vme",
            version_names={
                "CPLD1": "CPLD000232_REV0700",
                "CPLD2": "CPLD000324_REV0300",
                "CPLD3": "CPLD000268_REV0700",
            }
        )
        self.current_cpld_version = BaseSwitch.CpldImageConsts(
            burn_image_path="/auto/sw_system_project/NVOS_INFRA/verification_files/cpld_fw/FUI000276_BURN_Gorilla_MNG_CPLD000232_REV0700_CPLD000324_REV0400_CPLD000268_REV0700_IPN.vme",
            refresh_image_path="/auto/sw_system_project/NVOS_INFRA/verification_files/cpld_fw/FUI000276_REFRESH_Gorilla_MNG_CPLD000232_REV0700_CPLD000324_REV0400_CPLD000268_REV0700.vme",
            version_names={
                "CPLD1": "CPLD000232_REV0700",
                "CPLD2": "CPLD000324_REV0400",
                "CPLD3": "CPLD000268_REV0700",
            }
        )
        self.stats_disk_header_num_of_lines = 16
        self.stats_cpu_header_num_of_lines = 39
        self.stats_temperature_header_num_of_lines = 53
        self.valid_ports_count = 64
        self.cpld_amount = 3
        self.number_of_transceivers = 64
        self.transceivers_tables_name = "TRANSCEIVER_FIRMWARE_INFO"
        self.transceiver_list = [f'sw{a + 1}' for a in range(32)]
        self.supports_tpm_testing = False
        self.supports_ima_testing = False

    def get_mgmt_ports(self) -> list[str]:
        return self.mgmt_ports

    def verify_sed_password(self, tpm_tool, sed_default_password=""):
        return  # This should be ignored on gorilla, overrides method from base switch

    def _init_fan_list(self):
        super()._init_fan_list()
        self.fan_list += ["FAN7/1", "FAN7/2"]

    def _init_led_list(self):
        super()._init_led_list()
        self.led_list.append('FAN7')

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors += ["CPU-Core-2-Temp", "CPU-Core-3-Temp", "PCH-Temp", "PSU-2-Temp"]

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=2000, range_max=10000),
            "max-speed": ExpectedString(range_min=20000, range_max=40000)}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": ExpectedString(regex="MQM9700.*")})
        self._extend_firmware_by_cpld_amount()

    def _init_eth0_speeds(self):
        super()._init_eth0_speeds()
        self.supported_eth0_speeds += ['10M']

    def _init_boot_time_timeouts(self):
        super()._init_boot_time_timeouts()
        self.timeout_system_is_ready = 10 * MINUTE


# -------------------------- Gorilla BF3 Switch ----------------------------
class GorillaSwitchBF3(GorillaSwitch):

    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.constants.firmware.remove(PlatformConsts.FW_BIOS)
        self.ib_ports_num = 64
        self.core_count = 16
        self.asic_type = NvosConst.QTM2

    def get_mgmt_ports(self) -> list[str]:
        return self.mgmt_ports

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors += ["xSFP-module-26-Temp", "xSFP-module-29-Temp"]


# -------------------------- BlackMamba Switch ----------------------------
class BlackMambaSwitch(IbSwitch):

    def __init__(self, switch_class=NvosConst.BLACK_MAMBA_SWITCH):
        super().__init__(asic_amount=4, switch_class=switch_class)

    def _init_constants(self):
        self.asic_amount = 4
        self.asic_numbers = [f"ASIC{i}" for i in range(1, self.asic_amount + 1)]
        super()._init_constants()
        # BM Factory defaults
        self.default_phy_recovery_dict = {
            PhyRecoveryConsts.LogicRelock.MODE: PhyRecoveryConsts.ENABLED,
            PhyRecoveryConsts.LogicRelock.TIMEOUT: str(100),
        }
        self.use_ga_default_config = True
        self.supports_ssd_upgrade = True
        self.ib_ports_num = 2 * 72
        self.core_count = 4
        self.cpld_amount = 6
        self.asic_type = NvosConst.QTM3
        self.multi_planar = True
        self.ib_host_player = 'hfnm'
        self.platform_file_path = MultiPlanarConsts.PLATFORM_FILE_FULL_PATH.format("x86_64-mlnx_qm8790-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "Q3400_RA",
            "asic-model": self.asic_type,
        })
        self.asic_version = BaseSwitch.AsicImageConsts(
            version="35.2014.2012",
            filename="fw-QTM3-rel-35_2014_2012.mfa"
        )
        self.voltage_sensors = ['PMIC-1-12V-VDD-ASIC1-In-1', 'PMIC-1-ASIC1-VDD-Out-1',
                                'PMIC-2-12V-HVDD-DVDD-ASIC1-In-1', 'PMIC-2-ASIC1-DVDD-PL0-Out-2',
                                'PMIC-2-ASIC1-HVDD-PL0-Out-1', 'PMIC-3-12V-HVDD-DVDD-ASIC1-In-1',
                                'PMIC-3-ASIC1-DVDD-PL1-Out-2', 'PMIC-3-ASIC1-HVDD-PL1-Out-1',
                                'PMIC-4-12V-VDD-ASIC2-In-1', 'PMIC-4-ASIC2-VDD-Out-1',
                                'PMIC-5-12V-HVDD-DVDD-ASIC2-In-1', 'PMIC-5-ASIC2-DVDD-PL0-Out-2',
                                'PMIC-5-ASIC2-HVDD-PL0-Out-1', 'PMIC-6-12V-HVDD-DVDD-ASIC2-In-1',
                                'PMIC-6-ASIC2-DVDD-PL1-Out-2', 'PMIC-6-ASIC2-HVDD-PL1-Out-1',
                                'PMIC-7-12V-VDD-ASIC3-In-1', 'PMIC-7-ASIC3-VDD-Out-1',
                                'PMIC-8-12V-HVDD-DVDD-ASIC3-In-1', 'PMIC-8-ASIC3-DVDD-PL0-Out-2',
                                'PMIC-8-ASIC3-HVDD-PL0-Out-1', 'PMIC-9-12V-HVDD-DVDD-ASIC3-In-1',
                                'PMIC-9-ASIC3-DVDD-PL1-Out-2', 'PMIC-9-ASIC3-HVDD-PL1-Out-1',
                                'PMIC-10-12V-VDD-ASIC4-In-1', 'PMIC-10-ASIC4-VDD-Out-1',
                                'PMIC-11-12V-HVDD-DVDD-ASIC4-In-1', 'PMIC-11-ASIC4-DVDD-PL0-Out-2',
                                'PMIC-11-ASIC4-HVDD-PL0-Out-1', 'PMIC-12-12V-HVDD-DVDD-ASIC4-In-1',
                                'PMIC-12-ASIC4-DVDD-PL1-Out-2', 'PMIC-12-ASIC4-HVDD-PL1-Out-1', 'PMIC-13-12V-MAIN-In-1',
                                'PMIC-13-CEX-VDD-Out-1', 'PSU-1-12V-Out', 'PSU-2-12V-Out', 'PSU-3-12V-Out',
                                'PSU-4-12V-Out', 'PSU-5-12V-Out', 'PSU-6-12V-Out', 'PSU-7-12V-Out', 'PSU-8-12V-Out']

        self.stats_disk_header_num_of_lines = 16
        self.stats_cpu_header_num_of_lines = 39
        self.stats_temperature_header_num_of_lines = 45
        self.fw_versions_json_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/black_mamba_versions.json"
        self.allow_cpld_update = True
        self.system_profile_default_values = ['enabled', '1792', 'enabled', 'disabled', '1']
        self.mst_dev_name = tuple(f'/dev/mst/mt54004_pciconf{i}' for i in [2, 1, 0, 3])
        self.valid_ports_count = 144
        self.number_of_transceivers = 145
        self.transceivers_tables_name = "TRANSCEIVER_FIRMWARE_INFO"
        self.transceiver_list = ['fnm1'] + [f'sw{a + 1}' for a in range(72)]
        self.expected_operation_durations.update({
            "Install BIOS": 550,
            'install cpld': 1000,
            'install ssd': 20,
            self.generate_tech_support: 105,
            InstallSteps.SYSTEM_IS_READY_AFTER_MANUFACTURE: 14.5 * MINUTE,
            InstallSteps.SYSTEM_IS_READY_AFTER_UPGRADE: 10 * MINUTE,
            'ISSU CPU max downtime': 135,
        })
        self.memory_speed = 2667  # in MT/s
        self.ib_host_player = 'hfnm'
        self.memory_size: list[float] = [30.73]
        self.supported_disk_list: list[SSDConsts.SSDType] = [SSDConsts.VIRTIUM_VTPM24CEXI08_BM110006]

    def get_mgmt_ports(self) -> list[str]:
        return self.mgmt_ports

    def _init_fan_list(self):
        super()._init_fan_list()
        self.fan_list += ["FAN7/1", "FAN7/2", "FAN8/1", "FAN8/2", "FAN9/1", "FAN9/2", "FAN10/1", "FAN10/2"]

    def _init_led_list(self):
        super()._init_led_list()
        self.led_list += ['FAN7', 'FAN8', 'FAN9', 'FAN10']

    def _init_psu_list(self):
        super()._init_psu_list()
        self.psu_list += ["PSU3", "PSU4", "PSU5", "PSU6", "PSU7", "PSU8"]
        self.psu_fan_list += ["PSU3/FAN", "PSU4/FAN", "PSU5/FAN", "PSU6/FAN", "PSU7/FAN", "PSU8/FAN"]

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = [
            'ASIC1', 'ASIC2', 'ASIC3', 'ASIC4', 'Ambient-Fan-Side-Temp', 'Ambient-Port-Side-Temp', 'PCH-Temp',
            'CPU-Core-0-Temp', 'CPU-Core-1-Temp', 'CPU-Core-2-Temp', 'CPU-Core-3-Temp', 'CPU-Pack-Temp', 'Drive-Temp',
            'PMIC-1-Temp', 'PMIC-2-Temp', 'PMIC-3-Temp', 'PMIC-4-Temp', 'PMIC-5-Temp', 'PMIC-6-Temp', 'PMIC-7-Temp',
            'PMIC-8-Temp', 'PMIC-9-Temp', 'PMIC-10-Temp', 'PMIC-11-Temp', 'PMIC-12-Temp', 'PMIC-13-Temp',
            'PSU-1-Temp', 'PSU-2-Temp', 'PSU-3-Temp', 'PSU-4-Temp', 'PSU-5-Temp', 'PSU-6-Temp', 'PSU-7-Temp', 'PSU-8-Temp',
            'SODIMM-1-Temp', 'SODIMM-2-Temp']

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK.lower(), "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=2000, range_max=10000),
            "max-speed": ExpectedString(range_min=20000, range_max=40000)}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": None})
        self._extend_firmware_by_cpld_amount()

    def _init_interface_lists(self):
        super()._init_interface_lists()
        self.ib_ports_num = 2 * 72
        self.mgmt_ports = ['eth0', 'eth1']
        ib_ports = self.fnm_external_port_list + [f'sw{a + 1}p{b}' for a in range(self.ib_ports_num // 2) for b in (1, 2)]
        # = ['fnm1', 'sw1p1', 'sw1p2', ..., 'sw72p1', 'sw72p2']
        self.interface_list = self.network_ports + ib_ports + ['eth1']
        self.interface_fae_list = (
            self.interface_list +
            [f'{p}pl{pl + 1}' for p in ib_ports for pl in range(self.asic_amount)] +  # e.g. sw7p1 - sw7p4
            [f'fnma{pl + 1}p{i + 1}' for i in range(3) for pl in range(self.asic_amount)])  # fnma1p1 - fnma4p3
        self.interface_active_internal_fnm_ports = {'fnma1p1', 'fnma1p2', 'fnma2p1', 'fnma2p2', 'fnma3p1', 'fnma3p2',
                                                    'fnma4p1', 'fnma4p2'}
        # because other internal fnm ports are unused currently
        self.fnm_link_speed = '800G'
        self.fnm_internal_link_speed = '50G'

    def _init_interface_attributes_mapping_dict(self):
        super()._init_interface_attributes_mapping_dict()
        self.interface_attributes_mapping_dict.update({
            IbInterfaceConsts.LINK_MTU: GnmiConstants.MTU,
            IbInterfaceConsts.LINK_OPERATIONAL_VLS: GnmiConstants.OPERATIONAL_VL,
            IbInterfaceConsts.LINK_LANES: GnmiConstants.WIDTH,
            IbInterfaceConsts.LINK_IB_SPEED: GnmiConstants.IB_SPEED,
            IbInterfaceConsts.LINK_SUPPORTED_IB_SPEEDS: GnmiConstants.SUPPORTED_IB_SPEEDS,
            IbInterfaceConsts.LINK_IB_SUBNET: GnmiConstants.IB_SUBNET,
        })

    def _init_eth0_speeds(self):
        super()._init_eth0_speeds()
        self.supported_eth0_speeds += ['10M']

    def _init_ib_speeds(self):
        super()._init_ib_speeds()
        # BlackMamba traffic ports are wired with XDR-only optical modules (e.g. 980-9IAS0-00XM0N,
        # rated IB-XDR). Although the ASIC advertises supported-ib-speed sdr,hdr,ndr,xdr, the installed
        # optics cannot train a link at the lower generations: setting a lower ib-speed leaves the port
        # stuck Down with PHY reason Cable_was_unplugged. So only 'xdr' is actually configurable here, and
        # test_ib_interface_speed skips via the len(supported_ib_speeds) <= 1 guard (same as Taipan).
        # See Redmine #4940283 (supported-ib-speed advertises speeds that cannot be configured).
        self.supported_ib_speeds = ('xdr',)
        self.supported_fnm_ib_speeds = ('sdr', 'hdr', 'ndr', 'xdr')  # BlackMamba FNM also supports XDR
        self.supported_internal_fnm_ib_speeds = ('sdr', 'hdr')  # Internal FNM has fewer speeds

    def _init_interfaces_ib_lanes(self):
        super()._init_interfaces_ib_lanes()
        self.supported_fnm_lanes = '4X'  # BlackMamba regular FNM
        self.supported_internal_fnm_lanes = '1X'  # BlackMamba internal FNM

    def _init_boot_time_timeouts(self):
        super()._init_boot_time_timeouts()
        self.timeout_system_is_ready = 15 * MINUTE

    @classmethod
    def _get_lane_bmap(cls, port):
        return 0x10 ** (port.local_port - 1) * 2 ** (port.plane_number - 1)

    def convert_port_to_infiniband(self, port_name: str) -> str:
        """
        Converts a port name to Infiniband port name for Black Mamba switches.

        For Black Mamba (BM): Each physical switch port has 2 subports (p1, p2),
        so Infiniband number = (port_number - 1) * 2 + (local_port - 1)
        Example: sw1p1 -> Infiniband0, sw1p2 -> Infiniband1, sw30p2 -> Infiniband59

        Args:
            port_name: Port name (e.g., "sw1p1", "sw72p2", "fnm1")

        Returns:
            str: Infiniband port name (e.g., "Infiniband0", "Infiniband59")
        """
        from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port

        # Handle FNM ports (if needed, can be extended based on requirements)
        if port_name.startswith('fnm'):
            logger.warning(f"FNM port {port_name} conversion not implemented for BM, returning as-is")
            return port_name

        try:
            # Use Port.parse_port_name to extract port components
            asic_letter, port_number, local_port, split_number, plane_number = Port.parse_port_name(port_name)

            # For BM: each switch port has 2 subports, so ib_num accounts for both
            ib_num = (port_number - 1) * 2 + (local_port - 1)

            return f"Infiniband{ib_num}"
        except ValueError as e:
            logger.error(f"Failed to parse port name {port_name} for BM switch: {e}")
            return port_name


# -------------------------- BlackMambaDGX Switch ----------------------------
class BlackMambaDGXSwitch(BlackMambaSwitch):

    def __init__(self, switch_class=NvosConst.BLACK_MAMBA_DGX_SWITCH):
        super().__init__(switch_class=switch_class)
        SwitchCapabilityHandler.apply_capability(self, NoPSUCapability())

    def _init_fan_list(self):
        super()._init_fan_list()
        remove = {"FAN9/1", "FAN9/2", "FAN10/1", "FAN10/2"}
        self.fan_list = [f for f in self.fan_list if f not in remove]

    def _init_led_list(self):
        super()._init_led_list()
        self.led_list = ['FAN1', 'FAN2', 'FAN3', 'FAN4', 'FAN5', 'FAN6', "FAN7", "FAN8", "STATUS", "UID"]

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = [
            'ASIC1', 'ASIC2', 'ASIC3', 'ASIC4', 'Ambient-Fan-Side-Temp', 'Ambient-Port-Side-Temp', 'PCH-Temp',
            'CPU-Core-0-Temp', 'CPU-Core-1-Temp', 'CPU-Core-2-Temp', 'CPU-Core-3-Temp', 'CPU-Pack-Temp', 'Drive-Temp',
            'PMIC-1-Temp', 'PMIC-2-Temp', 'PMIC-3-Temp', 'PMIC-4-Temp', 'PMIC-5-Temp', 'PMIC-6-Temp', 'PMIC-7-Temp',
            'PMIC-8-Temp', 'PMIC-9-Temp', 'PMIC-10-Temp', 'PMIC-11-Temp', 'PMIC-12-Temp', 'PMIC-13-Temp',
            'SODIMM-1-Temp', 'SODIMM-2-Temp']

    def _init_constants(self):
        super()._init_constants()
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "Q3401_RD",
            "asic-model": self.asic_type,
        })
        self.stats_temperature_header_num_of_lines = 35
        self.voltage_sensors = [
            # PMIC sensors for ASIC1
            'PMIC-1-12V-VDD-ASIC1-In-1', 'PMIC-1-ASIC1-VDD-Out-1',
            'PMIC-2-12V-HVDD-DVDD-ASIC1-In-1', 'PMIC-2-ASIC1-DVDD-PL0-Out-2',
            'PMIC-2-ASIC1-HVDD-PL0-Out-1', 'PMIC-3-12V-HVDD-DVDD-ASIC1-In-1',
            'PMIC-3-ASIC1-DVDD-PL1-Out-2', 'PMIC-3-ASIC1-HVDD-PL1-Out-1',
            # PMIC sensors for ASIC2
            'PMIC-4-12V-VDD-ASIC2-In-1', 'PMIC-4-ASIC2-VDD-Out-1',
            'PMIC-5-12V-HVDD-DVDD-ASIC2-In-1', 'PMIC-5-ASIC2-DVDD-PL0-Out-2',
            'PMIC-5-ASIC2-HVDD-PL0-Out-1', 'PMIC-6-12V-HVDD-DVDD-ASIC2-In-1',
            'PMIC-6-ASIC2-DVDD-PL1-Out-2', 'PMIC-6-ASIC2-HVDD-PL1-Out-1',
            # PMIC sensors for ASIC3
            'PMIC-7-12V-VDD-ASIC3-In-1', 'PMIC-7-ASIC3-VDD-Out-1',
            'PMIC-8-12V-HVDD-DVDD-ASIC3-In-1', 'PMIC-8-ASIC3-DVDD-PL0-Out-2',
            'PMIC-8-ASIC3-HVDD-PL0-Out-1', 'PMIC-9-12V-HVDD-DVDD-ASIC3-In-1',
            'PMIC-9-ASIC3-DVDD-PL1-Out-2', 'PMIC-9-ASIC3-HVDD-PL1-Out-1',
            # PMIC sensors for ASIC4
            'PMIC-10-12V-VDD-ASIC4-In-1', 'PMIC-10-ASIC4-VDD-Out-1',
            'PMIC-11-12V-HVDD-DVDD-ASIC4-In-1', 'PMIC-11-ASIC4-DVDD-PL0-Out-2',
            'PMIC-11-ASIC4-HVDD-PL0-Out-1', 'PMIC-12-12V-HVDD-DVDD-ASIC4-In-1',
            'PMIC-12-ASIC4-DVDD-PL1-Out-2', 'PMIC-12-ASIC4-HVDD-PL1-Out-1',
            # PMIC sensors for main system
            'PMIC-13-12V-MAIN-In-1', 'PMIC-13-CEX-VDD-Out-1',
            # Additional voltage sensors from nv show platform environment output
            'FAN-HSC1-Volt-In', 'FAN-HSC1-Volt-Out',
            'HSC1-VinDC-In', 'HSC1-VinDC-Out',
            'HSC2-VinDC-In', 'HSC2-VinDC-Out',
            'PDB-1-Conv-In-1', 'PDB-1-Conv-Out-1'
        ]
        self.fw_versions_json_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/black_mamba_dgx_versions.json"
        self.memory_size: list[float] = [31.17, 30.73]

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK.lower(),
            "direction": 'F2B',
            "current-speed": ExpectedString(range_min=2741, range_max=15525),
            "min-speed": ExpectedString(regex="(2741|2842)"),
            "max-speed": ExpectedString(regex="(15525|14493)")}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": None})
# -------------------------- Taipan Switch ----------------------------


class TaipanSwitch(IbSwitch):
    els_index_to_ga: dict[int, int]

    def __init__(self, switch_class=NvosConst.TAIPAN_SWITCH):
        super().__init__(asic_amount=4, switch_class=switch_class)
        SwitchCapabilityHandler.apply_capability(self, CpoCapability(
            els_index_to_ga={
                1: 1, 2: 2, 3: 1, 4: 2, 5: 1, 6: 2, 7: 1, 8: 2,
                9: 1, 10: 3, 11: 0, 12: 3, 13: 0, 14: 3, 15: 0, 16: 3, 17: 0, 18: 3
            },
            pmaos_module_offset=71,
            number_of_lasers_per_els=8,
            els_port_mapping={
                'els1': ['sw137p1', 'sw138p1', 'sw139p1', 'sw140p1', 'sw141p1', 'sw142p1', 'sw143p1', 'sw144p1'],
                'els2': ['sw129p1', 'sw130p1', 'sw131p1', 'sw132p1', 'sw133p1', 'sw134p1', 'sw135p1', 'sw136p1'],
                'els3': ['sw121p1', 'sw122p1', 'sw123p1', 'sw124p1', 'sw125p1', 'sw126p1', 'sw127p1', 'sw128p1'],
                'els4': ['sw113p1', 'sw114p1', 'sw115p1', 'sw116p1', 'sw117p1', 'sw118p1', 'sw119p1', 'sw120p1'],
                'els5': ['sw105p1', 'sw106p1', 'sw107p1', 'sw108p1', 'sw109p1', 'sw110p1', 'sw111p1', 'sw112p1'],
                'els6': ['sw97p1', 'sw98p1', 'sw99p1', 'sw100p1', 'sw101p1', 'sw102p1', 'sw103p1', 'sw104p1'],
                'els7': ['sw89p1', 'sw90p1', 'sw91p1', 'sw92p1', 'sw93p1', 'sw94p1', 'sw95p1', 'sw96p1'],
                'els8': ['sw81p1', 'sw82p1', 'sw83p1', 'sw84p1', 'sw85p1', 'sw86p1', 'sw87p1', 'sw88p1'],
                'els9': ['sw73p1', 'sw74p1', 'sw75p1', 'sw76p1', 'sw77p1', 'sw78p1', 'sw79p1', 'sw80p1'],
                'els10': ['sw65p1', 'sw66p1', 'sw67p1', 'sw68p1', 'sw69p1', 'sw70p1', 'sw71p1', 'sw72p1'],
                'els11': ['sw57p1', 'sw58p1', 'sw59p1', 'sw60p1', 'sw61p1', 'sw62p1', 'sw63p1', 'sw64p1'],
                'els12': ['sw49p1', 'sw50p1', 'sw51p1', 'sw52p1', 'sw53p1', 'sw54p1', 'sw55p1', 'sw56p1'],
                'els13': ['sw41p1', 'sw42p1', 'sw43p1', 'sw44p1', 'sw45p1', 'sw46p1', 'sw47p1', 'sw48p1'],
                'els14': ['sw33p1', 'sw34p1', 'sw35p1', 'sw36p1', 'sw37p1', 'sw38p1', 'sw39p1', 'sw40p1'],
                'els15': ['sw25p1', 'sw26p1', 'sw27p1', 'sw28p1', 'sw29p1', 'sw30p1', 'sw31p1', 'sw32p1'],
                'els16': ['sw17p1', 'sw18p1', 'sw19p1', 'sw20p1', 'sw21p1', 'sw22p1', 'sw23p1', 'sw24p1'],
                'els17': ['sw9p1', 'sw10p1', 'sw11p1', 'sw12p1', 'sw13p1', 'sw14p1', 'sw15p1', 'sw16p1'],
                'els18': ['sw1p1', 'sw2p1', 'sw3p1', 'sw4p1', 'sw5p1', 'sw6p1', 'sw7p1', 'sw8p1'],
            },
            els_oe_mapping={
                'els1': ['oe10', 'oe28', 'oe45', 'oe63'], 'els2': ['oe11', 'oe29', 'oe44', 'oe62'],
                'els3': ['oe12', 'oe30', 'oe43', 'oe61'], 'els4': ['oe13', 'oe31', 'oe42', 'oe60'],
                'els5': ['oe14', 'oe32', 'oe41', 'oe59'], 'els6': ['oe15', 'oe33', 'oe40', 'oe58'],
                'els7': ['oe16', 'oe34', 'oe39', 'oe57'], 'els8': ['oe17', 'oe35', 'oe38', 'oe56'],
                'els9': ['oe18', 'oe36', 'oe37', 'oe55'], 'els10': ['oe1', 'oe19', 'oe54', 'oe72'],
                'els11': ['oe2', 'oe20', 'oe53', 'oe71'], 'els12': ['oe3', 'oe21', 'oe52', 'oe70'],
                'els13': ['oe4', 'oe22', 'oe51', 'oe69'], 'els14': ['oe5', 'oe23', 'oe50', 'oe68'],
                'els15': ['oe6', 'oe24', 'oe49', 'oe67'], 'els16': ['oe7', 'oe25', 'oe48', 'oe66'],
                'els17': ['oe8', 'oe26', 'oe47', 'oe65'], 'els18': ['oe9', 'oe27', 'oe46', 'oe64'],
            },
        ))
        SwitchCapabilityHandler.apply_capability(self, NoPSUCapability())

    def _init_constants(self):
        super()._init_constants()
        # Multi-ASIC / QTM3 constants
        self.supports_ssd_upgrade = True
        self.ib_ports_num = 144
        self.core_count = 4
        self.asic_type = NvosConst.QTM3
        self.multi_planar = True
        self.platform_file_path = MultiPlanarConsts.PLATFORM_FILE_FULL_PATH.format("x86_64-nvidia_q3450_ld-r0")
        self.asic_version = BaseSwitch.AsicImageConsts(
            version="35.2014.2012",
            filename="fw-QTM3-rel-35_2014_2012.mfa"
        )
        self.allow_cpld_update = True
        self.mst_dev_name = tuple(f'/dev/mst/mt54004_pciconf{i}' for i in [2, 1, 0, 3])
        self.valid_ports_count = 144
        self.stats_disk_header_num_of_lines = 16
        self.stats_cpu_header_num_of_lines = 39
        self.stats_temperature_header_num_of_lines = 45
        self.system_profile_default_values = ['enabled', '1792', 'enabled', 'disabled', '1']
        self.expected_operation_durations.update({
            "Install BIOS": 550,
            'install cpld': 1000,
            'install ssd': 20,
            self.generate_tech_support: 105,
            InstallSteps.SYSTEM_IS_READY_AFTER_MANUFACTURE: 14.5 * MINUTE,
            InstallSteps.SYSTEM_IS_READY_AFTER_UPGRADE: 10 * MINUTE,
            'ISSU CPU max downtime': 135,
        })
        self.memory_speed = 2667  # in MT/s
        self.memory_size: list[float] = [30.73]
        self.supported_disk_list: list[SSDConsts.SSDType] = [SSDConsts.VIRTIUM_VTPM24CEXI08_BM110006]
        # Taipan-specific constants
        self.number_of_transceivers = 18
        self.transceivers_tables_name = "TRANSCEIVER_INFO"
        self.transceiver_list = [f'els{a + 1}' for a in range(18)] + ['fnm1'] + [f'oe{b + 1}' for b in range(72)]
        self.ib_host_player = 'hfnm'
        self.cpld_amount = 7
        self._extend_firmware_by_cpld_amount()
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "Q3450_LD",
            "asic-model": self.asic_type,
        })
        # Taipan-specific expected module status values
        self.expected_module_status_dict = {
            'transceiver_no_cable_diagnostic_status': PlatformConsts.HARDWARE_TRANCEIVER_DIAGNOSTIC_DATA_AVAILABLE,
            'link_diagnostics_unplugged_port': IbInterfaceConsts.LINK_DIAGNOSTICS_SIGNAL_NOT_DETECTED,
        }
        self.voltage_sensors = [
            "HSC-1-VinDC-In", "HSC-1-VinDC-Out", "HSC-2-VinDC-In", "HSC-2-VinDC-Out", "HSCC-1-Conv-In-1",
            "HSCC-1-Conv-Out-1", "HSCC-2-Conv-In-1", "HSCC-2-Conv-Out-1", "PMIC-1-12V-VDD-ASIC1-In-1",
            "PMIC-1-ASIC1-VDD-Out-1", "PMIC-2-12V-HVDD-DVDD-ASIC1-In-1", "PMIC-2-ASIC1-DVDD-PL0-Out-2",
            "PMIC-2-ASIC1-HVDD-PL0-Out-1", "PMIC-3-12V-HVDD-DVDD-ASIC1-In-1", "PMIC-3-ASIC1-DVDD-PL1-Out-2",
            "PMIC-3-ASIC1-HVDD-PL1-Out-1", "PMIC-4-12V-VDD-ASIC2-In-1", "PMIC-4-ASIC2-VDD-Out-1",
            "PMIC-5-12V-HVDD-DVDD-ASIC2-In-1", "PMIC-5-ASIC2-DVDD-PL0-Out-2", "PMIC-5-ASIC2-HVDD-PL0-Out-1",
            "PMIC-6-12V-HVDD-DVDD-ASIC2-In-1", "PMIC-6-ASIC2-DVDD-PL1-Out-2", "PMIC-6-ASIC2-HVDD-PL1-Out-1",
            "PMIC-7-12V-VDD-ASIC3-In-1", "PMIC-7-ASIC3-VDD-Out-1", "PMIC-8-12V-HVDD-DVDD-ASIC3-In-1",
            "PMIC-8-ASIC3-DVDD-PL0-Out-2", "PMIC-8-ASIC3-HVDD-PL0-Out-1", "PMIC-9-12V-HVDD-DVDD-ASIC3-In-1",
            "PMIC-9-ASIC3-DVDD-PL1-Out-2", "PMIC-9-ASIC3-HVDD-PL1-Out-1", "PMIC-10-12V-VDD-ASIC4-In-1",
            "PMIC-10-ASIC4-VDD-Out-1", "PMIC-11-12V-HVDD-DVDD-ASIC4-In-1", "PMIC-11-ASIC4-DVDD-PL0-Out-2",
            "PMIC-11-ASIC4-HVDD-PL0-Out-1", "PMIC-12-12V-HVDD-DVDD-ASIC4-In-1", "PMIC-12-ASIC4-DVDD-PL1-Out-2",
            "PMIC-12-ASIC4-HVDD-PL1-Out-1", "PMIC-13-12V-MAIN-In-1", "PMIC-13-CEX-VDD-Out-1",
        ]
        self.fw_versions_json_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/taipan_versions.json"
        self.unsupported_commands_list.remove("nv show platform environment leakage")

    def _init_interface_lists(self):
        super()._init_interface_lists()
        self.ib_ports_num = 144
        self.mgmt_ports = ['eth0', 'eth1']
        ib_ports = self.fnm_external_port_list + [f'sw{a + 1}p1' for a in range(self.ib_ports_num)]
        self.interface_list = self.network_ports + ib_ports + ['eth1']
        self.interface_fae_list = (
            self.interface_list +
            [f'{p}pl{pl + 1}' for p in ib_ports for pl in range(self.asic_amount)] +
            [f'fnma{pl + 1}p{i + 1}' for i in range(3) for pl in range(self.asic_amount)])
        self.interface_active_internal_fnm_ports = {'fnma1p1', 'fnma1p2', 'fnma2p1', 'fnma2p2',
                                                    'fnma3p1', 'fnma3p2', 'fnma4p1', 'fnma4p2'}
        self.fnm_link_speed = '800G'
        self.fnm_internal_link_speed = '50G'

    def get_mgmt_ports(self) -> list[str]:
        return self.mgmt_ports

    def _init_eth0_speeds(self):
        super()._init_eth0_speeds()
        self.supported_eth0_speeds += ['10M']

    def _init_ib_speeds(self):
        super()._init_ib_speeds()
        self.supported_ib_speeds = ('xdr',)  # Taipan FW only supports xdr, ib-speed is not configurable
        self.supported_fnm_ib_speeds = ('sdr', 'hdr', 'ndr', 'xdr')
        self.supported_internal_fnm_ib_speeds = ('sdr', 'hdr')

    def _init_interfaces_ib_lanes(self):
        super()._init_interfaces_ib_lanes()
        self.supported_fnm_lanes = '4X'
        self.supported_internal_fnm_lanes = '1X'

    def _init_interface_attributes_mapping_dict(self):
        super()._init_interface_attributes_mapping_dict()
        self.interface_attributes_mapping_dict.update({
            IbInterfaceConsts.LINK_MTU: GnmiConstants.MTU,
            IbInterfaceConsts.LINK_OPERATIONAL_VLS: GnmiConstants.OPERATIONAL_VL,
            IbInterfaceConsts.LINK_LANES: GnmiConstants.WIDTH,
            IbInterfaceConsts.LINK_IB_SPEED: GnmiConstants.IB_SPEED,
            IbInterfaceConsts.LINK_SUPPORTED_IB_SPEEDS: GnmiConstants.SUPPORTED_IB_SPEEDS,
            IbInterfaceConsts.LINK_IB_SUBNET: GnmiConstants.IB_SUBNET,
        })

    def _relevant_config_filename_by_version(self, version: str) -> str:
        return 'nvos_config_ga_7400.yml'

    def _init_boot_time_timeouts(self):
        super()._init_boot_time_timeouts()
        self.timeout_system_is_ready = 15 * MINUTE
        self.cli_up_retries = 44  # 220s - Taipan NVUE takes longer after reboot

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK.lower(),
            "direction": 'F2B',
            "current-speed": ExpectedString(range_min=4140, range_max=15870),
            "min-speed": '4140',
            "max-speed": '15870'}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": None})

    def _init_fan_list(self):
        super()._init_fan_list()
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2"]

    def _init_led_list(self):
        super()._init_led_list()
        self.led_list = ['FAN1', 'FAN2', 'STATUS', 'UID']

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = [
            "ASIC1", "ASIC2", "ASIC3", "ASIC4", "Ambient-Port-Side-Temp",
            "CPU-Core-0-Temp", "CPU-Core-1-Temp", "CPU-Core-2-Temp", "CPU-Core-3-Temp",
            "CPU-Pack-Temp", "Drive-Temp", "PCH-Temp",
            "PMIC-1-Temp", "PMIC-2-Temp", "PMIC-3-Temp", "PMIC-4-Temp", "PMIC-5-Temp", "PMIC-6-Temp", "PMIC-7-Temp",
            "PMIC-8-Temp", "PMIC-9-Temp", "PMIC-10-Temp", "PMIC-11-Temp", "PMIC-12-Temp", "PMIC-13-Temp",
            "SODIMM-1-Temp", "SODIMM-2-Temp"]

    def get_mst_device_for_els_index(self, els_index):
        """Get MST device path for a given ELS index using platform-specific GA mapping."""
        if els_index not in self.els_index_to_ga:
            raise ValueError(f"Invalid ELS index: {els_index}. Valid: {sorted(self.els_index_to_ga.keys())}")
        ga_value = self.els_index_to_ga[els_index]
        return f"/dev/mst/mt54004_pciconf{(ga_value + 1) % 4}"

    @classmethod
    def _get_lane_bmap(cls, port):
        # For Taipan, lane_bmap cycles through 8 values per module based on port_number
        # Each module has 8 ports: 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80
        lane_index = (port.port_number - 1) % 8
        return 2 ** lane_index

    def convert_port_to_infiniband(self, port_name: str) -> str:
        """
        Converts a port name to Infiniband port name for Taipan switches.

        For Taipan: Each switch has a single port (p1), so Infiniband number = port_number - 1
        Example: sw1p1 -> Infiniband0, sw144p1 -> Infiniband143

        Args:
            port_name: Port name (e.g., "sw1p1", "sw144p1", "fnm1")

        Returns:
            str: Infiniband port name (e.g., "Infiniband0", "Infiniband143")
        """
        from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port

        if port_name.startswith('fnm'):
            logger.warning(f"FNM port {port_name} conversion not implemented for Taipan, returning as-is")
            return port_name

        try:
            asic_letter, port_number, local_port, split_number, plane_number = Port.parse_port_name(port_name)
            ib_num = port_number - 1
            return f"Infiniband{ib_num}"
        except ValueError as e:
            logger.error(f"Failed to parse port name {port_name} for Taipan switch: {e}")
            return port_name

    def recover_after_fw_crash(self, engine, system):
        """Recover after FW crash on Taipan (CPO) systems.

        On CPO systems, docker restart retries are set to 0, so a FW crash
        triggers an automatic system reboot. We follow the full reboot recovery
        flow (no-ping → ping → NVOS functional) without issuing the reboot command.
        """
        with allure.step('Wait for auto-reboot to complete after FW crash'):
            logger.info("CPO system - FW crash triggers auto-reboot, waiting for system recovery")
            DutUtilsTool.wait_on_system_reboot(engine, device=self)


# -------------------------- Crocodile Switch ----------------------------
class CrocodileSwitch(IbSwitch):

    def __init__(self):
        super().__init__(asic_amount=2, switch_class=NvosConst.CROCODILE_SWITCH)

    def _init_constants(self):
        super()._init_constants()
        self.use_ga_default_config = True
        self.supports_ssd_upgrade = True
        self.asic_numbers = [f"ASIC{i}" for i in range(1, self.asic_amount + 1)]
        self.ib_ports_num = 64
        self.core_count = 4
        self.split_ports_supported = True
        self.asic_type = NvosConst.QTM3
        self.supported_lanes = '4X'  # Crocodile regular IB ports
        self.supported_fnm_lanes = '1X,2X'  # Crocodile regular FNM
        self.supported_internal_fnm_lanes = '1X,2X'  # Crocodile internal FNM (same as regular FNM)
        self.platform_file_path = MultiPlanarConsts.PLATFORM_FILE_FULL_PATH.format("x86_64-nvidia_qm3400-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: ExpectedString(regex="(QM3400|Q3200_RA)"),
            "asic-model": self.asic_type,
        })
        self.asic_version = BaseSwitch.AsicImageConsts(
            version="35.2014.2012",
            filename="fw-QTM3-rel-35_2014_2012.mfa"
        )
        self.mst_dev_name = tuple(f'/dev/mst/mt54004_pciconf{i}' for i in [1, 0])
        self.voltage_sensors = ['PMIC-1-12V-VDD-ASIC1-In-1', 'PMIC-1-ASIC1-VDD-Out-1',
                                'PMIC-2-12V-HVDD-DVDD-ASIC1-In-1', 'PMIC-2-ASIC1-DVDD-PL0-Out-2',
                                'PMIC-2-ASIC1-HVDD-PL0-Out-1', 'PMIC-3-12V-HVDD-DVDD-ASIC1-In-1',
                                'PMIC-3-ASIC1-DVDD-PL1-Out-2', 'PMIC-3-ASIC1-HVDD-PL1-Out-1',
                                'PMIC-4-12V-VDD-ASIC2-In-1', 'PMIC-4-ASIC2-VDD-Out-1',
                                'PMIC-5-12V-HVDD-DVDD-ASIC2-In-1', 'PMIC-5-ASIC2-DVDD-PL0-Out-2',
                                'PMIC-5-ASIC2-HVDD-PL0-Out-1', 'PMIC-6-12V-HVDD-DVDD-ASIC2-In-1',
                                'PMIC-6-ASIC2-DVDD-PL1-Out-2', 'PMIC-6-ASIC2-HVDD-PL1-Out-1', 'PMIC-7-12V-MAIN-In-1',
                                'PMIC-7-CEX-VDD-Out-1', 'PSU-1-12V-Out', 'PSU-2-12V-Out', 'PSU-3-12V-Out',
                                'PSU-4-12V-Out']
        self.stats_disk_header_num_of_lines = 16
        self.system_profile_default_values = ['enabled', '1792', 'enabled', 'disabled', '1']
        self.stats_cpu_header_num_of_lines = 39
        self.stats_power_header_num_of_lines = 17
        self.stats_temperature_header_num_of_lines = 32
        self.cpld_amount = 4
        self.valid_ports_count = 73
        self.number_of_transceivers = 73
        self.transceivers_tables_name = "TRANSCEIVER_FIRMWARE_INFO"
        self.transceiver_list = ['fnm1'] + [f'swA{a + 1}' for a in range(18)] + [f'swB{b + 1}' for b in range(18)]
        self.allow_cpld_update = True
        self.fw_versions_json_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/crocodile_versions.json"
        self.expected_operation_durations.update({
            "Install BIOS": 500,
            'install cpld': 720,
            'install ssd': 20,
            self.generate_tech_support: 75,
            InstallSteps.SYSTEM_IS_READY_AFTER_MANUFACTURE: 13.5 * MINUTE,
            InstallSteps.SYSTEM_IS_READY_AFTER_UPGRADE: 9 * MINUTE,
            'ISSU CPU max downtime': 120,
        })
        self.unsupported_commands_list.extend(["nv show ib device ASIC3",
                                               "nv show ib device ASIC4"])
        self.memory_speed = 2667  # in MT/s
        self.port_range_group_separator = ','

    def get_mgmt_ports(self) -> list[str]:
        return self.mgmt_ports

    def _init_fan_list(self):
        super()._init_fan_list()
        self.fan_list.remove("FAN6/1")
        self.fan_list.remove("FAN6/2")

    def _init_led_list(self):
        super()._init_led_list()
        self.led_list.remove('FAN6')

    def _init_psu_list(self):
        super()._init_psu_list()
        self.psu_list += ["PSU3", "PSU4"]
        self.psu_fan_list += ["PSU3/FAN", "PSU4/FAN"]

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors += ["ASIC2", "PSU-3-Temp", "PSU-4-Temp", "PMIC-2-Temp", "PMIC-7-Temp"]

    def _init_eth0_speeds(self):
        super()._init_eth0_speeds()
        self.supported_eth0_speeds += ['10M']

    def _init_ib_speeds(self):
        super()._init_ib_speeds()
        self.supported_ib_speeds = ('sdr', 'hdr', 'ndr', 'xdr')  # Crocodile supports XDR
        self.supported_fnm_ib_speeds = ('sdr', 'hdr', 'ndr')  # Crocodile FNM (no XDR)
        self.supported_internal_fnm_ib_speeds = ('sdr', 'hdr')  # Crocodile internal FNM

    def _init_interfaces_ib_lanes(self):
        super()._init_interfaces_ib_lanes()
        self.supported_fnm_lanes = '1X,2X'  # Crocodile FNM
        self.supported_internal_fnm_lanes = '1X,2X'  # Crocodile internal FNM

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK.lower(), "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=2000, range_max=10000),
            "max-speed": ExpectedString(range_min=20000, range_max=40000)}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": None})
        self._extend_firmware_by_cpld_amount()

    def _init_interface_lists(self):
        super()._init_interface_lists()
        self.mgmt_ports = ['eth0', 'eth1']
        self.interface_active_internal_fnm_ports = {'fnma0p1', 'fnma1p1'}
        self.default_port = 'swA1p1'
        self.fnm_link_speed = '200G'
        self.fnm_internal_link_speed = '100G'
        traffic_ports = [f'sw{a}{n}p{p}' for a in 'AB' for n in range(1, 19) for p in (1, 2)]
        self.interface_list = self.network_ports + ['eth1', 'fnm1'] + traffic_ports
        self.interface_fae_list = (
            self.interface_list +
            ['fnma0p1', 'fnma0p2', 'fnma1p1'] +
            [f'{port}pl{i}' for port in traffic_ports for i in range(1, 5)]
        )

    def _init_interface_attributes_mapping_dict(self):
        super()._init_interface_attributes_mapping_dict()
        self.interface_attributes_mapping_dict.update({
            # IbInterfaceConsts.LINK_DUPLEX: '', #TODO: check if attributes exist in gnmi output in different names, otherwise delete
            IbInterfaceConsts.LINK_MTU: GnmiConstants.MTU,
            IbInterfaceConsts.LINK_OPERATIONAL_VLS: GnmiConstants.OPERATIONAL_VL,
            IbInterfaceConsts.LINK_LANES: GnmiConstants.WIDTH,
            IbInterfaceConsts.LINK_IB_SPEED: GnmiConstants.IB_SPEED,
            IbInterfaceConsts.LINK_SUPPORTED_IB_SPEEDS: GnmiConstants.SUPPORTED_IB_SPEEDS,
            IbInterfaceConsts.LINK_IB_SUBNET: GnmiConstants.IB_SUBNET,
            # IbInterfaceConsts.LINK_ROUND_TRIP_LATENCY: '' #TODO: check if attributes exist in gnmi output in different names, otherwise delete
        })

    def _init_boot_time_timeouts(self):
        super()._init_boot_time_timeouts()
        self.timeout_system_is_ready = 10 * MINUTE

    @classmethod
    def _get_lane_bmap(cls, port):
        # for a non-planarized port-name we assume a port with connection-mode ndr
        if port.plane_number or port.split_number:
            digit = (2 if port.split_number == 2 else 0) + (port.plane_number or 1)
            digit = 2 ** (digit - 1)
        else:
            digit = 0xF
        return 0x10 ** (port.local_port - 1) * digit

    def convert_port_to_infiniband(self, port_name: str) -> str:
        """
        Converts a port name to Infiniband port name for Crocodile switches.

        For Crocodile: Complex formula based on ASIC (A/B), cage number, and port number
        - If there's B: add 144
        - Cage number adds (cage_number - 1) * 8
        - If port number is 2: add 4

        Example: swB14p1 -> 144 + (14-1)*8 + 0 = 248
        Example: swA13p1 -> 0 + (13-1)*8 + 0 = 96
        Example: swB14p2 -> 144 + (14-1)*8 + 4 = 252

        Args:
            port_name: Port name (e.g., "swA1p1", "swB14p1", "fnm1")

        Returns:
            str: Infiniband port name (e.g., "Infiniband0", "Infiniband248")
        """
        from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port

        # Handle FNM ports (if needed)
        if port_name.startswith('fnm'):
            logger.warning(f"FNM port {port_name} conversion not implemented for Crocodile, returning as-is")
            return port_name

        try:
            # Use Port.parse_port_name to extract port components
            asic_letter, port_number, local_port, split_number, plane_number = Port.parse_port_name(port_name)

            # Calculate Infiniband number based on formula
            ib_num = 0

            # If there's B, add 144
            if asic_letter == 'B':
                ib_num += 144

            # Cage number (port_number) adds (cage_number - 1) * 8
            ib_num += (port_number - 1) * 8

            # If local port (p1/p2) is 2, add 4
            if local_port == 2:
                ib_num += 4

            logger.debug(f"Converted {port_name} -> Infiniband{ib_num} (asic={asic_letter}, cage={port_number}, port={local_port})")

            return f"Infiniband{ib_num}"
        except ValueError as e:
            logger.error(f"Failed to parse port name {port_name} for Crocodile switch: {e}")
            return port_name


# -------------------------- Crocodile Simx Switch ----------------------------
class CrocodileSimxSwitch(IbSwitch):

    def __init__(self):
        super().__init__(asic_amount=1, switch_class=NvosConst.CROCODILE_SWITCH)


# -------------------------- NvLink Switch ----------------------------
class NvLinkSwitch(IbSwitch):

    def __init__(self, asic_amount):
        super().__init__(switch_type=NvosConst.NVL_SWITCH_TYPE, asic_amount=asic_amount,
                         switch_class=NvosConst.JULIET_SWITCH)
        self.supports_mtu_testing = False

    def get_configurable_nvl_speeds(self):
        """
        NVL speeds safe to configure in tests.

        Defaults to supported_nvl_speeds (what the DUT advertises). Platforms that
        advertise a speed but cannot safely configure it without extra setup should
        override this (see RosalindRTF).
        """
        return list(getattr(self, 'supported_nvl_speeds', []))

    def _init_interface_lists(self):
        super()._init_interface_lists()
        self.mgmt_ports = ['eth0', 'eth1']

    def _init_ib_speeds(self):
        # NVL doesn't use IB speeds, but has FNM with different speed format
        self.supported_fnm_speeds = ['10G', '200G']  # Juliet FNM speeds (NVL format)

    def _init_interfaces_ib_lanes(self):
        self.supported_lanes = '2X'  # Juliet NVL ports
        self.supported_fnm_lanes = '2X'  # Juliet regular FNM
        self.supported_internal_fnm_lanes = '2X'  # Juliet internal FNM

    def _init_interface_attributes_mapping_dict(self):
        super()._init_interface_attributes_mapping_dict()
        self.interface_attributes_mapping_dict.update({
            # IbInterfaceConsts.LINK_SUPPORTED_SPEEDS: GnmiConstants.SUPPORTED_SPEED #TODO: check if attributes exist in gnmi output in different names, otherwise delete
        })

    def _init_constants(self):
        super()._init_constants()
        self.ib_ports_num = 64
        self.core_count = 4
        self.asic_type = NvosConst.QTM3
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-mlnx_mqm9700-r0")
        self.platform_file_path = MultiPlanarConsts.PLATFORM_FILE_FULL_PATH.format("x86_64-mlnx_mqm9700-r0")
        self.unset_all_command += "; nv unset cluster"
        self.unsupported_commands_list = ["nv show platform ps-redundancy",
                                          "nv show platform environment psu",
                                          "nv show ib device ASIC3",
                                          "nv show ib device ASIC4",
                                          "nv show system profile",
                                          "nv show ib ibdiagnet",
                                          # delayed-recovery only supported on croc+mamba
                                          "nv set fae interface {port} link delayed-recovery",
                                          "nv set fae interface {port} link phy-recovery logic-relock-mode",
                                          "nv set fae interface {port} link phy-recovery logic-relock-timeout",
                                          # Rosalind-only PHY recovery commands (not supported on juliet)
                                          "nv set fae interface {port} link phy-role",
                                          "nv set fae interface {port} link constant-role",
                                          "nv set fae interface {port} link phy-recovery link-down-timeout",
                                          "nv set fae interface {port} link phy-recovery recovery-neg-type",
                                          "nv set fae interface {port} link phy-recovery recovery-status",
                                          "nv set fae interface {port} link phy-recovery step-1",
                                          "nv set fae interface {port} link phy-recovery step-2",
                                          "nv show fae interface acp1 link low-power",
                                          "nv set fae interface acp1 link low-power state",
                                          "nv unset fae interface acp1 link low-power state"]
        # NVL7 CPO trunk-port PHY-recovery leaves + counters debug are pruned on NVL5.
        self.unsupported_commands_list.extend(PhyRecoveryConsts.NVL7_PRUNED_COMMANDS)
        self.mgmt_ports = ['eth0', 'eth1']
        self.default_phy_recovery_counters = {
            PhyRecoveryConsts.UNINTENTIONAL_LINK_DOWN_EVENTS: 0,
            PhyRecoveryConsts.TOTAL_SUCCESSFUL_RECOVERY_EVENTS: 0,
            PhyRecoveryConsts.SUCCESSFUL_RECOVERY_EVENTS: 0,
            PhyRecoveryConsts.INTENTIONAL_LINK_DOWN_EVENTS: 0,
            PhyRecoveryConsts.TIME_SINCE_LAST_RECOVERY: 0,
            PhyRecoveryConsts.TIME_BETWEEN_LAST_TWO_RECOVERIES: 0,
            PhyRecoveryConsts.TIME_IN_LAST_LOGIC_RECOVERY_EVENT: 0,
            PhyRecoveryConsts.TIME_IN_LAST_SERDES_EQ_RECOVERY_EVENT: 0,
            PhyRecoveryConsts.LAST_LOGIC_RECOVERY_ATTEMPTS: 0,
            PhyRecoveryConsts.LAST_SERDES_EQ_RECOVERY_ATTEMPTS: 0
        }

        self.memory_size: list[float] = [15.04]
        self.supported_disk_list: list[SSDConsts.SSDType] = [SSDConsts.VIRTIUM_VTPM24CEXI08_BM110006]

    def get_mgmt_ports(self) -> list[str]:
        return self.mgmt_ports


# -------------------------- Juliet Switch ----------------------------


class JulietSwitch(NvLinkSwitch):
    FaeImagesTestConsts = namedtuple('FaeImagesTestConsts', ('current_image_version', 'alternate_image_version'))
    NmxClusterAppsConsts = namedtuple('NmxClusterAppsConsts',
                                      ('burn_path', 'burn_version_names'))

    def __init__(self, asic_amount):
        super().__init__(asic_amount=asic_amount)

    def _init_constants(self):
        super()._init_constants()
        self.supports_ssd_upgrade = True
        self.asic_numbers = [f"ASIC{i}" for i in range(1, self.asic_amount + 1)]
        self.mgmt_ports = ['eth0', 'eth1']
        self.components_list = [FW_COMPONENT_CPLD,
                                FW_COMPONENT_BMC,
                                FW_COMPONENT_FPGA,
                                FW_COMPONENT_BIOS,
                                FW_COMPONENT_EROT]
        self.supported_nvl_speeds = ['200G', '400G']
        self.category_list = ['temperature', 'cpu', 'disk', 'fan', 'mgmt-interface', 'voltage']
        self.category_disabled_dict = {
            self.category_list[0]: self.category_temperature_default_disable_dict,
            self.category_list[1]: self.category_default_disabled_dict,
            self.category_list[2]: self.category_disk_default_disable_dict,
            self.category_list[3]: self.category_default_disabled_dict,
            self.category_list[4]: self.category_default_disabled_dict,
            self.category_list[5]: self.category_default_disabled_dict
        }
        self.category_list_default_dict = {
            self.category_list[0]: self.category_temperature_default_dict,
            self.category_list[1]: self.category_default_dict,
            self.category_list[2]: self.category_disk_default_dict,
            self.category_list[3]: self.category_default_dict,
            self.category_list[4]: self.category_default_dict,
            self.category_list[5]: self.category_default_dict
        }
        self.bmc_older_version_path = "/auto/sw_system_release/low_level/openbmc/88.0002.0472/dev/juliet-bmc/erot_sign_debug/cec1736-apfw-000201d8.fwpkg"
        self.fpga_older_version_path = "/auto/sw_system_release/fpga/juliet/V0_15/FPGA_juliet_0v15.fwpkg"
        self.has_nmx = True
        self.has_bmc = True
        self.show_platform_chassis_location_output = {
            ChassisLocationConsts.TRAY_ID: ExpectedString(range_min=-1, range_max=18),
            ChassisLocationConsts.SLOT_NUM: ExpectedString(range_min=0, range_max=28),
            ChassisLocationConsts.CHAS_SN: ExpectedString(regex="^\\d+$|^999WWYY123456$"),
            ChassisLocationConsts.TOPO_ID: ExpectedString(regex=f"^({'|'.join(ChassisLocationConsts.ALLOWED_TOPOLOGIES)})$")
        }
        self.show_platform_cable_cartridge_output = {
            CableCartridgeConsts.KEY_TRAY_ID: ExpectedString(range_min=-1, range_max=18),
            CableCartridgeConsts.KEY_SLOT_ID: ExpectedString(range_min=0, range_max=28),
            CableCartridgeConsts.KEY_SERIAL: ExpectedString(regex="^\\d+$|^999WWYY123456$"),
            CableCartridgeConsts.KEY_PART_NUMBER: ExpectedString(regex=f"^({'|'.join(CableCartridgeConsts.ALLOWED_PART_NUMBERS)})$"),
            CableCartridgeConsts.KEY_MANUFACTURING_DATE: ExpectedString(regex="^(0[1-9]|1[0-2])/([0-2][0-9]|3[0-1])/\\d{2} - ([0-1][0-9]|2[0-3]):([0-5][0-9]):([0-5][0-9])$")
        }
        cluster_files = ['conf', ClusterConsts.NMX_CONTROLLER, ClusterConsts.NMX_TELEMETRY]
        self.constants = self.constants._replace(cluster_files=cluster_files)
        bmc_dump_files = ['bmc_debug_log_dump.tar.zst']
        self.constants = self.constants._replace(bmc_dump_files=bmc_dump_files)
        self.constants.dump_files.append('BMCeeprom')
        self.constants.dump_files.remove('hdparm')

        # Define log files per cluster app (these are app-specific)
        self.cluster_log_files_by_app = {
            ClusterConsts.NMX_CONTROLLER: ['fabricmanager.log.gz', 'gwapi.log.gz', 'nvlsm.log.gz'],
            ClusterConsts.NMX_TELEMETRY: []  # TBD: Add telemetry-specific logs when identified
        }
        self.cluster_standalone_excluded_files = ['fabricmanager.log.gz']
        # Backward compatibility: extend generic log_nmx_files with all app logs
        for app_logs in self.cluster_log_files_by_app.values():
            if app_logs:  # Only extend if list is not empty
                self.constants.log_nmx_files.extend(app_logs)
        stats_dump_files = ["cpu.csv.gz", "disk.csv.gz", "fan.csv.gz",
                            "mgmt-interface.csv.gz", "temperature.csv.gz", "voltage.csv.gz"]
        self.constants = self.constants._replace(stats_dump_files=stats_dump_files)
        self.constants.erots.extend(
            [PlatformConsts.EROT_BMC_PATH_NAME, PlatformConsts.EROT_CPU_PATH_NAME, PlatformConsts.EROT_FPGA_PATH_NAME,
             PlatformConsts.EROT_ASIC1_PATH_NAME, PlatformConsts.EROT_ASIC2_PATH_NAME])

        self.nmx_cluster_apps_versions_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/nmx-versions/4300_versions.json"

        self.supported_commands.extend([ActionConsts.POWER_CYCLE])
        self.asic_version = BaseSwitch.AsicImageConsts(
            version="35.2014.1492",
            filename="fw-QTM3-rel-35_2014_1492.mfa"
        )
        self.bios_image_info = BaseSwitch.BiosImagesConsts(
            current_version={
                'path': '/auto/sw_system_release/sx_mlnx_bios/SnowyOwl/0ACTV_00.00.018/Release/erot_sign_debug/cec1736-apfw-0000012.fwpkg',
                'filename': 'cec1736-apfw-0000012.fwpkg',
                'version_name': '0ACTV_00.00.018d',
                'date': '08/21/2024'},
            alternate_version={
                'path': '/auto/sw_system_release/sx_mlnx_bios/SnowyOwl/0ACTV_00.00.018/Release/erot_sign_debug/cec1736-apfw-0000012.fwpkg',
                'filename': 'cec1736-apfw-0000012.fwpkg',
                'version_name': '0ACTV_00.00.018d',
                'date': '08/21/2024'})

        # SDN Configuration edit commands for Juliet (both FM and SM configs)
        self.sdn_fm_config_edits_standalone = [
            "sudo sed -i '/^MNNVL_TOPOLOGY=/c\\MNNVL_TOPOLOGY=gb200_nvl8r1_c2g4_etf_topology' {file}",
            "sudo grep -q '^MNNVL_TOPOLOGY=' {file} || echo 'MNNVL_TOPOLOGY=gb200_nvl8r1_c2g4_etf_topology' | sudo tee -a {file}",
            "sudo sed -i '/^MNNVL_PARTIALLY_POPULATED_TOPOLOGY=/c\\MNNVL_PARTIALLY_POPULATED_TOPOLOGY=1' {file}",
            "sudo grep -q '^MNNVL_PARTIALLY_POPULATED_TOPOLOGY=' {file} || echo 'MNNVL_PARTIALLY_POPULATED_TOPOLOGY=1' | sudo tee -a {file}"
        ]
        self.sdn_fm_config_edits = self.sdn_fm_config_edits_standalone
        # SM config edits for Juliet (only when has_loopbox or is_simx)
        self.sdn_sm_config_edits = [
            "# Ensure nvlink_enable=FALSE",
            "sudo sed -i '/^nvlink_enable[ ]*TRUE/c\\nvlink_enable FALSE' {file}",
            "sudo grep -q '^nvlink_enable' {file} || echo 'nvlink_enable FALSE' | sudo tee -a {file}",
            "# Comment plugin_name grpc_mgr",
            "sudo sed -i '/^[ ]*plugin_name[ ]\\+grpc_mgr/s/^/#/' {file}",
            "# Comment plugin_options -grpc_mgr",
            "sudo sed -i '/^[ ]*plugin_options[ ]\\+-grpc_mgr[ ]\\+--config_file[ ]\\+/s/^/#/' {file}"
        ]

        self.reboot_reason_dict = {
            RebootConsts.HALT: (RebootConsts.REBOOT_REASON_POWER_CYCLE, RebootConsts.REBOOT_USER_ADMIN),
            RebootConsts.POWER_CYCLE: (RebootConsts.REBOOT_REASON_POWER_CYCLE, RebootConsts.REBOOT_USER_ADMIN),
            RebootConsts.COLD: ("reboot", RebootConsts.REBOOT_USER_ADMIN),
            RebootConsts.IMMEDIATE: ("reboot", RebootConsts.REBOOT_USER_ADMIN),
            RebootConsts.FACTORY_RESET: ("reboot", RebootConsts.REBOOT_USER_SYSTEM),
            RebootConsts.POWER_BUTTON: (SystemConsts.REBOOT_REASON_POWER_BUTTON, RebootConsts.REBOOT_USER_NA),
            RebootConsts.REMOTE_REBOOT: (RebootConsts.REBOOT_REASON_POWER_CYCLE, RebootConsts.REBOOT_USER_NA),
            RebootConsts.INSTALL_FW: ("reboot", RebootConsts.REBOOT_USER_ADMIN),
            RebootConsts.RESET_BIT: (RebootConsts.REBOOT_REASON_RESET_BIT, RebootConsts.REBOOT_USER_NA,
                                     RebootConsts.REBOOT_REASON_TYPE_CRITICAL_ERROR)
        }

        self.power_cycle_type = 'juliet-power-cycle'
        self.fw_versions_json_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/juliet_versions_4500.json"
        self.valid_ports_count = 72
        self.nmx_simulation_gpu_count = 72
        self.number_of_transceivers = 72
        self.cpld_amount = 4
        self.transceivers_tables_name = "TRANSCEIVER_FIRMWARE_INFO"
        self.transceiver_list = [f'sw{a + 1}' for a in range(18)]
        self.module_offset = 9
        self.leakage_sensors_count = 6
        self.list_of_leakages = [f"LEAKAGE-{i}" for i in range(1, self.leakage_sensors_count + 1)]
        self.expected_operation_durations.update({
            "Install BIOS": 600,
            'install cpld': 720,
            'install ssd': 20,
            self.generate_tech_support: 120,
            'julietscaleout generate_tech_support': 132,
            InternalNvosConsts.ACP_PORT_GOES_UP: InternalNvosConsts.NVL5_ACP_LINK_UP_TIMEOUT_LTX_DISABLED,
        })
        self.num_of_plane_ports = 1
        self.mst_dev_name = tuple(f'/dev/mst/mt54004_pciconf{i}' for i in [0, 1])
        self.memory_speed = 2400  # in MT/s
        self.show_platform_chassis_location_standalone_values = ChassisLocationConsts.EXPECTED_STANDALONE_DICT

        self.unsupported_commands_list.extend(['nv show interface {port} link low-power'])

    def _init_fan_list(self):
        super()._init_fan_list()

    def _init_led_list(self):
        self.led_list = [FansConsts.FAN_STATUS_LED, "STATUS", "UID"]

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_inventory_items.extend([PlatformConsts.FW_BMC])
        self.platform_inventory_items_dict.update({'bmc': [PlatformConsts.FW_BMC]})
        platform_inventory_bmc_values = {
            "hardware-version": NvosConst.NOT_AVAILABLE, "model": None,
            "serial": None, PlatformConsts.INV_STATE: FansConsts.STATE_OK,
            "type": PlatformConsts.FW_BMC.lower()}
        self.platform_inventory_values.update({'bmc': platform_inventory_bmc_values})
        self._extend_firmware_by_cpld_amount()

    def _init_fae_lists(self):
        super()._init_fae_lists()
        self.fae_eeprom_values = {
            "BMC": {"Manufacturer": "NVIDIA", "Model": None, "PartNumber": ExpectedString(r"[-\dA-Z]+"),
                    "SerialNumber": ExpectedString.number_and_string(""), "State": "Enabled"}
        }

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = [
            'ASIC1', 'ASIC2', 'Ambient-MNG-Temp', 'CPU-Pack-Temp', 'Drive-Temp', 'HSC-VinDC-Temp', 'PDB-Conv-1-Temp',
            'PDB-Conv-2-Temp', 'PDB-Conv-3-Temp', 'PDB-Conv-4-Temp', 'PMIC-1-Temp', 'PMIC-2-Temp', 'PMIC-3-Temp',
            'PMIC-4-Temp', 'PMIC-5-Temp', 'PMIC-6-Temp', 'PMIC-7-Temp', 'PMIC-8-Temp',
            'SWB-ASIC1-PCB-Temp', 'SWB-ASIC2-PCB-Temp', 'SODIMM-1-Temp']

    def _init_psu_list(self):
        self.psu_list = []
        self.psu_fan_list = []

    def _init_gnmi_consts(self):
        super()._init_gnmi_consts()
        self.bmc_xpath = "platform-general/versions/state/fw-version-bmc"
        self.erot_xpath = "platform-general/versions/state/fw-version-erot"
        self.fpga_xpath = "platform-general/versions/state/fw-version-fpga"
        self.components_gnmi_xpath = [self.bmc_xpath, self.bios_xpath, self.erot_xpath, self.fpga_xpath,
                                      self.cpld1_xpath, self.cpld2_xpath, self.cpld3_xpath, self.cpld4_xpath]

    def _init_boot_time_timeouts(self):
        super()._init_boot_time_timeouts()
        self.timeout_system_is_ready = 20 * MINUTE
        self.timeout_reboot_to_grub_menu = 5 * MINUTE

    def get_spdm_components(self, setup_name: str) -> list[str]:
        """
        Get available SPDM components for this device type (setup-specific for Juliet).
        SPDM components include: ERoTs (BMC, CPU, FPGA, NVSwitch), MCU, etc.
        """
        available_erots_per_juliet_number: dict[str, list[str]] = {
            '68': [SPDMComponents.BMC],
            '121': SPDMComponents.ALL_SUPPORTED_COMPONENTS,
            '126': [SPDMComponents.BMC],
            '128': [SPDMComponents.BMC, SPDMComponents.FPGA, SPDMComponents.NVSWITCH_0],
        }
        for juliet_num, available_erots in available_erots_per_juliet_number.items():
            if setup_name.endswith(juliet_num):
                logger.info(f'available ERoTs for {setup_name} - {available_erots}')
                return available_erots
        # Default: Standard Juliet systems have all SPDM components
        logger.info(f'Using default (all) SPDM components for {setup_name}')
        return SPDMComponents.juliet_components()


# -------------------------- JulietScaleout Switch ----------------------------
class JulietScaleoutSwitch(JulietSwitch):

    def __init__(self, asic_amount=2):
        super().__init__(asic_amount=asic_amount)

    def _init_constants(self):
        super()._init_constants()
        self.asic_type = NvosConst.NVL5
        self.asic_numbers = [f"ASIC{i}" for i in range(1, self.asic_amount + 1)]
        self.cluster_app_nmx_controller = {'addition-info': ExpectedString(regex=".*"), 'app-id': 'nmx-c-nvos', 'app-ver': None, 'capabilities': 'sm, gfm, fib, gw-api', 'components-ver': None, 'reason': '', 'status': 'ok'}
        self.cluster_app_nmx_telemetry = {'addition-info': ExpectedString(regex=".*"), 'app-id': 'nmx-telemetry', 'app-ver': None, 'capabilities': 'nvl, gnmi, syslog, bmc', 'components-ver': None, 'reason': '', 'status': 'ok'}
        self.cluster_app = {
            ClusterConsts.NMX_CONTROLLER: {
                **{  # Unpack
                    key: value
                    for key, value in self.cluster_app_nmx_controller.items()
                    if key not in []
                },
                'manager': {
                    "ca-certificate": "",
                    "certificate": "",
                    "crl": "",
                    "encryption": "disabled",
                    "state": "disabled"
                },
                "rbac": {
                    "rbac-file": "",
                    "rbac-mode": "disabled"
                }
            },
            ClusterConsts.NMX_TELEMETRY: {
                **{
                    key: value
                    for key, value in self.cluster_app_nmx_telemetry.items()
                    if key not in []
                },
                'manager': {
                    "ca-certificate": "",
                    "certificate": "",
                    "crl": "",
                    "encryption": "disabled",
                    "state": "enabled"
                },
                "rbac": {
                    "rbac-file": "",
                    "rbac-mode": "disabled"
                }
            }
        }
        # self.cluster_app = {
        #     'nmx-controller': {key: value for key, value in self.cluster_app_nmx_controller.items() if key not in []},
        #     'nmx-telemetry': {key: value for key, value in self.cluster_app_nmx_telemetry.items() if key not in []}
        # }
        self.cluster_app_installed = {
            ClusterConsts.NMX_CONTROLLER: {key: value for key, value in self.cluster_app_nmx_controller.items() if key not in ['reason', 'status', 'addition-info']},
            ClusterConsts.NMX_TELEMETRY: {key: value for key, value in self.cluster_app_nmx_telemetry.items() if key not in ['reason', 'status', 'addition-info']}
        }
        # self.cluster_app = {'nmx-controller': self.cluster_app_nmx_controller, 'nmx-telemetry': self.cluster_app_nmx_telemetry}

        # Cluster apps configuration - Juliet has both controller and telemetry
        self.expected_cluster_apps = ClusterConsts.INITIAL_EXPECTED_APPS
        self.cluster_config_files_by_app = {
            ClusterConsts.NMX_CONTROLLER: ClusterConsts.NMX_CONTROLLER_CONFIG_FILE_TYPES,
            ClusterConsts.NMX_TELEMETRY: ClusterConsts.NMX_TELEMETRY_CONFIG_FILE_TYPES
        }
        self.cluster_state_files_by_app = {
            ClusterConsts.NMX_CONTROLLER: ClusterConsts.NMX_CONTROLLER_STATE_FILE_TYPES,
            ClusterConsts.NMX_TELEMETRY: ClusterConsts.NMX_TELEMETRY_STATE_FILE_TYPES
        }
        # Tech-support: directory path and expected log files per app
        self.cluster_techsupport_dirs_by_app = {
            ClusterConsts.NMX_CONTROLLER: f'log/nmx/{ClusterConsts.NMX_CONTROLLER_PREFIX}',
            ClusterConsts.NMX_TELEMETRY: f'log/nmx/{ClusterConsts.NMX_TELEMETRY_PREFIX}'
        }
        # Note: cluster_log_files_by_app inherited from JulietSwitch parent

        self.core_count = 8
        self.reboot_type = 'julietscaleout_reboot'
        self.reset_factory = 'julietscaleout reset factory'
        self.generate_tech_support = 'julietscaleout generate_tech_support'
        self.constants.firmware.extend([PlatformConsts.FW_FPGA, PlatformConsts.FW_BMC])
        self.ssd_image = None
        self.voltage_sensors = [
            'HSC-VinDC-In', 'HSC-VinDC-Out', 'PDB-1-Conv-In-1', 'PDB-1-Conv-Out-1', 'PDB-2-Conv-In-1',
            'PDB-2-Conv-Out-1', 'PDB-3-Conv-In-1', 'PDB-3-Conv-Out-1', 'PDB-4-Conv-In-1', 'PDB-4-Conv-Out-1',
            'PMIC-1-12V-VDD-ASIC1-In-1', 'PMIC-1-ASIC1-VDD-Out-1', 'PMIC-2-12V-HVDD-DVDD-ASIC1-In-1',
            'PMIC-2-ASIC1-DVDD-PL0-Out-2', 'PMIC-2-ASIC1-HVDD-PL0-Out-1', 'PMIC-3-12V-HVDD-DVDD-ASIC1-In-1',
            'PMIC-3-ASIC1-DVDD-PL1-Out-2', 'PMIC-3-ASIC1-HVDD-PL1-Out-1', 'PMIC-4-12V-VDD-ASIC2-In-1',
            'PMIC-4-ASIC2-VDD-Out-1', 'PMIC-5-12V-HVDD-DVDD-ASIC2-In-1', 'PMIC-5-ASIC2-DVDD-PL0-Out-2',
            'PMIC-5-ASIC2-HVDD-PL0-Out-1', 'PMIC-6-12V-HVDD-DVDD-ASIC2-In-1', 'PMIC-6-ASIC2-DVDD-PL1-Out-2',
            'PMIC-6-ASIC2-HVDD-PL1-Out-1', 'PMIC-7-12V-MAIN-In-1', 'PMIC-7-CEX-VDD-Out-1',
            'PMIC-8-COMEX-VDD-MEM-In-1', 'PMIC-8-COMEX-VDD-MEM-Out-1']
        # TBD
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n5110_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N5110_LD",
            "asic-model": self.asic_type,
        })

        self.stats_disk_header_num_of_lines = 16
        self.stats_cpu_header_num_of_lines = 39
        self.stats_temperature_header_num_of_lines = 25
        self.allow_cpld_update = True

        # Port 1-36 is from asic1/ Port 37-72 is from asic2
        self.nvl_access_ports_list = ['acp1', 'acp2', 'acp3', 'acp4', 'acp5', 'acp6',
                                      'acp7', 'acp8', 'acp9', 'acp10', 'acp11', 'acp12', 'acp13', 'acp14',
                                      'acp15', 'acp16', 'acp17', 'acp18', 'acp19', 'acp20',
                                      'acp21', 'acp22', 'acp23', 'acp24', 'acp25', 'acp26',
                                      'acp27', 'acp28', 'acp29', 'acp30', 'acp31', 'acp32',
                                      'acp33', 'acp34', 'acp35', 'acp36', 'acp37', 'acp38', 'acp39', 'acp40',
                                      'acp41', 'acp42', 'acp43', 'acp44', 'acp45', 'acp46',
                                      'acp47', 'acp48', 'acp49', 'acp50', 'acp51', 'acp52',
                                      'acp53', 'acp54', 'acp55', 'acp56', 'acp57', 'acp58',
                                      'acp59', 'acp60', 'acp61', 'acp62', 'acp63', 'acp64',
                                      'acp65', 'acp66', 'acp67', 'acp68', 'acp69', 'acp70',
                                      'acp71', 'acp72']

        self.nvl_trunk_ports_list = ['sw1p1s1', 'sw1p1s2', 'sw1p2s1', 'sw1p2s2',
                                     'sw2p1s1', 'sw2p1s2', 'sw2p2s1', 'sw2p2s2',
                                     'sw3p1s1', 'sw3p1s2', 'sw3p2s1', 'sw3p2s2',
                                     'sw4p1s1', 'sw4p1s2', 'sw4p2s1', 'sw4p2s2',
                                     'sw5p1s1', 'sw5p1s2', 'sw5p2s1', 'sw5p2s2',
                                     'sw6p1s1', 'sw6p1s2', 'sw6p2s1', 'sw6p2s2',
                                     'sw7p1s1', 'sw7p1s2', 'sw7p2s1', 'sw7p2s2',
                                     'sw8p1s1', 'sw8p1s2', 'sw8p2s1', 'sw8p2s2',
                                     'sw9p1s1', 'sw9p1s2', 'sw9p2s1', 'sw9p2s2',
                                     'sw10p1s1', 'sw10p1s2', 'sw10p2s1', 'sw10p2s2',
                                     'sw11p1s1', 'sw11p1s2', 'sw11p2s1', 'sw11p2s2',
                                     'sw12p1s1', 'sw12p1s2', 'sw12p2s1', 'sw12p2s2',
                                     'sw13p1s1', 'sw13p1s2', 'sw13p2s1', 'sw13p2s2',
                                     'sw14p1s1', 'sw14p1s2', 'sw14p2s1', 'sw14p2s2',
                                     'sw15p1s1', 'sw15p1s2', 'sw15p2s1', 'sw15p2s2',
                                     'sw16p1s1', 'sw16p1s2', 'sw16p2s1', 'sw16p2s2',
                                     'sw17p1s1', 'sw17p1s2', 'sw17p2s1', 'sw17p2s2',
                                     'sw18p1s1', 'sw18p1s2', 'sw18p2s1', 'sw18p2s2'
                                     ]
        self.network_ports = ['eth0', 'eth1', 'lo']
        self.all_nvl_ports_list = self.nvl_access_ports_list + self.nvl_trunk_ports_list + self.network_ports
        self.nvl_fnm_ports = ['fnm1', 'fnm2']
        self.nvl_internal_fnm_ports = ['fnma0p1', 'fnma1p1']
        self.all_fae_nvl_ports_list = self.all_nvl_ports_list + self.nvl_fnm_ports + self.nvl_internal_fnm_ports
        self.nvl_port = ['sw1p1s1']
        self.nvl_trunk_port_speed = '400G'
        self.access_port_speed = '400G'
        self.fnm_link_speed = '100G'
        self.fnm_fae_link_speed = '100G'
        self.nvl_port_type = 'nvl'
        self.num_of_cartridges = 4
        # will be updated

    def _init_fan_list(self):
        super()._init_fan_list()
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2", "FAN5/1", "FAN5/2", "FAN6/1", "FAN6/2"]
        self.fan_led_list = []

    def _init_psu_list(self):
        self.psu_list = []
        self.psu_fan_list = []

    def _init_led_list(self):
        super()._init_led_list()

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=2000, range_max=10000),
            "max-speed": ExpectedString(range_min=20000, range_max=40000)}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": "692-9K36F-00MV-JS0"})

    def sleep_after_system_reboot(self):
        logger.info("Sleeping for 60 seconds - Reboot takes longer on juliet for now")
        time.sleep(60)

    def _relevant_config_filename_by_version(self, version: str) -> str:
        return 'nvos_config_nvl5.yml'

    @classmethod
    def _get_lane_bmap(cls, port):
        return (0x3 if port.split_number == 1 else 0xc) * (0x10 if port.local_port == 2 else 1)


# -------------------------- JulietScaleoutSunbirdSwitch Switch ----------------------------


class JulietScaleoutSunbirdSwitch(JulietScaleoutSwitch):

    def __init__(self):
        super().__init__()

    def _init_fan_list(self):
        super()._init_fan_list()
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2"]
        self.fan_led_list = []

    def _init_temperature(self):
        super()._init_temperature()
        sensors_to_remove = ['PDB-Conv-3-Temp', 'PDB-Conv-4-Temp']
        for sensor in sensors_to_remove:
            self.temperature_sensors.remove(sensor)

    def _init_platform_lists(self):
        super()._init_platform_lists()
        sensors_to_remove = [
            'PDB-3-Conv-In-1', 'PDB-3-Conv-Out-1',
            'PDB-4-Conv-In-1', 'PDB-4-Conv-Out-1'
        ]
        for sensor in sensors_to_remove:
            self.voltage_sensors.remove(sensor)
        self.list_of_leakages = []
        self.platform_inventory_switch_values.update({"model": "692-9K36F-00MV-S11"})


# -------------------------- JulietTTM Switch ----------------------------


class JulietTTMSwitch(JulietScaleoutSwitch):

    def __init__(self):
        super().__init__()

    def _init_constants(self):
        super()._init_constants()
        self.allow_cpld_update = True
        self.asic_numbers = [f"ASIC{i}" for i in range(1, self.asic_amount + 1)]

    def _init_fan_list(self):
        super()._init_fan_list()
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2"]
        self.fan_led_list = []

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_inventory_switch_values.update({"model": "692-9K36F-00MV-JSL"})

    def _init_led_list(self):
        super()._init_led_list()

# -------------------------- Ariel Switch ----------------------------


class JulietAriel(JulietTTMSwitch):

    def __init__(self):
        super().__init__()

    def _init_constants(self):
        super()._init_constants()
        self.asic_numbers = [f"ASIC{i}" for i in range(1, self.asic_amount + 1)]
        # TODO - Need to be changed to correct values for Ariel. Double check with tamuz.
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n5112_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N5112_LD",
            "asic-model": self.asic_type,
        })
        self.num_of_cartridges = 2
        self.nvl_access_ports_list = ['acp1', 'acp2', 'acp3', 'acp4', 'acp5', 'acp6',
                                      'acp7', 'acp8', 'acp9', 'acp10', 'acp11', 'acp12', 'acp13', 'acp14',
                                      'acp15', 'acp16', 'acp17', 'acp18', 'acp19', 'acp20',
                                      'acp21', 'acp22', 'acp23', 'acp24', 'acp25', 'acp26',
                                      'acp27', 'acp28', 'acp29', 'acp30', 'acp31', 'acp32',
                                      'acp33', 'acp34', 'acp35', 'acp36', 'acp37', 'acp38', 'acp39', 'acp40',
                                      'acp41', 'acp42', 'acp43', 'acp44', 'acp45', 'acp46',
                                      'acp47', 'acp48', 'acp49', 'acp50', 'acp51', 'acp52',
                                      'acp53', 'acp54', 'acp55', 'acp56', 'acp57', 'acp58',
                                      'acp59', 'acp60', 'acp61', 'acp62', 'acp63', 'acp64',
                                      'acp65', 'acp66', 'acp67', 'acp68', 'acp69', 'acp70',
                                      'acp71', 'acp72']

        self.show_platform_chassis_location_standalone_values.update({ChassisLocationConsts.TOPO_ID: ChassisLocationConsts.OBERON_36})  # https://redmine.mellanox.com/issues/4275347
        self.all_nvl_ports_list = self.nvl_access_ports_list + self.nvl_trunk_ports_list + self.network_ports

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_inventory_switch_values.update({"model": "692-9K36F-A5MV-JQS"})


# -------------------------- ArielPS Switch ----------------------------


class JulietArielPS(JulietTTMSwitch):

    def __init__(self):
        super().__init__()

    def _init_constants(self):
        super()._init_constants()
        self.asic_numbers = [f"ASIC{i}" for i in range(1, self.asic_amount + 1)]
        # TODO - Need to be changed to correct values for Ariel. Double check with tamuz.
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n5112_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N5112_LD",
            "asic-model": self.asic_type,
        })

        self.voltage_sensors = ['HSC-VinDC-In', 'HSC-VinDC-Out', 'PDB-1-Conv-In-1', 'PDB-1-Conv-Out-1',
                                'PDB-2-Conv-In-1',
                                'PDB-2-Conv-Out-1', 'PMIC-1-12V-VDD-ASIC1-In-1', 'PMIC-1-ASIC1-VDD-Out-1',
                                'PMIC-2-12V-HVDD-DVDD-ASIC1-In-1',
                                'PMIC-2-ASIC1-DVDD-PL0-Out-2', 'PMIC-2-ASIC1-HVDD-PL0-Out-1',
                                'PMIC-3-12V-HVDD-DVDD-ASIC1-In-1',
                                'PMIC-3-ASIC1-DVDD-PL1-Out-2', 'PMIC-3-ASIC1-HVDD-PL1-Out-1',
                                'PMIC-4-12V-VDD-ASIC2-In-1',
                                'PMIC-4-ASIC2-VDD-Out-1', 'PMIC-5-12V-HVDD-DVDD-ASIC2-In-1',
                                'PMIC-5-ASIC2-DVDD-PL0-Out-2',
                                'PMIC-5-ASIC2-HVDD-PL0-Out-1', 'PMIC-6-12V-HVDD-DVDD-ASIC2-In-1',
                                'PMIC-6-ASIC2-DVDD-PL1-Out-2',
                                'PMIC-6-ASIC2-HVDD-PL1-Out-1', 'PMIC-7-12V-MAIN-In-1', 'PMIC-7-CEX-VDD-Out-1',
                                'PMIC-8-COMEX-VDD-MEM-In-1', 'PMIC-8-COMEX-VDD-MEM-Out-1']

        self.nvl_access_ports_list = ['acp1', 'acp2', 'acp3', 'acp4', 'acp5', 'acp6',
                                      'acp7', 'acp8', 'acp9', 'acp10', 'acp11', 'acp12', 'acp13', 'acp14',
                                      'acp15', 'acp16', 'acp17', 'acp18', 'acp19', 'acp20',
                                      'acp21', 'acp22', 'acp23', 'acp24', 'acp25', 'acp26',
                                      'acp27', 'acp28', 'acp29', 'acp30', 'acp31', 'acp32',
                                      'acp33', 'acp34', 'acp35', 'acp36', 'acp37', 'acp38', 'acp39', 'acp40',
                                      'acp41', 'acp42', 'acp43', 'acp44', 'acp45', 'acp46',
                                      'acp47', 'acp48', 'acp49', 'acp50', 'acp51', 'acp52',
                                      'acp53', 'acp54', 'acp55', 'acp56', 'acp57', 'acp58',
                                      'acp59', 'acp60', 'acp61', 'acp62', 'acp63', 'acp64',
                                      'acp65', 'acp66', 'acp67', 'acp68', 'acp69', 'acp70',
                                      'acp71', 'acp72']

        self.all_nvl_ports_list = self.nvl_access_ports_list + self.nvl_trunk_ports_list + self.network_ports
        self.show_platform_chassis_location_standalone_values.update({ChassisLocationConsts.TOPO_ID: ChassisLocationConsts.OBERON_36})  # https://redmine.mellanox.com/issues/4275347

    def _init_temperature(self):
        super()._init_temperature()
        sensors_to_remove = ['PDB-Conv-3-Temp', 'PDB-Conv-4-Temp']
        for sensor in sensors_to_remove:
            self.temperature_sensors.remove(sensor)

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_inventory_switch_values.update({"model": "692-9K36F-A5MV-JCR"})


# -------------------------- JulietNonScaleoutSwitch Switch ----------------------------


class JulietNonScaleoutSwitch(JulietScaleoutSwitch):

    def __init__(self, asic_amount=2):
        super().__init__(asic_amount=asic_amount)

    def _init_constants(self):
        super()._init_constants()
        self.asic_numbers = [f"ASIC{i}" for i in range(1, self.asic_amount + 1)]
        self.nvl_access_ports_list = [
            'acp1', 'acp2', 'acp3', 'acp4', 'acp5', 'acp6',
            'acp7', 'acp8', 'acp9', 'acp10', 'acp11', 'acp12',
            'acp13', 'acp14', 'acp15', 'acp16', 'acp17', 'acp18',
            'acp19', 'acp20', 'acp21', 'acp22', 'acp23', 'acp24',
            'acp25', 'acp26', 'acp27', 'acp28', 'acp29', 'acp30',
            'acp31', 'acp32', 'acp33', 'acp34', 'acp35', 'acp36',
            'acp37', 'acp38', 'acp39', 'acp40', 'acp41', 'acp42',
            'acp43', 'acp44', 'acp45', 'acp46', 'acp47', 'acp48',
            'acp49', 'acp50', 'acp51', 'acp52', 'acp53', 'acp54',
            'acp55', 'acp56', 'acp57', 'acp58', 'acp59', 'acp60',
            'acp61', 'acp62', 'acp63', 'acp64', 'acp65', 'acp66',
            'acp67', 'acp68', 'acp69', 'acp70', 'acp71', 'acp72',
            'acp73', 'acp74', 'acp75', 'acp76', 'acp77', 'acp78',
            'acp79', 'acp80', 'acp81', 'acp82', 'acp83', 'acp84',
            'acp85', 'acp86', 'acp87', 'acp88', 'acp89', 'acp90',
            'acp91', 'acp92', 'acp93', 'acp94', 'acp95', 'acp96',
            'acp97', 'acp98', 'acp99', 'acp100', 'acp101', 'acp102',
            'acp103', 'acp104', 'acp105', 'acp106', 'acp107', 'acp108',
            'acp109', 'acp110', 'acp111', 'acp112', 'acp113', 'acp114',
            'acp115', 'acp116', 'acp117', 'acp118', 'acp119', 'acp120',
            'acp121', 'acp122', 'acp123', 'acp124', 'acp125', 'acp126',
            'acp127', 'acp128', 'acp129', 'acp130', 'acp131', 'acp132',
            'acp133', 'acp134', 'acp135', 'acp136', 'acp137', 'acp138',
            'acp139', 'acp140', 'acp141', 'acp142', 'acp143', 'acp144'
        ]
        self.nvl_trunk_ports_list = []
        self.network_ports = ['eth0', 'eth1', 'lo']
        self.all_nvl_ports_list = self.nvl_access_ports_list + self.nvl_trunk_ports_list + self.network_ports
        self.nvl_fnm_ports = ['fnm1', 'fnm2']
        self.nvl_internal_fnm_ports = ['fnma0p1', 'fnma1p1']
        self.all_fae_nvl_ports_list = self.all_nvl_ports_list + self.nvl_fnm_ports + self.nvl_internal_fnm_ports
        self.nvl_port = ['sw1p1s1']
        self.nvl_trunk_port_speed = '400G'
        self.access_port_speed = '400G'
        self.fnm_link_speed = '100G'
        self.fnm_fae_link_speed = '100G'
        self.nvl_port_type = 'nvl'
        # will be updated
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n5100_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N5100_LD",
            "asic-model": self.asic_type,
        })
        self.memory_size: list[float] = [15.48]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2", "FAN5/1", "FAN5/2", "FAN6/1", "FAN6/2"]
        self.fan_led_list = []

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=2000, range_max=10000),
            "max-speed": ExpectedString(range_min=20000, range_max=40000)}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": ExpectedString(regex="692-9K36F-00MV-JS0")})

    @classmethod
    def _get_lane_bmap(cls, port):
        raise NotImplementedError("Implemented only for sw ports. Juliet NSO doesn't have sw ports.")

# -------------------------- JulietNonScaleoutSwitchGB300 Switch ----------------------------


class JulietNonScaleoutSwitchGB300(JulietNonScaleoutSwitch):

    def __init__(self):
        super().__init__()

    def _init_constants(self):
        super()._init_constants()
        self.category_list = ['asic-power', 'cpu', 'disk', 'mgmt-interface', 'temperature', 'voltage']
        self.category_disabled_dict = {
            self.category_list[0]: self.category_default_disabled_dict,
            self.category_list[1]: self.category_default_disabled_dict,
            self.category_list[2]: self.category_disk_default_disable_dict,
            self.category_list[3]: self.category_default_disabled_dict,
            self.category_list[4]: self.category_temperature_default_disable_dict,
            self.category_list[5]: self.category_default_disabled_dict,
        }
        self.category_list_default_dict = {
            self.category_list[0]: self.category_default_dict,
            self.category_list[1]: self.category_default_dict,
            self.category_list[2]: self.category_disk_default_dict,
            self.category_list[3]: self.category_default_dict,
            self.category_list[4]: self.category_temperature_default_dict,
            self.category_list[5]: self.category_default_dict,
        }
        self.fw_versions_json_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/juliet_gb300_versions.json"
        # will be updated
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n5500_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N5500_LD",
            "asic-model": self.asic_type,
        })
        self.stats_disk_header_num_of_lines = 16
        self.stats_cpu_header_num_of_lines = 12
        self.stats_temperature_header_num_of_lines = 17
        self.cpld_amount = 3
        self._extend_firmware_by_cpld_amount()
        self.memory_speed = 2667  # in MT/s (GB300 uses 2667, only GB200 uses 2400)
        stats_dump_files = ["cpu.csv.gz", "disk.csv.gz", "mgmt-interface.csv.gz",
                            "temperature.csv.gz", "voltage.csv.gz"]
        self.constants = self.constants._replace(stats_dump_files=stats_dump_files)
        log_dump_files = ["audit", "auth.log.gz", "btmp.gz", "cron.log.gz",
                          "firewall_packet_capture.log.gz", "health_history.gz",
                          "nv-cli.log.gz", "nvued.log.gz", "syslog.gz", "wtmp.gz", "ztp.log.gz"]
        self.constants = self.constants._replace(log_dump_files=log_dump_files)
        self.voltage_sensors = [
            "HSC-VinDC-In",
            "HSC-VinDC-Out",
            "PDB-1-Conv-In-1",
            "PDB-1-Conv-Out-1",
            "PMIC-1-12V-VDD-ASIC1-In-1",
            "PMIC-1-ASIC1-VDD-Out-1",
            "PMIC-2-12V-HVDD-DVDD-ASIC1-In-1",
            "PMIC-2-ASIC1-DVDD-PL0-Out-2",
            "PMIC-2-ASIC1-HVDD-PL0-Out-1",
            "PMIC-3-12V-HVDD-DVDD-ASIC1-In-1",
            "PMIC-3-ASIC1-DVDD-PL1-Out-2",
            "PMIC-3-ASIC1-HVDD-PL1-Out-1",
            "PMIC-4-12V-VDD-ASIC2-In-1",
            "PMIC-4-ASIC2-VDD-Out-1",
            "PMIC-5-12V-HVDD-DVDD-ASIC2-In-1",
            "PMIC-5-ASIC2-DVDD-PL0-Out-2",
            "PMIC-5-ASIC2-HVDD-PL0-Out-1",
            "PMIC-6-12V-HVDD-DVDD-ASIC2-In-1",
            "PMIC-6-ASIC2-DVDD-PL1-Out-2",
            "PMIC-6-ASIC2-HVDD-PL1-Out-1",
            "PMIC-7-12V-MAIN-In-1",
            "PMIC-7-CPU-Out-1",
            "PMIC-7-SOC-Out-2"
        ]
        self.leakage_sensors_count = 2
        self.list_of_leakages = [f"LEAKAGE-{i}" for i in range(1, self.leakage_sensors_count + 1)]
        self.ztp_prod_json = 'uninstall_juliet_prod_gb300.json'
        self.ztp_dev_json = 'uninstall_juliet_gb300.json'
        self.ztp_complex_prod_json = 'complex_prod_juliet_gb300.json'
        self.ztp_complex_dev_json = 'complex_juliet_gb300.json'
        self.power_capping_profiles_default_dict = {
            'compute': {
                'long-term-power-allocation': '475',
                'short-term-power-allocation': '575'
            },
            'networking': {
                'long-term-power-allocation': '575',
                'short-term-power-allocation': '575'
            },
            'reduced-bandwidth': {
                'long-term-power-allocation': '380',
                'short-term-power-allocation': '475'
            }
        }

    def _init_fan_list(self):
        # GB300 is 100% liquid cooled
        self.fan_list = []
        self.fan_led_list = []

    def _init_services(self):
        super()._init_services()
        self.available_services.remove('hw-management.service')

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": ExpectedString(regex="692-9K33R-00MV-JES")})

    def _init_led_list(self):
        super()._init_led_list()
        self.led_list = ["STATUS", "UID"]

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = [
            "ASIC1",
            "ASIC2",
            "CPU-Pack-Temp",
            "Drive-Temp",
            "HSC-VinDC-Temp",
            "PDB-Conv-1-Temp",
            "PMIC-7-Temp",
            "SODIMM-1-Temp"
        ]

    def _init_gnmi_consts(self):
        super()._init_gnmi_consts()
        self.components_gnmi_xpath = [self.bmc_xpath, self.bios_xpath, self.erot_xpath, self.fpga_xpath,
                                      self.cpld1_xpath, self.cpld2_xpath, self.cpld3_xpath]

    def _relevant_config_filename_by_version(self, version: str) -> str:
        return 'nvos_config_nvl5_gb300.yml'


# -------------------------- JulietNonScaleoutSwitchGB300QS Switch ----------------------------


class JulietNonScaleoutSwitchGB300QS(JulietNonScaleoutSwitchGB300):

    def __init__(self):
        super().__init__()

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": ExpectedString(regex="692-9K33R-00MV-JQS")})


# -------------------------- JulietNonScaleoutSwitchGB300PS Switch ----------------------------


class JulietNonScaleoutSwitchGB300PS(JulietNonScaleoutSwitchGB300):

    def __init__(self):
        super().__init__()

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": ExpectedString(regex="692-9K33R-00MV-JCR")})


# -------------------------- JulietNonScaleoutNoNCISwitch Switch ----------------------------


class JulietNonScaleoutSwitchNoNCI(JulietNonScaleoutSwitch):

    def __init__(self):
        super().__init__()

    def _init_constants(self):
        super()._init_constants()
        self.asic_numbers = [f"ASIC{i}" for i in range(1, self.asic_amount + 1)]
        self.nvl_access_ports_list = [
            'acp1', 'acp2', 'acp3', 'acp4', 'acp5', 'acp6',
            'acp7', 'acp8', 'acp9', 'acp10', 'acp11', 'acp12',
            'acp13', 'acp14', 'acp15', 'acp16', 'acp17', 'acp18',
            'acp19', 'acp20', 'acp21', 'acp22', 'acp23', 'acp24',
            'acp25', 'acp26', 'acp27', 'acp28', 'acp29', 'acp30',
            'acp31', 'acp32', 'acp33', 'acp34', 'acp35', 'acp36',
            'acp37', 'acp38', 'acp39', 'acp40', 'acp41', 'acp42',
            'acp43', 'acp44', 'acp45', 'acp46', 'acp47', 'acp48',
            'acp49', 'acp50', 'acp51', 'acp52', 'acp53', 'acp54',
            'acp55', 'acp56', 'acp57', 'acp58', 'acp59', 'acp60',
            'acp61', 'acp62', 'acp63', 'acp64', 'acp65', 'acp66',
            'acp67', 'acp68', 'acp69', 'acp70', 'acp71', 'acp72',
            'acp73', 'acp74', 'acp75', 'acp76', 'acp77', 'acp78',
            'acp79', 'acp80', 'acp81', 'acp82', 'acp83', 'acp84',
            'acp85', 'acp86', 'acp87', 'acp88', 'acp89', 'acp90',
            'acp91', 'acp92', 'acp93', 'acp94', 'acp95', 'acp96',
            'acp97', 'acp98', 'acp99', 'acp100', 'acp101', 'acp102',
            'acp103', 'acp104', 'acp105', 'acp106', 'acp107', 'acp108',
            'acp109', 'acp110', 'acp111', 'acp112', 'acp113', 'acp114',
            'acp115', 'acp116', 'acp117', 'acp118', 'acp119', 'acp120',
            'acp121', 'acp122', 'acp123', 'acp124', 'acp125', 'acp126',
            'acp127', 'acp128', 'acp129', 'acp130', 'acp131', 'acp132',
            'acp133', 'acp134', 'acp135', 'acp136', 'acp137', 'acp138',
            'acp139', 'acp140', 'acp141', 'acp142', 'acp143', 'acp144'
        ]
        self.nvl_trunk_ports_list = []
        self.network_ports = ['eth0', 'eth1', 'lo']
        self.all_nvl_ports_list = self.nvl_access_ports_list + self.nvl_trunk_ports_list + self.network_ports
        self.nvl_fnm_ports = ['fnm1', 'fnm2']
        self.nvl_internal_fnm_ports = ['fnma0p1', 'fnma1p1']
        self.all_fae_nvl_ports_list = self.all_nvl_ports_list + self.nvl_fnm_ports + self.nvl_internal_fnm_ports
        self.nvl_port = ['sw1p1s1']
        self.nvl_trunk_port_speed = '400G'
        self.access_port_speed = '400G'
        self.fnm_link_speed = '100G'
        self.fnm_fae_link_speed = '100G'
        self.nvl_port_type = 'nvl'
        # will be updated
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n5200_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N5200_LD",
            "asic-model": self.asic_type,
        })
        self.voltage_sensors = ['HSC-VinDC-In', 'HSC-VinDC-Out', 'PDB-1-Conv-In-1', 'PDB-1-Conv-Out-1', 'PDB-2-Conv-In-1',
                                'PDB-2-Conv-Out-1', 'PMIC-1-12V-VDD-ASIC1-In-1', 'PMIC-1-ASIC1-VDD-Out-1', 'PMIC-2-12V-HVDD-DVDD-ASIC1-In-1',
                                'PMIC-2-ASIC1-DVDD-PL0-Out-2', 'PMIC-2-ASIC1-HVDD-PL0-Out-1', 'PMIC-3-12V-HVDD-DVDD-ASIC1-In-1',
                                'PMIC-3-ASIC1-DVDD-PL1-Out-2', 'PMIC-3-ASIC1-HVDD-PL1-Out-1', 'PMIC-4-12V-VDD-ASIC2-In-1',
                                'PMIC-4-ASIC2-VDD-Out-1', 'PMIC-5-12V-HVDD-DVDD-ASIC2-In-1', 'PMIC-5-ASIC2-DVDD-PL0-Out-2',
                                'PMIC-5-ASIC2-HVDD-PL0-Out-1', 'PMIC-6-12V-HVDD-DVDD-ASIC2-In-1', 'PMIC-6-ASIC2-DVDD-PL1-Out-2',
                                'PMIC-6-ASIC2-HVDD-PL1-Out-1', 'PMIC-7-12V-MAIN-In-1', 'PMIC-7-CEX-VDD-Out-1',
                                'PMIC-8-COMEX-VDD-MEM-In-1', 'PMIC-8-COMEX-VDD-MEM-Out-1']

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2"]
        self.fan_led_list = []

    def _init_temperature(self):
        super()._init_temperature()
        sensors_to_remove = ['PDB-Conv-3-Temp', 'PDB-Conv-4-Temp']
        for sensor in sensors_to_remove:
            self.temperature_sensors.remove(sensor)

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=2000, range_max=10000),
            "max-speed": ExpectedString(range_min=20000, range_max=40000)}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": ExpectedString(regex="692-96099-00MV-JS0")})

# -------------------------- JulietNonScaleoutSwitchNoNCI5600 Switch ----------------------------


class JulietNonScaleoutSwitchNoNCI5600(JulietNonScaleoutSwitchNoNCI):

    def __init__(self):
        super().__init__()

    def _init_constants(self):
        super()._init_constants()
        self.asic_numbers = [f"ASIC{i}" for i in range(1, self.asic_amount + 1)]
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n5600_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N5600_LD",
            "asic-model": self.asic_type,
        })

    def _init_fan_list(self):
        super()._init_fan_list()

    def _init_temperature(self):
        super()._init_temperature()

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": ExpectedString(regex="692-9K33R-00MV-JS0")})


# -------------------------- RosalindSurrogate Switch ----------------------------


class RosalindSurrogateSwitch(JulietNonScaleoutSwitch):

    def __init__(self, asic_amount=4):
        super().__init__(asic_amount=asic_amount)
        self.single_fw_pkg_supported = True

    def get_spdm_components(self, setup_name: str) -> list[str]:
        """
        Get available SPDM components for Rosalind devices.
        Rosalind: BMC, CPU, MCUs (SMA), and 4 NVSwitches.
        """
        logger.info(f'Rosalind available SPDM components for {setup_name}')
        return SPDMComponents.rosalind_components()

    def _init_dockers(self):
        """Override docker list for Rosalind/Surrogate platforms - uses nv-gnmi/nv-umf instead of gnmi-server."""
        super()._init_dockers()
        # Remove gnmi-server if present (Rosalind/Surrogate uses nv-gnmi and nv-umf)
        if 'gnmi-server' in self.available_dockers:
            self.available_dockers.remove('gnmi-server')

    def _init_constants(self):
        super()._init_constants()
        self.asic_type = NvosConst.NVL5
        self.supported_nvl_speeds = ['200G', '400G']
        self.category_list = ['temperature', 'cpu', 'disk', 'mgmt-interface', 'voltage', 'asic-power']
        self.nvl_port_type = 'acp'
        # Override mst device names for Rosalind Surrogate (4 ASICs) to mt54008 devices
        self.mst_dev_name = tuple(f'/dev/mst/mt54008_pciconf{i}' for i in range(self.asic_amount))
        self.sma_amount = 2
        self.sma_components = list(f"{PlatformConsts.FW_SMA}{i if i else ''}" for i in range(0, self.sma_amount + 1))
        self._extend_firmware_by_sma_amount()
        # Rosalind/Surrogate platforms require nv-bridge configuration
        self.requires_nv_bridge_config = True
        # Rosalind/Surrogate platforms have nvbridge capability - update only the controller definition
        self.cluster_app_nmx_controller = {'addition-info': ExpectedString(regex=".*"), 'app-id': 'nmx-c-nvos', 'app-ver': None, 'capabilities': 'sm, nvbridge, gfm, fib, gw-api', 'components-ver': None, 'reason': '', 'status': 'ok'}
        # Rebuild cluster_app dictionaries with updated nmx_controller (inherits telemetry from parent)
        self.cluster_app = {
            ClusterConsts.NMX_CONTROLLER: {
                **{key: value for key, value in self.cluster_app_nmx_controller.items() if key not in []},
                'manager': {"ca-certificate": "", "certificate": "", "crl": "", "encryption": "disabled", "state": "disabled"},
                "rbac": {"rbac-file": "", "rbac-mode": "disabled"}
            },
            ClusterConsts.NMX_TELEMETRY: {
                **{key: value for key, value in self.cluster_app_nmx_telemetry.items() if key not in []},
                'manager': {"ca-certificate": "", "certificate": "", "crl": "", "encryption": "disabled", "state": "enabled"},
                "rbac": {"rbac-file": "", "rbac-mode": "disabled"}
            }
        }

        self.cluster_app_installed = {
            ClusterConsts.NMX_CONTROLLER: {key: value for key, value in self.cluster_app_nmx_controller.items() if key not in ['reason', 'status', 'addition-info']},
            ClusterConsts.NMX_TELEMETRY: {key: value for key, value in self.cluster_app_nmx_telemetry.items() if key not in ['reason', 'status', 'addition-info']}
        }

        # Note: expected_cluster_apps, cluster_config_files_by_app, cluster_state_files_by_app
        # are inherited from JulietScaleoutSwitch parent (both apps) - no need to redefine
        self.components_list = [FW_COMPONENT_CPLD,
                                FW_COMPONENT_BMC,
                                FW_COMPONENT_SMA,
                                FW_COMPONENT_BIOS,
                                FW_COMPONENT_EROT]
        self.category_disabled_dict = {
            self.category_list[0]: self.category_temperature_default_disable_dict,
            self.category_list[1]: self.category_default_disabled_dict,
            self.category_list[2]: self.category_disk_default_disable_dict,
            self.category_list[3]: self.category_default_disabled_dict,
            self.category_list[4]: self.category_default_disabled_dict,
            self.category_list[5]: self.category_default_disabled_dict,
        }
        self.category_list_default_dict = {
            self.category_list[0]: self.category_temperature_default_dict,
            self.category_list[1]: self.category_default_dict,
            self.category_list[2]: self.category_disk_default_dict,
            self.category_list[3]: self.category_default_dict,
            self.category_list[4]: self.category_default_dict,
            self.category_list[5]: self.category_default_dict,
        }
        # TODO -- Define the following new file. It has only 2 cplds instead of 3/4
        self.fw_versions_json_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/surrogate_versions.json"
        # will be updated
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n6150_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N6150_LD",
            "asic-model": self.asic_type,
        })
        self.cpld_amount = 2
        self._extend_firmware_by_cpld_amount()
        self.memory_speed = 2667  # in MT/s (Rosalind/Surrogate use 2667, not 2400)
        # Rosalind Surrogate has an extra whitelist rule enabling nvbridge communication
        self.expected_acl_rule_counts['acl-default-whitelist'] = 26
        stats_dump_files = [f'{category}.csv.gz' for category in self.category_list]
        self.constants = self.constants._replace(stats_dump_files=stats_dump_files)
        self.constants.erots.clear()
        self.constants.erots.extend([PlatformConsts.EROT_BMC_PATH_NAME, PlatformConsts.EROT_CPU_PATH_NAME])
        self.constants.firmware.remove(PlatformConsts.FW_FPGA)
        log_dump_files = ["audit", "auth.log.gz", "btmp.gz", "cron.log.gz",
                          "firewall_packet_capture.log.gz", "health_history.gz",
                          "nv-cli.log.gz", "nvued.log.gz", "syslog.gz", "wtmp.gz", "ztp.log.gz"]
        self.constants = self.constants._replace(log_dump_files=log_dump_files, techsupport_size_limit_mb=140)
        self.voltage_sensors = [
            "PDB-1-Conv-In-1",
            "PDB-1-Conv-Out-1",
            "PDB-2-Conv-In-1",
            "PDB-2-Conv-Out-1",
            "HSC-VinDC-In",
            "HSC-VinDC-Out",
            "PMIC-1-ASIC1-VDD-Out-1",
            "PMIC-1-PVIN1-VDD-ASIC1-In-1",
            "PMIC-2-ASIC1-AVDD-PL0-Out-1",
            "PMIC-2-ASIC1-DVDD-PL0-Out-2",
            "PMIC-2-PVIN1-AVDD-DVDD-ASIC1-In-1",
            "PMIC-3-ASIC1-AVDD-PL1-Out-1",
            "PMIC-3-ASIC1-DVDD-PL1-Out-2",
            "PMIC-3-PVIN1-AVDD-DVDD-ASIC1-In-1",
            "PMIC-4-ASIC1-AVCC-PL0-PL1-Out-1",
            "PMIC-4-ASIC1-HVDD-PL0-PL1-Out-2",
            "PMIC-4-PVIN1-AVCC-HVDD-ASIC1-In-1",
            "PMIC-5-ASIC2-VDD-Out-1",
            "PMIC-5-PVIN1-VDD-ASIC2-In-1",
            "PMIC-6-ASIC2-AVDD-PL0-Out-1",
            "PMIC-6-ASIC2-DVDD-PL0-Out-2",
            "PMIC-6-PVIN1-AVDD-DVDD-ASIC2-In-1",
            "PMIC-7-ASIC2-AVDD-PL1-Out-1",
            "PMIC-7-ASIC2-DVDD-PL1-Out-2",
            "PMIC-7-PVIN1-AVDD-DVDD-ASIC2-In-1",
            "PMIC-8-ASIC2-AVCC-PL0-PL1-Out-1",
            "PMIC-8-ASIC2-HVDD-PL0-PL1-Out-2",
            "PMIC-8-PVIN1-AVCC-HVDD-ASIC2-In-1",
            "PMIC-9-ASIC3-VDD-Out-1",
            "PMIC-9-PVIN1-VDD-ASIC3-In-1",
            "PMIC-10-ASIC3-AVDD-PL0-Out-1",
            "PMIC-10-ASIC3-DVDD-PL0-Out-2",
            "PMIC-10-PVIN1-AVDD-DVDD-ASIC3-In-1",
            "PMIC-11-ASIC3-AVDD-PL1-Out-1",
            "PMIC-11-ASIC3-DVDD-PL1-Out-2",
            "PMIC-11-PVIN1-AVDD-DVDD-ASIC3-In-1",
            "PMIC-12-ASIC3-AVCC-PL0-PL1-Out-1",
            "PMIC-12-ASIC3-HVDD-PL0-PL1-Out-2",
            "PMIC-12-PVIN1-AVCC-HVDD-ASIC3-In-1",
            "PMIC-13-ASIC4-VDD-Out-1",
            "PMIC-13-PVIN1-VDD-ASIC4-In-1",
            "PMIC-14-ASIC4-AVDD-PL0-Out-1",
            "PMIC-14-ASIC4-DVDD-PL0-Out-2",
            "PMIC-14-PVIN1-AVDD-DVDD-ASIC4-In-1",
            "PMIC-15-ASIC4-AVDD-PL1-Out-1",
            "PMIC-15-ASIC4-DVDD-PL1-Out-2",
            "PMIC-15-PVIN1-AVDD-DVDD-ASIC4-In-1",
            "PMIC-16-ASIC4-AVCC-PL0-PL1-Out-1",
            "PMIC-16-ASIC4-HVDD-PL0-PL1-Out-2",
            "PMIC-16-PVIN1-AVCC-HVDD-ASIC4-In-1",
            "PMIC-17-12V-MAIN-In-1",
            "PMIC-17-CPU-Out-1",
            "PMIC-17-SOC-Out-2",
            "PMIC-18-COMEX-VDD-MEM-In-1",
            "PMIC-18-COMEX-VDD-MEM-Out-1"
        ]

        self.leakage_sensors_count = 2
        self.list_of_leakages = [f"LEAKAGE-{i}" for i in range(1, self.leakage_sensors_count + 1)]
        self.expected_operation_durations.update({
            self.generate_tech_support: 165,
            'julietscaleout generate_tech_support': 165,
            'reboot with new user FW': 450 if is_bug_active(4854038) else 390,
            InternalNvosConsts.ACP_PORT_GOES_UP: InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED,
        })
        if is_bug_active(4694678):
            power_cycle_increase_timeout = 60
            # Make sure all operations that depend on power cycle have a longer timeout because of the bug 4694678.
            self.expected_operation_durations.update({
                operation: self.expected_operation_durations[operation] + power_cycle_increase_timeout
                for operation in ('install sma', 'install bios', 'juliet-power-cycle', ActionConsts.POWER_CYCLE)
            })
        self.nvl_access_ports_list = [
            'acp1', 'acp2', 'acp3', 'acp4', 'acp5', 'acp6',
            'acp7', 'acp8', 'acp9', 'acp10', 'acp11', 'acp12',
            'acp13', 'acp14', 'acp15', 'acp16', 'acp17', 'acp18',
            'acp19', 'acp20', 'acp21', 'acp22', 'acp23', 'acp24',
            'acp25', 'acp26', 'acp27', 'acp28', 'acp29', 'acp30',
            'acp31', 'acp32', 'acp33', 'acp34', 'acp35', 'acp36',
            'acp37', 'acp38', 'acp39', 'acp40', 'acp41', 'acp42',
            'acp43', 'acp44', 'acp45', 'acp46', 'acp47', 'acp48',
            'acp49', 'acp50', 'acp51', 'acp52', 'acp53', 'acp54',
            'acp55', 'acp56', 'acp57', 'acp58', 'acp59', 'acp60',
            'acp61', 'acp62', 'acp63', 'acp64', 'acp65', 'acp66',
            'acp67', 'acp68', 'acp69', 'acp70', 'acp71', 'acp72',
            'acp73', 'acp74', 'acp75', 'acp76', 'acp77', 'acp78',
            'acp79', 'acp80', 'acp81', 'acp82', 'acp83', 'acp84',
            'acp85', 'acp86', 'acp87', 'acp88', 'acp89', 'acp90',
            'acp91', 'acp92', 'acp93', 'acp94', 'acp95', 'acp96',
            'acp97', 'acp98', 'acp99', 'acp100', 'acp101', 'acp102',
            'acp103', 'acp104', 'acp105', 'acp106', 'acp107', 'acp108',
            'acp109', 'acp110', 'acp111', 'acp112', 'acp113', 'acp114',
            'acp115', 'acp116', 'acp117', 'acp118', 'acp119', 'acp120',
            'acp121', 'acp122', 'acp123', 'acp124', 'acp125', 'acp126',
            'acp127', 'acp128', 'acp129', 'acp130', 'acp131', 'acp132',
            'acp133', 'acp134', 'acp135', 'acp136', 'acp137', 'acp138',
            'acp139', 'acp140', 'acp141', 'acp142', 'acp143', 'acp144'
        ]
        self.network_ports = ['eth0', 'eth1', 'lo']
        self.all_nvl_ports_list = self.nvl_access_ports_list + self.nvl_trunk_ports_list + self.network_ports
        self.nvl_fnm_ports = []
        self.nvl_internal_fnm_ports = ["fnma0p1", "fnma0p2", "fnma1p1", "fnma1p2", "fnma2p1", "fnma2p2", "fnma3p1", "fnma3p2"]
        self.all_fae_nvl_ports_list = self.all_nvl_ports_list + self.nvl_fnm_ports + self.nvl_internal_fnm_ports
        self.nvl_port = ['acp1']

        self.techsupport_dump_empty_files_to_ignore = ['queue.counters_2', 'queue.counters_1.0', 'swapon', 'queue.counters_1',
                                                       'queue.counters_2.0', 'queue.counters_1.1', 'queue.counters_2.1',
                                                       'queue.counters_1.3', 'queue.counters_1.2', 'queue.counters_2.3',
                                                       'queue.counters_2.2', 'rsyslog.conf', 'verification_test',
                                                       'verification_test.gz', 'system.profile']

        self.nvl_trunk_port_speed = '400G'
        self.access_port_speed = '200G'  # Updated default for Rosalind systems
        self.fnm_link_speed = '200G'
        self.fnm_fae_link_speed = '200G'
        self.nvl_port_type = 'nvl'
        self.num_of_cartridges = 4
        self.requires_tpm_pass = True
        self.default_phy_recovery_counters.update({
            PhyRecoveryConsts.LAST_RS_FEC_UNCORRECTABLE_DURING_RECOVERY: 0,
            PhyRecoveryConsts.TOTAL_RS_FEC_UNCORRECTABLE_DURING_RECOVERY: 0,
            PhyRecoveryConsts.LAST_SUCCESSFUL_RECOVERY_TIME: 0,
            PhyRecoveryConsts.TOTAL_SUCCESSFUL_RECOVERY_TIME: 0,
            PhyRecoveryConsts.LAST_SUCCESSFUL_RECOVERY_STEP_ATTEMPTS: 0
        })
        self.unsupported_commands_list = ["nv show platform ps-redundancy",
                                          "nv show platform environment psu",
                                          "nv show system profile",
                                          "nv show sdn transceivers",
                                          "nv set fae interface {port} link phy-recovery logic-relock-mode",
                                          "nv set fae interface {port} link phy-recovery logic-relock-timeout",
                                          "nv set fae interface {port} link phy-recovery serdes-eq-mode",
                                          "nv set fae interface {port} link phy-recovery serdes-eq-timeout",
                                          "nv set fae interface {port} link low-power state",
                                          "nv unset fae interface {port} link low-power state"]
        # NVL7 CPO trunk-port PHY-recovery leaves + counters debug are pruned on NVL6.
        # (Removed in PortiaCpoSwitch (CPO NVL7), where they ARE supported; PortiaSimx/NPO keeps
        # them pruned.)
        self.unsupported_commands_list.extend(PhyRecoveryConsts.NVL7_PRUNED_COMMANDS)

        # Rosalind does not have fans, PSUs, or transceivers (liquid cooled)
        self.fan_list = []
        self.fan_led_list = []
        self.psu_list = []
        self.psu_fan_list = []
        self.transceiver_list = []
        self.power_capping_profiles_default_dict = {
            'compute': {
                'long-term-power-allocation': '495',
                'short-term-power-allocation': '630'
            },
            'networking': {
                'long-term-power-allocation': '630',
                'short-term-power-allocation': '630'
            },
            'reduced-bandwidth': {
                'long-term-power-allocation': '400',
                'short-term-power-allocation': '550'
            }
        }

    def _init_gnmi_consts(self):
        super()._init_gnmi_consts()
        self.sma1_xpath = 'components/component[name=SMA1]/state/firmware-version'
        self.sma2_xpath = 'components/component[name=SMA2]/state/firmware-version'
        self.components_gnmi_xpath = [self.bmc_xpath, self.bios_xpath, self.erot_xpath, self.sma1_xpath, self.sma2_xpath,
                                      self.cpld1_xpath, self.cpld2_xpath]

    def _init_fan_list(self):
        # GB300 is 100% liquid cooled
        self.fan_list = []
        self.fan_led_list = []

    def _init_services(self):
        super()._init_services()
        self.available_services.remove('hw-management-tc.service')  # TC is relevant for systems with FANs, Rosalind does not have FANs.

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": ExpectedString(regex="920-9K42W-00L6-SR0")})  # TBD -- This is for OPN, need to replace with the real one once arrive.

    def _init_led_list(self):
        super()._init_led_list()
        self.led_list = ["STATUS", "UID"]

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = [
            "ASIC1",
            "ASIC2",
            "ASIC3",
            "ASIC4",
            "CPU-Pack-Temp",
            "Drive-Temp",
            "HSC-VinDC-Temp",
            "PDB-Conv-1-Temp",
            "PDB-Conv-2-Temp",
            "PMIC-1-Temp",
            "PMIC-2-Temp",
            "PMIC-3-Temp",
            "PMIC-4-Temp",
            "PMIC-5-Temp",
            "PMIC-6-Temp",
            "PMIC-7-Temp",
            "PMIC-8-Temp",
            "PMIC-9-Temp",
            "PMIC-10-Temp",
            "PMIC-11-Temp",
            "PMIC-12-Temp",
            "PMIC-13-Temp",
            "PMIC-14-Temp",
            "PMIC-15-Temp",
            "PMIC-16-Temp",
            "PMIC-17-Temp",
            "PMIC-18-Temp",
            "SODIMM-1-Temp",
            "SODIMM-2-Temp"
        ]

    def _relevant_config_filename_by_version(self, version: str) -> str:
        return 'nvos_config_nvl6.yml'

    def _init_ib_speeds(self):
        super()._init_ib_speeds()
        self.supported_fnm_speeds = ['200G']  # Rosalind internal FNM only

    def _init_interfaces_ib_lanes(self):
        self.supported_lanes = '2X'  # Rosalind regular NVL ports (simplex + duplex)
        self.supported_internal_fnm_lanes = '2X'  # Rosalind internal FNM

# -------------------------- Rosalind Switch ----------------------------


class RosalindChipless(RosalindSurrogateSwitch):

    def __init__(self):
        super().__init__(asic_amount=0)

    def _init_constants(self):
        super()._init_constants()
        self.asic_type = NvosConst.NVL6
        # will be updated
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n6100_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N6100_LD",
            "asic-model": self.asic_type,
        })

        self.nvl_access_ports_list = [
            'acp1', 'acp2', 'acp3', 'acp4', 'acp5', 'acp6',
            'acp7', 'acp8', 'acp9', 'acp10', 'acp11', 'acp12',
            'acp13', 'acp14', 'acp15', 'acp16', 'acp17', 'acp18',
            'acp19', 'acp20', 'acp21', 'acp22', 'acp23', 'acp24',
            'acp25', 'acp26', 'acp27', 'acp28', 'acp29', 'acp30',
            'acp31', 'acp32', 'acp33', 'acp34', 'acp35', 'acp36',
            'acp37', 'acp38', 'acp39', 'acp40', 'acp41', 'acp42',
            'acp43', 'acp44', 'acp45', 'acp46', 'acp47', 'acp48',
            'acp49', 'acp50', 'acp51', 'acp52', 'acp53', 'acp54',
            'acp55', 'acp56', 'acp57', 'acp58', 'acp59', 'acp60',
            'acp61', 'acp62', 'acp63', 'acp64', 'acp65', 'acp66',
            'acp67', 'acp68', 'acp69', 'acp70', 'acp71', 'acp72',
            'acp73', 'acp74', 'acp75', 'acp76', 'acp77', 'acp78',
            'acp79', 'acp80', 'acp81', 'acp82', 'acp83', 'acp84',
            'acp85', 'acp86', 'acp87', 'acp88', 'acp89', 'acp90',
            'acp91', 'acp92', 'acp93', 'acp94', 'acp95', 'acp96',
            'acp97', 'acp98', 'acp99', 'acp100', 'acp101', 'acp102',
            'acp103', 'acp104', 'acp105', 'acp106', 'acp107', 'acp108',
            'acp109', 'acp110', 'acp111', 'acp112', 'acp113', 'acp114',
            'acp115', 'acp116', 'acp117', 'acp118', 'acp119', 'acp120',
            'acp121', 'acp122', 'acp123', 'acp124', 'acp125', 'acp126',
            'acp127', 'acp128', 'acp129', 'acp130', 'acp131', 'acp132',
            'acp133', 'acp134', 'acp135', 'acp136', 'acp137', 'acp138',
            'acp139', 'acp140', 'acp141', 'acp142', 'acp143', 'acp144'
        ]
        self.network_ports = ['eth0', 'eth1', 'lo']
        self.all_nvl_ports_list = self.nvl_access_ports_list + self.nvl_trunk_ports_list + self.network_ports
        self.nvl_fnm_ports = []
        self.nvl_internal_fnm_ports = []
        self.all_fae_nvl_ports_list = self.all_nvl_ports_list + self.nvl_fnm_ports

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": ExpectedString(regex="920-9K42W-00L6-SR2")})  # TBD -- This is for OPN, need to replace with the real one once arrive.

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = [
            "CPU-Pack-Temp",
            "Drive-Temp",
            "HSC-VinDC-Temp",
            "PDB-Conv-1-Temp",
            "PDB-Conv-2-Temp",
            "PMIC-1-Temp",
            "PMIC-2-Temp",
            "PMIC-3-Temp",
            "PMIC-4-Temp",
            "PMIC-5-Temp",
            "PMIC-6-Temp",
            "PMIC-7-Temp",
            "PMIC-8-Temp",
            "PMIC-9-Temp",
            "PMIC-10-Temp",
            "PMIC-11-Temp",
            "PMIC-12-Temp",
            "PMIC-13-Temp",
            "PMIC-14-Temp",
            "PMIC-15-Temp",
            "PMIC-16-Temp",
            "PMIC-17-Temp",
            "PMIC-18-Temp",
            "SODIMM-1-Temp",
            "SODIMM-2-Temp"
        ]

# -------------------------- Rosalind Switch ----------------------------


class RosalindSwitch(RosalindSurrogateSwitch):
    def __init__(self, asic_amount=4):
        super().__init__(asic_amount=asic_amount)

    def update_show_platform_output(self, platform_output):
        part_number = (platform_output.get(PlatformConsts.FW_PART_NUMBER) or "").strip()
        self.show_platform_output[PlatformConsts.ASIC_REVISION] = PlatformConsts.ROSALIND_ASIC_REVISION_BY_PART_NUMBER.get(
            part_number,
            PlatformConsts.ROSALIND_ASIC_REVISION_4,
        )

    def _init_constants(self):
        super()._init_constants()
        self.asic_type = NvosConst.NVL6
        self.supported_nvl_speeds = ['200G', '328G']  # Only approved speeds.
        # MHz -> NVL link speeds allowed for that FAE core-clock (operational profile).
        self.fae_supported_core_clocks = {
            755: ['328G'],
            800: self.supported_nvl_speeds,
        }
        self.default_core_clock = 800
        # Note: Rosalind has no regular FNM (nvl_fnm_ports is empty), only internal FNM

        # Rosalind cluster apps expose additional internal defaults in show output.
        internal_defaults = {
            ALTERNATE_CERTIFICATE: Defaults.ALTERNATE_CERTIFICATE,
            CA_CERTIFICATE: Defaults.CACERT,
            CERTIFICATE: Defaults.CERT,
            CRL: Defaults.CRL,
            ENCRYPTION: Defaults.ENCRYPTION,
        }
        for app_name, app_data in self.cluster_app.items():
            self.cluster_app[app_name] = {**app_data, "internal": internal_defaults.copy()}

        # Only nmx-controller exposes log-stream and OTLP configuration in its show output.
        self.cluster_app[ClusterConsts.NMX_CONTROLLER].update({
            "log-stream": {
                CA_CERTIFICATE: Defaults.CACERT,
                CERTIFICATE: Defaults.CERT,
                CRL: Defaults.CRL,
                ENCRYPTION: Defaults.ENCRYPTION,
                PROTOCOL: Defaults.PROTOCOL,
                REMOTE_URL: Defaults.REMOTE_URL,
            },
            "otlp": {
                CA_CERTIFICATE: Defaults.CACERT,
                CERTIFICATE: Defaults.CERT,
                CRL: Defaults.CRL,
                ENCRYPTION: Defaults.ENCRYPTION,
                ENDPOINT: Defaults.ENDPOINT,
            },
        })

        # Rosalind has nmx-telemetry-agent
        self.has_nmx_telemetry_agent = True

        # Expected ACL rule counts after migration (device-specific, can be overridden)
        self.expected_acl_rule_counts.update({
            'acl-default-whitelist': 27,
            'acl-default-whitelist-ipv6': 26})

        # SDN Configuration edit commands for Rosalind
        # Rosalind does NOT need FM config edits (no fm_config_edits needed)
        self.sdn_fm_config_edits_standalone = None
        # Rosalind-RTF needs FM config edits (fm_config_edits needed)
        self.sdn_fm_config_edits = [
            "sudo sed -i '/^MNNVL_TOPOLOGY=/c\\MNNVL_TOPOLOGY=vr_nvl8r1_c2g4_rtf_topology' {file}",
            "sudo grep -q '^MNNVL_TOPOLOGY=' {file} || echo 'MNNVL_TOPOLOGY=vr_nvl8r1_c2g4_rtf_topology' | sudo tee -a {file}",
            "sudo sed -i '/^PVT_MNNVL_SKIP_SINGLE_CHASSIS_SN_VALIDATION=/c\\PVT_MNNVL_SKIP_SINGLE_CHASSIS_SN_VALIDATION=1' {file}",
            "sudo grep -q '^PVT_MNNVL_SKIP_SINGLE_CHASSIS_SN_VALIDATION=' {file} || echo 'PVT_MNNVL_SKIP_SINGLE_CHASSIS_SN_VALIDATION=1' | sudo tee -a {file}"
        ]
        # Rosalind ONLY edits SM config (and only when cluster is enabled, not loopbox-dependent)
        self.sdn_sm_config_edits = [
            "# Ensure nvlink_enable=FALSE",
            "sudo sed -i 's/^nvlink_enable .*/nvlink_enable FALSE/' {file}",
            "# Comment plugin_name grpc_mgr",
            "sudo sed -i '/^plugin_name grpc_mgr/s/^/#/' {file}",
            "# Comment plugin_options -grpc_mgr",
            "sudo sed -i '/^plugin_options -grpc_mgr.*/s/^/#/' {file}"
        ]
        # Rosalind requires explicit cluster setup before SM config generation (pre-cluster config)
        # Flag to indicate this device needs pre-cluster setup
        self.sdn_needs_pre_cluster_setup = True

        self.nmx_cluster_apps_versions_file_path = (
            "/auto/sw_system_project/NVOS_INFRA/verification_files/nmx-versions/rosalind_versions.json"
        )

        # TODO -- Define the following new file. It has only 2 cplds instead of 3/4
        self.fw_versions_json_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/rosalind_versions.json"
        self.nmx_cluster_apps_versions_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/nmx-versions/rosalind_versions.json"
        # will be updated
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n6100_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N6100_LD",
            "asic-model": self.asic_type,
        })

        # Rosalind does not have fans, PSUs, or transceivers (liquid cooled) - must be set AFTER super()
        self.fan_list = []
        self.fan_led_list = []
        self.psu_list = []
        self.psu_fan_list = []
        self.transceiver_list = []

        self.expected_operation_durations.update({
            InstallSteps.SYSTEM_IS_READY_AFTER_MANUFACTURE: 10 * MINUTE,
            InstallSteps.SYSTEM_IS_READY_AFTER_UPGRADE: 10 * MINUTE,
        })

        self.nvl_access_ports_list = [
            'acp1', 'acp2', 'acp3', 'acp4', 'acp5', 'acp6',
            'acp7', 'acp8', 'acp9', 'acp10', 'acp11', 'acp12',
            'acp13', 'acp14', 'acp15', 'acp16', 'acp17', 'acp18',
            'acp19', 'acp20', 'acp21', 'acp22', 'acp23', 'acp24',
            'acp25', 'acp26', 'acp27', 'acp28', 'acp29', 'acp30',
            'acp31', 'acp32', 'acp33', 'acp34', 'acp35', 'acp36',
            'acp37', 'acp38', 'acp39', 'acp40', 'acp41', 'acp42',
            'acp43', 'acp44', 'acp45', 'acp46', 'acp47', 'acp48',
            'acp49', 'acp50', 'acp51', 'acp52', 'acp53', 'acp54',
            'acp55', 'acp56', 'acp57', 'acp58', 'acp59', 'acp60',
            'acp61', 'acp62', 'acp63', 'acp64', 'acp65', 'acp66',
            'acp67', 'acp68', 'acp69', 'acp70', 'acp71', 'acp72',
            'acp73', 'acp74', 'acp75', 'acp76', 'acp77', 'acp78',
            'acp79', 'acp80', 'acp81', 'acp82', 'acp83', 'acp84',
            'acp85', 'acp86', 'acp87', 'acp88', 'acp89', 'acp90',
            'acp91', 'acp92', 'acp93', 'acp94', 'acp95', 'acp96',
            'acp97', 'acp98', 'acp99', 'acp100', 'acp101', 'acp102',
            'acp103', 'acp104', 'acp105', 'acp106', 'acp107', 'acp108',
            'acp109', 'acp110', 'acp111', 'acp112', 'acp113', 'acp114',
            'acp115', 'acp116', 'acp117', 'acp118', 'acp119', 'acp120',
            'acp121', 'acp122', 'acp123', 'acp124', 'acp125', 'acp126',
            'acp127', 'acp128', 'acp129', 'acp130', 'acp131', 'acp132',
            'acp133', 'acp134', 'acp135', 'acp136', 'acp137', 'acp138',
            'acp139', 'acp140', 'acp141', 'acp142', 'acp143', 'acp144',
            'acp145', 'acp146', 'acp147', 'acp148', 'acp149', 'acp150',
            'acp151', 'acp152', 'acp153', 'acp154', 'acp155', 'acp156',
            'acp157', 'acp158', 'acp159', 'acp160', 'acp161', 'acp162',
            'acp163', 'acp164', 'acp165', 'acp166', 'acp167', 'acp168',
            'acp169', 'acp170', 'acp171', 'acp172', 'acp173', 'acp174',
            'acp175', 'acp176', 'acp177', 'acp178', 'acp179', 'acp180',
            'acp181', 'acp182', 'acp183', 'acp184', 'acp185', 'acp186',
            'acp187', 'acp188', 'acp189', 'acp190', 'acp191', 'acp192',
            'acp193', 'acp194', 'acp195', 'acp196', 'acp197', 'acp198',
            'acp199', 'acp200', 'acp201', 'acp202', 'acp203', 'acp204',
            'acp205', 'acp206', 'acp207', 'acp208', 'acp209', 'acp210',
            'acp211', 'acp212', 'acp213', 'acp214', 'acp215', 'acp216',
            'acp217', 'acp218', 'acp219', 'acp220', 'acp221', 'acp222',
            'acp223', 'acp224', 'acp225', 'acp226', 'acp227', 'acp228',
            'acp229', 'acp230', 'acp231', 'acp232', 'acp233', 'acp234',
            'acp235', 'acp236', 'acp237', 'acp238', 'acp239', 'acp240',
            'acp241', 'acp242', 'acp243', 'acp244', 'acp245', 'acp246',
            'acp247', 'acp248', 'acp249', 'acp250', 'acp251', 'acp252',
            'acp253', 'acp254', 'acp255', 'acp256', 'acp257', 'acp258',
            'acp259', 'acp260', 'acp261', 'acp262', 'acp263', 'acp264',
            'acp265', 'acp266', 'acp267', 'acp268', 'acp269', 'acp270',
            'acp271', 'acp272', 'acp273', 'acp274', 'acp275', 'acp276',
            'acp277', 'acp278', 'acp279', 'acp280', 'acp281', 'acp282',
            'acp283', 'acp284', 'acp285', 'acp286', 'acp287', 'acp288'
        ]
        self.network_ports = ['eth0', 'eth1', 'lo']
        self.all_nvl_ports_list = self.nvl_access_ports_list + self.nvl_trunk_ports_list + self.network_ports
        self.nvl_fnm_ports = []
        self.nvl_internal_fnm_ports = ["fnma0p1", "fnma0p2", "fnma1p1", "fnma1p2", "fnma2p1", "fnma2p2", "fnma3p1",
                                       "fnma3p2"]
        self.all_fae_nvl_ports_list = self.all_nvl_ports_list + self.nvl_fnm_ports
        self.requires_tpm_pass = True
        # These attributes should be only on QTM4
        self.default_phy_recovery_attributes = {
            PhyRecoveryConsts.LINK_DOWN_TIMEOUT: '150',
            PhyRecoveryConsts.RECOVERY_ENTRY_REASON: 'Received_TS1',
            PhyRecoveryConsts.RECOVERY_NEGATIVE_TYPE: PhyRecoveryConsts.AUTO,
            PhyRecoveryConsts.RECOVERY_STATUS: PhyRecoveryConsts.ENABLED,
            PhyRecoveryConsts.RECOVERY_SUPPORTED: 'true',
            PhyRecoveryConsts.STEP_1: {
                PhyRecoveryConsts.PEQ_NUMBER_OF_RETRY_PRESET1: '0',
                PhyRecoveryConsts.PEQ_NUMBER_OF_RETRY_PRESET2: '0',
                PhyRecoveryConsts.PEQ_NUMBER_OF_RETRY_PRESET3: '2',
                PhyRecoveryConsts.PRESENT_MODE: PhyRecoveryConsts.FULL_DUPLEX,
                PhyRecoveryConsts.STATE_60_TIMEOUT: '1500',
                PhyRecoveryConsts.STATE_61_TIMEOUT: '1500',
                PhyRecoveryConsts.STATE_62_TIMEOUT: '1500',
                PhyRecoveryConsts.STATE_65_TO_66_TIME_PRESET1: '0',
                PhyRecoveryConsts.STATE_65_TO_66_TIME_PRESET2: '0',
                PhyRecoveryConsts.STATE_65_TO_66_TIME_PRESET3: '1000',
                PhyRecoveryConsts.STATE_66_TO_67_TIME_PRESET1: '0',
                PhyRecoveryConsts.STATE_66_TO_67_TIME_PRESET2: '0',
                PhyRecoveryConsts.STATE_66_TO_67_TIME_PRESET3: '15000',
                PhyRecoveryConsts.STATE_67_TO_68_TIME_PRESET1: '0',
                PhyRecoveryConsts.STATE_67_TO_68_TIME_PRESET2: '0',
                PhyRecoveryConsts.STATE_67_TO_68_TIME_PRESET3: '32000',
            },
            PhyRecoveryConsts.STEP_2: {
                PhyRecoveryConsts.PEQ_NUMBER_OF_RETRY_PRESET1: '0',
                PhyRecoveryConsts.PEQ_NUMBER_OF_RETRY_PRESET2: '0',
                PhyRecoveryConsts.PEQ_NUMBER_OF_RETRY_PRESET3: '0',
                PhyRecoveryConsts.PRESENT_MODE: PhyRecoveryConsts.AUTO,
                PhyRecoveryConsts.STATE_60_TIMEOUT: '0',
                PhyRecoveryConsts.STATE_60_TO_LINKUP_TIMEOUT: '0',
                PhyRecoveryConsts.STATE_61_TIMEOUT: '0',
                PhyRecoveryConsts.STATE_62_TIMEOUT: '0',
                PhyRecoveryConsts.STATE_65_TO_66_TIME_PRESET1: '0',
                PhyRecoveryConsts.STATE_65_TO_66_TIME_PRESET2: '0',
                PhyRecoveryConsts.STATE_65_TO_66_TIME_PRESET3: '0',
                PhyRecoveryConsts.STATE_66_TO_67_TIME_PRESET1: '0',
                PhyRecoveryConsts.STATE_66_TO_67_TIME_PRESET2: '0',
                PhyRecoveryConsts.STATE_66_TO_67_TIME_PRESET3: '0',
                PhyRecoveryConsts.STATE_67_TO_68_TIME_PRESET1: '0',
                PhyRecoveryConsts.STATE_67_TO_68_TIME_PRESET2: '0',
                PhyRecoveryConsts.STATE_67_TO_68_TIME_PRESET3: '0',
            }
        }
        self.memory_size: list[float] = [30.77, 31.21]
        self.supported_disk_list: list[SSDConsts.SSDType] = [SSDConsts.VTPM24GLXI160_BM11, SSDConsts.VTPM24GLXI160_BM12]
        self.constants = self.constants._replace(
            dump_files=self.constants.dump_files + ['bkv'])
        self.access_port_speed = '328G'

        self.techsupport_etc_empty_files_to_ignore += ['gpu_telemetry_enable']

    def _init_fan_list(self):
        """Rosalind does not have fans (100% liquid cooled)"""
        self.fan_list = []
        self.fan_led_list = []

    def _init_psu_list(self):
        """Rosalind does not have PSUs (100% liquid cooled)"""
        self.psu_list = []
        self.psu_fan_list = []

    def setup_cluster_for_sdn_config(self, cluster, engine, dut_engines):
        """
        Rosalind-specific: Setup cluster before generating SDN configs.
        This method sets the cluster node primary server and enables the cluster.
        Note: Cleanup is handled by the disabled_access_ports decorator (cluster.unset).
        """
        from ngts.nvos_constants.constants_nvos import SystemConsts
        from ngts.tools.test_utils import allure_utils as allure

        with allure.step("Set cluster nodes"):
            for _, dut_engine in dut_engines.items():
                cluster.node.primary.set_cluster_node(op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER,
                                                      op_param_value=dut_engine.ip, apply=True,
                                                      dut_engine=engine)

    def wa_restart_nv_bridge_after_sm_config(self, cluster, engine):
        """
        Rosalind-specific workaround for Bug SW #4910763.
        After loading SM config on Rosalind, restart nv-bridge to recover connections.
        This should be called after SM config is installed.
        """
        from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
        from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
        from ngts.tools.test_utils import allure_utils as allure

        # Only apply workaround if bug is active
        if is_bug_active(4910763):
            with allure.step("WA for Bug 4910763: Restart nv-bridge after SM config"):
                logger.info("Stopping nmx-controller app")
                cluster.apps.app_name['nmx-controller'].action_stop_cluster_app(engine=engine)

                logger.info("Starting nmx-controller app")
                cluster.apps.app_name['nmx-controller'].action_start_cluster_app(engine=engine)

                logger.info("Waiting for nmx-controller to be ready after restart")
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state='up', engine=engine)
        else:
            logger.info("Bug 4910763 is not active, skipping nv-bridge restart workaround")

    def _init_ib_speeds(self):
        super()._init_ib_speeds()
        # Rosalind has only internal FNM (no regular FNM)
        self.supported_fnm_speeds = ['200G']  # Internal FNM only

    def _init_interfaces_ib_lanes(self):
        self.supported_lanes = '1X,2X'  # Rosalind regular NVL ports (simplex + duplex)
        self.supported_internal_fnm_lanes = '2X'  # Rosalind internal FNM

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {}
        self.platform_inventory_switch_values.update({
            "hardware-version": None,
            "model": ExpectedString(
                regex="699-23809-0600-EB1|920-9K42W-00L6-GS0|920-9K42W-00L6-EB2|920-9K24W-00L6-ES1|"
                      "920-9K42W-00L6-TS1|920-9K42W-00L6-TS2|920-9K42W-1313-TS3|920-9K42W-1313-TS4|920-9K42W-1313-TS5"),
        })  # TBD -- This is for OPN, need to replace with the real one once arrive.


# -------------------------- Rosalind Stacked Switch ----------------------------


class RosalindStackedSwitch(RosalindSwitch):

    def __init__(self, asic_amount=4):
        super().__init__(asic_amount=asic_amount)

    def _init_constants(self):
        super()._init_constants()

        self.cpld_amount = 3
        self._extend_firmware_by_cpld_amount()

        self.fw_versions_json_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/rosalind_stacked_0400_versions.json"
        # will be updated
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n6300_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N6300_LD",
            "asic-model": self.asic_type,
        })

        self.all_nvl_ports_list = self.nvl_access_ports_list + self.nvl_trunk_ports_list + self.network_ports
        self.nvl_fnm_ports = []
        self.nvl_internal_fnm_ports = ["fnma0p1", "fnma1p1", "fnma2p1", "fnma3p1"]
        self.all_fae_nvl_ports_list = self.all_nvl_ports_list + self.nvl_fnm_ports

        self.voltage_sensors = [
            "PDB-1-Conv-In-1",
            "PDB-1-Conv-Out-1",
            "PDB-2-Conv-In-1",
            "PDB-2-Conv-Out-1",
            "HSC-VinDC-In",
            "HSC-VinDC-Out",
            "PMIC-1-ASIC1-VDD-Out-1",
            "PMIC-1-PVIN1-VDD-ASIC1-In-1",
            "PMIC-2-ASIC1-AVDD-PL0-Out-1",
            "PMIC-2-ASIC1-DVDD-PL0-Out-2",
            "PMIC-2-PVIN1-AVDD-DVDD-ASIC1-In-1",
            "PMIC-3-ASIC1-AVDD-PL1-Out-1",
            "PMIC-3-ASIC1-DVDD-PL1-Out-2",
            "PMIC-3-PVIN1-AVDD-DVDD-ASIC1-In-1",
            "PMIC-4-ASIC1-AVCC-PL0-PL1-Out-1",
            "PMIC-4-ASIC1-HVDD-PL0-PL1-Out-2",
            "PMIC-4-PVIN1-AVCC-HVDD-ASIC1-In-1",
            "PMIC-5-ASIC2-VDD-Out-1",
            "PMIC-5-PVIN1-VDD-ASIC2-In-1",
            "PMIC-6-ASIC2-AVDD-PL0-Out-1",
            "PMIC-6-ASIC2-DVDD-PL0-Out-2",
            "PMIC-6-PVIN1-AVDD-DVDD-ASIC2-In-1",
            "PMIC-7-ASIC2-AVDD-PL1-Out-1",
            "PMIC-7-ASIC2-DVDD-PL1-Out-2",
            "PMIC-7-PVIN1-AVDD-DVDD-ASIC2-In-1",
            "PMIC-8-ASIC2-AVCC-PL0-PL1-Out-1",
            "PMIC-8-ASIC2-HVDD-PL0-PL1-Out-2",
            "PMIC-8-PVIN1-AVCC-HVDD-ASIC2-In-1",
            "PMIC-9-ASIC3-VDD-Out-1",
            "PMIC-9-PVIN1-VDD-ASIC3-In-1",
            "PMIC-10-ASIC3-AVDD-PL0-Out-1",
            "PMIC-10-ASIC3-DVDD-PL0-Out-2",
            "PMIC-10-PVIN1-AVDD-DVDD-ASIC3-In-1",
            "PMIC-11-ASIC3-AVDD-PL1-Out-1",
            "PMIC-11-ASIC3-DVDD-PL1-Out-2",
            "PMIC-11-PVIN1-AVDD-DVDD-ASIC3-In-1",
            "PMIC-12-ASIC3-AVCC-PL0-PL1-Out-1",
            "PMIC-12-ASIC3-HVDD-PL0-PL1-Out-2",
            "PMIC-12-PVIN1-AVCC-HVDD-ASIC3-In-1",
            "PMIC-13-ASIC4-VDD-Out-1",
            "PMIC-13-PVIN1-VDD-ASIC4-In-1",
            "PMIC-14-ASIC4-AVDD-PL0-Out-1",
            "PMIC-14-ASIC4-DVDD-PL0-Out-2",
            "PMIC-14-PVIN1-AVDD-DVDD-ASIC4-In-1",
            "PMIC-15-ASIC4-AVDD-PL1-Out-1",
            "PMIC-15-ASIC4-DVDD-PL1-Out-2",
            "PMIC-15-PVIN1-AVDD-DVDD-ASIC4-In-1",
            "PMIC-16-ASIC4-AVCC-PL0-PL1-Out-1",
            "PMIC-16-ASIC4-HVDD-PL0-PL1-Out-2",
            "PMIC-16-PVIN1-AVCC-HVDD-ASIC4-In-1",
            "PMIC-17-12V-MAIN-In-1",
            "PMIC-17-CPU-Out-1",
            "PMIC-17-SOC-Out-2",
            "PMIC-18-COMEX-VDD-MEM-In-1",
            "PMIC-18-COMEX-VDD-MEM-Out-1"
        ]

    def _init_ib_speeds(self):
        super()._init_ib_speeds()
        # Rosalind has only internal FNM (no regular FNM)
        self.supported_fnm_speeds = ['200G']  # Internal FNM only

    def _init_interfaces_ib_lanes(self):
        self.supported_lanes = '1X,2X'  # Rosalind regular NVL ports (simplex + duplex)
        self.supported_internal_fnm_lanes = '2X'  # Rosalind internal FNM

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {}
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": ExpectedString(regex="699-23809-0600-EB1|920-9K42W-00L6-GS0|920-9K42W-00L6-EB2|920-9K24W-00L6-ES1|920-9K42W-00L6-TS1|920-9K42W-00L6-TS2|920-9K42W-2310-EB1")})  # TBD -- This is for OPN, need to replace with the real one once arrive.

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = [
            "ASIC1",
            "ASIC2",
            "ASIC3",
            "ASIC4",
            "CPU-Pack-Temp",
            "Drive-Temp",
            "HSC-VinDC-Temp",
            "PDB-Conv-1-Temp",
            "PDB-Conv-2-Temp",
            "PMIC-1-Temp",
            "PMIC-2-Temp",
            "PMIC-3-Temp",
            "PMIC-4-Temp",
            "PMIC-5-Temp",
            "PMIC-6-Temp",
            "PMIC-7-Temp",
            "PMIC-8-Temp",
            "PMIC-9-Temp",
            "PMIC-10-Temp",
            "PMIC-11-Temp",
            "PMIC-12-Temp",
            "PMIC-13-Temp",
            "PMIC-14-Temp",
            "PMIC-15-Temp",
            "PMIC-16-Temp",
            "PMIC-17-Temp",
            "PMIC-18-Temp",
            "SODIMM-1-Temp",
            "SODIMM-2-Temp"
        ]

# -------------------------- RosalindStackedSimx Switch ---------------------


class SimxDevice:
    """Overlay for simx-emulated flavors of real switch classes.

    Holds only the emulation gaps shared by every simx flavor: the device is
    marked as simx and requires the mloop loopback setup for links to come up.
    Platform/SKU differences (asic amount, ports, sensors) stay in the switch
    classes. Must precede the switch class in the bases list:
    class FooSimx(SimxDevice, FooSwitch).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_simx = True
        self.require_mloop_setup = True


class RosalindStackedSimx(SimxDevice, RosalindStackedSwitch):

    def __init__(self):
        super().__init__(asic_amount=4)

# -------------------------- RosalindSimx Switch ----------------------------


class RosalindSimx(SimxDevice, RosalindSwitch):

    def update_show_platform_output(self, platform_output):
        pass

    def __init__(self, asic_amount=4):
        super().__init__(asic_amount=asic_amount)

    def _init_constants(self):
        super()._init_constants()
        self.asic_type = NvosConst.NVL6
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n6100_ld-r0")
        self.show_platform_output.update({
            "system-type": "N6100_LD",
            "asic-model": self.asic_type,
            PlatformConsts.ASIC_REVISION: PlatformConsts.FW_ASIC_REVISION_VALUE,
        })

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = [
            "ASIC1",
            "ASIC2",
            "ASIC3",
            "ASIC4",
            "CPU-Pack-Temp",
            "Drive-Temp",
            "HSC-VinDC-Temp",
            "PDB-Conv-1-Temp",
            "PDB-Conv-2-Temp",
            "PMIC-1-Temp",
            "PMIC-2-Temp",
            "PMIC-3-Temp",
            "PMIC-4-Temp",
            "PMIC-5-Temp",
            "PMIC-6-Temp",
            "PMIC-7-Temp",
            "PMIC-8-Temp",
            "PMIC-9-Temp",
            "PMIC-10-Temp",
            "PMIC-11-Temp",
            "PMIC-12-Temp",
            "PMIC-13-Temp",
            "PMIC-14-Temp",
            "PMIC-15-Temp",
            "PMIC-16-Temp",
            "PMIC-17-Temp",
            "PMIC-18-Temp"
        ]

        self.voltage_sensors = [
            "PDB-HSC-Volt-In",
            "PDB-HSC-Volt-Out",
            "PDB-PwrConv1-In-1",
            "PDB-PwrConv1-Out-1",
            "PDB-PwrConv2-In-1",
            "PDB-PwrConv2-Out-1",
            "PMIC-1-ASIC1-VDD-Out-1",
            "PMIC-1-PVIN1-VDD-ASIC1-In-1",
            "PMIC-2-ASIC1-AVDD-PL0-Out-1",
            "PMIC-2-ASIC1-DVDD-PL0-Out-2",
            "PMIC-2-PVIN1-AVDD-DVDD-ASIC1-In-1",
            "PMIC-3-ASIC1-AVDD-PL1-Out-1",
            "PMIC-3-ASIC1-DVDD-PL1-Out-2",
            "PMIC-3-PVIN1-AVDD-DVDD-ASIC1-In-1",
            "PMIC-4-ASIC1-AVCC-PL0-PL1-Out-1",
            "PMIC-4-ASIC1-HVDD-PL0-PL1-Out-2",
            "PMIC-4-PVIN1-AVCC-HVDD-ASIC1-In-1",
            "PMIC-5-ASIC2-VDD-Out-1",
            "PMIC-5-PVIN1-VDD-ASIC2-In-1",
            "PMIC-6-ASIC2-AVDD-PL0-Out-1",
            "PMIC-6-ASIC2-DVDD-PL0-Out-2",
            "PMIC-6-PVIN1-AVDD-DVDD-ASIC2-In-1",
            "PMIC-7-ASIC2-AVDD-PL1-Out-1",
            "PMIC-7-ASIC2-DVDD-PL1-Out-2",
            "PMIC-7-PVIN1-AVDD-DVDD-ASIC2-In-1",
            "PMIC-8-ASIC2-AVCC-PL0-PL1-Out-1",
            "PMIC-8-ASIC2-HVDD-PL0-PL1-Out-2",
            "PMIC-8-PVIN1-AVCC-HVDD-ASIC2-In-1",
            "PMIC-9-ASIC3-VDD-Out-1",
            "PMIC-9-PVIN1-VDD-ASIC3-In-1",
            "PMIC-10-ASIC3-AVDD-PL0-Out-1",
            "PMIC-10-ASIC3-DVDD-PL0-Out-2",
            "PMIC-10-PVIN1-AVDD-DVDD-ASIC3-In-1",
            "PMIC-11-ASIC3-AVDD-PL1-Out-1",
            "PMIC-11-ASIC3-DVDD-PL1-Out-2",
            "PMIC-11-PVIN1-AVDD-DVDD-ASIC3-In-1",
            "PMIC-12-ASIC3-AVCC-PL0-PL1-Out-1",
            "PMIC-12-ASIC3-HVDD-PL0-PL1-Out-2",
            "PMIC-12-PVIN1-AVCC-HVDD-ASIC3-In-1",
            "PMIC-13-ASIC4-VDD-Out-1",
            "PMIC-13-PVIN1-VDD-ASIC4-In-1",
            "PMIC-14-ASIC4-AVDD-PL0-Out-1",
            "PMIC-14-ASIC4-DVDD-PL0-Out-2",
            "PMIC-14-PVIN1-AVDD-DVDD-ASIC4-In-1",
            "PMIC-15-ASIC4-AVDD-PL1-Out-1",
            "PMIC-15-ASIC4-DVDD-PL1-Out-2",
            "PMIC-15-PVIN1-AVDD-DVDD-ASIC4-In-1",
            "PMIC-16-ASIC4-AVCC-PL0-PL1-Out-1",
            "PMIC-16-ASIC4-HVDD-PL0-PL1-Out-2",
            "PMIC-16-PVIN1-AVCC-HVDD-ASIC4-In-1",
            "PMIC-17-12V-MAIN-In-1",
            "PMIC-17-CPU-Out-1",
            "PMIC-17-SOC-Out-2",
            "PMIC-18-COMEX-VDD-MEM-In-1",
            "PMIC-18-COMEX-VDD-MEM-Out-1"
        ]


# -------------------------- RosalindSA Switch ----------------------------


class RosalindSA(RosalindSimx):

    def __init__(self):
        super().__init__(asic_amount=1)


# -------------------------- RosalindRTF Switch ----------------------------


class RosalindRTF(RosalindSwitch):

    def __init__(self):
        super().__init__(asic_amount=4)

    def get_configurable_nvl_speeds(self):
        # DUT still advertises 200G in supported_nvl_speeds, but configuring it on RTF
        # needs GPU-side setup — exclude from speed-change tests.
        return [s for s in self.supported_nvl_speeds if s != IbInterfaceConsts.XDR_SLOW_SPEED]


# -------------------------- Portia Switch ----------------------------


class PortiaSwitch(RosalindSwitch):
    """Four-ASIC non-expandable NVL72 Portia tray (N7200_LD)."""

    FNM_PORTS_PER_ASIC = 2

    def update_show_platform_output(self, platform_output):
        # no per-part-number asic-revision map for Portia yet - the revision
        # stays pinned in show_platform_output (revisit on real HW)
        pass

    def __init__(self, asic_amount=4):
        super().__init__(asic_amount=asic_amount)

    def _init_constants(self):
        super()._init_constants()
        # Portia inherits the Rosalind device model, but it has no ERoT components.
        self.constants.erots.clear()
        self.components_list = [component for component in self.components_list if component != FW_COMPONENT_EROT]
        self.asic_type = NvosConst.NVL7
        self.core_count = 4
        self.supported_nvl_speeds = ['200G']
        self.fae_supported_core_clocks = {
            core_clock: self.supported_nvl_speeds
            for core_clock in self.fae_supported_core_clocks
        }
        self.access_port_speed = '200G'
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n7200_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N7200_LD",
            "asic-model": self.asic_type,
            PlatformConsts.ASIC_REVISION: PlatformConsts.FW_ASIC_REVISION_VALUE,
        })
        expected_firmware_components = [
            PlatformConsts.FW_ASIC,
            PlatformConsts.FW_BIOS,
            PlatformConsts.FW_SSD,
            PlatformConsts.FW_BMC,
            "transceiver",
        ]
        self.constants.firmware[:] = expected_firmware_components
        self.cpld_amount = 2
        self._extend_firmware_by_cpld_amount()
        self.sma_amount = 2
        self._extend_firmware_by_sma_amount()

    def _init_gnmi_consts(self):
        super()._init_gnmi_consts()
        self.components_gnmi_xpath = [xpath for xpath in self.components_gnmi_xpath if xpath != self.erot_xpath]

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_inventory_switch_values.update({
            "model": ExpectedString(regex="920-9K51W-02L7-GS0"),
        })

    def _init_temperature(self):
        super()._init_temperature()
        # TODO: interim two-floor inventory - the ASICn entries scale with
        # asic_amount, but the PMIC/PDB/HSC sensor set below is the one-floor
        # (4-ASIC) inventory. The second SWB floor's naming (floor-local vs
        # global PMIC numbering, extra PDB/HSC instances) must be pinned from
        # real 8-ASIC HW output before extending it.
        self.temperature_sensors = (
            [f"ASIC{asic_num}" for asic_num in range(1, self.asic_amount + 1)] +
            [
                "CPU-Pack-Temp",
                "Drive-Temp",
                "HSC-VinDC-Temp",
                "PDB-Conv-1-Temp",
                "PDB-Conv-2-Temp",
            ] +
            [f"PMIC-{pmic_num}-Temp" for pmic_num in range(1, 19)] +
            ["SODIMM-1-Temp", "SODIMM-2-Temp"]
        )
        self.voltage_sensors = [
            "PDB-HSC-Volt-In",
            "PDB-HSC-Volt-Out",
            "PDB-PwrConv1-In-1",
            "PDB-PwrConv1-Out-1",
            "PDB-PwrConv2-In-1",
            "PDB-PwrConv2-Out-1",
        ]
        # TODO: PMIC numbering beyond ASIC4 is unverified - on 8 ASICs this
        # formula yields PMIC-17..32 for the second floor, colliding with the
        # fixed PMIC-17/18 CPU/COMEX names below; pin from real 8-ASIC HW.
        for asic_num in range(1, self.asic_amount + 1):
            first_pmic_num = (asic_num - 1) * 4 + 1
            self.voltage_sensors.extend([
                f"PMIC-{first_pmic_num}-ASIC{asic_num}-VDD-Out-1",
                f"PMIC-{first_pmic_num}-PVIN1-VDD-ASIC{asic_num}-In-1",
                f"PMIC-{first_pmic_num + 1}-ASIC{asic_num}-AVDD-PL0-Out-1",
                f"PMIC-{first_pmic_num + 1}-ASIC{asic_num}-DVDD-PL0-Out-2",
                f"PMIC-{first_pmic_num + 1}-PVIN1-AVDD-DVDD-ASIC{asic_num}-In-1",
                f"PMIC-{first_pmic_num + 2}-ASIC{asic_num}-AVDD-PL1-Out-1",
                f"PMIC-{first_pmic_num + 2}-ASIC{asic_num}-DVDD-PL1-Out-2",
                f"PMIC-{first_pmic_num + 2}-PVIN1-AVDD-DVDD-ASIC{asic_num}-In-1",
                f"PMIC-{first_pmic_num + 3}-ASIC{asic_num}-AVCC-PL0-PL1-Out-1",
                f"PMIC-{first_pmic_num + 3}-ASIC{asic_num}-HVDD-PL0-PL1-Out-2",
                f"PMIC-{first_pmic_num + 3}-PVIN1-AVCC-HVDD-ASIC{asic_num}-In-1",
            ])
        self.voltage_sensors.extend([
            "PMIC-17-12V-MAIN-In-1",
            "PMIC-17-CPU-Out-1",
            "PMIC-17-SOC-Out-2",
            "PMIC-18-COMEX-VDD-MEM-In-1",
            "PMIC-18-COMEX-VDD-MEM-Out-1",
        ])

    def _init_interface_lists(self):
        super()._init_interface_lists()
        self.mgmt_ports = ['eth0']
        self.network_ports = self.mgmt_ports + ['lo']
        self.nvl_access_ports_list = [f'acp{num}' for num in range(1, 144 * self.asic_amount + 1)]
        self.nvl_trunk_ports_list = []
        self.nvl_internal_fnm_ports = [f'fnma{asic}p{port_num}'
                                       for asic in range(self.asic_amount)
                                       for port_num in range(1, self.FNM_PORTS_PER_ASIC + 1)]
        self.all_nvl_ports_list = self.nvl_access_ports_list + self.nvl_trunk_ports_list + self.network_ports
        self.all_fae_nvl_ports_list = self.all_nvl_ports_list + self.nvl_fnm_ports + self.nvl_internal_fnm_ports


# -------------------------- PortiaSimx Switch ----------------------------


class PortiaSimx(SimxDevice, PortiaSwitch):
    """Four-ASIC non-expandable NVL72 SIMX tray (N7200_LD)."""


# -------------------------- Portia CPO Switch ----------------------------


class PortiaCpoSwitch(PortiaSwitch):
    """Expandable Portia CPO tray (N7300_LD) with Gen2 CPO vModule support.

    Applies PortiaCpoCapability, which attaches a first-class CpoTopology as
    self.cpo (one CPO / vModule per ASIC, 4 OEs + 1 ELS each). One NVOS device
    spans both SWB floors. SIMX supports the full 8-ASIC model and the reduced
    2-ASIC TA model.

    Port model: per ASIC, 72 GPU-facing access ports (acp) over the backplane
    and 56 CPO scale-out trunk interfaces exposed as 7 swX groups x 8
    subports (p1s1-p1s8). The four Sagittas contribute 16, 16, 16 and 8
    connected lanes respectively. The last Sagitta's other 8 lanes are not
    user-facing, so the two ELS lasers serving them remain off. swX numbering
    is global and compact across ASICs (ASIC1: sw1-sw7, ASIC2: sw8-sw14,
    ...). acp ports are not CPO-associated.
    """

    ACP_PORTS_PER_ASIC = 72
    SW_SUBPORTS_PER_GROUP = 8
    SAGITTA_CONNECTED_LANES = (16, 16, 16, 8)
    SW_GROUPS_PER_ASIC = sum(SAGITTA_CONNECTED_LANES) // SW_SUBPORTS_PER_GROUP

    def __init__(self, asic_amount=8):
        super().__init__(asic_amount=asic_amount)
        SwitchCapabilityHandler.apply_capability(self, PortiaCpoCapability())

    def _init_constants(self):
        super()._init_constants()
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n7300_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N7300_LD",
        })
        # sw scale-out ports are 1-lane NVL7 simplex (200G PAM-4 per lane)
        self.nvl_trunk_port_speed = '200G'

        self.default_phy_recovery_debug_counters = dict(PhyRecoveryConsts.NVL7_DEFAULT_DEBUG_COUNTERS)
        for cmd in PhyRecoveryConsts.NVL7_PRUNED_COMMANDS:
            if cmd in self.unsupported_commands_list:
                self.unsupported_commands_list.remove(cmd)

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_inventory_switch_values.update({
            "model": ExpectedString(regex="920-9K57P-00L7-GS0"),
        })

    def _init_interface_lists(self):
        super()._init_interface_lists()
        self.nvl_access_ports_list = [f'acp{num}' for num in range(1, self.ACP_PORTS_PER_ASIC * self.asic_amount + 1)]
        self.nvl_trunk_ports_list = [f'sw{sw}p1s{subport}'
                                     for sw in range(1, self.SW_GROUPS_PER_ASIC * self.asic_amount + 1)
                                     for subport in range(1, self.SW_SUBPORTS_PER_GROUP + 1)]
        self.all_nvl_ports_list = self.nvl_access_ports_list + self.nvl_trunk_ports_list + self.network_ports
        self.all_fae_nvl_ports_list = self.all_nvl_ports_list + self.nvl_fnm_ports + self.nvl_internal_fnm_ports


class PortiaCpoSimx(SimxDevice, PortiaCpoSwitch):
    """Full 8-ASIC N7300_LD SIMX profile."""


class PortiaCpoTASimx(SimxDevice, PortiaCpoSwitch):
    """Reduced 2-ASIC N7300_LD_TA profile, available only in SIMX."""

    def __init__(self):
        super().__init__(asic_amount=2)

    def _init_constants(self):
        super()._init_constants()
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n7300_ld_ta-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N7300_LD_TA",
        })


# -------------------------- Portia NPO Switch ----------------------------


class PortiaNpoSwitch(PortiaSwitch):
    """Expandable Portia NPO tray (N7400_LD): 576 internal and 512 external ports."""

    ACP_PORTS_PER_ASIC = 72
    SW_GROUPS_PER_ASIC = 8
    SW_SUBPORTS_PER_GROUP = 8

    def __init__(self, asic_amount=8):
        super().__init__(asic_amount=asic_amount)

    def _init_constants(self):
        super()._init_constants()
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n7400_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N7400_LD",
        })
        self.nvl_trunk_port_speed = '200G'

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_inventory_switch_values.update({
            "model": ExpectedString(regex="920-9K57E-00L7-GS0"),
        })

    def _init_interface_lists(self):
        super()._init_interface_lists()
        self.nvl_access_ports_list = [f'acp{num}' for num in range(1, self.ACP_PORTS_PER_ASIC * self.asic_amount + 1)]
        self.nvl_trunk_ports_list = [f'sw{sw}p1s{subport}'
                                     for sw in range(1, self.SW_GROUPS_PER_ASIC * self.asic_amount + 1)
                                     for subport in range(1, self.SW_SUBPORTS_PER_GROUP + 1)]
        self.all_nvl_ports_list = self.nvl_access_ports_list + self.nvl_trunk_ports_list + self.network_ports
        self.all_fae_nvl_ports_list = self.all_nvl_ports_list + self.nvl_fnm_ports + self.nvl_internal_fnm_ports


class PortiaNpoSimx(SimxDevice, PortiaNpoSwitch):
    """Full 8-ASIC N7400_LD SIMX profile."""


class PortiaNpoTASimx(SimxDevice, PortiaNpoSwitch):
    """Reduced 2-ASIC N7400_LD_TA profile, available only in SIMX."""

    def __init__(self):
        super().__init__(asic_amount=2)

    def _init_constants(self):
        super()._init_constants()
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n7400_ld_ta-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N7400_LD_TA",
        })


class PortiaSurrogateNex(PortiaSwitch):
    """Two-ASIC Portia NEX surrogate tray (N7210_LD): no fans."""

    def __init__(self, asic_amount=2):
        super().__init__(asic_amount=asic_amount)

    def _init_constants(self):
        super()._init_constants()
        self.fw_versions_json_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/portia-surrogate-nex.json"
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n7210_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N7210_LD",
        })

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {}
        self.platform_inventory_switch_values.update({
            "hardware-version": None,
            "model": ExpectedString(regex="920-9K51W-02L7-SR0"),
        })


class PortiaSurrogateNpo(PortiaNpoSwitch):
    """Two-ASIC Portia NPO surrogate tray (N7410_LD): no fans."""

    def __init__(self, asic_amount=2):
        super().__init__(asic_amount=asic_amount)

    def _init_constants(self):
        super()._init_constants()
        self.fw_versions_json_file_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/portia-surrogate-npo.json"
        self.health_monitor_config_file_path = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(
            "x86_64-nvidia_n7410_ld-r0")
        self.show_platform_output.update({
            PlatformConsts.SYSTEM_TYPE: "N7410_LD",
        })

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_environment_fan_values = {}
        self.platform_inventory_switch_values.update({
            "hardware-version": None,
            "model": ExpectedString(regex="920-9K57E-00L7-SR0"),
        })


# -------------------------- Caiman Switch ----------------------------


class CaimanSwitch(NvLinkSwitch):

    def __init__(self):
        super().__init__(asic_amount=4)

    def _init_constants(self):
        super()._init_constants()
        self.ib_ports_num = 64
        self.core_count = 4
        self.asic_numbers = [f"ASIC{i}" for i in range(1, self.asic_amount + 1)]
        self.platform_file_path = MultiPlanarConsts.PLATFORM_FILE_FULL_PATH.format("x86_64-mlnx_mqm9700-r0")
