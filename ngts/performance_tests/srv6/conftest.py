import copy
import os
import random
import pytest
from ngts.cli_wrappers.nvue.nvue_cli import NvueCli
from ngts.constants.performance_constants import PerfConsts, MongoDbConsts, MRCConsts, ValidationConsts
from ngts.constants.constants import CliType
from ngts.cli_wrappers.sonic.sonic_cli import SonicCli
from ngts.helpers.performance.performance_setup_helpers import (skip_test_on_unsupported_os,
                                                                restore_default_port_group_df_on_dut)


@pytest.fixture(scope='module', autouse=True)
def skip_test_conditionally(players):
    skip_test_on_unsupported_os(players['dut']['cli'], CliType.DVS)
    yield


@pytest.fixture(scope='function', autouse=True)
def restore_default_port_group_df_after_test(players):
    """Restore default left/right validator port groups after each SRv6 test."""
    yield
    restore_default_port_group_df_on_dut(players[PerfConsts.DUT_ALIAS]['cli'].performance)


def get_upstream_downstream_port_group_df(players, upstream_ports_num, downstream_ports_num, sequential=False):
    """
    This function creates a port group dataframe for the upstream and downstream groups.
    The ports are selected consecutively from the left and right ports.

    Parameters:
        players: dictionary containing the players
        upstream_ports_num: number of ports in the upstream group
        downstream_ports_num: number of ports in the downstream group
        sequential: if True, select the first N ports instead of random sampling.
            This ensures all 8 logical ports of each physical OSFP panel port are utilized.
    Returns:
        upstream: list of ports in the upstream group
        downstream: list of ports in the downstream group
        port_group_df: list of dictionaries containing the port and the port group name
    """
    port_group_df = []
    upstream, downstream = players['dut']['cli'].performance.get_upstream_downstream_ports_dict(upstream_ports_num, downstream_ports_num,
                                                                                                sequential=sequential)
    sdk_port_list_upstream = players['dut']['cli'].performance.get_sdk_ports(upstream)
    sdk_port_list_downstream = players['dut']['cli'].performance.get_sdk_ports(downstream)
    for port in sdk_port_list_upstream:
        port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.BISECTION_UPSTREAM_PORT_GROUP_NAME})
    for port in sdk_port_list_downstream:
        port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.BISECTION_DOWNSTREAM_PORT_GROUP_NAME})
    return upstream, downstream, port_group_df


def get_upstream_downstream_groups_port_group_df(players, upstream_ports_num, downstream_ports_num, num_of_groups):
    """
    This function creates a port group dataframe for the upstream and downstream groups.
    The ports are selected consecutively from the left and right ports.

    Parameters:
        players: dictionary containing the players
        upstream_ports_num: number of ports in the upstream group
        downstream_ports_num: number of ports in the downstream group
    Returns:
        upstream: list of ports in the upstream group
        downstream: list of ports in the downstream group
        port_group_df: list of dictionaries containing the port and the port group name
    """
    port_group_df = []
    ports = players['dut']['cli'].performance.get_right_left_ports_dict()
    left_ports = copy.deepcopy(ports["left_ports"])
    right_ports = copy.deepcopy(ports["right_ports"])
    upstream_group_size = upstream_ports_num * num_of_groups
    downstream_group_size = downstream_ports_num * num_of_groups
    upstream_start_index = random.randint(0, len(left_ports) - upstream_group_size)
    upstream_end_index = upstream_start_index + upstream_group_size
    downstream_start_index = random.randint(0, len(right_ports) - downstream_group_size)
    downstream_end_index = downstream_start_index + downstream_group_size
    upstream = left_ports[upstream_start_index:upstream_end_index]
    downstream = right_ports[downstream_start_index:downstream_end_index]
    upstream_groups = split_into_subsets(upstream, upstream_ports_num)
    downstream_groups = split_into_subsets(downstream, downstream_ports_num)
    for i in range(num_of_groups):
        sdk_port_list_upstream = players['dut']['cli'].performance.get_sdk_ports(upstream_groups[i])
        sdk_port_list_downstream = players['dut']['cli'].performance.get_sdk_ports(downstream_groups[i])
        for port in sdk_port_list_upstream:
            port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.LEAF_ROUND_ROBIN_UPSTREAM_PORT_GROUP_NAME})
        for port in sdk_port_list_downstream:
            port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.LEAF_ROUND_ROBIN_DOWNSTREAM_PORT_GROUP_NAME})
    return upstream_groups, downstream_groups, port_group_df


def split_into_subsets(lst, subset_size):
    return [lst[i:i + subset_size] for i in range(0, len(lst), subset_size)]


def get_spine_many_to_few_port_group_df(players, chip_type, M):
    port_group_df = []
    num_of_ingress_ports = MRCConsts.T1_MANY_TO_FEW_INGRESS_PORTS_NUM_BY_CHIP_TYPE[chip_type]
    ports = players['dut']['cli'].performance.get_dut_ports()
    dut_ports = copy.deepcopy(ports)
    random.shuffle(dut_ports)
    egress_ports_num = len(dut_ports) // M
    egress_ports = dut_ports[:egress_ports_num]
    ingress_ports = ports[:num_of_ingress_ports]
    sdk_port_list_egress = players['dut']['cli'].performance.get_sdk_ports(egress_ports)
    sdk_port_list_ingress = players['dut']['cli'].performance.get_sdk_ports(ingress_ports)
    for port in sdk_port_list_egress:
        port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.EGRESS_PORT_GROUP_NAME})
    for port in sdk_port_list_ingress:
        port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.INGRESS_PORT_GROUP_NAME})
    return egress_ports, ingress_ports, port_group_df


def get_victim_flow_port_group_df(players):
    port_group_df = []
    victim_ports_num = MRCConsts.VICTIM_PORTS_NUM
    upstream_ports_num = 2 * victim_ports_num - 1
    downstream_ports_num = victim_ports_num + 1
    upstream, downstream, upstream_downstream_port_group_df = get_upstream_downstream_port_group_df(players, upstream_ports_num, downstream_ports_num)
    bisection_left, many_to_one_ingress_ports = upstream[:victim_ports_num], upstream[victim_ports_num:2 * victim_ports_num - 1]
    bisection_right, many_to_one_egress_ports = downstream[:victim_ports_num], downstream[victim_ports_num:victim_ports_num + 1]
    many_to_one_ingress_ports.extend(many_to_one_egress_ports)
    sdk_port_list_bisection_left = players['dut']['cli'].performance.get_sdk_ports(bisection_left)
    sdk_port_list_bisection_right = players['dut']['cli'].performance.get_sdk_ports(bisection_right)
    sdk_port_list_many_to_one_ingress_ports = players['dut']['cli'].performance.get_sdk_ports(many_to_one_ingress_ports)
    sdk_port_list_many_to_one_egress_ports = players['dut']['cli'].performance.get_sdk_ports(many_to_one_egress_ports)
    for port in sdk_port_list_bisection_left:
        port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.BISECTION_DOWNSTREAM_PORT_GROUP_NAME})
    for port in sdk_port_list_bisection_right:
        port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.BISECTION_UPSTREAM_PORT_GROUP_NAME})
    for port in sdk_port_list_many_to_one_ingress_ports:
        port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.INGRESS_PORT_GROUP_NAME})
    for port in sdk_port_list_many_to_one_egress_ports:
        port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.EGRESS_PORT_GROUP_NAME})
    return bisection_left, bisection_right, many_to_one_ingress_ports, many_to_one_egress_ports, port_group_df


def get_spine_downstream_groups_port_group_df(players, downstream_ports_num, num_of_groups):
    port_group_df = []
    ports = players['dut']['cli'].performance.get_right_left_ports_dict()
    left_ports = copy.deepcopy(ports["left_ports"])
    right_ports = copy.deepcopy(ports["right_ports"])
    downstream_group_size = downstream_ports_num * num_of_groups
    downstream_start_index = random.randint(0, len(right_ports) - downstream_group_size)
    downstream_end_index = downstream_start_index + downstream_group_size
    downstream_1 = left_ports[downstream_start_index:downstream_end_index]
    downstream_2 = right_ports[downstream_start_index:downstream_end_index]
    downstream_groups_1 = split_into_subsets(downstream_1, downstream_ports_num)
    downstream_groups_2 = split_into_subsets(downstream_2, downstream_ports_num)
    for i in range(num_of_groups):
        sdk_port_list_downstream_groups_1 = players['dut']['cli'].performance.get_sdk_ports(downstream_groups_1[i])
        sdk_port_list_downstream_groups_2 = players['dut']['cli'].performance.get_sdk_ports(downstream_groups_2[i])
        for port in sdk_port_list_downstream_groups_1:
            port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.SPINE_ROUND_ROBIN_DOWNSTREAM_PORT_GROUP_1_NAME})
        for port in sdk_port_list_downstream_groups_2:
            port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.SPINE_ROUND_ROBIN_DOWNSTREAM_PORT_GROUP_2_NAME})
    return downstream_groups_1, downstream_groups_2, port_group_df


@pytest.fixture(scope="function", autouse=False)
def cleanup_ports_shaper(cli_objects):
    yield
    for tg_alias in PerfConsts.TG_ALIAS_LIST:
        cli_objects[tg_alias].performance.configure_ports_shaper(shaper_value=MRCConsts.SHAPER_VALUE_AFTER_TEST)


def get_srv6_tests_skip_condition(cli_obj, actual_chip_type):
    unsupported_chip_type = "None"
    if isinstance(cli_obj, SonicCli):
        unsupported_chip_type = "SPC4"
    condition = actual_chip_type == unsupported_chip_type
    skip_message = f"This test is not supported on {unsupported_chip_type} on cli type {cli_obj.__class__.__name__}"
    return condition, skip_message


@pytest.fixture(scope="class", autouse=True)
def shaper_value(request, players):
    request.getfixturevalue('basic_setup_configuration')
    cli_obj = players['dut']['cli']
    if isinstance(cli_obj, SonicCli):
        return PerfConsts.SRV6_SONIC_OS_SHAPER_VALUE
    elif isinstance(cli_obj, NvueCli):
        return PerfConsts.SRV6_NVUE_OS_SHAPER_VALUE
    else:
        raise ValueError(f"Unsupported cli type: {cli_obj.__class__.__name__}")
