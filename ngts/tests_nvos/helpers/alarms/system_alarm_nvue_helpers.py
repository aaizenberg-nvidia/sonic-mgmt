"""Alarm validation helper for `nv show system alarms`.

Checks that alarms are raised/cleared in response to DUT faults, and verifies severity, description, and raise time (if supplied). Also used by gNMI and OTLP `/system/alarms` validation.
"""

import logging
import re
from datetime import datetime
from typing import Dict, Tuple

from retry.api import retry_call

from ngts.nvos_constants.constants_nvos import AlarmsConsts, DateTimeConsts
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tests_nvos.helpers.general_helpers import normalize_sensor_name

logger = logging.getLogger(__name__)


def parse_dut_time(time_value: str) -> datetime:
    """Parse a DUT-rendered '<YYYY-MM-DD HH:MM:SS> [timezone]' value, dropping
    the timezone name when one is appended.
    """
    text = time_value.strip()
    head, _, tail = text.rpartition(' ')
    if head and tail.isalpha():
        text = head
    return datetime.strptime(text, AlarmsConsts.TIME_CREATED_FORMAT)


def datetime_show(system) -> dict:
    return Tools.OutputParsingTool.parse_json_str_to_dictionary(
        system.datetime.show()).verify_result()


def get_dut_time(system) -> datetime:
    """The DUT's current local time, to be taken before injecting a fault and
    handed to `validate_nvue_alarms` as `raised_after`.

    Read off the DUT rather than off the test runner, since an alarm's
    time-created is rendered in the DUT's own timezone and the two clocks can
    drift apart.
    """
    return parse_dut_time(datetime_show(system)[DateTimeConsts.LOCAL_TIME])


def resolve_alarm_family(resource, collection_name):
    """Map alarm resource to the resource token used
    """
    if collection_name == AlarmsConsts.SOFTWARE_ALARMS:
        return AlarmsConsts.SOFTWARE
    normalized_resource = normalize_sensor_name(resource)
    for token, family in AlarmsConsts.RESOURCE_BY_TOKEN:
        if token in normalized_resource:
            return family
    return None


def alarm_resource_matches_sensor(resource, sensor_name):
    """
    Return True if the sticky-alarm resource name could correspond to `sensor_name`.

    Sensors report their alarms under a shortened resource name (for example, 'CPU' for 'CPU-Pack-Temp', or 'PSU1' for 'PSU-1-Temp').
    This method checks if the normalized names overlap (substring match).
    """
    normalized_resource = normalize_sensor_name(resource)
    normalized_sensor = normalize_sensor_name(sensor_name)
    if not normalized_resource:
        return False
    return normalized_resource in normalized_sensor or normalized_sensor in normalized_resource


def _matching_alarms(alarm_collection, alarm_type, sensor_name):
    """Active `alarm_type` alarms in `alarm_collection` whose resource can carry
    `sensor_name`.
    """
    return {
        key: row for key, row in alarm_collection.items()
        if row.get(AlarmsConsts.TYPE) == alarm_type and
        alarm_resource_matches_sensor(row.get(AlarmsConsts.RESOURCE, ''), sensor_name)
    }


def _description_pattern(description):
    """Regex for a catalog description, with each '<...>' placeholder standing
    for the value filled in when the alarm is raised.
    """
    return '^' + '.+'.join(
        re.escape(part) for part in re.split(r'<[^>]*>', description)) + '$'


def validate_severity_and_description(key, family, alarm_type, severity, description=None):
    """Hold the severity and description of one raised alarm to what the alarm
    catalog fixes for (`family`, `alarm_type`), whichever API reported them.

    A component the catalog carries no text for still has its severity checked
    when the alarm type fixes the same one everywhere; whatever the catalog
    cannot resolve is left to the log.

    `description` is left out by an API that publishes no alarm text at all -
    the OTLP export carries only the resource, the type and the severity - and
    the catalog description is then not compared, while the severity still is.
    """
    catalog_entry = AlarmsConsts.CATALOG.get((family, alarm_type))
    if catalog_entry is None:
        # No catalog text for this component: assert whatever the type fixes on
        # its own and leave the description to the log.
        logger.info(f"Alarm '{key}': no catalog entry for family '{family}' and {alarm_type}, "
                    f"severity='{severity}', description='{description}'")
        expected_severity, expected_description = AlarmsConsts.SEVERITY_BY_TYPE.get(alarm_type), None
    else:
        expected_severity, expected_description = catalog_entry

    if expected_severity is None:
        logger.info(f"Alarm '{key}': no known severity for {alarm_type}, "
                    f"reported severity is '{severity}'")
    else:
        assert severity == expected_severity, (
            f"Alarm '{key}': expected severity='{expected_severity}', got '{severity}'"
        )
    if expected_description is not None and description is not None:
        assert re.match(_description_pattern(expected_description), description), (
            f"Alarm '{key}': expected description='{expected_description}', got '{description}'"
        )


def _validate_alarm_row(key, row, alarm_type, family, raised_after=None):
    """Validate the content of one sticky-alarm row: every leaf present, the
    severity and description the catalog fixes for (`family`, `alarm_type`), and
    a real raise time (a row shown as active must not carry the never-raised
    'N/A').

    `raised_after` is the DUT time taken before the fault was injected, when the
    caller has it: the row then also has to be timestamped at or after it, so a
    leftover alarm from an earlier fault cannot pass as the one just raised.
    """
    missing = [field for field in AlarmsConsts.ROW_FIELDS if not row.get(field)]
    assert not missing, f"Alarm '{key}' is missing fields {missing}: {row}"

    validate_severity_and_description(key, family, alarm_type, row[AlarmsConsts.SEVERITY],
                                      row[AlarmsConsts.DESCRIPTION])

    time_created = row[AlarmsConsts.TIME_CREATED]
    assert AlarmsConsts.TIME_CREATED_PATTERN.match(time_created), (
        f"Alarm '{key}': time-created='{time_created}' is not a raise "
        f"timestamp; an active alarm must carry the time it was raised"
    )
    if raised_after is not None:
        assert parse_dut_time(time_created) >= raised_after, (
            f"Alarm '{key}': time-created='{time_created}' is earlier than "
            f"'{raised_after}', the DUT time taken before the fault was injected; "
            f"the row was raised by something other than this fault"
        )
    assert key == f"{row[AlarmsConsts.RESOURCE]}:{row[AlarmsConsts.TYPE]}", (
        f"Alarm key '{key}' does not match its own resource/alarm-type: {row}"
    )


def validate_nvue_alarm(alarm_collections, alarm_type, alarm_origin_name, expected_present, raised_after=None):
    """Validate one alarm type of `alarm_origin_name` across the shown collections.

    The type decides which collection has to carry it (hardware alarms go to
    component-alarms, process/container alarms to software-alarms), so an alarm
    raised in the wrong collection fails even when it is present in the right
    one. The resource of each raised row then selects the catalog entry its
    severity and description are held to.
    """
    collection_name = AlarmsConsts.COLLECTION_BY_TYPE.get(alarm_type, AlarmsConsts.COMPONENT_ALARMS)
    matched = _matching_alarms(alarm_collections.get(collection_name, {}), alarm_type, alarm_origin_name)
    logger.info(f"Matched alarms: {matched}")

    if not expected_present:
        assert not matched, (
            f"Expected no active {alarm_type} alarm for sensor '{alarm_origin_name}', "
            f"but got: {matched}"
        )
        logger.info(f"No active {alarm_type} alarm for {alarm_origin_name}, as expected "
                    f"(active {collection_name}: {list(alarm_collections[collection_name])})")
        return

    assert matched, (
        f"Expected an active {alarm_type} alarm for sensor '{alarm_origin_name}' (under an "
        f"resource), but the active {collection_name} are: "
        f"{alarm_collections[collection_name]}"
    )
    for key, row in matched.items():
        family = resolve_alarm_family(row[AlarmsConsts.RESOURCE], collection_name)
        _validate_alarm_row(key, row, alarm_type, family, raised_after)
    for other_name, other_collection in alarm_collections.items():
        if other_name == collection_name:
            continue
        misplaced = _matching_alarms(other_collection, alarm_type, alarm_origin_name)
        assert not misplaced, (
            f"{alarm_type} must only be reported under '{collection_name}', but it is "
            f"also under '{other_name}': {misplaced}"
        )
    logger.info(f"Active {alarm_type} alarm raised for {alarm_origin_name} "
                f"under {collection_name}: {matched}")


def validate_nvue_alarms(system, alarm_origin_name: str, alarm_types: str | list[str], *, expected_present: bool,
                         raised_after: datetime = None, system_alarms: dict = None):
    """Validate the alarms of `alarm_origin_name` in `nv show system alarms`.

    A live read retries for ~1 minute (healthd's poll cycle); a passed `system_alarms` snapshot is validated once.
    """
    alarm_types = [alarm_types] if isinstance(alarm_types, str) else alarm_types

    def _check(collections):
        for alarm_type in alarm_types:
            validate_nvue_alarm(collections, alarm_type, alarm_origin_name, expected_present, raised_after)

    if system_alarms is not None:
        return _check(system_alarms)
    retry_call(lambda: _check(system.alarms.parse_show()), exceptions=AssertionError, tries=10, delay=10)
