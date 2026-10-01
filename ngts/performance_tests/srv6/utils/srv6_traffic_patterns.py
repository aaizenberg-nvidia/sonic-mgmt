import os
from collections import deque
from ngts.constants.constants import BugHandlerConst
from ngts.constants.performance_constants import PerfConsts, PortMappingOptionsConsts, MRCConsts
from ngts.helpers.performance.traffic_helpers import create_json_traffic_file_with_stream_list
from ngts.performance_tests.srv6.utils.srv6_workloads import create_round_robin_stream
from ngts.cli_wrappers.nvue.nvue_cli import NvueCli


def get_tg_bisection_traffic_params(players, player_alias, conf_args, traffic_type, template_suite, create_workload_stream,
                                    dut_interfaces_ipv6_configuration_dict, traffic_jsons, port_bisection_pairs,
                                    mrc_data_packet_size=None, mrc_num_packets=None, vary_src_ip=False):
    player_cli_obj = players[player_alias]['cli']
    traffic_parameters = player_cli_obj.performance.get_traffic_parameters(scenario=conf_args["scenario"],
                                                                           conf_args=conf_args)
    if mrc_data_packet_size is not None:
        traffic_parameters["mrc_data_packet_size"] = mrc_data_packet_size
    if mrc_num_packets is not None:
        traffic_parameters["mrc_num_packets"] = mrc_num_packets
    if vary_src_ip:
        traffic_parameters["vary_src_ip"] = True
    json_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                             conf_args["scenario"], f"{player_alias}_{conf_args['scenario']}_bisection.json")
    mloops_dict = dict(player_cli_obj.performance.mloops)
    stream_list = []
    dut_mac_addresses = players['dut']['cli'].interface.get_all_interfaces_mac_addresses()
    for (src_port, dst_port) in port_bisection_pairs:
        player_cli_obj.performance.update_dst_mac_address(src_port, dut_mac_addresses, traffic_parameters)
        create_workload_stream(player_alias, player_cli_obj, [src_port], dst_port, traffic_parameters, traffic_type,
                               mloops_dict, dut_interfaces_ipv6_configuration_dict,
                               stream_list=stream_list, congestion=False)
    create_json_traffic_file_with_stream_list(player_alias, traffic_parameters, json_path, stream_list)
    traffic_jsons[player_alias] = json_path


def get_round_robin_traffic(players, conf_args, traffic_type,
                            upstream_downstream_group, bisection_traffic,
                            dut_interfaces_ipv6_configuration_dict,
                            template_suite="traffic_packets_json_files"):
    """
    First round (port i↔ port j: Means 1 packet ingress port i and egress port j, 1 packet ingress port j and egress port i):
    Port 0 ↔ 180
    Port 1 ↔ 181
    ...
    Port 178 ↔ 358.
    Port 179 ↔ 359.

    Second round:
    Port 0 ↔ 181.
    Port 1 ↔ 182.
    ...
    Port 178 ↔ 359.
    Port 179 ↔ 180.

    """
    traffic_jsons = {}
    ports = players['dut']['cli'].performance.get_right_left_ports_dict()
    left_ports, right_ports = ports["left_ports"], ports["right_ports"]
    tg_src_dst_ports_dict = {PerfConsts.LEFT_TG_ALIAS: (left_ports, right_ports),
                             PerfConsts.RIGHT_TG_ALIAS: (right_ports, left_ports)}
    for tg_alias, (src_ports, dst_ports) in tg_src_dst_ports_dict.items():
        get_tg_round_robin_traffic_params(players, tg_alias, conf_args,
                                          traffic_type, template_suite,
                                          dut_interfaces_ipv6_configuration_dict, traffic_jsons,
                                          upstream_downstream_group, src_ports=src_ports, dst_ports=dst_ports,
                                          bisection_traffic=bisection_traffic)
    return traffic_jsons


def get_tg_round_robin_traffic_params(players, player_alias, conf_args, traffic_type, template_suite,
                                      dut_interfaces_ipv6_configuration_dict, traffic_jsons,
                                      upstream_downstream_group, src_ports, dst_ports,
                                      bisection_traffic, send_control_packets=False):
    player_cli_obj = players[player_alias]['cli']
    traffic_parameters = player_cli_obj.performance.get_traffic_parameters(scenario=conf_args["scenario"],
                                                                           conf_args=conf_args)
    json_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                             conf_args["scenario"], f"{player_alias}_{conf_args['scenario']}_round_robin.json")
    mloops_dict = dict(player_cli_obj.performance.mloops)
    stream_list = []
    dut_mac_addresses = players['dut']['cli'].interface.get_all_interfaces_mac_addresses()
    for upstream_ports, downstream_ports in upstream_downstream_group:
        cycle_ports_pairs = get_cycle_ports_pairs(upstream_ports, downstream_ports)
        round_len = len(upstream_ports)
        for (port1, port2) in cycle_ports_pairs:
            ports_cycle_flow = get_ports_cycle_flow_by_tg(port1, port2, src_ports, dst_ports, bisection_traffic)
            for (src_port, dst_port) in ports_cycle_flow:
                player_cli_obj.performance.update_dst_mac_address(src_port, dut_mac_addresses, traffic_parameters)
                create_round_robin_stream(player_alias, player_cli_obj, [src_port], dst_port, traffic_parameters, traffic_type,
                                          mloops_dict, dut_interfaces_ipv6_configuration_dict,
                                          stream_list=stream_list, send_data=True, send_ack=False, mrc_num_packets=1)
        if send_control_packets:
            last_round = cycle_ports_pairs[-round_len:]
            for (port1, port2) in last_round:
                ports_cycle_flow = get_ports_cycle_flow_by_tg(port1, port2, src_ports, dst_ports, bisection_traffic)
                for (src_port, dst_port) in ports_cycle_flow:
                    player_cli_obj.performance.update_dst_mac_address(src_port, dut_mac_addresses, traffic_parameters)
                    create_round_robin_stream(player_alias, player_cli_obj, [src_port], dst_port, traffic_parameters, traffic_type,
                                              mloops_dict, dut_interfaces_ipv6_configuration_dict,
                                              stream_list=stream_list, send_data=False, send_ack=True)
    create_json_traffic_file_with_stream_list(player_alias, traffic_parameters, json_path, stream_list)
    traffic_jsons[player_alias] = json_path


def get_ports_cycle_flow_by_tg(port1, port2, tg_src_ports, other_tg_dst_ports, bisection_traffic):
    """
    In round-robin port i ↔ port j:
    Means 1 packet ingress port i and egress port j and 1 packet ingress port j and egress port i
    so, if both ports are of the tg, the tg needs to send both ways.
    otherwise, if both ports are of the other tg, the tg is not sending anything via these ports (they are not his ports).
    and in the other cases, the traffic directions depends on to which tg each port belongs.

    Args:
        port1: the first port in the pair, i.e. Ethernet0
        port2: the first port in the pair, i.e. Ethernet33
        tg_src_ports: the tg ports ("src_ports")
        other_tg_dst_ports:  the other tg ports ("dst ports")
        bisection_traffic: True if the ports should send to each other

    Returns:
    list of the relevant traffic pairs the tg need's to send.
    """
    if port1 in tg_src_ports and port2 in other_tg_dst_ports:
        return [(port1, port2)]
    elif port2 in tg_src_ports and port1 in other_tg_dst_ports and bisection_traffic:
        return [(port2, port1)]
    elif port1 in tg_src_ports and port2 in tg_src_ports and not bisection_traffic:
        return [(port1, port2)]
    else:
        return []


def get_ingress_ports_by_tg(ingress_ports, src_ports):
    return list(set(src_ports).intersection(ingress_ports))


def get_many_to_one_traffic(players, conf_args, traffic_type, dut_interfaces_ipv6_configuration_dict,
                            egress_ports, ingress_ports, create_workload_stream, congestion=False,
                            template_suite="traffic_packets_json_files", mrc_num_packets=None):
    if mrc_num_packets is None:
        mrc_num_packets = MRCConsts.get_many_to_one_mrc_num_packets(conf_args.get("chip_type"))
    traffic_jsons = {}
    ports = players['dut']['cli'].performance.get_right_left_ports_dict()
    left_ports, right_ports = ports["left_ports"], ports["right_ports"]
    tg_src_ports = {PerfConsts.LEFT_TG_ALIAS: left_ports,
                    PerfConsts.RIGHT_TG_ALIAS: right_ports}
    for tg_alias, src_ports in tg_src_ports.items():
        get_tg_many_to_one_traffic_params(players, tg_alias, conf_args,
                                          traffic_type, template_suite, create_workload_stream,
                                          dut_interfaces_ipv6_configuration_dict, traffic_jsons,
                                          egress_ports, ingress_ports, src_ports=src_ports,
                                          congestion=congestion, mrc_num_packets=mrc_num_packets)
    return traffic_jsons


def get_tg_many_to_one_traffic_params(players, player_alias, conf_args,
                                      traffic_type, template_suite, create_workload_stream,
                                      dut_interfaces_ipv6_configuration_dict, traffic_jsons,
                                      egress_ports, ingress_ports, src_ports, congestion,
                                      mrc_num_packets=None):
    player_cli_obj = players[player_alias]['cli']
    traffic_parameters = player_cli_obj.performance.get_traffic_parameters(scenario=conf_args["scenario"],
                                                                           conf_args=conf_args)
    if mrc_num_packets is not None:
        traffic_parameters["mrc_num_packets"] = mrc_num_packets
    json_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                             conf_args["scenario"], f"{player_alias}_{conf_args['scenario']}_many_to_one.json")
    mloops_dict = dict(player_cli_obj.performance.mloops)
    stream_list = []

    tg_ingress_ports = get_ingress_ports_by_tg(ingress_ports, src_ports)
    dut_mac_addresses = players['dut']['cli'].interface.get_all_interfaces_mac_addresses()
    for ingress_port in tg_ingress_ports:
        for egress_port in egress_ports:
            player_cli_obj.performance.update_dst_mac_address(ingress_port, dut_mac_addresses, traffic_parameters)
            create_workload_stream(player_alias, player_cli_obj, [ingress_port], egress_port, traffic_parameters, traffic_type,
                                   mloops_dict, dut_interfaces_ipv6_configuration_dict,
                                   stream_list=stream_list, congestion=congestion)
    create_json_traffic_file_with_stream_list(player_alias, traffic_parameters, json_path, stream_list)
    traffic_jsons[player_alias] = json_path


def get_ingress_ports_by_tg(ingress_ports, src_ports):
    return list(set(src_ports).intersection(ingress_ports))


def get_many_to_few_traffic(players, conf_args, traffic_type, dut_interfaces_ipv6_configuration_dict,
                            egress_ports, ingress_ports, create_workload_stream, congestion=False,
                            template_suite="traffic_packets_json_files", pairing=None):
    traffic_jsons = {}
    ports = players['dut']['cli'].performance.get_right_left_ports_dict()
    left_ports, right_ports = ports["left_ports"], ports["right_ports"]
    tg_src_ports = {PerfConsts.LEFT_TG_ALIAS: left_ports,
                    PerfConsts.RIGHT_TG_ALIAS: right_ports}
    if pairing is None:
        ingress_egress_ports_pairing = get_ingress_egress_ports_pairing(ingress_ports, egress_ports)
    else:
        ingress_egress_ports_pairing = pairing
    for tg_alias, src_ports in tg_src_ports.items():
        get_tg_many_to_few_traffic_params(players, tg_alias, conf_args,
                                          traffic_type, template_suite, create_workload_stream,
                                          dut_interfaces_ipv6_configuration_dict, traffic_jsons,
                                          ingress_egress_ports_pairing, src_ports=src_ports,
                                          congestion=congestion)
    return traffic_jsons


def get_tg_many_to_few_traffic_params(players, player_alias, conf_args,
                                      traffic_type, template_suite, create_workload_stream,
                                      dut_interfaces_ipv6_configuration_dict, traffic_jsons,
                                      ingress_egress_ports_pairing, src_ports, congestion):
    player_cli_obj = players[player_alias]['cli']
    traffic_parameters = player_cli_obj.performance.get_traffic_parameters(scenario=conf_args["scenario"],
                                                                           conf_args=conf_args)
    json_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                             conf_args["scenario"], f"{player_alias}_{conf_args['scenario']}_many_to_few.json")
    mloops_dict = dict(player_cli_obj.performance.mloops)
    dut_mac_addresses = players['dut']['cli'].interface.get_all_interfaces_mac_addresses()
    stream_list = []
    for ingress_ports, egress_port in ingress_egress_ports_pairing:
        tg_ingress_ports = get_ingress_ports_by_tg(ingress_ports, src_ports)
        for ingress_port in tg_ingress_ports:
            player_cli_obj.performance.update_dst_mac_address(ingress_port, dut_mac_addresses, traffic_parameters)
            create_workload_stream(player_alias, player_cli_obj, [ingress_port], egress_port, traffic_parameters, traffic_type,
                                   mloops_dict, dut_interfaces_ipv6_configuration_dict,
                                   stream_list=stream_list, congestion=congestion)
    create_json_traffic_file_with_stream_list(player_alias, traffic_parameters, json_path, stream_list)
    traffic_jsons[player_alias] = json_path


def get_ingress_egress_ports_pairing(ingress_ports, egress_ports):
    sublist_size = len(ingress_ports) // len(egress_ports)
    ingress_egress_ports_pairing = [ingress_ports[i:i + sublist_size] for i in range(0, len(ingress_ports), sublist_size)]
    return list(zip(ingress_egress_ports_pairing, egress_ports))


def get_ingress_egress_ports_pairing_for_debug(ingress_ports, egress_ports, M, option):
    """
    A function to get the pairing of ingress and egress ports for many to few traffic.
    Args:
        ingress_ports: the list of ingress ports
        egress_ports: the list of egress ports
        M: the number of ingress ports per egress port
        option: the option for the pairing

    Option 1: 4 consecutive ingress ports to egress ports in the same 8x group
    Option 2: out of the 4 consecutive ingress ports, 2 sends to egress ports in the one 8x group, and the other 2 sends to egress ports in the different 8x group
    Option 3: out of the 4 consecutive ingress ports, each sends to egress port in a different 8x group
    Option 4: out of the 4 consecutive ingress ports, 2 sends to egress ports in the one 8x group, 1 sends to egress port in a different 8x group,
    and the other 1 sends to egress port in a different 8x group than the first 2.
    Option 5: out of the 4 consecutive ingress ports, 3 sends to egress ports in a one 8x group,
    and the other 1 sends to egress port in a different 8x group than the first one.

    Returns:
        the pairing of ingress and egress ports
    """
    egress_port_num = len(ingress_ports) // M
    ingress_port_num = egress_port_num * M
    pairing = []
    idx_list = list(range(len(ingress_ports)))
    index_8x_groups = [idx_list[i:i + 8] for i in range(0, len(egress_ports), 8)]
    egress_ports_counters_dict = {egress_port: M for egress_port in range(len(egress_ports))}
    if option == PortMappingOptionsConsts.EGRESS_SAME_8X_OPTION:
        for idx in range(0, len(ingress_ports), 4):
            group_idx, group, idx_list = get_x8_group_elements_by_counters_values(egress_ports_counters_dict, index_8x_groups, 1, 4)
            for i in range(4):
                pairing.append(([ingress_ports[idx + i]], egress_ports[idx_list[i]]))
                update_group_by_counters_values(egress_ports_counters_dict, group, idx_list[i], 1)
            index_8x_groups = [group for group in index_8x_groups if len(group) > 0]
    elif option == PortMappingOptionsConsts.EGRESS_2_IN_SAME_8X_2_IN_OTHER_8X_OPTION:
        for idx in range(0, len(ingress_ports), 4):
            first_group_idx, first_8x_group, first_8x_index = update_pairing_for_option_2(pairing, ingress_ports, egress_ports, egress_ports_counters_dict, index_8x_groups, idx)
            update_pairing_for_option_2(pairing, ingress_ports, egress_ports, egress_ports_counters_dict, index_8x_groups, idx + 2, group_idx_list=[first_group_idx])
            index_8x_groups = [group for group in index_8x_groups if len(group) > 0]
    elif option == PortMappingOptionsConsts.EGRESS_DIFFERENT_8X_OPTION:
        for idx in range(0, len(ingress_ports), 4):
            first_group_idx, first_8x_group, first_8x_index = get_x8_group_by_counters_values(egress_ports_counters_dict, index_8x_groups, 1)
            second_group_idx, second_8x_group, second_8x_index = get_x8_group_by_counters_values(egress_ports_counters_dict, index_8x_groups, 1, group_idx_list=[first_group_idx])
            third_group_idx, third_8x_group, third_8x_index = get_x8_group_by_counters_values(egress_ports_counters_dict, index_8x_groups, 1, group_idx_list=[first_group_idx, second_group_idx])
            forth_group_idx, forth_8x_group, forth_8x_index = get_x8_group_by_counters_values(egress_ports_counters_dict, index_8x_groups, 1, group_idx_list=[first_group_idx, second_group_idx, third_group_idx])
            update_group_by_counters_values(egress_ports_counters_dict, first_8x_group, first_8x_index, 1)
            update_group_by_counters_values(egress_ports_counters_dict, second_8x_group, second_8x_index, 1)
            update_group_by_counters_values(egress_ports_counters_dict, third_8x_group, third_8x_index, 1)
            update_group_by_counters_values(egress_ports_counters_dict, forth_8x_group, forth_8x_index, 1)
            index_8x_groups = [group for group in index_8x_groups if len(group) > 0]
            pairing.append(([ingress_ports[idx]], egress_ports[first_8x_index]))
            pairing.append(([ingress_ports[idx + 1]], egress_ports[second_8x_index]))
            pairing.append(([ingress_ports[idx + 2]], egress_ports[third_8x_index]))
            pairing.append(([ingress_ports[idx + 3]], egress_ports[forth_8x_index]))
    elif option == PortMappingOptionsConsts.EGRESS_2_IN_SAME_8X_1_IN_OTHER_8X_LAST_IN_OTHER_8X_OPTION:
        for idx in range(0, len(ingress_ports), 4):
            first_group_idx, first_8x_group, first_8x_index_list = update_pairing_for_option_4(pairing, ingress_ports, egress_ports, egress_ports_counters_dict, index_8x_groups, idx)
            second_group_idx, second_8x_group, second_8x_index = get_x8_group_by_counters_values(egress_ports_counters_dict, index_8x_groups, 1, group_idx_list=[first_group_idx])
            third_group_idx, third_8x_group, third_8x_index = get_x8_group_by_counters_values(egress_ports_counters_dict, index_8x_groups, 1, group_idx_list=[first_group_idx, second_group_idx])
            update_group_by_counters_values(egress_ports_counters_dict, second_8x_group, second_8x_index, 1)
            update_group_by_counters_values(egress_ports_counters_dict, third_8x_group, third_8x_index, 1)
            index_8x_groups = [group for group in index_8x_groups if len(group) > 0]
            pairing.append(([ingress_ports[idx + 2]], ingress_ports[second_8x_index]))
            pairing.append(([ingress_ports[idx + 3]], ingress_ports[third_8x_index]))
    elif option == PortMappingOptionsConsts.EGRESS_3_IN_SAME_8X_1_IN_OTHER_8X_OPTION:
        for idx in range(0, len(ingress_ports), 4):
            first_group_idx, first_8x_group, first_8x_index = update_pairing_for_option_5(pairing, ingress_ports, egress_ports, egress_ports_counters_dict, index_8x_groups, idx)
            second_group_idx, second_8x_group, second_8x_index = get_x8_group_by_counters_values(egress_ports_counters_dict, index_8x_groups, 1, group_idx_list=[first_group_idx])
            update_group_by_counters_values(egress_ports_counters_dict, second_8x_group, second_8x_index, 1)
            index_8x_groups = [group for group in index_8x_groups if len(group) > 0]
            pairing.append(([ingress_ports[idx + 3]], egress_ports[second_8x_index]))
    elif option == PortMappingOptionsConsts.EGRESS_SEQUENTIAL_OPTION:
        egress_idx = 0
        for idx in range(0, ingress_port_num, M):
            for i in range(M):
                pairing.append(([ingress_ports[idx + i]], egress_ports[egress_idx]))
            egress_idx += 1
    return pairing


def update_group_by_counters_values(egress_ports_counters_dict, group, idx_ele, counter_val):
    """
    Update the counters values of the egress ports in the group.

    Args:
        egress_ports_counters_dict: the dictionary of the counters values of the egress ports
        group: the group of the egress ports
        idx_ele: the index of the egress port in the group
        counter_val: the value to subtract from the counters values of the egress port
    """
    egress_ports_counters_dict[idx_ele] -= counter_val
    if egress_ports_counters_dict[idx_ele] == 0:
        group.remove(idx_ele)


def update_pairing_for_option_2(pairing, ingress_ports, egress_ports, egress_ports_counters_dict, index_8x_groups, cur_idx, group_idx_list=None):
    group_idx, group, idx_list = get_x8_group_elements_by_counters_values(egress_ports_counters_dict, index_8x_groups, 1, 2, group_idx_list=group_idx_list)
    for i in range(2):
        pairing.append(([ingress_ports[cur_idx + i]], egress_ports[idx_list[i]]))
        update_group_by_counters_values(egress_ports_counters_dict, group, idx_list[i], 1)
    return group_idx, group, idx_list


def update_pairing_for_option_4(pairing, ingress_ports, egress_ports, egress_ports_counters_dict, index_8x_groups, cur_idx, group_idx_list=None):
    group_idx, group, idx_list = get_x8_group_elements_by_counters_values(egress_ports_counters_dict, index_8x_groups, 1, 2, group_idx_list=group_idx_list)
    for i in range(2):
        pairing.append(([ingress_ports[cur_idx + i]], egress_ports[idx_list[i]]))
        update_group_by_counters_values(egress_ports_counters_dict, group, idx_list[i], 1)
    return group_idx, group, idx_list


def update_pairing_for_option_5(pairing, ingress_ports, egress_ports, egress_ports_counters_dict, index_8x_groups, cur_idx, group_idx_list=None):
    group_idx, group, idx_list = get_x8_group_elements_by_counters_values(egress_ports_counters_dict, index_8x_groups, 1, 3, group_idx_list=group_idx_list)
    for i in range(3):
        pairing.append(([ingress_ports[cur_idx + i]], egress_ports[idx_list[i]]))
        update_group_by_counters_values(egress_ports_counters_dict, group, idx_list[i], 1)
    return group_idx, group, idx_list


def get_x8_group_by_counters_values(egress_ports_counters_dict, index_8x_groups, counter_val, group_idx_list=None):
    """
    Get the group of the egress ports with the counters values greater than the counter value.

    Args:
        egress_ports_counters_dict: the dictionary of the counters values of the egress ports
        index_8x_groups: the list of the groups of the egress ports
        counter_val: the value to compare the counters values of the egress ports
        group_idx_list: the list of the indices of the groups to exclude

    Returns:
        the index of the group, the group, and the element of the group
    """
    chosen_idx, chosen_group, chosen_ele = None, None, None
    for idx, group in enumerate(index_8x_groups):
        if group_idx_list:
            if all([group_idx != idx for group_idx in group_idx_list]):
                for ele in group:
                    if egress_ports_counters_dict[ele] >= counter_val:
                        chosen_idx, chosen_group, chosen_ele = idx, group, ele
        else:
            for ele in group:
                if egress_ports_counters_dict[ele] >= counter_val:
                    chosen_idx, chosen_group, chosen_ele = idx, group, ele
    return chosen_idx, chosen_group, chosen_ele


def get_x8_group_elements_by_counters_values(egress_ports_counters_dict, index_8x_groups, counter_val, elements_num, group_idx_list=None):
    """
    Get the elements of the group of the egress ports with the counters values greater than the counter value.

    Args:
        egress_ports_counters_dict: the dictionary of the counters values of the egress ports
        index_8x_groups: the list of the groups of the egress ports
        counter_val: the value to compare the counters values of the egress ports
        elements_num: the number of the elements to get
        group_idx_list: the list of the indices of the groups to exclude

    Returns:
        the index of the group, the group, and the list of the elements of the group
    """
    chosen_idx, chosen_group, ele_list = None, None, []
    for idx, group in enumerate(index_8x_groups):
        if group_idx_list:
            if all([group_idx != idx for group_idx in group_idx_list]):
                for ele in group:
                    if egress_ports_counters_dict[ele] >= counter_val:
                        ele_list.append(ele)
                        if len(ele_list) == elements_num:
                            return idx, group, ele_list
                ele_list = []
        else:
            for ele in group:
                if egress_ports_counters_dict[ele] >= counter_val:
                    ele_list.append(ele)
                    if len(ele_list) == elements_num:
                        return idx, group, ele_list
            ele_list = []
    return chosen_idx, chosen_group, ele_list


def get_cycle_ports_pairs(upstream, downstream):
    round_len = len(upstream)
    cycle_pairing = []
    for round in range(round_len):
        for i in range(round_len):
            if upstream[i] != downstream[(i + round) % round_len]:
                cycle_pairing.append((upstream[i], downstream[(i + round) % round_len]))
    return cycle_pairing


def get_cycle_ports_pairs_for_debug(upstream, downstream):
    downstream_deque = deque(downstream)
    cycle_pairing = []
    for upstream_port in upstream:
        cycle_pairing.extend((upstream_port, downstream_port) for downstream_port in downstream_deque)
        downstream_deque.rotate(-1)
    return cycle_pairing
