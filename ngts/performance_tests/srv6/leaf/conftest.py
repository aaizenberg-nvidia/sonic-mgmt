import logging
import os
from collections import defaultdict

import allure
import pytest

from ngts.constants.constants import SonicConst, BugHandlerConst
from ngts.constants.performance_constants import Cl_Consts, PerfConsts, MRCConsts
from ngts.helpers.performance.performance_recovery import has_primary_test_failure
from ngts.helpers.performance.performance_setup_helpers import (
    apply_test_configuration_guarded, restore_session_basic_configuration,
    save_session_base_configuration)
from ngts.cli_wrappers.nvue.nvue_performance_clis import get_srv6_dut_left_ports_num
from ngts.performance_tests.srv6.utils.srv6_traffic_patterns import get_tg_bisection_traffic_params


logger = logging.getLogger()


def get_bisection_traffic(players, conf_args, traffic_type,
                          dut_interfaces_ipv6_configuration_dict, create_workload_stream,
                          upstream_ports, downstream_ports,
                          template_suite="traffic_packets_json_files",
                          packet_size=None, mrc_num_packets=None, vary_src_ip=False):
    """Build bisection traffic JSONs for each TG.

    Args:
        packet_size: If set, MRC data streams use this size. If omitted, MRC data
            size defaults to ``MRCConsts.MRC_DATA_PACKET_SIZE`` in stream creation.
        mrc_num_packets: If set, overrides the number of MRC data packets per stream.
            If omitted, defaults to 6 per MRC class in workload_1.
        vary_src_ip: If True, each MRC data packet uses a different consecutive src IPv6
            address so the switch treats each packet as a unique flow.
    """
    traffic_jsons = {}
    tg_src_dst_port_bisection_pairs_dict = sort_bisection_pairs_by_tg_alias(players, upstream_ports, downstream_ports)
    for tg_alias, bisection_pairs in tg_src_dst_port_bisection_pairs_dict.items():
        get_tg_bisection_traffic_params(players, tg_alias, conf_args, traffic_type, template_suite, create_workload_stream,
                                        dut_interfaces_ipv6_configuration_dict, traffic_jsons, bisection_pairs,
                                        mrc_data_packet_size=packet_size, mrc_num_packets=mrc_num_packets,
                                        vary_src_ip=vary_src_ip)

    return traffic_jsons


def sort_bisection_pairs_by_tg_alias(players, upstream_ports, downstream_ports):
    ports = players['dut']['cli'].performance.get_right_left_ports_dict()
    left_ports = ports["left_ports"]
    right_ports = ports["right_ports"]
    upstream_to_downstream_port_bisection_pairs = list(zip(upstream_ports, downstream_ports))
    downstream_to_upstream_port_bisection_pairs = list(zip(downstream_ports, upstream_ports))
    all_bisection_pairs = upstream_to_downstream_port_bisection_pairs + downstream_to_upstream_port_bisection_pairs
    tg_src_dst_port_bisection_pairs_dict = defaultdict(list)
    for pair in all_bisection_pairs:
        if pair[0] in left_ports:
            tg_src_dst_port_bisection_pairs_dict[PerfConsts.LEFT_TG_ALIAS].append(pair)
        elif pair[0] in right_ports:
            tg_src_dst_port_bisection_pairs_dict[PerfConsts.RIGHT_TG_ALIAS].append(pair)
    return tg_src_dst_port_bisection_pairs_dict


def build_conf_args_base(chip_type, players):
    """Return the shared ``conf_args`` keys common to the base fixture and any per-test override.

    Callers add the derived keys -- ``split_left``, ``split_right`` and
    ``speed`` -- which are the only ones that legitimately vary between the
    base fixture and the no-SRH override, plus any OS-specific extras
    (e.g. ``link_phy_speed`` / ``dut_left_ports_num`` on Cumulus).

    Populates the following keys:
        * ``is_ipv6``: ``True``.
        * ``packet_size``: ``4096``.
        * ``scenario``: ``"srv6"``.
        * ``hwsku``: ``MRCConsts.HWSKU_BY_CHIP_TYPE[chip_type]["leaf"]``.
        * ``dut_mac``: ``players['dut']['cli'].performance.mac``.
        * ``dut``: ``"leaf"``.
        * ``chip_type``: passthrough of ``chip_type``.
        * ``downlinks_tg``: ``PerfConsts.RIGHT_TG_ALIAS``.
        * ``template_path``: absolute path to the SRv6 SONiC template dir
          under ``BugHandlerConst.NGTS_PATH/performance_tests/<default templates dir>/srv6/sonic``.

    Args:
        chip_type: Chip family key (e.g. ``"SPC4"``, ``"SPC5"``), used to
            look up the leaf HWSKU from ``MRCConsts.HWSKU_BY_CHIP_TYPE``.
        players: Mapping of alias -> player dict; must contain a ``'dut'``
            entry exposing ``cli.performance.mac``.

    Returns:
        dict: The base ``conf_args`` mapping described above (no
            ``split_left`` / ``split_right`` / ``speed`` keys).

    Raises:
        KeyError: If ``chip_type`` is not a key of
            ``MRCConsts.HWSKU_BY_CHIP_TYPE`` or its entry lacks a
            ``"leaf"`` key, or if ``players['dut']`` is missing.
    """
    sku = MRCConsts.HWSKU_BY_CHIP_TYPE[chip_type]["leaf"]
    return {
        "is_ipv6": True,
        "packet_size": 4096,
        "scenario": "srv6",
        "hwsku": sku,
        "dut_mac": players['dut']['cli'].performance.mac,
        "dut": "leaf",
        "chip_type": chip_type,
        "downlinks_tg": PerfConsts.RIGHT_TG_ALIAS,
        "template_path": os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                      PerfConsts.DEFAULT_PERF_TEMPLATES_DIR, "srv6", "sonic"),
    }


@pytest.fixture(scope='class', autouse=True)
def conf_args(chip_type, players):
    is_cumulus = players["dut"].get("is_cumulus", False)
    base = build_conf_args_base(chip_type, players)
    split_left = Cl_Consts.SRV6_SPLIT_LEFT_BY_CHIP_TYPE[chip_type]
    split_right = Cl_Consts.SRV6_SPLIT_RIGHT_BY_CHIP_TYPE[chip_type]
    speed = (Cl_Consts.SRV6_SPEED_BY_CHIP_TYPE[chip_type] if is_cumulus
             else SonicConst.HWSKU_DOWNSTREAM_PORTS_SPEED[base["hwsku"]])
    conf_args = {
        **base,
        "split_left": split_left,
        "split_right": split_right,
        "speed": speed,
    }
    if (is_cumulus and chip_type == "SPC6" and
            split_left == 8 and split_right == 8):
        conf_args["link_phy_speed"] = "200G"
    if is_cumulus:
        conf_args["dut_left_ports_num"] = get_srv6_dut_left_ports_num(
            players['dut']['cli'].performance, split_left)
    return conf_args


@pytest.fixture(scope='class', autouse=True)
def basic_setup_configuration(request, players, conf_args):
    baseline_available = False
    primary_error = None
    try:
        with allure.step('Save Players initial Configuration'):
            save_session_base_configuration(players)
            baseline_available = True
        with allure.step("Apply Test configuration on all Players"):
            apply_test_configuration_guarded(
                players, scenario=conf_args["scenario"], conf_args=conf_args,
                nodeid=request.node.nodeid)
        yield
    except Exception as error:
        primary_error = error
        raise
    finally:
        if baseline_available:
            try:
                with allure.step('Restore Base Configuration on all Players'):
                    restore_session_basic_configuration(
                        players, nodeid=request.node.nodeid)
            except Exception:
                if not has_primary_test_failure(primary_error, request.node):
                    raise
                logger.exception(
                    "Baseline restore failed while preserving the primary setup/test failure")


@pytest.fixture(scope='function', autouse=False)
def cleanup_trimming_threshold(players, conf_args):
    """
    to remove zero scheduler configuration
    """
    yield
    cli_obj = players['dut']['cli']
    cli_obj.general.reload_configuration(force=True)
    cli_obj.general.verify_dockers_are_up()
    cli_obj.trimming.configure_packets_aging()
