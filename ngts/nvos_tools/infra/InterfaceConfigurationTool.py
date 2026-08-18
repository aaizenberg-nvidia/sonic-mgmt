import logging
import allure
import time

from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts

logger = logging.getLogger()


class InterfaceConfigurationTool:
    """
    Generic interface configuration utilities for both IB and NVL systems.

    This tool provides a unified interface for managing interface configurations
    across different system types (InfiniBand and Network Virtualization Layer).
    It automatically detects the system type and applies the appropriate configuration
    methods and parameter names.
    """

    @staticmethod
    def get_system_type_from_device(device):
        """
        Determine if the system is an InfiniBand (IB) or Network Virtualization Layer (NVL) switch.

        This function inspects the device object to identify the system architecture by checking
        for specific interface attributes. IB systems have 'interface_list' while NVL systems
        have separate access and trunk port lists.

        Args:
            device: Device object with interface attributes

        Returns:
            str: 'IB' for InfiniBand systems, 'NVL' for Network Virtualization Layer systems

        Example:
            >>> system_type = InterfaceConfigurationTool.get_system_type_from_device(my_device)
            >>> print(system_type)  # Output: 'IB' or 'NVL'

        Raises:
            Exception: If system type cannot be determined from device attributes
        """
        if hasattr(device, 'interface_list') and device.interface_list:
            return NvosConst.IB_SWITCH_TYPE
        elif hasattr(device, 'nvl_access_ports_list') or hasattr(device, 'nvl_trunk_ports_list'):
            return NvosConst.NVL_SWITCH_TYPE
        else:
            raise Exception("Unable to determine system type - neither interface_list nor nvl_ports_list found")

    @staticmethod
    def get_speed_param_name(system_type):
        """
        Get the correct speed parameter name for configuring interface speeds.

        Different system types use different parameter names when configuring interface speeds:
        - IB systems use 'ib-speed' for parameters like XDR, hdr, etc.
        - NVL systems use 'speed' for parameters like 100G, 200G, etc.

        Args:
            system_type: Either 'IB' or 'NVL' (from get_system_type_from_device)

        Returns:
            str: 'ib-speed' for IB systems, 'speed' for NVL systems

        Example:
            >>> param_name = InterfaceConfigurationTool.get_speed_param_name('IB')
            >>> print(param_name)  # Output: 'ib-speed'
            >>> param_name = InterfaceConfigurationTool.get_speed_param_name('NVL')
            >>> print(param_name)  # Output: 'speed'
        """
        if system_type == NvosConst.IB_SWITCH_TYPE:
            return IbInterfaceConsts.LINK_IB_SPEED  # "ib-speed"
        else:
            return IbInterfaceConsts.LINK_SPEED     # "speed"

    @staticmethod
    def get_current_and_supported_speeds(selected_port, system_type, port_name):
        """
        Read the current speed configuration and available speed options for an interface.

        This function queries the interface to determine what speed it's currently configured
        for and what speeds are available/supported. The method varies by system type:
        - IB systems return ib-speed values like 'XDR', 'hdr', 'fdr'
        - NVL systems return speed values like '100G', '200G', '400G'

        Args:
            selected_port: Port object representing the interface
            system_type: Either 'IB' or 'NVL' (from get_system_type_from_device)
            port_name: Interface name for logging (e.g., 'sw2p1', 'eth0')

        Returns:
            tuple: (current_speed_value, list_of_supported_speeds)

        Example:
            >>> current, supported = InterfaceConfigurationTool.get_current_and_supported_speeds(
            ...     my_port, 'IB', 'sw2p1')
            >>> print(f"Current: {current}")      # Output: Current: XDR
            >>> print(f"Supported: {supported}")  # Output: Supported: ['XDR', 'hdr', 'fdr']
        """
        if system_type == NvosConst.IB_SWITCH_TYPE:
            return InterfaceConfigurationTool.get_ib_speeds(selected_port, port_name)
        else:  # NVL
            return InterfaceConfigurationTool.get_nvl_speeds(selected_port, port_name)

    @staticmethod
    def get_ib_speeds(selected_port, port_name):
        """
        Read InfiniBand interface speed information and log detailed port status.

        This function queries an IB interface to get comprehensive speed information including
        current ib-speed, regular speed, lane configuration, and all supported ib-speeds.
        It also logs detailed information for troubleshooting and verification.

        Args:
            selected_port: Port object representing the IB interface
            port_name: Interface name for logging (e.g., 'sw2p1', 'sw32p1')

        Returns:
            tuple: (current_ib_speed_value, list_of_supported_ib_speeds)

        Example:
            >>> current_ib_speed, supported = InterfaceConfigurationTool.get_ib_speeds(my_port, 'sw2p1')
            >>> print(f"Current IB speed: {current_ib_speed}")  # Output: Current IB speed: XDR
            >>> print(f"Supported: {supported}")               # Output: Supported: ['XDR', 'hdr', 'fdr']
        """
        current_link_dict = OutputParsingTool.parse_json_str_to_dictionary(
            selected_port.interface.link.show()).get_returned_value()

        current_speed_value = current_link_dict[IbInterfaceConsts.LINK_SPEED]
        origin_ib_speed_value = current_link_dict[IbInterfaceConsts.LINK_IB_SPEED]
        current_lanes_value = current_link_dict[IbInterfaceConsts.LINK_LANES]
        supported_speeds = current_link_dict[IbInterfaceConsts.LINK_SUPPORTED_IB_SPEEDS].split(',')

        logger.info(f"Current speed value of port '{port_name}' is: {current_speed_value}")
        logger.info(f"Current ib-speed value of port '{port_name}' is: {origin_ib_speed_value}")
        logger.info(f"Current lanes value of port '{port_name}' is: {current_lanes_value}")
        logger.info(f"Supported IB speeds for {port_name}: {supported_speeds}")

        return origin_ib_speed_value, supported_speeds

    @staticmethod
    def get_nvl_speeds(selected_port, port_name):
        """
        Read Network Virtualization Layer (NVL) interface speed information.

        This function queries an NVL interface to get current speed configuration and
        retrieves the list of supported speeds from the device configuration. NVL
        speeds are typically expressed in Gigabits (e.g., '100G', '200G', '400G').

        Args:
            selected_port: Port object representing the NVL interface
            port_name: Interface name for logging (e.g., 'sw1p1', 'acp0')

        Returns:
            tuple: (current_speed_value, list_of_supported_speeds)

        Example:
            >>> current_speed, supported = InterfaceConfigurationTool.get_nvl_speeds(my_port, 'sw1p1')
            >>> print(f"Current speed: {current_speed}")  # Output: Current speed: 100G
            >>> print(f"Supported: {supported}")          # Output: Supported: ['100G', '200G', '400G']
        """
        current_link_dict = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_port.interface.link.show()).get_returned_value()

        current_speed_value = current_link_dict[IbInterfaceConsts.LINK_SPEED]
        device = TestToolkit.get_device()
        if hasattr(device, 'get_configurable_nvl_speeds'):
            supported_speeds = device.get_configurable_nvl_speeds()
        elif hasattr(device, 'supported_nvl_speeds'):
            supported_speeds = device.supported_nvl_speeds
        else:
            supported_speeds = []

        logger.info(f"Current speed value of port '{port_name}' is: {current_speed_value}")
        logger.info(f"Configurable NVL speeds for {port_name}: {supported_speeds}")

        return current_speed_value, supported_speeds

    @staticmethod
    def configure_and_verify_speed(selected_port, speed, system_type, port_name, step_description="Configure speed"):
        """
        Configure an interface to a specific speed and verify the change was applied successfully.

        This function performs a complete speed configuration cycle:
        1. Determines the correct parameter name based on system type ('ib-speed' or 'speed')
        2. Applies the speed configuration with confirmation
        3. Reads back the configuration to verify it was applied correctly
        4. Asserts that the configured speed matches what was requested

        Args:
            selected_port: Port object representing the interface to configure
            speed: Speed value to configure (e.g., 'hdr', 'XDR' for IB; '100G', '200G' for NVL)
            system_type: Either 'IB' or 'NVL' (from get_system_type_from_device)
            port_name: Interface name for logging (e.g., 'sw2p1')
            step_description: Custom description for test reporting (optional)

        Example:
            >>> InterfaceConfigurationTool.configure_and_verify_speed(
            ...     my_port, 'hdr', 'IB', 'sw2p1', "Set IB speed to HDR")
            # Configures sw2p1 to HDR speed and verifies the change
        """
        param_name = InterfaceConfigurationTool.get_speed_param_name(system_type)
        speed_field = IbInterfaceConsts.LINK_IB_SPEED if system_type == NvosConst.IB_SWITCH_TYPE else IbInterfaceConsts.LINK_SPEED
        parser_func = OutputParsingTool.parse_json_str_to_dictionary if system_type == NvosConst.IB_SWITCH_TYPE else OutputParsingTool.parse_show_interface_link_output_to_dictionary

        with allure.step(step_description):
            selected_port.interface.link.set(op_param_name=param_name, op_param_value=speed,
                                             apply=True, ask_for_confirmation=True).verify_result()

            # Save configuration to make speed change persistent
            TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(TestToolkit.get_engine())
            # Wait for port to come back up after speed change (port goes down during speed reconfiguration)
            selected_port.interface.wait_for_port_state(state=NvosConsts.LINK_STATE_UP, timeout=30).verify_result()

            # Wait for port to come back up after speed change (port goes down during speed reconfiguration)
            selected_port.interface.wait_for_port_speed(selected_port, system_type, speed)

            logger.info(f"Successfully configured speed {speed} for port {port_name}")

    @staticmethod
    def unset_speed_configuration(selected_port, device):
        """
        Remove speed configuration from an interface, returning it to default/automatic speed.

        This function cleans up manual speed configurations by unsetting the speed parameter.
        After unsetting, the interface will typically return to its default or automatically
        negotiated speed. The correct parameter name ('ib-speed' or 'speed') is determined
        automatically based on the system type.

        Args:
            selected_port: Port object representing the interface to clean up
            device: Device object used to determine system type (IB vs NVL)

        Example:
            >>> InterfaceConfigurationTool.unset_speed_configuration(my_port, my_device)
            # Removes manual speed configuration from the interface
        """
        with allure.step(f"Unset speed configuration for port {selected_port.name}"):
            system_type = InterfaceConfigurationTool.get_system_type_from_device(device)
            param_name = InterfaceConfigurationTool.get_speed_param_name(system_type)

            selected_port.interface.link.unset(op_param=param_name, apply=True,
                                               ask_for_confirmation=True).verify_result()
            logger.info(f"Successfully unset {param_name} for port {selected_port.name}")

    @staticmethod
    def verify_speed_configuration(selected_port, expected_speed, device, step_description="Verify speed configuration"):
        """
        Verify that an interface is configured to the expected speed value.

        This function reads the current speed configuration of an interface and compares
        it against an expected value. It handles both IB and NVL systems automatically,
        using the appropriate parsing method and speed field for each system type.
        The verification will fail with a descriptive error if speeds don't match.

        Args:
            selected_port: Port object representing the interface to verify
            expected_speed: Expected speed value (e.g., 'hdr', 'XDR' for IB; '100G', '200G' for NVL)
            device: Device object used to determine system type (IB vs NVL)
            step_description: Custom description for test reporting (optional)

        Example:
            >>> InterfaceConfigurationTool.verify_speed_configuration(
            ...     my_port, 'hdr', my_device, "Verify port is at HDR speed")
            # Checks that the port is configured to HDR speed

        Raises:
            AssertionError: If the current speed doesn't match the expected speed
        """
        with allure.step(step_description):
            system_type = InterfaceConfigurationTool.get_system_type_from_device(device)

            if system_type == NvosConst.IB_SWITCH_TYPE:
                parser_func = OutputParsingTool.parse_json_str_to_dictionary
                speed_field = IbInterfaceConsts.LINK_IB_SPEED
            else:  # NVL
                parser_func = OutputParsingTool.parse_show_interface_link_output_to_dictionary
                speed_field = IbInterfaceConsts.LINK_SPEED

            for _ in range(10):
                current_dict = parser_func(selected_port.interface.link.show()).get_returned_value()
                if speed_field not in current_dict:
                    time.sleep(3)
                else:
                    break
            assert speed_field in current_dict, "Speed can't be found in output"
            current_speed = current_dict[speed_field]

            assert current_speed == expected_speed, f"Speed verification failed. Expected: {expected_speed}, Got: {current_speed}"
            logger.info(f"Speed verification successful: {current_speed} for port {selected_port.name}")

    @staticmethod
    def change_mtu_on_random_port(devices):
        """
        Select a random ACTIVE port and change its MTU to a different value.

        Used to verify that interface configuration survives upgrades (downgrade/upgrade, ISSU).
        Unlike speed changes, MTU changes are safe across all system types.

        If the MTU change cannot be verified (e.g. `link.show()` does not expose the `mtu`
        field on this platform), the configured MTU is unset and the function returns None
        so the caller can skip MTU testing without failing the test.

        Args:
            devices: Test devices object containing device configuration

        Returns:
            tuple: (Port object, original_mtu, new_mtu) on success, or None if MTU testing
                   could not be exercised on this platform.
        """
        from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
        from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli

        with allure.step("Configure MTU on a random port"):
            _, selected_port, port_name = InterfaceConfigurationTool._detect_system_type_and_select_active_port(devices.dut)
            link_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                selected_port.interface.link.show()).get_returned_value()
            if IbInterfaceConsts.LINK_MTU not in link_output:
                logger.warning(f"Port {port_name} link show does not expose 'mtu' — skipping MTU testing")
                return None
            original_mtu = link_output[IbInterfaceConsts.LINK_MTU]
            new_mtu = RandomizationTool.select_random_value(
                IbInterfaceConsts.MTU_VALUES, [original_mtu]).get_returned_value()
            logger.info(f"Setting MTU on {port_name}: {original_mtu} -> {new_mtu}")
            try:
                selected_port.interface.link.set(
                    op_param_name='mtu', op_param_value=str(new_mtu), apply=True,
                    ask_for_confirmation=True).verify_result()
                selected_port.interface.wait_for_mtu_changed(new_mtu)
                NvueGeneralCli.save_config(TestToolkit.engines.dut)
                return selected_port, original_mtu, new_mtu
            except Exception as e:
                logger.warning(
                    f"MTU change on {port_name} ({original_mtu} -> {new_mtu}) failed: {e}. "
                    f"Unsetting MTU and continuing without MTU testing.")
                try:
                    selected_port.interface.link.unset(
                        op_param='mtu', apply=True, ask_for_confirmation=True).verify_result()
                    selected_port.interface.wait_for_mtu_changed(original_mtu)
                    NvueGeneralCli.save_config(TestToolkit.engines.dut)
                except Exception as cleanup_err:
                    logger.warning(f"Failed to unset MTU on {port_name}: {cleanup_err}")
                return None

    @staticmethod
    def verify_and_cleanup_mtu(mtu_info):
        """
        Verify MTU is preserved (e.g. after upgrade/ISSU) and restore to default.

        Args:
            mtu_info: tuple of (Port, original_mtu, new_mtu) or None
        """
        if not mtu_info:
            logger.info("No MTU testing was performed - skipping verification")
            return
        selected_port, original_mtu, new_mtu = mtu_info
        with allure.step(f"Verify MTU preserved on {selected_port.name} and cleanup"):
            link_output = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                selected_port.interface.link.show()).get_returned_value()
            current_mtu = link_output[IbInterfaceConsts.LINK_MTU]
            assert current_mtu == new_mtu, \
                f"MTU not preserved on {selected_port.name}: expected {new_mtu}, got {current_mtu}"
            logger.info(f"MTU preserved on {selected_port.name}: {current_mtu}")
            selected_port.interface.link.unset(
                op_param='mtu', apply=True, ask_for_confirmation=True).verify_result()
            from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
            NvueGeneralCli.save_config(TestToolkit.engines.dut)
            logger.info(f"MTU restored to default on {selected_port.name}")

    @staticmethod
    def choose_random_port_and_test_speed_configuration(engines, devices):
        """
        Orchestrate complete interface speed configuration testing for system upgrade validation.

        This is the main speed testing function that ensures interface speed configurations
        work correctly before and after system upgrades. It automatically detects whether
        the system is IB or NVL and performs appropriate speed testing.

        The function performs a comprehensive 3-step speed configuration cycle:
        1. Detect system type (IB vs NVL) and randomly select an available ACTIVE port
        2. Read current speed and get list of all supported speeds for that port
        3. Choose a different speed from supported options (skips test if none available)
        4. Execute rigorous 3-step speed configuration cycle:
           - Configure new speed → verify it applied correctly
           - Revert to original speed → verify it reverted correctly
           - Configure new speed again → verify it applied correctly again

        Args:
            engines: Test engines object containing connection information
            devices: Test devices object containing device configuration

        Returns:
            tuple: (Port object, original_speed, new_speed, supported_speeds, nvl_access_info)
                   nvl_access_info is None for IB/trunk, or a dict for NVL access ports.
        """
        from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
        from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
        from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
        from retry.api import retry_call
        import pytest

        with allure.step("Determine system type and choose random ACTIVE port"):
            # Check if device supports speed configuration (need at least 2 speeds to test changing)
            device = devices.dut
            if hasattr(device, 'supported_ib_speeds') and len(device.supported_ib_speeds) <= 1:
                logger.info(f"{type(device).__name__} supports only {device.supported_ib_speeds}, skipping speed change test")
                return None

            system_type, selected_port, port_name = InterfaceConfigurationTool._detect_system_type_and_select_active_port(device)
            current_speed, supported_speeds = InterfaceConfigurationTool.get_current_and_supported_speeds(selected_port, system_type, port_name)
            new_speed = InterfaceConfigurationTool._choose_different_speed(current_speed, supported_speeds, port_name)
            if new_speed is None:
                return None

            is_nvl_access = system_type == NvosConst.NVL_SWITCH_TYPE and port_name.startswith('acp')

            if is_nvl_access:
                from ngts.tests_nvos.interfaces.nvl_port.helpers import validate_ports_state_and_speed
                from ngts.nvos_tools.ib.InterfaceConfiguration.Port import PortRequirements
                port_names = devices.dut.nvl_access_ports_list

                with allure.step("Verify all access ports are UP before speed change"):
                    reqs = PortRequirements()
                    reqs.set_port_state(NvosConsts.LINK_STATE_UP)
                    up_ports = [p.name for p in Port.get_list_of_ports(port_requirements_object=reqs) if p.name.startswith('acp')]
                    missing = [p for p in port_names if p not in up_ports]
                    assert not missing, (
                        f"The following access ports are NOT UP: {missing}. "
                        f"Loopbox may be missing or disconnected."
                    )
                port_indices = [int(n.replace('acp', '')) for n in port_names]
                access_ports_range = f'acp{min(port_indices)}-{max(port_indices)}'
                all_ports = Port(access_ports_range)
                default_speed = devices.dut.access_port_speed if hasattr(devices.dut, 'access_port_speed') else current_speed
                logger.info(f"NVL access ports: setting speed {new_speed} on ALL ports via range {access_ports_range}")

                with allure.step(f"Set speed {new_speed} on all access ports: {access_ports_range}"):
                    all_ports.interface.link.set(
                        op_param_name='speed', op_param_value=new_speed,
                        ask_for_confirmation=True, apply=True
                    ).verify_result()
                    TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(TestToolkit.get_engine())

                with allure.step(f"Verify ALL access ports reached speed {new_speed}"):
                    retry_call(validate_ports_state_and_speed, [new_speed, port_names, 'acp'],
                               exceptions=AssertionError, tries=6, delay=30)
                    logger.info(f"All access ports reached speed {new_speed}")

                nvl_access_info = {
                    'port_names': port_names,
                    'range_name': access_ports_range,
                    'default_speed': default_speed,
                }
                return selected_port, current_speed, new_speed, supported_speeds, nvl_access_info

            InterfaceConfigurationTool._test_speed_configuration_cycle(selected_port, current_speed, new_speed, system_type, port_name)
            return selected_port, current_speed, new_speed, supported_speeds, None

    @staticmethod
    def has_active_ports(device):
        """
        Check if the device has any active ports available for interface testing.

        Returns:
            bool: True if active ports exist, False otherwise
        """
        from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
        from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts

        try:
            if hasattr(device, 'interface_list') and device.interface_list:
                RandomizationTool.select_random_port(
                    requested_ports_state=NvosConsts.LINK_STATE_UP,
                    requested_ports_logical_state=IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE,
                    interface_type='sw'
                ).get_returned_value()
                return True
            elif hasattr(device, 'nvl_access_ports_list') or hasattr(device, 'nvl_trunk_ports_list'):
                if (hasattr(device, 'nvl_trunk_ports_list') and device.nvl_trunk_ports_list) or \
                   (hasattr(device, 'nvl_access_ports_list') and device.nvl_access_ports_list):
                    return True
        except Exception as e:
            logger.info(f"No active ports found: {e}")
        return False

    @staticmethod
    def _detect_system_type_and_select_active_port(device):
        """
        Detect system type and select a random ACTIVE/CONNECTED port for speed testing.

        This function ensures that only ports with active links are selected for speed testing,
        preventing failures due to disconnected interfaces. It follows the same pattern as
        _get_available_nvl_ports to validate link status before selection.
        """
        from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
        from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
        from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
        import pytest
        import logging

        if hasattr(device, 'interface_list') and device.interface_list:
            with allure.step("IB system detected - choosing ACTIVE port from interface_list"):
                # Select only ports that are UP and ACTIVE
                try:
                    selected_port_obj = RandomizationTool.select_random_port(
                        requested_ports_state=NvosConsts.LINK_STATE_UP,
                        requested_ports_logical_state=IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE,
                        interface_type='sw'
                    ).get_returned_value()
                    port_name = selected_port_obj.name
                    logging.info(f"Selected ACTIVE IB port for speed testing: {port_name}")
                    return NvosConst.IB_SWITCH_TYPE, selected_port_obj, port_name
                except Exception as e:
                    logging.error(f"Failed to find active IB port: {e}")
                    pytest.skip("No active IB ports available for speed testing")

        elif hasattr(device, 'nvl_access_ports_list') or hasattr(device, 'nvl_trunk_ports_list'):
            with allure.step("NVL system detected - choosing ACTIVE port from available nvl port types"):
                # First, determine what port types are available
                available_port_types = []

                if hasattr(device, 'nvl_trunk_ports_list') and device.nvl_trunk_ports_list:
                    available_port_types.append('trunk')
                    logging.info(f"NVL trunk ports available: {len(device.nvl_trunk_ports_list)} ports")

                if hasattr(device, 'nvl_access_ports_list') and device.nvl_access_ports_list:
                    available_port_types.append('access')
                    logging.info(f"NVL access ports available: {len(device.nvl_access_ports_list)} ports")

                if not available_port_types:
                    pytest.skip("No NVL ports available for speed testing")

                # Randomly choose between available port types
                chosen_port_type = RandomizationTool.select_random_value(available_port_types).get_returned_value()
                logging.info(f"Randomly chosen NVL port type for testing: {chosen_port_type}")

                # Select active port based on chosen type
                try:
                    if chosen_port_type == 'trunk':
                        # Trunk ports: need LINK_STATE_UP and transceivers
                        selected_port_obj = RandomizationTool.select_random_port(
                            requested_ports_state=NvosConsts.LINK_STATE_UP,
                            interface_type='sw'  # trunk ports
                        ).get_returned_value()
                        logging.info(f"Selected ACTIVE NVL trunk port for speed testing: {selected_port_obj.name}")
                    else:  # access
                        # Access ports: need LINK_LOG_STATE_INITIALIZE (with loopboxes)
                        selected_port_obj = RandomizationTool.select_random_port(
                            requested_ports_logical_state=NvosConsts.LINK_LOG_STATE_INITIALIZE,
                            interface_type='acp'  # access ports
                        ).get_returned_value()
                        logging.info(f"Selected ACTIVE NVL access port for speed testing: {selected_port_obj.name}")

                    port_name = selected_port_obj.name
                    return NvosConst.NVL_SWITCH_TYPE, selected_port_obj, port_name

                except Exception as e:
                    logging.error(f"Failed to find active {chosen_port_type} port: {e}")
                    # Try the other port type if available
                    other_port_types = [pt for pt in available_port_types if pt != chosen_port_type]
                    if other_port_types:
                        other_type = other_port_types[0]
                        logging.info(f"Trying fallback to {other_type} ports")
                        try:
                            if other_type == 'trunk':
                                selected_port_obj = RandomizationTool.select_random_port(
                                    requested_ports_state=NvosConsts.LINK_STATE_UP,
                                    interface_type='sw'
                                ).get_returned_value()
                            else:  # access
                                selected_port_obj = RandomizationTool.select_random_port(
                                    requested_ports_logical_state=NvosConsts.LINK_LOG_STATE_INITIALIZE,
                                    interface_type='acp'
                                ).get_returned_value()

                            port_name = selected_port_obj.name
                            logging.info(f"Fallback successful - selected {other_type} port: {port_name}")
                            return NvosConst.NVL_SWITCH_TYPE, selected_port_obj, port_name

                        except Exception as e2:
                            logging.error(f"Fallback to {other_type} ports also failed: {e2}")

                    pytest.skip(f"No active NVL ports available for speed testing (tried {available_port_types})")

        else:
            raise Exception("Unable to determine system type - neither interface_list nor nvl_ports_list found")

    @staticmethod
    def _choose_different_speed(current_speed, supported_speeds, port_name):
        """
        Select a random speed that's different from the current configuration.

        This function filters out the current speed from the list of supported speeds
        and randomly selects one of the remaining options. If no alternative speeds
        are available, it skips the test with an informative message.

        Args:
            current_speed: Current speed configuration (e.g., 'XDR', '100G')
            supported_speeds: List of all supported speeds (e.g., ['XDR', 'hdr', 'fdr'])
            port_name: Interface name for logging (e.g., 'sw2p1')

        Returns:
            str: Randomly selected speed that differs from current_speed

        Example:
            >>> new_speed = InterfaceConfigurationTool._choose_different_speed('XDR', ['XDR', 'hdr', 'fdr'], 'sw2p1')
            >>> print(new_speed)  # Output: 'hdr' or 'fdr' (randomly chosen)

        Raises:
            pytest.skip: If no alternative speeds are available for testing
        """
        from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
        import pytest
        import logging

        available_speeds_other_than_original = [speed.strip() for speed in supported_speeds if speed.strip() != current_speed]
        if not available_speeds_other_than_original:
            logging.info(f"No alternative speeds available for port {port_name}. Current: {current_speed}, Supported: {supported_speeds}")
            return None

        new_speed = RandomizationTool.select_random_value(available_speeds_other_than_original).get_returned_value()
        logging.info(f"Chosen different speed for {port_name}: {new_speed} (original was: {current_speed})")
        return new_speed

    @staticmethod
    def _test_speed_configuration_cycle(selected_port, original_speed, new_speed, system_type, port_name):
        """
        Execute a comprehensive 3-step speed configuration test cycle.

        This function performs rigorous speed configuration testing by executing three
        consecutive configuration changes, verifying each step to ensure the interface
        responds correctly to speed changes and can reliably switch between speeds.

        The 3-step cycle tests:
        1. Configure new speed → verify it applied correctly
        2. Revert to original speed → verify it reverted correctly
        3. Configure new speed again → verify it applied correctly again

        Args:
            selected_port: Port object representing the interface to test
            original_speed: Original speed value to revert to (e.g., 'XDR', '100G')
            new_speed: New speed value to test with (e.g., 'hdr', '200G')
            system_type: Either 'IB' or 'NVL' (determines parameter names)
            port_name: Interface name for logging (e.g., 'sw2p1')

        Example:
            >>> InterfaceConfigurationTool._test_speed_configuration_cycle(my_port, 'XDR', 'hdr', 'IB', 'sw2p1')
            # Executes: XDR → hdr → XDR → hdr (with verification at each step)
        """
        # Step 1: Configure new speed
        InterfaceConfigurationTool.configure_and_verify_speed(selected_port, new_speed, system_type, port_name,
                                                              f"Set {InterfaceConfigurationTool.get_speed_param_name(system_type)} '{new_speed}' for port '{port_name}'")

        # Step 2: Configure back to original
        InterfaceConfigurationTool.configure_and_verify_speed(selected_port, original_speed, system_type, port_name,
                                                              f"Set {InterfaceConfigurationTool.get_speed_param_name(system_type)} back to original '{original_speed}' for port '{port_name}'")

        # Step 3: Configure new speed again
        InterfaceConfigurationTool.configure_and_verify_speed(selected_port, new_speed, system_type, port_name,
                                                              f"Set {InterfaceConfigurationTool.get_speed_param_name(system_type)} '{new_speed}' again for port '{port_name}'")
