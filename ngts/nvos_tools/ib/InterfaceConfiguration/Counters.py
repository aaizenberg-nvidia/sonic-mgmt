from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
import allure
import logging

logger = logging.getLogger()


class Counters(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/counters')
        # Counters can be accessed directly via show()
        # Returns all counter data including top-level fields (in-bytes, out-bytes, etc.)
        # and nested structures (ib, link, and nvl sub-sections)
        self.ib = BaseComponent(self, path='/ib')
        self.link = BaseComponent(self, path='/link')
        self.nvl = BaseComponent(self, path='/nvl')
        self.debug = BaseComponent(self, path='/debug')

    def clear_counters(self, dut_engine=None, fae_param=""):
        """
        Clears all interface counters

        :param dut_engine: DUT engine to use
        :param fae_param: optional - run the command with fae
        :return: ResultObj
        """
        if not dut_engine:
            dut_engine = TestToolkit.get_engine()

        # Get port name from parent Interface
        port_name = self.parent_obj.port_obj.name

        with allure.step('Clear counters for {port_name}'.format(port_name=port_name)):
            try:
                resource_path = f'interface {port_name}'
                result_obj = SendCommandTool.execute_command(
                    self.api_obj[TestToolkit.tested_api].action_clear_counters,
                    dut_engine,
                    resource_path,
                    fae_param
                )
                return result_obj
            except Exception as e:
                logger.error(f"Error clearing counters for {port_name}: {e}")
                return ResultObj(False, str(e))
