import copy
import logging
import os
import json
import random
import re
import pandas as pd
from retry import retry
from collections import defaultdict
from devts.infra.tools.exceptions.test_issue import TestIssue
from jsonmerge import merge
import allure
from ngts.helpers.system_helpers import copy_files_to_syncd
from ngts.constants.constants import BugHandlerConst, InfraConst, CliType, SonicConst, ConfigDbJsonConst, \
    IndependentModuleConst
from ngts.constants.performance_constants import PerfConsts, PowerConsts, ValidationConsts, MRCConsts, MongoDbConsts, PortMappingOptionsConsts
from ngts.cli_wrappers.common.performance_clis_common import PerformanceCommon
from ngts.helpers.performance.port_selection import PortSelection
from ngts.helpers.performance.sensors_power_parse import (
    build_controllers_info_dicts_list,
    get_controllers_info_str_list as sensors_get_controllers_info_str_list,
    get_sensors_output_key as sensors_get_sensors_output_key,
    parse_sensor_line as sensors_parse_sensor_line,
)
from ngts.helpers.interface_helpers import get_alias_letter, get_alias_number, convert_letter_to_idx
from ngts.helpers.performance.traffic_helpers import generate_ip_address_dict, generate_mac_range
from ngts.helpers.config_db_utils import save_config_db_json
from jinja2 import Environment, FileSystemLoader
from ngts.helpers.performance.traffic_helpers import is_ipv6, pick_random_non_consecutive_ports
from ngts.performance_tests.srv6.utils.srv6_traffic_patterns import get_ingress_egress_ports_pairing_for_debug
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active


class SonicPerformanceCli(PerformanceCommon):
    """
    This class is for Performance cli commands for sonic only
    """
    PORT_STYLE = "ethernet"

    def __init__(self, topology_obj, engine, dut_alias, cli_obj):
        self.topology_obj = topology_obj
        self.engine = engine
        self.dut_alias = dut_alias
        self.hostname = self.topology_obj.players[self.dut_alias]['attributes'].noga_query_data['attributes']['Common'][
            'Name']
        self.cli_obj = cli_obj
        self.chip_type = \
            self.topology_obj.players[self.dut_alias]['attributes'].noga_query_data['attributes']['Specific']['chip_type']
        self.mac = self.topology_obj.players[self.dut_alias]['attributes'].noga_query_data['attributes']['Specific'][
            'mac_address'].lower()
        self.service_port_idx = -1
        self.service_ports = []
        self.sonic_to_sdk_ports_dict = {}
        self.sdk_to_sonic_ports_dict = {}
        self.sonic_ports_aliases_dict = {}
        self.connected_ports, self.unconnected_ports = [], []
        self.mloops = []
        self.port_groups = {}
        self.port_selection = PortSelection(port_style=self.PORT_STYLE)
        self._port_selection_params = None
        self.excluded_port_names = set()

    def validate_no_drops_on_tg_ports(self):
        violations = []
        with allure.step(f"Validate no drops on TG ports on {self.dut_alias}"):
            self.execute_cmd("show interfaces counters")
            counters_dict = self.cli_obj.interface.parse_interfaces_counters()
            self.validate_tg_ports(self.connected_ports, counters_dict, "Connected", violations)
            self.validate_tg_ports(self.unconnected_ports, counters_dict, "Unconnected (Mloop)", violations)
        return violations

    def validate_tg_ports(self, ports_list, counters_dict, port_type, violations):
        for port in ports_list:
            tx_drop_counter = counters_dict[port]['TX_DRP'].replace(",", "")
            if int(tx_drop_counter) > 0:
                violations.append(
                    f"{port_type} Port {port} on {self.dut_alias}-{self.hostname} has {tx_drop_counter} TX_DRP")

    def get_cmd_for_sdk(self, cmd, env_variables=[]):
        docker_exec_syncd_cmd = InfraConst.DOCKER_EXEC_BASH_CMD.format(DOCKER=InfraConst.SYNCD_DOCKER)
        variables = " ".join(env_variables)
        new_cmd = f"{docker_exec_syncd_cmd} '{PerfConsts.EXPORT_PYTHONPATH} {variables} && {cmd}'"
        return new_cmd

    def get_sensors_data(self):
        """Collect all SPC6 sensors; keep legacy bus-5 collection for older chips."""
        if self.chip_type == "SPC6":
            return self.execute_cmd(PerfConsts.SPC6_SENSORS_CMD)
        return super().get_sensors_data()

    def get_controllers_info_dicts_list(self, sensors_output):
        """
        returns voltage/current per controller
        Args:
            sensors_output: a string with the output of sensors command

        Returns:
        A list of dicts, each dict contains the values of a controller on the device i.e,
        [{'vout1': 1.20, 'vout2': 1.20, 'iout1': 13.00, 'iout2': 94.00},...]
        """
        return build_controllers_info_dicts_list(sensors_output)

    @staticmethod
    def _parse_sensor_line(line):
        """Delegate to :mod:`ngts.helpers.performance.sensors_power_parse` (kept for callers)."""
        return sensors_parse_sensor_line(line)

    @staticmethod
    def get_sensors_output_key(key, measure_unit):
        return sensors_get_sensors_output_key(key, measure_unit)

    @staticmethod
    def get_controllers_info_str_list(sensors_output):
        return sensors_get_controllers_info_str_list(sensors_output)

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
        chip_type = self.topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific'][
            'chip_type'].replace("SPC", "SPECTRUM")
        dut_hostname = self.topology_obj.players[self.dut_alias]['attributes'].noga_query_data['attributes']['Common'][
            'Name']
        fw_info = self.cli_obj.chassis.get_fw_info()
        platform_summary_info = self.cli_obj.chassis.show_platform_summary()
        platform_syseeprom_info = self.cli_obj.chassis.show_platform_syseeprom()
        dut_system_information = {"marsSessionId": session_id,
                                  "setupName": setup_name,
                                  "osType": CliType.SONIC,
                                  "chip": chip_type,
                                  "osVersion": self.cli_obj.general.get_image_sonic_version(only_branch=False,
                                                                                            short_version=True),
                                  "sdkVersion": self.cli_obj.general.get_sdk_version(),
                                  "hostDetails": f"{dut_hostname}, {self.engine.ip}"}
        full_info = "\n".join([fw_info, platform_summary_info, platform_syseeprom_info])
        regex_dict = {
            "board": r"Product\s+Name.*(SN\d+)",
            "fwVersion": r"FW\s+Version:\s+([\d|\.]*)",
            "hwChassisRev": r"Hardware\s+Revision:\s+(.*)",
            "modelNumber": r"Part\s+Number\s+[\d|x]*\s+\d+\s+(.*)",
            "serialNumber": r"Serial\s+Number\s+[\d|x]*\s+\d+\s+(.*)",
            "onieVersion": r"ONIE Version\s+[\d|x]*\s+\d+\s+(.*)",
            "psid": r"PSID:\s+(.*)",
        }
        for key, regex in regex_dict.items():
            match = re.search(regex, full_info)
            if match:
                dut_system_information[key] = match.group(1)
        updated_fw_version = dut_system_information["fwVersion"].replace(".", "_")
        dut_system_information["fwVersion"] = updated_fw_version
        return dut_system_information

    def get_test_specific_values(self, testname):
        """
        Args:
            testname: returns all the test values stored during the test run,
            and adds any OS specific info

        Returns:
            a json obj with the test info, i.e,
            {
                "testName": "test_ar_perf_max_bandwidth[4096-IPv6]",
                "timeStamp": "23-02-2025 14:09:25",
                "configurationName": "x2_400G", ...
            }
        """
        with open(os.path.join(PerfConsts.REQUIRMENTS_DIR, f"{testname}_info_dump.json")) as f:
            test_specific_values = json.load(f)
            platform_summary_info = self.cli_obj.chassis.show_platform_summary()
            hwsku = re.search(r"HwSKU: (.*)", platform_summary_info).group(1)
            test_specific_values["hwsku"] = hwsku
        return test_specific_values

    def apply_configuration_file(self, scenario, conf_args, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        with allure.step(f'Apply SKU {conf_args["hwsku"]} on {self.dut_alias}'):
            self.apply_sku(conf_args["hwsku"], conf_args["dut"], conf_args["chip_type"])

        self.load_qos_config_on_tg()

        with allure.step(f'Apply Test Configuration on {self.dut_alias}'):
            self.get_configuration_file(scenario, conf_args, template_suite)

        self.disable_im_on_tg()
        self.load_qos_config_on_dut()

        with allure.step(f'Reload SKU and Test configuration on {self.dut_alias}'):
            self.cli_obj.general.reload_configuration(force=True)
            self.cli_obj.general.verify_dockers_are_up()

        with allure.step(f'Install Traffic generator on {self.dut_alias}'):
            self.cli_obj.general.install_traffic_generator()
        self.sonic_to_sdk_ports_dict, self.sdk_to_sonic_ports_dict = self.get_sonic_to_sdk_port_mapping()
        self.add_ports_connectivity_to_dut(conf_args)
        self.port_groups = self.get_right_left_ports_dict()
        self.sonic_ports_aliases_dict = self.cli_obj.interface.parse_ports_aliases_on_sonic()

    def disable_im_on_tg(self):
        """
        Disables Independent module on traffic generators otherwise unconnected ports
        won't be up after mloop configuration
        """
        if self.dut_alias in PerfConsts.TG_ALIAS_LIST:
            with allure.step(f'Disable IM on {self.dut_alias}'):
                self.cli_obj.general.reload_configuration(force=True)
                self.cli_obj.general.verify_dockers_are_up()
                self.disable_im()

    def load_tg_custom_buffer_conf(self, updated_config_db, templates_path):
        """
        This function is used to get the custom buffer configuration for the traffic generator
        """
        env = Environment(loader=FileSystemLoader(templates_path))
        template_name = "tg_custom_buffer_conf.jinja"
        jinja_template = env.get_template(template_name)
        template_string = jinja_template.render(connected_ports=self.connected_ports,
                                                unconnected_ports=self.unconnected_ports)
        tg_custom_buffer_conf = json.loads(template_string)
        for key, dict_value in tg_custom_buffer_conf.items():
            for inner_dict_key, inner_dict_value in dict_value.items():
                updated_config_db[key][inner_dict_key] = inner_dict_value
        return updated_config_db

    def load_qos_config_on_dut(self):
        """
        Without loading the QOS configuration the dut won't have
        the dscp to tc port mapping determined by the SKU.
        """
        if self.dut_alias in PerfConsts.PERF_SETUP_DUT_ALIASES:
            with allure.step(f'Load QoS configuration on {self.dut_alias}'):
                self.cli_obj.general.reload_configuration(force=True)
                self.cli_obj.general.verify_dockers_are_up()
                self.cli_obj.qos.reload_qos(no_dynamic=True)
                self.cli_obj.general.save_configuration()

    def load_qos_config_on_tg(self):
        """
        Loads the QoS configuration on the traffic generator
        """
        if self.dut_alias in PerfConsts.TG_ALIAS_LIST:
            with allure.step(f'Load QoS configuration on {self.dut_alias}'):
                self.cli_obj.general.reload_configuration(force=True)
                self.cli_obj.general.verify_dockers_are_up()
                self.cli_obj.qos.reload_qos()
                self.cli_obj.general.save_configuration()

    def get_configuration_file(self, scenario, conf_args, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        templates_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                      template_suite, scenario, "sonic")
        env = Environment(loader=FileSystemLoader(templates_path))
        template_name = f"{self.dut_alias}.jinja" if self.dut_alias in PerfConsts.PERF_SETUP_DUT_ALIASES else "tg.jinja"
        jinja_template = env.get_template(template_name)
        func_dict = {"generate_ip_address_dict": generate_ip_address_dict,
                     "get_right_left_ports_dict": self.get_right_left_ports_dict
                     }
        jinja_template.globals.update(func_dict)
        self.mloops = self.get_mloops_tuples_list()
        vlan_conf = {}
        for idx, mloop in enumerate(self.mloops):
            vlan_conf[100 + idx * 10] = mloop
        template_string = jinja_template.render(connected_ports=self.connected_ports,
                                                unconnected_ports=self.unconnected_ports, vlan_conf=vlan_conf,
                                                dut_alias=self.dut_alias)
        json_dict = json.loads(template_string)
        hwsku_json = self.cli_obj.general.get_config_db()
        updated_config_db = merge(json_dict, hwsku_json)
        self.update_mac(updated_config_db)
        if self.dut_alias in PerfConsts.TG_ALIAS_LIST:
            updated_config_db = self.load_tg_custom_buffer_conf(updated_config_db, templates_path)
        conf_path = os.path.join(templates_path, f"{self.dut_alias}_config_db.json")
        self.save_configuration_file(conf_path, updated_config_db, dst_dut_dir="/tmp")
        return conf_path

    def get_device_configuration(self, scenario, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        conf_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                 template_suite, scenario, "sonic", f"{self.dut_alias}_config_db.json")
        with open(conf_path, "r") as f:
            conf_json = json.load(f)
        return conf_json

    def apply_sku(self, sku, sku_type, chip_type):
        hwsku_json = self.cli_obj.general.load_sku_init_conf(sku)
        self.update_sku(sku, hwsku_json)
        self.update_connected_unconnected_ports(sku, hwsku_json, sku_type, chip_type)

    def get_spine_downstream_ports(self, sku, sku_ports_dict):
        spine_downstream_ports = []
        upstream_port_speed = SonicConst.T1_SKU_UPSTREAM_PORTS_SPEED[sku]
        for port, port_dict in sku_ports_dict.items():
            if port_dict[ConfigDbJsonConst.SPEED] != upstream_port_speed:
                spine_downstream_ports.append(port)
        return spine_downstream_ports

    def set_class_vars(self):
        """
        This method sets the class variables for the performance test
        used only for debug where we don't want to apply the sku again
        """
        sku = self.cli_obj.chassis.get_hwsku()
        hwsku_json = self.cli_obj.general.get_config_db()
        switch_role = self.get_switch_role(sku)
        self.update_connected_unconnected_ports(sku, hwsku_json, switch_role, self.chip_type)
        self.sonic_to_sdk_ports_dict, self.sdk_to_sonic_ports_dict = self.get_sonic_to_sdk_port_mapping()
        self.mloops = self.get_mloops_tuples_list()
        self.port_groups = self.get_right_left_ports_dict()
        self.sonic_ports_aliases_dict = self.cli_obj.interface.parse_ports_aliases_on_sonic()

    def get_switch_role(self, sku):
        sku_by_chip_type = MRCConsts.HWSKU_BY_CHIP_TYPE[self.chip_type]
        if sku in sku_by_chip_type["leaf"]:
            return "leaf"
        elif sku in sku_by_chip_type["spine"]:
            return "spine"
        else:
            raise TestIssue(f"Unrecognized sku {sku} for chip type {self.chip_type}")

    def update_connected_unconnected_ports(self, sku, hwsku_json, sku_type, chip_type):
        sku_ports_dict = hwsku_json["PORT"]
        ports_list = list(sku_ports_dict.keys())
        if sku_type == "spine":
            ports_list = self.get_spine_downstream_ports(sku, sku_ports_dict)
        self.remove_service_ports(ports_list, chip_type)
        if len(ports_list) % 2 == 1:
            raise TestIssue(f"Expected port number should be even, actual port number {len(ports_list)}")
        middle = len(ports_list) // 2
        if self.dut_alias == PerfConsts.LEFT_TG_ALIAS:
            self.connected_ports, self.unconnected_ports = ports_list[:middle], ports_list[middle:]
        elif self.dut_alias == PerfConsts.RIGHT_TG_ALIAS:
            self.unconnected_ports, self.connected_ports = ports_list[:middle], ports_list[middle:]
        else:
            self.connected_ports, self.unconnected_ports = ports_list, []

    def remove_service_ports(self, ports_list, chip_type):
        if chip_type == "SPC4":
            service_port = ports_list.pop(self.service_port_idx)
            self.service_ports.append(service_port)
        elif chip_type in ("SPC5", "SPC6"):
            service_port1 = ports_list.pop(self.service_port_idx)
            service_port2 = ports_list.pop(self.service_port_idx)
            self.service_ports += [service_port1, service_port2]

    def update_sku(self, sku, hwsku_json):
        switch_type = MRCConsts.HWSKU_SWITCH_TYPE[sku]
        self.update_sku_port_admin_status(hwsku_json)
        self.update_mac(hwsku_json)
        self.update_switch_type(hwsku_json, switch_type)
        save_config_db_json(self.engine, hwsku_json, remove_json_path=False)

    def update_sku_port_admin_status(self, hwsku_json):
        for port, port_dict in hwsku_json["PORT"].items():
            port_dict.update({"admin_status": "up"})

    def update_mac(self, hwsku_json):
        hwsku_json["DEVICE_METADATA"]["localhost"]["mac"] = self.mac

    def update_switch_type(self, hwsku_json, switch_type):
        hwsku_json["DEVICE_METADATA"]["localhost"]["type"] = switch_type

    def save_configuration_file(self, conf_path, conf_json, dst_dut_dir="/tmp"):
        save_config_db_json(self.engine, conf_json, conf_path, remove_json_path=False)

    def save_basic_configuration(self, players):
        pass

    def get_player_ports(self, dst_dut_dir="/tmp"):
        return {'connected_ports': self.connected_ports,
                'unconnected_ports': self.unconnected_ports}

    def configure_mloops(self, validate_mloops=True, is_simx=False):
        logging.info(f"Configure Mloop on {self.dut_alias}")
        self.update_mloops_conf_on_syncd()
        self.execute_cmd(self.get_cmd_for_sdk(f"python3 {PerfConsts.DISABLE_MAC_SCRIPT}"))
        self.cli_obj.mac.clear_fdb()
        env_variables = [f"{PerfConsts.SEND_FWS_ENV}={'false' if is_simx else 'true'}"]
        configure_mloops_cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} --names {PerfConsts.DVS_TG_MLOOP_CONFIGURATION}"
        self.logrotate("rsyslog")
        self.execute_cmd(self.get_cmd_for_sdk(configure_mloops_cmd, env_variables=env_variables))
        if validate_mloops:
            self.cli_obj.interface.check_link_state(ifaces=self.unconnected_ports + self.connected_ports)
        self.configure_ports_shaper(shaper_value=PerfConsts.SRV6_SONIC_OS_SHAPER_VALUE)

    def update_mloops_conf_on_syncd(self):
        mloops = list(zip(self.get_hex_int_sdk_ports(self.unconnected_ports),
                          self.get_hex_int_sdk_ports(self.connected_ports)))
        mloop_conf_file = "user_mloop_configuration.json"
        full_path = os.path.join(PerfConsts.CONFIG_FILES_DIR, self.dut_alias, mloop_conf_file)
        with open(full_path, 'w') as f:
            json.dump(mloops, f)
        logging.info(f"Configure Mloop on {self.dut_alias}")
        files_list = PerfConsts.CONFIG_FILES_DICT[self.dut_alias]
        copy_files_to_syncd(self.engine, files_list, PerfConsts.CONFIG_FILES_DIR)
        copy_files_to_syncd(self.engine, [mloop_conf_file],
                            os.path.join(PerfConsts.CONFIG_FILES_DIR, self.dut_alias))

    def copy_traffic_json_to_player(self, scenario, json_path, dst_dut_dir="/tmp"):
        dir_path, file_name = os.path.split(json_path)
        copy_files_to_syncd(self.engine, [file_name], dir_path)
        return f"/{file_name}"

    def restore_basic_configuration(self, dst_dut_dir="/tmp"):
        self.cleanup_shared_json_file()
        self.restore_im_on_tg()
        return

    def restore_im_on_tg(self):
        """
        Re-enable Independent Module (IM) on traffic generators after test finishes.
        During setup IM is disabled on TGs via `cmis_host_mgmt.py --disable`. This restores it
        with the symmetric `cmis_host_mgmt.py --enable`, passing the SKU media_settings.json and
        optics_si_settings.json paths, then reloads config and verifies dockers.
        Only runs on traffic generators.
        """
        if self.dut_alias in PerfConsts.TG_ALIAS_LIST:
            with allure.step(f'Restore IM on {self.dut_alias}'):
                logging.info(f"Re-enabling IM on {self.dut_alias}")
                self.cli_obj.im.enable_im_in_sai()
                self.cli_obj.general.reload_configuration(force=True)
                self.cli_obj.general.verify_dockers_are_up()

    def get_tg_unconnected_ports(self):
        return self.unconnected_ports

    def get_dut_ports(self):
        return self.connected_ports

    def get_sdk_ports(self, ports_list):
        return list(map(self.get_sdk_port, ports_list))

    def get_hex_int_sdk_ports(self, ports_list):
        return list(map(self.get_hex_int_sdk_port, ports_list))

    def get_hex_int_sdk_port(self, port):
        """
        Args:
            port: sonic port, i.e, Ethernet0

        Returns: sdk port mapped to sonic port , i.e, 65777
        """
        if port not in self.sonic_to_sdk_ports_dict:
            raise TestIssue(f"Port not found in sonic_to_sdk_ports_dict debug info: "
                            f"sonic_to_sdk_ports_dict: {self.sonic_to_sdk_ports_dict}, "
                            f"port: {port}, "
                            f"connected_ports: {self.connected_ports}, "
                            f"unconnected_ports: {self.unconnected_ports}")
        return int(self.sonic_to_sdk_ports_dict[port], PerfConsts.HEX_BASE)

    def get_sdk_port(self, port):
        """
        Args:
            port: sonic port, i.e, Ethernet0

        Returns: sdk port mapped to sonic port , i.e, 0x100f1
        """
        return self.sonic_to_sdk_ports_dict[port]

    def get_sdk_port_mapping(self):
        """
        Returns: a dict of port number and a list of sdk hex ports sorted by lane mapping.
        i.e,
        { 1: ['0x100f1', '0x100f2', '0x100f3',...], 2: [...], ... }

        Parses the output of sx_api_ports_mapping_dump.py which has the following table format:
        | log_port  | dev_id | phy_port | label_port | admin_st | phy_mod | lane_bmap | width |
        | 0x100f1   |   1    |    1     |     1      | ENABLED  |    0    |   0x01    |   1   |

        The regex captures three groups from ENABLED rows:
            Group 1 (hex_port):    SDK logical port hex   (e.g., 0x100f1)
            Group 2 (port_number): label port number      (e.g., 1)
            Group 3 (lane_map):    lane bitmap hex        (e.g., 0x01)
        """
        sdk_port_mapping_cmd = "sx_api_ports_mapping_dump.py"
        sdk_port_mapping_info = self.execute_cmd(self.get_cmd_for_sdk(sdk_port_mapping_cmd))
        regex = r"\|\s+(0x[\d|\w]*)\|\s+\d+\|\s+\d+\|\s+(\d+)\|\s+ENABLED\|\s+\d+\|\s+(0x[\da-fA-F]+)\|\s+\d+\|"
        sdk_port_mapping = re.findall(regex, sdk_port_mapping_info)
        sdk_port_mapping_dict = defaultdict(list)

        for hex_port, port_number, lane_map in sdk_port_mapping:
            port_number = int(port_number)
            lane_map_hex_port_tuple = (int(lane_map, PerfConsts.HEX_BASE), hex_port)
            sdk_port_mapping_dict[port_number].append(lane_map_hex_port_tuple)
        for port_number, hex_ports in sdk_port_mapping_dict.items():
            sorted_hex_ports_by_lane_map = sorted(hex_ports,
                                                  key=lambda lane_map_hex_port_tuple: lane_map_hex_port_tuple[0])
            sorted_hex_ports = [lane_map_hex_port_tuple[1] for lane_map_hex_port_tuple in sorted_hex_ports_by_lane_map]
            sdk_port_mapping_dict[port_number] = sorted_hex_ports
        return sdk_port_mapping_dict

    def get_sonic_port_mapping(self):
        """
        Returns: a list of sonic port number, port name and the lane index in sdk, i.e,
        [(1, Ethernet0, 0),(1, Ethernet1, 1), (1, Ethernet2, 2), ..]
        """
        sonic_port_mapping = []
        ports_aliases_dict = self.cli_obj.interface.parse_ports_aliases_on_sonic()
        for port, port_alias in ports_aliases_dict.items():
            port_number = int(get_alias_number(port_alias))
            port_letter = get_alias_letter(port_alias)
            port_idx = convert_letter_to_idx(port_letter)
            sonic_port_mapping.append((port_number, port, port_idx))
        return sonic_port_mapping

    def get_sonic_to_sdk_port_mapping(self):
        """
        Returns: a dict of sonic port mapped to sdk port, i.e,
        { 'Ethernet0': '0x100f1',...}
        """
        sonic_to_sdk_ports_dict = {}
        sdk_to_sonic_ports_dict = {}
        sdk_port_mapping_dict = self.get_sdk_port_mapping()
        sonic_port_mapping = self.get_sonic_port_mapping()
        for (port_number, port, idx) in sonic_port_mapping:
            sonic_to_sdk_ports_dict[port] = sdk_port_mapping_dict[port_number][idx]
            sdk_to_sonic_ports_dict[sdk_port_mapping_dict[port_number][idx]] = port
        return sonic_to_sdk_ports_dict, sdk_to_sonic_ports_dict

    def get_os_ports_name_mapping(self):
        """
        Returns: a list of dicts with os port name for each port
        i.e,
        [{'osPortName': 'Ethernet0', 'port': '0x100f1'},...]
        """
        os_ports_name_mapping_df = []
        for sdk_port, sonic_port in self.sdk_to_sonic_ports_dict.items():
            os_ports_name_mapping_df.append({ValidationConsts.OS_PORT_NAME: sonic_port,
                                             ValidationConsts.PORT: sdk_port})
        return os_ports_name_mapping_df

    def get_traffic_parameters(self, scenario, conf_args):
        """
        Args:
            scenario: name of the scenario, i.e, spcx_ra
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
        traffic_parameters = {
            "ports": self.get_sdk_ports(self.get_tg_unconnected_ports()),
            "MAC": {"src": self.mac,
                    "dst": conf_args["dut_mac"]},
            "IP": {},
            "IPV6": {},
            PerfConsts.IP_PROTOCOL_UDP: {"src": PerfConsts.UDP_SOURCE_PORT, "dst": PerfConsts.ROCE_PORT},
            PerfConsts.IP_PROTOCOL_TCP: {"sport": PerfConsts.TCP_SOURCE_PORT, "dport": PerfConsts.TCP_DOURCE_PORT},
            "packet_size": conf_args["packet_size"],
            "is_ipv6": conf_args["is_ipv6"]
        }
        return traffic_parameters

    def get_mac(self):
        output = self.execute_cmd("sonic-cfggen -j /etc/sonic/config_db.json --var-json DEVICE_METADATA | grep mac")
        return re.search(r'"mac":\s"(.*)"', output).group(1)

    def get_right_left_ports_dict(self):
        """
        Returns:
        A dict of ports in the dut connect to the right TG and left TG, i.e,
        {'right_ports': ['Ethernet256', ...,], 'left_ports': ['Ethernet0',...]}
        """
        dut_ports = self.get_dut_ports()
        middle = len(dut_ports) // 2
        right_left_ports_dict = {"left_ports": dut_ports[:middle], "right_ports": dut_ports[middle:]}
        return right_left_ports_dict

    def get_upstream_downstream_ports_dict(self, upstream_ports_num, downstream_ports_num, sequential=False):
        """Select upstream (uplink) and downstream (downlink) DUT ports for T0 traffic tests.
        Ports are taken from the fixed groups in ``MRCConsts.T0_UPSTREAM_DOWNSTREAM_PORT_GROUPS_DICT``.

        Args:
            upstream_ports_num: Number of uplink ports to return.
            downstream_ports_num: Number of downlink ports to return.
            sequential: If True, take the first ``upstream_ports_num`` / ``downstream_ports_num`` ports
                from each group in order (deterministic; useful so all logical ports on a physical panel
                are used as intended). If False, sample ports uniformly at random without replacement.

        Returns:
            A tuple ``(upstream, downstream)`` where each element is a list of SONiC interface names
            (e.g. ``Ethernet0``) for uplinks and downlinks respectively.
        """
        upstream_ports = copy.deepcopy(MRCConsts.T0_UPSTREAM_DOWNSTREAM_PORT_GROUPS_DICT[MRCConsts.UPLINKS])
        downstream_ports = copy.deepcopy(MRCConsts.T0_UPSTREAM_DOWNSTREAM_PORT_GROUPS_DICT[MRCConsts.DOWNLINKS])
        if sequential:
            if len(upstream_ports) < upstream_ports_num:
                raise TestIssue(f"Not enough upstream ports for sequential selection: "
                                f"requested {upstream_ports_num}, available {len(upstream_ports)}")
            if len(downstream_ports) < downstream_ports_num:
                raise TestIssue(f"Not enough downstream ports for sequential selection: "
                                f"requested {downstream_ports_num}, available {len(downstream_ports)}")
            upstream = upstream_ports[:upstream_ports_num]
            downstream = downstream_ports[:downstream_ports_num]
        else:
            upstream = random.sample(upstream_ports, upstream_ports_num)
            downstream = random.sample(downstream_ports, downstream_ports_num)
        return upstream, downstream

    def configure_dummy_acls(self, template_path, dut_ports, num_acls):
        """Configure dummy ACLs on SONiC using Jinja template + sonic-cfggen --write-to-db."""
        self.cli_obj.acl.load_acl_rules_from_template(
            template_path, "add_dummy_acls.jinja", dut_ports, num_acls=num_acls)
        self.cli_obj.acl.apply_acl_rules("/tmp/acl.json")

    def remove_dummy_acls(self):
        """Remove all ACLs on SONiC using acl-loader delete."""
        self.cli_obj.acl.delete_config()

    def get_mloops_tuples_list(self):
        mloops_tuples_list = []
        if len(self.connected_ports) == len(self.unconnected_ports):
            mloops_tuples_list = list(zip(self.connected_ports, self.unconnected_ports))
        return mloops_tuples_list

    def get_connected_unconnected_ports(self):
        transceiver_presence_table_info = self.cli_obj.interface.parse_interfaces_transceiver_presence()
        connected_ports = []
        unconnected_ports = []
        for port, status in transceiver_presence_table_info.items():
            if status['Presence'] == 'Present':
                connected_ports.append(port)
            else:
                unconnected_ports.append(port)
        self.remove_service_port(connected_ports)
        self.remove_service_port(unconnected_ports)
        return connected_ports, unconnected_ports

    def remove_service_port(self, ports_list):
        for service_port in self.service_ports:
            ports_list.remove(service_port)

    def disable_im(self):
        self.execute_cmd("sudo cmis_host_mgmt.py --disable")

    def fdb_discard_creation(self):
        configure_fdb_drop_cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} --names TempFdbDiscardCreation"
        self.execute_cmd(self.get_cmd_for_sdk(configure_fdb_drop_cmd))

    def get_tg_interfaces_vlan_configuration(self):
        vlan_brief_info = self.cli_obj.vlan.get_show_vlan_brief_parsed_output(data_line_index=3)
        vlan_interface_configuration_dict = {}
        for vlan, vlan_info_dict in vlan_brief_info.items():
            vlan_ports = list(vlan_info_dict["ports"].keys())
            for port in vlan_ports:
                vlan_interface_configuration_dict[port] = int(vlan)
        self.vlan_interface_configuration_dict = vlan_interface_configuration_dict
        return vlan_interface_configuration_dict

    def get_dut_interfaces_ipv6_configuration(self):
        dut_configuration = self.cli_obj.general.get_config_db()
        interface_configuration = dut_configuration[ConfigDbJsonConst.INTERFACE]
        dut_interfaces_ipv6_configuration_dict = {}
        for key in interface_configuration:
            if "|" in key:
                interface, ip = key.split("|")
                if is_ipv6(ip):
                    ipv6_address = ip.split("/")[0]
                    dut_interfaces_ipv6_configuration_dict[interface] = ipv6_address
        self.dut_interfaces_ipv6_configuration_dict = dut_interfaces_ipv6_configuration_dict
        return dut_interfaces_ipv6_configuration_dict

    def start_mst(self):
        """Start MST on host and syncd container for temperature collection via mget_temp."""
        self.execute_cmd("sudo mst start 2>&1 || true")
        self.execute_cmd(f"docker exec {InfraConst.SYNCD_DOCKER} mst start 2>&1 || true")

    def validate_traffic(self, json_path, samples_params_dict, dst_dut_dir="/tmp"):
        logging.info("Running traffic validator on the dut")
        self.start_mst()
        samples_params = self.get_power_sensor_env_variables()
        for env_var_name, param_val in samples_params_dict.items():
            samples_params.append(f"{env_var_name}={param_val}")
        run_validator_cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} --names {PerfConsts.DVS_TG_VALIDATOR_NAME}"
        self.logrotate("rsyslog")
        try:
            self.execute_cmd(self.get_cmd_for_sdk(run_validator_cmd, env_variables=samples_params))
        except Exception as e:
            logging.error(f"Error running traffic validator: {e}")
            raise TestIssue(msg=f"Error running traffic validator: {type(e).__name__}: {e}") from e
        try:
            self.execute_cmd(f"docker cp {InfraConst.SYNCD_DOCKER}:/tmp/TrafficValidator.json {dst_dut_dir}/TrafficValidator.json")
            self.engine.copy_file(source_file="TrafficValidator.json", file_system=dst_dut_dir, dest_file=json_path,
                                  overwrite_file=True, verify_file=False, direction='get')
        except Exception as e:
            logging.error(f"Error copying traffic validator JSON from syncd: {e}")
            raise TestIssue(msg=f"Error copying traffic validator JSON from syncd: {type(e).__name__}: {e}") from e

    def logrotate(self, daemon):
        if is_redmine_issue_active([4388176])[0]:
            logging.info(f"Rotating log for {daemon}")
            self.execute_cmd(f"sudo /usr/sbin/logrotate -f /etc/logrotate.d/{daemon}  > /dev/null 2>&1")

    def add_ports_connectivity_to_dut(self, conf_args, selected_connected_ports=None):
        ports_file = "ports.json"
        full_path = os.path.join(PerfConsts.CONFIG_FILES_DIR, ports_file)
        connected_ports = selected_connected_ports if selected_connected_ports else self.connected_ports
        ports_connectivity_dict = {
            "unconnected_ports": self.get_hex_int_sdk_ports(self.unconnected_ports),
            "connected_ports": self.get_hex_int_sdk_ports(connected_ports),
            "speed": conf_args["speed"]}
        with open(full_path, 'w') as f:
            json.dump(ports_connectivity_dict, f)
        copy_files_to_syncd(self.engine, [ports_file], PerfConsts.CONFIG_FILES_DIR)

    def configure_ports_shaper(self, shaper_value):
        configure_ports_shaper_cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} --names ConfigureShaperOnAllPorts"
        self.execute_cmd(self.get_cmd_for_sdk(configure_ports_shaper_cmd,
                                              env_variables=[f'{PerfConsts.SHAPER_VALUE_ENV_VAR}={shaper_value}']))

    def configure_interfaces_mac_neighbor(self, vlan_interface_configuration_dict):
        """
        TODO: Should be done via BGP - this is temporary
        Configure static route on dut, however this configuration eventually will be done via BGP
        """
        dut_ports = self.cli_obj.performance.get_dut_ports()
        mac_range = generate_mac_range("00:11:22:33:44:55", count=len(dut_ports))
        fdb_discard_conf = []
        cmd_list = []
        for idx, port in enumerate(dut_ports):
            port_ipv6_address = self.dut_interfaces_ipv6_configuration_dict[port].replace("aaaa", "bbbb")
            port_neighbor_mac = mac_range[idx]
            vlan = vlan_interface_configuration_dict[port]
            cmd_list.append(f"sudo ip -6 route add {port_ipv6_address}/48 dev {port}")
            cmd_list.append(f"sudo ip -6 neigh add {port_ipv6_address} lladdr {port_neighbor_mac} dev {port}")
            fdb_discard_conf.append([self.cli_obj.performance.get_hex_int_sdk_port(port), port_neighbor_mac, vlan])
        self.engine.run_cmd_set(cmd_list)
        self.configure_fdb_discard(fdb_discard_conf)

    def configure_fdb_discard(self, fdb_discard_conf):
        """
        TODO: should be removed with correct ACL - this is temporary

        All egress traffic going into the tg should be dropped, this should be done by ACL
        However currently this is being done by adding correct FDB discard entries via sdk.
        """
        conf_file = "fdb_discard_conf.json"
        full_path = os.path.join(PerfConsts.CONFIG_FILES_DIR, conf_file)
        with open(full_path, 'w') as f:
            json.dump(fdb_discard_conf, f)
        for alias in PerfConsts.PERF_SETUP_TG_ALIASES:
            copy_files_to_syncd(self.topology_obj.players[alias]['engine'], [conf_file], PerfConsts.CONFIG_FILES_DIR)
        for alias in PerfConsts.PERF_SETUP_TG_ALIASES:
            tg_cli = self.topology_obj.players[alias]['cli']
            tg_cli.performance.fdb_discard_creation()

    def get_leaf_many_to_few_port_group_df(self, M, num_of_ingress_ports):
        port_group_df = []
        downlinks = MRCConsts.T0_UPSTREAM_DOWNSTREAM_PORT_GROUPS_DICT[MRCConsts.DOWNLINKS]
        uplinks = MRCConsts.T0_UPSTREAM_DOWNSTREAM_PORT_GROUPS_DICT[MRCConsts.UPLINKS]
        egress_ports_num = num_of_ingress_ports // M
        uplinks_start_index = random.randint(0, len(uplinks) - num_of_ingress_ports)
        egress_ports = random.sample(downlinks, egress_ports_num)
        ingress_ports = uplinks[uplinks_start_index:uplinks_start_index + num_of_ingress_ports]
        sdk_port_list_egress = self.get_sdk_ports(egress_ports)
        sdk_port_list_ingress = self.get_sdk_ports(ingress_ports)
        for port in sdk_port_list_egress:
            port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.EGRESS_PORT_GROUP_NAME})
        for port in sdk_port_list_ingress:
            port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.INGRESS_PORT_GROUP_NAME})
        return egress_ports, ingress_ports, port_group_df

    def get_leaf_many_to_few_port_group_df_for_debug(self, M, num_of_ingress_ports, option):
        port_group_df = []
        dut_ports = self.get_dut_ports()
        downlinks = MRCConsts.T0_UPSTREAM_DOWNSTREAM_PORT_GROUPS_DICT[MRCConsts.DOWNLINKS]
        uplinks = MRCConsts.T0_UPSTREAM_DOWNSTREAM_PORT_GROUPS_DICT[MRCConsts.UPLINKS]
        sorted_sdk_downlinks = sorted(self.get_hex_int_sdk_ports(downlinks))
        sorted_sdk_uplinks = sorted(self.get_hex_int_sdk_ports(uplinks))
        sorted_sdk_dut_ports = sorted(self.get_hex_int_sdk_ports(dut_ports))
        half_ingress_ports_num = num_of_ingress_ports // 2
        sorted_sdk_sonic_downlinks = [self.sdk_to_sonic_ports_dict[hex(port)] for port in sorted_sdk_downlinks]
        sorted_sdk_sonic_uplinks = [self.sdk_to_sonic_ports_dict[hex(port)] for port in sorted_sdk_uplinks]
        sorted_sdk_sonic_dut_ports = [self.sdk_to_sonic_ports_dict[hex(port)] for port in sorted_sdk_dut_ports]
        pairing_downlinks_to_uplinks = get_ingress_egress_ports_pairing_for_debug(sorted_sdk_sonic_downlinks[:half_ingress_ports_num], sorted_sdk_sonic_uplinks, M, option)
        pairing_uplinks_to_downlinks = get_ingress_egress_ports_pairing_for_debug(sorted_sdk_sonic_uplinks[:half_ingress_ports_num], sorted_sdk_sonic_downlinks, M, option)
        pairing = pairing_downlinks_to_uplinks + pairing_uplinks_to_downlinks
        if option == PortMappingOptionsConsts.EGRESS_SEQUENTIAL_OPTION:
            pairing = get_ingress_egress_ports_pairing_for_debug(sorted_sdk_sonic_dut_ports, sorted_sdk_sonic_dut_ports, M, option)
        egress_ports = list(set([t[1] for t in pairing]))
        ingress_ports = [t[0][0] for t in pairing]
        sdk_port_list_egress = self.get_sdk_ports(egress_ports)
        sdk_port_list_ingress = self.get_sdk_ports(ingress_ports)
        for port in sdk_port_list_egress:
            port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: MRCConsts.EGRESS_PORT_GROUP_NAME})
        return egress_ports, ingress_ports, port_group_df, pairing

    def validate_ets(self, interface_list, queues_list, violations_list):
        """
        For each queue in (1,2,4):

        (#bytes transmitted from each queue) == (Sum of #bytes transmitted from queues 1,2,4) * (Approximate normalized DWRR weight / sum of Approximate normalized DWRR weights of queues 1,2,4).
        """
        dwrr_weights = self.cli_obj.qos.get_dwrr_weights(queues_list)
        dwrr_weights_sum = sum(dwrr_weights.values())
        with allure.step(f"Validate ETS for {queues_list}"):
            for interface in interface_list:
                show_queue_counters_dict = self.cli_obj.interface.parse_show_queue_counters(interface)
                sum_queue_bytes = self.cli_obj.interface.get_sum_queue_bytes(show_queue_counters_dict, queues_list)
                for queue in queues_list:
                    queue_counter_bytes, queue_counter_drop_bytes = self.cli_obj.interface.get_counters_for_queue_bytes(show_queue_counters_dict, queue, PerfConsts.PACKET_SIZE_4K)
                    queue_dwrr_weight = dwrr_weights[queue]
                    if queue_counter_bytes != sum_queue_bytes * (queue_dwrr_weight / dwrr_weights_sum):
                        violations_list.append(f"Queue {queue} bytes: queue_counter_bytes {queue_counter_bytes} != sum_queue_bytes {sum_queue_bytes} * queue_dwrr_weight {queue_dwrr_weight} / dwrr_weights_sum {dwrr_weights_sum} for {interface}")
        return violations_list

    def update_port_group_df_on_dut(self, port_group_df):
        """
        Convert list of dicts format [{"port":"0x101f1", "portGroupName": "uplink"},..]
        to dict format {"uplink": [65777, 65778, ...], ...}
        """
        port_group_df_dict = self.convert_port_group_df_to_dict(port_group_df)
        port_group_df_file = "conf.json"
        full_path = os.path.join(PerfConsts.CONFIG_FILES_DIR, port_group_df_file)
        with open(full_path, 'w') as f:
            json.dump(port_group_df_dict, f)
        copy_files_to_syncd(self.engine, [port_group_df_file], PerfConsts.CONFIG_FILES_DIR, syncd_dir='/tmp')
