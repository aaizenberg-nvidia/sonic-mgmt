"""
BMC-initiated reset of the main NOS (host CPU).

The BMC is the initiator and the NOS / host CPU is the target. This is the
inverse of NOS-side reboot coverage (NVUE reboot / power-cycle tests).

Coverage in this module:
  * ``bmc-cli reset`` (usb0) — ``cpu-reset`` / power-cycle variants
  * OOB Redfish ``ComputerSystem.Reset`` (SN49) — ``GracefulRestart`` /
    ``ForceRestart``. Stays HTTPS; do not retarget ``CurlTool`` for usb0 HTTP.

Safety: the host/NOS is reset, not the BMC (inventory ``ok``, firmware unchanged).
``bmc-cli reset bmc-reset`` is covered elsewhere (destructive BMC reset suite).
"""
import logging
import time

import pytest
from netmiko.exceptions import NetmikoTimeoutException
try:
    from netmiko.exceptions import ReadTimeout as NetmikoReadTimeout
except ImportError:
    NetmikoReadTimeout = NetmikoTimeoutException

from ngts.nvos_constants.constants_nvos import FansConsts
from ngts.nvos_tools.infra.BmcCliTool import BmcCliTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.tests_nvos.platform.bmc_helpers import (
    ensure_usb0_up,
    make_oob_curl_tool,
    nos_uptime_seconds,
    oob_driver_engine,
    redfish_action_accepted,
    reset_nos_via_redfish,
    resolve_redfish_system_id,
    skip_if_no_bmc,
    snapshot_bmc_state_safe,
    wait_for_nos_reboot,
    wait_for_redfish_power_state,
)
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.switch_recovery import recover_dut_with_remote_reboot

cumulus_owner = "gpazbenitzha"
logger = logging.getLogger()
HARD_RESET_SETTLE_SECONDS = 60
POWER_OFF_TIMEOUT = 180
POWER_OFF_ON_SETTLE_SECONDS = 60
RECONNECT_TRIES = 18
RECONNECT_DELAY = 10
_DUT_STRANDED = {"reason": None}
ETH0_ISSUE_MARKER = "BMC_RESET_ETH0_CHECK"


def _tcp_open(host, port=22, timeout=5):
    """Bounded, pure-socket TCP connect check.

    Never enters netmiko/paramiko, so it CANNOT get stuck in the endless
    ``read_channel`` spin that a dead SSH transport triggers. Returns True/False.
    """
    import socket
    if not host:
        return True  # unknown IP -> don't gate on TCP, fall through to reconnect
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _log_eth0_reachability(engines, context, tries=3, delay=5):
    """Emit a greppable marker for whether the DUT mgmt/eth0 is reachable.

    Pure bounded TCP :22 probe (never enters netmiko, so it cannot hang). Logs:
      [BMC_RESET_ETH0_CHECK] OK (<context>) ...          -> eth0/mgmt reachable
      [BMC_RESET_ETH0_CHECK] REPRODUCED (<context>) ...  -> eth0/mgmt NOT reachable

    Diagnostic only; returns the bool, never raises. Grep the log for
    'BMC_RESET_ETH0_CHECK' to see, per reset, whether the reset-recovery issue
    (NOS/eth0 unreachable after a BMC reset) happened again.
    """
    host = getattr(engines.dut, 'ip', None)
    reachable = False
    for attempt in range(1, tries + 1):
        if _tcp_open(host, 22, timeout=5):
            reachable = True
            break
        logger.info(f"[{ETH0_ISSUE_MARKER}] probe {attempt}/{tries} ({context}): "
                    "TCP {} port 22 closed".format(host))
        if attempt < tries:
            time.sleep(delay)
    if reachable:
        logger.info(f"[{ETH0_ISSUE_MARKER}] OK ({context}): DUT mgmt/eth0 reachable at "
                    "{} port 22".format(host))
    else:
        logger.error(f"[{ETH0_ISSUE_MARKER}] REPRODUCED ({context}): DUT mgmt/eth0 NOT "
                     "reachable at {} port 22 — post-reset recovery issue happened again"
                     .format(host))
    return reachable


def _quick_dut_reachable(engines, tries=3, delay=5):
    """Bounded reachability probe that CANNOT hang on a dead netmiko transport.

    A stale SSH session whose transport has EOF'd makes netmiko spin forever in
    ``read_channel`` (empty reads, no timeout) — the exact hang that stranded
    earlier runs. So we must NOT probe by running a command on the existing
    engine. Instead, for each attempt:
      1. bounded TCP connect to :22 (pure socket, never enters netmiko);
      2. only if the port is open, force a fresh disconnect + short reconnect
         (a NEW transport — it can't inherit the dead, spinning one) and confirm
         the NOS answers 'hostname'.
    Returns True/False, never raises, bounded to a handful of seconds per attempt.
    """
    host = getattr(engines.dut, 'ip', None)
    for attempt in range(1, tries + 1):
        if not _tcp_open(host, 22, timeout=5):
            logger.info(f"DUT reachability probe {attempt}/{tries}: TCP port 22 closed on {host}")
        else:
            try:
                # Drop any stale/half-dead session so run_cmd can't spin on it,
                # then reconnect fresh (bounded) before touching the shell.
                try:
                    engines.dut.disconnect()
                except Exception:
                    pass
                DutUtilsTool.reconnect_engine(engines.dut, find_prompt_tries=3,
                                              find_prompt_delay=3)
                out = engines.dut.run_cmd('hostname', validate=False)
                if out and out.strip():
                    return True
            except Exception as err:
                logger.info(f"DUT reachability probe {attempt}/{tries} failed: "
                            f"{type(err).__name__}: {err}")
        if attempt < tries:
            time.sleep(delay)
    return False


def _hostname_is_nvue_default(name):
    """True if live hostname is CL's unset default (pytest clear_config leftover)."""
    leaf = (name or "").strip().split(".")[0].lower()
    return leaf == "cumulus"


def _startup_yaml_size(engines):
    """On-disk /etc/nvue.d/startup.yaml size in bytes, or None if unreadable."""
    try:
        out = engines.dut.run_cmd(
            "sudo stat -c %s /etc/nvue.d/startup.yaml 2>/dev/null || echo 0",
            validate=False,
        )
    except Exception:
        return None
    for tok in (out or "").split():
        if tok.isdigit():
            return int(tok)
    return None


def _save_nos_config(engines):
    """Persist applied NVUE before reset, then sync so cpu-reset cannot leave a
    0-byte startup.yaml (NVUE truncates the file, write may still be in page
    cache when bmc-cli reset cpu-reset reboots the host).

    Skip save when live hostname is the NVUE default ``cumulus``: that means
    pytest clear_config applied a strip without saving, and saving it would
    rewrite a good on-disk yaml immediately before reset.
    """
    try:
        hostname = (engines.dut.run_cmd("hostname", validate=False) or "").strip()
        if _hostname_is_nvue_default(hostname):
            logger.warning(
                "skip nv config save: live hostname %r is NVUE default "
                "(clear_config applied, not saved); saving would rewrite "
                "startup.yaml and a reset can leave it 0 bytes",
                hostname,
            )
            return
        engines.dut.run_cmd("nv config save", validate=False)
        engines.dut.run_cmd("sync", validate=False)
        time.sleep(2)
        size = _startup_yaml_size(engines)
        if size is not None and size == 0:
            pytest.fail(
                "nv config save left /etc/nvue.d/startup.yaml at 0 bytes — "
                "refusing to reset the host (would boot unconfigured)"
            )
        logger.info("startup.yaml size after save+sync: %s bytes", size)
    except pytest.fail.Exception:
        raise
    except Exception as err:
        logger.warning(f"nv config save failed (continuing): {type(err).__name__}: {err}")


def _recover_via_bmc_power_cycle(engines, topology_obj):
    """Last-resort recovery: power-cycle the host from the BMC over Redfish.

    Redfish is the only supported protocol for host power state on this platform
    — SW #5165532 waived obmcutil / ipmitool / busctl as unreliable — so both the
    action and the state polling go through ComputerSystem.Reset and PowerState.

    Only helps if the NOS applies its saved config on boot; on setups hitting the
    Cumulus 'boots unconfigured after reset' defect the box may still come up
    unreachable — hence this is best-effort and callers must tolerate failure.
    """
    curl_tool, oob_ip = make_oob_curl_tool(engines, topology_obj)
    # The NOS is down when this runs, so curl must be issued from a host that
    # survives the power cycle rather than from the DUT.
    driver = oob_driver_engine(engines)
    system_id = resolve_redfish_system_id(curl_tool, dut_engine=driver)
    logger.info(f"Recovering host via Redfish power cycle on {oob_ip} "
                f"(System {system_id})")
    reset_nos_via_redfish(curl_tool, 'ForceOff', system_id, dut_engine=driver)
    wait_for_redfish_power_state(curl_tool, system_id, 'Off', dut_engine=driver,
                                 timeout=POWER_OFF_TIMEOUT)
    time.sleep(POWER_OFF_ON_SETTLE_SECONDS)
    reset_nos_via_redfish(curl_tool, 'On', system_id, dut_engine=driver)
    wait_for_redfish_power_state(curl_tool, system_id, 'On', dut_engine=driver,
                                 timeout=POWER_OFF_TIMEOUT)
    time.sleep(HARD_RESET_SETTLE_SECONDS)
    DutUtilsTool.reconnect_engine(engines.dut, find_prompt_tries=30, find_prompt_delay=10)
    ensure_usb0_up(engines.dut, strict=False)


def _reset_nos_health_gate(engines, topology_obj):
    """Fail-fast gate for the bmc-cli reset-NOS suite (via autouse fixture).

    - If a previous reset already stranded the DUT, skip immediately so the run
      does not hang retrying a dead box for hours.
    - Otherwise confirm the NOS is reachable up front (bounded); if not, attempt
      one best-effort recovery, and if that fails mark the DUT stranded + skip.
    - Save the running config before the reset (guards the unsaved-config case).
    """
    # Per-test baseline: is mgmt/eth0 reachable before this reset test starts?
    _log_eth0_reachability(engines, "test-start baseline")
    if _DUT_STRANDED["reason"]:
        pytest.skip(f"DUT stranded by an earlier reset ({_DUT_STRANDED['reason']}) — "
                    f"skipping to avoid a multi-hour cascade — see Cumulus "
                    f"boots-unconfigured-after-reset product bug")
    if not _quick_dut_reachable(engines):
        with allure.step("DUT unreachable at test start — attempt best-effort recovery"):
            _safe_recover_nos(engines, topology_obj)
        if not _quick_dut_reachable(engines):
            reason = "unreachable at test start (recovery failed)"
            _DUT_STRANDED["reason"] = reason
            # Fail the first stranded case so CI is not green-with-skips only;
            # later cases skip via the gate above.
            pytest.fail(
                "DUT stranded: {} — remaining reset cases will skip — "
                "likely Cumulus boots-unconfigured-after-reset product bug"
                .format(reason)
            )
    with allure.step("Save NOS config before reset (guard against unsaved-config loss)"):
        _save_nos_config(engines)


def _safe_recover_nos(engines, topology_obj):
    """Best-effort, bounded NOS recovery after a test failure — never hangs.

    Order of attempts (each bounded; stops as soon as the NOS answers):
      1. Noga remote-reboot/PDU — a no-op on this SPC6 setup (no ``remote_reboot``
         entry in Noga: ``recover_dut_with_remote_reboot`` fails fast with
         "Reboot command is empty").
      2. Reconnect the DUT engine — enough when the box is already rebooting / up.
      3. BMC OOB power-cycle then reconnect — last resort for a wedged box.

    If nothing brings the NOS back, mark the DUT stranded (module-wide) so the
    remaining reset tests skip fast instead of each retrying for minutes. This is
    the Cumulus 'boots unconfigured after reset' product defect — not recoverable
    in-band.
    """
    # Record whether mgmt/eth0 is reachable at the moment recovery starts — this is
    # the clearest signal of whether the reset-recovery defect just reproduced.
    _log_eth0_reachability(engines, "post-failure / recovery-start")

    # 1) Noga remote-reboot (fast failure on this setup)
    try:
        recover_dut_with_remote_reboot(topology_obj, engines)
        if _quick_dut_reachable(engines):
            return
    except Exception as err:
        logger.warning(f"remote-reboot recovery unavailable ({type(err).__name__}: {err}) — "
                       f"falling back to reconnect")

    # 2) plain reconnect (bounded)
    try:
        engines.dut.disconnect()
    except Exception:
        pass
    time.sleep(HARD_RESET_SETTLE_SECONDS)
    try:
        DutUtilsTool.reconnect_engine(engines.dut, find_prompt_tries=RECONNECT_TRIES,
                                      find_prompt_delay=RECONNECT_DELAY)
        ensure_usb0_up(engines.dut, strict=False)
        if _quick_dut_reachable(engines):
            return
    except Exception as err:
        logger.warning(f"reconnect recovery failed: {type(err).__name__}: {err}")

    # 3) BMC OOB power-cycle then reconnect (last resort)
    try:
        _recover_via_bmc_power_cycle(engines, topology_obj)
        if _quick_dut_reachable(engines):
            return
    except Exception as err:
        logger.warning(f"BMC power-cycle recovery failed: {type(err).__name__}: {err}")

    _DUT_STRANDED["reason"] = "recovery exhausted (NOS did not come back)"
    logger.error("NOS recovery exhausted — marking DUT stranded; remaining reset "
                 "tests will skip fast. Likely Cumulus 'boots unconfigured after "
                 "reset' product bug.")
    # Do not pytest.fail here: callers either fail the health gate (first strand
    # at test start) or re-raise the original test exception after recovery.


def _assert_nos_up(engines):
    """Assert the NOS answers a basic command."""
    hostname = engines.dut.run_cmd('hostname', validate=False)
    assert hostname and hostname.strip(), "NOS did not respond to 'hostname' — NOS appears down"
    return hostname.strip()


def _assert_bmc_undisturbed(engines, state_before):
    """Assert the BMC is healthy and unchanged (it must NOT have been rebooted)."""
    state_after = snapshot_bmc_state_safe(engines)
    assert state_after['state'] == FansConsts.STATE_OK, \
        f"BMC state after host reset: '{state_after['state']}', expected '{FansConsts.STATE_OK}'"
    assert state_before['firmware_version'] == state_after['firmware_version'], \
        (f"BMC FW version changed across host reset: "
         f"'{state_before['firmware_version']}' -> '{state_after['firmware_version']}'")
    return state_after


BMC_CLI_NOS_RESET_TYPES = [
    "cpu-reset",
    "power-cycle",
    "power-cycle-immediate",
    "power-cycle-bypass",
]


@pytest.mark.bmc
@pytest.mark.platform
@pytest.mark.disable_loganalyzer
@pytest.mark.skip_clear_config
class TestBmcCliResetNos:
    """BMC resets the main NOS (host CPU) via the CL-native ``bmc-cli reset ...``
    command, issued from the CPU over the internal usb0 link.

    The BMC is the initiator; the NOS / host is the target.
    ``bmc-reset`` is deliberately not covered here — it resets the BMC, not the NOS
    (see ``test_bmc_cli_destructive.py::test_bmc_cli_reset_bmc`` for that).
    OOB Redfish NOS-reset is ``TestBmcResetNosRedfish`` (SN49).
    """

    @pytest.fixture(autouse=True)
    def _health_gate(self, engines, topology_obj):
        """Fail-fast gate: skip fast (and save config) if the DUT is / was stranded."""
        _reset_nos_health_gate(engines, topology_obj)

    @pytest.mark.cumulus
    @pytest.mark.parametrize("reset_type", BMC_CLI_NOS_RESET_TYPES)
    def test_reset_nos_via_bmc_cli(self, engines, devices, topology_obj, reset_type):
        """Reset the NOS from the BMC via ``bmc-cli reset <type>``; verify the NOS
        reboots and recovers, and that the BMC itself was undisturbed.

        The reset is issued from the CPU over usb0, so the command tears down its
        own SSH session — it is fired via ``run_cmd_with_disconnect``.
        """
        skip_if_no_bmc(devices, engines)

        with allure.step("Snapshot BMC state and NOS uptime; confirm NOS is up"):
            state_before = snapshot_bmc_state_safe(engines)
            assert state_before['state'] == FansConsts.STATE_OK, \
                f"BMC not healthy before test: {state_before['state']}"
            nos_uptime_before = nos_uptime_seconds(engines.dut)
            _assert_nos_up(engines)

        try:
            with allure.step(f"Issue 'bmc-cli reset {reset_type}' (host SSH will drop on success)"):
                cmd = BmcCliTool(engines.dut).build_cmd(f"reset {reset_type}")
                # A SUCCESSFUL reset never returns a prompt: the CPU resets and the
                # SSH session drops (netmiko raises) — that is the expected, good
                # outcome. If the command instead RETURNS with a bmc-cli error
                # (e.g. "failed with error code: -12"), the host was NOT reset; this
                # is flaky for cpu-reset, so retry up to 3 times. (power-cycle
                # variants return a short 'Performing ...' message and reset a
                # moment later — that is NOT an error and must not be retried.)
                max_attempts = 3
                attempt_used = 0
                session_dropped = False
                last_error_out = ""
                for attempt in range(1, max_attempts + 1):
                    attempt_used = attempt
                    try:
                        out = engines.dut.run_cmd(
                            cmd, timeout=20, retry_run=False, validate=False)
                    except Exception as err:
                        session_dropped = True
                        logger.info(
                            f"reset dropped the session as expected "
                            f"(attempt {attempt}/{max_attempts}): "
                            f"{type(err).__name__}: {err}")
                        break
                    out_l = (out or '').lower()
                    if ('failed with error code' in out_l or 'error code:' in out_l or
                            'error:' in out_l):
                        last_error_out = str(out).strip()[-200:]
                        logger.warning(
                            f"'bmc-cli reset {reset_type}' returned an error "
                            f"without resetting (attempt {attempt}/{max_attempts}): "
                            f"{last_error_out}")
                        if attempt < max_attempts:
                            time.sleep(10)
                            continue
                        # Exhausted budget — surface count and fail (do not absorb).
                        with allure.step(
                                f"bmc-cli reset {reset_type} failed after "
                                f"{max_attempts} attempts"):
                            pass
                        pytest.fail(
                            f"'bmc-cli reset {reset_type}' still failing after "
                            f"{max_attempts} attempts (last: {last_error_out})"
                        )
                    # Returned without an error (e.g. power-cycle 'Performing ...')
                    break
                with allure.step(
                        f"bmc-cli reset {reset_type} completed on attempt "
                        f"{attempt_used}/{max_attempts}"
                        f"{' (session dropped)' if session_dropped else ''}"):
                    logger.info(
                        f"reset attempt_used={attempt_used}/{max_attempts} "
                        f"session_dropped={session_dropped}"
                    )
                engines.dut.disconnect()

            with allure.step("Wait for the NOS to reboot and come back"):
                # CL reboots fast; wait_on_system_reboot's shutdown-detection phase
                # is unreliable here, so reconnect explicitly (as the firmware tests do).
                time.sleep(HARD_RESET_SETTLE_SECONDS)
                # Log the eth0/mgmt verdict BEFORE the (bounded) reconnect so the run
                # clearly records if the reset-recovery issue reproduced for this type.
                _log_eth0_reachability(engines, f"after bmc-cli reset {reset_type}")
                DutUtilsTool.reconnect_engine(engines.dut, find_prompt_tries=RECONNECT_TRIES,
                                              find_prompt_delay=RECONNECT_DELAY)
                # Non-strict during post-reset settle; subsequent BMC asserts are loud.
                ensure_usb0_up(engines.dut, strict=False)
                _assert_nos_up(engines)

            with allure.step("Assert NOS uptime reset (host actually rebooted)"):
                nos_uptime_after = nos_uptime_seconds(engines.dut)
                if nos_uptime_before is not None and nos_uptime_after is not None:
                    assert nos_uptime_after < nos_uptime_before, (
                        "NOS uptime did not reset (%.0fs -> %.0fs) — "
                        "host was not reset by 'bmc-cli reset %s'"
                        % (nos_uptime_before, nos_uptime_after, reset_type)
                    )

            with allure.step("Assert BMC was undisturbed (not rebooted)"):
                _assert_bmc_undisturbed(engines, state_before)

        except Exception:
            # Bounded recovery: _safe_recover_nos fast-fails the (absent) Noga
            # remote-reboot, then tries a bounded reconnect and a BMC OOB
            # power-cycle, and marks the DUT stranded if none work — so we neither
            # hang on an endless channel read nor cascade into the rest of the run.
            with allure.step("Recovery: recover the NOS after failure"):
                _safe_recover_nos(engines, topology_obj)
            raise


REDFISH_NOS_RESET_TYPES = [
    "GracefulRestart",
    "ForceRestart",
]


@pytest.mark.bmc
@pytest.mark.platform
@pytest.mark.disable_loganalyzer
@pytest.mark.skip_clear_config
@pytest.mark.cumulus
class TestBmcResetNosRedfish:
    """SN49 — BMC resets the host over OOB Redfish ComputerSystem.Reset.

    HTTPS only (CurlTool). Do not gate. Do not dual-probe HTTP. usb0 HTTP is
    SN11; this case is the OOB Reset command.
    """

    @pytest.fixture(autouse=True)
    def _health_gate(self, engines, topology_obj):
        _reset_nos_health_gate(engines, topology_obj)

    @pytest.mark.parametrize("reset_type", REDFISH_NOS_RESET_TYPES)
    def test_reset_nos_via_redfish_oob(
            self, engines, devices, topology_obj, reset_type):
        """POST ComputerSystem.Reset from OOB; NOS reboots; BMC stays up."""
        skip_if_no_bmc(devices, engines)
        driver = oob_driver_engine(engines)
        curl_tool, oob_ip = make_oob_curl_tool(engines, topology_obj)
        logger.info(
            "SN49 OOB Redfish %s via HTTPS to %s (driver=%s)",
            reset_type, oob_ip, type(driver).__name__,
        )

        with allure.step("Snapshot BMC state and NOS uptime; confirm NOS is up"):
            state_before = snapshot_bmc_state_safe(engines)
            assert state_before['state'] == FansConsts.STATE_OK, (
                f"BMC not healthy before test: {state_before['state']}"
            )
            nos_uptime_before = nos_uptime_seconds(engines.dut)
            _assert_nos_up(engines)

        try:
            with allure.step(
                    f"Resolve System id; POST {reset_type} "
                    f"https://{oob_ip}/redfish/v1/Systems/<id>/Actions/"
                    "ComputerSystem.Reset"):
                system_id = resolve_redfish_system_id(
                    curl_tool, dut_engine=driver)
                session_dropped = False
                try:
                    resp = reset_nos_via_redfish(
                        curl_tool, reset_type, system_id, dut_engine=driver)
                except (NetmikoTimeoutException, NetmikoReadTimeout,
                        OSError, EOFError) as err:
                    # Curl ran on the NOS: host reset drops the SSH used to
                    # issue it. That is success if the box then reboots.
                    session_dropped = True
                    logger.info(
                        "Redfish %s dropped the driver session (%s: %s)",
                        reset_type, type(err).__name__, err)
                    resp = None
                if not session_dropped:
                    assert redfish_action_accepted(resp), (
                        f"Redfish {reset_type} not accepted: {resp}"
                    )

            with allure.step("Wait for NOS reboot and reconnect"):
                wait_for_nos_reboot(
                    engines,
                    uptime_before=nos_uptime_before,
                    settle=HARD_RESET_SETTLE_SECONDS,
                    find_prompt_tries=30,
                    find_prompt_delay=10,
                )
                _log_eth0_reachability(
                    engines, f"after Redfish {reset_type}")
                _assert_nos_up(engines)

            with allure.step("Assert BMC was undisturbed (not rebooted)"):
                _assert_bmc_undisturbed(engines, state_before)

        except Exception:
            with allure.step("Recovery: recover the NOS after failure"):
                _safe_recover_nos(engines, topology_obj)
            raise
