import ipaddress
import re
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader


TEMPLATES_ROOT = Path(__file__).resolve().parents[3] / "performance_tests" / "performance_config_templates"
ALIASES = ("dut", "left-tg", "right-tg")


def _sort_ports(ports):
    return sorted(ports, key=lambda port: tuple(int(value) for value in re.findall(r"\d+", port)))


def _parent_ports(ports):
    return _sort_ports(set(re.sub(r"s\d+$", "", port) for port in ports))


def _split_ports(ports, split):
    parents = _parent_ports(ports)
    if int(split) == 1:
        return parents
    return [f"{port}s{lane}" for port in parents for lane in range(int(split))]


def _generate_addresses(address_start_v4="1.1.1.1", address_start_v6="1::1", mode="dual",
                        number_of_address=1, step_v6=None, **kwargs):
    del kwargs
    count = int(number_of_address)
    step = int(step_v6, 0) if step_v6 else 1
    v4 = [str(ipaddress.IPv4Address(address_start_v4) + index) for index in range(count)]
    v6 = [str(ipaddress.IPv6Address(address_start_v6) + index * step) for index in range(count)]
    return {"v4": v4, "v6": v6} if mode == "dual" else v6


def _calculate_address(address, operation, step, operand):
    value = int(ipaddress.ip_address(address))
    delta = int(step, 0) * int(operand)
    return str(ipaddress.ip_address(value + delta if operation == "add" else value - delta))


def _render_template(scenario, alias, chip_type, split, pre_split):
    left_parents = [f"swp{index}" for index in range(1, 33)]
    right_parents = [f"swp{index}" for index in range(33, 65)]
    if pre_split:
        left_ports = [f"{port}s{lane}" for port in left_parents for lane in range(2)]
        right_ports = [f"{port}s{lane}" for port in right_parents for lane in range(2)]
    else:
        left_ports = left_parents
        right_ports = right_parents

    environment = Environment(loader=FileSystemLoader(TEMPLATES_ROOT / scenario / "cumulus_jinja"))
    template = environment.get_template(f"{alias}.yaml.jinja")
    template.globals.update({
        "get_right_left_ports_dict": lambda bring_up_ports=False: {
            "left_ports": left_ports,
            "right_ports": right_ports,
        },
        "get_full_right_left_ports_dict": lambda bring_up_ports=False: {
            "left_ports": left_ports,
            "right_ports": right_ports,
        },
        "filter_ports": lambda neighbors: {"dut": left_ports},
        "down_ports": lambda: right_ports,
        "generate_ip_address_list": _generate_addresses,
        "address_calculator": _calculate_address,
        "cumulus_ports_already_logical_split": (
            lambda ports: bool(ports) and all(re.match(r"^swp\d+s\d+$", port) for port in ports)
        ),
        "cumulus_ports_match_requested_split": (
            lambda ports, requested_split: (
                bool(ports) and
                all(re.match(r"^swp\d+s\d+$", port) for port in ports) and
                max(int(re.search(r"s(\d+)$", port).group(1)) for port in ports) + 1 ==
                int(requested_split)
            )
        ),
        "sort_swp_split_port_names": _sort_ports,
        "get_swp_parent_port_names": _parent_ports,
        "get_swp_ports_for_split": _split_ports,
        "validate_no_unsupported_service_port_split": lambda ports, requested_split, context: True,
        "validate_no_overlapping_swp_parent_ports": lambda first, second, context: True,
        "port_selection_active": lambda: False,
        "list_index": lambda sequence, item: list(sequence).index(item),
    })
    parameters = {
        "split_left": split,
        "split_right": split,
        "total_ports": 64,
        "dut_left_ports_num": 32 * split,
        "speed": "800000000" if split == 2 else "200000000",
        "two_sided_ar": True,
        "link_auto_negotiate": True,
        "link_phy_autoneg": None,
        "link_phy_speed": "200G" if split == 8 else None,
        "chip_type": chip_type,
    }
    return yaml.safe_load(template.render(parameter_dict=parameters))


@pytest.mark.parametrize("scenario", ["spcx_ra", "srv6"])
@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize("split", [2, 4, 8])
def test_spc6_presplit_templates_keep_x2_and_render_x8(scenario, alias, split):
    """Verify SPC6 reuses default x2 children and always emits parent breakouts.

    ``nv config replace`` wipes prior breakout, so parent ``breakout: Nx`` must
    stay in the rendered YAML even when ports already look pre-split.
    """
    data = _render_template(scenario, alias, "SPC6", split, pre_split=True)
    interfaces = data[1]["set"]["interface"]
    children = [name for name in interfaces if re.match(r"^swp\d+s\d+$", name)]
    parent_entries = [name for name in interfaces if re.match(r"^swp\d+$", name)]

    assert len(children) == 64 * split
    assert len(parent_entries) == 64
    assert data[1]["set"]["system"]["wjh"]["state"] == "disabled"


@pytest.mark.parametrize("scenario", ["spcx_ra", "srv6"])
@pytest.mark.parametrize("alias", ALIASES)
def test_spc5_breakout_1x_ports_still_emit_requested_2x_breakout(scenario, alias):
    """SPC4/5 init leaves ``swpNs0`` (1x). Requesting 2x must expand and break out."""
    left_ports = [f"swp{index}s0" for index in range(1, 33)]
    right_ports = [f"swp{index}s0" for index in range(33, 65)]
    environment = Environment(loader=FileSystemLoader(TEMPLATES_ROOT / scenario / "cumulus_jinja"))
    template = environment.get_template(f"{alias}.yaml.jinja")
    template.globals.update({
        "get_right_left_ports_dict": lambda bring_up_ports=False: {
            "left_ports": left_ports,
            "right_ports": right_ports,
        },
        "get_full_right_left_ports_dict": lambda bring_up_ports=False: {
            "left_ports": left_ports,
            "right_ports": right_ports,
        },
        "filter_ports": lambda neighbors: {"dut": left_ports},
        "down_ports": lambda: right_ports,
        "generate_ip_address_list": _generate_addresses,
        "address_calculator": _calculate_address,
        "cumulus_ports_already_logical_split": (
            lambda ports: bool(ports) and all(re.match(r"^swp\d+s\d+$", port) for port in ports)
        ),
        "cumulus_ports_match_requested_split": (
            lambda ports, requested_split: (
                bool(ports) and
                all(re.match(r"^swp\d+s\d+$", port) for port in ports) and
                max(int(re.search(r"s(\d+)$", port).group(1)) for port in ports) + 1 ==
                int(requested_split)
            )
        ),
        "sort_swp_split_port_names": _sort_ports,
        "get_swp_parent_port_names": _parent_ports,
        "get_swp_ports_for_split": _split_ports,
        "validate_no_unsupported_service_port_split": lambda ports, requested_split, context: True,
        "validate_no_overlapping_swp_parent_ports": lambda first, second, context: True,
        "port_selection_active": lambda: False,
        "list_index": lambda sequence, item: list(sequence).index(item),
    })
    parameters = {
        "split_left": 2,
        "split_right": 2,
        "total_ports": 64,
        "dut_left_ports_num": 64,
        "speed": "400000000",
        "two_sided_ar": True,
        "link_auto_negotiate": False,
        "link_phy_autoneg": None,
        "link_phy_speed": None,
        "chip_type": "SPC5",
    }
    data = yaml.safe_load(template.render(parameter_dict=parameters))
    interfaces = data[1]["set"]["interface"]
    children = [name for name in interfaces if re.match(r"^swp\d+s\d+$", name)]
    parent_entries = [name for name in interfaces if re.match(r"^swp\d+$", name)]
    assert "swp1s1" in children or "swp33s1" in children
    assert len(parent_entries) == 64
    assert interfaces[parent_entries[0]]["link"]["breakout"] == {"2x": {}}


@pytest.mark.parametrize("scenario", ["spcx_ra", "srv6"])
@pytest.mark.parametrize("alias", ALIASES)
def test_spc5_wjh_behavior_is_unchanged(scenario, alias):
    """Verify SPC6 WJH changes do not alter SPC5 rendered configuration."""
    data = _render_template(scenario, alias, "SPC5", 2, pre_split=False)
    wjh = data[1]["set"]["system"].get("wjh")

    if scenario == "srv6" or alias == "dut":
        assert wjh["state"] == "enabled"
    else:
        assert wjh is None
