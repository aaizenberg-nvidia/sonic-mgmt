"""
Shared pytest infrastructure for Spectrum-X RA performance tests.

Provides:
  - TESTS_SCENARIO        scenario identifier used by Jinja templates,
                          CLI wrappers, MARS plumbing, and the test classes.
  - Autouse fixtures      apply per-config skip rules, build the baseline,
                          and attach per-instance test state on TestSPCXRA_*
                          classes. Each fixture short-circuits on sibling
                          tests in this directory (Alibaba, 400G->200G).
  - Traffic helpers       spine/leaf JSON builders used by spcx_ra tests
                          and by sibling suites (optimizer, Alibaba,
                          400G->200G).
  - resolve_ar_algorithm  the AR port-selection algorithm every spcx_ra
                          conf_args builder configures on the DUT.

The LanesConfig dataclass and the test classes live in test_spcx_ra.py.
This module uses duck typing (getattr(cls, "CONFIG", None)) so it has no
dependency on the dataclass.
"""
import copy
import logging
import os
import random
from collections import Counter
from typing import Dict, Iterable, List, Tuple

import allure
import pytest

from devts.infra.tools.exceptions.test_issue import TestIssue
from ngts.cli_wrappers.nvue.nvue_cli import NvueCli
from ngts.constants.constants import BugHandlerConst, InfraConst
from ngts.constants.performance_constants import (
    ArAlgorithm, MongoDbConsts, PerfConsts, SPCXRAConsts)
from ngts.helpers.performance.performance_db_helpers import (
    add_test_mongo_metadata, get_perf_test_name)
from ngts.helpers.performance.performance_recovery import has_primary_test_failure
from ngts.helpers.performance.performance_setup_helpers import (
    allure_attach_performance_conf_context, apply_test_configuration_guarded,
    get_topology_obj, restore_session_basic_configuration,
    save_session_base_configuration, skip_test_on_unsupported_os)
from ngts.helpers.performance.traffic_helpers import (
    create_json_traffic_file, create_json_traffic_file_with_stream_list,
    create_json_traffic_stream)

logger = logging.getLogger()

TESTS_SCENARIO = "spcx_ra"

# Round-Robin and Semi-Round-Robin need the AR port-selection hardware introduced
# in SPC6, so they are rejected on SPC4 and SPC5 (see ArAlgorithm's docstring).
ROUND_ROBIN_ALGORITHMS = (ArAlgorithm.ROUND_ROBIN, ArAlgorithm.SEMI_ROUND_ROBIN)
ROUND_ROBIN_UNSUPPORTED_CHIPS = ("SPC4", "SPC5")


def resolve_ar_algorithm(chip_type) -> ArAlgorithm:
    """Return the AR algorithm the spcx_ra suites configure on the DUT.

    The choice belongs to the test layer, so every spcx_ra ``conf_args`` builder
    - this suite, the optimizer, Alibaba AR and 400G->200G - calls this helper
    instead of each spelling out its own rule.

    The ``AR_ALGORITHM`` env variable wins when set, e.g.
    ``AR_ALGORITHM=semi_round_robin pytest ...`` - it applies to the whole run,
    and raises both on an unsupported value and on a round-robin algorithm
    requested on SPC4 / SPC5. Otherwise the per-chip default applies -
    Round-Robin on SPC6, Random-From-Best on older silicon. That default is also
    perf_sys_sdk's own, so a suite that starts passing the resolved value keeps
    the behavior it had while the key was omitted.

    The value flows: conf_args -> dut.jinja "ar_algorithm" key -> perf_sys_sdk
    spcx_basic_ra_dut_config.config_ar -> ar_profile_create(port_select_mode=...).

    Args:
        chip_type: Detected chip-type identifier, e.g. ``"SPC5"`` / ``"SPC6"``.

    Returns:
        ArAlgorithm: the member to place in ``conf_args[PerfConsts.AR_ALGORITHM]``.

    Raises:
        ValueError: ``AR_ALGORITHM`` holds a value that is not supported.
        TestIssue: ``AR_ALGORITHM`` requests a round-robin algorithm on a chip
            that does not support it (SPC4 / SPC5).
    """
    algorithm = ArAlgorithm.from_env()
    if algorithm is None:
        return (ArAlgorithm.ROUND_ROBIN if chip_type == "SPC6"
                else ArAlgorithm.RANDOM_FROM_BEST)
    if (algorithm in ROUND_ROBIN_ALGORITHMS and
            chip_type in ROUND_ROBIN_UNSUPPORTED_CHIPS):
        raise TestIssue(
            f"{PerfConsts.AR_ALGORITHM_ENV_VAR}='{algorithm.value}' is not supported on "
            f"{chip_type} | supported on this DUT: {ArAlgorithm.RANDOM_FROM_BEST.value}. "
            f"Unset {PerfConsts.AR_ALGORITHM_ENV_VAR} to use the per-chip default")
    return algorithm


def _get_test_config(request):
    """Return the ``CONFIG`` attribute of the current test class, or ``None``.

    Used by autouse fixtures to short-circuit on sibling tests in this
    directory tree (``Alibaba_AR_tests``, ``400G_to_200G_Configuration``, ...)
    that don't carry the ``CONFIG`` attribute.

    Args:
        request: pytest ``FixtureRequest``.

    Returns:
        The ``CONFIG`` attribute (a ``LanesConfig``) of the test class
        currently being collected, or ``None`` when the test isn't one of
        the ``TestSpcXRA_Nx_lanes`` classes.
    """
    cls = getattr(request, "cls", None)
    return getattr(cls, "CONFIG", None) if cls else None


# ---------------------------------------------------------------------- fixtures

@pytest.fixture(scope='class', autouse=True)
def _skip_if_chip_or_os_unsupported(request, players, chip_type):
    """Skip the test when the current chip / OS is unsupported by the config.

    Skips a ``TestSpcXRA_Nx_lanes`` class when the current chip is missing
    from the ``CONFIG.chip_dict`` mapping, or when the chip's per-chip entry
    declares the current CLI/OS family unsupported.

    Class-scoped because ``test_spcx_ra.py`` defines several classes in one
    module, each with its own per-chip rules.

    Args:
        request: pytest ``FixtureRequest``.
        players: Test players mapping.
        chip_type: Detected chip-type identifier of the current DUT.
    """
    config = _get_test_config(request)
    if config is None:
        return
    chip_speed_config = config.for_chip(chip_type)
    if chip_speed_config is None:
        pytest.skip(
            f"{request.cls.__name__}: no configuration for chip {chip_type}")
    if chip_speed_config.unsupported_os is not None:
        skip_test_on_unsupported_os(players['dut']['cli'], chip_speed_config.unsupported_os)


@pytest.fixture(scope='class', autouse=True)
def _disable_unused_spc6_nvue_tc_latency(request, players, chip_type):
    """Disable unused TC-latency collection for SPC6 NVUE max-breakout (1x) tests.

    Bandwidth / fairness / occupancy / power / counters are validated, but no
    SPCX-RA test consumes ``TC_latency_samples``. On SPC6 1x (many logical
    ports) collecting latency takes several minutes. SRv6 keeps the collector
    enabled because it validates latency.
    """
    config = _get_test_config(request)
    if config is None or config.lanes_per_subport != 1:
        yield
        return

    collect_tc_latency = PerfConsts.SAMPLES_PARAMS[PerfConsts.COLLECT_TC_LATENCY_ENV_VAR]
    if chip_type == "SPC6" and isinstance(players[PerfConsts.DUT_ALIAS]['cli'], NvueCli):
        logger.info("Disabling unused TC latency collection for SPC6 NVUE 1x tests")
        PerfConsts.SAMPLES_PARAMS[PerfConsts.COLLECT_TC_LATENCY_ENV_VAR] = "False"
    yield
    PerfConsts.SAMPLES_PARAMS[PerfConsts.COLLECT_TC_LATENCY_ENV_VAR] = collect_tc_latency


@pytest.fixture(scope='class')
def basic_setup_configuration(request, players, chip_type,
                              _skip_if_chip_or_os_unsupported):
    """Apply the per-config baseline once per test class.

    Indirect-parametrized over IPv4/IPv6 by ``_SpcXRATestBase``. Sibling
    tests (Alibaba, 400G->200G) shadow this fixture in their own child
    conftests, so this definition is only consumed by
    ``TestSpcXRA_Nx_lanes`` classes.

    Depends on ``_skip_if_chip_or_os_unsupported`` so pytest evaluates the
    chip/OS skip first; if the skip fires, the DUT baseline is never
    saved or modified.

    Args:
        request: pytest ``FixtureRequest``; ``request.param`` carries the
            indirect-parametrize value (``InfraConst.IPV4`` or ``IPV6``).
        players: Test players mapping.
        chip_type: Detected chip-type identifier; selects the per-chip
            entry inside ``CONFIG.chip_dict``.
        _skip_if_chip_or_os_unsupported: Dependency-only — forces the
            skip fixture to run before any config is applied.

    Yields:
        ``bool``: ``True`` when the test is running under IPv6, ``False``
        for IPv4. ``None`` when invoked from a non-SPCXRA test (defensive
        no-op).
    """
    config = _get_test_config(request)
    if config is None:
        yield None
        return
    is_ipv6 = request.param == InfraConst.IPV6
    absolute_breakout = isinstance(players[PerfConsts.DUT_ALIAS]['cli'], NvueCli)
    conf_args = config.to_template_args(
        chip_type, is_ipv6, absolute_breakout=absolute_breakout)
    baseline_available = False
    primary_error = None
    try:
        with allure.step('Save Players initial Configuration'):
            save_session_base_configuration(players)
            baseline_available = True
        with allure.step("Apply Test configuration on all Players"):
            apply_test_configuration_guarded(
                players, scenario=TESTS_SCENARIO, conf_args=conf_args,
                nodeid=request.node.nodeid)
        with allure.step("Allure: attach conf_args and DUT applied NVUE YAML"):
            allure_attach_performance_conf_context(players, conf_args)
        yield is_ipv6
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


@pytest.fixture(autouse=True)
def _init_test_instance_state(request, players, engines,
                              power_thresholds_by_chip_type, chip_type):
    """Attach the runtime test state onto each TestSpcXRA_Nx_lanes instance.

    Pulls ``basic_setup_configuration`` lazily via
    :func:`pytest.FixtureRequest.getfixturevalue` so sibling tests in this
    directory tree (which don't request that fixture) are unaffected.

    Args:
        request: pytest ``FixtureRequest``.
        players: Test players mapping.
        engines: Engines mapping (DUT, traffic generators).
        power_thresholds_by_chip_type: Power-consumption threshold for the
            DUT's chip type.
        chip_type: Detected chip-type identifier.

    Yields:
        ``None``. The fixture exists only for its side effect of
        populating instance attributes used by every test method.
    """
    config = _get_test_config(request)
    if config is None:
        yield
        return
    is_ipv6 = request.getfixturevalue('basic_setup_configuration')
    _populate_test_state(
        request.instance, config, is_ipv6, request.node.nodeid,
        players, engines, power_thresholds_by_chip_type, chip_type)
    yield


def _populate_test_state(instance, config, is_ipv6, nodeid, players, engines,
                         power_thresholds_by_chip_type, chip_type):
    """Attach the per-test runtime state to a TestSpcXRA_Nx_lanes instance.

    The static configuration is already on the class as ``instance.CONFIG``;
    this function only attaches runtime values (players, engines, conf_args,
    pre-built traffic JSONs, ...).

    Args:
        instance: The pytest test-class instance to populate.
        config: The ``LanesConfig`` pinned to the class.
        is_ipv6: Whether the test runs under IPv6.
        nodeid: Pytest node identifier for fatal-state attribution.
        players: Test players mapping.
        engines: Engines mapping (DUT, traffic generators).
        power_thresholds_by_chip_type: Power-consumption threshold for the
            DUT's chip type.
        chip_type: Detected chip-type identifier.
    """
    instance.players = players
    instance.nodeid = nodeid
    instance.engines = engines
    instance.dut_engine = engines['dut']
    instance.cli_object = players['dut']['cli']
    instance.chip_type = chip_type
    instance.power_thresholds_by_chip_type = power_thresholds_by_chip_type
    instance.scenario = TESTS_SCENARIO
    instance.is_ipv6 = is_ipv6
    instance.ip = InfraConst.IPV6 if is_ipv6 else InfraConst.IPV4
    instance.topology_obj = get_topology_obj(players)
    absolute_breakout = isinstance(players[PerfConsts.DUT_ALIAS]['cli'], NvueCli)
    instance.conf_args = config.to_template_args(
        chip_type, is_ipv6, absolute_breakout=absolute_breakout)
    instance.ar_algorithm = instance.conf_args[PerfConsts.AR_ALGORITHM]
    instance.traffic_jsons = get_spcx_ra_spine_traffic(players, instance.conf_args)


@pytest.fixture(autouse=True)
def _report_ar_parameters(request, _init_test_instance_state):
    """Show the AR algorithm and congestion thresholds in the Allure parameters.

    Reads ``instance.ar_algorithm`` and the low/mid/high congestion thresholds
    off ``instance.conf_args`` rather than resolving them again, so the reported
    values are the ones that went into ``conf_args`` and on to the DUT.

    Depends on ``_init_test_instance_state`` so the attributes are attached
    first, and is function-scoped so the parameters land on every test rather
    than on the first test of the class only.

    Args:
        request: pytest ``FixtureRequest``.
        _init_test_instance_state: Dependency-only - forces the instance state
            this fixture reads to be populated first.
    """
    if _get_test_config(request) is None:
        return
    allure.dynamic.parameter(PerfConsts.AR_ALGORITHM, request.instance.ar_algorithm)
    for threshold_key in ("congestion_thresh_lo", "congestion_thresh_med",
                          "congestion_thresh_hi"):
        allure.dynamic.parameter(threshold_key, request.instance.conf_args[threshold_key])


@pytest.fixture(autouse=True)
def _set_cumulus_ar_profile(request, players, chip_type, _init_test_instance_state):
    """Apply the Cumulus AR profile on the DUT.

    Depends on ``_init_test_instance_state`` so ``conf_args`` is already
    attached to the test instance before ``set_ibm`` consumes it.

    Args:
        request: pytest ``FixtureRequest``.
        players: Test players mapping.
        chip_type: Detected chip-type identifier of the current DUT.
        _init_test_instance_state: Dependency-only - forces the instance state
            this fixture reads to be populated first.
    """
    if _get_test_config(request) is None:
        return
    cli_object = players[PerfConsts.DUT_ALIAS]['cli']
    if not isinstance(cli_object, NvueCli):
        return
    with allure.step("Apply the Cumulus AR profile on the DUT"):
        cli_object.performance.set_ibm(
            TESTS_SCENARIO, request.instance.conf_args, chip_type)


@pytest.fixture(autouse=True)
def update_test_mongo_metadata(request, port_group_df, chip_type, players):
    """Record per-test Mongo metadata for SPCXRA tests.

    Pushes the port-group dataframe to the DUT (``/tmp/conf.json``) so
    TrafficValidator uses the same groups, then writes the chip-specific
    spine ``CONF_NAME`` and port-group dataframe onto each test's Mongo
    record. Sibling tests (Alibaba, 400G->200G) override this fixture in
    their child conftests; this version is a no-op for them.

    Args:
        request: pytest ``FixtureRequest``.
        port_group_df: Per-test port-group dataframe.
        chip_type: Detected chip-type identifier; selects the per-chip
            entry inside ``CONFIG.chip_dict``.
        players: Test players mapping.

    Yields:
        ``None``. Exists only for its side effect of recording metadata.
    """
    config = _get_test_config(request)
    if config is None:
        yield
        return
    test_name = get_perf_test_name(request)
    players['dut']['cli'].performance.update_port_group_df_on_dut(port_group_df)
    add_test_mongo_metadata(
        test_name,
        {MongoDbConsts.CONF_NAME: config.mongo_conf_name_spine(chip_type),
         MongoDbConsts.PORT_GROUP_DF: port_group_df})
    yield


# ------------------------------------------------------- shared traffic helpers

def get_spcx_ra_spine_traffic(players, conf_args, template_suite="traffic_packets_json_files"):
    traffic_jsons = {}
    pkt_size = PerfConsts.PACKET_SIZE_LIST[0]
    for player_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
        player_cli_obj = players[player_alias]['cli']
        json_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                                 TESTS_SCENARIO,
                                 f"{player_alias}_{TESTS_SCENARIO.replace('/', '_')}_{pkt_size}.json")
        traffic_parameters = player_cli_obj.performance.get_traffic_parameters(
            scenario=TESTS_SCENARIO, conf_args=conf_args)
        create_json_traffic_file(
            player_alias=player_alias, traffic_parameters=traffic_parameters, json_path=json_path)
        traffic_jsons[player_alias] = json_path
    return traffic_jsons


def get_spcx_ra_leaf_traffic(players, conf_args, template_suite="traffic_packets_json_files",
                             use_incremental_dips=False, incremental_dip_num_packets=None):
    if not use_incremental_dips:
        conf_args["left_num_packets"] = 1
    traffic_jsons = {}
    pkt_size = PerfConsts.PACKET_SIZE_LIST[0]
    for player_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
        player_cli_obj = players[player_alias]['cli']
        json_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                                 TESTS_SCENARIO,
                                 f"{player_alias}_{TESTS_SCENARIO.replace('/', '_')}_{pkt_size}.json")
        traffic_parameters = player_cli_obj.performance.get_traffic_parameters(
            scenario=TESTS_SCENARIO, conf_args=conf_args)
        if player_alias == conf_args["host"]:
            create_json_traffic_file(
                player_alias=player_alias, traffic_parameters=traffic_parameters,
                json_path=json_path)
        elif use_incremental_dips:
            get_spine_to_leaf_incremental_dip_stream(
                players, player_alias, conf_args, traffic_parameters, json_path,
                num_packets=incremental_dip_num_packets)
        else:
            get_spine_to_leaf_stream_list(
                players, player_alias, conf_args, traffic_parameters, json_path)
        traffic_jsons[player_alias] = json_path
    return traffic_jsons


def get_spine_to_leaf_stream_list(players, spine_tg, conf_args, traffic_parameters,
                                  json_path, ip_protocol=PerfConsts.IP_PROTOCOL_UDP):
    dut_configuration = players['dut']['cli'].performance.get_device_configuration(
        conf_args=conf_args)
    leaf_dst_ips = list(dut_configuration["right_side_ports_to_ip_dict"].values())
    stream_list = []
    for ip in leaf_dst_ips:
        stream_name = f"spine_to_leaf_ip_{ip}"
        traffic_parameters["IP"]["dst"] = ip
        stream = create_json_traffic_stream(
            spine_tg, traffic_parameters, stream_name, ip_protocol=ip_protocol)
        stream_list.append(stream)
    create_json_traffic_file_with_stream_list(
        spine_tg, traffic_parameters, json_path, stream_list)


def get_spine_to_leaf_incremental_dip_stream(players, spine_tg, conf_args, traffic_parameters,
                                             json_path, ip_protocol=PerfConsts.IP_PROTOCOL_UDP,
                                             num_packets=None):
    """Build a single spine-to-leaf stream; MultiDipsTG increments DIP per packet on the TG.

    Args:
        players: Test players mapping.
        spine_tg: Spine traffic-generator alias (left_tg in leaf scenarios).
        conf_args: Configuration arguments dictionary.
        traffic_parameters: Traffic parameters from get_traffic_parameters.
        json_path: Output path for the traffic JSON file.
        ip_protocol: IP protocol constant (UDP by default).
        num_packets: Number of packets to send per port. When None, defaults to
            the number of leaf destination IPs (one packet per DIP).
    """
    dut_configuration = players['dut']['cli'].performance.get_device_configuration(
        conf_args=conf_args)
    leaf_dst_ips = list(dut_configuration["right_side_ports_to_ip_dict"].values())
    start_dip = leaf_dst_ips[0]
    traffic_parameters["IP"]["dst"] = start_dip
    traffic_parameters["num_packets"] = num_packets if num_packets is not None else len(leaf_dst_ips)
    stream_name = f"spine_to_leaf_incremental_dip_{start_dip}"
    stream = create_json_traffic_stream(
        spine_tg, traffic_parameters, stream_name, ip_protocol=ip_protocol)
    create_json_traffic_file_with_stream_list(
        spine_tg, traffic_parameters, json_path, [stream])


def get_leaf_scenario_traffic(players, conf_args, template_suite="traffic_packets_json_files",
                              use_incremental_dips=False, incremental_dip_num_packets=None):
    """
    Builds the traffic JSON files for the leaf scenario.

    Deliberately separate from get_spcx_ra_leaf_traffic: that helper is shared with the Alibaba AR
    and 400G-to-200G suites. This one asks the NOS for its
    own downstream destination set through performance.get_leaf_dip_list, DVS and SONiC keep
    using the destinations they already publish while Cumulus supplies per-port routed ones.

    Only Cumulus gets the scheduled downstream pattern. DVS already meets the leaf bar with the
    one-stream-per-destination pattern it has always used, so it keeps that path byte for byte.

    Args:
        players (dict): Test players mapping.
        conf_args (dict): Configuration arguments dictionary.
        template_suite (str): Directory holding the traffic JSON templates.
        use_incremental_dips (bool): Let the TG increment the destination per packet instead of
            emitting one stream per destination.
        incremental_dip_num_packets (int): Packets per port when incrementing on the TG. Defaults to
            the number of leaf destinations when None.

    Returns:
        dict: Traffic JSON path per player alias.
    """
    is_nvue_dut = isinstance(players[PerfConsts.DUT_ALIAS]['cli'], NvueCli)
    if not is_nvue_dut and not use_incremental_dips:
        conf_args["left_num_packets"] = 1
    traffic_jsons = {}
    pkt_size = PerfConsts.PACKET_SIZE_LIST[0]
    for player_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
        player_cli_obj = players[player_alias]['cli']
        json_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", template_suite,
                                 TESTS_SCENARIO,
                                 f"{player_alias}_{TESTS_SCENARIO.replace('/', '_')}_{pkt_size}.json")
        traffic_parameters = player_cli_obj.performance.get_traffic_parameters(
            scenario=TESTS_SCENARIO, conf_args=conf_args)
        if player_alias == conf_args["host"]:
            create_json_traffic_file(
                player_alias=player_alias, traffic_parameters=traffic_parameters,
                json_path=json_path)
        elif use_incremental_dips:
            build_leaf_dip_incremental_stream(
                players, player_alias, conf_args, traffic_parameters, json_path,
                num_packets=incremental_dip_num_packets)
        elif is_nvue_dut:
            build_leaf_scheduled_stream_list(
                players, player_alias, conf_args, traffic_parameters, json_path)
        else:
            get_spine_to_leaf_stream_list(
                players, player_alias, conf_args, traffic_parameters, json_path)
        traffic_jsons[player_alias] = json_path
    return traffic_jsons


def build_leaf_scheduled_stream_list(players, spine_tg, conf_args, traffic_parameters, json_path,
                                     ip_protocol=PerfConsts.IP_PROTOCOL_UDP):
    """
    Builds the downstream leaf traffic from a per-port destination schedule.

    Each ingress port is given its own scattered subset of the destination list. Destinations are
    what pick the egress side here, by longest-prefix match onto one right port each, so the
    schedule decides how the load divides: a right port carries exactly the packets addressed to
    its destination. The pattern this replaces listed every TG port on every stream and staged 64
    packets per port to do it, which ran at 0.655 of line rate with egress occupancy nineteen
    times its threshold.

    Args:
        players (dict): Test players mapping.
        spine_tg (str): Spine traffic-generator alias (left_tg in leaf scenarios).
        conf_args (dict): Configuration arguments dictionary.
        traffic_parameters (dict): Traffic parameters from get_traffic_parameters.
        json_path (str): Output path for the traffic JSON file.
        ip_protocol (str): IP protocol constant, UDP by default.

    Raises:
        TestIssue: If the DUT resolves no leaf destination IPs, or the TG exposes no traffic ports.
    """
    leaf_dst_ips = _resolve_leaf_dip_list(players, spine_tg, conf_args)
    tg_ports = list(traffic_parameters["ports"])
    if not tg_ports:
        raise TestIssue(f"No traffic ports resolved for {spine_tg} | "
                        f"expected one per DUT left port, got {tg_ports}")
    set_size, packets_per_stream = _resolve_leaf_schedule_shape(
        len(tg_ports), len(leaf_dst_ips), int(traffic_parameters["num_packets"]))
    port_schedules = build_leaf_dip_schedule(len(tg_ports), len(leaf_dst_ips), set_size)
    logger.info(f"Scheduling leaf traffic on {spine_tg} | {len(tg_ports)} ports x {set_size} "
                f"destinations x {packets_per_stream} packets, drawn from {len(leaf_dst_ips)} "
                f"destinations, {len(tg_ports) * set_size * packets_per_stream} packets resident")
    _log_leaf_schedule_spread(port_schedules, len(leaf_dst_ips), spine_tg)
    traffic_parameters["num_packets"] = packets_per_stream
    stream_list = []
    for slot in range(set_size):
        for port_index, tg_port in enumerate(tg_ports):
            dst_ip = leaf_dst_ips[port_schedules[port_index][slot]]
            traffic_parameters["ports"] = [tg_port]
            traffic_parameters["IP"]["dst"] = dst_ip
            stream = create_json_traffic_stream(
                spine_tg, traffic_parameters, f"spine_to_leaf_p{tg_port}_s{slot}_ip_{dst_ip}",
                ip_protocol=ip_protocol)
            stream_list.append(_shrink_stream_payload(stream, spine_tg))
    traffic_parameters["ports"] = tg_ports
    create_json_traffic_file_with_stream_list(
        spine_tg, traffic_parameters, json_path, stream_list)


def build_leaf_dip_schedule(port_count, dip_count, set_size,
                            seed=SPCXRAConsts.LEAF_SCHEDULE_SEED):
    """
    Assigns every traffic-generator port its own scattered subset of the destination list.

    Built as a Latin rectangle: one random permutation of the destinations, read at a different
    random offset for each slot. Slots carry no timing meaning, since the generator recirculates
    packets through the mac loopback and nothing preserves the order they were written in, so
    what the fabric sees is only the multiset of resident packets. Making every slot a
    permutation is the arithmetic that pins that multiset down: a destination appears once per
    slot, so exactly set_size times overall, and every right-side port ends up carrying the same
    share. Drawing each port an independent subset covers the range just as well but is even only
    on average, and its realised draw left some destinations used up to 5.6 times more often than
    others, which caps the rate at whichever right port drew the most.

    Two properties follow from the two random inputs. The permutation decides which destination
    sits at each position, so the values a port ends up with are scattered over the whole range
    rather than a contiguous block. The offsets are distinct, so a port never lands on the same
    position twice and its destinations are all different.

    Args:
        port_count (int): Number of traffic-generator ports.
        dip_count (int): Number of leaf destination IPs.
        set_size (int): Destinations assigned to each port.
        seed (str): Seed for the draw. Fixed by default, so two runs schedule identically and a
            throughput difference is a real regression rather than a different sample.

    Returns:
        list: One list of destination indices per port, each holding set_size distinct entries.
    """
    generator = random.Random(seed)
    dip_order = list(range(dip_count))
    generator.shuffle(dip_order)
    slot_offsets = generator.sample(range(dip_count), set_size)
    return [[dip_order[(port_index + offset) % dip_count] for offset in slot_offsets]
            for port_index in range(port_count)]


def _log_leaf_schedule_spread(port_schedules, dip_count, spine_tg):
    """
    Reports how evenly the schedule spreads packets over the right-side ports.

    The schedule is deterministic for a fixed seed, but port counts come from a live LLDP read,
    so an unexpected topology can still produce an uneven schedule. Logging the spread makes that
    visible in the run rather than only in offline analysis. The per-slot repeat count is a check
    on the construction, not a fan-in the fabric ever sees: slots are a way of laying the schedule
    out, and the loopback reorders the packets regardless.

    Args:
        port_schedules (list): Per-port destination index lists.
        dip_count (int): Number of leaf destination IPs.
        spine_tg (str): Spine traffic-generator alias, for log context.
    """
    usage = Counter(index for schedule in port_schedules for index in schedule)
    worst_slot_repeat = max(
        Counter(schedule[slot] for schedule in port_schedules).most_common(1)[0][1]
        for slot in range(len(port_schedules[0])))
    least_used, most_used = min(usage.values()), max(usage.values())
    logger.info(f"Leaf schedule spread on {spine_tg} | {len(usage)}/{dip_count} destinations "
                f"covered, each used {least_used}-{most_used} times, at most "
                f"{worst_slot_repeat} repeats within a slot")
    if most_used > least_used:
        logger.warning(f"Leaf schedule is uneven on {spine_tg} | busiest destination is used "
                       f"{most_used / least_used:.2f}x the quietest, so its right port carries "
                       f"that much more traffic and caps the aggregate rate")


def _resolve_leaf_schedule_shape(port_count, dip_count, calibrated_packets):
    """
    Derives how many destinations each port gets and how many packets each of those carries.

    Packets per port follow the chip's calibrated count rather than whatever the resident-packet
    budget would allow, because exceeding the calibration collapses the lossless class. Measured
    at 2 lanes per sub-port, 32 packets per port against a calibration of 12 drove average egress
    occupancy to 3570 against a 400 threshold, with pause frames and discards sustained across
    every sample; 1 lane at 16 against a calibration of 9 degraded from the second sample onwards;
    4 lanes at 32 against a calibration of 30 stayed at 30 and stayed clean. The budget is only a
    ceiling now, not the thing that sizes the schedule.

    Destinations per port are capped below the destination count so per-port sets can differ, so a
    chip calibrated for more packets than that cap sends several packets per destination instead.
    That keeps packets per port at or under the calibration, never above it.

    Args:
        port_count (int): Number of traffic-generator ports.
        dip_count (int): Number of leaf destination IPs.
        calibrated_packets (int): The chip's calibrated packets per port, both target and cap.

    Returns:
        tuple: (destinations per port, packets per destination).

    Raises:
        TestIssue: If no shape fits the destination list or the resident-packet budget.
    """
    budget = SPCXRAConsts.LEAF_RESIDENT_PACKET_BUDGET
    set_size = min(calibrated_packets, dip_count // SPCXRAConsts.LEAF_PORT_SET_DIVISOR)
    if set_size < 1:
        raise TestIssue(f"Cannot schedule leaf traffic | {port_count} ports over {dip_count} "
                        f"destinations at {calibrated_packets} packets per port leaves no room")
    packets_per_stream = max(1, calibrated_packets // set_size)
    resident = port_count * set_size * packets_per_stream
    if resident > budget:
        raise TestIssue(f"Leaf schedule exceeds the generator budget | {port_count} ports x "
                        f"{set_size} destinations x {packets_per_stream} packets = {resident} "
                        f"resident, budget={budget}, calibrated at {calibrated_packets} per port")
    return set_size, packets_per_stream


def _shrink_stream_payload(stream, payload_seed):
    """
    Reduces a stream's Raw payload to the seed string the generator expands it from.

    create_json_traffic_stream stores the payload already expanded to the full packet size, which
    across a few thousand scheduled streams makes the traffic JSON tens of megabytes. The
    generator's validate_raw_layer repeats the seed back up to packet_size using the same formula,
    so the packet on the wire is byte for byte the same.

    Args:
        stream (dict): Stream dictionary from create_json_traffic_stream.
        payload_seed (str): String the payload was expanded from, the player alias.

    Returns:
        dict: The same stream, with its Raw payload reduced to the seed.
    """
    raw_header = stream.get("headers", {}).get("Raw")
    if raw_header is not None:
        raw_header["load"] = payload_seed
    return stream


def build_leaf_dip_incremental_stream(players, spine_tg, conf_args, traffic_parameters, json_path,
                                      ip_protocol=PerfConsts.IP_PROTOCOL_UDP, num_packets=None):
    """
    Builds a single downstream stream whose destination the TG increments per packet.

    Args:
        players (dict): Test players mapping.
        spine_tg (str): Spine traffic-generator alias (left_tg in leaf scenarios).
        conf_args (dict): Configuration arguments dictionary.
        traffic_parameters (dict): Traffic parameters from get_traffic_parameters.
        json_path (str): Output path for the traffic JSON file.
        ip_protocol (str): IP protocol constant, UDP by default.
        num_packets (int): Packets per port. Defaults to the number of leaf destinations when None.

    Raises:
        TestIssue: If the DUT resolves no leaf destination IPs.
    """
    leaf_dst_ips = _resolve_leaf_dip_list(players, spine_tg, conf_args)
    start_dip = leaf_dst_ips[0]
    traffic_parameters["IP"]["dst"] = start_dip
    traffic_parameters["num_packets"] = (num_packets if num_packets is not None
                                         else len(leaf_dst_ips))
    logger.info(f"Building incremental leaf stream on {spine_tg} starting at {start_dip}")
    stream = create_json_traffic_stream(
        spine_tg, traffic_parameters, f"spine_to_leaf_incremental_dip_{start_dip}",
        ip_protocol=ip_protocol)
    create_json_traffic_file_with_stream_list(
        spine_tg, traffic_parameters, json_path, [stream])


def _resolve_leaf_dip_list(players, spine_tg, conf_args):
    """
    Resolves the NOS-specific downstream destination IPs for the leaf scenario.

    Args:
        players (dict): Test players mapping.
        spine_tg (str): Spine traffic-generator alias, used for error context only.
        conf_args (dict): Configuration arguments dictionary.

    Returns:
        list: One destination IP per DUT right port.

    Raises:
        TestIssue: If the DUT resolves no leaf destination IPs.
    """
    dut_cli = players['dut']['cli']
    dut_configuration = dut_cli.performance.get_device_configuration(conf_args=conf_args)
    leaf_dst_ips = dut_cli.performance.get_leaf_dip_list(dut_configuration, conf_args)
    if not leaf_dst_ips:
        raise TestIssue(f"No leaf destination IPs resolved for {spine_tg} | "
                        f"expected one per DUT right port, got {leaf_dst_ips}")
    logger.debug(f"Resolved {len(leaf_dst_ips)} leaf destinations | "
                 f"first={leaf_dst_ips[0]}, last={leaf_dst_ips[-1]}")
    return leaf_dst_ips


# ------------------------------------------------- link-flap validation helpers
#
# The link-flap tests pick a subset of DUT ports to admin-down. The DUT splits
# its ports into left-facing and right-facing halves. Whether the random pick
# happens to be symmetric across the two halves drives how the post-flap
# traffic should behave, and therefore how it should be validated:
#
#   * Equal counts on both sides: throughput, queue depth and DUT counters
#     match the no-flap baseline, so the standard thresholds apply.
#   * Unequal counts: the side with fewer up-ports caps the aggregate
#     throughput; per-port BW averages drop on the side with more up-ports
#     and queue depth grows on the side with more down-ports. The DUT also
#     legitimately accrues non-zero drop / error counters during the
#     asymmetric flap window, so the strict per-sample DUT counter check
#     is disabled.

def partition_dut_ports_by_side(
        ports: Iterable[str],
        left_ports: Iterable[str],
        right_ports: Iterable[str]) -> Tuple[List[str], List[str]]:
    """Split a port list into ``(left_subset, right_subset)``.

    Order within each returned list follows the input ``ports`` order.
    Ports that appear in neither side are silently dropped.
    """
    left_set = set(left_ports)
    right_set = set(right_ports)
    left = [p for p in ports if p in left_set]
    right = [p for p in ports if p in right_set]
    return left, right


def scale_bw_threshold(threshold: Dict, ratio: float) -> Dict:
    """Return a copy of a per-port-group BW threshold scaled by ``ratio``.

    Only dict-valued entries (the per-port-group sub-dicts like
    ``{TX: 0.92, RX: 0.92}``) are scaled; the optional ``VALIDATION_KEY``
    tuple is preserved as-is.
    """
    scaled = copy.deepcopy(threshold)
    for group_name, group_val in scaled.items():
        if isinstance(group_val, dict):
            scaled[group_name] = {k: v * ratio for k, v in group_val.items()}
    return scaled


def scale_tc_occ_threshold(threshold: Dict, ratio: float) -> Dict:
    """Return a copy of a TC-occupancy threshold relaxed by ``1/ratio``.

    Smaller imbalance ratios (more L/R asymmetry) produce larger
    occupancy thresholds. ``ratio <= 0`` returns the threshold unchanged
    so we never divide by zero.
    """
    if ratio <= 0:
        return copy.deepcopy(threshold)
    inverse = 1.0 / ratio
    return {k: int(v * inverse) if isinstance(v, (int, float)) else v
            for k, v in threshold.items()}


def compute_flap_validation_overrides(
        ports_down: Iterable[str],
        side_ports: Dict[str, Iterable[str]],
        bw_baseline: Dict,
        tc_occ_baseline: Dict) -> Dict:
    """Decide BW/TC thresholds and DUT counter-check policy for a flap result.

    Args:
        ports_down: DUT ports that were administratively down during the flap.
        side_ports: ``{"left_ports": [...], "right_ports": [...]}`` — the
            full L/R partition of DUT ports as reported by
            ``cli.performance.get_right_left_ports_dict()``.
        bw_baseline: baseline per-port-group BW threshold (e.g.
            ``SPCXRAConsts.DUT_TX_UTIL_AUTO_TH_DICT``).
        tc_occ_baseline: baseline TC-occupancy threshold (e.g.
            ``PerfConsts.OCC_TH_DICT``).

    Returns:
        A dict with keys ``bw_threshold``, ``tc_occ_threshold``,
        ``run_validate_counters`` and ``step``, ready to splat into the
        ``_SpcXRATestBase._validate`` kwargs.
    """
    left_ports = side_ports["left_ports"]
    right_ports = side_ports["right_ports"]
    left_down, right_down = partition_dut_ports_by_side(
        ports_down, left_ports, right_ports)
    l_down_n = len(left_down)
    r_down_n = len(right_down)
    l_up = len(left_ports) - l_down_n
    r_up = len(right_ports) - r_down_n

    if l_down_n == r_down_n:
        return {
            "bw_threshold": bw_baseline,
            "tc_occ_threshold": tc_occ_baseline,
            "run_validate_counters": True,
            "step": (f"Verifying traffic after flap "
                     f"(symmetric: L_down={l_down_n}, R_down={r_down_n})"),
        }

    bottleneck_ratio = (
        min(l_up, r_up) / max(l_up, r_up) if max(l_up, r_up) > 0 else 0.0)
    return {
        "bw_threshold": scale_bw_threshold(bw_baseline, bottleneck_ratio),
        "tc_occ_threshold": scale_tc_occ_threshold(
            tc_occ_baseline, bottleneck_ratio),
        "run_validate_counters": False,
        "step": (f"Verifying traffic after flap (asymmetric: "
                 f"L_down={l_down_n}, R_down={r_down_n}, "
                 f"bottleneck_ratio={bottleneck_ratio:.3f}, "
                 f"skipping DUT counter validation)"),
    }
