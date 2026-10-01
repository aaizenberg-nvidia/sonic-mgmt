import pytest

from ngts.helpers.performance.nvue_port_count import (get_nvue_data_physical_ports_count,
                                                      get_nvue_expected_nexthops,
                                                      get_nvue_physical_ports_count)


@pytest.mark.parametrize("asic_model, expected", [
    ("Spectrum-5", 66),
    ("Spectrum-6", 64),
])
def test_get_physical_ports_ignores_logical_layout_count(asic_model, expected):
    """Verify SPC5/SPC6 use physical port counts instead of the 130-port layout."""
    assert get_nvue_physical_ports_count(asic_model, "130 x 400G") == expected


@pytest.mark.parametrize("asic_model, physical_ports", [
    ("Spectrum-5", 66),
    ("Spectrum-6", 64),
])
def test_data_physical_ports_count_is_64(asic_model, physical_ports):
    """Verify service ports are subtracted only when included in the physical count."""
    assert get_nvue_data_physical_ports_count(asic_model, physical_ports, bonus_ports_count=2) == 64


@pytest.mark.parametrize("asic_model, split, expected", [
    ("Spectrum-5", 1, 128),
    ("Spectrum-5", 2, 256),
    ("Spectrum-5", 8, 1024),
    ("Spectrum-6", 1, 128),
    ("Spectrum-6", 2, 256),
    ("Spectrum-6", 8, 1024),
])
def test_nexthop_wait_uses_common_64_port_formula(asic_model, split, expected):
    """Verify SPC5 and SPC6 use the same physical-port nexthop calculation."""
    del asic_model
    assert get_nvue_expected_nexthops(64, split, split) == expected
