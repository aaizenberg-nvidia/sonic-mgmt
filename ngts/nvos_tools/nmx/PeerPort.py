import logging
import random

import allure
from ngts.cli_wrappers.nvue.nvue_peer_port_clis import NvuePeerPortCli
from ngts.cli_wrappers.openapi.openapi_peer_port_clis import OpenApiPeerPortCli
from ngts.nvos_constants.constants_nvos import ApiType, OutputFormat
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import PeerPortConsts

logger = logging.getLogger()


class PeerPort(BaseComponent):
    api_obj = {ApiType.NVUE: NvuePeerPortCli, ApiType.OPENAPI: OpenApiPeerPortCli}

    def __init__(self, parent_obj, peer_port_name=""):
        BaseComponent.__init__(self, parent=parent_obj,
                               path=f'/{PeerPortConsts.PEER_PORT}' + (f'/{peer_port_name}' if peer_port_name else ''),
                               api={ApiType.NVUE: NvuePeerPortCli, ApiType.OPENAPI: OpenApiPeerPortCli})
        self.port_obj = parent_obj
        self.counters = Counters(self)
        self.link = Link(self)

    def get_list_of_ports(self):
        with allure.step('Get the list of all peer ports from show command'):
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(self.show()).verify_result()
            port_list = []
            for port_name in output_dictionary.keys():
                port = PeerPortInterface(port_name)
                port_list.append(port)
            return port_list

    def get_random_peer_port(self):
        with allure.step('Get the list of all peer ports and select a random port'):
            port_list = self.get_list_of_ports()
            if len(port_list) == 0:
                return None
            random_peer_port = random.choice(port_list)
            return random_peer_port

    def peer_port_by_name(self, peer_port_name):
        peer_port_list = self.get_list_of_ports()
        for peer_port_interface in peer_port_list:
            if peer_port_interface.peer_port_name == peer_port_name:
                return peer_port_interface

    def peer_port_names_get(self):
        return [peer_port_interface.peer_port_name for peer_port_interface in self.get_list_of_ports()]


class PeerPortInterface(BaseComponent):
    api_obj = {ApiType.NVUE: NvuePeerPortCli, ApiType.OPENAPI: OpenApiPeerPortCli}

    def __init__(self, peer_port_name, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj,
                               api={ApiType.NVUE: NvuePeerPortCli, ApiType.OPENAPI: OpenApiPeerPortCli}, path='')
        self.peer_port = PeerPort(self, peer_port_name)
        self.peer_port_name = peer_port_name

    def __str__(self):
        return f"{self.__class__.__name__}('{self.peer_port_name}')"

    def show_peer_port(self, dut_engine=None, fae_param="", output_format=OutputFormat.json):
        """
        Executes show interface
        :param output_format: OutputFormat
        :param fae_param: optional - to command with fae
        :param dut_engine: ssh engine
        :return: str/json output
        """
        with allure.step(f"Executing show peer-port for {self.peer_port_name}"):
            if not dut_engine:
                dut_engine = TestToolkit.get_engine()
            return PeerPortInterface.api_obj[TestToolkit.tested_api].show_peer_port(engine=dut_engine,
                                                                                    peer_port_name=self.peer_port_name,
                                                                                    fae_param=fae_param,
                                                                                    output_format=output_format)


class Counters(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/counters')
        self.link = Link(self)
        self.nvl = Nvl(self)


class Link(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/link')
        self.phy = Phy(self)
        self.health = BaseComponent(self, path='/health')


class Nvl(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/nvl')


class Phy(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/phy')
        self.detail = Detail(self)
        self.health = Health(self)


class Health(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/health')
        self.histogram = Histogram(self)


class Histogram(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/histogram')


class Detail(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/detail')
