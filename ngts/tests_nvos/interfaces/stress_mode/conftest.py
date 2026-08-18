"""
Fixtures for Stress Mode Testing
"""
import logging
import pytest

from ngts.nvos_constants.constants_nvos import HealthConsts
from ngts.tools.test_utils import allure_utils as allure

from .constants import StressModeConsts
from .helpers import set_stress_mode_state, validate_health_status

logger = logging.getLogger()


@pytest.fixture(scope="function", autouse=True)
def cleanup_stress_mode(engines, nv_command):
    """
    Fixture that ensures stress mode is disabled before and after test execution.

    This fixture runs automatically for all tests in this folder.
    Setup phase:
      - Disables stress mode to ensure clean state (prevents "already enabled" scenario)
      - Rotates logs to start with fresh syslog
    Teardown phase:
      - Disables stress mode even if the test fails or raises an exception

    This guarantees state transitions (disabled → enabled) trigger expected syslog messages.
    """
    # Setup: Ensure stress mode is disabled before test starts
    with allure.step("Setup: Ensure stress mode is disabled"):
        set_stress_mode_state(engines.dut, StressModeConsts.STATE_DISABLED, tolerate_missing_disabled=True)
        validate_health_status(HealthConsts.OK)

    nv_command.system.log.rotate_logs()
    yield

    # Teardown: Ensure stress mode is disabled after test completes
    with allure.step("Cleanup: Disable stress mode"):
        set_stress_mode_state(engines.dut, StressModeConsts.STATE_DISABLED, tolerate_missing_disabled=True)
        validate_health_status(HealthConsts.OK)
