from ngts.nvos_constants.constants_nvos import ActionConsts, CertificateFiles, OtlpConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.ResultObj import ResultObj


class Otlp(BaseComponent):
    def __init__(self, parent=None):
        super().__init__(parent=parent, path="/otlp")
        self.endpoint = OtlpEndpoint(self)
        self.certificate = OtlpCertificate(self, f"/{CertificateFiles.CERTIFICATE}")
        self.ca_certificate = OtlpCertificate(self, f"/{CertificateFiles.CA_CERTIFICATE}")
        self.crl = OtlpCrl(self)
        self.encryption = OtlpEncryption(self)


class OtlpEndpoint(BaseComponent):
    def __init__(self, parent):
        super().__init__(parent=parent, path=f"/{OtlpConsts.ENDPOINT}")

    def action_update(self, endpoint: str = "", dut_engine=None) -> ResultObj:
        return self.action(ActionConsts.UPDATE, main_param=(OtlpConsts.ENDPOINT, endpoint), engine=dut_engine)

    def action_restore(self, dut_engine=None) -> ResultObj:
        return self.action(ActionConsts.RESTORE, engine=dut_engine)


class OtlpCertificate(BaseComponent):
    def __init__(self, parent, path):
        super().__init__(parent=parent, path=path)
        self.is_ca = CertificateFiles.CA_CERTIFICATE in path

    def action_update(self, cert_id: str = "", dut_engine=None) -> ResultObj:
        param_name = OtlpConsts.CA_CERT_ID if self.is_ca else OtlpConsts.CERT_ID
        return self.action(ActionConsts.UPDATE, main_param=(param_name, cert_id), engine=dut_engine)

    def action_restore(self, dut_engine=None) -> ResultObj:
        return self.action(ActionConsts.RESTORE, engine=dut_engine)


class OtlpCrl(BaseComponent):
    def __init__(self, parent):
        super().__init__(parent=parent, path=f"/{OtlpConsts.CRL}")

    def action_update(self, crl_id: str = "", dut_engine=None) -> ResultObj:
        return self.action(ActionConsts.UPDATE, main_param=(OtlpConsts.CRL_ID, crl_id), engine=dut_engine)

    def action_restore(self, dut_engine=None) -> ResultObj:
        return self.action(ActionConsts.RESTORE, engine=dut_engine)


class OtlpEncryption(BaseComponent):
    def __init__(self, parent):
        super().__init__(parent=parent, path=f"/{OtlpConsts.ENCRYPTION}")

    def action_update(self, mode: str = "", dut_engine=None) -> ResultObj:
        return self.action(ActionConsts.UPDATE, main_param=(OtlpConsts.MODE, mode), engine=dut_engine)

    def action_restore(self, dut_engine=None) -> ResultObj:
        return self.action(ActionConsts.RESTORE, engine=dut_engine)
