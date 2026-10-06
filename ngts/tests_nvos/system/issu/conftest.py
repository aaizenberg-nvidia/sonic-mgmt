"""ISSU Test Configuration - Dynamic FW Image Detection for Last_FW testing."""
import logging
import os
import pytest

from ngts.nvos_constants.constants_nvos import IssuConsts, SystemConsts
from ngts.nvos_tools.infra.IbRouterTool import IbRouterTool
from ngts.nvos_tools.infra.NvosGitTool import NvosGitTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.ib_router.constants import IbRouterConsts
from ngts.tests_nvos.system.issu.test_system_issu import install_system_image_and_start_opensm

logger = logging.getLogger()


@pytest.fixture(scope='session', autouse=True)
def ensure_ib_routing_disabled(engines, devices):
    """Make sure ib-routing is disabled before the ISSU tests run, and restore it after.

    ISSU is rejected while ib-routing is enabled, so a profile left enabled by an aborted run
    might fail several test in this package. Each profile change reboots the switch.
    """
    profile_output = OutputParsingTool.parse_json_str_to_dictionary(
        System().profile.show()).get_returned_value()

    if SystemConsts.PROFILE_IB_ROUTING not in profile_output:
        logger.info(f"System profile has no '{SystemConsts.PROFILE_IB_ROUTING}' field. Nothing to disable")
    elif profile_output[SystemConsts.PROFILE_IB_ROUTING] == SystemConsts.PROFILE_STATE_DISABLED:
        logger.info('ib-routing is already disabled')
    else:
        with allure.step('Disable the ib-routing profile before running ISSU tests'):
            IbRouterTool.disable_ib_router_profile()
            IbRouterTool.verify_profile_status(SystemConsts.PROFILE_STATE_DISABLED, SystemConsts.DEFAULT_NUM_SWIDS)

    yield

    with allure.step('Restore the ib-routing profile to its original state after running ISSU tests'):
        if profile_output.get(SystemConsts.PROFILE_IB_ROUTING) != SystemConsts.PROFILE_STATE_ENABLED:
            logger.info('ib-routing was already disabled, nothing to restore')
        else:
            number_of_swids = profile_output.get(SystemConsts.PROFILE_NUMBER_OF_SWIDS, IbRouterConsts.SWID_NUM)
            IbRouterTool.enable_ib_router_profile(number_of_swids=number_of_swids)
            IbRouterTool.verify_profile_status(SystemConsts.PROFILE_STATE_ENABLED, number_of_swids)


@pytest.fixture(scope='session', autouse=True)
def prepare_and_recover_issu(engines, devices, target_version, issu_version, request):
    """Setup: find previous FW image. Teardown: recover to target version."""
    logger.info("Starting ISSU session")
    logger.info(f"Target version: {target_version}")

    # Use NvosGitTool to find previous FW image
    git_tool = NvosGitTool()

    try:
        version, image_type = git_tool.parse_version_from_path(target_version)
        logger.info(f"Detected image type: {image_type}")
    except ValueError as e:
        logger.warning(f"Could not parse target version: {e}")
        image_type = 'dev'

    prev_fw_path = git_tool.find_previous_fw_image_path(target_version, asic_type='QTM3')

    if not prev_fw_path or not os.path.exists(prev_fw_path):
        # Fallback to GA version - test will be skipped if Last_FW == Last_GA
        logger.warning(f"Could not find previous FW image, falling back to GA version")
        logger.warning(f"Last_FW test will be skipped (same as Last_GA)")
        prev_fw_path = issu_version  # Set to GA version so skip condition triggers

    logger.info(f"Previous FW image: {prev_fw_path}")
    request.config.issu_last_fw_path = prev_fw_path

    yield

    # Teardown: recover system to target version
    system = System(devices_dut=devices.dut)
    if hasattr(engines, 'ha') and hasattr(engines, 'hb'):
        with allure.step("Recover system to target image"):
            install_system_image_and_start_opensm(engines, devices.dut, system, target_version, False)
        with allure.step("Clean temp files"):
            for engine, output in [(engines.ha, IssuConsts.SERVER_OUTPUT), (engines.hb, IssuConsts.CLIENT_OUTPUT)]:
                if engine.run_cmd(f'ls {output}'):
                    engine.run_cmd(f'rm -f {output}')
    else:
        with allure.step("Recover system to target image"):
            install_system_image_and_start_opensm(engines, devices.dut, system, target_version)
