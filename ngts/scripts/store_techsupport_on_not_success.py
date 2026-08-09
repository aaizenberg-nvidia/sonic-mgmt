import allure
import os
import time
import logging
import pytest

from devts.infra.tools.general_constants.constants import DefaultTestServerCred, DefaultConnectionValues
from ngts.scripts.sonic_deploy.community_only_methods import is_dualtor_topo
from ngts.cli_wrappers.nvue.nvue_cli import NvueCli
from ngts.constants.constants import PlayersAliases
from ngts.nvos_tools.system.System import System
from .collect_simx_logs_on_not_success import dump_simx_data, dump_simx_syslog_data


logger = logging.getLogger()
FETCH_THECHSURPORT_STATUS = False
# Because there is memory buffer limitation when use tee to get log by telnet， When syslog size is too larger,
# we cannot fetch all log one time, so define READ_LINE_STEP as the max line number to fetch syslog for every one time
READ_LINE_STEP = 10000
DUAL_TOR_SIMULATOR_LOG_PREFIXE_REGEX_LIST = ['mux_simulator_*', 'nic_simulator_*']
DUAL_TOR_SIMULATOR_LOG_FOLDER = '/tmp/'


@pytest.fixture(scope='function')
def session_id_arg(request):
    """
    Method for get session id from pytest arguments
    :param request: pytest builtin
    :return: session id, i.e. 4973482
    """
    return request.config.getoption('--session_id')


@pytest.fixture(scope='function')
def duration(request):
    """
    Method for get techsupport duration from pytest arguments in seconds
    :param request: pytest builtin
    :return: techsupport duration, i.e. 7200
    """
    return request.config.getoption('--tech_support_duration')


def get_nvos_techsupport_info(dut_cli_object, duration, dumps_folder, dut_engine):
    """
    :param dut_cli_object:
    :param duration:
    :param dumps_folder:
    :return: dumps_folder: NVOS dump folders will be on a separated folder (not in the logs folder)
             tar_file: NVOS file name will include the session id
             tarball_file_name: the full path for dest + file name
    """
    with allure.step('get session_id and dumps folder name'):
        dump_folder = dumps_folder.split('/')[-1]
        session_id = dumps_folder.split('/')[-2]
        logger.info('session_id = {}, dump folder name = {}'.format(session_id, dump_folder))

    with allure.step('generate tarball file name'):
        dumps_folder = dumps_folder.rpartition('/')[:-2][0]
        dumps_folder = dumps_folder.rpartition('/')[:-2][0]
        dumps_folder = dumps_folder + '/' + dump_folder
        logger.info('NVOS dump folder path {}'.format(dumps_folder))

    with allure.step('generate the file name'):
        tar_file = dut_cli_object.general.generate_techsupport(duration)
        logger.info('NVOS tar_file {}'.format(tar_file))
        tarball_file_name = str(session_id) + '_' + tar_file.rpartition('/')[-1]
        logger.info('NVOS tarball_file_name {}'.format(tarball_file_name))

    with allure.step('testing the flow of NVOS commands'):
        system = System(None)
        temp_tar_file, duration = system.techsupport.action_generate(engine=dut_engine)
        logger.info('NVOS temp_tarball_file_name {}'.format(temp_tar_file))

    return dumps_folder, tar_file, tarball_file_name


def get_hypervisor_engine(topology_obj):
    """
    Method for getting the engine of the hypervisor
    :param topology_obj: topology_obj fixture
    :return: the engine of the hypervisor
    """
    hyper_engine = topology_obj.players['hypervisor']['engine']
    hyper_engine.username = DefaultTestServerCred.DEFAULT_USERNAME
    hyper_engine.password = DefaultTestServerCred.DEFAULT_PASS
    return hyper_engine


def is_file_exist(hypervisor_engine, folder, file):
    """
    Method for file exist validation
    :param hypervisor_engine: hypervisor engine
    :param folder: the check folder
    :param file: the check file
    :return: None or matched file number
    """
    res = hypervisor_engine.run_cmd(f"ls -l {folder} | grep '{file}' | wc -l")
    return res if res != '0' else None


def collect_dualtor_simulator_log(hypervisor_engine, target_folder):
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
            dest_tar_file_path = target_folder + '/' + tar_file_name
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


@pytest.mark.disable_loganalyzer
def test_store_techsupport_on_not_success(topology_obj, duration, dumps_folder, is_simx, is_air, sonic_topo):
    dut_cli_object_list = [topology_obj.players['dut']['cli']]
    dut_engine_list = [topology_obj.players['dut']['engine']]
    if topology_obj.players.get('dut-b'):
        dut_cli_object_list.append(topology_obj.players['dut-b']['cli'])
        dut_engine_list.append(topology_obj.players['dut-b']['engine'])
    for alias in PlayersAliases.multi_duts_list:
        if topology_obj.players.get(alias):
            dut_cli_object_list.append(topology_obj.players[alias]['cli'])
            dut_engine_list.append(topology_obj.players[alias]['engine'])

    for i in range(len(dut_cli_object_list)):
        with allure.step('Generating a sysdump'):
            if isinstance(dut_cli_object_list[i], NvueCli):
                dumps_folder, tar_file, tarball_file_name = get_nvos_techsupport_info(dut_cli_object_list[i], duration,
                                                                                      dumps_folder, dut_engine_list[i])
            else:
                tar_file = dut_cli_object_list[i].general.generate_techsupport(duration)
                tarball_file_name = str(tar_file.replace('/var/dump/', ''))

            logger.info("Dump was created at: {}".format(tar_file))

        with allure.step('Copy dump: {} to log folder {}'.format(tarball_file_name, dumps_folder)):
            dest_file = dumps_folder + '/sysdump_' + tarball_file_name
            logger.info('Copy dump {} to log folder {}'.format(tar_file, dumps_folder))
            dut_engine_list[i].copy_file(source_file=tar_file,
                                         dest_file=dest_file,
                                         file_system='/',
                                         direction='get',
                                         overwrite_file=True,
                                         verify_file=False)
            os.chmod(dest_file, 0o777)
            logger.info('Dump file location: {}'.format(dest_file))
    global FETCH_THECHSURPORT_STATUS
    FETCH_THECHSURPORT_STATUS = True

    if is_simx and not is_air:
        dump_simx_data(topology_obj, dumps_folder)

    if sonic_topo and is_dualtor_topo(sonic_topo):
        hyper_engine = get_hypervisor_engine(topology_obj)
        collect_dualtor_simulator_log(hyper_engine, dumps_folder)

    logger.info("Script Finished")


@pytest.mark.disable_loganalyzer
def test_store_simx_dump_on_not_success(topology_obj, dumps_folder, is_simx, is_air):
    if is_simx and not is_air:
        dump_simx_data(topology_obj, dumps_folder)


@pytest.mark.disable_loganalyzer
def test_store_simx_dump_syslog_on_not_success(topology_obj, dumps_folder, is_simx, is_air):
    if not FETCH_THECHSURPORT_STATUS:
        if is_simx and not is_air:
            with allure.step('Fetch syslog for simx switch by telnet'):
                dump_simx_syslog_data(topology_obj, dumps_folder)
