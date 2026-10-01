"""Helpers for the Counters Historical Analysis suite (test_counters_historical.py).

These orchestrate/verify the new interface / peer-port / transceivers categories:
supported-category discovery, per-day internal-file access
(``/var/stats/<category>/<date>.csv``), report-vs-per-day comparison,
recorded-vs-live counter comparison, sampling-cadence measurement, retention
pruning, ``stats-reportd`` CPU measurement, and clock-step-and-restore.

Anything the feature has not finalized (exact per-day file name format, agreed
CPU limits, DOM tolerance) is driven by TODO-tagged constants in
:class:`ngts.tests_nvos.system.stats.constants.StatsConsts`.
"""
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime
import logging
import time

from ngts.tests_nvos.system.stats.constants import StatsConsts
from ngts.tests_nvos.system.stats import stats_helpers
from ngts.nvos_constants.constants_nvos import DatabaseConst, ImageConsts, SystemConsts
from ngts.nvos_tools.infra.DatabaseTool import DatabaseTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Category discovery / configuration
# --------------------------------------------------------------------------- #
def get_supported_new_categories(system):
    """Return the new categories (interface / peer-port / transceivers) that are
    actually present in ``nv show system stats category`` on this platform.

    A platform that cannot hold the entity at all prunes the category, so it is
    simply absent from the returned list. A platform that supports it but has
    nothing connected still lists it: see
    :func:`assert_generate_rejected_without_data`.
    """
    with allure.step("Discover supported Counters Historical categories"):
        show = OutputParsingTool.parse_json_str_to_dictionary(
            system.stats.category.show()).get_returned_value()
        present = [cat for cat in StatsConsts.NEW_CATEGORIES if cat in show]
        logger.info("Supported new stats categories: {}".format(present))
        return present


def set_category_config(system, name, interval=None, history_duration=None, state=None, apply=True):
    """Set one new category's config fields, applying once after the last field."""
    fields = []
    if state is not None:
        fields.append((StatsConsts.STATE, state))
    if interval is not None:
        fields.append((StatsConsts.INTERVAL, int(interval)))
    if history_duration is not None:
        fields.append((StatsConsts.HISTORY_DURATION, int(history_duration)))

    for idx, (param_name, param_value) in enumerate(fields):
        is_last = idx == len(fields) - 1
        system.stats.category.categoryName[name].set(
            op_param_name=param_name, op_param_value=param_value,
            apply=apply and is_last).verify_result()


def enable_category(system, name, interval=None, apply=True):
    """Enable one category (optionally set its interval) and apply."""
    interval = interval if interval is not None else int(StatsConsts.NEW_INTERVAL_MIN)
    with allure.step("Enable category {} at interval {}".format(name, interval)):
        system.stats.category.categoryName[name].set(
            op_param_name=StatsConsts.INTERVAL, op_param_value=int(interval)).verify_result()
        system.stats.category.categoryName[name].set(
            op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.ENABLED.value,
            apply=apply).verify_result()


def count_peer_counter_keys(engine):
    """Return the number of PEER_COUNTERS entries currently published to COUNTERS_DB."""
    output = DatabaseTool.sonic_db_cli_get_keys(
        engine, asic="", db_name=DatabaseConst.COUNTERS_DB_NAME,
        grep_str=StatsConsts.PEER_COUNTERS_KEY_PREFIX)
    return len([line for line in (output or "").splitlines() if line.strip()])


def ensure_peer_port_feature_enabled(system, engine, categories):
    """Turn the peer-port telemetry feature on and wait for its counters to publish.

    The category is advertised whether or not the feature is enabled, but while
    it is off COUNTERS_DB holds no peer entries and there is nothing to sample.
    Callers must invoke this before restarting stats-reportd: a daemon that
    starts while the counters are absent does not pick the category up later.
    """
    if StatsConsts.PEER_PORT not in categories:
        return True
    with allure.step("Enable the peer-port telemetry feature and wait for counters"):
        system.peer_port.set(op_param_name=StatsConsts.STATE,
                             op_param_value=StatsConsts.State.ENABLED.value,
                             apply=True).verify_result()
        deadline = time.time() + StatsConsts.PEER_PORT_COUNTER_KEYS_TIMEOUT
        while time.time() < deadline:
            if count_peer_counter_keys(engine):
                return True
            time.sleep(10)
        logger.warning("peer-port counters did not publish within %ss; the category "
                       "will be treated as having nothing to sample",
                       StatsConsts.PEER_PORT_COUNTER_KEYS_TIMEOUT)
        return False


def disable_category(system, name, apply=True):
    with allure.step("Disable category {}".format(name)):
        system.stats.category.categoryName[name].set(
            op_param_name=StatsConsts.STATE, op_param_value=StatsConsts.State.DISABLED.value,
            apply=apply).verify_result()


def get_category_show(system, name):
    """Return the parsed ``nv show system stats category <name>`` dict."""
    return OutputParsingTool.parse_json_str_to_dictionary(
        system.stats.category.categoryName[name].show()).get_returned_value()


def restart_sampler(engine):
    """Restart the sampling daemon so it takes the configuration just applied.

    The daemon reads its configuration when a cycle begins, so a fresh interval or
    state would otherwise only take hold up to a cycle later. Every test that
    configures a category and then waits for a sample restarts it first to keep
    those waits bounded (grey-box: the CLI has no equivalent).
    """
    engine.run_cmd("sudo systemctl restart {}".format(StatsConsts.SAMPLER_SERVICE))


def wait_one_cycle(interval_min, extra_sec=20):
    """Sleep long enough for at least one sampling cycle to complete."""
    seconds = int(interval_min) * 60 + extra_sec
    with allure.step("Wait one sampling cycle (~{}s)".format(seconds)):
        time.sleep(seconds)


# --------------------------------------------------------------------------- #
# Per-day internal file access (grey-box, /var/stats/<category>/<date>.csv)
# --------------------------------------------------------------------------- #
def per_day_dir(category):
    return StatsConsts.INTERNAL_CAT_DIR.format(category=category)


def day_file_date(file_name):
    """Parse the date a per-day file name carries, or None if it carries none.

    Retention, the one-file-per-day check and the tech-support trim all judge a
    file by the date in its name, so they all read it through here.
    """
    try:
        return datetime.strptime(file_name.replace(StatsConsts.PER_DAY_FILE_EXT, ''),
                                 StatsConsts.PER_DAY_FILE_DATE_FORMAT)
    except ValueError:
        return None


def assert_names_carry_dates(day_files, where):
    """Fail when files are present but none of their names carry a date.

    Retention and the tech-support trim both judge a file by the date in its
    name and pass over the ones they cannot read, so a device that renamed its
    files would leave them asserting nothing rather than failing.
    """
    assert not day_files or any(day_file_date(name) for name in day_files), \
        "No file in {} carries a {} name: {}".format(
            where, StatsConsts.PER_DAY_FILE_DATE_FORMAT, day_files)


def list_per_day_files(engine, category):
    """Return sorted per-day file names under /var/stats/<category>/ (empty if none)."""
    directory = per_day_dir(category)
    output = engine.run_cmd(
        "if [ -d {d} ]; then ls -1 {d}; else echo ''; fi".format(d=directory))
    files = [line.strip() for line in output.strip().splitlines() if line.strip()]
    return sorted(files)


def read_per_day_file(engine, category, file_name):
    """Return the raw text of a single per-day file."""
    path = "{}/{}".format(per_day_dir(category), file_name)
    return engine.run_cmd("sudo cat {}".format(path))


def split_header_and_data(text):
    """Split a stats file into (column-name row, data rows).

    The header block is the '#' comment lines plus one row of column names, so
    the first line that is neither is where the data starts. A merged report
    carries that block once while each per-day file carries its own, which is
    the difference a comparison between the two has to account for.
    """
    lines = [line for line in text.splitlines()
             if line.strip() and not line.startswith('#')]
    if not lines:
        return None, []
    return lines[0], lines[1:]


def day_file_data_rows(engine, category, file_name):
    """Return the data rows of one per-day file (header excluded)."""
    return split_header_and_data(read_per_day_file(engine, category, file_name))[1]


def count_day_file_data_rows(engine, category):
    """Total data rows held across a category's per-day files (headers excluded)."""
    return sum(len(day_file_data_rows(engine, category, file_name))
               for file_name in list_per_day_files(engine, category))


def current_day_file_name(engine):
    """Per-day file name for the date the DUT itself reports.

    Read from the switch rather than computed here, so it stays right while a
    test holds the DUT clock stepped forward.
    """
    return "{}{}".format(stats_helpers.dut_day_string(engine),
                         StatsConsts.PER_DAY_FILE_EXT)


def wait_for_day_file(engine, category, file_name, timeout_sec=180, poll_sec=10):
    """Wait until a given per-day file exists and holds data; True if it did in time."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if file_name in list_per_day_files(engine, category) and \
                day_file_data_rows(engine, category, file_name):
            return True
        time.sleep(poll_sec)
    return False


def count_data_rows(engine, category):
    """Count non-comment data rows across all per-day files for a category (grey-box)."""
    directory = per_day_dir(category)
    output = engine.run_cmd(
        "if [ -d {d} ]; then cat {d}/*{ext} 2>/dev/null | grep -vc '^#'; else echo 0; fi".format(
            d=directory, ext=StatsConsts.PER_DAY_FILE_EXT))
    try:
        return int(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def wait_for_new_sample(engine, category, timeout_sec, poll_sec=10):
    """Wait until the category's data-row count grows; return True if it grew in time."""
    baseline = count_data_rows(engine, category)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(poll_sec)
        if count_data_rows(engine, category) > baseline:
            return True
    return False


def count_distinct_samples(engine, category):
    """Count distinct sample timestamps for a category (one cycle = many rows)."""
    return len(set(_collect_timestamps(engine, category)))


def wait_for_min_samples(engine, category, min_samples=StatsConsts.MIN_EXPECTED_SAMPLES,
                         interval_min=int(StatsConsts.NEW_INTERVAL_MIN), poll_sec=10):
    """Wait until the category has at least ``min_samples`` distinct sample times.

    A generated report has to carry MIN_EXPECTED_SAMPLES cycles to be validated, and
    one cycle spans many rows, so waiting for the row count to grow is not enough.
    Returns True if the threshold was reached in time.
    """
    with allure.step("Wait for {} to collect {} samples".format(category, min_samples)):
        # Allow one extra cycle plus a margin for the first sample landing mid-cycle.
        deadline = time.time() + (min_samples + 1) * interval_min * 60 + poll_sec
        while time.time() < deadline:
            if count_distinct_samples(engine, category) >= min_samples:
                return True
            time.sleep(poll_sec)
        return count_distinct_samples(engine, category) >= min_samples


def _collect_timestamps(engine, category):
    """Return the ordered list of sample timestamps (datetime) across per-day files."""
    timestamps = []
    for file_name in list_per_day_files(engine, category):
        content = read_per_day_file(engine, category, file_name)
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            first = line.split(',')[0].strip()
            try:
                timestamps.append(datetime.strptime(first, StatsConsts.TIMESTAMP_FORMAT))
            except ValueError:
                # Unknown timestamp format on this build; skip (TODO confirm format).
                continue
    return timestamps


def wait_for_health_ok(engine, timeout_sec=StatsConsts.HEALTH_SETTLE_TIMEOUT_SEC, poll_sec=10):
    """Wait for the switch to report healthy after a boot, and assert that it does.

    Sensor rails can read marginally out of range for a short while after boot -
    a hundredth of a volt under the minimum, on a different PMIC each time - and
    the framework's own check fails on the first reading that is not OK, which
    fails a test for something it did not cause. Re-reading to a deadline keeps
    the assertion while letting the reading settle: a rail that is really bad
    stays out of range and still fails.
    """
    from ngts.nvos_constants.constants_nvos import HealthConsts
    from ngts.nvos_tools.system.System import System

    with allure.step("Wait for system health to settle"):
        health = System().health
        deadline = time.time() + timeout_sec
        while True:
            output = OutputParsingTool.parse_json_str_to_dictionary(
                health.show(dut_engine=engine)).get_returned_value()
            if output[HealthConsts.STATUS] == HealthConsts.OK:
                return
            if time.time() >= deadline:
                assert False, "System health is still {} {}s after the boot: {}".format(
                    output[HealthConsts.STATUS], timeout_sec, output.get(HealthConsts.ISSUES))
            logger.info("Health is %s, re-reading in %ss", output[HealthConsts.STATUS], poll_sec)
            time.sleep(poll_sec)


def assert_one_file_per_day(engine, category):
    """A category's store holds one per-day file per date and nothing else.

    Two files covering the same day, or a leftover temporary one, would be
    counted twice by a generated report and could survive a retention pass,
    since retention decides on the date parsed out of the name. Everything else
    here just lists the directory and takes what it finds.
    """
    with allure.step("Check {} keeps one file per day".format(category)):
        dates = {}
        unexpected = []
        for file_name in list_per_day_files(engine, category):
            parsed = day_file_date(file_name)
            if parsed is None:
                unexpected.append(file_name)
                continue
            dates.setdefault(parsed.date(), []).append(file_name)

        assert not unexpected, \
            "{} holds files that are not per-day files: {}".format(category, unexpected)
        repeated = {date: names for date, names in dates.items() if len(names) > 1}
        assert not repeated, \
            "{} holds more than one file for the same day: {}".format(category, repeated)


def assert_no_duplicate_samples(engine, category):
    """No per-day file records the same row twice.

    Row-per-entity categories write one row per port per cycle, so a repeated
    timestamp is normal and only a repeated timestamp-and-entity pair is a real
    duplicate. Every cadence and sample count check counts distinct timestamps
    for exactly that reason, which leaves a duplicated row invisible to all of
    them - the daemon re-flushing its cache after a restart being the likely way
    to write one.
    """
    with allure.step("Check {} per-day files for duplicated rows".format(category)):
        config = StatsConsts.CATEGORY_VALIDATION_CONFIG.get(category, {})
        # Column 0 is the cycle timestamp and column 1 the entity name, so the two
        # together identify a row; the one-row-per-cycle categories need only the
        # timestamp. peer-port carries a switch-port column too, but a peer port
        # terminates on exactly one switch port, so the entity still identifies it.
        identity_width = 2 if config.get('row_per_entity') else 1
        for file_name in list_per_day_files(engine, category):
            seen = set()
            duplicates = []
            for line in read_per_day_file(engine, category, file_name).splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                fields = [field.strip() for field in line.split(',')]
                try:
                    datetime.strptime(fields[0], StatsConsts.TIMESTAMP_FORMAT)
                except ValueError:
                    # The column header, which every file carries once.
                    continue
                identity = tuple(fields[:identity_width])
                if identity in seen:
                    duplicates.append(identity)
                seen.add(identity)
            assert not duplicates, \
                "{} {} records {} row(s) twice, first few: {}".format(
                    category, file_name, len(duplicates), duplicates[:5])


def measure_sample_cadence(engine, category):
    """Return the gaps (seconds) between consecutive distinct sample timestamps.

    The per-day files are row-per-port, so one sampling cycle writes many rows
    that all share the same cycle timestamp. We dedupe to the distinct sample
    times first, otherwise within-cycle rows would produce spurious 0s gaps.
    """
    timestamps = sorted(set(_collect_timestamps(engine, category)))
    return [(timestamps[i] - timestamps[i - 1]).total_seconds()
            for i in range(1, len(timestamps))]


def wait_for_sampling_categories(engine, categories, timeout_sec, poll_sec=10):
    """Return the subset of ``categories`` that actually produce per-day data
    within ``timeout_sec`` (all waited on together, not one full timeout each).

    A category can be present in ``nv show`` yet legitimately never sample on a
    given setup (e.g. peer-port with no connected peer): those are excluded so
    data-dependent validation runs on the rest. Callers must still hold the
    empty ones to the no-data contract via
    :func:`assert_generate_rejected_without_data`.
    """
    with allure.step("Wait for categories to produce per-day data: {}".format(categories)):
        baseline = {c: count_data_rows(engine, c) for c in categories}
        producing = []
        deadline = time.time() + timeout_sec
        while time.time() < deadline and len(producing) < len(categories):
            time.sleep(poll_sec)
            for c in categories:
                if c not in producing and count_data_rows(engine, c) > baseline[c]:
                    producing.append(c)
        empty = [c for c in categories if c not in producing]
        if empty:
            logger.warning("Categories present but producing no per-day data: %s", empty)
        return producing


def assert_generate_rejected_without_data(system, engine, categories):
    """Assert each never-sampled category refuses to generate a report.

    A platform can support a category and still have nothing to sample: a
    transceiver cage with no module, a peer-port with no connected peer. The
    feature keeps such a category enabled, never creates
    ``/var/stats/<category>``, and fails the generate action instead of handing
    back an empty report. Asserting that keeps a setup with nothing connected
    from passing the data-driven tests vacuously.

    A category that sampled at least once and then stopped is a different case:
    it does have data to report, so the contract above does not apply to it.
    """
    for category in categories:
        if list_per_day_files(engine, category):
            logger.warning("%s has per-day files but stopped producing new rows", category)
            continue
        with allure.step("Generate is refused for {} while it has no sampled data".format(category)):
            system.stats.category.categoryName[category].action_general(
                StatsConsts.GENERATE).verify_result(
                    should_succeed=False, expected_value=StatsConsts.NO_SAMPLED_DATA_ERROR)


# --------------------------------------------------------------------------- #
# Report vs per-day files / recorded vs live
# --------------------------------------------------------------------------- #
def validate_report_equals_per_day_files(engine, category, report_path):
    """Assert the generated report equals the concatenation of per-day files:
    same data rows, in date order, with the header appearing once (HLD 16.1).
    """
    with allure.step("Compare generated report to concatenation of per-day files"):
        report = engine.run_cmd("sudo cat {}".format(report_path))
        report_columns, report_data = split_header_and_data(report)

        per_day_data = []
        for file_name in list_per_day_files(engine, category):
            columns, data = split_header_and_data(
                read_per_day_file(engine, category, file_name))
            assert columns == report_columns, (
                "Per-day file {} of category {} has different columns than the report "
                "({} vs {})".format(file_name, category, columns, report_columns))
            per_day_data.extend(data)

        # The report is a snapshot taken when generate ran, while sampling keeps
        # appending to the per-day files, so rows that landed between the generate
        # and this read are legitimately absent from the report. Requiring the
        # report to be a prefix still checks every one of its rows against the same
        # position on disk, and catches reordering, gaps and corruption.
        assert report_data, "Generated report for category {} has no data rows".format(category)
        assert per_day_data[:len(report_data)] == report_data, (
            "Report data rows do not match the start of the concatenation of per-day "
            "files for category {} ({} report rows vs {} per-day rows)".format(
                category, len(report_data), len(per_day_data)))

        header_count = sum(1 for line in report.splitlines()
                           if line.startswith(StatsConsts.HEADER_GROUP))
        assert header_count <= 1, \
            "Report header appears {} times, expected once".format(header_count)


def assert_report_covers_rows(engine, category, report_path, expected_rows):
    """Assert the report carries at least ``expected_rows`` data rows.

    Matching the concatenation is a prefix check, so a report holding only the
    newest day would still match one whose earlier days repeat the same rows.
    Counting the rows the store held when generate ran rules that out.
    """
    with allure.step("Check the report covers all {} day-file rows".format(expected_rows)):
        report = engine.run_cmd("sudo cat {}".format(report_path))
        row_count = len(split_header_and_data(report)[1])
        assert row_count >= expected_rows, (
            "Report for category {} holds {} data rows, fewer than the {} the per-day "
            "files held when it was generated".format(category, row_count, expected_rows))


def compare_recorded_to_live_counter(recorded_value, live_value, col_name):
    """Assert a recorded monotonic-counter sample is <= the current live value.

    'N/A' recorded values are accepted (counter absent on the port/transceiver).
    """
    if recorded_value == 'N/A':
        return
    assert float(recorded_value) <= float(live_value), (
        "Recorded {} value {} is greater than the live value {}".format(
            col_name, recorded_value, live_value))


def compare_dom_value(recorded_value, live_value, col_name,
                      tolerance=StatsConsts.DOM_COMPARE_TOLERANCE):
    """Assert a recorded DOM-sensor snapshot is within tolerance of the live reading."""
    if recorded_value == 'N/A':
        return
    diff = abs(float(recorded_value) - float(live_value))
    assert diff <= tolerance, (
        "Recorded DOM {} value {} differs from live {} by {} (> tolerance {})".format(
            col_name, recorded_value, live_value, diff, tolerance))


# --------------------------------------------------------------------------- #
# Retention / pruning
# --------------------------------------------------------------------------- #
def seed_old_day_file(engine, category, age_days):
    """Create a per-day file dated ``age_days`` in the past by copying the current
    header, so a retention cycle has an old day-file to prune. Returns its name.
    """
    directory = per_day_dir(category)
    old_file = "{}{}".format(stats_helpers.dut_day_string(engine, age_days),
                             StatsConsts.PER_DAY_FILE_EXT)
    with allure.step("Seed old per-day file {} ({} days old)".format(old_file, age_days)):
        existing = list_per_day_files(engine, category)
        if existing:
            source = "{}/{}".format(directory, existing[-1])
            engine.run_cmd("sudo cp {} {}/{}".format(source, directory, old_file))
        else:
            engine.run_cmd("sudo mkdir -p {}".format(directory))
            engine.run_cmd("sudo touch {}/{}".format(directory, old_file))
        # Backdate the file mtime too, in case retention keys off mtime.
        engine.run_cmd("sudo touch -d '{} days ago' {}/{}".format(age_days, directory, old_file))
    return old_file


def remove_day_files(engine, category, file_names):
    """Delete per-day files, so a test that seeded some leaves the history as it
    found it. Tolerates names that are already gone, since retention may have
    pruned them while the test ran.
    """
    for file_name in file_names or []:
        engine.run_cmd("sudo rm -f {}/{}".format(per_day_dir(category), file_name))


def assert_day_files_pruned(engine, category, history_duration, kept_files=None):
    """Assert every remaining per-day file is within the history window and that
    any seeded ``kept_files`` (recent) are still present.
    """
    with allure.step("Verify per-day files older than {} days are pruned".format(history_duration)):
        # Both sides of the comparison come off the switch clock: the names the
        # sampler wrote, and the oldest date the window still allows.
        cutoff = day_file_date(stats_helpers.dut_day_string(engine, int(history_duration)))
        remaining = list_per_day_files(engine, category)
        assert_names_carry_dates(remaining, category)
        for file_name in remaining:
            file_date = day_file_date(file_name)
            if file_date is None:
                continue
            assert file_date >= cutoff, (
                "Per-day file {} for {} is older than the {}-day window and was not pruned".format(
                    file_name, category, history_duration))
        for kept in (kept_files or []):
            assert kept in remaining, \
                "Expected in-window file {} to be kept for {}".format(kept, category)


# --------------------------------------------------------------------------- #
# CPU measurement (grey-box)
# --------------------------------------------------------------------------- #
def sampler_pids(engine):
    """Return every sampler pid, empty when it is not running.

    The daemon runs a parent and a worker, so callers get both.
    """
    output = engine.run_cmd("pgrep -x {}".format(StatsConsts.SAMPLER_SERVICE))
    return [line.strip() for line in output.strip().splitlines() if line.strip().isdigit()]


def _sampler_pids_or_fail(engine):
    pids = sampler_pids(engine)
    assert pids, "{} is not running, so its resource usage cannot be measured".format(
        StatsConsts.SAMPLER_SERVICE)
    return pids


def _read_unit_cpu_nanoseconds(engine):
    """Return the stats-reportd unit's cumulative CPU time in nanoseconds.

    Reads the cgroup counter rather than one process' /proc entry: the daemon
    forks a worker that does the sampling, so the parent - the pid systemd
    reports and pgrep lists first - accrues almost no CPU of its own. Handles
    both the v2 and v1 cgroup layouts, since switches run either one.
    """
    # Microseconds to nanoseconds by appending zeros rather than multiplying: awk
    # does its arithmetic in floating point and some implementations print a large
    # result in scientific notation, which int() below would not take.
    output = engine.run_cmd(
        "if [ -f {v2} ]; then awk '/^usage_usec/ {{print $2 \"000\"}}' {v2}; "
        "elif [ -f {v1} ]; then cat {v1}; else echo NONE; fi".format(
            v2=StatsConsts.CGROUP_V2_CPU_STAT, v1=StatsConsts.CGROUP_V1_CPUACCT))
    try:
        return int(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise AssertionError(
            "Could not read cgroup CPU accounting for stats-reportd: {!r}".format(output))


def measure_stats_reportd_cpu(engine, sample_seconds=10):
    """Return average stats-reportd CPU% over ``sample_seconds`` (grey-box).

    Measures the whole systemd unit through its cgroup counter. Two earlier
    approaches both reported a flat 0.00%: pidstat rounds to two decimals and
    this daemon sits below that on a quiet switch, and counting /proc clock ticks
    on the first pid tracks only the idle parent process. The cgroup counter has
    nanosecond resolution and covers the worker too. Raises rather than returning
    a default when it cannot measure, since a silent zero passes every limit.
    """
    with allure.step("Measure stats-reportd CPU over {}s".format(sample_seconds)):
        pids = _sampler_pids_or_fail(engine)
        before = _read_unit_cpu_nanoseconds(engine)
        started = time.time()
        time.sleep(sample_seconds)
        elapsed = time.time() - started
        after = _read_unit_cpu_nanoseconds(engine)
        assert _sampler_pids_or_fail(engine) == pids, \
            "stats-reportd restarted during the measurement window; the CPU sample is void"
        return (after - before) / 1e9 / elapsed * 100


def measure_stats_reportd_rss_mb(engine):
    """Return stats-reportd resident memory in MB (point sample, grey-box).

    Sums the parent and its worker, so the figure covers the process actually
    doing the sampling. Pages shared between the two after the fork are counted
    twice, which errs on the high side. RSS moves slowly compared with CPU, so a
    sample taken at the end of a measurement window represents the steady state
    well enough; callers wanting a peak can sample before and after.
    """
    with allure.step("Measure stats-reportd resident memory"):
        pids = _sampler_pids_or_fail(engine)
        output = engine.run_cmd("ps -o rss= -p {}".format(",".join(pids)))
        values = [line.strip() for line in output.strip().splitlines() if line.strip().isdigit()]
        if len(values) != len(pids):
            raise AssertionError(
                "Could not read resident memory for stats-reportd pids {}: {!r}".format(pids, output))
        return sum(int(value) for value in values) / 1024.0


def measure_file_bytes(engine, path):
    """Return the size of one file in bytes, 0 when it does not exist (grey-box)."""
    output = engine.run_cmd("sudo stat --format=%s {} 2>/dev/null || echo 0".format(path))
    try:
        return int(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise AssertionError("Could not size {}: {!r}".format(path, output))


def measure_dir_bytes(engine, path):
    """Return the disk usage of ``path`` in bytes (du, grey-box)."""
    output = engine.run_cmd("sudo du -sb {} 2>/dev/null | awk '{{print $1}}'".format(path))
    try:
        return int(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise AssertionError("Could not measure disk usage of {}: {!r}".format(path, output))


def project_daily_mb(delta_bytes, elapsed_sec):
    """Extrapolate an observed growth delta to a 24-hour total in MB.

    Sampling a whole day per category is impractical in a test run, so growth is
    measured over a short window and scaled up.
    """
    assert elapsed_sec > 0, "Cannot extrapolate growth over a zero-length window"
    per_second = delta_bytes / float(elapsed_sec)
    return per_second * StatsConsts.SECONDS_PER_DAY / StatsConsts.BYTES_PER_MB


# --------------------------------------------------------------------------- #
# Clock step (for retention / resilience)
# --------------------------------------------------------------------------- #
@contextmanager
def step_clock_forward_and_restore(engine, days=1):
    """Temporarily step the DUT clock forward by ``days`` and restore it after.

    Disables NTP sync while stepped (via timedatectl; best-effort
    stop of standalone chronyd/ntp) so it does not immediately snap back.
    """
    with allure.step("Step DUT clock forward by {} day(s)".format(days)):
        original = engine.run_cmd("date '+%Y-%m-%d %H:%M:%S'").strip()
        engine.run_cmd("sudo timedatectl set-ntp false 2>/dev/null || true")
        # Best-effort: stop standalone NTP daemons that timedatectl may not control.
        engine.run_cmd("sudo systemctl stop chronyd 2>/dev/null || true")
        engine.run_cmd("sudo systemctl stop ntp 2>/dev/null || true")
        engine.run_cmd("sudo date -s '+{} day' 2>/dev/null || true".format(days))
    try:
        yield
    finally:
        with allure.step("Restore DUT clock and re-enable NTP"):
            engine.run_cmd("sudo date -s '{}' 2>/dev/null || true".format(original))
            engine.run_cmd("sudo systemctl start chronyd 2>/dev/null || true")
            engine.run_cmd("sudo systemctl start ntp 2>/dev/null || true")
            engine.run_cmd("sudo timedatectl set-ntp true 2>/dev/null || true")


def clear_report_files(engine):
    """Empty the generated-report store (grey-box).

    Deleting one file at a time through the CLI is the documented path and the
    delete test covers it; setup and teardown want a known-empty store even when
    an earlier failure left the listing and the directory disagreeing, so the
    directory is emptied directly, as the older stats suite does.
    """
    engine.run_cmd("sudo rm -f {}/*".format(StatsConsts.EXTERNAL_PATH))


def generate_and_get_report_path(system, engine, category):
    """Generate a report for one category and return (file_name, /host/stats/ path).

    Clears existing report files first and then selects the file whose name
    belongs to this category, so callers that generate several categories in a
    loop always get the right file.
    """
    with allure.step("Generate report for category {}".format(category)):
        clear_report_files(engine)
        system.stats.category.categoryName[category].action_general(
            StatsConsts.GENERATE).verify_result()
        stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(
            system.stats.files.show()).get_returned_value()
        assert stats_files_show, "No report file listed after generate for {}".format(category)
        # Report names look like stats_<category>_<host>_<date>_<time>.csv; match on
        # the delimited category token so 'interface' does not match 'mgmt-interface'.
        token = "_{}_".format(category)
        matches = [f for f in stats_files_show if token in f]
        file_name = matches[0] if matches else list(stats_files_show)[0]
        return file_name, "{}/{}".format(StatsConsts.EXTERNAL_PATH, file_name)


def list_report_files(system):
    """Return the sorted report file names from ``nv show system stats files``."""
    show = OutputParsingTool.parse_json_str_to_dictionary(
        system.stats.files.show()).get_returned_value()
    return sorted(show or {})


def list_report_files_on_disk(engine):
    """Return the sorted report file names actually present under /host/stats."""
    output = engine.run_cmd("ls -1 {} 2>/dev/null".format(StatsConsts.EXTERNAL_PATH))
    return sorted(line.strip() for line in output.splitlines() if line.strip())


def generate_report(system, category, retries=3):
    """Generate one report for ``category`` and return the new file name.

    Report names carry a whole-second timestamp, so a generate repeated inside
    the same second reuses the name and adds nothing; retry until the listing
    grows. The listing is diffed rather than sorted-and-last so this stays
    correct once the store is full and each generate also evicts a file.
    """
    with allure.step("Generate a report for {}".format(category)):
        for _ in range(retries):
            before = set(list_report_files(system))
            system.stats.category.categoryName[category].action_general(
                StatsConsts.GENERATE).verify_result()
            added = set(list_report_files(system)) - before
            if added:
                return added.pop()
            time.sleep(1)
        raise AssertionError(
            "Generate produced no new report file for {} in {} attempts".format(category, retries))


def _newest_day_files(day_files, count, where):
    """Split ``day_files`` into the newest ``count`` and the rest.

    Per-day names are ISO dates, so sorting them as text sorts them by date.
    Names that carry no date are returned as expected-present rather than judged
    against the cap, since their position in the order is unknown. ``where``
    names the place the list came from, for the message if none of them parse.
    """
    assert_names_carry_dates(day_files, where)
    dated = sorted(name for name in day_files if day_file_date(name))
    undated = [name for name in day_files if not day_file_date(name)]
    split = max(len(dated) - count, 0)
    return dated[split:] + undated, dated[:split]


@contextmanager
def techsupport_dump(system, engine, option="", since_time="", verify_size=False):
    """Generate a tech-support bundle, extract it, and clean both copies up.

    Yields the archive path the CLI reported. The cleanup has to run even when an
    assertion fails, or the next dump inherits this one's extracted tree and the
    switch slowly fills with bundles. It is skipped when no bundle was produced:
    the component derives the extracted path from the file name, which is empty
    until a generate succeeds, and would remove the whole dump directory.
    """
    with allure.step("Generate tech-support{}".format(
            " with '{}'".format(option) if option else "")):
        try:
            archive, _ = system.techsupport.action_generate(
                engine=engine, option=option, since_time=since_time, verify_size=verify_size)
            logger.info("The techsupport file name is: %s", archive)
            assert system.techsupport.file_name, (
                "No tech-support bundle was produced: {}. With an option in play "
                "('{}'), check it against 'nv action generate system tech-support "
                "--help' on this build".format(archive, option))
            system.techsupport.extract_techsupport_files(engine)
            yield archive
        finally:
            if system.techsupport.file_name:
                system.techsupport.cleanup(engine)
                system.techsupport.files.file_name[system.techsupport.file_name].action_delete()


def validate_per_day_files_in_techsupport(system, engine, files_by_category):
    """Assert a tech-support dump carries the newest TECHSUPPORT_MAX_DAY_FILES
    per-day files of each category and leaves the older ones behind.

    The new categories keep a directory per category (stats/<category>/<date>.csv),
    so the listing has to be taken one level deeper than the flat layout the older
    stats helper looks at. Individual files may or may not be gzipped in the dump,
    so both names are accepted.

    Tech-support keeps fewer days than the history-duration retained on disk, so a
    category with a longer history has to come back trimmed. Asserted as a count
    of newest files rather than an age cut-off: see the constant for why the two
    differ and which one the product enforces.
    """
    def dump_holds(day_file, in_dump):
        return day_file in in_dump or "{}.gz".format(day_file) in in_dump

    cap = StatsConsts.TECHSUPPORT_MAX_DAY_FILES
    with allure.step("Check tech-support keeps {} day files per category".format(cap)):
        # verify_size is the other half of the trim: keeping fewer days than the
        # disk holds exists to bound the bundle, so assert the archive fits the
        # platform limit rather than only counting the files inside it.
        with techsupport_dump(system, engine, verify_size=True):
            for category, day_files in files_by_category.items():
                in_dump = stats_helpers.per_day_files_in_dump(system, engine, category)
                expected, too_old = _newest_day_files(
                    day_files, cap, per_day_dir(category))
                assert len(in_dump) <= cap, (
                    "tech-support holds {} per-day files for {}, past the cap of {}: "
                    "{}".format(len(in_dump), category, cap, in_dump))
                for day_file in expected:
                    assert dump_holds(day_file, in_dump), \
                        "Expected {} in tech-support stats/{}, which contains {}".format(
                            day_file, category, in_dump)
                for day_file in too_old:
                    assert not dump_holds(day_file, in_dump), (
                        "{} is not among the newest {} day files of {}, yet the dump holds "
                        "it: {}".format(day_file, cap, category, in_dump))


def validate_since_limits_day_files(system, engine, category, older_files):
    """Assert 'since' decides which per-day files of a new category a dump carries.

    The files stay on disk either way, so a dump taken since yesterday must hold
    today's and none of ``older_files``, which is what tells a working ``since``
    apart from a dump that simply ignores it for these categories.
    """
    since = engine.run_cmd("date -d '1 day ago' '+{}'".format(
        StatsConsts.TECHSUPPORT_SINCE_DATE_FORMAT)).strip()
    today_file = current_day_file_name(engine)
    with allure.step("Tech-support since {} carries only {} for {}".format(
            since, today_file, category)):
        with techsupport_dump(system, engine, option=SystemConsts.ACTIONS_GENERATE_SINCE,
                              since_time=since):
            in_dump = stats_helpers.per_day_files_in_dump(system, engine, category)
            assert today_file in in_dump or "{}.gz".format(today_file) in in_dump, \
                "Tech-support since {} is missing today's file {} for {}: {}".format(
                    since, today_file, category, in_dump)
            behind_since = [f for f in older_files
                            if f in in_dump or "{}.gz".format(f) in in_dump]
            assert not behind_since, (
                "Tech-support since {} still carries {} for {}, so 'since' does not "
                "reach the new categories".format(since, behind_since, category))


def validate_techsupport_without_stats(system, engine, categories):
    """Assert a tech-support generated with the skip-stats flag carries no
    historical statistics for any of ``categories``.
    """
    option = StatsConsts.TECHSUPPORT_SKIP_STATS_OPTION
    with allure.step("Expect no history in a tech-support taken with '{}'".format(option)):
        with techsupport_dump(system, engine, option=option):
            for category in categories:
                in_dump = stats_helpers.per_day_files_in_dump(system, engine, category)
                assert not in_dump, (
                    "Tech-support generated with '{}' still carries per-day files for {}: "
                    "{}".format(option, category, in_dump))


# --------------------------------------------------------------------------- #
# Image upgrade
# --------------------------------------------------------------------------- #
ImageInstallResult = namedtuple('ImageInstallResult', ['ok', 'skipped', 'running'])


def running_image_build_id(image_path=None):
    """Return the build-id the switch is running, or the one ``image_path`` holds.

    ``nv show system image`` reports a build-id rather than a file name, so the
    two are compared through the same normalization the upgrade lane uses.
    """
    if image_path is not None:
        return image_path.name.replace('-amd64', '').replace('.bin', '')
    from ngts.tests_nvos.general.upgrade_downgrade.helpers import get_system_image
    return get_system_image().current


def install_image(engines, topology_obj, image_path):
    """Fetch an NVOS image from the sonic-mgmt host and install it with a reboot.

    Returns ``skipped`` when the switch already runs that image, which makes the
    call safe to repeat - the upgrade test relies on that to put the starting
    image back only when it actually left it.

    The fetch deliberately passes the whole URL with an empty ``base_url``: given
    a bare path, ``action_fetch`` prepends ImageConsts.SCP_PATH, whose credentials
    come from the VM_USER / VM_PASSWORD environment variables that only the MARS
    lanes set.
    """
    from ngts.nvos_tools.infra.IpTool import IpTool
    from ngts.nvos_tools.system.System import System

    system = System()
    wanted = running_image_build_id(image_path)
    with allure.step("Install image {}".format(image_path.name)):
        current = running_image_build_id()
        if current == wanted:
            logger.info("Switch already runs %s, nothing to install", wanted)
            return ImageInstallResult(ok=True, skipped=True, running=current)

        with allure.step("Uninstall the image on the other partition"):
            # Without this the install answers "Image ... is already installed.
            # Reboot skipped" whenever the wanted image is still on the other
            # partition - which it always is once the two images have been
            # alternated - and the switch simply stays where it is. The upgrade
            # matrix clears the partition for the same reason. The result is
            # ignored the way the matrix ignores it: there is nothing to remove on
            # a switch that has only ever booted one image.
            system.image.action_uninstall(params='force', verify_res=False)

        url = ImageConsts.SCP_PATH_SERVER.format(
            username=engines.sonic_mgmt.username,
            password=engines.sonic_mgmt.password,
            ip=IpTool.format_ip_for_uri(engines.sonic_mgmt),
            path=image_path)
        with allure.step("Fetch the image onto the switch"):
            system.image.action_fetch(url, base_url='').verify_result()

        with allure.step("Install and reboot into it"):
            system.image.files.file_name[image_path.name].action_file_install_with_reboot(
                topology_obj=topology_obj).verify_result()

        running = running_image_build_id()
        logger.info("Running image after install: %s (wanted %s)", running, wanted)
        return ImageInstallResult(ok=running == wanted, skipped=False, running=running)


# Re-export shared helpers so the test module has one import site.
set_system_stats_to_default = stats_helpers.set_system_stats_to_default
set_cache_duration = stats_helpers.set_cache_duration
validate_upload_stats_file = stats_helpers.validate_upload_stats_file
validate_external_file_header_and_data = stats_helpers.validate_external_file_header_and_data
