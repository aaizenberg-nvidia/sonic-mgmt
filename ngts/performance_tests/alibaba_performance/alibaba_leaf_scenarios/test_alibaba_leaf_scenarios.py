import allure
import logging
import os
import pytest

from ngts.constants.constants import InfraConst
from ngts.constants.performance_constants import PerfConsts, BwFairnessThreshold
from ngts.helpers.performance.performance_setup_helpers import (configure_mloops, run_traffic, run_validation,
                                                                get_topology_obj, create_acl_dump, ValidationConfig,
                                                                stop_traffic, configure_incremental_dips_on_tg,
                                                                create_occ_watermark_dump,
                                                                modify_pg_buffer_for_connected_ports)
from ngts.helpers.performance.performance_db_helpers import get_perf_test_name
from ngts.constants.performance_constants import SPCXRAConsts
from ngts.performance_tests.alibaba_performance.conftest import (get_alibaba_leaf_traffic,
                                                                 get_alibaba_super_spine_to_leaf_traffic,
                                                                 get_pws_traffic)
from ngts.performance_tests.alibaba_performance.alibaba_leaf_scenarios.conftest import (
    TESTS_SCENARIO, TestIPCombinations, TestParameters,
    TEST_ID_SHAPER_97_5_AR_ENABLED_SPLIT_4_64K_DIPS,
    TEST_ID_SHAPER_99_9_AR_ENABLED_SPLIT_4_128_DIPS,
    TEST_ID_SHAPER_97_5_AR_DISABLED_SPLIT_4_64K_DIPS,
    TEST_ID_SHAPER_97_5_AR_DISABLED_SPLIT_2_64K_DIPS,
    TEST_ID_SUPER_SPINE_TO_LEAF_AR_ENABLED_SPLIT_2_64K_DIPS,
    TEST_ID_ALI_PWS_SPINE_AR_ENABLED_SPLIT_2,
    TEST_ID_ALI_PWS_SPINE_AR_ENABLED_SPLIT_4,
    TEST_ID_ALI_PWS_SPINE_AR_ENABLED_ASYM_SPLIT,
    ALI_PWS_TESTS_LIST)
from ngts.constants.constants import BugHandlerConst
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active

logger = logging.getLogger(__name__)


def _get_traffic_jsons(test_params, players, conf_args, chip_type):
    """Build traffic JSONs, forwarding incremental_dip_num_packets for leaf traffic only."""
    if test_params.is_leaf_scenario:
        return get_alibaba_leaf_traffic(
            players, conf_args,
            incremental_dip_num_packets=SPCXRAConsts.PACKET_NUM_400G_x2[chip_type])
    return test_params.traffic_function(players, conf_args)


@pytest.mark.parametrize(
    "test_params",
    [
        TestParameters(shaper_value=0.975, ar_enabled=True, split_host_ports=4, split_left=2, num_left_dips=128, num_right_dips=60, is_leaf_scenario=True, traffic_function=get_alibaba_leaf_traffic, adjust_buffer_config=False, test_id=TEST_ID_SHAPER_97_5_AR_ENABLED_SPLIT_4_64K_DIPS),
        TestParameters(shaper_value=0.999, ar_enabled=True, split_host_ports=4, split_left=2, num_left_dips=128, num_right_dips=60, is_leaf_scenario=True, traffic_function=get_alibaba_leaf_traffic, adjust_buffer_config=False, test_id=TEST_ID_SHAPER_99_9_AR_ENABLED_SPLIT_4_128_DIPS),
        TestParameters(shaper_value=0.975, ar_enabled=False, split_host_ports=4, split_left=2, num_left_dips=64000, num_right_dips=64000, is_leaf_scenario=True, traffic_function=get_alibaba_leaf_traffic, adjust_buffer_config=False, test_id=TEST_ID_SHAPER_97_5_AR_DISABLED_SPLIT_4_64K_DIPS),
        TestParameters(shaper_value=0.975, ar_enabled=False, split_host_ports=2, split_left=2, num_left_dips=64000, num_right_dips=64000, is_leaf_scenario=True, traffic_function=get_alibaba_leaf_traffic, adjust_buffer_config=False, test_id=TEST_ID_SHAPER_97_5_AR_DISABLED_SPLIT_2_64K_DIPS),
        TestParameters(shaper_value=round(0.975 * 0.975, 3), ar_enabled=True, split_host_ports=2, split_left=2, num_left_dips=64000, num_right_dips=64000, is_leaf_scenario=False, traffic_function=get_alibaba_super_spine_to_leaf_traffic, adjust_buffer_config=False, test_id=TEST_ID_SUPER_SPINE_TO_LEAF_AR_ENABLED_SPLIT_2_64K_DIPS),
        TestParameters(shaper_value=0.975, ar_enabled=True, split_host_ports=2, split_left=2, num_left_dips=64, num_right_dips=64, is_leaf_scenario=False, traffic_function=get_pws_traffic, adjust_buffer_config=True, test_id=TEST_ID_ALI_PWS_SPINE_AR_ENABLED_SPLIT_2),
        TestParameters(shaper_value=0.975, ar_enabled=True, split_host_ports=4, split_left=4, num_left_dips=128, num_right_dips=128, is_leaf_scenario=False, traffic_function=get_pws_traffic, adjust_buffer_config=True, test_id=TEST_ID_ALI_PWS_SPINE_AR_ENABLED_SPLIT_4),
        TestParameters(shaper_value=0.975, ar_enabled=True, split_host_ports=4, split_left=2, num_left_dips=64, num_right_dips=128, is_leaf_scenario=False, traffic_function=get_pws_traffic, adjust_buffer_config=True, test_id=TEST_ID_ALI_PWS_SPINE_AR_ENABLED_ASYM_SPLIT),
    ],
    indirect=True
)
@pytest.mark.parametrize(
    "ip_combinations",
    [
        TestIPCombinations(ipv4_enabled="ipv4_enabled", ipv6_enabled="ipv6_disabled"),
        TestIPCombinations(ipv4_enabled="ipv4_disabled", ipv6_enabled="ipv6_enabled"),
        TestIPCombinations(ipv4_enabled="ipv4_enabled", ipv6_enabled="ipv6_enabled"),
    ],
    indirect=True
)
class TestAlibabaLeafScenario:
    @pytest.fixture(autouse=True)
    def setup(self, players, engines, power_thresholds_by_chip_type, chip_type):
        self.topology_obj = get_topology_obj(players)
        self.players = players
        self.engines = engines
        self.cli_object = self.players['dut']['cli']
        self.scenario = TESTS_SCENARIO
        self.power_thresholds_by_chip_type = power_thresholds_by_chip_type
        self.chip_type = chip_type
        self.ip = InfraConst.IPV4
        self.is_ipv6 = False

    def get_expected_bw(self, test_params, conf_args):
        """
        Calculate expected bandwidth thresholds based on test parameters.

        Args:
            test_params (TestParameters): Test parameters object
            conf_args (dict): Configuration arguments

        Returns:
            dict: Expected bandwidth dictionary with tx/rx values for left_split_ports and right_split_ports
        """
        if test_params.test_id == TEST_ID_SUPER_SPINE_TO_LEAF_AR_ENABLED_SPLIT_2_64K_DIPS:
            super_spine_to_leaf_percentage = 0.2
            leaf_to_super_spine_percentage = 0.2
            leaf_to_leaf_percentage = 0.75
            leaf_ports_to_super_spine_ports_ratio = 3

            expected_tx_leaf = leaf_to_leaf_percentage + super_spine_to_leaf_percentage * (1 / leaf_ports_to_super_spine_ports_ratio)
            expected_rx_leaf = leaf_to_leaf_percentage + leaf_to_super_spine_percentage
            expected_tx_super_spine = leaf_to_super_spine_percentage * leaf_ports_to_super_spine_ports_ratio
            expected_rx_super_spine = super_spine_to_leaf_percentage

            expected_bw = {
                "left_split_ports": {"tx": expected_tx_super_spine * 0.95, "rx": expected_rx_super_spine * 0.95},
                "right_split_ports": {"tx": expected_tx_leaf * 0.95, "rx": expected_rx_leaf * 0.95},
            }
        elif test_params.test_id in ALI_PWS_TESTS_LIST:
            expected_bw = {
                "connected_ports": {"tx": (0.7 / conf_args['split_right']) * 2, "rx": (0.7 / conf_args['split_right']) * 2},
                "unconnected_ports": {"tx": (0.7 / conf_args['split_right']) * 2, "rx": (0.7 / conf_args['split_right']) * 2}
            }
        else:
            bw_th = SPCXRAConsts.get_min_line_rate_bw_threshold_ibm(self.chip_type)
            expected_bw = {
                "left_split_ports": {"tx": bw_th, "rx": bw_th},
                "right_split_ports": {"tx": (bw_th / conf_args['split_right']) * 2, "rx": (bw_th / conf_args['split_right']) * 2}
            }
        return expected_bw

    @allure.title('alibaba_performance_leaf_scenario. Added dynamically in test body')
    @allure.description('Added dynamically in test body')
    def test_alibaba_performance_leaf_scenario(self, request, conf_args, ip_combinations, test_params):
        logging.info(f"Testing with IPv4={conf_args['is_ipv4']}, IPv6={conf_args['is_ipv6']}")
        test_name = get_perf_test_name(request)

        with allure.step(f"Testing with IPv4={conf_args['is_ipv4']}, IPv6={conf_args['is_ipv6']}"):
            with allure.step("Adding dynamic description to allure report"):
                scenario_name = (f"Alibaba Performance real life leaf Scenario. "
                                 f"{32 * conf_args['split_left']} X {32 * conf_args['split_right']} ports. "
                                 f"shaper value: {conf_args['shaper_value']}. "
                                 f"packet size: {conf_args['packet_size']}. "
                                 f"{'with' if conf_args['is_ipv6'] else 'without'} IPv6. "
                                 f"{'with' if conf_args['is_ipv4'] else 'without'} IPv4. "
                                 f"{'with' if conf_args['ar_enabled'] else 'without'} AR. "
                                 f"{conf_args['left_num_dip_to_send']} dips to host. "
                                 f"{conf_args['right_num_dip_to_send']} dips to spine. "
                                 )
                if test_params.test_id in ALI_PWS_TESTS_LIST:
                    scenario_name = f"PWS scenario.\n" + scenario_name

            scenario_description = f"{scenario_name} "
            f"{'with' if conf_args['set_lpm_root'] else 'without'} LPM root. "
            f"{'with' if conf_args['disable_locality'] else 'without'} locality. "

            allure.dynamic.title(scenario_name)
            allure.dynamic.description(scenario_description)

        if test_params.test_id not in ALI_PWS_TESTS_LIST:
            with allure.step("Create incremental dips"):
                configure_incremental_dips_on_tg(self.players)

        with allure.step("Get Alibaba traffic"):
            traffic_jsons = _get_traffic_jsons(test_params, self.players, conf_args, self.chip_type)

        with allure.step("Run Traffic on all the ports"):
            run_traffic(self.players, self.scenario, traffic_jsons)

        if test_params.test_id in ALI_PWS_TESTS_LIST:
            with allure.step("Modify PG buffer configuration for connected ports"):
                modified_count = modify_pg_buffer_for_connected_ports(self.players)

            with allure.step(f"PG buffer modification completed: {modified_count} TG(s) configured"):
                logging.info(f"PG buffer modification completed: {modified_count} TG(s) configured")

        if conf_args['get_acl_dump']:
            with allure.step("Creating ACL dump"):
                acl_dump = create_acl_dump(self.players)
                logging.info(f"Creating ACL dump {acl_dump}")

                if acl_dump:
                    allure.attach(acl_dump, name="ACL_Dump", attachment_type=allure.attachment_type.TEXT)

        with allure.step("Verifying the traffic"):
            expected_bw = self.get_expected_bw(test_params, conf_args)
            if test_params.test_id not in ALI_PWS_TESTS_LIST:
                ignore_counter_list = ['tx_ecn_marked_tc_3']
            elif test_params.test_id in ALI_PWS_TESTS_LIST:
                ignore_counter_list = ['egress_vlan_membership']

            if is_redmine_issue_active([5145920])[0]:
                self.players[PerfConsts.DUT_ALIAS]['cli'].performance.clear_syslog()

            config = ValidationConfig(players=self.players, test_name=test_name, scenario=self.scenario,
                                      chip_type=self.chip_type,
                                      bw_threshold=expected_bw,
                                      bw_fairness_threshold_per_port_group=BwFairnessThreshold.get_bw_fairness_threshold_per_port_group(expected_bw),
                                      power_threshold=self.power_thresholds_by_chip_type,
                                      skip_first_counters_iteration=True,
                                      tc_occ_threshold=None,
                                      ignore_counter_list=ignore_counter_list,
                                      players_to_be_validated=PerfConsts.PERF_SETUP_TG_ALIASES if test_params.test_id in ALI_PWS_TESTS_LIST else PerfConsts.PERF_SETUP_DUT_ALIASES)
            run_validation(config)

        if conf_args['get_occ_per_port']:
            with allure.step("Copy occupancy and watermark dump from DUT"):
                hostname = self.players[PerfConsts.DUT_ALIAS]['cli'].chassis.get_hostname()
                tar_file_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", "occ_watermark_dumps",
                                             self.scenario, f"{hostname}_{test_name}_occ_headroom_per_port.tar.gz")
                os.makedirs(os.path.dirname(tar_file_path), exist_ok=True)
                create_occ_watermark_dump(self.players, sonic_mgmt_path=tar_file_path)

    def _run_single_test_with_packet_size(self, packet_size, conf_args, test_params, test_name, create_acl_dump_flag=True):
        """
        Run a single test iteration with the specified packet size.

        Args:
            packet_size (int): The packet size to test
            conf_args (dict): Configuration arguments
            test_params (TestParameters): Test parameters object
            test_name (str): Test name for reporting
            create_acl_dump_flag (bool): Whether to create ACL dump for this iteration

        Returns:
            tuple: (bool, str or None) - (test_passed, acl_dump_content)
        """
        acl_dump_content = None
        try:
            # Update packet size in configuration
            modified_conf_args = conf_args.copy()
            modified_conf_args['packet_size'] = packet_size

            with allure.step(f"Testing packet size: {packet_size}"):
                with allure.step(f"Get Alibaba traffic with packet size {packet_size}"):
                    traffic_jsons = _get_traffic_jsons(test_params, self.players, modified_conf_args, self.chip_type)

                with allure.step(f"Run Traffic on all the ports with packet size {packet_size}"):
                    run_traffic(self.players, self.scenario, traffic_jsons)

                if create_acl_dump_flag:
                    with allure.step(f"Creating ACL dump for packet size {packet_size}"):
                        acl_dump_content = create_acl_dump(self.players)
                        logging.info(f"Creating ACL dump {acl_dump_content}")

                with allure.step(f"Verifying the traffic with packet size {packet_size}"):
                    expected_bw = self.get_expected_bw(test_params, conf_args)
                    ignore_counter_list = ['tx_ecn_marked_tc_3']
                    config = ValidationConfig(players=self.players, test_name=test_name, scenario=self.scenario,
                                              chip_type=self.chip_type,
                                              bw_threshold=expected_bw,
                                              bw_fairness_threshold_per_port_group=BwFairnessThreshold.get_bw_fairness_threshold_per_port_group(expected_bw),
                                              power_threshold=self.power_thresholds_by_chip_type,
                                              skip_first_counters_iteration=True,
                                              ignore_counter_list=ignore_counter_list)
                    run_validation(config)

                    logger.info(f"Test PASSED with packet size: {packet_size}")
                    return True, acl_dump_content

        except Exception as e:
            logger.info(f"Test FAILED with packet size: {packet_size}, Error: {str(e)}")
            return False, acl_dump_content
        finally:
            # Stop traffic to clean up
            try:
                stop_traffic(self.players)
                configure_mloops(self.players)
            except Exception as cleanup_error:
                logger.warning(f"Failed to stop traffic during cleanup: {cleanup_error}")

    def _binary_search_min_packet_size(self, conf_args, test_params, test_name, min_size=1500, max_size=4200, step=100):
        """
        Perform binary search to find the minimum packet size where test passes.

        Args:
            conf_args (dict): Configuration arguments
            test_params (TestParameters): Test parameters object
            test_name (str): Test name for reporting
            min_size (int): Minimum packet size to test
            max_size (int): Maximum packet size to test
            step (int): Step size (must be multiple of this value)

        Returns:
            tuple: (int or None, str or None) - (minimum_packet_size, relevant_acl_dump_content)
        """
        # Generate list of valid packet sizes (multiples of step)
        packet_sizes = list(range(min_size, max_size + 1, step))
        acl_dumps = {}  # Dictionary to store ACL dumps by packet size

        with allure.step(f"Starting binary search for minimum packet size in range [{min_size}, {max_size}] with step {step}"):
            logger.info(f"Binary search packet sizes: {packet_sizes}")

            # First check if the maximum packet size passes
            test_passed, acl_dump_content = self._run_single_test_with_packet_size(max_size, conf_args, test_params, test_name)
            if acl_dump_content:
                acl_dumps[max_size] = acl_dump_content
            if not test_passed:
                # If max packet size fails, return None for min_passing_size and the max ACL dump
                logger.error(f"Maximum packet size {max_size} failed, cannot find minimum passing size")
                relevant_acl_dump = acl_dumps.get(max_size)
                return None, relevant_acl_dump

            # Binary search implementation
            left, right = 0, len(packet_sizes) - 1
            min_passing_size = None

            while left <= right:
                mid = (left + right) // 2
                current_packet_size = packet_sizes[mid]

                with allure.step(f"Binary search iteration: testing packet size {current_packet_size} (index {mid})"):
                    logger.info(f"Testing packet size: {current_packet_size} (left={left}, right={right}, mid={mid})")

                    test_passed, acl_dump_content = self._run_single_test_with_packet_size(current_packet_size, conf_args, test_params, test_name)
                    if acl_dump_content:
                        acl_dumps[current_packet_size] = acl_dump_content

                    if test_passed:
                        # Test passed, try smaller packet sizes
                        min_passing_size = current_packet_size
                        right = mid - 1
                        logger.info(f"Packet size {current_packet_size} PASSED, searching for smaller sizes")
                    else:
                        # Test failed, try larger packet sizes
                        left = mid + 1
                        logger.info(f"Packet size {current_packet_size} FAILED, searching for larger sizes")

            # Return the relevant ACL dump path
            final_packet_size = min_passing_size if min_passing_size is not None else max_size
            relevant_acl_dump = acl_dumps.get(final_packet_size)

            logger.info(f"Binary search completed. Final packet size: {final_packet_size}, ACL dumps collected: {list(acl_dumps.keys())}")

            return min_passing_size, relevant_acl_dump

    @pytest.mark.skip(reason="Helper test to get the min packet size for Ali scenarios.")
    @allure.title('alibaba_performance_leaf_scenario_binary_search. Added dynamically in test body')
    @allure.description('Binary search to find minimum packet size where test passes')
    def test_alibaba_performance_leaf_scenario_binary_search(self, request, conf_args, ip_combinations, test_params):
        logging.info(f"Testing with IPv4={conf_args['is_ipv4']}, IPv6={conf_args['is_ipv6']}")
        test_name = get_perf_test_name(request)

        with allure.step("Adding dynamic description to allure report"):
            scenario_name = (f"Alibaba Performance Binary Search Leaf Scenario. "
                             f"{32 * conf_args['split_left']} X {32 * conf_args['split_right']} ports. "
                             f"shaper value: {conf_args['shaper_value']}. "
                             f"Binary search packet size range: [1500, 4200]. "
                             f"{'with' if conf_args['is_ipv6'] else 'without'} IPv6. "
                             f"{'with' if conf_args['is_ipv4'] else 'without'} IPv4. "
                             f"{'with' if conf_args['ar_enabled'] else 'without'} AR. "
                             f"{conf_args['left_num_dip_to_send']} dips to host. "
                             f"{conf_args['right_num_dip_to_send']} dips to spine. "
                             f"{'with' if conf_args['adjust_buffer_config'] else 'without'} adjust buffer config. "
                             )

            scenario_description = f"{scenario_name} "
            f"{'with' if conf_args['set_lpm_root'] else 'without'} LPM root. "
            f"{'with' if conf_args['disable_locality'] else 'without'} locality. "

            allure.dynamic.title(scenario_name)
            allure.dynamic.description(scenario_description)

        with allure.step(f"Create incremental dips"):
            configure_incremental_dips_on_tg(self.players)

        # Perform binary search to find minimum packet size
        min_packet_size, acl_dump_content = self._binary_search_min_packet_size(conf_args, test_params, test_name)

        # Publish ACL dump if available (before any potential test failure)
        if acl_dump_content:
            with allure.step(f"Publishing ACL dump for packet size {min_packet_size or 4200}"):
                allure.attach(acl_dump_content, name="Final_ACL_Dump", attachment_type=allure.attachment_type.TEXT)

        if min_packet_size is not None:
            with allure.step(f"Minimum passing packet size found: {min_packet_size}"):
                logger.info(f"Binary search completed successfully. Minimum packet size: {min_packet_size}")
                allure.attach(
                    str(min_packet_size),
                    name="Minimum Passing Packet Size",
                    attachment_type=allure.attachment_type.TEXT
                )
        else:
            with allure.step("No passing packet size found in the tested range"):
                logger.error("Binary search failed to find any passing packet size")
                pytest.fail("No packet size in range [1500, 4200] resulted in a passing test")
