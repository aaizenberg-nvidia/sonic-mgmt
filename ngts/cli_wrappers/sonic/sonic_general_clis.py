import json
import logging
import os
import re
import time
import traceback
from typing import Any
import xml.etree.ElementTree as ET

import netmiko
from retry import retry
from retry.api import retry_call

import ngts.helpers.json_file_helper as json_file_helper
from ngts.helpers.system_helpers import set_timezone as system_set_timezone
from ngts.scripts.sonic_deploy.sonic_only_methods import validate_and_get_asic_count
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.general_constants.constants import DefaultTestServerCred, SonicSimxConstants
from devts.infra.tools.general_constants.air_constants import HostsConstants
from devts.infra.tools.nvidia_air_tools.air import get_dhcp_ips_dict
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from devts.infra.tools.utilities.onie_sonic_clis import SonicOnieCli as SonicOnieCliDevts
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.cli_util.cli_constants import SonicConstant
from ngts.cli_util.cli_parsers import generic_sonic_output_parser
from ngts.cli_wrappers.common.general_clis_common import GeneralCliCommon
from ngts.cli_wrappers.linux.linux_general_clis import LinuxGeneralCli
from ngts.cli_wrappers.sonic.sonic_chassis_clis import SonicChassisCli
from ngts.cli_wrappers.sonic.sonic_onie_clis import SonicOnieCli, OnieInstallationError, get_latest_onie_version
from ngts.constants.constants import CliType, FanoutConfigFile
from ngts.constants.constants import SonicConst, InfraConst, ConfigDbJsonConst, PerformanceSetupConstants, \
    AppExtensionInstallationConstants, DefaultCredentialConstants, BluefieldConstants, \
    PlatformTypesConstants, SonicDeployConstants, SanitizerConst
from ngts.constants.performance_constants import PerfConsts
from ngts.helpers.breakout_helpers import get_port_current_breakout_mode, get_all_split_ports_parents, \
    get_split_mode_supported_breakout_modes, get_split_mode_supported_speeds, get_all_unsplit_ports
from ngts.helpers.config_db_utils import save_config_db_json
from ngts.helpers.interface_helpers import get_dut_default_ports_list
from ngts.helpers.run_process_on_host import run_process_on_host


from ngts.helpers.sonic_branch_helper import get_sonic_branch
from ngts.helpers.system_helpers import copy_files_to_syncd
from ngts.helpers.sanitizer_helper import check_sanitizer_and_store_dump
from ngts.scripts.sonic_deploy.sonic_only_methods import SonicInstallationSteps, detect_asic_count
from ngts.scripts.sonic_deploy.os_upgrade_flag import set_os_upgrade_flag

from ngts.tests.nightly.app_extension.app_extension_helper import get_installed_mellanox_extensions
from ngts.tests.nightly.show_techsupport.constants import HealthEventConst
from ngts.tests.nightly.show_techsupport.test_health_event import get_health_event_config
from ngts.tools.infra import update_platform_info_files
from tests.common.plugins.allure_wrapper import allure_step_wrapper as allure

logger = logging.getLogger()
DUMMY_COMMAND = 'echo dummy_command'
NOT_SUPPORTED_DPB_SKU_LIST = ['Mellanox-SN5810_LD-O128A2', 'ACS-SN6810_LD']
PLATFORMS_TO_NOT_UPDATE_PLATFORM_PARAMS = ['sn6600_ld', 'sn4280']
SANITIZER_REBOOT_EXTRA_WAIT_SEC = SanitizerConst.SANITIZER_REBOOT_EXTRA_WAIT_SEC


class SonicGeneralCli:

    def __new__(cls, **kwargs):
        branch = kwargs.get('branch')
        engine = kwargs['engine']
        cli_obj = kwargs.get('cli_obj')
        dut_alias = kwargs.get('dut_alias', 'dut')

        supported_cli_classes = {'default': SonicGeneralCliDefault(engine, cli_obj, dut_alias),
                                 '202012': SonicGeneralCli202012(engine, cli_obj, dut_alias)}

        cli_class = supported_cli_classes.get(branch, supported_cli_classes['default'])
        cli_class_name = cli_class.__class__.__name__
        logger.info(f'Going to use General CLI class: {cli_class_name}')

        return cli_class


class SonicGeneralCliDefault(GeneralCliCommon):
    """
    This class is for general cli commands for sonic only
    """

    def __init__(self, engine, cli_obj, dut_alias):
        self.backup_logs_stored = False
        super().__init__(engine, cli_obj, dut_alias)
        self._is_simx_moose = None
        self._is_bison = None

    def show_setup_versions(self):
        return ''

    def is_dut_supports_image(self, base_version_url, dut_name, cli_type) -> bool:
        """
        This method checks whether the given base version url is supported for the given dut , or not
        :return: True/False
        """
        image_supports = True
        # production devices support only prod versions of ONIE and SONiC
        if (dut_name in SonicDeployConstants.PRODUCTION_DUTS and "prod" not in base_version_url and
                cli_type == CliType.SONIC):
            image_supports = False
        # when executed deploy of production image, skip the flow on not production devices
        if (dut_name not in SonicDeployConstants.PRODUCTION_DUTS and 'prod' in base_version_url and
                cli_type == CliType.SONIC):
            image_supports = False
        logger.info(
            f"dut: {dut_name} {'supports' if image_supports else 'does not support'} version: {base_version_url}")
        return image_supports

    def show_feature_status(self):
        """
        This method show feature status on the sonic switch
        :return: command output
        """
        return self.engine.run_cmd('show feature status')

    def show_and_parse_feature_status(self):
        """
        This method show feature status on the sonic switch
        :return: command output
        """
        output_content = self.show_feature_status()
        return generic_sonic_output_parser(output_content, output_key="Feature")

    def set_feature_state(self, feature_name, state):
        """
        This method to set feature state on the sonic switch
        :param feature_name: the feature name
        :param state: state
        """
        self.engine.run_cmd('sudo config feature state {} {}'.format(feature_name, state), validate=True)

    def restart_determine_reboot_cause(self):
        """
        This method restarts the determine-reboot-cause service, necessary for AIR platform after sku change
        """
        logger.info('Restarting determine-reboot-cause service')
        self.engine.run_cmd('sudo systemctl restart determine-reboot-cause', validate=True)

    def get_installer_delimiter(self):
        dash_installer = 'sonic-installer'
        delimiter = '_'
        output = self.engine.run_cmd('which {}'.format(dash_installer))
        if dash_installer in output:
            delimiter = '-'
        return delimiter

    def install_image(self, image_path, delimiter='-', is_skipping_migrating_package=False, validate=True):
        if not is_skipping_migrating_package:
            output = self.engine.run_cmd('sudo sonic{}installer install {} -y'.
                                         format(delimiter, image_path), validate=validate)
        else:
            output = self.engine.run_cmd('sudo sonic{}installer install {} -y --skip-package-migration'.
                                         format(delimiter, image_path), validate=validate)
        return output

    def get_image_binary_version(self, image_path, delimiter='-'):
        output = self.engine.run_cmd('sudo sonic{}installer binary{}version {}'.
                                     format(delimiter, delimiter, image_path),
                                     validate=True)
        return output

    def get_image_sonic_version(self, only_branch=True, short_version=False):
        output = self.engine.run_cmd('sudo show boot')
        if only_branch:
            pattern = r"Current:\s*SONiC-OS-([\d|\w|\-]*)\..*"
        elif short_version:
            pattern = r"Current:\s*SONiC-OS-([\d|\w|\-]*\..*)"
        else:
            pattern = r"Current:\s*(SONiC-OS-[\d|\w|\-]*\..*)"
        current_image = re.search(pattern, output, re.IGNORECASE).group(1)
        return current_image

    def get_image_sonic_release(self):
        return self.engine.run_cmd('sonic-cfggen -y /etc/sonic/sonic_version.yml -v release')

    def set_default_image(self, image_binary, delimiter='-'):
        output = self.engine.run_cmd('sudo sonic{}installer set{}default {}'.format(delimiter, delimiter, image_binary),
                                     validate=True)
        return output

    def set_next_boot_entry_to_onie(self):
        self.engine.run_cmd('sudo grub-editenv /host/grub/grubenv set next_entry=ONIE', validate=True)

    def get_sonic_image_list(self, delimiter='-'):
        output = self.engine.run_cmd('sudo sonic{}installer list'.format(delimiter), validate=True)
        return output

    def load_configuration(self, config_file):
        self.engine.run_cmd('sudo config load -y {}'.format(config_file), validate=True)

    def is_switch_ready_to_reload(self):
        # TERM=dumb avoids TTY control sequences in stdout over SSH
        out = self.engine.run_cmd(
            'TERM=dumb systemctl show swss.service --property=ActiveEnterTimestampMonotonic --value',
            validate=True)
        swss_up_time = float(out.strip()) / 1000000
        now_out = self.engine.run_cmd("python3 -c 'import time; print(time.monotonic())'", validate=True)
        now_time = float(now_out.strip())
        active_sec = now_time - swss_up_time
        logger.info("SWSS has been active for {:.2f} seconds".format(active_sec))
        if active_sec < 120:
            raise Exception("SWSS has been active for less than 120s; not ready for reload")

    def reload_configuration(self, force=False):
        cmd = 'sudo config reload -y'
        if force:
            cmd += ' -f'
        else:
            retry_call(self.is_switch_ready_to_reload, tries=12, delay=10)
        output = self.engine.run_cmd('{} ; echo "RELOAD_RC=$?"'.format(cmd), validate=False)
        if 'RELOAD_RC=0' not in output:
            raise Exception('config reload failed (RC != 0), output:\n{}'.format(output))

    def save_configuration(self):
        self.engine.run_cmd('sudo config save -y', validate=True)

    def download_file_from_http_url(self, url, target_file_path):
        self.engine.run_cmd('sudo curl {} -o {}'.format(url, target_file_path), validate=True)

    def check_and_apply_dns(self):
        if not self.cli_obj.ip.is_static_dns_supported():
            with allure.step('Apply DNS servers configuration after reboot/reload'):
                self.cli_obj.ip.apply_dns_servers_into_resolv_conf()

    def reboot_reload_flow(self, r_type='reboot', ports_list=None, topology_obj=None, wait_after_ping=45,
                           reload_force=False, deploy_chipless=False, platform_params=None):
        """
        Wrapper for reboot and reload methods - which executes appropriate method based on reboot/reload type
        """
        if deploy_chipless:
            self.reload_configuration(reload_force)
            time.sleep(180)
        elif r_type == SonicConst.CONFIG_RELOAD_CMD:
            self.reload_flow(ports_list, topology_obj, reload_force, platform_params)
        else:
            self.reboot_flow(r_type, ports_list, topology_obj, wait_after_ping, platform_params)

    def reboot_flow(self, reboot_type='reboot', ports_list=None, topology_obj=None, wait_after_ping=45, platform_params=None):
        """
        Rebooting switch by given way(reboot, fast-reboot, warm-reboot) and validate dockers and ports state
        :param reboot_type: reboot type
        :param ports_list: list of the ports to check status after reboot
        :param topology_obj: topology object
        :param wait_after_ping: how long in second wait after ping before ssh connection
        :return: None, raise error in case of unexpected result
        """
        ports_list = self.get_ports_list_reboot_reload_flow(ports_list, topology_obj)
        with allure.step('Reboot switch by CLI - sudo {}'.format(reboot_type)):
            self.safe_reboot_flow(topology_obj, reboot_type, wait_after_ping=wait_after_ping)
            self.port_reload_reboot_checks(ports_list, platform_params=platform_params)
            self.check_and_apply_dns()

    def safe_reboot_flow(self, topology_obj, reboot_type='reboot', wait_after_ping=45,
                         check_sanitizer_after_reboot=True):
        logs_in_tmpfs = self.is_logs_in_tmpfs()
        self.copy_logs_before_reboot(logs_in_tmpfs)
        sanitizer = topology_obj.players['dut']['sanitizer']

        # ASAN/sanitizer images take significantly longer to stop swss/syncd
        # (graceful stop_services_asan so ASAN reports are flushed). If the
        # reboot command is run in the foreground of an SSH channel that is
        # then closed before /sbin/reboot is reached, the reboot script gets
        # SIGHUP mid-flow and the switch is left up with swss/syncd stopped.
        # See Redmine #5134692. Detach reboot from the SSH session and give
        # `reload()` extra headroom to detect shutdown.
        if sanitizer:
            reboot_cmd = (
                "nohup sudo /sbin/{rt} "
                "</dev/null >/dev/null 2>&1 & disown"
            ).format(rt=reboot_type)
            effective_wait_after_ping = wait_after_ping + SANITIZER_REBOOT_EXTRA_WAIT_SEC
            logger.info("ASAN/sanitizer image detected: using detached reboot "
                        "and extending wait_after_ping %ss -> %ss",
                        wait_after_ping, effective_wait_after_ping)
        else:
            reboot_cmd = f'sudo {reboot_type}'
            effective_wait_after_ping = wait_after_ping

        self.engine.reload([reboot_cmd], wait_after_ping=effective_wait_after_ping)
        self.restore_logs_after_reboot(logs_in_tmpfs)

        if check_sanitizer_after_reboot and sanitizer:
            test_name = os.environ.get('PYTEST_CURRENT_TEST').split(':')[-1].split(' ')[0]
            dumps_folder = os.environ.get(InfraConst.ENV_LOG_FOLDER)
            check_sanitizer_and_store_dump(self.engine, dumps_folder, test_name)

    def reload_flow(self, ports_list=None, topology_obj=None, reload_force=False, platform_params=None):
        """
        Reloading switch and validate dockers and ports state
        :param ports_list: list of the ports to check status after reboot
        :param topology_obj: topology object
        :param reload_force: provide if want to do reload with -f flag(force)
        :return: None, raise error in case of unexpected result
        """
        ports_list = self.get_ports_list_reboot_reload_flow(ports_list, topology_obj)
        self.disable_ztp(disable_ztp=True)
        with allure.step('Reloading dut'):
            logger.info("Reloading dut")
            self.reload_configuration(reload_force)
            self.port_reload_reboot_checks(ports_list, platform_params=platform_params)
            self.check_and_apply_dns()

    def get_ports_list_reboot_reload_flow(self, ports_list, topology_obj):
        if not (ports_list or topology_obj):
            raise Exception('ports_list or topology_obj must be passed to reload_flow method')
        if not ports_list:
            ports_list = topology_obj.players_all_ports[self.dut_alias]
        # in perf setup, expected ports in up state are left-tg: (Ethernet0::252, 4), right-tg: (Ethernet256::508, 4)
        if self.cli_obj.dut_alias in [PerfConsts.LEFT_TG_ALIAS, PerfConsts.RIGHT_TG_ALIAS]:
            ports_list = self.get_performance_ports_list(topology_obj)
        return ports_list

    def port_reload_reboot_checks(self, ports_list, platform_params=None):
        self.verify_dockers_are_up(platform_params=platform_params)
        self.cli_obj.interface.check_link_state(ports_list)

    def is_logs_in_tmpfs(self):
        log_filesystem = self.engine.run_cmd("df --output=fstype -h /var/log")
        logs_in_tmpfs = log_filesystem and "tmpfs" in log_filesystem
        config_db = self.get_config_db()
        platform = config_db['DEVICE_METADATA']['localhost']['platform']
        return logs_in_tmpfs or platform in PlatformTypesConstants.LOGS_ON_TMPFS_PLATFORMS

    def copy_logs_before_reboot(self, logs_in_tmpfs):
        if logs_in_tmpfs:
            # If it is not the first reboot in this test run, needs to backup also syslog.99
            if not self.backup_logs_stored:
                syslogs_99 = ''
                sairedis_99 = ''
                swss_99 = ''
            else:
                syslogs_99 = '/host/syslog.99'
                sairedis_99 = '/var/log/swss/sairedis.rec.99'
                swss_99 = '/var/log/swss/swss.rec.99'
            backup_log_cmds = [
                "sudo su",
                f"cat {syslogs_99} /var/log/syslog.1 /var/log/syslog > /host/syslog.99 || true",
                f"cat {sairedis_99} /var/log/swss/sairedis.rec.1 /var/log/swss/sairedis.rec > "
                f"/host/sairedis.rec.99 || true",
                f"cat {swss_99} /var/log/swss/swss.rec.1 /var/log/swss/swss.rec > /host/swss.rec.99 || true",
                "exit"
            ]
            self.engine.run_cmd_set(backup_log_cmds)
            self.backup_logs_stored = True

    def restore_logs_after_reboot(self, logs_in_tmpfs):
        if logs_in_tmpfs:
            restore_backup_cmds = ["sudo su",
                                   "mv /host/syslog.99 /var/log/",
                                   "mv /host/sairedis.rec.99 /var/log/swss/",
                                   "mv /host/swss.rec.99 /var/log/swss/",
                                   "exit"]
            self.engine.run_cmd_set(restore_backup_cmds)

    def validate_dockers_are_up_reboot_if_fail(self, topology_obj, retries=2, dockers_list=None, platform_params=None):
        """
        Reboot and validate docker containers are up on the switch
        :param topology_obj: Topology object for safe_reboot_flow (required)
        :param retries: int how many times do reboot of switch
        :param dockers_list: list of dockers to check (optional)
        :param platform_params: Platform parameters for multi-ASIC detection (optional)
        """
        initial_count = retries
        while retries:
            try:
                self.verify_dockers_are_up(dockers_list=dockers_list, platform_params=platform_params)
                logger.info(f"✓ All docker containers verified successfully")
                break
            except BaseException:
                logger.error('Caught exception {} during verifying docker containers are up.'
                             ' Rebooting dut and try again, try number {}'.format(traceback.print_exc(),
                                                                                  initial_count - retries + 1))
                logger.info(f"Performing reboot (attempt {initial_count - retries + 1}/{initial_count}) via safe_reboot_flow...")
                self.safe_reboot_flow(topology_obj, reboot_type='reboot')
            retries = retries - 1

    @retry(Exception, tries=25, delay=10)
    def verify_dockers_are_up(self, dockers_list=None, running_config=False, platform_params=None):
        """
        Verifying the dockers are in up state during a specific time interval
        :param dockers_list: list of dockers to check
        :param running_config: whether to read config from running config or file
        :param platform_params: Platform parameters for multi-ASIC detection
        :return: None, raise error in case of unexpected result
        """
        with allure.step('Check that dockers in UP state'):
            self._verify_dockers_are_up(dockers_list, running_config=running_config, platform_params=platform_params)

    def _get_default_dockers_list(self, running_config=False):
        """
        Verifying the dockers are in up state
        :param dockers_list: list of dockers to check
        :param running_config: whether to read config from running config or file
        :return: None, raise error in case of unexpected result
        """
        dockers_list = SonicConst.DOCKERS_LIST_LEAF

        # Try to get extended docker list for DUT type ToRRouter
        try:
            if running_config:
                logger.info('Reading config from running config')
                config_db = self.get_config_db_from_running_config()
            else:
                logger.info('Reading config from config_db.json')
                config_db = self.get_config_db()
            if self.is_bluefield(config_db['DEVICE_METADATA']['localhost']['hwsku']):
                dockers_list = SonicConst.DOCKERS_LIST_BF
            elif config_db['DEVICE_METADATA']['localhost']['type'] == 'ToRRouter':
                dockers_list = SonicConst.DOCKERS_LIST_TOR
            # in performance setup the docker dhcp_relay is disabled on TG switches to exclude multicast
            if self.cli_obj.dut_alias in [PerfConsts.LEFT_TG_ALIAS, PerfConsts.RIGHT_TG_ALIAS]:
                if 'dhcp_relay' in dockers_list:
                    dockers_list.remove('dhcp_relay')
        except json.JSONDecodeError:
            logger.warning('Can not get device type from config_db.json. Unable to parse config_db.json file')
        except KeyError:
            logger.warning('Can not get device type from config_db.json. Key does not exist')

        return dockers_list

    def _filter_dockers_by_branch(self, dockers_list):
        """
        Remove unsupported dockers based on the SONiC branch version
        :param dockers_list: List of docker names
        :return: Filtered list of docker names
        """
        cur_branch = self.get_image_sonic_version()
        base_branch = re.match(r'20[0-9]{4}', cur_branch)

        if not base_branch:
            base_branch = 999999
        else:
            base_branch = int(base_branch.group()[0])

        if base_branch < 202411:
            if 'gnmi' in dockers_list:
                dockers_list.remove('gnmi')

        return dockers_list

    def _expand_dockers_for_multi_asic(self, dockers_list, platform_params):
        """
        Expand docker names with ASIC indices for multi-ASIC platforms
        :param dockers_list: Original list of docker names
        :param platform_params: Platform parameters dict
        :return: Expanded list of docker names with ASIC indices
        """
        asic_count = detect_asic_count(self.engine, platform_params, raise_on_error=True)
        logger.info(f"Multi-ASIC platform: {platform_params.get('platform')}, expanding {len(dockers_list)} dockers for {asic_count} ASICs")

        expanded_dockers = []
        for docker in dockers_list:
            if docker in SonicConst.MultiAsic.DOCKERS_GLOBAL:
                expanded_dockers.append(docker)
            if docker in SonicConst.MultiAsic.DOCKERS_PER_ASIC:
                for asic_id in range(asic_count):
                    expanded_dockers.append(f'{docker}{asic_id}')

        return expanded_dockers

    def _verify_all_dockers_running(self, dockers_list):
        """
        Verify multiple docker containers are running using a single command execution on DUT.
        This is much faster than checking each container individually (1 SSH call vs N calls).

        :param dockers_list: list of docker container names to check
        :raises Exception: If any docker is not running
        """
        if not dockers_list:
            return

        # Get all running containers in a single command
        try:
            result = self.engine.run_cmd("docker ps --format '{{.Names}}'", validate=True)
            running_containers = set(result.strip().split('\n'))

            # Check which containers are missing
            required_containers = set(dockers_list)
            missing_containers = required_containers - running_containers

            if missing_containers:
                missing_list = ', '.join(sorted(missing_containers))
                raise Exception(f"Docker containers not running: {missing_list}")

        except Exception as e:
            if "Docker containers not running:" in str(e):
                raise
            else:
                logger.error(f"Error during batch docker verification: {e}")
                raise Exception(f"Failed to verify docker containers: {e}")

    def _verify_dockers_are_up(self, dockers_list, running_config=False, platform_params=None):
        """
        Verifying the dockers are in up state
        :param dockers_list: list of dockers to check
        :param running_config: whether to read config from running config or file
        :param platform_params: Platform parameters for multi-ASIC detection
        :return: None, raise error in case of unexpected result
        """
        # Get default docker list if not provided
        if dockers_list is None:
            dockers_list = self._get_default_dockers_list(running_config=running_config)

        # Filter dockers based on SONiC branch version
        dockers_list = self._filter_dockers_by_branch(dockers_list)

        # Expand docker names for multi-ASIC platforms
        if SonicInstallationSteps.is_multi_asic_platform(platform_params=platform_params):
            dockers_list = self._expand_dockers_for_multi_asic(dockers_list, platform_params)

        logger.info(f"Verifying docker containers are UP: {dockers_list}")

        self._verify_all_dockers_running(dockers_list)

        logger.info(f"✓ All {len(dockers_list)} docker containers verified successfully")

    def verify_processes_of_dockers(self, docker_list, hwsku):
        """
        Verifying the processes of dockers are in RUNNING state
        :param docker_list: list of dockers to check
        :return: None, raise error in case of unexpected result
        """
        success = True
        if self.is_bluefield(hwsku):
            expected_processes = SonicConst.DAEMONS_DICT_BF
        else:
            expected_processes = SonicConst.DAEMONS_DICT

        for docker in docker_list:
            running_processes = []
            processes_output = self.engine.run_cmd(f'docker exec {docker} supervisorctl status')
            for process_info in processes_output.splitlines():
                logger.info(f"process info:   {process_info}")
                process_name = process_info.split()[0].strip()
                process_status = process_info.split()[1].strip()
                if process_name in expected_processes[docker] and process_status == "RUNNING":
                    running_processes.append(process_name)
            if len(running_processes) != len(expected_processes[docker]):
                logger.error(f"Not all expected processes of docker {docker} are running.\n"
                             f"Expected: {expected_processes[docker]}\nRunning: {running_processes}")
                success = False
        assert success, 'Not all expected processes in RUNNING status'

    @retry(Exception, tries=2, delay=30)
    def generate_techsupport(self, duration=60):
        """
        Generate sysdump for a given time frame in seconds
        :param duration: time frame in seconds
        :return: dump path
        """
        with allure.step('Generate Techsupport of last {} seconds'.format(duration)):
            output = self.engine.run_cmd('sudo generate_dump -s \"-{} seconds\"'.format(duration), validate=True)
            return output.splitlines()[-1]

    def do_installation(self, topology_obj, image_path, deploy_type, fw_pkg_path, platform_params, dut_alias='dut'):
        if deploy_type == 'onie':
            if 'simx' in platform_params.platform:
                in_onie = True
            else:
                with allure.step('Preparing switch for installation'):
                    logger.info("Begin: Preparing switch for installation ")
                    in_onie = self.prepare_for_installation(topology_obj, dut_alias)
                    logger.info("End: Preparing switch for installation ")
            self.deploy_onie(image_path, in_onie, fw_pkg_path, platform_params, topology_obj, dut_alias)

        if deploy_type == 'sonic':
            self.deploy_sonic(image_path)

        if deploy_type == 'bfb':
            self.deploy_bfb(image_path, topology_obj)

        if deploy_type == 'pxe':
            self.deploy_pxe(image_path, topology_obj, platform_params['hwsku'])

    def deploy_image(self, topology_obj, image_path, apply_base_config=False, setup_name=None,
                     platform_params=None, deploy_type='sonic', reboot_after_install=None, fw_pkg_path=None,
                     set_timezone='Israel', disable_ztp=False, configure_dns=False, destination_hwsku=None,
                     setup_info=None, dut_alias=None, deploy_fanout_threads=None, docker_list=None,
                     fanout_target_version=None):

        if image_path.startswith('http'):
            image_path = '/auto/' + image_path.split('/auto/')[1]

        if setup_info and dut_alias and self.is_fanout_deploy_needed(setup_name):
            self.deploy_fanout(topology_obj, destination_hwsku, platform_params, setup_info, dut_alias,
                               deploy_fanout_threads, fanout_target_version=fanout_target_version)

        try:
            with allure.step("Trying to install sonic image"):
                self.do_installation(topology_obj, image_path, deploy_type, fw_pkg_path, platform_params, dut_alias)
        except OnieInstallationError:
            with allure.step("Caught exception OnieInstallationError during install. Perform reboot and trying again"):
                logger.error('Caught exception OnieInstallationError during install. Perform reboot and trying again')
                self.engine.disconnect()
                self.remote_reboot(topology_obj, dut_alias)
                logger.info('Sleeping %s seconds to handle ssh flapping' % InfraConst.SLEEP_AFTER_RRBOOT)
                time.sleep(InfraConst.SLEEP_AFTER_RRBOOT)
                self.do_installation(topology_obj, image_path, deploy_type, fw_pkg_path, platform_params, dut_alias)

    def modify_init_cfg_timezone(self, timezone):
        """
        Modify timezone in /etc/sonic/init_cfg.json file

        Args:
            timezone (str): The timezone value to set
        """
        try:
            logger.info('Modifying timezone in /etc/sonic/init_cfg.json to {}'.format(timezone))

            # Update timezone in /etc/sonic/init_cfg.json
            self.engine.run_cmd('sudo cp /etc/sonic/init_cfg.json /tmp/init_cfg.json')
            self.engine.run_cmd('sudo sed -i \'s|"timezone": "[^"]*"|"timezone": "{}"|g\' /tmp/init_cfg.json'.format(timezone))
            self.engine.run_cmd('sudo cp /tmp/init_cfg.json /etc/sonic/init_cfg.json')

        except Exception as e:
            logger.error('Failed to update timezone in init_cfg.json: {}'.format(str(e)))

    def deploy_image_post_installtion(self, topology_obj, apply_base_config=False, setup_name=None,
                                      platform_params=None, reboot_after_install=None,
                                      set_timezone='Israel', disable_ztp=False, configure_dns=False,
                                      setup_info=None, dut_alias=None, is_air=False, custom_config_db_air_path=None,
                                      deploy_chipless=False):
        # docker containers are not stable before all the platform configs are applied
        # will be verified after config_db is updated
        if not deploy_chipless:

            if not is_air:
                with allure.step('Verify dockers are up'):
                    self.verify_dockers_are_up()

            with allure.step("Modify default timezone"):
                self.modify_init_cfg_timezone(SonicConst.TIMEZONE)

        # Remove asic_table.json only for Air platforms
        if is_air:
            with allure.step('Remove asic_table.json to allow regeneration'):
                self.remove_asic_table(self.engine)

        # Validate and reboot if needed for multi-asic Air platforms or when reboot_after_install is True
        if reboot_after_install or (is_air and SonicInstallationSteps.is_multi_asic_platform(platform_params=platform_params)):
            with allure.step("Validate dockers are up, reboot if any docker is not up"):
                self.validate_dockers_are_up_reboot_if_fail(topology_obj, retries=3)

        if set_timezone:
            with allure.step("Set dut NTP timezone to {} time.".format(set_timezone)):
                self.engine.disconnect()
                system_set_timezone(self.engine, set_timezone)

        if not deploy_chipless:
            with allure.step("Init telemetry keys"):
                self.init_telemetry_keys()

        self.engine.disconnect()

        if apply_base_config:
            # hwsku for Air setups are read from the simulation metadata, there is no need to update the platform params
            if not is_air:
                self.update_platform_params(platform_params, setup_name)
            with allure.step("Apply basic config"):
                self.apply_basic_config(topology_obj, setup_name, platform_params, disable_ztp=disable_ztp,
                                        configure_dns=configure_dns, is_air=is_air, custom_config_db_air_path=custom_config_db_air_path,
                                        deploy_chipless=deploy_chipless)
            self.restart_determine_reboot_cause()
        else:
            self.disable_ztp(disable_ztp)

        self.configure_dhclient_if_simx()

    def init_telemetry_keys(self, engine=None):
        """
        Generate the streaming-telemetry server/root certificates under
        /etc/sonic/telemetry.

        :param engine: SSH engine identifying the host the certificates are
            generated on. Defaults to self.engine, i.e. the DUT this CLI object
            is bound to (the switch/fanout in the normal post-installation flow).
            Pass an explicit engine to target a different host -- e.g. a BMC
            reached over a dedicated SSH engine to bmc_ip during the BMC-only
            deploy flow, where the switch post-installation is skipped.
        """
        engine = engine or self.engine
        logger.info("Create telemetry directory")
        engine.run_cmd(f"sudo mkdir {SonicConst.TELEMETRY_PATH}")
        engine.run_cmd(f"sudo chmod 0755 {SonicConst.TELEMETRY_PATH}")
        logger.info("Generate server cert using openssl.")
        engine.run_cmd(f"sudo openssl req -x509 -sha256 -nodes -newkey rsa:2048 "
                       f"-keyout {SonicConst.TELEMETRY_SERVER_KEY} -subj '/CN=ndastreamingservertest' "
                       f"-out {SonicConst.TELEMETRY_SERVER_CER}")
        logger.info("Generate dsmsroot cert using openssl")
        engine.run_cmd(f"sudo openssl req -x509 -sha256 -nodes -newkey rsa:2048 "
                       f"-keyout {SonicConst.TELEMETRY_DSMSROOT_KEY} -subj '/CN=ndastreamingclienttest' "
                       f"-out {SonicConst.TELEMETRY_DSMSROOT_CER}")

    def deploy_sonic(self, image_path, is_skipping_migrating_package=False):
        tmp_target_path = '/home/admin/sonic-mellanox.bin'
        delimiter = self.get_installer_delimiter()

        def get_file_md5sum(file_path):
            import hashlib
            hash_md5 = hashlib.md5()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        image_path_md5sum = get_file_md5sum(image_path)
        with allure.step('Deploying image via SONiC'):
            self.configure_dhclient_if_simx()
            with allure.step('Copying image to dut'):
                retries, tmp_target_path_md5sum = 3, ""
                while retries > 0:
                    self.engine.copy_file(source_file=image_path, dest_file='sonic-mellanox.bin',
                                          file_system='/home/admin/', overwrite_file=True, verify_file=False)
                    tmp_target_path_md5sum = self.engine.run_cmd(f'md5sum {tmp_target_path}|cut -d" " -f1')
                    if tmp_target_path_md5sum == image_path_md5sum:
                        break
                    retries -= 1
                assert tmp_target_path_md5sum == image_path_md5sum, \
                    f"MD5sum of the image in local machine is not the same as the image in the dut. " \
                    f"Local machine: {image_path_md5sum}, DUT: {tmp_target_path_md5sum}"
                logger.info(f"image file md5sum check passed")
            with allure.step('Installing the image'):
                self.install_image(tmp_target_path, delimiter, is_skipping_migrating_package)

            with allure.step('Setting image as default'):
                image_binary = self.get_image_binary_version(tmp_target_path, delimiter)
                self.set_default_image(image_binary, delimiter)
            with allure.step('Removing the image'):
                self.engine.run_cmd(f'sudo rm -f {tmp_target_path}')

        with allure.step('Rebooting the dut'):
            self.engine.run_cmd('sudo reboot')
            logger.info("Waiting for switch shutdown after reload command")
            check_port_status_till_alive(False, self.engine.ip, self.engine.ssh_port)
            self.engine.disconnect()
            logger.info("Waiting for switch to be ready")
            check_port_status_till_alive(True, self.engine.ip, self.engine.ssh_port)

        with allure.step('Verifying installation'):
            with allure.step('Verifying dut booted with correct image'):
                # installer flavor might change after loading a different version
                delimiter = self.get_installer_delimiter()
                image_list = self.get_sonic_image_list(delimiter)
                assert 'Current: {}'.format(image_binary) in image_list

    def configure_dhclient_if_simx(self):
        if 'simx' in self.engine.run_cmd("hostname"):
            with allure.step('Configure dhclient on simx dut'):
                self.engine.run_cmd('sudo dhclient', validate=True)

    def deploy_bfb(self, image_path, topology_obj):
        rshim = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific'][
            'Parent_device_NIC_name']
        hyper_name = 'hyper' if 'hyper' in topology_obj.players else 'hypervisor'
        hyper_engine = topology_obj.players[hyper_name]['engine']

        with allure.step('Installing image by "bfb-install" on the switch/server'):
            LinuxGeneralCli(hyper_engine).install_bfb_image(image_path=image_path, rshim_num=rshim)

        # Broken engine after install. Disconnect it. In next run_cmd, it will connect back
        self.engine.disconnect()

        with allure.step('Waiting for switch bring-up after reload'):
            logger.info('Waiting for switch bring-up after reload')
            check_port_status_till_alive(True, self.engine.ip, self.engine.ssh_port)

    def deploy_pxe(self, image_path, topology_obj, hwsku):
        bf_cli_ssh_connect_bringup = 480
        if not self.is_bluefield(hwsku):
            raise Exception('The installation via PXE available only for Bluefield Devices')

        with allure.step('Installing image by PXE'):
            logger.info('Installing image by PXE')

        self.update_pxe_grub_config(image_path, topology_obj, hwsku)

        bmc_cli_obj = self.get_bf_bmc_cli_obj(topology_obj)
        with allure.step('Set next boot to PXE'):
            bmc_cli_obj.set_next_boot_pxe_bf()
        with allure.step('Reboot remotely the dut'):
            bmc_cli_obj.remote_reboot_bf()

        with allure.step(f'Wait {bf_cli_ssh_connect_bringup} seconds for downloading the image'):
            logger.info(f'Wait {bf_cli_ssh_connect_bringup} seconds for downloading the image')
            time.sleep(bf_cli_ssh_connect_bringup)

        with allure.step('Start to check if CLI connection is available'):
            logger.info('Start to check if CLI connection is available')
            retry_call(self.check_bf_cli_connection,
                       fargs=[],
                       tries=24,  # temporary workaround due to bad PXE performance, original value was 12
                       delay=60,
                       logger=logger)

    def check_bf_cli_connection(self):
        """
        This function checks the connection to switch via mgmt.
        During installation the Bluefield have flapped ping,
        so it can't be checked in regular way.

        :return: exception if connection not available
        """
        self.engine.disconnect()
        self.engine.run_cmd(DUMMY_COMMAND, validate=True)

    @staticmethod
    def update_pxe_grub_config(image_path, topology_obj, hwsku):
        """
        This function updating the grub cfg config on LAB-PXE server for Image and initramfs files for Bluefield
        device to required version.
        These files used for PXE boot.
        """
        image_path = os.path.realpath(image_path)  # in case provided image as symbolic link(for example the latest)

        pxe_dir_path = '/'.join(image_path.split('/')[:-2]) + '/Nvidia-bluefield/pxe'
        pxe_server_dir_path = '/sonic/sonic_dpu{}'.format(pxe_dir_path.split('/sonic/sonic_dpu')[1])

        dut_mgmt_mac_addr = \
            topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific']['mac_address']
        pxe_server_config_path = f'{BluefieldConstants.PXE_SERVER_CONFIGS_PATH}grub.cfg-bf-{dut_mgmt_mac_addr}'

        pxe_server_engine = LinuxSshEngine(ip=BluefieldConstants.PXE_SERVER,
                                           username=DefaultTestServerCred.DEFAULT_USERNAME,
                                           password=DefaultTestServerCred.DEFAULT_PASS)

        orig_grub_cfg_file = BluefieldConstants.GRUB_CFG_FILE_MAP[hwsku]
        image_str = '(tftp)/<update the location>/Image'
        initramfs_str = '(tftp)/<update the location>/initramfs'

        with open(os.path.join(pxe_dir_path, orig_grub_cfg_file)) as cfg_file_obj:
            pxe_server_engine.run_cmd(f'echo "" > {pxe_server_config_path}')
            for line in cfg_file_obj.readlines():
                if image_str in line:
                    line = line.replace(image_str, f'{pxe_server_dir_path}/Image')
                elif initramfs_str in line:
                    line = line.replace(initramfs_str, f'{pxe_server_dir_path}/initramfs')

                pxe_server_engine.run_cmd(f"echo '{line}' >> {pxe_server_config_path}")

    @staticmethod
    def get_bf_bmc_cli_obj(topology_obj):
        bmc_ip = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['BF Switch']['bmc']
        bmc_engine = LinuxSshEngine(ip=bmc_ip,
                                    username=BluefieldConstants.BMC_USER,
                                    password=BluefieldConstants.BMC_PASS)
        return LinuxGeneralCli(bmc_engine)

    def deploy_onie(self, image_path, in_onie, fw_pkg_path, platform_params, topology_obj, dut_alias='dut'):
        if not in_onie:
            onie_reboot_script_path = self.prepare_onie_reboot_script_on_dut()
            if self.required_onie_upgrade(fw_pkg_path, platform_params):
                self.reboot_by_onie_reboot_script(onie_reboot_script_path, 'update')
            else:
                self.reboot_by_onie_reboot_script(onie_reboot_script_path, 'install')

        if 'simx' in platform_params.platform:
            self.update_simx_platform_type(platform_params)
        else:
            SonicOnieCli(self.engine.ip, self.engine.ssh_port, fw_pkg_path, platform_params).update_onie()
            self.confirm_in_onie_install_mode(topology_obj, dut_alias)

        self.install_image_onie(self.engine, image_path)

    def confirm_in_onie_install_mode(self, topology_obj, dut_alias='dut'):
        in_onie = self.prepare_for_installation(topology_obj, dut_alias)
        logger.info(f"Onie status:{in_onie}, after preparing installation")
        if not in_onie:
            onie_reboot_script_path = self.prepare_onie_reboot_script_on_dut()
            self.reboot_by_onie_reboot_script(onie_reboot_script_path, 'install')

    def update_simx_platform_type(self, platform_params):
        """
        Update SIMX platform type in ONIE
        :param platform_params: platform_params fixture
        """
        board_name = SonicSimxConstants.PLATFORM_TO_MACHINE_NAME_DICT[platform_params.platform]
        SonicOnieCliDevts(self.engine.ip, board_name, logger, self.engine.ssh_port).update_onie()

    def required_onie_upgrade(self, fw_pkg_path, platform_params):
        platform_syseeprom_output = SonicChassisCli(self.engine).show_platform_syseeprom()
        latest_onie_version, _ = get_latest_onie_version(fw_pkg_path, platform_params)
        return latest_onie_version not in platform_syseeprom_output

    def is_onyx_deploy_needed(self, onyx_engine, onyx_name, onyx_config_path):
        """
        Check if deploy is needed for onyx fanout switch
        """
        logger.info("Save configuration at ONYX fanout switch")
        onyx_engine.run_cmd("configuration write")
        logger.info(f"Get into ONYX shell of {onyx_name}")
        dst_md5 = onyx_engine.run_cmd(f"md5sum {FanoutConfigFile.ONYX_CONFIG_PATH + FanoutConfigFile.ONYX}",
                                      run_in_shell=True).strip().split()[0]
        logger.info(f"Current MD5 value of configuration file at {onyx_name} is {dst_md5}")
        src_md5 = os.popen(f"sudo md5sum {onyx_config_path}").read().strip().split()[0]
        logger.info(f"Current MD5 value of configuration file at ngts docker is {src_md5}")

        if dst_md5 and src_md5 == dst_md5:
            logger.info("No need to deploy ONYX switch")
            return False
        logger.info("Shall deploy ONYX switch")
        return True

    def is_fanout_deploy_needed(self, setup_name):
        # skip deploy fanout for DPU setup
        if setup_name in BluefieldConstants.DPU_SETUP_LIST:
            return False
        return True

    def deploy_fanout_image(self, topology_obj, image_path, fanout_alias, platform_params=None):
        deploy_type = "onie"
        fw_pkg_path = None

        if image_path.startswith('http'):
            image_path = '/auto/' + image_path.split('/auto/')[1]

        try:
            with allure.step("Trying to install sonic image"):
                self.do_installation(topology_obj, image_path, deploy_type, fw_pkg_path, platform_params, fanout_alias)
        except OnieInstallationError:
            with allure.step("Caught exception OnieInstallationError during install. Perform reboot and trying again"):
                logger.error('Caught exception OnieInstallationError during install. Perform reboot and trying again')
                self.engine.disconnect()
                self.remote_reboot(topology_obj, fanout_alias)
                logger.info('Sleeping %s seconds to handle ssh flapping' % InfraConst.SLEEP_AFTER_RRBOOT)
                time.sleep(InfraConst.SLEEP_AFTER_RRBOOT)
                self.do_installation(topology_obj, image_path, deploy_type, fw_pkg_path, platform_params, fanout_alias)

        with allure.step('Verify dockers are up'):
            self.verify_dockers_are_up(dockers_list=SonicConst.DOCKERS_FANOUT)

    def deploy_sonic_fanout(self, topology_obj, target_version, setup_info, platform_params, fanout_name,
                            fanout_alias, set_timezone='Israel', disable_ztp=True):
        if target_version:
            # Check if is needed to install image on fanout.
            cli_version = self.get_image_sonic_version(only_branch=False)
            logger.info(f"Current fanout version is {cli_version}. The target version is {target_version}")

            match = re.search(r'sonic/([\w.-]+)/dev', target_version)

            install_image = False

            if match:
                target_version_name = match.group(1)
                if target_version_name != cli_version and target_version_name not in cli_version:
                    logger.info(f"Target version is {target_version_name}, but current fanout version is {cli_version}")
                    install_image = True
                logger.info(f"Current fanout version is {cli_version}")
            else:
                install_image = True
                logger.warning(
                    f"No version name find in `fanout_target_version`({str(target_version)}), use this target image forcefully.")

            if install_image:
                with allure.step("Upgrade fanout version on fanout."):
                    self.deploy_fanout_image(topology_obj, target_version, fanout_alias, platform_params)

        logger.info('Deploy SONiC fanout switch configurations')
        ansible_cmd = f"ansible-playbook -i lab fanout.yml -l {fanout_name}"
        logger.info(f"Running CMD: {ansible_cmd}")
        run_process_on_host(ansible_cmd, timeout=600, exec_path=setup_info['ansible_path'], validate=True)
        logger.info(f"Ansible deployment for fanout {fanout_name} completed")

        # Post deployment configuration
        with allure.step('Verify dockers are up'):
            self.verify_dockers_are_up(dockers_list=SonicConst.DOCKERS_FANOUT)

        if set_timezone:
            with allure.step("Set fanout NTP timezone to {} time.".format(set_timezone)):
                self.engine.disconnect()
                system_set_timezone(self.engine, set_timezone)

        with allure.step("Init telemetry keys"):
            self.init_telemetry_keys()

        self.engine.disconnect()

        with allure.step("Disable ZTP"):
            self.disable_ztp(disable_ztp)

    def deploy_onyx_fanout(self, base_path, setup_info, destination_hwsku, fanout_engine, fanout_name):
        fanout_config_path = os.path.join(base_path,
                                          f"../../../ansible/files/hwsku_vars/{setup_info['setup_name']}/"
                                          f"{destination_hwsku}/{FanoutConfigFile.ONYX}")
        if self.is_onyx_deploy_needed(fanout_engine, fanout_name, fanout_config_path):
            logger.info(f"Copy fanout configuration file to ONYX fanout switch {fanout_name}")
            fanout_engine.copy_file(source_file=fanout_config_path, dest_file=FanoutConfigFile.ONYX,
                                    file_system=FanoutConfigFile.ONYX_CONFIG_PATH, overwrite_file=True,
                                    verify_file=False)
            logger.info(f"Load fanout config file {FanoutConfigFile.ONYX}")
            fanout_engine.run_cmd(f"configuration switch-to {FanoutConfigFile.ONYX} no-reboot")
            fanout_engine.run_cmd("reload")

    def deploy_fanout(self, topology_obj, destination_hwsku, platform_params, setup_info, dut_alias, threads_dict,
                      fanout_target_version=None):
        """
        Copy the specific configuration file to fanout switch and load it
        """

        fanout_engine_type, fanout_name, fanout = self.get_fanout_info(topology_obj, dut_alias)

        if fanout and 'r-moose-06' not in fanout_name:
            if fanout_engine_type == CliType.SONIC:
                # Convert dut_alias to fanout_alias
                fanout_alias = 'fanout'
                if dut_alias == 'dut-b':
                    fanout_alias = 'fanout-b'

                fanout.deploy_sonic_fanout(topology_obj, fanout_target_version, setup_info,
                                           platform_params, fanout_name, fanout_alias)
            else:
                base_path = os.path.dirname(os.path.realpath(__file__))
                self.deploy_onyx_fanout(base_path, setup_info, destination_hwsku, fanout, fanout_name)

    def get_fanout_info(self, topology_obj, dut_alias):
        fanout_engine_type = None
        fanout_name = None
        fanout = None
        fanout_alias = 'fanout'
        if dut_alias == 'dut-b':
            fanout_alias = 'fanout-b'

        if topology_obj.players.get(fanout_alias):
            fanout_engine_type = topology_obj.players[fanout_alias]['attributes'].noga_query_data['attributes'][
                'Topology Conn.']['CLI_TYPE']
            fanout_name = topology_obj.players[fanout_alias]['attributes'].noga_query_data['attributes'][
                'Common']['Name']

            if fanout_engine_type == CliType.SONIC:
                fanout = topology_obj.players[fanout_alias]['cli']
                fanout = fanout.general
            else:
                fanout = topology_obj.players[fanout_alias]['engine']
        return fanout_engine_type, fanout_name, fanout

    def disable_ipv6_sonic_fanout(self, topology_obj, dut_alias=None):
        """
        For sonic fanout, after fanout deployment or system reboot, it should disable ipv6 function to make sure
        no icmpv6 packets generated
        """
        if topology_obj.players.get('fanout') or topology_obj.players.get('fanout-b'):
            fanout_alias = 'fanout'
            if dut_alias == 'dut-b':
                fanout_alias = 'fanout-b'
            if topology_obj.players.get(fanout_alias):
                fanout_engine_type = topology_obj.players[fanout_alias]['attributes'].noga_query_data['attributes'][
                    'Topology Conn.']['CLI_TYPE']
                if fanout_engine_type == CliType.SONIC:
                    fanout_engine = topology_obj.players.get(fanout_alias)['engine']
                    fanout_engine.run_cmd('sudo sysctl -p')

    def switch_dut_to_onie_due_to_unmatched_password(self):
        switch_in_onie = False
        with allure.step("Try other password because default password doesn't work"):
            try:
                # Checking if device is in sonic
                self.engine.run_cmd(DUMMY_COMMAND, validate=True)
            except netmiko.ssh_exception.NetmikoAuthenticationException:
                self.if_other_credentials_used_set_boot_order_onie()
                logger.info('Next boot set to onie succeed with new password')
                SonicOnieCli(self.engine.ip, self.engine.ssh_port).confirm_onie_boot_mode_install()
                switch_in_onie = True
            return switch_in_onie

    def disable_ztp(self, disable_ztp=False):
        if disable_ztp:
            with allure.step('Disable ZTP'):
                retry_call(self.cli_obj.ztp.disable_ztp,
                           fargs=[],
                           tries=3,
                           delay=10,
                           logger=logger)

    def update_platform_params(self, platform_params, setup_name):
        if platform_params.filtered_platform in PLATFORMS_TO_NOT_UPDATE_PLATFORM_PARAMS:
            return
        if hasattr(self, 'cli_obj'):  # SONiC only
            current_platform_summary = self.cli_obj.chassis.parse_platform_summary()
            if platform_params["hwsku"] != current_platform_summary["HwSKU"] \
                    or platform_params["platform"] != current_platform_summary["Platform"] \
                    or self.is_performance_setup(setup_name):
                logger.info(f'Updating the platform_params for {setup_name}')
                logger.info(f'Current platform_params: \n{platform_params}')
                platform_params["hwsku"] = current_platform_summary["HwSKU"]
                platform_params["platform"] = current_platform_summary["Platform"]
                hostname = self.cli_obj.chassis.get_hostname()
                update_platform_info_files(hostname, current_platform_summary, update_inventory=True)
                logger.info(f'Updated platform_params: \n{platform_params}')

    def apply_basic_config(self, topology_obj, setup_name, platform_params, reload_before_qos=False,
                           disable_ztp=False, configure_dns=True, is_air=False, custom_config_db_air_path=None,
                           deploy_chipless=False):
        with allure.step("Upload port_config.ini and config_db.json with reload of dut"):
            retry_call(self.apply_config_files,
                       fargs=[topology_obj, setup_name, platform_params, is_air, custom_config_db_air_path, deploy_chipless],
                       tries=3,
                       delay=10,
                       logger=logger)

        self.disable_ztp(disable_ztp)

        with allure.step('Remove FRR configuration(which may contain default BGP config)'):
            self.cli_obj.frr.remove_frr_config_files()

        if reload_before_qos:
            with allure.step("Reload the dut"):
                self.reboot_reload_flow(r_type=SonicConst.CONFIG_RELOAD_CMD, topology_obj=topology_obj,
                                        reload_force=True)

        if deploy_chipless:
            self.cli_obj.qos.reload_qos(no_dynamic=True, platform_params=platform_params)
        else:
            if not self.is_performance_setup(setup_name):
                with allure.step("Apply qos and dynamic buffer config"):
                    no_dynamic = platform_params["hwsku"].upper().startswith(SonicConst.NO_DYNAMIC_BUFFER_HWSKU_PREFIXES)
                    self.cli_obj.qos.reload_qos(no_dynamic=no_dynamic, platform_params=platform_params)
                    self.verify_dockers_are_up(dockers_list=['swss'], platform_params=platform_params)
                    self.cli_obj.qos.stop_buffermgrd(platform_params=platform_params)
                    self.cli_obj.qos.start_buffermgrd(platform_params=platform_params)

        with allure.step("Enable INFO logging on swss"):
            self.enable_info_logging_on_swss(platform_params=platform_params)

        with allure.step("Configure ntp servers"):
            for ntp_server in SonicConst.NTP_SERVERS:
                self.add_ntp_server(ntp_server)

        if configure_dns:
            with allure.step('Apply DNS servers configuration'):
                self.cli_obj.ip.apply_dns_servers_into_resolv_conf(is_air_setup=is_air)

        self.cli_obj.general.save_configuration()

    def get_chip_gen(self, platform_params):
        chip_gen = int((re.search(r'SN(\d)', platform_params['hwsku']).group(1))) - 1
        return f"SPC{chip_gen}"

    def is_config_db_json_exists(self, setup_name):
        config_db_path = os.path.join(InfraConst.MARS_TOPO_FOLDER_PATH, setup_name, SonicConst.CONFIG_DB_JSON)
        return os.path.exists(config_db_path)

    def _list_config_db_files(self, config_db_files_path):
        """
        List all config_db files from source directory
        :param config_db_files_path: Path to directory containing multi-ASIC config files
        :return: List of config file names
        """
        config_files = [f for f in os.listdir(config_db_files_path)
                        if f.startswith('config_db') and os.path.isfile(os.path.join(config_db_files_path, f))]

        if not config_files:
            error_msg = f"No config_db files found in {config_db_files_path}"
            logger.error(error_msg)
            raise FileNotFoundError(error_msg)

        logger.info(f"Found {len(config_files)} config_db files: {config_files}")
        return config_files

    def _copy_config_db_files_to_switch_tmp(self, config_db_files_path, config_files):
        """
        Copy config_db files from source directory to /tmp/ on the switch
        :param config_db_files_path: Path to directory containing multi-ASIC config files
        :param config_files: List of config file names to copy
        """
        logger.info(f"Copying {len(config_files)} config files to /tmp/ on the switch: {config_files}")
        for config_file in config_files:
            source_file = os.path.join(config_db_files_path, config_file)
            try:
                self.engine.copy_file(source_file=source_file,
                                      dest_file=config_file, file_system='/tmp/',
                                      overwrite_file=True, verify_file=False)
            except Exception as e:
                logger.error(f"✗ Failed to copy {config_file}: {e}")
                raise

    def _move_config_files_to_sonic(self, engine):
        """
        Move all config_db files from /tmp/ to /etc/sonic/
        :param engine: SSH engine object
        """
        engine.run_cmd(f'sudo mv /tmp/config_db* /etc/sonic/')

    def _run_db_migrator(self, engine):
        """
        Run db_migrator to migrate configuration
        :param engine: SSH engine object
        """
        logger.info("Running db_migrator to migrate configuration")
        engine.run_cmd('sudo /usr/local/bin/db_migrator.py -o migrate')

    def _load_global_config_to_db(self, engine):
        """
        Load global configuration to database
        :param engine: SSH engine object
        """
        engine.run_cmd('sudo /usr/local/bin/sonic-cfggen -j /etc/sonic/init_cfg.json -j /etc/sonic/config_db.json --write-to-db')

    def _load_per_asic_configs(self, engine, config_files):
        """
        Load per-ASIC configurations into their respective namespaces
        :param engine: SSH engine object
        :param config_files: List of all config file names
        """
        per_asic_configs = [f for f in config_files if f.startswith('config_db') and f != 'config_db.json']

        if not per_asic_configs:
            return

        logger.info(f"Loading {len(per_asic_configs)} per-ASIC configs: {sorted(per_asic_configs)}")
        for config_file in per_asic_configs:
            match = re.search(r'config_db(\d+)\.json', config_file)
            if not match:
                logger.warning(f"Skipping {config_file}: doesn't match expected pattern config_db<N>.json")
                continue

            asic_num = match.group(1)
            try:
                engine.run_cmd(f'sudo ip netns exec asic{asic_num} sonic-cfggen -j /etc/sonic/{config_file} --write-to-db')
            except Exception as e:
                logger.error(f"✗ Failed to load {config_file} into asic{asic_num}: {e}")
                raise

    def _generate_sonic_environment(self, engine):
        """
        Generate sonic-environment configuration
        :param engine: SSH engine object
        """
        engine.run_cmd('sudo /usr/local/bin/sonic-cfggen -d -y /etc/sonic/sonic_version.yml -t /usr/share/sonic/templates/sonic-environment.j2,/etc/sonic/sonic-environment')

    def _cleanup_docker_containers(self, engine):
        """
        Remove all docker containers before reboot
        :param engine: SSH engine object
        """
        logger.info("Cleaning up docker containers before reboot")
        engine.run_cmd('sudo docker rm -f $(docker ps -aq)', validate=False)

    def apply_multi_asic_config_db(self, engine, topology_obj, config_db_files_path, platform_params=None):
        """
        Finish multi-asic setup
        :param engine: SSH engine object
        :param topology_obj: Topology object
        :param config_db_files_path: Path to directory containing multi-ASIC config files
        :param platform_params: Platform parameters for docker verification (optional)
        """
        logger.info(f"Applying multi-ASIC config from: {config_db_files_path}")

        config_files = self._list_config_db_files(config_db_files_path)
        self._copy_config_db_files_to_switch_tmp(config_db_files_path, config_files)
        self._move_config_files_to_sonic(engine)
        self._run_db_migrator(engine)
        self._load_global_config_to_db(engine)
        self._load_per_asic_configs(engine, config_files)
        self._generate_sonic_environment(engine)
        self._cleanup_docker_containers(engine)

        logger.info("Rebooting to apply multi-ASIC configuration")
        self.safe_reboot_flow(topology_obj, reboot_type='reboot')

        # Verify dockers are up, with automatic reboot-and-retry if needed (up to 2 attempts)
        logger.info("Verifying multi-ASIC docker containers are up (will auto-reboot if needed)...")
        self.validate_dockers_are_up_reboot_if_fail(topology_obj, retries=2, dockers_list=None,
                                                    platform_params=platform_params)

        logger.info("✓ Multi-ASIC configuration applied successfully")

    def _copy_sn5640_simx_sku_from_physical_to_simx_and_update_simx_specific_files(self, sn5640_simx_platform):
        """
        After copying Mellanox-SN5640-C512S2 from physical to simx, align simx-specific files:
        drop independent-module SAI mode from sai.profile, and skip xcvrd in PMON (not CMIS mgr).
        """
        # This is a workaround to copy the SN5640 default sku from physical to simx
        # The default sku is Mellanox-SN5640-C512S2
        # and it is not available in the simx
        # so we need to copy it from the physical platform folder to the simx platform folder
        logger.info("Copying 5640 default sku from physical to simx")
        cp_5640_default_sku_from_physic_to_simx = f"sudo cp -rf /usr/share/sonic/device/x86_64-nvidia_sn5640-r0/Mellanox-SN5640-C512S2 /usr/share/sonic/device/{sn5640_simx_platform}/"
        self.engine.run_cmd(cp_5640_default_sku_from_physic_to_simx)

        logger.info("Updating simx-specific files")
        sku_path = f'/usr/share/sonic/device/{sn5640_simx_platform}/Mellanox-SN5640-C512S2'
        logger.info('Removing SAI_INDEPENDENT_MODULE_MODE=1 from simx sai.profile if present')
        self.engine.run_cmd(f"sudo sed -i '/^SAI_INDEPENDENT_MODULE_MODE=1$/d' {sku_path}/sai.profile")
        logger.info('Updating pmon_daemon_control.json: remove skip_xcvrd_cmis_mgr, set skip_xcvrd true')
        self.engine.run_cmd(
            f"sudo python3 -c \"import json; p='{sku_path}/pmon_daemon_control.json'; "
            f"d=json.load(open(p)); d.pop('skip_xcvrd_cmis_mgr', None); d['skip_xcvrd']=True; "
            f"f=open(p,'w'); json.dump(d, f, indent=4); f.write('\\n'); f.close()\""
        )

    def apply_config_files(self, topology_obj, setup_name, platform_params, is_air, custom_config_db_air_path=None,
                           deploy_chipless=False):
        platform = platform_params['platform']
        hwsku = platform_params['hwsku']
        shared_path = '{}{}'.format(InfraConst.MARS_TOPO_FOLDER_PATH, setup_name)
        config_db = None

        # this is a workaround to copy the SN5640 default sku from physical to simx
        # when image implementation supports SN5640-C512S2, this will be removed
        sn5640_simx_platform = PlatformTypesConstants.PLATFORM_BISON_SIMX
        if platform == sn5640_simx_platform:
            self._copy_sn5640_simx_sku_from_physical_to_simx_and_update_simx_specific_files(sn5640_simx_platform)

        if is_air:
            if custom_config_db_air_path:
                if SonicInstallationSteps.is_multi_asic_platform(platform_params=platform_params):
                    logger.info(f"Applying multi-ASIC config from: {custom_config_db_air_path}")
                    self.apply_multi_asic_config_db(self.engine, topology_obj, custom_config_db_air_path, platform_params)
                    logger.info(f"✓ Multi-ASIC config applied for {setup_name}")
                    return
                else:
                    logger.info(f"Loading single-ASIC config from: {custom_config_db_air_path}")
                    with open(custom_config_db_air_path, 'r') as f:
                        config_db = json.load(f)
            else:
                logger.info("Preparing default AIR config")
                config_db = self.prepare_nvidia_air_basic_config_db_json(topology_obj, setup_name, hwsku, platform)
        elif not self.is_performance_setup(setup_name):
            self.upload_port_config_ini(platform, hwsku, shared_path)

        config_db = self.upload_config_db_file(topology_obj, setup_name, hwsku, platform, config_db, deploy_chipless)
        if not is_air:
            config_file_prefix = self.get_config_file_prefix(setup_name)
            config_db_file_name = f"{self.get_image_sonic_version()}_{config_file_prefix}config_db.json"
            self.create_extended_config_db_file(setup_name, config_db, file_name=config_db_file_name)
        self.cli_obj.im.enable_im(topology_obj=topology_obj, platform_params=platform_params,
                                  chip_type=self.get_chip_gen(platform_params), enable_im=True)
        self.reboot_reload_flow(r_type=SonicConst.CONFIG_RELOAD_CMD, topology_obj=topology_obj, reload_force=True,
                                deploy_chipless=deploy_chipless)

    def upload_port_config_ini(self, platform, hwsku, shared_path):
        switch_config_ini_path = f'/usr/share/sonic/device/{platform}/{hwsku}'
        config_file_prefix = self.get_config_file_prefix(shared_path)
        source_file = os.path.join(shared_path, config_file_prefix + SonicConst.PORT_CONFIG_INI)
        logger.info(f'Copy file {source_file} to /tmp directory on a switch')
        self.engine.copy_file(source_file=source_file,
                              dest_file=SonicConst.PORT_CONFIG_INI, file_system='/tmp/',
                              overwrite_file=True, verify_file=False)
        self.engine.run_cmd(f'sudo mv /tmp/{SonicConst.PORT_CONFIG_INI} {switch_config_ini_path}')

    def get_config_file_prefix(self, str_with_setup_name):
        prefix = ''
        if self.is_performance_setup(str_with_setup_name):
            prefix = self.hostname() + '_'
        return prefix

    def upload_config_db_file(self, topology_obj, setup_name, hwsku, platform, config_db=None, deploy_chipless=False):
        config_file_prefix = self.get_config_file_prefix(setup_name)
        if config_db is None:
            config_db = self.get_config_db_json_obj(setup_name, config_file_prefix + SonicConst.CONFIG_DB_JSON)
        if not self.is_performance_setup(setup_name):
            config_db = self.get_updated_config_db(topology_obj, setup_name, hwsku, platform, config_db, deploy_chipless)

        save_config_db_json(self.engine, config_db)
        if self.is_performance_setup(setup_name):
            self.reload_configuration(force=True)
        return config_db

    def update_sai_xml_file(self, platform, hwsku, global_flag=False, local_flags=False, platform_params=None):
        if SonicInstallationSteps.is_multi_asic_platform(platform_params=platform_params):
            # Workaround for bug https://redmine.mellanox.com/issues/4741956
            # Per HLD, sai.profile should be in the common/ directory for multi-ASIC platforms,
            # but is currently located under the /0 directory. Check if bug is still active.
            if is_redmine_issue_active(4741956):
                logger.info("Bug 4741956 still active - using workaround path: /0 directory")
                switch_sai_xml_path = f'/usr/share/sonic/device/{platform}/{hwsku}/0'
            else:
                logger.info("Bug 4741956 resolved - using correct path: common/ directory")
                switch_sai_xml_path = f'/usr/share/sonic/device/{platform}/{hwsku}/common'
        else:
            switch_sai_xml_path = f'/usr/share/sonic/device/{platform}/{hwsku}'
        default_sai_xml_file_name = 'sai.profile'

        logger.info('Get SAI init config file path')
        output = self.engine.run_cmd(
            f"sudo cat {switch_sai_xml_path}/{default_sai_xml_file_name} | grep \"SAI_INIT_CONFIG_FILE\"")

        logger.info(f'Get SAI init config file name {output}')
        # Match output of SAI_INIT_CONFIG_FILE=/usr/share/sonic/hwsku/sai_platform_name.xml
        actual_sai_xml_file_name = output.split("=")[1].split("/")[-1]

        logger.info(f'Get SAI actual config file name {actual_sai_xml_file_name}')
        actual_switch_sai_xml_path = switch_sai_xml_path + '/' + actual_sai_xml_file_name

        logger.info('Copy actual SAI config file to sonic-mgmt')
        self.engine.copy_file(source_file=f"{actual_switch_sai_xml_path}",
                              dest_file=f'/tmp/{actual_sai_xml_file_name}', file_system='/tmp/', direction='get')
        logger.info(f'Add appropriate flags to actual sai.xml {actual_switch_sai_xml_path}')
        self.modify_xml(f"/tmp/{actual_sai_xml_file_name}", global_flag=global_flag, local_flags=local_flags)

        logger.info('Copy modified file to DUT /tmp/ folder')
        self.engine.copy_file(source_file=f"/tmp/{actual_sai_xml_file_name}",
                              dest_file=actual_sai_xml_file_name, file_system='/tmp/',
                              overwrite_file=True, verify_file=False)

        logger.info(f'Move file {actual_sai_xml_file_name} from /tmp/ to {actual_switch_sai_xml_path}')
        self.engine.run_cmd(f'sudo mv /tmp/{actual_sai_xml_file_name} {actual_switch_sai_xml_path}')

    def modify_xml(self, filepath, global_flag=False, local_flags=False):
        doc = ET.parse(filepath)
        root_node = doc.getroot()
        child_node = root_node.find('platform_info')
        if global_flag:
            # Add global flag to child node
            logger.info('Add global flag to child node')
            global_flag = ET.Element("late-create-all-ports")
            global_flag.text = "1"
            child_node.append(global_flag)

        if local_flags:
            logger.info('Add local flag to child node')
            # Add local flag to all ports
            local_flag = ET.Element("late-create")
            local_flag.text = "true"
            for child in child_node:
                for element in child:
                    if element.tag == 'port-info':
                        element.append(local_flag)
        tree = ET.ElementTree(root_node)
        # ET.indent(tree, space="\t", level=0)
        tree.write(filepath, encoding="utf-8")

    def get_updated_config_db(self, topology_obj, setup_name, hwsku, platform, config_db, deploy_chipless=False):
        self.update_config_db_metadata_router(config_db)
        self.update_config_db_metadata_mgmt_port(config_db)
        self.update_config_db_metadata_hwsku(hwsku, config_db)
        self.update_config_db_metadata_timezone(SonicConst.TIMEZONE, config_db)
        self.update_config_db_features(setup_name, hwsku, platform, config_db)
        self.update_config_db_feature_config("database", "auto_restart", "always_enabled", config_db)
        default_mtu = "9100"
        self.update_config_db_port_mtu_config(default_mtu, config_db)
        if not deploy_chipless:
            self.update_config_db_breakout_cfg(topology_obj, setup_name, hwsku, platform, config_db)
        self.update_config_db_simx_setup_metadata_mac(setup_name, config_db)
        self.restore_container_autorestart(config_db)
        return config_db

    def update_config_db_simx_setup_metadata_mac(self, setup_name, config_db):
        expected_base_mac_file_path = f'/tmp/simx_base_mac_{self.engine.ip}'
        if os.path.exists(expected_base_mac_file_path):
            mac_address = os.popen(f"cat {expected_base_mac_file_path}").read()
            logger.info(f"Update the mac address for simx setup to: {mac_address}")
            config_db[ConfigDbJsonConst.DEVICE_METADATA][ConfigDbJsonConst.LOCALHOST][ConfigDbJsonConst.MAC] = \
                mac_address

    @staticmethod
    def get_image_unsupported_features(current_features, image_supported_features):
        image_unsupported_features = [feature for feature in current_features if
                                      feature not in image_supported_features]
        logger.info(f"image_unsupported_features: {image_unsupported_features}")
        return image_unsupported_features

    @staticmethod
    def remove_config_db_unsupported_features(current_features, image_unsupported_features,
                                              auto_techsupport_features):
        for feature in image_unsupported_features:
            del current_features[feature]
            auto_techsupport_features.pop(feature, None)

    def remove_unsupported_features_from_syslog_config_feature(self, config_db_json_data, image_unsupported_features):
        for feature in image_unsupported_features:
            self.remove_feature_from_syslog_config_feature(config_db_json_data, feature)

    def update_config_db_features(self, setup_name, hwsku, platform, config_db):
        init_config_db_json = self.get_init_config_db_json_obj(hwsku, platform, setup_name)
        image_supported_features = init_config_db_json[ConfigDbJsonConst.FEATURE]
        current_features = config_db[ConfigDbJsonConst.FEATURE]
        auto_techsupport_features = config_db.get(ConfigDbJsonConst.AUTO_TECHSUPPORT_FEATURE, {})
        for feature, feature_properties in image_supported_features.items():
            if feature not in current_features:
                current_features[feature] = feature_properties
            has_timer_value = current_features[feature].pop("has_timer", None)
            if has_timer_value:
                current_features[feature]["delayed"] = has_timer_value

        image_unsupported_features = self.get_image_unsupported_features(current_features, image_supported_features)
        self.remove_config_db_unsupported_features(
            current_features, image_unsupported_features, auto_techsupport_features)
        self.remove_unsupported_features_from_syslog_config_feature(config_db, image_unsupported_features)

        if 'doai' not in current_features:
            config_db.pop('AR_GLOBAL', None)

        if 'what-just-happened' not in current_features:
            config_db.pop('WJH', None)
            config_db.pop('WJH_CHANNEL', None)
            logger.info("Removed WJH and WJH_CHANNEL tables as what-just-happened feature is not supported")

    def update_config_db_metadata_router(self, config_db_json):
        hwsku = config_db_json[ConfigDbJsonConst.DEVICE_METADATA][ConfigDbJsonConst.LOCALHOST][ConfigDbJsonConst.HWSKU]
        localhost_type = ConfigDbJsonConst.TOR_ROUTER
        if 'bobcat' in self.hostname():
            localhost_type = ConfigDbJsonConst.LEAF_ROUTER
        if self.is_bluefield(hwsku):
            localhost_type = ConfigDbJsonConst.SONIC_HOST
        config_db_json[ConfigDbJsonConst.DEVICE_METADATA][ConfigDbJsonConst.LOCALHOST][ConfigDbJsonConst.TYPE] = \
            localhost_type

    def update_config_db_docker_routing_config_mode(self, topology_obj, mode='split-unified',
                                                    remove_docker_routing_config_mode=False):
        config_db = self.get_config_db()
        config_db_localhost = config_db[ConfigDbJsonConst.DEVICE_METADATA][ConfigDbJsonConst.LOCALHOST]

        if remove_docker_routing_config_mode:
            config_db_localhost.pop(ConfigDbJsonConst.DOCKER_ROUTING_CONFIG_MODE, None)
        else:
            config_db_localhost.update({ConfigDbJsonConst.DOCKER_ROUTING_CONFIG_MODE: mode})

        save_config_db_json(self.engine, config_db)
        self.reboot_reload_flow(r_type=SonicConst.CONFIG_RELOAD_CMD, topology_obj=topology_obj, reload_force=True)

    def update_config_db_metadata_mgmt_port(self, config_db):
        config_db[ConfigDbJsonConst.MGMT_PORT] = json.loads(ConfigDbJsonConst.MGMT_PORT_VALUE)

    def update_config_db_metadata_hwsku(self, hwsku, config_db):
        config_db[ConfigDbJsonConst.DEVICE_METADATA][ConfigDbJsonConst.LOCALHOST][ConfigDbJsonConst.HWSKU] = \
            hwsku

    def update_config_db_metadata_timezone(self, timezone, config_db):
        config_db[ConfigDbJsonConst.DEVICE_METADATA][ConfigDbJsonConst.LOCALHOST]['timezone'] = timezone

    def update_config_db_hostname(self, setup_name, hostname, config_db_json_file_name):
        config_db_json = self.get_config_db_json_obj(setup_name, config_db_json_file_name=config_db_json_file_name)
        config_db_json[ConfigDbJsonConst.DEVICE_METADATA][ConfigDbJsonConst.LOCALHOST][ConfigDbJsonConst.HOSTNAME] = \
            hostname
        return self.create_extended_config_db_file(setup_name, config_db_json, file_name=config_db_json_file_name)

    def update_config_db_feature_config(self, feature_name, feature_config_key, feature_config_value, config_db):
        config_db[ConfigDbJsonConst.FEATURE][feature_name][feature_config_key] = feature_config_value

    def update_config_db_port_mtu_config(self, mtu, config_db):
        for k, _ in config_db[ConfigDbJsonConst.PORT].items():
            config_db[ConfigDbJsonConst.PORT][k]["mtu"] = mtu

    @staticmethod
    def remove_feature_from_syslog_config_feature(config_db_json_data, feature_name):
        syslog_config_key = "SYSLOG_CONFIG_FEATURE"
        if syslog_config_key in config_db_json_data:
            if feature_name in config_db_json_data[syslog_config_key]:
                config_db_json_data[syslog_config_key].pop(feature_name)
                logger.info(f"Remove {feature_name} from SYSLOG_CONFIG_FEATURE")

    def restore_container_autorestart(self, config_db):
        with allure.step("Restoring container autorestart"):
            feature_key = "FEATURE"
            if feature_key in config_db:
                feature_status_dict = self.show_and_parse_feature_status()
                for feature in feature_status_dict:
                    if feature in config_db[feature_key]:
                        auto_restart_state = feature_status_dict[feature]["AutoRestart"]
                        config_db[feature_key][feature]["auto_restart"] = auto_restart_state

    def update_config_db_metadata_mgmt_ip(self, setup_name, ip, file_name=SonicConst.CONFIG_DB_JSON):
        def _get_subnet_mask(ip, interfaces_ips_output):
            for elem in interfaces_ips_output:
                if InfraConst.IP in elem:
                    if elem[InfraConst.IP] == ip:
                        return elem[InfraConst.MASK]

        def _get_default_gw():
            def _get_by_iproute():
                routes = self.cli_obj.route.show_ip_route()
                default_gw_obj = re.search(r'0\.0\.0\.0\/0 \[.*\] via (.*), eth0', routes)
                return default_gw_obj.group(1) if default_gw_obj else None

            def _get_by_arp():
                arp_table_dict = self.cli_obj.arp.show_arp_table()
                for arp_ip, arp_info in arp_table_dict.items():
                    if arp_info['Iface'] == 'eth0' and arp_info['MacAddress'] != '(incomplete)':
                        return arp_ip

            _default_gw = _get_by_iproute()
            if not _default_gw:
                _default_gw = _get_by_arp()
            return _default_gw

        config_db_json = self.get_config_db()
        mask = _get_subnet_mask(ip, self.cli_obj.ip.get_interface_ips('eth0'))
        default_gw = _get_default_gw()
        config_db_json[ConfigDbJsonConst.MGMT_INTERFACE] = \
            json.loads(ConfigDbJsonConst.MGMT_INTERFACE_VALUE % (ip, mask, default_gw))

        return self.create_extended_config_db_file(setup_name, config_db_json, file_name)

    @staticmethod
    def is_platform_supports_split_without_unmap(hwsku):
        platform_prefix_with_unmap = ["SN2700", "MSN4600C"]
        for platform_prefix in platform_prefix_with_unmap:
            if re.search(platform_prefix, hwsku):
                return False
        return True

    def update_config_db_breakout_cfg(self, topology_obj, setup_name, hwsku, platform, config_db):
        init_config_db_json = self.get_init_config_db_json_obj(hwsku, platform, setup_name)
        if init_config_db_json.get("BREAKOUT_CFG") and not self.is_bluefield(hwsku):
            self.update_breakout_cfg(topology_obj, init_config_db_json, config_db, hwsku)

    @staticmethod
    def is_bluefield(hwsku):
        for bf_hwsku in BluefieldConstants.BLUEFIELD_HWSKUS_LIST:
            if bf_hwsku.lower() in hwsku.lower():
                return True
        return False

    def is_default_hwsku(self, hwsku, setup_name):
        is_default_hwsku = True
        if self.is_performance_setup(setup_name) and hwsku == PerformanceSetupConstants.HWSKU:
            is_default_hwsku = False
        return is_default_hwsku

    @staticmethod
    def get_config_db_json_obj(setup_name, config_db_json_file_name=SonicConst.CONFIG_DB_JSON):
        config_db_path = str(os.path.join(InfraConst.MARS_TOPO_FOLDER_PATH, setup_name, config_db_json_file_name))
        with open(config_db_path) as config_db_json_file:
            config_db_json = json.load(config_db_json_file)
        return config_db_json

    def get_init_config_db_json_obj(self, hwsku, platform, setup_name):
        cmd = f"sonic-cfggen -k {hwsku} -H -j /etc/sonic/init_cfg.json --print-data"
        if not self.is_default_hwsku(hwsku, setup_name):
            switch_config_ini_path = f'/usr/share/sonic/device/{platform}/{hwsku}/{SonicConst.PORT_CONFIG_INI}'
            cmd = f"sonic-cfggen -k {hwsku} -H -j /etc/sonic/init_cfg.json -p {switch_config_ini_path} --print-data"
        init_config_db = self.engine.run_cmd(cmd, print_output=False)
        init_config_db_json = json.loads(init_config_db)
        return init_config_db_json

    def load_sku_init_conf(self, sku):
        self.engine.run_cmd(f"sonic-cfggen -k {sku} -H -j /etc/sonic/init_cfg.json --print-data  > /tmp/config_db.json")
        self.engine.run_cmd('sudo mv /tmp/config_db.json {}'.format(SonicConst.CONFIG_DB_JSON_PATH))
        sku_json = self.cli_obj.general.get_config_db()
        return sku_json

    def get_hwsku_json_as_dict(self, platform, hwsku):
        hwsku_path = f'/usr/share/sonic/device/{platform}/{hwsku}/hwsku.json'
        data = self.engine.run_cmd(f'cat {hwsku_path}')
        return json.loads(data)

    def update_breakout_cfg(self, topology_obj, init_config_db_json, config_db_json, hwsku):
        """
        This function updates the config_sb.json file with BREAKOUT_CFG section.
        In cases of systems where the consecutive port is unmapped after split of 4,
        the static split ports of 4, will not be include in the BREAKOUT_CFG.

        :param topology_obj: a topology object fixture
        :param init_config_db_json: a json object of the initial config_db.json file on the dut
        :param config_db_json: a json object of the config_db.json file on the dut
        :param hwsku:  hwsku of the dut  i.e, Mellanox-SN2700
        :return: the name of the updated config_db.json file with BREAKOUT_CFG section
        """
        breakout_cfg_dict = init_config_db_json.get("BREAKOUT_CFG")
        platform_json_obj = json_file_helper.get_platform_json(self.engine, self.cli_obj)
        parsed_platform_json_by_breakout_modes = self.parse_platform_json(topology_obj, platform_json_obj,
                                                                          parse_by_breakout_modes=True)
        split_ports_for_update = get_all_split_ports_parents(config_db_json, topology_obj)
        unsplit_ports_for_update = get_all_unsplit_ports(config_db_json, topology_obj)
        # TODO this is WA for virtual smartswitch, when last 4 ports connected to DPU and supports only 1x200G
        if hwsku == 'Mellanox-SN4700-O28':
            for i in range(1, 6):
                port = unsplit_ports_for_update[-i]
                parsed_platform_json_by_breakout_modes[port][1] = {'1x200G[100G,50G,40G,25G,10G,1G]'}
                config_db_json['PORT'][port]['speed'] = '200000'
                # TODO add condition when the name of virtual setup is know: "if not virtual_setup"
                # For CI/Build and regression on canonical setups. Last port connected to host with max 100G
                port = unsplit_ports_for_update[-1]
                parsed_platform_json_by_breakout_modes[port][1] = {'1x100G[50G,25G,10G,1G]'}
                config_db_json['PORT'][port]['speed'] = '100000'
        if hwsku not in NOT_SUPPORTED_DPB_SKU_LIST:
            self.update_breakout_mode_for_split_ports(split_ports_for_update, hwsku, breakout_cfg_dict,
                                                      config_db_json, parsed_platform_json_by_breakout_modes)
            self.update_breakout_mode_for_unsplit_ports(unsplit_ports_for_update, breakout_cfg_dict,
                                                        config_db_json, parsed_platform_json_by_breakout_modes)
        port_info_dict = config_db_json.get(ConfigDbJsonConst.PORT, [])
        breakout_cfg_dict = {port: breakout_mode for port, breakout_mode in breakout_cfg_dict.items()
                             if port in port_info_dict}
        config_db_json["BREAKOUT_CFG"] = breakout_cfg_dict

    def is_supported_split_mode(self, hwsku, split_num):
        split_supported_without_unmap = self.is_platform_supports_split_without_unmap(hwsku) or split_num == 2
        split_supported_on_sn5 = True
        if re.search("SN5[46]00", hwsku) and split_num == 8:
            split_supported_on_sn5 = False
        return split_supported_without_unmap and split_supported_on_sn5

    def update_breakout_mode_for_split_ports(self, split_ports_for_update, hwsku, breakout_cfg_dict,
                                             config_db_json, parsed_platform_json_by_breakout_modes):
        for port, split_num in split_ports_for_update:
            if self.is_supported_split_mode(hwsku, split_num):
                self.update_port_breakout_cfg_mode(breakout_cfg_dict, port, config_db_json, split_num,
                                                   parsed_platform_json_by_breakout_modes)

    def update_breakout_mode_for_unsplit_ports(self, unsplit_ports_for_update, breakout_cfg_dict,
                                               config_db_json, parsed_platform_json_by_breakout_modes):
        unsplit_ports_split_num = 1
        for port in unsplit_ports_for_update:
            self.update_port_breakout_cfg_mode(breakout_cfg_dict, port, config_db_json,
                                               unsplit_ports_split_num,
                                               parsed_platform_json_by_breakout_modes)

    @staticmethod
    def update_port_breakout_cfg_mode(breakout_cfg_dict, port, config_db_json,
                                      split_num, parsed_platform_json_by_breakout_modes):
        breakout_cfg_dict[port]["brkout_mode"] = get_port_current_breakout_mode(config_db_json, port, split_num,
                                                                                parsed_platform_json_by_breakout_modes)

    @staticmethod
    def create_extended_config_db_file(setup_name, config_db_json, file_name=SonicConst.EXTENDED_CONFIG_DB_PATH):
        new_config_db_json_path = str(os.path.join(InfraConst.MARS_TOPO_FOLDER_PATH,
                                                   setup_name,
                                                   file_name))
        if os.path.exists(new_config_db_json_path):
            os.remove(new_config_db_json_path)
        with open(new_config_db_json_path, 'w') as f:
            json.dump(config_db_json, f, indent=4)
            f.write('\n')
        os.chmod(new_config_db_json_path, 0o777)
        return file_name

    def update_dhclient_lease_time(self):
        dhclient_lease_time = 'send dhcp-lease-time 7200;'
        dhclient_conf_file = '/usr/share/sonic/templates/dhclient.conf.j2'
        update_dhclient_lease_time_cmd = f"""
        if grep -q dhcp-lease-time {dhclient_conf_file}; then
            sudo sed -i 's/^send dhcp-lease-time.*/{dhclient_lease_time}/g' {dhclient_conf_file}
        else
            sudo sed -i '/^send host-name = gethostname();/a {dhclient_lease_time}' {dhclient_conf_file}
        fi
        """
        self.engine.run_cmd(update_dhclient_lease_time_cmd, validate=True)

    def install_wjh(self, wjh_deb_url):
        wjh_package_local_name = '/home/admin/wjh.deb'
        self.engine.run_cmd('sudo curl {} -o {}'.format(wjh_deb_url, wjh_package_local_name))
        self.engine.run_cmd('sudo dpkg -i {}'.format(wjh_package_local_name), validate=True)
        logger.info('Sleep {} after what-just-happened installation'.format(InfraConst.SLEEP_AFTER_WJH_INSTALLATION))
        time.sleep(InfraConst.SLEEP_AFTER_WJH_INSTALLATION)
        self.set_feature_state('what-just-happened', 'enabled')
        self.save_configuration()
        self.engine.run_cmd('sudo rm -f {}'.format(wjh_package_local_name))
        retry_call(self._verify_dockers_are_up,
                   fargs=[[AppExtensionInstallationConstants.WJH_APP_NAME]],
                   tries=24,
                   delay=10,
                   logger=logger)

    def get_warm_reboot_status(self):
        return self.engine.run_cmd('systemctl is-active warmboot-finalizer')

    def check_warm_reboot_status(self, expected_status):
        warm_reboot_status = self.get_warm_reboot_status()
        if expected_status not in warm_reboot_status:
            raise Exception('warm-reboot status "{}" not as expected "{}"'.format(warm_reboot_status, expected_status))

    def get_config_db(self):
        config_db_json = self.engine.run_cmd('cat {} ; echo'.format(SonicConst.CONFIG_DB_JSON_PATH), print_output=False)
        return json.loads(config_db_json)

    def wait_for_frr_daemons_ready(self, tries=10, delay=3, timeout=30):
        """
        Wait for FRR daemons (zebra) to be ready to accept vtysh connections.
        BGP container being up does not guarantee FRR daemons are initialized.
        Raises the last exception if FRR daemons are not ready after all retry attempts.
        :param tries: Number of retry attempts
        :param delay: Delay between retries in seconds
        :param timeout: Timeout in seconds for each vtysh command
        """
        retry_call(
            self.engine.run_cmd,
            fargs=['sudo vtysh -c "show zebra"'],
            fkwargs={'validate': True, 'print_output': False, 'timeout': timeout},
            tries=tries,
            delay=delay,
            logger=logger
        )

    def get_config_db_from_running_config(self):
        """
        Get the running configuration from the DUT.
        show runningconfiguration all uses vtysh internally, which may fail if FRR daemons
        are not yet ready (e.g. after config reload/reboot). Retry on FRR connection errors.
        """
        config = self.engine.run_cmd('sudo show runningconfiguration all', print_output=False)
        try:
            return json.loads(config)
        except (json.JSONDecodeError, ValueError):
            if 'failed to connect to any daemons' in config:
                logger.warning('FRR daemons not ready, waiting for FRR to initialize before retrying')
            else:
                logger.warning('Running config returned non-JSON output, retrying after FRR readiness check: %s',
                               config[:200])
            self.wait_for_frr_daemons_ready()
            config = self.engine.run_cmd('sudo show runningconfiguration all', print_output=False)
            return json.loads(config)

    def is_spc1(self):
        """
        Function to check if the current DUT is SPC1
        """
        # if sn2 in platform, it's spc1. e.g. x86_64-mlnx_msn2700-r0
        return self.check_is_platform(['sn2'])

    def is_spc2(self):
        """
        Function to check if the current DUT is SPC2
        """
        # if sn3 in platform, it's spc1. e.g. x86_64-mlnx_msn3800-r0
        return self.check_is_platform(['sn3'])

    def is_spc3(self):
        """
        Function to check if the current DUT is SPC3
        """
        # if sn4 in platform, it's spc3. e.g. x86_64-mlnx_msn4700-r0
        return self.check_is_platform(['sn4'])

    def is_spc4_or_above(self):
        """
        Function to check if the current DUT is SPC4 or above
        """
        platform = self.cli_obj.chassis.get_platform()

        match = re.search(r'sn(\d)', platform, re.IGNORECASE)
        if not match:
            raise ValueError(f"Cannot get platform: '{platform}'")

        chip_gen = int(match.group(1)) - 1
        return chip_gen >= 4

    def is_simx_moose(self):
        if self._is_simx_moose is None:
            self._is_simx_moose = self.check_is_platform(['sn5600', 'simx'])
        return self._is_simx_moose

    def is_bison(self):
        if self._is_bison is None:
            self._is_bison = self.check_is_platform(['sn5640'])
        return self._is_bison

    def check_is_platform(self, exp_condition_list, platform=None):
        """
        Checks if the platform as an expected condition list.
        :param exp_condition_list: platform expected conditions. Example: ['sn5640', 'simx']
        :param platform: system platform. Example: 'x86_64-nvidia_sn5640_simx-r0'
        """
        if platform is None:
            platform = self.cli_obj.chassis.get_platform()
        return all(condition in platform for condition in exp_condition_list)

    def is_asan_image(self, validate=False):
        asan_val_from_sonic_ver_cmd = "sonic-cfggen -y /etc/sonic/sonic_version.yml -v asan"
        asan_val = self.engine.run_cmd(asan_val_from_sonic_ver_cmd, validate=validate)
        is_asan = False
        if "yes" in asan_val:
            logger.info("The current sonic image is a ASAN image")
            is_asan = True
        return is_asan

    def show_version(self, validate=False):
        return self.engine.run_cmd('show version', validate=validate)

    def parse_platform_json(self, topology_obj, platform_json_obj, parse_by_breakout_modes=False):
        """
        parsing platform breakout options and config_db.json breakout configuration.
        :param topology_obj: topology object fixture
        :param platform_json_obj: a json object of platform.json file
        :param parse_by_breakout_modes: If true the function will return a dictionary with
        available breakout options by split number for all dut ports
        :return: a dictionary with available speeds option/ breakout options for each split number on all dut ports
        i.e, parse_by_breakout_modes = FALSE

           { 'Ethernet0' :{'1': [200G,100G,50G,40G,25G,10G,1G],
                           '2': [100G, 50G,40G,25G,10G,1G],
                           '4': [50G,40G,25G,10G,1G]},..}

        OR Iin case of: parse_by_breakout_modes = TRUE

           { 'Ethernet0' :    {1:{'1x100G[50G,40G,25G,10G]'},
                               2: {'2x50G[40G,25G,10G]'},
                               4: {'4x25G[10G]'},...}

        """
        ports_speeds_by_modes_info = {}
        breakout_options = SonicConst.BREAKOUT_MODES_REGEX
        if not platform_json_obj.get("interfaces"):
            ports_speeds_by_modes_info = self.generate_mock_ports_speeds(topology_obj, parse_by_breakout_modes)
        else:
            for port_name, port_dict in platform_json_obj["interfaces"].items():
                port_start_index = int(re.search(r'Ethernet(.*)', port_name).group(1))
                lanes = port_dict[SonicConstant.LANES].split(",")
                breakout_modes = re.findall(breakout_options, ",".join(list(port_dict[SonicConstant.BREAKOUT_MODES].
                                                                            keys())))
                lane_count = len(lanes)
                breakout_ports = ["Ethernet{}".format(port_start_index + i) for i in range(lane_count)]
                for port in breakout_ports:
                    if port in topology_obj.players_all_ports[self.dut_alias]:
                        if parse_by_breakout_modes:
                            ports_speeds_by_modes_info[port] = get_split_mode_supported_breakout_modes(breakout_modes)
                        else:
                            ports_speeds_by_modes_info[port] = get_split_mode_supported_speeds(breakout_modes)
        return ports_speeds_by_modes_info

    @staticmethod
    def generate_mock_ports_speeds(topology_obj, parse_by_breakout_modes=False):
        if parse_by_breakout_modes:
            raise AssertionError("This version doesn't support platform.json,\n"
                                 "there no mock option for interfaces breakout mode option")
        else:
            mock_ports_speeds_by_modes_info = {}
            port_list = get_dut_default_ports_list(topology_obj)
            for port in port_list:
                mock_ports_speeds_by_modes_info[port] = {
                    1: ['100G', '50G', '25G', '10G'],
                    2: ['50G', '25G', '10G'],
                    4: ['25G', '10G']
                }
            logger.debug("Mock ports speed option dictionary: {}".format(mock_ports_speeds_by_modes_info))
            return mock_ports_speeds_by_modes_info

    def show_warm_restart_state(self):
        """
        Show warm_restart_state
        Example:
            name             restore_count  state
            -------------  ---------------  ----------------------
            warm-shutdown                0  pre-shutdown-succeeded
            vlanmgrd                     4  reconciled
            cpu-report                   1  reconciled
            vrfmgrd                      4  reconciled
            syncd                        4
            neighsyncd                   4  reconciled
        return: warm_restart stat dict like below, or raise exception
            { "warm-shutdown": {"name":"warm-shutdown", "restore_count":"0", "state": "pre-shutdown-succeeded"},
              "vlanmgrd": {"name":"vlanmgrd", "restore_count":"4", "state": "reconciled"},
              "cpu-report": {"name":"cpu-report", "restore_count":"1", "state": "reconciled"},
              "vrfmgrd": {"name":"vrfmgrd", "restore_count":"4", "state": "reconciled"},
              "syncd": {"name":"syncd", "restore_count":"4", "state": ""},
              "neighsyncd", {"name":"neighsyncd", "restore_count":"4", "state": "reconciled"}
            }
        """
        warm_restart_state = self.engine.run_cmd("show warm_restart state")
        warm_restart_state_dict = generic_sonic_output_parser(warm_restart_state,
                                                              headers_ofset=0,
                                                              len_ofset=1,
                                                              data_ofset_from_start=2,
                                                              data_ofset_from_end=None,
                                                              column_ofset=2,
                                                              output_key='name')
        return warm_restart_state_dict

    def get_base_and_target_images(self):
        """
        This method getting base and target image from "sonic-installer list" output
        """
        installed_list_output = self.get_sonic_image_list()
        target_image = re.search(r'Current:\s(.*)', installed_list_output, re.IGNORECASE).group(1)
        try:
            available_images = re.search(r'Available:\s\n(.*)\n(.*)', installed_list_output, re.IGNORECASE)
            available_image_1 = available_images.group(1)
            available_image_2 = available_images.group(2)
            if target_image == available_image_1:
                base_image = available_image_2
            else:
                base_image = available_image_1
        except Exception:
            logger.warning('Only 1 installed image available')
            base_image = None

        return base_image, target_image

    def verify_installed_extensions_running(self):
        """
        Verify installed mellanox app_extension to image exist in docker ps output
        :return: None if successful, otherwise Exception
        """
        if self.cli_obj.app_ext.verify_version_support_app_ext():
            installed_mellanox_ext = get_installed_mellanox_extensions(self.cli_obj)
            if installed_mellanox_ext:
                retry_call(self._verify_dockers_are_up,
                           fargs=[installed_mellanox_ext],
                           tries=36,
                           delay=10,
                           logger=logger)

    def if_other_credentials_used_set_boot_order_onie(self):
        engine = self.get_sonic_engine_try_different_passwords()
        if engine:
            with allure.step("Other credentials used then default"):
                logger.info('Other credentials used then default')
                with allure.step('Setting boot order to onie'):
                    self.set_next_boot_entry_to_onie()
                    self.engine = engine
                with allure.step('Rebooting the switch'):
                    engine.reload(['sudo reboot'], wait_after_ping=25, ssh_after_reload=False)

    def get_sonic_engine_try_different_passwords(self):
        for password in DefaultCredentialConstants.OTHER_SONIC_PASSWORD_LIST:
            engine = LinuxSshEngine(
                self.engine.ip, username=DefaultCredentialConstants.OTHER_SONIC_USER, password=password)
            if self.is_dummy_command_succeed():
                return engine

    def enable_info_logging_on_swss(self, platform_params=None):
        """
        Enable INFO logging on swss container(s) using swssloglevel
        :param platform_params: Platform parameters for multi-ASIC detection
        """
        logger.info(f"Starting enable_info_logging_on_swss")

        # Multi-ASIC specific logic
        if SonicInstallationSteps.is_multi_asic_platform(platform_params=platform_params):
            asic_count = validate_and_get_asic_count(platform_params)
            logger.info(f"MULTI-ASIC platform detected: platform={platform_params.get('platform')}, asic_count={asic_count}")
            logger.info(f"Enabling INFO logging on {asic_count} swss containers")

            for asic_id in range(asic_count):
                logger.info(f"[ASIC {asic_id}/{asic_count - 1}] Enabling INFO logging on swss{asic_id}")
                try:
                    self.engine.run_cmd(f"swssloglevel -n {asic_id} -l INFO -a")
                    logger.info(f"[ASIC {asic_id}/{asic_count - 1}] Successfully enabled INFO logging on swss{asic_id}")
                except Exception as e:
                    logger.error(f"[ASIC {asic_id}/{asic_count - 1}] Failed to enable INFO logging on swss{asic_id}: {e}")
                    raise

            logger.info(f"✓ Successfully enabled INFO logging on all {asic_count} swss containers")
            return
        else:
            logger.info(f"Enabling INFO logging on swss container")
            self.engine.run_cmd(f"swssloglevel -l INFO -a")
            logger.info(f"✓ Successfully enabled INFO logging on swss container")

    def restart_service(self, service_name):
        self.engine.run_cmd(f'sudo service {service_name} restart')

    def prepare_nvidia_air_basic_config_db_json(self, topology_obj, setup_name, hwsku, platform):
        config_db_dict = self.get_init_config_db_json_obj(hwsku, platform, setup_name)
        dut_name = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Common']['Name']
        config_db_dict['DEVICE_METADATA']['localhost']['hostname'] = dut_name
        for interface in config_db_dict['PORT']:
            config_db_dict['PORT'][interface]['admin_status'] = 'up'
        ips_dict = get_dhcp_ips_dict(setup_name)
        gw_ip = ips_dict[HostsConstants.OOB_MGMT_SERVER]
        dut_ip = ips_dict[HostsConstants.DUT]
        config_db_dict['MGMT_INTERFACE'] = {f'eth0|{dut_ip}/24': {'gwaddr': gw_ip}}
        return config_db_dict

    def get_simx_version_and_chip_type(self):
        """
        This method is to get the simx version and chip type
        :return: version, chip_type
        """
        reg_simx_version = ".*Vendor specific: SimX version (?P<version>.*)"
        reg_simx_chip_type = ".*Vendor specific: SimX chip type: (?P<chip_type>.*)"

        simx_info = self.engine.run_cmd('sudo lspci -vvv | grep SimX')
        '''
        simx info like below:
        Product Name: SimX: Spectrum simulation
                        [V1] Vendor specific: SimX version 5.1.1057
                        [V2] Vendor specific: SimX chip type: Spectrum-3
        '''

        chip_type, version = '', ''

        for line in simx_info.split("\n"):
            match_version = re.match(reg_simx_version, line)
            if match_version:
                version = match_version.groupdict()["version"].strip()
            else:
                match_chip_type = re.match(reg_simx_chip_type, line)
                if match_chip_type:
                    chip_type = match_chip_type.groupdict()["chip_type"].strip()

        logger.info(f'Simx version:{version}, simx chip type:{chip_type}')
        return version, chip_type

    def get_sai_version(self):
        """
        This method is to get the sai version
        :return: sai_version
        """
        reg_sai_version = r".*mlnx.SAIBuild(?P<version>[\d.]+)\s+.*"

        sai_info = self.engine.run_cmd('docker exec syncd bash -c "dpkg -l | grep mlnx-sai"')
        '''
        sai info like below:
        ii  mlnx-sai     1.mlnx.SAIBuild2211.23.1.0     amd64        contains SAI implementation for Mellanox hardware
        '''
        sai_version = None
        sai_res = re.match(reg_sai_version, sai_info)
        if sai_res:
            sai_version = sai_res.groupdict()["version"].strip()

        logger.info(f'sai version:{sai_version}')
        return sai_version

    def get_secureboot_status(self):
        """
        This method is to get the output of command 'mokutil --sb-state'
        :return: output
        """
        return self.engine.run_cmd('mokutil --sb-state')

    def is_async_route_enabled(self, async_route_param):
        """
        Checks the status of async_route feature

        :param str async_route_param: async_route feature parameter from sai.profile, f.e. 'SAI_ASYNC_ROUTING_ENABLED=1'
        :return bool: True if feature is enabled, False otherwise
        """
        async_route_status = async_route_param.split('=')[-1]
        return async_route_status == '1'

    def enable_async_route_feature(self, platform, hwsku):
        """
        Checks if async route feature is enabled and enables it otherwise
        """
        switch_sai_xml_path = f'/usr/share/sonic/device/{platform}/{hwsku}'
        default_sai_xml_file_name = 'sai.profile'
        async_routing_enabled = 'SAI_ASYNC_ROUTING_ENABLED'

        logger.info('Get SAI init config file path')
        sai_profile_path = f'{switch_sai_xml_path}/{default_sai_xml_file_name}'
        async_route_param = self.engine.run_cmd(f'sudo cat {sai_profile_path} | grep {async_routing_enabled}')
        if async_route_param and not self.is_async_route_enabled(async_route_param):
            if self.is_async_route_enabled(async_route_param):
                logger.info('Async_route feature is already enabled')
            else:
                self.engine.run_cmd(f'sed -i "/{async_routing_enabled}/d" {sai_profile_path}')
                self.engine.run_cmd(f'echo "{async_routing_enabled}=1" >> {sai_profile_path}')

    @staticmethod
    def config_health_event(dut_engine, severity, category_list=HealthEventConst.CATEGORY_NONE,
                            max_events=HealthEventConst.MAX_EVENTS_NUM_DEFAULT):
        """
        Config health event.

        Parameters:
        - severity (str): Required, supported values are "fatal", "warning", "notice".
        - category_list (str): Optional, supported values are "none", "all", "software", "asic_hw", "cpu_hw", "firmware".
        - max_events (int): Optional, supported values are greater than or equal to 0 integers.

        Example command:
        sudo config asic-sdk-health-event suppress fatal --category-list firmware,software --max-events 10
        """

        logger.info(
            f"Config with parameter: severity={severity}, category_list={category_list}, max_events={max_events}")

        # Check if the "severity" parameter is valid
        valid_severities = ["fatal", "warning", "notice"]
        if severity not in valid_severities:
            raise ValueError(f"Invalid 'severity' parameter: {severity}. Expected one of {valid_severities}.")

        # Ensure at least one of category-list or max-events is provided
        if not category_list and not max_events:
            raise ValueError("Invalid command. Please include at least a category-list or max-events parameter.")

        command = f"sudo config asic-sdk-health-event suppress {severity}"

        # Process the "category-list" parameter
        if category_list is not None:
            valid_categories = ["none", "all", "software", "asic_hw", "cpu_hw", "firmware"]
            categories = category_list.split(',')
            for category in categories:
                if category not in valid_categories:
                    raise ValueError(
                        f"Invalid 'category-list' parameter: {category}. Expected one of {valid_categories}.")
            command += f" --category-list {category_list}"

        # Process the "max-events" parameter
        if max_events is not None:
            if not isinstance(max_events, int) or max_events < 0:
                raise ValueError(f"Invalid 'max-events' parameter: {max_events}. Expected a non-negative integer.")
            command += f" --max-events {max_events}"

        logger.info(f"Health event config command is: {command}")
        dut_engine.run_cmd(command)
        get_health_event_config(dut_engine)

    def add_ntp_server(self, server_ip):
        """
        This method add a ntp server on the dut
        :return: command output
        """
        return self.engine.run_cmd(f'sudo config ntp add {server_ip}')

    def change_default_grub_timeout(self, timeout):
        cmd = "sudo sed -i 's/set timeout=5/set timeout={}/' /host/grub/grub.cfg".format(timeout)
        self.engine.run_cmd(cmd)

    def delete_redis_table(self, table_name):
        """
        This method deletes the redis table from redis db
        :param table_name: name of table to delete
        """
        table_keys = self.engine.run_cmd(f'sudo sonic-db-cli CONFIG_DB keys "{table_name}|*"').split("\n")
        self.delete_redis_key_from_table(table_keys)

    def delete_redis_key_from_table(self, table_keys):
        """
        This method deletes a list of redis keys from a redis table in the redis db
        :param table_keys: list of table_keys to delete
        """
        for key in table_keys:
            self.engine.run_cmd(f'sudo sonic-db-cli CONFIG_DB DEL "{key}"')

    def delete_redis_sub_keys_from_table(self, changed_keys_mapping):
        """
        This method deletes from each key in the mapping, the list of redis sub-keys specified
        :param changed_keys_mapping: A dictionary mapping between keys in the redis (i.e. SFLOW|global for 'global' key
        of 'SFLOW' table) to sub-keys for deletion (i.e. sub_key 'polling_interval' of 'SFLOW|global')
        """
        for key, sub_keys in changed_keys_mapping.items():
            for sub_key in sub_keys:
                self.engine.run_cmd(f'sudo sonic-db-cli CONFIG_DB HDEL "{key}" "{sub_key}"')

    def install_traffic_generator(self, latest_version=False, cli_type=CliType.SONIC):
        """
        Function verifies the traffic generator is functional post deploy on SONiC OS

        Function first unpack sdk verification git on top of switch
        install necessary packages for sdk verification git to be functional
        then, run test to verify  sdk verification git works correctly and
        is running traffic generation script.
        :return: None
        """
        with allure.step('Get SDK_VER git'):
            sdk_version, sdk_branch = self.resolve_sdk_version_and_branch_for_traffic_generator(
                latest_version=latest_version)
            sdk_deb_path = self.resolve_sdk_git_deb_path(sdk_version, cli_type=cli_type)
            logger.info(f"Resolved SDK package for traffic generator installation: '{sdk_deb_path}'")
            docker_exec_syncd_cmd = InfraConst.DOCKER_EXEC_BASH_CMD.format(DOCKER=InfraConst.SYNCD_DOCKER)
            sdk_deb_dir = os.path.dirname(sdk_deb_path)
            sdk_deb_file = os.path.basename(sdk_deb_path)
            sdk_dev_deb_file = sdk_deb_file.replace('sys-sdk-git', 'sys-sdk').replace('_amd64.deb', '_amd64-dev.deb')
            copy_files_to_syncd(self.engine, [sdk_deb_file, sdk_dev_deb_file], sdk_deb_dir)
            self.engine.run_cmd(f"{docker_exec_syncd_cmd} 'dpkg -i {sdk_deb_file}'")
            self.engine.run_cmd(f"{docker_exec_syncd_cmd} 'dpkg -i {sdk_dev_deb_file}'")

        with allure.step('apt get'):
            self.engine.run_cmd(f"{docker_exec_syncd_cmd} 'chmod 1777 /tmp'", validate=True)
            self.engine.run_cmd(f"{docker_exec_syncd_cmd} 'apt-get update'")
            self.engine.run_cmd(f"{docker_exec_syncd_cmd} 'apt-get install -y build-essential python3-dev swig'")
            self.engine.run_cmd(f"{docker_exec_syncd_cmd} 'apt-get install -y dmidecode kmod pciutils git rsync lm-sensors'")
            self.engine.run_cmd(f"{docker_exec_syncd_cmd} 'touch /var/log/syslog'")

        with allure.step('pip dependencies'):
            copy_files_to_syncd(self.engine, [PerfConsts.REQUIRMENTS_FILE],
                                PerfConsts.REQUIRMENTS_DIR)
            self.engine.run_cmd(
                f"{docker_exec_syncd_cmd} 'python3 -m pip config set global.index-url "
                f"{PerfConsts.SYNCD_PIP_INDEX_URL}'")
            self.engine.run_cmd(
                f"{docker_exec_syncd_cmd} 'pip install -r {PerfConsts.REQUIRMENTS_FILE} "
                f"--break-system-packages --root-user-action=ignore'")

        with allure.step('Prepare SDK_VER git to run tests'):
            self.overlay_perf_sys_sdk_to_sys_sdk(sdk_branch, is_in_syncd=True)
            self.engine.run_cmd(f"{docker_exec_syncd_cmd} '{PerfConsts.EXPORT_PYTHONPATH} "
                                f"&& {PerfConsts.DVS_RUN_TEST_PATH} -si'")

    def get_sdk_version(self, docker_name=InfraConst.SYNCD_DOCKER):
        sdk_version_output = self.engine.run_cmd(InfraConst.CMD_GET_SDK_VERSION_FROM_DOCKER.format(DOCKER=docker_name),
                                                 validate=True)
        sdk_version = re.search(r"SX-SDK ETH (\d+\.\d+\.\d+)", sdk_version_output).group(1)
        return sdk_version

    def startup_dpus(self, dpu_index_list):
        for dpu_index in dpu_index_list:
            self.set_dpu_state_with_retry(dpu_index, True)

    def shutdown_dpus(self, dpu_index_list):
        for dpu_index in dpu_index_list:
            self.set_dpu_state_with_retry(dpu_index, False)

    @retry(Exception, tries=36, delay=10)
    def set_dpu_state_with_retry(self, dpu_index, state: bool):
        if state:
            action = 'startup'
        else:
            action = 'shutdown'
        output = self.engine.run_cmd(f'sudo config chassis modules {action} DPU{dpu_index}', validate=True, timeout=3)
        if "state transition is already in progress" in output:
            raise Exception(f"DPU{dpu_index} state transition is already in progress")

    def shutdown_dpu_data_interfaces(self, setup_name):
        if 'bobcat' in setup_name:
            self.engine.run_cmd(f'sudo config interface shutdown Ethernet224-248')

    def get_dpus_status(self):
        dpu_status = self.engine.run_cmd(f'show chassis module status')
        return generic_sonic_output_parser(dpu_status, output_key="Name")

    @retry(Exception, tries=90, delay=3)
    def verify_dpus_down(self, dpu_index_list):
        """
        Verifying the Oper-Status are Offline and Admin-Status are down for the specified DPUs
        """
        with allure.step('Check that DPUS in DOWN state'):
            dpu_status = self.get_dpus_status()
            for dpu_index in dpu_index_list:
                dpu_name = f"DPU{dpu_index}"
                assert dpu_status[dpu_name]['Oper-Status'] == 'Offline' and dpu_status[dpu_name]['Admin-Status'] == 'down', \
                    f'For {dpu_name}, dpu status is {dpu_status[dpu_name]} '
        logger.info("all dpus:{dpu_index_list} are down")

    # retry for ~200s if the dpus are up
    @retry(Exception, tries=51, delay=5)
    def verify_dpus_up(self, dpu_index_list):
        """
        Verifying the Oper-Status are Online and Admin-Status are up for the specified DPUs
        """
        with allure.step('Check that DPUS in UP state'):
            dpu_status = self.get_dpus_status()
            for dpu_index in dpu_index_list:
                dpu_name = f"DPU{dpu_index}"
                assert dpu_status[dpu_name]['Oper-Status'] == 'Online' and dpu_status[dpu_name]['Admin-Status'] == 'up', \
                    f'For {dpu_name}, dpu status is {dpu_status[dpu_name]} '
        logger.info(f"all dpus:{dpu_index_list} are up")

    # retry for ~375s if the dpus are up
    @retry(Exception, tries=75, delay=5)
    def verify_dpus_status_aligned(self, dpu_name_list, valid_aligned_statuses={('up', 'Online'), ('down', 'Offline')}):
        """
        For all DPUs
        Verifying all Admin-Status are the same either up or down
        and the Oper-Status (Online or Offline) matches the Admin-Status (up or down)
        """
        # accepted admin and oper statuses combinations
        dpu_statuses = self.get_dpus_status()
        dpu_statuses_set = {(dpu_statuses[dpu_name]['Admin-Status'], dpu_statuses[dpu_name]['Oper-Status']) for dpu_name in dpu_name_list}
        assert len(dpu_statuses_set) == 1, f"DPUs statuses are not aligned: {dpu_statuses} for dpus: {dpu_name_list}"
        assert list(dpu_statuses_set)[0] in valid_aligned_statuses, f"DPUs status is not valid: {dpu_statuses} for dpus: {dpu_name_list}"
        logger.info(f"{len(dpu_name_list)} available dpus are aligned in admin status and oper status")

    def is_dark_mode(self):
        """
        Decide whether the SmartSwitch is running in dark mode based on the DPU admin status
        """
        chassis_module_status = self.engine.run_cmd('show chassis module status')
        dpu_count = len(re.findall(r'DPU\d+', chassis_module_status))
        admin_down_dpu_count = len(re.findall(r'DPU\d+.*down', chassis_module_status))
        return admin_down_dpu_count == dpu_count

    def verify_dpu_boot_progress(self, dpu_index_list, bad_states):
        """
        Parses the output of the command 'dpuctl dpu-status' and verifies the boot progress of the specified DPUs,
        Output example:
        DPU    dpu ready    dpu shutdown ready    boot progress      Force Power Required
        -----  -----------  --------------------  -----------------  ----------------------
        dpu0   True         False                 5 - OS is running  False
        dpu1   True         False                 5 - OS is running  False
        dpu2   True         False                 5 - OS is running  False
        dpu3   True         False                 5 - OS is running  False
        Verifying the boot progress of the specified DPUs, if it is not in the bad_states
        otherwise raise an exception
        """
        with allure.step('Check the boot progress of the DPUs'):
            dpu_ctl_output = self.engine.run_cmd('dpuctl dpu-status')
            dpu_ctl_parsed = generic_sonic_output_parser(dpu_ctl_output, output_key="DPU")
            for dpu_index in dpu_index_list:
                dpu_name = f"dpu{dpu_index}"
                dpu_boot_progress = dpu_ctl_parsed[dpu_name]['boot progress']
                dpu_boot_progress_code = int(dpu_boot_progress.split()[0])
                assert dpu_boot_progress_code not in bad_states, \
                    f'For {dpu_name}, dpu boot progress is "{dpu_boot_progress}"'
                logger.info(f"all dpus:{dpu_index_list} are ready for image install")

    def pre_installation_steps(self, context, threads_dict):
        """Execute SONiC pre-installation steps - base implementation"""
        SonicInstallationSteps.pre_installation_steps(
            context.sonic_topo, context.neighbor_type, context.base_version, context.target_version,
            context.setup_info, context.port_number, context.is_simx,
            threads_dict, context.destination_hwsku, context.is_performance,
            parallel=context.deploy_testbed_in_parallel, deploy_image_only=context.deploy_image_only,
            deploy_chipless=context.deploy_chipless,
            deploy_sequential=context.deploy_sequential
        )

    def post_installation_steps(self, context, image_helper=None):
        """Execute SONiC post-installation steps - base implementation"""
        last_dut = context.all_duts[-1] if context.all_duts else context.primary_dut
        SonicInstallationSteps.post_installation_steps(
            context.topology_obj, context.sonic_topo, context.recover_by_reboot, context.setup_name,
            context.platform_params, context.apply_base_config, context.target_version,
            context.is_shutdown_bgp, context.reboot_after_install, context.deploy_only_target,
            context.fw_pkg_path, context.reboot, context.additional_apps, context.setup_info,
            last_dut['dut_alias'], context.is_performance, context.chip_type, context.deploy_dpu, context.is_air,
            deploy_chipless=context.deploy_chipless
        )

        # SONiC-specific post-upgrade operations
        if context.base_version and context.target_version and not context.deploy_only_target:
            if not set_os_upgrade_flag():
                logger.warning("Failed to set the OS upgrade flag")

        # Clean reboot-cause history after upgrade (SONiC-specific)
        with allure.step('clean reboot-cause history after upgrade'):
            # see the fix for 202505 https://github.com/sonic-net/sonic-host-services/pull/293
            logger.info("Cleaning reboot-cause history to avoid timezone conflicts between versions")
            image_helper.cleanup_reboot_cause_history(context.topology_obj, context.setup_info)
        # Configure DNS for IPv6 setups
        self._configure_ipv6_dns_for_sonic_mgmt_container(context.setup_info)

    def dpu_post_installation_steps(self, context):
        """Execute SONiC post-installation steps - base implementation"""
        SonicInstallationSteps.dpu_post_installation_steps(
            context.topology_obj, context.sonic_topo, context.recover_by_reboot, context.setup_name, context.setup_info
        )

    def deploy_image_steps(self, topology_obj, setup_name, platform_params, image_url, deploy_type,
                           apply_base_config, reboot_after_install, is_shutdown_bgp, fw_pkg_path,
                           target_image_url='', destination_hwsku=None, setup_info=None, dut_alias=None,
                           fanout_deploy_threads=None, serial_log_analyzers=None, dut_ip='',
                           fanout_target_version=None):
        """Execute SONiC deploy image steps - base implementation"""
        SonicInstallationSteps.deploy_image(
            cli=self, topology_obj=topology_obj, setup_name=setup_name,
            platform_params=platform_params, image_url=image_url, deploy_type=deploy_type,
            apply_base_config=apply_base_config, reboot_after_install=reboot_after_install,
            is_shutdown_bgp=is_shutdown_bgp, fw_pkg_path=fw_pkg_path,
            destination_hwsku=destination_hwsku, setup_info=setup_info,
            dut_alias=dut_alias, fanout_deploy_threads=fanout_deploy_threads,
            fanout_target_version=fanout_target_version
        )

    def _configure_ipv6_dns_for_sonic_mgmt_container(self, setup_info):
        """
        Configure DNS server for IPv6 setups in sonic-mgmt container.
        Move the default docker dns server to the end of file /etc/resolv.conf
        for ipv6 setups, i.e. r-panther-02 and r-panther-42

        :param setup_info: Setup information containing DUT details
        """
        # Get DUT IP from setup_info
        dut_ip = setup_info['duts'][0]['dut_ip']
        if ":" in dut_ip or dut_ip.endswith(".v6lab"):
            logger.info(f"Configuring IPv6 DNS for sonic-mgmt container (DUT IP: {dut_ip})")
            default_docker_dns_server = 'nameserver 127.0.0.11'
            with open('/etc/resolv.conf', 'r') as f:
                lines = f.readlines()
            with open('/etc/resolv.conf', 'w') as f:
                for line in lines:
                    if default_docker_dns_server == line.strip():
                        continue
                    else:
                        f.write(line)
                f.write(default_docker_dns_server)
            logger.info("IPv6 DNS configuration completed for sonic-mgmt container")

    @staticmethod
    def get_swss_instances(dut_engine):
        """
        Get list of swss instance services for multi-ASIC systems
        :param dut_engine: SSH engine object
        :return: List of swss instance service names (e.g., ['swss@0.service', 'swss@1.service'])
        """
        logger.info('Getting list of swss instances')
        swss_instances = []
        try:
            result = dut_engine.run_cmd('systemctl list-units "swss@*" --all --no-legend')
            swss_instances = [
                line.split()[0] for line in result.splitlines()
                if line.strip() and 'swss@' in line and '@' in line.split()[0]
            ]
            logger.info(f'Found {len(swss_instances)} swss instances: {swss_instances}')
        except Exception as err:
            logger.error(f'Failed to list swss instances: {err}')
            raise
        return swss_instances

    @staticmethod
    def stop_swss_instance(dut_engine, instance_num=''):
        """
        Stop a swss instance service
        :param dut_engine: SSH engine object
        :param instance_num: Instance number (e.g., '0', '1', '2', '3') or empty string for single-ASIC (default: '')
        """
        service_name = f'swss@{instance_num}' if instance_num else 'swss'
        logger.info(f'Stopping {service_name} service')
        try:
            dut_engine.run_cmd(f'sudo config feature state {service_name} disabled')
            logger.info(f'Successfully stopped {service_name} service')
        except Exception as err:
            logger.error(f'Failed to stop {service_name} service: {err}')
            raise

    @staticmethod
    def remove_swss_container(dut_engine, instance_num=''):
        """
        Remove a swss instance container
        :param dut_engine: SSH engine object
        :param instance_num: Instance number (e.g., '0', '1', '2', '3') or empty string for single-ASIC (default: '')
        """
        container_name = f'swss{instance_num}'
        logger.info(f'Removing {container_name} container')
        try:
            dut_engine.run_cmd(f'sudo docker rm -f {container_name}')
            logger.info(f'Successfully removed {container_name} container')
        except Exception as err:
            logger.error(f'Failed to remove {container_name} container: {err}')
            raise

    @staticmethod
    def remove_asic_table_file(dut_engine):
        """
        Remove the asic_table.json file
        :param dut_engine: SSH engine object
        """
        logger.info('Removing /etc/sonic/asic_table.json')
        try:
            dut_engine.run_cmd('sudo rm -f /etc/sonic/asic_table.json')
            logger.info('Successfully removed /etc/sonic/asic_table.json')
        except Exception as err:
            logger.error(f'Failed to remove asic_table.json: {err}')
            raise

    @staticmethod
    @retry(Exception, tries=20, delay=5)
    def remove_asic_table(dut_engine):
        """
        Remove asic_table.json file to generate new one
        Auto-detects and handles both single-ASIC and multi-ASIC systems
        :param dut_engine: SSH engine object
        """
        logger.info("Starting asic_table removal")
        try:
            swss_instances = SonicGeneralCliDefault.get_swss_instances(dut_engine)
            if swss_instances:
                instance_nums = [inst.split('@')[1].split('.')[0] for inst in swss_instances]
            else:
                instance_nums = ['']
            logger.info(f'Found {len(instance_nums)} swss instance(s)')

            for instance_num in instance_nums:
                SonicGeneralCliDefault.stop_swss_instance(dut_engine, instance_num)

            for instance_num in instance_nums:
                SonicGeneralCliDefault.remove_swss_container(dut_engine, instance_num)

            SonicGeneralCliDefault.remove_asic_table_file(dut_engine)
            logger.info('Successfully completed asic_table removal')
        except Exception as err:
            logger.error(f'Failed to remove asic_table.json: {err}')
            raise


class SonicGeneralCli202012(SonicGeneralCliDefault):

    def __init__(self, engine, cli_obj, dut_alias):
        self.engine = engine
        self.cli_obj = cli_obj
        self.dut_alias = dut_alias

    def reload_flow(self, ports_list=None, topology_obj=None, reload_force=False):
        """
        Reloading switch and validate dockers and ports state
        :param ports_list: list of the ports to check status after reboot
        :param topology_obj: topology object
        :param reload_force: provide if want to do reload with -f flag(force)
        :return: None, raise error in case of unexpected result
        """
        if not (ports_list or topology_obj):
            raise Exception('ports_list or topology_obj must be passed to reload_flow method')
        if not ports_list:
            ports_list = topology_obj.players_all_ports[self.dut_alias]
        with allure.step('Reloading dut'):
            logger.info("Reloading dut")
            self.reload_configuration(reload_force)
            self.port_reload_reboot_checks(ports_list)

    def reload_configuration(self, force=False):
        if force:
            logger.warning('Force reload config not supported on branch 202012, using default cmd "config reload -y"')

        cmd = 'sudo config reload -y'
        self.engine.run_cmd(cmd, validate=True)
