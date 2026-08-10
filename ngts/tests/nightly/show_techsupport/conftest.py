import logging
import os
import pytest

from ngts.common.checkers import is_ver1_greater_or_equal_ver2
from tests.common.plugins.allure_wrapper import allure_step_wrapper as allure

logger = logging.getLogger()


MIN_SDK_VERSION_FOR_DEBUG_STATE_FLAGS = "4.8.4000"
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
FILES_DIR = os.path.join(BASE_DIR, 'files')


@pytest.fixture(scope='session')
def duthost(engines):
    return engines.dut


@pytest.fixture(scope='session', autouse=True)
def copy_sdk_event_trigger_script_to_dut_syncd(duthost):
    """
    Fixture to copy mellanox_sdk_trigger_event_script.py to dut syncd only once.
    """
    with allure.step('Copy mellanox_sdk_trigger_event_script.py to DUT syncd'):
        dst = os.path.join('/tmp', 'mellanox_sdk_trigger_event_script.py')
        duthost.copy_file(source_file=os.path.join(FILES_DIR, 'mellanox_sdk_trigger_event_script.py'),
                          dest_file='mellanox_sdk_trigger_event_script.py',
                          file_system='/tmp',
                          direction='put'
                          )
        duthost.run_cmd('docker cp {} {}'.format(dst, 'syncd:/'))


@pytest.fixture(scope='function')
def set_health_event_debug_state_flags(duthost, cli_objects):
    """
    Fixture to set health check debug state flags before test and restore after test.
    Only applies when SDK version is >= 4.8.4000.
    """
    sdk_version = cli_objects.dut.general.get_sdk_version()
    apply_debug_flags = is_ver1_greater_or_equal_ver2(
        sdk_version, MIN_SDK_VERSION_FOR_DEBUG_STATE_FLAGS
    )

    if apply_debug_flags:
        with allure.step('Enable health check debug state flags (Fatal-on-Warn)'):
            duthost.run_cmd(
                "sudo sh -c 'echo health_check set_debug_state_flags 1 1 > /proc/dbg_dump/dev_1/dbg_cmd'"
            )
            verify_debug_state_flags(duthost, enabled_flag='Fatal-on-Warn', disabled_flag='NONE')
    else:
        logger.info(
            "Skipping health check debug state flags setup - SDK version %s is below %s",
            sdk_version,
            MIN_SDK_VERSION_FOR_DEBUG_STATE_FLAGS,
        )

    yield

    if apply_debug_flags:
        with allure.step('Restore health check debug state flags'):
            duthost.run_cmd(
                "sudo sh -c 'echo health_check unset_debug_state_flags 1 1 > /proc/dbg_dump/dev_1/dbg_cmd'"
            )
            verify_debug_state_flags(duthost, enabled_flag='NONE', disabled_flag='Fatal-on-Warn')


def verify_debug_state_flags(dut_engine, enabled_flag, disabled_flag):
    output_lines = dut_engine.run_cmd('cat /proc/dbg_dump/dev_1/health_check_dump | grep flags')
    logger.info(f"Current debug state flags output: {output_lines}")

    assert 'Enabled flags' in output_lines and enabled_flag in output_lines
    assert 'Disabled flags' in output_lines and disabled_flag in output_lines


def trigger_sdk_health_event(duthost, fw_event_id):
    duthost.run_cmd(f'docker exec -it syncd python mellanox_sdk_trigger_event_script.py --fw_event {fw_event_id}')
