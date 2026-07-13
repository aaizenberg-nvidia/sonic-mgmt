from ngts.nvos_constants.constants_nvos import ClusterApps

CLUSTER_STATE_TOGGLE_WAIT_TIME = 20
CLUSTER_APP_MNGR_STATE_UPDATE_WAIT_TIME = 5

NA = "N/A"
STATE = "state"

ENABLED = "enabled"
DISABLED = "disabled"

PORT = "port"
CERTIFICATE = "certificate"
CA_CERTIFICATE = "ca-certificate"
ENCRYPTION = "encryption"
ALTERNATE_CERTIFICATE = "alternate-certificate"
CRL = "crl"
ENDPOINT = "endpoint"
PROTOCOL = "protocol"
REMOTE_URL = "remote-url"

DEFAULT_NMX_C_MGMT_PORT = 9370
DEFAULT_NMX_T_MGMT_PORT = 9351

NV_BRIDGE_CLIENT_PORT = 9381
NV_BRIDGE_SERVER_PORT = 50052

USR_CFG_JSON_PATH = "/etc/cluster_infra/conf/user_config.json"
SYSTEM_INTERNAL_JSON_PATH = "/etc/internal_infra/conf/internal_config.json"
CLUSTER_INTERNAL_JSON_PATH = USR_CFG_JSON_PATH

FILE_NOT_EXIST_ERR = "No such file or directory"
ITEM_NOT_EXIST_ERR = "The requested item does not exist"


class SystemInternalJsonFields:
    CERTIFICATE = "certificate"
    CERTIFICATE_PRIVATE_KEY = "certificate-private-key"
    CA_CERTIFICATE = "ca-certificate"
    ENCRYPTION = "encryption-mode"
    ALTERNATE_CERTIFICATE = "alternate-certificate"
    ALTERNATE_CERTIFICATE_PRIVATE_KEY = "alternate-certificate-private-key"
    CERTIFICATE_SPIFFE = "certificate-spiffe"
    ALTERNATE_CERTIFICATE_SPIFFE = "alternate-certificate-spiffe"
    ALL_FIELDS = [
        CERTIFICATE,
        CA_CERTIFICATE,
        ENCRYPTION,
        ALTERNATE_CERTIFICATE,
        ALTERNATE_CERTIFICATE_PRIVATE_KEY,
        CERTIFICATE_SPIFFE,
        ALTERNATE_CERTIFICATE_SPIFFE,
    ]


class ClusterInternalJsonFields:
    CERTIFICATE = "{}-internal-certificate"
    CERTIFICATE_PRIVATE_KEY = "{}-internal-private-key"
    CA_CERTIFICATE = "{}-internal-ca-certificate"
    ENCRYPTION = "{}-internal-encryption-mode"
    ALTERNATE_CERTIFICATE = "{}-internal-alternate-certificate"
    ALTERNATE_CERTIFICATE_PRIVATE_KEY = "{}-internal-alternate-private-key"
    CERTIFICATE_SPIFFE = "{}-internal-spiffe"
    ALTERNATE_CERTIFICATE_SPIFFE = "{}-internal-alternate-spiffe"


class FieldsInShowOf:
    MANAGER = [STATE, CERTIFICATE, CA_CERTIFICATE, ENCRYPTION]
    INTERNAL = [CERTIFICATE, CA_CERTIFICATE, ENCRYPTION, ALTERNATE_CERTIFICATE, CRL]
    CERTIFICATE = [CERTIFICATE]
    CA_CERTIFICATE = [CA_CERTIFICATE]
    ENCRYPTION = [ENCRYPTION]
    ALTERNATE_CERTIFICATE = [ALTERNATE_CERTIFICATE]
    CRL = [CRL]


class EncryptionMode:
    DISABLED = "disabled"
    TLS = "tls"
    MTLS = "mtls"
    ALL_MODES = [DISABLED, TLS, MTLS]


class Defaults:
    STATE = DISABLED
    CERT = ""
    CACERT = ""
    ALTERNATE_CERTIFICATE = ""
    CRL = ""
    ENDPOINT = ""
    ENCRYPTION = EncryptionMode.DISABLED
    PROTOCOL = ""
    REMOTE_URL = ""


class ClusterAppUserCfgJsonFields:
    def __init__(self, app_name: str):
        self.certificate = f"{app_name}-manager-certificate"
        self.private_key = f"{app_name}-manager-private-key"
        self.ca_certificate = f"{app_name}-manager-ca-certificate"
        self.encryption = f"{app_name}-manager-encryption"
        self.state = f"{app_name}-manager-port"
        self.all_fields = [self.certificate, self.private_key, self.ca_certificate, self.encryption, self.state]


class ClusterAppConsts:
    def __init__(self, app_name: str, external_port, state: str = Defaults.STATE):
        self.app_name: str = app_name
        self.user_config_json_path: str = USR_CFG_JSON_PATH
        self.user_config_json_fields: ClusterAppUserCfgJsonFields = ClusterAppUserCfgJsonFields(app_name)
        self.fields_that_must_exist_in_user_config_json: dict = {self.user_config_json_fields.state: state}
        self.cert_private_key_path: str = f"/etc/{app_name}/cert" + "/{}.key"
        self.cert_public_key_path: str = f"/etc/{app_name}/cert" + "/{}.crt"
        self.cacert_path: str = f"/etc/{app_name}/ca_cert" + "/{}.crt"
        self.external_manager_port = external_port


NMX_C_CONSTS = ClusterAppConsts(
    app_name=ClusterApps.NMX_CONTROLLER,
    external_port=DEFAULT_NMX_C_MGMT_PORT,
)

NMX_T_CONSTS = ClusterAppConsts(app_name=ClusterApps.NMX_TELEMETRY, external_port=DEFAULT_NMX_T_MGMT_PORT, state=ENABLED)

APP_CONSTS: dict[str, ClusterAppConsts] = {ClusterApps.NMX_CONTROLLER: NMX_C_CONSTS, ClusterApps.NMX_TELEMETRY: NMX_T_CONSTS}
