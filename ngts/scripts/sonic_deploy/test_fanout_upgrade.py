import logging

import pytest
import allure

from ngts.constants.constants import CliType, FanoutVersionConsts
from ngts.helpers.shared_fanout_helper import is_shared_fanout
from ngts.scripts.sonic_deploy.deploy_helper_methods import DeployTopologyHelper

logger = logging.getLogger()


@pytest.mark.disable_loganalyzer
def test_fanout_upgrade(topology_obj, workspace_path, setup_name, fanout_target_version, platform_params):
    """
    To upgrade fanout to target version.
    """
    if not fanout_target_version:
        pytest.skip("Skip it when fanout_target_version is not specified")

    setup_info = DeployTopologyHelper.get_info_from_topology(topology_obj, workspace_path)
    setup_info['setup_name'] = setup_name

    with allure.step('Installing fanout image on fanout'):
        for dut in setup_info['duts']:
            fanout_engine_type, fanout_name, fanout, fanout_player = dut['cli_obj'].get_fanout_info(
                topology_obj, dut['dut_alias'])
            if is_shared_fanout(fanout_player):
                logger.info(f"Skipping fanout upgrade on shared fanout {fanout_name}")
                continue
            if fanout_engine_type == CliType.SONIC:
                _, current_image = fanout.get_base_and_target_images()
                current_version = current_image.replace("SONiC-OS-", "")
                logger.info(f"Current fanout version: {current_version}")

                if current_version in FanoutVersionConsts.EXPECTED_SONIC_VERSION_LIST:
                    logger.info("Skipping fanout upgrade as it already runs an expected version")
                    continue

                # Prepare fanout_alias
                fanout_alias = 'fanout'
                if dut['dut_alias'] == 'dut-b':
                    fanout_alias = 'fanout-b'

                logger.info(f"Installing fanout image on {fanout_name}")
                fanout.deploy_sonic_fanout(topology_obj=topology_obj,
                                           target_version=fanout_target_version,
                                           setup_info=setup_info,
                                           platform_params=platform_params,
                                           fanout_name=fanout_name,
                                           fanout_alias=fanout_alias)
                logger.info(f"Fanout {fanout_name} upgrade and configuration completed")
            else:
                pytest.skip(f"{fanout_name} is not SONiC.")
