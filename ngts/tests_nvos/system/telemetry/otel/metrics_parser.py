"""Parse OTEL collector JSON artifacts into metric structures."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, DefaultDict, Dict, Iterable, List, Optional, Tuple, Union

AttributeKey = Union[Tuple[Any, ...], str]
MetricTimestamps = Dict[str, Dict[AttributeKey, List[int]]]
MetricValues = Dict[str, Dict[AttributeKey, List[float]]]

# Scalar OTLP ``NumberDataPoint`` value fields (protobuf union — exactly one is set).
_OTLP_NUMBER_FIELDS = ("asInt", "asDouble")


# OTLP ``AnyValue`` typed fields, in preference order (protobuf union — exactly one is set).
_OTLP_VALUE_FIELDS = (
    "stringValue",
    "intValue",
    "boolValue",
    "doubleValue",
    "bytesValue",
    "arrayValue",
    "kvlistValue",
)


def _extract_otlp_value(value: Any) -> Any:
    """Return the single typed scalar from an OTLP ``AnyValue`` object.

    OTLP encodes an attribute value as ``{"stringValue": ...}`` / ``{"intValue": ...}`` / etc.
    (a protobuf union: exactly one field is populated). We pull that one typed field so the
    flattened key stays ``key, value`` aligned regardless of JSON member ordering. Non-dict
    values are returned as-is; an unknown/empty ``AnyValue`` falls back to its first member.
    """
    if not isinstance(value, dict):
        return value
    for field in _OTLP_VALUE_FIELDS:
        if field in value:
            return value[field]
    for member in value.values():
        return member
    return None


def prepare_attributes_key(attributes: List[Dict[str, Any]]) -> AttributeKey:
    """Build a hashable, key/value-aligned tuple from OTLP attribute objects.

    Each OTLP attribute is ``{"key": <name>, "value": <AnyValue>}``. We deterministically append
    the name then the single typed scalar from its value, yielding
    ``(key0, value0, key1, value1, ...)`` independent of JSON member insertion order. Consumers
    (e.g. :func:`ngts.tests_nvos.system.telemetry.otel.cumulus.coexistence._attr_tuple_to_pairs`)
    rely on that positional key/value alternation, so producing it explicitly here — rather than
    iterating ``attr.values()`` — prevents silent mis-pairing if the JSON orders ``value`` first
    or the inner ``AnyValue`` carries more than one member.
    """
    parts: List[Any] = []
    for attr in attributes:
        parts.append(attr.get("key"))
        parts.append(_extract_otlp_value(attr.get("value")))
    return tuple(parts)


def _append_datapoint_timestamps(
    out: DefaultDict[str, DefaultDict[AttributeKey, List[int]]],
    metric_name: str,
    datapoints: Iterable[Dict[str, Any]],
) -> None:
    for dp in datapoints:
        if "attributes" in dp:
            attribute_key = prepare_attributes_key(dp["attributes"])
        else:
            attribute_key = "No Attr"
        out[metric_name][attribute_key].append(int(dp["timeUnixNano"]))


def datapoint_value(datapoint: Dict[str, Any]) -> Optional[float]:
    """Numeric value of a scalar OTLP datapoint, or ``None`` when it carries none.

    ``asInt`` arrives as a JSON string for 64-bit safety, so both fields go through
    ``float``. A datapoint carrying neither field is not a scalar one (a histogram,
    say) and yields ``None``, which callers skip.

    A field that is present but will not parse is corruption rather than absence,
    so it yields NaN instead: reporting it as "no value" would drop it from the
    series and hide the very thing a value-sanity check is looking for.
    """
    for field in _OTLP_NUMBER_FIELDS:
        if field in datapoint:
            try:
                return float(datapoint[field])
            except (TypeError, ValueError):
                return float("nan")
    return None


def metric_values_from_docs(docs: Iterable[Dict[str, Any]]) -> MetricValues:
    """Return ``{metric_name: {attr_key: [value, ...]}}`` for scalar datapoints.

    The twin of :func:`metric_timestamps_from_docs`, keyed identically, so a caller
    can line a series' values up with its timestamps. Only ``gauge`` and ``sum``
    carry a scalar value; histograms are skipped. A scalar value that will not
    parse stays in its series as NaN — see :func:`datapoint_value`.
    """
    out: DefaultDict[str, DefaultDict[AttributeKey, List[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for doc in docs:
        for resource in doc.get("resourceMetrics", []) or []:
            for scope in resource.get("scopeMetrics", []) or []:
                for metric in scope.get("metrics", []) or []:
                    name = metric.get("name")
                    if not name:
                        continue
                    for mtype in ("gauge", "sum"):
                        payload = metric.get(mtype)
                        if not payload:
                            continue
                        for dp in payload.get("dataPoints", []) or []:
                            value = datapoint_value(dp)
                            if value is None:
                                continue
                            if "attributes" in dp:
                                attribute_key = prepare_attributes_key(dp["attributes"])
                            else:
                                attribute_key = "No Attr"
                            out[name][attribute_key].append(value)
    return {k: dict(v) for k, v in out.items()}


def metric_timestamps_from_docs(docs: Iterable[Dict[str, Any]]) -> MetricTimestamps:
    """Return ``{metric_name: {attr_key: [timeUnixNano, ...]}}`` from OTLP JSON docs."""
    out: DefaultDict[str, DefaultDict[AttributeKey, List[int]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for doc in docs:
        for resource in doc.get("resourceMetrics", []) or []:
            for scope in resource.get("scopeMetrics", []) or []:
                for metric in scope.get("metrics", []) or []:
                    name = metric.get("name")
                    if not name:
                        continue
                    for mtype in ("gauge", "histogram", "sum"):
                        payload = metric.get(mtype)
                        if not payload:
                            continue
                        _append_datapoint_timestamps(
                            out, name, payload.get("dataPoints", []) or []
                        )
    return {k: dict(v) for k, v in out.items()}
