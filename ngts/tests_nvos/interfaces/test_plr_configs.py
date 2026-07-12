from allure_commons.types import AttachmentType
from functools import partial
from typing import Iterable
import logging
import random
import pytest
import retry
import time
import re

from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import InternalNvosConsts, NvlInterfaceConsts, NvosConsts
from ngts.nvos_constants.constants_nvos import OutputFormat, MultiPlanarConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.tools.test_utils import allure_utils as allure
from ngts.ngts_types import EnginesT, DevicesT
from ngts.tests_nvos.constants import MINUTE
import sys

from ngts.tests_nvos.helpers import redmine_helpers  # TODO: remove after 5013850 is fixed
from . import helpers

if sys.version_info < (3, 11):
    from exceptiongroup import ExceptionGroup

pytestmark = pytest.mark.usefixtures("enable_cluster_for_mini_oberon", "has_active_access_ports")

logger = logging.getLogger(__name__)
PLR_DEFAULT_MODE = 'cs-and-crc'
PLR_MODES_MAPPER = {
    'margin-threshold1': {
        'margin-threshold': 1,
        'reject-mode': 'rejection-based-on-plr-margin',
    },
    'margin-threshold2': {
        'margin-threshold': 2,
        'reject-mode': 'rejection-based-on-plr-margin',
    },
    'cs': {
        'margin-threshold': 0,
        'reject-mode': 'rejection-based-on-cs',
    },
    'cs-and-crc': {
        'margin-threshold': 0,
        'reject-mode': 'rejection-based-on-crc-and-cs',
    },
}


@pytest.fixture(scope='module', autouse=True)
def cleanup_plr_config(access_ports: Port):
    yield
    access_ports.interface.link.plr.unset('mode', apply=True, ask_for_confirmation=True)
    helpers.reboot_gpus()


def _cleanup(port: Port):
    with allure.step(f"Unset PLR config on port {port.name}"):
        port.interface.link.plr.unset(apply=True, ask_for_confirmation=True)

    helpers.reboot_gpus()


def _dedupe_ports(*ports_groups: Iterable[Port]) -> list[Port]:
    """Keep ports in stable order while removing duplicates by name."""
    deduped_ports = {}
    for ports_group in ports_groups:
        for port in ports_group:
            deduped_ports.setdefault(port.name, port)

    return list(deduped_ports.values())


@retry.retry(exceptions=ExceptionGroup, tries=10 if redmine_helpers.is_bug_active(5013850) else 5, delay=5)
def _verify_plr_configuration(port: Port, mode: str):
    """
    Verify the PLR configuration on the port.

    Test Steps:
        1. Wait for the port to be up
        2. Verify the PLR configuration

    Expected Outcome:
        The PLR configuration is as expected

    Args:
        port: The port to verify the PLR configuration on
        mode: The mode to verify the PLR configuration for
    """
    with allure.step("Attempt to verify PLR configuration"):
        port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP, InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED)
        # the port could be up, but not down yet for applying the new configuration
        # therefore, we are using the retry,
        # but if the port is down,
        # we should wait till it goes up again so we can verify the configuration
        plr = port.interface.link.plr.parse_show()
        allure.attach(f"plr-cfg-{port.name}.json", plr, attachment_type=AttachmentType.JSON)
        logger.info(f"Current PLR configuration: {plr}")

        items = (
            ('mode', plr['mode'], mode),
            ('reject-mode', plr['reject-mode'], PLR_MODES_MAPPER[mode]['reject-mode']),
            ('margin-threshold', plr['margin-threshold'], PLR_MODES_MAPPER[mode]['margin-threshold']),
        )

        with allure.step('Verify PLR configuration'):
            for check_name, actual_value, expected_value in items:
                logger.info(f"Checking {check_name}: actual={actual_value}, expected={expected_value}")
                with allure.independent_step(f'Verify {check_name}'):
                    error_msg = f'{check_name} is not as expected. Expected: {expected_value!r}, Actual: {actual_value!r}'
                    assert actual_value == expected_value, error_msg


def _verify_plr_configuration_on_ports(ports: Iterable[Port], mode: str, step_name: str) -> None:
    verification_ports = list(ports)
    logger.info(f"{step_name}. Verification ports: {[port.name for port in verification_ports]}")

    sorted_ports = sorted(verification_ports, key=lambda p: int(re.search(r'\d+', p.name).group(0)))

    @retry.retry(exceptions=ExceptionGroup, tries=5, delay=10)
    def _wait_port_down(port: Port):
        port.wait_for_port_state(port, NvosConsts.LINK_STATE_DOWN)

    with allure.step(f"Wait for port {sorted_ports[0].name} to be down"):
        _wait_port_down(sorted_ports[0])

    with allure.step(step_name):
        # sort the ports by the port number, since lower number ports might be up faster than higher number ports
        for port in sorted_ports:
            with allure.independent_step(f'Verify PLR configuration on port {port.name}'):
                _verify_plr_configuration(port, mode)


@pytest.mark.timeout(20 * MINUTE, func_only=True)
def test_plr_cli_flow(engines: EnginesT, devices: DevicesT, access_ports: Port, register_cleanup, unregister_cleanup):
    last_acp_port_name = max(devices.dut.nvl_access_ports_list, key=lambda p: int(p.replace(NvlInterfaceConsts.ACP_PORT_TYPE, '')))
    port = Port(last_acp_port_name)
    logger.info(f"Selected NVLink port: {port.name}")

    with allure.step('Show PLR configuration'):
        logger.info(f"Running PLR show command on port {port.name}")
        output = port.interface.link.plr.show(output_format=OutputFormat.yaml)

        with allure.step('Validate output format'):
            modes = set(PLR_MODES_MAPPER.keys())
            modes_regex = "|".join(modes)
            logger.info(f"Validating mode in output against pattern: {modes_regex}")
            assert re.search(f'mode: ({modes_regex})', output), f"Output format is not as expected. Expected: {modes}, Actual: {output}"

            margin_thresholds = set(map(str, [i['margin-threshold'] for i in PLR_MODES_MAPPER.values()]))
            margin_thresholds_regex = r"\s*$|".join(margin_thresholds) + r"\s*$"
            logger.info(f"Validating margin-threshold in output against pattern: {margin_thresholds_regex}")
            assert re.search(f'margin-threshold: ({margin_thresholds_regex})', output, flags=re.M), \
                f"Output format is not as expected. Expected: {margin_thresholds}, Actual: {output}"

            reject_modes = set(i['reject-mode'] for i in PLR_MODES_MAPPER.values())
            reject_modes_regex = r"\s*$|".join(reject_modes) + r"\s*$"
            logger.info(f"Validating reject-mode in output against pattern: {reject_modes_regex}")
            assert re.search(f'reject-mode: ({reject_modes_regex})', output, flags=re.M), \
                f"Output format is not as expected. Expected: {reject_modes}, Actual: {output}"

    with allure.step('Select fixed verification ports'):
        verification_ports = _dedupe_ports((port,), helpers.get_random_ports(engines.dut)[1])
        allure.attach("verification-ports.json", [p.name for p in verification_ports], attachment_type=AttachmentType.JSON)

    with allure.step('Select random PLR mode'):
        port_mode: str = port.interface.link.plr.parse_show()['mode']
        logger.info(f"Current port mode: {port_mode}")
        available_modes = [mode for mode in PLR_MODES_MAPPER if mode != port_mode]
        random_mode = random.choice(available_modes)
        logger.info(f"Selected random mode for testing: {random_mode}")

        with allure.step(f'Set {random_mode} mode'):
            logger.info(f"Setting PLR mode to {random_mode} on port {port.name}")
            access_ports.interface.link.plr.set('mode', random_mode, apply=True, ask_for_confirmation=True)
            register_cleanup(cleanup_func_ptr := partial(_cleanup, access_ports))

            helpers.reboot_gpus()  # need to reboot the GPUs to ensure that link will go up

        logger.info(f"Verifying PLR configuration after setting mode to {random_mode}")

        _verify_plr_configuration_on_ports(verification_ports, random_mode, "Verify PLR configuration on fixed ports")

    with allure.step('Unset PLR mode'):
        logger.info(f"Unsetting PLR mode on port {port.name}")
        access_ports.interface.link.plr.unset('mode', apply=True, ask_for_confirmation=True)
        with allure.step(f"Wait for port {port.name} to be up"):
            port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP, InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED)
        helpers.reboot_gpus()  # need to reboot the GPUs to ensure that link will go up

        _verify_plr_configuration_on_ports(verification_ports, PLR_DEFAULT_MODE, "Verify PLR mode reverted to default on fixed ports")

        # the test successfully returned to the original state,
        # so we can unregister the cleanup function
        unregister_cleanup(cleanup_func_ptr)


@pytest.mark.timeout(MINUTE, func_only=True)
def test_feature_invalid_plr_mode_rejected(engines: EnginesT):
    """
    Verify that attempting to configure an unsupported PLR mode results in an error.

    Test Steps:
        1. Get a random NVLink interface port
        2. Run: nv set interface <nvlink-interface-id> link plr mode invalid_mode
        3. Verify the command fails as expected

    Expected Outcome:
        The CLI returns an error indicating that "invalid_mode" is not a supported PLR mode,
        and the configuration is not applied
    """
    port = helpers.get_random_port(engines.dut)
    logger.info(f"Selected NVLink port: {port.name}")

    with allure.step('Attempt to set invalid PLR mode'):
        logger.info(f"Attempting to set invalid PLR mode 'invalid-mode' on port {port.name}")
        port.interface.link.plr.set('mode', 'invalid-mode').verify_result(
            should_succeed=False,
            expected_value='Error:'
        )


@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_feature_link_transition_timing(engines: EnginesT, register_cleanup):
    """
    Verify that after applying a PLR configuration change, the interface goes down and returns up
    within defined time thresholds.

    Test Steps:
        1. Get a random NVLink interface port
        2. Configure a different PLR mode than the current one
        3. Start a timer when the configuration is applied
        4. Monitor the interface status until a "link down" event is observed
        5. Continue monitoring until the interface is "up" again
        6. Calculate the elapsed times for link down and return up

    Expected Outcome:
        The interface goes down and up within 12 seconds
    """
    port = helpers.get_random_port(engines.dut)
    logger.info(f"Selected NVLink port: {port.name}")

    plr_mode = port.interface.link.plr.parse_show()['mode']
    logger.info(f"Current PLR mode on port {port.name}: {plr_mode}")

    available_modes = [mode for mode in PLR_MODES_MAPPER if mode != plr_mode]
    random_mode = random.choice(available_modes)
    logger.info(f"Selected random mode for testing: {random_mode}")

    start_time = time.perf_counter()
    logger.info(f"Starting timer at {start_time}")

    with allure.step(f'Set {random_mode} mode'):
        logger.info(f"Setting PLR mode to {random_mode} on port {port.name}")
        port.interface.link.plr.set('mode', random_mode, apply=True, ask_for_confirmation=True)
        register_cleanup(partial(_cleanup, port))

        helpers.reboot_gpus()  # need to reboot the GPUs to ensure that link will go up
        port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP, InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED)
        logger.info(f"Successfully set PLR mode to {random_mode}")

    start_time = time.perf_counter()
    with allure.step('Wait for port to come back up'):
        logger.info(f"Waiting for port {port.name} to come back up")
        port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP, InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED)
        up_time = time.perf_counter() - start_time
        logger.info(f"Port {port.name} came back up after {up_time:.2f} seconds")

        # TODO: verify what is the expected time for the port to go up
        expected_transition_time = MultiPlanarConsts.PORT_UP_MAX_TIME + MultiPlanarConsts.PORT_DOWN_MAX_TIME
    assert up_time < expected_transition_time, \
        f'Port did not go up within {expected_transition_time} seconds (took {up_time:.2f}s)'
