import allure
import logging
import pytest
from ngts.performance_tests.lossy_lossless.lossy_lossless_scenario_6_7.conftest import get_conf_args, get_lossy_lossless_scenario_6_7_traffic
from ngts.helpers.performance.performance_db_helpers import get_perf_test_name
from ngts.helpers.performance.performance_setup_helpers import (ValidationConfig, run_traffic, run_validation)
from ngts.constants.performance_constants import PerfConsts, ValidationConsts, BwFairnessThreshold
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
logger = logging.getLogger()

LOSSY_LOSSLESS_SCENARIOS_LIST = ["scenario_6a", "scenario_6b", "scenario_7b", "scenario_7c"]


@pytest.mark.parametrize("basic_setup_configuration", LOSSY_LOSSLESS_SCENARIOS_LIST, indirect=True)
class TestLossyLosslessScenario6and7:

    @pytest.fixture(autouse=True)
    def setup(self, players, engines, cli_objects, power_thresholds_by_chip_type, chip_type, basic_setup_configuration):
        self.players = players
        self.engines = engines
        self.cli_objects = cli_objects
        self.dut_engine = engines['dut']
        self.cli_object = self.cli_objects['dut']
        self.scenario = "lossy_lossless"
        self.power_thresholds_by_chip_type = power_thresholds_by_chip_type
        self.chip_type = chip_type
        self.scenario_name, self.conf_args = basic_setup_configuration
        self.traffic_jsons = get_lossy_lossless_scenario_6_7_traffic(self.cli_objects, self.conf_args, self.scenario_name)

    def test_scenario_6_and_7(self, request):
        test_name = get_perf_test_name(request)
        counters_to_ignore = ['tx_ecn_marked_tc_3', 'tx_ecn_marked_tc_4']
        test_custom_counters_to_ignore = self.conf_args.get(ValidationConsts.IGNORE_COUNTER_LIST, [])
        counters_to_ignore.extend(test_custom_counters_to_ignore)
        with allure.step(f"Run traffic on all the ports:"):
            run_traffic(self.players, self.scenario, self.traffic_jsons)
        with allure.step(f"Verifying the traffic for lossy lossless {self.scenario_name}"):
            bw_threshold = self.conf_args[PerfConsts.BW_THRESHOLD]
            config = ValidationConfig(players=self.players, test_name=test_name,
                                      scenario=self.scenario,
                                      chip_type=self.chip_type,
                                      bw_threshold=bw_threshold,
                                      bw_fairness_threshold_per_port_group=BwFairnessThreshold.get_bw_fairness_threshold_per_port_group(bw_threshold),
                                      tc_occ_threshold=None,
                                      run_validate_counters=True,
                                      power_threshold=self.power_thresholds_by_chip_type,
                                      ignore_counter_list=counters_to_ignore,
                                      skip_first_counters_iteration=True)
            run_validation(config)
