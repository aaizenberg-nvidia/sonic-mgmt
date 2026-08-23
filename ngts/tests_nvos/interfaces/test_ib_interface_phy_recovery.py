"""
IB Interface PHY Recovery Tests

This module contains tests for IB (InfiniBand) interface PHY recovery functionality.
The tests verify PHY recovery configuration, counters, and traffic scenarios during recovery events.

Based on Link_Phy_Recovery_NVOS_HLD document.

Test Cases:
    1. test_ib_phy_recovery_go_once - Go-once action (bad flow + good flow)
    2. test_ib_phy_recovery_attributes - Verify default attributes
    3. test_ib_phy_recovery_bad_flow - Negative test for invalid params
    4. test_ib_set_fae_phy_recovery - Set mode/timeout on ALL ports
    5. test_ib_recovery_go_once_during_traffic - Go-once during traffic
    6. test_ib_recovery_always_fail_during_traffic - Always fail injection
    7. test_ib_recovery_noise_injection_during_traffic - Noise injection

Key Differences from NVLink:
    - IB uses `logic-relock-mode` and `logic-relock-timeout` (NOT serdes-eq)
    - IB timeout range is 0-126 (NVLink is 0-2550)
    - Recovery counters viewed via `nv show interface <port> link phy detail`
"""
import logging
import random
import time

import pytest

from ngts.nvos_constants.constants_nvos import ApiType, LogsSources
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import (
    IbInterfaceConsts,
    NvosConsts,
)
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure

# Import constants from the ib_phy_recovery module
from ngts.tests_nvos.interfaces.ib_phy_recovery.consts import (
    IbPhyRecoveryCounters,
    IbPhyRecoveryConfig,
    IbPhyRecoveryTestConsts,
    PREIErrorInjection,
    ErrorMessages,
    GoOnceConsts,
)

# Import helpers from the ib_phy_recovery module
from ngts.tests_nvos.interfaces.ib_phy_recovery.helpers import (
    get_phy_recovery_counters,
    get_successful_recovery_events,
    trigger_recovery_go_once,
    inject_error_via_prei,
    disable_error_injection,
    validate_traffic_counters_increased,
    wait_for_recovery_counters_update,
    wait_for_traffic_running,
    validate_ib_mode_set,
    validate_ib_default_config,
    validate_link_state,
    get_all_ib_ports_range,
    apply_ib_mode,
    apply_ib_mode_if_needed,
    apply_ib_timeout,
    verify_ib_config,
    unset_ib_config,
    get_local_port_and_mst_device,
    setup_traffic_test,
    teardown_traffic_test,
    verify_link_recovery_and_traffic,
    verify_traffic_stopped_and_cleanup,
    verify_traffic_restored,
    cleanup_phy_recovery_config,
    kill_traffic_processes,
)

# logic-relock is XDR-scoped: a port that trained at a lower generation reports the
# firmware default instead of the platform default, so the tests that assert the
# platform default must select on the negotiated ib-speed.
XDR_SKIP_REASON = (
    "no UP IB port reports ib-speed xdr - logic-relock defaults are XDR-scoped, "
    "and ib-speed is only reported once the port is Active"
)

logger = logging.getLogger(__name__)


# =============================================================================
# Test: PHY Recovery Go-Once Action (Bad Flow - Parametrized)
# =============================================================================

@pytest.mark.ib_interfaces
@pytest.mark.ib_phy_recovery
@pytest.mark.parametrize("invalid_port", GoOnceConsts.INVALID_PORT_TYPES)
def test_ib_phy_recovery_go_once_bad_flow(invalid_port, random_api):
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
        # Action should fail on non-IB ports - use verify_result(False) to assert failure
        result = fae_port.port.interface.link.phy_recovery.action(
            "start",
            expected_output=""  # Expect failure, not success message
        )
        result.verify_result(False)
        logger.info(f"Bad flow verified: action correctly failed on {invalid_port}")


# =============================================================================
# Test: PHY Recovery Go-Once Action (Good Flow)
# =============================================================================

@pytest.mark.ib_interfaces
@pytest.mark.ib_phy_recovery
def test_ib_phy_recovery_go_once(engines, devices, random_api):
    """
    Verify the go-once phy-recovery action for IB ports (good flow).

    Command: nv action start fae interface <port> link phy-recovery

    Steps:
        1. Select a random IB port in LINK-UP state.
        2. Set logic-relock-mode to enabled if not already enabled.
        3. Verify mode is enabled on verification port.
        4. Run go-once action and verify it succeeds.
        5. Check syslog for expected message.
    """
    TestToolkit.tested_api = random_api

    system = System()

    with allure.step("Good flow: Select a random XDR IB port in LINK-UP state"):
        port_result = RandomizationTool.select_random_port(
            requested_ports_state=NvosConsts.LINK_STATE_UP,
            requested_ports_type=IbInterfaceConsts.IB_PORT_TYPE,
            requested_ib_speed=IbInterfaceConsts.XDR
        )
        if not port_result.result:
            port_result.ignore_result()
            pytest.skip(f"Skipping test - {XDR_SKIP_REASON}: {port_result.info}")
        selected_port = port_result.get_returned_value()
        logger.info(f"Selected IB port for go-once test: {selected_port.name}")

    fae_ib_port = Fae(port_name=selected_port.name)

    try:
        with allure.step(f"Set {IbPhyRecoveryConfig.MODE} to {IbPhyRecoveryConfig.ENABLED} if not already enabled"):
            mode_was_changed = apply_ib_mode_if_needed(fae_ib_port, IbPhyRecoveryConfig.ENABLED)
            logger.info(f"Mode was changed: {mode_was_changed}")

        with allure.step(f"Verify mode is {IbPhyRecoveryConfig.ENABLED} on port {selected_port.name}"):
            verify_ib_config(fae_ib_port, IbPhyRecoveryConfig.ENABLED)

        with allure.step(f"Run action start phy-recovery on IB port {selected_port.name}"):
            result = trigger_recovery_go_once(selected_port.name)
            result.verify_result()

        with allure.step("Wait for syslog to be updated"):
            time.sleep(IbPhyRecoveryTestConsts.BETWEEN_TRIGGERS_WAIT_SECONDS)

        with allure.step(f"Verify expected log message '{GoOnceConsts.LOG_MESSAGE}' in syslog"):
            system.log.verify_expected_logs(
                logs_to_find=[GoOnceConsts.LOG_MESSAGE],
                logs_source=LogsSources.SYSLOG,
                engine=engines.dut,
                only_latest_log=True
            )

    finally:
        cleanup_phy_recovery_config(fae_ib_port)


# =============================================================================
# Test: PHY Recovery Attributes
# =============================================================================

@pytest.mark.ib_interfaces
@pytest.mark.ib_phy_recovery
def test_ib_phy_recovery_attributes(devices, random_api):
    """
    Verify default IB PHY recovery attributes via "nv show interface".

    Steps:
        1. Select a random IB port in LINK-UP state for test
        2. Run `nv show fae interface <port> link phy-recovery` and parse JSON output.
        3. Confirm all default attributes match expected values.
    """
    TestToolkit.tested_api = random_api

    with allure.step("Select an XDR IB port for test"):
        port_result = RandomizationTool.select_random_port(
            requested_ports_state=NvosConsts.LINK_STATE_UP,
            requested_ports_type=IbInterfaceConsts.IB_PORT_TYPE,
            requested_ib_speed=IbInterfaceConsts.XDR
        )
        if not port_result.result:
            port_result.ignore_result()
            pytest.skip(f"Skipping test - {XDR_SKIP_REASON}: {port_result.info}")
        selected_port = Fae(port_name=port_result.get_returned_value().name)

    with allure.step("Validate PHY recovery has default values"):
        validate_ib_default_config(selected_port)


# =============================================================================
# Test: PHY Recovery Bad Flow (Negative)
# =============================================================================

@pytest.mark.ib_interfaces
@pytest.mark.ib_phy_recovery
def test_ib_phy_recovery_bad_flow(devices, random_api):
    """
    Negative validation of IB phy-recovery parameters - reject invalid values.

    For IB, we use logic-relock-mode and logic-relock-timeout (not serdes-eq).
    Timeout range for IB is 0-126.

    Steps:
        1. Attempt to show a non-existent phy-recovery attribute; expect failure.
        2. Test setting bad mode value for logic-relock-mode.
        3. Test setting bad timeout value for logic-relock-timeout.
    """
    TestToolkit.tested_api = random_api

    with allure.step("Select an IB port for test"):
        port_result = RandomizationTool.select_random_port(
            requested_ports_state=NvosConsts.LINK_STATE_UP,
            requested_ports_type=IbInterfaceConsts.IB_PORT_TYPE
        )
        if not port_result.result:
            pytest.skip(f"Skipping test - no IB ports in UP state: {port_result.info}")
        selected_port = Fae(port_name=port_result.get_returned_value().name)

    phy_recovery_obj = selected_port.port.interface.link.phy_recovery

    with allure.step("Start bad-flow scenarios"):
        with allure.independent_step("Testing show non-existing attribute in phy-recovery"):
            phy_recovery_obj.show('non-existing', should_succeed=False)

        with allure.independent_step(f"Testing bad-mode on interface {selected_port.port.name}"):
            logger.info(f"Set {IbPhyRecoveryConfig.MODE} to bad-mode")
            phy_recovery_obj.set(
                IbPhyRecoveryConfig.MODE,
                "bad-mode",
                expected_str=ErrorMessages.BAD_MODE
            ).verify_result()

        with allure.independent_step(f"Testing timeout below min on interface {selected_port.port.name}"):
            below_min = IbPhyRecoveryConfig.TIMEOUT_MIN - 1
            logger.info(f"Set {IbPhyRecoveryConfig.TIMEOUT} to {below_min} (below min)")
            phy_recovery_obj.set(
                IbPhyRecoveryConfig.TIMEOUT,
                below_min,
                expected_str=ErrorMessages.TIMEOUT_BELOW_MIN_ANY
            ).verify_result()

        with allure.independent_step(f"Testing timeout above max on interface {selected_port.port.name}"):
            above_max = IbPhyRecoveryConfig.TIMEOUT_MAX + 1
            logger.info(f"Set {IbPhyRecoveryConfig.TIMEOUT} to {above_max} (above max)")
            phy_recovery_obj.set(
                IbPhyRecoveryConfig.TIMEOUT,
                above_max,
                expected_str=ErrorMessages.TIMEOUT_ABOVE_MAX_ANY
            ).verify_result()

        with allure.independent_step(f"Testing non-integer timeout on interface {selected_port.port.name}"):
            logger.info(f"Set {IbPhyRecoveryConfig.TIMEOUT} to 'x' (non-integer)")
            phy_recovery_obj.set(
                IbPhyRecoveryConfig.TIMEOUT,
                "x",
                expected_str=ErrorMessages.TIMEOUT_NOT_INTEGER_ANY
            ).verify_result()


# =============================================================================
# Test: Set PHY Recovery Mode and Timeout (All IB Ports)
# =============================================================================

@pytest.mark.ib_interfaces
@pytest.mark.ib_phy_recovery
def test_ib_set_fae_phy_recovery(devices, random_api, test_name):
    """
    Verify IB PHY recovery settings can be applied to ALL IB ports.

    This test applies settings to ALL IB ports at once using a port range
    (e.g., "sw1-72p1-2"), then verifies the settings on a single random port.

    For IB interfaces:
        - Use logic-relock-mode (not serdes-eq-mode)
        - Use logic-relock-timeout (not serdes-eq-timeout)
        - Timeout range is 0-126

    Steps:
        1. Get all IB ports and create range string (e.g., "sw1-72p1-2")
        2. Select a random IB port for verification
        3. Verify default configuration on verification port
        4. Set logic-relock-mode to enabled on ALL ports
        5. Verify mode is enabled on verification port
        6. Set logic-relock-timeout to higher value on ALL ports
        7. Verify new timeout value on verification port
        8. Set logic-relock-timeout to lower value on ALL ports
        9. Verify new timeout value on verification port
        10. Set logic-relock-mode to disabled on ALL ports
        11. Unset configuration on ALL ports to restore defaults
    """
    TestToolkit.tested_api = random_api

    with allure.step("Get all IB ports range"):
        all_ports_range = get_all_ib_ports_range()
        if not all_ports_range:
            pytest.skip("No IB ports found")
        logger.info(f"All IB ports range: {all_ports_range}")

        # Create Fae object for ALL ports (using range)
        all_ports = Fae(port_name=all_ports_range)

    with allure.step("Select a random XDR IB port for verification"):
        port_result = RandomizationTool.select_random_port(
            requested_ports_state=NvosConsts.LINK_STATE_UP,
            requested_ports_type=IbInterfaceConsts.IB_PORT_TYPE,
            requested_ib_speed=IbInterfaceConsts.XDR
        )
        if not port_result.result:
            port_result.ignore_result()
            pytest.skip(f"Skipping test - {XDR_SKIP_REASON}: {port_result.info}")

        # Single port for verification
        verification_port_name = port_result.get_returned_value().name
        verification_port = Fae(port_name=verification_port_name)
        logger.info(f"Verification port: {verification_port_name}")

    with allure.step("Verify default configuration"):
        validate_ib_default_config(verification_port)

    try:
        with allure.step(f"Set {IbPhyRecoveryConfig.MODE} to {IbPhyRecoveryConfig.ENABLED} on ALL ports"):
            result_obj, duration = OperationTime.save_duration(
                'bulk_ib_mode_apply',
                f'mode={IbPhyRecoveryConfig.ENABLED}',
                test_name,
                apply_ib_mode,
                all_ports,
                IbPhyRecoveryConfig.ENABLED
            )
            logger.info(f"Bulk apply mode completed in {duration:.2f}s")
            # Only verify mode here - don't verify timeout since we only set mode
            # (device may auto-set timeout to 100 when mode is enabled)
            verify_ib_config(verification_port, IbPhyRecoveryConfig.ENABLED)

        # Test higher timeout (within 0-126 range for IB)
        higher_timeout = random.randint(
            IbPhyRecoveryTestConsts.HIGHER_TIMEOUT_MIN,
            IbPhyRecoveryTestConsts.HIGHER_TIMEOUT_MAX,
        )

        with allure.step(f"Update timeout to higher value ({higher_timeout}) on ALL ports"):
            result_obj, duration = OperationTime.save_duration(
                'bulk_ib_timeout_apply',
                f'timeout={higher_timeout}',
                test_name,
                apply_ib_timeout,
                all_ports,
                higher_timeout
            )
            logger.info(f"Bulk apply timeout completed in {duration:.2f}s")
            verify_ib_config(verification_port, IbPhyRecoveryConfig.ENABLED, higher_timeout)

    finally:
        with allure.step("Cleanup: Unset configuration on ALL ports to return to defaults"):
            # Use helper function with extended timeout for bulk operations
            result_obj, duration = OperationTime.save_duration(
                'bulk_ib_config_unset',
                '',
                test_name,
                unset_ib_config,
                all_ports
            )
            logger.info(f"Bulk unset config completed in {duration:.2f}s")

        with allure.step("Verify configuration restored to defaults on verification port"):
            cleanup_phy_recovery_config(verification_port)


# =============================================================================
# Traffic Test Scenarios
# =============================================================================

@pytest.mark.ib_interfaces
@pytest.mark.ib_phy_recovery
@pytest.mark.ib_traffic
def test_ib_recovery_go_once_during_traffic(
    engines, devices, players, interfaces, start_sm, setup_name, random_api
):
    """
    Test: Recovery Go Once during traffic - Traffic should not be interrupted.

    This test verifies that triggering PHY recovery via "go-once" function
    multiple times during active 2-minute traffic does not interrupt the traffic flow.

    Steps:
        1. Select random traffic port (LINK-UP)
        2. Clear all traffic counters to validate traffic works
        3. Record initial recovery counters (successful-recovery-events and total-successful-recovery-events)
        4. Start IB traffic (2 minutes between 2 hosts)
        5. Trigger recovery go-once (repeat 3 times) during traffic
        6. Stop traffic and verify results
        7. Verify at least one of the recovery counters increased
        8. Cleanup
    """
    TestToolkit.tested_api = random_api

    # Setup: Start traffic and get initial state
    # If setup fails, abort test - no point continuing with partial setup
    try:
        selected_port, initial_counters, traffic_start_time = setup_traffic_test(
            engines, devices,
            IbPhyRecoveryTestConsts.TRAFFIC_DURATION_2MIN_SECONDS,
            IbPhyRecoveryTestConsts.TRAFFIC_SERVER_OUTPUT_2MIN,
            IbPhyRecoveryTestConsts.TRAFFIC_CLIENT_OUTPUT_2MIN
        )
    except Exception as e:
        pytest.fail(f"Traffic setup failed, aborting test: {e}")

    try:
        # Do test actions: Trigger recovery go-once multiple times during traffic
        num_triggers = IbPhyRecoveryTestConsts.NUM_RECOVERY_GO_ONCE_TRIGGERS
        successful_triggers = 0
        with allure.step(f"Trigger recovery go-once {num_triggers} times during traffic"):
            for i in range(num_triggers):
                with allure.step(f"Recovery trigger {i + 1}/{num_triggers}"):
                    trigger_result = trigger_recovery_go_once(selected_port.name)
                    if trigger_result.result:
                        successful_triggers += 1
                        logger.info(f"Recovery trigger {i + 1} succeeded")
                    else:
                        logger.warning(f"Recovery trigger {i + 1} returned: {trigger_result.info}")
                    time.sleep(IbPhyRecoveryTestConsts.BETWEEN_TRIGGERS_WAIT_SECONDS)

        with allure.step(f"Verify at least one trigger succeeded ({successful_triggers}/{num_triggers})"):
            assert successful_triggers > 0, (
                f"All {num_triggers} recovery triggers failed. "
                f"This may indicate an issue with the OpenAPI action or the device. "
                f"Check logs for details."
            )

        # Wait for recovery counters to update; pass if either counter increased
        initial_successful = initial_counters.get(IbPhyRecoveryCounters.SUCCESSFUL_RECOVERY_EVENTS, 0)
        initial_total = initial_counters.get(IbPhyRecoveryCounters.TOTAL_SUCCESSFUL_RECOVERY_EVENTS, 0)
        wait_for_recovery_counters_update(
            port_name=selected_port.name,
            initial_count=initial_successful,
            initial_total_count=initial_total
        )

        # Teardown: Stop traffic and verify results
        teardown_traffic_test(
            engines, devices, selected_port, initial_counters,
            traffic_start_time,
            IbPhyRecoveryTestConsts.TRAFFIC_TIMEOUT_2MIN_SECONDS,
            IbPhyRecoveryTestConsts.TRAFFIC_SERVER_OUTPUT_2MIN,
            IbPhyRecoveryTestConsts.TRAFFIC_CLIENT_OUTPUT_2MIN
        )

    finally:
        # Ensure cleanup runs even if test fails
        kill_traffic_processes(engines)
        cleanup_phy_recovery_config(Fae(port_name=selected_port.name))


@pytest.mark.ib_interfaces
@pytest.mark.ib_phy_recovery
@pytest.mark.ib_traffic
def test_ib_recovery_always_fail_during_traffic(
    engines, devices, players, interfaces, start_sm, setup_name, random_api
):
    """
    Test: Inject "broken cable" error via PREI to verify PHY recovery behavior.

    This test simulates a broken cable scenario where error injection causes the link
    to fail and traffic to stop. After disabling injection and triggering recovery,
    the link should recover and traffic should work again.

    PREI Semantics:
        - BROKEN_CABLE (finite time): Injection stops automatically when time expires
        - Traffic client dies during injection (expected - link is broken)
        - PREI can only be configured when port is UP or TEST MODE

    Steps:
        1. Select random traffic port (LINK-UP)
        2. Enable PHY recovery mode and record initial counters
        3. Start IB traffic (7 minutes between 2 hosts)
        4. Inject "broken cable" error via PREI for ~3 minutes
        5. Disable error injection
        6. Verify traffic stopped and cleanup processes (client death expected)
        7. Trigger PHY recovery
        8. Verify recovery counters updated
        9. Verify traffic is restored after recovery
        10. Cleanup
    """
    TestToolkit.tested_api = random_api
    injection_duration_seconds = IbPhyRecoveryTestConsts.ALWAYS_FAIL_DURATION_SECONDS

    # Setup: Start traffic and get initial state
    # If setup fails, abort test - no point continuing with partial setup
    try:
        selected_port, initial_counters, traffic_start_time = setup_traffic_test(
            engines, devices,
            IbPhyRecoveryTestConsts.TRAFFIC_DURATION_7MIN_SECONDS,
            IbPhyRecoveryTestConsts.TRAFFIC_SERVER_OUTPUT_7MIN,
            IbPhyRecoveryTestConsts.TRAFFIC_CLIENT_OUTPUT_7MIN
        )
    except Exception as e:
        pytest.fail(f"Traffic setup failed, aborting test: {e}")

    # Verify traffic is running before proceeding with error injection
    traffic_running_result = wait_for_traffic_running(engines)
    traffic_running_result.verify_result()

    # Initialize before try block to ensure defined in finally
    local_port, mst_device = None, None

    with allure.step("Get local port and MST device"):
        try:
            local_port, mst_device = get_local_port_and_mst_device(selected_port.name)
        except ValueError as e:
            pytest.skip(f"Could not get local port info: {e}")

    try:
        # Do test actions: Inject "always fail" error (broken cable) and wait for the duration
        with allure.step(f"Inject always-fail error via PREI and wait {injection_duration_seconds} seconds"):
            inject_error_via_prei(
                engines.dut,
                mst_device,
                local_port,
                PREIErrorInjection.ERROR_TYPE_ADMIN_TRIGGER_RECOVERY,
                PREIErrorInjection.BROKEN_CABLE
            )
            logger.info(f"Injected BROKEN_CABLE error, waiting {injection_duration_seconds} seconds")
            time.sleep(injection_duration_seconds)

        with allure.step("Disable error injection"):
            disable_error_injection(engines.dut, mst_device, local_port)
            time.sleep(30)

        # Verify traffic stopped (client death expected) and cleanup remaining processes
        cleanup_result = verify_traffic_stopped_and_cleanup(engines)
        logger.info(f"Traffic cleanup result: {cleanup_result.info}")

        with allure.step("Recovery trigger"):
            trigger_result = trigger_recovery_go_once(selected_port.name)
            if trigger_result.result:
                logger.info("Recovery trigger succeeded")
            else:
                logger.warning(f"Recovery trigger returned: {trigger_result.info}")

        # Wait for recovery counters to update; pass if either counter increased
        initial_successful = initial_counters.get(IbPhyRecoveryCounters.SUCCESSFUL_RECOVERY_EVENTS, 0)
        initial_total = initial_counters.get(IbPhyRecoveryCounters.TOTAL_SUCCESSFUL_RECOVERY_EVENTS, 0)
        final_successful = wait_for_recovery_counters_update(
            port_name=selected_port.name,
            initial_count=initial_successful,
            initial_total_count=initial_total
        )
        logger.info("Recovery counter(s) verified: successful-recovery-events %s -> %s", initial_successful, final_successful)

        # Verify traffic is restored after recovery
        traffic_result = verify_traffic_restored(
            engines, players, interfaces, setup_name, wait_time=10
        )
        traffic_result.verify_result()

    finally:
        # Disable error injection if local_port and mst_device were obtained
        if local_port and mst_device:
            with allure.step("Disable error injection (cleanup)"):
                disable_error_injection(engines.dut, mst_device, local_port)
        # Ensure all traffic processes are killed in cleanup
        kill_traffic_processes(engines)
        cleanup_phy_recovery_config(Fae(port_name=selected_port.name))


@pytest.mark.ib_interfaces
@pytest.mark.ib_phy_recovery
@pytest.mark.ib_traffic
def test_ib_recovery_noise_injection_during_traffic(
    engines, devices, players, interfaces, start_sm, setup_name, random_api
):
    """
    Test: Inject noise during 7-minute traffic with random delays.

    This test mimics a flaky cable scenario with intermittent errors.
    Noise is injected at random intervals (3-10 seconds) for 5 minutes total.

    Steps:
        1. Select random traffic port (LINK-UP)
        2. Clear all traffic counters to validate traffic works
        3. Record initial recovery counters (successful-recovery-events and total-successful-recovery-events)
        4. Start IB traffic (7 minutes between 2 hosts)
        5. Inject "noise" error via PREI at random intervals during traffic (5 minutes)
        6. Disable error injection
        7. Stop traffic and verify results
        8. Verify at least one of the recovery counters increased
        9. Cleanup
    """
    TestToolkit.tested_api = random_api
    total_duration = IbPhyRecoveryTestConsts.NOISE_INJECTION_TOTAL_DURATION_SECONDS
    min_delay = IbPhyRecoveryTestConsts.NOISE_INJECTION_MIN_DELAY_SECONDS
    max_delay = IbPhyRecoveryTestConsts.NOISE_INJECTION_MAX_DELAY_SECONDS

    # Setup: Start traffic and get initial state
    # If setup fails, abort test - no point continuing with partial setup
    try:
        selected_port, initial_counters, traffic_start_time = setup_traffic_test(
            engines, devices,
            IbPhyRecoveryTestConsts.TRAFFIC_DURATION_7MIN_SECONDS,
            IbPhyRecoveryTestConsts.TRAFFIC_SERVER_OUTPUT_7MIN,
            IbPhyRecoveryTestConsts.TRAFFIC_CLIENT_OUTPUT_7MIN
        )
    except Exception as e:
        pytest.fail(f"Traffic setup failed, aborting test: {e}")

    # Initialize before try block to ensure defined in finally
    local_port, mst_device = None, None

    with allure.step("Get local port and MST device"):
        try:
            local_port, mst_device = get_local_port_and_mst_device(selected_port.name)
        except ValueError as e:
            pytest.skip(f"Could not get local port info: {e}")

    try:
        # Do test actions: Inject noise at random intervals during traffic
        with allure.step(f"Inject noise during traffic for {total_duration} seconds with random delays ({min_delay}-{max_delay}s)"):
            start_time = time.time()
            injection_count = 0

            while (time.time() - start_time) < total_duration:
                elapsed = int(time.time() - start_time)
                remaining = total_duration - elapsed
                injection_count += 1

                # Random delay between injections
                delay = random.randint(min_delay, max_delay)

                with allure.step(f"Injection {injection_count} (elapsed: {elapsed}s, remaining: {remaining}s, next delay: {delay}s)"):
                    inject_error_via_prei(
                        engines.dut,
                        mst_device,
                        local_port,
                        PREIErrorInjection.ERROR_TYPE_ADMIN_TRIGGER_RECOVERY,
                        PREIErrorInjection.FLAKY_CABLE
                    )
                    logger.info(f"Injection {injection_count}: waiting {delay}s before next injection")

                    # Wait random delay before next injection (unless we've exceeded duration)
                    if (time.time() - start_time + delay) < total_duration:
                        time.sleep(delay)
                    else:
                        # Don't wait if we've reached the end
                        break

            logger.info(f"Completed {injection_count} noise injections over {total_duration} seconds")

        with allure.step("Disable error injection"):
            disable_error_injection(engines.dut, mst_device, local_port)

        # Wait for recovery counters to update; pass if either counter increased
        initial_successful = initial_counters.get(IbPhyRecoveryCounters.SUCCESSFUL_RECOVERY_EVENTS, 0)
        initial_total = initial_counters.get(IbPhyRecoveryCounters.TOTAL_SUCCESSFUL_RECOVERY_EVENTS, 0)
        wait_for_recovery_counters_update(
            port_name=selected_port.name,
            initial_count=initial_successful,
            initial_total_count=initial_total
        )

        # Teardown: Stop traffic and verify results
        teardown_traffic_test(
            engines, devices, selected_port, initial_counters,
            traffic_start_time,
            IbPhyRecoveryTestConsts.TRAFFIC_TIMEOUT_7MIN_SECONDS,
            IbPhyRecoveryTestConsts.TRAFFIC_SERVER_OUTPUT_7MIN,
            IbPhyRecoveryTestConsts.TRAFFIC_CLIENT_OUTPUT_7MIN
        )

    finally:
        # Disable error injection if local_port and mst_device were obtained
        if local_port and mst_device:
            with allure.step("Cleanup: Disable error injection"):
                disable_error_injection(engines.dut, mst_device, local_port)
        kill_traffic_processes(engines)
        cleanup_phy_recovery_config(Fae(port_name=selected_port.name))
