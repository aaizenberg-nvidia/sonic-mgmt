from typing import Dict

from ngts.nvos_tools.infra.DefaultDict import DefaultDict
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.cli_wrappers.nvue.nvue_platform_clis import NvuePlatformCli
from ngts.cli_wrappers.openapi.openapi_platform_clis import OpenApiPlatformCli


class Asic(BaseComponent):
    power = None
    temperature = None
    asic_id = ''

    def __init__(self, parent_obj):
        BaseComponent.__init__(self, parent=parent_obj, path='/asic')
        self.power = BaseComponent(self, self.api_obj, '/power')
        self.temperature = BaseComponent(self, self.api_obj, '/temperature')
        self.api_obj = {ApiType.NVUE: NvuePlatformCli, ApiType.OPENAPI: OpenApiPlatformCli}
        self.asic_id: Dict[str, AsicComponent] = DefaultDict(lambda asic_name: AsicComponent(self, asic_name=asic_name))
        self.parent_obj = parent_obj

    def set(self, op_param_name="", op_param_value=""):
        raise Exception("set is not implemented for /asic/{asic_id}")

    def unset(self, op_param=""):
        raise Exception("unset is not implemented for /asic/{asic_id}")


class AsicComponent(BaseComponent):
    def __init__(self, parent_obj=None, asic_name=None):
        super().__init__(parent=parent_obj, path=f"/{asic_name}")
        self.power = BaseComponent(self, self.api_obj, '/power')
        self.temperature = BaseComponent(self, self.api_obj, '/temperature')
        self.frequency = BaseComponent(self, self.api_obj, '/frequency')
