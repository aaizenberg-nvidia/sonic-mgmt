from __future__ import annotations

from typing import Callable, Tuple, Optional, Union, Dict, List
from functools import partial
import dataclasses
import logging
import random
import pytest
import time
import re

from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvlInterfaceConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.helpers import redmine_helpers
from ngts.tools.test_utils import pytest_utils
from ngts.tests_nvos.cluster import cluster_tools
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_tools.infra.Fae import Fae
from ngts.ngts_types import EnginesT
# TODO: remove this once KR feature is supported on all systems
from ngts.nvos_tools.Devices.IbDevice import RosalindSurrogateSwitch
from ngts.ngts_types import DevicesT

from . import helpers

pytestmark = pytest.mark.usefixtures("enable_cluster_for_mini_oberon", "has_active_access_ports")


@pytest.fixture(scope='module', autouse=True)
def skip_if_kr_not_supported(devices: DevicesT):
    if isinstance(devices.dut, RosalindSurrogateSwitch):
        pytest.skip(reason="KR feature is not supported on this system")
    yield


@dataclasses.dataclass
class KrAttribute:
    default: Union[str, int]
    values: List[Union[str, int]] = dataclasses.field(default_factory=list)

    def __post_init__(self):
        if isinstance(self.values, range):
            self.values = tuple(self.values)
        self.default = str(self.default)

    @property
    def max_value(self) -> Optional[int]:
        """ get the max value from the values list if the values are integers """
        if self.values and isinstance(self.values[0], int):
            return max(self.values)

    @property
    def min_value(self) -> Optional[int]:
        """ get the min value from the values list if the values are integers """
        if self.values and isinstance(self.values[0], int):
            return min(self.values)

    @property
    def random_value(self) -> str:
        """ get a random value from the values list """
        return str(random.choice(self.values))

    @property
    def bad_value(self) -> Union[str, int]:
        """ get a bad value from the values list """
        if isinstance(self.values[0], str):
            return 'bad-attribute-value'

        min_value, max_value = self.min_value, self.max_value
        return random.choice([
            random.choice(range(min_value - 100, min_value - 1)),
            random.choice(range(max_value + 1, max_value + 100))
        ])


logger = logging.getLogger(__name__)
IS_STANDALONE_SYSTEM = True
SETUP_NAME: str = None
CleanUpT = Callable[[Callable[[], None]], None]
KrConfigT = Dict[str, Union[str, int]]
KR_DEFAULT_CONFIG: Dict[str, str] = {
    "kr-algo": "disable-lt",
    "xdr-c2c-algo": "disable-lt",
    "num-iterations": '5',
    "ber-window": '1000',
    # "ber-target-coef": '1',  # XXX not supported in the current version
    # "ber-target-mag": '10',  # XXX not supported in the current version
    "prbs-type": "prbs31",
}
KR_MODES_MAPPER: Dict[str, KrAttribute] = {
    "kr-algo": KrAttribute(values=('regular-lt', 'advanced-lt'), default='disable-lt'),
    'xdr-c2c-algo': KrAttribute(values=('disable-lt', 'enable-lt'), default='disable-lt'),
    'num-iterations': KrAttribute(values=range(1, 31), default=5),
    'ber-window': KrAttribute(values=range(10, 3001, 10), default=1000),
    # 'ber-target-coef': KrAttribute(values=range(1, 10), default=1),  # XXX not supported in the current version
    # 'ber-target-mag': KrAttribute(values=range(1, 31), default=10),  # XXX not supported in the current version
    'prbs-type': KrAttribute(values=('prbs13', 'prbs31'), default='prbs31'),
}


@pytest.fixture(scope='module', autouse=True)
def cleanup_kr_config(access_ports: Port):
    yield
    Fae(port_name=access_ports.name).interface.link.link_training.unset(apply=True, ask_for_confirmation=True)
    helpers.reboot_gpus()


def _cleanup(port: Port):
    with allure.step(f"Unset KR config on port {port.name}"):
        Fae(port_name=port.name).interface.link.link_training.unset(apply=True, ask_for_confirmation=True)

    helpers.reboot_gpus()


def _configure_port_randomly(port: Port) -> dict[str, str]:
    expected_kr_config = {'xdr-c2c-algo': 'enable-lt'}
    with allure.step(f"Set random KR config on port {port.name}"):
        expected_kr_config['kr-algo'] = (rand_kr_mode := KR_MODES_MAPPER['kr-algo'].random_value)
        logger.info(f"Setting kr-algo to {rand_kr_mode}")
        Fae(port_name=port.name).interface.link.link_training.set('kr-algo', rand_kr_mode).verify_result()
        logger.info("Setting xdr-c2c-algo to enable-lt")

        for kr_attr in (i for i in KR_MODES_MAPPER if i not in ('kr-algo', 'xdr-c2c-algo')):
            expected_kr_config[kr_attr] = (kr_attr_value := KR_MODES_MAPPER[kr_attr].random_value)
            logger.info(f"Setting {kr_attr} to {kr_attr_value}")
            Fae(port_name=port.name).interface.link.link_training.set(kr_attr, kr_attr_value).verify_result()

        with allure.step('Apply KR config changes'):
            Fae(port_name=port.name).interface.link.link_training.set('xdr-c2c-algo', 'enable-lt', apply=True, ask_for_confirmation=True).verify_result()

        helpers.reboot_gpus()  # need to reboot the GPUs to ensure that link will go up

    return expected_kr_config


@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_kr_cli_flow(engines: EnginesT, register_cleanup: CleanUpT, access_ports: Port, request: pytest.FixtureRequest):
    port = helpers.get_random_port(engines.dut)

    with allure.step(f'show kr config on port: {port.name}'):
        str_result: str = Fae(port_name=port.name).interface.link.link_training.show(output_format=None)
        logger.debug(f"nv show interface {port.name} link kr result:\n{str_result}")

    with allure.step('Verify KR config is as expected'):
        str_errors = []
        for attr in KR_MODES_MAPPER:
            if not re.search(rf'{attr}\s+(.+)\s+(.+)', str_result):
                str_errors.append(msg := f'{attr} has wrong show format')
                logger.error(msg)

        assert not str_errors, "Errors:\n\t%s" % "\n\t".join(str_errors)

    register_cleanup(partial(_cleanup, access_ports))

    expected_kr_config = _configure_port_randomly(access_ports)
    time.sleep(5)
    with allure.step('Wait for port to be up'):
        tries = 25 if redmine_helpers.is_bug_active('4427436') else None
        with pytest_utils.temporary_xfail_mark(request,
                                               reason="The auto negotiation is not supported yet.",
                                               condition=helpers.ctx.has_gpus):
            Port.wait_for_port_state(port, NvosConsts.LINK_STATE_UP, tries=tries)

    with allure.step('Get KR config'):
        kr_config: KrConfigT = Fae(port_name=port.name).interface.link.link_training.parse_show()
        logger.info(f"KR config: {kr_config}")

        with allure.step('Verify KR config is as expected'):
            # check if expected_kr_config is a subset of kr_config
            assert expected_kr_config.items() <= kr_config.items(), \
                f"KR config is not as expected. Expected: {expected_kr_config}, Actual: {kr_config}"


@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_config_removal(engines: EnginesT, register_cleanup: CleanUpT):
    """
    Verify that unsetting the KR mode removes the explicit configuration and reverts to the default state.

    Test Steps:
        1. Get a random NVLink interface port
        2. Record the initial KR mode
        3. Run: nv fae unset interface <nvlink-interface-id> link kr mode
        4. Run: nv fae show interface <nvlink-interface-id> link kr
        5. Verify the KR mode is set to the default value

    Expected Outcome:
        The explicit configuration is removed, and the system falls back to the default KR configuration
    """
    port = helpers.get_random_port(engines.dut)
    logger.info(f"Selected NVLink port: {port.name}")

    kr_config: KrConfigT = Fae(port_name=port.name).interface.link.link_training.parse_show()

    with allure.step('Unset KR mode'):
        Fae(port_name=port.name).interface.link.link_training.unset(apply=True, ask_for_confirmation=True).get_returned_value()
        helpers.reboot_gpus()  # need to reboot the GPUs to ensure that link will go up
        with allure.step('Wait for port to be up'):
            Port.wait_for_port_state(port, NvosConsts.LINK_STATE_UP)

        with allure.step('Verify KR config is as expected'):
            kr_config: KrConfigT = Fae(port_name=port.name).interface.link.link_training.parse_show()
            assert KR_DEFAULT_CONFIG.items() <= kr_config.items(), f"KR config is not as expected. Expected: {KR_DEFAULT_CONFIG}, Actual: {kr_config}"


@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_invalid_kr_mode_rejected(engines: EnginesT):
    """
    Verify that attempting to configure an unsupported KR mode results in an error.

    Test Steps:
        1. Get a random NVLink interface port
        2. Run: nv fae set interface <nvlink-interface-id> link kr mode invalid_mode
        3. Verify the command fails as expected

    Expected Outcome:
        The CLI returns an error indicating that "invalid_mode" is not a supported KR mode,
        and the configuration is not applied
    """
    port = helpers.get_random_port(engines.dut)
    logger.info(f"Selected NVLink port: {port.name}")

    with allure.step('Attempt to set invalid KR mode'):
        for kr_attr, kr_attr_value in KR_MODES_MAPPER.items():
            Fae(port_name=port.name).interface.link.link_training.set(
                kr_attr,
                kr_attr_value.bad_value,
                apply=True,
                ask_for_confirmation=True
            ).get_returned_value(should_succeed=False)


@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_interface_range_sequential_actions(engines: EnginesT, register_cleanup: CleanUpT, access_ports: Port, request: pytest.FixtureRequest):
    """
    Verify that a range of NVLink interfaces correctly perform configuration actions.

    Test Steps:
        1. Select a group of NVLink interfaces
        2. Record the initial KR mode for each port
        3. Configure a new KR mode on the entire range at once
        4. Verify each port in the range has the new KR mode
        5. Wait for all ports to return to UP state

    Expected Outcome:
        All selected interfaces should be configured with the new KR mode
        and return to UP state successfully
    """
    ports_range, ports = helpers.get_random_ports(engines.dut, num_of_ports_to_select=5)
    logger.info(f"Selected port range: {ports_range.name}")
    logger.info(f"Individual ports: {[p.name for p in ports]}")

    ports_kr_configs: List[KrConfigT] = []
    for port in ports:
        kr_config = Fae(port_name=port.name).interface.link.link_training.parse_show()
        ports_kr_configs.append(kr_config)
        logger.info(f"Initial KR config for port {port.name}: {kr_config}")

    register_cleanup(partial(_cleanup, access_ports))
    errors: List[str] = []

    with allure.step('Set KR mode on the entire range'):
        expected_kr_config = _configure_port_randomly(access_ports)
        time.sleep(5)

        with allure.step('Wait for all ports to return to UP state'):
            for port in ports:
                tries = 25 if redmine_helpers.is_bug_active('4427436') else None
                with pytest_utils.temporary_xfail_mark(request,
                                                       reason="The auto negotiation is not supported yet.",
                                                       condition=helpers.ctx.has_gpus):
                    port.wait_for_port_state(port, NvosConsts.LINK_STATE_UP, tries=tries)
                # check that the expected_kr_config is NOT a subset of the current_kr_cfg
                if not expected_kr_config.items() <= (current_kr_cfg := Fae(port_name=port.name).interface.link.link_training.parse_show()).items():
                    errors.append(f"Port {port.name} did not get the expected KR mode. Expected: {expected_kr_config}, Actual: {current_kr_cfg}")

    assert not errors, "Errors:\n\t%s" % "\n\t".join(errors)


@pytest.mark.timeout(20 * MINUTE, func_only=True)
def test_link_transition_timing(engines: EnginesT, register_cleanup: CleanUpT, access_ports: Port, request: pytest.FixtureRequest):
    """
    Verify that after applying a KR configuration change, the interface goes down and returns up
    within defined time thresholds.

    Test Steps:
        1. Get a random NVLink interface port
        2. Configure a different KR mode than the current one
        3. Start a timer when the configuration is applied
        4. Monitor the interface status until a "link down" event is observed
        5. Continue monitoring until the interface is "up" again
        6. Calculate the elapsed times for link down and return up

    Expected Outcome:
        The interface goes down within 10 seconds and comes back up within 20 seconds
    """

    port = helpers.get_random_port(engines.dut)
    logger.info(f"Selected NVLink port: {port.name}")

    register_cleanup(partial(_cleanup, access_ports))

    with allure.step(f'Set random KR config on port {port.name}'):
        expected_kr_config = {
            'kr-algo': 'advanced-lt',
            'xdr-c2c-algo': 'enable-lt',
            # 'ber-target-coef': 1,  # XXX not supported yet, their value will be 0, exclude it for the meantime
            # 'ber-target-mag': 30,  # XXX not supported yet, their value will be 0, exclude it for the meantime
            'num-iterations': (num_iterations := KR_MODES_MAPPER['num-iterations'].random_value),
            'ber-window': (ber_window := KR_MODES_MAPPER['ber-window'].random_value),
        }
        timeout = (int(num_iterations) * int(ber_window)) * .0013  # iterations * ber-window  (msec) * 0.0013 (convert to seconds and add 30%) to timeout
        if redmine_helpers.is_bug_active('4427436'):  # TODO: remove this once the bug is fixed
            # [GPU FW IB Phy – Design] Bug SW #4427436: [GB200 NVL5][1.1 Build 2] When enabling the KR-X tuning parameters, link training times are much longer | Assignee: Omer Aroukh | Status: Assigned
            logger.info("Bug 4427436 is active, doubling the timeout")
            timeout *= 5

        with allure.step('Set KR config on port'):
            Fae(port_name=access_ports.name).interface.link.link_training.set('kr-algo', 'advanced-lt').get_returned_value()
            # Fae(port_name=access_ports.name).interface.link.link_training.set('ber-target-coef', KR_MODES_MAPPER['ber-target-coef'].min_value).get_returned_value()  # XXX not supported yet, but they won't be applied
            # Fae(port_name=access_ports.name).interface.link.link_training.set('ber-target-mag', KR_MODES_MAPPER['ber-target-mag'].max_value).get_returned_value()  # XXX not supported yet, but they won't be applied
            Fae(port_name=access_ports.name).interface.link.link_training.set('num-iterations', num_iterations).get_returned_value()
            Fae(port_name=access_ports.name).interface.link.link_training.set('ber-window', ber_window).get_returned_value()
            Fae(port_name=access_ports.name).interface.link.link_training.set('xdr-c2c-algo', 'enable-lt', apply=True, ask_for_confirmation=True).get_returned_value()

        helpers.reboot_gpus()  # need to reboot the GPUs to ensure that link will go up
        time.sleep(5)

        with allure.step('Wait for port to go up'):
            start_time = time.perf_counter()
            with allure.step('Wait for port to go down'):
                tries = 25 if redmine_helpers.is_bug_active('4427436') else None
                with pytest_utils.temporary_xfail_mark(request,
                                                       reason="The auto negotiation is not supported yet.",
                                                       condition=helpers.ctx.has_gpus):
                    Port.wait_for_port_state(port, NvosConsts.LINK_STATE_UP, tries=tries)
            end_time = time.perf_counter()
            elapsed_time = end_time - start_time
            logger.info(f"Elapsed time: {elapsed_time} seconds")

        with allure.step(f'Verify port went up within timeout {timeout:.02f} seconds'):
            assert elapsed_time < timeout, f"Port {port.name} did not go down within {timeout:.02f} seconds"

        with allure.step('Verify KR config is as expected'):
            assert expected_kr_config.items() <= (current_kr_cfg := Fae(port_name=port.name).interface.link.link_training.parse_show()).items(), \
                f"KR config is not as expected. Expected: {expected_kr_config}, Actual: {current_kr_cfg}"


@pytest.mark.timeout(20 * MINUTE, func_only=True)
def test_error_ber_target_not_reached(engines: EnginesT, register_cleanup: CleanUpT, access_ports: Port, request: pytest.FixtureRequest):
    """
    Verify that the system correctly handles a BER target not being reached.

    Test Steps:
        1. Get a random NVLink interface port
        2. Configure a KR mode with a specific BER target
        3. Start a timer when the configuration is applied
        4. Monitor the BER until it reaches the target
    """

    # port = helpers.get_random_port(engines.dut)
    port = Port('acp1', '', '')
    logger.info(f"Selected NVLink port: {port.name}")

    register_cleanup(partial(_cleanup, access_ports))

    with allure.step('Set KR mode with a specific BER target'):
        Fae(port_name=access_ports.name).interface.link.link_training.set('kr-algo', 'advanced-lt').get_returned_value()
        # Fae(port_name=access_ports.name).interface.link.link_training.set('ber-target-coef', KR_MODES_MAPPER['ber-target-coef'].min_value).get_returned_value()  # XXX not supported yet, but they won't be applied
        # Fae(port_name=access_ports.name).interface.link.link_training.set('ber-target-mag', KR_MODES_MAPPER['ber-target-mag'].max_value).get_returned_value()  # XXX not supported yet, but they won't be applied
        Fae(port_name=access_ports.name).interface.link.link_training.set('num-iterations', KR_MODES_MAPPER['num-iterations'].max_value).get_returned_value()
        Fae(port_name=access_ports.name).interface.link.link_training.set('ber-window', KR_MODES_MAPPER['ber-window'].max_value).get_returned_value()
        Fae(port_name=access_ports.name).interface.link.link_training.set('xdr-c2c-algo', 'enable-lt', apply=True, ask_for_confirmation=True).get_returned_value()
        helpers.reboot_gpus()  # need to reboot the GPUs to ensure that link will go up
        time.sleep(5)

        start_time = time.perf_counter()
        with allure.step('Wait for port to go up'):
            tries = 25 if redmine_helpers.is_bug_active('4427436') else None
            with pytest_utils.temporary_xfail_mark(request,
                                                   reason="The auto negotiation is not supported yet.",
                                                   condition=helpers.ctx.has_gpus):
                Port.wait_for_port_state(port, NvosConsts.LINK_STATE_UP, tries=tries)

        end_time = time.perf_counter()
        elapsed_time = end_time - start_time

    # 15 iterations * 3000 ber-window (3 seconds) * 0.0013 (convert to seconds and add 30%) to timeout
    timeout = (int(KR_MODES_MAPPER['num-iterations'].max_value) * int(KR_MODES_MAPPER['ber-window'].max_value)) * .0013
    if redmine_helpers.is_bug_active('4427436'):  # TODO: remove this once the bug is fixed
        # [GPU FW IB Phy – Design] Bug SW #4427436: [GB200 NVL5][1.1 Build 2] When enabling the KR-X tuning parameters, link training times are much longer | Assignee: Omer Aroukh | Status: Assigned
        logger.info("Bug 4427436 is active, doubling the timeout")
        timeout *= 2

    with allure.step('Verify port went down within timeout'):
        assert elapsed_time < timeout, f"Port {port.name} did not go down within {timeout} seconds"
