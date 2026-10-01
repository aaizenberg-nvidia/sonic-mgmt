import logging
import time
from typing import Union, Dict, Tuple, List

import pytest

from ngts.nvos_constants.constants_nvos import ActionConsts, NvosConst
from ngts.nvos_tools.Devices.IbDevice import JulietSwitch
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts, PhyDetailConsts
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.IbInterfaceTool import IbInterfaceTool
from ngts.nvos_tools.infra.MultiPlanarTool import MultiPlanarTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.RegressionConfigurations import RegressionLinks
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.interfaces.nvl_port.helpers import skip_if_no_trunk_links
from ngts.tools.test_utils import allure_utils as allure

LOCAL = 'local'
REMOTE = 'remote'
REASON_CODE_FIELD = {side: f'linkdown-reason-code-{side}' for side in (LOCAL, REMOTE)}
REASON_STATUS_FIELD = {side: f'linkdown-reason-status-{side}' for side in (LOCAL, REMOTE)}
INTENTIONAL_LINK_DOWN_EVENTS = 'intentional-link-down-events'
UNINTENTIONAL_LINK_DOWN_EVENTS = 'unintentional-link-down-events'
PLANE_SPLITTER = '/'
INTENTIONAL_LINK_DOWN_LOCAL_CODE = 22
INTENTIONAL_LINK_DOWN_REMOTE_CODE = 33
LINK_DOWN_PREI_CODE = 4
LINK_DOWN_UNKNOWN_CODE = 1

# source: PRM Rev 1.55.205, section 9.17.7 PUDE - Port Up/Down Event, table 1684 - PUDE - Port Up/Down Event Fields
CODE_TO_DESCRIPTION = {
    0: "No_link_down_indication", 1: "Unknown_reason", 2: "Hi_SER_or_Hi_BER", 3: "Block_Lock_loss", 4: "Alignment_loss",
    5: "FEC_sync_loss", 6: "PLL_lock_loss", 7: "FIFO_overflow", 8: "false_SKIP_condition",
    9: "Minor_Error_threshold_exceeded", 10: "Physical_layer_retransmission_timeout", 11: "Heartbeat_errors",
    12: "Link_Layer_credit_monitoring_watchdog", 13: "Link_Layer_integrity_threshold_exceeded",
    14: "Link_Layer_buffer_overrun", 15: "Down_by_outband_command_with_healthy_link",
    16: "Down_by_outband_command_for_link_with_hi_ber", 17: "Down_by_inband_command_with_healthy_link",
    18: "Down_by_inband_command_for_link_with_hi_ber", 19: "Down_by_verification_GW", 20: "Received_Remote_Fault",
    21: "Received_TS1", 22: "Down_by_management_command", 23: "Cable_was_unplugged", 24: "Cable_access_issue",
    25: "Cable_Thermal_shutdown", 26: "Current_issue", 27: "Power_budget", 28: "Fast_recovery_raw_ber",
    29: "Fast_recovery_effective_ber", 30: "Fast_recovery_symbol_ber", 31: "Fast_recovery_credit_watchdog",
    32: "Peer_side_down_to_sleep_state", 33: "Peer_side_down_to_disable_state",
    34: "Peer_side_down_to_disable_and_port_lock", 35: "Peer_side_down_due_to_thermal_event",
    36: "Peer_side_down_due_to_force_event", 37: "Peer_side_down_due_to_reset_event", 38: "Reset_no_power_cycle",
    39: "Fast_recovery_tx_plr_trigger", 40: "Down_due_to_HW_force_event", 41: "Down_due_to_thermal_event",
    42: "L1_exit_failure", 43: "too_many_link_error_recoveries", 44: "Down_due_to_contain_mode",
    45: "BW_loss_threshold_exceeded", 46: "ELS_laser_fault",
}

logger = logging.getLogger()


@pytest.mark.ib_interfaces
def test_show_phy_detail(engines, test_api, output_format):
    """Checks that `nv show interface <port> link phy detail` returns non-empty output in a valid format."""
    selected_port = RandomizationTool.select_random_port(requested_ports_state=None).get_returned_value()
    output = selected_port.interface.link.phy.detail.show(output_format=output_format)
    d = OutputParsingTool.parse_show_output_to_dict(output, output_format).get_returned_value()
    assert len(d) > 1


@pytest.mark.ib_interfaces
def test_phy_detail_attribute_types(engines, devices, test_api):
    """
    Verify attribute types in 'nv show interface <port> link phy detail' output match ASIC generation.

    Test flow:
    1. Determine ASIC generation (QTM3 or QTM4+)
    2. Select a random port (any state - works on Juliet NVL and Crocodile IB ports)
    3. Run 'nv show interface <port> link phy detail'
    4. Verify each attribute's type matches expected type for the ASIC generation

    Note: QTM3 group includes both QTM3 and NVL5 chip types
    """
    with allure.step("Determine ASIC generation and expected attribute types"):
        asic_type = getattr(devices.dut, 'asic_type', 'unknown')
        is_qtm3 = asic_type in [NvosConst.QTM3, NvosConst.NVL5]
        expected_types = PhyDetailConsts.ATTR_TYPES_QTM3 if is_qtm3 else PhyDetailConsts.ATTR_TYPES_QTM4_AND_NEWER
        asic_gen = "QTM3 (includes NVL5)" if is_qtm3 else "QTM4 and newer"
        logger.info(f"ASIC type: {asic_type}, Generation: {asic_gen}")

    with allure.step("Select random port"):
        selected_port = RandomizationTool.select_random_port(requested_ports_state=None).get_returned_value()
        logger.info(f"Selected port: {selected_port.name}")

    with allure.step(f"Run 'nv show interface {selected_port.name} link phy detail'"):
        output = selected_port.interface.link.phy.detail.show()
        phy_detail_output = OutputParsingTool.parse_show_output_to_dict(output).get_returned_value()
        logger.info(f"PHY detail output has {len(phy_detail_output)} fields")

    with allure.step("Verify attribute types"):
        validate_phy_attribute_types(phy_detail_output, expected_types, is_qtm3, asic_gen)

    # For QTM4+ ASICs, verify certain attributes should NOT exist at all
    if not is_qtm3:
        with allure.step("Verify QTM4+ non-existent attributes are absent"):
            validate_qtm4_non_existent_attributes(phy_detail_output, asic_gen)


@pytest.mark.ib_interfaces
def test_no_logging_flood_on_port_state_change(engines, devices, nv_command):
    """
    Regression test to verify that port state changes don't cause excessive logging (flooding).

    Test flow:
    1. Select a random IB port
    2. Rotate syslog to start with a clean log
    3. Set port down and verify write message count is within acceptable threshold
    4. Set port up and verify write message count is within acceptable threshold

    Expected behavior after fix (per operation):
    - 9 writes for initialization: '0/0/0/0' state
    - 4 writes for plane transitions: '22/0/0/0' → '22/22/0/0' → '22/22/22/0' → '22/22/22/22'
    - Total: ~13 writes per DOWN or UP operation

    Before fix (regression indicator):
    - Port down operation: 60-70+ messages (severe flood)
    - Port up operation: 60-70+ messages (severe flood)
    """
    MAX_WRITE_MESSAGES_PER_OPERATION = 15  # 13 expected + buffer for timing variations

    skip_if_no_trunk_links(devices)

    with allure.step("Select random port"):
        selected_port: Port = RandomizationTool.select_random_port().get_returned_value()
        port_name = selected_port.name
        logger.info(f"Testing port {port_name} for logging flood")

    try:
        with allure.step("Rotate syslog to start with clean logs"):
            nv_command.system.log.rotate_logs()

        with allure.step("Set port DOWN and check for logging flood"):
            selected_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_DOWN, apply=True).verify_result()
            time.sleep(3)  # Allow state change to propagate and logs to be written

            down_write_count = _count_port_write_messages(engines.dut, port_name, devices.dut)
            logger.info(f"Port {port_name} DOWN operation generated {down_write_count} write messages")

            with allure.step(f"Assert write count ({down_write_count}) is within acceptable threshold"):
                assert down_write_count > 0, (
                    f"Port DOWN operation generated 0 write messages. "
                    f"Expected at least 1 message - check if grep pattern is matching correctly."
                )
                assert down_write_count <= MAX_WRITE_MESSAGES_PER_OPERATION, (
                    f"Port DOWN operation generated {down_write_count} write messages, "
                    f"exceeding threshold of {MAX_WRITE_MESSAGES_PER_OPERATION}. "
                    f"This indicates a logging flood issue!"
                )

        with allure.step("Rotate syslog again before UP operation"):
            nv_command.system.log.rotate_logs()

        with allure.step("Set port UP and check for logging flood"):
            selected_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_UP, apply=True).verify_result()
            time.sleep(10)  # Allow state change to propagate and logs to be written

            up_write_count = _count_port_write_messages(engines.dut, port_name, devices.dut)
            logger.info(f"Port {port_name} UP operation generated {up_write_count} write messages")

            with allure.step(f"Assert write count ({up_write_count}) is within acceptable threshold"):
                assert up_write_count > 0, (
                    f"Port UP operation generated 0 write messages. "
                    f"Expected at least 1 message - check if grep pattern is matching correctly."
                )
                assert up_write_count <= MAX_WRITE_MESSAGES_PER_OPERATION, (
                    f"Port UP operation generated {up_write_count} write messages, "
                    f"exceeding threshold of {MAX_WRITE_MESSAGES_PER_OPERATION}. "
                    f"This indicates a logging flood issue!"
                )
    finally:
        with allure.step("Cleanup: Ensure port is set back to UP"):
            selected_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_UP, apply=True).verify_result()


def _count_port_write_messages(engine, port_name: str, device) -> int:
    """
    Counts the number of portsyncmgrd "Handling op SET key <port>" messages in syslog for the specified port.

    Uses grep to search syslog for the specific pattern that indicates a SET operation to the port.
    Uses the device-specific port-to-Infiniband conversion method to get the base Infiniband port name,
    which matches all planes (e.g., Infiniband260 matches Infiniband260pl1, pl2, pl3, pl4).

    Args:
        engine: The device engine to execute commands on
        port_name: Port name (e.g., "swA5p1", "sw72p2", "swB14p1")
        device: The device object with convert_port_to_infiniband method

    Returns:
        int: Number of write messages found in syslog
    """
    # Use device-specific conversion method
    if hasattr(device, 'convert_port_to_infiniband'):
        ib_port_name = device.convert_port_to_infiniband(port_name)
    else:
        logger.warning(f"Device does not have convert_port_to_infiniband method, using port name as-is")
        ib_port_name = port_name

    # Search pattern: "portsyncmgrd: Handling op SET key <port_name>" in syslog
    # Note: We use the base port name (e.g., Infiniband260) to match all planes (pl1, pl2, pl3, pl4)
    # Note: grep -c returns exit code 1 when no matches found, so we use '|| true' to avoid command failure
    # and take only the first line of output (the count)
    grep_count_command = f'grep -c "Handling op SET key {ib_port_name}" /var/log/syslog || true'
    grep_messages_command = f'grep "Handling op SET key {ib_port_name}" /var/log/syslog | head -20 || true'

    try:
        result = engine.run_cmd(grep_count_command)
        # Take only the first line in case of multiple outputs, default to 0 if empty
        first_line = result.strip().split('\n')[0] if result.strip() else '0'
        count = int(first_line) if first_line.isdigit() else 0
        logger.info(f"Found {count} 'Handling op SET key' messages for port {ib_port_name} (original: {port_name})")

        # Log the actual messages found (limited to first 20 to avoid log flood)
        if count > 0:
            messages = engine.run_cmd(grep_messages_command)
            logger.info(f"SET key messages for port {ib_port_name} (first 20):\n{messages}")

        return count
    except (ValueError, AttributeError) as e:
        logger.error(f"Failed to parse write message count: {e}, result: {result}")
        return 0


@pytest.mark.ib_interfaces
def test_intentional_link_down_counter(engines, devices):
    """
    Test for `nv show interface <port> link phy detail` intentional-link-down-events field. Flow:
    1.  Get intentional-link-down-events and unintentional-link-down-events counters for a port
    2.  Set port down
    3.  Assert the intentional- counter increased
    4.  Assert the unintentional- counter did not increase
    """
    skip_if_no_trunk_links(devices)
    with allure.step("Get initial values"):
        selected_port: Port = RandomizationTool.select_random_port().get_returned_value()
        initial_intentional, initial_unintentional = get_counters(selected_port)

    selected_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_DOWN, apply=True).verify_result()
    time.sleep(5)

    with allure.step("Get final values"):
        intentional, unintentional = get_counters(selected_port)

    with allure.step("Assert intentional counter increased"):
        # The counter aggregates all planes so the increase amount should equal the number of planes
        assert intentional > initial_intentional, (
            f"Expected the {INTENTIONAL_LINK_DOWN_EVENTS} counter to increase, but the old value is "
            f"{initial_intentional} and the new value is {intentional}"
        )
    with allure.step("Assert unintentional counter did not change"):
        assert unintentional == initial_unintentional, (
            f"Expected the {UNINTENTIONAL_LINK_DOWN_EVENTS} counter not to change, but the old value is "
            f"{initial_unintentional} and the new value is {unintentional}"
        )


@pytest.mark.ib_interfaces
def test_unintentional_link_down_counter(engines, devices, enable_asic_error_injection):
    """
    Test for `nv show interface <port> link phy detail` unintentional-link-down-events field. Flow:
    1.  Get intentional-link-down-events and unintentional-link-down-events counters for a port
    2.  Simulate link drop
    3.  Assert the unintentional- counter increased
    4.  Assert the intentional- counter did not increase
    """
    skip_if_no_trunk_links(devices)
    with allure.step("Get initial values"):
        selected_port: Port = RandomizationTool.select_random_port(interface_type='sw').get_returned_value()
        initial_intentional, initial_unintentional = get_counters(selected_port)

    port_name = (selected_port.name if isinstance(devices.dut, JulietSwitch) else
                 MultiPlanarTool.select_random_plane_port(Fae(port_name=selected_port.name)).port.name)
    IbInterfaceTool.simulate_toggle_port_event(engines.dut, devices.dut, port_name)

    with allure.step("Get final values"):
        intentional, unintentional = get_counters(selected_port)

    with allure.step("Assert unintentional counter increased"):
        assert unintentional == initial_unintentional + 1, (
            f"Expected the {UNINTENTIONAL_LINK_DOWN_EVENTS} counter to increase by 1, but the old value is "
            f"{initial_unintentional} and the new value is {unintentional}"
        )
    with allure.step("Assert intentional counter did not change"):
        assert intentional == initial_intentional, (
            f"Expected the {INTENTIONAL_LINK_DOWN_EVENTS} counter not to change, but the old value is "
            f"{initial_intentional} and the new value is {intentional}"
        )


@pytest.mark.ib_interfaces
def test_link_down_reason(engines, devices, setup_name, enable_asic_error_injection):
    """
    Test the 'link-down-code' field (and related fields) under `nv show interface <port> link phy detail`.
    Flow:
        1. Choose a random loopback port ("test port") and some other connected port.
        2. Set the test-port down, then set it back up.
        3. Assert the test-port's link-down-reason has the value it's expected to have after the user set the link down.
        4. Assert also for the other end of the same loopback connection (the "remote port").
        5. Simulate link error on the test-port, and wait for it to recover.
        6. Assert the test-port's local & remote link-down reasons are as expected after a link drop.
        7. Assert also for the remote port.
        8. Assert that the other port (selected in stage 1) link-down-reason has not changed during the test.
    """
    skip_if_no_trunk_links(devices)
    tested_port, tested_plane, remote_port, remote_plane, other_port = _get_test_ports(engines.dut, devices.dut)
    # todo: the above line will be replaced by the following once the required port selection functions are implemented
    # tested_port, [tested_plane], remote_port, [remote_plane] = get_loopback_plane_ports(engines.dut, setup_name)
    # _, other_port = RandomizationTool.get_random_transceiver_and_port(
    #     engines.dut, setup_name, is_loopback=True,
    #     forbidden_transceivers=[tested_port.get_transceiver_name(), remote_port.get_transceiver_name()])
    # other_port = Port(other_port)
    with allure.step("Set port down & up to reset the link-down reason"):
        tested_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_DOWN, apply=True).verify_result()
        tested_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_UP, apply=True).verify_result()
        tested_port.interface.wait_for_port_state(state=NvosConsts.LINK_STATE_UP)

    with allure.step("Check initial link down reasons"):
        tested_port_initial_codes = get_codes(tested_port)
        remote_port_initial_codes = get_codes(remote_port)
        other_port_initial_codes = get_codes(other_port)
        assert_reason_all_planes(tested_port, LOCAL, INTENTIONAL_LINK_DOWN_LOCAL_CODE)
        assert_reason_all_planes(tested_port, REMOTE, INTENTIONAL_LINK_DOWN_REMOTE_CODE)
        assert_reason_all_planes(remote_port, LOCAL, INTENTIONAL_LINK_DOWN_REMOTE_CODE)
        assert_reason_all_planes(remote_port, REMOTE, INTENTIONAL_LINK_DOWN_LOCAL_CODE)

    with allure.step("Simulate link error and wait for link to return to up state"):
        IbInterfaceTool.simulate_toggle_port_event(engines.dut, devices.dut, tested_plane.name)
        tested_port.interface.wait_for_port_state(state=NvosConsts.LINK_STATE_UP)

    with allure.step("Check link-down reasons"):
        assert_reason_for_plane(tested_plane, LINK_DOWN_PREI_CODE, tested_port_initial_codes)
        assert_reason_for_plane(remote_plane, LINK_DOWN_PREI_CODE, remote_port_initial_codes)
        assert_reason_not_changed(other_port, other_port_initial_codes)


def _get_test_ports(engine, device) -> Tuple[Port, Port, Port, Port, Port]:
    # temporary function to get ports until the needed port-selection-functions are implemented
    PORTS = {
        '10.7.145.61': ('swA5p1', 'swA8p1', 'swA3p1'),
        '10.7.145.62': ('swA5p1', 'swA8p1', 'swA3p1'),
        '10.7.148.94': ('swB5p1', 'swB6p1', 'swA1p1'),
        '10.7.148.95': ('swB5p1', 'swB6p1', 'swA1p1'),
        '10.7.148.138': ('swA1p1', 'swA2p1', 'swA15p1'),
        '10.7.148.139': ('swA1p1', 'swA2p1', 'swA15p1'),
        '10.7.148.248': ('sw7p1', 'sw8p2', 'sw61p1'),
        '10.7.148.249': ('sw7p1', 'sw8p2', 'sw61p1'),

        # taipan:
        '10.7.145.34': ('sw121p1', 'sw122p1', 'sw43p1'),

        # juliet:
        '10.7.145.52': ('sw2p1s1', 'sw3p1s1', 'sw17p1s1'),
        '10.7.145.53': ('sw2p1s1', 'sw3p1s1', 'sw17p1s1'),
        '10.7.148.126': ('sw11p1s1', 'sw12p1s1', 'sw12p2s1'),
        '10.7.148.127': ('sw11p1s1', 'sw12p1s1', 'sw12p2s1'),
        '10.7.148.146': ('sw7p1s1', 'sw13p1s1', 'sw13p2s1'),
        '10.7.148.147': ('sw7p1s1', 'sw13p1s1', 'sw13p2s1'),
        '10.7.148.160': ('sw17p1s1', 'sw18p1s1', 'sw2p2s1'),
        '10.7.148.161': ('sw17p1s1', 'sw18p1s1', 'sw2p2s1'),
    }
    test_port, remote_port, other_port = [Port(p) for p in PORTS[engine.ip]]
    if isinstance(device, JulietSwitch):
        test_plane = test_port
        remote_plane = remote_port
    else:
        test_plane = test_port.get_plane_port(2)
        remote_plane = remote_port.get_plane_port(2)
    return test_port, test_plane, remote_port, remote_plane, other_port


@pytest.fixture
def enable_asic_error_injection(devices):
    """
    Enables error injection on juliet switches, then disables it when the test ends. For other switches, does nothing.
    This is necessary because on juliet-switches error-injection is blocked by default, and needs to be enabled in
    order to simulate link drops.
    """
    if 'bmc' in devices.dut.platform_inventory_items_dict:
        fae = Fae()
        with allure.step("Enable ASIC error injection"):
            fae.platform.asic.error_injection.action(ActionConsts.ENABLE, expected_output='Error injection has been')
        try:
            yield
        finally:
            with allure.step("Cleanup: disable ASIC error injection"):
                fae.platform.asic.error_injection.action(ActionConsts.DISABLE, expected_output='Error injection has been')
    else:
        yield
        return


def get_phy_detail(port):
    return OutputParsingTool.parse_show_output_to_dict(port.interface.link.phy.detail.show()).get_returned_value()


def get_counters(port: Port) -> Tuple[int, int]:
    """Runs nv show interface <port> link phy detail, and returns the intentional & unintentional link-down counters"""
    phy_detail_output = get_phy_detail(port)
    return int(phy_detail_output[INTENTIONAL_LINK_DOWN_EVENTS]), int(phy_detail_output[UNINTENTIONAL_LINK_DOWN_EVENTS])


def assert_reason_for_plane(plane_port: Port, code: int, all_planes_previous_codes: Dict):
    """
    For the given plane_port, asserts the link down code (local or remote - according to `side`) is the given `code` and
    that the link down status matches this code. For all other planes of the same port, asserts their local & remote
    codes are the same as in all_planes_previous_codes.
    """
    with allure.step("Check link down reasons"):
        new_codes = get_codes(plane_port.get_aggregated_port())
        plane_index = 0 if plane_port.plane_number is None else plane_port.plane_number - 1
        # converts the plane number (1-based) to a list index (0-based)
        with allure.independent_step(f"Check reason for downed plane {plane_port.name}"):
            ValidationTool.assert_expected_value(code, new_codes[LOCAL][plane_index], 'link-down-reason code')
            ValidationTool.assert_expected_value(code, new_codes[REMOTE][plane_index], 'link-down-reason code')
        for i in range(len(all_planes_previous_codes[LOCAL])):
            if i == plane_index:
                continue
            with allure.independent_step(f"For plane {i + 1}"):
                for side in (LOCAL, REMOTE):
                    ValidationTool.assert_expected_value(all_planes_previous_codes[side][i], new_codes[side][i],
                                                         f"Plane {i + 1} {side} link-down-reason has changed")


def assert_reason_not_changed(port: Port, previous_codes):
    """Asserts the link-down local & remote codes are the same as the previous_codes."""
    with allure.step(f"Assert link-down reasons have not changed for {port.name}"):
        new_codes = get_codes(port)
        for i in range(len(new_codes[LOCAL])):
            for side in (LOCAL, REMOTE):
                ValidationTool.assert_expected_value(previous_codes[side][i], new_codes[side][i],
                                                     f"{port.name} plane {i} {side} reason changed")


def assert_reason_all_planes(aggregated_port: Port, side: str, expected_code: int):
    """Finds the current link-down-reason for the port and asserts all planes show the expected reason code"""
    with allure.step(f'Assert port {aggregated_port.name} {side} link-down-reason code is {expected_code} for all planes'):
        codes = get_codes(aggregated_port)[side]
        for i, code in enumerate(codes):
            with allure.independent_step(f'Plane {i}'):
                ValidationTool.assert_expected_value(expected_code, code)


def assert_description_matches_code(description: str, code: Union[int, str], item: str):
    expected = CODE_TO_DESCRIPTION[code].upper()
    assert description.upper() == expected, (
        f"{item} has {description=} but reason-code is {code} which is expected to have this description: " +
        expected
    )


def get_codes(port: Port) -> Dict[str, Tuple[int]]:
    """
    Obtains the output of nv show interface <port> link phy detail --output json , for example:
    {
      ...
      "linkdown-reason-code-local": "22###22###22###22",
      "linkdown-reason-code-remote": "33###33###33###33",
      "linkdown-reason-status-local": "DOWN_BY_MANAGEMENT_COMMAND###DOWN_BY_MANAGEMENT_COMMAND###DOWN_BY_MANAGEMENT_COMMAND###DOWN_BY_MANAGEMENT_COMMAND",
      "linkdown-reason-status-remote": "PEER_SIDE_DOWN_TO_DISABLE_STATE###PEER_SIDE_DOWN_TO_DISABLE_STATE###PEER_SIDE_DOWN_TO_DISABLE_STATE###PEER_SIDE_DOWN_TO_DISABLE_STATE",
      ...
    }

    Asserts that the local & remote link-down-reason descriptions for each plane, match their respective codes.
    Then returns the list of local-reason codes and the list of remote-reason codes; in this example:
      {LOCAL: (22, 22, 22, 22), REMOTE: (33, 33, 33, 33)}
    """
    with allure.step(f"Get link-down reasons for {port.name}"):
        output = port.interface.link.phy.detail.show()
        output = OutputParsingTool.parse_show_output_to_dict(output).get_returned_value()
        result = {}
        for side in (LOCAL, REMOTE):
            with allure.step(f"Assert {side} reason descriptions match their codes"):
                codes = tuple(int(x) for x in output[REASON_CODE_FIELD[side]].split(PLANE_SPLITTER))
                descriptions = output[REASON_STATUS_FIELD[side]].split(PLANE_SPLITTER)
                assert len(codes) == len(descriptions), (
                    f"{REASON_CODE_FIELD[side]} shows {len(codes)} planes but {REASON_STATUS_FIELD[side]} shows {len(descriptions)}")
                for i in range(len(codes)):
                    assert_description_matches_code(descriptions[i], codes[i], f"Plane {i} {side}")
                result[side] = codes
        logger.info(f"Link down reason codes for {port.name}: {result}")
        assert len(result[LOCAL]) == len(result[REMOTE]), (
            f"{REASON_CODE_FIELD[LOCAL]} shows {len(result[LOCAL])} planes but {REASON_STATUS_FIELD[REMOTE]} shows {len(result[REMOTE])}")
        return result


def get_loopback_plane_ports(engine, setup_name, num_of_planes=1, forbidden_transceivers=None
                             ) -> Tuple[Port, List[Port], Port, List[Port]]:
    """
    Finds two ends of a loopback connection and returns fae plane-ports.
    :returns: local_planes, remote_planes
        where local_planes is a list of fae plane-ports on the same aggregated port, and remote_planes is a list of
        their respective plane-ports at the other side of the same loopback cable. The number of items in each list is
        num_of_planes.
    """
    with allure.step("Getting loopback ports"):
        transceiver, local_port = RandomizationTool.get_random_transceiver_and_port(
            engine, setup_name, requested_ports_state=NvosConsts.LINK_STATE_UP, is_loopback=True,
            forbidden_transceivers=forbidden_transceivers)
        remote_port = Port(name=RegressionLinks.get_loopback_end(setup_name, transceiver, local_port))
        local_planes = [fae.port for fae in
                        MultiPlanarTool.select_random_plane_ports(Fae(port_name=local_port), num_of_planes)]
        remote_planes = [remote_port.get_plane_port(local_plane.plane_number) for local_plane in local_planes]
        local_port = Port(local_port)
        allure.attach("Selected ports", f"{local_port=}, {local_planes=}, {remote_port=}, {remote_planes=}")
        return local_port, local_planes, remote_port, remote_planes


def validate_qtm4_non_existent_attributes(phy_detail_output: Dict, asic_gen: str):
    """
    Validate that certain attributes are None/null on QTM4+ ASICs.
    These attributes should either be absent or have a null value.
    Collects ALL violations before failing.

    :param phy_detail_output: Parsed output from 'nv show interface <port> link phy detail'
    :param asic_gen: Human-readable ASIC generation string for error messages
    """
    violations = []

    for attr_name in PhyDetailConsts.QTM4_NON_EXISTENT_ATTRS:
        if attr_name in phy_detail_output:
            value = phy_detail_output[attr_name]
            # None/null is expected - that's correct behavior
            if value is None:
                logger.info(f"  ✓ Attribute '{attr_name}' is null on {asic_gen} (expected)")
            else:
                # Has a real value - that's the violation!
                violations.append((attr_name, value))
                logger.error(f"  ✗ Attribute '{attr_name}' should be null on {asic_gen}, but found value: '{value}'")
        else:
            logger.info(f"  ✓ Attribute '{attr_name}' is absent on {asic_gen} (expected)")

    if violations:
        error_msg = (
            f"\n{'=' * 80}\n"
            f"ATTRIBUTES SHOULD BE NULL ON QTM4+!\n"
            f"{'=' * 80}\n"
            f"ASIC Generation: {asic_gen}\n"
            f"Found {len(violations)} attribute(s) with unexpected values:\n"
        )
        for attr_name, value in violations:
            error_msg += f"  - '{attr_name}': {value}\n"
        error_msg += (
            f"\nThese attributes should be null/absent on QTM4 and newer ASICs\n"
            f"{'=' * 80}\n"
        )
        assert False, error_msg


def validate_phy_attribute_types(phy_detail_output: Dict, expected_types: Dict, is_qtm3: bool, asic_gen: str):
    """
    Validate that PHY detail attributes have the expected types.

    :param phy_detail_output: Parsed output from 'nv show interface <port> link phy detail'
    :param expected_types: Dictionary mapping attribute names to expected types
    :param is_qtm3: True if ASIC is QTM3 generation (includes NVL5)
    :param asic_gen: Human-readable ASIC generation string for error messages
    """
    for attr_name, expected_type in expected_types.items():
        with allure.step(f"Validate '{attr_name}' type"):
            # For QTM4+, skip attributes that should NOT exist (they're validated separately)
            if not is_qtm3 and attr_name in PhyDetailConsts.QTM4_NON_EXISTENT_ATTRS:
                logger.info(f"  {attr_name}: skipping type validation (attribute should not exist on QTM4+)")
                continue

            if attr_name not in phy_detail_output:
                logger.warning(f"Attribute '{attr_name}' not found in output. Available: {list(phy_detail_output.keys())}")
                continue

            value = phy_detail_output[attr_name]

            # Skip validation for None/null values - we can't determine SAI type from a null value
            if value is None:
                logger.info(f"  {attr_name}: value is None/null, skipping type validation (no type inference possible)")
                continue

            is_compatible, error_reason = is_value_compatible_with_type(str(value), expected_type)

            if is_compatible:
                logger.info(f"  {attr_name}: value='{value}' is compatible with {expected_type}")
            else:
                error_msg = (
                    f"\n{'=' * 80}\n"
                    f"TYPE MISMATCH DETECTED!\n"
                    f"{'=' * 80}\n"
                    f"Attribute: '{attr_name}'\n"
                    f"ASIC Generation: {asic_gen}\n"
                    f"Value: '{value}'\n"
                    f"Expected Type: {expected_type}\n"
                    f"Reason: {error_reason}\n"
                    f"{'=' * 80}\n"
                )
                logger.error(error_msg)
                assert False, error_msg


def is_value_compatible_with_type(value: str, expected_type: str) -> tuple:
    """
    Check if a value is compatible with an expected SAI type.

    Note: We can't determine exact SAI type from value alone (e.g., 0 could be uint8 or uint32).
    Instead, we check if the value VIOLATES the expected type's constraints.

    Compatibility rules:
    - sai_uint8_t: integer 0-255, or per-plane "v0/v1/.../vN" where each vN is uint8
    - sai_uint32_t: integer 0-4294967295, or per-plane "v0/v1/.../vN" where each vN is uint32
    - sai_u32_list_t: SAI list "count:val1:val2..." with non-negative values,
      OR per-plane "v0/v1/.../vN" where each vN is a non-negative u32
    - sai_s32_list_t: SAI list "count:val1:val2..." (signed values allowed),
      OR per-plane "v0/v1/.../vN" where each vN is a signed 32-bit int

    Multi-plane ports (e.g. QTM3 4-plane IB ports) report list-typed attributes
    as '/'-separated per-plane scalars instead of the SAI 'count:v1:v2' form, so
    both shapes are accepted.

    Args:
        value: The string value from JSON output
        expected_type: The expected SAI type

    Returns:
        tuple: (is_compatible: bool, error_reason: str or None)
    """
    value = value.strip()
    is_sai_list = ":" in value
    is_per_plane = "/" in value

    # List types - either traditional SAI "count:v1:v2..." or per-plane "v0/v1/..."
    if expected_type in ("sai_u32_list_t", "sai_s32_list_t"):
        allow_negative = expected_type == "sai_s32_list_t"
        if is_sai_list:
            parts = value.split(":")
            if not parts[0].isdigit():
                return False, f"List format invalid - first part '{parts[0]}' should be count"
            return True, None
        if is_per_plane:
            for part in value.split("/"):
                try:
                    num = int(part)
                except ValueError:
                    return False, f"Per-plane value '{part}' is not a valid integer"
                if not allow_negative and num < 0:
                    return False, f"Value {num} is negative, invalid for u32 list"
            return True, None
        return False, f"Expected list ('count:v1:v2...') or per-plane ('v0/v1/...') format, got '{value}'"

    # Single value types (uint8, uint32) - also accept per-plane "v0/v1/v2/v3" format
    if expected_type in ("sai_uint8_t", "sai_uint32_t"):
        if is_sai_list:
            return False, f"Expected single value but got SAI list format '{value}'"
        scalars = value.split("/") if is_per_plane else [value]
        for part in scalars:
            try:
                num = int(part)
            except ValueError:
                return False, f"Value '{part}' is not a valid integer"
            if expected_type == "sai_uint8_t" and (num < 0 or num > 255):
                return False, f"Value {num} out of range for uint8 (0-255)"
            if expected_type == "sai_uint32_t" and num < 0:
                return False, f"Value {num} is negative, invalid for uint32"
        return True, None

    # Unknown type
    return False, f"Unknown expected type: {expected_type}"
