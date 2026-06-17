from ngts.nvos_constants.constants_nvos import ActionConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent


class BmcFactoryDefaultMode:
    CONFIG_ONLY = 'config-only'
    CONFIG_AND_LOGS = 'config-and-logs'
    SECURE_ERASE = 'secure-erase'


class BmcFactoryDefaultErrors:
    CONNECT_ERROR = "Can't connect to BMC"


class BmcFactoryDefault(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj,
                         path='/bmc-factory-default')

    def action_reset(self, mode=None, force=True, timeout=30):
        """ nv action reset platform bmc-factory-default [mode <mode>] [force] """
        additional_params = {'mode': mode} if mode else {}
        flags = 'force' if force else ''
        return self.action(
            ActionConsts.RESET,
            additional_params=additional_params,
            flags=flags,
            reboot_params=False,
            timeout=timeout,
        )
