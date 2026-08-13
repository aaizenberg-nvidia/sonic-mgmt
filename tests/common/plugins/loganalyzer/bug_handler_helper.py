import os
import re
import json
import logging
from pathlib import Path
import time
from paramiko.ssh_exception import SSHException
import allure as raw_allure
import pytest
from abc import ABC, abstractmethod
from datetime import timedelta

from tests.common.helpers.parallel import parallel_run
from devts.infra.tools.redmine.redmine_api import REDMINE_ISSUES_URL
from tests.common.plugins.allure_wrapper import allure_step_wrapper as allure
from ngts.constants.constants import BugHandlerConst, InfraConst, NvosCliTypes, REGRESSION_TYPE_ENV_VAR, RegressionType
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.helpers.bug_handler.bug_handler_helper import create_session_tmp_folder, clear_files, bug_handler_wrapper_err_msg, \
    create_log_analyzer_yaml_file, deduplicate_log_errors, group_log_errors_by_timestamp, summarize_la_bug_handler
from ngts.scripts.allure_reporter import predict_allure_report_link
from tests.common.helpers.parallel import reset_ansible_local_tmp

logger = logging.getLogger()
PYTEST_RUN_CMD = 'pytest_run_cmd'
PI_LINK = "https://app.powerbi.com/groups/9b79a1d8-7408-4848-90c5-9dd5dab8493d/reports/89874ebf-554e-45be-b941-f4966fdda0ae/ReportSectionbb345ebc8fb547a45dfd?experience=power-bi"

# inject dut hostname into log file name to avoid collision
LOG_ANALYZER_LOG_FILE = '/tmp/loganalyzer-[{0}].log'
KEY_IS_TEST_FUNCTION_FAILED = "is_test_function_failed"
PRIVATE_BRANCHES_TO_ENABLE_BUG_HANDLER_LIST = ["smart-switch-master",
                                               "master_rc",
                                               "master_rc_chamelleon",
                                               "master_spc6",
                                               "master_spc6_es",
                                               "master_rc_integration",
                                               "master_rc_trixie",
                                               "multi-asic",
                                               "master_rc_chameleon6",
                                               "master_ha_nv"]

class BugHandler(ABC):
    @abstractmethod
    def bug_handler_wrapper(self, analyzers, duthosts, la_results):
        pass


class ConsolidatedBugHandler(BugHandler):
    def bug_handler_wrapper(self, analyzers, duthosts, la_results):
        """
        Consolidated bug handler.
        This handler will consolidate the loganalyzer results from all the DUTs
        """
        bug_handler_wrapper(analyzers, duthosts, la_results)


class NoOpBugHandler(BugHandler):
    """No operation bug handler"""
    def bug_handler_wrapper(self, analyzers, duthosts, la_results):
        pass


def get_bughandler_instance(kwargs: dict) -> BugHandler:
    if isinstance(kwargs, dict):
        bh_type = kwargs.get("type", "noop")
        if bh_type in {"consolidated", "default"}:
            return ConsolidatedBugHandler()
    return NoOpBugHandler()

def handle_log_analyzer_errors(cli_type, branch, test_name, duthost, log_analyzer_bug_metadata, testbed,
                               bug_handler_action, log_errors_dir_path=None, is_serial_log=False, logger=None):
    """
    Call bug handler on all log errors and return a list of dictionaries with results and a list of the LA errors caught
    :param cli_type: i.e, Sonic
    :param branch: i.e 202211
    :param test_name: i.e, test_lags_scale
    :param duthost: duthost object
    :param log_analyzer_bug_metadata: dictionary with info that we want to add to log analyzer bug
    :param testbed: testbed
    :param bug_handler_action: dictionary include if need to create or update the RM issue when err msg found.
    :param logger: logger instance to use for logging (optional, defaults to module logger)
    :return: A tuple of two values. The first value is a list of dictionaries with results for each optional bug:
    i.e., ['test_name': 'test_lags_scale',
            'results':
    [{'file_name': '2022-06-21_23-24-07_orchagent-asan.log.40',
    'messages': ['INFO:handle_bug:reading configuration from', ...],
    'rc': 0,
    'decision': 'update'},...]
    },...]
            The second value is a list of LA errors that happened in the test:
     i.e., ['May 12 06:57:50.560887 r-tigon-04 ERR admin: This is An Error #1',
     'May 12 06:57:50.857573 r-tigon-04 ERR admin: Some Error #2']
    """
    # Use passed logger or fall back to module logger
    if logger is None:
        logger = logging.getLogger()

    with allure.step("Log Analyzer bug handler"):
        la_errors = []
        bug_handler_dumps_results = []
        hostname = duthost.hostname if duthost else "custom"
        if not log_errors_dir_path:
            log_errors_dir_path = Path(BugHandlerConst.LOG_ERRORS_DIR_PATH.format(hostname=hostname))

        try:
            session_id = os.environ.get(InfraConst.ENV_SESSION_ID)
            if not session_id:
                timestamp = time.strftime("%Y-%m-%d-%H:%M:%S", time.gmtime())
                session_id = f"manual_run_{timestamp}"
            session_tmp_folder = create_session_tmp_folder(session_id)
            redmine_project = BugHandlerConst.CLI_TYPE_REDMINE_PROJECT[cli_type]
            if log_analyzer_bug_metadata.get(KEY_IS_TEST_FUNCTION_FAILED, False) and cli_type == "Sonic":
                redmine_project = "SONiC-Verification"
            conf_path = BugHandlerConst.BUG_HANDLER_CONF_FILE[redmine_project]

            bug_handler_create_action = bug_handler_action.get("create", False)
            bug_handler_update_action = bug_handler_action.get("update", False)
            bug_handler_no_action = not bug_handler_create_action and not bug_handler_update_action
            logger.info(f"Run bug handler in no action mode: {bug_handler_no_action}")

            bug_handler_params = {"duthost": duthost,
                                  "testbed": testbed,
                                  "cli_type": cli_type,
                                  "session_id": session_id,
                                  "test_name": test_name}

            list_of_bugs_ids = []
            for log_errors_file_path in log_errors_dir_path.iterdir():
                with log_errors_file_path.open("r") as log_errors_file:
                    data = json.load(log_errors_file)
                logger.info(f"Handling the err msg: {data}")
                log_errors = data.get(BugHandlerConst.LOG_ERRORS_FILE_ROOT_ITEM, "")
                la_errors.extend([line for line in log_errors.splitlines() if line.strip()])
                error_groups = group_log_errors_by_timestamp(log_errors)
                if cli_type == "Sonic":
                    # De-duplicate error messages to handle log flooding with similar/same errors in periodic patterns
                    logger.info(f"[BEFORE] {len(error_groups)} error groups")
                    logger.info(f"[BEFORE] Number of errors in each group: {[len(group) for group in error_groups]}")
                    error_groups = deduplicate_log_errors(error_groups)
                    logger.info(f"[AFTER] {len(error_groups)} error groups")
                    logger.info(f"[AFTER] Number of errors in each group: {[len(group) for group in error_groups]}")
                log_errors_file_path.unlink()

                for trace_number, error_group in enumerate(error_groups, 1):
                    with allure.step(f"Handling trace number {trace_number} of {len(error_groups)}"):
                        yaml_file_path = create_log_analyzer_yaml_file(error_group, session_tmp_folder, redmine_project,
                                                                    test_name, hostname,
                                                                    log_analyzer_bug_metadata, bug_handler_params,
                                                                    bug_handler_dumps_results, is_serial_log)
                        logger.info(f"yaml_file_path: {yaml_file_path}")
                        logger.info(f"{yaml_file_path} exists?: {os.path.exists(yaml_file_path)}")
                        if yaml_file_path:
                            with allure.step("Run Bug Handler on Log Analyzer error"):
                                start_time = time.perf_counter()
                                # logger.info(f"Run Bug Handler on Log Analyzer error: {error_group}")
                                error_dict = {BugHandlerConst.LA_ERROR: error_group}
                                error_dict.update(
                                    bug_handler_wrapper_err_msg(
                                        conf_path,
                                        redmine_project,
                                        branch,
                                        yaml_file_path,
                                        BugHandlerConst.BUG_HANDLER_LOG_ANALYZER_USER,
                                        BugHandlerConst.BUG_HANDLER_SCRIPT.get(redmine_project, BugHandlerConst.BUG_HANDLER_SCRIPT["default"]),
                                        bug_handler_action,
                                        bug_handler_params
                                    )
                                )
                                bug_handler_dumps_results.append(error_dict)
                                list_of_bugs_ids.append(error_dict.get(BugHandlerConst.BUG_HANDLER_BUG_ID, ''))
                                logger.info(f"Bug handler took {timedelta(seconds=time.perf_counter() - start_time)} seconds for trace number {trace_number}")
        except Exception as err:
            logger.error("Bug handler failed")
            raise err
        logger.info(f"List of bugs ids: {list_of_bugs_ids}")
        return summarize_la_bug_handler(bug_handler_dumps_results, bug_handler_action), la_errors


def skip_bug_handler(duthost, request, logger=logger):
    """
    return True if the bug handler will be skipped.
    """
    hostname = duthost.hostname
    log_errors_dir_path = Path(BugHandlerConst.LOG_ERRORS_DIR_PATH.format(hostname=hostname))

    def _skip_bug_handler(duthost, request):
        if not request:
            logger.warning("Skip the loganalyzer bug handler, To run the it, "
                           "'request' is needed when create LogAnalyzer")
            return True

        if not (log_errors_dir_path.exists() and len(list(log_errors_dir_path.iterdir())) > 0):
            logger.warning(f"Skip the loganalyzer bug handler: No err msg detected")
            return True

        log_analyzer_handler_info = get_log_analyzer_handler_info(duthost)
        if log_analyzer_handler_info['branch'] in BugHandlerConst.BUG_HANDLER_SKIP_BRNACH:
            logger.warning(f"Skip the loganalyzer bug handler for branch: {log_analyzer_handler_info['branch']}")
            return True

        bug_handler_actions = get_bug_handler_actions(request, log_analyzer_handler_info)
        if not is_log_analyzer_bug_handler_enabled(bug_handler_actions):
            logger.warning("Skip the loganalyzer bug handler since it is not enabled")
            return True

        return False

    if _skip_bug_handler(duthost, request):
        if log_errors_dir_path.exists():
            for log_errors_file in log_errors_dir_path.iterdir():
                log_errors_file.unlink()
        return True
    return False


def log_analyzer_bug_handler(duthost, request, log_errors_dir_path=None,
                             only_check=False, is_serial_log=False,
                             is_test_function_failed=False, logger=None):
    """
    If the run_log_analyzer_bug_handler is True, run this function to handle the err msg detected in the loganalyzer
    """
    # Use passed logger or fall back to module logger
    if logger is None:
        logger = logging.getLogger()

    test_name = re.sub(r'[\\/\'"<>|]', '_', request.node.name)
    la_rm_issues = request.session.config.cache.get(BugHandlerConst.LA_RM_ISSUES_DICT, dict())
    test_id = request.node.nodeid
    test_rm_issues = set()
    log_analyzer_handler_info = get_log_analyzer_handler_info(duthost)
    bug_handler_actions = get_bug_handler_actions(request, log_analyzer_handler_info, only_check)

    if "allure_server_project_id" in request.config.option:
        allure_project = request.config.getoption('--allure_server_project_id')
        allure_report_url = predict_allure_report_link(InfraConst.ALLURE_SERVER_URL, allure_project)
    else:
        current_time = str(time.time()).replace('.', '')
        request.session.config.option.allure_server_project_id = current_time
        allure_report_url = \
            f"{InfraConst.ALLURE_SERVER_URL}/allure-docker-service/projects/{current_time}/reports/1/index.html"

    logger.info("--------------- Start Log Analyzer Bug Handler ---------------")
    # for community test case, it has --testbed, for canonical test cases, it has --setup_name
    if "setup_name" in request.config.option:
        setup_name = request.config.getoption('--setup_name')
    else:
        setup_name = request.config.getoption('--testbed')

    system_type = duthost.facts['hwsku']
    pytest_cmd_args = get_pytest_cmd(request, log_analyzer_handler_info['cli_type'])
    bug_handler_dict = {'test_description': request.node.function.__doc__,
                        'pytest_cmd_args': pytest_cmd_args,
                        'system_type': system_type,
                        'detected_in_version': log_analyzer_handler_info['version'],
                        'setup_name': setup_name,
                        'report_url': allure_report_url,
                        'powerbi_url': PI_LINK,
                        KEY_IS_TEST_FUNCTION_FAILED: _is_test_function_failed(request)}

    if "components" in log_analyzer_handler_info:
        bug_handler_dict["components"] = log_analyzer_handler_info["components"]

    cli_type = log_analyzer_handler_info['cli_type']
    if cli_type == 'NVUE':
        bug_handler_dict.update(get_nvue_additional_info(duthost, request))
    log_analyzer_res, la_error_messages = handle_log_analyzer_errors(cli_type, log_analyzer_handler_info['branch'], test_name, duthost,
                                                  bug_handler_dict, setup_name, bug_handler_actions,
                                                  log_errors_dir_path, is_serial_log, logger=logger)
    logger.info(f"Log Analyzer result: {json.dumps(log_analyzer_res, indent=2)}")
    error_msg = ''
    ci_mode = bug_handler_actions.get('ci_mode', False)
    if ci_mode:
        logger.info("CI Mode is enabled: UPDATE decisions in NO_ACTION_MODE will not fail the test")
    if log_analyzer_res[BugHandlerConst.NO_ACTION_MODE]:
        # Collect errors that should fail the test
        failing_errors = []
        for err_with_no_action in log_analyzer_res[BugHandlerConst.NO_ACTION_MODE]:
            action = err_with_no_action.get(BugHandlerConst.BUG_HANDLER_ACTION, '')
            bug_id = err_with_no_action.get(BugHandlerConst.BUG_HANDLER_BUG_ID, '')
            err_logs = err_with_no_action[BugHandlerConst.LA_ERROR]
            # Handle based on decision type and CI mode
            if action == BugHandlerConst.BUG_HANDLER_DECISION_CREATE:
                # CREATE decisions always fail the test (cannot create in no_action mode)
                failing_errors.append((None, err_logs))  # No bug_id for CREATE decisions
            elif action == BugHandlerConst.BUG_HANDLER_DECISION_UPDATE:
                # UPDATE decisions: check CI mode
                if ci_mode:
                    # In CI mode, UPDATE decisions pass (known issues)
                    logger.info(f"CI mode: Relative bug #{bug_id} detected for err logs: {err_logs} - Test will pass")
                    if bug_id:
                        test_rm_issues.add(bug_id)
                    # Do not add to failing_errors
                else:
                    # Non-CI mode: UPDATE decisions fail the test
                    failing_errors.append((bug_id, err_logs))
                    if bug_id:
                        test_rm_issues.add(bug_id)
            elif action == BugHandlerConst.BUG_HANDLER_DECISION_SKIP:
                # SKIP decisions don't fail the test
                logger.info(f"SKIP decision: Bug #{bug_id} for err logs: {err_logs} - Test will pass")
                if bug_id:
                    test_rm_issues.add(bug_id)
                # Do not add to failing_errors
            else:
                # Other unknown decisions - fail the test for safety
                logger.warning(f"Unknown bug handler decision '{action}' for err logs: {err_logs}")
                failing_errors.append((bug_id, err_logs))
                if bug_id:
                    test_rm_issues.add(bug_id)
        # Build error message only if there are failing errors
        if failing_errors:
            error_msg += f"There are err msg detected under the {BugHandlerConst.NO_ACTION_MODE} mode:\n"
            for bug_id, err_logs in failing_errors:
                if bug_id:
                    error_msg += f"Relative bug is #{bug_id} detected for the err logs: {err_logs} \n"
                else:
                    error_msg += f"No relative bug detected for the err logs: {err_logs} \n"

    if log_analyzer_res[BugHandlerConst.UPDATE_ONLY]:
        error_msg += f"There are err msg detected under the {BugHandlerConst.UPDATE_ONLY} mode:\n"
        for err_with_update_only in log_analyzer_res[BugHandlerConst.UPDATE_ONLY]:
            err_logs = err_with_update_only[BugHandlerConst.LA_ERROR]
            error_msg += f"No relative bug detected for the err logs: {err_logs} \n"
    elif log_analyzer_res[BugHandlerConst.BUG_HANDLER_DECISION_CREATE]:

        created_bug_items = log_analyzer_res[BugHandlerConst.BUG_HANDLER_DECISION_CREATE]
        error_msg += f"There are {len(created_bug_items)} new Log Analyzer bugs Created: \n"
        for index, (bug_id, bug_title) in enumerate(created_bug_items.items(), start=1):
            error_msg += f"{index}) {REDMINE_ISSUES_URL+str(bug_id)}:  {bug_title}\n"
            test_rm_issues.add(bug_id)
    elif log_analyzer_res[BugHandlerConst.BUG_HANDLER_DECISION_UPDATE]:
        created_bug_items = log_analyzer_res[BugHandlerConst.BUG_HANDLER_DECISION_UPDATE]
        if is_test_function_failed:
            error_msg += f"There are {len(created_bug_items)} related bug found\n"
        for index, (bug_id, bug_title) in enumerate(created_bug_items.items(), start=1):
            test_rm_issues.add(bug_id)
            if is_test_function_failed:
                error_msg += f"{index}) {REDMINE_ISSUES_URL+str(bug_id)}:  {bug_title}\n"
    elif log_analyzer_res[BugHandlerConst.BUG_HANDLER_DECISION_SKIP] and is_test_function_failed:
        skipped_bug_items = log_analyzer_res[BugHandlerConst.BUG_HANDLER_DECISION_SKIP]
        error_msg += f"There are {len(skipped_bug_items)} related bug found but skipped\n"
        for index, (bug_id, bug_title) in enumerate(skipped_bug_items.items(), start=1):
            test_rm_issues.add(bug_id)
            error_msg += f"{index}) {REDMINE_ISSUES_URL+str(bug_id)}:  {bug_title}\n"
    if log_analyzer_res[BugHandlerConst.BUG_HANDLER_FAILURE]:
        la_error_messages = f"{BugHandlerConst.BUG_HANDLER_FAILURE_EXCEPTION}, due to the following:" \
                            f"{json.dumps(log_analyzer_res[BugHandlerConst.BUG_HANDLER_FAILURE], indent=2)}"
        error_msg = error_msg + la_error_messages

    if error_msg:
        la_rm_issues[test_id] = (list(test_rm_issues), la_error_messages)
        request.session.config.cache.set(BugHandlerConst.LA_RM_ISSUES_DICT, la_rm_issues)
        raise Exception(error_msg)


def is_log_analyzer_bug_handler_enabled(bug_handler_actions):
    """
    Check if need to run the log analyzer bug handler based on the bug handler actions.
    """
    return (bug_handler_actions['only_check'] or
            bug_handler_actions['create'] or
            bug_handler_actions['update'] or
            bug_handler_actions.get('ci_mode', False))


def get_pytest_cmd(request, cli_type):
    if cli_type == "Sonic":
        cmd = request.session.config.cache.get(PYTEST_RUN_CMD, None)
        if "--bug_handler_params" not in cmd:
            cmd += " --bug_handler_params only_check"
        if "test_check_errors_in_log_during_deploy_sonic_image" in request.node.name:
            cmd = "****************Please run the deployment script before the pytest command****************\n" + cmd
        return cmd
    else:
       return " ".join(request.node.config.invocation_params.args)


def get_log_analyzer_handler_info(duthost):

    log_analyzer_handler_info = {
        'branch': '',
        'cli_type': '',
        'version': ''
    }
    cli_type = os.environ.get("CLI_TYPE")
    if not cli_type:
        try:
            duthost.shell("show version")
            cli_type = "Sonic"
        except:  # noqa: E722
            cli_type = "NVUE"

    log_analyzer_handler_info['cli_type'] = cli_type
    log_analyzer_handler_info['branch'] = get_sonic_branch(duthost, cli_type)
    log_analyzer_handler_info['version'] = duthost.os_version
    if cli_type == "Sonic":
        log_analyzer_handler_info['components'] = get_low_layer_components(duthost)
    return log_analyzer_handler_info


def get_low_layer_components(duthost):
    components = duthost.show_and_parse("get_component_versions.py", module_ignore_errors=True)
    comps = ""
    for component in components:
        comp_name = component['component']
        comp_value = component['compilation'] if component['actual'] == "N/A" else component['actual']
        comps += f"{comp_name}: {comp_value} \n"

    return comps


def get_bug_handler_actions(request, log_analyzer_handler_info, only_check=False):
    """
    Get the bug handler actions, the return is a dictionary with 4 keys, "create", "update", "only_check", and "ci_mode".
    If only_check=True then bugs will not be created or updated.
    ci_mode=True indicates special handling for CI projects where update decisions should pass the test.
    """

    bug_handler_actions = {
        'create': False,
        'update': False,
        'only_check': True,
        'ci_mode': False
    }

    project_bug_create_map = {rt: rt in RegressionType.bug_create_types() for rt in RegressionType}
    project_bug_update_map = {rt: rt == RegressionType.REGRESSION for rt in RegressionType}
    project_bug_only_check_map = {rt: False for rt in RegressionType}

    if not only_check:
        project = os.environ.get(REGRESSION_TYPE_ENV_VAR)
        bug_handler_actions['create'] = project_bug_create_map.get(project, False)
        bug_handler_actions['update'] = project_bug_update_map.get(project, False)
        bug_handler_actions['only_check'] = project_bug_only_check_map.get(project, True)
        _update_bug_handler_actions_for_private_image(project, log_analyzer_handler_info, bug_handler_actions)
        _update_bug_handler_actions(request, bug_handler_actions)
        # Set ci_mode for CI projects when both create and update are False
        if project in RegressionType.relaxed_error_handling_types() and not bug_handler_actions['create'] and not bug_handler_actions['update']:
            bug_handler_actions['ci_mode'] = True
        logger.info(f"The bug handler actions for the {project} is: {bug_handler_actions}")

    return bug_handler_actions


def _update_bug_handler_actions_for_private_image(project, log_analyzer_handler_info, bug_handler_actions):
    cli_type = log_analyzer_handler_info['cli_type']
    if project != RegressionType.REGRESSION or cli_type != "Sonic":
        return
    branch = log_analyzer_handler_info["branch"]
    is_private_branch = False if re.match("^[0-9]{6,6}$", branch) else True
    if is_private_branch:
        bug_handler_actions['create'] = False
        bug_handler_actions['update'] = False
        bug_handler_actions['only_check'] = True


def _update_bug_handler_actions(request, bug_handler_actions):
    """
    Update the bug handler actions with the value specified in the param enable_bug_handler
    """
    bug_handler_params = request.config.getoption('--bug_handler_params')
    if bug_handler_params == "enable":
        bug_handler_actions['create'] = True
        bug_handler_actions['update'] = True
    elif bug_handler_params == "only_check":
        bug_handler_actions['create'] = False
        bug_handler_actions['update'] = False
        bug_handler_actions['only_check'] = True


def get_sonic_branch(duthost, cli_type):
    """
    Get the SONiC branch based on release field from /etc/sonic/sonic_version.yml
    :return: branch name
    """
    if cli_type in NvosCliTypes.NvueCliTypes:
        branch = "master"
    else:
        try:
            release_output = duthost.shell("sonic-cfggen -y /etc/sonic/sonic_version.yml -v release")['stdout_lines']
            branch = release_output[0]
        except SSHException as err:
            branch = 'Unknown'
            logger.error(f'Unable to get branch. Assuming that the device is not reachable. Setting the branch as Unknown. '
                         f'Got error: {err}')
    # master branch always has release "none"
    if branch == "none":
        branch_output = duthost.shell("sonic-cfggen -y /etc/sonic/sonic_version.yml -v branch")['stdout']
        if branch_output.lower() == "detached":
            build_version = duthost.shell("sonic-cfggen -y /etc/sonic/sonic_version.yml -v build_version")['stdout']
            branch_output = build_version.split(".")[0]
        if branch_output.lower() in PRIVATE_BRANCHES_TO_ENABLE_BUG_HANDLER_LIST:
            branch = "000000"
        else:
            branch = "master"
    return branch.strip()


def get_nvue_additional_info(duthost, request):
    """
    Fetches additional NVUE-related information from the DUT (Device Under Test).

    Args:
        duthost: DUT object.

    Returns:
        dict: Contains the history of executed commands and specific outputs for show_system and show_platform_firmware.
    """
    nvue_info = {}
    try:
        # List of commands to execute
        commands = [
            "nv show system reboot history",
            "nv show platform firmware",
        ]

        # Run commands on the remote duthost
        results = duthost.shell_cmds(cmds=commands, continue_on_fail=True, timeout=30)['results']
        # Parse results
        command_results = {}
        for result in results:
            cmd = result['cmd']
            command_results[cmd] = {
                'stdout': result.get('stdout', '').strip(),
                'stderr': result.get('stderr', '').strip(),
                'rc': result.get('rc', 0)
            }

        # Populate nvue_info
        nvue_info['show_system'] = command_results.get("nv show system reboot history", {}).get('stdout', '')
        nvue_info['show_platform_firmware'] = command_results.get("nv show platform firmware", {}).get('stdout', '')

        # Read executed commands from local file that was copied by list_of_executed_commands fixture
        try:
            from pathlib import Path

            # Use predictable local path
            local_commands_dir = Path("/tmp/executed_commands")

            # Try the fixed filename first
            local_file_path = local_commands_dir / "executed_commands.txt"

            # If fixed filename doesn't exist, try hostname-based filename
            if not local_file_path.exists() and hasattr(duthost, 'hostname'):
                hostname_based_path = local_commands_dir / f"executed_commands_{duthost.hostname}.txt"
                if hostname_based_path.exists():
                    local_file_path = hostname_based_path

            if local_file_path.exists():
                commands_content = local_file_path.read_text().strip()
                nvue_info['executed_commands'] = commands_content
            else:
                nvue_info['executed_commands'] = f"Error: Local commands file not found at {local_file_path}"

        except Exception as file_error:
            nvue_info['executed_commands'] = f"Error: Unable to read executed commands from local file - {str(file_error)}"

    except Exception as e:
        logging.error(f"Failed to retrieve NVUE information from {duthost}: {e}")
        nvue_info['show_system'] = "Error: Unable to fetch 'nv show system reboot history' output"
        nvue_info['show_platform_firmware'] = "Error: Unable to fetch 'nv show platform firmware' output"
        nvue_info['executed_commands'] = f"Error: Unable to read executed commands from local file"

    return nvue_info

def _set_dice_coefficient_threshold(config_file: str, dice_coefficient_threshold: float = 1.0) -> None:
    """Set the dice coefficient threshold in the config json file.

    Args:
        config_file: path to the config json file
        dice_coefficient_threshold: dice coefficient threshold
    """
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"Config file not found: {config_file}")
    if not os.access(config_file, os.R_OK | os.W_OK):
        raise PermissionError(f"Insufficient permissions for file: {config_file}")
    with open(config_file, 'r') as f:
        lines = f.readlines()
    with open(config_file, 'w') as f:
        for line in lines:
            if '"comparison_threshold":' in line:
                line = re.sub(
                    r'"comparison_threshold":.*',
                    f'"comparison_threshold": {dice_coefficient_threshold}',
                    line
                )
            f.write(line)

def bug_handler_wrapper(analyzers, duthosts, la_results):
    """
    The wrapper function for log_analyzer_bug_handler.
    Will be called in parallel_run to run log_analyzer_bug_handler in parallel
    for each DUT.
    """
    if not isinstance(la_results, dict):
        logging.error(f'Expect la_results is a dict, but got {type(la_results)}: {la_results}')
        return
    for node, la_result in la_results.items():
        if "failed" in la_result:
            logging.error(f'Failed to run log analyzer on {node}')
            return
    try:
        # clear files from previous run
        clear_files(os.environ.get(InfraConst.ENV_SESSION_ID, 'unknown_session_id'))
        # run bug handler in seperated step to decouple from analyze_logs
        # save original logging handlers and level
        original_handlers = logging.getLogger().handlers[:]
        original_level = logging.getLogger().level
        logging.getLogger().handlers = []
        bh_results = parallel_run(bug_handler_processing, [analyzers, la_results], {}, duthosts, timeout=1080)
        # restore original logging handlers and level
        logging.getLogger().handlers = original_handlers
        logging.getLogger().setLevel(original_level)
        for node in bh_results.keys():
            if 'failed' in bh_results[node]:
                logging.error(f'Failed to run bug handler on {node}')
    finally:
        # always attach allure log for bug handler results
        for duthost in duthosts:
            log_file = LOG_ANALYZER_LOG_FILE.format(duthost.hostname)
            if os.path.exists(log_file):
                raw_allure.attach.file(log_file, name=os.path.basename(log_file),
                                   attachment_type=raw_allure.attachment_type.TEXT)

@reset_ansible_local_tmp
def bug_handler_processing(analyzers, la_results: dict, node=None, results=None):
    """
    Will be called in parallel_run to run log_analyzer_bug_handler concurrently
    for each DUT.
    """
    file_handler = None
    log_file = LOG_ANALYZER_LOG_FILE.format(node.hostname)
    if os.path.exists(log_file):
        os.remove(log_file)
    file_handler = logging.FileHandler(log_file)
    formatter = logging.Formatter('[%(asctime)s][%(levelname)s] %(message)s')
    formatter.datefmt = '%Y-%m-%d %H:%M:%S'
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    # Use the root logger so that all logging calls in helper modules
    # (e.g., ngts/helpers/bug_handler/bug_handler_helper.py which uses logging.getLogger())
    # will also be captured in the file handler.
    # Remove all existing handlers to prevent logging to stdout/stderr
    # which can cause pipe deadlock when subprocess output buffer fills
    # and parent process does not read from the pipe.
    bh_logger = logging.getLogger()
    bh_logger.handlers = []
    bh_logger.setLevel(logging.INFO)
    bh_logger.addHandler(file_handler)
    try:
        analyzer = analyzers[node.hostname]
        analyzer_summary = la_results[node.hostname]
        duthost, request = analyzer.ansible_host, analyzer.request
        if skip_bug_handler(duthost, request, logger=bh_logger):
            bh_logger.info("Bug handler is skipped for %s, will verify log analyzer summary", node.hostname)
            analyzer._verify_log(analyzer_summary)
        else:
            log_analyzer_bug_handler(duthost, request, is_test_function_failed=_is_test_function_failed(request), logger=bh_logger)
    finally:
        bh_logger.info("Bug handler processing finished")
        if file_handler:
            bh_logger.removeHandler(file_handler)
            file_handler.close()

def _is_test_function_failed(request: pytest.FixtureRequest) -> bool:
    if "rep_setup" in request.node.__dict__ and request.node.rep_setup.failed:
            return True
    return "rep_call" in request.node.__dict__ and request.node.rep_call.failed
