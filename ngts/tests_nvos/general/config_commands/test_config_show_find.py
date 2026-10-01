import logging
import pytest
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.general.config_commands.helpers import *
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit


cumulus_owner = "gosaini"


logger = logging.getLogger()


@pytest.mark.cumulus
@pytest.mark.general
@pytest.mark.simx
def test_config_show_all(engines):
    """
    Test flow:
        1. run nv config show save output as show_output
        2. run nv config show --all save output as show_all_output
        3. verify show_output is sub-dict of show_all_output
        4. verify default_values_dict is sub-dict of show_all_output
        5. verify default_values_dict is not sub-dict of show_output
    """
    with allure.step('run nv config show commands'):
        show_output = OutputParsingTool.parse_json_str_to_dictionary(
            TestToolkit.GeneralApi[TestToolkit.tested_api].show_config(engines.dut)).get_returned_value()
        show_all_output = OutputParsingTool.parse_json_str_to_dictionary(
            TestToolkit.GeneralApi[TestToolkit.tested_api].show_config(engines.dut, param='--all')).get_returned_value()

    with allure.step('run show commands and verify expected behaviors'):
        with allure.independent_step('verify nv config show output is sub-dict of nv config show --all output'):
            ValidationTool.compare_nested_dictionary_content(show_all_output[1], show_output[1], ['interface']).verify_result()

        with allure.independent_step('verify default values dict is sub-dict of nv config show --all output'):
            ValidationTool.compare_nested_dictionary_content(show_all_output[1], default_values_dict).verify_result()


@pytest.mark.cumulus
@pytest.mark.general
@pytest.mark.simx
def test_config_show_all_after_configuration(engines):
    """
    Test flow:
        1. run nv config show --all --pending save output verify error msg.
        2. run nv set system message pre-login "Testing"
        3. run nv config show --all --pending save output as pending_all_output
        4. verify default_values_dict is sub-dict of pending_all_output
        5. verify new_command_dict is sub-dict of pending_all_output
        6. apply configuration
        7. run nv config show --all save output as show_all_output
        8. verify new_command_dict is sub-dict of show_all_output
    """
    err_msg = "Error: No current pending revision for user "
    system = System(None)
    configuration_dict = {
        'set': {
            'system': {
                'message': {
                    'pre-login': "TESTING_001"
                }
            }
        }
    }

    with allure.step("Check config show all behavior after new configuration"):

        with allure.independent_step('run nv config show --pending command before configuration and verify expected output'):
            with allure.step("run nv config show --all --pending command"):
                show_output = TestToolkit.GeneralApi[TestToolkit.tested_api].show_config(engines.dut, revision='pending', param='--all')

            with allure.step("verify error message"):
                assert err_msg in show_output, f"the pending list should be empty and we expected for the next error message: {err_msg}"

        with allure.independent_step('configure new system message and verify expected behavior'):
            with allure.step('set pre-login message and apply'):
                system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value='"TESTING_001"', dut_engine=engines.dut).verify_result()

            with allure.step("run nv config show --all --pending command"):
                pending_all_output = OutputParsingTool.parse_json_str_to_dictionary(TestToolkit.GeneralApi[TestToolkit.tested_api].show_config(engines.dut, revision='pending', param='--all')).get_returned_value()

            with allure.independent_step('verify default values dict is sub-dict of the pending output'):
                output = ValidationTool.compare_nested_dictionary_content(pending_all_output[1], default_values_dict, ['interface'])

            with allure.independent_step('verify new configuration dict is sub-dict of the pending output'):
                ValidationTool.compare_nested_dictionary_content(pending_all_output[1], configuration_dict).verify_result()

            with allure.step("apply configuration"):
                NvueGeneralCli.apply_config(engines.dut)

            with allure.step("run config show --all command"):
                show_all_output = OutputParsingTool.parse_json_str_to_dictionary(TestToolkit.GeneralApi[TestToolkit.tested_api].show_config(engines.dut, param='--all')).get_returned_value()

            with allure.independent_step('verify new configuration dict is sub-dict of the applied output'):
                ValidationTool.compare_nested_dictionary_content(show_all_output[1], configuration_dict).verify_result()
