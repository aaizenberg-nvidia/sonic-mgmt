import logging
import re
import string
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum

from ngts.constants.constants import GnmiConsts
from ngts.ngts_types import DevicesT, EnginesT
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_constants.telemetry_mapping import (
    GNMI_ENUM_RENAMES_BY_KIND,
    NVUE_TO_TELEMETRY,
    AlarmFlags,
    TelemetryLeaf,
)
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.PollingTool import PollingTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.gnmi.constants import (
    GNMI_WATCH_CMD_TIME_SEC,
    GnmiMode,
    GnmiServerStatus,
    GnmiStreamMode,
)
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.helpers import (
    _extract_json_objects_from_gnmic_output,
    build_leaf_value_timeline,
    get_gnmic_engine,
    is_gnmi_failure,
    parse_gnmi_status,
)
from ngts.tools.test_utils import allure_utils as allure

__all__ = [
    "Reads",
    "TelemetrySource",
    "TelemetryWatch",
    "Timeline",
    "assert_bounced",
    "assert_unchanged",
    "gnmi_server_enabled",
    "gnmi_instances",
    "gnmi_snapshot",
    "gnmi_watch",
    "read_all",
    "read_mismatches",
    "telemetry_xpaths",
]

logger = logging.getLogger()


class TelemetrySource(StrEnum):
    NVUE = "nvue"
    OPENAPI = "openapi"
    GNMI = "gnmi"
    DB = "db"


Timeline = dict[str, list[tuple[int, object]]]
Reads = dict[str, dict[TelemetrySource, object]]

_IDENTITYREF_PATTERN = re.compile(r"^[A-Za-z_][\w.-]*:([A-Z][A-Z0-9_]*)$")
_NVUE_PLACEHOLDER_PATTERN = re.compile(r"\\\{(\w+)\\\}")
_INSTANCE_INDEX_PATTERN = re.compile(r"-(\d+)$")
_ABSENT_VALUES = ("", "N/A")
_TRUE_VALUES = ("true", "on", "1")
_FALSE_VALUES = ("false", "off", "0")
_DISPLAY_UNIT_CHARS = string.ascii_letters + "%°µ "


@dataclass
class TelemetryWatch:
    paths: list[str]
    baseline: dict[str, object]
    timeline: Timeline = field(default_factory=dict)
    err: str = ""


def _strip_identityref_prefix(value: object) -> object:
    match = _IDENTITYREF_PATTERN.match(value) if isinstance(value, str) else None
    return match.group(1) if match else value


def _glob_matches(glob: str, path: str) -> bool:
    """`*` matches within one path segment."""
    return re.fullmatch("[^/]*".join(map(re.escape, glob.split("*"))), path) is not None


def _nvue_path_keys(pattern: str, resource_path: str) -> dict[str, str] | None:
    """Path instance keys plus `<key>_index` / zero-based `<key>_index0` for instances named `<prefix>-N`;
    a `*` instance selects every instance."""
    regex = _NVUE_PLACEHOLDER_PATTERN.sub(r"(?P<\1>[^/]+)", re.escape(pattern))
    match = re.fullmatch(regex, resource_path)
    if not match:
        return None
    keys = match.groupdict()
    for name, value in match.groupdict().items():
        if value == "*":
            keys |= {f"{name}_index": value, f"{name}_index0": value}
        elif index := _INSTANCE_INDEX_PATTERN.search(value):
            keys |= {f"{name}_index": index.group(1), f"{name}_index0": str(int(index.group(1)) - 1)}
    return keys


def _pinned_row(component: BaseComponent) -> tuple[str, dict[str, str], dict[str, TelemetryLeaf]]:
    resource_path = component.get_resource_path()
    for pattern, leaves in NVUE_TO_TELEMETRY.items():
        keys = _nvue_path_keys(pattern, resource_path)
        if keys is not None:
            return pattern, keys, leaves
    raise AssertionError(f"no pinned telemetry mapping for {resource_path}")


def _resolve(template: str, keys: Mapping[str, str], row: str) -> str:
    try:
        return template.format(**keys)
    except KeyError as missing:
        raise AssertionError(f"pinned row {row} needs runtime key {missing} for {template!r}") from None


def telemetry_xpaths(component: BaseComponent, fields: list[str] | None = None, /,
                     **runtime_keys: str) -> dict[str, str]:
    """Field -> concrete gNMI xpath for the pinned leaves of `component` (all of them by default)."""
    pattern, keys, leaves = _pinned_row(component)
    unknown = set(fields or ()) - leaves.keys()
    assert not unknown, f"fields {sorted(unknown)} are not pinned for {pattern}"
    return {field_path: _resolve(leaf.gnmi, keys | runtime_keys, f"{pattern} {field_path}")
            for field_path, leaf in leaves.items() if leaf.gnmi and (fields is None or field_path in fields)}


def _gnmic_client(engines: EnginesT, devices: DevicesT) -> GnmiClient:
    return GnmiClient(engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, devices.dut.default_username,
                      devices.dut.default_password, cmd_time=GNMI_WATCH_CMD_TIME_SEC, print_outputs=False,
                      spool_output=True, engine=get_gnmic_engine(engines))


def gnmi_snapshot(engines: EnginesT, devices: DevicesT, paths: list[str]) -> dict[str, object]:
    """Latest value per leaf; a leaf without a value is absent."""
    out, err, _, _ = _gnmic_client(engines, devices).gnmic_subscribe_multi_path(
        prefix="", paths=paths, mode=GnmiMode.ONCE, skip_cert_verify=True, wait_till_done=True)
    for stream in (err, out):
        assert not is_gnmi_failure(stream), f"gNMI ONCE on {paths} failed: {stream.strip()}"
    timeline = build_leaf_value_timeline(_extract_json_objects_from_gnmic_output(out))
    return {path: entries[-1][1] for path, entries in timeline.items()}


def gnmi_instances(engines: EnginesT, devices: DevicesT, component: BaseComponent, field_path: str, /,
                   **runtime_keys: str) -> dict[str, object]:
    """Normalized gNMI value of `field_path` per instance the one `*` in the component path or pinned xpath selects."""
    _, _, leaves = _pinned_row(component)
    xpath = telemetry_xpaths(component, [field_path], **runtime_keys)[field_path]
    instance = re.compile("([^/]*)".join(map(re.escape, xpath.split("*"))) + "(?:/.*)?")
    return {match.group(1): _normalize(leaves[field_path].kind, value)
            for path, value in gnmi_snapshot(engines, devices, [xpath]).items() if (match := instance.fullmatch(path))}


def _active_subscriptions() -> int:
    status = parse_gnmi_status(System().gnmi_server.status.show())
    return int(status.get(GnmiServerStatus.TOTAL_ACTIVE_SUBSCRIPTIONS, 0))


@contextmanager
def gnmi_watch(engines: EnginesT, devices: DevicesT, paths: list[str],
               stream_mode: str = GnmiStreamMode.ON_CHANGE) -> Iterator[TelemetryWatch]:
    """`.timeline` is filled on exit; failures land in `.err` so they never mask the with-block's own."""
    paths = [path.strip("/") for path in paths]
    client = _gnmic_client(engines, devices)
    with allure.step(f"Start gNMI {stream_mode} watch on {len(paths)} leaves"):
        watch = TelemetryWatch(paths, baseline=gnmi_snapshot(engines, devices, paths))
        subscriptions_before = _active_subscriptions()
        _, _, _, process = client.gnmic_subscribe_multi_path(
            prefix="", paths=paths, mode=GnmiMode.STREAM, stream_mode=stream_mode, log_to_stderr=True,
            skip_cert_verify=True, keep_session_alive=True, wait_till_done=False)
    try:
        # the remote runner cannot poll gnmic, and a trigger fired before registration is never streamed
        PollingTool.poll_until(_active_subscriptions, lambda count: count > subscriptions_before,
                               timeout_seconds=GNMI_WATCH_CMD_TIME_SEC,
                               description="gnmi-server registers the watch")
        yield watch
    finally:
        with allure.step("Stop gNMI watch"):
            try:
                out, err = client.close_session_and_get_out_and_err(process)
                watch.timeline = build_leaf_value_timeline(_extract_json_objects_from_gnmic_output(out))
                if is_gnmi_failure(err) or is_gnmi_failure(out):
                    watch.err = err.strip() or out.strip()
            except Exception as error:
                watch.err = f"failed to stop gnmic: {error}"
            logger.info("gNMI watch timeline: %s", watch.timeline)


def _matched_entries(watch: TelemetryWatch, glob: str) -> dict[str, list[tuple[int, object]]]:
    assert not watch.err, f"telemetry watch failed: {watch.err}"
    paths = {path for path in [*watch.paths, *watch.timeline] if _glob_matches(glob, path)}
    assert paths, f"no watched leaf matches {glob}; watched {watch.paths}"
    silent = sorted(path for path in paths if not watch.timeline.get(path))
    assert not silent, f"leaves never streamed: {silent}"
    return {path: watch.timeline[path] for path in sorted(paths)}


def _spelling_neutral(value: object) -> object:
    return _normalize("enum", value)


def _matched_values(watch: TelemetryWatch, glob: str) -> dict[str, list[object]]:
    return {path: [_spelling_neutral(value) for _, value in entries]
            for path, entries in _matched_entries(watch, glob).items()}


def assert_bounced(watch: TelemetryWatch, glob: str, steady: object) -> None:
    """Every matching leaf left `steady` (changed or deleted) and returned to it."""
    steady = _spelling_neutral(steady)
    failures = {path: values for path, values in _matched_values(watch, glob).items()
                if all(value == steady for value in values) or values[-1] != steady}
    assert not failures, f"leaves did not bounce away from and back to {steady!r}: {failures}"


def assert_unchanged(watch: TelemetryWatch, glob: str) -> None:
    """Every matching leaf kept its pre-watch value; an ON_CHANGE tick can deliver the first value after the trigger."""
    failures = {path: values for path, values in _matched_values(watch, glob).items()
                if any(value != _spelling_neutral(watch.baseline.get(path, values[0])) for value in values)}
    assert not failures, f"leaves changed while they should not have: {failures}"


@contextmanager
def gnmi_server_enabled() -> Iterator[None]:
    """Restores gnmi-server to disabled only if it was disabled before."""
    gnmi_server = System().gnmi_server
    was_enabled = gnmi_server.parsed_show_gnmi()[GnmiConsts.GNMI_STATE_FIELD] == GnmiConsts.GNMI_STATE_ENABLED
    if not was_enabled:
        with allure.step("Enable gnmi-server"):
            gnmi_server.enable_gnmi_server().verify_result()
    try:
        yield
    finally:
        if not was_enabled:
            with allure.step("Restore gnmi-server to disabled"):
                gnmi_server.disable_gnmi_server().verify_result()


def _show(component: BaseComponent, api: str) -> dict:
    with TestToolkit.force_api(api):
        return OutputParsingTool.parse_json_str_to_dictionary(component.show()).get_returned_value()


def _field(output: object, field_path: str) -> object:
    for key in field_path.split("/"):
        if not isinstance(output, Mapping) or key not in output:
            return None
        output = output[key]
    return output


def read_all(engines: EnginesT, devices: DevicesT, component: BaseComponent, fields: list[str] | None = None, /, *,
             asic: str = "", **runtime_keys: str) -> Reads:
    """Pinned leaves with a CLI field (or only `fields`); `runtime_keys` fill placeholders the NVUE path lacks;
    `asic` is the DB namespace."""
    pattern, keys, leaves = _pinned_row(component)
    keys |= runtime_keys
    xpaths = telemetry_xpaths(component, fields, **runtime_keys)
    leaves = {field_path: leaf for field_path, leaf in leaves.items()
              if (fields is None or field_path in fields) and leaf.cli}
    gnmi_paths = [xpaths[field_path] for field_path in leaves if field_path in xpaths]
    gnmi_values = gnmi_snapshot(engines, devices, gnmi_paths) if gnmi_paths else {}
    nvue_output, openapi_output = _show(component, ApiType.NVUE), _show(component, ApiType.OPENAPI)
    reads: Reads = {}
    for field_path, leaf in leaves.items():
        sources: dict[TelemetrySource, object] = {
            TelemetrySource.NVUE: _field(nvue_output, field_path),
            TelemetrySource.OPENAPI: _field(openapi_output, field_path),
        }
        if field_path in xpaths:
            sources[TelemetrySource.GNMI] = gnmi_values.get(xpaths[field_path])
        if leaf.db:
            sources[TelemetrySource.DB] = _db_read(engines, asic, leaf, keys, f"{pattern} {field_path}")
        reads[field_path] = {source: _normalize(leaf.kind, value) for source, value in sources.items()}
    logger.info("4-way read of %s: %s", component.get_resource_path(), reads)
    return reads


def _db_read(engines: EnginesT, asic: str, leaf: TelemetryLeaf, keys: Mapping[str, str], row: str) -> str:
    assert leaf.db, f"pinned row {row} has no DB leg"
    db_name, key_template, field_templates = leaf.db
    db_key = _resolve(key_template, keys, row)

    def hget(field_template: str) -> str:
        return Tools.DatabaseTool.sonic_db_cli_hget(
            engines.dut, asic, db_name, db_key, _resolve(field_template, keys, row)).strip()

    if isinstance(field_templates, str):
        return hget(field_templates)
    return _alarm_from_flags(leaf.kind, AlarmFlags(*map(hget, field_templates)))


def _alarm_from_flags(kind: str, flags: AlarmFlags) -> str:
    if not any(flags):
        return ""
    alarm, warning = "1" in (flags.high_alarm, flags.low_alarm), "1" in (flags.high_warning, flags.low_warning)
    severity = "critical" if alarm else "warning" if warning else "none"
    if kind == "alarm_severity":
        return severity
    return "off" if severity == "none" else "on"


def read_mismatches(component: BaseComponent, reads: Reads, expected: Reads | None = None) -> Reads:
    """Fields where a source differs from the gNMI value, or from its own `expected` value, beyond the pinned
    tolerance."""
    _, _, leaves = _pinned_row(component)
    mismatches: Reads = {}
    for field_path, sources in reads.items():
        for source, value in sources.items():
            reference = (expected[field_path].get(source) if expected
                         else sources.get(TelemetrySource.GNMI, sources[TelemetrySource.NVUE]))
            if not _same_value(value, reference, leaves[field_path].tolerance):
                mismatches[field_path] = sources
                break
    return mismatches


def _same_value(value: object, reference: object, tolerance: float) -> bool:
    if isinstance(value, float) and isinstance(reference, float):
        return abs(value - reference) <= tolerance
    return value == reference


def _to_float(text: str, value: object) -> object:
    try:
        return float(text.rstrip(_DISPLAY_UNIT_CHARS))
    except ValueError:
        return value


def _to_bool(text: str, value: object) -> object:
    lowered = text.lower()
    return True if lowered in _TRUE_VALUES else False if lowered in _FALSE_VALUES else value


def _to_index_list(text: str, value: object) -> list[int]:
    names = value if isinstance(value, Mapping | list) else text.split(",")
    return sorted(int(match.group()) for name in names if (match := re.search(r"\d+$", str(name).strip())))


_CONVERTERS: dict[str, Callable[[str, object], object]] = {
    "float": _to_float,
    "bool": _to_bool,
    "index_list": _to_index_list,
}


def _normalize(kind: str, value: object) -> object:
    """Any source value -> the gNMI-typed value; sonic-db-cli and xcvrd write '' and 'N/A' for absent."""
    renames = GNMI_ENUM_RENAMES_BY_KIND.get(kind)
    text = str(_strip_identityref_prefix(value)).strip() if value is not None else ""
    if renames is not None and text.lower() in renames:
        return renames[text.lower()]
    if text in _ABSENT_VALUES:
        return None
    if renames is not None:
        return re.sub(r"[\s-]+", "_", text).upper()
    return _CONVERTERS[kind](text, value) if kind in _CONVERTERS else text
