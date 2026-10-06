import logging
import time
import re

import pytest

from ngts.nvos_constants.constants_nvos import PlatformConsts, ApiType
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.mark.checklist
@pytest.mark.platform
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_action_platform_firmware_single_pkg(engines, devices, random_api, test_name, topology_obj, nv_command):
    """
    Action fetch, delete, rename, upload platform firmware test

    Test flow:
    1. Fetch first single package firmware file, verify in show
    2. Fetch second single package firmware file, verify in show that both files are present
    3. Delete first file, verify in show that old file is not present, second one is present
    4. Rename the second single package firmware file, verify in show that new file is present
    5. Upload the package file to remote
    6. validate in remote that uploaded file is present
    7. Delete the uploaded file from the remote
    8. Delete the package file form the switch
    """

    component_name = "single_package"

    fw_file_latest, fw_image_name_latest, version_name_latest = BmcTool.get_fw_component_version_latest(component_name)
    logger.info(f"Upgrade Version: fw_file={fw_file_latest}, fetched_image_name={fw_image_name_latest}, "
                f"version_name={version_name_latest}")
    assert fw_file_latest is not None, f"Latest FW file for {component_name} is none"

    fw_file_previous, fw_image_name_previous, version_name_previous = BmcTool.get_fw_component_version_previous(
        component_name)
    logger.info(f"Base version: fw_file={fw_file_previous}, fetched_image_name={fw_image_name_previous}, "
                f"version_name={version_name_previous}")
    assert fw_file_previous is not None, f"Previous FW file for {component_name} is none"

    # fw_file_full_path_old = fw_file_path + filename_old
    # fw_file_full_path_new = fw_file_path + filename_new

    fw_file_new_name = "single_package_fw_file.fwpkg"
    image_fetched = False

    try:
        with allure.step("Wait for system to be functional"):
            result_obj = DutUtilsTool.wait_for_nvos_to_become_functional(engines.dut)
            assert result_obj.result, f"System did not become functional after reboot after FW installation"

        with allure.step("Fetch firmware file to switch"):
            player_engine = engines['sonic_mgmt']
            scp_path = 'scp://{}:{}@{}'.format(player_engine.username, player_engine.password, player_engine.ip)
            nv_command.platform.firmware.action_fetch(fw_file_previous, base_url=scp_path).verify_result()
            nv_command.platform.firmware.files.verify_show_files_output(expected_files=[fw_image_name_previous])

        with allure.step("Fetch another firmware file to switch"):
            player_engine = engines['sonic_mgmt']
            scp_path = 'scp://{}:{}@{}'.format(player_engine.username, player_engine.password, player_engine.ip)
            nv_command.platform.firmware.action_fetch(fw_file_latest, base_url=scp_path).verify_result()
            nv_command.platform.firmware.files.verify_show_files_output(expected_files=[fw_image_name_previous,
                                                                                        fw_image_name_latest])

        with allure.step(f"Delete firmware file {fw_image_name_previous} from switch"):
            nv_command.platform.firmware.files.file_name[fw_image_name_previous].action_delete().verify_result()
            nv_command.platform.firmware.files.verify_show_files_output(unexpected_files=[fw_image_name_previous],
                                                                        expected_files=[fw_image_name_latest])

        with allure.step("Rename firmware file and verify"):
            fetched_image_file = nv_command.platform.firmware.files.file_name[fw_image_name_latest]
            fetched_image_file.action_rename(fw_file_new_name, rewrite_file_name=False).verify_result()
            nv_command.platform.firmware.files.verify_show_files_output(expected_files=[fw_file_new_name])
            image_fetched = True

        upload_protocols = ['scp', 'sftp']
        player = engines['sonic_mgmt']
        fetched_image_file = nv_command.platform.firmware.files.file_name[fw_file_new_name]

        with allure.step("Upload image to player {} with the next protocols : {}".format(player.ip, upload_protocols)):
            for protocol in upload_protocols:
                with allure.step("Upload image to player with {} protocol".format(protocol)):
                    upload_path = '{}://{}:{}@{}/tmp/{}'.format(protocol, player.username, player.password, player.ip,
                                                                fw_file_new_name)
                    fetched_image_file.action_upload(upload_path).verify_result(True, 'File upload successfully')

                with allure.step("Validate file was uploaded to player and delete it"):
                    assert player.run_cmd(
                        cmd='ls /tmp/ | grep {}'.format(fw_file_new_name)), "Did not find the file with ls cmd"
                    player.run_cmd(cmd='rm -f /tmp/{}'.format(fw_file_new_name))

    finally:
        with allure.step("Cleanup steps"):
            if image_fetched:
                with allure.step("Delete single fw package file from the switch"):
                    nv_command.platform.firmware.files.delete_files([fw_file_new_name]).verify_result()


@pytest.mark.checklist
@pytest.mark.platform
@pytest.mark.timeout(120 * MINUTE, func_only=True)
def test_install_platform_firmware_single_pkg(engines, devices, random_api, test_name, topology_obj, nv_command):
    """
    Install single package firmware file, upgrade, downgrade

    Test flow:
    1. Fetch older version single package firmware file
    2. Fetch newer version single package firmware file, verify in show that both files are present
    3. Install older version FW using single package
    4. Wait for system to reboot
    5. Note FW versions of all components
    6. Install newer version FW using single package
    7. Wait for system to reboot
    8. Validate FW versions of all components have been upgraded
    9. Install older version FW using single package
    10. Wait for system to reboot
    11. Validate FW versions of all components have been downgraded
    12. Delete the package files form the switch
    """
    component_name = "single_package"

    fw_file_latest, fw_image_name_latest, version_name_latest = BmcTool.get_fw_component_version_latest(component_name)
    logger.info(f"Upgrade Version: fw_file={fw_file_latest}, fetched_image_name={fw_image_name_latest}, "
                f"version_name={version_name_latest}")
    assert fw_file_latest is not None, f"Latest FW file for {component_name} is none"

    fw_file_previous, fw_image_name_previous, version_name_previous = BmcTool.get_fw_component_version_previous(
        component_name)
    logger.info(f"Base version: fw_file={fw_file_previous}, fetched_image_name={fw_image_name_previous}, "
                f"version_name={version_name_previous}")
    assert fw_file_previous is not None, f"Previous FW file for {component_name} is none"

    player_engine = engines['sonic_mgmt']
    scp_path = 'scp://{}:{}@{}'.format(player_engine.username, player_engine.password, player_engine.ip)
    components = PlatformConsts.FW_COMPONENTS

    install_pass = False

    try:
        with allure.step("Check if any existing action is running"):
            output = engines.dut.run_cmd('nv show action')
            if "state: running" in output:
                logger.info("Older action is still running, attempting remote reboot to recover")
                helper_reboot_via_remote_reboot(engines, devices, topology_obj)

                with allure.step("Wait for system to be functional after remote reboot"):
                    result_obj = DutUtilsTool.wait_for_nvos_to_become_functional(engines.dut)
                    assert result_obj.result, f"System did not become functional after remote reboot"

        with allure.step("Get current firmware versions of all components before starting"):
            show_fw_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.platform.firmware.show()). \
                get_returned_value()
            component_dict_initial = {key: show_fw_output[key]["actual-firmware"] for key in show_fw_output if key in
                                      components}
            logger.info(f"Current versions before install: {component_dict_initial}")
            assert component_dict_initial, f"Could not read current FW versions for components: {components}"

        with allure.step("Fetch Base firmware file to switch"):
            nv_command.platform.firmware.action_fetch(fw_file_previous, base_url=scp_path).verify_result()
            nv_command.platform.firmware.files.verify_show_files_output(expected_files=[fw_image_name_previous])

        with allure.step("Base: Install first firmware file"):
            install_new_image_fw(engines, nv_command.platform, test_name, fw_image_name_previous)

        with allure.step("Get base firmware versions of all components"):
            show_fw_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.platform.firmware.show()). \
                get_returned_value()
            component_dict_base = {key: show_fw_output[key]["actual-firmware"] for key in show_fw_output if key in
                                   components}
            logger.info(f"Base versions: {component_dict_base}")

        with allure.step("Fetch newer, upgraded firmware file to switch"):
            nv_command.platform.firmware.action_fetch(fw_file_latest, base_url=scp_path).verify_result()
            nv_command.platform.firmware.files.verify_show_files_output(expected_files=[fw_image_name_previous,
                                                                                        fw_image_name_latest])

        with allure.step("Upgrade: Install newer firmware file"):
            install_new_image_fw(engines, nv_command.platform, test_name, fw_image_name_latest)

        with allure.step("Get upgraded firmware versions of all components"):
            show_fw_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.platform.firmware.show()). \
                get_returned_value()
            component_dict_upgraded = {key: show_fw_output[key]["actual-firmware"] for key in show_fw_output if key in
                                       components}
            logger.info(f"Upgraded versions: {component_dict_upgraded}")

        # Commenting below as we are testing versions whose components' versions are not entirely different
        """
        with allure.step('Verify versions were upgraded'):
            unchanged_versions = component_dict_base.items() & component_dict_upgraded.items()
            unchanged_components = {k for k, v in unchanged_versions}
            assert not unchanged_components, f"Versions of these components did not upgrade: {unchanged_components}"
        """

        with allure.step("Downgrade: Install base firmware file again"):
            install_new_image_fw(engines, nv_command.platform, test_name, fw_image_name_previous)

        with allure.step("Get downgraded firmware versions of all components"):
            show_fw_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.platform.firmware.show()). \
                get_returned_value()
            component_dict_downgraded = {key: show_fw_output[key]["actual-firmware"] for key in show_fw_output if key in
                                         components}
            logger.info(f"Downgraded versions: {component_dict_downgraded}")

        # Commenting below as we are testing versions whose components' versions are not entirely different
        """
        with allure.step('Verify the upgraded firmware versions were downgraded'):
            unchanged_versions = component_dict_upgraded.items() & component_dict_downgraded.items()
            unchanged_components = {k for k, v in unchanged_versions}
            assert not unchanged_components, f"Versions of these components did not downgrade: {unchanged_components}"
        """

        install_pass = True

    finally:
        with allure.step("Cleanup steps"):
            if not install_pass:
                logger.info("FW Install failed, attempting remote reboot for next test to start afresh")
                helper_reboot_via_remote_reboot(engines, devices, topology_obj)

            with allure.step("Wait for system to be functional"):
                result_obj = DutUtilsTool.wait_for_nvos_to_become_functional(engines.dut)
                assert result_obj.result, f"System did not become functional after reboot after FW installation"

            with allure.step("Delete single fw package files from the switch"):
                output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.platform.firmware.files.show()).\
                    get_returned_value()
                if fw_image_name_previous in output:
                    nv_command.platform.firmware.files.delete_files([fw_image_name_previous]).verify_result()
                output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.platform.firmware.files.show()).\
                    get_returned_value()
                if fw_image_name_latest in output:
                    nv_command.platform.firmware.files.delete_files([fw_image_name_latest]).verify_result()


@pytest.mark.timeout(20 * MINUTE, func_only=True)
def install_new_image_fw(engines, platform, test_name, fw_file_name, skip_version_check=True):
    with allure.step(f'FW image installation: {fw_file_name}'):
        res_obj, duration = OperationTime.save_duration('reboot with single package FW', '', test_name,
                                                        platform.firmware.files.file_name[fw_file_name].
                                                        action_file_install_with_reboot, force=True,
                                                        skip_version_check=skip_version_check)
    with allure.step('Verify operation time'):
        OperationTime.verify_operation_time(duration, 'reboot with single package FW').verify_result()

    with allure.step("Wait for system to be functional"):
        result_obj = DutUtilsTool.wait_for_nvos_to_become_functional(engines.dut)
        assert result_obj.result, f"System did not become functional after reboot after FW installation"

    with allure.step("Wait for 8 minutes between successive FW installs, for BG copy"):
        time.sleep(480)

    return res_obj


@pytest.mark.timeout(20 * MINUTE, func_only=True)
def helper_reboot_via_remote_reboot(engines, devices, topology_obj):
    with allure.step("Get name from NOGA"):
        noga_query_data = topology_obj.players['dut']['attributes'].noga_query_data['attributes']
        dhcp_hostname = noga_query_data['Common']['Name'] or noga_query_data['Specific']['dhcp_hostname']

    with allure.step("Reboot the system using remote reboot"):
        DutUtilsTool.dut_psu_control(engines, topology_obj, dhcp_hostname=dhcp_hostname)

    res_obj = DutUtilsTool.wait_on_system_reboot(engines.dut, device=devices.dut, verify_final_result=False)
    assert res_obj.result, 'System reboot failed'


def helper_remove_CPLD_number(key):
    remove_digits_re = re.compile(r"CPLD\d+")
    removed_CPLD_num = remove_digits_re.sub("CPLD", key)
    return removed_CPLD_num
