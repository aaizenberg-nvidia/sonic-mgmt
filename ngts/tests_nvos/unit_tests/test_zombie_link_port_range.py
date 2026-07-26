"""Guard the documented ZombieLinkManager.build_port_range output.

NVUE's ``interface-name`` grammar rejects concatenated switch-prefix
groups on Crocodile (``swA...swB...`` must be ``swA...,swB...``). This
regressed once already; keep the per-platform outputs pinned.
"""
from types import SimpleNamespace

import pytest

from ngts.tests_nvos.interfaces.zombie_link.managers import ZombieLinkManager


def _mgr(group_separator=''):
    m = ZombieLinkManager.__new__(ZombieLinkManager)  # bypass __init__: only .dut is used
    m.dut = SimpleNamespace(port_range_group_separator=group_separator)
    return m


@pytest.mark.parametrize("ports,expected", [
    ([f"acp{i}" for i in range(1, 73)], "acp1-72"),                                          # NVL5/NVL6
    ([f"sw{i}p1" for i in range(1, 145)], "sw1-144p1"),                                      # Taipan
    ([f"sw{i}p{j}" for i in range(1, 73) for j in (1, 2)], "sw1-72p1-2"),                    # Black Mamba
])
def test_build_port_range_single_group(ports, expected):
    assert _mgr().build_port_range(ports) == expected


def test_build_port_range_crocodile_uses_comma_separator():
    ports = [f"sw{L}{i}p{j}" for L in ("A", "B") for i in range(1, 19) for j in (1, 2)]
    assert _mgr(group_separator=',').build_port_range(ports) == "swA1-18p1-2,swB1-18p1-2"
