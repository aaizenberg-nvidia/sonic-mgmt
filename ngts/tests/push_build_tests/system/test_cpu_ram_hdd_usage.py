import re
import time
import pytest
import logging

from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from pathlib import Path

from ngts.test_commons.skynet.system.test_cpu_ram_hdd_usage import do_cpu_usage_test, do_ram_usage_test, do_hdd_usage_test, \
    do_ssd_endurance_test  # noqa

ONE_DAY_IN_SEC = 86400
ADDITIONAL_DELAY = 300
SLEEP_TIME = ONE_DAY_IN_SEC + ADDITIONAL_DELAY
BYTES_TO_MB_FACTOR = 1 / (1024 * 1024)
FILE_NAME_IND = 0
FILE_SIZE_IND = 1
SDK_DUMPS_PATH = '/var/log/mellanox/sdk-dumps'
partitions_and_expected_usage = [{'partition': '/', 'max_usage': 9000}, {'partition': '/var/log/', 'max_usage': 800}]

logger = logging.getLogger()


@pytest.fixture()
def skip_test_ram_usage_on_asan(topology_obj):
    """
    Fixture that skips test execution in case setup is running ASAN image
    """
    if topology_obj.players['dut']['sanitizer']:
        pytest.skip("Skipping execution of test on ASAN image because image "
                    "consumes more RAM then usually expected on dut")


def is_parent_folder(parent_path, child_path):
    """
    The function returns whether parent_path is indeed a parent path of child_path.
    """
    parent = Path(parent_path)
    child = Path(child_path)
    return parent in child.parents


class TestCpuRamHddUsage:

    @pytest.fixture(autouse=True)
    def setup(self, engines, cli_objects):
        self.dut_engine = engines.dut
        self.cli_objects = cli_objects

    def get_sai_fdw_dump_size(self):
        """
        The function adds to the hdd threshold used in test_hdd_usage the size of the sdk_dumps, to make sure they
        do not fail the test. Aside from calculating the size of the sdk_dump folder,
        The function also creates a dict of {file_name: file_size} mainly used for logging output, so it's easier to
        track the size of the sdk_dumps.
        """
        # The following command parses pairs of {file, fileSize} from the ls command.
        # We take the output of ls, skip the first line (total line of ls) with tail and then use awk to get the pairs.
        # The ninth and fifth fields of the ls command are the file name and its size respectfully.
        fetch_mem_usage_command = f"ls -l {SDK_DUMPS_PATH} | tail -n +2 | awk '{{print $9, $5}}'"
        dut_ls_output = self.dut_engine.run_cmd(fetch_mem_usage_command)
        # Parse the output into a dictionary
        file_sizes = {}
        total_size = 0
        for line in dut_ls_output.splitlines():
            pair = line.split()
            file_name = pair[FILE_NAME_IND]
            size = pair[FILE_SIZE_IND]
            file_sizes[file_name] = int(size) * BYTES_TO_MB_FACTOR
            total_size += int(size)
        total_size_in_mb = total_size * BYTES_TO_MB_FACTOR
        logger.info(f"File sizes in MB in sai dumps folder is {file_sizes}\nTotal size in MB is {total_size_in_mb}\n")
        return total_size_in_mb

    @pytest.mark.parametrize('partition_usage', partitions_and_expected_usage)
    def test_hdd_usage(self, partition_usage, platform_params):
        """
        This tests checks HDD usage in specific partition
        Test doing "df {partition}" and then check usage and compare with expected usage from test parameters
        :param partition_usage: dictionary with partition name and expected usage: {'partition': '/', 'max_usage': 6000}
        """
        if partition_usage['partition'] == '/var/log/':
            if is_redmine_issue_active([3454585])[0]:
                partition_usage['max_usage'] = 1000
            platform_hwsku = platform_params.hwsku
            if re.search('SN5', platform_hwsku):
                partition_usage['max_usage'] = 1500

        partition_dir = partition_usage['partition']
        if is_parent_folder(partition_dir, SDK_DUMPS_PATH):
            partition_usage['max_usage'] += self.get_sai_fdw_dump_size()

        # Update max_usage threshold if there are more than one available image
        image_list = self.cli_objects.dut.general.get_sonic_image_list()
        lines = [line.strip() for line in image_list.split('\n')]
        available_index = lines.index('Available:')
        available_images = [item for item in lines[available_index + 1:] if item]
        if len(available_images) > 1:
            partition_usage['max_usage'] = 13000

        if partition_dir == '/' and re.search('SN6600_LD|SN6810_LD', platform_params.hwsku):
            partition_usage['max_usage'] += 4000

        logger.info(f"\nChecking hdd_usage of {partition_dir} with max_usage of {partition_usage['max_usage']}\n")
        do_hdd_usage_test(self.dut_engine, partition_usage)

    @pytest.mark.build
    @pytest.mark.push_gate
    @pytest.mark.physical_coverage
    def test_cpu_usage(self, request, expected_cpu_usage_dict):
        """
        This tests checks CPU usage - total and per process
        Test doing command "top" - then parse output and check total cpu usage
        Also from "top" output test case find CPU usage for specific process
        If total CPU usage or by process CPU usage is bigger than expected - raise exception
        :param request: pytest build-in
        :param expected_cpu_usage_dict: expected_cpu_usage_dict fixture
        """
        do_cpu_usage_test(request.node.originalname, self.dut_engine, expected_cpu_usage_dict)

    @pytest.mark.build
    @pytest.mark.push_gate
    @pytest.mark.physical_coverage
    def test_ram_usage(self, request, expected_ram_usage_dict, skip_test_ram_usage_on_asan, platform_params):
        """
        This tests checks RAM usage - total and per process
        Test doing command "top" - then parse output and check from it PIDs for running processes
        Then test doing command "free" and parse total RAM usage
        After it test check RAM usage by process using command:
        sudo cat /proc/{PID}/smaps | grep Pss | awk '{Total+=$2} END {print Total/1024}
        If total RAM usage or by process RAM usage is bigger than expected - raise exception
        :param request: pytest build-in
        :param expected_ram_usage_dict: expected_ram_usage_dict fixture
        """
        if is_redmine_issue_active([5152247])[0]:
            expected_ram_usage_dict['telemetry'] += 10
        if is_redmine_issue_active([5226570])[0] and re.search('SN5640', platform_params.hwsku):
            expected_ram_usage_dict['lldp_syncd'] = 25
        do_ram_usage_test(request.node.originalname, self.dut_engine, expected_ram_usage_dict)

    @pytest.mark.release_check
    def test_ssd_endurance(self, release_mode, min_gap):
        """
        ssd_test
        """
        try:
            nos_name = 'sonic'
            do_ssd_endurance_test(self.dut_engine, nos_name, min_gap, release_mode)
        except RuntimeWarning as err:
            if release_mode:
                logger.info("First iteration of the test ended successfully, sleeping for {} days and rerunning".format(
                    min_gap))
                self.dut_engine.disconnect()
                time.sleep(min_gap * SLEEP_TIME)
                do_ssd_endurance_test(self.dut_engine, nos_name, min_gap)
            else:
                raise RuntimeWarning from err
