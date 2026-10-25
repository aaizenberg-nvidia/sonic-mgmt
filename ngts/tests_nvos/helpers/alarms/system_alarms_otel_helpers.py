"""Validation of the system alarms exported over OTLP.

`nv set system telemetry alarms export state enabled` exports every active alarm
as a series under `nvswitch_alarms_platform` or `nvswitch_alarms_software`
(attributes: resource, alarm_type, severity; value 1 while active), plus the
number of active alarms of each family, one datapoint per severity, under
`nvswitch_active_alarms_{platform,software}_count`.

A cleared alarm keeps reporting 0 for `suppress-zeroes delay-intervals` samples
(5 by default) and then stops being exported, so an inactive alarm reads either
as a 0 or as no series at all.

Collection windows are sized from `OtelCollectorConst.ALARMS_SAMPLE_INTERVAL_SEC`
unless the caller passes the interval `enable_otel_alarms_export` returns. The export carries no
alarm text and no raise time, so only the severity is held to the alarm catalog
(through `system_alarm_nvue_helpers`, which also does the resource matching).
"""

import logging
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

from ngts.tools.test_utils import allure_utils as allure
from retry.api import retry_call

from ngts.nvos_constants.constants_nvos import AlarmsConsts, TelemetryConsts
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.helpers.alarms.system_alarm_nvue_helpers import (alarm_resource_matches_sensor,
                                                                      resolve_alarm_family,
                                                                      validate_severity_and_description)
from ngts.tests_nvos.helpers.general_helpers import normalize_sensor_name
from ngts.tests_nvos.system.telemetry.otel.constants import OtelCollectorConst
from ngts.tests_nvos.system.telemetry.otel.helpers import read_otel_collector_documents
from ngts.tests_nvos.system.telemetry.otel.metrics_parser import datapoint_value, prepare_attributes_key
from ngts.tests_nvos.system.telemetry.otel.nvos_cli_helpers import set_leaf, show_applied, unset_path, verify_leaf
from ngts.tests_nvos.system.telemetry.otel.otel_collector import OtelCollector
from ngts.tests_nvos.helpers.alarms.helpers import NS_PER_SEC, epoch_ns_as_utc

logger = logging.getLogger(__name__)

OTEL_PLATFORM_ALARMS_METRIC = 'nvswitch_alarms_platform'
OTEL_SOFTWARE_ALARMS_METRIC = 'nvswitch_alarms_software'

METRIC_BY_COLLECTION = {
    AlarmsConsts.COMPONENT_ALARMS: OTEL_PLATFORM_ALARMS_METRIC,
    AlarmsConsts.SOFTWARE_ALARMS: OTEL_SOFTWARE_ALARMS_METRIC,
}
COLLECTION_BY_METRIC = {metric: collection for collection, metric in METRIC_BY_COLLECTION.items()}
COUNT_METRIC_BY_METRIC = {
    OTEL_PLATFORM_ALARMS_METRIC: 'nvswitch_active_alarms_platform_count',
    OTEL_SOFTWARE_ALARMS_METRIC: 'nvswitch_active_alarms_software_count',
}
METRIC_BY_COUNT_METRIC = {count_metric: metric for metric, count_metric in COUNT_METRIC_BY_METRIC.items()}
ALARM_METRICS = tuple(COUNT_METRIC_BY_METRIC)
COUNT_METRICS = tuple(COUNT_METRIC_BY_METRIC.values())
METRIC_BY_ALARMS_CLASS = {
    TelemetryConsts.ALARMS_CLASS_PLATFORM: OTEL_PLATFORM_ALARMS_METRIC,
    TelemetryConsts.ALARMS_CLASS_SOFTWARE: OTEL_SOFTWARE_ALARMS_METRIC,
}

ALARM_ATTRIBUTES = ('resource', 'alarm_type', 'severity')
# The export spells it `alarm_type`, NVUE and gNMI spell it `alarm-type`.
ATTR_KEY_ALIASES = {'alarm-type': 'alarm_type'}

ACTIVE_VALUE = 1
CLEARED_VALUE = 0


# How long healthd takes to raise or clear an alarm. Set by its own poll cycle, not
# by the export interval; in line with the NVUE and gNMI validators (tries=6, delay=10).
ALARM_RAISE_PATIENCE_SEC = 60

AlarmKey = Tuple[str, str]
CountSample = Tuple[int, float]


def alarms_collection_window_sec(samples: int = 1, sample_interval_sec: int = None) -> int:
    """How long to capture for `samples` samples of the alarms export.

    Not `OtelCollectorConst.collection_window_sec`: its 70s floor is sized for 60s
    platform stats, while the alarms export samples every few seconds.
    """
    interval = sample_interval_sec or OtelCollectorConst.ALARMS_SAMPLE_INTERVAL_SEC
    # One interval per wanted sample, plus a fixed warm-up for export and artifact-staging latency.
    return samples * interval + OtelCollectorConst.OTEL_COLLECTION_WARM_UP_SEC


@dataclass
class OtelAlarmEntry:
    """One datapoint of an alarm series. Missing attributes stay empty and fail validation."""
    resource: str = ''
    alarm_type: str = ''
    severity: str = ''
    value: float = 0.0
    timestamp_ns: int = 0
    metric: str = ''

    @property
    def is_active(self) -> bool:
        return self.value == ACTIVE_VALUE

    @property
    def key(self) -> AlarmKey:
        return normalize_sensor_name(self.resource), self.alarm_type

    def matches(self, alarm_type: str, alarm_origin_name: str) -> bool:
        return self.alarm_type == alarm_type and alarm_resource_matches_sensor(self.resource, alarm_origin_name)


@dataclass
class OtelAlarmSample:
    """Every alarm and count datapoint the export stamped with one timestamp.

    `counts[count_metric][severity]`: the value of that count in this sample.
    """
    timestamp_ns: int = 0
    alarms: Dict[AlarmKey, OtelAlarmEntry] = field(default_factory=dict)
    counts: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def active(self, metric: str) -> Dict[AlarmKey, OtelAlarmEntry]:
        return {key: entry for key, entry in self.alarms.items() if entry.metric == metric and entry.is_active}

    def has_family(self, metric: str) -> bool:
        """Whether this sample carries any datapoint of `metric`'s family (its series or its count)."""
        return COUNT_METRIC_BY_METRIC[metric] in self.counts or any(
            entry.metric == metric for entry in self.alarms.values())


@dataclass
class OtelAlarmSnapshot:
    """What the alarm metrics reported over one collection window, one `OtelAlarmSample` per timestamp.

    `samples`: in time order.
    `series`: every datapoint of each alarm, in time order.
    `alarms`: the newest datapoint of each alarm, i.e. what the DUT reports now.
    `counts[count_metric][severity]`: the (timestamp_ns, value) samples, in time order.
    `sample_interval_sec`: the export interval the window was collected at.
    """
    samples: List[OtelAlarmSample] = field(default_factory=list)
    sample_interval_sec: int = OtelCollectorConst.ALARMS_SAMPLE_INTERVAL_SEC

    @property
    def series(self) -> Dict[AlarmKey, List[OtelAlarmEntry]]:
        series = {}
        for sample in self.samples:
            for key, entry in sample.alarms.items():
                series.setdefault(key, []).append(entry)
        return series

    @property
    def alarms(self) -> Dict[AlarmKey, OtelAlarmEntry]:
        return {key: entries[-1] for key, entries in self.series.items()}

    @property
    def counts(self) -> Dict[str, Dict[str, List[CountSample]]]:
        counts = {}
        for sample in self.samples:
            for count_metric, by_severity in sample.counts.items():
                for severity, value in by_severity.items():
                    counts.setdefault(count_metric, {}).setdefault(severity, []).append((sample.timestamp_ns, value))
        return counts

    def _latest_family_sample(self, metric: str) -> OtelAlarmSample | None:
        """The newest sample carrying `metric`'s family, unless the export has since moved on without it."""
        family_sample = next((sample for sample in reversed(self.samples) if sample.has_family(metric)), None)
        # A family suppress-zeroes stopped exporting only shows up in older samples, which are no longer current.
        if family_sample is None or \
                self.samples[-1].timestamp_ns - family_sample.timestamp_ns > self.sample_interval_sec * NS_PER_SEC:
            return None
        return family_sample

    def latest_active(self, metric: str) -> Dict[AlarmKey, OtelAlarmEntry]:
        """The alarms `metric` reports as active in the newest sample."""
        sample = self._latest_family_sample(metric)
        logger.info(f"Latest active {metric} sample: {sample}")
        return sample.active(metric) if sample else {}

    def latest_counts(self, count_metric: str) -> Dict[str, float]:
        """The count `count_metric` reports per severity in the newest sample; empty if not exported."""
        sample = self._latest_family_sample(METRIC_BY_COUNT_METRIC[count_metric])
        logger.info(f"Latest counts {count_metric} sample: {sample}")
        return dict(sample.counts.get(count_metric, {})) if sample else {}


def _scalar_datapoints(metric: dict) -> Iterable[Tuple[dict, float]]:
    """The scalar datapoints of a `gauge` or `sum` metric (the agent picks either), with their value."""
    for metric_type in ('gauge', 'sum'):
        for datapoint in (metric.get(metric_type) or {}).get('dataPoints') or []:
            value = datapoint_value(datapoint)
            if value is not None:
                yield datapoint, value


def _datapoint_attributes(datapoint: dict) -> Dict[str, str]:
    """The alarm attributes of a datapoint. `prepare_attributes_key` returns them flat: (k0, v0, k1, v1, ...)."""
    flat = prepare_attributes_key(datapoint.get('attributes') or [])
    attributes = {}
    for key, value in zip(flat[::2], flat[1::2]):
        key = str(key).strip()
        key = ATTR_KEY_ALIASES.get(key, key)
        if key in ALARM_ATTRIBUTES:
            attributes[key] = str(value).strip()
    return attributes


def _timestamp_ns(datapoint: dict) -> int:
    try:
        return int(datapoint.get('timeUnixNano') or 0)
    except (TypeError, ValueError):
        return 0


def parse_otel_alarms(docs: Iterable[Dict[str, Any]]) -> OtelAlarmSnapshot:
    """Parse collector OTLP documents into one `OtelAlarmSample` per exact timestamp, ignoring every non-alarm metric."""
    samples: Dict[int, OtelAlarmSample] = {}
    metrics = []
    for doc in docs:
        for resource_metrics in doc.get('resourceMetrics') or []:
            for scope_metrics in resource_metrics.get('scopeMetrics') or []:
                for metric in scope_metrics.get('metrics') or []:
                    metrics.append(metric)
    for metric in metrics:
        name = metric.get('name')
        if name not in ALARM_METRICS and name not in COUNT_METRICS:
            continue
        for datapoint, value in _scalar_datapoints(metric):
            timestamp_ns = _timestamp_ns(datapoint)
            sample = samples.setdefault(timestamp_ns, OtelAlarmSample(timestamp_ns=timestamp_ns))
            attributes = _datapoint_attributes(datapoint)
            if name in ALARM_METRICS:
                entry = OtelAlarmEntry(value=value, timestamp_ns=timestamp_ns, metric=name, **attributes)
                sample.alarms[entry.key] = entry
            else:
                sample.counts.setdefault(name, {})[attributes.get('severity', '')] = value
    # Datapoints arrive in export order, not time order.
    logger.info(f'Parsed alarms: {samples}')
    return OtelAlarmSnapshot(samples=[samples[timestamp_ns] for timestamp_ns in sorted(samples)])


def get_otel_alarms(collector: OtelCollector, local_dir: str, *, samples: int = 1,
                    label: str = 'alarms', sample_interval_sec: int = None) -> OtelAlarmSnapshot:
    """Collect `samples` fresh samples of the alarms export and parse them.

    An empty artifact is a DUT with no active alarms, so it parses into an empty snapshot.
    """
    sample_interval_sec = sample_interval_sec or OtelCollectorConst.ALARMS_SAMPLE_INTERVAL_SEC
    wait_sec = alarms_collection_window_sec(samples, sample_interval_sec)
    with allure.step(f"OTEL: collect {samples} alarm sample(s) over {wait_sec}s"):
        collector.truncate_artifact()
        time.sleep(wait_sec)
        local_json = collector.fetch_artifact_if_any(local_dir, file_name=f"otel-out-{label}.json")
        if local_json is None:
            logger.info(f"OTEL alarms: the collector wrote no artifact in {wait_sec}s")
            return OtelAlarmSnapshot(sample_interval_sec=sample_interval_sec)
        snapshot = parse_otel_alarms(read_otel_collector_documents(local_json))
        snapshot.sample_interval_sec = sample_interval_sec
        return snapshot


def otel_alarms_matching(snapshot: OtelAlarmSnapshot, alarm_type: str,
                         alarm_origin_name: str) -> Dict[AlarmKey, OtelAlarmEntry]:
    """The newest `alarm_type` datapoints whose resource matches `alarm_origin_name`."""
    return {key: entry for key, entry in snapshot.alarms.items() if entry.matches(alarm_type, alarm_origin_name)}


def _expected_metric(alarm_type: str) -> str:
    collection_name = AlarmsConsts.COLLECTION_BY_TYPE.get(alarm_type, AlarmsConsts.COMPONENT_ALARMS)
    return METRIC_BY_COLLECTION[collection_name]


def _validate_otel_alarm_entry(key: AlarmKey, entry: OtelAlarmEntry, alarm_type: str,
                               raised_after_ns: int = None) -> None:
    """Validate one exported alarm: all attributes present, the right metric, the catalog severity.

    `raised_after_ns` (`get_dut_time_ref().epoch_ns` taken before the fault) only guards against
    stale samples: this path has no time-created, so it cannot prove this fault raised the alarm.
    """
    missing = [attribute for attribute in ALARM_ATTRIBUTES if not getattr(entry, attribute)]
    assert not missing, f"OTEL alarm '{key}' is missing attributes {missing}: {entry}"

    expected_metric = _expected_metric(alarm_type)
    assert entry.metric == expected_metric, (
        f"OTEL alarm '{key}': {alarm_type} must only be exported under "
        f"'{expected_metric}', but it arrived under '{entry.metric}': {entry}"
    )

    family = resolve_alarm_family(entry.resource, COLLECTION_BY_METRIC[entry.metric])
    validate_severity_and_description(key, family, alarm_type, entry.severity)

    # Both are DUT-clock epoch ns, so a sample stamped before the pre-fault reading was taken before the fault.
    if raised_after_ns is not None:
        assert entry.timestamp_ns >= raised_after_ns, (
            f"OTEL alarm '{key}': the sample is stamped {entry.timestamp_ns} "
            f"({epoch_ns_as_utc(entry.timestamp_ns)}), earlier than {raised_after_ns} "
            f"({epoch_ns_as_utc(raised_after_ns)}), the DUT clock taken before the fault was "
            f"injected; the alarm is being read off a sample that predates the fault"
        )


def assert_active_alarm_counts_consistent(snapshot: OtelAlarmSnapshot) -> None:
    """In every sample, each family's active-count must match its active series per severity.

    A family with no active alarm may skip its count entirely, so a sample without the count is
    only logged. A severity missing from an exported count is read as 0.
    """
    with allure.step("Verify the active-alarm counts match the exported alarm series in every sample"):
        for sample in snapshot.samples:
            for metric, count_metric in COUNT_METRIC_BY_METRIC.items():
                active = sample.active(metric)
                if count_metric not in sample.counts:
                    if active:
                        logger.warning(f"Sample {sample.timestamp_ns}: '{count_metric}' was not exported, "
                                       f"so {len(active)} active '{metric}' series go uncounted: {active}")
                    continue
                active_by_severity = dict(Counter(entry.severity for entry in active.values()))
                counted = {severity: int(count) for severity, count in sample.counts[count_metric].items() if count}
                assert counted == active_by_severity, (
                    f"Sample {sample.timestamp_ns} ({epoch_ns_as_utc(sample.timestamp_ns)}): '{count_metric}' "
                    f"reports {counted} per severity, but the '{metric}' series active in it give "
                    f"{active_by_severity}: {active}"
                )
                logger.info(f"Sample {sample.timestamp_ns}: '{count_metric}' matches the active "
                            f"'{metric}' series: {counted}")


def otel_active_alarm_count(snapshot: OtelAlarmSnapshot, metric: str) -> int:
    """The active-alarm count of `metric`'s family in the newest sample, summed over severities.

    A count that was not exported reads as 0 (nothing to count, or the class is disabled).
    """
    count_metric = COUNT_METRIC_BY_METRIC[metric]
    counted = snapshot.latest_counts(count_metric)
    total = int(sum(counted.values()))
    logger.info(f"'{count_metric}' reports {total} active alarm(s) over {counted or 'nothing exported'}")
    return total


def _validate_otel_alarm(snapshot: OtelAlarmSnapshot, alarm_type: str, alarm_origin_name: str,
                         expected_present: bool, raised_after_ns: int = None) -> None:
    """Validate one alarm type of `alarm_origin_name`. Absent means no series or a series at 0."""
    matched = otel_alarms_matching(snapshot, alarm_type, alarm_origin_name)
    logger.info(f"Matched OTEL alarm series: {matched}")
    active = {key: entry for key, entry in matched.items() if entry.is_active}

    if not expected_present:
        assert not active, (
            f"Expected no active {alarm_type} alarm for '{alarm_origin_name}' over OTLP, but "
            f"these series report {ACTIVE_VALUE}: {active}"
        )
        logger.info(f"No active {alarm_type} alarm for {alarm_origin_name} over OTLP, as expected "
                    f"(matched series: {matched or 'none'}, exported alarms: {list(snapshot.alarms)})")
        return

    assert matched, (
        f"Expected an active {alarm_type} alarm for '{alarm_origin_name}' over OTLP, but the "
        f"exported alarms are: {snapshot.alarms}"
    )
    assert active, (
        f"Expected the {alarm_type} alarm of '{alarm_origin_name}' to report {ACTIVE_VALUE} over "
        f"OTLP, but its series report: {({key: entry.value for key, entry in matched.items()})}"
    )
    for key, entry in active.items():
        _validate_otel_alarm_entry(key, entry, alarm_type, raised_after_ns)

    # A misplaced series shares its snapshot key with the correct one, so look through every datapoint.
    expected_metric = _expected_metric(alarm_type)
    misplaced = [entry for entries in snapshot.series.values() for entry in entries
                 if entry.metric != expected_metric and entry.is_active and
                 entry.matches(alarm_type, alarm_origin_name)]
    assert not misplaced, (
        f"{alarm_type} must only be exported under '{expected_metric}', but it is also under: {misplaced}"
    )
    logger.info(f"Active {alarm_type} alarm exported for {alarm_origin_name} under {expected_metric}: {active}")


# Each attempt already sleeps a whole collection window, so no extra delay is needed.
def validate_otel_alarms(collector: OtelCollector, local_dir: str, alarm_origin_name: str,
                         alarm_types: str | list[str], *, expected_present: bool,
                         raised_after_ns: int = None,
                         snapshot: OtelAlarmSnapshot = None,
                         sample_interval_sec: int = OtelCollectorConst.ALARMS_SAMPLE_INTERVAL_SEC) -> None:
    """Check that OTEL exported alarms for `alarm_origin_name` match `expected_present` for the given types.

    Retries until alarms are raised or cleared, or times out. See `_validate_otel_alarm_entry` for what `raised_after_ns` does.
    `sample_interval_sec` must match the DUT's `alarms export sample-interval` (the value
    `enable_otel_alarms_export` returns): each live read collects one window sized from it, so a
    shorter value than the DUT's can collect no sample and pass an absence check vacuously.
    """
    def _check(snapshot):
        for alarm_type in [alarm_types] if isinstance(alarm_types, str) else alarm_types:
            _validate_otel_alarm(snapshot, alarm_type, alarm_origin_name, expected_present, raised_after_ns)
        assert_active_alarm_counts_consistent(snapshot)

    if snapshot is not None:
        return _check(snapshot)
    window_sec = alarms_collection_window_sec(sample_interval_sec=sample_interval_sec)
    tries = max(3, math.ceil(ALARM_RAISE_PATIENCE_SEC / window_sec))
    retry_call(lambda: _check(get_otel_alarms(collector, local_dir, sample_interval_sec=sample_interval_sec)),
               exceptions=AssertionError, tries=tries, delay=0)


def _applied_sample_interval(applied: Dict[str, Any], fallback: int) -> int:
    """The interval the DUT read back, else `fallback`."""
    raw = applied.get(TelemetryConsts.SAMPLE_INTERVAL)
    try:
        interval = int(str(raw).strip())
    except ValueError:
        interval = 0
    if interval <= 0:
        logger.warning(f"'{TelemetryConsts.SAMPLE_INTERVAL}' read back as {raw!r}; using {fallback}s instead")
        interval = fallback
    logger.info(f"OTEL alarms: {interval}s sample-interval "
                f"(one sample: {alarms_collection_window_sec(sample_interval_sec=interval)}s)")
    return interval


def enable_otel_alarms_export(dut, *,
                              sample_interval: int = OtelCollectorConst.ALARMS_SAMPLE_INTERVAL_SEC) -> int:
    """Enable the alarms export at `sample_interval` and return the interval the DUT reads back.

    Only the alarms subtree is touched; the OTLP destination is set up by `setup_otel_suite`.
    Pass `sample_interval=None` to keep whatever interval the DUT already has.
    """
    export = System().telemetry.alarms.export
    with allure.step(f"NVUE: enable the system telemetry alarms export at {sample_interval}s"):
        if sample_interval is not None:
            set_leaf(export, dut, TelemetryConsts.SAMPLE_INTERVAL, sample_interval)
        set_leaf(export, dut, TelemetryConsts.STATE, TelemetryConsts.State.ENABLED.value, apply=True)
        applied = show_applied(export, dut)
        verify_leaf(applied, TelemetryConsts.STATE, TelemetryConsts.State.ENABLED.value)
        if sample_interval is not None:
            verify_leaf(applied, TelemetryConsts.SAMPLE_INTERVAL, sample_interval)
        return _applied_sample_interval(
            applied, sample_interval or TelemetryConsts.Defaults.ALARMS_SAMPLE_INTERVAL)


def unset_otel_alarms_export(dut) -> None:
    """Unset the alarms subtree."""
    with allure.step("NVUE: unset the system telemetry alarms subtree"):
        unset_path(System().telemetry.alarms, dut, apply=True)
        applied = show_applied(System().telemetry.alarms.export, dut)
        verify_leaf(applied, TelemetryConsts.STATE, TelemetryConsts.Defaults.ALARMS_EXPORT_STATE)


def set_otel_alarms_class(dut, alarm_class: str, *, state: str = None, sample_interval: int = None) -> None:
    """Set `alarms class <alarm_class>` state and/or sample-interval in one commit and verify them.

    A caller that sets a class interval sizes its own collection windows.
    """
    node = System().telemetry.alarms.cls.categories[alarm_class]
    with allure.step(f"NVUE: set the system telemetry alarms class {alarm_class} "
                     f"state={state} sample-interval={sample_interval}"):
        if sample_interval is not None:
            set_leaf(node, dut, TelemetryConsts.SAMPLE_INTERVAL, sample_interval, apply=state is None)
        if state is not None:
            set_leaf(node, dut, TelemetryConsts.STATE, state, apply=True)
        applied = show_applied(node, dut)
        if sample_interval is not None:
            verify_leaf(applied, TelemetryConsts.SAMPLE_INTERVAL, sample_interval)
        if state is not None:
            verify_leaf(applied, TelemetryConsts.STATE, state)


def unset_otel_alarms_classes(dut) -> None:
    """Unset the `alarms class` subtree, leaving every class at its factory default."""
    with allure.step("NVUE: unset the system telemetry alarms class subtree"):
        unset_path(System().telemetry.alarms.cls, dut, apply=True)
