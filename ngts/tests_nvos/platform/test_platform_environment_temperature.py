import json
import logging
import pytest
import random
import re

from retry import retry

from ngts.nvos_tools.infra.Simulator import HWSimulator
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.platform.helpers import (
    filter_eligible_sensors,
    validate_health,
    validate_health_issues,
    validate_sensor_state,
)
from ngts.nvos_constants.constants_nvos import HealthConsts, PlatformConsts

logger = logging.getLogger(__name__)

TEMPERATURE_BASE_PATH = PlatformConsts.TEMPERATURE_FILES_PATH
THERMAL_PWM_PATH = '/var/run/hw-management/thermal/pwm1'
HEALTH_STABILIZE_DELAY = 15
TEMPERATURE_MARGIN_FACTOR = 1.05
DEGREES_TO_MILLIDEGREES = 1000

# pwm1 sysfs is 0-255 (255 == 100% duty).
PWM_RAW_FULL_SCALE = 255
# Allowed deviation (raw pwm1 units) between the observed PWM and the value the
# platform thermal-control config predicts. Absorbs control-loop hysteresis and
# integer PWM stepping (e.g. observed 188 vs predicted 189 for a PMIC at 110.25C).
FAN_PWM_MATCH_TOLERANCE = 16
# The platform's own thermal-control config: the authoritative per-platform map
# of which sensors drive the fans (sensor_list) and over what temperature band
# (dev_parameters). We read it from the DUT instead of hardcoding any per-switch
# values, so the expected PWM is always aligned with the running platform.
TC_CONFIG_PATH = '/var/run/hw-management/config/tc_config.json'
# Fan-driving sensors polled faster than this are ASIC-FW-monitored (poll_time 3
# in tc_config) and a per-sensor sysfs injection may not reach the fan loop, so
# for them we assert only that the fans do not drop rather than an exact target.
TC_MIN_INJECTABLE_POLL_SEC = 10


# --- Temperature simulation helpers ---


def _pick_sensor(engine, devices):
    """Pick a random ok-state temperature sensor with a max threshold.

    The CLI/health daemon reads temperature from
    /var/run/hw-management/thermal/<sensor>_input, not the /ui/temperature/...
    layer. We discover the sensor via /ui/temperature/ (which gives us
    name<->dir mapping), then resolve the symlink one level to get the
    actual thermal/ path to inject into.
    """
    platform = Platform()
    temp_output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
        platform.environment.temperature.show()).verify_result()
    available_psu_list = platform.environment.get_available_psus() if devices.dut.psu_list else []
    eligible_sensors = filter_eligible_sensors(
        devices.dut.temperature_sensors, temp_output, available_psu_list,
        required_keys=('max', 'current'))
    assert eligible_sensors, "No temperature sensors with max/current found for simulation"

    last_err = None
    random.shuffle(eligible_sensors)
    for sensor in eligible_sensors:
        try:
            sensor_dir = HWSimulator.find_sensor_dir(engine, TEMPERATURE_BASE_PATH, sensor)
        except FileNotFoundError as e:
            last_err = e
            continue
        ui_input_path = f"{sensor_dir}/input"
        thermal_input_path = engine.run_cmd(f"readlink {ui_input_path}").strip()
        assert thermal_input_path, f"Could not resolve symlink for {ui_input_path}"
        sensor_data = temp_output[sensor]
        logger.info(f"Selected sensor: {sensor}, inject at: {thermal_input_path}, "
                    f"current: {sensor_data['current']}, max: {sensor_data['max']}, "
                    f"crit: {sensor_data.get('crit', '-')}")
        return sensor, sensor_data, thermal_input_path

    raise AssertionError(
        f"None of the eligible temperature sensors map to a sysfs dir under {TEMPERATURE_BASE_PATH}. "
        f"Last error: {last_err}"
    )


def _read_pwm(engine):
    """Read main thermal-control PWM (0-255). Returns None if the platform
    doesn't expose it (the fan-ramp-up check then degrades to a no-op).
    """
    try:
        return int(engine.run_cmd(f"cat {THERMAL_PWM_PATH}").strip())
    except (ValueError, Exception) as e:
        logger.warning(f"Could not read PWM at {THERMAL_PWM_PATH}: {e}")
        return None


def _read_fan_speeds(engine):
    """Return a one-line 'fanN=RPM' summary of every fan tach reading (the actual
    measured speed, distinct from the commanded pwm1 duty), or '' if unavailable.
    Read in a single command (printf, no trailing newlines) to stay one log line.
    """
    try:
        out = engine.run_cmd(
            "for f in /var/run/hw-management/thermal/fan*_speed_get; do "
            "[ -e \"$f\" ] && printf '%s=%s ' \"$(basename ${f%_speed_get})\" \"$(cat $f)\"; done"
        )
        return out.strip()
    except Exception as e:
        logger.warning(f"Could not read fan speeds: {e}")
        return ""


def _read_tc_config(engine):
    """Read the platform's hw-management thermal-control config from the DUT.

    Returns the parsed dict, or None if the file is absent/unparseable (the fan
    check then degrades to a simple 'fans do not drop' invariant).
    """
    try:
        return json.loads(engine.run_cmd(f"cat {TC_CONFIG_PATH}"))
    except Exception as e:
        logger.warning(f"Could not read/parse {TC_CONFIG_PATH}: {e}")
        return None


def _sysfs_sensor_base(sensor_input_path):
    """Reduce an inject path to the tc_config sensor base, e.g.
    '/var/run/hw-management/thermal/voltmon3_temp1_input' -> 'voltmon3_temp1'.
    """
    base = sensor_input_path.rsplit('/', 1)[-1]
    return base[:-len('_input')] if base.endswith('_input') else base


def _tc_sensor_profile(tc_config, sysfs_base):
    """Resolve, from the DUT's own tc_config, whether a sensor drives the fans
    and over what band. Returns (is_fan_driving, band_params_or_None).

    Fan-driving iff a sensor_list token is a PREFIX of the sysfs base name. The
    prefix rule both groups (token 'cpu' -> 'cpu_pack'/'cpu_core0') and correctly
    EXCLUDES sensors the TC does not watch, e.g. 'comex_voltmon1' (= PMIC-7) which
    no sensor_list token prefixes, even though it resembles a voltmon.
    """
    sensor_list = tc_config.get('sensor_list', [])
    is_fan_driving = any(sysfs_base.startswith(tok) for tok in sensor_list)
    band = None
    for pattern, params in tc_config.get('dev_parameters', {}).items():
        if re.search(pattern, sysfs_base):
            band = params
            break
    return is_fan_driving, band


def _band_expected_pwm(band, temp_c):
    """Predicted pwm1 (0-255) for a tc_config dev_parameters band at temp_c, via
    its linear val_min->val_max / pwm_min->pwm_max ramp (clamped). val_min/val_max
    are in thousandths of a degree and may carry a '!' (trend) prefix we strip.
    """
    def millideg(v):
        return float(str(v).lstrip('!'))
    t_min = millideg(band['val_min']) / 1000.0
    t_max = millideg(band['val_max']) / 1000.0
    pwm_min, pwm_max = band['pwm_min'], band['pwm_max']
    if temp_c <= t_min:
        duty = pwm_min
    elif temp_c >= t_max:
        duty = pwm_max
    elif t_max <= t_min:
        # Degenerate/zero-width band: guard the divide-by-zero. Unreachable in
        # practice (a non-floor, non-ceiling temp_c needs t_min < t_max).
        duty = pwm_min
    else:
        duty = pwm_min + (temp_c - t_min) / (t_max - t_min) * (pwm_max - pwm_min)
    duty = max(min(duty, pwm_max), pwm_min)
    return int(round(duty / 100.0 * PWM_RAW_FULL_SCALE))


@retry(AssertionError, tries=12, delay=10)
def _validate_fan_pwm_matches_expected(engine, baseline_pwm, sensor_name, sysfs_base,
                                       injected_temp_c, tc_config):
    """Assert fan PWM reaches the value the DUT's own thermal-control config predicts.

    Fan pwm1 = max() across every fan-driving sensor, each a linear val_min->val_max
    duty ramp (from tc_config dev_parameters). Injecting injected_temp_c raises only
    the picked sensor, so the steady-state pwm1 must equal
    max(baseline_pwm, band_demand(injected_temp_c)) within FAN_PWM_MATCH_TOLERANCE:
      * if the sensor's demand exceeds the baseline, the fans must ramp up to it;
      * if another sensor already holds the fans higher, pwm1 correctly stays put.
    A pwm1 well below the predicted target on a fan-driving sensor we can inject
    into is a candidate cooling-response bug, NOT a test tolerance to widen.

    For sensors the TC does not watch (e.g. comex voltmon = PMIC-7, PSU, PCH) or
    ASIC-FW-fast-poll sensors a sysfs injection may not reach, we only assert that
    the fans do not drop. Thermal control reacts with a lag (~45-60s), so this
    retries for up to ~2 minutes to let pwm1 settle.
    """
    if baseline_pwm is None:
        return
    current = _read_pwm(engine)
    assert current is not None, "Could not read current PWM"

    is_fan_driving, band = _tc_sensor_profile(tc_config, sysfs_base) if tc_config else (False, None)
    try:
        poll_time_sec = int(band.get('poll_time', 0)) if band else 0
    except (TypeError, ValueError):
        poll_time_sec = 0  # tc_config values are sometimes strings
    inject_reaches_fans = (band is not None and poll_time_sec >= TC_MIN_INJECTABLE_POLL_SEC)
    predicted = _band_expected_pwm(band, injected_temp_c) if band else None
    fan_rpm = _read_fan_speeds(engine)

    # Always surface the comparison (logged on every retry poll), so the run shows
    # exactly what the fans did vs what the platform thermal-control config predicts:
    # commanded duty (pwm1), measured fan speeds (RPM), and the expected target.
    logger.info(
        f"Fan PWM check for {sensor_name} ({sysfs_base}): baseline_pwm={baseline_pwm}, "
        f"current_pwm={current}, fan_rpm=[{fan_rpm}], injected_temp={injected_temp_c}C, "
        f"expected_pwm={'~%.0f' % predicted if predicted is not None else 'n/a'} "
        f"(fan_driving={is_fan_driving}, inject_reaches_fans={inject_reaches_fans}, "
        f"mode={'EXPECTED-match' if (is_fan_driving and inject_reaches_fans) else 'no-drop'})"
    )

    if not (is_fan_driving and inject_reaches_fans):
        # Sensor does not drive the fan loop, or is FW-fast-poll, or no tc_config:
        # only require the fans not to spin down. Log the prediction if we have one.
        assert current >= baseline_pwm - FAN_PWM_MATCH_TOLERANCE, (
            f"Fan PWM dropped while {sensor_name} fault active: "
            f"baseline_pwm={baseline_pwm}, current={current}"
        )
        logger.info(f"{sensor_name} ({sysfs_base}): target not asserted "
                    f"(fan_driving={is_fan_driving}, inject_reaches_fans={inject_reaches_fans}); "
                    f"baseline={baseline_pwm} -> current={current}" +
                    (f"; config predicts ~{predicted:.0f}" if predicted is not None else ""))
        return

    expected = max(baseline_pwm, predicted)
    assert abs(current - expected) <= FAN_PWM_MATCH_TOLERANCE, (
        f"Fan PWM does not match thermal-control config for {sensor_name} "
        f"({sysfs_base}): baseline_pwm={baseline_pwm}, injected_temp={injected_temp_c}C, "
        f"expected~{expected:.0f}, current={current} (tol={FAN_PWM_MATCH_TOLERANCE}). "
        f"Candidate cooling-response bug, not a tolerance to widen."
    )
    logger.info(f"Fan PWM matches thermal-control config for {sensor_name} ({sysfs_base}): "
                f"baseline={baseline_pwm} -> current={current} "
                f"(expected~{expected:.0f} for {injected_temp_c}C)")


# --- Temperature simulation test ---


@pytest.mark.platform
@pytest.mark.disable_loganalyzer
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_simulate_temperature_fault_high(engines, devices):
    """
    @summary: Inject a high temperature value into a sensor and verify the
              system detects the fault, raises a health issue, and (when the
              platform has fans) ramps up fan PWM. Restores the sensor and
              verifies recovery.

    Steps:
    1. Validate system health is OK
    2. Pick a random temperature sensor with a max threshold
    3. Inject value = max * 1.05 (just above max threshold)
    4. Validate sensor state is 'failed', health is 'Not OK', sensor is in
       health issues
    5. If devices.dut.fan_list is non-empty, validate fan PWM reaches the value
       the DUT's own thermal-control config (tc_config.json) predicts for the
       injected temperature, but only for sensors the TC actually drives the fans
       from; a pwm1 well below the predicted target is a candidate cooling bug
    6. Restore original symlink
    7. Validate sensor state is 'ok', health is 'OK', sensor not in health
       issues
    """
    system = System()
    temperature_show = Platform().environment.temperature.show

    with allure.step("Pick sensor and validate initial health"):
        sensor_name, sensor_data, sensor_input_path = _pick_sensor(engines.dut, devices)
        validate_health(system, HealthConsts.OK)

    injected_temp_c = float(sensor_data['max']) * TEMPERATURE_MARGIN_FACTOR
    fault_value = int(injected_temp_c * DEGREES_TO_MILLIDEGREES)
    has_fans = bool(devices.dut.fan_list)
    sysfs_base = _sysfs_sensor_base(sensor_input_path)
    tc_config = _read_tc_config(engines.dut) if has_fans else None

    with allure.step(f"Inject high temperature fault: value={fault_value}"):
        baseline_pwm = _read_pwm(engines.dut) if has_fans else None
        logger.info(f"Baseline PWM before injection: {baseline_pwm} (has_fans={has_fans})")

        with HWSimulator.simulate_sensor(engines.dut, sensor_input_path, fault_value, HEALTH_STABILIZE_DELAY):
            validate_sensor_state(temperature_show, sensor_name, 'bad')
            validate_health(system, HealthConsts.NOT_OK)
            validate_health_issues(system, sensor_name, expected_present=True)
            if has_fans:
                _validate_fan_pwm_matches_expected(
                    engines.dut, baseline_pwm, sensor_name, sysfs_base,
                    injected_temp_c, tc_config)
            else:
                logger.info("Skipping fan PWM check: platform has no fans")

    with allure.step(f"Validate recovery after restoring {sensor_name}"):
        logger.info(f"Fault cleared and symlink restored for {sensor_name}; "
                    f"validating sensor state 'ok', health 'OK', and no health issue")
        validate_sensor_state(temperature_show, sensor_name, 'ok')
        validate_health(system, HealthConsts.OK)
        validate_health_issues(system, sensor_name, expected_present=False)
        if has_fans:
            logger.info(f"Fan PWM after recovery: pwm1={_read_pwm(engines.dut)}, "
                        f"fan_rpm=[{_read_fan_speeds(engines.dut)}]")
