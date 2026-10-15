"""
@summary:
    This script contains the tests for CPU RAM and HDD usage.
    The better way is to use the do_<cpu/ram>_usage_test function from another project with all relevant parameters.
    Exist also option to run the test separately, but without inserting results to database,
        example of cmd to run the test:
            /ngts_venv/bin/pytest --setup_name=sonic_leopard_r-leopard-56 -c ngts/test_commons/skynet/pytest.ini
                --expected_cpu_usage_dict='{"total":"20","sx_sdk":"10"}'
                --expected_ram_usage_dict='{"total":"4250","sx_sdk":"1000","syncd":"300"}'
                --partitions_and_expected_usage='[{'partition': '/', 'max_usage': 9000},
                                                  {'partition': '/var/log/', 'max_usage': 500}]'
                --log-level=INFO --showlocals  ngts/test_commons/skynet/system/test_cpu_ram_hdd_usage.py
"""
# pylint MESSAGES CONTROL settings for this file:
# pylint: disable=R1702
# pylint: disable=W0201

import logging
import time
import os
import re
import json
import pytest
from infra.tools.sql.constants import SkynetGeneralConstants
from devts.infra.tools.custom_skipif.GitHub import GitHubApi
logger = logging.getLogger(__name__)

ALLOWED_DEVIATION = 0.05
SSD_DIR = "/home/admin/ssd_check"
WRITTEN_MB_PATH = SSD_DIR + "/last_written_value.txt"
SSD_TH_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ssd_threshold.json")
TB_IN_MB = 1048576
HOURS_IN_10_YEAR = 87600
github_api = GitHubApi(os.getenv("GITHUB_USER"), os.getenv("GITHUB_API_TOKEN"))


class TestDevtsCpuRamHddUsage:
    """
    test class TestDevtsCpuRamHddUsage
    """

    @pytest.fixture(autouse=True)
    def setup(self, topology_obj):
        """
        setup fixture
        :param topology_obj: topology object
        """
        self.topology_obj = topology_obj
        self.dut_engine = topology_obj.players['dut']['engine']

    def test_cpu_usage(self, request, expected_cpu_usage_dict):
        """
        This tests checks CPU usage - total and per process
        Test doing command "top" - then parse output and check total cpu usage
        Also from "top" output test case find CPU usage for specific process
        If total CPU usage or by process CPU usage is bigger than expected - raise exception

        The test can be executed in ways:
         1. separate pytest test with provided dictionary of expected_cpu_usage_dict in command line
         2. help function "do_cpu_usage_test" with provided dictionary of expected_cpu_usage_dict as one of parameters

        :param request: pytest build-in
        :param expected_cpu_usage_dict: expected_cpu_usage_dict fixture
        """
        do_cpu_usage_test(request.node.originalname, self.dut_engine, expected_cpu_usage_dict)

    def test_ram_usage(self, request, expected_ram_usage_dict):
        """
        This tests checks RAM usage - total and per process
        Test doing command "top" - then parse output and check from it PIDs for running processes
        Then test doing command "free" and parse total RAM usage
        After it test check RAM usage by process using command:
        sudo cat /proc/{PID}/smaps | grep Pss | grep -v Pss_Dirty | awk '{Total+=$2} END {print Total/1024}'
        If total RAM usage or by process RAM usage is bigger than expected - raise exception

        The test can be executed in ways:
         1. separate pytest test with provided dictionary of expected_ram_usage_dict in command line
         2. help function "do_ram_usage_test" with provided dictionary of expected_ram_usage_dict as one of parameters

        :param request: pytest build-in
        :param expected_ram_usage_dict: expected_ram_usage_dict fixture
        """
        do_ram_usage_test(request.node.originalname, self.dut_engine, expected_ram_usage_dict)

    def test_hdd_usage(self, partitions_and_expected_usage):
        """
        This tests checks HDD usage in specific partition
        Test doing "df {partition}" and then check usage and compare with expected usage from test parameters
        :param partitions_and_expected_usage: fixture, which returns list of dictionaries with
                            partition name and expected usage: [{'partition': '/', 'max_usage': 6000}]
        """
        for partition_usage in partitions_and_expected_usage:
            do_hdd_usage_test(self.dut_engine, partition_usage)


def do_hdd_usage_test(dut_engine, partition_usage, results_file_name=None):
    """
    Contains all logic for hdd usage check
    :param dut_engine: engine of dut
    :param partition_usage: dictionary with partition name and expected usage: {'partition': '/', 'max_usage': 6000}
    :param results_file_name: optional string, if name is given, a file will be created under /tmp with the test results
    """
    _, used_size, _ = get_partition_disk_usage(dut_engine, partition_usage['partition'])
    max_usage = partition_usage['max_usage']
    try:
        assert used_size < max_usage, f'Used disk size: {used_size} is more than expected: {max_usage}'

    except Exception as err:
        raise AssertionError from err

    finally:
        if results_file_name:
            ssd_usage_dict = {"path_" + partition_usage['partition']: used_size}
            store_results_into_file(filename=os.path.join('/tmp/', results_file_name),
                                    results_dict=ssd_usage_dict)


def do_ram_usage_test(results_file_name, dut_engine, expected_ram_usage_dict):
    """
    Contains all logic for ram usage check
    :param results_file_name: name of file for results
    :param dut_engine: engine of dut
    :param expected_ram_usage_dict: expected ram usage
    """
    total_ram_size_mb = 0
    used_ram_size_mb = 0
    ram_usage_per_process = {}
    overall_assertions_list = []
    (total_ram_expected, no_simx_proc_list, ram_expected_usage_dict_filtered,
     proc_to_container_mapping_dict, container_to_proc_dict) = read_expected_usage(dut_engine, expected_ram_usage_dict)

    try:
        proc_to_pids_mapping_dict = {}
        for container in container_to_proc_dict:
            cur_proc_to_pids_mapping_dict = get_processes_pids(dut_engine, container_to_proc_dict, container)
            proc_to_pids_mapping_dict = merge_by_nested_keys(proc_to_pids_mapping_dict, cur_proc_to_pids_mapping_dict)

        find_missing_processes(ram_expected_usage_dict_filtered, proc_to_pids_mapping_dict, no_simx_proc_list)

        for attempt in range(3):
            logger.info('Checking RAM utilization, attempt number: %s', attempt)
            assertions_list_total_usage, total_ram_size_mb, used_ram_size_mb = (
                validate_total_ram_usage(dut_engine, total_ram_expected))
            assertions_list_processes_usage, ram_usage_per_process = validate_processes_ram_usage(dut_engine,
                                                                                                  ram_expected_usage_dict_filtered, proc_to_container_mapping_dict, proc_to_pids_mapping_dict)
            overall_assertions_list = assertions_list_total_usage + assertions_list_processes_usage
            if not overall_assertions_list:
                break

        assert not overall_assertions_list, \
            f'RAM usage: {overall_assertions_list} \n is more than expected: \n {ram_expected_usage_dict_filtered}'

    except Exception as err:
        raise AssertionError(f"RAM usage in these processes is more than expected : {overall_assertions_list}") from err

    finally:
        ram_usage_per_process.update({'total_ram_size': total_ram_size_mb, 'total_used_ram_size': used_ram_size_mb})
        store_results_into_file(filename=os.path.join('/tmp/', results_file_name),
                                results_dict=ram_usage_per_process)


def do_cpu_usage_test(results_file_name, dut_engine, expected_cpu_usage_dict):
    """
    Contains all logic for cpu usage check
    :param results_file_name: name of file for results
    :param dut_engine: engine of dut
    :param expected_cpu_usage_dict: expected cpu usage
    """
    timeout_between_attempts = 5
    cpu_usage_per_process = {}
    (total_cpu_usage_expected, no_simx_proc_list, cpu_expected_usage_dict_filtered,
     proc_to_container_mapping_dict, container_to_proc_mapping_dict) = read_expected_usage(dut_engine, expected_cpu_usage_dict)

    try:
        assertions_list = []
        failed_attempts = []

        for attempt in range(10):
            logger.info('Checking CPU utilization, attempt number: %s', attempt)
            assertions_list = []
            cpu_usage_per_process_dict = {}
            total_cpu_usage_dict = {}
            for container in container_to_proc_mapping_dict:
                total_cpu_usage_dict[container], cur_cpu_usage_per_process_dict = (
                    get_cpu_usage(dut_engine, container_to_proc_mapping_dict, container))
                cpu_usage_per_process_dict = (
                    merge_by_nested_keys(cpu_usage_per_process_dict, cur_cpu_usage_per_process_dict))

            assertions_list, cpu_usage_per_process = validate_cpu_usage(total_cpu_usage_dict['host'], total_cpu_usage_expected,
                                                                        cpu_expected_usage_dict_filtered, proc_to_container_mapping_dict, cpu_usage_per_process_dict)

            if assertions_list:
                failed_attempts.append(assertions_list)
            passed_attempts = attempt + 1 - len(failed_attempts)
            if passed_attempts > len(failed_attempts):
                break
            time.sleep(timeout_between_attempts)

        find_missing_processes(cpu_expected_usage_dict_filtered, cpu_usage_per_process_dict, no_simx_proc_list)
        assert passed_attempts > len(failed_attempts), \
            f'CPU usage: {failed_attempts} \n is more than expected: \n {expected_cpu_usage_dict}'

    except Exception as err:
        raise AssertionError(f"CPU usage in these processes is more than expected : {failed_attempts}") from err
    finally:
        cpu_usage_per_process.update({'total_cpu_usage': total_cpu_usage_dict['host']})
        store_results_into_file(filename=os.path.join('/tmp/', results_file_name),
                                results_dict=cpu_usage_per_process)


def read_expected_usage(dut_engine, expected_usage_dict):
    """
    Read expected usage dict for both RAM and CPU and return total usage,no_simx_proc_list, expected usage dictionary,
    dictionary that map process to his containers and dictionary that map container to his processes.

    Example for return values:
    no_simx_proc_list = ['sensord']
    expected_usage_dict_filtered = {'syncd':15,'sx_sdk':20, 'rsyslogd':10}
    proc_to_container_mapping_dict = {'syncd':['host'],'sx_sdk':['host'], 'rsyslogd':['eventd','pmon']}
    container_to_proc_mapping_dict = {'host':['syncd','sx_sdk'], 'eventd':['rsyslogd'], 'pmon':['rsyslogd']}
    """
    total_usage = expected_usage_dict['total']
    del expected_usage_dict['total']

    no_simx_proc_list = []
    proc_to_container_mapping_dict = {}
    expected_usage_dict_filtered = {}

    docker_ps_output = dut_engine.run_cmd('docker ps --format "{{.Names}}"')
    what_just_happened_missing = 'what-just-happened' not in docker_ps_output

    for process in expected_usage_dict:
        if what_just_happened_missing and process == 'wjhd':
            pass
        elif isinstance(expected_usage_dict[process], dict):
            expected_usage_dict_filtered[process] = expected_usage_dict[process]['expected_value']
            if expected_usage_dict[process].get('no_simx_flag'):
                no_simx_proc_list.append(process)
            proc_to_container_mapping_dict[process] = expected_usage_dict[process].get('containers_list', ['host'])
            if 'what-just-happened' in proc_to_container_mapping_dict[process] and what_just_happened_missing:
                logger.warning("'what-just-happened' container not found in docker ps output")
                proc_to_container_mapping_dict[process].remove('what-just-happened')
        else:
            expected_usage_dict_filtered[process] = expected_usage_dict[process]
            proc_to_container_mapping_dict[process] = ['host']

    # reverse the dict in order to iter over containers when running the 'top' cmd later
    container_to_proc_mapping_dict = {}
    for key, v_list in proc_to_container_mapping_dict.items():
        for val in v_list:
            if val not in container_to_proc_mapping_dict:
                container_to_proc_mapping_dict[val] = []
            container_to_proc_mapping_dict[val].append(key)

    return (total_usage, no_simx_proc_list, expected_usage_dict_filtered, proc_to_container_mapping_dict,
            container_to_proc_mapping_dict)


def get_processes_pids(dut_engine, container_to_proc_mapping_dict, container_name):
    """
    This method returns pids list for every process

    Example for return values:
    proc_to_pids_mapping_dict = {'sx_sdk': {'host':[12345,6789], {'gnmi':[1357,2468]} ...}
    """
    _, top_cmd_processes = get_top_cmd_parsed(dut_engine, container_name)
    processes_data_start_index = 1
    pid_index = 0
    proc_to_pids_mapping_dict = {}
    processes_names_sorted = sorted(container_to_proc_mapping_dict[container_name], key=len, reverse=True)

    for process in top_cmd_processes[processes_data_start_index:]:
        splited_line = process.split()
        pid = splited_line[pid_index]
        proc = find_process_name(splited_line, processes_names_sorted)
        # filter running processes from top_cmd_output that don't have a relevant process in the expected usage dict
        if not proc:
            continue
        if proc not in proc_to_pids_mapping_dict:
            proc_to_pids_mapping_dict[proc] = proc_to_pids_mapping_dict.get(proc, {container_name: [pid]})
        if pid not in proc_to_pids_mapping_dict[proc][container_name]:  # to remove duplicate PIDs
            proc_to_pids_mapping_dict[proc][container_name].append(pid)

    return proc_to_pids_mapping_dict


def get_cpu_usage(dut_engine, container_to_proc_mapping_dict, container_name):
    """
    This method returns cpu usage for every process and the total_cpu_usage

    Example for return values:
    cpu_usage_dict = {'sx_sdk': {'host':10}, {'gnmi':8} ...}
    total_cpu_usage = 200
    """
    processes_data_start_index = 1
    cpu_usage_index = 8

    headers, top_cmd_processes = get_top_cmd_parsed(dut_engine, container_name)
    total_cpu_usage = calculate_total_cpu_usage(headers)
    num_of_cores = get_num_of_cpu_cores(dut_engine)
    cpu_usage_dict = {}

    processes_names_sorted = sorted(container_to_proc_mapping_dict[container_name], key=len, reverse=True)
    for process in top_cmd_processes[processes_data_start_index:]:
        splited_line = process.split()
        proc = find_process_name(splited_line, processes_names_sorted)
        # filter running processes from top_cmd_output that don't have a relevant process in the expected usage dict
        if not proc:
            continue
        cpu_us_by_process = float(splited_line[cpu_usage_index]) / num_of_cores  # top show it for 1 core
        if proc not in cpu_usage_dict:
            cpu_usage_dict[proc] = cpu_usage_dict.get(proc, {container_name: 0})
        cpu_usage_dict[proc][container_name] += cpu_us_by_process

    return total_cpu_usage, cpu_usage_dict


def get_top_cmd_parsed(dut_engine, container_name):
    """
    This method will run 'top' command and return it after parsing
    """

    top_cmd_with_flags = 'top -b -d 1 -n 2 -c'
    if container_name != 'host':
        top_cmd = f'docker exec -it {container_name} {top_cmd_with_flags}'
    else:
        top_cmd = f'sudo {top_cmd_with_flags}'
    top_cmd_output = dut_engine.run_cmd(top_cmd)
    headers, processes = parse_top_cmd_output(top_cmd_output)

    return headers, processes


def parse_top_cmd_output(top_cmd_output):
    """
    This method will parse top cmd output and split the headers and processes into separate lines
    """
    headers_index = -2
    processes_index = -1

    top_cmd_output_splited_by_empty_line = top_cmd_output.split('\n\n')
    headers = top_cmd_output_splited_by_empty_line[headers_index].splitlines()
    processes = top_cmd_output_splited_by_empty_line[processes_index].splitlines()
    return headers, processes


def calculate_total_cpu_usage(headers):
    """
    This method will calculate and returns the total CPU usage by summing the user space and kernel space usages.
    """
    user_usage_group = 1
    kernel_usage_group = 2
    cpu_usage_line_index = 2

    cpu_usage_user_and_kernel = re.search(r'%Cpu\(s\):\s+(\d+.\d)\s+us,\s+(\d+.\d)', headers[cpu_usage_line_index])
    user_space_usage = cpu_usage_user_and_kernel.group(user_usage_group)
    kernel_space_usage = cpu_usage_user_and_kernel.group(kernel_usage_group)

    total_cpu_usage = float(user_space_usage) + float(kernel_space_usage)
    return total_cpu_usage


def get_num_of_cpu_cores(dut_engine):
    """
    get num of cpu cores
    :param dut_engine: engine of dut
    :return num of cores
    """
    num_of_cores = len(dut_engine.run_cmd('sudo cat /proc/cpuinfo | grep processor').splitlines())
    return num_of_cores


def find_process_name(splited_line, possible_processes_list):
    """
    This method will find the exact process name using the process line and processes list for current container
    """

    min_proc_name_index = 11
    proc = ''

    full_proc_name = ' '.join(splited_line[min_proc_name_index:])
    for optional_proc in possible_processes_list:
        if optional_proc in full_proc_name:
            proc = optional_proc
            break

    # to avoid confusion between 'telemetry' process to 'nv-telemetry' docker
    if proc == 'telemetry' and 'nv-telemetry' in full_proc_name:
        proc = ''

    return proc


def merge_by_nested_keys(dict1, dict2):
    """
    Merges two dictionaries, combining nested dictionaries by their keys.
    Example:
        dict1 = {'key1': {'sub_key1': value1}, 'key2': {'sub_key2': value2}}
        dict2 = {'key1': {'sub_key3': value3}}
        Result: {'key1': {'sub_key1': value1, 'sub_key3': value3}, 'key2': {'sub_key2': value2}}

    :param dict1: First dictionary to merge.
    :param dict2: Second dictionary to merge.
    :return: Merged dictionary with combined nested dictionaries.
    """
    merged_dict = {}

    for key in set(dict1) | set(dict2):
        if key in dict1 and key in dict2:
            merged_dict[key] = {**dict1[key], **dict2[key]}
        elif key in dict1:
            merged_dict[key] = dict1[key]
        else:
            merged_dict[key] = dict2[key]

    return merged_dict


def find_missing_processes(expected_usage_dict_filtered, usage_per_process_dict, no_simx_proc_list):
    """
    This method will find all the processes from the expected usage dictionary that are not found in the
    'top' command output and raise AssertionError if these processes exist
    """
    missing_proc_list = []

    for proc in expected_usage_dict_filtered:
        if proc not in usage_per_process_dict and proc not in no_simx_proc_list:
            missing_proc_list.append(proc)

    if len(missing_proc_list) > 0:
        raise AssertionError(f"The following processes are missing : {missing_proc_list}")


def validate_total_ram_usage(dut_engine, total_ram_usage):
    """
    This method will validate the expected cpu usage compare to the actual cpu usage
    """
    assertions_list = []
    free_output = dut_engine.run_cmd('sudo free')
    total_ram_size_mb = int(free_output.splitlines()[1].split()[1]) / 1024
    used_ram_size_mb = int(free_output.splitlines()[1].split()[2]) / 1024
    logger.info('DUT total RAM size: %s Mb', total_ram_size_mb)
    logger.info('DUT use: %s Mb of RAM', used_ram_size_mb)

    # RAM usage is integer and in Mb
    if used_ram_size_mb > int(total_ram_usage) * (1 + ALLOWED_DEVIATION):
        assertions_list.append({'total': used_ram_size_mb})

    return assertions_list, total_ram_size_mb, used_ram_size_mb


def validate_processes_ram_usage(dut_engine, ram_expected_usage_dict, proc_to_container_dict, proc_to_pids_dict):
    """
    This method will validate the expected ram usage of each process compare to actual ram usage
    """
    assertions_list = []
    ram_usage_per_process = {}

    host_pid_usage_dict = calculate_pid_usage_dict(dut_engine, ram_expected_usage_dict, proc_to_pids_dict)

    for process, process_expected in ram_expected_usage_dict.items():
        for container in proc_to_container_dict[process]:
            try:
                if container == 'host':
                    process_and_container = process
                else:
                    process_and_container = f'{process}_{container}'
                used_ram_mb = calculate_ram_usage_per_process(
                    dut_engine, proc_to_pids_dict[process][container], container, host_pid_usage_dict)
                if container == 'host':
                    logger.info('Process: %s used %s Mb of RAM',
                                process, used_ram_mb)
                else:
                    logger.info('Process: %s in container: %s used %s Mb of RAM',
                                process, container, used_ram_mb)
                ram_usage_per_process[process_and_container] = used_ram_mb
                if used_ram_mb > int(process_expected) * (1 + ALLOWED_DEVIATION):
                    if process == 'rsyslogd' and github_api.is_github_issue_active('https://github.com/sonic-net/sonic-buildimage/issues/19763'):
                        logger.warning('High RAM usage for rsyslog detected. This might be related to known issue : '
                                       'https://github.com/sonic-net/sonic-buildimage/issues/19763 ')
                        continue
                    assertions_list.append({process_and_container: used_ram_mb})
            except KeyError:
                ram_usage_per_process[process_and_container] = 0
                logger.error('Can not find RAM usage for process : %s '
                             'in container : %s - process is not running', process, container)

    return assertions_list, ram_usage_per_process


def calculate_pid_usage_dict(dut_engine, ram_expected_usage_dict, proc_to_pids_dict):
    """
    This method will calculate the RAM usage for every PID in 'host'
    """
    pid_index = 2
    usage_index = 5
    host_pids_list = []

    for process in ram_expected_usage_dict:
        if 'host' in proc_to_pids_dict[process]:
            for pid in proc_to_pids_dict[process]['host']:
                host_pids_list.append(pid)

    pids_list_string = '|'.join(host_pids_list)
    xargs_cmd = ('echo "' + pids_list_string + '|" | xargs -d "|" -I {} sudo sh -c '
                 "'cat /proc/{}/smaps | grep Pss | grep -v Pss_Dirty | awk \"{Total+=\\$2} END "
                 "{print \\\"PID = \\\", {}, \\\"Usage = \\\", Total/1024}\"'")
    xargs_cmd_output = dut_engine.run_cmd(xargs_cmd)
    xargs_cmd_output_lines = xargs_cmd_output.split('\n')
    pid_usage_dict = {}

    for line in xargs_cmd_output_lines:
        if line.startswith('PID'):
            splited_line = line.split()
            pid = splited_line[pid_index]
            usage = float(splited_line[usage_index])
            pid_usage_dict[pid] = usage

    return pid_usage_dict


def calculate_ram_usage_per_process(dut_engine, process_info, container, pid_usage_dict):
    """
    This method will calculate ram usage of given process
    """
    used_ram_mb = 0
    for proc_id in process_info:
        if container == 'host':
            used_ram_mb += pid_usage_dict[proc_id]
        else:
            cat_smaps_cmd = f"sudo cat /proc/{proc_id}/smaps"
            get_ram_usage_cmd = \
                cat_smaps_cmd + "| grep Pss | grep -v Pss_Dirty | awk '{Total+=$2} END {print Total/1024}'"
            if container != 'host':
                get_ram_usage_cmd = get_ram_usage_cmd.replace('sudo ', '')
                get_ram_usage_cmd = f'docker exec -it {container} {get_ram_usage_cmd}'
            used_ram_mb += float(dut_engine.run_cmd(get_ram_usage_cmd))
    return used_ram_mb


def validate_cpu_usage(total_cpu_usage, total_cpu_usage_expected, cpu_expected_dict, proc_to_container_mapping_dict, cpu_usage_per_process_dict):
    """
    This method will validate the expected cpu usage compare to the actual cpu usage
    """
    assertions_list = []
    cpu_usage_per_process = {}

    logger.info('DUT use : %s %% of CPU', total_cpu_usage)
    # CPU usage is integer and in %
    if total_cpu_usage > int(total_cpu_usage_expected) * (1 + ALLOWED_DEVIATION):
        assertions_list.append({'total': total_cpu_usage})

    for process in cpu_expected_dict:
        for container in proc_to_container_mapping_dict[process]:
            try:
                proc_cpu_usage = cpu_usage_per_process_dict[process][container]
                if container == 'host':
                    process_and_container = process
                else:
                    process_and_container = f'{process}_{container}'
                process_and_container = f'{process}_{container}'
                cpu_usage_per_process[process_and_container] = proc_cpu_usage
                logger.info('Process: %s in container: %s use %s %% of CPU', process, container, proc_cpu_usage)
                if proc_cpu_usage > int(cpu_expected_dict[process]) * (1 + ALLOWED_DEVIATION):
                    assertions_list.append({process_and_container: proc_cpu_usage})
            except KeyError:
                cpu_usage_per_process[process_and_container] = 0
                logger.error('Can not find CPU usage for process: %s - process is not running', process)

    return assertions_list, cpu_usage_per_process


def store_results_into_file(filename, results_dict):
    """
    Store results into file
    :param filename: filename
    :param results_dict: results_dict
    """
    with open(filename, 'w', encoding='utf-8') as test_report_file_obj:
        json.dump(results_dict, test_report_file_obj)


def get_partition_disk_usage(dut_engine, partition):
    """
    Get partition disk usage
    :param dut_engine: engine of dut
    :param partition: partition
    :return total, used, available size
    """
    data_line_index = 1
    column_total_size_index = 1
    column_used_index = 2
    column_available_index = 3

    df_output = dut_engine.run_cmd(f'sudo df {partition}')
    total_size = df_output.splitlines()[data_line_index].split()[column_total_size_index]
    used_size = df_output.splitlines()[data_line_index].split()[column_used_index]
    available_size = df_output.splitlines()[data_line_index].split()[column_available_index]
    # convert all to Mb(by default it Kb)
    total_size_mb = int(total_size) / 1024
    used_size_mb = int(used_size) / 1024
    available_size_mb = int(available_size) / 1024

    return total_size_mb, used_size_mb, available_size_mb


class TestDevtsSSDEndurace:
    """
    test class TestDevtsSSDEndurace
    """

    @pytest.fixture(autouse=True)
    def setup(self, topology_obj):
        """
        setup fixture
        :param topology_obj: topology object
        """
        self.topology_obj = topology_obj
        self.dut_engine = topology_obj.players['dut']['engine']

    def test_ssd_endurance(self, nos_name, min_gap, release_mode=False):
        """
        This test checks SSD endurance.
        Test running command "iostat" - checks the amount of Mb written on the SSD.
        The test will compare between the current value, to the value stored in a file on the DUT last time the test ran.
        If the calculated value is exceeding the threshold of the SSD, the test will fail

        The test can be executed in 2 modes:
         1. Store mode - The test will only collect and store the current value of Mb written to the DUT.
         2. Compare mode - The test will compare the last result stored on the DUT to the current result, and check if
         it exceeds the threshold

        :param nos_name: the name of the operating system on which the tests runs on
        :param min_gap: The minimum amount of time in days allowed between 2 tests
        """
        do_ssd_endurance_test(self.dut_engine, nos_name, min_gap, release_mode)


def do_ssd_endurance_test(dut_engine, nos_name, min_gap, release_mode=False):
    """
    Contains all logic for ssd endurance check
    :param dut_engine: engine of dut
    :param nos_name: the name of the operating system on which the tests runs on
    :param min_gap: The minimum amount of time in days allowed between 2 tests
    :param release_mode: Determines if the test is running in release mode or not.
    """
    try:
        skip_writing = False
        ssd_device_name = dut_engine.run_cmd("lsblk -o NAME,TYPE -p | grep disk").strip().split()[0]
        ssd_device_filter = "'Model Number'" if "nvme" in ssd_device_name else "'Device Model'"
        ssd_device_id = dut_engine.run_cmd(f"sudo smartctl -a {ssd_device_name} | grep {ssd_device_filter}",
                                           validate=True).strip().split()[-1]
        ssd_threshold_tb = get_writing_threshold_for_device(ssd_device_id)
        ssd_threshold_mb = ssd_threshold_tb * TB_IN_MB
        set_log_level_default(dut_engine, nos_name)
        logger.info("Calculate the current 'written mb' value on the DUT")
        if "nvme" in ssd_device_name:
            current_written_value = int(dut_engine.run_cmd("iostat -m | grep nvme | awk '{{print $7}}'", validate=True))
        else:
            current_written_value = int(dut_engine.run_cmd("iostat -m | grep sda | awk '{{print $7}}'", validate=True))
        last_written_value = get_last_written_value(dut_engine)
        logger.debug("Values for debug: last: %d, curr: %d", last_written_value, current_written_value)
        sec_from_last_modification = get_sec_from_last_modification(dut_engine)
        logger.info("Checking if the current value is valid:"
                    " a reboot wasn't performed or %d days has passed from last writing", min_gap)
        validate_last_written_value(dut_engine, min_gap, sec_from_last_modification)
        logger.debug("Values for debug: sec from last modify: %d", sec_from_last_modification)
        logger.info("Test starts: calculating the SSD writing tempo")
        mb_written_per_hour = \
            calculate_ssd_writing(last_written_value, current_written_value, sec_from_last_modification)
        estimate_mb_write_for_10_years = mb_written_per_hour * HOURS_IN_10_YEAR
        logger.debug("Values for debug: est_mb_for_10_years: %d,"
                     " mb_written per hour: %d, hours in 10 years: %d, ssd thresh: %d",
                     estimate_mb_write_for_10_years, mb_written_per_hour, HOURS_IN_10_YEAR, ssd_threshold_mb)
        if estimate_mb_write_for_10_years > ssd_threshold_mb:
            raise AssertionError('FAILED: The writing tempo to ssd has exceeded the allowed threshold!')
        logger.info("Test passed!")

    except ValueError as err:
        skip_writing = True
        raise ValueError from err

    except AssertionError as err:
        raise AssertionError from err

    except RuntimeWarning as skip_phrase:
        str_skip = str(skip_phrase)
        if re.match("Not enough time", str_skip):
            skip_writing = True
        if not release_mode:
            pytest.skip(str_skip)
        #  If release mode is active, this error is expected in the first iteration,
        #  We will raise and catch it on the local project environment, instead of skipping.
        raise RuntimeWarning from skip_phrase

    finally:
        if not skip_writing:
            logger.info("Updating the current value to %s", current_written_value)
            update_current_value(dut_engine, current_written_value)
        else:
            logger.info("Update of current value is not needed, test finished")


def set_log_level_default(dut_engine, nos_name):
    """
    set the log level of the DUT to default to avoid extensive logs writing
    """
    nos_name = nos_name.lower()
    if nos_name == SkynetGeneralConstants.SONIC.lower():
        dut_engine.run_cmd('swssloglevel -l NOTICE -a')

    elif nos_name == SkynetGeneralConstants.CL.lower():
        pass

    elif nos_name == SkynetGeneralConstants.NVOS.lower():
        pass

    else:
        raise ValueError(f"{nos_name} is not a valid Operation system")


def update_current_value(dut_engine, current_val):
    """
    Update the file on the DUT with the current Mb written value
    """
    dut_engine.run_cmd(f'mkdir -p {SSD_DIR}', validate=True)
    dut_engine.run_cmd(f"echo {current_val} > {WRITTEN_MB_PATH}", validate=True)


def get_sec_from_last_modification(dut_engine):
    """
    get from DUT how many seconds passed from last modification of the written mb file
    """
    return int(dut_engine.run_cmd(
        f"stat -c \"%Y\" {WRITTEN_MB_PATH} | xargs -I{{}} date +%s --date=\"now - {{}} seconds\"",
        validate=True))


def get_last_written_value(dut_engine):
    """
    This method will return the "last written Mb" value from the switch, if exists.
    :param dut_engine: engines fixture
    """
    logger.info("Checking if a file with the latest value exists on DUT")
    dut_engine.run_cmd(f'ls {WRITTEN_MB_PATH}')
    rc = int(dut_engine.run_cmd('echo $?', validate=True))
    if rc:
        raise RuntimeWarning("The current value file does not exist, Writing the current value and exiting")
    written_mb_value = dut_engine.run_cmd(f'cat {WRITTEN_MB_PATH}', validate=True)
    logger.info("File exists, the last value written to the file is %s mb", written_mb_value)
    return int(written_mb_value)


def validate_last_written_value(dut_engine, min_gap, sec_from_last_modification):
    """
    This method will check if the switch was rebooted since the "last written value" was written to file
    or the gap between performing the tests is too low.
    """
    min_gap_in_sec = min_gap * 24 * 3600
    sec_from_uptime = int(float(dut_engine.run_cmd("sudo cat /proc/uptime | awk '{print $1}'")))
    if sec_from_uptime < sec_from_last_modification:
        raise RuntimeWarning("System has rebooted since last time ssd sampled, value is not valid")
    if sec_from_last_modification < min_gap_in_sec:
        remaining_hours_for_next_test = int((min_gap_in_sec - sec_from_last_modification) / 3600)
        raise RuntimeWarning(f"Not enough time has passed to calculate the SSD endurance. Please try again in {remaining_hours_for_next_test} hours")


def calculate_ssd_writing(last_written_value, current_written_value, sec_from_last_modification):
    """
    Calculate SSD writing tempo per hour
    """
    total_written = current_written_value - last_written_value
    write_mb_per_hour = (total_written / sec_from_last_modification) * 3600
    return write_mb_per_hour


def get_writing_threshold_for_device(device_id):
    """
    Fetch the SSD TBW threshold from the json file
    """
    with open(SSD_TH_FILE_PATH, 'r', encoding='utf-8') as json_file:
        th_dict = json.load(json_file)
        for json_device_id, ssd_th_value in th_dict.items():
            pattern = fr'.*{json_device_id}$'
            if re.match(pattern, device_id):
                return ssd_th_value
    raise ValueError(f'The device {device_id} was not found in the active SSD list')
