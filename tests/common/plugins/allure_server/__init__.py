import time
import logging
import os
import requests
import base64
import re
import subprocess
import sys
import allure
import pytest
import copy
import socket
import json
from retry import retry
from requests.packages import urllib3
from tests.common.helpers.constants import RANDOM_SEED
from ngts.constants.constants import FILE_INCLUDE_FAILED_SANITY_CHECKER_CASE
from ngts.constants.constants import MarsConstants, SonicConst

urllib3.disable_warnings()

logger = logging.getLogger()

ALLURE_REPORT_URL = 'allure_report_url'
PYTEST_RUN_CMD = 'pytest_run_cmd'
SSL_VERIFICATION = False


def pytest_addoption(parser):
    """
    Parse pytest options
    :param parser: pytest buildin
    """
    parser.addoption('--allure_server_addr', action='store', default=None, help='Allure server address: IP/domain name')
    parser.addoption('--allure_server_port', action='store', default=5050, help='Allure server port')
    parser.addoption('--allure_server_project_id', action='store', default=None, help='Allure server project ID')


def pytest_sessionfinish(session, exitstatus):
    """
    Pytest hook which are executed after all tests before exist from program
    :param session: pytest buildin
    :param exitstatus: pytest buildin
    """
    if not session.config.getoption("--collectonly"):
        allure_server_addr = session.config.option.allure_server_addr
        allure_server_port = session.config.option.allure_server_port
        allure_server_project_id = session.config.option.allure_server_project_id
        #  allure project id doesn't support '_' and upper case
        if allure_server_project_id:
            allure_server_project_id = allure_server_project_id.replace('_', '-').lower()

        if allure_server_addr:
            allure_report_dir = session.config.option.allure_report_dir
            if allure_report_dir:
                session_info_dict = {}
                try:
                    session_info_dict = get_setup_session_info(session)
                except Exception as err:
                    logger.warning('Can not get session info for Allure report. Error: {}'.format(err))

                if session_info_dict:
                    export_session_info_to_allure(session_info_dict, allure_report_dir)

                try:
                    report_url = None
                    allure_server_obj = AllureServer(allure_server_addr, allure_server_port, allure_report_dir,
                                                     allure_server_project_id)
                    report_url = allure_server_obj.generate_allure_report()

                except Exception as err:
                    logger.error('Failed to send-results?project_id= allure report to server. Allure report not available. '
                                 '\nError: {}'.format(err))
                finally:
                    session.config.cache.set(ALLURE_REPORT_URL, report_url)

            else:
                logger.error('PyTest argument "--alluredir" not provided. Impossible to generate Allure report')


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    logger.info('Pytest terminal summary started')
    if config.option.allure_server_addr:
        logger.info('Allure server address option exists')
        report_url = config.cache.get(ALLURE_REPORT_URL, None)
        if report_url:
            logger.info('Allure report URL: {}'.format(report_url))
        else:
            logger.info('Can not get Allure report URL. Please check logs')
    else:
        logger.info('Allure server address option does not exist')


def get_dut_info(ansible_dir, dut_host):
    original_dir = os.getcwd()
    os.chdir(ansible_dir)

    cmd = "ansible -m command -i inventory {} -a 'show version'".format(dut_host)
    version_output = subprocess.check_output(cmd, shell=True).decode('utf-8')
    cmd = "ansible -m command -i inventory {} -a 'sonic-cfggen -y \
        /etc/sonic/sonic_version.yml -v branch'".format(dut_host)
    branch = subprocess.check_output(cmd, shell=True).splitlines()[-1].decode('utf-8')
    os.chdir(original_dir)

    version_reg = re.compile(r"sonic software version: +([^\s]+)\s", re.IGNORECASE)
    platform_reg = re.compile(r"platform: +([^\s]+)\s", re.IGNORECASE)
    hwsku_reg = re.compile(r"hwsku: +([^\s]+)\s", re.IGNORECASE)
    asic_reg = re.compile(r"asic: +([^\s]+)\s", re.IGNORECASE)

    version = version_reg.findall(version_output)[0] if version_reg.search(version_output) else ""
    platform = platform_reg.findall(version_output)[0] if platform_reg.search(version_output) else ""
    hwsku = hwsku_reg.findall(version_output)[0] if hwsku_reg.search(version_output) else ""
    asic = asic_reg.findall(version_output)[0] if asic_reg.search(version_output) else ""

    return version, platform, hwsku, asic, branch


def get_setup_session_info(session):
    ansible_dir = get_ansible_path(session)
    dut_hosts = session.config.option.ansible_host_pattern.split(",")
    logger.info(f"dut hosts are:{dut_hosts}")

    version_list = []
    platform_list = []
    hwsku_list = []
    asic_list = []
    branch_list = []
    mars_session_id = session.config.option.session_id
    for dut_host in dut_hosts:
        version, platform, hwsku, asic, branch = get_dut_info(ansible_dir, dut_host)
        version_list.append(version)
        platform_list.append(platform)
        hwsku_list.append(hwsku)
        asic_list.append(asic)
        branch_list.append(branch)

    # Save basic session info in a cache which can be used to update the Mars respond DB
    # Assuming the version/platform/hwsku/asic are the same for all DUTs
    data_to_cache = {
        "dut_names": ",".join(dut_hosts),
        "os_version": version_list[0],
        "branch": branch_list[0],
        "platform": platform_list[0],
        "hwsku": hwsku_list[0],
        "asic": asic_list[0]
    }
    cache_path = os.path.join(MarsConstants.SONIC_MGMT_DIR, SonicConst.BASIC_SESSION_INFO_FILE_NAME)
    if not os.path.exists(cache_path):
        with open(cache_path, "w") as f:
            json.dump(data_to_cache, f)

    random_seed = session.config.cache.get(RANDOM_SEED, None)
    pytest_run_cmd_args = session.config.cache.get(PYTEST_RUN_CMD, None)
    host_executor_ip = get_test_executor_host_ip_address()

    failed_sanity_check_cases = get_failed_sanity_check_cases()
    result = {}
    if failed_sanity_check_cases:
        result.update(
            {
                "Failed_sanity_check_cases":
                    f"Be careful!!! the following Sanity checker cases fail: {failed_sanity_check_cases}. "
                    f"It might affect the test"
            }
        )

    result.update(
        {
            "Dut_host": ", ".join(dut_hosts),
            "Version": ", ".join(version_list),
            "Platform": ", ".join(platform_list),
            "HwSKU": ", ".join(hwsku_list),
            "Sonic_mgmt_branch": get_sonic_mgmt_branch(session),
            "Executor_IP": host_executor_ip,
            "Mars_Session": mars_session_id,
            "PyTest_args": pytest_run_cmd_args,
            "ASIC": ",".join(asic_list),
            "Random_seed": random_seed,
        }
    )
    job_name = os.environ.get("REGRESSION_TYPE")
    # only show metadata for CI jobs, no need for regression runs
    if job_name and job_name != "regression":
        result.update({"Job_name": job_name})
        ci_log_folder = os.environ.get("LOG_FOLDER")
        if ci_log_folder:
            segs = ci_log_folder.split(os.sep)
            if len(segs) > 2:
                result.update({"Build_ID": segs[-3]})
    return result


def _run_git(repo_dir, *git_args):
    repo_dir = os.path.realpath(str(repo_dir))
    for safe_dir in (repo_dir, "*"):
        cmd = ["git", "-c", "safe.directory={}".format(safe_dir), "-C", repo_dir, *git_args]
        try:
            return subprocess.check_output(cmd, stderr=subprocess.PIPE).decode("utf-8").strip()
        except subprocess.CalledProcessError as err:
            stderr = err.stderr.decode("utf-8", errors="replace") if err.stderr else ""
            if safe_dir == "*" or "dubious ownership" not in stderr:
                raise
            logger.debug('git rejected the ownership of "{}", retrying with safe.directory=*'.format(repo_dir))


def get_sonic_mgmt_branch(session):
    env_branch = os.environ.get("SONIC_MGMT_BRANCH", "").strip()
    if env_branch:
        return env_branch.splitlines()[0].strip()

    repo_dir = session.fspath.dirname
    try:
        branch = _run_git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
        return branch if branch != "HEAD" else _run_git(repo_dir, "rev-parse", "--short", "HEAD")
    except Exception as err:
        logger.warning('Can not get sonic-mgmt branch. Error: {}'.format(err))
        return "unknown"


def get_test_executor_host_ip_address():
    ip_index = 0
    dummy_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dummy_socket.connect(("8.8.8.8", 80))
    host_ip = dummy_socket.getsockname()[ip_index]
    return host_ip


def export_session_info_to_allure(session_info_dict, allure_report_dir):
    allure_env_file_name = 'environment.properties'
    allure_env_file_path = os.path.join(allure_report_dir, allure_env_file_name)
    with open(allure_env_file_path, 'w') as env_file_obj:
        for item, value in session_info_dict.items():
            env_file_obj.write('{}={}\n'.format(item, value))


def get_ansible_path(session):
    sonic_mgmt_dir_path = session.fspath.dirname
    ansible_dir = os.path.join(sonic_mgmt_dir_path, 'ansible')

    if not os.path.exists(ansible_dir):
        raise FileNotFoundError('Ansible path "{}" does not exist'.format(ansible_dir))

    return ansible_dir


def get_time_stamp_str():
    """
    This method return string with current time
    :return: string, example: 16063138520755782
    """
    current_time = time.time()
    current_time_without_dot = str(current_time).replace('.', '')
    return current_time_without_dot


@pytest.fixture(autouse=True, scope='session')
def cache_pytest_session_run_cmd(request):
    """
    Fixture which save pytest run command(similar to command provided by user) to pytest cache to be accessible from
    other methods
    :param request: pytest buildin
    """
    pytest_run_cmd = get_pytest_run_cmd(request)
    request.session.config.cache.set(PYTEST_RUN_CMD, pytest_run_cmd)

    yield


@pytest.fixture(name="pytest_run_test_cmd", autouse=True)
def attach_pytest_specific_test_run_cmd_to_allure_report(request):
    """
    Fixture which attach pytest run command string for specific test case into allure report
    :param request: pytest buildin
    """
    pytest_run_cmd = get_pytest_run_cmd(request, get_current_test_run_cmd=True)
    allure.attach(pytest_run_cmd, PYTEST_RUN_CMD, allure.attachment_type.TEXT)

    yield


def get_pytest_run_cmd(request, get_current_test_run_cmd=False):
    """
    This method gets pytest run command and based on it - it can return original pytest run command or
    build command for run only specific test case
    :param request: pytest buildin
    :param get_current_test_run_cmd: if True - will return command for run specific test only(by default return
    original pytest run cmd)
    :return: string with pytest run command
    """
    pytest_cmd_line_args = copy.deepcopy(sys.argv)
    new_test_path = None
    test_path_args_index = None
    allure_server_project_id_index = None

    for arg in pytest_cmd_line_args:

        # update the pytest cmd for community case
        if "pytest/__main__.py" in arg:
            pytest_cmd_line_args[pytest_cmd_line_args.index(arg)] = "python3 -m pytest"

        if '--allure_server_project_id' in arg:
            allure_server_project_id_index = pytest_cmd_line_args.index(arg)
            continue

        for specific_arg in ['-k=', '--inventory=', '--allure_server_addr=']:
            if specific_arg in arg:
                specific_arg_value = arg.split(specific_arg)[1]
                pytest_cmd_line_args[pytest_cmd_line_args.index(arg)] = '{}"{}"'.format(specific_arg,
                                                                                        specific_arg_value)
        for specific_arg in ['-k']:
            if specific_arg == arg:
                specific_arg_value = pytest_cmd_line_args[pytest_cmd_line_args.index(arg) + 1]
                pytest_cmd_line_args[pytest_cmd_line_args.index(arg)+1] = f'"{specific_arg_value}"'

        if get_current_test_run_cmd:
            # If need pytest run command for specific test only - then building args with path to test case
            nodeid_test_path = request.node.nodeid.split('::')[0]
            try:
                path_to_test_dir = request.config.inifile.dirname
            except AttributeError:
                logger.warning('Can not get pytest run command for specific test case, pytest session run cmd will be '
                               'attached to Allure report')
                continue

            full_path_to_test = os.path.join(path_to_test_dir, nodeid_test_path)
            if os.path.exists(full_path_to_test):
                if arg in full_path_to_test:
                    test_path_args_index = pytest_cmd_line_args.index(arg)
                    new_test_path = os.path.join(path_to_test_dir, request.node.nodeid)

    if new_test_path:
        # Replace original path to test by path to specific test only
        pytest_cmd_line_args[test_path_args_index] = new_test_path

    if allure_server_project_id_index:
        # Remove --allure_server_project_id argument from arguments to prevent flood in allure history
        pytest_cmd_line_args.pop(allure_server_project_id_index)

    cmd = ' '.join(pytest_cmd_line_args)

    return cmd


def get_failed_sanity_check_cases():
    failed_cases = None
    if os.path.exists(FILE_INCLUDE_FAILED_SANITY_CHECKER_CASE):
        with open(FILE_INCLUDE_FAILED_SANITY_CHECKER_CASE) as f:
            failed_cases = f.read()
            logger.info(f"sanity check failed cases:{failed_cases}")
    return failed_cases


class AllureServer:
    def __init__(self, allure_server_ip, allure_server_port, allure_report_dir, project_id=None):
        self.allure_report_dir = allure_report_dir
        if allure_server_port:
            self.base_url = 'http://{}:{}/allure-docker-service'.format(allure_server_ip, allure_server_port)
        else:
            self.base_url = 'https://{}/allure-docker-service'.format(allure_server_ip)
        self.project_id = project_id if project_id else get_time_stamp_str()
        self.http_headers = {'Content-type': 'application/json'}

    def generate_allure_report(self):
        """
        This method creates new project(if need) on allure server, uploads test results to server and generates report
        """
        self.create_project_on_allure_server()
        self.clean_results_on_allure_server()
        self.upload_results_to_allure_server()
        report_url = self.generate_report_on_allure_server()
        logger.info('Allure report URL: {}'.format(report_url))
        return report_url

    @retry(Exception, tries=2, delay=5)
    def create_project_on_allure_server(self):
        """
        This method creates new project(if need) on allure server
        """
        data = {'id': self.project_id}
        url = self.base_url + '/projects'

        if requests.get(url + '/' + self.project_id, verify=SSL_VERIFICATION).status_code != 200:
            logger.info('Creating project {} on allure server'.format(self.project_id))
            start_time = time.time()
            response = requests.post(url, json=data, headers=self.http_headers, verify=SSL_VERIFICATION)
            diff_time = time.time() - start_time
            logger.info("Creating project {} takes {}".format(self.project_id, diff_time))
            # wait several seconds, it might improve the probability for uploading results successfully
            # Maybe when creating new project, server side need some time to prepare resource
            wait_time_for_new_project = 5
            time.sleep(wait_time_for_new_project)
            if response.raise_for_status():
                logger.error('Failed to create project on allure server, error: {}, \n status_code :{}'.format(
                    response.content, response.status_code))
        else:
            logger.info('Allure project {} already exist on server. No need to create project'.format(self.project_id))

    @retry(Exception, tries=5, delay=5)
    def upload_results_to_allure_server(self):
        """
        This method uploads files from allure results folder to allure server
        """
        data = {'results': self.get_allure_files_content()}
        url = self.base_url + '/send-results?project_id=' + self.project_id
        logger.info('Sending allure results to allure server')
        start_time = time.time()
        response = requests.post(url, json=data, headers=self.http_headers, verify=SSL_VERIFICATION,
                                 timeout=300)
        diff_time = time.time() - start_time
        logger.info("uploading allure results takes {}".format(diff_time))
        if response.status_code != 200:
            logger.error('Failed to upload results to allure server, error: {}. \n status_code: {},'
                         ' \n headers is :{}, \n URL :{}, \n SSL_VERIFICATION :{}'.format(
                             response.content, response.status_code, self.http_headers, url, SSL_VERIFICATION))
            for result in data["results"]:
                if "json" in result["file_name"]:
                    logger.info(result)
            import shutil
            import datetime
            import os
            now_str = datetime.datetime.now().strftime("%y-%m-%d-%H-%M-%S")
            dest_folder = "/auto/sw_regression/system/SONIC/MARS/results/allure_400_log/{}/{}".format(self.project_id, now_str)
            os.makedirs(dest_folder)
            logger.info("dest folder :{}".format(dest_folder))
            for result in data["results"]:
                file_name = '/tmp/allure-results/{}'.format(result["file_name"])
                logger.info("file name:{}".format(file_name))
                shutil.copy(file_name, dest_folder)
        response.raise_for_status()

    def get_allure_files_content(self):
        """
        This method creates a list all files under allure report folder
        :return: list with allure folder content, example [{'file1': 'file content'}, {'file2': 'file2 content'}]
        """
        files = os.listdir(self.allure_report_dir)
        results = []

        for file in files:
            result = {}
            file_path = self.allure_report_dir + "/" + file
            if os.path.isfile(file_path):
                try:
                    with open(file_path, "rb") as f:
                        content = f.read()
                        if content.strip():
                            b64_content = base64.b64encode(content)
                            result['file_name'] = file
                            result['content_base64'] = b64_content.decode('UTF-8')
                            results.append(result)
                finally:
                    f.close()
        return results

    @retry(Exception, tries=2, delay=5)
    def generate_report_on_allure_server(self):
        """
        This method would generate the report on the remote allure server and display the report URL in the log
        """
        logger.info('Generating report on allure server')
        url = self.base_url + '/generate-report?project_id=' + self.project_id
        start_time = time.time()
        response = requests.get(url, headers=self.http_headers, timeout=120, verify=SSL_VERIFICATION)
        diff_time = time.time() - start_time
        logger.info("generate allure report takes {}".format(diff_time))
        logger.info('Finish generating report on allure server')
        if response.raise_for_status():
            logger.error('Failed to generate report on allure server, error: {}'.format(response.content))
        else:
            report_url = response.json()['data']['report_url']
            logger.info('Allure report URL: {}'.format(report_url))
            return report_url

    def clean_results_on_allure_server(self):
        """
        This method would clean results for project on the remote allure server
        """
        url = self.base_url + '/clean-results?project_id=' + self.project_id
        response = requests.get(url, headers=self.http_headers, verify=SSL_VERIFICATION)

        if response.raise_for_status():
            logger.error('Failed to clean results on allure server, error: {}'.format(response.content))
