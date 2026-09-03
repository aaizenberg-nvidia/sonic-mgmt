from typing import Callable, Iterable
import logging
import retry
import re

from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, DelayedRecovery, InterfaceConsts
from ngts.nvos_constants.constants_nvos import HealthConsts, LogsSources  # noqa: F401
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.Fae import Fae
from ngts.ngts_types import EnginesT
import time

logger = logging.getLogger(__name__)


def _pre_port_config(ports: list[Port]) -> list[dict]:
    """
    in this function we will configure the port with the following:
    - description
    - delayed recovery state
    - delayed recovery retry threshold
    - link phy-recovery serdes-eq-mode - enabled
    :param port: the port to configure
    :return: the output of the show command
    """
    with allure.step("Apply link configurations to selected ports"):
        show_ports_output = []
        for port in ports:
            with allure.step(f"configuration for port {port}"):
                fae = Fae(port_name=port.name)
                port.interface.set(InterfaceConsts.DESCRIPTION, "testing").verify_result()
                fae.interface.link.delayed_recovery.set(DelayedRecovery.DELAYED_RECOVERY_STATE, "enabled").verify_result()
                fae.interface.link.delayed_recovery.set(
                    DelayedRecovery.DELAYED_RECOVERY_RETRY_TH, 200, apply=True, ask_for_confirmation=True
                ).verify_result()
                # fae.interface.link.phy_recovery.set("serdes-eq-mode", "enabled", apply=True), we need to add more configurations here - NVL and IB
            time.sleep(5)
            with allure.step(f"run show fae interface link for {port}"):
                show_ports_output.append(OutputParsingTool.parse_json_str_to_dictionary(fae.interface.link.show()).get_returned_value())
        return show_ports_output


def _post_port_config(show_ports_output: list[dict], ports: list[Port], ignore_fields: list[str] = None) -> list[dict]:
    diff_result = []
    with allure.step("verify link configurations"):
        for output, port in zip(show_ports_output, ports):
            fae = Fae(port_name=port.name)
            current_output = OutputParsingTool.parse_json_str_to_dictionary(fae.interface.link.show()).get_returned_value()
            with allure.independent_step(f"verify output for {port}"):
                if output != current_output:
                    diff_only = ValidationTool._compute_dict_diff(output, current_output, ignore_fields=[IbInterfaceConsts.LINK_ROUND_TRIP_LATENCY])
                    if diff_only:
                        diff_result.append({"port": port.name, "diff": diff_only})
        assert not diff_result, f"some ports are not configured as expected, diff: {diff_result}"

    with allure.step("unset link configurations"):
        for port in ports:
            fae = Fae(port_name=port.name)
            port.interface.unset(apply=True, ask_for_confirmation=True).verify_result()
            fae.interface.unset(apply=True, ask_for_confirmation=True).verify_result()
        return diff_result


# --- Environment sensor fault-simulation helpers ---
# Shared by test_platform_environment_voltage.py and
# test_platform_environment_temperature.py.


def normalize_sensor_name(name: str) -> str:
    """Lowercase and strip non-alphanumeric characters. Used to match CLI
    sensor names (hyphenated, e.g. 'PMIC-3-ASIC1-DVDD-PL1-Out-2') against
    health-issue keys (space-separated) and sysfs directory names
    (plus-separated)."""
    return re.sub(r'[^a-z0-9]', '', name.lower())


def is_sensor_for_absent_psu(sensor: str, available_psu_list: Iterable[str]) -> bool:
    """True if `sensor` belongs to a PSU that is not currently installed."""
    match = re.search(r"PSU-(\d+)-.*", sensor)
    if match is None:
        return False
    return f"PSU{match.group(1)}" not in available_psu_list


def find_sensor_in_health_issues(sensor_name: str, health_issues: dict) -> str | None:
    """Find a sensor in the health issues dict, accounting for CLI-vs-issue
    name format differences. Returns the matching issue key, or None."""
    target = normalize_sensor_name(sensor_name)
    for key in health_issues:
        if normalize_sensor_name(key) == target:
            return key
    return None


def filter_eligible_sensors(sensor_names: Iterable[str],
                            sensor_output: dict,
                            available_psu_list: Iterable[str],
                            required_keys: Iterable[str]) -> list[str]:
    """Return sensors that are 'ok', expose every key in `required_keys`, and
    don't belong to an absent PSU."""
    return [
        s for s in sensor_names
        if sensor_output.get(s, {}).get('state') == 'ok' and
        all(k in sensor_output.get(s, {}) for k in required_keys) and
        not is_sensor_for_absent_psu(s, available_psu_list)
    ]


@retry.retry(AssertionError, tries=6, delay=10)
def validate_health(system: System, expected: str) -> None:
    """Retry-wrapped wrapper around `system.validate_health_status`."""
    system.validate_health_status(expected)


@retry.retry(AssertionError, tries=6, delay=10)
def validate_sensor_state(show_fn: Callable[[], str], sensor_name: str, expected_state: str) -> None:
    """Validate a sensor's `state` field. `show_fn` is the bound
    `nv show platform environment <kind>` callable (e.g.
    `Platform().environment.voltage.show`)."""
    output = Tools.OutputParsingTool.parse_json_str_to_dictionary(show_fn()).verify_result()
    actual = output.get(sensor_name, {}).get('state', '')
    assert actual == expected_state, (
        f"Sensor '{sensor_name}': expected state='{expected_state}', got '{actual}'"
    )


@retry.retry(AssertionError, tries=6, delay=10)
def validate_health_issues(system: System, sensor_name: str, *, expected_present: bool) -> None:
    """Validate that a sensor appears (or not) in the health issues dict."""
    health_output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
        system.health.show()).get_returned_value()
    health_issues = health_output.get(HealthConsts.ISSUES, {})
    matched_key = find_sensor_in_health_issues(sensor_name, health_issues)
    if expected_present:
        assert matched_key is not None, (
            f"Expected sensor '{sensor_name}' in health issues, "
            f"but got: {list(health_issues.keys())}"
        )
    else:
        assert matched_key is None, (
            f"Expected sensor '{sensor_name}' NOT in health issues, "
            f"but it is still present as '{matched_key}': {health_issues.get(matched_key)}"
        )


def validate_invalid_voltage_value_logged(system: System, engine: EnginesT) -> None:
    """Verify syslog contains evidence that an unreadable voltage value was detected.

    Called after a non-numeric (gibberish) value is injected.  Two messages are
    expected:
      1. healthd logs that the sensor reading is unavailable.
      2. health-statsd logs the out-of-range event with voltage=N/A.
    """
    with allure.step("Verify syslog contains evidence of unreadable voltage value"):
        system.log.verify_expected_logs(
            logs_to_find=[
                'Sensor voltage reading is not available',
                'voltage=N/A',
            ],
            logs_source=LogsSources.SYSLOG,
            engine=engine,
            only_latest_log=True,
        )
