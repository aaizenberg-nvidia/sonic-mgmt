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

The LanesConfig dataclass and the test classes live in test_spcx_ra.py.
This module uses duck typing (getattr(cls, "CONFIG", None)) so it has no
dependency on the dataclass.
"""
import copy
import logging
import os
from typing import Dict, Iterable, List, Tuple

import allure
import pytest

from ngts.cli_wrappers.nvue.nvue_cli import NvueCli
from ngts.constants.constants import BugHandlerConst, InfraConst
from ngts.constants.performance_constants import MongoDbConsts, PerfConsts
from ngts.helpers.performance.performance_db_helpers import (
    add_test_mongo_metadata, get_perf_test_name)
from ngts.helpers.performance.performance_setup_helpers import (
    allure_attach_performance_conf_context, apply_test_configuration,
    get_topology_obj, restore_basic_configuration, save_base_configuration,
    skip_test_on_unsupported_os)
from ngts.helpers.performance.traffic_helpers import (
    create_json_traffic_file, create_json_traffic_file_with_stream_list,
    create_json_traffic_stream)

logger = logging.getLogger()

TESTS_SCENARIO = "spcx_ra"


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
    try:
        with allure.step('Save Players initial Configuration'):
            save_base_configuration(players)
        with allure.step("Apply Test configuration on all Players"):
            apply_test_configuration(
                players, scenario=TESTS_SCENARIO, conf_args=conf_args)
        with allure.step("Allure: attach conf_args and DUT applied NVUE YAML"):
            allure_attach_performance_conf_context(players, conf_args)
        yield is_ipv6
    finally:
        with allure.step('Restore Base Configuration on all Players'):
            restore_basic_configuration(players)


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
        request.instance, config, is_ipv6,
        players, engines, power_thresholds_by_chip_type, chip_type)
    yield


def _populate_test_state(instance, config, is_ipv6, players, engines,
                         power_thresholds_by_chip_type, chip_type):
    """Attach the per-test runtime state to a TestSpcXRA_Nx_lanes instance.

    The static configuration is already on the class as ``instance.CONFIG``;
    this function only attaches runtime values (players, engines, conf_args,
    pre-built traffic JSONs, ...).

    Args:
        instance: The pytest test-class instance to populate.
        config: The ``LanesConfig`` pinned to the class.
        is_ipv6: Whether the test runs under IPv6.
        players: Test players mapping.
        engines: Engines mapping (DUT, traffic generators).
        power_thresholds_by_chip_type: Power-consumption threshold for the
            DUT's chip type.
        chip_type: Detected chip-type identifier.
    """
    instance.players = players
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
    instance.traffic_jsons = get_spcx_ra_spine_traffic(players, instance.conf_args)


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
