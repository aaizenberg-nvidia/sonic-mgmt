"""Parse ``sensors`` output for performance power validation.

This module stays free of CLI wrapper imports so NVUE and SONiC paths can share
logic without import cycles (e.g. ``nvue`` → ``sonic`` → ``srv6`` → ``nvue_cli``).
"""

import re

from infra.tools.exceptions.test_issue import TestIssue
from ngts.constants.performance_constants import PowerConsts, RailModel


def get_controllers_info_str_list(sensors_output):
    """Split full ``sensors`` text into per-controller blocks (text after each controller id).

    ``re.split(CONTROLLER_REGEX, text)`` yields one leading segment (preamble before the first
    chip name) then one segment per ``re.findall`` chip, in order. We drop the preamble so the
    returned list aligns index-wise with ``re.findall(PowerConsts.CONTROLLER_REGEX, ...)``.
    """
    parts = re.split(PowerConsts.CONTROLLER_REGEX, sensors_output)
    if not parts or len(parts) <= 1:
        raise TestIssue("No controller blocks found in sensors output")
    return parts[1:]


def normalized_i2c_address(controller_name):
    """Return ``0x..`` I2C address from an lm-sensors chip name (``*-i2c-<bus>-<addr>``)."""
    if not controller_name:
        return None
    name = controller_name.strip()
    address_match = re.search(r'-i2c-\d+-([0-9a-fA-F]+)\s*$', name)
    if not address_match:
        return None
    return str(hex(int(address_match.group(1), 16)))


def parse_i2c_bus_and_address(controller_name):
    """Return ``(bus, address)`` from an lm-sensors chip name (``*-i2c-<bus>-<addr>``).

    Unlike :func:`normalized_i2c_address`, this keeps the i2c bus so callers can
    tell branches apart and separate ASIC buses from board buses that reuse the
    same address (e.g. ``0x60`` on i2c-16 is VDD_main, on i2c-6/7 it is a board
    PDB converter).

    Args:
        controller_name (str): lm-sensors chip name (e.g. ``mp29816-i2c-16-60``).

    Returns:
        Tuple[Optional[int], Optional[str]]: ``(bus, '0x..')``, or ``(None, None)``
        when the name has no ``-i2c-<bus>-<addr>`` suffix.
    """
    if not controller_name:
        return None, None
    # Bus is decimal, address hex without "0x": "mp29816-i2c-16-61" -> bus 16, 0x61.
    # Anchored to the end because driver names carry their own digits and hyphens
    # (e.g. "raa228004-i2c-7-60"), so only the trailing pair identifies the controller.
    match = re.search(r'-i2c-(\d+)-([0-9a-fA-F]+)\s*$', controller_name.strip())
    if not match:
        return None, None
    bus = int(match.group(1))
    address = str(hex(int(match.group(2), 16)))
    return bus, address


def _match_rail_by_label(chip_type, block_text):
    """Return the first rail of ``chip_type`` whose ``label_regex`` matches text.

    Fallback for descriptive/bench ``sensors`` dumps that expose rail tokens
    instead of an i2c chip name.

    Args:
        chip_type (str): Chip identifier such as ``"SPC6"``.
        block_text (str): Text chunk for one chip block.

    Returns:
        Optional[RailDef]: Matching rail definition, or ``None`` when no labeled
        rail matches.
    """
    for rail in RailModel.rails_for_chip_type(chip_type):
        if rail.label_regex and re.search(rail.label_regex, block_text):
            return rail
    return None


def resolve_rail_name(chip_type, controller_name, block_text=None):
    """Resolve a controller to its canonical rail name via the RailModel table.

    Chip-agnostic. The primary key is the ``(i2c bus, address)`` pair (with
    address-only matching for single-bank legacy chips whose rails set
    ``buses=()``). When that misses, an optional label-regex fallback covers
    descriptive/bench dumps.

    Args:
        chip_type (str): Chip identifier such as ``"SPC6"``.
        controller_name (str): lm-sensors chip name (e.g. ``mp29816-i2c-16-60``).
        block_text (Optional[str]): Chip block text used for the label fallback.

    Returns:
        Optional[str]: Canonical rail name, or ``None`` when nothing matches (the
        caller decides the fallback display name).
    """
    bus, address = parse_i2c_bus_and_address(controller_name)
    rail = RailModel.get_rail_def(chip_type, bus, address)
    if rail is None and block_text:
        rail = _match_rail_by_label(chip_type, block_text)
    return rail.canonical_name if rail else None


def parse_sensor_line(line):
    """Parse one line of sensor output into ``(channel, value_str, unit_str)`` or ``None``."""
    # Compact lm-sensors line: ``vout1: 12.0 V`` / ``iin: 500 mA`` / ``pout2: 1.2 W``
    # Group 1: measure prefix (v/i/p), group 2: channel (outN or in),
    # Group 3: numeric value, group 4: unit (V/A/W or mV/mA/mW).
    compact_match = re.search(
        r"([vip])(out\d+|in):\s+(-?\d*\.?\d+)\s+(m?[VAW])", line)
    if compact_match:
        channel = compact_match.group(2)
        value = compact_match.group(3)
        unit = compact_match.group(4)
        if channel == "in":
            # lm-sensors uses ``in`` for the primary rail; power helpers expect ``out1``.
            channel = "out1"
        return channel, value, unit

    # Parenthesized voltage line: ``Volt (out1): 1200 mV`` or ``Volt (in): 12.0 V``
    # Group 1: channel (in/out/outN), group 2: value, group 3: unit (V or mV).
    volt_paren = re.search(
        r"Volt\s*\((in|out\d+|out)\):\s+(-?\d*\.?\d+)\s+(m?V)\b", line, re.I)
    if volt_paren:
        channel = volt_paren.group(1).lower()
        value = volt_paren.group(2)
        unit = volt_paren.group(3)
        if channel in ("in", "out"):
            channel = "out1"
        return channel, value, unit

    curr_paren = re.search(
        r"Curr\s*\((in\d+|out\d+|out|in)\):\s+(-?\d*\.?\d+)\s+(m?A)\b", line, re.I)
    if curr_paren:
        channel = curr_paren.group(1).lower()
        value = curr_paren.group(2)
        unit = curr_paren.group(3)
        if channel == 'out':
            channel = 'out1'
        return channel, value, unit

    pwr_paren = re.search(
        r"Pwr\s*\((in\d+|out\d+|out|in)\):\s+(-?\d*\.?\d+)\s+(m?W)\b", line, re.I)
    if pwr_paren:
        channel = pwr_paren.group(1).lower()
        value = pwr_paren.group(2)
        unit = pwr_paren.group(3)
        if channel == 'out':
            channel = 'out1'
        return channel, value, unit

    labeled = re.search(
        r".*?\b(Rail|Curr|Pwr)\s*\((in\d+|out\d+|out|in)\):\s+(-?\d*\.?\d+)\s+(mV|mW|mA|V|A|W)\b",
        line, re.I)
    if labeled:
        metric = labeled.group(1).lower()
        channel = labeled.group(2).lower()
        value = labeled.group(3)
        unit = labeled.group(4)
        if channel == 'out':
            channel = 'out1'
        elif channel == 'in' and metric == 'volt':
            channel = 'out1'
        return channel, value, unit

    return None


def get_sensors_output_key(key, measure_unit):
    """Map parsed channel + unit to dict keys used by power helpers (``vout1``, ``iout1``, …)."""
    unit_lower = measure_unit.lower()
    if unit_lower in ('mv', 'v'):
        return f"v{key}"
    if unit_lower in ('ma', 'a'):
        return f"i{key}"
    if unit_lower in ('mw', 'w'):
        return f"p{key}"
    raise TestIssue(f"Unrecognized measure unit {measure_unit} in sensors output parsing")


def build_controllers_info_dicts_list(sensors_output):
    """Return one dict per I2C controller with ``vout*`` / ``iout*`` / ``pout*`` floats."""
    controllers_info_dicts_list = []
    controller_names_list = re.findall(PowerConsts.CONTROLLER_REGEX, sensors_output)
    controllers_info_str_list = get_controllers_info_str_list(sensors_output)
    for idx, controller_info_str in enumerate(controllers_info_str_list):
        controller_name = controller_names_list[idx]
        is_controller_name_vddscc = 'i2c-5-6e' in controller_name
        controller_info_list = controller_info_str.splitlines()
        if controller_info_list:
            controllers_info_dict = {}
            for controller_info in controller_info_list:
                parsed = parse_sensor_line(controller_info)
                if not parsed:
                    continue
                key, value, measure_unit = parsed
                key = get_sensors_output_key(key, measure_unit)
                scale = 'm' in measure_unit.lower()
                val = float(value)
                if is_controller_name_vddscc and '1' in key:
                    controllers_info_dict[key] = val / 1000 if scale else val
                elif not is_controller_name_vddscc:
                    controllers_info_dict[key] = val / 1000 if scale else val
            controllers_info_dicts_list.append(controllers_info_dict)
    return controllers_info_dicts_list
