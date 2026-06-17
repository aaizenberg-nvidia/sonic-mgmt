import logging

from ngts.cli_wrappers.nvue.nvue_platform_clis import NvuePlatformCli
from ngts.cli_wrappers.openapi.openapi_platform_clis import OpenApiPlatformCli
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.platform.Asic import Asic
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.platform.CableCartridge import CableCartridge
from ngts.nvos_tools.platform.Environment import Environment
from ngts.nvos_tools.platform.Inventory import Inventory
from ngts.nvos_tools.platform.PowerCapping import PowerCapping
from ngts.nvos_tools.system.Firmware import Firmware
from ngts.nvos_tools.system.Transceiver import Transceiver
from ngts.nvos_tools.platform.PSRedundancy import PSRedundancy
from ngts.nvos_tools.platform.BmcFactoryDefault import BmcFactoryDefault
from ngts.nvos_tools.platform.Bmc_password import Bmc_password

logger = logging.getLogger()


class Platform(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj,
                         api={ApiType.NVUE: NvuePlatformCli, ApiType.OPENAPI: OpenApiPlatformCli},
                         path='/platform')
        self.firmware = Firmware(self)
        self.environment = Environment(self)
        self.asic = Asic(self)
        self.inventory = Inventory(self)
        self.ps_redundancy = PSRedundancy(self)
        self.transceiver = Transceiver(self)
        self.chassis_location = BaseComponent(self, path='/chassis-location')
        self.bmc_password = Bmc_password(self)
        self.bmc_factory_default = BmcFactoryDefault(self)
        self.cable_cartridge = CableCartridge(self)
        self.power_capping = PowerCapping(self)
        self.boot_policy = BaseComponent(self, path='/boot-policy')

    def set(self, op_param_name="", op_param_value=""):
        raise Exception("set is not implemented for /platform")

    def unset(self, op_param=""):
        raise Exception("unset is not implemented for /platform")
