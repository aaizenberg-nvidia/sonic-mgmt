import logging
import os
import time
from abc import ABC, ABCMeta, abstractmethod
from collections import namedtuple

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_constants.constants_nvos import (
    ActionConsts,
    CumulusConsts,
    DatabaseConst,
    DateTimeConsts,
    DiskConsts,
    FansConsts,
    NvosConst,
    PlatformConsts,
    RebootConsts,
    SystemConsts,
)
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ngts.nvos_tools.infra.DatabaseTool import DatabaseTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.ValidationTool import ExpectedString
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.general.security.constants import SecurityConsts
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_general_utils import get_version_info

logger = logging.getLogger()


# -------------------------- Base Device ----------------------------
class BaseDevice(ABC):

    def __init__(self, switch_type="", asic_amount=1, switch_class=""):
        self.default_password = ""
        self.default_username = ""
        self.prev_default_password = ""
        self.open_api_port = ""
        self.dependent_dockers = []
        self.asic_amount = asic_amount
        self.switch_type = switch_type
        self.switch_class = switch_class
        self.cli_coverage_path = ""
        self.cli_coverage_project_name = ""
        self.cur_mgmt_port_name = ''
        self.cur_mgmt_port_ip = ''
        # simx flavors (SimxDevice subclasses) override both
        self.is_simx = False
        self.require_mloop_setup = False

        self._init_constants()
        self._init_available_databases()
        self._init_services()
        self._init_dependent_services()
        self._init_dockers()
        self._init_fan_list()
        self._init_led_list()
        self._init_psu_list()
        self._init_fan_direction_dir()
        self._init_temperature()
        self._init_health_components()
        self._init_platform_lists()
        self._init_system_lists()
        self._init_fae_lists()
        self._init_security_lists()
        self._init_password_hardening_lists()
        self._init_boot_time_timeouts()

    def init_documents_consts(self):
        self.documents_path = {}
        self.documents_files = {}

    def _init_available_databases(self):
        self.available_databases = {}
        self.available_tables = {}
        self.available_tables_per_asic = {}

    def _init_services(self):
        self.available_services = []

    def _init_dependent_services(self):
        self.dependent_services = []

    def _init_dockers(self):
        self.available_dockers = []

    def _init_constants(self):
        self.pre_login_message = ""
        self.post_login_message = ""
        self.install_from_onie_timeout = 7 * 60  # seconds
        self.install_success_patterns = ""
        self.core_count = 1
        self.asic_type = ""
        self.ib_ports_num = 0
        self.mst_dev_name = ()  # (device_for_asic_1, device_for_asic_2, ... )
        self.constants = None
        self.split_ports_supported = False
        self.profile_change_supported = False
        self.voltage_sensors = []
        self.has_nmx = False
        self.has_bmc = False
        self.supports_ssd_upgrade = False
        self.supported_commands = []
        self.supports_tpm_testing = True
        self.unset_all_command = "nv unset acl; nv unset interface; nv unset platform; nv unset system"
        self.allow_cpld_update = False
        self.sys_rst_n_bit_command = None
        self.reboot_reason_dict = {}
        self.wait_before_reboot_single_fw_install = 900
        self.techsupport_dump_empty_files_to_ignore = []
        self.techsupport_etc_empty_files_to_ignore = []
        self.techsupport_cluster_empty_files_to_ignore = []
        self.techsupport_hw_mgmt_empty_files_to_ignore = []
        self.techsupport_skynet_hw_mgmt_empty_files_to_ignore = []

    def _init_fan_list(self):
        self.fan_list = []

    def _init_led_list(self):
        self.led_list = []

    def _init_psu_list(self):
        self.psu_list = []
        self.psu_fan_list = []
        self.platform_env_psu_prop = []

    def _init_fan_direction_dir(self):
        self.fan_direction_dir = ""

    def _init_temperature(self):
        self.temperature_sensors = []

    def _init_health_components(self):
        self.health_components = []

    def _init_platform_lists(self):
        self.platform_hw_list = []

    def _init_system_lists(self):
        self.user_fields = []

    def _init_fae_lists(self):
        pass

    def _init_security_lists(self):
        self.kex_algorithms = []
        self.aaa_cleanup_cmds = []

    def _init_password_hardening_lists(self):
        self.local_test_users = []

    def _should_use_new_format(self, dut_engine=None):
        """
        Determines if the device should use new format/methods.
        Default implementation returns False.
        Override in subclasses to enable new format support.

        Args:
            dut_engine: Optional SSH engine to use for version checks.
                       If None, implementations may use TestToolkit.engines.dut.

        Returns:
            bool: True if new format should be used, False otherwise.
        """
        return False

    def _init_boot_time_timeouts(self):
        self.timeout_system_is_ready = 5 * MINUTE
        self.timeout_reboot_to_grub_menu = 3 * MINUTE
        self.cli_up_retries = 36  # 36 * 5s = 180s default

    def init_cli_coverage_prop(self, cli_coverage_project_name):
        self.cli_coverage_project_name = cli_coverage_project_name
        self.cli_coverage_path = f"/auto/sw/tools/comet/{self.cli_coverage_project_name}/"

    def handle_exception(self, dut_engine):
        logging.info("Handle exception")

    def clear_config(self, dut_engine, markers=None, default_yml_path=None, root_dir=""):
        pass

    @abstractmethod
    def get_ib_ports_num(self):
        pass

    def get_spdm_components(self, setup_name: str) -> list[str]:
        """
        Get available SPDM components (ERoTs, MCU, etc.) for this device type.
        Override in subclasses to define device-specific SPDM components.
        """
        return []

    def update_mgmt_port(self, name, ip):
        self.cur_mgmt_port_name = name
        self.cur_mgmt_port_ip = ip

    def get_mgmt_ports(self) -> list[str]:
        return None

    def get_admins_group(self):
        return 'nvapply'

    def get_default_nvue_config(self, dut_engine=None):
        return {}

    def update_show_platform_output(self, platform_output):
        """Update expected nv show platform values before validation."""
        return None

    def get_default_password_by_version(self, version: str):
        return self.default_password

    def get_test_config_file_by_version(self, version: str) -> tuple[str, str]:
        ngts_path = os.path.join(os.path.abspath(__file__).split('ngts', 1)[0], 'ngts')
        config_filename = self._relevant_config_filename_by_version(version)
        config_file_path = os.path.join(ngts_path, 'tools', 'test_utils', 'nvos_resources', config_filename)
        logger.info(f'NGTS_PATH: {ngts_path}')
        logger.info(f'CONF_YML_FILE_PATH: {config_file_path}')
        return config_file_path, config_filename

    def is_eth(self):
        return self.switch_type == CumulusConsts.ETH_SWITCH_TYPE

    def is_ib(self):
        return self.switch_type == NvosConst.IB_SWITCH_TYPE

    @property
    def has_sx_core(self):
        return self.is_ib()

    def get_lldp_port_name_from_dump(self, lldp_dict):
        """Return the port name from parsed LLDP dump; key is chosen by device type (eth vs ib)."""
        pass

    def post_reload_actions(self, engine):
        """
        Optional actions to run after a reload (e.g. factory reset).
        Override in subclasses (e.g. EthDevice) when device-specific setup is needed.
        """
        pass

    @staticmethod
    def _version_to_global_build(version: str) -> int:
        """
        Convert version string to a global build number that preserves ordering across releases.

        Before 25.03, build numbers were continuous across releases (e.g., 25.01.3932, 25.02.6079).
        From 25.03 onward, build numbers reset each release cycle (e.g., 25.03.0106).
        We offset 25.03+ builds above the 25.02 range so config file selection stays correct.

        Examples:
            25.01.3932 → 3932   (unchanged, maps to ga_4000)
            25.02.6079 → 6079   (unchanged, maps to ga_7000)
            25.03.0106 → 8106   (offset, maps to ga_7000 as closest)
            25.04.0500 → 8500   (offset, maps to ga_7000 as closest)
        """
        version_num, _ = get_version_info(version)
        if not version_num:
            return 0
        parts = version_num.split('.')
        major, minor, build = int(parts[0]), int(parts[1]), int(parts[2])
        if (major, minor) >= (25, 3):
            return 8000 + build
        return build

    def _relevant_config_filename_by_version(self, version: str) -> str:
        global_build = self._version_to_global_build(version)
        if global_build:
            relevant_ga_num_for_conf = (global_build // 1000) * 1000
            if relevant_ga_num_for_conf == 0:
                relevant_ga_num_for_conf = 4000
            return f'nvos_config_ga_{relevant_ga_num_for_conf}.yml'
        return 'nvos_config_ga_8000.yml'

    def verify_databases(self, dut_engine):
        """
        validate the redis includes all expected tables
        :param dut_engine: ssh dut engine
        Return result_obj with True result if all tables exists, False and a relevant info if one or more tables are missing
        """
        with allure.step('Verify databases'):
            time.sleep(10)
            result_obj = ResultObj(True, "")
            for db_name, db_id in self.available_databases.items():
                if db_name == DatabaseConst.STATE_DB_NAME:
                    continue

                for database_docker in self.available_tables.keys():
                    table_info = self.available_tables[database_docker][db_id]
                    for table_name, expected_entries in table_info.items():
                        output = self.get_all_table_names_in_database(dut_engine, db_name, table_name,
                                                                      database_docker=database_docker).returned_value
                        if len(output) < expected_entries:
                            result_obj.result = False
                            result_obj.info += (
                                f"Database docker: {database_docker} DB: {db_name}, Table: {table_name}. "
                                f"Table count mismatch, Expected: {str(expected_entries)} != Actual {str(len(output))}\n"
                            )

            return result_obj

    def verify_dockers(self, dut_engine, dockers_list=""):
        """
        Validate existing dockers
        """
        with allure.step('Verify dockers'):
            result_obj = ResultObj(True, "")
            cmd_output = dut_engine.run_cmd('docker ps --format \"table {{.Names}}\"')
            list_of_dockers = dockers_list or self.available_dockers
            for docker in list_of_dockers:
                if docker not in cmd_output:
                    result_obj.result = False
                    result_obj.info += f"{docker} docker is not active.\n"

            return result_obj

    def verify_services(self, dut_engine):
        """
        Validate expected dockers
        """
        with allure.step('Verify services'):
            result_obj = ResultObj(True, "")
            for service in self.available_services:
                cmd_output = dut_engine.run_cmd(f'systemctl --type=service | grep -E "{service}"')
                if NvosConst.SERVICE_STATUS_ACTIVE not in cmd_output:
                    result_obj.result = False
                    result_obj.info += f"{service} service is not active. {cmd_output} \n"

            temp_res = self.verify_nvue_service(dut_engine)
            if not temp_res.result:
                result_obj.result = False
                result_obj.info += temp_res.info

            return result_obj

    def verify_nvue_service(self, dut_engine):
        with allure.step('Verify nvue service'):
            logging.info("Verify nvued service is active")
            nvued_cmd_output = dut_engine.run_cmd_after_cmd(["sudo systemctl status nvued", 'q'])
            if NvosConst.SERVICE_STATUS_ACTIVE not in nvued_cmd_output:
                return ResultObj(False, f"nvued service is not active. info: {nvued_cmd_output}")
            return ResultObj(True)

    def get_database_id(self, db_name):
        return self.available_databases[db_name]

    def get_all_table_names_in_database(self, engine, database_name, table_name_substring='.', database_docker=None):
        """
        :param engine: dut engine
        :param database_name: database name
        :param table_name_substring: full or partial table name
        :return: any database table that includes the substring, if table_name_substring is none we will return all the tables
        """
        with allure.step('Get all table names in database'):
            result_obj = ResultObj(True, "")
            # docker_exec_cmd = 'docker exec -it {database_docker} '.format(database_docker=database_docker) if database_docker else ''
            output = DatabaseTool.sonic_db_run_get_keys_in_docker(
                docker_name=database_docker if database_docker else '', engine=engine, asic="",
                db_name=DatabaseConst.REDIS_DB_NUM_TO_NAME[self.get_database_id(database_name)],
                grep_str=table_name_substring)

            # cmd = docker_exec_cmd + "redis-cli -n {database_name} keys * | grep {prefix}".format(
            #    database_name=self.get_database_id(database_name), prefix=table_name_substring)
            # output = engine.run_cmd(cmd)
            result_obj.returned_value = output.splitlines()
            return result_obj

    @abstractmethod
    def wait_for_os_to_become_functional(self, engine, find_prompt_tries=60, find_prompt_delay=10):
        raise Exception(f"Not implemented for this switch {self.__class__.__name__}")

    @abstractmethod
    def reload_device(self, engine, cmd_list, validate=False):
        raise Exception(f"Not implemented for this switch {self.__class__.__name__}")

    @abstractmethod
    def get_voltage_sensors(self, dut_engine=None):
        raise Exception(f"Not implemented for this switch {self.__class__.__name__}")

    @abstractmethod
    def show_setup_versions(self, dut_engine: LinuxSshEngine = None):
        raise Exception(f"Not implemented for this switch {self.__class__.__name__}")

    @abstractmethod
    def setup_base_aaa_config(self, dut_engine: LinuxSshEngine):
        raise Exception(f"Not implemented for this switch {self.__class__.__name__}")

    @abstractmethod
    def cleanup_base_aaa_config(self, dut_engine: LinuxSshEngine):
        raise Exception(f"Not implemented for this switch {self.__class__.__name__}")

    def bypass_password_on_sudo_commands(self, dut_engine: LinuxSshEngine):
        pass

    def is_nvl(self):
        return self.switch_type == NvosConst.NVL_SWITCH_TYPE

    def get_system_log_date_str_for_parsing(self, timestamp_match_str):
        """
        Return the timestamp string as needed for strptime with TIMESTAMP_FORMATS[3].
        Override in EthDevice to convert ISO 8601 to '%b %d %H:%M:%S.%f' format.
        """
        return timestamp_match_str

# -------------------------- Base Appliance ----------------------------


class BaseAppliance(BaseDevice):
    def __init__(self):
        BaseDevice.__init__(self)

    @abstractmethod
    def eth_ports_num(self):
        pass


# -------------------------- Base Switch ----------------------------
class BaseSwitch(BaseDevice):
    __metaclass__ = ABCMeta

    Constants = namedtuple('Constants', ['system', 'dump_files', 'sdk_dump_files', 'sdk_dump_files_template', 'firmware',
                                         'log_dump_files', 'log_nginx_files', 'log_nmx_files', 'stats_dump_files',
                                         'hw_mgmt_files', 'audit_files', 'etc_files', 'cluster_files', 'bmc_dump_files', 'erots',
                                         'kdump_files', 'techsupport_size_limit_mb'])

    @staticmethod
    def get_sdk_dump_files_for_asic(asic_num, sdk_dump_files_template):
        """
        Generate SDK dump file list for a specific ASIC.

        For multi-ASIC systems, SDK dump files contain device identifiers:
        - ASIC 0 (sai_sdk_dump0) → dev1
        - ASIC 1 (sai_sdk_dump1) → dev2
        - ASIC 2 (sai_sdk_dump2) → dev3
        - ASIC 3 (sai_sdk_dump3) → dev4

        Args:
            asic_num: ASIC number (0, 1, 2, 3, ...)
            sdk_dump_files_template: Base template list with dev1 placeholders

        Returns:
            List of SDK dump files with correct dev{N} identifiers
        """
        dev_num = asic_num + 1
        return [filename.replace('dev1', f'dev{dev_num}') for filename in sdk_dump_files_template]
    CpldImageConsts = namedtuple('CpldImageConsts', ('burn_image_path', 'refresh_image_path', 'version_names'))
    SsdImageConsts = namedtuple('SsdImageConsts', ('file', 'current_version', 'alternate_version'))
    BiosImagesConsts = namedtuple('BiosImagesConsts', ('current_version', 'alternate_version'))
    AsicImageConsts = namedtuple('AsicImageConsts', ('version', 'filename'))

    def init_documents_consts(self):
        super().init_documents_consts()

    # Gorilla and Eth override it as they don't support it
    def verify_sed_password(self, tpm_tool, sed_default_password=SecurityConsts.SED_DEFAULT_PASSWORD):
        password_from_tpm = tpm_tool.get_sed_password_primary_bank(device=self)
        assert password_from_tpm == sed_default_password, "Pass from tpm should be default SED pass"
        password_from_tpm = tpm_tool.get_sed_password_secondary_bank(device=self)
        assert password_from_tpm == sed_default_password, "Pass from tpm should be default SED pass"

    def _init_available_databases(self):
        super()._init_available_databases()

    def _init_services(self):
        super()._init_services()

    def get_ib_ports_num(self):
        return self.ib_ports_num

    def _init_dockers(self):
        super()._init_dockers()

    def _init_constants(self):
        super()._init_constants()
        # nv-bridge config flag: set to True in subclasses that require it (e.g., RosalindSurrogateSwitch)
        self.requires_nv_bridge_config = False
        system_dic = {
            'system': [SystemConsts.HOSTNAME, SystemConsts.PRODUCT_NAME, SystemConsts.VERSION, SystemConsts.UPTIME,
                       SystemConsts.PRODUCT_RELEASE, SystemConsts.HEALTH, SystemConsts.DATE_TIME, SystemConsts.STATUS],
            'message': [SystemConsts.PRE_LOGIN_MESSAGE, SystemConsts.POST_LOGIN_MESSAGE],
            'reboot': [SystemConsts.REBOOT_REASON],
            'version': [SystemConsts.VERSION_BASE_OS, SystemConsts.VERSION_IMAGE, SystemConsts.VERSION_KERNEL,
                        SystemConsts.VERSION_ONIE, SystemConsts.VERSION_PRODUCT_RELEASE],
            'date-time': [
                DateTimeConsts.LOCAL_TIME,
                DateTimeConsts.UNIVERSAL_TIME,
                DateTimeConsts.RTC_TIME,
                DateTimeConsts.TIMEZONE,
                DateTimeConsts.SYSTEM_CLOCK_SYNCHRONIZED,
                DateTimeConsts.NTP_SERVICE,
                DateTimeConsts.RTC_IN_LOCAL_TZ,
                DateTimeConsts.UNIX_TIME
            ]
        }
        dump_files = ['APPL_DB.json', 'ASIC_DB.json', 'boot.conf', 'bridge.fdb', 'bridge.vlan', 'CONFIG_DB.json',
                      'COUNTERS_DB_1.json', 'COUNTERS_DB_2.json', 'COUNTERS_DB.json', 'date.counter_1',
                      'date.counter_2', 'df', 'dmesg', 'docker.pmon', 'docker.ps', 'docker.stats',
                      'docker.swss-ibv0.log', 'dpkg', 'fan', 'FLEX_COUNTER_DB.json', 'free', 'hdparm',
                      'ifconfig.counters_1', 'ifconfig.counters_2', 'interface.status', 'interface.xcvrs.eeprom',
                      'interface.xcvrs.presence', 'ip.addr', 'ip.interface', 'ip.link', 'ip.link.stats', 'ip.neigh',
                      'ip.neigh.noarp', 'ip.route', 'ip.rule', 'lspci', 'lsusb', 'machine.conf', 'mount',
                      'netstat.counters_1', 'netstat.counters_2', 'platform.summary', 'ps.aux', 'ps.extended',
                      'psustatus', 'queue.counters_1', 'queue.counters_2', 'reboot.cause',
                      'saidump', 'sensors', 'services.summary', 'ssdhealth', 'STATE_DB.json', 'swapon', 'sysctl',
                      'syseeprom', 'systemd.analyze.blame', 'systemd.analyze.dump', 'systemd.analyze.plot.svg',
                      'temperature', 'top', 'version', 'vlan.summary', 'vmstat', 'vmstat.m', 'vmstat.s', 'who']
        sdk_dump_files = ['fw_trace_attr.json.gz', 'sai_sdk_dump.gz', "fw_trace_attr.json.gz",
                          'sdk_dump_ext_dev1_summary.txt.gz', 'sdk_dump_ext_dev1_cr_space_2.udmp.gz',
                          'sdk_dump_ext_dev1_cr_space_1.udmp.gz', 'sdk_dump_ext_dev1_cr_space_2.udmp.gz', 'sdk_dump_ext_dev1_cr_space_3.udmp.gz', 'sdk_dump_ext_dev1_dpt.txt.gz',
                          'sdk_dump_ext_dev1_fw_trace.txt.gz', 'fw_trace_attr.json.gz', 'fw_trace_string_db.json.gz',
                          'sai_sdk_dump.json.gz', 'sdk_dump_ext_dev1_cr_space_1.udmp.gz',
                          'sdk_dump_ext_dev1_cr_space_3.udmp.gz', 'sdk_dump_ext_dev1_driver.txt.gz',
                          'sdk_dump_ext_dev1_amber.hex.gz']

        # Base SDK dump files template (dev1 is the template, will be replaced per ASIC)
        sdk_dump_files_template = sdk_dump_files
        log_dump_files = ["auth.log.gz", "btmp.gz", "cron.log.gz",
                          "firewall_packet_capture.log.gz", "health_history.gz",
                          "nv-cli.log.gz", "nvued.log.gz", "syslog.gz", "tc_log.gz", "wtmp.gz", "ztp.log.gz"]
        log_nginx_files = ["access.log.gz", "error.log.gz"]

        stats_dump_files = ["cpu.csv.gz", "disk.csv.gz", "fan.csv.gz", "power.csv.gz",
                            "mgmt-interface.csv.gz", "temperature.csv.gz", "voltage.csv.gz"]
        hw_mgmt_files = ['cpld_reg_direct_dump', 'cpld_dump']
        audit_files = ['audit.log.gz']
        log_nmx_files = []
        etc_files = ["resolv.conf"]
        kdump_files = ["dmesg.{}.gz", "kdump.{}", "kdump_lock.gz"]

        bmc_dump_files = None
        cluster_files = None

        firmware = [PlatformConsts.FW_ASIC, PlatformConsts.FW_BIOS, PlatformConsts.FW_SSD]
        erots = []
        techsupport_size_limit_mb = 75  # Default for older systems (pre-Surrogate)
        self.constants = BaseSwitch.Constants(system_dic, dump_files, sdk_dump_files, sdk_dump_files_template, firmware, log_dump_files,
                                              log_nginx_files, log_nmx_files, stats_dump_files, hw_mgmt_files, audit_files, etc_files, cluster_files,
                                              bmc_dump_files, erots, kdump_files, techsupport_size_limit_mb)

        self.current_bios_version_name = ""
        self.current_bios_version_path = ""
        self.previous_bios_version_name = ""
        self.previous_bios_version_path = ""
        self.fw_versions_json_file_path = None
        self.ssd_pkg_info_path = "/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/SSD/ssd_pkg_info.json"
        self.show_platform_output = {
            "system-mac": ExpectedString(regex=r"([\dA-F]{2}:){5}[\dA-F]{2}"),
            "manufacturer": "Nvidia",
            "system-type": "",     # These fields need to be updated in subclasses.
            "cpu": None,            # `None` means we expect any string not in ['', 'N/A'].
            "memory": ExpectedString.number_and_string('GB', range_min=6),  # Expects "x GB" where x > 6
            "disk-size": ExpectedString.number_and_string('GB', range_min=14.0),
            "port-layout": None,
            "part-number": None,
            "serial-number": None,
            "asic-model": "",
            "asic-revision": PlatformConsts.FW_ASIC_REVISION_VALUE,
            "system-uuid": ExpectedString(regex=r"[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}"),
        }
        self.disk_default_partition_name = DiskConsts.DEFAULT_PARTITION_NAME
        self.disk_partition_capacity_limit = DiskConsts.PARTITION_CAPACITY_LIMIT
        self.disk_minimum_free_space = DiskConsts.MINIMUM_FREE_SPACE
        self.reboot_type = 'reboot'  # If system has special reboot (in terms of time) this var will describe it. Used for expected_operation_durations lookup
        self.generate_tech_support = 'generate tech-support'
        self.reset_factory = 'reset factory'
        self.power_cycle_type = 'power-cycle'
        # Initialize empty expected_operation_durations, then populate via _init_expected_operation_durations()
        self.expected_operation_durations = {}
        self._init_expected_operation_durations()

    def _init_expected_operation_durations(self):
        """
        Initialize all expected operation durations for timing validations.

        All thresholds are defined here for simplicity. Child device classes
        can override specific values (e.g., generate_tech_support) by calling
        super()._init_expected_operation_durations() first, then updating.

        Note: Values with bug-conditional adjustments use is_bug_active() for temporary workarounds.
        Multiplier-based values (stressed resources, etc.) are pre-calculated for clarity.
        """
        self.expected_operation_durations.update({
            # Reboot operations
            'reboot': 225,
            'julietscaleout_reboot': 270,
            'julietscaleout reset factory': 600,
            'reset factory': 300,
            'reset bit': 360,

            # Firmware installation operations
            'install user FW': 500,
            'install default fw': 360,
            'reboot with default FW installation': 500,
            'reboot with new user FW': 525,  # Pre-calculated: 500 * 1.05
            'reboot with single package FW': 1200,

            # Port operations
            'port goes up': 30,
            'port goes down': 4,

            # System operations
            'set hostname': 12,
            self.generate_tech_support: 75,  # Default for IB devices; Eth overrides to 130
            'julietscaleout generate_tech_support': 132,  # Pre-calculated: 120 * 1.1

            # Cluster operations
            'start stop cluster app': 810,
            'start stop cluster app with loopbox': 720,
            'start stop cluster': 285,
            'start stop cluster app stressed resources': 891,  # Pre-calculated: 810 * 1.1
            'start stop cluster app stressed resources with loopbox': 792,  # Pre-calculated: 720 * 1.1
            'start stop cluster stressed resources': 313.5,  # Pre-calculated: 285 * 1.1
            'cluster update log level': 6,

            # Platform firmware component installations
            'install bmc': 1200,
            'bmc install without reboot': 710,
            'install fpga': 900,
            'fpga install without reboot': 130,
            'install sma': 400,
            'install asic': 600,
            'install bios': 600,
            'bios install without reboot': 250,
            'install cpld': 720,
            'cpld install without reboot': 400,
            'install erot': 420,

            # Power operations
            ActionConsts.POWER_CYCLE: 360,
            'juliet-power-cycle': 335,

            # Security and user operations
            'users disconnection by inactivity timeout': 65,
            'enable snmp': 22,
            'activate els': 75,
        })

    def _init_psu_list(self):
        super()._init_psu_list()
        self.psu_list = ["PSU1", "PSU2"]
        self.psu_fan_list = ["PSU1/FAN", "PSU2/FAN"]
        self.platform_env_psu_prop = ["capacity", "current", "power", "state", "voltage"]

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = ["ASIC1", "Ambient-Fan-Side-Temp", "Ambient-Port-Side-Temp",
                                    "CPU-Core-0-Temp", "CPU-Core-1-Temp", "CPU-Pack-Temp",
                                    "PSU-1-Temp", "Drive-Temp", "PMIC-3-Temp", "PMIC-4-Temp",
                                    "PMIC-5-Temp", "PMIC-1-Temp", "PMIC-6-Temp"]

    def _init_health_components(self):
        super()._init_health_components()
        self.health_components = self.fan_list + self.psu_list + self.psu_fan_list + \
            ["ASIC-HEALTH", "Containers", "CPU utilization", "Disk check", "Disk space",
             "Disk space log"]

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_hw_list = ["asic-count", "cpu", "cpu-load-averages", "disk-size", "hw-revision", "manufacturer",
                                 "memory", "model", "onie-version", "part-number", "product-name", "serial-number",
                                 "system-mac", "system-uuid"]
        self.fan_prop_auto = {"Fan State": "state", "Current Speed (RPM)": "current-speed",
                              "Fan Direction": "direction"}
        self.platform_inventory_items = self.fan_list + self.psu_list + [PlatformConsts.HW_COMP_SWITCH]
        self.platform_inventory_items_dict = {
            'fan': self.fan_list, 'psu': self.psu_list, 'switch': [PlatformConsts.HW_COMP_SWITCH]}
        self.platform_inventory_fields = ["hardware-version", "model", "serial", "state", "type"]
        self.platform_inventory_fan_values = {
            "hardware-version": NvosConst.NOT_AVAILABLE, "model": NvosConst.NOT_AVAILABLE,
            "serial": NvosConst.NOT_AVAILABLE, "state": FansConsts.STATE_OK, "type": PlatformConsts.ENV_FAN}
        self.platform_inventory_psu_values = {
            "hardware-version": None, "model": None,
            "serial": None, "state": FansConsts.STATE_OK, "type": PlatformConsts.ENV_PSU}
        self.platform_inventory_switch_values = {
            "hardware-version": "", "model": "",  # update these in subclasses
            "serial": None, "state": FansConsts.STATE_OK, "type": PlatformConsts.HW_COMP_SWITCH.lower()}
        self.platform_port_state = {
            "polling-physical-state": IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_POLLING
        }
        self.platform_inventory_values = {"fan": self.platform_inventory_fan_values,
                                          "psu": self.platform_inventory_psu_values,
                                          "switch": self.platform_inventory_switch_values}
        self.platform_environment_absent_fan_values = {
            "state": FansConsts.STATE_ABSENT, "direction": "N/A", "current-speed": "N/A",
            "min-speed": "N/A", "max-speed": "N/A"}

    def _init_fae_lists(self):
        super()._init_fae_lists()
        self.fae_eeprom_values = {}

    def _init_fan_direction_dir(self):
        super()._init_fan_direction_dir()
        self.fan_direction_dir = "/var/run/hw-management/thermal"

    def get_default_config_yml(self, engine, root_dir):
        return ""

    def _extend_firmware_by_cpld_amount(self):
        if hasattr(self, "cpld_amount"):
            self.constants.firmware[:] = [fw for fw in self.constants.firmware if not fw.startswith(PlatformConsts.FW_CPLD)]
            self.constants.firmware.extend(
                PlatformConsts.FW_CPLD + str(i) for i in range(1, self.cpld_amount + 1)
            )

    def _extend_firmware_by_sma_amount(self):
        # We display SMA, and SMA1, SMA2. Similar to erot not CPLD.
        if hasattr(self, "sma_amount"):
            self.constants.firmware.extend(
                f"{PlatformConsts.FW_SMA}{i if i else ''}"
                for i in range(0, self.sma_amount + 1)
            )
