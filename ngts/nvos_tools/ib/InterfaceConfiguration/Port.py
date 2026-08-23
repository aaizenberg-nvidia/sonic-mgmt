import logging
import re

import allure
from retry import retry

from ngts.cli_wrappers.linux.linux_ip_clis import LinuxIpCli
from ngts.cli_wrappers.nvue.nvue_ib_interface_clis import NvueIbInterfaceCli
from ngts.cli_wrappers.openapi.openapi_ib_interface_clis import OpenApiIbInterfaceCli
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_constants.constants_nvos import OutputFormat
from ngts.nvos_tools.acl.acl import Acl
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool

logger = logging.getLogger()
WAIT_FOR_PORT_STATE_TRIES = 15
WAIT_FOR_PORT_STATE_DELAY = 5


class PortRequirements:
    default_port_requirements = {IbInterfaceConsts.NAME: "",
                                 IbInterfaceConsts.DESCRIPTION: "",
                                 IbInterfaceConsts.LINK_MTU: "",
                                 IbInterfaceConsts.LINK_BREAKOUT: "",
                                 IbInterfaceConsts.LINK_SPEED: "",
                                 IbInterfaceConsts.LINK_IB_SPEED: "",
                                 IbInterfaceConsts.LINK_STATE: "",
                                 IbInterfaceConsts.LINK_LOGICAL_PORT_STATE: "",
                                 IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE: "",
                                 IbInterfaceConsts.TYPE: ""}
    port_requirements = None
    interface_type = ''
    strict_fields = None

    def __init__(self, interface_type=''):
        self.port_requirements = self.default_port_requirements.copy()
        self.interface_type = interface_type  # Port type refers to "internal/external/fnm ports"
        # Requirements that a port must report explicitly. By default a requirement
        # whose field is absent from the port output is treated as satisfied; fields
        # listed here reject such a port instead.
        self.strict_fields = set()

    def __str__(self):
        return self.__class__.__name__ + "=" + str({k: v for k, v in self.port_requirements.items() if v})

    def set_port_name(self, name):
        self.port_requirements[IbInterfaceConsts.NAME] = name

    def set_port_description(self, description):
        self.port_requirements[IbInterfaceConsts.DESCRIPTION] = description

    def set_port_speed(self, speed):
        self.port_requirements[IbInterfaceConsts.LINK_SPEED] = speed

    def set_port_ib_speed(self, ib_speed, strict=False):
        """
        Require a negotiated ib-speed (e.g. ``xdr``).

        :param strict: when True, a port that does not report ib-speed at all is
            rejected rather than accepted. ib-speed only appears once the port is
            Active, so a caller that needs the negotiated speed cannot treat
            "not reported" as a match.
        """
        self.port_requirements[IbInterfaceConsts.LINK_IB_SPEED] = ib_speed
        if strict:
            self.strict_fields.add(IbInterfaceConsts.LINK_IB_SPEED)
        else:
            self.strict_fields.discard(IbInterfaceConsts.LINK_IB_SPEED)

    def set_port_state(self, state):
        self.port_requirements[IbInterfaceConsts.LINK_STATE] = state

    def set_port_type(self, req_type):
        self.port_requirements[IbInterfaceConsts.TYPE] = req_type

    def set_port_logical_state(self, req_type):
        self.port_requirements[IbInterfaceConsts.LINK_LOGICAL_PORT_STATE] = req_type


class Port(BaseComponent):
    api_obj = {ApiType.NVUE: NvueIbInterfaceCli, ApiType.OPENAPI: OpenApiIbInterfaceCli}

    def __init__(self, name='eth0', show_output_dictionary={}, name_in_redis='', parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj,
                               api={ApiType.NVUE: NvueIbInterfaceCli, ApiType.OPENAPI: OpenApiIbInterfaceCli}, path='')
        self.name = name
        self.show_output_dictionary = show_output_dictionary
        self.name_in_redis = name_in_redis
        self.interface = Interface(self, name)
        self.acl = Acl(self)

        self.asic_letter = None
        self.port_number = None
        self.local_port = None
        self.split_number = None
        self.plane_number = None
        try:
            self.asic_letter, self.port_number, self.local_port, self.split_number, self.plane_number = (
                self.parse_port_name(self.name))
        except BaseException:
            pass

    def __str__(self):
        return f"{self.__class__.__name__}('{self.name}')"

    def __repr__(self):
        return f"Port(name={self.name}, show_output_dictionary={self.show_output_dictionary}, name_in_redis={self.name_in_redis})"

    @staticmethod
    def get_list_of_active_ports(port_type=IbInterfaceConsts.IB_PORT_TYPE, interface_type=''):
        """
        Return a list of ports which are connected to a traffic server
        """
        with allure.step('Get a list of ports which state is up and logical state is Active'):
            port_requirements_object = PortRequirements(interface_type)
            port_requirements_object.set_port_state(NvosConsts.LINK_STATE_UP)
            port_requirements_object.set_port_type(port_type)
            port_requirements_object.set_port_logical_state("Active")
            up_port_list = Port.get_list_of_ports(None, port_requirements_object)

        return up_port_list

    @staticmethod
    def get_list_of_ports(dut_engine=None, port_requirements_object=None):
        """
        Returns a list of port according to port_requirements
        :param dut_engine: ssh dut engine
        :param port_requirements_object: PortRequirements object
        :return: a list of Port objects
        """
        with allure.step('Search for ports that meet provided requirements'):
            if not dut_engine:
                dut_engine = TestToolkit.get_engine()

            logging.info("get_list_of_ports - Searching for relevant ports")
            output_dictionary = OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
                Port.show_interface(dut_engine)).verify_result()

            port_list = []

            if not port_requirements_object or not port_requirements_object.port_requirements:
                logging.info("get_list_of_ports - port_requirements not provided. Selecting all ports.")
                for port_name in output_dictionary.keys():
                    if port_requirements_object is not None:
                        if port_requirements_object.interface_type in port_name:
                            port_list.append(Port(port_name, "", ""))
                    else:
                        port_list.append(Port(port_name, "", ""))
                return port_list

            for port_name, port_details in output_dictionary.items():
                select_port = True
                if port_requirements_object.interface_type not in port_name:
                    select_port = False

                for field_name, port_requirements_list in port_requirements_object.port_requirements.items():

                    if field_name == IbInterfaceConsts.NAME and port_requirements_list and \
                       port_requirements_list != port_name:
                        select_port = False
                        break
                    elif field_name == IbInterfaceConsts.LINK_STATE and isinstance(port_requirements_list, list):
                        if not any(port_details[field_name] == expected_state for expected_state in port_requirements_list):
                            select_port = False
                            break
                    elif port_requirements_list and field_name in (port_requirements_object.strict_fields or ()) \
                            and port_details.get(field_name) != port_requirements_list:
                        select_port = False
                        break
                    elif port_requirements_list and field_name in port_details.keys() and port_details[field_name] != port_requirements_list:
                        select_port = False
                        break

                if select_port:
                    port_list.append(Port(port_name, {}, ""))
                    logging.info("get_list_of_ports - {port_name} meets the requirements".format(port_name=port_name))

            return port_list

    def update_output_dictionary(self, dut_engine=None):
        """
        Execute "show" command and create the output dictionary for a specific port
        :param dut_engine: ssh engine
        """
        with allure.step('Execute "show" command and create the output dictionary for {port_name}'.format(
                port_name=self.name)):
            if not dut_engine:
                dut_engine = TestToolkit.get_engine()

            logging.info("Updating output dictionary of '{port_name}'".format(port_name=self.name))
            self.show_output_dictionary = OutputParsingTool.parse_show_interface_output_to_dictionary(
                Port.show_interface(dut_engine, self.name)).get_returned_value()

    @staticmethod
    def show_interface(dut_engine=None, port_names="", fae_param="", output_format=OutputFormat.json):
        """
        Executes show interface
        :param output_format: OutputFormat
        :param port_names: ports on which to run show command
        :param fae_param: optional - to command with fae
        :param dut_engine: ssh engine
        :return: str/json output
        """
        with allure.step('Execute show interface for {port_names}'.format(port_names=port_names)):
            if not dut_engine:
                dut_engine = TestToolkit.get_engine()

            logging.info("Executing show interface for {port_names}".format(port_names=port_names))
            return Port.api_obj[TestToolkit.tested_api].show_interface(engine=dut_engine,
                                                                       port_name=port_names,
                                                                       fae_param=fae_param,
                                                                       output_format=output_format)

    @staticmethod
    def _wait_for_port_state(port_obj, expected_state):
        with allure.step("Waiting for port {} state {}".format(port_obj.name, expected_state)):
            output_dictionary = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                port_obj.interface.link.show()).get_returned_value()
            current_state = output_dictionary[IbInterfaceConsts.LINK_STATE]
            assert current_state == expected_state, "Current state {} is not {} as expected".format(current_state,
                                                                                                    expected_state)

    def get_port_ip_addresses(self, dut_engine):
        with allure.step(f"Get mgmt port {self.name} ip addresses"):
            try:
                ip_addresses_show = list(OutputParsingTool.parse_json_str_to_dictionary(
                    self.interface.ipv4.address.show(dut_engine=dut_engine)).get_returned_value())
                return ip_addresses_show[0].split("/")[0]
            except Exception as e:
                logger.info(f"Fallback: parsing 'ip addr' since older version image 25.02.6000 - {e}")
                linux_ip_cli = LinuxIpCli(dut_engine)
                ip_info = linux_ip_cli.get_ip_info(ip_ver='4')
                ip_addresses = linux_ip_cli.get_interface_ip_addresses(self.name, ip_info)
                return ip_addresses[0] if ip_addresses else None

    @staticmethod
    def parse_port_name(name):
        """swA13p2pl1 --> ('A', 13, 2, None, 1).   sw13p2s1 --> (None, 13, 2, 1, None)"""
        match = re.fullmatch(r'[sS][wW]([A-Za-z])?\s*(\d+)p(\d+)(?:s(\d+))?(?:pl(\d+))?', name)
        if not match:
            raise ValueError(f"Invalid port name format: {name}")

        asic_letter = match.group(1)  # This can be None or 'A' or 'B'
        port_number = int(match.group(2))
        local_port = int(match.group(3))
        split_number = int(match.group(4)) if match.group(4) else None
        plane_number = int(match.group(5)) if match.group(5) else None
        result = asic_letter, port_number, local_port, split_number, plane_number
        logger.info(f'parsed "{name}" ==> {result}')
        return result

    def get_plane_port(self, plane_number) -> "Port":
        """ Port('sw1p1') --> Fae(port_name='sw1p1pl3').port """
        if self.is_plane_port():
            raise ValueError(f'{self=} is a plane-port')
        from ngts.nvos_tools.infra.Fae import Fae
        # can't import at the top of the file because of a circular import between Port.py and Fae.py
        return Fae(port_name=f"{self.name}pl{plane_number}").port

    def is_plane_port(self) -> bool:
        return bool(re.search(r'pl\d+$', self.name))

    def get_aggregated_port(self):
        """'sw1p1pl3' --> 'sw1p1';  'sw1p1' --> 'sw1p1'"""
        return Port(name=re.sub(r'pl\d+$', '', self.name))

    def get_transceiver_name(self):
        """ 'swA1p1pl3' --> 'swA1' """
        return re.search(r'^\D+\d+', self.name).group(0)

    @staticmethod
    def wait_for_port_state(port_obj, expected_state, tries=None, delay=None):
        tries, delay = tries or WAIT_FOR_PORT_STATE_TRIES, delay or WAIT_FOR_PORT_STATE_DELAY

        @retry(Exception, tries=tries, delay=delay)
        def _wait_for_port_state():
            Port._wait_for_port_state(port_obj, expected_state)

        return _wait_for_port_state()
