import logging
import os
import shlex
import subprocess
import time

import allure
import pytest
from retry import retry

from ngts.helpers.object_filters import filter_objects
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.conftest import devices  # noqa: F401  fixture: populates TestToolkit.devices
from ngts.tools.test_utils.nvos_config_utils import set_base_configurations

logger = logging.getLogger(__name__)

ci_chipsim_script_path = "{ci_temp_path}/scripts/run_nvos_in_chipsim.py"
nvos_repo_url = "https://gitlab-master.nvidia.com/nbu-sws/nos/nvos.git"


# the devices fixture populates TestToolkit.devices, needed by version_path_to_release_name
@pytest.mark.usefixtures("devices")
def test_run_nvos_simx_docker(topology_obj, target_version, use_bin_image, use_master_script, is_regression_run,
                              chip, platform, chipsim_version, custom_flags, chipsim_extra_args, pelican_tag,
                              cable_script, chipsim_force, chipsim_script_branch):
    with allure.step("Get server and dut details"):
        dut_engine = topology_obj.players['dut']['engine']
        noga_attributes = topology_obj.players['dut']['attributes'].noga_query_data['attributes']
        server_name = noga_attributes['Specific']['serial_conn_command'].split()[1]
        dut_name = noga_attributes['Common']['Name']
        logger.info("DUT ip: %s(%s) on server: %s", dut_engine.ip, dut_name, server_name)

    server_engine = ConnectionTool.create_ssh_conn(server_name, os.getenv("TEST_SERVER_USER"),
                                                   os.getenv("TEST_SERVER_PASSWORD")).returned_value

    with allure.step("Get path to chipsim script"):
        path_to_chipsim_script, clone_dir = _get_path_to_chipsim_script(is_regression_run, target_version,
                                                                        use_master_script, chipsim_script_branch,
                                                                        server_engine)
        logger.info("Path to chipsim script: %s", path_to_chipsim_script)

    try:
        with allure.step("Start the NVOS simx docker"):
            if not use_bin_image:
                with allure.step("Get path to disk image"):
                    target_version = _get_path_to_disk_image(target_version, is_regression_run)
                    logger.info("Path to image: %s", target_version)
            else:
                logging.info("'--use_bin_image' flag was provided, force using .bin image")

            for player in filter_objects(topology_obj.players, host_type='dut', engine_type='ssh').values():
                dut_engine = player["engine"]
                start_simx_docker(target_version, dut_engine, server_engine, path_to_chipsim_script,
                                  chip, platform, chipsim_version, custom_flags, chipsim_extra_args, pelican_tag,
                                  cable_script, chipsim_force)
                _wait_till_switch_is_ready(dut_engine)
    finally:
        if clone_dir:
            try:
                server_engine.run_cmd(f"rm -rf {clone_dir}")
            except Exception:
                logger.warning("Failed to remove %s on the server", clone_dir, exc_info=True)


def start_simx_docker(target_version, dut_engine, server_engine, path_to_chipsim_script,
                      chip='', platform=None, chipsim_version=None, custom_flags=None,
                      chipsim_extra_args=None, pelican_tag=None, cable_script=None, chipsim_force=False):
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
    if pelican_tag:
        cmd += f" --pelican-tag {shlex.quote(pelican_tag)}"
    if cable_script:
        cmd += f" --cable-script {shlex.quote(cable_script)}"
    if chipsim_force:
        cmd += " --force"
    if chipsim_extra_args:
        cmd += " " + chipsim_extra_args.strip()
    logger.debug("Running chipsim command: %s", cmd)
    output = server_engine.run_cmd(cmd)
    time.sleep(5)
    assert any(msg in output for msg in ["NOS installed successfully", "Serial connection: telnet"]), \
        "Failed to start simx docker"


def wait_till_the_switch_is_ready(switch_ip):
    try:
        switch_is_ready = ConnectionTool.ping_device(switch_ip)
    except BaseException as err:
        raise Exception("Timeout during simx docker initiation") from err

    assert switch_is_ready, "Failed to initiate simx docker components"
    logging.info("All simx docker components are active")


def test_wait_till_the_switch_is_ready(topology_obj):
    dut_engine = topology_obj.players['dut']['engine']
    noga_attributes = topology_obj.players['dut']['attributes'].noga_query_data['attributes']
    server_name = noga_attributes['Specific']['serial_conn_command'].split()[1]
    logger.info("DUT ip: %s on server: %s", dut_engine.ip, server_name)
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


def _get_path_to_chipsim_script(is_regression_run, target_version, use_master_script, chipsim_script_branch,
                                server_engine):
    """Return (script_path, clone_dir); clone_dir is a temp dir on the server to remove after use, or None"""
    if not (chipsim_script_branch or use_master_script or is_regression_run):
        # CI keeps the exact source snapshot of the tested build next to the bin
        ci_path = ci_chipsim_script_path.format(
            ci_temp_path=target_version.replace('/nvos/nvos.bin', '/nvos_source_code'))
        if os.path.exists(ci_path):
            return ci_path, None
        logging.warning("CI chipsim script not found at: %s, using master branch instead", ci_path)

    if chipsim_script_branch:
        branch = chipsim_script_branch
    elif use_master_script or not is_regression_run:
        branch = "master"
    else:
        # release names ('nvos-25-03-0100') are also the names of the release branches in the nvos repo
        branch = TestToolkit.version_path_to_release_name(target_version)
    logging.info("Using chipsim script from branch: %s", branch)
    cloned = _clone_chipsim_script(server_engine, branch)

    if not cloned and branch != "master" and not chipsim_script_branch:
        logging.warning("Clone of release branch %s failed, falling back to master", branch)
        branch = "master"
        cloned = _clone_chipsim_script(server_engine, branch)

    assert cloned, f"Failed to clone chipsim script from branch '{branch}'"
    return cloned


def _clone_chipsim_script(server_engine, branch):
    """
    Sparse-clone an nvos branch locally (~7s, ~6MB) so the script is never stale, then scp
    the script files to the server (hypervisors have no git).
    Returns (script_path, clone_dir on the server) or None if any step failed.
    """
    clone_dir = f"/tmp/nvos-chipsim-{os.getpid()}-{int(time.time())}"
    try:
        clone = subprocess.run(
            f"git clone --quiet --depth 1 --filter=blob:none --sparse --single-branch"
            f" --branch {shlex.quote(branch)} {nvos_repo_url} {clone_dir}"
            f" && git -C {clone_dir} sparse-checkout set scripts platform/mellanox",
            shell=True, capture_output=True, text=True, timeout=300, check=False)
        if clone.returncode != 0:
            logging.warning("Failed to clone nvos branch %s: %s", branch, clone.stderr.strip())
            return None

        copy = subprocess.run(
            f"tar -C {clone_dir} -cz scripts platform | "
            f"sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
            f" {os.getenv('TEST_SERVER_USER')}@{server_engine.ip}"
            f" 'mkdir -p {clone_dir} && tar -xz -C {clone_dir}'",
            shell=True, capture_output=True, text=True, timeout=300, check=False,
            env={**os.environ, "SSHPASS": os.getenv("TEST_SERVER_PASSWORD") or ""})
        if copy.returncode != 0:
            logging.warning("Failed to copy chipsim script to server: %s", copy.stderr.strip())
            server_engine.run_cmd(f"rm -rf {clone_dir}")
            return None

        logging.info("Cloned nvos branch %s chipsim script to %s on server", branch, clone_dir)
        return ci_chipsim_script_path.format(ci_temp_path=clone_dir), clone_dir
    except Exception as err:
        logging.warning("Chipsim script clone failed: %s", err)
        return None
    finally:
        subprocess.run(f"rm -rf {clone_dir}", shell=True, check=False)


def _get_path_to_disk_image(target_version, is_regression_run):
    """
    Convert supported .bin image names to their disk-image equivalents for both CI and regression runs.

    examples:
        from '/auto/sw_system_project/devops/sw-r2d2-bot/nos/nvos_ci/10398/nvos/nvos.bin'
        to '/auto/sw_system_project/devops/sw-r2d2-bot/nos/nvos_ci/10398/nvos/nvos-disk.img'

        from '/auto/sw_system_release/nos/nvos/25.02.5930-025/amd64/dev/nvos-amd64-25.02.5930-025.bin'
        to '/auto/sw_system_release/nos/nvos/25.02.5930-025/amd64/dev/nvos-disk-amd64-25.02.5930-025.img'
    """
    logging.info("Regression run" if is_regression_run else "CI run")

    if target_version.endswith('.bin'):
        image_directory, image_name = os.path.split(target_version)
        disk_image_name = image_name.replace('nvos', 'nvos-disk', 1).replace('.bin', '.img')
        disk_image_path = os.path.join(image_directory, disk_image_name)
    else:
        disk_image_path = target_version

    if os.path.exists(disk_image_path):
        return disk_image_path

    logging.warning("Disk image not found at: %s", disk_image_path)
    logging.info("Using .bin file instead")
    return target_version
