import logging
import random
import time

import pytest

from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.nvos_tools.infra.DMIDecodeTool import DMIDecodeTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_constants.constants_nvos import ApiType, PlatformConsts
from ngts.nvos_tools.Devices.IbDevice import RosalindSurrogateSwitch
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit

from ngts.scripts.bios_config import configure_bios
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.platform.firmware_telemetry_helpers import (
    assert_gnmi_firmware_version_matches_nvue,
)
from ngts.tests_nvos.system.reboot_telemetry_helpers import gnmi_client_for_dut

logger = logging.getLogger()


@pytest.fixture(scope='module')
def restore_bios(topology_obj):
    yield
    configure_bios(topology_obj)


@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.bios
@pytest.mark.dut_reboot
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
@pytest.mark.parametrize("platform_component_with_clear", ["bios"], indirect=True)
def test_bios_manual_update(engines, devices, topology_obj, test_api, platform_component_with_clear, test_name, nv_command, show_platform_initial_state):
    """
    Test flow:
        1. fetch alternate BIOS version
        2. install the alternate BIOS version using nv action install fae platform firmware BIOS <abs-path-to-file>
        3. power cycle
        4. validate BIOS version was changed in nv show platform firmware
        5. install the current latest BIOS version (the one the machine begun the test with)
        6. power cycle
        7. validate BIOS version was changed in nv show platform firmware

    Description:
    This test is specifically for systems that do not have BIOS auto-update feature. It is pruned for such systems,
    as the following test logic will not work if auto-update is enabled.
    Currently, only Juliet systems are supported.

    """
    TestToolkit.tested_api = test_api
    component_name = platform_component_with_clear.get_resource_basename().lower()
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)

    try:
        path, filename, version_name = BmcTool.get_fw_component_version_previous(component_name)
        res_obj = BmcTool.fetch_and_install_platform_component(platform_component=platform_component_with_clear, path=path,
                                                               name=version_name, filename=filename, topology_obj=topology_obj,
                                                               test_name=test_name)
        BmcTool.verify_platform_component_version(platform_component_with_clear, version_name)
        assert_gnmi_firmware_version_matches_nvue(
            gnmi_client, PlatformConsts.FW_BIOS, version_name
        )
        DMIDecodeTool.verify_dmi_info(engines, devices)
        with allure.step(f"Verify background copy status is completed in 7 minutes time"):
            BmcTool.verify_background_copy_completed_if_enabled(engines.dut, nv_command.platform, erot_name=PlatformConsts.EROT_CPU_PATH_NAME)
        with allure.step(f"verify operation time for install bios {version_name!r} (duration: {res_obj.duration})"):
            OperationTime.verify_operation_time(res_obj.duration, 'install bios', devices).verify_result()
    finally:
        path, filename, version_name = BmcTool.get_fw_component_version_latest(component_name)
        BmcTool.fetch_and_install_platform_component(platform_component=platform_component_with_clear, path=path,
                                                     name=version_name, filename=filename, topology_obj=topology_obj,
                                                     test_name=test_name)
        BmcTool.verify_platform_component_version(platform_component_with_clear, version_name)
        assert_gnmi_firmware_version_matches_nvue(
            gnmi_client, PlatformConsts.FW_BIOS, version_name
        )
        DMIDecodeTool.verify_dmi_info(engines, devices)

        if isinstance(devices.dut, RosalindSurrogateSwitch):
            with allure.step("Assert AutomaticBackgroundCopyEnabled is true on Rosalind system"):
                assert BmcTool.is_automatic_background_copy_enabled(engines.dut, PlatformConsts.EROT_CPU_PATH_NAME), \
                    f"AutomaticBackgroundCopyEnabled is false for {PlatformConsts.EROT_CPU_PATH_NAME} on Rosalind system - expected true"
