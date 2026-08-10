import re


NVUE_PHYSICAL_PORTS_BY_ASIC = {
    "Spectrum-5": 66,
    "Spectrum-6": 64,
}


def get_nvue_physical_ports_count(asic_model, port_layout):
    """Return the physical port count reported to NVUE performance flows.

    Args:
        asic_model: NVUE ASIC model name.
        port_layout: Platform port-layout string used for other ASICs.

    Returns:
        Corrected physical count for SPC5/SPC6, otherwise the parsed layout sum.
    """
    if asic_model in NVUE_PHYSICAL_PORTS_BY_ASIC:
        return NVUE_PHYSICAL_PORTS_BY_ASIC[asic_model]
    return sum(int(count) for count in re.findall(r"(\d+) x", port_layout))


def get_nvue_data_physical_ports_count(asic_model, physical_ports, bonus_ports_count):
    """Return usable front-panel ports after accounting for service ports.

    Args:
        asic_model: NVUE ASIC model name.
        physical_ports: Corrected physical port count.
        bonus_ports_count: Number of ASIC bonus/service interface names.

    Returns:
        Number of usable front-panel data ports.
    """
    if asic_model == "Spectrum-6":
        return physical_ports
    return physical_ports - bonus_ports_count


def get_nvue_expected_nexthops(data_ports, split_left, split_right):
    """Return the common SPC5/SPC6 expected neighbor count.

    Args:
        data_ports: Number of usable physical data ports.
        split_left: Left-side breakout factor.
        split_right: Right-side breakout factor.

    Returns:
        Expected IPv4/IPv6 neighbor count used by the readiness wait.
    """
    return data_ports * (int(split_left) + int(split_right))
