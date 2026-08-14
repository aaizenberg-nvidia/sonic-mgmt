import logging

import pytest

from ngts.nvos_constants.constants_nvos import ConfState
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import PhyRecoveryConsts
from ngts.nvos_tools.infra.Fae import Fae
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.interfaces.delayed_recovery.helpers import (
    apply_config,
    wait_for_delayed_recovery_ports,
)

logger = logging.getLogger()


@pytest.fixture(scope='package')
def delayed_recovery_ports(ib_ports):
    """
    Resolve the connected switch-switch (loopback) port pair once per package.

    Package scoped, not session scoped, so the pair is looked up once for these tests
    without the logic-relock teardown below being deferred to the end of the whole
    pytest run.

    The pair comes from live ibdiagnet discovery rather than the static connectivity
    json, so a link that has gone down (or come up) since that json was last
    regenerated is handled correctly.

    IB only: delayed-recovery is listed in NVL's unsupported_commands_list
    ("only supported on croc+mamba"), and these cases run from nvos_multi_planar.db,
    so there is no NVL path to keep working here.
    """
    loopback_pairs = ib_ports.all_loop_ports()
    if not loopback_pairs:
        pytest.skip("delayed-recovery peer tests need a connected switch-switch (loopback) port "
                    f"pair with both ends up; none found on '{ib_ports.description}'")
    port_name, peer_name = loopback_pairs[0]
    logger.info(f"selected loopback pair: {port_name} <-> {peer_name} "
                f"(of {len(loopback_pairs)} available)")
    return Fae(port_name=port_name), Fae(port_name=peer_name)


@pytest.fixture(scope='package')
def disable_logic_relock(delayed_recovery_ports):
    """
    Disable phy-recovery logic-relock on both ends of the link for these tests.

    logic-relock interferes with delayed-recovery operational values: with it active
    the enabled-mode thresholds are not resolved consistently, so delayed-recovery
    scenarios must run with it off on BOTH ends (the peer's setting affects the
    negotiated result too).

    Not autouse: only the tests that exercise a peer pair need it, and requesting it
    also pulls in the loopback-pair lookup, which tests that pick their own random
    ports must not be gated on.

    The original applied values are captured up front and restored afterwards, so the
    DUT is left as found. Capturing matters rather than assuming a default: on Black
    Mamba the platform default is enabled/100 while on Crocodile it is disabled/0.
    """
    mode_field = PhyRecoveryConsts.LogicRelock.MODE
    original_values = {}

    def restore():
        if not original_values:
            return
        with allure.step("Restore phy-recovery logic-relock on both link ends"):
            for fae_port in delayed_recovery_ports:
                port_name = fae_port.port.name
                if port_name not in original_values:
                    # Never written, nothing to put back.
                    continue
                original = original_values[port_name]
                if original is None:
                    # The leaf had no applied value before we touched it, so restoring
                    # means removing ours - setting it back would be skipping the
                    # restore entirely and leaving logic-relock disabled on the DUT.
                    logger.info(f"'{port_name}': no original {mode_field} applied, unsetting")
                    fae_port.interface.link.phy_recovery.unset(mode_field).verify_result()
                else:
                    logger.info(f"'{port_name}': restoring {mode_field} to '{original}'")
                    fae_port.interface.link.phy_recovery.set(mode_field, original).verify_result()
            apply_config()
            wait_for_delayed_recovery_ports(delayed_recovery_ports)

    try:
        with allure.step("Disable phy-recovery logic-relock on both link ends"):
            for fae_port in delayed_recovery_ports:
                port_name = fae_port.port.name
                applied = fae_port.interface.link.phy_recovery.parse_show(rev=ConfState.APPLIED)
                original_values[port_name] = applied.get(mode_field)
                logger.info(f"'{port_name}' original {mode_field}: {original_values[port_name]}")
                fae_port.interface.link.phy_recovery.set(
                    mode_field, PhyRecoveryConsts.DISABLED).verify_result()
            apply_config()
            wait_for_delayed_recovery_ports(delayed_recovery_ports)
    except Exception:
        # Setup failed partway - the ports already written must still be put back,
        # otherwise logic-relock stays disabled on the DUT for every later session.
        logger.error("logic-relock setup failed, restoring captured values before re-raising")
        restore()
        raise

    yield delayed_recovery_ports

    restore()
