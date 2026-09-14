import logging
import pytest
import random
import re
from pathlib import Path

from retry import retry

from ngts.nvos_constants.constants_nvos import DatabaseConst, HealthConsts, PlatformConsts
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.helpers.alarms.system_alarm_helpers import get_all_alarms_once, validate_all_alarms, validate_all_alarms_once
from ngts.tests_nvos.helpers.alarms.system_alarms_gnmi_helpers import gnmi_client_for_alarms, validate_gnmi_alarms
from ngts.tests_nvos.helpers.alarms.system_alarms_otel_helpers import validate_otel_alarms
from ngts.tests_nvos.system.telemetry.otel.otel_collector import OtelCollector
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.Simulator import HWSimulator
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.ngts_types import EnginesT, DevicesT
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tests_nvos.constants import MINUTE
from . import helpers as platform_helpers
from ngts.nvos_constants.constants_nvos import DatabaseConst, HealthConsts, PlatformConsts, AlarmsConsts
from ngts.tests_nvos.platform.helpers import validate_health_issues

cumulus_owner = "hiept"


logger = logging.getLogger(__name__)

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
def test_show_platform_environment_voltage(engines: EnginesT, devices: DevicesT):
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
                if platform_helpers.is_sensor_for_absent_psu(sensor, available_psu_list):
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
            if platform_helpers.is_sensor_for_absent_psu(sensor, available_psu_list):
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
def test_show_voltage_bad_flow(engines: EnginesT, devices: DevicesT):
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
def test_database_platform_environment_voltage(engines: EnginesT, devices: DevicesT):
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

    with allure.step("get all VOLTAGE_INFO tables in STATE_DB"):
        database_output = Tools.DatabaseTool.sonic_db_cli_get_keys(engine=engines.dut, asic="",
                                                                   db_name=DatabaseConst.STATE_DB_NAME,
                                                                   grep_str="VOLTAGE_INFO").splitlines()

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


def get_random_sensor_max_min(sensors_dic: dict[str, dict[str, str]]) -> str:
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


def check_voltage_in_range(sensor: str, sensor_output: dict[str, str]) -> str:
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

def _get_psu_voltage_sensor_input_path(sensor):
    """Get the input path for a PSU sensor."""
    match = re.search(r"PSU-(\d+)-.*", sensor)
    psu_number = match.group(1)
    sensor_dir = PlatformConsts.POWER_FILES_PATH
    return f"{sensor_dir}/psu{psu_number}_volt_out2"


def _pick_sensor(engine: EnginesT, devices: DevicesT) -> tuple[str, dict[str, str], str]:
    """Pick a random ok-state voltage sensor with max/min thresholds, including PSU sensors."""
    platform = Platform()
    voltage_output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
        platform.environment.voltage.show()).verify_result()
    available_psu_list = platform.environment.get_available_psus() if devices.dut.psu_list else []
    eligible_sensors = platform_helpers.filter_eligible_sensors(
        devices.dut.voltage_sensors, voltage_output, available_psu_list,
        required_keys=('max', 'min', 'actual'))
    assert eligible_sensors, "No voltage sensors with max/min/actual found for simulation"
    sensor = random.choice(eligible_sensors)
    sensor_data = voltage_output[sensor]
    if sensor.startswith('PSU-'):
        sensor_input_path = _get_psu_voltage_sensor_input_path(sensor)
    else:
        sensor_dir = HWSimulator.find_sensor_dir(engine, VOLTAGE_BASE_PATH, sensor)
        sensor_input_path = f"{sensor_dir}/input"
    logger.info(f"Selected sensor: {sensor}, path: {sensor_input_path}, "
                f"actual: {sensor_data['actual']}, max: {sensor_data['max']}, min: {sensor_data['min']}")
    return sensor, sensor_data, sensor_input_path


# @retry(AssertionError, tries=6, delay=10)
# def _validate_voltage_health_issues(system, sensor_name: str, *, expected_present: bool):
#     """Validate that a sensor appears (or not) in the health issues dict."""
#     health_output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
#         system.health.show()).get_returned_value()
#     health_issues = health_output.get(HealthConsts.ISSUES, {})
#     target = normalize_sensor_name(sensor_name)
#     matched_key = None
#     for key in health_issues:
#         if normalize_sensor_name(key) == target or ('psu' in normalize_sensor_name(key) and normalize_sensor_name(key) in target):
#             matched_key = key
#             break

#     if expected_present:
#         assert matched_key is not None, (
#             f"Expected sensor '{sensor_name}' in health issues, "
#             f"but got: {list(health_issues.keys())}"
#         )
#     else:
#         assert matched_key is None, (
#             f"Expected sensor '{sensor_name}' NOT in health issues, "
#             f"but it is still present as '{matched_key}': {health_issues.get(matched_key)}"
#         )


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
def test_simulate_voltage_faults(engines: EnginesT, devices: DevicesT, otel_alarms_collector: OtelCollector,
                                 tmp_path: Path, random_api):
    """
    @summary: Inject various bad voltage values into a sensor and verify the system
              detects each fault and recovers after restore. Validates sensor state,
              overall health status, health issues presence and system alarms.

    Steps:
    1. Validate system health is OK
    2. Pick a random voltage sensor with max/min thresholds, including PSU sensors
    3. For each fault scenario (high, low, negative, gibberish):
       a. Inject the bad value into the sensor
       b. Validate sensor state is 'failed', health is 'Not OK', sensor in health issues,
          and a voltage-out-of-range alarm is raised (NVUE, gNMI and OTEL) for the sensor,
          or for the generic voltage sensor origin on PMIC sensors
       c. For gibberish, validate the invalid voltage value is logged
       d. Restore original symlink
       e. Validate sensor state is 'ok', health is 'OK', sensor not in health issues,
          and no voltage-out-of-range or hw-fault alarm is present (NVUE, gNMI and OTEL)
    """
    system = System()
    voltage_show = Platform().environment.voltage.show
    gnmiclient = gnmi_client_for_alarms(engines)
    with allure.step("Pick sensor and validate initial health"):
        sensor_name, sensor_data, sensor_input_path = _pick_sensor(engines.dut, devices)
        alarm_origin = AlarmsConsts.VOLTAGE_SENSOR if 'PMIC' in sensor_name else sensor_name
        platform_helpers.validate_health(system, HealthConsts.OK)

    with allure.step("Simulate voltage faults"):
        for fault_name, compute_value in FAULT_SCENARIOS:
            fault_value = compute_value(sensor_data)

            with allure.independent_step(f"Simulate voltage fault: {fault_name} (value={fault_value})"):
                with HWSimulator.simulate_sensor(engines.dut, sensor_input_path, fault_value, HEALTH_STABILIZE_DELAY):
                    platform_helpers.validate_sensor_state(voltage_show, sensor_name, 'failed')
                    platform_helpers.validate_health(system, HealthConsts.NOT_OK)
                    validate_health_issues(system, sensor_name, expected_present=True)
                    validate_all_alarms_once(system, gnmiclient, otel_alarms_collector, tmp_path, alarm_origin_name=alarm_origin, alarm_types=AlarmsConsts.VOLTAGE_OUT_OF_RANGE, expected_present=True)
                    if fault_name == 'gibberish':
                        platform_helpers.validate_invalid_voltage_value_logged(system, engines.dut)

                platform_helpers.validate_sensor_state(voltage_show, sensor_name, 'ok')
                platform_helpers.validate_health(system, HealthConsts.OK)
                validate_health_issues(system, sensor_name, expected_present=False)
                validate_all_alarms_once(system, gnmiclient, otel_alarms_collector, tmp_path, alarm_origin_name=alarm_origin, alarm_types=[AlarmsConsts.VOLTAGE_OUT_OF_RANGE, AlarmsConsts.HW_FAULT], expected_present=False)
