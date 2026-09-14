"""Constants for the NVOS *Syslog over OTEL* (V1) test suite.

Generic telemetry field names / enums live in
:class:`ngts.nvos_constants.constants_nvos.TelemetryConsts`; OTel-collector test
choices (binary, ports, file-exporter paths) live in
:mod:`ngts.tests_nvos.system.telemetry.otel.constants.OtelCollectorConst`. This
module only holds the syslog-over-OTEL-specific choices: the marker convention,
on-box artifact paths, and every value that is still **TBD** (kept here as a single
edit point per the test plan's "TBD-as-constant" rule).

See ``test_plans/syslog_over_otel_test_plan.md`` Section 4.2 / Section 11 for the open issues that
back the TBD values below.
"""

from ngts.nvos_constants.constants_nvos import TelemetryConsts


class SyslogOtelConst:
    """Syslog-over-OTEL V1 test constants."""

    # --- CLI surface (reuse TelemetryConsts for the shared field names/enums) ---
    # `nv set system telemetry logs export state {enabled|disabled}`
    STATE = TelemetryConsts.STATE
    ENABLED = TelemetryConsts.State.ENABLED.value
    DISABLED = TelemetryConsts.State.DISABLED.value
    LOGS_EXPORT_DEFAULT_STATE = TelemetryConsts.Defaults.LOGS_EXPORT_STATE

    # An intentionally invalid `state` enum, used to prove the knob rejects bad input
    # at apply time (plan Section 5.1 step 6).
    INVALID_STATE = "on"

    # --- Marker convention (functional verification, plan Section 4.2) --------------
    # Each test emits a unique marker line via the syslog suite's `send_msg_to_server`
    # (which runs `logger` on the DUT); delivery is checked by grepping the receiver's
    # file-exporter artifact for this literal. The prefix keeps markers easy to grep
    # and lets loganalyzer ignore them. Body stays alphanumeric/underscore so it is
    # shell-safe.
    MARKER_PREFIX = "SYSLOG_OTEL"
    # Program/tag + facility/severity used when emitting a marker. NOTICE keeps the
    # line below loganalyzer's error threshold; facility/severity are also asserted in
    # the field-mapping test (Section 6.2).
    MARKER_PROGRAM = "syslog_otel_test"
    MARKER_FACILITY = "local0"
    MARKER_SEVERITY = "notice"

    # --- On-box paths (grey-box artifacts; confirmed with the feature owner) ---
    # rsyslog fragment staged when the feature is enabled.
    RSYSLOG_FRAGMENT_PATH = "/etc/rsyslog.d/47-otel-syslog.conf"
    # Effective (regenerated) OTEL collector config on the DUT.
    COLLECTOR_CONFIG_PATH = "/etc/nv-telemetry/nv-telemetry-config.yaml"

    # --- Timeouts / tolerances ------------------------------------------------------
    # Wait window for a probe marker: seen => pipeline active, elapsed => inactive.
    # The forwarding-ready gate absorbs the post-reconfigure latency first, so probes
    # run against a warm pipeline (~2-3s steady-state delivery); 15s is ample headroom.
    # The delivered path short-circuits on arrival (~3s), so this value mainly bounds the
    # absence path (assert_marker_absent) which cannot short-circuit -- keeping it tight is
    # a suite-wide runtime saver. This is a TEST tolerance only: the feature owner confirmed
    # there is NO committed product delivery-latency SLA in V1 (the sole intentional delay is
    # the collector's 10ms logs batch-processor timeout), so delivery time is never asserted.
    DELIVERY_TIMEOUT_SEC = 15
    # Poll cadence while waiting for a marker in the receiver artifact.
    MARKER_POLL_INTERVAL_SEC = 3
    # Wait window for a marker to reach a legacy remote syslog server (plan Section 6.5).
    # Same headroom as the OTLP path today; separate name so it can be tuned independently.
    REMOTE_SYSLOG_DELIVERY_TIMEOUT_SEC = DELIVERY_TIMEOUT_SEC
    # Backup of the remote server's rsyslog.conf while UDP reception is enabled for Section 6.5.
    REMOTE_RSYSLOG_CONF_BACKUP = "/etc/rsyslog.conf.orig"
    # Allowed skew between the emitted wall-clock time and the OTLP record timestamp.
    # Absorbs NTP skew between the test host and the DUT while still catching mapping
    # errors (wrong unit, timezone offset, or a static/ingestion time).
    TIMESTAMP_TOLERANCE_SEC = 60

    # --- Field mapping (plan Section 6.2) -------------------------------------------
    # OTLP log-record attribute keys emitted by the on-box syslog receiver, asserted by
    # value in the field-mapping test. The feature owner confirmed the build uses the
    # collector syslog receiver's BARE stanza-parser keys (NOT the OTel semantic-convention
    # `syslog.*` names, which the spec appendix lists only as normative mapping guidance):
    #   PROCID -> proc_id, STRUCTURED-DATA -> structured_data (nested map), message-id -> msg_id,
    #   APP-NAME -> appname, HOSTNAME -> hostname, facility -> facility.
    ATTR_KEY_HOSTNAME = "hostname"
    ATTR_KEY_APP_NAME = "appname"
    ATTR_KEY_FACILITY_TEXT = "facility_text"
    # OTLP severityNumber the receiver maps a `notice` line to.
    MARKER_SEVERITY_NUMBER = 10

    # --- process-id + structured data (plan Section 6.2) ---------------------------
    # process-id: NOT a bug. rsyslog derives PROCID from the source line's `tag[pid]:`
    # form, so the marker must carry a PID (emit `logger --id=<pid> -t <tag>`); a plain
    # `logger` line has no PID and correctly yields the `-` nil value. With a PID present the
    # exported record carries `proc_id` = that PID, which the test asserts by value.
    #
    # structured data: WAS a real V1 bug (the rsyslog forwarding template hardcoded the
    # message-id + STRUCTURED-DATA slots to the `-` nil value) -- now FIXED by the feature owner.
    # Caveat that shapes the test: even with the fix, rsyslog parses messages arriving on the
    # local /dev/log socket as RFC 3164 and leaves structured-data empty; SD only survives
    # where rsyslog parses a genuine RFC 5424 frame, i.e. a network input. So SD-present is
    # NOT observable through our local `logger` injection -- the local field-mapping test
    # asserts proc_id and treats the SD-present case as DEFERRED (needs an rsyslog network
    # input on localhost); see docs/NVBUG_DRAFT_syslog_otel_procid_structured_data.md.
    MARKER_PROC_ID = "4529"
    # Bare OTLP attribute keys (feature-owner-confirmed), not the spec's `syslog.*` names.
    ATTR_KEY_PROC_ID = "proc_id"
    ATTR_KEY_STRUCTURED_DATA = "structured_data"  # nested map on a fixed build (network input)
    ATTR_KEY_MSG_ID = "msg_id"
    # Structured-data content (only observable over a network RFC 5424 input; deferred leg).
    STRUCTURED_DATA_ID = "session@1234"
    STRUCTURED_DATA_PARAM_KEY = "user"
    STRUCTURED_DATA_PARAM_VALUE = "alice"

    # --- Device-scoped telemetry label (plan Section 6.3) --------------------------
    # A device label set via `nv set system telemetry label <id> description <value>`
    # appears as a resource attribute on exported log records. The feature owner confirmed
    # the label lands as a BARE resource attribute keyed by the exact label id (e.g. `rack`,
    # `platform_type`) -- not `syslog.*`, not prefixed -- carrying the description value, the
    # same way metrics apply it. The test pins the literal label id on resource.attributes.
    # Build 004 exported empty resource attributes on logs; retest against a build carrying
    # the feature owner's fixes -> if still empty it is a PRODUCT BUG (see the NVBug draft).
    LABEL_ID = "syslog-otel-label"
    LABEL_DESCRIPTION = "syslog-otel-test"
    LABEL_DESCRIPTION_KEY = "description"
    ATTR_KEY_LABEL = LABEL_ID  # bare resource-attribute key = the literal label id

    # --- Secured channel session bring-up (plan Section 6.4) -----------------------
    # The secured (TLS) suite enables one metric family purely to open the TLS OTLP
    # gRPC session during setup (before logs export is turned on, so `netstat` sees an
    # ESTABLISHED session). Keep this to a universally-supported family only: enabling
    # ib-router-stats triggered an unmapped-metric subscription-retry storm on the
    # juliet-128 (NVLink) build that destabilizes the telemetry pipeline, so the syslog
    # secured suite enables platform-stats only.
    SECURED_SESSION_FAMILIES = (TelemetryConsts.PLATFORM_STATS,)

    # --- Grey-box config correctness (plan Section 6.7a) ---------------------------
    # Token present in COLLECTOR_CONFIG_PATH only while logs export is active.
    COLLECTOR_LOGS_CONFIG_TOKEN = "syslog"

    # --- Tech-support forensic artifacts (plan Section 10.4) -----------------------
    # Filenames expected in the tech-support bundle when the feature is enabled.
    TECHSUPPORT_SYSLOG_ARTIFACTS = [
        RSYSLOG_FRAGMENT_PATH.rsplit("/", 1)[-1],
        COLLECTOR_CONFIG_PATH.rsplit("/", 1)[-1],
    ]

    # --- Log-error detection (plan Section 11 open issue 2, used by Section 6.7b) ---
    # The feature owner confirmed V1 has NO self-telemetry counters (no logs
    # received/exported/dropped, parse-error or export-failure metrics), so failure
    # detection cannot read counters and relies on marker NON-DELIVERY + core-daemon health.
    # No on-switch error strings are confirmed either, so the optional log-error-pattern leg
    # stays disabled (empty list => that leg is skipped; non-delivery + daemon health still
    # run). Populate this only if a later build emits a stable error signature.
    LOG_ERROR_PATTERNS = []  # none confirmed — log-error-pattern leg stays disabled
    # On-switch log the error patterns above would be matched against, if ever populated.
    LOG_ERROR_SOURCE = "/var/log/syslog"

    # --- Core-daemon health (plan Section 6.7b step 5 / 8.1 step 5) -----------------
    # A syslog-over-OTEL failure/overload is non-critical and must not degrade the
    # switch: these core NOS daemons must stay healthy. Each is only asserted when it is
    # actually loaded on the platform (units differ IB vs NVLink and some run in
    # containers), so the check never false-fails on a daemon the platform does not run.
    # Reuses the OTEL suite's unit names (cumulus CumulusOtelConst.TEST01_SYSTEMD_UNIT_NAMES).
    CORE_DAEMONS = ("frr", "switchd", "nvued")

    # --- Post-disable teardown check -----------------------------------------------
    # Disabling the feature removes the rsyslog drop-in and stops forwarding; the tests
    # prove this with fragment-absent + forwarding-inactive (the feature owner confirmed
    # this is the strongest functional check). Ownership: NVUE only writes
    # syslog_state=disabled to CONFIG_DB; hostcfgd then reloads nv-umf-manager, which removes
    # the drop-in and runs `systemctl reload-or-restart rsyslog` -- so the refresh LAGS the
    # NVUE apply. We do NOT assert an rsyslog reload marker: stock NVOS rsyslog has no
    # ExecReload, so `reload-or-restart` performs a full RESTART (journal:
    # "exiting on signal 15" / "Started rsyslog.service"), never "rsyslogd was HUPed" (those
    # lines come from logrotate, not this path); the restart signature lands in the systemd
    # journal, not /var/log/syslog which the suite reads, and the async refresh cannot be
    # timed cleanly. When the drop-in was never created (no destination) disable is a no-op
    # for rsyslog by design.

    # --- Toggle-stability stress (plan Section 8.3) --------------------------------
    # Enable/disable cycles run in the repeated-toggle test. Each cycle costs two collector
    # restarts plus an absence wait, so this dominates suite runtime; 5 keeps meaningful
    # repetition while roughly halving the cost (drop to 3 for a fast smoke run).
    TOGGLE_STABILITY_CYCLES = 5

    # --- Resource-budget stress (plan Section 8.1 / 9.1) ---------------------------
    # Shared body prefix for burst/sustained-rate lines; the last line emitted is a
    # unique sentinel whose delivery is asserted.
    STRESS_BODY_PREFIX = "syslog-otel-stress"
    # Test characterization values: the feature owner confirmed V1 has NO official product
    # burst/rate thresholds (only informal numbers from the verification-plan review), so
    # these size the stress load but are never asserted as product limits.
    LOG_STORM_BURST_COUNT = 200
    SUSTAINED_RATE_MSGS_PER_SEC = 10
    SUSTAINED_RATE_DURATION_SEC = 30

    # --- Drop measurement (plan Section 8.1 / 9.1 — emitted-vs-received delta) ------
    # A counted batch emits `<batch-id>_<n>` lines; delivery is counted by grepping the
    # receiver artifact for the distinct `<batch-id>_<n>` tokens. drops = emitted - received.
    DROP_MEASURE_BODY_PREFIX = "syslog-otel-count"
    # Count of unique markers emitted in a counted batch (drop-delta legs). The plan's
    # ~1k logs/s reference is a characterization target; the feature owner confirmed V1 has
    # no committed per-platform max rate, so this is a test value, not a product limit.
    DROP_MEASURE_COUNT = 200
    # Extra settle time after a counted batch before counting arrivals (drain the queue).
    DROP_MEASURE_SETTLE_SEC = 15
    # Resource budget for the collector under load (plan §8.1/§9.1: memory ≤10%, CPU ≤10%).
    # The authoritative budget assertion reuses the OTEL suite's mem/CPU limit tests; how to
    # invoke those from the syslog suite is an open item for the OTEL suite owner, so the
    # combined-load / budget legs are gated on STRESS_BUDGET_REUSE_AVAILABLE below.
    COLLECTOR_RESOURCE_BUDGET_PCT = 10
    # Set True once the OTEL suite's mem/CPU-budget reuse hook is wired for the syslog suite
    # (plan Section 9.1 combined ~40k metrics/s + ~1k logs/s leg + the ≤10% budget check).
    STRESS_BUDGET_REUSE_AVAILABLE = False

    # --- Local staging for fetched receiver artifacts ------------------------
    LOCAL_ARTIFACT_DIR = "/tmp/syslog-otel-artifacts"

    # --- Forwarding-ready gating -------------------------------------------------
    # `nv config apply` restarts the collector container, so forwarding resumes ~17-41s
    # later, not instantly. Tests emit throwaway readiness markers until forwarding reaches
    # the expected state before probing.
    # Overall ceiling for the gate (both transitions loop under this deadline).
    FORWARDING_READY_TIMEOUT_SEC = 180
    # Wait per readiness marker in the active gate: must EXCEED the post-reconfigure
    # collector-restart latency (~17-41s), else an in-flight marker is never caught and the
    # gate loops needlessly. Sized just above that latency.
    FORWARDING_ACTIVE_PROBE_SEC = 45
    # Wait per readiness marker in the inactive gate. Absence cannot short-circuit, so the
    # final (undelivered) probe pays this value in full every time a test disables the feature.
    # Once teardown removes the drop-in, forwarding stays down, so a short window is safe and
    # this is the dominant suite-wide runtime saver (was folded into a single 90s constant).
    FORWARDING_INACTIVE_PROBE_SEC = 15

    @classmethod
    def new_marker(cls):
        """Return a unique, shell-safe marker string (prefix + uuid hex)."""
        import uuid
        return "{}_{}".format(cls.MARKER_PREFIX, uuid.uuid4().hex)
