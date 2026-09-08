from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import logging
import allure
import pytest
import shlex
import json
import math
import time
import os

from ngts.constants.constants import PytestConst, SETUPS_WITH_NON_DEFAULT_PTF
from ngts.nvos_constants.constants_nvos import TopologyConsts
from ngts.scripts.sonic_deploy import community_only_methods
from ngts.tools.allure_report import allure_report_attacher
from ngts.scripts import collect_simx_logs_on_not_success
from ngts.scripts import store_techsupport_on_not_success
from ngts.conftest import update_topology_with_cli_class
from ngts.tools.topology_tools import topology_by_setup
from ngts.tools.test_utils import nvos_general_utils
from ngts.helpers import general_helper
from ngts.tools import infra

DUAL_TOR_SIMULATOR_LOG_PREFIXE_REGEX_LIST = ['mux_simulator_*', 'nic_simulator_*']
DUAL_TOR_SIMULATOR_LOG_FOLDER = '/tmp/'

logger = logging.getLogger(__name__)

test_suites_dumps = defaultdict(int)


def get_topology_obj(item):
    topology = topology_by_setup.get_topology_by_setup_name_and_aliases(item.config.option.setup_name, slow_cli=False)
    update_topology_with_cli_class(topology, item._request)
    return topology


def collect_ptf_logs(hyper_engine, dumps_folder, setup_name):
    ptf_log_file = 'ptf_logs.{}.tgz'.format(time.time_ns())
    dest_file = dumps_folder + '/' + ptf_log_file
    ptf_docker_name = f'ptf_vm-t{2 if setup_name in SETUPS_WITH_NON_DEFAULT_PTF else 1}'
    try:
        with allure.step('Generate ptf log tar file {}'.format(ptf_log_file)):
            hyper_engine.run_cmd(
                'docker exec {} tar -czvf /tmp/{} --exclude=/tmp/{} /tmp/ /var/log/tac_plus_daily.log'.format(
                    ptf_docker_name, ptf_log_file, ptf_log_file)
            )
            hyper_engine.run_cmd('docker cp {}:/tmp/{} /tmp'.format(ptf_docker_name, ptf_log_file))
            hyper_engine.run_cmd('docker exec {} rm /tmp/{}'.format(ptf_docker_name, ptf_log_file))
        with allure.step('Copy the ptf log tar file to log folder {}'.format(dumps_folder)):
            hyper_engine.run_cmd('sudo cp /tmp/{} {}'.format(ptf_log_file, dest_file))
            os.chmod(dest_file, 0o777)
            hyper_engine.run_cmd('rm /tmp/{}'.format(ptf_log_file))
        logger.info('Ptf log tar file location: {}'.format(dest_file))
    except Exception as err:
        logger.error(f'Failed to collect the ptf log files: {err}')


def is_file_exist(hypervisor_engine, folder, file):
    """
    Method for file exist validation
    :param hypervisor_engine: hypervisor engine
    :param folder: the check folder
    :param file: the check file
    :return: True if the file exists, False otherwise
    """
    safe_file = shlex.quote(file)
    safe_folder = shlex.quote(folder)
    cmd = f"find {safe_folder} -maxdepth 1 -name {safe_file} | wc -l"
    res = hypervisor_engine.run_cmd(cmd)
    return int(res.strip()) > 0


def collect_dualtor_logs(hypervisor_engine, dumps_folder):
    """
    Method for collecting dual-tor related simulator logs
    :param hypervisor_engine: the hypervisor engine
    :param target_folder: the target folder to store the simulator logs
    :return:
    """
    for log_file_regex in DUAL_TOR_SIMULATOR_LOG_PREFIXE_REGEX_LIST:
        if is_file_exist(hypervisor_engine, DUAL_TOR_SIMULATOR_LOG_FOLDER, log_file_regex):
            name_prefix = time.strftime('%Y_%b_%d_%H_%M_%S')
            tar_file_name = log_file_regex[:-1] + name_prefix + '.tar.gz'
            tar_file_path = DUAL_TOR_SIMULATOR_LOG_FOLDER + tar_file_name
            dest_tar_file_path = os.path.join(dumps_folder, tar_file_name)
            log_files = hypervisor_engine.run_cmd(f"ls -l {DUAL_TOR_SIMULATOR_LOG_FOLDER} | grep '{log_file_regex}'")
            try:
                logger.info(f"Compressing: \n{log_files}")
                hypervisor_engine.run_cmd(f"tar -czvf {tar_file_path} {DUAL_TOR_SIMULATOR_LOG_FOLDER + log_file_regex}")
                logger.info(f"Copying {tar_file_path} to {dest_tar_file_path}")
                hypervisor_engine.copy_file(source_file=tar_file_path,
                                            dest_file=dest_tar_file_path,
                                            file_system='/',
                                            direction='get',
                                            overwrite_file=True,
                                            verify_file=False)
                os.chmod(dest_tar_file_path, 0o777)
            except Exception as err:
                logger.error(f"Error exist during collection of dualtor simulator logs: {err}")
            finally:
                hypervisor_engine.run_cmd(f"rm -f {tar_file_path}")


def _collect_one_neighbor_dump_nbrhost(neighbor_name, host, dumps_folder, duration, item_clean_name):
    """
    Generate dump on one neighbor using community nbrhosts host (SonicHost: shell + fetch).
    Mirrors collect_techsupport_on_dut(request, nbrhosts[nbr]['host']) from tests/conftest.py.
    """
    res = host.shell('sudo generate_dump -s "-{} seconds"'.format(duration))
    if res.get('rc') != 0:
        logger.error(f"generate_dump failed on {neighbor_name}: {res}")
        return
    remote_dump_path = res.get('stdout_lines', [])[-1] if res.get('stdout_lines') else None
    if not remote_dump_path:
        logger.error(f"No dump path in output for {neighbor_name}: {res}")
        return
    dest_file = os.path.join(dumps_folder, f'sysdump_{neighbor_name}_{item_clean_name}.tar.gz')
    try:
        host.fetch(src=remote_dump_path, dest=f'{dumps_folder}/', flat=True)
        local_fetched = os.path.join(dumps_folder, os.path.basename(remote_dump_path))
        if os.path.isfile(local_fetched) and local_fetched != dest_file:
            os.rename(local_fetched, dest_file)
        os.chmod(dest_file, 0o777)
        complete_msg = f"Completed {neighbor_name} sysdump: {dest_file}"
        with allure.step(complete_msg):
            logger.info(complete_msg)
    except Exception as err:
        logger.error(f"Failed to fetch dump from {neighbor_name}: {err}")
    try:
        host.shell('sudo rm -rf {}'.format(remote_dump_path))
    except Exception as err:
        logger.warning(f"Failed to remove remote dump on {neighbor_name}: {err}")


def _get_nbrhosts_for_item(item):
    """
    Get nbrhosts: first from funcargs; if missing, try to build from tbinfo + ansible_adhoc + creds
    (only works when those fixtures are in the test's request closure, e.g. test has localhost/duthost).
    """
    nbrhosts = item.funcargs.get('nbrhosts', None)
    if nbrhosts is not None:
        return nbrhosts
    logger.warning("nbrhosts not available, skipping neighbor dumps")


def collect_neighbor_logs(item, dumps_folder, duration):
    """
    Collect dumps from all neighbor VMs in parallel. Only used for tests under tests/upgrade_path
    with sonic neighbor_type.
    """
    item_clean_name = item.name.replace('/', '_').replace('[', '_').replace(']', '_')

    nbrhosts = _get_nbrhosts_for_item(item)
    if not nbrhosts:
        logger.warning("nbrhosts not available, skipping neighbor dumps")
        return

    num_workers = min(len(nbrhosts), 8)
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {}
        for neighbor_name, device in nbrhosts.items():
            host = device['host']
            futures[executor.submit(
                _collect_one_neighbor_dump_nbrhost,
                neighbor_name, host, dumps_folder, duration, item_clean_name
            )] = neighbor_name
        for future in as_completed(futures):
            neighbor_name = futures[future]
            try:
                future.result()
            except Exception as err:
                logger.error(f"Failed to collect dump from {neighbor_name}: {err}")


def generate_and_copy_dump(item, dumps_folder, topology_obj, duration):
    switch_type = nvos_general_utils.get_switch_type(topology_obj)
    dut_engine = topology_obj.players['dut']['engine']
    allure_report_attacher.collect_stored_cmds_then_attach_to_allure_report(topology_obj)
    generate_dump_method[switch_type](topology_obj, dut_engine, dumps_folder, duration, item)
    testbed = item.config.option.testbed
    # BMC testbeds run on the BMC host directly; their topology has no 'hypervisor' player
    # and PTF/dualtor log collection does not apply.
    if switch_type == TopologyConsts.SONIC and not general_helper.is_bmc_testbed(testbed):
        hypervisor_engine = topology_obj.players['hypervisor']['engine']
        setup_name = item.config.option.setup_name
        if 'ptf-any' not in testbed:
            collect_ptf_logs(hypervisor_engine, dumps_folder, setup_name)
        if community_only_methods.is_dualtor_topo(testbed):
            collect_dualtor_logs(hypervisor_engine, dumps_folder)
        if 'upgrade_path' in item.name and 'sonic' in item.config.option.neighbor_type:
            collect_neighbor_logs(item, dumps_folder, duration)


def is_performance_setup(item):
    return "nv_performance" in item.config.option.setup_name


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """
    Techsupport creator. Will be executed at the end of test call in case of failure.
    """
    logger.debug(f"Executing pytest_runtest_makereport for {item.name}")
    outcome = yield
    rep = outcome.get_result()

    if rep.failed and os.environ.get(PytestConst.GET_DUMP_AT_TEST_FALIURE) != "False" and not is_performance_setup(item):
        logger.debug(f"Entering sysdump creation for {item.name}")
        os.environ.pop(item.name, None)
        session_id = item.config.option.session_id
        if session_id is None and item.config.option.store_dump_on_fail is not True:
            logger.info('### `session_id` and `store_dump_on_fail` flags were not provided, '
                        'sysdump will not be created ###')
            return
        suite_name = item.parent.name

        with allure.step('The test case has failed, generating a sysdump'):
            try:
                test_name = item.name.replace('/', '_')
                # Set up configuration for Community test infra as it doesn't have topology_obj
                if (topology_obj := item.funcargs.get('topology_obj')) is None:
                    topology_obj = get_topology_obj(item)
                    duration = 7200
                elif "test_sanity_checker" in suite_name:
                    duration = 7200
                else:
                    duration = get_test_duration(item)
                if (dumps_folder := item.funcargs.get('dumps_folder')) is None:
                    dumps_folder = infra.get_dumps_folder(item.config.option.setup_name, session_id, topology_obj)
                existing_dump_file = next(
                    (dump_file for dump_file in os.listdir(dumps_folder) if test_name in dump_file), None)
                if existing_dump_file and session_id:
                    dump_path = f'{dumps_folder}/{existing_dump_file}'
                    with allure.step(f'The test case has failed, dump already exists at log folder {dump_path}'):
                        pass
                elif test_suites_dumps.get(suite_name, 0) > 2:
                    logger.info('### The number of sysdumps for this test suite is more than 3, sysdump will not be created ###')
                else:
                    with allure.step('The test case has failed, generating a sysdump'):
                        # no-techsupport-on-fail flag is set in pytest_configure, pytest_addoption, _configure_skip_gen_techsupport_on_fail
                        if getattr(item.config, "_no_techsupport_on_fail") or item.config.getoption("--no-techsupport-on-fail", default=False):
                            logger.warning("Skipping sysdump generation on test failure")
                        else:
                            test_suites_dumps[suite_name] += 1
                            generate_and_copy_dump(item, dumps_folder, topology_obj, duration)
            except BaseException as err:
                error_message = f'Failed to generate/store techsupport dump.\nGot error: {err}'
                logger.error(error_message)
        topology_obj = item.funcargs.get('topology_obj')
        if topology_obj:
            allure_report_attacher.clean_stored_cmds_with_fixture_scope_list(topology_obj)
        os.environ[PytestConst.GET_DUMP_AT_TEST_FALIURE] = "True"
    else:
        logger.debug(f"Skipping sysdump creation for {item.name}")


def generate_and_copy_nvos_dump(topology_obj, dut_engine, dumps_folder, duration, item):
    with allure.step("Generating NVOS tech-support"):
        logging.info("disconnect dut engine")
        dut_engine.disconnect()
        logging.info("Generating tech-support")
        dut_engine.run_cmd('nv action generate system tech-support', validate=True)
        output = dut_engine.run_cmd('nv show system tech-support files -o json', validate=True)
        tech_support_file_on_switch = json.loads(output)["latest"]["path"]

    dest_tech_support_file = dumps_folder + '/sysdump_' + item.name.replace('/', '_') + '.tar.gz'
    copy_dump_file(dut_engine, tech_support_file_on_switch, dest_tech_support_file)
    store_dest_file_path(dest_tech_support_file, item.name.replace('/', '_'))


def generate_and_copy_cumulus_dump(topology_obj, dut_engine, dumps_folder, duration, item):
    logging.info("Generate dump for Cumulus - not implemented yet")
    pass


def need_implicit_dpu_dump(item):
    """The adapter function to check if the DPUs need to be dumped implicitly

    Args:
        item (_type_): The reference to the pytest test called

    Returns:
        bool: True if need to dump all DPUs available in the topology
    """
    if "smart_switch" in item.location[0]:
        return True
    if "smartswitch" in item.location[0]:
        return True
    if "dash" in item.location[0]:
        return True
    if item.location[0].startswith('ha/'):
        return True
    return False


def generate_and_copy_sonic_dump_runner(topology_obj, device_engine, device_alias, dumps_folder, duration, item_clean_name):
    """The function to run the separate thread to generate and copy the sysdump for the DUT

    Args:
        topology_obj (_type_): The reference to the topology object
        device_engine (_type_): The reference to the device engine
        device_alias (_type_): The alias of the device to dump
        dumps_folder (_type_): The reference to the dumps folder
        duration (_type_): The duration/timeout to generate the sysdump
        item_clean_name (_type_): The clean name of the test

    Returns:
        str: The path to the generated sysdump file
    """
    logger.info(f"Started {device_alias} sysdump")
    # Retry generate_dump if another techsupport instance is already running.
    retry_timeout = 360
    retry_interval = 30
    start_time = time.time()
    output = None
    while True:
        try:
            output = device_engine.run_cmd(f'sudo generate_dump -s \"-{duration} seconds\"', validate=True)
        except Exception as err:
            elapsed = time.time() - start_time
            if "Another instance of techsupport" in str(err) and elapsed < retry_timeout:
                logger.warning(f"Another techsupport instance is running on {device_alias}. "
                               f"Retrying in {retry_interval}s (elapsed: {elapsed:.0f}s / {retry_timeout}s)")
                time.sleep(retry_interval)
                continue
            raise
        break

    if not output or not output.strip():
        raise RuntimeError(f"generate_dump produced no output on {device_alias}")

    remote_dump_path = output.splitlines()[-1]
    logger.debug(f"Remote dump path for {device_alias}: {remote_dump_path}")
    dest_file = dumps_folder + f'/sysdump_{device_alias}_' + item_clean_name + '.tar.gz'
    logger.info(f"Destination file for {device_alias}: {dest_file}")
    copy_dump_file(device_engine, remote_dump_path, dest_file)
    logger.info(f"Completed {device_alias} sysdump: {dest_file}")
    logger.info(f"Removing the dump file from the device {device_alias}")
    device_engine.run_cmd(f'sudo rm -rf {remote_dump_path}')
    logger.info(f"Removing the dump file from the device {device_alias} completed")
    return dest_file


def get_duts_and_dpus_to_dump(topology_obj, item):
    """The function to get the DUTs and DPUs to dump

    Args:
        topology_obj (_type_): The reference to the topology object
        item (_type_): The reference to the pytest test called

    Returns:
        dict: The dictionary of the DPUs to with the DPU device info structure
    """
    duts_and_dpus_to_dump = dict()
    for dut_alias, cli_obj in general_helper.get_dut_cli_objs_from_topo_obj(topology_obj).items():
        try:
            # Add the DUT itself to the dumps
            duts_and_dpus_to_dump[dut_alias] = topology_obj.players[dut_alias]['engine']
            dpu_status = cli_obj.get_dpus_status()
        except Exception as err:
            logger.info(f"Error getting DPUs status form {dut_alias}, skipping the DPU dumps: {err}")
            continue

        for device_alias, status_dict in dpu_status.items():
            device_alias = device_alias.lower()
            if status_dict['Admin-Status'] != 'up' or status_dict['Oper-Status'] != 'Online':
                logger.warning(f"DPU {device_alias} is down or offline on {dut_alias}: {status_dict}, skipping the DPU dump")
                continue

            # for the main dut, the dpus are named as dpu0, dpu1, dpu2, dpu3
            # for the other duts, the dpus are prefixed with the dut alias
            # as dut-b-dpu0, dut-b-dpu1, dut-b-dpu2, dut-b-dpu3
            dut_player_key = topology_by_setup.get_dpu_player_key(dut_alias, device_alias)

            dpu_info = topology_obj.players.get(dut_player_key)
            if not dpu_info:
                logger.warning(f"No DPU player available for {dut_player_key} on {dut_alias}, skipping the DPU dump")
                continue
            duts_and_dpus_to_dump[dut_player_key] = dpu_info['engine']
    return duts_and_dpus_to_dump


def collect_all_dumps(topology_obj, duts_to_dump, dumps_folder, duration, item_clean_name):
    """The function to collect all the sysdumps running in parallel

    Args:
        topology_obj (_type_): The reference to the topology object
        duts_to_dump (_type_): The dictionary of the DUTs to dump
        dumps_folder (_type_): The reference to the dumps folder
        duration (_type_): The duration/timeout to generate the sysdump
        item_clean_name (_type_): The clean name of the test, removed special characters

    Returns:
        str: The path to the generated sysdump DUT file
    """
    futures = dict()
    # run the sysdump in parallel for the DUT and DPUs
    logger.debug(f"Topology players counter: {len(topology_obj.players)}")
    with ThreadPoolExecutor(max_workers=len(topology_obj.players)) as executor:
        for dut_alias, dut_engine in duts_to_dump.items():
            futures[dut_alias] = executor.submit(generate_and_copy_sonic_dump_runner,
                                                 topology_obj, dut_engine, dut_alias,
                                                 dumps_folder, duration, item_clean_name)
        logger.debug("Waiting for all sysdumps to complete")
        for done_future in as_completed(futures.values()):
            try:
                complete_msg = f"Completed sysdump: {done_future.result()}"
                with allure.step(complete_msg):
                    logger.info(complete_msg)
            except Exception as err:
                logger.error(f"Error generating sysdump: {err}")
    return futures["dut"].result()


def get_dpus_from_duthosts(topology_obj, duthosts_value):
    """The function to get the DUTs to dump from the duthosts fixture. Matches the dut hosts to the available players in the topology

    Args:
        duthosts_value (_type_): The reference to the duthosts fixture

    Returns:
        dict: The dictionary of the DUTs to dump
    """
    duts_to_dump = dict()
    # Obtain the host name for all duts
    try:
        dut_hosts_set = set(dut_host.hostname for dut_host in duthosts_value)
        logger.info(f"DUT hosts set: {dut_hosts_set}")
    except Exception as err:
        dut_hosts_set = set()
        logger.warning(f"Unable to obtain the duthosts, using empty set. Error: {err}")
        # Add add the matching DUT to the duts to dump
    for dut_alias, dut_info in topology_obj.players.items():
        if dut_alias.endswith('_serial'):
            continue
        try:
            dut_hostname = dut_info['attributes'].noga_query_data['attributes']['Common']['Name']
        except Exception:
            logger.warning(f"Unable to obtain the DUT hostname for {dut_alias} from nog attributes")
            continue

        if dut_hostname not in dut_hosts_set:
            logger.info(f"DUT {dut_alias} is not in the duthosts of the caller function, dumps is not required")
            continue
        logger.info(f"Adding the DUT {dut_alias} to the duts_to_dump")
        duts_to_dump[dut_alias] = dut_info['engine']
    return duts_to_dump


def generate_and_copy_sonic_dump(topology_obj, dut_engine, dumps_folder, duration, item):
    item_clean_name = item.name.replace('/', '_').replace('[', '_').replace(']', '_')
    with allure.step('Generate Techsupport of last {} seconds'.format(duration)):
        logger.debug(f"item.location: {item.location}, item.name: {item.name}, item.originalname: {item.originalname}")
        if general_helper.is_bmc_testbed(item.config.option.testbed):
            # Test runs against the BMC; the switch DUT engine from noga points to the switch IP
            # which is not the right target. Use the BMC engine attached by conftest._attach_bmc_player.
            bmc_engine = topology_obj.players.get('bmc', {}).get('engine')
            if bmc_engine is None:
                logger.warning("BMC testbed detected but no 'bmc' player attached in topology; skipping sysdump")
                return
            duts_to_dump = {"dut": bmc_engine}
        elif item.originalname == "test_check_errors_in_log_during_deploy_sonic_image":
            # log analyzer is special since it runs on specific DUT parameter "switch" or "dpu"
            # this is to avoid the duplicate dumps of the DPU and SWITCH
            duts_to_dump = {"dut": item.funcargs['dut_host']}
        elif need_implicit_dpu_dump(item):
            # tests that need to dump all available DPUs
            logger.info(f"Implicit DPU dumps required per {item.name}")
            # get dpus to dump implicitly
            duts_to_dump = get_duts_and_dpus_to_dump(topology_obj, item)
        elif not item.module.__name__.startswith('ngts.'):
            # for community tests, check 'duthosts' fixture and add all available DPUs from it
            logger.info("Checking 'duthosts' fixture for the DPUs to dump")
            duts_to_dump = get_dpus_from_duthosts(topology_obj, item.funcargs['duthosts'])
        else:
            logger.info(f"DPU dumps are not required per {item.name}")
            duts_to_dump = dict()

        # always add the DUT itself
        if ("dut" not in duts_to_dump):
            duts_to_dump["dut"] = topology_obj.players["dut"]['engine']

        # send the sysdumps requests to the worker threads
        dut_dump_file = collect_all_dumps(topology_obj, duts_to_dump, dumps_folder, duration, item_clean_name)

    is_simx = item.funcargs.get('is_simx')
    is_air = item.funcargs.get('is_air')
    if is_simx and not is_air:
        with allure.step('Dump SIMX VM logs'):
            store_techsupport_on_not_success.dump_simx_data(topology_obj, dumps_folder, name_prefix=item_clean_name)
        with allure.step('Collect hypervisor logs'):
            collect_simx_logs_on_not_success.collect_hypervisor_logs(topology_obj, dumps_folder, name_prefix=item_clean_name)
    logger.debug(f"Storing the DUT sysdump {dut_dump_file}")
    store_dest_file_path(dut_dump_file, item_clean_name)


generate_dump_method = {TopologyConsts.NVOS: generate_and_copy_nvos_dump,
                        TopologyConsts.CL: generate_and_copy_cumulus_dump,
                        TopologyConsts.SONIC: generate_and_copy_sonic_dump}


def copy_dump_file(dut_engine, source_file, dest_file):
    copy_msg = 'Copy dump {} to log folder {}'.format(source_file, dest_file)
    with allure.step(copy_msg):
        logger.info(copy_msg)
        dut_engine.copy_file(source_file=source_file,
                             dest_file=dest_file,
                             file_system='/',
                             direction='get',
                             overwrite_file=True,
                             verify_file=False)
        os.chmod(dest_file, 0o777)


def get_test_duration(item):
    """
    Get duration of test case. Init time + test body time + 120 seconds
    :param item: pytest build-in
    :return: integer, test duration
    """
    duration = math.ceil(item.rep_setup.duration) + 120
    if hasattr(item, "rep_call"):
        duration = duration + math.ceil(item.rep_call.duration)
    if hasattr(item, "rep_teardown"):
        duration = duration + math.ceil(item.rep_teardown.duration)
    return duration


def store_dest_file_path(dest_file, test_name):
    """
    Store the dump path to environment variables to later usage by pytest_terminal_summary
    :param dest_file: dump file
    :param test_name: test_name
    """
    os.environ[test_name] = dest_file
