"""Offline tests for the gNMI telemetry watch, fed canned gnmic --format json output."""

import itertools

import pytest

from ngts.ngts_types import DevicesT, EnginesT
from ngts.nvos_constants.constants_nvos import Cpov2Consts, NvosConst
from ngts.nvos_constants.telemetry_mapping import GNMI_ELS_PRESENT, GNMI_SUBCOMPONENTS, TelemetryLeaf
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.platform.Cpo import Cpo
from ngts.nvos_tools.platform.LaserSource import LaserSource
from ngts.tests_nvos.system.gnmi.helpers import (
    _extract_json_objects_from_gnmic_output,
    build_leaf_timeline,
    build_leaf_value_timeline,
)
from ngts.tests_nvos.system.telemetry import telemetry_watch
from ngts.tests_nvos.system.telemetry.telemetry_watch import (
    TelemetrySource,
    TelemetryWatch,
    assert_bounced,
    assert_unchanged,
    gnmi_instances,
    gnmi_watch,
    read_all,
    read_mismatches,
    telemetry_xpaths,
)

CPO1 = "components/component[name=cpo1]"
CPO2 = "components/component[name=cpo2]"
CPO1_OPER = f"{CPO1}/state/oper-status"
CPO2_OPER = f"{CPO2}/state/oper-status"

# the delete arrives after a newer update: ordering must follow timestamps, not arrival
GNMIC_STREAM_OUTPUT = f"""
2026/10/05 10:00:00.000 [gnmic] target "nvos": subscription default-1 rcv
{{
  "source": "10.0.0.1:9339",
  "subscription-name": "default-1",
  "timestamp": 1000,
  "prefix": "{CPO1}",
  "target": "nvos",
  "updates": [
    {{"Path": "state/oper-status", "values": {{"state/oper-status": "openconfig-platform-types:ACTIVE"}}}}
  ]
}}
{{
  "source": "10.0.0.1:9339",
  "timestamp": 1000,
  "prefix": "{CPO2}",
  "updates": [
    {{"Path": "state/oper-status", "values": {{"state/oper-status": "openconfig-platform-types:ACTIVE"}}}}
  ]
}}
{{
  "sync-response": true
}}
{{
  "timestamp": 3000,
  "prefix": "{CPO1}",
  "updates": [
    {{"Path": "state/oper-status", "values": {{"state/oper-status": "openconfig-platform-types:ACTIVE"}}}}
  ]
}}
{{
  "timestamp": 2000,
  "prefix": "{CPO1}",
  "deletes": ["state/oper-status"]
}}
"""


@pytest.fixture
def watch():
    notifications = _extract_json_objects_from_gnmic_output(GNMIC_STREAM_OUTPUT)
    return TelemetryWatch([CPO1_OPER, CPO2_OPER], baseline={}, timeline=build_leaf_value_timeline(notifications))


def test_value_timeline_orders_deliveries_and_records_deletes(watch):
    active = "openconfig-platform-types:ACTIVE"
    assert watch.timeline == {
        CPO1_OPER: [(1000, active), (2000, None), (3000, active)],
        CPO2_OPER: [(1000, active)],
    }


def test_timestamp_timeline_is_a_delete_free_projection():
    notifications = _extract_json_objects_from_gnmic_output(GNMIC_STREAM_OUTPUT)
    assert build_leaf_timeline(notifications) == {CPO1_OPER: [1000, 3000], CPO2_OPER: [1000]}


def test_bounce_through_delete_matches_unprefixed_identity(watch):
    assert_bounced(watch, f"{CPO1}/state/*", steady="ACTIVE")
    assert_unchanged(watch, CPO2_OPER)


def test_leaf_that_never_left_steady_did_not_bounce(watch):
    with pytest.raises(AssertionError, match="did not bounce"):
        assert_bounced(watch, CPO2_OPER, steady="ACTIVE")
    with pytest.raises(AssertionError, match="changed while"):
        assert_unchanged(watch, CPO1_OPER)


def test_leaf_that_left_its_baseline_before_the_first_delivery_changed(watch):
    watch.baseline[CPO2_OPER] = "openconfig-platform-types:INACTIVE"
    with pytest.raises(AssertionError, match="changed while"):
        assert_unchanged(watch, CPO2_OPER)


def test_bool_and_enum_spellings_compare_equal():
    spelled = TelemetryWatch([CPO1_OPER, CPO2_OPER], baseline={}, timeline={
        CPO1_OPER: [(1, "false"), (2, "true"), (3, False)],
        CPO2_OPER: [(1, "openconfig-platform-types:ACTIVE"), (2, "active")],
    })
    assert_bounced(spelled, CPO1_OPER, steady=False)
    assert_unchanged(spelled, CPO2_OPER)


def test_watched_leaf_that_never_streamed_fails(watch):
    watch.paths.append(f"{CPO2}/state/temperature/instant")
    with pytest.raises(AssertionError, match="never streamed"):
        assert_unchanged(watch, f"{CPO2}/state/temperature/*")


def test_glob_star_stays_inside_one_segment(watch):
    assert_unchanged(watch, "components/component[name=*2]/state/oper-status")
    with pytest.raises(AssertionError, match="no watched leaf matches"):
        assert_unchanged(watch, "components/*/oper-status")


def test_pinned_interface_description_xpath():
    interface = BaseComponent(path="/interface/sw1p1")
    assert telemetry_xpaths(interface) == {
        NvosConst.DESCRIPTION: "interfaces/interface[name=sw1p1]/state/description",
    }
    with pytest.raises(AssertionError, match="not pinned"):
        telemetry_xpaths(interface, ["mtu"])


@pytest.fixture
def channel_table(monkeypatch):
    monkeypatch.setattr(telemetry_watch, "NVUE_TO_TELEMETRY", {
        "/platform/cpo/{cpo}": {
            "status": TelemetryLeaf(gnmi=f"{CPO1}/state/oper-status"),
        },
        "/platform/cpo/{cpo}/channel/{channel}": {
            "status": TelemetryLeaf(gnmi="components/component[name={cpo}]/channel[index={channel}]/state/status"),
            "port": TelemetryLeaf(gnmi="interfaces/interface[name={ib_port}]/state/oper-status"),
        },
    })


def test_nvue_path_instance_keys_resolve_the_matching_row(channel_table):
    channel = BaseComponent(path="/platform/cpo/cpo1/channel/7")
    assert telemetry_xpaths(channel, ["status"]) == {
        "status": "components/component[name=cpo1]/channel[index=7]/state/status",
    }
    assert telemetry_xpaths(channel, ["port"], ib_port="Infiniband3") == {
        "port": "interfaces/interface[name=Infiniband3]/state/oper-status",
    }


def test_missing_runtime_key_names_the_placeholder_and_row(channel_table):
    channel = BaseComponent(path="/platform/cpo/cpo1/channel/7")
    with pytest.raises(AssertionError, match=r"channel/\{channel\} port needs runtime key 'ib_port'"):
        telemetry_xpaths(channel)


def test_unpinned_resource_is_rejected():
    with pytest.raises(AssertionError, match="no pinned telemetry mapping for /platform/fan/fan1"):
        telemetry_xpaths(BaseComponent(path="/platform/fan/fan1"))


def test_stream_failure_is_recorded_not_raised(monkeypatch):
    class FakeClient:
        def gnmic_subscribe_multi_path(self, **_):
            return "", "", 0, "process"

        def close_session_and_get_out_and_err(self, process):
            return "", "rpc error: code = Unavailable desc = connection refused"

    monkeypatch.setattr(telemetry_watch, "_gnmic_client", lambda engines, devices: FakeClient())
    monkeypatch.setattr(telemetry_watch, "gnmi_snapshot", lambda engines, devices, paths: {})
    monkeypatch.setattr(telemetry_watch, "_active_subscriptions", itertools.count().__next__)
    with gnmi_watch(EnginesT(), DevicesT(), [f"/{CPO1_OPER}"]) as failed_watch:
        pass
    assert failed_watch.paths == [CPO1_OPER]
    with pytest.raises(AssertionError, match="telemetry watch failed: rpc error"):
        assert_unchanged(failed_watch, CPO1_OPER)


ENGINES = EnginesT(dut=None)
PLATFORM = BaseComponent(path="/platform")
CPO = Cpo(PLATFORM).cpo_id["cpo1"]
ELS = LaserSource(PLATFORM).els_id["els2"]
INTERFACE = Interface(None, port_name="sw2p1")
CHANNEL_STATE = f"{CPO1}/cpo/physical-channels/channel[index=7]/state"
HOST_LANE_STATE = f"{CPO1}/cpo/host-lanes/lane[lane-number=7]/state"
ELS2 = "components/component[name=els2]"
LASER_STATE = f"{ELS2}/laser-source/lasers/laser[index=4]/state"
CPO_STATUS_DB = ("STATE_DB", "CPO_STATUS|cpo1")
CHANNELS_DB = ("STATE_DB", "CPO_CHANNELS_TELEMETRY|cpo1")
ELS_INFO_DB = ("STATE_DB", "CPO_ELS_INFO|els2")
ELS_TELEMETRY_DB = ("STATE_DB", "CPO_ELS_TELEMETRY|els2")

# NVUE (HLD rendering), gNMI (typed, prefixed identities), STATE_DB (xcvrd strings) for the same state
FOUR_WAY_CASES = {
    "cpo": (CPO, {}, {
        Cpov2Consts.STATUS: "up",
        Cpov2Consts.ERROR_STATUS: "N/A",
        Cpov2Consts.THRESHOLDS: {Cpov2Consts.CH_TX_POWER: {Cpov2Consts.LOW_WARNING: -6.0}},
    }, {
        f"{CPO1}/state/oper-status": "openconfig-platform-types:ACTIVE",
        f"{CPO1}/state/error-status": "none",
        f"{CPO1}/cpo/thresholds/threshold[severity=WARNING]/state/output-power-lower": "-6.00",
    }, {
        (*CPO_STATUS_DB, "status"): "1",
        (*CPO_STATUS_DB, "error_status"): "N/A",
        ("STATE_DB", "CPO_INFO|cpo1", "tx_power_low_warning"): "-6.0",
    }),
    "channel": (CPO.channel.channel_id["channel-7"], {}, {
        Cpov2Consts.CH_RX_POWER: {Cpov2Consts.POWER: -1.0, Cpov2Consts.ALARM_STATUS: "off"},
        Cpov2Consts.CH_RX_LOS: "False",
        Cpov2Consts.CH_TX_LOS: "False",
        Cpov2Consts.CH_DP_STATE: "Initialized",
    }, {
        f"{CHANNEL_STATE}/input-power/instant": -1.04,
        f"{CHANNEL_STATE}/input-power/alarm-status": False,
        f"{CHANNEL_STATE}/rx-los": False,
        f"{HOST_LANE_STATE}/tx-los": False,
        f"{HOST_LANE_STATE}/dp-state": "Initialized",
    }, {
        (*CHANNELS_DB, "lane7_rx_power"): "-0.97",
        (*CHANNELS_DB, "lane7_rx_los"): "False",
        (*CHANNELS_DB, "lane7_tx_los"): "False",
        (*CHANNELS_DB, "lane7_dp_st"): "7",
    }),
    "oe": (CPO.oe.oe_id["oe3"], {}, {Cpov2Consts.OE_TEMPERATURE: 36.8}, {
        "components/component[name=oe3]/state/temperature/instant": "37.1",
    }, {
        ("STATE_DB", "CPO_OE_TELEMETRY|oe3", "temperature"): "36.6",
    }),
    "els": (ELS, {}, {
        Cpov2Consts.STATUS: "Inserted",
        Cpov2Consts.PARENT: "cpo1",
        Cpov2Consts.ELS_VENDOR_SN: "MT2443FT01035",
        Cpov2Consts.TEMPERATURE: 28.0,
        Cpov2Consts.THRESHOLD: {Cpov2Consts.ALARM: {Cpov2Consts.TX_POWER_UPPER: 1.0}},
    }, {
        f"{ELS2}/state/oper-status": "openconfig-platform-types:ACTIVE",
        f"{ELS2}/state/parent": "cpo1",
        f"{ELS2}/state/serial-no": "MT2443FT01035",
        f"{ELS2}/state/temperature/instant": "28.3",
        f"{ELS2}/laser-source/thresholds/threshold[severity=CRITICAL]/state/optical-power-upper": "1.00",
    }, {
        ("STATE_DB", "CPO_ELS_STATUS|els2", "status"): "1",
        (*ELS_INFO_DB, "vmodule"): "cpo1",
        (*ELS_INFO_DB, "vendor_sn"): "MT2443FT01035",
        (*ELS_TELEMETRY_DB, "laser_source_temperature"): "28.0",
        (*ELS_INFO_DB, "laser_optical_power_hi_al_th"): "1.0",
    }),
    "laser": (ELS.laser.laser_id["laser-4"], {}, {
        Cpov2Consts.LASER_ENABLED: "laser_enabled",
        Cpov2Consts.LASER_OPER_STATUS: "laser_down_with_fault",
        Cpov2Consts.LASER_ERROR_STATUS: "laser_fiber_contaminated",
        Cpov2Consts.LASER_POWER_RESTRICTION: "laser_restriction_off",
        Cpov2Consts.LASER_BIAS_CURRENT: {Cpov2Consts.CURRENT: 12.2, Cpov2Consts.ALARM_STATUS: "on",
                                         Cpov2Consts.ALARM_SEVERITY: "warning",
                                         Cpov2Consts.THRESHOLD: {Cpov2Consts.HIGH_WARNING: 13.0}},
        Cpov2Consts.LASER_TX_POWER: {Cpov2Consts.ALARM_STATUS: "off", Cpov2Consts.ALARM_SEVERITY: "none"},
    }, {
        f"{LASER_STATE}/enabled": True,
        f"{LASER_STATE}/oper-status": "DOWN_WITH_FAULT",
        f"{LASER_STATE}/error-status": "LASER_FIBER_CONTAMINATED",
        f"{LASER_STATE}/power-restriction": "OFF",
        f"{LASER_STATE}/laser-bias-current/instant": "12.31",
        f"{LASER_STATE}/laser-bias-current/alarm-status": True,
        f"{LASER_STATE}/laser-bias-current/alarm-severity": "openconfig-alarm-types:WARNING",
        f"{LASER_STATE}/optical-power/alarm-status": False,
        f"{LASER_STATE}/optical-power/alarm-severity": "NONE",
        f"{ELS2}/laser-source/thresholds/threshold[severity=WARNING]/state/laser-bias-current-upper": "13.00",
    }, {
        (*ELS_TELEMETRY_DB, "laser_enabled3"): "1",
        (*ELS_TELEMETRY_DB, "els_oper_state_laser3"): "4",
        (*ELS_TELEMETRY_DB, "els_laser_state_fault3"): "1",
        (*ELS_TELEMETRY_DB, "laser_restriction3"): "1",
        (*ELS_TELEMETRY_DB, "bias_current_monitor3"): "12.2",
        (*ELS_TELEMETRY_DB, "laser_bias_hi_al3"): "0",
        (*ELS_TELEMETRY_DB, "laser_bias_lo_al3"): "0",
        (*ELS_TELEMETRY_DB, "laser_bias_hi_war3"): "1",
        (*ELS_TELEMETRY_DB, "laser_bias_lo_war3"): "0",
        **{(*ELS_TELEMETRY_DB, f"laser_opt_pwr_{bound}_{level}3"): "0"
           for bound in ("hi", "lo") for level in ("al", "war")},
        (*ELS_INFO_DB, "laser_bias_hi_war_th"): "13.0",
    }),
    "interface cpo": (INTERFACE.cpo, {}, {
        Cpov2Consts.PARENT: "cpo1",
        Cpov2Consts.CHANNEL: {"channel-18": {}, "channel-17": {}},
    }, {
        "interfaces/interface[name=sw2p1]/state/cpo-module": "cpo1",
        "interfaces/interface[name=sw2p1]/state/cpo-channels": [17, 18],
    }, {}),
    "interface link": (INTERFACE.link, {"ib_port": "Infiniband9"}, {IbInterfaceConsts.LINK_OPER_STATUS: "up"}, {
        "interfaces/interface[name=sw2p1]/state/oper-status": "openconfig-interfaces:UP",
    }, {
        ("STATE_DB", "IB_PORT_TABLE|Infiniband9", "oper_status"): "up",
    }),
    "unset description": (INTERFACE, {"ib_port": "Infiniband9"}, {}, {
        "interfaces/interface[name=sw2p1]/state/description": "",
    }, {
        ("CONFIG_DB", "IB_PORT|Infiniband9", "description"): "",
    }),
}


@pytest.fixture
def fake_sources(monkeypatch):
    def install(nvue_output: dict, gnmi_values: dict, db_values: dict) -> None:
        monkeypatch.setattr(telemetry_watch, "_show", lambda component, api: nvue_output)
        monkeypatch.setattr(telemetry_watch, "gnmi_snapshot",
                            lambda engines, devices, paths: {path: gnmi_values[path] for path in paths
                                                             if path in gnmi_values})
        monkeypatch.setattr(telemetry_watch.Tools.DatabaseTool, "sonic_db_cli_hget",
                            lambda engine, asic, db, key, field: db_values.get((db, key, field), ""))
    return install


@pytest.mark.parametrize("case", FOUR_WAY_CASES)
def test_every_component_kind_resolves_and_normalizes_to_four_way_parity(case, fake_sources):
    component, runtime_keys, nvue_output, gnmi_values, db_values = FOUR_WAY_CASES[case]
    fake_sources(nvue_output, gnmi_values, db_values)
    reads = read_all(ENGINES, DevicesT(), component, **runtime_keys)
    assert set(gnmi_values) <= set(telemetry_xpaths(component, **runtime_keys).values())
    assert not read_mismatches(component, reads)
    unread = [field for field, sources in reads.items()
              if any(sources.values()) and None in sources.values()]
    assert not unread, f"a source missed a populated field: {[reads[field] for field in unread]}"


def test_normalized_values_are_gnmi_typed(fake_sources):
    fake_sources(*FOUR_WAY_CASES["laser"][2:])
    reads = read_all(ENGINES, DevicesT(), ELS.laser.laser_id["laser-4"])
    bias, tx_power = Cpov2Consts.LASER_BIAS_CURRENT, Cpov2Consts.LASER_TX_POWER
    assert {field: sources[TelemetrySource.DB] for field, sources in reads.items()
            if sources.get(TelemetrySource.DB) is not None} == {
        Cpov2Consts.LASER_ENABLED: True,
        Cpov2Consts.LASER_OPER_STATUS: "DOWN_WITH_FAULT",
        Cpov2Consts.LASER_ERROR_STATUS: "LASER_FIBER_CONTAMINATED",
        Cpov2Consts.LASER_POWER_RESTRICTION: "OFF",
        f"{bias}/{Cpov2Consts.CURRENT}": 12.2,
        f"{bias}/{Cpov2Consts.ALARM_STATUS}": True,
        f"{bias}/{Cpov2Consts.ALARM_SEVERITY}": "WARNING",
        f"{tx_power}/{Cpov2Consts.ALARM_STATUS}": False,
        f"{tx_power}/{Cpov2Consts.ALARM_SEVERITY}": "NONE",
        f"{Cpov2Consts.LASER_BIAS_CURRENT}/{Cpov2Consts.THRESHOLD}/{Cpov2Consts.HIGH_WARNING}": 13.0,
    }
    fake_sources(*FOUR_WAY_CASES["interface cpo"][2:])
    assert read_all(ENGINES, DevicesT(), INTERFACE.cpo)[Cpov2Consts.CHANNEL][TelemetrySource.NVUE] == [17, 18]


def test_out_of_tolerance_and_unrenamed_values_are_mismatches(fake_sources):
    laser = ELS.laser.laser_id["laser-4"]
    nvue_output, gnmi_values, db_values = FOUR_WAY_CASES["laser"][2:]
    current = f"{Cpov2Consts.LASER_BIAS_CURRENT}/{Cpov2Consts.CURRENT}"
    fake_sources({**nvue_output, Cpov2Consts.LASER_ENABLED: "on"}, gnmi_values,
                 {**db_values, (*ELS_TELEMETRY_DB, "bias_current_monitor3"): "13.0"})
    reads = read_all(ENGINES, DevicesT(), laser)
    assert set(read_mismatches(laser, reads)) == {Cpov2Consts.LASER_ENABLED, current}


def test_cli_numbers_with_display_units_reach_parity(fake_sources):
    laser = ELS.laser.laser_id["laser-4"]
    nvue_output, gnmi_values, db_values = FOUR_WAY_CASES["laser"][2:]
    bias = {Cpov2Consts.CURRENT: "12.2 mA", Cpov2Consts.ALARM_STATUS: "on", Cpov2Consts.ALARM_SEVERITY: "warning",
            Cpov2Consts.THRESHOLD: {Cpov2Consts.HIGH_WARNING: "13.0mA"}}
    fake_sources({**nvue_output, Cpov2Consts.LASER_BIAS_CURRENT: bias}, gnmi_values, db_values)
    assert not read_mismatches(laser, read_all(ENGINES, DevicesT(), laser))


def test_expected_baseline_is_compared_per_source_within_tolerance(fake_sources):
    laser = ELS.laser.laser_id["laser-4"]
    fake_sources(*FOUR_WAY_CASES["laser"][2:])
    baseline = read_all(ENGINES, DevicesT(), laser)
    drifted = {field: dict(sources) for field, sources in baseline.items()}
    current = f"{Cpov2Consts.LASER_BIAS_CURRENT}/{Cpov2Consts.CURRENT}"
    drifted[current][TelemetrySource.DB] = 12.4
    assert not read_mismatches(laser, drifted, baseline)
    drifted[Cpov2Consts.LASER_OPER_STATUS][TelemetrySource.GNMI] = "ACTIVE"
    assert set(read_mismatches(laser, drifted, baseline)) == {Cpov2Consts.LASER_OPER_STATUS}


def test_db_template_without_its_instance_index_names_the_key():
    with pytest.raises(AssertionError, match="needs runtime key 'channel_index'"):
        read_all(ENGINES, DevicesT(), BaseComponent(path=f"/platform/cpo/cpo1/{Cpov2Consts.CHANNEL}/ch7"))


def test_fields_limit_the_read_and_gnmi_only_leaves_are_never_read(fake_sources):
    fake_sources(*FOUR_WAY_CASES["cpo"][2:])
    assert set(read_all(ENGINES, DevicesT(), CPO, [Cpov2Consts.STATUS])) == {Cpov2Consts.STATUS}
    assert GNMI_SUBCOMPONENTS in telemetry_xpaths(CPO)
    assert GNMI_SUBCOMPONENTS not in read_all(ENGINES, DevicesT(), CPO)
    assert telemetry_xpaths(ELS, [GNMI_ELS_PRESENT]) == {GNMI_ELS_PRESENT: f"{ELS2}/laser-source/state/present"}


def test_wildcard_reads_every_instance_keyed_and_normalized(monkeypatch):
    monkeypatch.setattr(telemetry_watch, "gnmi_snapshot", lambda engines, devices, paths: {
        f"{CHANNEL_STATE}/rx-los": "true",
        f"{CPO1}/cpo/physical-channels/channel[index=8]/state/rx-los": "false",
        f"{CHANNEL_STATE}/tx-failure": "true",
        f"{CPO1}/subcomponents/subcomponent[name=els1]/name": "els1",
        f"{CPO1}/subcomponents/subcomponent[name=els1]/state/name": "els1",
        f"{CPO1}/subcomponents/subcomponent[name=oe1]/state/name": "oe1",
        "interfaces/interface[name=sw2p1]/state/cpo-channels": ["18", "17"],
    })
    assert gnmi_instances(ENGINES, DevicesT(), CPO.channel.channel_id["*"], Cpov2Consts.CH_RX_LOS) == {
        "7": True, "8": False}
    assert set(gnmi_instances(ENGINES, DevicesT(), CPO, GNMI_SUBCOMPONENTS)) == {"els1", "oe1"}
    assert gnmi_instances(ENGINES, DevicesT(), Interface(None, port_name="*").cpo, Cpov2Consts.CHANNEL) == {
        "sw2p1": [17, 18]}


def test_config_leaf_without_gnmi_reads_and_compares_three_ways(fake_sources):
    fae_cpo = BaseComponent(path="/fae/system/cpo")
    dump_state_db = ("CONFIG_DB", "SYSTEM_CO_OPTICS|els-initialization", "cpo_dump_state")
    fake_sources({Cpov2Consts.CPO_DUMP_STATE: "disabled"}, {}, {dump_state_db: "disabled"})
    reads = read_all(ENGINES, DevicesT(), fae_cpo)
    assert reads == {Cpov2Consts.CPO_DUMP_STATE: dict.fromkeys(
        (TelemetrySource.NVUE, TelemetrySource.OPENAPI, TelemetrySource.DB), "disabled")}
    assert telemetry_xpaths(fae_cpo) == {}
    assert not read_mismatches(fae_cpo, reads)
    fake_sources({Cpov2Consts.CPO_DUMP_STATE: "disabled"}, {}, {dump_state_db: "enabled"})
    assert set(read_mismatches(fae_cpo, read_all(ENGINES, DevicesT(), fae_cpo))) == {Cpov2Consts.CPO_DUMP_STATE}
