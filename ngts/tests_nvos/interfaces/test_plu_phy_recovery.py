"""
PLU PHY Recovery Tests

This module contains tests for PLU PHY recovery functionality on all port types
(sw and acl). The tests verify PHY recovery configuration, counters, and
go-once action behavior.

Based on Link_Phy_Recovery_NVOS_HLD document.

Test Cases:
    1. test_plu_phy_recovery_go_once_bad_flow - Go-once bad flow (invalid port types)
    2. test_plu_phy_recovery_go_once - Go-once good flow on a random port
    3. test_plu_phy_recovery_attributes - Verify default attributes
    4. test_plu_phy_recovery_bad_flow - Negative test for invalid params
    5. test_plu_set_fae_phy_recovery - Set mode/timeout on ALL ports

Configuration:
    - Uses `logic-relock-mode` and `logic-relock-timeout`
    - Timeout range is 0-126
    - Recovery counters viewed via `nv show interface <port> link phy detail`
"""
import logging
import random

import pytest

from ngts.nvos_constants.constants_nvos import ActionConsts, LogsSources
from ngts.nvos_tools.Devices.IbDevice import RosalindSurrogateSwitch
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure

from ngts.tests_nvos.interfaces.plu_phy_recovery.constants import (
    PhyRecoveryConfig,
    PhyRecoveryCounters,
    PhyRecoveryTestConsts,
    ErrorMessages,
    GoOnceConsts,
)

from ngts.tests_nvos.interfaces.plu_phy_recovery.helpers import (
    trigger_recovery_go_once,
    validate_default_config,
    get_all_ports_ranges,
    get_phy_recovery_counters,
    wait_for_recovery_counters_update,
    apply_mode,
    apply_mode_if_needed,
    apply_mode_to_all,
    apply_timeout,
    apply_timeout_to_all,
    verify_config,
    unset_config_all,
    cleanup_phy_recovery_config,
    select_random_xdr_up_port,
)

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def skip_if_logic_relock_unsupported(devices):
    """
    logic-relock-mode is not in the phy-recovery NVUE schema on Rosalind/Portia
    (croc/mamba/taipan/juliet only). Skip the whole module there even if MARS
    .db ignore is missing.
    """
    if isinstance(devices.dut, RosalindSurrogateSwitch):
        pytest.skip(
            "logic-relock-mode / PLU phy-recovery not supported on Rosalind/Portia "
            f"(dut={type(devices.dut).__name__})"
        )


# =============================================================================
# Test: PHY Recovery Go-Once Action (Bad Flow - Parametrized)
# =============================================================================

@pytest.mark.plu_phy_recovery
@pytest.mark.parametrize("invalid_port", GoOnceConsts.INVALID_PORT_TYPES)
def test_plu_phy_recovery_go_once_bad_flow(invalid_port, random_api):
    """
    Negative test: Verify go-once phy-recovery action fails on invalid port types.

    Command: nv action start fae interface <port> link phy-recovery

    This test is parametrized to run independently for each invalid port type,
    ensuring test isolation - if one port type fails unexpectedly, others still run.

    Args:
        invalid_port: Invalid port type to test (e.g., "eth0", "lo")
        random_api: API type fixture (NVUE/OpenAPI)
    """
    TestToolkit.tested_api = random_api

    with allure.step(f"Test action start phy-recovery on invalid port: {invalid_port}"):
        fae_port = Fae(port_name=invalid_port)
        result = fae_port.port.interface.link.phy_recovery.action(
            ActionConsts.START,
            expected_output=""
        )
        result.verify_result(False)
        logger.info(f"Bad flow verified: action correctly failed on {invalid_port}")


# =============================================================================
# Test: PHY Recovery Go-Once Action (Good Flow)
# =============================================================================

@pytest.mark.plu_phy_recovery
def test_plu_phy_recovery_go_once(engines, devices, random_api):
    """
    Verify the go-once phy-recovery action (good flow).

    Command: nv action start fae interface <port> link phy-recovery

    Steps:
        1. Select a random UP port with connection-mode xdr.
        2. Set logic-relock-mode to enabled if not already enabled.
        3. Verify mode is enabled on the port.
        4. Record initial recovery counters (total-successful-recovery-events,
           successful-recovery-events).
        5. Run go-once action and verify it succeeds.
        6. Wait for at least one recovery counter to increase (polling).
        7. Check syslog for expected message.
    """
    TestToolkit.tested_api = random_api

    system = System()

    with allure.step(f"Select a random UP port with ib-speed {IbInterfaceConsts.XDR}"):
        port_result = RandomizationTool.select_random_port(
            requested_ports_state=NvosConsts.LINK_STATE_UP,
            requested_ib_speed=IbInterfaceConsts.XDR
        )
        if not port_result.result:
            port_result.ignore_result()
            pytest.skip(
                f"Skipping test - no UP port reports ib-speed {IbInterfaceConsts.XDR} "
                f"(logic-relock is XDR-scoped): {port_result.info}"
            )
        selected_port = port_result.get_returned_value()
        logger.info(f"Selected XDR port for go-once test: {selected_port.name}")

    fae_port = Fae(port_name=selected_port.name)

    try:
        with allure.step(f"Set {PhyRecoveryConfig.MODE} to {PhyRecoveryConfig.ENABLED} if not already enabled"):
            mode_was_changed = apply_mode_if_needed(fae_port, PhyRecoveryConfig.ENABLED)
            logger.info(f"Mode was changed: {mode_was_changed}")

        with allure.step(f"Verify mode is {PhyRecoveryConfig.ENABLED} on port {selected_port.name}"):
            verify_config(fae_port, PhyRecoveryConfig.ENABLED)

        with allure.step("Record initial recovery counters"):
            initial_counters = get_phy_recovery_counters(selected_port.name)
            initial_total = initial_counters[PhyRecoveryCounters.TOTAL_SUCCESSFUL_RECOVERY_EVENTS]
            initial_successful = initial_counters[PhyRecoveryCounters.SUCCESSFUL_RECOVERY_EVENTS]
            logger.info(
                "Initial counters: total-successful=%s, successful=%s",
                initial_total, initial_successful,
            )

        with allure.step(f"Run action start phy-recovery on port {selected_port.name}"):
            result = trigger_recovery_go_once(selected_port.name)
            result.verify_result()

        with allure.step("Verify at least one recovery counter increased after go-once"):
            new_counters = wait_for_recovery_counters_update(
                port_name=selected_port.name,
                initial_total_count=initial_total,
                initial_successful_count=initial_successful,
            )
            logger.info(
                "Recovery counters after go-once: total-successful %s -> %s, "
                "successful %s -> %s",
                initial_total, new_counters[PhyRecoveryCounters.TOTAL_SUCCESSFUL_RECOVERY_EVENTS],
                initial_successful, new_counters[PhyRecoveryCounters.SUCCESSFUL_RECOVERY_EVENTS],
            )

        with allure.step(f"Verify expected log message '{GoOnceConsts.LOG_MESSAGE}' in syslog"):
            system.log.verify_expected_logs(
                logs_to_find=[GoOnceConsts.LOG_MESSAGE],
                logs_source=LogsSources.SYSLOG,
                engine=engines.dut,
                only_latest_log=True
            )

    finally:
        cleanup_phy_recovery_config(fae_port)


# =============================================================================
# Test: PHY Recovery Attributes
# =============================================================================

@pytest.mark.plu_phy_recovery
def test_plu_phy_recovery_attributes(devices, random_api):
    """
    Verify default PHY recovery attributes via "nv show interface".

    Steps:
        1. Select a random port in LINK-UP state.
        2. Run `nv show fae interface <port> link phy-recovery` and parse output.
        3. Confirm all default attributes match expected values.
    """
    TestToolkit.tested_api = random_api

    with allure.step("Select a port for test"):
        port_result = RandomizationTool.select_random_port(
            requested_ports_state=NvosConsts.LINK_STATE_UP,
        )
        if not port_result.result:
            pytest.skip(f"Skipping test - {port_result.info}")
        selected_port = Fae(port_name=port_result.get_returned_value().name)

    with allure.step("Validate PHY recovery has default values"):
        validate_default_config(selected_port)


# =============================================================================
# Test: PHY Recovery Bad Flow (Negative)
# =============================================================================

@pytest.mark.plu_phy_recovery
def test_plu_phy_recovery_bad_flow(devices, random_api):
    """
    Negative validation of phy-recovery parameters - reject invalid values.

    Uses logic-relock-mode and logic-relock-timeout.
    Timeout range is 0-126.

    Steps:
        1. Attempt to show a non-existent phy-recovery attribute; expect failure.
        2. Test setting bad mode value for logic-relock-mode.
        3. Test setting bad timeout value for logic-relock-timeout.
    """
    TestToolkit.tested_api = random_api

    with allure.step("Select a port for test"):
        port_result = RandomizationTool.select_random_port(
            requested_ports_state=NvosConsts.LINK_STATE_UP,
        )
        if not port_result.result:
            pytest.skip(f"Skipping test - no ports in UP state: {port_result.info}")
        selected_port = Fae(port_name=port_result.get_returned_value().name)

    phy_recovery_obj = selected_port.port.interface.link.phy_recovery

    with allure.step("Start bad-flow scenarios"):
        with allure.independent_step("Testing show non-existing attribute in phy-recovery"):
            phy_recovery_obj.show('non-existing', should_succeed=False)

        with allure.independent_step(f"Testing bad-mode on interface {selected_port.port.name}"):
            logger.info(f"Set {PhyRecoveryConfig.MODE} to bad-mode")
            phy_recovery_obj.set(
                PhyRecoveryConfig.MODE,
                "bad-mode",
                expected_str=ErrorMessages.BAD_MODE
            ).verify_result()

        with allure.independent_step(f"Testing timeout below min on interface {selected_port.port.name}"):
            below_min = PhyRecoveryConfig.TIMEOUT_MIN - 1
            logger.info(f"Set {PhyRecoveryConfig.TIMEOUT} to {below_min} (below min)")
            phy_recovery_obj.set(
                PhyRecoveryConfig.TIMEOUT,
                below_min,
                expected_str=ErrorMessages.TIMEOUT_BELOW_MIN_ANY
            ).verify_result()

        with allure.independent_step(f"Testing timeout above max on interface {selected_port.port.name}"):
            above_max = PhyRecoveryConfig.TIMEOUT_MAX + 1
            logger.info(f"Set {PhyRecoveryConfig.TIMEOUT} to {above_max} (above max)")
            phy_recovery_obj.set(
                PhyRecoveryConfig.TIMEOUT,
                above_max,
                expected_str=ErrorMessages.TIMEOUT_ABOVE_MAX_ANY
            ).verify_result()

        with allure.independent_step(f"Testing non-integer timeout on interface {selected_port.port.name}"):
            logger.info(f"Set {PhyRecoveryConfig.TIMEOUT} to 'x' (non-integer)")
            phy_recovery_obj.set(
                PhyRecoveryConfig.TIMEOUT,
                "x",
                expected_str=ErrorMessages.TIMEOUT_NOT_INTEGER_ANY
            ).verify_result()


# =============================================================================
# Test: Set PHY Recovery Mode and Timeout (All Ports)
# =============================================================================

@pytest.mark.plu_phy_recovery
def test_plu_set_fae_phy_recovery(devices, random_api, test_name):
    """
    Verify PHY recovery settings can be applied to ALL ports (sw and acl).

    Applies settings to ALL port groups (grouped by prefix) and then
    verifies on a single random port.

    Steps:
        1. Get all port group ranges (one range per prefix)
        2. Select a random port for verification
        3. Verify default configuration on verification port
        4. Set logic-relock-mode to enabled on ALL port groups
        5. Verify mode is enabled on verification port
        6. Set logic-relock-timeout to a higher value on ALL port groups
        7. Verify new timeout value on verification port
        8. Unset configuration on ALL port groups to restore defaults
    """
    TestToolkit.tested_api = random_api

    with allure.step("Get all port group ranges"):
        all_port_groups = get_all_ports_ranges()
        if not all_port_groups:
            pytest.skip("No ports found")
        logger.info(f"All port groups: {all_port_groups}")

    with allure.step("Select a random port for verification"):
        port_result = RandomizationTool.select_random_port(
            requested_ports_state=NvosConsts.LINK_STATE_UP,
        )
        if not port_result.result:
            port_result.ignore_result()
            pytest.skip(f"Skipping test - {port_result.info}")

        verification_port_name = port_result.get_returned_value().name
        verification_port = Fae(port_name=verification_port_name)
        logger.info(f"Verification port: {verification_port_name}")

    with allure.step("Verify default configuration"):
        validate_default_config(verification_port)

    try:
        with allure.step(f"Set {PhyRecoveryConfig.MODE} to {PhyRecoveryConfig.ENABLED} on ALL port groups"):
            result_obj, duration = OperationTime.save_duration(
                'bulk_mode_apply',
                f'mode={PhyRecoveryConfig.ENABLED}',
                test_name,
                apply_mode_to_all,
                all_port_groups,
                PhyRecoveryConfig.ENABLED
            )
            logger.info(f"Bulk apply mode completed in {duration:.2f}s")
            verify_config(verification_port, PhyRecoveryConfig.ENABLED)

        higher_timeout = random.randint(
            PhyRecoveryTestConsts.HIGHER_TIMEOUT_MIN,
            PhyRecoveryTestConsts.HIGHER_TIMEOUT_MAX,
        )

        with allure.step(f"Update timeout to higher value ({higher_timeout}) on ALL port groups"):
            result_obj, duration = OperationTime.save_duration(
                'bulk_timeout_apply',
                f'timeout={higher_timeout}',
                test_name,
                apply_timeout_to_all,
                all_port_groups,
                higher_timeout
            )
            logger.info(f"Bulk apply timeout completed in {duration:.2f}s")
            verify_config(verification_port, PhyRecoveryConfig.ENABLED, higher_timeout)

    finally:
        with allure.step("Cleanup: Unset configuration on ALL port groups to return to defaults"):
            result_obj, duration = OperationTime.save_duration(
                'bulk_config_unset',
                '',
                test_name,
                unset_config_all,
                all_port_groups
            )
            logger.info(f"Bulk unset config completed in {duration:.2f}s")

        with allure.step("Verify configuration restored to defaults on verification port"):
            cleanup_phy_recovery_config(verification_port)
