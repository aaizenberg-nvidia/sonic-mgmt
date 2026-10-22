import logging
import random

import pytest
from retry import retry

from ngts.nvos_constants.constants_nvos import ApiType, PlatformConsts
from ngts.nvos_tools.Devices.IbDevice import RosalindSurrogateSwitch
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.nvos_tools.infra.CurlTool import CurlTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import BmcUsers
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.mark.timeout(60 * MINUTE, func_only=True)
@pytest.mark.bmc
@pytest.mark.cumulus
@pytest.mark.dut_reboot
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
@pytest.mark.parametrize("platform_component_with_clear", ["bmc"], indirect=True)
def test_bmc_install(engines, devices, topology_obj, test_api, platform_component_with_clear, test_name, nv_command, show_platform_initial_state):
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
            # Either the platform has no BMC at all, or it has one with no image
            # pair that is safe to burn. SN6800 is the latter: the SPC6 images in
            # our catalog do not boot on it. Neither case is a test failure.
            pytest.skip("No BMC firmware image pair is defined for this platform")

    TestToolkit.tested_api = test_api
    component_name = platform_component_with_clear.get_resource_basename().lower()
    curl_tool = CurlTool(server_host=PlatformConsts.BMC_INTERNAL_IP, username=BmcUsers.root.username,
                         password=BmcUsers.root.another_password)

    try:
        path, filename, version_name = BmcTool.get_fw_component_version_previous(component_name)
        res_obj = BmcTool.fetch_and_install_platform_component(platform_component=platform_component_with_clear, path=path,
                                                               name=version_name, filename=filename, topology_obj=topology_obj,
                                                               test_name=test_name)
        with allure.step(f"Verify background copy status is completed in 7 minutes time"):
            BmcTool.verify_background_copy_completed_if_enabled(engines.dut, nv_command.platform, erot_name=PlatformConsts.EROT_BMC_PATH_NAME)
        with allure.step(f"verify operation time for install bmc {version_name!r} (duration: {res_obj.duration})"):
            OperationTime.verify_operation_time(res_obj.duration, 'install bmc', devices).verify_result()
        BmcTool.verify_platform_component_version(platform_component_with_clear, version_name)
    finally:
        path, filename, version_name = BmcTool.get_fw_component_version_latest(component_name)
        res_obj = BmcTool.fetch_and_install_platform_component(platform_component=platform_component_with_clear, path=path,
                                                               name=version_name, filename=filename, topology_obj=topology_obj,
                                                               test_name=test_name)
        with allure.step(f"verify operation time for install bmc {version_name!r} (duration: {res_obj.duration})"):
            OperationTime.verify_operation_time(res_obj.duration, 'install bmc', devices).verify_result()
        BmcTool.verify_platform_component_version(platform_component_with_clear, version_name)
        # BmcTool.compare_bmc_version_issu_module(engines, version_name)  !TBD uncomment after merge 1800 to master
        if isinstance(device, RosalindSurrogateSwitch) and PlatformConsts.EROT_BMC_PATH_NAME in device.constants.erots:
            with allure.step("Assert AutomaticBackgroundCopyEnabled for erot-bmc is false on Rosalind"):
                assert not BmcTool.is_automatic_background_copy_enabled(engines.dut, PlatformConsts.EROT_BMC_PATH_NAME), \
                    f"AutomaticBackgroundCopyEnabled is true for {PlatformConsts.EROT_BMC_PATH_NAME} on Rosalind system - expected false"
