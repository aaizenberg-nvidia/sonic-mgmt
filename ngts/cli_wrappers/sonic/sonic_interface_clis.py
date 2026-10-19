import re
import logging
import ast
import allure
import json
from retry.api import retry_call
from ngts.cli_wrappers.common.interface_clis_common import InterfaceCliCommon
from ngts.cli_wrappers.sonic.sonic_multi_asic_cli import SonicMultiAsicCli
from ngts.cli_wrappers.sonic.sonic_general_clis import SonicGeneralCliDefault
from ngts.cli_util.cli_parsers import generic_sonic_output_parser, parse_show_interfaces_transceiver_eeprom
from ngts.constants.constants import AutonegCommandConstants, SonicConst

logger = logging.getLogger()


class SonicInterfaceCli(InterfaceCliCommon, SonicMultiAsicCli):

    def __init__(self, engine, cli_obj, asic_id=None):
        SonicMultiAsicCli.__init__(self, engine, asic_id)
        self.cli_obj = cli_obj

    def add_interface(self, interface, iface_type):
        raise NotImplementedError

    def del_interface(self, interface):
        raise NotImplementedError

    def enable_interface(self, interface):
        """
        This method enables a network interface
        :param interface: interface name which should be enabled, example: Ethernet0
        :return: command output
        """
        return self.engine.run_cmd("sudo config interface {} startup {}".format(self.multi_asic_config_cmd_ext, interface))

    def enable_interfaces(self, interfaces_list):
        """
        This method enables a list of network interfaces
        :param interfaces_list: a list of interfaces which should be enabled, example: ["Ethernet0", "Ethernet4"]
        :return: command output
        """
        if self.asic_id is None:
            interfaces_to_enable = ','.join(interfaces_list)
            return self.enable_interface(interfaces_to_enable)
        for interface in interfaces_list:
            self.enable_interface(interface)

    def disable_interface(self, interface):
        """
        This method disables network interface
        :param interface: interface name which should be disabled, example: Ethernet0
        :return: command output
        """
        return self.engine.run_cmd("sudo config interface {} shutdown {}".format(self.multi_asic_config_cmd_ext, interface))

    def disable_interfaces(self, interfaces_list):
        """
        This method disables a list of network interfaces
        :param interfaces_list: a list of interfaces which should be disabled, example: ["Ethernet0", "Ethernet4"]
        :return: none
        """
        if self.asic_id is None:
            interfaces_to_disable = ','.join(interfaces_list)
            return self.disable_interface(interfaces_to_disable)
        for interface in interfaces_list:
            self.disable_interface(interface)

    def set_interface_speed(self, interface, speed, validate=False):
        """
        Method which setting interface speed
        :param interface: interface name
        :param speed: speed value
        :return: command output
        """
        # TODO: Move 2 lines below to separate method in ngts/utilities
        if 'G' in speed:
            speed = int(speed.split('G')[0]) * 1000
        elif 'M' in speed:
            speed = int(speed.split('M')[0])

        return self.engine.run_cmd("sudo config interface {} speed {} {}".format(self.multi_asic_config_cmd_ext, interface, speed), validate=validate)

    def set_interfaces_speed(self, interfaces_speed_dict):
        """
        Method which setting interface speed
        :param interfaces_speed_dict:  i.e, {'Ethernet0': '50G'}
        :return: command output
        """
        for interface, speed in interfaces_speed_dict.items():
            self.set_interface_speed(interface, speed)

    def set_interface_mtu(self, interface, mtu):
        """
        Method which setting interface MTU
        :param interface: interface name
        :param mtu: mtu value
        :return: command output
        """
        return self.engine.run_cmd("sudo config interface {} mtu {} {}".format(self.multi_asic_config_cmd_ext, interface, mtu))

    def show_interfaces_status(self):
        """
        Method which getting interfaces status
        :return: parsed command output
        """
        return self.engine.run_cmd("sudo show interfaces status")

    def parse_interfaces_status(self, headers_ofset=0, len_ofset=1, data_ofset_from_start=2, asic_num=None):
        """
        Method which getting parsed interfaces status
        :param headers_ofset: Line number in which we have headers
        :param len_ofset: Line number from which we can find len for all fields, in example above it is line 2
        :param data_ofset_from_start: Line number from which we will start parsing data and fill dictionary with results
        :return: dictionary, example: {'Ethernet0': {'Lanes': '0,1,2,3,4,5,6,7', 'Speed': '100G', 'MTU': '9100',
        'FEC': 'N/A', 'Alias': 'etp1', 'Vlan': 'routed', 'Oper': 'up', 'Admin': 'up', 'Type': 'QSFP28 or later',
        'Asym PFC': 'N/A'}, 'Ethernet8': {'Lanes'.......
        """
        ifaces_status = self.show_interfaces_status()
        result = generic_sonic_output_parser(ifaces_status, headers_ofset=headers_ofset, len_ofset=len_ofset,
                                             data_ofset_from_start=data_ofset_from_start,
                                             data_ofset_from_end=None, column_ofset=2, output_key='Interface')
        if asic_num is not None:
            result = self._filter_interfaces_status_by_asic(result, asic_num)
        return result

    def _filter_interfaces_status_by_asic(self, ifaces_info, asic_num):
        """Return only Ethernet interfaces belonging to the given ASIC (single-ASIC: all; multi-ASIC: asic{asic_num})."""
        filtered = {}
        for iface, info in ifaces_info.items():
            if not iface.startswith('Ethernet'):
                continue
            alias = info.get('Alias', '')
            if alias.startswith('etp'):
                # Single ASIC - no filtering, return full dict
                return ifaces_info
            if alias.startswith(f'asic{asic_num}'):
                filtered[iface] = info
        return filtered

    def show_interfaces_fec_status(self):
        """
        Method which getting interfaces fec status
        :return: command output
        """
        return self.engine.run_cmd("sudo show interfaces fec status")

    def parse_interfaces_fec_status(self, headers_ofset=0, len_ofset=1, data_ofset_from_start=2):
        """
        Method which getting parsed interfaces fec status
        :param headers_ofset: Line number in which we have headers
        :param len_ofset: Line number from which we can find len for all fields, in example above it is line 2
        :param data_ofset_from_start: Line number from which we will start parsing data and fill dictionary with results
        :return: dictionary of parsed command, i.e,
        {'Ethernet0': {'Interface': 'Ethernet0', 'FEC Oper': 'none', 'FEC Admin': 'N/A'},...}
        """
        ifaces_fec_status = self.show_interfaces_fec_status()
        return generic_sonic_output_parser(ifaces_fec_status, headers_ofset=headers_ofset, len_ofset=len_ofset,
                                           data_ofset_from_start=data_ofset_from_start,
                                           data_ofset_from_end=None, column_ofset=2, output_key='Interface')

    def get_interface_speed(self, interface):
        """
        Method which getting interface speed
        :param interface: interface name
        :return: interface speed, example: 200G
        """
        return self.parse_interfaces_status()[interface]['Speed']

    def get_interfaces_speed(self, interfaces_list):
        """
        Method which getting interface speed
        :param interfaces_list: interfaces name list, example: ['eth1', 'eth2']
        :return: interface speed dict, example: {'eth1': 200G, 'eth2': '100G'}
        """
        result = {}
        interfaces_data = self.parse_interfaces_status()
        for interface in interfaces_list:
            result[interface] = interfaces_data[interface]['Speed']
        return result

    def get_interfaces_transceiver_eeprom(self, interface=''):
        """
        :param interface:  interfaces name, example: 'Ethernet0'
        :return: interface transceiver eeprom,
        i.e, command output sample:
        Ethernet0: SFP EEPROM is not applicable for RJ45 port
        """
        cmd = f'show interfaces transceiver eeprom {interface}'
        return self.engine.run_cmd(cmd)

    def parse_interfaces_transceiver_eeprom(self, interface='', interfaces_transceiver_eeprom_output=None):
        """
        Parse output 'show interfaces transceiver eeprom' as dictionary
        :param interface: interfaces name, example: 'Ethernet0'
        :param interfaces_transceiver_eeprom_output: output 'show interfaces transceiver eeprom'
        :return: dict, example: {'Ethernet0': 'Present', 'Ethernet1': 'Present'...}
        """
        if not interfaces_transceiver_eeprom_output:
            interfaces_transceiver_eeprom_output = self.get_interfaces_transceiver_eeprom(interface)

        return parse_show_interfaces_transceiver_eeprom(interfaces_transceiver_eeprom_output)

    def get_interfaces_transceiver_presence(self, interface=''):
        """
        :param interface:  interfaces name, example: 'Ethernet0'
        :return: interface transceiver presence,
        i.e, command output sample:
        Port       Presence
        ---------  -----------
        Ethernet0  Not present
        """
        cmd = f'show interfaces transceiver presence {interface}'
        return self.engine.run_cmd(cmd)

    def parse_interfaces_transceiver_presence(self, interface='', interfaces_transceiver_presence_output=None):
        """
        Parse output 'show interfaces transceiver presence' as dictionary
        :param interface: interfaces name, example: 'Ethernet0'
        :param interfaces_transceiver_presence_output: output 'show interfaces transceiver presence'
        :return: dict, example: {'Ethernet0': 'Present', 'Ethernet1': 'Present'...}
        """
        if not interfaces_transceiver_presence_output:
            interfaces_transceiver_presence_output = self.get_interfaces_transceiver_presence(interface)

        return generic_sonic_output_parser(interfaces_transceiver_presence_output,
                                           headers_ofset=0, len_ofset=1, data_ofset_from_start=2,
                                           data_ofset_from_end=None, column_ofset=2, output_key='Port')

    def get_interfaces_transceiver_lpmode(self, interface=''):
        """
        :param interface:  interfaces name, example: 'Ethernet0'
        :return: interface transceiver lpmode,
        i.e, command output sample:
        Port       Low-power Mode
        ---------  ----------------
        Ethernet0  N/A
        """
        cmd = f'show interfaces transceiver lpmode {interface}'
        return self.engine.run_cmd(cmd)

    def parse_interfaces_transceiver_lpmode(self, interface='', interfaces_transceiver_lpmode_output=None):
        """
        Parse output 'show interfaces transceiver lpmode' as dictionary
        :param interface: interfaces name, example: 'Ethernet0'
        :param interfaces_transceiver_lpmode_output: output 'show interfaces transceiver lpmode'
        :return: dict, example: {'Ethernet0': 'Present', 'Ethernet1': 'Present'...}
        """
        if not interfaces_transceiver_lpmode_output:
            interfaces_transceiver_lpmode_output = self.get_interfaces_transceiver_lpmode(interface)

        return generic_sonic_output_parser(interfaces_transceiver_lpmode_output,
                                           headers_ofset=0, len_ofset=1, data_ofset_from_start=2,
                                           data_ofset_from_end=None, column_ofset=2, output_key='Port')

    def get_interfaces_transceiver_error_status(self, interface='', validate=False):
        """
        :param interface:  interfaces name, example: 'Ethernet0'
        :return: interface transceiver error-status,
        i.e, command output sample:
        Port       Error Status
        ---------  --------------
        Ethernet0  N/A
        """
        cmd = f'show interfaces transceiver error-status {interface}'
        return self.engine.run_cmd(cmd, validate=validate)

    def parse_interfaces_transceiver_error_status(self, interface='',
                                                  interfaces_transceiver_error_status_output=None):
        """
        Parse output 'show interfaces transceiver error-status' as dictionary
        :param interface: interfaces name, example: 'Ethernet0'
        :param interfaces_transceiver_error_status_output: output 'show interfaces transceiver error-status'
        :return: dict, example: {'Ethernet0': 'Present', 'Ethernet1': 'Present'...}
        """
        if not interfaces_transceiver_error_status_output:
            interfaces_transceiver_error_status_output = self.get_interfaces_transceiver_error_status(interface)

        return generic_sonic_output_parser(interfaces_transceiver_error_status_output,
                                           headers_ofset=0, len_ofset=1, data_ofset_from_start=2,
                                           data_ofset_from_end=None, column_ofset=2, output_key='Port')

    def get_interface_mtu(self, interface):
        """
        Method which getting interface MTU
        :param interface: interface name
        :return: interface MTU, example: 9100
        """
        return self.parse_interfaces_status()[interface]['MTU']

    def get_interfaces_mtu(self, interfaces_list):
        """
        Method which getting interface MTU
        :param interfaces_list: interfaces name list, example: ['eth1', 'eth2']
        :return: interface MTU, example: interface mtu dict, example: {'eth1': 9100, 'eth2': '1500'}
        """
        result = {}
        interfaces_data = self.parse_interfaces_status()
        for interface in interfaces_list:
            result[interface] = interfaces_data[interface]['MTU']
        return result

    def show_interfaces_alias(self, validate=False):
        """
        This method return output of "show interfaces alias" command
        :return: command output
        """
        return self.engine.run_cmd('show interfaces alias', validate=validate)

    def parse_ports_aliases_on_sonic(self):
        """
        Method which parse "show interfaces alias" command
        :return: a dictionary with port aliases, example: {'Ethernet0': 'etp1'}
        """
        result = {}
        interfaces_data = self.show_interfaces_alias()
        regex_pattern = r"(Ethernet\d+)\s*(etp\d+\w*)"
        list_output = re.findall(regex_pattern, interfaces_data, re.IGNORECASE)
        for port, port_sonic_alias in list_output:
            result[port] = port_sonic_alias
        return result

    def get_tries_num(self):
        tries_num = 40
        if self.cli_obj.general.is_asan_image() and self.cli_obj.general.is_bison():
            tries_num = 70
        elif self.cli_obj.general.is_simx_moose():
            #  Adding extra minute for simx SPC4 due to scale limitations
            tries_num = 44
        return tries_num

    def check_link_state(self, ifaces=None, expected_status='up'):
        """
        Verify that links in UP state. Default interface is  Ethernet0, this link exist in each Canonical setup
        :param ifaces: list of interfaces to check
        :param expected_status: 'up' if expected UP, or 'down' if expected DOWN
        :return: None, raise error in case of unexpected result
        """
        if ifaces is None:
            ifaces = ['Ethernet0']
        tries_num = self.get_tries_num()
        with allure.step(f'Check that link in {expected_status.upper()} state'):
            retry_call(self.check_ports_status,
                       fargs=[ifaces, expected_status],
                       tries=tries_num,
                       delay=5,
                       logger=logger)

    def check_ports_status(self, ports_list, expected_status='up'):
        """
        This method verifies that each interface is in expected oper state
        :param ports_list: list with port names which should be in UP state
        :param expected_status: 'up' if expected UP, or 'down' if expected DOWN
        :return Assertion exception in case of failure
        """
        logger.info('Checking that ifaces: {} in expected state: {}'.format(ports_list, expected_status))
        ports_status = self.parse_interfaces_status()

        for port in ports_list:
            assert ports_status.get(port), \
                f"show interfaces status doesn't return status of port {port}, check the dockers status"
            assert ports_status[port]['Oper'] == expected_status, \
                'Interface {} in unexpected state, expected is {}'.format(port, expected_status)

    def configure_dpb_on_ports(self, conf, expect_error=False, force=False):
        """
        This function configures breakout on ports and returns the breakout ports
        that were created after the breakout.
        :param conf: a dictionary of breakout mode and ports list to configure the breakout mode on.
        i.e,
        {'4x50G[40G,25G,10G,1G]': ['Ethernet212', 'Ethernet216']}
        :param expect_error: a boolean if we expect breakout command to fail
        :param force: a boolean if breakout command should include a force flag
        :return: a dictionary with the port configuration after breakout, i.e,
            {'Ethernet216': '25000',
            'Ethernet217': '25000',...}
        """
        breakout_ports_conf = {}
        for breakout_mode, ports_list in conf.items():
            for port in ports_list:
                with allure.step(f"Configuring breakout mode: {breakout_mode} on port: {port}, force mode: {force}"):
                    output = self.configure_dpb_on_port(port, breakout_mode, expect_error, force)
                    breakout_ports_conf.update(self.parse_added_breakout_ports(output))
        return breakout_ports_conf

    def configure_dpb_on_port(self, port, breakout_mode, expect_error=False, force=False):
        """
        :param port: i.e, Ethernet0
        :param breakout_mode: i.e, 4x50G[40G,25G,10G,1G]
        :param expect_error: True if breakout configuration is expected to fail, else False
        :param force: True if breakout configuration should be applied with force, else False
        :return: command output
        """
        logger.info('Configuring breakout mode: {} on port: {}, force mode: {}'.format(breakout_mode, port, force))
        force = "" if force is False else "-f"
        try:
            cmd = f'sudo config interface breakout {port} "{breakout_mode}" -y {force}'
            pattern = r"\s+".join([r"Do", r"you", r"wish", r"to", r"Continue\?", r"\[y\/N\]:"])
            output = self.engine.run_cmd_set([cmd, 'y'], tries_after_run_cmd=75, patterns_list=[pattern])
            self.verify_dpb_cmd(output, expect_error)
            return output
        except Exception as e:
            if expect_error:
                logger.info(output)
                return output
            else:
                logger.error(f"Command: {cmd} failed with error {e} when was expected to pass")
                raise AssertionError(f"Command: {cmd} failed with error {e} when was expected to pass")

    @staticmethod
    def verify_dpb_cmd(output, expect_error):
        """
        :param output: output of breakout command
        :param expect_error: True if command was expected to fail
        :return: verify the breakout command output only if it expected to pass successfully
        """
        if not expect_error:
            expected_msg_breakout_success = \
                r"\s+".join([r"Breakout", r"process", r"got",
                             r"successfully", r"completed"])
            expected_msg_breakout_mode_same = \
                r"\s+".join([r"No", r"action", r"will", r"be", r"taken", r"as",
                             r"current", r"and", r"desired", r"Breakout",
                             r"Mode", r"are", r"same"])
            with allure.step("Verify breakout command output"):
                if not re.search(f"{expected_msg_breakout_success}|{expected_msg_breakout_mode_same}",
                                 output, re.IGNORECASE):
                    logger.error(f"Breakout command didn't return expected message: {expected_msg_breakout_success}")
                    raise AssertionError("Verification of Breakout command failed")

    @staticmethod
    def parse_added_breakout_ports(breakout_cmd_output):
        """
        Extracts from breakout command output the dictionary of created ports and returns it as dictionary.
        :param breakout_cmd_output: the breakout command output, i.e,
            Running Breakout Mode : 1x100G[50G,40G,25G,10G,1G]
            Target Breakout Mode : 2x50G[25G,10G,1G]
            ​...
            ports to be added :
             {
                "Ethernet124": "50000",
                "Ethernet126": "50000"
            }
            U+001B[4mBreakout process got successfully completed.
            Please note loaded setting will be lost after system reboot. To preserve setting, run `config save`.
        :return: a dictionary of created ports after breakout command:
        i.e,
            {
            "Ethernet124": "50000",
            "Ethernet126": "50000"
            }
        in case breakout mode is already applied, the created breakout ports returned are {}
        """
        expected_msg_breakout_mode_same = \
            r"\s+".join([r"No", r"action", r"will", r"be", r"taken", r"as",
                         r"current", r"and", r"desired", r"Breakout",
                         r"Mode", r"are", r"same"])
        created_breakout_ports = {}
        if not re.search(expected_msg_breakout_mode_same, breakout_cmd_output, re.IGNORECASE):
            regex_prefix = r"ports to be added :.*(\r|\n)*.*"
            regex_grep_added_ports_json_dict = r"({(.|\n)*,*(\n|\r|.)*})"
            regex_suffix = r"(\n|\r|.)*sonic_yang" if "sonic_yang" in breakout_cmd_output else ''
            regex = regex_prefix + regex_grep_added_ports_json_dict + regex_suffix
            matched_added_ports_json_string = re.search(regex, breakout_cmd_output, re.IGNORECASE).group(2)
            created_breakout_ports = json.loads(matched_added_ports_json_string)
        return created_breakout_ports

    def get_interface_current_breakout_mode(self, interface):
        """
        return current breakout mode on the interface
        :param interface: i.e, Ethernet0
        :return: breakout mode, i.e, 1x400G[200G,100G,50G,40G,25G,10G,1G]
        """
        output = self.engine.run_cmd(f"show interfaces breakout current-mode {interface}")
        breakout_pattern = r"(\dx\d+G\[[\d*G,]*\]|\dx\d+G|\dx\d+\[[\d+,]*\])"
        breakout_mode = re.search(breakout_pattern, output).group(1)
        return breakout_mode

    def config_auto_negotiation_mode(self, interface, mode):
        """
        configure the auto negotiation mode on the interface
        :param interface: i.e, Ethernet0
        :param mode: the auto negotiation mode to be configured, i.e. 'enabled'/'disabled'
        :return: the command output
        """
        return self.engine.run_cmd("sudo config interface {} autoneg {interface_name} {mode}"
                                   .format(self.multi_asic_config_cmd_ext, interface_name=interface, mode=mode))

    def config_advertised_speeds(self, interface, speed_list):
        """
        configure the advertised speeds on the interface
        :param interface: i.e, Ethernet0
        :param speed_list: a string of speed configuration, i.e, "10000,50000" or "all"
        :return:  the command output
        """
        return self.engine.run_cmd("sudo config interface {} advertised-speeds {interface_name} {speed_list}"
                                   .format(self.multi_asic_config_cmd_ext, interface_name=interface, speed_list=speed_list))

    def config_interface_type(self, interface, interface_type):
        """
        configure the type on the interface
        :param interface: i.e, Ethernet0
        :param interface_type: a string of interface type , i.e. "CR"/"CR2"
        :return: the command output
        """
        return self.engine.run_cmd("sudo config interface {} type {interface_name} {interface_type}"
                                   .format(self.multi_asic_config_cmd_ext, interface_name=interface, interface_type=interface_type))

    def config_advertised_interface_types(self, interface, interface_type_list):
        """
        configure the advertised types on the interface
        :param interface: i.e, Ethernet0
        :param interface_type_list:  a string of interfaces types to advertised , i.e. "CR,CR2" or "all"
        :return: the command output
        """
        return self.engine.run_cmd("sudo config interface {} advertised-types {interface_name} {interface_type_list}"
                                   .format(self.multi_asic_config_cmd_ext, interface_name=interface, interface_type_list=interface_type_list))

    def show_interfaces_auto_negotiation_status(self, interface='', validate=False):
        """
        show interfaces auto negotiation status for specific interface or all interfaces.
        :param interface: i.e, Ethernet0 or empty string '' for all interfaces
        :return: the command output
        """
        return self.engine.run_cmd(f"sudo show interfaces autoneg status {interface}", validate=validate)

    def parse_show_interfaces_auto_negotiation_status(self, interface=''):
        """
        Method which getting parsed interfaces auto negotiation status
        :return: a dictionary of parsed output of show command
        {'Ethernet0':
        {'Interface': 'Ethernet0',
        'Auto-Neg Mode': 'disabled',
        'Speed': '10G',
        'Adv Speeds': 'all',
        'Type': 'CR',
        'Adv Types': 'all',
        'Oper': 'up',
        'Admin': 'up'}}
        """
        ifaces_auto_neg_status = self.show_interfaces_auto_negotiation_status(interface=interface)
        return generic_sonic_output_parser(ifaces_auto_neg_status,
                                           headers_ofset=0, len_ofset=1, data_ofset_from_start=2,
                                           data_ofset_from_end=None, column_ofset=2, output_key='Interface')

    def show_port_mlxlink_status(self, pci_conf, port_number):
        """
        show the mlxlink status of the interface
        :param pci_conf: i.e, /dev/mst/mt53100_pciconf0
        :param port_number: i.e, 1,2,3
        :return: the command output
        """
        return self.engine.run_cmd("sudo mlxlink -d {pci_conf} -m -p {port_number}"
                                   .format(pci_conf=pci_conf, port_number=port_number))

    def configure_interface_fec(self, interface, fec_option):
        """
        configure the interface fec
        :param interface: i.e Ethernet0
        :param fec_option: i.e, none | fec91 | fec74
        :return: the command output
        """
        return self.engine.run_cmd("sudo config interface {} fec {interface_name} {fec_option}"
                                   .format(self.multi_asic_config_cmd_ext, interface_name=interface, fec_option=fec_option))

    def parse_port_mlxlink_status(self, pci_conf, port_number, port_name=""):
        """
        parsing the mlxlink status command,
        sudo mlxlink -d /dev/mst/mt53100_pciconf0 -m -p 35

        Operational Info
        ----------------
        State                           : Active
        Physical state                  : ETH_AN_FSM_ENABLE
        Speed                           : 1G
        Width                           : 1x
        FEC                             : No FEC
        Loopback Mode                   : No Loopback
        Auto Negotiation                : ON

        ...
        :param pci_conf: /dev/mst/mt53100_pciconf0
        :param port_number: i.e. 35
        :param port_name: i.e. Ethernet0, optional, only used as workaround to get Vendor Part Number when
            SAI_INDEPENDENT_MODULE_MODE is enabled
        :return: a dictionary with the parsed mlxlink status of the port
        for example,
        {
        }
        """
        port_mlxlink_status = self.show_port_mlxlink_status(pci_conf, port_number)
        regex_expressions = AutonegCommandConstants.REGEX_PARSE_EXPRESSION_FOR_MLXLINK
        parsed_info = {}
        for key, regex_expression_tuple in regex_expressions.items():
            regex_exp, expected_val, parsed_val, default_val, additional_val = regex_expression_tuple
            actual_val = re.search(regex_exp, port_mlxlink_status)
            # If SAI INDEPENDENT MODULE MODE is enabled, there's a known limitation that we can't get the
            # Vendor Part Number from mlxlink command, so we need to get it from transceiver eeprom as a workaround.
            # See https://redmine.mellanox.com/issues/3588062 for more details.
            if key == AutonegCommandConstants.PART_NUMBER and actual_val is None:
                logger.info(
                    "Unable to get Vendor Part Number from mlxlink command, getting it from transceiver eeprom"
                )
                if not port_name:
                    raise AssertionError(f"Port name is required for port_number: {port_number}")
                logger.info(
                    "Getting transceiver eeprom for port_name: %s, port_number: %s",
                    port_name, port_number
                )
                transceiver_eeprom_output = self.get_interfaces_transceiver_eeprom(port_name)
                logger.info("Transceiver eeprom output: %s", transceiver_eeprom_output)
                actual_val = re.search(r"Vendor PN\s*:\s*(\S+)", transceiver_eeprom_output)
            if actual_val:
                actual_val = actual_val.group(1)
            else:
                raise AssertionError("Couldn't get value match with regex expression {}".format(regex_exp))
            if key == AutonegCommandConstants.FEC:
                parsed_info[key] = self.parse_fec_mode(actual_val)
            elif key == AutonegCommandConstants.CABLE_SPEED:
                # When SAI INDEPENDENT MODULE MODE is enabled, Supported Cable Speed shows N/A
                # In this case, actual_val will be None since N/A is outside the capture group
                if actual_val is None:
                    logger.info("Supported Cable Speed is N/A, likely due to independent module mode")
                    parsed_info[key] = ["N/A"]
                else:
                    parsed_info[key] = actual_val.split(',')
            elif expected_val is not None and re.search(expected_val, actual_val):
                if additional_val is not None and re.search(additional_val, actual_val, re.IGNORECASE):
                    parsed_info[key] = additional_val
                else:
                    parsed_info[key] = parsed_val
            elif default_val is not None:
                parsed_info[key] = default_val
            else:
                parsed_info[key] = actual_val
        return parsed_info

    @staticmethod
    def parse_fec_mode(actual_mlxlink_fec_val):
        if re.search("Firecode FEC", actual_mlxlink_fec_val, re.IGNORECASE):
            return SonicConst.FEC_FC_MODE
        elif re.search(SonicConst.FEC_RS_MODE, actual_mlxlink_fec_val, re.IGNORECASE):
            return SonicConst.FEC_RS_MODE
        elif re.search("No FEC", actual_mlxlink_fec_val, re.IGNORECASE):
            return SonicConst.FEC_NONE_MODE
        else:
            raise AssertionError("Couldn't parse FEC value: {} on mlxlink output".format(actual_mlxlink_fec_val))

    def clear_counters(self, validate=True):
        """
        clear counters
        """
        self.engine.run_cmd("sonic-clear counters", validate=validate)
        self.engine.run_cmd("sudo sonic-clear counters", validate=validate)

    def clear_counters_by_force(self, validate: bool = True) -> None:
        """
        Clears all port counters at the SDK level inside the syncd container.

        sonic-clear only snapshots the CLI baseline, so the SDK counters keep their values. Call this
        before clear_counters(), otherwise the sonic-clear snapshot is taken on pre-reset values.

        Args:
            validate (bool): Verify the command succeeded.

        Returns:
            None
        """
        logger.info("Clearing all SDK port counters by force")
        self.engine.run_cmd(SonicConst.SDK_CLEAR_ALL_COUNTERS_FORCE_CMD, validate=validate)

    def clear_queue_counters(self):
        """
        clear queue counters
        """
        self.engine.run_cmd("sonic-clear queuecounters", validate=True)
        self.engine.run_cmd("sudo sonic-clear queuecounters", validate=True)

    def get_interface_supported_fec_modes(self, interface):
        """
        configure invalid fec mode on port to get actual fec modes supported on port from the error message
        :param interface: port name, i.e Ethernet0
        :return: a list of FEC modes supported on port
        """
        invalid_fec_option = "invalid_fec_mode"
        output = self.configure_interface_fec(interface, fec_option=invalid_fec_option)
        if re.search(r"Setting fec is not supported", output):
            return []
        fec_options_list_string = re.search(
            r"fec\s+{}\s+is\s+not\s+in\s+(\[.*\])".format(invalid_fec_option), output).group(1)
        fec_options_list = ast.literal_eval(fec_options_list_string)
        return fec_options_list

    def get_active_phy_port(self):
        intf_status = self.parse_interfaces_status()
        for interface in intf_status.keys():
            if (intf_status[interface]['Oper'] == 'up' and
                intf_status[interface]['Admin'] == 'up' and
                    interface.startswith('Ethernet')):

                return interface
        return None

    def get_passive_phy_ports(self):
        intf_status = self.parse_interfaces_status()
        passive_ports = []
        for interface in intf_status.keys():
            if intf_status[interface]['Oper'] == 'down' and \
                    intf_status[interface]['Admin'] == 'down' and interface.startswith('Ethernet'):
                passive_ports.append(interface)
        return passive_ports

    def get_admin_up_ports(self):
        intf_status = self.parse_interfaces_status()
        admin_up_ports = []
        for interface in intf_status.keys():
            if intf_status[interface]['Admin'] == 'up' and interface.startswith('Ethernet'):
                admin_up_ports.append(interface)
        return admin_up_ports

    def show_interfaces_counters(self, validate=False):
        """
        show interfaces counters
        :return: the command output
        """
        return self.engine.run_cmd("sudo show interfaces counters", validate=validate)

    def parse_interfaces_counters(self, headers_ofset=0, len_ofset=1, data_ofset_from_start=2):
        """
        Method which getting parsed interfaces counters
        :param headers_ofset: Line number in which we have headers
        :param len_ofset: Line number from which we can find len for all fields, in example above it is line 2
        :param data_ofset_from_start: Line number from which we will start parsing data and fill dictionary with results
        :return: dictionary, example: {'Ethernet0': {'STATE': 'U', 'RX_OK': '1,392', 'RX_BPS': '5.55 B/s',
        'RX_UTIL': '0.00%', 'RX_ERR': '0', 'RX_DRP': '0', 'RX_OVR': '0', 'TX_OK': '27,332', 'TX_BPS': '123.21 B/s',
        'TX_UTIL': '0.00%', 'TX_ERR': '0', 'TX_DRP': '0', 'TX_OVR': '0'}, 'Ethernet8': {'STATE'.......
        """
        ifaces_status = self.show_interfaces_counters()
        filtered_ifaces_status = re.sub(r"Last cached time was.*\d+\n", "", ifaces_status)
        return generic_sonic_output_parser(filtered_ifaces_status, headers_ofset=headers_ofset, len_ofset=len_ofset,
                                           data_ofset_from_start=data_ofset_from_start,
                                           data_ofset_from_end=None, column_ofset=2, output_key='IFACE')

    def show_interfaces_counters_detailed(self, interface, validate=False):
        """
        show interfaces counters detailed
        :param interface: port name, i.e Ethernet0
        :return: the command output
        """
        return self.engine.run_cmd(f"sudo show interfaces counters detailed {interface}", validate=validate)

    def show_interfaces_counters_errors(self, validate=False):
        """
        show interfaces counters errors
        :return: the command output
        """
        return self.engine.run_cmd("sudo show interfaces counters errors", validate=validate)

    def show_interfaces_counters_rates(self, validate=False):
        """
        show interfaces counters rates
        :return: the command output
        """
        return self.engine.run_cmd("sudo show interfaces counters rates", validate=validate)

    def show_interfaces_counters_description(self, interface='', validate=False):
        """
        show interfaces description
        :param interface: port name, i.e Ethernet0
        :return: the command output
        """
        return self.engine.run_cmd(f"sudo show interfaces description {interface}", validate=validate)

    def show_interfaces_naming_mode(self, validate=False):
        """
        show interfaces naming mode
        :return: the command output
        """
        return self.engine.run_cmd("sudo show interfaces naming_mode", validate=validate)

    def show_interfaces_neighbor_expected(self, validate=False):
        """
        show interfaces neighbor expected
        :return: the command output
        """
        return self.engine.run_cmd("sudo show interfaces neighbor expected", validate=validate)

    def add_sub_interface(self, sub_interface, vlan_id=""):
        """
        This method is to add sub interface
        :param sub_interface: sub interface name which should be added, example: Ethernet0.100
        :param vlan_id: vid such as 100, 200
        :return: command output
        """
        return self.engine.run_cmd("sudo config subinterface add {} {}".format(sub_interface, vlan_id))

    def del_sub_interface(self, sub_interface):
        """
        This method is to del sub interface
        :param sub_interface: sub interface name which should be deleted, example: Ethernet0.100
        :return: command output
        """
        return self.engine.run_cmd("sudo config subinterface del {}".format(sub_interface))

    def config_port_scheduler(self, port_scheduler, shaper_value):
        """
        This method is to config the scheduler with given shaper value.
        Here is how shaper value applied to the low layer:
        1. for 0, it will be converted to SXD_COS_SHAPER_RATE_MAX
        2. for range (0, MIN_SHAPER_RATE_BPS), it will be 0. MIN_SHAPER_RATE_BPS = (200 * 1000 * 1000 / 8) = 25000000bps
        when set the value in this range, the speed of the port will be 0
        3. for the rest values, it will be value / 1000 * 80, it is to open the shaper.
        :param port_scheduler: the scheduler name
        :param shaper_value: the value of the shaper on the scheduler
        :return: command output
        """
        return self.engine.run_cmd(f'redis-cli -n 4 hset "SCHEDULER|{port_scheduler}" type STRICT pir {shaper_value}')

    def config_port_qos_map(self, interface, port_scheduler):
        """
        This method is config the qos map for the interface
        :param interface: interface name
        :param interface: the scheduler name
        :return: command output
        """
        return self.engine.run_cmd(f'redis-cli -n 4 hset "PORT_QOS_MAP|{interface}" scheduler {port_scheduler}')

    def del_port_scheduler(self, port_scheduler):
        """
        This method is to delete the scheduler.
        :param port_scheduler: the scheduler name
        :return: command output
        """
        return self.engine.run_cmd(f'redis-cli -n 4 del "SCHEDULER|{port_scheduler}"')

    def del_port_qos_map(self, interface, port_scheduler):
        """
        This method is to delete the qos map for the interface
        :param interface: interface name
        :param interface: the scheduler name
        :return: command output
        """
        return self.engine.run_cmd(f'redis-cli -n 4 hdel "PORT_QOS_MAP|{interface}" scheduler {port_scheduler}')

    def show_queue_counters(self, interface):
        """
        show queue counters
        :param interface: port name, i.e Ethernet111
        :return: the command output
        """
        return self.engine.run_cmd(f"show queue counters {interface}")

    def parse_show_queue_counters(self, interface):
        """
        parse show queue counters
        :param interface: port name, i.e Ethernet111
        :return: dictionary, example:
        {'TxQ': 'UC0', {'Counter/pkts': '0', 'Counter/bytes': '0', 'Drop/pkts': '0', 'Drop/bytes': 'N/A', 'Port': 'Ethernet111'}}
        """
        show_queue_counters_output = self.show_queue_counters(interface)
        return generic_sonic_output_parser(show_queue_counters_output, headers_ofset=2, len_ofset=3,
                                           data_ofset_from_start=4, data_ofset_from_end=None, column_ofset=2, output_key='TxQ')

    def get_all_interfaces_mac_addresses(self, validate=True):
        """
        Get all interfaces mac addresses
        :param validate: validate execution
        :return: ETHERNET0:00:11:22:33:44:55
        ETHERNET1:00:11:22:33:44:56
        """
        all_interfaces_mac_addresses = self.engine.run_cmd("sudo ip -o link show | awk '/link\\/ether/ {print $2 " " $17}'", print_output=False, validate=validate)
        return {line.split(":")[0]: line.split(":")[1].strip() for line in all_interfaces_mac_addresses.strip().splitlines() if line}

    def get_sorted_ports_list(self, ports_list, breakout=None):
        return sorted(ports_list, key=lambda port: int(re.search(r'Ethernet(\d+)', port).group(1)))

    def get_counters_for_queue(self, show_queue_counters_dict, queue):
        queue_counter_pkts = int(show_queue_counters_dict[f"UC{queue}"]["Counter/pkts"].replace(",", ""))
        queue_drop_pkts = int(show_queue_counters_dict[f"UC{queue}"]["Drop/pkts"].replace(",", ""))
        return queue_counter_pkts, queue_drop_pkts

    def get_counters_for_queue_bytes(self, show_queue_counters_dict, queue, packet_size):
        queue_counter_bytes = int(show_queue_counters_dict[f"UC{queue}"]["Counter/bytes"].replace(",", ""))
        queue_drop_bytes = int(show_queue_counters_dict[f"UC{queue}"]["Drop/pkts"].replace(",", "")) * packet_size
        return queue_counter_bytes, queue_drop_bytes

    def get_sum_queue_bytes(self, show_queue_counters_dict, queues_list):
        sum_queue_bytes = 0
        for queue in queues_list:
            queue_counter_bytes, queue_counter_drop_bytes = self.get_counters_for_queue_bytes(show_queue_counters_dict, queue, PerfConsts.PACKET_SIZE_4K)
            sum_queue_bytes += queue_counter_bytes
        return sum_queue_bytes

    def parse_port_queuestat(self, interface):
        """
        parse port queuestat
        :param interface: port name, i.e Ethernet111
        :return: dictionary, example:
        """
        show_interface_queuestat_output = self.engine.run_cmd(f"sudo queuestat -p {interface}")
        return generic_sonic_output_parser(show_interface_queuestat_output, headers_ofset=2, len_ofset=3,
                                           data_ofset_from_start=4, data_ofset_from_end=None, column_ofset=2, output_key='TxQ')

    def parse_port_portstat(self, interface, trim_flag=False):
        """
        parse port portstat
        :param interface: port name, i.e Ethernet111
        :param trim_flag: False by default, if True, add --trim to the command
        :return: dict , i.e,
        {'IFACE': 'Ethernet30', 'STATE': 'U', 'TRIM_PKTS': '3,699,151,700', 'TRIM_TX_PKTS': '3,699,151,700', 'TRIM_DRP_PKTS': '0'}
        """
        cmd_suffix = "--trim" if trim_flag else ""
        show_interface_portstat_output = self.engine.run_cmd(f"sudo portstat -i {interface} {cmd_suffix}")
        parsed_output = generic_sonic_output_parser(show_interface_portstat_output,
                                                    headers_ofset=1, len_ofset=2,
                                                    data_ofset_from_start=3, data_ofset_from_end=None,
                                                    column_ofset=2, output_key='IFACE')
        return parsed_output[interface]

    def get_trimming_counters(self, queuestat_dict, queue):
        queue_trimmed_pkts = int(queuestat_dict[f"UC{queue}"]["Trim/pkts"].replace(",", ""))
        queue_drop_pkts = int(queuestat_dict[f"UC{queue}"]["Drop/pkts"].replace(",", ""))
        return queue_trimmed_pkts, queue_drop_pkts
