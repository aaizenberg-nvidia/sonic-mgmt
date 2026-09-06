import logging
import random

import pytest

from ngts.nvos_constants.constants_nvos import ApiType, PlatformConsts
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.platform.firmware_telemetry_helpers import (
    assert_gnmi_firmware_version_matches_nvue,
)
from ngts.tests_nvos.system.reboot_telemetry_helpers import gnmi_client_for_dut

logger = logging.getLogger(__name__)


@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.bmc
@pytest.mark.dut_reboot
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
@pytest.mark.parametrize("platform_component_with_clear", ["sma"], indirect=True)
def test_sma_install(engines, devices, topology_obj, test_api, platform_component_with_clear, test_name, nv_command, show_platform_initial_state):
    """
    @summary: test all these commands:
        nv show platform firmware sma files
        nv action delete platform firmware sma files <file-name>
        nv action fetch platform firmware sma <remote-url-fetch>
        nv action install platform firmware sma files <file-name> [force]
    Note: because firmware installation takes a long time and the test does it twice,
    the test randomly chooses to do it on OpenApi or NVUE

    Test flow:
        1. Check if device has BMC or skip
        2. Fetches and installs sma alternate_version
        3. Verifies correct versioning for installed fw package.
        4. Fetches and installs sma current_version
        5. Verifies correct versioning for installed fw package.
    """
    # Skip test if device has no SMA components (e.g., RosalindSwitch hardware)
    if not devices.dut.sma_components or devices.dut.sma_amount == 0:
        pytest.skip(f"Device {devices.dut.__class__.__name__} has no SMA components - skipping SMA install test")

    device = devices.dut
    with allure.step('Check whether device has BMC'):
        bmc_older_version_path = getattr(device, 'bmc_older_version_path', None)
        if bmc_older_version_path is None:
            pytest.skip("Device does not have BMC")
    TestToolkit.tested_api = test_api
    component_name = platform_component_with_clear.get_resource_basename().lower()

    platform = Platform()
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)

    try:
        path, filename, version_name = BmcTool.get_fw_component_version_previous(component_name)
        res_obj = BmcTool.fetch_and_install_platform_component(platform_component=platform_component_with_clear, path=path,
                                                               name=version_name, filename=filename, topology_obj=topology_obj,
                                                               test_name=test_name)

        with allure.step(f"verify operation time for install sma {version_name!r} (duration: {res_obj.duration})"):
            OperationTime.verify_operation_time(res_obj.duration, 'install sma', devices).verify_result()
        validate_firmware_versions(version_name, device.sma_amount, platform)
        for idx in range(1, device.sma_amount + 1):
            sma_name = f"SMA{idx}"
            assert_gnmi_firmware_version_matches_nvue(gnmi_client, sma_name, version_name)
        BmcTool.verify_platform_component_version(platform_component_with_clear, version_name)
    finally:
        path, filename, version_name = BmcTool.get_fw_component_version_latest(component_name)
        res_obj = BmcTool.fetch_and_install_platform_component(platform_component=platform_component_with_clear, path=path,
                                                               name=version_name, filename=filename, topology_obj=topology_obj,
                                                               test_name=test_name)
        with allure.step(f"verify operation time for install sma {version_name!r} (duration: {res_obj.duration})"):
            OperationTime.verify_operation_time(res_obj.duration, 'install sma', devices).verify_result()
        validate_firmware_versions(version_name, device.sma_amount, platform)
        for idx in range(1, device.sma_amount + 1):
            sma_name = f"SMA{idx}"
            assert_gnmi_firmware_version_matches_nvue(gnmi_client, sma_name, version_name)


def validate_firmware_versions(version_name, sma_amount, platform):
    firmware_shown = OutputParsingTool.parse_json_str_to_dictionary(platform.firmware.show()).get_returned_value()
    with allure.step('validate sma firmware versions'):
        for index in range(1, sma_amount + 1):
            curr_sma = f"SMA{index}"
            with allure.independent_step(f"Checking {curr_sma} version"):
                actual_firmware = firmware_shown[curr_sma][PlatformConsts.FW_ACTUAL]
                assert actual_firmware == version_name, f"{curr_sma} version mismatch: Expected '{version_name}', Got '{actual_firmware}'"
