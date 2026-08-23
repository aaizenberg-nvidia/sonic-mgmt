"""Shared helpers for the system stats test suites.

Extracted from test_system_stats.py so both the existing stats suite and the
Counters Historical Analysis suite (test_counters_historical.py) can reuse them
from a single source of truth.
"""
from datetime import datetime, timedelta
import base64
import logging
import re
import shlex
import time

import pandas as pd
import pytest
from retry import retry

from devts.infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine
from ngts.ngts_types import EnginesT

from ngts.nvos_constants.constants_nvos import DatabaseConst, NvosConst
from ngts.tests_nvos.system.stats.constants import StatsConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.infra.DatabaseTool import DatabaseTool
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


def set_system_stats_to_default(engine: ProxySshEngine, system: System) -> None:
    with allure.step("Update Stats feature to default"):
        system.stats.unset(apply=True).verify_result()

    with allure.step("Update cache general configuration to default"):
        set_cache_duration(engine, 10)
        set_cache_duration(engine, 1)


def set_cache_duration(engine, minutes):
    """Set the stats cache duration in CONFIG_DB."""
    DatabaseTool.sonic_db_cli_hset(
        engine, "", db_name=DatabaseConst.CONFIG_DB_NAME,
        db_config="STATS_CONFIG|GENERAL",
        param="cache_duration", value=str(minutes))


def configure_all_categories(system, category_list, interval=None,
                             history_duration=None, state=None, apply=True):
    """Configure all categories with the given parameters and optionally apply."""
    for name in category_list:
        if state is not None:
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.STATE,
                op_param_value=state).verify_result()
        if interval is not None:
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.INTERVAL,
                op_param_value=int(interval)).verify_result()
        if history_duration is not None:
            system.stats.category.categoryName[name].set(
                op_param_name=StatsConsts.HISTORY_DURATION,
                op_param_value=int(history_duration)).verify_result()
    if apply:
        SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].
                                        apply_config, TestToolkit.engines.dut, False).verify_result()


def clear_all_internal_and_external_files(engine: ProxySshEngine, system: System, category_list: list[str]) -> None:
    system.stats.action_general(StatsConsts.CLEAR).verify_result()
    stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()).get_returned_value()
    if stats_files_show != "":
        for file in stats_files_show.keys():
            system.stats.files.file_name[file].action_delete().verify_result()
    # clear old files if exist
    engine.run_cmd("sudo rm -f /var/stats/*.old")


def uses_per_day_layout(engine: ProxySshEngine) -> bool:
    """True when the internal store keeps a directory of <date>.csv per category.

    Counters Historical Analysis brought both the per-day layout and the new
    categories, so the category list answers this even when /var/stats is empty
    - which it is right after a clear, where looking at the store would lie.
    """
    listed = OutputParsingTool.parse_json_str_to_dictionary(
        System(None).stats.category.show(dut_engine=engine)).get_returned_value()
    return bool(set(listed) & set(StatsConsts.NEW_CATEGORIES))


def current_category_file(engine: ProxySshEngine, category: str) -> str:
    """Path of the file the sampler is currently writing for the category."""
    if not uses_per_day_layout(engine):
        return "{}/{}{}".format(StatsConsts.INTERNAL_ROOT, category, StatsConsts.PER_DAY_FILE_EXT)
    return _per_day_path(engine, category, age_days=0)


def dut_day_string(engine: ProxySshEngine, age_days: int = 0) -> str:
    """The date ``age_days`` back as the DUT itself reports it.

    The sampler names its files from the switch clock, and a test may hold that
    clock stepped forward or sit in a different timezone from the runner, so a
    date computed here would point at a file the switch never wrote.
    """
    return engine.run_cmd("date -d '{} days ago' '+{}'".format(
        age_days, StatsConsts.PER_DAY_FILE_DATE_FORMAT)).strip()


def _per_day_path(engine: ProxySshEngine, category: str, age_days: int) -> str:
    return "{}/{}{}".format(StatsConsts.INTERNAL_CAT_DIR.format(category=category),
                            dut_day_string(engine, age_days), StatsConsts.PER_DAY_FILE_EXT)


def install_internal_file(engine: ProxySshEngine, category: str, age_days: int = 0) -> None:
    """Copy a canned CSV already staged in /tmp into the internal store, so a test
    can hand the sampler a file of its choosing.

    Flat images take it as <category>.csv; per-day images take it as a <date>.csv
    inside the category directory, dated ``age_days`` back - which is how a test
    hands retention something old enough to prune.
    """
    source = "{}/{}{}".format(StatsConsts.INTERNAL_PATH, category, StatsConsts.PER_DAY_FILE_EXT)
    if not uses_per_day_layout(engine):
        engine.run_cmd("sudo cp {} {}".format(source, StatsConsts.INTERNAL_ROOT))
        return
    engine.run_cmd("sudo mkdir -p {d} && sudo cp {s} {t}".format(
        d=StatsConsts.INTERNAL_CAT_DIR.format(category=category),
        s=source, t=_per_day_path(engine, category, age_days)))


def remove_internal_files(engine: ProxySshEngine) -> None:
    """Empty the internal store, whether it holds flat files or category directories."""
    engine.run_cmd("sudo rm -rf {}/*".format(StatsConsts.INTERNAL_ROOT))


def install_out_of_window_file(engine: ProxySshEngine, category: str, age_days: int) -> None:
    """Hand the sampler a canned file that retention has to get rid of.

    Per-day images take it as a day file dated out of the history window and lose
    today's file, since a day file is dropped whole by the date in its name and
    its rows are never rewritten - old samples inside today's file stay put.
    Flat images keep one file per category, so the canned file goes there and its
    old rows are the ones the sampler drops.
    """
    if not uses_per_day_layout(engine):
        install_internal_file(engine, category)
        return
    install_internal_file(engine, category, age_days=age_days)
    engine.run_cmd("sudo rm -f {}".format(_per_day_path(engine, category, age_days=0)))


def create_old_samples_internal_file(engine: ProxySshEngine, name: str, num_samples: int = 5) -> None:
    """Seed the internal /var/stats/<name>.csv with old-dated samples that match the DUT's
    own column schema, so the retention/cleanup check has stale rows to purge.

    The schema is derived from the DUT's current internal file (its header is written as soon
    as the category is enabled) rather than uploaded from a static fixture whose column layout
    drifts per-platform (e.g. a 2-PSU jaguar file vs an 8-PSU switch) and produces a ragged CSV
    that crashes the timestamp parser. Only the timestamp column matters to the cleanup check,
    so data columns get a benign placeholder; the timestamps are set far in the past.
    """
    internal_file = "/var/stats/{}.csv".format(name)

    # Restart stats-reportd so it re-initializes and writes the category header promptly
    # (~5s); otherwise the internal file only appears after a full sampling interval (up to
    # 1 min) and the read below would race ahead of it.
    engine.run_cmd("sudo systemctl restart stats-reportd")
    header_block = ""
    for _ in range(10):
        header_block = engine.run_cmd("sudo cat {} 2>/dev/null".format(internal_file))
        if header_block.strip():
            break
        time.sleep(3)
    assert header_block.strip(), \
        "internal stats file {} was not created after restarting stats-reportd".format(internal_file)

    # Keep the comment block (incl. its blank lines) up to and including the CSV column-header
    # line — the first non-comment, non-blank line (e.g. "Timestamp,PSU 1 Voltage [V],...").
    header_lines = []
    column_header = None
    for line in header_block.splitlines():
        header_lines.append(line)
        if line.startswith('#') or not line.strip():
            continue
        column_header = line
        break
    assert column_header, "no CSV column header found in {}".format(internal_file)
    num_columns = len(column_header.split(','))

    # Build old-dated rows matching the schema: past timestamp + placeholder data columns.
    old_base = datetime(2023, 5, 10, 16, 23, 18)
    placeholder = ','.join(['0'] * (num_columns - 1))
    old_rows = [
        "{},{}".format((old_base + timedelta(minutes=i)).strftime(StatsConsts.TIMESTAMP_FORMAT),
                       placeholder)
        for i in range(num_samples)
    ]

    content = '\n'.join(header_lines + old_rows) + '\n'
    # base64 round-trip avoids all shell quoting/newline issues when writing the file back.
    encoded = base64.b64encode(content.encode()).decode()
    engine.run_cmd("echo '{}' | base64 -d | sudo tee {} > /dev/null".format(encoded, internal_file))


def internal_row_count(engine: ProxySshEngine, category: str) -> int:
    """Data rows a category holds in the internal store, across both layouts."""
    if uses_per_day_layout(engine):
        target = "{}/*{}".format(StatsConsts.INTERNAL_CAT_DIR.format(category=category),
                                 StatsConsts.PER_DAY_FILE_EXT)
    else:
        target = "{}/{}{}".format(StatsConsts.INTERNAL_ROOT, category,
                                  StatsConsts.PER_DAY_FILE_EXT)
    output = engine.run_cmd("cat {} 2>/dev/null | grep -vc '^#' || echo 0".format(target))
    try:
        return int(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def wait_for_internal_rows_between(engine: ProxySshEngine, category: str, min_rows: int,
                                   max_rows: int, tries: int = 24, delay: int = 5) -> None:
    """Wait for a category's internal store to hold between min_rows and max_rows rows.

    Both bounds matter after retention runs: the upper one says the old samples
    are gone, the lower one that the sampler has written into the store again, so
    a report generated next has something in it.
    """
    @retry(AssertionError, tries=tries, delay=delay)
    def _wait():
        rows = internal_row_count(engine, category)
        assert min_rows <= rows <= max_rows, \
            "{} holds {} rows, waiting for {}-{}".format(category, rows, min_rows, max_rows)

    _wait()


def wait_for_new_samples(engine: ProxySshEngine, category: str, since_rows: int = None,
                         tries: int = 24, delay: int = 5) -> None:
    """Wait for the sampler to write rows beyond the ones a category already holds.

    Per-day images append to whatever day file they find and never rewrite one, so
    a test that hands the sampler a damaged file cannot look for a fresh file in
    its place. What it can ask is that the damage did not stop the next samples.
    """
    start = internal_row_count(engine, category) if since_rows is None else since_rows

    @retry(AssertionError, tries=tries, delay=delay)
    def _wait():
        rows = internal_row_count(engine, category)
        assert rows > start, \
            "{} still holds {} rows, waiting for a sample past {}".format(category, rows, start)

    _wait()


def file_size_bytes(engine: ProxySshEngine, path: str):
    """Size of a file on the DUT, or None when its size could not be read.

    Missing is kept apart from empty on purpose: a caller waiting for a file to
    shrink would take an unreadable file as a zero-byte one and stop waiting,
    which turns a broken path or a lost sudo into a pass.
    """
    output = engine.run_cmd("sudo stat --format=%s {} 2>/dev/null || echo unreadable".format(path))
    lines = output.strip().splitlines()
    last = lines[-1].strip() if lines else ""
    return int(last) if last.isdigit() else None


def internal_file_size_bytes(engine: ProxySshEngine, category: str) -> int:
    """Size of the file the sampler is currently writing, 0 when there is none."""
    return file_size_bytes(engine, current_category_file(engine, category)) or 0


def assert_internal_file_kept_whole(engine: ProxySshEngine, category: str, size_before: int) -> None:
    """Assert a day file that grew past 600MB was left alone, rotation included.

    Truncating at 600MB into a <category>.csv.old belonged to the single-file
    design. Per-day images prune by dropping whole day files, so a large file has
    to survive until its date leaves the history window.
    """
    size_after = internal_file_size_bytes(engine, category)
    assert size_after >= size_before, \
        "{} shrank from {} to {} bytes - it was truncated".format(category, size_before, size_after)
    rotated = engine.run_cmd("sudo ls {} 2>/dev/null | grep '.old' || true".format(
        StatsConsts.INTERNAL_CAT_DIR.format(category=category)))
    assert not rotated.strip(), "{} was rotated into {}".format(category, rotated.strip())


def _sampled_categories(engine: ProxySshEngine, category_list: list[str], per_day: bool) -> set[str]:
    """Return the categories that hold sampled data under the internal store.

    Only the layout the image actually uses counts, so a flat file left behind
    on a per-day image is not mistaken for live data.
    """
    target = "{root}/{cat}/*{ext}" if per_day else "{root}/{cat}{ext}"
    probe = "".join(
        ("ls " + target + " >/dev/null 2>&1 && echo {cat}; ").format(
            root=StatsConsts.INTERNAL_ROOT, cat=cat, ext=StatsConsts.PER_DAY_FILE_EXT)
        for cat in category_list)
    output = engine.run_cmd(probe)
    return {line.strip() for line in output.splitlines() if line.strip() in category_list}


def categories_with_internal_data(engine: ProxySshEngine, category_list: list[str]) -> set[str]:
    """Which of ``category_list`` currently hold sampled data in the internal store."""
    return _sampled_categories(engine, category_list, uses_per_day_layout(engine))


def category_has_internal_data(engine: ProxySshEngine, category: str) -> bool:
    """True when the category holds sampled data, whichever layout the image uses."""
    return bool(categories_with_internal_data(engine, [category]))


def check_category_internal_files_exist(engine: ProxySshEngine, category_list: list[str],
                                        retries: int = 40, interval: int = 5) -> None:
    """Wait for every category to hold sampled data in the internal store.

    The per-day layout creates a category's directory only when its first sample
    lands, where the flat layout created the file at startup, so the default
    budget covers a minimum-interval cycle rather than a handful of seconds.
    Callers whose categories sit on longer intervals have to raise it.
    """
    per_day = uses_per_day_layout(engine)
    start_time = time.perf_counter()
    for attempt in range(1, retries + 1):
        sampled = _sampled_categories(engine, category_list, per_day)
        missing = [cat for cat in category_list if cat not in sampled]
        if not missing:
            duration = time.perf_counter() - start_time
            logger.info("All {} categories sampled in {:.1f}s ({} attempts)".format(
                len(category_list), duration, attempt))
            OperationTime.save_manual_operation_duration_to_db(
                'stats category files generation', duration, pytest.test_name)
            break
        logger.info("Attempt {}/{}: still missing {} categories: {}".format(
            attempt, retries, len(missing), missing))
        time.sleep(interval)
    else:
        duration = time.perf_counter() - start_time
        assert False, "After {:.1f}s ({} retries), still missing categories: {}".format(
            duration, retries, missing)

    known = set(category_list) | set(StatsConsts.NEW_CATEGORIES)
    known.update("{}{}".format(cat, StatsConsts.PER_DAY_FILE_EXT) for cat in category_list)
    known.update(("mgmt-interface.csv.old", "asic-power.csv.old", "verification_test"))
    entries = list(filter(None, engine.run_cmd("ls {}".format(StatsConsts.INTERNAL_ROOT)).strip().split()))
    extra = [entry for entry in entries if entry not in known]

    assert not extra, f"Unexpected extra files found: {extra}"


def validate_upload_stats_file(engines: EnginesT, system: System, file: str, delete: bool = True) -> None:
    """
    validate upload stats file with scp and sftp
    """
    upload_protocols = ['scp', 'sftp']
    player = engines['sonic_mgmt']
    dest_path = NvosConst.MARS_RESULTS_FOLDER

    with allure.step("Upload stats file to player {} with the next protocols : {}".format(player.ip, upload_protocols)):
        for protocol in upload_protocols:
            with allure.step("Upload stats file to player with {} protocol".format(protocol)):
                upload_path = '{}://{}:{}@{}{}'.format(
                    protocol, player.username, player.password, player.ip, dest_path)
                system.stats.files.file_name[file].action_upload(upload_path).verify_result()

            with allure.step("Validate file was uploaded to player"):
                assert player.run_cmd(cmd='ls {} | grep {}'.format(dest_path, file)), \
                    "Did not find the file with ls cmd"

            if delete:
                with allure.step("Delete uploaded file"):
                    player.run_cmd(cmd='rm -f {}{}'.format(dest_path, file))


def parse_stats_csv(file_path):
    """Parse a stats CSV file, returning header metadata and a pandas DataFrame.

    Returns:
        dict with keys:
            'hostname'    — hostname from the file header (with -mgmt2 stripped)
            'group'       — category/group name
            'export_time' — the "Started sampling" timestamp
            'column_descriptions' — list of column names from "# Column N:" lines
            'df'          — DataFrame with all data rows (all values as strings)
    """
    metadata = {'column_descriptions': []}
    with open(file_path, 'r') as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith(StatsConsts.HEADER_HOSTNAME):
                metadata['hostname'] = stripped.replace('-mgmt2', '')
            elif stripped.startswith(StatsConsts.HEADER_GROUP):
                metadata['group'] = stripped.replace(StatsConsts.HEADER_GROUP, '')
            elif stripped.startswith(StatsConsts.HEADER_TIME):
                metadata['export_time'] = datetime.strptime(
                    stripped.replace(StatsConsts.HEADER_TIME, ''), StatsConsts.TIMESTAMP_FORMAT)
            elif stripped.startswith('# Column'):
                metadata['column_descriptions'].append(stripped.split(': ')[-1])
            elif not stripped.startswith('#') and stripped:
                break

    df = pd.read_csv(file_path, comment='#', keep_default_na=False, dtype=str)
    metadata['df'] = df
    return metadata


def assert_value_in_range(col_name, value, min_val, max_val, sample_num, category, allow_na=True):
    """Assert that a CSV cell value falls within [min_val, max_val].

    Some columns report one value per lane, slash-delimited (e.g. interface
    'linkdown-reason-code-local' = '23/23/23/23' on a 4-lane port, plain '0' on a
    single-lane one), so every lane is range-checked independently. A cell with no
    delimiter is simply the one-lane case.

    Args:
        allow_na: If True, 'N/A' values are silently accepted.
                  If False, 'N/A' values cause an assertion failure.
    """
    # 'N/A' contains the lane delimiter, so shield it before splitting; otherwise a
    # partially-absent cell like '0/N/A/0' would tear into bogus 'N' and 'A' lanes.
    na_placeholder = '\x00'
    shielded = str(value).replace('N/A', na_placeholder)
    for lane_value in shielded.split('/'):
        lane_value = lane_value.strip().replace(na_placeholder, 'N/A')
        if lane_value == 'N/A':
            assert allow_na, (
                f"{category} '{col_name}' has N/A at sample #{sample_num} (not permitted)")
            continue
        try:
            numeric = float(lane_value)
        except ValueError:
            raise AssertionError(
                f"{category} '{col_name}' = {value} is not numeric at sample #{sample_num}")
        assert min_val <= numeric <= max_val, (
            f"{category} '{col_name}' = {value} not in [{min_val}, {max_val}] at sample #{sample_num}")


def assert_boolean_value(col_name, value, sample_num, category, allow_na=True):
    """Assert that a flag column holds a boolean (transceiver fault/LOS flags)."""
    text = str(value).strip()
    if text == 'N/A':
        assert allow_na, (
            f"{category} '{col_name}' has N/A at sample #{sample_num} (not permitted)")
        return
    assert text.lower() in ('true', 'false'), (
        f"{category} '{col_name}' = {value} is not a boolean at sample #{sample_num}")


def _validate_row_from_config(config, col_names, row_values, num_of_columns, sample_num, category):
    """Apply validation rules from a category config dict to a single data row."""
    # Columns holding a state enum rather than a number (e.g. interface
    # 'phy-manager-state' = RECEIVER_DETECT). Matched by name so the config
    # survives column reordering; they are checked for presence, not range.
    non_numeric = set(config.get('non_numeric_columns', ()))
    # (regex, kind, min, max) rules matched against the column name, first match
    # wins over the generic range. Name matching lets a single rule cover every lane
    # of a per-lane field ('rx-power1 [mW]' .. 'rx-power8 [mW]') whatever the
    # module's lane count.
    column_patterns = config.get('column_patterns', ())

    def check(col_idx, min_val, max_val, allow_na):
        col_name = col_names[col_idx]
        value = row_values[col_idx]
        if col_name in non_numeric:
            assert str(value).strip(), (
                f"{category} '{col_name}' is empty at sample #{sample_num}")
            return
        for regex, kind, rule_min, rule_max in column_patterns:
            if re.search(regex, col_name):
                if kind == 'bool':
                    assert_boolean_value(col_name, value, sample_num, category, allow_na)
                else:
                    assert_value_in_range(col_name, value, rule_min, rule_max,
                                          sample_num, category, allow_na)
                return
        assert_value_in_range(col_name, value, min_val, max_val, sample_num, category, allow_na)

    if 'columns' in config:
        for col_idx, min_val, max_val, allow_na in config['columns']:
            if col_idx < num_of_columns:
                check(col_idx, min_val, max_val, allow_na)

    if 'uniform_range' in config:
        r = config['uniform_range']
        start = r['start']
        end = num_of_columns if r['end'] is None else r['end']
        for col_idx in range(start, end):
            check(col_idx, r['min'], r['max'], r['allow_na'])

    if 'remaining_columns' in config:
        r = config['remaining_columns']
        start = r['start']
        end = num_of_columns if r['end'] is None else (num_of_columns + r['end'] if r['end'] < 0 else r['end'])
        for col_idx in range(start, end):
            check(col_idx, r['min'], r['max'], r['allow_na'])

    if 'tail_columns' in config:
        t = config['tail_columns']
        for col_idx in range(num_of_columns - t['count'], num_of_columns):
            check(col_idx, t['min'], t['max'], t['allow_na'])


def validate_external_file_header_and_data(name, file_path, hostname, start_time, end_time):
    with allure.step(f"validate external file header and data - {file_path}"):
        meta = parse_stats_csv(file_path)
        df = meta['df']

        with allure.step("Validate header metadata"):
            assert meta['hostname'] == StatsConsts.HEADER_HOSTNAME + hostname, \
                f"unexpected hostname: {meta['hostname']} vs {hostname}"
            assert meta['group'] == name, \
                f"unexpected group: {meta['group']} vs {name}"

            if start_time and end_time:
                assert start_time < meta['export_time'] < end_time, \
                    f"Started sampling time {meta['export_time']} not between {start_time} and {end_time}"

        with allure.step("Validate column descriptions match DataFrame columns"):
            assert meta['column_descriptions'] == list(df.columns), \
                "mismatch between # Column descriptions and CSV header columns"

        with allure.step("Validate data is not empty"):
            assert len(df) > 0, f"No data rows in {name} stats file"

        config = StatsConsts.CATEGORY_VALIDATION_CONFIG.get(name)
        assert config is not None, \
            f"Unknown stats category '{name}' — no validation config. " \
            f"Known: {list(StatsConsts.CATEGORY_VALIDATION_CONFIG.keys())}"

        export_time = meta['export_time']
        col_names = list(df.columns)
        num_of_columns = len(col_names)

        with allure.step(f"Validate {name} data rows ({len(df)} rows)"):
            for row_idx, row in df.iterrows():
                sample_num = row_idx + 1
                row_values = list(row)

                assert len(row_values) == num_of_columns, \
                    f"Row {sample_num} has {len(row_values)} values, expected {num_of_columns}"

                _validate_row_from_config(config, col_names, row_values,
                                          num_of_columns, sample_num, name)

        # Row-per-entity categories (interface / peer-port / transceivers) write one
        # row per port per cycle, so a single cycle spans many rows sharing one
        # timestamp. Cadence therefore lives in the distinct timestamps; for the
        # one-row-per-sample categories that is simply the row sequence.
        sample_times = list(dict.fromkeys(df.iloc[:, 0].tolist()))

        row_per_entity = config.get('row_per_entity', False)

        with allure.step(f"Validate sampling cadence ({len(sample_times)} samples)"):
            if row_per_entity:
                # Restarting stats-reportd (which the tests do to apply config) can make
                # the first samples bunch closer together than the interval, so only an
                # oversized gap - a genuinely skipped cycle - fails here. That the
                # cadence equals the configured interval is asserted directly by
                # test_counters_historical_interval_reflection.
                max_gap = timedelta(minutes=int(StatsConsts.INTERVAL_MIN),
                                    seconds=StatsConsts.SAMPLE_CADENCE_TOLERANCE_SEC)
                times = [datetime.strptime(t, StatsConsts.TIMESTAMP_FORMAT)
                         for t in sample_times]
                for i in range(1, len(times)):
                    gap = times[i] - times[i - 1]
                    assert timedelta(0) < gap <= max_gap, \
                        f"{name}: gap {gap} between samples {times[i - 1]} and " \
                        f"{times[i]} exceeds the {max_gap} allowed"
            else:
                prev_sample_time = export_time - timedelta(minutes=int(StatsConsts.INTERVAL_MIN))
                for timestamp_str in sample_times:
                    prev_sample_time = check_sample_timestamp([timestamp_str], prev_sample_time, name)

        with allure.step("Validate minimum sample count"):
            assert len(sample_times) >= StatsConsts.MIN_EXPECTED_SAMPLES, \
                f"{name}: only {len(sample_times)} samples collected, " \
                f"expected >= {StatsConsts.MIN_EXPECTED_SAMPLES}"

        with allure.step("Validate timestamp ordering"):
            # Row-per-entity categories repeat a timestamp for every port in the cycle,
            # so their rows only have to be non-decreasing (the strict increase between
            # cycles is enforced by the cadence walk above). One-row-per-sample
            # categories keep the stricter rule, where a repeat means a double write.
            timestamps = [datetime.strptime(t, StatsConsts.TIMESTAMP_FORMAT)
                          for t in df.iloc[:, 0].tolist()]
            for i in range(1, len(timestamps)):
                in_order = (timestamps[i] >= timestamps[i - 1] if row_per_entity
                            else timestamps[i] > timestamps[i - 1])
                assert in_order, \
                    f"{name}: timestamp at row {i + 1} ({timestamps[i]}) does not follow " \
                    f"row {i} ({timestamps[i - 1]})"


def validate_external_file_timestamps(file_name: str, clear_time: datetime) -> None:
    full_path = NvosConst.MARS_RESULTS_FOLDER + file_name
    meta = parse_stats_csv(full_path)
    df = meta['df']

    assert not df.empty, f"No data rows found in {file_name}"

    for row_idx, row in df.iterrows():
        sample_time = datetime.strptime(row.iloc[0], StatsConsts.TIMESTAMP_FORMAT)
        assert sample_time > clear_time, \
            f"Sample at row {row_idx + 1} has timestamp {sample_time} earlier than clear time {clear_time}"


def validate_number_of_lines_in_external_file(engines: EnginesT, system: System, cat_name: str,
                                              min_lines: int, max_lines: int) -> None:
    player = engines['sonic_mgmt']

    with allure.step("Clear external files"):
        engines.dut.run_cmd("sudo rm -f /host/stats/*.csv")

    with allure.step("Generate system stats category"):
        system.stats.category.categoryName[cat_name].action_general(StatsConsts.GENERATE).verify_result()
        stats_files_show = OutputParsingTool.parse_json_str_to_dictionary(system.stats.files.show()). \
            get_returned_value()
        file_name = list(stats_files_show)[0]

    with allure.step("Upload stats file to URL"):
        validate_upload_stats_file(engines, system, file_name, False)

    with allure.step("Validate number of lines in file"):
        full_path = NvosConst.MARS_RESULTS_FOLDER + file_name
        with open(full_path, 'r') as f:
            num_of_lines = sum(1 for _ in f)
        assert min_lines < num_of_lines < max_lines, \
            f"Number of lines: {num_of_lines} is not as expected: {min_lines}-{max_lines}"

    with allure.step("Delete uploaded file"):
        player.run_cmd(cmd='rm -f {}'.format(full_path))


def get_stats_file_line_count(engine: ProxySshEngine, file_path: str) -> int:
    """Return the number of lines in a DUT stats file, or -1 if the file is absent."""
    quoted_path = shlex.quote(file_path)
    output: str = engine.run_cmd(f"if [ -e {quoted_path} ]; then wc -l < {quoted_path}; else echo -1; fi")
    return int(output.strip().splitlines()[-1])


def per_day_files_in_dump(system: System, engine: ProxySshEngine, category: str) -> list[str]:
    """Return the per-day file names a tech-support dump holds for ``category``.

    Reads the listing rather than trusting the directory to exist: when the dump
    carries no history for a category the command that lists it returns its own
    error text, which holds no per-day file name and so reads as empty.
    """
    listing = system.techsupport.get_techsupport_files_list(engine, "stats/{}".format(category))
    return [name for name in listing
            if name.endswith(StatsConsts.PER_DAY_FILE_EXT) or
            name.endswith("{}.gz".format(StatsConsts.PER_DAY_FILE_EXT))]


def validate_stats_files_exist_in_techsupport(system: System, engine: ProxySshEngine,
                                              categories: list[str]) -> None:
    """
    generate techsupport and validate each category's stats exist in the stats dir

    Flat images keep one <category>.csv per category, per-day images a directory
    of <date>.csv files, so what counts as present differs between them.
    """
    per_day = uses_per_day_layout(engine)
    try:
        tech_support_folder, duration = system.techsupport.action_generate(engine=engine)
        logger.info("The techsupport file name is : " + tech_support_folder)
        system.techsupport.extract_techsupport_files(engine)
        techsupport_files_list = system.techsupport.get_techsupport_files_list(engine, 'stats')
        for category in categories:
            if per_day:
                in_dump = per_day_files_in_dump(system, engine, category)
                assert in_dump, \
                    "Expect per-day files for {} in the tech support stats dir {}".format(
                        category, techsupport_files_list)
            else:
                expected = "{}{}.gz".format(category, StatsConsts.PER_DAY_FILE_EXT)
                assert expected in techsupport_files_list, \
                    "Expect to have {} file, in the tech support stats files {}".format(
                        expected, techsupport_files_list)

    finally:
        system.techsupport.cleanup(engine)
        if system.techsupport.file_name:
            system.techsupport.files.file_name[system.techsupport.file_name].action_delete()


def check_sample_timestamp(row, prev_sample_time, category):
    timestamp_str = row[0] if isinstance(row, list) else row.iloc[0]
    sample_time = datetime.strptime(timestamp_str, StatsConsts.TIMESTAMP_FORMAT)
    expected_time = prev_sample_time + timedelta(minutes=int(StatsConsts.INTERVAL_MIN))
    time_low_thresh = expected_time - timedelta(seconds=5)
    time_high_thresh = expected_time + timedelta(seconds=5)
    assert time_low_thresh < sample_time < time_high_thresh, \
        f"{category} timestamp {sample_time} is too far from expected {expected_time}"
    return sample_time


def wait_for_stats_file_cleanup(engine, category, max_size_kb=1024):
    """Wait for stats-reportd to finish cleaning up a huge internal file.

    Polls the size of the file the sampler is currently writing until it drops
    below max_size_kb, indicating that stats-reportd has completed the cleanup
    and created a new file.
    """
    stats_file = current_category_file(engine, category)

    @retry(AssertionError, tries=24, delay=5)
    def _wait():
        size = file_size_bytes(engine, stats_file)
        assert size is not None, \
            f"Could not read the size of {stats_file} - the cleanup wait cannot tell whether " \
            f"stats-reportd shrank it"
        file_size_kb = size / 1024
        assert file_size_kb < max_size_kb, \
            f"stats-reportd still cleaning up {stats_file} " \
            f"(size: {file_size_kb:.0f}KB, waiting for < {max_size_kb}KB)"

    _wait()


def generate_huge_stats_file(engine, category, target_bytes=StatsConsts.HUGE_FILE_TARGET_BYTES):
    """Generate a huge stats file (>600MB) on the device using its own header.

    Uses the header of the file the sampler is currently writing, so the copy
    carries the correct platform-specific header regardless of device type.
    Replicates a real data line as many times as that line's width needs to pass
    target_bytes - a fixed line count lands short on a category with narrow rows,
    and a file under the threshold says nothing about what happens above it.

    The copy is built next to the file it replaces, so the device never has to
    hold two of them on separate filesystems.
    """
    stats_file = current_category_file(engine, category)
    engine.run_cmd(
        f"sudo bash -c '"
        f"grep \"^#\" {stats_file} > {stats_file}.huge && "
        f"grep -v \"^#\" {stats_file} | head -1 >> {stats_file}.huge && "
        f"DATA=$(grep -v \"^#\" {stats_file} | tail -1) && "
        f"LINES=$(( {target_bytes} / (${{#DATA}} + 1) + 1 )) && "
        f"yes \"$DATA\" | head -n $LINES >> {stats_file}.huge && "
        f"mv {stats_file}.huge {stats_file}'"
    )


def get_header_number_of_lines(engines: EnginesT, category: str) -> int:
    with allure.step(f"Get {category} header number of lines"):
        stats_file = current_category_file(engines.dut, category)
        num_of_lines = int(engines.dut.run_cmd(f"grep -c '^#' {stats_file}")) + 2

    return num_of_lines


def wait_for_stats_file_to_shrink(engine: ProxySshEngine, category: str, max_size_kb: int = 1024) -> int:
    """Wait for stats-reportd to finish cleaning up a huge internal file.

    Polls the size of the file the sampler is currently writing until it drops
    below max_size_kb, indicating that stats-reportd has completed the cleanup
    and created a new file.
    """
    counter = 0
    stats_file = current_category_file(engine, category)

    @retry(AssertionError, tries=24, delay=5)  # ~2 min timeout
    def _wait():
        nonlocal counter
        counter += 1

        with allure.step(f"Check {category!r} stats file shrink: attempt {counter}"):
            size = file_size_bytes(engine, stats_file)
            assert size is not None, \
                f"Could not read the size of {stats_file} - the shrink wait cannot tell whether " \
                f"stats-reportd shrank it"
            file_size_kb = size / 1024
            assert file_size_kb < max_size_kb, \
                f"stats-reportd still cleaning up {stats_file} " \
                f"(size: {file_size_kb:.0f}KB, waiting for < {max_size_kb}KB)"

    _wait()
