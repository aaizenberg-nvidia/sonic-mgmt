class RedfishCollection:
    MANAGERS = "redfish/v1/Managers"
    SYSTEM = "redfish/v1/Systems"
    TASK_SERVICE = "redfish/v1/TaskService/Tasks"
    UPDATE_SERVICE = "redfish/v1/UpdateService"
    UPDATE_SERVICE_MULTIPART = "redfish/v1/UpdateService/update-multipart"
    SESSION_SERVICE = "redfish/v1/SessionService"
    FIRMWARE_INVENTORY = "redfish/v1/UpdateService/FirmwareInventory"
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
    BMC_PACKAGE_SUFFIX = '.fwpkg'  # a cpld package in this format is flashed through the BMC, .vme by align_cpld
    PRODUCTION = 'prod'
    DEVELOPMENT = 'dev'
    BMC_READY_TRIES = 36
    BMC_READY_DELAY = 5


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
    COMMON = 'Common'
    SWITCH = 'Switch'
    OPN = 'opn'
    YES = 'yes'


# Component name in the versions json -> FirmwareInventory id regex; a platform with new id naming = one line here.
FW_INVENTORY_IDS = {
    Defaults.BMC_NAME: r'(MGX_)?FW_BMC_0',  # Portia AST2700 BMC: FW_BMC_0
    Defaults.BIOS_NAME: r'(MGX_)?FW_CPU_0',  # Portia BMC 88.0060.2466 has no CPU entry: BMC bug
    Defaults.EROT_NAME: r'MGX_FW_ERoT_BMC_0',
    Defaults.FPGA_NAME: r'MGX_FW_FPGA_0',
    Defaults.FPGA_ENCRYPTED_NAME: r'MGX_FW_FPGA_0',
    Defaults.SMA_NAME: r'MGX_FW_NVSwitch_SMA_\d+|FW_SMA_\d+',  # Rosalind has 2, Portia 3
    Defaults.CPLD_NAME: r'CPLD_0',  # only for a .fwpkg cpld package, a .vme one is align_cpld.py's
}
