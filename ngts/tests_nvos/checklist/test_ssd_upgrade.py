import logging
import os.path
from typing import Tuple

import pytest

from ngts.nvos_constants.constants_nvos import ApiType, PlatformConsts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.platform.firmware_telemetry_helpers import (
    assert_gnmi_firmware_version_matches_nvue,
)
from ngts.tests_nvos.system.reboot_telemetry_helpers import gnmi_client_for_dut

logger = logging.getLogger()


@pytest.mark.dut_reboot
@pytest.mark.ssd
def test_ssd_upgrade(engines, devices):
    """
    @summary: test all these commands:
        nv show fae platform firmware ssd files
        nv action delete fae platform firmware ssd files <file-name>
        nv action fetch fae platform firmware ssd <remote-url-fetch>
        nv action install fae platform firmware ssd files <file-name> [force]
    Note: because firmware installation takes a long time and the test does it twice, one time it's done
    on NVUE and one time on OpenAPI.

    Note that a single image file contains two images (different versions). Running action-install on the
    file assumes that one of these is already installed, and triggers an installation of the other version.
    That's why this test uses only one file but actually swaps back-and-forth between two firmware versions.

    Test flow:
        (On NVUE:)
        1. Gets the SSD model name and name of currently-installed firmware version
        2. Fetch image file
        3. Assert the file now exists
        4. Install firmware and reboots
        (On OpenAPI:)
        5. Assert that the currently-installed firmware is the expected version
        6. Re-installs the original firmware and reboots
        (On NVUE:)
        7. Assert the current version is now the original version (from step 1)
        8. Delete the image file
        9. Assert the file no longer exists
    """
    with allure.step('Create System objects'):
        platform = Platform()
        fae = Fae()
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)

    TestToolkit.tested_api = ApiType.NVUE
    with allure.step(f"With {TestToolkit.tested_api}"):
        with allure.step("Get SSD model and verify we have an image for it"):
            ssd_model, initial_version = _get_ssd_model_and_firmware(platform)
            if ssd_model not in devices.dut.ssd_image_per_ssd_model:  # test error, not assertion failure
                raise Exception(f"Can't run test because there is no firmware image for SSD model {ssd_model}.")
            ssd_image_properties = devices.dut.ssd_image_per_ssd_model[ssd_model]
            if ssd_image_properties.current_version != initial_version:
                raise Exception(f"Can't run test because there's no image file for restoring to the currently-"
                                f"installed version. Current version is {initial_version} and the image files has"
                                f"version {ssd_image_properties.current_version}.")

        with allure.step("Asserting the image files don't exist yet"):
            expected_version = ssd_image_properties.alternate_version
            image_path = ssd_image_properties.file
            image_filename = os.path.basename(image_path)
            initial_files = fae.platform.firmware.ssd.show_files_as_list()
            assert image_filename not in initial_files, \
                f"Can't test `fetch` because file is already present: {image_filename}"

        with allure.step("Fetch image file"):
            fae.platform.firmware.ssd.action_fetch(image_path).verify_result()

        with allure.step("Asserting fetch was successful"):
            file_list = fae.platform.firmware.ssd.show_files_as_list()
            assert set(file_list) == set(initial_files) | {image_filename}, (
                f"The `fetch` command was expected to only add the file {image_filename}, but the old file list is:\n"
                f"{initial_files}\n and the new file list is: {file_list}")

    try:
        with allure.step(f"Installing firmware and rebooting (with {TestToolkit.tested_api})"):
            result, _ = OperationTime.save_duration(
                "nv action install fae platform firmware ssd files",
                f"(version {expected_version} from file {image_filename})", test_ssd_upgrade.__name__,
                fae.platform.firmware.ssd.action_install,
                image_filename, devices.dut, expect_reboot=True)
            result.verify_result()

    finally:
        TestToolkit.tested_api = ApiType.OPENAPI
        with allure.step(f"With {TestToolkit.tested_api}"):
            with allure.step("Asserting install was successful"):
                _, current_version = _get_ssd_model_and_firmware(platform)
                assert current_version == expected_version, (
                    f"Expected SSD FW version {expected_version} but actual version is {current_version}. "
                    f"Initial version before action-install was {initial_version}")
                assert_gnmi_firmware_version_matches_nvue(
                    gnmi_client, PlatformConsts.FW_SSD, current_version
                )

            with allure.step("Re-installing original firmware"):
                result, _ = OperationTime.save_duration(
                    "nv action install fae platform firmware ssd files",
                    f"(version {initial_version} from file {image_filename})", test_ssd_upgrade.__name__,
                    fae.platform.firmware.ssd.action_install,
                    image_filename, devices.dut, expect_reboot=True)
                result.verify_result()

        TestToolkit.tested_api = ApiType.NVUE
        with allure.step(f"With {TestToolkit.tested_api} again"):
            with allure.step("Asserting install was successful"):
                _, current_version = _get_ssd_model_and_firmware(platform)
                assert current_version == initial_version, (
                    f"Expected SSD FW version to be the initial version {initial_version} but actual version is "
                    f"{current_version}")
                assert_gnmi_firmware_version_matches_nvue(
                    gnmi_client, PlatformConsts.FW_SSD, current_version
                )

            with allure.step("Deleting image file"):
                fae.platform.firmware.ssd.action_delete(image_filename).verify_result()

            with allure.step("Asserting delete was successful"):
                final_file_list = fae.platform.firmware.ssd.show_files_as_list()
                assert set(initial_files) == set(final_file_list), (
                    f"File list is expected to be the same at the start and end of the test, but the initial file list "
                    f"is:\n {initial_files}\nAnd at the end of the test the list is:\n{final_file_list}")


def _get_ssd_model_and_firmware(platform: Platform) -> Tuple[str, str]:
    """Run `nv show platform firmware`, parse it and return (SSD-model, firmware-version)"""
    output = OutputParsingTool.parse_json_str_to_dictionary(
        platform.firmware.show()).get_returned_value()[PlatformConsts.FW_SSD]
    return output[PlatformConsts.FW_PART_NUMBER], output[PlatformConsts.FW_ACTUAL]
