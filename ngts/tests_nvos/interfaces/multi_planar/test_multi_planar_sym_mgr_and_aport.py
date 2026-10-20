from abc import ABC
import pytest
import logging
import random
import time

# from ngts.cli_wrappers.common.general_clis_common import GeneralCliCommon
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import ApiType, IbConsts, MultiPlanarConsts, LogsSources
from ngts.nvos_tools.ib.Ib import Ib
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.DatabaseTool import DatabaseTool
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.MultiPlanarTool import MultiPlanarTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.interfaces.test_ib_interface_state import wait_for_port_state
from ngts.tools.test_utils import allure_utils as allure
# from ngts.tests_nvos.interfaces.test_ib_interface_counters import test_ib_clear_counters, test_clear_all_counters

logger = logging.getLogger()


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.nvos_ci
def test_internal_fnm_ports(devices):
    """
    nv show fae interfaces --> Validate that all internal FNM ports that should be always up - are up.
    """
    output_dictionary = OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
        Port.show_interface(fae_param='fae')).get_returned_value()

    down_internal_fnm_ports = {port: output_dictionary[port][IbInterfaceConsts.LINK_STATE]
                               for port in devices.dut.interface_active_internal_fnm_ports
                               if output_dictionary[port][IbInterfaceConsts.LINK_STATE] != NvosConsts.LINK_STATE_UP}
    assert not down_internal_fnm_ports


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.simx_xdr
def test_fae_interface_commands(engines, devices, random_api, start_sm, setup_name):
    """
    validate all show fae interface commands.

    Test flow:
    1. Validate show fae interface
    2. Validate show fae interface <interface-id>
    3. Validate show fae interface <interface-id> link
    4. Validate show fae interface <interface-id> link counters
    5. Validate show fae interface <interface-id> link diagnostics
    6. Validate show fae interface <interface-id> link state
    7. Validate show fae interface <interface-id> link plane-ports
    8. Validate set fae interface <interface-id> link lanes <1X/2X/4X>
    9. Validate unset fae interface <interface-id> link lanes
    """

    dut_device = devices.dut

    with allure.step("Select random ports"):
        with allure.step("Select a random aggregated port and plane"):
            selected_port, selected_fae_port, selected_fae_plane_port = MultiPlanarTool.select_random_port_and_plane(dut_device, setup_name)

        with (allure.step("Select random external fnm port and fnm plane port")):
            selected_fae_fnm_port, selected_fae_fnm_plane_port = (
                MultiPlanarTool.select_random_fnm_port_and_plane(devices.dut))

            with allure.step(f"Verify external FNM port is in connection_mode {IbInterfaceConsts.XDR}"):
                output_fae_fnm_port = OutputParsingTool.parse_show_interface_output_to_dictionary(
                    selected_fae_fnm_port.port.interface.show()).get_returned_value()
                if output_fae_fnm_port[IbInterfaceConsts.LINK][IbInterfaceConsts.LINK_CONNECTION_MODE] != IbInterfaceConsts.XDR:
                    Port(selected_fae_fnm_port.port.name).interface.link.set(
                        op_param_name=IbInterfaceConsts.LINK_CONNECTION_MODE, op_param_value=IbInterfaceConsts.XDR,
                        apply=True, ask_for_confirmation=True).verify_result()

    # ------------- show commands -------------------------------------------------------------

    with allure.step("'show' commands"):
        with allure.independent_step("Validate show interface command"):
            output_dictionary = OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
                Port.show_interface()).get_returned_value()
            output_keys = list(output_dictionary.keys())
            ValidationTool.compare_values(output_keys.sort(), dut_device.interface_list.sort()).verify_result()

        with allure.independent_step("Validate external FNM port speed"):
            output_dictionary = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                selected_fae_fnm_port.port.interface.link.show()).get_returned_value()
            if output_dictionary[IbInterfaceConsts.LINK_STATE] == NvosConsts.LINK_STATE_UP:
                assert output_dictionary[IbInterfaceConsts.LINK_SPEED] == dut_device.fnm_link_speed, \
                    f"External FNM port speed should be {dut_device.fnm_link_speed} instead of" \
                    f"{output_dictionary[IbInterfaceConsts.LINK_SPEED]}"

        with allure.independent_step("Validate internal FNM port speed"):
            output_dictionary = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                selected_fae_fnm_plane_port.port.interface.link.show()).get_returned_value()
            if output_dictionary[IbInterfaceConsts.LINK_STATE] == NvosConsts.LINK_STATE_UP:
                assert output_dictionary[IbInterfaceConsts.LINK_SPEED] == dut_device.fnm_internal_link_speed, \
                    f"Internal FNM port speed should be {dut_device.fnm_internal_link_speed} instead of" \
                    f"{output_dictionary[IbInterfaceConsts.LINK_SPEED]}"

        with allure.independent_step("Validate show fae interface command"):
            output_dictionary = OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
                Port.show_interface(fae_param='fae')).get_returned_value()
            output_keys = list(output_dictionary.keys())
            ValidationTool.compare_values(output_keys.sort(), dut_device.interface_fae_list.sort()).\
                verify_result()

        with allure.independent_step("Validate all multi planar fields exist in show fae interface <port>"):
            output_fae_port = OutputParsingTool.parse_show_interface_output_to_dictionary(
                selected_fae_plane_port.interface.show()).get_returned_value()
            fae_port_keys = list(output_fae_port.keys())
            ValidationTool.validate_all_values_exists_in_list(MultiPlanarConsts.MULTI_PLANAR_KEYS, fae_port_keys). \
                verify_result()

        with allure.independent_step("Validate show fae interface <port-id> command"):
            validate_mp_show_interface_commands(OutputParsingTool.parse_show_interface_output_to_dictionary,
                                                selected_port.interface.show,
                                                selected_fae_port.interface.show,
                                                selected_fae_plane_port.interface.show)

        with allure.independent_step("Validate show fae interface <port-id> link command"):
            validate_mp_show_interface_commands(OutputParsingTool.parse_show_interface_link_output_to_dictionary,
                                                selected_port.interface.link.show,
                                                selected_fae_port.interface.link.show,
                                                selected_fae_plane_port.interface.link.show)

        with allure.independent_step("Validate show fae interface <port-id> link counters command"):
            validate_mp_show_interface_commands(OutputParsingTool.parse_json_str_to_dictionary,
                                                selected_port.interface.counters.show,
                                                selected_fae_port.interface.counters.show,
                                                selected_fae_plane_port.interface.counters.show)

        with allure.independent_step("Validate show fae interface <port-id> link diagnostics command"):
            validate_mp_show_interface_commands(OutputParsingTool.parse_json_str_to_dictionary,
                                                selected_port.interface.link.diagnostics.show,
                                                selected_fae_port.interface.link.diagnostics.show,
                                                selected_fae_plane_port.interface.link.diagnostics.show)

        with allure.independent_step("Validate show fae interface <port-id> link state command"):
            validate_mp_show_interface_commands(OutputParsingTool.parse_json_str_to_dictionary,
                                                selected_port.interface.link.state.show,
                                                selected_fae_port.interface.link.state.show,
                                                selected_fae_plane_port.interface.link.state.show)

        with allure.independent_step("Validate show fae interface <port-id> plane-ports command"):
            output_fae_port = OutputParsingTool.parse_json_str_to_dictionary(
                selected_fae_port.interface.plane_ports.show()).get_returned_value()
            fae_port_plane_ports = list(output_fae_port.keys())
            for plane in range(dut_device.num_of_plane_ports):
                full_plane_name = selected_fae_port.name + 'pl' + str(plane + 1)
                assert full_plane_name in fae_port_plane_ports, \
                    f"{full_plane_name} not exists in aggregated port {output_fae_port.port.name} plane-ports"

        with allure.independent_step("Validate show fae interface internal and external fnm commands"):
            validate_mp_show_interface_commands(OutputParsingTool.parse_show_interface_output_to_dictionary,
                                                selected_port.interface.show,
                                                selected_fae_fnm_port.port.interface.show,
                                                selected_fae_fnm_plane_port.port.interface.show)

    # ------------- set/unset commands (Not in scope for upcoming release) -----------------
    # with allure.step("Validate set/unset fae interface of a non aggregated port"):
    #     validate_set_and_unset_fae_interface_link_lanes_command(selected_fae_port)
    #
    # with allure.step("Validate set/unset fae interface of fnm external port"):
    #     validate_set_and_unset_fae_interface_link_lanes_command(selected_fae_fnm_port)

    # ------------- action commands --------------------------------------------------------
    # tested on test_action_fae_clear_counters


# @pytest.mark.interfaces
# @pytest.mark.multiplanar
# @pytest.mark.simx_xdr
# @pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
# def test_action_fae_clear_counters(engines, players, interfaces, start_sm, test_api):
#     """
#     Validate fae action commands:
#         - nv action clear fae interface <interface-id> link counters
#         - nv action clear fae interface counters
#
#     Test flow:
#     1. run the existing "test_ib_clear_counters" with fae param.
#     2. run the existing "test_clear_all_counters" with fae param.
#     """
#
#     TestToolkit.tested_api = test_api
#
#     try:
#         with allure.step("Validate action clear fae interface <interface-id> link counters"):
#             test_ib_clear_counters(engines, players, interfaces, start_sm, fae_param="fae")
#
#         with allure.step("Validate action clear fae interface counters"):
#             test_clear_all_counters(engines, players, interfaces, start_sm, fae_param="fae")
#
#     finally:
#         with allure.step("set config to default"):
#             set_mp_config_to_default()


class AggregatedPortConfigBaseTest(ABC):
    LINK_PARAM = ''
    POSSIBLE_VALUES = tuple()

    @classmethod
    def test_config(cls, engines, devices, test_api, test_name, setup_name):
        TestToolkit.tested_api = test_api
        selected_aggregated_port, _, selected_fae_plane_port = MultiPlanarTool.select_random_port_and_plane(devices.dut, setup_name)
        try:
            new_value = cls.set_config(devices.dut, selected_aggregated_port, selected_fae_plane_port)
            cls.assert_aggregation(selected_aggregated_port, selected_fae_plane_port, new_value)
        finally:
            with allure.step("cleanup - unset config and wait for port to become active"):
                selected_aggregated_port.interface.link.unset(apply=True, ask_for_confirmation=True).verify_result()
                wait_for_port_state(selected_aggregated_port, NvosConsts.LINK_STATE_UP,
                                    NvosConsts.LINK_LOG_STATE_ACTIVE, test_name)

    @classmethod
    def get_possible_values(cls, device):
        if not cls.POSSIBLE_VALUES:
            raise NotImplementedError("Subclass must override either POSSIBLE_VALUES or choose_value()")
        return cls.POSSIBLE_VALUES

    @classmethod
    def set_config(cls, device, aggregated_port, plane_port):
        with allure.step('make config change'):
            aggregated_port_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                aggregated_port.interface.link.show()).get_returned_value()
            param_new_value = RandomizationTool.select_random_value(
                cls.get_possible_values(device), forbidden_values=[aggregated_port_output[cls.LINK_PARAM]]
            ).get_returned_value()
            aggregated_port.interface.link.set(op_param_name=cls.LINK_PARAM, op_param_value=param_new_value,
                                               apply=True, ask_for_confirmation=True).verify_result()
            logger.info(f"set port {aggregated_port.name} link param: {cls.LINK_PARAM} = {param_new_value}")
            time.sleep(MultiPlanarConsts.PORT_UPDATE_TIME)  # todo assert port goes up & active
            return param_new_value

    @classmethod
    def assert_aggregation(cls, aggregated_port, plane_port, expected_value):
        with allure.step('assert aggregation'):
            aggregated_port_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                aggregated_port.interface.link.show()).get_returned_value()
            plane_port_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                plane_port.interface.link.show()).get_returned_value()
            aggregated_value = aggregated_port_output[cls.LINK_PARAM]
            plane_value = plane_port_output[cls.LINK_PARAM]
            assert aggregated_value == plane_value == expected_value, \
                f"mismatch in {cls.LINK_PARAM}: {aggregated_value=}, {plane_value=}, {expected_value=}"


# todo: pending decision which speeds are actually supported by mamba
# class AggregatedPortConfigIbSpeedTest(AggregatedPortConfigBaseTest):
#     LINK_PARAM = IbInterfaceConsts.LINK_IB_SPEED
#
#     @classmethod
#     def get_possible_values(cls, device):
#         return device.supported_ib_speeds  # todo: only take speeds supported by the transceiver


class AggregatedPortConfigMtuTest(AggregatedPortConfigBaseTest):
    LINK_PARAM = IbInterfaceConsts.LINK_MTU
    POSSIBLE_VALUES = IbInterfaceConsts.MTU_VALUES


class AggregatedPortConfigOpVlsTest(AggregatedPortConfigBaseTest):
    LINK_PARAM = IbInterfaceConsts.LINK_OPERATIONAL_VLS
    POSSIBLE_VALUES = IbInterfaceConsts.SUPPORTED_VLS


class AggregatedPortConfigStateTest(AggregatedPortConfigBaseTest):
    LINK_PARAM = IbInterfaceConsts.LINK_STATE
    POSSIBLE_VALUES = (NvosConsts.LINK_STATE_UP, NvosConsts.LINK_STATE_DOWN)

    @classmethod
    def set_config(cls, device, aggregated_port, plane_port):
        new_state = NvosConsts.LINK_STATE_DOWN
        aggregated_port.interface.link.state.set(op_param_name=new_state, apply=True).verify_result()
        time.sleep(MultiPlanarConsts.PORT_UP_MAX_TIME)
        return new_state


# todo: pending decision which speeds are actually supported by mamba
# @pytest.mark.interfaces
# @pytest.mark.multiplanar
# @pytest.mark.simx_xdr
# @pytest.mark.parametrize('test_api', [ApiType.NVUE])
# def test_aggregated_port_config_ib_speed(engines, devices, start_sm, test_api, test_name):
#     AggregatedPortConfigIbSpeedTest.test_config(engines, devices, test_api, test_name)


@pytest.mark.timeout(5 * MINUTE)
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.simx_xdr
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_aggregated_port_config_op_vls(engines, devices, start_sm, test_api, test_name, setup_name):
    AggregatedPortConfigOpVlsTest.test_config(engines, devices, test_api, test_name, setup_name)


@pytest.mark.timeout(5 * MINUTE)
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.simx_xdr
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_aggregated_port_config_mtu(engines, devices, start_sm, test_api, test_name, setup_name):
    AggregatedPortConfigMtuTest.test_config(engines, devices, test_api, test_name, setup_name)


@pytest.mark.timeout(5 * MINUTE)
@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.simx_xdr
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_aggregated_port_config_state(engines, devices, start_sm, test_api, test_name, setup_name):
    AggregatedPortConfigStateTest.test_config(engines, devices, test_api, test_name, setup_name)


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.simx_xdr
def test_aggregated_port_mismatch_between_planes(engines, devices, random_api):
    """
    validate correct aggregation in Aport while there is a mismatch in fields values between port’s planes.

    Test flow:
    1. Validate ib-speed field mismatch aggregation
    2. Validate lanes field mismatch aggregation
    3. Validate mtu field mismatch aggregation
    4. Validate op-vls field mismatch aggregation
    5. Validate max-supported-mtu field mismatch aggregation
    6. Validate supported-ib-speed field mismatch aggregation
    7. Validate state field mismatch aggregation
    """
    pytest.skip('Test needs to be restructured and fixed (how can we change config for a single plane?)')

    dut_device = devices.dut

    try:
        with allure.step(f"Configure ports"):
            loop_back_name = RandomizationTool.select_random_value(dut_device.default_loopback_ports).\
                get_returned_value()
            loop_back_port = Port(loop_back_name)
            aggregated_port = Port(dut_device.default_aggregated_port)
            for port in dut_device.default_loopback_ports:
                if loop_back_name == port:
                    selected_plane_port = Fae(port_name=dut_device.loop_back_to_ports[port])
                else:
                    other_plane_port = Fae(port_name=dut_device.loop_back_to_ports[port])

        with allure.independent_step("Validate ib-speed mismatch aggregation"):
            new_value, aggregated_port_output, selected_plane_port_output, other_plane_port_output = \
                set_param_value_in_specific_plane(loop_back_port, aggregated_port, selected_plane_port,
                                                  other_plane_port, IbInterfaceConsts.LINK_IB_SPEED,
                                                  dut_device.supported_ib_speeds)

            assert selected_plane_port_output[IbInterfaceConsts.LINK_IB_SPEED] == new_value, \
                f"plane port {IbInterfaceConsts.LINK_IB_SPEED} value is: " \
                f"{selected_plane_port_output[IbInterfaceConsts.LINK_IB_SPEED]}, instead of: {new_value}"

            assert aggregated_port_output[IbInterfaceConsts.LINK_IB_SPEED] == 0, \
                f"aggregated port {IbInterfaceConsts.LINK_IB_SPEED} value is: " \
                f"{aggregated_port_output[IbInterfaceConsts.LINK_IB_SPEED]}, instead of: 0"

            assert aggregated_port_output[IbInterfaceConsts.LINK_STATE] == 'down', \
                f"aggregated port {IbInterfaceConsts.LINK_STATE} value is: " \
                f"{aggregated_port_output[IbInterfaceConsts.LINK_STATE]}, instead of: down"

        # with allure.step("Validate lanes mismatch aggregation"):
        # TODO: currently not supported by operational code - set fae interface command has not implemented yet

        with allure.independent_step("Validate mtu mismatch aggregation"):
            new_value, aggregated_port_output, selected_plane_port_output, other_plane_port_output = \
                set_param_value_in_specific_plane(loop_back_port, aggregated_port, selected_plane_port,
                                                  other_plane_port, IbInterfaceConsts.LINK_MTU,
                                                  IbInterfaceConsts.MTU_VALUES)

            assert selected_plane_port_output[IbInterfaceConsts.LINK_MTU] == new_value, \
                f"plane port {IbInterfaceConsts.LINK_MTU} value is: " \
                f"{selected_plane_port_output[IbInterfaceConsts.LINK_MTU]}, instead of: {new_value}"

            planes_min = min(int(new_value), int(other_plane_port_output[IbInterfaceConsts.LINK_MTU]))

            assert aggregated_port_output[IbInterfaceConsts.LINK_MTU] == planes_min, \
                f"aggregated port {IbInterfaceConsts.LINK_MTU} value is: " \
                f"{aggregated_port_output[IbInterfaceConsts.LINK_MTU]}, instead of: {planes_min}"

        with allure.independent_step("Validate op-vls mismatch aggregation"):
            new_value, aggregated_port_output, selected_plane_port_output, other_plane_port_output = \
                set_param_value_in_specific_plane(loop_back_port, aggregated_port, selected_plane_port,
                                                  other_plane_port, IbInterfaceConsts.LINK_OPERATIONAL_VLS,
                                                  IbInterfaceConsts.SUPPORTED_VLS)

            assert selected_plane_port_output[IbInterfaceConsts.LINK_OPERATIONAL_VLS] == new_value, \
                f"plane port {IbInterfaceConsts.LINK_OPERATIONAL_VLS} value is: " \
                f"{selected_plane_port_output[IbInterfaceConsts.LINK_OPERATIONAL_VLS]}, instead of: {new_value}"

            planes_min = min(int(new_value), int(other_plane_port_output[IbInterfaceConsts.LINK_OPERATIONAL_VLS]))

            assert aggregated_port_output[IbInterfaceConsts.LINK_OPERATIONAL_VLS] == planes_min, \
                f"aggregated port {IbInterfaceConsts.LINK_OPERATIONAL_VLS} value is: " \
                f"{aggregated_port_output[IbInterfaceConsts.LINK_OPERATIONAL_VLS]}, instead of: {planes_min}"

        # with allure.step("Validate max-supported-mtu mismatch aggregation"):
        # TODO: currently not supported by operational code - update STATE_DB values directly is not available

        # with allure.step("Validate supported-ib-speed mismatch aggregation"):
        # TODO: currently not supported by operational code - update STATE_DB values directly is not available

        with allure.independent_step("Validate state mismatch aggregation"):
            loop_back_port.interface.link.unset(apply=True, ask_for_confirmation=True).verify_result()
            loop_back_port.interface.link.state.set(op_param_value='down', apply=True, ask_for_confirmation=True).\
                verify_result()
            aggregated_port_state = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                aggregated_port.interface.link.state.show()).get_returned_value()
            selected_plane_port_state = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                selected_plane_port.port.interface.link.state.show()).get_returned_value()
            other_plane_port_state = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                other_plane_port.port.interface.link.state.show()).get_returned_value()

            assert selected_plane_port_state == 'down', \
                f"selected plane port {IbInterfaceConsts.LINK_STATE} value is: " \
                f"{selected_plane_port_state}, instead of: down"

            assert other_plane_port_state == 'up', \
                f"other plane port {IbInterfaceConsts.LINK_STATE} value is: " \
                f"{other_plane_port_state}, instead of: up"

            assert aggregated_port_state == 'down', \
                f"aggregated plane port {IbInterfaceConsts.LINK_STATE} value is: " \
                f"{aggregated_port_state}, instead of: down"

    finally:
        with allure.step("set config to default"):
            set_mp_config_to_default()


# @pytest.mark.interfaces
# @pytest.mark.multiplanar
# @pytest.mark.simx_xdr
# @pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
# def test_aggregated_port_physical_and_logical_state_machines(engines, devices, test_api):
#     """
#     validate Aport Physical state and Logical state aggregation according to the following rule priorities:
#     State Type  | Rule Priority | Logic                                                                    |APort State
#     --------------------------------------------------------------------------------------------------------------
#                 | 0             | Any underlying plane port is disabled                                    | Disabled
#     Physical    | 1             | Any underlying plane port is in Sleep state                              | Sleep
#                 | 2             | Any underlying plane port is in Polling state                            | Polling
#                 | 3             | Any underlying plane port are in LinkUp state                            | LinkUp
#     --------------------------------------------------------------------------------------------------------------
#                 | 0             | At least on underlying plane port is in Down state                       | Down
#     Logical     | 1             | Any ul plane port is in Init state AND no ul plane port is in Down state | Init
#                 | 2             | Any ul plane p is in Armed state AND no ul pp is in Down OR Init state   | Armed
#                 | 3             | All underlying plane ports are in Active state                           | Active
#     --------------------------------------------------------------------------------------------------------------
#
#     Test flow:
#     1. Validate physical state in all plane port combinations:
#         a.	plane1: disabled, plane2: disabled
#         b.	plane1: disabled, plane2: sleep
#         c.	plane1: disabled, plane2: polling
#         d.	plane1: disabled, plane2: linkup
#         e.	plane1: sleep   , plane2: sleep
#         f.	plane1: sleep   , plane2: polling
#         g.	plane1: sleep   , plane2: linkup
#         h.	plane1: polling , plane2: polling
#         i.	plane1: polling , plane2: linkup
#         j.	plane1: linkup  , plane2: linkup
#
#     2. Validate logical state in all plane port combinations:
#         a.	plane1: down  , plane2: down
#         b.	plane1: down  , plane2: init
#         c.	plane1: down  , plane2: armed
#         d.	plane1: down  , plane2: active
#         e.	plane1: init  , plane2: init
#         f.	plane1: init  , plane2: armed
#         g.	plane1: init  , plane2: active
#         h.	plane1: armed , plane2: armed
#         i.	plane1: armed , plane2: active
#         j.	plane1: active, plane2: active
#     """
#
#     TestToolkit.tested_api = test_api
#     engine = engines.dut
#
#     try:
#         with allure.step("Select a random aggregated port (connected in loop back to another port)"):
#             # selected_fae_aggregated_port = Fae(port_name=RandomizationTool.select_random_port(
#             #     requested_ports_logical_state=IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE).
#             #                                     get_returned_value().name)
#             selected_fae_aggregated_port = Fae(port_name='swA8p1')
#
#         with allure.step("Validate physical state aggregation - all combinations"):
#             for combine in MultiPlanarConsts.PHYSICAL_STATE_AGG_TABLE:
#                 validate_state_aggregation(engine, devices, selected_fae_aggregated_port,
#                                            MultiPlanarConsts.PHYSICAL_STATE_PARAM,
#                                            combine['p1'], combine['p2'], combine['exp'])
#
#         with allure.step("Validate logical state aggregation - all combinations"):
#             for combine in MultiPlanarConsts.LOGICAL_STATE_AGG_TABLE:
#                 validate_state_aggregation(engine, devices, selected_fae_aggregated_port,
#                                            MultiPlanarConsts.LOGICAL_STATE_PARAM,
#                                            combine['p1'], combine['p2'], combine['exp'])
#
#     finally:
#         with allure.step("set config to default"):
#             set_mp_config_to_default()


# @pytest.mark.interfaces
# @pytest.mark.multiplanar
# @pytest.mark.simx_xdr
# @pytest.mark.parametrize('test_api', [ApiType.NVUE])
# def test_symmetry_manager_resiliency(engines, devices, test_api):
#     """
#     validate:
#     -	Configuration of the aggregated port persists through reboot
#     -	The system recovers automatically after killing the symmetry manager docker.
#     -	No unexpected behavior (access violation, leak etc.) when processing malformed input
#         (e.g. malformed/missing config in DB)
#     -	System is still stable after causing an exception in Counter manager
#
#     Test flow:
#     1. Validate aggregated port configuration persists through reboot
#     2. Validate system recovery after docker kill
#     3. Remove sampled data from DB
#     """
#
#     TestToolkit.tested_api = test_api
#     dut_device = devices.dut
#     system = System(devices_dut=dut_device)
#
#     try:
#         with allure.step('Get a list of active ports'):
#             active_port_list = Port.get_list_of_active_ports()
#             assert active_port_list, "No active ports"
#             port_name_list = []
#             for port in active_port_list:
#                 port_name_list.append(port.name)
#
#         with allure.step("Select a random aggregated port"):
#             aggregated_active_list = list(set(port_name_list).intersection(dut_device.aggregated_port_list))
#             aggregated_port_name = RandomizationTool.select_random_value(aggregated_active_list). \
#                 get_returned_value()
#             selected_fae_aggregated_port = Fae(port_name=aggregated_port_name)
#             selected_aggregated_port = Port(selected_fae_aggregated_port.port.name)
#
#         with allure.step("Select a random plane port"):
#             selected_fae_plane_port = MultiPlanarTool.select_random_plane_port(devices, selected_fae_aggregated_port,
#                                                                                dut_device.num_of_plane_ports)
#
#         # Validate ib-speed field aggregation
#         validate_aggregation_of_specific_link_param(selected_aggregated_port, selected_fae_plane_port,
#                                                     IbInterfaceConsts.LINK_IB_SPEED,
#                                                     dut_device.supported_ib_speeds)
#
#         # # Validate mtu field aggregation
#         # validate_aggregation_of_specific_link_param(selected_aggregated_port, selected_fae_plane_port,
#         #                                             IbInterfaceConsts.LINK_MTU,
#         #                                             IbInterfaceConsts.MTU_VALUES)
#
#         with allure.step("Save aggregated port link output before reboot"):
#             output_before_reboot = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
#                 selected_aggregated_port.interface.link.show()).get_returned_value()
#
#         with allure.step("Perform system reboot"):
#             system.reboot.action_reboot(params='force').verify_result()
#
#         with allure.step("Save aggregated port link output after reboot"):
#             output_after_reboot = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
#                 selected_aggregated_port.interface.link.show()).get_returned_value()
#
#         assert output_before_reboot[IbInterfaceConsts.LINK_IB_SPEED] == \
#             output_after_reboot[IbInterfaceConsts.LINK_IB_SPEED],\
#             f"Aggregated port {IbInterfaceConsts.LINK_IB_SPEED} configuration did not persist after reboot," \
#             f"before: {output_before_reboot[IbInterfaceConsts.LINK_IB_SPEED]}, " \
#             f"after: {output_after_reboot[IbInterfaceConsts.LINK_IB_SPEED]}" \
#
#         assert output_before_reboot[IbInterfaceConsts.LINK_MTU] == \
#             output_after_reboot[IbInterfaceConsts.LINK_MTU],\
#             f"Aggregated port {IbInterfaceConsts.LINK_MTU} configuration did not persist after reboot," \
#             f"before: {output_before_reboot[IbInterfaceConsts.LINK_MTU]}, " \
#             f"after: {output_after_reboot[IbInterfaceConsts.LINK_MTU]}" \
#
#         # with allure.step(f"stop {MultiPlanarConsts.CONFIG_MANAGER_SERVICE} daemon"):
#         #     GeneralCliCommon(TestToolkit.engines.dut).systemctl_stop(MultiPlanarConsts.CONFIG_MANAGER_SERVICE)
#         #
#         # with allure.step(f"wait for {MultiPlanarConsts.SERVICE_RECOVERY_MAX_TIME} seconds..."):
#         #     time.sleep(MultiPlanarConsts.SERVICE_RECOVERY_MAX_TIME)
#         #
#         # with allure.step(f"verify {MultiPlanarConsts.CONFIG_MANAGER_SERVICE} daemon automatic recovery"):
#         #     if not GeneralCliCommon(TestToolkit.engines.dut).systemctl_is_service_active(
#         #             MultiPlanarConsts.CONFIG_MANAGER_SERVICE):
#         #         GeneralCliCommon(TestToolkit.engines.dut).systemctl_start(MultiPlanarConsts.CONFIG_MANAGER_SERVICE)
#         #         assert False, f"{MultiPlanarConsts.CONFIG_MANAGER_SERVICE} service automatic recovery failed"
#
#     finally:
#         with allure.step("set config to default"):
#             if active_port_list:
#                 selected_aggregated_port.interface.link.unset(apply=True, ask_for_confirmation=True).verify_result()


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.simx_xdr
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_symmetry_manager_log_and_tech_support(engines, devices, test_api, setup_name):
    """
    validate:
    - Configuring commands are logged to system log
    - Aport and port’s planes data based are stored in tech support

    Test flow:
    1. Validate symmetry manager log in system log
    2. Validate Aport and planes db exist in debug dump
    """

    TestToolkit.tested_api = test_api
    dut_device = devices.dut
    system = System(devices_dut=dut_device)

    with allure.step("Select random aggregated port and plane port"):
        selected_fae_aggregated_port = MultiPlanarTool.select_random_aggregated_port(dut_device, setup_name)
        selected_aggregated_port = Port(selected_fae_aggregated_port.port.name)
        selected_fae_plane_port = MultiPlanarTool.select_random_plane_port(selected_fae_aggregated_port,
                                                                           dut_device.num_of_plane_ports)

    try:
        with allure.step("Set fae interface link state and check log file"):
            system.log.rotate_logs()
            selected_aggregated_port.interface.link.state.set(op_param_name='down', apply=True).verify_result()
            system.log.verify_expected_logs([f"{MultiPlanarConsts.LOG_MSG_SET_FAE_INTERFACE}{selected_aggregated_port.name}"], logs_source=LogsSources.NVUED, engine=engines.dut)

        with allure.step("Unset fae interface link state and check log file"):
            system.log.rotate_logs()
            selected_aggregated_port.interface.link.state.unset(apply=True, ask_for_confirmation=True).\
                verify_result()
            system.log.verify_expected_logs(
                [f"{MultiPlanarConsts.LOG_MSG_SET_FAE_INTERFACE}{selected_aggregated_port.name}"],
                logs_source=LogsSources.NVUED, engine=engines.dut)

        with allure.step("Run action clear fae interface and check log file"):
            system.log.rotate_logs()
            selected_fae_plane_port.port.interface.link.stats.clear_stats(fae_param="fae").verify_result()
            system.log.verify_expected_logs([MultiPlanarConsts.LOG_MSG_ACTION_CLEAR_FAE_INTERFACE.format(port_name=selected_fae_plane_port.port.name)], logs_source=LogsSources.NVUED, engine=engines.dut)
            system.log.verify_expected_logs(["Cleared counters successfully"], only_latest_log=True, engine=engines.dut)

        with allure.step("Validate all asics database files exist in tech support file"):
            validate_mp_database_files_exist_in_techsupport(system, engines.dut)

    finally:
        with allure.step("set config to default"):
            selected_aggregated_port.interface.link.unset(apply=True, ask_for_confirmation=True).verify_result()


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.simx_xdr
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_fae_invalid_commands(engines, devices, test_api):
    """
    validate fae interface commands with invalid param values.

    Test flow:
    1. nv show fae interface <unknown interface-id>
    2. nv show fae interface <unknown interface-id> link
    3. nv show fae interface <unknown interface-id> counters
    4. nv show fae interface <unknown interface-id> link diagnostics
    5. nv show fae interface <unknown interface-id> link state
    6. nv show fae interface <unknown interface-id> plane-ports
    7. nv action clear fae interface <unknown interface-id> counters
    8. nv show interface <internal-fnm-id>
    """

    TestToolkit.tested_api = test_api

    with allure.step("Validate show fae interface with unknown interface-id"):
        Fae(port_name='unknown').port.interface.show(should_succeed=False)

    with allure.step("Validate show fae interface link with unknown interface-id"):
        Fae(port_name='unknown').port.interface.link.show(should_succeed=False)

    with allure.step("Validate show fae interface counters with unknown interface-id"):
        Fae(port_name='unknown').port.interface.counters.show(should_succeed=False)

    with allure.step("Validate show fae interface link diagnostics with unknown interface-id"):
        Fae(port_name='unknown').port.interface.link.diagnostics.show(should_succeed=False)

    with allure.step("Validate show fae interface link state with unknown interface-id"):
        Fae(port_name='unknown').port.interface.link.state.show(should_succeed=False)

    with allure.step("Validate show fae interface link plane-ports with unknown interface-id"):
        Fae(port_name='unknown').port.interface.link.plane_ports.show(should_succeed=False)

    with allure.step("Validate action clear fae interface counters command with unknown interface-id"):
        Fae(port_name='unknown').port.interface.counters.clear_counters(dut_engine=engines.dut, fae_param="fae").\
            verify_result(should_succeed=False)

    with allure.step("Validate show interface with internal fnm id"):
        fnm_internal_port_name = 'fnma1p236'
        Port(fnm_internal_port_name).interface.show(should_succeed=False)


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.simx_xdr
def test_verify_sm_commands_not_exist(engines, random_api):
    """
    Validate the following sm commands are not exist nor supported:
    nv show ib sm
    nv show ib sm log
    nv show ib sm log files
    nv show ib sm log files <file-name>
    nv set ib sm state (enabled|disabled)
    nv set ib sm sm-priority (0-15)
    nv set ib sm sm-sl (0-15)
    nv unset ib sm
    nv unset ib sm state
    nv unset ib sm sm-priority
    nv unset ib sm sm-sl

    Test flow:
    1. run 'nv list-commands | grep " sm" on dut
    2. check if any "sm" command exists
    3. validate all "sm" commands don't work.
    """
    engines_dut = engines.dut
    ib = Ib(None)

    with allure.step('verify "sm" commands not exist in commands list'):
        output = NvueGeneralCli.search_in_list_commands(engines_dut, " sm").replace('nv show interface small', '')
        assert not output, "sm commands should not exist"

    with allure.step("Validate show ib sm"):
        ib.sm.show(should_succeed=False)

    with allure.step("Validate show ib sm log"):
        ib.sm.log.show(should_succeed=False)

    with allure.step("Validate show ib sm log files"):
        ib.sm.log.show(IbConsts.FILES, should_succeed=False)

    with allure.step("Validate show ib sm log files <file-name>"):
        ib.sm.log.show(IbConsts.FILES + ' opensm.log', should_succeed=False)

    with allure.step("Validate set ib sm state enabled"):
        ib.sm.set(op_param_name=IbConsts.SM_STATE, op_param_value=IbConsts.SM_STATE_ENABLE,
                  apply=True, ask_for_confirmation=True).verify_result(False)

    with allure.step("Validate set ib sm state disabled"):
        ib.sm.set(op_param_name=IbConsts.SM_STATE, op_param_value=IbConsts.SM_STATE_DISABLE,
                  apply=True, ask_for_confirmation=True).verify_result(False)

    with allure.step("Validate set ib sm sm-priority"):
        priority_random_val = random.randint(1, 15)
        ib.sm.set(op_param_name=IbConsts.SM_PRIORITY, op_param_value=str(priority_random_val),
                  apply=True, ask_for_confirmation=True).verify_result(False)

    with allure.step("Validate set ib sm sm-sl"):
        sl_random_val = random.randint(1, 15)
        ib.sm.set(op_param_name=IbConsts.SM_SL, op_param_value=str(sl_random_val),
                  apply=True, ask_for_confirmation=True).verify_result(False)

    with allure.step("Validate unset ib sm command"):
        ib.sm.unset(apply=True, ask_for_confirmation=True).verify_result(False)

    with allure.step("Validate unset ib sm state command"):
        ib.sm.unset(op_param=IbConsts.SM_STATE, apply=True, ask_for_confirmation=True).verify_result(False)

    with allure.step("Validate unset ib sm sm-priority command"):
        ib.sm.unset(op_param=IbConsts.SM_PRIORITY, apply=True, ask_for_confirmation=True).verify_result(False)

    with allure.step("Validate unset ib sm sm-sl command"):
        ib.sm.unset(op_param=IbConsts.SM_SL, apply=True, ask_for_confirmation=True).verify_result(False)
# ---------------------------------------------


def validate_mp_show_interface_commands(parse_func, port_cmd, port_fae_cmd, pport_fae_cmd):
    with allure.step("Show interface of an aggregated port"):
        output_port = parse_func(port_cmd()).get_returned_value()
        port_keys = list(output_port.keys())
        if 'acl' in port_keys:
            port_keys.remove('acl')

    with allure.step("Show fae interface of an aggregated port"):
        output_fae_port = parse_func(port_fae_cmd()).get_returned_value()
        fae_port_keys = list(output_fae_port.keys())

    with allure.step("Show fae interface of a plane port"):
        output_fae_plane_port = parse_func(pport_fae_cmd()).get_returned_value()
        fae_plane_port_keys = list(output_fae_plane_port.keys())

    with allure.step("Validate all show interface <port> fields exist in show fae interface <port>"):
        ValidationTool.validate_all_values_exists_in_list(port_keys, fae_port_keys).verify_result()

    with allure.step("Compare between fae aggregated port and plane port show interface"):
        fae_plane_port_keys = list(set(fae_plane_port_keys) - set(MultiPlanarConsts.MULTI_PLANAR_KEYS))
        ValidationTool.compare_values(fae_port_keys.sort(), fae_plane_port_keys.sort()).verify_result()


def set_param_value_in_specific_plane(loop_back_port, aggregated_port, selected_plane_port,
                                      other_plane_port, param, param_list):
    with allure.step(f"Change {param} field value in {loop_back_port.name} port"):
        loop_back_port.interface.link.unset(apply=True, ask_for_confirmation=True).verify_result()
        loop_back_port_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            loop_back_port.interface.link.show()).get_returned_value()
        param_list.remove(loop_back_port_output[param])
        param_new_value = RandomizationTool.select_random_value(param_list).get_returned_value()
        loop_back_port.interface.link.set(op_param_name=param, op_param_value=param_new_value, apply=True,
                                          ask_for_confirmation=True).verify_result()
        aggregated_port_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            aggregated_port.interface.link.show()).get_returned_value()
        selected_plane_port_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_plane_port.port.interface.link.show()).get_returned_value()
        other_plane_port_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            other_plane_port.port.interface.link.show()).get_returned_value()

        return param_new_value, aggregated_port_output, selected_plane_port_output, other_plane_port_output


def validate_state_aggregation(engine, devices, aggregated_port, param, value0, value1, expected_value):
    dut_device = devices.dut
    with allure.step(f"Update asic0 {param} state to: {value0} and asic1 {param} state to: {value1}"):
        DatabaseTool.sonic_db_cli_hset(engine, dut_device.asic0, dut_device.counters_db_name,
                                       dut_device.object_numbers[aggregated_port.port.name]['plane1'],
                                       param, value0)
        DatabaseTool.sonic_db_cli_hset(engine, dut_device.asic1, dut_device.counters_db_name,
                                       dut_device.object_numbers[aggregated_port.port.name]['plane2'],
                                       param, value1)

    with allure.step(f"wait {MultiPlanarConsts.SYNC_TIME} secs for sync"):
        time.sleep(MultiPlanarConsts.SYNC_TIME)

    with allure.step(f"Validate {param} field aggregation"):
        aggregated_port_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            aggregated_port.port.interface.link.show()).get_returned_value()
        assert aggregated_port_output[param] == expected_value, \
            f"aggregated port {param} is {aggregated_port_output[param]}, instead of {expected_value}"


def validate_mp_database_files_exist_in_techsupport(system, engine):
    """
    generate techsupport and validate all asics database files exist in the dump dir
    """
    try:
        tech_support_folder, duration = system.techsupport.action_generate(engine=engine)
        logger.info("The techsupport file name is : " + tech_support_folder)
        system.techsupport.extract_techsupport_files(engine)
        techsupport_files_list = system.techsupport.get_techsupport_files_list(engine, 'dump')
        for db_table in MultiPlanarConsts.DATABASE_TABLES:
            assert "{}.json".format(db_table) in techsupport_files_list, \
                "Expect to have {}.json file, in the tech support dump files {}".format(db_table, techsupport_files_list)
            assert "{}.json.0".format(db_table) in techsupport_files_list, \
                "Expect to have {}.json file, in the tech support dump files {}".format(db_table, techsupport_files_list)
    finally:
        system.techsupport.cleanup(engine)
        if system.techsupport.file_name:
            system.techsupport.files.file_name[system.techsupport.file_name].action_delete()


def validate_set_and_unset_fae_interface_link_lanes_command(selected_fae_port):
    with allure.step(f"Validate set fae interface {selected_fae_port.port.name} link lanes command"):
        output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_fae_port.port.interface.link.show()).get_returned_value()
        lanes_list = IbInterfaceConsts.SUPPORTED_LANES - output[IbInterfaceConsts.LINK_LANES]
        new_lanes = RandomizationTool.select_random_value(lanes_list).get_returned_value()
        selected_fae_port.port.interface.link.set(op_param_name=IbInterfaceConsts.LINK_LANES,
                                                  op_param_value=new_lanes, apply=True).verify_result()
        output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_fae_port.port.interface.link.show()).get_returned_value()
        assert output[IbInterfaceConsts.LINK_LANES] == new_lanes, \
            f"{IbInterfaceConsts.LINK_LANES} value is {output[IbInterfaceConsts.LINK_LANES]}," \
            f"instead of {new_lanes}"

    with allure.step(f"Validate unset fae interface {selected_fae_port.port.name} link lanes command"):
        selected_fae_port.port.interface.link.unset(op_param_name=IbInterfaceConsts.LINK_LANES,
                                                    apply=True).verify_result()
        output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_fae_port.port.interface.link.show()).get_returned_value()
        assert output[IbInterfaceConsts.LINK_LANES] == IbInterfaceConsts.DEFAULT_LANES, \
            f"{IbInterfaceConsts.LINK_LANES} value is {output[IbInterfaceConsts.LINK_LANES]}," \
            f"instead of {IbInterfaceConsts.DEFAULT_LANES}"


def set_mp_config_to_default():
    logger.info("TBD")
