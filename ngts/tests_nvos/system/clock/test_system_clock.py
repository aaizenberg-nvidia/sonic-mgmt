import logging
import random
from datetime import datetime
from ngts.nvos_constants.constants_nvos import ApiType, DateTimeConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.system.clock.ClockConsts import ClockConsts
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
import pytest
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.Tools import Tools
from retry.api import retry_call


cumulus_owner = "lbenbasat"


# --------------------- Basic Good Flow --------------------- #


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.clock
@pytest.mark.cumulus
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_show_system_date_time(test_api, engines, devices, nv_command):
    """
    @summary:
    Verify that 'nv show system date-time' returns all expected fields with values
    and that key values match the output from 'timedatectl'.

    @steps:
    1. Execute 'nv show system date-time'
    2. Validate that all expected fields exist and have non-empty values
    3. Compare the timezone and local time with 'timedatectl' output

    @param test_api: The API type to use (from ApiType enum)
    @param engines: Object giving access to DUT shell/command interfaces
    @param devices: Provides DUT metadata and expected schema
    @param nv_command: Wrapper for executing NVUE CLI/API commands
    """
    TestToolkit.tested_api = test_api

    with allure.step('Run show system command and verify that each field has a value'):
        date_time_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.datetime.show()).get_returned_value()
        ValidationTool.verify_all_fields_value_exist_in_output_dictionary(
            date_time_output, nv_command.system.get_expected_fields(devices.dut, 'date-time')).verify_result()

    with allure.step("Validate timezone value"):
        # verify that timezones are the same
        timedatectl_output_str = engines.dut.run_cmd(ClockConsts.TIMEDATECTL_CMD)
        nv_timezone = ClockTools.normalize_timezone(date_time_output[DateTimeConsts.TIMEZONE])
        timedatectl_timezone = ClockTools.get_timezone_from_timedatectl_output(timedatectl_output_str)
        ValidationTool.compare_values(nv_timezone, timedatectl_timezone).verify_result()

    with allure.step("Validate date-time value"):
        # extract date-time value from outputs
        timedatectl_output_str = engines.dut.run_cmd(ClockConsts.TIMEDATECTL_CMD)
        show_system_datetime = date_time_output[DateTimeConsts.LOCAL_TIME]
        timedatectl_datetime = ClockTools.get_datetime_from_timedatectl_output(timedatectl_output_str)
        # verify both date-time values are the same
        ClockTools.verify_same_datetimes(show_system_datetime, timedatectl_datetime)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.clock
@pytest.mark.cumulus
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_set_unset_timezone_ntp_off(test_api, engines, system, valid_timezones, orig_timezone, ntp_off):
    """
    @summary:
    Validate timezone configuration using NVUE when NTP is disabled.

    @steps:
    1. Pick a valid timezone (different from the current one)
    2. Set it using 'nv set system date-time timezone'
    3. Verify that it is correctly reflected in NVUE and timedatectl
    4. Unset the timezone and validate it returns to default

    @param test_api: The API type to use (from ApiType enum)
    @param engines: Provides shell/command execution on DUT
    @param system: Interface to system-level NVUE commands
    @param valid_timezones: List of valid timezones from config (e.g. timezone.yaml)
    @param orig_timezone: Original timezone on the DUT before the test
    @param ntp_off: Fixture that disables NTP to allow manual time changes
    """
    TestToolkit.tested_api = test_api

    with allure.step("Pick a random new timezone to set (from timezone.yaml)"):
        new_timezone = RandomizationTool.select_random_value(list_of_values=valid_timezones,
                                                             forbidden_values=[orig_timezone, None]).get_returned_value()

    with allure.step("Set the new timezone with 'nv set system date-time timezone'"):
        ClockTools.set_timezone(new_timezone, system, apply=True).verify_result()

    with allure.step("Verify 'nv show system date-time' output"):
        with allure.independent_step("Verify new timezone in 'nv show system date-time' and in 'timedatectl'"):
            ClockTools.verify_timezone(engines, system, expected_timezone=new_timezone)

        if not TestToolkit.devices.dut.is_eth():
            with allure.independent_step(f"Verify NTP related values in 'nv show system date-time'"):
                ClockTools.verify_ntp_fields(system, False)

    with allure.step("Unset the timezone with 'nv unset system date-time timezone'"):
        ClockTools.unset_timezone(system, apply=True).verify_result()

    with allure.step("Verify default timezone in 'nv show system date-time' and in 'timedatectl'"):
        ClockTools.verify_timezone(engines, system, expected_timezone=ClockConsts.DEFAULT_TIMEZONE)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.clock
@pytest.mark.cumulus
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_set_unset_timezone_ntp_on(test_api, engines, system, valid_timezones, orig_timezone, ntp_on):
    """
    @summary:
    Verify that the system timezone can be set and unset correctly when NTP is enabled.

    @steps:
    1. Select a random valid timezone from the configuration (excluding the current one).
    2. Set the new timezone using NVUE.
    3. Validate the timezone is reflected correctly in both 'nv show system date-time' and 'timedatectl'.
    4. Confirm NTP-related fields reflect that NTP is enabled.
    5. Unset the timezone.
    6. Ensure the system reverts to the default timezone.

    @param test_api: Type of API being used for the test (CLI, REST, etc.)
    @param engines: Test execution engines providing DUT command access
    @param system: NVUE system object to apply configuration
    @param valid_timezones: List of allowed timezones for testing
    @param orig_timezone: Timezone configured prior to test start
    @param ntp_on: Fixture that ensures NTP is enabled before the test
    """
    TestToolkit.tested_api = test_api

    with allure.step("Pick a random new timezone to set (from timezone.yaml)"):
        new_timezone = RandomizationTool.select_random_value(list_of_values=valid_timezones,
                                                             forbidden_values=[orig_timezone, None]).get_returned_value()

    with allure.step("Set the new timezone with 'nv set system date-time timezone'"):
        ClockTools.set_timezone(new_timezone, system, apply=True).verify_result()

    with allure.step("Verify 'nv show system date-time' output"):
        with allure.independent_step("Verify new timezone in 'nv show system date-time' and in 'timedatectl'"):
            ClockTools.verify_timezone(engines, system, expected_timezone=new_timezone)
        if not TestToolkit.devices.dut.is_eth():
            with allure.independent_step(f"Verify NTP related values in 'nv show system date-time'"):
                ClockTools.verify_ntp_fields(system, True)

    with allure.step("Unset the timezone with 'nv unset system date-time timezone'"):
        ClockTools.unset_timezone(system, apply=True).verify_result()

    with allure.step("Verify default timezone in 'nv show system date-time' and in 'timedatectl'"):
        ClockTools.verify_timezone(engines, system, expected_timezone=ClockConsts.DEFAULT_TIMEZONE)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.clock
@pytest.mark.cumulus
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_action_change_date_time_ntp_off(test_api, engines, system, init_datetime, pwh_off, ntp_off):
    """
    @summary:
    Validate the system correctly updates its date and time using NVUE when NTP is disabled.

    @steps:
    1. Select a random valid date and time.
    2. Apply the date-time update using 'nv action change system date-time'.
    3. Confirm the change is reflected in 'nv show system date-time'.
    4. Also validate the new time using the system's 'timedatectl' output.

    @param test_api: Type of API being used for the test (CLI, REST, etc.)
    @param engines: Test execution engines providing DUT command access
    @param system: NVUE system object to apply configuration
    @param init_datetime: Initial datetime before the test begins (fixture)
    @param ntp_off: Fixture that ensures NTP is turned off
    @param pwh_off: Fixture to disable persistent write hooks if applicable
    """
    TestToolkit.tested_api = test_api

    with allure.step("Pick random new date-time to set"):
        new_datetime = RandomizationTool.select_random_datetime().get_returned_value()

    with allure.step("Set the new date-time with 'nv action change system date-time'"):
        retry_call(_set_new_date_time, [system, new_datetime], exceptions=AssertionError, tries=3, delay=2)

    with allure.step("Run 'nv show system' and 'timedatectl' immediately to verify date-time changed"):
        show_date_time_output_str = system.datetime.show()
        timedatectl_output_str = engines.dut.run_cmd(ClockConsts.TIMEDATECTL_CMD)

    with allure.step("Verify date-time"):
        show_system_datetime = ClockTools.get_local_time_from_show_system_date_time_output(show_date_time_output_str)
        ClockTools.verify_same_datetimes(new_datetime, show_system_datetime)
        timedatectl_datetime = ClockTools.get_datetime_from_timedatectl_output(timedatectl_output_str)
        ClockTools.verify_same_datetimes(new_datetime, timedatectl_datetime)


'''
# The test below is not relevant for this release.
# In the first design, the feature should have supported changing time only
# For the current GA decided not to support it.
# maybe in the future will support changing time only too
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.clock
def test_action_change_time_only_ntp_off(engines, system, datetime_backup_restore):  # todo: what would disable ntp?
    """
    @summary:
        Check that system date-time change action command work correctly with valid input of time only
        example: 'nv action change system date-time' should allow parameter 'hh:mm:ss' only

        Test steps:
        1. Pick a random time
        2. Set new time with the action change command
        3. Verify new time in 'nv show system' and 'timedatectl'
    """

    with allure.step("Pick random new time to set"):
        logging.info("Pick random new time to set")
        new_time = RandomizationTool.select_random_time().get_returned_value()
        new_datetime = datetime_backup_restore.split(' ')[0] + ' ' + new_time

    with allure.step("Set the new time with 'nv action change system date-time'"):
        logging.info("Set the new time with 'nv action change system date-time'")
        system.datetime.action_change(params=new_time).verify_result()  # todo: implement System.DateTime.action_change_datetime()

    with allure.step("Run 'nv show system' and 'timedatectl' immediately to verify date-time changed"):
        logging.info("Run 'nv show system' and 'timedatectl' immediately to verify date-time changed")
        show_system_output_str = system.show()
        timedatectl_output_str = engines.dut.run_cmd(ClockConsts.TIMEDATECTL_CMD)

    with allure.step("Verify date-time"):
        logging.info("Verify date-time")
        show_system_datetime = ClockTools.get_datetime_from_show_system_output(show_system_output_str)
        ClockTools.verify_same_datetime(new_datetime, show_system_datetime)
        timedatectl_datetime = ClockTools.get_datetime_from_timedatectl_output(timedatectl_output_str)
        ClockTools.verify_same_datetime(new_datetime, timedatectl_datetime)
'''


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.clock
@pytest.mark.cumulus
def test_rtc_in_local_tz(engines, nv_command):
    """
    @summary:
    Verify that setting 'rtc-in-local-tz' via timedatectl is reflected correctly in NVUE output.
    Steps:
        1. Check default (should be 'no')
        2. Set to 'yes' via timedatectl, validate NVUE reflects it
        3. Revert to 'no', and re-validate
    """
    with allure.step("Validate default rtc-in-local-tz is 'no'"):
        ClockTools.verify_rtc_in_local_tz(DateTimeConsts.RtcInLocalTZ.NO.value, nv_command)

    with allure.step("Set rtc-in-local-tz to 'yes' and validate"):
        ClockTools.set_rtc_in_local_tz(engines, True)
        ClockTools.verify_rtc_in_local_tz(DateTimeConsts.RtcInLocalTZ.YES.value, nv_command)

    with allure.step("Revert rtc-in-local-tz to 'no' and validate"):
        ClockTools.set_rtc_in_local_tz(engines, False)
        ClockTools.verify_rtc_in_local_tz(DateTimeConsts.RtcInLocalTZ.NO.value, nv_command)


# --------------------- Basic Bad Flow --------------------- #


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.clock
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_set_system_invalid_timezone_ntp_off_error_flow(test_api, engines, system, valid_timezones, orig_timezone, ntp_off):
    """
    @summary:
        Check that system timezone set command works correctly with invalid inputs
        * invalid - not exist in timezone.yaml file

        Main Steps:
            1. Set new timezone to random invalid timezone
            2. Verify error
            3. verify timezone hasn't changed (still the original one) in 'nv show system' and 'timedatectl'
    """
    TestToolkit.tested_api = test_api

    # try to set random strings of varying length (also len 0 -> "")
    with allure.step("Pick random strings of different lengths as bad timezone"):
        for n in range(0, 10, 3):
            with allure.step("Pick a random string of length {n} as bad timezone".format(n=n)):
                bad_timezone = RandomizationTool.get_random_string(length=n)

        ClockTools.set_invalid_timezone_and_verify(bad_timezone, system, engines, orig_timezone)

    # try to change random existing timezones from timezone.yaml and set them
    with allure.step("Pick 3 random timezone from timezone.yaml and change them to test case sensitivity"):
        random_timezones = RandomizationTool.select_random_values(list_of_values=valid_timezones,
                                                                  forbidden_values=[None],
                                                                  number_of_values_to_select=3).get_returned_value()
        bad_timezones = list(map(lambda s: ClockTools.alternate_capital_lower(s), random_timezones))

    for bad_timezone in bad_timezones:
        ClockTools.set_invalid_timezone_and_verify(bad_timezone, system, engines, orig_timezone)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.clock
@pytest.mark.cumulus
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_set_system_invalid_timezone_ntp_on_error_flow(test_api, engines, system, valid_timezones, orig_timezone, ntp_on):
    """
    @summary:
        Check that system timezone set command works correctly with invalid inputs, when ntp is enabled
        * invalid - not exist in timezone.yaml file

        Main Steps:
            1. Set new timezone to random invalid timezone
            2. Verify error
            3. verify timezone hasn't changed (still the original one) in 'nv show system' and 'timedatectl'
    """
    TestToolkit.tested_api = test_api

    # try to set random strings of varying length (also len 0 -> "")
    with allure.step("Pick random strings of different lengths as bad timezone"):
        for n in range(0, 10, 3):
            with allure.step("Pick a random string of length {n} as bad timezone".format(n=n)):
                bad_timezone = RandomizationTool.get_random_string(length=n)

        ClockTools.set_invalid_timezone_and_verify(bad_timezone, system, engines, orig_timezone)

    # try to change random existing timezones from timezone.yaml and set them
    with allure.step("Pick 3 random timezone from timezone.yaml and change them to test case sensitivity"):
        random_timezones = random.sample(valid_timezones, 3)
        logging.info(f'Picked random timezones: {random_timezones}')
        bad_timezones = list(map(lambda s: ClockTools.alternate_capital_lower(s), random_timezones))

    for bad_timezone in bad_timezones:
        ClockTools.set_invalid_timezone_and_verify(bad_timezone, system, engines, orig_timezone)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.clock
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_change_valid_datetime_ntp_on_error_flow(test_api, engines, system, ntp_on):
    """
    @summary:
        Check that system date-time change action command gives the right error with ntp enabled,
        even in the situation of giving a valid date-time parameter.
        * valid input can be any parameter in the format "YYYY-MM-DD hh:mm:ss"
            while both date ("YYYY-MM-DD") and time ("hh:mm:ss") define a valid date and time, which exists in the
            calendar year, and is in the 'allowed range'
        * the 'allowed range' is between ClockConsts.MIN_SYSTEM_DATETIME to ClockConsts.MAX_SYSTEM_DATETIME

        Main Steps:
            1. Try to change date-time with several invalid inputs
            2. verify error
            3. verify that date-time hasn't changed
    """
    TestToolkit.tested_api = test_api

    with allure.step("Pick {} random new date-time to set".format(ClockConsts.NUM_SAMPLES)):
        new_datetimes = []
        for i in range(ClockConsts.NUM_SAMPLES):
            new_datetimes.append(RandomizationTool.select_random_datetime().get_returned_value())
            logging.info('Random datetime #{}: ""'.format(i, new_datetimes[i]))

    for dt in new_datetimes:
        with allure.step("Test that 'nv action change system date-time {}' with ntp on failure".format(dt)):
            ClockTools.change_datetime_and_verify_error(dt, system, engines, ClockConsts.ERR_DATETIME_NTP)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.clock
@pytest.mark.cumulus
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_change_invalid_datetime_ntp_off_error_flow(test_api, engines, system, ntp_off):
    """
    @summary:
        Check that system date-time change action command works correctly (error) with invalid inputs,
            and when ntp is disabled.
        * valid input can be any parameter in the format "YYYY-MM-DD hh:mm:ss" or just "hh:mm:ss",
            while both date ("YYYY-MM-DD") and time ("hh:mm:ss") define a valid date and time, which exists in the
            calendar year, and is in the 'allowed range'
        * the 'allowed range' is between ClockConsts.MIN_SYSTEM_DATETIME to ClockConsts.MAX_SYSTEM_DATETIME

        Main Steps:
            1. Generate several invalid inputs, then for each one: (don't stop in case of assertion failure)
                1. Try to change set date-time to that input
                2. verify error
                3. verify the proper error message was printed
                4. verify that date-time hasn't changed
            2. Print all the failed assertions (if any)
    """
    _change_invalid_datetime_test_flow(test_api, engines, system)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.clock
@pytest.mark.cumulus
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_change_invalid_datetime_ntp_on_error_flow(test_api, engines, system, ntp_on):
    """
    @summary:
        Check that system date-time change action command works correctly (error) with invalid inputs,
            and when ntp is on (errors should be the same as they are when ntp is off)
        * valid input can be any parameter in the format "YYYY-MM-DD hh:mm:ss" or just "hh:mm:ss",
            while both date ("YYYY-MM-DD") and time ("hh:mm:ss") define a valid date and time, which exists in the
            calendar year, and is in the 'allowed range'
        * the 'allowed range' is between ClockConsts.MIN_SYSTEM_DATETIME to ClockConsts.MAX_SYSTEM_DATETIME

        Main Steps:
            1. Generate several invalid inputs, then for each one: (don't stop in case of assertion failure)
                1. Try to change set date-time to that input
                2. verify error
                3. verify that error message is about ntp
                4. verify that date-time hasn't changed
            2. Print all the failed assertions (if any)
    """
    _change_invalid_datetime_test_flow(test_api, engines, system)


def _change_invalid_datetime_test_flow(test_api, engines, system):
    TestToolkit.tested_api = test_api
    with allure.step("Generate several invalid inputs for 'nv action change system date-time"):
        bad_inputs = ClockTools.generate_invalid_datetime_inputs()
        logging.info("Generated invalid date-time inputs:\n{bi}".format(bi=bad_inputs))

    errors = list()
    for bad_datetime in bad_inputs:
        if bad_datetime == '':
            errs = ClockConsts.ERR_EMPTY_PARAM
        elif len(bad_datetime.split(' ')) == 1:
            if ClockTools.is_valid_system_date(bad_datetime):
                errs = ClockConsts.ERR_EMPTY_PARAM
            else:
                errs = [ClockConsts.ERR_INVALID_DATE.format(bad_datetime)]
        else:  # there are 2 arguments in the input
            b_date = bad_datetime.split(' ')[0]
            b_time = bad_datetime.split(' ')[1]
            if not ClockTools.is_valid_system_date(b_date):
                errs = [ClockConsts.ERR_INVALID_DATE.format(b_date)]
            elif not ClockTools.is_valid_time(b_time):
                errs = [ClockConsts.ERR_INVALID_TIME.format(b_time)]
            else:
                errs = ClockConsts.ERR_INVALID_DATETIME
        try:
            with allure.step(f'Check bad datetime: "{bad_datetime}"'):
                ClockTools.change_datetime_and_verify_error(bad_datetime, system, engines, errs)
        except AssertionError as e:
            errors.append(bad_datetime)
            logging.error(f"AssertionError: {e}")
    assert not errors, f"Test failed, search the log for 'AssertionError'. Failed inputs: {errors}"


# --------------------- Other Flows --------------------- #


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.clock
@pytest.mark.cumulus
def test_new_time_in_logs(engines, system, orig_timezone, valid_timezones, init_datetime, ntp_off, pwh_off):
    """
    @summary:
        Test that new date and time appear in logs timestamps after timezone/date-time changes.

        Steps:
            1. Show logs timestamp are same as current time (before change)
            2. Set different timezone
            3. Verify logs timestamp similar to the date-time in show
            4. Unset timezone
            5. Verify logs timestamp similar to the date-time in show
            6. Change date-time
            7. Verify logs timestamp similar to the date-time in show
    """
    with allure.step('Verify show date-time same as last log timestamp'):
        ClockTools.verify_show_and_log_times(system)

    with allure.step('Set a random timezone'):
        new_timezone = RandomizationTool.select_random_value(list_of_values=valid_timezones,
                                                             forbidden_values=[orig_timezone, None]
                                                             ).get_returned_value()
        logging.info('Random timezone: "{}"'.format(new_timezone))

        logging.info("Set the new timezone")
        ClockTools.set_timezone(new_timezone, system, apply=True).verify_result()

    with allure.step('Verify show date-time same as last log timestamp'):
        ClockTools.verify_show_and_log_times(system)

    with allure.step("Unset the timezone"):
        ClockTools.unset_timezone(system, apply=True).verify_result()

    with allure.step('Verify show date-time same as last log timestamp'):
        ClockTools.verify_show_and_log_times(system)

    with allure.step("Change date-time"):
        logging.info("Pick random new date-time to set")
        now = datetime.now()
        min_dt = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
        max_dt = now.replace(hour=23, minute=59, second=59, microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
        new_datetime = RandomizationTool.select_random_datetime(min_datetime=min_dt, max_datetime=max_dt) \
            .get_returned_value()

        logging.info("Change date-time")
        system.datetime.action_change(params=new_datetime).verify_result()

    with allure.step('Verify show date-time same as last log timestamp'):
        ClockTools.verify_show_and_log_times(system)


def _set_new_date_time(system, new_datetime):
    system.datetime.action_change(params=new_datetime).verify_result()
    ClockTools.verify_show_and_log_times(system)
