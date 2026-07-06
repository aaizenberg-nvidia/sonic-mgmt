import concurrent
import datetime
import logging
import os
import threading
import pytest
import time
import json
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from ngts.constants.constants import CometConsts
from ngts.tools.infra import get_infra_type, CANONICAL_INFRA_TYPE, NVOS_INFRA_TYPE
from tests.common.plugins.cli_coverage.cli_coverage import CliCoverage
from ngts.cli_wrappers.common.general_clis_common import GeneralCliCommon

logger = logging.getLogger()
executor = ThreadPoolExecutor(max_workers=1)
data_lock = threading.Lock()
thread_local_data = threading.local()
running_threads = {}
commands_list_lock = threading.Lock()
commands_list = []


def get_sonic_image_info(request):
    """
    :param request:
    :return: sonic_branch, sonic_version and hwsku
    """
    test_type = get_infra_type(request)
    if test_type == CANONICAL_INFRA_TYPE:
        sonic_branch = request.getfixturevalue('sonic_branch')
        sonic_version = request.getfixturevalue('sonic_version')
        hwsku = request.getfixturevalue('platform_params').hwsku
    else:
        sonic_branch = request.getfixturevalue('duthost').sonic_release
        if sonic_branch == 'none':
            sonic_branch = 'master'
        sonic_branch = sonic_branch
        sonic_version = request.getfixturevalue('duthost').os_version
        hwsku = request.getfixturevalue('duthost').facts['hwsku']
    return sonic_branch, sonic_version, hwsku

class SessionName:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.session_name = None
        return cls._instance

    def set_session_name(self, name):
        if self.session_name is None:
            self.session_name = name

    def get_session_name(self):
        return self.session_name


def is_dut_in_onie(request):
    coverage = CliCoverage(request)

    try:
        dut = request.getfixturevalue('engines').dut if coverage.is_canonical_setup else request.getfixturevalue('duthost')
        engine = dut if coverage.is_canonical_setup else dut.shell
        general_cli = GeneralCliCommon(engine)
        try:
            engine.run_cmd('show version', validate=False)
            logger.info("Device is in SONiC mode")
            return False
        except Exception:
            # If show version fails, we're likely not in SONiC mode
            pass

        is_os_onie = general_cli.check_dut_in_onie_install_status()
        return is_os_onie
    except Exception as err:
        logger.error(f"Error checking ONIE status: {err}")
        return False

def is_test_deploy_and_upgrade():
    """
    Check if the test is an upgrade switch test.
    """
    return "test_deploy_and_upgrade" in os.getenv("PYTEST_CURRENT_TEST", "").split(" ")[0].split("::")[-1]


if os.environ.get("REGRESSION_TYPE") == "regression":
    @pytest.fixture(scope="session")
    def initialize_hit_list_per_session(request):
        """
         initialize data for Comet JSON file and creates it with updated data at the end of the session.
        :param request:
        """
        test_type = get_infra_type(request)
        if test_type == NVOS_INFRA_TYPE:
            logger.info("NVOS infra type, skipping CLI coverage plugin.")
            yield None
            return

        if is_test_deploy_and_upgrade():
            # A deploy that installs a BMC image power-cycles the whole board, so the
            # host-switch is rebooted into ONIE and is not reachable in SONiC until it
            # is re-installed. CLI coverage needs to SSH the switch to read its SONiC
            # version, so skip it whenever a BMC image is being deployed (BMC-only or
            # dual BMC + switch) to avoid SSH timeouts on the unreachable DUT.
            base_version_bmc = request.config.getoption('--base-version-bmc', default='') or ''
            if base_version_bmc:
                logger.info("BMC image deploy detected, skipping CLI coverage plugin.")
                yield None
                return
            if is_dut_in_onie(request):
                logger.info("DUT is in ONIE mode, skipping CLI coverage plugin.")
                yield None
                return
        current_date = datetime.datetime.now().date()
        file_path = CometConsts.COMET_FILE_PATH + f'/{current_date}/'
        if not os.path.exists(file_path):
            os.makedirs(file_path)
            os.chmod(file_path, 0o755)

        sonic_branch, sonic_version, hwsku = get_sonic_image_info(request)
        if sonic_version and 'master' in sonic_version:
            logger.info("Skipping CLI coverage plugin for master branch.")
            yield None
            return

        start_time = time.time()
        full_json = OrderedDict([
                ("project", "Sonic"),
                ("department", "verification"),
                ("sw version", sonic_branch),
                ("system type", hwsku),
                ("build id", sonic_version),
                ("test type", test_type),
                ("test name", ''),
                ("test result", ''),
                ("start time", start_time),
                ("end time", ''),
                ("commands hit", [])])

        shared_data = {"status": "Success", "commands": set()}

        yield shared_data
        session_metadata = SessionName()
        setup_name = request.config.option.ansible_host_pattern
        full_json['end time'] = time.time()
        full_json["commands hit"] = commands_list
        full_json["test result"] = shared_data["status"]
        session_name = session_metadata.get_session_name()
        full_json["test name"] = session_name
        session_hit_list_path = f"hit_list_{session_name}_{setup_name}.json"
        session_abs_path = file_path + session_hit_list_path
        with open(session_abs_path, "w") as file:
            json.dump(full_json, file)


    @pytest.fixture(scope="function", autouse=True)
    def collect_cli_coverage(request, initialize_hit_list_per_session):
        """
        Collects CLI coverage for test if it's not skipped
        :param request:
        :param initialize_hit_list_per_session:
        :return:
        """
        if initialize_hit_list_per_session is None:
            logger.info("CLI coverage collection skipped because DUT is in ONIE install mode.")
            yield
            return
        session_metadata = SessionName()
        if session_metadata.get_session_name() is None:
            session_metadata.set_session_name(os.path.basename(request.node.fspath).replace('.py', ''))
        shared_data = initialize_hit_list_per_session
        yield
        cli_coverage = CliCoverage(request)
        if (hasattr(request.node, 'rep_setup') and request.node.rep_setup.skipped) or \
                (hasattr(request.node, 'rep_call') and request.node.rep_call.skipped):
            logger.info("Test is skipped, skipping collect_cli_coverage.")
            return
        if hasattr(request.node, 'rep_call'):
            status = request.node.rep_call.failed
            if status:
                shared_data["status"] = "Failed"
        else:
            logger.error(f"Test does not have a call phase; skipping collect_cli_coverage.")
            return
        with data_lock:
            thread_local_data.table_data = cli_coverage.get_config_data()
            thread_local_data.test_id = request.node.name
            thread_local_data.commands_set = shared_data["commands"]
            thread_local_data.commands_list = commands_list

        with commands_list_lock:
            future = executor.submit(
                run_cli_coverage,
                cli_coverage,
                thread_local_data.table_data,
                thread_local_data.commands_set,
                thread_local_data.commands_list
            )
            running_threads[future] = thread_local_data.test_id

else:
    logger.info("CLI coverage data is collected only in regression runs or on SONiC.")

def run_cli_coverage(cli_coverage, table_data, commands_set, commands_list):
    try:
        cli_coverage.config_db = table_data
        with commands_list_lock:
            cli_coverage.save_hit_list(table_data, commands_set, commands_list)
    except Exception:
        raise

def wait_for_threads_to_complete(timeout=10):
    """
    Wait for all running threads to complete within timeout.
    """
    start_time = time.time()
    failed_futures = []
    for future, test_name in running_threads.items():
        remaining_time = timeout - (time.time() - start_time)
        if remaining_time <= 0:
            logger.error("Timeout reached before all threads finished.")
            break
        try:
            if future.done():
                logger.info(f"Thread for test {test_name} completed.")
            else:
                logger.info(f"Waiting for thread {test_name} to complete...")
            future.result(timeout=remaining_time)
        except concurrent.futures.TimeoutError:
            pytest.fail(f"Timeout while waiting for thread of test {test_name}.")
        except Exception as e:
            logger.error(f"Error occurred in thread for {test_name}: {e}.")
            failed_futures.append(test_name)

    if failed_futures:
        if len(failed_futures) == 1:
            pytest.fail(f"Error occurred in thread for {test_name}.")
        else:
            pytest.fail(f"Errors occurred in the following tests: {', '.join(failed_futures)}")

@pytest.fixture(scope="session", autouse=True)
def check_all_threads_status(request):
    """
    Ensure all threads are finished before the test suite ends.
    """
    yield
    wait_for_threads_to_complete(timeout=10)

