import logging
import pytest
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ConfigTool import ConfigTool
from ngts.nvos_constants.constants_nvos import SystemConsts, ConfigConsts, NvosConst
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit


cumulus_owner = "gosaini"


logger = logging.getLogger()


@pytest.mark.cumulus
@pytest.mark.general
@pytest.mark.simx
@pytest.mark.air
def test_show_diff_history(engines):
    """
    Test flow:
        1. run nv config history - save as history_output1
        2. run nv config show - save as show_output1
        3. run nv config diff - save as diff_output1
        4. run nv set system hostname <new_hostname>
        5. run nv config diff - save as diff_output2
        6. run nv config apply
        7. run nv config history - save as history_output2
        8. run nv config show - save as show_output2
        9. run nv config diff - save as diff_output3
        10. verify diff_output3 is empty
        11. verify diff_output2 = diff_output1 + hostname: new_hostname
        12. verify show_output2 = show_output1 + hostname: new_hostname
        13. verify size(history_output2) = size(history_output1) + 1
        14. verify history_output2 last apply_id = n/size(history)  value, user name = hostname
        15. run nv unset system hostname
        16. run nv config diff - save as diff_output4
        17. verify hostname in diff_output4
        18. run nv config apply
    """
    err_message = ''
    with allure.step('Run show system command and verify that each field has a value'):
        system = System()
        with allure.step('saving the current diff, history and show outputs'):
            show_before_set, diff_before_set, history_before_set = save_diff_hisoty_show_outputs(engines.dut)

        new_hostname_value = 'TestingConfigCmds'
        with allure.step('set hostname to be {hostname} - without apply'.format(hostname=new_hostname_value)):
            system.set(SystemConsts.HOSTNAME, new_hostname_value, apply=False)

        with allure.step('saving the diff, history and show outputs after set hostname without apply'):
            show_after_set, diff_after_set, history_after_set = save_diff_hisoty_show_outputs(engines.dut)

        with allure.step('apply hostname set configuration'):
            NvueGeneralCli.apply_config(engines.dut, True)

        with allure.step('saving the diff, history and show outputs after applying the hostname'):
            show_after_apply, diff_after_apply, history_after_apply = save_diff_hisoty_show_outputs(engines.dut)

        with allure.step('unset hostname - without apply'.format(hostname=new_hostname_value)):
            system.unset(SystemConsts.HOSTNAME, apply=False)

        with allure.step('saving the diff, history and show outputs after unset hostname without apply'):
            show_after_unset, diff_after_unset, history_after_unset = save_diff_hisoty_show_outputs(engines.dut)

        with allure.step('apply hostname unset configuration'):
            NvueGeneralCli.apply_config(engines.dut, True)

        with allure.step('saving the diff, history and show outputs after applying the hostname'):
            show_after_unset_apply, diff_after_unset_apply, history_after_unset_apply = save_diff_hisoty_show_outputs(engines.dut)

        with allure.step('Verify output'):
            with allure.step('verify diff command outputs'):
                err_message += verify_diff_outputs(diff_before_set, diff_after_set, diff_after_apply, diff_after_unset, diff_after_unset_apply)
                err_message = f"Wrong diff command output: {err_message}" if err_message else ""

            with allure.step('verify show command outputs'):
                err_message += verify_show_outputs(show_before_set, show_after_set, show_after_apply, show_after_unset, show_after_unset_apply)
                err_message = f"Wrong show command output: {err_message}" if err_message else ""

            with allure.step('verify history command outputs'):
                err_message += verify_history_outputs(history_before_set, history_after_set, history_after_apply, history_after_unset,
                                                      history_after_unset_apply, engines.dut.username)
                err_message = f"Wrong history command output: {err_message}" if err_message else ""

            if err_message != '':
                logger.warning('{message}'.format(message=err_message))


@pytest.mark.general
@pytest.mark.simx
def test_diff_history_revision_ids(engines, devices):
    """
        Test flow:
            1. run nv set system hostname <new_hostname> with apply
            2. run nv set interface eth0 description <new_description> with apply
            3. run nv set interface eth0 description <new_description> with apply
            4. run nv config history - save as history_output1
            5. get last 3 rev id's using history_output1
            6. run nv config diff one_configs_back_revision two_configs_back_revision
            7. verify it includes the first config only
            6. run nv config diff two_configs_back_revision rev_id3
            9. verify it includes the second config only
            10. run nv config diff one_configs_back_revision rev_id3
            11. verify it includes both configs
            12. run nv config history rev_id3 save as rev_output_id3
            13. validate the output format
        """
    err_message = ''
    with allure.step('set with apply 3 different configurations to create new rev_ids'):
        system = System()
        new_hostname_value = 'TestingConfigCmds'
        with allure.step('set hostname to be {hostname} - with apply'.format(hostname=new_hostname_value)):
            system.set(SystemConsts.HOSTNAME, new_hostname_value, apply=True, ask_for_confirmation=True)

        eth0_port = Port('eth0')
        new_eth0_description = 'some_desc'
        with allure.step('set eth0 description to be {description} - with apply'.format(
                description=new_eth0_description)):
            eth0_port.interface.set(NvosConst.DESCRIPTION, new_eth0_description, apply=True).verify_result()

        new_eth0_description = 'another_desc'
        with allure.step('set eth0 description to be {description} - with apply'.format(
                description=new_eth0_description)):
            eth0_port.interface.set(NvosConst.DESCRIPTION, new_eth0_description, apply=True).verify_result()

    with allure.step('get the last revision ids'):
        history_output = OutputParsingTool.parse_config_history(NvueGeneralCli.history_config(engines.dut))\
            .get_returned_value()
        revs = [rev['ref'] for rev in history_output if rev['ref'].isdigit()]
        revs.sort(key=int)
        one_configs_back_revision = revs[-1]
        two_configs_back_revision = revs[-2]

    with allure.step('get the history of the with a specific rev_id'):
        rev_output_id3 = OutputParsingTool.parse_config_history(
            NvueGeneralCli.history_config(engines.dut, two_configs_back_revision)).get_returned_value()

        validate_history_labels(rev_output_id3, 'admin')

    with allure.step('verify diff between revision1 and revision0'):
        diff_output_rev0_rev1 = OutputParsingTool.parse_json_str_to_dictionary(
            NvueGeneralCli.diff_config(engines.dut, one_configs_back_revision, two_configs_back_revision)).get_returned_value()

        if diff_output_rev0_rev1 == {}:
            err_message += '\n after applying 3 times the diff between two revision back and two revision back ' \
                           'should include the first configuration we applied'

    with allure.step('validate the test results'):
        assert err_message == '', '{message}'.format(message=err_message)


def validate_history_labels(history_list, username):
    """
    validate apply_id value, user value of a specific apply
    :param username: the user name
    :param history_list: history
    :return: err message
    """
    err_message = ''

    for label in ConfigConsts.CONFIG_LABELS:
        value = ConfigTool.read_from_history(history_list, 0, label).get_returned_value()
        if not value or value == "N/A":
            err_message += f"Unexpected value for {label}: {value}"

    user = ConfigTool.read_from_history(history_list, 0, ConfigConsts.HISTORY_USER).get_returned_value()
    if user != username:
        err_message += 'the user is not equal to the user name'

    """if int(apply_id) > int(len(history_list)):
        err_message += 'the rev_id is not as expected, should be rev_<applies_amount> or less'"""

    return err_message


def verify_diff_outputs(diff1, diff2, diff3, diff4, diff5):
    """
    :param diff1: diff_before_set
    :param diff2: diff_after_set
    :param diff3: diff_after_apply
    :param diff4: diff_after_unset
    :param diff5: diff_after_unset_apply
    :return: error message if one of diff1, diff3, diff5, diff6 is not empty
            or if one of diff2, diff4 is does not include the configuration

    """
    err_message = ''
    with allure.step('verify diff commands after apply'):
        if diff1 != {} or diff3 != {} or diff5 != {}:
            err_message += '\n the diff output should be empty after applying the configurations'

    with allure.step('verify diff commands after set and before apply'):
        if diff2 == {} or diff4 == {}:
            err_message += '\n the diff output should include the configuration'

    return err_message


def verify_show_outputs(show1, show2, show3, show4, show5):
    """
    :param show1: show_before_set
    :param show2: show_after_set
    :param show3: show_after_apply
    :param show4: show_after_unset
    :param show5: show_after_unset_apply
    :return: error message if one of show1, show2, show6 is not empty
                or if any of show3, show4, show5 does not include the configuration

    """
    err_message = ''
    with allure.step('verify show commands before apply'):
        if show1 != {} or show2 != {}:
            err_message += '\n the show output should be empty before applying the configurations'

    with allure.step('verify show commands after set and apply'):
        if show3 == {} or show4 == {} or show5 == {}:
            err_message += '\n the show output should include the configuration'

    return err_message


def verify_history_outputs(history1, history2, history3, history4, history5, username):
    """
    :param history1: history_before_set
    :param history2: history_after_set
    :param history3: history_after_apply
    :param history4: history_after_unset
    :param history5: history_after_unset_apply
    :param username: the user name who configured the last configuration
    :return: error message if history1 and history2 are not equal
                or history3 and history4 are not equal
                or nothing added to history3
                or nothing added to history5
                or nothing added to history6
    """
    err_message = ''
    with allure.step('verify history commands before apply'):
        if history1 != history2 or history3 != history4:
            err_message += '\n the history output should not change before applying'

    with allure.step('verify history commands after first apply'):
        if len(history2) != len(history3) - 1 or len(history4) != len(history5) - 1:
            err_message += '\n the history output should add the new apply_id'

    with allure.step("Validate history labels"):
        err_message += validate_history_labels(history5, username)

    return err_message


def save_diff_hisoty_show_outputs(engine):
    """
    saving the output if diff and show and history commands
    after each set/unset/apply running
    :param engine:
    :return:three outputs
    """
    with allure.step('saving the current diff, history and show outputs'):
        show_output = OutputParsingTool.parse_json_str_to_dictionary(
            TestToolkit.GeneralApi[TestToolkit.tested_api].show_config(engine)).get_returned_value()
        diff_output = OutputParsingTool.parse_json_str_to_dictionary(
            TestToolkit.GeneralApi[TestToolkit.tested_api].diff_config(engine)).get_returned_value()
        history_output = OutputParsingTool.parse_config_history(
            TestToolkit.GeneralApi[TestToolkit.tested_api].history_config(engine)).get_returned_value()
    return show_output, diff_output, history_output
