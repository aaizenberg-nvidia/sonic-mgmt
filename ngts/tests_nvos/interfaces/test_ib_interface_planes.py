import logging
import re
import time
from typing import Dict, List

import pytest
from retry import retry
from ngts.nvos_constants.constants_nvos import ApiType, EventConsts
from ngts.nvos_tools.infra.RegressionConfigurations import RegressionLinks
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts, IbInterfaceConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port, PortRequirements
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()

# Constants for mlxlink commands
MLXLINK_ADMIN_DOWN = "DN"
MLXLINK_ADMIN_UP = "UP"
# mlxlink state mappings
MLXLINK_STATE_ACTIVE = "Active"
MLXLINK_STATE_DISABLED = "Disabled"

# Event table query settings
EVENT_HISTORY_COUNT = 10

# Retry settings for state verification
STATE_CHECK_RETRIES = 3
STATE_CHECK_DELAY = 2  # seconds between retries
STATE_CHANGE_TIMEOUT = 30  # seconds to wait for state change
STATE_POLL_INTERVAL = 2  # seconds between polls


def get_mst_device_for_plane(port_name: str, plane_num: int) -> str:
    """
    Get the MST device for a specific plane port using 'nv show fae interface'.

    Args:
        port_name: Aggregated port name (e.g., 'swA15p2')
        plane_num: Plane number (1-based)

    Returns:
        str: MST device path (e.g., '/dev/mst/mt54004_pciconf0')
    """
    plane_port_name = f"{port_name}pl{plane_num}"

    # Get primary-asic-device directly from fae interface show
    output_dict = OutputParsingTool.parse_show_interface_output_to_dictionary(
        Port.show_interface(fae_param='fae', port_names=plane_port_name)).get_returned_value()
    mst_dev_name = output_dict[IbInterfaceConsts.PRIMARY_ASIC_DEVICE]

    logger.info(f"MST device for {plane_port_name}: {mst_dev_name}")
    return mst_dev_name


def get_mst_devices_for_ports(port_names: List[str]) -> Dict[str, str]:
    """
    Pre-fetch MST devices for given ports.

    Args:
        port_names: List of port names

    Returns:
        dict: Mapping of port_name to MST device path
    """
    mst_devices = {}
    with allure.step('Pre-fetch MST devices selected ports'):
        for port_name in port_names:
            # MST device is the same for all planes of a port, so just get plane 1
            mst_devices[port_name] = get_mst_device_for_plane(port_name, 1)
    logger.info(f"Pre-fetched MST devices for {len(mst_devices)} ports")
    return mst_devices


def build_mlxlink_port_param(port_name: str, plane_num: int) -> str:
    """
    Build the mlxlink -p parameter from port name and plane number.

    For port swA15p2 and plane 1: returns "15/2/1"

    Args:
        port_name: Port name (e.g., 'swA15p2')
        plane_num: Plane number (1-based)

    Returns:
        str: Port parameter for mlxlink (e.g., "15/2/1")
    """
    asic_letter, interface_num, local_port, split_num, _ = Port.parse_port_name(port_name)
    return f"{interface_num}/{local_port}/{plane_num}"


def set_plane_state(engine, port_name: str, plane_num: int, admin_state: str,
                    mst_devices: Dict[str, str] = None):
    """
    Set the state of a specific plane using mlxlink.

    Args:
        engine: DUT engine
        port_name: Aggregated port name (e.g., 'swA15p2')
        plane_num: Plane number (1-based)
        admin_state: 'DN' for down, 'UP' for up
        mst_devices: Optional pre-fetched MST devices cache (port_name -> mst_device)
    """
    if mst_devices and port_name in mst_devices:
        mst_device = mst_devices[port_name]
    else:
        mst_device = get_mst_device_for_plane(port_name, plane_num)
    port_param = build_mlxlink_port_param(port_name, plane_num)

    cmd = f"sudo mlxlink -d {mst_device} -p {port_param} -a {admin_state}"
    logger.info(f"Running mlxlink command: {cmd}")

    with allure.step(f"Set plane {plane_num} of {port_name} to {admin_state}"):
        result = engine.run_cmd(cmd)
        logger.info(f"mlxlink output: {result}")

    return result


def wait_for_plane_state(port_name: str, plane_num: int, expected_state: str,
                         timeout: int = STATE_CHANGE_TIMEOUT) -> str:
    """
    Wait for a plane to reach the expected state.

    Args:
        port_name: Aggregated port name (e.g., 'swA15p2')
        plane_num: Plane number (1-based)
        expected_state: Expected state ('up' or 'down')
        timeout: Maximum time to wait in seconds

    Returns:
        str: Final state

    Raises:
        AssertionError: If state doesn't match after timeout
    """
    plane_port_name = f"{port_name}pl{plane_num}"
    start_time = time.time()
    last_state = None

    while time.time() - start_time < timeout:
        last_state = get_plane_state_via_nv_show(port_name, plane_num)
        if last_state == expected_state:
            logger.info(f"Plane {plane_port_name} reached state '{expected_state}' "
                        f"after {time.time() - start_time:.1f}s")
            return last_state
        time.sleep(STATE_POLL_INTERVAL)

    raise AssertionError(
        f"Plane {plane_port_name} did not reach state '{expected_state}' "
        f"within {timeout}s (current state: '{last_state}')"
    )


@retry(tries=STATE_CHECK_RETRIES, delay=STATE_CHECK_DELAY)
def get_plane_state_via_nv_show(port_name: str, plane_num: int) -> str:
    """
    Get the state of a specific plane port using 'nv show interface'.

    Args:
        port_name: Aggregated port name (e.g., 'swA15p2')
        plane_num: Plane number (1-based)

    Returns:
        str: State ('up' or 'down')
    """
    plane_port_name = f"{port_name}pl{plane_num}"
    plane_port = Fae(port_name=plane_port_name).port

    output_dict = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
        plane_port.interface.link.show()).get_returned_value()

    return output_dict[IbInterfaceConsts.LINK_STATE]


def get_plane_state_via_mlxlink(engine, port_name: str, plane_num: int,
                                mst_devices: Dict[str, str] = None) -> str:
    """
    Get the state of a specific plane port using 'mlxlink' query command.

    Args:
        engine: DUT engine
        port_name: Aggregated port name (e.g., 'swA15p2')
        plane_num: Plane number (1-based)
        mst_devices: Optional pre-fetched MST devices cache (port_name -> mst_device)

    Returns:
        str: State ('up' or 'down') - normalized from mlxlink output
    """
    if mst_devices and port_name in mst_devices:
        mst_device = mst_devices[port_name]
    else:
        mst_device = get_mst_device_for_plane(port_name, plane_num)
    port_param = build_mlxlink_port_param(port_name, plane_num)

    cmd = f"sudo mlxlink -d {mst_device} -p {port_param}"
    logger.info(f"Querying mlxlink state: {cmd}")

    output = engine.run_cmd(cmd)
    logger.info(f"mlxlink query output: {output}")

    # Parse the State field from mlxlink output
    # Example: "State                           : Active" or "State                           : Disabled"
    state_match = re.search(r'State\s*:\s*(\S+)', output)
    if state_match:
        mlxlink_state = state_match.group(1)
        logger.info(f"mlxlink state for plane {plane_num}: {mlxlink_state}")
        # Normalize to 'up' or 'down'
        if mlxlink_state == MLXLINK_STATE_ACTIVE:
            return NvosConsts.LINK_STATE_UP
        else:
            return NvosConsts.LINK_STATE_DOWN
    else:
        raise AssertionError(f"Could not parse State from mlxlink output: {output}")


def get_plane_events_from_table(port_name: str, plane_num: int, expected_state: str) -> List[Dict]:
    """
    Get events from the system event table for a specific plane port.

    Note: Events are logged for the aggregated port (e.g., 'swA15p2'), not the plane port.

    Args:
        port_name: Aggregated port name (e.g., 'swA15p2')
        plane_num: Plane number (1-based)
        expected_state: Expected state to look for ('up' or 'down')

    Returns:
        list: List of matching events
    """
    system = System()

    # Get recent events
    events_output = system.events.show(f'--last {EVENT_HISTORY_COUNT}')
    events_dict = OutputParsingTool.parse_json_str_to_dictionary(events_output).get_returned_value()

    matching_events = []
    state_text = "down" if expected_state == 'down' else "up"

    for event_id, event in events_dict.items():
        if isinstance(event, dict):
            resource = event.get(EventConsts.RESOURCE, '')
            text = event.get(EventConsts.TEXT, '')

            # Check if this event is for our aggregated port and matches the expected state
            # Events are for aggregated port, not plane port
            if resource == port_name and state_text in text.lower():
                matching_events.append({
                    EventConsts.RESOURCE: resource,
                    EventConsts.TEXT: text,
                    EventConsts.TIME_CREATED: event.get(EventConsts.TIME_CREATED, ''),
                    EventConsts.SEVERITY: event.get(EventConsts.SEVERITY, '')
                })
                logger.info(f"Found matching event: {event_id} - {resource}: {text}")

    return matching_events


def verify_plane_state_all_methods(engine, port_name: str, plane_num: int, expected_state: str,
                                   check_events: bool = True, mst_devices: Dict[str, str] = None) -> Dict[str, str]:
    """
    Verify plane state using all 3 methods:
    1. nv show interface
    2. mlxlink query
    3. Event table (optional)

    Args:
        engine: DUT engine
        port_name: Aggregated port name (e.g., 'swA15p2')
        plane_num: Plane number (1-based)
        expected_state: Expected state ('up' or 'down')
        check_events: Whether to check the event table
        mst_devices: Optional pre-fetched MST devices cache

    Returns:
        dict: Results from all verification methods
    """
    plane_port_name = f"{port_name}pl{plane_num}"
    results = {}

    with allure.step(f'Verify plane {plane_port_name} is {expected_state.upper()} using all methods'):

        with allure.step(f'Method 1: Check via nv show interface (with wait for state change)'):
            nv_state = wait_for_plane_state(port_name, plane_num, expected_state)
            results['nv_show'] = nv_state
            logger.info(f"[nv show] Plane {plane_port_name} state: {nv_state}")
            assert nv_state == expected_state, \
                f"[nv show] Plane {plane_port_name} expected '{expected_state}' but got '{nv_state}'"

        with allure.step(f'Method 2: Check via mlxlink query'):
            mlxlink_state = get_plane_state_via_mlxlink(engine, port_name, plane_num, mst_devices)
            results['mlxlink'] = mlxlink_state
            logger.info(f"[mlxlink] Plane {plane_port_name} state: {mlxlink_state}")
            assert mlxlink_state == expected_state, \
                f"[mlxlink] Plane {plane_port_name} expected '{expected_state}' but got '{mlxlink_state}'"

        # Note: Events are logged for aggregated port, not individual plane ports
        if check_events:
            with allure.step(f'Method 3: Check event table for state change'):
                logger.info(f"[events] Searching for events: port={port_name}, state={expected_state}")
                events = get_plane_events_from_table(port_name, plane_num, expected_state)
                results['events'] = events
                assert len(events) > 0, \
                    f"[events] No events found for {port_name} state change to '{expected_state}'"
                logger.info(f"[events] ✓ Found {len(events)} matching events for aggregated port {port_name}")
                for evt in events:
                    logger.info(f"  Event: {evt[EventConsts.TEXT]}")

    logger.info(f"✓ Verification passed for {plane_port_name} = {expected_state}")
    return results


def verify_planes_up(port_name: str, num_of_planes: int, exclude_plane: int = None,
                     step_description: str = None) -> Dict[str, str]:
    """
    Verify planes are UP, optionally excluding one plane from the check.

    Args:
        port_name: Aggregated port name (e.g., 'swA15p2')
        num_of_planes: Total number of planes
        exclude_plane: Plane number to skip (None means check all planes)
        step_description: Optional description for the allure step

    Returns:
        dict: Mapping of plane port name to its state
    """
    if exclude_plane is not None:
        default_desc = f'Verify other planes are still UP on {port_name}'
    else:
        default_desc = f'Verify all planes of {port_name} are UP'
    step_desc = step_description or default_desc

    plane_states = {}

    with allure.step(step_desc):
        for plane_num in range(1, num_of_planes + 1):
            if exclude_plane is not None and plane_num == exclude_plane:
                continue
            plane_port_name = f"{port_name}pl{plane_num}"
            state = get_plane_state_via_nv_show(port_name, plane_num)
            plane_states[plane_port_name] = state

            assert state == 'up', \
                f"Plane {plane_port_name} should be UP but is '{state}'"
            logger.info(f"✓ Plane {plane_port_name} is UP")

    return plane_states


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_interface_plane_status_change(engines, devices, setup_name, test_api):
    """
    Test setting individual planes down/up using mlxlink and verifies interface plane states for connected loopback ports.

    Flow:
    1. Choose interface port that is XDR, UP, and connected in loopback mode
    2. Check state of all planes for the chosen port - verify all UP
    3. Find connected port and check state of all its planes - verify all UP
    4. For each plane:
       a. Set plane DOWN using mlxlink
       b. Verify the plane is DOWN
       c. Verify other planes are still UP
       d. Verify corresponding plane on connected port is also DOWN
       e. Set plane back UP
       f. Verify all planes are UP again
    """
    port_requirements = PortRequirements()
    port_requirements.set_port_ib_speed(IbInterfaceConsts.XDR)

    selected_up_ports = Tools.RandomizationTool.select_random_ports(requested_ports_state=NvosConsts.LINK_STATE_UP,
                                                                    requested_ports_type=devices.dut.switch_type.lower(),
                                                                    port_requirements_object=port_requirements,
                                                                    num_of_ports_to_select=0).get_returned_value()
    with allure.step('Get ports connected to each others'):
        ports_connected = RegressionLinks.get_filtered_ports_list(setup_name=setup_name, is_loopback=True)
        assert ports_connected, 'Connected in loopback ports not found'

    with allure.step('Pick random port from ports_connected that are UP'):
        up_port_names = {p.name for p in selected_up_ports}
        ports_connected_and_up = {k: v for k, v in ports_connected.items()
                                  if k in up_port_names and v in up_port_names}
        assert ports_connected_and_up, 'No UP ports connected in loopback found'

        random_port_name = Tools.RandomizationTool.select_random_value(ports_connected_and_up).get_returned_value()
        random_port = next(p for p in selected_up_ports if p.name == random_port_name)
        connected_port_name = ports_connected_and_up[random_port_name]
        random_port_connected = next(p for p in selected_up_ports if p.name == connected_port_name)

        # Verify we have two different ports
        assert random_port.name != random_port_connected.name, \
            "Selected port and connected port should be different"

    num_of_planes = devices.dut.num_of_plane_ports
    logger.info(f"Selected port: {random_port.name}, Connected port: {random_port_connected.name}")
    logger.info(f"Number of planes: {num_of_planes}")

    # Pre-fetch MST devices for both ports to avoid repeated lookups
    mst_devices = get_mst_devices_for_ports([random_port.name, random_port_connected.name])

    verify_planes_up(random_port.name, num_of_planes,
                     step_description=f'Verify all planes of chosen port {random_port.name} are UP initially')
    verify_planes_up(random_port_connected.name, num_of_planes,
                     step_description=f'Verify all planes of connected port {random_port_connected.name} are UP initially')

    try:
        with allure.step(f'Test all planes of {random_port.name} ; for each plane set down, verify, set back up'):
            for plane_num in range(1, num_of_planes + 1):
                with allure.independent_step(f'Test plane {plane_num} of {random_port.name}'):

                    with allure.step(f'Set plane {plane_num} DOWN using mlxlink'):
                        set_plane_state(engines.dut, random_port.name, plane_num, MLXLINK_ADMIN_DOWN, mst_devices)

                    with allure.step(f'Verify plane {plane_num} is DOWN on {random_port.name} (all methods)'):
                        verify_plane_state_all_methods(
                            engines.dut, random_port.name, plane_num,
                            expected_state='down', check_events=True, mst_devices=mst_devices
                        )

                    verify_planes_up(random_port.name, num_of_planes, exclude_plane=plane_num)

                    with allure.step(f'Verify plane {plane_num} is DOWN on connected port {random_port_connected.name} (all methods)'):
                        verify_plane_state_all_methods(
                            engines.dut, random_port_connected.name, plane_num,
                            expected_state='down', check_events=True, mst_devices=mst_devices
                        )

                    verify_planes_up(random_port_connected.name, num_of_planes, exclude_plane=plane_num)

                    with allure.step(f'Set plane {plane_num} back UP using mlxlink'):
                        set_plane_state(engines.dut, random_port.name, plane_num, MLXLINK_ADMIN_UP, mst_devices)

                    with allure.step(f'Verify plane {plane_num} is UP after restore (all methods)'):
                        verify_plane_state_all_methods(
                            engines.dut, random_port.name, plane_num,
                            expected_state='up', check_events=True, mst_devices=mst_devices
                        )
                        verify_plane_state_all_methods(
                            engines.dut, random_port_connected.name, plane_num,
                            expected_state='up', check_events=True, mst_devices=mst_devices
                        )

                    verify_planes_up(random_port.name, num_of_planes, exclude_plane=plane_num,
                                     step_description=f'Verify other planes still UP on {random_port.name} after restore')
                    verify_planes_up(random_port_connected.name, num_of_planes, exclude_plane=plane_num,
                                     step_description=f'Verify other planes still UP on {random_port_connected.name} after restore')

                    logger.info(f"✓ Plane {plane_num} test completed successfully")

    finally:
        with allure.step('Cleanup: Restore all planes to UP state'):
            for plane_num in range(1, num_of_planes + 1):
                try:
                    set_plane_state(engines.dut, random_port.name, plane_num, MLXLINK_ADMIN_UP, mst_devices)
                except Exception as e:
                    logger.warning(f"Failed to restore plane {plane_num}: {e}")
