"""Pytest infrastructure for AR L3 snake DVS performance tests.

Bring-up checklist (lab, prefer 2x first):
1. Confirm low log_ports (near 0x10001) cable to right_tg; high ports to left_tg.
2. Confirm right_tg has >=1 unconnected port for FWS after the chosen split.
3. Smoke TestArL3Snake_2x_lanes::test_ar_l3_snake_max_bandwidth, then 4x and 1x lanes.
4. The DUT sets the TTL_ERROR trap to forward so long snakes ignore the
   ~255-hop TTL limit (Alibaba snake phases 1 & 2); no TTL ACL is needed.
5. Recompute mloop pairs per split (install_snake_user_mloop runs after apply).
"""

import ipaddress
import logging
import os

import allure
import pytest

from devts.infra.tools.exceptions.test_issue import TestIssue
from ngts.constants.constants import BugHandlerConst, CliType
from ngts.constants.performance_constants import MongoDbConsts, PerfConsts
from ngts.helpers.performance.performance_db_helpers import (
    add_test_mongo_metadata, get_perf_test_name)
from ngts.helpers.performance.performance_recovery import has_primary_test_failure
from ngts.helpers.performance.performance_setup_helpers import (
    allure_attach_performance_conf_context, apply_test_configuration_guarded,
    restore_session_basic_configuration, save_session_base_configuration, skip_test_on_unsupported_os)
from ngts.helpers.performance.traffic_helpers import (
    create_json_traffic_file_with_stream_list, create_json_traffic_stream,
    dscp_to_tc)

logger = logging.getLogger()

TESTS_SCENARIO = PerfConsts.AR_L3_SNAKE_SCENARIO
SNAKE_INJECT_PAIRS = 1
# Chips the customer ACL profile is verified on; must match CUSTOMER_ACL_CHIP_NEEDS_SPC4_REMAP in perf_sys_sdk's ar_l3_snake_dut.py.
CUSTOMER_ACL_CHIPS = ("SPC4", "SPC6")

# The DUT and TG consume the same dynamically counted route-family dictionaries.
# Each unique prefix is installed in every forwarding VR, so dividing the 10K
# per-family route budget by the forwarding-hop count keeps the replicated route
# total at or below 10K per family.
ROUTE_BUDGET_PER_FAMILY = 10000
# The two IPv4 bases are far enough apart that the smallest CIDR covering each
# family's span stays disjoint from the other family's span, which is what lets
# the TG match each IPv4 family with its own windowed rule.
ROUTE_FAMILY_SPECS = (
    {"name": "ipv4_32", "base": "100.0.0.0", "prefix_len": 32},
    {"name": "ipv4_16", "base": "128.0.0.0", "prefix_len": 16},
    {"name": "ipv6_50", "base": "2100::", "prefix_len": 50},
    {"name": "ipv6_64", "base": "2200::", "prefix_len": 64},
)
# Source IP for the IPv6 family seed streams; the DVS TG conf only carries a
# v4 source and the scale families are swept in both AFs regardless of the
# class's address family.
FAMILY_SEED_SOURCE_IPV6 = "2003::1"
# UDP destination of the BTH-less streams. Anything but PerfConsts.ROCE_PORT:
# on 4791 the chip parses a BTH out of the payload whether or not one was built.
NON_ROCE_UDP_DST_PORT = 1234
# The TG increments the DIP with a flex-ACL ALU, which works on one 16-bit chunk
# of the DIP field at a time and can only store a chunk back on a 16-bit
# boundary. Route spacing and DIP stride both follow from this.
SWEEP_CHUNK_BITS = 16
L4_PORT_MAX = 0xFFFF


def build_sport_sweep(dip_sweep_families, flow_target):
    """Size the TG's UDP SPORT sweep so DIP x SPORT reaches ``flow_target`` flows.

    A flow is a distinct 5-tuple; with the SIP, DPORT and protocol fixed, the TG
    makes one per (DIP, SPORT) pair by walking every SPORT of the sweep before
    moving the DIP on. The sweep starts at the seed streams' own SPORT, so the
    seeds enter it on its first port.

    Args:
        dip_sweep_families: Sweep descriptors from ``build_dip_sweep_families``.
        flow_target: Distinct flows to reach across all families. 0 turns the
            SPORT sweep off.

    Returns:
        dict: ``{start, count, flows}`` for ``MultiDipsTG.sport_sweep`` (``flows``
        is what the run reaches, for the report), or ``{}`` when off.

    Raises:
        ValueError: If the sweep would leave the 16-bit L4 port space.
    """
    if not flow_target:
        return {}
    dips = sum(family["count"] for family in dip_sweep_families)
    count = max(2, -(-flow_target // dips))
    start = PerfConsts.UDP_SOURCE_PORT
    if start + count - 1 > L4_PORT_MAX:
        raise ValueError(f"A {flow_target}-flow SPORT sweep over {dips} DIPs needs {count} ports from {start}, "
                         f"past the last L4 port {L4_PORT_MAX}")
    return {"start": start, "count": count, "flows": dips * count}


def _smallest_covering_cidr(first, last):
    """Return the smallest single CIDR that contains the inclusive range.

    Args:
        first: First address of the range.
        last: Last address of the range.

    Returns:
        ip_network: Covering network, used as the TG's per-family match key.
    """
    differing_bits = (int(first) ^ int(last)).bit_length()
    return ipaddress.ip_network(f"{first}/{first.max_prefixlen - differing_bits}", strict=False)


def build_route_families(forwarding_hops, family_names=()):
    """Build route families sized for replication across every forwarding VR.

    Args:
        forwarding_hops: Number of routed snake hops, excluding the route-free tail.
        family_names: Names out of ``ROUTE_FAMILY_SPECS`` to run. Empty runs all
            of them; naming a subset is what sweeps one family in isolation.

    Returns:
        tuple: Family dictionaries with a floor-divided per-family prefix count.

    Raises:
        ValueError: If the hop count cannot produce at least one prefix per
            family, or if a requested family name is not defined.
    """
    if forwarding_hops <= 0:
        raise ValueError(f"AR L3 snake needs at least one forwarding hop, got {forwarding_hops}")
    unique_prefixes = ROUTE_BUDGET_PER_FAMILY // forwarding_hops
    if unique_prefixes == 0:
        raise ValueError(f"AR L3 snake has {forwarding_hops} forwarding hops, exceeding the {ROUTE_BUDGET_PER_FAMILY}-route family budget")
    specs = ROUTE_FAMILY_SPECS
    if family_names:
        specs_by_name = {family["name"]: family for family in ROUTE_FAMILY_SPECS}
        unknown = [name for name in family_names if name not in specs_by_name]
        if unknown:
            raise ValueError(f"AR L3 snake got unknown route families {unknown}, defined families are {sorted(specs_by_name)}")
        specs = tuple(specs_by_name[name] for name in family_names)
    return tuple({**family, "count": unique_prefixes} for family in specs)


def build_dip_sweep_families(families):
    """Derive TG DIP-sweep descriptors from the exact DUT route families.

    The TG increments a 16-bit-aligned DIP chunk high enough to change prefix
    bits. The DUT spaces prefixes by the same increment, so every generated DIP
    has an exact route in every forwarding VR.

    Each family also carries the smallest CIDR covering its whole span as a
    match key, so families of the same address family get separate rules. The
    keys do not overlap each other. Ordered most-specific key first, which is
    the order the TG installs them in.

    Args:
        families: Iterable of route-family dictionaries including ``count``.

    Returns:
        list: Sweep descriptors for ``MultiDipsTG.dip_sweep_families``.
    """
    sweep = []
    for family in families:
        network = ipaddress.ip_network(f"{family['base']}/{family['prefix_len']}", strict=False)
        host_bits = network.max_prefixlen - family["prefix_len"]
        field_offset = -(-host_bits // SWEEP_CHUNK_BITS) * SWEEP_CHUNK_BITS
        first = ipaddress.ip_address(int(network.network_address))
        last = ipaddress.ip_address(int(network.network_address) + family["count"] * (1 << field_offset) - 1)
        match = _smallest_covering_cidr(first, last)
        sweep.append({
            "name": family["name"],
            "start": family["base"],
            "count": family["count"],
            "field_offset": field_offset,
            "window_size": SWEEP_CHUNK_BITS,
            "is_ipv6": network.version == 6,
            "match_prefix": str(match.network_address),
            "match_mask": str(match.netmask) if match.version == 4 else match.netmask.exploded.upper(),
            "match_len": match.prefixlen,
        })
    return sorted(sweep, key=lambda family: family["match_len"], reverse=True)


def _get_test_config(request):
    """Return the ``CONFIG`` attribute of the current test class, or ``None``."""
    return getattr(request.cls, "CONFIG", None) if request.cls is not None else None


def _build_conf_args(request, players, chip_type):
    """Build Jinja arguments with one shared DUT/TG route-family model.

    Args:
        request: pytest FixtureRequest.
        players: Test players mapping.
        chip_type: Detected chip type string.

    Returns:
        dict: conf_args for ar_l3_snake Jinja templates.
    """
    config = _get_test_config(request)
    conf_args = config.to_template_args(chip_type)
    conf_args["snake_variant"] = request.cls.SNAKE_VARIANT
    conf_args["ar_traffic"] = request.cls.AR_TRAFFIC
    conf_args["customer_acl_enabled"] = request.cls.CUSTOMER_ACL
    port_groups = players[PerfConsts.DUT_ALIAS]["cli"].performance.get_ar_l3_snake_port_groups(conf_args["split_left"])
    forwarding_hops = sum(len(ports) for ports in port_groups.values()) - 1
    conf_args["expected_forwarding_hops"] = forwarding_hops
    conf_args["scale_route_families"] = build_route_families(forwarding_hops, request.cls.ROUTE_FAMILIES)
    conf_args["dip_sweep_families"] = build_dip_sweep_families(conf_args["scale_route_families"])
    conf_args["sport_sweep"] = build_sport_sweep(conf_args["dip_sweep_families"], request.cls.FLOW_TARGET)
    conf_args["lossy_dscp_value"] = PerfConsts.DVS_LOSSY_TC
    return conf_args


@pytest.fixture(scope="class")
def basic_setup_configuration(request, players, chip_type):
    """Apply the AR L3 snake baseline once per test class.

    Args:
        request: pytest FixtureRequest.
        players: Test players mapping.
        chip_type: Detected chip type string.

    Yields:
        dict: The class's ``conf_args``, or None for a class without a snake config.
    """
    config = _get_test_config(request)
    if config is None:
        yield None
        return
    skip_test_on_unsupported_os(players[PerfConsts.DUT_ALIAS]["cli"], CliType.NVUE)
    skip_test_on_unsupported_os(players[PerfConsts.DUT_ALIAS]["cli"], CliType.SONIC)
    if config.for_chip(chip_type) is None:
        pytest.skip(f"AR L3 snake config not defined for chip {chip_type}")
    if request.cls.CUSTOMER_ACL and chip_type not in CUSTOMER_ACL_CHIPS:
        pytest.skip(f"The customer ACL profile has no verified port mapping for chip {chip_type}")
    conf_args = _build_conf_args(request, players, chip_type)
    baseline_available = False
    primary_error = None
    try:
        with allure.step("Save Players initial Configuration"):
            save_session_base_configuration(players)
            baseline_available = True
        with allure.step("Apply AR L3 snake configuration on all Players"):
            apply_test_configuration_guarded(players, scenario=TESTS_SCENARIO, conf_args=conf_args,
                                             nodeid=request.node.nodeid)
        with allure.step("Install right_tg user mloop pair for the snake head"):
            players[PerfConsts.RIGHT_TG_ALIAS]["cli"].performance.install_snake_user_mloop(
                num_pairs=SNAKE_INJECT_PAIRS)
        with allure.step("Allure: attach conf_args"):
            allure_attach_performance_conf_context(players, conf_args)
        yield conf_args
    except Exception as error:
        primary_error = error
        raise
    finally:
        try:
            try:
                with allure.step("Remove right_tg user mloop pair so later tests auto-pair all ports"):
                    players[PerfConsts.RIGHT_TG_ALIAS]["cli"].performance.remove_snake_user_mloop()
            except Exception:
                if not has_primary_test_failure(primary_error, request.node):
                    raise
                logger.exception("Removing the user mloop pair failed while preserving the primary setup/test failure")
        finally:
            if baseline_available:
                try:
                    with allure.step("Restore Base Configuration on all Players"):
                        restore_session_basic_configuration(players, nodeid=request.node.nodeid)
                except Exception:
                    if not has_primary_test_failure(primary_error, request.node):
                        raise
                    logger.exception("Baseline restore failed while preserving the primary setup/test failure")


@pytest.fixture(autouse=True)
def _init_test_instance_state(request, players, power_thresholds_by_chip_type, chip_type):
    """Attach runtime state onto each AR L3 snake test instance.

    Args:
        request: pytest FixtureRequest.
        players: Test players mapping.
        power_thresholds_by_chip_type: Power threshold for the DUT chip.
        chip_type: Detected chip type string.
    """
    config = _get_test_config(request)
    if config is None:
        yield
        return
    conf_args = request.getfixturevalue("basic_setup_configuration")
    instance = request.instance
    instance.players = players
    instance.cli_object = players["dut"]["cli"]
    instance.chip_type = chip_type
    instance.power_thresholds_by_chip_type = power_thresholds_by_chip_type
    instance.scenario = TESTS_SCENARIO
    instance.conf_args = conf_args
    instance.traffic_jsons = get_ar_l3_snake_traffic(players, conf_args)
    yield


@pytest.fixture(autouse=True)
def update_test_mongo_metadata(request, port_group_df, chip_type, players):
    """Record Mongo metadata and push port groups to the DUT for TrafficValidator.

    Args:
        request: pytest FixtureRequest.
        port_group_df: Port-group dataframe fixture.
        chip_type: Detected chip type string.
        players: Test players mapping.
    """
    config = _get_test_config(request)
    if config is None:
        yield
        return
    test_name = get_perf_test_name(request)
    players["dut"]["cli"].performance.update_port_group_df_on_dut(port_group_df)
    add_test_mongo_metadata(
        test_name,
        {MongoDbConsts.CONF_NAME: config.mongo_conf_name(chip_type),
         MongoDbConsts.PORT_GROUP_DF: port_group_df})
    yield


def get_ar_l3_snake_traffic(players, conf_args, template_suite="traffic_packets_json_files"):
    """Build one lossy seed stream per route family on the right TG.

    Args:
        players: Test players mapping.
        conf_args: Template conf_args including packet counts and split.
        template_suite: Directory under performance_tests for JSON output.

    Returns:
        dict: Mapping of TG alias to traffic JSON path (only right_tg populated).

    Raises:
        TestIssue: If right_tg has fewer unconnected ports than the snake injects through.
    """
    traffic_jsons = {}
    pkt_size = conf_args["packet_size"]
    player_alias = PerfConsts.RIGHT_TG_ALIAS
    player_cli_obj = players[player_alias]["cli"]
    json_path = os.path.join(
        BugHandlerConst.NGTS_PATH, "performance_tests", template_suite, TESTS_SCENARIO,
        f"{player_alias}_{TESTS_SCENARIO}_{pkt_size}.json")
    traffic_parameters = player_cli_obj.performance.get_traffic_parameters(
        scenario=TESTS_SCENARIO, conf_args=conf_args)
    if len(traffic_parameters["ports"]) < SNAKE_INJECT_PAIRS:
        raise TestIssue(f"{player_alias} has {len(traffic_parameters['ports'])} unconnected ports after the split, and the snake "
                        f"injects through {SNAKE_INJECT_PAIRS}: {traffic_parameters['ports']}")
    ports = sorted(traffic_parameters["ports"])[:SNAKE_INJECT_PAIRS]
    traffic_parameters["ports"] = ports
    stream_list = _build_family_seed_streams(player_alias, traffic_parameters, conf_args)
    # A new scenario has no tracked directory under traffic_packets_json_files.
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    create_json_traffic_file_with_stream_list(player_alias, traffic_parameters, json_path, stream_list)
    traffic_jsons[player_alias] = json_path
    logger.info(f"AR L3 snake traffic JSON for {player_alias}: ports={ports}, streams={len(stream_list)}, "
                f"packet_size={pkt_size}, seeds_per_family={traffic_parameters['num_packets']}, "
                f"ar_eligible={'yes (RoCE BTH, AR bit set)' if conf_args['ar_traffic'] else 'no (plain UDP, no BTH)'}, "
                f"path={json_path}")
    return traffic_jsons


def _build_family_seed_streams(player_alias, traffic_parameters, conf_args):
    """Build one seed stream per scale-route family, each starting that sweep.

    Args:
        player_alias: TG alias the streams run on.
        traffic_parameters: Base traffic parameters from ``get_traffic_parameters``.
        conf_args: Template conf_args carrying ``dip_sweep_families`` and
            ``ar_traffic`` - RoCE with the AR bit set when True, plain UDP with
            no BTH when False.

    Returns:
        list: One lossy stream dictionary per configured route family.
    """
    sweep_families = conf_args["dip_sweep_families"]
    ipv4_source = traffic_parameters["IP"]["src"]
    stream_list = []
    lossy_tc = dscp_to_tc(conf_args["lossy_dscp_value"], 2)
    # An AR-ineligible scenario drops the BTH *and* moves off the RoCE port:
    # behind UDP 4791 the chip parses the first payload bytes as a BTH, and
    # this payload puts an ASCII letter (>= 0x40) where the AR bit lives, which
    # would read as AR-eligible. The DUT's classifier key follows the same flag
    # and is what actually keeps AR out of the picture.
    ar_traffic = conf_args["ar_traffic"]
    udp_ports = dict(traffic_parameters["UDP"])
    if not ar_traffic:
        udp_ports["dst"] = NON_ROCE_UDP_DST_PORT
    for family in sweep_families:
        family_parameters = dict(traffic_parameters)
        family_parameters["AR"] = PerfConsts.ADAPTIVE_ROUTING_ENABLED
        family_parameters["UDP"] = udp_ports
        family_parameters["is_ipv6"] = family["is_ipv6"]
        family_parameters["IP"] = {
            "src": FAMILY_SEED_SOURCE_IPV6 if family["is_ipv6"] else ipv4_source,
            "dst": family["start"],
        }
        stream_list.append(create_json_traffic_stream(
            player_alias, family_parameters, f"{TESTS_SCENARIO}_{player_alias}_seed_{family['name']}", tc=lossy_tc,
            add_bth=ar_traffic))
    return stream_list
