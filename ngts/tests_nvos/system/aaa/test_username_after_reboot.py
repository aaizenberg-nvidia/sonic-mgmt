import logging

import pytest

from ngts.nvos_constants.constants_nvos import ApiType, SystemConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


@pytest.mark.dut_reboot
@pytest.mark.system
def test_saved_users_after_reboot(engines):
    """
    Test flow:
        1. create new user
        2. nv config save
        3. reboot
        4. verify user still
    """
    with allure.step("Set new user (monitor) and save"):
        system = System(force_api=ApiType.NVUE)
        viewer_name, viewer_password = system.aaa.user.set_new_user(role=SystemConsts.ROLE_VIEWER, apply=True)
        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)
    with allure.step("Set another new user (admin) without save"):
        configurator_name, configurator_password = system.aaa.user.set_new_user(role=SystemConsts.ROLE_CONFIGURATOR, apply=True)
    with allure.step("reboot testing"):
        system.action_reboot(send_user_confirmation='y').verify_result()
    with allure.step("Verify saved user exists and non saved does not exist"):
        output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.user.show()).get_returned_value()
        assert configurator_name not in output.keys(), f"the not saved user {viewer_name} in the users list"
        assert viewer_name in output.keys(), f"the saved user {configurator_name} not in the users list"
