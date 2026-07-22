from ngts.tests_nvos.system.stats.constants import StatsConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.system.Files import Files
from ngts.tools.test_utils import allure_utils as allure


class Stats(BaseComponent):
    def __init__(self, parent_obj, devices_dut):
        BaseComponent.__init__(self, parent=parent_obj, path='/stats')
        self.category = StatsCategory(self, devices_dut)
        self.files = Files(self)

    def action_general(self, action_str, dut_engine=None):
        if not dut_engine:
            dut_engine = TestToolkit.get_engine()
        with allure.step("Run system stats action '{action_type}'".format(action_type=action_str)):
            return SendCommandTool.execute_command(self.api_obj[TestToolkit.tested_api].action_general,
                                                   dut_engine, action_str, self.get_resource_path())


class StatsCategory(BaseComponent):
    def __init__(self, parent_obj, devices_dut):
        BaseComponent.__init__(self, parent=parent_obj, path='/category')
        # Per instance: a shared dict would keep categories a later device prunes,
        # and hand back objects still parented to the System that registered them.
        self.categoryName = {}
        if devices_dut:
            for name in devices_dut.category_list:
                self.categoryName.update({name: StatsCategoryName(self, name)})
            # Counters Historical Analysis categories are kept in a separate list on
            # the device so the existing stats suite is unaffected; register them here.
            for name in getattr(devices_dut, 'new_category_list', []):
                self.categoryName.update({name: StatsCategoryName(self, name)})
            self.categoryName.update(
                {StatsConsts.INVALID_CATEGORY_NAME: StatsCategoryName(self, StatsConsts.INVALID_CATEGORY_NAME)})


class StatsCategoryName(BaseComponent):
    def __init__(self, parent_obj, resource):
        BaseComponent.__init__(self, parent=parent_obj, path='/' + resource)

    def action_general(self, action_str):
        with allure.step("Run system stats category action '{action_type}'".format(action_type=action_str)):
            return SendCommandTool.execute_command(self.api_obj[TestToolkit.tested_api].action_general,
                                                   TestToolkit.get_engine(), action_str, self.get_resource_path())
