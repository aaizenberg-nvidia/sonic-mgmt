import pytest
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

from ngts.ngts_types import DevicesT
from ngts.nvos_tools.infra.TpmTool import TpmTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.constants import SecurityConsts
from ngts.tools.test_utils import allure_utils as allure


def _is_valid_sed_password(password: str) -> bool:
    """SED password must be 8-127 chars and contain no whitespace."""
    return (SecurityConsts.SED_MIN_PASSWORD_LENGTH <= len(password) <= SecurityConsts.SED_MAX_PASSWORD_LENGTH and
            not any(char.isspace() for char in password))


@pytest.fixture(scope='session')
def tpm_tool(engines):
    """TpmTool instance with cached TPM password for reuse across test functions."""
    switch: LinuxSshEngine = engines.dut
    return TpmTool(switch)


@pytest.fixture(scope='function')
def sed_default_password(tpm_tool, devices: DevicesT):
    with allure.step("Get SED password from tpm"):
        sed_password: str = tpm_tool.get_sed_password_primary_bank(device=devices.dut).strip()

    password_valid = _is_valid_sed_password(sed_password)
    if not password_valid:
        with allure.step(
                f"TPM SED password invalid (len={len(sed_password)}, "
                f"has_whitespace={any(char.isspace() for char in sed_password)}); "
                f"expected {SecurityConsts.SED_MIN_PASSWORD_LENGTH}-"
                f"{SecurityConsts.SED_MAX_PASSWORD_LENGTH} chars with no whitespace"
        ):
            allure.attach("raw_tpm_sed_password", repr(sed_password))
    try:
        if not password_valid:
            pytest.fail(
                f"TPM SED password is invalid (len={len(sed_password)}, "
                f"has_whitespace={any(char.isspace() for char in sed_password)}); "
                f"raw={sed_password!r}"
            )
        yield sed_password
    finally:
        restore_password = sed_password
        if not password_valid:
            with allure.step(
                    "Falling back to SecurityConsts.SED_DEFAULT_PASSWORD for SED restore"
            ):
                allure.attach("fallback_reason",
                              f"invalid TPM password len={len(sed_password)}")
                restore_password = SecurityConsts.SED_DEFAULT_PASSWORD
        with allure.step(
                f"Restore SED password "
                f"({'platform default' if restore_password == SecurityConsts.SED_DEFAULT_PASSWORD else 'TPM value'}, "
                f"len={len(restore_password)})"
        ):
            system = System()
            system.security.action_change_sed_password(restore_password).verify_result()
