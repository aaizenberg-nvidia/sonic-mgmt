"""

conftest.py

Defines the methods and fixtures which will be used by pytest for only performance setups.

"""
from dataclasses import dataclass
import pytest
import logging
import allure
import copy
import os
from ngts.constants.constants import BugHandlerConst, CliType, InfraConst
from ngts.helpers.performance.performance_setup_helpers import (save_base_configuration,
                                                                restore_basic_configuration,
                                                                apply_test_configuration, skip_test_on_unsupported_os)
from ngts.constants.performance_constants import PerfConsts, SPCXRAConsts, MongoDbConsts
from ngts.helpers.performance.performance_db_helpers import get_perf_test_name, add_test_mongo_metadata
from ngts.helpers.performance.traffic_helpers import create_json_traffic_file_with_stream_list, create_json_traffic_stream
from ngts.performance_tests.spcx_ra.conftest import get_spine_to_leaf_stream_list
import re


logger = logging.getLogger()
TESTS_SCENARIO = "spcx_ra"


@dataclass
class AlibabaScenarioToconfiguration:
    scenario_name: str
    packet_size: int
    num_left_packets: int
    num_right_packets: int
    ecmp_type_stateless: bool
    ecmp_size: int
    create_acls: bool
    create_goto_acl: bool
    two_sided_ar: bool
    num_left_packets_spc6: int = None

    def get_num_left_packets(self, chip_type):
        """
        Return the number of left TG packets to use for the given chip type.

        Args:
            chip_type (str): Chip type identifier (e.g. "SPC5", "SPC6").

        Returns:
            int: The SPC6-specific value when defined and running on SPC6, otherwise the default value.
        """
        if chip_type == "SPC6" and self.num_left_packets_spc6 is not None:
            return self.num_left_packets_spc6
        return self.num_left_packets


@pytest.fixture(scope='module', autouse=True)
def skip_test_conditionally(players):
    skip_test_on_unsupported_os(players['dut']['cli'], CliType.NVUE)
    yield


def get_conf_args(is_ipv6, chip_type):
    """
    Config args for the test.
    Note that unlike must tests, those test perform DVS_START at the end. Then, we override some variables according
    to the new test.
    """
    conf_args = {
        "auto_buffer_mode": "True" if chip_type == "SPC6" else "False",
        "congestion_thresh_lo": PerfConsts.LOW_AR_THRESHOLD_SPC6 if chip_type == "SPC6" else PerfConsts.LOW_AR_THRESHOLD,
        "is_ipv6": is_ipv6,
        "host": PerfConsts.RIGHT_TG_ALIAS,
        "spine": PerfConsts.LEFT_TG_ALIAS,
        "split_right": 2,
        "split_left": 2,
        "scenario": TESTS_SCENARIO,
        "hash_type": "crc",
        "shaper_value": 0.975,
        "goto_acl_destination_port": 81,
        "params": None,
        "two_sided_ar": True,  # overridden in fixture
        "packet_size": PerfConsts.PACKET_SIZE_LIST[0],  # overridden in fixture
        "left_num_packets": SPCXRAConsts.PACKET_NUM_400G_x2[chip_type],  # overridden in fixture
        "right_num_packets": SPCXRAConsts.PACKET_NUM_400G_x2[chip_type],  # overridden in fixture
        "ecmp_type_stateless": True,  # overridden in fixture
        "ecmp_size": 4096,  # overridden in fixture
        "create_acls": False,  # overridden in fixture
        "create_goto_acl": False  # overridden in fixture
    }
    return conf_args


def apply_basic_setup_configuration(is_ipv6, players, chip_type):
    conf_args = get_conf_args(is_ipv6, chip_type)
    with allure.step('Save Players initial Configuration'):
        save_base_configuration(players)
    with allure.step("Apply Test configuration on all Players"):
        apply_test_configuration(players, scenario=TESTS_SCENARIO, conf_args=conf_args)


@pytest.fixture(scope='class')
def basic_setup_configuration(request, players, chip_type):
    is_ipv6 = request.param == InfraConst.IPV6
    try:
        apply_basic_setup_configuration(is_ipv6, players, chip_type)
        yield is_ipv6
    except Exception as e:
        raise e
    finally:
        with allure.step('Restore Base Configuration on all Players'):
            restore_basic_configuration(players)


@pytest.fixture(scope='function', autouse=False)
def alibaba_scenarios_fixture(players, chip_type, scenario_configuration, hash_type, basic_setup_configuration):
    """
    Fixture to apply scenario-specific configuration for each test case.
    Updates conf_args with scenario configuration and applies it to players.
    """
    if scenario_configuration is None:
        raise ValueError("scenario_configuration must be provided")
    is_ipv6 = basic_setup_configuration
    conf_args = get_conf_args(is_ipv6, chip_type)
    try:
        with allure.step("Restore Base Configuration on all Players"):
            restore_basic_configuration(players)

        conf_args.update({
            "packet_size": scenario_configuration.packet_size,
            "left_num_packets": scenario_configuration.get_num_left_packets(chip_type),
            "right_num_packets": scenario_configuration.num_right_packets if chip_type != "SPC6" else int(scenario_configuration.num_right_packets * 1.5),
            "ecmp_type_stateless": scenario_configuration.ecmp_type_stateless,
            "ecmp_size": scenario_configuration.ecmp_size,
            "create_acls": scenario_configuration.create_acls,
            "create_goto_acl": scenario_configuration.create_goto_acl,
            "two_sided_ar": scenario_configuration.two_sided_ar,
            "hash_type": hash_type
        })

        with allure.step("Apply the updated test configuration"):
            apply_test_configuration(players, scenario=TESTS_SCENARIO, conf_args=conf_args)
        return conf_args
    except Exception as e:
        logger.error(f"Error in alibaba_scenarios_fixture: {str(e)}")
        raise


def get_alibaba_host_to_spine_json_traffic_file(player_alias, traffic_parameters, json_path):
    regular_stream = create_json_traffic_stream(player_alias, traffic_parameters, f"{player_alias}_regular_stream",
                                                ip_protocol=PerfConsts.IP_PROTOCOL_TCP)
    stream_list = [regular_stream]

    if traffic_parameters["num_traffic_streams"] == 2:
        traffic_parameters[PerfConsts.IP_PROTOCOL_TCP]["dport"] = traffic_parameters["goto_acl_destination_port"]
        traffic_parameters["num_packets"] = 1
        goto_stream = create_json_traffic_stream(player_alias, traffic_parameters, f"{player_alias}_goto_stream",
                                                 ip_protocol=PerfConsts.IP_PROTOCOL_TCP)
        stream_list.append(goto_stream)

    create_json_traffic_file_with_stream_list(player_alias, traffic_parameters, json_path,
                                              stream_list=stream_list)


def get_alibaba_traffic(players, conf_args, template_suite="traffic_packets_json_files", spine_scenario=False):
    traffic_jsons = {}
    pkt_size = conf_args["packet_size"]
    for player_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
        player_cli_obj = players[player_alias]['cli']
        json_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                                 TESTS_SCENARIO, f"{player_alias}_{TESTS_SCENARIO.replace('/', '_')}_{pkt_size}.json")
        traffic_parameters = player_cli_obj.performance.get_traffic_parameters(scenario=TESTS_SCENARIO,
                                                                               conf_args=conf_args)

        traffic_parameters.pop(PerfConsts.IP_PROTOCOL_UDP)
        traffic_parameters[PerfConsts.IP_PROTOCOL_TCP] = {"sport": PerfConsts.TCP_SOURCE_PORT, "dport": PerfConsts.TCP_DOURCE_PORT}
        traffic_parameters["num_traffic_streams"] = 2 if conf_args["create_goto_acl"] else 1
        traffic_parameters["goto_acl_destination_port"] = conf_args["goto_acl_destination_port"]

        if spine_scenario or player_alias == conf_args["host"]:
            get_alibaba_host_to_spine_json_traffic_file(player_alias=player_alias, traffic_parameters=traffic_parameters, json_path=json_path)
        else:
            get_spine_to_leaf_stream_list(players=players, spine_tg=player_alias, conf_args=conf_args,
                                          traffic_parameters=traffic_parameters, json_path=json_path,
                                          ip_protocol=PerfConsts.IP_PROTOCOL_TCP)
        traffic_jsons[player_alias] = json_path
    return traffic_jsons


@pytest.fixture(scope='function', autouse=True)
def update_test_mongo_metadata(request, port_group_df, scenario_name):
    """
    Fixture to update test metadata in MongoDB.
    Requires scenario_name parameter to be present in the test function.
    """
    test_name = get_perf_test_name(request)
    add_test_mongo_metadata(test_name, {MongoDbConsts.CONF_NAME: scenario_name,
                                        MongoDbConsts.PORT_GROUP_DF: port_group_df})
    yield


def extract_acl_counters(acl_dump, create_acls, create_goto_acl):
    """
    Extract ACL counters from the ACL dump and calculate the goto percentage.

    Args:
        acl_dump (str): The ACL dump string to parse
        create_acls (bool): Whether ACLs were created
        create_goto_acl (bool): Whether goto ACL was created

    Returns:
        tuple: (acl_ar_counter, acl_goto_counter, goto_percentage)
    """
    if create_acls:
        acl_ar_counter = re.search(r'\|.*AR_PACKET_CLASS.*\(Val:(\d+)\)\|', acl_dump).group(1)
        if create_goto_acl:
            acl_goto_counter = re.search(r'\|.*DIP.*\(Val:(\d+)\)\|', acl_dump).group(1)
        else:
            acl_goto_counter = 0
        goto_percentage = ((int(acl_goto_counter) / int(acl_ar_counter)) * 100)
    else:
        acl_ar_counter = 0
        acl_goto_counter = 0
        goto_percentage = 0

    return acl_ar_counter, acl_goto_counter, goto_percentage


@pytest.fixture(scope='function', autouse=True)
def update_test_mongo_metadata(request, port_group_df, scenario_name):
    """
    Fixture to update test metadata in MongoDB.
    Requires scenario_name parameter to be present in the test function.
    """
    test_name = get_perf_test_name(request)
    add_test_mongo_metadata(test_name, {MongoDbConsts.CONF_NAME: scenario_name,
                                        MongoDbConsts.PORT_GROUP_DF: port_group_df})
    yield
