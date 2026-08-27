import copy
import os

ADMIN = 'yormnAnb'
ROOT = 'root'
OPENBMC = '0penBmc'
ADMIN_USER_DEFAULT_PASSWORD = 'ABYX12#14artb51'
BMC_USER_DEFAULT_PASSWORD = OPENBMC
BMC_USER_BACKUP_PASSWORD = 'ABYX12#14artb'

# Env / Vault keys for the Cumulus/SPC6 BMC root password used by bmc-cli and Redfish,
# in resolution order. The BMC root account on these platforms is provisioned with the
# same password as the Cumulus root user, and CUMULUS_ROOT_PASSWORD is what the Vault
# secret export provides, so CI/lab runs need no extra setup. CL_BMC_ROOT_PASSWORD stays
# first as an explicit override for setups where the two differ.
CL_BMC_ROOT_PASSWORD_ENV = "CL_BMC_ROOT_PASSWORD"
CL_BMC_ROOT_PASSWORD_ENVS = (CL_BMC_ROOT_PASSWORD_ENV, "CUMULUS_ROOT_PASSWORD")

# Env / Vault key for the BMC "backup" / alternate password used by BmcSshEngine
# login candidates and change-password rotation. Prefer Vault; falls back to
# BMC_USER_BACKUP_PASSWORD so existing IB labs keep working until Vault exports it.
BMC_BACKUP_PASSWORD_ENV = "BMC_BACKUP_PASSWORD"


def get_cl_bmc_root_password() -> str:
    """Return the CL/SPC6 BMC root password from the environment.

    Checks ``CL_BMC_ROOT_PASSWORD_ENVS`` in order and returns the first non-empty value.

    Raises:
        RuntimeError: if none of those variables is set.
    """
    for env_name in CL_BMC_ROOT_PASSWORD_ENVS:
        password = os.environ.get(env_name, "").strip()
        if password:
            return password
    raise RuntimeError(
        "None of {} is set. The pytest session loads Vault secrets into os.environ, "
        "which provides CUMULUS_ROOT_PASSWORD; set one of these variables in the "
        "environment before running Cumulus BMC tests.".format(
            ", ".join(CL_BMC_ROOT_PASSWORD_ENVS))
    )


def get_bmc_backup_password() -> str:
    """Return the BMC backup/alternate password (env first, then constants fallback).

    Prefer ``BMC_BACKUP_PASSWORD`` from Vault/env so the secret is not required to
    live only in source. Falls back to ``BMC_USER_BACKUP_PASSWORD`` to preserve
    existing IB behavior when the env var is not yet wired.
    """
    password = os.environ.get(BMC_BACKUP_PASSWORD_ENV, "").strip()
    if password:
        return password
    return BMC_USER_BACKUP_PASSWORD


FACTORY_RESET_FLAG = 'factory_reset'

CURL_AUTHORIZATION_ERR_MSGS = ['Authorization Error', 'authorization error', 'Invalid username or password']


class BmcCliCmd:
    factory_reset = 'fw_setenv openbmconce factory-reset'
    check_factory_reset_flag = 'fw_printenv'
    enable_mctp_pcie_ctrl_service = 'systemctl enable mctp-pcie-ctrl ; systemctl start mctp-pcie-ctrl'


class BmcUserInfo:
    def __init__(self, username: str, default_password: str, another_password: str):
        self.username = username
        self.default_password = default_password
        self.another_password = another_password

    def copy(self, deep=False):
        if deep:
            return copy.deepcopy(self)
        else:
            return copy.copy(self)


class BmcUsers:
    root = BmcUserInfo(ROOT, BMC_USER_DEFAULT_PASSWORD, BMC_USER_BACKUP_PASSWORD)
    admin = BmcUserInfo(ADMIN, ADMIN_USER_DEFAULT_PASSWORD, BMC_USER_BACKUP_PASSWORD)
