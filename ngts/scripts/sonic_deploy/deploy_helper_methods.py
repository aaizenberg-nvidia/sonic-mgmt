import concurrent.futures
import copy
import logging
import os
import json
import re
import shutil
import time
import yaml

import allure
from retry.api import retry_call
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
try:
    from netmiko.ssh_exception import NetmikoAuthenticationException
except ImportError:
    from netmiko.exceptions import NetmikoAuthenticationException
from devts.infra.tools.topology_tools.nogaq import upload_data_to_noga, get_noga_resource_data
from devts.infra.tools.general_constants.constants import NogaConstants
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active

from ngts.constants.constants import (
    PlayersAliases, SonicDeployConstants, BmcDeployConstants, MarsConstants,
    SerialLoggerConst, CliType, SSHConsts,
)
from ngts.constants.performance_constants import PerfConsts, Cl_Consts
from ngts.scripts.sonic_deploy.image_preparetion_methods import get_real_paths, prepare_images
from ngts.tools.align_components.nogaq import CACHE_FILE_NAME as NOGA_CACHE_FILE
from ngts.helpers.general_helper import extract_host_details_from_topo_obj, get_cli_obj
from ngts.scripts.sonic_deploy.sonic_only_methods import is_community, SonicInstallationSteps
from ngts.scripts.sonic_deploy.community_only_methods import get_deploy_minigraph_cmd, execute_script
from ngts.nvos_tools.Devices.IbDevice import BlackMambaSwitch, CrocodileSwitch
from ngts.cli_wrappers.nvue.cumulus.cumulus_general_cli import CumulusGeneralCli
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.cli_wrappers.dvs.dvs_general_clis import DvsGeneralCli
from ngts.cli_wrappers.sonic.sonic_general_clis import SonicGeneralCliDefault
from ngts.cli_wrappers.sonic.sonic_ip_clis import SonicIpCli
from ngts.cli_wrappers.common.general_clis_common import GeneralCliCommon
from ngts.helpers.run_process_on_host import run_process_on_host, wait_until_background_procs_done
from ngts.common.util import download_file_to_dut, save_specified_installed_dpus, get_installed_dpu_info
from ngts.tools.infra import get_dumps_folder

logger = logging.getLogger()


class DeploymentContext:
    """
    Encapsulates all deployment parameters and derived values.

    This class replaces the complex parameter passing and initialization logic
    from the original test_deploy_and_upgrade function, providing a single
    source of truth for deployment configuration.
    """

    def __init__(self, topology_obj, is_simx, is_performance, base_version, base_version_dpu,
                 target_version, serve_files, sonic_topo, neighbor_type, deploy_only_target,
                 port_number, setup_name, platform_params, deploy_dpu, deploy_type,
                 apply_base_config, reboot_after_install, is_shutdown_bgp, fw_pkg_path,
                 recover_by_reboot, reboot, additional_apps, workspace_path, wjh_deb_url,
                 verify_secure_boot, chip_type, destination_hwsku, show_setup_versions,
                 serial_log_analyzers, fanout_target_version, request, is_air,
                 deploy_testbed_in_parallel=False, deploy_image_only=False, deploy_chipless=False,
                 deploy_sequential=False, base_version_bmc="", neighbor_mode=""):
        """
        Initialize DeploymentContext with all parameters.

        This constructor replaces the initialization logic from lines 102-123
        of the original test_deploy_and_upgrade function.
        """
        # Store all input parameters
        self.topology_obj = topology_obj
        self.is_simx = is_simx
        self.is_performance = is_performance
        self.base_version = base_version
        self.base_version_dpu = base_version_dpu
        self.target_version = target_version
        self.serve_files = serve_files
        self.sonic_topo = sonic_topo
        self.neighbor_type = neighbor_type
        self.deploy_only_target = deploy_only_target
        self.port_number = port_number
        self.setup_name = setup_name
        self.platform_params = platform_params
        self.deploy_dpu = deploy_dpu
        self.deploy_type = deploy_type
        self.apply_base_config = apply_base_config
        self.reboot_after_install = reboot_after_install
        self.is_shutdown_bgp = is_shutdown_bgp
        self.fw_pkg_path = fw_pkg_path
        self.recover_by_reboot = recover_by_reboot
        self.reboot = reboot
        self.additional_apps = additional_apps
        self.workspace_path = workspace_path
        self.wjh_deb_url = wjh_deb_url
        self.verify_secure_boot = verify_secure_boot
        self.chip_type = chip_type
        self.destination_hwsku = destination_hwsku
        self.show_setup_versions = show_setup_versions
        self.serial_log_analyzers = serial_log_analyzers
        self.fanout_target_version = fanout_target_version
        self.request = request
        self.is_air = is_air
        self.deploy_testbed_in_parallel = deploy_testbed_in_parallel
        self.deploy_image_only = deploy_image_only
        self.deploy_chipless = deploy_chipless
        self.deploy_sequential = deploy_sequential
        self.base_version_bmc = base_version_bmc
        self.neighbor_mode = neighbor_mode
        # True when a BMC image is provided and BMC installation should run
        self.deploy_bmc = bool(base_version_bmc)
        # True when a switch base/target image is provided and switch deployment should run
        self.deploy_switch = bool(base_version) or bool(target_version)
        # Initialize derived values (replaces lines 102-123 from original function)
        self._initialize()

    def _initialize(self):
        """
        Initialize derived values after the object is created.

        This method replaces the initialization logic from lines 102-123
        of the original test_deploy_and_upgrade function.
        """
        self._initialize_setup_info()
        self._initialize_hwsku()
        self._initialize_image_urls()
        self._validate_and_adjust_parameters()

    def _initialize_setup_info(self):
        """Initialize setup information from topology object."""
        with allure.step('preparations'):
            with allure.step('Initialize setup information'):
                logger.info("Deploy SONiC testing topology and upgrade switch")

            self.setup_info = DeployTopologyHelper.get_info_from_topology(self.topology_obj, self.workspace_path)
            self.setup_info['setup_name'] = self.setup_name

            # Create convenient attributes for commonly used data
            self.all_duts = self.setup_info['duts']
            self.primary_dut = self.setup_info['duts'][0]
            self.primary_cli_obj = self.setup_info['duts'][0]['cli_obj']
            self.all_cli_objs = [dut['cli_obj'] for dut in self.setup_info['duts']]

    def _initialize_hwsku(self):
        """Initialize hardware SKU configuration."""
        self.destination_hwsku = DeployTopologyHelper.get_hwsku(
            self.sonic_topo,
            self.destination_hwsku,
            self.setup_name
        )

    def _initialize_image_urls(self):
        """Prepare image versions and URLs."""
        # When no switch image is provided, skip the base/target image preparation
        # entirely. This avoids the exception raised by prepare_images() when both
        # --base-version and --target-version are empty (e.g. BMC-only runs).
        if not self.deploy_switch:
            self.base_version_url = ''
            self.target_version_url = ''
            self.image_urls = {'base_version': '', 'target_version': ''}
            return

        with allure.step('prepare versions paths/urls'):

            # Get real paths for base and target versions
            cli_type = self.setup_info["duts"][0]["cli_type"]
            self.base_version, self.target_version = get_real_paths(
                self.base_version,
                self.target_version,
                cli_type
            )

            # Prepare images for installation
            self.image_urls = DeployImageHelper.prepare_images_to_install(
                self.base_version,
                self.target_version,
                self.serve_files,
                cli_type,
                self.deploy_sequential
            )

            # Get base and target version URLs
            self.base_version_url = DeployImageHelper.get_base_version_url(
                self.deploy_only_target,
                self.image_urls
            )

            self.target_version_url = (
                '' if not self.target_version
                else DeployImageHelper.get_target_version_url(self.image_urls)
            )

    def _validate_and_adjust_parameters(self):
        """Validate parameters and make necessary adjustments."""
        if not self.deploy_switch and not self.deploy_bmc:
            logger.warning(
                'No image provided via "--base-version", "--target-version" or '
                '"--base-version-bmc"; deployment flow will be skipped.'
            )

        # Adjust apply_base_config for ptf-any topology
        if self.sonic_topo == 'ptf-any':
            self.apply_base_config = True

        # Validate and merge WJH and additional apps parameters
        if self.wjh_deb_url and self.additional_apps:
            raise Exception(
                'Arguments "wjh_deb_url" and "additional_apps" can not be used together'
            )

        if not self.additional_apps:
            self.additional_apps = self.wjh_deb_url

    @classmethod
    def from_function_params(cls, **kwargs):
        """
        Factory method to create DeploymentContext from function parameters.

        This method provides a clean way to create a DeploymentContext object
        from the original test_deploy_and_upgrade function parameters.

        Args:
            **kwargs: All the parameters from the original test_deploy_and_upgrade function

        Returns:
            DeploymentContext: Initialized deployment context
        """
        return cls(**kwargs)

    def get_dut_by_alias(self, dut_alias):
        """
        Get DUT information for a specific DUT alias.

        Args:
            dut_alias: Alias of the DUT (e.g., 'dut', 'dut-b', 'dut-c')

        Returns:
            Dict: DUT information dictionary

        Raises:
            ValueError: If DUT with specified alias is not found
        """
        for dut in self.all_duts:
            if dut['dut_alias'] == dut_alias:
                return dut
        raise ValueError(f"DUT with alias '{dut_alias}' not found")

    def get_cli_obj(self, dut):
        """
        Get CLI object from a DUT dictionary.

        Args:
            dut: DUT dictionary containing CLI object

        Returns:
            CLI object for the specified DUT
        """
        return dut['cli_obj']


class DeployImageHelper:
    """Handle all image-related operations"""

    @staticmethod
    def get_related_image_urls(base_version_url, target_version_url, dut, use_ga_image=False):
        """
        Get the appropriate image URLs for a specific DUT.

        Args:
            base_version_url: Base version URL
            target_version_url: Target version URL
            dut: DUT information dictionary
            use_ga_image: Whether to use GA image for traffic generators

        Returns:
            Tuple of (base_version_url, target_version_url)
        """
        return DeployImageHelper.get_related_image_to_switch(
            base_version_url,
            target_version_url,
            dut,
            use_ga_image
        )

    @staticmethod
    def get_related_image_to_switch(base_version, target_version, dut, use_GA_image):
        """production devices support only prod versions of ONIE and SONiC"""
        if dut['dut_alias'] == "dut":
            base_version, target_version = DeployImageHelper.get_image_for_dut(base_version, target_version, dut)
        elif dut['dut_alias'] in PerfConsts.TG_ALIAS_LIST:
            base_version, target_version = DeployMultiNosHelper.get_image_for_traffic_generators(base_version, target_version, dut, use_GA_image)
        return base_version, target_version

    @staticmethod
    def get_image_for_dut(base_version, target_version, dut):
        if dut['cli_type'] == CliType.NVUE:
            if target_version.startswith('http') and '/auto/' in target_version:
                target_version = '/auto/' + target_version.split('/auto/')[1]
        return base_version, target_version

    @staticmethod
    def prepare_images_to_install(base_version, target_version, serve_files, cli_type, deploy_sequential=False):
        """
        Prepare images to be installed
        :param base_version: base version argument
        :param target_version: target version argument
        :param serve_files: serve files
        :param cli_type: cli_type of the system
        :param deploy_sequential: deploy steps serially
        :return:
        """
        with allure.step('Prepare images and get base version url'):
            return prepare_images(base_version, target_version, serve_files, cli_type, deploy_sequential=deploy_sequential)

    @staticmethod
    def get_base_version_url(deploy_only_target, image_urls):
        """Get base version url"""
        with allure.step('Get base version url'):
            base_version_url = image_urls["base_version"]
            if deploy_only_target:
                if image_urls["target_version"]:
                    base_version_url = image_urls["target_version"]
                else:
                    raise Exception(
                        'Argument "target_version" must be provided when "deploy_only_target" flag is set to "yes".'
                        ' Please provide a target version.')
        return base_version_url

    @staticmethod
    def get_target_version_url(image_urls):
        """Get target version url"""
        with allure.step('Get target version url'):
            return DeployImageHelper.get_base_version_url(True, image_urls)

    @staticmethod
    def cleanup_reboot_cause_history(topology_obj, setup_info):
        """
        Clean reboot-cause history after SONiC upgrade to resolve timezone conflicts.
        This function removes old reboot-cause history files that may have inconsistent
        timezone formats between different SONiC versions (e.g., 202411 IDT vs 202505 UTC).

        :param topology_obj: topology object containing device connections
        :param setup_info: setup information containing DUT details
        """
        logger.info("Starting reboot-cause history cleanup after SONiC upgrade")

        for dut in setup_info['duts']:
            dut_name = dut['dut_name']
            dut_alias = dut['dut_alias']
            try:
                logger.info(f"Cleaning reboot-cause history on DUT: {dut_name}")
                engine = topology_obj.players[dut_alias]['engine']
                # Commands to clean up reboot-cause history
                cleanup_commands = [
                    # Backup existing history (optional - for safety)
                    'sudo mkdir -p /host/reboot-cause/backup',
                    'sudo cp -r /host/reboot-cause/history/* /host/reboot-cause/backup/ 2>/dev/null || true',
                    # Remove all old reboot-cause history files
                    'sudo rm -f /host/reboot-cause/history/reboot-cause-*.json',
                    # remove all reboot-cause history from the STATE_DB
                    'sonic-db-cli STATE_DB KEYS "REBOOT_CAUSE|*" | xargs -I {} sonic-db-cli STATE_DB DEL "{}" || true',
                    # Restart the process-reboot-cause service to reset state
                    'sudo systemctl restart process-reboot-cause.service || true',
                    # Verify cleanup
                    'ls -la /host/reboot-cause/history/ || true',
                    'sonic-db-cli STATE_DB KEYS "REBOOT_CAUSE|*" || true',
                ]
                for cmd in cleanup_commands:
                    try:
                        result = engine.run_cmd(cmd)
                        logger.debug(f"Command '{cmd}' executed on {dut_name}: {result}")
                    except Exception as e:
                        logger.warning(f"Non-critical error executing '{cmd}' on {dut_name}: {e}")
                logger.info(f"Reboot-cause history cleanup completed for DUT: {dut_name}")
            except Exception as e:
                logger.error(f"Failed to clean reboot-cause history on DUT {dut_name}: {e}")
                # Don't fail the entire upgrade process for history cleanup issues
                continue
        logger.info("Reboot-cause history cleanup completed for all DUTs")


class DeployTopologyHelper:
    """Handle topology and setup information"""

    @staticmethod
    def get_info_from_topology(topology_obj, workspace_path):
        """
        Creates a class which contains setup info
        :param topology_obj: topology object
        :param workspace_path: workspace_path argument
        :return: SetupInfo object
        """
        ansible_path = os.path.join(workspace_path, "sonic-mgmt/ansible/")
        setup_info = {'ansible_path': ansible_path, 'duts': [], 'fanouts': []}

        with allure.step("Create setup_info object"):
            for host in topology_obj.players:
                if host in PlayersAliases.duts_list:
                    cli_type, dut_alias, dut_ip, dut_name, engine, switch_type = extract_host_details_from_topo_obj(
                        topology_obj, host)
                    cli_obj = get_cli_obj(topology_obj, cli_type, switch_type, engine, host, dut_alias)
                    dut_info = {'dut_name': dut_name, 'cli_type': cli_type, 'engine': engine, 'cli_obj': cli_obj,
                                'dut_alias': dut_alias, 'switch_type': switch_type, 'dut_ip': dut_ip, 'cli': topology_obj.players[dut_alias]['cli']}
                    if dut_info['dut_alias'] == "dut":
                        setup_info['duts'].insert(0, dut_info)
                    else:
                        if 'dpu' not in dut_info['dut_alias']:
                            setup_info['duts'].append(dut_info)
                elif host == 'hypervisor':
                    hypervisor_name = topology_obj.players[host]['attributes'].noga_query_data['attributes']['Common']['Name']
                    hypervisor_ip = topology_obj.players[host]['attributes'].noga_query_data['attributes']['Specific']['ip']
                    hypervisor_info = {'hypervisor_name': hypervisor_name, 'hypervisor_ip': hypervisor_ip}
                    setup_info['hypervisor'] = hypervisor_info

        return setup_info

    @staticmethod
    def get_hwsku(sonic_topo, dest_hwsku, setup_name):
        if is_community(sonic_topo):
            base_path = os.path.dirname(os.path.realpath(__file__))
            default_hwsku_file_path = os.path.join(base_path, f"../../../{SonicDeployConstants.DEFAULT_HWSKU_FILE_PATH}")
            logger.info(f"Reading {SonicDeployConstants.DEFAULT_HWSKU_FILE_PATH}")
            with open(default_hwsku_file_path, 'r') as hwsku_json:
                hwsku_data = json.load(hwsku_json)
            if dest_hwsku:
                if dest_hwsku in hwsku_data[setup_name]['support_hwsku']:
                    return dest_hwsku
                else:
                    raise Exception(f"Un supported hwsku provided: {dest_hwsku}, "
                                    f"the supported hwsku: {hwsku_data[setup_name]['support_hwsku']}")

            else:
                logger.warning(f"No hwsku assigned, will use the default value: "
                               f"{hwsku_data[setup_name]['default_hwsku']}")
                return hwsku_data[setup_name]['default_hwsku']

    @staticmethod
    def customize_testbed_yaml_file(setup_info, sonic_topo, neighbor_mode):
        """
        Remove from testbed.yaml file all configurations, which not relevant to setup.
        And set neighbor mode if provided.
        This action will save us ~1.5 minutes of runtime in the first test,
         where need to get basic_facts in the first time.
        :param setup_info: setup_info dictionary
        :param sonic_topo: sonic topology information
        :param neighbor_mode: neighbor mode to use for the dual-tor deployment
        """

        # neighbor mode is only supported for dual-tor deployment
        is_dualtor = bool(sonic_topo) and 'dualtor' in sonic_topo
        if is_dualtor and neighbor_mode not in ['host-route', 'prefix-route', '']:
            raise ValueError(f"Invalid neighbor mode: {neighbor_mode}, valid values are: host-route, prefix-route, ''")

        dut_names = {dut['dut_name'] for dut in setup_info['duts']}
        testbed_yaml_file_path = os.path.join(os.path.dirname(__file__), "../../../ansible/testbed.yaml")
        testbed_yaml_backup_file_path = os.path.join(os.path.dirname(__file__), "../../../ansible/testbed.yaml.backup")
        # backup of original file
        shutil.copyfile(testbed_yaml_file_path, testbed_yaml_backup_file_path)
        # get current testbed.yaml data
        with open(testbed_yaml_file_path, 'r') as f:
            data = yaml.safe_load(f)
        # entry should include at least one on switch name
        customized_data = []
        for entry in data:
            entry_duts = entry.get('dut') or []
            if not any(device in dut_names for device in entry_duts):
                continue
            if is_dualtor and neighbor_mode:
                entry['neighbor_mode'] = neighbor_mode
            customized_data.append(entry)
        # store customized data
        with open(testbed_yaml_file_path, 'w') as out_file:
            yaml.dump(customized_data, out_file, default_flow_style=False)


class DeployConnectionHelper:
    """Handle SSH connections and OS detection"""

    @staticmethod
    def get_current_os_engine(dut_ip, expected_cli_type=None):
        logger.info("Trying connect with SSH to switch")
        preferred_nos = SSHConsts.CLI_TYPE_TO_NOS.get(expected_cli_type)
        if preferred_nos:
            engine = DeployConnectionHelper.attempt_connect_to_switch(dut_ip, preferred_nos, SSHConsts.SSH_CREDS_DICT[preferred_nos])
            if engine:
                logger.info(f"Current OS is {preferred_nos}")
                return preferred_nos, engine
        for nos_name, creds in SSHConsts.SSH_CREDS_DICT.items():
            if nos_name == preferred_nos:
                continue
            engine = DeployConnectionHelper.attempt_connect_to_switch(dut_ip, nos_name, creds)
            if engine:
                logger.info("Current OS is {}".format(nos_name))
                return nos_name, engine
        logger.error("SSH connection to Cumulus, SONiC, and DVS has failed, check switch")
        return None, None

    @staticmethod
    def attempt_connect_to_switch(ip, nos_name, creds_dict):
        """
        Attempt to connect to a switch with the given credentials.
        retried reduced from default 3 to 2 to avoid OpenSSH PerSourcePenalties.
        """
        try:
            username = creds_dict.get('username')
            password = creds_dict.get('password')
            engine = LinuxSshEngine(ip, username=username, password=password)
            engine._engine = engine.get_engine_with_retry(tries=2)
            engine.run_cmd("echo $?")
        except NetmikoAuthenticationException:
            logger.error(f"Login to with {nos_name} credentials has failed")
            return None

        return engine

    @staticmethod
    def handle_serial_log_analyzers(serial_log_analyzers):
        """
        Handle serial log analyzers - ignore manufacture stage if both manufacture and upgrade stages are present

        Args:
            serial_log_analyzers: Dictionary of serial log analyzers
        """
        for analyzer in serial_log_analyzers.values():
            # if manufacture and upgrade stages are both present then we don't analyze the manufacture stage because
            # it runs an older OS version so we shouldn't debug it.
            if {SerialLoggerConst.MANUFACTURE_STAGE, SerialLoggerConst.UPGRADE_STAGE} <= set(analyzer.list_stages()):
                analyzer.ignore_stage(SerialLoggerConst.MANUFACTURE_STAGE)


class DeployMultiNosHelper:
    """Handle multi-NOS operations and NOGA updates"""

    @staticmethod
    def get_image_for_traffic_generators(base_version, target_version, dut, use_GA_image):
        if dut['cli_type'] == CliType.SONIC:
            base_version = PerfConsts.SONIC_GA_IMAGE if use_GA_image else base_version
        elif dut['cli_type'] == CliType.NVUE:
            target_version = Cl_Consts.CL_GA_IMAGE if use_GA_image else target_version
        return base_version, target_version

    @staticmethod
    def validate_sudo_config(engine, current_os):
        if current_os == "Cumulus":
            cl_password = os.getenv("CUMULUS_SWITCH_PASSWORD")
            engine.run_cmd_set([
                "sudo sed -i --follow-symlinks 's/%sudo.*all=(all:all) all/%sudo all=(all:all) nopasswd: all/' /etc/sudoers",
                cl_password],
                patterns_list=["password for cumulus"])

    @staticmethod
    def multi_nos_pre_installation_steps(duts, target_cli_type, chip_type, deploy_sequential=False):
        """Run per-DUT multi-NOS pre-install steps and surface any failures.

        Without awaiting per-future results, any exception raised inside
        ``do_multi_nos_pre_install`` (e.g. uninstall-mode setup failing on the
        switch) is silently swallowed by the executor and the deploy proceeds
        as if pre-installation succeeded, which causes confusing downstream
        failures (e.g. ``OnieInstallationError``). Collect futures and call
        ``result()`` so the first exception propagates to the caller.

        Args:
            duts: Iterable of DUT info dicts as built by ``DeploymentContext``;
                each entry must contain at least ``dut_ip``.
            target_cli_type: Target CLI/NOS being installed (e.g. ``"DVS"``,
                ``"NVUE"``, ``"SONIC"``).
            chip_type: ASIC family string used to look up timeouts in
                ``PerfConsts.TIMEOUT_FOR_UNINSTALL_MODE`` (e.g. ``"SPC5"``).
            deploy_sequential: If ``True``, run per-DUT pre-install steps
                inline; otherwise run them in a thread pool and join.

        Raises:
            Exception: The first exception raised by any per-DUT
                ``do_multi_nos_pre_install`` invocation. In parallel mode all
                submitted tasks are still allowed to finish (the executor
                joins on context exit) before the exception is re-raised.
        """
        logger.info("Multi NOS pre installation steps")
        if deploy_sequential:
            for dut in duts:
                DeployMultiNosHelper.do_multi_nos_pre_install(dut, target_cli_type, chip_type)
            return

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(DeployMultiNosHelper.do_multi_nos_pre_install,
                                       dut, target_cli_type, chip_type)
                       for dut in duts]
        for future in futures:
            future.result()

    @staticmethod
    def _get_expected_noga_cli_type(dut_ip):
        """
        Read the expected current CLI_TYPE from Noga for a given DUT IP.
        :param dut_ip: DUT IP
        :return: CLI_TYPE
        """
        try:
            noga_data = get_noga_resource_data(ip_address=dut_ip, use_cache=False)
            cli_type_str = noga_data['attributes']['Topology Conn.']['CLI_TYPE']
            return cli_type_str
        except Exception as e:
            logger.warning(
                f"Could not fetch expected CLI_TYPE from Noga for {dut_ip}: {e}.")
            return None

    @staticmethod
    def do_multi_nos_pre_install(dut, target_cli_type, chip_type):
        dut_ip = dut['dut_ip']
        noga_cli_type = DeployMultiNosHelper._get_expected_noga_cli_type(dut_ip)
        current_os, engine = DeployConnectionHelper.get_current_os_engine(dut_ip, noga_cli_type)
        if engine:
            DeployMultiNosHelper.validate_sudo_config(engine, current_os)
            GeneralCliCommon(engine).uninstall_os_flow(current_os, target_cli_type, chip_type)

    @staticmethod
    def multi_nos_post_installation_steps(duts, target_cli_type, is_performance, deploy_sequential=False):
        for dut in duts:
            data_query = json.loads('{ "update": { "CLI_TYPE": "' + target_cli_type +
                                    '", "TYPE": "' + CliType.NOS_TO_TYPE_DICT[target_cli_type] +
                                    '"}, "filter": { "name": "' + dut['dut_name'] + '" }, "params": { "login_user": "' +
                                    NogaConstants.NOGA_USER +
                                    '", "api_key":"' + NogaConstants.NOGA_API_KEY + '" } }')
            logger.info(f"Set cli type of {dut['dut_name']} to {target_cli_type} and switch type to "
                        f"{CliType.NOS_TO_TYPE_DICT[target_cli_type]}")
            upload_data_to_noga(data_query)
        DeployMultiNosHelper._remove_noga_cache()
        if is_performance:
            DeployMultiNosHelper.multi_nos_install_traffic_generator(duts, deploy_sequential=deploy_sequential)

    @staticmethod
    def _remove_noga_cache():
        """Remove local Noga cache so subsequent test runs fetch fresh credentials
        matching the newly deployed OS. Stale cache entries cause SSH auth failures
        on SONiC (OpenSSH >= 9.8) due to PerSourcePenalties."""
        try:
            if os.path.exists(NOGA_CACHE_FILE):
                os.remove(NOGA_CACHE_FILE)
                logger.info(f"Removed stale Noga cache: {NOGA_CACHE_FILE}")
        except OSError as e:
            logger.warning(f"Failed to remove Noga cache {NOGA_CACHE_FILE}: {e}")

    @staticmethod
    def multi_nos_install_traffic_generator(duts, deploy_sequential=False):
        if deploy_sequential:
            for dut in duts:
                cli_obj = dut['cli_obj']
                with allure.step('Install traffic generator on switch: {}'.format(dut['dut_name'])):
                    cli_obj.install_traffic_generator()
        else:
            install_threads = []
            executor = concurrent.futures.ThreadPoolExecutor()
            for dut in duts:
                cli_obj = dut['cli_obj']
                with allure.step('Install traffic generator on switch: {}'.format(dut['dut_name'])):
                    install_threads.append((f"Traffic Generator install on {dut['dut_name']}",
                                            executor.submit(cli_obj.install_traffic_generator)))
            DeployOrchestrator.wait_until_deploy_background_process(install_threads)


class DeployOrchestrator:
    """Coordinate deployment operations and thread management"""

    def __init__(self, context):
        self.context = context
        self.pre_install_threads = {}
        self.install_threads = []

    @staticmethod
    def wait_until_deploy_background_process(install_threads, timeout=1200):
        for task_name, task in install_threads:
            with allure.step(f'Wait until {task_name} background process done'):
                try:
                    task.result(timeout=timeout)
                    logger.info(f"{task_name} finished successfully")
                except concurrent.futures.TimeoutError:
                    logger.error(f"{task_name} failed to complete in {timeout}s.")
                    raise

    def execute_pre_installation_steps(self):
        cli_obj = self.context.primary_cli_obj

        cli_obj.pre_installation_steps(self.context, self.pre_install_threads)

        replace_nos = self.context.request.config.getoption('--target_cli_type')
        if replace_nos:
            DeployMultiNosHelper.multi_nos_pre_installation_steps(
                self.context.all_duts,
                replace_nos,
                self.context.chip_type,
                deploy_sequential=self.context.deploy_sequential
            )

        return self.pre_install_threads

    def execute_installation(self):
        """Execute installation phase for all DUTs"""

        executor = None if self.context.deploy_sequential else concurrent.futures.ThreadPoolExecutor()
        use_GA_image = False

        try:
            for dut in self.context.all_duts:
                cli_obj = self.context.get_cli_obj(dut)
                related_base_version_url, related_target_version = DeployImageHelper.get_related_image_urls(
                    self.context.base_version_url, self.context.target_version_url, dut, use_GA_image)

                if not cli_obj.is_dut_supports_image(related_base_version_url, dut['dut_name'], dut['cli_type']):
                    continue

                with allure.step('Install image on dut: {}'.format(dut['dut_name'])):
                    # Disconnect ssh connection
                    self.context.topology_obj.players[dut['dut_alias']]['engine'].disconnect()
                    platform_params_copy = copy.deepcopy(self.context.platform_params)

                    if self.context.deploy_sequential:
                        self.deploy_image(
                            topology_obj=self.context.topology_obj,
                            setup_name=self.context.setup_name,
                            image_url=related_base_version_url,
                            platform_params=platform_params_copy,
                            deploy_type=self.context.deploy_type,
                            apply_base_config=self.context.apply_base_config,
                            reboot_after_install=self.context.reboot_after_install,
                            is_shutdown_bgp=self.context.is_shutdown_bgp,
                            fw_pkg_path=self.context.fw_pkg_path,
                            cli_type=dut['cli_obj'],
                            target_image_url=related_target_version,
                            destination_hwsku=self.context.destination_hwsku,
                            setup_info=self.context.setup_info,
                            dut_alias=dut['dut_alias'],
                            fanout_deploy_threads=self.pre_install_threads,
                            serial_log_analyzers=self.context.serial_log_analyzers,
                            dut_ip=dut['dut_ip'],
                            fanout_target_version=self.context.fanout_target_version
                        )
                    else:
                        self.install_threads.append((f"image install on {dut['dut_name']}",
                                                     executor.submit(self.deploy_image,
                                                                     topology_obj=self.context.topology_obj,
                                                                     setup_name=self.context.setup_name,
                                                                     image_url=related_base_version_url,
                                                                     platform_params=platform_params_copy,
                                                                     deploy_type=self.context.deploy_type,
                                                                     apply_base_config=self.context.apply_base_config,
                                                                     reboot_after_install=self.context.reboot_after_install,
                                                                     is_shutdown_bgp=self.context.is_shutdown_bgp,
                                                                     fw_pkg_path=self.context.fw_pkg_path,
                                                                     cli_type=dut['cli_obj'],
                                                                     target_image_url=related_target_version,
                                                                     destination_hwsku=self.context.destination_hwsku,
                                                                     setup_info=self.context.setup_info,
                                                                     dut_alias=dut['dut_alias'],
                                                                     fanout_deploy_threads=self.pre_install_threads,
                                                                     serial_log_analyzers=self.context.serial_log_analyzers,
                                                                     dut_ip=dut['dut_ip'],
                                                                     fanout_target_version=self.context.fanout_target_version)))
        finally:
            if executor is not None:
                # Allow running tasks to complete but release executor resources once they finish.
                # The caller's wait_until_deploy_background_process still enforces timeouts on futures.
                executor.shutdown(wait=False)

        return self.install_threads

    def deploy_image(self, topology_obj, setup_name, platform_params, image_url, deploy_type,
                     apply_base_config, reboot_after_install, is_shutdown_bgp, fw_pkg_path, cli_type, target_image_url='',
                     destination_hwsku=None, setup_info=None, dut_alias=None, fanout_deploy_threads=None,
                     serial_log_analyzers=None, dut_ip='', fanout_target_version=None):
        cli_type.deploy_image_steps(
            topology_obj=topology_obj,
            setup_name=setup_name,
            platform_params=platform_params,
            image_url=image_url,
            deploy_type=deploy_type,
            apply_base_config=apply_base_config,
            reboot_after_install=reboot_after_install,
            is_shutdown_bgp=is_shutdown_bgp,
            fw_pkg_path=fw_pkg_path,
            target_image_url=target_image_url,
            destination_hwsku=destination_hwsku,
            setup_info=setup_info,
            dut_alias=dut_alias,
            fanout_deploy_threads=fanout_deploy_threads,
            serial_log_analyzers=serial_log_analyzers,
            dut_ip=dut_ip,
            fanout_target_version=fanout_target_version
        )
        time.sleep(30)

    def execute_post_installation_steps(self):
        cli_obj = self.context.primary_cli_obj

        DeployTopologyHelper.customize_testbed_yaml_file(self.context.setup_info, self.context.sonic_topo, self.context.neighbor_mode)
        cli_obj.post_installation_steps(self.context, DeployImageHelper)

        replace_nos = self.context.request.config.getoption('--target_cli_type')
        if replace_nos:
            DeployMultiNosHelper.multi_nos_post_installation_steps(
                self.context.setup_info['duts'],
                replace_nos,
                self.context.is_performance,
                deploy_sequential=self.context.deploy_sequential
            )

    def execute_dpu_post_installation_steps(self):
        cli_obj = self.context.primary_cli_obj
        cli_obj.dpu_post_installation_steps(self.context)

    def execute_dpu_image_installation(self):
        with allure.step(f'Start to install the bfb image on DPUs:{self.context.base_version_dpu}'):
            base_version_dpu = self.context.base_version_dpu
            install_threads = []
            try:
                if self.context.deploy_sequential or len(self.context.setup_info['duts']) == 1:
                    # run sequentially: either explicitly requested or single DUT
                    for dut in self.context.setup_info['duts']:
                        DeployDpuHelper.bfb_install_dpu(
                            self.context.topology_obj,
                            base_version_dpu,
                            dut['dut_alias'],
                            dut['dut_name'],
                            dut['cli_obj'],
                            self.context.setup_name
                        )
                else:
                    # for multiple DUTs, run in parallel
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        for dut in self.context.setup_info['duts']:
                            install_threads.append((f"DPU image install on {dut['dut_name']}",
                                                    executor.submit(DeployDpuHelper.bfb_install_dpu,
                                                                    self.context.topology_obj,
                                                                    base_version_dpu,
                                                                    dut['dut_alias'], dut['dut_name'], dut['cli_obj'],
                                                                    self.context.setup_name)))
                        DeployOrchestrator.wait_until_deploy_background_process(install_threads, timeout=2000)

            except Exception as e:
                raise Exception(f"Failed to install the DPU image on one of the DUTs: {e}")

    def execute_full_deployment(self):
        """
        Execute the complete deployment flow

        Switch and BMC deployments are independent:
        - BMC flow runs first when --base-version-bmc is provided.
        - Switch flow (Phase 1-5, including DPU) runs when --base-version or --target-version is provided.
        Both can run together, or either one can run alone. For a BMC-only run
        (no switch image) the ordering has no effect since the switch phases are skipped.

        Returns:
            dict: Results containing thread information and status
        """
        results = {}

        # BMC installation runs before the switch: the BMC boots first and
        # controls Switch-Host power, so deploying it first.
        # The BMC reuses the community topo flow (bmc-dual-mgmt is a normal topo),
        # so it is driven by --sonic-topo / --dest_hwsku, not by dedicated params.
        if self.context.deploy_bmc:
            ctx = self.context
            bmc_topo_threads = {}
            dut_name = ctx.primary_dut['dut_name']
            ansible_path = ctx.setup_info['ansible_path']
            bmc_params = DeployBmcHelper._resolve_bmc_params(ctx)

            # remove-topo / add-topo / gen-mg reuse the community deploy functions
            # in sonic_only_methods directly (bmc-dual-mgmt is a normal topo); there
            # are no BMC-specific topo wrappers.
            with allure.step('BMC remove-topo'):
                SonicInstallationSteps.remove_topologies(
                    ansible_path=ansible_path,
                    dut_names=[dut_name],
                    setup_name=ctx.setup_name,
                    sonic_topo=ctx.sonic_topo,
                )
            with allure.step('BMC add-topo and generate minigraph'):
                SonicInstallationSteps.start_community_background_threads(
                    bmc_topo_threads, ctx.setup_name, dut_name, ctx.sonic_topo,
                    'ceos', MarsConstants.DEFAULT_PTF_TAG, '', ansible_path,
                    ctx.setup_info, ctx.destination_hwsku,
                    deploy_sequential=ctx.deploy_sequential,
                )
            with allure.step('BMC installation'):
                DeployBmcHelper.install_bmc(ctx)
            with allure.step('Wait for BMC add-topo/gen-mg to finish'):
                wait_until_background_procs_done(bmc_topo_threads)

            # deploy-mg produces the NetworkBmc DEVICE_METADATA, the default ACL
            # tables and the telemetry certificates on the BMC.
            with allure.step('BMC deploy minigraph'):
                deploy_cmd = get_deploy_minigraph_cmd().format(SWITCH=dut_name, TOPO=ctx.sonic_topo)
                logger.info(f"Running CMD: {deploy_cmd}")
                execute_script(deploy_cmd, ansible_path, validate=True, timeout=900)

            bmc_params['engine'].disconnect()
            with allure.step('Wait for BMC is ready after config reload'):
                DeployBmcHelper._wait_bmc_sonic_db_ready(bmc_params)
                DeployBmcHelper._wait_bmc_containers_running(bmc_params)
            # Apply DNS servers after deploy-mg
            with allure.step('Apply DNS servers configuration on BMC'):
                DeployBmcHelper._apply_dns_servers(bmc_params)
            # Disable extra services not available on the BMC. Must run after deploy-mg:
            # 'config load_minigraph' / 'config reload' resets FEATURE state.
            if is_redmine_issue_active([5057220])[0] or is_redmine_issue_active([5057221])[0]:
                with allure.step('Disable unavailable services in the config'):
                    DeployBmcHelper._disable_unavailable_services(bmc_params)

        if self.context.deploy_switch:
            # Phase 1: Pre-installation
            with allure.step('pre installation steps'):
                results['pre_install_threads'] = self.execute_pre_installation_steps()

            # In sequential mode, pre-install processes have already completed inline.
            # Verify them now so a failure stops the run before installation begins.
            if self.context.deploy_sequential:
                with allure.step('verify pre installation processes are done'):
                    self._verify_pre_installation_processes(results['pre_install_threads'])

            # Phase 2: Installation
            with allure.step('installation'):
                results['install_threads'] = self.execute_installation()
                if self.context.deploy_sequential:
                    logger.info("Sequential mode: installation completed inline, "
                                "centralized timeout not applied (command-level timeouts still active)")
                else:
                    self.wait_until_deploy_background_process(results['install_threads'], timeout=2400)

            # Phase 3: Verify pre-installation processes (background mode)
            if not self.context.deploy_sequential:
                with allure.step('verify pre installation processes are done'):
                    self._verify_pre_installation_processes(results['pre_install_threads'])

            # Phase 4: Post-installation
            with allure.step('post installation steps'):
                self.execute_post_installation_steps()

                # Cleanup
                cache_full_path = os.path.join(os.path.dirname(__file__), '../../.pytest_cache')
                shutil.rmtree(cache_full_path, ignore_errors=True)

        # Phase 5: DPU installation
        # TODO: WA for RM#4946685, power cycle duts
        if self.context.deploy_dpu and "01-03-ha" in self.context.setup_name:
            with allure.step("Power cycle the DUTs if it's HA setup"):
                for dut in self.context.setup_info["duts"]:
                    dut["cli_obj"].remote_reboot(self.context.topology_obj, dut_alias=dut["dut_alias"])
        if self.context.deploy_dpu:
            # install DPUs
            self.execute_dpu_image_installation()
            # TODO: WA for RM#4946685, power cycle duts
            if "03-04-ha" in self.context.setup_name:
                with allure.step("Power cycle the DUTs if it's HA setup"):
                    time.sleep(30)
                    for dut in self.context.setup_info["duts"]:
                        dut["cli_obj"].remote_reboot(self.context.topology_obj, dut_alias=dut["dut_alias"])
                    time.sleep(120)
            self.execute_dpu_post_installation_steps()

        return results

    def _verify_pre_installation_processes(self, pre_install_threads):
        """Verify pre-installation background processes are complete"""

        logger.info("Wait until pre-installation background process done")
        try:
            wait_until_background_procs_done(pre_install_threads)
        except AssertionError:
            # Give it another try if the background processes fail
            self.execute_pre_installation_steps()
            wait_until_background_procs_done(pre_install_threads)
        logger.info("Pre-installation background processes are done")


class DeployDpuHelper:
    """Handle DPU-specific deployment operations"""

    @staticmethod
    def bfb_install_dpu(topology_obj, base_version_dpu, dut_alias, dut_name, cli_obj, setup_name):

        rshim_value, dpu_index_list, installed_dpus = get_installed_dpu_info(topology_obj, dut_alias, dut_name)
        dut_engine = topology_obj.players[dut_alias]['engine']

        with allure.step('Downloading image to switch dut'):
            dut_engine.run_cmd("sudo config bgp shutdown all")
            try:
                dpu_image_url = MarsConstants.HTTP_SERVER_NBU_NFS + base_version_dpu
                dest_file = "/tmp/" + base_version_dpu.split('/')[-1]
                download_file_to_dut(dut_engine, dpu_image_url, dest_file)
            finally:
                dut_engine.run_cmd("sudo config bgp startup all")

        with allure.step('Start monitoring minicom'):
            for index in dpu_index_list:
                try:
                    dut_engine.run_cmd(
                        f"sudo screen -dmS ttyUSB{index}_log minicom -D /dev/ttyUSB{index} -C /var/log/ttyUSB{index}", validate=True)
                except Exception as e:
                    logger.warning(f"Failed to start monitoring minicom for DPU{index}: {e}")

        with allure.step(f"Disable dark mode on {dut_name} {dut_alias}"):
            DeployDpuHelper.disable_dark_mode(topology_obj, cli_obj, dpu_index_list, dut_alias)

        try:
            with allure.step('Install BFB image on all DPUs'):
                output = dut_engine.run_cmd(
                    f"sudo sonic-bfb-installer.sh -r {rshim_value} -b {dest_file} -v")
                failures = []
                for index in dpu_index_list:
                    pattern = f"{index}.*Installation Successful"
                    if not re.search(pattern, output):
                        failures.append(index)
                if failures:
                    assert False, f"Failed to install bfb image on DPU: {failures}."

                if installed_dpus:
                    save_specified_installed_dpus(installed_dpus, dut_alias, dut_name)
        finally:
            with allure.step('Stop monitoring minicom'):
                for index in dpu_index_list:
                    dut_engine.run_cmd(f"sudo screen -S ttyUSB{index}_log -X quit", validate=False)
                # Always copy the minicom logs to the dumps folder
                # Sometimes we need analyze the logs even if the deployment doesn't fail
                try:
                    dumps_folder = get_dumps_folder(setup_name, "monitor", topology_obj)
                    for index in dpu_index_list:
                        source_file = f"/var/log/ttyUSB{index}"
                        dest_file = os.path.join(dumps_folder, f"ttyUSB{index}.log")
                        dut_engine.copy_file(
                            source_file=source_file,
                            dest_file=dest_file,
                            file_system='/',
                            direction='get',
                            overwrite_file=True,
                            verify_file=False)
                    logger.info(f"Minicom logs are copied to dumps folder: {dumps_folder}")
                except Exception as e:
                    logger.warning(f"Failed to copy minicom logs to dumps folder: {e}")

    @staticmethod
    def disable_dark_mode(topology_obj, cli_obj, dpu_index_list, dut_alias):
        with allure.step('Disable dark mode by config chassis modules startup DPU'):
            cli_obj.startup_dpus(dpu_index_list)
            try:
                cli_obj.verify_dpus_up(dpu_index_list)
            except AssertionError:
                logger.warning("Failed to verify DPUs are up, checking if they can receive the new image")
                cli_obj.verify_dpu_boot_progress(dpu_index_list, bad_states={0, 15})
                time.sleep(50)  # Wait 50s to make sure the rshim will be ready
            cli_obj.save_configuration()


class DeployBmcHelper:
    """
    Handle SONiC BMC image installation.

    Reuses noga "BMC IP", "Serial Connection Command" and "Remote Reboot"
    fields that already drive the regular switch deployment flow.

    Reference: BMC SONiC wiki section 2 "Image installation".
    """

    @staticmethod
    def install_bmc(context):
        """
        Install SONiC BMC image on the primary DUT's BMC.

        HW gate runs in the U-Boot phase by parsing the boot banner
        captured during autoboot interruption. Only HW types in
        SONIC_BMC_SUPPORTED_HW_TYPES proceed with the install;
        unsupported HW resumes its normal boot and the function
        returns. A banner that cannot be parsed raises - the caller
        explicitly requested a BMC install, so an unidentifiable BMC
        must fail loud rather than be silently skipped.

        Image files (sonic_tftp_install.fit and
        sonic-aspeed-arm64-emmc.img.gz) must already be staged under
        the TFTP server root.
        """
        from ngts.helpers.secure_boot_helper import SecureBootHelper

        primary_dut = context.primary_dut
        dut_name = primary_dut['dut_name']
        dut_alias = primary_dut['dut_alias']

        with allure.step(f'Resolve BMC parameters for {dut_name}'):
            bmc_params = DeployBmcHelper._resolve_bmc_params(context)

        # Run a single attempt only - on failure raise immediately so the
        # operator can inspect the BMC in its post-failure state instead
        # of having the whole TFTP/eMMC install repeated and the failure
        # state wiped by the next retry.
        serial_engine = None
        try:
            with allure.step('Open BMC serial console'):
                serial_engine = SecureBootHelper.get_serial_engine_instance(
                    context.topology_obj, dut_alias
                )
                serial_engine.create_serial_engine(login_to_switch=False)

            with allure.step('Power-cycle DUT to enter U-Boot'):
                context.primary_cli_obj.remote_reboot(
                    context.topology_obj, dut_alias, wait_till_alive=False
                )

            with allure.step('Interrupt autoboot to reach U-Boot prompt'):
                uboot_banner = DeployBmcHelper._wait_uboot_prompt(serial_engine)

            with allure.step('Detect BMC hardware type from U-Boot banner'):
                hw_type = DeployBmcHelper._parse_soc_from_banner(
                    uboot_banner, dut_name
                )

            if hw_type not in BmcDeployConstants.SONIC_BMC_SUPPORTED_HW_TYPES:
                # Resume normal boot so the DUT does not stay parked at '=>'.
                logger.warning(
                    f"BMC HW type '{hw_type}' on {dut_name} not in "
                    f"{BmcDeployConstants.SONIC_BMC_SUPPORTED_HW_TYPES}; "
                    f"skipping SONiC BMC install."
                )
                with allure.step('Resume BMC boot (HW not supported)'):
                    DeployBmcHelper._run_uboot_cmd(serial_engine, 'boot')
                return False

            with allure.step('Run U-Boot install sequence'):
                DeployBmcHelper._run_uboot_install(serial_engine, bmc_params)

            with allure.step('Wait for eMMC installation to finish'):
                DeployBmcHelper._wait_emmc_write_done(serial_engine)

            with allure.step('Verify BMC SSH login'):
                DeployBmcHelper._wait_bmc_login_with_power_cycle(
                    context, dut_alias, serial_engine
                )
                bmc_params['engine'].disconnect()
                DeployBmcHelper._verify_bmc_login(bmc_params)

            # sshd comes up well before the SONiC stack after the install reboots.
            # The config steps below run sonic-db-cli / config commands, so wait
            # until CONFIG_DB (redis) is reachable and sonic-environment exists,
            # otherwise they race the boot and fail ('Unable to connect to redis').
            with allure.step('Wait for BMC SONiC DB to be ready'):
                DeployBmcHelper._wait_bmc_sonic_db_ready(bmc_params)

            with allure.step('Disable ZTP on BMC'):
                DeployBmcHelper._disable_ztp(bmc_params)

            # Install the DHCP recovery workaround before the reboot below, so this
            # reboot (and every later reboot / power-cycle during tests) self-heals
            # eth0 when the ftgmac100 driver comes up without a DHCP lease.
            # Tracked by Redmine #5091238; drops automatically once it is fixed.
            if is_redmine_issue_active([5091238])[0]:
                with allure.step('Install BMC DHCP recovery workaround service'):
                    DeployBmcHelper._install_dhcp_recovery_service(bmc_params)

            # Sync the BMC clock
            with allure.step('Sync BMC clock via chrony'):
                try:
                    DeployBmcHelper._sync_bmc_clock(bmc_params)
                except Exception as e:
                    logger.warning(
                        f"Failed to sync BMC clock on {dut_name}: {e}"
                    )

            logger.info(f"BMC installation succeeded on {dut_name}")
            return True
        finally:
            DeployBmcHelper._close_serial(serial_engine)

    @staticmethod
    def _parse_soc_from_banner(banner_text, dut_name):
        """
        Pull the SoC ID (e.g. 'AST2700-A1') out of a captured U-Boot
        banner. Prefer the 'SOC:' line; fall back to 'Model: AST...'
        because the 'SOC:' line is printed earlier and is sometimes
        missed by the serial capture. Raise if neither is found.
        """
        match = re.search(r'^\s*SOC:\s*(\S+)', banner_text, re.MULTILINE)
        if match:
            soc = match.group(1)
            logger.info(f"BMC SoC parsed from 'SOC:' line: '{soc}'")
            return soc

        match = re.search(
            r'^\s*Model:\s*(AST\d+(?:-A\d+)?)\b',
            banner_text, re.MULTILINE | re.IGNORECASE,
        )
        if match:
            soc = match.group(1)
            logger.info(f"BMC SoC parsed from 'Model:' line: '{soc}'")
            return soc

        tail = banner_text[-2000:] if banner_text else '(empty)'
        logger.error(
            f"U-Boot banner from {dut_name} had no 'SOC:' or "
            f"'Model: AST...' line. Last 2000 chars:\n{tail}"
        )
        raise RuntimeError(
            f"Could not parse BMC SoC from U-Boot banner on {dut_name}"
        )

    @staticmethod
    def _resolve_bmc_params(context):
        """
        Read BMC-related fields from the primary DUT's noga attributes.

        Required:
            bmc_ip - matches the noga "BMC IP" field.
        Optional:
            bmc_bootconf - per-platform U-Boot bootconf override; falls back
                           to BmcDeployConstants.UBOOT_BOOTCONF_DEFAULT.
        """
        primary_dut = context.primary_dut
        attrs = context.topology_obj.players[primary_dut['dut_alias']]['attributes']
        specific = attrs.noga_query_data['attributes'].get('Specific', {})

        bmc_ip = specific.get('bmc_ip')
        if not bmc_ip:
            raise Exception(
                f"BMC install requires the 'BMC IP' (bmc_ip) field configured "
                f"under noga Specific attributes for DUT {primary_dut['dut_name']}"
            )

        return {
            'dut_name': primary_dut['dut_name'],
            'tftp_server_ip': BmcDeployConstants.BMC_TFTP_SERVER_IP,
            'bmc_ip': bmc_ip,
            'bmc_bootconf': specific.get(
                'bmc_bootconf', BmcDeployConstants.UBOOT_BOOTCONF_DEFAULT
            ),
            'engine': context.topology_obj.players['bmc']['engine'],
        }

    @staticmethod
    def _close_serial(serial_engine):
        if serial_engine is None:
            return
        try:
            close = getattr(serial_engine, 'close_serial_engine', None) or \
                getattr(serial_engine, 'disconnect', None)
            if callable(close):
                close()
        except Exception as e:
            logger.warning(f"Failed to close BMC serial console: {e}")

    @staticmethod
    def _wait_uboot_prompt(serial_engine):
        """
        Hammer Enter during the ~3s autoboot window until the U-Boot
        prompt shows up. Returns the captured serial output (boot
        banner + everything up to the prompt) so the caller can parse
        the SoC. Each per-attempt timeout salvages the underlying
        pexpect 'before' buffer so the banner is not lost across loop
        iterations.
        """
        deadline = time.time() + BmcDeployConstants.UBOOT_PROMPT_TIMEOUT
        per_attempt_timeout = 1   # autoboot window is ~3s wide
        captured = []
        while time.time() < deadline:
            try:
                output, _ = serial_engine.run_cmd(
                    '',
                    expected_value=BmcDeployConstants.UBOOT_PROMPT,
                    timeout=per_attempt_timeout,
                )
                captured.append(output)
                return ''.join(captured)
            except Exception:
                try:
                    pending = serial_engine.serial_engine.before
                    if pending:
                        captured.append(
                            pending.decode('utf-8', errors='ignore')
                            if isinstance(pending, bytes) else str(pending)
                        )
                except Exception:
                    pass
                continue
        raise Exception(
            "Failed to reach U-Boot prompt within "
            f"{BmcDeployConstants.UBOOT_PROMPT_TIMEOUT}s"
        )

    @staticmethod
    def _run_uboot_cmd(serial_engine, cmd, timeout=None):
        timeout = timeout or BmcDeployConstants.UBOOT_PROMPT_TIMEOUT
        serial_engine.run_cmd(
            cmd,
            expected_value=BmcDeployConstants.UBOOT_PROMPT,
            timeout=timeout,
        )

    @staticmethod
    def _run_uboot_install(serial_engine, bmc_params):
        """Execute the U-Boot command sequence to start the install."""
        emmc_img = BmcDeployConstants.BMC_EMMC_IMG_FILE_NAME

        # DHCP can occasionally fail; retry a few times.
        dhcp_ok = False
        for attempt in range(BmcDeployConstants.DHCP_RETRY_LIMIT):
            try:
                DeployBmcHelper._run_uboot_cmd(
                    serial_engine, 'dhcp', timeout=BmcDeployConstants.DHCP_TIMEOUT
                )
                output = DeployBmcHelper._wait_uboot_prompt(serial_engine)
                logger.info(f"DHCP output: {output}")
                dhcp_ok = "DHCP client bound to address" in output
                if dhcp_ok:
                    break
            except Exception as e:
                logger.warning(f"DHCP attempt {attempt + 1} failed in U-Boot: {e}")
        if not dhcp_ok:
            raise Exception("Failed to acquire DHCP lease in U-Boot")

        DeployBmcHelper._run_uboot_cmd(
            serial_engine, f"setenv serverip {bmc_params['tftp_server_ip']}"
        )
        DeployBmcHelper._run_uboot_cmd(
            serial_engine, f"setenv loadaddr {BmcDeployConstants.UBOOT_LOAD_ADDR}"
        )

        # Build bootargs incrementally: first reset to empty, then append
        # each tail with a quoted 'setenv bootargs "${bootargs}<tail>"'.
        # The eMMC image URL is HTTP, served by the build server.
        DeployBmcHelper._run_uboot_cmd(serial_engine, 'setenv bootargs ""')
        for tail_tmpl in BmcDeployConstants.UBOOT_BOOTARGS_TAILS:
            tail = tail_tmpl.format(
                emmc_img=emmc_img,
                http_server=BmcDeployConstants.BMC_HTTP_SERVER_URL,
            )
            DeployBmcHelper._run_uboot_cmd(
                serial_engine, f'setenv bootargs "${{bootargs}}{tail}"'
            )

        # Echo the assembled bootargs back to the console so the test
        # log captures the actual U-Boot env value. If any 'setenv'
        # silently dropped characters on the slow BMC serial, the wrong
        # value will be visible here rather than only via a much later
        # kernel cmdline mismatch.
        DeployBmcHelper._run_uboot_cmd(serial_engine, 'print bootargs')

        DeployBmcHelper._tftp_download_fit(serial_engine)
        DeployBmcHelper._run_uboot_cmd(
            serial_engine, f"setenv bootconf {bmc_params['bmc_bootconf']}"
        )
        # 'bootm' starts the installer kernel; do not expect the U-Boot
        # prompt back. The next stage waits for the eMMC marker.
        serial_engine.run_cmd(
            "bootm $loadaddr#conf-$bootconf",
            expected_value='Starting kernel',
            timeout=BmcDeployConstants.UBOOT_PROMPT_TIMEOUT,
        )

    @staticmethod
    def _tftp_download_fit(serial_engine):
        """
        Run 'tftp $loadaddr <fit>' with retries. Success is signalled by
        U-Boot printing 'Bytes transferred = ...' once the transfer
        completes, so we use that line directly as the success pattern.
        We also list the U-Boot prompt as a fallback pattern: if U-Boot
        returns to its prompt without printing 'Bytes transferred' (e.g.
        after 'Retry count exceeded; starting again' or 'TFTP error'),
        the transfer failed and we retry up to TFTP_DOWNLOAD_RETRY_LIMIT.
        """
        cmd = f"tftp $loadaddr {BmcDeployConstants.BMC_FIT_FILE_NAME}"
        retry_limit = BmcDeployConstants.TFTP_DOWNLOAD_RETRY_LIMIT
        last_summary = ''
        for attempt in range(1, retry_limit + 1):
            try:
                output, idx = serial_engine.run_cmd(
                    cmd,
                    expected_value=[
                        'Bytes transferred',
                        BmcDeployConstants.UBOOT_PROMPT,
                    ],
                    timeout=BmcDeployConstants.TFTP_DOWNLOAD_TIMEOUT,
                )
                if idx == 0:
                    logger.info(
                        f"TFTP fit download succeeded on attempt {attempt}"
                    )
                    # Drain the trailing '<bytes> hex)\n=> ' so the next
                    # U-Boot command starts cleanly.
                    try:
                        serial_engine.run_cmd(
                            '',
                            expected_value=BmcDeployConstants.UBOOT_PROMPT,
                            timeout=10,
                            send_without_enter=True,
                        )
                    except Exception:
                        pass
                    return
                last_summary = (output or '')[-300:]
                logger.warning(
                    f"TFTP fit attempt {attempt}/{retry_limit} returned to "
                    f"U-Boot prompt without 'Bytes transferred'. "
                    f"Tail: {last_summary}"
                )
            except Exception as e:
                last_summary = str(e)
                logger.warning(
                    f"TFTP fit attempt {attempt}/{retry_limit} raised: {e}"
                )
                # Drain whatever is buffered so the next 'tftp' command
                # starts at a clean prompt.
                try:
                    serial_engine.run_cmd(
                        '\r',
                        expected_value=BmcDeployConstants.UBOOT_PROMPT,
                        timeout=30,
                    )
                except Exception:
                    pass
        raise Exception(
            f"Failed to download {BmcDeployConstants.BMC_FIT_FILE_NAME} via TFTP "
            f"after {retry_limit} attempts. Last output tail: {last_summary}"
        )

    @staticmethod
    def _wait_emmc_write_done(serial_engine):
        """Wait for the installer to finish writing eMMC and updating U-Boot env."""
        serial_engine.run_cmd(
            '',
            expected_value=BmcDeployConstants.EMMC_WRITE_DONE_MARKER,
            timeout=BmcDeployConstants.EMMC_WRITE_TIMEOUT,
            send_without_enter=True,
        )

    @staticmethod
    def _wait_bmc_login(serial_engine):
        """Best-effort confirmation that the BMC has reached its login prompt."""
        serial_engine.run_cmd(
            '',
            expected_value=BmcDeployConstants.BMC_LOGIN_PROMPT,
            timeout=BmcDeployConstants.BMC_BOOT_TIMEOUT,
            send_without_enter=True,
        )

    @staticmethod
    def _wait_bmc_login_with_power_cycle(context, dut_alias, serial_engine):
        """
        Wait for the BMC login prompt after a reboot.
        """
        try:
            DeployBmcHelper._wait_bmc_login(serial_engine)
            return
        except Exception as err:
            logger.warning(
                f"BMC login prompt not seen within timeout: {err}. "
                f"Power-cycling to recover from a possibly stuck reboot."
            )
            context.primary_cli_obj.remote_reboot(
                context.topology_obj, dut_alias, wait_till_alive=False
            )
        DeployBmcHelper._wait_bmc_login(serial_engine)

    @staticmethod
    def _verify_bmc_login(bmc_params, tries=30, delay=5):
        """
        Confirm SSH login to the freshly-booted BMC works. sshd accepts TCP well
        before it actually serves a password prompt / the admin account is ready,
        so the first connections can time out without ever showing a prompt. Retry
        until login succeeds (same pattern as _wait_bmc_sonic_db_ready); otherwise a
        transient not-ready window aborts the whole BMC install.
        """
        engine = bmc_params['engine']
        retry_call(engine.run_cmd, fargs=["uname -a"],
                   tries=tries, delay=delay, logger=logger)

    @staticmethod
    def _wait_bmc_sonic_db_ready(bmc_params, tries=30, delay=10):
        """
        After the install reboots, sshd on the BMC comes up well before the
        SONiC stack. The config steps that follow (DEVICE_METADATA type, ACLs,
        feature disable) run sonic-db-cli / config commands and edit
        /etc/sonic/sonic-environment, so wait until CONFIG_DB (redis) is
        reachable and sonic-environment exists. Without this they race the boot
        and fail with 'Unable to connect to redis' / sonic-environment missing.
        """
        db_errors = ("Unable to connect", "Invalid database name",
                     "Connection refused", "doesn't exist")
        engine = bmc_params['engine']

        def _check():
            db = engine.run_cmd("sonic-db-cli CONFIG_DB PING")
            env = engine.run_cmd(
                "test -f /etc/sonic/sonic-environment && echo OK || echo MISSING"
            )
            if not (all(err not in db for err in db_errors) and "OK" in env):
                raise RuntimeError(
                    f"BMC SONiC not ready yet (CONFIG_DB PING={db.strip()!r}, "
                    f"sonic-environment={env.strip()!r})"
                )
            logger.info("BMC SONiC CONFIG_DB is ready")

        retry_call(_check, tries=tries, delay=delay, logger=logger)

    @staticmethod
    def _install_dhcp_recovery_service(bmc_params):
        """
        Install and enable a systemd timer that recovers eth0's DHCP lease after a
        BMC reboot / power-cycle (and at runtime).

        Known issue: on the AST2700 BMC the ftgmac100 NIC reports "Link is Up"
        after a (re)boot but no DHCP offer ever arrives, so eth0 stays without an
        IPv4 address and the BMC is unreachable over the network. Reloading the
        driver and re-running dhclient restores connectivity. Because the BMC has
        no IP at that point it cannot be fixed remotely, so the recovery has to
        run locally - hence a systemd unit instead of a per-test step.

        A oneshot at boot is not enough: eth0 can briefly carry an early lease at
        the moment a boot-time check runs (so it no-ops) and then drop it, and the
        lease can also be lost at runtime. So the recovery script is driven by a
        timer that re-checks periodically and reloads the driver only when eth0
        has no IPv4 address.

        The script and units are staged in ngts/common/; this copies them to the
        BMC, installs them and enables the timer.
        """
        common_dir = os.path.join(os.path.dirname(__file__), "..", "..", "common")
        script_src = os.path.join(common_dir, BmcDeployConstants.BMC_DHCP_WA_SCRIPT_SRC)
        service_src = os.path.join(common_dir, BmcDeployConstants.BMC_DHCP_WA_SERVICE_SRC)
        timer_src = os.path.join(common_dir, BmcDeployConstants.BMC_DHCP_WA_TIMER_SRC)

        engine = bmc_params['engine']

        # Stage the files in /tmp (writable for the admin user), then move them
        # into place with sudo.
        for src, dst, mode in (
            (script_src, BmcDeployConstants.BMC_DHCP_WA_SCRIPT_DST, "0755"),
            (service_src, BmcDeployConstants.BMC_DHCP_WA_SERVICE_DST, "0644"),
            (timer_src, BmcDeployConstants.BMC_DHCP_WA_TIMER_DST, "0644"),
        ):
            staged = os.path.basename(src)
            engine.copy_file(
                source_file=src, dest_file=staged,
                file_system="/tmp/", overwrite_file=True, verify_file=True,
            )
            engine.run_cmd(f"sudo install -m {mode} /tmp/{staged} {dst}")

        engine.run_cmd("sudo systemctl daemon-reload")
        # Enable + start the timer (the service is triggered by the timer).
        engine.run_cmd(
            f"sudo systemctl enable --now {BmcDeployConstants.BMC_DHCP_WA_TIMER_NAME}"
        )

        enabled = engine.run_cmd(
            f"sudo systemctl is-enabled {BmcDeployConstants.BMC_DHCP_WA_TIMER_NAME}"
        ).strip()
        logger.info(f"BMC DHCP recovery timer is-enabled: '{enabled}'")
        if "enabled" not in enabled:
            raise RuntimeError(
                f"Failed to enable {BmcDeployConstants.BMC_DHCP_WA_TIMER_NAME} "
                f"on the BMC; 'systemctl is-enabled' returned '{enabled}'"
            )

    @staticmethod
    def _wait_bmc_containers_running(bmc_params, tries=30, delay=10):
        """
        Wait BMC containers are running
        """
        engine = bmc_params['engine']
        general_cli = GeneralCliCommon(engine)

        def _check():
            defined = engine.run_cmd("docker ps -a --format '{{.Names}}'").split()
            if not defined:
                raise RuntimeError("No BMC containers reported by 'docker ps' yet")
            running = set(general_cli.get_running_containers_names())
            not_running = [name for name in defined if name not in running]
            if not_running:
                raise RuntimeError(
                    f"BMC containers not running yet: {', '.join(not_running)}"
                )
            logger.info(
                "All BMC containers are running"
            )

        retry_call(_check, tries=tries, delay=delay, logger=logger)

    @staticmethod
    def _sync_bmc_clock(bmc_params):
        """
        Force a one-shot clock sync via SSH. Equivalent to running
        'sudo systemctl start chrony; sleep N; sudo chronyc -a makestep'
        from the BMC shell. Assumes sudo is NOPASSWD for the BMC admin.
        """
        engine = bmc_params['engine']
        logger.info(
            f"BMC clock before sync: {engine.run_cmd('date').strip()}"
        )
        engine.run_cmd("sudo systemctl start chrony")
        time.sleep(BmcDeployConstants.BMC_CHRONY_SETTLE_SECONDS)
        engine.run_cmd("sudo chronyc -a makestep")
        if is_redmine_issue_active([5083483])[0]:
            logger.info('Redmine #5083483 is active, persisting chrony makestep on BMC')
            engine.run_cmd("echo 'makestep 1 3' | sudo tee /etc/chrony/conf.d/step.conf")
            engine.run_cmd("sudo systemctl restart chrony")
        logger.info(
            f"BMC clock after sync:  {engine.run_cmd('date').strip()}"
        )

    @staticmethod
    def _apply_dns_servers(bmc_params):
        """
        Configure the DNS servers on the BMC.
        """
        engine = bmc_params['engine']
        SonicIpCli(engine).apply_dns_servers_into_resolv_conf(is_air_setup=False)
        logger.info(
            f"BMC DNS nameservers after apply: "
            f"{engine.run_cmd('show dns nameserver').strip()}"
        )

    @staticmethod
    def _disable_ztp(bmc_params):
        """
        Disable ZTP on the BMC.
        """
        engine = bmc_params['engine']
        engine.run_cmd("sudo config ztp disable -y")

    @staticmethod
    def _disable_unavailable_services(bmc_params):
        """
        Disabled mgmt-framework, radv, snmp, swss, syncd and if not available eventd
        """
        engine = bmc_params['engine']
        services_to_disable = []
        if is_redmine_issue_active([5057220])[0]:
            services_to_disable.append("eventd")
        if is_redmine_issue_active([5057221])[0]:
            services_to_disable.extend(["mgmt-framework", "radv", "snmp", "swss", "syncd"])
        for service in services_to_disable:
            engine.run_cmd(f"sudo config feature state {service} disabled")
        else:
            engine.run_cmd("sudo config save -y")
