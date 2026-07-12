from ngts.nvos_constants.constants_nvos import ActionConsts, LinkHealthConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.ib.InterfaceConfiguration.MgmtStats import MgmtStats
from ngts.nvos_tools.ib.InterfaceConfiguration.Phy import Phy
from ngts.nvos_tools.ib.InterfaceConfiguration.PhyRecovery import PhyRecovery
from ngts.tools.test_utils import allure_utils as allure


class LowPower(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/low-power')
        self.state = BaseComponent(self, path='/state')


class LinkHealth(BaseComponent):
    """``…/link/health`` show subtree + FAE clear action (HLD 0.17/0.18).

    Clear command: ``nv action clear fae interface <id> link health``
    (ranging supported by NVUE: ``acp1-72``, ``acp1-3,7``).
    """

    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/health')

    def action_clear(self) -> ResultObj:
        """Clear sticky Attention + trigger/metric/time without toggling the link."""
        with allure.step(f"Clear link-health attention for {self.get_resource_path()}"):
            return self.parent_obj.action(
                ActionConsts.CLEAR,
                main_param=LinkHealthConsts.FAE_CLEAR_MAIN_PARAM,
            )


class LinkMgmt(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/link')
        self.state = BaseComponent(self, path='/state')
        self.diagnostics = BaseComponent(self, path='/diagnostics')
        self.health = LinkHealth(self)
        self.stats = MgmtStats(self)
        self.phy = Phy(self)
        self.plan_ports = BaseComponent(self, path='/plan-ports')
        self.connection_mode = BaseComponent(self, path='/connection-mode')
        self.delayed_recovery = BaseComponent(self, path='/delayed-recovery')
        self.phy_recovery = PhyRecovery(self)
        self.link_training = BaseComponent(self, path='/link-training')
        self.plr = BaseComponent(self, path='/plr')
        self.tx_bandwidth_loss_monitor = BaseComponent(self, path='/tx-bandwidth-loss-monitor')
        self.ib_subnet = BaseComponent(self, path='/ib-subnet')
        self.low_power = LowPower(self)
        self.mloop = BaseComponent(self, path='/mloop')
