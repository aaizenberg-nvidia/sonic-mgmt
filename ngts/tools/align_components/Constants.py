class RedfishCollection:
    MANAGERS = "redfish/v1/Managers"
    SYSTEM = "redfish/v1/Systems"
    TASK_SERVICE = "redfish/v1/TaskService/Tasks"
    UPDATE_SERVICE = "redfish/v1/UpdateService"
    UPDATE_SERVICE_MULTIPART = "redfish/v1/UpdateService/update-multipart"
    SESSION_SERVICE = "redfish/v1/SessionService"
    FIRMWARE_INVENTORY = "redfish/v1/UpdateService/FirmwareInventory"
    CPU_REDFISH_NAME = "MGX_FW_CPU_0"
    ACCOUNT_SERVICE = "redfish/v1/AccountService"
    SYSTEM_ACTIONS = f"{SYSTEM}/System_0/Actions"
    RESET = f"{SYSTEM_ACTIONS}/ComputerSystem.Reset"
    BMC_MANAGER = f"{MANAGERS}/BMC_0"
    ROOT_ACCOUNT = f"{ACCOUNT_SERVICE}/Accounts/root"


class Defaults:
    DEFAULT_SWITCH_USERNAME = 'admin'
    DEFAULT_SWITCH_PASSWORD = 'admin'
    DEFAULT_BMC_USER = 'root'
    DEFAULT_BMC_PASSWORD = 'ABYX12#14artb'
    BMC_NVOS_USER = 'yormnAnb'
    GET_BMC_PASSWORD_FROM_TPM_CMD = ('sudo python3 -c "from sonic_platform.bmc import BMC; '
                                     'print(BMC(\'10.0.1.1\').get_login_password())"')
    SSH_CMD_TIMEOUT = 60
    DEFAULT_BRANCH_NAME = 'develop'
    CPLD_NAME = 'cpld'
    BMC_NAME = 'bmc'
    BIOS_NAME = 'bios'
    SMA_NAME = 'sma'
    FPGA_NAME = 'fpga'
    FPGA_ENCRYPTED_NAME = 'fpga_encrypted'
    EROT_NAME = 'erot'
    PLDM_NAME = 'pldm'
    PRODUCTION = 'prod'
    DEVELOPMENT = 'dev'


class NogaConstants:
    """
    NogaConstants class
    """
    RELATIONS = "relations"
    HAS_A = "has a"
    TYPE_TITLE = "TYPE_TITLE"
    NAME = "NAME"
    ATTRIBUTES = 'attributes'
    BMC_IP = 'bmc_ip'
    BMC_IPV6 = 'bmc_ipv6'
    MGMT_IPV6 = 'ipv6'
    HARDWARE_STATE_DETAILS = 'Hardware_state_details'
    IPV6_MARKER = 'IPv6 setup'
    REMOTE_REBOOT = 'remote_reboot'
    SPECIFIC = 'Specific'
    BIOS_VERSION = 'bios_version'
    HARDWARE_COMPONENTS = 'Hardware Components'
    COMMON = 'Common'
    SWITCH = 'Switch'
    OPN = 'opn'
    YES = 'yes'
