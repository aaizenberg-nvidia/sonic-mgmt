"""
This test verifies that the execution time of a set of SONiC CLI commands (config and show)
does not exceed a pre-defined per-platform threshold.

For each tested command, the test profiles its execution on the DUT via cProfile, averages
the measured time over several attempts, and compares the average against a threshold stored
in expected_results.json, allowing a per-test deviation.

show techsupport is handled separately from the rest of the commands: it runs a single attempt
(dump generation is slow) with a tighter allowed deviation, and the generated dump file is
removed from the DUT after each measurement.

Measured results are additionally stored per test case under /tmp/ for later collection on sonic-mgmt runner.

For a new platform/system, expected_results.json has no entry yet - measure the baseline
runtimes on that platform and add a threshold for it before running this test there.
"""

import os
import pytest
import json
import re
import logging
import time
import allure

from ngts.common.checkers import verify_up_deviation
from ngts.constants.constants import SonicConst

logger = logging.getLogger()

EXPECTED_RESULTS_JSON = 'expected_results.json'
EXPECTED_RESULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), EXPECTED_RESULTS_JSON)
BASE_PROFILE_CMD = 'sudo python3 -m cProfile -s tottime {} {} 2>&1| grep -E "seconds|\\.tar"'

# use the 'show' word in the method name of show commands. Like 'show_ifaces_status'
TESTED_CMD_METHODS = {'add_del_vlan': {'add_vlan': 'vlan add 1234',
                                       'del_vlan': 'vlan del 1234'},
                      'add_del_route': {'add_route': 'route add prefix 2.3.4.0/24 nexthop 69.0.0.5',
                                        'del_route': 'route del prefix 2.3.4.0/24 nexthop 69.0.0.5'},
                      'add_del_po': {'add_po': 'portchannel add PortChannel01',
                                     'del_po': 'portchannel del PortChannel01'},
                      'show_ifaces_status': {'show_ifaces_status': 'interfaces status'},
                      'show_iface_tr_eeprom': {'show_iface_tr_eeprom': 'interface transceiver eeprom Ethernet0 -d'},
                      'show_platform': {'show_pl_fan': 'platform fan',
                                        'show_pl_psustatus': 'platform psustatus',
                                        'show_pl_firmware': 'platform firmware',
                                        'show_pl_pcieinfo': 'platform pcieinfo',
                                        'show_pl_ssdhealth': 'platform ssdhealth',
                                        'show_pl_syseeprom': 'platform syseeprom',
                                        'show_pl_temperature': 'platform temperature'},
                      'show_system_health': {'show_sys_health_summary': 'system-health summary'},
                      'show_techsupport': {'show_techsupport': 'techsupport --silent --since "2 hours ago"'}}
TESTED_METHODS = list(TESTED_CMD_METHODS.keys())
METHOD_ATTEMPTS = 5
TECHSUPPORT_ATTEMPTS = 1
ALLOWED_DEVIATION = 0.5
TECHSUPPORT_ALLOWED_DEVIATION = 0.15


class TestRuntime:
    """
    The tests will verify that the execution time of certain commands is less than a pre-defined threshold.
    It covers the following issue: RM 3169962.
    The predefined thresholds are based on measurements done on 202012.329-ed728abb0_Internal.
    """
    @pytest.fixture(autouse=True)
    def setup(self, engines, expected_results_data):
        self.engines = engines
        self.results = {}
        self.expected_results_data = expected_results_data

    @pytest.fixture(autouse=True)
    def expected_results_data(self, platform_params):
        try:
            with open(EXPECTED_RESULTS_PATH) as expected_results_file:
                expected_results_data = json.load(expected_results_file)
            expected_results_file.close()
            platform_info = platform_params.hwsku.split('-')
            platform = platform_info[1]
            if 'SN2700' in platform_info or 'MSN2700' in platform_info:
                platform = 'SN2700'
                if 'A1' not in platform_info:
                    platform = 'SN2700_A0'

            return expected_results_data[platform]
        except Exception as e:
            raise AssertionError(e)

    @pytest.mark.parametrize("tested_method", TESTED_METHODS)
    def test_runtime(self, request, tested_method):
        self.method_dic = TESTED_CMD_METHODS[tested_method]
        self.set_test_params(tested_method)
        try:
            self.get_average_result()
            self.validate_results()
        finally:
            self.store_results(request, tested_method)

    def set_test_params(self, tested_method):
        if tested_method == 'show_techsupport':
            self.attempts = TECHSUPPORT_ATTEMPTS
            self.allowed_deviation = TECHSUPPORT_ALLOWED_DEVIATION
        else:
            self.attempts = METHOD_ATTEMPTS
            self.allowed_deviation = ALLOWED_DEVIATION

    def get_average_result(self):
        for _ in range(self.attempts):
            for method_name, method_cmd in self.method_dic.items():
                profile_time = self.get_profile_result(method_name, method_cmd)
                self.update_results(method_name, profile_time)
        self.calculate_average()

    def get_profile_result(self, method_name, method_cmd):
        profile_cmd = self.get_profile_cmd(method_name, method_cmd)
        output = self.engines.dut.run_cmd(profile_cmd)
        executed_time = float(re.search(r"(\d+.\d+) seconds", output, re.IGNORECASE).group(1))
        if method_name == 'show_techsupport':
            self.cleanup_techsupport_dump(output)
        return executed_time

    @staticmethod
    def get_profile_cmd(method_name, method_cmd):
        cmd_area = SonicConst.SHOW if 'show' in method_name else SonicConst.CONFIG
        cmd = BASE_PROFILE_CMD.format(cmd_area, method_cmd)
        return cmd

    def update_results(self, method_name, profile_time):
        if method_name in self.results:
            self.results[method_name] = self.results[method_name] + profile_time
        else:
            self.results.update({method_name: profile_time})

    def calculate_average(self):
        round_value = 3
        for method_name in self.method_dic.keys():
            average_time = round(self.results[method_name] / self.attempts, round_value)
            logger.info(f"The average time of the method {method_name} is {average_time} seconds")
            self.results[method_name] = average_time

    def validate_results(self):
        all_failures = []
        with allure.step('Validate results'):
            for method, current_result in self.results.items():
                with allure.step(f'Validate results of method {method}'):
                    expected_result = self.expected_results_data[method]
                    logger.info(f"Compare the current result {current_result} of method {method} to the"
                                f" threshold {expected_result} with allowed deviation of {self.allowed_deviation * 100}%")
                    try:
                        verify_up_deviation(current_result, expected_result, self.allowed_deviation)
                    except AssertionError as error:
                        all_failures.append(f"Method {method}: {error}")

        with allure.step('Fail the test if there are any failures'):
            assert not all_failures, "\n".join(all_failures)

    def cleanup_techsupport_dump(self, output):
        match = re.search(r'(\S+\.tar(?:\.gz)?)', output)
        if match:
            tar_file = match.group(1)
            logger.info(f"Removing generated techsupport dump: {tar_file}")
            self.engines.dut.run_cmd(f"sudo rm -f {tar_file}")

    def store_results(self, request, tested_method):
        filename = os.path.join('/tmp/', f'{request.node.originalname}_{tested_method}')
        logger.info(f"Store test results to {filename}")
        with open(filename, 'w') as test_report_file_obj:
            json.dump(self.results, test_report_file_obj)
