"""
Helper functions for the test ``test_repeated_fatal_fw_dumps_rotation_and_cleanup``.

What this test checks (in plain terms):
  When the switch hits repeated firmware (FW) fatal errors, it should save
  debug dump files (sai-dfw-*.tar.gz) under /var/log/mellanox/. Old dumps
  should be rotated out when the limit is reached. The switch should recover
  after each fatal, tech-support should still work, and health should return
  to OK.

This file contains all the logic for that test. You can tune timing and limits
with environment variables named FATAL_DUMP_* (for example FATAL_DUMP_ROTATION_CYCLES).

Some functions call back into test_fatal_mode.py for shared fatal-mode actions
(simulate event, check syncd restart, etc.). That import is done lazily inside
functions so Python does not get stuck in a circular import at startup.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from ngts.nvos_constants.constants_nvos import HealthConsts
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)

FATAL_DUMP_ROTATION_CYCLES = int(os.environ.get("FATAL_DUMP_ROTATION_CYCLES", "5"))
# Aligned with test_fatal_mode.WAIT_BETWEEN_EVENTS_SECONDS (20s): the canonical fatal path
# notes "fatal doesn't work if we don't wait between events"; too-short spacing risks the two
# CATAS events not both registering, delaying or preventing fatal onset.
FATAL_DUMP_WAIT_BETWEEN_EVENTS_SEC = int(os.environ.get("FATAL_DUMP_WAIT_BETWEEN_EVENTS_SEC", "20"))
FATAL_DUMP_MAX_FILES = int(os.environ.get("FATAL_DUMP_MAX_FILES", "20"))
FATAL_DUMP_MAX_TOTAL_MB = int(os.environ.get("FATAL_DUMP_MAX_TOTAL_MB", "600"))
FATAL_DUMP_GROWTH_WAIT_SEC = int(os.environ.get("FATAL_DUMP_GROWTH_WAIT_SEC", "90"))
FATAL_DUMP_CLEAR_TIME_MIN = int(os.environ.get("FATAL_DUMP_CLEAR_TIME_MIN", "2"))
FATAL_DUMP_CLEAR_EXTRA_SEC = int(os.environ.get("FATAL_DUMP_CLEAR_EXTRA_SEC", "5"))
FATAL_DUMP_EVICTION_ASSERT_FROM_COUNT = int(os.environ.get("FATAL_DUMP_EVICTION_ASSERT_FROM_COUNT", "3"))
FATAL_DUMP_REQUIRE_EVICTION_OBSERVED = os.environ.get("FATAL_DUMP_REQUIRE_EVICTION_OBSERVED", "").lower() in (
    "1", "true", "yes",
)
SAI_DFW_GLOB = (
    "/var/log/mellanox/sdk-dumps_dev*/sai-dfw-*.tar.gz "
    "/var/log/mellanox/sdk-wrn-dumps_dev*/sai-dfw-*.tar.gz"
)
SAI_DFW_DUMP_DIR_GLOB = "/var/log/mellanox/sdk-dumps_dev* /var/log/mellanox/sdk-wrn-dumps_dev*"


def fw_inject_events_for_fatal() -> list[int]:
    """
    Decide which fake health events to inject in each test cycle.

    To trigger fatal mode we need at least two fatal health events close together.
    This test uses event type 5 twice (SAI cause=5), which simulates a serious
    firmware health failure on the chosen ASIC.

    Returns:
        [5, 5] — inject event 5, wait, then inject event 5 again.
    """
    return [5, 5]


def prepare_fatal_dump_rotation_baseline(engine) -> None:
    """
    Step 1 of the test: start from a clean slate.

    Deletes any old sai-dfw dump files left on the switch from earlier runs,
    then checks that none are left (count should be 0). Also checks that the
    dump folders under /var/log/mellanox/ actually exist — if they are missing,
    the switch cannot create new dump files and the test would fail later.

    Args:
        engine: SSH connection to the switch (engines.dut).
    """
    clean_sai_dfw_dumps(engine)
    assert count_sai_dfw_dumps(engine) == 0, "Expected no sai-dfw dumps after baseline cleanup"
    assert_sai_dfw_dump_directories_exist(engine)


def configure_fatal_dump_rotation_settings(events_count_setting: int) -> dict[str, Any]:
    """
    Step 2 of the test: set up fatal-mode settings on the switch.

    Tells the FAE (fatal auto-escalation) feature how to behave: how many events
    count as fatal, how long until fatal clears, etc. We always use at least 2
    events per window because this test injects two health events per cycle.
    Settings are applied and saved on the DUT like other fatal-mode tests.

    Args:
        events_count_setting: Random value from the test fixture (1 or 2).

    Returns:
        A small dict the test uses in the loop: how many events, and which ones.
    """
    fatal_events_count = max(2, events_count_setting)
    fw_inject_events = fw_inject_events_for_fatal()
    _fatal_mode()._set_settings(
        reboot_count=2,
        clear_time=FATAL_DUMP_CLEAR_TIME_MIN,
        events_count=fatal_events_count,
    )
    return {
        "fatal_events_count": fatal_events_count,
        "fw_inject_events": fw_inject_events,
    }


def run_fatal_dump_rotation_cycle(
    engine,
    random_asic: int,
    cycle: int,
    fw_inject_events: list[int],
) -> dict[str, Any]:
    """
    Run one full repeat of: cause fatal → check dump → wait for recovery.

    In each cycle we:
      1. Note how many dump files exist now.
      2. Inject health events so the switch enters fatal (soft reset).
      3. Wait until a new sai-dfw dump appears (or an old one is replaced).
      4. Check the dump files are valid and rotation rules are respected.
      5. Wait for fatal to clear, then check the switch is healthy again.

    Saves before/after details to the Allure report for debugging.

    Args:
        engine: SSH connection to the switch.
        random_asic: Which ASIC to inject on (e.g. ASIC1, ASIC2).
        cycle: Which repetition this is (1, 2, 3, ...).
        fw_inject_events: List of events to inject (usually [5, 5]).

    Returns:
        Summary of this cycle plus whether we saw old dumps removed and new ones added.
    """
    fm = _fatal_mode()
    paths_before = list_sai_dfw_dump_paths(engine)
    count_before = len(paths_before)
    # Anchor activity detection to injection start so pre-existing dumps or
    # unrelated filesystem churn cannot be mistaken for this cycle's rotation.
    # Use DUT clock (same domain as newest_sai_dfw_dump_mtime / stat -c %Y).
    since_epoch = int(engine.run_cmd("date +%s", validate=False).strip())

    with allure.step(
        f"Inject FW health events {fw_inject_events} on ASIC{random_asic} "
        f"and trigger System-Fatal soft-reset"
    ):
        trigger_dump_rotation_soft_reset(
            random_asic, fw_inject_events, FATAL_DUMP_WAIT_BETWEEN_EVENTS_SEC,
        )

    with allure.step("Wait for sai-dfw dump creation or rotation"):
        wait_for_sai_dfw_dump_activity(
            engine, count_before, timeout_sec=FATAL_DUMP_GROWTH_WAIT_SEC,
            since_epoch=since_epoch,
        )

    paths_after = list_sai_dfw_dump_paths(engine)
    count_after = len(paths_after)
    new_paths = sorted(set(paths_after) - set(paths_before))
    removed_paths = sorted(set(paths_before) - set(paths_after))
    rotation_observed = bool(removed_paths and new_paths)

    summary = {
        "cycle": cycle,
        "before": count_before,
        "after": count_after,
        "new": new_paths,
        "removed": removed_paths,
    }
    allure.attach(
        f"fatal_dump_cycle_{cycle}",
        f"before={count_before} after={count_after} new={new_paths} removed={removed_paths} "
        f"max_files={FATAL_DUMP_MAX_FILES}",
    )

    with allure.step(f"Validate sai-dfw dump count and archive integrity (cycle {cycle})"):
        assert count_after > 0, (
            f"Cycle {cycle}: expected at least one sai-dfw dump after fatal, got 0"
        )
        assert count_after <= FATAL_DUMP_MAX_FILES, (
            f"Cycle {cycle}: sai-dfw count {count_after} exceeds cap {FATAL_DUMP_MAX_FILES} "
            "(rotation/cleanup may be broken)"
        )
        assert_sai_dfw_archives_valid(engine, new_paths if new_paths else paths_after)
        assert_sai_dfw_rotation_at_retention(
            paths_before, paths_after, count_before, count_after, cycle,
        )

    with allure.step(f"Wait for fatal clear-time and verify post-cycle recovery (cycle {cycle})"):
        wait_dump_rotation_exit_fatal(FATAL_DUMP_CLEAR_TIME_MIN, FATAL_DUMP_CLEAR_EXTRA_SEC)
        assert_post_fatal_cycle_recovery(engine)
        fm._reset_base_prompt(engine)

    return {"summary": summary, "rotation_observed": rotation_observed}


def assert_fatal_dump_rotation_inventory(
    engine,
    per_cycle_counts: list[dict],
    rotation_observed: bool,
) -> None:
    """
    After all cycles: make sure we did not accumulate too many dump files.

    Checks two things on the switch:
      - Total number of sai-dfw files is not above the allowed maximum.
      - Total disk space used by those files is not above the MB limit.

    Optionally (if FATAL_DUMP_REQUIRE_EVICTION_OBSERVED=true) also checks that
    at least one cycle actually deleted an old dump and added a new one — proof
    that rotation/cleanup is working, not just piling up files.

    Args:
        engine: SSH connection to the switch.
        per_cycle_counts: Results collected from each cycle in the loop.
        rotation_observed: True if we ever saw an old file removed and a new one added.
    """
    final_count = count_sai_dfw_dumps(engine)
    total_mb = total_sai_dfw_size_mb(engine)
    allure.attach(
        "fatal_dump_rotation_summary",
        f"cycles={FATAL_DUMP_ROTATION_CYCLES} per_cycle={per_cycle_counts} "
        f"final_count={final_count} total_mb={total_mb:.1f} max_mb={FATAL_DUMP_MAX_TOTAL_MB}",
    )
    assert final_count <= FATAL_DUMP_MAX_FILES, (
        f"Final sai-dfw count {final_count} > {FATAL_DUMP_MAX_FILES}"
    )
    assert total_mb <= FATAL_DUMP_MAX_TOTAL_MB, (
        f"Total sai-dfw size {total_mb:.1f} MB > {FATAL_DUMP_MAX_TOTAL_MB} MB"
    )
    if FATAL_DUMP_REQUIRE_EVICTION_OBSERVED:
        assert rotation_observed, (
            f"No sai-dfw eviction observed in {FATAL_DUMP_ROTATION_CYCLES} cycles "
            f"(platform retention may exceed cycle count; increase cycles or set "
            f"FATAL_DUMP_REQUIRE_EVICTION_OBSERVED=false)"
        )


def verify_tech_support_after_fatal_cycles(engine, test_name: str) -> str:
    """
    Check that tech-support still works after many fatal events.

    Runs ``nv action generate`` to create a tech-support bundle on the switch.
    We expect a .tar.gz file that is not empty, and that contains fatal-related
    info (dump/fatal_reason). If tech-support breaks after repeated fatal events, this
    step fails.

    Args:
        engine: SSH connection to the switch.
        test_name: Name of the pytest test (used when naming the tarball).

    Returns:
        Path to the tech-support .tar.gz on the switch (deleted in cleanup).
    """
    tech_support_tar, duration = System().techsupport.action_generate(engine, test_name=test_name)
    assert isinstance(tech_support_tar, str) and tech_support_tar.endswith(".tar.gz"), (
        f"tech-support path invalid: {tech_support_tar!r} duration={duration}"
    )
    sz = send_command_timing(engine, f'stat -c %s "{tech_support_tar}"').strip()
    assert sz.isdigit() and int(sz) > 0, (
        f"Tech-support tarball empty: {tech_support_tar} stat={sz!r}"
    )
    _fatal_mode()._assert_overlap_tech_support_tarball_lists_fatal_reason(engine, tech_support_tar)
    return tech_support_tar


def verify_system_healthy_after_fatal_cycles() -> None:
    """
    Final sanity check: the switch should be fully healthy, not stuck in fatal.

    Verifies the switch is no longer in System-Fatal (prompt, LED, health status),
    then runs ``nv show system health`` and expects status OK — not just "not
    fatal". After many soft resets, the switch can look recovered but still
    report a health problem; this catches that.
    """
    fm = _fatal_mode()
    fm._assert_system_fatal_mode(False, state_just_changed=False)
    health = OutputParsingTool.parse_json_str_to_dictionary(
        System().health.show()
    ).get_returned_value()
    assert health[HealthConsts.STATUS] == HealthConsts.OK, (
        f"Expected health OK after repeated fatal cycles, got {health[HealthConsts.STATUS]}"
    )


def cleanup_fatal_dump_tech_support(engine, tech_support_tar: str | None) -> None:
    """
    Delete the tech-support file we created so it does not fill up disk.

    Runs in a finally block so cleanup happens whether the test passes or fails.
    Does nothing if no tarball was created (path is None).

    Args:
        engine: SSH connection to the switch.
        tech_support_tar: Path from verify_tech_support_after_fatal_cycles, or None.
    """
    if isinstance(tech_support_tar, str) and tech_support_tar.endswith(".tar.gz"):
        send_command_timing(engine, f'sudo rm -f "{tech_support_tar}"')


# ---------------------------------------------------------------------------
# Low-level helpers (used only by this test flow)
# ---------------------------------------------------------------------------


def _fatal_mode():
    """
    Load test_fatal_mode.py only when we need it (not at the top of this file).

    test_fatal_mode imports this helpers file, and this file needs functions
    from test_fatal_mode — so we import inside this function to avoid Python
    import errors. Use this when you need simulate_event, syncd checks, etc.
    """
    from ngts.tests_nvos.multi_asic import test_fatal_mode as fm
    return fm


def send_command_timing(engine, cmd: str) -> str:
    """
    Run a shell command on the switch when the prompt may be unusual.

    During fatal mode the CLI prompt can show [System_Fatal_State]. A normal
    SSH "wait for prompt" can hang or get confused. This runs the command with
    a timing-based approach instead (same as other fatal-mode tests).

    Args:
        engine: SSH connection to the switch.
        cmd: The shell command to run.

    Returns:
        The command output as text.
    """
    return _fatal_mode()._send_command_timing(engine, cmd)


def clean_sai_dfw_dumps(engine) -> None:
    """
    Remove all sai-dfw dump .tar.gz files from the switch.

    Looks in both fatal and warning dump folders under /var/log/mellanox/.
    Used at the start of the test so we count only dumps created during this run.

    Args:
        engine: SSH connection to the switch.
    """
    send_command_timing(engine, f"sudo rm -f {SAI_DFW_GLOB}")


def list_sai_dfw_dump_paths(engine) -> list[str]:
    """
    Get a list of every sai-dfw dump file path currently on the switch.

    Runs ls on the mellanox dump directories. Returns full paths like
    /var/log/mellanox/sdk-dumps_dev0/sai-dfw-....tar.gz. Empty list means
    no dump files found (normal right after cleanup).

    Args:
        engine: SSH connection to the switch.

    Returns:
        List of file paths (strings).
    """
    out = send_command_timing(
        engine,
        f"sudo ls -1 {SAI_DFW_GLOB} 2>/dev/null || true",
    )
    return [line.strip() for line in out.splitlines() if line.strip().startswith("/")]


def count_sai_dfw_dumps(engine) -> int:
    """
    How many sai-dfw dump files are on the switch right now?

    Same as len(list_sai_dfw_dump_paths(...)). Handy when you only need a number,
    not the actual file names.

    Args:
        engine: SSH connection to the switch.

    Returns:
        Number of dump files (0 or more).
    """
    return len(list_sai_dfw_dump_paths(engine))


def total_sai_dfw_size_mb(engine) -> float:
    """
    How much disk space (in MB) do all sai-dfw dumps use together?

    Adds up the size of every sai-dfw .tar.gz file. Returns 0 if there are none.
    The test compares this to FATAL_DUMP_MAX_TOTAL_MB so dumps do not fill /var/log.

    Args:
        engine: SSH connection to the switch.

    Returns:
        Total size in megabytes.
    """
    out = send_command_timing(
        engine,
        f"sudo du -cb {SAI_DFW_GLOB} 2>/dev/null | tail -1 | awk '{{print $1}}' || echo 0",
    ).strip()
    if not out.isdigit():
        return 0.0
    return int(out) / (1024 * 1024)


def newest_sai_dfw_dump_mtime(engine) -> int:
    """
    Return the newest (largest) mtime, in epoch seconds, across all sai-dfw dump
    files, or 0 when no dump files exist.

    Used to confirm that observed dump "activity" actually happened after event
    injection (mtime > injection start) rather than being pre-existing files or
    unrelated filesystem churn.

    Args:
        engine: SSH connection to the switch.

    Returns:
        Newest dump file mtime in epoch seconds (int), or 0 if none.
    """
    out = send_command_timing(
        engine,
        f"sudo stat -c %Y {SAI_DFW_GLOB} 2>/dev/null | sort -n | tail -1 || echo 0",
    ).strip()
    return int(out) if out.isdigit() else 0


def wait_for_sai_dfw_dump_activity(
    engine, count_before: int, timeout_sec: int, since_epoch: int = None
) -> None:
    """
    Wait until the switch creates or updates a sai-dfw dump file.

    After injecting fatal events, the FW should write a dump. We poll every 10s
    and stop when any of these happens:
      - First dump appeared (count went from 0 to 1+).
      - Count went up (new file added).
      - Count stayed the same for a while at the limit (old file replaced — rotation).

    To avoid false positives from pre-existing dumps or unrelated filesystem
    churn, an observation only counts as activity when the newest dump file's
    mtime is strictly after ``since_epoch`` (the moment just before injection).

    Fails if nothing happens before timeout_sec — often means dump creation failed.

    Args:
        engine: SSH connection to the switch.
        count_before: How many dump files existed before this cycle.
        timeout_sec: How long to wait (see FATAL_DUMP_GROWTH_WAIT_SEC).
        since_epoch: Epoch seconds captured just before injection. When provided,
            only dumps written after this moment are treated as activity.
    """
    since_epoch = int(since_epoch or 0)
    min_stable_sec = min(45, max(15, timeout_sec // 4))
    deadline = time.time() + timeout_sec
    stable_since = None
    last_count = count_before
    while time.time() < deadline:
        last_count = count_sai_dfw_dumps(engine)
        newest_mtime = newest_sai_dfw_dump_mtime(engine)
        # Only accept changes that happened after injection started.
        post_injection = (not since_epoch) or newest_mtime > since_epoch
        if count_before == 0 and last_count > 0 and post_injection:
            logger.info("sai-dfw: first dump appeared (count=%s)", last_count)
            return
        if count_before > 0 and last_count > count_before and post_injection:
            logger.info("sai-dfw: count grew %s -> %s", count_before, last_count)
            return
        if (count_before > 0 and last_count == count_before and last_count > 0 and
                post_injection):
            now = time.time()
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= min_stable_sec:
                logger.info(
                    "sai-dfw: count stable at %s for %ss (rotation may have replaced oldest)",
                    last_count,
                    int(now - stable_since),
                )
                return
        else:
            stable_since = None
        time.sleep(10)
    assert False, (
        f"No sai-dfw dump activity within {timeout_sec}s "
        f"(before={count_before} last={last_count})"
    )


def assert_sai_dfw_dump_directories_exist(engine) -> None:
    """
    Make sure the folders where dumps are stored actually exist.

    Dumps go under /var/log/mellanox/sdk-dumps_dev* (and warning variants).
    If those directories are missing, the switch cannot save a dump and the
    test would time out later with a unclear error — so we check early.

    Args:
        engine: SSH connection to the switch.
    """
    out = send_command_timing(
        engine,
        f"sudo ls -d {SAI_DFW_DUMP_DIR_GLOB} 2>/dev/null || true",
    )
    dirs = []
    for line in out.splitlines():
        dirs.extend(part for part in line.split() if part.startswith("/"))
    assert dirs, (
        f"Expected at least one path matching {SAI_DFW_DUMP_DIR_GLOB}; "
        f"dump generation may fail without these directories"
    )
    logger.info("SDK dump directories: %s", dirs)


def assert_sai_dfw_archives_valid(engine, paths: list[str]) -> None:
    """
    Check that dump files are real, non-empty tar archives — not broken files.

    For each .tar.gz: file size must be > 0, and tar -tf must list contents
    without errors. Catches empty or corrupted dumps before we trust rotation logic.

    Args:
        engine: SSH connection to the switch.
        paths: Which files to check; if empty, checks all dumps on the switch.
    """
    targets = paths or list_sai_dfw_dump_paths(engine)
    assert targets, "No sai-dfw paths to validate"
    for archive in targets:
        sz = send_command_timing(engine, f'sudo stat -c %s "{archive}"').strip()
        assert sz.isdigit() and int(sz) > 0, (
            f"sai-dfw archive empty or missing: {archive} stat={sz!r}"
        )
        listing = send_command_timing(
            engine, f'sudo tar -tf "{archive}" 2>&1 | head -5',
        )
        assert listing.strip() and "Error" not in listing and "error" not in listing[:80], (
            f"sai-dfw archive not readable: {archive}; tar output: {listing[:500]!r}"
        )


def assert_sai_dfw_rotation_at_retention(
    paths_before: list[str],
    paths_after: list[str],
    count_before: int,
    count_after: int,
    cycle: int,
) -> None:
    """
    When the switch is at its dump file limit, old files must be replaced — not ignored.

    If we already have several dumps and the count does not go up after a new fatal,
    we expect rotation: at least one old file deleted and one new file added.
    Skipped when we still have few files (below FATAL_DUMP_EVICTION_ASSERT_FROM_COUNT)
    or when the count increased (still room for more files).

    Args:
        paths_before: Dump file paths before this cycle.
        paths_after: Dump file paths after waiting for the new dump.
        count_before: Number of files before.
        count_after: Number of files after.
        cycle: Cycle number (for error messages).
    """
    if count_before < FATAL_DUMP_EVICTION_ASSERT_FROM_COUNT:
        return
    if count_after > count_before:
        return
    removed = set(paths_before) - set(paths_after)
    added = set(paths_after) - set(paths_before)
    assert removed, (
        f"Cycle {cycle}: at retention (before={count_before} after={count_after}) "
        f"expected oldest sai-dfw removed; before={paths_before} after={paths_after}"
    )
    assert added, (
        f"Cycle {cycle}: at retention expected new sai-dfw after eviction; "
        f"added={added}"
    )


def trigger_dump_rotation_soft_reset(asic: int, events: list, between_events_sec: int) -> None:
    """
    Fake firmware health failures on one ASIC until the switch enters fatal mode.

    Injects each event in the list (e.g. [5, 5]), waiting between_events_sec
    between them. Then checks that syncd/swss restarted (soft reset) and the
    switch shows System-Fatal in health, LED, and CLI prompt.

    Args:
        asic: Which ASIC to hit (1 = first ASIC, 2 = second, etc.).
        events: List of event types to inject in order.
        between_events_sec: Seconds to wait between events (not after the last one).
    """
    fm = _fatal_mode()
    for i, event_id in enumerate(events):
        fm._simulate_event(event_id, asic)
        if i < len(events) - 1:
            fm._wait(0, between_events_sec)
    fm._assert_syncd_restart()
    fm._assert_system_fatal_mode(True, True)
    fm._wait(0, 10)


def wait_dump_rotation_exit_fatal(minutes: int, extra_seconds: int) -> None:
    """
    Wait for fatal mode to clear on its own, then check the switch recovered.

    Fatal mode has a configured "clear time" — after enough minutes without new
    problems, the switch should leave System-Fatal. We sleep for that time plus
    a small extra buffer, then verify health/prompt are no longer fatal.

    Args:
        minutes: How long fatal is configured to last (clear-time).
        extra_seconds: Extra seconds to wait after that (safety margin).
    """
    fm = _fatal_mode()
    fm._wait(minutes, seconds=extra_seconds)
    fm._assert_system_fatal_mode(False, state_just_changed=False)


def assert_post_fatal_cycle_recovery(engine) -> None:
    """
    After fatal clears, make sure the switch is really usable — not just "not fatal".

    Checks:
      - Not in fatal mode anymore.
      - NVOS CLI is working again.
      - Every syncd and swss docker for each ASIC is running.

    Sometimes fatal clears but containers stay down; this step catches that.

    Args:
        engine: SSH connection to the switch.
    """
    fm = _fatal_mode()
    fm._assert_system_fatal_mode(False, state_just_changed=False)
    DutUtilsTool.wait_for_nvos_to_become_functional(engine).verify_result()
    dockers = {
        f"{name}{asic}"
        for name in ("syncd-ibv0", "swss-ibv0")
        for asic in range(TestToolkit.devices.dut.asic_amount)
    }
    running = set(DutUtilsTool.get_running_dockers(engine)) & dockers
    missing = dockers - running
    assert not missing, (
        f"After fatal recovery expected containers running, missing: {missing}; "
        f"running relevant: {running}"
    )
