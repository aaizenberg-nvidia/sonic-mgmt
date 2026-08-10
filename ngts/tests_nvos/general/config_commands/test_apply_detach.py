import pytest
import time

from ngts.tools.test_utils import allure_utils as allure
import re
import logging
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.ConfigTool import ConfigTool
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_constants.constants_nvos import SystemConsts, ConfigConsts
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli


cumulus_owner = "gosaini"


logger = logging.getLogger()


@pytest.mark.cumulus
@pytest.mark.general
@pytest.mark.nvos_ci
@pytest.mark.configuration
@pytest.mark.simx
@pytest.mark.air
def test_detach(engines):
    """
        Test flow:
            1. run nv set system hostname <new_hostname>
            2. run nv config detach
            3. run nv config diff save as diff_output
            4. verify diff_output is empty
        """
    with allure.step('Run show system command and verify that each field has a value'):
        system = System()
        new_hostname_value = 'TestingConfigCmds'
        with allure.step('set hostname to be {hostname} - without apply'.format(hostname=new_hostname_value)):
            system.set(SystemConsts.HOSTNAME, new_hostname_value, apply=False)

            TestToolkit.GeneralApi[TestToolkit.tested_api].detach_config(engines.dut)

        diff_output = OutputParsingTool.parse_json_str_to_dictionary(
            NvueGeneralCli.diff_config(engines.dut)).get_returned_value()

        with allure.step('verify the pending list is empty'):
            assert not diff_output or diff_output == {}, \
                "pending revision should be empty, detach command should clean the last revision"


@pytest.mark.cumulus
@pytest.mark.general
@pytest.mark.configuration
@pytest.mark.simx
@pytest.mark.air
def test_apply_assume(engines):
    """
        Test flow:
            1. run nv set system hostname <hostname> without apply
            2. run nv --assume-yes config apply
            3. verify output includes "applied"
            4. run nv config show
            5. verify the show command output includes set/system/hostname
            6. run nv unset system hostname without apply
            7. run nv --assume-no config apply
            8. verify output includes "Declined apply after warnings"
            9. run nv config diff
            10. verify the diff command output includes unset/system/hostname
            11. run nv -y config apply
    """
    system = System(None)
    new_hostname = 'TESTINGAPPLY'
    with allure.step('set system hostname to {hostname} - without apply'.format(
            hostname=new_hostname)):
        system.set(SystemConsts.HOSTNAME, new_hostname, apply=False)

    with allure.step('apple system hostname change using {opt}'.format(opt=ConfigConsts.APPLY_ASSUME_YES)):
        apply_output = NvueGeneralCli.apply_config(engines.dut, False, ConfigConsts.APPLY_ASSUME_YES)
        assert 'applied' in apply_output, "failed to apply new system hostname"

    with allure.step('verify the show command output includes set/system/hostname'):
        show_after_apply = TestToolkit.GeneralApi[TestToolkit.tested_api].show_config(engines.dut)
        ConfigTool.verify_show_after_apply(show_after_apply, 'set', 'system/hostname', new_hostname).get_returned_value()

    with allure.step('unset system hostname - without apply'):
        system.unset(SystemConsts.HOSTNAME, False)

    with allure.step('apple system hostname change using {opt}'.format(opt=ConfigConsts.APPLY_ASSUME_NO)):
        apply_output = NvueGeneralCli.apply_config(engines.dut, False, ConfigConsts.APPLY_ASSUME_NO)
        assert 'Declined apply after warnings' in apply_output, "expected warning message wasn't found"

    with allure.step('verify output includes "Declined apply after warnings"'):
        diff_after_apply = TestToolkit.GeneralApi[TestToolkit.tested_api].diff_config(engines.dut)
        ConfigTool.verify_diff_after_config(diff_after_apply, 'unset', 'system/hostname').get_returned_value()

    with allure.step('apply system hostname change using {opt}'.format(opt=ConfigConsts.APPLY_YES)):
        NvueGeneralCli.apply_config(engines.dut, True, ConfigConsts.APPLY_YES)


@pytest.mark.cumulus
@pytest.mark.general
@pytest.mark.configuration
@pytest.mark.simx
def test_apply_rev_id(engines):
    """
        Test flow:
            1. run nv set system message pre-login <first_message>
            2. run nv config apply and save rev id as <first_rev_id>
            3. run nv set system message pre-login <second_message>
            4. run nv config apply and save rev id as <second_rev_id>
            5. run nv config apply <first_rev_id>
            6. verify the pre-login = <first_message>
            5. run nv config apply ref <second_rev_id>
            8. verify the pre-login = <second_message>
            9. check history data for both cases, should be: rev = original rev and ref should be rev_<rev_id>_apply_2
            10. try again and check rev_<rev_id>_apply_3

    """
    system = System(None)
    with allure.step('set pre-login message and apply'):
        output = system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value='"TESTING_001"',
                                    apply=True, dut_engine=engines.dut).verify_result()

    with allure.step('get the rev id and ref'):
        rev_id_1 = output.split()[-1].replace(']', '')
        ref_1 = 'rev_' + rev_id_1 + '_apply_1'

    with allure.step('set pre-login message and apply'):
        output = system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value='"TESTING_002"',
                                    apply=True, dut_engine=engines.dut).verify_result()

    with allure.step('get the rev id and ref'):
        rev_id_2 = output.split()[-1].replace(']', '')
        ref_2 = 'rev_' + rev_id_2 + '_apply_1'

    with allure.step('apply using rev id and verify output'):
        apply_output = NvueGeneralCli.apply_config(engine=engines.dut, rev_id=rev_id_1, option='-y')
        if is_bug_active(4868603):
            time.sleep(4)
        message_output = OutputParsingTool.parse_json_str_to_dictionary(system.message.show()).get_returned_value()

        with allure.step('Verify pre-login changed to TESTING_001 in show system'):
            ValidationTool.verify_field_value_in_output(message_output, SystemConsts.PRE_LOGIN_MESSAGE, "TESTING_001").verify_result()
        assert 'applied' in apply_output, "failed to apply using rev_id"

    with allure.step('apply using ref and verify output'):
        apply_output = NvueGeneralCli.apply_config(engine=engines.dut, rev_id=ref_2, option='-y')
        time.sleep(2)
        message_output = OutputParsingTool.parse_json_str_to_dictionary(system.message.show()).get_returned_value()

        with allure.step('Verify pre-login changed to TESTING_002 in show system'):
            ValidationTool.verify_field_value_in_output(message_output, SystemConsts.PRE_LOGIN_MESSAGE, "TESTING_002").verify_result()
        assert 'applied' in apply_output, "failed to apply using ref"

    with allure.step('run nv config history'):
        history_output = OutputParsingTool.parse_config_history(
            TestToolkit.GeneralApi[TestToolkit.tested_api].history_config(engines.dut)).get_returned_value()
        configs_list = [history_output[0], history_output[1]]
        expected_ref = [ref_2, rev_id_1]
    with allure.step('verify the history output'):
        verify_history_value(configs_list, expected_ref)


def verify_history_value(configs_list, expected_ref):
    """
    Verify that the history entries match the expected fields and references.
    Uses ValidationTool.compare_dictionaries() to compare the fields.

    :param configs_list: List of config history entries
    :param expected_ref: List of expected references
    :return: None, raises AssertionError if verification fails
    """
    for rev, ref in zip(configs_list, expected_ref):
        # Create a dictionary with only the fields we want to verify
        actual_fields = {
            'message': rev['message'],
            'reason': rev['reason'],
            'type': rev['type'],
            'user': rev['user']
        }

        with allure.step('verify history fields match expected values'):
            ValidationTool.compare_dictionaries(actual_fields, ConfigConsts.EXPECTED_HISTORY_FIELDS).verify_result()

        with allure.step('verify the history ref matches expected value'):
            assert rev['ref'] == ref, f"Expected ref {ref} but got {rev['ref']}"
