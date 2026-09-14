"""Validation of the system alarms published over gNMI as OpenConfig `/system/alarms`.

One list entry per active condition, held to the same alarm catalog as NVUE
(through `system_alarm_nvue_helpers`, which also does the resource matching).
"""

import logging
import re
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Dict, List

from ngts.tools.test_utils import allure_utils as allure
from retry.api import retry_call

from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import AlarmsConsts, DateTimeConsts
from ngts.tests_nvos.helpers.alarms.system_alarm_nvue_helpers import (alarm_resource_matches_sensor,
                                                                      datetime_show, parse_dut_time,
                                                                      resolve_alarm_family,
                                                                      validate_severity_and_description)
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.constants import GnmiMode
from ngts.tests_nvos.system.gnmi.helpers import is_gnmi_failure, wait_for_gnmi_ready
from ngts.tests_nvos.helpers.alarms.helpers import NS_PER_SEC, epoch_ns_as_utc

logger = logging.getLogger(__name__)

GNMI_ALARMS_PATH = 'system/alarms'
# Identityref leaves are rendered with their YANG module ('nvidia-alarm-types:TEMPERATURE_HIGH').
_IDENTITYREF_LEAVES = ('type-id', 'severity')
# One `--format flat` line: `system/alarms/alarm[id=<id>]/state/<leaf>: <value>`.
_ALARM_LEAF_LINE = re.compile(r'alarm\[id=(?P<id>[^\]]+)\]/state/(?P<leaf>[a-z0-9-]+):\s*(?P<value>.*)$')


@dataclass
class AlarmEntry:
    """The state leaves of one active alarm. The id hashes resource, type, severity and
    time-created, so a severity change arrives as a new entry, not an update."""
    id: str = ''
    resource: str = ''
    type_id: str = ''
    severity: str = ''
    text: str = ''
    time_created: str = ''

    def missing_leaves(self) -> List[str]:
        return [f.name for f in fields(self) if not getattr(self, f.name)]

    def matches(self, alarm_type: str, alarm_origin_name: str) -> bool:
        return self.type_id == alarm_type and alarm_resource_matches_sensor(self.resource, alarm_origin_name)


@dataclass
class DutTimeRef:
    """One instant of the DUT clock: `local` for NVUE time-created, `epoch_ns` for gNMI/OTEL."""
    local: datetime
    epoch_ns: int


def _dut_epoch_ns(dut_datetime: dict) -> int:
    """`time-created` is oc-types:timeticks64: nanoseconds since the Unix epoch, not DUT-local wall time."""
    unix_time = dut_datetime.get(DateTimeConsts.UNIX_TIME)
    if unix_time:
        return int(float(str(unix_time).strip()) * NS_PER_SEC)
    universal_time = parse_dut_time(dut_datetime[DateTimeConsts.UNIVERSAL_TIME])
    return int(universal_time.replace(tzinfo=timezone.utc).timestamp() * NS_PER_SEC)


def get_dut_time_ref(system) -> DutTimeRef:
    """The DUT's current time in both forms, from a single `nv show system datetime`."""
    dut_datetime = datetime_show(system)
    return DutTimeRef(local=parse_dut_time(dut_datetime[DateTimeConsts.LOCAL_TIME]),
                      epoch_ns=_dut_epoch_ns(dut_datetime))


def gnmi_client_for_alarms(engines) -> GnmiClient:
    """A gnmic client for the DUT, built once the gNMI agent is up."""
    wait_for_gnmi_ready(engines)
    return GnmiClient(engines.dut.ip, int(GnmiConsts.GNMI_DEFAULT_PORT),
                      engines.dut.username, engines.dut.password, verify_tools_installed=True)


def parse_gnmi_alarms(output: str) -> Dict[str, AlarmEntry]:
    """Parse gnmic `--format flat` output into one `AlarmEntry` per alarm id."""
    alarms: Dict[str, AlarmEntry] = {}
    for match in map(_ALARM_LEAF_LINE.search, (output or '').splitlines()):
        if not match:
            continue
        field_name = match['leaf'].replace('-', '_')
        if not hasattr(AlarmEntry, field_name):
            continue
        value = match['value'].strip().strip('"')
        if match['leaf'] in _IDENTITYREF_LEAVES:
            value = value.split(':')[-1]
        setattr(alarms.setdefault(match['id'], AlarmEntry()), field_name, value)
    return alarms


def get_gnmi_alarms(gnmi_client: GnmiClient) -> Dict[str, AlarmEntry]:
    """The alarms gNMI reports as active right now, from one ONCE subscription.

    A failed read is asserted on, since it would otherwise parse as "no alarms active".
    """
    with allure.step("gNMI: read active system alarms"):
        output, err, _duration, _process = gnmi_client.gnmic_subscribe(
            prefix='', path=GNMI_ALARMS_PATH, mode=GnmiMode.ONCE, flat=True, skip_cert_verify=True,
            wait_till_done=True)
        assert not is_gnmi_failure(err) and not is_gnmi_failure(output), (
            f"gnmic failed to read '{GNMI_ALARMS_PATH}':\n  stdout: {output}\n  stderr: {err}"
        )
        alarms = parse_gnmi_alarms(output)
        logger.info(f"gNMI active alarms: {alarms}")
        return alarms


def _validate_gnmi_alarm_entry(alarm_id: str, alarm: AlarmEntry, alarm_type: str, raised_after_ns: int = None):
    """All leaves present, `state/id` equal to its list key, catalog severity and text, a real raise time."""
    missing = alarm.missing_leaves()
    assert not missing, f"gNMI alarm '{alarm_id}' is missing leaves {missing}: {alarm}"
    assert alarm.id == alarm_id, f"gNMI alarm '{alarm_id}': state/id='{alarm.id}' does not match its list key: {alarm}"

    collection_name = AlarmsConsts.COLLECTION_BY_TYPE.get(alarm_type, AlarmsConsts.COMPONENT_ALARMS)
    family = resolve_alarm_family(alarm.resource, collection_name)
    validate_severity_and_description(alarm_id, family, alarm_type, alarm.severity, alarm.text)

    # `--format flat` prints large uint64 leaves in scientific notation (Redmine #5073631, WontFix).
    time_created_ns = int(float(alarm.time_created))
    assert time_created_ns > 0, (
        f"gNMI alarm '{alarm_id}': time-created='{alarm.time_created}' is not epoch nanoseconds"
    )
    if raised_after_ns is not None:
        assert time_created_ns >= raised_after_ns, (
            f"gNMI alarm '{alarm_id}': time-created {epoch_ns_as_utc(time_created_ns)} is earlier than "
            f"{epoch_ns_as_utc(raised_after_ns)}, the DUT clock taken before the fault was injected; "
            f"the alarm was raised by something other than this fault"
        )


def _validate_gnmi_snapshot(alarms: Dict[str, AlarmEntry], alarm_origin_name: str, alarm_types: List[str],
                            expected_present: bool, raised_after_ns: int = None) -> None:
    for alarm_type in alarm_types:
        matched = {alarm_id: alarm for alarm_id, alarm in alarms.items()
                   if alarm.matches(alarm_type, alarm_origin_name)}
        logger.info(f"Matched gNMI {alarm_type} alarms for {alarm_origin_name}: {matched}")
        if not expected_present:
            assert not matched, (
                f"Expected no active {alarm_type} alarm for '{alarm_origin_name}' over gNMI, but got: {matched}"
            )
            continue
        assert matched, (
            f"Expected an active {alarm_type} alarm for '{alarm_origin_name}' over gNMI, "
            f"but the active alarms are: {alarms}"
        )
        for alarm_id, alarm in matched.items():
            _validate_gnmi_alarm_entry(alarm_id, alarm, alarm_type, raised_after_ns)


def validate_gnmi_alarms(gnmi_client: GnmiClient, alarm_origin_name: str, alarm_types: str | list[str], *,
                         expected_present: bool, raised_after_ns: int = None,
                         gnmi_alarms: Dict[str, AlarmEntry] = None) -> None:
    """Validate the alarms of `alarm_origin_name` in `/system/alarms`; the gNMI counterpart of
    `validate_nvue_alarms`. `raised_after_ns` is `get_dut_time_ref().epoch_ns` taken before the fault.

    A live read retries for ~1 minute (healthd's poll cycle); a passed `gnmi_alarms` snapshot is
    validated once, since retrying cannot change it.
    """
    alarm_types = [alarm_types] if isinstance(alarm_types, str) else alarm_types
    if gnmi_alarms is not None:
        _validate_gnmi_snapshot(gnmi_alarms, alarm_origin_name, alarm_types, expected_present, raised_after_ns)
        return
    retry_call(lambda: _validate_gnmi_snapshot(get_gnmi_alarms(gnmi_client), alarm_origin_name, alarm_types,
                                               expected_present, raised_after_ns),
               exceptions=AssertionError, tries=6, delay=10)
