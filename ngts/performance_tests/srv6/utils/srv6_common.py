import os
import json
import allure
import logging
import pytest
import random
import re
import numpy as np
import time
import pandas as pd
from collections import defaultdict
from ngts.helpers.performance.traffic_helpers import (pick_random_non_consecutive_ports,
                                                      pick_random_consecutive_ports,
                                                      validate_per_tc, compare_tc_occ_to_reference,
                                                      compare_latency_to_reference, compare_pg_to_reference)
from ngts.helpers.performance.performance_setup_helpers import (Validation, ValidationConfig, run_traffic,
                                                                stop_traffic, run_validation, configure_mloops,
                                                                skip_test_on_unsupported_os, add_test_mongo_metadata,
                                                                set_shaper_on_traffic_gen, create_sdk_dump)
from ngts.constants.constants import CliType, InfraConst, BugHandlerConst
from ngts.constants.performance_constants import PerfConsts, MongoDbConsts, MRCConsts, ValidationConsts, BwFairnessThreshold
from ngts.performance_tests.srv6.utils.srv6_workloads import get_workload_method
from ngts.performance_tests.srv6.utils.srv6_traffic_patterns import (get_round_robin_traffic, get_many_to_few_traffic, get_many_to_one_traffic)
from devts.infra.tools.exceptions.test_issue import TestIssue
from ngts.cli_wrappers.nvue.nvue_cli import NvueCli
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active

logger = logging.getLogger()


def validate_drop_over_max_watermark(traffic_json, players, sample_port, violations_list,
                                     pg_to_validate=0):
    """
    Validate that PG headroom max utilization is within the expected range for Drop Over Max.

    Reads HR Size, MBD, and Max HR Util from the SDK dump (sx_api_dbg_generate_dump.py)
    for the specified lossy PG. Validates:
    - Max HR Util > HR Size (confirming borrowing is occurring)
    - Max HR Util < HR Size + MBD (confirming borrowing limit is respected)

    Args:
        traffic_json: Traffic validation JSON from run_validation (unused, required by validation signature)
        players: Test players configuration (used to generate SDK dump)
        sample_port: Port name to read attributes for (e.g. 'Ethernet0')
        violations_list: List to append violations to
        pg_to_validate: Priority group number to validate (default 0)
    """
    cli_object = players[PerfConsts.DUT_ALIAS]['cli']
    sdk_dump_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                 "sdk_dumps", "srv6", "sdk_dump_drop_over_max")
    with allure.step("Generate SDK dump and read HR Size, MBD, and Max HR Util"):
        sdk_dump_str = create_sdk_dump(players, sdk_dump_path)
        allure.attach(sdk_dump_str, "SDK dump", allure.attachment_type.TEXT)
        pg_attrs = cli_object.performance.get_pg_buffer_attributes_from_sdk_dump(sdk_dump_str, sample_port,
                                                                                 pg=pg_to_validate)
        hr_size = pg_attrs['hr_size']
        mbd = pg_attrs['mbd']
        max_hr_util = pg_attrs['max_hr_util']
        if hr_size is None or mbd is None or max_hr_util is None:
            violations_list.append(
                f"Drop Over Max: Failed to parse PG{pg_to_validate} buffer attributes from SDK dump "
                f"for port {sample_port}. hr_size={hr_size}, mbd={mbd}, max_hr_util={max_hr_util}")
            return

    upper_limit = hr_size + mbd
    logger.info(f"Drop Over Max - HR Size: {hr_size}, MBD: {mbd}, Max HR Util: {max_hr_util}, "
                f"upper_limit: {upper_limit}, sample_port: {sample_port}")
    allure.attach(f"HR Size: {hr_size} cells\n"
                  f"MBD: {mbd} cells\n"
                  f"Max HR Util: {max_hr_util} cells\n"
                  f"upper_limit (HR Size + MBD): {upper_limit} cells\n"
                  f"sample_port: {sample_port}",
                  "Drop Over Max - DUT Buffer Config", allure.attachment_type.TEXT)
    with allure.step(f"Validate Max HR Util: {hr_size} < {max_hr_util} < {upper_limit}"):
        if max_hr_util <= hr_size:
            violations_list.append(
                f"Drop Over Max: PG{pg_to_validate} Max HR Util ({max_hr_util}) <= "
                f"HR Size ({hr_size}) for port {sample_port}. Borrowing is not occurring.")
        if max_hr_util > upper_limit:
            violations_list.append(
                f"Drop Over Max: PG{pg_to_validate} Max HR Util ({max_hr_util}) > "
                f"HR Size + MBD ({upper_limit}) for port {sample_port}. Borrowing limit exceeded.")


class TestSRv6Base:

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
        self.opt_ts = os.getenv(MRCConsts.OPT_TS,
                                default=MRCConsts.get_opt_ts_default(self.chip_type))
        self.conf_args = conf_args
        self.hwsku = conf_args["hwsku"]
        self.power_thresholds_by_chip_type = power_thresholds_by_chip_type
        self.dut_interfaces_ipv6_configuration_dict = {}
        self.vlan_interface_configuration_dict = {}
        self.shaper_value = shaper_value

    def wait_for_traffic_to_stabilize(self, scenario_tag="traffic", delay=10):
        """Fixed sleep before SDK validation; temporary workaround for SPC6 timing.

        TODO: Replace with deterministic readiness (egress counter movement, link/BGP up) once
        validated on SPC6 SRv6 lab runs — see docs/agent-memory/runs/2026-05-26-srv6-spc6-x4-x8-breakout.md.
        """
        with allure.step(f"Wait for {scenario_tag} traffic to stabilize before validation"):
            time.sleep(delay)

    def round_robin_traffic_test_runner(self, test_name, traffic_type,
                                        upstream_group, downstream_group, bisection_traffic=True):
        upstream_downstream_group = list(zip(upstream_group, downstream_group))
        all_ports_in_test, group_size, num_of_groups = self.get_all_ports_in_test(upstream_downstream_group)
        with allure.step(f"Run round-robin traffic pattern on {group_size}<-->{group_size} ports in {num_of_groups} groups"):
            traffic_jsons = get_round_robin_traffic(players=self.players,
                                                    conf_args=self.conf_args,
                                                    traffic_type=traffic_type,
                                                    upstream_downstream_group=upstream_downstream_group,
                                                    bisection_traffic=bisection_traffic,
                                                    dut_interfaces_ipv6_configuration_dict=self.dut_interfaces_ipv6_configuration_dict)
            set_shaper_on_traffic_gen(self.players, speed=self.conf_args["speed"], shaper_value=MRCConsts.BEFORE_TEST_SHAPER_VALUE)
            run_traffic(self.players, self.scenario, traffic_jsons, attach_traffic_json=False)
            self.wait_for_traffic_to_stabilize("round-robin")  # TODO: see wait_for_traffic_to_stabilize
        with allure.step(f"Verifying round-robin traffic pattern on all upstream ports and all downstream ports"):
            half_ports_num = len(all_ports_in_test) // 2
            round_robin_occ_th_dict = {ValidationConsts.OCC_AVG: 11 * half_ports_num,
                                       ValidationConsts.OCC_99: 22 * half_ports_num}
            additional_validations = self.get_additional_validations(traffic_type)
            set_shaper_on_traffic_gen(self.players, speed=self.conf_args["speed"], shaper_value=self.shaper_value)
            bw_threshold = MRCConsts.DUT_TX_UTIL_TH
            config = ValidationConfig(players=self.players, test_name=test_name, scenario=self.scenario,
                                      chip_type=self.chip_type,
                                      bw_threshold=bw_threshold,
                                      bw_fairness_threshold_per_port_group=BwFairnessThreshold.get_bw_fairness_threshold_per_port_group(bw_threshold),
                                      tc_occ_threshold=round_robin_occ_th_dict,
                                      power_threshold=self.power_thresholds_by_chip_type,
                                      additional_validations=additional_validations)
            run_validation(config)
            set_shaper_on_traffic_gen(self.players, speed=self.conf_args["speed"], shaper_value=MRCConsts.SHAPER_VALUE_AFTER_TEST)

    def many_to_one_traffic_test_runner(self, test_name, traffic_type, workload, egress_port, ingress_ports):
        """
        For each M in [(4K/OPT_TS)-1, (4K/OPT_TS)]:

        Define M ingress ports that send traffic to a certain single egress port
        Apply SRV6 configuration
        Generate many-to-one traffic
        Validate line rate BW in the egress port.
        Validate no discards (due to trimming) when M =(4K/OPT_TS)-1.
        Validate MRC retransmits and MRC control packets are not dropped
        because of the congestion caused by MRC data flow.
        Validate TC SB max (among ports) watermark < 22*260 buffer cells.
        """
        initial_trimming_counters = 0
        with allure.step(f"Many to one traffic with ingress ports num={len(ingress_ports)}"):
            traffic_jsons = get_many_to_one_traffic(self.players, self.conf_args, traffic_type,
                                                    self.dut_interfaces_ipv6_configuration_dict,
                                                    egress_port, ingress_ports,
                                                    create_workload_stream=get_workload_method(workload),
                                                    congestion=True)
            with allure.step(f"Clear counters"):
                self.cli_object.interface.clear_counters()
                self.cli_object.trimming.clear_trimming_counters()
                self.cli_object.interface.clear_queue_counters()

            with allure.step("Attach pre-traffic packet-trim and WJH state"):
                if isinstance(self.cli_object, NvueCli):
                    scenario_tag = "many_to_one"
                    nvue_pre_traffic_state_dump = (
                        self.cli_object.trimming.build_pre_traffic_packet_trim_state_json(scenario_tag))
                    allure.attach(nvue_pre_traffic_state_dump,
                                  f"nvue_pre_traffic_state_{scenario_tag}.json",
                                  attachment_type=allure.attachment_type.JSON)
            with allure.step(f"Run traffic"):
                start_time = time.time()
                run_traffic(self.players, self.scenario, traffic_jsons, attach_traffic_json=False)
            self.wait_for_traffic_to_stabilize("many-to-one")  # TODO: see wait_for_traffic_to_stabilize
            samples_params_dict = PerfConsts.SAMPLES_PARAMS.copy()
            samples_params_dict[PerfConsts.CLEAR_COUNTERS_ENV_VAR] = "False"
            additional_validations = self.get_many_to_one_additional_validations(traffic_type)
            with allure.step(f"Verifying the traffic on {len(ingress_ports)} ingress/egress ports"):
                bw_threshold = self.get_trimming_bw_threshold(traffic_type)
                config = ValidationConfig(players=self.players, test_name=test_name, scenario=self.scenario,
                                          chip_type=self.chip_type,
                                          run_validate_counters=False,
                                          bw_threshold=bw_threshold,
                                          bw_fairness_threshold_per_port_group=BwFairnessThreshold.get_bw_fairness_threshold_per_port_group(bw_threshold),
                                          validate_bw_rx=False,
                                          tc_occ_threshold=self.get_many_to_one_tc_occ_threshold(),
                                          power_threshold=self.power_thresholds_by_chip_type,
                                          samples_params_dict=samples_params_dict,
                                          additional_validations=additional_validations)
                traffic_validation_jsons_list, violations_list = run_validation(config, ignore_violations=True)
            with allure.step(f"stop traffic"):
                stop_traffic(self.players)
                end_time = time.time()
                duration = end_time - start_time
                time.sleep(10)
            with allure.step(f"validate trimmed untrimmed dropped percentages"):
                if len(ingress_ports) == MRCConsts.INCAST_VALUE_WITH_TRIMMING_DROP:
                    self.cli_object.performance.validate_ets(egress_port, MRCConsts.ETS_TC_LIST, violations_list)
                trimmed_untrimmed_dropped_percentages = self.validate_trimmed_untrimmed_dropped_percentages(test_name, egress_port, MRCConsts.TRIMMING_TC, [MRCConsts.MRC1_DATA_TC, MRCConsts.MRC2_DATA_TC, MRCConsts.MRC_RETRANSMISSION_TC], violations_list, duration=duration)
        if violations_list:
            raise TestIssue("\n".join(violations_list))

    def many_to_few_traffic_test_runner(self, test_name, traffic_type, workload,
                                        egress_ports, ingress_ports, tc_threshold, M, get_ports_from_start=False, pairing=None):
        configure_mloops(self.players)
        with allure.step(f"Clear counters"):
            self.cli_object.interface.clear_counters()
            self.cli_object.trimming.clear_trimming_counters()
            self.cli_object.interface.clear_queue_counters()
        with allure.step("Attach pre-traffic packet-trim and WJH state"):
            if isinstance(self.cli_object, NvueCli):
                scenario_tag = "many_to_few"
                nvue_pre_traffic_state_dump = (
                    self.cli_object.trimming.build_pre_traffic_packet_trim_state_json(scenario_tag))
                allure.attach(nvue_pre_traffic_state_dump,
                              f"nvue_pre_traffic_state_{scenario_tag}.json",
                              attachment_type=allure.attachment_type.JSON)
        with allure.step(f"Run many to few traffic on {len(ingress_ports)} ingress ports and {len(egress_ports)} egress ports, M={M}"):
            traffic_jsons = get_many_to_few_traffic(self.players, self.conf_args, traffic_type,
                                                    self.dut_interfaces_ipv6_configuration_dict,
                                                    egress_ports, ingress_ports,
                                                    create_workload_stream=get_workload_method(workload),
                                                    congestion=True,
                                                    pairing=pairing)
            start_time = time.time()
            run_traffic(self.players, self.scenario, traffic_jsons, attach_traffic_json=False)
            self.wait_for_traffic_to_stabilize("many-to-few")  # TODO: see wait_for_traffic_to_stabilize
        samples_params_dict = PerfConsts.SAMPLES_PARAMS.copy()
        samples_params_dict[PerfConsts.CLEAR_COUNTERS_ENV_VAR] = "False"
        bw_threshold = self.get_trimming_bw_threshold(traffic_type)
        additional_validations = self.get_many_to_few_additional_validations(egress_ports, tc_threshold)
        with allure.step(f"Verifying the traffic on {len(ingress_ports)} ingress/egress ports"):
            config = ValidationConfig(players=self.players, test_name=test_name, scenario=self.scenario,
                                      chip_type=self.chip_type,
                                      run_validate_counters=False,
                                      bw_threshold=bw_threshold,
                                      bw_fairness_threshold_per_port_group=BwFairnessThreshold.get_bw_fairness_threshold_per_port_group(bw_threshold),
                                      validate_bw_rx=False,
                                      tc_occ_threshold=None,
                                      power_threshold=self.power_thresholds_by_chip_type,
                                      samples_params_dict=samples_params_dict,
                                      additional_validations=additional_validations)
            traffic_validation_jsons_list, violations_list = run_validation(config, ignore_violations=True)
        with allure.step(f"stop traffic"):
            stop_traffic(self.players)
            end_time = time.time()
            duration = end_time - start_time
        with allure.step(f"validate trimmed untrimmed dropped percentages"):
            if M == MRCConsts.INCAST_VALUE_WITH_TRIMMING_DROP:
                self.cli_object.performance.validate_ets(egress_ports, MRCConsts.ETS_TC_LIST, violations_list)
            pairing_df = None
            if pairing:
                pairing_df = self.convert_pairing_into_df(pairing)
            trimmed_untrimmed_dropped_percentages = self.validate_trimmed_untrimmed_dropped_percentages(test_name, egress_ports, MRCConsts.TRIMMING_TC, [MRCConsts.MRC1_DATA_TC, MRCConsts.MRC2_DATA_TC, MRCConsts.MRC_RETRANSMISSION_TC], violations_list, pairing_df, duration)
        return traffic_validation_jsons_list, violations_list, trimmed_untrimmed_dropped_percentages

    def convert_pairing_into_df(self, pairing):
        port_group_df = []
        dut_ports = self.cli_object.performance.get_dut_ports()
        sorted_sdk_ports = sorted(self.cli_object.performance.get_hex_int_sdk_ports(dut_ports))
        ingress_sdk_port_dict = defaultdict(list)
        ingress_sonic_port_dict = defaultdict(list)
        ingress_idx_dict = defaultdict(list)
        egress_port_idx_dict = {}
        for ingress_port_list, egress_port in pairing:
            egress_int_port = self.cli_object.performance.get_hex_int_sdk_port(egress_port)
            egress_port_idx = sorted_sdk_ports.index(egress_int_port)
            egress_port_idx_dict[egress_port] = egress_port_idx
            for ingress_port in ingress_port_list:
                ingress_int_port = self.cli_object.performance.get_hex_int_sdk_port(ingress_port)
                ingress_port_idx = sorted_sdk_ports.index(ingress_int_port)
                sdk_ingress_port = self.cli_object.performance.get_sdk_port(ingress_port)
                ingress_sdk_port_dict[egress_port].append(str(sdk_ingress_port))
                ingress_sonic_port_dict[egress_port].append(str(ingress_port))
                ingress_idx_dict[egress_port].append(str(ingress_port_idx))
        for egress_port in ingress_sdk_port_dict.keys():
            port_group_df.append({ValidationConsts.OS_PORT_NAME: egress_port,
                                  "egress_idx": egress_port_idx_dict[egress_port],
                                  "ingress_sdk_ports": ",".join(ingress_sdk_port_dict[egress_port]),
                                  "ingress_sonic_ports": ",".join(ingress_sonic_port_dict[egress_port]),
                                  "ingress_ports_sdk_idx": ",".join(ingress_idx_dict[egress_port])})
        return pd.DataFrame(port_group_df)

    def get_egress_port_group_df(self, port_number, get_ports_from_start=False):
        port_group_df = []
        ports = self.cli_object.performance.get_right_left_ports_dict()["right_ports"]
        egress_ports = pick_random_non_consecutive_ports(ports_list=ports, port_number=port_number)
        if get_ports_from_start:
            egress_ports = ports[:port_number]
        for port in egress_ports:
            port_group_df.append({ValidationConsts.PORT: self.players['dut']['cli'].performance.get_sdk_port(port), MongoDbConsts.PORT_GROUP_NAME: "egress_ports"})
        return egress_ports, port_group_df

    def get_ingress_ports(self, egress_ports, ingress_ports_num, ingress_port_sequence=MRCConsts.INGRESS_PORT_SEQUENCE_CONSECUTIVE, get_ports_from_start=False):
        dut_ports = self.cli_object.performance.get_right_left_ports_dict()["left_ports"]
        port_list = list(set(dut_ports).difference(egress_ports))
        ingress_ports_candidates = self.cli_object.interface.get_sorted_ports_list(port_list, self.conf_args["split_left"])
        if ingress_port_sequence == MRCConsts.INGRESS_PORT_SEQUENCE_NON_CONSECUTIVE:
            ingress_ports = pick_random_non_consecutive_ports(ports_list=ingress_ports_candidates, port_number=ingress_ports_num, non_consecutive_gap=8)
        elif ingress_port_sequence == MRCConsts.INGRESS_PORT_SEQUENCE_CONSECUTIVE:
            ingress_ports = pick_random_consecutive_ports(ports_list=ingress_ports_candidates, port_number=ingress_ports_num)
        if get_ports_from_start:
            ingress_ports = ingress_ports_candidates[:ingress_ports_num]
        return ingress_ports

    def get_egress_ports(self, egress_ports_candidates, egress_ports_num, get_ports_from_start=False):
        egress_ports = random.sample(egress_ports_candidates, egress_ports_num)
        if get_ports_from_start:
            egress_ports = egress_ports_candidates[:egress_ports_num]
        return egress_ports

    def get_all_ports_in_test(self, upstream_downstream_group):
        all_ports_in_test = []
        for upstream_ports, downstream_ports in upstream_downstream_group:
            all_ports_in_test.extend(upstream_ports + downstream_ports)
        group_size = len(upstream_downstream_group[0][0])
        num_of_groups = len(upstream_downstream_group)
        return all_ports_in_test, group_size, num_of_groups

    def get_max_watermark_th(self, num_of_congested_ports, num_of_congested_tc):
        n = num_of_congested_ports * num_of_congested_tc
        pool_size_cells = MRCConsts.POOL_SIZE_CELLS_BY_CHIP_TYPE[self.chip_type]
        tc_max_occ_th = pool_size_cells * MRCConsts.TC_1_2_3_ALPHA / (1 + MRCConsts.TC_1_2_3_ALPHA * n)
        return {MRCConsts.EGRESS_PORT_GROUP_NAME: {ValidationConsts.MAX_WATERMARK: tc_max_occ_th},
                MRCConsts.INGRESS_PORT_GROUP_NAME: {}}

    def get_additional_validations(self, traffic_type, tc_occ_allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION,
                                   drop_over_max_sample_port=None):
        additional_validations = {}
        if traffic_type == MRCConsts.TRAFFIC_TYPE_SRV6:
            ipv6_validation_json_path = os.getenv(PerfConsts.TRAFFIC_VALIDATION_JSON_PATH)
            if ipv6_validation_json_path and os.path.exists(ipv6_validation_json_path):
                with open(ipv6_validation_json_path, 'r') as f:
                    ipv6_validation_json = json.load(f)
                if drop_over_max_sample_port is None:
                    additional_validations['compare_tc_occ_to_reference'] = Validation(compare_tc_occ_to_reference, {'reference_json': ipv6_validation_json,
                                                                                                                     'tc_keys': [ValidationConsts.OCC_AVG],
                                                                                                                     'tc_to_validate': MRCConsts.TRIMMING_DATA_QUEUE_NUM,
                                                                                                                     'allowed_deviation': tc_occ_allowed_deviation})
                tc3_occ_threshold = {ValidationConsts.OCC_AVG: MRCConsts.MRC_RETRANSMISSION_TC_OCC_AVG_THRESHOLD}
                additional_validations['validate_tc3_occ_below_threshold'] = Validation(validate_per_tc, {'tc_occ_threshold': tc3_occ_threshold,
                                                                                                          'tc_to_validate': MRCConsts.MRC_RETRANSMISSION_TC_NUM,
                                                                                                          'tolerance': None,
                                                                                                          'port_group_name_to_validate_list': []})
                if drop_over_max_sample_port is None:
                    additional_validations['compare_pg_to_reference'] = Validation(compare_pg_to_reference, {'reference_json': ipv6_validation_json,
                                                                                                             'pg_keys': [ValidationConsts.OCC_AVG],
                                                                                                             'pg_to_validate': MRCConsts.PG_LIST,
                                                                                                             'allowed_deviation': MRCConsts.HEADROOM_ALLOWED_DEVIATION,
                                                                                                             'pg_buffer_type': ValidationConsts.PG_BUFFER_DATAFRAME})

                if not is_redmine_issue_active([4743477])[0]:
                    additional_validations['compare_latency_to_reference'] = Validation(compare_latency_to_reference, {'reference_json': ipv6_validation_json,
                                                                                                                       'latency_keys': [ValidationConsts.TC_AVG_LATENCY],
                                                                                                                       'tc_to_validate': MRCConsts.TRIMMING_ELEGABLE_QUEUE_NUM,
                                                                                                                       'allowed_deviation': MRCConsts.LATENCY_ALLOWED_DEVIATION})
        if drop_over_max_sample_port is not None:
            additional_validations['validate_drop_over_max_watermark'] = Validation(
                validate_drop_over_max_watermark,
                {'players': self.players, 'sample_port': drop_over_max_sample_port})
        return additional_validations

    def get_many_to_few_additional_validations(self, egress_ports, tc_threshold):
        additional_validations = {}
        if not is_redmine_issue_active([4668758])[0]:
            max_watermark_th = self.get_max_watermark_th(num_of_congested_ports=len(egress_ports), num_of_congested_tc=len(MRCConsts.TRIMMING_ELEGABLE_QUEUE_NUM))
            additional_validations = {
                'validate_max_watermark_on_trimming_queue': Validation(validate_per_tc, {'tc_occ_threshold': max_watermark_th,
                                                                                         'tc_to_validate': MRCConsts.TRIMMING_QUEUE_NUM,
                                                                                         'tolerance': MRCConsts.MAX_WATERMARK_BY_ALPHA_TOLERANCE,
                                                                                         'port_group_name_to_validate_list': [MRCConsts.EGRESS_PORT_GROUP_NAME]}),
                'validate_max_watermark_on_data_queues': Validation(validate_per_tc, {'tc_occ_threshold': tc_threshold,
                                                                                      'tc_to_validate': MRCConsts.TRIMMING_ELEGABLE_QUEUE_NUM,
                                                                                      'tolerance': None,
                                                                                      'port_group_name_to_validate_list': [MRCConsts.EGRESS_PORT_GROUP_NAME]})
            }
        return additional_validations

    def get_many_to_one_additional_validations(self, traffic_type):
        additional_validations = self.get_additional_validations(traffic_type)
        return additional_validations

    def get_trimming_bw_threshold(self, traffic_type):
        bw_threshold = {
            MRCConsts.EGRESS_PORT_GROUP_NAME: {ValidationConsts.TX: min(self.shaper_value, MRCConsts.DUT_TX_UTIL_TH),
                                               ValidationConsts.RX: None},
            MRCConsts.INGRESS_PORT_GROUP_NAME: {ValidationConsts.TX: None,
                                                ValidationConsts.RX: self.shaper_value}
        }
        if traffic_type == MRCConsts.TRAFFIC_TYPE_SRV6:
            bw_threshold[MRCConsts.EGRESS_PORT_GROUP_NAME][ValidationConsts.TX] = MRCConsts.TRIMMING_SRV6_DUT_TX_UTIL_TH
        if is_redmine_issue_active([4667031])[0]:
            bw_threshold[ValidationConsts.VALIDATION_KEY] = (ValidationConsts.TX_BW_AVG, ValidationConsts.RX_BW_AVG)
        if is_redmine_issue_active([4762193])[0] and isinstance(self.cli_object, NvueCli):
            bw_threshold[MRCConsts.EGRESS_PORT_GROUP_NAME][ValidationConsts.TX] = MRCConsts.CL_TRIMMING_DUT_TX_UTIL_TH
        return bw_threshold

    def validate_trimmed_untrimmed_dropped_percentages(self, test_name, egress_ports, trimming_queue, drop_queues, violations_list, pairing_df=None, duration=None):
        trimmed_untrimmed_dropped_percentages = self.cli_object.trimming.validate_trimmed_untrimmed_dropped_percentages(egress_ports, trimming_queue=trimming_queue,
                                                                                                                        drop_queues=drop_queues,
                                                                                                                        violations_list=violations_list,
                                                                                                                        duration=duration, pairing_df=pairing_df)
        self.cli_object.trimming.validate_trimming_counters(egress_ports, violations_list)
        add_test_mongo_metadata(test_name, {MongoDbConsts.TRIMMED_UNTRIMMED_DROPPED_PERCENTAGES: trimmed_untrimmed_dropped_percentages})
        return trimmed_untrimmed_dropped_percentages

    def get_many_to_one_tc_occ_threshold(self):
        tc_occ_threshold = None
        if not is_redmine_issue_active([4668758])[0] and isinstance(self.cli_object, NvueCli):
            tc_occ_threshold = MRCConsts.MANY_TO_ONE_TRAFFIC_TC_OCC_TH
        return tc_occ_threshold
