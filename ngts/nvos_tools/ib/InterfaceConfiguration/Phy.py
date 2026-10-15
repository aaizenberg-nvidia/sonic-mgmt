from ngts.nvos_tools.infra.BaseComponent import BaseComponent


class Phy(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/phy')
        self.detail = BaseComponent(self, path='/detail')
        self.health = BaseComponent(self, path='/health')
        self.ber_monitor = BaseComponent(self, path='/ber-monitor')
