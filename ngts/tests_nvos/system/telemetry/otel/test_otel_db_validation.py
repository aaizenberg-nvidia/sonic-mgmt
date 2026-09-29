"""NVOS OTEL telemetry — exported values and labels against the Redis databases they come from.

nv-umf-agent reads Redis and exports what ``agent-mappings.yaml`` tells it to, running
some values through a calculator or metric builder first. :mod:`db_reference` replays
that on Redis snapshots taken around the collection window, so every exported series
can be checked against the value UMF would have produced. Tolerances follow the
Cumulus platform-stats validation: counters must land between the two snapshots and
sampled gauges within a per-class percentage of either one; declared descriptive
labels (e.g. ``speed``) must equal the pre or post Redis value.

Metric *inventory* (which names should exist at all) is out of scope here - that is
:mod:`test_otel_metric_coverage`. A metric this test expects but did not see exported
only fails when Redis actually had a value for it; otherwise it is reported as not
validated rather than failed, and unexpected/extra OTEL series are ignored outright.
"""

from dataclasses import dataclass
from typing import Dict, List

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import ApiType, TelemetryConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.telemetry.otel import db_reference as ref
from ngts.tests_nvos.system.telemetry.otel.agent_mappings import (
    fetch_agent_mappings_yaml_from_dut,
    load_metric_mappings,
)
from ngts.tests_nvos.system.telemetry.otel.constants import (
    OtelCollectorConst,
    OtelDbValidationConst,
    OtelFunctionalTestConst,
)
from ngts.tests_nvos.system.telemetry.otel.helpers import (
    collect_otlp_metrics_window,
    collection_window_for_samples,
    enable_nvos_telemetry_families,
    expected_metric_names_from_dut,
    is_nvos_dut,
)
from ngts.tests_nvos.system.telemetry.otel.nvos_cli_helpers import unset_path
from ngts.tests_nvos.system.telemetry.otel.umf_calculators import UMF_SOURCE_COMMIT, CalculatorContext

pytestmark = [
    pytest.mark.system,
    pytest.mark.otel,
]


@pytest.fixture(autouse=True)
def otel_db_validation_nvue_default():
    """Run the family enablement over NVUE, restoring the previous API afterwards."""
    with TestToolkit.force_api(ApiType.NVUE):
        yield


def test_otel_all_metrics_vs_db(engines, devices, otel_suite_single, is_ib_router, tmp_path):
    """Every exported metric matches the value and labels UMF derives from Redis.

    Enables all telemetry families, snapshots the Redis rows behind a sample of ports
    (random up ports covering every operational speed, random down ports, their
    peers and transceiver modules) and every switch-level table, collects one window,
    snapshots again, and compares each datapoint stamped between the snapshots -
    both its value and its declared descriptive labels.

    Metric inventory is out of scope (see ``test_otel_all_metrics_present``): an
    expected metric or series only fails here when Redis actually has a value for
    it; unexpected metrics and extra OTEL series are ignored. Series whose value
    could not be derived (event metrics, calculators not yet ported, rows UMF itself
    rejects) are reported as not validated rather than failed.
    """
    if not is_nvos_dut(devices):
        pytest.skip("Redis-backed OTEL validation applies to NVOS only")

    dut = engines.dut
    local_dir = str(tmp_path)
    reader = ref.RedisSnapshotReader(dut)

    try:
        with allure.step("Enable every telemetry family"):
            enable_nvos_telemetry_families(
                dut,
                otel_suite_single.primary_ip,
                TelemetryConsts.ALL_STATS_SUBTREES,
                collector_ips=(otel_suite_single.primary_ip,),
                use_stats_group=False,
            )

        with allure.step("Load the metric contract and the expected metric names"):
            mappings = load_metric_mappings(fetch_agent_mappings_yaml_from_dut(engines, local_dir))
            expected = expected_metric_names_from_dut(
                engines,
                local_dir,
                OtelCollectorConst.METRICS_CLASSES_FULL_EXPORT_GROUP_PREFIXES,
                file_name="metrics-classes-full.yaml",
                is_ib_router=is_ib_router,
                devices=devices,
            )
            assert expected, "No expected metric names after capability gating."
            allure.attach("UMF source commit", UMF_SOURCE_COMMIT)

        with allure.step("Select ports: up ports of every speed, down ports, peers, modules"):
            discovery = reader.read(ref.discovery_requests(), label="discovery")
            maps = ref.build_interface_mappings(discovery)
            speeds = reader.read(ref.speed_requests(maps), label="speeds")
            selection = ref.select_ports(maps, discovery.merged(speeds))
            assert selection.ports, "No switch port found in STATE_DB IB_PORT_TABLE."
            allure.attach("Port selection", selection.describe())

        specs = [ref.parse_metric_spec(name, mappings[name]) for name in sorted(expected) if name in mappings]
        groups = ref.group_collectors(spec for spec in specs if not spec.unsupported)
        requests = ref.value_requests(groups, maps, selection)
        reference = _Reference(
            specs=specs,
            groups=groups,
            maps=maps,
            selection=selection,
            ctx=CalculatorContext(hostname=(dut.run_cmd("hostname", print_output=False) or "").strip()),
        )

        max_sample_interval_sec = max(
            OtelCollectorConst.INTERFACE_STATS_SAMPLE_INTERVAL_SEC,
            OtelCollectorConst.PEER_PORT_STATS_SAMPLE_INTERVAL_SEC,
            OtelCollectorConst.IB_ROUTER_STATS_SAMPLE_INTERVAL_SEC,
            OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC,
        )
        window_sec = collection_window_for_samples(
            max_sample_interval_sec, OtelFunctionalTestConst.MIN_SAMPLES_IN_WINDOW
        )

        with allure.step(f"Snapshot Redis, collect OTLP for {window_sec}s, snapshot Redis again"):
            pre = reader.read(requests, label="pre")
            exported, docs, _ = collect_otlp_metrics_window(
                otel_suite_single.primary,
                local_dir,
                label=OtelDbValidationConst.COLLECTION_LABEL,
                max_sample_interval_sec=max_sample_interval_sec,
                wait_sec=window_sec,
            )
            post = reader.read(requests, label="post")

        with allure.step("Compare every exported series with its Redis reference"):
            rows, ignored_by_family = _compare_all(reference, exported, docs, (pre, post))
            allure.attach("OTEL vs Redis summary", ref.summarize(rows, ignored_by_family))
            allure.attach("OTEL vs Redis series", ref.format_rows(rows))

        failures = [row for row in rows if row.result == ref.RESULT_FAIL]
        if failures:
            shown = ref.format_rows(failures[: OtelDbValidationConst.MAX_FAILURES_IN_MESSAGE])
            pytest.fail(
                f"{len(failures)} OTEL series disagree with Redis "
                f"(first {min(len(failures), OtelDbValidationConst.MAX_FAILURES_IN_MESSAGE)}):\n{shown}"
            )
    finally:
        with allure.step("Unset the telemetry export subtree and apply"):
            unset_path(System().telemetry.export, dut, apply=True)


@dataclass
class _Reference:
    """What the comparison needs besides the snapshots and the OTLP payload."""
    specs: List[ref.MetricSpec]
    groups: Dict[str, ref.CollectorGroup]
    maps: ref.InterfaceMappings
    selection: ref.PortSelection
    ctx: CalculatorContext


def _compare_all(reference: _Reference, exported, docs, snapshots) -> "tuple[List[ref.ComparisonRow], Dict[str, int]]":
    """Comparison rows for every metric, plus per-family ignored/not-exported counts.

    ``snapshots`` is ``(pre, post)``. A metric expected but not exported is only a
    failure when Redis has a value for it (``compare_metric`` on an empty OTEL side
    surfaces that as missing-series rows); otherwise it is reported not validated.
    """
    pre, post = snapshots
    otel = ref.otel_points_by_metric(docs)
    window = (pre.t_after_ns, post.t_before_ns - OtelDbValidationConst.SAMPLE_READ_MARGIN_NS)
    selection = reference.selection
    scope = set(selection.ports) | set(selection.peers) | selection.modules

    rows: List[ref.ComparisonRow] = []
    ignored_by_family: Dict[str, int] = {}

    def _bump_ignored(name: str) -> None:
        family = ref.metric_family(name)
        ignored_by_family[family] = ignored_by_family.get(family, 0) + 1

    for spec in reference.specs:
        if spec.unsupported:
            rows.append(_metric_row(spec.name, ref.RESULT_NOT_VALIDATED, spec.unsupported))
            continue

        group = reference.groups[spec.collector_id]
        pre_set = ref.build_expected_points(spec, ref.collector_rows(group, pre, reference.maps), reference.ctx)
        post_set = ref.build_expected_points(spec, ref.collector_rows(group, post, reference.maps), reference.ctx)

        if spec.name not in exported:
            inputs = ref.MetricInputs(spec=spec, pre=pre_set, post=post_set, otel=[], scope_values=scope)
            missing_rows, ignored = ref.compare_metric(inputs, window=window)
            for _ in range(ignored):
                _bump_ignored(spec.name)
            if missing_rows:
                rows.extend(missing_rows)
            elif pre_set.error_rows or post_set.error_rows:
                rows.append(_metric_row(spec.name, ref.RESULT_NOT_VALIDATED, "Redis row failed to parse"))
            else:
                rows.append(_metric_row(spec.name, ref.RESULT_NOT_VALIDATED, "not exported and no Redis source"))
            continue

        inputs = ref.MetricInputs(
            spec=spec, pre=pre_set, post=post_set, otel=otel.get(spec.name, []), scope_values=scope,
        )
        metric_rows, ignored = ref.compare_metric(inputs, window=window)
        for _ in range(ignored):
            _bump_ignored(spec.name)
        rows.extend(metric_rows)
    return rows, ignored_by_family


def _metric_row(name: str, result: str, detail: str) -> ref.ComparisonRow:
    """A whole-metric verdict (no per-series comparison was possible)."""
    return ref.ComparisonRow(
        metric=name, family=ref.metric_family(name), labels="", mode="-", otel=None,
        pre=None, post=None, result=result, detail=detail,
    )
