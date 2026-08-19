import logging
import allure

from ngts.nvos_tools.infra.DefaultDict import DefaultDict
from ngts.cli_wrappers.nvue.nvue_cluster_clis import NvueClusterCli
from ngts.cli_wrappers.openapi.openapi_cluster_clis import OpenApiClusterCli
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit

logger = logging.getLogger()


class Trays(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj,
                         api={ApiType.NVUE: NvueClusterCli, ApiType.OPENAPI: OpenApiClusterCli},
                         path='/trays')
        self.tray: dict[str, 'Tray'] = DefaultDict(lambda tray_id: Tray(self, tray_id=tray_id))

    def action_update_maintenance_state(self, tray_id='', maintenance_state='up', engine=None):
        """
        Update maintenance state for a tray.
        tray_id: can be slot-number (e.g., '0') or chassis-sn.slot-number (e.g., 'MT2352X00001.0')
        """
        engine = engine if engine else TestToolkit.engines.dut
        with allure.step(f'Update tray {tray_id} maintenance state to {maintenance_state}'):
            return self._cli_wrapper.action_update_sdn_trays_maintenance_state(engine, self.get_resource_path(),
                                                                               tray_id=tray_id,
                                                                               maintenance_state=maintenance_state)

    def action_restore_maintenance_state(self, tray_id='', engine=None):
        """
        Restore maintenance state for a tray back to default (up).
        tray_id: can be slot-number (e.g., '0') or chassis-sn.slot-number (e.g., 'MT2352X00001.0')
        """
        engine = engine if engine else TestToolkit.engines.dut
        with allure.step(f'Restore tray {tray_id} maintenance state to default'):
            return self._cli_wrapper.action_restore_sdn_trays_maintenance_state(engine, self.get_resource_path(),
                                                                                tray_id=tray_id)


class Tray(BaseComponent):
    def __init__(self, parent, tray_id):
        super().__init__(parent=parent, path=f'/{tray_id}')
        self.tray_id = tray_id

    def action_update_maintenance_state(self, maintenance_state='up', engine=None):
        engine = engine if engine else TestToolkit.engines.dut
        with allure.step(f'Update tray {self.tray_id} maintenance state to {maintenance_state}'):
            return SendCommandTool.execute_command(self._cli_wrapper.action_update_sdn_trays_maintenance_state,
                                                   engine, self.parent_obj.get_resource_path(), self.tray_id,
                                                   maintenance_state)

    def action_restore_maintenance_state(self, engine=None):
        engine = engine if engine else TestToolkit.engines.dut
        with allure.step(f'Restore tray {self.tray_id} maintenance state to default'):
            return SendCommandTool.execute_command(self._cli_wrapper.action_restore_sdn_trays_maintenance_state,
                                                   engine, self.parent_obj.get_resource_path(), self.tray_id)
