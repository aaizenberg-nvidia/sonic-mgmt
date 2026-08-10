import os

import allure
import logging
from ngts.performance_tests.srv6.utils.srv6_traffic_patterns import get_many_to_few_traffic
import pytest
import random
import time
import pandas as pd
import numpy as np
from ngts.helpers.performance.performance_setup_helpers import (ValidationConfig,
                                                                configure_mloops, stop_traffic,
                                                                run_traffic, run_validation,
                                                                add_test_mongo_metadata, update_port_group_in_df)
from ngts.constants.constants import InfraConst
from ngts.constants.performance_constants import PerfConsts, MongoDbConsts, MRCConsts, ValidationConsts, BwFairnessThreshold
from ngts.performance_tests.srv6.utils.srv6_common import TestSRv6Base
from ngts.performance_tests.srv6.utils.srv6_workloads import get_workload_method
from ngts.performance_tests.srv6.utils.srv6_traffic_patterns import get_many_to_one_traffic
from ngts.helpers.performance.performance_db_helpers import get_perf_test_name
from ngts.helpers.performance.traffic_helpers import (get_ports_avg_bw, get_tc_occ, convert_to_percentage)
from devts.infra.tools.exceptions.test_issue import TestIssue

logger = logging.getLogger()


class TestSRv6LeafCalibration(TestSRv6Base):

    @pytest.fixture(autouse=True)
    def setup(self, players, engines, cli_objects, is_ipv6, chip_type, conf_args, shaper_value, power_thresholds_by_chip_type):
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

    @pytest.mark.parametrize("workload", MRCConsts.MRC_REGRESSION_WORKLOADS_LIST)
    @pytest.mark.parametrize("traffic_type", [MRCConsts.TRAFFIC_TYPE_SRV6])
    def test_optimal_trimming_value_with_srv6(self, request, cleanup_trimming_threshold, traffic_type, workload,
                                              packet_size=4096):
        pytest.skip("This test is not running in regression, only used for manual calibration of OPT_TS")
        egress_ports, port_group_df = self.get_egress_port_group_df(port_number=8)
        self.cli_object.performance.update_port_group_df_on_dut(port_group_df)
        self.cli_object.trimming.configure_trim_of_all_packets(ports=egress_ports, queues=MRCConsts.TRIMMING_ELEGABLE_QUEUE_NUM, scenario=self.scenario)
        self.cli_object.trimming.enable_trimming_on_lossy_queue()
        self.cli_object.trimming.disable_packets_aging()
        with allure.step(f"Set test correct allure title with {self.ip} parameter"):
            test_name = get_perf_test_name(request)
            add_test_mongo_metadata(test_name, {MongoDbConsts.CONF_NAME: f"Calibrate-OPT_TS",
                                                MongoDbConsts.PORT_GROUP_DF: port_group_df,
                                                MongoDbConsts.TEST_WORKLOAD: workload,
                                                MongoDbConsts.TEST_TRAFFIC_TYPE: traffic_type})
        comparison_value_dict = {}
        total_violations_list = []
        data_df = []
        with allure.step(f"Start to find optimal trimming size in range {MRCConsts.MINIMAL_TRIM_SIZE} - {MRCConsts.MAX_TRIM_SIZE_CHECKING_RANGE} with step 16"):
            for trimming_size in range(MRCConsts.MINIMAL_TRIM_SIZE, MRCConsts.MAX_TRIM_SIZE_CHECKING_RANGE + 1, 16):
                with allure.step(f"Set trimming size to {trimming_size}"):
                    self.cli_object.trimming.configure_trimming_size(trimming_size)
                    ingress_to_one_egress_ports_num = int(np.ceil(packet_size / trimming_size))
                    total_ingress_ports_num = ingress_to_one_egress_ports_num * len(egress_ports)
                    with allure.step(f"Get {total_ingress_ports_num} ingress ports, {ingress_to_one_egress_ports_num} to 1 ratio"):
                        ingress_ports = self.get_ingress_ports(egress_ports, total_ingress_ports_num, get_ports_from_start=True)
                    traffic_jsons = get_many_to_few_traffic(self.players, self.conf_args, traffic_type,
                                                            self.dut_interfaces_ipv6_configuration_dict,
                                                            egress_ports, ingress_ports,
                                                            create_workload_stream=get_workload_method(workload))
                    self.cli_object.interface.clear_counters()
                    self.cli_object.interface.clear_queue_counters()
                    configure_mloops(self.players)
                    with allure.step(f"Run traffic from {total_ingress_ports_num} ingress ports"
                                     f" to {len(egress_ports)} egress ports"):
                        run_traffic(self.players, self.scenario, traffic_jsons)
                    with allure.step(f"Verifying the traffic for trimming size: {trimming_size}, "
                                     f"packet size: {packet_size}"):
                        bw_threshold = None
                        config = ValidationConfig(players=self.players, test_name=test_name, scenario=self.scenario,
                                                  chip_type=self.chip_type,
                                                  run_validate_counters=False,
                                                  bw_threshold=bw_threshold,
                                                  bw_fairness_threshold_per_port_group=BwFairnessThreshold.get_bw_fairness_threshold_per_port_group(bw_threshold),
                                                  tc_occ_threshold=None,
                                                  power_threshold=self.power_thresholds_by_chip_type)
                        traffic_validation_jsons_list, violations_list = run_validation(config, ignore_violations=True)
                        traffic_validation_json = traffic_validation_jsons_list.pop()
                        comparison_value = self.get_comparison_value(traffic_validation_json, trimming_size)
                        with allure.step(f"comparison value for trimming size: {trimming_size}, is {comparison_value}"):
                            comparison_value_dict[trimming_size] = comparison_value
                        total_violations_list.append(f"Violations for trimming size: {trimming_size}, "
                                                     f"ingress ports number: {total_ingress_ports_num}")
                        total_violations_list.extend(violations_list)
                        stop_traffic(self.players)
                        queue_packet_percentages_dict = self.cli_object.performance.validate_trimmed_untrimmed_dropped_percentages(egress_ports, trimming_queue=MRCConsts.TRIMMING_TC,
                                                                                                                                   drop_queues=MRCConsts.TRIMMING_ELEGABLE_QUEUE_NUM,
                                                                                                                                   violations_list=total_violations_list, return_dict=True)
                        queue_packet_percentages_dict['trimming_size'] = trimming_size
                        queue_packet_percentages_dict['comparison_value'] = comparison_value
                        queue_packet_percentages_dict['ingress_ports_num'] = total_ingress_ports_num
                        queue_packet_percentages_dict['egress_ports_num'] = len(egress_ports)
                        avg_ports_tx, avg_ports_rx = get_ports_avg_bw(traffic_validation_json, MRCConsts.EGRESS_PORT_GROUP_NAME)
                        tc_occ_dict = get_tc_occ(traffic_validation_json, tc_list=MRCConsts.WORKLOAD_1_TC_LIST, port_group_name=MRCConsts.EGRESS_PORT_GROUP_NAME)
                        queue_packet_percentages_dict['avg_ports_tx'] = convert_to_percentage(avg_ports_tx)
                        queue_packet_percentages_dict['avg_ports_rx'] = convert_to_percentage(avg_ports_rx)
                        queue_packet_percentages_dict.update(tc_occ_dict)
                        data_df.append(queue_packet_percentages_dict)
        trimming_df = pd.DataFrame(data_df)
        with allure.step(f"Attach trimming_df"):
            allure.attach(trimming_df.to_html(), "Trimming dataframe", attachment_type=allure.attachment_type.HTML)
        self.set_opt_ts(comparison_value_dict)
        if total_violations_list:
            raise TestIssue("\n".join(total_violations_list))

    @pytest.mark.parametrize("workload", [MRCConsts.WORKLOAD1_NAME])
    @pytest.mark.parametrize("traffic_type", [MRCConsts.TRAFFIC_TYPE_SRV6])
    @pytest.mark.parametrize("ingress_port_sequence", [MRCConsts.INGRESS_PORT_SEQUENCE_NON_CONSECUTIVE])
    def test_leaf_srv6_trimming_many_to_one_for_experiments(self, request, traffic_type, workload, ingress_port_sequence):
        pytest.skip("This test is not running in regression, only used for manual calibration of many to one ingress ports num")
        test_name = get_perf_test_name(request)
        data_df = []
        egress_port, port_group_df = self.get_egress_port_group_df(port_number=1, get_ports_from_start=True)
        with allure.step(f"Set test info"):
            add_test_mongo_metadata(test_name, {MongoDbConsts.CONF_NAME: f"many_to_one_traffic_for_experiments",
                                                MongoDbConsts.PORT_GROUP_DF: port_group_df})
        total_violations_list = []
        for ingress_ports_num in range(2, 12):
            with allure.step(f"Many to one traffic with ingress ports num={ingress_ports_num}"):
                ingress_ports = self.get_ingress_ports(egress_port, ingress_ports_num, ingress_port_sequence, get_ports_from_start=True)
                sdk_ingress_ports = self.cli_object.performance.get_sdk_ports(ingress_ports)
                update_port_group_in_df(port_group_df, MRCConsts.INGRESS_PORT_GROUP_NAME, sdk_ingress_ports)
                self.cli_object.performance.update_port_group_df_on_dut(port_group_df)
                traffic_jsons = get_many_to_one_traffic(self.players, self.conf_args, traffic_type,
                                                        self.dut_interfaces_ipv6_configuration_dict,
                                                        egress_port, ingress_ports,
                                                        create_workload_stream=get_workload_method(workload),
                                                        congestion=True)

                with allure.step(f"Configure mloops"):
                    configure_mloops(self.players)
                with allure.step(f"Clear counters"):
                    self.cli_object.interface.clear_counters()
                    self.cli_object.interface.clear_queue_counters()
                with allure.step(f"Run traffic"):
                    run_traffic(self.players, self.scenario, traffic_jsons, attach_traffic_json=False)
                with allure.step(f"Verifying the traffic on egress port: {egress_port}"):
                    samples_params_dict = PerfConsts.SAMPLES_PARAMS.copy()
                    samples_params_dict[PerfConsts.CLEAR_COUNTERS_ENV_VAR] = "False"
                    bw_threshold = None
                    config = ValidationConfig(players=self.players, test_name=test_name, scenario=self.scenario,
                                              chip_type=self.chip_type,
                                              run_validate_counters=False,
                                              bw_threshold=bw_threshold,
                                              bw_fairness_threshold_per_port_group=BwFairnessThreshold.get_bw_fairness_threshold_per_port_group(bw_threshold),
                                              tc_occ_threshold=None,
                                              power_threshold=self.power_thresholds_by_chip_type,
                                              samples_params_dict=samples_params_dict)
                    traffic_validation_jsons_list, violations_list = run_validation(config, ignore_violations=True, attach_to_allure=False)
                    traffic_validation_json = traffic_validation_jsons_list.pop()
                    avg_ports_tx, avg_ports_rx = get_ports_avg_bw(traffic_validation_json, MRCConsts.EGRESS_PORT_GROUP_NAME)
                    tc_occ_dict = get_tc_occ(traffic_validation_json, tc_list=MRCConsts.WORKLOAD_1_TC_LIST, port_group_name=MRCConsts.EGRESS_PORT_GROUP_NAME)
                with allure.step(f"avg ports tx: {avg_ports_tx}, avg ports rx: {avg_ports_rx}, tc_occ_dict: {tc_occ_dict}"):
                    logger.info(f"avg ports tx: {avg_ports_tx}, avg ports rx: {avg_ports_rx}, tc_occ_dict: {tc_occ_dict}")
                with allure.step(f"Stop traffic"):
                    stop_traffic(self.players)
                queue_packet_percentages_dict = self.cli_object.trimming.validate_trimmed_untrimmed_dropped_percentages(egress_port,
                                                                                                                        trimming_queue=MRCConsts.TRIMMING_TC,
                                                                                                                        drop_queues=[MRCConsts.MRC1_DATA_TC, MRCConsts.MRC2_DATA_TC, MRCConsts.MRC_RETRANSMISSION_TC],
                                                                                                                        violations_list=violations_list, return_dict=True)
                queue_packet_percentages_dict.update(self.cli_object.trimming.get_queue_packet_percentages(egress_port, [MRCConsts.MRC1_DATA_TC, MRCConsts.MRC2_DATA_TC, MRCConsts.MRC_RETRANSMISSION_TC, MRCConsts.TRIMMING_TC]))
                queue_packet_percentages_dict['ingress_ports_num'] = ingress_ports_num
                queue_packet_percentages_dict[ValidationConsts.TX_RATE] = convert_to_percentage(avg_ports_tx)
                queue_packet_percentages_dict[ValidationConsts.RX_RATE] = convert_to_percentage(avg_ports_rx)
                queue_packet_percentages_dict.update(tc_occ_dict)
                data_df.append(queue_packet_percentages_dict)
                total_violations_list.append(f"ingress ports num: {ingress_ports_num} violations:")
                total_violations_list.extend(violations_list)

        many_to_one_df = pd.DataFrame(data_df)
        with allure.step(f"Attach many_to_one_df"):
            allure.attach(many_to_one_df.to_html(), "Many to one dataframe", attachment_type=allure.attachment_type.HTML)
        if total_violations_list:
            raise TestIssue("\n".join(total_violations_list))

    @pytest.mark.parametrize("workload", MRCConsts.MRC_REGRESSION_WORKLOADS_LIST)
    @pytest.mark.parametrize("traffic_type", MRCConsts.REGRESSION_TRAFFIC_TYPE_LIST)
    @pytest.mark.parametrize("acl_mode", MRCConsts.ACL_MODES)
    def test_leaf_srv6_trimming_many_to_few(self, request, traffic_type, workload, acl_mode):
        pytest.skip("This test is not running in regression, only used for manual calibration of many to few ingress ports num")
        total_results_df = []
        total_violations_list = []
        test_name = get_perf_test_name(request)
        if acl_mode:
            with allure.step(f"Load ports acl conf"):
                dut_ports = self.cli_object.performance.get_dut_ports()
                self.cli_object.acl.load_acl_rules_from_template(self.conf_args["template_path"], "add_msft_acl.jinja", dut_ports)
        for num_of_ports in MRCConsts.PORTS_NUM_LIST:
            with allure.step(f"num_of_ports: {num_of_ports}"):
                for M in MRCConsts.M_LIST:
                    with allure.step(f"M: {M}"):
                        for option in MRCConsts.OPTION_LIST:
                            with allure.step(f"option: {option}"):
                                egress_ports, ingress_ports, port_group_df, pairing = self.cli_object.performance.get_leaf_many_to_few_port_group_df_for_debug(M, num_of_ports, option)
                                self.cli_object.performance.update_port_group_df_on_dut(port_group_df)
                                with allure.step(f"Set test configuration description"):
                                    add_test_mongo_metadata(test_name,
                                                            {MongoDbConsts.CONF_NAME: f"leaf-many-to-few",
                                                             MongoDbConsts.PORT_GROUP_DF: port_group_df,
                                                             MongoDbConsts.TEST_TRAFFIC_TYPE: traffic_type,
                                                             MongoDbConsts.TEST_WORKLOAD: workload})
                                traffic_validation_jsons_list, violations_list, trimmed_untrimmed_dropped_percentages = self.many_to_few_traffic_test_runner(test_name, traffic_type, workload,
                                                                                                                                                             egress_ports=egress_ports, ingress_ports=ingress_ports, M=M,
                                                                                                                                                             tc_threshold=MRCConsts.LEAF_MANY_TO_FEW_TRAFFIC_TC_OCC_TH, pairing=pairing)
                                traffic_validation_json = traffic_validation_jsons_list.pop()
                                avg_ports_tx, avg_ports_rx = get_ports_avg_bw(traffic_validation_json, MRCConsts.EGRESS_PORT_GROUP_NAME)
                                tc_occ_dict = get_tc_occ(traffic_validation_json, tc_list=MRCConsts.WORKLOAD_1_TC_LIST, port_group_name=MRCConsts.EGRESS_PORT_GROUP_NAME)
                                average_row = trimmed_untrimmed_dropped_percentages.pop()
                                average_row['incast'] = M
                                average_row['num_of_ingress_ports'] = num_of_ports
                                average_row[ValidationConsts.TX_RATE] = convert_to_percentage(avg_ports_tx)
                                average_row[ValidationConsts.RX_RATE] = convert_to_percentage(avg_ports_rx)
                                average_row.update(tc_occ_dict)
                                total_results_df.append(average_row)
                            total_violations_list.append(f"Violations for M: {M}, num_of_ports: {num_of_ports}")
                            total_violations_list.extend(violations_list)
        total_results_df = pd.DataFrame(total_results_df)
        with allure.step(f"Attach total_results_df"):
            allure.attach(total_results_df.to_html(), "Total results dataframe", attachment_type=allure.attachment_type.HTML)
        if total_violations_list:
            raise TestIssue("\n".join(total_violations_list))

    def get_comparison_value(self, traffic_validation_json, trimming_size):
        avg_ports_tx, avg_ports_rx = get_ports_avg_bw(traffic_validation_json, port_group_name=MRCConsts.EGRESS_PORT_GROUP_NAME)
        with allure.step(f"Get comparison value for trimming size: {trimming_size}, avg ports tx: {avg_ports_tx}, avg ports rx: {avg_ports_rx}"):
            comparison_value = avg_ports_tx * 158 / trimming_size
        return comparison_value

    def set_opt_ts(self, comparison_value_dict):
        opt_ts, max_value = max(comparison_value_dict.items(), key=lambda x: x[1])
        self.opt_ts = opt_ts
        logger.info(f"Optimal trimming size is: {opt_ts}, max value is: {max_value}")
        with allure.step(f"Set optimal trimming size to {opt_ts}"):
            os.environ[MRCConsts.OPT_TS] = str(opt_ts)
