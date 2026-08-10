"""Pure helpers for CPO event, syslog and health-transition checks.

Keep this module independent of the DUT object model so the event cycle
rules, the O-6 syslog gate and the health transitions are testable without
hardware; on-DUT callers pass thin closures over the object model.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime

from ngts.nvos_constants.constants_nvos import Cpov2Consts, EventConsts, HealthConsts
from ngts.tests_nvos.platform.cpo.action_helpers import CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS, poll_until

logger = logging.getLogger()

# O-6 in CPO_TEST_PLAN.md: the remove/add syslog wording must be pinned with
# the dev team. None keeps the syslog check an explicit no-op instead of
# encoding a guessed format. Templates are regexes that may reference {name}.
CPO_EVENT_SYSLOG_REGEX_TEMPLATES: tuple[str, ...] | None = None

# Events publish asynchronously after a reset CLI returns; the full cycle can
# lag as long as the module recovery itself, so share the recovery budget.
CPO_EVENT_POLL_TIMEOUT_SECONDS = CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS

# Mid-reset `nv show` reads can transiently fail (CLI error or partial output
# surfaces as a failed ResultObj => AssertionError); the pollers ride those out.
TRANSIENT_READ_ERRORS: tuple[type[Exception], ...] = (AssertionError,)

EventMatch = tuple[int, Mapping[str, str]]

# component type -> (ejected, inserted) event descriptions
INSERTION_CYCLE_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    HealthConsts.Component.CPO: (
        Cpov2Consts.CPO_EJECTED_EVENT,
        Cpov2Consts.CPO_INSERTED_EVENT,
    ),
    HealthConsts.Component.Laser_Source: (
        Cpov2Consts.LASER_SOURCE_EJECTED_EVENT,
        Cpov2Consts.LASER_SOURCE_INSERTED_EVENT,
    ),
}


def component_event_predicate(instance: str) -> Callable[[Mapping[str, str]], bool]:
    """Match every event whose Component column is exactly this instance."""

    def predicate(event: Mapping[str, str]) -> bool:
        return event.get(EventConsts.RESOURCE) == instance

    return predicate


def split_insertion_cycle(
    matches: Iterable[EventMatch], ejected_description: str, inserted_description: str
) -> tuple[list[EventMatch], list[EventMatch]]:
    """Partition one component's event matches into (ejected, inserted)."""
    ejected: list[EventMatch] = []
    inserted: list[EventMatch] = []
    for event_id, event in matches:
        text = event.get(EventConsts.TEXT, "")
        if re.search(ejected_description, text):
            ejected.append((event_id, event))
        elif re.search(inserted_description, text):
            inserted.append((event_id, event))
    return ejected, inserted


def assert_insertion_follows_removal(
    ejected: Sequence[EventMatch], inserted: Sequence[EventMatch], context: str
) -> None:
    """The remove/add cycle must contain both events, eject first."""
    assert ejected, f"{context}: no ejected event was generated"
    assert inserted, f"{context}: no inserted event was generated"
    first_ejected = min(event_id for event_id, _ in ejected)
    first_inserted = min(event_id for event_id, _ in inserted)
    assert first_ejected < first_inserted, (
        f"{context}: inserted event {first_inserted} precedes ejected event {first_ejected}"
    )


def wait_for_insertion_cycle(  # noqa: PLR0913 - clock/sleep injection keeps polling tests deterministic
    read_component_events: Callable[[], Iterable[EventMatch]],
    *,
    ejected_description: str,
    inserted_description: str,
    context: str,
    timeout_seconds: float = CPO_EVENT_POLL_TIMEOUT_SECONDS,
    require_inserted: bool = True,
    **poll_overrides,
) -> tuple[list[EventMatch], list[EventMatch]]:
    """Poll the events table until a remove -> add cycle is visible and ordered.

    ``read_component_events`` returns (id, body) matches for one instance,
    e.g. a closure over ``Events.find_events(component_event_predicate(name),
    since_event_id)``. ``require_inserted=False`` stops at the ejected event -
    see ``wait_for_ejected_event``.
    """
    poll_overrides.setdefault("acceptable_exceptions", TRANSIENT_READ_ERRORS)
    wanted = "ejected and inserted events" if require_inserted else "ejected event"
    ejected, inserted = poll_until(
        lambda: split_insertion_cycle(read_component_events(), ejected_description, inserted_description),
        lambda cycle: bool(cycle[0]) and (bool(cycle[1]) or not require_inserted),
        timeout_seconds=timeout_seconds,
        description=f"{context}: {wanted}",
        **poll_overrides,
    )
    if require_inserted:
        assert_insertion_follows_removal(ejected, inserted, context)
    return ejected, inserted


def wait_for_ejected_event(read_component_events: Callable[[], Iterable[EventMatch]], **kwargs) -> list[EventMatch]:
    """Gate on the outage actually starting before verifying recovery.

    A reset CLI returns before status/events flip, so an immediate ``Inserted``
    read can be pre-reset state; the ejected event is the durable proof that
    the removal window opened.
    """
    ejected, _ = wait_for_insertion_cycle(read_component_events, require_inserted=False, **kwargs)
    return ejected


def parse_event_time(time_created: str) -> datetime:
    """Parse an events-table 'time-created' value, tolerating the tz suffix."""
    text = time_created.strip()
    head, _, tail = text.rpartition(" ")
    if head and tail.isalpha():
        text = head
    return datetime.strptime(text, EventConsts.TIME_CREATED_FORMAT)


def port_up_event_times(matches: Iterable[EventMatch]) -> dict[str, datetime]:
    """Earliest 'Interface operational state is up' event time per port."""
    up_times: dict[str, datetime] = {}
    for _, event in matches:
        if event.get(EventConsts.TEXT) != EventConsts.INTERFACE_UP_EVENT:
            continue
        port = event.get(EventConsts.RESOURCE)
        if not port:
            continue
        event_time = parse_event_time(event[EventConsts.TIME_CREATED])
        if port not in up_times or event_time < up_times[port]:
            up_times[port] = event_time
    return up_times


def wait_for_ports_up_events(
    read_port_events: Callable[[], Iterable[EventMatch]],
    port_names: Iterable[str],
    *,
    context: str,
    timeout_seconds: float = CPO_EVENT_POLL_TIMEOUT_SECONDS,
    **poll_overrides,
) -> dict[str, datetime]:
    """Poll the events table until every port has published an up event.

    Link-up is proven and timed through the per-port 'Interface operational
    state is up' events; their DUT-side timestamps feed the budget check.
    """
    expected = set(port_names)
    assert expected, "no ports supplied"
    poll_overrides.setdefault("acceptable_exceptions", TRANSIENT_READ_ERRORS)
    return poll_until(
        lambda: port_up_event_times(read_port_events()),
        lambda up_times: expected <= set(up_times),
        timeout_seconds=timeout_seconds,
        description=f"{context}: up events for ports {sorted(expected)}",
        **poll_overrides,
    )


def assert_link_up_within_budget(
    up_times: Mapping[str, datetime],
    start_time: datetime,
    budget_seconds: float | None,
    context: str,
) -> None:
    """Enforce the link-up budget on event-measured up times when one is set."""
    if budget_seconds is None:
        return
    late = {
        port: (up_time - start_time).total_seconds()
        for port, up_time in up_times.items()
        if (up_time - start_time).total_seconds() > budget_seconds
    }
    assert not late, f"{context}: ports exceeded the {budget_seconds}s link-up budget: {late}"


def build_syslog_patterns(name: str, templates: Iterable[str]) -> list[str]:
    """Expand the O-6 syslog regex templates for one cpoN / elsN instance."""
    return [template.format(name=name) for template in templates]


def verify_transition_syslog(
    log,
    engine,
    names: Iterable[str],
    start_time,
    templates: tuple[str, ...] | None = CPO_EVENT_SYSLOG_REGEX_TEMPLATES,
) -> bool:
    """Assert the remove/add syslog lines for each instance once O-6 pins them.

    Returns False without touching the DUT while the wording is undefined.
    ``log`` is a ``System().log``-shaped object.
    """
    names = list(names)
    if templates is None:
        logger.info(
            "CPO remove/add syslog wording is pending O-6; skipping log assertions for %s",
            names,
        )
        return False
    patterns = [pattern for name in names for pattern in build_syslog_patterns(name, templates)]
    log.verify_expected_logs_by_time(patterns, engine=engine, start_time=start_time)
    return True


def health_instances(health_payload: Mapping, component: str) -> Mapping[str, Mapping]:
    instances = health_payload.get(component, {}).get(HealthConsts.Component.INSTANCE)
    assert isinstance(instances, Mapping), f"system health output has no {component} instances: {health_payload!r}"
    return instances


def unhealthy_counts(health_payload: Mapping, component: str) -> dict[str, int]:
    """Snapshot per-instance unhealthy counters for later delta assertions."""
    return {
        name: int(data[HealthConsts.Component.UNHEALTHY_COUNT])
        for name, data in health_instances(health_payload, component).items()
    }


def assert_unhealthy_count_incremented(
    before: Mapping[str, int], after: Mapping[str, int], instance: str, context: str
) -> None:
    """The faulted instance's unhealthy counter must grow."""
    assert instance in after, f"{context}: {instance} missing from health output"
    assert after[instance] > before.get(instance, 0), (
        f"{context}: {instance} unhealthy count did not increment "
        f"(before={before.get(instance, 0)}, after={after[instance]})"
    )


def wait_for_healthy_instances(
    read_health: Callable[[], Mapping],
    targets: Iterable[tuple[str, str]],
    *,
    timeout_seconds: float,
    **poll_overrides,
) -> None:
    """Poll system health until every (component, instance) target is HEALTHY."""
    targets = tuple(targets)
    assert targets, "no health targets supplied"
    poll_overrides.setdefault("acceptable_exceptions", TRANSIENT_READ_ERRORS)

    def states(payload: Mapping) -> dict[tuple[str, str], object]:
        return {
            (component, instance): health_instances(payload, component)
            .get(instance, {})
            .get(HealthConsts.Component.STATE)
            for component, instance in targets
        }

    poll_until(
        lambda: states(read_health()),
        lambda current: all(state == HealthConsts.Component.HEALTHY for state in current.values()),
        timeout_seconds=timeout_seconds,
        description=f"health targets to be HEALTHY: {targets}",
        **poll_overrides,
    )
