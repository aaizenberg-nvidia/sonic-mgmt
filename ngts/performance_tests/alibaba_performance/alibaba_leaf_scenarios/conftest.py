"""

conftest.py

Defines the methods and fixtures which will be used by pytest for only performance setups.

"""
import pytest
import logging
import allure
import copy
import os
import ipaddress
from dataclasses import dataclass
from ngts.constants.constants import BugHandlerConst, CliType
from ngts.helpers.performance.performance_setup_helpers import (save_base_configuration,
                                                                restore_basic_configuration,
                                                                apply_test_configuration,
                                                                skip_test_on_unsupported_os)
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.performance_tests.conftest import get_all_players_ports
from ngts.constants.performance_constants import PerfConsts, SPCXRAConsts, MongoDbConsts, MultiNosSharedData
from ngts.helpers.performance.performance_db_helpers import get_perf_test_name, add_test_mongo_metadata
from ngts.helpers.performance.traffic_helpers import generate_incremental_addresses

logger = logging.getLogger()


@pytest.fixture(scope='module', autouse=True)
def skip_test_conditionally(players):
    skip_test_on_unsupported_os(players['dut']['cli'], CliType.NVUE)
    yield


@dataclass(frozen=True)
class PacketSizeKey:
    """Data class for packet size mapping key - frozen to make it hash able"""
    test_id: str
    is_ipv4: bool
    is_ipv6: bool


# Test ID constants
TEST_ID_SHAPER_97_5_AR_ENABLED_SPLIT_4_64K_DIPS = "shaper_97.5_pkt_size_1900_ar_enabled_split_4_host_ports_64000_dips_to_host_64000_dips_to_spine"
TEST_ID_SHAPER_99_9_AR_ENABLED_SPLIT_4_128_DIPS = "shaper_99.9_pkt_size_4096_ar_enabled_split_4_host_ports_128_dips_to_host_60_dips_to_spine"
TEST_ID_SHAPER_97_5_AR_DISABLED_SPLIT_4_64K_DIPS = "shaper_97.5_pkt_size_1900_ar_disabled_split_4_host_ports_64000_dips_to_host_64000_dips_to_spine"
TEST_ID_SHAPER_97_5_AR_DISABLED_SPLIT_2_64K_DIPS = "shaper_97.5_pkt_size_2000_ar_disabled_split_2_host_ports_64000_dips_to_host_64000_dips_to_spine"
TEST_ID_SUPER_SPINE_TO_LEAF_AR_ENABLED_SPLIT_2_64K_DIPS = "super_spine_to_leaf_ar_enabled_split_2_host_ports_64000_dips_to_host_64000_dips_to_spine"
TEST_ID_ALI_PWS_SPINE_AR_ENABLED_SPLIT_2 = "ali_pws_spine_ar_enabled_split_2_host_ports"
TEST_ID_ALI_PWS_SPINE_AR_ENABLED_SPLIT_4 = "ali_pws_spine_ar_enabled_split_4_host_ports"
TEST_ID_ALI_PWS_SPINE_AR_ENABLED_ASYM_SPLIT = "ali_pws_spine_ar_enabled_asymmetric_split"
ALI_PWS_TESTS_LIST = [TEST_ID_ALI_PWS_SPINE_AR_ENABLED_SPLIT_2, TEST_ID_ALI_PWS_SPINE_AR_ENABLED_SPLIT_4, TEST_ID_ALI_PWS_SPINE_AR_ENABLED_ASYM_SPLIT]
HIGH_NUM_DIPS = 64000

# Dictionary mapping PacketSizeKey to packet_size
PACKET_SIZE_MAPPING = {
    PacketSizeKey(TEST_ID_SHAPER_97_5_AR_ENABLED_SPLIT_4_64K_DIPS, True, False): 2000,
    PacketSizeKey(TEST_ID_SHAPER_97_5_AR_ENABLED_SPLIT_4_64K_DIPS, False, True): 2000,
    PacketSizeKey(TEST_ID_SHAPER_97_5_AR_ENABLED_SPLIT_4_64K_DIPS, True, True): 2100,

    PacketSizeKey(TEST_ID_SHAPER_99_9_AR_ENABLED_SPLIT_4_128_DIPS, True, False): PerfConsts.PACKET_SIZE_4K,
    PacketSizeKey(TEST_ID_SHAPER_99_9_AR_ENABLED_SPLIT_4_128_DIPS, False, True): PerfConsts.PACKET_SIZE_4K,
    PacketSizeKey(TEST_ID_SHAPER_99_9_AR_ENABLED_SPLIT_4_128_DIPS, True, True): PerfConsts.PACKET_SIZE_4K,

    PacketSizeKey(TEST_ID_SHAPER_97_5_AR_DISABLED_SPLIT_4_64K_DIPS, True, False): 2100,
    PacketSizeKey(TEST_ID_SHAPER_97_5_AR_DISABLED_SPLIT_4_64K_DIPS, False, True): 2100,
    PacketSizeKey(TEST_ID_SHAPER_97_5_AR_DISABLED_SPLIT_4_64K_DIPS, True, True): PerfConsts.PACKET_SIZE_4K,

    PacketSizeKey(TEST_ID_SHAPER_97_5_AR_DISABLED_SPLIT_2_64K_DIPS, True, False): 2100,
    PacketSizeKey(TEST_ID_SHAPER_97_5_AR_DISABLED_SPLIT_2_64K_DIPS, False, True): 2100,
    PacketSizeKey(TEST_ID_SHAPER_97_5_AR_DISABLED_SPLIT_2_64K_DIPS, True, True): PerfConsts.PACKET_SIZE_4K,

    PacketSizeKey(TEST_ID_SUPER_SPINE_TO_LEAF_AR_ENABLED_SPLIT_2_64K_DIPS, True, False): 2300,
    PacketSizeKey(TEST_ID_SUPER_SPINE_TO_LEAF_AR_ENABLED_SPLIT_2_64K_DIPS, False, True): 2300,
    PacketSizeKey(TEST_ID_SUPER_SPINE_TO_LEAF_AR_ENABLED_SPLIT_2_64K_DIPS, True, True): 2400,
}


def get_packet_size_for_test(test_id, is_ipv4, is_ipv6):
    """
    Get the packet size for a specific test configuration.

    Args:
        test_id (str): The test identifier
        is_ipv4 (bool): True if IPv4 is enabled
        is_ipv6 (bool): True if IPv6 is enabled

    Returns:
        int: The packet size for this configuration, or None if not found
    """
    key = PacketSizeKey(test_id, is_ipv4, is_ipv6)
    return PACKET_SIZE_MAPPING.get(key)


TESTS_SCENARIO = "alibaba_performance"


@dataclass
class TestParameters:
    """Data class to hold test parameters"""
    shaper_value: float
    ar_enabled: bool
    split_host_ports: int
    split_left: int
    num_left_dips: int
    num_right_dips: int
    is_leaf_scenario: bool
    traffic_function: callable
    adjust_buffer_config: bool
    test_id: str


@dataclass
class TestIPCombinations:
    """Data class to hold ip combinations"""
    ipv4_enabled: str
    ipv6_enabled: str


@pytest.fixture(scope='class')
def test_params(request):
    """Fixture to provide test parameters with class scope"""
    return request.param


@pytest.fixture(scope='class')
def shaper_value(test_params):
    """Extract shaper_value from test_params"""
    return test_params.shaper_value


@pytest.fixture(scope='class')
def ar_enabled(test_params):
    """Extract ar_enabled from test_params"""
    return test_params.ar_enabled


@pytest.fixture(scope='class')
def split_host_ports(test_params):
    """Extract split_host_ports from test_params"""
    return test_params.split_host_ports


@pytest.fixture(scope='class')
def split_left(test_params):
    """Extract split_left from test_params"""
    return test_params.split_left


@pytest.fixture(scope='class')
def num_left_dips(test_params):
    """Extract num_left_packets from test_params"""
    return test_params.num_left_dips


@pytest.fixture(scope='class')
def num_right_dips(test_params):
    """Extract num_right_packets from test_params"""
    return test_params.num_right_dips


@pytest.fixture(scope='class')
def is_leaf_scenario(test_params):
    """Extract is_leaf_scenario from test_params"""
    return test_params.is_leaf_scenario


@pytest.fixture(scope='class')
def test_id(test_params):
    """Extract test_id from test_params"""
    return test_params.test_id


@pytest.fixture(scope='class')
def ip_combinations(request):
    """Fixture to provide test parameters with class scope"""
    return request.param


@pytest.fixture(scope='class')
def ipv4_enabled(ip_combinations):
    """Extract ipv4_enabled from ip_combinations"""
    return ip_combinations.ipv4_enabled


@pytest.fixture(scope='class')
def adjust_buffer_config(test_params):
    """Extract adjust_buffer_config from test_params"""
    return test_params.adjust_buffer_config


@pytest.fixture(scope='class')
def ipv6_enabled(ip_combinations):
    """Extract ipv6_enabled from ip_combinations"""
    return ip_combinations.ipv6_enabled


def get_super_spine_to_leaf_port_groups(players, conf_args, split_right=2, split_left=2, num_of_super_spine_ports=None):
    """
    Configure port groups for super spine to leaf network topology testing.

    This function organizes network ports into logical groups for performance testing in a super spine-leaf
    architecture. It splits ports on both traffic generator and the DUT,
    then assigns them to super-spine and leaf port groups based on the specified configuration.
    Note that in this scenario, for every super spine port, there are 3 leaf ports.

    Args:
        players (dict): Dictionary containing the test players (left-tg, right-tg, dut) with their port information.
        conf_args (dict): Configuration arguments dictionary that will be updated with port group assignments.
        split_right (int, optional): Number of splits for right-side ports. Defaults to 2.
        split_left (int, optional): Number of splits for left-side ports. Defaults to 2.
        num_of_super_spine_ports (int, optional): Number of ports to allocate for super-spine connections. Defaults to half of left side ports.

    Returns:
        dict: Updated conf_args dictionary with PORT_GROUPS key containing the organized port assignments:
            - LEFT_TG_ALIAS: Contains super_spine and leaf port groups
            - RIGHT_TG_ALIAS: Contains leaf port groups
            - DUT_ALIAS: Contains super_spine and leaf port groups

    """
    all_ports_after_split = get_all_players_ports(players, split_right, split_left)
    num_of_super_spine_ports = num_of_super_spine_ports if num_of_super_spine_ports else len(all_ports_after_split[PerfConsts.DUT_ALIAS]["left_split_ports"]) // 2

    conf_args[PerfConsts.PORT_GROUPS] = {
        PerfConsts.LEFT_TG_ALIAS: {
            PerfConsts.SUPER_SPINE_PORTS_GROUP: all_ports_after_split[PerfConsts.LEFT_TG_ALIAS]["unconnected_ports"][:num_of_super_spine_ports],
            PerfConsts.LEAF_PORTS_GROUP: all_ports_after_split[PerfConsts.LEFT_TG_ALIAS]["unconnected_ports"][num_of_super_spine_ports:]
        },
        PerfConsts.RIGHT_TG_ALIAS: {
            PerfConsts.LEAF_PORTS_GROUP: all_ports_after_split[PerfConsts.RIGHT_TG_ALIAS]["unconnected_ports"]
        },
        PerfConsts.DUT_ALIAS: {
            'left_split_ports': all_ports_after_split[PerfConsts.DUT_ALIAS]["left_split_ports"][:num_of_super_spine_ports],
            'right_split_ports': all_ports_after_split[PerfConsts.DUT_ALIAS]["left_split_ports"][num_of_super_spine_ports:] + all_ports_after_split[PerfConsts.DUT_ALIAS]["right_split_ports"]}
    }
    return conf_args


def adjust_num_dips_for_chip(num_dips, chip_type):
    """Double DIP count on SPC6 unless already at the high-DIP scale."""
    if chip_type == "SPC6" and num_dips != HIGH_NUM_DIPS:
        return num_dips * 2
    return num_dips


@pytest.fixture(scope='class', autouse=True)
def conf_args(players, chip_type, test_id, shaper_value, ar_enabled, split_host_ports, split_left, num_left_dips, num_right_dips, ipv4_enabled, ipv6_enabled, is_leaf_scenario, adjust_buffer_config):

    num_left_dips = adjust_num_dips_for_chip(num_left_dips, chip_type)
    num_right_dips = adjust_num_dips_for_chip(num_right_dips, chip_type)

    conf_args = {"auto_buffer_mode": "False",
                 "congestion_thresh_lo": PerfConsts.LOW_AR_THRESHOLD_SPC6 if chip_type == "SPC6" else PerfConsts.LOW_AR_THRESHOLD,
                 "two_sided_ar": False,
                 "ar_enabled": ar_enabled,
                 "split_right": split_host_ports,
                 "split_left": split_left,
                 "host": PerfConsts.RIGHT_TG_ALIAS,
                 "spine": PerfConsts.LEFT_TG_ALIAS,
                 "get_acl_dump": False,
                 "scenario": TESTS_SCENARIO,
                 "packet_size": get_packet_size_for_test(test_id, ipv4_enabled == "ipv4_enabled", ipv6_enabled == "ipv6_enabled"),
                 "left_num_packets": SPCXRAConsts.PACKET_NUM_800G_x1_WITH_INCREMENTAL_DIPS[chip_type] // 2,
                 "left_num_dip_to_send": num_left_dips,
                 "right_num_packets": SPCXRAConsts.PACKET_NUM_800G_x1_WITH_INCREMENTAL_DIPS[chip_type] // split_host_ports,
                 "right_num_dip_to_send": num_right_dips,
                 "num_routes_ipv4": 64000,
                 "num_routes_ipv6": 64000,
                 "disable_locality": True,
                 "apply_acl": True,
                 "set_lpm_root": True,
                 "hash_type": "crc",
                 "ecmp_size": 4096,
                 "from_leaf_dest_mac": "00:00:00:00:10:60",
                 "from_spine_dest_mac": "00:01:02:03:04:06",
                 "left_ingress_dmac": "00:01:02:03:04:06",
                 "right_ingress_dmac": "00:00:00:00:10:60",
                 "is_ipv4": ipv4_enabled == "ipv4_enabled",
                 "is_ipv6": ipv6_enabled == "ipv6_enabled",
                 "ipv4_source_ip": "4.4.4.4",
                 "ipv6_source_ip": "192:168:0:0:0:0:0:1",
                 "dip_left_to_right_start_ipv4_list": ["10.0.1.0"],  # DIP received in right
                 "dip_left_to_right_start_ipv6_list": ["2001:db8::2"],  # DIP received in right
                 "dip_right_to_left_start_ipv4_list": ["192.168.1.0"],  # DIP received in left
                 "dip_right_to_left_start_ipv6_list": ["192:168:5:1:1:1:2:0"],  # DIP received in left
                 "neigh_mac_left_to_right_start": "00:00:00:00:10:70",  # Neigh MAC for DIP received in right. ingres rif
                 "neigh_mac_right_to_left_start": "00:01:02:03:04:08",  # Neigh MAC for DIP received in left. ingress rif
                 "is_leaf_scenario": is_leaf_scenario,
                 "shaper_value": shaper_value,
                 "adjust_buffer_config": adjust_buffer_config,
                 "get_occ_per_port": False,
                 "params": None
                 }

    if conf_args['is_ipv4'] and conf_args['is_ipv6']:
        conf_args['left_num_packets'] = conf_args['left_num_packets'] // 2
        conf_args['right_num_packets'] = conf_args['right_num_packets'] // 2

    # Reduce scale in case of 64K dips in IPv6- not enough space in KVD.
    if ipv4_enabled == "ipv4_disabled" and ipv6_enabled == "ipv6_enabled" and conf_args['left_num_dip_to_send'] == HIGH_NUM_DIPS:
        conf_args['left_num_dip_to_send'] = 55000
        conf_args['right_num_dip_to_send'] = 55000
        conf_args['num_routes_ipv6'] = 55000

    if test_id == TEST_ID_SUPER_SPINE_TO_LEAF_AR_ENABLED_SPLIT_2_64K_DIPS:
        conf_args = get_super_spine_to_leaf_port_groups(players, conf_args)
        conf_args['dip_left_to_right_start_ipv4_list'] = ["10.0.1.0", "192.168.1.0"]
        conf_args['dip_left_to_right_start_ipv6_list'] = ["2001:db8::2", "192:168:5:1:1:1:2:0"]
        conf_args['dip_right_to_left_start_ipv4_list'] = ["192.168.1.0", "10.0.1.0"]
        conf_args['dip_right_to_left_start_ipv6_list'] = ["192:168:5:1:1:1:2:0", "2001:db8::2"]

    if test_id in ALI_PWS_TESTS_LIST:
        arch_defined_pipeline_latency_size = 700  # (99% occupancy + max_occupancy) / 2, calculation based on arch definition, results checked manually
        arch_defined_max_borrowed_delta = 600  # (max_watermark - 99% occupancy) * 2, calculation based on arch definition, results checked manually

        conf_args['pg_buffer_configs'] = {
            PerfConsts.LEFT_TG_ALIAS: {
                "pg_list": [4],
                "pipeline_latency_size": (arch_defined_pipeline_latency_size // split_left) * 2,
                "max_borrowed_delta": (arch_defined_max_borrowed_delta // split_left) * 2
            },
            PerfConsts.RIGHT_TG_ALIAS: {
                "pg_list": [4],
                "pipeline_latency_size": (arch_defined_pipeline_latency_size // split_host_ports) * 2,
                "max_borrowed_delta": (arch_defined_max_borrowed_delta // split_host_ports) * 2
            }
        }

        conf_args['num_routes_ipv4'] = max(num_left_dips, num_right_dips)
        conf_args['num_routes_ipv6'] = max(num_left_dips, num_right_dips)
        conf_args['disable_locality'] = False
        conf_args['apply_acl'] = False
        conf_args['sdk_tg_config_script_name'] = "IngressAclTG"
    else:
        conf_args['sdk_tg_config_script_name'] = "SpcxTG"

    right_side_ipv4_to_mac_list = generate_incremental_addresses(conf_args["neigh_mac_right_to_left_start"],
                                                                 conf_args["dip_right_to_left_start_ipv4_list"][0],
                                                                 int(conf_args["num_routes_ipv4"]))

    left_side_ipv4_to_mac_list = generate_incremental_addresses(conf_args["neigh_mac_left_to_right_start"],
                                                                conf_args["dip_left_to_right_start_ipv4_list"][0],
                                                                int(conf_args["num_routes_ipv4"]))

    right_side_ipv6_to_mac_list = generate_incremental_addresses(conf_args["neigh_mac_right_to_left_start"],
                                                                 conf_args["dip_right_to_left_start_ipv6_list"][0],
                                                                 int(conf_args["num_routes_ipv6"]))

    left_side_ipv6_to_mac_list = generate_incremental_addresses(conf_args["neigh_mac_left_to_right_start"],
                                                                conf_args["dip_left_to_right_start_ipv6_list"][0],
                                                                int(conf_args["num_routes_ipv6"]))

    conf_args["ip_to_mac_dict"] = {"left": {"ipv4": left_side_ipv4_to_mac_list, "ipv6": left_side_ipv6_to_mac_list},
                                   "right": {"ipv4": right_side_ipv4_to_mac_list, "ipv6": right_side_ipv6_to_mac_list}}

    conf_args["left_uc_route_ipv4_list"] = []
    conf_args["right_uc_route_ipv4_list"] = []
    conf_args["left_uc_route_ipv6_list"] = []
    conf_args["right_uc_route_ipv6_list"] = []

    if test_id in ALI_PWS_TESTS_LIST:
        left_uc_route_ipv4_list = [left_side_ipv4_to_mac_list[i][0] for i in range(num_left_dips)]
        right_uc_route_ipv4_list = [right_side_ipv4_to_mac_list[i][0] for i in range(num_right_dips)]
        left_uc_route_ipv6_list = [left_side_ipv6_to_mac_list[i][0] for i in range(num_left_dips)]
        right_uc_route_ipv6_list = [right_side_ipv6_to_mac_list[i][0] for i in range(num_right_dips)]

        conf_args["left_uc_route_ipv4_list"] = left_uc_route_ipv4_list
        conf_args["right_uc_route_ipv4_list"] = right_uc_route_ipv4_list
        conf_args["left_uc_route_ipv6_list"] = left_uc_route_ipv6_list
        conf_args["right_uc_route_ipv6_list"] = right_uc_route_ipv6_list

    return conf_args


def move_alibaba_acl_dump(players):
    """
    Move the Alibaba ACL dump to the DUT, using the shared JSON file.

    Args:
        players (dict): Dictionary of test players containing DUT configurations.

    Raises:
        FileNotFoundError: If the ACL dump source file does not exist.
    """
    acl_dump_path = '/tmp'
    acl_dump_name = 'acl_list.txt'
    acl_dump_original_file = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", TESTS_SCENARIO, "acl_list.txt")

    logger.info(f"ACL dump source file path: {acl_dump_original_file}")
    logger.info(f"ACL dump file exists: {os.path.exists(acl_dump_original_file)}")
    if os.path.exists(acl_dump_original_file):
        logger.info(f"ACL dump file size: {os.path.getsize(acl_dump_original_file)} bytes")
    else:
        logger.error(f"ACL dump source file does not exist at: {acl_dump_original_file}")
        raise FileNotFoundError(f"ACL dump source file not found: {acl_dump_original_file}")

    dut_alias = PerfConsts.PERF_SETUP_DUT_ALIASES[0]
    player_cli_obj = players[dut_alias]['cli']
    player_cli_obj.performance.write_shared_json(key=MultiNosSharedData.ALIBABA_ACL_DUMP_PATH, data=acl_dump_path)
    player_cli_obj.performance.write_shared_json(key=MultiNosSharedData.ALIBABA_ACL_DUMP_NAME, data=acl_dump_name)
    player_cli_obj.performance.engine.copy_file(
        source_file=acl_dump_original_file,
        file_system=acl_dump_path,
        dest_file=acl_dump_name,
        overwrite_file=True,
        verify_file=True
    )


def _apply_alibaba_leaf_skip_conditions(test_params, ip_combinations):
    """
    Apply all skip conditions for alibaba leaf scenario tests.
    Must be called before apply_test_configuration so skips happen without configuring the DUT.
    """
    if test_params.test_id in ALI_PWS_TESTS_LIST and ip_combinations.ipv6_enabled == "ipv6_enabled":
        pytest.skip(f"Skipping PWS test {test_params.test_id} with IPv6 enabled")

    if test_params.test_id == TEST_ID_SHAPER_99_9_AR_ENABLED_SPLIT_4_128_DIPS:
        pytest.skip("Skipping test for 99.9 percent shaper value")

    if (not test_params.ar_enabled and ip_combinations.ipv4_enabled == "ipv4_enabled" and
            ip_combinations.ipv6_enabled == "ipv6_enabled" and is_redmine_issue_active([4662379])[0]):
        pytest.skip("Skipping test for non-AR scenario, IPv4 enabled and IPv6 enabled")

    if (test_params.test_id == TEST_ID_SUPER_SPINE_TO_LEAF_AR_ENABLED_SPLIT_2_64K_DIPS and
        ip_combinations.ipv4_enabled == "ipv4_enabled" and ip_combinations.ipv6_enabled == "ipv6_enabled" and
            is_redmine_issue_active([4662379])[0]):
        pytest.skip("Skipping Ali scenario 3, IPv4 enabled and IPv6 enabled")

    if is_redmine_issue_active([4662378])[0] and test_params.test_id == TEST_ID_SHAPER_99_9_AR_ENABLED_SPLIT_4_128_DIPS:
        pytest.skip("Skipping test for 99.9 percent shaper value")

    if (is_redmine_issue_active([4662379])[0] and not test_params.ar_enabled and
            ip_combinations.ipv4_enabled == "ipv4_enabled" and ip_combinations.ipv6_enabled == "ipv6_enabled"):
        pytest.skip("Skipping test for non-AR scenario, IPv4 enabled and IPv6 enabled")


@pytest.fixture(scope='class', autouse=True)
def basic_setup_configuration(players, conf_args, test_params, ip_combinations):
    _apply_alibaba_leaf_skip_conditions(test_params, ip_combinations)

    try:
        with allure.step('Save Players initial Configuration'):
            save_base_configuration(players)
        with allure.step("Apply Test configuration on all Players"):
            move_alibaba_acl_dump(players)
            apply_test_configuration(players, scenario=TESTS_SCENARIO, conf_args=conf_args)
        yield
    except Exception as e:
        raise e
    finally:
        with allure.step('Restore Base Configuration on all Players'):
            restore_basic_configuration(players)


@pytest.fixture(scope='function', autouse=True)
def update_test_mongo_metadata(request, port_group_df):
    test_name = get_perf_test_name(request)
    add_test_mongo_metadata(test_name, {MongoDbConsts.CONF_NAME: test_name,
                                        MongoDbConsts.PORT_GROUP_DF: port_group_df})
    yield
