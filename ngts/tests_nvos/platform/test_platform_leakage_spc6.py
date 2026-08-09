"""
SPC-6 leakage detection — Core #1–#4, SN 5–8.

Leak injection for Core #1–#3 / SN 5 / SN 6–8 uses BMC CPLD ``i2ctransfer`` over OOB SSH.
Core #4 intentionally faults the NOS sysfs read path (no BMC injection) and asserts
BMC sysfs stays OK — NOS corruption must not poison BMC.
SN 5 stubs ``smonctl`` (exit 1) while sysfs still tracks CPLD — upper layers
must fail-closed (never stale OK). Owner (Jay) approved short stub window; ignore
global health noise under stub.

TODO — generalization for platforms with more than 2 sensors:
    Inventory/asserts are already dynamic (``leakage_counter`` / ``get_leakage_sensors``).
    Core #2 parametrizes over ``CPLD_MASK_BY_SENSOR_INDEX`` keys. The remaining bind is
    the CPLD bit map + lab sim (today Salamandra / mlx-6600: 2 sensors, bus 5, 0x31, 0x20).
    Unsupported DUT classes fail the whole module via ``assert_cpld_i2c_platform_supported``
    (``LEAKAGE_CPLD_I2C_SUPPORTED_DUT_TYPES``). Before extending (e.g. Chameleon6M):
      - Which I2C bus / address / register controls each sensor's bit?
      - What is the bitmask for each sensor index beyond index 2?
      - Are additional sensors on the same register byte, or on separate registers?
    Add entries to ``CPLD_MASK_BY_SENSOR_INDEX`` (and bus/addr if needed), then add the
    DUT class to ``LEAKAGE_CPLD_I2C_SUPPORTED_DUT_TYPES``. Lab sim today is 2 sensors
    max; masks / bus / addr above are Salamandra SN6600-specific.
"""
import json
import logging
import os
import time
import pytest

from ngts.nvos_constants.constants_nvos import HealthConsts
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.platform.leakage_helpers import (
    BMC_CPLD_LEAK_SENSOR1_AND_2,
    read_bmc_firmware_version,
    BMC_HWMGMT_LEAKAGE_AGGR,
    BMC_HWMGMT_LEAKAGE_LEAK,
    BMC_HWMGMT_LEAKAGE_OK,
    GNMI_ALARM_STATUS_LEAK,
    GNMI_ALARM_STATUS_OK,
    HWMGMT_SERVICE_NAME,
    LEAKAGE_NVUE_STATE_ALERT,
    HEALTH_ISSUE_LEAKAGE_ALERT,
    LEAKAGE_NVUE_STATE_NORMAL,
    PNE_SERVICE_NAME,
    PNE_TELEMETRY_STATUS_LEAK,
    PNE_TELEMETRY_STATUS_OK,
    SMONCTL_STATUS_LEAK,
    SMONCTL_STATUS_NORMAL,
    SYSFS_FAULT_VALUES,
    SYSFS_FAULT_CORRUPTIONS,
    UMF_SERVICE_NAME,
    assert_bmc_hwmgmt_leakage_values,
    assert_bmc_leak_detection_states,
    assert_gnmi_component_alarm_statuses,
    assert_gnmi_leakage_statuses,
    assert_gnmi_on_change_clear_transition,
    assert_gnmi_on_change_leak_transition,
    assert_gnmi_on_change_no_leak,
    assert_gnmi_oper_statuses,
    assert_sysfs_fault_health_not_silently_ok,
    assert_sysfs_fault_nvue_rest_not_ok,
    assert_leakage_nvue_rest_states,
    assert_sysfs_fault_pne_not_ok,
    assert_pne_telemetry_leakage_statuses,
    assert_sysfs_fault_smonctl_not_ok,
    assert_smonctl_leakage_statuses,
    assert_smonctl_command_failed,
    assert_smonctl_fault_gnmi_not_ok,
    assert_smonctl_fault_otel_not_ok,
    apply_sysfs_fault_corruption,
    assert_cl_support_leak_evidence,
    cleanup_cl_support_archive,
    close_bmc_oob_shell,
    close_gnmi_on_change_subscription,
    count_pne_telemetry_leakage_lines,
    create_leakage_gnmi_client,
    ensure_gnmi_server_ready,
    restore_gnmi_server_state,
    extract_cl_support_archive_path,
    GNMI_OPER_STATUS_ACTIVE,
    GNMI_SETUP_ERR_GNMIC_MISSING,
    clear_and_verify_cpld,
    clear_and_verify_cpld_with_revive,
    CPLD_MASK_BY_SENSOR_INDEX,
    get_hwmon_leakage_folder,
    get_leakage_sensors,
    inject_leak_via_bmc_cpld,
    inject_leak_via_sysfs,
    inject_single_sensor_bmc_cpld,
    open_bmc_oob_shell,
    open_gnmi_on_change_subscription,
    otel_collector_file_size,
    parse_terminal_json,
    read_bmc_hwmgmt_leakage_file,
    read_bmc_leak_detection_json,
    read_health_history_since,
    read_nvue_leakage,
    read_rest_leakage,
    read_smonctl_leakage,
    read_pne_telemetry_leakage_metrics,
    reboot_bmc_soft,
    reboot_dut_pdb_power_cycle,
    reboot_dut_soft,
    assert_bmc_rebooted,
    assert_dut_rebooted,
    read_bmc_boot_identity,
    read_dut_boot_identity,
    PDB_GNMI_PORT_READY_TIMEOUT,
    PDB_POWER_CYCLE_SSH_TIMEOUT,
    PDB_REBOOT_SSH_DOWN_TIMEOUT,
    remove_sysfs_leakage_file,
    resolve_bmc_oob_ip,
    restart_service,
    restore_sysfs_leakage_links,
    run_cl_support,
    save_cl_support_archive_aside,
    HEALTH_HISTORY_SETTLE_SEC,
    LEAKAGE_SURFACE_HOLD_TIMEOUT,
    LEAKAGE_SURFACE_POLL_INTERVAL,
    LEAKAGE_SURFACE_REBOOT_TIMEOUT,
    LEAKAGE_SURFACE_SETTLE_TIMEOUT,
    SMONCTL_FAULT_OTEL_SETTLE_SEC,
    SMONCTL_FAULT_PNE_SETTLE_SEC,
    SOFT_REBOOT_SSH_DOWN_TIMEOUT,
    SYSFS_FAULT_SETTLE_SEC,
    smonctl_restore_failed,
    sysfs_restore_failed,
    cpld_clear_failed,
    mark_cpld_clear_failed,
    smonctl_stubbed,
    assert_cpld_masks_cover_all_sensors,
    skip_suite_unless_leakage_sensors,
    verify_platform_versions,
    wait_for_bmc_hwmgmt_leakage_values,
    wait_for_bmc_oob_ssh,
    wait_for_bmc_oob_ssh_down,
    wait_for_dut_ssh,
    wait_for_dut_ssh_down,
    wait_for_gnmi_leakage_status,
    wait_for_gnmi_on_change_initial_status,
    wait_for_nvue_leakage_states,
    wait_for_pne_metric_available,
    wait_for_pne_leakage_statuses,
    wait_for_service_active,
    wait_for_sysfs_leakage_missing_settled,
    wait_for_sysfs_leakage_value,
    OTEL_LEAKAGE_STATUS_OK,
    OTEL_LEAKAGE_STATUS_LEAK,
    assert_otel_leakage_statuses,
    assert_otel_sensors_present,
    wait_for_otel_leakage_value,
    setup_otel_leakage_collector,
    teardown_otel_leakage_collector,
    apply_otel_leakage_dut_export,
)
from ngts.tools.test_utils import allure_utils as allure

cumulus_owner = "gpazbenitzha"

logger = logging.getLogger()

OK = HealthConsts.OK
NOT_OK = HealthConsts.NOT_OK


class BmcOobShellHolder:
    """Mutable BMC OOB SSH handle for fixture teardown after test reconnects.

    Assign a fresh engine to ``.shell`` after BMC reboot / re-open so
    ``bmc_cpld_shell`` teardown and ``leakage_recovery_guard`` clear CPLD on a
    live session instead of a closed one. ``.oob_ip`` travels with the handle so
    cleanup can re-open the shell itself when a test aborts with a closed one in
    place — see ``clear_and_verify_cpld_with_revive``.
    """

    __slots__ = ("shell", "oob_ip")

    def __init__(self, shell, oob_ip=None):
        self.shell = shell
        self.oob_ip = oob_ip


def _require_bmc(devices):
    """Hard-fail if DUT has no BMC — suite is BMC-dependent (CPLD sim + BMC surfaces)."""
    if not getattr(devices.dut, "has_bmc", False):
        raise AssertionError(
            "SPC-6 leakage suite requires a BMC: CPLD leak simulation and BMC "
            "surfaces (OOB i2ctransfer, BMC sysfs, bmc-cli) are BMC-dependent. "
            "devices.dut.has_bmc is False — refusing to run."
        )


@pytest.fixture(scope="module", autouse=True)
def check_platform_versions(engines, devices):
    """Verify platform / CL / BMC / CPLD gates once per module before any leakage test.

    Includes the CPLD i2ctransfer SKU allowlist — unsupported DUT → entire suite fails.
    After version/SKU gates: skip the module if ``leakage_counter`` is missing or 0
    (DUT has no leakage sensors — suite not applicable), then require the CPLD mask
    table to cover exactly this DUT's sensors so injection coverage cannot drift
    without failing (partial coverage must never report green).
    """
    verify_platform_versions(engines, devices)
    sensor_count = skip_suite_unless_leakage_sensors(engines.dut)
    assert_cpld_masks_cover_all_sensors(sensor_count)


@pytest.fixture
def leakage_gnmi_enabled():
    """Opt-in token for leakage cases that exercise gNMI."""
    return None


@pytest.fixture(scope="function", autouse=True)
def ensure_leakage_gnmi_server(request, engines, nv_command):
    """Enable DUT gnmi-server (remote bind) so Core #1–3 / SN 7 gNMI steps can reach :9339.

    Function-scoped: NVUE config restore often ``nv unset system gnmi-server`` after each
    test, which would leave later cores with connection refused if we only enabled once.
    Uses ``0.0.0.0`` when localhost-only (HW-proven for remote gnmic); restores the prior
    listening-address in teardown when we widened it. Mid-test re-ensures (e.g. SN 7 after
    reboot) append to the same restore list via ``request.node._leakage_gnmi_listen_restores``.
    """
    restores = []
    request.node._leakage_gnmi_listen_restores = restores
    if "leakage_gnmi_enabled" in request.fixturenames:
        restores.append(ensure_gnmi_server_ready(nv_command, engines))
    yield
    for state in reversed(restores):
        restore_gnmi_server_state(nv_command, state)


@pytest.fixture(autouse=True)
def _abort_if_smonctl_restore_failed():
    """If SN 5 left smonctl stubbed, fail remaining module tests instead of cascading noise."""
    if smonctl_restore_failed():
        pytest.fail(
            "smonctl restore failed earlier in this module — DUT may still be stubbed; "
            "aborting remaining leakage tests. "
            "manual: sudo mv -f $(command -v smonctl).ngts_bak $(command -v smonctl)"
        )


@pytest.fixture(autouse=True)
def _abort_if_sysfs_restore_failed():
    """If Core #4 left leakage sysfs unrestored, fail remaining module tests."""
    if sysfs_restore_failed():
        pytest.fail(
            "sysfs leakage symlink restore failed earlier in this module — DUT may lack "
            "usable /var/run/hw-management/system/leakageN links; aborting remaining "
            "leakage tests. manual: restore symlinks into hwmon leakage files"
        )


@pytest.fixture(autouse=True)
def _abort_if_cpld_clear_failed():
    """If CPLD clear/verify failed in cleanup, fail remaining module tests."""
    if cpld_clear_failed():
        pytest.fail(
            "CPLD clear/verify failed earlier in this module — DUT may still be "
            "simulating a leak via i2ctransfer; aborting remaining leakage tests. "
            "manual: clear CPLD leak registers over BMC OOB SSH"
        )


@pytest.fixture(scope="module")
def bmc_version(engines):
    return read_bmc_firmware_version(engines.dut)


@pytest.fixture(scope="module")
def otel_collector(engines, nv_command, check_platform_versions):
    """Start OTEL collector and configure switch OTEL export.

    Depends on ``check_platform_versions`` so unsupported / no-BMC DUTs fail the
    module gate first — no GitHub download, DUT telemetry rewrite, or sample wait.

    Module-scoped: collector runs once for the full module; samples accumulate.
    Prefers ``engines.sonic_mgmt``; on CL BSP setups without that player, runs the
    collector on the local pytest host (VDI) and advertises the DUT-reachable IP.
    Yields the collector engine for per-test helpers.
    """
    _ = check_platform_versions  # dependency only — ensures module gates ran first
    try:
        collector_engine = setup_otel_leakage_collector(engines, nv_command)
    except BaseException:
        # Setup accumulates state before it can fail: a /var/tmp run dir, a root otelcol
        # holding :4317, and rewritten DUT telemetry export. A fixture that raises during
        # setup never reaches the teardown below, so clean up from here instead — a stranded
        # collector fails the *next* run's preflight on port 4317, one box away from the
        # failure that caused it.
        #
        # BaseException, not Exception: Ctrl-C during a 50-minute run is the likeliest way
        # to hit this, and an interrupt that leaves the port held is the same mess.
        #
        # Best-effort and re-raised bare, so the cleanup cannot replace the failure it is
        # cleaning up after.
        try:
            teardown_otel_leakage_collector(engines, nv_command)
        except Exception:
            logger.exception("OTEL collector cleanup after failed setup did not complete")
        raise
    try:
        yield collector_engine
    finally:
        teardown_otel_leakage_collector(engines, nv_command)


@pytest.fixture(autouse=True)
def _reapply_otel_export_each_test(request, engines):
    """Ensure DUT OTEL export is present before every test that uses ``otel_collector``.

    NGTS per-test cleanup runs ``nv unset system telemetry``, which removes the
    module-scoped OTLP destination. Without a re-apply, Core2+ read a stale
    collector file. ``apply_otel_leakage_dut_export`` skips apply when already
    configured so we do not restart telemetry services on every test.

    Tests that do not request ``otel_collector`` (e.g. SN 8) skip this fixture body
    so the module-scoped collector is not force-loaded.
    """
    if "otel_collector" not in request.fixturenames:
        yield
        return
    collector = request.getfixturevalue("otel_collector")
    apply_otel_leakage_dut_export(engines.dut, collector.ip)
    yield


@pytest.fixture
def leakage_sensors(engines, devices):
    return get_leakage_sensors(engines.dut, devices)


@pytest.fixture
def bmc_cpld_shell(engines, topology_obj, devices):
    """OOB BMC shell holder with CPLD cleared/verified before and after the test.

    Yields a ``BmcOobShellHolder`` (``.shell``). Tests that reconnect after BMC/NOS
    reboot must assign the fresh engine to ``holder.shell`` so teardown and
    ``leakage_recovery_guard`` always clear against a live session.
    """
    _require_bmc(devices)
    oob_ip = resolve_bmc_oob_ip(engines, topology_obj)
    holder = BmcOobShellHolder(open_bmc_oob_shell(oob_ip), oob_ip)
    try:
        clear_and_verify_cpld(holder.shell)
    except Exception:
        close_bmc_oob_shell(holder.shell)
        raise
    try:
        yield holder
    finally:
        clear_err = None
        try:
            holder.shell = clear_and_verify_cpld_with_revive(
                holder.shell, holder.oob_ip
            )
        except Exception as err:
            clear_err = err
            logger.error(
                "BMC CPLD cleanup/verification failed: %s — refusing soft pass with dirty CPLD",
                err,
            )
            mark_cpld_clear_failed()
        close_bmc_oob_shell(holder.shell)
        if clear_err is not None:
            raise AssertionError(
                f"BMC CPLD cleanup/verification failed: {clear_err}"
            ) from clear_err


@pytest.fixture
def leakage_recovery_guard(bmc_cpld_shell, nv_command, leakage_sensors):
    """Test-local failure-path recovery for cases that mutate leakage state."""
    yield
    try:
        bmc_cpld_shell.shell = clear_and_verify_cpld_with_revive(
            bmc_cpld_shell.shell, bmc_cpld_shell.oob_ip
        )
        wait_for_nvue_leakage_states(
            nv_command, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL
        )
    except Exception as err:
        mark_cpld_clear_failed()
        raise AssertionError(
            f"Test-local leakage recovery failed (CPLD/NVUE): {err}"
        ) from err


# Health/LED lags the leakage leaf it is derived from: the platform monitor recomputes it on
# its own cadence, so gating on NVUE first does not mean health has caught up. Measured on
# -08 2026-09-30, one inject went green at +1s, green at +5s, amber at +10s — landing on the
# last poll of a 10s budget — while core2[2] in the same run missed it, purely on where the
# cycle fell. A state *change* therefore needs a propagation budget like every other surface
# (SETTLE = 2x cadence, so one entirely missed recompute still passes), and only a baseline
# check — where nothing has changed — is a true confirmation poll.
# Post-reboot convergence is a third case; see the docstring below for the helper it needs.
HEALTH_STEADY_STATE_TIMEOUT = 10        # baseline only: nothing changed, nothing to propagate


def _validate_health(
    nv_command,
    expected,
    expected_led=None,
    timeout=LEAKAGE_SURFACE_SETTLE_TIMEOUT,
    interval=LEAKAGE_SURFACE_POLL_INTERVAL,
):
    """Poll health/LED after a leakage state change (inject or clear, no reboot).

    Defaults to the surface SETTLE budget because health is recomputed on the platform
    monitor's cadence rather than with the leakage leaf — see the constant above. Baseline
    checks, where no state has changed, pass ``HEALTH_STEADY_STATE_TIMEOUT`` instead.

    Post-reboot paths must use
    ``nv_command.system.health.wait_until_health_status_change_after_reboot``
    (``@retry(tries=12, delay=30)`` ≈ 6 min) — health/LED do not converge in 10s
    after soft reboot or PDB power cycle.
    """
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            nv_command.system.validate_health_status(expected, expected_led=expected_led)
            return
        except AssertionError as err:
            last_err = err
            time.sleep(interval)
    raise AssertionError(
        f"Health did not reach {expected!r} within {timeout}s: {last_err}"
    )


def _collect_sysfs_fault_surface_errors(
    engines, nv_command, sensor, label, errors, bmc_engine=None, sibling_sensors=None,
):
    """Run Core #4 surface checks for one fault injection; append failures to ``errors``.

    Surfaces: smonctl, NVUE, system health, PNE metric, REST, and (when provided)
    BMC hw-mgmt sysfs — which must stay OK. That BMC check is the independence proof:
    NOS-side sysfs corruption must not poison the BMC's CPLD-backed view.

    Invariant per NOS surface: command returns (no crash/timeout); faulted sensor is never OK.
    Invariant for BMC: faulted sensor still reports BMC_HWMGMT_LEAKAGE_OK.

    ABSENT is accepted only with sibling evidence (other sensors still OK) so a dead
    surface cannot pass as fail-closed.

    Collect-all: catch AssertionError *and* command/transport failures (e.g. read timeout)
    so one hung surface does not abort the remaining checks for this fault case.
    """
    siblings = [s for s in (sibling_sensors or []) if s.lower() != sensor.lower()]
    prefix = f"[{label}]"
    logger.info("%s collecting surfaces for faulted sensor %s (siblings=%s)", prefix, sensor, siblings)

    with allure.step(f"{prefix} smonctl — returns, faulted sensor not OK"):
        try:
            raw = engines.dut.run_cmd("smonctl -jvt leakage", validate=False)
            assert raw is not None and str(raw).strip() != "", (
                "smonctl returned empty output (no usable response)"
            )
            logger.info("%s smonctl raw: %s", prefix, raw)
            try:
                smon = parse_terminal_json(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                smon = None
                logger.info("%s smonctl non-JSON output (fail-closed): %r", prefix, str(raw)[:200])
            else:
                logger.info("%s smonctl parsed: %s", prefix, smon)
            assert_sysfs_fault_smonctl_not_ok(smon, sensor, sibling_sensors=siblings)
        except Exception as e:
            errors.append(f"{prefix} smonctl: {type(e).__name__}: {e}")

    with allure.step(f"{prefix} NVUE leakage — returns, faulted sensor not normal"):
        try:
            raw = engines.dut.run_cmd(
                "nv show platform environment leakage -o json", validate=False
            )
            assert raw is not None and str(raw).strip() != "", (
                "NVUE leakage show returned empty output (no usable response)"
            )
            logger.info("%s NVUE raw: %s", prefix, raw)
            try:
                nvue = OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()
            except Exception as parse_err:
                logger.info("%s NVUE non-JSON / parse failure (fail-closed): %s", prefix, parse_err)
                nvue = {}
            else:
                logger.info("%s NVUE parsed: %s", prefix, nvue)
            assert_sysfs_fault_nvue_rest_not_ok(nvue, sensor, sibling_sensors=siblings)
        except Exception as e:
            errors.append(f"{prefix} NVUE: {type(e).__name__}: {e}")

    with allure.step(f"{prefix} system health — returns, not silently OK for faulted sensor"):
        try:
            raw = nv_command.system.health.show()
            assert raw is not None and str(raw).strip() != "", (
                "nv show system health returned empty output"
            )
            health = OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()
            logger.info("%s system health: %s", prefix, health)
            assert_sysfs_fault_health_not_silently_ok(health, sensor)
        except Exception as e:
            errors.append(f"{prefix} system health: {type(e).__name__}: {e}")

    with allure.step(f"{prefix} PNE metric — returns, faulted sensor not OK (1)"):
        try:
            metrics = read_pne_telemetry_leakage_metrics(engines.dut)
            assert metrics is not None and str(metrics).strip() != "", (
                "PNE /metrics returned empty output"
            )
            # Full /metrics blob is huge; log only the leakage lines (+ parse outcome in assert).
            leak_lines = [
                line for line in str(metrics).splitlines()
                if "leak_sensor_status" in line and not line.startswith("#")
            ]
            logger.info("%s PNE leakage metric lines: %s", prefix, leak_lines)
            assert_sysfs_fault_pne_not_ok(metrics, sensor, sibling_sensors=siblings)
        except Exception as e:
            errors.append(f"{prefix} PNE telemetry: {type(e).__name__}: {e}")

    with allure.step(f"{prefix} REST leakage — returns, faulted sensor not normal (NVUE parity)"):
        try:
            rest = read_rest_leakage(engines.dut)
            assert rest is not None, "REST leakage returned no usable response"
            logger.info("%s REST parsed: %s", prefix, rest)
            assert_sysfs_fault_nvue_rest_not_ok(rest, sensor, sibling_sensors=siblings)
        except Exception as e:
            errors.append(f"{prefix} REST: {type(e).__name__}: {e}")

    if bmc_engine is not None:
        with allure.step(
            f"{prefix} BMC sysfs — faulted sensor still OK "
            "(NOS corruption must not poison BMC); siblings still OK"
        ):
            try:
                assert_bmc_hwmgmt_leakage_values(
                    bmc_engine, [sensor], BMC_HWMGMT_LEAKAGE_OK
                )
                if siblings:
                    assert_bmc_hwmgmt_leakage_values(
                        bmc_engine, siblings, BMC_HWMGMT_LEAKAGE_OK
                    )
                logger.info(
                    "%s BMC sysfs %s still %r (siblings %s OK) — independence OK",
                    prefix, sensor, BMC_HWMGMT_LEAKAGE_OK, siblings,
                )
            except Exception as e:
                errors.append(f"{prefix} BMC sysfs independence: {type(e).__name__}: {e}")


@pytest.mark.cumulus
@pytest.mark.platform
@pytest.mark.disable_loganalyzer
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_leakage_core1_no_leak_baseline_cross_surface(
    engines, nv_command, leakage_sensors, bmc_cpld_shell, bmc_version,
    otel_collector, leakage_gnmi_enabled,
):
    """Core #1: no-leak baseline + cross-surface agreement (BMC CPLD cleared)."""
    with allure.step("Health history snapshot — pre-test baseline"):
        history_snapshot = nv_command.system.health.history.show()

    with allure.step("Gate: clear CPLD and wait for full stack propagation"):
        clear_and_verify_cpld(bmc_cpld_shell.shell)
        wait_for_nvue_leakage_states(
            nv_command, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL
        )

    # Open gNMI ON_CHANGE after the gate; wait for initial sync leaf (current value),
    # not a fixed sleep — gNMI requires current-state updates before sync_response.
    errors = []
    gnmi_client = create_leakage_gnmi_client(engines)
    on_change_proc = None
    if gnmi_client is None:
        errors.append(GNMI_SETUP_ERR_GNMIC_MISSING)
    else:
        on_change_proc = open_gnmi_on_change_subscription(engines, leakage_sensors[0])
        try:
            wait_for_gnmi_on_change_initial_status(on_change_proc, GNMI_ALARM_STATUS_OK)
        except AssertionError as e:
            errors.append(f"gNMI ON_CHANGE initial sync: {e}")
            close_gnmi_on_change_subscription(on_change_proc)
            on_change_proc = None

    try:
        with allure.step("System health OK, LED green, no issues at baseline"):
            try:
                # Baseline: nothing was injected, so health is not catching up to anything
                # and the tight confirmation poll is the honest assertion. Using the
                # propagation budget here would hide a DUT that starts out unhealthy.
                _validate_health(
                    nv_command, OK, expected_led=HealthConsts.LED_OK_STATUS,
                    timeout=HEALTH_STEADY_STATE_TIMEOUT,
                )
                health = OutputParsingTool.parse_json_str_to_dictionary(
                    nv_command.system.health.show()
                ).get_returned_value()
                issues = health.get(HealthConsts.ISSUES, {})
                leakage_issues = {k: v for k, v in issues.items() if k in leakage_sensors}
                assert not leakage_issues, (
                    f"Expected no leakage sensors in health issues at baseline, got: {leakage_issues}"
                )
            except AssertionError as e:
                errors.append(f"System health: {e}")

        with allure.step("NVUE leakage — all sensors normal"):
            try:
                nvue = read_nvue_leakage(nv_command)
                assert len(nvue) == len(leakage_sensors), (
                    f"NVUE: expected {len(leakage_sensors)} sensors, got {len(nvue)}: {sorted(nvue)}"
                )
                assert_leakage_nvue_rest_states(nvue, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL)
            except AssertionError as e:
                errors.append(f"NVUE: {e}")

        with allure.step("smonctl leakage — primary source, all sensors normal"):
            try:
                smon = read_smonctl_leakage(engines.dut)
                logger.info("smonctl leakage: %s", smon)
                assert len(smon) == len(leakage_sensors), (
                    f"smonctl: expected {len(leakage_sensors)} sensors, got {len(smon)}: {[i['name'] for i in smon]}"
                )
                assert_smonctl_leakage_statuses(smon, leakage_sensors, SMONCTL_STATUS_NORMAL)
            except AssertionError as e:
                errors.append(f"smonctl: {e}")

        with allure.step("REST leakage — all sensors normal"):
            try:
                rest = read_rest_leakage(engines.dut)
                assert len(rest) == len(leakage_sensors), (
                    f"REST: expected {len(leakage_sensors)} sensors, got {len(rest)}: {sorted(rest)}"
                )
                assert_leakage_nvue_rest_states(rest, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL)
            except AssertionError as e:
                errors.append(f"REST: {e}")

        with allure.step("PNE telemetry leakage — all sensors OK"):
            try:
                metrics = read_pne_telemetry_leakage_metrics(engines.dut)
                metric_count = count_pne_telemetry_leakage_lines(metrics)
                assert metric_count == len(leakage_sensors), (
                    f"telemetry: expected {len(leakage_sensors)} metric lines, got {metric_count}"
                )
                assert_pne_telemetry_leakage_statuses(metrics, leakage_sensors, expected_status=PNE_TELEMETRY_STATUS_OK)
            except AssertionError as e:
                errors.append(f"PNE telemetry: {e}")

        with allure.step("gNMI ONCE (exact xpath) — all sensors alarm-status=false (OK)"):
            try:
                if gnmi_client is not None:
                    assert_gnmi_leakage_statuses(gnmi_client, leakage_sensors, GNMI_ALARM_STATUS_OK)
            except AssertionError as e:
                errors.append(f"gNMI exact xpath: {e}")

        with allure.step("gNMI ONCE (component xpath) — alarm-status=false in state/ subtree"):
            try:
                if gnmi_client is not None:
                    assert_gnmi_component_alarm_statuses(gnmi_client, leakage_sensors, GNMI_ALARM_STATUS_OK)
            except AssertionError as e:
                errors.append(f"gNMI component xpath: {e}")

        with allure.step("gNMI ONCE (oper-status) — all sensors ACTIVE"):
            try:
                if gnmi_client is not None:
                    assert_gnmi_oper_statuses(gnmi_client, leakage_sensors, GNMI_OPER_STATUS_ACTIVE)
            except AssertionError as e:
                errors.append(f"gNMI oper-status: {e}")

        with allure.step("OTEL platform-stats — all sensors OK (1.0) + all sensors present"):
            try:
                # Module fixture waited one full sample interval before yielding, so at
                # least one batch is in the file by the time the test body runs here.
                # No poll needed: assert directly on the latest batch.
                assert_otel_sensors_present(otel_collector, leakage_sensors)
                assert_otel_leakage_statuses(otel_collector, leakage_sensors, OTEL_LEAKAGE_STATUS_OK)
            except AssertionError as e:
                errors.append(f"OTEL: {e}")

        with allure.step("BMC sysfs leakage files — all sensors OK (1)"):
            try:
                assert_bmc_hwmgmt_leakage_values(bmc_cpld_shell.shell, leakage_sensors, BMC_HWMGMT_LEAKAGE_OK)
                aggr_val = read_bmc_hwmgmt_leakage_file(bmc_cpld_shell.shell, BMC_HWMGMT_LEAKAGE_AGGR)
                logger.info("BMC sysfs %s = %s (informational only, not asserted)", BMC_HWMGMT_LEAKAGE_AGGR, aggr_val)
            except AssertionError as e:
                errors.append(f"BMC sysfs: {e}")

        with allure.step("BMC-CLI LeakDetection — all detectors OK"):
            # NOTE: DetectorState always asserted. Status.Health only when present (RM #5171996).
            try:
                bmc_leak = read_bmc_leak_detection_json(engines.dut)
                logger.info("BMC LeakDetection: %s", bmc_leak)
                assert_bmc_leak_detection_states(bmc_leak, leakage_sensors, leaking_sensors=[], bmc_version=bmc_version)
            except AssertionError as e:
                errors.append(f"BMC-CLI: {e}")

        with allure.step("Health history — no new leakage entries during baseline"):
            try:
                history_after = nv_command.system.health.history.show()
                for name in leakage_sensors:
                    count_before = str(history_snapshot).count(name)
                    count_after = str(history_after).count(name)
                    assert count_after <= count_before, (
                        f"Unexpected new history entry for {name!r} appeared during baseline test "
                        f"(was {count_before}, now {count_after})"
                    )
            except AssertionError as e:
                errors.append(f"Health history: {e}")

        with allure.step("gNMI ON_CHANGE — initial sync present, no leak notification at baseline"):
            # All surface checks (including PNE metric) have now run. PNE check is the gate:
            # if PNE showed the correct value, UMF has forwarded to gNMI — initial sync received.
            if on_change_proc is None:
                if gnmi_client is not None:
                    errors.append("gNMI ON_CHANGE: subscription was not opened")
            else:
                try:
                    on_change_output = close_gnmi_on_change_subscription(on_change_proc)
                    on_change_proc = None
                    assert_gnmi_on_change_no_leak(on_change_output, leakage_sensors[0])
                except AssertionError as e:
                    errors.append(f"gNMI ON_CHANGE: {e}")

        assert not errors, "Surface check failures:\n" + "\n".join(errors)

    finally:
        if on_change_proc is not None:
            try:
                close_gnmi_on_change_subscription(on_change_proc)
            except Exception as e:
                logger.warning("gNMI ON_CHANGE cleanup failed: %s", e)
            on_change_proc = None


@pytest.mark.cumulus
@pytest.mark.platform
@pytest.mark.disable_loganalyzer
@pytest.mark.timeout(15 * MINUTE, func_only=True)
@pytest.mark.parametrize("sensor_index", sorted(CPLD_MASK_BY_SENSOR_INDEX))
def test_leakage_core2_single_sensor(
    engines, nv_command, leakage_sensors, bmc_cpld_shell, sensor_index, bmc_version,
    otel_collector, leakage_gnmi_enabled, leakage_recovery_guard,
):
    """Core #2: single leak per sensor — all surfaces agree.

    inject_single_sensor_bmc_cpld clears the CPLD fully before asserting the target
    sensor bit, so the non-leaking sensors are always in the cleared state (i.e.
    injecting leakage1 implicitly cancels leakage2 and vice versa). The ``others``
    assertions below rely on this CPLD behavior.

    leakage_aggr is NOT injected or asserted: CPLD simulation does not reliably
    drive the aggregate bit under test conditions (would cause spurious fails).
    Aggregate coverage is deferred until sim/product behaviour is reliable.
    """
    if sensor_index > len(leakage_sensors):
        pytest.skip(f"Platform has only {len(leakage_sensors)} leak sensor(s)")

    sensor = leakage_sensors[sensor_index - 1]
    others = [s for s in leakage_sensors if s != sensor]

    # Open ON_CHANGE and wait for initial sync (false) before inject so the rising
    # edge is ordered false→true→false. Fixed sleep(3) was racing UMF/gNMI sync.
    errors = []
    gnmi_client = create_leakage_gnmi_client(engines)
    on_change_proc = None
    if gnmi_client is None:
        errors.append(GNMI_SETUP_ERR_GNMIC_MISSING)
    else:
        on_change_proc = open_gnmi_on_change_subscription(engines, sensor)
        try:
            wait_for_gnmi_on_change_initial_status(on_change_proc, GNMI_ALARM_STATUS_OK)
        except AssertionError as e:
            errors.append(f"gNMI ON_CHANGE initial sync: {e}")
            close_gnmi_on_change_subscription(on_change_proc)
            on_change_proc = None

    try:
        with allure.step(f"Inject leak on sensor {sensor_index} via BMC CPLD"):
            # Bound health-history asserts to this inject/clear cycle (not stale DUT logs).
            # Boundary comes from the DUT clock — same domain as the history stamps.
            history_since = read_health_history_since(engines.dut)
            inject_single_sensor_bmc_cpld(bmc_cpld_shell.shell, sensor_index)

        with allure.step("Gate: wait for leak propagation to NVUE"):
            try:
                wait_for_nvue_leakage_states(
                    nv_command, [sensor], LEAKAGE_NVUE_STATE_ALERT
                )
            except AssertionError as e:
                errors.append(f"Propagation gate (NVUE): {e}")

        with allure.step("System health not OK, LED amber — leaking sensor in issues"):
            try:
                _validate_health(nv_command, NOT_OK, expected_led=HealthConsts.LED_NOT_OK_STATUS)
                health = OutputParsingTool.parse_json_str_to_dictionary(
                    nv_command.system.health.show()
                ).get_returned_value()
                issues = health.get(HealthConsts.ISSUES, {})
                assert sensor in issues, (
                    f"Expected {sensor!r} in health issues (JSON key is lowercase), got: {list(issues)}"
                )
                assert issues[sensor].get("issue") == HEALTH_ISSUE_LEAKAGE_ALERT, (
                    f"Expected health issue value {HEALTH_ISSUE_LEAKAGE_ALERT!r} for {sensor!r}, got: {issues[sensor]}"
                )
            except AssertionError as e:
                errors.append(f"System health: {e}")

        with allure.step(f"NVUE leakage — {sensor} alert, all others normal"):
            try:
                nvue = read_nvue_leakage(nv_command)
                assert len(nvue) == len(leakage_sensors), (
                    f"NVUE: expected {len(leakage_sensors)} sensors, got {len(nvue)}: {sorted(nvue)}"
                )
                assert_leakage_nvue_rest_states(nvue, [sensor], LEAKAGE_NVUE_STATE_ALERT)
                assert_leakage_nvue_rest_states(nvue, others, LEAKAGE_NVUE_STATE_NORMAL)
            except AssertionError as e:
                errors.append(f"NVUE: {e}")

        with allure.step(f"smonctl leakage — {sensor} leaking (0), others normal (1)"):
            try:
                smon = read_smonctl_leakage(engines.dut)
                logger.info("smonctl leakage: %s", smon)
                assert len(smon) == len(leakage_sensors), (
                    f"smonctl: expected {len(leakage_sensors)} sensors, got {len(smon)}: {[i['name'] for i in smon]}"
                )
                assert_smonctl_leakage_statuses(smon, [sensor], SMONCTL_STATUS_LEAK)
                assert_smonctl_leakage_statuses(smon, others, SMONCTL_STATUS_NORMAL)
            except AssertionError as e:
                errors.append(f"smonctl: {e}")

        with allure.step(f"REST leakage — {sensor} alert, others normal"):
            try:
                rest = read_rest_leakage(engines.dut)
                assert len(rest) == len(leakage_sensors), (
                    f"REST: expected {len(leakage_sensors)} sensors, got {len(rest)}: {sorted(rest)}"
                )
                assert_leakage_nvue_rest_states(rest, [sensor], LEAKAGE_NVUE_STATE_ALERT)
                assert_leakage_nvue_rest_states(rest, others, LEAKAGE_NVUE_STATE_NORMAL)
            except AssertionError as e:
                errors.append(f"REST: {e}")

        with allure.step(f"PNE telemetry leakage — {sensor} metric=0 (LEAK_DETECTED), others metric=1 (OK)"):
            try:
                metrics = read_pne_telemetry_leakage_metrics(engines.dut)
                metric_count = count_pne_telemetry_leakage_lines(metrics)
                assert metric_count == len(leakage_sensors), (
                    f"telemetry: expected {len(leakage_sensors)} metric lines, got {metric_count}"
                )
                assert_pne_telemetry_leakage_statuses(metrics, [sensor], expected_status=PNE_TELEMETRY_STATUS_LEAK)
                assert_pne_telemetry_leakage_statuses(metrics, others, expected_status=PNE_TELEMETRY_STATUS_OK)
            except AssertionError as e:
                errors.append(f"PNE telemetry: {e}")

        with allure.step(f"gNMI ONCE (exact xpath) — {sensor} alarm-status=true (LEAK), others=false (OK)"):
            try:
                if gnmi_client is not None:
                    assert_gnmi_leakage_statuses(gnmi_client, [sensor], GNMI_ALARM_STATUS_LEAK)
                    assert_gnmi_leakage_statuses(gnmi_client, others, GNMI_ALARM_STATUS_OK)
            except AssertionError as e:
                errors.append(f"gNMI exact xpath: {e}")

        with allure.step(f"gNMI ONCE (component xpath) — {sensor} alarm-status=true in state/ subtree, others=false"):
            try:
                if gnmi_client is not None:
                    assert_gnmi_component_alarm_statuses(gnmi_client, [sensor], GNMI_ALARM_STATUS_LEAK)
                    assert_gnmi_component_alarm_statuses(gnmi_client, others, GNMI_ALARM_STATUS_OK)
            except AssertionError as e:
                errors.append(f"gNMI component xpath: {e}")

        with allure.step("gNMI ONCE (oper-status) — all sensors ACTIVE"):
            try:
                if gnmi_client is not None:
                    assert_gnmi_oper_statuses(gnmi_client, leakage_sensors, GNMI_OPER_STATUS_ACTIVE)
            except AssertionError as e:
                errors.append(f"gNMI oper-status: {e}")

        with allure.step(f"OTEL — {sensor} LEAK_DETECTED (0.0), others OK (1.0), all sensors present"):
            try:
                # Poll: PNE check above confirmed the state; OTEL needs ≤60s to catch up.
                # By now (after all surface checks) at least partial time has elapsed.
                wait_for_otel_leakage_value(otel_collector, [sensor], OTEL_LEAKAGE_STATUS_LEAK)
                assert_otel_sensors_present(otel_collector, leakage_sensors)
                assert_otel_leakage_statuses(otel_collector, others, OTEL_LEAKAGE_STATUS_OK)
            except AssertionError as e:
                errors.append(f"OTEL: {e}")

        with allure.step(f"BMC sysfs leakage files — {sensor} = 0 (leak), others = 1 (OK)"):
            try:
                assert_bmc_hwmgmt_leakage_values(bmc_cpld_shell.shell, [sensor], BMC_HWMGMT_LEAKAGE_LEAK)
                assert_bmc_hwmgmt_leakage_values(bmc_cpld_shell.shell, others, BMC_HWMGMT_LEAKAGE_OK)
                # leakage_aggr not asserted: CPLD simulation does not reliably drive the aggregate bit
            except AssertionError as e:
                errors.append(f"BMC sysfs: {e}")

        with allure.step("BMC-CLI LeakDetection — leaking sensor Critical, others OK"):
            # NOTE: DetectorState always; Status.Health presence-gated (RM #5171996).
            try:
                bmc_leak = read_bmc_leak_detection_json(engines.dut)
                logger.info("BMC LeakDetection: %s", bmc_leak)
                assert_bmc_leak_detection_states(
                    bmc_leak,
                    leakage_sensors,
                    leaking_sensors=[sensor],
                    bmc_version=bmc_version,
                )
            except AssertionError as e:
                errors.append(f"BMC-CLI: {e}")

        with allure.step(f"Health history — {sensor} transitioned from OK to BAD (since inject)"):
            try:
                nv_command.system.health.history.search_line_by_date(
                    lines_to_search=f"{sensor} changed from OK to BAD",
                    since_date=history_since,
                )
            except AssertionError as e:
                errors.append(f"Health history: {e}")

        with allure.step("Clear CPLD and verify full recovery"):
            try:
                clear_and_verify_cpld(bmc_cpld_shell.shell)
                wait_for_nvue_leakage_states(
                    nv_command, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL
                )
                _validate_health(nv_command, OK, expected_led=HealthConsts.LED_OK_STATUS)
                time.sleep(HEALTH_HISTORY_SETTLE_SEC)
                nv_command.system.health.history.search_line_by_date(
                    lines_to_search=f"{sensor} changed from BAD to OK",
                    since_date=history_since,
                )
            except AssertionError as e:
                errors.append(f"Recovery (CPLD clear / health / history): {e}")

        with allure.step("Recovery gate — PNE all OK (1) after CPLD clear"):
            # Live PNE settle before OTEL/ON_CHANGE: NVUE can be normal while UMF still
            # lags. Polling :9100 for metric=1 proves the scrape path recovered; instant
            # assert_pne_* / wait_for_pne_metric_available (presence-only) are not enough.
            try:
                wait_for_pne_leakage_statuses(
                    engines.dut, leakage_sensors, PNE_TELEMETRY_STATUS_OK
                )
            except AssertionError as e:
                errors.append(f"Recovery gate (PNE): {e}")

        with allure.step(f"Recovery gate — gNMI alarm-status=false for {sensor}"):
            try:
                if gnmi_client is not None:
                    wait_for_gnmi_leakage_status(
                        gnmi_client, sensor, GNMI_ALARM_STATUS_OK
                    )
                    assert_gnmi_leakage_statuses(gnmi_client, others, GNMI_ALARM_STATUS_OK)
            except AssertionError as e:
                errors.append(f"Recovery gate (gNMI): {e}")

        with allure.step("OTEL recovery — all sensors OK (1.0)"):
            try:
                # Stall kick: if collector file mtime freezes, force one DUT OTEL re-apply.
                wait_for_otel_leakage_value(
                    otel_collector,
                    leakage_sensors,
                    OTEL_LEAKAGE_STATUS_OK,
                    dut=engines.dut,
                    collector_ip=otel_collector.ip,
                )
            except AssertionError as e:
                errors.append(f"OTEL recovery: {e}")

        with allure.step(f"gNMI ON_CHANGE — false→true→false transition captured for {sensor}"):
            # PNE + gNMI ONCE recovery gates above must pass before close so the
            # falling edge has time to land on the ON_CHANGE stream.
            if on_change_proc is None:
                if gnmi_client is not None:
                    errors.append("gNMI ON_CHANGE: subscription was not opened")
            else:
                try:
                    on_change_output = close_gnmi_on_change_subscription(on_change_proc)
                    on_change_proc = None
                    assert_gnmi_on_change_leak_transition(on_change_output, sensor)
                except AssertionError as e:
                    errors.append(f"gNMI ON_CHANGE: {e}")

        assert not errors, "Surface check failures:\n" + "\n".join(errors)

    finally:
        if on_change_proc is not None:
            try:
                close_gnmi_on_change_subscription(on_change_proc)
            except Exception as e:
                logger.warning("gNMI ON_CHANGE cleanup failed: %s", e)
            on_change_proc = None


@pytest.mark.cumulus
@pytest.mark.platform
@pytest.mark.disable_loganalyzer
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_leakage_core3_multiple_leaks(
    engines, nv_command, leakage_sensors, bmc_cpld_shell, bmc_version,
    otel_collector, leakage_gnmi_enabled, leakage_recovery_guard,
):
    """Core #3: both sensors leak simultaneously — all surfaces agree.

    leakage_aggr is NOT injected or asserted: CPLD simulation does not reliably
    drive the aggregate bit under test conditions (would cause spurious fails).
    Aggregate coverage is deferred until sim/product behaviour is reliable.
    """
    if len(leakage_sensors) < 2:
        pytest.skip("Need at least 2 leak sensors for multi-leak test")

    # Open ON_CHANGE and wait for initial sync (false) before inject (sensor[0]
    # representative). One sensor is enough — ONCE covers all; ON_CHANGE covers events.
    errors = []
    gnmi_client = create_leakage_gnmi_client(engines)
    on_change_proc = None
    if gnmi_client is None:
        errors.append(GNMI_SETUP_ERR_GNMIC_MISSING)
    else:
        on_change_proc = open_gnmi_on_change_subscription(engines, leakage_sensors[0])
        try:
            wait_for_gnmi_on_change_initial_status(on_change_proc, GNMI_ALARM_STATUS_OK)
        except AssertionError as e:
            errors.append(f"gNMI ON_CHANGE initial sync: {e}")
            close_gnmi_on_change_subscription(on_change_proc)
            on_change_proc = None

    try:
        with allure.step("Inject both sensors via BMC CPLD"):
            # Bound health-history asserts to this inject/clear cycle (not stale DUT logs).
            # Boundary comes from the DUT clock — same domain as the history stamps.
            history_since = read_health_history_since(engines.dut)
            inject_leak_via_bmc_cpld(bmc_cpld_shell.shell, BMC_CPLD_LEAK_SENSOR1_AND_2)

        with allure.step("Gate: wait for leak propagation to NVUE"):
            try:
                wait_for_nvue_leakage_states(
                    nv_command, leakage_sensors, LEAKAGE_NVUE_STATE_ALERT
                )
            except AssertionError as e:
                errors.append(f"Propagation gate (NVUE): {e}")

        with allure.step("System health not OK, LED amber — all leaking sensors in issues"):
            try:
                _validate_health(nv_command, NOT_OK, expected_led=HealthConsts.LED_NOT_OK_STATUS)
                health = OutputParsingTool.parse_json_str_to_dictionary(
                    nv_command.system.health.show()
                ).get_returned_value()
                issues = health.get(HealthConsts.ISSUES, {})
                for s in leakage_sensors:
                    assert s in issues, (
                        f"Expected {s!r} in health issues (JSON key is lowercase), got: {list(issues)}"
                    )
                    assert issues[s].get("issue") == HEALTH_ISSUE_LEAKAGE_ALERT, (
                        f"Expected health issue value {HEALTH_ISSUE_LEAKAGE_ALERT!r} for {s!r}, got: {issues[s]}"
                    )
            except AssertionError as e:
                errors.append(f"System health: {e}")

        with allure.step("NVUE leakage — all sensors alert"):
            try:
                nvue = read_nvue_leakage(nv_command)
                assert len(nvue) == len(leakage_sensors), (
                    f"NVUE: expected {len(leakage_sensors)} sensors, got {len(nvue)}: {sorted(nvue)}"
                )
                assert_leakage_nvue_rest_states(nvue, leakage_sensors, LEAKAGE_NVUE_STATE_ALERT)
            except AssertionError as e:
                errors.append(f"NVUE: {e}")

        with allure.step("smonctl leakage — all sensors leaking (0)"):
            try:
                smon = read_smonctl_leakage(engines.dut)
                logger.info("smonctl leakage: %s", smon)
                assert len(smon) == len(leakage_sensors), (
                    f"smonctl: expected {len(leakage_sensors)} sensors, got {len(smon)}: {[i['name'] for i in smon]}"
                )
                assert_smonctl_leakage_statuses(smon, leakage_sensors, SMONCTL_STATUS_LEAK)
            except AssertionError as e:
                errors.append(f"smonctl: {e}")

        with allure.step("REST leakage — all sensors alert"):
            try:
                rest = read_rest_leakage(engines.dut)
                assert len(rest) == len(leakage_sensors), (
                    f"REST: expected {len(leakage_sensors)} sensors, got {len(rest)}: {sorted(rest)}"
                )
                assert_leakage_nvue_rest_states(rest, leakage_sensors, LEAKAGE_NVUE_STATE_ALERT)
            except AssertionError as e:
                errors.append(f"REST: {e}")

        with allure.step("PNE telemetry leakage — all sensor metrics = 0 (LEAK_DETECTED)"):
            try:
                wait_for_pne_leakage_statuses(
                    engines.dut, leakage_sensors, PNE_TELEMETRY_STATUS_LEAK
                )
            except AssertionError as e:
                errors.append(f"PNE telemetry: {e}")

        with allure.step("gNMI ONCE (exact xpath) — all sensors alarm-status=true (LEAK)"):
            try:
                if gnmi_client is not None:
                    assert_gnmi_leakage_statuses(gnmi_client, leakage_sensors, GNMI_ALARM_STATUS_LEAK)
            except AssertionError as e:
                errors.append(f"gNMI exact xpath: {e}")

        with allure.step("gNMI ONCE (component xpath) — alarm-status=true in state/ subtree"):
            try:
                if gnmi_client is not None:
                    assert_gnmi_component_alarm_statuses(
                        gnmi_client, leakage_sensors, GNMI_ALARM_STATUS_LEAK
                    )
            except AssertionError as e:
                errors.append(f"gNMI component xpath: {e}")

        with allure.step("gNMI ONCE (oper-status) — all sensors ACTIVE"):
            try:
                if gnmi_client is not None:
                    assert_gnmi_oper_statuses(gnmi_client, leakage_sensors, GNMI_OPER_STATUS_ACTIVE)
            except AssertionError as e:
                errors.append(f"gNMI oper-status: {e}")

        with allure.step("OTEL — all sensors LEAK_DETECTED (0.0)"):
            try:
                # wait_for_otel_leakage_value confirms both presence and value for all sensors.
                wait_for_otel_leakage_value(otel_collector, leakage_sensors, OTEL_LEAKAGE_STATUS_LEAK)
            except AssertionError as e:
                errors.append(f"OTEL: {e}")

        with allure.step("BMC sysfs leakage files — all sensors = 0 (leak)"):
            try:
                assert_bmc_hwmgmt_leakage_values(bmc_cpld_shell.shell, leakage_sensors, BMC_HWMGMT_LEAKAGE_LEAK)
                # leakage_aggr not asserted: CPLD simulation does not reliably drive the aggregate bit
            except AssertionError as e:
                errors.append(f"BMC sysfs: {e}")

        with allure.step("BMC-CLI LeakDetection — all sensors Critical"):
            # NOTE: DetectorState always; Status.Health presence-gated (RM #5171996).
            try:
                bmc_leak = read_bmc_leak_detection_json(engines.dut)
                logger.info("BMC LeakDetection: %s", bmc_leak)
                assert_bmc_leak_detection_states(
                    bmc_leak,
                    leakage_sensors,
                    leaking_sensors=leakage_sensors,
                    bmc_version=bmc_version,
                )
            except AssertionError as e:
                errors.append(f"BMC-CLI: {e}")

        with allure.step("Health history — each sensor OK→BAD since this inject"):
            try:
                for sensor in leakage_sensors:
                    nv_command.system.health.history.search_line_by_date(
                        lines_to_search=f"{sensor} changed from OK to BAD",
                        since_date=history_since,
                    )
            except AssertionError as e:
                errors.append(f"Health history: {e}")

        with allure.step("Clear CPLD and verify full recovery"):
            try:
                clear_and_verify_cpld(bmc_cpld_shell.shell)
                wait_for_nvue_leakage_states(
                    nv_command, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL
                )
                _validate_health(nv_command, OK, expected_led=HealthConsts.LED_OK_STATUS)
                time.sleep(HEALTH_HISTORY_SETTLE_SEC)
                for sensor in leakage_sensors:
                    nv_command.system.health.history.search_line_by_date(
                        lines_to_search=f"{sensor} changed from BAD to OK",
                        since_date=history_since,
                    )
            except AssertionError as e:
                errors.append(f"Recovery (CPLD clear / health / history): {e}")

        with allure.step("Recovery gate — PNE all OK (1) after CPLD clear"):
            # Live PNE settle before OTEL/ON_CHANGE: NVUE can be normal while UMF still
            # lags. Polling :9100 for metric=1 proves the scrape path recovered; instant
            # assert_pne_* / wait_for_pne_metric_available (presence-only) are not enough.
            try:
                wait_for_pne_leakage_statuses(
                    engines.dut, leakage_sensors, PNE_TELEMETRY_STATUS_OK
                )
            except AssertionError as e:
                errors.append(f"Recovery gate (PNE): {e}")

        with allure.step(f"Recovery gate — gNMI alarm-status=false for {leakage_sensors[0]}"):
            try:
                if gnmi_client is not None:
                    wait_for_gnmi_leakage_status(
                        gnmi_client, leakage_sensors[0], GNMI_ALARM_STATUS_OK
                    )
                    assert_gnmi_leakage_statuses(
                        gnmi_client, leakage_sensors, GNMI_ALARM_STATUS_OK
                    )
            except AssertionError as e:
                errors.append(f"Recovery gate (gNMI): {e}")

        with allure.step("OTEL recovery — all sensors OK (1.0)"):
            try:
                wait_for_otel_leakage_value(
                    otel_collector,
                    leakage_sensors,
                    OTEL_LEAKAGE_STATUS_OK,
                    dut=engines.dut,
                    collector_ip=otel_collector.ip,
                )
            except AssertionError as e:
                errors.append(f"OTEL recovery: {e}")

        with allure.step(f"gNMI ON_CHANGE — false→true→false transition captured for {leakage_sensors[0]}"):
            # PNE + gNMI ONCE recovery gates above must pass before close so the
            # falling edge has time to land on the ON_CHANGE stream.
            if on_change_proc is None:
                if gnmi_client is not None:
                    errors.append("gNMI ON_CHANGE: subscription was not opened")
            else:
                try:
                    on_change_output = close_gnmi_on_change_subscription(on_change_proc)
                    on_change_proc = None
                    assert_gnmi_on_change_leak_transition(on_change_output, leakage_sensors[0])
                except AssertionError as e:
                    errors.append(f"gNMI ON_CHANGE: {e}")

        assert not errors, "Surface check failures:\n" + "\n".join(errors)

    finally:
        if on_change_proc is not None:
            try:
                close_gnmi_on_change_subscription(on_change_proc)
            except Exception as e:
                logger.warning("gNMI ON_CHANGE cleanup failed: %s", e)
            on_change_proc = None


@pytest.mark.cumulus
@pytest.mark.platform
@pytest.mark.disable_loganalyzer
@pytest.mark.timeout(20 * MINUTE, func_only=True)
def test_leakage_core4_sysfs_fault_paths(
    engines, nv_command, leakage_sensors, bmc_cpld_shell, otel_collector, leakage_recovery_guard,
):
    """Core #4: missing / invalid / corrupt NOS sysfs — never crash, never stale OK.

    Faults the NOS hw-mgmt leakage path only (no BMC CPLD injection). CPLD stays
    clear via ``bmc_cpld_shell``. One sensor (``leakage_sensors[0]``) is enough:
    sensors are symmetric.

    For each fault case, all CL surfaces must return and must not report the faulted
    sensor as OK/normal. Independently, BMC hw-mgmt sysfs for that sensor must remain
    OK — proving NOS-side corruption does not poison the BMC's CPLD-backed view.

    Cases:
      - missing file
      - invalid content values (SYSFS_FAULT_VALUES)
      - structural corruption (unreadable chmod 000 / directory / binary garbage)
    """
    hwmon_folder = get_hwmon_leakage_folder(engines.dut)
    sensor = leakage_sensors[0]
    logger.info("Core #4 faulting sensor: %s (of %s)", sensor, leakage_sensors)
    errors = []

    try:
        with allure.step(f"Missing leakage file — fault {sensor}"):
            remove_sysfs_leakage_file(engines.dut, sensor)
            try:
                wait_for_sysfs_leakage_missing_settled(engines.dut, sensor)
            except AssertionError as e:
                errors.append(f"[missing_file] settle: {e}")
            _collect_sysfs_fault_surface_errors(
                engines, nv_command, sensor, label="missing_file", errors=errors,
                bmc_engine=bmc_cpld_shell.shell, sibling_sensors=leakage_sensors,
            )

        for label, value in SYSFS_FAULT_VALUES:
            with allure.step(f"Sysfs fault value {label!r}={value!r} — fault {sensor}"):
                restore_sysfs_leakage_links(engines.dut, [sensor], hwmon_folder)
                inject_leak_via_sysfs(engines.dut, sensor, value)
                time.sleep(SYSFS_FAULT_SETTLE_SEC)
                _collect_sysfs_fault_surface_errors(
                    engines, nv_command, sensor, label=label, errors=errors,
                    bmc_engine=bmc_cpld_shell.shell, sibling_sensors=leakage_sensors,
                )

        for label, kind in SYSFS_FAULT_CORRUPTIONS:
            with allure.step(f"Sysfs structural corruption {label!r} — fault {sensor}"):
                restore_sysfs_leakage_links(engines.dut, [sensor], hwmon_folder)
                apply_sysfs_fault_corruption(engines.dut, sensor, kind)
                time.sleep(SYSFS_FAULT_SETTLE_SEC)
                _collect_sysfs_fault_surface_errors(
                    engines, nv_command, sensor, label=label, errors=errors,
                    bmc_engine=bmc_cpld_shell.shell, sibling_sensors=leakage_sensors,
                )
    finally:
        with allure.step("Restore sysfs leakage links"):
            restore_sysfs_leakage_links(engines.dut, leakage_sensors, hwmon_folder)

    with allure.step("Recovery — all sensors normal after sysfs restore"):
        try:
            wait_for_nvue_leakage_states(
                nv_command, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL
            )
            nvue = read_nvue_leakage(nv_command)
            logger.info("Recovery NVUE: %s", nvue)
            # Doubly needs the propagation budget that is now the default: the faults above
            # break the sysfs path itself, so the amber LED here reflects the platform
            # monitor's own read-failure bookkeeping rather than the leakage leaf that
            # wait_for_nvue_leakage_states just gated on, and clearing that costs up to a
            # full monitor cycle. 10s passed on HW 2026-09-08 15:45 and failed at 20:08 on
            # the same DUT purely on where the cycle landed.
            _validate_health(nv_command, OK, expected_led=HealthConsts.LED_OK_STATUS)
            health = OutputParsingTool.parse_json_str_to_dictionary(
                nv_command.system.health.show()
            ).get_returned_value()
            logger.info("Recovery system health: %s", health)
            assert_bmc_hwmgmt_leakage_values(
                bmc_cpld_shell.shell, leakage_sensors, BMC_HWMGMT_LEAKAGE_OK
            )
        except AssertionError as e:
            errors.append(f"Recovery: {e}")

    with allure.step("OTEL recovery — all sensors OK (1.0) after sysfs restore"):
        # Under-fault gNMI/OTEL omitted: many fault cases → too expensive vs 60s OTEL
        # cadence and ~2s fault windows; reader-fault for both is covered by SN 5.
        # PNE remains the under-fault telemetry gate; OTEL is recovery-only here.
        try:
            wait_for_otel_leakage_value(otel_collector, leakage_sensors, OTEL_LEAKAGE_STATUS_OK)
        except AssertionError as e:
            errors.append(f"OTEL recovery: {e}")

    assert not errors, "Surface check failures:\n" + "\n".join(errors)


# =============================================================================
# SN 5 — smonctl stub while sysfs still tracks CPLD
# =============================================================================

@pytest.mark.cumulus
@pytest.mark.platform
@pytest.mark.disable_loganalyzer
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_leakage_sn5_smonctl_fault_while_sysfs_updates(
    engines, nv_command, leakage_sensors, bmc_cpld_shell, otel_collector, leakage_gnmi_enabled,
    leakage_recovery_guard,
):
    """SN 5: smonctl exit-1 stub; sysfs still follows CPLD; upper layers fail-closed.

    Owner (Jay): temporary overwrite is fine for a short window; NVUE/health will
    report failure — do **not** assert global health OK under stub. Hard invariant:
    never stale OK (NVUE/REST ``normal``, PNE ``1``, gNMI ``false``, OTEL ``1.0``)
    while sysfs shows leak. HW 2026-08-04: NVUE/REST ``{}``, PNE metric absent,
    gNMI empty; smonctl path ``/usr/sbin/smonctl``.
    """
    sensor = leakage_sensors[0]
    sensor_index = leakage_sensors.index(sensor) + 1
    others = [s for s in leakage_sensors if s != sensor]
    errors = []

    gnmi_client = create_leakage_gnmi_client(engines)
    if gnmi_client is None:
        errors.append(GNMI_SETUP_ERR_GNMIC_MISSING)

    with allure.step("Baseline — surfaces alive before stub (NVUE/smonctl/REST/PNE/gNMI)"):
        clear_and_verify_cpld(bmc_cpld_shell.shell)
        wait_for_nvue_leakage_states(
            nv_command, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL
        )
        smon = read_smonctl_leakage(engines.dut)
        assert_smonctl_leakage_statuses(smon, leakage_sensors, SMONCTL_STATUS_NORMAL)
        rest = read_rest_leakage(engines.dut)
        assert_leakage_nvue_rest_states(rest, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL)
        metrics = read_pne_telemetry_leakage_metrics(engines.dut)
        assert_pne_telemetry_leakage_statuses(
            metrics, leakage_sensors, expected_status=PNE_TELEMETRY_STATUS_OK
        )
        if gnmi_client is not None:
            assert_gnmi_leakage_statuses(
                gnmi_client, leakage_sensors, GNMI_ALARM_STATUS_OK
            )

    with smonctl_stubbed(engines.dut):
        with allure.step(f"Inject CPLD leak on {sensor} while smonctl stubbed"):
            file_offset_at_fault = otel_collector_file_size(otel_collector)
            inject_single_sensor_bmc_cpld(bmc_cpld_shell.shell, sensor_index=sensor_index)

        with allure.step(f"sysfs — {sensor} still tracks CPLD (expect 0); siblings still 1"):
            try:
                wait_for_sysfs_leakage_value(engines.dut, sensor, "0")
                for sib in others:
                    wait_for_sysfs_leakage_value(
                        engines.dut, sib, "1", timeout=LEAKAGE_SURFACE_HOLD_TIMEOUT
                    )
            except AssertionError as e:
                errors.append(f"sysfs under stub: {e}")

        with allure.step("smonctl — stub fails (exit != 0)"):
            try:
                assert_smonctl_command_failed(engines.dut)
            except AssertionError as e:
                errors.append(f"smonctl stub: {e}")

        with allure.step("NVUE leakage — fail-closed (not normal; clean empty OK)"):
            try:
                # Do not map transport/parse exceptions to {} — that painted dead reads as pass.
                # Successful empty / missing sensor is acceptable fail-closed under stub.
                nvue = read_nvue_leakage(nv_command)
                assert isinstance(nvue, dict), (
                    f"NVUE under stub returned non-dict: {type(nvue).__name__}"
                )
                logger.info("NVUE under smonctl stub: %s", nvue)
                assert_sysfs_fault_nvue_rest_not_ok(nvue, sensor)
            except Exception as e:
                errors.append(f"NVUE under stub: {type(e).__name__}: {e}")

        with allure.step("REST leakage — fail-closed (not normal; clean empty OK)"):
            try:
                rest = read_rest_leakage(engines.dut)
                assert isinstance(rest, dict), (
                    f"REST under stub returned non-dict: {type(rest).__name__}"
                )
                logger.info("REST under smonctl stub: %s", rest)
                assert_sysfs_fault_nvue_rest_not_ok(rest, sensor)
            except Exception as e:
                errors.append(f"REST under stub: {type(e).__name__}: {e}")

        with allure.step(
            f"PNE metric — fail-closed after {SMONCTL_FAULT_PNE_SETTLE_SEC}s settle"
        ):
            try:
                time.sleep(SMONCTL_FAULT_PNE_SETTLE_SEC)
                metrics = read_pne_telemetry_leakage_metrics(engines.dut)
                leak_lines = [
                    line for line in str(metrics).splitlines()
                    if "leak_sensor_status" in line and not line.startswith("#")
                ]
                logger.info("PNE under smonctl stub lines: %s", leak_lines)
                assert_sysfs_fault_pne_not_ok(metrics, sensor)
            except Exception as e:
                errors.append(f"PNE under stub: {type(e).__name__}: {e}")

        with allure.step("gNMI — fail-closed (not stale alarm-status=false)"):
            try:
                if gnmi_client is not None:
                    assert_smonctl_fault_gnmi_not_ok(gnmi_client, sensor)
            except Exception as e:
                errors.append(f"gNMI under stub: {type(e).__name__}: {e}")

        with allure.step(
            f"OTEL — fail-closed after {SMONCTL_FAULT_OTEL_SETTLE_SEC}s settle"
        ):
            try:
                time.sleep(SMONCTL_FAULT_OTEL_SETTLE_SEC)
                assert_smonctl_fault_otel_not_ok(
                    otel_collector, sensor, file_offset_at_fault=file_offset_at_fault
                )
            except Exception as e:
                errors.append(f"OTEL under stub: {type(e).__name__}: {e}")

    # smonctl restored by context manager; clear CPLD and confirm recovery
    with allure.step("Clear CPLD and verify recovery after smonctl restore"):
        clear_and_verify_cpld(bmc_cpld_shell.shell)
        wait_for_nvue_leakage_states(
            nv_command, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL
        )
        smon = read_smonctl_leakage(engines.dut)
        assert_smonctl_leakage_statuses(smon, leakage_sensors, SMONCTL_STATUS_NORMAL)

    assert not errors, "SN 5 surface check failures:\n" + "\n".join(errors)


# =============================================================================
# SN 6 — service restart resilience
# (Core #2/#3 cover the full ON_CHANGE edge sequence; SN 6 checks only that a fresh
#  post-restart subscription still delivers the clear notification)
# =============================================================================

@pytest.mark.cumulus
@pytest.mark.platform
@pytest.mark.disable_loganalyzer
@pytest.mark.timeout(20 * MINUTE, func_only=True)
def test_leakage_service_restart_resilience(
    engines, nv_command, leakage_sensors, bmc_cpld_shell, otel_collector, leakage_gnmi_enabled,
    leakage_recovery_guard,
):
    """SN 6: leakage path survives pne / hw-mgmt / nv-umf service restarts.

    Phase A — pne restart: :9100 metric correct, then gNMI ONCE agrees.
    Phase B — hw-mgmt restart: sysfs (leak=0, others=1) + smonctl. No NVUE/PNE.
    Phase C — nv-umf restart: gNMI ONCE shows correct leak, then a subscription
              opened *after* the restart must receive the leak→clear notification
              (event path alive, not just polled reads). Light e2e smoke; does not
              replace telemetry-suite UMF lifecycle / reconnect coverage.
    Collect-all: phases reported independently.
    """
    sensor = leakage_sensors[0]
    sensor_index = leakage_sensors.index(sensor) + 1
    others = [s for s in leakage_sensors if s != sensor]
    errors = []

    gnmi_client = create_leakage_gnmi_client(engines)
    if gnmi_client is None:
        errors.append(GNMI_SETUP_ERR_GNMIC_MISSING)

    # ── Phase A: pne restart ─────────────────────────────────────────────────
    with allure.step("Phase A — pne restart"):
        with allure.step(f"Inject leak on {sensor} via BMC CPLD"):
            inject_single_sensor_bmc_cpld(bmc_cpld_shell.shell, sensor_index=sensor_index)
            wait_for_nvue_leakage_states(nv_command, [sensor], LEAKAGE_NVUE_STATE_ALERT)

        with allure.step(f"Restart {PNE_SERVICE_NAME}"):
            try:
                restart_service(engines.dut, PNE_SERVICE_NAME)
                wait_for_service_active(engines.dut, PNE_SERVICE_NAME)
                wait_for_pne_metric_available(engines.dut)
                metrics = read_pne_telemetry_leakage_metrics(engines.dut)
                assert_pne_telemetry_leakage_statuses(metrics, [sensor], PNE_TELEMETRY_STATUS_LEAK)
                assert_pne_telemetry_leakage_statuses(metrics, others, PNE_TELEMETRY_STATUS_OK)
                if gnmi_client is not None:
                    assert_gnmi_leakage_statuses(gnmi_client, [sensor], GNMI_ALARM_STATUS_LEAK)
                    assert_gnmi_leakage_statuses(gnmi_client, others, GNMI_ALARM_STATUS_OK)
                wait_for_otel_leakage_value(otel_collector, [sensor], OTEL_LEAKAGE_STATUS_LEAK)
                assert_otel_leakage_statuses(otel_collector, others, OTEL_LEAKAGE_STATUS_OK)
                logger.info("Phase A passed: pne metric + gNMI + OTEL correct after restart")
            except AssertionError as e:
                errors.append(f"Phase A (pne restart): {e}")

        with allure.step("Clear CPLD after Phase A"):
            clear_and_verify_cpld(bmc_cpld_shell.shell)
            wait_for_nvue_leakage_states(nv_command, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL)

    # ── Phase B: hw-mgmt restart ─────────────────────────────────────────────
    # Scope: hw-mgmt must repopulate sysfs from CPLD. Upper layers (NVUE/PNE/gNMI)
    # are Core #1–3; here we only check sysfs + smonctl (direct sysfs reader).
    with allure.step("Phase B — hw-mgmt restart"):
        with allure.step(f"Inject leak on {sensor} via BMC CPLD"):
            inject_single_sensor_bmc_cpld(bmc_cpld_shell.shell, sensor_index=sensor_index)
            wait_for_nvue_leakage_states(nv_command, [sensor], LEAKAGE_NVUE_STATE_ALERT)

        with allure.step(f"Restart {HWMGMT_SERVICE_NAME}"):
            try:
                restart_service(engines.dut, HWMGMT_SERVICE_NAME)
                wait_for_service_active(engines.dut, HWMGMT_SERVICE_NAME)
                wait_for_sysfs_leakage_value(engines.dut, sensor, "0")
                for other in others:
                    wait_for_sysfs_leakage_value(engines.dut, other, "1")
                smon = read_smonctl_leakage(engines.dut)
                assert_smonctl_leakage_statuses(smon, [sensor], SMONCTL_STATUS_LEAK)
                assert_smonctl_leakage_statuses(smon, others, SMONCTL_STATUS_NORMAL)
                logger.info(
                    "Phase B passed: sysfs + smonctl correct after hw-mgmt restart "
                    "(%s=0, others=1)", sensor,
                )
            except AssertionError as e:
                errors.append(f"Phase B (hw-mgmt restart): {e}")

        with allure.step("Clear CPLD after Phase B"):
            clear_and_verify_cpld(bmc_cpld_shell.shell)
            wait_for_nvue_leakage_states(nv_command, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL)

    # ── Phase C: nv-umf restart ──────────────────────────────────────────────
    # Light leakage e2e smoke: UMF is on the path to gNMI. Full UMF lifecycle /
    # multi-subscriber reconnect remains telemetry-suite scope.
    on_change_proc = None
    with allure.step("Phase C — nv-umf restart"):
        try:
            with allure.step(f"Inject leak on {sensor} via BMC CPLD"):
                inject_single_sensor_bmc_cpld(bmc_cpld_shell.shell, sensor_index=sensor_index)
                wait_for_nvue_leakage_states(nv_command, [sensor], LEAKAGE_NVUE_STATE_ALERT)

            with allure.step(f"Restart {UMF_SERVICE_NAME}"):
                try:
                    restart_service(engines.dut, UMF_SERVICE_NAME)
                    wait_for_service_active(engines.dut, UMF_SERVICE_NAME)
                    if gnmi_client is not None:
                        wait_for_gnmi_leakage_status(
                            gnmi_client, sensor, GNMI_ALARM_STATUS_LEAK
                        )
                        assert_gnmi_leakage_statuses(gnmi_client, others, GNMI_ALARM_STATUS_OK)
                    wait_for_otel_leakage_value(otel_collector, [sensor], OTEL_LEAKAGE_STATUS_LEAK)
                    assert_otel_leakage_statuses(otel_collector, others, OTEL_LEAKAGE_STATUS_OK)
                    logger.info("Phase C passed: gNMI + OTEL correct after nv-umf restart")
                except AssertionError as e:
                    errors.append(f"Phase C (nv-umf restart): {e}")

            # ONCE can recover while the event path stays dead, so subscribe *after* the
            # restart (leak still injected) and require the falling edge below.
            if gnmi_client is not None:
                with allure.step("Subscribe ON_CHANGE after restart (initial sync = leak)"):
                    try:
                        on_change_proc = open_gnmi_on_change_subscription(engines, sensor)
                        wait_for_gnmi_on_change_initial_status(
                            on_change_proc, GNMI_ALARM_STATUS_LEAK
                        )
                    except AssertionError as e:
                        errors.append(f"Phase C (ON_CHANGE initial sync): {e}")
                        if on_change_proc is not None:
                            close_gnmi_on_change_subscription(on_change_proc)
                            on_change_proc = None

            with allure.step("Clear CPLD after Phase C"):
                clear_and_verify_cpld(bmc_cpld_shell.shell)
                wait_for_nvue_leakage_states(
                    nv_command, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL
                )

            if on_change_proc is not None:
                with allure.step("ON_CHANGE — leak→clear notification after nv-umf restart"):
                    try:
                        on_change_output = close_gnmi_on_change_subscription(on_change_proc)
                        on_change_proc = None
                        assert_gnmi_on_change_clear_transition(on_change_output, sensor)
                    except AssertionError as e:
                        errors.append(f"Phase C (ON_CHANGE clear transition): {e}")
        finally:
            if on_change_proc is not None:
                try:
                    close_gnmi_on_change_subscription(on_change_proc)
                except Exception as e:
                    logger.warning("gNMI ON_CHANGE cleanup failed: %s", e)
                on_change_proc = None

    assert not errors, "Service restart failures:\n" + "\n".join(errors)


# =============================================================================
# SN 7 — reboot state persistence (soft / bmc / pwr_cycle / aux_pwr_cycle)
# =============================================================================

_SN7_AUX_PWR_CYCLE_SKIP_REASON = (
    "SN7 [aux_pwr_cycle]: ipmitool chassis power cycle is full-board AUX "
    "(BMC reboots). CPLD i2ctransfer leak-injection simulation clears across AUX "
    "(Guy Poel 2026-08-02 — expected; no way to keep sim data). Use [pwr_cycle] "
    "(PDB/host) for persist-across-host-power-cycle with this inject method."
)


@pytest.mark.cumulus
@pytest.mark.platform
@pytest.mark.disable_loganalyzer
@pytest.mark.timeout(45 * MINUTE, func_only=True)
# Order: soft → bmc → pwr_cycle (host/PDB; sim-valid persist).
# aux_pwr_cycle collected but skipped at collection (no fixture setup) — sim limitation.
@pytest.mark.parametrize(
    "reboot_type",
    [
        "soft",
        "bmc",
        "pwr_cycle",
        pytest.param(
            "aux_pwr_cycle",
            marks=pytest.mark.skip(reason=_SN7_AUX_PWR_CYCLE_SKIP_REASON),
        ),
    ],
)
def test_leakage_reboot_state_persistence(
    engines, nv_command, leakage_sensors, topology_obj,
    bmc_cpld_shell, reboot_type, otel_collector, leakage_gnmi_enabled,
    leakage_recovery_guard, request,
):
    """SN 7: leakage detection initializes correctly after reboot.

    Injects a CPLD leak before reboot. Clears CPLD and verifies recovery at the end.
    BMC presence / initial CPLD clear come from ``bmc_cpld_shell`` (same as Core #1–3).

    reboot_type='soft':
        NOS soft reboot (BMC stays up). Full NOS surfaces + BMC sysfs after return.
        Sim leak is expected to persist.

    reboot_type='pwr_cycle':
        Host/PDB cycle via BMC ``echo 1 > …/pwr_cycle`` (CPU/12V only; BMC+CPLD stay).
        Same post-NOS surface suite as soft. Sim leak is expected to persist (Guy Poel).

    reboot_type='aux_pwr_cycle':
        Skipped at collection (see ``_SN7_AUX_PWR_CYCLE_SKIP_REASON``). AUX clears CPLD
        sim; use ``pwr_cycle`` for persist-across-host-power-cycle with this inject.

    reboot_type='bmc':
        BMC-only ``reboot`` on OOB (NOS stays up). Scope is asymmetric on purpose:
        verify NOS independence (still up, still reporting leak) + BMC-side leakage
        after BMC returns. No full NOS surface suite — NOS was never rebooted.
        Post-BMC-reboot BMC sysfs uses settle/retry (hw-mgmt files empty until ready).
    """
    sensor = leakage_sensors[0]
    sensor_index = leakage_sensors.index(sensor) + 1
    others = [s for s in leakage_sensors if s != sensor]
    errors = []

    # Fresh handle may replace holder.shell after NOS/BMC reboot; fixture teardown
    # and leakage_recovery_guard always use holder.shell (must stay live).
    oob_ip = resolve_bmc_oob_ip(engines, topology_obj)
    holder = bmc_cpld_shell

    gnmi_client = create_leakage_gnmi_client(engines)
    if gnmi_client is None:
        errors.append(GNMI_SETUP_ERR_GNMIC_MISSING)

    with allure.step(f"Inject CPLD leak on {sensor} before reboot"):
        inject_single_sensor_bmc_cpld(holder.shell, sensor_index=sensor_index)
        wait_for_nvue_leakage_states(nv_command, [sensor], LEAKAGE_NVUE_STATE_ALERT)
        # BMC hw-mgmt sysfs = second source of truth (CPLD-backed). Must agree with NOS
        # before reboot so post-reboot BMC checks are meaningful.
        logger.info(
            "Pre-reboot BMC source of truth: %s=leak(%s), others=%s OK(%s)",
            sensor, BMC_HWMGMT_LEAKAGE_LEAK, others, BMC_HWMGMT_LEAKAGE_OK,
        )
        assert_bmc_hwmgmt_leakage_values(holder.shell, [sensor], BMC_HWMGMT_LEAKAGE_LEAK)
        assert_bmc_hwmgmt_leakage_values(holder.shell, others, BMC_HWMGMT_LEAKAGE_OK)

    if reboot_type == "bmc":
        with allure.step("BMC-only reboot (OOB reboot)"):
            before_bmc = read_bmc_boot_identity(holder.shell)
            reboot_started_at = time.monotonic()
            reboot_bmc_soft(holder.shell)
            try:
                wait_for_bmc_oob_ssh_down(oob_ip, bmc_engine=holder.shell)
            except Exception:
                # The down-wait closed the handle before polling, so failing here would
                # strand a closed session in the holder (e.g. a reboot the BMC ignored
                # and OOB SSH never dropped). Put a live shell back for cleanup, then
                # re-raise the original failure untouched.
                try:
                    holder.shell = wait_for_bmc_oob_ssh(oob_ip)
                except Exception as revive_err:
                    logger.warning(
                        "BMC OOB revive after down-wait failure did not connect (%s: %s)",
                        type(revive_err).__name__, revive_err,
                    )
                raise
            with allure.step("NOS independence — still up, still reporting leak"):
                try:
                    wait_for_nvue_leakage_states(
                        nv_command, [sensor], LEAKAGE_NVUE_STATE_ALERT
                    )
                except AssertionError as e:
                    errors.append(f"NOS independence during BMC reboot: {e}")
            with allure.step("Wait for BMC OOB SSH after BMC reboot"):
                holder.shell = wait_for_bmc_oob_ssh(oob_ip)
            try:
                holder.shell = assert_bmc_rebooted(
                    holder.shell,
                    before_bmc,
                    reboot_started_at=reboot_started_at,
                    oob_ip=oob_ip,
                )
            except AssertionError as e:
                errors.append(f"BMC reboot proof: {e}")

            with allure.step("BMC sysfs after BMC reboot — leak persisted (BMC-side only)"):
                # Same settle pattern as soft: OOB SSH can return before BMC hw-mgmt has
                # re-created leakage sysfs (empty '' read) — wait/retry. Pass oob_ip so a
                # hung/half-open post-reboot session is replaced instead of blocking.
                try:
                    holder.shell = wait_for_bmc_hwmgmt_leakage_values(
                        holder.shell,
                        [sensor],
                        BMC_HWMGMT_LEAKAGE_LEAK,
                        oob_ip=oob_ip,
                    )
                    holder.shell = wait_for_bmc_hwmgmt_leakage_values(
                        holder.shell,
                        others,
                        BMC_HWMGMT_LEAKAGE_OK,
                        oob_ip=oob_ip,
                    )
                except Exception as e:
                    errors.append(f"Post-BMC-reboot BMC sysfs: {type(e).__name__}: {e}")
    else:
        with allure.step(f"Reboot NOS ({reboot_type})"):
            before_boot = read_dut_boot_identity(engines.dut)
            reboot_started_at = time.monotonic()
            if reboot_type == "soft":
                reboot_dut_soft(engines.dut)
                wait_for_dut_ssh_down(
                    engines.dut, timeout=SOFT_REBOOT_SSH_DOWN_TIMEOUT
                )
                wait_for_dut_ssh(engines.dut)
            else:
                # pwr_cycle — Host/PDB: CPU cold boot; BMC stays up (sim expected to persist).
                reboot_dut_pdb_power_cycle(holder.shell)
                wait_for_dut_ssh_down(
                    engines.dut, timeout=PDB_REBOOT_SSH_DOWN_TIMEOUT
                )
                wait_for_dut_ssh(engines.dut, timeout=PDB_POWER_CYCLE_SSH_TIMEOUT)
            assert_dut_rebooted(
                engines.dut, before_boot, reboot_started_at=reboot_started_at
            )
            # NVUE/gNMI/OTEL config does not survive NOS reboot; autouse fixture
            # already ran at test start and will not re-run mid-test. pwr_cycle
            # needs a longer remote-listen wait (docker/nv-gnmi after host cold boot).
            # Track restore so fixture teardown undoes any post-reboot 0.0.0.0 widen.
            if reboot_type == "pwr_cycle":
                post_gnmi = ensure_gnmi_server_ready(
                    nv_command, engines, timeout=PDB_GNMI_PORT_READY_TIMEOUT
                )
            else:
                post_gnmi = ensure_gnmi_server_ready(nv_command, engines)
            getattr(request.node, "_leakage_gnmi_listen_restores", []).append(post_gnmi)
            gnmi_client = create_leakage_gnmi_client(engines)
            if gnmi_client is None:
                errors.append(GNMI_SETUP_ERR_GNMIC_MISSING)
            apply_otel_leakage_dut_export(engines.dut, otel_collector.ip)

        with allure.step("Assert leakage still reported after NOS reboot — CPLD persisted"):
            with allure.step("Gate: wait for NVUE leakage propagation after reboot"):
                try:
                    wait_for_nvue_leakage_states(
                        nv_command, [sensor], LEAKAGE_NVUE_STATE_ALERT, timeout=LEAKAGE_SURFACE_REBOOT_TIMEOUT
                    )
                except AssertionError as e:
                    errors.append(f"Post-reboot propagation gate (NVUE): {e}")

            with allure.step("System health not OK, LED amber — leaking sensor in issues"):
                try:
                    nv_command.system.health.wait_until_health_status_change_after_reboot(
                        NOT_OK, expected_led=HealthConsts.LED_NOT_OK_STATUS
                    )
                    health = OutputParsingTool.parse_json_str_to_dictionary(
                        nv_command.system.health.show()
                    ).get_returned_value()
                    issues = health.get(HealthConsts.ISSUES, {})
                    assert sensor in issues, (
                        f"Expected {sensor!r} in health issues (JSON key is lowercase), got: {list(issues)}"
                    )
                    assert issues[sensor].get("issue") == HEALTH_ISSUE_LEAKAGE_ALERT, (
                        f"Expected health issue value {HEALTH_ISSUE_LEAKAGE_ALERT!r} for {sensor!r}, "
                        f"got: {issues[sensor]}"
                    )
                except AssertionError as e:
                    errors.append(f"Post-reboot system health: {e}")

            with allure.step("NVUE leakage — leaking sensor alert"):
                try:
                    nvue = read_nvue_leakage(nv_command)
                    assert_leakage_nvue_rest_states(nvue, [sensor], LEAKAGE_NVUE_STATE_ALERT)
                    assert_leakage_nvue_rest_states(nvue, others, LEAKAGE_NVUE_STATE_NORMAL)
                except AssertionError as e:
                    errors.append(f"Post-reboot NVUE: {e}")

            with allure.step("REST leakage — leaking sensor alert, others normal"):
                try:
                    rest = read_rest_leakage(engines.dut)
                    assert_leakage_nvue_rest_states(rest, [sensor], LEAKAGE_NVUE_STATE_ALERT)
                    assert_leakage_nvue_rest_states(rest, others, LEAKAGE_NVUE_STATE_NORMAL)
                except AssertionError as e:
                    errors.append(f"Post-reboot REST: {e}")

            with allure.step("smonctl leakage — leaking sensor 0"):
                try:
                    smon = read_smonctl_leakage(engines.dut)
                    assert_smonctl_leakage_statuses(smon, [sensor], SMONCTL_STATUS_LEAK)
                    assert_smonctl_leakage_statuses(smon, others, SMONCTL_STATUS_NORMAL)
                except AssertionError as e:
                    errors.append(f"Post-reboot smonctl: {e}")

            with allure.step("PNE metric — leaking sensor 0"):
                try:
                    wait_for_pne_metric_available(engines.dut)
                    metrics = read_pne_telemetry_leakage_metrics(engines.dut)
                    assert_pne_telemetry_leakage_statuses(
                        metrics, [sensor], PNE_TELEMETRY_STATUS_LEAK
                    )
                    assert_pne_telemetry_leakage_statuses(
                        metrics, others, PNE_TELEMETRY_STATUS_OK
                    )
                except AssertionError as e:
                    errors.append(f"Post-reboot PNE metric: {e}")

            with allure.step("gNMI — leaking sensor alarm-status=true"):
                if gnmi_client is not None:
                    try:
                        assert_gnmi_leakage_statuses(
                            gnmi_client, [sensor], GNMI_ALARM_STATUS_LEAK
                        )
                        assert_gnmi_leakage_statuses(
                            gnmi_client, others, GNMI_ALARM_STATUS_OK
                        )
                    except AssertionError as e:
                        errors.append(f"Post-reboot gNMI: {e}")

            with allure.step("OTEL — post-reboot leaking sensor 0.0, others 1.0"):
                try:
                    wait_for_otel_leakage_value(
                        otel_collector, [sensor], OTEL_LEAKAGE_STATUS_LEAK
                    )
                    assert_otel_leakage_statuses(otel_collector, others, OTEL_LEAKAGE_STATUS_OK)
                except AssertionError as e:
                    errors.append(f"Post-reboot OTEL: {e}")

            with allure.step(
                "BMC sysfs — second source of truth (must match NOS: leak=0, others=1)"
            ):
                try:
                    close_bmc_oob_shell(holder.shell)
                    holder.shell = open_bmc_oob_shell(oob_ip)
                    holder.shell = wait_for_bmc_hwmgmt_leakage_values(
                        holder.shell,
                        [sensor],
                        BMC_HWMGMT_LEAKAGE_LEAK,
                        oob_ip=oob_ip,
                    )
                    holder.shell = wait_for_bmc_hwmgmt_leakage_values(
                        holder.shell,
                        others,
                        BMC_HWMGMT_LEAKAGE_OK,
                        oob_ip=oob_ip,
                    )
                except Exception as e:
                    errors.append(f"Post-reboot BMC sysfs: {type(e).__name__}: {e}")

    with allure.step("Clear CPLD and verify full recovery"):
        # Reviving clear: this runs after BMC/NOS reboots, so the handle here is the most
        # likely one in the suite to be dead or half-open.
        holder.shell = clear_and_verify_cpld_with_revive(holder.shell, oob_ip)
        wait_for_nvue_leakage_states(nv_command, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL)
        try:
            # After NOS soft/PDB reboot, health/LED need the long post-reboot wait.
            # BMC-only path also lands here; first poll usually succeeds immediately.
            nv_command.system.health.wait_until_health_status_change_after_reboot(
                OK, expected_led=HealthConsts.LED_OK_STATUS
            )
            assert_bmc_hwmgmt_leakage_values(
                holder.shell, leakage_sensors, BMC_HWMGMT_LEAKAGE_OK
            )
        except AssertionError as e:
            errors.append(f"Post-reboot recovery health: {e}")

    with allure.step("OTEL recovery — all sensors OK (1.0) after CPLD clear"):
        try:
            wait_for_otel_leakage_value(otel_collector, leakage_sensors, OTEL_LEAKAGE_STATUS_OK)
        except AssertionError as e:
            errors.append(f"Post-reboot OTEL recovery: {e}")

    assert not errors, "Post-reboot surface failures:\n" + "\n".join(errors)


# =============================================================================
# SN 8 — cl-support captures leak status
# =============================================================================

@pytest.mark.cumulus
@pytest.mark.platform
@pytest.mark.disable_loganalyzer
@pytest.mark.timeout(25 * MINUTE, func_only=True)
def test_leakage_cl_support_captures_leak_status(
    engines, nv_command, leakage_sensors, bmc_cpld_shell, tmp_path,
):
    """SN 8: cl-support archive contains leak evidence while a sensor is in alert.

    Inject via BMC CPLD, run ``sudo cl-support``, SCP the ``.txz`` to pytest
    ``tmp_path`` (ephemeral on the runner — not the git tree), assert both
    ``Support/smonctl`` (LEAK DETECTED) and ``Support/bmc_complete_dump.json.gz``
    (DetectorState Critical / Leak detected at <sensor>) from the **local** copy
    (avoids DUT wall-broadcast pollution of SSH stdout), then clear CPLD and
    delete DUT + local archives.
    """
    leaking_sensor = leakage_sensors[0]
    sensor_index = leakage_sensors.index(leaking_sensor) + 1
    archive_path = None
    local_archive = None

    try:
        with allure.step(f"Inject leak on {leaking_sensor} via BMC CPLD"):
            inject_single_sensor_bmc_cpld(bmc_cpld_shell.shell, sensor_index=sensor_index)
            wait_for_nvue_leakage_states(
                nv_command, [leaking_sensor], LEAKAGE_NVUE_STATE_ALERT
            )

        with allure.step("Run cl-support while leak is active"):
            output = run_cl_support(engines.dut)
            archive_path = extract_cl_support_archive_path(output)
            # Log path only — do not dump cl-support stdout / archive into pytest logs.
            logger.info("cl-support archive on DUT: %s", archive_path)
            local_archive = save_cl_support_archive_aside(
                engines.dut,
                archive_path,
                sensor_name=leaking_sensor,
                dest_dir=str(tmp_path),
            )
            logger.info("cl-support archive saved aside: %s", local_archive)
            allure.attach(
                "cl-support local aside path",
                local_archive,
            )

        with allure.step(
            f"Verify Support/smonctl + Support/bmc_complete_dump for {leaking_sensor}"
        ):
            hits = assert_cl_support_leak_evidence(
                engines.dut,
                archive_path,
                sensor_name=leaking_sensor,
                local_archive_path=local_archive,
            )
            logger.info("cl-support leak evidence: %s", hits)

    finally:
        if archive_path:
            try:
                cleanup_cl_support_archive(engines.dut, archive_path)
            except Exception as e:
                logger.error("cl-support DUT archive cleanup failed: %s", e)
        if local_archive:
            try:
                os.remove(local_archive)
                logger.info("Removed local cl-support aside: %s", local_archive)
            except OSError as e:
                logger.error("cl-support local aside cleanup failed: %s", e)
        try:
            bmc_cpld_shell.shell = clear_and_verify_cpld_with_revive(
                bmc_cpld_shell.shell, bmc_cpld_shell.oob_ip
            )
            wait_for_nvue_leakage_states(
                nv_command, leakage_sensors, LEAKAGE_NVUE_STATE_NORMAL
            )
        except Exception as e:
            mark_cpld_clear_failed()
            raise AssertionError(f"SN8 CPLD/NVUE cleanup failed: {e}") from e
