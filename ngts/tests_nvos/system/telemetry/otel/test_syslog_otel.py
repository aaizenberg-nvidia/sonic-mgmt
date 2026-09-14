"""NVOS *Syslog over OTEL* (Version 1) functional tests.

Foundation pass — implements the standalone V1 tests from
``test_plans/syslog_over_otel_test_plan.md``:

* Section 5.1 :func:`test_syslog_otel_configuration` — pure NVUE CLI show/set/unset/invalid.
* Section 6.1 :func:`test_syslog_otel_logs_export` — enable + marker delivery.
* Section 6.2 :func:`test_syslog_otel_field_mapping` — syslog fields -> OTLP log record.
* Section 6.3 :func:`test_syslog_otel_labels` — device-scoped label on exported records.
* Section 6.4 :func:`test_syslog_otel_secured_channel` — export over TLS-secured OTLP.
* Section 6.5 :func:`test_syslog_otel_additive_legacy` — local logging path unaffected.
* Section 6.5 :func:`test_syslog_otel_additive_legacy_remote` — legacy remote path unaffected.
* Section 6.6 :func:`test_syslog_otel_recovery` — forwarding recovers after receiver restart.
* Section 6.7a :func:`test_syslog_otel_config_artifacts` — rsyslog/collector config track state.
* Section 7.1 :func:`test_syslog_otel_enabled_but_inactive` — prerequisite progression.
* Section 8.1 :func:`test_syslog_otel_log_storm` — burst does not crash the pipeline.
* Section 8.2 :func:`test_syslog_otel_scale_destinations` — a log reaches every destination.
* Section 8.3 :func:`test_syslog_otel_toggle_stability` — repeated enable/disable stays healthy.
* Section 9.1 :func:`test_syslog_otel_sustained_rate` — sustained rate stays healthy.
* Section 10.4 :func:`test_syslog_otel_techsupport_bundle` — dump captures syslog artifacts.

* Section 6.7b :func:`test_syslog_otel_config_artifacts` — break the pipeline: marker
  non-delivery + core NOS daemons healthy + recovery (log-error-pattern leg activates once
  ``LOG_ERROR_PATTERNS`` is confirmed).

Explicit coverage rows that skip with a precise blocker (visible in reports, ready to
fill once the blocker clears):

* Section 6.4 mutual-TLS leg :func:`test_syslog_otel_secured_channel_mtls` — needs a DUT
  client certificate + collector client-CA the OTEL harness does not stage for the syslog
  suite today.
* Section 6.8 gNMI coexistence :func:`test_syslog_otel_telemetry_interop` — extends the
  OTEL suite's (cumulus-scoped) coexistence harness; needs an NVOS-runnable hook.
* Section 10.1/10.2/10.3 :func:`test_syslog_otel_reboot_persistence` /
  :func:`test_syslog_otel_factory_reset` / :func:`test_syslog_otel_upgrade` — destructive;
  no NVOS OTEL test to fold into yet + owner sign-off.

Still deferred (documented, no test row):

* Section 9.1 combined ~40k metrics/s + ~1k logs/s load and the mem/CPU ≤10% budget — reuse
  the OTEL suite's mem/CPU limit tests (gated on ``STRESS_BUDGET_REUSE_AVAILABLE``).
* Section 6.2 structured-data-present — needs an rsyslog network input (SD is empty over the
  local /dev/log socket even on a fixed build; feature owner confirmed).
* Section 10.4 disabled-state reflection — the dump retains the rsyslog fragment after disable.

The stress tests (8.1, 9.1) size their load from values in
:class:`~ngts.tests_nvos.system.telemetry.otel.syslog_constants.SyslogOtelConst`; the feature
owner confirmed V1 has no official burst/rate thresholds, so these are test characterization
values, not asserted product limits. The log-error-pattern leg stays disabled: V1 has no
self-telemetry counters and no confirmed error strings, so failure detection relies on marker
non-delivery + core-daemon health (plan Section 4.2 / Section 11).
"""

import time

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import ApiType, ConfState
from ngts.nvos_tools.infra.FilesTool import FilesTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.ResultObj import IssueType, ResultObj
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.telemetry.otel.helpers import (
    cleanup_switch_otlp_grpc,
    configure_switch_otlp_grpc_dual_destination,
    enable_telemetry_master_state,
)
from ngts.tests_nvos.system.telemetry.otel.syslog_constants import SyslogOtelConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import (
    get_system_telemetry_labels_applied,
    unset_system_telemetry_label,
)
from ngts.tests_nvos.system.telemetry.otel.syslog_helpers import (
    add_otlp_destination,
    assert_collector_service_not_crashed,
    assert_core_daemons_healthy,
    assert_label_on_record,
    assert_marker_absent,
    assert_marker_at_remote_server,
    assert_marker_delivered,
    assert_marker_in_local_syslog,
    assert_rsyslog_fragment_removed_on_disable,
    break_syslog_ingestion,
    collector_config_has_logs,
    configure_receiver_for_logs,
    configure_receiver_for_logs_secured,
    configure_remote_syslog_server,
    emit_syslog_burst,
    emit_syslog_counted_batch,
    emit_syslog_marker,
    emit_syslog_sustained,
    enable_remote_syslog_reception,
    fetch_log_records,
    find_log_error_patterns,
    find_record_by_body,
    is_syslog_otel_supported,
    logs_export_state,
    make_receiver_unreachable,
    record_timestamp_sec,
    remove_otlp_destination,
    restore_receiver,
    restore_remote_syslog_reception,
    restore_syslog_ingestion,
    running_config_has_logs_export,
    set_device_label,
    set_logs_export_state,
    set_logs_export_state_expect_reject,
    unset_logs_export,
    unset_remote_syslog_servers,
    wait_batch_settled,
    wait_forwarding_active,
    wait_forwarding_inactive,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.otel,
]


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def require_syslog_otel_feature(engines):
    """Skip the whole module when the build has no ``system telemetry logs`` CLI.

    Lets the suite run on any OTEL build (e.g. regression) and report a clear
    skip reason instead of failing when syslog-over-OTEL is not present yet.
    """
    TestToolkit.tested_api = ApiType.NVUE
    if not is_syslog_otel_supported(engines.dut):
        pytest.skip(
            "syslog-over-OTEL not present in this build "
            "(`nv set system telemetry logs …` is unsupported)"
        )


@pytest.fixture(scope="module")
def syslog_otel_setup(engines, devices, require_syslog_otel_feature, otel_suite_single):
    """Layer syslog-over-OTEL capture on the single-collector OTEL suite.

    ``require_syslog_otel_feature`` is listed before ``otel_suite_single`` so the
    cheap feature check (and skip) runs before the expensive collector bring-up.

    Uses ``otel_suite_single`` (primary collector only, no HA player) so the
    functional syslog tests run on setups without a second collector host. Adds a
    ``logs`` pipeline to the primary receiver (the stock receiver only captures
    metrics) and, on teardown, disables the syslog-over-OTEL knob. The blanket
    telemetry reset is handled by the ``otel_suite_single`` teardown.
    """
    TestToolkit.tested_api = ApiType.NVUE
    configure_receiver_for_logs(otel_suite_single.primary)
    yield otel_suite_single
    with allure.step("Disable syslog-over-OTEL export"):
        unset_logs_export(engines.dut)


@pytest.fixture
def syslog_otel_prereqs(engines, syslog_otel_setup):
    """Re-apply the switch-side OTLP prerequisites before each test (function scope)."""
    # The shared per-test clear_config teardown runs `nv config replace` to factory defaults,
    # unsetting the whole `system telemetry` subtree the module-scoped syslog_otel_setup
    # configured; without re-applying, every test after the first runs with no destination.
    # The collector on the player is untouched by the switch config-replace (stays module-scoped).
    suite = syslog_otel_setup
    with allure.step("Re-apply switch OTLP prerequisites (undo per-test config clear)"):
        configure_switch_otlp_grpc_dual_destination(
            engines.dut,
            primary_ip=suite.primary_ip,
            secondary_ip=suite.secondary_ip,
            is_nvos=suite.is_nvos,
        )
    return suite


@pytest.fixture(scope="module")
def syslog_otel_secured_setup(engines, devices, require_syslog_otel_feature, otel_suite_single_secured):
    """Layer syslog-over-OTEL capture on the single-collector TLS OTEL suite.

    The secured suite already configured server-authenticated TLS OTLP on the DUT and
    the collector; this only adds a ``logs`` pipeline to the TLS receiver (preserving
    the ``tls`` block) and disables the syslog-over-OTEL knob on teardown.
    """
    TestToolkit.tested_api = ApiType.NVUE
    configure_receiver_for_logs_secured(otel_suite_single_secured.primary)
    yield otel_suite_single_secured
    with allure.step("Disable syslog-over-OTEL export"):
        unset_logs_export(engines.dut)


@pytest.fixture(scope="module")
def require_two_collectors(engines):
    """Skip when there is no second collector host (no ``ha`` player) for the scale test."""
    if not hasattr(engines, "ha"):
        pytest.skip("needs a second collector host ('ha' player) for two OTLP destinations")


@pytest.fixture(scope="module")
def syslog_otel_dual_setup(
    engines, devices, require_syslog_otel_feature, require_two_collectors, otel_suite_dual
):
    """Layer syslog-over-OTEL capture on the two-collector OTEL suite.

    The cheap feature/two-collector guards are listed before ``otel_suite_dual`` so they
    skip before the expensive dual collector bring-up. Adds a ``logs`` pipeline to
    both receivers and disables the syslog-over-OTEL knob on teardown.
    """
    TestToolkit.tested_api = ApiType.NVUE
    configure_receiver_for_logs(otel_suite_dual.primary)
    configure_receiver_for_logs(otel_suite_dual.secondary)
    yield otel_suite_dual
    with allure.step("Disable syslog-over-OTEL export"):
        unset_logs_export(engines.dut)


@pytest.fixture
def syslog_otel_dual_prereqs(engines, syslog_otel_dual_setup):
    """Re-apply the two-collector OTLP prerequisites before each test (function scope)."""
    suite = syslog_otel_dual_setup
    with allure.step("Re-apply dual OTLP prerequisites (undo per-test config clear)"):
        configure_switch_otlp_grpc_dual_destination(
            engines.dut,
            primary_ip=suite.primary_ip,
            secondary_ip=suite.secondary_ip,
            is_nvos=suite.is_nvos,
        )
    return suite


# =============================================================================
# Section 5.1 — CLI configuration
# =============================================================================


@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_syslog_otel_configuration(engines, devices, require_syslog_otel_feature, test_api):
    """Section 5.1 — show/set/unset and invalid-enum rejection for `logs export state`.

    Pure CLI (no collector), so it runs under every API type (NVUE + OpenAPI).
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut

    try:
        with allure.step("Default: unset then verify state reads back disabled"):
            unset_logs_export(dut)
            state = logs_export_state(dut)
            ValidationTool.compare_values(
                state, SyslogOtelConst.LOGS_EXPORT_DEFAULT_STATE
            ).verify_result()

        with allure.step("Enable: set state enabled + apply, verify applied=enabled"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            state = logs_export_state(dut, rev=ConfState.APPLIED)
            ValidationTool.compare_values(state, SyslogOtelConst.ENABLED).verify_result()

        with allure.step("Explicit disable: set state disabled + apply; key present in config"):
            set_logs_export_state(dut, SyslogOtelConst.DISABLED)
            state = logs_export_state(dut, rev=ConfState.APPLIED)
            ValidationTool.compare_values(state, SyslogOtelConst.DISABLED).verify_result()
            ResultObj(
                running_config_has_logs_export(dut),
                "explicit `set ... state disabled` not present in `nv config show`",
                issue_type=IssueType.PossibleBug,
            ).verify_result()

        with allure.step("Unset: key absent from config, state defaults to disabled"):
            unset_logs_export(dut)
            state = logs_export_state(dut, rev=ConfState.APPLIED)
            ValidationTool.compare_values(
                state, SyslogOtelConst.LOGS_EXPORT_DEFAULT_STATE
            ).verify_result()
            ResultObj(
                not running_config_has_logs_export(dut),
                "`unset` left an explicit logs export key in `nv config show`",
                issue_type=IssueType.PossibleBug,
            ).verify_result()

        with allure.step("Invalid enum: set state {} is rejected".format(SyslogOtelConst.INVALID_STATE)):
            set_logs_export_state_expect_reject(dut, SyslogOtelConst.INVALID_STATE)
    finally:
        with allure.step("Restore default (unset logs export)"):
            unset_logs_export(dut)


# =============================================================================
# Section 6.1 — enable + marker delivery
# =============================================================================


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_logs_export(engines, devices, loganalyzer, syslog_otel_prereqs, test_api):
    """Section 6.1 — enabling the feature forwards a marker log to the OTLP receiver."""
    TestToolkit.tested_api = test_api
    dut = engines.dut
    collector = syslog_otel_prereqs.primary

    try:
        with allure.step("Enable syslog-over-OTEL export"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            wait_forwarding_active(collector, loganalyzer)

        with allure.step("Emit a marker and verify it reaches the receiver"):
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_delivered(collector, marker)
    finally:
        unset_logs_export(dut)


# =============================================================================
# Section 6.2 — field mapping
# =============================================================================


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_field_mapping(engines, devices, loganalyzer, syslog_otel_prereqs, dut_hostname, test_api):
    """Section 6.2 — a syslog message maps onto the expected OTLP log-record fields.

    Asserts by value that the captured record's body, severity (text + number),
    timestamp (within tolerance), hostname, app-name and facility match the emitted
    marker, then that a second marker carrying a PID surfaces as the ``proc_id``
    attribute. Structured data is covered by a deferred network-input leg (see the 6.2
    step comment).
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    collector = syslog_otel_prereqs.primary

    try:
        with allure.step("Enable syslog-over-OTEL export"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            wait_forwarding_active(collector, loganalyzer)

        with allure.step("Emit a marker with known program/facility/severity"):
            emit_time = time.time()
            marker = emit_syslog_marker(
                program=SyslogOtelConst.MARKER_PROGRAM,
                facility=SyslogOtelConst.MARKER_FACILITY,
                priority=SyslogOtelConst.MARKER_SEVERITY,
                loganalyzer=loganalyzer,
            )
            assert_marker_delivered(collector, marker)

        with allure.step("Capture the OTLP log record and verify field mapping"):
            records = fetch_log_records(collector, label="field-mapping")
            record = find_record_by_body(records, marker)
            ResultObj(
                record is not None,
                "no OTLP log record carrying marker {!r} was captured".format(marker),
                issue_type=IssueType.PossibleBug,
            ).verify_result()

            attrs = dict(record.get("attributes") or {})
            attrs.update(record.get("resource_attributes") or {})

            with allure.independent_step("body carries the emitted message"):
                ResultObj(
                    marker in (record.get("body") or ""),
                    "record body {!r} does not contain marker".format(record.get("body")),
                    issue_type=IssueType.PossibleBug,
                ).verify_result()

            with allure.independent_step("severity maps to the emitted level"):
                ValidationTool.compare_values(
                    record.get("severityText"), SyslogOtelConst.MARKER_SEVERITY
                ).verify_result()
                ValidationTool.compare_values(
                    record.get("severityNumber"), SyslogOtelConst.MARKER_SEVERITY_NUMBER
                ).verify_result()

            with allure.independent_step("timestamp within tolerance"):
                ts = record_timestamp_sec(record)
                ResultObj(
                    ts is not None, "record has no usable timestamp", issue_type=IssueType.PossibleBug
                ).verify_result()
                skew = abs(emit_time - ts)
                ResultObj(
                    skew <= SyslogOtelConst.TIMESTAMP_TOLERANCE_SEC,
                    "timestamp skew {:.1f}s exceeds tolerance {}s".format(
                        skew, SyslogOtelConst.TIMESTAMP_TOLERANCE_SEC
                    ),
                    issue_type=IssueType.PossibleBug,
                ).verify_result()

            with allure.independent_step("hostname attribute equals the DUT hostname"):
                ValidationTool.compare_values(
                    attrs.get(SyslogOtelConst.ATTR_KEY_HOSTNAME), dut_hostname
                ).verify_result()

            with allure.independent_step("app-name attribute equals the marker program"):
                ValidationTool.compare_values(
                    attrs.get(SyslogOtelConst.ATTR_KEY_APP_NAME), SyslogOtelConst.MARKER_PROGRAM
                ).verify_result()

            with allure.independent_step("facility attribute equals the marker facility"):
                ValidationTool.compare_values(
                    attrs.get(SyslogOtelConst.ATTR_KEY_FACILITY_TEXT), SyslogOtelConst.MARKER_FACILITY
                ).verify_result()

        with allure.step("Verify process-id mapping (marker carrying a PID)"):
            # Emit a marker with a PID (`logger --id=<pid>`): rsyslog derives PROCID from
            # the source line's `tag[pid]:` form and exports it as the bare `proc_id`
            # attribute (feature owner confirmed; NOT a bug -- a plain `logger` line has no
            # PID and correctly yields the `-` nil value). Structured data is deliberately
            # NOT asserted here: it was a real V1 bug (now fixed) but only survives an
            # rsyslog network input; over the local /dev/log socket rsyslog parses RFC 3164
            # and leaves SD empty, so an SD-present assertion is unreachable via local
            # injection and is covered by a deferred network-input leg (plan Section 6.2 /
            # docs/NVBUG_DRAFT_syslog_otel_procid_structured_data.md).
            proc_marker = emit_syslog_marker(
                program=SyslogOtelConst.MARKER_PROGRAM,
                facility=SyslogOtelConst.MARKER_FACILITY,
                priority=SyslogOtelConst.MARKER_SEVERITY,
                proc_id=SyslogOtelConst.MARKER_PROC_ID,
                loganalyzer=loganalyzer,
            )
            assert_marker_delivered(collector, proc_marker)

            proc_records = fetch_log_records(collector, label="field-mapping-procid")
            proc_record = find_record_by_body(proc_records, proc_marker)
            ResultObj(
                proc_record is not None,
                "no OTLP log record carrying process-id marker {!r} was captured".format(proc_marker),
                issue_type=IssueType.PossibleBug,
            ).verify_result()

            proc_attrs = dict(proc_record.get("attributes") or {})
            proc_attrs.update(proc_record.get("resource_attributes") or {})

            with allure.independent_step("process-id attribute equals the emitted PID"):
                proc_val = proc_attrs.get(SyslogOtelConst.ATTR_KEY_PROC_ID)
                ResultObj(
                    str(proc_val) == SyslogOtelConst.MARKER_PROC_ID,
                    "{} attribute {!r} != emitted PID {!r}; captured attribute keys = {}".format(
                        SyslogOtelConst.ATTR_KEY_PROC_ID, proc_val, SyslogOtelConst.MARKER_PROC_ID,
                        sorted(proc_attrs.keys()),
                    ),
                    issue_type=IssueType.PossibleBug,
                ).verify_result()
    finally:
        unset_logs_export(dut)


# =============================================================================
# Section 6.3 — device-scoped telemetry label on log records
# =============================================================================


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_labels(engines, devices, loganalyzer, syslog_otel_prereqs, test_api):
    """Section 6.3 — a device-scoped telemetry label is applied to exported log records.

    Configures a device label, enables the feature, emits a marker and asserts the
    label appears as a resource attribute on the captured log record (same as it does
    on metrics). The feature owner confirmed the key is the literal label id; the check
    accepts that key (or the description value as a fallback) and dumps the captured
    attribute keys on mismatch so a retest either passes or flags a real product bug.
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    collector = syslog_otel_prereqs.primary

    try:
        with allure.step("Configure a device-scoped telemetry label"):
            set_device_label(dut, SyslogOtelConst.LABEL_ID, SyslogOtelConst.LABEL_DESCRIPTION)
            applied = get_system_telemetry_labels_applied(dut)
            ResultObj(
                isinstance(applied, dict) and SyslogOtelConst.LABEL_ID in applied,
                "label {!r} not present in applied telemetry labels {}".format(
                    SyslogOtelConst.LABEL_ID, sorted(applied) if isinstance(applied, dict) else applied
                ),
                issue_type=IssueType.PossibleBug,
            ).verify_result()

        with allure.step("Enable syslog-over-OTEL export"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            wait_forwarding_active(collector, loganalyzer)

        with allure.step("Emit a marker and capture the exported log record"):
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_delivered(collector, marker)
            records = fetch_log_records(collector, label="labels")
            record = find_record_by_body(records, marker)
            ResultObj(
                record is not None,
                "no OTLP log record carrying marker {!r} was captured".format(marker),
                issue_type=IssueType.PossibleBug,
            ).verify_result()

        with allure.step("Verify the device label is a resource attribute on the record"):
            # The feature owner confirmed the label lands as a bare resource attribute keyed
            # by the literal label id. Build 004 exported empty resource attributes on logs;
            # this soft-asserts and dumps the captured keys so a retest on a build with the
            # fixes either passes or flags a real product bug (plan Section 6.3).
            with allure.independent_step("device label present as a resource attribute"):
                assert_label_on_record(record, SyslogOtelConst.LABEL_ID, SyslogOtelConst.LABEL_DESCRIPTION)
    finally:
        with allure.step("Restore default (remove label, disable feature)"):
            # Stage the label unset first; the unset_logs_export apply commits both.
            unset_system_telemetry_label(dut, SyslogOtelConst.LABEL_ID)
            unset_logs_export(dut)


# =============================================================================
# Section 6.5 — additive to legacy logging
# =============================================================================


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_additive_legacy(engines, devices, loganalyzer, syslog_otel_prereqs, test_api):
    """Section 6.5 — the existing local logging path keeps working; the receiver is additive.

    The local syslog file is the source of truth for "legacy paths unaffected"
    (marker present in all phases); the OTLP receiver only gets the marker while the
    feature is enabled. (The remote-syslog-server leg reuses the syslog suite's
    server fixture and is deferred to a later pass.)
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    collector = syslog_otel_prereqs.primary

    try:
        with allure.step("Baseline (feature disabled): local file yes, receiver no"):
            unset_logs_export(dut)
            wait_forwarding_inactive(collector, loganalyzer)
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_in_local_syslog(dut, marker)
            assert_marker_absent(collector, marker)

        with allure.step("Enabled: local file yes, receiver yes"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            wait_forwarding_active(collector, loganalyzer)
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_delivered(collector, marker)
            assert_marker_in_local_syslog(dut, marker)

        with allure.step("Disabled again: local file yes, receiver no"):
            set_logs_export_state(dut, SyslogOtelConst.DISABLED)
            wait_forwarding_inactive(collector, loganalyzer)
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_in_local_syslog(dut, marker)
            assert_marker_absent(collector, marker)

        with allure.step("Disable removed the rsyslog ingestion fragment"):
            assert_rsyslog_fragment_removed_on_disable(dut)
    finally:
        unset_logs_export(dut)


@pytest.fixture(scope="module")
def remote_syslog_server(engines):
    """Prepare the sonic-mgmt container as a legacy remote syslog server (UDP).

    The syslog suite's autouse ``enable_rsyslog_on_sonic_mgmt_container`` only applies
    under that suite, so the OTEL suite enables UDP reception here (un-comment imudp +
    restart rsyslogd) and restores the original config on teardown. Skips when there is
    no sonic-mgmt player.
    """
    server_engine = getattr(engines, "sonic_mgmt", None)
    if server_engine is None:
        pytest.skip("no 'sonic-mgmt' player to act as the legacy remote syslog server")
    backup = enable_remote_syslog_reception(server_engine)
    yield server_engine
    restore_remote_syslog_reception(server_engine, backup)


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_additive_legacy_remote(
    engines, devices, loganalyzer, syslog_otel_prereqs, remote_syslog_server, test_api
):
    """Section 6.5 (remote leg) — the legacy remote syslog path is unaffected by the feature.

    Configures a legacy remote syslog server (the collector host) and asserts a marker
    reaches it in every phase (disabled/enabled/disabled), while the OTLP receiver only
    gets it while enabled. Skips when no ``sonic-mgmt`` player is available to host the
    remote server.
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    collector = syslog_otel_prereqs.primary
    server_engine = remote_syslog_server
    server_ip = server_engine.ip

    try:
        with allure.step("Configure a legacy remote syslog server on the DUT"):
            configure_remote_syslog_server(dut, server_ip, is_eth=devices.dut.is_eth())

        with allure.step("Baseline (feature disabled): remote yes, receiver no"):
            unset_logs_export(dut)
            wait_forwarding_inactive(collector, loganalyzer)
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_at_remote_server(server_engine, marker)
            assert_marker_absent(collector, marker)

        with allure.step("Enabled: remote yes, receiver yes"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            wait_forwarding_active(collector, loganalyzer)
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_delivered(collector, marker)
            assert_marker_at_remote_server(server_engine, marker)

        with allure.step("Disabled again: remote yes, receiver no"):
            set_logs_export_state(dut, SyslogOtelConst.DISABLED)
            wait_forwarding_inactive(collector, loganalyzer)
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_at_remote_server(server_engine, marker)
            assert_marker_absent(collector, marker)
    finally:
        with allure.step("Restore default (remove remote server, disable feature)"):
            unset_remote_syslog_servers(dut)
            unset_logs_export(dut)


# =============================================================================
# Section 6.6 — recovery after receiver restart
# =============================================================================


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_recovery(engines, devices, loganalyzer, syslog_otel_prereqs, test_api):
    """Section 6.6 — forwarding recovers after the OTLP receiver restarts.

    Enables the feature and confirms delivery, restarts the collector process (drops
    and re-establishes the OTLP session), then confirms a fresh marker is delivered
    again — the pipeline recovers on its own without reconfiguring the DUT.
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    collector = syslog_otel_prereqs.primary

    try:
        with allure.step("Enable and confirm baseline delivery"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            wait_forwarding_active(collector, loganalyzer)
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_delivered(collector, marker)

        with allure.step("Restart the OTLP receiver (collector process)"):
            collector.stop()
            collector.ensure_running(install_if_missing=False)

        with allure.step("Confirm forwarding recovers and a fresh marker is delivered"):
            wait_forwarding_active(collector, loganalyzer)
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_delivered(collector, marker)
    finally:
        unset_logs_export(dut)


# =============================================================================
# Section 6.7a — config-artifact correctness (grey-box)
# =============================================================================


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_config_artifacts(engines, devices, loganalyzer, syslog_otel_prereqs, test_api):
    """Section 6.7 — config correctness (a) and failure detection & isolation (b).

    Grey-box. Part (a): on enable the rsyslog fragment is staged and the collector
    config gains a logs pipeline (marker forwarded); on disable both are removed (no
    marker). Part (b): with the feature enabled, break the staged ingestion path (remove
    the rsyslog fragment) and assert a marker no longer reaches the receiver while core
    NOS daemons keep running (non-critical feature must not degrade the switch); restore
    and assert recovery. The on-switch log-error-pattern leg runs only once
    ``SyslogOtelConst.LOG_ERROR_PATTERNS`` is populated (TBD — plan Section 11 open issue 2).
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    collector = syslog_otel_prereqs.primary
    fragment = SyslogOtelConst.RSYSLOG_FRAGMENT_PATH

    try:
        with allure.step("Baseline (disabled): artifacts absent, no forwarding"):
            unset_logs_export(dut)
            wait_forwarding_inactive(collector, loganalyzer)
            with allure.independent_step("rsyslog fragment absent"):
                FilesTool.file_exists_sudo(dut, fragment).verify_result(should_succeed=False)
            with allure.independent_step("collector config is metrics-only"):
                ResultObj(
                    not collector_config_has_logs(dut),
                    "collector config carries a logs pipeline while disabled",
                    issue_type=IssueType.PossibleBug,
                ).verify_result()
            with allure.independent_step("marker not forwarded"):
                marker = emit_syslog_marker(loganalyzer=loganalyzer)
                assert_marker_absent(collector, marker)

        with allure.step("Enable: artifacts staged, forwarding active"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            wait_forwarding_active(collector, loganalyzer)
            with allure.independent_step("rsyslog fragment staged"):
                FilesTool.file_exists_sudo(dut, fragment).verify_result(should_succeed=True)
            with allure.independent_step("collector config carries a logs pipeline"):
                ResultObj(
                    collector_config_has_logs(dut),
                    "collector config has no logs pipeline while enabled (token {!r})".format(
                        SyslogOtelConst.COLLECTOR_LOGS_CONFIG_TOKEN
                    ),
                    issue_type=IssueType.PossibleBug,
                ).verify_result()
            with allure.independent_step("marker forwarded"):
                marker = emit_syslog_marker(loganalyzer=loganalyzer)
                assert_marker_delivered(collector, marker)

        with allure.step("Disable: artifacts removed, no forwarding"):
            set_logs_export_state(dut, SyslogOtelConst.DISABLED)
            wait_forwarding_inactive(collector, loganalyzer)
            with allure.independent_step("rsyslog fragment removed"):
                FilesTool.file_exists_sudo(dut, fragment).verify_result(should_succeed=False)
            with allure.independent_step("collector config back to metrics-only"):
                ResultObj(
                    not collector_config_has_logs(dut),
                    "collector config still carries a logs pipeline after disable",
                    issue_type=IssueType.PossibleBug,
                ).verify_result()
            with allure.independent_step("marker not forwarded"):
                marker = emit_syslog_marker(loganalyzer=loganalyzer)
                assert_marker_absent(collector, marker)

        with allure.step("Re-enable and break the pipeline: non-delivery, core daemons healthy (6.7b)"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            wait_forwarding_active(collector, loganalyzer)
            assert_marker_delivered(collector, emit_syslog_marker(loganalyzer=loganalyzer))

            break_syslog_ingestion(dut)
            try:
                with allure.independent_step("marker does not reach the receiver while broken"):
                    marker = emit_syslog_marker(loganalyzer=loganalyzer)
                    assert_marker_absent(collector, marker)
                with allure.independent_step("core NOS daemons unaffected by the broken feature"):
                    assert_core_daemons_healthy(dut)
                matched = find_log_error_patterns(dut)
                if SyslogOtelConst.LOG_ERROR_PATTERNS:
                    with allure.independent_step("on-switch log shows a broken-pipeline error pattern"):
                        ResultObj(
                            bool(matched),
                            "no configured log-error pattern {} matched in {} while the pipeline "
                            "was broken".format(
                                SyslogOtelConst.LOG_ERROR_PATTERNS, SyslogOtelConst.LOG_ERROR_SOURCE
                            ),
                            issue_type=IssueType.PossibleBug,
                        ).verify_result()
            finally:
                restore_syslog_ingestion(dut)

        with allure.step("Restore the pipeline: forwarding recovers (no false negative)"):
            wait_forwarding_active(collector, loganalyzer)
            with allure.independent_step("marker reaches the receiver again after restore"):
                assert_marker_delivered(collector, emit_syslog_marker(loganalyzer=loganalyzer))
    finally:
        unset_logs_export(dut)


# =============================================================================
# Section 6.8 — gNMI metrics/state telemetry unaffected (coexistence)
# =============================================================================


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_telemetry_interop(engines, devices, loganalyzer, syslog_otel_prereqs, test_api):
    """Section 6.8 — toggling syslog-over-OTEL does not disturb gNMI metrics/state telemetry.

    Intended flow (plan Section 6.8): hold a gNMI metrics/state subscription open, toggle
    the syslog feature on then off, and assert the stream stays uninterrupted (no
    gap/reset, same paths/schema, consistent values before/after).

    Blocked: the plan implements this by extending the OTEL suite's gNMI+OTEL
    coexistence harness (``cumulus/gnmi_coexistence.py`` / ``test_telemetry_coexistence.py``),
    which is cumulus-scoped today. Skips until an NVOS-runnable coexistence hook exists to
    fold the syslog toggle + marker-delivery assertion into (OTEL suite owner).
    """
    pytest.skip(
        "6.8 gNMI coexistence extends the OTEL suite's gNMI+OTEL coexistence harness "
        "(currently cumulus-scoped); needs an NVOS-runnable coexistence hook (OTEL suite owner)."
    )


# =============================================================================
# Section 7.1 — enabled-but-inactive / prerequisite progression
# =============================================================================


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_enabled_but_inactive(engines, devices, loganalyzer, syslog_otel_prereqs, test_api):
    """Section 7.1 — enabling without prerequisites is accepted-but-inactive; activates when present.

    Resets the DUT telemetry to build prerequisites up from scratch, then restores
    the suite's OTLP baseline on teardown (so it is order-independent).
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    suite = syslog_otel_prereqs
    collector = suite.primary
    collector_ip = suite.primary_ip

    try:
        with allure.step("Reset DUT telemetry (remove all prerequisites)"):
            cleanup_switch_otlp_grpc(dut)

        with allure.step("Feature only (no OTLP export): accepted but inactive"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_absent(collector, marker)

        with allure.step("+ OTLP export enabled, still no destination: inactive"):
            enable_telemetry_master_state(
                System(), dut, is_nvos=suite.is_nvos, apply=True, ask_for_confirmation=True
            )
            # General robustness guard (feature owner saw no crash; hardening came from code
            # analysis): syslog + OTLP export enabled with zero destinations must not crash
            # the collector -- it stays "inactive" until a destination exists.
            assert_collector_service_not_crashed(dut, suite.telemetry_unit)
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_absent(collector, marker)

        with allure.step("+ destination: forwarding starts automatically"):
            add_otlp_destination(dut, collector_ip)
            wait_forwarding_active(collector, loganalyzer)
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_delivered(collector, marker)

        with allure.step("Reverse: remove the last destination -> forwarding stops gracefully"):
            remove_otlp_destination(dut, collector_ip)
            wait_forwarding_inactive(collector, loganalyzer)
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_absent(collector, marker)
    finally:
        with allure.step("Restore suite OTLP baseline and disable feature"):
            unset_logs_export(dut)
            cleanup_switch_otlp_grpc(dut)
            configure_switch_otlp_grpc_dual_destination(
                dut,
                primary_ip=suite.primary_ip,
                secondary_ip=suite.secondary_ip,
                is_nvos=suite.is_nvos,
            )


# =============================================================================
# Section 8.1 — log storm (burst) does not crash the pipeline
# =============================================================================


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_log_storm(engines, devices, loganalyzer, syslog_otel_prereqs, test_api):
    """Section 8.1 — a log storm stays bounded and stable; drops are bounded/measurable.

    Legs (plan Section 8.1):

    * Receiver reachable — burst does not crash the collector; sentinel still lands.
    * Receiver reachable — a counted batch delivers (nearly) all markers (baseline
      drop delta = emitted - received).
    * Receiver unreachable — a burst under outage keeps the collector and core NOS
      daemons healthy (queues bounded, controlled drops, no unbounded growth / crash).
    * Receiver restored — export recovers and later markers arrive; core daemons healthy.

    Burst/batch sizes are test characterization values (see SyslogOtelConst); the feature
    owner confirmed V1 has no official thresholds, so they size the load but are not asserted
    as product limits. The memory/CPU ≤10% budget is verified by the OTEL suite's mem/CPU
    limit tests (reused), gated on ``STRESS_BUDGET_REUSE_AVAILABLE`` until that hook is wired
    (plan open item / Anna).
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    suite = syslog_otel_prereqs
    collector = suite.primary

    try:
        with allure.step("Enable syslog-over-OTEL export"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            wait_forwarding_active(collector, loganalyzer)
            assert_marker_delivered(collector, emit_syslog_marker(loganalyzer=loganalyzer))

        with allure.step("Receiver reachable: burst does not crash the pipeline"):
            sentinel = emit_syslog_burst(SyslogOtelConst.LOG_STORM_BURST_COUNT, loganalyzer=loganalyzer)
            with allure.independent_step("collector survives the burst"):
                assert_collector_service_not_crashed(dut, suite.telemetry_unit)
            with allure.independent_step("sentinel marker delivered after the burst"):
                assert_marker_delivered(collector, sentinel)

        with allure.step("Receiver reachable: baseline counted batch (bounded drops)"):
            batch = emit_syslog_counted_batch(SyslogOtelConst.DROP_MEASURE_COUNT, loganalyzer=loganalyzer)
            delivered = wait_batch_settled(collector, batch, SyslogOtelConst.DROP_MEASURE_COUNT)
            drops = SyslogOtelConst.DROP_MEASURE_COUNT - delivered
            with allure.independent_step("baseline drops are bounded ({}/{} delivered)".format(
                    delivered, SyslogOtelConst.DROP_MEASURE_COUNT)):
                ResultObj(
                    drops <= 0,
                    "baseline (receiver reachable) dropped {} of {} markers (delivered {})".format(
                        drops, SyslogOtelConst.DROP_MEASURE_COUNT, delivered
                    ),
                    issue_type=IssueType.PossibleBug,
                ).verify_result()

        with allure.step("Receiver unreachable: queues stay bounded, nothing crashes"):
            make_receiver_unreachable(collector)
            try:
                emit_syslog_counted_batch(SyslogOtelConst.DROP_MEASURE_COUNT, loganalyzer=loganalyzer)
                with allure.independent_step("collector survives the receiver outage"):
                    assert_collector_service_not_crashed(dut, suite.telemetry_unit)
                with allure.independent_step("core NOS daemons healthy during the outage"):
                    assert_core_daemons_healthy(dut)
            finally:
                restore_receiver(collector)

        with allure.step("Receiver restored: export recovers, core daemons healthy"):
            wait_forwarding_active(collector, loganalyzer)
            recovered = emit_syslog_counted_batch(SyslogOtelConst.DROP_MEASURE_COUNT, loganalyzer=loganalyzer)
            delivered = wait_batch_settled(collector, recovered, SyslogOtelConst.DROP_MEASURE_COUNT)
            with allure.independent_step("export recovers after the outage ({} delivered)".format(delivered)):
                ResultObj(
                    delivered > 0,
                    "no markers delivered after the receiver was restored (export did not recover)",
                    issue_type=IssueType.PossibleBug,
                ).verify_result()
            with allure.independent_step("core NOS daemons healthy after the storm"):
                assert_core_daemons_healthy(dut)
    finally:
        unset_logs_export(dut)


# =============================================================================
# Section 8.2 — one logs pipeline per OTLP destination (scale)
# =============================================================================


@pytest.mark.multiple_otlp_collectors
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_scale_destinations(engines, devices, loganalyzer, syslog_otel_dual_prereqs, test_api):
    """Section 8.2 — with multiple OTLP destinations, a log reaches every destination.

    Runs on the two-collector suite (primary + secondary gRPC destinations); enabling
    the feature must forward each marker to both receivers (one logs-export pipeline
    per destination). N is fixed at 2 here (plan Section 8.2: per-platform N is TBD).
    Skips at runtime when the setup has no second collector host (no ``ha`` player).
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    suite = syslog_otel_dual_prereqs
    collectors = {"primary": suite.primary, "secondary": suite.secondary}

    try:
        with allure.step("Enable syslog-over-OTEL export"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            wait_forwarding_active(suite.primary, loganalyzer)

        with allure.step("Emit a marker and verify it reaches every destination"):
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            for name, collector in collectors.items():
                with allure.independent_step("marker reaches {} destination".format(name)):
                    assert_marker_delivered(collector, marker)
    finally:
        unset_logs_export(dut)


# =============================================================================
# Section 8.3 — repeated enable/disable stays healthy (toggle stability)
# =============================================================================


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_toggle_stability(engines, devices, loganalyzer, syslog_otel_prereqs, test_api):
    """Section 8.3 — repeated enable/disable cycles keep the pipeline healthy.

    Each cycle enables the feature (marker must be delivered, collector must not
    crash), then disables it (marker must not be forwarded, rsyslog must not storm
    the removed ingestion socket). Per-cycle checks are soft so every cycle runs.
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    suite = syslog_otel_prereqs
    collector = suite.primary

    try:
        for cycle in range(1, SyslogOtelConst.TOGGLE_STABILITY_CYCLES + 1):
            with allure.step("Cycle {}/{}: enable -> deliver -> disable".format(
                    cycle, SyslogOtelConst.TOGGLE_STABILITY_CYCLES)):
                set_logs_export_state(dut, SyslogOtelConst.ENABLED)
                wait_forwarding_active(collector, loganalyzer)
                with allure.independent_step("collector healthy after enable"):
                    assert_collector_service_not_crashed(dut, suite.telemetry_unit)
                with allure.independent_step("marker delivered while enabled"):
                    marker = emit_syslog_marker(loganalyzer=loganalyzer)
                    assert_marker_delivered(collector, marker)

                set_logs_export_state(dut, SyslogOtelConst.DISABLED)
                wait_forwarding_inactive(collector, loganalyzer)
                with allure.independent_step("marker not forwarded while disabled"):
                    marker = emit_syslog_marker(loganalyzer=loganalyzer)
                    assert_marker_absent(collector, marker)
                with allure.independent_step("rsyslog ingestion fragment removed after disable"):
                    assert_rsyslog_fragment_removed_on_disable(dut)
    finally:
        unset_logs_export(dut)


# =============================================================================
# Section 9.1 — sustained rate stays healthy
# =============================================================================


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_sustained_rate(engines, devices, loganalyzer, syslog_otel_prereqs, test_api):
    """Section 9.1 — sustained throughput delivers with no drops; pipeline stays healthy.

    Legs (plan Section 9.1):

    * Sustained rate, receiver reachable — the collector survives and the sentinel lands.
    * No-drops measurement — a counted batch at the sustained rate delivers every marker
      (received count equals emitted count) and core NOS daemons stay healthy.
    * Combined metrics+logs load (~40k metrics/s + ~1k logs/s) with the ≤10% mem/CPU
      budget — gated on ``STRESS_BUDGET_REUSE_AVAILABLE`` until the OTEL suite's
      mem/CPU-budget reuse hook is wired for the syslog suite (plan open item / Anna).

    Rate/duration are test characterization values; the feature owner confirmed V1 has no
    committed rate target, so they are not asserted as a product limit.
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    suite = syslog_otel_prereqs
    collector = suite.primary
    rate = SyslogOtelConst.SUSTAINED_RATE_MSGS_PER_SEC
    duration = SyslogOtelConst.SUSTAINED_RATE_DURATION_SEC

    try:
        with allure.step("Enable syslog-over-OTEL export"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            wait_forwarding_active(collector, loganalyzer)

        with allure.step("Sustained load does not crash the pipeline"):
            sentinel = emit_syslog_sustained(rate, duration, loganalyzer=loganalyzer)
            with allure.independent_step("collector survives the sustained rate"):
                assert_collector_service_not_crashed(dut, suite.telemetry_unit)
            with allure.independent_step("sentinel marker delivered after sustained load"):
                assert_marker_delivered(collector, sentinel)

        with allure.step("No-drops at the sustained reference rate (received == emitted)"):
            expected = max(1, rate * duration)
            batch = emit_syslog_counted_batch(expected, interval_sec=1.0 / rate if rate else 0.0,
                                              loganalyzer=loganalyzer)
            delivered = wait_batch_settled(collector, batch, expected)
            drops = expected - delivered
            with allure.independent_step("no drops at the sustained rate ({}/{} delivered)".format(
                    delivered, expected)):
                ResultObj(
                    drops <= 0,
                    "sustained-rate run dropped {} of {} markers (delivered {})".format(
                        drops, expected, delivered
                    ),
                    issue_type=IssueType.PossibleBug,
                ).verify_result()
            with allure.independent_step("core NOS daemons healthy under sustained load"):
                assert_core_daemons_healthy(dut)

        if not SyslogOtelConst.STRESS_BUDGET_REUSE_AVAILABLE:
            with allure.step(
                "Combined metrics+logs load + mem/CPU <=10% budget: deferred (reuses the OTEL "
                "suite's mem/CPU limit tests; reuse hook not yet wired — plan Section 9.1, "
                "OTEL suite owner)."
            ):
                pass
    finally:
        unset_logs_export(dut)


# =============================================================================
# Section 10.1 / 10.2 / 10.3 — reboot / factory-reset / upgrade persistence
# =============================================================================
# Destructive DUT operations the plan implements by extending the OTEL suite's existing
# reboot / factory-reset / upgrade tests. No NVOS OTEL test exists to fold into yet and
# these need owner sign-off before running blind, so they are captured as explicit
# coverage rows that skip with the blocker (OTEL suite owner + sign-off).


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_reboot_persistence(engines, devices, syslog_otel_prereqs, test_api):
    """Section 10.1 — the enabled feature survives a cold reboot and export auto-recovers.

    Intended flow: enable + save, cold reboot, then assert ``state`` is still ``enabled``
    and a marker reaches the receiver again (a brief bring-up gap is acceptable).
    """
    pytest.skip(
        "10.1 reboot persistence extends the OTEL suite's reboot test (destructive); "
        "no NVOS OTEL reboot test to fold into yet + needs owner sign-off (plan Section 10.1)."
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_factory_reset(engines, devices, syslog_otel_prereqs, test_api):
    """Section 10.2 — factory reset returns the feature to default (disabled).

    Intended flow: with the feature enabled, factory-reset, then assert ``state`` is back
    to ``disabled``, log forwarding is removed, and the collector is metrics-only.
    """
    pytest.skip(
        "10.2 factory reset extends the OTEL suite's factory-reset test (destructive); "
        "no NVOS OTEL factory-reset test to fold into yet + needs owner sign-off (plan Section 10.2)."
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_upgrade(engines, devices, syslog_otel_prereqs, test_api):
    """Section 10.3 — feature behavior across an image upgrade.

    Intended flow: upgrade to the feature image; the feature defaults to ``disabled`` (no
    migration); with it enabled in saved startup config, apply validates on first boot
    post-upgrade and a marker reaches the receiver.
    """
    pytest.skip(
        "10.3 upgrade extends the OTEL suite's upgrade test (destructive, needs a target "
        "image); no NVOS OTEL upgrade test to fold into yet + needs owner sign-off (plan Section 10.3)."
    )


# =============================================================================
# Section 10.4 — tech-support bundle captures syslog artifacts
# =============================================================================


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_techsupport_bundle(engines, devices, loganalyzer, syslog_otel_prereqs, request, test_api):
    """Section 10.4 — a tech-support dump captures the syslog-over-OTEL artifacts.

    Enables the feature, generates a dump and asserts it carries the deterministic
    syslog-over-OTEL files. Disabled-state reflection is deferred: the bundle still
    contains the rsyslog fragment after disable (it survives even though the on-disk
    fragment is removed, see Section 6.7a), so that half needs the feature owner.
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    collector = syslog_otel_prereqs.primary
    system = System()
    tech_support_file = None

    try:
        with allure.step("Enable syslog-over-OTEL export"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            wait_forwarding_active(collector, loganalyzer)

        with allure.step("Generate a tech-support dump and check for syslog artifacts"):
            tech_support_file, _ = system.techsupport.action_generate(test_name=request.node.name)
            system.techsupport.extract_techsupport_files(dut)
            tech_support_dir = tech_support_file.replace(".tar.gz", "")
            for name in SyslogOtelConst.TECHSUPPORT_SYSLOG_ARTIFACTS:
                with allure.independent_step("bundle contains {}".format(name)):
                    ResultObj(
                        system.techsupport.bundle_contains_file(dut, name, tech_support_dir),
                        "tech-support bundle missing {} while enabled".format(name),
                        issue_type=IssueType.PossibleBug,
                    ).verify_result()
    finally:
        with allure.step("Clean up tech-support dump and disable feature"):
            # rm the tarball + its extracted dir directly; the NVUE delete action rejects
            # a raw dump filename on this build, and its error would register a failed
            # ResultObj that fails the test at teardown.
            if tech_support_file:
                dut.run_cmd("sudo rm -rf {tar} {extracted}".format(
                    tar=tech_support_file, extracted=tech_support_file.replace(".tar.gz", "")
                ))
            unset_logs_export(dut)


# =============================================================================
# Section 6.4 — secured (TLS) OTLP channel
# =============================================================================
# Runs last in the module: its module-scoped TLS suite rewrites the shared
# collector for TLS and enables a metric family, so keeping it after the insecure
# delivery tests prevents that state from bleeding into them.


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_secured_channel(engines, devices, loganalyzer, syslog_otel_secured_setup, test_api):
    """Section 6.4 — logs are exported over a TLS-secured OTLP channel.

    Runs on the TLS suite (DUT exports OTLP over server-authenticated TLS). Enables
    syslog-over-OTEL and asserts a marker is delivered to the collector across the
    encrypted channel. The mutual-TLS (client-cert) leg is deferred — it needs a DUT
    client certificate plus a collector client-CA, which the OTEL harness does not
    stage today (see plan open issues).
    """
    TestToolkit.tested_api = test_api
    dut = engines.dut
    collector = syslog_otel_secured_setup.primary

    try:
        with allure.step("Enable syslog-over-OTEL export over the TLS channel"):
            set_logs_export_state(dut, SyslogOtelConst.ENABLED)
            wait_forwarding_active(collector, loganalyzer)

        with allure.step("Emit a marker and verify TLS delivery to the receiver"):
            marker = emit_syslog_marker(loganalyzer=loganalyzer)
            assert_marker_delivered(collector, marker)
    finally:
        unset_logs_export(dut)


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_syslog_otel_secured_channel_mtls(engines, devices, loganalyzer, test_api):
    """Section 6.4 (mTLS leg) — logs are exported over a mutual-TLS OTLP channel.

    Intended flow (plan Section 6.4 step 4): with a client-certificate (mTLS) destination
    configured, enable the feature and assert a marker arrives over the mTLS channel.

    Blocked: needs a DUT client certificate + a collector client-CA staged (reuse the OTEL
    metrics cert flow, e.g. ``cumulus/certs_gen_scripts/entity_client_cert_generate.sh``);
    the OTEL harness does not stage client-cert material for the syslog suite today. Skips
    until that staging exists (OTEL suite owner).
    """
    pytest.skip(
        "6.4 mTLS leg needs a DUT client certificate + collector client-CA the OTEL harness "
        "does not stage for the syslog suite yet (reuse the metrics cert flow) (OTEL suite owner)."
    )
