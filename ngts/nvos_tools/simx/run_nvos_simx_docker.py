import logging
import pytest
import allure
import os
import time
import argparse
from retry import retry

from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.tools.test_utils.nvos_config_utils import set_base_configurations
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.conftest import devices
from ngts.helpers.object_filters import filter_objects

logger = logging.getLogger()

base_chipsim_script_path = "/auto/sw_system_project/NVOS_INFRA/ChipSim/{release_name}/nvos/scripts/run_nvos_in_chipsim.py"
master_folder_name = "nvos-master"
ci_chipsim_script_path = "{ci_temp_path}/scripts/run_nvos_in_chipsim.py"


def test_run_nvos_simx_docker(topology_obj, target_version, devices, use_bin_image, use_master_script, is_regression_run,
                              chip, platform, chipsim_version, custom_flags, chipsim_script_branch):
    with allure.step("Get server and dut details"):
        dut_engine = topology_obj.players['dut']['engine']
        server_name = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific']['serial_conn_command'].split()[1]
        dut_name = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Common']['Name']
        logger.info(f"DUT ip: {dut_engine.ip}({dut_name}) on server: {server_name}")

    with allure.step("Get path to chipsim script"):
        path_to_chipsim_script = _get_path_to_chipsim_script(is_regression_run, target_version, use_master_script,
                                                             chipsim_script_branch)
        logger.info(f"Path to chipsim script: {path_to_chipsim_script}")

    with allure.step("Start the NVOS simx docker"):
        if not use_bin_image:
            with allure.step("Get path to disk image"):
                target_version = _get_path_to_disk_image(target_version, is_regression_run)
                logger.info(f"Path to image: {target_version}")
        else:
            logging.info("'--use_bin_image' flag was provided, force using .bin image")

        for player_name, player in filter_objects(topology_obj.players, host_type='dut', engine_type='ssh').items():
            dut_engine = player["engine"]
            server_engine = ConnectionTool.create_ssh_conn(server_name, os.getenv("TEST_SERVER_USER"),
                                                           os.getenv("TEST_SERVER_PASSWORD")).returned_value
            start_simx_docker(target_version, dut_engine, server_engine, devices, path_to_chipsim_script,
                              chip, platform, chipsim_version, custom_flags)
            _wait_till_switch_is_ready(dut_engine)


def start_simx_docker(target_version, dut_engine, server_engine, devices, path_to_chipsim_script,
                      chip='', platform=None, chipsim_version=None, custom_flags=None):
    image_type = "--nos-image" if ".bin" in target_version else "--simulator-image"
    cmd = f"{path_to_chipsim_script} --ip {dut_engine.ip} {image_type} {target_version}"
    if chip:
        cmd += f" --chip {chip}"
    if platform:
        cmd += f" --platform {platform}"
    if chipsim_version:
        cmd += f" --chipsim-version {chipsim_version}"
    if custom_flags:
        cmd += f' --custom-flags " {custom_flags}"'
    logger.debug('Running chipsim command: ' + cmd)
    output = server_engine.run_cmd(cmd)
    time.sleep(5)
    assert any(msg in output for msg in ["NOS installed successfully", "Serial connection: telnet"]), "Failed to start simx docker"


def wait_till_the_switch_is_ready(switch_ip):
    try:
        switch_is_ready = ConnectionTool.ping_device(switch_ip)
    except BaseException:
        raise Exception("Timeout during simx docker initiation")

    assert switch_is_ready, "Failed to initiate simx docker components"
    logging.info("All simx docker components are active")


def test_wait_till_the_switch_is_ready(topology_obj):
    dut_engine = topology_obj.players['dut']['engine']
    server_name = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific']['serial_conn_command'].split()[1]
    logger.info(f"DUT ip: {dut_engine.ip} on server: {server_name}")
    _wait_till_switch_is_ready(dut_engine)


def _wait_till_switch_is_ready(dut_engine):
    with allure.step("Wait until the switch is ready (~5 min)"):
        wait_till_the_switch_is_ready(dut_engine.ip)

    with allure.step("Wait until ssh is ready"):
        wait_till_ssh_is_ready(dut_engine)

    with allure.step("Apply basic config"):
        set_base_configurations(dut_engine=dut_engine, apply=True)


@retry(Exception, tries=10, delay=5)
def wait_till_ssh_is_ready(dut_engine):
    dut_engine.run_cmd('nv show system version')


def _get_path_to_chipsim_script(is_regression_run, target_version, use_master_script, chipsim_script_branch):
    if is_regression_run:
        return _get_path_to_chipsim_script_for_regression(target_version, use_master_script, chipsim_script_branch)
    else:
        return _get_path_to_chipsim_script_for_ci(target_version)


def _get_path_to_chipsim_script_for_ci(target_version):
    """
    Convert a CI bin path to a CI source code path for chipsim script
    examples:
        from '/auto/sw_system_project/devops/sw-r2d2-bot/nos/nvos_ci/10475/nvos/nvos.bin'
        to '/auto/sw_system_project/devops/sw-r2d2-bot/nos/nvos_ci/10475/nvos_source_code'
    """
    ci_path = ci_chipsim_script_path.format(ci_temp_path=target_version.replace('/nvos/nvos.bin', '/nvos_source_code'))

    if not os.path.exists(ci_path):
        logging.warning(f"ChipSim script not found at: {ci_path}")
        logging.info("Using master version instead")
        ci_path = base_chipsim_script_path.format(release_name=master_folder_name)

    return ci_path


def _get_path_to_chipsim_script_for_regression(target_version, use_master_script, chipsim_script_branch):
    if chipsim_script_branch:
        version_name = chipsim_script_branch if chipsim_script_branch.startswith("nvos-") else f"nvos-{chipsim_script_branch}"
        logging.info(f"Using overridden chipsim branch: {version_name}")
    elif use_master_script:
        version_name = master_folder_name
        logging.info("Using master chipsim script")
    else:
        version_name = TestToolkit.version_path_to_release_name(target_version)
        logging.info(f"Using release {version_name} chipsim scrip")

    path_to_script = base_chipsim_script_path.format(release_name=version_name)

    if not os.path.exists(path_to_script):
        logging.warning(f"ChipSim script not found at: {path_to_script}")
        logging.info("Using master version instead")
        path_to_script = base_chipsim_script_path.format(release_name=master_folder_name)

    return path_to_script


def _get_path_to_disk_image(target_version, is_regression_run):
    if is_regression_run:
        logging.info("Regression run")
        return _get_path_to_disk_image_for_regression(target_version)
    else:
        logging.info("CI run")
        return _get_path_to_disk_image_for_ci(target_version)


def _get_path_to_disk_image_for_regression(target_version):
    """
    Convert a .bin file path to a .img file path for regression
    examples:
        from '/auto/sw_system_release/nos/nvos/25.02.5930-025/amd64/dev/nvos-amd64-25.02.5930-025.bin'
        to '/auto/sw_system_release/nos/nvos/25.02.5930-025/amd64/dev/nvos-disk-amd64-25.02.5930-025.img'
    """
    if target_version.endswith('.bin'):
        disk_image_path = target_version.replace('nvos-', 'nvos-disk-').replace('.bin', '.img')
    else:
        disk_image_path = target_version

    if os.path.exists(disk_image_path):
        return disk_image_path

    logging.warning(f"Disk image not found at: {disk_image_path}")
    logging.info("Using .bin file instead")
    return target_version


def _get_path_to_disk_image_for_ci(target_version):
    """
    Convert a .bin file path to a .img file path for CI
    examples:
        from '/auto/sw_system_project/devops/sw-r2d2-bot/nos/nvos_ci/10398/nvos/nvos.bin'
        to '/auto/sw_system_project/devops/sw-r2d2-bot/nos/nvos_ci/10398/nvos/nvos-disk.img'
    """
    if target_version.endswith('.bin'):
        disk_image_path = target_version.replace('nvos.bin', 'nvos-disk.img')
    else:
        disk_image_path = target_version

    if os.path.exists(disk_image_path):
        return disk_image_path

    logging.warning(f"Disk image not found at: {disk_image_path}")
    logging.info("Using .bin file instead")
    return target_version
