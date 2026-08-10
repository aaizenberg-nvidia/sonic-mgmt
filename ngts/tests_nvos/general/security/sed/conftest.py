import pytest
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

from ngts.ngts_types import DevicesT
from ngts.nvos_tools.infra.TpmTool import TpmTool
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure


@pytest.fixture(scope='session')
def tpm_tool(engines):
    """TpmTool instance with cached TPM password for reuse across test functions."""
    switch: LinuxSshEngine = engines.dut
    return TpmTool(switch)


@pytest.fixture(scope='function')
def sed_default_password(tpm_tool, devices: DevicesT):
    with allure.step("Get SED password from tpm"):
        sed_password: str = tpm_tool.get_sed_password_primary_bank(device=devices.dut).strip()
        assert sed_password, "Empty SED password read from TPM primary bank - aborting to avoid a broken restore in teardown"
    try:
        yield sed_password
    finally:
        with allure.step(f"Set default SED password {sed_password}"):
            system = System()
            system.security.action_change_sed_password(sed_password).verify_result()
