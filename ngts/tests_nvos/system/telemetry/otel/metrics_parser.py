"""Parse OTEL collector JSON artifacts into metric structures."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, DefaultDict, Dict, Iterable, List, Tuple, Union

AttributeKey = Union[Tuple[Any, ...], str]
MetricTimestamps = Dict[str, Dict[AttributeKey, List[int]]]


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
