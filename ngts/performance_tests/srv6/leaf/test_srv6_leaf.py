import os
import time
import allure
import logging
from ngts.performance_tests.srv6.utils.srv6_traffic_patterns import get_many_to_few_traffic
import pytest
import pandas as pd
from ngts.helpers.performance.performance_setup_helpers import (ValidationConfig, Validation, stop_traffic,
                                                                run_traffic, run_validation,
                                                                add_test_mongo_metadata,
                                                                skip_test_on_unsupported_os,
                                                                skip_performance_test_conditionally, skip_test_on_unsupported_chip_type,
                                                                set_shaper_on_traffic_gen)
from ngts.constants.constants import InfraConst, CliType
from ngts.constants.performance_constants import PerfConsts, MongoDbConsts, MRCConsts, ValidationConsts, BwFairnessThreshold
from ngts.performance_tests.srv6.conftest import (get_upstream_downstream_port_group_df,
                                                  get_upstream_downstream_groups_port_group_df,
                                                  get_srv6_tests_skip_condition, get_victim_flow_port_group_df)
from ngts.performance_tests.srv6.utils.srv6_common import TestSRv6Base
from ngts.performance_tests.srv6.utils.srv6_workloads import get_workload_method
from ngts.performance_tests.srv6.leaf.conftest import get_bisection_traffic
from ngts.helpers.performance.performance_db_helpers import get_perf_test_name
from ngts.helpers.performance.traffic_helpers import validate_per_tc
from devts.infra.tools.exceptions.test_issue import TestIssue
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.cli_wrappers.nvue.nvue_cli import NvueCli


logger = logging.getLogger()


class TestSRv6Leaf(TestSRv6Base):

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
        trimming_size = int(os.getenv(MRCConsts.OPT_TS,
                                      MRCConsts.get_opt_ts_default(self.chip_type)))
        if isinstance(self.cli_object, NvueCli):
            self.cli_object.trimming.configure_trimming_size(trimming_size)
        else:
            self.cli_object.trimming.config_optimal_trimming_size(self.chip_type)
        self.opt_ts = trimming_size
        self.cli_object.trimming.configure_custom_dwrr_weights()
        self.shaper_value = shaper_value

    @pytest.mark.parametrize("workload", MRCConsts.MRC_REGRESSION_WORKLOADS_LIST)
    @pytest.mark.parametrize("traffic_type", MRCConsts.REGRESSION_TRAFFIC_TYPE_LIST)
    @pytest.mark.parametrize("is_drop_over_max", [False, True])
    @pytest.mark.FR_4911232()
    def test_bisection_srv6(self, request, traffic_type, workload, is_drop_over_max):
        condition, skip_message = get_srv6_tests_skip_condition(self.cli_object, self.chip_type)
        skip_performance_test_conditionally(condition, skip_message)
        if is_drop_over_max:
            skip_test_on_unsupported_os(self.cli_object, CliType.NVUE)
        with allure.step(f"Set test correct allure title with {self.ip} parameter"):
            test_name = get_perf_test_name(request)
            num_of_ports = MRCConsts.UPSTREAM_DOWNSTREAM_NUM_OF_PORTS_BY_CHIP_TYPE[self.chip_type]
            upstream, downstream, port_group_df = get_upstream_downstream_port_group_df(self.players, upstream_ports_num=num_of_ports,
                                                                                        downstream_ports_num=num_of_ports,
                                                                                        sequential=is_drop_over_max)
            self.cli_object.performance.update_port_group_df_on_dut(port_group_df)
            add_test_mongo_metadata(test_name, {MongoDbConsts.CONF_NAME: "bisection",
                                                MongoDbConsts.TEST_WORKLOAD: workload,
                                                MongoDbConsts.TEST_TRAFFIC_TYPE: traffic_type,
                                                MongoDbConsts.PORT_GROUP_DF: port_group_df})

        drop_over_max_config = None
        if is_drop_over_max:
            drop_over_max_config = MRCConsts.DROP_OVER_MAX_BY_CHIP_TYPE.get(self.chip_type)
            skip_performance_test_conditionally(drop_over_max_config is None,
                                                f"Drop Over Max not configured for chip type {self.chip_type}")
            with allure.step(f"Configure {drop_over_max_config['num_dummy_acls']} dummy ACLs for pipeline lookup stress"):
                dut_ports = upstream + downstream
                self.cli_object.performance.configure_dummy_acls(
                    self.conf_args["template_path"],
                    dut_ports, drop_over_max_config['num_dummy_acls'])

        with allure.step(f"Run bisection traffic pattern on {len(upstream)} upstream ports <-> {len(downstream)} downstream ports"):
            create_workload_stream = get_workload_method(workload)
            packet_size = drop_over_max_config['packet_size'] if is_drop_over_max else None
            mrc_num_packets = drop_over_max_config['mrc_num_packets'] if is_drop_over_max else None
            traffic_jsons = get_bisection_traffic(self.players, self.conf_args, traffic_type,
                                                  self.dut_interfaces_ipv6_configuration_dict,
                                                  create_workload_stream, upstream_ports=upstream, downstream_ports=downstream,
                                                  packet_size=packet_size, mrc_num_packets=mrc_num_packets,
                                                  vary_src_ip=is_drop_over_max)
            set_shaper_on_traffic_gen(self.players, speed=self.conf_args["speed"], shaper_value=self.shaper_value)
            run_traffic(self.players, self.scenario, traffic_jsons, attach_traffic_json=False)
            self.wait_for_traffic_to_stabilize("bisection")  # TODO: see wait_for_traffic_to_stabilize — replace fixed sleep

        with allure.step("Verifying the traffic for all egress ports"):
            additional_validations = self.get_additional_validations(traffic_type,
                                                                     tc_occ_allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION_BISECTION,
                                                                     drop_over_max_sample_port=upstream[0] if is_drop_over_max else None)
            bw_threshold = self.get_bisection_bw_threshold() if not is_drop_over_max else None
            config = ValidationConfig(players=self.players, test_name=test_name, scenario=self.scenario,
                                      chip_type=self.chip_type,
                                      bw_threshold=bw_threshold,
                                      bw_fairness_threshold_per_port_group=BwFairnessThreshold.get_bw_fairness_threshold_per_port_group(bw_threshold),
                                      tc_occ_threshold=MRCConsts.OCC_TH_DICT,
                                      power_threshold=self.power_thresholds_by_chip_type,
                                      run_validate_counters=not is_drop_over_max,
                                      run_validate_no_drops_on_tg_ports=not is_drop_over_max,
                                      additional_validations=additional_validations)
            try:
                run_validation(config)
            finally:
                if is_drop_over_max:
                    with allure.step("Stop traffic and reload DUT configuration to clean residual state"):
                        stop_traffic(self.players)
                        reload_start = time.time()
                        logger.info(f"Config reload start: {reload_start:.3f}")
                        self.cli_object.general.reload_configuration(force=True)
                        self.cli_object.general.verify_dockers_are_up()
                        reload_end = time.time()
                        logger.info(f"Config reload + dockers verified end: {reload_end:.3f} "
                                    f"(duration: {reload_end - reload_start:.3f}s)")
            set_shaper_on_traffic_gen(self.players, speed=self.conf_args["speed"], shaper_value=MRCConsts.SHAPER_VALUE_AFTER_TEST)

    @pytest.mark.parametrize("traffic_type", MRCConsts.REGRESSION_TRAFFIC_TYPE_LIST)
    def test_leaf_round_robin_srv6(self, request, traffic_type):
        pytest.skip("This test is no longer supported on performance setup, due to infra limitation - Contact QA for test results using IXIA")
        condition, skip_message = get_srv6_tests_skip_condition(self.cli_object, self.chip_type)
        skip_performance_test_conditionally(condition, skip_message)
        test_name = get_perf_test_name(request)
        round_robin_dict = MRCConsts.LEAF_ROUND_ROBIN_PORTS_NUM_BY_CHIP_TYPE[self.chip_type]
        round_robin_ports_num, round_robin_groups_num = round_robin_dict['group_size'], round_robin_dict['group_num']
        upstream_groups, downstream_groups, port_group_df = get_upstream_downstream_groups_port_group_df(self.players,
                                                                                                         upstream_ports_num=round_robin_ports_num,
                                                                                                         downstream_ports_num=round_robin_ports_num,
                                                                                                         num_of_groups=round_robin_groups_num)
        self.cli_object.performance.update_port_group_df_on_dut(port_group_df)
        with allure.step("Set test configuration description"):
            add_test_mongo_metadata(test_name,
                                    {MongoDbConsts.CONF_NAME:
                                     "leaf-round-robin",
                                     MongoDbConsts.TEST_TRAFFIC_TYPE: traffic_type,
                                     MongoDbConsts.PORT_GROUP_DF: port_group_df})
        self.round_robin_traffic_test_runner(test_name, traffic_type, upstream_groups, downstream_groups)

    @pytest.mark.parametrize("workload", MRCConsts.MRC_REGRESSION_WORKLOADS_LIST)
    @pytest.mark.parametrize("traffic_type", MRCConsts.REGRESSION_TRAFFIC_TYPE_LIST)
    @pytest.mark.parametrize("ingress_port_sequence", MRCConsts.INGRESS_PORT_SEQUENCE)
    @pytest.mark.parametrize("ingress_ports_num", MRCConsts.INGRESS_PORT_NUMBER_LIST)
    def test_leaf_srv6_trimming_many_to_one(self, request,
                                            traffic_type, workload, ingress_port_sequence, ingress_ports_num, get_ports_from_start=False):
        condition, skip_message = get_srv6_tests_skip_condition(self.cli_object, self.chip_type)
        skip_performance_test_conditionally(condition, skip_message)
        test_name = get_perf_test_name(request)
        egress_port, ingress_ports, port_group_df = self.cli_object.performance.get_leaf_many_to_few_port_group_df(ingress_ports_num, ingress_ports_num)
        self.cli_object.performance.update_port_group_df_on_dut(port_group_df)
        with allure.step("Set test configuration description"):
            add_test_mongo_metadata(test_name, {MongoDbConsts.CONF_NAME: f"{ingress_ports_num}_to_one_traffic",
                                                MongoDbConsts.TEST_WORKLOAD: workload,
                                                MongoDbConsts.TEST_TRAFFIC_TYPE: traffic_type,
                                                MongoDbConsts.INGRESS_PORT_SEQUENCE: ingress_port_sequence,
                                                MongoDbConsts.PORT_GROUP_DF: port_group_df})
        self.many_to_one_traffic_test_runner(test_name, traffic_type, workload, egress_port, ingress_ports)

    @pytest.mark.parametrize("workload", MRCConsts.MRC_REGRESSION_WORKLOADS_LIST)
    @pytest.mark.parametrize("traffic_type", MRCConsts.REGRESSION_TRAFFIC_TYPE_LIST)
    @pytest.mark.parametrize("M", MRCConsts.INGRESS_PORT_NUMBER_LIST)
    def test_leaf_srv6_trimming_many_to_few(self, request, traffic_type, workload, M):
        condition, skip_message = get_srv6_tests_skip_condition(self.cli_object, self.chip_type)
        skip_performance_test_conditionally(condition, skip_message)
        test_name = get_perf_test_name(request)
        num_of_ports = MRCConsts.UPSTREAM_DOWNSTREAM_NUM_OF_PORTS_BY_CHIP_TYPE[self.chip_type]
        egress_ports, ingress_ports, port_group_df = self.cli_object.performance.get_leaf_many_to_few_port_group_df(M, num_of_ports)
        self.cli_object.performance.update_port_group_df_on_dut(port_group_df)
        with allure.step("Set test configuration description"):
            add_test_mongo_metadata(test_name,
                                    {MongoDbConsts.CONF_NAME: "leaf-many-to-few",
                                     MongoDbConsts.PORT_GROUP_DF: port_group_df,
                                     MongoDbConsts.TEST_TRAFFIC_TYPE: traffic_type,
                                     MongoDbConsts.TEST_WORKLOAD: workload})
        traffic_validation_jsons_list, violations_list, trimmed_untrimmed_dropped_percentages = self.many_to_few_traffic_test_runner(test_name, traffic_type, workload,
                                                                                                                                     egress_ports=egress_ports, ingress_ports=ingress_ports, M=M,
                                                                                                                                     tc_threshold=MRCConsts.LEAF_MANY_TO_FEW_TRAFFIC_TC_OCC_TH)
        if violations_list:
            raise TestIssue("\n".join(violations_list))

    @pytest.mark.parametrize("traffic_type", [MRCConsts.TRAFFIC_TYPE_SRV6])
    def test_victim_flow_srv6(self, request, traffic_type, packet_size=4096):
        skip_test_on_unsupported_chip_type(self.chip_type, "SPC4")
        skip_performance_test_conditionally(isinstance(self.cli_object, NvueCli), "This test is to verify a FW bug, it's not OS related, so it can run on 1 OS and not all")
        test_name = get_perf_test_name(request)
        with allure.step("Set test configuration description"):
            add_test_mongo_metadata(test_name,
                                    {MongoDbConsts.CONF_NAME:
                                     f"leaf-victim-flow-{traffic_type}"})
        with allure.step("Set test correct port group dataframe"):
            bisection_left, bisection_right, many_to_one_ingress_ports, many_to_one_egress_ports, port_group_df = get_victim_flow_port_group_df(self.players)
            bisection_ports = bisection_left + bisection_right
            self.cli_object.performance.update_port_group_df_on_dut(port_group_df)
            add_test_mongo_metadata(test_name, {MongoDbConsts.PORT_GROUP_DF: port_group_df,
                                                MongoDbConsts.TEST_TRAFFIC_TYPE: traffic_type})
            allure.attach(pd.DataFrame(port_group_df).to_html(), MongoDbConsts.PORT_GROUP_DF, allure.attachment_type.HTML)
        with allure.step("get bisection traffic json for traffic generators"):
            bisection_traffic_jsons = get_bisection_traffic(self.players, self.conf_args, traffic_type,
                                                            self.dut_interfaces_ipv6_configuration_dict,
                                                            create_workload_stream=get_workload_method(MRCConsts.MRC2_DATA_ONLY_WORKLOAD_NAME),
                                                            upstream_ports=bisection_left,
                                                            downstream_ports=bisection_right)
        with allure.step("get many to one traffic json for traffic generators"):
            many_to_one_traffic_jsons = get_many_to_few_traffic(self.players, self.conf_args, traffic_type,
                                                                self.dut_interfaces_ipv6_configuration_dict,
                                                                egress_ports=many_to_one_egress_ports,
                                                                ingress_ports=many_to_one_ingress_ports,
                                                                create_workload_stream=get_workload_method(MRCConsts.MRC1_DATA_ONLY_WORKLOAD_NAME),
                                                                congestion=True)
        with allure.step("Clear counters"):
            self.cli_object.interface.clear_counters()
            self.cli_object.interface.clear_queue_counters()
        with allure.step("run bisection traffic"):
            run_traffic(self.players, self.scenario, bisection_traffic_jsons)
        with allure.step("run many to one traffic"):
            run_traffic(self.players, self.scenario, many_to_one_traffic_jsons)
        self.wait_for_traffic_to_stabilize("victim-flow")  # TODO: see wait_for_traffic_to_stabilize — replace fixed sleep

        with allure.step(f"Verifying the traffic for packet size {packet_size}"):
            samples_params_dict = PerfConsts.SAMPLES_PARAMS.copy()
            samples_params_dict[PerfConsts.CLEAR_COUNTERS_ENV_VAR] = "False"
            additional_validations = self.get_victim_flow_additional_validations()
            bw_threshold = self.get_victim_flow_bw_threshold()
            config = ValidationConfig(players=self.players, test_name=test_name, scenario=self.scenario,
                                      chip_type=self.chip_type,
                                      run_validate_counters=False,
                                      bw_threshold=bw_threshold,
                                      bw_fairness_threshold_per_port_group=BwFairnessThreshold.get_bw_fairness_threshold_per_port_group(bw_threshold),
                                      tc_occ_threshold=None,
                                      power_threshold=self.power_thresholds_by_chip_type,
                                      samples_params_dict=samples_params_dict,
                                      additional_validations=additional_validations)
            traffic_validation_jsons_list, violations_list = run_validation(config, ignore_violations=True)
        with allure.step("stop traffic"):
            stop_traffic(self.players)
        with allure.step("validate no dropped packets on queues"):
            self.cli_object.trimming.validate_trimmed_untrimmed_dropped_percentages(bisection_ports,
                                                                                    trimming_queue=MRCConsts.TRIMMING_TC,
                                                                                    drop_queues=MRCConsts.MRC_DATA_ONLY_WORKLOAD_TC_LIST,
                                                                                    violations_list=violations_list,
                                                                                    duration=None)

        if violations_list:
            raise TestIssue("\n".join(violations_list))

    def get_victim_flow_additional_validations(self):
        additional_validations = {
            'validate_per_tc': Validation(validate_per_tc, {'tc_occ_threshold': MRCConsts.OCC_TH_DICT,
                                                            'tc_to_validate': MRCConsts.MRC_DATA_ONLY_WORKLOAD_TC_LIST,
                                                            'tolerance': None,
                                                            'port_group_name_to_validate_list': []})
        }
        return additional_validations

    def get_bisection_bw_threshold(self):
        bw_threshold = {
            MRCConsts.BISECTION_DOWNSTREAM_PORT_GROUP_NAME: {ValidationConsts.TX: min(self.shaper_value, MRCConsts.DUT_TX_UTIL_TH),
                                                             ValidationConsts.RX: min(self.shaper_value, MRCConsts.DUT_TX_UTIL_TH)},
            MRCConsts.BISECTION_UPSTREAM_PORT_GROUP_NAME: {ValidationConsts.TX: min(self.shaper_value, MRCConsts.DUT_TX_UTIL_TH),
                                                           ValidationConsts.RX: min(self.shaper_value, MRCConsts.DUT_TX_UTIL_TH)}
        }
        if is_redmine_issue_active([4667031])[0]:
            bw_threshold[ValidationConsts.VALIDATION_KEY] = (ValidationConsts.TX_BW_AVG, ValidationConsts.RX_BW_AVG)
        return bw_threshold

    def get_victim_flow_bw_threshold(self):
        bw_threshold = {
            MRCConsts.EGRESS_PORT_GROUP_NAME: {ValidationConsts.TX: None,
                                               ValidationConsts.RX: self.shaper_value},
            MRCConsts.INGRESS_PORT_GROUP_NAME: {ValidationConsts.TX: None,
                                                ValidationConsts.RX: self.shaper_value},
        }
        bw_threshold.update(self.get_bisection_bw_threshold())
        return bw_threshold
