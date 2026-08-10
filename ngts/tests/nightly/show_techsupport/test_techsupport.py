import allure
import os
import pytest
from retry.api import retry_call
import re
import logging
import tarfile
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.helpers.sonic_branch_helper import get_sonic_branch
from ngts.common.checkers import is_ver1_greater_or_equal_ver2
from ngts.tests.nightly.show_techsupport.constants import HealthEventConst
from ngts.tests.nightly.show_techsupport.conftest import trigger_sdk_health_event

logger = logging.getLogger(__name__)

SUCCESS_CODE = 0

SDK_DUMP_DIR = '/var/log/mellanox/sdk-dumps'
# these two numbers are set in sai.profile, not expected to be changed by user
MAX_SDK_DFW_DUMPS_BEFORE_CLEANUP = 5
MAX_SDK_DFW_WARN_DUMPS_BEFORE_CLEANUP = 2
SAI_DFW_DUMP_PREFIX = 'sai-dfw-'
SAI_DFW_WARN_DUMP_PREFIX = 'sai-dfw-warn-'
HEALTH_CHECK_INJECT_FILE_PATH = '/proc/mlx_sx/sx_core'

SAI_PROFILE_PATH = "/etc/mlnx/sai-common.profile"
SNIFFER_MODE_KEY = "SAI_KEY_SDK_SNIFFER_MODE"
POSSIBLE_API_SNIFFER_MODES = ['cyclic', 'linear']
API_SNIFFER_DUMPS_PATH = "/var/log/sdk_dbg"
PCAP_FILE_FORMAT = "sx_sdk*.pcap*"  # pattern that captures .pcap and .pcap.gz files with ls command
LIST_PCAP_CMD = f"ls -la {API_SNIFFER_DUMPS_PATH}/{PCAP_FILE_FORMAT}"
PARSE_FILE_NAME_CMD = "awk '{{print $NF}}'"
FIRST_PCAP_REGEX_PATTERN = r".*sx_sdk.*_0\.pcap"
GREP_PCAP_FILES_CMD = r"grep 'sx_sdk.*\.pcap.*'"
ZIPPED_FILE_POSTFIX = ".gz"


@pytest.mark.disable_loganalyzer
@pytest.mark.parametrize("fw_event", ["FW_FATAL_EVENT", "FW_WARN_EVENT"])
@allure.title('Tests that dump file contains all the expected dumps when fw stuck occurs')
def test_techsupport_fw_stuck_dump(topology_obj, engines, cli_objects, fw_event):
    duthost = engines.dut
    chip_type = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific']['chip_type']
    fatal_dumps, warn_dumps = check_sai_sdk_dumps(duthost)

    try:
        with allure.step(f'Trigger {fw_event} event'):
            trigger_sdk_health_event(duthost, HealthEventConst.FW_EVENTS_DICT[fw_event])

        with allure.step('Wait for dump file to be created'):
            retry_call(
                verify_sdkdump_created,
                fargs=[duthost, fatal_dumps + warn_dumps],
                tries=30,
                delay=10,
                logger=logger,
            )

        with allure.step('Validate that the dump file contain all of the SDK extended dump files'):
            check_all_dumps_file_exsits(topology_obj, duthost, chip_type)

    except Exception as err:
        raise err
    finally:
        with allure.step('Rebooting the system - necessary to restart the iRISCs'):
            cli_objects.dut.general.reboot_reload_flow(topology_obj=topology_obj)


@pytest.mark.parametrize('disable_rsyslog_ratelimit', ['syncd'], indirect=True)
@pytest.mark.parametrize("fw_event", ["FW_FATAL_EVENT", "FW_WARN_EVENT", "PLL_LOCK_EVENT"])
def test_techsupport_mellanox_sdk_dump(topology_obj, engines, cli_objects, loganalyzer, fw_event,
                                       disable_rsyslog_ratelimit):
    duthost = engines.dut
    logger.debug("Running show techsupport ... ")
    with allure.step('STEP1: Count number of SDK extended dumps at dut before test'):
        sdk_dumps_files_before = generate_tech_support_and_count_sdk_dumps(duthost)

    with allure.step('STEP2: Trigger SDK health event at dut'):
        trigger_sdk_health_event(duthost, HealthEventConst.FW_EVENTS_DICT[fw_event])
        for dut in loganalyzer:
            loganalyzer[dut].expect_regex.extend(["Health event happened"])
            loganalyzer[dut].ignore_regex = [r".*"]
    with allure.step('STEP3: Count number of SDK extended dumps at dut after event occurred'):
        sdk_dumps_files_after = generate_tech_support_and_count_sdk_dumps(duthost)

    with allure.step('Validate that the techsupport file contain the new generated dump'):
        new_dump_files = list(set(sdk_dumps_files_after) - set(sdk_dumps_files_before))
        logger.info(f"new_dump_files: {new_dump_files}")
        assert len(new_dump_files) == 1, 'Did not create dump file'

    with allure.step('Reload switch'):
        cli_objects.dut.general.reload_flow(topology_obj=topology_obj, reload_force=True)


@allure.title('Tests that health check event dump contains all the expected dumps when health check event occurs')
def test_techsupport_health_event_sdk_dump(topology_obj, loganalyzer, engines, cli_objects):
    duthost = engines.dut
    chip_type = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific']['chip_type']
    fatal_dumps, warn_dumps = check_sai_sdk_dumps(duthost)

    try:
        with allure.step('Verify Health-Check: Trigger SYSFS failure appears in syslog'):
            for dut in loganalyzer:
                loganalyzer[dut].expect_regex.extend(["Health-Check: Trigger SYSFS failure"])
                loganalyzer[dut].ignore_regex = [r".*"]

        with allure.step('Generate health check trigger event'):
            duthost.run_cmd(f'sudo echo health_check_trigger sysfs > {HEALTH_CHECK_INJECT_FILE_PATH}')

        with allure.step('Get health_check_running_counter after trigger event'):
            health_check_counter_after_event_triggered = int(get_health_check_running_counter(duthost))

        with allure.step('Wait for health check dump to be created'):
            retry_call(
                verify_sdkdump_created,
                fargs=[duthost, fatal_dumps + warn_dumps],
                tries=30,
                delay=10,
                logger=logger,
            )

        with allure.step('Get health_check_running_counter after dumps generated'):
            health_check_counter_after_dump_generated = int(get_health_check_running_counter(duthost))

        with allure.step('Get health_check_running_counter after dump generated'):
            assert health_check_counter_after_dump_generated == health_check_counter_after_event_triggered, \
                "Health check counter was not stopped"

        with allure.step('Validate that the health check dump contain all of the SDK extended dump files'):
            check_all_dumps_file_exsits(topology_obj, duthost, chip_type)

        with allure.step("Verify basic container is up before orchagent core dump generated"):
            cli_objects.dut.general.verify_dockers_are_up()

    except Exception as err:
        raise err

    finally:
        with allure.step('Reload switch'):
            cli_objects.dut.general.reload_flow(topology_obj=topology_obj, reload_force=True)


def is_api_sniffer_enabled(duthost):
    cmd = f"docker exec syncd cat {SAI_PROFILE_PATH} | grep {SNIFFER_MODE_KEY}"
    sniffer_mode = duthost.run_cmd(cmd)
    sniffer_mode_regex = rf"{SNIFFER_MODE_KEY}=([^ ]+)"
    match = re.search(sniffer_mode_regex, sniffer_mode)
    sniffer_mode_val_group = 1
    # return true if the match is not none (hence the mode is configured) and the mode is valid
    return match and match.group(sniffer_mode_val_group) in POSSIBLE_API_SNIFFER_MODES


@pytest.mark.disable_loganalyzer
@pytest.mark.parametrize("fw_event", ["FW_FATAL_EVENT", "FW_WARN_EVENT"])
def test_techsupport_dump_retention(topology_obj, engines, cli_objects, mock_dump_file_to_capacity_limit, fw_event):
    duthost = engines.dut
    try:
        with allure.step(f'Get dump files before {fw_event} health event'):
            fatal_dumps_before, warn_dumps_before = check_sai_sdk_dumps(duthost)

        with allure.step(f'Trigger {fw_event} health event'):
            trigger_sdk_health_event(duthost, HealthEventConst.FW_EVENTS_DICT[fw_event])

        with allure.step('Wait for sdk dump to be created'):
            retry_call(
                verify_sdkdump_created,
                fargs=[duthost, fatal_dumps_before + warn_dumps_before],
                tries=30,
                delay=10,
                logger=logger,
            )

        if fw_event == "FW_FATAL_EVENT":
            with allure.step('Validate that the only fatal dump files changed and the oldest fatal dump is replaced'):
                fatal_dumps_after, warn_dumps_after = check_sai_sdk_dumps(duthost)
                assert warn_dumps_before == warn_dumps_after, \
                    "Warn dump files are affected"
                assert fatal_dumps_before[0] not in fatal_dumps_after, \
                    "Oldest fatal dump file is not replaced"
        elif fw_event == "FW_WARN_EVENT":
            with allure.step('Validate that the only warn dump files changed and the newest warn dump is replaced'):
                fatal_dumps_after, warn_dumps_after = check_sai_sdk_dumps(duthost)
                assert fatal_dumps_after == fatal_dumps_before, \
                    "Fatal dump files are affected"
                assert warn_dumps_before[-1] not in warn_dumps_after, \
                    "Newest warn dump file is not replaced"
    except Exception as err:
        raise err
    finally:
        with allure.step('Reload switch'):
            cli_objects.dut.general.reload_flow(topology_obj=topology_obj, reload_force=True)


@allure.title('Tests that the api sniffer feature dumps are included in the techsupport command')
def test_techsupport_validate_api_sniffer_dumps(topology_obj, engines, cli_objects):
    duthost = engines.dut
    with allure.step("Check that the API Sniffer is enabled"):
        if not is_api_sniffer_enabled(duthost):
            pytest.skip("Skipping test as API Sniffer is not enabled")

    with allure.step("Fetch API Sniffer dumps and assert at least one exists"):
        # Fetch all .pcap files of the API SNIFFER
        pcap_file_paths = duthost.run_cmd(f"{LIST_PCAP_CMD} | {PARSE_FILE_NAME_CMD}").strip().split('\n')
        pcap_file_names = [os.path.basename(file_path) for file_path in pcap_file_paths]  # get base file names
        pattern = re.compile(FIRST_PCAP_REGEX_PATTERN)
        # Check if any filename matches the regex pattern
        assert any(pattern.match(filename) for filename in pcap_file_names), (
            "No .pcap file was found in sniffer api dumps, "
            "expected at least one file")
    try:
        with allure.step("Verify existing API Sniffer dumps are valid .pcap files"):
            for file_path in pcap_file_paths:
                if ZIPPED_FILE_POSTFIX in file_path:
                    tcpdump_cmd = f"zcat {file_path} | tcpdump -r - -c 1"
                else:
                    tcpdump_cmd = f"tcpdump -r {file_path} -c 1"
                duthost.run_cmd(tcpdump_cmd, validate=True)
    except Exception as err:
        raise AssertionError(f"Failed to validate .pcap files, error={err}")

    with allure.step("Show tech support and verify the expected .pcap files are in it"):
        dump_file = cli_objects.dut.general.generate_techsupport()
        fetch_pcap_from_techsupport_cmd = f"sudo tar -tf {dump_file} | {GREP_PCAP_FILES_CMD}"
        res = duthost.run_cmd(fetch_pcap_from_techsupport_cmd).strip().split('\n')
        # Add .gz to none-compressed files
        pcap_techsupport_expected_files = set(
            [f"{file}{ZIPPED_FILE_POSTFIX}" if ZIPPED_FILE_POSTFIX not in file else file for file in pcap_file_names])
        pcap_files_techsupport = set(
            [f"{os.path.basename(file_path)}" for file_path in res])  # Fetch base file names
        assert pcap_techsupport_expected_files <= pcap_files_techsupport, (f"expected techsupport .pcap files not found in techsupport.\n "
                                                                           f"expected.\n Expected: {pcap_techsupport_expected_files},"
                                                                           f"Actual: {pcap_files_techsupport}")


def check_sai_sdk_dumps(engine):
    """
    Return the sai-dfw/sai-dfw-warn dump names.
     - Fatal(sai-dfw): top 5 newest
     - Warn(sai-dfw-warn): 1 oldest + 1 newest
    """
    # Sort by file timestamp in ascending order: old -> new.
    output = engine.run_cmd(f"ls -1tr {SDK_DUMP_DIR}/{SAI_DFW_DUMP_PREFIX}* 2>&1", validate=False).strip()
    if not output or "No such file or directory" in output:
        return [], []

    file_names = [os.path.basename(file_path.strip()) for file_path in output.splitlines() if file_path.strip()]
    warn_dumps = [name for name in file_names if name.startswith(SAI_DFW_WARN_DUMP_PREFIX)]
    fatal_dumps = [name for name in file_names if name not in warn_dumps]
    return fatal_dumps, warn_dumps


def generate_tech_support_and_count_sdk_dumps(engine):
    sdk_dump_dir = 'sai_sdk_dump'
    sdk_file_pattern = 'sai-dfw-.*'

    output_lines = engine.run_cmd('show techsupport').split('\n')

    tar_file = output_lines[len(output_lines) - 1]
    tarball_file_name = str(tar_file.replace('/var/dump/', ''))
    tarball_dir_name = str(tarball_file_name.replace('.tar.gz', ''))

    sdk_dump_pattern = '{}/{}/{}'.format(tarball_dir_name, sdk_dump_dir, sdk_file_pattern)

    engine.copy_file(source_file=tar_file, dest_file=tarball_file_name, file_system='/tmp/', direction='get')

    t = tarfile.open(tarball_file_name, "r")

    filenames = t.getnames()
    r = re.compile(sdk_dump_pattern)

    after_list = list(filter(r.match, filenames))

    engine.run_cmd("sudo rm -rf {}".format(tar_file))
    return [os.path.basename(path) for path in after_list]


def verify_sdkdump_created(engine, before):
    after_fatal_dumps, after_warn_dumps = check_sai_sdk_dumps(engine)
    new_files = list(set(after_fatal_dumps + after_warn_dumps) - set(before))
    assert len(new_files) > 0, 'Did not create dump file'


def stop_irisics(chip_type, host):
    if chip_type == 'SPC':
        host.run_cmd('sudo mcra /dev/mst/mt52100_pci_cr0 0xa01e4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt52100_pci_cr0 0xa05e4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt52100_pci_cr0 0xa07e4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt52100_pci_cr0 0xa09e4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt52100_pci_cr0 0xa0be4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt52100_pci_cr0 0xa0de4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt52100_pci_cr0 0xa0fe4 0x10')
    elif chip_type == 'SPC2':
        host.run_cmd('sudo mcra /dev/mst/mt53100_pci_cr0 0xa01e4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53100_pci_cr0 0xa05e4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53100_pci_cr0 0xa07e4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53100_pci_cr0 0xa09e4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53100_pci_cr0 0xa0be4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53100_pci_cr0 0xa0de4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53100_pci_cr0 0xa0fe4 0x10')
    elif chip_type == 'SPC3':
        host.run_cmd('sudo mcra /dev/mst/mt53104_pci_cr0 0xa01c4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53104_pci_cr0 0xa05c4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53104_pci_cr0 0xa07c4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53104_pci_cr0 0xa09c4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53104_pci_cr0 0xa0bc4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53104_pci_cr0 0xa0dc4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53104_pci_cr0 0xa0fc4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53104_pci_cr0 0xa11c4 0x10')
        host.run_cmd('sudo mcra /dev/mst/mt53104_pci_cr0 0xa11c4 0x10')
    else:
        raise ValueError("Not supported chip type: {}".format(chip_type))


def check_all_dumps_file_exsits(topology_obj, engine, chip_type):
    # DumpMe dumps should contain the following dumps:
    # 3 CR space dumps
    # SDK dump + SDK thread backtrace dump
    # mlxtrace dump
    # FW core dump - only on FW event from level CRITICAL or ERROR
    sonic_branch = get_sonic_branch(topology_obj)
    # Some files name are changed after the SDK 2000, now 202211 include the SDK older than 2000
    branch_with_old_sdk = ['202211']
    latest_fw_dump = engine.run_cmd('ls -t {}/*.tar | head -1'.format(SDK_DUMP_DIR))
    output_fw_dump = engine.run_cmd('sudo tar -tf {}'.format(latest_fw_dump))

    # Check CR space dump:
    sdk_dump = "sdkdump" if sonic_branch in branch_with_old_sdk else "sdk_dump"
    assert len(re.findall(fr'{sdk_dump}_ext_.*cr_space.*.udmp', output_fw_dump)) == 3, 'Missing CR space dump'

    # Check SDK dump:
    assert 'sai_sdk_dump.txt' in output_fw_dump, 'Missing SDK dump'
    # Check mlxtrace dump:
    if not (is_redmine_issue_active([3587386])[0] and chip_type == "SPC4"):
        if sonic_branch in branch_with_old_sdk:
            assert '_pci_cr0_mlxtrace.trc' in output_fw_dump, 'Missing mlxtrace'
        else:
            assert re.search(r'sdk_dump_ext_.*fw_trace.txt', output_fw_dump) is not None, 'Missing FW trace'

    # Check SDK Thread Backtrace dump:
    sai_version = topology_obj.players['dut']['cli'].general.get_sai_version()
    base_sai_version_thread_backtrace = "2405.29.2.49"  # SDK Thread Backtrace dump will appear from this version onward
    logger.info(f'sai_version: {sai_version}, base sai_version:{base_sai_version_thread_backtrace}')
    if sai_version and is_ver1_greater_or_equal_ver2(sai_version, base_sai_version_thread_backtrace):
        assert re.search(r'sdk_dump_ext_.*sdk_threads_backtrace.txt', output_fw_dump), 'Missing SDK Thread Backtrace'
    else:
        logger.info(f"sai_version doesn't contain SDK Thread Backtrace dump")

    # Check FW core dump:
    # This should be uncommented when FW stuck event level would change to critical
    # assert 'ir_core_dump_' in output, 'Missing FW core dump'


def get_health_check_running_counter(engine):
    health_check_counter_file_path = "/sys/module/sx_core/health_check_running_counter"
    return engine.run_cmd(f"sudo cat {health_check_counter_file_path}")


@pytest.fixture(scope='function', autouse=False)
def mock_dump_file_to_capacity_limit(duthost):
    """
    Mock dump files to the capacity limit, 5 fatal dumps and 2 warn dumps.
    """
    try:
        with allure.step('Mock dump files to the capacity limit'):
            fatal_dumps, warn_dumps = check_sai_sdk_dumps(duthost)
            mock_fatal_dumps = MAX_SDK_DFW_DUMPS_BEFORE_CLEANUP - len(fatal_dumps)
            mock_warn_dumps = MAX_SDK_DFW_WARN_DUMPS_BEFORE_CLEANUP - len(warn_dumps)
            for i in range(mock_fatal_dumps):
                duthost.run_cmd(f'sudo touch {SDK_DUMP_DIR}/{SAI_DFW_DUMP_PREFIX}{1000000000 + i}.tar')
            for i in range(mock_warn_dumps):
                duthost.run_cmd(f'sudo touch {SDK_DUMP_DIR}/{SAI_DFW_WARN_DUMP_PREFIX}{2000000000 + i}.tar')
        yield
    except Exception as err:
        raise err
    finally:
        with allure.step('Remove mock dump files'):
            duthost.run_cmd(f'sudo rm -f {SDK_DUMP_DIR}/{SAI_DFW_DUMP_PREFIX}100000000*')
            duthost.run_cmd(f'sudo rm -f {SDK_DUMP_DIR}/{SAI_DFW_WARN_DUMP_PREFIX}200000000*')