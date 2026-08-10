import random
import pytest
import logging
import time  # TODO: Remove when XDR workaround is removed
from typing import Tuple
from ngts.ngts_types.devices_T import DevicesT
from ngts.ngts_types.engines_T import EnginesT
from ngts.nvos_constants.constants_nvos import MultiPlanarConsts
from ngts.nvos_tools.infra.MultiPlanarTool import MultiPlanarTool
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.Devices.IbDevice import RosalindSimx, RosalindStackedSimx, RosalindSwitch  # TODO: Remove RosalindSwitch import when XDR WA is removed
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import MloopConsts
from ngts.nvos_tools.infra.IbInterfaceTool import IbInterfaceTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.cluster.cluster_tools import summarize_switch_ports
from ngts.tests_nvos.interfaces.nvl_port.helpers import get_linked_ports_pair

logger = logging.getLogger(__name__)


@pytest.fixture(scope='module')
def mloop_mode(request) -> MloopConsts.Mode:
    """
    FAE mloop mode applied by rosalind_simx_setup. Default: LOGICAL (LLU2LLU loopback).

    Indirect-parametrizable: tests can pick a mode via::

        @pytest.mark.parametrize('mloop_mode', [MloopConsts.Mode.PHY], indirect=True)
        def test_something(...):
            ...

    Accepts either a MloopConsts.Mode member or its string value ('phy', 'logical',
    'disabled'). Invalid values raise ValueError with the list of valid modes.
    """
    raw = getattr(request, 'param', MloopConsts.Mode.LOGICAL)
    if isinstance(raw, MloopConsts.Mode):
        return raw
    try:
        return MloopConsts.Mode(raw)
    except ValueError:
        valid = ', '.join(m.value for m in MloopConsts.Mode)
        raise ValueError(
            f"mloop_mode={raw!r} is not a valid MloopConsts.Mode (valid: {valid})"
        )


@pytest.fixture(scope='module', autouse=True)
@pytest.mark.timeout(20 * MINUTE, func_only=True)  # 20 minutes - setup typically takes 7-15 minutes
def rosalind_simx_setup(engines, devices, mloop_mode, nv_command):
    """
    RosalindSimx specific setup configuration that runs once per test module (.py file).

    Per nvbug 6072833 the FAE mloop knob is per-interface, and the system toggles
    affected ports automatically. The fixture:
    1. Sets fae mloop=<mloop_mode> on all access ports (auto-toggles ports up)
    2. Waits for stabilization
    3. On teardown, sets fae mloop=disabled on the same ports

    The mode comes from the `mloop_mode` fixture (default: LOGICAL); test modules
    can override that fixture to pick a different mode.

    Note: 20 minute timeout allows for variability in setup time (typically 7-15 minutes).
    Individual tests still have 900s timeout from MARS configuration.
    """
    # Skip setup if bug 4681425 is active (SIMX instability with loopback config)
    if is_bug_active(4681425):
        logger.warning("Bug 4681425 is active - skipping RosalindSimx loopback setup, tests will run without it")
        yield
        return

    # Check if this device requires MLOOP setup (simx flavors and future compatible platforms)
    if not devices.dut.require_mloop_setup:
        logger.info("Device does not require MLOOP setup, skipping special configuration")
        yield
        return

    # Use the reusable utility function for RosalindSimx loopback configuration
    config_success = IbInterfaceTool.configure_rosalind_simx_loopback(
        engines, devices, mloop_mode=mloop_mode)
    if not config_success:
        pytest.fail("RosalindSimx loopback configuration failed - cannot run tests")

    logger.info("RosalindSimx loopback configuration completed successfully")

    # Verify that links are actually up
    verification_success = IbInterfaceTool.verify_rosalind_simx_links_up(devices, max_retries=2, retry_wait_minutes=3)
    if not verification_success:
        pytest.fail("RosalindSimx link verification failed - no ports found UP")

    logger.info("RosalindSimx link verification successful")

    yield  # ALL tests in module execute here

    # Cleanup: disable per-interface MLOOP after all tests complete (system auto-toggles ports)
    if config_success:
        logger.info("Cleaning up: disabling per-interface MLOOP")
        try:
            access_ports = devices.dut.nvl_access_ports_list
            if access_ports:
                port_range = summarize_switch_ports(access_ports)
                with allure.step(f"Disable mloop on {port_range}"):
                    nv_command.fae.interfaces[port_range].link.mloop.set(
                        op_param_name=MloopConsts.Mode.DISABLED,
                        apply=True,
                        ask_for_confirmation=True,
                    ).verify_result()
                    logger.info(f"Set mloop=disabled on {port_range} (system auto-toggles ports)")

            engines.dut.run_cmd('nv config save')
            logger.info("System restored to initial state: MLOOP disabled, config saved")
        except Exception as e:
            logger.warning(f"Failed during cleanup: {e}")


# TODO: XDR Workaround Fixture - Currently DISABLED
# This fixture is a temporary workaround for XDR speed issues on RosalindSwitch
# To ENABLE this fixture: Change autouse=False to autouse=True below
# To DISABLE this fixture: Change autouse=True to autouse=False below
@pytest.fixture(scope='module', autouse=False)  # DISABLED - change to autouse=True to enable
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def xdr_wa_rosalind(engines, devices):
    """
    TODO: TEMPORARY WORKAROUND - Remove this entire fixture when XDR speed issues are fixed

    XDR workaround for Rosalind systems - applies 200G speed to all access ports.

    This fixture runs once per test module and performs:
    1. Sets all access ports speed to 200G (links come up automatically)
    2. Saves configuration
    3. Waits for 1 minute for system stabilization

    Cleanup restores default speed configuration.

    Note: Only runs on RosalindSwitch (not RosalindSimx).
    """
    # TODO: This entire fixture is a temporary workaround and should be removed
    # Only run on RosalindSwitch (not RosalindSimx)
    if not isinstance(devices.dut, RosalindSwitch):
        logger.info("Not a RosalindSwitch device, skipping XDR workaround")
        yield
        return

    logger.info("RosalindSwitch detected - applying XDR 200G speed workaround (TEMPORARY WA)")

    # Get access ports list and create range string
    access_ports = devices.dut.nvl_access_ports_list
    if not access_ports:
        logger.warning("No access ports found, skipping XDR workaround")
        yield
        return

    port_range = summarize_switch_ports(access_ports)
    logger.info(f"Access ports range: {port_range}")

    config_success = False
    try:
        # Step 1: Set speed to 200G (links will come up automatically)
        with allure.step(f"Set {port_range} speed to 200G"):
            engines.dut.run_cmd(f'nv set interface {port_range} link speed 200G')
            engines.dut.run_cmd('nv -y config apply', timeout=5000)  # 5 minutes timeout
            logger.info(f"Set {port_range} speed to 200G")

        # Step 2: Save configuration
        with allure.step("Save configuration"):
            engines.dut.run_cmd('nv config save')
            logger.info("Configuration saved")

        # Step 3: Wait for system stabilization
        with allure.step("Wait 1 minute for system stabilization"):
            logger.info("Waiting 1 minute for system stabilization...")
            time.sleep(60)
            logger.info("XDR 200G workaround applied successfully - links should be up")

        config_success = True

    except Exception as e:
        logger.error(f"Failed to apply XDR workaround: {e}")
        pytest.fail(f"XDR workaround configuration failed: {e}")

    yield  # ALL tests in module execute here

    # TODO: Cleanup for temporary workaround - remove when fixture is removed
    # Cleanup: Restore default speed
    if config_success:
        logger.info("Cleanup: Restoring default speed configuration (XDR WA cleanup)")
        try:
            with allure.step(f"Unset speed on {port_range} to restore default"):
                engines.dut.run_cmd(f'nv unset interface {port_range} link speed')
                engines.dut.run_cmd('nv -y config apply', timeout=5000)  # 5 minutes timeout
                logger.info(f"Unset speed on {port_range}")

            # Save configuration
            engines.dut.run_cmd('nv config save')
            logger.info("System restored: default speed configuration saved")
        except Exception as e:
            logger.warning(f"Failed during cleanup: {e}")


@pytest.fixture(scope='session', autouse=True)
def install_and_uninstall_platform_file(engines, devices):
    """
    install/uninstall xdr simulation on switch.
    """
    system = System(devices_dut=devices.dut)

    with allure.step("install xdr simulation on switch"):
        MultiPlanarTool.override_platform_file(system, engines, devices, MultiPlanarConsts.NVL_SIMULATION_FILE)

    yield

    with allure.step("uninstall xdr simulation on switch"):
        MultiPlanarTool.override_platform_file(system, engines, devices, MultiPlanarConsts.ORIGIN_FILE)


@pytest.fixture(scope="session")
def linked_ports_pair(engines: EnginesT, devices: DevicesT, has_loopbox: bool) -> Tuple[str, str]:
    """
    Session-scoped fixture that provides a tuple of two linked port names.
    """
    if not has_loopbox:
        pytest.skip("No loopbox found, skipping linked ports pair")

    return get_linked_ports_pair(devices, engines)
