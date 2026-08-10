from __future__ import annotations

from typing import TYPE_CHECKING, Dict

from ngts.nvos_constants.constants_nvos import ActionConsts, Cpov2Consts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.DefaultDict import DefaultDict
from ngts.nvos_tools.infra.ResultObj import ResultObj

if TYPE_CHECKING:
    from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine


class LaserComponent(BaseComponent):
    """A single laser: `nv show platform laser-source elsN laser laser-M`."""

    def __init__(self, parent_obj=None, laser_id=None):
        super().__init__(parent=parent_obj, path=f"/{laser_id}")
        self.laser_id = laser_id


class LaserCollection(BaseComponent):
    """The `laser` list container under an ELS: `nv show platform laser-source elsN laser [laser-M]`."""

    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path=f"/{Cpov2Consts.LASER}")
        self.laser_id: Dict[str, LaserComponent] = DefaultDict(
            lambda laser_id: LaserComponent(self, laser_id=laser_id)
        )


class LaserSourceComponent(BaseComponent):
    """A single laser-source / ELS (elsN): its laser drill-down and control actions.

    The `laser` child mirrors the YANG list container (the same key that nests
    per-laser data in this component's show output). Laser-scoped ACTIONS still
    pass the laser as a command parameter (see action_reset / action_activate),
    not as a path segment.
    """

    def __init__(self, parent_obj=None, els_id=None):
        super().__init__(parent=parent_obj, path=f"/{els_id}")
        self.els_id = els_id
        self.laser = LaserCollection(self)

    def action_reset(
        self,
        laser_id: str | None = None,
        expected_output: str = "",
        engine: LinuxSshEngine | None = None,
    ) -> ResultObj:
        """nv action reset platform laser-source <els-id> [laser <laser-id>]

        When laser_id is given (e.g. 'laser-2') the reset is scoped to that laser.
        """
        # `is not None` (not truthiness): a buggy empty-string laser_id must not
        # silently widen the reset to all 16 lasers of the ELS
        main_param = (Cpov2Consts.LASER, laser_id) if laser_id is not None else None
        return self.action(
            ActionConsts.RESET,
            main_param=main_param,
            expected_output=expected_output,
            engine=engine,
        )

    def action_activate(
        self,
        laser_id: str | None = None,
        step: str | None = None,
        expected_output: str = "",
        engine: LinuxSshEngine | None = None,
    ) -> ResultObj:
        """nv action activate fae platform laser-source <els-id> [laser <laser-id>] [step <step-name>]

        FAE-only action - call it on the instance mounted under fae
        (fae.platform.laser_source.els_id[...]). Runs the ELS initialization
        steps; by default steps 1-3 (fiber-check, laser-tuning, laser-up) for
        all lasers. Unlike reset, both scoping params are named in NVUE
        (e.g. 'laser laser-4 step laser-up').

        :param laser_id: e.g. 'laser-4'; when omitted, all lasers are targeted.
        :param step: one of Cpov2Consts.ALL_ACTIVATE_STEPS; when omitted, the
            default steps (Cpov2Consts.DEFAULT_ACTIVATE_STEPS) are executed.
        """
        # the same component class is mounted under /platform and /fae/platform,
        # but `nv action activate ... laser-source` only exists on the fae mount
        if not self.get_resource_path().startswith("/fae"):
            raise Exception(
                "activate is FAE-only - call it via fae.platform.laser_source, "
                f"not {self.get_resource_path()}"
            )
        params: dict[str, str] = {}
        if laser_id is not None:
            params[Cpov2Consts.LASER] = laser_id
        if step is not None:
            params[Cpov2Consts.STEP] = step
        return self.action(
            ActionConsts.ACTIVATE,
            additional_params=params,
            expected_output=expected_output,
            engine=engine,
        )


class LaserSource(BaseComponent):
    """The `platform laser-source` subtree (Gen2). nv show platform laser-source [elsN]."""

    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path="/laser-source")
        self.els_id: Dict[str, LaserSourceComponent] = DefaultDict(
            lambda els_id: LaserSourceComponent(self, els_id=els_id)
        )

    def show_detailed(self, dut_engine: LinuxSshEngine | None = None) -> str:
        return self.show(op_param="--view=detail", dut_engine=dut_engine)

    def get_list_of_laser_sources(
        self, dut_engine: LinuxSshEngine | None = None
    ) -> list[str]:
        """Names of all laser sources reported by `nv show platform laser-source`."""
        return list(self.parse_show(dut_engine=dut_engine).keys())

    def set(self, op_param_name="", op_param_value=""):
        raise Exception("set is not implemented for /platform/laser-source")

    def unset(self, op_param=""):
        raise Exception("unset is not implemented for /platform/laser-source")
