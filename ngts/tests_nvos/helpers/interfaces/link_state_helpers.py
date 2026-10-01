import logging

from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.nvos_tools.infra.RegressionConfigurations import Configurations
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


def get_all_interfaces_with_transceivers(devices, engine=None):
    interfaces = []
    platform = Platform()
    present_transceivers = platform.transceiver.get_list_of_connected_transceivers(dut_engine=engine)

    for transceiver in present_transceivers:
        interfaces.extend([interface for interface in devices.dut.nvl_trunk_ports_list if interface.startswith(f"{transceiver}p")])
        # This way, if sw1 is present and sw10 is not, we will not take sw10. because we force sw1p so sw10p falls.
    return interfaces


def verify_external_interfaces_state_up_and_active(devices, engine=None):
    interfaces = get_all_interfaces_with_transceivers(devices, engine=engine)
    for interface in interfaces:
        selected_port = Port(interface, "", "")
        # Verify fields.
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_port.interface.link.show(dut_engine=engine)).get_returned_value()
        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.LINK_STATE,
                                                          expected_value=NvosConsts.LINK_STATE_UP).verify_result()
        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.LINK_LOGICAL_PORT_STATE,
                                                          expected_value=IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE).verify_result()
        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE,
                                                          expected_value=IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP).verify_result()


def verify_interface_up(devices, has_loopbox, setup_name, engine=None):
    # Build list of interface types to check based on what actually exists
    interface_types = []

    if setup_name in Configurations.non_standalone_systems:
        # Non-standalone systems: only check access ports
        interface_types = ['acp']
    elif has_loopbox:
        # Loopbox systems: check FNM and access ports (if they exist)
        # Check if device actually has regular FNM ports (not just internal FNM)
        if hasattr(devices.dut, 'nvl_fnm_ports') and devices.dut.nvl_fnm_ports:
            interface_types.append('fnm')
        interface_types.append('acp')

    for interface_type in interface_types:
        port_type = 'fnm' if interface_type == 'fnm' else ''
        selected_port = Tools.RandomizationTool.select_random_port(
            dut_engine=engine,
            requested_ports_logical_state=NvosConsts.LINK_LOG_STATE_ACTIVE,
            requested_ports_type=port_type,
            interface_type=interface_type).get_returned_value()
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_port.interface.link.show(dut_engine=engine)).get_returned_value()
        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.LINK_STATE,
                                                          expected_value=NvosConsts.LINK_STATE_UP).verify_result()
        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.LINK_LOGICAL_PORT_STATE,
                                                          expected_value=IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE).verify_result()
        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE,
                                                          expected_value=IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP).verify_result()

    if hasattr(devices.dut, 'nvl_trunk_ports_list') and devices.dut.nvl_trunk_ports_list:
        with allure.step("Verify switch ports state - that are connected to transceivers"):
            verify_external_interfaces_state_up_and_active(devices, engine=engine)
