import allure
import logging
import os
import time
from datetime import datetime
from typing import List

import pexpect

from packaging.version import Version
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_constants.constants_nvos import NvosConst, FansConsts, PlatformConsts, CumulusConsts, OperationTimeConsts, SystemConsts, ApiType, NtpConsts, TcpDumpConsts, RebootConsts, ImageConsts
from ngts.nvos_tools.Devices.BaseDevice import BaseSwitch
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ExpectedString
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.security_test_tools.constants import AaaConsts
from ngts.tools.test_utils.nvos_config_utils import clear_cl_conf
from ngts.cli_wrappers.nvue.cumulus.cumulus_general_cli import CumulusGeneralCli

logger = logging.getLogger()

# Version threshold for new authentication order format
AUTH_ORDER_CHANGE_VERSION = '5.15.0'


class EthSwitch(BaseSwitch):

    def __init__(self, asic_amount):
        super().__init__()
        self.asic_amount = asic_amount
        self.mgmt_ports = ['eth0']
        self._init_sensors_dict()
        self.open_api_port = "8765"
        self.default_password = os.environ["CUMULUS_SWITCH_PASSWORD"]
        self.default_username = os.environ["CUMULUS_SWITCH_USER"]
        self.manufacture_password = "cumulus"
        self.switch_type = CumulusConsts.ETH_SWITCH_TYPE
        self.port_type = "swp"
        self.init_documents_consts()
        self.init_cli_coverage_prop("cumulus")
        self._init_eth0_speeds()
        self._init_eth0_duplex()
        # Cumulus reports factory reset as "SW asserted reset through CPLD", user "system/root"
        self.reboot_reason_dict = {
            RebootConsts.FACTORY_RESET: ("SW asserted reset through CPLD", RebootConsts.REBOOT_USER_SYSTEM),
        }

    def init_documents_consts(self):
        super().init_documents_consts()

    def verify_sed_password(self, tpm_tool, sed_default_password=""):
        pass  # This should be ignored on eth switches, overrides method from base switch

    @staticmethod
    def check_fec_capability():
        """Ethernet switches do not have IB FEC (NVLink-6) capability."""
        return False

    def get_voltage_sensors(self, dut_engine=None):
        return self.voltage_sensors

    def get_lldp_port_name_from_dump(self, lldp_dict):
        """Return the port name from parsed LLDP dump; eth uses Port Description TLV."""
        return lldp_dict[TcpDumpConsts.LLDP_PORT_DESCRIPTION]

    def get_default_nvue_config(self, dut_engine=None):
        default_conf = NvosConst.DEFAULT_CL_CONFIG
        default_conf["interface"] = NvosConst.DEFAULT_CL_IFACE_CONFIG
        return default_conf

    def show_setup_versions(self, dut_engine: LinuxSshEngine = None):
        with allure.step('Show setup versions'):
            system = System(force_api=ApiType.NVUE)
            platform = Platform()
            outputs = {
                'system version': system.version.show(dut_engine=dut_engine),
                'platform firmware': platform.firmware.show(dut_engine=dut_engine),
            }
            res = [f'{title.upper()}:\n{output}\n' for title, output in outputs.items()]
            return '\n'.join(res)

    def clear_config(self, dut_engine, markers=None, default_yml_path=None, root_dir=""):
        with allure.step('Clear config'):
            clear_cl_conf(dut_engine, markers, self)

    def post_reload_actions(self, engine):
        """Update sudoers for nopasswd after reload (e.g. factory reset) so log-analyzer can run."""
        CumulusGeneralCli(engine, self).update_sudoers_nopasswd()

    def get_health_issue_dict_fan_and_psu(self, psu_display_name, fan_display_name):
        """Return expected health issues dict for simulated fan+PSU fault (ETH/non-IB)."""
        return {
            psu_display_name: {"bad"},
            fan_display_name: "bad",
        }

    def check_and_set_default_password(self, engine):
        """Connect via SSH; if image requires default password change, set it and update engine.password.

        Initial login uses manufacturing password; the new password is always CUMULUS_SWITCH_PASSWORD
        (self.default_password). It must be set and must differ from the manufacturing default.
        """
        cur = self.manufacture_password
        new = self.default_password
        if not new or new == self.manufacture_password:
            logger.info(
                "Skipping Cumulus forced password change: set CUMULUS_SWITCH_PASSWORD to the target password "
                "(not the manufacturing default)."
            )
            return
        ssh_cmd = f'ssh -o "UserKnownHostsFile=/dev/null" -o "StrictHostKeyChecking=no" -o "PubkeyAuthentication=no" -p {engine.ssh_port} {self.default_username}@{engine.ip}'
        logger.info("Checking if DUT requires default password change...")
        session = pexpect.spawn(ssh_cmd, use_poll=True, encoding='utf-8')
        logfile = open(os.devnull, 'w')
        session.logfile = logfile
        changed = False
        try:
            if session.expect([r".*'s password:", pexpect.TIMEOUT, pexpect.EOF], timeout=30) != 0:
                logger.info("SSH did not prompt for password")
                return
            session.sendline(cur)
            idx = session.expect([r"\(current\) UNIX password: ", r"Current password: ", r".*'s password:", pexpect.TIMEOUT, pexpect.EOF], timeout=30)
            if idx <= 1:
                session.sendline(cur)
                session.sendline(new)
                session.sendline(new)
                changed = True
                session.expect([r"cumulus@.*[$#]", r".*[$#]", pexpect.TIMEOUT, pexpect.EOF], timeout=10)
            elif idx == 2:
                session.expect([r"cumulus@.*[$#]", r".*[$#]", pexpect.TIMEOUT], timeout=10)
            time.sleep(2)
        except (pexpect.exceptions.TIMEOUT, pexpect.exceptions.EOF, Exception) as e:
            logger.info("Password check skipped/failed (non-fatal): %s", e)
            return
        finally:
            session.close()
            logfile.close()
        if changed:
            engine.password = new
            logger.info("Engine password updated for subsequent connections.")

    def _init_constants(self):
        super()._init_constants()
        self.pre_login_message = "Welcome to NVIDIA Cumulus (R) Linux (R)\n"
        self.post_login_message = "Welcome to NVIDIA Cumulus (R) Linux (R)\n" \
                                  "For support and online technical documentation, visit https://www.nvidia.com/en-us/support\n" \
                                  "The registered trademark Linux (R) is used pursuant to a sublicense from LMI, "\
                                  "the exclusive licensee of Linus Torvalds, owner of the mark on a world-wide basis.\n"
        self.install_from_onie_timeout = 10 * 60
        self.login_pattern = CumulusConsts.LINUX_BOOT_PATTERN
        self.install_patterns = {self.login_pattern: 0, NvosConst.INSTALL_BOOT_PATTERN: 1,
                                 CumulusConsts.LOGIN_BOOT_PATTERN: 2}
        self.install_success_patterns = list(self.install_patterns.keys())

        # Techsupport constants for ETH devices
        self.techsupport_files_path = CumulusConsts.TECHSUPPORT_FILES_PATH
        self.techsupport_upload_success_message = 'Successfully uploaded the file'
        self.techsupport_delete_success_message = CumulusConsts.TECHSUPPORT_ACTION_DELETE_SUCCESS_MESSAGE
        self.techsupport_threshold = CumulusConsts.TECHSUPPORT_THRESHOLD
        self.techsupport_file_not_found_message = CumulusConsts.TECHSUPPORT_FILE_NOT_FOUND_MESSAGE
        self.ntp_service_name = 'ntpsec@mgmt'
        self.ntp_reboot_action_param = ""
        self.ntp_dict = NtpConsts.CUMULUS_NTP_DEFAULT_DICT
        self.ntp_default_reference = NtpConsts.REFERENCE_DEFAULT_VALUE
        self.ntp_expected_time = NtpConsts.CUMULUS_EXPECTED_DURATION
        self.ntp_server1_values_dict = NtpConsts.CUMULUS_SERVER1_DEFAULT_VALUES_DICT
        self.ntp_server_none_values_dict = NtpConsts.CUMULUS_SERVER_NONE_DEFAULT_VALUES_DICT
        self.ntp_multiple_servers_values_dict = NtpConsts.CUMULUS_MULTIPLE_SERVERS_CONFIG_DICT

        # System message constants for ETH devices
        self.post_logout_message = SystemConsts.POST_LOGOUT_MESSAGE_DEFAULT_VALUE

        # System image constants for ETH devices
        self.nfs_server = CumulusConsts.CL_NFS_SERVER
        self.upload_path = '://{}:{}@{}/tmp/'.format(NvosConst.ROOT_USER, NvosConst.ROOT_PASSWORD, NvosConst.FIT70)
        self.default_username = SystemConsts.DEFAULT_USER_CL

        self.voltage_sensors = ["PMIC-1-PSU-12V-RAIL-IN", "PMIC-2-PSU-12V-RAIL-IN",
                                "PMIC-2-ASIC-1.2V_MAIN-RAIL-OUT2", "PMIC-2-ASIC-1.8V_MAIN-RAIL-OUT1",
                                "PMIC-3-ASIC-1.8V_T0_3-RAIL-OUT2", "PMIC-3-COMEX-1.05V-RAIL-OUT",
                                "PMIC-3-PSU-12V-RAIL-IN", "PMIC-3-PSU-12V-RAIL-IN1",
                                "PMIC-5-ASIC-1.2V_T0_3-RAIL-OUT1", "PMIC-5-ASIC-1.2V_T4_7-RAIL-OUT2",
                                "PMIC-5-PSU-12V-RAIL-IN", "PMIC-6-COMEX-1.8V-RAIL-OUT1",
                                "PMIC-6-PSU-12V-RAIL-IN1", "PMIC-6-PSU-12V-RAIL-IN2",
                                "PMIC-7-COMEX-1.2V-RAIL-OUT", "PMIC-7-PSU-12V-RAIL-IN1",
                                "PMIC-7-PSU-12V-RAIL-IN2", "PSU-2L-12V-RAIL-OUT",
                                "PSU-2L-220V-RAIL-IN"]
        self.constants.firmware.remove(PlatformConsts.FW_ASIC)

        self.show_platform_output.update({
            "system-mac": ExpectedString(regex=r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}"),
            "manufacturer": "Mellanox"
        })

        self.disk_partition_capacity_limit = 70  # Percent value
        self.disk_minimum_free_space = 5.5  # Gig
        self.ib_ports_num = 32
        self.supports_tpm_testing = False
        self.supports_ima_testing = False
        # Note: expected_operation_durations is populated via _init_expected_operation_durations()
        self.asic_numbers = [f"ASIC{i}" for i in range(1, self.asic_amount + 1)]
        self.constants.system['system'].remove(SystemConsts.PRODUCT_RELEASE)
        self.constants.system['system'].remove(SystemConsts.STATUS)
        self.constants.system['system'].append(SystemConsts.GLOBAL)
        self.constants.system['system'].append(SystemConsts.DNS)
        self.constants.system['system'].append(SystemConsts.FQDN)
        self.system_default_value_dict = {
            SystemConsts.HOSTNAME: NvosConst.DEFAULT_CL_CONFIG['system']['hostname'],
            SystemConsts.CONTACT: "",
            SystemConsts.LOCATION: ""
        }
        self.system_log_value = "system log"
        self.system_log_timestamp_regex = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?'

    def get_system_log_date_str_for_parsing(self, timestamp_match_str):
        """Convert ISO 8601 syslog timestamp to '%b %d %H:%M:%S.%f' for common strptime parsing."""
        return datetime.fromisoformat(timestamp_match_str).strftime("%b %d %H:%M:%S.%f")

    def _init_expected_operation_durations(self):
        """Initialize expected operation durations for Ethernet devices."""
        super()._init_expected_operation_durations()
        self.expected_operation_durations.update({
            self.generate_tech_support: 130
        })
        self.fetch_success_message = CumulusConsts.FETCH_SUCCESS_MESSAGE
        self.fetch_error_message = CumulusConsts.FETCH_ERROR_MESSAGE
        self.ask_for_confirmation = True

    def get_base_image(self):
        release_name = ImageConsts.CL_RELEASE_5_16_0
        return ImageConsts.CL_BASE_IMAGE_VERSION_TO_INSTALL.format(
            pre_release_name=release_name
        )

    def get_base_image_path(self, image_file):
        release_name = ImageConsts.CL_RELEASE_5_16_0
        return ImageConsts.CL_BASE_IMAGE_VERSION_TO_INSTALL_PATH.format(
            pre_release_name=release_name,
            base_image=image_file
        )

    def get_upload_prefix(self):
        return "://{}:{}@{}/tmp/".format(
            NvosConst.ROOT_USER,
            NvosConst.ROOT_PASSWORD,
            NvosConst.FIT70
        )

    def wait_for_os_to_become_functional(self, engine, find_prompt_tries=60, find_prompt_delay=10):
        with allure.step('Wait for OS to become functional'):
            try:
                self.check_and_set_default_password(engine)
            except Exception as e:
                logger.info("Password check/set skipped or failed (non-fatal): %s", e)
            result_obj = DutUtilsTool.wait_for_cumulus_to_become_functional(engine)
            return result_obj

    def reload_device(self, engine, cmd_set, validate=False):
        with allure.step('Reload device'):
            # If cmd_set has multiple commands (command + confirmation), handle interactively
            if isinstance(cmd_set, list) and len(cmd_set) > 1:
                command = cmd_set[0]
                confirmation = cmd_set[1]
                # Send command and wait for confirmation prompt
                output = engine.engine.send_command_expect(
                    command,
                    expect_string="Do you want to continue?",
                    max_loops=40
                )
                # Send confirmation response
                engine.engine.write_channel(f"{confirmation}\n".encode())
                return output
            else:
                # Single command, no confirmation needed
                return engine.run_cmd_set(cmd_set, validate=False)

    def get_mgmt_ports(self) -> List[str]:
        return self.mgmt_ports

    def get_admins_group(self):
        return 'cumulus'

    def _init_eth0_speeds(self):
        self.supported_eth0_speeds = ['10M', '100M', '1G']

    def _init_eth0_duplex(self):
        self.supported_eth0_duplex = ['full']

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2",
                         "FAN5/1", "FAN5/2", "FAN6/1", "FAN6/2"]

    def _init_led_list(self):
        self.led_list = ["FAN1", "FAN2", "FAN3", "FAN4", "FAN5", "FAN6", "PSU", "SYSTEM"]

    def _init_platform_lists(self):
        super()._init_platform_lists()
        self.platform_hw_list = ["base-mac", "cpu", "disk-size", "manufacturer", "memory", "model", "onie-version",
                                 "part-number", "platform-name", "port-layout", "product-name", "serial-number",
                                 "system-mac", "asic-model", "asic-vendor"]
        self.hw_comp_list = ["device"]
        self.hw_comp_prop = ["model", "type"]
        self.fan_prop = ["max-speed", "min-speed", "speed", "state"]
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=2000, range_max=10000),
            "max-speed": ExpectedString(range_min=19500, range_max=40000)}
        self.platform_environment_absent_fan_values = {
            "state": FansConsts.STATE_ABSENT, "direction": "N/A", "current-speed": "N/A",
            "min-speed": "N/A", "max-speed": "N/A"}
        self.platform_inventory_items = self.fan_list + self.psu_list + self.psu_fan_list \
            + [PlatformConsts.HW_COMP_SWITCH]
        self.platform_inventory_switch_values.update({"hardware-version": None,
                                                      "model": ExpectedString(regex="MSN.*")})

    def _init_psu_list(self):
        self.psu_list = ["PSU1", "PSU2"]
        self.psu_fan_list = ["PSU1/FAN", "PSU2/FAN"]
        self.platform_env_psu_prop = ["state"]

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Package-Sensor", "Main-Board-Ambient-Sensor",
                                    "CPU-Core-Sensor-0", "CPU-Core-Sensor-1",
                                    "CPU-Core-Sensor-2", "CPU-Core-Sensor-3",
                                    "PSU1-Temp-Sensor", "PSU2-Temp-Sensor",
                                    "CPU-Core-Sensor-0", "CPU-Core-Sensor-1", "Port-Ambient-Sensor"]

    def _init_sensors_dict(self):
        self.sensors_dict = {"VOLTAGE": self.voltage_sensors,
                             "TEMPERATURE": self.temperature_sensors}

    def _init_system_lists(self):
        self.user_fields = ['root', 'cumulus']

    def _init_security_lists(self):
        super()._init_security_lists()
        self.kex_algorithms = ['curve25519-sha256',
                               'curve25519-sha256@libssh.org',
                               'diffie-hellman-group16-sha512',
                               'diffie-hellman-group18-sha512',
                               'diffie-hellman-group14-sha256'
                               ]
        # Use object-based cleanup instead of raw CLI commands
        self.aaa_cleanup_function = self._cleanup_aaa_configuration

    def _should_use_new_format(self, dut_engine=None):
        """
        Determine if we should use the new authentication order format
        based on Cumulus Linux version (>= 5.15.0).

        Args:
            dut_engine: Optional SSH engine to use for version retrieval.

        Returns:
            bool: True if version >= 5.15.0, False otherwise
        """
        logger.info('EthDevice: Checking if should use new authentication order format...')

        try:
            current_version = self._get_system_version(dut_engine)
            if not current_version:
                logger.info('EthDevice: No version found, using LEGACY format')
                return False

            with allure.step(f'Deciding authentication order format for version {current_version}'):
                # Use Version utility to check if version >= AUTH_ORDER_CHANGE_VERSION
                if current_version and Version(current_version) >= Version(AUTH_ORDER_CHANGE_VERSION):
                    logger.info(f'EthDevice: ✓ Version {current_version} >= {AUTH_ORDER_CHANGE_VERSION} - Using NEW format')
                    return True
                else:
                    logger.info(f'EthDevice: Version {current_version} < {AUTH_ORDER_CHANGE_VERSION} - Using LEGACY format')
                    return False

        except Exception as e:
            logger.exception(f'EthDevice: Exception during version check - using LEGACY format: {e}')
            return False

    def _get_system_version(self, dut_engine=None):
        """
        Extract version from system using nv show system version command.

        Returns:
            str: The product-release version string (e.g., '5.16.0') or None if not found
        """
        with allure.step('Extracting version from system version info'):
            engine = dut_engine or (TestToolkit.engines.dut if hasattr(TestToolkit, 'engines') and TestToolkit.engines else None)

            logger.info('EthDevice: Retrieving system version via nv show system version...')
            system = System(force_api=ApiType.NVUE)
            version_info = OutputParsingTool.parse_json_str_to_dictionary(
                system.version.show(dut_engine=engine)
            ).get_returned_value()

            current_version = version_info.get(SystemConsts.VERSION_PRODUCT_RELEASE, "")
            logger.info(f'EthDevice: Current product-release version: {current_version}')

            if not current_version:
                logger.info('EthDevice: No product-release version found')
                return None

            return current_version

    def _cleanup_aaa_configuration(self):
        """
        Version-aware AAA cleanup using proper object-based configuration
        """
        with allure.step('Cleanup AAA configuration'):
            try:
                logger.info('Cleaning up AAA configuration using version-aware method')
                system = System(force_api=ApiType.NVUE)

                # Use the new version-aware unset method
                system.aaa.authentication.unset_authentication_order(apply=True, ask_for_confirmation='-y').verify_result(should_succeed=True)

            except Exception as e:
                logger.warning(f'Error during object-based AAA cleanup, falling back to CLI: {e}')
                # Fallback to raw CLI commands if needed
                engine = TestToolkit.engines.dut
                engine.run_cmd('nv unset system aaa authentication', validate=False)
                engine.run_cmd('nv config apply -y', validate=False)

    def _init_password_hardening_lists(self):
        self.aaa_admin_role = 'nvue-admin'
        self.aaa_monitor_role = 'nvue-monitor'
        self.local_test_users = [{AaaConsts.USERNAME: AaaConsts.LOCALADMIN,
                                  AaaConsts.PASSWORD: AaaConsts.STRONG_PASSWORD,
                                  AaaConsts.ROLE: self.aaa_admin_role},
                                 {AaaConsts.USERNAME: AaaConsts.LOCALMONITOR,
                                  AaaConsts.PASSWORD: AaaConsts.STRONG_PASSWORD,
                                  AaaConsts.ROLE: self.aaa_monitor_role}]

    def _init_available_databases(self):
        super()._init_available_databases()

    def _init_services(self):
        super()._init_services()

    def _init_dependent_services(self):
        super()._init_dependent_services()

    def _init_dockers(self):
        super()._init_dockers()

    def setup_base_aaa_config(self, dut_engine: LinuxSshEngine):
        with allure.step('Setup base AAA config'):
            dut_engine.run_cmd('nv set system config apply ignore "/etc/hosts"')
            dut_engine.run_cmd("nv set system aaa role admin class nvapply")
            dut_engine.run_cmd("nv set system aaa role admin class sudo")
            dut_engine.run_cmd("nv set system aaa role monitor class nvshow")
            dut_engine.run_cmd("nv config apply --assume-yes")

    def cleanup_base_aaa_config(self, dut_engine: LinuxSshEngine):
        with allure.step('Cleanup base AAA config'):
            dut_engine.run_cmd('nv unset system config apply ignore "/etc/hosts"')
            dut_engine.run_cmd("nv unset system aaa role admin")
            dut_engine.run_cmd("nv unset system aaa role monitor")
            dut_engine.run_cmd("nv config apply --assume-yes")

    def bypass_password_on_sudo_commands(self, dut_engine: LinuxSshEngine):
        with allure.step('Bypass password on sudo commands'):
            dut_engine.run_cmd(f"echo '{dut_engine.password}' | sudo -S echo")

    def normalize_platform_asic_temperature_output(self, temperature_dict):
        return {k: str(v) for k, v in temperature_dict.items()}

# -------------------------- Mlx3700 Anaconda Switch ----------------------------


class Mlx3700Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 8
        self.asic_type = 'Spectrum-2'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM2)
        self.show_platform_output.update({
            "product-name": "MSN3700",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["PMIC-1-PSU-12V-RAIL-IN1", "PMIC-1-PSU-12V-RAIL-IN2", "PMIC-2-PSU-12V-RAIL-IN1", "PMIC-2-PSU-12V-RAIL-IN2",
                                "PMIC-3-COMEX-1.8V-RAIL-OUT", "PMIC-3-PSU-12V-RAIL-IN1", "PMIC-3-PSU-12V-RAIL-IN2", "PMIC-4-COMEX-1.2V-RAIL-OUT",
                                "PMIC-4-PSU-12V-RAIL-IN1", "PMIC-4-PSU-12V-RAIL-IN2", "PSU-1-12V-RAIL-OUT", "PSU-1-220V-RAIL-IN", "PSU-2-12V-RAIL-OUT", "PSU-2-220V-RAIL-IN"]

    def _init_temperature(self):
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Package-Sensor", "Main-Board-Ambient-Sensor",
                                    "CPU-Core-Sensor-0", "CPU-Core-Sensor-1",
                                    "CPU-Core-Sensor-2", "CPU-Core-Sensor-3",
                                    "PSU1-Temp-Sensor", "PSU2-Temp-Sensor",
                                    "Port-Ambient-Sensor"]

# -------------------------- Mlx2410 Switch -----------------------------


class Mlx2410Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 2
        self.ib_ports_num = 56
        self.asic_type = 'Spectrum'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM1)

        self.show_platform_output.update({
            "product-name": "MSN2410",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["VIN", "VOUT1", "VOUT2"]

    def _init_platform_lists(self):
        self.fan_prop_auto = {"Fan State": "state", "Current Speed (RPM)": "current-speed",
                              "Fan Direction": "direction"}
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=1500, range_max=10000),
            "max-speed": ExpectedString(range_min=18000, range_max=40000)}
        self.platform_environment_absent_fan_values = {
            "state": FansConsts.STATE_ABSENT, "direction": "N/A", "current-speed": "N/A",
            "min-speed": "N/A", "max-speed": "N/A"}
        self.platform_inventory_items = self.fan_list + self.psu_list + self.psu_fan_list \
            + [PlatformConsts.HW_COMP_SWITCH]

    def _init_temperature(self):
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Core-Sensor-0",
                                    "CPU-Core-Sensor-1",
                                    "CPU-Package-Sensor", "Main-Board-Ambient-Sensor",
                                    "PSU1-Temp-Sensor", "PSU2-Temp-Sensor",
                                    "Port-Ambient-Sensor"]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2"]

    def _init_led_list(self):
        self.led_list = ["FAN1", "FAN2", "FAN3", "FAN4", "PSU", "SYSTEM"]


# -------------------------- Mlx4600 Switch -----------------------------


class Mlx4600Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 8
        self.ib_ports_num = 64
        self.asic_type = 'Spectrum-3'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM3)

        self.show_platform_output.update({
            "product-name": "MSN4600",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["PMIC-1-PSU-12V-RAIL-IN1", "PMIC-2-PSU-12V-RAIL-IN1",
                                "PMIC-2-PSU-12V-RAIL-IN2", "PMIC-3-PSU-12V-RAIL-IN1",
                                "PMIC-3-PSU-12V-RAIL-IN2", "PMIC-4-PSU-12V-RAIL-IN1",
                                "PMIC-4-PSU-12V-RAIL-IN2", "PMIC-5-PSU-12V-RAIL-IN1",
                                "PMIC-5-PSU-12V-RAIL-IN2", "PMIC-6-PSU-12V-RAIL-IN1",
                                "PMIC-6-PSU-12V-RAIL-IN2", "PMIC-7-PSU-12V-RAIL-IN1",
                                "PMIC-7-PSU-12V-RAIL-IN2", "PMIC-8-COMEX-1.8V-RAIL-OUT",
                                "PMIC-8-PSU-12V-RAIL-IN1", "PMIC-8-PSU-12V-RAIL-IN2",
                                "PMIC-9-COMEX-1.2V-RAIL-OUT", "PMIC-9-PSU-12V-RAIL-IN1",
                                "PMIC-9-PSU-12V-RAIL-IN2", "PSU-2R-12V-RAIL-OUT",
                                "PSU-2R-220V-RAIL-IN"]

    def _init_platform_lists(self):
        self.fan_prop_auto = {"Fan State": "state", "Current Speed (RPM)": "current-speed",
                              "Fan Direction": "direction"}
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=2000, range_max=10000),
            "max-speed": ExpectedString(range_min=10000, range_max=40000)}
        self.platform_environment_absent_fan_values = {
            "state": FansConsts.STATE_ABSENT, "direction": "N/A", "current-speed": "N/A",
            "min-speed": "N/A", "max-speed": "N/A"}
        self.platform_inventory_items = self.fan_list + self.psu_list + self.psu_fan_list \
            + [PlatformConsts.HW_COMP_SWITCH]

    def _init_temperature(self):
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Core-Sensor-0", "CPU-Core-Sensor-1",
                                    "CPU-Core-Sensor-2", "CPU-Core-Sensor-3",
                                    "CPU-Package-Sensor", "Main-Board-Ambient-Sensor",
                                    "PSU1-Temp-Sensor", "PSU2-Temp-Sensor",
                                    "Port-Ambient-Sensor"]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN2/1", "FAN3/1"]

    def _init_led_list(self):
        self.led_list = ["FAN1", "FAN2", "FAN3", "PSU", "SYSTEM"]


# -------------------------- Mlx4600C Switch -----------------------------


class Mlx4600cSwitch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 8
        self.ib_ports_num = 64
        self.asic_type = 'Spectrum-3'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM3)

        self.show_platform_output.update({
            "product-name": "MSN4600C",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["PMIC-1-PSU-12V-RAIL-IN1", "PMIC-2-PSU-12V-RAIL-IN1",
                                "PMIC-2-PSU-12V-RAIL-IN2", "PMIC-3-PSU-12V-RAIL-IN1",
                                "PMIC-3-PSU-12V-RAIL-IN2", "PMIC-4-PSU-12V-RAIL-IN1",
                                "PMIC-4-PSU-12V-RAIL-IN2", "PMIC-5-PSU-12V-RAIL-IN1",
                                "PMIC-5-PSU-12V-RAIL-IN2", "PMIC-6-PSU-12V-RAIL-IN1",
                                "PMIC-6-PSU-12V-RAIL-IN2", "PMIC-7-PSU-12V-RAIL-IN1",
                                "PMIC-7-PSU-12V-RAIL-IN2", "PMIC-8-COMEX-1.8V-RAIL-OUT1",
                                "PMIC-8-PSU-12V-RAIL-IN1", "PMIC-8-PSU-12V-RAIL-IN2",
                                "PMIC-9-COMEX-1.2V-RAIL-OUT", "PMIC-9-PSU-12V-RAIL-IN1",
                                "PMIC-9-PSU-12V-RAIL-IN2", "PSU-1R-12V-RAIL-OUT",
                                "PSU-1R-220V-RAIL-IN"]

    def _init_platform_lists(self):
        self.fan_prop_auto = {"Fan State": "state", "Current Speed (RPM)": "current-speed",
                              "Fan Direction": "direction"}
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=2000, range_max=10000),
            "max-speed": ExpectedString(range_min=10000, range_max=40000)}
        self.platform_environment_absent_fan_values = {
            "state": FansConsts.STATE_ABSENT, "direction": "N/A", "current-speed": "N/A",
            "min-speed": "N/A", "max-speed": "N/A"}
        self.platform_inventory_items = self.fan_list + self.psu_list + self.psu_fan_list \
            + [PlatformConsts.HW_COMP_SWITCH]

    def _init_temperature(self):
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Core-Sensor-0", "CPU-Core-Sensor-1",
                                    "CPU-Core-Sensor-2", "CPU-Core-Sensor-3",
                                    "CPU-Package-Sensor", "Main-Board-Ambient-Sensor",
                                    "PSU1-Temp-Sensor", "PSU2-Temp-Sensor",
                                    "Port-Ambient-Sensor"]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN2/1", "FAN3/1"]

    def _init_led_list(self):
        self.led_list = ["FAN1", "FAN2", "FAN3", "PSU", "SYSTEM"]


# -------------------------- Mlx4700 Switch -----------------------------


class Mlx4700Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 8
        self.asic_type = 'Spectrum-3'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM3)

        self.show_platform_output.update({
            "product-name": "MSN4700",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["PMIC-1-PSU-12V-RAIL-IN1", "PMIC-2-PSU-12V-RAIL-IN1", "PMIC-2-PSU-12V-RAIL-IN2",
                                "PMIC-3-PSU-12V-RAIL-IN1", "PMIC-3-PSU-12V-RAIL-IN2", "PMIC-4-PSU-12V-RAIL-IN1",
                                "PMIC-4-PSU-12V-RAIL-IN2", "PMIC-5-PSU-12V-RAIL-IN1", "PMIC-5-PSU-12V-RAIL-IN2",
                                "PMIC-6-PSU-12V-RAIL-IN1", "PMIC-6-PSU-12V-RAIL-IN2", "PMIC-7-PSU-12V-RAIL-IN1",
                                "PMIC-7-PSU-12V-RAIL-IN2", "PMIC-8-COMEX-1.8V-RAIL-OUT", "PMIC-8-PSU-12V-RAIL-IN1",
                                "PMIC-8-PSU-12V-RAIL-IN2", "PMIC-9-COMEX-1.2V-RAIL-OUT", "PMIC-9-PSU-12V-RAIL-IN1",
                                "PMIC-9-PSU-12V-RAIL-IN2", "PSU-2R-12V-RAIL-OUT", "PSU-2R-220V-RAIL-IN"]

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Core-Sensor-0", "CPU-Core-Sensor-1",
                                    "CPU-Core-Sensor-2", "CPU-Core-Sensor-3", "CPU-Package-Sensor",
                                    "Main-Board-Ambient-Sensor", "PSU1-Temp-Sensor", "PSU2-Temp-Sensor",
                                    "Port-Ambient-Sensor"]


# -------------------------- Mlx5600 Switch -----------------------------


class Mlx5600Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 12
        self.ib_ports_num = 65
        self.asic_type = 'Spectrum-4'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM4)
        self.show_platform_output.update({
            "product-name": "SN5600",
            "asic-model": self.asic_type
        })
        self.voltage_sensors = ["ADAPTER", "IBC-1-13V5-RAIL-OUT", "IBC-1-PWR-CONV-54V-RAIL-IN1",
                                "IBC-2-13V5-RAIL-OUT", "IBC-2-PWR-CONV-54V-RAIL-IN1", "IBC-3-13V5-RAIL-OUT",
                                "IBC-3-PWR-CONV-54V-RAIL-IN1", "IBC-4-13V5-RAIL-OUT", "IBC-4-PWR-CONV-54V-RAIL-IN1",
                                "PMIC-1-PSU-13V5-RAIL-IN1", "PMIC-2-PSU-13V5-RAIL-IN1", "PMIC-3-PSU-13V5-RAIL-IN1",
                                "PMIC-4-PSU-13V5-RAIL-IN1", "PMIC-5-PSU-13V5-RAIL-IN1", "PMIC-6-PSU-13V5-RAIL-IN1",
                                "PMIC-7-PSU-13V5-RAIL-IN1", "PMIC-8-PSU-13V5-RAIL-IN1", "PMIC-9-PSU-13V5-RAIL-IN1",
                                "PMIC-10-HVDD_T03-1V2-RAIL-OUT1", "PMIC-10-HVDD_T47-1V2-RAIL-OUT2", "PMIC-10-PSU-13V5-RAIL-IN1",
                                "PMIC-11-PSU-13V5-RAIL-IN1", "PMIC-11-VDDSCC-0V75-RAIL-OUT1", "PMIC-12-COMEX-VCCSA-OUT2",
                                "PMIC-12-COMEX-VCORE-OUT1", "PMIC-12-PSU-13V5-RAIL-VIN", "PSU-1L-54V-RAIL-OUT",
                                "PSU-1L-220V-RAIL-IN"]

    def _init_platform_lists(self):
        self.fan_prop_auto = {"Fan State": "state", "Current Speed (RPM)": "current-speed",
                              "Fan Direction": "direction"}
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=2800, range_max=10000),
            "max-speed": ExpectedString(range_min=10000, range_max=40000)}
        self.platform_environment_absent_fan_values = {
            "state": FansConsts.STATE_ABSENT, "direction": "N/A", "current-speed": "N/A",
            "min-speed": "N/A", "max-speed": "N/A"}
        self.platform_inventory_items = self.fan_list + self.psu_list + self.psu_fan_list \
            + [PlatformConsts.HW_COMP_SWITCH]

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Core-Sensor-0", "CPU-Core-Sensor-1",
                                    "CPU-Core-Sensor-2", "CPU-Core-Sensor-3", "CPU-Core-Sensor-4",
                                    "CPU-Core-Sensor-5", "CPU-Package-Sensor", "Main-Board-Ambient-Sensor",
                                    "PSU1-Temp-Sensor", "PSU2-Temp-Sensor", "Port-Ambient-Sensor"]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2"]

    def _init_led_list(self):
        self.led_list = ["FAN1", "FAN2", "FAN3", "FAN4", "PSU", "SYSTEM"]


# -------------------------- Mlx5400 Switch -----------------------------


class Mlx5400Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 12
        self.ib_ports_num = 66
        self.asic_type = 'Spectrum-4'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM4)
        self.show_platform_output.update({
            "product-name": "SN5400",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["ADAPTER", "IBC-1-13V5-RAIL-OUT", "IBC-1-PWR-CONV-54V-RAIL-IN1",
                                "IBC-2-13V5-RAIL-OUT", "IBC-2-PWR-CONV-54V-RAIL-IN1", "IBC-3-13V5-RAIL-OUT",
                                "IBC-3-PWR-CONV-54V-RAIL-IN1", "IBC-4-13V5-RAIL-OUT", "IBC-4-PWR-CONV-54V-RAIL-IN1",
                                "PMIC-1-PSU-13V5-RAIL-IN1", "PMIC-2-PSU-13V5-RAIL-IN1", "PMIC-3-PSU-13V5-RAIL-IN1",
                                "PMIC-4-PSU-13V5-RAIL-IN1", "PMIC-5-PSU-13V5-RAIL-IN1", "PMIC-6-PSU-13V5-RAIL-IN1",
                                "PMIC-7-PSU-13V5-RAIL-IN1", "PMIC-8-PSU-13V5-RAIL-IN1", "PMIC-9-PSU-13V5-RAIL-IN1",
                                "PMIC-10-HVDD_T03-1V2-RAIL-OUT1", "PMIC-10-HVDD_T47-1V2-RAIL-OUT2", "PMIC-10-PSU-13V5-RAIL-IN1",
                                "PMIC-11-PSU-13V5-RAIL-IN1", "PMIC-11-VDDSCC-0V75-RAIL-OUT1", "PMIC-12-COMEX-VCCSA-OUT2",
                                "PMIC-12-COMEX-VCORE-OUT1", "PMIC-12-PSU-13V5-RAIL-VIN", "PSU-1L-54V-RAIL-OUT",
                                "PSU-1L-220V-RAIL-IN"]

    def _init_platform_lists(self):
        self.fan_prop_auto = {"Fan State": "state", "Current Speed (RPM)": "current-speed",
                              "Fan Direction": "direction"}
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=2800, range_max=10000),
            "max-speed": ExpectedString(range_min=10000, range_max=40000)}
        self.platform_environment_absent_fan_values = {
            "state": FansConsts.STATE_ABSENT, "direction": "N/A", "current-speed": "N/A",
            "min-speed": "N/A", "max-speed": "N/A"}
        self.platform_inventory_items = self.fan_list + self.psu_list + self.psu_fan_list \
            + [PlatformConsts.HW_COMP_SWITCH]

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Core-Sensor-0", "CPU-Core-Sensor-1",
                                    "CPU-Core-Sensor-2", "CPU-Core-Sensor-3", "CPU-Core-Sensor-4",
                                    "CPU-Core-Sensor-5", "CPU-Package-Sensor", "Main-Board-Ambient-Sensor",
                                    "PSU1-Temp-Sensor", "PSU2-Temp-Sensor", "Port-Ambient-Sensor"]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2"]

    def _init_led_list(self):
        self.led_list = ["FAN1", "FAN2", "FAN3", "FAN4", "PSU", "SYSTEM"]

# -------------------------- Mlx5640 Switch -----------------------------


class Mlx5640Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 16
        self.ib_ports_num = 66
        self.asic_type = 'Spectrum-5'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM5)
        self.show_platform_output.update({
            "product-name": "SN5640",
            "asic-model": self.asic_type
        })
        self.voltage_sensors = ["PMIC-1-PSU-13V5-RAIL-IN1", "PMIC-2-PSU-13V5-RAIL-IN1", "PMIC-3-PSU-13V5-RAIL-IN1",
                                "PMIC-4-PSU-13V5-RAIL-IN1", "PMIC-5-PSU-13V5-RAIL-IN1", "PMIC-6-PSU-13V5-RAIL-IN1",
                                "PMIC-7-PSU-13V5-RAIL-IN1", "PMIC-8-PSU-13V5-RAIL-IN1", "PMIC-9-PSU-13V5-RAIL-IN1",
                                "PMIC-10-HVDD_T03-1V2-RAIL-OUT1", "PMIC-10-HVDD_T47-1V2-RAIL-OUT2", "PMIC-10-PSU-13V5-RAIL-IN1",
                                "PMIC-11-PSU-13V5-RAIL-IN1", "PMIC-11-VDDSCC-0V75-RAIL-OUT1", "PMIC-12-COMEX-IN-VDDCR-INPUT-VOLT ",
                                "PMIC-12-COMEX-OUT2-VDDCR_SOC-VOLT", "PMIC-12-COMEX-OUT-VDDCR_CPU-VOLT", "PMIC-13-COMEX-VDD_MEM-INPUT-VOLT",
                                "PMIC-13-COMEX-VDD_MEM-OUTPUT-VOLT", "PSU-1L-12V-RAIL-OUT", "PSU-1L-220V-RAIL-IN", "PSU-2L-12V-RAIL-OUT", "PSU-2L-220V-RAIL-IN",
                                "PSU-3R-12V-RAIL-OUT", "PSU-3R-220V-RAIL-IN", "PSU-4R-12V-RAIL-OUT", "PSU-4R-220V-RAIL-IN"]

    def _init_platform_lists(self):
        self.fan_prop_auto = {"Fan State": "state", "Current Speed (RPM)": "current-speed",
                              "Fan Direction": "direction"}
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=2800, range_max=10000),
            "max-speed": ExpectedString(range_min=10000, range_max=40000)}
        self.platform_environment_absent_fan_values = {
            "state": FansConsts.STATE_ABSENT, "direction": "N/A", "current-speed": "N/A",
            "min-speed": "N/A", "max-speed": "N/A"}
        self.platform_inventory_items = self.fan_list + self.psu_list + self.psu_fan_list \
            + [PlatformConsts.HW_COMP_SWITCH]

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Package-Sensor", "Main-Board-Ambient-Sensor",
                                    "PSU1-Temp-Sensor", "PSU2-Temp-Sensor", "PSU3-Temp-Sensor", "PSU4-Temp-Sensor",
                                    "Port-Ambient-Sensor"]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2", "Fan5/1", "Fan5/2"]

    def _init_led_list(self):
        self.led_list = ["FAN1", "FAN2", "FAN3", "FAN4", "FAN5", "PSU", "SYSTEM"]


# -------------------------- Mlx6600 Switch -----------------------------


class Mlx6600Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 16
        self.ib_ports_num = 66
        self.asic_type = 'Spectrum-6'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM6)
        self.show_platform_output.update({
            "product-name": "SN6600",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = [
            "CPU-PMIC-VDDCR-VOLT-IN",
            "CPU-PMIC-VDDCR_CPU-VOLT-OUT1",
            "CPU-PMIC-VDDCR_SOC-VOLT-OUT2",
            "DDR-PMIC-VDD_MEM-VOLT-IN",
            "DDR-PMIC-VDD_MEM-VOLT-OUT2",
            "DDR-PMIC-VDD_MEMIO-VOLT-OUT1",
            "PDB-1-CONV-VINDC-VOLT-IN",
            "PDB-1-CONV-VOUT-VOLT-OUT",
            "PDB-1-HSC-VINDC-VOLT-IN",
            "PDB-1-HSC-VOUT-VOLT-OUT",
            "PDB-2-CONV-VINDC-VOLT-IN",
            "PDB-2-CONV-VOUT-VOLT-OUT",
            "PDB-2-HSC-VINDC-VOLT-IN",
            "PDB-2-HSC-VOUT-VOLT-OUT",
            "PMIC-1-PVIN1_VDD_ASIC-VOLT-IN",
            "PMIC-2-PVIN1_VDD_TILE0_TILE1_ASIC-VOLT-IN",
            "PMIC-3-PVIN1_VDD_TILE2_TILE3_ASIC-VOLT-IN",
            "PMIC-4-PVIN1_DVDD_TILE0_TILE1_ASIC-VOLT-IN",
            "PMIC-5-PVIN1_DVDD_TILE2_TILE3_ASIC-VOLT-IN",
            "PMIC-6-PVIN1_AVDD_TILE0_TILE1_ASIC-VOLT-IN",
            "PMIC-7-PVIN1_AVDD_TILE0_TILE1_ASIC-VOLT-IN",
            "PMIC-8-PVIN1_HBID_TILE12_37_ASIC-VOLT-IN",
            "PMIC-9-PVIN1_OSFPX16_T-VOLT-IN",
            "PMIC-10-PVIN1_OSFPX16_B-VOLT-IN",
            "PMIC-11-PVIN1_VDD_TILE4_TILE5_ASIC-VOLT-IN",
            "PMIC-12-PVIN1_VDD_TILE6_TILE7_ASIC-VOLT-IN",
            "PMIC-13-PVIN1_DVDD_TILE4_TILE5_ASIC-VOLT-IN",
            "PMIC-14-PVIN1_DVDD_TILE6_TILE7_ASIC-VOLT-IN",
            "PMIC-15-PVIN1_AVDD_TILE4_TILE5_ASIC-VOLT-IN",
            "PMIC-16-PVIN1_AVDD_TILE6_TILE7_ASIC-VOLT-IN",
            "PMIC-17-PVIN1_HBID_TILE04_56_ASIC-VOLT-IN",
            "PMIC-18-PVIN1_OSFPX16_M1-VOLT-IN",
            "PMIC-19-PVIN1_OSFPX16_M2-VOLT-IN",
            "PMIC-20-PVIN1_HVDD-VOLT-IN",
        ]

        self.leakage_sensors_count = 2
        self.list_of_leakages = [f"leakage{i}" for i in range(1, self.leakage_sensors_count + 1)]

    def _init_temperature(self):
        super()._init_temperature()
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Package-Sensor"]

    def _init_psu_list(self):
        self.psu_list = ["PDB1", "PDB2"]
        self.psu_fan_list = []
        self.platform_env_psu_prop = ["state"]

    def _init_fan_list(self):
        self.fan_list = []

    def _init_led_list(self):
        self.led_list = ["POWER", "SYSTEM"]

    def _init_sensors_dict(self):
        super()._init_sensors_dict()
        self.sensors_dict["LEAKAGE"] = self.list_of_leakages


# -------------------------- Mlx410 Switch -----------------------------


class Mlx4410Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 8
        self.ib_ports_num = 32
        self.asic_type = 'Spectrum-3'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM3)

        self.show_platform_output.update({
            "product-name": "MSN4410",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["PMIC-1-PSU-12V-RAIL-IN", "PMIC-2-PSU-12V-RAIL-IN", "PMIC-3-COMEX-1.05V-RAIL-OUT",
                                "PMIC-3-COMEX-1.8V-RAIL-OUT", "PMIC-3-PSU-12V-RAIL-IN", "PMIC-3-PSU-12V-RAIL-IN1",
                                "PMIC-5-PSU-12V-RAIL-IN", "PMIC-6-COMEX-1.8V-RAIL-OUT1", "PMIC-6-PSU-12V-RAIL-IN1",
                                "PMIC-6-PSU-12V-RAIL-IN2", "PMIC-7-COMEX-1.2V-RAIL-OUT", "PMIC-7-PSU-12V-RAIL-IN1",
                                "PMIC-7-PSU-12V-RAIL-IN2", "PSU-2R-12V-RAIL-OUT", "PSU-2R-220V-RAIL-IN"]

    def _init_temperature(self):
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Core-Sensor-0", "CPU-Core-Sensor-1", "CPU-Core-Sensor-2",
                                    "CPU-Core-Sensor-3", "CPU-Package-Sensor", "Main-Board-Ambient-Sensor",
                                    "PSU1-Temp-Sensor", "PSU2-Temp-Sensor", "Port-Ambient-Sensor"]

# -------------------------- Mlx3750sx Switch -----------------------------


class Mlx3750sxSwitch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 8
        self.ib_ports_num = 32
        self.asic_type = 'Spectrum-2'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM2)

        self.show_platform_output.update({
            "product-name": "MSN3750sx",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["ADAPTER", "PMIC-1-ASIC-1.2V-RAIL-OUT",
                                "PMIC-1-PSU-12V-RAIL-IN", "PMIC-2-ASIC-1.8V-RAIL-OUT",
                                "PMIC-2-ASIC-3.3V-RAIL-OUT", "PMIC-2-PSU-12V-RAIL-IN",
                                "PMIC-6-PSU-12V-RAIL-VIN", "PSU-2-12V-RAIL-OUT",
                                "PSU-2-220V-RAIL-IN"]

    def _init_temperature(self):
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Package-Sensor", "Main-Board-Ambient-Sensor",
                                    "CPU-Core-Sensor-0", "CPU-Core-Sensor-1",
                                    "PSU1-Temp-Sensor", "PSU2-Temp-Sensor",
                                    "Port-Ambient-Sensor"]


# -------------------------- Mlx3700cs Switch -----------------------------


class Mlx3700csSwitch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 8
        self.ib_ports_num = 32
        self.asic_type = 'Spectrum-2'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM2)

        self.show_platform_output.update({
            "product-name": "MSN3700cs",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["ADAPTER", "PMIC-1-ASIC-0.8V-VCORE-RAIL-OUT",
                                "PMIC-1-PSU-12V-RAIL-IN1", "PMIC-2-ASIC-3.3V-RAIL-OUT",
                                "PMIC-2-PSU-12V-RAIL-IN1", "PMIC-2-PSU-12V-RAIL-IN2",
                                "PSU-2-12V-RAIL-OUT", "PSU-2-220V-RAIL-IN", "VIN"]

    def _init_temperature(self):
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Core-Sensor-0",
                                    "CPU-Core-Sensor-1", "CPU-Package-Sensor",
                                    "Main-Board-Ambient-Sensor", "PSU1-Temp-Sensor",
                                    "PSU2-Temp-Sensor", "Port-Ambient-Sensor"]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2"]

    def _init_led_list(self):
        self.led_list = ["FAN1", "FAN2", "FAN3", "FAN4", "PSU", "SYSTEM"]


# -------------------------- Mlx3700c Switch -----------------------------


class Mlx3700cSwitch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 8
        self.ib_ports_num = 32
        self.asic_type = 'Spectrum-2'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM2)

        self.show_platform_output.update({
            "product-name": "MSN3700c",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["PMIC-1-PSU-12V-RAIL-IN1", "PMIC-1-PSU-12V-RAIL-IN2", "PMIC-2-PSU-12V-RAIL-IN1",
                                "PMIC-2-PSU-12V-RAIL-IN2", "PMIC-3-COMEX-1.8V-RAIL-OUT", "PMIC-3-PSU-12V-RAIL-IN1",
                                "PMIC-3-PSU-12V-RAIL-IN2", "PMIC-4-COMEX-1.2V-RAIL-OUT", "PMIC-4-PSU-12V-RAIL-IN1",
                                "PMIC-4-PSU-12V-RAIL-IN2", "PSU-2-12V-RAIL-OUT", "PSU-2-220V-RAIL-IN"]

    def _init_temperature(self):
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Core-Sensor-0", "CPU-Core-Sensor-1",
                                    "CPU-Package-Sensor", "Main-Board-Ambient-Sensor", "PSU1-Temp-Sensor",
                                    "PSU2-Temp-Sensor", "Port-Ambient-Sensor"]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2"]

    def _init_led_list(self):
        self.led_list = ["FAN1", "FAN2", "FAN3", "FAN4", "PSU", "SYSTEM"]


# -------------------------- Mlx3420 Switch -----------------------------


class Mlx3420Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 4
        self.ib_ports_num = 60
        self.asic_type = 'Spectrum-2'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM2)

        self.show_platform_output.update({
            "product-name": "MSN3420",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["PMIC-3-COMEX-1.8V-RAIL-OUT", "PMIC-3-PSU-12V-RAIL-IN1",
                                "PMIC-3-PSU-12V-RAIL-IN2", "PMIC-4-COMEX-1.8V-RAIL-OUT",
                                "PMIC-4-PSU-12V-RAIL-IN1", "PMIC-4-PSU-12V-RAIL-IN2",
                                "PSU-2-12V-RAIL-OUT", "PSU-2-220V-RAIL-IN",
                                "VIN1", "VIN2", "VOUT1", "VOUT2"]

    def _init_temperature(self):
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Core-Sensor-0",
                                    "CPU-Core-Sensor-1", "CPU-Package-Sensor",
                                    "Main-Board-Ambient-Sensor", "PSU1-Temp-Sensor",
                                    "PSU2-Temp-Sensor", "Port-Ambient-Sensor"]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2", "FAN5/1", "FAN5/2"]

    def _init_led_list(self):
        self.led_list = ["FAN1", "FAN2", "FAN3", "FAN4", "FAN5", "PSU", "SYSTEM"]


# -------------------------- Mlx2700 Switch -----------------------------


class Mlx2700Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 2
        self.ib_ports_num = 32
        self.asic_type = 'Spectrum'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM1)

        self.show_platform_output.update({
            "product-name": "MSN2700",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["VIN", "VOUT1", "VOUT2"]

    def _init_platform_lists(self):
        self.fan_prop_auto = {"Fan State": "state", "Current Speed (RPM)": "current-speed",
                              "Fan Direction": "direction"}
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=1500, range_max=2000),
            "max-speed": ExpectedString(range_min=18000, range_max=25000)}
        self.platform_environment_absent_fan_values = {
            "state": FansConsts.STATE_ABSENT, "direction": "N/A", "current-speed": "N/A",
            "min-speed": "N/A", "max-speed": "N/A"}
        self.platform_inventory_items = self.fan_list + self.psu_list + self.psu_fan_list \
            + [PlatformConsts.HW_COMP_SWITCH]

    def _init_temperature(self):
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Core-Sensor-0",
                                    "CPU-Core-Sensor-1", "CPU-Package-Sensor",
                                    "Main-Board-Ambient-Sensor", "PSU1-Temp-Sensor",
                                    "PSU2-Temp-Sensor", "Port-Ambient-Sensor"]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN1/2", "FAN2/1", "FAN2/2", "FAN3/1", "FAN3/2", "FAN4/1", "FAN4/2"]

    def _init_led_list(self):
        self.led_list = ["FAN1", "FAN2", "FAN3", "FAN4", "PSU", "SYSTEM"]


# -------------------------- Mlx2201 Switch -----------------------------


class Mlx2201Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 2
        self.ib_ports_num = 32
        self.asic_type = 'Spectrum'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM1)

        self.show_platform_output.update({
            "product-name": "SN2201",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["MONITOR-CPU-BOARD-P0V6_VTT_DIMM", "MONITOR-CPU-BOARD-P2V5_VPP",
                                "MONITOR-CPU-BOARD-V1P05", "MONITOR-CPU-BOARD-V1P8",
                                "MONITOR-CPU-BOARD-V1P24", "MONITOR-CPU-BOARD-V3P3",
                                "MONITOR-CPU-BOARD-VR_VCCRAM_1V15", "MONITOR-CPU-BOARD-VR_VCC_1V15",
                                "MONITOR-CPU-BOARD-VR_VDDQ_1V20", "MONITOR-CPU-BOARD-VR_VNN_1V05",
                                "PSU-2-12V-RAILOUT", "PSU-2-220V-RAILIN", "VR-IC-PSU-12V-RAIL"]

    def _init_platform_lists(self):
        self.fan_prop_auto = {"Fan State": "state", "Current Speed (RPM)": "current-speed",
                              "Fan Direction": "direction"}
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=2200, range_max=2500),
            "max-speed": ExpectedString(range_min=16000, range_max=22000)}
        self.platform_environment_absent_fan_values = {
            "state": FansConsts.STATE_ABSENT, "direction": "N/A", "current-speed": "N/A",
            "min-speed": "N/A", "max-speed": "N/A"}
        self.platform_inventory_items = self.fan_list + self.psu_list + self.psu_fan_list \
            + [PlatformConsts.HW_COMP_SWITCH]

    def _init_temperature(self):
        self.temperature_sensors = ["Asic-Temp-Sensor", "CPU-Core-Sensor-0",
                                    "CPU-Core-Sensor-1", "CPU-Package-Sensor",
                                    "Main-Board-Ambient-Sensor", "PSU1-Temp-Sensor",
                                    "PSU2-Temp-Sensor", "Port-Ambient-Sensor"]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN2/1", "FAN3/1", "FAN4/1"]

    def _init_led_list(self):
        self.led_list = ["FAN1", "FAN2", "FAN3", "FAN4", "PSU", "SYSTEM"]


# -------------------------- Mlx2100 Switch -----------------------------


class Mlx2100Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 4
        self.ib_ports_num = 16
        self.asic_type = 'Spectrum'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM1)

        self.show_platform_output.update({
            "product-name": "MSN2100",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["VIN", "VOUT1", "VOUT2"]

    def _init_platform_lists(self):
        self.fan_prop_auto = {"Fan State": "state", "Current Speed (RPM)": "current-speed",
                              "Fan Direction": "direction"}
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=1500, range_max=2500),
            "max-speed": ExpectedString(range_min=16000, range_max=25000)}
        self.platform_environment_absent_fan_values = {
            "state": FansConsts.STATE_ABSENT, "direction": "N/A", "current-speed": "N/A",
            "min-speed": "N/A", "max-speed": "N/A"}
        self.platform_inventory_items = self.fan_list + self.psu_list + self.psu_fan_list \
            + [PlatformConsts.HW_COMP_SWITCH]

    def _init_temperature(self):
        self.temperature_sensors = ["Asic-Temp-Sensor", "Main-Board-Ambient-Sensor",
                                    "Port-Ambient-Sensor", "core0-Sensor",
                                    "core1-Sensor", "core2-Sensor", "core3-Sensor"]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN2/1", "FAN3/1", "FAN4/1"]

    def _init_led_list(self):
        self.led_list = ["FAN", "PSU1", "PSU2", "SYSTEM"]


# -------------------------- Mlx2010 Switch -----------------------------


class Mlx2010Switch(EthSwitch):
    def __init__(self):
        super().__init__(asic_amount=1)

    def _init_constants(self):
        super()._init_constants()
        self.core_count = 4
        self.ib_ports_num = 22
        self.asic_type = 'Spectrum'
        self.constants.firmware.append(PlatformConsts.FW_SPECTRUM1)

        self.show_platform_output.update({
            "product-name": "MSN2010",
            "asic-model": self.asic_type
        })

        self.voltage_sensors = ["VIN", "VOUT1", "VOUT2"]

    def _init_platform_lists(self):
        self.fan_prop_auto = {"Fan State": "state", "Current Speed (RPM)": "current-speed",
                              "Fan Direction": "direction"}
        self.platform_environment_fan_values = {
            "state": FansConsts.STATE_OK, "direction": None, "current-speed": None,
            "min-speed": ExpectedString(range_min=4500, range_max=5000),
            "max-speed": ExpectedString(range_min=20000, range_max=30000)}
        self.platform_environment_absent_fan_values = {
            "state": FansConsts.STATE_ABSENT, "direction": "N/A", "current-speed": "N/A",
            "min-speed": "N/A", "max-speed": "N/A"}
        self.platform_inventory_items = self.fan_list + self.psu_list \
            + [PlatformConsts.HW_COMP_SWITCH]

    def _init_psu_list(self):
        self.psu_list = ["PSU1", "PSU2"]
        self.psu_fan_list = []
        self.platform_env_psu_prop = ["state"]

    def _init_temperature(self):
        self.temperature_sensors = ["Asic-Temp-Sensor", "Main-Board-Ambient-Sensor",
                                    "Port-Ambient-Sensor", "core0-Sensor",
                                    "core1-Sensor", "core2-Sensor", "core3-Sensor"]

    def _init_fan_list(self):
        self.fan_list = ["FAN1/1", "FAN2/1", "FAN3/1", "FAN4/1"]

    def _init_led_list(self):
        self.led_list = ["FAN", "PSU1", "PSU2", "SYSTEM"]
