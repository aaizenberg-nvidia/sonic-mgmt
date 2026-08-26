import math
import random
import re
import pytest
import logging

from retry.api import retry_call

from ngts.nvos_tools.Devices.IbDevice import JulietSwitch, JulietNonScaleoutSwitch, RosalindSurrogateSwitch, RosalindSwitch
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port, PortRequirements
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.RegressionConfigurations import Configurations
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.helpers.interfaces.link_state_helpers import get_all_interfaces_with_transceivers
from ngts.tools.test_utils.allure_utils import step as allure_step
from ngts.nvos_tools.ib.InterfaceConfiguration import nvos_consts as nv_consts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.ngts_types import DevicesT, EnginesT, LinkDiagnosticT
from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.infra.IbnetdiscoverTool import IbnetdiscoverTool
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


def show_interface_and_validate(engines, devices, ports_list, command=''):
    output_dictionary = OutputParsingTool.\
        parse_show_all_interfaces_output_to_dictionary(Port.show_interface(engines.dut, fae_param=command))\
        .get_returned_value()
    actual_ports = set(output_dictionary)
    expected_ports = set(ports_list)
    missing_ports = sorted(expected_ports - actual_ports)
    unexpected_ports = sorted(actual_ports - expected_ports)

    assert not missing_ports and not unexpected_ports, (
        f"Interface list mismatch: missing={missing_ports}, unexpected={unexpected_ports}"
    )


def toggle_port_state(selected_port, port_state, test_name, devices):
    selected_port.interface.link.state.set(
        op_param_name=port_state, apply=True, ask_for_confirmation=True,
    ).verify_result()
    is_acp_up = selected_port.name.startswith('acp') and port_state == nv_consts.NvosConsts.LINK_STATE_UP
    operation = nv_consts.InternalNvosConsts.ACP_PORT_GOES_UP if is_acp_up else f'port goes {port_state}'
    timeout_kwargs = {}
    if is_acp_up:
        acp_timeout = devices.dut.expected_operation_durations.get(nv_consts.InternalNvosConsts.ACP_PORT_GOES_UP)
        if acp_timeout:
            timeout_kwargs = {'timeout': acp_timeout}
    with allure_step(f"Wait till port {selected_port} is {port_state}"):
        res_obj, duration = OperationTime.save_duration(
            operation, '', test_name,
            selected_port.interface.wait_for_port_state, port_state,
            sleep_time=0.2, **timeout_kwargs,
        )
        res_obj.verify_result()
        OperationTime.verify_operation_time(duration, operation, devices).verify_result()


def validate_ports_state_and_speed(speed, expected_ports: list, prefix: str, state=nv_consts.NvosConsts.LINK_STATE_UP):
    port_requirements = PortRequirements()
    port_requirements.set_port_speed(speed)
    port_requirements.set_port_state(state)
    actual_ports = [port.name for port in Port.get_list_of_ports(port_requirements_object=port_requirements) if port.name.startswith(prefix)]

    ValidationTool.validate_subset_in_superset(expected_ports, actual_ports).verify_result()


def acp_link_up_timeout(devices) -> int:
    """Device budget for an acp port to link up (Rosalind/NVL6: 375s, Juliet/NVL5: 30s)."""
    return devices.dut.expected_operation_durations.get(nv_consts.InternalNvosConsts.ACP_PORT_GOES_UP,
                                                        nv_consts.InternalNvosConsts.DEFAULT_TIMEOUT)


def group_link_up_timeout(devices, prefix: str, state=nv_consts.NvosConsts.LINK_STATE_UP) -> int:
    """Budget for a port group to reach state. Only acp ports going up link up slowly."""
    if prefix != 'acp' or state != nv_consts.NvosConsts.LINK_STATE_UP:
        return nv_consts.InternalNvosConsts.DEFAULT_TIMEOUT
    return acp_link_up_timeout(devices)


def wait_for_ports_state_and_speed(devices, speed, expected_ports: list, prefix: str, delay: int = 15):
    """Wait for ports to reach speed and up state, within the port type's link-up budget."""
    retry_call(validate_ports_state_and_speed, [speed, expected_ports, prefix], exceptions=AssertionError,
               tries=math.ceil(group_link_up_timeout(devices, prefix) / delay) + 1, delay=delay)


def wait_for_ports_state(devices, expected_ports: list, prefix: str,
                         state=nv_consts.NvosConsts.LINK_STATE_UP, delay: int = 15):
    """Wait for ports to reach state, within the same budget as wait_for_ports_state_and_speed."""
    retry_call(validate_ports_state, [expected_ports, prefix, state], exceptions=AssertionError,
               tries=math.ceil(group_link_up_timeout(devices, prefix, state) / delay) + 1, delay=delay)


def validate_ports_state(expected_ports: list, prefix: str, state=nv_consts.NvosConsts.LINK_STATE_UP):
    port_requirements = PortRequirements()
    port_requirements.set_port_state(state)
    actual_ports = [port.name for port in Port.get_list_of_ports(port_requirements_object=port_requirements) if port.name.startswith(prefix)]

    ValidationTool.validate_subset_in_superset(expected_ports, actual_ports).verify_result()


def select_random_nvl_port_name(devices, prefix=None):
    with allure.step(f"Select {devices.dut.nvl_port_type} port in up state"):
        port_names = [port.name for port in
                      RandomizationTool.select_random_ports(requested_ports_type=devices.dut.nvl_port_type,
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


def skip_if_no_access_links(has_loopbox, standalone_system, is_simx):
    if not is_simx and not has_loopbox and standalone_system:
        pytest.skip("Skipping test - no connected access ports")


def reset_gpus_if_needed(setup_name):
    if setup_name in Configurations.non_standalone_systems:
        with allure.step(f"Reset the GPUs on non standalone_system: {setup_name}"):
            ClusterTools.reboot_compute_nodes_gpus(setup_name)


def is_qtm3_device(devices: DevicesT) -> bool:
    return isinstance(devices.dut, JulietSwitch) and devices.dut.asic_type in [NvosConst.QTM3, NvosConst.NVL5]


def is_qtm4_device(devices: DevicesT) -> bool:
    return isinstance(devices.dut, RosalindSurrogateSwitch) and devices.dut.asic_type in [NvosConst.QTM4, NvosConst.NVL6]


def is_nvl_device(devices: DevicesT) -> bool:
    return is_qtm3_device(devices) or is_qtm4_device(devices)


def skip_if_fec_measure_not_supported(devices: DevicesT) -> None:
    if isinstance(devices.dut, RosalindSwitch) and devices.dut.asic_type in [NvosConst.QTM4, NvosConst.NVL6]:
        return
    pytest.skip("fec-measure-mode is not supported on this device (requires QTM4/NVL6 Rosalind)")


def verify_link_diagnostic(
    ports: list[Port],
    expected_link_diagnostic_status: LinkDiagnosticT = nv_consts.IbInterfaceConsts.LINK_DIAGNOSTICS_WITHOUT_ISSUE_PORT,
    additional_link_diagnostic_status: LinkDiagnosticT | list[LinkDiagnosticT] | None = None,
) -> None:
    expected_link_diagnostic_status: LinkDiagnosticT = expected_link_diagnostic_status.copy()
    if additional_link_diagnostic_status:
        if isinstance(additional_link_diagnostic_status, dict):
            additional_link_diagnostic_status = [additional_link_diagnostic_status]
        for additional in additional_link_diagnostic_status:
            expected_link_diagnostic_status.update(additional)

    for port in ports:
        port_diagnostics = port.interface.link.diagnostics.parse_show()
        assert all(
            expected_link_diagnostic_status.get(code) == status
            for code, status in port_diagnostics.items()
        ), (
            f"Port {port.name} diagnostics value is {port_diagnostics} "
            f"- expected one of {expected_link_diagnostic_status}"
        )


def get_linked_ports_pair(devices: DevicesT, engines: EnginesT) -> tuple[str, str]:
    with allure.step("Get a pair of linked port names"):
        # Select an up acp port; its link partner is necessarily up too, so only
        # the source needs to be picked by state.
        src_name = select_random_nvl_port_name(devices, prefix='acp')

        # Build acp_name -> remote_acp_name from the topology. ibnetdiscover only lists
        # ports that currently have a link, so the relation already excludes down ports.
        # ibnetdiscover omits unlinked ports, so len(ports) varies per run and would
        # corrupt the index. Access ports are port_num 1..ports_per_switch; the trailing
        # XDR ports (73, 74) are inter-switch links. The global acp index is
        # acp{port_num + ports_per_switch * (order - 1)} to match nvl_access_ports_list.
        switches_list = IbnetdiscoverTool.run_ibnetdiscover(engines)
        assert switches_list, "ibnetdiscover returned no switches - expected the IB topology to be discoverable"
        ports_per_switch = len(devices.dut.nvl_access_ports_list) // len(switches_list)
        acp_name_by_switch_port: dict[tuple[str, int], str] = {}
        for switch in switches_list:
            for port in switch['ports']:
                if 1 <= port['port_num'] <= ports_per_switch:
                    acp_name = f"acp{port['port_num'] + (ports_per_switch * (switch['order'] - 1))}"
                    acp_name_by_switch_port[(switch['switch_guid'], port['port_num'])] = acp_name

        remote_by_acp: dict[str, str] = {}
        for switch in switches_list:
            for port in switch['ports']:
                src = acp_name_by_switch_port.get((switch['switch_guid'], port['port_num']))
                dst = acp_name_by_switch_port.get((port['remote_switch_guid'], port['remote_port_num']))
                if src is not None and dst is not None:
                    remote_by_acp[src] = dst

        assert src_name in remote_by_acp, (
            f"Selected up port {src_name} value is not in the ibnetdiscover topology "
            f"{list(remote_by_acp)} - expected the up acp port to have a discovered link partner"
        )
        dst_name = remote_by_acp[src_name]

        assert src_name in devices.dut.nvl_access_ports_list, (
            f"Source port {src_name} value is not in {devices.dut.nvl_access_ports_list} "
            f"- expected a valid access port name"
        )
        assert dst_name in devices.dut.nvl_access_ports_list, (
            f"Destination port {dst_name} value is not in {devices.dut.nvl_access_ports_list} "
            f"- expected a valid access port name"
        )
        logger.info("Linked ports pair: %s <-> %s", src_name, dst_name)
        return (src_name, dst_name)


def setup_nvl_speed(devices, exclude_speeds=None, required=False):
    """
    Set NVL port(s) to a random speed different from the current one.

    Args:
        devices: Devices fixture.
        port: Target port object. If None, applies to all access ports (bulk mode).
        exclude_speeds: Speeds to exclude from selection (e.g. ['200G']).
        required: If True, pytest.skip when no speed change is possible.
                  If False, return None.

    Returns:
        A (new_speed, port_obj, original_speed, port_names) tuple for restore_nvl_speed,
        or None if no change was made.
    """
    if hasattr(devices.dut, 'get_configurable_nvl_speeds'):
        supported_speeds = devices.dut.get_configurable_nvl_speeds()
    else:
        supported_speeds = getattr(devices.dut, 'supported_nvl_speeds', [])
    if not supported_speeds:
        if required:
            pytest.skip("No configurable NVL speeds available on device")
        return None

    candidates = [s for s in supported_speeds if s not in (exclude_speeds or [])]

    port_names = getattr(devices.dut, 'nvl_access_ports_list', [])
    if not port_names:
        if required:
            pytest.skip("No access ports available")
        return None
    original_speed = getattr(devices.dut, 'access_port_speed', '400G')

    candidates = [s for s in candidates if s != original_speed]
    port_obj = Port(f'acp1-{len(port_names)}')
    if not candidates:
        if required:
            logger.error(f"No speed change possible (current: {original_speed}, excluded: {exclude_speeds})")
        return (original_speed, port_obj, original_speed, port_names)

    new_speed = random.choice(candidates)

    with allure.step(f"Set all access ports to {new_speed}"):
        port_obj.interface.link.set(
            op_param_name='speed', op_param_value=new_speed,
            ask_for_confirmation=True, apply=True
        ).verify_result()
        prefix = re.match(r'[a-zA-Z]+', port_names[0]).group() if port_names else 'acp'
        wait_for_ports_state_and_speed(devices, new_speed, port_names, prefix)

    logger.info(f"Speed changed from {original_speed} to {new_speed}")
    return (new_speed, port_obj, original_speed, port_names)


def restore_nvl_speed(devices, speed_info):
    """Restore NVL port speed to default."""
    if speed_info is None:
        return
    _, ports_obj, default_speed, port_names = speed_info
    prefix = re.match(r'[a-zA-Z]+', port_names[0]).group() if port_names else 'acp'
    with allure.step(f"Restore ports to default speed {default_speed}"):
        ports_obj.interface.link.unset(
            op_param='speed', apply=True, ask_for_confirmation=True
        ).verify_result()
        wait_for_ports_state_and_speed(devices, default_speed, port_names, prefix)
