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
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import MloopConsts, NvosConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.IbInterfaceTool import IbInterfaceTool
from ngts.nvos_tools.infra.NvCommand import NvCommand
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.cluster.cluster_tools import summarize_switch_ports
from ngts.tests_nvos.interfaces.nvl_port.helpers import (
    get_acp_ports_in_state,
    get_linked_ports_pair,
    reset_gpus_if_needed,
    skip_if_no_access_links,
    wait_for_ports_state,
)
from ngts.tests_nvos.interfaces.nvl_port.phy_recovery_helpers import is_nvl7

logger = logging.getLogger(__name__)
# Trunks are waited for once per session: they take minutes to link in mloop and stay up between tests
_trunk_wait = {'done': False}


@pytest.fixture(scope='session')
def has_loopbox(has_loopbox: bool, devices: DevicesT) -> bool:
    """On simx the FAE mloop loopback configured by nvl_simx_setup plays the loopbox role."""
    return has_loopbox or devices.dut.require_mloop_setup


@pytest.fixture(scope='module')
def mloop_mode(request) -> MloopConsts.Mode:
    """
    FAE mloop mode applied by nvl_simx_setup. Default: LOGICAL (LLU2LLU loopback).

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


@pytest.fixture(scope='package', autouse=True)
def nvl_simx_cleanup(engines, devices):
    """Disable mloop once after all NVL interface tests finish."""
    if not devices.dut.require_mloop_setup:
        yield
        return

    yield

    logger.info("Cleaning up: disabling per-interface MLOOP")
    try:
        for ports in (devices.dut.nvl_access_ports_list, devices.dut.nvl_trunk_ports_list):
            if not ports:
                continue
            port_range = summarize_switch_ports(ports)
            with allure.step(f"Disable mloop on {port_range}"):
                NvCommand().fae.interfaces[port_range].link.mloop.set(
                    op_param_name=MloopConsts.Mode.DISABLED,
                    apply=True,
                    ask_for_confirmation=True,
                ).verify_result()
                logger.info(f"Set mloop=disabled on {port_range} (system auto-toggles ports)")

        engines.dut.run_cmd('nv config save')
        logger.info("System restored to initial state: MLOOP disabled, config saved")
    except Exception as e:
        logger.warning(f"Failed during cleanup: {e}")


@pytest.fixture(scope='function', autouse=True)
def nvl_simx_setup(engines, devices, mloop_mode):
    """
    NVL SIMX-specific setup configuration that runs before each test.

    Per nvbug 6072833 the FAE mloop knob is per-interface, and the system toggles
    affected ports automatically. The fixture:
    1. Ensures fae mloop=<mloop_mode> on all access and trunk ports
    2. Waits for every access port to be up, and once per session for the trunk ports
       (a trunk that stays down is only logged)

    The mode comes from the `mloop_mode` fixture (default: LOGICAL); test modules
    can override that fixture to pick a different mode.
    """
    # Check if this device requires MLOOP setup (simx flavors and future compatible platforms)
    if not devices.dut.require_mloop_setup:
        logger.info("Device does not require MLOOP setup, skipping special configuration")
        return

    # Skip setup if bug 4681425 is active (SIMX instability with loopback config)
    if is_bug_active(4681425):
        logger.warning("Bug 4681425 is active - skipping NVL SIMX loopback setup, tests will run without it")
        return

    # Use the reusable utility function for NVL SIMX loopback configuration
    if not IbInterfaceTool.configure_nvl_simx_loopback(engines, devices, mloop_mode=mloop_mode):
        pytest.fail("NVL SIMX loopback configuration failed - cannot run tests")

    logger.info("NVL SIMX loopback configuration completed successfully")

    access_ports = devices.dut.nvl_access_ports_list
    with allure.step(f"Wait for all {len(access_ports)} ACP ports to be up"):
        wait_for_ports_state(devices, access_ports, 'acp')
    trunk_ports = devices.dut.nvl_trunk_ports_list
    if trunk_ports and not _trunk_wait['done']:
        _trunk_wait['done'] = True
        with allure.step(f"Wait for all {len(trunk_ports)} trunk ports to be up"):
            try:
                wait_for_ports_state(devices, trunk_ports, 'sw')
            except AssertionError as error:
                logger.warning("Not all trunk ports are up, trunk tests use the up ones: %s", error)
    logger.info("All NVL SIMX ACP links are up")


@pytest.fixture
def nvl7_acp_phy_recovery(request, devices, is_simx, has_loopbox, standalone_system):
    """
    Skip off NVL7 or without connected access ports. If the test failed before its own unset, unset
    phy-recovery on all access ports and wait for them up.
    """
    if not is_nvl7(devices):
        pytest.skip("NVL7 only test")
    skip_if_no_access_links(has_loopbox, standalone_system, is_simx)
    yield
    rep_call = getattr(request.node, 'rep_call', None)
    if rep_call is None or not rep_call.failed:
        return
    acp_names = devices.dut.nvl_access_ports_list
    all_ports = Fae(port_name=summarize_switch_ports(acp_names))
    with allure.step(f"Cleanup - unset phy-recovery on {all_ports.port.name}"):
        all_ports.port.interface.link.phy_recovery.unset(apply=True, ask_for_confirmation=True).verify_result()
        reset_gpus_if_needed(request.getfixturevalue('setup_name'))
        wait_for_ports_state(devices, acp_names, 'acp')


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


@pytest.fixture
def down_access_ports(access_ports: Port) -> list[str]:
    """
    Names of the access ports that are already down before the test touches anything.

    On a standalone or partially cabled setup those ports have no link partner, so they will never
    come up. Tests pass this list as `exclude_ports` to the range waits, so a port that was never
    up does not hold the whole link-up budget open and then fail the test. Function-scoped on
    purpose: the snapshot has to be re-read per test, not cached across them.

    The snapshot reads the all-interfaces show, which exists on both NVUE and OpenAPI, so it does
    not matter that fixtures run before the test body picks the API for the test.
    """
    return get_acp_ports_in_state(access_ports, NvosConsts.LINK_STATE_DOWN)
