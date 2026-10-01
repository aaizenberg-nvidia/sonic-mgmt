import ipaddress
import math
import random
from infra.tools.exceptions.test_issue import TestIssue
from ngts.constants.performance_constants import MRCConsts, NoSrhPerfConsts, PerfConsts
from ngts.helpers.performance.traffic_helpers import (create_srv6_json_traffic_stream, dscp_to_tc)

PORT_DEFAULT_IPV6_PREFIX = "aaaa"
PORT_DEFAULT_IPV6_ROUTE_PREFIX = "bbbb"
PORT_DEFAULT_SRV6_PREFIX = "bbbb:1"
PORT_DEFAULT_SRC_PREFIX = "cccc"


def get_mloop_ports(mloops_dict, ports):
    mloop_ports = []
    for port in ports:
        mloop_ports.append(mloops_dict[port])
    return mloop_ports


def get_workload_method(workload):
    workload_to_method_dict = {
        MRCConsts.WORKLOAD1_NAME: create_workload1_stream,
        MRCConsts.MRC1_DATA_ONLY_WORKLOAD_NAME: create_mrc1_data_only_workload_stream,
        MRCConsts.MRC2_DATA_ONLY_WORKLOAD_NAME: create_mrc2_data_only_workload_stream,
    }
    return workload_to_method_dict[workload]


def get_dst_ip_by_traffic_type(traffic_type, dut_interfaces_ipv6_configuration_dict, dst_port):
    if traffic_type == MRCConsts.TRAFFIC_TYPE_IPV6:
        return dut_interfaces_ipv6_configuration_dict[dst_port].replace(PORT_DEFAULT_IPV6_PREFIX,
                                                                        PORT_DEFAULT_IPV6_ROUTE_PREFIX)
    elif traffic_type in (MRCConsts.TRAFFIC_TYPE_SRV6, NoSrhPerfConsts.TRAFFIC_TYPE_SRV6_NO_SRH):
        return dut_interfaces_ipv6_configuration_dict[dst_port].replace(PORT_DEFAULT_IPV6_PREFIX,
                                                                        PORT_DEFAULT_SRV6_PREFIX)
    else:
        raise TestIssue(f"Unknown traffic type {traffic_type} is not supported")


def create_round_robin_stream(player_alias, cli_obj, src_ports, dst_port, traffic_parameters, traffic_type,
                              mloops_dict, dut_interfaces_ipv6_configuration_dict, stream_list,
                              mrc_num_packets=1, send_data=True, send_ack=True):
    set_workload_traffic_parameters(cli_obj, traffic_parameters, mloops_dict, src_ports, dst_port,
                                    dut_interfaces_ipv6_configuration_dict, traffic_type)
    if send_data:
        mrc_dscp = random.choice([MRCConsts.MRC1_DSCP, MRCConsts.MRC2_DSCP])
        stream_num = 1 if mrc_dscp == MRCConsts.MRC1_DSCP else 2
        mrc_stream = get_mrc_stream(player_alias, traffic_parameters, mrc_num_packets,
                                    src_ports=src_ports, dst_port=dst_port,
                                    mrc_dscp=mrc_dscp, mrc_stream_name=stream_num)
        stream_list.append(mrc_stream)
    if send_ack:
        rtt_dscp = random.choice([MRCConsts.MRC1_RTT_DSCP, MRCConsts.MRC2_RTT_DSCP])
        stream_num = 1 if rtt_dscp == MRCConsts.MRC1_RTT_DSCP else 2
        retransmission_dscp = random.choice([MRCConsts.MRC1_RETRANSMISSION_DSCP, MRCConsts.MRC2_RETRANSMISSION_DSCP])
        retransmission_stream_num = 1 if retransmission_dscp == MRCConsts.MRC1_RETRANSMISSION_DSCP else 2
        rtt_stream = get_rtt_stream(player_alias, traffic_parameters, src_ports, dst_port,
                                    rtt_dscp=rtt_dscp, rtt_stream_num=stream_num)
        probe_ack_stream = get_probe_ack_stream(player_alias, traffic_parameters, src_ports, dst_port)
        roce_ack_stream = get_roce_ack_stream(player_alias, traffic_parameters, src_ports, dst_port, num_packets=2)
        retransmission_stream = get_mrc_stream(player_alias, traffic_parameters, 1, src_ports, dst_port,
                                               retransmission_dscp, mrc_stream_name=f"_RETRANSMIT_{retransmission_stream_num}")
        stream_list.extend([rtt_stream, probe_ack_stream, roce_ack_stream, retransmission_stream])
        set_workload_traffic_parameters(cli_obj, traffic_parameters, mloops_dict, src_ports, dst_port,
                                        dut_interfaces_ipv6_configuration_dict, traffic_type=MRCConsts.TRAFFIC_TYPE_IPV6)
        gfp_streams = get_gfp_streams(player_alias, traffic_parameters, src_ports, dst_port, both=True)
        stream_list.extend(gfp_streams)


def create_workload1_stream(player_alias, cli_obj, src_ports, dst_port, traffic_parameters, traffic_type,
                            mloops_dict, dut_interfaces_ipv6_configuration_dict, stream_list,
                            mrc1_num_packets=6, mrc2_num_packets=6,
                            send_roce_ack=True, send_rtt_and_probe_ack=True, congestion=False, send_retransmission=True):
    """
    packet breakdown:
    +------------------------+------------------+
    | Packet Type           | Number of Packets |
    +------------------------+------------------+
    | MRC1 data packets     | 6                 |
    | MRC2 data packets     | 6                 |
    | MRC retransmission    | 1                 |
    +------------------------+------------------+
    | Total                 | 13                |
    +------------------------+------------------+
    """
    mrc1_num_packets = traffic_parameters.get("mrc_num_packets", mrc1_num_packets)
    mrc2_num_packets = traffic_parameters.get("mrc_num_packets", mrc2_num_packets)
    set_workload_traffic_parameters(cli_obj, traffic_parameters, mloops_dict, src_ports, dst_port,
                                    dut_interfaces_ipv6_configuration_dict, traffic_type)
    get_mrc_data_stream(player_alias, traffic_parameters, src_ports, dst_port,
                        mrc_num_packets=mrc1_num_packets,
                        mrc_dscp=MRCConsts.MRC1_DSCP, rtt_dscp=MRCConsts.MRC1_RTT_DSCP,
                        stream_list=stream_list,
                        send_roce_ack=False,
                        send_rtt_and_probe_ack=False,
                        congestion=False,
                        stream_num=1)
    get_mrc_data_stream(player_alias, traffic_parameters, src_ports, dst_port,
                        mrc_num_packets=mrc2_num_packets,
                        mrc_dscp=MRCConsts.MRC2_DSCP, rtt_dscp=MRCConsts.MRC2_RTT_DSCP,
                        stream_list=stream_list,
                        send_roce_ack=False,
                        send_rtt_and_probe_ack=False,
                        congestion=False,
                        stream_num=2)
    if send_retransmission:
        dscp = random.choice([MRCConsts.MRC1_RETRANSMISSION_DSCP, MRCConsts.MRC2_RETRANSMISSION_DSCP])
        stream_num = 1 if dscp == MRCConsts.MRC1_RETRANSMISSION_DSCP else 2
        mrc_retransmit_stream = get_mrc_stream(player_alias, traffic_parameters, 1, src_ports, dst_port,
                                               dscp, mrc_stream_name=f"{stream_num}_RETRANSMIT")
        stream_list.extend([mrc_retransmit_stream])


def create_mrc1_data_only_workload_stream(player_alias, cli_obj, src_ports, dst_port, traffic_parameters, traffic_type,
                                          mloops_dict, dut_interfaces_ipv6_configuration_dict,
                                          stream_list, send_roce_ack=False,
                                          send_rtt_and_probe_ack=False, congestion=False, send_retransmission=False):
    """
    packet breakdown:
    +------------------------+------------------+
    | Packet Type           | Number of Packets |
    +------------------------+------------------+
    | MRC1 data packets     | 10                |
    +------------------------+------------------+
    | Total                 | 10                |
    +------------------------+------------------+
    """
    set_workload_traffic_parameters(cli_obj, traffic_parameters, mloops_dict, src_ports, dst_port,
                                    dut_interfaces_ipv6_configuration_dict, traffic_type)
    get_mrc_data_stream(player_alias, traffic_parameters, src_ports, dst_port,
                        mrc_num_packets=10,
                        mrc_dscp=MRCConsts.MRC1_DSCP, rtt_dscp=MRCConsts.MRC1_RTT_DSCP,
                        stream_list=stream_list,
                        send_roce_ack=False,
                        send_rtt_and_probe_ack=False,
                        congestion=False,
                        stream_num=1)


def create_mrc2_data_only_workload_stream(player_alias, cli_obj, src_ports, dst_port, traffic_parameters, traffic_type,
                                          mloops_dict, dut_interfaces_ipv6_configuration_dict,
                                          stream_list, send_roce_ack=False,
                                          send_rtt_and_probe_ack=False, congestion=False, send_retransmission=False):
    """
    packet breakdown:
    +------------------------+------------------+
    | Packet Type           | Number of Packets |
    +------------------------+------------------+
    | MRC2 data packets     | 10                |
    +------------------------+------------------+
    | Total                 | 10                |
    +------------------------+------------------+
    """
    set_workload_traffic_parameters(cli_obj, traffic_parameters, mloops_dict, src_ports, dst_port,
                                    dut_interfaces_ipv6_configuration_dict, traffic_type)
    get_mrc_data_stream(player_alias, traffic_parameters, src_ports, dst_port,
                        mrc_num_packets=10,
                        mrc_dscp=MRCConsts.MRC2_DSCP, rtt_dscp=MRCConsts.MRC2_RTT_DSCP,
                        stream_list=stream_list,
                        send_roce_ack=False,
                        send_rtt_and_probe_ack=False,
                        congestion=False,
                        stream_num=2)


def get_mrc_data_stream(player_alias, traffic_parameters, src_ports, dst_port,
                        mrc_num_packets, mrc_dscp, rtt_dscp, stream_list,
                        send_roce_ack=False, send_rtt_and_probe_ack=False,
                        congestion=False, stream_num=1, roce_num_packets=2, cnp_num_packets=1):
    if traffic_parameters.get("vary_src_ip"):
        base_src_ipv6 = ipaddress.ip_address(traffic_parameters["IPV6"]["src"])
        original_src = traffic_parameters["IPV6"]["src"]
        for i in range(mrc_num_packets):
            traffic_parameters["IPV6"]["src"] = str(base_src_ipv6 + i)
            mrc_stream = get_mrc_stream(player_alias, traffic_parameters, 1,
                                        src_ports=src_ports, dst_port=dst_port,
                                        mrc_dscp=mrc_dscp, mrc_stream_name=f"{stream_num}_pkt{i}")
            stream_list.append(mrc_stream)
        traffic_parameters["IPV6"]["src"] = original_src
    else:
        mrc_stream = get_mrc_stream(player_alias, traffic_parameters, mrc_num_packets,
                                    src_ports=src_ports, dst_port=dst_port,
                                    mrc_dscp=mrc_dscp, mrc_stream_name=stream_num)
        stream_list.append(mrc_stream)

    if send_rtt_and_probe_ack:
        rtt_stream = get_rtt_stream(player_alias, traffic_parameters, src_ports, dst_port,
                                    rtt_dscp=rtt_dscp, rtt_stream_num=stream_num)
        probe_ack_stream = get_probe_ack_stream(player_alias, traffic_parameters, src_ports, dst_port)
        stream_list.extend([rtt_stream, probe_ack_stream])

    if send_roce_ack:
        roce_ack_stream = get_roce_ack_stream(player_alias, traffic_parameters, src_ports, dst_port, roce_num_packets)
        stream_list.extend([roce_ack_stream])

    if congestion:
        cnp_stream = get_cnp_stream(player_alias, traffic_parameters,
                                    src_ports, dst_port, cnp_num_packets)
        stream_list.extend([cnp_stream])


def set_workload_traffic_parameters(cli_obj, traffic_parameters,
                                    mloops_dict, src_ports, dst_port, dut_interfaces_ipv6_configuration_dict, traffic_type):
    mloops_ports = get_mloop_ports(mloops_dict, src_ports)
    src_port = random.choice(src_ports)
    traffic_parameters["ports"] = cli_obj.performance.get_hex_int_sdk_ports(mloops_ports)
    traffic_parameters["IP"] = {"src": "4.4.4.4", "dst": "10.0.1.0"}
    traffic_parameters["IPV6"]["src"] = dut_interfaces_ipv6_configuration_dict[src_port].replace(PORT_DEFAULT_IPV6_PREFIX,
                                                                                                 PORT_DEFAULT_SRC_PREFIX)
    traffic_parameters["IPV6"]["dst"] = get_dst_ip_by_traffic_type(traffic_type,
                                                                   dut_interfaces_ipv6_configuration_dict, dst_port)


def get_mrc_stream(player_alias, traffic_parameters, mrc_num_packets,
                   src_ports, dst_port, mrc_dscp, mrc_stream_name,
                   packet_size=None, payload=True):
    if packet_size is None:
        packet_size = traffic_parameters.get("mrc_data_packet_size", MRCConsts.MRC_DATA_PACKET_SIZE)
    traffic_parameters["num_packets"] = mrc_num_packets
    traffic_parameters["packet_size"] = packet_size
    mrc_stream = create_srv6_json_traffic_stream(player_alias, traffic_parameters,
                                                 tc=dscp_to_tc(mrc_dscp, ecn=PerfConsts.ECN_CAPABLE_TRANSPORT),
                                                 stream_name=f"{', '.join(src_ports)}_to_{dst_port}_MRC{mrc_stream_name}",
                                                 BTH={'opcode': int(0x00)}, payload=payload)
    return mrc_stream


def get_rtt_stream(player_alias, traffic_parameters, src_ports,
                   dst_port, rtt_dscp, rtt_stream_num):
    traffic_parameters["num_packets"] = 1
    traffic_parameters["packet_size"] = PerfConsts.RTT_PROB_SIZE
    rtt_stream = create_srv6_json_traffic_stream(player_alias, traffic_parameters,
                                                 tc=dscp_to_tc(rtt_dscp, ecn=PerfConsts.ECN_CAPABLE_TRANSPORT),
                                                 stream_name=f"{', '.join(src_ports)}_to_{dst_port}_RTT_{rtt_stream_num}",
                                                 BTH={'opcode': int(0x64)}, payload=False)
    return rtt_stream


def get_probe_ack_stream(player_alias, traffic_parameters, src_ports, dst_port):
    traffic_parameters["num_packets"] = 1
    traffic_parameters["packet_size"] = PerfConsts.RTT_PROB_RESPONSE_SIZE
    probe_ack_stream = create_srv6_json_traffic_stream(player_alias, traffic_parameters,
                                                       tc=dscp_to_tc(MRCConsts.PROBE_ACK_DSCP, ecn=PerfConsts.ECN_CAPABLE_TRANSPORT),
                                                       stream_name=f"{', '.join(src_ports)}_to_{dst_port}_ProbeAck",
                                                       BTH={'opcode': int(0x64)}, payload=False)
    return probe_ack_stream


def get_roce_ack_stream(player_alias, traffic_parameters, src_ports, dst_port, num_packets=2):
    traffic_parameters["num_packets"] = num_packets
    traffic_parameters["packet_size"] = PerfConsts.ROCE_ACK_SIZE
    roce_ack_stream = create_srv6_json_traffic_stream(player_alias, traffic_parameters,
                                                      tc=dscp_to_tc(MRCConsts.ROCE_ACK_DSCP, ecn=PerfConsts.ECN_CAPABLE_TRANSPORT),
                                                      stream_name=f"{', '.join(src_ports)}_to_{dst_port}_ROCE_ACK",
                                                      BTH={'opcode': int(0x11)}, payload=False)
    return roce_ack_stream


def get_nack_stream(player_alias, traffic_parameters, src_ports, dst_port):
    traffic_parameters["num_packets"] = 1
    traffic_parameters["packet_size"] = PerfConsts.NACK_SIZE
    nack_stream = create_srv6_json_traffic_stream(player_alias, traffic_parameters,
                                                  tc=dscp_to_tc(MRCConsts.NACK_DSCP, ecn=PerfConsts.ECN_CAPABLE_TRANSPORT),
                                                  stream_name=f"{', '.join(src_ports)}_to_{dst_port}_NACK",
                                                  BTH={'opcode': int(0x1E)}, payload=False)
    return nack_stream


def get_sack_stream(player_alias, traffic_parameters, src_ports, dst_port):
    traffic_parameters["num_packets"] = 1
    traffic_parameters["packet_size"] = PerfConsts.SACK_SIZE
    sack_stream = create_srv6_json_traffic_stream(player_alias, traffic_parameters,
                                                  tc=dscp_to_tc(MRCConsts.SACK_DSCP, ecn=PerfConsts.ECN_CAPABLE_TRANSPORT),
                                                  stream_name=f"{', '.join(src_ports)}_to_{dst_port}_SACK",
                                                  BTH={'opcode': int(0x1D)}, payload=False)
    return sack_stream


def get_cnp_stream(player_alias, traffic_parameters, src_ports, dst_port, num_packets=1):
    traffic_parameters["num_packets"] = num_packets
    traffic_parameters["packet_size"] = PerfConsts.CNP_SIZE
    cnp_stream = create_srv6_json_traffic_stream(player_alias, traffic_parameters,
                                                 tc=dscp_to_tc(MRCConsts.CNP_DSCP, ecn=PerfConsts.ECN_CAPABLE_TRANSPORT),
                                                 stream_name=f"{', '.join(src_ports)}_to_{dst_port}_CNP",
                                                 BTH={'opcode': int(0x81)}, payload=False)
    return cnp_stream


def get_gfp_data_streams(player_alias, traffic_parameters, src_ports, dst_port, both=False):
    gfp_data_streams = []
    traffic_parameters["num_packets"] = 1
    traffic_parameters["packet_size"] = 4096
    if both:
        for protocol in [PerfConsts.IP_PROTOCOL_UDP, PerfConsts.IP_PROTOCOL_TCP]:
            gfp_data_stream_name = f"{', '.join(src_ports)}_to_{dst_port}_GFP_DATA_{protocol}"
            gfp_data_stream = create_srv6_json_traffic_stream(player_alias, traffic_parameters,
                                                              tc=dscp_to_tc(MRCConsts.GFP_DATA_DSCP, ecn=PerfConsts.ECN_CAPABLE_TRANSPORT),
                                                              stream_name=gfp_data_stream_name,
                                                              BTH={'opcode': int(0x00)}, payload=False, ip_protocol=protocol)
            gfp_data_streams.append(gfp_data_stream)
    else:
        gfp_data_protocol = random.choice([PerfConsts.IP_PROTOCOL_UDP, PerfConsts.IP_PROTOCOL_TCP])
        gfp_data_stream_name = f"{', '.join(src_ports)}_to_{dst_port}_GFP_DATA_{gfp_data_protocol}"
        gfp_data_stream = create_srv6_json_traffic_stream(player_alias, traffic_parameters,
                                                          tc=dscp_to_tc(MRCConsts.GFP_DATA_DSCP, ecn=PerfConsts.ECN_CAPABLE_TRANSPORT),
                                                          stream_name=gfp_data_stream_name,
                                                          BTH={'opcode': int(0x00)}, payload=False, ip_protocol=gfp_data_protocol)
        gfp_data_streams.append(gfp_data_stream)
    return gfp_data_streams


def get_gfp_control_streams(player_alias, traffic_parameters, src_ports, dst_port, both=False):
    gfp_control_streams = []
    traffic_parameters["num_packets"] = 1
    traffic_parameters["packet_size"] = PerfConsts.GFP_CONTROL_SIZE
    if both:
        for protocol in [PerfConsts.IP_PROTOCOL_UDP, PerfConsts.IP_PROTOCOL_TCP]:
            gfp_control_stream_name = f"{', '.join(src_ports)}_to_{dst_port}_GFP_CONTROL_{protocol}"
            gfp_control_stream = create_srv6_json_traffic_stream(player_alias, traffic_parameters,
                                                                 tc=dscp_to_tc(MRCConsts.GFP_CONTROL_DSCP, ecn=PerfConsts.ECN_CAPABLE_TRANSPORT),
                                                                 stream_name=gfp_control_stream_name,
                                                                 BTH={'opcode': int(0x81)}, payload=False, ip_protocol=protocol)
            gfp_control_streams.append(gfp_control_stream)
    else:
        gfp_control_protocol = random.choice([PerfConsts.IP_PROTOCOL_UDP, PerfConsts.IP_PROTOCOL_TCP])
        gfp_control_stream_name = f"{', '.join(src_ports)}_to_{dst_port}_GFP_CONTROL_{gfp_control_protocol}"
        gfp_control_stream = create_srv6_json_traffic_stream(player_alias, traffic_parameters,
                                                             tc=dscp_to_tc(MRCConsts.GFP_CONTROL_DSCP, ecn=PerfConsts.ECN_CAPABLE_TRANSPORT),
                                                             stream_name=gfp_control_stream_name,
                                                             BTH={'opcode': int(0x81)}, payload=False, ip_protocol=gfp_control_protocol)
        gfp_control_streams.append(gfp_control_stream)
    return gfp_control_streams


def get_gfp_streams(player_alias, traffic_parameters, src_ports, dst_port, both=False):
    gfp_data_streams = get_gfp_data_streams(player_alias, traffic_parameters, src_ports, dst_port, both)
    gfp_control_streams = get_gfp_control_streams(player_alias, traffic_parameters, src_ports, dst_port, both)
    gfp_streams = gfp_data_streams + gfp_control_streams
    return gfp_streams


def create_no_srh_stream(player_alias, cli_obj, src_ports, dst_port, traffic_parameters, traffic_type,
                         mloops_dict, dut_interfaces_ipv6_configuration_dict, stream_list,
                         entropy_type=NoSrhPerfConsts.ENTROPY_LOW, packet_size=None,
                         num_packets=NoSrhPerfConsts.NO_SRH_NUM_DATA_PACKETS, **kwargs):
    """Create regular IPv6/UDP traffic stream for no-SRH SRv6 testing (no BTH/MRC headers).

    For high entropy: each packet gets a different DIPv6 lower 64 bits (many flows).
    For low entropy: all packets share the same DIPv6 (single flow).

    Args:
        player_alias: TG alias (e.g. 'left_tg')
        cli_obj: CLI object for the TG
        src_ports: List of source ports
        dst_port: Destination port
        traffic_parameters: Dict with ports, MAC, IP, packet_size etc.
        traffic_type: Must be NoSrhPerfConsts.TRAFFIC_TYPE_SRV6_NO_SRH
        mloops_dict: Mloop port mapping
        dut_interfaces_ipv6_configuration_dict: Port-to-IPv6 mapping
        stream_list: List to append created streams to
        entropy_type: ENTROPY_HIGH or ENTROPY_LOW
        packet_size: Override packet size (if None, uses traffic_parameters default)
        num_packets: Number of data packets per stream
    """
    set_workload_traffic_parameters(cli_obj, traffic_parameters, mloops_dict, src_ports, dst_port,
                                    dut_interfaces_ipv6_configuration_dict, traffic_type)
    get_no_srh_data_stream(player_alias, traffic_parameters, src_ports, dst_port,
                           num_packets=num_packets, stream_list=stream_list,
                           entropy_type=entropy_type, packet_size=packet_size)


def get_no_srh_data_stream(player_alias, traffic_parameters, src_ports, dst_port,
                           num_packets, stream_list, entropy_type, packet_size=None):
    """Create no-SRH data streams with configurable DIPv6 entropy.

    High entropy: N individual streams, each with a unique DIPv6 (lower 64 bits).
    Low entropy: One stream with all packets sharing the same DIPv6.
    Packets are plain IPv6/UDP (no BTH/RoCE headers).

    Each stream emits `packets_per_dip` copies of its template — scaled
    inversely with packet_size so smaller packets get more in-flight packets
    to sustain line rate via the mloop. With ~10 packets per pair, 4096B
    saturates but 512B under flows; the scaling compensates.
    """
    if packet_size is not None:
        traffic_parameters["packet_size"] = packet_size
    actual_pkt_size = traffic_parameters["packet_size"]
    # Scale packets_per_dip inversely with packet_size (relative to the
    # largest packet size in the sweep, which is the reference that hits line
    # rate at 10 packets/pair). pow(ratio, 0.55) is empirical, balancing
    # "too few → underflow" and "too many → buffer/drop".
    reference_size = max(NoSrhPerfConsts.PACKET_SIZE_SWEEP_LIST)
    packets_per_dip = max(1, int(math.pow(
        reference_size / max(actual_pkt_size, 1),
        NoSrhPerfConsts.NO_SRH_PACKETS_PER_DIP_SCALING_EXPONENT)))
    dscp = NoSrhPerfConsts.NO_SRH_DATA_DSCP
    tc = dscp_to_tc(dscp, ecn=PerfConsts.ECN_CAPABLE_TRANSPORT)

    if entropy_type == NoSrhPerfConsts.ENTROPY_HIGH:
        base_dst_ipv6 = ipaddress.ip_address(traffic_parameters["IPV6"]["dst"])
        original_dst = traffic_parameters["IPV6"]["dst"]
        for i in range(num_packets):
            traffic_parameters["IPV6"]["dst"] = str(base_dst_ipv6 + i)
            traffic_parameters["num_packets"] = packets_per_dip
            stream = create_srv6_json_traffic_stream(
                player_alias, traffic_parameters, tc=tc,
                stream_name=f"{', '.join(src_ports)}_to_{dst_port}_NO_SRH_HE_pkt{i}",
                payload=True)
            stream_list.append(stream)
        traffic_parameters["IPV6"]["dst"] = original_dst
    else:
        traffic_parameters["num_packets"] = num_packets * packets_per_dip
        stream = create_srv6_json_traffic_stream(
            player_alias, traffic_parameters, tc=tc,
            stream_name=f"{', '.join(src_ports)}_to_{dst_port}_NO_SRH_LE",
            payload=True)
        stream_list.append(stream)
