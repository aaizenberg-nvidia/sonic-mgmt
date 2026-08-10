import logging
import json
import os
import re
import time
import allure
import netmiko

from ngts.cli_wrappers.interfaces.interface_general_clis import GeneralCliInterface
from ngts.cli_wrappers.common.general_clis_common import GeneralCliCommon
from ngts.cli_wrappers.sonic.sonic_onie_clis import SonicOnieCli, OnieInstallationError
from ngts.constants.constants import InfraConst
from ngts.constants.performance_constants import PerfConsts
from ngts.helpers.run_process_on_host import run_process_on_host
from ngts.helpers.secure_boot_helper import SecureBootHelper
from ngts.scripts.sonic_deploy.dvs_only_methods import DvsInstallationSteps
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active

from devts.infra.tools.topology_tools.nogaq import get_noga_entire_resource_data
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive

logger = logging.getLogger()


class DvsGeneralCli(GeneralCliCommon):

    def __init__(self, engine, dut_alias):
        super().__init__(engine, dut_alias)

    def deploy_image(self, image_path, topology_obj, dut_alias):
        logger.info(f"DvsGeneralCli.deploy_image: starting for dut_alias={dut_alias} ip={self.engine.ip} "
                    f"image_path={image_path}")
        if not self.prepare_for_installation(topology_obj, dut_alias):
            raise OnieInstallationError(
                f"Failed to move DUT {self.engine.ip} into ONIE install mode before DVS image installation"
            )
        logger.info(f"DvsGeneralCli.deploy_image: ip={self.engine.ip} prepare_for_installation done, "
                    f"starting install_image_onie")
        self.install_image_onie(self.engine, image_path)

    def _install_sdk_and_fw(self, sdk_version, fw_version, debian_enabled):
        """
        Internal method to install SDK and FW with debian flag control
        """
        di_flag = "-di" if debian_enabled else ""
        install_cmd = "sdk_install -v {} {} && fw_burn -v {} -f --ocr".format(sdk_version, di_flag, fw_version)
        self.engine.run_cmd(install_cmd, validate=True)

    def install_sdk_and_burn_fw_flow(self, sdk_version):
        """
        install SDK on the switch and burn fw to it, with dedicated script
        """
        with allure.step("Install SDK & FW"):
            logger.info("installing SDK and FW on the switch")
            self.apply_mount()
            self.set_aliases()
            fw_version = self.get_fw_version_from_sdk(sdk_version)
            logger.info(f"Starting installation of SDK version: {sdk_version} and FW version: {fw_version}")

            try:
                self._install_sdk_and_fw(sdk_version, fw_version, debian_enabled=True)
            except Exception as e:
                logger.warning(f"Installation with debian enabled failed: {str(e)}. Trying without debian...")
                self._install_sdk_and_fw(sdk_version, fw_version, debian_enabled=False)

            self.dvs_restart()
            logger.info("SDK and FW installation has ended successfully!")

    def install_traffic_generator(self):
        """
        Function verifies the traffic generator is functional post deploy on DVS OS
        :return: None
        """
        sdk_version = self.get_sdk_version()
        sdk_branch = self.get_sdk_branch(sdk_version)
        self.overlay_perf_sys_sdk_to_sys_sdk(sdk_branch)

    def get_fw_version_from_sdk(self, sdk_version):
        fw_version_path = os.path.join(PerfConsts.SDK_VERSION_PATH, sdk_version, PerfConsts.FW_VERSION_FILE)
        fw_version = self.engine.run_cmd(f"cat {fw_version_path}")
        logger.info(f"FW version is {fw_version}")
        return fw_version

    def dvs_restart(self):
        logger.info("Performing restart to DVS")
        clean_switch_alias_cmd = f"alias clean_switch={PerfConsts.CLEAN_SWITCH_PATH}"
        self.engine.run_cmd_set([clean_switch_alias_cmd], validate=True)
        restart_cmd = "dvs_stop.sh && clean_switch && dvs_start.sh --sdk_bridge_mode=HYBRID --boot_mode=NORMAL"
        if self._get_chip_type_from_noga() == "SPC6":
            restart_cmd += f" --custom_config_file={PerfConsts.SPC6_DVS_CUSTOM_CONFIG_FILE}"
        self.engine.run_cmd(restart_cmd, validate=True)

    def _get_chip_type_from_noga(self):
        hostname = self.hostname().strip()
        noga_data = get_noga_entire_resource_data(resource_name=hostname)
        return noga_data[0]['chip_type']

    def apply_mount(self):
        with allure.step("Apply mount and configure switch"):
            hostname = self.hostname().strip()
            logger.info(f"Querying Noga for site information of {hostname}")

            try:
                noga_data = get_noga_entire_resource_data(resource_name=hostname)
                setup_site = noga_data[0]['site']

                logger.info(f"Adding mounts for {setup_site} site")
                cmd = f"SITE={setup_site} nis_add.sh"
                self.engine.run_cmd(cmd)
            except Exception as e:
                logger.error(f"Error querying Noga for site information: {e}")
                raise

            cmd = "prepare_switch_for_regression.sh"
            self.engine.run_cmd(cmd)

    def set_aliases(self):
        logger.info("Setting aliases for sdk install, clean switch and fw burn commands")
        sdk_install_alias_cmd = f"alias sdk_install={PerfConsts.SDK_INSTALL_PATH}"
        clean_switch_alias_cmd = f"alias clean_switch={PerfConsts.CLEAN_SWITCH_PATH}"
        fw_burn_alias_cmd = f"alias fw_burn={PerfConsts.FW_BURN_PATH}"
        self.engine.run_cmd_set([sdk_install_alias_cmd, clean_switch_alias_cmd, fw_burn_alias_cmd])

    def is_dut_supports_image(self, base_version_url, dut_name, cli_type) -> bool:
        """
        This method checks whether the given base version url is supported for the given dut , or not
        In the future this method will check the sdk version compatibility to fw
        :return: True/False
        """
        image_supports = True
        logger.info(
            f"dut: {dut_name} {'supports' if image_supports else 'does not support'} version: {base_version_url}")
        return image_supports

    def pre_installation_steps(self, context, threads_dict):
        """Execute DVS pre-installation steps"""
        DvsInstallationSteps.pre_installation_steps(context.setup_info)

    def post_installation_steps(self, context, image_helper=None):
        """Execute DVS post-installation steps"""
        DvsInstallationSteps.post_installation_steps(context.setup_info['duts'], context.target_version,
                                                     deploy_sequential=context.deploy_sequential)

    @staticmethod
    def _bin_path_from_dvs_release_properties(props_path):
        """Read ``BIN_PATH`` from ``dvs_release.properties``.

        Args:
            props_path: Absolute path to ``dvs_release.properties``.

        Returns:
            str: Installer path.

        Raises:
            ValueError: If the key is missing or the value is empty.
        """
        BIN_PATH_KEY = "BIN_PATH"
        prefix = f"{BIN_PATH_KEY}="
        with open(props_path, encoding='utf-8', errors='replace') as props_file:
            for line in props_file:
                line = line.strip()
                if line.startswith(prefix):
                    value = line.split('=', 1)[1].strip()
                    if not value:
                        raise ValueError(f"Empty {BIN_PATH_KEY} in {props_path}")
                    return value
        raise ValueError(f"{BIN_PATH_KEY} not found in {props_path}")

    @staticmethod
    def _resolve_dvs_os_installer_path(target_image_url):
        """Pick DVS OS ONIE installer path from deploy args or default GA image.

        Args:
            target_image_url: SDK release root (e.g. ``.../sx_sdk_eth/lastrc_master``), resolving
                symlinks to ``sx_sdk_eth-<ver>`` and reading ``BIN_PATH`` from ``dvs_release.properties``;
                or a direct path to ``*_installer.bin``; or a directory containing exactly one
                ``*installer.bin``. Empty uses ``PerfConsts.DVS_GA_IMAGE``.
                HTTP URLs are normalized to ``/auto/...`` NFS paths.

        Returns:
            str: NFS-style path passed to ONIE install (wget via nbu-nfs HTTP).

        Raises:
            ValueError: If ``dvs_release.properties`` exists but ``BIN_PATH`` is missing or empty.
            AssertionError: If a directory is given but the installer cannot be resolved.
        """
        path = str(target_image_url).strip()
        dir_path = os.path.realpath(path.rstrip('/'))
        if os.path.isdir(dir_path):
            props_file = os.path.join(dir_path, PerfConsts.DVS_LATEST_VERSION_FILE)
            if os.path.isfile(props_file):
                bin_path = DvsGeneralCli._bin_path_from_dvs_release_properties(props_file)
                logger.info(f"Using BIN_PATH from {props_file}: {bin_path}")
                return bin_path
        return path

    def deploy_image_steps(self, topology_obj, setup_name, platform_params, image_url, deploy_type,
                           apply_base_config, reboot_after_install, is_shutdown_bgp, fw_pkg_path,
                           target_image_url, destination_hwsku=None, setup_info=None, dut_alias=None,
                           fanout_deploy_threads=None, serial_log_analyzers=None, dut_ip='',
                           fanout_target_version=None):
        """Execute DVS deploy image steps.

        Uses ``target_image_url`` (SDK root like ``.../sx_sdk_eth/lastrc_master``) when set
        """
        dvs_image_path = self._resolve_dvs_os_installer_path(target_image_url)
        logger.info(f"DVS deploy installer path: {dvs_image_path}")
        self.deploy_image(dvs_image_path, topology_obj, dut_alias)
