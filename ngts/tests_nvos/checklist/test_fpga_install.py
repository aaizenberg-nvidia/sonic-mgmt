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

logger = logging.getLogger()


@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.bmc
@pytest.mark.dut_reboot
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
@pytest.mark.parametrize("platform_component_with_clear", ["fpga"], indirect=True)
def test_fpga_install(engines, devices, topology_obj, test_api, platform_component_with_clear, test_name, nv_command, show_platform_initial_state):
    """
    @summary: test all these commands:
        nv show platform firmware BMC files
        nv action delete platform firmware BMC files <file-name>
        nv action fetch platform firmware BMC <remote-url-fetch>
        nv action install platform firmware BMC files <file-name> [force]
    Note: because firmware installation takes a long time and the test does it twice,
    the test randomly chooses to do it on OpenApi or NVUE

    Test flow:
        1. Check if device has BMC or skip
        2. Fetches and installs BMC alternate_version
        3. Verifies correct versioning for installed fw package.
        4. Fetches and installs BMC current_version
        5. Verifies correct versioning for installed fw package.
    """
    device = devices.dut
    with allure.step('Check whether device has BMC'):
        bmc_older_version_path = getattr(device, 'bmc_older_version_path', None)
        if bmc_older_version_path is None:
            pytest.skip("Device does not have BMC")
    TestToolkit.tested_api = test_api
    component_name = platform_component_with_clear.get_resource_basename().lower()
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)

    platform = Platform()
    platform_output = OutputParsingTool.parse_show_output_to_dict(platform.show()).get_returned_value()

    with allure.step("Check whether device has non encrypted FPGA"):
        encrypted_fpga = '_encrypted'
        non_encrypted_fpga_pns = {"692-9K36F-A5MV-JS0", "692-9K36F-00MV-JSL", "920-9K36F-00MV-ES1"}
        part_number = platform_output[PlatformConsts.FW_PART_NUMBER].strip()
        if part_number in non_encrypted_fpga_pns:
            encrypted_fpga = ''
        component_name = f'{component_name}{encrypted_fpga}'

    try:
        path, filename, version_name = BmcTool.get_fw_component_version_previous(component_name)
        res_obj = BmcTool.fetch_and_install_platform_component(platform_component=platform_component_with_clear, path=path,
                                                               name=version_name, filename=filename, topology_obj=topology_obj,
                                                               test_name=test_name)
        with allure.step(f"Verify background copy status is completed in 7 minutes time"):
            BmcTool.verify_background_copy_completed_if_enabled(engines.dut, nv_command.platform, erot_name=PlatformConsts.EROT_FPGA_PATH_NAME)
        with allure.step(f"verify operation time for install fpga {version_name!r} (duration: {res_obj.duration})"):
            OperationTime.verify_operation_time(res_obj.duration, 'install fpga', devices).verify_result()
        BmcTool.verify_platform_component_version(platform_component_with_clear, version_name)
        assert_gnmi_firmware_version_matches_nvue(
            gnmi_client, PlatformConsts.FW_FPGA, version_name
        )
    finally:
        path, filename, version_name = BmcTool.get_fw_component_version_latest(component_name)
        res_obj = BmcTool.fetch_and_install_platform_component(platform_component=platform_component_with_clear, path=path,
                                                               name=version_name, filename=filename, topology_obj=topology_obj,
                                                               test_name=test_name)
        with allure.step(f"verify operation time for install fpga {version_name!r} (duration: {res_obj.duration})"):
            OperationTime.verify_operation_time(res_obj.duration, 'install fpga', devices).verify_result()
        BmcTool.verify_platform_component_version(platform_component_with_clear, version_name)
        assert_gnmi_firmware_version_matches_nvue(
            gnmi_client, PlatformConsts.FW_FPGA, version_name
        )
