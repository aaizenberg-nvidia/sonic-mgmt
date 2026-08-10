import logging
import os
import re
import json
from ngts.helpers.performance.traffic_helpers import generate_ip_address_dict
from ngts.constants.constants import BugHandlerConst, ResultUploaderConst
from ngts.constants.performance_constants import PerfConsts, PowerConsts, ValidationConsts
from ngts.cli_wrappers.common.performance_clis_common import PerformanceCommon
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.tools.infra import get_chip_type
from devts.infra.tools.exceptions.test_issue import TestIssue
from jinja2 import Environment, FileSystemLoader, StrictUndefined, UndefinedError, meta


class DvsPerformance(PerformanceCommon):
    PORT_STYLE = "sdk_hex"

    def __init__(self, topology_obj, engine, dut_alias, cli_obj):
        super().__init__(topology_obj, engine, dut_alias, cli_obj)
        self.base_ports, self.ports_lanes = self.get_base_ports()
        self.right_left_ports_dict = self.get_right_left_ports_dict()
        self.connected_ports, self.unconnected_ports = self.get_player_ports()
        self.original_connected_ports = self.connected_ports
        self.original_unconnected_ports = self.unconnected_ports
        self.original_port_lanes = self.ports_lanes
        self.port_groups = None

    def configure_reserved_buffer_size(self, shared_buffer_size, port_group_df,
                                       collectors_list=[ValidationConsts.COUNTERS_SAMPLES,
                                                        ValidationConsts.BW_SAMPLES]):
        self.set_configuration_file(port_group_df, shared_buffer_size, collectors_list)
        configure_shared_buffer_size_cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} --names HeadroomConfig"
        self.execute_cmd(self.get_cmd_for_sdk(configure_shared_buffer_size_cmd))

    def set_configuration_file(self, port_group_df, shared_buffer_size, collectors_list=None):
        conf_path = os.path.join(PerfConsts.CONFIG_FILES_DIR, f"{self.dut_alias}_conf.json")
        conf_json = {"sdk_test_conf": {ValidationConsts.PORT_GROUPS: port_group_df,
                                       ValidationConsts.SHARED_BUFFER_SIZE: shared_buffer_size}}
        if collectors_list:
            conf_json["sdk_test_conf"][ValidationConsts.COLLECTORS_LIST] = collectors_list
        self.save_configuration_file(conf_path, conf_json)

    def _create_sdk_dump_dirs(self):
        "This is not needed in DVS"
        pass

    def get_cmd_for_sdk(self, cmd, env_variables=None):
        """
        Returns:
        a cmd that is running on the sdk. When env_variables (e.g. SEND_FWS=0 for Simx) is set, adds export.
        """
        if env_variables:
            export_part = " ".join(f"export {v}" for v in env_variables)
            return f"{export_part} && {cmd}"
        return cmd

    def update_player_ports(self):
        self.connected_ports, self.unconnected_ports = self.get_player_ports()

    def apply_configuration_file(self, scenario, conf_args, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        # TODO: Remove this once we have a better way to undo SDK tests
        if is_redmine_issue_active([4644033])[0]:
            self.clear_syslog()

        self.base_ports, self.ports_lanes = self.get_base_ports()
        self.right_left_ports_dict = self.get_right_left_ports_dict()
        self.connected_ports, self.unconnected_ports = self.get_player_ports()
        test_name = self.get_configuration_file(scenario, conf_args, template_suite)
        logging.info(f"Configuration to be run {test_name}")
        logging.info(f"Applying the configuration on {self.dut_alias}")
        cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} --names {test_name}"
        self.execute_cmd(cmd)
        self.update_player_ports()

    def get_configuration_file(self, scenario, conf_args, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        templates_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                      template_suite, scenario, "dvs")
        json_dict = self.render_configuration_file(conf_args, templates_path)
        conf_path = os.path.join(templates_path, f"{self.dut_alias}_conf.json")
        self.save_configuration_file(conf_path, json_dict, dst_dut_dir="/tmp")
        self.port_groups = json_dict[PerfConsts.SDK_TEST_CONF][PerfConsts.PORT_GROUPS]
        return json_dict["sdk_test_info"]["sdk_test_name"]

    def _get_template_function_dict(self):
        """
        Get the standard function dictionary for Jinja2 template rendering.

        Returns:
            dict: Dictionary of functions and constants available in templates.
        """
        return {"get_split_ports": self.get_split_ports,
                "generate_ip_list": generate_ip_address_dict,
                "get_player_unconnected_connected_after_split": self.get_player_unconnected_connected_after_split,
                "PerfConsts": PerfConsts}

    def _validate_template_variables(self, template_source, template_context, template_globals):
        """
        Validate that all variables referenced in the template are available in the context.

        Args:
            template_source: The raw template source code
            template_context: Dictionary of variables passed to render()
            template_globals: Dictionary of global functions/variables in template

        Returns:
            tuple: (missing_variables, available_variables)
        """
        env = Environment()
        ast = env.parse(template_source)
        template_vars = meta.find_undeclared_variables(ast)

        complete_context = set(template_context.keys())
        complete_context.update(template_globals.keys())

        missing_vars = template_vars - complete_context
        available_vars = template_vars & complete_context

        return missing_vars, available_vars

    def _diagnose_undefined_error(self, error, env, template_name, render_context, func_dict):
        """
        Diagnose and log details about an UndefinedError during template rendering.

        Args:
            error: The UndefinedError exception
            env: The Jinja2 Environment
            template_name: Name of the template being rendered
            render_context: Dictionary of variables passed to render()
            func_dict: Dictionary of functions available in template globals
        """
        logging.error(f"UndefinedError: {error}")
        logging.error("The template tried to access a variable that doesn't exist.")

        try:
            template_source = env.loader.get_source(env, template_name)[0]
            template_globals = {**func_dict, **env.globals}
            missing_vars, available_vars = self._validate_template_variables(
                template_source, render_context, template_globals
            )

            if missing_vars:
                logging.error(f"Template references {len(missing_vars)} UNDEFINED variables:")
                for var in sorted(missing_vars):
                    logging.error(f"  - {var}")

            conf_args = render_context.get("conf_args", {})
            logging.error("Available variables in render context:")
            if isinstance(conf_args, dict):
                logging.error(f"  conf_args (dict with {len(conf_args)} keys):")
                for key in sorted(conf_args.keys()):
                    logging.error(f"    - conf_args.{key}")
            else:
                logging.error("  conf_args (not a dict)")
            logging.error(f"  dut_alias: {self.dut_alias}")
            logging.error(f"  right_left_ports_dict: {list(self.right_left_ports_dict.keys())}")
            logging.error("  Functions: " + ", ".join(sorted(func_dict.keys())))
        except Exception as analysis_error:
            logging.error(f"Could not analyze template variables: {analysis_error}")

    def _diagnose_type_error(self, error, conf_args, func_dict):
        """
        Diagnose and log details about a TypeError during template rendering.

        Args:
            error: The TypeError exception
            conf_args: Configuration arguments dictionary
            func_dict: Dictionary of functions available in template globals
        """
        logging.error(f"TypeError during template rendering: {error}")
        logging.error("This likely means:")
        logging.error("  1. A function in the template is trying to JSON serialize an Undefined value")
        logging.error("  2. The '| to json' filter is being applied to a non-serializable object")

        logging.error("Checking conf_args for non-JSON-serializable values...")
        non_serializable_count = 0
        if isinstance(conf_args, dict):
            for key, value in conf_args.items():
                try:
                    json.dumps({key: value})
                except (TypeError, ValueError) as check_error:
                    non_serializable_count += 1
                    logging.error(f"  conf_args['{key}'] is not JSON serializable: {type(value)} - {check_error}")

        if non_serializable_count:
            logging.error(f"Found {non_serializable_count} non-serializable values in conf_args")
        else:
            logging.error("All conf_args values appear to be JSON serializable")

        logging.error(f"\nTemplate functions available: {list(func_dict.keys())}")
        logging.error(f"conf_args keys: {list(conf_args.keys()) if isinstance(conf_args, dict) else 'Not a dict'}")

    def _diagnose_json_decode_error(self, error, template_string):
        """
        Diagnose and log details about a JSONDecodeError after template rendering.

        Args:
            error: The JSONDecodeError exception
            template_string: The rendered template string that failed to parse
        """
        logging.error(f"JSON parsing error at position {error.pos}: {error.msg}")
        # Show context around error
        start = max(0, error.pos - 150)
        end = min(len(template_string), error.pos + 150)
        context = template_string[start:end]
        logging.error(f"Context around error:\n{context}")
        logging.error(f"\nFull rendered template:\n{template_string}")

    def check_template_requirements(self, scenario, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        """
        Utility method to check what variables a template requires without rendering it.
        Useful for debugging and documentation.

        Args:
            scenario: Scenario name (e.g., "alibaba_performance")
            template_suite: Template suite directory name

        Returns:
            dict: Dictionary with template requirements
                {
                    'template_name': str,
                    'required_variables': set,
                    'available_functions': list
                }
        """
        templates_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                      template_suite, scenario, "dvs")
        template_name = f"{self.dut_alias}.jinja"

        env = Environment(loader=FileSystemLoader(templates_path))
        template_source = env.loader.get_source(env, template_name)[0]
        ast = env.parse(template_source)
        required_vars = meta.find_undeclared_variables(ast)

        func_dict = self._get_template_function_dict()

        return {
            'template_name': template_name,
            'template_path': os.path.join(templates_path, template_name),
            'required_variables': required_vars,
            'available_functions': list(func_dict.keys()),
            'context_variables': ['conf_args', 'dut_alias', 'right_left_ports_dict']
        }

    def render_configuration_file(self, conf_args, templates_path):
        """
        Render Jinja2 configuration template with comprehensive error handling.

        This method renders templates and provides detailed error messages if rendering fails.
        Validation checks are only performed on failure to avoid overhead during successful runs.

        Args:
            conf_args (dict): Dictionary of configuration arguments
            templates_path (str): Path to directory containing Jinja2 templates

        Returns:
            dict: Parsed JSON configuration

        Raises:
            UndefinedError: If template references undefined variables
            json.JSONDecodeError: If rendered template is not valid JSON
            TypeError: If conf_args contains non-JSON-serializable values
        """
        template_name = f"{self.dut_alias}.jinja"
        logging.info(f"Rendering template: {template_name} from {templates_path}")

        env = Environment(loader=FileSystemLoader(templates_path), undefined=StrictUndefined)
        jinja_template = env.get_template(template_name)

        func_dict = self._get_template_function_dict()
        jinja_template.globals.update(func_dict)

        render_context = {
            "conf_args": conf_args,
            "dut_alias": self.dut_alias,
            "right_left_ports_dict": self.right_left_ports_dict
        }

        try:
            template_string = jinja_template.render(**render_context)
            logging.info(f"Template rendered successfully, length: {len(template_string)} characters")

        except UndefinedError as e:
            self._diagnose_undefined_error(e, env, template_name, render_context, func_dict)
            raise

        except TypeError as e:
            self._diagnose_type_error(e, conf_args, func_dict)
            raise

        except Exception as e:
            logging.error(f"Unexpected error during template rendering: {type(e).__name__}: {e}")
            raise

        try:
            json_dict = json.loads(template_string)
            logging.info(f"JSON parsed successfully, {len(json_dict)} top-level keys")
            return json_dict

        except json.JSONDecodeError as e:
            self._diagnose_json_decode_error(e, template_string)
            raise

    def get_device_configuration(self, conf_args, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        conf_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                 template_suite, conf_args["scenario"], "dvs", f"{self.dut_alias}_conf.json")
        with open(conf_path, "r") as f:
            conf_json = json.load(f)
        return conf_json

    def save_configuration_file(self, conf_path, conf_json, dst_dut_dir="/tmp"):
        with open(conf_path, "w") as f:
            json.dump(conf_json["sdk_test_conf"], f)
        file_name = "conf.json"
        conf_json_path = os.path.join(dst_dut_dir, file_name)
        logging.info(f"Copy Configuration JSON to : {conf_json_path} on {self.dut_alias}")
        self.engine.copy_file(source_file=conf_path, file_system=dst_dut_dir, dest_file=file_name,
                              overwrite_file=True, verify_file=False)

    def save_basic_configuration(self, players):
        pass

    def restore_basic_configuration(self):
        self.cleanup_shared_json_file()
        switch_attributes = self.topology_obj.players['dut']['attributes'].noga_query_data['attributes']
        chip_type = get_chip_type(switch_attributes)

        restart_cmd = "dvs_stop.sh && dvs_start.sh --sdk_bridge_mode=HYBRID --boot_mode=NORMAL"
        if chip_type == "SPC6":
            restart_cmd += f" --custom_config_file={PerfConsts.SPC6_DVS_CUSTOM_CONFIG_FILE}"
        self.execute_cmd(restart_cmd)

        # TODO: Remove this once we have a better way to undo SDK tests
        if is_redmine_issue_active([4644033])[0]:
            self.clear_syslog()

        if chip_type == "SPC5":
            self.unsplit_all_ports()
        self.connected_ports = self.original_connected_ports
        self.unconnected_ports = self.original_unconnected_ports
        self.ports_lanes = self.original_port_lanes

    def clear_syslog(self):
        """
        Clear the syslog file to prevent issues with SDK tests.

        This is a workaround for Redmine issue #4644033.
        """
        self.execute_cmd('> /var/log/syslog')

    def set_ports(self, ports_list, port_state):
        for port in ports_list:
            set_port_cmd = f"echo y |  sx_api_port_state_set.py --log_port {hex(port)} --state {port_state}"
            self.execute_cmd(set_port_cmd)

    def get_player_ports(self, dst_dut_dir="/tmp"):
        """Classify switch ports as connected or unconnected by parsing sx_api_ports_dump.py output.

        Runs sx_api_ports_dump.py on the switch, which produces a table like:

            | log_port | dev | local | phy | ... | oper  | module   |
            | 0x10001  |  1  |   1   |  0  | ... | DOWN  | UNPLUGGED|
            | 0x10041  |  1  |  65   | 64  | ... | UP    | PLUGGED  |

        The regex extracts (log_port, oper_state, module_state) from each row.
        Ports with module_state == "PLUGGED" are classified as connected (cable present),
        all others as unconnected. ASIC-specific bonus ports are excluded from the tail.

        The result is also saved as tg_ports.json on the switch and locally as
        {dut_alias}_ports.json.

        Args:
            dst_dut_dir: by default /tmp, where the file tg_ports.json is saved

        Returns:
            tuple: (connected_ports, unconnected_ports) where each is a sorted
                   list of SDK logical port numbers (int).
        """
        bonus_ports_to_remove = self.get_bonus_ports_to_remove()
        ports_dump = self.execute_cmd("sx_api_ports_dump.py")
        self.connected_ports, self.unconnected_ports = self._parse_ports_dump(
            ports_dump, bonus_ports_to_remove
        )
        player_ports = {"connected_ports": self.connected_ports,
                        "unconnected_ports": self.unconnected_ports}
        self._save_player_ports(player_ports, dst_dut_dir)
        return self.connected_ports, self.unconnected_ports

    def get_player_unconnected_connected_ports_aliases(self):
        """
        Returns:
        A dict of connected and unconnected ports aliases, i.e,
        {"unconnected_ports": [('left_tg_unconnected_port_p1', '0x10001'), ...,],
        "connected_ports": [('left_tg_connected_port_p1', '0x10041'),...,]
        }
        """
        player_ports_aliases_dict = {"unconnected_ports": [], "connected_ports": []}
        player_ports_aliases_dict["unconnected_ports"] = [
            (f"{self.dut_alias}_unconnected_port_p{port_index}", unconnected_port)
            for port_index, unconnected_port in enumerate(self.get_sdk_ports(self.unconnected_ports), start=1)
        ]
        player_ports_aliases_dict["connected_ports"] = [
            (f"{self.dut_alias}_connected_port_p{port_index}", connected_port)
            for port_index, connected_port in enumerate(self.get_sdk_ports(self.connected_ports), start=1)
        ]
        return player_ports_aliases_dict

    def get_player_unconnected_connected_after_split(self, split_num):
        """Get lists of unconnected and connected ports after port splitting.

        This method takes a split number and returns two lists of ports after applying
        the split configuration. It first gets the original unconnected and connected ports,
        then applies the breakout configuration based on the split number.

        Args:
            split_num (int): The number to split the ports by (e.g., 2 for 2x split, 4 for 4x split)

        Returns:
            dict: A dictionary containing two lists of ports after splitting:
                {
                    "unconnected_ports": ['65537', '65539',...],  # Ports not connected to DUT
                    "connected_ports": ['65665', '65667',...]      # Ports connected to DUT
                }
        """
        all_ports_after_split = {}
        all_ports_after_split['unconnected_ports'] = [int(port) for port in self.get_all_breakout_ports(split_num, self.unconnected_ports)]
        all_ports_after_split['connected_ports'] = [int(port) for port in self.get_all_breakout_ports(split_num, self.connected_ports)]
        return all_ports_after_split

    def get_tg_unconnected_ports(self):
        return self.unconnected_ports

    def get_dut_ports(self):
        return self.connected_ports

    def get_os_ports_name_mapping(self):
        """
        This method should be implemented in child class
        Returns:
        a list of dicts with os port name for each port
        i.e,
        [{'osPortName': 'Ethernet0', 'port': '0x100f1'},...]
        """
        os_ports_name_mapping = []
        dut_ports = self.get_dut_ports()
        sdk_ports = self.get_sdk_ports(dut_ports)
        for port in sdk_ports:
            os_ports_name_mapping.append({ValidationConsts.PORT: port,
                                          ValidationConsts.OS_PORT_NAME: port})
        return os_ports_name_mapping

    def get_bonus_ports_to_remove(self):
        """
        Get the number of bonus ports to remove from the end of the port list.

        Different chip types have different numbers of management/bonus ports
        that should not be used for testing.

        Returns:
            int: Number of ports to remove (0 for SPC1/2/3, 1 for SPC4, 2 for SPC5/6)

        Raises:
            TestIssue: If chip type is not recognized
        """
        switch_attributes = self.topology_obj.players['dut']['attributes'].noga_query_data['attributes']
        chip_type = get_chip_type(switch_attributes)

        bonus_ports_map = {
            "SPC1": 0,
            "SPC2": 0,
            "SPC3": 0,
            "SPC4": 1,
            "SPC5": 2,
            "SPC6": 2
        }

        if chip_type not in bonus_ports_map:
            raise TestIssue(f"Invalid chip type: {chip_type}")

        return bonus_ports_map[chip_type]

    def get_base_ports(self):
        """
        Returns:
        A list of tuples of sdk port and label without last port which is mgmt port, i.e,
        [('0x10001',1), ('0x10003',2),...]
        """
        port_dump = self.execute_cmd('sx_api_ports_dump.py')
        port_label_tuple_list = re.findall(r"\|\s+(0x\d*\w*\d*)\|\s+\d+\|\s+\d+\|\s+(\d+)\|", port_dump)

        bonus_ports_to_remove = self.get_bonus_ports_to_remove()
        if bonus_ports_to_remove > 0:
            port_label_tuple_list = port_label_tuple_list[:-bonus_ports_to_remove]
        ports_lanes = self.get_ports_lanes(port_label_tuple_list)

        self.base_ports = port_label_tuple_list
        self.ports_lanes = ports_lanes

        return self.base_ports, self.ports_lanes

    @staticmethod
    def get_ports_lanes(port_label_tuple_list):
        """
        Returns:
        A dict with which port number of lanes:
        {'0x10001': 4, '0x10003': 4, ...}
        """
        ports_lanes = {}
        for idx, (port, label) in enumerate(port_label_tuple_list[:-1]):
            nxt_port, nxt_label = port_label_tuple_list[idx + 1]
            ports_lanes[port] = abs(int(port, 16) - int(nxt_port, 16))
        if len(port_label_tuple_list) >= 2:
            last_port = port_label_tuple_list[-1][0]
            prev_port = port_label_tuple_list[-2][0]
            ports_lanes[last_port] = ports_lanes[prev_port]
        return ports_lanes

    def get_right_left_ports_dict(self):
        """
        Returns:
        A dict of ports in the dut connect to the right TG and left TG, i.e,
        {'right_ports': ['0x10001', '0x10003', ...,], 'left_ports': ['0x10041', '0x10043',...]}
        """
        ports = {}
        half_ports_num = len(self.base_ports) // 2
        sorted_base_ports_by_label = sorted(self.base_ports, key=lambda port_label_tuple: int(port_label_tuple[1]))
        sorted_base_ports = list(map(lambda port_label_tuple: port_label_tuple[0], sorted_base_ports_by_label))
        ports["left_ports"] = sorted_base_ports[:half_ports_num]
        ports["right_ports"] = sorted_base_ports[half_ports_num:]
        return ports

    def get_player_left_right_ports_aliases(self):
        """
        Returns:
        A dict of left and right ports aliases, i.e,
        {'right_ports': [('right_port_p1', '0x10001'), ...,],
        'left_ports': [('left_port_p1', '0x10041'),...,]
        }
        """
        port_alias_regex = r"((left|right)_port)"
        player_ports = self.get_right_left_ports_dict()
        player_ports_aliases_dict = {"left_ports": [], "right_ports": []}
        for ports_aliases, port_list in player_ports.items():
            port_alias = re.search(port_alias_regex, ports_aliases).group(1)
            for port_index, port in enumerate(port_list, start=1):
                player_ports_aliases_dict[ports_aliases].append((f"{self.dut_alias}_{port_alias}_p{port_index}",
                                                                 port))
        return player_ports_aliases_dict

    def get_split_ports(self, right_split_num, left_split_num):
        """
        TODO: separate the split and test configuration, and build the topology object after the split was made
        Args:
            right_split_num: i.e, 2
            left_split_num: i.e, 4

        Returns:
        A dict with the left / right ports after the split,
            {"right_split_ports": ["65537", "65539",...],
             "left_split_ports": ["65665", "65667",...]
             }
        """
        split_ports = {}
        split_ports["left_split_ports"] = self.get_all_breakout_ports(left_split_num, self.right_left_ports_dict["left_ports"])
        split_ports["left_split_ports"].sort()
        split_ports["right_split_ports"] = self.get_all_breakout_ports(right_split_num, self.right_left_ports_dict["right_ports"])
        split_ports["right_split_ports"].sort()

        return split_ports

    def get_all_breakout_ports(self, split_num, ports_list):
        breakout_ports = []
        for port in ports_list:
            lane = self.ports_lanes[self.get_sdk_port(port)]
            num_lanes_after_breakout = lane // int(split_num)
            lanes_after_breakout = list(range(0, lane, num_lanes_after_breakout))
            breakout_ports += [str(int(self.get_sdk_port(port), 16) + lane) for lane in lanes_after_breakout]
        return breakout_ports

    def get_traffic_parameters(self, scenario, conf_args, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        """

        Args:
            scenario: name of scenario, i.e, spcx_ra
            conf_args: a dict with the configuration arguments per test

        Returns:
        player traffic params based on the scenario configuration file, i.e,
        {
                "MAC" : {"src" : "aa:bb:cc:dd:ee:ff", "dst" : "aa:bb:cc:dd:ee:ff"},
                "IP   : {"src" : "11.11.11.11", "dst" : "22:22:22:22"},
                -------------    OR   -------------
                "IP"  :  {"src" : "1::1", "dst" : "2::2"}
                "UDP" : {"src": int, "dst": int},
                "AR"  : 0/1
            }
        """
        conf_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                 template_suite, scenario, "dvs", f"{self.dut_alias}_conf.json")
        with open(conf_path) as f:
            conf_json = json.load(f)

        tg_regex = PerfConsts.TG_REGEX
        tg_alias = re.search(tg_regex, self.dut_alias).group(1)
        traffic_parameters = {
            "ports": self.get_tg_unconnected_ports(),
            "MAC": {"src": conf_json["smac"], "dst": conf_json[f"{tg_alias}_mac"]},
            "IP": {"src": conf_json["source_ip"], "dst": conf_json[f"{tg_alias}_dst_ip"]},
            "UDP": {"src": PerfConsts.UDP_SOURCE_PORT, "dst": PerfConsts.ROCE_PORT},
            "AR": PerfConsts.ADAPTIVE_ROUTING_ENABLED,
            "packet_size": conf_args["packet_size"],
            "num_packets": conf_args[f"{tg_alias}_num_packets"],
            "is_ipv6": conf_args["is_ipv6"]
        }
        return traffic_parameters

    def set_ibm(self, scenario, conf_args, chip_type):
        return

    def get_sdk_ports(self, ports_list):
        """

        Args:
            ports_list: a list of ports, i.e, ['0x10001','0x10003',..] or
            [65573,65578,...]

        Returns:
            return the list of ports as sdk ports in hex format, i.e, ['0x10001','0x10003',..]
        """
        return list(map(self.get_sdk_port, ports_list))

    def get_sdk_port(self, port):
        """
        Args:
            port: any sdk port identifier, "65537" or '0x10001'

        Returns:
            sdk port in hex format, i.e, "0x10001"
        """
        if isinstance(port, str) and port.startswith('0x'):
            return port
        elif isinstance(port, str):
            port = int(port)
        if isinstance(port, int):
            return hex(port)

    @staticmethod
    def get_controllers_info_dicts_list(sensors_output):
        """
        returns voltage/current per controller
        Args:
            sensors_output: a string with the output of sensors command

        Returns:
        A list of dicts, each dict contains the values of a controller on the device i.e,
        [{'vout1': 1.20, 'vout2': 1.20, 'iout1': 13.00, 'iout2': 94.00},...]
        """
        controllers_info_dicts_list = []
        controllers_info_str_list = re.split(PowerConsts.CONTROLLER_REGEX, sensors_output)
        for controller_info_str in controllers_info_str_list:
            controller_info_list = controller_info_str.splitlines()
            if controller_info_list:
                controllers_info_dict = {}
                for controller_info in controller_info_list:
                    controller_info_parsed = re.search(r"((vout|iout)\d):\s+(\d*\.*\d+)\s(\w*)", controller_info)
                    if controller_info_parsed:
                        key = controller_info_parsed.group(1)
                        value = controller_info_parsed.group(3)
                        measure_unit = controller_info_parsed.group(4)
                        controllers_info_dict[key] = float(value) / 1000 if 'm' in measure_unit else float(value)
                controllers_info_dicts_list.append(controllers_info_dict)
        return controllers_info_dicts_list

    def get_dut_system_information(self, session_id, setup_name):
        """

        Args:
            session_id: Mars session id, i.e, 9443960
            setup_name: i.e, nv_performance_mtvr-moose-17

        Returns: a dictionary with the full dut system information, .i.e,
         "dutSystemInformation": {
                "marsSessionId": "9438676",
                "setupName": "nv_performance_mtvr-moose-17",
                "osType": "DVS",
                "chip": "SPECTRUM4",
                "board": "sn5600",
                "sdkVersion": "4.7.3094-003",
                "hwChassisRev": "AJ",
                "modelNumber": "MSN-9N402-00RI-7N0_Ax",
                "hostDetails": "mtvr-moose-17, IP N/A",
                "serialNumber": "MT2443J011Q7",
                "onieVersion": "2023.11-5.3.0012-115200",
                "psid": "MT_0000000955",
                "osVersion": "dvs-os-sonic_4.7.1920_DEV_LK6.1.38_x86_64---2024-09-09 10:43:49"
            }
        """
        cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} -si"
        output = self.execute_cmd(cmd)
        dut_system_information = {"marsSessionId": session_id,
                                  "setupName": setup_name,
                                  "osType": "DVS"}
        regex_dict = {
            "chip": r"Platform String\s*=\s*(SPECTRUM\d+).*",
            "board": r"Board\s*=\s*(.*)",
            "sdkVersion": r"SDK Version\s*=\s*ETH\s*([\d|.|-]*)\s*\(FROM .*\)",
            "fwVersion": r"FW\/SimX Version\s*=\s*([\d|_|-]*)\s*\(FROM .*\)",
            "hwChassisRev": r"HW Chassis Rev\s*=\s*(.*)",
            "modelNumber": r"Part Number\s*=\s*(.*)",
            "hostDetails": r"Host Details\s*=\s*(.*)",
            "serialNumber": r"Serial Number\s*=\s*(.*)",
            "onieVersion": r"ONIE Version\s*=\s*(.*)",
            "psid": r"PSID\s*=\s*(.*)",
            "osVersion": r"DVS\/OPT_OS\s*=\s*(.*)"
        }
        for key, regex in regex_dict.items():
            match = re.search(regex, output)
            if match:
                dut_system_information[key] = match.group(1)
        self.modify_board_host_internal_name(output, regex_dict, dut_system_information)
        return dut_system_information

    @staticmethod
    def modify_board_host_internal_name(output, regex_dict, dut_system_information):
        match = re.search(regex_dict["board"], output)
        if match:
            dut_system_information["board"] = ResultUploaderConst.HOST_INTERNAL_NAMES_MAP[match.group(1).lower()]

    def unsplit_all_ports(self):
        """
        Override base class method to unsplit all ports.
        This is needed because SPC5 comes up with ports already split after dvs_start.sh
        """
        logging.info("Unsplit all ports")
        unsplit_ports_cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} --names {PerfConsts.DVS_UNSPLIT_ALL_PORTS}"
        self.execute_cmd(unsplit_ports_cmd)
        self.update_player_ports()
        self.get_base_ports()
        self.right_left_ports_dict = self.get_right_left_ports_dict()
        self.original_connected_ports = self.connected_ports
        self.original_unconnected_ports = self.unconnected_ports
        self.original_base_ports = self.base_ports
        self.original_port_lanes = self.ports_lanes

    def dynamic_configuration_helper(self, scenario, performance_parameters):
        """
        This method is used to apply the dynamic configuration on the dut
        """
        if not performance_parameters:
            logging.warning("No performance parameters provided for dynamic configuration")
            logging.warning(f"Continuing with the default {scenario} configuration")
            return

        config_file = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests", "performance_config_templates", scenario, "dvs", f"{self.dut_alias}_dynamic_conf.json")
        with open(config_file, "w") as f:
            json.dump(performance_parameters, f, indent=4)

        # Copy the file to the target system
        self.engine.copy_file(
            source_file=config_file,
            file_system="/tmp",
            dest_file=f"{self.dut_alias}_dynamic_conf.json",
            overwrite_file=True,
            verify_file=False
        )

        get_dynamic_conf_cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} --names {PerfConsts.DVS_DYNAMIC_CONF_PREFIX}{scenario}"
        self.execute_cmd(get_dynamic_conf_cmd)

    def configure_incremental_dips_on_tg(self):
        """
        This method is used to create the incremental dips on the traffic generator
        """
        logging.info(f"Create incremental dips on {self.dut_alias}")
        cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} --names {PerfConsts.DVS_CREATE_INCREMENTAL_DIPS}"
        self.execute_cmd(cmd)
