"""Pure helpers for CPO reset and link-up tests.

Keep this module free of DUT state and engines. That makes lifecycle rules and
Portia port grouping testable without hardware.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping

from ngts.nvos_constants.constants_nvos import Cpov2Consts
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import InternalNvosConsts
from ngts.nvos_tools.platform.Cpo import Cpo

CPO_SW_LINK_UP_TIMEOUT_SECONDS: int = InternalNvosConsts.NVL7_SW_LINK_UP_TIMEOUT
CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS = InternalNvosConsts.DEFAULT_TIMEOUT
CPO_POLL_INTERVAL_SECONDS = 2

_SW_PORT_PATTERN = re.compile(r"^(sw\d+p\d+s)(\d+)$")
_COUNTER_NAME_PATTERN = re.compile(r"(?:error|drop)", re.IGNORECASE)
_INTEGER_PATTERN = re.compile(r"^\s*(\d[\d,]*)\s*$")


def mapping_snapshot(cpo_detail: Mapping, els_detail: Mapping) -> dict[str, object]:
    """Capture relationships that must survive CPO/ELS reset operations."""
    mapping_fields = (Cpov2Consts.PORTS, Cpov2Consts.LASER_SOURCES, Cpov2Consts.OE)
    return {
        **{field: tuple(sorted(Cpo.split_names(cpo_detail[field]))) for field in mapping_fields},
        Cpov2Consts.PARENT: els_detail[Cpov2Consts.PARENT],
    }


def assert_mapping_unchanged(before: Mapping[str, object], cpo_detail: Mapping, els_detail: Mapping) -> None:
    after = mapping_snapshot(cpo_detail, els_detail)
    assert after == before, f"CPO mapping changed across reset: before={before}, after={after}"


def laser_sibling_ports(port_name: str, available_ports: Iterable[str], lanes_per_laser: int = 4) -> list[str]:
    """Return the other Portia ``sw`` ports driven by the same laser.

    Portia exposes consecutive ``sN`` lanes.  One ELS laser fans out to four
    lanes, so s1-s4 and s5-s8 form the two laser groups in each eight-lane swX
    group.  Restrict the result to the DUT inventory so future breakout shapes
    fail clearly instead of producing phantom ports.
    """
    assert lanes_per_laser > 0, "lanes_per_laser must be positive"
    match = _SW_PORT_PATTERN.fullmatch(port_name)
    assert match, f"unsupported Portia sw port name: {port_name!r}"
    prefix, lane_text = match.groups()
    lane = int(lane_text)
    group_start = ((lane - 1) // lanes_per_laser) * lanes_per_laser + 1
    expected = {f"{prefix}{index}" for index in range(group_start, group_start + lanes_per_laser)}
    inventory = set(available_ports)
    missing = expected - inventory
    assert not missing, f"laser group for {port_name} is incomplete; missing ports: {sorted(missing)}"
    return sorted(expected - {port_name})


def carrier_down_count(counters: Mapping) -> int:
    """Extract link.carrier-down-count from a counters payload.

    Reset tests use its delta to prove a port actually dropped.
    """
    link_counters = counters.get("link")
    assert isinstance(link_counters, Mapping), f"counters payload has no link section: {sorted(counters)}"
    value = link_counters.get("carrier-down-count")
    assert value is not None, f"link counters have no carrier-down-count: {sorted(link_counters)}"
    return int(str(value).replace(",", ""))


def error_drop_counters(data: Mapping, prefix: str = "") -> dict[str, int]:
    """Flatten numeric error/drop counters from a nested counter payload."""
    counters: dict[str, int] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            counters.update(error_drop_counters(value, path))
            continue
        match = _INTEGER_PATTERN.fullmatch(str(value))
        if _COUNTER_NAME_PATTERN.search(path) and match:
            counters[path] = int(match.group(1).replace(",", ""))
    return counters


def assert_no_counter_increase(before: Mapping[str, int], after: Mapping[str, int], context: str) -> None:
    increased = {
        name: (before.get(name, 0), after.get(name))
        for name in before.keys() | after.keys()
        if after.get(name, before.get(name, 0)) > before.get(name, 0)
    }
    assert not increased, f"{context} error/drop counters increased: {increased}"


def poll_until[T](  # noqa: PLR0913 - clock/sleep injection keeps polling tests deterministic
    read: Callable[[], T],
    predicate: Callable[[T], bool],
    *,
    timeout_seconds: float,
    description: str,
    interval_seconds: float = CPO_POLL_INTERVAL_SECONDS,
    acceptable_exceptions: tuple[type[Exception], ...] = (),
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Poll a value until a lifecycle condition is met, returning the last read.

    ``acceptable_exceptions`` raised by ``read`` are transient (mid-reset CLI
    or parse blips): polling continues, and the last one is re-raised with
    context only if the deadline expires without a conclusive read.
    """
    assert timeout_seconds >= 0, "timeout_seconds must be non-negative"
    assert interval_seconds > 0, "interval_seconds must be positive"
    deadline = clock() + timeout_seconds
    last_value: T | None = None
    last_error: Exception | None = None
    while True:
        try:
            last_value = read()
            last_error = None
            if predicate(last_value):
                return last_value
        except acceptable_exceptions as error:
            last_error = error
        if clock() >= deadline:
            break
        sleep(interval_seconds)
    if last_error is not None:
        raise AssertionError(
            f"Timed out after {timeout_seconds}s waiting for {description}; last read failed: {last_error}"
        ) from last_error
    raise AssertionError(f"Timed out after {timeout_seconds}s waiting for {description}; last value: {last_value!r}")
