"""Pinned from the HLD and architecture docs, never read from the DUT: the DUT must not be its own oracle."""

from enum import IntEnum, StrEnum
from typing import NamedTuple

from ngts.nvos_constants.constants_nvos import Cpov2Consts, NvosConst
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts

ROUNDING_TOLERANCE = 0.01


class AlarmFlags(NamedTuple):
    high_alarm: str
    low_alarm: str
    high_warning: str
    low_warning: str


class TelemetryLeaf(NamedTuple):
    gnmi: str | None
    db: tuple[str, str, str | AlarmFlags] | None = None
    kind: str = "str"
    tolerance: float = ROUNDING_TOLERANCE
    cli: bool = True


LIVE_POWER_DBM_TOLERANCE = 0.5
LIVE_TEMPERATURE_C_TOLERANCE = 1.0
LIVE_ANALOG_TOLERANCE = 0.5

GNMI_NO_ERROR = "NONE"
GNMI_ACTIVE = "ACTIVE"
GNMI_UP = "UP"
GNMI_PRESENT = "PRESENT"
GNMI_DP_ACTIVATED = "ACTIVATED"
GNMI_SUBCOMPONENTS = "subcomponents"
GNMI_CHANNEL_INTERFACE = "mapping/interface"
GNMI_CHANNEL_INDEX = "index"
GNMI_ELS_PRESENT = "present"


class ElsStatusCode(StrEnum):
    REMOVED = "0"
    INSERTED = "1"


class PmpeStatusCode(StrEnum):
    ENABLED = "1"
    UNPLUGGED = "2"
    PLUGGED_WITH_ERROR = "3"
    DISABLED = "4"


class CmisDataPathState(IntEnum):
    DEACTIVATED = 1
    INIT = 2
    DEINIT = 3
    ACTIVATED = 4
    TXTURNON = 5
    TXTURNOFF = 6
    INITIALIZED = 7


class LaserOperState(IntEnum):
    INIT = 0
    ACTIVE = 1
    ACTIVE_WITH_FAULT = 2
    DOWN = 3
    DOWN_WITH_FAULT = 4


GNMI_ENUM_RENAMES_BY_KIND: dict[str, dict[str, str | bool]] = {
    "enum": {},
    "component_oper_status": {ElsStatusCode.REMOVED: "INACTIVE", ElsStatusCode.INSERTED: "ACTIVE",
                              PmpeStatusCode.ENABLED: "ACTIVE", PmpeStatusCode.UNPLUGGED: "INACTIVE",
                              PmpeStatusCode.PLUGGED_WITH_ERROR: "INACTIVE", PmpeStatusCode.DISABLED: "DISABLED",
                              "up": "ACTIVE", "down": "INACTIVE", "inserted": "ACTIVE", "removed": "INACTIVE"},
    "error_status": {"n/a": "NONE"},
    "alarm_severity": {"alarm": "CRITICAL"},
    "dp_state": {str(state.value): state.name for state in CmisDataPathState},
    "laser_oper_status": {str(state.value): state.name for state in LaserOperState} |
    {f"laser_{state.name.lower()}": state.name for state in LaserOperState},
    "laser_error_status": {str(code): name for code, name in enumerate([
        "NO_FAULT", "LASER_FIBER_CONTAMINATED", "LASER_APC_FAULT", "LASER_POWER_EXCEEDED_ALLOWED_RANGE",
        "LASER_POWER_SUBCEEDED_ALLOWED_RANGE", "LASER_TEC_CONTROL_LOOP_FAULT", "LASER_RAMPING_TIMEOUT_FAULT",
        "LASER_POWER_TUNING_FAULT", "LASER_WAVELENGTH_TUNING_FAULT", "LASER_HIGH_PATH_LOSS_FAULT",
        "ELS_INPUT_POWER_DROP_FAULT"])},
    "laser_enabled": {"1": True, "0": False, "true": True, "false": False,
                      "laser_enabled": True, "laser_disabled": False},
    "power_restriction": {"0": "ON", "1": "OFF", "laser_restriction_on": "ON", "laser_restriction_off": "OFF"},
}

_CPO = "components/component[name={cpo}]"
_CHANNEL_ENTRY = f"{_CPO}/cpo/physical-channels/channel[index={{channel_index}}]"
_CHANNEL = f"{_CHANNEL_ENTRY}/state"
_HOST_LANE = f"{_CPO}/cpo/host-lanes/lane[lane-number={{channel_index}}]/state"
_ELS = "components/component[name={els}]"
_LASER = f"{_ELS}/laser-source/lasers/laser[index={{laser_index}}]/state"
_INTERFACE = "interfaces/interface[name={port}]/state"

_CPO_STATUS = ("STATE_DB", "CPO_STATUS|{cpo}")
_CPO_INFO = ("STATE_DB", "CPO_INFO|{cpo}")
_CPO_CHANNELS = ("STATE_DB", "CPO_CHANNELS_TELEMETRY|{cpo}")
_ELS_STATUS = ("STATE_DB", "CPO_ELS_STATUS|{els}")
_ELS_INFO = ("STATE_DB", "CPO_ELS_INFO|{els}")
_ELS_TELEMETRY = ("STATE_DB", "CPO_ELS_TELEMETRY|{els}")

_SEVERITIES = {Cpov2Consts.ALARM: "CRITICAL", Cpov2Consts.WARNING: "WARNING"}
_BOUNDS = {"upper": (Cpov2Consts.HIGH_ALARM, Cpov2Consts.HIGH_WARNING),
           "lower": (Cpov2Consts.LOW_ALARM, Cpov2Consts.LOW_WARNING)}


def _cpo_thresholds() -> dict[str, TelemetryLeaf]:
    db_prefixes = {Cpov2Consts.CH_RX_POWER: "rx_power", Cpov2Consts.CH_TX_POWER: "tx_power"}
    gnmi_names = {Cpov2Consts.CH_RX_POWER: "input-power", Cpov2Consts.CH_TX_POWER: "output-power"}
    db_suffixes = {Cpov2Consts.HIGH_ALARM: "high_alarm", Cpov2Consts.LOW_ALARM: "low_alarm",
                   Cpov2Consts.HIGH_WARNING: "high_warning", Cpov2Consts.LOW_WARNING: "low_warning"}
    return {
        f"{Cpov2Consts.THRESHOLD}/{measure}/{bound}": TelemetryLeaf(
            gnmi=f"{_CPO}/cpo/thresholds/threshold[severity={severity}]/state/{gnmi_names[measure]}-{direction}",
            db=(*_CPO_INFO, f"{db_prefixes[measure]}_{db_suffixes[bound]}"),
            kind="float")
        for measure in db_prefixes
        for direction, bounds in _BOUNDS.items()
        for bound, severity in zip(bounds, _SEVERITIES.values(), strict=True)
    }


def _channel_power(cli_name: str, gnmi_name: str, db_prefix: str) -> dict[str, TelemetryLeaf]:
    return {
        f"{cli_name}/{Cpov2Consts.POWER}": TelemetryLeaf(
            gnmi=f"{_CHANNEL}/{gnmi_name}/instant", db=(*_CPO_CHANNELS, f"lane{{channel_index}}_{db_prefix}"),
            kind="float", tolerance=LIVE_POWER_DBM_TOLERANCE),
        f"{cli_name}/{Cpov2Consts.ALARM_STATUS}": TelemetryLeaf(gnmi=f"{_CHANNEL}/{gnmi_name}/alarm-status",
                                                                kind="bool"),
        f"{cli_name}/{Cpov2Consts.ALARM_SEVERITY}": TelemetryLeaf(gnmi=f"{_CHANNEL}/{gnmi_name}/alarm-severity",
                                                                  kind="alarm_severity"),
    }


def _els_thresholds() -> dict[str, TelemetryLeaf]:
    db_bounds = {"upper": "hi", "lower": "lo"}
    db_severities = {Cpov2Consts.ALARM: "al", Cpov2Consts.WARNING: "war"}
    cli_bounds = {"upper": Cpov2Consts.TX_POWER_UPPER, "lower": Cpov2Consts.TX_POWER_LOWER}
    return {
        f"{Cpov2Consts.THRESHOLD}/{cli_severity}/{cli_bounds[direction]}": TelemetryLeaf(
            gnmi=f"{_ELS}/laser-source/thresholds/threshold[severity={severity}]/state/optical-power-{direction}",
            db=(*_ELS_INFO, f"laser_optical_power_{db_bounds[direction]}_{db_severities[cli_severity]}_th"),
            kind="float")
        for cli_severity, severity in _SEVERITIES.items()
        for direction in cli_bounds
    }


def _laser_bias_thresholds() -> dict[str, TelemetryLeaf]:
    db_fields = {Cpov2Consts.HIGH_ALARM: "laser_bias_hi_al_th", Cpov2Consts.LOW_ALARM: "laser_bias_lo_al_th",
                 Cpov2Consts.HIGH_WARNING: "laser_bias_hi_war_th", Cpov2Consts.LOW_WARNING: "laser_bias_lo_war_th"}
    return {
        f"{Cpov2Consts.LASER_BIAS_CURRENT}/{Cpov2Consts.THRESHOLD}/{bound}": TelemetryLeaf(
            gnmi=f"{_ELS}/laser-source/thresholds/threshold[severity={severity}]/state/laser-bias-current-{direction}",
            db=(*_ELS_INFO, db_fields[bound]), kind="float")
        for direction, bounds in _BOUNDS.items()
        for bound, severity in zip(bounds, _SEVERITIES.values(), strict=True)
    }


def _laser_measure(cli_path: str, value_key: str, gnmi_name: str, db_field: str,
                   db_flag_prefix: str) -> dict[str, TelemetryLeaf]:
    flags = AlarmFlags(*(f"{db_flag_prefix}_{bound}_{level}{{laser_index0}}"
                         for level in ("al", "war") for bound in ("hi", "lo")))
    return {
        f"{cli_path}/{value_key}": _laser_analog(f"{gnmi_name}/instant", db_field),
        f"{cli_path}/{Cpov2Consts.ALARM_STATUS}": TelemetryLeaf(
            gnmi=f"{_LASER}/{gnmi_name}/alarm-status", db=(*_ELS_TELEMETRY, flags), kind="bool"),
        f"{cli_path}/{Cpov2Consts.ALARM_SEVERITY}": TelemetryLeaf(
            gnmi=f"{_LASER}/{gnmi_name}/alarm-severity", db=(*_ELS_TELEMETRY, flags), kind="alarm_severity"),
    }


def _laser_analog(gnmi_leaf: str, db_field: str, tolerance: float = LIVE_ANALOG_TOLERANCE) -> TelemetryLeaf:
    return TelemetryLeaf(gnmi=f"{_LASER}/{gnmi_leaf}", db=(*_ELS_TELEMETRY, f"{db_field}{{laser_index0}}"),
                         kind="float", tolerance=tolerance)


NVUE_TO_TELEMETRY: dict[str, dict[str, TelemetryLeaf]] = {
    "/fae/system/cpo": {
        Cpov2Consts.CPO_DUMP_STATE: TelemetryLeaf(
            gnmi=None, db=("CONFIG_DB", "SYSTEM_CO_OPTICS|els-initialization", "cpo_dump_state")),
    },
    "/interface/{port}": {
        NvosConst.DESCRIPTION: TelemetryLeaf(
            gnmi="interfaces/interface[name={port}]/state/description",
            db=("CONFIG_DB", "IB_PORT|{ib_port}", "description"),
        ),
    },
    f"/interface/{{port}}/{IbInterfaceConsts.LINK}": {
        IbInterfaceConsts.LINK_OPER_STATUS: TelemetryLeaf(
            gnmi=f"{_INTERFACE}/oper-status", db=("STATE_DB", "IB_PORT_TABLE|{ib_port}", "oper_status"), kind="enum"),
    },
    "/interface/{port}/cpo": {
        Cpov2Consts.PARENT: TelemetryLeaf(gnmi=f"{_INTERFACE}/cpo-module"),
        Cpov2Consts.CHANNEL: TelemetryLeaf(gnmi=f"{_INTERFACE}/cpo-channels", kind="index_list"),
    },
    "/platform/cpo/{cpo}": {
        Cpov2Consts.STATUS: TelemetryLeaf(gnmi=f"{_CPO}/state/oper-status", db=(*_CPO_STATUS, "status"),
                                          kind="component_oper_status"),
        Cpov2Consts.ERROR_STATUS: TelemetryLeaf(gnmi=f"{_CPO}/state/error-status", db=(*_CPO_STATUS, "error_status"),
                                                kind="error_status"),
        Cpov2Consts.IDENTIFIER: TelemetryLeaf(gnmi=f"{_CPO}/state/description",
                                              db=(*_CPO_INFO, "cable_identifier")),
        Cpov2Consts.FW_VERSION: TelemetryLeaf(gnmi=f"{_CPO}/state/firmware-version", db=(*_CPO_INFO, "fw_version")),
        **_cpo_thresholds(),
        GNMI_SUBCOMPONENTS: TelemetryLeaf(gnmi=f"{_CPO}/subcomponents/subcomponent[name=*]", cli=False),
    },
    f"/platform/cpo/{{cpo}}/{Cpov2Consts.CHANNEL}/{{channel}}": {
        **_channel_power(Cpov2Consts.CH_RX_POWER, "input-power", "rx_power"),
        **_channel_power(Cpov2Consts.CH_TX_POWER, "output-power", "tx_power"),
        **_channel_power(Cpov2Consts.CH_LASER_SOURCE_INPUT_POWER, "laser-source-input-power", "els_input_power"),
        Cpov2Consts.CH_RX_LOS: TelemetryLeaf(gnmi=f"{_CHANNEL}/rx-los",
                                             db=(*_CPO_CHANNELS, "lane{channel_index}_rx_los"), kind="bool"),
        Cpov2Consts.CH_TX_FAULT: TelemetryLeaf(gnmi=f"{_CHANNEL}/tx-failure",
                                               db=(*_CPO_CHANNELS, "lane{channel_index}_failure_flag_tx"), kind="bool"),
        Cpov2Consts.CH_FAULT_OPCODE: TelemetryLeaf(
            gnmi=f"{_CHANNEL}/fault-opcode", db=(*_CPO_CHANNELS, "lane{channel_index}_oe_advanced_troubleshooting")),
        Cpov2Consts.CH_TX_LOS: TelemetryLeaf(gnmi=f"{_HOST_LANE}/tx-los",
                                             db=(*_CPO_CHANNELS, "lane{channel_index}_tx_los"), kind="bool"),
        Cpov2Consts.CH_DP_STATE: TelemetryLeaf(gnmi=f"{_HOST_LANE}/dp-state",
                                               db=(*_CPO_CHANNELS, "lane{channel_index}_dp_st"), kind="dp_state"),
        GNMI_CHANNEL_INTERFACE: TelemetryLeaf(gnmi=f"{_CHANNEL}/mapping/interface", cli=False),
        GNMI_CHANNEL_INDEX: TelemetryLeaf(gnmi=f"{_CHANNEL_ENTRY}/index", cli=False),
    },
    f"/platform/cpo/{{cpo}}/{Cpov2Consts.OE}/{{oe}}": {
        Cpov2Consts.OE_SERIAL_NUMBER: TelemetryLeaf(gnmi="components/component[name={oe}]/state/serial-no",
                                                    db=("STATE_DB", "CPO_OE_INFO|{oe}", "oe_sn")),
        Cpov2Consts.OE_TEMPERATURE: TelemetryLeaf(
            gnmi="components/component[name={oe}]/state/temperature/instant",
            db=("STATE_DB", "CPO_OE_TELEMETRY|{oe}", "temperature"), kind="float",
            tolerance=LIVE_TEMPERATURE_C_TOLERANCE),
    },
    "/platform/laser-source/{els}": {
        Cpov2Consts.STATUS: TelemetryLeaf(gnmi=f"{_ELS}/state/oper-status", db=(*_ELS_STATUS, "status"),
                                          kind="component_oper_status"),
        Cpov2Consts.ERROR_STATUS: TelemetryLeaf(gnmi=f"{_ELS}/state/error-status", db=(*_ELS_STATUS, "error_status"),
                                                kind="error_status"),
        Cpov2Consts.IDENTIFIER: TelemetryLeaf(gnmi=f"{_ELS}/state/description", db=(*_ELS_INFO, "identifier")),
        Cpov2Consts.ELS_VENDOR_NAME: TelemetryLeaf(gnmi=f"{_ELS}/laser-source/state/vendor",
                                                   db=(*_ELS_INFO, "vendor_name")),
        Cpov2Consts.ELS_VENDOR_PN: TelemetryLeaf(gnmi=f"{_ELS}/state/part-no", db=(*_ELS_INFO, "vendor_pn")),
        Cpov2Consts.ELS_VENDOR_SN: TelemetryLeaf(gnmi=f"{_ELS}/state/serial-no", db=(*_ELS_INFO, "vendor_sn")),
        Cpov2Consts.ELS_VENDOR_REV: TelemetryLeaf(gnmi=f"{_ELS}/laser-source/state/vendor-rev",
                                                  db=(*_ELS_INFO, "vendor_rev")),
        Cpov2Consts.ELS_VENDOR_DATE_CODE: TelemetryLeaf(gnmi=f"{_ELS}/laser-source/state/date-code",
                                                        db=(*_ELS_INFO, "vendor_date_code")),
        Cpov2Consts.FW_VERSION: TelemetryLeaf(gnmi=f"{_ELS}/state/firmware-version",
                                              db=(*_ELS_INFO, "fw_version")),
        Cpov2Consts.PARENT: TelemetryLeaf(gnmi=f"{_ELS}/state/parent", db=(*_ELS_INFO, "vmodule")),
        Cpov2Consts.TEMPERATURE: TelemetryLeaf(
            gnmi=f"{_ELS}/state/temperature/instant", db=(*_ELS_TELEMETRY, "laser_source_temperature"),
            kind="float", tolerance=LIVE_TEMPERATURE_C_TOLERANCE),
        Cpov2Consts.ELS_POWER_CONSUMPTION: TelemetryLeaf(
            gnmi=f"{_ELS}/state/used-power", db=(*_ELS_TELEMETRY, "power_consumption"), kind="float",
            tolerance=LIVE_ANALOG_TOLERANCE),
        Cpov2Consts.ELS_ICC_CURRENT: TelemetryLeaf(
            gnmi=f"{_ELS}/laser-source/state/icc-current", db=(*_ELS_TELEMETRY, "icc_monitor"), kind="float",
            tolerance=LIVE_ANALOG_TOLERANCE),
        **_els_thresholds(),
        GNMI_ELS_PRESENT: TelemetryLeaf(gnmi=f"{_ELS}/laser-source/state/present", cli=False),
    },
    f"/platform/laser-source/{{els}}/{Cpov2Consts.LASER}/{{laser}}": {
        Cpov2Consts.LASER_ENABLED: TelemetryLeaf(gnmi=f"{_LASER}/enabled",
                                                 db=(*_ELS_TELEMETRY, "laser_enabled{laser_index0}"),
                                                 kind="laser_enabled"),
        Cpov2Consts.LASER_OPER_STATUS: TelemetryLeaf(
            gnmi=f"{_LASER}/oper-status", db=(*_ELS_TELEMETRY, "els_oper_state_laser{laser_index0}"),
            kind="laser_oper_status"),
        Cpov2Consts.LASER_ERROR_STATUS: TelemetryLeaf(
            gnmi=f"{_LASER}/error-status", db=(*_ELS_TELEMETRY, "els_laser_state_fault{laser_index0}"),
            kind="laser_error_status"),
        Cpov2Consts.LASER_POWER_RESTRICTION: TelemetryLeaf(
            gnmi=f"{_LASER}/power-restriction", db=(*_ELS_TELEMETRY, "laser_restriction{laser_index0}"),
            kind="power_restriction"),
        Cpov2Consts.LASER_AGE: _laser_analog("laser-age", "laser_age_lane"),
        Cpov2Consts.LASER_HEALTH: _laser_analog("laser-health", "health_value_laser"),
        Cpov2Consts.TEC_HEALTH: _laser_analog("tec-health", "health_value_tec"),
        Cpov2Consts.LASER_TARGET_OUTPUT_POWER: _laser_analog("target-optical-power", "opt_power_setpoint"),
        Cpov2Consts.LASER_MPD_CURRENT: _laser_analog("laser-mpd-current", "laser_mpd_lane"),
        Cpov2Consts.FREQUENCY_ERROR: _laser_analog("target-frequency-deviation/instant", "laser_frequency_error_lane"),
        Cpov2Consts.LASER_TEC_CURRENT: _laser_analog("tec-current/instant", "tec_current_laser"),
        Cpov2Consts.LASER_TEC_VOLTAGE: _laser_analog("tec-voltage/instant", "tec_voltage_laser"),
        Cpov2Consts.LASER_TEMPERATURE: _laser_analog("laser-temperature/instant", "cooled_laser_temperature_lane",
                                                     LIVE_TEMPERATURE_C_TOLERANCE),
        **_laser_measure(Cpov2Consts.LASER_BIAS_CURRENT, Cpov2Consts.CURRENT, "laser-bias-current",
                         "bias_current_monitor", "laser_bias"),
        **_laser_measure(Cpov2Consts.LASER_TX_POWER, Cpov2Consts.POWER, "optical-power", "opt_power_monitor",
                         "laser_opt_pwr"),
        **_laser_bias_thresholds(),
    },
}
