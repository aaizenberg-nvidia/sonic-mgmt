import os
import allure
import logging
import pytest
from ngts.helpers.performance.traffic_helpers import is_ipv6
from ngts.helpers.performance.performance_setup_helpers import (add_test_mongo_metadata, skip_performance_test_conditionally,
                                                                update_port_group_in_df)
from ngts.constants.constants import InfraConst
from ngts.constants.performance_constants import PerfConsts, MongoDbConsts, MRCConsts
from ngts.performance_tests.srv6.utils.srv6_common import TestSRv6Base
from ngts.helpers.performance.performance_db_helpers import get_perf_test_name
from ngts.performance_tests.srv6.conftest import (get_spine_many_to_few_port_group_df,
                                                  get_spine_downstream_groups_port_group_df,
                                                  get_srv6_tests_skip_condition)

logger = logging.getLogger()


class TestSRv6Spine(TestSRv6Base):

    @pytest.fixture(autouse=True)
    def setup(self, players, engines, cli_objects, is_ipv6, chip_type, conf_args, power_thresholds_by_chip_type, shaper_value):
        self.players = players
        self.engines = engines
        self.cli_objects = cli_objects
        self.engine = engines['dut']
        self.cli_object = self.cli_objects['dut']
        self.tg_cli_object = self.cli_objects[PerfConsts.LEFT_TG_ALIAS]
        self.ip = InfraConst.IPV6 if is_ipv6 else InfraConst.IPV4
        self.chip_type = chip_type
        self.scenario = "srv6"
        self.conf_args = conf_args
        self.hwsku = conf_args["hwsku"]
        self.power_thresholds_by_chip_type = power_thresholds_by_chip_type
        self.dut_interfaces_ipv6_configuration_dict = self.cli_object.performance.get_dut_interfaces_ipv6_configuration()
        self.vlan_interface_configuration_dict = self.tg_cli_object.performance.get_tg_interfaces_vlan_configuration()
        self.cli_object.performance.configure_interfaces_mac_neighbor(self.vlan_interface_configuration_dict)
        self.cli_object.trimming.config_optimal_trimming_size(self.chip_type)
        self.opt_ts = os.getenv(MRCConsts.OPT_TS,
                                default=MRCConsts.get_opt_ts_default(self.chip_type))
        self.cli_object.trimming.configure_custom_dwrr_weights()
        self.shaper_value = shaper_value

    @pytest.mark.skip(reason="This test is no longer supported on performance setup, due to infra limitation - Contact QA for test results using IXIA")
    @pytest.mark.parametrize("traffic_type", MRCConsts.REGRESSION_TRAFFIC_TYPE_LIST)
    def test_spine_round_robin_srv6(self, request, traffic_type):
        """
        All to all Full mesh (spine)-

        Each ingress port sends traffic to all egress ports in round-robin.
        In each round, each ingress port sends workload x to egress ports in increasing order,
        different then the increasing order of the other ingress ports. For example:
        First round (1 packet sent):
            port 0 → port 1.
            port 1 → port 2.
            ...
            port 447 → port 0.
        Second round (1 packet sent):
            port 0 → port 2.
            port 1 → port 3.
            ...
            port 447 → port 1.

        In order to recreate this full mesh we just define all the spine downstream ports as both
        downstream and upstream this way get the traffic pattern as seen above.
        """
        pytest.skip("This test is no longer supported on performance setup, due to infra limitation - Contact QA for test results using IXIA")
        test_name = get_perf_test_name(request)
        round_robin_dict = MRCConsts.SPINE_ROUND_ROBIN_PORTS_NUM_BY_CHIP_TYPE[self.chip_type]
        round_robin_ports_num, round_robin_groups_num = round_robin_dict['group_size'], round_robin_dict['group_num']
        downstream_group1, downstream_group2, port_group_df = get_spine_downstream_groups_port_group_df(self.players, round_robin_ports_num, round_robin_groups_num)
        self.cli_object.performance.update_port_group_df_on_dut(port_group_df)
        with allure.step(f"Set test configuration description"):
            add_test_mongo_metadata(test_name,
                                    {MongoDbConsts.CONF_NAME: f"spine-round-robin",
                                     MongoDbConsts.TEST_TRAFFIC_TYPE: traffic_type,
                                     MongoDbConsts.PORT_GROUP_DF: port_group_df})
        self.round_robin_traffic_test_runner(test_name, traffic_type, downstream_group1,
                                             downstream_group2)

    @pytest.mark.parametrize("workload", MRCConsts.MRC_REGRESSION_WORKLOADS_LIST)
    @pytest.mark.parametrize("traffic_type", MRCConsts.REGRESSION_TRAFFIC_TYPE_LIST)
    @pytest.mark.parametrize("ingress_port_sequence", MRCConsts.INGRESS_PORT_SEQUENCE)
    @pytest.mark.parametrize("ingress_ports_num", MRCConsts.INGRESS_PORT_NUMBER_LIST)
    def test_spine_srv6_trimming_many_to_one(self, request,
                                             traffic_type, workload, ingress_port_sequence, ingress_ports_num, get_ports_from_start=False):
        condition, skip_message = get_srv6_tests_skip_condition(self.cli_object, self.chip_type)
        skip_performance_test_conditionally(condition, skip_message)
        test_name = get_perf_test_name(request)
        egress_port, port_group_df = self.get_egress_port_group_df(port_number=1, get_ports_from_start=get_ports_from_start)
        with allure.step(f"Many to one traffic with ingress ports num={ingress_ports_num}"):
            ingress_ports = self.get_ingress_ports(egress_port, ingress_ports_num, ingress_port_sequence, get_ports_from_start=get_ports_from_start)
        sdk_ingress_ports = self.cli_object.performance.get_sdk_ports(ingress_ports)
        port_group_df = update_port_group_in_df(port_group_df, "ingress_ports", sdk_ingress_ports)
        self.cli_object.performance.update_port_group_df_on_dut(port_group_df)
        with allure.step(f"Set test configuration description"):
            add_test_mongo_metadata(test_name, {MongoDbConsts.CONF_NAME: f"{ingress_ports_num}_to_one_traffic",
                                                MongoDbConsts.TEST_WORKLOAD: workload,
                                                MongoDbConsts.TEST_TRAFFIC_TYPE: traffic_type,
                                                MongoDbConsts.PORT_GROUP_DF: port_group_df})
        self.many_to_one_traffic_test_runner(test_name, traffic_type, workload, egress_port, ingress_ports)

    @pytest.mark.parametrize("workload", MRCConsts.MRC_REGRESSION_WORKLOADS_LIST)
    @pytest.mark.parametrize("traffic_type", MRCConsts.REGRESSION_TRAFFIC_TYPE_LIST)
    @pytest.mark.parametrize("M", MRCConsts.INGRESS_PORT_NUMBER_LIST)
    def test_spine_srv6_trimming_many_to_few(self, request, traffic_type, workload, M):
        condition, skip_message = get_srv6_tests_skip_condition(self.cli_object, self.chip_type)
        skip_performance_test_conditionally(condition, skip_message)
        test_name = get_perf_test_name(request)
        egress_ports, ingress_ports, port_group_df = get_spine_many_to_few_port_group_df(self.players, self.chip_type, M)
        self.cli_object.performance.update_port_group_df_on_dut(port_group_df)
        with allure.step(f"Set test configuration description"):
            add_test_mongo_metadata(test_name,
                                    {MongoDbConsts.CONF_NAME: f"spine-many-to-few({len(ingress_ports)}-to-{len(egress_ports)})",
                                     MongoDbConsts.PORT_GROUP_DF: port_group_df,
                                     MongoDbConsts.TEST_TRAFFIC_TYPE: traffic_type,
                                     MongoDbConsts.TEST_WORKLOAD: workload})
        self.many_to_few_traffic_test_runner(test_name, traffic_type, workload,
                                             egress_ports=egress_ports, ingress_ports=ingress_ports, M=M,
                                             tc_threshold=MRCConsts.SPINE_MANY_TO_FEW_TRAFFIC_TC_OCC_TH)
