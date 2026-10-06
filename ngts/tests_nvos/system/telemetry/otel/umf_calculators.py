"""Python port of the UMF agent calculator and metric-builder functions.

``agent-mappings.yaml`` names Go functions (``calculatorFunc`` / ``metricBuilderFunc``)
that turn raw Redis fields into the exported OTLP value or labels. To compare the
export against Redis, the test has to apply the same transform, so this module
re-implements them one-to-one, keyed by the same names as the Go registries.

Source: ``nvos/umf`` ``contractagent/mappings/{calculators.go,metric_builders.go,
interface_type.go}`` at commit ``eb92240b2f687d96d7077f0e09c674ea16fba0e4``.
A function the switch's mapping names but this module does not know is reported
as "not validated" by the caller rather than guessed.

Return conventions follow the Go code:

* calculators return ``int``/``float`` for values, ``str`` for labels, ``None`` to
  skip the datapoint; a ``str`` used as a value is dropped (Go rejects it as an
  unsupported value type);
* builders return a list of ``(labels, value)`` and raise :class:`BuilderError`
  where Go returns an error (the Go loop order is random, so the points already
  built for that row are unpredictable and the row is not validated).
"""

import calendar
import datetime
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

UMF_SOURCE_COMMIT = "eb92240b2f687d96d7077f0e09c674ea16fba0e4"

Attributes = Dict[str, str]
Labels = Dict[str, str]
BuiltPoint = Tuple[Labels, Any]

_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1
_UINT64_MAX = (1 << 64) - 1

_FLAG_TRUE = "true"
_FLAG_FALSE = "false"
_FLAG_NA = "n/a"
_STATE_UNSET = "unset"

_OPER_INACTIVE = 0
_OPER_ACTIVE = 1
_OPER_UNSET = 2


class BuilderError(ValueError):
    """A metric builder hit a parse error (Go returns an error for the row)."""


class CalculatorContext:
    """Host-local inputs some calculators read outside Redis (``getHostname``)."""

    def __init__(self, hostname: str = ""):
        self.hostname = hostname


# --------------------------------------------------------------------------- #
# Go string-to-number conversion equivalents
# --------------------------------------------------------------------------- #

_DEC_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_UINT_RE = re.compile(r"^[0-9]+$")
_BASE0_RE = re.compile(r"^([+-]?)(0[xX][0-9a-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+|0[0-7_]*|[1-9][0-9_]*)$")
_FLOAT_RE = re.compile(
    r"^[+-]?(?:(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|inf|infinity|nan)$",
    re.IGNORECASE,
)


def go_parse_int(value: str) -> int:
    """Go ``ParseInt(value, 10, 64)``."""
    if not _DEC_INT_RE.match(value):
        raise ValueError(f"invalid int {value!r}")
    n = int(value)
    if not _INT64_MIN <= n <= _INT64_MAX:
        raise ValueError(f"int out of range {value!r}")
    return n


def go_parse_int_base0(value: str) -> int:
    """Go ``ParseInt(value, 0, 64)`` (decimal, ``0x``, ``0o``, ``0b`` or leading-0 octal)."""
    match = _BASE0_RE.match(value)
    if not match:
        raise ValueError(f"invalid int {value!r}")
    sign, body = match.groups()
    lower = body.lower()
    if lower.startswith("0x"):
        n = int(body[2:].replace("_", ""), 16)
    elif lower.startswith("0o"):
        n = int(body[2:].replace("_", ""), 8)
    elif lower.startswith("0b"):
        n = int(body[2:].replace("_", ""), 2)
    elif len(body) > 1 and body.startswith("0"):
        n = int(body[1:].replace("_", ""), 8)
    else:
        n = int(body.replace("_", ""))
    if sign == "-":
        n = -n
    if not _INT64_MIN <= n <= _INT64_MAX:
        raise ValueError(f"int out of range {value!r}")
    return n


def go_parse_uint(value: str) -> int:
    """Go ``ParseUint(value, 10, 64)``."""
    if not _UINT_RE.match(value):
        raise ValueError(f"invalid uint {value!r}")
    n = int(value)
    if n > _UINT64_MAX:
        raise ValueError(f"uint out of range {value!r}")
    return n


def go_parse_float(value: str) -> float:
    """Go ``ParseFloat(value, 64)`` for the decimal forms Redis carries."""
    if not _FLOAT_RE.match(value):
        raise ValueError(f"invalid float {value!r}")
    return float(value)


def go_atoi(value: str) -> int:
    """Go ``Atoi`` (64-bit ``int``)."""
    return go_parse_int(value)


def _format_float_f0(value: float) -> str:
    """Go ``FormatFloat(value, 'f', 0, 64)``."""
    return f"{value:.0f}"


# --------------------------------------------------------------------------- #
# interface_type.go
# --------------------------------------------------------------------------- #

_IBPP_PLANE_PORT_RE = re.compile(r"^sw.+pl\d+$", re.IGNORECASE)

_IF_TYPE_BY_PREFIX = (
    (("fnm", "sw", "ib"), "infiniband"),
    (("eth",), "ethernetCsmacd"),
    (("bond",), "ieee8023adLag"),
    (("lo",), "softwareLoopback"),
    (("vlan",), "l3ipvlan"),
    (("tunnel",), "tunnel"),
    (("peerlink",), "propVirtual"),
)


def infer_if_type_identity(if_name: str) -> str:
    """``InferIfTypeIdentity``: interface-type identity derived from the name."""
    name = if_name.strip()
    if not name:
        return "other"
    dot = name.find(".")
    if dot > 0:
        return infer_if_type_identity(name[:dot])
    lower = name.lower()
    if lower.startswith(("acp", "nvl")):
        return "nvlink"
    if _IBPP_PLANE_PORT_RE.match(name):
        return "infiniband-plane-port"
    for prefixes, if_type in _IF_TYPE_BY_PREFIX:
        if lower.startswith(prefixes):
            return if_type
    return "other"


# --------------------------------------------------------------------------- #
# calculators.go
# --------------------------------------------------------------------------- #


def _state_with_fallback(status: str, fallback: str) -> int:
    if status == _FLAG_TRUE:
        return _OPER_ACTIVE
    if status == _FLAG_FALSE:
        return _OPER_INACTIVE
    if status == _FLAG_NA:
        return _OPER_UNSET
    if fallback == "":
        return _OPER_UNSET
    if fallback != "0":
        return _OPER_ACTIVE
    return _OPER_INACTIVE


def calculate_psu_state(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return _state_with_fallback(attrs.get("status", ""), attrs.get("power", ""))


def calculate_fan_state(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return _state_with_fallback(attrs.get("status", ""), attrs.get("speed_rpm", ""))


def calculate_leak_sensor_state(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    state = attrs.get("state", "").lower().strip()
    return {"ok": 0, "leak": 1}.get(state, 2)


_OPER_STATE = {
    "up": 1,
    "down": 2,
    "not present": 3,
    "not_present": 3,
    "dormant": 4,
    "lower layer down": 5,
    "lower_layer_down": 5,
    "testing": 6,
}


def calculate_interface_oper_state(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return _OPER_STATE.get(attrs.get("oper_status", "").lower().strip(), 0)


def calculate_link_health_state(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    state = attrs.get("SAI_PORT_ATTR_CUSTOM_NVLINK_LINK_HEALTH_STATE", "").strip()
    return {
        "SAI_PORT_NVLINK_LINK_HEALTH_STATE_NORMAL": 2,
        "SAI_PORT_NVLINK_LINK_HEALTH_STATE_ALARM": 1,
    }.get(state, 0)


def calculate_link_health_attention_since(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    raw = attrs.get("link_attention_since", "").strip()
    if not raw:
        return 0
    try:
        return go_parse_int(raw)
    except ValueError:
        return 0


def calculate_interface_admin_state(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return {"up": 1, "down": 2}.get(attrs.get("admin_status", "").lower().strip(), 0)


def calculate_logical_port_state(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return {"1": "down", "2": "initialize", "3": "armed", "4": "active"}.get(
        attrs.get("SAI_PORT_STAT_INFINIBAND_LOGICAL_STATE", ""), _STATE_UNSET
    )


_PHYSICAL_PORT_STATE = {
    "1": "sleep",
    "2": "polling",
    "3": "disabled",
    "4": "port_configuration_training",
    "5": "link_up",
    "6": "link_error_recovery",
    "7": "phy_test",
    "8": "disabled_by_chassis_manager",
}


def calculate_physical_port_state(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    state = attrs.get("SAI_PORT_STAT_INFINIBAND_PHYSICAL_STATE", "")
    if state == "":
        state = attrs.get("v1_admin", "")
    return _PHYSICAL_PORT_STATE.get(state, _STATE_UNSET)


def convert_loss_percent(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    try:
        return go_parse_float(attrs.get("SAI_PORT_STAT_INFINIBAND_PLR_CODES_LOSS", "")) / 1e8
    except ValueError:
        return _STATE_UNSET


def get_hostname(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return ctx.hostname


def convert_ib_subnet(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    value = attrs.get("ib_subnet", "")
    if value == "0":
        return "infiniband-default"
    return "infiniband-" + value


def _mbps_field_to_gbps(attrs: Attributes, field: str) -> int:
    raw = attrs.get(field, "")
    if raw.strip() == "":
        return -1
    try:
        return int(go_parse_float(raw) / 1000.0)
    except ValueError:
        return -1


def convert_ib_speed_mbps_to_gbps(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return _mbps_field_to_gbps(attrs, "SAI_PORT_STAT_INFINIBAND_SPEED_OPER_BY_LANES")


def convert_ib_port_speed_mbps_to_gbps(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return _mbps_field_to_gbps(attrs, "SAI_PORT_STAT_INFINIBAND_SPEED_OPER")


def convert_supported_ib_speeds_mbps_to_gbps(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    raw = attrs.get("supported_speeds", "")
    if raw == "":
        return ""
    out = []
    for raw_token in raw.split(","):
        token = raw_token.strip()
        if not token:
            continue
        try:
            out.append(_format_float_f0(go_parse_float(token) / 1000.0))
        except ValueError:
            continue
    return ",".join(out)


def calculate_health_status_value(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return {"HEALTHY": 0, "UNHEALTHY": 1}.get(attrs.get("platform-health-state", "").upper(), 2)


def _alarm_bitmap(attrs: Attributes, prefix: str) -> int:
    output = 0
    for suffix, bit in (("highalarm_flag", 1), ("lowalarm_flag", 2), ("highwarning_flag", 4), ("lowwarning_flag", 8)):
        if attrs.get(prefix + suffix, "") == _FLAG_TRUE:
            output |= bit
    return output


def calculate_channel_tx_bias_current_alarm(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return _alarm_bitmap(attrs, "bias")


def calculate_channel_power_alarm(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return _alarm_bitmap(attrs, "power")


def calculate_transceiver_voltage_alarm(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return _alarm_bitmap(attrs, "vcc")


def calculate_transceiver_temperature_alarm(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return _alarm_bitmap(attrs, "temp")


def calculate_transceiver_dp_fw_fault(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return 1 if attrs.get("datapath_firmware_fault", "") == _FLAG_TRUE else 0


def calculate_transceiver_module_fw_fault(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return 1 if attrs.get("module_firmware_fault", "") == _FLAG_TRUE else 0


def calculate_transceiver_firmware_version(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    fw = attrs.get("actual-firmware", "").strip()
    if fw == "" or fw.lower() == "n/a":
        return None
    return 1


def calculate_loopback_mode(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    mode = attrs.get("SAI_PORT_ATTR_PDDR_LOOPBACK_MODE", "").upper().strip()
    return {"NONE": 0, "REMOTE": 1, "LOCAL": 2, "EXTERNAL": 3}.get(mode, 0)


_SUPPORTED_SPEEDS_BIT = {10000: 0, 20000: 1, 40000: 2, 56000: 3, 100000: 4, 200000: 5, 400000: 6, 800000: 7}


def calculate_interface_supported_speeds(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    raw = attrs.get("supported_speeds", "")
    if raw == "":
        return 0
    mask = 0
    for raw_token in raw.split(","):
        token = raw_token.strip().upper()
        if token.endswith("G"):
            token = token[:-1]
        if not token:
            continue
        try:
            n = go_atoi(token)
        except ValueError:
            continue
        if n in _SUPPORTED_SPEEDS_BIT:
            mask |= 1 << _SUPPORTED_SPEEDS_BIT[n]
    return mask


_PHY_MANAGER_STATE = {
    "DISABLED": 0,
    "OPEN_PORT": 1,
    "POLLING": 2,
    "ACTIVE": 3,
    "CLOSE_PORT": 4,
    "PHY_UP": 5,
    "SLEEP": 6,
    "RX_DISABLE": 7,
    "SIGNAL_DETECT": 8,
    "RECEIVER_DETECT": 9,
    "SYNC_PEER": 10,
    "NEGOTIATION": 11,
    "TRAINING": 12,
    "SUBFSM_ACTIVE_E": 13,
}


def calculate_interface_phy_manager_state(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return _PHY_MANAGER_STATE.get(attrs.get("SAI_PORT_ATTR_PHY_MNGR_FSM_STATE", ""), 14)


def _utc_seconds(*parts: str) -> Optional[int]:
    """Unix seconds for a UTC ``(year, month, day, hour, minute, second)``, ``None`` if invalid."""
    try:
        moment = datetime.datetime(*(int(p) for p in parts))  # noqa: DTZ001 - interpreted as UTC below
    except ValueError:
        return None
    return calendar.timegm(moment.timetuple())


_DATETIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})([T ])(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?$"
)


def convert_datetime_to_unix_ns(value: str) -> int:
    """``convertDateTimeToUnixTimeNanoseconds``: YANG date-and-time -> Unix ns (0 on failure).

    Go accepts a timezone (``Z`` or ``+hh:mm``) with either separator, and no
    timezone (UTC) only with the space separator.
    """
    if not value:
        return 0
    match = _DATETIME_RE.match(value)
    if not match:
        return 0
    year, month, day, sep, hour, minute, second, frac, tz = match.groups()
    if tz is None and sep != " ":
        return 0
    secs = _utc_seconds(year, month, day, hour, minute, second)
    if secs is None:
        return 0
    offset = 0
    if tz and tz != "Z":
        sign = -1 if tz[0] == "-" else 1
        offset = sign * (int(tz[1:3]) * 3600 + int(tz[4:6]) * 60)
    nanos = int((frac or "").ljust(9, "0")) if frac else 0
    return (secs - offset) * 1_000_000_000 + nanos


def calculate_boot_time(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return convert_datetime_to_unix_ns(attrs.get("boot_time", ""))


def calculate_last_unhealthy_time(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return convert_datetime_to_unix_ns(attrs.get("last-unhealthy", ""))


_GEN_TIME_KEY_RE = re.compile(r"^(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})$")


def calculate_last_reboot_time_from_key(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    match = _GEN_TIME_KEY_RE.match(key)
    if not match:
        return 0
    secs = _utc_seconds(*match.groups())
    return 0 if secs is None else secs * 1_000_000_000


def get_partition_id_from_key(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return {"IMAGE1": "1", "IMAGE2": "2"}.get(key, "")


def parse_partition_image_usage(usage: str) -> int:
    """``ParsePartitionImageUsage``: 1 = current, 2 = next, OR-ed."""
    raw = usage.strip()
    if not raw:
        return 0
    for sep in (",", "|", ";"):
        raw = raw.replace(sep, " ")
    flags = 0
    for token in raw.split():
        flags |= {"current": 1, "next": 2}.get(token.lower(), 0)
    return flags


def calculate_partition_info_value(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return parse_partition_image_usage(attrs.get("usage", ""))


def calculate_ib_router_enabled(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return 1 if attrs.get("ib_routing", "") == "enabled" else 0


def calculate_ib_router_subnet_valid(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return 1 if attrs.get("SAI_PORT_ATTR_CUSTOM_INFINIBAND_ROUTER_VALID", "") == _FLAG_TRUE else 0


def calculate_interface_type(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return infer_if_type_identity(key)


def convert_maintenance_state(key: str, attrs: Attributes, ctx: CalculatorContext) -> Any:
    return {"n/a": 0, "up": 1, "down": 2, "diag": 3}.get(attrs.get("maintenance_state", "").lower().strip(), 0)


CalculatorFunc = Callable[[str, Attributes, CalculatorContext], Any]

CALCULATORS: Dict[str, CalculatorFunc] = {
    "calculatePSUState": calculate_psu_state,
    "calculateFanState": calculate_fan_state,
    "calculateLogicalPortState": calculate_logical_port_state,
    "calculatePhysicalPortState": calculate_physical_port_state,
    "convertLossPercent": convert_loss_percent,
    "getHostname": get_hostname,
    "convertIbSubnet": convert_ib_subnet,
    "convertIbSpeedMbpsToGbps": convert_ib_speed_mbps_to_gbps,
    "convertSupportedIbSpeedsMbpsToGbps": convert_supported_ib_speeds_mbps_to_gbps,
    "convertIbPortSpeedMbpsToGbps": convert_ib_port_speed_mbps_to_gbps,
    "calculateHealthStatusValue": calculate_health_status_value,
    "calculateLeakSensorState": calculate_leak_sensor_state,
    "calculateInterfaceOperState": calculate_interface_oper_state,
    "calculateInterfaceAdminState": calculate_interface_admin_state,
    "calculateLinkHealthState": calculate_link_health_state,
    "calculateLinkHealthAttentionSince": calculate_link_health_attention_since,
    "calculateChannelPowerAlarm": calculate_channel_power_alarm,
    "calculateChannelTxBiasCurrentAlarm": calculate_channel_tx_bias_current_alarm,
    "calculateTransceiverVoltageAlarm": calculate_transceiver_voltage_alarm,
    "calculateTransceiverTemperatureAlarm": calculate_transceiver_temperature_alarm,
    "calculateTransceiverDpFwFault": calculate_transceiver_dp_fw_fault,
    "calculateTransceiverModuleFwFault": calculate_transceiver_module_fw_fault,
    "calculateTransceiverFirmwareVersion": calculate_transceiver_firmware_version,
    "calculateLoopbackMode": calculate_loopback_mode,
    "calculateInterfaceSupportedSpeeds": calculate_interface_supported_speeds,
    "calculateInterfacePhyManagerState": calculate_interface_phy_manager_state,
    "calculateBootTime": calculate_boot_time,
    "calculateLastUnhealthyTime": calculate_last_unhealthy_time,
    "calculateLastRebootTimeFromKey": calculate_last_reboot_time_from_key,
    "getPartitionIDFromKey": get_partition_id_from_key,
    "calculatePartitionInfoValue": calculate_partition_info_value,
    "calculateIbRouterEnabled": calculate_ib_router_enabled,
    "calculateIbRouterSubnetValid": calculate_ib_router_subnet_valid,
    "calculateInterfaceType": calculate_interface_type,
    "convertMaintenanceState": convert_maintenance_state,
}


# --------------------------------------------------------------------------- #
# metric_builders.go
# --------------------------------------------------------------------------- #


def _resolve_interface_name(attrs: Attributes, key: str) -> str:
    name = attrs.get("interface", "")
    if name in ("", "N/A"):
        return key
    return name


def _parse_state_value(value: str) -> bool:
    if value in (_FLAG_TRUE, "1", "yes"):
        return True
    if value in (_FLAG_FALSE, "0", "no", ""):
        return False
    return value.lower() in (_FLAG_TRUE, "yes")


def _state_field_points(
    key: str, attrs: Attributes, field_to_state: Dict[str, str], channel_label: str
) -> List[BuiltPoint]:
    channel = attrs.get("channel", "")
    points: List[BuiltPoint] = []
    for field, state in field_to_state.items():
        if field not in attrs:
            continue
        labels = {"name": key, channel_label: channel, "state": state}
        points.append((labels, 1 if _parse_state_value(attrs[field]) else 0))
    return points


def build_transceiver_physical_channel_state(key: str, attrs: Attributes) -> List[BuiltPoint]:
    return _state_field_points(key, attrs, {"rxlos": "rx-los", "rxcdrlol": "rx-cdr-lol"}, "channel")


def build_transceiver_host_lane_state(key: str, attrs: Attributes) -> List[BuiltPoint]:
    fields = {
        "txfault": "tx-fault",
        "txadeqfault_flag": "tx-ad-eq-fault",
        "txlos_hostlane": "tx-los",
        "txcdrlol_hostlane": "tx-cdr-lol",
    }
    return _state_field_points(key, attrs, fields, "lane")


def build_transceiver_temp_threshold_info(key: str, attrs: Attributes) -> List[BuiltPoint]:
    points: List[BuiltPoint] = []
    for field, threshold in (("templowalarm", "lower"), ("temphighalarm", "upper")):
        raw = attrs.get(field)
        if raw is None or raw.strip() == "":
            continue
        try:
            value = go_parse_float(raw.strip())
        except ValueError as err:
            raise BuilderError(f"{field}={raw!r}") from err
        points.append(({"name": key, "threshold": threshold, "severity": "WARNING"}, value))
    return points


def _int_field_points(
    attrs: Attributes,
    field_to_label: Dict[str, str],
    *,
    interface: str,
    label_name: str,
    parse: Callable[[str], int] = go_parse_int,
) -> List[BuiltPoint]:
    points: List[BuiltPoint] = []
    for field, label_value in field_to_label.items():
        raw = attrs.get(field)
        if raw is None or raw.strip() == "":
            continue
        try:
            value = parse(raw)
        except ValueError as err:
            raise BuilderError(f"{field}={raw!r}") from err
        points.append(({"interface": interface, label_name: label_value}, value))
    return points


def build_interface_phy_fec_mode(key: str, attrs: Attributes) -> List[BuiltPoint]:
    fields = {"SAI_PORT_ATTR_FEC_MODE_REQUEST": "requested", "SAI_PORT_ATTR_PROFILE_FEC_IN_USE": "in_use"}
    return _int_field_points(attrs, fields, interface=key, label_name="fec_status_type")


def build_interface_phy_link_down_opcode(key: str, attrs: Attributes) -> List[BuiltPoint]:
    fields = {"local_reason_opcode": "local", "remote_reason_opcode": "remote"}
    return _int_field_points(
        attrs,
        fields,
        interface=_resolve_interface_name(attrs, key),
        label_name="code_type",
        parse=go_parse_int_base0,
    )


def build_interface_phy_stats_attempts_in_last_recovery(key: str, attrs: Attributes) -> List[BuiltPoint]:
    fields = {
        "SAI_PORT_STAT_NVLINK_LAST_LOGIC_RECOVERY_ATTEMPTS": "logic",
        "SAI_PORT_STAT_NVLINK_LAST_SERDES_EQ_RECOVERY_ATTEMPTS": "serdes_eq",
        "SAI_PORT_STAT_INFINIBAND_LAST_LOGIC_RECOVERY_ATTEMPTS": "logic",
        "SAI_PORT_STAT_INFINIBAND_LAST_SERDES_EQ_RECOVERY_ATTEMPTS": "serdes_eq",
    }
    return _int_field_points(
        attrs, fields, interface=_resolve_interface_name(attrs, key), label_name="recovery_event"
    )


def build_interface_link_down_events(key: str, attrs: Attributes) -> List[BuiltPoint]:
    fields = {
        "SAI_PORT_STAT_INFINIBAND_UNINTENTIONAL_LINK_DOWN_EVENTS": "unintentional",
        "SAI_PORT_STAT_INFINIBAND_INTENTIONAL_LINK_DOWN_EVENTS": "intentional",
    }
    return _int_field_points(attrs, fields, interface=_resolve_interface_name(attrs, key), label_name="type")


def build_interface_phy_retransmission_mode(key: str, attrs: Attributes) -> List[BuiltPoint]:
    fields = {"SAI_PORT_ATTR_RETRAN_MODE_ACTIVE": "active", "SAI_PORT_ATTR_RETRAN_MODE_REQUEST": "requested"}
    return _int_field_points(attrs, fields, interface=key, label_name="retransmission_type")


_FSM_FIELDS = {
    "SAI_PORT_ATTR_PD_FSM_STATE": "pd",
    "SAI_PORT_ATTR_PSI_FSM_STATE": "psi",
    "SAI_PORT_ATTR_PHY_HST_FSM_STATE": "phy_hst",
    "SAI_PORT_ATTR_IB_PHY_FSM_STATE": "ib_phy",
    "SAI_PORT_ATTR_ETH_AN_FSM_STATE": "eth_an",
}
_FSM_ENUMS: Dict[str, Tuple[Dict[str, int], int]] = {
    "ib_phy": (
        {
            "DISABLED": 0,
            "INITIALLY": 1,
            "RCVR_CFG": 2,
            "CFG_TEST": 3,
            "WAIT_RMT_TEST": 4,
            "WAIT_CFG_ENHANCED": 5,
            "CFG_IDLE": 6,
            "LINK_UP": 7,
            "POLLING": 8,
            "SYNC_CHECK": 9,
        },
        10,
    ),
    "phy_hst": (
        {
            "DISABLED": 0,
            "DESKEW_FIX_REVERSALS": 1,
            "WAIT_TS2": 2,
            "WAIT_MCB": 3,
            "MCB_AN": 4,
            "RECEIVER_READY": 5,
            "SIGNAL_DETECT": 6,
            "ADAPTATION": 7,
            "LINKUP": 8,
            "SUBFSM_FAIL": 9,
            "ERROR": 10,
        },
        11,
    ),
    "psi": ({"IDLE": 0, "WAIT4UCDONE": 1, "SAFE_OPEN_CLOSE": 2, "ERROR": 3}, 4),
}


def build_interface_phy_fsm_state(key: str, attrs: Attributes) -> List[BuiltPoint]:
    points: List[BuiltPoint] = []
    for field, component in _FSM_FIELDS.items():
        raw = attrs.get(field)
        if raw is None or raw.strip() == "":
            continue
        value = raw.strip()
        if component in _FSM_ENUMS:
            enum, unknown = _FSM_ENUMS[component]
            number = enum.get(value, unknown)
        else:
            try:
                number = go_parse_int(value)
            except ValueError:
                continue
        points.append(({"interface": key, "fsm_component": component}, number))
    return points


_PHY_LINK_INFO_FIELDS = (
    ("phy-hst", "width", "SAI_PORT_ATTR_PHY_HST_LINK_WIDTH_ENABLED"),
    ("phy-hst", "speed", "SAI_PORT_ATTR_PHY_HST_LINK_SPEED_ENABLED"),
    ("pd", "speed", "SAI_PORT_ATTR_PD_LINK_SPEED_ENABLED"),
    ("pd", "width", "SAI_PORT_ATTR_PD_LINK_WIDTH_ENABLED"),
    ("core-to-phy", "proto", "SAI_PORT_ATTR_CORE_TO_PHY_LINK_PROTO_ENABLED"),
    ("core-to-phy", "width", "SAI_PORT_ATTR_CORE_TO_PHY_LINK_WIDTH_ENABLED"),
    ("phy-manager", "proto", "SAI_PORT_ATTR_PHY_MANAGER_LINK_PROTO_ENABLED"),
    ("phy-manager", "width", "SAI_PORT_ATTR_PHY_MANAGER_LINK_WIDTH_ENABLED"),
)


def build_interface_phy_link_info(key: str, attrs: Attributes) -> List[BuiltPoint]:
    points: List[BuiltPoint] = []
    for segment, config_type, field in _PHY_LINK_INFO_FIELDS:
        raw = attrs.get(field)
        if raw is None:
            continue
        try:
            if field == "SAI_PORT_ATTR_PHY_HST_LINK_SPEED_ENABLED":
                if raw == "0:null":
                    value = 0
                else:
                    _, sep, tail = raw.partition(":")
                    if not sep or ":" in tail:
                        continue
                    value = go_parse_int(tail)
            else:
                value = go_parse_int(raw.strip())
        except ValueError:
            continue
        points.append(({"interface": key, "phy_segment": segment, "config_type": config_type}, value))
    return points


def _reboot_count_points(attrs: Attributes, *, prefix: str, label: str, rename: bool) -> List[BuiltPoint]:
    points: List[BuiltPoint] = []
    for leaf, count in attrs.items():
        if leaf != "count" and not leaf.startswith(prefix):
            continue
        try:
            value = go_parse_int(count.strip())
        except ValueError as err:
            raise BuilderError(f"{leaf}={count!r}") from err
        label_value = leaf
        if rename:
            label_value = "total" if leaf == "count" else leaf[len(prefix):]
        points.append(({label: label_value}, value))
    return points


def build_gnmi_reboot_count(key: str, attrs: Attributes) -> List[BuiltPoint]:
    return _reboot_count_points(attrs, prefix="openconfig-", label="type", rename=False)


def build_otel_reboot_count(key: str, attrs: Attributes) -> List[BuiltPoint]:
    return _reboot_count_points(attrs, prefix="internal-", label="cause", rename=True)


RS_HIST_BUCKETS = 16


def build_interface_phy_stats_rs_hist(key: str, attrs: Attributes) -> List[BuiltPoint]:
    """One histogram point: ``{"bucket_counts": [...17], "count": n, "sum": float(n)}``."""
    counts = [0] * (RS_HIST_BUCKETS + 1)
    for i in range(RS_HIST_BUCKETS):
        raw = attrs.get(f"SAI_PORT_STAT_INFINIBAND_RS_HIST_{i}")
        if raw is None:
            continue
        try:
            counts[i] = go_parse_uint(raw)
        except ValueError as err:
            raise BuilderError(f"RS_HIST_{i}={raw!r}") from err
    total = sum(counts)
    return [({"interface": key}, {"bucket_counts": counts, "count": total, "sum": float(total)})]


BuilderFunc = Callable[[str, Attributes], List[BuiltPoint]]

METRIC_BUILDERS: Dict[str, BuilderFunc] = {
    "buildTransceiverPhysicalChannelState": build_transceiver_physical_channel_state,
    "buildTransceiverHostLaneState": build_transceiver_host_lane_state,
    "buildTransceiverTempThresholdInfo": build_transceiver_temp_threshold_info,
    "buildInterfacePhyFecMode": build_interface_phy_fec_mode,
    "buildInterfacePhyRetransmissionMode": build_interface_phy_retransmission_mode,
    "buildInterfacePhyFsmState": build_interface_phy_fsm_state,
    "buildInterfacePhyLinkInfo": build_interface_phy_link_info,
    "buildInterfaceLinkDownEvents": build_interface_link_down_events,
    "buildInterfacePhyLinkDownOpcode": build_interface_phy_link_down_opcode,
    "buildInterfacePhyStatsAttemptsInLastRecovery": build_interface_phy_stats_attempts_in_last_recovery,
    "buildGNMIRebootCount": build_gnmi_reboot_count,
    "buildOTELRebootCount": build_otel_reboot_count,
    "buildInterfacePhyStatsRsHist": build_interface_phy_stats_rs_hist,
}

HISTOGRAM_BUILDERS = frozenset({"buildInterfacePhyStatsRsHist"})


def lookup_calculator(name: str) -> Optional[CalculatorFunc]:
    return CALCULATORS.get(name)


def lookup_builder(name: str) -> Optional[BuilderFunc]:
    return METRIC_BUILDERS.get(name)
