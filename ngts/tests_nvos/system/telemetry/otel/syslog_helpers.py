"""Helpers for the *Syslog over OTEL* (V1) test suite.

Layered on the existing OTEL harness (do not reinvent — see
:mod:`ngts.tests_nvos.system.telemetry.otel.helpers`):

* :func:`configure_receiver_for_logs` extends an :class:`OtelCollector` with a
  ``logs`` pipeline so exported log records are captured by the file exporter (the
  stock receiver config only defines a ``metrics`` pipeline).
* :func:`emit_syslog_marker` emits a unique marker line on the DUT via the syslog
  suite's ``send_msg_to_server`` (which runs ``logger`` on the DUT).
* :func:`marker_delivered` / :func:`assert_marker_delivered` /
  :func:`assert_marker_absent` implement the functional (marker-based) delivery
  check from the test plan Section 4.2.
* :func:`fetch_log_records` and its helpers parse the receiver's OTLP log records
  for the field-mapping test (Section 6.2).
"""

import logging
import shlex
import time
import uuid
from typing import Any, Dict, List, Optional

import ngts.tools.test_utils.allure_utils as allure
from ngts.cli_wrappers.common.general_clis_common import GeneralCliCommon
from ngts.nvos_constants.constants_nvos import ConfState, SyslogConsts, TelemetryConsts
from ngts.nvos_tools.infra.FilesTool import FilesTool
from ngts.nvos_tools.infra.SonicMgmtContainer import SonicMgmtContainer
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ResultObj import IssueType, ResultObj
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.telemetry.otel.constants import OtelCollectorConst
from ngts.tests_nvos.system.telemetry.otel.helpers import _read_otel_collector_documents
from ngts.tests_nvos.system.telemetry.otel.otel_collector import OtelCollector
from ngts.tests_nvos.system.telemetry.otel.syslog_constants import SyslogOtelConst
from ngts.tests_nvos.system.syslog.test_system_syslog import (
    add_msg_to_ignore_loganalyzer_regex,
    send_msg_to_server,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Feature detection
# =============================================================================


def is_syslog_otel_supported(dut) -> bool:
    """True when the build exposes the ``system telemetry logs`` CLI subtree.

    On an OTEL build without syslog-over-OTEL, NVUE rejects the ``logs`` node
    (``Error: 'logs' is not one of [...]``); tests use this to skip gracefully
    instead of failing on the missing knob.

    Deliberately a low-level probe: it must survive the "node absent" NVUE error,
    so it does not go through :func:`logs_export_state` (whose JSON parse assumes
    the node exists and would raise on an unsupported build).
    """
    out = dut.run_cmd("nv show system telemetry logs export") or ""
    return "is not one of" not in out


def logs_export_state(dut, rev=ConfState.OPERATIONAL):
    """Return `state` from `nv show system telemetry logs export` at the given rev.

    Returns ``None`` when the ``state`` key is absent (a supported build always
    echoes the default, so callers should treat ``None`` as an unexpected value,
    not as "feature missing" - that is :func:`is_syslog_otel_supported`'s job).
    """
    raw = System().telemetry.logs.export.show(dut_engine=dut, rev=rev)
    parsed = OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()
    return parsed.get(SyslogOtelConst.STATE) if isinstance(parsed, dict) else None


def set_logs_export_state(dut, state, *, apply=True):
    """`nv set system telemetry logs export state <state>` (optionally apply)."""
    System().telemetry.logs.export.set(
        SyslogOtelConst.STATE, state, apply=apply, ask_for_confirmation=True, dut_engine=dut
    ).verify_result()


def set_logs_export_state_expect_reject(dut, state):
    """`nv set … logs export state <state>` + apply that must be rejected."""
    System().telemetry.logs.export.set(
        SyslogOtelConst.STATE, state, apply=True, ask_for_confirmation=True, dut_engine=dut
    ).verify_result(should_succeed=False)


def unset_logs_export(dut, *, apply=True):
    """`nv unset system telemetry logs export` (optionally apply)."""
    System().telemetry.logs.export.unset(
        apply=apply, ask_for_confirmation=True, dut_engine=dut
    ).verify_result()


def running_config_has_logs_export(dut):
    """True when `nv config show` carries an explicit telemetry logs/export/state key.

    Distinguishes an explicit ``set … state disabled`` (key present) from ``unset``
    (key absent), which both read back as ``disabled`` via ``nv show``.
    """
    out = dut.run_cmd("nv config show") or ""
    lines = out.splitlines()
    for idx, line in enumerate(lines):
        if line.strip().rstrip(":") == TelemetryConsts.LOGS:
            window = " ".join(lines[idx: idx + 6])
            if TelemetryConsts.EXPORT in window and SyslogOtelConst.STATE in window:
                return True
    return False


# =============================================================================
# OTLP export prerequisites (single insecure gRPC destination)
# =============================================================================
# The shared helpers (`configure_switch_otlp_grpc_dual_destination`,
# `_apply_otlp_grpc_base`, `cleanup_switch_otlp_grpc`) bundle master-enable +
# destination(s) + apply into one call. The prerequisite-progression test
# (plan Section 7.1) needs to add / remove a SINGLE destination as its own
# observable step, so it uses these finer-grained wrappers instead.


def add_otlp_destination(dut, collector_ip, port=TelemetryConsts.Defaults.GRPC_PORT):
    """Add a single OTLP/gRPC destination (+ port), insecure, and apply."""
    grpc = System().telemetry.export.otlp.grpc
    grpc.destination.set_resource(collector_ip).verify_result()
    grpc.destination.resources_dict[collector_ip].set(
        TelemetryConsts.PORT, port, dut_engine=dut
    ).verify_result()
    grpc.set(TelemetryConsts.INSECURE, SyslogOtelConst.ENABLED, apply=True,
             ask_for_confirmation=True, dut_engine=dut).verify_result()


def remove_otlp_destination(dut, collector_ip):
    """Remove a single OTLP/gRPC destination and apply."""
    # A fresh System() has an empty resources_dict (populated by set_resource, not by querying
    # the DUT), so build the destination resource path directly and unset it; guarding on
    # `collector_ip in resources_dict` would always skip the unset.
    destination = System().telemetry.export.otlp.grpc.destination
    resource = destination.resources_dict.get(collector_ip) or destination._create_resource(collector_ip)
    resource.unset(apply=True, ask_for_confirmation=True, dut_engine=dut).verify_result()


# =============================================================================
# Device-scoped telemetry label (plan Section 6.3)
# =============================================================================


def set_device_label(dut, label_id: str, description: str, *, apply=True) -> None:
    """`nv set system telemetry label <id> description <value>` (optionally apply)."""
    labels = System().telemetry.label
    labels.set_resource(label_id).verify_result()
    labels.resources_dict[label_id].set(
        SyslogOtelConst.LABEL_DESCRIPTION_KEY, description,
        apply=apply, ask_for_confirmation=True, dut_engine=dut,
    ).verify_result()


def label_value_in_record(record: Dict[str, Any], label_id: str, value: str) -> bool:
    """True when the record carries the device label as a resource attribute.

    The feature owner confirmed the label lands as a bare resource attribute keyed by the
    literal ``label_id``; we also accept any resource attribute whose value equals the
    description as a fallback, so a key-naming difference still surfaces the value.
    """
    resource_attrs = dict(record.get("resource_attributes") or {})
    if str(resource_attrs.get(label_id)) == value:
        return True
    return value in {str(v) for v in resource_attrs.values()}


def assert_label_on_record(record: Dict[str, Any], label_id: str, value: str) -> None:
    """Fail if the device label is absent from the record's resource attributes."""
    resource_attrs = dict(record.get("resource_attributes") or {})
    ResultObj(
        label_value_in_record(record, label_id, value),
        "device label {!r}={!r} not found in record resource attributes {}".format(
            label_id, value, sorted(resource_attrs.keys())
        ),
        issue_type=IssueType.PossibleBug,
    ).verify_result()


# =============================================================================
# Receiver: add a logs pipeline
# =============================================================================


def logs_receiver_config_yaml(collector: OtelCollector) -> str:
    """OTEL collector config with BOTH a ``metrics`` and a ``logs`` pipeline (insecure).

    Both pipelines share the single OTLP gRPC receiver and the file exporter, so
    exported log records land in the same artifact as metrics (parsed via
    ``resourceLogs`` rather than ``resourceMetrics``). Metrics keep flowing so the
    reconfigure does not disturb the metrics tests.
    """
    endpoint = "{}:{}".format(
        OtelCollectorConst.OTLP_GRPC_BIND_ADDR, OtelCollectorConst.OTLP_GRPC_PORT
    )
    return """receivers:
  otlp:
    protocols:
      grpc:
        endpoint: {endpoint}
processors: {{}}
exporters:
  file:
    path: {path}
    rotation:
      max_megabytes: {max_mb}
      max_backups: {max_backups}
service:
  telemetry:
    metrics:
      level: none
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [file]
    logs:
      receivers: [otlp]
      exporters: [file]
""".format(
        endpoint=endpoint,
        path=collector.output_json_path,
        max_mb=OtelCollectorConst.FILE_EXPORT_MAX_MB_BY_LABEL[collector.label],
        max_backups=OtelCollectorConst.FILE_EXPORT_MAX_BACKUPS_BY_LABEL[collector.label],
    )


def configure_receiver_for_logs(collector: OtelCollector) -> None:
    """Reconfigure ``collector`` with a logs+metrics pipeline and restart it."""
    with allure.step("Reconfigure receiver {} with a logs pipeline".format(collector.label.value)):
        collector.config_yaml = logs_receiver_config_yaml(collector)
        collector.ensure_running(install_if_missing=False)


def logs_receiver_config_yaml_tls(collector: OtelCollector) -> str:
    """Collector config with metrics+logs pipelines over a TLS OTLP gRPC receiver.

    Same shape as :func:`logs_receiver_config_yaml` but keeps the server-side ``tls``
    block from the secured suite so adding the logs pipeline does not drop TLS. Reuses
    the server cert/key the secured suite already staged on the collector.
    """
    from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst

    endpoint = "{}:{}".format(
        OtelCollectorConst.OTLP_GRPC_BIND_ADDR, OtelCollectorConst.OTLP_GRPC_PORT
    )
    return """receivers:
  otlp:
    protocols:
      grpc:
        endpoint: {endpoint}
        tls:
          cert_file: {cert_file}
          key_file: {key_file}
processors: {{}}
exporters:
  file:
    path: {path}
    rotation:
      max_megabytes: {max_mb}
      max_backups: {max_backups}
service:
  telemetry:
    metrics:
      level: none
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [file]
    logs:
      receivers: [otlp]
      exporters: [file]
""".format(
        endpoint=endpoint,
        cert_file=CumulusOtelConst.OTEL_TLS_SERVER_CRT,
        key_file=CumulusOtelConst.OTEL_TLS_SERVER_KEY,
        path=collector.output_json_path,
        max_mb=OtelCollectorConst.FILE_EXPORT_MAX_MB_BY_LABEL[collector.label],
        max_backups=OtelCollectorConst.FILE_EXPORT_MAX_BACKUPS_BY_LABEL[collector.label],
    )


def configure_receiver_for_logs_secured(collector: OtelCollector) -> None:
    """Reconfigure a TLS ``collector`` with a logs+metrics pipeline and restart it."""
    with allure.step("Reconfigure secured receiver {} with a logs pipeline".format(collector.label.value)):
        collector.config_yaml = logs_receiver_config_yaml_tls(collector)
        collector.ensure_running(install_if_missing=False)


# =============================================================================
# Emit markers on the DUT
# =============================================================================


def _marker_extra_flags(proc_id: Optional[str], structured_data: bool) -> str:
    """Build raw ``logger`` flags for a process-id and/or structured-data marker.

    process-id uses ``--id=<pid>`` in default (RFC 3164) mode, which makes ``logger`` write
    the source line as ``tag[pid]:``; rsyslog derives PROCID from that form -> the exported
    ``proc_id`` attribute (plan Section 6.2). Structured data (RFC 5424 ``--sd-*``) only
    survives an rsyslog network input, not the local ``/dev/log`` socket, so it is emitted
    only for the deferred network-input leg; over local injection the SD field is correctly
    empty. Returns ``''`` when neither is requested; otherwise a leading-space-prefixed
    string, matching ``send_msg_to_server``'s flag convention.
    """
    if not proc_id and not structured_data:
        return ""
    flags = []
    if proc_id:
        flags.append("--id={}".format(proc_id))
    if structured_data:
        flags.append("--rfc5424")
        flags.append("--sd-id {}".format(SyslogOtelConst.STRUCTURED_DATA_ID))
        flags.append(
            "--sd-param '{}=\"{}\"'".format(
                SyslogOtelConst.STRUCTURED_DATA_PARAM_KEY,
                SyslogOtelConst.STRUCTURED_DATA_PARAM_VALUE,
            )
        )
    return " " + " ".join(flags)


def emit_syslog_marker(
    marker: Optional[str] = None,
    *,
    program: str = SyslogOtelConst.MARKER_PROGRAM,
    facility: str = SyslogOtelConst.MARKER_FACILITY,
    priority: Optional[str] = None,
    proc_id: Optional[str] = None,
    structured_data: bool = False,
    loganalyzer=None,
) -> str:
    """Emit a unique syslog marker on the DUT and return it.

    Wraps the syslog suite's ``send_msg_to_server`` (runs ``logger`` on
    ``TestToolkit.engines.dut``). When a ``loganalyzer`` mapping is passed, the
    marker is added to its ignore list first so the emitted line is not flagged.
    Passing ``proc_id`` emits ``--id=<pid>`` so the line carries a PID (exported as the
    ``proc_id`` attribute); ``structured_data`` adds an RFC 5424 ``[session@1234 ...]``
    element (only observable over a network input, see ``_marker_extra_flags``).
    """
    marker = marker or SyslogOtelConst.new_marker()
    if loganalyzer:
        add_msg_to_ignore_loganalyzer_regex(loganalyzer, marker)
    additional_flags = _marker_extra_flags(proc_id, structured_data)
    with allure.step("Emit syslog marker {}".format(marker)):
        send_msg_to_server(
            marker,
            server_name="otel-syslog",
            server_engine=TestToolkit.engines.dut,
            priority=priority,
            program=program,
            facility=facility,
            additional_flags=additional_flags,
        )
    return marker


def _emit_burst_loop(count: int, interval_sec: float, program: str, loganalyzer) -> str:
    """Emit ``count`` lines on the DUT in one shell loop, then a unique sentinel last.

    ``interval_sec`` paces the loop (0 for a back-to-back burst). Returns the sentinel
    marker whose delivery the caller asserts.
    """
    sentinel = SyslogOtelConst.new_marker()
    if loganalyzer:
        add_msg_to_ignore_loganalyzer_regex(loganalyzer, sentinel)
        add_msg_to_ignore_loganalyzer_regex(loganalyzer, SyslogOtelConst.STRESS_BODY_PREFIX)
    body = SyslogOtelConst.STRESS_BODY_PREFIX
    pace = "sleep {};".format(interval_sec) if interval_sec else ""
    cmd = (
        'for i in $(seq 1 {count}); do logger -t {tag} "{body}_$i"; {pace} done; '
        'logger -t {tag} "{sentinel}"'
    ).format(count=count, tag=program, body=body, pace=pace, sentinel=sentinel)
    TestToolkit.engines.dut.run_cmd(cmd)
    return sentinel


def emit_syslog_burst(
    count: int,
    *,
    program: str = SyslogOtelConst.MARKER_PROGRAM,
    loganalyzer=None,
) -> str:
    """Emit a back-to-back burst of ``count`` markers; return the sentinel emitted last."""
    with allure.step("Emit burst of {} syslog markers".format(count)):
        return _emit_burst_loop(count, 0.0, program, loganalyzer)


def emit_syslog_sustained(
    msgs_per_sec: int,
    duration_sec: int,
    *,
    program: str = SyslogOtelConst.MARKER_PROGRAM,
    loganalyzer=None,
) -> str:
    """Emit markers at ``msgs_per_sec`` for ``duration_sec``; return the sentinel emitted last."""
    total = max(1, msgs_per_sec * duration_sec)
    interval = 1.0 / msgs_per_sec if msgs_per_sec else 0.0
    with allure.step("Emit {} markers/s for {}s".format(msgs_per_sec, duration_sec)):
        return _emit_burst_loop(total, interval, program, loganalyzer)


def emit_syslog_counted_batch(
    count: int,
    *,
    interval_sec: float = 0.0,
    program: str = SyslogOtelConst.MARKER_PROGRAM,
    loganalyzer=None,
) -> str:
    """Emit ``count`` uniquely-numbered markers; return the batch id for drop counting.

    Each line body is ``<batch-id>_<n>`` (n = 1..count), so the receiver-side count of
    distinct ``<batch-id>_<n>`` tokens gives the delivered count and
    ``count - delivered`` is the drop delta (plan Section 8.1 / 9.1). ``interval_sec``
    paces the loop (0 for a back-to-back burst).
    """
    batch_id = "{}_{}".format(SyslogOtelConst.DROP_MEASURE_BODY_PREFIX, uuid.uuid4().hex)
    if loganalyzer:
        add_msg_to_ignore_loganalyzer_regex(loganalyzer, batch_id)
    pace = "sleep {};".format(interval_sec) if interval_sec else ""
    cmd = (
        'for i in $(seq 1 {count}); do logger -t {tag} "{batch}_$i"; {pace} done'
    ).format(count=count, tag=program, batch=batch_id, pace=pace)
    with allure.step("Emit counted batch of {} markers (batch {})".format(count, batch_id)):
        TestToolkit.engines.dut.run_cmd(cmd)
    return batch_id


def count_markers_delivered(collector: OtelCollector, batch_id: str) -> int:
    """Return the number of distinct ``<batch-id>_<n>`` tokens in the receiver artifact."""
    live = shlex.quote(collector.output_json_path)
    rotated = collector.output_json_rotated_glob or ""
    grep_targets = live if not rotated or rotated == collector.output_json_path else "{} {}".format(live, rotated)
    # -oE emits one match per token; sort -u de-dups tokens that repeat across rotated files.
    inner = "sudo grep -hoE {pat} {targets} 2>/dev/null | sort -u | wc -l || true".format(
        pat=shlex.quote(batch_id + "_[0-9]+"), targets=grep_targets
    )
    out = collector.engine.run_cmd("bash -lc " + shlex.quote(inner), validate=False, print_output=False)
    try:
        return int((out or "0").strip().splitlines()[-1].strip())
    except (ValueError, IndexError):
        return 0


def wait_batch_settled(
    collector: OtelCollector,
    batch_id: str,
    expected: int,
    *,
    timeout_sec: int = SyslogOtelConst.DELIVERY_TIMEOUT_SEC,
    poll_interval_sec: int = SyslogOtelConst.MARKER_POLL_INTERVAL_SEC,
) -> int:
    """Poll until the delivered count for ``batch_id`` reaches ``expected`` or ``timeout_sec``.

    Returns the final delivered count (caller computes drops = emitted - returned).
    """
    with allure.step("Wait up to {}s for the counted batch to drain".format(timeout_sec)):
        deadline = time.time() + timeout_sec
        delivered = count_markers_delivered(collector, batch_id)
        while delivered < expected and time.time() < deadline:
            time.sleep(poll_interval_sec)
            delivered = count_markers_delivered(collector, batch_id)
        return delivered


# =============================================================================
# Delivery check (marker-based, plan Section 4.2)
# =============================================================================


def _receiver_has_marker(collector: OtelCollector, marker: str) -> bool:
    """True when ``marker`` is present in the receiver's file-exporter artifact."""
    live = shlex.quote(collector.output_json_path)
    rotated = collector.output_json_rotated_glob or ""
    # rotated glob stays UNQUOTED so the remote shell expands it; the marker is
    # shell-quoted and passed as a fixed string (``-F``) after ``--`` so a caller-supplied
    # marker with whitespace, special shell characters, or a leading ``-`` cannot break or
    # re-interpret the grep. Trailing ``|| true`` keeps exit 0 when nothing matches
    # (a normal "not yet" state).
    grep_targets = live if not rotated or rotated == collector.output_json_path else "{} {}".format(live, rotated)
    inner = "sudo grep -h -F -- {marker} {targets} 2>/dev/null | head -1 || true".format(
        marker=shlex.quote(marker), targets=grep_targets
    )
    out = collector.engine.run_cmd("bash -lc " + shlex.quote(inner), validate=False, print_output=False)
    return bool(out and out.strip())


def marker_delivered(
    collector: OtelCollector,
    marker: str,
    timeout_sec: int = SyslogOtelConst.DELIVERY_TIMEOUT_SEC,
    poll_interval_sec: int = SyslogOtelConst.MARKER_POLL_INTERVAL_SEC,
) -> bool:
    """Poll the receiver artifact for ``marker`` up to ``timeout_sec``; return arrival bool."""
    with allure.step("Wait up to {}s for marker at receiver".format(timeout_sec)):
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if _receiver_has_marker(collector, marker):
                return True
            time.sleep(poll_interval_sec)
        return _receiver_has_marker(collector, marker)


def wait_forwarding_active(
    collector: OtelCollector,
    loganalyzer=None,
    timeout_sec: int = SyslogOtelConst.FORWARDING_READY_TIMEOUT_SEC,
) -> None:
    """Block until syslog-over-OTEL forwarding is active, or timeout_sec elapses (best-effort gate)."""
    # `nv config apply` restarts the collector container, so forwarding resumes ~17-41s after
    # apply returns, not instantly. Emit throwaway readiness markers, each polled up to
    # FORWARDING_ACTIVE_PROBE_SEC (must exceed that latency, else an in-flight marker is never
    # caught), until one is delivered. The caller's own delivery assertion is authoritative.
    with allure.step("Wait until syslog-over-OTEL forwarding is active"):
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            readiness_marker = emit_syslog_marker(loganalyzer=loganalyzer)
            if marker_delivered(collector=collector, marker=readiness_marker, timeout_sec=SyslogOtelConst.FORWARDING_ACTIVE_PROBE_SEC):
                return
        logger.warning("syslog-over-OTEL forwarding not active after %ss; caller will assert", timeout_sec)


def wait_forwarding_inactive(
    collector: OtelCollector,
    loganalyzer=None,
    timeout_sec: int = SyslogOtelConst.FORWARDING_READY_TIMEOUT_SEC,
) -> None:
    """Block until syslog-over-OTEL forwarding is torn down, or timeout_sec elapses (best-effort gate)."""
    # Teardown restarts the collector, so a marker emitted right after may still be
    # forwarded. Emit throwaway readiness markers until one is NOT delivered; the caller's
    # own absence assertion is authoritative.
    with allure.step("Wait until syslog-over-OTEL forwarding is inactive"):
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            readiness_marker = emit_syslog_marker(loganalyzer=loganalyzer)
            if not marker_delivered(collector=collector, marker=readiness_marker, timeout_sec=SyslogOtelConst.FORWARDING_INACTIVE_PROBE_SEC):
                return
        logger.warning("syslog-over-OTEL forwarding still active after %ss; caller will assert", timeout_sec)


def assert_marker_delivered(
    collector: OtelCollector,
    marker: str,
    timeout_sec: int = SyslogOtelConst.DELIVERY_TIMEOUT_SEC,
) -> None:
    """Fail if ``marker`` does not reach the receiver within ``timeout_sec`` (pipeline active)."""
    ResultObj(
        marker_delivered(collector=collector, marker=marker, timeout_sec=timeout_sec),
        "Marker {!r} did not reach the receiver within {}s (expected forwarding active).".format(
            marker, timeout_sec
        ),
        issue_type=IssueType.PossibleBug,
    ).verify_result()


def assert_marker_absent(
    collector: OtelCollector,
    marker: str,
    timeout_sec: int = SyslogOtelConst.DELIVERY_TIMEOUT_SEC,
) -> None:
    """Fail if ``marker`` reaches the receiver within ``timeout_sec`` (pipeline inactive)."""
    ResultObj(
        not marker_delivered(collector=collector, marker=marker, timeout_sec=timeout_sec),
        "Marker {!r} reached the receiver within {}s (expected no forwarding).".format(
            marker, timeout_sec
        ),
        issue_type=IssueType.PossibleBug,
    ).verify_result()


# =============================================================================
# Product-health probes (regression guards for known collector/rsyslog defects)
# =============================================================================


def collector_service_state(dut, service: str) -> str:
    """Return the on-box collector unit's ``systemctl is-active`` state."""
    out = dut.run_cmd("sudo systemctl is-active {} 2>&1".format(shlex.quote(service)), validate=False)
    return out.strip().splitlines()[-1].strip() if out and out.strip() else ""


def assert_collector_service_not_crashed(dut, service: str) -> None:
    """Fail if the on-box collector unit is in a failed / crash-looping state."""
    # General robustness guard for the "syslog + OTLP export enabled but no destination"
    # state: the feature owner did NOT observe a crash (the hardening came from code
    # analysis), so this asserts the pipeline stays healthy by design rather than
    # reproducing a known crash. The intended healthy state here is legitimately "inactive":
    # NVUE health logic keeps the collector stopped unless OTLP is enabled with at least one
    # destination, so "inactive" (and "active") pass; only "failed"/"activating" (crash-loop)
    # is a defect.
    state = collector_service_state(dut, service)
    ResultObj(
        state not in ("failed", "activating"),
        "collector unit {!r} is {!r} (it must not crash when syslog-over-OTEL is enabled "
        "without an OTLP destination).".format(service, state),
        issue_type=IssueType.PossibleBug,
    ).verify_result()


def assert_rsyslog_fragment_removed_on_disable(dut) -> None:
    """Prove disable tore down the syslog ingestion path: the rsyslog drop-in is gone.

    Pair with the caller's forwarding-inactive check (a marker no longer reaches the
    collector) for the full disable proof -- the feature owner confirmed fragment-absent +
    forwarding-inactive is the strongest functional check. We deliberately do NOT assert an
    rsyslog reload marker: disable's rsyslog refresh is async (NVUE writes CONFIG_DB ->
    hostcfgd -> nv-umf-manager removes the drop-in and runs ``systemctl reload-or-restart
    rsyslog``), and stock NVOS rsyslog has no ExecReload so that is a full RESTART logged to
    the systemd journal (``exiting on signal 15`` / ``Started rsyslog.service``) -- never
    ``rsyslogd was HUPed`` (those come from logrotate). The suite reads ``/var/log/syslog``,
    not the journal, and the async refresh cannot be timed cleanly, so fragment-absent is
    the reliable on-disk proof.
    """
    FilesTool.file_exists_sudo(dut, SyslogOtelConst.RSYSLOG_FRAGMENT_PATH).verify_result(
        should_succeed=False
    )


def _unit_is_loaded(dut, unit: str) -> bool:
    """True when systemd knows the unit (``LoadState`` is not not-found/unknown)."""
    out = dut.run_cmd(
        "sudo systemctl show -p LoadState --value {} 2>&1".format(shlex.quote(unit)),
        validate=False,
    )
    state = (out or "").strip().splitlines()[-1].strip() if out and out.strip() else ""
    return state == "loaded"


def core_daemon_states(dut, units=SyslogOtelConst.CORE_DAEMONS) -> Dict[str, str]:
    """Return ``{unit: is-active}`` for each loaded core daemon (skips units not present)."""
    states: Dict[str, str] = {}
    for unit in units:
        if _unit_is_loaded(dut, unit):
            states[unit] = collector_service_state(dut, unit)
    return states


def assert_core_daemons_healthy(dut, units=SyslogOtelConst.CORE_DAEMONS) -> None:
    """Fail if any *loaded* core NOS daemon is not active.

    A syslog-over-OTEL failure/overload is non-critical (plan Section 6.7b step 5 /
    8.1 step 5) and must not degrade the switch. Only asserts daemons that are actually
    loaded, so it never false-fails on a unit the platform does not run (IB vs NVLink,
    container-hosted daemons). Soft per daemon so every one is reported.
    """
    states = core_daemon_states(dut, units)
    for unit, state in states.items():
        with allure.independent_step("core daemon {} is active".format(unit)):
            ResultObj(
                state == "active",
                "core daemon {!r} is {!r} (syslog-over-OTEL must not degrade core NOS "
                "daemons).".format(unit, state),
                issue_type=IssueType.PossibleBug,
            ).verify_result()


def make_receiver_unreachable(collector: OtelCollector) -> None:
    """Stop the external OTLP receiver so the DUT exporter cannot connect (plan 8.1 step 3)."""
    with allure.step("Make the OTLP receiver unreachable (stop the collector)"):
        collector.stop()


def restore_receiver(collector: OtelCollector) -> None:
    """Restart the external OTLP receiver (plan 8.1 step 4)."""
    with allure.step("Restore the OTLP receiver (restart the collector)"):
        collector.ensure_running(install_if_missing=False)


def break_syslog_ingestion(dut) -> None:
    """Break the staged pipeline by removing the rsyslog fragment + reloading rsyslog.

    Simulates a broken ingestion path while the feature is enabled (plan Section 6.7b
    step 4): the fragment is moved aside and rsyslog restarted so it no longer forwards
    to the collector's ingestion socket. Reverse with :func:`restore_syslog_ingestion`.
    """
    fragment = SyslogOtelConst.RSYSLOG_FRAGMENT_PATH
    with allure.step("Break the syslog ingestion path (remove the rsyslog fragment)"):
        dut.run_cmd("sudo mv {frag} {frag}.bak".format(frag=shlex.quote(fragment)), validate=False)
        dut.run_cmd("sudo systemctl restart rsyslog", validate=False)


def restore_syslog_ingestion(dut) -> None:
    """Restore the rsyslog fragment removed by :func:`break_syslog_ingestion` + reload."""
    fragment = SyslogOtelConst.RSYSLOG_FRAGMENT_PATH
    with allure.step("Restore the syslog ingestion path (re-stage the rsyslog fragment)"):
        dut.run_cmd("sudo mv {frag}.bak {frag}".format(frag=shlex.quote(fragment)), validate=False)
        dut.run_cmd("sudo systemctl restart rsyslog", validate=False)


def find_log_error_patterns(dut, patterns=None, source=None) -> List[str]:
    """Return the on-switch log-error patterns (plan §6.7b) that currently match.

    ``patterns`` defaults to :data:`SyslogOtelConst.LOG_ERROR_PATTERNS`, which is empty
    until the feature owner confirms the real strings (plan §11 open issue 2). With no
    patterns this returns ``[]`` and the caller skips the log-error leg.
    """
    patterns = SyslogOtelConst.LOG_ERROR_PATTERNS if patterns is None else patterns
    source = source or SyslogOtelConst.LOG_ERROR_SOURCE
    found: List[str] = []
    for pattern in patterns:
        cmd = "sudo grep -a -E {pat} {src} 2>/dev/null | tail -1 || true".format(
            pat=shlex.quote(pattern), src=shlex.quote(source)
        )
        out = dut.run_cmd("bash -lc " + shlex.quote(cmd), validate=False, print_output=False)
        if out and out.strip():
            found.append(pattern)
    return found


def local_syslog_has_marker(dut, marker: str) -> bool:
    """True when ``marker`` is in the DUT's local syslog (text-mode grep)."""
    cmd = "sudo grep -a -F {} {}".format(shlex.quote(marker), SyslogConsts.SYSLOG_LOG_PATH)
    out = dut.run_cmd(cmd, validate=False, print_output=False)
    return bool(out) and marker in out


def assert_marker_in_local_syslog(dut, marker: str) -> None:
    """Fail if ``marker`` is absent from the DUT's local syslog (legacy logging path)."""
    ResultObj(
        local_syslog_has_marker(dut, marker),
        "marker {!r} not found in {} (local logging path).".format(
            marker, SyslogConsts.SYSLOG_LOG_PATH
        ),
        issue_type=IssueType.PossibleBug,
    ).verify_result()


# =============================================================================
# Legacy remote syslog server (plan Section 6.5 remote leg)
# =============================================================================


def enable_remote_syslog_reception(server_engine) -> str:
    """Enable UDP syslog reception on the remote server; return the config backup path.

    Un-comments imudp in the server's rsyslog.conf and restarts rsyslogd (reuses
    ``SonicMgmtContainer``). Mirrors the syslog suite's session fixture, which only
    applies under that suite — the OTEL suite must prep the server itself.
    """
    backup = SyslogOtelConst.REMOTE_RSYSLOG_CONF_BACKUP
    GeneralCliCommon(server_engine).cp(SyslogConsts.RSYSLOG_CONF_FILE, backup)
    SonicMgmtContainer.enable_rsyslog(server_engine, SyslogConsts.UDP, restart_rsyslog=True)
    return backup


def restore_remote_syslog_reception(server_engine, backup: str) -> None:
    """Restore the remote server's original rsyslog.conf and restart rsyslogd."""
    GeneralCliCommon(server_engine).cp(backup, SyslogConsts.RSYSLOG_CONF_FILE)
    SonicMgmtContainer.restart_rsyslog(server_engine)


def configure_remote_syslog_server(dut, server_id: str, *, is_eth: bool = False) -> None:
    """Configure the DUT to forward syslog to a legacy remote server (+ apply).

    ``server_id`` is the remote server IP (matches the syslog suite's IPv4 flow — no
    reliance on the DUT resolving a reverse-DNS hostname).
    """
    servers = System().syslog.servers
    servers.set_server(server_id, apply=True)
    # eth (NVLink) platforms reach the server over the mgmt VRF; IB uses default.
    if is_eth:
        servers.servers_dict[server_id].set_vrf(
            TestToolkit.devices.dut.vrf_mgmt, apply=True, ask_for_confirmation=True
        )


def unset_remote_syslog_servers(dut) -> None:
    """Remove all configured remote syslog servers (+ apply)."""
    System().syslog.servers.unset(apply=True, ask_for_confirmation=True)


def remote_server_has_marker(
    server_engine,
    marker: str,
    timeout_sec: int = SyslogOtelConst.REMOTE_SYSLOG_DELIVERY_TIMEOUT_SEC,
    poll_interval_sec: int = SyslogOtelConst.MARKER_POLL_INTERVAL_SEC,
) -> bool:
    """Poll the remote server's syslog for ``marker`` up to ``timeout_sec``."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if local_syslog_has_marker(server_engine, marker):
            return True
        time.sleep(poll_interval_sec)
    return local_syslog_has_marker(server_engine, marker)


def assert_marker_at_remote_server(server_engine, marker: str) -> None:
    """Fail if ``marker`` does not reach the legacy remote syslog server."""
    ResultObj(
        remote_server_has_marker(server_engine, marker),
        "marker {!r} did not reach the legacy remote syslog server.".format(marker),
        issue_type=IssueType.PossibleBug,
    ).verify_result()


# =============================================================================
# Grey-box config inspection (plan Section 6.7a / 10.4)
# =============================================================================


def collector_config_has_logs(dut, token: str = SyslogOtelConst.COLLECTOR_LOGS_CONFIG_TOKEN) -> bool:
    """True when the effective collector config carries the logs-pipeline ``token``."""
    # read_file_content always succeeds (missing/unreadable file -> error text as content),
    # so verify_result() just hands back the content without asserting.
    content = FilesTool.read_file_content(dut, SyslogOtelConst.COLLECTOR_CONFIG_PATH).verify_result()
    return token in content


# =============================================================================
# Log-record parsing (field mapping, plan Section 6.2)
# =============================================================================


def _attr_scalar(value: Dict[str, Any]) -> Any:
    """Extract the scalar from an OTLP AnyValue ({"stringValue": ...}, {"intValue": ...}, ...)."""
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    return value


def _attrs_to_dict(attributes: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Flatten an OTLP attribute list ([{"key":..,"value":..}, ...]) to a plain dict."""
    out: Dict[str, Any] = {}
    for attr in attributes or []:
        key = attr.get("key")
        if key is not None:
            out[key] = _attr_scalar(attr.get("value", {}))
    return out


def log_records_from_docs(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten OTLP documents into a list of log-record dicts.

    Each entry: ``body`` (str), ``severityNumber``, ``severityText``,
    ``timeUnixNano`` / ``observedTimeUnixNano``, ``attributes`` (dict) and
    ``resource_attributes`` (dict).
    """
    records: List[Dict[str, Any]] = []
    for doc in docs:
        for resource in doc.get("resourceLogs", []) or []:
            resource_attrs = _attrs_to_dict(resource.get("resource", {}).get("attributes"))
            for scope in resource.get("scopeLogs", []) or []:
                for rec in scope.get("logRecords", []) or []:
                    body = rec.get("body", {})
                    records.append(
                        {
                            "body": _attr_scalar(body) if isinstance(body, dict) else body,
                            "severityNumber": rec.get("severityNumber"),
                            "severityText": rec.get("severityText"),
                            "timeUnixNano": rec.get("timeUnixNano"),
                            "observedTimeUnixNano": rec.get("observedTimeUnixNano"),
                            "attributes": _attrs_to_dict(rec.get("attributes")),
                            "resource_attributes": resource_attrs,
                        }
                    )
    return records


def fetch_log_records(collector: OtelCollector, *, label: str = "logs") -> List[Dict[str, Any]]:
    """Fetch the receiver artifact and return its parsed OTLP log records."""
    poll_timeout = SyslogOtelConst.DELIVERY_TIMEOUT_SEC + OtelCollectorConst.ARTIFACT_TIMEOUT_SEC
    local_json = collector.fetch_artifact(
        SyslogOtelConst.LOCAL_ARTIFACT_DIR,
        file_name="syslog-otel-{}.json".format(label),
        timeout_sec=poll_timeout,
    )
    return log_records_from_docs(_read_otel_collector_documents(local_json))


def find_record_by_body(records: List[Dict[str, Any]], marker: str) -> Optional[Dict[str, Any]]:
    """Return the first log record whose body contains ``marker`` (or ``None``)."""
    for rec in records:
        body = rec.get("body")
        if isinstance(body, str) and marker in body:
            return rec
    return None


def record_timestamp_sec(record: Dict[str, Any]) -> Optional[float]:
    """Return the record's timestamp (seconds) from ``timeUnixNano`` / ``observedTimeUnixNano``."""
    for key in ("timeUnixNano", "observedTimeUnixNano"):
        raw = record.get(key)
        if raw:
            try:
                return int(raw) / 1e9
            except (TypeError, ValueError):
                continue
    return None
