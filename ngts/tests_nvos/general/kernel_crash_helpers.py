"""
Helper functions for ``test_repeated_kernel_crash_core_dump_retention`` (Landman row 27).

Repeated sysrq kernel panics: verify kdump capture per cycle, reboot reason, SSD/artifact
bounds, auto tech-support, manual tech-support after stress, and cleanup.

Tune with KERNEL_CRASH_* environment variables (see constants below).
"""

from __future__ import annotations

import ast
import logging
import os
import re
import time
from datetime import datetime
from typing import List, Optional, Set

import pytest

from ngts.constants.constants import BugHandlerConst, CoreDumpConsts
from ngts.nvos_constants.constants_nvos import (
    DatabaseConst,
    HealthConsts,
    NvosConst,
    RebootConsts,
    RemarkableLogsConsts,
    StatsConsts,
    SyslogConsts,
    SystemConsts,
)
from ngts.nvos_tools.infra.DeviceLogTool import grep_log_lines_after_datetime
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.DatabaseTool import DatabaseTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
from ngts.tools.test_utils import allure_utils as allure
try:
    from devts.infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine
except ModuleNotFoundError:
    from infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine

logger = logging.getLogger(__name__)

VAR_CRASH_ROOT = "/var/crash"
KERNEL_CRASH_COLLECTED_PATH = "/var/crash/collected"
VAR_CORE_PATH = "/var/core"
KDUMP_CONFIG_DB_KEY = "KDUMP|config"

# Repeated kernel-crash / core-retention stress (env overrides).
KERNEL_CRASH_STRESS_CYCLES = int(os.environ.get("KERNEL_CRASH_STRESS_CYCLES", "0"))
KERNEL_CRASH_KDUMP_NUM_DUMPS_FALLBACK = int(
    os.environ.get("KERNEL_CRASH_KDUMP_NUM_DUMPS_FALLBACK", "3")
)
KERNEL_CRASH_MAX_VAR_CORE_FILES = int(os.environ.get("KERNEL_CRASH_MAX_VAR_CORE_FILES", "25"))
KERNEL_CRASH_MAX_ARTIFACT_MB = int(os.environ.get("KERNEL_CRASH_MAX_ARTIFACT_MB", "2500"))
KERNEL_CRASH_MIN_SSD_AVAIL_GB = float(os.environ.get("KERNEL_CRASH_MIN_SSD_AVAIL_GB", "2"))
KERNEL_CRASH_POST_REBOOT_WAIT_SEC = int(os.environ.get("KERNEL_CRASH_POST_REBOOT_WAIT_SEC", "15"))
KERNEL_CRASH_DETECTION_LOG_WAIT_SEC = int(
    os.environ.get("KERNEL_CRASH_DETECTION_LOG_WAIT_SEC", str(5 * 60))
)
KERNEL_CRASH_TECHSUPPORT_LOG_WAIT_SEC = int(
    os.environ.get("KERNEL_CRASH_TECHSUPPORT_LOG_WAIT_SEC", str(12 * 60))
)
# Default repeated-crash cycle count when KERNEL_CRASH_STRESS_CYCLES is unset (0).
KERNEL_CRASH_STRESS_CYCLES_DEFAULT = int(
    os.environ.get("KERNEL_CRASH_STRESS_CYCLES_DEFAULT", "4")
)

# Landman row 27 — failure-mode traceability (coverage depth, not pass/fail gates).
LANDMAN_ROW27_FMEA = {
    "SW-006": "primary: repeated sysrq kernel panic, reboot reason, recovery, final health",
    "SW-026": "primary: kdump capture per crash, cleanup, bounded SSD / artifact growth",
    "SYS-010": "secondary: SSD usable for logging and tech-support after repeated crashes",
    "SYS-012": "proxy only: crash/reboot resilience — not DRAM fault injection",
}


def get_kdump_expected_patterns():
    kdump_files_names_templates = ["dmesg.{}.gz", "kdump.{}", "kdump_lock.gz"]
    expected_patterns_list = []
    for template in kdump_files_names_templates:
        if "{}" in template:
            pattern = re.escape(template).replace(r"\{\}", NvosConst.TIMESTAMP_REGEX)
        else:
            pattern = re.escape(template)
        expected_patterns_list.append(re.compile(f"^{pattern}$"))
    return expected_patterns_list


def serial_login_before_crash(
    topology_obj, devices, serial_engine: PexpectSerialEngine, cycle: int
) -> PexpectSerialEngine:
    """After a kernel panic the DUT reboots to ``nvos login:``; re-login from cycle 2 onward."""
    if cycle == 1:
        return serial_engine
    with allure.step(f"Re-login serial console before kernel crash (cycle {cycle})"):
        return ConnectionTool.create_serial_connection(
            topology_obj, devices, force_new_login=True
        )


def trigger_kernel_crash_via_serial(serial_engine: PexpectSerialEngine):
    serial_engine.run_cmd("echo 1 | sudo tee /proc/sys/kernel/sysrq")
    serial_engine.run_cmd("echo c | sudo tee /proc/sysrq-trigger")


def kernel_crash_start_time(system: System) -> datetime:
    return datetime.strptime(
        ClockTools.get_local_time_from_show_system_date_time_output(system.datetime.show()),
        BugHandlerConst.TIMESTAMP_FORMATS[4],
    )


def wait_after_kernel_crash_reboot(engines, post_wait_sec: int = KERNEL_CRASH_POST_REBOOT_WAIT_SEC):
    DutUtilsTool.wait_on_system_reboot(engines.dut)
    time.sleep(post_wait_sec)


def assert_kernel_panic_reboot_reason(system: System):
    # /system/reboot is a container (counters, history, reason); the reason text
    # lives on the /reason leaf, so query that resource directly.
    reason_row = OutputParsingTool.parse_json_str_to_dictionary(
        system.reboot.reason.show()
    ).get_returned_value()
    reboot_reason = str(reason_row.get("reason", "")).strip()
    assert RebootConsts.KERNEL_PANIC in reboot_reason, (
        f"Expected reason: '{RebootConsts.KERNEL_PANIC}'. Got: {reason_row}"
    )


def kernel_crash_syslog_paths(system: System, engine=None) -> List[str]:
    """NVUE ``/system/log/file`` paths for rotation-safe grep (syslog, syslog.1, *.gz)."""
    entries = {}
    try:
        entries = OutputParsingTool.parse_json_str_to_dictionary(
            system.log.file.show()
        ).get_returned_value()
    except Exception as exc:
        logger.warning("nv show system log file failed: %s", exc)
    paths = []
    for key in sorted(entries.keys()):
        if "syslog" not in key:
            continue
        val = entries[key]
        if isinstance(val, dict) and val.get("path"):
            paths.append(val["path"])
        else:
            paths.append(f"{RemarkableLogsConsts.LOGS_PATH}{key}")
    if not paths and engine is not None:
        listing = engine.run_cmd(
            f"ls -1 {RemarkableLogsConsts.LOGS_PATH} 2>/dev/null | grep syslog || true",
            validate=False,
        ).strip()
        paths = [
            f"{RemarkableLogsConsts.LOGS_PATH}{name}"
            for name in listing.splitlines()
            if name.strip()
        ]
    if not paths:
        paths = [SyslogConsts.SYSLOG_LOG_PATH, SyslogConsts.SYSLOG_LOG_PATH + ".1"]
    return paths


def live_syslog_paths(log_paths: List[str]) -> List[str]:
    """Current + previous plain syslog (post-reboot lines land here before rotation)."""
    live = [
        p
        for p in log_paths
        if p.endswith("/syslog") or p.endswith("/syslog.1")
    ]
    return live or [SyslogConsts.SYSLOG_LOG_PATH, SyslogConsts.SYSLOG_LOG_PATH + ".1"]


_AUTO_TECHSUPPORT_DUMP_RE = re.compile(
    r"nvos_dump_.*_(\d{8}_\d{6})\.tar\.gz$"
)


def auto_techsupport_archive_after(engine, start_time: datetime) -> Optional[str]:
    """Return newest auto-generated nvos_dump archive whose embedded time is >= crash start."""
    start_str = start_time.strftime(StatsConsts.SYSTEM_TIME_FORMAT)
    search_dirs = (
        SystemConsts.TECHSUPPORT_FILES_PATH,
        "/var/support/",
    )
    for base in search_dirs:
        listing = engine.run_cmd(
            f"sudo ls -1t {base}nvos_dump_*.tar.gz 2>/dev/null | head -8 || true",
            validate=False,
        ).strip()
        for path in listing.splitlines():
            path = path.strip()
            if not path:
                continue
            name = path.rsplit("/", 1)[-1]
            match = _AUTO_TECHSUPPORT_DUMP_RE.search(name)
            if not match:
                continue
            dump_ts = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").strftime(
                StatsConsts.SYSTEM_TIME_FORMAT
            )
            if dump_ts >= start_str:
                return path
    return None


def assert_kernel_crash_log_patterns(
    engine,
    system: System,
    patterns: List[str],
    start_time: datetime,
    log_paths: Optional[List[str]] = None,
):
    """Rotation-safe log check via DeviceLogTool (cat/zcat across rotated syslog files)."""
    if log_paths is None:
        log_paths = kernel_crash_syslog_paths(system, engine)
    start_str = start_time.strftime(StatsConsts.SYSTEM_TIME_FORMAT)
    missing = []
    with allure.step(f"Verify kernel-crash logs after {start_str} in {log_paths}"):
        for pattern in patterns:
            lines = grep_log_lines_after_datetime(
                engine,
                pattern,
                start_time,
                log_files=log_paths,
                extended_regex=True,
            )
            if not any(re.search(pattern, line) for line in lines):
                missing.append(pattern)
                logger.warning(
                    "Log pattern %r not found after %s (got %d line(s), sample=%r)",
                    pattern,
                    start_str,
                    len(lines),
                    lines[:3] if lines else None,
                )
        assert not missing, (
            f"Missing log pattern(s) after {start_str} "
            f"(searched {log_paths}): {missing}"
        )


def verify_kernel_crash_detection_logs(system: System, engines, start_time: datetime):
    assert_kernel_crash_log_patterns(
        engines.dut,
        system,
        [
            r"Kernel crashes detected:",
            r"System is ready to respond, will take tech support file\.",
            r"Generating system tech-support file, it might take a few minutes\.\.\.",
        ],
        start_time,
    )


def wait_for_kernel_crash_detection_logs(
    system: System, engines, start_time: datetime, timeout_sec: int = None
):
    """Poll syslog until crash-detection and auto tech-support start messages appear."""
    timeout_sec = timeout_sec or KERNEL_CRASH_DETECTION_LOG_WAIT_SEC
    all_paths = kernel_crash_syslog_paths(system, engines.dut)
    log_paths = live_syslog_paths(all_paths)
    deadline = time.time() + timeout_sec
    last_error = None
    while time.time() < deadline:
        try:
            assert_kernel_crash_log_patterns(
                engines.dut,
                system,
                [
                    r"Kernel crashes detected:",
                    r"System is ready to respond, will take tech support file\.",
                    r"Generating system tech-support file, it might take a few minutes\.\.\.",
                ],
                start_time,
                log_paths=log_paths,
            )
            return time.time()
        except AssertionError as exc:
            last_error = exc
            time.sleep(30)
    pytest.fail(
        f"Kernel crash detection logs not seen within {timeout_sec}s after crash: "
        f"{last_error}"
    )


def wait_for_auto_techsupport_logs(system: System, engines, start_time: datetime, timeout_sec: int):
    """Poll live syslog and /host/dump for auto tech-support completion after this crash."""
    log_paths = live_syslog_paths(
        kernel_crash_syslog_paths(system, engines.dut)
    )
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        archive = auto_techsupport_archive_after(engines.dut, start_time)
        if archive:
            logger.info("Auto tech-support archive on disk: %s", archive)
            allure.attach("auto_techsupport_archive", archive)
            return time.time()
        try:
            assert_kernel_crash_log_patterns(
                engines.dut,
                system,
                [r"Generated tech-support"],
                start_time,
                log_paths=log_paths,
            )
            return time.time()
        except AssertionError:
            time.sleep(30)
    pytest.fail(
        f"Auto tech-support log not seen within {timeout_sec}s after kernel crash"
    )


def parse_kdump_fields(raw: str) -> dict:
    """Extract enabled / ready / num_dumps from a single CLI output block."""
    enabled = bool(
        re.search(r"Enabled\s*:\s*(true|yes|1|enabled)", raw, re.I) or
        re.search(r"kdump\s+enabled", raw, re.I) or
        re.search(r"Kdump Administrative Mode:\s*Enabled", raw, re.I)
    )
    disabled = bool(
        re.search(r"Enabled\s*:\s*(false|no|0|disabled)", raw, re.I) or
        re.search(r"Kdump Administrative Mode:\s*Disabled", raw, re.I)
    )
    if disabled:
        enabled = False

    ready = bool(
        re.search(r"Kdump Operational State:\s*Ready", raw, re.I) or
        re.search(r"Kdump operational mode:\s*Ready", raw, re.I) or
        re.search(r"Operational State\s*:\s*Ready", raw, re.I) or
        re.search(r"operational mode:\s*Ready", raw, re.I)
    )

    num_match = re.search(
        r"(?:num_dumps|Maximum number of (?:Kernel Core files Stored|Kdump files))"
        r"\s*[:=]\s*[\"']?(\d+)",
        raw,
        re.I,
    )
    num_dumps = int(num_match.group(1)) if num_match else None
    if num_dumps is not None:
        num_dumps = max(1, min(num_dumps, 9))

    return {"enabled": enabled, "ready": ready, "num_dumps": num_dumps}


def cli_output_unavailable(output: str) -> bool:
    lower = (output or "").lower()
    return (
        "command not found" in lower or
        "unknown command" in lower or
        ("no such file" in lower and "show" in lower)
    )


def parse_kdump_config_db(raw: str) -> dict:
    """Parse ``sonic-db-cli CONFIG_DB HGETALL KDUMP|config`` style output."""
    result = {"enabled": None, "num_dumps": None}
    if not raw or cli_output_unavailable(raw):
        return result
    text = raw.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, dict):
                if "enabled" in parsed:
                    val = parsed["enabled"]
                    if isinstance(val, bool):
                        result["enabled"] = val
                    elif isinstance(val, str):
                        result["enabled"] = val.lower() in ("true", "yes", "1", "enabled")
                if "num_dumps" in parsed:
                    try:
                        result["num_dumps"] = max(1, min(int(parsed["num_dumps"]), 9))
                    except (TypeError, ValueError):
                        pass
                return result
        except (SyntaxError, ValueError):
            pass
    tokens = [t.strip().strip("'\"") for t in re.split(r"[\s\n,{}]+", raw) if t.strip()]
    for idx, tok in enumerate(tokens):
        if tok.lower() == "enabled" and idx + 1 < len(tokens):
            result["enabled"] = tokens[idx + 1].lower() in ("true", "yes", "1", "enabled")
        if tok.lower() == "num_dumps" and idx + 1 < len(tokens):
            try:
                result["num_dumps"] = max(1, min(int(tokens[idx + 1]), 9))
            except ValueError:
                pass
    return result


def has_crashkernel_reserved(engine) -> bool:
    cmdline = engine.run_cmd("cat /proc/cmdline", validate=False)
    return bool(re.search(r"crashkernel=", cmdline))


def get_kdump_config(engine) -> dict:
    """
    Discover kdump settings (NVOS + SONiC):

    1. CONFIG_DB ``KDUMP|config`` (NVOS / NVUE — no ``show`` CLI)
    2. ``nv show kdump`` JSON if present
    3. SONiC ``show kdump config`` / ``status`` / ``show kdump``
    4. ``/proc/cmdline`` crashkernel as an informational reservation hint
    """
    cli_outputs = {}
    merged = {"enabled": False, "ready": False, "num_dumps": None}
    num_dumps_source = None

    kdump_status_out = engine.run_cmd("sudo kdump-config status", validate=False)
    cli_outputs["kdump-config status"] = kdump_status_out
    if re.search(r"ready to kdump", kdump_status_out, re.I):
        merged["enabled"] = True
        merged["ready"] = True

    config_db_raw = DatabaseTool.sonic_db_cli_hgetall(
        engine,
        asic="",
        db_name=DatabaseConst.CONFIG_DB_NAME,
        table_name=f'"{KDUMP_CONFIG_DB_KEY}"',
    )
    cli_outputs["CONFIG_DB KDUMP|config"] = config_db_raw or ""
    db_parsed = parse_kdump_config_db(config_db_raw or "")
    if db_parsed["enabled"] is True:
        merged["enabled"] = True
    if db_parsed["num_dumps"] is not None:
        merged["num_dumps"] = db_parsed["num_dumps"]
        num_dumps_source = "CONFIG_DB"

    nv_commands = ("nv show kdump -o json", "nv show kdump config -o json")
    for cmd in nv_commands:
        out = engine.run_cmd(cmd, validate=False)
        cli_outputs[cmd] = out
        if cli_output_unavailable(out):
            continue
        parsed = parse_kdump_fields(out)
        if parsed["enabled"]:
            merged["enabled"] = True
        if parsed["ready"]:
            merged["ready"] = True
        if parsed["num_dumps"] is not None and merged["num_dumps"] is None:
            merged["num_dumps"] = parsed["num_dumps"]
            num_dumps_source = cmd

    sonic_commands = ("show kdump config", "show kdump status", "show kdump")
    for cmd in sonic_commands:
        out = engine.run_cmd(cmd, validate=False)
        cli_outputs[cmd] = out
        if cli_output_unavailable(out):
            continue
        parsed = parse_kdump_fields(out)
        if parsed["enabled"]:
            merged["enabled"] = True
        if parsed["ready"]:
            merged["ready"] = True
        if parsed["num_dumps"] is not None and merged["num_dumps"] is None:
            merged["num_dumps"] = parsed["num_dumps"]
            num_dumps_source = cmd

    crashkernel = has_crashkernel_reserved(engine)
    cli_outputs["/proc/cmdline crashkernel"] = (
        "present" if crashkernel else "not present"
    )
    if crashkernel and num_dumps_source is None and merged["num_dumps"] is None:
        num_dumps_source = "crashkernel_cmdline"

    if merged["num_dumps"] is None:
        merged["num_dumps"] = KERNEL_CRASH_KDUMP_NUM_DUMPS_FALLBACK
        num_dumps_source = "fallback_default"

    combined_raw = "\n---\n".join(
        f"[{cmd}]\n{out}" for cmd, out in cli_outputs.items()
    )
    return {
        "enabled": merged["enabled"],
        "ready": merged["ready"],
        "num_dumps": merged["num_dumps"],
        "num_dumps_source": num_dumps_source,
        "crashkernel_reserved": crashkernel,
        "config_db_enabled": db_parsed["enabled"],
        "cli_outputs": cli_outputs,
        "raw": combined_raw,
    }


def list_kdump_dump_dirs(engine) -> list[str]:
    """
    Discover kdump directories under /var/crash (supports both
    /var/crash/<timestamp> and /var/crash/collected/<timestamp> layouts).
    """
    out = engine.run_cmd(
        r"sudo find /var/crash -type f \( -name 'dmesg.*' -o -name 'kdump.*' \) "
        r"! -name 'kdump_lock.gz' -printf '%h\n' 2>/dev/null | sort -u",
        validate=False,
    ).strip()
    if not out or out == "find:":
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def list_var_core_entries(engine) -> list[str]:
    out = engine.run_cmd(
        f"sudo ls -1 {VAR_CORE_PATH} 2>/dev/null || true", validate=False
    ).strip()
    if not out or "No such file" in out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def list_systemd_coredump_entries(engine) -> list[str]:
    """Informational only — SONiC/NVOS user cores typically use /var/core, not systemd."""
    out = engine.run_cmd(
        f"sudo ls -1 {CoreDumpConsts.COREDUMP_PATH} 2>/dev/null || true", validate=False
    ).strip()
    if not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def crash_artifact_inventory(engine) -> dict:
    kdump_dirs = list_kdump_dump_dirs(engine)
    var_core = list_var_core_entries(engine)
    systemd = list_systemd_coredump_entries(engine)
    du_cmd = (
        f"sudo du -sm {VAR_CRASH_ROOT} {VAR_CORE_PATH} 2>/dev/null "
        r"| awk '{s+=$1} END {print s+0}'"
    )
    mb_raw = engine.run_cmd(du_cmd, validate=False).strip()
    total_mb = float(mb_raw) if mb_raw.replace(".", "", 1).isdigit() else 0.0
    return {
        "kdump_count": len(kdump_dirs),
        "kdump_dirs": kdump_dirs,
        "kdump_dir_names": sorted(os.path.basename(d) for d in kdump_dirs),
        "var_core_files": len(var_core),
        "systemd_cores": len(systemd),
        "total_mb": total_mb,
        # Legacy keys for Allure / summaries
        "collected_dirs": len(kdump_dirs),
        "collected_names": [os.path.basename(d) for d in kdump_dirs],
    }


def get_ssd_avail_gb(engine) -> float:
    df_output = engine.run_cmd("df -h | grep root-overlay", validate=False)
    parts = df_output.split()
    if len(parts) < 4:
        return 0.0
    avail = parts[3]
    if avail.endswith("G"):
        return float(avail[:-1])
    if avail.endswith("M"):
        return float(avail[:-1]) / 1024
    return 0.0


def assert_kdump_dir_sanity(engine, dump_dir: str):
    """Lightweight per-cycle check: newest kdump dir has dmesg.* and kdump.* artifacts."""
    listing = engine.run_cmd(f"sudo ls -1 {dump_dir} 2>/dev/null || true", validate=False)
    assert re.search(r"dmesg\.", listing), (
        f"Expected dmesg.* in kdump dir {dump_dir}, got: {listing!r}"
    )
    assert re.search(r"^kdump\.[^/]+$", listing, re.M), (
        f"Expected kdump.<timestamp> in kdump dir {dump_dir}, got: {listing!r}"
    )


def pick_new_kdump_dirs(current_dirs: List[str], previous_dirs: Set[str]) -> List[str]:
    return [d for d in current_dirs if d not in previous_dirs]


def assert_disk_guardrails(inv: dict, cycle_label: str):
    assert inv["var_core_files"] <= KERNEL_CRASH_MAX_VAR_CORE_FILES, (
        f"{cycle_label}: /var/core entries {inv['var_core_files']} > "
        f"{KERNEL_CRASH_MAX_VAR_CORE_FILES}"
    )
    assert inv["total_mb"] <= KERNEL_CRASH_MAX_ARTIFACT_MB, (
        f"{cycle_label}: crash artifact size {inv['total_mb']:.0f} MB > "
        f"{KERNEL_CRASH_MAX_ARTIFACT_MB} MB"
    )


def cleanup_kernel_crash_artifacts(engine):
    """Best-effort cleanup so later tests / coredump fixture are not polluted."""
    engine.run_cmd(f"sudo rm -rf {KERNEL_CRASH_COLLECTED_PATH}/*", validate=False)
    engine.run_cmd(
        f"sudo find {VAR_CRASH_ROOT} -mindepth 1 -maxdepth 1 "
        r"! -name collected -exec rm -rf {} +",
        validate=False,
    )
    engine.run_cmd(f"sudo rm -f {VAR_CORE_PATH}/*", validate=False)
    engine.run_cmd(f"sudo rm -f {CoreDumpConsts.COREDUMP_PATH}/*", validate=False)


def assert_crash_paths_empty_after_cleanup(engine):
    inv = crash_artifact_inventory(engine)
    assert inv["kdump_count"] == 0, (
        f"Expected no kdump dirs under {VAR_CRASH_ROOT} after cleanup, got {inv['kdump_dirs']}"
    )
    assert inv["var_core_files"] == 0, (
        f"Expected no files under {VAR_CORE_PATH} after cleanup"
    )


def stress_cycle_count() -> int:
    """Repeated panic cycles. Not tied to CONFIG_DB ``num_dumps`` (admin cap ≠ on-disk cleanup)."""
    if KERNEL_CRASH_STRESS_CYCLES > 0:
        return KERNEL_CRASH_STRESS_CYCLES
    return KERNEL_CRASH_STRESS_CYCLES_DEFAULT


# ---------------------------------------------------------------------------
# High-level orchestration (test_repeated_kernel_crash_core_dump_retention)
# ---------------------------------------------------------------------------


def precheck_kdump_for_stress(engine) -> dict:
    """
    Step 1: confirm kdump is enabled, ready, and decide how many stress cycles to run.

    Skips the test when kdump is not configured. ``num_dumps`` is logged for
    information only — this stress test does not assert against it.

    Returns:
        ``{"kdump_cfg": ..., "stress_cycles": int}``
    """
    kdump_cfg = get_kdump_config(engine)
    allure.attach("kdump_config", str(kdump_cfg))
    if kdump_cfg["num_dumps_source"] == "fallback_default":
        allure.attach(
            "kdump_num_dumps_fallback",
            f"num_dumps not parsed from CLI/CONFIG_DB; using default "
            f"{KERNEL_CRASH_KDUMP_NUM_DUMPS_FALLBACK}. "
            f"Override via KERNEL_CRASH_KDUMP_NUM_DUMPS_FALLBACK env if needed.",
        )
        logger.warning(
            "kdump num_dumps not parsed from CLI/CONFIG_DB; using fallback %s",
            KERNEL_CRASH_KDUMP_NUM_DUMPS_FALLBACK,
        )
    if not kdump_cfg["enabled"]:
        pytest.skip(
            "kdump not enabled on DUT: CONFIG_DB enabled is not true and no "
            "crashkernel= in /proc/cmdline. Enable kdump (config + reboot) before "
            f"this test. Details:\n{kdump_cfg['raw'][:1200]}"
        )
    if not kdump_cfg["ready"]:
        pytest.skip(
            "kdump not ready on DUT: no SONiC 'Operational State: Ready' and no "
            f"crashkernel reserved. Details:\n{kdump_cfg['raw'][:1200]}"
        )
    if kdump_cfg.get("config_db_enabled") is False and kdump_cfg["crashkernel_reserved"]:
        logger.warning(
            "CONFIG_DB KDUMP enabled=false but crashkernel is present; continuing"
        )
    stress_cycles = stress_cycle_count()
    logger.info(
        "kdump config num_dumps=%s (informational), stress_cycles=%s",
        kdump_cfg["num_dumps"],
        stress_cycles,
    )
    return {"kdump_cfg": kdump_cfg, "stress_cycles": stress_cycles}


def prepare_kernel_crash_stress_baseline(engine) -> None:
    """
    Step 2: baseline SSD free space before injecting repeated kernel panics.
    """
    avail_before = get_ssd_avail_gb(engine)
    allure.attach("ssd_avail_before_gb", str(avail_before))
    assert avail_before >= KERNEL_CRASH_MIN_SSD_AVAIL_GB, (
        f"Insufficient SSD before stress: {avail_before}G < {KERNEL_CRASH_MIN_SSD_AVAIL_GB}G"
    )


def run_kernel_crash_stress_cycle(
    system: System,
    engines,
    engine,
    topology_obj,
    devices,
    serial_engine: PexpectSerialEngine,
    cycle: int,
    stress_cycles: int,
    seen_kdump_dirs: Set[str],
) -> dict:
    """
    Run one kernel-panic cycle: sysrq trigger → reboot → kdump → log/SSD checks.

    Returns:
        ``{"summary": cycle_record, "seen_kdump_dirs": updated_set, "serial_engine": ...}``
    """
    cycle_label = f"cycle {cycle}"
    cycle_t0 = time.time()
    start_time = kernel_crash_start_time(system)
    serial_engine = serial_login_before_crash(
        topology_obj, devices, serial_engine, cycle
    )
    trigger_kernel_crash_via_serial(serial_engine)
    reboot_done = time.time()
    wait_after_kernel_crash_reboot(engines)
    assert_kernel_panic_reboot_reason(system)
    wait_for_kernel_crash_detection_logs(system, engines, start_time)
    techsupport_done = wait_for_auto_techsupport_logs(
        system,
        engines,
        start_time,
        KERNEL_CRASH_TECHSUPPORT_LOG_WAIT_SEC,
    )

    inv = crash_artifact_inventory(engine)
    avail_gb = get_ssd_avail_gb(engine)

    new_dirs = pick_new_kdump_dirs(inv["kdump_dirs"], seen_kdump_dirs)
    assert new_dirs, (
        f"{cycle_label}: expected new kdump dir under {VAR_CRASH_ROOT}, "
        f"dirs={inv['kdump_dirs']}, previously seen={seen_kdump_dirs}"
    )
    for dump_dir in new_dirs:
        assert_kdump_dir_sanity(engine, dump_dir)
    seen_kdump_dirs = set(seen_kdump_dirs)
    seen_kdump_dirs.update(inv["kdump_dirs"])

    cycle_record = {
        "cycle": cycle,
        **inv,
        "ssd_avail_gb": avail_gb,
        "new_kdump_dirs": new_dirs,
        "systemd_cores_info": inv["systemd_cores"],
        "elapsed_reboot_sec": round(reboot_done - cycle_t0, 1),
        "elapsed_techsupport_sec": round(techsupport_done - cycle_t0, 1),
    }
    allure.attach(
        f"kernel_crash_{cycle_label}",
        f"cycle={cycle}/{stress_cycles} record={cycle_record}",
    )

    assert_disk_guardrails(inv, cycle_label)
    assert avail_gb >= KERNEL_CRASH_MIN_SSD_AVAIL_GB, (
        f"{cycle_label}: SSD avail {avail_gb}G < {KERNEL_CRASH_MIN_SSD_AVAIL_GB}G"
    )
    return {
        "summary": cycle_record,
        "seen_kdump_dirs": seen_kdump_dirs,
        "serial_engine": serial_engine,
    }


def verify_system_healthy_after_kernel_crash_stress(system: System, stress_cycles: int) -> None:
    """Step 4: health must be OK after all repeated kernel panics."""
    health = OutputParsingTool.parse_json_str_to_dictionary(
        system.health.show()
    ).get_returned_value()
    assert health[HealthConsts.STATUS] == HealthConsts.OK, (
        f"Health not OK after {stress_cycles} kernel crashes: "
        f"{health[HealthConsts.STATUS]}"
    )


def verify_tech_support_after_kernel_crash_stress(
    system: System, engine, test_name: str
) -> str:
    """
    Step 5: manual tech-support must still succeed after the stress loop.

    Returns:
        Path to the generated .tar.gz (removed in cleanup).
    """
    tech_path, duration = system.techsupport.action_generate(
        engine, test_name=test_name
    )
    assert isinstance(tech_path, str) and tech_path.endswith(".tar.gz"), (
        f"tech-support failed after repeated crashes: {tech_path!r} duration={duration}"
    )
    sz = engine.run_cmd(f'stat -c %s "{tech_path}"', validate=False).strip()
    assert sz.isdigit() and int(sz) > 0, (
        f"tech-support tarball empty: {tech_path} stat={sz!r}"
    )
    return tech_path


def cleanup_kernel_crash_stress(
    system: System, engine, tech_support_tar: str | None = None
) -> None:
    """
    Step 6: remove crash artifacts and any tech-support tarball from this test.
    """
    cleanup_kernel_crash_artifacts(engine)
    if isinstance(tech_support_tar, str) and tech_support_tar.endswith(".tar.gz"):
        engine.run_cmd(f'sudo rm -f "{tech_support_tar}"', validate=False)
    else:
        ts = system.techsupport
        if getattr(ts, "file_name", None):
            ts.files.file_name[ts.file_name].action_delete()
    assert_crash_paths_empty_after_cleanup(engine)


def verify_techsupport_files_names(techsupport_files_list, expected_patterns_list):
    files_search_errors = {}
    for expected_file_pattern in expected_patterns_list:
        if not any(expected_file_pattern.match(file) for file in techsupport_files_list):
            files_search_errors[expected_file_pattern] = (
                f'file "{expected_file_pattern}" was not found'
            )
    err = ",\n".join(list(files_search_errors.values()))
    assert not files_search_errors, f"The following files weren't found:\n{err}"


def verify_techsupport_files_sizes(engine, techsupport_file_name):
    system = System()
    files_list = system.techsupport.get_techsupport_empty_files(
        engine, techsupport_file_name, "kdump"
    )
    assert len(files_list) == 0, f"the next files are unexpectedly empty {files_list}"
