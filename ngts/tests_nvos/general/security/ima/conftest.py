"""Fixtures for the IMA test suite.

Readiness gating mirrors the TPM suite: IMA needs a TPM *and* an IMA-enabled
kernel build, so tests are skipped on platforms/images that lack either.
"""
import pytest

from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.infra.ImaTool import ImaTool
from ngts.tools.test_utils import allure_utils as allure


@pytest.fixture(scope='session', autouse=True)
def check_ima_ready_for_testing(engines, devices):
    """Skip the whole suite when the setup cannot exercise IMA."""
    if not devices.dut.supports_ima_testing:
        pytest.skip('DUT device does not support IMA testing on this platform')
    if not ImaTool(engines.dut).is_ima_kernel_supported():
        pytest.skip('Running image kernel is not built with IMA (security filesystem path missing)')


@pytest.fixture()
def ima_tool(engines) -> ImaTool:
    """Convenience kernel-side IMA helper bound to the DUT engine."""
    return ImaTool(engines.dut)


@pytest.fixture(scope='session')
def remote_engine(engines, check_ima_ready_for_testing):
    """Reachable remote host (sonic-mgmt) used as the SCP upload destination."""
    with allure.step('check sonic-mgmt is reachable'):
        sonic_mgmt_engine = engines[NvosConst.SONIC_MGMT]
        sonic_mgmt_engine.run_cmd('')
    return sonic_mgmt_engine


@pytest.fixture()
def restore_ima_default(engines):
    """
    Ensure IMA is reverted to its default (disabled) state after a config test.

    This only touches the NVUE config (apply); it does not reboot, so it is safe
    as a lightweight cleanup for the non-reboot configuration/error tests.
    """
    yield
    from ngts.nvos_tools.system.System import System
    from ngts.tests_nvos.general.security.ima.constants import ImaConsts
    with allure.step('cleanup: revert IMA state to default'):
        System().security.ima.unset(ImaConsts.STATE, apply=True).verify_result()
