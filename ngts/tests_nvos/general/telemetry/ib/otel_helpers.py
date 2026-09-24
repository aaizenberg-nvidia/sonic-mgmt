"""OTEL surface of the IB plane-port, peer-port and local extended-telemetry tests.

The switch exports every family over OTLP to the collector brought up by
``setup_otel_suite``. A test reads one collection window as an :class:`OtelSnapshot` and holds
its series against the other surfaces (NVUE, gNMI) and against the System DB rows they are
sampled from.
"""

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import pytest

from ngts.tests_nvos.system.telemetry.otel.helpers import (
    OtelSuiteContext,
    collect_otlp_metrics_window,
    enable_nvos_telemetry_families,
)
from ngts.tests_nvos.system.telemetry.otel.metrics_parser import (
    datapoint_value,
    prepare_attributes_key,
)
from ngts.tools.test_utils import allure_utils as allure

from ngts.tests_nvos.general.telemetry.ib.constants import (
    IB_OTEL_CURRENT_SAMPLE_SLACK_SEC,
    IB_OTEL_FAMILIES,
    IB_OTEL_PLATFORM_WINDOW_SEC,
    IB_OTEL_PORT_WINDOW_SEC,
    OTEL_DB_FIELD_COMPARE,
    PEER_PORT_BER_MAX_RATIO,
    PEER_PORT_BER_ZERO_FLOOR,
    PEER_PORT_OTEL_COMPONENT_INFO,
    PEER_PORT_OTEL_IDENTITY_DB_FIELD,
    PEER_PORT_OTEL_IDENTITY_LEAVES,
    PEER_PORT_OTEL_NODE_INFO,
    PEER_PORT_OTEL_SWITCH_INFO,
    PEER_PORT_SPEED_MBPS_PER_GBPS,
    OtelDbCompare,
    OtelGroup,
    OtelLabel,
    OtelLeaf,
)

logger = logging.getLogger(__name__)


_NS_PER_SEC = 1_000_000_000


_METRIC_KINDS = ("gauge", "sum", "histogram")


_HISTOGRAM = "histogram"


# The info metrics that together carry a peer's identity labels (HCA peer).
_PEER_IDENTITY_METRICS = (
    PEER_PORT_OTEL_NODE_INFO, PEER_PORT_OTEL_SWITCH_INFO, PEER_PORT_OTEL_COMPONENT_INFO,
)


# Labels with few values, whose values the catalog lists next to the metric name.
_CATALOG_SMALL_LABELS = (
    "stat", "state", "direction", "ber_type", "component_type", "type", "channel", "lane",
)


def _labels(datapoint: Dict[str, Any]) -> Dict[str, str]:
    flat = prepare_attributes_key(datapoint.get("attributes", []) or [])
    return {k: str(v) for k, v in zip(flat[::2], flat[1::2]) if k is not None}


def _decode_value(spec: OtelLeaf, value: Optional[float]) -> Any:
    """The leaf reading a series value stands for.

    A bitmap leaf (``spec.bit``) reads 1 when its mask is set and 0 otherwise; an enumerated leaf
    (``spec.enum``) reads as the name at the value's index. Any other value is returned as is.
    """
    if value is None:
        return None
    if spec.bit is not None:
        return 1.0 if int(value) & spec.bit else 0.0
    if spec.enum:
        index = int(value)
        return spec.enum[index] if 0 <= index < len(spec.enum) else value
    return value


def _histogram_count(datapoint: Dict[str, Any]) -> Optional[float]:
    try:
        return float(datapoint.get("count"))
    except (TypeError, ValueError):
        return None


@dataclass
class OtelSeries:
    """All points of one metric under one label set, oldest first."""
    labels: Dict[str, str]
    kind: str
    points: List[Tuple[int, Optional[float]]] = field(default_factory=list)
    bucket_counts: List[int] = field(default_factory=list)
    explicit_bounds: List[float] = field(default_factory=list)

    @property
    def latest_ts(self) -> int:
        return self.points[-1][0]

    @property
    def latest(self) -> Optional[float]:
        return self.points[-1][1]

    def value_at(self, ts_ns: int) -> Optional[float]:
        for ts, value in self.points:
            if ts == ts_ns:
                return value
        return None


class OtelSnapshot:
    """One collection window of OTLP documents, indexed by metric and label set."""

    def __init__(self, docs: Iterable[Dict[str, Any]], label: str):
        self.label = label
        self._series: Dict[str, Dict[Tuple, OtelSeries]] = defaultdict(dict)
        for doc in docs:
            for resource in doc.get("resourceMetrics", []) or []:
                for scope in resource.get("scopeMetrics", []) or []:
                    for metric in scope.get("metrics", []) or []:
                        self._add_metric(metric)
        for by_labels in self._series.values():
            for series in by_labels.values():
                series.points.sort(key=lambda point: point[0])

    def _add_metric(self, metric: Dict[str, Any]) -> None:
        name = metric.get("name")
        if not name:
            return
        for kind in _METRIC_KINDS:
            payload = metric.get(kind)
            if not payload:
                continue
            for dp in payload.get("dataPoints", []) or []:
                labels = _labels(dp)
                key = tuple(sorted(labels.items()))
                series = self._series[name].setdefault(key, OtelSeries(labels, kind))
                if kind == _HISTOGRAM:
                    value = _histogram_count(dp)
                    series.bucket_counts = [int(c) for c in dp.get("bucketCounts", []) or []]
                    series.explicit_bounds = [float(b) for b in dp.get("explicitBounds", []) or []]
                else:
                    value = datapoint_value(dp)
                series.points.append((int(dp.get("timeUnixNano", 0)), value))

    @property
    def names(self) -> Set[str]:
        return set(self._series)

    def catalog(self) -> List[str]:
        """One line per exported metric: kind, label keys, series count, small-label values.

        Logged and attached per window, so a run keeps the exported names and labels for comparison
        with the ones the tests expect.
        """
        lines = []
        for name in sorted(self._series):
            by_labels = self._series[name]
            keys: Set[str] = set()
            small: Dict[str, Set[str]] = defaultdict(set)
            kind = ""
            for series in by_labels.values():
                kind = series.kind
                keys.update(series.labels)
                for key in _CATALOG_SMALL_LABELS:
                    if key in series.labels:
                        small[key].add(series.labels[key])
            values = "; ".join(f"{k}={sorted(v)[:12]}" for k, v in sorted(small.items()))
            lines.append(f"{name} | {kind} | {sorted(keys)} | series={len(by_labels)} | {values}")
        return lines

    def series(self, metric: str, **match: str) -> List[OtelSeries]:
        """Every series of ``metric`` whose labels carry ``match``."""
        return [
            series for series in self._series.get(metric, {}).values()
            if all(series.labels.get(k) == str(v) for k, v in match.items())
        ]

    def current(self, metric: str, **match: str) -> List[OtelSeries]:
        """The series of ``metric`` present in its newest sample.

        A label set that stopped being exported inside the window (a link-down reason that
        cleared, a type label that changed with the knob) keeps its older points in the
        artifact; only the series the switch still exports describe the current state.
        """
        all_series = list(self._series.get(metric, {}).values())
        if not all_series:
            return []
        newest = max(series.latest_ts for series in all_series)
        floor = newest - IB_OTEL_CURRENT_SAMPLE_SLACK_SEC * _NS_PER_SEC
        return [
            series for series in self.series(metric, **match) if series.latest_ts >= floor
        ]

    def keys(self, metric: str, key_label: str) -> Set[str]:
        return {
            series.labels[key_label] for series in self.current(metric)
            if key_label in series.labels
        }

    def leaf_values(self, spec: OtelLeaf, key_label: str, key: str,
                    member_label: Optional[str] = None,
                    member: Optional[str] = None) -> List[Any]:
        """Current renderings of one leaf of one entity: label strings or latest values."""
        match = {key_label: key, **dict(spec.match)}
        if member_label is not None and member is not None:
            match[member_label] = member
        values = []
        for series in self.current(spec.metric, **match):
            if spec.label is None:
                values.append(_decode_value(spec, series.latest))
            elif spec.label in series.labels:
                values.append(series.labels[spec.label])
        return values

    def members(self, spec: OtelLeaf, key_label: str, key: str, member_label: str) -> Set[str]:
        match = {key_label: key, **dict(spec.match)}
        return {
            series.labels[member_label] for series in self.current(spec.metric, **match)
            if member_label in series.labels
        }

    def value(self, spec: OtelLeaf, key_label: str, key: str) -> Optional[float]:
        """The latest value of a single-series value leaf, or None when it is not exported."""
        values = [v for v in self.leaf_values(spec, key_label, key) if v is not None]
        return values[0] if len(values) == 1 else None

    def label(self, spec: OtelLeaf, key_label: str, key: str) -> Optional[str]:
        values = self.leaf_values(spec, key_label, key)
        return values[0] if len(values) == 1 else None

    def per_key_points(self, metric: str, key_label: str) -> Dict[str, Dict[int, float]]:
        """``{key: {ts: value}}`` of a single-series-per-key metric."""
        out: Dict[str, Dict[int, float]] = {}
        for series in self.series(metric):
            key = series.labels.get(key_label)
            if key is None:
                continue
            out.setdefault(key, {}).update(
                {ts: value for ts, value in series.points if value is not None}
            )
        return out


# ---------------------------------------------------------------------------
# Per-module surface
# ---------------------------------------------------------------------------


class IbOtelSurface:
    """The collector and export configuration one IB test module reads OTEL through.

    ``clear_config`` wipes the telemetry tree after every test, so the export is configured
    again on first use in each test and after anything that resets the DUT configuration.
    """

    def __init__(self, engines, suite: OtelSuiteContext, local_dir: str):
        self.engines = engines
        self.suite = suite
        self.local_dir = local_dir
        self._configured = False
        self._cache: Dict[str, OtelSnapshot] = {}

    def begin_test(self) -> None:
        self._configured = False
        self._cache.clear()

    def invalidate(self, reconfigure: bool = False) -> None:
        """Drop cached windows after the DUT state changed; ``reconfigure`` after a reboot."""
        self._cache.clear()
        if reconfigure:
            self._configured = False

    def configure(self) -> None:
        if self._configured:
            return
        enable_nvos_telemetry_families(
            self.engines.dut,
            self.suite.primary_ip,
            IB_OTEL_FAMILIES,
            export_vrf=self.suite.export_vrf,
            collector_ips=self.suite.collector_ips,
            use_stats_group=False,
        )
        self._configured = True

    def snapshot(self, label: str, *, platform: bool = False, fresh: bool = False) -> OtelSnapshot:
        """One collection window, reused within the test until ``fresh`` or ``invalidate``."""
        self.configure()
        if not fresh and label in self._cache:
            return self._cache[label]
        window = IB_OTEL_PLATFORM_WINDOW_SEC if platform else IB_OTEL_PORT_WINDOW_SEC
        _, docs, _ = collect_otlp_metrics_window(
            self.suite.primary, self.local_dir, label=label, wait_sec=window
        )
        snap = OtelSnapshot(docs, label)
        catalog = "\n".join(snap.catalog())
        logger.info("OTEL-CATALOG window %s:\n%s", label, catalog)
        allure.attach(f"OTEL catalog {label}", catalog)
        assert snap.names, (
            f"OTEL window {label!r} ({window}s) carried no metrics: the switch did not export "
            f"{', '.join(IB_OTEL_FAMILIES)} to {self.suite.primary_ip}"
        )
        self._cache[label] = snap
        return snap


_ACTIVE_SURFACE: Optional[IbOtelSurface] = None


def register(surface: IbOtelSurface) -> None:
    global _ACTIVE_SURFACE
    _ACTIVE_SURFACE = surface


def unregister() -> None:
    global _ACTIVE_SURFACE
    _ACTIVE_SURFACE = None


def active_surface() -> IbOtelSurface:
    if _ACTIVE_SURFACE is None:
        pytest.fail("OTEL surface is not set up: request the ib_otel fixture (or api=otel)")
    return _ACTIVE_SURFACE


# ---------------------------------------------------------------------------
# Checks. Each returns a list of problems; the caller asserts it is empty.
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, float) and not math.isnan(value) and not math.isinf(value)


def _rendering_problem(leaf: str, spec: OtelLeaf, values: List[Any], where: str) -> Optional[str]:
    if not values:
        target = f"label {spec.label!r} of {spec.metric}" if spec.label else spec.metric
        return f"{where}: {leaf} is not exported ({target})"
    if spec.label is None:
        # An enum leaf is decoded to its name before it gets here; any other value leaf is a number.
        bad = [v for v in values if not (v in spec.enum if spec.enum else _is_number(v))]
        if bad:
            kind = f"outside {list(spec.enum)}" if spec.enum else "non-numeric"
            return f"{where}: {leaf} ({spec.metric}) carries a {kind} value {bad}"
    elif not all(str(v).strip() for v in values):
        return f"{where}: {leaf} (label {spec.label!r} of {spec.metric}) is empty"
    return None


def group_problems(snap: OtelSnapshot, group: OtelGroup, key: str, *,
                   leaves: Optional[Iterable[str]] = None,
                   members: Optional[Iterable[str]] = None) -> List[str]:
    """Every leaf of ``group`` must be exported for ``key`` with a usable value.

    For a list group each leaf needs at least one member, or every one of ``members`` when
    the other surface names them.
    """
    wanted = list(leaves) if leaves is not None else list(group.leaves)
    problems = []
    for leaf in wanted:
        spec = group.leaves.get(leaf)
        if spec is None:
            problems.append(f"{key}: {leaf} has no OTEL counterpart defined")
            continue
        if group.member_label is None:
            problem = _rendering_problem(
                leaf, spec, snap.leaf_values(spec, group.key_label, key), key)
            if problem:
                problems.append(problem)
            continue
        exported = snap.members(spec, group.key_label, key, group.member_label)
        expected = set(members) if members is not None else exported
        if not exported:
            problems.append(f"{key}: {leaf} ({spec.metric}) has no {group.member_label}")
            continue
        missing = sorted(expected - exported)
        if missing:
            problems.append(
                f"{key}: {leaf} ({spec.metric}) lacks {group.member_label} {missing}")
        for member in sorted(expected & exported):
            problem = _rendering_problem(
                leaf, spec,
                snap.leaf_values(spec, group.key_label, key, group.member_label, member),
                f"{key} {group.member_label}={member}")
            if problem:
                problems.append(problem)
    return problems


def _decimal(raw: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None


def _db_value_matches(compare: str, otel: float, db_values: List[Decimal]) -> bool:
    otel_dec = Decimal(repr(otel))
    if compare == OtelDbCompare.COUNTER:
        return min(db_values) <= otel_dec <= max(db_values)
    if compare == OtelDbCompare.STATE:
        return otel_dec in db_values
    if compare == OtelDbCompare.SPEED:
        return any(otel_dec == db // PEER_PORT_SPEED_MBPS_PER_GBPS for db in db_values)
    if compare == OtelDbCompare.BER:
        for db in db_values:
            if db < PEER_PORT_BER_ZERO_FLOOR and otel_dec < PEER_PORT_BER_ZERO_FLOOR:
                return True
            if db > 0 and otel_dec > 0:
                ratio = max(db, otel_dec) / min(db, otel_dec)
                if ratio <= PEER_PORT_BER_MAX_RATIO:
                    return True
        return False
    raise ValueError(compare)


def db_parity_problems(snap: OtelSnapshot, key: str,
                       db_before: Mapping[str, Any], db_after: Mapping[str, Any],
                       metric_by_field: Mapping[str, str],
                       fields: Iterable[str]) -> List[str]:
    """Each OTEL value must be one the DB held while the window was collected.

    ``db_before`` is read before the window and ``db_after`` after it, so a counter must sit
    between the two, and a state, width or rate must equal (for a rate: stay within a decade
    of) one of them.
    """
    problems = []
    for db_field in fields:
        metric = metric_by_field.get(db_field)
        if metric is None:
            problems.append(f"{key}: {db_field} has no OTEL metric defined")
            continue
        db_values = [
            d for d in (_decimal(db_before.get(db_field)), _decimal(db_after.get(db_field)))
            if d is not None
        ]
        if not db_values:
            problems.append(f"{key}: the DB row carries no numeric {db_field}")
            continue
        otel = snap.value(OtelLeaf(metric), OtelLabel.INTERFACE, key)
        if otel is None:
            problems.append(f"{key}: {metric} is not exported (DB {db_field}={db_values[0]})")
            continue
        if not _is_number(otel):
            problems.append(f"{key}: {metric} carries a non-numeric value {otel}")
            continue
        compare = OTEL_DB_FIELD_COMPARE.get(db_field, OtelDbCompare.COUNTER)
        if not _db_value_matches(compare, otel, db_values):
            problems.append(
                f"{key}: {metric}={otel} does not match DB {db_field} "
                f"(before={db_before.get(db_field)!r}, after={db_after.get(db_field)!r}, "
                f"compared as {compare})"
            )
    return problems


def peer_identity(snap: OtelSnapshot, peer_id: str) -> Dict[str, str]:
    """``{label: value}`` of the peer's current node, switch and component info series."""
    identity: Dict[str, str] = {}
    for metric in _PEER_IDENTITY_METRICS:
        series = snap.current(metric, **{OtelLabel.INTERFACE: peer_id})
        if len(series) == 1:
            identity.update(series[0].labels)
    return identity


def peer_identity_problems(snap: OtelSnapshot, peer_id: str,
                           db_row: Mapping[str, Any]) -> List[str]:
    """The peer's identity labels must be exported and equal the DB fields they are read from.

    ``db_row`` is the merged PEER_COUNTERS + PEER_PORT_MAPPING row of the peer.
    """
    problems = []
    for metric in _PEER_IDENTITY_METRICS:
        count = len(snap.current(metric, **{OtelLabel.INTERFACE: peer_id}))
        if count != 1:
            problems.append(f"{peer_id}: {metric} has {count} current series (expected 1)")
    identity = peer_identity(snap, peer_id)
    for label, db_field in PEER_PORT_OTEL_IDENTITY_DB_FIELD.items():
        if db_field not in db_row:
            continue
        if label not in identity:
            problems.append(f"{peer_id}: identity label {label} is not exported")
            continue
        if str(identity[label]).strip() != str(db_row[db_field]).strip():
            problems.append(
                f"{peer_id}: identity label {label}={identity[label]!r} != DB "
                f"{db_field}={db_row[db_field]!r}"
            )
    return problems


def _same_rendering(otel: Any, other: Any) -> bool:
    if other is None:
        return False
    if isinstance(otel, float):
        other_dec = _decimal(other)
        return other_dec is not None and Decimal(repr(otel)) == other_dec
    left, right = str(otel).strip(), str(other).strip()
    if left == right:
        return True
    left_dec, right_dec = _decimal(left), _decimal(right)
    return left_dec is not None and left_dec == right_dec


def cross_surface_problems(snap: OtelSnapshot, group: OtelGroup, key: str,
                           other: Mapping[str, Any], other_label: str,
                           leaves: Iterable[str]) -> List[str]:
    """Static leaves must read the same over OTEL as over ``other_label`` for ``key``."""
    problems = []
    for leaf in leaves:
        spec = group.leaves.get(leaf)
        if spec is None:
            problems.append(f"{key}: {leaf} has no OTEL counterpart defined")
            continue
        if leaf not in other:
            continue
        values = snap.leaf_values(spec, group.key_label, key)
        if not any(_same_rendering(v, other[leaf]) for v in values):
            problems.append(
                f"{key}: {leaf} reads {values} over OTEL but {other[leaf]!r} over {other_label}")
    return problems


def value_within_problems(snap: OtelSnapshot, group: OtelGroup, key: str,
                          readings: Mapping[str, Any], other_label: str,
                          within: Callable[[float, float], bool],
                          leaves: Iterable[str]) -> List[str]:
    """Sampled leaves must agree with ``readings`` under ``within(otel, other)`` for one series.

    A leaf fanned out over several series (channel power per direction) passes when any of
    them agrees.
    """
    problems = []
    for leaf in leaves:
        spec = group.leaves.get(leaf)
        if spec is None:
            problems.append(f"{key}: {leaf} has no OTEL counterpart defined")
            continue
        other = _decimal(readings.get(leaf))
        if other is None:
            continue
        values = [v for v in snap.leaf_values(spec, group.key_label, key) if _is_number(v)]
        if not values:
            problems.append(f"{key}: {leaf} ({spec.metric}) is not exported")
        elif not any(within(v, float(other)) for v in values):
            problems.append(
                f"{key}: {leaf} reads {values} over OTEL but {readings.get(leaf)!r} "
                f"over {other_label}")
    return problems


def peer_identity_leaf_values(snap: OtelSnapshot, peer_id: str) -> Dict[str, str]:
    """The peer's identity as ``{leaf: value}``, under the NVUE and gNMI leaf spellings."""
    identity = peer_identity(snap, peer_id)
    return {
        leaf: identity[spec.label]
        for leaf, spec in PEER_PORT_OTEL_IDENTITY_LEAVES.items()
        if spec.label in identity
    }


def attach_snapshot_summary(snap: OtelSnapshot, metrics: Iterable[str]) -> None:
    """Attach the current series of ``metrics`` for post-mortem inspection."""
    lines = []
    for metric in sorted(set(metrics)):
        current = snap.current(metric)
        lines.append(f"{metric}: {len(current)} current series")
        for series in current[:20]:
            lines.append(f"  {series.labels} -> {series.latest}")
    allure.attach(f"OTEL window {snap.label}", "\n".join(lines))
