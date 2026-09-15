import logging
import os.path
import sys
import time
from typing import Dict

import pytest

from ngts.nvos_constants.constants_nvos import ApiType, PlatformConsts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool, RebootParams
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.switch_recovery import recover_dut_with_remote_reboot

logger = logging.getLogger()


@pytest.mark.dut_reboot
@pytest.mark.timeout(45 * MINUTE, func_only=True)
@pytest.mark.cpld
def test_cpld_upgrade(engines, devices, topology_obj, show_platform_initial_state):
    """
    @summary: test all these commands:
        nv show fae platform firmware cpld files
        nv action delete fae platform firmware cpld files <file-name>
        nv action fetch fae platform firmware cpld <remote-url-fetch>
        nv action install fae platform firmware cpld files <file-name> [force]
    Note: because the test takes a long time, half of it runs on NVUE and half on OpenAPI

    Test flow:
        1. Fetch old CPLD firmware images (two files: BURN and REFRESH)
        2. Assert the images exist now
        3. Install them (BURN, then REFRESH, then reboot system)
        4. Assert the current CPLD firmware version is the one just installed
        5. Delete the images that were used for installation
        6. Assert the images no longer exist
        7. Repeat steps 1-6 on for the new CPLD firmware image
    """
    with allure.step('Create System objects'):
        platform = Platform()

    device = devices.dut

    if not device.allow_cpld_update:
        pytest.skip("Not a crocodile, nor Juliet TTM... Should ignore test.")

    try:
        TestToolkit.tested_api = ApiType.NVUE
        with allure.step(f"Fetch, install and assert old CPLD version (through {TestToolkit.tested_api})"):
            image_previous_details = BmcTool.get_fw_component_version_dict("cpld", "previous")
            _firmware_install_test(devices, platform, image_previous_details, engines, topology_obj)
    finally:
        TestToolkit.tested_api = ApiType.OPENAPI
        with allure.step(f"Cleanup: Fetch, install and assert original CPLD version (through {TestToolkit.tested_api})"):
            image_details = BmcTool.get_fw_component_version_dict("cpld", "latest")
            _firmware_install_test(devices, platform, image_details, engines, topology_obj)


def _firmware_install_test(devices, platform: Platform, image_details, engines, topology_obj):
    player_engine = engines['sonic_mgmt']
    scp_path = 'scp://{}:{}@{}'.format(player_engine.username, player_engine.password, player_engine.ip)
    burn_filename = os.path.basename(image_details['path'])
    has_refresh_image = 'refresh_path' in image_details
    if has_refresh_image:
        refresh_filename = os.path.basename(image_details['refresh_path'])  # will be empty for switches that have no REFRESH file
        file_names = {burn_filename, refresh_filename}
    else:
        file_names = {burn_filename}
    logger.info(f"{file_names=} {type(devices.dut)=}")

    with allure.step(f"Asserting the image files don't exist yet"):
        initial_files = platform.firmware.cpld.show_files_as_list()
        assert not (file_names & set(initial_files)), ("Can't test `fetch` because file is already present: " +
                                                       str(set(initial_files) & file_names))

    with allure.step(f"Fetching BURN image"):
        platform.firmware.cpld.action_fetch(image_details['path'], base_url=scp_path).verify_result()

    if has_refresh_image:
        with allure.step(f"Fetching REFRESH image"):
            platform.firmware.cpld.action_fetch(image_details['refresh_path'], base_url=scp_path).verify_result()

    with allure.step(f"Asserting fetch was successful"):
        file_list = platform.firmware.cpld.show_files_as_list()
        assert set(file_list) == set(initial_files) | file_names, \
            f"Expected new files {file_names} but the old file list is {initial_files} " \
            f"and the new one is {file_list}"

    try:
        with allure.step(f"Installing BURN image {burn_filename}"):
            result, operation_duration = OperationTime.save_duration(
                "install CPLD (BURN)", '', test_cpld_upgrade.__name__,
                platform.firmware.cpld.files.file_name[burn_filename].action_file_install,
                dut_engine=engines.dut, force=False)
            result.verify_result()

            with allure.step(
                f"verify operation time for install cpld {burn_filename!r} "
                f"(duration: {operation_duration})"
            ):
                OperationTime.verify_operation_time(
                    operation_duration,
                    'install cpld',
                    threshold=devices.dut.expected_operation_durations['install cpld'],
                ).verify_result()

            if has_refresh_image:
                with allure.step(f"Installing REFRESH image (and rebooting) {refresh_filename}"):
                    reboot_params = RebootParams(
                        topology_obj=topology_obj,
                        wait_time_before_reboot=devices.dut.wait_before_reboot_single_fw_install)
                    platform.firmware.cpld.files.file_name[refresh_filename].cpld_refresh_install(
                        reboot_params=reboot_params, device=devices.dut, engine=engines.dut,
                        force=True).verify_result()
            else:
                recover_dut_with_remote_reboot(topology_obj, engines, device=devices.dut)

            with allure.step(f"Asserting install was successful"):
                firmware_shown = OutputParsingTool.parse_json_str_to_dictionary(platform.firmware.show()).get_returned_value()
                validate_firmware_versions(firmware_shown, image_details)

    finally:
        original_exception = sys.exc_info()[1]
        try:
            with allure.step("Wait for system to be functional"):
                # Needed if reboot took longer than expected; small overhead if the system is already functional.
                engines.dut.disconnect()
                DutUtilsTool.wait_for_nvos_to_become_functional(engines.dut).verify_result()
            for file_name in file_names:
                with allure.step(f"Deleting image file {file_name}"):
                    platform.firmware.cpld.files.file_name[file_name].action_delete().verify_result()

            with allure.step(f"Asserting delete was successful"):
                final_file_list = platform.firmware.cpld.show_files_as_list()
                assert set(initial_files) == set(final_file_list), (
                    f"File list is expected to be the same at the start and end of the test, but the initial file list is:\n"
                    f"{initial_files}\nAnd at the end of the test the list is:\n{final_file_list}")
        except Exception as cleanup_exc:
            if original_exception is None:
                raise
            logger.warning(
                "Second exception during cleanup (wait/delete), preserving original failure: %s",
                cleanup_exc,
            )


def validate_firmware_versions(firmware_shown, image_details: Dict[str, Dict[str, str]]):
    with allure.step('validate cpld firmware versions'):
        for cpld_number, expected_version in image_details['version_name'].items():
            with allure.independent_step(f"Checking {cpld_number}"):
                actual_firmware = firmware_shown[cpld_number][PlatformConsts.FW_ACTUAL]
                assert actual_firmware == expected_version, f"{cpld_number} version mismatch: Expected '{expected_version}', Got '{actual_firmware}'"
