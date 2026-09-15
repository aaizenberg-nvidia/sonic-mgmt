import logging
import random
import pytest

from ngts.tests_nvos.platform.test_platform_firmware_bios import helpers as platform_firmware_bios_helpers
from ngts.nvos_constants.constants_nvos import ApiType, NvosConst
from ngts.nvos_tools.infra.FWComponentsTool import FWComponentsTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.nvos_constants.constants_nvos import PlatformConsts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.bios
@pytest.mark.system
@pytest.mark.dut_reboot
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_bios_auto_update_disabled(devices, engines, topology_obj, test_api, original_version, test_name):
    """
    Test flow:
        1. fetch previous BIOS version
        2. downgrade to previous BIOS version
        3. reboot
        4. validate BIOS version was NOT updated
        5. cleanup
    """
    TestToolkit.tested_api = test_api
    with allure.step('Create System objects'):
        platform = Platform()
        system = System()
        component_name = 'bios'

    platform_firmware_bios_helpers.verify_current_version(original_version, system)
    platform_firmware_bios_helpers.verify_bios_auto_update_value(platform, NvosConst.DISABLED)
    path, filename, version_name = FWComponentsTool.get_fw_component_version_previous(component_name)
    res_obj = platform_firmware_bios_helpers.fetch_and_install_bios(platform=platform, path=path, name=version_name, filename=filename,
                                                                    topology_obj=topology_obj, test_name=test_name)
    with allure.step(f"verify operation time for install bios {version_name!r} (duration: {res_obj.duration})"):
        OperationTime.verify_operation_time(res_obj.duration, 'install bios', devices).verify_result()

    platform_firmware_bios_helpers.verify_bios_version(engines, platform, version_name)

    with allure.step(f'reboot with BIOS version {version_name=}'):
        res, duration = OperationTime.save_duration(f'reboot with BIOS {version_name}', '',
                                                    test_name, system.reboot.action_reboot, topology_obj=topology_obj)

    platform_firmware_bios_helpers.verify_bios_version(engines, platform, version_name)


@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.bios
@pytest.mark.system
@pytest.mark.dut_reboot
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_bios_auto_update_enabled(devices, engines, topology_obj, test_api, original_version, test_name):
    """
    Test flow:
        1. verify previous BIOS version and auto-update is enabled
        2. reboot
        3. validate BIOS version was updated in nv show platform firmware
    """
    TestToolkit.tested_api = test_api
    with allure.step('Create System objects'):
        platform = Platform()
        system = System()
        component_name = 'bios'
        path, filename, version_name = FWComponentsTool.get_fw_component_version_previous(component_name)

    try:
        platform.firmware.bios.set(op_param_name=PlatformConsts.FW_AUTO_UPDATE,
                                   op_param_value=NvosConst.ENABLED, apply=True).verify_result()
        platform_firmware_bios_helpers.verify_bios_auto_update_value(platform, NvosConst.ENABLED)
        TestToolkit.GeneralApi[test_api].save_config(engine=engines.dut)

        if platform_firmware_bios_helpers.get_bios_version(platform) != version_name:
            platform.firmware.bios.set(op_param_name=PlatformConsts.FW_AUTO_UPDATE,
                                       op_param_value=NvosConst.DISABLED, apply=True).verify_result()
            TestToolkit.GeneralApi[test_api].save_config(engine=engines.dut)

            res_obj = platform_firmware_bios_helpers.fetch_and_install_bios(platform=platform, path=path, name=version_name, filename=filename,
                                                                            topology_obj=topology_obj, test_name=test_name)
            with allure.step(f"verify operation time for install bios {version_name!r} (duration: {res_obj.duration})"):
                OperationTime.verify_operation_time(res_obj.duration, 'install bios', devices).verify_result()
            platform_firmware_bios_helpers.verify_bios_version(engines, platform, version_name)
    finally:
        with allure.step(f'Installation and reboot with latest BIOS version '):
            res, _ = OperationTime.save_duration(f'install BIOS 006', '',
                                                 test_name, system.reboot.action_reboot, topology_obj=topology_obj, system_is_ready_timeout=PlatformConsts.TIMEOUT_AFTER_BIOS_INSTALL)

        path, filename, version_name = FWComponentsTool.get_fw_component_version_latest(component_name)
        platform_firmware_bios_helpers.verify_bios_version(engines, platform, version_name)

        with allure.step('Verify operation time'):
            OperationTime.verify_operation_time(res.duration, 'install bios', devices).verify_result()

        platform.firmware.bios.set(op_param_name=PlatformConsts.FW_AUTO_UPDATE, op_param_value=NvosConst.DISABLED,
                                   apply=True).verify_result()
        TestToolkit.GeneralApi[test_api].save_config(engine=engines.dut)
