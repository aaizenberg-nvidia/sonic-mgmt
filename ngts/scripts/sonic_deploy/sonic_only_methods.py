import logging
import os
import subprocess
import requests
import json
import allure
import pytest
import sys
import time
import yaml
from pathlib import Path

from ngts.helpers import json_file_helper
from ngts.helpers.system_helpers import set_timezone as system_set_timezone
from ngts.scripts.sonic_deploy.image_preparetion_methods import is_url, get_sonic_branch
from ngts.constants.constants import MarsConstants, SonicDeployConstants, SonicConst, SimxCommunityConsts
from ngts.scripts.sonic_deploy.community_only_methods import deploy_minigpraph, \
    reboot_validation, execute_script, is_dualtor_topo, is_dualtor_aa_topo, generate_minigraph, \
    config_y_cable_simulator, add_host_for_y_cable_simulator
from retry.api import retry_call, retry
from ngts.helpers.run_process_on_host import run_background_process_on_host
from ngts.helpers.interface_helpers import get_service_port
from ngts.common.util import get_installed_dpu_info, get_specified_installed_dpus_from_noga

logger = logging.getLogger()


class SonicInstallationSteps:

    @staticmethod
    def is_multi_asic_platform(platform_params):
        if not platform_params:
            logger.warning("platform_params is empty, assuming single-ASIC device")
        return platform_params and "sn5800_ld" in platform_params.platform.lower()

    @staticmethod
    def pre_installation_steps_ha(sonic_topo, neighbor_type,
                                  base_version, target_version,
                                  setup_info, port_number,
                                  threads_dict, destination_hwsku, deploy_sequential=False):
        """
        Pre-installation steps for HA setup
        """
        setup_name = setup_info['setup_name']
        ansible_path = setup_info['ansible_path']
        # Get ptf docker tag
        ptf_tag = SonicInstallationSteps.get_ptf_tag_sonic(base_version, target_version)
        for dut in setup_info['duts']:
            dut_name = dut['dut_name']
            cached_hwsku = get_cached_hwsku(dut_name)
            if cached_hwsku:
                # override the individual setup files if found
                SonicInstallationSteps.override_hwsku_files({"setup_name": f"{dut_name}_setup"}, cached_hwsku)
                # remove any cached topos from the DUTs
                SonicInstallationSteps.remove_topologies(ansible_path=ansible_path, dut_names=[dut_name],
                                                         setup_name=None, sonic_topo=sonic_topo)
        # remove the HA topologies separately
        SonicInstallationSteps.override_hwsku_files(setup_info, destination_hwsku)
        SonicInstallationSteps.remove_topologies(ansible_path=ansible_path, dut_names=None,
                                                 setup_name=setup_name, sonic_topo=sonic_topo)
        SonicInstallationSteps.start_community_background_threads(threads_dict, setup_name,
                                                                  dut_name, sonic_topo, neighbor_type,
                                                                  ptf_tag, port_number,
                                                                  ansible_path, setup_info, destination_hwsku,
                                                                  deploy_sequential=deploy_sequential)

    @staticmethod
    def pre_installation_steps(
            sonic_topo, neighbor_type, base_version, target_version, setup_info, port_number, is_simx, threads_dict,
            destination_hwsku, is_performance=False, parallel=False, deploy_image_only=False, is_air=False,
            deploy_chipless=False, deploy_sequential=False):
        """
        Pre-installation steps for SONIC
        :param sonic_topo: the topo for SONiC testing, for example: t0, t1, t1-lag, ptf32
        :param neighbor_type: neighbor_type fixture
        :param base_version: base version
        :param target_version: target version if provided
        :param setup_info: dictionary with setup info
        :param port_number: number of DUT ports
        :param is_simx: fixture, True if setup is SIMX, else False
        :param threads_dict: dict, contain threads which will run in background
        :param destination_hwsku: the destination hwsku value
        :param is_performance: True if setup is performance, else False
        :param parallel: deploy testbed in parallel flag
        :param deploy_image_only: deploy image only flag
        :param deploy_chipless: deploy chipless flag
        :param deploy_sequential: perform deployment sequentially
        """
        setup_name = setup_info['setup_name']
        dut_name = setup_info['duts'][0]['dut_name']
        if setup_name.endswith('-ha'):
            # Run the pre-installation steps for HA setup
            SonicInstallationSteps.pre_installation_steps_ha(
                sonic_topo,
                neighbor_type,
                base_version,
                target_version,
                setup_info,
                port_number,
                threads_dict,
                destination_hwsku,
                deploy_sequential=deploy_sequential
            )
        elif is_community(sonic_topo):
            ansible_path = setup_info['ansible_path']
            SonicInstallationSteps.override_hwsku_files(setup_info, destination_hwsku, is_air)
            # Get ptf docker tag
            ptf_tag = SonicInstallationSteps.get_ptf_tag_sonic(base_version, target_version)
            dut_names = []
            for dut in setup_info['duts']:
                dut_names.append(dut['dut_name'])
            if deploy_image_only:
                logger.info("Skipping remove-topo as deploy_image_only is True")
                return
            with allure.step('Remove topologies'):
                cached_hwsku = get_cached_hwsku(dut_name)
                if cached_hwsku and cached_hwsku != destination_hwsku:
                    if "dual-tor" in setup_name:
                        SonicInstallationSteps.remove_topologies(ansible_path=ansible_path, dut_names=None,
                                                                 setup_name=setup_name, sonic_topo=sonic_topo, parallel=parallel)
                    logger.info(f"Copy the setup related file for the hwsku {cached_hwsku}")
                    SonicInstallationSteps.override_hwsku_files(setup_info, cached_hwsku)
                    SonicInstallationSteps.remove_topologies(ansible_path=ansible_path, dut_names=dut_names,
                                                             setup_name=None, sonic_topo=sonic_topo, parallel=parallel)
                else:
                    SonicInstallationSteps.remove_topologies(ansible_path=ansible_path,
                                                             dut_names=dut_names,
                                                             setup_name=setup_name,
                                                             sonic_topo=sonic_topo, parallel=parallel)
                if cached_hwsku and cached_hwsku != destination_hwsku:
                    SonicInstallationSteps.override_hwsku_files(setup_info, destination_hwsku)
            SonicInstallationSteps.start_community_background_threads(threads_dict, setup_name,
                                                                      dut_name, sonic_topo, neighbor_type,
                                                                      ptf_tag, port_number,
                                                                      ansible_path, setup_info, destination_hwsku,
                                                                      parallel=parallel,
                                                                      deploy_image_only=deploy_image_only,
                                                                      deploy_sequential=deploy_sequential)
            if is_dualtor_topo(sonic_topo):
                generate_minigraph(ansible_path, setup_info, setup_info['setup_name'], sonic_topo, port_number)
        elif is_performance:
            pass
        else:
            SonicInstallationSteps.start_canonical_background_threads(
                threads_dict,
                setup_name,
                dut_name,
                is_simx,
                deploy_chipless=deploy_chipless,
                deploy_sequential=deploy_sequential
            )

    @staticmethod
    def start_community_background_threads(threads_dict, setup_name, dut_name, sonic_topo, neighbor_type, ptf_tag,
                                           port_number, ansible_path, setup_info, hwsku, parallel=False,
                                           deploy_image_only=False, deploy_sequential=False):
        """
        Start background threads for community setup
        """
        if neighbor_type == 'vsonic':
            logger.info("Starting vsonic VMs")
            SonicInstallationSteps.start_vsonic_vms(ansible_path=ansible_path,
                                                    setup_name=setup_name,
                                                    dut_names=[dut_name],
                                                    sonic_topo=sonic_topo)
        is_scale_topo = sonic_topo in SonicDeployConstants.SCALE_TOPOLOGIES_LIST
        is_bgp_scale_topo = sonic_topo in SonicDeployConstants.BGP_SCALE_TOPOLOGIES_LIST

        if not deploy_image_only:
            add_topo_cmd = SonicInstallationSteps.get_add_topology_cmd(setup_name, dut_name, sonic_topo, neighbor_type,
                                                                       ptf_tag, hwsku, parallel)
            if is_scale_topo:
                add_topo_timeout = SonicDeployConstants.ADD_TOPO_TIMEOUT_SCALE
            elif is_bgp_scale_topo:
                add_topo_timeout = SonicDeployConstants.ADD_TOPO_TIMEOUT_BGP_SCALE
            else:
                add_topo_timeout = SonicDeployConstants.ADD_TOPO_TIMEOUT
            logger.info(f"Using add topology timeout: {add_topo_timeout}s for topology: {sonic_topo}")
            run_background_process_on_host(threads_dict, 'add_topology', add_topo_cmd, timeout=add_topo_timeout,
                                           exec_path=ansible_path, deploy_sequential=deploy_sequential)
        else:
            logger.info("Skipping add-topo as deploy_image_only is True")

    @staticmethod
    def copy_csv_inventory_lab(setup_name, destination_hwsku, is_air=False):
        base_path = os.path.dirname(os.path.realpath(__file__))
        common_csv_file_path = os.path.join(base_path, "../../../ansible/files/")
        setup_csv_file_path = os.path.join(common_csv_file_path, f"hwsku_vars/{setup_name}/*.csv")
        setup_hwsku_csv_file_path = os.path.join(common_csv_file_path,
                                                 f"hwsku_vars/{setup_name}/{destination_hwsku}/*.csv")

        common_inventory_lab_path = os.path.join(base_path, "../../../ansible/")
        setup_hwsku_inventory_path = os.path.join(common_csv_file_path,
                                                  f"hwsku_vars/{setup_name}/{destination_hwsku}/inventory")
        setup_hwsku_lab_path = os.path.join(common_csv_file_path,
                                            f"hwsku_vars/{setup_name}/{destination_hwsku}/lab")
        setup_hwsku_veos_path = os.path.join(common_csv_file_path,
                                             f"hwsku_vars/{setup_name}/veos")

        logger.info(f"Common csv files path: {common_csv_file_path}")
        logger.info(f"Copy {setup_name} - {destination_hwsku} related csv files to override the common csv files")
        os.system(f"cp -f {setup_hwsku_csv_file_path} {common_csv_file_path}")
        os.system(f"cp -f {setup_csv_file_path} {common_csv_file_path}")

        logger.info(f"Common inventory and lab files path: {common_inventory_lab_path}")
        logger.info(f"Copy {setup_name} - {destination_hwsku} related inventory and lab files to override the "
                    f"common inventory and lab files")
        os.system(f"cp -f {setup_hwsku_inventory_path} {common_inventory_lab_path}")
        os.system(f"cp -f {setup_hwsku_lab_path} {common_inventory_lab_path}")

        if os.path.exists(setup_hwsku_veos_path):
            logger.info(f"Copy {setup_name} - {destination_hwsku} related veos file to override the common veos file")
            os.system(f"cp -f {setup_hwsku_veos_path} {common_inventory_lab_path}")

        if is_air:
            air_setup_testbed_yaml_path = os.path.join(common_csv_file_path, f"hwsku_vars/{setup_name}/{destination_hwsku}/testbed.yaml")
            if os.path.exists(air_setup_testbed_yaml_path):
                logger.info(f"Copy {setup_name} - {destination_hwsku} related testbed.yaml file to override the common testbed.yaml file")
                os.system(f"cp -f {air_setup_testbed_yaml_path} {common_inventory_lab_path}")
            else:
                raise ValueError(f"testbed.yaml file not found for {setup_name} - {destination_hwsku}")

    @staticmethod
    def override_hwsku_files(setup_info, destination_hwsku, is_air=False):
        """
        Copy the csv/inventory/lab files under folder setup_name and folder hwsku to override the common files
        """
        setup_name = setup_info['setup_name']
        if 'dual-tor' in setup_name:
            SonicInstallationSteps.copy_csv_inventory_lab(setup_info['duts'][0]['dut_name'] + '_setup',
                                                          destination_hwsku)
        else:
            # Single DUT and HA setup
            SonicInstallationSteps.copy_csv_inventory_lab(setup_name, destination_hwsku, is_air)

    @staticmethod
    def start_canonical_background_threads(threads_dict, setup_name, dut_name, is_simx, deploy_chipless=False,
                                           deploy_sequential=False):
        """
        Start background threads for canonical setup
        """
        python_bin_path = sys.executable

        if not is_simx and not deploy_chipless:
            run_containers_cmd = SonicInstallationSteps.generate_run_containers_command(python_bin_path, setup_name)
            run_background_process_on_host(threads_dict, 'containers_bringup', run_containers_cmd, timeout=600,
                                           deploy_sequential=deploy_sequential)

        update_repo_cmd = SonicInstallationSteps.generate_update_sonic_mgmt_cmd(python_bin_path, dut_name, setup_name)
        run_background_process_on_host(threads_dict, 'update_sonic_mgmt', update_repo_cmd, deploy_sequential=deploy_sequential)

    @staticmethod
    def generate_run_containers_command(python_bin_path, setup_name):
        """
        Generate command which can run containers_bringup.py script
        :param python_bin_path: path to python interpreter
        :param setup_name: name of setup
        :return: string, command which will contain containers_bringup.py script with arguments
        """
        devts_path = SonicInstallationSteps.get_devts_path()
        cmd = f'{python_bin_path} {devts_path}/scripts/docker/containers_bringup.py ' \
            f'--setup_name {setup_name} --sonic_setup'
        return cmd

    @staticmethod
    def generate_update_sonic_mgmt_cmd(python_bin_path, dut_name, setup_name=None):
        """
        Generate command which can run update_sonic_mgmt.py script
        :param python_bin_path: path to python interpreter
        :param dut_name: name of DUT
        :return: string, command which will contain update_sonic_mgmt.py script with arguments
        """
        sonic_mgmt_path = os.path.abspath(__file__).split('/ngts/')[0]
        cmd = f'{python_bin_path} {sonic_mgmt_path}/sonic-tool/sonic_ngts/scripts/update_sonic_mgmt.py ' \
            f'--dut={dut_name} --mgmt_repo={sonic_mgmt_path}'
        if setup_name:
            cmd += f' --setup_name={setup_name}'
        return cmd

    @staticmethod
    def get_devts_path():
        """
        Get path to DevTS repository
        :return: string, path to DevTS repository
        """
        devts_path = None
        for path in sys.path:
            if path.endswith('devts'):
                devts_path = path
                break
        return devts_path

    @staticmethod
    def get_ptf_tag_sonic(base_version, target_version):
        """
        Getting ptf docker tag
        :param base_version: base version
        :param target_version: target version if provided
        :return: ptf_tag
        """
        with allure.step('Getting ptf docker tag'):
            if target_version:
                ptf_tag = SonicInstallationSteps.get_ptf_docker_tag(target_version)
            else:
                ptf_tag = SonicInstallationSteps.get_ptf_docker_tag(base_version)
        return ptf_tag

    @staticmethod
    def get_ptf_docker_tag(image_path):
        """
        Get PTF docker tag from SONiC image path
        :param image_path:
            example: /auto/sw_system_release/sonic/master.234-27a6641fb_Internal/Mellanox/sonic-mellanox.bin
        :return: ptf docker tag, example: '42007'
        """
        ptf_tag = MarsConstants.DEFAULT_PTF_TAG
        try:
            if is_url(image_path):
                file_path_index = 3
                image_path = '/' + '/'.join(image_path.split('/')[file_path_index:])
            branch = get_sonic_branch(image_path)
            logger.info('SONiC branch is: {}'.format(branch))
            ptf_tag = MarsConstants.BRANCH_PTF_MAPPING.get(branch, MarsConstants.DEFAULT_PTF_TAG)
        except Exception as err:
            logger.error('Can not get SONiC branch and PTF tag from path: {}, using "latest". Error: {}'.format(
                image_path, err))

        return ptf_tag

    @staticmethod
    def stop_vsonic_vms(ansible_path, setup_name, dut_names, sonic_topo):
        """
        The method removes the topologies to get the clear environment.
        """
        for dut_name in dut_names:
            cmd = SonicInstallationSteps.get_stop_start_sonic_vms_cmd(setup_name, dut_name, sonic_topo, "stop",
                                                                      "sonic-vs.img")
            try:
                execute_script(cmd, ansible_path, validate=False, timeout=1200)
            except Exception as err:
                logger.warning(f'Failed to stop for dut {dut_name}. Got error: {err}')

    @staticmethod
    def start_vsonic_vms(ansible_path, setup_name, dut_names, sonic_topo):
        """
        The method removes the topologies to get the clear environment.
        """
        for dut_name in dut_names:
            cmd = SonicInstallationSteps.get_stop_start_sonic_vms_cmd(setup_name, dut_name, sonic_topo, "start",
                                                                      "sonic-vs.img")
            try:
                execute_script(cmd, ansible_path, validate=False, timeout=2400)
            except Exception as err:
                logger.warning(f'Failed to start SONiC VMs for dut {dut_name}. Got error: {err}')

    @staticmethod
    def remove_topologies(ansible_path, dut_names, setup_name, sonic_topo, parallel=False):
        """
        The method removes the topologies to get the clear environment.
        """
        def _remove_topologies(setup, topo_list):
            logger.info(
                f"Remove topologies: {topo_list}. This may increase a chance to deploy a new one successful")
            cached_vm_type = get_cached_vm_type(setup)

            for topo in topo_list:
                if cached_vm_type == 'vsonic':
                    logger.info("Stopping vsonic VMs")
                    SonicInstallationSteps.stop_vsonic_vms(ansible_path=ansible_path,
                                                           setup_name=setup_name,
                                                           dut_names=dut_names,
                                                           sonic_topo=topo)
                cmd = "./testbed-cli.sh -k {NEIGHBOR_TYPE} remove-topo {SETUP}-{TOPO} vault".format(SETUP=setup, TOPO=topo, NEIGHBOR_TYPE=cached_vm_type)
                if parallel:
                    cmd += " --parallel"
                logger.info("Remove topo {}".format(topo))
                logger.info("Running CMD: {}".format(cmd))

                # Get timeout based on topology type
                if topo in SonicDeployConstants.SCALE_TOPOLOGIES_LIST or topo in SonicDeployConstants.BGP_SCALE_TOPOLOGIES_LIST:
                    remove_timeout = SonicDeployConstants.REMOVE_TOPO_TIMEOUT_SCALE
                else:
                    remove_timeout = SonicDeployConstants.REMOVE_TOPO_TIMEOUT
                logger.info(f"Using remove topology timeout: {remove_timeout}s for topology: {topo}")

                try:
                    execute_script(cmd, ansible_path, validate=True, timeout=remove_timeout)
                except Exception as err:
                    logger.warning(f'Failed to remove topology. Got error: {err}')

        logger.info("Removing topologies to get the clear environment")
        with allure.step("Remove Topologies (community step)"):
            if setup_name and (is_dualtor_topo(sonic_topo) or setup_name.endswith('-ha')):
                topologies = SonicInstallationSteps.get_topologies_to_remove(setup_name)
                _remove_topologies(setup_name, topologies)
            if dut_names:
                for dut_name in dut_names:
                    topologies = SonicInstallationSteps.get_topologies_to_remove(dut_name)
                    _remove_topologies(dut_name, topologies)

    @staticmethod
    def get_topologies_to_remove(dut_name):
        cached_topo = get_cached_topology(dut_name)
        if cached_topo:
            logger.info(f"Found cached topology: {cached_topo}, removing only this one")
            topos_to_remove = [cached_topo]
        else:
            if 'dual-tor' in dut_name:
                topos_to_remove = MarsConstants.TOPO_ARRAY_DUALTOR
            elif dut_name.endswith('-ha'):
                topos_to_remove = MarsConstants.TOPO_ARRAY_HA
            else:
                topos_to_remove = MarsConstants.TOPO_ARRAY
        return topos_to_remove

    @staticmethod
    def get_add_topology_cmd(setup_name, dut_name, sonic_topo, neighbor_type, ptf_tag, hwsku=None, parallel=False):
        testbed_file = ''
        if is_dualtor_topo(sonic_topo) or setup_name.endswith('-ha'):
            dut_name = setup_name
        cmd = "./testbed-cli.sh -k {NEIGHBOR_TYPE} -h {HWSKU} add-topo {SWITCH}-{TOPO} vault -e " \
              "ptf_imagetag={PTF_TAG} -vvvvv".format(SWITCH=dut_name,
                                                     TOPO=sonic_topo, PTF_TAG=ptf_tag, NEIGHBOR_TYPE=neighbor_type,
                                                     HWSKU=hwsku)
        if SonicInstallationSteps._is_use_converged_peers(setup_name, dut_name, sonic_topo):
            cmd += " -e max_fp_num=127"
        if parallel:
            cmd += " --parallel"
        return cmd

    @staticmethod
    def get_stop_start_sonic_vms_cmd(setup_name, dut_name, sonic_topo, action, sonic_file_name):
        testbed_file = ''
        if is_dualtor_topo(sonic_topo):
            dut_name = setup_name
            if is_dualtor_aa_topo(sonic_topo):
                testbed_file = '-t testbed.yaml'
        cmd = "./testbed-cli.sh {TESTBED_FILE} -k vsonic {ACTION}-topo-vms {SWITCH}-{TOPO} vault -e " \
              "{SONIC_FILE_NAME} -vvvvv".format(TESTBED_FILE=testbed_file, SWITCH=dut_name, TOPO=sonic_topo,
                                                ACTION=action, SONIC_FILE_NAME=sonic_file_name)
        return cmd

    @staticmethod
    def post_install_check(ansible_path, dut_name, sonic_topo):
        """
        Method which doing post install checks: check ports status, check dockers status, etc.
        """
        with allure.step("Post install check"):
            post_install_validation = "ansible-playbook -i inventory --limit {SWITCH} " \
                                      "post_upgrade_check.yml -e " \
                                      "topo={TOPO} -b -vvv".format(SWITCH=dut_name, TOPO=sonic_topo)
            logger.info("Performing post-install validation by running: {}".format(post_install_validation))
            return execute_script(cmd=post_install_validation, exec_path=ansible_path)

    @staticmethod
    def is_additional_apps_argument_is_deb_package(additional_apps_argument):
        is_deb_package = False
        path = additional_apps_argument
        try:
            if os.path.islink(additional_apps_argument):
                path = os.readlink(additional_apps_argument)
            if path.endswith('.deb'):
                is_deb_package = True
        except OSError:
            pass
        return is_deb_package

    @staticmethod
    def install_supported_app_extensions(ansible_path, setup_name, dut_name, app_extension_dict_path, sonic_topo):
        app_extension_path_str = ''
        if app_extension_dict_path:
            app_extension_path_str = '--app_extension_dict_path={}'.format(app_extension_dict_path)
        cmd = "{ngts_pytest} --setup_name={setup_name} --dut_name={dut_name} --rootdir={sonic_mgmt_dir}/ngts" \
              " -c {sonic_mgmt_dir}/ngts/pytest.ini --log-level=INFO" \
              " --clean-alluredir --alluredir=/tmp/allure-results --sonic-topo={sonic_topo}" \
              " --disable_loganalyzer {app_extension_path_str} " \
              " {sonic_mgmt_dir}/ngts/scripts/install_app_extension/install_app_extensions.py". \
            format(ngts_pytest=MarsConstants.NGTS_PATH_PYTEST, sonic_mgmt_dir=MarsConstants.SONIC_MGMT_DIR,
                   setup_name=setup_name, dut_name=dut_name, sonic_topo=sonic_topo,
                   app_extension_path_str=app_extension_path_str)
        logger.info("Running CMD: {}".format(cmd))
        execute_script(cmd, ansible_path)

    @staticmethod
    def check_bgp_is_shutdown(dut_engine):
        assert dut_engine.run_cmd("show ip route bgp") == "" and dut_engine.run_cmd("show ipv6 route bgp") == "", \
            "Not all bgp sessions are down"

    @staticmethod
    def is_additional_apps_argument_is_app_ext_dict(additional_apps_argument):
        is_app_ext_dict = False
        try:
            requests.get('{}/{}'.format(MarsConstants.HTTP_SERVER_NBU_NFS, additional_apps_argument)).json()
            is_app_ext_dict = True
        except json.decoder.JSONDecodeError:
            pass
        return is_app_ext_dict

    @staticmethod
    def copy_json_to_dut(json_content, filename, dest_path, dut_engine):
        with open(f'/tmp/{filename}', 'w') as f:
            json.dump(json_content, f, indent=4)
        os.chmod(f'/tmp/{filename}', 0o777)
        dut_engine.copy_file(source_file=f'/tmp/{filename}', dest_file=filename, file_system='/tmp/',
                             overwrite_file=True, verify_file=False)
        dut_engine.run_cmd(f'sudo cp /tmp/{filename} {dest_path}')

    @staticmethod
    def remove_redundant_service_port(dut_platform_path, hwsku, dut_engine, cli_obj):
        port_to_remove = 'Ethernet520'
        port_config_path = f'{dut_platform_path}/{hwsku}/port_config.ini'
        dut_engine.run_cmd(f'grep -v "{port_to_remove}" {port_config_path} > tmp_port_config')
        dut_engine.run_cmd(f'sudo mv tmp_port_config {port_config_path}')

        platform_json_path = f'{dut_platform_path}/platform.json'
        platform_json_obj = json_file_helper.get_platform_json(dut_engine, cli_obj)
        del platform_json_obj['interfaces'][port_to_remove]
        SonicInstallationSteps.copy_json_to_dut(platform_json_obj, 'platform.json', platform_json_path, dut_engine)

    @staticmethod
    def configure_single_service_port_mloop(dut_engine, platform):
        if platform not in SonicDeployConstants.SINGLE_SERVICE_PORT_PLATFORMS:
            return

        service_ports = get_service_port(platform)
        if not service_ports:
            raise Exception(f"Failed to resolve service ports for platform {platform}")

        persistent_mloop_dir = os.path.realpath(
            os.path.join(os.path.dirname(__file__), "../../../sonic-tool/persistent_mloop")
        )
        service_conf = "persistent_mloop.conf"
        script_name = "persistent_mloop.py"

        dut_engine.copy_file(source_file=os.path.join(persistent_mloop_dir, service_conf),
                             dest_file=service_conf,
                             file_system='/tmp',
                             direction='put')
        dut_engine.copy_file(source_file=os.path.join(persistent_mloop_dir, script_name),
                             dest_file=script_name,
                             file_system='/tmp',
                             direction='put')

        dut_engine.run_cmd(f"docker cp /tmp/{service_conf} syncd:/etc/supervisor/conf.d/{service_conf}", validate=True)
        dut_engine.run_cmd(f"docker cp /tmp/{script_name} syncd:/usr/bin/{script_name}", validate=True)
        dut_engine.run_cmd(f"docker exec syncd chmod +x /usr/bin/{script_name}", validate=True)

        ports_arg = " ".join(service_ports)
        dut_engine.run_cmd(f"docker exec syncd python3 /usr/bin/{script_name} --ports {ports_arg}", validate=True)

    @staticmethod
    def post_installation_steps(topology_obj, sonic_topo, recover_by_reboot, setup_name, platform_params,
                                apply_base_config, target_version, is_shutdown_bgp, reboot_after_install,
                                deploy_only_target, fw_pkg_path, reboot, additional_apps, setup_info, dut_alias,
                                is_performance, chip_type, deploy_dpu=False, xml_rpc=True, is_air=False,
                                custom_config_db_air_path=None, deploy_chipless=False):
        """
        Post-installation steps
        :param topology_obj: topology object
        :param sonic_topo: sonic_topo fixture
        :param recover_by_reboot: bool value
        :param setup_name: setup_name from NOGA
        :param platform_params: platform_params
        :param apply_base_config: apply_base_config
        :param target_version: target_version
        :param is_shutdown_bgp: bool value
        :param reboot_after_install:  bool value
        :param deploy_only_target:  bool value
        :param fw_pkg_path: path to FW pkg
        :param reboot: reboot fixture
        :param additional_apps: additional_apps fixture
        :param setup_info: dictionary with setup info
        :param dut_alias: alias of dut
        :param is_performance: True in case when setup is performance
        :param chip_type: the type of chip
        :param deploy_dpu: deploy dpu flag
        :param is_air: is_air fixture
        :param custom_config_db_air_path: path to custom config_db.json file
        :param deploy_chipless: deploy chipless flag
        """
        ansible_path = setup_info['ansible_path']
        cli = SonicInstallationSteps.get_dut_cli(setup_info)
        for dut in setup_info['duts']:
            cli = dut['cli_obj']
            # for the smartswitch, need to make sure all DPUs are aligned in admin status and oper status
            dut_alias = dut['dut_alias']
            dut_name = dut['dut_name']
            dpus_in_noga_str = get_specified_installed_dpus_from_noga(topology_obj, dut_alias, dut_name, default_dpus=None)
            if dpus_in_noga_str:
                with allure.step('Check if pmon is running'):
                    cli.cli_obj.general.verify_dockers_are_up(['pmon'])
                with allure.step('Check that the DPUs status is aligned'):
                    cli.cli_obj.general.verify_dpus_status_aligned([dpu_name.strip() for dpu_name in dpus_in_noga_str.upper().split(',')])
            else:
                logger.info(f"Not a smartswitch or no DPUs are specified in noga for the dut: {dut_name}({dut_alias}). Skipping the DPU status alignment check")

            apply_base_config = (
                True if deploy_chipless
                else False if is_performance
                else apply_base_config
            )
            cli.cli_obj.general.deploy_image_post_installtion(topology_obj, apply_base_config=apply_base_config,
                                                              setup_name=setup_name,
                                                              platform_params=platform_params,
                                                              reboot_after_install=reboot_after_install,
                                                              configure_dns=True, is_air=is_air, disable_ztp=True,
                                                              setup_info=setup_info,
                                                              dut_alias=dut_alias,
                                                              custom_config_db_air_path=custom_config_db_air_path,
                                                              deploy_chipless=deploy_chipless)
        dut_engine = topology_obj.players['dut']['engine']
        logger.info(f'Current hwsku in the platform_params is: {platform_params["platform"]}')

        for dut in setup_info['duts']:
            cli = dut['cli_obj']
            cli.enable_async_route_feature(platform_params['platform'], platform_params['hwsku'])

        if SonicInstallationSteps.is_multi_asic_platform(platform_params=platform_params):
            logger.info(f"Multi-ASIC platform {platform_params['platform']} detected")

        if not is_community(sonic_topo) and not is_performance:
            if not deploy_chipless:
                # Enable Port Init Profile for Canonical setups
                logger.info("Prepare sai.xml files for Port Init feature testing")
                cli.update_sai_xml_file(platform_params['platform'], platform_params['hwsku'], global_flag=True,
                                        local_flags=False, platform_params=platform_params)

        # Disable IM on t1-isolated-d32u1s2 topo temporarily for hwsku Mellanox-SN5640-C508O1X2
        # TODO: It should be removed after the software control supported.
        if "t1-isolated-d32u1s2" in sonic_topo:
            dut_engine.run_cmd("sudo cmis_host_mgmt.py --disable")

        # Community only steps
        if is_community(sonic_topo):
            if is_dualtor_topo(sonic_topo) and 'dualtor-aa' not in sonic_topo:
                config_y_cable_simulator(ansible_path=ansible_path, setup_name=setup_name, sonic_topo=sonic_topo)
                for dut in setup_info['duts']:
                    add_host_for_y_cable_simulator(dut, setup_info)
            if setup_name.endswith('-ha'):
                deploy_minigpraph(ansible_path=ansible_path, dut_name=setup_name, sonic_topo=sonic_topo,
                                  recover_by_reboot=False, topology_obj=topology_obj,
                                  cli_objs=None, deploy_dpu=deploy_dpu,
                                  dut_ip=setup_info['duts'][0].get('dut_ip', ''))
            elif is_dualtor_topo(sonic_topo):
                deploy_minigpraph(ansible_path=ansible_path, dut_name=setup_name, sonic_topo=sonic_topo,
                                  recover_by_reboot=False, topology_obj=topology_obj,
                                  cli_objs=[cli],
                                  dut_ip=setup_info['duts'][0].get('dut_ip', ''))
            else:
                for dut in setup_info['duts']:
                    general_cli_obj = dut['cli_obj']
                    deploy_minigpraph(ansible_path=ansible_path, dut_name=dut['dut_name'], sonic_topo=sonic_topo,
                                      recover_by_reboot=recover_by_reboot, topology_obj=topology_obj,
                                      cli_objs=[general_cli_obj], deploy_dpu=deploy_dpu,
                                      dut_ip=dut.get('dut_ip', ''))
                    if deploy_dpu:
                        dut['engine'].run_cmd('sudo config save -y')
                    if is_air:
                        logger.info(f"Restarting determine-reboot-cause service on AIR platform {dut['dut_name']}")
                        general_cli_obj.cli_obj.general.restart_determine_reboot_cause()
            logger.info("Deploying DASH API")
            with allure.step('Apply DNS servers configuration'):
                logger.info("Applying DNS servers configuration")
                for dut in setup_info['duts']:
                    general_cli_obj = dut['cli_obj']
                    topology_obj.players[dut['dut_alias']]['engine'].disconnect()
                    general_cli_obj.cli_obj.ip.apply_dns_servers_into_resolv_conf(is_air_setup=is_air)
                    general_cli_obj.save_configuration()
            logger.info("Validating Post Install DUT configuration")
            sync_docker_time_to_israel(topology_obj)

        for dut in setup_info['duts']:
            SonicInstallationSteps.upgrade_switch(topology_obj=topology_obj, dut_name=dut['dut_name'],
                                                  setup_name=setup_name, platform_params=platform_params,
                                                  sonic_topo=sonic_topo, deploy_type='sonic',
                                                  apply_base_config=apply_base_config, target_version=target_version,
                                                  is_shutdown_bgp=is_shutdown_bgp, ansible_path=ansible_path,
                                                  reboot_after_install=reboot_after_install,
                                                  deploy_only_target=deploy_only_target, fw_pkg_path=fw_pkg_path,
                                                  cli=dut['cli_obj'], chip_type=chip_type)

        for dut in setup_info['duts']:
            if additional_apps:
                SonicInstallationSteps.install_app_extension_sonic(dut_name=dut['dut_name'], setup_name=setup_name,
                                                                   additional_apps=additional_apps,
                                                                   ansible_path=ansible_path,
                                                                   sonic_topo=sonic_topo)

        for dut in setup_info['duts']:
            # Disconnect ssh connection, prevent "Socket is closed" in case when previous steps did reboot
            topology_obj.players[dut['dut_alias']]['engine'].disconnect()

        if not is_community(sonic_topo) and not is_performance:
            if xml_rpc:
                # deploy the xmlrpc, the traffic may loss right after the xml rpc server is started
                # Get all traffic hosts (ha-*/hb-* players with engines)
                traffic_hosts = [name for name in topology_obj.players.keys()
                                 if name.startswith(('ha-', 'hb-')) and 'engine' in topology_obj.players[name]]

                if traffic_hosts:
                    logger.info(f"Starting XML-RPC servers on traffic hosts: {traffic_hosts}")
                    for host in traffic_hosts:
                        try:
                            logger.info(f"Starting XML-RPC server on {host}")
                            topology_obj.players[host]['engine'].start_xml_rcp_server()
                        except Exception as e:
                            logger.error(f"Failed to start XML-RPC server on {host}: {e}")
                    logger.info(f"✓ Successfully started XML-RPC servers on all {len(traffic_hosts)} traffic hosts")
                else:
                    logger.info("No traffic hosts (ha/hb) found in topology, skipping XML-RPC server startup")

            if (not deploy_dpu) and 'bobcat' in setup_name:
                with allure.step('Disable DPUs for darkmode'):
                    for dut in setup_info['duts']:
                        dut_alias = dut['dut_alias']
                        dut_name = dut['dut_name']
                        cli_obj = dut['cli_obj']
                        # TODO parallelize this
                        _, dpu_index_list, _ = get_installed_dpu_info(topology_obj, dut_alias, dut_name)
                        cli_obj.shutdown_dpus(dpu_index_list)
                        cli_obj.shutdown_dpu_data_interfaces(setup_name)
                        cli_obj.save_configuration()
            if deploy_chipless:
                with allure.step('Bring up pmon docker for chipless deploy'):
                    SonicInstallationSteps.bring_up_pmon_for_chipless(dut_engine)
            else:
                # Only check port status at canonical setup, there is an ansible counterpart for community setup
                for dut in setup_info['duts']:
                    ports_list = topology_obj.players_all_ports[dut['dut_alias']]
                    dut['cli_obj'].cli_obj.interface.check_link_state(ports_list)

    @staticmethod
    def bring_up_pmon_for_chipless(dut_engine):
        """
        Deploy chipless steps
        """
        dut_engine.run_cmd("sudo config feature state syncd disabled", validate=False)
        dut_engine.run_cmd("sudo config feature state swss disabled", validate=False)
        dut_engine.run_cmd("sudo config save -y", validate=False)
        dut_engine.run_cmd("sudo sed -i '/^After=syncd.service$/d' /usr/lib/systemd/system/pmon.service", validate=False)
        dut_engine.run_cmd("sudo sed -i '/sx_core/d' /usr/bin/pmon.sh", validate=False)
        dut_engine.run_cmd("sudo docker rm pmon", validate=False)
        dut_engine.run_cmd("sudo systemctl daemon-reload", validate=False)
        dut_engine.run_cmd("sudo systemctl reset-failed pmon", validate=False)
        dut_engine.run_cmd("sudo systemctl restart pmon", validate=False)

    @staticmethod
    def dpu_post_installation_steps(topology_obj, sonic_topo, recover_by_reboot, setup_name, setup_info):

        # Common for community and canonical setup
        logger.info("Deploying DASH API")
        with allure.step('Update the dash api in sonic-mgmt'):
            try:
                retry_call(fetch_dash_api_package, tries=1, delay=2, logger=logger)
            except Exception as e:
                logger.error(f"Failed to download the dash api package from artifact server: {e}")
                logger.info("Copying the dash api to sonic-mgmt from local nfs")
                try:
                    subprocess.run(["scp", "/auto/sw_system_release/sonic/internal/bjb/dash_deb/libdashapi_1.0.0_amd64.deb", "./libdashapi_1.0.0_amd64.deb"],
                                   check=True, capture_output=False, text=True)
                except Exception as e:
                    logger.error(f"Failed to scp the dash api to sonic-mgmt: {e}")
                    raise
            logger.info("Extracting the dash api to /tmp/dashapi_extract")
            subprocess.run(["dpkg-deb", "-x", "libdashapi_1.0.0_amd64.deb", "/tmp/dashapi_extract"],
                           check=True, capture_output=False, text=True)
            src_path = subprocess.run(
                ["find", "/tmp/dashapi_extract", "-type", "d", "-path", "*dist-packages/dash_api"],
                check=True, capture_output=True, text=True).stdout.strip()
            logger.info(f"Found the dash api python package in {src_path}")
            dst_path = "/opt/venv/lib/python3.12/site-packages/"
            logger.info(f"Copying the dash api python package to {dst_path}")
            subprocess.run(["sudo", "cp", "-r", src_path, dst_path],
                           check=True, capture_output=False, text=True)
            logger.info("Successfully copied the dash api python package to the venv")

        if is_community(sonic_topo):
            # only for community setup
            ansible_path = setup_info['ansible_path']
            if setup_name.endswith('-ha'):
                deploy_minigpraph(ansible_path=ansible_path, dut_name=setup_name, sonic_topo=sonic_topo,
                                  recover_by_reboot=False, topology_obj=topology_obj,
                                  cli_objs=None, deploy_dpu=True, config_dpu=True,
                                  dut_ip=setup_info['duts'][0].get('dut_ip', ''))
            else:
                for dut in setup_info['duts']:
                    general_cli_obj = dut['cli_obj']
                    deploy_minigpraph(ansible_path=ansible_path, dut_name=dut['dut_name'], sonic_topo=sonic_topo,
                                      recover_by_reboot=recover_by_reboot, topology_obj=topology_obj,
                                      cli_objs=[general_cli_obj], deploy_dpu=True, config_dpu=True,
                                      dut_ip=dut.get('dut_ip', ''))
            logger.info("Validating DPU configuration")
            for dut in setup_info['duts']:
                # TODO parallelize this
                general_cli_obj = dut['cli_obj']
                dut_engine = dut['engine']
                dut_engine.run_cmd('sudo config save -y')

        # For both community and canonical setup
        logger.info("Applying NAT config to smartSwitch")
        with allure.step('Apply NAT config to smartSwitch'):
            for dut in setup_info['duts']:
                enable_nat_from_dut_mgmt_to_dpu_mgmt_intf(dut['engine'])

    @staticmethod
    def get_dut_cli(setup_info):
        cli = None
        for dut in setup_info['duts']:
            if dut['dut_alias'] == 'dut':
                cli = dut['cli_obj']
                break
        return cli

    @staticmethod
    def deploy_image(cli, topology_obj, setup_name, platform_params, image_url, deploy_type,
                     apply_base_config, reboot_after_install,
                     is_shutdown_bgp, fw_pkg_path,
                     destination_hwsku=None,
                     setup_info=None, dut_alias=None, fanout_deploy_threads=None,
                     docker_list=None, fanout_target_version=None):
        """
        This method will deploy sonic image on the dut.
        :param topology_obj: topology object
        :param setup_name: setup_name from NOGA
        :param platform_params: platform_params
        :param image_url: path to sonic version to be installed
        :param deploy_type: deploy_type
        :param apply_base_config: apply_base_config
        :param reboot_after_install: reboot_after_install
        :param is_shutdown_bgp: shutdown bgp flag, True or False
        :param fw_pkg_path: fw_pkg_path
        :param cli : SONIC cli object
        :param destination_hwsku: the destination hwsku value
        :param setup_info: setup information
        :param dut_alias: dut alias, such as 'dut-b'
        :param fanout_deploy_threads: dict contains fanout deploy background threads
        :param docker_list : List of docker name to validate
        :param fanout_target_version :Path to target version of fanout.
        :return: raise assertion error in case of script failure
        """
        dut_engine = None
        try:
            # TODO: Temp workaround for overcoming ipv6 ssh issue
            os.system("sudo /bin/sh -c 'echo \"precedence ::ffff:0:0/96 100\" >> /etc/gai.conf'")
            # when bgp is up, dut can not access the external IP such as nbu-mtr-nfs.nvidia.com. So shutdown bgp
            # for sonic only (is_shutdown_bgp is False for NVOS)
            if is_shutdown_bgp:
                with allure.step('Shutdown bgp'):
                    dut_engine = cli.engine
                    dut_engine.run_cmd('sudo config bgp shutdown all', validate=True)
                    logger.info("Wait all bgp sessions are down")
                    retry_call(SonicInstallationSteps.check_bgp_is_shutdown,
                               fargs=[dut_engine],
                               tries=6,
                               delay=10,
                               logger=logger)

            with allure.step('Deploy sonic image on the dut'):
                disable_ztp = False
                if deploy_type == 'onie':
                    disable_ztp = True
                cli.deploy_image(topology_obj=topology_obj, image_path=image_url, apply_base_config=apply_base_config,
                                 setup_name=setup_name, platform_params=platform_params,
                                 deploy_type=deploy_type,
                                 reboot_after_install=reboot_after_install, fw_pkg_path=fw_pkg_path,
                                 disable_ztp=disable_ztp, configure_dns=True, destination_hwsku=destination_hwsku,
                                 setup_info=setup_info, dut_alias=dut_alias,
                                 deploy_fanout_threads=fanout_deploy_threads,
                                 docker_list=docker_list,
                                 fanout_target_version=fanout_target_version)

        except Exception as err:
            raise AssertionError(err)
        finally:
            # for sonic only (is_shutdown_bgp is False for NVOS)
            if is_shutdown_bgp and dut_engine:
                with allure.step('Startup bgp'):
                    dut_engine.run_cmd('sudo config bgp startup all', validate=True)

    @staticmethod
    def post_install_check_sonic(sonic_topo, dut_name, ansible_path):
        """
        Method which doing post install checks: check ports status, check dockers status, etc.
        :param sonic_topo: the topo for SONiC testing, for example: t0, t1, t1-lag, ptf32
        :param dut_name: dut name
        :param ansible_path: path to ansible directory
        """
        SonicInstallationSteps.post_install_check(ansible_path=ansible_path, dut_name=dut_name,
                                                  sonic_topo=sonic_topo)

    @staticmethod
    def upgrade_switch(topology_obj, dut_name, setup_name, platform_params, sonic_topo, deploy_type,
                       apply_base_config, target_version, is_shutdown_bgp, ansible_path,
                       reboot_after_install, deploy_only_target, fw_pkg_path, cli, chip_type, set_timezone='Israel'):
        """
        Upgrade switch to the target version
        :param topology_obj: topology object
        :param dut_name: dut name
        :param setup_name: setup name
        :param platform_params: platform params
        :param sonic_topo: the topo for SONiC testing, for example: t0, t1, t1-lag, ptf32
        :param deploy_type: deploy type - 'onie', 'sonic'
        :param apply_base_config: bool value
        :param target_version: path to target version
        :param is_shutdown_bgp: bool value
        :param ansible_path: path to ansible directory
        :param reboot_after_install: bool value
        :param deploy_only_target: bool value
        :param fw_pkg_path: path to FW pkg
        :param cli: cli - SonicCli / NvueCli
        :param chip_type: chip_type - chip generation installed at platform
        :param set_timezone: set timezone
        """
        if target_version and not deploy_only_target:
            with allure.step("Upgrade switch to the target version"):
                logger.info("Target version is defined, upgrade switch again to the target version.")
                SonicInstallationSteps.deploy_image(topology_obj=topology_obj, setup_name=setup_name,
                                                    image_url=target_version, platform_params=platform_params,
                                                    deploy_type=deploy_type,
                                                    apply_base_config=apply_base_config,
                                                    reboot_after_install=reboot_after_install,
                                                    is_shutdown_bgp=is_shutdown_bgp, fw_pkg_path=fw_pkg_path, cli=cli)

                if not is_community(sonic_topo):
                    required_reload_config = cli.cli_obj.im.enable_im(topology_obj=topology_obj, platform_params=platform_params,
                                                                      chip_type=chip_type, enable_im=True)
                    if required_reload_config:
                        logger.info("IM is configured, but required to reload config to apply IM configuration")
                        cli.cli_obj.general.reload_flow(
                            ports_list=None, topology_obj=topology_obj, reload_force=True,
                            platform_params=platform_params)

            with allure.step("Set dut NTP timezone to {} time.".format(set_timezone)):
                cli.engine.disconnect()
                system_set_timezone(cli.engine, set_timezone)

            # There could be new running config generated by the new image
            with allure.step("Save new running config to config_db.json"):
                cli.cli_obj.qos.reload_qos()
                cli.cli_obj.general.verify_dockers_are_up()
                cli.cli_obj.general.enable_info_logging_on_swss()
                cli.cli_obj.general.save_configuration()

            with allure.step("Post installation check for community setup"):
                if is_community(sonic_topo):
                    SonicInstallationSteps.post_install_check(ansible_path=ansible_path, dut_name=dut_name,
                                                              sonic_topo=sonic_topo)
            cli.engine.run_cmd('sudo sonic-installer cleanup -y')

    @staticmethod
    def reboot_validation_sonic(dut_name, sonic_topo, reboot, ansible_path):
        """
        Reboot validation
        :param dut_name: dut name
        :param sonic_topo: the topo for SONiC testing, for example: t0, t1, t1-lag, ptf32
        :param reboot: whether reboot the switch after deploy. Default: 'no'
        :param ansible_path: path to ansible directory
        """
        if reboot and reboot != "no":
            reboot_validation(ansible_path=ansible_path, reboot=reboot, dut_name=dut_name, sonic_topo=sonic_topo)

    @staticmethod
    def install_app_extension_sonic(dut_name, setup_name, additional_apps, ansible_path, sonic_topo):
        """
        Install supported app extension
        :param dut_name: dut name
        :param setup_name: setup name
        :param additional_apps: additional apps
        :param ansible_path: path to ansible directory
        """
        app_extension_dict_path = additional_apps
        if app_extension_dict_path:
            with allure.step("Install supported app extension"):
                SonicInstallationSteps.install_supported_app_extensions(ansible_path=ansible_path,
                                                                        setup_name=setup_name,
                                                                        app_extension_dict_path=app_extension_dict_path,
                                                                        dut_name=dut_name,
                                                                        sonic_topo=sonic_topo)

    @staticmethod
    def verify_hw_management_version(engine):
        lowest_valid_version = '7.0020.3100'
        with allure.step('Getting the hw-management version from dut'):
            output = engine.run_cmd('dpkg -l | grep hw-management')

        with allure.step('Comparing the hw-management version with the lowest valid version'):
            version = output.split()[2]
            version = version.split('mlnx.')[-1]
            assert version >= lowest_valid_version, \
                'Current hw-management version {} is lower than the required version {}.'.format(
                    version, lowest_valid_version)

    @staticmethod
    def _is_use_converged_peers(setup_name, dut_name, sonic_topo):
        conf_name = setup_name if (is_dualtor_topo(sonic_topo) or setup_name.endswith("-ha")) else f"{dut_name}-{sonic_topo}"
        testbed_yaml = os.path.join(os.path.dirname(__file__), "../../../ansible/testbed.yaml")
        with open(testbed_yaml, "r") as f:
            tb = yaml.safe_load(f) or []
        entry = next((x for x in tb if x.get("conf-name") == conf_name), None)
        return bool(entry and entry.get("use_converged_peers", False))


def is_community(sonic_topo):
    if sonic_topo:
        return sonic_topo != 'ptf-any'


def update_hosts_file(dut_ip, dut_name, sonic_topo, setup_name):
    os.system(f"echo '{dut_ip} {dut_name}' >> /etc/hosts")
    if is_community(sonic_topo):
        os.system(f"echo '{SimxCommunityConsts.SERVER_DOCKER_IP} {setup_name}-hypervisor' >> /etc/hosts")


def get_cached_topology(dut_name):
    cached_topo = None
    cached_topo_path = f"{MarsConstants.SONIC_MARS_BASE_PATH}/cached_deployed_topologies/"
    setup_cached_topo_file = Path(f"{cached_topo_path}/{dut_name}")
    if setup_cached_topo_file.is_file():
        cached_topo_vm = setup_cached_topo_file.read_text().strip()
        if ',' in cached_topo_vm:
            cached_topo = cached_topo_vm.split(',')[0].strip()
        else:
            cached_topo = cached_topo_vm
        if cached_topo not in MarsConstants.TOPO_ARRAY:
            logger.info(f"There is a garbage in the cache file, {cached_topo} is not in {MarsConstants.TOPO_ARRAY}"
                        " removing all topologies")
            cached_topo = None
    return cached_topo


def get_cached_vm_type(dut_name):
    cached_vm_type = 'ceos'
    cached_topo_vm_type_path = f"{MarsConstants.SONIC_MARS_BASE_PATH}/cached_deployed_topologies/"
    setup_cached_topo_file = Path(f"{cached_topo_vm_type_path}/{dut_name}")
    if setup_cached_topo_file.is_file():
        topo_vm_type = setup_cached_topo_file.read_text().strip()
        if ',' in topo_vm_type:
            cached_vm_type = topo_vm_type.split(',')[1].strip()
    return cached_vm_type


def get_cached_hwsku(dut_name):
    cached_hwsku = None
    cached_topo_vm_type_path = f"{MarsConstants.SONIC_MARS_BASE_PATH}/cached_deployed_topologies/"
    setup_cached_topo_file = Path(f"{cached_topo_vm_type_path}/{dut_name}")
    if setup_cached_topo_file.is_file():
        topo_vm_type = setup_cached_topo_file.read_text().strip()
        if ',' in topo_vm_type:
            cached_vars = topo_vm_type.split(',')
            if len(cached_vars) >= 3:
                cached_hwsku = cached_vars[2].strip()
    return cached_hwsku


def enable_nat_from_dut_mgmt_to_dpu_mgmt_intf(engine):
    is_bookworm = "bookworm" in engine.run_cmd("cat /etc/os-release")
    sysctl_file = "/etc/sysctl.conf" if is_bookworm else "/usr/lib/sysctl.d/90-sonic.conf"
    enable_nat_cmds = [
        "sudo su",
        f"sudo echo net.ipv4.ip_forward=1 >> {sysctl_file}",
        f"sudo echo net.ipv4.conf.eth0.forwarding=1 >> {sysctl_file}",
        f"sudo sysctl -p {sysctl_file}",
        "sudo sysctl net.ipv4.ip_forward",
        "sudo sysctl net.ipv4.conf.eth0.forwarding",
        "sudo iptables -t nat -A POSTROUTING -s 169.254.200.0/24 -o eth0 -j MASQUERADE",
        "sudo iptables -t nat -A POSTROUTING -o bridge-midplane -d 169.254.200.0/24 -j SNAT --to-source 169.254.200.254",
        "sudo iptables -t nat -A PREROUTING -i eth0 -p tcp --dport 5021 -j DNAT --to-destination 169.254.200.1:22",
        "sudo iptables -t nat -A PREROUTING -i eth0 -p tcp --dport 5022 -j DNAT --to-destination 169.254.200.2:22",
        "sudo iptables -t nat -A PREROUTING -i eth0 -p tcp --dport 5023 -j DNAT --to-destination 169.254.200.3:22",
        "sudo iptables -t nat -A PREROUTING -i eth0 -p tcp --dport 5024 -j DNAT --to-destination 169.254.200.4:22",
        "sudo iptables -t nat -L",
        "sudo iptables-save > /etc/iptables/rules.v4",
        "exit"
    ]
    engine.run_cmd_set(enable_nat_cmds)


def fetch_dash_api_package():
    timeout = 15
    tries = 1
    rc = os.system(f"timeout {timeout} wget -t {tries} "
                   "'https://sonic-build.azurewebsites.net/api/sonic/artifacts?branchName=master&"
                   "definitionId=1055&artifactName=sonic-buildimage.amd64.ubuntu20_04&"
                   "target=libdashapi_1.0.0_amd64.deb' -O libdashapi_1.0.0_amd64.deb")
    assert rc == 0, "Failed to fetch the dash api package"


def update_container_timezone(hypervisor_engine, container):
    """
    Updates the timezone of a specific container to Israel time.
    :param hypervisor_engine: The engine to run commands on the hypervisor.
    :param container: The name of the container to update.
    """
    cmd = f"docker exec {container} ln -sf /usr/share/zoneinfo/Asia/Jerusalem /etc/localtime"
    hypervisor_engine.run_cmd(cmd, validate=True)
    logger.info(f"Timezone updated for container: {container}")


def sync_docker_time_to_israel(topology_obj):
    """
    Runs the NTP update process in all containers in the hypervisor.
    :param topology_obj : A fixture that returns setup players.
    """
    hypervisor_engine = None
    failed_containers = []

    try:

        hypervisor_engine = topology_obj.players['hypervisor']['engine']

        result = hypervisor_engine.run_cmd("docker ps --format {{.Names}}", validate=True)
        container_names = result.strip().splitlines()

        if not container_names or all(name.strip() == "" for name in container_names):
            raise Exception("No containers found")

        logger.info("Starting timezone update for all containers.")

        for container in container_names:
            if 'net_vm' not in container:
                with allure.step(f"Updating timezone for container: {container}"):
                    try:
                        retry_call(
                            update_container_timezone,
                            fargs=[hypervisor_engine, container],
                            tries=3,
                            delay=2,
                            logger=logger,
                        )
                    except Exception as e:
                        logger.error(f"Failed to update timezone for {container}. Error: {str(e)}")
                        failed_containers.append(container)

        if len(failed_containers) == 0:
            logger.info("Timezone update completed for all containers.")
        else:
            logger.warning(f"Timezone update failed for {failed_containers}.")
    except Exception as e:
        logger.warning(f"Unexpected error while updating timezones: {str(e)}")


def detect_asic_count(engine, platform_params, raise_on_error=False):
    """
    Detect the actual number of ASICs by reading asic.conf file.
    This is a shared utility function to avoid code duplication across modules.

    :param engine: SSH engine object for running commands
    :param platform_params: Platform parameters dict
    :param raise_on_error: If True, raises exception on failure; if False, returns default count
    :return: Number of ASICs detected
    :raises Exception: If raise_on_error is True and reading asic.conf fails
    """
    asic_count = None

    try:
        if SonicInstallationSteps.is_multi_asic_platform(platform_params=platform_params):
            asic_conf_path = SonicConst.MultiAsic.ASIC_CONF_PATH.format(PLATFORM=platform_params.platform)
            read_cmd = f"cat {asic_conf_path} | grep '^NUM_ASIC=' | cut -d'=' -f2"
            asic_count = int(engine.run_cmd(read_cmd, validate=True).strip())
            logger.info(f"{asic_count} ASIC(s) from asic.conf found in {asic_conf_path}")
        else:
            asic_count = SonicConst.DEFAULT_ASIC_COUNT
    except Exception as e:
        logger.warning(f"✗ Failed to read asic.conf: {e}")
        if raise_on_error:
            raise Exception(f"Failed to read asic.conf: {e}")

    return asic_count


def validate_and_get_asic_count(platform_params):
    """
    Validate that asic_count is provided for multi-ASIC platforms and return it.
    This prevents silent partial configurations by failing fast when asic_count is missing.

    :param platform_params: Platform parameters dict
    :return: asic_count value from platform_params
    :raises ValueError: If platform is multi-ASIC but asic_count is not specified
    """
    if not platform_params:
        raise ValueError("platform_params is required but not provided")

    if not SonicInstallationSteps.is_multi_asic_platform(platform_params=platform_params):
        # Not a multi-ASIC platform, this function shouldn't be called
        raise ValueError(
            f"validate_and_get_asic_count() called for non-multi-ASIC platform: "
            f"{platform_params.get('platform')}"
        )

    asic_count = platform_params.get('asic_count')
    if asic_count is None:
        raise ValueError(
            f"Multi-ASIC platform '{platform_params.get('platform')}' detected, "
            f"but 'asic_count' is not specified in platform_params. "
            f"Cannot proceed with partial configuration."
        )

    return asic_count


@retry(Exception, tries=20, delay=5)
def wait_for_system_table_to_exist(engine, asic_id=None):
    """
    Wait for SYSTEM_READY|SYSTEM_STATE table to exist in Redis STATE_DB
    :param engine: SSH engine object
    :param asic_id: ASIC ID for multi-ASIC systems (e.g., 0, 1, 2...). None for global/single-ASIC
    """
    asic_ns = f"-n asic{asic_id} " if asic_id is not None else ""
    cmd = f'sonic-db-cli {asic_ns}STATE_DB hgetall "SYSTEM_READY|SYSTEM_STATE"'
    output = engine.run_cmd(cmd)

    if '(empty array)' in output:
        asic_info = f" for ASIC {asic_id}" if asic_id is not None else ""
        logger.info(f'Waiting for SYSTEM_STATUS table to be available{asic_info}')
        raise Exception(f"System is not ready yet{asic_info}")
    return True


def wait_for_system_ready(engine, platform_params=None):
    """
    Wait for system to be ready by checking Redis STATE_DB
    For single-ASIC: checks global namespace only
    For multi-ASIC: checks global namespace + all ASIC namespaces
    :param engine: SSH engine object
    :param platform_params: Platform parameters dict (optional, None for single-ASIC)
    :return: True if system is ready
    """
    # Always check global namespace first
    logger.info("Checking global namespace Redis/system readiness...")
    wait_for_system_table_to_exist(engine)
    logger.info("✓ Global namespace is ready")

    # If multi-ASIC, check each ASIC namespace as well
    if SonicInstallationSteps.is_multi_asic_platform(platform_params=platform_params):
        asic_count = detect_asic_count(engine, platform_params, raise_on_error=False)
        logger.info(f"Multi-ASIC platform: checking Redis/system readiness for {asic_count} ASIC namespaces")

        for asic_id in range(asic_count):
            logger.info(f"Checking ASIC {asic_id} namespace...")
            wait_for_system_table_to_exist(engine, asic_id=asic_id)
            logger.info(f"✓ ASIC {asic_id} is ready")

        logger.info(f"✓ All {asic_count} ASIC namespaces are ready")

    return True
