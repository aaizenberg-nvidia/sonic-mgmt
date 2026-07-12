from enum import Enum
from typing import List
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.ib.InterfaceConfiguration.MgmtStats import MgmtStats
from ngts.nvos_tools.ib.InterfaceConfiguration.Phy import Phy
from ngts.nvos_tools.ib.InterfaceConfiguration.PhyRecovery import PhyRecovery


class LowPower(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/low-power')
        self.state = BaseComponent(self, path='/state')


class LinkMgmt(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/link')
        self.state = BaseComponent(self, path='/state')
        self.diagnostics = BaseComponent(self, path='/diagnostics')
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
