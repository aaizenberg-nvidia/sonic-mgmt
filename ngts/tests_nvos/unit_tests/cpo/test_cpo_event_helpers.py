"""Offline tests for CPO health registration, event and syslog helpers.

Proves without a DUT that the guarded health registration covers every Gen2
topology profile and stays a no-op on non-CPO devices, and that the event,
syslog and health-transition helpers enforce the remove -> add contract.
"""

import re
from datetime import datetime
from types import SimpleNamespace

import pytest

from ngts.nvos_constants.constants_nvos import Cpov2Consts, EventConsts, HealthConsts
from ngts.nvos_tools.Devices.cpo.CpoTopology import CpoTopology
from ngts.tests_nvos.platform.cpo.event_helpers import (
    INSERTION_CYCLE_DESCRIPTIONS,
    assert_insertion_follows_removal,
    assert_link_up_within_budget,
    assert_unhealthy_count_incremented,
    build_syslog_patterns,
    component_event_predicate,
    parse_event_time,
    port_up_event_times,
    split_insertion_cycle,
    unhealthy_counts,
    verify_transition_syslog,
    wait_for_ejected_event,
    wait_for_healthy_instances,
    wait_for_insertion_cycle,
    wait_for_ports_up_events,
)
from ngts.tests_nvos.system.test_system_health import (
    MULTI_INSTANCE_COMPONENTS,
    _build_validation_config,
)
from ngts.tests_nvos.unit_tests.cpo import sample_outputs as samples

# POR profiles: 1-ASIC developer, 2-ASIC simx, 4-ASIC de-populated, 8-ASIC full.
ALL_PROFILE_ASIC_COUNTS = (1, 2, 4, 8)


def _numeric_events(payload: dict) -> list[tuple[int, dict]]:
    """Mirror Events.find_events: numeric ids only, table metadata skipped."""
    return sorted((int(key), event) for key, event in payload.items() if str(key).isdigit() and isinstance(event, dict))


def _cycle_events(instance: str) -> list[tuple[int, dict]]:
    return [
        (event_id, event)
        for event_id, event in _numeric_events(samples.SHOW_SYSTEM_EVENTS_CPO_RESET)
        if event[EventConsts.RESOURCE] == instance
    ]


@pytest.mark.parametrize("asic_count", ALL_PROFILE_ASIC_COUNTS)
def test_health_instance_regexes_cover_all_profiles(asic_count):
    topology = CpoTopology(cpo_count=asic_count)
    for cpo in topology.cpo_names():
        assert re.match(Cpov2Consts.CPO_INSTANCE_REGEX, cpo), cpo
        assert not re.match(Cpov2Consts.ELS_INSTANCE_REGEX, cpo), cpo
    for els in topology.els_names():
        assert re.match(Cpov2Consts.ELS_INSTANCE_REGEX, els), els
        assert not re.match(Cpov2Consts.CPO_INSTANCE_REGEX, els), els


@pytest.mark.parametrize("bad_name", ["cpo1-stale", "cpo", "CPO1", "els1 ", "xcpo1"])
def test_health_instance_regexes_reject_invalid_names(bad_name):
    assert not re.match(Cpov2Consts.CPO_INSTANCE_REGEX, bad_name)
    assert not re.match(Cpov2Consts.ELS_INSTANCE_REGEX, bad_name)


def test_multi_instance_registration_uses_gen2_attributes():
    assert MULTI_INSTANCE_COMPONENTS[HealthConsts.Component.CPO] == (
        Cpov2Consts.CPO_INSTANCE_REGEX,
        "cpo_list",
    )
    assert MULTI_INSTANCE_COMPONENTS[HealthConsts.Component.Laser_Source] == (
        Cpov2Consts.ELS_INSTANCE_REGEX,
        "laser_source_list",
    )


def test_health_registration_guard_is_noop_on_non_cpo_device():
    """A device without cpo_list/laser_source_list must not gain CPO validation,
    even when the (buggy) DUT reports the components in its health output."""
    non_cpo_dut = SimpleNamespace(asic_amount=1)
    config = _build_validation_config(
        available_components=[
            HealthConsts.Component.ASIC,
            HealthConsts.Component.CPO,
            HealthConsts.Component.Laser_Source,
        ],
        devices=SimpleNamespace(dut=non_cpo_dut),
    )
    assert HealthConsts.Component.CPO not in config
    assert HealthConsts.Component.Laser_Source not in config
    assert HealthConsts.Component.ASIC in config


def test_health_registration_engages_on_cpo_device():
    topology = CpoTopology(cpo_count=2)
    cpo_dut = SimpleNamespace(
        cpo_list=list(topology.cpo_names()),
        laser_source_list=list(topology.els_names()),
    )
    config = _build_validation_config(
        available_components=[
            HealthConsts.Component.CPO,
            HealthConsts.Component.Laser_Source,
        ],
        devices=SimpleNamespace(dut=cpo_dut),
    )
    assert config[HealthConsts.Component.CPO] == (
        Cpov2Consts.CPO_INSTANCE_REGEX,
        "cpo_list",
    )
    assert config[HealthConsts.Component.Laser_Source] == (
        Cpov2Consts.ELS_INSTANCE_REGEX,
        "laser_source_list",
    )


def test_component_event_predicate_matches_exact_instance_only():
    predicate = component_event_predicate("cpo1")
    matched = [event for _, event in _numeric_events(samples.SHOW_SYSTEM_EVENTS_CPO_RESET) if predicate(event)]
    assert [event[EventConsts.TEXT] for event in matched] == [
        Cpov2Consts.CPO_EJECTED_EVENT,
        Cpov2Consts.CPO_INSERTED_EVENT,
    ]
    assert not predicate({EventConsts.RESOURCE: "cpo10", EventConsts.TEXT: "CPO was inserted"})
    assert not predicate({EventConsts.TEXT: "CPO was inserted"})


@pytest.mark.parametrize(
    ("component", "instance"),
    [
        (HealthConsts.Component.CPO, "cpo1"),
        (HealthConsts.Component.Laser_Source, "els1"),
    ],
)
def test_insertion_cycle_split_and_ordering(component, instance):
    ejected_description, inserted_description = INSERTION_CYCLE_DESCRIPTIONS[component]
    ejected, inserted = split_insertion_cycle(_cycle_events(instance), ejected_description, inserted_description)
    assert len(ejected) == len(inserted) == 1
    assert_insertion_follows_removal(ejected, inserted, f"{instance} reset")


def test_insertion_cycle_rejects_wrong_order_and_missing_events():
    ejected = [(4, {EventConsts.TEXT: Cpov2Consts.CPO_EJECTED_EVENT})]
    inserted = [(2, {EventConsts.TEXT: Cpov2Consts.CPO_INSERTED_EVENT})]
    with pytest.raises(AssertionError, match="precedes"):
        assert_insertion_follows_removal(ejected, inserted, "cpo1 reset")
    with pytest.raises(AssertionError, match="no ejected event"):
        assert_insertion_follows_removal([], inserted, "cpo1 reset")
    with pytest.raises(AssertionError, match="no inserted event"):
        assert_insertion_follows_removal(ejected, [], "cpo1 reset")


def test_wait_for_insertion_cycle_polls_until_insert_appears():
    ejected_only = _cycle_events("cpo1")[:1]
    reads = iter([ejected_only, _cycle_events("cpo1")])
    now = iter([0.0, 0.0, 1.0])
    sleeps = []
    ejected, inserted = wait_for_insertion_cycle(
        lambda: next(reads),
        ejected_description=Cpov2Consts.CPO_EJECTED_EVENT,
        inserted_description=Cpov2Consts.CPO_INSERTED_EVENT,
        context="cpo1 reset",
        timeout_seconds=10,
        interval_seconds=1,
        clock=lambda: next(now),
        sleep=sleeps.append,
    )
    assert sleeps == [1]
    assert [event_id for event_id, _ in ejected] == [2]
    assert [event_id for event_id, _ in inserted] == [4]


def test_wait_for_insertion_cycle_times_out_without_insert_event():
    ejected_only = _cycle_events("els1")[:1]
    now = iter([0.0, 0.0, 99.0])
    with pytest.raises(AssertionError, match="ejected and inserted events"):
        wait_for_insertion_cycle(
            lambda: ejected_only,
            ejected_description=Cpov2Consts.LASER_SOURCE_EJECTED_EVENT,
            inserted_description=Cpov2Consts.LASER_SOURCE_INSERTED_EVENT,
            context="els1 reset",
            timeout_seconds=10,
            clock=lambda: next(now),
            sleep=lambda _: None,
        )


def test_wait_for_ejected_event_gates_on_outage_start():
    reads = iter([[], _cycle_events("cpo1")[:1]])
    now = iter([0.0, 0.0, 1.0])
    sleeps = []
    ejected = wait_for_ejected_event(
        lambda: next(reads),
        ejected_description=Cpov2Consts.CPO_EJECTED_EVENT,
        inserted_description=Cpov2Consts.CPO_INSERTED_EVENT,
        context="cpo1 reset",
        timeout_seconds=10,
        interval_seconds=1,
        clock=lambda: next(now),
        sleep=sleeps.append,
    )
    assert sleeps == [1]
    assert [event_id for event_id, _ in ejected] == [2]


def test_wait_for_ejected_event_tolerates_transient_read_failure():
    reads = iter([AssertionError("cli busy mid-reset"), _cycle_events("cpo1")[:1]])

    def read_events():
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    now = iter([0.0, 0.0, 1.0])
    ejected = wait_for_ejected_event(
        read_events,
        ejected_description=Cpov2Consts.CPO_EJECTED_EVENT,
        inserted_description=Cpov2Consts.CPO_INSERTED_EVENT,
        context="cpo1 reset",
        timeout_seconds=10,
        interval_seconds=1,
        clock=lambda: next(now),
        sleep=lambda _: None,
    )
    assert [event_id for event_id, _ in ejected] == [2]


def test_wait_for_ejected_event_times_out_without_eject():
    now = iter([0.0, 0.0, 99.0])
    with pytest.raises(AssertionError, match="ejected event"):
        wait_for_ejected_event(
            lambda: [],
            ejected_description=Cpov2Consts.CPO_EJECTED_EVENT,
            inserted_description=Cpov2Consts.CPO_INSERTED_EVENT,
            context="cpo1 reset",
            timeout_seconds=10,
            clock=lambda: next(now),
            sleep=lambda _: None,
        )


def test_parse_event_time_handles_tz_suffix():
    assert parse_event_time("2026-07-21 09:03:13 IDT") == datetime(2026, 7, 21, 9, 3, 13)
    assert parse_event_time("2026-07-19 10:00:01") == datetime(2026, 7, 19, 10, 0, 1)


def test_port_up_event_times_earliest_per_port():
    up_times = port_up_event_times(_numeric_events(samples.SHOW_SYSTEM_EVENTS_PORT_UP))
    assert up_times == {
        "acp158": datetime(2026, 7, 21, 9, 3, 13),  # earliest of two up events
        "acp85": datetime(2026, 7, 21, 9, 3, 13),
    }
    # non-up events (e.g. the CPO eject/insert cycle) contribute nothing
    assert port_up_event_times(_numeric_events(samples.SHOW_SYSTEM_EVENTS_CPO_RESET)) == {}


def test_wait_for_ports_up_events_polls_until_all_ports_published():
    all_matches = _numeric_events(samples.SHOW_SYSTEM_EVENTS_PORT_UP)
    reads = iter([all_matches[:1], all_matches])
    now = iter([0.0, 0.0, 1.0])
    sleeps = []
    up_times = wait_for_ports_up_events(
        lambda: next(reads),
        ["acp158", "acp85"],
        context="cpo1 reset",
        timeout_seconds=10,
        interval_seconds=1,
        clock=lambda: next(now),
        sleep=sleeps.append,
    )
    assert sleeps == [1]
    assert set(up_times) == {"acp158", "acp85"}


def test_wait_for_ports_up_events_times_out_on_missing_port():
    now = iter([0.0, 0.0, 99.0])
    with pytest.raises(AssertionError, match="up events for ports"):
        wait_for_ports_up_events(
            lambda: _numeric_events(samples.SHOW_SYSTEM_EVENTS_PORT_UP),
            ["acp158", "acp999"],
            context="cpo1 reset",
            timeout_seconds=10,
            clock=lambda: next(now),
            sleep=lambda _: None,
        )


def test_link_up_budget_enforced_on_event_times():
    start = datetime(2026, 7, 21, 9, 3, 0)
    up_times = {"acp158": datetime(2026, 7, 21, 9, 3, 13)}
    assert_link_up_within_budget(up_times, start, None, "cpo1 reset")
    assert_link_up_within_budget(up_times, start, 20, "cpo1 reset")
    with pytest.raises(AssertionError, match="link-up budget"):
        assert_link_up_within_budget(up_times, start, 10, "cpo1 reset")


def test_fault_event_regexes_match_hld_wording():
    texts = {
        event[EventConsts.RESOURCE]: event[EventConsts.TEXT]
        for event in samples.SHOW_SYSTEM_EVENTS_CPO_FAULTS.values()
        if event[EventConsts.SEVERITY] == "WARNING"
    }
    assert re.search(Cpov2Consts.HW_NOT_OK_EVENT_REGEX, texts["cpo1"])
    assert re.search(Cpov2Consts.LASER_STATE_NOT_OK_EVENT_REGEX, texts["els1"])
    cleared = [
        event[EventConsts.TEXT]
        for event in samples.SHOW_SYSTEM_EVENTS_CPO_FAULTS.values()
        if event[EventConsts.SEVERITY] != "WARNING"
    ]
    for text, original in zip(sorted(cleared), sorted(texts.values()), strict=True):
        assert text == f"{Cpov2Consts.CLEARED_EVENT_PREFIX}{original}"


class _LogStub:
    def __init__(self):
        self.calls = []

    def verify_expected_logs_by_time(self, logs_to_find, engine=None, start_time=None):
        self.calls.append((logs_to_find, engine, start_time))


def test_syslog_gate_is_noop_until_o6_is_answered():
    log = _LogStub()
    assert verify_transition_syslog(log, engine=object(), names=["cpo1"], start_time=None) is False
    assert log.calls == []


def test_syslog_gate_expands_templates_per_instance():
    log = _LogStub()
    start_time = datetime(2026, 7, 19, 10, 0, 0)
    templates = (r"xcvrd.*{name}.*removed", r"xcvrd.*{name}.*inserted")
    assert (
        verify_transition_syslog(
            log, engine="engine", names=["cpo1", "els1"], start_time=start_time, templates=templates
        )
        is True
    )
    ((patterns, engine, passed_time),) = log.calls
    assert engine == "engine"
    assert passed_time is start_time
    assert patterns == build_syslog_patterns("cpo1", templates) + build_syslog_patterns("els1", templates)
    assert r"xcvrd.*els1.*inserted" in patterns


def test_unhealthy_count_snapshot_and_increment_contract():
    payload = samples.SHOW_SYSTEM_HEALTH_COMPONENT_CPO
    before = unhealthy_counts(payload, HealthConsts.Component.CPO)
    assert before == {cpo: 0 for cpo in samples.TOPOLOGY.cpo_names()}
    after = dict(before, cpo1=1)
    assert_unhealthy_count_incremented(before, after, "cpo1", "cpo1 fault")
    with pytest.raises(AssertionError, match="did not increment"):
        assert_unhealthy_count_incremented(before, dict(before), "cpo1", "cpo1 fault")
    with pytest.raises(AssertionError, match="missing from health output"):
        assert_unhealthy_count_incremented(before, {}, "cpo1", "cpo1 fault")


def test_wait_for_healthy_instances_recovers_after_unhealthy_read():
    healthy = samples.SHOW_SYSTEM_HEALTH_COMPONENT_CPO
    unhealthy = {
        HealthConsts.Component.CPO: {
            HealthConsts.Component.INSTANCE: {"cpo1": {HealthConsts.Component.STATE: HealthConsts.Component.UNHEALTHY}}
        },
        HealthConsts.Component.Laser_Source: healthy[HealthConsts.Component.Laser_Source],
    }
    reads = iter([unhealthy, healthy])
    now = iter([0.0, 0.0, 1.0])
    sleeps = []
    wait_for_healthy_instances(
        lambda: next(reads),
        [
            (HealthConsts.Component.CPO, "cpo1"),
            (HealthConsts.Component.Laser_Source, "els1"),
        ],
        timeout_seconds=10,
        interval_seconds=1,
        clock=lambda: next(now),
        sleep=sleeps.append,
    )
    assert sleeps == [1]


def test_wait_for_healthy_instances_times_out_on_missing_instance():
    now = iter([0.0, 0.0, 99.0])
    with pytest.raises(AssertionError, match="HEALTHY"):
        wait_for_healthy_instances(
            lambda: samples.SHOW_SYSTEM_HEALTH_COMPONENT_CPO,
            [(HealthConsts.Component.CPO, "cpo99")],
            timeout_seconds=10,
            clock=lambda: next(now),
            sleep=lambda _: None,
        )
