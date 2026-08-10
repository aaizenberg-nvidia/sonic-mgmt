import logging
import random
import time
from contextlib import contextmanager
import pytest

from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_constants.constants_nvos import FansConsts
from ngts.nvos_constants.constants_nvos import OutputFormat
from ngts.nvos_constants.constants_nvos import PlatformConsts, HealthConsts, ActionConsts, SystemConsts
from ngts.nvos_tools.Devices.BaseDevice import BaseDevice
from ngts.nvos_tools.infra.FilesTool import TempFileOnEngine
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.test_system_health import verify_health_status_and_led
from ngts.tests_nvos.helpers import redmine_helpers
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.Devices.IbDevice import JulietSwitch


cumulus_owner = "hiept"


logger = logging.getLogger()


@pytest.mark.platform
@pytest.mark.cumulus
@pytest.mark.simx
@pytest.mark.nvos_ci
@pytest.mark.nvl_ci
@pytest.mark.skynet
@pytest.mark.air
@pytest.mark.air_ci
@pytest.mark.air_sanity
def test_show_platform_environment(engines, devices, random_api, output_format):
    """
    Show platform environment test
    """

    with allure.step("Create System object"):
        platform = Platform()

    with allure.step("Get output"):
        raw_output = platform.environment.show(output_format=output_format)

    with allure.step("Validate field names (titles)"):
        output_field_names = OutputParsingTool.parse_show_output_to_field_names(raw_output, output_format
                                                                                ).get_returned_value()
        ValidationTool.validate_set_equal(output_field_names, ['type', 'state']).verify_result()

    with allure.step("Validate all environment items are present"):
        temperature_sensor_list = get_available_temperature_sensor_list(devices.dut)
        output = OutputParsingTool.parse_show_output_to_dict(raw_output, output_format).get_returned_value()
        ValidationTool.validate_set_equal(output.keys(),
                                          devices.dut.psu_fan_list + devices.dut.fan_list + devices.dut.psu_list +
                                          temperature_sensor_list + devices.dut.led_list +
                                          devices.dut.voltage_sensors + devices.dut.list_of_leakages).verify_result()


@pytest.mark.platform
@pytest.mark.cumulus
@pytest.mark.simx
@pytest.mark.nvl_ci
@pytest.mark.skynet
def test_show_platform_environment_fan(engines, devices, random_api, output_format, skip_for_fanless_setup):
    """
    Show platform environment fan test
    """

    with allure.step("Create Platform object"):
        platform = Platform()

    with allure.step("Execute 'show platform environment fan' and validate field names and fan list"):
        raw_output = platform.environment.fan.show(output_format=output_format)
        field_names = OutputParsingTool.parse_show_output_to_field_names(
            raw_output, output_format=output_format, field_name_dict=devices.dut.fan_prop_auto).get_returned_value()
        ValidationTool.validate_set_equal(field_names, devices.dut.platform_environment_fan_values.keys()
                                          ).verify_result()
        output = OutputParsingTool.parse_show_output_to_dict(
            raw_output, output_format=output_format, field_name_dict=devices.dut.fan_prop_auto).get_returned_value()
        actual_fan_list = output.keys()
        ValidationTool.validate_set_equal(actual_fan_list, devices.dut.fan_list + devices.dut.psu_fan_list
                                          ).verify_result()

    with allure.step("Assert all fans have the same direction"):
        directions = {fan["direction"] for fan in output.values() if fan["direction"] != "N/A"}
        assert len(directions) == 1, f"Not all fans show the same direction: {output}"

    with allure.step("Checking properties of random fan"):
        random_fan = random.choice(devices.dut.fan_list)
        _test_specific_fan(random_fan, output_format, devices.dut.platform_environment_fan_values, output, platform)

    with allure.step("Checking properties of PSU fans"):
        for fan in devices.dut.psu_fan_list:
            if output.get(fan).get("state") == "absent":
                _test_absent_fan(fan, output_format, devices.dut.platform_environment_absent_fan_values, output, platform)
                continue
            _test_specific_fan(fan, output_format, devices.dut.platform_environment_fan_values, output, platform)


def _test_absent_fan(fan, output_format, expected, output, platform):
    logger.info(f"Testing properties of absent fan {fan}")
    with allure.step("Checking output of 'show platform environment fan'"):
        ValidationTool.validate_output_of_show(output[fan], expected).verify_result()
    with allure.step("Checking output of 'show platform environment fan <fan-id>'"):
        fan_output = OutputParsingTool.parse_show_output_to_dict(
            platform.environment.fan.show(fan, output_format=output_format),
            output_format=output_format).get_returned_value()
        ValidationTool.validate_output_of_show(fan_output, expected).verify_result()


def _test_specific_fan(fan, output_format, expected, output, platform):
    logger.info(f"Testing properties of {fan}")
    with allure.step("Checking output of 'show platform environment fan'"):
        ValidationTool.validate_output_of_show(output[fan], expected).verify_result()
    with allure.step("Checking output of 'show platform environment fan <fan-id>'"):
        fan_output = OutputParsingTool.parse_show_output_to_dict(
            platform.environment.fan.show(fan, output_format=output_format),
            output_format=output_format).get_returned_value()
        ValidationTool.validate_output_of_show(fan_output, expected).verify_result()
        assert int(fan_output["min-speed"]) < int(fan_output["current-speed"]) < int(fan_output["max-speed"])


@pytest.mark.platform
@pytest.mark.cumulus
@pytest.mark.simx
@pytest.mark.nvl_ci
@pytest.mark.skynet
@pytest.mark.air
def test_show_platform_environment_led(engines, devices, random_api):
    """
    Show platform environment led test
    """

    with allure.step("Create System object"):
        platform = Platform()

    with allure.step("Execute show platform environment led and make sure all the components exist"):
        output = _verify_output(platform, "led", devices.dut.led_list)

    with allure.step("Check that all required properties for each led"):
        logging.info("Check that all required properties for each led")
        for led, led_prop in output.items():
            _verify_led_prop(led, led_prop)

    with allure.step("Check output of a specific Led"):
        led_to_check = list(output.keys())[0]
        output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
            platform.environment.led.show(op_param=led_to_check)).verify_result()
        _verify_led_prop(led_to_check, output)


@pytest.mark.platform
@pytest.mark.simx
@pytest.mark.skynet
def test_set_platform_environment_led(engines, devices, random_api):
    """
    Set platform environment led test

    Test flow:
    1. Check all leds are green by default exclude UID
    2. Negative testing, try to turn off leds, which we can't, check value didn't change
    3. Turn-on UID led and check led is green
    4. Unset it and check it returned to default values
    """

    with allure.step("Create System object"):
        platform = Platform()

    with allure.step("Execute show platform environment led and make sure all the components exist"):
        output = _verify_output(platform, PlatformConsts.ENV_LED, devices.dut.led_list)

    with allure.step("Check for missing PSUs (affects the PSU_STATUS led due to redmine #4272431)"):
        missing_psus = platform.environment.get_available_psus(invert=True) if devices.dut.psu_list else []

    with allure.step("Check that all leds are green and UID off by default"):
        logging.info("Check that all leds are green and UID off by default")
        for led, led_prop in output.items():
            _verify_led_color(led, led_prop, missing_psus)

    with allure.step("Negative set off to FAN or PSU, expect fail"):
        logging.info("Negative set off to FAN or PSU, expect fail")
        for led, led_prop in output.items():
            if led == PlatformConsts.ENV_UID:
                continue
            platform.environment.led.action_deprecated(action='turn-{type}'.format(type=PlatformConsts.ENV_LED_TURN_OFF),
                                                       suffix=led).verify_result(False)

    with allure.step("Check that all leds are green and UID off by default"):
        logging.info("Check that all leds are green and UID off by default")
        for led, led_prop in output.items():
            _verify_led_color(led, led_prop, missing_psus)

    with allure.step("Change UID state led to on"):
        logging.info("Check UID state led to on")
        platform.environment.led.action_deprecated(action='turn-{type}'.format(type=PlatformConsts.ENV_LED_TURN_ON),
                                                   suffix=PlatformConsts.ENV_UID)
        output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
            platform.environment.led.show()).verify_result()
        Tools.ValidationTool.compare_values(output[PlatformConsts.ENV_UID][PlatformConsts.ENV_LED_COLOR_LABEL],
                                            PlatformConsts.ENV_LED_COLOR_BLUE, True).verify_result()

    with allure.step("Change UID state led to off"):
        logging.info("Change UID state led to off")
        platform.environment.led.action_deprecated(action='turn-{type}'.format(type=PlatformConsts.ENV_LED_TURN_OFF),
                                                   suffix=PlatformConsts.ENV_UID)
        output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
            platform.environment.led.show()).verify_result()
        Tools.ValidationTool.compare_values(output[PlatformConsts.ENV_UID][PlatformConsts.ENV_LED_COLOR_LABEL],
                                            PlatformConsts.ENV_LED_TURN_OFF, True).verify_result()

    with allure.step("Check that all leds are green and UID off after unset"):
        logging.info("Check that all leds are green and UID off after unset")
        for led, led_prop in output.items():
            _verify_led_color(led, led_prop, missing_psus)


@pytest.mark.platform
@pytest.mark.cumulus
@pytest.mark.simx
@pytest.mark.nvl_ci
@pytest.mark.skynet
def test_show_platform_environment_psu(engines, devices, random_api):
    """
    Show platform environment psu test
    """
    if not devices.dut.psu_list:
        pytest.skip("Device has no PSUs")

    with allure.step("Create System object"):
        platform = Platform()

    with allure.step("Execute show platform environment psu and make sure all the components exist"):
        output = _verify_output(platform, "psu", devices.dut.psu_list)

    with allure.step("Check that all required properties for each psu"):
        logging.info("Check that all required properties for each psu")
        for psu, psu_prop in output.items():
            _verify_psu_prop(psu, psu_prop, devices)

    with allure.step("Check output of a specific PSU"):
        psu_to_check = list(output.keys())[0]
        output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
            platform.environment.psu.show(op_param=psu_to_check)).verify_result()
        _verify_psu_prop(psu_to_check, output, devices)


@pytest.mark.platform
@pytest.mark.cumulus
@pytest.mark.simx
@pytest.mark.skynet
def test_show_platform_environment_temperature(engines, devices, random_api):
    """
    Show platform environment temperature test

    Test flow:
    1. Select a FAN to test
    2. Validate every temperature sensor component in nv show platform environment temperature command
    3. Validate every temperature sensor component properties
    4. Validate every temperature sensor temp in the valid range
    5. Validate CPU sensors temp in specified range from mean by some tolerance
    6. Validate ASIC sensors temp in specified range from mean by some tolerance
    7. Validate PSU sensors temp in specified range from mean by some tolerance

    """

    with allure.step("Create System object"):
        platform = Platform()

    temperature_sensor_list = get_available_temperature_sensor_list(devices.dut)

    with allure.step("Execute show platform environment temperature and make sure all the components exist"):
        output = _verify_output(platform, "temperature", temperature_sensor_list)

    with allure.step("make sure all temperature sensors are present in the output"):
        with allure.step(
                "Verify for every sensor in sensors_dict[TEMPERATURE], it exist in nv show platform temperature"):
            diff_sensors = [x for x in temperature_sensor_list if x not in output.keys()]
            err_mes = '' if not len(diff_sensors) else 'the next sensors are not in the output: {}'.format(diff_sensors)
        with allure.step("Verify no extra sensors are found in nv show platform environment temperature"):
            diff_sensors = [x for x in output.keys() if x not in temperature_sensor_list]
            err_mes += '' if not len(diff_sensors) else 'there are extra sensors in the output: {}'.format(diff_sensors)

    assert not err_mes, err_mes

    with allure.step("Check that all required properties for each temperature"):
        logging.info("Check that all required properties for each temperature")
        for temp, temp_prop in output.items():
            if temp_prop.get("state") == 'absent':
                continue
            _verify_temp_prop(temp, temp_prop)

    with allure.step("Check that all sensors in required range"):
        logging.info("Check that all sensors in required range")
        for temp, temp_prop in output.items():
            _verify_temp_in_range(temp, temp_prop, PlatformConsts.ENV_TEMP_MIN,
                                  PlatformConsts.ENV_TEMP_MAX)

    verify_sensor_group_by_tolerance(output, PlatformConsts.ENV_CPU)

    if not redmine_helpers.is_bug_active(5070648):  # RM 5070648: ASIC temp tolerance flap
        verify_sensor_group_by_tolerance(output, PlatformConsts.FW_ASIC)
    if devices.dut.psu_list:
        verify_sensor_group_by_tolerance(output, PlatformConsts.ENV_PSU.upper())


def get_available_temperature_sensor_list(device: BaseDevice):
    with allure.step('Get temperature-sensor list'):
        try:
            if device.psu_list:
                missing_psus = [psu.replace('PSU', 'PSU-') for psu in Platform().environment.get_available_psus(invert=True)]
            else:
                missing_psus = []
            output = [sensor for sensor in device.temperature_sensors if not any(psu in sensor for psu in missing_psus)]
            logger.info("Available temperature sensors: " + str(output))
            return output
        except AssertionError as e:
            if "'psu' is not one of" in str(e):
                # then it's a Juliet or similar switch
                return device.temperature_sensors
            else:
                raise


@pytest.mark.platform
@pytest.mark.simx
@pytest.mark.disable_loganalyzer
def test_platform_environment_events_performance(engines, devices, skip_for_fanless_setup):
    """
     Simulate an event and verify that show event is not flooded with multiple entries

     Test flow:
     1. Validate System Health is OK
     2. Clear System events
     3. Assign default FAN direction for this system
     4. Assign wrong FAN direction for this system
     5. Change FAN direction to wrong direction and verify via CLI
     6. Wait for 2:10 minutes and verify that show event is not flooded with multiple entries for the same fan
     7. Change FAN direction to system default
     """
    TestToolkit.tested_api = ApiType.NVUE
    platform = Platform()
    system = System()
    fan_to_check = devices.dut.fan_list[2]
    op_param_val = " | grep '" + str(FansConsts.FAN_DIRECTION_MISMATCH_ERR) + "'"
    with allure.step('Check is Juliet Device'):
        if not isinstance(devices.dut, JulietSwitch):
            with allure.step('Validate System health status should be {}'.format(HealthConsts.OK)):
                output = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).verify_result()
                assert output['status'] == HealthConsts.OK, 'System health status is {} instead of {}'.format(
                    output['status'], HealthConsts.OK)

    with allure.step('Clear system events'):
        system.events.action_clear().verify_result()

    with allure.step("Assign default FAN direction as per this System"):
        output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
            platform.environment.fan.show(op_param=fan_to_check)).verify_result()
        def_dir = output['direction']

    with allure.step("Assign wrong direction (opposite to default) as per this System"):
        if def_dir == FansConsts.FORWARD_DIRECTION:
            wrong_dir = FansConsts.BACKWARD_DIRECTION
        else:
            wrong_dir = FansConsts.FORWARD_DIRECTION

    try:
        with allure.step("Change direction of {} to wrong dir({}) and verify".format(fan_to_check, wrong_dir)):
            _set_platform_environment_fan_direction(engines, devices, platform, fan_to_check, def_dir, wrong_dir)

        with allure.step("Wait for the system to be able to generate more events"):
            time.sleep(130)

        with allure.step('Run show system events command & validate there is 1 FAN direction issue per FAN'):
            output = OutputParsingTool.parse_json_str_to_dictionary(system.events.show_events_last_recent_entries(
                SystemConsts.SYSTEM_LAST_EVENT, '10000')).get_returned_value()
            fan_error_set = set()
            for events_no in output:
                err_found = False
                output_err_msg = str(output[events_no])
                if FansConsts.FAN_DIRECTION_MISMATCH_ERR in output_err_msg:
                    err_found = True
                elif FansConsts.FAN_DIRECTION_MISMATCH_ERR_CROC in output_err_msg:
                    # System is crocodile
                    err_found = True
                    op_param_val = " | grep '" + str(FansConsts.FAN_DIRECTION_MISMATCH_ERR_CROC) + "'"
                if err_found:
                    fan_err_msg = output[events_no]["text"]
                    assert (fan_err_msg not in fan_error_set), \
                        'Fan mismatch event occurred more times for FAN:{}'.format(fan_to_check)
                    fan_error_set.add(fan_err_msg)
                    logger.info("Fan direction mismatch Event captured for : {}".format(fan_to_check))

        with allure.step("Validate Fan direction error appears in system log but is not flooded"):
            no_of_errors_1 = len(system.log.file.show_log(param=op_param_val).splitlines())
            assert no_of_errors_1 > 0, 'Fan direction error does not appear in log'
            ret, no_of_errors_2 = _check_fan_error_logs(system, op_param_val, no_of_errors_1, 120)
            if ret is False:
                logger.info("Fan direction errors are being repeated in logs, retry to check for flooding")
                ret, _ = _check_fan_error_logs(system, op_param_val, no_of_errors_2, 60)
                assert ret, "Fan direction errors are being flooded in logs"

    finally:
        with allure.step("Change Fan direction of {} to default({}) and verify".format(fan_to_check, def_dir)):
            _set_platform_environment_fan_direction(engines, devices, platform, fan_to_check, def_dir, def_dir)


@pytest.mark.cumulus
@pytest.mark.platform
@pytest.mark.simx
@pytest.mark.disable_loganalyzer
def test_platform_environment_fan_direction_mismatch(engines, devices, skip_for_fanless_setup):
    """
    Set FAN direction test

    Test flow:
    1. Select a FAN to test
    2. Validate System health should be OK
    3. Validate there should not be any FAN direction related issues
    4. Assign default FAN direction for this system
    5. Assign wrong FAN direction for this system
    6. Change FAN direction to wrong direction and verify via CLI
    7. Validate System health should be NOT OK
    8. Validate FAN related health issues in System health
    9. Change FAN direction to system default abd verify via CLI
    10. Validate System health should be OK
    11. Validate there should not be any FAN direction related issues
    """
    TestToolkit.tested_api = ApiType.NVUE
    with allure.step('Validate Fan direction mismatch feature enabled'):
        _verify_fan_direction_mismatch_behaviour(engines, devices, True)


def _verify_fan_direction_mismatch_behaviour(engines, devices, feature_enable):
    platform = Platform()
    system = System()
    dir_changed = False
    health_changed_to_not_ok = False
    if feature_enable:
        state = FansConsts.STATE_NOT_OK
        should_str = 'be'
    else:
        state = FansConsts.STATE_OK
        should_str = 'not be'

    try:
        with allure.step('Select FAN to test'):
            # Don't choose FAN1/1 or FAN1/2 because NVOS considers fan1_direction as the "ground truth" that other fans
            # are compared against. This will be changed in the future.
            choose_from = devices.dut.fan_list[2:]
            fan_to_check = random.choice(choose_from)
            output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
                platform.environment.fan.show(op_param=fan_to_check)).verify_result()
            def_dir = output['direction']

        with allure.step('Check is Juliet Device'):
            if not isinstance(devices.dut, JulietSwitch):
                with allure.step('Validate System health status should be {}'.format(HealthConsts.OK)):
                    output = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).verify_result()
                    health_status = output['status']
                    assert health_status == HealthConsts.OK, 'System health status is {} instead of {}'.format(
                        health_status, HealthConsts.OK)

        with allure.step("Validate there should not be any Fan direction Health Issues"):
            output_dict = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).verify_result()
            health_issues = output_dict['issues']
            health_issues_keys = health_issues.keys()
            contains_fan = any("fan" in key.lower() for key in health_issues_keys)
            assert not contains_fan, f'Unexpected fan related Health Issues:\n{health_issues}'

        with allure.step("Assign default FAN direction as per this System"):
            output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
                platform.environment.fan.show(op_param=fan_to_check)).verify_result()
            def_dir = output['direction']

        with allure.step("Assign wrong direction (opposite to default) as per this System"):
            if def_dir == FansConsts.FORWARD_DIRECTION:
                wrong_dir = FansConsts.BACKWARD_DIRECTION
            else:
                wrong_dir = FansConsts.FORWARD_DIRECTION

        with allure.step("Get the latest event"):
            last_event = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.events.show_events_last_recent_entries(SystemConsts.SYSTEM_LAST_EVENT)).\
                get_returned_value()
            latest_event_id = int(list(last_event)[0])

        with allure.step("Change direction of {} to wrong dir({}) and verify".format(fan_to_check, wrong_dir)):
            _set_platform_environment_fan_direction(engines, devices, platform, fan_to_check, def_dir, wrong_dir)
            dir_changed = True

        with allure.step('Validate System health status should be {}'.format(state)):
            output = system.health.show(output_format=OutputFormat.json)
            output_dict = Tools.OutputParsingTool.parse_json_str_to_dictionary(output).verify_result()
            health_status = output_dict['status']
            assert health_status == state, 'System health status is {} instead of {}'.format(health_status, state)

        if state == FansConsts.STATE_NOT_OK:
            with allure.step("Validate system event regarding Health status: Health status is not ok"):
                events = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.events.show_events_last_recent_entries(SystemConsts.SYSTEM_LAST_EVENT, '')).\
                    get_returned_value()
                newer_events = [events[event]['text'] for event in list(events) if int(event) > int(latest_event_id)]
                assert PlatformConsts.HEALTH_STATUS_NOT_OK_EVENT in newer_events, \
                    "Health status not ok event not found in events"
                health_changed_to_not_ok = True

        with allure.step("Validate Issues should {} seen in System Health Report".format(should_str)):
            health_issues = output_dict['issues']
            if feature_enable:
                assert fan_to_check in health_issues.keys(), \
                    f'Expected to find issue with {fan_to_check} but issues are:\n{health_issues}'
            else:
                assert not health_issues, f'Unexpected Health Issues:\n{health_issues}'

    finally:
        if dir_changed:
            with allure.step("Change Fan direction of {} to default({}) and verify".format(fan_to_check, def_dir)):
                _set_platform_environment_fan_direction(engines, devices, platform, fan_to_check, def_dir, def_dir)

        if health_changed_to_not_ok:
            with allure.step("Validate system event regarding clear Health status- Cleared: Health status is not ok"):
                events0 = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.events.show_events_last_recent_entries(SystemConsts.SYSTEM_LAST_EVENT, '')).\
                    get_returned_value()
                newer_events = [events0[event]['text'] for event in list(events0) if int(event) > int(latest_event_id)]
                assert "Cleared: " + PlatformConsts.HEALTH_STATUS_NOT_OK_EVENT in newer_events, \
                    "Clear health event not found in events"

        with allure.step("Validate Issues should not be seen in System Health Report"):
            output = system.health.show(output_format=OutputFormat.json)
            output_dict = Tools.OutputParsingTool.parse_json_str_to_dictionary(output).verify_result()
            health_issues = output_dict['issues']
            if feature_enable:
                assert fan_to_check not in health_issues.keys(), \
                    f'Expected not to find issue with {fan_to_check} but issues are:\n{health_issues}'
            else:
                assert not health_issues, f'Unexpected Health Issues:\n{health_issues}'

        with allure.step('Check is Juliet Device'):
            if not isinstance(devices.dut, JulietSwitch):
                with allure.step('Check System health status'):
                    output = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).verify_result()
                    health_status = output['status']
                    assert health_status == HealthConsts.OK, 'System health status is {} instead of {}'. \
                        format(health_status, HealthConsts.OK)

        with allure.step("Validate there should not be any Fan direction Health Issues"):
            output = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).verify_result()
            health_issues = output['issues']
            health_issues_keys = health_issues.keys()
            contains_fan = any("fan" in key.lower() for key in health_issues_keys)
            assert not contains_fan, f'Unexpected fan related Health Issues:\n{health_issues}'


def _set_platform_environment_fan_direction(engines, devices, platform, fan_to_check, def_dir, direction):
    if "PSU" in fan_to_check:
        fan_name = fan_to_check.replace("/", "_").lower()
    else:
        fan_name = fan_to_check.split('/')[0].lower()

    with allure.step("Set the direction of the fan {} to {} direction".format(fan_name, direction)):
        simulate_fan_direction(engines, devices, fan_name, direction)

    with allure.step("Check output of a Fan {}".format(fan_name)):
        output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
            platform.environment.fan.show(op_param=fan_to_check)).verify_result()
        ValidationTool.validate_set_equal(output.keys(), devices.dut.platform_environment_fan_values.keys())

    with allure.step("Check fan state and direction via CLI"):
        actual_direction = output['direction']
        state = output['state']
        assert actual_direction == direction, "Unexpected direction of fan: {} instead of {}". \
            format(actual_direction, direction)


def simulate_fan_direction(engines, devices, fan_name, direction):
    """
    @summary: Simulate the direction of the module's fan.
    @param engines : Engine object
    @param devices : devices object
    @param fan_name: the module with the fan to simulate its direction.
    @param direction: the simulated direction, can be FORWARD/BACKWARD_DIRECTION.
    """
    direction_integer = 1 if direction == FansConsts.FORWARD_DIRECTION else 0
    chmod_cmd = "sudo chmod 777 {}/{}_dir".format(devices.dut.fan_direction_dir, fan_name)
    ret_val = engines.dut.run_cmd(chmod_cmd)
    assert len(ret_val) == 0, "Chmod command for fan file failed"

    simulate_cmd = "sudo echo {} > {}/{}_dir".format(direction_integer, devices.dut.fan_direction_dir, fan_name)
    logger.info('simulate {} fan direction to be {}. from linux shell, run: {}'.
                format(fan_name, direction, simulate_cmd))
    ret_val = engines.dut.run_cmd(simulate_cmd)
    assert len(ret_val) == 0, "Write command for fan file failed"
    time.sleep(5)  # for the change to take affect.

    chmod_cmd = "sudo chmod 644  {}/{}_dir".format(devices.dut.fan_direction_dir, fan_name)
    ret_val = engines.dut.run_cmd(chmod_cmd)
    assert len(ret_val) == 0, "Chmod command for fan file failed"


@pytest.mark.platform
@pytest.mark.disable_loganalyzer
def test_platform_environment_fan_shared_led(engines, devices):
    """
    Only for systems with shared fan LED. Tests the LED turns red when any fan has an error. Flow:
    - Check that health is OK and shared fan led is green before starting the test
    - Choose 2 random fans
    - Simulate error in fan A and assert led is amber
    - Additionally simulate an error in fan B and assert led is amber
    - Remove error only in fan B and assert led is amber
    - Remove error in fan A and assert led is green
    """
    if FansConsts.FAN_STATUS_LED not in devices.dut.led_list:
        pytest.skip(f"Skipping test because DUT has no shared fan led")

    with allure.step("Validate initial health status and shared led"):
        verify_health_status_and_led(System(), HealthConsts.OK)
        assert_led_color(FansConsts.FAN_STATUS_LED, ok=True)

    with allure.step("Random choose 2"):
        fan_a, fan_b = RandomizationTool.select_random_values(devices.dut.fan_list, number_of_values_to_select=2,
                                                              ).get_returned_value()
    with allure.step(f"{fan_a=}, {fan_b=}"):
        pass  # Puts the chosen_fans list in the allure report

    with fan_error_context(fan_a, devices.dut, engines.dut):
        assert_led_color(FansConsts.FAN_STATUS_LED, ok=False)
        with fan_error_context(fan_b, devices.dut, engines.dut):
            assert_led_color(FansConsts.FAN_STATUS_LED, ok=False)
        assert_led_color(FansConsts.FAN_STATUS_LED, ok=False)

    assert_led_color(FansConsts.FAN_STATUS_LED, ok=True)


def assert_led_color(led: str, ok: bool):
    led_name = 'led ' + led
    expected_color = (HealthConsts.LED_OK_STATUS if ok else HealthConsts.LED_NOT_OK_STATUS)
    with allure.step(f"Checking that {led_name} is {expected_color}"):
        led_output = OutputParsingTool.parse_json_str_to_dictionary(Platform().environment.show(led_name)
                                                                    ).get_returned_value()
        assert led_output[PlatformConsts.ENV_LED_COLOR_LABEL] == expected_color


@contextmanager
def fan_error_context(fan: str, device, engine):
    """
    with fan_error_context("FAN1/2", devices.dut, engines.dut):
        # this code runs while FAN1/2 status = failed
    # this code runs while FAN1/2 status = ok
    """
    platform = Platform()
    with allure.step(f"Simulating error in {fan}"):
        fan_file_number = device.fan_list.index(fan) + 1  # FAN1/1 -> 1, FAN1/2 -> 2, FAN2/1 -> 3, ...
        fan_fault_file = FansConsts.FAN_FAULT_FILE.format(fan_file_number)
        logger.info(f"{fan_fault_file=}. Getting the link target:")
        fan_fault_original_target = engine.run_cmd('readlink -f ' + fan_fault_file)
        with TempFileOnEngine(engine) as tmp_file:
            tmp_file.write('1')
            cmd_output = engine.run_cmd(f"sudo ln -sf {tmp_file.path} {fan_fault_file}")
            if cmd_output:
                raise Exception("Test failure. See command and output above.")
            time.sleep(10)
            try:
                fan_output = platform.environment.fan.show(fan)
                assert (OutputParsingTool.parse_json_str_to_dictionary(fan_output).get_returned_value()
                        [PlatformConsts.ENV_TEMP_STATE_PROP] == PlatformConsts.ENV_TEMP_STATE_FAILED), \
                    "Test error: could not simulate fan failure"
                yield
            finally:
                cmd_output = engine.run_cmd(f"sudo ln -sf {fan_fault_original_target} {fan_fault_file}")
                if cmd_output:
                    raise Exception("Test failure. See command and output above.")
                time.sleep(10)
                fan_output = platform.environment.fan.show(fan)
                assert (OutputParsingTool.parse_json_str_to_dictionary(fan_output).get_returned_value()
                        [PlatformConsts.ENV_TEMP_STATE_PROP] == PlatformConsts.ENV_TEMP_STATE_OK), \
                    "Test error: fan state did not go back to 'ok'"


def _verify_temp_prop(temp, temp_prop):
    logging.info("temp {}".format(temp))
    list_of_keys = temp_prop.keys()
    assert "state" in list_of_keys and "current" in list_of_keys, "state/current can't be found"

    if "max" in list_of_keys:
        max_temp = temp_prop["max"]
        assert _get_float(max_temp) or "N/A" in max_temp, "the max temperature value is invalid"

    if "crit" in list_of_keys:
        crit_value = temp_prop["crit"]
        assert _get_float(crit_value) or "N/A" in crit_value, "the critical temperature value is invalid"


def _verify_temp_in_range(temp, temp_prop, min_temp, max_temp):
    curr_temp = temp_prop[PlatformConsts.ENV_TEMP_CURR_PROP]
    assert temp_prop['state'] == PlatformConsts.ENV_TEMP_STATE_OK, 'sensor state is {} instead of {}'.format(
        temp_prop['state'], PlatformConsts.ENV_TEMP_STATE_OK)
    assert min_temp <= _get_float(curr_temp) <= max_temp, f"{temp} temperature {curr_temp} is not within" \
        f" the valid range ({min_temp} - {max_temp})."


def verify_sensor_group_by_tolerance(output, category):
    with allure.step("Check that {} temps are within the specified range from mean by tolerance of {}%"
                     .format(category, PlatformConsts.ENV_TEMP_TOLERANCE)):
        logging.info("Check that {} temps are within the specified range from mean by tolerance of {}%"
                     .format(category, PlatformConsts.ENV_TEMP_TOLERANCE))
    sensors = {temp: float(temp_prop[PlatformConsts.ENV_TEMP_CURR_PROP]) for temp, temp_prop in output.items()
               if temp.startswith(category) and temp_prop.get("state") != 'absent'}
    assert sensors, f'Output is missing sensor category "{category}"'
    sensor_mean_temp = sum(sensors.values()) / len(sensors)

    for sensor, sensor_temp in sensors.items():
        _verify_temp_by_tolerance(sensor, sensor_temp, sensor_mean_temp, PlatformConsts.ENV_TEMP_TOLERANCE,
                                  category)


def _verify_temp_by_tolerance(temp, curr_temp, mean_temp, p, category):
    min_temp = mean_temp * (1 - p / 100)
    max_temp = mean_temp * (1 + p / 100)
    assert min_temp <= curr_temp <= max_temp, \
        f"{category}: {temp} temperature {curr_temp} is not within the tolerated range from mean ({min_temp} - {max_temp})."


def _verify_output(platform, comp_name, req_fields):
    logging.info("Required comp: " + str(req_fields))
    with allure.step("Verify text output"):
        logging.info("Verify text output")
        output = platform.environment.show(comp_name, output_format=OutputFormat.auto)
        missing_components = [comp for comp in req_fields if comp not in output]
        assert not missing_components, f"The following fields were not found in the output: {missing_components}"

    with allure.step("Verify json output"):
        logging.info("Verify json output")
        output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
            platform.environment.show(comp_name)).verify_result()
        Tools.ValidationTool.verify_field_exist_in_json_output(output, req_fields).verify_result()

    return output


def _verify_led_prop(led, led_prop):
    logging.info("led {}".format(led))
    assert PlatformConsts.ENV_LED_COLOR_LABEL in led_prop.keys(), \
        PlatformConsts.ENV_LED_COLOR_LABEL + " not found for " + led
    assert led_prop[PlatformConsts.ENV_LED_COLOR_LABEL].lower() in PlatformConsts.ENV_LED_COLOR_OPTIONS, \
        led_prop[PlatformConsts.ENV_LED_COLOR_LABEL] + " is not a legal value"


def _verify_led_color(led, led_prop, missing_psus):
    logging.info("led {}".format(led))
    if led == PlatformConsts.ENV_UID:
        assert led_prop['color'] == PlatformConsts.ENV_LED_TURN_OFF, \
            PlatformConsts.ENV_LED_TURN_OFF + " not found for " + led
    elif led == PlatformConsts.ENV_PSU_STATUS_LED and missing_psus:  # redmine 4272431
        assert led_prop['color'] in [PlatformConsts.ENV_LED_COLOR_GREEN, PlatformConsts.ENV_LED_COLOR_AMBER], \
            f"{led} color is {led_prop['color']}"
    else:
        assert led_prop['color'] == PlatformConsts.ENV_LED_COLOR_GREEN, \
            PlatformConsts.ENV_LED_COLOR_GREEN + " not found for " + led


def _verify_psu_prop(psu, psu_prop, devices):
    logging.info("psu {}".format(psu))
    Tools.ValidationTool.verify_field_exist_in_json_output(psu_prop, devices.dut.platform_env_psu_prop).verify_result()


def _get_float(string):
    try:
        return float(string)
    except ValueError:
        return None


def _check_fan_error_logs(system, op_param_val, num_err_prev, seconds):
    num_err_next = len(system.log.file.show_log(param=op_param_val).splitlines())
    time.sleep(seconds)
    ret = False
    if num_err_next == num_err_prev:
        ret = True
    return ret, num_err_next
