import re
import allure
import pandas as pd
import logging
from ngts.constants.performance_constants import (
    PowerConsts, ValidationConsts, MongoDbConsts, RailModel, RailDomain)
from ngts.helpers.performance.performance_db_helpers import add_test_mongo_metadata, get_base_df, calculate_avg_on_all_samples
from ngts.helpers.performance.sensors_power_parse import (
    get_controllers_info_str_list,
    resolve_rail_name,
    normalized_i2c_address,
    parse_i2c_bus_and_address,
)
from infra.tools.exceptions.test_issue import TestIssue

logger = logging.getLogger()


def validate_temperature(traffic_json, temperature_threshold, violations_list):
    with allure.step(f"Validate all temperature samples are below {temperature_threshold}"):
        temperature_samples = traffic_json[ValidationConsts.TEMPERATURE_SAMPLES]
        temperature_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
        higher_temperature_samples = []
        for sample_id, temperature_samples_dict in temperature_samples.items():
            temperature = temperature_samples_dict[ValidationConsts.TEMPERATURE]
            if int(temperature) > temperature_threshold:
                higher_temperature_samples.append(f"{sample_id} - temperature: {temperature}")
        if higher_temperature_samples:
            violations_list.append(f"Not all temperature samples were lower than threshold {temperature_threshold}, "
                                   f"please check {higher_temperature_samples}")


def validate_power(traffic_json, players, test_name, chip_type, power_threshold, violations_list):
    """
    Validates power consumption samples against defined thresholds and generates power consumption reports.

    This function also attaches the power consumption dataframe to mongo DB

    Args:
        traffic_json (dict): JSON containing power samples data
        players (dict): Dictionary containing test players, including DUT CLI object
        test_name (str): Name of the test being executed
        chip_type (str): Type of chip being tested (e.g., 'SPC3')
        power_threshold (Optional[float]): ASIC-domain total power budget in watts,
            or ``None`` when the chip has no total check. Per-rail group budgets
            come from the declarative rail table, not this argument.
        violations_list (list): List to store any power threshold violations

    Returns:
        tuple: A tuple containing two pandas DataFrames:
            - power_df_with_total: Detailed power consumption data for all collectors with total
            - power_df_by_collectors_group_with_total: Summarized power consumption by collector groups with total
    """
    with allure.step(f"Validate all power samples are below the power_thresholds"):
        dut_cli_obj = players['dut']['cli']
        power_samples = traffic_json[ValidationConsts.POWER_SAMPLES]
        power_samples.pop(ValidationConsts.SAMPLES_PARAMS, None)
        power_df = get_avg_samples_power_dataframe(dut_cli_obj, chip_type, power_samples)
        power_df_with_total = validate_power_df_by_collectors(power_df, power_threshold, violations_list, chip_type)
        allure.attach(power_df_with_total.to_html(), 'Power full dataframe', allure.attachment_type.HTML)
        power_df_by_collectors_group_with_total = get_sum_power_df_by_collectors_group(power_df, chip_type)
        allure.attach(power_df_by_collectors_group_with_total.to_html(),
                      'Power summary by collectors', allure.attachment_type.HTML)
        add_test_mongo_metadata(test_name, {
            MongoDbConsts.POWER_TOTAL: power_df_with_total.to_dict(orient='records'),
            MongoDbConsts.POWER_BY_COLLECTORS: power_df_by_collectors_group_with_total.to_dict(orient='records')})
        return power_df_with_total, power_df_by_collectors_group_with_total


def get_avg_samples_power_dataframe(cli_obj, chip_type, power_samples):
    df_list = []
    for sample_id, power_sample in power_samples.items():
        sensors_output = get_sensors_data(cli_obj, power_sample)
        power_df = get_power_dataframe(cli_obj, sensors_output, chip_type)
        df_list.append(power_df)
    df_result = get_base_df(df_list)
    df_result[PowerConsts.POWER_CURRENT] = calculate_avg_on_all_samples(df_list, PowerConsts.POWER_CURRENT)
    df_result[PowerConsts.POWER_WATT] = calculate_avg_on_all_samples(df_list, PowerConsts.POWER_WATT)
    return df_result


def get_sensors_data(cli_obj, power_sample):
    sensors_output = power_sample[ValidationConsts.SENSORS_OUTPUT]
    if not sensors_output:
        get_sensors_data_func = getattr(cli_obj.performance, "get_sensors_data", None)
        if get_sensors_data_func:
            sensors_output = get_sensors_data_func()
    if not sensors_output:
        raise TestIssue("Sensors data was not collected by validator as expected by DVS/CL OS")
    return sensors_output


def _resolve_power_supply_name(chip_type, controller_name, block_text=None):
    """Map an I2C controller to its canonical display power-supply name.

    Chip-agnostic: every chip type resolves names from the declarative
    ``(bus, address)`` rail table (:class:`RailModel`), falling back to the raw
    controller name when the table has no match (e.g. a board rail not modeled).

    Args:
        chip_type (str): Chip identifier (e.g. ``"SPC6"``).
        controller_name (str): lm-sensors chip name (e.g. ``mp29816-i2c-16-60``).
        block_text (Optional[str]): Chip block text for the label fallback.

    Returns:
        str: Canonical/display supply name for the controller.
    """
    return resolve_rail_name(chip_type, controller_name, block_text) or controller_name


def _warn_if_unmapped_rail(chip_type, controller_name, supply_name):
    """Log a warning when a controller on an ASIC branch has no rail mapping.

    A controller on one of the chip's ASIC buses that the declarative table does
    not cover likely means a new or renamed ASIC rail. Surfacing it loudly keeps
    coverage gaps from being hidden. Board branches (intentionally not modeled)
    are ignored to avoid noise.

    Args:
        chip_type (str): Chip identifier (e.g. ``"SPC6"``).
        controller_name (str): lm-sensors chip name (e.g. ``mp29816-i2c-16-60``).
        supply_name (str): Resolved supply name; equals ``controller_name`` when
            no rail matched.
    """
    if supply_name != controller_name:
        return
    bus, _ = parse_i2c_bus_and_address(controller_name)
    if bus in RailModel.asic_buses(chip_type):
        logger.warning(f"Unmapped {chip_type} rail on ASIC bus i2c-{bus}: {controller_name} "
                       f"| add it to RailModel.RAILS_BY_CHIP_TYPE['{chip_type}'] so it is "
                       f"named and validated")


def get_power_dataframe(cli_obj, sensors_output, chip_type):
    """Parse ``sensors`` output into a per-channel (per-leg) power dataframe.

    Chip-agnostic: each ``*-i2c-<bus>-<addr>`` controller is resolved to its
    canonical rail name via the declarative :class:`RailModel` table, and every
    active channel becomes one row. Controllers on an ASIC branch with no table
    match are logged as coverage gaps (see :func:`_warn_if_unmapped_rail`).

    Args:
        cli_obj: CLI object of the DUT.
        sensors_output (str): Raw ``sensors`` output (may include many chips).
        chip_type (str): Chip identifier (e.g. ``"SPC6"``).

    Returns:
        pd.DataFrame: One row per active channel with columns
        ``powerSupply``, ``address``, ``voltage``, ``currentAmp``, ``powerWatt``,
        e.g. ``{"powerSupply": "HVDD", "address": "0x61", "voltage": 1.20,
        "currentAmp": 13.00, "powerWatt": 15.6}``.
    """
    controller_names_list = re.findall(PowerConsts.CONTROLLER_REGEX, sensors_output)
    controllers_info_dicts_list = cli_obj.performance.get_controllers_info_dicts_list(sensors_output)
    block_list = get_controllers_info_str_list(sensors_output)
    power_rows = []
    for controller_idx, controller_name in enumerate(controller_names_list):
        address_hex = normalized_i2c_address(controller_name)
        block_text = block_list[controller_idx] if controller_idx < len(block_list) else ""
        supply_name = _resolve_power_supply_name(chip_type, controller_name, block_text)
        _warn_if_unmapped_rail(chip_type, controller_name, supply_name)
        controller_info_dict = controllers_info_dicts_list[controller_idx]
        power_rows.extend(_build_rail_leg_rows(supply_name, address_hex or "na", controller_info_dict))
    return pd.DataFrame(power_rows)


def _build_rail_leg_rows(supply_name, address_display, controller_info_dict):
    """Build one power row per active controller channel (``vout1``/``vout2``).

    Args:
        supply_name (str): Canonical/display rail name for the controller.
        address_display (str): I2C address (``0x..``) or ``"na"`` for display.
        controller_info_dict (dict): Parsed per-controller values (``vout*``,
            ``iout*``/``pout*``).

    Returns:
        list[dict]: Zero, one or two per-channel power rows.
    """
    rows = []
    for index in (1, 2):
        voltage = controller_info_dict.get(f"vout{index}")
        if not voltage:
            continue
        current = _channel_current(index, controller_info_dict, voltage)
        if current is None:
            continue
        rows.append({
            PowerConsts.POWER_SUPPLY: supply_name,
            PowerConsts.POWER_SUPPLY_ADDRESS: address_display,
            PowerConsts.POWER_VOLTAGE: voltage,
            PowerConsts.POWER_CURRENT: current,
            PowerConsts.POWER_WATT: get_controller_power(index, controller_info_dict, voltage, current),
        })
    return rows


def _channel_current(index, controller_info_dict, voltage):
    """Return the channel current (A), derived from ``iout`` or ``pout/vout``.

    Args:
        index (int): Channel index (1 or 2).
        controller_info_dict (dict): Parsed per-controller values.
        voltage (float): Channel output voltage, used for the ``pout`` fallback.

    Returns:
        Optional[float]: Absolute current in amps, or ``None`` when the channel
        reports neither ``iout`` nor a usable ``pout``.
    """
    if f"iout{index}" in controller_info_dict:
        return abs(controller_info_dict[f"iout{index}"])
    if f"pout{index}" in controller_info_dict and voltage:
        return abs(controller_info_dict[f"pout{index}"] / voltage)
    return None


def get_controller_power(index, controller_info_dict, voltage, current):
    if controller_info_dict.get(f"pout{index}") is not None:
        power = controller_info_dict.get(f"pout{index}")
    else:
        power = abs(current * voltage)
    return abs(power)


def _power_total_by_domain(power_df, chip_type, domain):
    """Sum power for a single domain, excluding upstream feed rails.

    Domain is derived from the canonical rail name (ASIC names come from the
    table; everything else is Board). Upstream feeds (e.g. a PDB converter) are
    excluded to avoid double-counting the leaf rails they supply.

    Args:
        power_df (pd.DataFrame): Per-leg power stats.
        chip_type (str): Chip identifier (e.g. ``"SPC6"``).
        domain (str): Domain to total (see :class:`RailDomain`).

    Returns:
        float: Summed power in watts for the domain.
    """
    asic_names = RailModel.asic_canonical_names(chip_type)
    upstream_names = RailModel.upstream_feed_names(chip_type)

    def is_in_domain(supply_name):
        row_domain = RailDomain.ASIC if supply_name in asic_names else RailDomain.BOARD
        return row_domain == domain and supply_name not in upstream_names

    domain_mask = power_df[PowerConsts.POWER_SUPPLY].apply(is_in_domain)
    return power_df.loc[domain_mask, PowerConsts.POWER_WATT].sum()


def get_sum_power_df_by_collectors_group(power_df, chip_type):
    """Summarize power per rail group, split into ASIC and Board sections.

    Chip-agnostic (per-group summary report): legs are folded per canonical rail
    name (every leg sharing a name is summed, across all its controllers), each
    group is tagged with its domain, ASIC groups are ordered before Board groups,
    and per-domain subtotals plus a grand-total row are appended (the per-domain
    report and per-total report views).

    Args:
        power_df (pd.DataFrame): Per-leg power stats.
        chip_type (str): Chip identifier (e.g. ``"SPC6"``).

    Returns:
        pd.DataFrame: Sectioned summary with a domain column, per-domain subtotals
        and a grand-total row.
    """
    summary_df = _summarize_power_by_group(power_df, chip_type)
    return _append_domain_totals(summary_df, power_df, chip_type)


def _summarize_power_by_group(power_df, chip_type):
    """Fold per-leg power into one row per canonical rail, tagged with its domain.

    Args:
        power_df (pd.DataFrame): Per-leg power stats.
        chip_type (str): Chip identifier (e.g. ``"SPC6"``).

    Returns:
        pd.DataFrame: One row per canonical rail with columns ``powerSupply``,
        ``domain``, ``address`` (joined) and ``powerWatt``, ordered ASIC then Board.
    """
    asic_names = RailModel.asic_canonical_names(chip_type)
    group_rows = []
    for _, group_df in power_df.groupby(power_df[PowerConsts.POWER_SUPPLY], sort=False):
        name = group_df[PowerConsts.POWER_SUPPLY].iloc[0]
        addresses = ",".join(sorted(set(map(str, group_df[PowerConsts.POWER_SUPPLY_ADDRESS]))))
        domain = RailDomain.ASIC if name in asic_names else RailDomain.BOARD
        group_rows.append({
            PowerConsts.POWER_SUPPLY: name,
            PowerConsts.POWER_DOMAIN: domain,
            PowerConsts.POWER_SUPPLY_ADDRESS: addresses,
            PowerConsts.POWER_WATT: group_df[PowerConsts.POWER_WATT].sum(),
        })
    summary_df = pd.DataFrame(group_rows, columns=[PowerConsts.POWER_SUPPLY, PowerConsts.POWER_DOMAIN,
                                                   PowerConsts.POWER_SUPPLY_ADDRESS, PowerConsts.POWER_WATT])
    return summary_df.sort_values(
        [PowerConsts.POWER_DOMAIN, PowerConsts.POWER_SUPPLY]).reset_index(drop=True)


def _summary_total_rows(power_df, chip_type, present_domains):
    """Build the per-domain subtotal rows and the grand-total row.

    Args:
        power_df (pd.DataFrame): Per-leg power stats.
        chip_type (str): Chip identifier (e.g. ``"SPC6"``).
        present_domains (list): Domains that appear in the summary, so a subtotal
            is only emitted for domains that have rails.

    Returns:
        pd.DataFrame: A subtotal row per present domain (ASIC before Board) plus a
        grand-total row.
    """
    total_rows = []
    grand_total = 0.0
    for domain in (RailDomain.ASIC, RailDomain.BOARD):
        if domain not in present_domains:
            continue
        domain_total = _power_total_by_domain(power_df, chip_type, domain)
        grand_total += domain_total
        total_rows.append({PowerConsts.POWER_SUPPLY: f"{domain} Total", PowerConsts.POWER_DOMAIN: domain,
                           PowerConsts.POWER_SUPPLY_ADDRESS: None, PowerConsts.POWER_WATT: domain_total})
    total_rows.append({PowerConsts.POWER_SUPPLY: PowerConsts.TOTAL_POWER, PowerConsts.POWER_DOMAIN: None,
                       PowerConsts.POWER_SUPPLY_ADDRESS: None, PowerConsts.POWER_WATT: grand_total})
    return pd.DataFrame(total_rows)


def _annotate_rail_domain(power_df, chip_type):
    """Add a domain column to a per-leg power dataframe.

    Each leg is tagged ASIC or Board from its canonical rail name (ASIC names come
    from the declarative table; everything else is Board). The domain column is
    placed right after the power-supply column for readability.

    Args:
        power_df (pd.DataFrame): Per-leg power stats.
        chip_type (str): Chip identifier (e.g. ``"SPC6"``).

    Returns:
        pd.DataFrame: Copy of ``power_df`` with a domain column inserted after the
        power-supply column.
    """
    asic_names = RailModel.asic_canonical_names(chip_type)
    annotated_df = power_df.copy()
    annotated_df[PowerConsts.POWER_DOMAIN] = annotated_df[PowerConsts.POWER_SUPPLY].apply(
        lambda name: RailDomain.ASIC if name in asic_names else RailDomain.BOARD)
    ordered_cols = [PowerConsts.POWER_SUPPLY, PowerConsts.POWER_DOMAIN] + [
        col for col in annotated_df.columns
        if col not in (PowerConsts.POWER_SUPPLY, PowerConsts.POWER_DOMAIN)]
    return annotated_df[ordered_cols]


def _append_domain_totals(rows_df, power_df, chip_type):
    """Append per-domain subtotals and a grand-total row to a report frame.

    Shared by both report views so the per-rail measurement report and the
    per-group summary report carry identical totals (per-domain subtotals plus a
    grand total, both excluding upstream feeds).

    Args:
        rows_df (pd.DataFrame): Report rows already carrying a domain column
            (per-leg measurement rows or per-group summary rows).
        power_df (pd.DataFrame): Per-leg power stats used to compute the totals.
        chip_type (str): Chip identifier (e.g. ``"SPC6"``).

    Returns:
        pd.DataFrame: ``rows_df`` with per-domain subtotals and a grand-total row
        appended.
    """
    present_domains = list(rows_df[PowerConsts.POWER_DOMAIN].unique())
    total_rows = _summary_total_rows(power_df, chip_type, present_domains)
    return pd.concat([rows_df, total_rows], ignore_index=True)


def validate_power_df_by_collectors(power_df, asic_total_threshold, violations_list, chip_type):
    """Validate power at the rail-GROUP level, plus the ASIC-domain total.

    Chip-agnostic: every chip validates group budgets (per Arch's power design,
    thresholds are defined per rail group) and the ASIC-domain total. Group
    budgets come from the declarative rail table; the ASIC total budget is passed
    in. The per-leg dataframe is returned as the per-rail measurement report: each
    leg tagged with its domain, followed by per-domain subtotals and a grand-total
    row (same totals as the per-group summary report).

    Args:
        power_df (pd.DataFrame): Per-leg power stats.
        asic_total_threshold (Optional[float]): ASIC-domain total power budget in
            watts, or ``None`` to skip the total check.
        violations_list (list): Accumulator for threshold violations.
        chip_type (str): Chip identifier (e.g. ``"SPC6"``).

    Returns:
        pd.DataFrame: Per-leg dataframe with a domain column, per-domain subtotals
        and a grand-total row appended.
    """
    _validate_rail_group_power(power_df, chip_type, violations_list)
    _validate_asic_total_power(power_df, chip_type, asic_total_threshold, violations_list)
    per_rail_df = _annotate_rail_domain(power_df, chip_type)
    return _append_domain_totals(per_rail_df, power_df, chip_type)


def _validate_rail_group_power(power_df, chip_type, violations_list):
    """Validate each canonical rail's summed power against its table threshold.

    Legs are folded per canonical rail name (every leg sharing a name is summed,
    across all its controllers) and each rail's total is compared against its
    threshold. Rails without a defined threshold (e.g. SPC3, or Board rails) are
    skipped.

    Args:
        power_df (pd.DataFrame): Per-leg power stats.
        chip_type (str): Chip identifier (e.g. ``"SPC6"``).
        violations_list (list): Accumulator for threshold violations.
    """
    thresholds_by_rail = RailModel.power_thresholds_by_rail(chip_type)
    if not thresholds_by_rail:
        return
    for _, group_df in power_df.groupby(power_df[PowerConsts.POWER_SUPPLY], sort=False):
        rail_name = group_df[PowerConsts.POWER_SUPPLY].iloc[0]
        rail_threshold = thresholds_by_rail.get(rail_name)
        if rail_threshold is None:
            continue
        group_power = group_df[PowerConsts.POWER_WATT].sum()
        if group_power > rail_threshold:
            addresses = ",".join(sorted(set(map(str, group_df[PowerConsts.POWER_SUPPLY_ADDRESS]))))
            violations_list.append(f"Power for rail group {rail_name} ({addresses}): {group_power} W was higher "
                                   f"than group threshold {rail_threshold} W, please check table "
                                   f"\"Power summary by collectors\" in allure attachments")


def _validate_asic_total_power(power_df, chip_type, asic_total_threshold, violations_list):
    """Validate the ASIC-domain total against the ASIC total budget.

    Only the ASIC domain is summed, so Board and upstream feed rails never
    inflate the ASIC budget. For legacy chips whose rails are all ASIC-domain
    this equals the full measured total.

    Args:
        power_df (pd.DataFrame): Per-leg power stats.
        chip_type (str): Chip identifier (e.g. ``"SPC6"``).
        asic_total_threshold (Optional[float]): ASIC-domain total budget in watts,
            or ``None`` to skip the check.
        violations_list (list): Accumulator for threshold violations.
    """
    power_total_th = asic_total_threshold
    if power_total_th is None:
        return
    asic_total_power = _power_total_by_domain(power_df, chip_type, RailDomain.ASIC)
    if asic_total_power > power_total_th:
        violations_list.append(f"ASIC total power {asic_total_power} W was higher than total power threshold "
                               f"{power_total_th} W, please check table \"Power full dataframe\" in allure attachments")
