"""

conftest.py

Defines the methods and fixtures which will be used by pytest for only performance setups.

"""
import os
from dataclasses import dataclass
from ngts.constants.constants import BugHandlerConst
from ngts.helpers.performance.traffic_helpers import create_empty_json_traffic_file
from ngts.performance_tests.conftest import get_all_players_ports
from ngts.performance_tests.lossy_lossless.conftest import create_lossy_lossless_json_traffic_file
import pytest
import logging
import allure
from typing import Optional
import json
import re
from ngts.helpers.general_helper import get_pytest_test_name
from ngts.helpers.performance.performance_setup_helpers import (save_base_configuration,
                                                                restore_basic_configuration,
                                                                apply_test_configuration)
from ngts.constants.performance_constants import PerfConsts, MongoDbConsts, BwFairnessThreshold
from ngts.helpers.performance.performance_db_helpers import get_perf_test_name, add_test_mongo_metadata

logger = logging.getLogger()
TESTS_SCENARIO = "lossy_lossless"

# Define for port group naming pattern
TRAFFIC_PORTS_GROUP_NAME_TEMPLATE = "traffic_from_{}_ports_group"


@dataclass
class TestConfig:
    """Configuration for lossy/lossless test parameters."""
    num_of_traffic_ports: int
    num_of_lossy_packets: int
    num_of_lossless_packets: int
    packet_size: int
    split_left: int
    split_right: int
    auto_buffer_mode: bool
    fboss_enabled: bool
    adjust_buffer_config: bool
    test_id: str
    num_downlink_ports: Optional[int] = None
    bw_fairness_threshold: Optional[BwFairnessThreshold] = None


@pytest.fixture(scope='class')
def test_config(request):
    """Fixture to provide test parameters with class scope"""
    return request.param


@pytest.fixture(scope='class')
def packet_size(test_config):
    """Extract packet_size from test_config"""
    return test_config.packet_size


@pytest.fixture(scope='class')
def split_left(test_config):
    """Extract split_left from test_config"""
    return test_config.split_left


@pytest.fixture(scope='class')
def split_right(test_config):
    """Extract split_right from test_config"""
    return test_config.split_right


@pytest.fixture(scope='class')
def num_of_traffic_ports(test_config):
    """Extract num_of_traffic_ports from test_config"""
    return test_config.num_of_traffic_ports


@pytest.fixture(scope='class')
def num_of_lossy_packets(test_config):
    """Extract num_of_lossy_packets from test_config"""
    return test_config.num_of_lossy_packets


@pytest.fixture(scope='class')
def num_of_lossless_packets(test_config):
    """Extract num_of_lossless_packets from test_config"""
    return test_config.num_of_lossless_packets


@pytest.fixture(scope='class')
def fboss_enabled(test_config):
    """Extract fboss_enabled from test_config"""
    return test_config.fboss_enabled


@pytest.fixture(scope='class')
def auto_buffer_mode(test_config):
    """Extract auto_buffer_mode from test_config"""
    return test_config.auto_buffer_mode


@pytest.fixture(scope='class')
def adjust_buffer_config(test_config):
    """Extract adjust_buffer_config from test_config"""
    return test_config.adjust_buffer_config


@pytest.fixture(scope='class')
def num_downlink_ports(test_config):
    """Extract num_downlink_ports from test_config"""
    return test_config.num_downlink_ports


@pytest.fixture(scope='class', autouse=True)
def basic_setup_configuration(players, conf_args):
    try:
        with allure.step('Save Players initial Configuration'):
            save_base_configuration(players)
        with allure.step("Apply Test configuration on all Players"):
            apply_test_configuration(players, scenario=TESTS_SCENARIO, conf_args=conf_args)
        yield
    except Exception as e:
        raise e
    finally:
        with allure.step('Restore Base Configuration on all Players'):
            restore_basic_configuration(players)


def get_many_to_1_traffic(conf_args, num_lossy_packets, num_lossless_packets, num_traffic_ports, template_suite="traffic_packets_json_files"):
    """
    Generate traffic configuration for many-to-1 lossy/lossless scenario.

    Args:
        conf_args: Dictionary containing configuration parameters
        num_lossy_packets: Number of lossy packets to generate
        num_lossless_packets: Number of lossless packets to generate
        num_traffic_ports: Number of traffic ports to use for the test
        template_suite: Directory containing traffic template files

    Returns:
        Dictionary mapping player aliases to their traffic configuration file paths
    """
    traffic_jsons = {}

    left_conf_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                  PerfConsts.DEFAULT_PERF_TEMPLATES_DIR, TESTS_SCENARIO, "dvs",
                                  f"{PerfConsts.LEFT_TG_ALIAS}_conf.json")
    right_conf_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                   PerfConsts.DEFAULT_PERF_TEMPLATES_DIR, TESTS_SCENARIO, "dvs",
                                   f"{PerfConsts.RIGHT_TG_ALIAS}_conf.json")

    with open(left_conf_path) as f:
        left_conf_json = json.load(f)
    with open(right_conf_path) as f:
        right_conf_json = json.load(f)

    tg_alias_list = [PerfConsts.LEFT_TG_ALIAS, PerfConsts.RIGHT_TG_ALIAS] if conf_args["num_of_right_ports"] > 0 else [PerfConsts.LEFT_TG_ALIAS]

    for tg_alias in tg_alias_list:
        traffic_script_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                                           TESTS_SCENARIO, f"{tg_alias}_lossy_lossless_scenario_4_many_to_1.json")

        direction = "left" if tg_alias == PerfConsts.LEFT_TG_ALIAS else "right"
        port_group_key = TRAFFIC_PORTS_GROUP_NAME_TEMPLATE.format(conf_args[f"num_of_{direction}_ports"])
        ingress_ports = conf_args[PerfConsts.PORT_GROUPS][tg_alias][port_group_key]
        traffic_parameters = {
            "ports": ingress_ports,
            "MAC": {"src": left_conf_json["smac"], "dst": left_conf_json[f"left_mac"] if direction == "left" else right_conf_json[f"right_mac"]},
            "IP": {"src": left_conf_json["source_ip"], "dst": left_conf_json["left_dst_ip"]},
            "UDP": {"src": PerfConsts.UDP_SOURCE_PORT, "dst": PerfConsts.ROCE_PORT},
            "AR": PerfConsts.ADAPTIVE_ROUTING_ENABLED,
            "packet_size": conf_args["packet_size"],
            "num_packets": conf_args["left_num_packets"],
            "is_ipv6": conf_args["is_ipv6"],
            "lossy_dscp_value": 34,
            "lossless_dscp_value": 26
        }

        create_lossy_lossless_json_traffic_file(player_alias=tg_alias,
                                                traffic_parameters=traffic_parameters,
                                                json_path=traffic_script_path,
                                                num_lossy_packets=num_lossy_packets,
                                                num_lossless_packets=num_lossless_packets,
                                                chip_type=conf_args["chip_type"])

        traffic_jsons[tg_alias] = traffic_script_path

    if conf_args["num_of_right_ports"] == 0:
        empty_traffic_script_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                                                 TESTS_SCENARIO, "empty_file.json")

        create_empty_json_traffic_file(empty_traffic_script_path)
        traffic_jsons[PerfConsts.RIGHT_TG_ALIAS] = empty_traffic_script_path

    return traffic_jsons


@pytest.fixture(scope='class', autouse=True)
def conf_args(players, chip_type, test_config, split_left, split_right, num_of_traffic_ports, packet_size,
              auto_buffer_mode, fboss_enabled, num_downlink_ports, adjust_buffer_config):
    """
    This function alters all of the jinja template files.

    Note: SPC5 doesn't support 800G. In that case, uses doubled splits and adds effective_test_id with 400G_on_SPC5.
    """
    if chip_type == "SPC5" and (split_left == 1 or split_right == 1):
        effective_split_left = split_left * 2
        effective_split_right = split_right * 2
        effective_test_id = f"{test_config.test_id}_400G_on_SPC5"
    else:
        effective_split_left = split_left
        effective_split_right = split_right
        effective_test_id = test_config.test_id

    all_ports_after_split = get_all_players_ports(players, effective_split_right, effective_split_left)
    num_of_left_ports = min(num_of_traffic_ports, len(all_ports_after_split[PerfConsts.LEFT_TG_ALIAS]["unconnected_ports"]))
    num_of_right_ports = 0 if num_of_traffic_ports == num_of_left_ports else num_of_traffic_ports - num_of_left_ports

    num_of_downlink_ports = num_downlink_ports if num_downlink_ports else num_of_left_ports + num_of_right_ports

    conf_args = {"congestion_thresh_lo": PerfConsts.LOW_AR_THRESHOLD_SPC6 if chip_type == "SPC6" else PerfConsts.LOW_AR_THRESHOLD,
                 "sdk_test_name": "LossyLosslessDynamicPortsTo1PortDut",
                 "auto_buffer_mode": auto_buffer_mode,
                 "packet_size": packet_size,
                 "is_ipv6": False,
                 "fboss_enabled": fboss_enabled,
                 "adjust_buffer_config": adjust_buffer_config,
                 "num_of_left_ports": num_of_left_ports,
                 "num_of_right_ports": num_of_right_ports,
                 "split_right": effective_split_right,
                 "split_left": effective_split_left,
                 "effective_test_id": effective_test_id,
                 "num_of_traffic_ports": num_of_traffic_ports,
                 "num_of_downlink_ports": num_of_downlink_ports,
                 "chip_type": chip_type,
                 "two_sided_ar": False,
                 "scenario": TESTS_SCENARIO,
                 "left_num_packets": 0,  # Override in the test
                 "right_num_packets": 0,  # Override in the test
                 PerfConsts.PORT_GROUPS: {
                     PerfConsts.LEFT_TG_ALIAS: {
                         TRAFFIC_PORTS_GROUP_NAME_TEMPLATE.format(num_of_left_ports): all_ports_after_split[PerfConsts.LEFT_TG_ALIAS]["unconnected_ports"][:num_of_left_ports],
                         "dont_care_ports": all_ports_after_split[PerfConsts.LEFT_TG_ALIAS]["unconnected_ports"][num_of_left_ports:]
                     },
                     PerfConsts.RIGHT_TG_ALIAS: {
                         "single_port_group": all_ports_after_split[PerfConsts.RIGHT_TG_ALIAS]["unconnected_ports"][:1],
                         TRAFFIC_PORTS_GROUP_NAME_TEMPLATE.format(num_of_right_ports): all_ports_after_split[PerfConsts.RIGHT_TG_ALIAS]["unconnected_ports"][1:num_of_right_ports + 1],
                         "dont_care_ports": all_ports_after_split[PerfConsts.RIGHT_TG_ALIAS]["unconnected_ports"][1 + num_of_right_ports:]
                     },
                     PerfConsts.DUT_ALIAS: {
                         TRAFFIC_PORTS_GROUP_NAME_TEMPLATE.format(num_of_traffic_ports): all_ports_after_split[PerfConsts.DUT_ALIAS]["left_split_ports"][:num_of_traffic_ports] +
                         all_ports_after_split[PerfConsts.DUT_ALIAS]["right_split_ports"][1:num_of_right_ports + 1],
                         "single_port_group": all_ports_after_split[PerfConsts.DUT_ALIAS]["right_split_ports"][:1],
                         "dont_care_ports": (
                             all_ports_after_split[PerfConsts.DUT_ALIAS]["left_split_ports"][num_of_left_ports:] +
                             all_ports_after_split[PerfConsts.DUT_ALIAS]["right_split_ports"][num_of_right_ports + 1:]
                         )
                     }
                 }
                 }
    return conf_args
