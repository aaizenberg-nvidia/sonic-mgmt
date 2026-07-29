#!/usr/bin/env python

from pathlib import Path
import logging
import pytest
import allure
import time
import os
import re

from ngts.scripts.code_coverage.code_coverage_consts import SharedConsts, NvosConsts, SonicConsts
from ngts.cli_wrappers.common.general_clis_common import GeneralCliCommon
from devts.infra.tools.connection_tools.linux_ssh_engine import ProxySshEngine
from ngts.nvos_tools.Devices.DeviceFactory import DeviceFactory
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.HostMethods import HostMethods
from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.scripts.code_coverage import coverage_helpers
from ngts.scripts.code_coverage.configure_switch_before_code_coverage_nvos import HOST_SERVICES_FOR_COVERAGE
from ngts.cli_wrappers.sonic.sonic_cli import SonicCli
from ngts.cli_wrappers.nvue.nvue_cli import NvueCli
from ngts.constants.constants import NvosCliTypes
from ngts.ngts_types import EnginesT, TopologyT
from ngts.tests_nvos.constants import MINUTE
from ngts.helpers import system_helpers

logger = logging.getLogger(__name__)


def list_files(engine: system_helpers.PrefixEngine, path: str, pattern: str = '') -> list[str]:
    files = GeneralCliCommon(engine).ls(path, flags='-1').splitlines()
    return [os.path.join(path, file) for file in files if re.search(pattern, file)]


@pytest.mark.disable_loganalyzer
@allure.title('Extract Python Coverage')
@pytest.mark.timeout(17 * MINUTE, func_only=True)
def test_extract_python_coverage(topology_obj: TopologyT, dest: str, engines: EnginesT):
    """
    Extracts code coverage collected by the Coverage.py tool for Python scripts.
    Code coverage is extracted as .xml file, one for the main image and one for
    each Docker container.
    :param topology_obj: topology object fixture.
    :param dest: dest fixture, the directory in which to save the extracted coverage .xml files
    :raise AssertionError: in case of script failure.
    """
    engine, cli_obj, is_nvos = get_topology_info(topology_obj)

    if is_nvos:
        extract_python_coverage_for_nvos(dest if dest else NvosConsts.DEST_PATH, engines, cli_obj, topology_obj)
    else:
        extract_python_coverage_for_sonic(dest, engines, engine, cli_obj)


@pytest.mark.disable_loganalyzer
@allure.title('Extract GCOV Coverage')
def test_extract_gcov_coverage(topology_obj: TopologyT, dest: str, engines: EnginesT):
    """
    Extracts code coverage collected by GCOV for C/C++ binaries that were
    compiled with GCOV flags.
    This is currently relevant for the core components: swss, syncd
    :param topology_obj: topology object fixture.
    :param dest: dest fixture, the directory in which to save the extracted coverage .xml files
    :raise AssertionError: in case of script failure.
    """
    engine, cli_obj, is_nvos = get_topology_info(topology_obj)
    c_dest = f"{dest}/c_coverage/"

    if is_nvos:
        with allure.step('Check that sources exist on the switch'):
            cli_obj.general.ls(NvosConsts.NVOS_SOURCE_PATH, validate=True)
        with allure.step('Extract c coverage for NVOS'):
            extract_c_coverage_for_nvos(dest if dest else NvosConsts.DEST_PATH, engines, engine, cli_obj, topology_obj)
    else:
        with allure.step('Check that sources exist on the switch'):
            cli_obj.general.ls(SharedConsts.SONIC_SOURCES_PATH[0], validate=True)
        with allure.step('Extract c coverage for SONIC'):
            extract_c_coverage_for_sonic(c_dest, engines, engine, cli_obj)


def get_coverage_file_names(sudo_cli_general: GeneralCliCommon, containers: list[str]) -> tuple[str, str]:
    logger.info(f'Containers with GCOV coverage: {containers}')
    logger.info(f'GCOV dir: {SharedConsts.GCOV_DIR}')
    sudo_cli_general.mkdir(SharedConsts.GCOV_DIR, flags='-p')
    hostname = sudo_cli_general.hostname()
    gcov_filename_prefix = f'gcov-{hostname}'
    lcov_filename_prefix = f'lcov-{hostname}'

    return gcov_filename_prefix, lcov_filename_prefix


def extract_c_coverage_for_nvos(
    dest: str,
    engines: EnginesT,
    engine: ProxySshEngine,
    cli_obj: NvueCli,
    topology_obj: TopologyT,
) -> None:
    with allure.step("Create device object if needed"):
        if not TestToolkit.devices:
            devices = DeviceFactory.create_devices_object(topology_obj)
            TestToolkit.update_devices(devices)

    with allure.step("Create coverage report path"):
        c_dest = coverage_helpers.get_dest_path(engine, dest) / SharedConsts.C_DIR

    with allure.step(f'Run {NvosConsts.COVERAGE_SCRIPT_PATH} {NvosConsts.COVERAGE_SCRIPT_STAGE1}'):
        logger.info("Waiting till the system reboot completed")
        engines.dut.reload(reload_cmd_set=[f'{NvosConsts.COVERAGE_SCRIPT_PATH} {NvosConsts.COVERAGE_SCRIPT_STAGE1}'],
                           find_prompt_tries=60, find_prompt_delay=5, wait_after_ping=20, ssh_after_reload=False)

        with allure.step("Wait till system is functional"):
            DutUtilsTool.wait_for_nvos_to_become_functional(engine, throw_exception_on_unhealthy=False)
            engines.dut.run_cmd('sudo docker ps')

    with allure.step(f'Run {NvosConsts.COVERAGE_SCRIPT_PATH} {NvosConsts.COVERAGE_SCRIPT_STAGE2}'):
        engines.dut.run_cmd(f'sudo {NvosConsts.COVERAGE_SCRIPT_PATH} {NvosConsts.COVERAGE_SCRIPT_STAGE2}')
        time.sleep(2)

    with allure.step(f"Copy coverage files from {NvosConsts.COVERAGE_OUTPUT_ON_SWITCH} to {c_dest}"):
        logger.info(f'Coverage files: {NvosConsts.COVERAGE_OUTPUT_ON_SWITCH}')
        engine.run_cmd(f"ls {NvosConsts.COVERAGE_OUTPUT_ON_SWITCH}")

        logger.info(f'Destination directory: {c_dest}')
        c_dest.mkdir(parents=True, exist_ok=True)
        hostname: str = cli_obj.general.hostname()
        timestamp = int(time.time())
        coverage_filename_prefix = f'gcov-{hostname}-{timestamp}'

        coverage_files = list_files(engine, NvosConsts.COVERAGE_OUTPUT_ON_SWITCH, pattern='.')
        for remote_path in map(Path, coverage_files):
            remote_dir = '' if remote_path.parent.as_posix() == '.' else remote_path.parent.as_posix()
            destination_file = c_dest / f'{coverage_filename_prefix}-{remote_path.name}'
            destination_file.unlink(missing_ok=True)
            engine.copy_file(
                source_file=remote_path.name,
                dest_file=str(destination_file),
                file_system=remote_dir,
                direction='get',
            )

        logger.info(f"Coverage files under {c_dest}:")
        logger.info(list(map(str, c_dest.iterdir())))


def extract_c_coverage_for_sonic(dest: str, engines: EnginesT, engine: system_helpers.PrefixEngine, cli_obj: SonicCli) -> None:
    with allure.step('Restart system services to get coverage for running services'):
        engines.dut.reload('sudo systemctl restart sonic.target')
        system_helpers.wait_for_all_jobs_done(engine)

    with allure.step("Get sudo cli object"):
        sudo_cli_general = get_sudo_cli_obj(engine)

    with allure.step("Get coverage file names"):
        gcov_filename_prefix, lcov_filename_prefix = get_coverage_file_names(sudo_cli_general,
                                                                             SonicConsts.GCOV_CONTAINERS_SONIC)

    with allure.step(f'Collect GCOV coverage from docker containers: {SonicConsts.GCOV_CONTAINERS_SONIC}'):
        for container in SonicConsts.GCOV_CONTAINERS_SONIC:
            collect_gcov_for_container_sonic(engine, cli_obj, container, gcov_filename_prefix)

    with allure.step("install gcov"):
        install_gcov(sudo_cli_general)

    with allure.step(""):
        timestamp = int(time.time())
        gcov_report_file = os.path.join(SharedConsts.GCOV_DIR, f'{gcov_filename_prefix}-{timestamp}.xml')
        with allure.step('Combine GCOV JSON reports into a single report for SonarQube'):
            create_and_copy_xml_coverage_file(engine, sudo_cli_general, gcov_report_file, dest, gcov_filename_prefix)
        with allure.step("Delete JSON and LCOV files"):
            sudo_cli_general.rm(gcov_report_file, flags='-f')
            sudo_cli_general.rm(SharedConsts.GCOV_DIR + "/*.json", flags='-f')


def get_sudo_cli_obj(engine: system_helpers.PrefixEngine):
    sudo_engine = system_helpers.PrefixEngine(engine, 'sudo')
    return GeneralCliCommon(sudo_engine)


def get_topology_info(topology_obj: TopologyT):
    with allure.step("Get info from topology"):
        engine = topology_obj.players['dut']['engine']
        cli_obj = topology_obj.players['dut']['cli']
        is_nvos = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Topology Conn.'][
            'CLI_TYPE'] in NvosCliTypes.NvueCliTypes
        logger.info("Is NVOS: " + str(is_nvos))

        return engine, cli_obj, is_nvos


def extract_python_coverage_for_nvos(dest: str, engines: EnginesT, cli_obj: NvueCli, topology_obj: TopologyT) -> None:
    with allure.step("Create device object if needed"):
        if not TestToolkit.devices:
            devices = DeviceFactory.create_devices_object(topology_obj)
            TestToolkit.update_devices(devices)

    with allure.step("Create coverage report path"):
        dest = coverage_helpers.get_dest_path(engines.dut, dest) / SharedConsts.PYTHON_DIR

    with allure.step('Get coverage file path'):
        coverage_file = get_python_coverage_file(cli_obj)

    with allure.step('Restart system services to get coverage for running services'):
        for service in HOST_SERVICES_FOR_COVERAGE:
            try:
                engines.dut.run_cmd(f'sudo systemctl restart {service}.service')
            except Exception as ex:
                logger.warning(f'Failed to restart {service}.service: {ex}')

    with allure.step("Pre step - start dockers"):
        nvos_pre_step(engines.dut)

    with allure.step("Collect python coverage"):
        collect_python_coverage(cli_obj, engines.dut, dest, coverage_file)

    with allure.step("Delete 'raw' files from host"):
        engines.dut.run_cmd('rm -f /var/lib/python/coverage/raw.*')


def extract_python_coverage_for_sonic(dest: str, engines: EnginesT, engine: system_helpers.PrefixEngine, cli_obj: SonicCli) -> None:
    with allure.step('Get coverage file path'):
        coverage_file = get_python_coverage_file(cli_obj)

    with allure.step('Restart system services to get coverage for running services'):
        engines.dut.reload('sudo systemctl restart sonic.target')
        system_helpers.wait_for_all_jobs_done(engine)

    with allure.step("Collect python coverage"):
        collect_python_coverage(cli_obj, engine, dest, coverage_file)


def get_python_coverage_file(cli_obj: NvueCli | SonicCli) -> str:
    coverage_file = cli_obj.general.echo(f'${{{SharedConsts.ENV_COVERAGE_FILE}}}')
    if not coverage_file:
        raise Exception('The system is not configured to collect code coverage.\n'
                        f'The environment variable {SharedConsts.ENV_COVERAGE_FILE} is not defined.')
    logger.info(f'Coverage file path: {coverage_file}')
    return coverage_file


def collect_python_coverage(cli_obj: NvueCli | SonicCli, engine: ProxySshEngine, dest: Path, coverage_file: str) -> None:
    coverage_dir = os.path.dirname(coverage_file)
    hostname = cli_obj.general.hostname()
    timestamp = int(time.time())
    coverage_xml_filename_prefix = f'coverage-{hostname}'

    with allure.step('Create coverage xml report for the host'):
        try:
            host_coverage_xml_file = os.path.join(coverage_dir, f'{coverage_xml_filename_prefix}-{timestamp}.xml')
            create_coverage_xml(cli_obj.general, coverage_file, host_coverage_xml_file)
        except Exception as ex:
            logger.info("Coverage collection for host has failed: " + str(ex))

    with allure.step("Get a list of running containers"):
        containers = cli_obj.general.get_running_containers_names()
        logger.info(f'Running Docker containers: {containers}')

    for container in containers:
        try:
            with allure.step(f'Create coverage xml report for {container} container'):
                docker_exec_engine = system_helpers.PrefixEngine(engine, f'docker exec {container}')
                container_coverage_xml_file = os.path.join(coverage_dir,
                                                           f'{coverage_xml_filename_prefix}-{timestamp}-{container}.xml')
                create_coverage_xml(GeneralCliCommon(docker_exec_engine), coverage_file, container_coverage_xml_file)
                coverage_xml_files = list_files(docker_exec_engine, coverage_dir,
                                                pattern=coverage_xml_filename_prefix)
                logger.info(f'Coverage xml files in {container} container: {coverage_xml_files}')
                for file in coverage_xml_files:
                    cli_obj.general.copy_from_docker(container, file, file)
                    cli_obj.general.remove_from_docker(container, file)
        except Exception as ex:
            logger.info(f"Coverage collection for {container} has failed: " + str(ex))

    with allure.step('Copy coverage xml reports from the system to destination directory'):
        coverage_xml_files = list_files(engine, coverage_dir, pattern=coverage_xml_filename_prefix)
        logger.info(f'Coverage xml files on the system: {coverage_xml_files}')
        logger.info(f'Destination directory: {dest}')
        dest.mkdir(parents=True, exist_ok=True)
        for file in map(Path, coverage_xml_files):
            engine.copy_file(
                source_file=file.name,
                dest_file=str(dest / file.name),
                file_system=str(file.parent),
                direction='get',
            )
            cli_obj.general.rm(str(file), flags='-f')


def create_and_copy_lcov_files(
    engine: system_helpers.PrefixEngine,
    sudo_cli_general: GeneralCliCommon,
    c_dest: str,
    lcov_filename_prefix: str,
) -> None:
    combined_coverage_file = '/sonic/combined_coverage.info'
    lcov_files = list_files(engine, SharedConsts.GCOV_DIR, pattern=lcov_filename_prefix)
    lcov_file_to_combine = []

    for lcov_file in lcov_files:
        if int(engine.run_cmd(f"stat -c %s {lcov_file}")) > 0:
            lcov_file_to_combine.append(lcov_file)

    if len(lcov_file_to_combine) > 1:
        lcovr_flags = ' '.join(f'--add-tracefile {lcov_file}' for lcov_file in lcov_file_to_combine)
        lcovr_flags += f' --output-file {combined_coverage_file}'
        sudo_cli_general.lcovr(flags=lcovr_flags)
    else:
        combined_coverage_file = lcov_file_to_combine[0]

    timestamp = int(time.time())

    for scr_file in NvosConsts.NVOS_SOURCE_FILES:
        info_file_string = scr_file.replace("/", "-")
        lcov_file_name = f'{info_file_string}-{timestamp}.info'
        lcov_file = f'{SharedConsts.GCOV_DIR}/{lcov_file_name}'
        sudo_cli_general.lcovr(flags=f'-extract {combined_coverage_file} "*/{scr_file}.cpp" '
                               f'--output-file {lcov_file}')

        engine.copy_file(source_file=lcov_file,
                         dest_file=os.path.join(c_dest, lcov_file_name),
                         file_system=os.path.dirname(lcov_file),
                         direction='get')
        sudo_cli_general.rm(lcov_file, flags='-f')


def create_and_copy_xml_coverage_file(
    engine: system_helpers.PrefixEngine,
    sudo_cli_general: GeneralCliCommon,
    gcov_report_file: str,
    c_dest: str,
    gcov_filename_prefix: str,
) -> None:
    gcov_json_files = list_files(engine, SharedConsts.GCOV_DIR, pattern=gcov_filename_prefix)
    logger.info(f'GCOV JSON files on the system: {gcov_json_files}')

    logger.info("Combine all jsons")
    gcovr_flags = ' '.join(f'-a {gcov_json_file}' for gcov_json_file in gcov_json_files)
    gcovr_flags += ' -o /sonic/combined.json --json-pretty'
    sudo_cli_general.gcovr(flags=gcovr_flags)

    logger.info("Convert json to xml")
    gcovr_flags = ' -a /sonic/combined.json'
    gcovr_flags += f' --sonarqube -r {SharedConsts.GCOV_DIR} -o {gcov_report_file}'
    sudo_cli_general.gcovr(flags=gcovr_flags)

    logger.info(f'Destination directory: {c_dest}')
    os.makedirs(c_dest, exist_ok=True)
    gcov_report_filename = os.path.basename(gcov_report_file)
    engine.copy_file(source_file=gcov_report_filename,
                     dest_file=os.path.join(c_dest, gcov_report_filename),
                     file_system=os.path.dirname(gcov_report_file),
                     direction='get')


def install_gcov(cli_obj: NvueCli | SonicCli) -> None:
    with allure.step('Install required packages'):
        cli_obj.apt_update()
        cli_obj.apt_install('lcov', flags='-y')
        cli_obj.pip3_install('gcovr')


def create_coverage_xml(cli_general: GeneralCliCommon, coverage_file: str, coverage_xml_file: str) -> None:
    """
    Checks if coverage files exist, and if so combines them into an xml report.
    :param cli_general: GeneralCliCommon object
    :param coverage_file: the base name of coverage files.
        For example, with coverage_file='/var/lib/python/coverage/raw', coverage
        files created throughout system operation are located under
        /var/lib/python/coverage/, with names in the format 'raw.<hostname>.<pid>.<rand>'.
        Those coverage files are then combined into a single file (that is coverage_file),
        from which the xml report is generated.
    :param coverage_xml_file: the name of the xml report file to generate.
    """
    coverage_dir = os.path.dirname(coverage_file)
    coverage_basename = os.path.basename(coverage_file)
    coverage_files = list_files(cli_general.engine, coverage_dir, pattern=rf'{coverage_basename}\.')
    logger.info(f'Coverage files: {coverage_files}')
    if not coverage_files:
        logger.info('Coverage files not found, skipping...')
    else:
        cli_general.coverage_combine()
        cli_general.coverage_xml(coverage_xml_file)


def collect_gcov_for_container_sonic(
    engine: system_helpers.PrefixEngine,
    cli_obj: SonicCli,
    container: str,
    gcov_filename_prefix: str
) -> None:
    """
    Collect GCOV coverage from a container running on the system, and creates a
    JSON report from it. This JSON report may later be combined with other JSON
    reports to a get full report.
    :param engine: an engine to the system
    :param cli_obj: dut cli object
    :param container: the container to work on
    :param gcov_filename_prefix: the prefix to give to the result filename
    """
    with allure.step("Create docker cli object"):
        docker_cli_obj = create_docker_cli_obj(engine, container)

    with allure.step(f"Install gcov on container {container}"):
        install_gcov(docker_cli_obj)

    with allure.step(f'Create GCOV JSON report for {container} container'):
        container_gcov_json_file = create_gcov_report_for_container(docker_cli_obj, gcov_filename_prefix, container,
                                                                    SharedConsts.SONIC_SOURCES_PATH)
        cli_obj.general.copy_from_docker(container, container_gcov_json_file, container_gcov_json_file)
        docker_cli_obj.rm(container_gcov_json_file, flags='-f')


def collect_gcov_for_container_nvos(
    engine: system_helpers.PrefixEngine,
    cli_obj: NvueCli,
    container: str,
    gcov_filename_prefix: str,
) -> None:
    """
    Collect GCOV coverage from a container running on the system, and creates a
    JSON report from it. This JSON report may later be combined with other JSON
    reports to a get full report.
    :param engine: an engine to the system
    :param cli_obj: dut cli object
    :param container: the container to work on
    """

    with allure.step("Create docker cli object"):
        docker_cli_obj = create_docker_cli_obj(engine, container)

    with allure.step(f"Generate cpp coverage for {container} container"):
        engine.run_cmd(f'docker exec {container} nvos_collect_coverage.sh')

    with allure.step(""):
        cli_obj.general.copy_from_docker(container,
                                         NvosConsts.GCOV_JSON_FILE_PATTERN.format(gcov_filename_prefix=gcov_filename_prefix,
                                                                                  container=container),
                                         NvosConsts.JSON_PATH_DOCKER)
        docker_cli_obj.rm(NvosConsts.JSON_PATH_DOCKER, flags='-f')


def create_docker_cli_obj(engine: system_helpers.PrefixEngine, container: str) -> GeneralCliCommon:
    docker_exec_engine = system_helpers.PrefixEngine(engine, f'docker exec {container}')
    return GeneralCliCommon(docker_exec_engine)


def create_gcov_report_for_container(docker_cli_obj: GeneralCliCommon, gcov_filename_prefix: str, container: str, source_path: str) -> str:
    container_gcov_json_file = os.path.join(SharedConsts.GCOV_DIR, f'{gcov_filename_prefix}-{container}.json')
    docker_cli_obj.tar(flags=f'xzf {source_path} -C {SharedConsts.GCOV_DIR}')

    flags = f'--json-pretty -r {SharedConsts.GCOV_DIR} -o {container_gcov_json_file}'
    additional_flags = ' --gcov-ignore-errors=no_working_dir_found --exclude-unreachable-branches --exclude-throw-branches --decisions '

    for path in NvosConsts.NVOS_EXCLUDE_PATHS:
        additional_flags += f' --exclude-directories {path}'

    docker_cli_obj.gcovr(paths=SharedConsts.GCOV_DIR, flags=flags, additional_flags=additional_flags)
    return container_gcov_json_file


def create_lcov_report_for_container(docker_cli_obj: GeneralCliCommon, lcov_filename_prefix: str, container: str) -> str:
    container_lcov_file = os.path.join(SharedConsts.GCOV_DIR, f'{lcov_filename_prefix}-{container}.info')

    docker_cli_obj.lcovr(flags=f'--gcov-tool gcov --capture --directory {SharedConsts.GCOV_DIR} '
                         f'--output-file {container_lcov_file}')
    return container_lcov_file


def nvos_pre_step(engine: system_helpers.PrefixEngine):
    try:
        engine.run_cmd(f'sudo chmod 777 {NvosConst.COVERAGE_PATH}/raw.*')
        with allure.step("Start SNMP"):
            HostMethods.start_snmp_server(engine=engine, state=NvosConst.ENABLED, readonly_community='qwerty12',
                                          listening_address='all')
    except BaseException as ex:
        logger.warning(f"NVOS pre step failed: {ex}")
