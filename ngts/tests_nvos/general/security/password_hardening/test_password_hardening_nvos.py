import random
import re
import string

import pytest
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine
from devts.infra.tools.connection_tools.utils import generate_strong_password
from devts.infra.tools.validations.traffic_validations.ping.send import ping_till_alive

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import TestFlowType
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.SerialConsoleTool import SerialConsoleTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.System import *
from ngts.nvos_tools.system.User import User
from ngts.tests_nvos.general.security.password_hardening.PwhConsts import PwhConsts
from ngts.tests_nvos.general.security.password_hardening.PwhTools import PwhTools
from ngts.tests_nvos.general.security.security_test_tools.constants import AaaConsts


cumulus_owner = "gosaini"


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
def test_password_hardening_weak_and_strong_passwords(engines, system):
    """
    @summary:
        Verify that setting a strong password succeeds, an setting a weak password fails
        * strong/weak - according to a password hardening configuration
        * verify - check that the set password command succeeds/fails and also check login succeeds/fails accordingly

        Steps:
        1. Set a password hardening configuration
        2. Pick a strong and a weak password
        3. Set the strong password
        4. Verify set succeeds
        5. Verify login with the strong password succeeds
        6. Try to set the weak password
        7. Verify set fails
        8. Verify that login with weak password fails
    """
    with allure.step("Enable password hardening feature"):
        system.security.password_hardening.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()

    with allure.step("Get password hardening configuration"):
        conf = OutputParsingTool.parse_json_str_to_dictionary(system.security.password_hardening.show()).get_returned_value()

    with allure.step("Pick a strong and a weak password"):
        username = AaaConsts.LOCALADMIN
        user_obj = System().aaa.user.user_id[username]
        strong_pw = PwhTools.generate_strong_pw(conf, username, [])
        weak_pw = PwhTools.generate_weak_pw(conf, username, [strong_pw])
        logging.info(f'Test username: "{username}"\nstrong password: "{strong_pw}"\nweak password: "{weak_pw}"')

    with allure.step("Set the strong password"):
        res_obj = user_obj.set(PwhConsts.PW, '"' + strong_pw + '"', apply=True)

    with allure.step("Verify set succeeds"):
        res_obj.verify_result(should_succeed=True)

    with allure.step("Verify login with the strong password succeeds"):
        PwhTools.verify_user(system, username)
        PwhTools.verify_login(engines.dut, username, strong_pw, login_should_succeed=True)

    with allure.step("Try to set the weak password and expect errors"):
        expected_errors = PwhTools.get_expected_errors(conf, username, weak_pw, [strong_pw])
        PwhTools.set_pw_expect_pwh_error(user_obj, weak_pw, expected_errors)

    with allure.step("Verify that login with weak password fails"):
        PwhTools.verify_login(engines.dut, username, weak_pw, login_should_succeed=False)
        PwhTools.verify_login(engines.dut, username, strong_pw, login_should_succeed=True)


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
def test_password_hardening_show_system_security(engines, system):
    """
    Check pwh configuration appears correctly in show output,
    and verify initial pwh configuration contains default values to all pwh settings.

    Steps:
        1. run show command
        2. verify all info exist in output
        3. verify all values are set to default initially
    """

    with allure.step("Run 'nv show system security password-hardening'"):
        output = OutputParsingTool.parse_json_str_to_dictionary(system.security.password_hardening.show()).get_returned_value()

    with allure.step("Verify all fields exist in output"):
        ValidationTool.verify_all_fields_value_exist_in_output_dictionary(output, PwhConsts.FIELDS).verify_result()

    with allure.step("Verify all initial values are set to default"):
        ValidationTool.validate_fields_values_in_output(
            expected_fields=PwhConsts.FIELDS, expected_values=PwhConsts.DEFAULTS.values(), output_dict=output
        ).verify_result()


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
def test_password_hardening_enable_disable(engines, system, testing_users):
    """
    Check pwh configuration values (in show) when feature is enabled/disabled.
    Also, check pwh functionality when feature is enabled/disabled.
    * functionality: pwh rules are enforced on new pws when feature is enabled,
        and not enforced when feature is disabled.

    Steps:
    1. Disable feature
    2. Verify pwh configuration in show
    3. Set weak pw which violates pwh conf rules
    4. Verify pw changed (no rule enforcing on new pws)
    5. Enable feature
    6. Verify pwh configuration in show matches to original pwh conf
    7. Set weak pw which violates (some) pwh conf rules
    8. Verify pw didn't change (rules enforced)
    9. Set strong pw
    10. Verify pw changed
    """
    pwh = system.security.password_hardening
    usrname = AaaConsts.LOCALADMIN
    orig_pw = testing_users[usrname][PwhConsts.PW]
    user_obj = testing_users[usrname][PwhConsts.USER_OBJ]
    pw_history = [orig_pw]

    with allure.step("Enable feature"):
        pwh.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()

    with allure.step("Take original pwh configuration"):
        orig_pwh_conf = OutputParsingTool.parse_json_str_to_dictionary(pwh.show()).get_returned_value()

    with allure.step("Disable feature"):
        pwh.set(PwhConsts.STATE, PwhConsts.DISABLED, apply=True).verify_result()

    with allure.step("Verify pwh configuration in show"):
        cur_pwh_conf = OutputParsingTool.parse_json_str_to_dictionary(pwh.show()).get_returned_value()
        ValidationTool.compare_dictionaries(cur_pwh_conf, PwhConsts.DEFAULTS, True).verify_result()

    with allure.step("Generate weak pw which violates orig pwh conf rules"):
        weak_pw = PwhTools.generate_weak_pw(orig_pwh_conf, usrname, orig_pw)

    with allure.step(f'Set weak pw "{weak_pw}" and apply'):
        user_obj.set(PwhConsts.PW, '"' + weak_pw + '"', apply=True).verify_result()
        pw_history.append(weak_pw)  # save successful new pws in this list for 'history record' for the test

    with allure.step("Enable feature"):
        pwh.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()

    with allure.step("Verify pwh configuration in show matches to original pwh conf"):
        cur_pwh_conf = OutputParsingTool.parse_json_str_to_dictionary(pwh.show()).get_returned_value()
        ValidationTool.compare_dictionaries(cur_pwh_conf, orig_pwh_conf, True).verify_result()

    with allure.step("Try to set the weak password and expect errors"):
        weak_pw2 = PwhTools.generate_weak_pw(cur_pwh_conf, usrname, weak_pw)
        expected_errors = PwhTools.get_expected_errors(cur_pwh_conf, usrname, weak_pw2, pw_history)
        PwhTools.set_pw_expect_pwh_error(user_obj, weak_pw2, expected_errors)

    with allure.step("Set strong pw"):
        strong_pw = PwhTools.generate_strong_pw(cur_pwh_conf, usrname, pw_history)
        user_obj.set(PwhConsts.PW, '"' + strong_pw + '"', apply=True).verify_result()


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
def test_password_hardening_set_unset(engines, system):
    """
    Verify set/unset to each pwh setting, with valid inputs.
    The verification is done in show only (without functionality check - tested later)

    Steps:
        1. Set pwh setting with valid value
        2. Verify new setting in show
        3. Unset pwh setting
        4. Verify setting is set to default value in show
        * do the above to each pwh setting separately
    """
    pwh_obj = system.security.password_hardening

    with allure.step("Enable feature"):
        pwh_obj.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()

    with allure.step("Get current password hardening configuration"):
        orig_pwh_conf = OutputParsingTool.parse_json_str_to_dictionary(pwh_obj.show()).get_returned_value()
        logging.info(f"Current (orig) password hardening configuration:\n{orig_pwh_conf}")

    pwh_fields = [setting for setting in PwhConsts.FIELDS if setting != PwhConsts.STATE]

    for setting in pwh_fields:
        with allure.step(f'Select random valid value for setting "{setting}" (except value "{orig_pwh_conf[setting]}")'):
            value = RandomizationTool.select_random_value(PwhConsts.VALID_VALUES[setting], [orig_pwh_conf[setting]]).get_returned_value()

            if setting == PwhConsts.EXPIRATION or setting == PwhConsts.EXPIRATION_WARNING:
                smaller = int(value if setting == PwhConsts.EXPIRATION_WARNING else orig_pwh_conf[PwhConsts.EXPIRATION_WARNING])
                larger = int(value if setting == PwhConsts.EXPIRATION else orig_pwh_conf[PwhConsts.EXPIRATION])
                while smaller > larger:
                    value = RandomizationTool.select_random_value(
                        PwhConsts.VALID_VALUES[setting], [orig_pwh_conf[setting]]
                    ).get_returned_value()
                    smaller = int(value if setting == PwhConsts.EXPIRATION_WARNING else orig_pwh_conf[PwhConsts.EXPIRATION_WARNING])
                    larger = int(value if setting == PwhConsts.EXPIRATION else orig_pwh_conf[PwhConsts.EXPIRATION])

            logging.info(f'Selected value for setting "{setting}" - "{value}")')

            assert value in PwhConsts.VALID_VALUES[setting], (
                f'Error: Something went wrong with randomizing new value for setting "{setting}".\n'
                f'Problem: value "{value}" is not in valid values.'
            )

            assert value != orig_pwh_conf[setting], (
                f'Error: Something went wrong with randomizing new value for setting "{setting}".\n'
                f'Problem: selected value "{value}" == orig value "{orig_pwh_conf[setting]}"'
            )

        with allure.step(f'Set password hardening setting "{setting}" to "{value}"'):
            pwh_obj.set(setting, value, apply=True).verify_result()

        with allure.step(f'Verify new setting ("{setting}" = "{value}") in show output'):
            PwhTools.verify_pwh_setting_value_in_show(pwh_obj, setting, value)

        with allure.step(f'Unset password hardening setting "{setting}"'):
            pwh_obj.unset(setting, apply=True).verify_result()

        with allure.step(f'Verify setting "{setting}" is set to default enabled ("{PwhConsts.ENABLED_CONF[setting]}") in show output'):
            PwhTools.verify_pwh_setting_value_in_show(pwh_obj, setting, PwhConsts.ENABLED_CONF[setting])


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
def test_password_hardening_set_invalid_input(engines, system):
    """
    Verify that running set command with invalid input values cause error and doesn't change pwh configuration.

    Steps:
        1. Set pwh setting with invalid value
        2. Verify error
        3. Verify setting is still set to original value
        * do the above to each pwh setting separately
    """
    pwh_obj = system.security.password_hardening

    with allure.step("Enable feature"):
        pwh_obj.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()

    with allure.step("Get current password hardening configuration"):
        orig_pwh_conf = OutputParsingTool.parse_json_str_to_dictionary(pwh_obj.show()).get_returned_value()
        logging.info(f"Current (orig) password hardening configuration:\n{orig_pwh_conf}")

    with allure.step("check errors for setting invalid values to all fields"):
        for setting in PwhConsts.FIELDS:
            with allure.independent_step(f'check invalid values for field "{setting}"'):
                # invalid values: 1.empty value; 2.just a random string; 3.another value which is not in valid values list
                invalid_values_to_test = PwhTools.generate_invalid_field_inputs(setting)

                for invalid_value in invalid_values_to_test:
                    with allure.independent_step(f'invalid value: "{invalid_value}"'):
                        logging.info(f'Invalid value for setting "{setting}" - "{invalid_value}")')

                        with allure.step(f'Try to set password hardening setting "{setting}" to "{invalid_value}"'):
                            res_obj = pwh_obj.set(setting, invalid_value, apply=False).ignore_result()

                        with allure.independent_step("Verify error"):
                            if invalid_value == "":
                                expected_err = PwhConsts.ERR_INCOMPLETE_SET_CMD
                            elif PwhConsts.VALID_VALUES[setting] == [PwhConsts.ENABLED, PwhConsts.DISABLED]:
                                expected_err = PwhConsts.ERR_INVALID_SET_ENABLE_DISABLED
                            elif setting in PwhConsts.MIN.keys():  # setting is numeric
                                if re.match(PwhConsts.REGEX_NUMERIC, str(invalid_value)):  # value is numeric but not in range
                                    expected_err = PwhConsts.ERR_RANGE.format(setting, PwhConsts.MIN[setting], PwhConsts.MAX[setting])
                                    # if int(invalid_value) < PwhConsts.MIN[setting]:
                                    #     expected_err = PwhConsts.ERR_VALUE_LESS_THAN_MIN.format(setting, invalid_value, PwhConsts.MIN[setting])
                                    # else:
                                    #     expected_err = PwhConsts.ERR_VALUE_GREATER_THAN_MAX.format(setting, invalid_value, PwhConsts.MAX[setting])
                                else:
                                    expected_err = PwhConsts.ERR_INTEGER_EXPECTED.format(invalid_value)  # value is not numeric
                            else:
                                expected_err = PwhConsts.ERR_INVALID_SET_CMD
                            PwhTools.verify_error(res_obj=res_obj, error_should_contain=expected_err)

                        with allure.independent_step(f'Verify setting "{setting}" is still "{orig_pwh_conf[setting]}" in show output'):
                            PwhTools.verify_pwh_setting_value_in_show(pwh_obj, setting, orig_pwh_conf[setting])

    with allure.step("Verify the constraint expiration-warning must be less or equal to expiration"):
        pwh_obj.unset(apply=True)

        with allure.step("Enable feature"):
            pwh_obj.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()

        conf = {PwhConsts.EXPIRATION: "-1", PwhConsts.EXPIRATION_WARNING: "-1"}
        PwhTools.set_pwh_conf(conf, pwh_obj, engines)

        with allure.step("Try to set expiration-warning which is larger than expiration"):
            exp = random.randint(0, PwhConsts.MAX[PwhConsts.EXPIRATION_WARNING] - 1)
            bad_exp_warn = random.randint(exp + 1, PwhConsts.MAX[PwhConsts.EXPIRATION_WARNING])
            logging.info(f"Set expiration to {exp} - should succeed")
            pwh_obj.set(PwhConsts.EXPIRATION, exp, apply=True).verify_result()
            logging.info(f"Try to set expiration-warning to {bad_exp_warn} (larger) - should fail")
            res_obj = pwh_obj.set(PwhConsts.EXPIRATION_WARNING, bad_exp_warn, apply=True).ignore_result()
            logging.info("Verify error")
            PwhTools.verify_error(res_obj=res_obj, error_should_contain=PwhConsts.ERR_EXP_WARN_LEQ_EXP)

        pwh_obj.unset(apply=True)

        with allure.step("Enable feature"):
            pwh_obj.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()

        conf = {PwhConsts.EXPIRATION: "-1", PwhConsts.EXPIRATION_WARNING: "-1"}
        PwhTools.set_pwh_conf(conf, pwh_obj, engines)

        with allure.step("Try to set expiration which is smaller than expiration-warning"):
            exp_warn = random.randint(1, PwhConsts.MAX[PwhConsts.EXPIRATION_WARNING])
            bad_exp = random.randint(0, exp_warn - 1)
            logging.info(f"Set expiration-warning to {exp_warn} - should succeed")
            pwh_obj.set(PwhConsts.EXPIRATION_WARNING, exp_warn, apply=True).verify_result()
            logging.info(f"Try to set expiration to {bad_exp} (smaller) - should fail")
            res_obj = pwh_obj.set(PwhConsts.EXPIRATION, bad_exp, apply=True).ignore_result()
            logging.info("Verify error")
            PwhTools.verify_error(res_obj=res_obj, error_should_contain=PwhConsts.ERR_EXP_WARN_LEQ_EXP)


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
@pytest.mark.checklist
def test_password_hardening_functionality(engines, system, testing_users, tst_all_pwh_confs):
    """
    @summary:
        Check functionality with several password hardening configurations.
            * configuration functionality - password rule enforcing according to the configuration
            * feature enable/disable already checked in previous test
            * all checked configurations will have {state enabled, expiration&warning disabled, history disabled},
            *   these fields are checked in other tests

        Steps:
            1. Set password hardening configuration
            2. Verify configuration in show
            3. Try to set rule violating password (weak pw)
            4. Verify error
            5. Verify password didn't change
            6. Set rule complying new password (strong pw)
            7. Verify success and that password changed
    """
    pwh_obj = system.security.password_hardening
    with allure.step("Enable feature"):
        pwh_obj.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()
    test_username = AaaConsts.LOCALADMIN
    orig_pw = testing_users[AaaConsts.LOCALADMIN][PwhConsts.PW]
    test_user_obj = testing_users[AaaConsts.LOCALADMIN][PwhConsts.USER_OBJ]

    all_confs = PwhTools.generate_configurations()
    test_confs = all_confs if tst_all_pwh_confs else random.sample(all_confs, PwhConsts.NUM_SAMPLES)
    logging.info(f"The test will check with {len(test_confs)} password hardening configurations")

    old_pw = orig_pw
    pw_history = [orig_pw]

    with allure.step("Test functionality for each password hardening configuration"):
        prev_conf = OutputParsingTool.parse_json_str_to_dictionary(pwh_obj.show()).get_returned_value()
        for i, conf in enumerate(test_confs):
            logging.info(f"Testing with conf #{i} :\n{conf}")  # for debugging

            with allure.step("Verify conf is a valid password hardening configuration"):
                PwhTools.assert_is_pwh_conf(conf)

            with allure.step("Set password hardening configuration"):
                logging.info(f"Set password hardening configuration:\n{conf}")
                PwhTools.set_pwh_conf(conf, pwh_obj, engines, prev_conf)

            lowers = False if conf[PwhConsts.LOWER_CLASS] == PwhConsts.ENABLED else True
            uppers = False if conf[PwhConsts.UPPER_CLASS] == PwhConsts.ENABLED else True
            digits = False if conf[PwhConsts.DIGITS_CLASS] == PwhConsts.ENABLED else True
            specials = False if conf[PwhConsts.SPECIAL_CLASS] == PwhConsts.ENABLED else True

            # when all are False (the relevant fields enabled) -> cant generate 'weak' password without any character
            if not (lowers or uppers or digits or specials):
                with allure.step("Generate weak password that breaks enabled policies in current configuration"):
                    weak_pw = PwhTools.generate_random_pw(lowers, uppers, digits, specials)
                    logging.info(f'Generated weak password: "{weak_pw}"')

                with allure.step(f'Test with the weak password "{weak_pw}"'):
                    PwhTools.verify_conf_with_password(engines.dut, conf, test_user_obj, weak_pw, old_pw, pw_history)

            if conf[PwhConsts.REJECT_USER_PASSW_MATCH] == PwhConsts.ENABLED:
                with allure.step(f'Test with the username as a password "{test_username}"'):
                    PwhTools.verify_conf_with_password(engines.dut, conf, test_user_obj, test_username, old_pw, pw_history)

            with allure.step("Generate strong password that applies policies of current configuration"):
                strong_pw = PwhTools.generate_strong_pw(conf, test_username, pw_history)
                logging.info(f'Generated strong password: "{strong_pw}"')

            with allure.step(f'Test with the strong password "{strong_pw}"'):
                PwhTools.verify_conf_with_password(engines.dut, conf, test_user_obj, strong_pw, old_pw, pw_history)
                pw_history.append(strong_pw)
                old_pw = strong_pw

            prev_conf = conf  # step


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
def test_password_hardening_history_functionality(engines, system, testing_users):
    """
    Test the functionality of history-cnt password hardening setting.

    Steps:
        1. Set history-cnt to N
        2. Set N new passwords
        3. Verify success (for each of them)
        4. Try to set these N passwords again
        5. Verify failure and error (for each of them)
        6. Set the original password
        7. Verify success (it is N+1 passwords ago)
    """
    # random.randint(PwhConsts.MIN[PwhConsts.HISTORY_CNT], PwhConsts.MAX[PwhConsts.HISTORY_CNT]) -> too long test
    hist_cnt = random.randint(PwhConsts.MIN[PwhConsts.HISTORY_CNT], PwhConsts.NUM_SAMPLES)
    logging.info(f"Chosen N = {hist_cnt}")

    pwh_obj = system.security.password_hardening

    with allure.step("Enable feature"):
        pwh_obj.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()
    test_username = AaaConsts.LOCALADMIN
    test_user_obj = testing_users[test_username][PwhConsts.USER_OBJ]
    orig_pw = testing_users[test_username][PwhConsts.PW]

    pw_history = [orig_pw]

    with allure.step(f'Set setting "{PwhConsts.HISTORY_CNT}" to N ( {hist_cnt} )'):
        pwh_obj.set(PwhConsts.HISTORY_CNT, hist_cnt, apply=True).verify_result()
        pwh_conf = OutputParsingTool.parse_json_str_to_dictionary(pwh_obj.show()).get_returned_value()

    with allure.step(f'Set N ( {hist_cnt} ) new passwords to user "{test_username}" and verify success'):
        pw_history = PwhTools.verify_set_passwords(
            hist_cnt, pwh_conf, test_username, test_user_obj, pw_history, engines.dut, should_succeed=True
        )

    with allure.step(
        f"Try to set some ( {min(PwhConsts.NUM_SAMPLES, hist_cnt)} ) of these N ( {hist_cnt} ) passwords again, and verify errors"
    ):
        # todo: currently user can reuse current pw to set as new pw (bug).
        #   after bug fix, change blow code to let the test pick also the current pass (pw_history[-1])
        cant_reuse_pws = pw_history[1: len(pw_history) - 1]  # can reuse orig and current pws, so don't pick them
        pws_to_try_again = random.sample(cant_reuse_pws, min(PwhConsts.NUM_SAMPLES, len(cant_reuse_pws)))
        pw_history = PwhTools.verify_set_passwords(
            pws_to_try_again, pwh_conf, test_username, test_user_obj, pw_history, engines.dut, should_succeed=False
        )

    with allure.step(f'Set the original password ( "{orig_pw}" ), and verify success'):
        PwhTools.verify_set_passwords([orig_pw], pwh_conf, test_username, test_user_obj, pw_history, engines.dut, should_succeed=True)


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
def test_password_hardening_expiration_functionality(engines, system, init_time, testing_users):
    """
    Test the functionality of password expiration setting.

    Steps:
        1. Set user1 with password pw1 ('old' password)
        2. Set expiration to N (should apply to old and new passwords)
        3. Set user2 with password pw2 ('new' password)
        4. Let N days to pass
            in each of these days, login with both users (expect success)
        5. After the N days pass (on day #N+1), login with both users
            expect password expiration prompt
    """
    pwh_obj = system.security.password_hardening
    with allure.step("Enable feature"):
        pwh_obj.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()
    user1 = AaaConsts.LOCALADMIN
    pw1 = testing_users[user1][PwhConsts.PW]
    user1_obj = testing_users[user1][PwhConsts.USER_OBJ]
    user2 = AaaConsts.LOCALMONITOR
    pw2 = testing_users[user2][PwhConsts.PW]
    user2_obj = testing_users[user2][PwhConsts.USER_OBJ]

    exp = random.randint(
        0, PwhConsts.MAX[PwhConsts.EXPIRATION]
    )  # can randomize between min_expiration to max_expiration but the test will be too long

    with allure.step(f"Set expiration setting to {exp}"):
        pwh_obj.set(PwhConsts.EXPIRATION_WARNING, -1).verify_result()
        pwh_obj.set(PwhConsts.EXPIRATION, exp, apply=True).verify_result()

    with allure.step("Set user2 with new password"):
        pwh_conf = OutputParsingTool.parse_json_str_to_dictionary(pwh_obj.show()).get_returned_value()
        logging.info(f"Current password hardening configuration:\n{pwh_conf}")
        pw2 = PwhTools.generate_strong_pw(pwh_conf, user2, [pw2])
        logging.info(f'Setting new password for user2 ("{user2}") : "{pw2}"')
        user2_obj.set(PwhConsts.PW, '"' + pw2 + '"', apply=True).verify_result()

    with allure.step(f"Let {exp} days pass, and on each day, login (with both users) and expect success"):
        expired_day = exp + 1
        day_num = 0  # today
        while day_num <= expired_day:
            if day_num == expired_day:
                with allure.step(f"Day #{day_num} - verify expired"):
                    PwhTools.verify_expiration(engines.dut.ip, user1, pw1)
                    PwhTools.verify_expiration(engines.dut.ip, user2, pw2)
                break
            else:
                with allure.step(f"Day #{day_num} - verify login success"):
                    PwhTools.verify_login(engines.dut, user1, pw1, login_should_succeed=True)
                    PwhTools.verify_login(engines.dut, user2, pw2, login_should_succeed=True)

                step = random.randint((expired_day - day_num) // 2, expired_day - day_num)
                with allure.step(f"Move {step} days ahead"):
                    PwhTools.move_k_days(num_of_days=step, system=system)
                    day_num += step


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
def test_password_hardening_expiration_warning_functionality(engines, system, init_time, testing_users):
    """
    Test the functionality of password expiration-warning setting.

    Steps:
        1. Set user1 with password pw1 ('old' password)
        2. Set expiration to N, and expiration-warning to M < N (should apply to old and new passwords)
        3. Set user2 with password pw2 ('new' password)
        4. Let K (=N-M) days to pass
            in each of these days, login with both users (expect success)
        5. After the K days pass (on day #K+1), login with both users
            expect password expiration warning
    """
    pwh_obj = system.security.password_hardening
    with allure.step("Enable feature"):
        pwh_obj.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()
    user1 = AaaConsts.LOCALADMIN
    pw1 = testing_users[user1][PwhConsts.PW]
    user1_obj = testing_users[user1][PwhConsts.USER_OBJ]
    user2 = AaaConsts.LOCALMONITOR
    pw2 = testing_users[user2][PwhConsts.PW]
    user2_obj = testing_users[user2][PwhConsts.USER_OBJ]

    exp = random.randint(2, PwhConsts.MAX[PwhConsts.EXPIRATION])
    exp_warn = random.randint(1, min(exp - 1, PwhConsts.MAX[PwhConsts.EXPIRATION_WARNING]))

    with allure.step(f"Set expiration-warning setting to {exp_warn}"):
        pwh_obj.set(PwhConsts.EXPIRATION_WARNING, exp_warn).verify_result()

    with allure.step(f"Set expiration setting to {exp}"):
        pwh_obj.set(PwhConsts.EXPIRATION, exp, apply=True).verify_result()

    with allure.step("Set user2 with new password"):
        pwh_conf = OutputParsingTool.parse_json_str_to_dictionary(pwh_obj.show()).get_returned_value()
        logging.info(f"Current password hardening configuration:\n{pwh_conf}")
        pw2 = PwhTools.generate_strong_pw(pwh_conf, user2, [pw2])
        logging.info(f'Setting new password for user2 ("{user2}") : "{pw2}"')
        user2_obj.set(PwhConsts.PW, '"' + pw2 + '"', apply=True).verify_result()

    with allure.step(f"Let {exp} days to pass"):
        warning_day = exp - exp_warn + 1
        day_num = 0  # today
        while day_num <= warning_day:
            if day_num == warning_day:
                with allure.step(f"Day #{day_num} - Expect warning"):
                    PwhTools.verify_expiration(engines.dut.ip, user1, pw1, expiration_type=PwhConsts.EXPIRATION_WARNING)
                    PwhTools.verify_expiration(engines.dut.ip, user2, pw2, expiration_type=PwhConsts.EXPIRATION_WARNING)
                break
            else:
                with allure.step(f"Day #{day_num} - verify login success"):
                    PwhTools.verify_login(engines.dut, user1, pw1, login_should_succeed=True)
                    PwhTools.verify_login(engines.dut, user2, pw2, login_should_succeed=True)

                step = random.randint((warning_day - day_num) // 2, warning_day - day_num)
                with allure.step(f"Move {step} days ahead"):
                    PwhTools.move_k_days(num_of_days=step, system=system)
                    day_num += step


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
def test_password_hardening_history_multi_user(engines, system, testing_users):
    """
    @summary:
        Test that password history of one user doesn't affect another user

        1. Set history-count to N
        2. Set user1 with N new passwords (pw1, pw2, ... , pwN)
        3. Set user2 with the same N passwords
        4. Expect success (user1's history shouldn't affect user2)
        5. Set user2 with another password (pwN+1)
        6. Try to set user1 with password pw1
        7. Expect failure (pw1 is in the previous N passwords for user1)
        8. Set user2 with password pw1
        9. Expect success (pw1 is no longer in the previous N passwords for user2)
    """
    pwh = system.security.password_hardening
    with allure.step("Enable feature"):
        pwh.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()

    user1 = AaaConsts.LOCALADMIN
    user1_obj = testing_users[user1][PwhConsts.USER_OBJ]
    pw1 = testing_users[user1][PwhConsts.PW]
    pw_hist1 = [pw1]

    user2 = AaaConsts.LOCALMONITOR
    user2_obj = testing_users[user2][PwhConsts.USER_OBJ]
    pw2 = testing_users[user2][PwhConsts.PW]
    pw_hist2 = [pw2]

    hist_cnt = random.randint(PwhConsts.MIN[PwhConsts.HISTORY_CNT], PwhConsts.NUM_SAMPLES)
    logging.info(f"Chosen history-count for test_history_multi_user_password_hardening: {hist_cnt}")

    with allure.step(f"Set history-count to {hist_cnt}"):
        pwh.set(PwhConsts.HISTORY_CNT, hist_cnt, apply=True).verify_result()

    with allure.step(f'Set user1 "{user1}" with {hist_cnt} new passwords'):
        pwh_conf = OutputParsingTool.parse_json_str_to_dictionary(pwh.show()).get_returned_value()

        for i in range(hist_cnt):
            pw1 = PwhTools.generate_strong_pw(pwh_conf, user1, pw_hist1)
            logging.info(f'Round #{i + 1} - Set user1 "{user1}" with password "{pw1}"')
            user1_obj.set(PwhConsts.PW, '"' + pw1 + '"', apply=True).verify_result()
            pw_hist1.append(pw1)

    with allure.step(f'Set user2 "{user2}" with the same {hist_cnt} passwords, and expect success'):
        passwords_to_set = pw_hist1[1:]  # take the same N new passwords that were set to user1
        assert len(passwords_to_set) == hist_cnt, (
            f"Error: Something is wrong.\nExpected len(passwords_to_set) : {hist_cnt}\n"
            f"Actual len(passwords_to_set) : {len(passwords_to_set)}\n"
            f"passwords_to_set : {passwords_to_set}"
        )
        for i in range(hist_cnt):
            pw2 = passwords_to_set[i]
            logging.info(f'Round #{i + 1} - Set user2 "{user2}" with password "{pw2}"')
            user2_obj.set(PwhConsts.PW, '"' + pw2 + '"', apply=True).verify_result()
            pw_hist2.append(pw2)

    with allure.step(f'Set user2 "{user2}" with another password (pw_{hist_cnt}+1)'):
        pw2 = PwhTools.generate_strong_pw(pwh_conf, user2, pw_hist2)
        logging.info(f'Set user2 "{user2}" with password "{pw2}"')
        user2_obj.set(PwhConsts.PW, '"' + pw2 + '"', apply=True).verify_result()
        pw_hist2.append(pw2)

    with allure.step(f'Try to set user1 "{user1}" with password pw_1 "{pw_hist1[1]}" and expect errors'):
        PwhTools.set_pw_expect_pwh_error(user1_obj, pw_hist1[1], [PwhConsts.WEAK_PW_ERRORS[PwhConsts.HISTORY_CNT]])
        if TestToolkit.tested_api == ApiType.NVUE:
            logging.info("Detaching the failed config")
            NvueGeneralCli.detach_config(engines.dut)

    with allure.step(f'Set user2 "{user2}" with password pw_1 "{pw_hist2[1]}"'):
        assert pw_hist1[1] == pw_hist2[1], (
            f"Error: expected pw_hist1[1] == pw_hist2[1]\npw_hist1[1] = {pw_hist1[1]}\npw_hist2[1] = {pw_hist2[1]}"
        )
        res_obj = user2_obj.set(PwhConsts.PW, '"' + pw_hist2[1] + '"', apply=True)

    with allure.step("Expect success"):
        res_obj.verify_result()


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
def test_password_hardening_history_increase(engines, system, testing_users):
    """
    @summary:
        Check if a record of password which is older than history-count is not dropped from the records.

        Steps:
        1. Set history-count to N
        2. Set 2N new passwords (pw1, .. , pwN , .. , pw_2N)
        3. Increase history-count to 2N
        4. Try to set again pw1, .. , pwN
        5. Expect failure
    """
    pwh = system.security.password_hardening
    with allure.step("Enable the feature"):
        pwh.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()
    username = AaaConsts.LOCALADMIN
    user_obj = testing_users[username][PwhConsts.USER_OBJ]
    orig_pw = testing_users[username][PwhConsts.PW]

    with allure.step("Set history-count"):
        hist_cnt = random.randint(PwhConsts.MIN[PwhConsts.HISTORY_CNT], PwhConsts.NUM_SAMPLES)
        logging.info(f"Set history-count to {hist_cnt}")
        pwh.set(PwhConsts.HISTORY_CNT, hist_cnt, apply=True).verify_result()
        pwh_conf = OutputParsingTool.parse_json_str_to_dictionary(pwh.show()).get_returned_value()

    with allure.step(f"Set 2*{hist_cnt} ({2 * hist_cnt}) new passwords"):
        pw_history = [orig_pw]
        for i in range(1, (2 * hist_cnt) + 1):
            pw_i = PwhTools.generate_strong_pw(pwh_conf, username, pw_history)
            logging.info(f'Round #{i} - Set user "{username}" with password "{pw_i}"')
            user_obj.set(PwhConsts.PW, '"' + pw_i + '"', apply=True).verify_result()
            pw_history.append(pw_i)

    with allure.step(f"Increase history-count to 2*{hist_cnt} ({2 * hist_cnt})"):
        pwh.set(PwhConsts.HISTORY_CNT, 2 * hist_cnt, apply=True).verify_result()
        pwh_conf[PwhConsts.HISTORY_CNT] = 2 * hist_cnt

    with allure.step(f"Try to set again the first {hist_cnt} passwords. Expect failure"):
        for i in range(1, hist_cnt + 1):
            pw_i = pw_history[i]
            logging.info(f'Round #{i} - Set user "{username}" with password pw_{i} - "{pw_i}"')
            PwhTools.set_pw_expect_pwh_error(user_obj, pw_i, [PwhConsts.WEAK_PW_ERRORS[PwhConsts.HISTORY_CNT]])


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
def test_password_hardening_history_when_feature_disabled(engines, system, testing_users):
    """
    @summary:
        Check if passwords are recorded in password history when feature is disabled

        Steps:
        1. Set history-cnt to N
        2. Disable the feature
        3. Set N new passwords
        4. Enable the feature
        5. Try to set again the N passwords
        6. Expect failure
    """
    username = AaaConsts.LOCALADMIN
    user_obj = testing_users[username][PwhConsts.USER_OBJ]
    orig_pw = testing_users[username][PwhConsts.PW]
    pwh = system.security.password_hardening
    with allure.step("Enable the feature"):
        pwh.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()
    pw_history = [orig_pw]

    with allure.step("Set history-cnt"):
        hist_cnt = random.randint(PwhConsts.MIN[PwhConsts.HISTORY_CNT], PwhConsts.NUM_SAMPLES)
        logging.info(f"Set history-cnt to {hist_cnt}")
        pwh.set(PwhConsts.HISTORY_CNT, hist_cnt, apply=True).verify_result()
        pwh_conf = OutputParsingTool.parse_json_str_to_dictionary(pwh.show()).get_returned_value()

    with allure.step("Disable the feature"):
        pwh.set(PwhConsts.STATE, PwhConsts.DISABLED, apply=True).verify_result()

    with allure.step(f"Set {hist_cnt} new passwords"):
        for i in range(1, hist_cnt + 1):
            pw_i = PwhTools.generate_strong_pw(pwh_conf, username, pw_history)
            logging.info(f'Round #{i} - Set user "{username}" wit pw_{i} - "{pw_i}"')
            user_obj.set(PwhConsts.PW, '"' + pw_i + '"', apply=True).verify_result()
            pw_history.append(pw_i)

    with allure.step("Enable the feature"):
        pwh.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()

    with allure.step(f"Try to set again the {hist_cnt} new passwords. Expect failure"):
        for i in range(1, hist_cnt):
            pw_i = pw_history[i]
            with allure.step(f'Set user "{username}" with pw_{i} - "{pw_i}" and expect errors'):
                logging.info(f'Round #{i} - Set user "{username}" with pw_{i} - "{pw_i}" and expect errors')
                PwhTools.set_pw_expect_pwh_error(user_obj, pw_i, [PwhConsts.WEAK_PW_ERRORS[PwhConsts.HISTORY_CNT]])


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
def test_password_hardening_max_password_len(disable_password_hardening):
    """
    Max allowed password is 511 chars. Verify it

    1. Set user with password of 511 (and lower) chars
    2. set user with password of 512 (and higher) chars
    """

    def case_flow(flow_type):
        is_good_flow = flow_type == TestFlowType.GOOD_FLOW
        password_len = PwhConsts.MAX_VALID_PASSWORD_LEN
        if not is_good_flow:
            password_len += 1
        username = User.generate_username()
        password = "".join(random.choice(string.ascii_lowercase) for _ in range(password_len))
        with allure.step(f'set user "{username}" with password of len {password_len}'):
            res = System().aaa.user.user_id[username].set("password", password)
        with allure.step(f"verify command {'success' if is_good_flow else 'fail'}"):
            res.verify_result(should_succeed=is_good_flow)
        if not is_good_flow:
            with allure.step("verify error message"):
                assert PwhConsts.ERR_MAX_PASSWORD_LEN in res.info, (
                    f"error message mismatch.\nexpected: {PwhConsts.ERR_MAX_PASSWORD_LEN}\nactual: {res.info}"
                )

    for test_flow in TestFlowType.ALL_TYPES:
        with allure.step(test_flow):
            case_flow(test_flow)


@pytest.mark.reboot
@pytest.mark.system
@pytest.mark.security
@pytest.mark.user
@pytest.mark.simx_security
def test_password_hardening_history_with_reboot(engines, devices, topology_obj):
    """
    advanced password history verification

    1.  reset admin password (unset)
    2.  save
    3.  make serial connection with admin
    4.  login and apply new password (pw1) - cali law
    5.  apply another new password (pw2)
    6.  reboot (no save)
    7.  login and apply same new password (pw1) - cali law - expect success
    8.  apply again the other new password (pw2) - expect success
    9.  save
    10. restore password (unset to default)
    11. disconnect
    12. login and try apply same new password (pw1) - cali law - expect rejected
    13. try to apply the other new password (pw2) - cali law - expect rejected
    """
    dut: LinuxSshEngine = engines.dut
    system = System()
    username = dut.username
    password = dut.password
    new_password1, new_password2 = generate_strong_password(15), generate_strong_password(15)
    pwh = system.security.password_hardening

    password_history_err = "Password should be different than.*previous passwords"

    def _login_and_apply_new_password_for_cali_law(serial_engine: PexpectSerialEngine, new_password, should_reject_for_history=False):
        SerialConsoleTool.login_nos(serial_engine, username, password, False)

        if should_reject_for_history:
            with allure.step(f"enter new password1: {new_password} - expect reject for password history"):
                serial_engine.run_cmd(new_password, password_history_err, 10)
            with allure.step("hit ctrl+c to stop login session"):
                serial_engine.serial_engine.sendcontrol("c")
                time.sleep(SerialConsoleTool.TIME_FOR_LOGIN_PROMPT)
        else:
            SerialConsoleTool.handle_change_password_prompt(serial_engine, new_password, False)
            dut.last_new_password = new_password

    def _set_apply_new_password(serial_engine: PexpectSerialEngine, new_password):
        serial_engine.run_cmd(f"nv set system aaa user {username} password {new_password}")
        serial_engine.run_cmd("nv config apply -y", "applied")
        dut.last_new_password = new_password

    def _reboot_and_wait_for_system_ready(serial_engine: PexpectSerialEngine):
        with allure.step("run reboot command"):
            serial_engine.run_cmd("sudo reboot")
        with allure.step("Ping switch until shutting down"):
            ping_till_alive(should_be_alive=False, destination_host=serial_engine.ip)
        with allure.step("wait for System is ready"):
            DutUtilsTool.wait_for_system_ready_in_serial(topology_obj, serial_engine, devices.dut.timeout_system_is_ready)

    with allure.step("Enable the feature"):
        pwh.set(PwhConsts.STATE, PwhConsts.ENABLED, apply=True).verify_result()
    with allure.step("reset admin password"):
        system.aaa.user.user_id["admin"].unset(apply=True).verify_result()
    with allure.step("save config"):
        NvueGeneralCli.save_config(engines.dut)
        engines.dut.disconnect()  # to prevent socket error after all flow
    with allure.step("make serial connection with admin"):
        with allure.step("enter to serial context"):
            serial: PexpectSerialEngine = SerialConsoleTool.get_serial_console_session(topology_obj)
        with allure.step("exit existing login"):
            SerialConsoleTool.exit_existing_login(serial)
    with allure.step(f'login and apply new password1 "{new_password1}" - cali law'):
        _login_and_apply_new_password_for_cali_law(serial, new_password1)
    with allure.step(f'apply another new password2 "{new_password2}"'):
        _set_apply_new_password(serial, new_password2)
    with allure.step("reboot (no save)"):
        _reboot_and_wait_for_system_ready(serial)
    with allure.step(f'login and apply again same new password1 "{new_password1}" - cali law - expect success'):
        _login_and_apply_new_password_for_cali_law(serial, new_password1)
    with allure.step(f'apply again another new password2 "{new_password2}" - expect success'):
        _set_apply_new_password(serial, new_password2)
    with allure.step("save config"):
        serial.run_cmd("nv config save", "saved")
    with allure.step("unset password (restore to default)"):
        serial.run_cmd(f"nv unset system aaa user {username}")
        serial.run_cmd("nv config apply -y", "applied")
    with allure.step("disconnect"):
        SerialConsoleTool.exit_existing_login(serial)
    with allure.step(f'login and try apply same new password1 "{new_password1}" - expect rejected'):
        _login_and_apply_new_password_for_cali_law(serial, new_password1, True)
    with allure.step(f'try to apply the other new password2 "{new_password2}" - expect rejected'):
        _login_and_apply_new_password_for_cali_law(serial, new_password2, True)
