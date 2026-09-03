import json
import re
import pytest
import allure
import os
import logging
import time
from typing import Optional
from types import SimpleNamespace
from datetime import datetime
from ngts.helpers.performance.performance_setup_helpers import (configure_mloops, deploy_burst_config, stop_traffic,
                                                                unsplit_all_ports, get_is_simx,
                                                                _build_default_port_group_df)
from ngts.helpers.performance.performance_recovery import (
    enforce_fatal_session_exit_status, exit_if_performance_environment_fatal,
    record_performance_test_failure, reset_performance_session_state,
    run_guarded_operation)
from ngts.helpers.performance.Performance_log_print import print_players_logs, remove_players_logs
from ngts.constants.constants import FatalStateConsts, PytestConst
from ngts.constants.performance_constants import MongoDbConsts, RailModel, PerfConsts, ValidationConsts
from ngts.helpers.performance.burst_config import BurstConfig
from ngts.helpers.performance.performance_db_helpers import (create_performance_db_template,
                                                             create_test_validation_entry_to_db,
                                                             add_test_mongo_metadata, get_perf_test_name)
from ngts.helpers.thread_log_filter import config_root_logger
from ngts.performance_tests.log_files.players_logger import (init_players_cmd_logger,
                                                             get_players_cmd_log_paths,
                                                             PLAYERS_CMD_LOGGER_NAME)
from ngts.helpers.performance.port_selection import (set_cli_port_selection_options,
                                                     get_cli_port_selection_options,
                                                     port_selection_was_activated,
                                                     DEFAULT_PORT_SELECTION_CONFIG)
from devts.infra.tools.exceptions.test_issue import TestIssue

logger = logging.getLogger()

PLAYERS_CMD_LOG_PREFIX = "players_commands"
_players_cmd_log_run_timestamp = None


def pytest_sessionstart(session):
    """Reset process-local performance recovery state for this pytest session."""
    reset_performance_session_state()


def pytest_runtest_setup(item):
    """Abort before the next performance test after a fatal environment failure."""
    exit_if_performance_environment_fatal(
        pytest.exit, FatalStateConsts.FATAL_STATE_RETURN_CODE)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Track failed setup/call items for class-scoped restore exception handling."""
    outcome = yield
    report = outcome.get_result()
    if report.when in ("setup", "call") and report.failed:
        record_performance_test_failure(item.nodeid)


def pytest_configure(config):
    """Capture port-selection options and stamp the players-command log run timestamp.

    Port-selection options are stored at module scope in ``port_selection`` so resolution
    works regardless of fixture ordering — in particular when a class-scoped
    ``basic_setup_configuration`` applies the config before any function-scoped fixture would
    run. No-op behavior unless ``--perf-exclude-ports`` / ``--perf-include-ports`` is supplied.

    One players-command log timestamp is stamped for the whole session so every file the run
    produces shares it and the run's files can be selected as a group.
    """
    global _players_cmd_log_run_timestamp
    _players_cmd_log_run_timestamp = datetime.now().strftime(MongoDbConsts.TIME_REGEX_FORMAT_FOR_MONGO_DB)

    set_cli_port_selection_options(
        setup_name=config.getoption("--setup_name", default=None),
        exclude_enabled=config.getoption("--perf-exclude-ports", default=False),
        include_enabled=config.getoption("--perf-include-ports", default=False),
        config_path=config.getoption("--perf-ports-config", default=None),
    )
    opts = get_cli_port_selection_options()
    if opts["exclude_enabled"] or opts["include_enabled"]:
        logger.info("PORT SELECTION ENABLED via CLI: exclude=%s include=%s config=%s (setup=%s)",
                    opts["exclude_enabled"], opts["include_enabled"],
                    opts["config_path"] or DEFAULT_PORT_SELECTION_CONFIG, opts["setup_name"])


def pytest_runtest_setup(item):
    """Redirect the players-command log to a per-test file, before any of the test's fixtures.

    A conftest hookimpl runs ahead of pytest's own ``pytest_runtest_setup``, which is what
    triggers fixture setup, so this lands before every fixture for the item at every scope.
    That matters for the class-scoped ``basic_setup_configuration`` most suites use: it runs
    before any function-scoped fixture, so a fixture-based switch would attribute its
    configuration commands to the previous test.

    The name matches the test's MongoDB ``test_name`` so a log file and a DB record can be
    correlated; ``get_perf_test_name`` reads only ``request.node``.
    """
    init_players_cmd_logger(_players_cmd_log_run_timestamp,
                            f"{PLAYERS_CMD_LOG_PREFIX}_{get_perf_test_name(SimpleNamespace(node=item))}")


def pytest_sessionfinish(session, exitstatus):
    """Warn loudly if port selection was requested on the CLI but never took effect.

    This makes the "flag set but silently did nothing" failure mode impossible to miss (e.g.
    the test path never applied configuration, or the config file lacked an entry).
    """
    opts = get_cli_port_selection_options()
    if (opts["exclude_enabled"] or opts["include_enabled"]) and not port_selection_was_activated():
        logger.warning("PORT SELECTION WAS REQUESTED (--perf-exclude-ports/--perf-include-ports) "
                       "BUT NEVER BECAME ACTIVE this session. Check that: (1) the updated code is "
                       "deployed, (2) the test applies performance configuration, and (3) the "
                       "config file has an entry for setup '%s' and the test's scenario.",
                       opts["setup_name"])
    enforce_fatal_session_exit_status(
        session, FatalStateConsts.FATAL_STATE_RETURN_CODE)


@pytest.fixture(scope="session", autouse=True)
def config_root_logger_fixture():
    config_root_logger()
    # config_root_logger runs dictConfig, which defaults to disable_existing_loggers=True and so
    # switches off the players-command logger already created for the first test. Later tests
    # re-enable it on init; this keeps the first test's commands from being dropped.
    logging.getLogger(PLAYERS_CMD_LOGGER_NAME).disabled = False


@pytest.fixture(scope="session")
def performance_parameters(request):
    """
    Method for getting the file location of the parameters from nv_optimizer
    """
    if request.config.getoption('--parameter_file_location'):
        try:
            # Convert params string to JSON object
            file_location = request.config.getoption('--parameter_file_location')
            with open(file_location, 'r') as file:
                params_json = json.load(file)
            return params_json["parameter_set"]
        except json.JSONDecodeError as e:
            raise TestIssue(f"Failed to parse params as JSON: {type(e).__name__}: {e}") from e
    return False


@pytest.fixture(scope="session")
def cleanup(request):
    """
    Method for getting base version from pytest arguments
    :param request: pytest builtin
    :return: app_extension_dict
    """
    if request.config.getoption(PytestConst.run_cleanup_only_arg):
        return True
    return False


@pytest.fixture(scope="session")
def init(request):
    """
    Method for getting base version from pytest arguments
    :param request: pytest builtin
    :return: app_extension_dict
    """
    if request.config.getoption(PytestConst.run_config_only_arg):
        return True
    return False


@pytest.fixture(scope='session', autouse=True)
def power_thresholds_by_chip_type(chip_type):
    return RailModel.asic_total_threshold(chip_type)


@pytest.fixture(scope="session")
def is_ipv6(request):
    """
    Method for getting base version from pytest arguments
    :param request: pytest builtin
    :return: app_extension_dict
    """
    if request.config.getoption('--is_ipv6'):
        return True
    return False


@pytest.fixture(scope="function")
def burst_config():
    """No bonus port burst by default.

    Bonus port burst is opt-in per test suite: a suite that supports it overrides this fixture and builds
    the configuration from the burst its scenario declares. Keeping the default here lets
    basic_test_configuration stay burst aware for every performance test without each of them declaring one.
    """
    return None


@pytest.fixture(scope='function', autouse=True)
def basic_test_configuration(request, players, basic_setup_configuration, burst_config: Optional[BurstConfig]):
    mloops_configured = False
    try:
        if burst_config:
            with allure.step('Copy the burst configuration to the Traffic Generators'):
                deploy_burst_config(players, burst_config, parallel_run=False)
        with allure.step('Configure Mloops on Traffic Generators'):
            run_guarded_operation(
                players,
                lambda: configure_mloops(players, is_simx=get_is_simx(players), burst_config=burst_config),
                nodeid=request.node.nodeid)
            mloops_configured = True
        yield
        if mloops_configured:
            with allure.step('Stop Traffic on Traffic Generators'):
                run_guarded_operation(
                    players, lambda: stop_traffic(players, burst_config=burst_config),
                    nodeid=request.node.nodeid)
    except Exception as e:
        raise e
    finally:
        test_was_skipped = request.node.rep_call.skipped if hasattr(request.node, 'rep_call') else False
        if not test_was_skipped:
            with allure.step(f"Attaching Players Logs to Allure"):
                print_players_logs(print_to_stdout=True, players_info=players)
                remove_players_logs()
            players_cmd_log_paths = get_players_cmd_log_paths()
            if players_cmd_log_paths:
                with allure.step("Attaching Players Commands Log to Allure"):
                    for players_cmd_log_path in players_cmd_log_paths:
                        attachment_name = f"{PLAYERS_CMD_LOGGER_NAME}:{os.path.basename(players_cmd_log_path)}"
                        allure.attach.file(players_cmd_log_path, name=attachment_name,
                                           attachment_type=allure.attachment_type.TEXT)


@pytest.fixture(scope='function', autouse=True)
def os_ports_name_mapping_df(request, players, basic_setup_configuration):
    os_ports_name_mapping_df = players['dut']['cli'].performance.get_os_ports_name_mapping()
    test_name = get_perf_test_name(request)
    add_test_mongo_metadata(test_name,
                            {ValidationConsts.OS_PORTS_NAME_MAPPING_DATAFRAME:
                                os_ports_name_mapping_df})
    return os_ports_name_mapping_df


@pytest.fixture(scope='session', autouse=True)
def create_mongo_db_template_file(players, session_id, setup_name):
    create_performance_db_template(players, session_id, setup_name)


@pytest.fixture(scope='session', autouse=True)
def cleanup_shared_json_file(players):
    players[PerfConsts.DUT_ALIAS]['cli'].performance.cleanup_shared_json_file()


@pytest.fixture(scope='function', autouse=True)
def update_test_data_in_mongo_db(request, players):
    test_name = None
    try:
        test_name = get_perf_test_name(request)
        time_now = datetime.now().strftime(MongoDbConsts.TIME_REGEX_FORMAT)
        add_test_mongo_metadata(test_name, {MongoDbConsts.TEST_NAME: test_name,
                                            MongoDbConsts.TIME_STAMP: time_now})
        yield
    except Exception as e:
        raise e
    finally:
        if test_name is None:
            test_name = get_perf_test_name(request)
        if re.search('optimize', test_name, re.IGNORECASE):
            return
        # rep_call is None when setup failed/skipped before the test body ran;
        # fall back to rep_setup in that case. rep_teardown is not yet set here.
        rep_call = getattr(request.node, 'rep_call', None)
        rep_setup = getattr(request.node, 'rep_setup', None)
        report = rep_call if rep_call is not None else rep_setup
        if report is not None:
            if getattr(report, 'failed', False):
                test_state = "failed"
                longrepr = getattr(report, 'longrepr', "")
                crash_report = getattr(longrepr, 'reprcrash', None)
                state_info = getattr(crash_report, 'message', None) or str(longrepr)
            elif getattr(report, 'skipped', False):
                test_state = "skipped"
                longrepr = getattr(report, 'longrepr', "")
                if isinstance(longrepr, tuple) and len(longrepr) >= 3:
                    state_info = str(longrepr[2])
                else:
                    state_info = str(longrepr)
            else:
                test_state = "passed"
                state_info = ""
            add_test_mongo_metadata(test_name, {MongoDbConsts.TEST_STATE: test_state,
                                                MongoDbConsts.STATE_INFO: state_info})
        create_test_validation_entry_to_db(players, test_name)


def get_all_players_ports(players, right_split_num=1, left_split_num=1):
    """
    Retrieves port configurations for all players in the performance test setup.

    This function collects port information from both the Device Under Test (DUT)
    and Traffic Generators (TG), applying the specified split configurations.

    Args:
        players (dict): Dictionary containing all players' information and their CLI interfaces
        right_split_num (int, optional): Number of splits for right-side ports. Defaults to 1
        left_split_num (int, optional): Number of splits for left-side ports. Defaults to 1

    Returns:
        dict: Dictionary with player names as keys and their port configurations as values.
              Format: {
                  'player_name': {
                      'left_ports': [...],
                      'right_ports': [...]
                  }
              }
    """
    all_ports_after_split = {}
    for player in PerfConsts.PERF_SETUP_PLAYERS_ALIASES:
        if player == PerfConsts.DUT_ALIAS:
            all_ports_after_split[player] = players[player]['cli'].performance.get_split_ports(
                right_split_num,
                left_split_num
            )
        else:
            split_num = left_split_num if player == PerfConsts.LEFT_TG_ALIAS else right_split_num
            all_ports_after_split[player] = players[player]['cli'].performance.get_player_unconnected_connected_after_split(
                split_num
            )
    return all_ports_after_split


@pytest.fixture(scope='function', autouse=False)
def port_group_df(request, players, basic_setup_configuration, conf_args=None):
    """
    Pytest fixture that creates a port group configuration dataframe.

    This fixture generates a list of dictionaries containing port configurations
    for the Device Under Test (DUT), organizing ports into relevant port groups.
    It can handle both standard DUT port groups and custom port group configurations.

    Args:
        request: Pytest request object
        players (dict): Dictionary containing all players' information
        conf_args (dict, optional): Configuration arguments containing custom port groups

    Returns:
        list: List of dictionaries with port configurations.
              Format: [
                  {
                      "port": <sdk_port>,
                      "port_group_name": "left_ports|right_ports|port_group_name"
                  },
                  ...
              ]
    """
    dut_performance = players[PerfConsts.DUT_ALIAS]['cli'].performance
    return _build_default_port_group_df(dut_performance)


@pytest.fixture(scope='session', autouse=True)
def sdk_branch(players):
    """
    Returns the SDK branch currently running on the DUT.
    """
    general_cli = players['dut']['cli'].general
    sdk_version = general_cli.get_sdk_version()
    return general_cli.get_sdk_branch(sdk_version)


@pytest.fixture(scope='session', autouse=True)
def fix_tg_cli_objects_alias_keys(cli_objects, topology_obj):
    """
    Patch cli_objects so traffic generators are changed back to their hyphenated alias (e.g. 'left-tg')
    instead of the underscore key from DottedDict(e.g. 'left_tg').
    """
    for player_alias in topology_obj.players:
        if re.match(PerfConsts.TG_REGEX, player_alias):
            underscore_key = player_alias.replace('-', '_')
            if underscore_key in cli_objects.__dict__:
                del cli_objects.__dict__[underscore_key]
            cli_objects.__dict__[player_alias] = topology_obj.players[player_alias]['cli']


@pytest.fixture(scope='session', autouse=True)
def unsplit_all_ports_on_spc5_6(players):
    """
    Unsplit all ports on SPC5/SPC6.
    """
    unsplit_all_ports(players, step="unsplit_all_ports_on_spc5_6 - unsplit_all_ports")
    return
