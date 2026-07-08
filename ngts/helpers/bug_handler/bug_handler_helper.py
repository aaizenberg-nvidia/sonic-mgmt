from concurrent.futures import ThreadPoolExecutor, Future
from typing import IO, List
import dataclasses
import os
import re
import subprocess
import yaml
import json
import logging
import allure
import math
import pathlib

from ansible.utils.unsafe_proxy import AnsibleUnsafeText
from collections.abc import Mapping, Iterable
from retry.api import retry
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from datetime import datetime, timedelta
from ngts.constants.constants import BugHandlerConst, InfraConst, FILE_INCLUDE_FAILED_SANITY_CHECKER_CASE
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.infra import ExceptionTool
from devts.infra.tools.redmine.redmine_api import get_issue_fixed_in_version_value
from ngts.scripts.collect_simx_logs_on_not_success import collect_hypervisor_logs, dump_simx_data
from devts.infra.tools.topology_tools.topology_setup_utils import get_topology_by_setup_name
from ngts.helpers.redmine_cache_helper import access_redmine_cache

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class BugHandlerResult:
    message: str
    stderr: str
    returncode: int

    def __str__(self):
        return self.message.rstrip()

    def __bool__(self):
        return self.returncode == 0


def _run_bug_handler_command(command: str) -> BugHandlerResult:
    """
    Runs a shell command, streams stdout/stderr live to this process,
    and returns (rc, stdout_text, stderr_text).
    """
    _inner_logger = logger.getChild("proc")
    p = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,  # decode to str
        bufsize=1,  # line-buffered
        errors="replace",
    )

    out_buf, err_buf = [], []
    futures: List[Future[None]] = []

    def _pump(src: IO[str], level, sink: List[str]):
        for line in iter(src.readline, ''):
            _inner_logger.log(level, line.rstrip())
            sink.append(line)
        src.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures.append(executor.submit(_pump, p.stdout, logging.INFO, out_buf))
        futures.append(executor.submit(_pump, p.stderr, logging.ERROR, err_buf))
        rc = p.wait()

    for future in futures:
        try:
            future.result()
        except Exception as e:
            logger.error(f"Error while pumping stream: {e}")
    return BugHandlerResult(returncode=rc, message=''.join(out_buf), stderr=''.join(err_buf))


def handle_sanitizer_dumps(dump_paths, cli_type, branch, version, setup_name, topology_obj):
    """
    Call bug handler on all sanitizer files in each dump in dump_paths,
    and return list with results
    :param dump_paths: a list of sanitizer dumps paths which were created during the session
    :param cli_type: i.e, Sonic
    :param branch: i.e 202211
    :param version: i.e, SONiC-OS-202211_RC15.1-7ceec30cc_Internal
    :param setup_name: i.e, sonic_lionfish_r-lionfish-14
    :return: A list of dictionaries with results for each dump
    i.e., [{'dump_name': 'dump_test_lags_scale_sanitizer_files_06_21_2022_23_27_04.tar.gz',
    'test_name': 'test_lags_scale',
    'results':
    [{'file_name': '2022-06-21_23-24-07_orchagent-asan.log.40',
    'messages': ['INFO:handle_bug:reading configuration from', ...],
    'rc': 0,
    'decision': 'update'},...]
    },...]
    """
    bug_handler_dumps_results = []
    session_id = os.environ.get(InfraConst.ENV_SESSION_ID)
    create_session_tmp_folder(session_id)
    redmine_project = BugHandlerConst.CLI_TYPE_REDMINE_PROJECT[cli_type]
    conf_path = BugHandlerConst.BUG_HANDLER_CONF_FILE[redmine_project]
    for sanitizer_dump_path in dump_paths:
        with allure.step(f"Run Bug Handler on Sanitizer Dump: {sanitizer_dump_path}"):
            bug_handler_dumps_results.append(handle_sanitizer_dump(conf_path, sanitizer_dump_path, redmine_project,
                                                                   branch, version, setup_name))
    clear_files(session_id)
    return bug_handler_dumps_results


def create_session_tmp_folder(session_id):
    os.system(f"sudo mkdir /tmp/{session_id}")
    os.system(f"sudo chmod 777 /tmp/{session_id}")
    return f"/tmp/{session_id}"


def clear_files(session_id):
    if session_id:  # make sure that we don't delete the root tmp folder case the session id is not set
        os.system(f"sudo rm -rf /tmp/{session_id}")
    os.system("rm -rf /tmp/parsed_sanitizer_dumps/")


def handle_sanitizer_dump(conf_path, dump_path, redmine_project, branch, version, setup_name):
    """
    Call bug handler with ASAN dump files and send email with results
    :param conf_path: i.e, /tmp/sonic_bug_handler.conf
    :param dump_path: path to sanitizer dump
    :param redmine_project: i.e, SONiC-Design
    :param branch: i.e 202205
    :param version: i.e 202205_sai_integration.2-36792dcfc_Internal
    :param setup_name: i.e, sonic_lionfish_r-lionfish-14
    :return: dictionary with bug handler results for dump, i.e,
    {'dump_name': 'dump_test_lags_scale_sanitizer_files_06_21_2022_23_27_04.tar.gz',
    'test_name': 'test_lags_scale',
    'results':
    [{'file_name': '2022-06-21_23-24-07_orchagent-asan.log.40',
    'messages': ['INFO:handle_bug:reading configuration from', ...],
    'rc': 0,
    'decision': 'update'},...]
    }
    """
    bug_handler_dump_result = dict()
    bug_handler_dump_result["dump_name"] = Path(dump_path).name
    bug_handler_dump_result["test_name"] = get_test_name_from_sanitizer_dump(bug_handler_dump_result["dump_name"])
    bug_handler_dump_result["results"] = list()
    yaml_parsed_files_dict = parse_sanitizer_dump(dump_path, redmine_project, version, setup_name)
    for sanitizer_file_name, yaml_parsed_file in yaml_parsed_files_dict.items():
        with allure.step(f"Run Bug Handler on sanitizer file: {sanitizer_file_name}"):
            bug_handler_dump_result["results"].append(
                bug_handler_wrapper(
                    conf_path,
                    redmine_project,
                    branch,
                    sanitizer_file_name,
                    yaml_parsed_file,
                    BugHandlerConst.BUG_HANDLER_SANITIZER_USER,
                    BugHandlerConst.BUG_HANDLER_SCRIPT.get(redmine_project, BugHandlerConst.BUG_HANDLER_SCRIPT["default"])
                )
            )
    return bug_handler_dump_result


def get_test_name_from_sanitizer_dump(dump_name):
    regex = r"dump_(.*)_sanitizer_files_.*\.tar\.gz"
    return re.search(regex, dump_name).group(1)


def parse_sanitizer_dump(dump_path, project, version, setup_name):
    """
    :param dump_path: path to sanitizer dump
    :param project: i.e SONiC-Design
    :param version: i.e 202205_sai_integration.2-36792dcfc_Internal
    :param setup_name: i.e, sonic_lionfish_r-lionfish-14
    :return: a dictionary with parsed dump files paths for bug handler
    i.e, {'2022-06-21_23-24-07_orchagent-asan.log.40':
    '/tmp/parsed_sanitizer_dumps/dump_..._extracted/yaml_parsed_files/2022-06-21_23-24-07_orchagent-asan.log.40.yaml',
    ...}
    """
    yaml_parsed_files_dict = {}
    dump_base_dir, dump_file_name = os.path.split(dump_path)
    extracted_dump_dir = os.path.join(BugHandlerConst.SANITIZER_PARSED_DUMPS_FOLDER, f"{dump_file_name}_extracted")
    logger.info("Create folder: {} if it doesn't exist".format(extracted_dump_dir))
    Path(extracted_dump_dir).mkdir(parents=True, exist_ok=True)
    logger.info("Created folder - {}".format(extracted_dump_dir))
    with allure.step("Parse sanitizer dump contents"):
        os.system(f"tar -xzvf {dump_path} -C {extracted_dump_dir}")
        for filename in os.listdir(extracted_dump_dir):
            file_path = os.path.join(extracted_dump_dir, filename)
            if os.path.isfile(file_path):
                with allure.step(f"Parse sanitizer dump file: {filename}"):
                    yaml_file_path = parse_sanitizer_file(file_path, dump_path, project, version, setup_name)
                    yaml_parsed_files_dict.update({filename: yaml_file_path})
    return yaml_parsed_files_dict


def parse_sanitizer_file(file_path, dump_path, project, version, setup_name):
    """
    The function will create a YAML file in the needed format for bug handler script
    :param file_path: path to sanitizer file
    :param dump_path: path to sanitizer dump
    :param project: i.e SONiC-Design
    :param version: i.e 202205_sai_integration.2-36792dcfc_Internal
    :param setup_name: i.e, sonic_lionfish_r-lionfish-14
    :return: path to parsed YAML file
    """
    file_base_dir, file_name = os.path.split(file_path)
    yaml_file_dir = os.path.join(file_base_dir, "yaml_parsed_files")
    yaml_file_path = os.path.join(file_base_dir, "yaml_parsed_files", f"{file_name}.yaml")
    Path(yaml_file_dir).mkdir(parents=True, exist_ok=True)
    contents = Path(file_path).read_text()
    contents_without_prefix = remove_error_prefix_from_sanitizer_file(contents)
    yaml_content_as_dict = {'description': contents_without_prefix,
                            'project': project,
                            'uploads': [file_path, dump_path],
                            'detected_in_version': version,
                            'session_id': os.environ.get(InfraConst.ENV_SESSION_ID),
                            'setup_name': setup_name,
                            'test_name': get_test_name_from_sanitizer_dump(dump_path),
                            'system_type': os.environ.get('CLI_TYPE')}
    yaml_content = yaml.dump(yaml_content_as_dict)
    with open(yaml_file_path, "a") as file:
        file.write(yaml_content)
    return yaml_file_path


def remove_error_prefix_from_sanitizer_file(contents):
    error_prefix_regex = r"(.*\n*=+\n*=+\d+=*ERROR:\s+)"
    error_prefix = re.search(error_prefix_regex, contents, re.IGNORECASE).group(1)
    contents_without_prefix = contents.replace(error_prefix, "")
    return contents_without_prefix


def bug_handler_wrapper(conf_path, redmine_project, branch, upload_file_path, yaml_parsed_file, user, bug_handler_path):
    """
    call bug handler on sanitizer or log analyzer file and return results as dictionary
    :param conf_path: i.e, /tmp/sonic_bug_handler.conf
    :param redmine_project: i.e SONiC-Design
    :param branch: i.e 202205
    :param upload_file_path: i.e, 2023-04-02_16-35-19_wjhd-asan.log.22
    :param yaml_parsed_file: i.e, 2023-04-02_16-35-19_wjhd-asan.log.22.yaml
    :param user: i.e log_analyzer
    :param bug_handler_path: i.e /auto/sw_tools/Internal/BugHandling/bin/handle_bug.py
    :return: dictionary with bug handler results,
    i.e,
    {'file_name': '2022-06-21_23-24-07_orchagent-asan.log.40',
    'messages': ['INFO:handle_bug:reading configuration from', ...],
    'rc': 0,
    'action': 'update',
    'bug_id': '1122554'}
    """
    bug_handler_file_result = run_bug_handler_tool(conf_path, redmine_project, branch, yaml_parsed_file, user,
                                                   bug_handler_path)

    bug_handler_file_result["file_name"] = upload_file_path
    logger.info(f"Bug Handler RC: {bug_handler_file_result[BugHandlerConst.BUG_HANDLER_RC]}")
    logger.info(f"Bug Handler Status: {bug_handler_file_result[BugHandlerConst.BUG_HANDLER_STATUS]}")
    logger.info(f"Bug Handler Action: {bug_handler_file_result[BugHandlerConst.BUG_HANDLER_ACTION]}")
    return bug_handler_file_result


def run_bug_handler_tool(conf_path, redmine_project, branch, yaml_parsed_file, user, bug_handler_path):

    bug_handler_cmd = f"env LOG_FORMAT_JSON=1 {bug_handler_path} --cfg {conf_path} --project {redmine_project} " \
        f"--user {user} --branch {branch} --debug_level 2 --parsed_data '{yaml_parsed_file}'"

    bug_handler_output = _run_bug_handler_command(bug_handler_cmd)
    logger.info(f"Bug Handler Output: {bug_handler_output}")
    bug_handler_file_result = json.loads(bug_handler_output.message)

    return bug_handler_file_result


def bug_handler_wrapper_err_msg(conf_path, redmine_project, branch, yaml_parsed_file, user,
                                bug_handler_path, bug_handler_action={}, bug_handler_params={}):
    """
    call bug handler on sanitizer or log analyzer file and return results as dictionary
    :param conf_path: i.e, /tmp/sonic_bug_handler.conf
    :param redmine_project: i.e SONiC-Design
    :param branch: i.e 202205
    :param yaml_parsed_file: i.e, 2023-04-02_16-35-19_wjhd-asan.log.22.yaml
    :param user: i.e log_analyzer
    :param bug_handler_path: i.e /auto/sw_tools/Internal/BugHandling/bin/handle_bug.py
    :param bug_handler_action: a dictionary that defined the actions for the bughandler
    :param bug_handler_params: a dictionary to define the params of the bughandler
    :return: dictionary with bug handler results,
    i.e,
    {'file_name': '2022-06-21_23-24-07_orchagent-asan.log.40',
    'messages': ['INFO:handle_bug:reading configuration from', ...],
    'rc': 0,
    'action': 'update',
    'bug_id': '1122554'}
    """

    bug_handler_file_result = run_err_msg_bug_handler_tool(conf_path, redmine_project, branch, yaml_parsed_file, user,
                                                           bug_handler_path, bug_handler_action, bug_handler_params)

    logger.info(f"Bug Handler RC: {bug_handler_file_result[BugHandlerConst.BUG_HANDLER_RC]}")
    logger.info(f"Bug Handler Status: {bug_handler_file_result[BugHandlerConst.BUG_HANDLER_STATUS]}")
    logger.info(f"Bug Handler Action: {bug_handler_file_result[BugHandlerConst.BUG_HANDLER_ACTION]}")
    return bug_handler_file_result


def run_err_msg_bug_handler_tool(conf_path, redmine_project, branch, yaml_parsed_file, user, bug_handler_path,
                                 bug_handler_action={}, bug_handler_params={}):

    bug_handler_create_action = bug_handler_action.get("create", False)
    bug_handler_update_action = bug_handler_action.get("update", False)
    bug_handler_no_action = not bug_handler_create_action and not bug_handler_update_action

    no_action = '--no_action' if bug_handler_no_action else ''

    update_only = not bug_handler_action["create"] and bug_handler_action["update"]
    update_only_mode = '--update_only' if update_only else ''
    cli_type = bug_handler_params.get("cli_type", '')
    if cli_type == "Sonic":

        bug_handler_file_result = run_bug_handler_with_no_action(
            conf_path, redmine_project, branch, yaml_parsed_file, user, bug_handler_path)
        if not bug_handler_no_action:
            action_mode = get_action_based_on_no_action_results(
                bug_handler_file_result, branch, bug_handler_params, bug_handler_update_action)

            if action_mode == "no action":
                logger.info("To not fail case and attach file, need update the following 3 parameters")
                bug_handler_file_result["action"] = BugHandlerConst.BUG_HANDLER_DECISION_UPDATE
                bug_handler_file_result["status"] = "done"
                bug_handler_no_action = True
            elif Path(FILE_INCLUDE_FAILED_SANITY_CHECKER_CASE).exists():
                logger.info("Sanity checker failure, set bug handler to no action")
                bug_handler_no_action = True
                # pass case if there's existing ticket
                if bug_handler_file_result["action"] == BugHandlerConst.BUG_HANDLER_DECISION_UPDATE:
                    bug_handler_file_result["status"] = "done"

    if not bug_handler_no_action or cli_type != "Sonic":
        bug_handler_cmd = f"env LOG_FORMAT_JSON=1 {bug_handler_path} --cfg {conf_path} --project {redmine_project} " \
            f"--user {user} --branch {branch} --debug_level 2 " \
            f"--parsed_data '{yaml_parsed_file}' {no_action} {update_only_mode}"

        bug_handler_output = _run_bug_handler_command(bug_handler_cmd)
        logger.info(f"Bug Handler Output: {bug_handler_output}")
        try:
            bug_handler_file_result = json.loads(bug_handler_output.message)
        except json.JSONDecodeError as json_err:
            # Tool returned non-JSON (usually it crashed with a non-zero RC, e.g. Mongo backend
            # down). Raise a clear error instead of falling through and leaving
            # bug_handler_file_result unbound, which masked the real failure with an UnboundLocalError.
            err_msg = (f"Bug handler tool failed - could not parse its output as JSON.\n"
                       f"Command: {bug_handler_cmd}\n"
                       f"RC: {bug_handler_output.returncode}\n"
                       f"Stdout: {bug_handler_output.message}\n"
                       f"Stderr: {bug_handler_output.stderr}")
            logger.error(err_msg)
            raise RuntimeError(err_msg) from json_err

    if is_attachment_needed(bug_handler_file_result, update_only, bug_handler_no_action, yaml_parsed_file):
        ticket_id = get_ticket_id(bug_handler_file_result)
        logger.info(f"the RM ticket id is: {ticket_id}")
        tar_file_path_list = get_tech_support_from_switch(bug_handler_params)
        # Note: Executed commands are now included directly in the ticket description via get_nvue_additional_info
        # No longer attaching the commands file separately
        tar_file_path_list = [handle_file_size_exceedance(tar_file_path) for tar_file_path in tar_file_path_list]
        upload_script = BugHandlerConst.BUG_HANDLER_UPLOAD_ATTACHMENT_SCRIPT.get(redmine_project, BugHandlerConst.BUG_HANDLER_UPLOAD_ATTACHMENT_SCRIPT["default"])
        upload_cmd = f"env LOG_FORMAT_JSON=1 {upload_script} --bug_id {ticket_id}  --attachments {' '.join(tar_file_path_list)}"
        logger.info(f"Running uploading attachment command: {upload_cmd}")
        upload_attachment_output = subprocess.run(upload_cmd, shell=True, capture_output=True)
        upload_attachment_output, upload_error = upload_attachment_output.stdout, upload_attachment_output.stderr
        logger.info(upload_attachment_output)

        if not upload_error:
            try:
                upload_attachment_result = json.loads(upload_attachment_output)
                if "error" in upload_attachment_result:
                    upload_error = str(upload_attachment_result)
            except Exception as e:
                upload_error = ExceptionTool.format_exception(e)
        if upload_error:
            logger.error(f"Failed to upload the file: {upload_error}")

        bug_handler_file_result["file_name"] = ",".join(tar_file_path_list)
    return bug_handler_file_result


def handle_file_size_exceedance(tar_file_path):
    # add double quotes to the tar_file_path to avoid whitespace characters
    # in the file name to be interpreted as command line arguments
    result = subprocess.run(f'stat -c%s "{tar_file_path}"', shell=True, capture_output=True)
    if result.returncode != 0:
        logger.error(f"Failed to get the sysdump size of {tar_file_path} "
                     f"with error: {result.stderr.decode('utf-8')}")
        raise Exception(f"Failed to get the sysdump size of {tar_file_path}")
    decoded_output = result.stdout.decode('utf-8')
    cleaned_output = decoded_output.strip()
    tar_file_size = int(cleaned_output)

    if tar_file_size < BugHandlerConst.TAR_FILE_SIZE_RM_LIMIT:
        return tar_file_path

    logger.info(f"the sysdump file size is {tar_file_size}, more than expected {BugHandlerConst.TAR_FILE_SIZE_RM_LIMIT}, create new txt file includes uploading error message and the system dump full path")
    additional_text = (
        f"Failed to upload the tar file because the size is more than {BugHandlerConst.TAR_FILE_SIZE_RM_LIMIT}.\n"
        "The sysdump can be obtained by:\n"
    )
    parts = tar_file_path.split('/')
    compressed_tar_full_path = '/'.join(parts[:-2]) + '/' + f"{parts[-3]}.tgz"
    txt_file_path = './' + parts[-1].replace('.tar.gz', '.txt')
    with open(txt_file_path, 'w') as file:
        file.write(f"{additional_text} tar -xzvf {compressed_tar_full_path} '{parts[-2]}/{parts[-1]}'")

    return txt_file_path


def run_bug_handler_with_no_action(conf_path, redmine_project, branch, yaml_parsed_file, user, bug_handler_path):

    bug_handler_cmd = f"env LOG_FORMAT_JSON=1 {bug_handler_path} --cfg {conf_path} --project {redmine_project} " \
        f"--user {user} --branch {branch} --debug_level 2 " \
        f"--parsed_data '{yaml_parsed_file}' --no_action "

    if not (bug_handler_result := _run_bug_handler_command(bug_handler_cmd)):  # rc != 0
        logger.error(f"Bug Handler Failed: {bug_handler_result.stderr}")
        raise Exception(f"Bug Handler Failed: {bug_handler_result.stderr}")
    logger.info(f"Bug Handler Output: {bug_handler_result}")
    bug_handler_file_result = json.loads(bug_handler_result.message)

    logger.info(f"No action bug_handler_file_result:{bug_handler_file_result}")
    return bug_handler_file_result


def get_action_based_on_no_action_results(bug_handler_file_result, branch, bug_handler_params, bug_handler_update_action):

    action_mode = "create or update_only"

    # When the bug need to be updated, and the ticket is closed status,
    # Running bug handler tool again to reopen ticket will depend on that if the current includes the fix or not,
    # Otherwise, we will run bug handler tool as the original action mode
    if bug_handler_file_result["action"] == BugHandlerConst.BUG_HANDLER_DECISION_UPDATE:
        cur_version = bug_handler_params["duthost"].os_version
        ticket_id = get_ticket_id(bug_handler_file_result)
        ticket_status = access_redmine_cache([ticket_id], use_active_status=False)

        if ticket_status == "Closed":
            # When the script for getting fixed image is ready for RC image,
            # the two following line code should be removed
            if "_RC" in cur_version:
                logger.info("Non internal image without RC. "
                            f"Next, run bug handler with {action_mode} to reopen the ticket")
            else:
                fixed_in_version = get_issue_fixed_in_version_value(ticket_id)
                if branch in fixed_in_version:
                    if is_current_ver_newer_or_equal_than_fixed_ver(branch, cur_version, fixed_in_version):
                        logger.info("The current version includes the bug fix."
                                    f"Next, run bug handler with {action_mode} to reopen the ticket")
                    else:
                        logger.info("The current version doesn't include the bug fix. "
                                    "Not run bug handler again. So, the ticket will not be reopened")
                        action_mode = "no action"
                else:
                    logger.info("Bug is closed, but the current image not include bug fix. Not run bug handler again")
                    action_mode = "no action"
        elif not bug_handler_update_action:
            if "Closed" not in ticket_status:
                # When the update is False, if ticket is closed, then need to reopen it, if ticket is not closed,
                # then no need to update the ticket
                logger.info("The current config is to not update the ticket")
                action_mode = "no action"
    logger.info(f"get_action_mode :{action_mode} \n. bug_handler_file_result: {bug_handler_file_result}")
    return action_mode


def get_version_nubmer(branch, version):
    pattern_version_number_value = f".*{branch}(_RC|).(?P<ver_number>\\d+)-\\w+_(Internal|Public).*"
    res = re.match(pattern_version_number_value, version)
    ver_nubmer = 0
    if res:
        ver_nubmer = res.groupdict()["ver_number"]
    logger.info(f"version of {version} number is :{ver_nubmer}")
    return int(ver_nubmer)


def is_current_ver_newer_or_equal_than_fixed_ver(branch, cur_version, fixed_in_version):
    version_split_symbol = ", "

    for version in fixed_in_version.split(version_split_symbol):
        fixed_ver_nubmer = get_version_nubmer(branch, version)
        if fixed_ver_nubmer:
            break
    cur_version_number = get_version_nubmer(branch, cur_version)

    logger.info(f"fixed version number:{fixed_ver_nubmer}, current version is :{cur_version_number}")

    return cur_version_number >= fixed_ver_nubmer


def is_attachment_needed(bug_handler_file_result, update_only, bug_handler_no_action, yaml_parsed_file):
    if (update_only and bug_handler_file_result["action"] == "create") or bug_handler_no_action or \
            bug_handler_file_result["action"] == "skip":
        return False
    else:
        with open(yaml_parsed_file, 'r') as stream:
            data = yaml.safe_load(stream)
            return not data['attachments']


def get_ticket_id(bug_handler_file_result):
    if bug_handler_file_result['action'] == "update":
        msgs = bug_handler_file_result['messages']
        duplicated_reg = r"\[INFO\] found root bug: ([0-9]+) for duplicate bug:"
        for msg in msgs:
            match_res = re.search(duplicated_reg, msg)
            if match_res:
                ticket_id = match_res.group(1)
                bug_handler_file_result['bug_id'] = ticket_id

    return bug_handler_file_result['bug_id']


def get_tech_support_from_switch(bug_handler_params):
    """
    generate tech support from the switch and copy it to player
    :param bug_handler_params: bug_handler_params
    :return: file path
    """
    duthost = bug_handler_params['duthost']
    testbed = bug_handler_params['testbed']
    session_id = bug_handler_params['session_id']
    test_name = bug_handler_params['test_name']
    cli_type = bug_handler_params['cli_type']

    dumps_folder = os.environ.get(InfraConst.ENV_LOG_FOLDER)
    if not dumps_folder:  # default value is empty string, defined in steps file
        dumps_folder = create_result_dir(testbed, session_id, InfraConst.CASES_DUMPS_DIR)
    dumps_files = []
    if cli_type == "Sonic":
        platform = duthost.shell("show platform summary | grep Platform | awk '{print $2}'")['stdout']
        if "_simx" in platform:
            try:
                topology_obj = get_topology_by_setup_name(setup_name=testbed, slow_cli=True)
                dumps_files.extend(dump_simx_data(topology_obj, dumps_folder))
                dumps_files.append(collect_hypervisor_logs(topology_obj, dumps_folder))
            except Exception as e:
                logger.error(f"Exception while collecting the simx dump {str(e)}")
        tar_file_path_on_switch = _generate_sonic_techsupport(duthost)
    elif cli_type == "NVUE":
        tar_file_path_on_switch = _generate_nvue_techsupport(duthost)

    else:
        raise Exception(f"No such cli_type: {cli_type}")

    tar_file_name = add_test_name_to_tar_file_path_on_switch(tar_file_path_on_switch, test_name)
    tar_file_path = os.path.join(dumps_folder, tar_file_name)
    duthost.fetch(src=tar_file_path_on_switch, dest=tar_file_path, flat=True)
    _delete_techsupport(duthost, tar_file_path_on_switch)
    dumps_files.append(os.path.join(dumps_folder, tar_file_name))
    return dumps_files


def add_test_name_to_tar_file_path_on_switch(tar_file_path_on_switch, test_name):
    test_name = test_name.replace('/', '_')
    base_name, _ = tar_file_path_on_switch.split('/')[-1].rsplit(".tar.gz", 1)
    tar_file_with_test_name = f"{base_name}_{test_name}.tar.gz"
    return tar_file_with_test_name


def create_result_dir(testbed, session_id, suffix_path_name):
    """
    Create directory for test artifacts in shared location
    :param testbed: name of the testbed
    :param session_id: MARS session id
    :param suffix_path_name: End part of the directory name
    :return: created directory path
    """
    folder_path = '/'.join([InfraConst.REGRESSION_SHARED_RESULTS_DIR, testbed, session_id, suffix_path_name])
    logging.info("Create folder: {} if it doesn't exist".format(folder_path))
    pathlib.Path(folder_path).mkdir(parents=True, exist_ok=True)
    logging.info("Created folder - {}".format(folder_path))
    return folder_path


def get_test_duration(duthost):
    """
    Get duration of test case. Init time + test body time + 120 seconds
    :param item: pytest build-in
    :return: integer, test duration
    """
    item = duthost.loganalyzer.request.node
    duration = math.ceil(item.rep_setup.duration) + 120
    if hasattr(item, "rep_call"):
        duration = duration + math.ceil(item.rep_call.duration)
    if hasattr(item, "rep_teardown"):
        duration = duration + math.ceil(item.rep_teardown.duration)
    if "test_check_errors_in_log_during_deploy_sonic_image" in item.name:
        duration = 7200   # need to collect all the dumps for this test, assume 2 hours is enough after the deployment.
    return duration


@retry(Exception, tries=5, delay=20)
def _generate_sonic_techsupport(duthost):
    # required by design team to always collect the dump for the past 5 hrs.
    duration = 5 * 3600
    return duthost.shell(f'sudo generate_dump -s \"-{duration} seconds\"')["stdout_lines"][-1]


def _generate_nvue_techsupport(duthost):
    cmd = "nv action generate system tech-support"
    logger.info(f"Running {cmd}")
    dump_file = duthost.shell(cmd)["stdout_lines"][-2].split(' ')[-1]
    logger.info(f"action output is {dump_file}")
    return SystemConsts.TECHSUPPORT_FILES_PATH + dump_file


def _delete_techsupport(duthost, tar_file_path):
    logger.info(f"tech-support uploaded, remove from switch")
    cmd = f"rm -f {tar_file_path}"
    logger.info(f"Running {cmd}")
    duthost.shell(cmd)


def get_recommended_action_for_user(bug_handler_rc, bug_handler_decision, bug_handler_messages):
    recommended_action = "Unknown scenario, please debug bug handler output."
    if bug_handler_decision == BugHandlerConst.BUG_HANDLER_DECISION_UPDATE:
        recommended_action = "Bug handler updated an existing bug, no additional action needed"
    elif bug_handler_decision == BugHandlerConst.BUG_HANDLER_DECISION_CREATE:
        bug_id = get_created_bug_id(bug_handler_messages)
        recommended_action = f"Bug handler had created a new bug for this issue,<br>" \
            f" Please review ticket and update missing info.<br>" \
            f"Bug id: {bug_id}."
    elif bug_handler_decision == BugHandlerConst.BUG_HANDLER_DECISION_ABORT:
        recommended_action = f"Bug handler could not compare signature in sanitizer log.<br>" \
            f"1. If sanitizer log is missing traceback, update sanitizer tool owner.<br>" \
            f"2. If sanitizer log does not missing traceback, <br>" \
            f"update bug handler owner team that bug handler " \
            f"could not parse sanitizer output correctly.<br>" \
            f"3. Open bug manually for this leak, if an open issue does not exist."
    elif bug_handler_decision == BugHandlerConst.BUG_HANDLER_DECISION_REOPEN:
        recommended_action = "Bug handler had changed the status of an existing bug from fixed/closed to assigned." \
                             "<br>Review the bug and alert the bug owner that fix is not working or merged."
    elif bug_handler_rc is not InfraConst.RC_SUCCESS:
        recommended_action = f"Bug handler had failed, please review bug handler output.<br>" \
            f"1. If needed, consult with bug handler owner team about reason for failure.<br>" \
            f"2. Rerun bug handler after fix or review sanitizer leak manually."
    return recommended_action


def get_created_bug_id(bug_handler_messages):
    bug_id = "could not find bug id in bug handler output, please review regex pattern"
    result = re.search(r"\[INFO\] created bug with id=(\d+)", bug_handler_messages)
    if result:
        bug_id = result.group(1)
    return bug_id


def create_summary_html_report(session_id, setup_name, dumps_folder, dumps_info):
    bug_handler_summary_template = get_xml_template('bug_handler_summary_template.j2')
    bug_handler_summary_output = bug_handler_summary_template.render(session_id=session_id,
                                                                     setup_name=setup_name,
                                                                     dumps_folder=dumps_folder,
                                                                     dumps_info=dumps_info)
    bug_handler_summary_path = os.path.join(dumps_folder, f"bug_handler_summary_report_session_{session_id}.html")
    f = open(bug_handler_summary_path, "w+")
    f.write(bug_handler_summary_output)
    f.close()
    return bug_handler_summary_path


def get_xml_template(template_name):
    p = Path(__file__).parent
    file_loader = FileSystemLoader(str(p))
    env = Environment(loader=file_loader)
    env.trim_blocks = True
    env.lstrip_blocks = True
    env.rstrip_blocks = True
    template = env.get_template(template_name)
    return template


def review_bug_handler_results(bug_handler_results):
    for dump_info in bug_handler_results:
        for bug_handler_result in dump_info["results"]:
            if bug_handler_result["action"] not in [BugHandlerConst.BUG_HANDLER_DECISION_UPDATE,
                                                    BugHandlerConst.BUG_HANDLER_DECISION_SKIP]\
                    or bug_handler_result["rc"] != InfraConst.RC_SUCCESS:
                raise AssertionError("Bug handler found undetected issues, please review summary attached to allure")


def get_log_analyzer_yaml_path(test_name, dump_path):
    yaml_file_dir = os.path.join(dump_path, "yaml_parsed_files")
    Path(yaml_file_dir).mkdir(parents=True, exist_ok=True)
    date_time = datetime.now().strftime("%m_%d_%Y_%H-%M-%S-%f")
    file_name = f"{test_name}_log_analyzer_files_{date_time}".replace("::", "_")
    yaml_file_path = os.path.join(yaml_file_dir, f"{file_name}.yaml")
    return yaml_file_path


def sanitize_for_yaml(data):
    """Convert Ansible-specific types to plain Python types for YAML serialization."""
    # logger.info(f"sanitize_for_yaml data: {data}, type: {type(data)}, id: {id(data)}")
    if isinstance(data, AnsibleUnsafeText):
        # logger.info("converting AnsibleUnsafeText to plain string")
        # Convert any AnsibleUnsafeText type to plain string
        return "".join(c for c in data)
    if isinstance(data, Mapping):
        # logger.info("converting Mapping to dictionary")
        return {sanitize_for_yaml(k): sanitize_for_yaml(v) for k, v in data.items()}
    if isinstance(data, Iterable) and not isinstance(data, str) and not isinstance(data, bytes):
        # logger.info("converting Iterable to list")
        return [sanitize_for_yaml(item) for item in data]
    # logger.info("returning data as is")
    return data


def create_log_analyzer_yaml_file(log_errors, dump_path, project, test_name, hostname,
                                  bug_info_dictionary, bug_handler_params, bug_handler_dumps_results,
                                  is_serial_log=False):
    """
    The function will create a YAML file in the needed format for bug handler script
    :param log_errors: list with log errors
    :param dump_path: path to dumps
    :param project: i.e, NVOS - Design
    :param test_name: name of the test
    :param bug_info_dictionary: a dictionary to save the content that the bug handler script required
    :param bug_handler_params: a dictionary to define the params of the bughandler
    :param hostname: i.e, gorilla-153
    :return: path to parsed YAML file
    """
    tar_file_path = None
    for bug_handler_dumps_result in bug_handler_dumps_results:
        if "file_name" in bug_handler_dumps_result:
            tar_file_path = bug_handler_dumps_result["file_name"]
            break

    yaml_file_path = get_log_analyzer_yaml_path(test_name, dump_path)
    # remove date, time and hostname before creating the regex!

    if re.findall(hostname, log_errors[0]):
        hostname_regex = hostname
        if re.findall(f"{hostname}-{SystemConsts.MGMT2_HOSTNAME}", log_errors[0]):
            hostname_regex = f"{hostname}-{SystemConsts.MGMT2_HOSTNAME}"
    elif re.findall(r"\d sonic ", log_errors[0]):
        hostname_regex = "sonic"
    else:
        hostname_regex = r'\S+'
    bug_title = create_bug_title(hostname_regex, hostname, log_errors[0], is_serial_log)
    normalized_title = error_to_regex(bug_title)
    bug_regex = '.*' + normalized_title + '.*'
    description = f'| \n{bug_title}\n' + '\n'.join(log_errors)
    bug_info_dictionary.update({'search_regex': bug_regex,
                                'normalized_title': normalized_title,
                                'bug_title': bug_title,
                                'description': f"{description}",
                                'project': project,
                                'attachments': [tar_file_path] if tar_file_path else None,
                                'session_id': os.environ.get(InfraConst.ENV_SESSION_ID),
                                'test_name': test_name})
    bug_info_dictionary = sanitize_for_yaml(bug_info_dictionary)
    yaml_content = yaml.dump(bug_info_dictionary)
    yaml_content = yaml_content.replace(bug_regex, f"\"{bug_regex}\"")
    logger.info("yaml file content: {}".format(yaml_content))
    with open(yaml_file_path, "w+") as file:
        file.write(yaml_content)

    return yaml_file_path


def create_bug_title(hostname_regex, hostname, first_line, is_serial_log=False):
    time_pattern = r'.*\w+\s+\d+\s+\d+:\d+:\d+\.\d+\s+'
    if not re.findall(time_pattern, first_line):
        time_pattern = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+' + (r'\S*\s+' if hostname_regex == r'\S+' else r'.*')
    log_prefix = time_pattern + hostname_regex + r'\s'
    bug_title = re.sub(log_prefix, '', first_line)
    bug_title = re.sub(r'message repeated \d+ times: \[ (.*?)\]', r'\1', bug_title)
    # mask all the hostname in the bug title
    bug_title = bug_title.replace(hostname, '<masked_hostname>')
    if is_serial_log:
        bug_title = "[Serial log]" + bug_title
    if len(bug_title) > BugHandlerConst.BUG_TITLE_LIMIT:
        bug_title = bug_title[:BugHandlerConst.BUG_TITLE_LIMIT]
    return bug_title


def error_to_regex(error_string):
    """
    @summary: Converts a (list of) strings to one regular expression.
    @param error_string:    the string(s) to be converted
                            into a regular expression
    @return: A SINGLE regular expression string
    """
    # -- Escapes out of all the meta characters --#
    error_string = re.escape(error_string)
    error_string = error_string.replace("\\", "\\\\")
    # -- Replaces [123.1234], [ 123.1234], [   123.1234] to one regex
    error_string = re.sub(r"\\\\\[(\\\\\s)*\d+\\\\\.\d+\\\\\]", r"\\\\[\\\\s*\\\\d+\\\\.\\\\d+\\\\]", error_string)
    # -- Replaces a white space with the white space regular expression
    error_string = re.sub(r"(\\\s+)+", "\\\\s+", error_string)
    # -- Replaces date time with regular expressions
    error_string = re.sub(r" [A-Za-z]{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [A-Z]{3} ",
                          r" [A-Za-z]{3} \\\\d{4}-\\\\d{2}-\\\\d{2} \\\\d{2}:\\\\d{2}:\\\\d{2} [A-Z]{3} ", error_string)
    # -- Replaces a hex number with the hex regular expression
    error_string = re.sub(r"0x[0-9a-fA-F]+", r"0x[\\\\d+a-fA-F]+", error_string)
    error_string = re.sub(r"\b[0-9a-fA-F]{3,}\b", r"[\\\\d+a-fA-F]+", error_string)

    # -- Replaces etp1, etp1a, etp7, etp7a to etp[0-9][a-g]
    error_string = re.sub(r"etp[1-9][0-9a-g]*", r"etp[\\\\d+a-g]*", error_string)

    # -- Replaces any remaining digits with the digit regular expression
    error_string = re.sub(r"\d+", r"\\\\d+", error_string)
    error_string = re.sub(r'"', r'\"', error_string)
    return error_string


def deduplicate_log_errors(error_groups: List[List[str]]) -> List[List[str]]:
    """
    Deduplicate error messages by their regex pattern representation.
    This helps handle error log flooding with similar or same error messages in periodic patterns.
    :param error_groups: list of lists of log errors
    :return: deduplicated error groups as a list of lists
    e.g.,
    input: [[error_a, error_b, error_c], [error_b, error_c, error_d], [error_a, error_b, error_c]]
    output: [[error_a, error_b, error_c], [error_b, error_c, error_d]]
    """
    deduplicated_error_groups = []
    seen_patterns = set()

    for error_group in error_groups:
        # Convert all errors in the group to regex patterns
        patterns = [error_to_regex(error) for error in error_group]

        # Check if all patterns in this group have been seen before
        all_seen = all(pattern in seen_patterns for pattern in patterns)

        if not all_seen:
            # Add all new patterns to seen_patterns
            seen_patterns.update(patterns)
            deduplicated_error_groups.append(error_group)

    return deduplicated_error_groups


def test_deduplicate_log_errors():
    """unit test for deduplicate_log_errors"""
    error_groups = [["error_a", "error_b", "error_c"],
                    ["error_b", "error_c", "error_d"],
                    ["error_a", "error_b", "error_c"]]
    deduplicated_error_groups = deduplicate_log_errors(error_groups)
    assert deduplicated_error_groups == [["error_a", "error_b", "error_c"], ["error_b", "error_c", "error_d"]]


def group_log_errors_by_timestamp(log_errors: str) -> List[List[str]]:
    """
    Group the log errors by timestamp: new group starts if it is bigger than 5 sec from the first line in the group.
    so we will consider it as different bug.
    :param log_errors: string containing all found errors (one per line)
    :return: error_groups, list of lists. each list is the log errors bug that related to a bug.
    """
    error_line_list = [line for line in log_errors.splitlines() if line.strip()]
    error_groups = []   # list of optional bugs, each element here is a list with log errors.
    current_group = []  # single bug log errors
    prev_timestamp = get_timestamp_from_log_line(error_line_list[0])

    for line in error_line_list:
        timestamp = get_timestamp_from_log_line(line)

        if (timestamp - prev_timestamp) > timedelta(seconds=10):
            # close the group and create new one
            error_groups.append(current_group)
            current_group = []
            prev_timestamp = timestamp

        current_group.append(line)

    if current_group:
        error_groups.append(current_group)
    return error_groups


def get_timestamp_from_log_line(line: str) -> datetime:
    result = None
    for format, length in zip(BugHandlerConst.TIMESTAMP_FORMATS, BugHandlerConst.TIMESTAMP_LENGTH):
        try:
            if "%Y" in format:
                result = datetime.strptime(line[:length], format)
            else:
                # If the timestamp doesn't show the year then we need this workaround to make sure it doesn't crash on
                # February 29 because it's an invalid date. We specify 2020 because it was a leap year.
                result = datetime.strptime("2020 " + line[:length], "%Y " + format)
        except ValueError:
            pass
    if not result:
        raise ValueError(f"Failed to parse time stamp of the following log line: {line}")
    return result


def summarize_la_bug_handler(la_bug_handler_result, bug_handler_action):
    """
    summarize the log analyzer bug handler result.
    :param la_bug_handler_result: result from the la bug handler function.
    :return: dictionary
            {
                new_bugs: {<bug_id>: <errors>},
                existing_bugs: {
                                    update_bug: {<bug_id>: <errors>},
                                    skip_update_bug: {<bug_id>: <errors>}
                                }
            }
    """
    no_action_mode = False
    create_and_update_bugs_dict = {BugHandlerConst.BUG_HANDLER_DECISION_CREATE: {},
                                   BugHandlerConst.BUG_HANDLER_DECISION_UPDATE: {},
                                   BugHandlerConst.BUG_HANDLER_DECISION_SKIP: {},
                                   BugHandlerConst.BUG_HANDLER_FAILURE: [],
                                   BugHandlerConst.NO_ACTION_MODE: [],
                                   BugHandlerConst.UPDATE_ONLY: []}
    update_only = not bug_handler_action["create"] and bug_handler_action["update"]

    for bug_handler_result_dict in la_bug_handler_result:
        bug_handler_status = bug_handler_result_dict[BugHandlerConst.BUG_HANDLER_STATUS]
        bug_handler_action = bug_handler_result_dict[BugHandlerConst.BUG_HANDLER_ACTION]
        bug_id = bug_handler_result_dict[BugHandlerConst.BUG_HANDLER_BUG_ID]
        no_action_mode = no_action_mode or bug_handler_status == 'no_action mode'
        if no_action_mode:
            no_action_errs = {
                BugHandlerConst.LA_ERROR: bug_handler_result_dict[BugHandlerConst.LA_ERROR],
                BugHandlerConst.BUG_HANDLER_ACTION: bug_handler_action
            }
            if bug_handler_action == BugHandlerConst.BUG_HANDLER_DECISION_UPDATE:
                no_action_errs[BugHandlerConst.BUG_HANDLER_BUG_ID] = bug_id
            else:
                no_action_errs[BugHandlerConst.BUG_HANDLER_BUG_ID] = ""
            create_and_update_bugs_dict[BugHandlerConst.NO_ACTION_MODE].append(no_action_errs)
        elif update_only and bug_handler_action == BugHandlerConst.BUG_HANDLER_DECISION_CREATE:
            update_only_action_errs = {
                BugHandlerConst.LA_ERROR: bug_handler_result_dict[BugHandlerConst.LA_ERROR],
                BugHandlerConst.BUG_HANDLER_ACTION: bug_handler_action
            }
            create_and_update_bugs_dict[BugHandlerConst.UPDATE_ONLY].append(update_only_action_errs)
        elif bug_handler_action in BugHandlerConst.BUG_HANDLER_SUCCESS_ACTIONS_LIST\
                and bug_handler_result_dict[BugHandlerConst.BUG_HANDLER_STATUS] in ['done', 'no_action mode']:

            create_and_update_bugs_dict[bug_handler_action].update(
                {bug_id: bug_handler_result_dict[BugHandlerConst.LA_ERROR]})
        else:
            create_and_update_bugs_dict[BugHandlerConst.BUG_HANDLER_FAILURE].append(bug_handler_result_dict)

    logger.info(f"-------create_and_update_bugs_dict is : {create_and_update_bugs_dict}-------")
    logger.info(f"-------la_bug_handler_result is : {la_bug_handler_result}-------")

    return create_and_update_bugs_dict
