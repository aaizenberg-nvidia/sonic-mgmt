import logging
import time

import pytest

from ngts.nvos_constants.constants_nvos import ApiType, ActionConsts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.IbInterfaceTool import IbInterfaceTool
from ngts.nvos_tools.infra.MultiPlanarTool import MultiPlanarTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.mark.check_log_size
@pytest.mark.check_disk_usage
@pytest.mark.ib_interfaces
@pytest.mark.air
@pytest.mark.nvos_ci
def test_ib_interface_state(test_name, random_api, devices):
    """
    Configure port interface state and verify the configuration applied successfully
    Relevant cli commands:
    -	nv set interface <name> link state up/down
    -	nv show interface <name>

    flow:
    1. Select a random port (state of which is up)
    2. Set selected port state to 'down'
    3. Verify the configuration applied by running "show" command
    4. Set selected port state to 'up'
    5. Wait until the port is up
    6. Verify the configuration applied by running "show" command
    """
    selected_port = Tools.RandomizationTool.select_random_port().get_returned_value()
    TestToolkit.update_tested_ports([selected_port])
    try:
        set_port_state(selected_port, NvosConsts.LINK_STATE_DOWN, test_name, devices)

        output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_port.interface.link.show()).get_returned_value()

        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.LINK_STATE,
                                                          expected_value=NvosConsts.LINK_STATE_DOWN).verify_result()

    finally:
        set_port_state(selected_port, NvosConsts.LINK_STATE_UP, test_name, devices)

        output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_port.interface.link.show()).get_returned_value()

        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.LINK_STATE,
                                                          expected_value=NvosConsts.LINK_STATE_UP).verify_result()


def set_port_state(selected_port, port_state, test_name='', devices=None):
    selected_port.interface.link.state.set(op_param_name=port_state, apply=True, ask_for_confirmation=True).verify_result()
    wait_for_port_state(selected_port, port_state, test_name=test_name, devices=devices)


def wait_for_port_state(selected_port, port_state, logical_state=None, test_name='', devices=None):
    with allure.step("Wait till port {} is {}".format(selected_port, port_state)):
        res_obj, duration = OperationTime.save_duration('port goes {}'.format(port_state), '', test_name,
                                                        selected_port.interface.wait_for_port_state, port_state,
                                                        sleep_time=0.2, logical_state=logical_state)
        res_obj.verify_result()
        OperationTime.verify_operation_time(duration, 'port goes {}'.format(port_state), devices).verify_result()


@pytest.mark.ib_interfaces
def test_ib_interface_state_invalid(engines, random_api):
    """
    Configure port interface state using an invalid value
    Relevant cli commands:
    -	nv set interface <name> link state up/down
    -	nv show interface <name>

    flow:
    1. Select a random port (state of which is up)
    2. Set selected port state to invalid value -> should fail
    3. Verify the new value remain original in ConfigDB
    4. Verify the new value remain original in StateDB
    5. Verify the value remain original by running “show” command
    """
    selected_port = Tools.RandomizationTool.select_random_port().get_returned_value()

    TestToolkit.update_tested_ports([selected_port])

    selected_port.interface.link.state.set(op_param_name='invalid_value', apply=True,
                                           ask_for_confirmation=True).verify_result(False)

    output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
        selected_port.interface.link.show()).get_returned_value()

    Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                      field_name=IbInterfaceConsts.LINK_STATE,
                                                      expected_value=NvosConsts.LINK_STATE_UP).verify_result()


@pytest.mark.ib_interfaces
def test_ib_interface_state_unset(engines, random_api):
    """
    Configure port interface state using an invalid value
    Relevant cli commands:
    -	nv set interface <name> link state up/down
     -	nv unset interface <name> link state
    -	nv show interface <name>

    flow:
    1. Select a random port (state of which is up)
    2. 'Set selected port state to ‘up’
    3. Unset selected port state
    4. Wait until the port is up
    5. Verify the new value remain original in ConfigDB
    6. Verify the new value remain original in StateDB
    7. Verify the value remain original by running “show” command
    """
    selected_port = Tools.RandomizationTool.select_random_port().get_returned_value()

    TestToolkit.update_tested_ports([selected_port])

    selected_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_DOWN, apply=True,
                                           ask_for_confirmation=True).verify_result()

    selected_port.interface.link.state.unset(apply=True, ask_for_confirmation=True).verify_result()

    selected_port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP).verify_result()

    output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
        selected_port.interface.link.show()).get_returned_value()

    Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                      field_name=IbInterfaceConsts.LINK_STATE,
                                                      expected_value=NvosConsts.LINK_STATE_UP).verify_result()


@pytest.mark.dut_reboot
@pytest.mark.timeout(25 * MINUTE, func_only=True)
@pytest.mark.ib_interfaces
def test_ib_interface_state_up_once(engines, devices, random_api):
    """
    flow:
    1. Select a random port (state of which is up)
    2. Set selected port state to down
    3. Run nv action update fae interface <name> link state up-once and apply
    4. verify port state goes up after up-once
    5. Verify the port stays down after toggle event
    6. Verify the state is up after reboot
    """
    with allure.step('set up system objects'):
        selected_port = Tools.RandomizationTool.select_random_port().get_returned_value()
        port_name = selected_port.name
        TestToolkit.update_tested_ports([selected_port])
        fae = Fae(port_name=port_name)
        system = System()

    try:
        with allure.step(f'run nv set interface {port_name} link state down and apply'):
            set_port_state(selected_port, NvosConsts.LINK_STATE_DOWN, devices=devices)

        with allure.step(f'run nv action update fae interface {port_name} link state up-once and apply'):
            fae.interface.link.state.action(ActionConsts.UPDATE,
                                            (IbInterfaceConsts.INTERFACE_STATE, IbInterfaceConsts.UP_ONCE)
                                            ).verify_result()

        with allure.step('verify state is up after up-once'):
            selected_port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP, timeout=30).verify_result()

        with allure.step('verify state is down after port toggle event'):
            plane_port_name = (port_name if devices.dut.num_of_plane_ports == 1
                               else MultiPlanarTool.select_random_plane_port(fae).port.name)
            IbInterfaceTool.simulate_toggle_port_event(engines.dut, devices.dut, port_name=plane_port_name, sleep=5)
            logger.info(f'wait for 20 seconds to verify the port stays down after toggle event')
            time.sleep(20)
            selected_port.interface.wait_for_port_state(NvosConsts.LINK_STATE_DOWN).verify_result()

        with allure.step('verify state is up after reboot'):
            system.reboot.action_reboot(params='force')
            selected_port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP, timeout=60).verify_result()

    finally:
        with allure.step(f'run nv set interface {port_name} link state up and apply'):
            set_port_state(selected_port, NvosConsts.LINK_STATE_UP, devices=devices)


def verify_port_state(output_dictionary, expected_state):
    with allure.step(f'verify state is {expected_state} after port toggle failure'):
        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.LINK_STATE,
                                                          expected_value=expected_state).verify_result()
