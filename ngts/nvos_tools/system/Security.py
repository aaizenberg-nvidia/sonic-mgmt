from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.system.CaCertificate import CaCertificate
from ngts.nvos_tools.system.Certificate import Certificate
from ngts.nvos_tools.system.Crl import Crl
from ngts.nvos_tools.system.Ima import Ima
from ngts.nvos_tools.system.PasswordHardening import PasswordHardening
from ngts.nvos_tools.system.PlatformCertificate import PlatformCertificate
from ngts.nvos_tools.system.Spdm import Spdm
from ngts.nvos_tools.system.Tpm import Tpm
from ngts.tools.test_utils import allure_utils as allure


class Security(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/security')
        self.password_hardening = PasswordHardening(self)
        self.certificate = Certificate(self)
        self.crl = Crl(self)
        self.ca_certificate = CaCertificate(self)
        self.tpm = Tpm(self)
        self.spdm = Spdm(self)
        self.platform_certificate = PlatformCertificate(self)
        self.gpumad = GPUMad(self)
        self.ima = Ima(self)

    def action_change_sed_password(self, new_password: str, dut_engine=None) -> ResultObj:
        with allure.step(f'Execute action change sed pass for {self.get_resource_path()}'):
            engine = dut_engine if dut_engine else TestToolkit.get_engine()
            params = f"sed-password {new_password}"
            if TestToolkit.tested_api == ApiType.OPENAPI:
                params = {"sed-password": new_password}
            return SendCommandTool.execute_command(self._cli_wrapper.action_change, engine,
                                                   self.get_resource_path(), params)


class GPUMad(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/gpu-mad-firewall')
