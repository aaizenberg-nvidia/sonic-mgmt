import logging
import re
import time
from functools import lru_cache

from ngts.constants.constants import InfraConst
from ngts.nvos_constants.constants_nvos import LinkDetectionConsts
from ngts.nvos_tools.Devices.IbDevice import JulietSwitch
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, MloopConsts
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvCommand import NvCommand
from ngts.nvos_tools.infra.LinuxCmdBuilderTool import LinuxCmdBuilderTool
from ngts.nvos_tools.infra.MultiPlanarTool import MultiPlanarTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RegisterTool import RegisterTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tests_nvos.cluster.cluster_tools import summarize_switch_ports
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


class IbInterfaceTool:

    @staticmethod
    def switch_port_connection_mode(port_name, connection_mode):
        with allure.step(f"Set connection-mode for {port_name} to {connection_mode}"):
            interface = Interface(parent_obj=None, port_name=port_name)
            interface.link.set(op_param_name=LinkDetectionConsts.CONNECTION_MODE,
                               op_param_value=connection_mode, apply=True,
                               ask_for_confirmation=True).verify_result()

    @staticmethod
    def switch_port_connection_mode_to_default(port_name):
        with allure.step(f"Set connection-mode for {port_name} default"):
            interface = Interface(parent_obj=None, port_name=port_name)
            interface.link.unset(op_param_name=LinkDetectionConsts.CONNECTION_MODE,
                                 apply=True, ask_for_confirmation=True).verify_result()

    @staticmethod
    def simulate_plugin_module_event(engine, device, module_index, mst_dev_name, sleep=50):
        with allure.step(f"Simulate plugin event for module {module_index}"):
            admin_status = "1"  # The code to simulate plug event
            RegisterTool.update_pmaos_register(engine, device, mst_dev_name=mst_dev_name,
                                               admin_status=admin_status, module_index=module_index)
            time.sleep(sleep)

    @staticmethod
    def simulate_unplug_module_event(engine, device, module_index, mst_dev_name, sleep=8):
        with allure.step(f"Simulate unplug event for module {module_index}"):
            admin_status = "0xe"  # The code to simulate unplug event
            RegisterTool.update_pmaos_register(engine, device, mst_dev_name=mst_dev_name,
                                               admin_status=admin_status, module_index=module_index)
            time.sleep(sleep)

    @staticmethod
    def simulate_toggle_port_event(engine, device, port_name='', sleep=5):
        with allure.step(f"Simulate toggle port event for port {port_name}"):
            mst_dev_name = IbInterfaceTool.get_mst_dev_name(engine, port_name=port_name)
            local_port_hex = IbInterfaceTool.get_local_port_hex(engine, device, port_name)
            RegisterTool.update_prei_register(engine, mst_dev_name=mst_dev_name, local_port=local_port_hex)
            time.sleep(sleep)

    @staticmethod
    def get_port_admin_state_paos(engine, port_name, lp_msb="0", plane_ind="0"):
        with allure.step(f"Get PAOS admin state for port {port_name}"):
            mst_dev_name = IbInterfaceTool.get_mst_dev_name(engine, port_name=port_name)
            local_port = IbInterfaceTool.get_local_port_hex(engine, device, port_name)
            return RegisterTool.get_paos_register(engine, mst_dev_name, local_port, lp_msb, plane_ind)

    @staticmethod
    def set_port_admin_state_paos_down(engine, port_name, lp_msb="0", plane_ind="0", sleep=5):
        with allure.step(f"Set PAOS admin state DOWN for port {port_name}"):
            admin_status = "2"  # Admin state down
            mst_dev_name = IbInterfaceTool.get_mst_dev_name(engine, port_name=port_name)
            local_port = IbInterfaceTool.get_local_port_hex(engine, device, port_name)
            RegisterTool.update_paos_register(engine, mst_dev_name, local_port, admin_status, lp_msb, plane_ind)
            time.sleep(sleep)

    @staticmethod
    def set_port_admin_state_paos_up(engine, port_name, lp_msb="0", plane_ind="0", sleep=5):
        with allure.step(f"Set PAOS admin state UP for port {port_name}"):
            admin_status = "1"  # Admin state up
            mst_dev_name = IbInterfaceTool.get_mst_dev_name(engine, port_name=port_name)
            local_port = IbInterfaceTool.get_local_port_hex(engine, device, port_name)
            RegisterTool.update_paos_register(engine, mst_dev_name, local_port, admin_status, lp_msb, plane_ind)
            time.sleep(sleep)

    @staticmethod
    @lru_cache
    def get_mst_dev_name(engine, module_name=None, port_name=None):
        if not (module_name or port_name):
            raise ValueError(f'{module_name=}, {port_name=}')
        fae_port_name = port_name or f"{module_name}p1"
        asic_letter, _, _, _, plane_number = Port.parse_port_name(port_name)

        with allure.step(f"Find mst_dev_name for {fae_port_name}"):
            if asic_letter or plane_number:  # Crocodile ports, e.g. swA...
                # asic_letter is for Crocodile where the port name mentions ASIC 'A' or 'B'.
                # plane_number is for other systems and only for plane-ports, e.g. sw1p1pl3 is plane 3 which belongs
                # to ASIC 2 (because plane-numbers are 1-based and ASIC-numbers are (sometimes) 0-based, god knows why)
                asic_number = MultiPlanarTool.asic_letter_to_number(asic_letter) if asic_letter else plane_number - 1
                logger.info(f'{fae_port_name=} --> {asic_number=}')
                asic_dev_id_number = f"DEV_ID_ASIC_{asic_number}"
                asic_mapping_number = MultiPlanarTool.get_asic_conf_dict(engine)[asic_dev_id_number]
                cmd = LinuxCmdBuilderTool("sudo mst status -v").grep("pciconf").grep(f"{asic_mapping_number}").awk_print(
                    "2").build()
                mst_dev_name = engine.run_cmd(cmd, validate=True)

            else:  # for aggregated port on non-Crocodile, get the primary-asic-device from nv show fae interface <port>
                fae = Fae(port_name=fae_port_name)  # todo nv_command.fae[fae_port_name] ...
                output_fae_port = OutputParsingTool.parse_show_interface_output_to_dictionary(
                    fae.interface.show()).get_returned_value()
                mst_dev_name = output_fae_port[IbInterfaceConsts.PRIMARY_ASIC_DEVICE]

        logger.info(f'mst device for {fae_port_name} is {mst_dev_name}')
        return mst_dev_name

    @staticmethod
    def get_mst_cable_name(engines, transceiver_name, pci_conf):
        match = re.search(r'(?<=sw)[^\d]*(\d+)', transceiver_name)
        mst_dev_name = pci_conf + '_cable_'
        if match:
            mst_dev_name += str(int(match.group(1)) - 1)

        return mst_dev_name

    @staticmethod
    def is_dev_module(engine, transceiver_name):
        engine.run_cmd("sudo mst cable add")
        output = (engine.run_cmd(f'sudo flint -d {transceiver_name} q | grep DEV')).strip()
        return bool(output)

    @staticmethod
    def get_local_port_hex(engine, device, port_name):
        asic_letter, port_number, _, _, _ = Port.parse_port_name(port_name)
        asic_number = (MultiPlanarTool.asic_letter_to_number(asic_letter)
                       if asic_letter else MultiPlanarTool.get_primary_asic(Fae(port_name=port_name)))
        docker = InfraConst.SYNCD_IBV_DOCKER.format(asic_number)
        cmd = f"docker exec {docker} sx_api_ports_mapping_dump.py"
        table_output = engine.run_cmd(cmd, validate=True)
        lane_bmap = device.get_lane_bmap(Port(port_name))
        if isinstance(device, JulietSwitch) and port_number >= 10:
            # Juliet ports 10-18 belong to ASIC B and their label_port numbering restarts at 1
            port_number -= 9
        return get_log_port(table_output, port_number, lane_bmap)

    @staticmethod
    def configure_rosalind_simx_loopback(engines, devices,
                                         mloop_mode: MloopConsts.Mode = MloopConsts.Mode.LOGICAL):
        """
        Configure RosalindSimx using per-interface MLOOP to enable link simulation.

        Per nvbug 6072833 the FAE mloop knob is per-interface (replacing the old
        system-wide knob). The system toggles affected ports automatically, so no
        manual link state down/up dance is needed.

        Steps:
        1. Set fae mloop=<mloop_mode> on all access ports (system auto-toggles ports)
        2. Save configuration for persistence across reboots

        Args:
            engines: Engine objects containing DUT connection
            devices: Device objects containing device configuration
            mloop_mode: MloopConsts.Mode to apply (default: LOGICAL = LLU2LLU loopback)

        Returns:
            bool: True if configuration was successful, False if skipped
        """
        if not devices.dut.require_mloop_setup:
            logger.info("Device does not require MLOOP setup, skipping MLOOP configuration")
            return False

        logger.info(f"RosalindSimx detected, configuring per-interface MLOOP (mode={mloop_mode})")

        with allure.step(f"RosalindSimx per-interface MLOOP Configuration (mode={mloop_mode})"):
            access_ports = devices.dut.nvl_access_ports_list
            if not access_ports:
                logger.warning("No access ports found, skipping RosalindSimx configuration")
                return False

            port_range = summarize_switch_ports(access_ports)
            logger.info(f"Access ports range: {port_range}")

            with allure.step(f"Enable mloop={mloop_mode} on {port_range}"):
                NvCommand().fae.interfaces[port_range].link.mloop.set(
                    op_param_name=mloop_mode,
                    apply=True,
                    ask_for_confirmation=True,
                ).verify_result()
                logger.info(f"Set mloop={mloop_mode} on {port_range} (system auto-toggles ports)")

            with allure.step("Save configuration"):
                engines.dut.run_cmd('nv config save')
                logger.info("Configuration saved")

            with allure.step("Final stabilization wait - 1 minute"):
                logger.info("Waiting 1 minute for system stabilization...")
                time.sleep(60)
                logger.info("RosalindSimx per-interface MLOOP configuration completed")

        return True

    @staticmethod
    def verify_rosalind_simx_links_up(devices, max_retries=2, retry_wait_minutes=3):
        """
        Verify that RosalindSimx links are actually up after configuration.

        This function attempts to find any port that is in UP state. It will try different
        interface types (acp, sw) based on what's available on the device.

        Args:
            devices: Device objects containing device configuration
            max_retries: Number of additional retries if no links found (default: 1)
            retry_wait_minutes: Minutes to wait between retries (default: 3)

        Returns:
            bool: True if at least one link is found up, False if all attempts fail

        Example:
            >>> success = IbInterfaceTool.verify_rosalind_simx_links_up(devices)
            >>> if not success:
            ...     logger.warning("No UP links found on device")
        """
        if not devices.dut.require_mloop_setup:
            logger.info("Device does not require MLOOP setup, skipping link verification")
            return True

        with allure.step("Verify RosalindSimx links are up"):
            for attempt in range(max_retries + 1):
                attempt_num = attempt + 1
                logger.info(f"Link verification attempt {attempt_num}/{max_retries + 1}")

                # Try to find any up port using different interface types
                interface_types_to_try = []

                # Check what's available and build priority list
                if hasattr(devices.dut, 'nvl_access_ports_list') and devices.dut.nvl_access_ports_list:
                    interface_types_to_try.append('acp')
                if hasattr(devices.dut, 'nvl_trunk_ports_list') and devices.dut.nvl_trunk_ports_list:
                    interface_types_to_try.append('sw')

                if not interface_types_to_try:
                    logger.warning("No NVL ports found on device, skipping link verification")
                    return True

                # Try each interface type
                for interface_type in interface_types_to_try:
                    try:
                        with allure.step(f"Checking {interface_type} ports for UP state"):
                            logger.info(f"Attempting to find UP {interface_type} port")

                            # Try to get any port that is UP
                            up_port = Tools.RandomizationTool.select_random_port(
                                requested_ports_state="up",
                                interface_type=interface_type
                            ).get_returned_value()

                            if up_port:
                                logger.info(f"Found UP port: {up_port.name} (type: {interface_type})")
                                return True

                    except Exception as e:
                        logger.info(f"No UP {interface_type} ports found: {e}")
                        continue

                # If we reach here, no UP ports were found in this attempt
                if attempt < max_retries:
                    with allure.step(f"No UP links found, waiting {retry_wait_minutes} minutes before retry"):
                        logger.warning(f"No UP links found on attempt {attempt_num}. "
                                       f"Waiting {retry_wait_minutes} minutes before retry {attempt_num + 1}")
                        time.sleep(retry_wait_minutes * 60)  # Convert minutes to seconds
                else:
                    # Final attempt failed
                    logger.error(f"No UP links found after {max_retries + 1} attempts.")
                    return False

        return False

    @staticmethod
    def get_connected_transceivers_dict(engine, transceivers_list):
        """
        Gets the connection status of all transceivers by checking system status files.

        Args:
            engine: The engine object to run commands on
            transceivers_list (list): List of all transceivers (including non-ASIC ones like fnm1)

        Returns:
            dict: Dictionary with transceiver names as keys and True/False as values indicating connection status
        """
        # Sort transceivers so that FNM ports come at the end of the list
        sorted_transceivers_list = sorted(transceivers_list, key=lambda x: 'fnm' in x.lower())

        # Initialize result dictionary with all transceivers set to False
        transceiver_status = {transceiver: False for transceiver in sorted_transceivers_list}

        # Use a single command to get all status values at once
        cmd = f"for i in {{0..{len(sorted_transceivers_list) + 2}..1}}; do echo \"${{i}}: $(cat /sys/module/sx_core/asic0/module${{i}}/status 2>/dev/null)\"; done"
        output = engine.run_cmd(cmd, validate=True)

        # Map system status lines to transceivers by position
        # Each line corresponds to a transceiver in the list by index
        transceiver_index = 0
        for line in output.splitlines():
            # Parse line format: "1: 1" or "8: 2"
            parts = line.split(':')
            if len(parts) == 2:
                try:
                    status = parts[1].strip()
                    # Only process if we have a valid status and haven't exceeded our transceiver list
                    if status and transceiver_index < len(sorted_transceivers_list):
                        transceiver_name = sorted_transceivers_list[transceiver_index]
                        if status == '1':
                            transceiver_status[transceiver_name] = True
                        transceiver_index += 1  # Only increment if we successfully processed the line
                except (ValueError, IndexError):
                    continue  # Skip malformed lines

        return transceiver_status


def get_log_port(table: str, label_port: int, lane_bmap: str):
    """
    Fetches the log_port value based on label_port and lane_bmap values from a given table.
    Returns only the last two digits of log_port after removing the '0x100' prefix.

    Args:
        table (str): The table as a multiline string.
        label_port (int): The switch number (label_port).
        lane_bmap (str): The lane_bmap value (either '0x01' or '0x80').

    Returns:
        str: The corresponding log_port value without the '0x100' prefix, e.g., '09'.

    Raises:
        ValueError: If the specified label_port and lane_bmap combination are not found in the table.
    """

    # Split the table into rows
    rows = table.splitlines()

    # Find the header row by looking for a row that contains "log_port"
    header_index = None
    for i, row in enumerate(rows):
        if "log_port" in row and "|" in row:
            header_index = i
            break

    if header_index is None:
        raise ValueError("Could not find header row with 'log_port' in table output")

    header = rows[header_index]

    # Identify column indexes based on the header
    columns = header.split("|")
    col_indexes = {
        "log_port": columns.index("  log_port"),
        "label_port": columns.index("label_port"),
        "lane_bmap": columns.index(" lane_bmap")
    }

    # Parse rows and find the matching row based on label_port and lane_bmap
    # Start from 2 rows after the header (to skip the separator line)
    for row in rows[header_index + 2:]:
        # Skip empty rows and separator lines
        if not row.strip() or row.strip().startswith('='):
            continue

        cols = row.split("|")
        try:
            row_label_port = int(cols[col_indexes["label_port"]].strip())
            row_lane_bmap = cols[col_indexes["lane_bmap"]].strip()

            # Match the given label_port and lane_bmap
            if row_label_port == label_port and row_lane_bmap == lane_bmap:
                # Extract the last two digits of log_port, remove the "0x100" prefix
                log_port_value = cols[col_indexes["log_port"]].strip()
                return log_port_value[-2:]  # Return the last two characters
        except (ValueError, IndexError):
            continue  # Skip rows with non-numeric values or insufficient columns

    raise ValueError(f"Entry not found for label_port={label_port}, lane_bmap={lane_bmap}")
