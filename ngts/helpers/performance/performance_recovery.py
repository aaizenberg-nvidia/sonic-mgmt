"""Session-scoped recovery and fatal-state handling for performance tests."""

import hashlib
import json
import logging
import os
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

import allure

from devts.infra.tools.exceptions.test_issue import TestIssue


logger = logging.getLogger()

SDK_FATAL_SIGNATURES = ("COMM_ERROR", "sx_api_open failed", "SDK GOT Signal")
DIAGNOSTIC_COMMAND_TIMEOUT = 20
DIAGNOSTIC_OUTPUT_LIMIT = 128 * 1024
HEALTH_GATE_TIMEOUT = 180
HEALTH_GATE_INTERVAL = 10
# This regular expression matches interface names of the form 'swp' followed by one or more digits,
# optionally followed by an 's' and additional digits (e.g., 'swp1', 'swp2s0', 'swp10s5').
_SWP_NAME_RE = re.compile(r"^swp\d+(?:s\d+)?$")
_TRUNCATION_MARKER = "__PERF_DIAGNOSTIC_TRUNCATED__="


@dataclass
class PerformanceSessionState:
    """Track the known-good baseline and fatal recovery state for one pytest session."""

    recovery_enabled: bool = False
    baseline_saved: bool = False
    baseline_save_started: bool = False
    baseline_save_failed: bool = False
    restore_attempted: bool = False
    restore_failed: bool = False
    environment_fatal: bool = False
    fatal_reason: Optional[str] = None
    fatal_nodeid: Optional[str] = None
    diagnostics_started: bool = False
    diagnostics_collected: bool = False
    diagnostic_manifest: Dict[str, Any] = field(default_factory=dict)
    baseline_topology: Dict[str, Any] = field(default_factory=dict)
    failed_test_nodeids: Set[str] = field(default_factory=set)


_STATE_LOCK = threading.RLock()
_SESSION_STATE = PerformanceSessionState()


def get_performance_session_state() -> PerformanceSessionState:
    """Return the mutable process-local performance session state."""
    return _SESSION_STATE


def reset_performance_session_state() -> PerformanceSessionState:
    """Reset performance recovery state, primarily at session start and in unit tests."""
    global _SESSION_STATE
    with _STATE_LOCK:
        _SESSION_STATE = PerformanceSessionState()
        return _SESSION_STATE


def _exception_text(exception: BaseException) -> str:
    """Return messages from an exception and its chained causes."""
    messages = []
    seen = set()
    current = exception
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        messages.append(str(current))
        current = current.__cause__ or current.__context__
    return "\n".join(messages)


def is_sdk_fatal_exception(exception: BaseException) -> bool:
    """Return whether an exception chain contains an exact SDK fatal signature."""
    text = _exception_text(exception)
    return any(signature in text for signature in SDK_FATAL_SIGNATURES)


def has_primary_test_failure(primary_error, node) -> bool:
    """Return whether setup or the test body already supplied the primary failure."""
    call_report = getattr(node, "rep_call", None)
    if primary_error is not None or getattr(call_report, "failed", False):
        return True
    nodeid = getattr(node, "nodeid", "")
    with _STATE_LOCK:
        return any(
            failed_nodeid == nodeid or failed_nodeid.startswith(f"{nodeid}::")
            for failed_nodeid in get_performance_session_state().failed_test_nodeids)


def record_performance_test_failure(nodeid):
    """Record a failed setup or call report for class-scoped teardown decisions."""
    with _STATE_LOCK:
        get_performance_session_state().failed_test_nodeids.add(nodeid)


def _expected_neighbor_aliases(player_alias):
    """Return LLDP neighbor aliases expected for a performance player.

    Args:
        player_alias: Player alias from the performance setup.

    Returns:
        list: Expected neighbor aliases, or an empty list for an unknown alias.
    """
    from ngts.constants.performance_constants import PerfConsts

    if player_alias == PerfConsts.DUT_ALIAS:
        return list(PerfConsts.PERF_SETUP_TG_ALIASES)
    if player_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
        return [PerfConsts.DUT_ALIAS]
    return []


def _is_nvue_cli(cli_obj):
    """Return whether a CLI object's class hierarchy contains ``NvueCli``."""
    return any(cli_class.__name__ == "NvueCli" for cli_class in type(cli_obj).__mro__)


def _capture_nvue_topology(players):
    """Capture connected local ports and expected aliases before saving the baseline."""
    topology = {}
    for player_alias, player in players.items():
        cli_obj = player.get("cli")
        if not _is_nvue_cli(cli_obj):
            continue
        expected_aliases = _expected_neighbor_aliases(player_alias)
        neighbors = cli_obj.interface.filter_lldp_neighbors(expected_aliases)
        missing = [alias for alias in expected_aliases if not neighbors.get(alias)]
        if missing:
            raise TestIssue(
                f"Cannot save known-good baseline on {player_alias}: missing LLDP neighbors {missing}")
        neighbor_ports = {
            alias: sorted(port for port in neighbors.get(alias, []) if _SWP_NAME_RE.match(port))
            for alias in expected_aliases
        }
        topology[player_alias] = {
            "expected_neighbors": expected_aliases,
            "neighbor_ports": neighbor_ports,
            "connected_ports": sorted({
                port for ports in neighbor_ports.values() for port in ports
            }),
        }
    return topology


def save_session_baseline(players, save_callback, topology_capture=None):
    """Save one known-good baseline and block all later save attempts.

    Args:
        players: Performance players mapping.
        save_callback: Existing save operation to invoke once.
        topology_capture: Optional injectable topology snapshot function.

    Returns:
        bool: True when the baseline was saved, False when it already existed.

    Raises:
        TestIssue: If a restore or fatal failure already makes saving unsafe.
        Exception: The original save or topology exception.
    """
    if os.environ.get("PYTEST_XDIST_WORKER"):
        raise TestIssue(
            "Performance session recovery requires a single pytest process; xdist is unsupported")
    state = get_performance_session_state()
    with _STATE_LOCK:
        state.recovery_enabled = True
        if state.baseline_saved:
            return False
        if (state.baseline_save_failed or state.restore_attempted or
                state.restore_failed or state.environment_fatal):
            raise TestIssue("Refusing to overwrite the session baseline after restore or fatal failure")
        if state.baseline_save_started:
            raise TestIssue("Performance session baseline save is already in progress")
        state.baseline_save_started = True
    capture = topology_capture or _capture_nvue_topology
    try:
        baseline_topology = capture(players)
        save_callback()
    except Exception as exception:
        with _STATE_LOCK:
            state.baseline_save_started = False
            state.baseline_save_failed = True
            state.environment_fatal = True
            if state.fatal_reason is None:
                state.fatal_reason = _exception_text(exception)
        raise
    with _STATE_LOCK:
        state.baseline_topology = baseline_topology
        state.baseline_saved = True
        state.baseline_save_started = False
    return True


def _run_bounded_command(engine, command, output_limit=DIAGNOSTIC_OUTPUT_LIMIT):
    """Run a diagnostic command with bounded execution time and output size.

    Args:
        engine: Connection engine used to execute the command.
        command: Shell command to execute.
        output_limit: Maximum number of output bytes to retain.

    Returns:
        tuple: The retained command output and whether it was truncated.
    """
    # Limit output on the player so large diagnostics are not transferred over SSH.
    remote_script = (
        f"output=$(timeout {DIAGNOSTIC_COMMAND_TIMEOUT}s bash -o pipefail -c "
        f"{shlex.quote(command)} 2>&1 || true);\n"
        "size=$(printf '%s' \"$output\" | wc -c);\n"
        f"if [ \"$size\" -gt {output_limit} ]; then\n"
        f"    printf '{_TRUNCATION_MARKER}%s\\n' \"$size\";\n"
        f"    printf '%s' \"$output\" | tail -c {output_limit};\n"
        "else\n"
        "    printf '%s' \"$output\";\n"
        "fi")
    wrapped_command = f"bash -o pipefail -c {shlex.quote(remote_script)}"
    output = engine.run_cmd(wrapped_command, validate=False, print_output=False,
                            timeout=DIAGNOSTIC_COMMAND_TIMEOUT + 5, retry_run=False)
    output = str(output)
    truncated = output.startswith(_TRUNCATION_MARKER)
    if truncated:
        output = output.split("\n", 1)[1] if "\n" in output else ""

    # Defensively enforce the limit again in case the connection engine adds output.
    output_bytes = output.encode("utf-8", errors="replace")
    if len(output_bytes) > output_limit:
        truncated = True
        output = output_bytes[-output_limit:].decode("utf-8", errors="replace")
    return output, truncated


def _nvue_diagnostic_commands():
    """Return read-only NVUE diagnostic commands keyed by artifact name.

    Returns:
        dict: Mapping of diagnostic artifact names to shell commands.
    """
    return {
        "journals": (
            "journalctl -u switchd -u sx_sdk -u nvued --since '-15 minutes' "
            "--no-pager -n 1000"),
        "process_status": (
            "systemctl status switchd nvued --no-pager; "
            "ps -eo pid,ppid,state,lstart,cmd --sort=-lstart | head -100"),  # SpellCheck-ignoreWord:ppid,lstart
        "nvue_revision": (
            "nv config show; nv config diff; nv config history 2>/dev/null || true"),
        "startup_baseline_metadata": (
            "stat -c '%n size=%s mtime=%y' ~/startup.yaml; "
            "sha256sum ~/startup.yaml"),
        "qos_buffer": (
            "nv show qos -o json; nv show system buffer -o json 2>/dev/null || true"),
        "port_state": "nv show interface -o json",
        "lldp": "nv show interface lldp -o json",
        "coredumps": (
            "coredumpctl list --no-pager --since '-1 hour' 2>/dev/null || "  # SpellCheck-ignoreWord:coredumpctl
            "ls -lah /var/lib/systemd/coredump 2>/dev/null || true"),
    }


def collect_fatal_diagnostics(players, reason, nodeid=None):
    """Collect bounded, read-only diagnostics without masking the primary failure.

    Args:
        players: Performance players mapping.
        reason: Fatal-state reason recorded in the manifest.
        nodeid: Pytest node identifier associated with the failure.

    Returns:
        dict: Diagnostic manifest, including per-command errors.
    """
    state = get_performance_session_state()
    with _STATE_LOCK:
        if state.diagnostics_started:
            return state.diagnostic_manifest
        state.diagnostics_started = True

    manifest = {
        "reason": reason,
        "nodeid": nodeid,
        "command_timeout_seconds": DIAGNOSTIC_COMMAND_TIMEOUT,
        "output_limit_bytes": DIAGNOSTIC_OUTPUT_LIMIT,
        "sdk_dump": {
            "status": "skipped",
            "reason": (
                "SDK socket is unavailable"
                if any(signature in reason for signature in SDK_FATAL_SIGNATURES)
                else "automatic recovery collects bounded read-only artifacts only"),
        },
        "players": {},
    }
    try:
        for player_alias, player in players.items():
            cli_obj = player.get("cli")
            player_manifest = {"nvue_supported": _is_nvue_cli(cli_obj), "artifacts": {}}
            manifest["players"][player_alias] = player_manifest
            if not _is_nvue_cli(cli_obj):
                player_manifest["unsupported_reason"] = "NVUE diagnostics are not supported on this CLI type"
                continue
            engine = player.get("engine") or getattr(cli_obj, "engine", None)
            if engine is None:
                player_manifest["error"] = "Player has no command engine"
                continue
            for artifact_name, command in _nvue_diagnostic_commands().items():
                try:
                    output, truncated = _run_bounded_command(
                        engine, command)
                    digest = hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()
                    player_manifest["artifacts"][artifact_name] = {
                        "bytes": len(output.encode("utf-8", errors="replace")),
                        "command": command,
                        "sha256": digest,
                        "truncated": truncated,
                    }
                    try:
                        allure.attach(output, name=f"fatal_{player_alias}_{artifact_name}",
                                      attachment_type=allure.attachment_type.TEXT)
                    except Exception as attachment_error:
                        player_manifest["artifacts"][artifact_name]["attachment_error"] = str(attachment_error)
                except Exception as command_error:
                    player_manifest["artifacts"][artifact_name] = {
                        "command": command,
                        "error": str(command_error),
                    }
    except Exception as collector_error:
        manifest["collector_error"] = str(collector_error)
    finally:
        manifest_json = json.dumps(manifest, indent=2, sort_keys=True, default=str)
        try:
            allure.attach(manifest_json, name="fatal_diagnostics_manifest.json",
                          attachment_type=allure.attachment_type.JSON)
        except Exception as attachment_error:
            manifest["manifest_attachment_error"] = str(attachment_error)
        with _STATE_LOCK:
            state.diagnostic_manifest = manifest
            state.diagnostics_collected = True
    return manifest


def mark_environment_fatal(players, reason, nodeid=None, diagnostics_collector=None):
    """Mark the environment fatal and trigger diagnostics once."""
    state = get_performance_session_state()
    with _STATE_LOCK:
        state.environment_fatal = True
        if state.fatal_reason is None:
            state.fatal_reason = reason
            state.fatal_nodeid = nodeid
    collector = diagnostics_collector or collect_fatal_diagnostics
    try:
        collector(players, reason, nodeid)
    except Exception as collector_error:
        logger.exception("Fatal diagnostic collection failed and was ignored: %s", collector_error)


def handle_sdk_fatal_exception(players, exception, nodeid=None, diagnostics_collector=None):
    """Classify an SDK fatal signature, mark state, and collect diagnostics."""
    if not get_performance_session_state().recovery_enabled:
        return False
    if not is_sdk_fatal_exception(exception):
        return False
    mark_environment_fatal(players, _exception_text(exception), nodeid, diagnostics_collector)
    return True


def _check_nvue_topology_health(players, baseline_topology):
    """Check expected connected-port state and exact LLDP attachment points once."""
    for player_alias, expected in baseline_topology.items():
        player = players[player_alias]
        cli_obj = player["cli"]
        if not _is_nvue_cli(cli_obj):
            continue
        interface_output = cli_obj.performance.execute_cmd("nv show interface", print_output=False)
        states = {}
        for line in interface_output.splitlines():
            match = re.match(r"^(swp\d+(?:s\d+)?)\s+(\S+)\s+(\S+)", line.strip())
            if match:
                states[match.group(1)] = match.groups()[1:]
        down_ports = [
            f"{port}(admin={states.get(port, ('missing', 'missing'))[0]}, "
            f"oper={states.get(port, ('missing', 'missing'))[1]})"
            for port in expected["connected_ports"]
            if states.get(port) != ("up", "up")
        ]
        if down_ports:
            raise TestIssue(f"Expected topology ports are not oper-up on {player_alias}: {down_ports}")
        neighbors = cli_obj.interface.filter_lldp_neighbors(expected["expected_neighbors"])
        missing_neighbors = [
            alias for alias in expected["expected_neighbors"] if not neighbors.get(alias)]
        if missing_neighbors:
            raise TestIssue(
                f"Expected LLDP neighbors are missing on {player_alias}: {missing_neighbors}")
        changed_neighbors = {
            alias: {
                "expected": sorted(expected.get("neighbor_ports", {}).get(alias, [])),
                "actual": sorted(
                    port for port in neighbors.get(alias, []) if _SWP_NAME_RE.match(port)),
            }
            for alias in expected["expected_neighbors"]
        }
        changed_neighbors = {
            alias: ports for alias, ports in changed_neighbors.items()
            if ports["actual"] != ports["expected"]
        }
        if changed_neighbors:
            raise TestIssue(
                f"LLDP attachment points changed on {player_alias}: {changed_neighbors}")


def _default_nvue_health_gate(players, baseline_topology, timeout=HEALTH_GATE_TIMEOUT,
                              interval=HEALTH_GATE_INTERVAL):
    """Wait for SDK, expected ports, and exact LLDP attachment points to converge."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            for player_alias in baseline_topology:
                player = players[player_alias]
                cli_obj = player["cli"]
                if not _is_nvue_cli(cli_obj):
                    continue
                engine = player.get("engine") or cli_obj.engine
                engine.run_cmd(
                    f"timeout {DIAGNOSTIC_COMMAND_TIMEOUT}s sudo sx_api_ports_dump.py >/dev/null",
                    validate=True, print_output=False, timeout=DIAGNOSTIC_COMMAND_TIMEOUT + 5,
                    retry_run=False)
            _check_nvue_topology_health(players, baseline_topology)
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(interval)


def restore_session_baseline(players, restore_callback, nodeid=None, health_checker=None,
                             diagnostics_collector=None):
    """Restore the session baseline, health-gate NVUE, and preserve failures.

    Args:
        players: Performance players mapping.
        restore_callback: Existing restore operation, preserving suite-specific aliases.
        nodeid: Pytest node identifier associated with the restore.
        health_checker: Optional injectable health gate.
        diagnostics_collector: Optional injectable best-effort collector.

    Returns:
        The restore callback result.

    Raises:
        TestIssue: If no session baseline was saved.
        Exception: The original restore or health-gate exception.
    """
    state = get_performance_session_state()
    with _STATE_LOCK:
        if not state.baseline_saved:
            raise TestIssue("Cannot restore performance configuration before saving the session baseline")
        state.restore_attempted = True
    checker = health_checker or _default_nvue_health_gate
    try:
        result = restore_callback()
        checker(players, state.baseline_topology)
        return result
    except Exception as exception:
        with _STATE_LOCK:
            state.restore_failed = True
        mark_environment_fatal(players, _exception_text(exception), nodeid, diagnostics_collector)
        raise


def run_guarded_operation(players, operation, nodeid=None, diagnostics_collector=None):
    """Run setup/validator work and classify only exact SDK fatal signatures."""
    try:
        return operation()
    except Exception as exception:
        handle_sdk_fatal_exception(players, exception, nodeid, diagnostics_collector)
        raise


def exit_if_performance_environment_fatal(exit_callback, returncode):
    """Invoke the supplied pytest exit callback when the session is fatal."""
    state = get_performance_session_state()
    if not state.environment_fatal:
        return
    reason = state.fatal_reason or "unknown fatal performance environment failure"
    exit_callback(
        f"Performance environment is fatal after {state.fatal_nodeid}: {reason}",
        returncode=returncode)


def enforce_fatal_session_exit_status(session, returncode):
    """Preserve the fatal return code when no later test reaches the setup gate."""
    if (get_performance_session_state().environment_fatal and
            session.exitstatus in (0, 1)):
        session.exitstatus = returncode
