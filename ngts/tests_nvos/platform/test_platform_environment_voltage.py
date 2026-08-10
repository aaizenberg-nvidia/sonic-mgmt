import logging
import re
import pytest
import random

from ngts.nvos_tools.infra.Simulator import HWSimulator
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.platform.helpers import (
    filter_eligible_sensors,
    is_sensor_for_absent_psu,
    validate_health,
    validate_health_issues,
    validate_invalid_voltage_value_logged,
    validate_sensor_state,
)
from ngts.nvos_constants.constants_nvos import DatabaseConst, HealthConsts, PlatformConsts


cumulus_owner = "hiept"


logger = logging.getLogger()

VOLTAGE_BASE_PATH = PlatformConsts.VOLTAGE_FILES_PATH
HEALTH_STABILIZE_DELAY = 10
VOLTAGE_MARGIN_FACTOR = 1.5


@pytest.mark.cumulus
@pytest.mark.platform
@pytest.mark.simx
@pytest.mark.skynet
@pytest.mark.nvos_chipsim_ci
@pytest.mark.air
@pytest.mark.timeout(2 * MINUTE, func_only=True)
def test_show_platform_environment_voltage(engines, devices):
    """
    Show platform environment test
    """
    with allure.step("Create System object"):
        platform = Platform()

    with (allure.step("Execute show platform environment and make sure all the components exist")):
        voltage_output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
            platform.environment.voltage.show()).verify_result()
        sensors = devices.dut.get_voltage_sensors(engines.dut)
        assert len(sensors) == len(voltage_output.keys()), ("test failed - expected sensors count = "
                                                            "{expected}, show command output = {output} \n "
                                                            "expected sensors list: {expected_list}").format(
            expected=len(sensors), output=len(voltage_output.keys()), expected_list=sensors)

    with allure.step("Check all details of all sensors are available in show platform environment voltage"):
        sensors_absent = []
        actual_volt_absent = []
        available_psu_list = platform.environment.get_available_psus() if devices.dut.psu_list else []
        for sensor in devices.dut.voltage_sensors:
            if voltage_output[sensor]['state'] != 'ok':
                if is_sensor_for_absent_psu(sensor, available_psu_list):
                    # Ignore absence of sensor for absent PSU
                    continue
                sensors_absent.append(sensor)
                continue
            if 'actual' not in voltage_output[sensor].keys():
                actual_volt_absent.append(sensor)

        assert len(sensors_absent) == 0 and len(actual_volt_absent) == 0, \
            'Absent sensors={}, Actual voltage missing={}'.format(sensors_absent, actual_volt_absent)

    with allure.step("Execute show platform environment voltage for every sensor and compare with aggregated show"):
        mismatch = False
        err = []
        for sensor in devices.dut.voltage_sensors:
            if is_sensor_for_absent_psu(sensor, available_psu_list):
                # Ignore absence of sensor for for absent PSU
                continue
            sensor_output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
                platform.environment.voltage.show(sensor)).verify_result()
            voltage_output_for_sensor = voltage_output[sensor].copy()
            # the actual voltage might fluctuate between the two `nv show` commands, so we keep 5% tolerance
            if 'actual' in sensor_output.keys():
                actual_voltage_low = float(voltage_output_for_sensor['actual']) * 0.95
                actual_voltage_high = float(voltage_output_for_sensor['actual']) * 1.05
                if not (actual_voltage_low <= float(sensor_output['actual']) <= actual_voltage_high):
                    mismatch = True
                    err.append("Actual voltage of sensor {} varied more than 5%, from {} to {}".
                               format(sensor, voltage_output_for_sensor['actual'], sensor_output['actual']))
                del sensor_output['actual']
                del voltage_output_for_sensor['actual']
            else:
                mismatch = True
                err.append('Actual voltage for sensor {} not present in sensor specific output'.format(sensor))
            if sensor_output != voltage_output_for_sensor:
                mismatch = True
                err.append('Min/max not matching for {}:{}'.format(sensor, voltage_output_for_sensor - sensor_output))

        assert not mismatch, "Mismatch between aggregated output and single sensor output:{}".format(err)

    with allure.step("Check voltage range for voltage sensors"):
        voltage_issue = False
        err_msg = []
        for sensor in devices.dut.voltage_sensors:
            err = check_voltage_in_range(sensor, voltage_output[sensor])
            if err != "":
                voltage_issue = True
                err_msg.append(err)
        assert not voltage_issue, 'Voltage of sensors out of range:{}'.format(err_msg)


@pytest.mark.cumulus
@pytest.mark.platform
@pytest.mark.skynet
@pytest.mark.simx
@pytest.mark.timeout(1 * MINUTE, func_only=True)
def test_show_voltage_bad_flow(engines, devices):
    """
    For Each Sensor we have DB (should be part of init flow)
    """
    with allure.step("Create System object"):
        platform = Platform()
        expected_msg = 'The requested item does not exist'
    with allure.step("Try nv show platform environment voltage <not_exist_sensor>"):
        output = platform.environment.voltage.show('not_sensor', should_succeed=False)
        assert expected_msg in output, "check the show command for not exist sensor, the expected message is {}, " \
                                       "the current output is {}".format(expected_msg, output)


@pytest.mark.platform
@pytest.mark.simx
@pytest.mark.skynet
@pytest.mark.timeout(1 * MINUTE, func_only=True)
def test_database_platform_environment_voltage(engines, devices):
    """
    For Each Sensor we have DB (should be part of init flow)
    """
    with allure.step("Create System object"):
        platform = Platform()

    with allure.step("get expected sensors"):
        sensors_list = platform.environment.voltage.get_sensors_list(engines.dut)
        logger.info("the expected sensors from switch's file system are: {}".format(sensors_list))

    with allure.step("get expected CLI voltage sensors"):
        cli_sensors_list = platform.environment.voltage.get_cli_sensors_list(engines.dut)

        logger.info("the sensors from switch's CLI are: {}".format(cli_sensors_list))

    with allure.step("get all the tabled with SENSOR in STATE_DB"):
        database_output = Tools.DatabaseTool.sonic_db_cli_get_keys(engine=engines.dut, asic="",
                                                                   db_name=DatabaseConst.STATE_DB_NAME,
                                                                   grep_str="VOLTAGE").splitlines()

    with allure.step("Check the Sensors output from CLI and db tables"):
        # Normalize sensor names by removing all non-alphanumeric characters
        def normalization(s):
            return re.sub(r'[^a-z0-9]', '', s.lower())

        def transform_sensor_for_database(sensor_name):
            """
            Transform filesystem sensor names to match database format.

            Args:
                sensor_name: Sensor name from filesystem (e.g., 'FAN+HSC1+VinDC+Volt+In')

            Returns:
                Transformed name ready for database comparison (e.g., 'VOLTAGE_INFO|FAN HSC1 Volt In')
            """
            # HSC database names omit VinDC but retain Volt.
            if any(hsc_name in sensor_name for hsc_name in ('FAN+HSC1', 'PDB+HSC')):
                return sensor_name.replace('+VinDC', '')

            # Handle other sensors: remove only Volt/Vol suffixes
            return sensor_name.replace('+Volt', '').replace('+Vol', '')

        def normalize_for_database_comparison(sensor_name):
            """
            Apply database transformation and normalization for comparison.
            """
            transformed = transform_sensor_for_database(sensor_name)
            return normalization("VOLTAGE_INFO|" + transformed)

        with allure.independent_step("Verify for every sensor in sensors_dict[VOLTAGE], it exist in nv show platform environment voltage"):
            ValidationTool.validate_equal_with_normalization(
                cli_sensors_list,
                devices.dut.sensors_dict["VOLTAGE"],
                normalization
            ).verify_result()

        with allure.independent_step("Verify for every sensor: VOLTAGE_INFO|<sensor_name> table exist in STATE_DB"):
            ValidationTool.validate_equal_with_normalization(
                database_output,
                sensors_list,
                normalization,
                normalize_for_database_comparison
            ).verify_result()


def get_random_sensor_max_min(sensors_dic):
    """
        get random sensor out of all the sensors with: ok state and have max, min values
    :param sensors_dic:
    :return:
    """
    sensors_list = []
    for item in sensors_dic.keys():
        if 'min' in sensors_dic[item].keys() and 'max' in sensors_dic[item].keys():
            sensors_list.append(item)
    assert sensors_list, "No sensors with Max and Min values"
    return random.choice(sensors_list)


def check_voltage_in_range(sensor, sensor_output):
    """
    :param sensor
    :param sensor_output:
    :return:
    """
    with allure.step("Verify the actual voltage is between min and max inclusive for {}".format(sensor)):
        if 'max' in sensor_output.keys():
            if float(sensor_output['actual']) > float(sensor_output['max']):
                return "Actual voltage {} more than max of {}".format(sensor_output['actual'], sensor_output['max'])
        if 'min' in sensor_output.keys():
            if float(sensor_output['actual']) < float(sensor_output['min']):
                return "Actual voltage {} less than min of {}".format(sensor_output['actual'], sensor_output['min'])
        return ""


# --- Voltage simulation helpers ---

def _pick_sensor(engine, devices):
    """Pick a random ok-state voltage sensor with max/min thresholds, including PSU sensors."""
    platform = Platform()
    voltage_output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
        platform.environment.voltage.show()).verify_result()
    available_psu_list = platform.environment.get_available_psus() if devices.dut.psu_list else []
    eligible_sensors = filter_eligible_sensors(
        devices.dut.voltage_sensors, voltage_output, available_psu_list,
        required_keys=('max', 'min', 'actual'))
    assert eligible_sensors, "No voltage sensors with max/min/actual found for simulation"
    sensor = random.choice(eligible_sensors)
    sensor_data = voltage_output[sensor]
    sensor_dir = HWSimulator.find_sensor_dir(engine, VOLTAGE_BASE_PATH, sensor)
    sensor_input_path = f"{sensor_dir}/input"
    logger.info(f"Selected sensor: {sensor}, path: {sensor_input_path}, "
                f"actual: {sensor_data['actual']}, max: {sensor_data['max']}, min: {sensor_data['min']}")
    return sensor, sensor_data, sensor_input_path


# --- Voltage simulation test ---

VOLTS_TO_MILLIVOLTS = 1000

FAULT_SCENARIOS = [
    ("high", lambda sensor_data: int(float(sensor_data['max']) * VOLTS_TO_MILLIVOLTS * VOLTAGE_MARGIN_FACTOR)),
    ("low", lambda sensor_data: max(-1, int(float(sensor_data['min']) * VOLTS_TO_MILLIVOLTS / VOLTAGE_MARGIN_FACTOR) - 1)),
    ("negative", lambda sensor_data: -100),
    ("gibberish", lambda sensor_data: 'abc'),
]


@pytest.mark.platform
@pytest.mark.disable_loganalyzer
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_simulate_voltage_faults(engines, devices):
    """
    @summary: Inject various bad voltage values into a sensor and verify the system
              detects each fault and recovers after restore. Validates sensor state,
              overall health status, and health issues presence.

    Steps:
    1. Validate system health is OK
    2. Pick a random voltage sensor with max/min thresholds
    3. For each fault scenario (high, low, negative, gibberish):
       a. Inject the bad value into the sensor
       b. Validate sensor state is 'failed', health is 'Not OK', sensor in health issues
       c. Restore original symlink
       d. Validate sensor state is 'ok', health is 'OK', sensor not in health issues
    """
    system = System()

    voltage_show = Platform().environment.voltage.show

    with allure.step("Pick sensor and validate initial health"):
        sensor_name, sensor_data, sensor_input_path = _pick_sensor(engines.dut, devices)
        validate_health(system, HealthConsts.OK)

    with allure.step("Simulate voltage faults"):
        for fault_name, compute_value in FAULT_SCENARIOS:
            fault_value = compute_value(sensor_data)

            with allure.independent_step(f"Simulate voltage fault: {fault_name} (value={fault_value})"):
                with HWSimulator.simulate_sensor(engines.dut, sensor_input_path, fault_value, HEALTH_STABILIZE_DELAY):
                    validate_sensor_state(voltage_show, sensor_name, 'failed')
                    validate_health(system, HealthConsts.NOT_OK)
                    validate_health_issues(system, sensor_name, expected_present=True)
                    if fault_name == 'gibberish':
                        validate_invalid_voltage_value_logged(system, engines.dut)

                validate_sensor_state(voltage_show, sensor_name, 'ok')
                validate_health(system, HealthConsts.OK)
                validate_health_issues(system, sensor_name, expected_present=False)
