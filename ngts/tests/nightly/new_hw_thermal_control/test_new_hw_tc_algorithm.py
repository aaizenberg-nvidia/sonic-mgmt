import allure
import copy
import logging
import pytest
import random
import os
import time
import operator
import re
from retry.api import retry_call


from ngts.helpers.new_hw_thermal_control_helper import TC_CONST, MockSensors, SENSOR_DATA, \
    verify_pwd_and_rpm_are_expected_value, get_sensor_temperature_file_name, get_sensor_err_test_data, get_pwm, \
    get_temperature_digit, update_effective_val_min_max, SENSOR_ERR_TEST_DATA, compare_pwd_with_expected_value, \
    verify_rpm_is_expected_value
from ngts.common.checkers import is_ver1_greater_or_equal_ver2

logger = logging.getLogger()


class TestNewTc:

    @pytest.fixture(autouse=True)
    def setup_param(self, topology_obj, engines, cli_objects, interfaces, players):
        self.cli_objects = cli_objects
        self.dut_engine = engines.dut
        self.topology_obj = topology_obj
        self.blacklisted_modules = []

    def _wait_tc_loop_resume(self, retries=30):
        """Wait for the hw-management TC loop to exit suspend mode.

        After the suspend file is removed, the TC loop needs up to one
        iteration cycle to detect the change and drive PWM back to
        sensor-driven levels.  Poll until PWM drops below 100 %.
        """
        for _ in range(retries):
            pwm = get_pwm(MockSensors(self.dut_engine, self.cli_objects))
            if pwm < TC_CONST.PWM_MAX_PERCENT:
                logger.info(f"TC loop resumed, PWM={pwm}")
                return
            time.sleep(1)
        logger.warning(f"TC loop did not resume within {retries} retries, PWM still at 100%")

    def _backup_thermal_files(self):
        """Backup asic/module thermal values before stopping thermal services.

        Stopping thermalctld deletes these sysfs files; tests recreate them afterward
        so the TC loop can keep reading sensors while the test mocks temperatures.
        """
        thermal_folder = TC_CONST.HW_THERMAL_FOLDER
        thermal_file_list = self.dut_engine.run_cmd(
            f"sudo ls {thermal_folder} 2>/dev/null | "
            f"grep -E '^(asic|module[0-9]+_temp_(input|fault|emergency|crit))$' || true"
        ).strip()
        backups = {}
        for fname in thermal_file_list.split('\n'):
            fname = fname.strip()
            if not fname:
                continue
            file_path = os.path.join(thermal_folder, fname)
            value = self.dut_engine.run_cmd(f"sudo cat {file_path} 2>/dev/null || true").strip()
            if value.isdigit():
                backups[file_path] = value
        assert backups, "No thermal files found to backup before stopping services"
        logger.info(f"Backed up {len(backups)} thermal files before stopping services")
        return backups

    def _thermal_file_exists(self, file_path):
        check = self.dut_engine.run_cmd(
            f"test -e {file_path} && echo EXISTS || echo MISSING"
        ).strip()
        return "MISSING" not in check

    def _ensure_thermal_files_exist(self, file_backups):
        """Wait for stop cleanup to delete sysfs files, recreate them, and verify.

        Stopping thermalctld can delete sysfs files asynchronously.  Recreating
        before that cleanup finishes can be undone by a late delete, leaving the
        TC loop without readable sensor files.  Wait until every backed-up file
        is gone, then recreate them and verify they stay present.
        """

        def _restore_files():
            present_files = [
                file_path for file_path in file_backups
                if self._thermal_file_exists(file_path)
            ]
            assert not present_files, (
                "Temperature files still present after stopping thermal services, "
                f"waiting for cleanup to finish: {present_files}")

            for file_path, backup_value in file_backups.items():
                logger.info(
                    f"File {file_path} was deleted after stopping thermalctld, "
                    f"re-creating with value {backup_value}"
                )
                self.dut_engine.run_cmd(f"sudo touch {file_path}")
                self.dut_engine.run_cmd(f"sudo chown admin {file_path}")
                self.dut_engine.run_cmd(f"sudo echo {backup_value} > {file_path}")

            missing_files = [
                file_path for file_path in file_backups
                if not self._thermal_file_exists(file_path)
            ]
            assert not missing_files, \
                f"Temperature files still missing after recreate: {missing_files}"

        retry_call(_restore_files, exceptions=AssertionError, tries=10, delay=1)

    def _stop_thermal_services(self):
        """
        Stop services that write asic/module temperature sysfs files, so that
        mocked values are not overwritten. Only needed for asic/module sensors.

        thermalctld creates a suspend file on stop — we remove it via
        resume_thermal_control() and wait for the TC loop to stabilize so
        PWM is back to sensor-driven levels before the test begins.

        After thermalctld is stopped, the asic/module sysfs files will be deleted.
        The hw-management TC loop (hw-management-tc.service)
        still runs independently and would fail to read the missing file,
        triggering sensor_read_error protection that locks PWM at 100%.
        To prevent this, we check immediately after stopping thermalctld
        and re-create the files with backed-up temperature values if they were
        deleted.

        Must NOT be called when testing cpu_pack/ambient sensors: stopping
        thermalctld causes module temp files to go stale, triggering TC loop
        total_err_cnt >= 2 protection which locks PWM at 100%.
        """
        file_backups = self._backup_thermal_files()

        # suspend thermal control to prevent sensor_read_error protection from locking PWM at 100%
        self.cli_objects.dut.hw_mgmt.suspend_thermal_control()

        # Blacklist all module sensors to prevent TC loop from detecting
        # "missing" module temp files after thermalctld is stopped.
        module_files = self.dut_engine.run_cmd(
            f"ls -1 {TC_CONST.HW_THERMAL_FOLDER}/module*_temp_input 2>/dev/null"
        ).strip().splitlines()
        self.blacklisted_modules = []
        cmds = []
        for temp_file in module_files:
            if temp_file == "":
                pytest.fail("No module temp files found")
            module_name = os.path.basename(temp_file.strip()).replace("_temp_input", "")
            bl_file = os.path.join(TC_CONST.HW_THERMAL_FOLDER, f"{module_name}_blacklist")
            cmds.append(f"sudo touch {bl_file} && sudo chown admin {bl_file} && sudo echo 1 > {bl_file}")
            self.blacklisted_modules.append(bl_file)
        if cmds:
            self.dut_engine.run_cmd(" && ".join(cmds))

        self.dut_engine.run_cmd("docker exec pmon supervisorctl stop thermalctld")
        self.dut_engine.run_cmd("sudo systemctl stop hw-management-sync")
        self.dut_engine.run_cmd("sudo systemctl stop hw-management-thermal-updater")

        self._ensure_thermal_files_exist(file_backups)

        self.cli_objects.dut.hw_mgmt.resume_thermal_control()
        self._wait_tc_loop_resume()

    def _start_thermal_services(self):
        """Restart services stopped by _stop_thermal_services()."""
        # Clean up blacklist files
        bl_files = getattr(self, 'blacklisted_modules', [])
        if bl_files:
            self.dut_engine.run_cmd(f"sudo rm -f {' '.join(bl_files)}")
        self.blacklisted_modules = []

        self.dut_engine.run_cmd("sudo systemctl start hw-management-sync")
        self.dut_engine.run_cmd("sudo systemctl start hw-management-thermal-updater")
        self.dut_engine.run_cmd("docker exec pmon supervisorctl start thermalctld")

    def _run_temperature_sweep(self, mock_sensor, tc_config_dict, sensor_type, file_path_list):
        """
        Mock sensor temperature from val_min to val_max and back, verifying PWM and RPM at each step.
        """
        dev_parameters_name = SENSOR_DATA[sensor_type]['dev_parameters_name']

        update_effective_val_min_max(mock_sensor, tc_config_dict, sensor_type, file_path_list[0])

        temperature_max = get_temperature_digit(tc_config_dict['dev_parameters'][dev_parameters_name]['val_max'])
        temperature_min = get_temperature_digit(tc_config_dict['dev_parameters'][dev_parameters_name]['val_min'])
        logger.info(
            f"\n sensor_type:{sensor_type}, \n sensor_path: {file_path_list},"
            f" \n dev_parameters_name :{dev_parameters_name},"
            f" \n temp max:{temperature_max}, \ntemp min: {temperature_min}")

        initial_pwm = get_pwm(mock_sensor)
        logger.info(f"Initial PWM before mocking: {initial_pwm}")

        def mock_temp_and_check(file_path, temperature, initial_pwm):
            mock_sensor.mock_temperature(file_path, temperature)
            verify_pwd_and_rpm_are_expected_value(mock_sensor, tc_config_dict, sensor_type, temperature,
                                                  initial_pwm=initial_pwm)

        tested_temperature_file = file_path_list[0]
        with allure.step(f'Mock {tested_temperature_file} to  temperature_min:{temperature_min}'):
            mock_temp_and_check(tested_temperature_file, temperature_min, initial_pwm)

        if sensor_type == "ambient":
            with allure.step(f'For ambient, Mock {file_path_list[1]} to  temperature_max:{temperature_max}'):
                mock_sensor.mock_temperature(file_path_list[1], temperature_max)

        temp_change_step = 10000
        temperature = temperature_min + temp_change_step
        with allure.step(
                f'Mock temperature of {tested_temperature_file} increase from {temperature_min} to '
                f'{temperature_max} with step {temp_change_step}'):
            while temperature <= temperature_max:
                with allure.step(f'mock {sensor_type} temperature increase to {temperature}'):
                    mock_temp_and_check(tested_temperature_file, temperature, initial_pwm)
                    temperature += temp_change_step

        temperature = temperature_max - temp_change_step
        with allure.step(
                f'Mock temperature of {tested_temperature_file} decrease from {temperature_max} to '
                f'{temperature_min} with step {temp_change_step}'):
            while temperature >= temperature_min:
                with allure.step(f'mock {sensor_type} temperature decrease to {temperature}'):
                    mock_temp_and_check(tested_temperature_file, temperature, initial_pwm)
                    temperature -= temp_change_step

    @allure.title('test temperature sweep')
    def test_temperature_sweep(self, request, get_dut_supported_sensors_and_tc_config, platform_params):
        """
        This test is to verify temperature sweep
        1. Mock tested sensor temperature to val_min/10000-1. Wait poll_time secs
        2. Mock temperature increase
            Increase sensor temperature value with step 10
            Calculate the expected PWM with below formula:
            expected_pwm = pwm_min + ((new_temp - val_min)/(val_max-val_min)) * (pwm_max - pwm_min)
            Wait poll_time secs, and get current system PWM by reading /var/run/hw-management/thermal/pwm1
            Check current_system_pwm == expected_pwm within +-1% tolerance
            Check FAN speed is set to correct range
            Iterate the above steps until temperature is over val_max
        3. Mock temperature decrease
            decrease sensor temperature value with step 10
            Calculate the expected PWM with below formula:
            expected_pwm = pwm_min + ((new_temp - val_min)/(val_max-val_min)) * (pwm_max - pwm_min)
            Wait poll_time secs, and get current system PWM
            Check current_system_pwm == expected_pwm within +-1% tolerance
            Check FAN speed is set to correct range
            Iterate the above steps until temperature is under val_min
        4. Repeat step 1~ step 5 for following sensors or randomly select one sensor to test
           (Need to check if dut includes this sensor. if no, skip testing this sensor):
            asic
            cpu-pack
            voltmon1_temp_input
            module{X} — when val_max_clamp is configured, sweeps with mocked EEPROM max above and below it
            ambient
            Gearbox
        """
        sensor_temperature_test_list, tc_config_dict_orig = get_dut_supported_sensors_and_tc_config
        sensor_type = request.config.getoption("--sensor_type")
        if sensor_type == "all":
            tested_sensors = sensor_temperature_test_list
        elif sensor_type in sensor_temperature_test_list:
            tested_sensors = [sensor_type]
        else:
            tested_sensors = [random.choice(sensor_temperature_test_list)]

        logger.info(f"The sensors tested are: {tested_sensors}")

        # For IM enabled setup, 'module' and 'asic' sensor test are need be skipped as it created by kernel sysfs and
        # from user space we don't have permission to write it
        if tested_sensors[0] in ['module', 'asic'] and self.cli_objects.dut.im.is_im_enabled():
            pytest.skip(f"Skip mock temperature for sensor {tested_sensors} as it is not supported in IM enabled setup")

        for sensor_type in tested_sensors:
            # Stop services that would overwrite mocked asic/module temp files.
            # cpu_pack/ambient are maintained by kernel drivers — no services
            # need stopping for them. See _stop_thermal_services() docstring.
            need_stop_services = sensor_type in ("asic", "module")
            try:
                if need_stop_services:
                    self._stop_thermal_services()
                with MockSensors(self.dut_engine, self.cli_objects) as mock_sensor:
                    file_path_list = get_sensor_temperature_file_name(sensor_type, platform_params)

                    dev_parameters_name = SENSOR_DATA[sensor_type]['dev_parameters_name']
                    sensor_config = tc_config_dict_orig['dev_parameters'][dev_parameters_name]

                    if sensor_type == "module" and "val_max_clamp" in sensor_config:
                        temp_crit_path = file_path_list[0].replace("_temp_input", "_temp_crit")
                        val_max_clamp = int(sensor_config['val_max_clamp'])
                        val_max_offset = sensor_config.get("val_max_offset", 0)
                        module_clamp_scenarios = [
                            ("clamp_applies", TC_CONST.MODULE_EEPROM_TEMP_MAX_ABOVE_CLAMP),
                            ("clamp_not_applied", TC_CONST.MODULE_EEPROM_TEMP_MAX_BELOW_CLAMP)
                        ]
                        for scenario_name, mocked_eeprom_max in module_clamp_scenarios:
                            expected_val_max = min(mocked_eeprom_max, val_max_clamp) + val_max_offset
                            scenario_log = (
                                f"Module val_max_clamp {scenario_name}: mock {temp_crit_path}="
                                f"{mocked_eeprom_max}, expected val_max={expected_val_max}"
                            )
                            logger.info(scenario_log)
                            with allure.step(scenario_log):
                                tc_config_scenario = copy.deepcopy(tc_config_dict_orig)
                                mock_sensor.mock_value(temp_crit_path, mocked_eeprom_max)
                                update_effective_val_min_max(mock_sensor, tc_config_scenario, sensor_type, file_path_list[0])
                                actual_val_max = get_temperature_digit(tc_config_scenario['dev_parameters'][dev_parameters_name]['val_max'])
                                assert actual_val_max == expected_val_max, (
                                    f"Effective module val_max {actual_val_max} != expected {expected_val_max}")
                                self._run_temperature_sweep(
                                    mock_sensor, tc_config_scenario, sensor_type, file_path_list)
                    else:
                        tc_config_dict = copy.deepcopy(tc_config_dict_orig)
                        self._run_temperature_sweep(
                            mock_sensor, tc_config_dict, sensor_type, file_path_list)
            finally:
                if need_stop_services:
                    self._start_thermal_services()

    @pytest.mark.parametrize("sensor_err_type", SENSOR_ERR_TEST_DATA.keys())
    @allure.title('test sensor errors')
    def test_sensor_errors(self, get_dut_supported_sensors_and_tc_config, sensor_err_type, loganalyzer):
        """
        This test is to verify sensor errors behaviors
        1. Mock the temperature for tested sensors to a minimum(30c)
        2. Inject errors by writing err values to corresponding static files
        3. Check expected PWM is changed to the value from Dmin table. We can get the Dim table from /var/run/hw-management/config/tc_config.json
        4. Check FAN speed is set to correct range
        5. Repeat step 1 ~ step 4 for below scenarios
            FAN present err (FAN not present)
              e.g. echo 0 >> /run/hw-management/thermal/fan1_status
            FAN direction err (One or more FANs have opposite dir)
              e.g. echo 0 >> /run/hw-management/thermal/fan1_dir
            FAN tacho err (Current FAN RPM is not correct and does not correspond to expected)
              e.g. echo 10 >> /run/hw-management/thermal/fan1_speed_set
            PSU present err (PSU not present)
              e.g. echo 0 >> /run/hw-management/thermal/psu1_status
            PSU FAN direction err (PSU FAN dir is opposite to system FAN)
              e.g. echo 0 >> /run/hw-management/thermal/psu1_fan_dir
            Thermal sensor reading error (cpu_pack, amb, module) file missing
              remove the corresponding files
            Thermal sensor reading error (cpu_pack, amb, module) file including incompatible value (reading null/none/wrong format numeric value from the file)
              e.g. echo an incompatible value into the corresponding files
        """
        _, tc_config_dict = get_dut_supported_sensors_and_tc_config

        with allure.step(f"Mock {sensor_err_type}"):
            with MockSensors(self.dut_engine, self.cli_objects) as mock_sensor:
                if sensor_err_type.startswith("psu_err") and SENSOR_DATA["psu"]["total_number"] < 2:
                    pytest.skip("Only one psu, skipping this test")
                sensor_err_file, expected_pwm, mock_value = get_sensor_err_test_data(
                    sensor_err_type, mock_sensor, tc_config_dict, self.cli_objects)
                if sensor_err_type == "psu_err_present" and loganalyzer:
                    psu_status_match = re.search(r"psu(\d+)_status$", sensor_err_file)
                    assert psu_status_match, f"Unexpected PSU status file path: {sensor_err_file}"
                    psu_id = psu_status_match.group(1)
                    expected_psu_absence_log = rf".*ERR pmon#psud\[\d+\]: PSU {psu_id} is not present.*"
                    for analyzer in loganalyzer.values():
                        analyzer.ignore_regex.append(expected_psu_absence_log)
                sensor_read_error_type = None
                if "sensor_read_error" == sensor_err_file:
                    sensor_read_error_type = random.choice(SENSOR_ERR_TEST_DATA["sensor_read_error"])
                    logging.info(f"sensor read error type is {sensor_read_error_type}")
                if "missing_file" == sensor_read_error_type:
                    with allure.step(f"Mock remove {sensor_err_file}"):
                        mock_sensor.remove_file(sensor_err_file)
                else:
                    with allure.step(f"Mock {sensor_err_file} with {mock_value}"):
                        mock_sensor.mock_value(sensor_err_file, mock_value)
                compare_pwd_with_expected_value(mock_sensor, expected_pwm, operator.ge)
                verify_rpm_is_expected_value(mock_sensor, tc_config_dict)

    @allure.title('test sensor blacklist')
    def test_sensor_blacklist(self, get_dut_supported_sensors_and_tc_config):
        """
        This test is to verify sensor blacklist function
        1. Add asic sensor to blacklist
           Touch /var/run/hw-management/thermal/asic_blacklist
           echo 1 > /var/run/hw-management/thermal/asic_blacklist
        2. Mock asic temperature to val_max
        3. Check PWM is not PWM_MAX(100).
        """
        _, tc_config_dict = get_dut_supported_sensors_and_tc_config
        # Due to bug of https://redmine.mellanox.com/issues/3537782, change asic_blacklist to asic1_blacklist
        asic_blacklist_file = f"{TC_CONST.HW_THERMAL_FOLDER}/asic1_blacklist"
        try:
            with MockSensors(self.dut_engine, self.cli_objects) as mock_sensor:
                pwm_before_adding_blacklist = get_pwm(mock_sensor)
                if "asic" in list(tc_config_dict["dev_parameters"].keys()):
                    asci_dev_param_info = tc_config_dict["dev_parameters"]["asic"]
                else:
                    pytest.skip('"asic" sensor does not exist')
                pwm_max = asci_dev_param_info["pwm_max"]
                with allure.step(f'Verify pwm {pwm_before_adding_blacklist} before adding blacklist is smaller than max pwm {pwm_max}'):
                    if pwm_before_adding_blacklist >= pwm_max:
                        pytest.skip(
                            f"pwm {pwm_before_adding_blacklist} before adding asic to blacklist is ge max pwm {pwm_max},"
                            f" skipping this test")
                with allure.step(f'Create {asic_blacklist_file}, and enable it'):
                    self.dut_engine.run_cmd(f"sudo touch {asic_blacklist_file}")
                    self.dut_engine.run_cmd(f"sudo chown admin {asic_blacklist_file}")
                    self.dut_engine.run_cmd(f"sudo echo 1 > {asic_blacklist_file}")
                with allure.step(f'Mock asic temperature to val_max'):
                    with MockSensors(self.dut_engine, self.cli_objects) as mock_sensor:
                        poll_time = asci_dev_param_info["poll_time"]
                        asci_sensor_file = os.path.join(TC_CONST.HW_THERMAL_FOLDER, "asic")
                        mock_temp = get_temperature_digit(asci_dev_param_info["val_max"])
                        logger.info(f"mock_temperature: {mock_temp} \n poll_time:{poll_time} \n pwm_max:{pwm_max}\n")
                        mock_sensor.mock_temperature(asci_sensor_file, mock_temp)

                        time.sleep(poll_time + TC_CONST.PWM_GROW_TIME)
                        pwm_curr = get_pwm(mock_sensor)
                        assert pwm_curr < pwm_max, f"" \
                            f"sensor blacklist doesn't work, pwm_curr:{pwm_curr} >= pwm_max {pwm_max}," \
                            f"pwm_before_adding_blacklist is:{pwm_before_adding_blacklist} "
        except Exception as err:
            raise err
        finally:
            with allure.step(f'Remove {asic_blacklist_file}'):
                self.dut_engine.run_cmd(f"sudo rm -f {asic_blacklist_file}")

    @allure.title('test check tc_config link')
    def test_check_tc_config_link(self, platform_params):
        """
        This tests is to check tc_config is linked to the correct file
        """
        # For some platforms, tc_config is linked to the file with the specific hwsku name
        # The below map includes these info

        with allure.step("Get sku info"):
            hwsku = platform_params.hwsku
            reg_hwsku = r'(ACS|Mellanox)-(?P<hwsku>\w+).*'
            sku_res = re.search(reg_hwsku, hwsku)
            if sku_res:
                expected_hwsku_in_tc_config_file = sku_res.groupdict()['hwsku']
            else:
                assert False, f" Does not find sku name in {platform_params.hwsku}"
            expected_hwsku_in_tc_config_file = expected_hwsku_in_tc_config_file.replace('_', '')

        # In case platform name exists in NO_TC_CONFIG_LINK_PLATFORMS list, it means there is no link to TC_CONFIG_FILE,
        # and therefore just verifying config file contains the hwsku name. otherwise checking the tc_config link.
        if expected_hwsku_in_tc_config_file in TC_CONST.PLATFORMS_WITHOUT_TC_CONFIG_LINK:
            with allure.step(f"verify config file contains the hwsku name"):
                tc_config_content = self.dut_engine.run_cmd(f'sudo cat {TC_CONST.TC_CONFIG_FILE}')
                assert expected_hwsku_in_tc_config_file.lower() or \
                    expected_hwsku_in_tc_config_file.replace('QM', 'Q').lower() in tc_config_content, \
                    f"tc_config file should contains hwsku name: {expected_hwsku_in_tc_config_file.lower()}"

        # RM issue 3769500
        current_hw_version = self.cli_objects.dut.hw_mgmt.get_hw_version()
        base_hw_version = "7.0030.2013"
        logger.info(f'current hw_version: {current_hw_version}, base hw_version:{base_hw_version}')
        if is_ver1_greater_or_equal_ver2(current_hw_version, base_hw_version):
            with allure.step(f"Use {TC_CONST.HW_MGMT_THERMAL_FOLDER}/tc_config_*.json file to verify hwsku"):
                cmd = f'ls {TC_CONST.HW_MGMT_THERMAL_FOLDER} | grep -i {expected_hwsku_in_tc_config_file.lower()}.json'
                output = self.dut_engine.run_cmd(cmd)
                assert expected_hwsku_in_tc_config_file.lower() or \
                    expected_hwsku_in_tc_config_file.replace('QM', 'Q').lower() in output.lower(), \
                    "SKU not found in the expected file."
        else:
            with allure.step("Use tc_config link to verify hwsku"):
                tc_config_link = self.dut_engine.run_cmd(f'sudo readlink {TC_CONST.TC_CONFIG_FILE}')
                assert expected_hwsku_in_tc_config_file.lower() in tc_config_link.lower(), \
                    f"tc_config is linked to wrong file. tc_config link:{tc_config_link}, " \
                    f"platform sku: {platform_params.hwsku}"
