import datetime
import json
import logging
import math
import os
import random
import re
import time

import allure
from retry import retry

from ngts.common.checkers import is_ver1_greater_or_equal_ver2
logger = logging.getLogger()

SENSOR_DATA = {"asic": {"temperature_file_name_rule": "asic",
                        "dev_parameters_name": "asic",
                        "total_number": 1},
               "cpu_core": {"temperature_file_name_rule": "cpu_core{}",
                            "dev_parameters_name": "(cpu_pack|cpu_core\\d+)",
                            "total_number": 2,
                            "start_index": 0},
               "cpu_pack": {"temperature_file_name_rule": "cpu_pack",
                            "dev_parameters_name": "(cpu_pack|cpu_core\\d+)",
                            "total_number": 1},
               'psu': {"temperature_file_name_rule": "psu{}_temp",
                       "dev_parameters_name": "sensor_amb",
                       "total_number": 2,
                       "start_index": 1},
               "voltmon": {"temperature_file_name_rule": "voltmon{}_temp_input",
                           "dev_parameters_name": "voltmon\\d+_temp",
                           "total_number": 1,
                           "start_index": 1},
               "module": {"temperature_file_name_rule": "module{}_temp_input",
                          "dev_parameters_name": "module\\d+",
                          "total_number": 1,
                          "start_index": 1,
                          "index supporting sensor": []},
               "gearbox": {"temperature_file_name_rule": "gearbox{}_temp_input",
                           "dev_parameters_name": "gearbox",
                           "total_number": 0,
                           "start_index": 1},
               "sodimm": {"temperature_file_name_rule": "sodimm{}_temp_input",
                          "dev_parameters_name": "sodimm\\d_temp",
                          "total_number": 1,
                          "start_index": 1},
               "comex_amb": {"temperature_file_name_rule": "comex_amb",
                             "dev_parameters_name": "sensor_amb",
                             "total_number": 1},
               "pch": {"temperature_file_name_rule": "pch",
                       "dev_parameters_name": "pch_amb",
                       "total_number": 1},
               "fan": {"temperature_file_name_rule": "fan_amb",
                       "dev_parameters_name": "sensor_amb",
                       "total_number": 1},
               "port_amb": {"temperature_file_name_rule": "port_amb",
                            "dev_parameters_name": "sensor_amb",
                            "total_number": 1},
               "fan_amb": {"temperature_file_name_rule": "fan_amb",
                           "dev_parameters_name": "sensor_amb",
                           "total_number": 1},
               "ambient": {"temperature_file_name_rule": ["port_amb", "fan_amb"],
                           "dev_parameters_name": "sensor_amb",
                           "total_number": 1},
               "system_fan_dir": 1,
               "fan_drwr_capacity": 1,
               "fan_drwr_num": 1,
               }

SENSOR_ERR_TEST_DATA = {"fan_err_present": "fan{}_status", "fan_err_direction": "fan{}_dir",
                        "fan_err_tacho": "fan{}_speed_get", "psu_err_present": "psu{}_status",
                        "psu_err_direction": "psu{}_fan_dir",
                        "sensor_read_error": ["invalid_value", "missing_file"]}

SENSOR_TEMPERATURE_TEST_LIST = ["asic", "ambient", "cpu_pack", "module", "voltmon", "sodimm"]


class TC_CONST(object):
    """
    @summary: hw-management thermal control constants
    """

    # hw-management folder
    HW_MGMT_FOLDER = "/var/run/hw-management"
    # thermal folder
    HW_THERMAL_FOLDER = f"{HW_MGMT_FOLDER}/thermal"
    # pwm1 path
    PWM1_PATH = f"{HW_THERMAL_FOLDER}/pwm1"
    # thermal control config
    TC_CONFIG_FILE = f"{HW_MGMT_FOLDER}/config/tc_config.json"
    # list of platforms without a link to TC_CONFIG_FILE (copy file instead of link)
    PLATFORMS_WITHOUT_TC_CONFIG_LINK = ["MQM9700", 'QM8790', 'QM3400']
    # list of Juliet platforms
    JULIET_PLATFORMS = ['x86_64-nvidia_n5100_ld-r0', 'x86_64-nvidia_n5110_ld-r0', 'x86_64-nvidia_n5112_ld-r0',
                        'x86_64-nvidia_n5200_ld-r0', 'x86_64-nvidia_n5600_ld-r0']
    # hw-management-thermal folder
    HW_MGMT_THERMAL_FOLDER = "/etc/hw-management-thermal"

    # thermal control log file
    TC_LOG_FILE = "/var/log/tc_log"

    # File which define TC report period. TC should be restarted to apply changes in this file
    PERIODIC_REPORT_FILE = "config/periodic_report"
    # suspend control file path
    SUSPEND_FILE = f"{HW_MGMT_FOLDER}/config/suspend"
    # Sensor files for ambient temperature measurement
    FAN_SENS = "fan_amb"
    PORT_SENS = "port_amb"

    # Fan direction string alias
    # fan dir:
    # 0: port > fan, dir fan to port C2P  Port t change not affect
    # 1: port < fan, dir port to fan P2C  Fan t change not affect
    C2P = "C2P"
    P2C = "P2C"
    DEF_DIR = "P2C"
    DIR_C2P_CODE = 0
    DIR_P2C_CODE = 1

    UNKNOWN = "Unknown"

    # delay before TC start (sec)
    THERMAL_WAIT_FOR_CONFIG = 120

    # Default period for printing TC report (in sec.)
    PERIODIC_REPORT_TIME = 1 * 60

    PWM_MAX = 255
    PWM_MAX_PERCENT = 100
    PWM_ADJUST_STEP = 2
    RPM_TOLERANCE = 30

    # Main TC loop state
    UNCONFIGURED = "UNCONFIGURED"
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"

    # Default RPM MIN MAX value
    RPM_MIN_MAX = {"val_min": 5000, "val_max": 30000}

    # Time for FAN rotation stabilize after change
    FAN_RELAX_TIME = 10

    # Time for PWM reaching the target value when PWM grow
    PWM_GROW_TIME = 2

    # default system devices number
    PSU_COUNT_DEF = 2
    FAN_DRWR_COUNT_DEF = 6
    FAN_TACHO_COUNT_DEF = 6
    MODULE_COUNT_DEF = 16
    GEARBOX_COUNT_DEF = 0
    ASIC_COUNT_DEF = 1
    CPU_CORE_COUNT_DEF = 2
    CPU_PACK_DEF = 1
    SODIMM_DEF = 0
    VOLTMON_DEF = 0
    PCH_DEF = 0
    COMEX_AMB_DEF = 1
    FAN_AMB_DEF = 1
    PORT_AMB_DEF = 1
    AMBIENT_DEF = 2

    # Mocked module EEPROM max temperature values (millicelsius) for val_max_clamp scenarios
    MODULE_EEPROM_TEMP_MAX_ABOVE_CLAMP = 75000  # 75°C — clamp should apply
    MODULE_EEPROM_TEMP_MAX_BELOW_CLAMP = 60000  # 60°C — clamp should not apply


class SysfsNotExistError(Exception):
    """
    Exception when sys fs not exist.
    """
    pass


class MockSensors:

    def __init__(self, dut, cli_objects):
        self.dut = dut
        self.cli_object = cli_objects.dut
        self.links_to_be_recovered = {}
        self.regular_files_to_be_recovered = {}
        self.recover_retry = 3

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.recover_sensor_links()

    def read_value(self, file_path):
        """
        Read sys fs file content.
        :param file_path: Sys fs file path.
        :return: Content of sys fs file.
        """
        return self.cli_object.general.read_file(file_path).strip()

    def _cache_regular_file_value(self, file_path):
        """
        Cache file value for regular file.
        :param file_path: Regular file path.
        :return:
        """

        value = self.cli_object.general.read_file(file_path)
        self.regular_files_to_be_recovered[file_path] = value.strip()
        self.dut.run_cmd(f'sudo chown admin {file_path}')

    def mock_value(self, file_path, value):
        """
        Unlink existing sys fs file and replace it with a new one. Write given value to the new file.
        :param file_path: Sys fs file path.
        :param value: Value to write to sys fs file.
        :return:
        """
        if file_path not in self.regular_files_to_be_recovered and file_path not in self.links_to_be_recovered:
            file_status = self.cli_object.general.stat(file_path)
            if not file_status['exists']:
                raise SysfsNotExistError('{} not exist'.format(file_path))
            if file_status['islink']:
                # Suspend TC loop during unlink-to-write to prevent sensor_read_error
                # from pushing PWM to 100% when the file is momentarily empty.
                self.cli_object.hw_mgmt.suspend_thermal_control()
                try:
                    self._unlink(file_path)
                    self.dut.run_cmd(f'sudo echo {value} > {file_path}')
                finally:
                    self.cli_object.hw_mgmt.resume_thermal_control()
                return
            else:
                self._cache_regular_file_value(file_path)
        self.cli_object.hw_mgmt.suspend_thermal_control()
        try:
            self.dut.run_cmd(f'sudo echo {value} > {file_path}')
        finally:
            self.cli_object.hw_mgmt.resume_thermal_control()

    def remove_file(self, file_path):
        """
        Unlink existing sys file and remove sys file.
        :param file_path: Sys fs file path.
        :return:
        """
        if file_path not in self.regular_files_to_be_recovered and file_path not in self.links_to_be_recovered:
            file_status = self.cli_object.general.stat(file_path)
            if file_status['islink']:
                self._unlink(file_path)
            else:
                self._cache_regular_file_value(file_path)
        self.dut.run_cmd(f'rm {file_path}')

    def mock_temperature(self, file_path, temperature):
        """
        Mock temperature value of this thermal with given value.
        :param file_path: file_path value.
        :param temperature: Temperature value.
        :return:
        """
        self.mock_value(file_path, temperature)

    def mock_sensor_error(self, file_path, sensor_error_value):
        """
        Mock sensor value of this thermal with given value.
        :param file_path: sensor file_path value.
        :param sensor_error_value: Temperature value.
        :return:
        """
        self.mock_value(file_path, sensor_error_value)

    def _unlink(self, file_path):
        """
        Unlink given sys fs file, record its soft link target.
        :param file_path: Sys fs file path.
        :return:
        """
        readlink_output = self.dut.run_cmd(f'sudo readlink {file_path}')
        self.links_to_be_recovered[file_path] = readlink_output
        self.dut.run_cmd(f'sudo unlink {file_path}')
        self.dut.run_cmd(f'sudo touch {file_path}')
        self.dut.run_cmd(f'sudo chown admin {file_path}')

    def get_all_fan_speed(self):
        cmd_get_all_fan_speed = ""
        fan_total_number = SENSOR_DATA["fan"]["total_number"]
        for fan_index in range(1, fan_total_number + 1):
            file_path = f"{TC_CONST.HW_THERMAL_FOLDER}/fan{fan_index}_speed_get"
            cmd_get_all_fan_speed = cmd_get_all_fan_speed + f"sudo cat {file_path} "
            if fan_index != fan_total_number:
                cmd_get_all_fan_speed = cmd_get_all_fan_speed + " && "
        logger.info(f"get all fan speed cmd :{cmd_get_all_fan_speed}")
        all_fan_speed = self.dut.run_cmd(cmd_get_all_fan_speed)
        fan_speed_list = []
        for fan_speed in all_fan_speed.split("\n"):
            fan_speed_list.append(int(fan_speed.strip()))
        if len(fan_speed_list) != fan_total_number:
            raise Exception(
                f"Fan speed number is not correct. Actual fan speed total number is {len(fan_speed_list)}, expected number is :{fan_total_number}")
        return fan_speed_list

    def recover_sensor_links(self):
        """
        Destructor of MockerHelper. Re-link all sys fs files.
        :return:
        """
        failed_recover_links = {}
        for file_path, link_target in self.links_to_be_recovered.items():
            try:
                self.dut.run_cmd('sudo ln -f -s {} {}'.format(link_target, file_path))
            except Exception:
                # Catch any exception for later retry
                failed_recover_links[file_path] = link_target

        failed_recover_files = {}
        for file_path, value in self.regular_files_to_be_recovered.items():
            try:
                if value is None:
                    self.dut.run_cmd('sudo rm -f {}'.format(file_path))
                else:
                    self.dut.run_cmd('sudo echo \'{}\' > {}'.format(value, file_path))
            except Exception:
                # Catch any exception for later retry
                failed_recover_files[file_path] = value

        self.links_to_be_recovered.clear()
        self.regular_files_to_be_recovered.clear()
        # If there is any failed recover files, retry it
        if failed_recover_links or failed_recover_files:
            self.recover_retry -= 1
            if self.recover_retry > 0:
                self.links_to_be_recovered = failed_recover_links
                self.regular_files_to_be_recovered = failed_recover_files
                # The failed files might be used by other sonic daemons, delay 1 second
                # here to avoid conflict
                time.sleep(1)
                self.recover_sensor_links()
            else:
                # We don't want to retry it infinite, and 5 times retry
                # is enough, so if it still fails after the retry, it
                # means there is probably an issue with our sysfs, we need
                # mark it fail here
                failed_recover_files.update(failed_recover_links)
                error_message = "Failed to recover all files, failed files: {}".format(failed_recover_files)
                logger.error(error_message)
                raise RuntimeError(error_message)


def collect_sensors_info(cli_objects, dut):
    """
    Collect sensor information
    """
    cli_object_dut = cli_objects.dut
    sensors_counter = {"fan": TC_CONST.FAN_TACHO_COUNT_DEF,
                       "fan_drwr": TC_CONST.FAN_DRWR_COUNT_DEF,
                       "psu": TC_CONST.PSU_COUNT_DEF,
                       "psu_pwr": TC_CONST.PSU_COUNT_DEF,
                       "module": TC_CONST.MODULE_COUNT_DEF,
                       "gearbox": TC_CONST.GEARBOX_COUNT_DEF,
                       "asic": TC_CONST.ASIC_COUNT_DEF,
                       "cpu_core": TC_CONST.CPU_CORE_COUNT_DEF,
                       "cpu_pack": TC_CONST.CPU_PACK_DEF,
                       "sodimm": TC_CONST.SODIMM_DEF,
                       "voltmon": TC_CONST.VOLTMON_DEF,
                       "pch": TC_CONST.PCH_DEF,
                       "comex_amb": TC_CONST.COMEX_AMB_DEF,
                       "port_amb": TC_CONST.PORT_AMB_DEF,
                       "fan_amb": TC_CONST.FAN_AMB_DEF,
                       "ambient": TC_CONST.AMBIENT_DEF
                       }

    logger.info("Collecting sensors info...")

    sensors_counter['fan'] = int(cli_object_dut.general.read_file(f"{TC_CONST.HW_MGMT_FOLDER}/config/max_tachos"))
    logger.info(f"Fan {sensors_counter['fan']}")

    sensors_counter['fan_drwr'] = int(cli_object_dut.general.read_file(f"{TC_CONST.HW_MGMT_FOLDER}/config/fan_drwr_num"))
    logger.info(f"Fan :{sensors_counter['fan']}")

    fan_drwr_capacity = int(sensors_counter['fan'] / sensors_counter['fan_drwr'])
    logger.info(f"fan_drwr_capacity :{fan_drwr_capacity}")

    sensors_counter['psu'] = int(cli_object_dut.general.read_file(f"{TC_CONST.HW_MGMT_FOLDER}/config/hotplug_psus"))
    logger.info(f"PSU :{sensors_counter['psu']}")

    sensors_counter['gearbox'] = int(cli_object_dut.general.read_file(f"{TC_CONST.HW_MGMT_FOLDER}/config/gearbox_counter"))
    logger.info(f"gearbox counter:{sensors_counter['gearbox']}")

    thermal_file_list = cli_object_dut.general.ls(f"{TC_CONST.HW_THERMAL_FOLDER}", "-l").split("\n")
    # Find voltmon temp sensors num
    update_thermal_sensors_counter(sensors_counter, r'voltmon[0-9]+_temp_input', "voltmon", thermal_file_list)

    # Find cpu core sensors num
    update_thermal_sensors_counter(sensors_counter, r'cpu_core[0-9]', "cpu_core", thermal_file_list)

    # Find cpu pack sensors num
    update_thermal_sensors_counter(sensors_counter, r'cpu_pack', "cpu_pack", thermal_file_list)

    # Find pch sensors num
    update_thermal_sensors_counter(sensors_counter, r'pch', "pch", thermal_file_list)

    # Find sodimm sensors num
    update_thermal_sensors_counter(sensors_counter, r'sodimm[0-9]+_temp_input', "sodimm", thermal_file_list)

    # Find module sensors num
    update_thermal_sensors_counter(sensors_counter, r'module[0-9]+_temp_input', "module", thermal_file_list)

    # Update the the value for total_number in SENSOR_DATA
    for sensor_type, counter in sensors_counter.items():
        if sensor_type in SENSOR_DATA:
            SENSOR_DATA[sensor_type]['total_number'] = counter
    SENSOR_DATA["fan_drwr_capacity"] = fan_drwr_capacity
    SENSOR_DATA["fan_drwr_num"] = sensors_counter['fan_drwr']
    SENSOR_DATA["system_fan_dir"] = get_system_fan_dir(dut)
    return get_tested_sensor_list(sensors_counter, dut, cli_object_dut)


def update_thermal_sensors_counter(sensors_counter, reg_sensor_file_name, sensor_name, thermal_file_list):
    match_file_num = 0
    for fname in thermal_file_list:
        sensor_file_res = re.search(f'.* {reg_sensor_file_name}(?: .*|$)', fname)
        if sensor_file_res:
            match_file_num += 1

    sensors_counter[sensor_name] = match_file_num
    logger.info(f"{sensor_name} counter:{sensors_counter[sensor_name]}")


def get_system_fan_dir(dut):
    """
    This function is to get system fan direction
    We have two fan dirs: P2C and C2P,
    1. Calculate the fan number with dir which is P2C or C2P respectively
    2. The System fan dir is the one with the max one of above two
    :param dut:
    :return: sys_fan_dir
    """
    fan_drwr_total_num = SENSOR_DATA["fan_drwr_num"]
    cmd_get_all_system_fan_dir = ""
    fan_dir_list = []
    for fan_index in range(1, fan_drwr_total_num + 1):
        fan_dir_path = f"{TC_CONST.HW_THERMAL_FOLDER}/fan{fan_index}_dir"
        cmd_get_all_system_fan_dir = cmd_get_all_system_fan_dir + f"sudo cat {fan_dir_path} && "
    cmd_get_all_system_fan_dir = cmd_get_all_system_fan_dir.strip(' ').strip('&&')

    logger.info(f"get system fan dir cmd:{cmd_get_all_system_fan_dir}")

    all_system_fan_dir = dut.run_cmd(cmd_get_all_system_fan_dir)
    for fan_dir in all_system_fan_dir.split("\n"):
        fan_dir_list.append(fan_dir)
    sys_fan_dir = TC_CONST.DIR_P2C_CODE if fan_dir_list.count(str(TC_CONST.DIR_P2C_CODE)) > fan_dir_list.count(str(TC_CONST.DIR_C2P_CODE)) else TC_CONST.DIR_C2P_CODE
    logger.info(f"system fan dir is {sys_fan_dir}")
    return sys_fan_dir


def get_tested_sensor_list(sensors_counter, dut, cli_object_dut):
    sensor_temperature_test_list = []
    for sensor in SENSOR_TEMPERATURE_TEST_LIST:
        if sensor in sensors_counter and sensors_counter[sensor] > 0:
            sensor_temperature_test_list.append(sensor)
    if "module" in sensor_temperature_test_list:
        module_index_supporting_sensor_list = get_module_index_supporting_sensor(dut)
        if module_index_supporting_sensor_list:
            SENSOR_DATA["module"]["index supporting sensor"] = module_index_supporting_sensor_list
        else:
            sensor_temperature_test_list.remove("module")

    if hasattr(cli_object_dut.chassis, 'get_platform'):
        not_support_asic_platform_list = ["x86_64-nvidia_sn4280_simx-r0"] + TC_CONST.JULIET_PLATFORMS
        platform = cli_object_dut.chassis.get_platform()
        if platform in not_support_asic_platform_list:
            sensor_temperature_test_list.remove("asic")

        if platform in TC_CONST.JULIET_PLATFORMS:
            for sensor in ["ambient", "module"]:
                if sensor in sensor_temperature_test_list:
                    sensor_temperature_test_list.remove(sensor)

    if not sensor_temperature_test_list:
        raise Exception("No sensor is available for testing ")
    logger.info(f" Sensors for temperature sweep test are:{sensor_temperature_test_list}")
    return sensor_temperature_test_list


def get_module_index_supporting_sensor(dut):
    cmds_get_module_temp_crit_res = '('
    for module_index in range(SENSOR_DATA["module"]["start_index"], SENSOR_DATA["module"]["total_number"] + 1):
        cmds_get_module_temp_crit_res += f"sudo cat {TC_CONST.HW_THERMAL_FOLDER}/module{module_index}_temp_crit && echo && "
    cmds_get_module_temp_crit_res = cmds_get_module_temp_crit_res.strip(" ").strip('&&') + ") | grep -v '^\\s*$'"
    logging.info(f"get all module temp crits cmds :{cmds_get_module_temp_crit_res}")

    module_index_supporting_sensor_list = []
    all_module_temp_crit_res_list = dut.run_cmd(cmds_get_module_temp_crit_res)

    for module_index, module_value in enumerate(all_module_temp_crit_res_list.split("\n")):
        if module_value != "0":
            module_index_supporting_sensor_list.append(module_index + 1)
    logging.info(f"module index supporting sensor  is {module_index_supporting_sensor_list} ")

    return module_index_supporting_sensor_list


def get_tc_config(cli_objects):
    tc_config_content = cli_objects.dut.general.read_file(f"{TC_CONST.TC_CONFIG_FILE}")
    tc_config_content = tc_config_content.replace('asic\\\\d+', 'asic')
    tc_config_content = tc_config_content.replace('asic\\\\d*', 'asic')
    tc_config_dict = json.loads(tc_config_content)
    logger.info(f"tc_config: {tc_config_dict}")
    return tc_config_dict


def calculate_rpm(tc_config_dict, pwm_curr, fan_dir, fan_tachos_index):
    """
    This method is to calculate rpm based on pwm, fan_dir, fan_tachos_index
    1. Get fan_trend data
       e.g. fan_trend data
       "fan_trend" : {
                "C2P": {
                        "0" : {"rpm_min":3100, "rpm_max":11000, "slope": 99, "pwm_min" : 10,
                        "pwm_max_reduction" : 10, "rpm_tolerance" : 30},
                        "1" : {"rpm_min":3100, "rpm_max":11000, "slope": 99, "pwm_min" : 10,
                        "pwm_max_reduction" : 10, "rpm_tolerance" : 30}},
                "P2C": {
                        "0" : {"rpm_min":3100, "rpm_max":11000, "slope": 99, "pwm_min" : 10,
                        "pwm_max_reduction" : 10, "rpm_tolerance" : 30},
                        "1" : {"rpm_min":3100, "rpm_max":11000, "slope": 99, "pwm_min" : 10,
                        "pwm_max_reduction" : 10, "rpm_tolerance" : 30}
                }
        },
    2. Calculate rpm based on the below formula
       slope = fan_trend_info["slope"]
       rpm_calculated= rpm_max + slope * (pwm_curr - TC_CONST.PWM_MAX_PERCENT)
    :param tc_config_dict:
    :param pwm_curr:
    :param fan_dir:
    :param fan_tachos_index:
    :return: rpm_calculated
    """
    fan_trend_info = tc_config_dict["fan_trend"][fan_dir][fan_tachos_index]
    rpm_max = fan_trend_info["rpm_max"]
    if rpm_max == 0:
        logging.info("No rpm_max value, use the default value")
        rpm_max = TC_CONST.RPM_MIN_MAX["val_max"]
    slope = fan_trend_info["slope"]
    rpm_calculated = rpm_max + slope * (pwm_curr - TC_CONST.PWM_MAX_PERCENT)
    logger.info(f"rpm_calculated:{rpm_calculated}, pwm_curr:{pwm_curr}")
    return rpm_calculated


def calculate_rpm_diff_norm(rpm_real, rpm_calculated):
    rpm_diff = abs(rpm_real - rpm_calculated)
    rpm_diff_norm = float(rpm_diff) / rpm_calculated
    return rpm_diff_norm


def get_temperature_digit(value):
    if isinstance(value, str):
        value = int(value.strip("!"))
    logger.info(f"temperature digit is {value}")
    return value


def _resolve_thermal_threshold(mock_sensor, config_val, sysfs_path):
    """
    Resolve a val_min or val_max value following the TC loop's read_val_min_max logic:
      - If config value starts with "!" → use the constant (strip "!" and convert)
      - Otherwise → read from sysfs file; use config value as fallback

    Args:
        mock_sensor: MockSensors instance for reading sysfs files on the DUT
        config_val: value from tc_config.json (int, or str like "!70000" or "95000")
        sysfs_path: sysfs file path to read if not a "!" constant

    Returns:
        int value in millicelsius
    """
    if isinstance(config_val, str) and config_val.startswith("!"):
        val = int(config_val[1:])
        logger.info(f"Using constant value from config: {val} (from '{config_val}')")
        return val

    # Not a "!" constant — read from sysfs, fall back to config default
    fallback = int(config_val) if config_val is not None else 0
    try:
        val = int(mock_sensor.read_value(sysfs_path))
        logger.info(f"Read dynamic value from {sysfs_path}: {val}")
        return val
    except Exception as e:
        logger.warning(f"Failed to read {sysfs_path}, using fallback {fallback}: {e}")
        return fallback


def update_effective_val_min_max(mock_sensor, tc_config_dict, sensor_type, sensor_file_path):
    """
    Update tc_config_dict in-place with the effective val_min/val_max that the TC loop
    actually uses, so all downstream code (calculate_pwm, temperature sweep range, etc.)
    automatically uses the correct values without extra parameter passing.

    Replicates the TC loop's read_val_min_max logic (hw_management_thermal_control.py):
      - Config value starts with "!" → use constant directly
      - Otherwise → read from the corresponding sysfs file, config value as fallback

    For module sensors, val_min/val_max are derived from temp_crit, an optional clamp, and offsets:
        effective_temp_max = min(read(module{N}_temp_crit), val_max_clamp) if configured
                             read(module{N}_temp_crit) otherwise
        val_max = effective_temp_max + val_max_offset
        val_min = val_max + val_min_offset

    For asic sensors:
        val_max = read(asic_temp_crit)  (or "!" constant)
        val_min = read(asic_temp_norm)  (or "!" constant)

    For other sensors (sodimm, cpu_pack, voltmon, ambient):
        val_min/val_max = "!" constant, or read from {name}_min/{name}_max
    """
    dev_parameters_name = SENSOR_DATA[sensor_type]['dev_parameters_name']
    sensor_config = tc_config_dict['dev_parameters'][dev_parameters_name]
    config_val_min = sensor_config.get("val_min")
    config_val_max = sensor_config.get("val_max")

    if sensor_type == "module":
        temp_crit_path = sensor_file_path.replace("_temp_input", "_temp_crit")
        eeprom_max = _resolve_thermal_threshold(mock_sensor, config_val_max, temp_crit_path)
        val_max_clamp = sensor_config.get("val_max_clamp")
        effective_temp_max = eeprom_max
        if val_max_clamp is not None:
            val_max_clamp = int(val_max_clamp)
            effective_temp_max = min(eeprom_max, val_max_clamp)
            logger.info(f"Module effective temp max: min(eeprom_max={eeprom_max}, "
                        f"val_max_clamp={val_max_clamp}) = {effective_temp_max}")
        # Offsets are defined in the TC loop's internal default config
        # (hw_management_thermal_control.py thermal_module_sensor), not in tc_config.json.
        # Defaults: val_max_offset=0, val_min_offset=-20000
        val_max_offset = sensor_config.get("val_max_offset", 0)
        val_min_offset = sensor_config.get("val_min_offset", -20000)
        val_max = effective_temp_max + val_max_offset
        val_min = val_max + val_min_offset
    elif sensor_type == "asic":
        # sensor_file_path is like .../thermal/asic (no _temp_input suffix for asic)
        base_name = os.path.basename(sensor_file_path)
        temp_crit_path = os.path.join(TC_CONST.HW_THERMAL_FOLDER, f"{base_name}_temp_crit")
        temp_norm_path = os.path.join(TC_CONST.HW_THERMAL_FOLDER, f"{base_name}_temp_norm")
        val_max = _resolve_thermal_threshold(mock_sensor, config_val_max, temp_crit_path)
        val_min = _resolve_thermal_threshold(mock_sensor, config_val_min, temp_norm_path)
    else:
        sensor_base = os.path.basename(sensor_file_path)
        min_path = os.path.join(TC_CONST.HW_THERMAL_FOLDER,
                                sensor_base.replace("_input", "_min") if "_input" in sensor_base
                                else f"{sensor_base}_min")
        max_path = os.path.join(TC_CONST.HW_THERMAL_FOLDER,
                                sensor_base.replace("_input", "_max") if "_input" in sensor_base
                                else f"{sensor_base}_max")
        val_min = _resolve_thermal_threshold(mock_sensor, config_val_min, min_path)
        val_max = _resolve_thermal_threshold(mock_sensor, config_val_max, max_path)

    logger.info(f"Effective val_min={val_min}, val_max={val_max} for {sensor_type} "
                f"(config was val_min={config_val_min}, val_max={config_val_max})")
    sensor_config["val_min"] = val_min
    sensor_config["val_max"] = val_max


def calculate_pwm(tc_config_dict, dev_parameter, temperature):
    """
    @summary: Calculate PWM by formula
    PWM = pwm_min + ((value - value_min)/(value_max-value_min)) * (pwm_max - pwm_min)
    @return: PWM value rounded to nearest value
    """
    sensor_pwm_temp_info = tc_config_dict["dev_parameters"][dev_parameter]
    val_min = get_temperature_digit(sensor_pwm_temp_info["val_min"])
    val_max = get_temperature_digit(sensor_pwm_temp_info["val_max"])
    pwm_min = sensor_pwm_temp_info["pwm_min"]
    pwm_max = sensor_pwm_temp_info["pwm_max"]

    if val_max == val_min:
        return pwm_min

    pwm = pwm_min + (float(temperature - val_min) / (val_max - val_min)) * (pwm_max - pwm_min)

    if pwm > pwm_max:
        pwm = pwm_max

    if pwm < pwm_min:
        pwm = pwm_min
    logger.info(f"calculated pwm:{pwm} for temperature: {temperature}")
    return int(round(pwm))


def get_pwm(mock_sensor):
    pwm = round(int(mock_sensor.read_value(TC_CONST.PWM1_PATH)) * 100 / TC_CONST.PWM_MAX)
    logger.info(f"Current PWM is: {pwm}")
    return pwm


def verify_pwd_and_rpm_are_expected_value(mock_sensor, tc_config_dict, sensor_type, temperature, expected_pwm=None,
                                          initial_pwm=None):
    verify_pwm_matches_expected_value(mock_sensor, tc_config_dict, sensor_type, temperature, expected_pwm,
                                      initial_pwm=initial_pwm)
    verify_rpm_is_expected_value(mock_sensor, tc_config_dict)


def verify_pwm_matches_expected_value(mock_sensor, tc_config_dict, sensor_type, temperature, expected_pwm=None,
                                      initial_pwm=None):
    """
    Verify current PWM matches the expected value within ±1% tolerance.

    The system PWM is the MAX across all sensors. If expected_pwm is not higher
    than initial_pwm (the baseline PWM before any mocking), the tested sensor
    is NOT the dominant contributor — another sensor is already driving the PWM
    at least as high. In that case the tested sensor's target cannot be validated
    precisely, so we skip the check.

    When expected_pwm > initial_pwm the tested sensor IS the dominant one and
    we apply the strict ±1% tolerance to make sure the smoothing filter
    converges to the correct target (guards against overshoot from stale slope).

    Args:
        initial_pwm: The baseline PWM captured once before any sensor mocking.
            Must be provided by the caller. This value stays constant throughout
            the entire increase/decrease temperature sweep so that the dominance
            check is not affected by intermediate TC loop adjustments.
    """
    pwm_tolerance = 1  # ±1% PWM tolerance
    with allure.step(f"Verify current PWM matches expected value within ±{pwm_tolerance}%"):
        dev_parameter = SENSOR_DATA[sensor_type]["dev_parameters_name"]
        if not expected_pwm:
            expected_pwm = calculate_pwm(tc_config_dict, dev_parameter, temperature)

        if initial_pwm is None:
            initial_pwm = get_pwm(mock_sensor)
            logger.warning("initial_pwm not provided, reading current PWM as fallback")

        if expected_pwm <= initial_pwm:
            logger.info(f"expected_pwm:{expected_pwm} <= initial_pwm:{initial_pwm}, "
                        f"tested sensor is not the dominant PWM contributor, skip precise check")
            return

        poll_time = int(tc_config_dict["dev_parameters"][dev_parameter]['poll_time'])

        # cpu_pack and sodimm are special cases, when the pwm can be adjusted to the expected one,
        # the max wait time is 15*poll_time and 150 respectively.
        # for the remaining sensor, it just need wait poll_time + TC_CONST.PWM_GROW_TIME),
        # the pwm should be adjusted to the expected one
        # Module and asic sensors need extended retry due to TC loop PWM ramp-down
        # limiting: TC loop raises PWM instantly but lowers it gradually — each
        # pwm_worker cycle (5s) reduces PWM by at most pwm_max_reduction (10 on
        # MSN2700). E.g. PWM 100->60 takes ~7 worker cycles x 5s = 35s, plus
        # sensor poll wait. 60s provides sufficient margin for any sensor.
        # cpu_pack and sodimm also need extended wait due to input_smooth_level.
        # ambient (sensor_amb) needs extended retry because its poll_time is
        # 30s and the default formula (poll_time + PWM_GROW_TIME = 32) leaves
        # only 2s margin — insufficient for the TC loop PWM ramp-down phase.
        # The TC _pwm_worker uses a halving algorithm to lower PWM:
        #   step = min(round(diff/2 + 0.5), pwm_max_reduction)
        # running every PWM_WORKER_POLL_TIME (5s). E.g. PWM 40->30:
        #   cycle1: step=6 -> 34, cycle2: step=2 -> 32, cycle3: step=2 -> 30
        # = 15s of ramp-down. Worst case: 30s poll + 15s ramp = 45s.
        # Using 60 for sufficient margin.
        sepcial_sensor_try_times = {"module": 60,
                                    "ambient": 60,
                                    "asic": 60,
                                    "cpu_pack": 15 * poll_time,
                                    "sodimm": 360}
        try_times = sepcial_sensor_try_times.get(sensor_type, poll_time + TC_CONST.PWM_GROW_TIME)

        pwm_low = math.floor(expected_pwm * (1 - pwm_tolerance / 100.0))
        pwm_high = math.ceil(expected_pwm * (1 + pwm_tolerance / 100.0))

        @retry(Exception, tries=try_times, delay=1)
        def check_pwm_within_tolerance():
            pwm_curr = get_pwm(mock_sensor)
            assert pwm_low <= pwm_curr <= pwm_high, \
                f"PWM:{pwm_curr} is not within expected range [{pwm_low}, {pwm_high}] (target:{expected_pwm} ±{pwm_tolerance}%)"

        check_pwm_within_tolerance()


@retry(Exception, tries=30, delay=3)
def compare_pwd_with_expected_value(mock_sensor, expected_pwm, operation):
    with allure.step(f"Verify actual pwd is {operation.__name__} {expected_pwm}"):
        pwm_curr = get_pwm(mock_sensor)
        assert operation(pwm_curr, expected_pwm), f"expected_pwm:{expected_pwm} {operation.__name__} current pwm:{pwm_curr}"


@retry(Exception, tries=3, delay=TC_CONST.FAN_RELAX_TIME)
def verify_rpm_is_expected_value(mock_sensor, tc_config_dict):
    """
    This method is to verify rpm is in the expected range
    1. Get the value of fan_trend from tc_config based on the fan dir and tachos_index
    2. Calculate the expected rpm based on current pwm
    3. Get the rpm tolerance from the value of fan_trend
    4. Calculate the real rpm
    5. Verify the diff between real rpm and expected rpm should be less than rpm tolerance
    :param mock_sensor:
    :param tc_config_dict:
    :return: True or False
    """
    with allure.step("Verify rpm is expected_value"):
        pwm_curr = get_pwm(mock_sensor)
        sys_fan_dir_str = get_system_fan_str()
        fan_drwr_capacity = SENSOR_DATA["fan_drwr_capacity"]

        def get_rpm_info_by_tacho_index(fan_tachos_index):
            rpm_calculated_tacho = calculate_rpm(tc_config_dict, pwm_curr, sys_fan_dir_str, fan_tachos_index)
            if 'rpm_tolerance' in tc_config_dict["fan_trend"][sys_fan_dir_str][fan_tachos_index]:
                rpm_tolerance_tacho = tc_config_dict["fan_trend"][sys_fan_dir_str][fan_tachos_index]['rpm_tolerance']
            else:
                rpm_tolerance_tacho = TC_CONST.RPM_TOLERANCE
            return rpm_calculated_tacho, rpm_tolerance_tacho

        rpm_calculated_tacho0, rpm_tolerance_tach0 = get_rpm_info_by_tacho_index(fan_tachos_index="0")
        rpm_calculated_tacho1, rpm_tolerance_tach1 = get_rpm_info_by_tacho_index(fan_tachos_index="1")

        all_fan_speed_list = mock_sensor.get_all_fan_speed()
        for index, fan_speed in enumerate(all_fan_speed_list):
            fan_index = index + 1
            rpm_real = fan_speed
            if (fan_index + 1) % fan_drwr_capacity:
                rpm_calculated, rpm_tolerance = rpm_calculated_tacho1, rpm_tolerance_tach1
            else:
                rpm_calculated, rpm_tolerance = rpm_calculated_tacho0, rpm_tolerance_tach0
            rpm_diff_norm = calculate_rpm_diff_norm(rpm_real, rpm_calculated)
            logger.info(f"fan{fan_index}, rpm_diff_norm:{rpm_diff_norm}, rpm tolerance:{rpm_tolerance}")
            assert rpm_diff_norm <= rpm_tolerance, f"fan{fan_index}, actual rpm diff:{rpm_diff_norm} >  expected rpm tolerance:{rpm_tolerance}"


def get_sensor_temperature_file_name(sensor_type, platform_params):
    sensor_temperature_file_name = SENSOR_DATA[sensor_type]['temperature_file_name_rule']
    sensor_temperature_file_path_list = []
    if 'start_index' in SENSOR_DATA[sensor_type]:
        if sensor_type == "module":
            sensor_index = random.choice(SENSOR_DATA[sensor_type]["index supporting sensor"])
        elif sensor_type == "voltmon":
            # For ACS-MSN2100, ACS-MSN4600, ACS-MSN2410 and Mellanox-SN2700 the voltmon numbering has gaps.
            # it is by design, Hardware will not fix it.
            hwsku_voltmon_index_map = {'Mellanox-SN2700': [1, 2, 6]}
            default_voltmon_index_list = [str(index) for index in range(SENSOR_DATA[sensor_type]["start_index"],
                                                                        SENSOR_DATA[sensor_type]["total_number"] + 1)]
            voltmon_index_list = hwsku_voltmon_index_map.get(platform_params.hwsku, default_voltmon_index_list)
            sensor_index = random.choice(voltmon_index_list)
        else:
            start_index = SENSOR_DATA[sensor_type]["start_index"]
            total_number = SENSOR_DATA[sensor_type]["total_number"]
            if 'bison' in platform_params['host_name']:
                start_index = start_index + 1
                total_number = total_number + 1
            sensor_index = random.randint(start_index, total_number)
        sensor_temperature_file_name = sensor_temperature_file_name.format(sensor_index)

    if sensor_type == "ambient":
        for file in sensor_temperature_file_name:
            sensor_temperature_file_path_list.append(os.path.join(TC_CONST.HW_THERMAL_FOLDER, file))
    else:
        sensor_temperature_file_path_list.append(os.path.join(TC_CONST.HW_THERMAL_FOLDER, sensor_temperature_file_name))
    logger.info(f"sensor file name is {sensor_temperature_file_path_list}")
    return sensor_temperature_file_path_list


def get_expected_pwm_by_temp_from_dmin_table(tc_config_dict, temperature, fan_dir, sensor_err_type):
    """
    This method is get expected pwm based on the temperature and dmin table
    e.g.. dmin table

        "dmin" : {
                "C2P": {
                        "fan_err": {
                                "tacho": {"-127:30": 20, "31:40": 30, "41:120": 40},
                                "present": {"-127:25": 20, "26:35": 30, "36:120": 40},
                                "direction": {"-127:25": 20, "26:35": 30, "36:120": 40}
                        },
                        "psu_err":  {
                                "present": {"-127:25": 20, "26:35": 30, "36:120": 40},
                                "direction": {"-127:30": 20, "31:40": 30, "41:120": 40}
                        },
                        "sensor_read_error" : {"-127:30": 20, "31:40": 30, "41:120": 40}
                },
                "P2C": {

                        "fan_err": {
                                "tacho": {"-127:35": 20, "36:120": 30},
                                "present": {"-127:25": 20, "26:35": 30, "36:120": 40},
                                "direction": {"-127:25": 20, "26:35": 30, "36:120": 40}
                        },
                        "psu_err":  {
                                "present": {"-127:25": 20, "26:35": 30, "36:120": 40},
                                "direction": {"-127:35": 20, "36:120": 30}
                        },
                        "sensor_read_error" : {"-127:35": 20, "36:120": 30}
                }
        },
    1. Get the expected pwm base on the sensor error type and sensor temperature
    e.g. sensor error_type is fan_err_tacho, fan_dir is C2P
         it will get the data: "tacho": {"-127:30": 20, "31:40": 30, "41:120": 40} from above dmin table
         if temperature is 20 which is belong to the range of "-127:30", so the expected pwm is 20
    :param tc_config_dict: tc config dict
    :param temperature: temperature
    :param fan_dir: fan dir
    :param  sensor_err_type: sensor err type
    :return: expected_pwm
    """
    expected_pwm = None
    if sensor_err_type == "sensor_read_error":
        temp_to_pwm_table = tc_config_dict["dmin"][fan_dir][sensor_err_type]
    else:
        first_layer_err_key = sensor_err_type.split("_err_")[0] + "_err"
        second_layer_err_key = sensor_err_type.split("_err_")[1]
        temp_to_pwm_table = tc_config_dict["dmin"][fan_dir][first_layer_err_key][second_layer_err_key]

    for temp_range, pwm in temp_to_pwm_table.items():
        temp_min, temp_max = temp_range.split(":")
        if int(temp_min) <= int(temperature) // 1000 <= int(temp_max):
            expected_pwm = pwm

    if not expected_pwm:
        raise Exception(f"Could not find the corresponding pwm, temp_to_pwm_table:{temp_to_pwm_table},"
                        f" temperature:{temperature}, fan_dir:{fan_dir}, fan_err_type:{sensor_err_type}")

    logger.info(f"The pwm of {sensor_err_type} for temp {temperature} is :{expected_pwm}")
    return expected_pwm


def get_system_fan_str():
    return "C2P" if SENSOR_DATA["system_fan_dir"] == 0 else "P2C"


def get_fan_err_test_data(sensor_err_type):
    sensor_type = "fan"
    fan_sensor_total_num = SENSOR_DATA[sensor_type]["total_number"]
    if sensor_err_type != "fan_err_tacho":
        fan_sensor_total_num = SENSOR_DATA["fan_drwr_num"]
    sensor_index = random.randint(1, fan_sensor_total_num)
    sensor_temperature_file = SENSOR_DATA[sensor_type]["temperature_file_name_rule"]

    return sensor_type, sensor_index, sensor_temperature_file


def get_psu_err_test_data():
    sensor_type = "psu"
    sensor_index = random.randint(1, SENSOR_DATA[sensor_type]["total_number"])
    sensor_temperature_file = SENSOR_DATA[sensor_type]["temperature_file_name_rule"].format(sensor_index)

    return sensor_type, sensor_index, sensor_temperature_file


def get_sensor_read_error_test_data(cli_objects):
    sensor_read_error_test_sensors = ["ambient"]
    if SENSOR_DATA["cpu_pack"]["total_number"] > 0:
        sensor_read_error_test_sensors.append("cpu_pack")
    if SENSOR_DATA["module"]["index supporting sensor"]:
        # For IM enabled setup, 'module' sensor test are need be skipped as it created by kernel sysfs and
        # from user space we don't have permission to write it
        if cli_objects.dut.im.is_im_enabled():
            logger.info("skip module test due to IM is enabled")
        else:
            sensor_read_error_test_sensors.append("module")
    platform = cli_objects.dut.chassis.get_platform()
    if platform in TC_CONST.JULIET_PLATFORMS:
        for sensor in ["ambient", "module"]:
            if sensor in sensor_read_error_test_sensors:
                sensor_read_error_test_sensors.remove(sensor)
    sensor_type = random.choice(sensor_read_error_test_sensors)
    sensor_temperature_file = get_sensor_read_err_file(sensor_type)
    return sensor_type, sensor_temperature_file


def get_sensor_err_test_data(sensor_err_type, mock_sensor, tc_config_dict, cli_objects):
    sys_fan_dir_str = get_system_fan_str()
    if "fan_err" in sensor_err_type:
        sensor_type, sensor_index, sensor_temperature_file = get_fan_err_test_data(sensor_err_type)
    elif "psu_err" in sensor_err_type:
        sensor_type, sensor_index, sensor_temperature_file = get_psu_err_test_data()
    elif "sensor_read_error" == sensor_err_type:
        sensor_type, sensor_temperature_file = get_sensor_read_error_test_data(cli_objects)

    sensor_temperature_file = os.path.join(TC_CONST.HW_THERMAL_FOLDER, sensor_temperature_file)

    platform = cli_objects.dut.chassis.get_platform()
    if platform not in TC_CONST.JULIET_PLATFORMS:
        # For all sensor errors, we all use min(port_amb, fan_abm) as the key to get the expected pwm from dmin table
        temperature_port = mock_sensor.read_value(f"{TC_CONST.HW_THERMAL_FOLDER}/port_amb")
        temperature_fan = mock_sensor.read_value(f"{TC_CONST.HW_THERMAL_FOLDER}/fan_amb")
        logger.info(f"temperature_port :{temperature_port}, temperature_fan:{temperature_fan}")
        current_temp = min(temperature_port, temperature_fan)
    else:
        current_temp = TC_CONST.PWM_MAX

    expected_pwm = get_expected_pwm_by_temp_from_dmin_table(tc_config_dict, current_temp, sys_fan_dir_str,
                                                            sensor_err_type)
    if "sensor_read_error" == sensor_err_type:
        sensor_err_file = os.path.join(TC_CONST.HW_THERMAL_FOLDER, sensor_temperature_file)
    else:
        sensor_err_file = os.path.join(TC_CONST.HW_THERMAL_FOLDER,
                                       SENSOR_ERR_TEST_DATA[sensor_err_type].format(sensor_index))

    mock_value = gen_sensor_err_mock_value(sensor_err_type)

    return sensor_err_file, expected_pwm, mock_value


def gen_sensor_err_mock_value(sensor_err_type):

    sensor_err_mock_value_dict = {"fan_err_present": 0,
                                  "psu_err_present": 0,
                                  "fan_err_direction": 0 if SENSOR_DATA["system_fan_dir"] == 1 else 1,
                                  "psu_err_direction": 0 if SENSOR_DATA["system_fan_dir"] == 1 else 1,
                                  "fan_err_tacho": 10,
                                  "sensor_read_error": "invalid value"
                                  }
    if sensor_err_type in sensor_err_mock_value_dict:
        mock_value = sensor_err_mock_value_dict[sensor_err_type]
    else:
        raise Exception(f"{sensor_err_type} is not in {sensor_err_mock_value_dict}")

    logger.info(f"{sensor_err_type}: mock value is: {mock_value}")
    return mock_value


def get_sensor_read_err_file(sensor_type):
    if sensor_type == "module":
        sensor_index = random.choice(SENSOR_DATA[sensor_type]["index supporting sensor"])
        sensor_temperature_file = SENSOR_DATA[sensor_type]["temperature_file_name_rule"].format(sensor_index)
    elif sensor_type == "cpu_pack":
        sensor_temperature_file = SENSOR_DATA[sensor_type]["temperature_file_name_rule"]
    elif sensor_type == "ambient":
        sensor_temperature_file = 'port_amb'
    return sensor_temperature_file


def check_hw_thermal_control_status(cli_objects):
    assert cli_objects.dut.hw_mgmt.is_thermal_control_running(), "hw thermal control is not running"
    hw_tc_status = cli_objects.dut.hw_mgmt.show_thermal_control_status()
    warning_error_reg = r".*(ERROR - ).*"
    warning_or_error_log = re.findall(warning_error_reg, hw_tc_status, flags=re.DOTALL)
    if warning_or_error_log:
        raise Exception(f"tc status include warning or error log: {hw_tc_status}")


@retry(Exception, tries=3, delay=TC_CONST.PERIODIC_REPORT_TIME)
def check_periodic_report(dut):
    time_diff_tolerance = 0.05

    periodic_report_log_reg = r"(?P<timestamp>^[1-9]\d{3}-(0[1-9]|1[0-2])-(0[1-9]|[1-2][0-9]|3[0-1])\s+(20|21|22|23|[" \
                              r"0-1]\d):[0-5]\d:[0-5]\d),.* INFO - Thermal periodic report.*"
    thermal_periodic_report_log = dut.run_cmd(f'zgrep "Thermal periodic report" {TC_CONST.TC_LOG_FILE}')
    if thermal_periodic_report_log is None:
        logger.info(
            f" Periodic report is not available, please wait {TC_CONST.PERIODIC_REPORT_TIME} and then check again ")
        return False

    periodic_report_timestamp_list = []
    for line in thermal_periodic_report_log.split("\n"):
        report_timestamp_res = re.search(periodic_report_log_reg, line)
        if report_timestamp_res:
            periodic_report_timestamp_list.append(report_timestamp_res.groupdict()["timestamp"])
    if len(periodic_report_timestamp_list) < 2:
        logger.info("Thermal report item is less than 2")
        return False
    report_period_unmatch_expected_periods_list = []
    for timestamp_index in range(len(periodic_report_timestamp_list) - 1):
        t1 = datetime.datetime.fromisoformat(periodic_report_timestamp_list[timestamp_index])
        t2 = datetime.datetime.fromisoformat(periodic_report_timestamp_list[timestamp_index + 1])
        t_diff = t2 - t1
        if TC_CONST.PERIODIC_REPORT_TIME * (1 - time_diff_tolerance) <= t_diff.seconds <= TC_CONST.PERIODIC_REPORT_TIME * (
                1 + time_diff_tolerance):
            return True
        else:
            report_period_unmatch_expected_periods_list.append(t_diff)

    assert False, f"TC report period is not the expected one :{TC_CONST.PERIODIC_REPORT_TIME}, " \
        f"actual period is {report_period_unmatch_expected_periods_list}"


def is_support_new_hw_tc(cli_objects, is_simx):
    platform = cli_objects.dut.chassis.get_platform()
    if is_simx:
        support_new_hw_tc_platform_list = ["x86_64-nvidia_sn4280_simx-r0", "x86_64-mlnx_msn4700_simx-r0"]
        if platform not in support_new_hw_tc_platform_list:
            logger.info(f'simx platform {platform} does not support new hw tc')
            return False

    not_support_new_hw_tc_platform_list = ["x86_64-nvidia_sn5810_ld-r0"]
    if platform in not_support_new_hw_tc_platform_list:
        logger.info(f'platform {platform} does not support new hw tc')
        return False

    hw_version = cli_objects.dut.hw_mgmt.get_hw_version()
    base_hw_version = "7.0030.0951"
    logger.info(f'hw_version: {hw_version}, base hw_version:{base_hw_version}')
    if not hw_version or not is_ver1_greater_or_equal_ver2(hw_version, base_hw_version):
        logger.info(f'hw_version：{hw_version} not support new hw tc')
        return False

    return True
