import logging
import math
import random
import time
from pathlib import Path

from ngts.tools.test_utils import allure_utils as allure
import pytest
from retry.api import retry_call

from ngts.ngts_types import DevicesT, EnginesT
from ngts.nvos_constants.constants_nvos import AlarmsConsts, DatabaseConst, NvosConst, TelemetryConsts
from ngts.nvos_tools.infra.DatabaseTool import DatabaseTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.Simulator import HWSimulator
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.helpers.alarms.system_alarm_nvue_helpers import (alarm_resource_matches_sensor,
                                                                      parse_dut_time, validate_nvue_alarm,
                                                                      validate_nvue_alarms)
from ngts.tests_nvos.helpers.alarms.system_alarms_gnmi_helpers import get_dut_time_ref
from ngts.tests_nvos.helpers.alarms.system_alarms_otel_helpers import (ACTIVE_VALUE, CLEARED_VALUE,
                                                                       METRIC_BY_ALARMS_CLASS, OtelAlarmSnapshot,
                                                                       alarms_collection_window_sec,
                                                                       assert_active_alarm_counts_consistent,
                                                                       enable_otel_alarms_export,
                                                                       get_otel_alarms, otel_active_alarm_count,
                                                                       otel_alarms_matching, parse_otel_alarms,
                                                                       set_otel_alarms_class,
                                                                       unset_otel_alarms_classes,
                                                                       validate_otel_alarms)
from ngts.tests_nvos.system.telemetry.otel.constants import OtelCollectorConst
from ngts.tests_nvos.system.telemetry.otel.helpers import read_otel_collector_documents
from ngts.tests_nvos.system.telemetry.otel.nvos_cli_helpers import set_leaf, show_applied, verify_leaf
from ngts.tests_nvos.system.telemetry.otel.otel_collector import OtelCollector
from ngts.tests_nvos.helpers.alarms.helpers import get_fan_drawers, validate_docker_is_up, NS_PER_SEC

logger = logging.getLogger(__name__)

# Long enough for the fan fault to be raised and cleared between two samples.
SAMPLE_INTERVAL_SEC = 5

# One event per alarms class: a stopped container raises a software alarm on its
# processes, a missing fan a platform alarm.
SOFTWARE_DOCKER = 'lldp'
SOFTWARE_ALARM = AlarmsConsts.PROCESS_NOT_RUNNING
SOFTWARE_CLEARED_ALARMS = [AlarmsConsts.PROCESS_NOT_RUNNING, AlarmsConsts.CONTAINER_NOT_RUNNING]
PLATFORM_ALARM = AlarmsConsts.DEVICE_MISSING

# Enough samples for an enabled class to export its event several times, so a
# disabled class exporting nothing is not down to a short window.
TRIGGERED_WINDOW_SAMPLES = 3

# Slack on the timing checks: NVUE time-created has a one-second resolution and a
# sample tick jitters. Well under the gap between the two intervals compared.
CLASS_INTERVAL_TOLERANCE_SEC = 5

_ENABLED = TelemetryConsts.State.ENABLED.value
_DISABLED = TelemetryConsts.State.DISABLED.value


def _fan_values(snapshot: OtelAlarmSnapshot, fan: str, alarm_type: str) -> list[float]:
    """The values the export reported for the fan's alarm, in ascending (oldest to newest) timestamp order."""
    for entries in snapshot.series.values():
        if entries[-1].alarm_type == alarm_type and alarm_resource_matches_sensor(entries[-1].resource, fan):
            return [entry.value for entry in entries]
    return []


def _trigger_and_clear(engines: EnginesT, system: System, collector: OtelCollector, local_dir: str,
                       thermal_directory: str, drawer_id: int, fan: str, samples: int, label: str, alarm_type: str,
                       poll_sec: int = 2, sample_interval_sec: int = None) -> list[float]:
    """Raise and clear the fan alarm while capturing, then capture `samples` more samples.
    `poll_sec` is how often NVUE is polled for the raise; the alarm is cleared right after it shows,
    so a short `poll_sec` keeps the alarm raised for as little of the sample-interval as possible."""
    collector.truncate_artifact()
    symlink = HWSimulator.simulate_fan_missing_fault(engines.dut, thermal_directory, drawer_id)
    try:
        retry_call(lambda: validate_nvue_alarm(system.alarms.parse_show(), alarm_type, fan, True),
                   exceptions=AssertionError, tries=60 // poll_sec, delay=poll_sec)
    finally:
        HWSimulator.simulate_fix_fan_missing_fault(engines.dut, thermal_directory, drawer_id, symlink)
    retry_call(lambda: validate_nvue_alarm(system.alarms.parse_show(), alarm_type, fan, False),
               exceptions=AssertionError, tries=30, delay=2)
    sleep = alarms_collection_window_sec(samples, sample_interval_sec)
    time.sleep(sleep)
    local_json = collector.fetch_artifact_if_any(local_dir, file_name=f"otel-out-{label}.json")
    assert local_json, "The collector wrote no artifact"
    values = _fan_values(parse_otel_alarms(read_otel_collector_documents(local_json)), fan, alarm_type)
    logger.info(f"OTEL {alarm_type} values of {fan}: {values}")
    return values


def _class_alarms(fan: str) -> dict[str, tuple[str, str]]:
    """The origin name and the alarm type of the event each alarms class exports."""
    return {TelemetryConsts.ALARMS_CLASS_SOFTWARE: (SOFTWARE_DOCKER, SOFTWARE_ALARM),
            TelemetryConsts.ALARMS_CLASS_PLATFORM: (fan, PLATFORM_ALARM)}


def _set_docker_auto_restart(dut, status: str) -> None:
    DatabaseTool.sonic_db_cli_hset(engine=dut, asic="", db_name=DatabaseConst.CONFIG_DB_NAME,
                                   db_config=f"FEATURE|{SOFTWARE_DOCKER}",
                                   param=NvosConst.DOCKER_AUTO_RESTART, value=status)


def _validate_nvue_events(system: System, fan: str, *, expected_present: bool) -> None:
    """Wait for NVUE to show, or to drop, both the software and the platform alarm."""
    software_alarms = SOFTWARE_ALARM if expected_present else SOFTWARE_CLEARED_ALARMS
    with allure.step(f"Wait for NVUE to {'show' if expected_present else 'clear'} both alarms"):
        validate_nvue_alarms(system, SOFTWARE_DOCKER, software_alarms, expected_present=expected_present)
        validate_nvue_alarms(system, fan, PLATFORM_ALARM, expected_present=expected_present)


def _choose_fan(engines: EnginesT, devices: DevicesT, system: System) -> tuple[str, int, str]:
    """Choose the fan the platform event is raised on, and verify the class tests'
    precondition: suppress-zeroes is enabled and neither alarm is active.
    Returns the thermal directory, the fan drawer and the fan."""
    with allure.step("Precondition: choose a fan, verify suppress-zeroes is enabled and both alarms are inactive"):
        thermal_directory = devices.dut.fan_direction_dir
        fan_drawers = get_fan_drawers(OutputParsingTool.parse_json_str_to_dictionary(
            Platform().environment.fan.show()).verify_result())
        if not thermal_directory or not fan_drawers:
            pytest.skip("System has no fans to simulate a fault on. Skipping")
        if SOFTWARE_DOCKER not in engines.dut.run_cmd("docker ps"):
            pytest.skip(f"The {SOFTWARE_DOCKER} container is not running to be stopped. Skipping")
        drawer_id = random.choice(sorted(fan_drawers))
        fan = sorted(fan_drawers[drawer_id])[0]
        verify_leaf(show_applied(system.telemetry.alarms.export.suppress_zeroes, engines.dut),
                    TelemetryConsts.STATE, _ENABLED)
        _validate_nvue_events(system, fan, expected_present=False)
        return thermal_directory, drawer_id, fan


def _trigger_events(dut, thermal_directory: str, drawer_id: int) -> str:
    """Stop the software container, its auto-restart disabled so it stays down, and
    simulate the fan missing. Returns the fan file's symlink target to restore."""
    with allure.step(f"Stop the {SOFTWARE_DOCKER} container and simulate fan drawer {drawer_id} missing"):
        _set_docker_auto_restart(dut, NvosConst.DOCKER_STATUS_DISABLED)
        dut.run_cmd(f"docker stop {SOFTWARE_DOCKER}")
        return HWSimulator.simulate_fan_missing_fault(dut, thermal_directory, drawer_id)


def _restore_events(dut, thermal_directory: str, drawer_id: int, symlink: str | None) -> None:
    """Restore the fan, where it was faulted, and start the software container again."""
    if symlink:
        HWSimulator.simulate_fix_fan_missing_fault(dut, thermal_directory, drawer_id, symlink)
    with allure.step(f"Start the {SOFTWARE_DOCKER} container"):
        dut.run_cmd(f"docker start {SOFTWARE_DOCKER}")
        _set_docker_auto_restart(dut, NvosConst.DOCKER_STATUS_ENABLED)
        validate_docker_is_up(dut, SOFTWARE_DOCKER)


def _clear_events(dut, system: System, thermal_directory: str, drawer_id: int, fan: str, symlink: str) -> None:
    _restore_events(dut, thermal_directory, drawer_id, symlink)
    _validate_nvue_events(system, fan, expected_present=False)


def _validate_otel_classes(collector: OtelCollector, local_dir: str, fan: str, enabled: list[str],
                           label: str) -> None:
    """With both events raised, each `enabled` class shows its event and every other
    class shows no event and counts nothing; each class's counter equals its active alarms."""
    alarms = get_otel_alarms(collector, local_dir, samples=TRIGGERED_WINDOW_SAMPLES, label=label)
    for alarm_class, (origin, alarm_type) in _class_alarms(fan).items():
        shown = alarm_class in enabled
        with allure.step(f"Validate the {alarm_class} event {'shows' if shown else 'does not show'}"):
            validate_otel_alarms(collector, local_dir, origin, alarm_type, expected_present=shown, snapshot=alarms)
            if not shown:
                count = otel_active_alarm_count(alarms, METRIC_BY_ALARMS_CLASS[alarm_class])
                assert count == 0, f"Expected the disabled {alarm_class} class to count nothing, got {count}"
    assert_active_alarm_counts_consistent(alarms)


def _raised_at_ns(system: System, fan: str) -> dict[str, int]:
    """When NVUE says each event was raised, per class, in epoch nanoseconds on the
    DUT clock the OTLP samples are stamped with. The earliest row counts where one
    event raised the alarm on several resources."""
    alarms = system.alarms.parse_show()
    time_ref = get_dut_time_ref(system)
    raised = {}
    for alarm_class, (origin, alarm_type) in _class_alarms(fan).items():
        rows = alarms[AlarmsConsts.COLLECTION_BY_TYPE.get(alarm_type, AlarmsConsts.COMPONENT_ALARMS)]
        created = [parse_dut_time(row[AlarmsConsts.TIME_CREATED]) for row in rows.values()
                   if row.get(AlarmsConsts.TYPE) == alarm_type and
                   alarm_resource_matches_sensor(row.get(AlarmsConsts.RESOURCE, ''), origin)]
        assert created, f"Expected NVUE to show the {alarm_type} alarm of '{origin}', got: {rows}"
        raised[alarm_class] = time_ref.epoch_ns + int((min(created) - time_ref.local).total_seconds() * NS_PER_SEC)
    return raised


def _export_timings(collector: OtelCollector, local_dir: str, system: System, fan: str,
                    label: str) -> dict[str, tuple[float | None, float | None]]:
    """Once both events are raised on a freshly truncated collector, keep collecting
    until one default interval (plus the tolerance) past the later raise, then time
    each class's event: seconds from its raise to its first export, and between its
    two closest samples; None where there is nothing to measure."""
    raised = _raised_at_ns(system, fan)
    until_ns = max(raised.values()) + (TelemetryConsts.Defaults.ALARMS_SAMPLE_INTERVAL +
                                       CLASS_INTERVAL_TOLERANCE_SEC) * NS_PER_SEC
    wait_sec = max(0, math.ceil((until_ns - get_dut_time_ref(system).epoch_ns) / NS_PER_SEC))
    wait_sec += OtelCollectorConst.OTEL_COLLECTION_WARM_UP_SEC
    with allure.step(f"OTEL: keep collecting alarm samples for another {wait_sec}s"):
        time.sleep(wait_sec)
        local_json = collector.fetch_artifact_if_any(local_dir, file_name=f"otel-out-{label}.json")
    snapshot = parse_otel_alarms(read_otel_collector_documents(local_json)) if local_json else OtelAlarmSnapshot()
    timings = {}
    for alarm_class, (origin, alarm_type) in _class_alarms(fan).items():
        firsts, gaps = [], []
        for key in otel_alarms_matching(snapshot, alarm_type, origin):
            offsets = sorted({(entry.timestamp_ns - raised[alarm_class]) / NS_PER_SEC
                              for entry in snapshot.series[key] if entry.is_active})
            firsts += offsets[:1]
            gaps += [later - earlier for earlier, later in zip(offsets, offsets[1:])]
        timings[alarm_class] = (min(firsts, default=None), min(gaps, default=None))
    logger.info(f"OTEL export timings (first export after the raise, closest samples) in seconds: {timings}")
    return timings


def _assert_export_timing(timings: dict[str, tuple[float | None, float | None]], alarm_class: str,
                          interval_sec: int) -> None:
    """The class's event was first exported within one interval of its raise, and
    is never exported more often than `interval_sec`."""
    first_sec, closest_sec = timings[alarm_class]
    assert first_sec is not None and first_sec <= interval_sec + CLASS_INTERVAL_TOLERANCE_SEC, (
        f"Expected the {alarm_class} event exported within {interval_sec}s (+{CLASS_INTERVAL_TOLERANCE_SEC}s) "
        f"of its raise, first export after: {first_sec}s")
    assert closest_sec is None or closest_sec >= interval_sec - CLASS_INTERVAL_TOLERANCE_SEC, (
        f"The {alarm_class} event is exported {closest_sec}s apart, more often than its {interval_sec}s interval")


@pytest.mark.system
@pytest.mark.disable_loganalyzer
def test_otel_suppress_zero(engines, devices, otel_alarms_collector, tmp_path, random_api):
    """
    Test Objective:
    Check that when an alarm is healthy -> unhealthy -> healthy between intervals of telemetry output,
    on the next check the metric will show that an alarm was raised. Check that the output on otel
    interval is 1 followed by 0, will show that an event was raised and is now off. Validate that the
    suppress-zero works as intended

    Precondition:
    The selected alarms to trigger should be inactive and suppress-zero is enabled (default is enabled
    but verify). OTEL should be enabled.

    #   Test steps                              Expected result
    1.  Will set the interval to X              OTEL should be set to emit every X value
    2.  Check that value emitted is 0           Value should be 0
    3.  Trigger alarm and turn it off           Alarm should switch on and off
    4.  Check in event triggered                Event should be shown with a 1 followed by delay-intervals
                                                0s to show that it was triggered between intervals
    5.  Disable suppress-zero
    6.  Trigger event and turn it off
    7.  Check in event trigger                  Should be showing that event was triggered, print 1
                                                Should continuously print 0 after event was triggered
    8.  Enable suppress-zero
    9.  Check event                             Should not be printing 0 anymore
    10. Change delay-intervals to a new value   New value should be applied
    11. Trigger event and turn it off
    12. Check in event triggered                Event should be shown with a 1 followed by the new
                                                delay-intervals 0s and no more exports
    13. Restore the original delay-intervals    Original value should be applied
    14. Trigger event within a sample-interval and turn it off during the same interval
    15. Check the exported values               Event should be shown with a 1 followed by exactly
                                                delay-intervals 0s, all of the zeros printed correctly
    """
    TestToolkit.update_engines(engines)
    dut = engines.dut
    system = System()
    local_dir = str(tmp_path)
    suppress_zeroes = system.telemetry.alarms.export.suppress_zeroes
    thermal_directory = devices.dut.fan_direction_dir
    alarm_type = AlarmsConsts.DEVICE_MISSING

    with allure.step("Precondition: choose a fan and verify suppress-zeroes is enabled"):
        fan_output = OutputParsingTool.parse_json_str_to_dictionary(
            Platform().environment.fan.show()).verify_result()
        fan_drawers = get_fan_drawers(fan_output)
        if not thermal_directory or not fan_drawers:
            pytest.skip("System has no fans to simulate a fault on. Skipping")
        drawer_id = random.choice(sorted(fan_drawers))
        fan = sorted(fan_drawers[drawer_id])[0]
        applied = show_applied(suppress_zeroes, dut)
        verify_leaf(applied, TelemetryConsts.STATE, TelemetryConsts.State.ENABLED.value)
        delay_intervals = int(applied.get(TelemetryConsts.DELAY_INTERVALS,
                                          TelemetryConsts.Defaults.ALARMS_DELAY_INTERVALS))

    try:
        with allure.step(f"1. Set the alarms export sample-interval to {SAMPLE_INTERVAL_SEC}s"):
            interval = enable_otel_alarms_export(dut, sample_interval=SAMPLE_INTERVAL_SEC)

        with allure.step("2. Check no alarm is triggered"):
            snapshot: OtelAlarmSnapshot = get_otel_alarms(otel_alarms_collector, local_dir, label='baseline')
            active = [entry for entries in snapshot.series.values() for entry in entries if entry.is_active]
            assert not active, f"Expected no active alarms, got {active}"

        with allure.step("3. Trigger the alarm and turn it off"):
            values = _trigger_and_clear(engines=engines, system=system, collector=otel_alarms_collector, local_dir=local_dir, thermal_directory=thermal_directory,
                                        drawer_id=drawer_id, fan=fan, samples=delay_intervals + 7, label='suppress-enabled', alarm_type=alarm_type)

        with allure.step(f"4. Check the event is shown as 1 followed by {delay_intervals} zeros"):
            assert ACTIVE_VALUE in values, f"Expected the {alarm_type} of {fan} to be exported as 1: {values}"
            tail = values[len(values) - values[::-1].index(ACTIVE_VALUE):]
            assert tail == [CLEARED_VALUE] * delay_intervals, (
                f"Expected 1 followed by {delay_intervals} zeros for {fan}, got {values}")

        with allure.step("5. Disable suppress-zeroes"):
            set_leaf(suppress_zeroes, dut, TelemetryConsts.STATE, TelemetryConsts.State.DISABLED.value,
                     apply=True)

        with allure.step("6. Trigger the alarm and turn it off"):
            values = _trigger_and_clear(engines, system, otel_alarms_collector, local_dir, thermal_directory,
                                        drawer_id, fan, delay_intervals + 3, 'suppress-disabled', alarm_type)

        with allure.step("7. Check the event is shown as 1 followed by continuous zeros"):
            assert ACTIVE_VALUE in values, f"Expected the {alarm_type} of {fan} to be exported as 1: {values}"
            tail = values[len(values) - values[::-1].index(ACTIVE_VALUE):]
            assert len(tail) > delay_intervals and set(tail) == {CLEARED_VALUE}, (
                f"Expected 1 followed by more than {delay_intervals} zeros for {fan}, got {values}")

        with allure.step("8. Enable suppress-zeroes"):
            set_leaf(suppress_zeroes, dut, TelemetryConsts.STATE, TelemetryConsts.State.ENABLED.value,
                     apply=True)

        with allure.step("9. Check the event is not printing 0 anymore"):
            time.sleep(delay_intervals * interval)
            values = _fan_values(get_otel_alarms(otel_alarms_collector, local_dir, samples=2,
                                                 label='suppress-enabled-again'), fan, alarm_type)
            assert not values, f"Expected {fan} to no longer be exported, got {values}"

        new_delay = delay_intervals + 2
        with allure.step(f"10. Change the suppress-zeroes delay-intervals to {new_delay}"):
            set_leaf(suppress_zeroes, dut, TelemetryConsts.DELAY_INTERVALS, new_delay, apply=True)
            verify_leaf(show_applied(suppress_zeroes, dut), TelemetryConsts.DELAY_INTERVALS, new_delay)

        with allure.step("11. Trigger the alarm and turn it off"):
            values = _trigger_and_clear(engines, system, otel_alarms_collector, local_dir, thermal_directory,
                                        drawer_id, fan, new_delay + 10, 'delay-changed', alarm_type)

        with allure.step(f"12. Check the event is shown as 1 followed by {new_delay} zeros"):
            assert ACTIVE_VALUE in values, f"Expected the {alarm_type} of {fan} to be exported as 1: {values}"
            tail = values[len(values) - values[::-1].index(ACTIVE_VALUE):]
            assert tail == [CLEARED_VALUE] * new_delay, (
                f"Expected 1 followed by {new_delay} zeros for {fan}, got {values}")

        new_delay = 2
        with allure.step(f"13. Change the suppress-zeroes delay-intervals to {new_delay}"):
            set_leaf(suppress_zeroes, dut, TelemetryConsts.DELAY_INTERVALS, new_delay, apply=True)
            verify_leaf(show_applied(suppress_zeroes, dut), TelemetryConsts.DELAY_INTERVALS, new_delay)

        with allure.step("14. Set the sample-interval to "
                         f"{SAMPLE_INTERVAL_SEC * 2}s, trigger the alarm within a sample-interval "
                         "and turn it off during the same interval"):
            interval = enable_otel_alarms_export(dut, sample_interval=SAMPLE_INTERVAL_SEC * 2)
            values = _trigger_and_clear(engines, system, otel_alarms_collector, local_dir, thermal_directory,
                                        drawer_id, fan, new_delay + 3, 'raised-and-cleared-in-interval',
                                        alarm_type, poll_sec=1, sample_interval_sec=interval)

        with allure.step(f"15. Check the event is shown as 1 followed by exactly {new_delay} zeros"):
            assert ACTIVE_VALUE in values, f"Expected the {alarm_type} of {fan} to be exported as 1: {values}"
            first_active = values.index(ACTIVE_VALUE)
            last_active = len(values) - 1 - values[::-1].index(ACTIVE_VALUE)
            assert set(values[first_active:last_active + 1]) == {ACTIVE_VALUE}, (
                f"Expected the {alarm_type} of {fan} to stay at 1 until it was cleared, got {values}")
            zeros = values[last_active + 1:]
            assert zeros == [CLEARED_VALUE] * new_delay, (
                f"Expected {new_delay} zeros printed after the last 1 for {fan}, got {values}")
            assert set(values[:first_active]) <= {CLEARED_VALUE}, (
                f"Unexpected values before the alarm was raised for {fan}: {values}")

    finally:
        with allure.step("Restore suppress-zeroes and the sample-interval"):
            set_leaf(suppress_zeroes, dut, TelemetryConsts.DELAY_INTERVALS, TelemetryConsts.Defaults.ALARMS_DELAY_INTERVALS)
            set_leaf(suppress_zeroes, dut, TelemetryConsts.STATE, TelemetryConsts.State.ENABLED.value,
                     apply=True)
            enable_otel_alarms_export(dut)


@pytest.mark.system
@pytest.mark.disable_loganalyzer
def test_otel_alarms_change_class_enablement(engines, devices, otel_alarms_collector, tmp_path, random_api):
    """
        Test Objective:
        Validate that class change for OTEL exposes only the class that was selected as enabled.

        Precondition:
        The selected alarms to trigger should be inactive and suppress-zero is enabled (default is enabled
        but verify). OTEL should be enabled on Software and Platform class.

        Test Steps and Expected Results:
        1. Trigger both Software and Platform alarm events.
            - Expected: Both events appear in OTEL; counters increase accordingly.
        2. Clear all events.
        3. Disable the Platform alarm class.
        4. Trigger both Software and Platform alarm events.
            - Expected: Only the Software event and its counter change; Platform event/counter remain as before.
        5. Clear all events.
        6. Enable the Platform alarm class.
        7. Disable the Software alarm class.
        8. Trigger both Software and Platform alarm events.
        - Expected: Only the Platform event and its counter change; Software event/counter remain as before.
        9. Clear all events.
        10. Enable the Software alarm class.
        11. Trigger both Software and Platform alarm events.
            - Expected: Both events and counters increase; events show for both classes.
    """
    TestToolkit.update_engines(engines)
    dut = engines.dut
    system = System()
    local_dir = str(tmp_path)
    collector = otel_alarms_collector
    software, platform = TelemetryConsts.ALARMS_CLASS_SOFTWARE, TelemetryConsts.ALARMS_CLASS_PLATFORM
    thermal_directory, drawer_id, fan = _choose_fan(engines, devices, system)
    symlink = None
    # (steps, class states to set, classes expected to export), in test-plan order.
    phases = [("Precondition, 1-2", {software: _ENABLED, platform: _ENABLED}, [software, platform]),
              ("3-5", {platform: _DISABLED}, [software]),
              ("6-9", {platform: _ENABLED, software: _DISABLED}, [platform]),
              ("10-11", {software: _ENABLED}, [software, platform])]

    try:
        for steps, states, enabled in phases:
            label = '-'.join(f"{alarm_class}-{state}" for alarm_class, state in states.items())
            with allure.step(f"{steps}. Set {states}, trigger both events: only {enabled} show and count them"):
                logger.info(f"{steps}. Set {states}, trigger both events: only {enabled} show and count them")
                for alarm_class, state in states.items():
                    set_otel_alarms_class(dut, alarm_class, state=state)
                symlink = _trigger_events(dut, thermal_directory, drawer_id)
                _validate_nvue_events(system, fan, expected_present=True)
                _validate_otel_classes(collector, local_dir, fan, enabled, label)
                _clear_events(dut, system, thermal_directory, drawer_id, fan, symlink)
    finally:
        with allure.step("Restore the events and the alarms classes"):
            try:
                _restore_events(dut, thermal_directory, drawer_id, symlink)
            finally:
                unset_otel_alarms_classes(dut)


@pytest.mark.system
@pytest.mark.disable_loganalyzer
def test_alarms_otel_change_class_interval(engines, devices, otel_alarms_collector, tmp_path, random_api):
    """
        Test Objective:
        Validate that a class's sample-interval sets how soon and how often its events are exported over OTEL.

        Precondition:
        The selected alarms to trigger should be inactive and suppress-zero is enabled (default is enabled
        but verify). OTEL should be enabled on Software and Platform class.

        Test Steps and Expected Results:
        1. Trigger a Software and Platform event.
        2. Validate on OTEL that both events show.
            - Expected: Both events show.
        3. Change the Software class interval to 5 seconds.
        4. Trigger a Software and Platform event.
        5. Validate that the Software event shows up.
            - Expected: Shown after 5 seconds.
        6. Validate that the Platform event isn't up yet.
        7. Wait 25 seconds.
        8. Validate that the Platform event is shown.
        9. Return the Software class interval to 30 seconds.
        10. Trigger a Software and Platform event.
        11. Wait 5 seconds.
        12-13. Validate that neither the Software nor the Platform event is up yet.
        14. Wait 25 seconds.
        15-16. Validate that both the Software and the Platform event are shown.

        "Isn't up yet" depends on where each class's tick falls after the raise, which the test does not
        control, so it is held to the class's cadence: never exported more often than its interval.
        One capture per phase covers both the early and the late checks.
    """
    TestToolkit.update_engines(engines)
    dut = engines.dut
    system = System()
    local_dir = str(tmp_path)
    collector = otel_alarms_collector
    software, platform = TelemetryConsts.ALARMS_CLASS_SOFTWARE, TelemetryConsts.ALARMS_CLASS_PLATFORM
    fast, default = OtelCollectorConst.ALARMS_SAMPLE_INTERVAL_SEC, TelemetryConsts.Defaults.ALARMS_SAMPLE_INTERVAL
    thermal_directory, drawer_id, fan = _choose_fan(engines, devices, system)
    symlink = None

    try:
        with allure.step(f"Precondition: enable the software and platform classes at the default {default}s"):
            logger.info(f"Enabling the software and platform classes at the default {default}s")
            interval = enable_otel_alarms_export(dut, sample_interval=default)
            for alarm_class in (software, platform):
                set_otel_alarms_class(dut, alarm_class, state=_ENABLED, sample_interval=default)

        with allure.step("1. Trigger a software and a platform event"):
            logger.info(f"Triggering a software and a platform event")
            symlink = _trigger_events(dut, thermal_directory, drawer_id)
            _validate_nvue_events(system, fan, expected_present=True)

        with allure.step("2. Validate on OTEL that both events show"):
            logger.info(f"Validating on OTEL that both events show")
            alarms = get_otel_alarms(collector, local_dir, label='both-default', sample_interval_sec=interval)
            for origin, alarm_type in _class_alarms(fan).values():
                validate_otel_alarms(collector, local_dir, origin, alarm_type, expected_present=True, snapshot=alarms)
            _clear_events(dut, system, thermal_directory, drawer_id, fan, symlink)

        with allure.step(f"3. Change the software class interval to {fast}s"):
            logger.info(f"Changing the software class interval to {fast}s")
            set_otel_alarms_class(dut, software, sample_interval=fast)

        with allure.step("4. Trigger a software and a platform event"):
            logger.info(f"Triggering a software and a platform event")
            collector.truncate_artifact()
            symlink = _trigger_events(dut, thermal_directory, drawer_id)
            _validate_nvue_events(system, fan, expected_present=True)
            timings = _export_timings(collector, local_dir, system, fan, 'software-fast')

        with allure.step(f"5. Validate that the software event shows up after {fast}s, and every {fast}s"):
            _assert_export_timing(timings, software, fast)
            closest_sec = timings[software][1]
            assert closest_sec is not None and closest_sec <= fast + CLASS_INTERVAL_TOLERANCE_SEC, (
                f"Expected the software event exported again within {fast}s, closest samples: {closest_sec}s")

        with allure.step(f"6-8. Validate that the platform event is shown, no more often than every {default}s"):
            logger.info(f"Validating that the platform event is shown, no more often than every {default}s")
            _assert_export_timing(timings, platform, default)
            _clear_events(dut, system, thermal_directory, drawer_id, fan, symlink)

        with allure.step(f"9. Return the software class interval to {default}s"):
            logger.info(f"Returning the software class interval to {default}s")
            set_otel_alarms_class(dut, software, sample_interval=default)

        with allure.step("10. Trigger a software and a platform event"):
            logger.info(f"Triggering a software and a platform event")
            collector.truncate_artifact()
            symlink = _trigger_events(dut, thermal_directory, drawer_id)
            _validate_nvue_events(system, fan, expected_present=True)
            timings = _export_timings(collector, local_dir, system, fan, 'software-default')

        for alarm_class in (software, platform):
            with allure.step(f"11-16. Validate that the {alarm_class} event is shown, no more often than every "
                             f"{default}s"):
                logger.info(f"Validating that the {alarm_class} event is shown, no more often than every {default}s")
                _assert_export_timing(timings, alarm_class, default)

        _clear_events(dut, system, thermal_directory, drawer_id, fan, symlink)
    finally:
        with allure.step("Restore the events, the alarms classes and the sample-interval"):
            try:
                _restore_events(dut, thermal_directory, drawer_id, symlink)
            finally:
                try:
                    unset_otel_alarms_classes(dut)
                finally:
                    enable_otel_alarms_export(dut)
