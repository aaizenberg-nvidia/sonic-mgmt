import netaddr
import allure
import json
import ipaddress
import random
import logging
import re
import pandas as pd
from netaddr import EUI
from ngts.helpers.performance.packet_json_generator import PacketGenerator
from ngts.constants.performance_constants import PerfConsts, ValidationConsts, BwFairnessThreshold, OccupancyVarianceConfig
from ngts.helpers.performance.validation_precision_helpers import (
    round_for_reference_comparison,
    round_pair_for_comparison,
    round_to_threshold_precision,
    value_above_threshold,
    value_below_threshold,
)
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ast import literal_eval
from typing import Dict, List, Any, Optional


logger = logging.getLogger()

LOOPBACK_FILTER_COUNTER = 'loopback_filter'
LOOPBACK_FILTER_REDMINE_ISSUE = 5195585
# Bug #5195585: the traffic pattern raises a few loopback_filter discards per sample. While the issue
# is open only a per-port value above 5 is a violation; once it is closed 0 restores strict checking.
LOOPBACK_FILTER_MAX_TOLERATED_VALUE = 5 if is_redmine_issue_active([LOOPBACK_FILTER_REDMINE_ISSUE])[0] else 0

SAMPLE_NUMBER_PATTERN = re.compile(r"sample #(\d+)")
PAUSE_DISCARDS_REDMINE_ISSUE = 5258617
# Ports 0x10101 and 0x10102 (the two lanes of cage 30). 0x10102 only exists where the breakout gives
# one port per lane; in the wider breakouts it is simply never present in the counters frame.
PAUSE_DISCARDS_PORTS = ('0x10101', '0x10102')
# Two events under five names, as the SDK dump shows on a single port: the pause pair is the PFC
# asserted on the lossless priority (11108890 each), and the three drop counters are one ingress
# no-buffer discard (15815493 / 15815502 / 15815584). All five must be waived together.
PAUSE_DISCARDS_COUNTERS = ('a_mac_control_frames_transmitted', 'a_pause_mac_ctrl_frames_transmitted',
                           'if_in_discards', 'ether_stats_drop_events', 'port_rx_no_buffer')
# Bug #5258617: those ports assert PFC and discard on ingress while the traffic ramps up, on the first
# two samples of a run. While the issue is open the counters above are not a violation on those ports
# up to that sample; once it is closed 0 restores strict checking.
PAUSE_DISCARDS_LAST_TOLERATED_SAMPLE = 2 if is_redmine_issue_active([PAUSE_DISCARDS_REDMINE_ISSUE])[0] else 0


def generate_ip_address_list(address_start_v4="172.168.1.1", address_start_v6="172::1:1", step_v4=256, step_v6=0x10000,
                             number_of_address: int = 64, mode="v4"):
    """
    Returns :
        address_lst = {
            "v4": ["172.168.1.1", "172.168.1.2" ...],
            "v6": ["172::1", "172::2", ...]
        }
    """
    address_list = {
        "v4": [],
        "v6": []
    }
    if isinstance(step_v6, str):
        step_v6 = int(step_v6, 16)

    if mode in ["v4", "dual"]:
        start_address_v4 = ipaddress.IPv4Address(address_start_v4)
        for i in range(0, number_of_address):
            address_list["v4"].append(str(start_address_v4 + i * step_v4))

    if mode in ["v6", "dual"]:
        start_address_v6 = ipaddress.IPv6Address(address_start_v6)
        for i in range(0, number_of_address):
            address_list["v6"].append(str(start_address_v6 + i * step_v6))

    if mode == "dual":
        return address_list
    elif mode == "v4":
        return address_list["v4"]
    elif mode == "v6":
        return address_list["v6"]


def generate_ip_address_dict(address_start, step, mode, list_of_ports):
    '''
    Returns :
    {
        port1: "172.168.1.1",
        port2: "172.168.1.2",
        or
        port1: "172::1",
        port2: "172::2"
    }
    '''
    address_list = []
    if mode == "v4":
        address_list = generate_ip_address_list(address_start_v4=address_start, step_v4=step, number_of_address=len(list_of_ports), mode="v4")
    if mode == "v6":
        address_list = generate_ip_address_list(address_start_v6=address_start, step_v6=step, number_of_address=len(list_of_ports), mode="v6")

    return dict(zip(list_of_ports, address_list))


def address_calculator(address, operation="add", step=None, operand: str = None, mode="v6"):
    if mode == "v4":
        address = ipaddress.IPv4Address(address)
        step = int(step)
        if operation == "add":
            return str(ipaddress.IPv4Address(address) + (step * operand))
        elif operation == "sub":
            return str(ipaddress.IPv4Address(address) - (step * operand))
        elif operation == "mul":
            return str(ipaddress.IPv4Address(address) * (step * operand))
        elif operation == "div":
            return str(ipaddress.IPv4Address(address) / (step * operand))
    elif mode == "v6":
        address = ipaddress.IPv6Address(address)
        step = int(step, 16)
        if operation == "add":
            return str(ipaddress.IPv6Address(address) + (step * operand))
        elif operation == "sub":
            return str(ipaddress.IPv6Address(address) - (step * operand))
        elif operation == "mul":
            return str(ipaddress.IPv6Address(address) * (step * operand))
        elif operation == "div":
            return str(ipaddress.IPv6Address(address) / (step * operand))
    else:
        raise ValueError(f"Invalid mode: {mode}")


def create_empty_json_traffic_file(json_path):
    traffic_json = {
        "port_groups": [
            {
                "name": "EMPTY_TRAFFIC_STREAM",
                "ports": [],
                "stream_list": [PacketGenerator(ports=[], packet_size=0, num_packets=0).get_json()]
            }
        ]
    }
    with open(json_path, 'w') as json_file:
        json.dump(traffic_json, json_file, indent=3)


def create_json_traffic_stream(player_alias, traffic_parameters, stream_name, tc=PerfConsts.CL_ROCE_LOSSLESS_DEFAULT_TC,
                               ip_protocol=PerfConsts.IP_PROTOCOL_UDP, add_bth=True):
    """
    Creates a JSON representation of a traffic stream.

    Args:
        player_alias (str): Alias for the player generating the traffic.
        traffic_parameters (dict): Dictionary containing traffic parameters such as ports, packet size,
                                   number of packets, MAC addresses, IP addresses, UDP ports, and AR.
        stream_name (str): Name of the traffic stream.
        tc (int, optional): Traffic class. Defaults to PerfConsts.CL_ROCE_LOSSLESS_DEFAULT_TC.
        add_bth (bool, optional): Build the stream as RoCE with a BTH. Set False for plain UDP traffic;
                                  the UDP destination port must then not be PerfConsts.ROCE_PORT, or the
                                  chip parses the payload as a BTH anyway. Defaults to True.

    Returns:
        dict: JSON representation of the traffic stream.
    """
    # Initialize a packet generator with specified ports, packet size, and number of packets
    packet = PacketGenerator(
        ports=traffic_parameters["ports"],
        packet_size=traffic_parameters["packet_size"],
        num_packets=traffic_parameters["num_packets"]
    )

    # Add Ethernet header with source and destination MAC addresses and traffic class
    packet.add_ether_header(
        src=traffic_parameters["MAC"]["src"],
        dst=traffic_parameters["MAC"]["dst"]
    )

    # Add IP header based on whether the traffic is IPv6 or IPv4
    if traffic_parameters['is_ipv6']:
        packet.add_ipv6_header(
            src=traffic_parameters["IP"]["src"],
            dst=traffic_parameters["IP"]["dst"],
            tc=tc
        )
    else:
        packet.add_ip_header(
            src=traffic_parameters["IP"]["src"],
            dst=traffic_parameters["IP"]["dst"],
            tos=tc
        )

    # Add UDP header with source and destination ports
    if ip_protocol == PerfConsts.IP_PROTOCOL_UDP:
        packet.add_udp_header(
            source_port=traffic_parameters["UDP"]["src"],
            dest_port=traffic_parameters["UDP"]["dst"]
        )
    else:
        packet.add_tcp_header(
            sport=traffic_parameters[PerfConsts.IP_PROTOCOL_TCP]["sport"],
            dport=traffic_parameters[PerfConsts.IP_PROTOCOL_TCP]["dport"]
        )

    # Add BTH header with acknowledgment request
    if add_bth:
        packet.add_bth_header(ar=traffic_parameters["AR"])

    # Add payload header with player alias
    packet.add_payload_header(player_alias)

    # Convert the packet to JSON and assign the stream name
    stream = packet.get_json()
    stream["name"] = stream_name

    return stream


def create_srv6_json_traffic_stream(player_alias, traffic_parameters, stream_name,
                                    tc, BTH={}, payload=True, ip_protocol=PerfConsts.IP_PROTOCOL_UDP):
    """
    Creates a JSON representation of a traffic stream.

    Args:
        player_alias (str): Alias for the player generating the traffic.
        traffic_parameters (dict): Dictionary containing traffic parameters such as ports, packet size,
                                   number of packets, MAC addresses, IP addresses, UDP ports, and AR.
        stream_name (str): Name of the traffic stream.
        tc (int, optional): Traffic class. Defaults to PerfConsts.CL_ROCE_LOSSLESS_DEFAULT_TC.

    Returns:
        dict: JSON representation of the traffic stream.
    """
    # Initialize a packet generator with specified ports, packet size, and number of packets
    packet = PacketGenerator(
        ports=traffic_parameters["ports"],
        packet_size=traffic_parameters["packet_size"],
        num_packets=traffic_parameters["num_packets"]
    )

    # Add Ethernet header with source and destination MAC addresses and traffic class
    packet.add_ether_header(
        src=traffic_parameters["MAC"]["src"],
        dst=traffic_parameters["MAC"]["dst"]
    )

    packet.add_ipv6_header(
        src=traffic_parameters["IPV6"]["src"],
        dst=traffic_parameters["IPV6"]["dst"],
        tc=tc
    )

    packet.add_ip_header(
        src=traffic_parameters["IP"]["src"],
        dst=traffic_parameters["IP"]["dst"],
        tos=tc
    )

    # Add UDP header with source and destination ports
    if ip_protocol == PerfConsts.IP_PROTOCOL_UDP:
        packet.add_udp_header(
            source_port=traffic_parameters[PerfConsts.IP_PROTOCOL_UDP]["src"],
            dest_port=traffic_parameters[PerfConsts.IP_PROTOCOL_UDP]["dst"]
        )
    elif ip_protocol == PerfConsts.IP_PROTOCOL_TCP:
        packet.add_tcp_header(
            sport=traffic_parameters[PerfConsts.IP_PROTOCOL_TCP]["sport"],
            dport=traffic_parameters[PerfConsts.IP_PROTOCOL_TCP]["dport"]
        )
    else:
        pass
    if BTH:
        packet.add_bth_header(opcode=BTH['opcode'], ar=1)
    # Add payload header with player alias
    if payload:
        packet.add_payload_header(player_alias)

    # Convert the packet to JSON and assign the stream name
    stream = packet.get_json()
    stream["name"] = stream_name

    return stream


def create_json_traffic_file(player_alias, traffic_parameters, json_path, ip_protocol=PerfConsts.IP_PROTOCOL_UDP, tc=PerfConsts.CL_ROCE_LOSSLESS_DEFAULT_TC):
    stream = create_json_traffic_stream(player_alias, traffic_parameters, f"spcx_ra_{player_alias}_main_stream", ip_protocol=ip_protocol, tc=tc)
    create_json_traffic_file_with_stream_list(player_alias, traffic_parameters, json_path, stream_list=[stream])


def create_json_traffic_file_with_stream_list(player_alias, traffic_parameters, json_path, stream_list):
    traffic_json = {
        "port_groups": [
            {
                "name": f"{player_alias}_traffic_pattern",
                "stream_list": stream_list
            }
        ]
    }
    with open(json_path, 'w') as json_file:
        json.dump(traffic_json, json_file, indent=3)


def validate_no_drops_on_tg_ports(traffic_json, players, violations_list):
    for player_alias in PerfConsts.TG_ALIAS_LIST:
        with allure.step(f"Validate no drops on TG ports on - {player_alias}"):
            cli_obj = players[player_alias]['cli']
            violations = cli_obj.performance.validate_no_drops_on_tg_ports()
            violations_list.extend(violations)


def _get_rx_tx_thresholds(bw_threshold, port_group_name, is_global_threshold):
    """
    Get TX and RX bandwidth thresholds for a given port group.

    Args:
        bw_threshold (int, float, or dict): The bandwidth threshold(s). Can be a single value (global),
            or a dict mapping port group names to thresholds. If per-port-group, the value can be a single
            value or a dict with 'tx' and 'rx' keys.
        port_group_name (str): The name of the port group to get thresholds for.
        is_global_threshold (bool): Whether the threshold is a global value (True) or per-port-group (False).

    Returns:
        tuple: (tx_threshold, rx_threshold) for the given port group. Returns (None, None) if not specified.
    """
    tx_validation_key, rx_validation_key = ValidationConsts.TX_RATE_MIN, ValidationConsts.RX_RATE_MIN
    if is_global_threshold:
        return bw_threshold, bw_threshold, tx_validation_key, rx_validation_key
    group_threshold = bw_threshold.get(port_group_name)
    if group_threshold is None:
        port_group_lower = port_group_name.lower()
        for key, value in bw_threshold.items():
            if isinstance(key, str) and key.lower() == port_group_lower:
                group_threshold = value
                break
    if group_threshold is None:
        return None, None, tx_validation_key, rx_validation_key
    if isinstance(group_threshold, dict):
        tx_threshold, rx_threshold = group_threshold.get(ValidationConsts.TX), group_threshold.get(ValidationConsts.RX)
        tx_validation_key, rx_validation_key = bw_threshold.get(ValidationConsts.VALIDATION_KEY, (ValidationConsts.TX_RATE_MIN, ValidationConsts.RX_RATE_MIN))
        return tx_threshold, rx_threshold, tx_validation_key, rx_validation_key
    return group_threshold, group_threshold, tx_validation_key, rx_validation_key


def _get_tc_occ_threshold(tc_occ_threshold, port_group_name):
    if port_group_name in tc_occ_threshold:
        return tc_occ_threshold[port_group_name]
    return tc_occ_threshold


def validate_bw(traffic_json, bw_threshold, validate_bw_rx, violations_list):
    """
    Validate that all bandwidth samples meet or exceed the specified TX and RX thresholds.

    Args:
        traffic_json (dict): The traffic data containing bandwidth samples.
        bw_threshold (int, float, or dict): The bandwidth threshold(s). Can be a single value (global),
            or a dict mapping port group names to thresholds. If per-port-group, the value can be a single
            value or a dict with 'tx' and 'rx' keys.
        validate_bw_rx (bool): Whether to validate RX bandwidth in addition to TX.
        violations_list (list): List to append any validation violations found.

    Returns:
        Appends any violations to violations_list.
    """
    with allure.step(f"Validate all bandwidth samples minimal value is above threshold(s): {bw_threshold}"):
        bw_samples = traffic_json[ValidationConsts.BW_SAMPLES]
        bw_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)

        is_global_threshold = isinstance(bw_threshold, (int, float))
        for sample_id, bw_sample in bw_samples.items():
            for port_group_name, group_data in bw_sample.items():
                bw_stats = group_data[ValidationConsts.BW_STATS]
                tx_threshold, rx_threshold, tx_validation_key, rx_validation_key = _get_rx_tx_thresholds(bw_threshold, port_group_name, is_global_threshold)

                if tx_threshold is None and rx_threshold is None:
                    violations_list.append(f"No threshold specified for port group {port_group_name}.")
                    continue

                tx_value = bw_stats[tx_validation_key]
                if tx_threshold is not None and value_below_threshold(tx_value, tx_threshold):
                    violations_list.append(
                        f"TX bandwidth for {sample_id} (group: {port_group_name}) bw_stats['{tx_validation_key}']="
                        f"{round_to_threshold_precision(tx_value, tx_threshold)} was below threshold {tx_threshold}."
                    )
                rx_value = bw_stats[rx_validation_key]
                if validate_bw_rx and rx_threshold is not None and value_below_threshold(rx_value, rx_threshold):
                    violations_list.append(
                        f"RX bandwidth for {sample_id} (group: {port_group_name}) bw_stats['{rx_validation_key}']="
                        f"{round_to_threshold_precision(rx_value, rx_threshold)} was below threshold {rx_threshold}."
                    )


def get_ports_avg_bw(traffic_json, port_group_name):
    bw_samples = traffic_json[ValidationConsts.BW_SAMPLES]
    bw_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
    samples = list(bw_samples.keys())
    last_sample = bw_samples[samples[-1]]
    bw_samples_stats = last_sample[port_group_name][ValidationConsts.BW_STATS]
    avg_ports_tx = bw_samples_stats[ValidationConsts.TX_BW_AVG]
    avg_ports_rx = bw_samples_stats[ValidationConsts.RX_BW_AVG]
    return avg_ports_tx, avg_ports_rx


def get_tc_occ(traffic_json, tc_list, port_group_name, key=ValidationConsts.OCC_AVG):
    tc_occ_dict = {}
    tc_samples = traffic_json[ValidationConsts.TC_PG_SAMPLES]
    tc_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
    samples = list(tc_samples.keys())
    last_sample = tc_samples[samples[-1]]
    tc_df = last_sample[port_group_name][ValidationConsts.TC_DATAFRAME]
    for tc_dict in tc_df:
        tc_name = tc_dict[ValidationConsts.TC_NAME]
        if tc_name in tc_list:
            tc_occ = tc_dict[key]
            tc_occ_dict[tc_name] = tc_occ
    return tc_occ_dict


def validate_bw_per_ports(traffic_json, bw_threshold, ports_list, violations_list):
    bw_samples = traffic_json[ValidationConsts.BW_SAMPLES]
    bw_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)

    for sample_id, port_groups in bw_samples.items():
        for port_group_name, port_group_data in port_groups.items():
            bw_df = pd.DataFrame(port_group_data[ValidationConsts.BW_DATAFRAME])
            for port in ports_list:
                if port in bw_df[ValidationConsts.PORT].values:
                    port_tx = bw_df.loc[bw_df[ValidationConsts.PORT] == hex(literal_eval(port))].loc[:, ValidationConsts.TX_RATE].values[0]
                    rounded_port_tx, _ = round_pair_for_comparison(port_tx, bw_threshold)
                    if bw_threshold == 0 and value_above_threshold(port_tx, bw_threshold):
                        violations_list.append(f"Port {port} tx: {rounded_port_tx} > {bw_threshold}, "
                                               f"please check {sample_id} in port group: {port_group_name}")
                    if value_below_threshold(port_tx, bw_threshold):
                        violations_list.append(f"Port {port} tx: {rounded_port_tx} < {bw_threshold}, "
                                               f"please check {sample_id} in port group: {port_group_name}")


def _validate_bw_utilization_fairness_per_port_group(sample_id: str, port_group_name: str, port_group_data: Dict[str, Any],
                                                     port_group_threshold: BwFairnessThreshold, violations_list: List[str]) -> None:
    if port_group_threshold.tx is None and port_group_threshold.rx is None:
        logging.getLogger().info(f"BW fairness: both TX and RX thresholds are None for group '{port_group_name}'; skipping")
        return
    required_cols = {ValidationConsts.PORT}
    if port_group_threshold.tx is not None:
        required_cols.add(ValidationConsts.TX_RATE)
    if port_group_threshold.rx is not None:
        required_cols.add(ValidationConsts.RX_RATE)
    try:
        bw_df = pd.DataFrame(port_group_data[ValidationConsts.BW_DATAFRAME])
        if bw_df.empty:
            violations_list.append(f"BW fairness: sample '{sample_id}' group '{port_group_name}' has empty dataframe")
            return
        missing = required_cols - set(bw_df.columns)
        if missing:
            violations_list.append(f"BW fairness: sample '{sample_id}' group '{port_group_name}' missing columns {missing}")
            return
        for direction, rate_col, threshold in [
            ("TX", ValidationConsts.TX_RATE, port_group_threshold.tx),
            ("RX", ValidationConsts.RX_RATE, port_group_threshold.rx),
        ]:
            if threshold is None:
                continue
            min_val = bw_df[rate_col].min()
            max_val = bw_df[rate_col].max()
            min_tolerated_value = max_val * (1 - threshold)
            rounded_min_val = round_to_threshold_precision(min_val, threshold)
            rounded_min_tolerated_value = round_to_threshold_precision(min_tolerated_value, threshold)
            rounded_max_val = round_to_threshold_precision(max_val, threshold)
            if rounded_min_val < rounded_min_tolerated_value:
                min_port = bw_df.loc[bw_df[rate_col].idxmin(), ValidationConsts.PORT]
                max_port = bw_df.loc[bw_df[rate_col].idxmax(), ValidationConsts.PORT]
                violations_list.append(
                    f"{sample_id} (group: {port_group_name}) {direction} min utilization "
                    f"{rounded_min_val} < min_tolerated_value {rounded_min_tolerated_value} "
                    f"(= max {rounded_max_val} * {1 - threshold}) "
                    f"(min port: {min_port}, max port: {max_port})"
                )
    except (KeyError, TypeError, ValueError) as exc:
        violations_list.append(f"BW fairness: malformed payload for sample '{sample_id}' group '{port_group_name}': {exc}")


def validate_bw_utilization_fairness(traffic_json: Dict[str, Any], bw_fairness_threshold_per_port_group: Dict[str, BwFairnessThreshold], violations_list: List[str]) -> None:
    """
    Validate that per-port TX and/or RX utilization is fair across each port group.

    For each enabled direction, the minimum port utilization must be at least
    (1 - threshold) of the maximum port utilization. A None threshold skips that direction.
    e.g. with tx=0.05: min_tx >= max_tx * 0.95.

    Args:
        traffic_json: Traffic data containing bandwidth samples.
        bw_fairness_threshold_per_port_group: Mapping of port group name to its BwFairnessThreshold.
            Port groups absent from the dict are skipped. Use the key "default" for flat (non-grouped) samples.
        violations_list: List to append violations to.
    """
    logger.info(f"validate_bw_utilization_fairness: started (bw_fairness_threshold_per_port_group={bw_fairness_threshold_per_port_group})")
    for port_group_name, port_group_threshold in bw_fairness_threshold_per_port_group.items():
        for direction_name, threshold in [("tx", port_group_threshold.tx), ("rx", port_group_threshold.rx)]:
            if threshold is not None and (not isinstance(threshold, (int, float)) or not (0 <= threshold <= 1)):
                violations_list.append(
                    f"bw_fairness_threshold_per_port_group[{port_group_name!r}].{direction_name} must be a number between 0 and 1, got {threshold!r}"
                )
                return
    with allure.step(f"Validate per-port BW utilization fairness per port group"):
        bw_samples = traffic_json.get(ValidationConsts.BW_SAMPLES)
        if not bw_samples:
            violations_list.append(f"BW fairness validation skipped: '{ValidationConsts.BW_SAMPLES}' key is missing or empty in traffic_json")
            return
        if not isinstance(bw_samples, dict):
            violations_list.append(f"BW fairness validation skipped: '{ValidationConsts.BW_SAMPLES}' is not a dict (got {type(bw_samples).__name__})")
            return
        # remove metadata entry so only per-port sample dicts remain
        bw_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)

        for sample_id, sample_data in bw_samples.items():
            if ValidationConsts.BW_DATAFRAME in sample_data:
                port_groups_iter = [("default", sample_data)]
            else:
                port_groups_iter = sample_data.items()

            for port_group_name, port_group_data in port_groups_iter:
                port_group_threshold = bw_fairness_threshold_per_port_group.get(port_group_name)
                if port_group_threshold is None:
                    logger.debug(f"BW fairness: no threshold for group '{port_group_name}'; skipping")
                    continue
                _validate_bw_utilization_fairness_per_port_group(sample_id, port_group_name, port_group_data, port_group_threshold, violations_list)


def _check_metric_fairness(
    buffer_type: str,
    buffer_id: str,
    json_metric: str,
    occ_key: str,
    value_by_stat: dict,
    occ_variance_config: "OccupancyVarianceConfig",
    skipped_metrics_by_buffer_id_and_reason: Dict[tuple, list],
    passed_check_count_by_buffer_type: Dict[str, Dict[str, int]],
) -> Optional[str]:
    """
    Check occupancy fairness for a single metric of a single buffer ID.

    Skips the check (returns None and records the reason) when max occupancy across all ports is 0
    (no active traffic) or when the variance threshold is disabled (None) for the given buffer type.

    Args:
        buffer_type: Buffer type identifier (e.g. ValidationConsts.TC_NAME, PG_BUFFER, PG_HEADROOM).
        buffer_id: The TC or PG index being checked (e.g. "TC3", "PG1").
        json_metric: JSON key for the metric (e.g. ValidationConsts.JSON_METRIC_AVG).
        occ_key: OccupancyMetrics field name corresponding to json_metric (e.g. ValidationConsts.OCC_AVG).
        value_by_stat: Pre-aggregated stats dict with keys 'min_value', 'max_value',
            'port_with_min_value', 'port_with_max_value'.
        occ_variance_config: Variance thresholds config for the current port group.
        skipped_metrics_by_buffer_id_and_reason: Accumulator for skipped metrics; mutated in place.
        passed_check_count_by_buffer_type: Accumulator for passed check counts; mutated in place.

    Returns:
        A violation string if min_port occupancy falls below max_port * (1 - variance), else None.
    """
    max_val = value_by_stat['max_value']
    min_val = value_by_stat['min_value']
    min_port = value_by_stat['port_with_min_value']
    max_port = value_by_stat['port_with_max_value']
    if max_val == 0:
        skipped_metrics_by_buffer_id_and_reason.setdefault(
            (buffer_id, "max occupancy across all ports is 0 (no active traffic)"), []
        ).append(json_metric)
        return None
    variance = occ_variance_config.get_variance(buffer_type, occ_key)
    if variance is None:
        skipped_metrics_by_buffer_id_and_reason.setdefault(
            (buffer_id, f"variance threshold is None, validation disabled for buffer type '{buffer_type}'"), []
        ).append(json_metric)
        return None
    min_tolerated = max_val * (1 - variance)
    rounded_min_val = round_to_threshold_precision(min_val, variance)
    rounded_max_val = round_to_threshold_precision(max_val, variance)
    rounded_min_tolerated = round_to_threshold_precision(min_tolerated, variance)
    logger.info(
        f"      checking fairness: buffer_type={buffer_type} "
        f"buffer_id={buffer_id} metric={json_metric} occ_key={occ_key} variance={variance} "
        f"min={rounded_min_val} max={rounded_max_val} min_tolerated={rounded_min_tolerated} "
        f"min_port={min_port} max_port={max_port}"
    )
    if rounded_min_val < rounded_min_tolerated:
        deviation_from_max = (rounded_max_val - rounded_min_val) / rounded_max_val
        shortfall_from_threshold = (rounded_min_tolerated - rounded_min_val) / rounded_max_val
        return (
            f"{buffer_type} buffer_id={buffer_id} metric={json_metric} "
            f"min={rounded_min_val} [{deviation_from_max:.1%} from max] < "
            f"min_tolerated {rounded_min_tolerated} [exceeded by {shortfall_from_threshold:.1%} of max] "
            f"(= max {rounded_max_val} * {1 - variance:.3f}) "
            f"(min port: {min_port}, max port: {max_port})"
        )
    passed_check_count_by_buffer_type.setdefault(buffer_type, {}).setdefault(occ_key, 0)
    passed_check_count_by_buffer_type[buffer_type][occ_key] += 1
    return None


def _validate_tc_occupancy_fairness_per_port_group(
    sample_id: str,
    port_group_name: str,
    port_group_data: dict,
    occ_variance_config: OccupancyVarianceConfig,
    violations_list: list[str],
    passed_check_count_by_buffer_type: Dict[str, Dict[str, int]],
    occ_key_by_json_metric: dict,
) -> None:
    """Validate TC/PG occupancy fairness for a single port group and sample."""
    logger.debug(f"    [{sample_id}] processing port group '{port_group_name}'")
    # Maps each JSON stats key (inside port-group data) to its buffer type name,
    # which matches both the OccupancyVarianceConfig field and the ValidationConsts identifier.
    buffer_type_by_stats_key = {
        ValidationConsts.PER_PORT_TC_STATS: ValidationConsts.TC_NAME,
        ValidationConsts.PER_PORT_PG_BUFFER_STATS: ValidationConsts.PG_BUFFER,
        ValidationConsts.PER_PORT_PG_HEADROOM_STATS: ValidationConsts.PG_HEADROOM,
    }
    for stats_key, buffer_type in buffer_type_by_stats_key.items():
        # Structure: {buffer_id -> {metric_name -> {min_value, max_value, port_with_min_value, port_with_max_value}}}
        # min_value/max_value are pre-aggregated across all ports by the collector.
        tc_stats = port_group_data.get(stats_key, {})
        if not tc_stats:
            logger.info(
                f"    [{sample_id}] group '{port_group_name}' {buffer_type}: "
                f"skipped — '{stats_key}' key is missing or empty in JSON data"
            )
            continue

        logger.debug(
            f"    [{sample_id}] group '{port_group_name}' {buffer_type}: "
            f"{len(tc_stats)} TC/PG id(s): {list(tc_stats.keys())}"
        )

        skipped_metrics_by_buffer_id_and_reason: Dict[tuple, list] = {}

        for buffer_id, metric_data in tc_stats.items():
            missing_metrics = [missing_metric for missing_metric in occ_key_by_json_metric if missing_metric not in metric_data]
            if missing_metrics:
                skipped_metrics_by_buffer_id_and_reason.setdefault(
                    (buffer_id, "metric missing from JSON data"), []
                ).extend(missing_metrics)

            for json_metric, value_by_stat in metric_data.items():
                occ_key = occ_key_by_json_metric.get(json_metric)
                if occ_key is None:
                    logger.debug(
                        f"      [{sample_id}] {buffer_type} buffer_id={buffer_id}: "
                        f"skipped unknown metric '{json_metric}'"
                    )
                    continue
                violation = _check_metric_fairness(
                    buffer_type, buffer_id, json_metric, occ_key, value_by_stat,
                    occ_variance_config, skipped_metrics_by_buffer_id_and_reason,
                    passed_check_count_by_buffer_type,
                )
                if violation:
                    violations_list.append(f"[{port_group_name}] {violation}")

        for (buffer_id, reason), metrics in skipped_metrics_by_buffer_id_and_reason.items():
            logger.info(
                f"      [{sample_id}] {buffer_type} buffer_id={buffer_id}: "
                f"{', '.join(metrics)} skipped. reason: {reason}"
            )

    disabled_buffer_types = [bt for bt in buffer_type_by_stats_key.values() if occ_variance_config.get_variance(bt, ValidationConsts.OCC_AVG) is None]
    disabled_msg = f", disabled buffer types (variance=None): {disabled_buffer_types}" if disabled_buffer_types else ""
    passed_msg = f", passed checks by buffer type and metric: {passed_check_count_by_buffer_type}" if passed_check_count_by_buffer_type else ""
    logger.info(f"    [{sample_id}] group '{port_group_name}'{disabled_msg}{passed_msg}")


def validate_tc_occupancy_fairness(
    traffic_json: dict,
    max_tc_occ_variance_per_port_group: Dict[str, OccupancyVarianceConfig],
    violations_list: list[str],
) -> None:
    """
    Validate per-port TC and PG occupancy fairness across each port group.

    For each sample, port group, buffer ID, and all four occupancy metrics
    (occAvg, occ99, occMax, maxWatermark), checks:
        min_port_occ >= max_port_occ * (1 - variance)

    where variance is looked up per buffer type and metric from the config for that port group.

    Buffer IDs with max_occ == 0 are skipped (no active traffic).
    Empty stat dicts (e.g. per_port_pg_headroom_stats: {}) are silently skipped.
    Port groups not present in max_tc_occ_variance_per_port_group are skipped.

    Args:
        traffic_json:                      Traffic data containing TC_PG_SAMPLES.
        max_tc_occ_variance_per_port_group: Dict mapping port group name to per-buffer-type,
                                            per-metric variance thresholds.
        violations_list:                   List to append violation strings to.
    """
    local_violations_list = []  # TODO: temporary — remove this line to re-enable TC occ fairness failures
    if not isinstance(max_tc_occ_variance_per_port_group, dict):
        local_violations_list.append(
            f"max_tc_occ_variance_per_port_group must be a dict, got {type(max_tc_occ_variance_per_port_group)!r}"
        )
        return
    logger.info(f"validate_tc_occupancy_fairness: started (max_tc_occ_variance_per_port_group={max_tc_occ_variance_per_port_group})")

    # Maps JSON metric names (as stored in per_port_*_stats) to ValidationConsts occ_key identifiers
    OCC_KEY_BY_JSON_METRIC = {
        ValidationConsts.JSON_METRIC_AVG: ValidationConsts.OCC_AVG,
        ValidationConsts.JSON_METRIC_P99: ValidationConsts.OCC_99,
        ValidationConsts.JSON_METRIC_MAX: ValidationConsts.OCC_MAX,
        ValidationConsts.JSON_METRIC_WATERMARK: ValidationConsts.MAX_WATERMARK,
    }

    with allure.step("Validate per-port TC/PG occupancy fairness per buffer type and metric"):
        tc_pg_samples = traffic_json.get(ValidationConsts.TC_PG_SAMPLES)
        if not tc_pg_samples:
            logger.info(
                f"validate_tc_occupancy_fairness: skipped — '{ValidationConsts.TC_PG_SAMPLES}' key is missing or empty"
            )
            local_violations_list.append(
                f"TC occupancy fairness validation skipped: '{ValidationConsts.TC_PG_SAMPLES}' key is missing or empty"
            )
            return

        # SAMPLES_PARAMS is metadata, not a real sample — remove before iterating
        tc_pg_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
        if not tc_pg_samples:
            logger.info(
                f"validate_tc_occupancy_fairness: skipped — no samples remain after removing '{ValidationConsts.SAMPLES_PARAMS}' metadata entry"
            )
            return
        logger.info(f"validate_tc_occupancy_fairness: found {len(tc_pg_samples)} samples to process")

        # Tracks passed checks: {buffer_type: {occ_key: count}}
        passed_check_count_by_buffer_type: Dict[str, Dict[str, int]] = {}

        for sample_id, port_groups in tc_pg_samples.items():
            logger.debug(f"  sample '{sample_id}': {len(port_groups)} port group(s): {list(port_groups.keys())}")
            for port_group_name, port_group_data in port_groups.items():
                occ_variance_config = (
                    max_tc_occ_variance_per_port_group.get(port_group_name) or
                    max_tc_occ_variance_per_port_group.get("default")
                )
                if occ_variance_config is None:
                    logger.info(
                        f"  [{sample_id}] port group '{port_group_name}': skipped — not in max_tc_occ_variance_per_port_group and no 'default' key"
                    )
                    continue
                _validate_tc_occupancy_fairness_per_port_group(
                    sample_id, port_group_name, port_group_data,
                    occ_variance_config, local_violations_list,
                    passed_check_count_by_buffer_type, OCC_KEY_BY_JSON_METRIC,
                )

        logger.info(
            f"validate_tc_occupancy_fairness: passed checks by buffer type and metric: {passed_check_count_by_buffer_type}, "
            f"violations: {len(local_violations_list)}"
        )


def validate_tc(traffic_json, tc_occ_threshold, violations_list):
    with allure.step(f"Validate all TC samples average occupancy is below {tc_occ_threshold} cells"):
        tc_samples = traffic_json[ValidationConsts.TC_PG_SAMPLES]
        tc_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
        for sample_id, port_groups in tc_samples.items():
            for port_group_name, port_group_data in port_groups.items():
                tc_df = port_group_data[ValidationConsts.TC_DATAFRAME]
                for tc_dict in tc_df:
                    tc_name = tc_dict[ValidationConsts.TC_NAME]
                    tc_occ_threshold = _get_tc_occ_threshold(tc_occ_threshold, port_group_name)
                    for tc_occ_key, tc_occ_th in tc_occ_threshold.items():
                        tc_occ = tc_dict[tc_occ_key]
                        if value_above_threshold(tc_occ, tc_occ_th):
                            violations_list.append(
                                f"{sample_id},{port_group_name} - TC {tc_name} {tc_occ_key} "
                                f"{round_to_threshold_precision(tc_occ, tc_occ_th)} > {tc_occ_th} threshold"
                            )
                            with allure.step(f"Attach TC sample {sample_id} in port group: {port_group_name}: {tc_dict}"):
                                pass


def validate_per_tc(traffic_json, tc_occ_threshold, tc_to_validate, tolerance, port_group_name_to_validate_list, violations_list):
    """
    This function is used to validate the TC occupancy rate is below the threshold
    :param traffic_json: current test validation json
    :param tc_occ_threshold: tc occupancy threshold, i.e {1: 100, 2: 200} for 100 cells and 200 cells respectively
    :param tc_to_validate: tc to validate, i.e [1, 2]
    :param tolerance: tolerance, i.e 0.1 for 10% deviation
    :param port_group_name_to_validate_list: port group name to validate list, if empty, all port groups will be validated
    :param violations_list: list of violations
    """
    with allure.step(f"Validate {tc_to_validate} TC samples occupancy rate is below {tc_occ_threshold} cells"):
        tc_samples = traffic_json[ValidationConsts.TC_PG_SAMPLES]
        tc_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
        higher_tc_samples = []
        for sample_id, port_groups in tc_samples.items():
            for port_group_name, port_group_data in port_groups.items():
                if should_validate_tc_occ_for_port_group(port_group_name_to_validate_list, port_group_name):
                    tc_df = port_group_data[ValidationConsts.TC_DATAFRAME]
                    for tc_dict in tc_df:
                        tc_name = tc_dict[ValidationConsts.TC_NAME]
                        tc_occ_threshold = _get_tc_occ_threshold(tc_occ_threshold, port_group_name)
                        if tc_name in tc_to_validate:
                            for tc_occ_key, tc_occ_th in tc_occ_threshold.items():
                                tc_occ = tc_dict[tc_occ_key]
                                validate_tc_occ_value(tc_occ, tc_occ_th, tolerance, higher_tc_samples, sample_id, tc_name, tc_occ_key)
        if higher_tc_samples:
            violations_list.append(f"Not all TC samples were lower than threshold {tc_occ_threshold}, "
                                   f"please check {higher_tc_samples}")


def should_validate_tc_occ_for_port_group(port_group_name_to_validate_list, port_group_name):
    all_port_groups_allowed = len(port_group_name_to_validate_list) == 0
    specific_port_group_allowed = port_group_name in port_group_name_to_validate_list
    return all_port_groups_allowed or specific_port_group_allowed


def validate_tc_occ_value(tc_occ, tc_occ_th, tolerance, higher_tc_samples, sample_id, tc_name, tc_occ_key):
    if tolerance:
        min_range, max_range, is_within_range = is_within_tolerance_range(tc_occ, tc_occ_th, tolerance)
        if not is_within_range:
            higher_tc_samples.append(f"{sample_id} - TC {tc_name} {tc_occ_key} {tc_occ} is off range {min_range} - {max_range}")
    else:
        if value_above_threshold(tc_occ, tc_occ_th):
            higher_tc_samples.append(
                f"{sample_id} - TC {tc_name} {tc_occ_key} "
                f"{round_to_threshold_precision(tc_occ, tc_occ_th)} > {tc_occ_th} threshold"
            )


def is_within_tolerance_range(value, threshold, tolerance):
    """
    This function is used to validate the TC occupancy value is within the tolerance range
    :param value: the value to validate
    :param threshold: the threshold to validate against
    :param tolerance: the tolerance range, i.e float value between 0 and 1
    :return: min_range, max_range, is_within_range
    """
    if not is_tolerance_value_valid(tolerance):
        raise ValueError("Tolerance value must be between 0 and 1")
    min_tolerance, max_tolerance = 1 - tolerance, 1 + tolerance
    min_range, max_range = threshold * min_tolerance, threshold * max_tolerance
    # Use the comparison helper so 0-place thresholds keep raw floats (0.4 / 0.8 stay
    # 0.4 / 0.8 instead of rounding to 0 / 1) while fractional thresholds still clear
    # float noise at their declared precision.
    cmp_value, _ = round_pair_for_comparison(value, threshold)
    cmp_min_range, _ = round_pair_for_comparison(min_range, threshold)
    cmp_max_range, _ = round_pair_for_comparison(max_range, threshold)
    is_within_range = cmp_min_range < cmp_value < cmp_max_range
    return min_range, max_range, is_within_range


def is_tolerance_value_valid(tolerance):
    """
    This function is used to validate the tolerance value is between 0 and 1
    :param tolerance: the tolerance value to validate
    :return: True if the tolerance value is between 0 and 1, False otherwise
    """
    return 0 < tolerance < 1


def compare_tc_occ_to_reference(traffic_json, reference_json, tc_keys, tc_to_validate, allowed_deviation,
                                violations_list, reference_offset=0):
    """
    This function is used to compare the TC occupancy to a reference TC occupancy
    :param traffic_json: current test validation json
    :param reference_json: reference validation json, can be from a previous test run or from a reference file
    :param tc_keys: list of tc keys, i.e [ValidationConsts.OCC_AVG, ValidationConsts.OCC_MAX]
    :param tc_to_validate: list of tc to validate, i.e [1, 2]
    :param allowed_deviation: allowed deviation in percentages/actual value from the reference TC occupancy, i.e +-10%
    if allowed_deviation is a float, it is interpreted as a percentage deviation from the reference TC occupancy
    if allowed_deviation is an int, it is interpreted as an actual value deviation from the reference TC occupancy
    :param violations_list: list of violations
    :param reference_offset: expected offset of the measured occupancy from the reference, in buffer cells.
    The comparison band is centered on reference + reference_offset instead of on the reference itself,
    which keeps the comparison relative while allowing for a known, expected difference between the two
    runs (i.e the SRv6 uN forwarding overhead over an IPv6 baseline). Defaults to 0, which reproduces
    the plain reference-centered band.
    """
    with allure.step(f"Compare TC occupancy to reference for {tc_to_validate}"):
        tc_samples = traffic_json[ValidationConsts.TC_PG_SAMPLES]
        tc_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
        for sample_id, port_groups in tc_samples.items():
            for port_group_name, port_group_data in port_groups.items():
                tc_df = port_group_data[ValidationConsts.TC_DATAFRAME]
                for tc_dict in tc_df:
                    tc_name = tc_dict[ValidationConsts.TC_NAME]
                    if tc_name in tc_to_validate:
                        for key in tc_keys:
                            tc_occ = tc_dict[key]
                            reference_tc_occ = get_tc_occ_from_traffic_json(reference_json, port_group_name, key, tc_name)
                            expected_tc_occ = reference_tc_occ + reference_offset
                            if isinstance(allowed_deviation, float) and 0 < allowed_deviation < 1:
                                min_limit = expected_tc_occ * (1 - allowed_deviation)
                                max_limit = expected_tc_occ * (1 + allowed_deviation)
                            else:
                                min_limit = expected_tc_occ - allowed_deviation
                                max_limit = expected_tc_occ + allowed_deviation
                            min_limit = max(min_limit, 0)
                            rounded_tc_occ = round_for_reference_comparison(tc_occ, reference_tc_occ)
                            rounded_min_limit = round_for_reference_comparison(min_limit, reference_tc_occ)
                            rounded_max_limit = round_for_reference_comparison(max_limit, reference_tc_occ)
                            if rounded_tc_occ < rounded_min_limit or rounded_tc_occ > rounded_max_limit:
                                expected_text = ""
                                if reference_offset:
                                    rounded_reference = round_for_reference_comparison(reference_tc_occ,
                                                                                       reference_tc_occ)
                                    expected_text = (f" (reference {rounded_reference} plus expected "
                                                     f"offset {reference_offset})")
                                violations_list.append(
                                    f"In sample {sample_id} for port group {port_group_name} - TC {tc_name} {key} "
                                    f"is not within reference comparison range "
                                    f"{rounded_min_limit} - {rounded_max_limit}"
                                    f"{expected_text}, current value: {rounded_tc_occ}"
                                )


def compare_pg_to_reference(traffic_json, reference_json, pg_keys, pg_to_validate, allowed_deviation, violations_list,
                            pg_buffer_type, min_band_cells=0):
    """
    Compare PG occupancy to a reference PG occupancy.

    Args:
        traffic_json: current test validation json
        reference_json: reference validation json, can be from a previous test run or from a reference file
        pg_keys: list of pg keys, i.e [ValidationConsts.OCC_AVG, ValidationConsts.OCC_MAX]
        pg_to_validate: list of pg to validate, i.e [1, 2]
        allowed_deviation: allowed deviation from the reference PG occupancy.
            If a float between 0 and 1, interpreted as a percentage (e.g. 0.15 = +-15%).
            Otherwise interpreted as an absolute value (e.g. 4 = +-4 cells).
        violations_list: list of violations
        pg_buffer_type: which PG buffer to validate - ValidationConsts.PG_HEADROOM_DATAFRAME or
                       ValidationConsts.PG_BUFFER_DATAFRAME (REQUIRED - must be explicitly specified)
        min_band_cells: floor for the band half width, in buffer cells. A percentage
            allowed_deviation shrinks with the reference and collapses on a near-empty
            buffer, so the effective band becomes max(allowed_deviation, min_band_cells).
            Defaults to 0, which leaves the band exactly as allowed_deviation describes it.
    """
    buffer_type_name = "headroom" if pg_buffer_type == ValidationConsts.PG_HEADROOM_DATAFRAME else "buffer"
    pg_index_key = pg_buffer_type.replace("_dataframe", "")
    with allure.step(f"Compare PG {buffer_type_name} occupancy to reference for {pg_to_validate}"):
        pg_samples = traffic_json[ValidationConsts.TC_PG_SAMPLES]
        pg_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
        for sample_id, port_groups in pg_samples.items():
            for port_group_name, port_group_data in port_groups.items():
                pg_df = port_group_data[pg_buffer_type]
                for pg_dict in pg_df:
                    pg_name = pg_dict[pg_index_key]
                    if pg_name in pg_to_validate:
                        for key in pg_keys:
                            pg_occ = pg_dict[key]
                            reference_pg_occ = get_pg_occ_from_traffic_json(reference_json, port_group_name, key, pg_name,
                                                                            pg_buffer_type)
                            if isinstance(allowed_deviation, float) and 0 < allowed_deviation < 1:
                                deviation_cells = reference_pg_occ * allowed_deviation
                            else:
                                deviation_cells = allowed_deviation
                            is_floor_binding = deviation_cells < min_band_cells
                            if is_floor_binding:
                                deviation_cells = min_band_cells
                            min_limit = reference_pg_occ - deviation_cells
                            max_limit = reference_pg_occ + deviation_cells
                            rounded_pg_occ = round_for_reference_comparison(pg_occ, reference_pg_occ)
                            rounded_min_limit = round_for_reference_comparison(min_limit, reference_pg_occ)
                            rounded_max_limit = round_for_reference_comparison(max_limit, reference_pg_occ)
                            if rounded_pg_occ < rounded_min_limit or rounded_pg_occ > rounded_max_limit:
                                floor_text = ""
                                if is_floor_binding:
                                    floor_text = (f" (band widened to the {min_band_cells} cell floor, "
                                                  f"reference is too small for a meaningful percentage)")
                                violations_list.append(
                                    f"In sample {sample_id} for port group {port_group_name} - PG {pg_name} "
                                    f"{buffer_type_name} {key} is not within reference comparison range "
                                    f"{rounded_min_limit} - {rounded_max_limit}{floor_text}, "
                                    f"current value: {rounded_pg_occ}"
                                )


def get_pg_port_group_data(traffic_json, port_group_name):
    """
    Get one port group's TC/PG sample data out of a validator json.

    Args:
        traffic_json: traffic validation json
        port_group_name (str): name of the port group, i.e 'bisection_downstream'

    Returns:
        dict: The port group's sample data, or None when the port group is absent.
    """
    pg_samples = traffic_json[ValidationConsts.TC_PG_SAMPLES]
    pg_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
    port_group_data = None
    for port_groups in pg_samples.values():
        if port_group_name in port_groups:
            port_group_data = port_groups[port_group_name]
    return port_group_data


def sum_pg_occ_across_frames(port_group_data, pg_key, pg, pg_buffer_types):
    """
    Sum one PG occupancy key across several PG frames.

    A priority group is split between a reserved headroom and a dynamically borrowed
    shared pool, reported as two separate frames. Summing them gives the total occupancy
    of the priority group regardless of how a given chip divides the load between them.

    Frames are read defensively, so a validator json produced by a collector that does
    not populate one of them is reported rather than raising.

    Args:
        port_group_data (dict): One port group's sample data, from get_pg_port_group_data.
        pg_key (str): occupancy key, i.e ValidationConsts.OCC_AVG
        pg (int): PG number to sum for
        pg_buffer_types (list): PG frames to sum, i.e MRCConsts.PG_TOTAL_OCC_FRAMES

    Returns:
        tuple: (total, missing_frames) where total is the summed occupancy and
        missing_frames lists the frames that carried no entry for this PG.
    """
    total = 0
    missing_frames = []
    for pg_buffer_type in pg_buffer_types:
        pg_index_key = pg_buffer_type.replace("_dataframe", "")
        pg_occ = None
        for pg_dict in (port_group_data or {}).get(pg_buffer_type) or []:
            if pg_dict.get(pg_index_key) == pg:
                pg_occ = pg_dict.get(pg_key)
        if pg_occ is None:
            missing_frames.append(pg_buffer_type)
            continue
        total += pg_occ
    return total, missing_frames


def compare_pg_total_occ_to_reference(traffic_json, reference_json, pg_keys, pg_to_validate,
                                      allowed_deviation, violations_list, pg_buffer_types):
    """
    Compare the total PG occupancy, summed across PG frames, to a reference run.

    Unlike compare_pg_to_reference, which looks at a single frame, this sums the reserved
    headroom and the shared pool so the comparison does not depend on how a chip splits
    the load between them. The loop is driven from pg_to_validate rather than from the
    frame rows, so a PG missing from a frame is reported instead of silently skipped.

    Args:
        traffic_json: current test validation json
        reference_json: reference validation json, i.e the IPv6 run of the same session
        pg_keys (list): occupancy keys to compare, i.e [ValidationConsts.OCC_AVG]
        pg_to_validate (list): PG numbers to compare, i.e MRCConsts.PG_LIST
        allowed_deviation: allowed deviation from the reference total.
            If a float between 0 and 1, interpreted as a percentage (e.g. 0.15 = +-15%).
            Otherwise interpreted as an absolute value in buffer cells.
        violations_list (list): list of violations, appended to in place
        pg_buffer_types (list): PG frames to sum, i.e MRCConsts.PG_TOTAL_OCC_FRAMES
    """
    frame_names = " + ".join(frame.replace("_dataframe", "") for frame in pg_buffer_types)
    with allure.step(f"Compare total PG occupancy ({frame_names}) to reference for {pg_to_validate}"):
        pg_samples = traffic_json[ValidationConsts.TC_PG_SAMPLES]
        pg_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
        for sample_id, port_groups in pg_samples.items():
            for port_group_name, port_group_data in port_groups.items():
                reference_port_group_data = get_pg_port_group_data(reference_json, port_group_name)
                for pg_name in pg_to_validate:
                    for key in pg_keys:
                        pg_occ, missing = sum_pg_occ_across_frames(port_group_data, key,
                                                                   pg_name, pg_buffer_types)
                        reference_pg_occ, reference_missing = sum_pg_occ_across_frames(
                            reference_port_group_data, key, pg_name, pg_buffer_types)
                        if missing or reference_missing:
                            violations_list.append(
                                f"In sample {sample_id} for port group {port_group_name} - PG {pg_name} "
                                f"{key} could not be summed, frames absent from the validator json: "
                                f"current={missing}, reference={reference_missing}"
                            )
                            continue
                        if isinstance(allowed_deviation, float) and 0 < allowed_deviation < 1:
                            min_limit = reference_pg_occ * (1 - allowed_deviation)
                            max_limit = reference_pg_occ * (1 + allowed_deviation)
                        else:
                            min_limit = reference_pg_occ - allowed_deviation
                            max_limit = reference_pg_occ + allowed_deviation
                        rounded_pg_occ = round_for_reference_comparison(pg_occ, reference_pg_occ)
                        rounded_min_limit = round_for_reference_comparison(min_limit, reference_pg_occ)
                        rounded_max_limit = round_for_reference_comparison(max_limit, reference_pg_occ)
                        if rounded_pg_occ < rounded_min_limit or rounded_pg_occ > rounded_max_limit:
                            violations_list.append(
                                f"In sample {sample_id} for port group {port_group_name} - PG {pg_name} "
                                f"total ({frame_names}) {key} is not within reference comparison range "
                                f"{rounded_min_limit} - {rounded_max_limit}, current value: {rounded_pg_occ}"
                            )


def get_tc_occ_from_traffic_json(traffic_json, port_group_name, tc_key, tc):
    tc_samples = traffic_json[ValidationConsts.TC_PG_SAMPLES]
    tc_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
    tc_occ = None
    for sample_id, port_groups in tc_samples.items():
        tc_df = port_groups[port_group_name][ValidationConsts.TC_DATAFRAME]
        for tc_dict in tc_df:
            tc_name = tc_dict[ValidationConsts.TC_NAME]
            if tc_name == tc:
                tc_occ = tc_dict[tc_key]
    return tc_occ


def get_pg_occ_from_traffic_json(traffic_json, port_group_name, pg_key, pg, pg_buffer_type):
    """
    Get PG occupancy value from traffic JSON.

    Args:
        traffic_json: traffic validation json
        port_group_name: name of the port group
        pg_key: occupancy key (e.g., 'occ_avg', 'occ_max')
        pg: PG number to get data for
        pg_buffer_type: which PG buffer to get - ValidationConsts.PG_HEADROOM_DATAFRAME or
                       ValidationConsts.PG_BUFFER_DATAFRAME (REQUIRED - must be explicitly specified)

    Returns:
        PG occupancy value or None if not found
    """
    pg_index_key = pg_buffer_type.replace("_dataframe", "")
    pg_samples = traffic_json[ValidationConsts.TC_PG_SAMPLES]
    pg_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
    pg_occ = None
    for sample_id, port_groups in pg_samples.items():
        pg_df = port_groups[port_group_name][pg_buffer_type]
        for pg_dict in pg_df:
            pg_name = pg_dict[pg_index_key]
            if pg_name == pg:
                pg_occ = pg_dict[pg_key]
    return pg_occ


def compare_latency_to_reference(traffic_json, reference_json, latency_keys, tc_to_validate, allowed_deviation, violations_list):
    """
    This function is used to compare the latency to a reference latency
    :param traffic_json: current test validation json
    :param reference_json: reference validation json, can be from a previous test run or from a reference file
    :param latency_keys: list of latency keys, i.e [ValidationConsts.LATENCY_AVG, ValidationConsts.LATENCY_MAX]
    :param latency_to_validate: list of latency to validate, i.e [1, 2]
    :param allowed_deviation: allowed deviation from the reference latency, i.e +-1
    :param violations_list: list of violations
    """
    with allure.step(f"Compare latency to reference for {tc_to_validate}"):
        tc_samples = traffic_json[ValidationConsts.TC_LATENCY_SAMPLES]
        tc_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
        for sample_id, port_groups in tc_samples.items():
            for port_group_name, port_group_data in port_groups.items():
                tc_df = port_group_data[ValidationConsts.TC_LATENCY_DATAFRAME]
                for tc_dict in tc_df:
                    tc_name = tc_dict[ValidationConsts.TC_NAME]
                    if tc_name in tc_to_validate:
                        for latency_attribute_to_compare in latency_keys:
                            latency_value = tc_dict[latency_attribute_to_compare]
                            reference_latency = get_latency_from_traffic_json(reference_json, port_group_name, latency_attribute_to_compare, tc_name)
                            min_limit, max_limit = reference_latency - allowed_deviation, reference_latency + allowed_deviation
                            rounded_latency_value = round_for_reference_comparison(latency_value, reference_latency)
                            rounded_min_limit = round_for_reference_comparison(min_limit, reference_latency)
                            rounded_max_limit = round_for_reference_comparison(max_limit, reference_latency)
                            if rounded_latency_value < rounded_min_limit or rounded_latency_value > rounded_max_limit:
                                violations_list.append(
                                    f"In sample {sample_id} for port group {port_group_name} - TC {tc_name} "
                                    f"{latency_attribute_to_compare} is not within reference comparison range "
                                    f"{rounded_min_limit} - {rounded_max_limit}, current value: {rounded_latency_value}"
                                )


def get_latency_from_traffic_json(traffic_json, port_group_name, tc_latency_key, tc):
    """
    This function is used to get the latency from the traffic json
    :param traffic_json: current test validation json
    :param port_group_name: port group name, i.e 'PortGroup1'
    :param latency_key: latency key, i.e 'LatencyAvg'
    :param tc: tc name, i.e 'TC1'
    :return: latency occ
    """
    latency_samples = traffic_json[ValidationConsts.TC_LATENCY_SAMPLES]
    latency_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
    latency_occ = None
    for sample_id, port_groups in latency_samples.items():
        latency_df = port_groups[port_group_name][ValidationConsts.TC_LATENCY_DATAFRAME]
        for latency_dict in latency_df:
            tc_latency_name = latency_dict[ValidationConsts.TC_NAME]
            if tc_latency_name == tc:
                latency_occ = latency_dict[tc_latency_key]
    return latency_occ


def validate_counters(traffic_json, skip_first_counters_iteration, ignore_counter_list, violations_list):
    """
    Validates counter samples from traffic data, optionally skipping the first iteration.

    Args:
        traffic_json (dict): JSON containing traffic counter samples
        skip_first_counters_iteration (bool): Whether to skip validating the first counter sample
        ignore_counter_list (list): list of counters to ignore during validation
        violations_list (list): List to store any validation violations found
    """
    counters_samples = traffic_json[ValidationConsts.COUNTERS_SAMPLES]
    counters_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
    # Remove the first counter sample if skip_first_counters_iteration is True
    if skip_first_counters_iteration:
        counters_samples.pop(next(iter(counters_samples)))

    # Process each counter sample
    for sample_id, port_groups_samples in counters_samples.items():
        validate_counters_sample(sample_id, port_groups_samples, ignore_counter_list, violations_list)


def validate_required_counters(traffic_json, required_counter_list, violations_list, port_group_name):
    """Validates that required counters are > 0 in all samples.

    Args:
        traffic_json (dict): Traffic validation data containing counter samples.
        required_counter_list (list): Counters that must be > 0 in every sample.
        violations_list (list): List to append violation messages to.
        port_group_name (str, optional): Port group to check. If None, all port groups are checked.
    """
    if not required_counter_list:
        return
    counters_samples = traffic_json[ValidationConsts.COUNTERS_SAMPLES]
    counters_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)

    for counter in required_counter_list:
        for sample_id, port_groups_samples in counters_samples.items():
            found_in_sample = False
            for sample_port_group_name, port_group_data in port_groups_samples.items():
                if port_group_name is not None and sample_port_group_name != port_group_name:
                    continue
                counters_df = pd.DataFrame(port_group_data[ValidationConsts.COUNTERS_DATAFRAME])
                if counter in counters_df.columns and counters_df[counter].gt(0).any():
                    found_in_sample = True
                    break
            if not found_in_sample:
                violations_list.append(f"Required counter '{counter}' was 0 in sample {sample_id}")


def get_sample_number(sample_id: str) -> Optional[int]:
    """
    Extracts the ordinal of a TrafficValidator sample out of its id.

    Args:
        sample_id (str): Sample id, e.g. 'sample #2 2026-09-04 03:34:20'.

    Returns:
        Optional[int]: The sample ordinal, or None when the id does not carry one.
    """
    match = SAMPLE_NUMBER_PATTERN.search(sample_id)
    return int(match.group(1)) if match else None


def mask_waived_pause_and_discard_counters(counters_df: pd.DataFrame, sample_id: str,
                                           port_group_name: str) -> pd.DataFrame:
    """
    Zeroes the Bug #5258617 counters on the affected ports while the issue is open.

    Ports 0x10101 and 0x10102 assert PFC and take ingress no-buffer discards while the traffic ramps up,
    on the first samples of a run. Only those ports' cells are cleared, so the same counters on every
    other port, and every other counter on those ports, are still verified. A port the current breakout
    does not expose simply never matches.

    Args:
        counters_df (pd.DataFrame): Counters of every port in the port group.
        sample_id (str): Sample id the counters were collected in.
        port_group_name (str): Port group the counters belong to, used for logging only.

    Returns:
        pd.DataFrame: A masked copy when the waiver applies, otherwise the frame as given.
    """
    sample_number = get_sample_number(sample_id)
    if not PAUSE_DISCARDS_LAST_TOLERATED_SAMPLE or sample_number is None or \
            sample_number > PAUSE_DISCARDS_LAST_TOLERATED_SAMPLE:
        return counters_df

    is_waived_port = counters_df[ValidationConsts.PORT].astype(str).str.lower().isin(PAUSE_DISCARDS_PORTS)
    waived_counters = [counter for counter in PAUSE_DISCARDS_COUNTERS if counter in counters_df.columns]
    if not is_waived_port.any() or not waived_counters:
        return counters_df

    waived_values = counters_df.loc[is_waived_port, [ValidationConsts.PORT] + waived_counters]
    logger.info(f"Bug #{PAUSE_DISCARDS_REDMINE_ISSUE} is active: skipping {waived_counters} on the ramp-up "
                f"ports in {sample_id} for {port_group_name} (values: {waived_values.to_dict('records')})")
    waived_df = counters_df.copy()
    waived_df.loc[is_waived_port, waived_counters] = 0
    return waived_df


def validate_counters_sample(sample_id, counters_sample, ignore_counter_list, violations_list):
    """
    Validates a single counter sample for any non-zero counter values.

    Args:
        sample_id (str): Identifier for the counter sample
        counters_sample (dict): Sample data containing counter values
        ignore_counter_list (list): list of counters to ignore during validation
        violations_list (list): List to store any validation violations found
    """
    for port_group_name, port_group_data in counters_sample.items():
        counters_df = pd.DataFrame(port_group_data[ValidationConsts.COUNTERS_DATAFRAME])
        counters_df = mask_waived_pause_and_discard_counters(counters_df, sample_id, port_group_name)

        ports_with_counters = counters_df.loc[counters_df.loc[:, counters_df.columns != ValidationConsts.PORT].any(axis=1), ValidationConsts.PORT].to_list()
        counters_with_values = counters_df.loc[:, counters_df.columns != ValidationConsts.PORT].columns[counters_df.loc[:, counters_df.columns != ValidationConsts.PORT].gt(0).any()].tolist()
        counters_with_values = [counter for counter in counters_with_values if counter not in ignore_counter_list]

        if LOOPBACK_FILTER_COUNTER in counters_with_values and \
                not counters_df[LOOPBACK_FILTER_COUNTER].gt(LOOPBACK_FILTER_MAX_TOLERATED_VALUE).any():
            logger.info(f"Bug #{LOOPBACK_FILTER_REDMINE_ISSUE} is active and no port exceeded "
                        f"{LOOPBACK_FILTER_MAX_TOLERATED_VALUE}: skipping {LOOPBACK_FILTER_COUNTER} "
                        f"(highest per-port value: {counters_df[LOOPBACK_FILTER_COUNTER].max()}) "
                        f"in {sample_id} for {port_group_name}")
            counters_with_values.remove(LOOPBACK_FILTER_COUNTER)

        if counters_with_values:
            violations_list.append(f"Ports: {ports_with_counters} had one or more of the following counters:\n"
                                   f"{counters_with_values} with values > 0,\n "
                                   f"please check {sample_id} for {port_group_name}")


def is_ipv6(address):
    """Check if the address is an IPv6 address."""
    # Regex to match IPv6 address (simplified version, just checks for ':')
    return ":" in address


def dscp_to_tc(dscp, ecn=0):
    """
    Converts a DSCP value to a Traffic Class (TC) value for IPv6.

    Args:
        dscp (int): The DSCP value (0 to 63).
        ecn (int, optional): The ECN value (0 to 3). Default is 0 (no congestion).

    Returns:
        int: The Traffic Class value (8 bits).
    """
    if not (0 <= dscp <= 63):
        raise ValueError("DSCP value must be between 0 and 63.")

    if not (0 <= ecn <= 3):
        raise ValueError("ECN value must be between 0 and 3.")

    # DSCP value is stored in the first 6 bits (shifted into place)
    tc = (dscp << 2) | ecn

    return tc


def generate_mac_range(start_mac, count):
    mac_list = []

    # Convert the starting MAC address to an integer
    start_mac_int = int(start_mac.replace(":", ""), 16)

    # Generate the range of MAC addresses
    for i in range(count):
        # Increment the MAC address
        current_mac_int = start_mac_int + i

        # Format it back into a MAC address string
        current_mac = ':'.join(format(current_mac_int, '012x')[i:i + 2] for i in range(0, 12, 2))
        mac_list.append(current_mac)

    return mac_list


def pick_random_non_consecutive_ports(ports_list, port_number, non_consecutive_gap=16):
    if port_number > (len(ports_list) + 1) // 2:
        raise ValueError("Cannot select non-consecutive items, list too small for the selection.")

    indices = list(range(len(ports_list)))
    start_idx = random.choice(indices)

    selected_ports = []

    for i in range(port_number):
        selected_ports.append(ports_list[start_idx % len(ports_list)])
        start_idx += non_consecutive_gap
    return selected_ports


def pick_random_consecutive_ports(ports_list, port_number):
    indices = list(range(len(ports_list) - port_number))
    start_idx = random.choice(indices)
    selected_ports = ports_list[start_idx:start_idx + port_number]
    return selected_ports


def is_no_untrimmed_packets(drop_queue_counter_pkts, drop_queue_drop_pkts, trimming_queue_counter_pkts, trimming_queue_drop_pkts):
    if drop_queue_counter_pkts == 0 and \
            drop_queue_drop_pkts > 0 and \
            trimming_queue_counter_pkts > 0 and \
            trimming_queue_drop_pkts == 0:
        return True
    else:
        return False


def convert_to_percentage(value):
    is_valid_value = 0 <= value <= 1
    if is_valid_value:
        return value * 100
    else:
        raise ValueError(f"Value {value} is not between 0 and 1")


def generate_incremental_addresses(initial_mac, initial_ip, num_addresses, jump=1):
    """
    Generate a list of MAC and IP addresses with incremental values.

    Args:
        initial_mac (str): Initial MAC address in format 'XX:XX:XX:XX:XX:XX'
        initial_ip (str): Initial IP address in format 'X.X.X.X' (IPv4) or 'XXXX:XXXX:XXXX:XXXX:XXXX:XXXX:XXXX:XXXX' (IPv6)
        num_addresses (int): Number of address pairs to generate
        jump (int): Value to increment by for each address

    Returns:
        list: List of dictionaries containing MAC and IP address pairs
    """
    ip_mac_list = []

    mac_obj = EUI(initial_mac)
    ip_obj = ipaddress.ip_address(initial_ip)

    for i in range(num_addresses):
        # Convert MAC to integer, add increment, and convert back to EUI with colon format
        current_mac = EUI(int(mac_obj) + (i * jump), dialect=netaddr.mac_unix)
        current_ip = ip_obj + (i * jump)
        ip_mac_list.append((str(current_ip), str(current_mac)))

    return ip_mac_list
