import time
import pytest
import logging
import os
from tests.common.plugins.allure_wrapper import allure_step_wrapper as allure
from ngts.helpers.vxlan_helper import get_tech_support_tar_file, validate_dest_files_exist_in_tarball
from ngts.cli_util.cli_parsers import generic_sonic_output_parser
from ngts.tests.nightly.show_techsupport.constants import HealthEventConst
from ngts.tests.nightly.show_techsupport.conftest import trigger_sdk_health_event

logger = logging.getLogger()


# -------------------------------- Fixture --------------------------------
@pytest.fixture(scope='session', autouse=True)
def disable_auto_techsupport(duthost, cli_objects):
    """
    Fixture to disable auto techsupport.
    """
    with allure.step('Disable auto techsupport'):
        duthost.run_cmd('sudo config auto-techsupport global state disabled')
        cli_objects.dut.general.save_configuration()
    yield

    with allure.step('Enable auto techsupport'):
        duthost.run_cmd('sudo config auto-techsupport global state enabled')
        cli_objects.dut.general.save_configuration()


@pytest.fixture
def copy_bulk_event_trigger_script_to_dut(duthost):
    """
    Fixture to copy event trigger script ./generate-events.py to dut syncd.
    """
    with allure.step('Copy generate-events.py to DUT'):
        cp_generate_events_script_to_dut(duthost)


@pytest.fixture(scope='function', autouse=True)
def restore_health_event_config(duthost, cli_objects):
    """
    Fixture to restore health event configuration to default after test.
    """
    yield

    with allure.step('Restore health event configuration to default'):
        for severity in [HealthEventConst.SEVERITY_FATAL, HealthEventConst.SEVERITY_WARNING]:
            cli_objects.dut.general.config_health_event(duthost,
                                                        severity=severity,
                                                        category_list=HealthEventConst.CATEGORY_NONE,
                                                        max_events=HealthEventConst.MAX_EVENTS_NUM_DEFAULT)
        get_health_event_config(duthost)


@pytest.fixture
def cleanup_health_event(topology_obj, cli_objects):
    """
    Fixture to cleanup health event after test.
    """

    yield

    with allure.step('Reload switch to cleanup exist health events'):
        cli_objects.dut.general.reboot_flow(topology_obj=topology_obj)


# -------------------------------- Test cases --------------------------------
@pytest.mark.disable_loganalyzer
@pytest.mark.parametrize(
    "severity,fw_event",
    [
        pytest.param(HealthEventConst.SEVERITY_FATAL, "FW_FATAL_EVENT"),
        pytest.param(HealthEventConst.SEVERITY_WARNING, "FW_WARN_EVENT"),
    ]
)
def test_health_event_collect(duthost, cleanup_health_event, cli_objects, severity, fw_event):
    with allure.step('STEP1: Config health event without suppression'):
        cli_objects.dut.general.config_health_event(duthost,
                                                    severity=severity,
                                                    category_list=HealthEventConst.CATEGORY_NONE)
        get_health_event_config(duthost)

    with allure.step(f'STEP2: Trigger one {severity} health event at dut'):
        trigger_sdk_health_event(duthost, fw_event_id=HealthEventConst.FW_EVENTS_DICT[fw_event])

    with allure.step(f'STEP3: Verify receive one {severity} health event'):
        received_health_event = get_health_event_received(duthost)
        logger.info(f"The received health event is: {received_health_event}")
        assert len(received_health_event) == 1
        assert received_health_event[0]['Severity'] == severity


@pytest.mark.disable_loganalyzer
def test_health_event_suppression(duthost, cleanup_health_event, cli_objects):
    with allure.step('STEP1: Config health event suppression'):
        cli_objects.dut.general.config_health_event(duthost,
                                                    severity=HealthEventConst.DEFAULT_SEVERITY,
                                                    category_list=HealthEventConst.CATEGORY_FIRMWARE)
        get_health_event_config(duthost)

    with allure.step('STEP2: Trigger one health event at dut'):
        trigger_sdk_health_event(duthost, fw_event_id=HealthEventConst.DEFAULT_FW_HEALTH_EVENT_ID)

    with allure.step('STEP3: Verify no health event is received'):
        received_health_event = get_health_event_received(duthost)
        logger.info(f"The received health event is: {received_health_event}")
        assert len(received_health_event) == 0


@pytest.mark.disable_loganalyzer
def test_health_event_maximum_number(duthost, copy_bulk_event_trigger_script_to_dut, cleanup_health_event, cli_objects):
    with allure.step(f'STEP1: Generate {HealthEventConst.SCALE_EVENTS_NUM} health events'):
        duthost.run_cmd(f'python {HealthEventConst.GENERATE_EVENTS_SCRIPT_DEST_FOLDER}/{HealthEventConst.GENERATE_EVENTS_SCRIPT} {HealthEventConst.SCALE_EVENTS_NUM}')

    with allure.step(f'STEP2: Verify receive {HealthEventConst.SCALE_EVENTS_NUM} health events'):
        received_health_event = get_health_event_received(duthost)
        logger.info(f"The received health event is: {received_health_event}")
        assert len(received_health_event) == HealthEventConst.SCALE_EVENTS_NUM

    with allure.step(f'STEP3: Set maximum event number {HealthEventConst.MAX_EVENTS_NUM_ELIMINATE_THRESHOLD}'):
        cli_objects.dut.general.config_health_event(duthost,
                                                    severity=HealthEventConst.DEFAULT_SEVERITY,
                                                    max_events=HealthEventConst.MAX_EVENTS_NUM_ELIMINATE_THRESHOLD)
        get_health_event_config(duthost)

    with allure.step('STEP4: Trigger system check the max number of event immediately'):
        duthost.run_cmd(f'docker exec -it swss redis-cli --eval {HealthEventConst.ELIMINATE_EVENTS_SCRIPT}')

    with allure.step(f'STEP5: Verify only {HealthEventConst.MAX_EVENTS_NUM_ELIMINATE_THRESHOLD} health events exist in database'):
        received_health_event = get_health_event_received(duthost)
        logger.info(f"The received health event is: {received_health_event}")
        assert len(received_health_event) == HealthEventConst.MAX_EVENTS_NUM_ELIMINATE_THRESHOLD


def test_health_event_command(duthost):
    for param_type, test_values in HealthEventConst.PARAMETERS.items():
        for test_type, values in test_values.items():
            step_description = f"STEP: {param_type} {test_type} test"
            with allure.step(step_description):
                for value in values:
                    command = f"{HealthEventConst.BASE_COMMAND} --{param_type} {value}"
                    logger.info(f'Test parameter: {param_type}. Test command is: {command}')
                    result = duthost.run_cmd(command)

                    if test_type == 'positive':
                        assert result == "", f"Expected empty result for positive test, got: {result}"
                    else:
                        assert "Error: Invalid" in result, f"Expected 'Error: Invalid' in result, got: {result}"


@pytest.mark.disable_loganalyzer
def test_health_event_interop_with_techsupport(duthost, engines, cleanup_health_event, cli_objects):
    with allure.step('STEP1: Config health event without suppression'):
        cli_objects.dut.general.config_health_event(duthost,
                                                    severity=HealthEventConst.DEFAULT_SEVERITY,
                                                    category_list=HealthEventConst.CATEGORY_NONE)
        get_health_event_config(duthost)

    with allure.step('STEP2: Trigger one health event at dut'):
        trigger_sdk_health_event(duthost, fw_event_id=HealthEventConst.DEFAULT_FW_HEALTH_EVENT_ID)

    with allure.step('STEP3: Generate show techsupport'):
        tar_file = get_tech_support_tar_file(engines)

    with allure.step('STEP4: Check health event file exist in dump file'):
        validate_dest_files_exist_in_tarball(tar_file, HealthEventConst.HEALTH_EVENT_DUMP_FILE)


# -------------------------------- Function --------------------------------
def cp_generate_events_script_to_dut(duthost):
    """
    Copy generate health event script to dut
    """
    destination_path = os.path.join(HealthEventConst.GENERATE_EVENTS_SCRIPT_DEST_FOLDER, HealthEventConst.GENERATE_EVENTS_SCRIPT)
    source_path = os.path.join(HealthEventConst.FILES_DIR, HealthEventConst.GENERATE_EVENTS_SCRIPT)
    duthost.copy_file(source_file=source_path, dest_file=destination_path, file_system=HealthEventConst.GENERATE_EVENTS_SCRIPT_DEST_FOLDER, direction='put')


def get_health_event_config(dut_engine):
    """
    Get current health event config.

    Example:
    admin@r-panther-03:~$ show asic-sdk-health-event suppress-configuration
    Severity    Suppressed category-list            Max events
    ----------  --------------------------------  ------------
    fatal       software,asic_hw,cpu_hw,firmware            10
    """
    output_lines = dut_engine.run_cmd('show asic-sdk-health-event suppress-configuration')

    return output_lines.split('\n')


def get_health_event_received(dut_engine, max_retries=6, retry_delay=10):
    """
    Get received health event.

    Example:
    admin@r-panther-03:~$ show asic-sdk-health-event received
    Date                 Severity    Category    Description
    -------------------  ----------  ----------  --------------------------------------
    2024-02-23 11:28:23  fatal       firmware    {
                                                     "switch_id": "0x0000000100000021",
                                                     "severity": "0",
                                                     "timestamp": {
                                                         "tv_sec": "1708687703",
                                                         "tv_nsec": "599520740"
                                                     },
                                                     "category": "1",
                                                     "data": {
                                                         "data_type": "0"
                                                     },
                                                     "additional_data": ""
                                                 }
    """
    for attempt in range(max_retries):
        output_lines = dut_engine.run_cmd('show asic-sdk-health-event received')
        events = generic_sonic_output_parser(output_lines)
        # non-empty meas one or more health events have been received
        if events:
            return events
        elif attempt < max_retries - 1:
            time.sleep(retry_delay)
    logger.warning("Failed to receive health event after {} retries.".format(max_retries))
    return []
