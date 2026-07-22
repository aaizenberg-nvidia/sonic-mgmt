"""Counters Historical Analysis verification suite.

Covers the new ``system stats`` categories interface / peer-port / transceivers:
configuration, behavioral reflection of interval/state/history-duration into the
next sampling cycles, sampling and report generation, file content, per-platform
pruning, clear, tech-support, reboot/factory-reset persistence, error flows, and
the long-running resilience / stress / scale / CPU / file-size / upgrade checks.

Test ordering:
  * Group A - Basic / positive functionality (top of file).
  * Group B - Negative tests.
  * Group C - Long-running / resilience / performance / scale (bottom of file).

TBD values (CPU limits, soak duration, cadence tolerance, per-day file date
format) are single-edit-point constants tagged with TODO in StatsConsts.
"""
from pathlib import Path
import logging
import os
import time

import pytest

from ngts.ngts_types import DevicesT, EnginesT

from ngts.nvos_constants.constants_nvos import ApiType, NvosConst
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.system.stats.constants import StatsConsts
from ngts.tests_nvos.system.stats import counters_historical_helpers as ch
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.helpers.general_helpers import verify_result_obj_failure
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


# Categories a platform cannot have, keyed on the ASIC it runs. Peer-port counters
# come from the GPUs behind the switch, which NVL5 has none of and NVL6 does. Kept
# with the test rather than declared on the device classes, so that asserting it
# costs no change to shared framework code. An ASIC that is not listed states no
# expectation at all, rather than a wrong one.
CATEGORIES_ABSENT_BY_ASIC = {
    NvosConst.NVL5: (StatsConsts.PEER_PORT,),
}

# Scratch copy for the gzip ratio check, so the per-day file is left alone.
GZIP_CHECK_COPY = "/tmp/stats_gzip_ratio_check.csv"


def _require_supported_categories(system):
    """Return the supported new categories, or skip the test if none exist."""
    supported = ch.get_supported_new_categories(system)
    if not supported:
        pytest.skip("No Counters Historical categories (interface/peer-port/transceivers) "
                    "supported on this platform")
    return supported


def _get_hostname(system):
    system_show = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
    return system_show['hostname'].replace('-mgmt2', '')


def _measure_cpu_and_rss(engine, label):
    """Measure CPU over the standard window plus resident memory, and log both.

    The measured figures are the deliverable of the resource tests (the limits
    they are checked against were derived from earlier runs), so they are logged
    at info level to be readable straight from the run log.
    """
    cpu = ch.measure_stats_reportd_cpu(engine, sample_seconds=StatsConsts.CPU_MEASURE_WINDOW_SEC)
    rss_mb = ch.measure_stats_reportd_rss_mb(engine)
    logger.info("stats-reportd with %s: CPU %.4f%% (avg over %ss), memory %.1fMB",
                label, cpu, StatsConsts.CPU_MEASURE_WINDOW_SEC, rss_mb)
    return cpu, rss_mb


def _enable_legacy_categories_at_min_interval(system, devices):
    """Force every pre-existing category to the minimum interval, so the switch
    runs the heaviest configuration the feature allows.
    """
    legacy = getattr(devices.dut, 'category_list', [])
    with allure.step("Enable legacy categories {} at interval {}".format(
            legacy, StatsConsts.INTERVAL_MIN)):
        for name in legacy:
            ch.set_category_config(system, name, interval=int(StatsConsts.INTERVAL_MIN),
                                   state=StatsConsts.State.ENABLED.value, apply=True)


def _pick_sampling_category(supported):
    """Pick a category that reliably samples on any switch (for cadence / state /
    retention checks): prefer interface (always has ports), then transceivers,
    else a random supported one. Avoids drawing peer-port on setups with no
    connected peer, where it is present but produces no data.
    """
    for preferred in (StatsConsts.INTERFACE, StatsConsts.TRANSCEIVERS):
        if preferred in supported:
            return preferred
    return RandomizationTool.select_random_value(supported).get_returned_value()


# ============================================================================ #
# Group A - Basic / positive functionality tests (QA-first)
# ============================================================================ #
@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_configuration(engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: new-category defaults (5 / 7 / enabled), set/unset, and
    valid interval + history-duration boundaries are accepted and reflected in
    ``nv show``. Invalid inputs are covered in test_counters_historical_error_flows.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        supported = _require_supported_categories(system)

        with allure.step("Set stats feature to default"):
            system.stats.unset(apply=True).verify_result()

        with allure.step("Verify new-category defaults (interval 5, history-duration 7, enabled)"):
            for name in supported:
                show = ch.get_category_show(system, name)
                ValidationTool.compare_dictionary_content(
                    show, StatsConsts.NEW_CATEGORY_DEFAULT_DICT).verify_result()

        for name in supported:
            with allure.step("Configure category {}".format(name)):
                with allure.step("Set interval boundaries (min / max / mid)"):
                    for value in (StatsConsts.NEW_INTERVAL_MIN, StatsConsts.NEW_INTERVAL_MAX,
                                  StatsConsts.NEW_INTERVAL_MID):
                        system.stats.category.categoryName[name].set(
                            op_param_name=StatsConsts.INTERVAL, op_param_value=int(value),
                            apply=True).verify_result()
                        show = ch.get_category_show(system, name)
                        assert show[StatsConsts.INTERVAL] == str(value), \
                            "interval not reflected for {}: {}".format(name, show)

                with allure.step("Set history-duration boundaries (min / max)"):
                    for value in (StatsConsts.NEW_HISTORY_DURATION_MIN,
                                  StatsConsts.NEW_HISTORY_DURATION_MAX):
                        system.stats.category.categoryName[name].set(
                            op_param_name=StatsConsts.HISTORY_DURATION, op_param_value=int(value),
                            apply=True).verify_result()
                        show = ch.get_category_show(system, name)
                        assert show[StatsConsts.HISTORY_DURATION] == str(value), \
                            "history-duration not reflected for {}: {}".format(name, show)

                with allure.step("Disable then unset -> back to defaults"):
                    ch.disable_category(system, name, apply=True)
                    assert ch.get_category_show(system, name)[StatsConsts.STATE] == \
                        StatsConsts.State.DISABLED.value
                    system.stats.category.categoryName[name].unset(apply=True).verify_result()
                    ValidationTool.compare_dictionary_content(
                        ch.get_category_show(system, name),
                        StatsConsts.NEW_CATEGORY_DEFAULT_DICT).verify_result()
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_interval_reflection(engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: the configured ``interval`` is actually applied to sampling.

    Measures the real gap between consecutive per-day rows and asserts it matches
    the configured interval; then changes the interval and asserts the cadence
    changes accordingly (not just the ``nv show`` echo).
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        supported = _require_supported_categories(system)
        name = _pick_sampling_category(supported)

        for interval in (StatsConsts.REFLECTION_INTERVAL_A, StatsConsts.REFLECTION_INTERVAL_B):
            with allure.step("interval={} min cadence".format(interval)):
                with allure.step("Enable {} at interval {}, then clear".format(name, interval)):
                    ch.set_cache_duration(engine, 1)
                    ch.enable_category(system, name, interval=interval)
                    ch.restart_sampler(engine)
                    # Clear after the restart, not before: the still-running daemon
                    # writes a stray sample between a pre-restart clear and the restart.
                    system.stats.category.categoryName[name].action_general(
                        StatsConsts.CLEAR).verify_result()

                with allure.step("Collect a few cycles"):
                    time.sleep(interval * 60 * 3 + 30)

                with allure.step("Assert measured cadence matches the configured interval"):
                    gaps = ch.measure_sample_cadence(engine, name)
                    assert gaps, "No sample cadence measured for {}".format(name)
                    # The first gap covers daemon start-up rather than steady state.
                    steady_gaps = gaps[1:] or gaps
                    expected = interval * 60
                    tolerance = StatsConsts.SAMPLE_CADENCE_TOLERANCE_SEC
                    off = [g for g in steady_gaps if abs(g - expected) > tolerance]
                    assert not off, (
                        "{} sampling gaps {} deviate from interval {}s by more than {}s".format(
                            name, off, expected, tolerance))
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_state_reflection(engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: the ``state`` field really gates collection.

    Disabling a category stops new samples being written; enabling it resumes
    collection (row count grows again).
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        supported = _require_supported_categories(system)
        name = _pick_sampling_category(supported)

        with allure.step("Enable {} and confirm sampling starts".format(name)):
            ch.set_cache_duration(engine, 1)
            ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN))
            ch.restart_sampler(engine)
            assert ch.wait_for_new_sample(engine, name, timeout_sec=180), \
                "No samples written after enabling {}".format(name)

        with allure.step("Disable {} and confirm sampling stops".format(name)):
            ch.disable_category(system, name, apply=True)
            ch.wait_one_cycle(StatsConsts.NEW_INTERVAL_MIN)
            baseline = ch.count_data_rows(engine, name)
            ch.wait_one_cycle(StatsConsts.NEW_INTERVAL_MIN)
            assert ch.count_data_rows(engine, name) == baseline, \
                "Samples kept growing after disabling {}".format(name)

        with allure.step("Re-enable {} and confirm sampling resumes".format(name)):
            ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN))
            assert ch.wait_for_new_sample(engine, name, timeout_sec=180), \
                "Sampling did not resume after re-enabling {}".format(name)
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_retention_reflection(engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: ``history-duration`` retention is really enforced.

    Seeds a per-day file dated older than the configured history-duration,
    triggers a retention cycle, and asserts the old day-file is pruned while
    in-window files remain.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        supported = _require_supported_categories(system)
        name = _pick_sampling_category(supported)
        history = StatsConsts.REFLECTION_HISTORY_DURATION

        with allure.step("Enable {} with history-duration {}".format(name, history)):
            ch.set_cache_duration(engine, 1)
            ch.set_category_config(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN),
                                   history_duration=history,
                                   state=StatsConsts.State.ENABLED.value, apply=True)
            ch.restart_sampler(engine)
            assert ch.wait_for_new_sample(engine, name, timeout_sec=180), \
                "No samples written for {}".format(name)

        with allure.step("Seed an out-of-window day-file and record in-window ones"):
            recent_files = ch.list_per_day_files(engine, name)
            old_file = ch.seed_old_day_file(engine, name, StatsConsts.SEED_OLD_FILE_AGE_DAYS)
            assert old_file in ch.list_per_day_files(engine, name), "Seed of old day-file failed"

        with allure.step("Trigger a retention cycle via a clock step and restart"):
            with ch.step_clock_forward_and_restore(engine, days=1):
                ch.restart_sampler(engine)
                ch.wait_one_cycle(StatsConsts.NEW_INTERVAL_MIN)

        with allure.step("Assert old day-file pruned; in-window files kept"):
            ch.assert_day_files_pruned(engine, name, history, kept_files=recent_files)
            assert old_file not in ch.list_per_day_files(engine, name), \
                "Old day-file {} was not pruned for {}".format(old_file, name)
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_sampling_and_report(engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: end-to-end per-day sampling -> generate -> show/upload/delete,
    and the report equals the concatenation of the per-day internal files.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    player = engines['sonic_mgmt']
    try:
        supported = _require_supported_categories(system)
        ch.ensure_peer_port_feature_enabled(system, engine, supported)
        validated = []

        for name in supported:
            with allure.step("sampling+report for {}".format(name)):
                with allure.step("Enable at interval 1 and wait for per-day file"):
                    system.stats.category.categoryName[name].action_general(
                        StatsConsts.CLEAR).verify_result()
                    ch.set_cache_duration(engine, 1)
                    ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN))
                    ch.restart_sampler(engine)
                    if not ch.wait_for_new_sample(engine, name, timeout_sec=180):
                        logger.warning("No per-day sample for %s (nothing connected to sample)", name)
                        ch.assert_generate_rejected_without_data(system, engine, [name])
                        continue

                with allure.step("Generate report and validate report==concat of per-day files"):
                    file_name, report_path = ch.generate_and_get_report_path(system, engine, name)
                    ch.validate_report_equals_per_day_files(engine, name, report_path)

                with allure.step("Show, upload, then delete the report file"):
                    system.stats.files.file_name[file_name].show_file(exit_cmd='q')
                    ch.validate_upload_stats_file(engines, system, file_name, False)
                    player.run_cmd(cmd='rm -f {}{}'.format(NvosConst.MARS_RESULTS_FOLDER, file_name))
                    system.stats.files.file_name[file_name].action_delete().verify_result()
                    assert ch.list_per_day_files(engine, name), \
                        "Internal per-day files should remain after deleting the report"
                validated.append(name)

        assert validated, ("No supported category produced per-day data to validate "
                           "(all present categories were empty)")
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_generate_merges_day_files(engines: EnginesT, devices: DevicesT,
                                                       test_api: str):
    """Basic positive: generate concatenates several per-day files into one report,
    in date order and behind a single header (HLD 16.1).

    A running switch only ever holds today's file, so the earlier days a report
    has to merge are seeded from it, inside the history window so retention does
    not take them away mid-test.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    name = None
    seeded = []
    try:
        supported = _require_supported_categories(system)
        name = _pick_sampling_category(supported)

        with allure.step("Enable {} at interval 1 and wait for today's file".format(name)):
            ch.set_cache_duration(engine, 1)
            ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN),
                               apply=True)
            ch.restart_sampler(engine)
            assert ch.wait_for_new_sample(engine, name, timeout_sec=180), \
                "No samples written for {}".format(name)

        with allure.step("Seed earlier day-files so the report has days to merge"):
            for age_days in StatsConsts.MERGE_SEED_FILE_AGES_DAYS:
                seeded.append(ch.seed_old_day_file(engine, name, age_days))
            on_disk = ch.list_per_day_files(engine, name)
            assert set(seeded).issubset(on_disk), \
                "Seed of earlier day-files failed for {}: {}".format(name, on_disk)

        with allure.step("Generate and check every day-file made it into the report"):
            expected_rows = ch.count_day_file_data_rows(engine, name)
            _, report_path = ch.generate_and_get_report_path(system, engine, name)
            ch.validate_report_equals_per_day_files(engine, name, report_path)
            ch.assert_report_covers_rows(engine, name, report_path, expected_rows)
    finally:
        ch.remove_day_files(engine, name, seeded)
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_day_rollover(engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: crossing a date boundary starts a per-day file for the new
    date and leaves the previous day's file untouched (HLD per-day store).

    The boundary is reached by stepping the DUT clock a day forward rather than
    waiting for midnight, and the daemon is left running so the rollover is the
    one it does by itself.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    name = None
    new_day_file = None
    try:
        supported = _require_supported_categories(system)
        name = _pick_sampling_category(supported)

        with allure.step("Enable {} at interval 1 and wait for today's file".format(name)):
            ch.set_cache_duration(engine, 1)
            ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN),
                               apply=True)
            ch.restart_sampler(engine)
            assert ch.wait_for_new_sample(engine, name, timeout_sec=180), \
                "No samples written for {}".format(name)
            files_before = ch.list_per_day_files(engine, name)

        with ch.step_clock_forward_and_restore(engine, days=1):
            with allure.step("Assert a file is opened for the new date"):
                new_day_file = ch.current_day_file_name(engine)
                assert new_day_file not in files_before, \
                    "Clock step did not move the date past {}".format(files_before)
                assert ch.wait_for_day_file(engine, name, new_day_file, timeout_sec=240), \
                    "No per-day file {} written for {} after the date changed".format(
                        new_day_file, name)

            with allure.step("Assert the previous day's files are kept and closed"):
                files_after = ch.list_per_day_files(engine, name)
                assert set(files_before).issubset(files_after), \
                    "Files present before the date change are gone: {} vs {}".format(
                        files_before, files_after)
                # Counted here rather than before the step: a sample flushed while
                # the clock was being set still belongs to the previous day.
                rows_at_rollover = {f: len(ch.day_file_data_rows(engine, name, f))
                                    for f in files_before}
                ch.wait_one_cycle(StatsConsts.NEW_INTERVAL_MIN)
                still_written = [f for f in files_before
                                 if len(ch.day_file_data_rows(engine, name, f)) >
                                 rows_at_rollover[f]]
                assert not still_written, \
                    "{} kept appending to the previous day's files {}".format(name, still_written)
                ch.assert_one_file_per_day(engine, name)
    finally:
        # The future-dated file outlives the clock step and would otherwise sit in
        # the store until its date comes round, so retention cannot reach it.
        ch.remove_day_files(engine, name, [new_day_file] if new_day_file else [])
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_file_content(engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: generated files contain the category's counters with valid
    values; absent counters are recorded as N/A. Recorded monotonic counters are
    <= the current live value (best-effort live compare where the CLI is available).
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    player = engines['sonic_mgmt']
    try:
        supported = _require_supported_categories(system)
        hostname = _get_hostname(system)
        ch.ensure_peer_port_feature_enabled(system, engine, supported)

        with allure.step("Enable all supported categories at interval 1"):
            ch.set_cache_duration(engine, 1)
            for name in supported:
                ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN),
                                   apply=True)
            ch.restart_sampler(engine)

        with allure.step("Clear history after the restart, keeping only steady-state samples"):
            # Clearing before the restart leaves a stray sample behind: the daemon is
            # still running and writes one between the clear and the restart, which
            # then sits a few seconds before the first post-restart sample.
            for name in supported:
                system.stats.category.categoryName[name].action_general(
                    StatsConsts.CLEAR).verify_result()
            producing = ch.wait_for_sampling_categories(engine, supported, timeout_sec=240)
            ch.assert_generate_rejected_without_data(
                system, engine, [c for c in supported if c not in producing])
            if not producing:
                pytest.skip("No supported category produced per-day data on this setup")

        for name in producing:
            with allure.step("file content for {}".format(name)):
                assert ch.wait_for_min_samples(engine, name), \
                    "{} did not collect {} samples to validate".format(
                        name, StatsConsts.MIN_EXPECTED_SAMPLES)
                ch.assert_one_file_per_day(engine, name)
                ch.assert_no_duplicate_samples(engine, name)
                file_name, _ = ch.generate_and_get_report_path(system, engine, name)
                ch.validate_upload_stats_file(engines, system, file_name, False)
                file_path = NvosConst.MARS_RESULTS_FOLDER + file_name
                with allure.step("Validate header, columns, ranges and N/A handling"):
                    ch.validate_external_file_header_and_data(name, file_path, hostname, None, None)
                # TODO live recorded<=live compare per counter once the feature's
                # interface/transceiver counter CLIs are stable on the platform.
                player.run_cmd(cmd='rm -f {}'.format(file_path))
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_generate_all(engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: ``nv action generate system stats`` produces an all-categories
    bundle that includes the supported new categories.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        supported = _require_supported_categories(system)
        ch.ensure_peer_port_feature_enabled(system, engine, supported)

        with allure.step("Enable supported categories and collect a sample"):
            ch.set_cache_duration(engine, 1)
            for name in supported:
                ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN),
                                   apply=True)
            ch.restart_sampler(engine)
            producing = ch.wait_for_sampling_categories(engine, supported, timeout_sec=240)
            ch.assert_generate_rejected_without_data(
                system, engine, [c for c in supported if c not in producing])
            if not producing:
                pytest.skip("No supported category produced per-day data on this setup")

        with allure.step("Generate the all-categories bundle"):
            ch.clear_report_files(engine)
            system.stats.action_general(StatsConsts.GENERATE).verify_result()
            stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(
                system.stats.files.show()).get_returned_value()
            assert stats_files_show, "No bundle produced by generate-all"
            bundle = list(stats_files_show)[0]

        with allure.step("Verify the bundle includes the data-producing new categories"):
            listing = engine.run_cmd("tar -tf {}/{}".format(StatsConsts.EXTERNAL_PATH, bundle))
            for name in producing:
                assert name in listing, \
                    "Category {} missing from generate-all bundle:\n{}".format(name, listing)
    finally:
        ch.clear_report_files(engine)
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_prune_per_platform(engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: categories are pruned per hardware capability.

    A pruned category is absent from ``nv show system stats category``, and
    querying it by name is rejected with the standard unknown-category error.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        with allure.step("List categories and determine which new ones are pruned"):
            show = OutputParsingTool.parse_json_str_to_dictionary(
                system.stats.category.show()).get_returned_value()
            present = [c for c in StatsConsts.NEW_CATEGORIES if c in show]
            pruned = [c for c in StatsConsts.NEW_CATEGORIES if c not in show]
            logger.info("present=%s pruned=%s", present, pruned)

        if not present and not pruned:
            pytest.skip("No new categories on this platform to assess pruning")

        with allure.step("Categories the platform cannot have are the pruned ones"):
            # Without this the test would take the switch's own answer as the
            # expectation, and could never catch a category being advertised on
            # hardware that cannot support it.
            asic = getattr(devices.dut, 'asic_type', None)
            for name in CATEGORIES_ABSENT_BY_ASIC.get(asic, ()):
                assert name in pruned, \
                    "{} is advertised on {}, which does not support it".format(name, asic)

        with allure.step("Present categories are usable"):
            for name in present:
                assert StatsConsts.STATE in ch.get_category_show(system, name)

        with allure.step("Pruned categories are rejected like any unknown category"):
            for name in pruned:
                # Judged on the output rather than on the exit status: what matters
                # is that no configuration comes back for a category the platform
                # pruned, and asserting the rejection itself would tie the test to
                # how the CLI signals an unknown path.
                result = system.stats.category.categoryName[name].show(
                    should_succeed=False, if_returned_value=False).ignore_result()
                out = result.returned_value if result.result else result.info
                assert StatsConsts.STATE not in str(out), \
                    "Pruned category {} unexpectedly returned a valid config: {}".format(name, out)
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_clear(engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: ``nv action clear`` wipes a category's internal per-day
    files, and collection restarts fresh afterwards.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        supported = _require_supported_categories(system)
        name = _pick_sampling_category(supported)

        with allure.step("Enable {} and collect a per-day file".format(name)):
            ch.set_cache_duration(engine, 1)
            ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN))
            ch.restart_sampler(engine)
            assert ch.wait_for_new_sample(engine, name, timeout_sec=180), \
                "No per-day file for {}".format(name)

        with allure.step("Clear {} and confirm internal files removed".format(name)):
            system.stats.category.categoryName[name].action_general(
                StatsConsts.CLEAR).verify_result()
            assert not ch.list_per_day_files(engine, name), \
                "Internal files for {} were not cleared".format(name)

        with allure.step("Confirm collection restarts fresh"):
            assert ch.wait_for_new_sample(engine, name, timeout_sec=180), \
                "Collection did not restart for {} after clear".format(name)
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_generated_file_rotation(
        engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: the report store keeps at most MAX_GENERATED_FILES reports,
    evicts the oldest first, and applies that cap across all categories together
    rather than per category.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    cap = StatsConsts.MAX_GENERATED_FILES
    try:
        supported = _require_supported_categories(system)
        ch.ensure_peer_port_feature_enabled(system, engine, supported)
        name = _pick_sampling_category(supported)

        with allure.step("Enable {} and collect a per-day file".format(name)):
            ch.set_cache_duration(engine, 1)
            ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN))
            ch.restart_sampler(engine)
            assert ch.wait_for_new_sample(engine, name, timeout_sec=180), \
                "No per-day file for {}".format(name)

        with allure.step("Generate {} reports, two past the cap".format(cap + 2)):
            ch.clear_report_files(engine)
            generated = [ch.generate_report(system, name) for _ in range(cap + 2)]

        with allure.step("Only the newest {} reports are kept".format(cap)):
            listed = ch.list_report_files(system)
            assert listed == sorted(generated[-cap:]), (
                "Rotation kept {} reports, expected the newest {}. If the cap itself "
                "changed, update StatsConsts.MAX_GENERATED_FILES.\nkept: {}\ngenerated: "
                "{}".format(len(listed), cap, listed, generated))
            assert ch.list_report_files_on_disk(engine) == listed, \
                "The files under {} disagree with nv show system stats files".format(
                    StatsConsts.EXTERNAL_PATH)

        with allure.step("A report from another category evicts the oldest one"):
            # Proves the cap is global: the victim belongs to a different category
            # than the report that triggered the eviction.
            other = next((cat for cat in supported
                          if cat != name and ch.count_data_rows(engine, cat)), None)
            if other is None:
                logger.warning("Only %s produces data on this setup; cross-category "
                               "rotation was not exercised", name)
            else:
                oldest_kept = listed[0]
                added = ch.generate_report(system, other)
                after = ch.list_report_files(system)
                assert len(after) == cap, \
                    "Report count {} is past the cap of {} after generating {}".format(
                        len(after), cap, other)
                assert added in after, "The new {} report is missing from {}".format(other, after)
                assert oldest_kept not in after, (
                    "Generating a {} report should have evicted the oldest {} report "
                    "{}, but the listing still holds it".format(other, name, oldest_kept))
    finally:
        ch.clear_report_files(engine)
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_techsupport(engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: a tech-support dump carries the per-day internal files of
    the new categories, trims them to the newest TECHSUPPORT_MAX_DAY_FILES, honors
    the since argument for them, and leaves the history out entirely when generated
    with the skip-stats flag.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    stale_category = None
    stale_files = []
    try:
        supported = _require_supported_categories(system)
        ch.ensure_peer_port_feature_enabled(system, engine, supported)

        with allure.step("Enable supported categories and collect samples"):
            ch.set_cache_duration(engine, 1)
            for name in supported:
                ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN),
                                   apply=True)
            ch.restart_sampler(engine)
            producing = ch.wait_for_sampling_categories(engine, supported, timeout_sec=240)
            ch.assert_generate_rejected_without_data(
                system, engine, [c for c in supported if c not in producing])
            if not producing:
                pytest.skip("No supported category produced per-day data on this setup")

        # Before the window check rather than after it: the two assert unrelated
        # contracts, and the window one failing must not leave the flag unproven.
        with allure.step("The skip-stats flag leaves the history out of the dump"):
            ch.validate_techsupport_without_stats(system, engine, producing)

        with allure.step("Seed more per-day files than tech-support keeps"):
            # A day's sampling leaves one file, which the cap would never trim, so
            # the history has to be built up before the trimming can be seen. Each
            # seeded age stays inside the 7-day history-duration, so their absence
            # from the dump is the tech-support cap and not retention pruning.
            stale_category = sorted(producing)[0]
            stale_files = [ch.seed_old_day_file(engine, stale_category, age)
                           for age in StatsConsts.TECHSUPPORT_SEED_FILE_AGES_DAYS]

        with allure.step("Tech-support keeps the newest {} day files per category".format(
                StatsConsts.TECHSUPPORT_MAX_DAY_FILES)):
            files_by_category = {name: ch.list_per_day_files(engine, name) for name in producing}
            files_by_category = {k: v for k, v in files_by_category.items() if v}
            assert files_by_category, "No per-day files to look for in tech-support"
            on_disk = files_by_category.get(stale_category, [])
            assert len(on_disk) > StatsConsts.TECHSUPPORT_MAX_DAY_FILES, (
                "{} holds {}, not more than the cap of {}, so the check would pass "
                "vacuously".format(stale_category, on_disk,
                                   StatsConsts.TECHSUPPORT_MAX_DAY_FILES))
            ch.validate_per_day_files_in_techsupport(system, engine, files_by_category)

        with allure.step("The since argument reaches the new categories"):
            ch.validate_since_limits_day_files(system, engine, stale_category, stale_files)
    finally:
        ch.remove_day_files(engine, stale_category, stale_files)
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_reboot_persistence(engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: non-default configuration and the on-disk history survive
    a reboot, and the switch resumes sampling into the same history afterwards.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        supported = _require_supported_categories(system)
        name = _pick_sampling_category(supported)

        with allure.step("Set non-default config for {} and save".format(name)):
            # A short non-default interval, not the 12-hour one: it is still
            # different from the default so it proves the config persisted, and it
            # leaves the post-reboot sampling check observable within the test.
            ch.set_cache_duration(engine, 1)
            ch.set_category_config(system, name, interval=StatsConsts.REFLECTION_INTERVAL_B,
                                   history_duration=StatsConsts.REFLECTION_HISTORY_DURATION,
                                   state=StatsConsts.State.ENABLED.value, apply=True)
            SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].
                                            save_config, TestToolkit.engines.dut).verify_result()
            ch.restart_sampler(engine)
            ch.wait_for_new_sample(engine, name, timeout_sec=300)

        with allure.step("Reboot the DUT"):
            # The health check the reboot runs by itself is left out, and taken
            # below with a settle window instead: a rail reading a hundredth of a
            # volt low for the first moments after boot would otherwise fail this
            # test for something that has nothing to do with statistics.
            system.reboot.action_reboot(params='force',
                                        check_system_is_functional=False).verify_result()
            # The reboot action returns once the switch is reachable, which on a
            # bigger chassis is before NVUE serves requests: querying too early
            # comes back empty and the show parsing blows up on it.
            DutUtilsTool.wait_for_nvos_to_become_functional(
                engine, throw_exception_on_unhealthy=False).verify_result()
            ch.wait_for_health_ok(engine)

        with allure.step("Verify config and on-disk history preserved"):
            show = ch.get_category_show(system, name)
            assert show[StatsConsts.INTERVAL] == str(StatsConsts.REFLECTION_INTERVAL_B), \
                "interval not preserved across reboot: {}".format(show)
            assert show[StatsConsts.HISTORY_DURATION] == str(StatsConsts.REFLECTION_HISTORY_DURATION), \
                "history-duration not preserved across reboot: {}".format(show)
            assert ch.list_per_day_files(engine, name), \
                "On-disk history not preserved across reboot for {}".format(name)

        with allure.step("Verify sampling resumes on its own after the reboot"):
            # No config change here: the switch has to resume sampling by itself at
            # the persisted interval. Only the cache duration is restored, because it
            # lives in CONFIG_DB rather than the saved NVUE config and the reboot put
            # it back to the 10-minute default, which would hold samples in the cache
            # past the wait below.
            ch.set_cache_duration(engine, 1)
            assert ch.wait_for_new_sample(engine, name, timeout_sec=300), \
                "{} did not resume sampling on its own after the reboot".format(name)
            # Resuming into a history that already exists is where a re-flushed
            # cache would show up as the same sample written twice, or as a
            # second file for the day the switch rebooted on.
            ch.assert_one_file_per_day(engine, name)
            ch.assert_no_duplicate_samples(engine, name)
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_factory_reset(engines: EnginesT, devices: DevicesT, test_api: str):
    """Basic positive: a whole-DUT factory reset returns the new categories to
    their defaults (5 / 7 / enabled).
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        supported = _require_supported_categories(system)
        name = RandomizationTool.select_random_value(supported).get_returned_value()

        with allure.step("Set non-default config for {}".format(name)):
            ch.set_category_config(system, name, interval=int(StatsConsts.NEW_INTERVAL_MID),
                                   history_duration=StatsConsts.REFLECTION_HISTORY_DURATION,
                                   state=StatsConsts.State.DISABLED.value, apply=True)

        with allure.step("Factory reset the whole DUT"):
            system.factory_default.action_reset(device=devices.dut).verify_result()

        with allure.step("Verify {} back to defaults".format(name)):
            ValidationTool.compare_dictionary_content(
                ch.get_category_show(system, name),
                StatsConsts.NEW_CATEGORY_DEFAULT_DICT).verify_result()
    finally:
        ch.set_system_stats_to_default(engine, system)


# ============================================================================ #
# Group B - Negative tests
# ============================================================================ #
@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_error_flows(engines: EnginesT, devices: DevicesT, test_api: str):
    """Negative: invalid inputs are rejected and leave the config unchanged -
    unknown category across verbs, bad enum/range/type, missing file.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    player = engines['sonic_mgmt']
    valid_remote_url = 'scp://{}:{}@{}/tmp/'.format(player.username, player.password, player.ip)
    try:
        supported = _require_supported_categories(system)
        name = RandomizationTool.select_random_value(supported).get_returned_value()

        with allure.step("Unknown category rejected across verbs"):
            invalid = StatsConsts.INVALID_CATEGORY_NAME
            system.stats.category.categoryName[invalid].set(
                op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.DISABLED.value,
                apply=True).verify_result(should_succeed=False)
            system.stats.category.categoryName[invalid].show(should_succeed=False)
            system.stats.category.categoryName[invalid].action_general(
                StatsConsts.CLEAR).verify_result(should_succeed=False)
            system.stats.category.categoryName[invalid].action_general(
                StatsConsts.GENERATE).verify_result(should_succeed=False)
            # The unknown category is only refused on apply, so it stays in the
            # candidate config and would be reported again by every later apply in
            # this test. Drop it so each step below fails for its own reason.
            ch.set_system_stats_to_default(engine, system)

        with allure.step("Invalid state enum rejected"):
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.INVALID_STATE).\
                verify_result(should_succeed=False)

        with allure.step("Out-of-range / wrong-type interval rejected"):
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.INTERVAL, op_param_value=StatsConsts.INVALID_INTERVAL_LOW).\
                verify_result(should_succeed=False)
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.INTERVAL, op_param_value=StatsConsts.INVALID_INTERVAL_HIGH).\
                verify_result(should_succeed=False)

        with allure.step("Out-of-schema history-duration rejected on set"):
            for bad in (StatsConsts.INVALID_HISTORY_DURATION_LOW, StatsConsts.INVALID_HISTORY_DURATION_HIGH):
                system.stats.category.categoryName[name].set(
                    op_param_name=StatsConsts.HISTORY_DURATION, op_param_value=bad).\
                    verify_result(should_succeed=False)

        with allure.step("New-category history-duration above max rejected on apply"):
            expected_err = StatsConsts.NEW_HISTORY_DURATION_APPLY_ERROR.format(
                category=name, min=StatsConsts.NEW_HISTORY_DURATION_MIN,
                max=StatsConsts.NEW_HISTORY_DURATION_MAX)
            verify_result_obj_failure(
                system.stats.category.categoryName[name].set(
                    op_param_name=StatsConsts.HISTORY_DURATION,
                    op_param_value=StatsConsts.INVALID_NEW_HISTORY_DURATION_HIGH, apply=True),
                expected_err)
            # The rejected value stays in the candidate config - drop it so the
            # following steps run against a clean revision.
            ch.set_system_stats_to_default(engine, system)

        with allure.step("File-not-found on file verbs rejected"):
            # The URL is deliberately well-formed so the upload can only fail on
            # the missing file. Malformed-URL handling is covered by
            # test_system_stats_invalid_values against a file that exists.
            system.stats.files.file_name[StatsConsts.INVALID_FILE_NAME].action_delete().\
                verify_result(should_succeed=False)
            system.stats.files.file_name[StatsConsts.INVALID_FILE_NAME].action_upload(
                valid_remote_url).verify_result(should_succeed=False)
    finally:
        ch.clear_report_files(engine)
        ch.set_system_stats_to_default(engine, system)


# ============================================================================ #
# Group C - Long-running / resilience / performance / scale tests
# ============================================================================ #
@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_resilience(engines: EnginesT, devices: DevicesT, test_api: str):
    """Long-running: collection is resilient to a reboot mid-collection, a
    peer-port going down (absent counters -> N/A, no crash, resumes on restore),
    and a system-time (clock) step.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        supported = _require_supported_categories(system)
        name = _pick_sampling_category(supported)

        with allure.step("Enable {} at interval 1".format(name)):
            ch.set_cache_duration(engine, 1)
            ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN))
            ch.restart_sampler(engine)
            assert ch.wait_for_new_sample(engine, name, timeout_sec=180), \
                "No samples for {}".format(name)

        with allure.step("Reboot mid-collection and confirm sampling resumes"):
            system.reboot.action_reboot(params='force').verify_result()
            assert ch.wait_for_new_sample(engine, name, timeout_sec=300), \
                "Sampling did not resume after reboot for {}".format(name)

        with allure.step("Step the clock forward and confirm no crash, sampling continues"):
            with ch.step_clock_forward_and_restore(engine, days=1):
                ch.wait_one_cycle(StatsConsts.NEW_INTERVAL_MIN)
                assert ch.sampler_pids(engine), \
                    "{} not running after clock step".format(StatsConsts.SAMPLER_SERVICE)
        assert ch.wait_for_new_sample(engine, name, timeout_sec=180), \
            "Sampling did not continue after clock restore for {}".format(name)
        # TODO peer-port down flow: bring a peer-port down (nv set interface <aport>
        # link state down / up) and assert absent counters recorded as N/A and rows
        # resume after restore, reusing the peer-port suite's link-down flow.
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_stress(engines: EnginesT, devices: DevicesT, test_api: str):
    """Long-running: repeated generate/upload/delete/clear plus enable/disable
    toggling keeps the feature and DUT healthy.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        supported = _require_supported_categories(system)
        name = _pick_sampling_category(supported)

        with allure.step("Enable {} at interval 1".format(name)):
            ch.set_cache_duration(engine, 1)
            ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN))
            ch.restart_sampler(engine)
            assert ch.wait_for_new_sample(engine, name, timeout_sec=180), \
                "No samples for {}".format(name)

        with allure.step("Loop generate/delete/clear and toggle state"):
            for i in range(10):
                with allure.independent_step("stress iteration {}".format(i)):
                    # Every iteration wipes the category and toggles it off and on, so
                    # the next one must wait for collection to restart: generating from
                    # a category with no sampled data is refused, by design.
                    assert ch.wait_for_new_sample(engine, name, timeout_sec=180), \
                        "No sample for {} at stress iteration {}".format(name, i)
                    file_name, _ = ch.generate_and_get_report_path(system, engine, name)
                    system.stats.files.file_name[file_name].action_delete().verify_result()
                    system.stats.category.categoryName[name].action_general(
                        StatsConsts.CLEAR).verify_result()
                    ch.disable_category(system, name, apply=True)
                    ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN))

        with allure.step("Confirm the feature is still healthy"):
            assert ch.wait_for_new_sample(engine, name, timeout_sec=180), \
                "Sampling not healthy for {} after stress".format(name)
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_scale(engines: EnginesT, devices: DevicesT, test_api: str):
    """Long-running: all supported new categories enabled at interval 1 on a
    full-port system; soak for SCALE_SOAK_SECONDS (short CI default; raise for a
    real 24-72h soak) and confirm well-formed files and stable resource usage -
    CPU, resident memory and disk growth, so a leak that only shows after hours
    of sampling is caught here rather than in the short CPU test.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        supported = _require_supported_categories(system)
        ch.ensure_peer_port_feature_enabled(system, engine, supported)

        with allure.step("Enable all supported categories at interval 1"):
            ch.set_cache_duration(engine, 1)
            for name in supported:
                ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN),
                                   apply=True)
            ch.restart_sampler(engine)

        with allure.step("Soak for {}s".format(StatsConsts.SCALE_SOAK_SECONDS)):
            # Let one cycle pass before the baselines: straight after the restart the
            # daemon has not forked its worker yet, so memory would read as the parent
            # alone and the soak would look like it grew by the worker's whole size.
            ch.wait_one_cycle(StatsConsts.NEW_INTERVAL_MIN)
            disk_before = ch.measure_dir_bytes(engine, StatsConsts.INTERNAL_ROOT)
            rss_before = ch.measure_stats_reportd_rss_mb(engine)
            started = time.time()
            time.sleep(StatsConsts.SCALE_SOAK_SECONDS)
            soaked = time.time() - started

        with allure.step("Confirm resource usage stable and files well-formed"):
            rss_after = ch.measure_stats_reportd_rss_mb(engine)
            disk_after = ch.measure_dir_bytes(engine, StatsConsts.INTERNAL_ROOT)
            disk_daily_mb = ch.project_daily_mb(disk_after - disk_before, soaked)
            logger.info("after %.0fs soak: memory %.1fMB (from %.1fMB), /var/stats %.1fMB/day "
                        "projected", soaked, rss_after, rss_before, disk_daily_mb)
            assert disk_daily_mb <= StatsConsts.TOTAL_PER_DAY_MB, \
                "/var/stats grew at {:.1f}MB/day over the soak, above the {}MB limit".format(
                    disk_daily_mb, StatsConsts.TOTAL_PER_DAY_MB)
            assert rss_after <= StatsConsts.RAM_LIMIT_ALL_MB, \
                "stats-reportd memory {:.1f}MB after the soak exceeds limit {}MB (started at " \
                "{:.1f}MB)".format(rss_after, StatsConsts.RAM_LIMIT_ALL_MB, rss_before)
            cpu = ch.measure_stats_reportd_cpu(
                engine, sample_seconds=StatsConsts.CPU_MEASURE_WINDOW_SEC)
            assert cpu <= StatsConsts.CPU_LIMIT_ALL_PERCENT, \
                "stats-reportd CPU {}% exceeds limit {}%".format(
                    cpu, StatsConsts.CPU_LIMIT_ALL_PERCENT)
            # A supported category can legitimately sample nothing all soak long
            # (transceiver cages with no module, peer-port with no peer); those owe
            # a rejected generate rather than a report.
            producing = [c for c in supported if ch.count_data_rows(engine, c) > 0]
            assert producing, "No supported category produced data during the soak"
            for name in producing:
                file_name, report_path = ch.generate_and_get_report_path(system, engine, name)
                ch.validate_report_equals_per_day_files(engine, name, report_path)
                system.stats.files.file_name[file_name].action_delete().verify_result()
            ch.assert_generate_rejected_without_data(
                system, engine, [c for c in supported if c not in producing])
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_cpu(engines: EnginesT, devices: DevicesT, test_api: str):
    """Long-running: measure stats-reportd CPU and resident memory in three
    configurations - interface alone, all new categories, then every category
    including the legacy ones forced to interval 1 - against the allowed limits
    (averaged over CPU_MEASURE_WINDOW_SEC to match the feature-owner methodology).

    The legacy categories keep their default 5-30 min intervals in the first two
    phases, so those figures include the standing legacy load and the meaningful
    signal is the delta between phases.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        supported = _require_supported_categories(system)
        if StatsConsts.INTERFACE not in supported:
            pytest.skip("interface category not supported on this platform")
        # Without this the peer-port category is enabled but its telemetry feature is
        # off, so it samples nothing and the all-categories phases below measure a
        # lighter switch than the one being sized.
        ch.ensure_peer_port_feature_enabled(system, engine, supported)

        with allure.step("Disable all new categories, enable only interface"):
            ch.set_cache_duration(engine, 1)
            for name in supported:
                ch.disable_category(system, name, apply=True)
            ch.enable_category(system, StatsConsts.INTERFACE,
                               interval=int(StatsConsts.NEW_INTERVAL_MIN))
            ch.restart_sampler(engine)
            ch.wait_one_cycle(StatsConsts.NEW_INTERVAL_MIN)

        with allure.step("Measure single-category CPU and memory"):
            single, single_rss = _measure_cpu_and_rss(engine, "interface only")
            assert single <= StatsConsts.CPU_LIMIT_SINGLE_PERCENT, \
                "single-category CPU {}% exceeds limit {}%".format(
                    single, StatsConsts.CPU_LIMIT_SINGLE_PERCENT)
            assert single_rss <= StatsConsts.RAM_LIMIT_SINGLE_MB, \
                "single-category memory {:.1f}MB exceeds limit {}MB".format(
                    single_rss, StatsConsts.RAM_LIMIT_SINGLE_MB)

        with allure.step("Enable all new categories and measure"):
            for name in supported:
                ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN),
                                   apply=True)
            ch.restart_sampler(engine)
            ch.wait_one_cycle(StatsConsts.NEW_INTERVAL_MIN)
            total, total_rss = _measure_cpu_and_rss(engine, "all new categories")
            assert total <= StatsConsts.CPU_LIMIT_ALL_PERCENT, \
                "all-new-categories CPU {}% exceeds limit {}%".format(
                    total, StatsConsts.CPU_LIMIT_ALL_PERCENT)
            assert total_rss <= StatsConsts.RAM_LIMIT_ALL_MB, \
                "all-new-categories memory {:.1f}MB exceeds limit {}MB".format(
                    total_rss, StatsConsts.RAM_LIMIT_ALL_MB)

        with allure.step("Enable every category at interval 1 and measure the worst case"):
            _enable_legacy_categories_at_min_interval(system, devices)
            ch.restart_sampler(engine)
            ch.wait_one_cycle(StatsConsts.NEW_INTERVAL_MIN)
            every, every_rss = _measure_cpu_and_rss(engine, "every category at interval 1")
            assert every <= StatsConsts.CPU_LIMIT_ALL_CATEGORIES_PERCENT, \
                "all-categories CPU {}% exceeds limit {}%".format(
                    every, StatsConsts.CPU_LIMIT_ALL_CATEGORIES_PERCENT)
            assert every_rss <= StatsConsts.RAM_LIMIT_ALL_CATEGORIES_MB, \
                "all-categories memory {:.1f}MB exceeds limit {}MB".format(
                    every_rss, StatsConsts.RAM_LIMIT_ALL_CATEGORIES_MB)
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_file_size(engines: EnginesT, devices: DevicesT, test_api: str):
    """Long-running: with every category (legacy included) sampling at interval 1,
    measure how fast /var/stats actually grows and project that to a full day,
    per category and for the tree as a whole.

    Sizing a file a few minutes after the daemon starts would compare a nearly
    empty file against a whole-day estimate and pass no matter what, and letting
    the switch sample for a real day is not runnable, so growth is measured over
    DISK_GROWTH_WINDOW_SEC and extrapolated instead. A manual gzip of a per-day
    file is included for the compression ratio; files are stored uncompressed.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    try:
        supported = _require_supported_categories(system)
        ch.ensure_peer_port_feature_enabled(system, engine, supported)

        with allure.step("Enable every category at interval 1"):
            ch.set_cache_duration(engine, 1)
            for name in supported:
                ch.enable_category(system, name, interval=int(StatsConsts.NEW_INTERVAL_MIN),
                                   apply=True)
            _enable_legacy_categories_at_min_interval(system, devices)
            ch.restart_sampler(engine)
            producing = ch.wait_for_sampling_categories(engine, supported, timeout_sec=240)
            assert producing, "No new category produced data; nothing to size"

        with allure.step("Measure /var/stats growth over {}s".format(
                StatsConsts.DISK_GROWTH_WINDOW_SEC)):
            # A category only gets a directory once it writes its first sample, so
            # size the ones that are actually on disk.
            on_disk = set(engine.run_cmd(
                "sudo ls -1 {} 2>/dev/null".format(StatsConsts.INTERNAL_ROOT)).split())
            measured = [c for c in list(producing) + list(getattr(devices.dut, 'category_list', []))
                        if c in on_disk]
            before = {c: ch.measure_dir_bytes(engine, ch.per_day_dir(c)) for c in measured}
            before_total = ch.measure_dir_bytes(engine, StatsConsts.INTERNAL_ROOT)
            started = time.time()
            time.sleep(StatsConsts.DISK_GROWTH_WINDOW_SEC)
            elapsed = time.time() - started
            after = {c: ch.measure_dir_bytes(engine, ch.per_day_dir(c)) for c in measured}
            after_total = ch.measure_dir_bytes(engine, StatsConsts.INTERNAL_ROOT)

        with allure.step("Project the measured growth to 24 hours"):
            new_daily_mb = 0.0
            for category in measured:
                projected = ch.project_daily_mb(after[category] - before[category], elapsed)
                logger.info("%s: %.2fMB/day projected (%d bytes in %.0fs)",
                            category, projected, after[category] - before[category], elapsed)
                if category in producing:
                    new_daily_mb += projected
                # The estimate was sized for the heaviest category (interface on a
                # 288-port switch), so it bounds the legacy sensor categories too -
                # loosely for them, but loose beats logging a number nobody reads.
                assert projected <= StatsConsts.PER_DAY_MB_1MIN, \
                    "{} projected {:.1f}MB/day exceeds the {}MB estimate".format(
                        category, projected, StatsConsts.PER_DAY_MB_1MIN)
            total_projected = ch.project_daily_mb(after_total - before_total, elapsed)
            logger.info("/var/stats: %.2fMB/day projected in total; the new categories account "
                        "for %.2fMB/day, i.e. %.1fMB once their %s-day retention window is full",
                        total_projected, new_daily_mb,
                        new_daily_mb * int(StatsConsts.NEW_HISTORY_DURATION_MAX),
                        StatsConsts.NEW_HISTORY_DURATION_MAX)
            assert total_projected <= StatsConsts.TOTAL_PER_DAY_MB, \
                "/var/stats projected {:.1f}MB/day exceeds the {}MB limit".format(
                    total_projected, StatsConsts.TOTAL_PER_DAY_MB)

        with allure.step("Check the gzip ratio the HLD sizing assumes (files stored uncompressed)"):
            # The largest file gives the most meaningful ratio: on a tiny one the
            # gzip header is a large enough share of the output to skew it.
            name = max(producing, key=lambda c: after.get(c, 0))
            files = ch.list_per_day_files(engine, name)
            assert files, "No per-day file to compress for {}".format(name)
            path = "{}/{}".format(ch.per_day_dir(name), files[-1])
            gz_copy = "{}.gz".format(GZIP_CHECK_COPY)
            size_bytes = ch.measure_file_bytes(engine, path)
            # One privilege boundary for the whole copy: the per-day files are
            # root-owned, and gzip has to unlink its input after writing the .gz.
            engine.run_cmd("sudo sh -c 'cp {p} {c} && gzip -f {c}'".format(
                p=path, c=GZIP_CHECK_COPY))
            gz_bytes = ch.measure_file_bytes(engine, gz_copy)
            engine.run_cmd("sudo rm -f {}".format(gz_copy))
            assert size_bytes and gz_bytes, \
                "Could not size {} before/after gzip: raw={} gz={}".format(path, size_bytes, gz_bytes)
            ratio = size_bytes / float(gz_bytes)
            logger.info("gzip ratio for %s: %.1fx (raw=%d gz=%d)", name, ratio, size_bytes, gz_bytes)
            # One-sided on purpose: the HLD sizes archived history at a third of the
            # raw file, so compressing better than that is fine and only compressing
            # worse invalidates the estimate.
            assert ratio >= StatsConsts.GZIP_RATIO, \
                "{} compresses only {:.1f}x, below the {}x the HLD sizing assumes".format(
                    name, ratio, StatsConsts.GZIP_RATIO)
    finally:
        ch.set_system_stats_to_default(engine, system)


@pytest.mark.system
@pytest.mark.stats
@pytest.mark.simx
# Two image installs with a reboot each: Juliet alone allows 20 minutes per boot
# before NVUE is expected to serve. Stated here so a lane-wide --timeout, which
# some cases set to 15 minutes, cannot cut an install in half.
@pytest.mark.timeout(MINUTE * 120, func_only=True)
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_counters_historical_upgrade(engines: EnginesT, devices: DevicesT, topology_obj,
                                     request, test_api: str):
    """Long-running: the new-category configuration survives an image upgrade and
    the switch samples again afterwards. Per the feature's quality guarantees the
    history collected before the upgrade is not required to be kept, so this only
    requires collection to start again - not the old per-day files to be there.

    Version selection belongs to the lane, not here, so both images come from the
    command line the way the upgrade and checklist lanes already pass them:
    --target_version is the build under test, which the switch is expected to be
    running when the test starts, and --downgrade_version (or --base_version) is
    the older image to upgrade from - chosen by release management, since the
    release share offers no reliable "previous version" to derive. Both images
    must carry the feature. Skips cleanly when they are absent, which is the case
    in the ordinary lanes.

    The switch is handed back on the target image whichever way the test ends.
    """
    TestToolkit.tested_api = test_api
    system = System(devices_dut=devices.dut)
    engine = engines.dut
    target = request.config.getoption("--target_version")
    older = request.config.getoption("--downgrade_version") or \
        request.config.getoption("--base_version")
    if not target:
        pytest.skip("No --target_version given; run this in the upgrade lane with two "
                    "images that both carry the Counters Historical categories")
    try:
        supported = _require_supported_categories(system)

        if older:
            with allure.step("Go down to the image to upgrade from"):
                # Skips on its own when the switch already runs it.
                ch.install_image(engines, topology_obj, Path(os.path.realpath(older)))
                DutUtilsTool.wait_for_nvos_to_become_functional(engine).verify_result()
                # Re-read: a different image may prune a different set.
                supported = _require_supported_categories(system)

        # Picked from what the older image offers, since that is the set carried
        # across the upgrade and asserted on afterwards.
        name = _pick_sampling_category(supported)

        with allure.step("Configure every supported category away from its defaults and save"):
            for category in supported:
                ch.set_category_config(system, category,
                                       interval=StatsConsts.REFLECTION_INTERVAL_B,
                                       history_duration=StatsConsts.REFLECTION_HISTORY_DURATION,
                                       state=StatsConsts.State.ENABLED.value, apply=True)
            SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].
                                            save_config, TestToolkit.engines.dut).verify_result()
            before = {category: ch.get_category_show(system, category) for category in supported}

        with allure.step("Upgrade to the target image"):
            result = ch.install_image(engines, topology_obj, Path(os.path.realpath(target)))
            assert not result.skipped, \
                "The switch already runs the target image, so nothing was upgraded; " \
                "pass --downgrade_version to install the image to upgrade from"
            assert result.ok, "Upgrade to the target image failed: {}".format(result)
            DutUtilsTool.wait_for_nvos_to_become_functional(engine).verify_result()

        with allure.step("Verify the configuration came back unchanged"):
            for category, expected in before.items():
                show = ch.get_category_show(system, category)
                for field in (StatsConsts.INTERVAL, StatsConsts.HISTORY_DURATION,
                              StatsConsts.STATE):
                    assert show[field] == expected[field], \
                        "{} {} not preserved across the upgrade: {} before, {} after".format(
                            category, field, expected[field], show[field])

        with allure.step("Verify sampling starts again after the upgrade"):
            # Only the cache duration is restored: it lives in CONFIG_DB rather than
            # the saved NVUE config, so the new image comes up with the 10-minute
            # default and would hold samples past the wait below. The history from
            # before the upgrade is deliberately not asserted on.
            ch.set_cache_duration(engine, 1)
            assert ch.wait_for_new_sample(engine, name, timeout_sec=300), \
                "{} did not start sampling again after the upgrade".format(name)
    finally:
        if older:
            # Whatever went wrong, the switch must not be handed to the next case on
            # the older image. Asking for the target is decided on what is actually
            # running, so it costs one show command when the upgrade already
            # succeeded and reinstalls only on the paths that failed part-way -
            # including a failure raised from inside the downgrade itself, after it
            # had already rebooted into the older image.
            with allure.step("Leave the switch on the image it started with"):
                ch.install_image(engines, topology_obj, Path(os.path.realpath(target)))
                DutUtilsTool.wait_for_nvos_to_become_functional(engine).verify_result()
        ch.set_system_stats_to_default(engine, system)
