"""Show what a Mars scenario would really run, as two tables.

Run it before triggering a scenario to check that the configuration is the one expected:

    python3 ngts/scripts/MARS_regression/show_scenario_setups.py --scenario community_regression_side_branch_1

The setup table holds one row per setup: its topo and hwsku, the images, the tarball and the dbs it
executes. A global parameter that was not overridden on a setup, or a value overridden by mistake,
is visible at a glance.

The group table holds one row per topo and hwsku combination: the setups sharing it, the union of
the dbs they execute, and the dbs of the two community sets nobody in the group runs. The setups of
a group split the dbs between them, so only their union says whether the combination is covered.

The dbs of a setup come from three places, the column tells which one was used and they win in this
order:
  1. MARS_ORCH_CALCULATE_PARAMS_COMMAND, which is really executed against the scenario being
     reported, so the column shows what Mars would compute and not a guess
  2. the execution block generator panel of the scenario ui, written to extra_args_inst
  3. the execution blocks of the .setup file, the default read only when neither of the above is set
A setup may hold both of the first two, and the command then wins while the panel is ignored.
"""
import argparse
import ast
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import get_execution_block_generator_extra_params as generator  # noqa: E402
from db_lists_for_nightly_regression import (  # noqa: E402
    COMMUNITY_SET1_TEST_GROUP_MAP,
    COMMUNITY_SET2_TEST_GROUP_MAP,
    MARS_DBS_PATH,
    SCENARIO_JSON_ENV_VAR,
    SCENARIO_SETUP_CONF_OPTION,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Mars writes the json of every scenario here, it is rewritten every time the scenario is saved.
SCENARIOS_DIR = '/auto/MARS/central/etc/conf/jenkins_builder/json_files/scenarios'

# Options of extra_args_inst reported as is, one column each.
SCENARIO_BASE_VERSION_OPTION = '--meinfo_base_version'
SCENARIO_TARGET_VERSION_OPTION = '--meinfo_target_version'
SCENARIO_TARBALL_OPTION = '--meinfo_custom_tarball_name'

# Scenario key holding the command that computes the dbs of a setup, and the option it prints.
CALCULATE_PARAMS_KEY = 'MARS_ORCH_CALCULATE_PARAMS_COMMAND'
EXECUTION_BLOCK_OPTION = '--meinfo_execution_block_generator'

# Tag holding one db of a setup in its .setup file, the default source of the dbs.
SETUP_DBS_TAG = 'tests_dbs_tarball'

# The three places a setup can take its dbs from, reported so that the reader knows which one won.
SCENARIO_SOURCE = 'scenario'
COMMAND_SOURCE = 'command'
CONF_SOURCE = 'conf'
SOURCE_LABELS = {
    SCENARIO_SOURCE: 'set in the scenario',
    COMMAND_SOURCE: 'computed by the command',
    CONF_SOURCE: 'from the .setup file',
}

# Prefix dropped from the db names, anything left with a directory belongs to another one.
COMMUNITY_DBS_PREFIX = 'community/'
DB_SUFFIX = '.db'

UNRESOLVED_VALUE = '<MISSING>'
OPTION_NOT_SET = '-'

# Columns of the first table, one row per setup of the scenario.
# 28 is what it takes for 97% of the setup names to fit on one line, they are the key of this table.
SETUP_TABLE_COLUMNS = [('setup_name', 28), ('topo / hwsku', 26), ('base_image', 30),
                       ('target_image', 30), ('tarball', 26), ('DB', 52)]

# The two community sets hold every db a topo and hwsku combination is expected to cover, in the
# order the sets declare them so that two groups can be compared line by line.
ALL_COMMUNITY_DBS = list(COMMUNITY_SET1_TEST_GROUP_MAP) + [
    db for db in COMMUNITY_SET2_TEST_GROUP_MAP if db not in COMMUNITY_SET1_TEST_GROUP_MAP]

# Columns of the second table, one row per topo and hwsku combination.
# The executed dbs column is wide on purpose, a group covering both sets holds about 550 characters
# of db names. 120 keeps that under 5 lines and makes this table as wide as the one above.
GROUP_TABLE_COLUMNS = [('topo / hwsku', 26), ('setups', 28), ('executed dbs', 120),
                       ('missing dbs', 24)]
NO_MISSING_DB = '(none)'

# Shown above each table, a reader should not have to open this file to know what a table answers.
SETUP_TABLE_INTRO = [
    '[1] What every setup runs',
    '    One row per setup: show the topo / hwsku / images / tarball, and the dbs it executes',
]
GROUP_TABLE_INTRO = [
    '[2] What every topo and hwsku combination covers',
    '    One row per topo and hwsku: the setups sharing it, show the dbs they execute, and not execute.',
    '    It helps the maintainer to see whether the combination is fully covered.',
]
# Both tables are grids of the same width, they need real air between them to be told apart.
TABLES_SEPARATOR = '\n' * 3
# Appended to a setup whose dbs could not be read, its share is missing from the union of the group.
DB_ERROR_MARK = ' <db error>'


# -------------------------------------------------------------------- command line and scenario json


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--scenario', type=str, required=True,
                        help='Mars scenario name, its json is read from {}'.format(SCENARIOS_DIR))
    return parser


def resolve_scenario_json_path(scenario_name):
    """Turn a scenario name into the path of its json, a trailing .json is tolerated."""
    if scenario_name.endswith('.json'):
        scenario_name = scenario_name[:-len('.json')]
    return os.path.join(SCENARIOS_DIR, scenario_name + '.json')


def read_scenario(scenario_json_path):
    """Read the scenario json, or tell why it could not be read and return None.

    The /auto mounts fail sporadically, running the script again is enough and cheaper than hiding
    the failure behind retries.
    """
    try:
        with open(scenario_json_path) as scenario_json_file:
            return json.load(scenario_json_file)
    except (OSError, ValueError) as read_error:
        sys.stderr.write('ERROR: cannot read the scenario json {} ({})\n'
                         .format(scenario_json_path, read_error))
        return None


def iter_dicts(node):
    """Yield every dict of a json document, parents before children and in document order."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from iter_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_dicts(value)


def iter_scenario_setups(scenario):
    """Yield the parameters of every setup of the scenario, in the order Mars declares them.

    A setup is any execution item holding a setup_name. They sit under phases / parallel execution
    today, but walking the whole document says what a setup is instead of where Mars happens to nest
    it, and keeps working if that nesting changes. A setup declared twice is yielded twice, it
    really runs twice.
    """
    for execution_item in iter_dicts(scenario):
        if execution_item.get('skip') in (True, 'true', 'True'):
            continue
        parameters = execution_item.get('parameters')
        if isinstance(parameters, dict) and parameters.get('setup_name'):
            yield parameters


# -------------------------------------------------------------------------- what a single setup runs


def parse_execution_blocks(blocks_text):
    """Turn the value of an execution block generator option into the list of dbs it declares.

    Two formats are in use: the calculate params command prints json, while the scenario ui writes
    python like dicts quoted with single quotes, which json cannot read.
    """
    blocks_text = blocks_text.strip().strip("'")
    try:
        blocks = json.loads(blocks_text)
    except ValueError:
        try:
            blocks = ast.literal_eval(blocks_text)
        except (ValueError, SyntaxError) as parse_error:
            return None, 'cannot parse the execution blocks ({})'.format(parse_error)
    if not isinstance(blocks, list):
        return None, 'the execution blocks are not a list'
    return [block.get('tests_dbs_tarball', '') for block in blocks if isinstance(block, dict)], None


def find_execution_blocks_option(command_output):
    """Return the raw value the calculate params command printed, or None when it printed none."""
    for line in command_output.splitlines():
        line = line.strip()
        if line.startswith(EXECUTION_BLOCK_OPTION):
            return line[len(EXECUTION_BLOCK_OPTION):]
    return None


def get_dbs_from_command(calculate_params_command, scenario_json_path):
    """Really run the calculate params command of a setup and return the dbs it selects.

    The command points at the Mars clone, so only its arguments are kept and the generator sitting
    next to this script is the one run. The generator has no option naming a scenario, Mars hands it
    over through the environment and so do we: without it the generator cannot see the other setups
    of the group and silently falls back to a db list that is not the one Mars would run.
    """
    tokens = shlex.split(calculate_params_command)
    script_index = next((index for index, token in enumerate(tokens) if token.endswith('.py')), None)
    if script_index is None:
        return None, 'no script found in "{}"'.format(calculate_params_command)
    script_name = os.path.basename(tokens[script_index])
    script_path = os.path.join(SCRIPT_DIR, script_name)
    if not os.path.isfile(script_path):
        return None, '{} was not found next to this script'.format(script_name)
    command_env = dict(os.environ, **{SCENARIO_JSON_ENV_VAR: scenario_json_path})
    result = subprocess.run([sys.executable, script_path] + tokens[script_index + 1:],
                            env=command_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    if result.returncode:
        last_error_line = (result.stderr.strip().splitlines() or ['no output'])[-1]
        return None, 'the command failed with {} ({})'.format(result.returncode, last_error_line)
    printed_blocks = find_execution_blocks_option(result.stdout)
    if printed_blocks is None:
        return None, 'the command printed no {}'.format(EXECUTION_BLOCK_OPTION)
    return parse_execution_blocks(printed_blocks)


def get_dbs_from_conf(setup_name, conf_file_name):
    """Return the dbs of the execution blocks of the .setup file of a setup.

    This is what Mars runs when the scenario declares no calculate params command for the setup.
    """
    conf_content = generator.read_setup_conf(setup_name, conf_file_name)
    if conf_content is None:
        return None, 'cannot read the .setup file of {}'.format(setup_name)
    return [db.strip() for db in re.findall(r'<{0}>(.*?)</{0}>'.format(SETUP_DBS_TAG),
                                            conf_content, re.DOTALL)], None


def get_setup_dbs(setup_name, tokens, calculate_params_command, scenario_json_path):
    """Return the dbs of a setup, the error met while reading them, and where they come from.

    The calculate params command wins over the execution block generator panel of the scenario ui,
    which is simply ignored when both are set, and the execution blocks of the .setup file are the
    default used when the scenario sets neither.
    """
    # Get dbs from generate db tool
    if calculate_params_command:
        dbs, error = get_dbs_from_command(calculate_params_command, scenario_json_path)
        return dbs, error, COMMAND_SOURCE

    # Get dbs from execution block generator panel of the Mars scenario UI
    scenario_blocks = generator.get_option_value(tokens, EXECUTION_BLOCK_OPTION)
    if scenario_blocks is not None:
        dbs, error = parse_execution_blocks(scenario_blocks)
        return dbs, error, SCENARIO_SOURCE

    # Get dbs from NFS .setup file
    dbs, error = get_dbs_from_conf(setup_name,
                                   generator.get_option_value(tokens, SCENARIO_SETUP_CONF_OPTION))
    return dbs, error, CONF_SOURCE


def build_setup_report(parameters, scenario_json_path):
    """Collect everything reported for one setup of the scenario."""
    setup_name = parameters['setup_name']
    extra_args_inst = parameters.get('extra_args_inst') or ''
    tokens = shlex.split(extra_args_inst)
    topo, hwsku = generator.resolve_topo_hwsku(setup_name, extra_args_inst)
    dbs, dbs_error, dbs_source = get_setup_dbs(setup_name, tokens,
                                               parameters.get(CALCULATE_PARAMS_KEY),
                                               scenario_json_path)
    return {
        'setup_name': setup_name,
        'topo': topo,
        'hwsku': hwsku,
        'base_image': generator.get_option_value(tokens, SCENARIO_BASE_VERSION_OPTION),
        'target_image': generator.get_option_value(tokens, SCENARIO_TARGET_VERSION_OPTION),
        'tarball': generator.get_option_value(tokens, SCENARIO_TARBALL_OPTION),
        'dbs': dbs,
        'dbs_error': dbs_error,
        'dbs_source': dbs_source,
    }


def sort_setups_by_topo_hwsku_and_name(setup_reports):
    """Sort by topo, then hwsku, then setup name, so that the setups of a group end up adjacent.

    A setup whose topo or hwsku could not be resolved sorts last, it is a configuration hole.
    """
    return sorted(setup_reports,
                  key=lambda report: (not report['topo'], report['topo'] or '',
                                      not report['hwsku'], report['hwsku'] or '',
                                      report['setup_name']))


# ---------------------------------------------------------- what a topo and hwsku combination covers


def normalize_db_path(db_path):
    """Drop the tarball folder, so that a db of a scenario matches the keys of the community sets."""
    return db_path[len(MARS_DBS_PATH):] if db_path.startswith(MARS_DBS_PATH) else db_path


def group_setups_by_topo_and_hwsku(reports):
    """Gather the setups sharing a topo and a hwsku, and the dbs they cover together.

    The setups of a group split the dbs between them, so only the union of their lists tells whether
    the combination really runs everything. The reports are already sorted, so the groups are too.
    """
    groups = {}
    ordered_keys = []
    for report in reports:
        group_key = (report['topo'], report['hwsku'])
        if group_key not in groups:
            groups[group_key] = {'topo': report['topo'], 'hwsku': report['hwsku'],
                                 'setups': [], 'dbs': set()}
            ordered_keys.append(group_key)
        group = groups[group_key]
        group['setups'].append(report['setup_name'] +
                               (DB_ERROR_MARK if report['dbs_error'] else ''))
        group['dbs'].update(normalize_db_path(db) for db in report['dbs'] or [])
    return [groups[group_key] for group_key in ordered_keys]


# --------------------------------------------------------------------------------------- table cells


def db_display_name(db_path):
    """Drop from a db path everything the dbs have in common, only community dbs lose their folder."""
    name = db_path[len(MARS_DBS_PATH):] if db_path.startswith(MARS_DBS_PATH) else db_path
    name = name[:-len(DB_SUFFIX)] if name.endswith(DB_SUFFIX) else name
    return name[len(COMMUNITY_DBS_PREFIX):] if name.startswith(COMMUNITY_DBS_PREFIX) else name


def build_db_list_cell(dbs, empty_text):
    """Render a db list as its names followed by their count, or a single text when it is empty."""
    if not dbs:
        return [empty_text]
    return [', '.join(db_display_name(db) for db in dbs), '({})'.format(len(dbs))]


def build_setup_db_cell(report):
    """Render the dbs of a setup, followed by their count and the source they were read from."""
    label = SOURCE_LABELS[report['dbs_source']]
    if report['dbs_error']:
        return ['<ERROR, {}: {}>'.format(label, report['dbs_error'])]
    if not report['dbs']:
        return ['<no db> ({})'.format(label)]
    return [', '.join(db_display_name(db) for db in report['dbs']),
            '({}, {})'.format(len(report['dbs']), label)]


def build_setup_row_cells(report):
    """Turn a setup report into the cells of its row, every cell being a list of logical lines."""
    return [
        [report['setup_name']],
        [report['topo'] or UNRESOLVED_VALUE, report['hwsku'] or UNRESOLVED_VALUE],
        [report['base_image'] or OPTION_NOT_SET],
        [report['target_image'] or OPTION_NOT_SET],
        [report['tarball'] or OPTION_NOT_SET],
        build_setup_db_cell(report),
    ]


def build_group_row_cells(group):
    """Turn a group into the cells of its row, the dbs keeping the order of the community sets."""
    executed_dbs = [db for db in ALL_COMMUNITY_DBS if db in group['dbs']]
    executed_dbs += sorted(db for db in group['dbs'] if db not in ALL_COMMUNITY_DBS)
    missing_dbs = [db for db in ALL_COMMUNITY_DBS if db not in group['dbs']]
    return [
        [group['topo'] or UNRESOLVED_VALUE, group['hwsku'] or UNRESOLVED_VALUE],
        group['setups'],
        build_db_list_cell(executed_dbs, '<no db>'),
        build_db_list_cell(missing_dbs, NO_MISSING_DB),
    ]


# ------------------------------------------------------------------------------ rendering the tables


def wrap_cell(cell_lines, width):
    wrapped_lines = []
    for line in cell_lines:
        if not line:
            wrapped_lines.append('')
            continue
        wrapped_lines.extend(textwrap.wrap(line, width, break_long_words=True,
                                           break_on_hyphens=False) or [''])
    return wrapped_lines


def render_row(cells, widths):
    wrapped_cells = [wrap_cell(cell, width) for cell, width in zip(cells, widths)]
    row_height = max(len(cell) for cell in wrapped_cells)
    rendered_lines = []
    for line_index in range(row_height):
        parts = [' {} '.format((cell[line_index] if line_index < len(cell) else '').ljust(width))
                 for cell, width in zip(wrapped_cells, widths)]
        rendered_lines.append('|' + '|'.join(parts) + '|')
    return rendered_lines


def render_table(rows, columns):
    widths = [width for _, width in columns]
    separator = '+' + '+'.join('-' * (width + 2) for width in widths) + '+'
    rendered_lines = [separator]
    rendered_lines += render_row([[title] for title, _ in columns], widths)
    rendered_lines.append(separator)
    for cells in rows:
        rendered_lines += render_row(cells, widths)
        rendered_lines.append(separator)
    return '\n'.join(rendered_lines)


def render_table_intro(intro_lines):
    """Put the title and the explanation of a table above it, followed by a blank line."""
    return '\n'.join(list(intro_lines) + [''])


def render_header(scenario_json_path, setups_count):
    """Tell which scenario was read and how many setups it holds."""
    return '\n'.join(['Scenario json : {}'.format(scenario_json_path),
                      'Setups        : {}'.format(setups_count),
                      ''])


# --------------------------------------------------------------------------------------- entry point


def main():
    args = build_arg_parser().parse_args()
    scenario_json_path = resolve_scenario_json_path(args.scenario)

    # Read the scenario Mars would run.
    scenario = read_scenario(scenario_json_path)
    if scenario is None:
        return 1

    # Collect the setups info.
    setups_parameters = list(iter_scenario_setups(scenario))
    if not setups_parameters:
        sys.stderr.write('ERROR: no setup found in {}\n'.format(scenario_json_path))
        return 1

    # Resolve what every setup runs: topo, hwsku, images, tarball and dbs.
    setup_reports = sort_setups_by_topo_hwsku_and_name(
        [build_setup_report(parameters, scenario_json_path) for parameters in setups_parameters])

    # Gather the setups sharing a topo and a hwsku, and the dbs they cover together.
    groups = group_setups_by_topo_and_hwsku(setup_reports)

    # Draw the two tables.
    print(render_header(scenario_json_path, len(setup_reports)))
    print(render_table_intro(SETUP_TABLE_INTRO))
    print(render_table([build_setup_row_cells(report) for report in setup_reports],
                       SETUP_TABLE_COLUMNS))
    print(TABLES_SEPARATOR)
    print(render_table_intro(GROUP_TABLE_INTRO))
    print(render_table([build_group_row_cells(group) for group in groups],
                       GROUP_TABLE_COLUMNS))
    return 0


if __name__ == '__main__':
    sys.exit(main())
