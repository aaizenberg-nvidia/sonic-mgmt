import allure
import logging
import pytest
import time

from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.helpers.general_helper import get_pytest_test_name
from ngts.helpers.performance.performance_setup_helpers import (ValidationConfig, run_traffic, run_validation,
                                                                get_topology_obj)
from ngts.performance_tests.lossy_lossless.lossy_lossless_basic_scenarios_1_2_3.conftest import set_allure_lossy_lossless_title
from ngts.constants.performance_constants import PerfConsts, SPCXRAConsts, BwFairnessThreshold
from ngts.helpers.performance.burst_config import BurstConfig, BurstConfigConsts
from ngts.performance_tests.lossy_lossless.conftest import (get_lossy_lossless_basic_traffic,
                                                            get_lossy_lossless_burst_traffic)

logger = logging.getLogger()

PACKET_SIZE_LIST = PerfConsts.PACKET_SIZE_LIST

LOSSY_LOSSLESS_SCENARIOS = [("lossy_lossless_scenario_1", 0, 8, None),
                            ("lossy_lossless_scenario_2", 8, 0, None),
                            ("lossy_lossless_scenario_3a", 4, 4, None),
                            ("lossy_lossless_scenario_3b", 2, 6, None)]
# Example how to use burst, Not running in regression
BURST_LOSSY_SCENARIO = [("lossy_lossless_scenario_1_burst", 0, 8,
                         BurstConfig(cir=1, cbs=10, ebs=1, packet_size=PerfConsts.PACKET_SIZE_256))]
LOSSY_LOSSLESS_SCENARIO_IDS = [f"{scenario_name}-{num_lossy_packets}-{num_lossless_packets}"
                               for scenario_name, num_lossy_packets, num_lossless_packets, _
                               in LOSSY_LOSSLESS_SCENARIOS]


class TestLossyLossless:
    @pytest.fixture(autouse=True)
    def setup(self, players, engines, power_thresholds_by_chip_type, conf_args, chip_type, is_ipv6):
        self.topology_obj = get_topology_obj(players)
        self.players = players
        self.engines = engines
        self.dut_engine = engines['dut']
        self.cli_object = self.players['dut']['cli']

        self.conf_args = conf_args
        self.scenario = "lossy_lossless"
        self.power_thresholds_by_chip_type = power_thresholds_by_chip_type
        self.chip_type = chip_type
        self.is_ipv6 = is_ipv6

    @pytest.mark.parametrize("scenario_name, num_lossy_packets, num_lossless_packets, burst_config",
                             LOSSY_LOSSLESS_SCENARIOS, ids=LOSSY_LOSSLESS_SCENARIO_IDS)
    @allure.title('Lossy lossless scenarios. 400G to 400G lossy lossless test')
    @allure.description('400G<->400G test. Send lossy traffic from both sides.')
    def test_basic_loosy_lossless_tests_1_2_3(self, request, scenario_name, num_lossy_packets, num_lossless_packets,
                                              burst_config, packet_size=4096):
        with allure.step(f"Set test correct allure title with {scenario_name} parameter"):
            test_name = set_allure_lossy_lossless_title(request, scenario_name)

        self.traffic_jsons = get_lossy_lossless_basic_traffic(self.players, self.conf_args, num_lossy_packets,
                                                              num_lossless_packets, self.chip_type)
        burst_jsons = None
        if burst_config:
            # Built up front so a chip with no bonus ports fails the test before any traffic is started.
            burst_jsons = get_lossy_lossless_burst_traffic(self.players, self.conf_args, burst_config)

        with allure.step(f"Run traffic on all the ports. "
                         f"lossy percentage is {(num_lossy_packets / (num_lossy_packets + num_lossless_packets)) * 100}%.\n"
                         f"lossless percentage is {(num_lossless_packets / (num_lossy_packets + num_lossless_packets)) * 100}%"):
            run_traffic(self.players, self.scenario, self.traffic_jsons)

        if burst_jsons:
            # The burst seeds a policed loop on the bonus ports that dies on its own, so it is injected on top
            # of already stable traffic and right before the validation samples the counters.
            run_traffic(self.players, f"{self.scenario}_{BurstConfigConsts.BURST_SCENARIO_SUFFIX}", burst_jsons,
                        step="Running Burst Traffic on the bonus ports - Test body")

        with allure.step(f"Verifying the traffic for packet size {packet_size}"):
            # Note that ECN counters are expected, due to required low ECN thresholds, due to mix of lossy and lossless traffic
            # to make sure packets aren't dropped on RED.
            bw_threshold = SPCXRAConsts.DUT_TX_UTIL_AUTO_TH_DICT
            # A burst injects traffic of its own class from the bonus ports, so the lossy/lossless mix that
            # decides whether ECN marking is expected is not described by the parametrized counts alone.
            burst_dscp = burst_config.dscp if burst_config else None
            has_lossy = num_lossy_packets > 0 or burst_dscp == PerfConsts.DVS_LOSSY_TC
            has_lossless = num_lossless_packets > 0 or burst_dscp == PerfConsts.DVS_LOSSLESS_TC
            required_counter_list = ['tx_ecn_marked_tc_4'] if has_lossy and has_lossless else []
            ignore_counter_list = ['tx_ecn_marked_tc_3']
            if is_redmine_issue_active([5310085])[0]:
                # tx_ecn_marked_tc_4 chronically shows non-zero with no functional impact
                # identified; don't fail the test on it while the issue is open.
                if 'tx_ecn_marked_tc_4' in required_counter_list:
                    required_counter_list.remove('tx_ecn_marked_tc_4')
                ignore_counter_list.append('tx_ecn_marked_tc_4')
            config = ValidationConfig(players=self.players, test_name=test_name, scenario=self.scenario,
                                      chip_type=self.chip_type, bw_threshold=bw_threshold,
                                      bw_fairness_threshold_per_port_group=BwFairnessThreshold.get_bw_fairness_threshold_per_port_group(bw_threshold),
                                      tc_occ_threshold=PerfConsts.OCC_TH_DICT,
                                      power_threshold=self.power_thresholds_by_chip_type,
                                      required_counter_list=required_counter_list,
                                      ignore_counter_list=ignore_counter_list)
            run_validation(config)
