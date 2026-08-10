from .IfIndex import IfIndex
from .IpV4 import IpV4
from .IpV6 import IpV6
from .Cpo import InterfaceCpo
from .Link import LinkMgmt
from .LldpInterface import LldpInterface
from .Type import Type
from .Counters import Counters
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import InternalNvosConsts, IbInterfaceConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.cli_wrappers.nvue.nvue_ib_interface_clis import NvueIbInterfaceCli
from ngts.cli_wrappers.openapi.openapi_ib_interface_clis import OpenApiIbInterfaceCli
from ngts.nvos_constants.constants_nvos import ApiType, IbConsts, NvosConst
from ngts.nvos_tools.acl.acl import Acl
from retry import retry
import allure
import logging
import time
import re

logger = logging.getLogger()


class Interface(BaseComponent):
    def __init__(self, parent_obj, port_name="", path=None):
        self.mgmt_path = path if path else 'interface'
        BaseComponent.__init__(self, parent=parent_obj,
                               path='/interface' + (f'/{port_name}' if port_name else ''),
                               api={ApiType.NVUE: NvueIbInterfaceCli, ApiType.OPENAPI: OpenApiIbInterfaceCli})
        self.port_obj = parent_obj
        self.type = Type(self.port_obj)
        self.ifindex = IfIndex(self.port_obj)
        # New IPv4/IPv6 structure
        self.ipv4 = IpV4(self)
        self.ipv6 = IpV6(self)
        self.vrf = BaseComponent(self, path='/vrf')
        self.link = LinkMgmt(self)
        self.plan_ports = BaseComponent(self, path='/plan-ports')
        self.acl = Acl(self)
        self.lldp = LldpInterface(self)
        self.counters = Counters(self)
        self.cpo = InterfaceCpo(self)

    def wait_for_port_state(self, state, timeout=InternalNvosConsts.DEFAULT_TIMEOUT, logical_state=None, sleep_time=2,
                            dut_engine=None):
        with allure.step("Wait for '{port}' to reach state '{state}' (timeout: {timeout})".format(
                port=self.port_obj.name, state=state, timeout=timeout)):
            logger.info("Wait for '{port}' to reach state '{state}' (timeout: {timeout})".format(
                port=self.port_obj.name, state=state, timeout=timeout))

            result_obj = ResultObj(True, "")
            timer = timeout
            while OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                    self.link.show(dut_engine=dut_engine)).\
                    get_returned_value()[IbInterfaceConsts.LINK_STATE] != state and timer > 0:
                time.sleep(sleep_time)
                timer -= sleep_time

            if OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                    self.link.show(dut_engine=dut_engine)).get_returned_value()[IbInterfaceConsts.LINK_STATE] == state:
                logger.info("'{port}' successfully reached state '{state}'".format(
                    port=self.port_obj.name, state=state))
                result_obj.info = "'{port}' successfully reached state '{state}'".format(port=self.port_obj.name,
                                                                                         state=state)

            if timer <= 0:
                result_obj.info = "Timeout occurred while waiting for '{port}' to reach state '{state}'".format(
                    port=self.port_obj.name, state=state)
                result_obj.result = False
                return result_obj

            if logical_state:
                while OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                        self.port_obj.interface.link.show(dut_engine=dut_engine)). \
                        get_returned_value()[IbInterfaceConsts.LINK_LOGICAL_PORT_STATE] != logical_state and timer > 0:
                    time.sleep(sleep_time)
                    timer -= sleep_time
                if OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                        self.link.show(dut_engine=dut_engine)). \
                        get_returned_value()[IbInterfaceConsts.LINK_LOGICAL_PORT_STATE] == logical_state:
                    logger.info("'{port}' successfully reached logical_state '{state}'".format(
                        port=self.port_obj.name, state=logical_state))
                    result_obj.info += "\n'{port}' successfully reached logical_state '{state}'".format(
                        port=self.port_obj.name, state=logical_state)

                if timer <= 0:
                    result_obj.info += "\nTimeout occurred while waiting for '{port}' to reach logical_state " \
                        "'{state}'".format(port=self.port_obj.name, state=logical_state)
                    result_obj.result = False

            allure.attach(
                body=f"It took port {self.port_obj.name} {timeout - timer} seconds to reach state {state}",
                name=f"{self.port_obj.name} link-up time",
                attachment_type=allure.attachment_type.TEXT,
            )
            return result_obj

    @retry(Exception, tries=10, delay=5)
    def wait_for_port_speed(self, port_obj, system_type, speed_to_verify):
        with allure.step(f"Waiting for port {port_obj.name} speed changed to {speed_to_verify}"):
            output_dictionary = OutputParsingTool.parse_show_interface_link_output_to_dictionary(self.port_obj.interface.link.show()).get_returned_value()
            speed_field = IbInterfaceConsts.LINK_IB_SPEED if system_type == NvosConst.IB_SWITCH_TYPE else IbInterfaceConsts.LINK_SPEED
            current_speed = output_dictionary[speed_field]
            assert current_speed == speed_to_verify, "Current speed {} is not as expected {}".\
                format(current_speed, speed_to_verify)

    @retry(Exception, tries=10, delay=2)
    def wait_for_mtu_changed(self, mtu_to_verify):
        with allure.step("Waiting for '{}' port mtu changed to {}".format(self.port_obj.name, mtu_to_verify)):
            output_dictionary = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                self.link.show()).get_returned_value()
            current_mtu = int(output_dictionary[IbInterfaceConsts.LINK_MTU])
            assert current_mtu == int(mtu_to_verify), "Current mtu {} is not as expected {}".\
                format(current_mtu, mtu_to_verify)

    def action_clear_counters(self, interface_name=None, engine=None, fae_param="",
                              expected_str="Cleared counters successfully"):
        """
        Clear interface counters.
        :param interface_name: specific port name/range to clear, or None for all ports.
        :param engine: ssh engine object, defaults to DUT engine.
        :param fae_param: optional - run the command with fae.
        :param expected_str: expected output string for validation.
        """
        if not engine:
            engine = TestToolkit.get_engine()
        cli_wrapper = self.port_obj._cli_wrapper if self.port_obj else self._cli_wrapper

        if interface_name:
            with allure.step("Clear counters for interface {}".format(interface_name)):
                return SendCommandTool.execute_command_expected_str(
                    cli_wrapper.action_clear_counters, expected_str, engine,
                    f'interface {interface_name}', fae_param)
        else:
            with allure.step("Clear counters for all interfaces"):
                logging.info("Clear counters for all interfaces")
                return SendCommandTool.execute_command(
                    cli_wrapper.action_clear_counters, engine, self.mgmt_path, fae_param)

    # Backward-compatible wrappers
    def action_clear_counter_for_all_interfaces(self, engine=None, fae_param=""):
        return self.action_clear_counters(engine=engine, fae_param=fae_param)

    def action_clear_counter_for_interface(self, dut_engine=None, interface_name=None, fae_param="",
                                           expected_str="Cleared counters successfully"):
        if not interface_name:
            raise ValueError("interface_name must be provided for per-interface clear")
        return self.action_clear_counters(interface_name=interface_name, engine=dut_engine,
                                          fae_param=fae_param, expected_str=expected_str)

    def filter(self, dut_engine=None, filter_name="", value=""):
        with allure.step(f"filter using {filter_name}={value}"):
            if not dut_engine:
                dut_engine = TestToolkit.get_engine()
            cli_wrapper = self.port_obj._cli_wrapper if self.port_obj else self._cli_wrapper
            result_obj = SendCommandTool.execute_command_expected_str(
                cli_wrapper.filter, "", dut_engine, filter_name, value)
            return result_obj

    def get_sorted_interfaces_list(self):
        with allure.step("get sorted interfaces list"):
            output_list = list(OutputParsingTool.parse_show_output_to_dict(self.show()).get_returned_value().keys())
            return sorted(output_list, key=divide_interface_name)[3:]

    def get_ipv6_address(self):
        """
        Get the IPv6 address for the interface
        Uses the new ipv6 structure
        """
        try:
            # Use the new ipv6 structure
            ipv6_addr = self.ipv6.get_primary_ip_address()
            if ipv6_addr:
                return ipv6_addr
        except Exception as e:
            logger.warning(f"Error getting IPv6 address using new structure, trying legacy method: {e}")

        # Fallback: parse full interface show output
        try:
            output = OutputParsingTool.parse_show_interface_output_to_dictionary(self.show()).get_returned_value()
            assert output, "show mgmt interface output is empty"

            # Get addresses from new ipv6 structure
            if 'ipv6' not in output or 'address' not in output['ipv6']:
                logger.warning(f"No IPv6 address information found in show output")
                return None

            addresses = output['ipv6']['address'].keys()

            for address in addresses:
                addr_clean = address.split("/")[0] if "/" in address else address
                # Skip localhost and link-local addresses
                if ":" in addr_clean and addr_clean != "::1" and not addr_clean.lower().startswith("fe80:"):
                    if len(addr_clean) >= 32:
                        return addr_clean

            return None
        except Exception as fallback_error:
            logger.error(f"Failed to get IPv6 address: {fallback_error}")
            return None


def divide_interface_name(string):
    reg = IbConsts.IB_INTERFACE_NAME_REGEX
    match = re.match(reg, string)
    if match:
        prefix = match.group(1)
        numeric_part = match.group(2)
        suffix = match.group(3)
        return prefix, int(numeric_part), suffix
    else:
        return "", "", ""
