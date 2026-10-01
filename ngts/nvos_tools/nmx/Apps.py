import logging

import allure

from ngts.cli_wrappers.nvue.nvue_cluster_clis import NvueClusterCli
from ngts.cli_wrappers.openapi.openapi_cluster_clis import OpenApiClusterCli
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.DefaultDict import DefaultDict
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.nmx.ClusterInternal import ClusterInternal
from ngts.nvos_tools.nmx.Loglevel import Loglevel
from ngts.nvos_tools.nmx.Logstream import Logstream
from ngts.nvos_tools.nmx.Manager import Manager
from ngts.nvos_tools.nmx.Otlp import Otlp
from ngts.nvos_tools.nmx.RbacCluster import RbacApp
from ngts.nvos_tools.nmx.Type import Type

logger = logging.getLogger()


class Apps(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj,
                         api={ApiType.NVUE: NvueClusterCli, ApiType.OPENAPI: OpenApiClusterCli},
                         path='/apps')
        self.app_name: dict[str, ClusterApp] = DefaultDict(
            lambda app_name: ClusterApp(parent=self, app_name=app_name))
        self.installed = BaseComponent(self, path='/installed')
        self.running = BaseComponent(self, path='/running', force_api=ApiType.NVUE)


class ClusterApp(BaseComponent):
    def __init__(self, parent, app_name):
        super().__init__(parent=parent, path=f'/{app_name}')
        self.loglevel = Loglevel(self)
        self.manager = Manager(self)
        self.logstream = Logstream(self)
        self.type = Type(self)
        self.rbac = RbacApp(self)
        self.internal = ClusterInternal(self)
        self.otlp = Otlp(self)
        self.app_name = app_name

    def action_start_cluster_app(self, engine=None):
        engine = engine if engine else TestToolkit.get_engine()
        with allure.step('Start App'):
            return SendCommandTool.execute_command_expected_str(self._cli_wrapper.action_start_cluster_app,
                                                                "App has been successfully started", engine,
                                                                self.get_resource_path())

    def action_stop_cluster_app(self, engine=None):
        engine = engine if engine else TestToolkit.get_engine()
        with allure.step('Stop App'):
            return SendCommandTool.execute_command_expected_str(self._cli_wrapper.action_stop_cluster_app,
                                                                "App has been successfully stopped", engine,
                                                                self.get_resource_path())
