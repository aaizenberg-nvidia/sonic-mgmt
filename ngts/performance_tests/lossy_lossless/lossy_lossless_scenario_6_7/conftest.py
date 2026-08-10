import logging
import math
import os
from dataclasses import asdict
from collections import defaultdict
from itertools import islice, cycle
import allure
import pytest

from ngts.constants.constants import BugHandlerConst
from ngts.constants.performance_constants import (
    PerfConsts,
    LossyLosslessTrafficHandler,
    ValidationConsts,
    SPCXRAConsts
)
from ngts.helpers.performance.performance_setup_helpers import (
    apply_test_configuration,
    restore_basic_configuration,
    save_base_configuration
)
from ngts.helpers.performance.traffic_helpers import (
    create_json_traffic_file_with_stream_list,
    create_json_traffic_stream,
    dscp_to_tc
)
from ngts.performance_tests.conftest import get_all_players_ports
from ngts.tools.infra import get_chip_type

logger = logging.getLogger()


TESTS_SCENARIO = "lossy_lossless"


def get_lossy_lossless_scenario_6_7_traffic(
    cli_objects,
    conf_args,
    scenario_name,
    template_suite="traffic_packets_json_files"
):
    """
    Generate traffic configuration for many-to-1 lossy/lossless scenario.

    Args:
        cli_objects: Dictionary of CLI objects for traffic generators
        conf_args: Dictionary containing configuration parameters
        scenario_name: Name of the scenario (e.g., 'scenario_6a')
        template_suite: Directory containing traffic template files

    Returns:
        Dictionary mapping player aliases to their traffic configuration file paths
    """
    traffic_jsons = {}

    for tg_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
        stream_list = []
        traffic_patterns = conf_args[PerfConsts.TRAFFIC_PATTERN][tg_alias]

        traffic_script_path = os.path.join(
            BugHandlerConst.NGTS_PATH,
            "performance_tests",
            template_suite,
            TESTS_SCENARIO,
            f"{tg_alias}_lossy_lossless_{scenario_name}.json"
        )

        tg_conf_json = cli_objects[tg_alias].performance.get_device_configuration(conf_args)
        dmac = tg_conf_json["left_mac"] if tg_alias == PerfConsts.LEFT_TG_ALIAS else tg_conf_json["right_mac"]

        for traffic_pattern in traffic_patterns:
            ip, ports, num_lossy_packets, num_lossless_packets = traffic_pattern
            traffic_parameters = {
                "ports": ports,
                "MAC": {"src": tg_conf_json["smac"], "dst": dmac},
                "IP": {"src": tg_conf_json["source_ip"], "dst": ip},
                "UDP": {"src": PerfConsts.UDP_SOURCE_PORT, "dst": PerfConsts.ROCE_PORT},
                "AR": PerfConsts.ADAPTIVE_ROUTING_ENABLED,
                "packet_size": conf_args["packet_size"],
                "is_ipv6": conf_args["is_ipv6"],
                "lossy_dscp_value": 34,
                "lossless_dscp_value": 26
            }

            traffic_dict = {"lossy": num_lossy_packets, "lossless": num_lossless_packets}
            for traffic_type, num_packets in traffic_dict.items():
                if num_packets > 0:
                    traffic_parameters["num_packets"] = num_packets
                    stream = create_json_traffic_stream(
                        tg_alias,
                        traffic_parameters,
                        f"{tg_alias}_{traffic_type}_stream",
                        dscp_to_tc(traffic_parameters[f"{traffic_type}_dscp_value"], 2)
                    )
                    stream_list.append(stream)

        create_json_traffic_file_with_stream_list(
            tg_alias,
            traffic_parameters,
            traffic_script_path,
            stream_list=stream_list
        )
        traffic_jsons[tg_alias] = traffic_script_path

    return traffic_jsons


def get_conf_args(scenario_name, players, all_ports_after_split):
    """
    This function alters all of the jinja template files.
    """
    chip_type = get_chip_type(players['dut']['attributes'].noga_query_data['attributes'])

    conf_args = {
        "congestion_thresh_lo": PerfConsts.LOW_AR_THRESHOLD_SPC6 if chip_type == "SPC6" else PerfConsts.LOW_AR_THRESHOLD,
        "fboss_enabled": True,
        "auto_buffer_mode": "False",
        "packet_size": PerfConsts.PACKET_SIZE_LIST[0],
        "is_ipv6": False,
        "split_right": 2,
        "split_left": 2,
        "two_sided_ar": True,
        "scenario": TESTS_SCENARIO,
        "sdk_test_name": "LossyLosslesssScenario6And7DynamicConfig"
    }
    func_name = f"get_{scenario_name}_conf_args"
    globals()[func_name](players, all_ports_after_split, conf_args)
    return conf_args


@pytest.fixture(scope='session', autouse=True)
def all_ports_after_split(skip_test_conditionally, players, split_right=2, split_left=2):
    return get_all_players_ports(players, split_right, split_left)


def apply_basic_setup_configuration(scenario_name, players, all_ports_after_split):
    conf_args = get_conf_args(scenario_name, players, all_ports_after_split)
    with allure.step('Save Players initial Configuration'):
        save_base_configuration(players)
    with allure.step("Apply Test configuration on all Players"):
        apply_test_configuration(players, scenario=TESTS_SCENARIO, conf_args=conf_args)
    return conf_args


@pytest.fixture(scope='class')
def basic_setup_configuration(request, players, all_ports_after_split):
    scenario_name = request.param
    try:
        conf_args = apply_basic_setup_configuration(scenario_name, players, all_ports_after_split)
        yield scenario_name, conf_args
    except Exception as e:
        raise e
    finally:
        with allure.step('Restore Base Configuration on all Players'):
            restore_basic_configuration(players)


def get_scenario_6a_conf_args(players, all_ports_after_split, conf_args):
    """
    Scenario 6A (bidirectional mixed traffic)

    Configuration: 64 ports per side (TG LEFT and TG RIGHT), 128 ports total
    Traffic Direction: Bidirectional (left ↔ right)

    Traffic Pattern:

    Lossless Traffic (Many-to-1 pattern):
        - TG LEFT: 32 ports (32-63) → many to one ingress ports sends lossless traffic to egress port 127 on TG RIGHT
        - TG RIGHT: 32 ports (96-127) → many to one ingress ports sends lossless traffic to egress port 63 on TG LEFT

    Lossy Traffic (Bisection pattern):
        - TG LEFT: 32 ports (0-31) → bisection right group (64-95)
        - TG RIGHT: 32 ports (64-95) → bisection left group (0-31)

    setup configuration:

    ┌─────────────────────┐          ┌──────────────────────────────┐          ┌─────────────────────┐
    │      TG LEFT        │          │             DUT              │          │      TG RIGHT       │
    └─────────────────────┘          └──────────────────────────────┘          └─────────────────────┘

         0-31       ──────────────►  Bisection right group (64-95)

         32-63      ──────────────►  1 egress port for right TG (127)

                                     Bisection left group (0-31)     ◄────────────── Bisection right group (64-95)

                                     1 egress port for left TG [63]  ◄──────────────  many to one ingress ports (64-127)

    """
    neigh_mac_left_tg = '00:00:00:00:10:90'
    neigh_mac_right_tg = '00:00:00:00:10:70'
    num_of_traffic_ports = 32
    speed = 400
    lossless_rx_expected_bw = round((speed * PerfConsts.DVS_SHAPER_VALUE / num_of_traffic_ports) / speed, 2) - 0.002
    left_unconnected = all_ports_after_split[PerfConsts.LEFT_TG_ALIAS]["unconnected_ports"]
    right_unconnected = all_ports_after_split[PerfConsts.RIGHT_TG_ALIAS]["unconnected_ports"]
    left_split = all_ports_after_split[PerfConsts.DUT_ALIAS]["left_split_ports"]
    right_split = all_ports_after_split[PerfConsts.DUT_ALIAS]["right_split_ports"]
    chip_type = get_chip_type(players['dut']['attributes'].noga_query_data['attributes'])
    bw_th = SPCXRAConsts.get_min_line_rate_bw_threshold_ibm(chip_type)

    conf_args.update({
        PerfConsts.PORT_GROUPS: {
            PerfConsts.LEFT_TG_ALIAS: {
                "bisection_left_group": left_unconnected[:num_of_traffic_ports],
                "ingress_ports_many_to_1_group": left_unconnected[num_of_traffic_ports:2 * num_of_traffic_ports],
            },
            PerfConsts.RIGHT_TG_ALIAS: {
                "bisection_right_group": right_unconnected[:num_of_traffic_ports],
                "ingress_ports_many_to_1_group": right_unconnected[num_of_traffic_ports:2 * num_of_traffic_ports],
            },
            PerfConsts.DUT_ALIAS: {
                "bisection_left_group": left_split[:num_of_traffic_ports],
                "bisection_right_group": right_split[:num_of_traffic_ports],
                "egress_ports_many_to_1_group_left": left_split[-1:],
                "egress_ports_many_to_1_group_right": right_split[-1:],
                "ingress_ports_many_to_1_group_left": left_split[num_of_traffic_ports:2 * num_of_traffic_ports],
                "ingress_ports_many_to_1_group_right": right_split[num_of_traffic_ports:2 * num_of_traffic_ports],
            },
        },
        PerfConsts.BW_THRESHOLD: {
            "bisection_left_group": {
                ValidationConsts.TX: bw_th,
                ValidationConsts.RX: PerfConsts.DVS_SHAPER_VALUE - 0.002
            },
            "bisection_right_group": {
                ValidationConsts.TX: bw_th,
                ValidationConsts.RX: PerfConsts.DVS_SHAPER_VALUE - 0.002
            },
            "egress_ports_many_to_1_group_left": {
                ValidationConsts.TX: bw_th,
                ValidationConsts.RX: lossless_rx_expected_bw
            },
            "egress_ports_many_to_1_group_right": {
                ValidationConsts.TX: bw_th,
                ValidationConsts.RX: lossless_rx_expected_bw
            },
            "ingress_ports_many_to_1_group_left": {
                ValidationConsts.TX: None,
                ValidationConsts.RX: lossless_rx_expected_bw
            },
            "ingress_ports_many_to_1_group_right": {
                ValidationConsts.TX: None,
                ValidationConsts.RX: lossless_rx_expected_bw
            }
        }
    })
    port_groups = conf_args[PerfConsts.PORT_GROUPS][PerfConsts.DUT_ALIAS]

    conf_args[PerfConsts.ECMP_CONFIGURATIONS] = [
        LossyLosslessTrafficHandler(
            neigh_ip='192.168.1.0',
            neigh_mask='255.255.255.255',
            neigh_mac=neigh_mac_left_tg,
            nexthop_ip='10.0.1.0',
            ports_list=port_groups["bisection_left_group"],
            ecmp_type=PerfConsts.ECMP_TYPE_AR
        ),
        LossyLosslessTrafficHandler(
            neigh_ip='10.0.84.0',
            neigh_mask='255.255.255.255',
            neigh_mac=neigh_mac_left_tg,
            nexthop_ip='6.6.6.6',
            ports_list=port_groups["egress_ports_many_to_1_group_left"],
            ecmp_type=PerfConsts.ECMP_TYPE_AR
        ),
        LossyLosslessTrafficHandler(
            neigh_ip='10.0.1.0',
            neigh_mask='255.255.255.255',
            neigh_mac=neigh_mac_right_tg,
            nexthop_ip='8.8.8.8',
            ports_list=port_groups["bisection_right_group"],
            ecmp_type=PerfConsts.ECMP_TYPE_AR
        ),
        LossyLosslessTrafficHandler(
            neigh_ip='10.0.83.0',
            neigh_mask='255.255.255.255',
            neigh_mac=neigh_mac_right_tg,
            nexthop_ip='5.5.5.5',
            ports_list=port_groups["egress_ports_many_to_1_group_right"],
            ecmp_type=PerfConsts.ECMP_TYPE_AR
        ),
    ]

    left_ports = conf_args[PerfConsts.PORT_GROUPS][PerfConsts.LEFT_TG_ALIAS]
    right_ports = conf_args[PerfConsts.PORT_GROUPS][PerfConsts.RIGHT_TG_ALIAS]

    bisection_lossy_packets = 8 if chip_type != "SPC6" else int(8 * 1.5)
    bisection_lossless_packets = 0
    many_to_one_lossy_packets = 0
    many_to_one_lossless_packets = 8 if chip_type != "SPC6" else int(8 * 1.5)

    conf_args[PerfConsts.TRAFFIC_PATTERN] = {
        PerfConsts.LEFT_TG_ALIAS: [
            ('10.0.1.0', left_ports["bisection_left_group"], bisection_lossy_packets, bisection_lossless_packets),
            ('10.0.83.0', left_ports["ingress_ports_many_to_1_group"], many_to_one_lossy_packets, many_to_one_lossless_packets),
        ],
        PerfConsts.RIGHT_TG_ALIAS: [
            ('192.168.1.0', right_ports["bisection_right_group"], bisection_lossy_packets, bisection_lossless_packets),
            ('10.0.84.0', right_ports["ingress_ports_many_to_1_group"], many_to_one_lossy_packets, many_to_one_lossless_packets),
        ],
    }

    conf_args[PerfConsts.ECMP_CONFIGURATIONS] = [asdict(handler) for handler in conf_args[PerfConsts.ECMP_CONFIGURATIONS]]
    conf_args[ValidationConsts.IGNORE_COUNTER_LIST] = ['a_mac_control_frames_transmitted', 'a_pause_mac_ctrl_frames_transmitted']
    return conf_args


def get_scenario_6b_conf_args(players, all_ports_after_split, conf_args):
    """
    Scenario 6B (unidirectional mixed traffic)

    Configuration: 32 ingress ports (TG LEFT) → 32 egress ports (TG RIGHT)
    Traffic Direction: Unidirectional (left → right only)

    Traffic Pattern:

    Lossy Traffic (16 ports):
        - Source: Ports 0-15 (TG LEFT)
        - Destination: Ports 64-95 (TG RIGHT), 32 egress ports
        - Routing: Adaptive Routing (AR) enabled

    Lossless Traffic (16 ports):
        - Source: Ports 16-31 (TG LEFT)
        - Destination: Ports 64-79 (TG RIGHT), 16 egress ports (subset of lossy destinations)
        - Routing: Non-AR (static routing)

    setup configuration:

    ┌─────────────────────┐          ┌──────────────────────────────┐          ┌─────────────────────┐
    │      TG LEFT        │          │             DUT              │          │      TG RIGHT       │
    └─────────────────────┘          └──────────────────────────────┘          └─────────────────────┘

         0-15      ──────────────►  32 egress ports for right TG (64-95) 200G

         16-31     ──────────────►  16 egress ports for right TG, 200G + 400G = 600G (1.5x of the bandwidth)
                                    subset of the 32 lossy ports (64-79)

        since, Adaptive routing is enabled, the traffic will be distributed between the 16 egress ports
        which are less congested, resulting in an even distribution of the traffic.
    """
    num_of_traffic_ports = 16
    neigh_mac_right_tg = '00:00:00:00:10:70'
    left_unconnected = all_ports_after_split[PerfConsts.LEFT_TG_ALIAS]["unconnected_ports"]
    left_split = all_ports_after_split[PerfConsts.DUT_ALIAS]["left_split_ports"]
    right_split = all_ports_after_split[PerfConsts.DUT_ALIAS]["right_split_ports"]
    chip_type = get_chip_type(players['dut']['attributes'].noga_query_data['attributes'])
    bw_th = SPCXRAConsts.get_min_line_rate_bw_threshold_ibm(chip_type)

    conf_args.update({
        PerfConsts.PORT_GROUPS: {
            PerfConsts.LEFT_TG_ALIAS: {
                "ingress_ports_few_to_many_group": left_unconnected[:num_of_traffic_ports],
                "bisection_ingress_left_group": left_unconnected[num_of_traffic_ports:2 * num_of_traffic_ports],
            },
            PerfConsts.RIGHT_TG_ALIAS: {},
            PerfConsts.DUT_ALIAS: {
                "ingress_ports_few_to_many_group_left": left_split[:num_of_traffic_ports],
                "egress_ports_few_to_many_group_right": right_split[:2 * num_of_traffic_ports],
                "bisection_ingress_group_left": left_split[num_of_traffic_ports:2 * num_of_traffic_ports],
                "bisection_egress_group_right": right_split[:num_of_traffic_ports],
            },
        },
        PerfConsts.BW_THRESHOLD: {
            "egress_ports_few_to_many_group_right": {
                ValidationConsts.TX: bw_th,
                ValidationConsts.RX: None
            },
            "bisection_egress_group_right": {
                ValidationConsts.TX: bw_th,
                ValidationConsts.RX: None
            },
            "ingress_ports_few_to_many_group_left": {
                ValidationConsts.TX: None,
                ValidationConsts.RX: PerfConsts.DVS_SHAPER_VALUE
            },
            "bisection_ingress_group_left": {
                ValidationConsts.TX: None,
                ValidationConsts.RX: PerfConsts.DVS_SHAPER_VALUE
            }
        }
    })

    left_ports = conf_args[PerfConsts.PORT_GROUPS][PerfConsts.LEFT_TG_ALIAS]
    bisection_lossy_packets = 0
    bisection_lossless_packets = 8 if chip_type != "SPC6" else int(8 * 1.5)
    few_to_many_lossy_packets = 8 if chip_type != "SPC6" else int(8 * 1.5)
    few_to_many_lossless_packets = 0
    conf_args[PerfConsts.TRAFFIC_PATTERN] = {
        PerfConsts.LEFT_TG_ALIAS: [
            ('10.0.1.0', left_ports["ingress_ports_few_to_many_group"], few_to_many_lossy_packets, few_to_many_lossless_packets),
            ('10.0.83.0', left_ports["bisection_ingress_left_group"], bisection_lossy_packets, bisection_lossless_packets),
        ],
        PerfConsts.RIGHT_TG_ALIAS: []
    }

    port_groups = conf_args[PerfConsts.PORT_GROUPS][PerfConsts.DUT_ALIAS]

    conf_args[PerfConsts.ECMP_CONFIGURATIONS] = [
        LossyLosslessTrafficHandler(
            neigh_ip='10.0.1.0',
            neigh_mask='255.255.255.255',
            neigh_mac=neigh_mac_right_tg,
            nexthop_ip='8.8.8.8',
            ports_list=port_groups["egress_ports_few_to_many_group_right"],
            ecmp_type=PerfConsts.ECMP_TYPE_AR
        ),
        LossyLosslessTrafficHandler(
            neigh_ip='10.0.83.0',
            neigh_mask='255.255.255.255',
            neigh_mac=neigh_mac_right_tg,
            nexthop_ip='5.5.5.5',
            ports_list=port_groups["bisection_egress_group_right"],
            ecmp_type=PerfConsts.ECMP_TYPE_STATIC
        ),
    ]

    conf_args[PerfConsts.ECMP_CONFIGURATIONS] = [
        asdict(handler) for handler in conf_args[PerfConsts.ECMP_CONFIGURATIONS]
    ]

    return conf_args


def get_scenario_7a_conf_args(players, all_ports_after_split, conf_args):
    """
    Scenario 7a (leaf configuration)

    Configuration: 64 spine-facing ports + 64 NIC-facing ports (128 ports total)
    Bandwidth Distribution: 75% lossless, 25% lossy

    Traffic Pattern:

    Left Ports (Spine to NIC, ports 0-63):
        - Each spine port sends static traffic to all 64 NIC ports
        - Per spine port: Lossy traffic to 16 NIC ports (25%), Lossless traffic to 48 NIC ports (75%)

    Right Ports (NIC to Spine, ports 64-127):
        - Bisection traffic pattern with 64 ports using Adaptive Routing (AR)

    setup configuration:

    ┌──────────────────────┐         ┌──────────────────────────────┐         ┌──────────────────────┐
    │   TG LEFT (spine)    │         │             DUT              │         │   TG RIGHT (NIC)     │
    └──────────────────────┘         └──────────────────────────────┘         └──────────────────────┘

         0-64      ──────────────►  sends lossy traffic to 16 ports,
                                    sends lossless traffic to the other 48 ports (64-127) statically,
                                    bisection 64 AR (0-64)                 ◄──────────────  64-127 (64-127)

        No congestion is expected in this scenario, so no discards are expected.
    """
    num_of_traffic_ports = 64
    neigh_mac_left_tg = '00:00:00:00:10:90'
    left_unconnected = all_ports_after_split[PerfConsts.LEFT_TG_ALIAS]["unconnected_ports"]
    right_unconnected = all_ports_after_split[PerfConsts.RIGHT_TG_ALIAS]["unconnected_ports"]
    left_split = all_ports_after_split[PerfConsts.DUT_ALIAS]["left_split_ports"]
    right_split = all_ports_after_split[PerfConsts.DUT_ALIAS]["right_split_ports"]
    chip_type = get_chip_type(players['dut']['attributes'].noga_query_data['attributes'])
    bw_th = SPCXRAConsts.get_min_line_rate_bw_threshold_ibm(chip_type)

    conf_args.update({
        "two_sided_ar": False,
        "spine_tg": PerfConsts.LEFT_TG_ALIAS,
        "nic_tg": PerfConsts.RIGHT_TG_ALIAS,
        PerfConsts.PORT_GROUPS: {
            PerfConsts.LEFT_TG_ALIAS: {
                "ports_connected_to_spine": left_unconnected[:num_of_traffic_ports],
            },
            PerfConsts.RIGHT_TG_ALIAS: {
                "ports_connected_to_nic": right_unconnected[:num_of_traffic_ports],
            },
            PerfConsts.DUT_ALIAS: {
                "ports_connected_to_spine": left_split[:num_of_traffic_ports],
                "ports_connected_to_nic": right_split[:num_of_traffic_ports],
            }
        },
        PerfConsts.BW_THRESHOLD: {
            "ports_connected_to_spine": {
                ValidationConsts.TX: bw_th,
                ValidationConsts.RX: PerfConsts.DVS_SHAPER_VALUE
            },
            "ports_connected_to_nic": {
                ValidationConsts.TX: bw_th,
                ValidationConsts.RX: PerfConsts.DVS_SHAPER_VALUE
            },
        }
    })

    port_groups = conf_args[PerfConsts.PORT_GROUPS][PerfConsts.DUT_ALIAS]

    conf_args[PerfConsts.ECMP_CONFIGURATIONS] = [
        LossyLosslessTrafficHandler(
            neigh_ip='192.168.1.0',
            neigh_mask='255.255.255.255',
            neigh_mac=neigh_mac_left_tg,
            nexthop_ip='10.0.1.0',
            ports_list=port_groups["ports_connected_to_spine"],
            ecmp_type=PerfConsts.ECMP_TYPE_AR
        )
    ]

    right_ports = conf_args[PerfConsts.PORT_GROUPS][PerfConsts.RIGHT_TG_ALIAS]
    nic_to_spine_lossy_packets = 8 if chip_type != "SPC6" else int(8 * 1.5)
    nic_to_spine_lossless_packets = 0
    conf_args[PerfConsts.TRAFFIC_PATTERN] = {
        PerfConsts.RIGHT_TG_ALIAS: [
            ('192.168.1.0', right_ports["ports_connected_to_nic"], nic_to_spine_lossy_packets, nic_to_spine_lossless_packets),
        ],
        PerfConsts.LEFT_TG_ALIAS: []
    }

    conf_args[PerfConsts.ECMP_CONFIGURATIONS] = [
        asdict(handler) for handler in conf_args[PerfConsts.ECMP_CONFIGURATIONS]
    ]

    update_spine_to_leaf_scenario_7a_conf_args(players, conf_args["spine_tg"], conf_args)
    return conf_args


def get_scenario_7b_conf_args(players, all_ports_after_split, conf_args):
    """
    Represents scenario 7b in lossy-lossless tests:
    Left side:
        2 port groups of 32 ports, all send and receives lossless traffic (Groups A & B)
    Right side:
        1 port group of 32 ports that sends and receives lossless traffic (Group C)
        1 port group of 32 ports that sends and receives lossy traffic (Group D)

    Unlike all other scenarios, this scenario also sends traffic to group member of the same side.
    Meaning, group A sends and receives to groups B, C and D.
    Same goes to all other ports groups (A <-> BCD, B <-> ACD, C <-> ABD, D <->ABC)

    setup configuration:

    ┌──────────────────────┐         ┌──────────────────────────────┐         ┌──────────────────────┐
    │   TG LEFT (spine)    │         │             DUT              │         │   TG RIGHT (NIC)     │
    └──────────────────────┘         └──────────────────────────────┘         └──────────────────────┘

      0-31 (Group A)     ──────────►  sends lossless traffic to groups B, C
                                      sends lossy traffic to group D

      32-63 (Group B)    ──────────►  sends lossless traffic to groups A, C
                                      sends lossy traffic to group D

                                      sends lossless traffic to groups A, B        ◄──────────  64-95 (Group C)
                                      sends lossy traffic to group D

                                      sends lossy traffic to groups A, B and C     ◄──────────  96-127 (Group D)

    This test scenario simulates the traffic in the long cable scenario as it will be used in an FBOSS data center.
    The lossless traffic is sent to the groups A, B and C simulate data sent within the data center internally.
    The lossy traffic is sent to group D simulate data sent to the outside of the data center.

    same goes to scenario 7c.
    """
    num_of_traffic_ports = 32
    num_of_groups = 4
    return get_scenrio_7_b_or_c_conf_args(players, all_ports_after_split, num_of_traffic_ports, num_of_groups, conf_args)


def get_scenario_7c_conf_args(players, all_ports_after_split, conf_args):
    """
    Scenario 7C (spine configuration)

    Configuration: 8 port groups with 16 ports each (128 ports total)

    Traffic Pattern:

    Groups A-G (7 groups, 112 ports total):
        -  Send lossless traffic to all other groups A-G (87.5% bandwidth)
        -  Send lossy traffic to group H (12.5% bandwidth)

    Group H (1 group, 16 ports):
        -  Send lossy traffic to all groups A-G (100% bandwidth)

    setup configuration:

    ┌──────────────────────┐         ┌──────────────────────────────┐         ┌──────────────────────┐
    │   TG LEFT (spine)    │         │             DUT              │         │   TG RIGHT (NIC)     │
    └──────────────────────┘         └──────────────────────────────┘         └──────────────────────┘

    0-16 (Group A)      ──────────►  sends lossless traffic to groups B-G,
                                     sends lossy traffic to group H

    16-32 (Group B)     ──────────►  sends lossless traffic to groups A, C-G,
                                     sends lossy traffic to group H

    32-48 (Group C)     ──────────►  sends lossless traffic to groups A-B, D-G,
                                     sends lossy traffic to group H

    48-64 (Group D)     ──────────►  sends lossless traffic to groups A-C, E-G,
                                     sends lossy traffic to group H

                                     sends lossless traffic to groups A-D, F-G,   ◄──────────  48-64 (Group E)
                                     sends lossy traffic to group H

                                     sends lossless traffic to groups A-E, G      ◄──────────  64-80 (Group F)
                                     sends lossy traffic to group H

                                     sends lossless traffic to groups A-F         ◄──────────  80-96 (Group G)
                                     sends lossy traffic to group H

                                     sends lossy traffic to groups A-G            ◄──────────  96-112 (Group H)
    """
    num_of_traffic_ports = 16
    num_of_groups = 8
    return get_scenrio_7_b_or_c_conf_args(players, all_ports_after_split, num_of_traffic_ports, num_of_groups, conf_args)


def get_groups_letters(num_of_groups):
    groups_letters = [chr(i) for i in range(ord('a'), ord('a') + num_of_groups)]
    return groups_letters


def get_group_ips(groups_letters):
    group_ips = {
        f'group_{letter}': f'10.0.8{i}.0'
        for i, letter in enumerate(groups_letters)
    }
    return group_ips


def get_left_and_right_groups(group_ips, num_of_groups):
    left_groups = list(group_ips.keys())[:num_of_groups // 2]
    right_groups = list(group_ips.keys())[num_of_groups // 2:]
    all_groups = left_groups + right_groups
    return left_groups, right_groups, all_groups


def get_port_groups(left_groups, right_groups, all_groups, num_of_traffic_ports, left_unconnected, right_unconnected, left_split, right_split):
    port_groups_dict = {
        PerfConsts.LEFT_TG_ALIAS: {
            group_name: left_unconnected[
                idx * num_of_traffic_ports:(idx + 1) * num_of_traffic_ports
            ]
            for idx, group_name in enumerate(left_groups)
        },
        PerfConsts.RIGHT_TG_ALIAS: {
            group_name: right_unconnected[
                idx * num_of_traffic_ports:(idx + 1) * num_of_traffic_ports
            ]
            for idx, group_name in enumerate(right_groups)
        },
        PerfConsts.DUT_ALIAS: {
            **{
                group_name: left_split[
                    idx * num_of_traffic_ports:(idx + 1) * num_of_traffic_ports
                ]
                for idx, group_name in enumerate(left_groups)
            },
            **{
                group_name: right_split[
                    idx * num_of_traffic_ports:(idx + 1) * num_of_traffic_ports
                ]
                for idx, group_name in enumerate(right_groups)
            },
        }
    }

    return port_groups_dict


def get_groups_bw_threshold(all_groups, players):
    chip_type = get_chip_type(players['dut']['attributes'].noga_query_data['attributes'])
    bw_th = SPCXRAConsts.get_min_line_rate_bw_threshold_ibm(chip_type)
    groups_bw_threshold = {
        group_name: {
            ValidationConsts.TX: bw_th,
            ValidationConsts.RX: PerfConsts.DVS_SHAPER_VALUE
        }
        for group_name in all_groups
    }
    return groups_bw_threshold


def get_groups_ecmp_configurations(all_groups, group_ips, left_groups, neigh_mac_left_tg, neigh_mac_right_tg, port_groups_dut):
    ecmp_configurations_dict = {PerfConsts.ECMP_CONFIGURATIONS: []}
    for idx, group in enumerate(all_groups):
        neigh_mac = neigh_mac_left_tg if group in left_groups else neigh_mac_right_tg
        ecmp_configurations_dict[PerfConsts.ECMP_CONFIGURATIONS].append(
            LossyLosslessTrafficHandler(
                neigh_ip=group_ips[group],
                neigh_mask='255.255.255.255',
                neigh_mac=neigh_mac,
                nexthop_ip='.'.join([str(idx + 1)] * 4),
                ports_list=port_groups_dut[group],
                ecmp_type=PerfConsts.ECMP_TYPE_AR
            )
        )
    ecmp_configurations_dict[PerfConsts.ECMP_CONFIGURATIONS] = [
        asdict(handler) for handler in ecmp_configurations_dict[PerfConsts.ECMP_CONFIGURATIONS]
    ]
    return ecmp_configurations_dict


def get_traffic_pattern(all_groups, group_ips, left_groups, num_packets, port_groups_tg):
    traffic_pattern = defaultdict(list)
    for src_group in all_groups:
        tg_alias = PerfConsts.LEFT_TG_ALIAS if src_group in left_groups else PerfConsts.RIGHT_TG_ALIAS

        for dst_group in all_groups:
            if src_group != dst_group:
                dst_ip = group_ips[dst_group]
                is_lossy_traffic = (
                    dst_group == all_groups[-1] or src_group == all_groups[-1]
                )

                if is_lossy_traffic:
                    lossy_packets = num_packets
                    lossless_packets = 0
                    traffic_pattern[tg_alias].append(
                        (dst_ip, port_groups_tg[tg_alias][src_group], lossy_packets, lossless_packets)
                    )
                else:
                    lossy_packets = 0
                    lossless_packets = num_packets
                    traffic_pattern[tg_alias].append(
                        (dst_ip, port_groups_tg[tg_alias][src_group], lossy_packets, lossless_packets)
                    )
    return traffic_pattern


def get_scenrio_7_b_or_c_conf_args(
    players,
    all_ports_after_split,
    num_of_traffic_ports,
    num_of_groups,
    conf_args
):

    groups_letters = get_groups_letters(num_of_groups)
    group_ips = get_group_ips(groups_letters)
    left_groups, right_groups, all_groups = get_left_and_right_groups(group_ips, num_of_groups)

    neigh_mac_left_tg = '00:00:00:00:10:90'
    neigh_mac_right_tg = '00:00:00:00:10:70'

    left_unconnected = all_ports_after_split[PerfConsts.LEFT_TG_ALIAS]["unconnected_ports"]
    right_unconnected = all_ports_after_split[PerfConsts.RIGHT_TG_ALIAS]["unconnected_ports"]
    left_split = all_ports_after_split[PerfConsts.DUT_ALIAS]["left_split_ports"]
    right_split = all_ports_after_split[PerfConsts.DUT_ALIAS]["right_split_ports"]

    port_groups = get_port_groups(left_groups, right_groups, all_groups, num_of_traffic_ports, left_unconnected, right_unconnected, left_split, right_split)
    conf_args.update({PerfConsts.PORT_GROUPS: port_groups})
    groups_bw_threshold = get_groups_bw_threshold(all_groups, players)
    conf_args.update({PerfConsts.BW_THRESHOLD: groups_bw_threshold})
    port_groups_dut = conf_args[PerfConsts.PORT_GROUPS][PerfConsts.DUT_ALIAS]
    ecmp_configurations_dict = get_groups_ecmp_configurations(all_groups, group_ips, left_groups, neigh_mac_left_tg, neigh_mac_right_tg, port_groups_dut)
    conf_args.update(ecmp_configurations_dict)
    num_packets = math.ceil(8 / (len(all_groups) - 1))
    port_groups_tg = conf_args[PerfConsts.PORT_GROUPS]
    traffic_pattern = get_traffic_pattern(all_groups, group_ips, left_groups, num_packets, port_groups_tg)
    conf_args.update({PerfConsts.TRAFFIC_PATTERN: traffic_pattern})
    return conf_args


def update_spine_to_leaf_scenario_7a_conf_args(players, spine_tg_alias, conf_args):
    """
    Update traffic configuration for scenario 7a spine to leaf traffic pattern.

    Args:
        players: Dictionary of players
        spine_tg_alias: Alias of the spine traffic generator
        conf_args: Dictionary containing configuration parameters
    """
    template_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                 PerfConsts.DEFAULT_PERF_TEMPLATES_DIR,
                                 TESTS_SCENARIO,
                                 "dvs")
    dut_configuration = players['dut']['cli'].performance.render_configuration_file(conf_args, template_path)

    spine_tg_unconnected_ports = conf_args[PerfConsts.PORT_GROUPS][spine_tg_alias]["ports_connected_to_spine"]
    leaf_dst_ips = list(dut_configuration[PerfConsts.SDK_TEST_CONF]["right_side_ports_to_ip_dict"].values())

    for index, unconnected_spine_port in enumerate(spine_tg_unconnected_ports):
        ips_that_will_get_lossy_traffic = list(islice(cycle(leaf_dst_ips), index, index + 16))
        ips_that_will_get_lossless_traffic = list(set(leaf_dst_ips).difference(set(ips_that_will_get_lossy_traffic)))

        for ip in ips_that_will_get_lossy_traffic:
            lossy_packets = 1
            lossless_packets = 0
            conf_args[PerfConsts.TRAFFIC_PATTERN][PerfConsts.LEFT_TG_ALIAS].append(
                (ip, [unconnected_spine_port], lossy_packets, lossless_packets)
            )

        for ip in ips_that_will_get_lossless_traffic:
            lossy_packets = 0
            lossless_packets = 1
            conf_args[PerfConsts.TRAFFIC_PATTERN][PerfConsts.LEFT_TG_ALIAS].append(
                (ip, [unconnected_spine_port], lossy_packets, lossless_packets)
            )


@pytest.fixture(scope='function')
def scenario_name(basic_setup_configuration):
    """
    Provide scenario_name at function scope for the parent conftest's update_test_mongo_metadata fixture.
    """
    scenario_name, conf_args = basic_setup_configuration
    return scenario_name
