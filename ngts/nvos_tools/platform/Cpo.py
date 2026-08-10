from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from ngts.nvos_constants.constants_nvos import ActionConsts, Cpov2Consts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.DefaultDict import DefaultDict
from ngts.nvos_tools.infra.ResultObj import ResultObj

if TYPE_CHECKING:
    from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine


class OeComponent(BaseComponent):
    """A single optical engine: `nv show platform cpo cpoN optical-engines oeM`."""

    def __init__(self, parent_obj=None, oe_id=None):
        super().__init__(parent=parent_obj, path=f"/{oe_id}")
        self.oe_id = oe_id


class OeCollection(BaseComponent):
    """The OE list container under a CPO: `nv show platform cpo cpoN optical-engines [oeM]`."""

    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path=f"/{Cpov2Consts.OE}")
        self.oe_id: dict[str, OeComponent] = DefaultDict(
            lambda oe_id: OeComponent(self, oe_id=oe_id)
        )


class ChannelComponent(BaseComponent):
    """A single CPO channel: `nv show platform cpo cpoN channels channel-M`."""

    def __init__(self, parent_obj=None, channel_id=None):
        super().__init__(parent=parent_obj, path=f"/{channel_id}")
        self.channel_id = channel_id


class ChannelCollection(BaseComponent):
    """The channel list container under a CPO: `nv show platform cpo cpoN channels [channel-M]`."""

    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path=f"/{Cpov2Consts.CHANNEL}")
        self.channel_id: dict[str, ChannelComponent] = DefaultDict(
            lambda channel_id: ChannelComponent(self, channel_id=channel_id)
        )


class CpoComponent(BaseComponent):
    """A single CPO / vModule (cpoN): its OE/channel drill-downs and control actions.

    The `oe`/`channel` children mirror the YANG list containers (the same keys
    that nest their data in this component's show output), so drill-down paths
    include the collection segment: `platform cpo cpo1 optical-engines oe1`.
    The CPO->ELS *relationship* is a reference (gNMI ``subcomponents`` leafrefs;
    CLI ``laser-sources``), while OE membership is read straight off the nested
    `optical-engines` container keys.
    """

    def __init__(self, parent_obj=None, cpo_id=None):
        super().__init__(parent=parent_obj, path=f"/{cpo_id}")
        self.cpo_id = cpo_id
        self.oe = OeCollection(self)
        self.channel = ChannelCollection(self)

    def action_reset(
        self, expected_output: str = "", engine: LinuxSshEngine | None = None
    ) -> ResultObj:
        """nv action reset platform cpo <cpo-id>"""
        return self.action(
            ActionConsts.RESET, expected_output=expected_output, engine=engine
        )


class Cpo(BaseComponent):
    """The `platform cpo` subtree (Gen2). nv show platform cpo [cpoN]."""

    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path="/cpo")
        self.cpo_id: dict[str, CpoComponent] = DefaultDict(
            lambda cpo_id: CpoComponent(self, cpo_id=cpo_id)
        )

    def set(self, op_param_name="", op_param_value=""):
        raise Exception("set is not implemented for /platform/cpo")

    def unset(self, op_param=""):
        raise Exception("unset is not implemented for /platform/cpo")

    @staticmethod
    def split_names(value: str | Iterable[str] | None) -> list[str]:
        """Normalize a mapping field into a list of names.

        The CLI may render such fields as a comma-separated string
        ('oe1, oe2, oe3') or as a JSON list - accept both.  Any iterable of
        names works, so a JSON object such as the `oe` container can be passed
        directly and yields its keys.
        """
        if value is None:
            return []
        if isinstance(value, str):
            return [name.strip() for name in value.split(",") if name.strip()]
        return [str(name).strip() for name in value]

    @staticmethod
    def build_topology_maps(
        cpo_show_by_name: dict[str, dict],
        port_to_cpo: dict[str, str] | None = None,
    ) -> dict:
        """Convert parsed `nv show platform cpo <cpo-id>` outputs into kwargs for
        CpoTopology.assert_consistent: `assert_consistent(**build_topology_maps(...))`.

        :param cpo_show_by_name: parsed show dict per CPO, keyed by CPO name.
        :param port_to_cpo: port -> parent CPO map (from `nv show interface <port>
            cpo`). assert_consistent requires the two port maps together, so
            cpo_to_ports (from the `ports` field) is only included - along with
            port_to_cpo itself - when this argument is given.
        """
        maps: dict = {
            "cpo_to_oes": {
                cpo: list(data.get(Cpov2Consts.OE, {}))
                for cpo, data in cpo_show_by_name.items()
            },
            "cpo_to_els": {
                cpo: Cpo.split_names(data.get(Cpov2Consts.LASER_SOURCES))
                for cpo, data in cpo_show_by_name.items()
            },
        }
        if cpo_show_by_name and all(
            Cpov2Consts.CHANNEL in data for data in cpo_show_by_name.values()
        ):
            maps["cpo_to_channels"] = {
                cpo: list(data[Cpov2Consts.CHANNEL])
                for cpo, data in cpo_show_by_name.items()
            }
        if port_to_cpo is not None:
            maps["cpo_to_ports"] = {
                cpo: Cpo.split_names(data.get(Cpov2Consts.PORTS))
                for cpo, data in cpo_show_by_name.items()
            }
            maps["port_to_cpo"] = port_to_cpo
        return maps
