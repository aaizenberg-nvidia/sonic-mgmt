from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.platform.Cpo import ChannelCollection, OeCollection


class InterfaceCpo(BaseComponent):
    """The `interface <id> cpo` subtree. nv show interface swXpY cpo.

    Shows CPO telemetry relevant to a given interface: parent CPO identity, the
    relevant OE and a subset of channels (a slice of `nv show platform cpo cpoN`).
    The `optical-engines`/`channels` drill-downs reuse the platform CPO
    collections - only the mount point differs.
    """

    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path="/cpo")
        self.oe = OeCollection(self)
        self.channel = ChannelCollection(self)

    def set(self, op_param_name="", op_param_value=""):
        raise Exception("set is not implemented for /interface/{id}/cpo")

    def unset(self, op_param=""):
        raise Exception("unset is not implemented for /interface/{id}/cpo")
