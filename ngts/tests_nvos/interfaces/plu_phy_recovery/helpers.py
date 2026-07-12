"""
PLU PHY Recovery Helper Functions

Reusable helper functions for PLU PHY recovery tests.
Works on all port types (sw and acl).

Functions:
    - Port Selection: get_all_ports_range
    - Recovery Actions: trigger_recovery_go_once
    - Configuration: apply_mode, apply_mode_if_needed, apply_timeout,
                     verify_config, unset_config, cleanup_phy_recovery_config
    - Validation: validate_mode_set, validate_default_config
"""

import logging
import time
from typing import Dict, List, Optional

from retry.api import retry_call

from ngts.nvos_constants.constants_nvos import ApiType, ConfState, NvosConst
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port, PortRequirements
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.cluster.cluster_tools import summarize_switch_ports
from ngts.tools.test_utils import allure_utils as allure

from ngts.tests_nvos.interfaces.plu_phy_recovery.constants import (
    PhyRecoveryConfig,
    PhyRecoveryCounters,
    PhyRecoveryTestConsts,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Port Selection Helpers
# =============================================================================

def _group_ports_by_prefix() -> Dict[str, List[str]]:
    """
    Group all ports by their name prefix (e.g., ``sw``, ``acp``).

    Returns:
        Dictionary mapping prefix to list of port names.
    """
    port_requirements = PortRequirements()
    all_ports = Port.get_list_of_ports(port_requirements_object=port_requirements)

    groups: Dict[str, List[str]] = {}
    for port in all_ports:
        for prefix in PhyRecoveryConfig.SUPPORTED_PORT_PREFIXES:
            if port.name.startswith(prefix):
                groups.setdefault(prefix, []).append(port.name)
                break

    return groups


def get_all_ports_ranges() -> List[str]:
    """
    Get summarized range strings for all port groups (sw, acp, etc.).

    Groups ports by prefix and creates a separate range string for each
    group so that NVUE commands can be applied per-group.

    Returns:
        List of port range strings (one per prefix group), or empty list
        if no ports found.

    Example:
        ranges = get_all_ports_ranges()
        # ["sw1-18p1-2s1", "acp1-2p1"]
    """
    with allure.step("Get all port group ranges"):
        groups = _group_ports_by_prefix()

        if not groups:
            logger.warning("No ports found")
            return []

        ranges = []
        for prefix, port_names in groups.items():
            port_range = summarize_switch_ports(port_names)
            logger.info(f"Port group '{prefix}': {len(port_names)} ports -> {port_range}")
            ranges.append(port_range)

        return ranges


# =============================================================================
# Recovery Action Helpers
# =============================================================================

def trigger_recovery_go_once(port_name: str) -> ResultObj:
    """
    Trigger a single PHY recovery event using the action command.

    Command: nv action start fae interface <port> link phy-recovery

    Args:
        port_name: Port name to trigger recovery on

    Returns:
        ResultObj indicating success/failure
    """
    with allure.step(f"Trigger recovery go-once on {port_name}"):
        fae = Fae(port_name=port_name)
        try:
            result = fae.port.interface.link.phy_recovery.action_start_go_once()
            if not result.result:
                logger.warning(f"Recovery trigger for {port_name} did not return expected output. "
                               f"Result: {result.info}")
            return result
        except (OSError, TimeoutError, RuntimeError) as e:
            logger.error(f"Recovery trigger failed: {e}")
            return ResultObj(False, info=f"Recovery trigger failed: {e}")


# =============================================================================
# Validation Helpers
# =============================================================================

def validate_mode_set(selected_port, mode: str, timeout: Optional[int] = None):
    """
    Validate PHY recovery mode (logic-relock) is applied to the selected port.

    Args:
        selected_port: Fae port object
        mode: Expected mode (enabled/disabled/fw-default)
        timeout: Expected timeout value (optional)

    Raises:
        AssertionError: If validation fails
    """
    with allure.step(f"Validate logic-relock mode {mode} is applied"):
        output = OutputParsingTool.parse_json_str_to_dictionary(
            selected_port.port.interface.link.phy_recovery.show()
        ).get_returned_value()

        # fw-default maps to disabled operationally
        expected_mode = PhyRecoveryConfig.DISABLED if mode == PhyRecoveryConfig.FW_DEFAULT else mode
        actual_mode = output.get(PhyRecoveryConfig.MODE)
        ValidationTool.compare_values(actual_mode, expected_mode).verify_result()

        if timeout is not None:
            actual_timeout = int(output.get(PhyRecoveryConfig.TIMEOUT, 0))
            ValidationTool.compare_values(actual_timeout, timeout).verify_result()


def validate_default_config(selected_port):
    """
    Validate PHY recovery configuration has default values.

    Defaults: logic-relock-mode=disabled, logic-relock-timeout=0

    Args:
        selected_port: Fae port object

    Raises:
        AssertionError: If validation fails
    """
    with allure.step("Check default config (logic-relock)"):
        output_fae_port = OutputParsingTool.parse_json_str_to_dictionary(
            selected_port.port.interface.link.phy_recovery.show()
        ).get_returned_value()

        filtered_out = {
            key: value for key, value in output_fae_port.items()
            if key in PhyRecoveryConfig.DEFAULT_CONFIG
        }
        ValidationTool.compare_dictionaries(
            filtered_out,
            PhyRecoveryConfig.DEFAULT_CONFIG
        ).verify_result()


# =============================================================================
# Configuration Application Helpers
# =============================================================================

def _is_bulk_operation(port_name: str) -> bool:
    """Check if port name represents a bulk operation (port range)."""
    return '-' in port_name


def _bulk_apply_config(timeout_ms: int = PhyRecoveryTestConsts.BULK_APPLY_TIMEOUT_MS):
    """
    Apply staged configuration with extended timeout for bulk operations.

    Note: NVUE can legally return a "no config diff" message for idempotent applies.
    This is treated as success.

    Args:
        timeout_ms: Timeout in milliseconds for apply operation

    Raises:
        AssertionError: If config apply fails (excluding no-diff cases)
    """
    with allure.step(f"Apply config with extended timeout ({timeout_ms}ms)"):
        engine = TestToolkit.engines.dut
        start_time = time.perf_counter()
        output = engine.run_cmd('nv config apply -y', timeout=timeout_ms)
        duration = time.perf_counter() - start_time

        is_applied = ConfState.APPLIED in output.lower()
        is_no_diff = NvosConst.NO_CONFIG_DIFF_APPLY_MSG in output

        logger.info(f"Bulk apply completed in {duration:.2f}s")

        if not (is_applied or is_no_diff):
            raise AssertionError(f"Config apply failed. Output: {output}")


def _apply_config_with_bulk_support(
    fae_port,
    config_key: Optional[str],
    config_value,
    step_description: str
):
    """
    Apply configuration with automatic bulk operation support.

    Handles both single port and bulk port range operations.

    Args:
        fae_port: Fae object (single port or port range)
        config_key: Configuration key to set (or None for unset)
        config_value: Value to apply (or None for unset)
        step_description: Description for allure step
    """
    with allure.step(step_description):
        is_bulk = _is_bulk_operation(fae_port.port.name)
        use_staged_apply = is_bulk and TestToolkit.tested_api == ApiType.NVUE

        if use_staged_apply:
            apply_kwargs = {"apply": False}
        else:
            apply_kwargs = {"apply": True, "ask_for_confirmation": True}

        if config_key is None:
            fae_port.port.interface.link.phy_recovery.unset(**apply_kwargs).verify_result()
        else:
            fae_port.port.interface.link.phy_recovery.set(
                config_key, config_value, **apply_kwargs
            ).verify_result()

        if use_staged_apply:
            _bulk_apply_config()

    with allure.step(f"Wait {PhyRecoveryTestConsts.CONFIG_APPLY_WAIT_SECONDS}s for config to take effect"):
        time.sleep(PhyRecoveryTestConsts.CONFIG_APPLY_WAIT_SECONDS)


def get_current_mode(fae_port) -> str:
    """
    Get the current logic-relock-mode from a port.

    Args:
        fae_port: Fae object for a SINGLE port

    Returns:
        Current mode string (enabled/disabled/fw-default)
    """
    output = OutputParsingTool.parse_json_str_to_dictionary(
        fae_port.port.interface.link.phy_recovery.show()
    ).get_returned_value()

    current_mode = output.get(PhyRecoveryConfig.MODE, PhyRecoveryConfig.DEFAULT_MODE)
    logger.info(f"Current {PhyRecoveryConfig.MODE} for {fae_port.port.name}: {current_mode}")
    return current_mode


def apply_mode(fae_port, mode: str) -> ResultObj:
    """
    Apply logic-relock-mode to the selected port(s).

    Uses extended timeout for bulk operations (port ranges).

    Args:
        fae_port: Fae object (single port or port range)
        mode: Mode to apply (enabled/disabled/fw-default)

    Returns:
        ResultObj(True) on success
    """
    _apply_config_with_bulk_support(
        fae_port,
        PhyRecoveryConfig.MODE,
        mode,
        f"Apply {PhyRecoveryConfig.MODE}={mode}"
    )
    return ResultObj(True)


def apply_mode_if_needed(fae_port, mode: str) -> bool:
    """
    Apply logic-relock-mode only if not already set to the desired value.

    Args:
        fae_port: Fae object for a SINGLE port
        mode: Mode to apply (enabled/disabled/fw-default)

    Returns:
        True if mode was applied, False if already set
    """
    current_mode = get_current_mode(fae_port)

    # fw-default maps to disabled operationally
    effective_current = (
        PhyRecoveryConfig.DISABLED
        if current_mode == PhyRecoveryConfig.FW_DEFAULT
        else current_mode
    )
    effective_target = (
        PhyRecoveryConfig.DISABLED
        if mode == PhyRecoveryConfig.FW_DEFAULT
        else mode
    )

    if effective_current == effective_target:
        logger.info(f"Mode already set to {mode} (current: {current_mode}), skipping apply")
        with allure.step(f"Mode already {mode}, no change needed"):
            pass
        return False

    apply_mode(fae_port, mode)
    return True


def apply_timeout(fae_port, timeout: int) -> ResultObj:
    """
    Apply logic-relock-timeout to the selected port(s).

    Uses extended timeout for bulk operations (port ranges).

    Args:
        fae_port: Fae object (single port or port range)
        timeout: Timeout value (0-126)

    Returns:
        ResultObj(True) on success

    Raises:
        ValueError: If timeout is out of range
    """
    if not PhyRecoveryConfig.TIMEOUT_MIN <= timeout <= PhyRecoveryConfig.TIMEOUT_MAX:
        raise ValueError(
            f"Timeout {timeout} out of range "
            f"({PhyRecoveryConfig.TIMEOUT_MIN}-{PhyRecoveryConfig.TIMEOUT_MAX})"
        )

    _apply_config_with_bulk_support(
        fae_port,
        PhyRecoveryConfig.TIMEOUT,
        timeout,
        f"Apply {PhyRecoveryConfig.TIMEOUT}={timeout}"
    )
    return ResultObj(True)


def verify_config(selected_port, mode: str, timeout: Optional[int] = None):
    """
    Verify mode and timeout are applied to the selected port with retries.

    Args:
        selected_port: Fae object for a SINGLE port
        mode: Expected mode
        timeout: Expected timeout (optional)
    """
    retry_call(
        validate_mode_set,
        [selected_port, mode, timeout],
        exceptions=AssertionError,
        tries=PhyRecoveryTestConsts.VALIDATE_RETRIES,
        delay=PhyRecoveryTestConsts.VALIDATE_RETRY_DELAY_SECONDS,
    )


def unset_config(fae_port) -> ResultObj:
    """
    Unset PHY recovery configuration to restore defaults.

    Args:
        fae_port: Fae object (single port or port range)

    Returns:
        ResultObj(True) on success
    """
    _apply_config_with_bulk_support(
        fae_port,
        None,
        None,
        "Unset PHY recovery configuration"
    )
    return ResultObj(True)


def cleanup_phy_recovery_config(fae_port):
    """
    Reusable cleanup helper to unset PHY recovery config and verify defaults.

    Performs:
    1. Unsets the PHY recovery configuration
    2. Verifies the configuration was restored to defaults with retries

    Args:
        fae_port: Fae object (single port or port range)
    """
    with allure.step("Cleanup: Unset configuration to return to defaults"):
        fae_port.port.interface.link.phy_recovery.unset(
            apply=True,
            ask_for_confirmation=True
        )

    with allure.step("Verify configuration restored to defaults"):
        retry_call(
            validate_default_config,
            [fae_port],
            exceptions=AssertionError,
            tries=PhyRecoveryTestConsts.VALIDATE_RETRIES,
            delay=PhyRecoveryTestConsts.VALIDATE_RETRY_DELAY_SECONDS
        )


# =============================================================================
# Multi-Port-Group Helpers
# =============================================================================

def _apply_config_to_all_port_groups(all_port_groups: List[str], func, *args, **kwargs):
    """
    Apply a configuration function to each port group independently.

    Args:
        all_port_groups: List of port range strings (one per prefix group)
        func: Configuration function to call (e.g., apply_mode, unset_config)
        *args: Positional arguments forwarded to *func*
        **kwargs: Keyword arguments forwarded to *func*
    """
    for port_range in all_port_groups:
        fae = Fae(port_name=port_range)
        func(fae, *args, **kwargs)


def apply_mode_to_all(all_port_groups: List[str], mode: str) -> ResultObj:
    """Apply logic-relock-mode to every port group."""
    with allure.step(f"Apply {PhyRecoveryConfig.MODE}={mode} to all port groups"):
        _apply_config_to_all_port_groups(all_port_groups, apply_mode, mode)
    return ResultObj(True)


def apply_timeout_to_all(all_port_groups: List[str], timeout: int) -> ResultObj:
    """Apply logic-relock-timeout to every port group."""
    with allure.step(f"Apply {PhyRecoveryConfig.TIMEOUT}={timeout} to all port groups"):
        _apply_config_to_all_port_groups(all_port_groups, apply_timeout, timeout)
    return ResultObj(True)


def unset_config_all(all_port_groups: List[str]) -> ResultObj:
    """Unset PHY recovery configuration on every port group."""
    with allure.step("Unset PHY recovery config on all port groups"):
        _apply_config_to_all_port_groups(all_port_groups, unset_config)
    return ResultObj(True)


# =============================================================================
# Counter Helpers
# =============================================================================

def get_phy_recovery_counters(port_name: str) -> Dict[str, int]:
    """
    Retrieve PHY recovery counters from ``nv show interface <port> link phy-detail``.

    Uses a standalone ``Port`` object (not via ``Fae``) because ``phy-detail``
    is only available under the non-fae interface path.

    Args:
        port_name: Port name (e.g., ``sw2p2s1``)

    Returns:
        Dictionary of counter name to integer value.
    """
    with allure.step(f"Get PHY recovery counters for {port_name}"):
        port = Port(name=port_name)
        output = OutputParsingTool.parse_json_str_to_dictionary(
            port.interface.link.phy.detail.show()
        ).get_returned_value()

        counters = {
            PhyRecoveryCounters.TOTAL_SUCCESSFUL_RECOVERY_EVENTS:
                int(output.get(PhyRecoveryCounters.TOTAL_SUCCESSFUL_RECOVERY_EVENTS, 0)),
            PhyRecoveryCounters.SUCCESSFUL_RECOVERY_EVENTS:
                int(output.get(PhyRecoveryCounters.SUCCESSFUL_RECOVERY_EVENTS, 0)),
        }
        logger.info(f"PHY recovery counters for {port_name}: {counters}")
        return counters


def wait_for_recovery_counters_update(
    port_name: str,
    initial_total_count: int,
    initial_successful_count: int,
    tries: int = PhyRecoveryTestConsts.VALIDATE_RETRIES,
    delay: int = PhyRecoveryTestConsts.VALIDATE_RETRY_DELAY_SECONDS,
) -> Dict[str, int]:
    """
    Poll until at least one recovery counter increases beyond its initial value.

    Passes when **either** ``total-successful-recovery-events`` or
    ``successful-recovery-events`` has increased.

    Args:
        port_name: Port name to check
        initial_total_count: Initial ``total-successful-recovery-events``
        initial_successful_count: Initial ``successful-recovery-events``
        tries: Number of polling attempts
        delay: Seconds between attempts

    Returns:
        Dictionary with current values of both counters.

    Raises:
        AssertionError: If neither counter increases within the retry window
    """
    def _check_any_counter_increased():
        counters = get_phy_recovery_counters(port_name)
        cur_total = counters[PhyRecoveryCounters.TOTAL_SUCCESSFUL_RECOVERY_EVENTS]
        cur_successful = counters[PhyRecoveryCounters.SUCCESSFUL_RECOVERY_EVENTS]

        assert cur_total > initial_total_count or cur_successful > initial_successful_count, (
            f"No recovery counter increased: "
            f"total-successful-recovery-events {initial_total_count} -> {cur_total}, "
            f"successful-recovery-events {initial_successful_count} -> {cur_successful}"
        )
        return counters

    with allure.step(f"Wait for recovery counter to increase on {port_name}"):
        return retry_call(
            _check_any_counter_increased,
            exceptions=AssertionError,
            tries=tries,
            delay=delay,
        )
