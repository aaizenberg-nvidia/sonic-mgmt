import allure
import logging
import pytest

from ngts.helpers.performance.performance_setup_helpers import (ValidationConfig, configure_mloops, create_acl_dump,
                                                                run_traffic, run_validation, get_topology_obj)
from ngts.helpers.performance.performance_db_helpers import get_perf_test_name
from ngts.constants.performance_constants import PerfConsts, SPCXRAConsts, BwFairnessThreshold
from ngts.constants.constants import InfraConst
from ngts.performance_tests.spcx_ra.Alibaba_AR_tests.conftest import (AlibabaScenarioToconfiguration,
                                                                      get_alibaba_traffic, extract_acl_counters,
                                                                      get_conf_args)
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.helpers.performance.performance_counter_helpers import should_validate_performance_counters

logger = logging.getLogger()


SCENARIO_TO_PACKET_SIZE_DICT = {
    "p2_dut_no_acl": AlibabaScenarioToconfiguration(scenario_name="p2_dut_no_acl", packet_size=3200,
                                                    num_left_packets=12, num_right_packets=12, ecmp_type_stateless=True,
                                                    ecmp_size=4096, create_acls=False, create_goto_acl=False, two_sided_ar=True),

    "p3_leaf_no_acl": AlibabaScenarioToconfiguration(scenario_name="p3_leaf_no_acl_hash_crc", packet_size=1600,
                                                     num_left_packets=3, num_left_packets_spc6=1, num_right_packets=20, ecmp_type_stateless=True,
                                                     ecmp_size=4096, create_acls=False, create_goto_acl=False, two_sided_ar=False),

    "p4_leaf_six_acl": AlibabaScenarioToconfiguration(scenario_name="p4_1_leaf_six_acl_hash_crc", packet_size=1600,
                                                      num_left_packets=3, num_left_packets_spc6=1, num_right_packets=20, ecmp_type_stateless=False,
                                                      ecmp_size=512, create_acls=True, create_goto_acl=False, two_sided_ar=False),

    "p4_1_leaf_seven_acl": AlibabaScenarioToconfiguration(scenario_name="p4_2_leaf_seven_acl_hash_crc", packet_size=1500,
                                                          num_left_packets=12, num_left_packets_spc6=1, num_right_packets=19, ecmp_type_stateless=False,
                                                          ecmp_size=512, create_acls=True, create_goto_acl=True, two_sided_ar=False),
}


@pytest.mark.parametrize("basic_setup_configuration", [InfraConst.IPV4, InfraConst.IPV6], indirect=True)
class Test_Alibaba_scenarios_with_reset:
    @pytest.fixture(autouse=True)
    def setup(self, players, engines, power_thresholds_by_chip_type, chip_type, basic_setup_configuration):
        self.topology_obj = get_topology_obj(players)
        self.players = players
        self.engines = engines
        self.dut_engine = engines['dut']
        self.cli_object = self.players['dut']['cli']
        self.scenario = "spcx_ra"
        self.power_thresholds_by_chip_type = power_thresholds_by_chip_type
        self.ip = InfraConst.IPV6 if basic_setup_configuration else InfraConst.IPV4
        self.is_ipv6 = basic_setup_configuration
        self.chip_type = chip_type
        self.conf_args = get_conf_args(self.is_ipv6, self.chip_type)

    @pytest.mark.parametrize("scenario_name,scenario_configuration,packet_size,hash_type",
                             [(scenario_name, scenario_configuration, scenario_configuration.packet_size, hash_type)
                              for scenario_name, scenario_configuration in SCENARIO_TO_PACKET_SIZE_DICT.items()
                              for hash_type in ["crc", "random"]])
    @allure.title('test_Alibaba_scenario - {scenario_name} with {packet_size}B packets and {hash_type} hash')
    @allure.description('Added dynamically in test body')
    def test_alibaba_scenario(self, request, scenario_name, scenario_configuration, packet_size, hash_type, alibaba_scenarios_fixture, sdk_branch):
        if self.ip == InfraConst.IPV6 and is_redmine_issue_active([4584135])[0] and scenario_name == "p4_1_leaf_seven_acl":
            configure_mloops(self.players)
            pytest.skip("Skipping test due to active Redmine issue 4584135 for IPv6")

        with allure.step(f"Adjusting min packet size for SDK branch {sdk_branch}"):
            if sdk_branch == "sx_sdk_4_7_4500" and is_redmine_issue_active([4885962])[0]:
                packet_size += 100

        test_name = get_perf_test_name(request)

        with allure.step("Adding dynamic description to allure report"):
            allure.dynamic.description(f"Test Alibaba scenario {scenario_name} with packet size {packet_size}B and {hash_type} hash type. "
                                       f"ECMP type: {'stateless' if scenario_configuration.ecmp_type_stateless else 'stateful'}, "
                                       f"ECMP size: {scenario_configuration.ecmp_size}, "
                                       f"{'Create 6 ACLs' if scenario_configuration.create_acls else 'No ACLs'}, "
                                       f"{'Add goto ACL' if scenario_configuration.create_goto_acl else 'No goto ACL'}")
        with allure.step(f"Updating the configuration from the fixture"):
            self.conf_args = alibaba_scenarios_fixture
            self.conf_args["packet_size"] = packet_size
            self.traffic_jsons = get_alibaba_traffic(self.players, self.conf_args, spine_scenario=scenario_configuration.two_sided_ar)

        with allure.step(f"Run {packet_size}B packet Traffic on all the ports with hash type {hash_type}"):
            configure_mloops(self.players)
            run_traffic(self.players, self.scenario, self.traffic_jsons)

        with allure.step(f"Verifying the traffic for packet size {packet_size} with hash type {hash_type}"):
            if scenario_configuration.two_sided_ar:
                skip_first_counters_iteration = False
            else:
                skip_first_counters_iteration = True
            bw_threshold = SPCXRAConsts.DUT_TX_UTIL_IBM_TH_DICT
            config = ValidationConfig(players=self.players, test_name=test_name, scenario=self.scenario,
                                      chip_type=self.chip_type,
                                      bw_threshold=bw_threshold,
                                      bw_fairness_threshold_per_port_group=BwFairnessThreshold.get_bw_fairness_threshold_per_port_group(bw_threshold),
                                      packet_size=packet_size,
                                      run_validate_performance_counters=should_validate_performance_counters(self.cli_object),
                                      tc_occ_threshold=PerfConsts.OCC_TH_DICT,
                                      power_threshold=self.power_thresholds_by_chip_type,
                                      skip_first_counters_iteration=skip_first_counters_iteration)
            run_validation(config)

        if scenario_configuration.create_acls:
            with allure.step(f"Creating ACL dump"):
                acl_dump = create_acl_dump(self.players)
                logging.info(f"Creating ACL dump {acl_dump}")

            with allure.step(f"Extracting ACL counters"):
                acl_ar_counter, acl_goto_counter, goto_percentage = extract_acl_counters(acl_dump,
                                                                                         scenario_configuration.create_acls,
                                                                                         scenario_configuration.create_goto_acl)

                logging.info(f"ACL AR counter: {acl_ar_counter}")
                logging.info(f"ACL GOTO counter: {acl_goto_counter}")
            with allure.step(f"Verifying the ACL counters. ACL AR counter: {acl_ar_counter}, ACL GOTO counter: {acl_goto_counter} which are {goto_percentage:.3f}% of the total packets"):
                if scenario_configuration.create_acls and acl_ar_counter == 0:
                    raise Exception(f"AR ACL is not working as expected. {acl_ar_counter} is 0")
                if scenario_configuration.create_goto_acl and not (4.8 <= goto_percentage <= 5.2):
                    raise Exception(f"GOTO ACL is not working as expected. GOTO counter: {acl_goto_counter} is {goto_percentage:.3f}% of the total packets (expected 5 ± 0.2%)")
