import logging
import time
from contextlib import contextmanager

from retry import retry
from typing import Dict

from ngts.nvos_tools.infra import ExceptionTool
from ngts.nvos_tools.infra.FilesTool import EngineFile, TempFileOnEngine, FilesTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tools.test_utils import allure_utils as allure
from retry.api import retry_call
import pytest
import random
import re
import math
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.Simulator import HWSimulator
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_constants.constants_nvos import ActionConsts
from ngts.nvos_constants.constants_nvos import SystemConsts, HealthConsts, NvosConst, PlatformConsts, FansConsts, \
    Cpov2Consts
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
from ngts.nvos_tools.infra.DatabaseTool import DatabaseTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_constants.constants_nvos import DatabaseConst
from ngts.tests_nvos.platform.test_platform_environment_leakage import rewrite_files


cumulus_owner = "yleibovichwe"


logger = logging.getLogger()


OK = HealthConsts.OK
NOT_OK = HealthConsts.NOT_OK
IGNORED = HealthConsts.IGNORED
USER_DEFINED_CHECKERS_KEY = 'user_defined_checkers'
DEVICES_TO_IGNORE_KEY = "devices_to_ignore"
SIMULATED_ISSUES = {"bad_device": "device is out of power"}

# Multi-instance component validation: component -> (naming_pattern, device_spec_attr)
MULTI_INSTANCE_COMPONENTS = {
    HealthConsts.Component.ASIC: (r'ASIC\d+', 'asic_amount'),
    HealthConsts.Component.Leakage_Sensor: (r'LEAKAGE-\d+', 'leakage_sensors_count'),
    HealthConsts.Component.FAN: (r'FAN\d+(/\d+)?', 'fan_list'),  # Supports FAN1 or FAN1/1 format
    HealthConsts.Component.PSU: (r'PSU\d+', 'psu_list'),
    # Port-style (swA14p1) and module-style (fnm1) instance IDs both appear in health output
    HealthConsts.Component.Transceiver: (r'(?:[A-Za-z]+\d+p\d+|[A-Za-z]+\d+)', 'transceiver_list'),
    # Gen2 CPO (Portia): only validated when the DUT reports these components and
    # device.cpo_list / device.laser_source_list are populated (Gen2 CPO devices only).
    HealthConsts.Component.CPO: (Cpov2Consts.CPO_INSTANCE_REGEX, 'cpo_list'),
    HealthConsts.Component.Laser_Source: (Cpov2Consts.ELS_INSTANCE_REGEX, 'laser_source_list'),
}

# Single-instance components (only have "ALL" instance)
SINGLE_INSTANCE_COMPONENTS = [
    HealthConsts.Component.CPU,
    HealthConsts.Component.Switch,
    HealthConsts.Component.Software
]

# ASIC health simulation via STATE_DB (thermalctld stopped so value is not overwritten)
_HEALTH_POLL_WAIT_SEC = 70
_ASIC_TEMP_UNHEALTHY = "999"
_ASIC_TEMP_HEALTHY = "30"
_STATE_DB_TEMPERATURE_PREFIX = "TEMPERATURE_INFO|"
# ASIC_HEALTH fatal_state: when set to 'true', all ASIC instances are marked unhealthy (new image logic)
_STATE_DB_ASIC_HEALTH_STATE = "ASIC_HEALTH|STATE"
_ASIC_HEALTH_FATAL_FIELD = "fatal_state"
_FATAL_PROMPT = "[System_Fatal_State]"


def _inject_asic_temperature(engines, instance_id, temp_value):
    """Set STATE_DB TEMPERATURE_INFO|{instance_id} temperature to temp_value (e.g. ASIC1, 999 or 30)."""
    table_key = f"{_STATE_DB_TEMPERATURE_PREFIX}{instance_id}"
    DatabaseTool.sonic_db_cli_hset(
        engines.dut, "", DatabaseConst.STATE_DB_NAME, table_key, "temperature", temp_value)


def _wait_health_cycle():
    time.sleep(_HEALTH_POLL_WAIT_SEC)


def _assert_component_instance(system, component, instance_id, field_name, field_value, tries=5, delay=5):
    retry_call(validate_component_health_data_for_instance,
               [system, component, instance_id, field_name, field_value],
               exceptions=AssertionError, tries=tries, delay=delay)


def _assert_asic_instance(system, instance_id, field_name, field_value, tries=3, delay=10):
    _assert_component_instance(system, HealthConsts.Component.ASIC, instance_id, field_name, field_value, tries, delay)


def _get_asic_instance_count(system, instance_id):
    health_out = OutputParsingTool.parse_json_str_to_dictionary(system.health.component.show()).get_returned_value()
    return int(health_out[HealthConsts.Component.ASIC][HealthConsts.Component.INSTANCE][instance_id][
        HealthConsts.Component.UNHEALTHY_COUNT])


def _set_asic_health_fatal_state(engines, value, fire_and_forget=False, timeout_sec=10):
    """Set STATE_DB ASIC_HEALTH|STATE fatal_state to 'true' or 'false'.

    Default behavior is a normal (blocking) ``sonic-db-cli hset`` on
    ``engines.dut``.

    When ``fire_and_forget=True``, the hset is issued via
    ``DutUtilsTool.run_cmd_with_disconnect`` with ``timeout_sec``. This is
    used for the recovery write (setting back to 'false') because after
    fatal_state is asserted the SSH prompt changes to
    ``[System_Fatal_State]...$`` which netmiko does not recognize, so it hangs
    waiting for the original prompt even though the command actually ran. On
    timeout the stale channel is torn down and the next call on
    ``engines.dut`` reconnects automatically.
    """
    if fire_and_forget:
        cmd = (f'sonic-db-cli {DatabaseConst.STATE_DB_NAME} hset '
               f'"{_STATE_DB_ASIC_HEALTH_STATE}" "{_ASIC_HEALTH_FATAL_FIELD}" "{value}"')
        logger.info(f"Setting ASIC_HEALTH fatal_state={value} (fire-and-forget, "
                    f"timeout={timeout_sec}s): {cmd}")
        DutUtilsTool.run_cmd_with_disconnect(engines.dut, cmd, timeout=timeout_sec)
        return
    DatabaseTool.sonic_db_cli_hset(
        engines.dut, "", DatabaseConst.STATE_DB_NAME,
        _STATE_DB_ASIC_HEALTH_STATE, _ASIC_HEALTH_FATAL_FIELD, value)


def _reset_fatal_prompt(engines):
    """Strip [System_Fatal_State] from netmiko base_prompt so the engine recognizes the normal prompt after recovery."""
    engines.dut.engine.base_prompt = engines.dut.engine.base_prompt.replace(_FATAL_PROMPT, '')


def _build_validation_config(available_components, devices):
    """
    Build validation config based on components actually present in API response
    and device hardware specifications.

    Args:
        available_components: List of component names from API response
        devices: Device fixture with hardware specs

    Returns:
        Dictionary with validation config for multi-instance components
    """
    validation_config = {}

    # Check each multi-instance component
    for component, (pattern, spec_attr) in MULTI_INSTANCE_COMPONENTS.items():
        # Only add if component is in API response
        if component in available_components:
            device_spec = getattr(devices.dut, spec_attr, [])

            # Check if device actually has this hardware
            has_hardware = False
            if isinstance(device_spec, list):
                has_hardware = len(device_spec) > 0
            elif isinstance(device_spec, int):
                has_hardware = device_spec > 0

            if has_hardware:
                validation_config[component] = (pattern, spec_attr)
                logger.info(f"Added validation for {component}: pattern={pattern}, spec={spec_attr}")

    return validation_config


def _validate_instance_fields_and_values(component, instance_id, instance_data):
    """Validate instance fields have correct formats and values"""
    ValidationTool.verify_field_exist_in_json_output(
        instance_data, [HealthConsts.Component.LAST_HEALTHY, HealthConsts.Component.STATE,
                        HealthConsts.Component.UNHEALTHY_COUNT]).verify_result()

    state = instance_data[HealthConsts.Component.STATE]
    assert state in [HealthConsts.Component.HEALTHY, HealthConsts.Component.UNHEALTHY], \
        f"{component}/{instance_id} invalid state: '{state}'"

    count = instance_data[HealthConsts.Component.UNHEALTHY_COUNT]
    assert isinstance(count, str) and count.isdigit() and int(count) >= 0, \
        f"{component}/{instance_id} invalid unhealthy-count: '{count}'"

    timestamp = instance_data[HealthConsts.Component.LAST_HEALTHY]
    if timestamp:
        assert re.match(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}', timestamp), \
            f"{component}/{instance_id} invalid timestamp: '{timestamp}'"


def _validate_instance_naming_and_count(component, instances, devices, validation_config):
    """
    Validate instance naming patterns and count against device specs

    Args:
        component: Component name
        instances: Dictionary of instances
        devices: Device fixture with hardware specs
        validation_config: Validation configuration dictionary
    """
    instance_ids = list(instances.keys())

    # Multi-instance components with naming patterns
    if component in validation_config:
        pattern, spec_attr = validation_config[component]
        for inst_id in instance_ids:
            assert re.match(pattern, inst_id), f"Invalid {component} instance name: {inst_id}"

        # Get expected count from device specs (not used for transceivers)
        expected = getattr(devices.dut, spec_attr, 0)

        # For list attributes (fan_list, psu_list, transceiver_list), use len()
        if isinstance(expected, list):
            expected = len(expected)

        # Validate count if expected is non-zero (or for transceivers: just that instances > 0)
        if expected > 0:
            if component == HealthConsts.Component.Transceiver:
                # Transceivers: do not rely on ibdevice count; only check key is present and instances > 0
                assert len(instances) > 0, \
                    f"{component} has no instances in health output"
                logger.info(f"{component} validated: key present, instance count {len(instances)} > 0")
            else:
                # For other components (ASIC, Fan, PSU): exact count match
                assert len(instances) == expected, \
                    f"{component} count mismatch: expected {expected}, found {len(instances)}"
                logger.info(f"{component} count validated: {len(instances)} matches device spec")

    # Single-instance components must have "ALL"
    elif component in SINGLE_INSTANCE_COMPONENTS:
        assert len(instances) == 1 and "ALL" in instances, \
            f"{component} should have exactly one 'ALL' instance, found {instance_ids}"


@pytest.fixture(scope='function')
def set_unset_ps_redundancy_ps():
    """Set ps-redundancy to PS before test and unset after. No-op on ETH (ps-redundancy not required)."""
    if TestToolkit.devices.dut.is_eth():
        yield
        return

    platform = Platform()
    with allure.step(f"Set platform ps-redundancy to {PlatformConsts.PS_REDUNDANCY_PS}"):
        platform.ps_redundancy.set(PlatformConsts.PS_REDUNDANCY_POLICY, PlatformConsts.PS_REDUNDANCY_PS, apply=True)

    yield

    with allure.step('Run unset platform ps-redundancy command and apply'):
        platform.ps_redundancy.unset(apply=True).verify_result()


@pytest.fixture(scope='function')
def reset_health_service(engines):
    yield
    HWSimulator.reset_health_service(engines.dut)


@pytest.fixture(scope='function')
def validate_health_history():
    system = System()
    if TestToolkit.devices.dut.is_eth():
        verify_health_status_and_led(system, OK)
    else:
        system.health.history.retry_get_health_history_file_summary_line()


@pytest.mark.system
@pytest.mark.health
def test_reboot_test(validate_health_history, verify_no_kernel_errors, engines):
    """
    Validate health after reboot :
    - status is OK
    - same health file as before the reboot
    - relevant reboot line appears in the health file
    - new summary line after the reboot in the health file
    """

    system = System()

    system.validate_health_status(OK)
    last_status_line = system.health.history.search_line(HealthConsts.SUMMARY_REGEX_OK)[-1]
    expected_led = HealthConsts.LED_OK_STATUS if TestToolkit.devices.dut.is_eth() else None

    with allure.step('Reboot the system'):
        system.reboot.action_reboot()

    start_time = time.time()
    system.health.wait_until_health_status_change_after_reboot(
        OK, expected_led=expected_led)
    end_time = time.time()
    duration = end_time - start_time

    with allure.step("Took {} seconds until health status changed to OK after reboot".format(duration)):
        logger.info("Took {} seconds until health status changed to OK after reboot".format(duration))

    with allure.step(f"Validate agetty process uses only {SystemConsts.SERIAL_CONSOLE_BAUD_RATE} baud rate"):
        logger.info(f"Validate agetty process uses only {SystemConsts.SERIAL_CONSOLE_BAUD_RATE} baud rate")
        agetty_output = engines.dut.run_cmd("ps aux | grep '[a]getty'")
        agetty_lines = [line for line in agetty_output.splitlines() if "agetty" in line]
        target_lines = [line for line in agetty_lines if "ttyS" in line] or agetty_lines
        invalid_lines = [line for line in target_lines if SystemConsts.SERIAL_CONSOLE_BAUD_RATE not in line or f"{SystemConsts.SERIAL_CONSOLE_BAUD_RATE}," in line]
        valid_lines = [line for line in target_lines if SystemConsts.SERIAL_CONSOLE_BAUD_RATE in line and f"{SystemConsts.SERIAL_CONSOLE_BAUD_RATE}," not in line]

        assert (
            target_lines and
            valid_lines and
            not invalid_lines
        ), (
            f"Expected all relevant agetty processes to advertise a single {SystemConsts.SERIAL_CONSOLE_BAUD_RATE} baud rate. "
            f"Invalid lines: {invalid_lines or 'none'}; output: {agetty_output}"
        )

    with allure.step("Validate it is the same health file"):
        logger.info("Validate it is the same health file")
        health_history_output = system.health.history.show()
        assert len(system.health.history.search_line(last_status_line, health_history_output)) == 1, "Health file has changed after reboot"

    with allure.step("Validate health history file indicates reboot occurred and print the status again"):
        logger.info("Validate health history file indicates reboot occurred and print the status again")
        system.health.history.validate_new_summary_line_in_history_file_after_boot(last_status_line)

    with allure.step("Verify system health status and status-led after reboot"):
        system.validate_health_status(
            HealthConsts.OK, expected_led=expected_led)


@pytest.mark.bug(redmine=4969519, note="show system health intermittent failure")
@pytest.mark.system
@pytest.mark.air
@pytest.mark.health
def test_show_system_health(devices):
    """
    Validate all the show system health commands
        Test flow:
            1. validate nv show system health cmd
            2. validate nv show system cmd
            3. validate nv show fae health cmd
            4. validate nv show system health history cmd
            5. validate nv show system health history files cmd
            6. validate nv show system health history files <file> cmd
    """

    system = System()

    with allure.step("Validate \"nv show system health\" cmd"):
        logger.info("Validate \"nv show system health\" cmd")
        health_output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
        ValidationTool.validate_all_values_exists_in_list([HealthConsts.STATUS, HealthConsts.STATUS_LED], health_output.keys()).verify_result()
        system.validate_health_status(HealthConsts.OK)
        verify_health_status_and_led(system, HealthConsts.OK)

    with allure.step("Validate health status with \"nv show system\" cmd"):
        logger.info("Validate health status with \"nv show system\" cmd")
        system_output = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
        ValidationTool.verify_field_exist_in_json_output(system_output, [SystemConsts.HEALTH]).verify_result()
        verify_expected_health_status(system_output[SystemConsts.HEALTH], HealthConsts.STATUS, OK)
    with allure.step("Validate \"nv show fae health\" cmd"):
        logger.info("Validate \"nv show fae health\" cmd")
        detail_health_output = OutputParsingTool.parse_json_str_to_dictionary(Fae().health.show()).get_returned_value()
        ValidationTool.validate_all_values_exists_in_list([HealthConsts.STATUS, HealthConsts.STATUS_LED, HealthConsts.MONITOR_LIST],
                                                          detail_health_output).verify_result()
        verify_expected_health_status(detail_health_output, HealthConsts.STATUS, OK)
        monitor_dict = sort_monitor_list(detail_health_output[HealthConsts.MONITOR_LIST])
        assert len(monitor_dict[NOT_OK]) == 0, "Expected not to have \"Not OK\" devices, cause the health status is OK,\n" \
            "but those devices are not OK : {}".format(monitor_dict[NOT_OK])
        ValidationTool.validate_all_values_exists_in_list(devices.dut.health_components,
                                                          detail_health_output[HealthConsts.MONITOR_LIST].keys()).verify_result()

    with allure.step("Validate \"nv show system health history\" cmd"):
        logger.info("Validate \"nv show system health history\" cmd")
        health_history_output = system.health.history.show()
        assert system.health.history.get_last_status_from_health_file(health_history_output) == OK, "Last status in the health report file is Not OK but expected to be OK"

    with allure.step("Validate \"nv show system health history files\" cmd"):
        logger.info("Validate \"nv show system health history files\" cmd")
        health_history_files = OutputParsingTool.parse_json_str_to_dictionary(system.health.history.files.show()).get_returned_value()
        files_amount = len(health_history_files)
        assert files_amount in [1, 2], "Unexpected amount of history files.\n Expected: 1 or 2 , but got {}".format(files_amount)
        assert HealthConsts.HEALTH_FIRST_FILE in health_history_files, "Expect to have {} as health file, but have those files {}"\
            .format(HealthConsts.HEALTH_FIRST_FILE, health_history_files)
        if files_amount == 2:
            assert HealthConsts.HEALTH_SECOND_FILE in health_history_files, "Expect to have {} as health file, but have those files {}" \
                .format(HealthConsts.HEALTH_SECOND_FILE, health_history_files)

        health_history_file_output = system.health.history.show_health_report_file(HealthConsts.HEALTH_FIRST_FILE)
        # first line in the health report output is the cmd itself, so we will compared just the file itself.
        assert health_history_file_output.split("\n", 2)[2] == health_history_output.split("\n", 2)[2], "The first health file does not show the same info as the default cmd"


@pytest.mark.system
@pytest.mark.health
@pytest.mark.air
def test_show_system_health_component(devices, random_api):
    """
    Validate all three health component show endpoints (command coverage):
        1. nv show system health component
        2. nv show system health component <component> instance
        3. nv show system health component <component> instance <instance_id> (e.g. ASIC1, ALL)
    """

    system = System()
    health_comp_output = OutputParsingTool.parse_json_str_to_dictionary(system.health.component.show()).\
        get_returned_value()
    available_components = list(health_comp_output.keys())
    validation_config = _build_validation_config(available_components, devices)
    logger.info(f"Using validation config for components: {list(validation_config.keys())}")

    with allure.step("Validate show system health component command and instance option for each component"):
        for component in available_components:
            with allure.step(f"Validate show system health component {component}"):
                assert component in HealthConsts.Component.COMPONENTS, \
                    f"Unexpected component '{component}' found in output"

                comp_output = health_comp_output[component]
                assert HealthConsts.Component.INSTANCE in comp_output, \
                    f"Component '{component}' missing 'instance' field"
                instances = comp_output[HealthConsts.Component.INSTANCE]
                assert len(instances) > 0, f"Component '{component}' has no instances"

                _validate_instance_naming_and_count(component, instances, devices, validation_config)
                for instance_id, instance_data in instances.items():
                    _validate_instance_fields_and_values(component, instance_id, instance_data)

                # Endpoint 2: instance output must match full show's instance section (keeps both in sync, no re-validation)
                instance_list = OutputParsingTool.parse_json_str_to_dictionary(
                    system.health.component.show(op_param=f"{component} instance")).get_returned_value()
                assert instances == instance_list, \
                    f"Component '{component}' instance endpoint output != full show instance data"
                one_instance_id = next(iter(instances))  # e.g. ASIC1 for asic, ALL for switch/software/cpu
                out = OutputParsingTool.parse_json_str_to_dictionary(
                    system.health.component.show(op_param=f"{component} instance {one_instance_id}")).get_returned_value()
                ValidationTool.verify_field_exist_in_json_output(
                    out, [HealthConsts.Component.STATE, HealthConsts.Component.LAST_HEALTHY,
                          HealthConsts.Component.UNHEALTHY_COUNT]).verify_result()


@pytest.mark.system
@pytest.mark.health
def test_system_health_component_counters(engines, devices):
    """
    Validate system health component counters
        Test flow:
            1. Get system health component counters information
            2. Simulate fan error
            3. Fix fan error
            4. Simulate PSU error
            5. Fix PSU error
            6. Get updated system health component counters information
            7. Validate counters and timestamps are updated
    """

    system = System()
    platform = Platform()

    # Get the actual components from the API response
    with allure.step("Get available system health components from API"):
        health_out = OutputParsingTool.parse_json_str_to_dictionary(system.health.component.show()).get_returned_value()
        component_list = list(health_out.keys())
        logger.info(f"Available components from API: {component_list}")

    for component_name in component_list:
        with allure.step(f"Get system health component counters details of {component_name}"):
            health_out = OutputParsingTool.parse_json_str_to_dictionary(system.health.component.show()).\
                get_returned_value()
            # New structure: component -> instance -> instance_id -> fields
            component_data = health_out[component_name][HealthConsts.Component.INSTANCE]

            # For multi-instance components (leakage-sensor), store all instance data
            # For single-instance components (fan, psu, cpu, switch), just use the first
            initial_instance_data = {}
            for instance_id, instance_info in component_data.items():
                initial_instance_data[instance_id] = {
                    'unhealthy_count': int(instance_info[HealthConsts.Component.UNHEALTHY_COUNT]),
                    'last_unhealthy': instance_info[HealthConsts.Component.LAST_HEALTHY]
                }

            # Validate all instances are healthy
            for instance_id, instance_info in component_data.items():
                ValidationTool.verify_field_value_in_output(instance_info, HealthConsts.Component.STATE,
                                                            HealthConsts.Component.HEALTHY).verify_result()

        simulated_instance_id = None
        if component_name == "fan":
            with allure.step("Simulate and fix Fan error"):
                simulated_instance_id = HWSimulator.simulate_and_fix_fan_component_error(devices, engines)
                logger.info(f"Simulated error on fan instance: {simulated_instance_id}")

        elif component_name == "psu":
            show_out = OutputParsingTool.parse_json_str_to_dictionary(platform.environment.psu.show()).verify_result()
            with allure.step("Simulate and fix random PSU temperature error"):
                simulated_instance_id = HWSimulator.simulate_and_fix_psu_component_error(devices, engines, show_out)
                logger.info(f"Simulated error on psu instance: {simulated_instance_id}")

        elif component_name == "leakage-sensor":
            with allure.step("Simulate leakage on a random sensor and fix it"):
                simulated_instance_id = simulate_and_fix_leakage_sensor_error(engines, devices)
                logger.info(f"Simulated error on sensor: {simulated_instance_id}")

        elif component_name == HealthConsts.Component.ASIC:
            with allure.step("Simulate and fix ASIC temperature error (via STATE_DB)"):
                simulated_instance_id = simulate_and_fix_asic_component_error(engines, devices)
                if simulated_instance_id is None:
                    continue
                logger.info(f"Simulated error on ASIC instance: {simulated_instance_id}")

        else:
            # Other components (e.g. cpu, switch, software, transceiver) not simulated in this test
            continue

        with allure.step(f"Validate updated state of system health component - {component_name}"):
            retry_call(validate_component_health_data, [system, component_name, HealthConsts.Component.STATE,
                                                        HealthConsts.Component.HEALTHY], exceptions=AssertionError,
                       tries=5, delay=5)

        with allure.step(f"Validate that {component_name} counters are updated on {simulated_instance_id}"):
            expected_count = initial_instance_data[simulated_instance_id]['unhealthy_count'] + 1
            retry_call(validate_component_health_data_for_instance,
                       [system, component_name, simulated_instance_id,
                        HealthConsts.Component.UNHEALTHY_COUNT, str(expected_count)],
                       exceptions=AssertionError, tries=5, delay=5)

        with allure.step(f"Validate that {component_name} last-unhealthy timestamp is updated on {simulated_instance_id}"):
            initial_timestamp = initial_instance_data[simulated_instance_id]['last_unhealthy']
            retry_call(validate_health_component_last_unhealthy_for_instance,
                       [system, component_name, simulated_instance_id, initial_timestamp],
                       exceptions=AssertionError, tries=5, delay=5)

    with allure.step('Clear system health component unhealthy counter'):
        system.health.component.action(ActionConsts.CLEAR)
        time.sleep(5)

    with allure.step("Validate system all health components unhealthy data are cleared"):
        error_msgs = []
        health_out = OutputParsingTool.parse_json_str_to_dictionary(system.health.component.show()).get_returned_value()
        for component in health_out:
            # New structure: component -> instance -> instance_id -> fields
            instances = health_out[component][HealthConsts.Component.INSTANCE]
            for instance_id, instance_data in instances.items():
                with allure.step(f"Validate {component} {instance_id} unhealthy counters are cleared"):
                    count = int(instance_data[HealthConsts.Component.UNHEALTHY_COUNT])
                    if count != 0:
                        error_msgs.append(f"Unhealthy counter for {component} {instance_id} is {count} instead of 0")
                with allure.step(f"Validate {component} {instance_id} unhealthy timestamps are cleared"):
                    last_unhealthy = instance_data[HealthConsts.Component.LAST_HEALTHY]
                    if last_unhealthy != "":
                        error_msgs.append(f"Last-unhealthy timestamp for {component} {instance_id} is {last_unhealthy} instead of empty")
        assert len(error_msgs) == 0, f"Health components unhealthy data are not cleared: {error_msgs}"


@pytest.mark.system
@pytest.mark.health
def test_asic_health_unhealthy_triggers_and_recovery(engines):
    """
    Single test combining all 5 ASIC health scenarios in one series of events (STATE_DB + thermalctld stopped):
    1. New unhealthy detection: ASIC1 down -> validate UNHEALTHY and unhealthy-count increased.
    2. No duplicate trigger: wait another health cycle -> ASIC1 still down, count unchanged.
    3. Second instance unhealthy: ASIC2 down -> both ASIC1 and ASIC2 UNHEALTHY.
    4. Partial recovery: bring ASIC1 up -> ASIC1 HEALTHY, ASIC2 still UNHEALTHY.
    5. Full recovery: bring ASIC2 up -> both HEALTHY.
    """
    TestToolkit.update_engines(engines)
    system = System()
    health_out = OutputParsingTool.parse_json_str_to_dictionary(system.health.component.show()).get_returned_value()
    if HealthConsts.Component.ASIC not in health_out:
        pytest.skip("ASIC component not in health output")
    asic_data = health_out[HealthConsts.Component.ASIC][HealthConsts.Component.INSTANCE]
    if "ASIC1" not in asic_data or "ASIC2" not in asic_data:
        pytest.skip("DUT needs ASIC1 and ASIC2 in health output")

    with allure.step("Stop thermalctld for ASIC temperature simulation"):
        engines.dut.run_cmd("docker exec pmon supervisorctl stop thermalctld")
    try:
        initial_asic1_count = int(asic_data["ASIC1"][HealthConsts.Component.UNHEALTHY_COUNT])
        expected_after_first = initial_asic1_count + 1

        with allure.step("1. New unhealthy detection: set ASIC1 to unhealthy and validate state + count"):
            _inject_asic_temperature(engines, "ASIC1", _ASIC_TEMP_UNHEALTHY)
            _wait_health_cycle()
            _assert_asic_instance(system, "ASIC1", HealthConsts.Component.STATE, HealthConsts.Component.UNHEALTHY,
                                  tries=3, delay=_HEALTH_POLL_WAIT_SEC)
            _assert_asic_instance(system, "ASIC1", HealthConsts.Component.UNHEALTHY_COUNT, str(expected_after_first))

        with allure.step("2. No duplicate trigger: wait another health cycle and verify ASIC1 count unchanged"):
            _wait_health_cycle()
            assert _get_asic_instance_count(system, "ASIC1") == expected_after_first, "ASIC1 count should not change (no duplicate trigger)"

        with allure.step("3. Second instance unhealthy: set ASIC2 to unhealthy, validate both UNHEALTHY"):
            _inject_asic_temperature(engines, "ASIC2", _ASIC_TEMP_UNHEALTHY)
            _wait_health_cycle()
            for inst in ("ASIC1", "ASIC2"):
                _assert_asic_instance(system, inst, HealthConsts.Component.STATE, HealthConsts.Component.UNHEALTHY)

        with allure.step("4. Partial recovery: bring ASIC1 up, validate ASIC1 HEALTHY and ASIC2 still UNHEALTHY"):
            _inject_asic_temperature(engines, "ASIC1", _ASIC_TEMP_HEALTHY)
            _wait_health_cycle()
            _assert_asic_instance(system, "ASIC1", HealthConsts.Component.STATE, HealthConsts.Component.HEALTHY)
            _assert_asic_instance(system, "ASIC2", HealthConsts.Component.STATE, HealthConsts.Component.UNHEALTHY)

        with allure.step("5. Full recovery: bring ASIC2 up, validate both HEALTHY"):
            _inject_asic_temperature(engines, "ASIC2", _ASIC_TEMP_HEALTHY)
            _wait_health_cycle()
            for inst in ("ASIC1", "ASIC2"):
                _assert_asic_instance(system, inst, HealthConsts.Component.STATE, HealthConsts.Component.HEALTHY)
    finally:
        with allure.step("Start thermalctld after ASIC simulation"):
            engines.dut.run_cmd("docker exec pmon supervisorctl start thermalctld")

    with allure.step("Clear system health component unhealthy counters after ASIC test"):
        system.health.component.action(ActionConsts.CLEAR)
        time.sleep(5)


@pytest.mark.system
@pytest.mark.health
@pytest.mark.disable_loganalyzer
def test_asic_health_fatal_state_marks_all_unhealthy(engines):
    """
    When ASIC_HEALTH fatal_state is set to true in STATE_DB, all ASIC instances are marked unhealthy (new image logic).
    Steps: set ASIC_HEALTH|STATE fatal_state 'true' -> wait health cycle -> all ASICs UNHEALTHY ->

           set fatal_state 'false' -> wait -> all ASICs HEALTHY -> clear counters.

    The recovery write (fatal_state=false) is issued as fire-and-forget because
    once fatal_state is asserted the SSH prompt changes to
    ``[System_Fatal_State]...$`` which netmiko does not recognize, causing it
    to hang waiting for the prompt even though the command ran.

    LogAnalyzer is disabled because triggering fatal_state intentionally emits
    error logs on the DUT which would otherwise fail the run.
    """
    TestToolkit.update_engines(engines)
    system = System()
    health_out = OutputParsingTool.parse_json_str_to_dictionary(system.health.component.show()).get_returned_value()
    if HealthConsts.Component.ASIC not in health_out:
        pytest.skip("ASIC component not in health output")
    asic_instances = health_out[HealthConsts.Component.ASIC][HealthConsts.Component.INSTANCE]
    if not asic_instances:
        pytest.skip("No ASIC instances in health output")
    instance_ids = list(asic_instances.keys())

    initially_healthy_components = []
    for comp in (HealthConsts.Component.Software, HealthConsts.Component.Switch):
        if comp in health_out and HealthConsts.Component.INSTANCE in health_out[comp]:
            comp_instances = health_out[comp][HealthConsts.Component.INSTANCE]
            if "ALL" in comp_instances and comp_instances["ALL"].get(HealthConsts.Component.STATE) == HealthConsts.Component.HEALTHY:
                initially_healthy_components.append(comp)

    try:
        with allure.step("Set ASIC_HEALTH fatal_state to true (triggers all ASICs unhealthy)"):
            _set_asic_health_fatal_state(engines, "true")
        _wait_health_cycle()
        with allure.step("Validate all ASIC instances are UNHEALTHY"):
            for inst in instance_ids:
                _assert_asic_instance(system, inst, HealthConsts.Component.STATE, HealthConsts.Component.UNHEALTHY,
                                      tries=3, delay=_HEALTH_POLL_WAIT_SEC)

        with allure.step("Set ASIC_HEALTH fatal_state to false (recovery)"):
            _set_asic_health_fatal_state(engines, "false", fire_and_forget=True)
        _wait_health_cycle()
        _wait_health_cycle()
        with allure.step("Validate all ASIC instances are HEALTHY"):
            for inst in instance_ids:
                _assert_asic_instance(system, inst, HealthConsts.Component.STATE, HealthConsts.Component.HEALTHY,
                                      tries=3, delay=_HEALTH_POLL_WAIT_SEC)
        if initially_healthy_components:
            with allure.step(f"Validate initially healthy components recovered: {initially_healthy_components}"):
                for comp in initially_healthy_components:
                    _assert_component_instance(system, comp, "ALL", HealthConsts.Component.STATE,
                                               HealthConsts.Component.HEALTHY, tries=3, delay=_HEALTH_POLL_WAIT_SEC)
        with allure.step("Wait for system is ready (recovery after fatal state)"):
            DutUtilsTool.wait_for_nvos_to_become_functional(
                engines.dut, throw_exception_on_unhealthy=False).verify_result()
        with allure.step("Wait for software component to recover after fatal mode exit"):
            _assert_component_instance(
                system, HealthConsts.Component.Software, "ALL",
                HealthConsts.Component.STATE, HealthConsts.Component.HEALTHY,
                tries=36, delay=10)
    finally:
        _set_asic_health_fatal_state(engines, "false", fire_and_forget=True)

    with allure.step("Clear system health component unhealthy counters after ASIC fatal test"):
        system.health.component.action(ActionConsts.CLEAR)
        time.sleep(5)


@pytest.mark.system
@pytest.mark.health
def test_software_component_unhealthy_count_lldp_stop(engines):
    """
    Validate software component unhealthy state and count when LLDP is stopped:
    disable LLDP auto-restart (otherwise the daemon restarts the container and software
    can stay HEALTHY while unhealthy-count still increments) -> stop lldp -> sleep 5s ->
    software UNHEALTHY and count increment by 1 -> start lldp -> re-enable auto-restart ->
    state HEALTHY -> clear counters -> count 0.
    """
    TestToolkit.update_engines(engines)
    system = System()
    health_out = OutputParsingTool.parse_json_str_to_dictionary(system.health.component.show()).get_returned_value()
    if HealthConsts.Component.Software not in health_out:
        pytest.skip("Software component not in health output")
    software_instances = health_out[HealthConsts.Component.Software][HealthConsts.Component.INSTANCE]
    if "ALL" not in software_instances:
        pytest.skip("Software component has no ALL instance")
    initial_count = int(software_instances["ALL"][HealthConsts.Component.UNHEALTHY_COUNT])
    sw, inst = HealthConsts.Component.Software, "ALL"
    docker_to_stop = "lldp"

    try:
        with allure.step("Stop {} docker auto restart".format(docker_to_stop)):
            DatabaseTool.sonic_db_cli_hset(engine=engines.dut, asic="", db_name=DatabaseConst.CONFIG_DB_NAME,
                                           db_config="FEATURE|{}".format(docker_to_stop),
                                           param=NvosConst.DOCKER_AUTO_RESTART,
                                           value=NvosConst.DOCKER_STATUS_DISABLED)

        with allure.step("Stop LLDP container"):
            engines.dut.run_cmd("docker stop {}".format(docker_to_stop))
        time.sleep(5)
        with allure.step("Validate software UNHEALTHY and unhealthy-count incremented by 1"):
            _assert_component_instance(system, sw, inst, HealthConsts.Component.STATE, HealthConsts.Component.UNHEALTHY)
            _assert_component_instance(system, sw, inst, HealthConsts.Component.UNHEALTHY_COUNT, str(initial_count + 1))

        with allure.step("Start LLDP container"):
            engines.dut.run_cmd("docker start {}".format(docker_to_stop))
        with allure.step("Validate software state HEALTHY"):
            _assert_component_instance(system, sw, inst, HealthConsts.Component.STATE, HealthConsts.Component.HEALTHY, tries=10)

        with allure.step("Clear system health component unhealthy counters"):
            system.health.component.action(ActionConsts.CLEAR)
            time.sleep(5)
        with allure.step("Validate software unhealthy count is 0"):
            _assert_component_instance(system, sw, inst, HealthConsts.Component.UNHEALTHY_COUNT, "0", tries=3, delay=2)
    finally:
        # since we are disabling docker auto restart, we need to ensure the docker is running
        with allure.step("Ensure {} docker is running".format(docker_to_stop)):
            if docker_to_stop not in engines.dut.run_cmd("docker ps"):
                engines.dut.run_cmd("docker start {}".format(docker_to_stop))
                validate_docker_is_up(engines.dut, docker_to_stop)
        # enable docker auto restart
        with allure.step("Restore {} docker auto restart".format(docker_to_stop)):
            DatabaseTool.sonic_db_cli_hset(engine=engines.dut, asic="", db_name=DatabaseConst.CONFIG_DB_NAME,
                                           db_config="FEATURE|{}".format(docker_to_stop),
                                           param=NvosConst.DOCKER_AUTO_RESTART,
                                           value=NvosConst.DOCKER_STATUS_ENABLED)


@pytest.mark.system
@pytest.mark.health
@pytest.mark.air
@pytest.mark.air_ci
@pytest.mark.air_sanity
def test_system_health_files(engines):
    """
    Will validate the health files requirements:
        -	Tech-support will contain health files
        -	Upload health files
        -	Delete health files
    """

    system_health_files_test(engines, check_rotation=False)


@pytest.mark.system
@pytest.mark.health
@pytest.mark.checklist
def test_system_health_files_with_rotation(engines):
    """
    Will validate the health files requirements:
        -	file will be rotated after 10 MB
        -	maximum 2 health files
        -	Tech-support will contain health files
        -	Upload health files
        -	Delete health files
    """

    system_health_files_test(engines, check_rotation=True)


@pytest.mark.system
@pytest.mark.health
def test_ignore_health_issue(engines, devices, loganalyzer, reset_health_service):
    """
    Validate we can ignore all health issue and status will change to OK
    steps:
        1. Simulate PSU and FAN health issue
        2. Validate health status and report
        3. Ignore PSU issue and Validate
        4. Ignore FAN issue too and Validate health state change to OK
        5. Remove the ignore from FAN issue and Validate health state change to Not OK
        6. Remove the ignore from PSU issue too and Validate
        7. Fix PSU and FAN health issue
    """
    if len(devices.dut.fan_list) == 0 and len(devices.dut.psu_list) == 0:
        pytest.skip("Skipping test because setup has no fans and no psus")
    system = System()
    thermal_directory = devices.dut.fan_direction_dir
    validate_min_psus_for_redundancy(devices, Platform())
    health_config_file = EngineFile(engines.dut, get_system_health_monitoring_config_file_path(engines.dut))
    verify_health_before_test()

    try:
        with allure.step("Simulate PSU and FAN health issue"):
            psu_id, fan_id, psu_symlink, fan_symlink = simulate_fan_and_psu_health_issue(engines, devices)
            psu_display_name = "PSU{}".format(psu_id)
            psu_config_name = "PSU {}".format(psu_id)
            psu_fan_config_name = "psu{}_fan1".format(psu_id)
            fan_display_name = get_fan_display_name(fan_id)
            fan_config_name = "fan{}".format(fan_id)
            if loganalyzer:
                for hostname in loganalyzer.keys():
                    loganalyzer[hostname].ignore_regex.extend(
                        [f"\\.*Fan fault warning: {fan_config_name} is not working\\.*",
                         f"\\.*Fan removed warning: {psu_fan_config_name} was removed from the system, potential overheat hazard\\.*",
                         f"\\.*PSU absence warning: PSU {psu_id} is not present.\\.*",
                         f"\\.*Insufficient number of working fans warning\\.*",
                         ])

        with allure.step("Validate health status and report"):
            system.wait_until_health_status_change_to(NOT_OK)
            verify_health_status_and_led(system, NOT_OK)
            wait_until_devices_health_status_in_monitor_list({psu_display_name: NOT_OK, fan_display_name: NOT_OK})
            verify_devices_health_status_in_issues_list(system, [psu_display_name, fan_display_name])

        with allure.step("Ignore PSU issue and Validate"):
            initial_ignore_list = health_config_file.json_read()[DEVICES_TO_IGNORE_KEY]
            ignore_health_issue(initial_ignore_list + [psu_config_name, psu_fan_config_name], health_config_file,
                                ignore_psu_redundancy=True)
            system.wait_until_health_status_change_to(NOT_OK)
            verify_health_status_and_led(system, NOT_OK)
            wait_until_devices_health_status_in_monitor_list({psu_display_name: IGNORED, fan_display_name: NOT_OK})
            verify_devices_health_status_in_issues_list(system, [fan_display_name])

        with allure.step("Ignore FAN issue too and Validate health state change to OK"):
            ignore_health_issue(initial_ignore_list + [psu_config_name, psu_fan_config_name, fan_config_name],
                                health_config_file, ignore_psu_redundancy=True)
            system.wait_until_health_status_change_to(OK)
            verify_health_status_and_led(system, OK)
            wait_until_devices_health_status_in_monitor_list({psu_display_name: IGNORED, fan_display_name: IGNORED})
            verify_devices_health_status_in_issues_list(system, [])

        with allure.step("Remove the ignore from FAN issue and Validate health state change to Not OK"):
            ignore_health_issue(initial_ignore_list + [psu_config_name, psu_fan_config_name], health_config_file,
                                ignore_psu_redundancy=True)
            system.wait_until_health_status_change_to(NOT_OK)
            verify_health_status_and_led(system, NOT_OK)
            wait_until_devices_health_status_in_monitor_list({psu_display_name: IGNORED, fan_display_name: NOT_OK})
            verify_devices_health_status_in_issues_list(system, [fan_display_name])

        with allure.step("Remove the ignore from PSU issue too and Validate"):
            ignore_health_issue(initial_ignore_list, health_config_file, ignore_psu_redundancy=False)
            system.wait_until_health_status_change_to(NOT_OK)
            verify_health_status_and_led(system, NOT_OK)
            wait_until_devices_health_status_in_monitor_list({psu_display_name: NOT_OK, fan_display_name: NOT_OK})
            verify_devices_health_status_in_issues_list(system, [psu_display_name, fan_display_name])

    finally:

        with allure.step("Fix PSU and FAN health issue"):
            HWSimulator.clear_fan_fault(engines.dut, thermal_directory, fan_id, fan_symlink)
            HWSimulator.simulate_fix_psu_fault(engines.dut, thermal_directory, psu_id, psu_symlink)
            health_config_file.revert_to_original()
            system.wait_until_health_status_change_to(OK)
            verify_health_status_and_led(system, OK)

    with allure.step("Clear system health component unhealthy counters"):
        system.health.component.action(ActionConsts.CLEAR)
        time.sleep(5)


@pytest.mark.system
@pytest.mark.health
@pytest.mark.cumulus
def test_simulate_health_problem_with_hw_simulator(devices, engines, set_unset_ps_redundancy_ps, reset_health_service):
    """
    Validate health monitoring.
    Health status should change to "Not OK" when we simulate a problem and return to "OK" if status fixed or ignored.
        Test flow:
            1. Simulate health problem with HW simulator
            2. validate health status changed to "Not OK"
            3. validate devices appear in the detailed health report as not OK
            5. validate status has changed in the log
            6. fix the health issue
            7. validate health status changed to "OK"
            8. validate devices appear in the detailed health report as OK
    """
    with allure.step("Validate system had PSUs and Fans"):
        if len(devices.dut.fan_list) == 0 and len(devices.dut.psu_list) == 0:
            pytest.skip("Skipping test because setup has no fans and no psus")

    system = System()
    date_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())
    thermal_directory = devices.dut.fan_direction_dir
    health_issue_dict = {}

    with allure.step("Validate minimum no of PSUs for redundancy"):
        validate_min_psus_for_redundancy(devices, Platform())

    with allure.step("Clear health file and rotate logs"):
        system.log.rotate_logs()
        system.health.history.files.file_name[HealthConsts.HEALTH_FIRST_FILE].action_delete().verify_result()
        time.sleep(1)

    with allure.step("Validate system health prior to test"):
        verify_health_before_test()

    try:
        with allure.step("Simulate PSU and FAN health issue"):
            psu_id, fan_id, psu_symlink, fan_symlink = simulate_fan_and_psu_health_issue(engines, devices)
            logger.info("sleep 5 sec after simulating HW issue")
            time.sleep(5)
            psu_display_name = "PSU{}".format(psu_id)
            fan_display_name = get_fan_display_name(fan_id)
            health_issue_dict = devices.dut.get_health_issue_dict_fan_and_psu(psu_display_name, fan_display_name)

        with allure.step("Validate health issue"):
            validate_health_fix_or_issue(engines, system, health_issue_dict, date_time, False)

    finally:
        date_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())
        time.sleep(20)
        with allure.step("Cleanup - Fix the health issues"):
            with allure.step("Fix the Fan fault issue"):
                HWSimulator.clear_fan_fault(engines.dut, thermal_directory, fan_id, fan_symlink)
            with allure.step("Fix the PSU fault issue"):
                HWSimulator.simulate_fix_psu_fault(engines.dut, thermal_directory, psu_id, psu_symlink)
            validate_health_fix_or_issue(engines, system, health_issue_dict, date_time, True)

    with allure.step("Clear system health component unhealthy counters"):
        system.health.component.action(ActionConsts.CLEAR)
        time.sleep(5)


@pytest.mark.system
@pytest.mark.health
@pytest.mark.cumulus
def test_simulate_fan_speed_fault(devices, engines, loganalyzer, reset_health_service):
    """
    Validate health monitoring when having a fan speed fault.
        Test flow:
            1. Simulate fan speed fault
            2. validate health status changed to "Not OK"
            3. validate devices appear in the detailed health report as not OK
            5. validate status has changed in the log
            6. fix the health issue
            7. validate health status changed to "OK"
            8. validate devices appear in the detailed health report as OK
    """
    if len(devices.dut.fan_list) == 0:
        pytest.skip("System has no fans. Skipping")
    system = System()
    thermal_directory = devices.dut.fan_direction_dir
    speed_changed = False
    system.log.rotate_logs()
    date_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())
    system.health.history.files.file_name[HealthConsts.HEALTH_FIRST_FILE].action_delete().verify_result()
    time.sleep(1)
    verify_health_before_test()
    fan_id = random.randrange(1, len(devices.dut.fan_list) + 1)
    logger.info("Chosen fan : {}  - {}".format(fan_id, get_fan_display_name(fan_id)))
    fan_display_name = get_fan_display_name(fan_id)
    symlink_target = None
    health_issue_dict = {
        fan_display_name: [
            FansConsts.FAN_SPEED_OUT_OF_RANGE,
            FansConsts.FAN_NOT_WORKING,
            FansConsts.FAN_LOW_SPEED_WARNING,  # ETH/Cumulus may report "low" for fan speed fault
        ]
    }
    real_speed = 0
    if loganalyzer:
        for hostname in loganalyzer.keys():
            loganalyzer[hostname].ignore_regex.extend([f"\\.*Fan low speed warning: fan{fan_id} current speed\\.*",
                                                       f"\\.*Fan fault warning: fan{fan_id} is not working\\.*",
                                                       f"\\.*Insufficient number of working fans warning\\.*"])

    try:
        symlink_target = HWSimulator.simulate_fan_speed_fault(engines.dut, thermal_directory, fan_id, 1)
        speed_changed = True
        retry_validate_health_fix_or_issue(engines, system, health_issue_dict, date_time, False)

    finally:
        if speed_changed:
            with allure.step("Fix the health issues"):
                date_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())
                time.sleep(1)
                HWSimulator.simulate_fix_fan_speed_fault(engines.dut, thermal_directory, fan_id, symlink_target)
                retry_validate_health_fix_or_issue(engines, system, health_issue_dict, date_time, True)

    with allure.step("Clear system health component unhealthy counters"):
        system.health.component.action(ActionConsts.CLEAR)
        time.sleep(5)


@pytest.mark.disable_loganalyzer
@pytest.mark.system
@pytest.mark.health
def test_simulate_multi_fan_speed_fault(engines, devices, loganalyzer, reset_health_service):
    """
    Validate health monitoring when having a fan speed fault.
        Test flow:
            1. Get the last system event ID to be used as marker
            2. Choose two fans to be used for testing
            3. Simulate fan speed fault for the chosen fans
            4. Validate fan speed fault system events for these fans
            5. Simulate fan speed fault fix for the chosen fans
            6. Validate fan speed fault system clear events for these fans
    """
    if len(devices.dut.fan_list) == 0:
        pytest.skip("System has no fans. Skipping")
    system = System()
    platform = Platform()
    thermal_directory = devices.dut.fan_direction_dir
    show_output = OutputParsingTool.parse_json_str_to_dictionary(platform.environment.fan.show()).verify_result()
    no_of_fans = 0
    for key in show_output:
        if re.search("^FAN.*", key):
            no_of_fans += 1
    fan_ids = random.sample([i for i in range(1, no_of_fans)], 2)
    logger.info("Chosen fans : {}".format(fan_ids))
    fan_info = dict()
    fan_fault_events = [FansConsts.FAN_SPEED_OUT_OF_RANGE, "is " + FansConsts.FAN_NOT_WORKING]
    fan_speed_fixed = False

    if loganalyzer:
        for key, value in loganalyzer.items():
            for fan_id in fan_ids:
                value.ignore_regex.extend([f"\\.*Fan low speed warning: fan{fan_id} current speed\\.*",
                                           f"\\.*Fan fault warning: fan{fan_id} is not working\\.*",
                                           f"\\.*Insufficient number of working fans warning\\.*"])

    try:
        with allure.step("Get the latest event"):
            last_event = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.events.show_events_last_recent_entries(SystemConsts.SYSTEM_LAST_EVENT)).get_returned_value()
            latest_event_id = list(last_event)[0]

        with allure.step("Simulate fan speed fault for chosen fans:{}".format(fan_ids)):
            for fan_id in fan_ids:
                fan_display_name = get_fan_display_name(fan_id)
                symlink_target = HWSimulator.simulate_fan_speed_fault(engines.dut, thermal_directory, fan_id, 1)
                fan_info[fan_id] = [fan_display_name, symlink_target]
                time.sleep(2)
        with allure.step("Validate system event for fan speed fault for chosen fans:{}".format(fan_ids)):
            for fan_id in fan_ids:
                prefix = fan_info[fan_id][0] + " "
                events_to_search = [prefix + fan_fault_event for fan_fault_event in fan_fault_events]
                retry_call(validate_system_event, [system, latest_event_id, events_to_search],
                           exceptions=AssertionError, tries=24, delay=5)

        with allure.step("Simulate fix fan speed fault for chosen fans:{}".format(fan_ids)):
            for fan_id in fan_ids:
                HWSimulator.simulate_fix_fan_speed_fault(engines.dut, thermal_directory, fan_id, fan_info[fan_id][1])
            fan_speed_fixed = True

        with allure.step("Validate system clear event for speed fault for chosen fans:{}".format(fan_ids)):
            for fan_id in fan_ids:
                prefix = "Cleared: " + fan_info[fan_id][0] + " "
                clear_events_to_search = [prefix + fan_fault_event for fan_fault_event in fan_fault_events]
                retry_call(validate_system_event, [system, latest_event_id, clear_events_to_search],
                           exceptions=AssertionError, tries=24, delay=5)
    finally:
        time.sleep(2)
        if not fan_speed_fixed:
            with allure.step("Fix the fan speed fault"):
                for fan_id in fan_ids:
                    if fan_id in fan_info:
                        HWSimulator.simulate_fix_fan_speed_fault(engines.dut, thermal_directory, fan_id, fan_info[fan_id][1])

    with allure.step("Clear system health component unhealthy counters"):
        system.health.component.action(ActionConsts.CLEAR)
        time.sleep(5)


@pytest.mark.disable_loganalyzer
@pytest.mark.system
@pytest.mark.health
def test_simulate_psu_multi_faults(engines, devices, loganalyzer, reset_health_service):
    """
    Validate health monitoring when having a fan speed fault.
        Test flow:
            1. Get the last system event ID to be used as marker
            2. Choose one PSU to be used for testing
            3. Simulate PSU temperature out of range fault for the chosen PSU
            4. Validate PSU temperature fault system event for this PSU
            5. Simulate PSU absent for the chosen PSU
            6. Validate PSU absent fault system event for this PSU
            7. Get the last system event ID to be used as new marker
            8. Simulate PSU present for the chosen fans
            9. Validate PSU temperature fault system event again for this PSU
            10. Simulate PSU temperature in range for the chosen PSU
            11. Validate Cleared:PSU temperature fault system event for this PSU
    """
    if len(devices.dut.psu_list) == 0:
        pytest.skip("System has no psus. Skipping")
    system = System()
    platform = Platform()
    thermal_directory = devices.dut.fan_direction_dir
    show_output = OutputParsingTool.parse_json_str_to_dictionary(platform.environment.psu.show()).verify_result()

    psu_id_list = []
    for key in show_output:
        psu_id = re.search(r"PSU(\d+).*", key)
        if psu_id:
            if show_output[key][SystemConsts.STATE] == FansConsts.STATE_OK:
                psu_id_list.append(psu_id.group(1))
    assert len(psu_id_list) > 0, "No active PSUs shown"
    # Choose random PSU from list of present PSUs
    psu_id = random.choice(psu_id_list)

    logger.info("Chosen PSU : {}".format(psu_id))
    psu_display_name = "PSU{}".format(psu_id)
    psu_info = dict()
    psu_fault_events = ["temperature is too hot", "is missing - Unpopulated PSU slot"]
    temp_fault = False
    temp_fixed = False
    psu_status_fault = False
    psu_status_fixed = False

    if loganalyzer:
        for key, value in loganalyzer.items():
            value.ignore_regex.extend([f"\\.*PSU absence warning: PSU {psu_id} is not present.\\.*"])

    try:
        with allure.step("Get the latest event"):
            last_event = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.events.show_events_last_recent_entries(SystemConsts.SYSTEM_LAST_EVENT)).get_returned_value()
            latest_event_id = list(last_event)[0]

        with allure.step("Simulate PSU temperature fault for chosen PSU:{}".format(psu_id)):
            temp_symlink = HWSimulator.simulate_psu_temp_fault(engines.dut, thermal_directory, psu_id)
            psu_info[psu_id] = [psu_display_name, temp_symlink]
            temp_fault = True
            time.sleep(2)

        with allure.step("Validate system event for PSU temperature fault for chosen PSU:{}".format(psu_id)):
            event_to_search = psu_info[psu_id][0] + " " + psu_fault_events[0]
            retry_call(validate_system_event, [system, latest_event_id, [event_to_search]],
                       exceptions=AssertionError, tries=12, delay=5)

        with allure.step("Simulate PSU absent fault for chosen PSU:{}".format(psu_id)):
            psu_status_symlink = HWSimulator.simulate_psu_fault(engines.dut, thermal_directory, psu_id)
            psu_status_fault = True
            time.sleep(2)

        with allure.step("Validate system event for PSU missing status for chosen PSU:{}".format(psu_id)):
            event_to_search = psu_info[psu_id][0] + " " + psu_fault_events[1]
            retry_call(validate_system_event, [system, latest_event_id, [event_to_search]],
                       exceptions=AssertionError, tries=12, delay=5)

        with allure.step("Get the latest event to be used as new marker"):
            last_event = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.events.show_events_last_recent_entries(SystemConsts.SYSTEM_LAST_EVENT)).get_returned_value()
            latest_event_id = list(last_event)[0]

        with allure.step("Simulate fix PSU absent for chosen PSU:{}".format(psu_id)):
            HWSimulator.simulate_fix_psu_fault(engines.dut, thermal_directory, psu_id, psu_status_symlink)
            psu_status_fixed = True

        with allure.step("Validate system event for PSU temperature fault for chosen PSU:{}".format(psu_id)):
            event_to_search = psu_info[psu_id][0] + " " + psu_fault_events[0]
            retry_call(validate_system_event, [system, latest_event_id, [event_to_search]],
                       exceptions=AssertionError, tries=12, delay=5)

        with allure.step("Simulate PSU temperature fault fix for chosen PSU:{}".format(psu_id)):
            HWSimulator.simulate_fix_psu_temp_fault(engines.dut, thermal_directory, psu_id, psu_info[psu_id][1])
            time.sleep(2)
            temp_fixed = True

        with allure.step("Validate clear system event for PSU temperature fault for chosen PSU:{}".format(psu_id)):
            event_to_search = "Cleared: " + psu_info[psu_id][0] + " " + psu_fault_events[0]
            retry_call(validate_system_event, [system, latest_event_id, [event_to_search]],
                       exceptions=AssertionError, tries=12, delay=5)

    finally:
        time.sleep(1)
        if temp_fault and not temp_fixed:
            with allure.step("Fix the PSU temperature fault for PSU:{}".format(psu_id)):
                HWSimulator.simulate_fix_psu_temp_fault(engines.dut, thermal_directory, psu_id, psu_info[psu_id][1])

        if psu_status_fault and not psu_status_fixed:
            with allure.step("Fix PSU absent fault for chosen PSU:{}".format(psu_id)):
                HWSimulator.simulate_fix_psu_fault(engines.dut, thermal_directory, psu_id, psu_status_symlink)

    with allure.step("Clear system health component unhealthy counters"):
        system.health.component.action(ActionConsts.CLEAR)
        time.sleep(5)


@pytest.mark.system
@pytest.mark.health
def test_simulate_health_problem_with_user_config_file(devices, engines, reset_health_service):
    """
    Validate health monitoring.
    Health status should change to "Not OK" when we simulate a problem and return to "OK" if status fixed or ignored.
        Test flow:
            1. Simulate health problem with user config file
            2. validate health status changed to "Not OK"
            3. validate new devices appear in the detailed cmd
            5. validate status has changed in the log
            6. fix the health issue
            7. validate health status changed to "Not OK"
            8. validate new devices removed from the detailed cmd
    """

    system = System()
    system.log.rotate_logs()
    date_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())
    system.health.history.files.file_name[HealthConsts.HEALTH_FIRST_FILE].action_delete().verify_result()
    time.sleep(1)
    system.validate_health_status(OK)

    with allure.step("Simulate health issue"):
        with simulate_health_issue_using_config_file(engines.dut):
            validate_health_issues_exist(system, SIMULATED_ISSUES)
            validate_health_fix_or_issue(engines, system, SIMULATED_ISSUES, date_time, False)
            date_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())

    time.sleep(1)
    validate_health_fix_or_issue(engines, system, SIMULATED_ISSUES, date_time, True)

    with allure.step("Clear system health component unhealthy counters"):
        system.health.component.action(ActionConsts.CLEAR)
        time.sleep(5)


@pytest.mark.system
@pytest.mark.health
def test_simulate_health_problem_with_docker_stop(devices, engines, reset_health_service):
    """
    Validate health monitoring.
    Health status should change to "Not OK" when we simulate a problem and return to "OK" if status fixed or ignored.
        Test flow:
            1. Simulate health problem with user config file
            2. stop docker auto restart
            3. validate health status changed to "Not OK"
            4. validate new devices appear in the detailed cmd
            5. validate status has changed in the log
            6. fix the health issue
            7. validate health status changed to "Not OK"
            8. validate new devices removed from the detailed cmd
    """

    system = System()
    system.log.rotate_logs()
    date_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())
    system.health.history.files.file_name[HealthConsts.HEALTH_FIRST_FILE].action_delete().verify_result()
    time.sleep(1)
    system.validate_health_status(OK)
    docker_to_stop = 'lldp'
    docker_not_running_log_str = "Container '" + docker_to_stop + "' is not running"

    try:
        with allure.step("stop {} docker auto restart".format(docker_to_stop)):
            DatabaseTool.sonic_db_cli_hset(engine=engines.dut, asic="", db_name=DatabaseConst.CONFIG_DB_NAME,
                                           db_config="FEATURE|{}".format(docker_to_stop),
                                           param=NvosConst.DOCKER_AUTO_RESTART,
                                           value=NvosConst.DOCKER_STATUS_DISABLED)

        with allure.step("Get the latest event"):
            last_event = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.events.show_events_last_recent_entries(SystemConsts.SYSTEM_LAST_EVENT)).get_returned_value()
            latest_event_id = list(last_event)[0]

        with allure.step("stop {} docker".format(docker_to_stop)):
            time.sleep(3)
            output = engines.dut.run_cmd("docker stop {}".format(docker_to_stop))
            assert docker_to_stop in output, "Failed to stop docker"

        with allure.step("Validate docker not running in health issues"):
            health_issue_dict = {
                "lldp:lldpd": "Process 'lldpd' in container 'lldp' is not running",
                "lldp:lldpmgrd": "Process 'lldpmgrd' in container 'lldp' is not running"
            }
            validate_health_fix_or_issue(engines, system, health_issue_dict, date_time, False, False)

        with allure.step("validate docker not running event in system events"):
            retry_call(validate_system_event, [system, latest_event_id, [docker_not_running_log_str]],
                       exceptions=AssertionError, tries=12, delay=5)

    finally:
        date_time = ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show())
        time.sleep(1)
        with allure.step("Fix the health issue"):
            with allure.step("restart docker"):
                output = engines.dut.run_cmd("docker start {}".format(docker_to_stop))
                assert docker_to_stop in output, "Failed to start docker"
            with allure.step("restart docker auto start"):
                DatabaseTool.sonic_db_cli_hset(engine=engines.dut, asic="", db_name=DatabaseConst.CONFIG_DB_NAME,
                                               db_config="FEATURE|{}".format(docker_to_stop),
                                               param=NvosConst.DOCKER_AUTO_RESTART,
                                               value=NvosConst.DOCKER_STATUS_ENABLED)
            validate_docker_is_up(engines.dut, docker_to_stop)
            time.sleep(10)
            validate_health_fix_or_issue(engines, system, health_issue_dict, date_time, True)

        with allure.step("validate docker not running clear event in system events"):
            clear_docker_not_running_log_str = "Cleared: {}".format(docker_not_running_log_str)
            retry_call(validate_system_event, [system, latest_event_id, [clear_docker_not_running_log_str]],
                       exceptions=AssertionError, tries=12, delay=5)

    with allure.step("Clear system health component unhealthy counters"):
        system.health.component.action(ActionConsts.CLEAR)
        time.sleep(5)


def validate_system_event(system, latest_event_id, events_to_search):
    events = Tools.OutputParsingTool.parse_json_str_to_dictionary(system.events.show_events_last_recent_entries(SystemConsts.SYSTEM_LAST_EVENT, '')).get_returned_value()
    newer_events = [events[event]['text'] for event in list(events) if int(event) > int(latest_event_id)]
    assert bool(set(events_to_search) & set(newer_events)), "None of events:{} found in events".format(events_to_search)


@retry(Exception, tries=5, delay=2)
def validate_docker_is_up(engine, docker):
    assert docker in engine.run_cmd("docker ps")


def validate_min_psus_for_redundancy(devices, platform):
    if not devices.dut.psu_list:
        pytest.skip(f"DUT has 0 valid PSUs, we cant simulate psu fault due to ps-redundancy")
    valid_psus = platform.environment.get_available_psus()
    if len(valid_psus) == len(devices.dut.psu_list) / 2:
        pytest.skip(f"DUT has {len(valid_psus)} valid PSUs, we cant simulate psu fault due to ps-redundancy")


def verify_health_status_and_led(system, expected_status, output=None):
    # Validate health status through show system
    system.validate_health_status(expected_status)

    # Validate health status through show system health
    if not output:
        output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
    verify_expected_health_status(output, HealthConsts.STATUS, expected_status)

    # Validate health status-led through show system health
    expected_led = HealthConsts.LED_OK_STATUS if expected_status == HealthConsts.OK else HealthConsts.LED_NOT_OK_STATUS
    verify_expected_health_status(output, HealthConsts.STATUS_LED, expected_led)


def verify_health_before_test():
    try:
        verify_health_status_and_led(System(), OK)
    except AssertionError as e:
        raise Exception("Cannot run test because device health is not OK.\n" + ExceptionTool.format_traceback())


def verify_devices_health_status_in_monitor_list(device_status_dict, monitor_list=None):
    """
    verify device status in the health detail output
    :param device_status_dict: dictionary with devices and their status, example: {PSU1: OK , PSU2: Not OK, FAN1/1: Ignored}
    """
    monitor_dict = sort_monitor_list(monitor_list)
    for device_name, status in device_status_dict.items():
        if status == HealthConsts.IGNORED and monitor_list:
            assert device_name not in list(monitor_list.keys()), \
                f"{device_name} should be ignored , so should not appear in the monitor list"
        else:
            assert device_name in monitor_dict[status]


@retry(Exception, tries=10, delay=2)
def wait_until_devices_health_status_in_monitor_list(device_status_dict):
    """
    Poll fae health monitor list until all devices appear with their expected status.
    Retries up to 10 times with a 2-second delay to account for the health daemon polling interval.
    :param device_status_dict: dictionary with devices and their expected status,
                               example: {PSU1: NOT_OK, FAN1/1: NOT_OK}
    """
    monitor_list = OutputParsingTool.parse_json_str_to_dictionary(
        Fae().health.show()).get_returned_value()[HealthConsts.MONITOR_LIST]
    verify_devices_health_status_in_monitor_list(device_status_dict, monitor_list)


def verify_devices_health_status_in_issues_list(system, devices_list):
    """
    verify device status in the health show output, under the section "issues"
    :param devices_list: list of devices with issues, example: [PSU1, PSU2, FAN1/1]
    """
    issues_dict = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()[HealthConsts.ISSUES]
    assert set(devices_list) <= set(list(issues_dict.keys()))


def get_system_health_monitoring_config_file_path(engine):
    with allure.step("Get path of system_health_monitoring_config.json"):
        platform = engine.run_cmd("sonic-cfggen -H -v DEVICE_METADATA.localhost.platform").strip()
        ret = HealthConsts.HEALTH_MONITOR_CONFIG_FILE_PATH.format(platform)
        logger.info(ret)
        return ret


def simulate_fan_and_psu_health_issue(engines, devices):
    thermal_directory = devices.dut.fan_direction_dir
    if devices.dut.psu_list:
        psu_id = int(random.choice(Platform().environment.get_available_psus()).replace('PSU', ''))
    else:
        psu_id = None
    if devices.dut.fan_list:
        fan_id = random.randrange(1, len(devices.dut.fan_list) + 1)
    else:
        fan_id = None
    logger.info("Chosen PSU : {}\n Chosen fan : {}  - {}".format(psu_id, fan_id, get_fan_display_name(fan_id)))
    fan_symlink = HWSimulator.simulate_fan_fault(engines.dut, thermal_directory, fan_id)
    psu_symlink = HWSimulator.simulate_psu_fault(engines.dut, thermal_directory, psu_id)
    return psu_id, fan_id, psu_symlink, fan_symlink


def get_fan_display_name(fan_id):
    if fan_id:
        section = 1 if fan_id % 2 == 1 else 2
        num = math.floor(fan_id / 2) + fan_id % 2
        return "FAN{}/{}".format(num, section)
    else:
        return None


def ignore_health_issue(components_list_to_ignore, health_config_file: EngineFile, ignore_psu_redundancy=None):
    health_config_dict = health_config_file.json_read()
    health_config_dict[DEVICES_TO_IGNORE_KEY] = components_list_to_ignore
    health_config_dict["supports_ps_redundancy"] = not ignore_psu_redundancy
    health_config_file.json_overwrite(health_config_dict)


def verify_issues_in_health_output(health_issues, expected_issues, is_fae_output):
    key = 'message' if is_fae_output else 'issue'
    issues_matched = False
    for component, issues in expected_issues.items():
        for issue in issues:
            if issue in health_issues[component][key]:
                issues_matched = True

    assert issues_matched, (f'Expected {component} health issue to be one of: {issues}, '
                            f'but got "{health_issues[component][key]}"')


@retry(Exception, tries=6, delay=10)
def retry_validate_health_fix_or_issue(engines, system, health_issue_dict, search_since_datetime, is_fix, expected_in_monitor_list=True):
    validate_health_fix_or_issue(engines, system, health_issue_dict, search_since_datetime, is_fix, expected_in_monitor_list)


def system_health_files_test(engines, check_rotation=False):
    """
    Will validate the health files requirements:
    steps:
        1. validate health status is OK
        2. simulate health issue
        3. validate health status is not OK
        4. do file rotation (if flag is true)
        5. Validate health files in tech support file
        6. upload health files
        7. delete health files
    """

    system = System()
    system.validate_health_status(OK)

    try:
        with simulate_health_issue_using_config_file(engines.dut):
            validate_health_issues_exist(system, SIMULATED_ISSUES)

            if check_rotation:
                with allure.step("First file rotation"):
                    logger.info("First file rotation")
                    cause_health_file_rotation_and_validate(engines.dut, system)

                with allure.step("Second file rotation"):
                    logger.info("Second file rotation")
                    cause_health_file_rotation_and_validate(engines.dut, system)

            health_files = list(OutputParsingTool.parse_json_str_to_dictionary(system.health.history.files.show()).get_returned_value().keys())

            with allure.step("Validate health files in tech support file"):
                logger.info("Validate health files in tech support file")
                validate_health_files_exist_in_techsupport(system, engines.dut, health_files)

            with allure.step("Upload health files"):
                logger.info("Upload health files")
                validate_upload_health_files(engines, system, health_files)

            with allure.step("Delete health files"):
                logger.info("Delete health files")
                validate_delete_health_files(system, health_files)

    finally:
        system.wait_until_health_status_change_to(OK)


def validate_delete_health_files(system, health_files=[HealthConsts.HEALTH_FIRST_FILE, HealthConsts.HEALTH_SECOND_FILE]):
    """
    delete health files and validate new health file was crated with health summary status
    """
    last_status_line = system.health.history.search_line(HealthConsts.ADD_STATUS_TO_SUMMARY_REGEX + OK)[-1]
    system.health.history.files.delete_files(health_files).verify_result()
    time.sleep(5)
    with allure.step("Validate new file was created"):
        logger.info("Validate new file was created")
        assert len(system.health.history.search_line(last_status_line)) == 0, "Health file has not changed"

    with allure.step("Validate health status exist in the history file"):
        logger.info("Validate health status exist in the history file")
        health_history_output = system.health.history.show()
        assert system.health.history.get_last_status_from_health_file(health_history_output) == NOT_OK
        assert "health_history file deleted, creating new file" in health_history_output

    validate_health_files_amount(1)


def validate_upload_health_files(engines, system, health_files=[HealthConsts.HEALTH_FIRST_FILE, HealthConsts.HEALTH_SECOND_FILE]):
    """
    validate upload health files with scp and sftp
    """
    upload_protocols = ['scp', 'sftp']
    player = engines.get('sonic_mgmt') or engines.oob_mgmt_server
    file_to_upload = random.choice(health_files)

    with allure.step(
            "Upload health file to player {} with the next protocols : {}".format(player.ip, upload_protocols)):
        logging.info("Upload health file to player {} with the next protocols : {}".format(player.ip, upload_protocols))

        for protocol in upload_protocols:
            with allure.step("Upload health file to player with {} protocol".format(protocol)):
                logging.info("Upload health file to player with {} protocol".format(protocol))
                upload_path = '{}://{}:{}@{}/tmp/{}'.format(protocol, player.username, player.password, player.ip,
                                                            file_to_upload)
                system.health.history.files.file_name[file_to_upload].action_upload(upload_path).verify_result()

            with allure.step("Validate file was uploaded to player and delete it"):
                logging.info("Validate file was uploaded to player and delete it")
                assert player.run_cmd(
                    cmd='ls /tmp/ | grep {}'.format(file_to_upload)), "Did not find the file with ls cmd"
                player.run_cmd(cmd='rm -f /tmp/{}'.format(file_to_upload))
    with allure.step("Validate health files still exist"):
        logger.info("Validate health files still exist")
        validate_health_files_amount(len(health_files))


def validate_health_files_exist_in_techsupport(system, engine, health_files=[HealthConsts.HEALTH_FIRST_FILE, HealthConsts.HEALTH_SECOND_FILE]):
    """
    generate techsupport and validate we have the health files in the log dir
    """
    try:
        system.techsupport.action_generate()
        system.techsupport.extract_techsupport_files(engine)
        techsupport_files_list = system.techsupport.get_techsupport_files_list(engine, 'log')
        for health_file in health_files:
            assert "{}.gz".format(health_file) in techsupport_files_list, \
                "Expect to have {} file, in the tech support log files {}".format(HealthConsts.HEALTH_FIRST_FILE, techsupport_files_list)

    finally:
        system.techsupport.cleanup(engine)
        if system.techsupport.file_name:
            system.techsupport.files.file_name[system.techsupport.file_name].action_delete()


def validate_health_issues_exist(system, issues: Dict[str, str]):
    with allure.step("Validate health status has change and add the info of the new devices"):
        logger.info("Validate health status has change and add the info of the new devices")
        system.wait_until_health_status_change_to(NOT_OK)
        health_output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
        verify_expected_health_status(health_output, HealthConsts.STATUS, NOT_OK)
        detail_health_output = OutputParsingTool.parse_json_str_to_dictionary(
            Fae().health.show()).get_returned_value()
        verify_expected_health_status(detail_health_output, HealthConsts.STATUS, NOT_OK)
        for device, issue in issues.items():
            assert device in health_output[HealthConsts.ISSUES].keys()
            assert issue in health_output[HealthConsts.ISSUES][device].values()
            assert device in detail_health_output[HealthConsts.MONITOR_LIST].keys()
            assert issue in detail_health_output[HealthConsts.MONITOR_LIST][device].values()


def cause_health_file_rotation_and_validate(engine, system):
    last_status_line = system.health.history.search_line(HealthConsts.ADD_STATUS_TO_SUMMARY_REGEX + OK)[-1]
    with allure.step("create text file in size of 10 MB and replace it with the health file"):
        logger.info("create text file in size of 10 MB and replace it with the health file")
        engine.run_cmd("dd if=/dev/urandom bs=1M count=10 | base64 > file.txt")
        engine.run_cmd("sudo cp file.txt /var/log/health_history")

    with allure.step("Wait until file rotation"):
        logger.info("Wait until file rotation")
        system.health.history.wait_until_health_history_file_rotation(engine)

    with allure.step("Validate we have 2 health files"):
        logger.info("Validate we have 2 health files")
        wait_until_expected_health_files_amount(2)

    with allure.step("Validate new file was created"):
        logger.info("Validate new file was created")
        lines = system.health.history.search_line(last_status_line)
        assert len(lines) == 0, "Health file has not changed"

    with allure.step("Validate health status exist in the history file"):
        logger.info("Validate health status exist in the history file")
        health_history_output = system.health.history.show()
        assert system.health.history.get_last_status_from_health_file(health_history_output) == NOT_OK


def sort_monitor_list(monitor_list=None):
    """
    get the monitor list from the "nv show fae health command,
    return a dictionary with all the optional status as keys: [OK, Not OK , Ignored]
    if the status of a device is not one of [OK, Not OK , Ignored], we will consider it as NOT OK.
    :param monitor_list:
    :return:
    """
    if not monitor_list:
        monitor_list = OutputParsingTool.parse_json_str_to_dictionary(Fae().health.show()).get_returned_value()[HealthConsts.MONITOR_LIST]
    status_options = [OK, NOT_OK, IGNORED]
    monitor_dict = {status_key: [] for status_key in status_options}
    for key, value in monitor_list.items():
        status = value[HealthConsts.STATUS]
        assert status in status_options, "{} is not expected status. Expect to be one of them {}".format(status, status_options)
        monitor_dict[status].append(key)
    return monitor_dict


def verify_expected_health_status(health_output_dict, health_status_field, expected_status):
    """
    verify the expected health status
    :param health_output_dict: dictionary of health status. for example from "nv show system health" cmd.
    :param health_status_field: the health status field name. example : "Status"
    :param expected_status: the expected health status. example "Not OK"
    """
    assert expected_status == health_output_dict[health_status_field], \
        "Unexpected health status. \n Expected: {}, but got :{}".format(expected_status, health_output_dict[health_status_field])


@retry(Exception, tries=12, delay=30)
def wait_until_expected_health_files_amount(num_of_expected_files, actual_health_files=None):
    validate_health_files_amount(num_of_expected_files, actual_health_files)


def validate_health_files_amount(num_of_expected_files, actual_health_files=None):
    if not actual_health_files:
        actual_health_files = OutputParsingTool.parse_json_str_to_dictionary(System().health.history.files.show()).get_returned_value()
    assert num_of_expected_files == len(actual_health_files), \
        "Unexpected num of health files.\n Expected: {}, actual files: {}".format(num_of_expected_files, actual_health_files)


@contextmanager
def simulate_health_issue_using_config_file(engine):
    """
    Creates a simple script and configures the switch (using the health-config file) to use it as an extra
    health-checker. This will cause the switch to report the health issues configured in SIMULATED_ISSUES.
    When the context exits the script is deleted and the configuration restored so the health issues are gone.
    """
    with TempFileOnEngine(engine, 'py') as checker_file:
        checker_file.write('print("MyCategory")')
        for device, issue in SIMULATED_ISSUES.items():
            checker_file.write(f'print("{device}:{issue}")')

        with EngineFile(engine, get_system_health_monitoring_config_file_path(engine)) as config_file:
            with allure.step("Update monitoring config file with my_checker file"):
                config_dict = config_file.json_read()
                config_dict[USER_DEFINED_CHECKERS_KEY].append(f'python {checker_file.path}')
                config_file.json_overwrite(config_dict)

            yield


def simulate_and_fix_asic_component_error(engines, devices):
    """
    Simulate and fix an ASIC temperature error via STATE_DB (same approach as QA unit test).
    Stops thermalctld, injects high temperature for ASIC1, waits for health cycle, then restores.

    Returns:
        str: The instance ID that was simulated (e.g., "ASIC1"), or None if DUT has no ASICs.
    """
    if getattr(devices.dut, 'asic_amount', 0) < 1:
        logger.info("DUT has no ASICs, skip ASIC component error simulation")
        return None
    system = System()
    health_out = OutputParsingTool.parse_json_str_to_dictionary(system.health.component.show()).get_returned_value()
    if HealthConsts.Component.ASIC not in health_out:
        logger.info("ASIC component not in health output, skip")
        return None
    asic_instances = health_out[HealthConsts.Component.ASIC][HealthConsts.Component.INSTANCE]
    if "ASIC1" not in asic_instances:
        logger.info("ASIC1 instance not present, skip")
        return None

    table_key = f"{_STATE_DB_TEMPERATURE_PREFIX}ASIC1"
    with allure.step("Stop thermalctld for ASIC temperature simulation"):
        engines.dut.run_cmd("docker exec pmon supervisorctl stop thermalctld")
    try:
        with allure.step("Inject ASIC1 high temperature via STATE_DB"):
            DatabaseTool.sonic_db_cli_hset(
                engines.dut, "", DatabaseConst.STATE_DB_NAME, table_key, "temperature", _ASIC_TEMP_UNHEALTHY)
        time.sleep(_HEALTH_POLL_WAIT_SEC)
        with allure.step("Restore ASIC1 temperature via STATE_DB"):
            DatabaseTool.sonic_db_cli_hset(
                engines.dut, "", DatabaseConst.STATE_DB_NAME, table_key, "temperature", _ASIC_TEMP_HEALTHY)
        time.sleep(_HEALTH_POLL_WAIT_SEC)
    finally:
        with allure.step("Start thermalctld after ASIC simulation"):
            engines.dut.run_cmd("docker exec pmon supervisorctl start thermalctld")
    return "ASIC1"


def simulate_and_fix_leakage_sensor_error(engines, devices):
    """
    Simulate and fix a leakage sensor error.

    Returns:
        str: The sensor instance ID that was simulated (e.g., "LEAKAGE-1", "LEAKAGE-2")
    """
    selected_sensor = random.choice(devices.dut.list_of_leakages)
    leakage_file = selected_sensor.replace("-", "").lower()
    engines.dut.run_cmd("sudo sh -c 'unlink {0}{1} && echo {2} > {0}{1}'".format(
        PlatformConsts.LEAKAGE_FILES_FOLDER, leakage_file, PlatformConsts.LEAK_STATUS_LEAK))
    time.sleep(10)
    ls_output = engines.dut.run_cmd("ls -la {}".format(PlatformConsts.LEAKAGE_FILES_FOLDER))
    leakage_folder_name = re.search(r'hwmon/([^/]+)/leakage', ls_output).group(1)
    engines.dut.run_cmd("sudo sh -c 'rm {0}{1}'".format(PlatformConsts.LEAKAGE_FILES_FOLDER, leakage_file))
    engines.dut.run_cmd("sudo sh -c 'ln -s {0}{1}/{2} {3}{2}'".format(
        PlatformConsts.LEAKAGE_FILES_SYSFS_FOLDER, leakage_folder_name, leakage_file,
        PlatformConsts.LEAKAGE_FILES_FOLDER))
    return selected_sensor


def validate_component_health_data(system, component_name, field_name, field_value):
    """
    Validate health component field value using the new nested structure.

    New API structure: health[component][instance][instance_id][field]
    Example: health["fan"]["instance"]["ALL"]["unhealthy-count"]

    Args:
        system: System object
        component_name: Component name (e.g., "fan", "cpu", "asic")
        field_name: Field to validate (e.g., "state", "unhealthy-count")
        field_value: Expected value
    """
    health_out = OutputParsingTool.parse_json_str_to_dictionary(system.health.component.show()).get_returned_value()
    # New structure: component -> instance -> instance_id -> fields
    component_data = health_out[component_name][HealthConsts.Component.INSTANCE]

    # For multi-instance components (leakage-sensor, asic), check all instances and find any matching value
    # For single-instance components (fan, psu, cpu), there's only one to check
    found_matching_instance = False
    for instance_id, instance_data in component_data.items():
        if instance_data.get(field_name) == field_value:
            found_matching_instance = True
            break

    if not found_matching_instance:
        # If no instance matched, fail with a clear message
        actual_values = {instance_id: instance_data.get(field_name) for instance_id, instance_data in component_data.items()}
        raise AssertionError(f"No instance of {component_name} has {field_name}='{field_value}'. Actual values: {actual_values}")


def validate_component_health_data_for_instance(system, component_name, instance_id, field_name, field_value):
    """
    Validate health component field value for a specific instance.

    Args:
        system: System object
        component_name: Component name (e.g., "leakage-sensor")
        instance_id: Specific instance ID (e.g., "LEAKAGE-1", "LEAKAGE-2")
        field_name: Field to validate (e.g., "state", "unhealthy-count")
        field_value: Expected value
    """
    health_out = OutputParsingTool.parse_json_str_to_dictionary(system.health.component.show()).get_returned_value()
    component_data = health_out[component_name][HealthConsts.Component.INSTANCE]

    if instance_id not in component_data:
        raise AssertionError(f"Instance {instance_id} not found in {component_name}")

    instance_data = component_data[instance_id]
    actual_value = instance_data.get(field_name)

    if actual_value != field_value:
        raise AssertionError(f"{component_name} instance {instance_id} has {field_name}='{actual_value}', expected '{field_value}'")


def validate_health_component_last_unhealthy_for_instance(system, component_name, instance_id, last_unhealthy):
    """Validate that last-unhealthy on a specific instance has advanced past last_unhealthy."""
    health_out = OutputParsingTool.parse_json_str_to_dictionary(system.health.component.show()).get_returned_value()
    component_data = health_out[component_name][HealthConsts.Component.INSTANCE]
    if instance_id not in component_data:
        raise AssertionError(f"Instance {instance_id} not found in {component_name}")
    updated = component_data[instance_id].get(HealthConsts.Component.LAST_HEALTHY, "")
    assert updated and updated >= last_unhealthy, \
        f"{component_name} {instance_id} last-unhealthy not updated (was '{last_unhealthy}', is '{updated}')"
    return updated


def validate_health_fix_or_issue(engines, system, health_issue_dict, search_since_datetime, is_fix, expected_in_monitor_list=True):
    if TestToolkit.devices.dut.is_eth():
        validate_health_fix_or_issue_for_cumulus(engines, system, health_issue_dict, search_since_datetime, is_fix, expected_in_monitor_list)
    else:
        validate_health_fix_or_issue_for_ib(engines, system, health_issue_dict, search_since_datetime, is_fix, expected_in_monitor_list)


def validate_health_fix_or_issue_for_ib(engines, system, health_issue_dict, search_since_datetime, is_fix, expected_in_monitor_list=True):
    """
    validate health issue or fix with show commands
        - validate with system show cmd the health status
        - validate with health detailed report
        - validate with health history file the status and the issues
        - validate system log indicates that health status has changed
    """
    status = OK if is_fix else NOT_OK
    regex = HealthConsts.HEALTH_FIX_REGEX if is_fix else HealthConsts.HEALTH_ISSUE_REGEX
    # normalize health_issue_dict values to be sets of strings
    health_issue_dict = {k: ({v} if isinstance(v, str) else set(v)) for k, v in health_issue_dict.items()}

    with allure.step("Validate health issues {}".format("fix" if is_fix else "")):
        system.wait_until_health_status_change_to(status)

        with allure.independent_step("Validate health output issues"):
            health_output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
            verify_health_status_and_led(system, status, health_output)
            health_issues = health_output[HealthConsts.ISSUES]
            if is_fix:
                assert not (health_issue_dict.keys() & health_issues.keys()), (
                    f"Expected none of these health issues: {list(health_issue_dict.keys())}\n"
                    f"But got the following issues: {list(health_issues.keys())}"
                )
            else:
                assert health_issue_dict.keys() <= health_issues.keys(), (
                    f"The following health issues are expected but missing: {health_issue_dict.keys() - health_issues.keys()}"
                )
                verify_issues_in_health_output(health_issues, health_issue_dict, is_fae_output=False)

        if expected_in_monitor_list:
            with allure.independent_step("Validate detailed health report"):
                detail_health_output = OutputParsingTool.parse_json_str_to_dictionary(
                    Fae().health.show()).get_returned_value()
                verify_expected_health_status(detail_health_output, HealthConsts.STATUS, status)
                monitor_dict = sort_monitor_list(detail_health_output[HealthConsts.MONITOR_LIST])
                for component, issues in health_issue_dict.items():
                    if is_fix:
                        assert component not in monitor_dict[NOT_OK]
                    else:
                        assert component in monitor_dict[NOT_OK]
                        verify_issues_in_health_output(detail_health_output[HealthConsts.MONITOR_LIST],
                                                       health_issue_dict, is_fae_output=True)

        with allure.independent_step("Validate health history file"):
            # health_history_output = system.health.history.files.show("health_history | tail -1")
            health_history_output = engines.dut.run_cmd('nv show system health history files health_history | grep ""')
            assert system.health.history.get_last_status_from_health_file(
                health_history_output) == status, "Last status in the health report file is not {}, as we expect".format(status)
            assert len(TestToolkit.search_line_after_a_specific_date_time(
                HealthConsts.ADD_STATUS_TO_SUMMARY_REGEX + status, health_history_output,
                search_since_datetime)) > 0, "Didn't find health status in history file since time : {},\n" \
                                             "history:\n {}".format(search_since_datetime, health_history_output)
            for component, issues in health_issue_dict.items():
                issues_regex = "[" + "|".join(issues) + "]"
                assert len(TestToolkit.search_line_after_a_specific_date_time(
                    regex.format(time_regex=NvosConst.DATE_TIME_REGEX[0], component=component, issue=issues_regex),
                    health_history_output, search_since_datetime)) > 0

        with allure.independent_step("Validate health status change appears in system log"):
            exp_status = "Health status is {arg}ok".format(arg="" if is_fix else "not ")
            exp_summary = "HEALTH_SUMMARY_{arg}OK".format(arg="" if is_fix else "NOT_")
            log_output = system.log.file.show_log(param='| grep healthd', expected_str=exp_status)
            HealthConsts.SYSTEM_LOG_HEALTH_REGEX.format(status)
            regex_to_search = NvosConst.DATE_TIME_REGEX[0] + HealthConsts.SYSTEM_LOG_HEALTH_STATUS_REGEX.format(
                exp_status, exp_summary)
            assert len(TestToolkit.search_line_after_a_specific_date_time(regex_to_search,
                                                                          log_output, search_since_datetime)) > 0, \
                "Didn't find health status line in the system log since specific time :{}\nSystem Log:\n {}".format(
                    search_since_datetime, log_output)


def validate_health_fix_or_issue_for_cumulus(engines, system, health_issue_dict, search_since_datetime, is_fix, expected_in_monitor_list=True):
    """
    validate health issue or fix with show commands
        - validate with health history  the status and the issues
        - validate with system show cmd the health status
    """
    status = OK if is_fix else NOT_OK
    regex = HealthConsts.HEALTH_FIX_REGEX if is_fix else HealthConsts.HEALTH_ISSUE_REGEX
    # normalize health_issue_dict values to be sets of strings
    health_issue_dict = {k: ({v} if isinstance(v, str) else set(v)) for k, v in health_issue_dict.items()}
    with allure.step("Validate health issues {}".format("fix" if is_fix else "")):
        system.wait_until_health_status_change_to(status)
        with allure.independent_step("Validate health output issues"):
            health_output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
            health_issues = health_output[HealthConsts.ISSUES]
            if is_fix:
                assert not (health_issue_dict.keys() & health_issues.keys()), (
                    f"Expected none of these health issues: {list(health_issue_dict.keys())}\n"
                    f"But got the following issues: {list(health_issues.keys())}"
                )
            else:
                assert health_issue_dict.keys() <= health_issues.keys(), (
                    f"The following health issues are expected but missing: {health_issue_dict.keys() - health_issues.keys()}"
                )
                verify_issues_in_health_output(health_issues, health_issue_dict, is_fae_output=False)
    with allure.step("Validate \"nv show system health\" cmd"):
        if is_fix:
            system.validate_health_status(HealthConsts.OK)
        else:
            logger.info("Validate \"nv show system health\" cmd")
            health_output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
            ValidationTool.validate_all_values_exists_in_list([HealthConsts.STATUS, HealthConsts.STATUS_LED], health_output.keys()).verify_result()
            system.validate_health_status(HealthConsts.NOT_OK)
            verify_health_status_and_led(system, HealthConsts.NOT_OK)
