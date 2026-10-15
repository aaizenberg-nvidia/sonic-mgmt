"""NVOS-only: classify OTEL metric names missing from the strict export check by Redis source.

The strict ``expected == exported`` metric-name check (:func:`helpers.assert_metric_names_strict`)
treats every expected name absent from the export as a failure. Not every gap is real:
a metric can be legitimately absent because its Redis source has no rows on this setup
(no HCA peers, no transceivers plugged in, ...) - the CLI can't show it either, so
OTEL can't be expected to export it. This module replays the same
agent-mappings -> Redis path as :mod:`db_reference` (existence only, no OTLP value
comparison) to decide, for each missing name, whether Redis actually has data for it.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, Set

from ngts.tests_nvos.system.telemetry.otel import db_reference as ref
from ngts.tests_nvos.system.telemetry.otel.agent_mappings import (
    fetch_agent_mappings_yaml_from_dut,
    load_metric_mappings,
)
from ngts.tests_nvos.system.telemetry.otel.umf_calculators import CalculatorContext

logger = logging.getLogger(__name__)


@dataclass
class MissingByDb:
    """Missing OTEL metric names, split by whether Redis has data for them.

    ``real_missing``: Redis has *something* under the declared field for this
    metric - whether a clean value or one that fails to parse as the declared
    type - so the metric should have been exported in some form. A type/format
    mismatch is a real, different bug from "no data", but it still means data
    exists; it does not make the gap disappear. ``not_verifiable``: we could not
    even attempt the check (event-driven metric, not-yet-ported calculator/builder, or
    a mapping entry with no declared value source at all) - a structural gap in
    what this replay can check, not a data problem. ``no_db_source``: Redis
    genuinely has no rows/field for this metric on this setup. Each dict maps
    metric name -> a short reason.
    """

    real_missing: Dict[str, str] = field(default_factory=dict)
    not_verifiable: Dict[str, str] = field(default_factory=dict)
    no_db_source: Dict[str, str] = field(default_factory=dict)


def classify_missing_by_db(engines, cur_dir: str, missing: Iterable[str]) -> MissingByDb:
    """Bucket each name in ``missing`` by whether Redis has a value for it.

    Replays the agent-mappings -> Redis path over *every* switch port/peer/module
    (:func:`db_reference.select_all_ports`, no sampling - a metric present only on
    a port outside the sample must not be misclassified as having no source). Returns an
    empty result without touching Redis when ``missing`` is empty.
    """
    result = MissingByDb()
    missing = sorted(set(missing))
    if not missing:
        return result

    dut = engines.dut
    mappings = load_metric_mappings(fetch_agent_mappings_yaml_from_dut(engines, cur_dir))

    specs = []
    for name in missing:
        mapping = mappings.get(name)
        if mapping is None:
            result.real_missing[name] = "not in agent-mappings.yaml"
            continue
        spec = ref.parse_metric_spec(name, mapping)
        if spec.unsupported:
            result.not_verifiable[name] = spec.unsupported
        else:
            specs.append(spec)

    if not specs:
        return result

    reader = ref.RedisSnapshotReader(dut)
    discovery = reader.read(ref.discovery_requests(), label="inventory-discovery")
    maps = ref.build_interface_mappings(discovery)
    selection = ref.select_all_ports(maps, discovery)

    groups = ref.group_collectors(specs)
    # No peers/transceivers means no keys resolve; that is "no source", not an error.
    snapshot = reader.read(
        ref.value_requests(groups, maps, selection), label="inventory-fallback", allow_empty=True,
    )
    ctx = CalculatorContext(hostname=(dut.run_cmd("hostname", print_output=False) or "").strip())

    for spec in specs:
        group = groups[spec.collector_id]
        expected_set = ref.build_expected_points(spec, ref.collector_rows(group, snapshot, maps), ctx)
        if expected_set.points:
            count = len(expected_set.points)
            result.real_missing[spec.name] = f"{count} Redis row{'s' if count != 1 else ''} have a value"
        elif expected_set.error_rows:
            # The declared field is present - Redis has *something* for this metric,
            # it just doesn't parse as the declared type. That is still "Redis has
            # data this metric should have exported", with a type/format problem
            # noted alongside it - not a case we simply couldn't check.
            first_key = sorted(expected_set.error_rows)[0]
            detail = expected_set.error_rows[first_key]
            extra = f" (+{len(expected_set.error_rows) - 1} more)" if len(expected_set.error_rows) > 1 else ""
            result.real_missing[spec.name] = f"Redis has a value but it fails to parse - {first_key}: {detail}{extra}"
        else:
            result.no_db_source[spec.name] = f"no source in {spec.db}:{spec.table}"

    logger.info(
        "OTEL missing-metric DB fallback: %d real_missing, %d not_verifiable, %d no_db_source",
        len(result.real_missing), len(result.not_verifiable), len(result.no_db_source),
    )
    return result


def format_reasons(items: Dict[str, str]) -> str:
    """``name: reason`` per line, sorted; ``(none)`` when empty - for Allure attachments."""
    lines = [f"{name}: {reason}" for name, reason in sorted(items.items())]
    return "\n".join(lines) or "(none)"


def summarize_missing_by_db(missing_by_db: MissingByDb, unexpected: Set[str]) -> str:
    """Per-family counts across the four inventory buckets, using :func:`db_reference.metric_family`."""
    buckets = {
        "missing": missing_by_db.real_missing,
        "not_verifiable": missing_by_db.not_verifiable,
        "no_db_source": missing_by_db.no_db_source,
        "unexpected": {name: "" for name in unexpected},
    }
    counts: Dict[str, Dict[str, int]] = {}
    for bucket_name, items in buckets.items():
        for name in items:
            family_counts = counts.setdefault(ref.metric_family(name), {})
            family_counts[bucket_name] = family_counts.get(bucket_name, 0) + 1

    header = f"{'family':<20} {'missing':>8} {'not_verifiable':>15} {'no_db_source':>13} {'unexpected':>11}"
    lines = [header]
    for family in sorted(counts):
        c = counts[family]
        lines.append(
            f"{family:<20} {c.get('missing', 0):>8} {c.get('not_verifiable', 0):>15} "
            f"{c.get('no_db_source', 0):>13} {c.get('unexpected', 0):>11}"
        )
    return "\n".join(lines)
