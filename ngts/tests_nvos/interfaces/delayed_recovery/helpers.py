import logging
import time

from ngts.cli_wrappers.nvue import nvue_general_clis as gen_clis
from ngts.nvos_constants.constants_nvos import ConfState, PlatformConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import (
    IbInterfaceConsts,
    InternalNvosConsts,
    NvosConsts,
)
from ngts.nvos_tools.infra import NvosTestToolkit as TestToolkit
import ngts.nvos_tools.infra.Tools as Tools
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.interfaces.delayed_recovery.constants import (
    OPER_ENABLED_LOSS_TH_COPPER,
    OPER_ENABLED_LOSS_TH_OPTICAL,
    PASSIVE_COPPER_CABLE_TYPE,
)

logger = logging.getLogger()


def apply_config():
    with allure.step("Apply delayed recovery configuration"):
        return gen_clis.NvueGeneralCli.apply_config(
            TestToolkit.TestToolkit.engines.dut,
            ask_for_confirmation=True,
        )


def _wait_for_physical_linkup(fae_port, timeout=InternalNvosConsts.DEFAULT_TIMEOUT, sleep_time=2):
    """
    Wait until the port reports physical-state LinkUp.

    'state up' is not enough here: the port reports it while physical-state is still
    Polling / PortConfigurationTraining, i.e. while the FW is still negotiating with the
    peer. Delayed-recovery operational values are only valid once the port is physically
    back up, so reading them earlier returns the pre-flap values.
    """
    port_name = fae_port.port.name
    with allure.step(f"Wait for '{port_name}' to reach physical-state "
                     f"{IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP} (timeout: {timeout})"):
        timer = timeout
        physical_state = None
        while timer > 0:
            # .get(): during a flap - exactly when this runs - the show payload can
            # come back without physical-state, and a bare subscript would die with
            # KeyError instead of polling on to the intended timeout message.
            physical_state = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                fae_port.port.interface.link.show()).get_returned_value().get(
                    IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE)
            if physical_state == IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP:
                return
            time.sleep(sleep_time)
            timer -= sleep_time

        raise AssertionError(
            f"Timeout ({timeout}s) waiting for '{port_name}' to reach physical-state "
            f"{IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP}, last seen '{physical_state}'")


def wait_for_delayed_recovery_ports(fae_ports, timeout=InternalNvosConsts.DEFAULT_TIMEOUT):
    """
    Wait for every port to be up, first by link state and then by physical state.

    Both phases share one deadline: giving each its own full timeout would let a
    single call burn 2 x timeout per port, which stacks across the six scenarios and
    the fixture setup/teardown until pytest kills the test with an opaque timeout
    instead of the intended diagnostic.
    """
    with allure.step("Wait for delayed recovery ports to be up"):
        deadline = time.monotonic() + timeout

        def _remaining():
            return max(1, int(deadline - time.monotonic()))

        for fae_port in fae_ports:
            fae_port.port.interface.wait_for_port_state(
                NvosConsts.LINK_STATE_UP,
                timeout=_remaining(),
            ).verify_result()
        for fae_port in fae_ports:
            _wait_for_physical_linkup(fae_port, timeout=_remaining())


def validate_expected_values(fae_port, expected_values):
    with allure.step("Validate expected values"):
        output = Tools.OutputParsingTool.parse_json_str_to_dictionary(fae_port.interface.link.delayed_recovery.show()).verify_result()
        Tools.ValidationTool.compare_dictionaries(output, expected_values).verify_result()


def _stringify_values(values):
    if isinstance(values, dict):
        return {
            key: _stringify_values(value)
            for key, value in values.items()
        }
    return str(values)


def validate_expected_values_by_rev(fae_port, expected_values_by_rev):
    with allure.step(f"Validate delayed recovery values for {fae_port.port.name}"):
        actual_values_by_rev = {
            rev: _stringify_values(fae_port.interface.link.delayed_recovery.parse_show(rev=rev))
            for rev in expected_values_by_rev
        }
        Tools.ValidationTool.compare_nested_dictionary_content(
            actual_values_by_rev,
            expected_values_by_rev,
        ).verify_result()


def unset_delayed_recovery(fae_ports, apply=True):
    with allure.step("Unset delayed recovery configuration"):
        for fae_port in fae_ports:
            fae_port.interface.link.delayed_recovery.unset().verify_result()
        if apply:
            apply_config()
            wait_for_delayed_recovery_ports(fae_ports)


def set_delayed_recovery_values(fae_port, delayed_recovery_values):
    with allure.step(f"Set delayed recovery values for {fae_port.port.name}"):
        for field, value in delayed_recovery_values.items():
            fae_port.interface.link.delayed_recovery.set(field, value).verify_result()


def delayed_recovery_expected(applied, operational):
    return {
        ConfState.APPLIED: applied,
        ConfState.OPERATIONAL: operational,
    }


def get_enabled_loss_th(fae_port):
    """
    Expected operational fec-plr-align-loss-th when delayed recovery is enabled.

    The FW default is module-dependent: passive copper resolves to 127, optical to
    126. plr-retry-th is 255 regardless. Verified on croc-94 with a copper and an
    optical loopback pair on the same switch and firmware.

    The transceiver is addressed per cage ('swA13p1' -> 'swA13'), resolved with
    Port.get_transceiver_name() so split ('sw13p1s2') and plane ('swA1p1pl1') port
    names map correctly too. A module that is present but reports no cable-type is
    treated as non-passive, i.e. 126.
    """
    port_name = fae_port.port.name
    cage_name = fae_port.port.get_transceiver_name()
    with allure.step(f"Get module type of '{cage_name}' to derive the expected loss threshold"):
        transceivers = Tools.OutputParsingTool.parse_json_str_to_dictionary(
            Platform().transceiver.show_detailed()).verify_result()
        entry = transceivers.get(cage_name)
        # Fail loudly rather than defaulting: a cage we cannot find means the name
        # mapping is wrong, and silently assuming 126 would surface later as a bogus
        # threshold mismatch that reads like a firmware bug.
        assert isinstance(entry, dict), (
            f"transceiver cage '{cage_name}' (from port '{port_name}') not found in platform "
            f"transceiver output; available: {sorted(transceivers)}")
        cable_type = entry.get(PlatformConsts.TRANSCEIVER_CABLE_TYPE)
        if cable_type is None:
            logger.warning(f"no {PlatformConsts.TRANSCEIVER_CABLE_TYPE} for '{cage_name}', "
                           f"assuming non-passive module")
        is_passive_copper = cable_type == PASSIVE_COPPER_CABLE_TYPE
        expected = OPER_ENABLED_LOSS_TH_COPPER if is_passive_copper else OPER_ENABLED_LOSS_TH_OPTICAL
        logger.info(f"'{port_name}' (cage '{cage_name}') cable-type '{cable_type}' "
                    f"-> expected fec-plr-align-loss-th {expected}")
        return expected
