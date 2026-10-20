from ngts.nvos_constants.constants_nvos import ActionConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.tools.test_utils import allure_utils as allure


class PhyRecovery(BaseComponent):
    """Represents interface link phy-recovery subtree with go once action support."""

    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/phy-recovery')
        self.step_1 = BaseComponent(self, path='/step-1')

    def action_start_go_once(self) -> ResultObj:
        """
        Execute the go once phy-recovery action.
        Command: nv action start fae int <port> link phy-recovery

        Returns:
            ResultObj: Result object indicating success or failure of the action.
        """
        with allure.step(f"Execute action start go once for {self.get_resource_path()}"):
            return self.parent_obj.action(ActionConsts.START, main_param=("recovery", "phy-recovery"))
