import json
import logging
import re

from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.cli_wrappers.sonic.sonic_interface_clis import SonicInterfaceCli
from ngts.cli_wrappers.nvue.nvue_json_utils import parse_nvue_json_cli_output
from ngts.constants.performance_constants import Cl_Consts
from ngts.helpers.performance.nvue_port_count import get_nvue_physical_ports_count


class NvueInterfaceCli(SonicInterfaceCli):
    """
    This class is for interface cli commands for NVOS/cumulus
    It extends SonicInterfaceCli for backwards compatability.
    """

    def __init__(self, engine, cli_obj, device=None):
        super().__init__(engine, cli_obj)
        self.engine = engine
        self.device = device
        self.cli_obj = cli_obj

    @staticmethod
    def _get_interface_mac_address(engine, interface):
        """
        Description :- Get interface mac address using the following command
        nv sh interface {interface} link -o json
        Args:
        interface :- interface name to find the mac address for.
        """
        cmd = f"nv sh interface {interface} link -o json"
        output = engine.run_cmd(cmd, print_output=False)
        return output

    def get_interface_mac_address(self, interface, verify_execution=False):
        if verify_execution:
            try:
                output = SendCommandTool.execute_command(NvueInterfaceCli._get_interface_mac_address, self.engine, interface).verify_result()
            except Exception as e:
                logging.error(self.get_interface_status())
                logging.error(f"Error getting interface mac address for {interface}: {e}")
                raise
        else:
            output = NvueInterfaceCli._get_interface_mac_address(self.engine, interface)
        output_json = json.loads(output)
        return output_json['mac-address']

    @staticmethod
    def _get_all_interfaces_mac_addresses(engine):
        output = engine.run_cmd("nv sh interface mac -o json", print_output=False)
        output_json = json.loads(output)
        mac_addresses = {}
        for interface, data in output_json.items():
            mac_addresses[interface] = data['link']['mac-address']
        return mac_addresses

    def get_all_interfaces_mac_addresses(self, validate=False):
        if validate:
            try:
                output = SendCommandTool.execute_command(NvueInterfaceCli._get_all_interfaces_mac_addresses, self.engine).verify_result()
            except Exception as e:
                logging.error(f"Error getting all interfaces mac addresses: {e}")
                raise
        else:
            output = NvueInterfaceCli._get_all_interfaces_mac_addresses(self.engine)
        return output

    def get_bonus_ports(self, engine) -> list:
        asic_model = self.cli_obj.general.get_asic_model(engine)
        bonus_ports = Cl_Consts.BONUS_PORTS[asic_model]
        logging.info(f"Bonus ports are {bonus_ports}")
        return bonus_ports

    def set_ports_admin_state(self, port_list: list, port_state):
        string_of_ports = ",".join(port_list)
        self.engine.run_cmd(f"nv set interface {string_of_ports} link state {port_state}")
        self.cli_obj.general.apply_config(self.engine, option="-y", verify_execution=True)

    def bring_all_existing_swp_ports_up(self):
        """Bring every currently exposed Cumulus switch interface administratively up.

        Returns:
            List of existing ``swp*`` interface names updated through NVUE.

        Raises:
            RuntimeError: If the switch exposes no ``swp*`` interfaces.
        """
        output = self.engine.run_cmd(
            "for port in /sys/class/net/swp*; do basename \"$port\"; done",
            print_output=False)
        ports = [port.strip() for port in output.splitlines()
                 if re.match(r"^swp\d+(?:s\d+)?$", port.strip())]
        ports = sorted(ports, key=lambda port: tuple(int(value) for value in re.findall(r"\d+", port)))
        if not ports:
            raise RuntimeError("No existing swp* interfaces found")
        for port in ports:
            self.engine.run_cmd(f"nv set interface {port} link state up")
        self.cli_obj.general.apply_config(self.engine, option="-y", verify_execution=True)
        return ports

    def get_lldp_neighbors(self, output_type="json"):
        lldp_neighbors = self.engine.run_cmd(f"nv sh interface lldp -o {output_type}", print_output=False)
        try:
            lldp_neighbors = json.loads(lldp_neighbors)
        except json.JSONDecodeError as j:
            logging.error("Invalid lldp neighbor output received.")
            raise
        return lldp_neighbors

    def get_physical_ports(self):
        output = self.engine.run_cmd("nv sh platform -o json", print_output=False)
        output = parse_nvue_json_cli_output(output)
        return get_nvue_physical_ports_count(output["asic-model"], output["port-layout"])

    def initialize_physical_ports(self):
        number_of_ports = self.get_physical_ports()
        self.engine.run_cmd(f"nv unset interface swp1-{number_of_ports} link breakout")
        self.engine.run_cmd(f"nv set interface swp1-{number_of_ports} link breakout 1x")
        self.engine.run_cmd(f"nv set interface swp1-{number_of_ports} link state up")
        self.engine.run_cmd("nv config apply -y")

    def filter_lldp_neighbors(self, neighbor_list, include_neighbor_ports=False):
        lldp_neighbor = self.cli_obj.interface.get_lldp_neighbors(output_type="json")
        filtered_neighbors = {}
        for neighbor in neighbor_list:
            if include_neighbor_ports:
                filtered_neighbors[neighbor] = {}
            else:
                filtered_neighbors[neighbor] = []
        for port, properties in lldp_neighbor.items():
            neighbor = [*properties['lldp']['neighbor'].keys()][0]
            neighbor_port = properties['lldp']['neighbor'][neighbor]['port']['name']
            if neighbor in neighbor_list:
                if include_neighbor_ports:
                    filtered_neighbors[neighbor][port] = neighbor_port
                else:
                    filtered_neighbors[neighbor].append(port)
        return filtered_neighbors

    def get_down_ports(self):
        """Return down front-panel interfaces, excluding platform bonus ports."""

        output = self.engine.run_cmd("nv sh interface down -o json", print_output=False)
        output = json.loads(output)
        bonus_ports = set(self.cli_obj.interface.get_bonus_ports(self.engine))
        return [port_name for port_name, port_data in output.items()
                if port_data.get("type") == "swp" and port_name not in bonus_ports]

    def get_interface_status(self):
        output = self.engine.run_cmd("nv sh interface status -o json", print_output=False)
        output = json.loads(output)
        return output

    @staticmethod
    def _clear_counters(engine):
        return engine.run_cmd("nv action clear interface counters")

    def clear_counters(self, validate=False):
        if validate:
            try:
                SendCommandTool.execute_command(NvueInterfaceCli._clear_counters, self.engine).verify_result()
            except Exception as e:
                logging.error(f"Error clearing counters: {e}")
                raise
        else:
            NvueInterfaceCli._clear_counters(self.engine)

    def clear_queue_counters(self):
        pass

    def get_sorted_ports_list(self, ports_list, breakout=4):
        return sorted(ports_list, key=lambda port: int(re.search(r'swp(\d+)s(\d+)', port).group(1)) * breakout + int(re.search(r'swp(\d+)s(\d+)', port).group(2)))

    def get_interface_counters(self, interface_list, counters_type="tx-drop"):
        """
        Description : Get interface counters using the following command
        nv sh interface counters -o json
        Args:
        interface_list : list of interfaces to get the counters for.
        counters_type : type of counters to get. tx-drop, rx-okay, tx-okay,
        """
        output_json = self.engine.run_cmd("nv sh interface counters -o json", print_output=False)
        output_dict = json.loads(output_json)
        counters_dict = {}
        for interface in interface_list:
            counters_dict[interface] = output_dict[interface]['counters']['netstat'][counters_type]
        return counters_dict

    def get_interface_utilization(self, interface, direction='in'):
        output = self.engine.run_cmd(f"nv sh interface {interface} rates -o json", print_output=False)
        interface_rates = json.loads(output)
        return interface_rates[f"{direction}-utilization"]

    def get_interface_queue_counters(self, interface_list, counters_type="egress-queue-stats", sub_type=["tx-bytes", "tx-frames"]):
        counters_dict = {}
        for interface in interface_list:
            output = self.engine.run_cmd(f"nv sh interface {interface} counters qos {counters_type} -o json", print_output=False)
            output_dict = json.loads(output)
            counters_dict[interface] = {}
            for queue in output_dict.keys():
                counters_dict[interface][queue] = {}
                for s_type in sub_type:
                    counters_dict[interface][queue][s_type] = output_dict[queue][s_type]
        return counters_dict
