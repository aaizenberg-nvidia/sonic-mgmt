import re
from collections.abc import Iterable, Mapping

from ngts.nvos_constants.constants_nvos import Cpov2Consts, HealthConsts
from ngts.nvos_tools.Devices.cpo.CpoTopology import CpoTopology
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.platform.Cpo import Cpo

CPO_SUMMARY_FIELDS = (
    Cpov2Consts.FW_VERSION,
    Cpov2Consts.PORTS,
    Cpov2Consts.LASER_SOURCES,
)
CPO_DETAIL_FIELDS = (
    Cpov2Consts.STATUS,
    Cpov2Consts.ERROR_STATUS,
    Cpov2Consts.IDENTIFIER,
    Cpov2Consts.FW_VERSION,
    Cpov2Consts.PORTS,
    Cpov2Consts.LASER_SOURCES,
    Cpov2Consts.THRESHOLDS,
    Cpov2Consts.OE,
    Cpov2Consts.CHANNEL,
)
# `thresholds` is keyed by measured value, each carrying the four bounds
CPO_THRESHOLD_TYPES = (
    Cpov2Consts.CH_LASER_SOURCE_INPUT_POWER,
    Cpov2Consts.CH_RX_POWER,
    Cpov2Consts.CH_TX_POWER,
)
CPO_THRESHOLD_BOUNDS = (
    Cpov2Consts.HIGH_ALARM,
    Cpov2Consts.LOW_ALARM,
    Cpov2Consts.HIGH_WARNING,
    Cpov2Consts.LOW_WARNING,
)
OE_FIELDS = (
    Cpov2Consts.IDENTIFIER,
    Cpov2Consts.OE_SERIAL_NUMBER,
    Cpov2Consts.OE_TEMPERATURE,
)
CHANNEL_FIELDS = (
    Cpov2Consts.CH_RX_POWER,
    Cpov2Consts.CH_TX_POWER,
    Cpov2Consts.CH_RX_LOS,
    Cpov2Consts.CH_TX_LOS,
    Cpov2Consts.CH_TX_FAULT,
    Cpov2Consts.CH_LASER_SOURCE_INPUT_POWER,
    Cpov2Consts.CH_FAULT_OPCODE,
    Cpov2Consts.CH_DP_STATE,
)
POWER_FIELDS = (Cpov2Consts.POWER, Cpov2Consts.ALARM_STATUS, Cpov2Consts.ALARM_SEVERITY)
LASER_SOURCE_SUMMARY_FIELDS = (
    Cpov2Consts.IDENTIFIER,
    Cpov2Consts.ELS_VENDOR_NAME,
    Cpov2Consts.ELS_VENDOR_PN,
    Cpov2Consts.ELS_VENDOR_SN,
    Cpov2Consts.ELS_VENDOR_REV,
    Cpov2Consts.FW_VERSION,
)
LASER_SOURCE_DETAIL_FIELDS = (
    Cpov2Consts.DIAGNOSTICS_STATUS,
    Cpov2Consts.STATUS,
    Cpov2Consts.ERROR_STATUS,
    Cpov2Consts.ELS_VENDOR_DATE_CODE,
    Cpov2Consts.IDENTIFIER,
    Cpov2Consts.ELS_VENDOR_NAME,
    Cpov2Consts.ELS_VENDOR_REV,
    Cpov2Consts.ELS_VENDOR_PN,
    Cpov2Consts.ELS_VENDOR_SN,
    Cpov2Consts.FW_VERSION,
    Cpov2Consts.PARENT,
    Cpov2Consts.TEMPERATURE,
    Cpov2Consts.ELS_POWER_CONSUMPTION,
    Cpov2Consts.ELS_ICC_CURRENT,
    Cpov2Consts.THRESHOLD,
    Cpov2Consts.LASER,
)
LASER_SOURCE_THRESHOLD_FIELDS = (Cpov2Consts.TX_POWER_UPPER, Cpov2Consts.TX_POWER_LOWER)
LASER_FIELDS = (
    Cpov2Consts.LASER_ENABLED,
    Cpov2Consts.LASER_OPER_STATUS,
    Cpov2Consts.LASER_ERROR_STATUS,
    Cpov2Consts.LASER_RAMPING_STATUS,
    Cpov2Consts.LASER_POWER_RESTRICTION,
    Cpov2Consts.LASER_AGE,
    Cpov2Consts.LASER_TARGET_OUTPUT_POWER,
    Cpov2Consts.LASER_MPD_CURRENT,
    Cpov2Consts.LASER_BIAS_CURRENT,
    Cpov2Consts.LASER_TEC_CURRENT,
    Cpov2Consts.LASER_TEC_VOLTAGE,
    Cpov2Consts.LASER_TEMPERATURE,
    Cpov2Consts.LASER_HEALTH,
    Cpov2Consts.TEC_HEALTH,
    Cpov2Consts.FREQUENCY_ERROR,
    Cpov2Consts.LASER_TX_POWER,
)
INTERFACE_CPO_FIELDS = CPO_DETAIL_FIELDS + (Cpov2Consts.PARENT,)
# `nv show interface <port> cpo` header fields inherited AS-IS from the parent
# CPO (full mapping lists, not the port's subset); only the oe/channel blocks
# are the port's slice.
INTERFACE_HEADER_FIELDS = tuple(
    field for field in CPO_DETAIL_FIELDS if field not in (Cpov2Consts.OE, Cpov2Consts.CHANNEL)
)
# mapping fields may render as a comma-separated string or a JSON list
# (see Cpo.split_names), so header inheritance compares them as name sets.
_INTERFACE_HEADER_NAME_LISTS = (
    Cpov2Consts.PORTS,
    Cpov2Consts.LASER_SOURCES,
)
_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")


def sample_names(names: Iterable[str]) -> list[str]:
    values = list(names)
    assert values, "cannot sample an empty collection"
    return list(dict.fromkeys((values[0], values[len(values) // 2], values[-1])))


def unwrap_instance(data: dict, instance: str) -> dict:
    nested = data.get(instance)
    return nested if isinstance(nested, dict) else data


def _number(value: object) -> float:
    match = _NUMBER_PATTERN.search(str(value))
    assert match, f"value has no numeric part: {value!r}"
    return float(match.group())


def _assert_severity_first_threshold_order(thresholds: Mapping, fields: Iterable[str], context: str) -> None:
    """Order check for the severity-first shape (`laser-source` THRESHOLD)."""
    severities = (Cpov2Consts.WARNING, Cpov2Consts.ALARM)
    ValidationTool.validate_subset_in_superset(severities, thresholds).verify_result()
    for field in fields:
        warning = _number(thresholds[Cpov2Consts.WARNING][field])
        alarm = _number(thresholds[Cpov2Consts.ALARM][field])
        if field.endswith(("low", "lower")):
            assert alarm <= warning, f"{context} {field}: alarm {alarm} > warning {warning}"
        else:
            assert alarm >= warning, f"{context} {field}: alarm {alarm} < warning {warning}"


def _assert_cpo_threshold_order(thresholds: Mapping, context: str) -> None:
    """Order check for the measured-value-first shape (`platform cpo` THRESHOLDS).

    Each measured value carries its own four bounds, so alarm must sit outside
    warning on both ends and the low pair must sit below the high pair.
    """
    ValidationTool.validate_subset_in_superset(CPO_THRESHOLD_TYPES, thresholds).verify_result()
    for measured in CPO_THRESHOLD_TYPES:
        bounds = thresholds[measured]
        where = f"{context}/{measured}"
        ValidationTool.validate_subset_in_superset(CPO_THRESHOLD_BOUNDS, bounds).verify_result()
        low_alarm = _number(bounds[Cpov2Consts.LOW_ALARM])
        low_warning = _number(bounds[Cpov2Consts.LOW_WARNING])
        high_warning = _number(bounds[Cpov2Consts.HIGH_WARNING])
        high_alarm = _number(bounds[Cpov2Consts.HIGH_ALARM])
        assert low_alarm <= low_warning, f"{where}: low-alarm {low_alarm} > low-warning {low_warning}"
        assert high_warning <= high_alarm, f"{where}: high-warning {high_warning} > high-alarm {high_alarm}"
        assert low_warning <= high_warning, f"{where}: low-warning {low_warning} > high-warning {high_warning}"


def validate_cpo_summary(summary: Mapping, topology: CpoTopology) -> None:
    ValidationTool.validate_set_equal(summary, topology.cpo_names()).verify_result()
    for data in summary.values():
        ValidationTool.validate_subset_in_superset(CPO_SUMMARY_FIELDS, data).verify_result()


def validate_cpo_detail(cpo: str, detail: Mapping, topology: CpoTopology) -> None:
    ValidationTool.validate_subset_in_superset(CPO_DETAIL_FIELDS, detail).verify_result()
    laser_sources = Cpo.split_names(detail[Cpov2Consts.LASER_SOURCES])
    ValidationTool.validate_set_equal(laser_sources, topology.els_for_cpo(cpo)).verify_result()
    ValidationTool.validate_set_equal(detail[Cpov2Consts.OE], topology.oes_for_cpo(cpo)).verify_result()
    ValidationTool.validate_set_equal(detail[Cpov2Consts.CHANNEL], topology.channels_for_cpo(cpo)).verify_result()
    for data in detail[Cpov2Consts.OE].values():
        ValidationTool.validate_subset_in_superset(OE_FIELDS, data).verify_result()
    for data in detail[Cpov2Consts.CHANNEL].values():
        ValidationTool.validate_subset_in_superset(CHANNEL_FIELDS, data).verify_result()
        for field in (Cpov2Consts.CH_RX_POWER, Cpov2Consts.CH_TX_POWER):
            ValidationTool.validate_subset_in_superset(POWER_FIELDS, data[field]).verify_result()
    _assert_cpo_threshold_order(detail[Cpov2Consts.THRESHOLDS], f"{cpo} thresholds")


def validate_laser_source_summary(summary: Mapping, topology: CpoTopology) -> None:
    ValidationTool.validate_set_equal(summary, topology.els_names()).verify_result()
    for data in summary.values():
        ValidationTool.validate_subset_in_superset(LASER_SOURCE_SUMMARY_FIELDS, data).verify_result()


def validate_laser_source_detail(els: str, detail: Mapping, topology: CpoTopology) -> None:
    ValidationTool.validate_subset_in_superset(LASER_SOURCE_DETAIL_FIELDS, detail).verify_result()
    assert detail[Cpov2Consts.PARENT] == topology.cpo_for_els(els)
    ValidationTool.validate_set_equal(detail[Cpov2Consts.LASER], topology.lasers_for_els(els)).verify_result()
    for data in detail[Cpov2Consts.LASER].values():
        ValidationTool.validate_subset_in_superset(LASER_FIELDS, data).verify_result()
        tx_power = data[Cpov2Consts.LASER_TX_POWER]
        ValidationTool.validate_subset_in_superset(POWER_FIELDS, tx_power).verify_result()
    thresholds = detail[Cpov2Consts.THRESHOLD]
    for severity in (Cpov2Consts.WARNING, Cpov2Consts.ALARM):
        bounds = thresholds[severity]
        ValidationTool.validate_subset_in_superset(LASER_SOURCE_THRESHOLD_FIELDS, bounds).verify_result()
    _assert_severity_first_threshold_order(thresholds, LASER_SOURCE_THRESHOLD_FIELDS, f"{els} thresholds")


def validate_interface_cpo(port: str, detail: Mapping, cpo_detail: Mapping) -> str:
    ValidationTool.validate_subset_in_superset(INTERFACE_CPO_FIELDS, detail).verify_result()
    parent = detail[Cpov2Consts.PARENT]
    assert port in Cpo.split_names(cpo_detail[Cpov2Consts.PORTS])
    for field in INTERFACE_HEADER_FIELDS:
        if field in _INTERFACE_HEADER_NAME_LISTS:
            matches = set(Cpo.split_names(detail[field])) == set(Cpo.split_names(cpo_detail[field]))
        elif field == Cpov2Consts.STATUS:
            matches = str(detail[field]).lower() == str(cpo_detail[field]).lower()
        else:
            matches = detail[field] == cpo_detail[field]
        assert matches, f"{port} {field} differs from its parent CPO header"
    # the port's slice: exactly its own OE and channel(s), nothing else of the
    # CPO (exact expected identity needs the DB mapping - TP O-10)
    assert len(detail[Cpov2Consts.OE]) == 1, f"{port} must show exactly one OE, got {sorted(detail[Cpov2Consts.OE])}"
    assert detail[Cpov2Consts.CHANNEL], f"{port} shows no channels"
    if len(Cpo.split_names(cpo_detail[Cpov2Consts.PORTS])) > 1:
        assert len(detail[Cpov2Consts.CHANNEL]) < len(cpo_detail[Cpov2Consts.CHANNEL]), (
            f"{port} must show only its own channel slice, got all "
            f"{len(detail[Cpov2Consts.CHANNEL])} channels of the CPO"
        )
    for subtree in (Cpov2Consts.OE, Cpov2Consts.CHANNEL):
        ValidationTool.validate_subset_in_superset(detail[subtree], cpo_detail[subtree]).verify_result()
        for name, entry in detail[subtree].items():
            ValidationTool.validate_set_equal(entry, cpo_detail[subtree][name]).verify_result()
    return parent


def read_interface_cpo(port: str, engine) -> dict:
    return Interface(parent_obj=None, port_name=port).cpo.parse_show(dut_engine=engine)


def read_cpo(platform, cpo: str, engine) -> dict:
    return unwrap_instance(platform.cpo.cpo_id[cpo].parse_show(dut_engine=engine), cpo)


def read_els(platform, els: str, engine) -> dict:
    return unwrap_instance(platform.laser_source.els_id[els].parse_show(dut_engine=engine), els)


def validate_healthy_instances(component: str, data: Mapping, expected: Iterable[str]) -> None:
    instances = data[component][HealthConsts.Component.INSTANCE]
    ValidationTool.validate_set_equal(instances, expected).verify_result()
    fields = [HealthConsts.Component.STATE, HealthConsts.Component.UNHEALTHY_COUNT]
    values = [HealthConsts.Component.HEALTHY, 0]
    for health in instances.values():
        ValidationTool.validate_fields_values_in_output(fields, values, health).verify_result()
