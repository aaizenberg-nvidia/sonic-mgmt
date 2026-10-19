import ipaddress
import json
import os
import re
import tempfile
import time
import allure
import logging
import pytest
from ngts.constants.constants import InfraConst, BugHandlerConst
from ngts.constants.performance_constants import (PerfConsts, MongoDbConsts, MRCConsts,
                                                  ValidationConsts, NoSrhPerfConsts)
from ngts.helpers.config_db_utils import save_config_db_json
from ngts.helpers.performance.performance_setup_helpers import (ValidationConfig, run_traffic,
                                                                stop_traffic, configure_mloops,
                                                                run_validation, add_test_mongo_metadata,
                                                                set_shaper_on_traffic_gen,
                                                                skip_performance_test_conditionally,
                                                                apply_test_configuration,
                                                                call_performance_function_with_threads,
                                                                restore_basic_configuration,
                                                                save_base_configuration)
import threading
from ngts.helpers.performance.performance_db_helpers import get_perf_test_name
from ngts.helpers.performance.traffic_helpers import generate_mac_range
from functools import partial
from ngts.performance_tests.srv6.leaf.conftest import get_bisection_traffic
from ngts.performance_tests.srv6.utils.srv6_workloads import create_no_srh_stream
from infra.tools.exceptions.test_issue import TestIssue

logger = logging.getLogger()

TESTS_SCENARIO = "SRv6 No-SRH Shift-Left Performance"

# All port splits across all chip types (union), in LANES PER PORT semantics:
#   1 = 1 lane per port (max breakout — smallest port, most ports per cage)
#   2 = 2 lanes per port
#   4 = 4 lanes per port
# Unsupported (chip, split) combos are skipped at runtime via
# NoSrhPerfConsts.PORT_SPLITS_BY_CHIP_TYPE.
ALL_PORT_SPLITS = [(1, 1), (2, 2), (4, 4)]

# --- Per-test packet-size selection ---
# Each list controls the parametrize values for one test. Default is
# ``[PACKET_SIZE_MIN_SENTINEL]`` -- a single case whose size is resolved at
# test time to the per-chip minimum of the test's scenario (via
# NoSrhPerfConsts.ALL_PORTS_MIN_PACKET_SIZE_BY_CHIP_TYPE or
# NoSrhPerfConsts.SINGLE_CAGE_MIN_PACKET_SIZE_BY_CHIP_TYPE). To sweep every
# supported size, replace with ``NoSrhPerfConsts.PACKET_SIZE_SWEEP_LIST``.
# Custom subsets are fine too (e.g. ``[512, 1024, 2048]``).
PACKET_SIZE_MIN_SENTINEL = "MIN"

CHOSEN_PACKET_SIZES_ENTROPY_COMPARISON = [PACKET_SIZE_MIN_SENTINEL]
CHOSEN_PACKET_SIZES_SINGLE_CAGE_BASELINE = [PACKET_SIZE_MIN_SENTINEL]
CHOSEN_PACKET_SIZES_SPLIT_SHARING_WITH_ACL = [PACKET_SIZE_MIN_SENTINEL]
CHOSEN_PACKET_SIZES_SPLIT_SHARING_NO_ACL = [PACKET_SIZE_MIN_SENTINEL]


# ---------------------------------------------------------------------------
# Fixture overrides -- these take precedence over the leaf/conftest.py auto use
# fixtures for tests in this module, allowing different port split configs.
# ---------------------------------------------------------------------------

@pytest.fixture(scope='class')
def port_split(request, chip_type):
    """Parameterized port split (left, right) tuple injected via pytest.mark.parametrize indirect.

    Unsupported chip / split combinations are skipped here, before any class fixture configures the
    switches for a split the chip cannot run.
    """
    split_left, split_right = request.param
    skip_performance_test_conditionally(
        chip_type not in NoSrhPerfConsts.SUPPORTED_CHIP_TYPES,
        f"No-SRH test not supported on {chip_type}")
    skip_performance_test_conditionally(
        request.param not in NoSrhPerfConsts.PORT_SPLITS_BY_CHIP_TYPE.get(chip_type, []),
        f"Port split ({split_left}x, {split_right}x) not supported on {chip_type}")
    return request.param


@pytest.fixture(scope='class', autouse=True)
def shaper_value(basic_setup_configuration, shaper_value):
    """Per-split wrapper of srv6/conftest.py's shaper_value.

    The shared fixture pulls basic_setup_configuration via getfixturevalue, so pytest caches it once for
    the class regardless of the split and replays the first split's skip or setup failure for every
    other split. Depending on basic_setup_configuration statically keys it by port split.
    """
    return shaper_value


def _resolve_packet_size(chip_type, packet_size, scenario):
    """Resolve the ``PACKET_SIZE_MIN_SENTINEL`` sentinel to the per-chip minimum.

    Each test is parametrized over a ``CHOSEN_PACKET_SIZES_*`` list. When the
    list contains only ``PACKET_SIZE_MIN_SENTINEL`` (the default), the
    parametrized value must be resolved at test time to the per-chip minimum
    for the test's scenario. Concrete int values pass through unchanged.

    Args:
        chip_type: Chip family key (e.g. ``"SPC4"``, ``"SPC5"``, ``"SPC6"``).
        packet_size: The current parametrized value -- either
            ``PACKET_SIZE_MIN_SENTINEL`` or a concrete int (bytes).
        scenario: ``"all_ports"`` for the bisection entropy test or
            ``"single_cage"`` for the single-cage baseline / split-sharing
            sub-tests.

    Returns:
        int: Packet size in bytes.

    Raises:
        ValueError: If ``scenario`` is not ``"all_ports"`` or ``"single_cage"``.
        KeyError: If ``chip_type`` is missing from the relevant
            ``*_BY_CHIP_TYPE`` map (caller should guard upstream via
            ``NoSrhPerfConsts.SUPPORTED_CHIP_TYPES``).
    """
    if packet_size != PACKET_SIZE_MIN_SENTINEL:
        return packet_size
    if scenario == "all_ports":
        return NoSrhPerfConsts.ALL_PORTS_MIN_PACKET_SIZE_BY_CHIP_TYPE[chip_type]
    if scenario == "single_cage":
        return NoSrhPerfConsts.SINGLE_CAGE_MIN_PACKET_SIZE_BY_CHIP_TYPE[chip_type]
    raise ValueError(f"Unknown scenario: {scenario!r}")


def _speed_from_breakout_mode(mode_str):
    """Parse a SONiC breakout mode string and return the per-port speed in G.

    Examples: "4x200G[100G]" -> 200, "8x100G[50G]" -> 100, "1x400G" -> 400.
    """
    match = re.match(r'\d+x(\d+)G', mode_str)
    if not match:
        raise ValueError(f"Cannot parse SONiC breakout mode: {mode_str!r}")
    return int(match.group(1))


def _rewrite_breakout_in_config_db(config_db, platform_json, target_mode):
    """Rewrite PORT and BREAKOUT_CFG tables in-place for target breakout.

    For each cage parent whose platform.json supports `target_mode`, replace
    that cage's PORT entries with new sub-ports per the breakout mode and set
    BREAKOUT_CFG[cage].brkout_mode = target_mode. Cages that don't support
    target_mode are left as-is (keep existing entries).

    Tables that reference per-port names (CABLE_LENGTH, BUFFER_*, QUEUE,
    PORT_QOS_MAP) are dropped so the subsequent QoS reload regenerates them
    for the new port layout — avoids the libyang validation traps that bit
    `config interface breakout`.
    """
    existing_ports = config_db.get("PORT", {})
    existing_breakout = config_db.get("BREAKOUT_CFG", {})
    new_ports = {}
    new_breakout = {}
    speed_kbps = str(_speed_from_breakout_mode(target_mode) * 1000)
    n_subports = int(re.match(r'(\d+)x', target_mode).group(1))

    for cage_parent, info in platform_json.get("interfaces", {}).items():
        breakout_modes = info.get("breakout_modes", {})
        cage_idx = int(cage_parent.replace("Ethernet", ""))
        num_lanes = len(info["lanes"].split(","))

        if target_mode not in breakout_modes:
            # Cage can't support target mode (e.g., service ports) — preserve.
            if cage_parent in existing_breakout:
                new_breakout[cage_parent] = existing_breakout[cage_parent]
            for offset in range(num_lanes):
                pname = f"Ethernet{cage_idx + offset}"
                if pname in existing_ports:
                    new_ports[pname] = existing_ports[pname]
            continue

        aliases = breakout_modes[target_mode]
        lanes = info["lanes"].split(",")
        indexes = info["index"].split(",")
        lanes_per_subport = len(lanes) // n_subports
        new_breakout[cage_parent] = {"brkout_mode": target_mode}

        # Use the cage parent's existing entry as a template for shared fields
        # (admin_status, mtu, fec, autoneg). Fields that change per breakout
        # (lanes, alias, speed, index) are recomputed below.
        template = existing_ports.get(cage_parent, {})
        for i, alias in enumerate(aliases):
            sub_offset = i * lanes_per_subport
            new_pname = f"Ethernet{cage_idx + sub_offset}"
            sub_lanes = lanes[sub_offset:sub_offset + lanes_per_subport]
            new_ports[new_pname] = {
                "lanes": ",".join(sub_lanes),
                "alias": alias,
                "speed": speed_kbps,
                "index": indexes[sub_offset],
                "admin_status": template.get("admin_status", "up"),
                "mtu": template.get("mtu", "9100"),
                "fec": template.get("fec", "rs"),
                "autoneg": template.get("autoneg", "off"),
            }

    config_db["PORT"] = new_ports
    config_db["BREAKOUT_CFG"] = new_breakout
    # Filter port-referenced tables to keep only entries for ports that still
    # exist after breakout. Don't drop the tables wholesale — load_tg_custom_buffer_conf
    # (and other downstream merges) expect the keys to be present.
    kept_ports = set(new_ports.keys())
    # Tables keyed directly by port name
    for table in ("BUFFER_PORT_INGRESS_PROFILE_LIST",
                  "BUFFER_PORT_EGRESS_PROFILE_LIST", "PORT_QOS_MAP"):
        if table in config_db:
            config_db[table] = {k: v for k, v in config_db[table].items()
                                if k in kept_ports}
    # Tables keyed by "Ethernet0|N" (PG/queue index)
    for table in ("BUFFER_PG", "BUFFER_QUEUE", "QUEUE"):
        if table in config_db:
            config_db[table] = {k: v for k, v in config_db[table].items()
                                if k.split("|", 1)[0] in kept_ports}
    # CABLE_LENGTH has nested {set_name: {port: length}} structure
    if "CABLE_LENGTH" in config_db:
        for cable_set, port_lengths in list(config_db["CABLE_LENGTH"].items()):
            config_db["CABLE_LENGTH"][cable_set] = {
                k: v for k, v in port_lengths.items() if k in kept_ports}


def _apply_sonic_breakout_via_config_db(player, chip_type, split_left):
    """Modify /etc/sonic/config_db.json's PORT/BREAKOUT_CFG for target split, reload.

    Must be called AFTER apply_sku (which writes SKU defaults to
    /etc/sonic/config_db.json) and BEFORE get_configuration_file (so the
    Jinja merge later sees the post-breakout port layout when it calls
    `get_right_left_ports_dict`). No-op on Cumulus and when target equals
    the SKU default. Avoids the libyang CABLE_LENGTH bug that breaks
    sequential `config interface breakout` CLI calls — single cold reload
    of a complete config_db is what SONiC tolerates reliably.
    """
    if player.get("is_cumulus", False):
        return
    target_mode = NoSrhPerfConsts.SONIC_BREAKOUT_MODE_BY_CHIP_AND_SPLIT.get(
        chip_type, {}).get(split_left)
    if not target_mode:
        logger.info(
            f"No SONiC breakout mode defined for {chip_type} {split_left}x; "
            f"leaving DUT breakout untouched")
        return
    cli = player['cli']
    engine = player['engine']

    # Use cli.general.get_config_db() rather than a raw `cat` — the canonical
    # helper handles netmiko's output truncation for large config_db.json
    # files (~1.3MB) by appending '; echo' and disabling print_output.
    config_db = cli.general.get_config_db()
    breakout_cfg = config_db.get("BREAKOUT_CFG", {})
    # Only cages running one of this chip's breakout modes decide the short-circuit: aux / mgmt / leftover
    # cages keep other modes and are left untouched by _rewrite_breakout_in_config_db anyway.
    cage_target_modes = set(
        NoSrhPerfConsts.SONIC_BREAKOUT_MODE_BY_CHIP_AND_SPLIT.get(chip_type, {}).values())
    cage_modes_present = {b.get("brkout_mode") for b in breakout_cfg.values()} & cage_target_modes
    if cage_modes_present == {target_mode}:
        logger.info(f"All cages already at {target_mode}; skipping config_db breakout")
        return

    platform = engine.run_cmd(
        "sonic-cfggen -H -v DEVICE_METADATA.localhost.platform").strip()
    hwsku = engine.run_cmd(
        "sonic-cfggen -H -v DEVICE_METADATA.localhost.hwsku").strip()
    platform_json = json.loads(engine.run_cmd(
        f"cat /usr/share/sonic/device/{platform}/{hwsku}/platform.json ; echo",
        print_output=False))

    logger.info(f"Rewriting config_db PORT/BREAKOUT_CFG for {target_mode}")
    _rewrite_breakout_in_config_db(config_db, platform_json, target_mode)
    # save_config_db_json's default local path is /tmp/config_db.json — running
    # this in parallel threads (one per player) races on that single file:
    # one thread's `os.remove(json_path)` deletes the file another thread is
    # about to SCP. Use a unique temp file per call to avoid the race.
    with tempfile.NamedTemporaryFile(mode='w', suffix='_config_db.json',
                                     delete=False) as tf:
        local_path = tf.name
    try:
        save_config_db_json(engine, config_db, json_path=local_path,
                            remove_json_path=False)
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)
    cli.general.reload_configuration(force=True)
    cli.general.verify_dockers_are_up()


def _pick_single_cage_ports(engine, candidate_ports, n_ports, exclude_cages=None):
    """Pick n_ports adjacent 1x sub-ports from a single physical cage.

    Groups `candidate_ports` by their owning cage parent (per platform.json
    lane layout) and returns the first cage with at least n_ports members,
    sliced to its first n_ports by Ethernet index. Used by the split-sharing
    sub-tests to guarantee that ingress and egress traffic lands on distinct
    physical cages while all remaining sub-ports of each cage stay idle.

    Args:
        engine: DUT engine with `run_cmd`.
        candidate_ports: List of "EthernetN" sub-port names to choose from
            (typically one side of get_right_left_ports_dict()).
        n_ports: Number of adjacent sub-ports to return from the chosen cage.
        exclude_cages: Optional set of cage-parent Ethernet indices to skip
            (used to force ingress-cage != egress-cage).

    Returns:
        (cage_parent_idx, [EthernetN, ...]) — the chosen cage parent's index
        and the n_ports sub-port names on that cage, sorted by Ethernet index.

    Raises:
        TestIssue: if no cage in candidate_ports has n_ports members.
    """
    exclude_cages = exclude_cages or set()
    platform = engine.run_cmd(
        "sonic-cfggen -H -v DEVICE_METADATA.localhost.platform").strip()
    hwsku = engine.run_cmd(
        "sonic-cfggen -H -v DEVICE_METADATA.localhost.hwsku").strip()
    platform_json = json.loads(engine.run_cmd(
        f"cat /usr/share/sonic/device/{platform}/{hwsku}/platform.json ; echo",
        print_output=False))

    port_to_cage = {}
    for cage_parent, info in platform_json.get("interfaces", {}).items():
        cage_idx = int(cage_parent.replace("Ethernet", ""))
        lanes_per_cage = len(info["lanes"].split(","))
        for offset in range(lanes_per_cage):
            port_to_cage[cage_idx + offset] = cage_idx

    by_cage = {}
    for p in candidate_ports:
        idx = int(p.replace("Ethernet", ""))
        cage = port_to_cage.get(idx)
        if cage is None or cage in exclude_cages:
            continue
        by_cage.setdefault(cage, []).append(p)

    for cage in sorted(by_cage):
        members = sorted(by_cage[cage],
                         key=lambda x: int(x.replace("Ethernet", "")))
        if len(members) >= n_ports:
            return cage, members[:n_ports]

    raise TestIssue(
        f"No single cage has {n_ports} adjacent sub-ports in "
        f"{candidate_ports} (excluded cages: {exclude_cages})")


def _parallel_per_player(players, aliases, fn, label):
    """Run fn(player_dict) in parallel for each alias.

    Used for steps that need per-player parallelism but don't fit
    call_performance_function_with_threads (e.g. cli.general.* calls or
    local helpers). Mirrors what the standard apply does inside each
    player's thread — keeps every SSH session busy with its own work,
    avoiding netmiko's prompt read_timeout on idle sessions.
    """
    threads = []
    errors = []

    def _do(alias):
        try:
            fn(players[alias])
        except Exception as e:
            errors.append((alias, e))

    for alias in aliases:
        t = threading.Thread(target=_do, args=(alias,), name=f"{label}-{alias}")
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    if errors:
        first_alias, first_exc = errors[0]
        for alias, exc in errors[1:]:
            logger.error(f"{label} also failed on {alias}: {exc}")
        raise RuntimeError(f"{label} failed on {first_alias}: {first_exc}") from first_exc


def _apply_test_config_with_sonic_breakout(players, conf_args, chip_type, split_left):
    """Custom apply flow for the no-SRH test on SONiC.

    Replicates the steps of apply_configuration_file (sonic_performance_clis.py)
    but inserts a config_db breakout step between `apply_sku` and
    `get_configuration_file`. This ensures the SRv6 INTERFACE config rendered
    by the Jinja template references the POST-breakout port names rather than
    the SKU-default ones.

    Each multi-player step is run in parallel via call_performance_function_with_threads
    (or _parallel_per_player for cli.general.* and local helpers), mirroring the
    threading model the standard apply_test_configuration uses. Running them
    sequentially per-player leaves SSH sessions idle long enough to trip
    netmiko's prompt-timeout on the next reload command.

    On Cumulus, callers should use the standard apply_test_configuration —
    the NVUE template applies breakout declaratively in a single nv config
    apply, no surgical injection needed.
    """
    aliases = PerfConsts.PERF_SETUP_PLAYERS_ALIASES
    tg_aliases = PerfConsts.PERF_SETUP_TG_ALIASES

    call_performance_function_with_threads(
        players, players_aliases=aliases, action="apply SKU",
        performance_clis_function_name="apply_sku",
        performance_clis_function_args=(
            conf_args["hwsku"], conf_args["dut"], conf_args["chip_type"]),
        step="no_srh - apply_sku")

    call_performance_function_with_threads(
        players, players_aliases=tg_aliases, action="load QoS on TG",
        performance_clis_function_name="load_qos_config_on_tg",
        performance_clis_function_args=(),
        step="no_srh - load_qos_config_on_tg")

    # Apply the breakout on every player in parallel: DUT and both TGs must
    # be at the same split — physical link won't come up if the DUT cage
    # has lanes 0,1 wired as 200G and the TG cage still has lane 0 as 100G.
    with allure.step(f"Apply SONiC port breakout to {split_left}x on all players"):
        _parallel_per_player(
            players, aliases,
            lambda p: _apply_sonic_breakout_via_config_db(p, chip_type, split_left),
            label="breakout")

    # apply_sku populated self.connected_ports/self.unconnected_ports from the
    # SKU's pre-breakout default. After breakout the live port layout changed,
    # so these lists contain port names that no longer exist. Re-derive from
    # each player's post-breakout config_db before get_configuration_file
    # renders the Jinja template (which uses connected_ports/unconnected_ports).
    with allure.step("Refresh connected/unconnected ports after breakout"):
        def _refresh_ports(player):
            perf = player['cli'].performance
            cfg = player['cli'].general.get_config_db()
            perf.update_connected_unconnected_ports(
                conf_args["hwsku"], cfg, conf_args["dut"], conf_args["chip_type"])
        _parallel_per_player(players, aliases, _refresh_ports, label="refresh-ports")

    call_performance_function_with_threads(
        players, players_aliases=aliases, action="get configuration file",
        performance_clis_function_name="get_configuration_file",
        performance_clis_function_args=(conf_args["scenario"], conf_args),
        step="no_srh - get_configuration_file")

    call_performance_function_with_threads(
        players, players_aliases=tg_aliases, action="disable IM on TG",
        performance_clis_function_name="disable_im_on_tg",
        performance_clis_function_args=(),
        step="no_srh - disable_im_on_tg")

    players['dut']['cli'].performance.load_qos_config_on_dut()

    with allure.step("Reload SKU and Test configuration on all players"):
        def _reload(player):
            player['cli'].general.reload_configuration(force=True)
            player['cli'].general.verify_dockers_are_up()
        _parallel_per_player(players, aliases, _reload, label="reload")

    if chip_type == "SPC6":
        with allure.step("Bounce admin state on DUT ports "
                         "(WA: CMIS DEINIT after config qos reload on SN6600_LD OSFP 8X)"):
            dut_player = players['dut']
            dut_perf = dut_player['cli'].performance
            dut_engine = dut_player['engine']
            dut_ports = list(dut_perf.connected_ports) + list(dut_perf.unconnected_ports)
            for port in dut_ports:
                dut_engine.run_cmd(f"sudo config interface shutdown {port}")
            time.sleep(2)
            for port in dut_ports:
                dut_engine.run_cmd(f"sudo config interface startup {port}")
            time.sleep(30)

    for alias in aliases:
        players[alias]['cli'].general.install_traffic_generator()

    # Populate post-reload state on EACH player (DUT and TGs). The standard
    # apply_configuration_file does this inside each player's thread; my flow
    # needs to do it explicitly per player. `set_class_vars` re-derives
    # connected_ports, sonic_to_sdk_ports_dict, port_groups, etc. from the
    # current (post-reload) config_db — same effect as the per-player block
    # at the end of apply_configuration_file.
    for alias in aliases:
        with allure.step(f"Populate post-reload class vars on {alias}"):
            players[alias]['cli'].performance.set_class_vars()
            players[alias]['cli'].performance.add_ports_connectivity_to_dut(conf_args)


@pytest.fixture(scope='class')
def conf_args(chip_type, players, port_split):
    """conf_args override with parameterized port split for no-SRH tests.

    `port_split` is in LANES-PER-PORT semantics (matches the SONiC breakout
    mode key). On Cumulus, NVUE Jinja templates and get_sorted_ports_list()
    consume `conf_args["split_left"]` as SUB-PORTS-PER-CAGE (they emit
    `{{ split_left }}x: {}`). Translate at the fixture boundary so downstream
    consumers see the semantic they expect:
      cumulus_split = cage_lanes // lanes_per_port
    where cage_lanes is derived from the chip's max-breakout entry in
    SONIC_BREAKOUT_MODE_BY_CHIP_AND_SPLIT (single source of truth). The
    per-port Cumulus speed is taken from the same breakout-mode string.
    On SONiC split_left/right pass through as lanes-per-port and ``speed`` (Gbps)
    is the per-port speed of the split, from the same breakout-mode string.
    ``port_speed_ratio`` is the per-port speed relative to the chip's lowest
    split; it scales the number of injected packets.
    """
    sku = MRCConsts.HWSKU_BY_CHIP_TYPE[chip_type]["leaf"]
    is_cumulus = players["dut"].get("is_cumulus", False)
    split_left_lanes, split_right_lanes = port_split
    breakout_modes = NoSrhPerfConsts.SONIC_BREAKOUT_MODE_BY_CHIP_AND_SPLIT.get(
        chip_type, {})
    if not breakout_modes:
        raise TestIssue(f"No breakout-mode table for {chip_type}")
    per_port_g = _speed_from_breakout_mode(breakout_modes[split_left_lanes])
    lowest_split_port_g = _speed_from_breakout_mode(breakout_modes[min(breakout_modes)])
    if is_cumulus:
        cage_lanes = max(breakout_modes.keys())
        split_left = cage_lanes // split_left_lanes
        split_right = cage_lanes // split_right_lanes
        speed = str(per_port_g * 1_000_000)  # NVUE speed param is Kbps
    else:
        split_left = split_left_lanes
        split_right = split_right_lanes
        # The DUT validator and the shaper use this as the port speed, so it must follow the split
        # (the hwsku default is the lowest-split speed and under-reports 2x/4x ports).
        speed = per_port_g
    return {
        "is_ipv6": True,
        "packet_size": 4096,
        "scenario": "srv6",
        "split_left": split_left,
        "split_right": split_right,
        "speed": speed,
        "port_speed_ratio": per_port_g / lowest_split_port_g,
        "hwsku": sku,
        "dut_mac": players['dut']['cli'].performance.mac,
        "dut": "leaf",
        "chip_type": chip_type,
        "downlinks_tg": PerfConsts.RIGHT_TG_ALIAS,
        "template_path": os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                      PerfConsts.DEFAULT_PERF_TEMPLATES_DIR, "srv6", "sonic"),
    }


@pytest.fixture(scope='class')
def basic_setup_configuration(players, conf_args, chip_type, port_split):
    """Setup/teardown override for no-SRH tests with custom port split.

    On Cumulus the standard apply_test_configuration is used — the NVUE
    template applies breakout declaratively. On SONiC we run a custom apply
    flow that inserts a config_db breakout step between apply_sku and
    get_configuration_file, since SONiC's apply_sku resets breakout to SKU
    defaults and the Jinja template renders against the live (post-apply_sku)
    port layout.
    """
    split_left, _ = port_split
    is_cumulus = players['dut'].get("is_cumulus", False)
    with allure.step('Save Players initial Configuration'):
        save_base_configuration(players)
    primary_exc = None
    try:
        if is_cumulus:
            with allure.step("Apply Test configuration on all Players"):
                apply_test_configuration(players, scenario=conf_args["scenario"],
                                         conf_args=conf_args)
        else:
            with allure.step(f"Apply Test configuration on all Players "
                             f"(with SONiC {split_left}x breakout)"):
                _apply_test_config_with_sonic_breakout(
                    players, conf_args, chip_type, split_left)
        yield
    except Exception as e:
        primary_exc = e
        raise
    finally:
        with allure.step('Restore Base Configuration on all Players'):
            try:
                restore_basic_configuration(players)
            except Exception as restore_exc:
                # A restore failure on top of an already-failed setup/test would
                # otherwise replace the primary failure as the reported exception.
                # Log it and let the primary exception keep propagating; if there
                # was no primary failure, the restore failure is the real error
                # and must still be raised.
                if primary_exc is not None:
                    logger.error(
                        "restore_basic_configuration failed during teardown "
                        "while handling a prior setup/test failure "
                        "(%s: %s); primary failure takes precedence",
                        type(restore_exc).__name__, restore_exc)
                else:
                    raise


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("port_split", ALL_PORT_SPLITS, indirect=True)
class TestSRv6NoSrhPerf:
    """SRv6 no-SRH shift-left performance test.

    Validates that the optimized ACL path (1 lookup + 1 HW action for END/END+USD
    packets without SRH) delivers equivalent performance for high-entropy and
    low-entropy DIPv6 flows, across multiple packet sizes and port split configs.

    Feature #4894969, customer: OpenAI.
    Supported chips: SPC4, SPC5, SPC6.
    Port splits (LANES PER PORT):
      SPC4: 2x (2 lanes/port → 2 ports/cage × 200G),
            4x (4 lanes/port → 1 port/cage × 400G).
      SPC5/SPC6: 1x (1 lane/port → 8 ports/cage × 100G),
                 4x (4 lanes/port → 2 ports/cage × 400G).
    """

    @pytest.fixture(autouse=True)
    def setup(self, players, engines, cli_objects, is_ipv6, chip_type,
              conf_args, port_split, power_thresholds_by_chip_type, shaper_value,
              basic_setup_configuration):
        self.players = players
        self.engines = engines
        self.cli_objects = cli_objects
        self.engine = engines['dut']
        self.cli_object = self.cli_objects['dut']
        self.tg_cli_object = self.cli_objects[PerfConsts.LEFT_TG_ALIAS]
        self.ip = InfraConst.IPV6 if is_ipv6 else InfraConst.IPV4
        self.chip_type = chip_type
        self.scenario = "srv6"
        self.conf_args = conf_args
        self.port_split = port_split
        self.hwsku = conf_args["hwsku"]
        self.power_thresholds_by_chip_type = power_thresholds_by_chip_type
        self.shaper_value = shaper_value
        self.dut_interfaces_ipv6_configuration_dict = (
            self.cli_object.performance.get_dut_interfaces_ipv6_configuration())
        self.vlan_interface_configuration_dict = (
            self.tg_cli_object.performance.get_tg_interfaces_vlan_configuration())
        self.cli_object.performance.configure_interfaces_mac_neighbor(
            self.vlan_interface_configuration_dict)
        self._install_srv6_variant_neighbors()

    def _install_srv6_variant_neighbors(self):
        """Pre-install neighbor entries for the SRv6 no-SRH variant post-shift
        addresses, so high-entropy DIPs forward at line rate.

        configure_interfaces_mac_neighbor installs one neighbor per port at
        `fcbb:bbbb:N::`. When this test sends varied DIPv6
        (`fcbb:bbbb:1:N::M` for M=1..num_packets-1), the DUT's
        End+next compressed SID SRv6 endpoint shifts them to `fcbb:bbbb:N::M:0` —
        addresses without neighbor entries. Without this helper, the DUT
        triggers unanswered NS for each variant and packets are dropped,
        leaving only the base DIP sustaining in the mloop; TX scales as
        1/N_unique_DIPs (we measured 10% with 10 variants).

        This helper parses the live base-neighbor table to discover the
        per-port (MAC, iface) tuple installed by configure_interfaces_mac_neighbor,
        then installs neighbor entries for all variants at
        `fcbb:bbbb:N::M:0` pointing to the same MAC/iface.
        """
        # Must match the num_packets the workload actually sends — otherwise
        # we install too few variant neighbors and the unfixed M values drop.
        # The workload (create_no_srh_stream) uses NO_SRH_NUM_DATA_PACKETS
        # as its default, so use the same here.
        num_packets = NoSrhPerfConsts.NO_SRH_NUM_DATA_PACKETS
        if num_packets <= 1:
            return
        # Replicate the (port -> ipv6 address, MAC) mapping that
        # configure_interfaces_mac_neighbor builds, so we don't depend on
        # parsing the live `ip -6 neigh show` table (which had timing/race
        # issues — some base entries weren't visible yet when we parsed,
        # leaving their variants uninstalled).
        dut_ports = self.cli_object.performance.get_dut_ports()
        mac_range = generate_mac_range("00:11:22:33:44:55", count=len(dut_ports))
        cmd_list = []
        for idx, port in enumerate(dut_ports):
            port_ipv6_address = self.dut_interfaces_ipv6_configuration_dict[port].replace(
                "aaaa", "bbbb")
            try:
                base = ipaddress.IPv6Address(port_ipv6_address)
            except ValueError:
                continue
            mac = mac_range[idx]
            # Variant M ends up at base + (M << 16) after the End+next compressed SID
            # shift (M occupies the 7th 16-bit group). Install neighbors for
            # variants the test will actually generate: M=1..num_packets-1.
            for m in range(1, num_packets):
                variant_addr = base + (m << 16)
                cmd_list.append(
                    f"sudo ip -6 neigh replace {variant_addr} "
                    f"lladdr {mac} dev {port}")
        if cmd_list:
            self.engine.run_cmd_set(cmd_list)

    def _clear_counters(self, label: str) -> None:
        """Clear DUT port and queue counters right before a traffic run.

        The SDK-level force clear must precede ``clear_counters``, otherwise
        the sonic-clear baseline is snapshotted on pre-reset values. Callers
        pass ``CLEAR_COUNTERS=False`` to the traffic validator so these
        counters span the whole traffic run.

        Args:
            label (str): Run identifier shown in the allure step title.

        Returns:
            None
        """
        with allure.step(f"[{label}] Clear counters"):
            self.cli_object.interface.clear_counters_by_force()
            self.cli_object.interface.clear_counters()
            self.cli_object.interface.clear_queue_counters()

    @pytest.mark.FR_4867615()
    @pytest.mark.parametrize("packet_size", CHOSEN_PACKET_SIZES_ENTROPY_COMPARISON)
    def test_no_srh_entropy_comparison(self, request, packet_size):
        """Run high and low entropy at the same packet size and compare performance.

        For each packet size:
        1. Run all-ports traffic with high entropy (many different DIPv6 lower 64 bits)
        2. Capture throughput, latency, SB occupancy
        3. Run all-ports traffic with low entropy (same DIPv6 lower 64 bits)
        4. Capture throughput, latency, SB occupancy
        5. Assert both pass line-rate / zero-drop thresholds
        6. Assert performance is equivalent within tolerance

        Traffic lifecycle: basic_test_configuration configures mloops before the
        test. Between entropy runs, stop_traffic() + configure_mloops() resets to
        a clean state. basic_test_configuration handles final stop_traffic() at
        teardown.

        Packet-size selection: parametrized over CHOSEN_PACKET_SIZES_ENTROPY_COMPARISON.
        Default runs a single case at the per-chip minimum for the all-ports
        scenario; replace the constant with NoSrhPerfConsts.PACKET_SIZE_SWEEP_LIST
        (or any custom subset) to run more sizes.
        """
        packet_size = _resolve_packet_size(self.chip_type, packet_size, "all_ports")
        split_left, split_right = self.port_split

        test_name = get_perf_test_name(request)
        allure.dynamic.title(
            f"{TESTS_SCENARIO} - entropy comparison - "
            f"packet_size={packet_size}B - split={split_left}x")
        allure.dynamic.description(
            f"Compare high vs low DIPv6 entropy performance for {packet_size}B "
            f"no-SRH packets on {self.chip_type} with {split_left}x port split. "
            f"Expectation: equivalent throughput, latency, and buffer occupancy.")

        # Build upstream/downstream from LLDP-discovered ports (get_right_left_ports_dict)
        # rather than MRCConsts.T0_UPSTREAM_DOWNSTREAM_PORT_GROUPS_DICT, which is
        # hardcoded for the 8x layout (Ethernet0–511). On non-default splits the
        # hardcoded list contains port names that no longer exist on the DUT.
        ports = self.cli_object.performance.get_right_left_ports_dict()
        upstream = ports["left_ports"]
        downstream = ports["right_ports"]
        port_group_df = []
        sdk_upstream = self.cli_object.performance.get_sdk_ports(upstream)
        sdk_downstream = self.cli_object.performance.get_sdk_ports(downstream)
        for sdk_port in sdk_upstream:
            port_group_df.append({
                ValidationConsts.PORT: sdk_port,
                MongoDbConsts.PORT_GROUP_NAME: MRCConsts.BISECTION_UPSTREAM_PORT_GROUP_NAME})
        for sdk_port in sdk_downstream:
            port_group_df.append({
                ValidationConsts.PORT: sdk_port,
                MongoDbConsts.PORT_GROUP_NAME: MRCConsts.BISECTION_DOWNSTREAM_PORT_GROUP_NAME})
        self.cli_object.performance.update_port_group_df_on_dut(port_group_df)

        add_test_mongo_metadata(test_name, {
            MongoDbConsts.CONF_NAME: f"no_srh_entropy_comparison_pkt{packet_size}",
            MongoDbConsts.PORT_GROUP_DF: port_group_df,
            "packet_size": packet_size,
            "port_split": f"{split_left}x",
        })

        # --- High entropy run ---
        high_entropy_results = self._run_entropy_scenario(
            test_name, NoSrhPerfConsts.ENTROPY_HIGH, packet_size,
            upstream, downstream)

        # Stop high-entropy traffic and reconfigure mloops for the next run
        with allure.step("Stop high-entropy traffic and reconfigure mloops"):
            stop_traffic(self.players)
            configure_mloops(self.players)

        # --- Low entropy run ---
        low_entropy_results = self._run_entropy_scenario(
            test_name, NoSrhPerfConsts.ENTROPY_LOW, packet_size,
            upstream, downstream)

        # --- Compare results ---
        with allure.step("Compare high vs low entropy performance"):
            violations = _validate_entropy_equivalence(high_entropy_results,
                                                       low_entropy_results)
            if violations:
                raise TestIssue("\n".join(violations))

    def _run_entropy_scenario(self, test_name, entropy_type, packet_size,
                              upstream, downstream):
        """Run traffic for one entropy type and return validation results.

        Assumes mloops are already configured. Does not stop traffic — the
        caller is responsible for stop_traffic() + configure_mloops() between
        entropy runs, and the basic_test_configuration fixture handles final
        teardown.
        """
        label = "high" if entropy_type == NoSrhPerfConsts.ENTROPY_HIGH else "low"
        packets_per_dip = None
        if packet_size == NoSrhPerfConsts.ALL_PORTS_MIN_PACKET_SIZE_BY_CHIP_TYPE.get(self.chip_type):
            packets_per_dip = NoSrhPerfConsts.NO_SRH_PACKETS_PER_DIP_BY_CHIP_AND_SPLIT.get(
                self.chip_type, {}).get(self.port_split[0])

        with allure.step(f"Generate {label}-entropy no-SRH traffic "
                         f"(packet_size={packet_size}B)"):
            no_srh_stream = partial(create_no_srh_stream,
                                    entropy_type=entropy_type, packet_size=packet_size,
                                    speed_ratio=self.conf_args["port_speed_ratio"],
                                    packets_per_dip=packets_per_dip)
            traffic_jsons = get_bisection_traffic(
                self.players, self.conf_args,
                traffic_type=NoSrhPerfConsts.TRAFFIC_TYPE_SRV6_NO_SRH,
                dut_interfaces_ipv6_configuration_dict=self.dut_interfaces_ipv6_configuration_dict,
                create_workload_stream=no_srh_stream,
                upstream_ports=upstream, downstream_ports=downstream)

        self._clear_counters(f"{label}-entropy")

        with allure.step(f"Run {label}-entropy traffic on all ports"):
            set_shaper_on_traffic_gen(self.players, speed=self.conf_args["speed"],
                                      shaper_value=self.shaper_value)
            run_traffic(self.players, self.scenario, traffic_jsons,
                        attach_traffic_json=False)

        with allure.step(f"Validate {label}-entropy traffic"):
            bw_threshold = {
                MRCConsts.BISECTION_DOWNSTREAM_PORT_GROUP_NAME: {
                    ValidationConsts.TX: min(self.shaper_value, MRCConsts.DUT_TX_UTIL_TH),
                    ValidationConsts.RX: min(self.shaper_value, MRCConsts.DUT_TX_UTIL_TH)},
                MRCConsts.BISECTION_UPSTREAM_PORT_GROUP_NAME: {
                    ValidationConsts.TX: min(self.shaper_value, MRCConsts.DUT_TX_UTIL_TH),
                    ValidationConsts.RX: min(self.shaper_value, MRCConsts.DUT_TX_UTIL_TH)},
            }
            samples_params_dict = PerfConsts.SAMPLES_PARAMS.copy()
            samples_params_dict[PerfConsts.CLEAR_COUNTERS_ENV_VAR] = "False"
            config = ValidationConfig(
                players=self.players, test_name=test_name,
                scenario=self.scenario, chip_type=self.chip_type,
                bw_threshold=bw_threshold,
                tc_occ_threshold=None,
                power_threshold=self.power_thresholds_by_chip_type,
                samples_params_dict=samples_params_dict)
            traffic_validation_jsons_list, violations_list = run_validation(
                config, ignore_violations=True)

        with allure.step(f"Reset shaper after {label}-entropy traffic"):
            set_shaper_on_traffic_gen(self.players, speed=self.conf_args["speed"],
                                      shaper_value=MRCConsts.SHAPER_VALUE_AFTER_TEST)

        add_test_mongo_metadata(test_name, {
            f"{label}_entropy_violations": violations_list,
        })

        if violations_list:
            allure.attach("\n".join(violations_list),
                          f"{label}-entropy violations",
                          allure.attachment_type.TEXT)

        return {
            "entropy_type": entropy_type,
            "validation_jsons": traffic_validation_jsons_list,
            "violations": violations_list,
        }

    def _skip_if_cumulus(self):
        """Skip the single-cage tests on Cumulus.

        Single-cage port selection reads the SONiC ``platform.json`` via
        ``sonic-cfggen`` and the teardown cold-reloads the SONiC config, so
        these tests are SONiC-only for now.
        """
        skip_performance_test_conditionally(
            self.players['dut'].get("is_cumulus", False),
            "single-cage / split-sharing tests are not supported on Cumulus yet")

    def _skip_if_split_sharing_not_supported(self):
        """Skip the current test when split-sharing is not supported.

        Split-sharing is a SPC6-only, 1x-only feature. Skips via
        ``skip_performance_test_conditionally`` (rather than ``pytest.skip``)
        so the skip is attributed to the perf-run and reported to MongoDB
        with a reason string.

        Skip conditions:
          * ``self.chip_type`` not in
            ``NoSrhPerfConsts.SPLIT_SHARING_SUPPORTED_CHIP_TYPES`` — skip
            with reason ``"split-sharing test not supported on {chip}"``.
          * ``self.port_split`` not in
            ``NoSrhPerfConsts.SPLIT_SHARING_SUPPORTED_PORT_SPLITS`` — skip
            with reason ``"split-sharing test only runs at 1x split; ..."``.

        Raises:
            pytest.skip.Exception: Propagated by
                ``skip_performance_test_conditionally`` when either
                condition is met.
        """
        skip_performance_test_conditionally(
            self.chip_type not in NoSrhPerfConsts.SPLIT_SHARING_SUPPORTED_CHIP_TYPES,
            f"split-sharing test not supported on {self.chip_type}")
        skip_performance_test_conditionally(
            self.port_split not in NoSrhPerfConsts.SPLIT_SHARING_SUPPORTED_PORT_SPLITS,
            f"split-sharing test only runs at 1x split; current={self.port_split}")

    def _prepare_single_cage_ports(self, test_name, packet_size):
        """Select one ingress cage + one egress cage on the DUT, publish
        port_group_df, and register mongo metadata.

        All three single-cage test methods (the two split-sharing tests
        and the baseline) share this setup: exactly SINGLE_CAGE_PORTS_PER_SIDE
        adjacent 1x sub-ports on one physical cage act as ingress, the same
        count on a distinct physical cage act as egress, and all remaining
        sub-ports of both cages stay idle so the two active sub-ports can
        borrow their DCL headroom when split-sharing is enabled.

        Returns:
            (upstream, downstream) — Ethernet name lists for each side.
        """
        ports_per_side = NoSrhPerfConsts.SINGLE_CAGE_PORTS_PER_SIDE
        ports = self.cli_object.performance.get_right_left_ports_dict()
        ingress_cage, upstream = _pick_single_cage_ports(
            self.engine, ports["left_ports"], n_ports=ports_per_side)
        _, downstream = _pick_single_cage_ports(
            self.engine, ports["right_ports"], n_ports=ports_per_side,
            exclude_cages={ingress_cage})
        logger.info("split-sharing ports — upstream=%s downstream=%s",
                    upstream, downstream)

        sdk_upstream = self.cli_object.performance.get_sdk_ports(upstream)
        sdk_downstream = self.cli_object.performance.get_sdk_ports(downstream)
        port_group_df = []
        for sdk_port in sdk_upstream:
            port_group_df.append({
                ValidationConsts.PORT: sdk_port,
                MongoDbConsts.PORT_GROUP_NAME:
                    MRCConsts.BISECTION_UPSTREAM_PORT_GROUP_NAME})
        for sdk_port in sdk_downstream:
            port_group_df.append({
                ValidationConsts.PORT: sdk_port,
                MongoDbConsts.PORT_GROUP_NAME:
                    MRCConsts.BISECTION_DOWNSTREAM_PORT_GROUP_NAME})
        self.cli_object.performance.update_port_group_df_on_dut(port_group_df)

        add_test_mongo_metadata(test_name, {
            MongoDbConsts.CONF_NAME: "no_srh_split_sharing",
            MongoDbConsts.PORT_GROUP_DF: port_group_df,
            "packet_size": packet_size,
            "ports_per_side": ports_per_side
        })
        return upstream, downstream

    def _split_sharing_teardown(self):
        """Disable split sharing, remove any dummy ACLs, then cold-reload.

        Runs from every split-sharing test's finally block so a mid-run
        failure never leaves DCL bits flipped or dummy ACLs bound. The
        `reload_configuration` at the end restores a known-good config
        for the next parametrization.
        """
        with allure.step("Teardown: disable split sharing"):
            self.cli_object.performance.disable_split_sharing(self.chip_type)
        with allure.step("Teardown: remove dummy ACLs + reload DUT"):
            try:
                self.cli_object.performance.remove_dummy_acls()
            finally:
                stop_traffic(self.players)
                self.cli_object.general.reload_configuration(force=True)
                self.cli_object.general.verify_dockers_are_up()

    @pytest.mark.skip(reason="This test is not yet supported on SW side")
    @pytest.mark.parametrize("packet_size", CHOSEN_PACKET_SIZES_SPLIT_SHARING_WITH_ACL)
    def test_split_sharing_with_acl(self, request, packet_size):
        """SPC6 SRv6 no-SRH DCL split-sharing validation, with ACL stress.

        Scripted 3-step sequence on SINGLE_CAGE_PORTS_PER_SIDE ingress +
        SINGLE_CAGE_PORTS_PER_SIDE egress 1x sub-ports of a single SPC6
        cage, at the per-chip minimum single-cage packet size:
          1. Baseline (no ACL, no split-sharing) — expect PASS.
          2. Install one dummy ACL on the ingress ports so the pipeline
             runs 3 lookups instead of 2 → expect FAIL.
          3. Enable DCL split resource sharing via the toggle script; the
             active sub-ports now consume the performance budget of the
             cage's idle sub-ports → same ACL, same traffic passes.

        Teardown always disables split sharing and removes the dummy ACL.
        SONiC-only: skipped on Cumulus.

        Packet-size selection: parametrized over CHOSEN_PACKET_SIZES_SPLIT_SHARING_WITH_ACL.
        Default runs a single case at the per-chip minimum for the single-cage
        scenario; replace the constant with NoSrhPerfConsts.PACKET_SIZE_SWEEP_LIST
        (or any custom subset) to run more sizes.
        """
        self._skip_if_cumulus()
        packet_size = _resolve_packet_size(self.chip_type, packet_size, "single_cage")
        self._skip_if_split_sharing_not_supported()

        test_name = get_perf_test_name(request)
        ports_per_side = NoSrhPerfConsts.SINGLE_CAGE_PORTS_PER_SIDE

        allure.dynamic.title(
            f"{TESTS_SCENARIO} - split-sharing-with-acl on "
            f"{self.chip_type} split=1x")
        allure.dynamic.description(
            f"Validate DCL split resource sharing on {self.chip_type}: "
            f"baseline vs. ACL vs. ACL+split-sharing at {packet_size}B on "
            f"{ports_per_side} ingress + {ports_per_side} egress sub-ports "
            f"of a single cage.")

        upstream, downstream = self._prepare_single_cage_ports(
            test_name, packet_size)
        dut_ports_for_acl = upstream + downstream

        try:
            # --- Step 1: baseline (no ACL, no split-sharing) → expect PASS ---
            self._run_split_sharing_sub_scenario(
                test_name, sub_scenario="baseline",
                packet_size=packet_size,
                upstream=upstream, downstream=downstream,
                expect_pass=True)

            # --- Step 2: install ACL → expect FAIL (3rd pipeline lookup) ---
            with allure.step(
                    f"Install {NoSrhPerfConsts.SPLIT_SHARING_NUM_DUMMY_ACLS} "
                    f"dummy ACL(s) on {len(dut_ports_for_acl)} ingress port(s)"):
                self.cli_object.performance.configure_dummy_acls(
                    self.conf_args["template_path"],
                    dut_ports_for_acl,
                    NoSrhPerfConsts.SPLIT_SHARING_NUM_DUMMY_ACLS)
            self._run_split_sharing_sub_scenario(
                test_name, sub_scenario="acl_stress",
                packet_size=packet_size,
                upstream=upstream, downstream=downstream,
                expect_pass=False)

            # --- Step 3: enable split sharing, re-run → expect PASS ---
            with allure.step("Enable DCL split resource sharing"):
                self.cli_object.performance.enable_split_sharing(self.chip_type)
            self._run_split_sharing_sub_scenario(
                test_name, sub_scenario="acl_stress_with_split_sharing",
                packet_size=packet_size,
                upstream=upstream, downstream=downstream,
                expect_pass=True)

        finally:
            self._split_sharing_teardown()

    @pytest.mark.skip(reason="This test is not yet supported on SW side")
    @pytest.mark.parametrize("packet_size", CHOSEN_PACKET_SIZES_SPLIT_SHARING_NO_ACL)
    def test_split_sharing_no_acl(self, request, packet_size):
        """SPC6 SRv6 no-SRH DCL split-sharing validation, no ACL.

        Enable DCL split resource sharing via the toggle script up front,
        then run FWS traffic at the per-chip minimum single-cage packet size
        on SINGLE_CAGE_PORTS_PER_SIDE ingress + SINGLE_CAGE_PORTS_PER_SIDE
        egress 1x sub-ports of a single SPC6 cage. Expect PASS: the two
        active sub-ports consume the performance budget of the cage's idle
        sub-ports so the minimum packet size fits without an ACL in the
        picture.

        Teardown always disables split sharing. SONiC-only: skipped on Cumulus.

        Packet-size selection: parametrized over CHOSEN_PACKET_SIZES_SPLIT_SHARING_NO_ACL.
        Default runs a single case at the per-chip minimum for the single-cage
        scenario; replace the constant with NoSrhPerfConsts.PACKET_SIZE_SWEEP_LIST
        (or any custom subset) to run more sizes.
        """
        self._skip_if_cumulus()
        packet_size = _resolve_packet_size(self.chip_type, packet_size, "single_cage")
        self._skip_if_split_sharing_not_supported()

        test_name = get_perf_test_name(request)
        ports_per_side = NoSrhPerfConsts.SINGLE_CAGE_PORTS_PER_SIDE

        allure.dynamic.title(
            f"{TESTS_SCENARIO} - split-sharing-no-acl on "
            f"{self.chip_type} split=1x")
        allure.dynamic.description(
            f"Validate DCL split resource sharing on {self.chip_type}: "
            f"split-sharing enabled + {packet_size}B FWS on "
            f"{ports_per_side} ingress + {ports_per_side} egress sub-ports "
            f"of a single cage, no ACL installed.")

        upstream, downstream = self._prepare_single_cage_ports(
            test_name, packet_size)

        try:
            with allure.step("Enable DCL split resource sharing"):
                self.cli_object.performance.enable_split_sharing(self.chip_type)
            self._run_split_sharing_sub_scenario(
                test_name, sub_scenario="split_sharing_no_acl",
                packet_size=packet_size,
                upstream=upstream, downstream=downstream,
                expect_pass=True)

        finally:
            self._split_sharing_teardown()

    @pytest.mark.parametrize("packet_size", CHOSEN_PACKET_SIZES_SINGLE_CAGE_BASELINE)
    def test_no_srh_single_cage_baseline(self, request, packet_size):
        """SRv6 no-SRH baseline on a single-cage subset, no DCL WA.

        Runs FWS traffic on SINGLE_CAGE_PORTS_PER_SIDE ingress +
        SINGLE_CAGE_PORTS_PER_SIDE egress sub-ports of a single cage. No ACL
        is installed and DCL split resource sharing is NEVER toggled — this
        is the pure baseline that shows the standard no-SRH pipeline handles
        this subset natively. Fails on any validation violation.

        Teardown is minimal: no split-sharing to disable, no dummy ACLs
        to remove — just stop traffic and cold-reload so the next
        parametrization starts clean. SONiC-only: skipped on Cumulus.

        Packet-size selection: parametrized over CHOSEN_PACKET_SIZES_SINGLE_CAGE_BASELINE.
        Default runs a single case at the per-chip minimum for the single-cage
        scenario; replace the constant with NoSrhPerfConsts.PACKET_SIZE_SWEEP_LIST
        (or any custom subset) to run more sizes.
        """
        self._skip_if_cumulus()
        packet_size = _resolve_packet_size(self.chip_type, packet_size, "single_cage")

        test_name = get_perf_test_name(request)
        ports_per_side = NoSrhPerfConsts.SINGLE_CAGE_PORTS_PER_SIDE
        split_left, _ = self.port_split

        allure.dynamic.title(
            f"{TESTS_SCENARIO} - no-SRH single-cage baseline on "
            f"{self.chip_type} split={split_left}x")
        allure.dynamic.description(
            f"Validate baseline no-SRH pipeline on {self.chip_type} at "
            f"{packet_size}B on {ports_per_side} ingress + {ports_per_side} "
            f"egress sub-ports of a single cage, without any DCL WA.")

        upstream, downstream = self._prepare_single_cage_ports(
            test_name, packet_size)

        try:
            results = self._run_entropy_scenario(
                test_name, NoSrhPerfConsts.ENTROPY_LOW, packet_size,
                upstream, downstream)
        finally:
            with allure.step("Reload DUT"):
                stop_traffic(self.players)
                self.cli_object.general.reload_configuration(force=True)
                self.cli_object.general.verify_dockers_are_up()

        if results["violations"]:
            raise TestIssue("Single-cage baseline run had violations: " + "; ".join(results["violations"]))

    def _run_split_sharing_sub_scenario(self, test_name, sub_scenario, packet_size,
                                        upstream, downstream, expect_pass):
        """Run one split-sharing sub-scenario (build traffic → run → validate →
        cleanup between runs). Asserts pass/fail per expect_pass, mirroring the
        baseline/ACL/split-sharing steps of test_split_sharing_with_acl.

        Uses ENTROPY_LOW (single stream per pair) so the validation signal
        isn't muddied by per-DIP variance — this is a scripted correctness
        check, not an entropy sweep.
        """
        with allure.step(f"[{sub_scenario}] Build no-SRH traffic "
                         f"(packet_size={packet_size}B, entropy=LOW)"):
            no_srh_stream = partial(create_no_srh_stream,
                                    entropy_type=NoSrhPerfConsts.ENTROPY_LOW, packet_size=packet_size,
                                    speed_ratio=self.conf_args["port_speed_ratio"])
            traffic_jsons = get_bisection_traffic(
                self.players, self.conf_args,
                traffic_type=NoSrhPerfConsts.TRAFFIC_TYPE_SRV6_NO_SRH,
                dut_interfaces_ipv6_configuration_dict=self.dut_interfaces_ipv6_configuration_dict,
                create_workload_stream=no_srh_stream,
                upstream_ports=upstream, downstream_ports=downstream)

        self._clear_counters(sub_scenario)

        with allure.step(f"[{sub_scenario}] Set shaper and run traffic"):
            set_shaper_on_traffic_gen(self.players,
                                      speed=self.conf_args["speed"],
                                      shaper_value=self.shaper_value)
            run_traffic(self.players, self.scenario, traffic_jsons,
                        attach_traffic_json=False)

        with allure.step(f"[{sub_scenario}] Validate traffic "
                         f"(expect_pass={expect_pass})"):
            bw_threshold = {
                MRCConsts.BISECTION_DOWNSTREAM_PORT_GROUP_NAME: {
                    ValidationConsts.TX: min(self.shaper_value,
                                             MRCConsts.DUT_TX_UTIL_TH),
                    ValidationConsts.RX: min(self.shaper_value,
                                             MRCConsts.DUT_TX_UTIL_TH)},
                MRCConsts.BISECTION_UPSTREAM_PORT_GROUP_NAME: {
                    ValidationConsts.TX: min(self.shaper_value,
                                             MRCConsts.DUT_TX_UTIL_TH),
                    ValidationConsts.RX: min(self.shaper_value,
                                             MRCConsts.DUT_TX_UTIL_TH)},
            }
            samples_params_dict = PerfConsts.SAMPLES_PARAMS.copy()
            samples_params_dict[PerfConsts.CLEAR_COUNTERS_ENV_VAR] = "False"
            config = ValidationConfig(
                players=self.players, test_name=test_name,
                scenario=self.scenario, chip_type=self.chip_type,
                bw_threshold=bw_threshold,
                tc_occ_threshold=None,
                power_threshold=self.power_thresholds_by_chip_type,
                samples_params_dict=samples_params_dict)
            _, violations_list = run_validation(config, ignore_violations=True)

        # Reset shaper + reconfigure mloops between sub-scenarios (same
        # pattern test_no_srh_entropy_comparison uses between HE and LE runs).
        with allure.step(f"[{sub_scenario}] Reset shaper for next sub-scenario"):
            set_shaper_on_traffic_gen(self.players,
                                      speed=self.conf_args["speed"],
                                      shaper_value=MRCConsts.SHAPER_VALUE_AFTER_TEST)
            stop_traffic(self.players)
            configure_mloops(self.players)

        add_test_mongo_metadata(test_name, {
            f"{sub_scenario}_packet_size": packet_size,
            f"{sub_scenario}_expected_pass": expect_pass,
            f"{sub_scenario}_violations": violations_list,
        })

        if violations_list:
            allure.attach("\n".join(violations_list),
                          f"{sub_scenario} violations",
                          allure.attachment_type.TEXT)

        # Enforce expectation
        if expect_pass and violations_list:
            raise TestIssue(
                f"[{sub_scenario}] expected PASS but validator reported "
                f"violations:\n" + "\n".join(violations_list))
        if not expect_pass and not violations_list:
            raise TestIssue(
                f"[{sub_scenario}] expected FAIL (BW-below-threshold or "
                f"TG-side drops from ACL 3rd-lookup path) but validator "
                f"reported a clean run — split-sharing may already be "
                f"enabled, ACL install failed, or packet size is too big.")


def _validate_entropy_equivalence(high_results, low_results):
    """Compare high vs low entropy validation results and return violations.

    Checks:
    1. Neither run should have hard violations (line-rate, drops)
    2. Throughput should be equivalent within tolerance
    """
    violations = []

    if high_results["violations"]:
        violations.append(
            f"High-entropy run had violations: "
            f"{'; '.join(high_results['violations'])}")
    if low_results["violations"]:
        violations.append(
            f"Low-entropy run had violations: "
            f"{'; '.join(low_results['violations'])}")

    high_jsons = high_results.get("validation_jsons", [])
    low_jsons = low_results.get("validation_jsons", [])

    if not high_jsons or not low_jsons:
        return violations

    high_bw = _extract_avg_bw(high_jsons)
    low_bw = _extract_avg_bw(low_jsons)

    if high_bw is not None and low_bw is not None and max(high_bw, low_bw) > 0:
        bw_diff = abs(high_bw - low_bw) / max(high_bw, low_bw)
        if bw_diff > NoSrhPerfConsts.BW_EQUIVALENCE_TOLERANCE:
            violations.append(
                f"Throughput equivalence violated: high={high_bw:.4f}, "
                f"low={low_bw:.4f}, diff={bw_diff:.4f}, "
                f"tolerance={NoSrhPerfConsts.BW_EQUIVALENCE_TOLERANCE}")

    return violations


def _extract_avg_bw(validation_jsons):
    """Extract the average TX bandwidth over all bandwidth samples and port groups.

    tx_avg lives in Bandwidth_samples -> <sample> -> <port group> -> bw_stats.
    """
    if not validation_jsons:
        return None
    total_tx = 0.0
    count = 0
    for vjson in validation_jsons:
        if not isinstance(vjson, dict):
            continue
        for sample_id, bw_sample in vjson.get(ValidationConsts.BW_SAMPLES, {}).items():
            if sample_id == ValidationConsts.SAMPLES_PARAMS or not isinstance(bw_sample, dict):
                continue
            for group_data in bw_sample.values():
                tx_val = group_data.get(ValidationConsts.BW_STATS, {}).get(ValidationConsts.TX_BW_AVG)
                if tx_val is not None:
                    total_tx += float(tx_val)
                    count += 1
    return total_tx / count if count > 0 else None
