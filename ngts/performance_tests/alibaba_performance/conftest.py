import json
import re
import pytest
import time
import logging
import os
import ipaddress
import copy
from netaddr import EUI
from ngts.constants.performance_constants import PerfConsts, PowerConsts, MongoDbConsts
from ngts.constants.constants import BugHandlerConst
from ngts.helpers.performance.packet_json_generator import PacketGenerator
from ngts.helpers.performance.traffic_helpers import (create_json_traffic_file, create_json_traffic_stream,
                                                      create_json_traffic_file_with_stream_list, create_empty_json_traffic_file, dscp_to_tc)

logger = logging.getLogger()
TESTS_SCENARIO = "alibaba_performance"


def get_alibaba_leaf_traffic(players, conf_args, template_suite="traffic_packets_json_files",
                             incremental_dip_num_packets=None):
    traffic_jsons = {}
    for player_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
        player_cli_obj = players[player_alias]['cli']
        json_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                                 TESTS_SCENARIO, f"{player_alias}_{TESTS_SCENARIO.replace('/', '_')}_{conf_args['packet_size']}.json")
        traffic_parameters = player_cli_obj.performance.get_traffic_parameters(scenario=TESTS_SCENARIO,
                                                                               conf_args=conf_args)

        traffic_parameters["AR"] = PerfConsts.ADAPTIVE_ROUTING_ENABLED if conf_args["ar_enabled"] else PerfConsts.ADAPTIVE_ROUTING_DISABLED
        get_multiple_ip_stream_list(player_alias, traffic_parameters, json_path, conf_args,
                                    incremental_dip_num_packets=incremental_dip_num_packets * 3 if player_alias == PerfConsts.LEFT_TG_ALIAS else incremental_dip_num_packets * 7)
        traffic_jsons[player_alias] = json_path
    return traffic_jsons


def get_multiple_ip_stream_list(spine_tg, traffic_parameters, json_path, conf_args,
                                incremental_dip_num_packets=None):
    direction_from = "left" if spine_tg == PerfConsts.LEFT_TG_ALIAS else "right"
    stream_list = []

    ports_from = f'{direction_from} {"host" if conf_args["host"] == spine_tg else "spine"}'
    ports_to = f'{direction_from} {"spine" if conf_args["spine"] == spine_tg else "host"}'
    direction_to = "right" if direction_from == "left" else "left"

    effective_num_packets = (incremental_dip_num_packets if incremental_dip_num_packets is not None
                             else conf_args.get("incremental_dip_num_packets"))
    if effective_num_packets is not None:
        traffic_parameters["num_packets"] = effective_num_packets

    for traffic_type, traffic_enabled in zip(["ipv4", "ipv6"], [conf_args["is_ipv4"], conf_args["is_ipv6"]]):
        if traffic_enabled:
            traffic_parameters["IP"]["dst"] = conf_args["ip_to_mac_dict"][direction_from][traffic_type][0][0]
            traffic_parameters["IP"]["src"] = conf_args[f"{traffic_type}_source_ip"]
            traffic_parameters["is_ipv6"] = traffic_type == "ipv6"
            stream_name = f"From {direction_from} {ports_from} to {direction_to} {ports_to}. Packet number {0} to {traffic_parameters['IP']['dst']}"
            logger.info(f"Stream name: {stream_name}")
            stream = create_json_traffic_stream(spine_tg, traffic_parameters, stream_name, tc=dscp_to_tc(PerfConsts.DVS_LOSSLESS_TC, 2))
            stream_list.append(stream)
    create_json_traffic_file_with_stream_list(spine_tg, traffic_parameters, json_path, stream_list)


def get_alibaba_super_spine_to_leaf_traffic(players, conf_args, template_suite="traffic_packets_json_files"):
    """
    Generate traffic JSON files for Alibaba super spine to leaf performance testing.

    This function creates traffic pattern files for all traffic generators in a super spine-leaf
    network topology. It generates JSON files containing traffic stream definitions that simulate
    realistic data center traffic patterns between super spine and leaf nodes.

    Args:
        players (dict): Dictionary of test players containing traffic generator configurations.
            Expected structure: {player_alias: {'cli': cli_object, ...}, ...}
        conf_args (dict): Configuration arguments dictionary.
        template_suite (str, optional): Name of the directory containing traffic templates.
            Defaults to "traffic_packets_json_files".

    Returns:
        dict: Dictionary mapping each player alias to its generated traffic JSON file path.
            Structure: {player_alias: json_file_path, ...}

    """
    traffic_jsons = {}
    for player_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
        player_cli_obj = players[player_alias]['cli']
        json_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                                 TESTS_SCENARIO, f"{player_alias}_{TESTS_SCENARIO.replace('/', '_')}_{conf_args['packet_size']}.json")
        traffic_parameters = player_cli_obj.performance.get_traffic_parameters(scenario=TESTS_SCENARIO,
                                                                               conf_args=conf_args)

        get_spine_multiple_ip_stream_list(player_alias, traffic_parameters, json_path, conf_args)
        traffic_jsons[player_alias] = json_path
    return traffic_jsons


def get_spine_multiple_ip_stream_list(player_alias, traffic_parameters, json_path, conf_args):
    """
    Generate multiple traffic streams for spine-to-leaf topology scenarios.

    This function creates traffic streams for Alibaba spine performance testing, configuring
    different packet flows between super spine, leaf, and spine nodes. It supports both IPv4
    and IPv6 traffic with varying packet counts based on the traffic direction.

    Traffic Flow Patterns:
        - Super Spine to Leaf: 4 packets
        - Leaf to Leaf: 15 packets
        - Leaf to Super Spine: 4 packets

    Args:
        player_alias (str): The traffic generator alias (e.g., "left-tg", "right-tg").
        traffic_parameters (dict): Dictionary containing traffic configuration parameters including:
            - IP: Nested dict with 'src' and 'dst' IP addresses
            - is_ipv6: Boolean flag for IPv6 traffic
            - ports: List of ports to send traffic from
            - num_packets: Number of packets per stream
        json_path (str): File path where the generated traffic JSON configuration will be saved.
        conf_args (dict): Configuration arguments containing:
            - PORT_GROUPS: Port group configurations per player
            - is_ipv4: Boolean flag to enable IPv4 traffic
            - is_ipv6: Boolean flag to enable IPv6 traffic
            - ip_to_mac_dict: Nested dict mapping directions and traffic types to IP addresses
            - ipv4_source_ip/ipv6_source_ip: Source IP addresses for each protocol

    Returns:
        None. Writes the generated traffic streams to a JSON file at json_path.

    Side Effects:
        - Creates traffic stream JSON file with all configured streams
        - Logs stream names for debugging purposes

    Note:
        The function iterates through port groups and creates separate streams for each
        combination of port group, traffic type (IPv4/IPv6), and traffic direction. All
        streams use DVS_LOSSLESS_TC for traffic class configuration.
    """
    stream_list = []
    traffic_to_leaf = "left"
    traffic_to_spine = "right"
    num_packets_super_spine_to_leaf = 5
    num_packets_leaf_to_leaf = 15
    num_packets_leaf_to_spine = 4

    for port_group_name, port_group_ports in conf_args[PerfConsts.PORT_GROUPS][player_alias].items():
        for traffic_type, traffic_enabled in zip(["ipv4", "ipv6"], [conf_args["is_ipv4"], conf_args["is_ipv6"]]):
            if traffic_enabled:
                if port_group_name == PerfConsts.SUPER_SPINE_PORTS_GROUP:
                    ip_list = [(conf_args["ip_to_mac_dict"][traffic_to_leaf][traffic_type][0][0], "traffic_to_leaf")]
                    traffic_parameters["MAC"]["dst"] = conf_args[f"from_spine_dest_mac"]
                else:
                    ip_list = [(conf_args["ip_to_mac_dict"][traffic_to_leaf][traffic_type][0][0], "traffic_to_leaf"), (conf_args["ip_to_mac_dict"][traffic_to_spine][traffic_type][0][0], "traffic_to_spine")]
                    traffic_parameters["MAC"]["dst"] = conf_args[f"from_leaf_dest_mac"]

                for ip in ip_list:
                    traffic_parameters["IP"]["dst"] = ip[0]
                    traffic_parameters["IP"]["src"] = conf_args[f"{traffic_type}_source_ip"]
                    direction_to = ip[1]

                    traffic_parameters["is_ipv6"] = traffic_type == "ipv6"
                    traffic_parameters["ports"] = port_group_ports

                    if port_group_name == PerfConsts.SUPER_SPINE_PORTS_GROUP:  # Sending traffic from super spine to leaf
                        traffic_parameters["num_packets"] = num_packets_super_spine_to_leaf
                    elif direction_to == "traffic_to_leaf":  # Sending traffic from leaf to leaf
                        traffic_parameters["num_packets"] = num_packets_leaf_to_leaf
                    else:  # Sending traffic from leaf to super spine
                        traffic_parameters["num_packets"] = num_packets_leaf_to_spine

                    stream_name = f"{port_group_name} {direction_to}. Packet IP: {ip}"
                    logger.info(f"Stream name: {stream_name}")
                    stream = create_json_traffic_stream(player_alias, traffic_parameters, stream_name, tc=dscp_to_tc(PerfConsts.DVS_LOSSLESS_TC, 2))
                    stream_list.append(stream)

        create_json_traffic_file_with_stream_list(player_alias, traffic_parameters, json_path, stream_list)


def get_pws_traffic(players, conf_args, template_suite="traffic_packets_json_files"):
    """
    Generate PWS (Production Workload Simulation) traffic JSON files for all traffic generators.

    This function creates traffic pattern files simulating production workload patterns,
    including RoCE ACK packets, RTT probes, and RoCE data streams.

    Args:
        players (dict): Dictionary of test players containing traffic generator configurations.
            Expected structure: {player_alias: {'cli': cli_object, ...}, ...}
        conf_args (dict): Configuration arguments dictionary containing traffic parameters.
        template_suite (str, optional): Name of the directory containing traffic templates.
            Defaults to "traffic_packets_json_files".

    Returns:
        dict: Dictionary mapping each player alias to its generated traffic JSON file path.
            Structure: {player_alias: json_file_path, ...}
    """
    traffic_jsons = {}
    pkt_size = PerfConsts.PACKET_SIZE_LIST[0]
    for player_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
        player_cli_obj = players[player_alias]['cli']
        json_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                                 TESTS_SCENARIO, f"{player_alias}_{TESTS_SCENARIO.replace('/', '_')}_{pkt_size}.json")

        traffic_parameters = player_cli_obj.performance.get_traffic_parameters(scenario=TESTS_SCENARIO,
                                                                               conf_args=conf_args)
        get_spine_2_packet_types_traffic(player_alias, traffic_parameters, conf_args, json_path)
        traffic_jsons[player_alias] = json_path
    return traffic_jsons


def get_spine_2_packet_types_traffic(spine_tg, traffic_parameters, conf_args, json_path):
    """
    Generate traffic streams with multiple packet types for spine topology testing.

    This function creates traffic patterns with 4 different packet types simulating
    production workload: RoCE ACK packets (100B), RTT probes (200B), RTT probe
    responses (200B), and RoCE data packets (configurable size, default 4K).

    Args:
        spine_tg (str): The traffic generator alias (e.g., "left-tg", "right-tg").
        traffic_parameters (dict): Dictionary containing traffic configuration parameters.
        conf_args (dict): Configuration arguments containing:
            - is_ipv4: Boolean flag to enable IPv4 traffic
            - is_ipv6: Boolean flag to enable IPv6 traffic
            - ip_to_mac_dict: Nested dict mapping directions and traffic types to
                              IP-MAC pairs
        json_path (str): File path where the generated traffic JSON configuration
                         will be saved.

    Returns:
        None. Writes the generated traffic streams to a JSON file at json_path.
    """
    tc = dscp_to_tc(PerfConsts.DVS_LOSSY_TC, 2)
    direction_from = "left" if spine_tg == PerfConsts.LEFT_TG_ALIAS else "right"
    direction_to = "right" if direction_from == "left" else "left"
    port_pattern = f"_from_{direction_from}_to_{direction_to}"
    stream_list = []

    # Helper function to create stream with consistent formatting
    def create_stream(stream_name, packet_size, traffic_tc):
        traffic_parameters["packet_size"] = packet_size
        return create_json_traffic_stream(spine_tg, traffic_parameters, stream_name, tc=traffic_tc)

    ports = copy.deepcopy(traffic_parameters["ports"])

    # Define stream configurations: (name_suffix, num_packets, packet_size)
    stream_configs = [
        ("RoCE_ack", 19, 100),
        ("RTT_probe", 19, 200),
        ("RTT_probe_response", 20, 200),
        ("RoCE_data_", 42, PerfConsts.PACKET_SIZE_LIST[0])
    ]

    split_num = conf_args['split_right'] if spine_tg == PerfConsts.RIGHT_TG_ALIAS else conf_args['split_left']
    num_packets_adjust = (1.0 / split_num) * 2

    for (traffic_type, traffic_enabled) in zip(["ipv4", "ipv6"], [conf_args["is_ipv4"], conf_args["is_ipv6"]]):
        if traffic_enabled:
            for ip_to_mac, unconnected_port in zip(conf_args["ip_to_mac_dict"][direction_from][traffic_type], ports):
                traffic_parameters["ports"] = [unconnected_port]
                traffic_parameters["IP"]["dst"] = ip_to_mac[0]

                for name_suffix, num_packets, packet_size in stream_configs:
                    num_packets = int(num_packets * num_packets_adjust)
                    traffic_parameters["num_packets"] = num_packets
                    stream_name = f"{name_suffix}{port_pattern}"
                    stream = create_stream(stream_name, packet_size, tc)
                    stream_list.append(stream)

                create_json_traffic_file_with_stream_list(spine_tg, traffic_parameters, json_path, stream_list)
