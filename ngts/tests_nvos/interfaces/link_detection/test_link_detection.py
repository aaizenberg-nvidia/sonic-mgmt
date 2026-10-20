import logging

import pytest

from ngts.nvos_constants.constants_nvos import LinkDetectionConsts, NvosConst, ApiType
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.mark.interfaces
@pytest.mark.link_detection
def test_port_xdr(engines, devices, random_api):
    """
    Validate the link detection is handled correctly for xdr connected port.

    Test flow:
    1. Select xdr connected port.
    2. Switch it to legacy mode.
    3. Switch back to xdr mode.
    """

    port_name = "swB9p2"

    with allure.step("Verify port is planarized"):
        _verify_port_planarization(port_name, LinkDetectionConsts.CONNECTION_MODE_XDR)

    try:
        _switch_port_connection_mode(port_name, LinkDetectionConsts.CONNECTION_MODE_NDR)
        _verify_port_planarization(port_name, LinkDetectionConsts.CONNECTION_MODE_NDR)
    finally:
        _switch_port_connection_mode(port_name, LinkDetectionConsts.CONNECTION_MODE_XDR)
        _verify_port_planarization(port_name, LinkDetectionConsts.CONNECTION_MODE_XDR)


@pytest.mark.interfaces
@pytest.mark.link_detection
def test_port_legacy(engines, devices, random_api):
    """
    Validate the link detection is handled correctly for legacy port.

    Test flow:
    1. Select legacy connected port.
    2. Switch it to xdr mode.
    3. Verify link is down.
    4. Switch back to legacy mode.
    5. Verify port is up and planarized.
    """

    port_name = "swB10p2"

    with allure.step("Verify port is not planarized and legacy"):
        _verify_port_planarization(port_name, LinkDetectionConsts.CONNECTION_MODE_NDR, lanes=4)

    try:
        _switch_port_connection_mode(port_name, LinkDetectionConsts.CONNECTION_MODE_XDR)
        _verify_port_planarization(port_name, LinkDetectionConsts.CONNECTION_MODE_XDR,
                                   link_state=NvosConst.PORT_STATUS_DOWN)
    finally:
        _switch_port_connection_mode(port_name, LinkDetectionConsts.CONNECTION_MODE_NDR)
        _verify_port_planarization(port_name, LinkDetectionConsts.CONNECTION_MODE_NDR)


@pytest.mark.interfaces
@pytest.mark.link_detection
def test_port_loop_out(engines, devices, random_api):
    """
    Validate the link detection is handled correctly for ports connected via loop-out.

    Test flow:
    1. Select two interconnected ports.
    2. Switch it to xdr mode.
    3. Verify link is down.
    4. Switch back to legacy mode.
    5. Verify port is up and planarized.
    """

    p1_name = "swB9p2"
    p2_name = "swB10p2"

    try:
        with allure.step("Verify ports are xdr"):
            _verify_port_planarization(p1_name, LinkDetectionConsts.CONNECTION_MODE_XDR)
            _verify_port_planarization(p2_name, LinkDetectionConsts.CONNECTION_MODE_XDR)

        with allure.step(f"Switch {p1_name} to legacy"):
            _switch_port_connection_mode(p1_name, LinkDetectionConsts.CONNECTION_MODE_NDR)
            _verify_port_planarization(p1_name, LinkDetectionConsts.CONNECTION_MODE_NDR,
                                       link_state=NvosConst.PORT_STATUS_DOWN)
            _verify_port_planarization(p2_name, LinkDetectionConsts.CONNECTION_MODE_XDR,
                                       link_state=NvosConst.PORT_STATUS_DOWN)

        with allure.step(f"Switch {p2_name} to legacy and verify both are up"):
            _switch_port_connection_mode(p2_name, LinkDetectionConsts.CONNECTION_MODE_NDR)
            _verify_port_planarization(p1_name, LinkDetectionConsts.CONNECTION_MODE_NDR, lanes=4)
            _verify_port_planarization(p2_name, LinkDetectionConsts.CONNECTION_MODE_NDR, lanes=4)

    finally:
        _switch_port_connection_mode_to_default(p1_name)
        _switch_port_connection_mode_to_default(p2_name)
        _verify_port_planarization(p1_name, LinkDetectionConsts.CONNECTION_MODE_XDR)
        _verify_port_planarization(p2_name, LinkDetectionConsts.CONNECTION_MODE_XDR)


@pytest.mark.interfaces
@pytest.mark.link_detection
def test_port_ranges(engines, devices):
    """
    Validate the link detection is handled correctly for range of ports.

    Test flow:
    1. Select port range to perform link-detection command.
    2. Switch them to legacy mode.
    3. Verify each is legacy.
    3. Switch back to default mode.
    """

    # active_ports = Port.get_list_of_active_ports()
    port_names = ["swB1p1", "swB1p2", "swB2p1", "swB2p2", "swB3p1", "swB3p2"]
    port_ranges_name = "swB1-3p1-2"

    try:
        with allure.step(f"Change {port_ranges_name} to non-planarized"):
            _switch_port_connection_mode(port_ranges_name, LinkDetectionConsts.CONNECTION_MODE_NDR)
        with allure.step(f"Verify each port from range {port_ranges_name} was affected"):
            for port_name in port_names:
                _verify_port_planarization(port_name, LinkDetectionConsts.CONNECTION_MODE_NDR)
    finally:
        _switch_port_connection_mode_to_default(port_ranges_name)


@pytest.mark.interfaces
@pytest.mark.link_detection
def test_port_split(engines, devices):
    """
    Validate all the link detection is handled after doing port split.

    Test flow:
    1. Set connection-mode to legacy
    1. Split the port
    2. Verify parent and child are with connection-mode legacy
    """

    port_name = "swB1p2"

    try:
        _switch_port_connection_mode(port_name, LinkDetectionConsts.CONNECTION_MODE_NDR)
        _split_port_for(engines.dut, port_name, IbInterfaceConsts.LINK_BREAKOUT_NDR)

        _verify_port_planarization(f"{port_name}s1", LinkDetectionConsts.CONNECTION_MODE_NDR)
        _verify_port_planarization(f"{port_name}s2", LinkDetectionConsts.CONNECTION_MODE_NDR)
    finally:
        _switch_port_connection_mode_to_default(port_name)
        _unsplit_port_for(engine=engines.dut, peer_port_name=port_name)


def test_with_system_reboot(engines, devices):
    """
    Validate all the link detection is handled after doing reboot.

    Test flow:
    1. Verify link_detection was triggered for a port.
    2. Do a system reboot.
    3. Verify link_detection was triggered for a port again.
    4. Verify link is up.
    """
    port_name = "swB1p2"
    try:
        _switch_port_connection_mode(port_name, LinkDetectionConsts.CONNECTION_MODE_NDR)
        _verify_port_planarization(port_name, LinkDetectionConsts.CONNECTION_MODE_NDR)

        with allure.step("Reboot the system"):
            DutUtilsTool.reload(engines.dut, devices.dut, "sudo reboot")

        _verify_port_planarization(port_name, LinkDetectionConsts.CONNECTION_MODE_NDR)
    finally:
        _switch_port_connection_mode(port_name, LinkDetectionConsts.CONNECTION_MODE_XDR)
        _verify_port_planarization(port_name, LinkDetectionConsts.CONNECTION_MODE_XDR)


def _split_port_for(engine, peer_port_name, breakout_val):
    parent_port = Port(peer_port_name, "", "")
    with allure.step("Split port, check default values for child and parent port"):
        parent_port.ib_interface.link.set(op_param_name='breakout', op_param_value=breakout_val,
                                          apply=True, ask_for_confirmation=True, dut_engine=engine).verify_result()
        output_dictionary = OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
            parent_port.show_interface(port_names=parent_port.name)).get_returned_value()
        ValidationTool.validate_fields_values_in_output(expected_fields=['link'],
                                                        expected_values=[
                                                            {'breakout': breakout_val}],
                                                        output_dict=output_dictionary).verify_result()


def _unsplit_port_for(engine, peer_port_name):
    parent_port = Port(peer_port_name, "", "")
    with allure.step("Unsplit port, check default values for child and parent port"):
        parent_port.ib_interface.link.unset(op_param_name='breakout', apply=True, ask_for_confirmation=True,
                                            dut_engine=engine).verify_result()


def _switch_port_connection_mode(port_name, connection_mode):
    with allure.step(f"Set connection-mode for {port_name} to {connection_mode}"):
        interface = Interface(parent_obj=None, port_name=port_name)
        interface.link.set(op_param_name=LinkDetectionConsts.CONNECTION_MODE,
                           op_param_value=connection_mode, apply=True,
                           ask_for_confirmation=True).verify_result()


def _switch_port_connection_mode_to_default(port_name):
    with allure.step(f"Set connection-mode for {port_name} default"):
        interface = Interface(parent_obj=None, port_name=port_name)
        interface.link.connection_mode.unset(apply=True,
                                             ask_for_confirmation=True).verify_result()


def _verify_port_planarization(port_name, connection_mode_expected, lanes=None, link_state=NvosConst.PORT_STATUS_UP):
    with allure.step(f"Verify {port_name} is in connection_mode {connection_mode_expected}"):
        output_fae_port = OutputParsingTool.parse_show_interface_output_to_dictionary(
            Fae(port_name=port_name).port.interface.show()).get_returned_value()
        fae_port_link = output_fae_port[IbInterfaceConsts.LINK]
        connection_mode = fae_port_link[IbInterfaceConsts.LINK_CONNECTION_MODE]
        planarized_port = output_fae_port[IbInterfaceConsts.PLANARIZED_PORTS]

        if connection_mode_expected == LinkDetectionConsts.CONNECTION_MODE_XDR:
            assert int(planarized_port) != 0, f"The {port_name} is not planarized"
        elif connection_mode_expected == LinkDetectionConsts.CONNECTION_MODE_NDR:
            assert int(planarized_port) == 0, f"The {port_name} is planarized"

        assert connection_mode == connection_mode_expected, f"The {port_name} is not {connection_mode_expected}"
        assert fae_port_link[IbInterfaceConsts.LINK_STATE] == link_state, "The link state is not up"
        if lanes:
            lanes_state = fae_port_link[IbInterfaceConsts.LINK_LANES]
            assert lanes_state == lanes, f"There is lanes mismatch. Got: {lanes_state} instead of {lanes}"
