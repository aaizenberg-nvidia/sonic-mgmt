import allure
import logging
import pytest
import re
import csv
import os
import json
import shlex
import subprocess

from retry.api import retry, retry_call
from ngts.cli_util.cli_parsers import generic_sonic_output_parser
from ngts.helpers.json_file_helper import extract_fw_data, normalize_firmware_chassis_name
from ngts.constants.constants import MarsConstants
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.helpers.secure_boot_helper import SonicSecureBootHelper
from ngts.helpers.shared_fanout_helper import (
    is_shared_fanout, software_control_enabled, software_control_warning,
)
from ngts.tests.conftest import get_dut_loopbacks
from ngts.constants.constants import FILE_INCLUDE_FAILED_SANITY_CHECKER_CASE
from ngts.tests.nightly.sanity_checker.analyze_sanity_checker_result_and_take_action import write_failed_sanity_checker_cases_to_file
from ngts.tools.sysdumps import copy_dump_file
from tests.common.helpers.firmware_helper import (
    parse_firmware_status, get_bmc_version_from_firmware_data,
    get_bmc_info_from_firmware_data, resolve_bmc_flavor, load_bmc_creds
)
from tests.common.helpers.sonic_image_readme import resolve_sonic_image_readme_path

pytestmark = [
    pytest.mark.disable_loganalyzer,
    pytest.mark.not_fail_config_check
]

logger = logging.getLogger()

POSSIBLE_CPLD_LIST = ['CPLD1', 'CPLD2', 'CPLD3', 'CPLD4']
CURRENT_PATH = os.path.dirname(os.path.abspath(__file__))

BIOS_RESTORE_SKIP_HOSTS = {
    'r-bobcat-01',  # BIOS restore fails on this host, remove when the setup is fixed.
    'slm-174',  # BIOS update fails on this host.
}
BIOS_INSTALL_REBOOT_TIMEOUT = 900

# Component version check constants
COMPONENT_SCRIPT_NAME = "get_component_versions.py"
README_COVERED_COMPONENTS = ['SDK', 'FW', 'SAI', 'HW_MANAGEMENT', 'MFT', 'KERNEL', 'RSHIM']
FW_DEFAULT_VERSIONS = ['ONIE', 'SSD', 'BIOS', 'CPLD']  # Expected columns of the table if the setup is SIMX
COMMANDS_FOR_ACTUAL = {
    "MFT": ["dpkg -l | grep -e 'mft '", "mft *([0-9.-]*)"],
    "RSHIM": ["dpkg -l | grep -e 'rshim '", "rshim *([0-9.-]*)"],
    "HW_MANAGEMENT": ["dpkg -l | grep hw", ".*1\\.mlnx\\.([0-9.]*)"],
    "SDK": ["docker exec -it syncd bash -c 'dpkg -l | grep sdk'", ".*1\\.mlnx\\.([0-9.]*)"],
    "SAI": ["docker exec -it syncd bash -c 'dpkg -l | grep mlnx-sai'", ".*1\\.mlnx\\.([A-Za-z0-9.]*)"],
    "FW": ["sudo mlxfwmanager -d $(/usr/bin/asic_detect/asic_detect.sh -p) --query", "FW * [0-9]{2}\\.([0-9.]*)"],
    "KERNEL": ["uname -r", "(.*)-[a-z0-9]+$"]
}
OPTIONAL_COMMANDS_FOR_ACTUAL = {
    "RSHIM": ["dpkg -l | grep -e 'rshim '", "rshim *([0-9.]*)"]
}
ALL_COMMANDS_FOR_ACTUAL = {**COMMANDS_FOR_ACTUAL, **OPTIONAL_COMMANDS_FOR_ACTUAL}

# non-existent versions are versions that aren't supposed to appear, like BIOS compilation versions while unexpected
# missing versions are components that aren't available on the current setup, like fw versions on simx setups.
NON_EXISTENT_VERSION = '-'
UNEXPECTED_MISSING_VERSION = 'N/A'
VAR_CORE_DIR = '/var/core'


@pytest.fixture(scope='module')
def sonic_topo():
    return "ptf-any"


@pytest.fixture(scope='module')
def is_in_deploy_image_flow(request):
    # When some sanity checker cases run in the following scripts, it will not fail the case when case fail
    script_in_deploy_image_flow_list = ["test_deploy_and_upgrade.py", "test_apply_configuration.py"]
    if request.node.name in script_in_deploy_image_flow_list:
        logger.info("sanity checker cases are running in deploy flow script")
        return True
    else:
        logger.info("sanity checker cases are not running in deploy flow script")
        return False


@pytest.fixture(scope='module', autouse=True)
def clear_file_inlcude_failed_sanity_check_case():
    if os.path.exists(FILE_INCLUDE_FAILED_SANITY_CHECKER_CASE):
        os.remove(FILE_INCLUDE_FAILED_SANITY_CHECKER_CASE)


@pytest.fixture(scope='module')
def platform_json_data(topology_obj):
    platform_json_data = topology_obj.players['dut']['cli'].chassis.get_platform_json_data()
    yield platform_json_data


@pytest.fixture(scope='function')
def skip_for_liquid_cooling_platform(platform_params):
    if '_ld' in platform_params.platform:
        pytest.skip("Skip the testcases on liquid cooling platforms")


@pytest.fixture(scope='function')
def enable_and_disable_fanout_lldp(request, engines, topology_obj, interfaces):
    """
    Pytest fixture which is enabling lldp on fanout and disable it when teardown
    :param request: request object fixture
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    """

    def should_skip_fanout(topology_obj, player_alias):
        """Check if fanout should be skipped (shared resource)"""
        player = topology_obj.players.get(player_alias)
        if not is_shared_fanout(player):
            return False
        hostname = player["attributes"].noga_query_data["attributes"]["Common"]["Name"]
        logger.info(f"Skipping LLDP enable/disable on shared fanout {hostname}")
        return True

    def enable_lldp(engine):
        logger.info(f"enable lldp on {engine.ip}")
        cmd_enalbe_lldp = "sudo config feature state lldp enabled" if engine.device_type == "linux" else "lldp"
        engine.run_cmd(cmd_enalbe_lldp)

    def disable_lldp(engine):
        logger.info(f"disable lldp on {engine.ip}")
        cmd_disable_lldp = "sudo config feature state lldp disabled" if engine.device_type == "linux" else "no lldp"
        engine.run_cmd(cmd_disable_lldp)
        if engine.device_type == "linux":
            # When disable lldp, we need to stop and remove the lldp container
            # because if lldp container is not removed, run 'docker container stop lldp' still return success
            # so it might affect the later case such as qos sai case
            engine.run_cmd("docker container stop lldp")
            engine.run_cmd("docker container rm lldp")

    fanout_skipped = should_skip_fanout(topology_obj, "fanout")
    fanout_b_skipped = False

    if not fanout_skipped:
        enable_lldp(engines.fanout)
    if 'fanout_b' in engines:
        fanout_b_skipped = should_skip_fanout(topology_obj, "fanout-b")
        if not fanout_b_skipped:
            enable_lldp(engines.fanout_b)

    yield

    if not fanout_skipped:
        disable_lldp(engines.fanout)
    if 'fanout_b' in engines and not fanout_b_skipped:
        disable_lldp(engines.fanout_b)


@pytest.mark.sanity_checker_common
def test_cpld_version_check(topology_obj, engines, platform_params, cli_objects, request, is_in_deploy_image_flow, sonic_topo):
    """
    This test validates that the CPLD version(s) deployed on the dut are the latest approved ones,
    as defined in the firmware.json versions file. If not, try it install the latest one.
    If case fail,
        The following actions will be handled in analyze_sanity_checker_result_and_take_action.py
        1. For sonic_tigon_r-tigon-15, sonic_anaconda_r-anaconda-15, the regression will be stopped by mars
        2. For the remaining setups,
           we will raise the failed case information in the allure report and disable bug handler tool
    :param engines: engines fixture
    :param platform_params: platform_params fixture
    """
    if "dpu" in sonic_topo:
        pytest.skip("DPU platform does not support this case")
    is_test_failed = False
    with allure.step('Getting info about the CPLD component from firmware.json'):
        cpld_component_data = None
        defined_cpld = None
        for cpld in POSSIBLE_CPLD_LIST:
            try:
                cpld_component_data = SonicSecureBootHelper.get_component_data(platform_params, cpld)
                defined_cpld = cpld
                break
            except Exception as e:
                logger.info(e)
                pass
        if not (defined_cpld and cpld_component_data):
            err_msg = "Failed to get the data for any CPLD from the firmware.json"
            assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
            is_test_failed = True

    with allure.step('Getting info about CPLD from dut'):
        component_versions_dict = get_info_about_current_components_version_dict(engines.dut)

    with allure.step(f'Checking CPLD version for: {defined_cpld}'):
        _, latest_cpld_ver = SonicSecureBootHelper.get_latest_expected_cpld(cpld_component_data, defined_cpld)
        current_cpld_ver = component_versions_dict[defined_cpld]
        if current_cpld_ver != latest_cpld_ver:
            if not is_in_deploy_image_flow:
                dut_topology_obj = topology_obj.players['dut']['cli']
                try:
                    with allure.step(f'Shutdown bpg all'):
                        dut_topology_obj.bgp.shutdown_bgp_all()
                    with allure.step(f'Restore CPLD to {latest_cpld_ver}'):
                        logger.info(f"Restore CPLD to the expected one:{latest_cpld_ver}")
                        SonicSecureBootHelper.restore_cpld(cli_objects, engines, topology_obj, platform_params, defined_cpld)
                    with allure.step("disconnect dut"):
                        engines.dut.disconnect()
                    with allure.step(f" After power cycle, check containers and interfaces are up"):
                        dut_topology_obj.general.verify_dockers_are_up()
                        check_port_oper_up_on_admin_up(dut_topology_obj)

                    with allure.step(f"Check if the cpld version is updated to {latest_cpld_ver}"):
                        component_versions_dict = get_info_about_current_components_version_dict(engines.dut)
                        current_cpld_ver = component_versions_dict[defined_cpld]
                        assert current_cpld_ver == latest_cpld_ver, \
                            f'Current {defined_cpld} version: {current_cpld_ver} is not latest: {latest_cpld_ver}'
                except Exception as err:
                    raise Exception(f"Fail to restore cpld \n. {err}")
                finally:
                    with allure.step(f'Start bpg all'):
                        dut_topology_obj.bgp.startup_bgp_all()
            else:
                logger.error(
                    f"The current CPLD {current_cpld_ver} ver does not match the latest one {latest_cpld_ver}")
                is_test_failed = True

    write_failed_case_name(is_test_failed, request.node.name, is_in_deploy_image_flow)


@pytest.mark.sanity_checker_common
def test_bios_version_check(topology_obj, engines, platform_params, cli_objects,
                            request, is_in_deploy_image_flow, sonic_topo):
    """
    Validate that the BIOS version on the DUT matches the target version defined in firmware.json.
    If there is a mismatch, restore BIOS via fwutil install and wait for the automatic cold reboot.
    """
    if "dpu" in sonic_topo:
        pytest.skip("DPU platform does not support this case")

    hostname = platform_params.get('host_name', '')
    if hostname in BIOS_RESTORE_SKIP_HOSTS:
        pytest.skip(f"Host {hostname} is in BIOS restore skip list, skipping BIOS version check")

    is_test_failed = False

    with allure.step('Getting target BIOS version from firmware.json'):
        try:
            fw_pkg_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)).split("ngts")[0],
                MarsConstants.UPDATED_FW_TAR_PATH
            )
            fw_data = extract_fw_data(fw_pkg_path)
            chassis = normalize_firmware_chassis_name(platform_params.filtered_platform)
            chassis_data = fw_data.get('chassis', {}).get(chassis, {})
            component = dict(chassis_data.get('component', {}))
            if 'host' in fw_data and hostname in fw_data['host']:
                for comp_type in list(fw_data['host'][hostname]['component'].keys()):
                    component[comp_type] = fw_data['host'][hostname]['component'][comp_type]
            bios_revisions = component.get('BIOS')
            if not bios_revisions:
                err_msg = f"BIOS component not found in firmware.json for chassis {chassis}"
                assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
                is_test_failed = True
                write_failed_case_name(is_test_failed, request.node.name, is_in_deploy_image_flow)
                return
            target_version = bios_revisions[0]['version']
            target_firmware_url = bios_revisions[0]['firmware']
            logger.info(f"Target BIOS version for chassis {chassis}: {target_version}")
        except Exception as e:
            err_msg = f"Failed to get BIOS data from firmware.json: {e}"
            assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
            is_test_failed = True
            write_failed_case_name(is_test_failed, request.node.name, is_in_deploy_image_flow)
            return

    with allure.step('Getting current BIOS version from DUT'):
        component_versions_dict = get_info_about_current_components_version_dict(engines.dut)
        current_bios = component_versions_dict.get('BIOS')
        logger.info(f"Current BIOS: {current_bios}, Target BIOS: {target_version}")

    if current_bios == target_version:
        logger.info("BIOS version matches target. No restore needed.")
        write_failed_case_name(is_test_failed, request.node.name, is_in_deploy_image_flow)
        return

    with allure.step(f'BIOS version mismatch detected: current={current_bios}, target={target_version}'):
        if is_in_deploy_image_flow:
            logger.error(f"BIOS version {current_bios} does not match target {target_version}")
            is_test_failed = True
        else:
            try:
                dut_cli = topology_obj.players['dut']['cli']

                with allure.step('Verify dockers are up before BIOS install'):
                    cli_objects.dut.general.verify_dockers_are_up()

                fw_filename = os.path.basename(target_firmware_url.rstrip('/'))
                local_fw_path = f'/tmp/{fw_filename}'
                dut_fw_path = f'/tmp/{fw_filename}'

                with allure.step(f'Download BIOS firmware from {target_firmware_url}'):
                    try:
                        logger.info(f"Downloading BIOS firmware to NGTS:{local_fw_path}")
                        subprocess.run(
                            ['wget', '--timeout=60', '--continue', '-O', local_fw_path, target_firmware_url],
                            check=True)
                        logger.info(f"Copying BIOS firmware from NGTS:{local_fw_path} to DUT:{dut_fw_path}")
                        engines.dut.copy_file(source_file=local_fw_path, dest_file=fw_filename,
                                              file_system='/tmp/', direction='put')
                    finally:
                        if os.path.exists(local_fw_path):
                            os.remove(local_fw_path)

                with allure.step(f'Install BIOS via fwutil (will trigger automatic cold reboot)'):
                    install_cmd = f'sudo fwutil install chassis component BIOS fw {dut_fw_path} -y'
                    logger.info(f"Running: {install_cmd}")
                    engines.dut.engine.send_config_set([install_cmd], exit_config_mode=False, cmd_verify=False)

                port_check_delay = 10
                port_check_tries = BIOS_INSTALL_REBOOT_TIMEOUT // port_check_delay

                with allure.step(f'Wait for switch shutdown (max {BIOS_INSTALL_REBOOT_TIMEOUT}s)'):
                    check_port_status_till_alive(False, engines.dut.ip, engines.dut.ssh_port)

                with allure.step('Disconnect DUT'):
                    engines.dut.disconnect()

                with allure.step(f'Wait for switch bring-up (max {BIOS_INSTALL_REBOOT_TIMEOUT}s)'):
                    check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port,
                                                 tries=port_check_tries, delay=port_check_delay)

                with allure.step('Wait for CLI to be ready'):
                    retry_call(engines.dut.run_cmd, fargs=[""], tries=80, delay=2, logger=logger)

                with allure.step('Verify dockers and ports are up after BIOS restore'):
                    cli_objects.dut.general.verify_dockers_are_up()
                    check_port_oper_up_on_admin_up(dut_cli)

                with allure.step(f'Verify BIOS version is now {target_version}'):
                    component_versions_dict = get_info_about_current_components_version_dict(engines.dut)
                    new_bios = component_versions_dict.get('BIOS')
                    assert new_bios == target_version, \
                        f"BIOS version after restore is {new_bios}, expected {target_version}"
                    logger.info(f"BIOS successfully restored to {target_version}")

            except Exception as err:
                is_test_failed = True
                err_msg = f"Failed to restore BIOS: {err}"
                assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)

    write_failed_case_name(is_test_failed, request.node.name, is_in_deploy_image_flow)


@pytest.mark.sanity_checker_airspin
def test_export_sonic_mgmt_location_env_var():
    """
    This test is to verify that the SONIC_MGMT_LOCATION environment variable is set correctly.
    And export it to the sonic-mgmt docker bashrc.
    """
    assert os.getenv('SONIC_MGMT_LOCATION', '').lower() == 'air', \
        "SONIC_MGMT_LOCATION environment variable is not set correctly"
    env_exists_in_bashrc = os.system("sudo grep 'export SONIC_MGMT_LOCATION=air' /root/.bashrc") == 0
    if not env_exists_in_bashrc:
        logger.info("SONIC_MGMT_LOCATION environment variable is not set in bashrc, adding it.")
        os.system("echo 'export SONIC_MGMT_LOCATION=air' >> /root/.bashrc")
    else:
        logger.info("SONIC_MGMT_LOCATION environment variable is already set in bashrc.")


@pytest.mark.sanity_checker_ci
@pytest.mark.sanity_checker_common
@pytest.mark.sanity_checker_airspin
def test_device_asic_check(engines, platform_params):
    """
    This test is verify that device asic status is ok.
    If case fail, the consequent regression steps will be stopped by mars
    """
    regrex_pci_dvice_name = ".*PCI Device Name:(?P<pci_device_name>.*)"
    device_info = engines.dut.run_cmd('sudo mlxfwmanager --query')
    pci_device_name = None
    for line in device_info.split("\n"):
        res = re.search(regrex_pci_dvice_name, line.strip())
        if res:
            pci_device_name = res.groupdict()["pci_device_name"]
            logger.info(f"pic device name is {pci_device_name}")
    assert pci_device_name, "device asic is not up"


@pytest.mark.sanity_checker_community
def test_shared_fanout_software_control_check(topology_obj):
    hostnames = []
    for alias in ("fanout", "fanout-b"):
        player = topology_obj.players.get(alias)
        if is_shared_fanout(player) and software_control_enabled(player):
            hostnames.append(player["attributes"].noga_query_data["attributes"]["Common"]["Name"])
    assert not hostnames, "\n".join(software_control_warning(hostname) for hostname in hostnames)


@pytest.mark.flaky(reruns=2, reruns_delay=10)
@pytest.mark.sanity_checker_community
def test_cable_connection_between_dut_and_fanout_check(engines, topology_obj, request, enable_and_disable_fanout_lldp):
    """
    This test is verify that cable connection between dut and fanout is ok.
    If case fail, the consequent regression steps will be stopped by mars
    """
    cli_object_a = topology_obj.players['dut']['cli']
    dut_a = engines.dut

    with allure.step("Check cable connection between dut a and fanout a"):
        logger.info("Check cable connection between dut a and fanout a")
        check_one_dut_to_fanout_cable_connection(cli_object_a, dut_a, topology_obj)

    if "dual" in request.config.getoption("--setup_name"):
        cli_object_a = topology_obj.players['dut-b']['cli']
        dut_b = engines.dut_b
        with allure.step("Check cable connection between dut b and fanout b"):
            logger.info("Check cable connection between dut b and fanout b")
            check_one_dut_to_fanout_cable_connection(cli_object_a, dut_b, topology_obj)


@pytest.mark.flaky(reruns=30, reruns_delay=4)
@pytest.mark.sanity_checker_community
def test_bgp_session_status_check(topology_obj):
    """
    This test is verify that bgp session status is ok.
    If case fail, the consequent regression steps will be stopped by mars
    """
    ip_bgp_summary = topology_obj.players['dut']['cli'].bgp.parse_ip_bgp_summary(ip_version='ipv4')
    if not ip_bgp_summary:
        ip_bgp_summary = topology_obj.players['dut']['cli'].bgp.parse_ip_bgp_summary(ip_version='ipv6')
    regex_full_digit = r"^\d+$"
    for neighbor_ip, neighbor_info in ip_bgp_summary.items():
        assert re.match(regex_full_digit, neighbor_info["State/PfxRcd"]), \
            f"The bpg session with neighbor {neighbor_ip} doesn't work"


@pytest.mark.flaky(reruns=30, reruns_delay=4)
@pytest.mark.sanity_checker_canonical
def test_cable_connection_for_canonical_check(topology_obj, sonic_topo, platform_params):
    """
    This test is verify that the cable connection for canonical setup is ok.
    If case fail, the consequent regression steps will be stopped by mars
    """
    if sonic_topo != "ptf-any":
        pytest.skip(f"The topo {sonic_topo} does not support the case ")
    # TODO workaround due to service port issue on sn6600 platform
    from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
    if 'sn6600' in platform_params.platform and is_redmine_issue_active([5093036])[0]:
        pytest.skip("Testcase skipped due to RM issue: https://redmine.mellanox.com/issues/5093036 on sn6600 platform")
    dut_cli_object = topology_obj.players['dut']['cli']
    lldp_table_info = dut_cli_object.lldp.parse_lldp_table_info()

    def _check_port_between_dut_and_host(host_name):
        host_cli_object = topology_obj.players[host_name]['cli']
        host_hostname = host_cli_object.general.hostname()

        for index in range(1, 3):
            dut_host_port = topology_obj.ports[f"dut-{host_name}-{index}"]
            host_dut_port = topology_obj.ports[f"{host_name}-dut-{index}"]
            assert lldp_table_info[dut_host_port][0] == host_hostname \
                and host_dut_port in lldp_table_info[dut_host_port][3], \
                f"port {index} connection ({dut_host_port}, {host_dut_port}) " \
                f"between dut and {host_name} doesn't match the definition in noga"

    _check_port_between_dut_and_host("ha")
    _check_port_between_dut_and_host("hb")

    # check loopbacks on dut
    lb_ports = get_dut_loopbacks(topology_obj, split=True)
    for ports in lb_ports:
        assert ports[1] == lldp_table_info[ports[0]][
            3], f"loopback for ports {ports} doesn't match the definition in noga"


@pytest.mark.flaky(reruns=30, reruns_delay=4)
@pytest.mark.sanity_checker_ci
@pytest.mark.sanity_checker_common
def test_fan_status_check(platform_params, topology_obj, platform_json_data, request, is_in_deploy_image_flow, skip_for_liquid_cooling_platform):
    """
    This test is verify that the fan status is ok.
    If case fail, we will raise the failed case information in the allure report and disable bug handler tool
    """
    fan_status_info = topology_obj.players['dut']['cli'].chassis.show_platform_fan()
    is_test_failed = False

    if len(platform_json_data["chassis"]["fan_drawers"]) == 1:
        fan_number = len(platform_json_data["chassis"]["fan_drawers"][0]["fans"])
    else:
        fan_number = len(platform_json_data["chassis"]["fan_drawers"])
    actual_fan_number = 0
    for fan_name, status_info in fan_status_info.items():
        if "psu" not in fan_name:
            actual_fan_number += 1
        if status_info["Status"].lower() != "ok":
            err_msg = f"The status of {fan_name} is not ok"
            assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
            is_test_failed = True

    if actual_fan_number not in [fan_number, fan_number * 2]:
        err_msg = f"fan number is not correct. expected:{fan_number} or {fan_number * 2}, actual: {actual_fan_number}"
        assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
        is_test_failed = True

    write_failed_case_name(is_test_failed, request.node.name, is_in_deploy_image_flow)


@pytest.mark.flaky(reruns=30, reruns_delay=4)
@pytest.mark.sanity_checker_ci
@pytest.mark.sanity_checker_common
def test_more_then_2_fan_status_wrong_check(topology_obj, skip_for_liquid_cooling_platform):
    """
    This test is verify more than 2 fan status are not ok
    If case fail, the consequent regression steps will be stopped by mars
    """
    fan_status_info = topology_obj.players['dut']['cli'].chassis.show_platform_fan()
    broken_fan_number = 0
    fail_case_threshold_for_broken_fan_number = 2
    for fan_name, status_info in fan_status_info.items():
        if status_info["Status"].lower() != "ok":
            broken_fan_number += 1
    logger.info(f"broken fan number is {broken_fan_number}")
    assert broken_fan_number < fail_case_threshold_for_broken_fan_number, \
        f"The status of {broken_fan_number} fan are not ok "


@pytest.mark.flaky(reruns=30, reruns_delay=4)
@pytest.mark.sanity_checker_ci
@pytest.mark.sanity_checker_common
def test_psu_status_check(platform_params, topology_obj, platform_json_data, request, is_in_deploy_image_flow, skip_for_liquid_cooling_platform):
    """
    This test is verify the psu status is ok or not
    If case fail, we will raise the failed case information in the allure report and disable bug handler tool
    """
    is_test_failed = False
    psu_status_info = topology_obj.players['dut']['cli'].chassis.show_platform_psu_status()
    psu_number = len(platform_json_data["chassis"]["psus"])
    actual_psu_number = len(psu_status_info.keys())

    if actual_psu_number != psu_number:
        err_msg = f"psu number is correct.Expected: {psu_number}, actual: {actual_psu_number}"
        assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
        is_test_failed = True

    for psu_name, status_info in psu_status_info.items():
        if status_info["Status"].lower() != "ok":
            err_msg = f"The status of {psu_name} is not ok"
            assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
            is_test_failed = True

    write_failed_case_name(is_test_failed, request.node.name, is_in_deploy_image_flow)


def _copy_core_dumps_to_nfs(engine, dumps_folder, core_files):
    collected_paths = []
    for name in core_files:
        if ".core" not in name:
            continue
        dest = os.path.join(dumps_folder, name)
        copy_dump_file(engine, os.path.join(VAR_CORE_DIR, name), dest)
        collected_paths.append(dest)
    return collected_paths


@pytest.mark.sanity_checker_common
@pytest.mark.sanity_checker_airspin
def test_core_dump_file_in_var_core_check(engines, request, is_in_deploy_image_flow, dumps_folder):
    """
    This test is verify if the folder of /var/core has the core dump file, if yes fail case
    If case fail, we will raise the failed case information in the allure report and disable bug handler tool
    """
    is_test_failed = False
    raw = engines.dut.run_cmd(f"ls -1 {VAR_CORE_DIR} 2>/dev/null")
    var_core_files = [line.strip() for line in (raw or "").splitlines() if line.strip()]

    if var_core_files:
        collected_paths = _copy_core_dumps_to_nfs(engines.dut, dumps_folder, var_core_files)
        collected_msg = f", copied to: {collected_paths}" if collected_paths else ""
        err_msg = f"{VAR_CORE_DIR} folder has core dump file: {var_core_files}{collected_msg}"
        assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
        is_test_failed = True

    write_failed_case_name(is_test_failed, request.node.name, is_in_deploy_image_flow)


def get_info_about_current_components_version_dict(engine):
    """
    Get dictionary with component name as key and version as value
    :param engine: dut engine
    :return: dictionary with component name as key and version as value
    """

    fwutil_show_status_output = engine.run_cmd('sudo fwutil show status')
    fwutil_show_status_dict = generic_sonic_output_parser(fwutil_show_status_output)
    component_names_list = fwutil_show_status_dict[0]['Component']
    component_versions_list = fwutil_show_status_dict[0]['Version']
    component_versions_dict = {}
    for component, version in zip(component_names_list, component_versions_list):
        component_versions_dict[component] = version

    return component_versions_dict


def read_csv_file(csv_file):
    with open(csv_file) as csv_file_object:
        reader = csv.DictReader(csv_file_object)
        return [row for row in reader]


def get_setup_dir_name(dut_name):
    if "air" in dut_name.lower():
        return dut_name.split("-dut")[0]
    return f"{dut_name}_setup"


def check_one_dut_to_fanout_cable_connection(cli_object, dut_engine, topology_obj):
    dut_name = dut_engine.run_cmd("hostname")
    hwsku = cli_object.chassis.get_platform_hwsku()
    setup_dir_name = get_setup_dir_name(dut_name)
    dut_fanout_link_file = os.path.join(CURRENT_PATH,
                                        f"../../../../ansible/files/hwsku_vars/{setup_dir_name}/{hwsku}/sonic_nvidia_links.csv")
    dut_fanout_link_data = read_csv_file(dut_fanout_link_file)
    logger.info(f"dut_fanout_link_data:\n {dut_fanout_link_data}")

    interface_status_dict = cli_object.interface.parse_interfaces_status()
    logger.info(f"interface_status_dict:\n {interface_status_dict}")

    shared_fanouts = [player["attributes"].noga_query_data["attributes"]["Common"]["Name"]
                      for player in (topology_obj.players.get("fanout"), topology_obj.players.get("fanout-b"))
                      if is_shared_fanout(player)]

    map_dut_oper_up_interface_and_fanout_interface = {}
    for one_dut_fanout_link in dut_fanout_link_data:
        if one_dut_fanout_link["StartDevice"] != dut_name:
            continue
        if one_dut_fanout_link.get("EndDevice") in shared_fanouts:
            logger.info(f"Skipping port {one_dut_fanout_link['StartPort']} connected to {one_dut_fanout_link['EndDevice']}")
            continue
        dut_port = one_dut_fanout_link["StartPort"]
        if dut_port.startswith("Ethernet") and dut_port in interface_status_dict and \
                interface_status_dict.get(dut_port, "").get("Oper", "down") == "up":
            map_dut_oper_up_interface_and_fanout_interface[dut_port] = one_dut_fanout_link["EndPort"]

    logger.info(f"dut port to fanout port map: {map_dut_oper_up_interface_and_fanout_interface}")
    assert map_dut_oper_up_interface_and_fanout_interface, "Not find any port connecting from dut to fanout"

    check_lldp_info_dut_to_fanout(dut_engine, map_dut_oper_up_interface_and_fanout_interface)


@retry(Exception, tries=10, delay=5)
def check_lldp_info_dut_to_fanout(dut_engine, map_dut_oper_up_interface_and_fanout_interface):
    lldp_table_res = dut_engine.run_cmd("show lldp table")
    lldp_table_list = generic_sonic_output_parser(lldp_table_res,
                                                  headers_ofset=1,
                                                  len_ofset=2,
                                                  data_ofset_from_start=3,
                                                  data_ofset_from_end=-2,
                                                  column_ofset=2,
                                                  )
    logger.info(f"lldp_table_list:\n {lldp_table_list}")

    def _look_up_matched_lldp_info(dut_interface, fanout_interface):
        for one_lldp_info in lldp_table_list:
            if dut_interface == one_lldp_info["LocalPort"] and (
                    fanout_interface.split(" ")[-1] in one_lldp_info["RemotePortID"] or
                    fanout_interface == one_lldp_info["RemotePortDescr"]):
                return True
        assert False, f"Not find the lldp info for dut port {dut_interface} and  fanout port {fanout_interface}. " \
            f"llp info:{one_lldp_info} Please check the cable connection of the two ports"

    for dut_interface, fanout_interface in map_dut_oper_up_interface_and_fanout_interface.items():
        _look_up_matched_lldp_info(dut_interface, fanout_interface)


@retry(Exception, tries=15, delay=5)
def check_port_oper_up_on_admin_up(dut_topology_obj):
    logger.info("Check the oper status is up when the corresponding admin status is up")
    with allure.step(f'check ports are up'):
        port_status = dut_topology_obj.interface.parse_interfaces_status()
        for port, status in port_status.items():
            if status["Admin"] == "up":
                assert status["Oper"] == 'up', f"{port} is not up"


def assert_failure_or_just_print_err(print_msg, is_in_deploy_image_flow):
    if is_in_deploy_image_flow:
        logger.error(print_msg)
    else:
        assert False, print_msg


def write_failed_case_name(is_test_failed, case_name, is_in_deploy_image_flow):
    if is_test_failed and is_in_deploy_image_flow:
        logger.info(f"write test name {case_name} to file")
        write_failed_sanity_checker_cases_to_file([f"{case_name} "])


def is_valid_component_entry(component, component_info):
    """
    Check if a component entry is valid and should be included in parsing.

    :param component: the component name string
    :param component_info: the full parsed row dictionary
    :return: True if the entry is valid, False if it should be skipped
    """
    if not component or not component.strip('-'):
        return False
    if component.upper() in ('COMPONENT', 'SIMX') or component.upper().startswith('SAI_API_HEAD'):
        return False
    if 'SONIC_SAI' in component_info or 'VENDOR_SAI' in component_info:
        return False
    return True


def parse_component_version_table(engines):
    """
    The function parses the component version table gotten as the output of get_components_version.py script

    :param engines:  engines fixture
    :return: A dictionary, stating for each component what is the compilation version and what is the actual version.
    Example - {"SDK", ("4.6.2202", "4.6.2202")}
    """
    expected_component_version_table = engines.dut.run_cmd(f"sudo {COMPONENT_SCRIPT_NAME}")
    parsed_table = generic_sonic_output_parser(expected_component_version_table)
    version_dict = dict()
    for component_info in parsed_table:
        component = (component_info.get('COMPONENT') or component_info.get('component', '')).strip()
        if not is_valid_component_entry(component, component_info):
            continue
        compilation_version = component_info.get('COMPILATION') or component_info.get('compilation', '')
        actual_version = component_info.get('ACTUAL') or component_info.get('actual', '')
        version_dict[component] = (compilation_version, actual_version)
    logger.info(f"Parsed components from {COMPONENT_SCRIPT_NAME} are (compilation, actual): {version_dict}")
    return version_dict


def parse_readme_versions(sonic_image):
    """
    The function parses the component version table gotten as the output of get_components_version.py script
    :param sonic_image: the current sonic image deployed on the dut
    :return: A dictionary, stating for each component what is the readme version of it, example - {"SDK", "4.6.2202"}
    """
    readme_path = resolve_sonic_image_readme_path(sonic_image)
    if not readme_path:
        raise Exception(
            f"Sonic image path: /auto/sw_system_release/sonic/{sonic_image}/dev/README "
            f"doesn't include a README file"
        )
    logger.info(f"Parsing versions according to readme file: {readme_path}")
    with open(readme_path) as f:
        image_readme_content = f.read()
    readme_versions_dict = dict()

    # First match the version with the suffix "_VERSION_SWITCH"
    # RSHIM_VERSION pattern uses a special version format, so we need to handle it separately
    # Then match the version with the suffix "_VERSION"
    patterns = [
        re.compile(r"(?P<component>\w+)_VERSION_SWITCH:\s*(?P<version>\S+)"),
        re.compile(r"(?P<component>RSHIM)_VERSION:\s*(?:\S*@)?(?P<version>(\d+\.)+\d+)"),
        re.compile(r"(?P<component>\w+)_VERSION:\s*(?P<version>\S+)")
    ]

    for line in image_readme_content.strip().split('\n'):
        for pattern in patterns:
            match = pattern.match(line)
            if match:
                component = str(match.group('component').strip())
                # Add only if the component is in the README_COVERED_COMPONENTS and not stored in readme_versions_dict
                if component in README_COVERED_COMPONENTS and component not in readme_versions_dict:
                    version = str(match.group('version').strip())
                    if component == 'RSHIM' and '@' in version:
                        version = version.split('@')[-1]
                    readme_versions_dict[component] = version
                break

    logger.info(f"Parsed components from {readme_path} are:\n {readme_versions_dict}")
    return readme_versions_dict


def get_actual_version(dut_engine, component):
    """
    The function fetches the current version of the component from the dut engine and returns it.
    :param dut_engine: the dut engine
    :param component: the component to fetch version for
    :return: The version of "component" as it appears on the dut
    """
    dut_command_ind = 0
    command_regex_ind = 1
    required_regex_group = 1
    cmd = ALL_COMMANDS_FOR_ACTUAL[component][dut_command_ind]
    version = dut_engine.run_cmd(cmd)
    parsed_version = re.search(ALL_COMMANDS_FOR_ACTUAL[component][command_regex_ind], str(version))
    return parsed_version.group(required_regex_group) if parsed_version else UNEXPECTED_MISSING_VERSION


def fetch_versions_from_dut(dut_engine, is_simx, expected_components=None):
    """
    The function fetches the versions installed on the dut in runtime
    :param dut_engine: the dut engine
    :param is_simx: is_simx fixture
    :param expected_components: set of component names from the parsed version table,
           used to determine which optional components to fetch
    :return: A dictionary, stating for each component what is the actual version of it, example - {"SDK", "4.6.2202"}
    """
    actual_versions_dict = dict()
    for component in COMMANDS_FOR_ACTUAL:
        actual_versions_dict[component] = get_actual_version(dut_engine, component)
    if expected_components:
        for component in OPTIONAL_COMMANDS_FOR_ACTUAL:
            if component in expected_components:
                actual_versions_dict[component] = get_actual_version(dut_engine, component)
    if not is_simx:
        actual_versions_dict.update(get_info_about_current_components_version_dict(dut_engine))
    else:
        for component in FW_DEFAULT_VERSIONS:
            actual_versions_dict[component] = UNEXPECTED_MISSING_VERSION
    logger.info(f"Components fetched from the dut are {actual_versions_dict}")

    return actual_versions_dict


def get_bmc_version(dut_engine):
    """Get BMC version and chassis name from 'fwutil show status'."""
    output = dut_engine.run_cmd('sudo fwutil show status', timeout=60)

    # Use shared parsing logic from firmware_helper
    fw_data = parse_firmware_status(output)
    bmc_version, chassis_name = get_bmc_version_from_firmware_data(fw_data)

    if not bmc_version:
        logger.info("BMC component not found in firmware status")

    return bmc_version, chassis_name


@retry(Exception, tries=50, delay=15)
def verify_bmc_version(dut_engine, expected_version):
    """Verify BMC version matches expected version with retries."""
    current_version, _ = get_bmc_version(dut_engine)

    if current_version == expected_version:
        logger.info("BMC version verified: {}".format(current_version))
        return

    # Raise exception to trigger retry
    logger.info("BMC version mismatch. Expected: {}, Current: {}".format(
        expected_version, current_version))
    raise Exception("BMC version mismatch. Expected: {}, Current: {}".format(
        expected_version, current_version))


def update_bmc_firmware(dut_engine, expected_version, firmware_path):
    """Download, install BMC firmware and wait for update to complete."""
    tmp_fw_path = '/tmp/{}'.format(os.path.basename(firmware_path))

    try:
        # Download firmware locally on mgmt container
        result = subprocess.run(['curl', '-o', tmp_fw_path, firmware_path],
                                capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise Exception("Failed to download firmware: {}".format(result.stderr))

        # Verify file was downloaded locally
        if not os.path.exists(tmp_fw_path):
            raise Exception("Firmware file not found after download")
        logger.info("Firmware downloaded successfully to local: {}".format(tmp_fw_path))

        dut_engine.run_cmd('sudo rm -f {}'.format(shlex.quote(tmp_fw_path)), timeout=30)

        # Copy firmware from local to DUT
        dut_engine.copy_file(source_file=tmp_fw_path,
                             dest_file=os.path.basename(tmp_fw_path),
                             file_system='/tmp',
                             direction='put')
        logger.info("Firmware copied to DUT: {}".format(tmp_fw_path))

        install_cmd = 'sudo config platform firmware install chassis component BMC fw -y {}'.format(shlex.quote(tmp_fw_path))
        logger.info("Starting BMC firmware installation")

        # Enable SSH keep-alive to prevent connection drop during long-running commands
        transport = dut_engine.engine.remote_conn.get_transport()
        if transport:
            transport.set_keepalive(30)  # Send keep-alive every 30 seconds
            logger.info("SSH keep-alive enabled (interval: 30s)")

        # BMC install is NOT idempotent - use send_command directly to avoid retry
        output = dut_engine.engine.send_command(
            install_cmd,
            read_timeout=900,
            expect_string=r'[#\$]\s*$'
        )
        logger.info("BMC firmware installation output: {}".format(output))

        verify_bmc_version(dut_engine, expected_version)
        logger.info("BMC updated and verified successfully: {}".format(expected_version))
        return True
    except Exception as e:
        logger.error("BMC update failed: {}".format(e))
        return False
    finally:
        try:
            if os.path.exists(tmp_fw_path):
                os.remove(tmp_fw_path)
            dut_engine.run_cmd('sudo rm -f {}'.format(shlex.quote(tmp_fw_path)), timeout=30)
        except Exception as e:
            logger.warning("Cleanup failed: {}".format(e))


def check_update_bmc_version(engines, fw_pkg):
    """Check BMC version and update if needed."""
    current_version, chassis_name = get_bmc_version(engines.dut)
    logger.info("current_version: {}, chassis_name: {}".format(current_version, chassis_name))
    if not current_version or not chassis_name:
        logger.info("BMC not found or chassis name not found, skipping check")
        return True

    bmc_user, bmc_password = load_bmc_creds()
    try:
        platform = engines.dut.run_cmd(
            "sudo show platform summary | grep Platform | awk '{print $2}'", timeout=30
        ).strip()
        bmc_json_path = "/usr/share/sonic/device/{}/bmc.json".format(platform)
        bmc_json_raw = engines.dut.run_cmd("cat {}".format(bmc_json_path), timeout=30)
        bmc_ip = json.loads(bmc_json_raw)["bmc_addr"]
        logger.info("BMC IP resolved: {}".format(bmc_ip))
    except Exception as e:
        logger.warning("Failed to get BMC IP: {}".format(e))
        bmc_ip = None
    flavor = resolve_bmc_flavor(fw_pkg, chassis_name, engines.dut, bmc_ip,
                                bmc_user, bmc_password)
    # Get BMC info from firmware package
    expected_version, firmware_path = get_bmc_info_from_firmware_data(fw_pkg, chassis_name, flavor)
    if not expected_version:
        logger.info("BMC not defined in firmware.json for chassis={}".format(chassis_name))
        return True

    logger.info("BMC version - current: {}, expected: {}".format(current_version, expected_version))
    if current_version == expected_version:
        logger.info("BMC version is already up to date")
        return True

    if not firmware_path:
        logger.error("No firmware path for BMC update")
        return False

    return update_bmc_firmware(engines.dut, expected_version, firmware_path)


@pytest.mark.sanity_checker_common
def test_component_version_check(engines, cli_objects, request, is_in_deploy_image_flow, is_simx, fw_pkg):
    """
    This test validates that component versions match the README file specifications.
    It compares both the COMPILATION versions (from get_component_versions.py) against README values
    and ACTUAL versions against directly fetched versions from the DUT.
    Also checks and updates BMC version.

    If case fail, we will raise the failed case information in the allure report and disable bug handler tool

    :param engines: engines fixture
    :param cli_objects: cli_objects fixture
    :param request: pytest request fixture
    :param is_in_deploy_image_flow: flag indicating if running in deploy flow
    :param is_simx: flag indicating if running on SIMX platform
    :param fw_pkg: firmware package data fixture
    """
    # TODO: WA for RM#4895801 when the test is running in BAT with darkmode
    from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
    if is_in_deploy_image_flow and is_redmine_issue_active([4895801])[0]:
        if not request.config.getoption("--base-version-dpu"):
            pytest.skip("Skipping test for RM#4895801 while there is no DPU image available.")

    is_test_failed = False

    # Get sonic image version
    with allure.step("Get sonic image version"):
        _, sonic_image = cli_objects.dut.general.get_base_and_target_images()
        sonic_image = sonic_image.replace("SONiC-OS-", "").replace("_ASAN", "")
        logger.info(f"Sonic image: {sonic_image}")

    # Parse README versions
    with allure.step("Parse README versions"):
        try:
            readme_versions = parse_readme_versions(sonic_image)
        except Exception as e:
            err_msg = f"Failed to parse README versions: {e}"
            assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
            is_test_failed = True
            write_failed_case_name(is_test_failed, request.node.name, is_in_deploy_image_flow)
            return

    # Parse component version table from DUT
    with allure.step("Parse component version table from DUT"):
        try:
            expected_component_versions = parse_component_version_table(engines)
        except Exception as e:
            err_msg = f"Failed to parse component version table: {e}"
            assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
            is_test_failed = True
            write_failed_case_name(is_test_failed, request.node.name, is_in_deploy_image_flow)
            return

    # Fetch actual versions from DUT
    with allure.step("Fetch actual versions from DUT"):
        try:
            actual_versions = fetch_versions_from_dut(engines.dut, is_simx,
                                                      expected_components=set(expected_component_versions.keys()))
        except Exception as e:
            err_msg = f"Failed to fetch actual versions: {e}"
            assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
            is_test_failed = True
            write_failed_case_name(is_test_failed, request.node.name, is_in_deploy_image_flow)
            return

    # Verify component keys match
    with allure.step("Verify component keys match"):
        if set(actual_versions.keys()) != set(expected_component_versions.keys()):
            err_msg = f"Component keys mismatch. Expected: {set(expected_component_versions.keys())}, Got: {set(actual_versions.keys())}"
            assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
            is_test_failed = True

    # Validate each component version
    for component in actual_versions.keys():
        compilation_version, actual_version = expected_component_versions[component]

        # Check compilation version against README
        with allure.step(f"Validate {component} compilation version against README"):
            if component in readme_versions:
                if compilation_version != readme_versions[component]:
                    err_msg = (f"{component}: Compilation version mismatch. "
                               f"README: {readme_versions[component]}, "
                               f"COMPILATION: {compilation_version}")
                    logger.error(err_msg)
                    assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
                    is_test_failed = True
            else:
                if compilation_version != NON_EXISTENT_VERSION:
                    err_msg = (f"{component}: Expected compilation version '{NON_EXISTENT_VERSION}' "
                               f"(not in README), but got: {compilation_version}")
                    logger.error(err_msg)
                    assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
                    is_test_failed = True

        if component == 'FW' and is_simx:
            logger.info(f"Skip actual version check for FW on simx platform")
            continue
        # Check actual version matches fetched version
        with allure.step(f"Validate {component} actual version matches fetched"):
            if actual_version != actual_versions[component]:
                err_msg = (f"{component}: Actual version mismatch. "
                           f"Fetched: {actual_versions[component]}, "
                           f"Table ACTUAL: {actual_version}")
                logger.error(err_msg)
                assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
                is_test_failed = True

        # Check that actual version matches compilation version (for components in README)
        # This ensures deployed components match what was compiled into the image
        with allure.step(f"Validate {component} actual matches compilation"):
            if component in readme_versions and actual_version.replace('-', '.') != compilation_version.replace('-', '.'):
                err_msg = (f"{component}: Actual version doesn't match compilation version. "
                           f"COMPILATION: {compilation_version}, "
                           f"ACTUAL: {actual_version}")
                logger.error(err_msg)
                assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
                is_test_failed = True

    # Check BMC version
    with allure.step("Check BMC version"):
        try:
            if not check_update_bmc_version(engines, fw_pkg):
                err_msg = "BMC version update failed"
                logger.error(err_msg)
                assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
                is_test_failed = True
        except Exception as e:
            err_msg = "BMC version check encountered an error: {}".format(e)
            logger.error(err_msg)
            assert_failure_or_just_print_err(err_msg, is_in_deploy_image_flow)
            is_test_failed = True

    write_failed_case_name(is_test_failed, request.node.name, is_in_deploy_image_flow)
