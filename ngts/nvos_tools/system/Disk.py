from ngts.nvos_constants.constants_nvos import ActionConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.ResultObj import ResultObj


class Disk(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/disk')

    def action_erase(self, engine=None, device=None, force=None, timeout=300) -> ResultObj:
        flags = 'force' if force else ''
        return self.action(ActionConsts.ERASE, flags=flags, engine=engine, device=device, timeout=timeout)
