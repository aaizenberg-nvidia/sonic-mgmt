"""NVOS-only: validate exported OTLP datapoints against the UMF agent mapping.

On NVOS the UMF agent mapping file ``agent-mappings.yaml`` (its ``metricMappings``
section) is the contract for what each metric should look like: its value/metric
type and the attribute (label) set it may carry. This is the NVOS twin of the
Cumulus attribute-count / value-type checks in
:class:`~ngts.tests_nvos.system.telemetry.otel.cumulus.validations.OtelDataValidations`
(Cumulus has no such file, so this validation only runs on NVOS).

The check is one-directional (export -> contract): for every *exported* datapoint
we assert (a) the metric name exists in ``metricMappings``, (b) the OTLP container
type matches the declared type (``metricType: sum`` -> ``sum``, ``histogram`` ->
``histogram``, otherwise ``gauge``), and (c) the datapoint attribute keys are a
subset of the declared ``labels``. gNMI-only mapping entries that are never
exported need no datapoint, so we never require a mapping entry to appear in the
export.
"""

import logging
import os
import shlex
from typing import Any, Dict, Iterable, List, Optional, Set

import yaml

import ngts.tools.test_utils.allure_utils as allure
import pytest
from devts.infra.tools.linux_tools.linux_tools import scp_file

from ngts.tests_nvos.system.telemetry.otel.constants import OtelCollectorConst
from ngts.tests_nvos.system.telemetry.otel.helpers import _is_prometheus_sidecar_metric

logger = logging.getLogger(__name__)

# OTLP metric container -> mapping ``metricType`` value that produces it.
_OTLP_CONTAINER_TYPES = ("gauge", "sum", "histogram", "exponentialHistogram", "summary")


def _resolve_agent_mappings_remote_path(dut) -> Optional[str]:
    """Locate ``agent-mappings.yaml`` on the DUT (sudo-readable candidate paths)."""
    for path in OtelCollectorConst.AGENT_MAPPINGS_CANDIDATE_PATHS:
        out = dut.run_cmd(
            f"sudo test -f {shlex.quote(path)} && echo {shlex.quote(path)}",
            validate=False,
            print_output=False,
        ).strip()
        if out == path:
            logger.info("Found agent-mappings.yaml at %s", path)
            return path
    return None


def fetch_agent_mappings_yaml_from_dut(
    engines,
    local_output_dir: str,
    file_name: str = "agent-mappings.yaml",
) -> str:
    """SCP the DUT's ``agent-mappings.yaml`` locally (world-readable /tmp staging copy)."""
    os.makedirs(local_output_dir, exist_ok=True)
    local_path = os.path.join(local_output_dir, file_name)
    dut = engines.dut
    source = _resolve_agent_mappings_remote_path(dut)
    if not source:
        pytest.fail(
            "agent-mappings.yaml not found on DUT "
            f"(tried {OtelCollectorConst.AGENT_MAPPINGS_CANDIDATE_PATHS}). "
            "This file is the NVOS metric contract; it ships with nv-umf-agent."
        )
    staged = OtelCollectorConst.AGENT_MAPPINGS_STAGED_ON_DUT
    with allure.step(f"Fetch agent-mappings.yaml from DUT ({source})"):
        dut.run_cmd(
            f"sudo cp {shlex.quote(source)} {shlex.quote(staged)} && "
            f"sudo chmod 644 {shlex.quote(staged)}",
            validate=True,
        )
        try:
            scp_file(dut, staged, local_path, download_from_remote=True)
        finally:
            dut.run_cmd(f"sudo rm -f {shlex.quote(staged)}", validate=False)
    return local_path


def load_metric_mappings(yaml_path: str) -> Dict[str, Dict[str, Any]]:
    """Return the ``metricMappings`` mapping (metric name -> spec) from the YAML."""
    with open(yaml_path, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    mappings = doc.get("metricMappings", {})
    if not isinstance(mappings, dict) or not mappings:
        pytest.fail(
            f"agent-mappings.yaml has no usable metricMappings section: {yaml_path}"
        )
    return mappings


def _expected_container(spec: Dict[str, Any]) -> str:
    """Map a mapping ``metricType`` to the OTLP container key it should produce."""
    metric_type = str(spec.get("metricType", "")).strip().lower()
    if metric_type == "sum":
        return "sum"
    if metric_type == "histogram":
        return "histogram"
    return "gauge"


def _actual_container(metric: Dict[str, Any]) -> Optional[str]:
    for key in _OTLP_CONTAINER_TYPES:
        if key in metric:
            return key
    return None


def _declared_label_keys(spec: Dict[str, Any]) -> Set[str]:
    labels = spec.get("labels") or {}
    return set(labels.keys()) if isinstance(labels, dict) else set()


def _datapoint_attribute_keys(metric: Dict[str, Any], container: str) -> Set[str]:
    keys: Set[str] = set()
    for dp in metric.get(container, {}).get("dataPoints", []) or []:
        for attr in dp.get("attributes", []) or []:
            key = attr.get("key")
            if key:
                keys.add(key)
    return keys


def _iter_exported_metrics(docs: Iterable[Dict[str, Any]]):
    """Yield each metric object from parsed OTLP documents."""
    for doc in docs:
        for resource in doc.get("resourceMetrics", []) or []:
            for scope in resource.get("scopeMetrics", []) or []:
                for metric in scope.get("metrics", []) or []:
                    yield metric


def validate_otlp_conforms_to_agent_mappings(
    docs: List[Dict[str, Any]],
    mappings: Dict[str, Dict[str, Any]],
    *,
    collector_label: str,
    extra_ignore: Optional[Set[str]] = None,
    require_declared_attributes: bool = False,
) -> None:
    """Assert every exported NVUE datapoint conforms to the agent mapping contract.

    Prometheus sidecar metrics (``node_*`` / ``scrape_*`` / ...) and ``extra_ignore``
    names are skipped; only ``nvswitch_*`` telemetry is checked.

    ``require_declared_attributes`` additionally asserts the exported attribute keys
    are not a *strict* subset of the declared ``labels``, i.e. the metric carries its
    full declared attribute set. Off by default because the ported dual-platform
    suites only assert the no-undeclared-attribute direction; test-plan items that
    call for an attribute-count check turn it on.
    """
    ignore = set(extra_ignore or set())
    not_in_contract: Set[str] = set()
    type_mismatch: List[str] = []
    attr_violations: List[str] = []
    missing_attrs: List[str] = []
    checked: Set[str] = set()

    for metric in _iter_exported_metrics(docs):
        name = metric.get("name")
        if not name or name in ignore or _is_prometheus_sidecar_metric(name):
            continue
        if not name.startswith("nvswitch_"):
            continue

        spec = mappings.get(name)
        if spec is None:
            not_in_contract.add(name)
            continue

        checked.add(name)
        actual = _actual_container(metric)
        expected = _expected_container(spec)
        # gauge/sum are both numeric scalar containers; only flag a true mismatch
        # (e.g. histogram vs scalar) to avoid noise from agent gauge/sum nuances.
        if actual is None:
            type_mismatch.append(f"{name}: no OTLP container (expected {expected})")
        elif expected == "histogram" and actual not in ("histogram", "exponentialHistogram"):
            type_mismatch.append(f"{name}: expected histogram, got {actual}")
        elif expected != "histogram" and actual in ("histogram", "exponentialHistogram"):
            type_mismatch.append(f"{name}: expected {expected}, got {actual}")

        if actual is not None:
            declared = _declared_label_keys(spec)
            exported = _datapoint_attribute_keys(metric, actual)
            unexpected = exported - declared
            if unexpected:
                attr_violations.append(
                    f"{name}: attributes {sorted(unexpected)} not declared in mapping "
                    f"labels {sorted(declared)}"
                )
            # Union across the metric's datapoints: a declared label is satisfied by
            # appearing on any series, so per-series attributes are not miscounted.
            missing = declared - exported
            if require_declared_attributes and missing:
                missing_attrs.append(
                    f"{name}: exported {len(exported)} of {len(declared)} declared "
                    f"attribute(s), missing {sorted(missing)}"
                )

    with allure.step(
        f"Validate OTLP conforms to agent-mappings on {collector_label} "
        f"(checked={len(checked)})"
    ):
        logger.info(
            "agent-mappings validation (%s): checked=%d not_in_contract=%d "
            "type_mismatch=%d attr_violations=%d missing_attrs=%d",
            collector_label,
            len(checked),
            len(not_in_contract),
            len(type_mismatch),
            len(attr_violations),
            len(missing_attrs),
        )
        if not_in_contract or type_mismatch or attr_violations or missing_attrs:
            lines = [
                f"OTLP export does not conform to agent-mappings on {collector_label}.",
                f"  --- not in metricMappings ({len(not_in_contract)}) ---",
            ]
            lines.extend(sorted(not_in_contract) or ["  (none)"])
            lines.append(f"  --- type mismatches ({len(type_mismatch)}) ---")
            lines.extend(sorted(type_mismatch) or ["  (none)"])
            lines.append(f"  --- attribute violations ({len(attr_violations)}) ---")
            lines.extend(sorted(attr_violations) or ["  (none)"])
            lines.append(f"  --- incomplete attribute sets ({len(missing_attrs)}) ---")
            lines.extend(sorted(missing_attrs) or ["  (none)"])
            pytest.fail("\n".join(lines))


def expected_metrics_conform_to_agent_mappings(
    engines,
    local_output_dir: str,
    docs: List[Dict[str, Any]],
    *,
    collector_label: str,
    extra_ignore: Optional[Set[str]] = None,
    require_declared_attributes: bool = False,
) -> None:
    """Fetch ``agent-mappings.yaml`` from the DUT and validate ``docs`` against it."""
    yaml_path = fetch_agent_mappings_yaml_from_dut(engines, local_output_dir)
    mappings = load_metric_mappings(yaml_path)
    validate_otlp_conforms_to_agent_mappings(
        docs,
        mappings,
        collector_label=collector_label,
        extra_ignore=extra_ignore,
        require_declared_attributes=require_declared_attributes,
    )
