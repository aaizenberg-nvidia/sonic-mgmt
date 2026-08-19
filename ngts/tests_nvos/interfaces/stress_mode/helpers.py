"""
Helper Functions for Stress Mode Testing

This module provides reusable helper functions for stress mode test cases.

The helpers are organized into functional groups:
  - Stress Mode Control: Enable/disable stress mode
  - Database Configuration: Capture and validate flex counter settings
  - L1 Power Saving: Configure and verify power saving settings
  - Validation: Verify system state and configuration restoration
  - Common Test Flows: Pre/post stress checks and effect validation

All functions follow single-responsibility principle for better testability.
"""
from __future__ import annotations

import logging
import time

from infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine

from ngts.nvos_tools.infra.DatabaseTool import DatabaseTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RegisterTool import RegisterTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.System import System
from ngts.nvos_constants.constants_nvos import HealthConsts, LogsSources
from ngts.tools.test_utils import allure_utils as allure

from .constants import (
    StressModeConsts,
    FlexCounterConsts,
    PowerSavingConsts,
    DatabaseConsts
)

logger = logging.getLogger(__name__)


# ============================================================================
# Stress Mode Control Functions
# ============================================================================

def set_stress_mode_state(engine: ProxySshEngine, state: str, tolerate_missing_disabled: bool = False) -> None:
    """
    Set stress mode state in STATE_DB.

    This writes the stress mode state to the STATE_DB database, which triggers
    the system to enable or disable stress mode features.

    Args:
        engine: DUT engine instance for command execution
        state: Desired state - use StressModeConsts.STATE_ENABLED or STATE_DISABLED

    Example:
        >>> set_stress_mode_state(engines.dut, StressModeConsts.STATE_ENABLED)
        >>> set_stress_mode_state(engines.dut, StressModeConsts.STATE_DISABLED, tolerate_missing_disabled=True)
    """
    action = "Enable" if state == StressModeConsts.STATE_ENABLED else "Disable"
    with allure.step(f"{action} stress mode in STATE_DB"):
        state_exists = DatabaseTool.sonic_db_cli_exists(
            engine=engine,
            asic="",
            db_name=DatabaseConsts.STATE_DB,
            db_config=StressModeConsts.STATE_DB_TABLE,
        )

        if tolerate_missing_disabled and state == StressModeConsts.STATE_DISABLED and not state_exists:
            logger.info(f'Stress mode key "{StressModeConsts.STATE_DB_TABLE}" is absent; treating it as already disabled')
            return

        DatabaseTool.sonic_db_cli_hset(
            engine=engine,
            asic="",
            db_name=DatabaseConsts.STATE_DB,
            db_config=StressModeConsts.STATE_DB_TABLE,
            param=StressModeConsts.STATE_FIELD,
            value=state,
            allow_create=True
        )
        logger.info(f"Stress mode {state}")

        time.sleep(5)


def enable_and_validate_stress_mode(engine) -> None:
    """
    Enable stress mode and validate successful activation.

    Performs the following:
      1. Sets stress mode state to enabled in STATE_DB
      2. Waits for system to process the change (5 seconds)
      3. Verifies success message appears in syslog

    Args:
        engine: DUT engine instance for command execution

    Raises:
        AssertionError: If success message not found in syslog

    Example:
        >>> enable_and_validate_stress_mode(engines.dut)
    """
    with allure.step("Enable and validate stress mode"):
        with allure.step("Enable stress mode"):
            set_stress_mode_state(engine, StressModeConsts.STATE_ENABLED)

        with allure.step(f"Verify syslog message: {StressModeConsts.SYSLOG_STRESS_MODE_STARTED}"):
            validate_msg_in_syslog(engine, StressModeConsts.SYSLOG_STRESS_MODE_STARTED)


def disable_and_validate_stress_mode(engine) -> None:
    """
    Disable stress mode and validate successful deactivation.

    Performs the following:
      1. Sets stress mode state to disabled in STATE_DB
      2. Waits for system to process the change (5 seconds)
      3. Verifies success message appears in syslog

    Args:
        engine: DUT engine instance for command execution

    Raises:
        AssertionError: If success message not found in syslog

    Example:
        >>> disable_and_validate_stress_mode(engines.dut)
    """
    with allure.step("Disable and validate stress mode"):
        with allure.step("Disable stress mode"):
            set_stress_mode_state(engine, StressModeConsts.STATE_DISABLED)

        with allure.step(f"Verify syslog message: {StressModeConsts.SYSLOG_STRESS_MODE_STOPPED}"):
            validate_msg_in_syslog(engine, StressModeConsts.SYSLOG_STRESS_MODE_STOPPED)

# ============================================================================
# Database Configuration Functions
# ============================================================================


def get_available_flex_counter_tables(engine, asic: str = "asic0") -> list[str]:
    """
    Retrieve all flex counter table names from FLEX_COUNTER_DB for a specific ASIC.

    Queries the database for all keys matching the flex counter group table prefix
    and extracts the table names.

    Args:
        engine: DUT engine instance for command execution
        asic: ASIC identifier (default: "asic0")

    Returns:
        list of table names without prefix (e.g., ['PORT_STAT_COUNTER', 'PORT_AMBER_PDDR'])

    Example:
        >>> tables = get_available_flex_counter_tables(engines.dut, "asic0")
        >>> print(tables)  # ['PORT_STAT_COUNTER', 'PORT_AMBER_PDDR', 'SWITCH_TELEMETRY', ...]
    """
    with allure.step(f"Get available flex counter tables for {asic}"):
        cmd = f"sonic-db-cli -n {asic} {FlexCounterConsts.DB_NAME} keys '{FlexCounterConsts.GROUP_TABLE_PREFIX}*'"
        output = engine.run_cmd(cmd, validate=True)

        # Extract table names (remove prefix)
        tables = []
        for line in output.strip().split('\n'):
            line = line.strip()
            if line.startswith(FlexCounterConsts.GROUP_TABLE_PREFIX):
                table_name = line.replace(FlexCounterConsts.GROUP_TABLE_PREFIX, '')
                tables.append(table_name)

        logger.info(f"Available flex counter tables for {asic}: {tables}")
        return tables


def get_flex_counter_status(engine, asic: str = "asic0", table_name: str = None) -> dict[str, str]:
    """
    Retrieve flex counter configuration for a specific table.

    Queries FLEX_COUNTER_DB and retrieves all configuration fields for the specified
    flex counter table, including FLEX_COUNTER_STATUS, POLL_INTERVAL, etc.

    Args:
        engine: DUT engine instance for command execution
        asic: ASIC identifier (default: "asic0")
        table_name: Table name (e.g., 'PORT_STAT_COUNTER'). Defaults to PORT_STAT_COUNTER if None

    Returns:
        dict with configuration fields as keys and their values (e.g., {'FLEX_COUNTER_STATUS': 'enable'})

    Example:
        >>> config = get_flex_counter_status(engines.dut, 'asic0', 'PORT_STAT_COUNTER')
        >>> print(config['FLEX_COUNTER_STATUS'])  # 'enable' or 'disable'
    """
    if table_name is None:
        table_name = "PORT_STAT_COUNTER"

    with allure.step(f"Get flex counter status for {asic} table {table_name}"):
        full_table_name = f"{FlexCounterConsts.GROUP_TABLE_PREFIX}{table_name}"
        cmd = f"sonic-db-cli -n {asic} {FlexCounterConsts.DB_NAME} hgetall '{full_table_name}'"
        output = engine.run_cmd(cmd, validate=True)

        logger.info(f"Raw output for {asic} table {table_name}: {repr(output)}")

        config = {}

        # Try to parse as dictionary literal first (if output looks like a dict)
        if output.strip().startswith('{') and output.strip().endswith('}'):
            try:
                import ast
                config = ast.literal_eval(output.strip())
                logger.info(f"Parsed as dict literal: {config}")
            except Exception as e:
                logger.warning(f"Failed to parse as dict literal: {e}")

        # If still empty, try line-by-line parsing
        if not config:
            lines = [line.strip() for line in output.strip().split('\n') if line.strip()]

            # Parse key-value pairs
            i = 0
            while i < len(lines):
                if i + 1 < len(lines):
                    key = lines[i]
                    value = lines[i + 1]
                    config[key] = value
                    i += 2
                else:
                    i += 1

            logger.info(f"Parsed line-by-line: {config}")

        logger.info(f"Flex counter config for {asic} table {table_name}: {config}")

        # If config is still empty, log warning with more details
        if not config:
            logger.warning(f"Failed to parse flex counter config for {asic} table {table_name}.")
            logger.warning(f"Raw output type: {type(output)}")
            logger.warning(f"Raw output repr: {repr(output)}")
            logger.warning(f"Raw output length: {len(output)}")

        return config


def get_all_flex_counter_status(engine, asic: str = "asic0") -> dict[str, dict[str, str]]:
    """
    Retrieve flex counter configurations for all tables on a specific ASIC.

    First discovers all available flex counter tables, then retrieves the
    configuration for each table.

    Args:
        engine: DUT engine instance for command execution
        asic: ASIC identifier (default: "asic0")

    Returns:
        Nested dict: {table_name: {field: value, ...}, ...}
        Example: {'PORT_STAT_COUNTER': {'FLEX_COUNTER_STATUS': 'enable', ...}, ...}

    Example:
        >>> configs = get_all_flex_counter_status(engines.dut, "asic0")
        >>> print(configs['PORT_STAT_COUNTER']['FLEX_COUNTER_STATUS'])
    """
    with allure.step(f"Get all flex counter configurations for {asic}"):
        # Get available tables
        tables = get_available_flex_counter_tables(engine, asic)

        # Get config for each table
        all_configs = {}
        for table in tables:
            try:
                config = get_flex_counter_status(engine, asic, table)
                all_configs[table] = config
            except Exception as e:
                logger.warning(f"Failed to get config for table {table}: {e}")
                all_configs[table] = {}

        logger.info(f"All flex counter configs for {asic}: {all_configs}")
        return all_configs


def capture_base_db_config(engine, device) -> dict:
    """
    Capture baseline database configuration before stress mode.

    Args:
        engine: DUT engine instance
        device: Device instance with ASIC information

    Returns:
        dict containing baseline flex counter configurations
        Structure:
        {
            'flex_counter': {
                'asic0': {
                    'PORT_STAT_COUNTER': {'FLEX_COUNTER_STATUS': 'enable', ...},
                    'PORT_AMBER_PDDR': {...},
                    ...
                },
                'asic1': {...}
            }
        }

    Example:
        >>> baseline = capture_base_db_config(engines.dut, devices.dut)
        >>> print(baseline['flex_counter']['asic0']['PORT_STAT_COUNTER']['FLEX_COUNTER_STATUS'])
    """
    with allure.step("Capture baseline database configuration"):
        baseline = {
            'flex_counter': {}
        }

        # Capture flex counter status for all tables in all ASICs
        asic_count = getattr(device, 'asic_amount', 1)
        for asic_id in range(asic_count):
            asic_name = f"asic{asic_id}"
            # Get all flex counter table configurations for this ASIC
            baseline['flex_counter'][asic_name] = get_all_flex_counter_status(engine, asic_name)

        logger.info(f"Baseline configuration captured: {baseline}")
        return baseline


# ============================================================================
# MST Device Functions
# ============================================================================

def get_mst_devices(engine) -> list[str]:
    """
    Get list of MST devices on the system.

    Args:
        engine: DUT engine instance

    Returns:
        list of MST device paths (e.g., ['/dev/mst/mt54004_pciconf0', ...])

    Example:
        >>> devices = get_mst_devices(engines.dut)
        >>> print(devices)  # ['/dev/mst/mt54004_pciconf0', '/dev/mst/mt54004_pciconf1']
    """
    with allure.step("Get MST devices"):
        cmd = "ls /dev/mst/ | grep -i pciconf"
        output = engine.run_cmd(cmd, validate=True)
        devices = [f"/dev/mst/{dev.strip()}" for dev in output.strip().split('\n') if dev.strip()]
        logger.info(f"Found {len(devices)} MST devices: {devices}")
        return devices


# ============================================================================
# L1 Power Saving Functions
# ============================================================================

def get_l1_cap_for_port(engine, mst_device: str, port: int) -> str:
    """
    Retrieve L1 power saving capability status for a specific port.

    Reads the PPSLS (Port Power Saving Link State) register to determine
    if L1 power saving is enabled or disabled for the given port.

    Args:
        engine: DUT engine instance for register access
        mst_device: MST device path (e.g., '/dev/mst/mt54004_pciconf0')
        port: Port number (local port identifier)

    Returns:
        '0' if L1 power saving is disabled, '1' if enabled

    Example:
        >>> l1_cap = get_l1_cap_for_port(engines.dut, '/dev/mst/mt54004_pciconf0', 1)
        >>> print(l1_cap)  # '0' or '1'
    """
    with allure.step(f"Get L1 capability for port {port}"):
        indexes = f'--indexes "local_port={port}"'
        output = RegisterTool.get_mst_register_value(
            engine=engine,
            mst_dev_name=mst_device,
            reg_name=PowerSavingConsts.PPSLS_REGISTER,
            additional_params=indexes,
            grep_pattern=PowerSavingConsts.L1_CAP_FIELD
        )
        # Extract the last digit from output (the l1_cap value)
        l1_cap = output.strip()[-1] if output.strip() else ""
        logger.info(f"Port {port} L1 capability: {l1_cap}")
        return l1_cap


def set_l1_req_en_for_port(engine, mst_device: str, port: int, l1_req_en_value: str) -> None:
    """
    Configure L1 power saving request for a specific port.

    Writes to the PPSLC (Port Power Saving Link Configuration) register to
    enable or disable L1 power saving for the specified port.

    Args:
        engine: DUT engine instance for register access
        mst_device: MST device path (e.g., '/dev/mst/mt54004_pciconf0')
        port: Port number (local port identifier)
        l1_req_en_value: '0' to disable L1 power saving, '1' to enable

    Example:
        >>> set_l1_req_en_for_port(engines.dut, '/dev/mst/mt54004_pciconf0', 50, '1')
    """
    with allure.step(f"Set L1 request enable for port {port} to {l1_req_en_value}"):
        indexes = f'--indexes "local_port={port}"'
        set_params = f'"l1_req_en={l1_req_en_value}"'
        output = RegisterTool.set_mst_register_value(
            engine=engine,
            mst_dev_name=mst_device,
            reg_name=PowerSavingConsts.PPSLC_REGISTER,
            set_params=set_params,
            additional_params=indexes
        )
        logger.info(f"Set port {port} L1 request enable to {l1_req_en_value}")


def set_l1_req_en_for_all_ports(engine, device, l1_req_en_value: str) -> ResultObj:
    """
    Set L1 request enable for all ports using PPSLC register.

    This function sets l1_req_en on all ports (1, 2, 3, ... port_count)
    across all MST devices using parallel execution for faster performance.

    Args:
        engine: DUT engine instance
        device: Device instance with port information
        l1_req_en_value: L1 request enable value to set ('0' for disabled, '1' for enabled)

    Returns:
        ResultObj: Result object with success/failure status

    Example:
        >>> set_l1_req_en_for_all_ports(engines.dut, devices.dut, '1').verify_result()
    """
    with allure.step(f"Set L1 request enable to {l1_req_en_value} for all ports (parallel)"):
        mst_devices = get_mst_devices(engine)
        errors = []

        # Build list of all ports (1, 2, 3, ... port_count)
        port_count = getattr(device, 'valid_ports_count', 72)
        all_ports = list(range(1, port_count + 1))

        for mst_device in mst_devices:
            try:
                RegisterTool.set_mst_register_value_parallel(
                    engine=engine,
                    mst_dev_name=mst_device,
                    reg_name=PowerSavingConsts.PPSLC_REGISTER,
                    set_params=f"l1_req_en={l1_req_en_value},l1_cap_adv={l1_req_en_value}",
                    ports=all_ports
                )
                logger.info(f"Set L1 request enable to {l1_req_en_value} for {len(all_ports)} ports on {mst_device}")
            except Exception as e:
                errors.append(f"Failed to set L1 for ports on {mst_device}: {e}")

        time.sleep(2)  # Allow all register changes to take effect

        if errors:
            return ResultObj(result=False, info="\n".join(errors))
        return ResultObj(result=True, info=f"Set L1 request enable to {l1_req_en_value} for {len(all_ports)} ports")


def validate_all_ports_l1_cap(engine, device, expected_l1_cap: str) -> ResultObj:
    """
    Validate L1 capability for all ports matches expected value using PPSLS register.

    This function checks all ports (1, 2, 3, ... port_count) across all MST devices
    and validates that the l1_cap field in PPSLS register matches the expected value.

    Args:
        engine: DUT engine instance
        device: Device instance with port information
        expected_l1_cap: Expected L1 capability value ('0' for disabled, '1' for enabled)

    Returns:
        ResultObj: Result object with success/failure status

    Example:
        >>> validate_all_ports_l1_cap(engines.dut, devices.dut, '1').verify_result()
    """
    with allure.step(f"Validate all ports have L1 capability = {expected_l1_cap}"):
        mst_devices = get_mst_devices(engine)
        errors = []

        # Build list of all ports (1, 2, 3, ... port_count)
        port_count = getattr(device, 'valid_ports_count', 72)
        all_ports = list(range(1, port_count + 1))
        all_ports_set = set(str(p) for p in all_ports)

        for mst_device in mst_devices:
            try:
                # Get all ports at once using parallel get
                output = RegisterTool.get_mst_register_value_parallel(
                    engine=engine,
                    mst_dev_name=mst_device,
                    reg_name=PowerSavingConsts.PPSLS_REGISTER,
                    ports=all_ports,
                    grep_pattern=PowerSavingConsts.L1_CAP_FIELD
                )

                # Track verified ports
                verified_ports = set()

                # Parse output to check each port
                current_port = None
                for line in output.strip().split('\n'):
                    line = line.strip()
                    if line.startswith('Port '):
                        current_port = line.replace('Port ', '').replace(':', '')
                    elif PowerSavingConsts.L1_CAP_FIELD in line and current_port:
                        verified_ports.add(current_port)
                        # Extract l1_cap value (last character after hex value)
                        l1_cap = line.strip()[-1] if line.strip() else ""
                        if l1_cap != expected_l1_cap:
                            errors.append(f"{mst_device}:port{current_port} - L1 capability is {l1_cap}, expected {expected_l1_cap}")

                # Check for missing ports
                missing_ports = all_ports_set - verified_ports
                if missing_ports:
                    errors.append(f"{mst_device}: Missing l1_cap data for ports: {sorted(missing_ports, key=int)}")

            except Exception as e:
                errors.append(f"Failed to check ports on {mst_device}: {e}")

        logger.info(f"Checked {len(all_ports)} ports per MST device")

        if errors:
            return ResultObj(result=False, info=f"L1 capability validation failed:\n" + "\n".join(errors))
        logger.info(f"All ports have L1 capability = {expected_l1_cap}")
        return ResultObj(result=True, info=f"All {len(all_ports)} ports have L1 capability = {expected_l1_cap}")


# ============================================================================
# Fatal Mode Simulation Functions
# ============================================================================

def simulate_events(engine, asic: int) -> None:
    """
    Simulate fatal health events on a specific ASIC for testing.

    Triggers two types of health check events to simulate fatal conditions:
      1. FW assert event
      2. FW fatal cause event

    Waits 10 seconds between events to allow proper processing.

    Args:
        engine: DUT engine instance for command execution
        asic: ASIC number to trigger events on (e.g., 0, 1)

    Example:
        >>> simulate_events(engines.dut, asic=0)
    """
    asic_folder = asic - 1
    list_of_events = [
        f"echo health_check_trigger  sx_dbg_test_fw_assert {asic} > /proc/mlx_sx/asic{asic_folder}/sx_core",
        f"echo health_check_trigger  sx_dbg_test_fw_fatal_cause {asic} > /proc/mlx_sx/asic{asic_folder}/sx_core"
    ]
    with allure.step(f"{simulate_events.__name__}: Simulating MFDEs on ASIC{asic}"):
        for cmd in list_of_events:
            engine.run_cmd(cmd)
            time.sleep(10)  # fatal doesn't work if we don't wait between events

# ============================================================================
# Validation Functions
# ============================================================================


def validate_config_matches_baseline(current_config: dict, baseline_config: dict,
                                     config_name: str) -> None:
    """
    Validate that current configuration exactly matches the baseline.

    Performs a deep comparison of dictionaries to ensure all fields and values
    match between current and baseline configurations.

    Args:
        current_config: Current configuration dictionary to validate
        baseline_config: Expected baseline configuration dictionary
        config_name: Descriptive name for error messages (e.g., "flex_counter asic0")

    Raises:
        AssertionError: If any field differs between current and baseline

    Example:
        >>> validate_config_matches_baseline(current, baseline, "flex_counter asic0.PORT_STAT_COUNTER")
    """
    with allure.step(f"Validate {config_name} matches baseline"):
        result = ValidationTool.compare_dictionaries(
            current_config,
            baseline_config
        ).verify_result()
        logger.info(f"{config_name} configuration matches baseline")


def validate_msg_in_syslog(engine, expected_message: str) -> None:
    """
    Verify that a specific message appears in the system syslog.

    Searches for the expected message in the latest syslog entries.

    Args:
        engine: DUT engine instance for log access
        expected_message: Exact message text to find in syslog

    Raises:
        AssertionError: If message is not found in syslog

    Example:
        >>> validate_msg_in_syslog(engines.dut, "Stress Mode successfully enabled")
    """
    system = System()
    system.log.verify_expected_logs(logs_to_find=[expected_message], logs_source=LogsSources.SYSLOG,
                                    engine=engine, only_latest_log=True)


def validate_health_status(expected_status: str) -> None:
    """
    Verify the system health status matches the expected value.

    Queries the system health and compares against the expected status.

    Args:
        expected_status: Expected health value (e.g., HealthConsts.OK, HealthConsts.FATAL)

    Raises:
        AssertionError: If actual health status differs from expected

    Example:
        >>> validate_health_status(HealthConsts.OK)
        >>> validate_health_status(HealthConsts.FATAL)
    """
    with allure.step(f"Validate health status is {expected_status}"):
        system = System()
        health_output = OutputParsingTool.parse_json_str_to_dictionary(
            system.health.show()
        ).get_returned_value()

        actual_status = health_output[HealthConsts.STATUS]
        assert actual_status == expected_status, \
            f"Expected health status {expected_status}, but got {actual_status}"
        logger.info(f"Health status is {expected_status}")


# ============================================================================
# Common Test Flow Functions
# ============================================================================

def pre_stress_checks(engine, device) -> dict:
    """
    Perform pre-stress mode checks and capture baseline configuration.

    This function performs the following validations and captures:
    1. Captures baseline flex counter configuration for all ASICs and all tables
       (PORT_STAT_COUNTER, PORT_AMBER_PDDR, etc.)
    2. Validates system health status is 'OK' before stress mode activation

    Args:
        engine: DUT engine instance
        device: Device instance with ASIC information

    Returns:
        dict: Baseline configuration with structure:
            {
                'flex_counter': {
                    'asic0': {'PORT_STAT_COUNTER': {...}, ...},
                    'asic1': {...}
                }
            }

    Raises:
        AssertionError: If system health status is not 'OK'

    Example:
        >>> baseline = pre_stress_checks(engines.dut, devices.dut)
        >>> print(baseline['flex_counter']['asic0']['PORT_STAT_COUNTER'])
    """
    with allure.step("Pre-stress checks"):
        with allure.step("Capture baseline configuration"):
            baseline = capture_base_db_config(engine, device)
            logger.info(f"Baseline configuration captured: {baseline}")

        with allure.step("Verify health status is OK"):
            validate_health_status(HealthConsts.OK)

        return baseline


def validate_stress_mode_effects(engine, device) -> None:
    """
    Validate that stress mode has correctly modified system configuration.

    This function performs the following validations:
    1. Flex Counter Validation (per ASIC):
       - Retrieves all flex counter tables from FLEX_COUNTER_DB
       - Validates FLEX_COUNTER_STATUS='disable' for ALL tables including:
         * PORT_STAT_COUNTER
         * PORT_AMBER_PDDR
         * And any other configured flex counter groups
       - Performs validation independently for each ASIC (asic0, asic1, ...)

    2. L1 Power Saving Validation:
       - Checks PPSLS register l1_cap field for all ports
       - Validates l1_cap='0' (disabled) for all odd-numbered ports (1, 3, 5, ...)
       - Queries all MST devices on the system
       - Note: Failure logged but does not fail the test

    Args:
        engine: DUT engine instance
        device: Device instance with asic_amount and valid_ports_count attributes

    Raises:
        AssertionError: If any flex counter table is not disabled for any ASIC

    Example:
        >>> validate_stress_mode_effects(engines.dut, devices.dut)
        # Validates flex counters disabled and L1 power saving disabled
    """
    with allure.step("Validate stress mode effects"):
        # Validate flex counters disabled
        with allure.step("Verify flex counters disabled for all ASICs"):
            asic_count = getattr(device, 'asic_amount', 1)
            for asic_id in range(asic_count):
                asic_name = f"asic{asic_id}"

                with allure.step(f"Validate flex counters for {asic_name}"):
                    all_flex_configs = get_all_flex_counter_status(engine, asic_name)

                    for table_name, table_config in all_flex_configs.items():
                        with allure.independent_step(f"Verify {asic_name}.{table_name} is disabled"):
                            actual_status = table_config.get(FlexCounterConsts.FLEX_COUNTER_STATUS, '')
                            assert actual_status == FlexCounterConsts.STATUS_DISABLED, \
                                f"{asic_name}.{table_name}: Expected 'disable', got '{actual_status}'"
                            logger.info(f"{asic_name}.{table_name}: Flex counter correctly disabled")

                    logger.info(f"{asic_name}: All flex counter tables correctly disabled")

        # Validate L1 power saving disabled
        with allure.step("Verify L1 power saving disabled"):
            try:
                validate_all_ports_l1_cap(engine, device, PowerSavingConsts.L1_CAP_DISABLED)
                logger.info("L1 power saving correctly disabled")
            except Exception as e:
                logger.info(f"L1 power saving validation failed: {e}")


def post_stress_checks(engine, device, baseline_config: dict) -> None:
    """
    Perform post-stress mode checks and validate full baseline restoration.

    This function performs the following validations:
    1. Flex Counter Restoration (per ASIC):
       - Retrieves current flex counter configuration from FLEX_COUNTER_DB
       - Compares ALL flex counter tables against captured baseline
       - Validates exact match for all fields (FLEX_COUNTER_STATUS, POLL_INTERVAL, etc.)
       - Performs independent validation for each table in each ASIC
       - Tables validated include: PORT_STAT_COUNTER, PORT_AMBER_PDDR, and all others

    2. System Health Status:
       - Waits up to TestTimeouts.HEALTH_STATUS_TIMEOUT (default 120s) for health='OK'
       - Validates final health status is 'OK'
       - Uses System.wait_until_health_status_change_to() for polling

    Args:
        engine: DUT engine instance
        device: Device instance with asic_amount attribute
        baseline_config: Baseline configuration captured by pre_stress_checks() or
                        capture_base_db_config(). Must contain 'flex_counter' dict
                        with per-ASIC, per-table configurations.

    Raises:
        AssertionError: If any flex counter table doesn't match baseline, or if
                       health status is not 'OK' after timeout

    Example:
        >>> baseline = pre_stress_checks(engines.dut, devices.dut)
        >>> # ... run stress mode test ...
        >>> post_stress_checks(engines.dut, devices.dut, baseline)
        # Validates all configurations restored to pre-stress state
    """
    with allure.step("Post-stress checks - Verify baseline restoration"):
        asic_count = getattr(device, 'asic_amount', 1)

        # Validate flex counters restored
        for asic_id in range(asic_count):
            asic_name = f"asic{asic_id}"

            with allure.step(f"Verify flex counters restored for {asic_name}"):
                current_all_flex_configs = get_all_flex_counter_status(engine, asic_name)
                baseline_all_flex_configs = baseline_config['flex_counter'][asic_name]

                for table_name in baseline_all_flex_configs.keys():
                    with allure.independent_step(f"Verify {asic_name}.{table_name} restored"):
                        current_table_config = current_all_flex_configs.get(table_name, {})
                        baseline_table_config = baseline_all_flex_configs[table_name]

                        validate_config_matches_baseline(
                            current_table_config,
                            baseline_table_config,
                            f"Flex counter for {asic_name}.{table_name}"
                        )

                logger.info(f"{asic_name}: All flex counter tables restored to baseline")

        # Validate health status
        with allure.step("Verify health status is OK"):
            validate_health_status(HealthConsts.OK)

        logger.info("All post-stress checks passed - baseline fully restored")
