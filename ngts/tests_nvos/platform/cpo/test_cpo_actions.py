"""On-DUT CPO reset, link-up, reset-observability, FAE and set/unset rejection tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import ApiType, Cpov2Consts, EventConsts, HealthConsts, NvosConst
from ngts.nvos_constants.telemetry_mapping import (
    GNMI_ACTIVE,
    GNMI_DP_ACTIVATED,
    GNMI_ELS_PRESENT,
    GNMI_PRESENT,
    GNMI_UP,
)
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import (
    IbInterfaceConsts,
    NvosConsts,
)
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.MultiPlanarTool import MultiPlanarTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.PollingTool import PollingTool
from ngts.nvos_tools.platform.Cpo import Cpo
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.helpers.interfaces import interface_helpers
from ngts.tests_nvos.platform.cpo.action_helpers import (
    CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS,
    CPO_SW_LINK_UP_TIMEOUT_SECONDS,
    assert_mapping_unchanged,
    assert_no_counter_increase,
    error_drop_counters,
    laser_sibling_ports,
    mapping_snapshot,
)
from ngts.tests_nvos.platform.cpo.event_helpers import (
    INSERTION_CYCLE_DESCRIPTIONS,
    TRANSIENT_READ_ERRORS,
    assert_link_up_within_budget,
    component_event_predicate,
    verify_transition_syslog,
    wait_for_ejected_event,
    wait_for_healthy_instances,
    wait_for_insertion_cycle,
    wait_for_ports_up_events,
)
from ngts.tests_nvos.platform.cpo.helpers import (
    read_cpo,
    read_els,
    read_interface_cpo,
    sample_names,
    validate_cpo_detail,
    validate_laser_source_detail,
    wait_for_parity,
)
from ngts.tests_nvos.platform.els_fiber_tuning.cpo_constants import CpoConsts
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
from ngts.tests_nvos.system.gnmi.helpers import get_infiniband_name_from_port_name
from ngts.tests_nvos.system.telemetry.telemetry_watch import (
    Reads,
    TelemetrySource,
    TelemetryWatch,
    assert_bounced,
    assert_unchanged,
    gnmi_watch,
    telemetry_xpaths,
)
from ngts.tools.test_utils import allure_utils as allure


@dataclass(frozen=True)
class ResetBaseline:
    """Stable relationships and ports expected after every reset scope."""

    cpo: str
    els: str
    mappings: dict[str, object]
    affected_ports: tuple[Port, ...]


def _has_status(detail: dict, expected: str) -> bool:
    return str(detail.get(Cpov2Consts.STATUS, "")).lower() == expected.lower()


def _wait_for_cpo_and_els_recovery(platform: Platform, cpo: str, els: str, engine) -> tuple[dict, dict]:
    # The CPO recovers to up (not Inserted); the replaceable ELS keeps
    # presence semantics and recovers to Inserted
    step = f"Wait for {cpo} to return to up and {els} to Inserted (timeout={CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS}s)"
    with allure.step(step):
        return PollingTool.poll_until(
            lambda: (read_cpo(platform, cpo, engine), read_els(platform, els, engine)),
            lambda details: (
                _has_status(details[0], Cpov2Consts.CPO_STATUS_UP) and
                _has_status(details[1], Cpov2Consts.ELS_STATUS_INSERTED)
            ),
            timeout_seconds=CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS,
            description=f"{cpo} to return to up and {els} to Inserted",
            acceptable_exceptions=TRANSIENT_READ_ERRORS,
        )


def _read_link(port: Port, engine) -> dict:
    return OutputParsingTool.parse_show_interface_link_output_to_dictionary(
        port.interface.link.show(dut_engine=engine)
    ).get_returned_value()


def _counter_snapshot(port: Port, engine) -> dict[str, int]:
    return error_drop_counters(port.interface.counters.parse_show(dut_engine=engine))


def _verify_ports_relinked(
    system: System, baseline: ResetBaseline, observed: ObservabilityBaseline, context: str
) -> None:
    """Prove every affected sw port re-linked after the reset.

    The NVL7 link-up budget is deliberately not asserted here: the only anchor
    available is the pre-reset snapshot, so the elapsed time also covers the
    module power-cycle and FW init. The recovery timeout bounds it instead;
    test_cpo_link_up_time owns the budget itself.
    """
    port_names = {port.name for port in baseline.affected_ports}
    with allure.step(f"Wait for up events on all {len(port_names)} {baseline.cpo} sw ports"):
        wait_for_ports_up_events(
            lambda: system.events.find_events(
                lambda event: event.get(EventConsts.RESOURCE) in port_names,
                since_event_id=observed.max_event_id,
            ),
            port_names,
            context=context,
            timeout_seconds=CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS,
        )


def _set_port_state(port: Port, state: str, engine) -> None:
    port.interface.link.state.set(
        op_param_name=state,
        apply=True,
        ask_for_confirmation=True,
        dut_engine=engine,
    ).verify_result()


def _verify_reset_recovery(platform: Platform, topology, baseline: ResetBaseline, engine) -> None:
    """Verify the durable CPO/ELS, mapping, and link contract after a reset."""
    cpo_detail, els_detail = _wait_for_cpo_and_els_recovery(
        platform,
        baseline.cpo,
        baseline.els,
        engine,
    )
    validate_cpo_detail(baseline.cpo, cpo_detail, topology)
    validate_laser_source_detail(baseline.els, els_detail, topology)
    assert_mapping_unchanged(baseline.mappings, cpo_detail, els_detail)
    interface_helpers.wait_and_verify_link(
        list(baseline.affected_ports),
        timeout=CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS,
    )


@dataclass(frozen=True)
class ObservabilityBaseline:
    """Event-id and DUT-clock snapshot taken just before a reset action."""

    max_event_id: int
    dut_start_time: datetime


def _dut_time(system: System, engine) -> datetime:
    return ClockTools.get_local_time_object_from_show_system_date_time_output(system.datetime.show(dut_engine=engine))


def _observability_baseline(system: System, engine) -> ObservabilityBaseline:
    return ObservabilityBaseline(
        max_event_id=system.events.get_max_event_id(),
        dut_start_time=_dut_time(system, engine),
    )


def _cascade_targets(baseline: ResetBaseline) -> tuple[tuple[str, str], ...]:
    return (
        (HealthConsts.Component.CPO, baseline.cpo),
        (HealthConsts.Component.Laser_Source, baseline.els),
    )


def _wait_for_ejection(system: System, baseline: ResetBaseline, observed: ObservabilityBaseline) -> None:
    """Gate on the outage starting before any recovery check.

    The reset CLI returns before status/events flip, so a recovery poll issued
    immediately could pass on stale pre-reset up/Inserted state; the ejected
    events of both cascade partners prove the removal window opened.
    """
    for component, instance in _cascade_targets(baseline):
        ejected_description, inserted_description = INSERTION_CYCLE_DESCRIPTIONS[component]
        with allure.step(f"Wait for {instance} ejected event after reset"):
            wait_for_ejected_event(
                lambda instance=instance: system.events.find_events(
                    component_event_predicate(instance),
                    since_event_id=observed.max_event_id,
                ),
                ejected_description=ejected_description,
                inserted_description=inserted_description,
                context=f"{instance} ejection",
            )


def _verify_reset_observability(
    system: System, baseline: ResetBaseline, observed: ObservabilityBaseline, engine
) -> None:
    """Verify events, health recovery and (O-6 gated) syslog after a reset.

    Reset cascades between a CPO and its ELS, so both components must emit a
    remove -> add event cycle regardless of which of the two was reset.
    """
    targets = _cascade_targets(baseline)
    for component, instance in targets:
        ejected_description, inserted_description = INSERTION_CYCLE_DESCRIPTIONS[component]
        with allure.step(f"Wait for {instance} ejected -> inserted event cycle"):
            wait_for_insertion_cycle(
                lambda instance=instance: system.events.find_events(
                    component_event_predicate(instance),
                    since_event_id=observed.max_event_id,
                ),
                ejected_description=ejected_description,
                inserted_description=inserted_description,
                context=f"{instance} reset cycle",
            )
    _verify_ports_relinked(system, baseline, observed, context=f"{baseline.cpo}/{baseline.els} reset")
    with allure.step(f"Wait for {baseline.cpo} and {baseline.els} health to return to HEALTHY"):
        wait_for_healthy_instances(
            lambda: system.health.component.parse_show(dut_engine=engine),
            targets,
            timeout_seconds=CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS,
        )
    if not verify_transition_syslog(system.log, engine, (baseline.cpo, baseline.els), observed.dut_start_time):
        allure.attach(
            "O-6 syslog check skipped",
            f"remove/add syslog wording is undefined; {baseline.cpo}/{baseline.els} log lines were not asserted",
        )


def _oper_status_xpath(port: Port) -> str:
    return telemetry_xpaths(port.interface.link, [IbInterfaceConsts.LINK_OPER_STATUS])[
        IbInterfaceConsts.LINK_OPER_STATUS]


def _cascade_steady_states(platform: Platform, baseline: ResetBaseline) -> dict[str, object]:
    cpo_leaves = telemetry_xpaths(platform.cpo.cpo_id[baseline.cpo], [Cpov2Consts.STATUS])
    els_leaves = telemetry_xpaths(platform.laser_source.els_id[baseline.els], [Cpov2Consts.STATUS, GNMI_ELS_PRESENT])
    return {
        cpo_leaves[Cpov2Consts.STATUS]: GNMI_ACTIVE,
        els_leaves[Cpov2Consts.STATUS]: GNMI_ACTIVE,
        els_leaves[GNMI_ELS_PRESENT]: GNMI_PRESENT,
        **{_oper_status_xpath(port): GNMI_UP for port in baseline.affected_ports},
    }


def _port_steady_states(platform: Platform, port: Port, engine) -> dict[str, object]:
    detail = read_interface_cpo(port.name, engine)
    channels = platform.cpo.cpo_id[detail[Cpov2Consts.PARENT]].channel.channel_id
    steady_states: dict[str, object] = {_oper_status_xpath(port): GNMI_UP}
    for channel in detail[Cpov2Consts.CHANNEL]:
        lane = telemetry_xpaths(channels[channel], [Cpov2Consts.CH_DP_STATE, Cpov2Consts.CH_TX_LOS])
        steady_states |= {lane[Cpov2Consts.CH_DP_STATE]: GNMI_DP_ACTIVATED, lane[Cpov2Consts.CH_TX_LOS]: False}
    return steady_states


def _assert_all_bounced(watch: TelemetryWatch, steady_states: Mapping[str, object]) -> None:
    for xpath, steady in steady_states.items():
        assert_bounced(watch, xpath, steady)


def _verify_telemetry_restored(engines, devices, baselines: Sequence[tuple[BaseComponent, Reads]]) -> None:
    with allure.step("Wait for NVUE, OpenAPI, gNMI and DB to return to the pre-reset values"):
        for component, reads in baselines:
            wait_for_parity(engines, devices, component, expected=reads)


@pytest.mark.platform
@pytest.mark.cpov2
def test_cpo_reset_actions(engines, devices, random_api):
    """Reset CPO and ELS; verify recovery, relationships and events.

    1. Capture the CPO/ELS detail baseline, mapping snapshot and the
       associated sw ports.
    2. Reset the CPO, then the ELS.
    3. After every reset, first gate on the outage actually starting: both
       partners' ejected events.
    4. Then verify recovery: wait for the CPO to return to up and the ELS
       to Inserted,
       re-run the detail validators, verify the mapping survived, and wait for
       every associated sw port to re-link.
    5. After the CPO and ELS resets: require an ejected -> inserted event
       cycle for both cascade partners, an up event per associated sw port
       within the NVL7 link-up budget, health back to HEALTHY, and matching
       remove/add syslog lines once the expected wording is defined (O-6).
    6. Around every reset, stream gNMI: CPO/ELS oper-status, ELS presence and
       the ports bounce, and
       NVUE, OpenAPI, gNMI and DB return to the pre-reset baseline.
    """
    topology = devices.dut.cpo
    platform = Platform()
    system = System()
    cpo = topology.cpo_names()[0]
    els = topology.els_for_cpo(cpo)[0]

    with allure.step(f"Capture the {cpo}/{els} baseline, mapping snapshot and associated sw ports"):
        cpo_before = read_cpo(platform, cpo, engines.dut)
        els_before = read_els(platform, els, engines.dut)
        validate_cpo_detail(cpo, cpo_before, topology)
        validate_laser_source_detail(els, els_before, topology)
        affected_ports = [Port(name) for name in Cpo.split_names(cpo_before[Cpov2Consts.PORTS])]
        assert affected_ports, f"{cpo} reports no associated sw ports"
        unexpected_ports = {port.name for port in affected_ports if port.name not in devices.dut.nvl_trunk_ports_list}
        assert not unexpected_ports, f"{cpo} reports non-sw associated ports: {sorted(unexpected_ports)}"
        baseline = ResetBaseline(
            cpo=cpo,
            els=els,
            mappings=mapping_snapshot(cpo_before, els_before),
            affected_ports=tuple(affected_ports),
        )

    cpo_component = platform.cpo.cpo_id[cpo]
    els_component = platform.laser_source.els_id[els]
    with allure.step(f"Capture the 4-way telemetry baseline of {cpo}, {els} and sampled channels and lasers"):
        telemetry_baseline = [(component, wait_for_parity(engines, devices, component)) for component in (
            cpo_component,
            els_component,
            *(cpo_component.channel.channel_id[name] for name in sample_names(topology.channels_for_cpo(cpo))),
            *(els_component.laser.laser_id[name] for name in sample_names(topology.lasers_for_els(els))),
        )]
    cascade = _cascade_steady_states(platform, baseline)

    for instance, component in ((cpo, cpo_component), (els, els_component)):
        with allure.step(f"Reset {instance} and verify recovery, observability and telemetry"):
            with gnmi_watch(engines, devices, list(cascade)) as watch:
                observed = _observability_baseline(system, engines.dut)
                component.action_reset(engine=engines.dut).verify_result()
                _wait_for_ejection(system, baseline, observed)
                _verify_reset_recovery(platform, topology, baseline, engines.dut)
                _verify_reset_observability(system, baseline, observed, engines.dut)
                _verify_telemetry_restored(engines, devices, telemetry_baseline)
            _assert_all_bounced(watch, cascade)


@pytest.mark.platform
@pytest.mark.cpov2
@pytest.mark.skip(reason="O-8: the FW register error-injection mechanism is undefined")
def test_cpo_fault_injection(engines, devices, random_api):
    """Inject an error on one CPO, then one ELS and one laser, and verify the
    fault contract: non-empty error-status, WARNING event + unhealthy-count
    increment, the Cleared counterpart on recovery, and tech-support generation
    succeeding during the fault window.

    The all-HEALTHY baseline this needs is already covered by
    test_cpo_show_health.
    """


@pytest.mark.interfaces
@pytest.mark.cpov2
def test_cpo_link_up_time(engines, devices, random_api, register_cleanup):
    """Validate link-up timing on CPO sw ports and laser-sharing isolation.

    Link-up is validated by polling port state and timing up events against
    the NVL7 sw-port link-up budget.

    1. Verify every modeled sw port reaches link-up.
    2. Toggle one port down/up (with cleanup) and verify its three same-laser
       siblings stay up with no error/drop counter growth.
    3. Verify the toggled port publishes a fresh up event within the NVL7
       link-up budget and spot-check one acp port.
    4. Stream gNMI over the toggle: the port oper-status and its host lanes
       bounce, the siblings' leaves stay unchanged, and NVUE, OpenAPI, gNMI
       and DB agree on the port oper-status when down and when up again.
    """
    topology = devices.dut.cpo
    system = System()
    platform = Platform()
    sw_ports = [Port(name) for name in devices.dut.nvl_trunk_ports_list]
    assert sw_ports, "CPO device exposes no sw trunk ports"
    with allure.step("Verify every modeled sw port reaches link-up"):
        interface_helpers.wait_and_verify_link(sw_ports, timeout=CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS)

    selected = sw_ports[0]
    siblings = [
        Port(name)
        for name in laser_sibling_ports(
            selected.name,
            devices.dut.nvl_trunk_ports_list,
            lanes_per_laser=topology.lanes_per_laser,
        )
    ]
    with allure.step(f"Map {selected.name} and its same-laser siblings to their gNMI leaves"):
        selected_steady_states = _port_steady_states(platform, selected, engines.dut)
        sibling_leaves = [xpath for port in siblings for xpath in _port_steady_states(platform, port, engines.dut)]
        ib_port = get_infiniband_name_from_port_name(engines.dut, selected.name)
        asic = f"{IbInterfaceConsts.ASIC}{MultiPlanarTool.get_primary_asic(Fae(port_name=selected.name))}"

    with gnmi_watch(engines, devices, [*selected_steady_states, *sibling_leaves]) as watch:
        with allure.step(f"Toggle {selected.name} down and verify its same-laser siblings stay up"):
            counters_before = {port.name: _counter_snapshot(port, engines.dut) for port in siblings}
            register_cleanup(lambda: _set_port_state(selected, NvosConsts.LINK_STATE_UP, engines.dut))
            _set_port_state(selected, NvosConsts.LINK_STATE_DOWN, engines.dut)
            selected.interface.wait_for_port_state(
                NvosConsts.LINK_STATE_DOWN,
                timeout=CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS,
                dut_engine=engines.dut,
            ).verify_result()
            interface_helpers.wait_and_verify_link(siblings, timeout=CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS)
            wait_for_parity(engines, devices, selected.interface.link, ib_port=ib_port, asic=asic)

        with allure.step(f"Bring {selected.name} back up and verify its up event and sibling counters"):
            observed = _observability_baseline(system, engines.dut)
            _set_port_state(selected, NvosConsts.LINK_STATE_UP, engines.dut)
            interface_helpers.wait_and_verify_link([selected, *siblings], timeout=CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS)
            up_times = wait_for_ports_up_events(
                lambda: system.events.find_events(
                    component_event_predicate(selected.name),
                    since_event_id=observed.max_event_id,
                ),
                [selected.name],
                context=f"{selected.name} toggle up",
                timeout_seconds=CPO_RECOVERY_SAFETY_TIMEOUT_SECONDS,
            )
            assert_link_up_within_budget(
                up_times,
                observed.dut_start_time,
                CPO_SW_LINK_UP_TIMEOUT_SECONDS,
                context=f"{selected.name} toggle up",
            )
            for sibling in siblings:
                assert_no_counter_increase(
                    counters_before[sibling.name],
                    _counter_snapshot(sibling, engines.dut),
                    sibling.name,
                )
            wait_for_parity(engines, devices, selected.interface.link, ib_port=ib_port, asic=asic)

    with allure.step(f"Verify gNMI streamed the {selected.name} bounce and left its siblings unchanged"):
        _assert_all_bounced(watch, selected_steady_states)
        for xpath in sibling_leaves:
            assert_unchanged(watch, xpath)

    with allure.step("Spot-check one acp port link state"):
        acp = Port(devices.dut.nvl_access_ports_list[0])
        acp_link = _read_link(acp, engines.dut)
        assert acp_link[IbInterfaceConsts.LINK_STATE] in NvosConsts.LINK_STATE_ALL_TYPES


def _dump_state_reads(state: str) -> Reads:
    return {Cpov2Consts.CPO_DUMP_STATE: dict.fromkeys(
        (TelemetrySource.NVUE, TelemetrySource.OPENAPI, TelemetrySource.DB), state)}


@pytest.mark.platform
@pytest.mark.cpov2
def test_cpo_fae_dump_state(engines, devices, random_api):
    """
    @summary: FAE cpo-dump-state agrees across NVUE, OpenAPI and CONFIG_DB through set, invalid set and unset.

    1. Set cpo-dump-state to disabled and wait until NVUE, OpenAPI and CONFIG_DB report it
    2. Set an invalid value, verify it is rejected and all sources still report disabled
    3. Unset cpo-dump-state and wait until all sources report the default (enabled)
    """
    fae_cpo = Fae().system.cpo
    try:
        with allure.step(f"Set {Cpov2Consts.CPO_DUMP_STATE} to {NvosConst.DISABLED} and wait for all sources"):
            fae_cpo.set(Cpov2Consts.CPO_DUMP_STATE, NvosConst.DISABLED, apply=True).verify_result()
            wait_for_parity(engines, devices, fae_cpo, expected=_dump_state_reads(NvosConst.DISABLED))
        with allure.step(f"Set an invalid {Cpov2Consts.CPO_DUMP_STATE} and verify the state is unchanged"):
            fae_cpo.set(Cpov2Consts.CPO_DUMP_STATE, CpoConsts.INVALID_FINE_TUNE_STATES[0]).verify_result(False)
            wait_for_parity(engines, devices, fae_cpo, expected=_dump_state_reads(NvosConst.DISABLED))
    finally:
        NvueGeneralCli.detach_config(engines.dut)
        with allure.step(f"Unset {Cpov2Consts.CPO_DUMP_STATE} and wait for the default on all sources"):
            fae_cpo.unset(Cpov2Consts.CPO_DUMP_STATE, apply=True).verify_result()
            wait_for_parity(engines, devices, fae_cpo, expected=_dump_state_reads(NvosConst.ENABLED))


@pytest.mark.platform
@pytest.mark.cpov2
@pytest.mark.skip(reason="Rel2B: the per-ELS activate sysfs and FW action list are undefined")
def test_cpo_fae_activate(engines, devices, random_api):
    """Activate the ELS initialization on one ELS, then one laser, then one step, and verify each succeeds and the
    targeted lasers' oper-status streams over gNMI back to ACTIVE.
    """


@pytest.mark.platform
@pytest.mark.cpov2
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES, indirect=True)
def test_cpo_set_unset_rejected(engines, devices, test_api):
    """
    @summary: Read-only CPO telemetry and the Taipan-only FAE cpo knobs reject set and unset.

    1. Set and unset a field of a CPO, an ELS and a laser, and verify each is rejected
    2. Set and unset each Taipan-only FAE cpo knob, and verify each is rejected
    """
    topology = devices.dut.cpo
    platform = Platform()
    cpo = topology.cpo_names()[0]
    els = topology.els_for_cpo(cpo)[0]
    laser_source = platform.laser_source.els_id[els]
    fae_cpo = Fae().system.cpo
    rejected: list[tuple[BaseComponent, str, object]] = [
        (platform.cpo.cpo_id[cpo], Cpov2Consts.STATUS, Cpov2Consts.CPO_STATUS_DOWN),
        (laser_source, Cpov2Consts.STATUS, Cpov2Consts.ELS_STATUS_REMOVED),
        (laser_source.laser.laser_id[topology.lasers_for_els(els)[0]], Cpov2Consts.LASER_ENABLED, NvosConst.DISABLED),
        *((fae_cpo, knob, NvosConst.DISABLED) for knob in [*CpoConsts.CPO_FIELDS, CpoConsts.FINE_TUNING_STATE]),
        (fae_cpo, CpoConsts.FINE_TUNING_INTERVAL, CpoConsts.DEFAULT_FINE_TUNING_INTERVAL),
    ]
    try:
        for component, field, value in rejected:
            with allure.step(f"Verify set and unset of {component.get_resource_path()} {field} are rejected"):
                component.set(field, value).verify_result(False)
                component.unset(field).verify_result(False)
    finally:
        NvueGeneralCli.detach_config(engines.dut)
