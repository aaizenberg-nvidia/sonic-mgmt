import random
import pytest

from ngts.nvos_tools.Devices.IbDevice import JulietSwitch, JulietNonScaleoutSwitch
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port, PortRequirements
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.RegressionConfigurations import Configurations
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import FWRecoveryConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.helpers.interfaces.link_state_helpers import get_all_interfaces_with_transceivers
from ngts.tools.test_utils.allure_utils import step as allure_step
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.tools.test_utils import allure_utils as allure


def show_interface_and_validate(engines, devices, ports_list, command=''):
    output_dictionary = OutputParsingTool.\
        parse_show_all_interfaces_output_to_dictionary(Port.show_interface(engines.dut, fae_param=command))\
        .get_returned_value()
    output_keys = list(output_dictionary.keys())
    ValidationTool.compare_values(output_keys.sort(), ports_list.sort()).verify_result()


def toggle_port_state(selected_port, port_state, test_name=''):
    selected_port.interface.link.state.set(op_param_name=port_state, apply=True, ask_for_confirmation=True).verify_result()
    with allure_step("Wait till port {} is {}".format(selected_port, port_state)):
        res_obj, duration = OperationTime.save_duration('port goes {}'.format(port_state), '', test_name,
                                                        selected_port.interface.wait_for_port_state, port_state,
                                                        sleep_time=0.2)
        res_obj.verify_result()
        OperationTime.verify_operation_time(duration, 'port goes {}'.format(port_state)).verify_result()


def validate_ports_state_and_speed(speed, expected_ports: list, prefix: str, state=NvosConsts.LINK_STATE_UP):
    port_requirements = PortRequirements()
    port_requirements.set_port_speed(speed)
    port_requirements.set_port_state(state)
    actual_ports = [port.name for port in Port.get_list_of_ports(port_requirements_object=port_requirements) if port.name.startswith(prefix)]

    ValidationTool.validate_subset_in_superset(expected_ports, actual_ports).verify_result()


def validate_mode_set(selected_port, config, mode: str, timeout: int = None):
    with allure.step(f"Validate mode {mode} is applied"):
        output = OutputParsingTool.parse_json_str_to_dictionary(
            selected_port.port.interface.link.phy_recovery.show()
        ).get_returned_value()
        mode = FWRecoveryConsts.DISABLED if mode == FWRecoveryConsts.FW_DEFAULT else mode
        ValidationTool.compare_values(output[config["mode"]], mode).verify_result()
        if timeout:
            ValidationTool.compare_values(int(output[config["timeout"]]), timeout).verify_result()


def validate_default_config(selected_port):
    with allure_step("Check default values"):
        output_fae_port = OutputParsingTool.parse_json_str_to_dictionary(
            selected_port.port.interface.link.phy_recovery.show()).get_returned_value()
        ValidationTool.compare_dictionaries(output_fae_port, FWRecoveryConsts.DEFAULT_PHY_RECOVERY_DICT).verify_result()


def select_random_nvl_port_name(devices, prefix=None):
    with allure.step(f"Select {devices.dut.nvl5_port_type} port in up state"):
        port_names = [port.name for port in
                      RandomizationTool.select_random_ports(requested_ports_type=devices.dut.nvl5_port_type,
                                                            num_of_ports_to_select=0).get_returned_value()]
        if prefix:
            with allure.step(f"filter by prefix: {prefix}"):
                port_names = [port for port in port_names if port.startswith(prefix)]

        return random.choice(port_names)


def skip_if_no_trunk_links(devices):
    if isinstance(devices.dut, JulietSwitch):
        has_any_connected_transceivers = bool(get_all_interfaces_with_transceivers(devices))
        if isinstance(devices.dut, JulietNonScaleoutSwitch) or not has_any_connected_transceivers:
            pytest.skip("Skipping test - no connected trunk ports")


def skip_if_no_access_links(has_loopbox, standalone_system):
    if not has_loopbox and standalone_system:
        pytest.skip("Skipping test - no connected access ports")


def reset_gpus_if_needed(setup_name):
    if setup_name in Configurations.non_standalone_systems:
        with allure.step(f"Reset the GPUs on non standalone_system: {setup_name}"):
            ClusterTools.reboot_compute_nodes_gpus(setup_name)
