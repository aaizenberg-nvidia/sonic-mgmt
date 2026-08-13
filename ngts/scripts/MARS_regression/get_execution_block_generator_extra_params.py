"""Compute the dbs a setup runs tonight and print them as a Mars parameter.

Legacy behaviour, unchanged: a setup looks itself up in SETUPS_GROUPS_MAP and runs every
PLATFORM_DEPENDENT db of its set, plus the PLATFORM_AGNOSTIC group of the day.

Added by this change: the setups of a scenario running the same topo and hwsku run the same
platform, so their PLATFORM_DEPENDENT results are interchangeable and those dbs are split between
them instead of being run by each of them. PLATFORM_AGNOSTIC dbs are never shared, they stay on the
setup owning the set, which leaves the daily rotation untouched. One or two such setups all already
listed in SETUPS_GROUPS_MAP keep the legacy behaviour, adding one more starts the sharing. A db is
never given to a setup removing it with --remove_dbs, such a setup is saying it cannot run it and
nobody would then run it, see get_setups_able_to_run().

Those setups and their arguments are read from the scenario json, see read_scenario_setups(). Only
the setups computing their dbs with this script take part, a setup holding its dbs in the execution
block generator panel of the scenario ui, in its .setup file, or computed by the script of another
team, runs exactly those dbs and is left out. Any failure of that read falls back to the legacy
behaviour and is reported on stderr, an unreported fallback is indistinguishable from a scenario
with nothing to share.

They are called the same topo and hwsku setups, and never a group, because the word group already
means three different things in this file:
  SETUPS_GROUP_1 / _2          the rotation phase of a setup in SETUPS_GROUPS_MAP, it decides which
                               agnostic group that setup runs on an even and on an odd day. The two
                               setups paired in SETUPS_GROUPS_MAP hold opposite phases, so such a
                               pair is NOT what the same topo and hwsku setups are.
  PLATFORM_AGNOSTIC_GROUP1 / 2 the two halves the platform agnostic dbs of a set are split into.
  test group                   the label of a db in TEST_GROUP_MAP, either PLATFORM_DEPENDENT or
                               one of the two agnostic groups above.
"""
from db_lists_for_nightly_regression import *
from datetime import datetime
import json
import argparse
import os
import re
import shlex
import sys
import time

COMMUNITY_SET1_NAME = 'community_set1'
COMMUNITY_SET2_NAME = 'community_set2'
COMMUNITY_LIST_NAMES = [COMMUNITY_SET1_NAME, COMMUNITY_SET2_NAME]
COMMUNITY_NAME = 'community'
CANONICAL_NAME = 'canonical'

# Name of this script, the command of a setup must run it, see read_scenario_setups().
GENERATOR_SCRIPT_NAME = os.path.basename(__file__)


def get_setup_set_and_group(setup_name):
    for set_name, topo_dict in SETUPS_GROUPS_MAP.items():
        for (_, _), setup_dict in topo_dict.items():
            if setup_name in setup_dict:
                return set_name, setup_dict[setup_name]
    return None, None


def get_test_group_map_by_regression_set(setup_set_name):
    if 'both' in setup_set_name:
        return {**TEST_GROUP_MAP[COMMUNITY_SET1_NAME], **TEST_GROUP_MAP[COMMUNITY_SET2_NAME]}
    return TEST_GROUP_MAP[setup_set_name]


def get_all_tests_per_set(set_tests_group_map):
    return [test_name for test_name, _ in set_tests_group_map.items()]


def get_daily_platform_agnostic_tests_group_name(setup_group_name, platform_agnostic_tests_group_name_input=None):
    if platform_agnostic_tests_group_name_input:
        if platform_agnostic_tests_group_name_input not in [PLATFORM_AGNOSTIC_GROUP1, PLATFORM_AGNOSTIC_GROUP2]:
            # Without this check a typo silently drops every platform agnostic db of the setup.
            sys.stderr.write('WARNING: invalid --platform_agnostic_group {}, expected one of {}. '
                             'Falling back to the daily rotation\n'
                             .format(platform_agnostic_tests_group_name_input,
                                     [PLATFORM_AGNOSTIC_GROUP1, PLATFORM_AGNOSTIC_GROUP2]))
        else:
            return platform_agnostic_tests_group_name_input
    day_of_year = datetime.today().timetuple().tm_yday
    is_even_day = day_of_year % 2 == 0
    if setup_group_name == SETUPS_GROUP_1:
        platform_agnostic_tests_group_name = PLATFORM_AGNOSTIC_GROUP1 if is_even_day else PLATFORM_AGNOSTIC_GROUP2
    else:
        platform_agnostic_tests_group_name = PLATFORM_AGNOSTIC_GROUP2 if is_even_day else PLATFORM_AGNOSTIC_GROUP1
    return platform_agnostic_tests_group_name


def get_tests_list(test_group_map, setup_platform_agnostic_tests_group_name):
    tests_list = [test_name for test_name, test_group in test_group_map.items() if test_group in [PLATFORM_DEPENDENT, setup_platform_agnostic_tests_group_name]]
    return tests_list


def filter_control_tests(tests_list, setup_name, setup_set_name):
    regression_set_name = COMMUNITY_NAME if setup_set_name in COMMUNITY_LIST_NAMES else CANONICAL_NAME
    control_plane_setups_and_tests = CONTROL_PLANE_TESTS_MAP[regression_set_name]
    control_plane_setups = control_plane_setups_and_tests['setups']
    if setup_name not in control_plane_setups:
        tests_list = [test for test in tests_list if test not in control_plane_setups_and_tests['tests']]
    return tests_list


def get_all_available_tests():
    return (
        set(TEST_GROUP_MAP[COMMUNITY_SET1_NAME].keys()) |
        set(TEST_GROUP_MAP[COMMUNITY_SET2_NAME].keys()) |
        set(TEST_GROUP_MAP[CANONICAL_NAME].keys())
    )


def add_remove_dbs_by_input(tests_list, add_dbs, remove_dbs):
    all_available_tests = get_all_available_tests()
    for db in add_dbs:
        if db not in all_available_tests:
            sys.stderr.write('WARNING: --add_dbs {} is not a known db, it is ignored\n'.format(db))
            continue
        if db not in tests_list:
            tests_list.append(db)
    for db in remove_dbs:
        if db not in tests_list:
            sys.stderr.write('WARNING: --remove_dbs {} is not in the list of this setup, it is ignored\n'.format(db))
            continue
        tests_list.remove(db)
    return tests_list


def print_execution_block_generator_in_mars_format(db_paths):
    json_strings = []
    for db_path in db_paths:
        json_obj = {
            'entry_points': 'SONIC_MGMT',
            'tests_dbs_tarball': MARS_DBS_PATH + db_path
        }
        json_strings.append(json.dumps(json_obj))
    result = f"'[{','.join(json_strings)}]'"
    print(f"--meinfo_execution_block_generator {result}")


def is_setup_exist_in_setups_groups_map(setup_name):
    for category in SETUPS_GROUPS_MAP.values():
        for topology_dict in category.values():
            if setup_name in topology_dict:
                return True
    return False


# --- Sharing the PLATFORM_DEPENDENT dbs between the setups with the same topo and hwsku ---
# --- Pure computation, no IO ---


def assign_sets_to_setups(same_topo_hwsku_setups):
    """Decide which set every setup with the same topo and hwsku owns.

    Priority: --set_name, then SETUPS_GROUPS_MAP, then the first free setups by name are used to fill
    the sets that nobody owns yet. Every remaining setup gets None, it only takes PLATFORM_DEPENDENT
    dbs and no PLATFORM_AGNOSTIC db at all.
    A set given with --set_name to more than one of those setups is owned by all of
    them, and its PLATFORM_AGNOSTIC dbs are then executed by each of them.
    Returns a dict of setup name to set name or None.
    """
    set_by_setup = {}
    for setup in same_topo_hwsku_setups:
        if setup['set_name']:
            set_by_setup[setup['name']] = setup['set_name']
    for setup in same_topo_hwsku_setups:
        if setup['name'] not in set_by_setup:
            registered_set_name, _ = get_setup_set_and_group(setup['name'])
            if registered_set_name:
                set_by_setup[setup['name']] = registered_set_name
    # Give the sets nobody owns yet to the setups still free, by name so that the result is stable.
    free_setup_names = sorted(setup['name'] for setup in same_topo_hwsku_setups if setup['name'] not in set_by_setup)
    for set_name in COMMUNITY_LIST_NAMES:
        if set_name not in set_by_setup.values() and free_setup_names:
            set_by_setup[free_setup_names.pop(0)] = set_name
    return {setup['name']: set_by_setup.get(setup['name']) for setup in same_topo_hwsku_setups}


def is_db_sharing_needed(same_topo_hwsku_setups):
    """Tell whether the PLATFORM_DEPENDENT dbs have to be shared between the given setups.

    Canonical setups keep the legacy behaviour, they have a single set and nothing to share. One or
    two setups all listed in SETUPS_GROUPS_MAP keep it too, so the setups already paired there are
    not impacted by this feature. A lone setup missing from that map still goes through the sharing,
    that is how it is given a set instead of running nothing.
    """
    for setup in same_topo_hwsku_setups:
        registered_set_name, _ = get_setup_set_and_group(setup['name'])
        if registered_set_name == CANONICAL_NAME:
            return False
    all_registered = all(is_setup_exist_in_setups_groups_map(setup['name']) for setup in same_topo_hwsku_setups)
    return not (len(same_topo_hwsku_setups) <= 2 and all_registered)


def get_agnostic_dbs(set_name, owner_setup):
    """Return the PLATFORM_AGNOSTIC dbs of a set, they are locked to the setup owning that set.

    A setup that is not in SETUPS_GROUPS_MAP has no rotation phase, so no daily rotation can be
    computed for it and it takes both agnostic groups.
    """
    _, owner_group_name = get_setup_set_and_group(owner_setup['name'])
    if owner_setup['run_all_tests']:
        agnostic_group_names = [PLATFORM_AGNOSTIC_GROUP1, PLATFORM_AGNOSTIC_GROUP2]
    elif owner_setup['platform_agnostic_group'] in [PLATFORM_AGNOSTIC_GROUP1, PLATFORM_AGNOSTIC_GROUP2]:
        agnostic_group_names = [owner_setup['platform_agnostic_group']]
    elif owner_group_name:
        agnostic_group_names = [get_daily_platform_agnostic_tests_group_name(owner_group_name)]
    else:
        agnostic_group_names = [PLATFORM_AGNOSTIC_GROUP1, PLATFORM_AGNOSTIC_GROUP2]
    # Read through get_test_group_map_by_regression_set() and not in TEST_GROUP_MAP, a setup given
    # --set_name both owns a set holding the dbs of set1 and of set2, which is not a key of the map.
    return [test_name for test_name, test_group in get_test_group_map_by_regression_set(set_name).items()
            if test_group in agnostic_group_names]


def split_dependent_dbs(set_names):
    """Split the PLATFORM_DEPENDENT dbs of the given sets into the three parts used by the sharing.

    Returns (pinned, every_setup, sharable):
      pinned      control plane dbs, only a whitelisted setup may run them
      every_setup dbs every setup has to run
      sharable    the rest, they are distributed across the setups
    """
    dependent_dbs = []
    # Sorted so that the dbs are always split in the same order, whatever the order of the sets.
    for set_name in sorted(set_names):
        for test_name, test_group in get_test_group_map_by_regression_set(set_name).items():
            if test_group == PLATFORM_DEPENDENT and test_name not in dependent_dbs:
                dependent_dbs.append(test_name)
    control_plane_dbs = CONTROL_PLANE_TESTS_MAP[COMMUNITY_NAME]['tests']
    pinned = [db for db in dependent_dbs if db in control_plane_dbs]
    every_setup = [db for db in dependent_dbs if db in EVERY_SETUP_DBS]
    sharable = [db for db in dependent_dbs if db not in control_plane_dbs and db not in EVERY_SETUP_DBS]
    return pinned, every_setup, sharable


def pack_dbs_into_blocks(dbs):
    """Pack the dbs into blocks, the dbs that depend on each other stay together in a single block."""
    blocks = []
    packed_dbs = set()
    for indivisible_block in INDIVISIBLE_DB_BLOCKS:
        block = [db for db in indivisible_block if db in dbs]
        if block:
            blocks.append(block)
            packed_dbs.update(block)
    blocks.extend([db] for db in dbs if db not in packed_dbs)
    return blocks


def get_setups_able_to_run(block, removed_dbs_per_setup):
    """Return the setups of the group that would really run every db of a block.

    A setup removing a db with --remove_dbs is saying it cannot run that db, a setup without the dpu
    image of a smartswitch does that for the smartswitch dbs. The sharing gives a db to a single
    setup, so giving it to that one drops it from the whole group while its neighbours were able to
    run it. A block is refused as a whole and never in part, its dbs depend on each other.

    When no setup of the group is able to run the block, every one of them is returned: the block is
    then placed as before and removed by whoever gets it, which is what all of them asked for.
    """
    able_setups = [setup_name for setup_name, removed_dbs in removed_dbs_per_setup.items()
                   if not set(block) & removed_dbs]
    return able_setups or list(removed_dbs_per_setup)


def distribute_blocks_by_total_dbs(blocks, initial_dbs_per_setup, removed_dbs_per_setup):
    """Give every block to the setup holding the smallest total number of dbs so far.

    The balance is done on the total of a setup and not on the shared dbs only, so a setup without
    agnostic and control plane dbs takes more dependent dbs and all the setups end up
    with a similar amount of work. Only a setup able to run a block may receive it, see
    get_setups_able_to_run().
    """
    dependent_dbs_per_setup = {setup_name: [] for setup_name in initial_dbs_per_setup}
    totals = {setup_name: len(dbs) for setup_name, dbs in initial_dbs_per_setup.items()}
    blocks_with_their_setups = [(get_setups_able_to_run(block, removed_dbs_per_setup), block)
                                for block in blocks]
    # The blocks the fewest setups can run first, then the biggest ones, placing either of them last
    # would leave no setup able to take them evenly.
    for able_setups, block in sorted(blocks_with_their_setups,
                                     key=lambda pair: (len(pair[0]), -len(pair[1]))):
        target_setup = min(able_setups, key=lambda setup_name: (totals[setup_name], setup_name))
        dependent_dbs_per_setup[target_setup].extend(block)
        totals[target_setup] += len(block)
    return dependent_dbs_per_setup


def compute_shared_db_lists(same_topo_hwsku_setups):
    """Compute the db list of every setup with the same topo and hwsku, sharing their dependent dbs.

    same_topo_hwsku_setups is a list of dicts holding the name and the input arguments of every
    setup sharing the topo and the hwsku. The control plane dbs are given to the whitelisted setup
    only, so when none of those setups is whitelisted nobody runs them and they have to be added
    with --add_dbs.
    Returns a dict of setup name to db list.
    """
    set_by_setup = assign_sets_to_setups(same_topo_hwsku_setups)
    set_names = {set_name for set_name in set_by_setup.values() if set_name}
    pinned_dbs, every_setup_dbs, sharable_dbs = split_dependent_dbs(set_names)
    control_plane_setups = CONTROL_PLANE_TESTS_MAP[COMMUNITY_NAME]['setups']
    locked_dbs_per_setup = {}
    for setup in same_topo_hwsku_setups:
        locked_dbs = list(every_setup_dbs)
        if setup['name'] in control_plane_setups:
            locked_dbs += pinned_dbs
        set_name = set_by_setup[setup['name']]
        if set_name:
            locked_dbs += get_agnostic_dbs(set_name, setup)
        locked_dbs_per_setup[setup['name']] = locked_dbs
    removed_dbs_per_setup = {setup['name']: set(setup['remove_dbs']) for setup in same_topo_hwsku_setups}
    dependent_dbs_per_setup = distribute_blocks_by_total_dbs(pack_dbs_into_blocks(sharable_dbs),
                                                             locked_dbs_per_setup, removed_dbs_per_setup)
    return {setup_name: locked_dbs + dependent_dbs_per_setup[setup_name]
            for setup_name, locked_dbs in locked_dbs_per_setup.items()}


# --- Input arguments, shared by the main block and by the parsing of the other setups ---


def build_arg_parser():
    parser = argparse.ArgumentParser(description='Parse input arguments')
    parser.add_argument('--setup_name', type=str, help='Mars setup name')
    parser.add_argument('--run_all_tests', type=str, default='False', help='Flag to run all tests of the setup\'s regression set (community_set1, community_set2 or canonical). Use "True" or "False".')
    parser.add_argument('--set_name', type=str, default=None, help='update setups\'s set (canonical, set1, set2 or both). When provided, run_all_tests is set to True.')
    parser.add_argument('--platform_agnostic_group', type=str, default=None, help='override platform agnostic tests group name. Has no effect if run_all_tests is True or set_name is provided')
    parser.add_argument("--add_dbs", nargs='*', default=[], help='add dbs to the execution block generator. Example  --add_dbs "community/ip_neigh.db" "community/resources.db"')
    parser.add_argument('--remove_dbs', nargs='*', default=[], help='remove dbs from the execution block generator. Example  --remove_dbs "community/ip_neigh.db" "community/resources.db" ')
    return parser


def proccess_input_args():
    parser = build_arg_parser()

    args = parser.parse_args()
    setup_name = args.setup_name
    run_all_tests = args.run_all_tests.lower() == 'true'
    override_set_name = args.set_name
    platform_agnostic_tests_group_name = args.platform_agnostic_group
    add_dbs = args.add_dbs
    remove_dbs = args.remove_dbs
    return setup_name, run_all_tests, override_set_name, platform_agnostic_tests_group_name, add_dbs, remove_dbs


# --- Sharing: reading the scenario json and the .setup files ---


def get_option_value(tokens, option_name):
    """Return the value of an option in a command line already split into tokens.

    Both '--option value' and '--option=value' are used in the scenario json.
    """
    if option_name in tokens:
        value_index = tokens.index(option_name) + 1
        if value_index < len(tokens):
            return tokens[value_index]
    for token in tokens:
        if token.startswith(option_name + '='):
            return token.split('=', 1)[1]
    return None


def read_nfs_file(file_path, file_description):
    """Return the content of a file living on the /auto mounts, or None when it cannot be read.

    Those mounts fail sporadically, so the read is retried. A failure is reported because every
    caller falls back to the legacy behaviour.
    """
    for attempt in range(NFS_READ_RETRIES):
        try:
            with open(file_path, errors='ignore') as opened_file:
                return opened_file.read()
        except OSError as read_error:
            if attempt + 1 == NFS_READ_RETRIES:
                sys.stderr.write('WARNING: cannot read the {} {} after {} attempts ({})\n'
                                 .format(file_description, file_path, NFS_READ_RETRIES, read_error))
                return None
            time.sleep(NFS_RETRY_DELAY_SECONDS)
    return None


def read_setup_conf(setup_name, conf_file_name):
    """Return the content of the .setup file of a setup, or None when it cannot be read."""
    if not conf_file_name:
        conf_file_name = setup_name.replace('_setup', '') + '.setup'
    conf_file_path = os.path.join(SETUP_CONF_DIR, setup_name, conf_file_name)
    return read_nfs_file(conf_file_path, 'setup conf file')


def read_setup_conf_tag(conf_content, tag_name):
    """Return the value of an xml like tag of a .setup file, or None when the tag is absent."""
    tag_match = re.search(r'<{0}>(.*?)</{0}>'.format(tag_name), conf_content, re.DOTALL)
    return tag_match.group(1).strip() if tag_match else None


def read_setup_conf_topo_hwsku(setup_name, conf_file_name):
    """Read the default topo and hwsku of a setup from its .setup file."""
    conf_content = read_setup_conf(setup_name, conf_file_name)
    if conf_content is None:
        return None, None
    return (read_setup_conf_tag(conf_content, SETUP_CONF_TOPOLOGY_TAG),
            read_setup_conf_tag(conf_content, SETUP_CONF_HWSKU_TAG))


def resolve_topo_hwsku(setup_name, extra_args_inst):
    """Return the topo and the hwsku a setup runs with in the current scenario.

    A topo or a hwsku set on the setup in the scenario wins over the default of the .setup file.
    """
    tokens = shlex.split(extra_args_inst or '')
    topo = get_option_value(tokens, SCENARIO_TOPOLOGY_OPTION)
    hwsku = get_option_value(tokens, SCENARIO_HWSKU_OPTION)
    if topo and hwsku:
        return topo, hwsku
    conf_topo, conf_hwsku = read_setup_conf_topo_hwsku(setup_name,
                                                       get_option_value(tokens, SCENARIO_SETUP_CONF_OPTION))
    return topo or conf_topo, hwsku or conf_hwsku


def runs_this_script(calculate_params_command):
    """Tell whether the calculate params command of a setup runs this script.

    The scenario key holding that command is a generic Mars one, other teams point it at a script of
    their own. Such a setup takes its dbs from that script, exactly like a setup taking them from
    the scenario ui panel, so it must not be given a share of the dbs computed here.
    """
    return any(os.path.basename(token) == GENERATOR_SCRIPT_NAME
               for token in shlex.split(calculate_params_command or ''))


def parse_calculate_params_command(setup_name, calculate_params_command):
    """Turn the calculate params command of a setup into the fields used to share the dbs.

    Every setup taking part holds its own command, so the arguments of the other setups with the
    same topo and hwsku are read from there. As in the legacy flow, --set_name also means
    running the whole set. --remove_dbs is read as well, it names the dbs that setup cannot run,
    see get_setups_able_to_run().
    """
    command_tokens = shlex.split(calculate_params_command or '')
    # The command usually starts with 'python3 <script path>', but that prefix is not guaranteed, so
    # the options are located instead of being skipped by position. Skipping the wrong number of
    # tokens would silently drop --set_name and --run_all_tests, and misplace the shared dbs.
    options_start = next((index for index, token in enumerate(command_tokens)
                          if token.startswith('--')), len(command_tokens))
    args, _ = build_arg_parser().parse_known_args(command_tokens[options_start:])
    set_name = None
    if args.set_name:
        set_name = args.set_name if args.set_name == CANONICAL_NAME else 'community_' + args.set_name
        if not (set_name in TEST_GROUP_MAP or 'both' in set_name):
            # The --set_name of that setup names no set, a scenario leaving a Mars placeholder
            # unexpanded does that. Sharing with it as if it had no set keeps its neighbours running,
            # the alternative being every setup of the group failing on its argument.
            sys.stderr.write('WARNING: {} asks for the unknown set {}, the dbs are shared with it as '
                             'if it had no set\n'.format(setup_name, args.set_name))
            set_name = None
    return {'name': setup_name,
            'set_name': set_name,
            'run_all_tests': args.run_all_tests.lower() == 'true' or bool(args.set_name),
            'platform_agnostic_group': args.platform_agnostic_group,
            'remove_dbs': args.remove_dbs}


def read_scenario_json():
    """Read the scenario json of the current run, or None when it cannot be read.

    The path comes from an environment variable set by Mars and always points to the file being
    executed. Building it from the scenario name would break a manual build with selected phases,
    which writes to another directory.
    """
    scenario_json_path = os.environ.get(SCENARIO_JSON_ENV_VAR)
    if not scenario_json_path:
        sys.stderr.write('WARNING: {} is not set, the dbs are not shared\n'
                         .format(SCENARIO_JSON_ENV_VAR))
        return None
    scenario_json_content = read_nfs_file(scenario_json_path, 'scenario json')
    if scenario_json_content is None:
        return None
    try:
        return json.loads(scenario_json_content)
    except ValueError as parse_error:
        sys.stderr.write('WARNING: cannot parse {} ({}), the dbs are not shared\n'
                         .format(scenario_json_path, parse_error))
        return None


def read_scenario_setups():
    """Return the setups of the current scenario computing their dbs with this script.

    Every returned setup holds its arguments, its topo and its hwsku. A setup taking its dbs from
    the execution block generator panel of the scenario ui, from its .setup file, or from the script
    of another team, is left out: it runs those dbs and none of the ones computed here, so giving it
    a share would only lose them.

    The json holds one entry per setup, under a phase name that is not fixed:
      {"phases": [{"phase_main_1": [{"parallel execution": [
          {"parameters": {"setup_name": "...",
                          "extra_args_inst": "--meinfo_topology t0-56 ...",
                          "MARS_ORCH_CALCULATE_PARAMS_COMMAND": "python3 ... --set_name set1"}},
          ...]}]}]}
    The phase name varies, hence the iteration on the values, and a phase may hold entries that are
    not dicts.
    Returns a dict of setup name to setup, a setup listed in several phases is kept once.
    """
    scenario = read_scenario_json()
    if not scenario:
        return {}
    scenario_setups = {}
    for phase in scenario.get('phases', []):
        for phase_steps in phase.values():
            for phase_step in phase_steps:
                if not isinstance(phase_step, dict):
                    continue
                for execution_item in phase_step.get('parallel execution', []):
                    parameters = execution_item.get('parameters', {})
                    setup_name = parameters.get('setup_name')
                    if not setup_name or setup_name in scenario_setups:
                        # The same setup may be listed in several phases, keep the first entry.
                        continue
                    calculate_params_command = parameters.get(SCENARIO_CALCULATE_PARAMS_KEY)
                    if not runs_this_script(calculate_params_command):
                        # The dbs of that setup are set in the scenario ui, come from its .setup
                        # file, or are computed by the script of another team, it is not one of the
                        # setups sharing the dbs.
                        continue
                    setup = parse_calculate_params_command(setup_name, calculate_params_command)
                    setup['topo'], setup['hwsku'] = resolve_topo_hwsku(setup_name,
                                                                       parameters.get('extra_args_inst'))
                    scenario_setups[setup_name] = setup
    return scenario_setups


def get_setup_list_with_same_topo_and_hwsku(setup_name):
    """Return the list of setups of the current scenario running the same topo and hwsku, itself included.

    The same topo and the same hwsku mean the same platform, so a PLATFORM_DEPENDENT db produces
    the same result on any of those setups and can be run by only one of them. Setups running a
    different topo or hwsku never share anything.

    Returns None when the scenario is unknown or when the topo or the hwsku cannot be resolved, the
    caller then keeps the legacy behaviour.
    """
    scenario_setups = read_scenario_setups()
    if not scenario_setups:
        # The scenario could not be read, read_scenario_json() reported why, or none of its setups
        # computes its dbs with this script.
        return None
    my_setup = scenario_setups.get(setup_name)
    if not my_setup:
        sys.stderr.write('WARNING: {} is not in the scenario json, or its dbs do not come from this '
                         'script, the dbs are not shared\n'.format(setup_name))
        return None
    if not my_setup['topo'] or not my_setup['hwsku']:
        sys.stderr.write('WARNING: cannot resolve the topo and the hwsku of {} (topo={} hwsku={}), '
                         'the dbs are not shared\n'
                         .format(setup_name, my_setup['topo'], my_setup['hwsku']))
        return None
    return [setup for setup in scenario_setups.values()
            if (setup['topo'], setup['hwsku']) == (my_setup['topo'], my_setup['hwsku'])]


# --- Sharing: entry point used by the main block ---


def get_shared_dbs_list(setup_name):
    """Return the db list of a setup sharing its PLATFORM_DEPENDENT dbs with the same topo and hwsku setups.

    Returns None when there is nothing to share, the caller then keeps the legacy behaviour.
    """
    same_topo_hwsku_setups = get_setup_list_with_same_topo_and_hwsku(setup_name)
    if not same_topo_hwsku_setups or not is_db_sharing_needed(same_topo_hwsku_setups):
        return None
    return compute_shared_db_lists(same_topo_hwsku_setups).get(setup_name)


def report_selected_dbs(setup_name, dbs_list, is_shared):
    """Report on stderr what was selected, stdout must hold the Mars parameter only."""
    sys.stderr.write('[db_selector] setup={} mode={} dbs={}\n'
                     .format(setup_name, 'shared' if is_shared else 'legacy', len(dbs_list)))


if __name__ == "__main__":
    setup_name, run_all_tests, override_set_name, platform_agnostic_tests_group_name_input, add_dbs, remove_dbs = proccess_input_args()

    # Get the dbs shared with the setups of same topo and hwsku, None when they are not shared.
    selected_dbs = get_shared_dbs_list(setup_name)
    is_shared = selected_dbs is not None
    apply_input_filters = True

    if is_shared:
        # Set the set name needed by the control plane filter below.
        setup_set_name = COMMUNITY_SET1_NAME
    else:
        # Get the dbs from the set of the setup.
        setup_set_name, setup_group_name = get_setup_set_and_group(setup_name)
        if not is_setup_exist_in_setups_groups_map(setup_name) and not override_set_name:
            # Run nothing, and skip the filters below.
            selected_dbs = []
            apply_input_filters = False
        else:
            if override_set_name:
                # Use the set given on the command line and run all its dbs.
                setup_set_name = (override_set_name if override_set_name == CANONICAL_NAME
                                  else 'community_' + override_set_name)
                run_all_tests = True
            set_tests_group_map = get_test_group_map_by_regression_set(setup_set_name)
            if run_all_tests:
                # Take all the dbs of the set.
                selected_dbs = get_all_tests_per_set(set_tests_group_map)
            else:
                # Take the dependent dbs plus the agnostic group of the day.
                platform_agnostic_tests_group_name = get_daily_platform_agnostic_tests_group_name(
                    setup_group_name, platform_agnostic_tests_group_name_input)
                selected_dbs = get_tests_list(set_tests_group_map, platform_agnostic_tests_group_name)

    # Apply --add_dbs and --remove_dbs.
    if apply_input_filters:
        selected_dbs = filter_control_tests(selected_dbs, setup_name, setup_set_name)
        selected_dbs = add_remove_dbs_by_input(selected_dbs, add_dbs, remove_dbs)

    # Report on stderr and print the Mars parameter on stdout.
    report_selected_dbs(setup_name, selected_dbs, is_shared)
    print_execution_block_generator_in_mars_format(selected_dbs)
