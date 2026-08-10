"""Offline unit tests for the Portia device model matrix."""

import sys
from types import ModuleType

import pytest

from ngts.nvos_tools.Devices.cpo.CpoTopology import CpoCapable, CpoTopology, is_cpo_capable

PORTIA_NVL72_HW_KEY = "N7200_LD - Portia"
PORTIA_NVL72_SIMX_KEY = "N7200_LD_simx - Portia"
PORTIA_CPO_HW_KEY = "N7300_LD - Portia"
PORTIA_CPO_SIMX_KEY = "N7300_LD_simx - Portia"
PORTIA_CPO_TA_SIMX_KEY = "N7300_LD_TA_simx - Portia"
PORTIA_NPO_HW_KEY = "N7400_LD - Portia"
PORTIA_NPO_SIMX_KEY = "N7400_LD_simx - Portia"
PORTIA_NPO_TA_SIMX_KEY = "N7400_LD_TA_simx - Portia"
NVL72_INTERNAL_PORTS = 576


@pytest.fixture(autouse=True)
def switch_env_vars(monkeypatch):
    """BaseSwitch.__init__ reads switch credentials from the environment."""
    from devts.infra.tools.redmine import redmine_api

    from ngts.tests_nvos.helpers import redmine_helpers

    monkeypatch.setenv("NVU_SWITCH_NEW_PASSWORD", "dummy")
    monkeypatch.setenv("NVU_SWITCH_USER", "dummy")
    monkeypatch.setenv("NVU_SWITCH_PASSWORD", "dummy")
    monkeypatch.setattr(redmine_api, "is_redmine_issue_active", lambda issues: [False] * len(issues))
    monkeypatch.setattr(redmine_helpers, "is_bug_active", lambda _issue: False)
    fake_air = ModuleType("devts.infra.tools.nvidia_air_tools.air")
    fake_air.get_dhcp_ips_dict = lambda *_args, **_kwargs: {}
    fake_air.get_setup_devices_from_air = lambda *_args, **_kwargs: []
    fake_air.get_simulation_ports_from_air = lambda *_args, **_kwargs: []
    fake_air.get_player_data_from_air = lambda *_args, **_kwargs: {}
    monkeypatch.setitem(sys.modules, fake_air.__name__, fake_air)
    monkeypatch.setitem(sys.modules, "infra.tools.nvidia_air_tools.air", fake_air)


@pytest.mark.parametrize(
    ("switch_type", "expected"),
    [
        (PORTIA_NVL72_HW_KEY, ("PortiaSwitch", 4, "N7200_LD", "920-9K51W-02L7-GS0", False)),
        (PORTIA_NVL72_SIMX_KEY, ("PortiaSimx", 4, "N7200_LD", "920-9K51W-02L7-GS0", True)),
        (PORTIA_CPO_HW_KEY, ("PortiaCpoSwitch", 8, "N7300_LD", "920-9K57P-00L7-GS0", False)),
        (PORTIA_CPO_SIMX_KEY, ("PortiaCpoSimx", 8, "N7300_LD", "920-9K57P-00L7-GS0", True)),
        (PORTIA_CPO_TA_SIMX_KEY, ("PortiaCpoTASimx", 2, "N7300_LD_TA", "920-9K57P-00L7-GS0", True)),
        (PORTIA_NPO_HW_KEY, ("PortiaNpoSwitch", 8, "N7400_LD", "920-9K57E-00L7-GS0", False)),
        (PORTIA_NPO_SIMX_KEY, ("PortiaNpoSimx", 8, "N7400_LD", "920-9K57E-00L7-GS0", True)),
        (PORTIA_NPO_TA_SIMX_KEY, ("PortiaNpoTASimx", 2, "N7400_LD_TA", "920-9K57E-00L7-GS0", True)),
    ],
)
def test_factory_resolves_portia_models(switch_type, expected):
    from ngts.nvos_tools.Devices.DeviceFactory import DeviceFactory

    class_name, asic_amount, system_type, model, is_simx = expected
    switch = DeviceFactory.create_device(switch_type)
    assert switch.__class__.__name__ == class_name
    assert switch.asic_amount == asic_amount
    assert switch.is_simx is is_simx
    assert switch.require_mloop_setup is is_simx
    assert switch.show_platform_output["system-type"] == system_type
    assert switch.platform_inventory_switch_values["model"].regex.pattern == model


def test_simx_classes_are_thin_overlays_of_real_models():
    from ngts.nvos_tools.Devices.IbDevice import (
        PortiaCpoSimx,
        PortiaCpoSwitch,
        PortiaCpoTASimx,
        PortiaNpoSimx,
        PortiaNpoSwitch,
        PortiaNpoTASimx,
        PortiaSimx,
        PortiaSwitch,
        SimxDevice,
    )

    assert PortiaSimx.__bases__ == (SimxDevice, PortiaSwitch)
    assert PortiaCpoSimx.__bases__ == (SimxDevice, PortiaCpoSwitch)
    assert PortiaCpoTASimx.__bases__ == (SimxDevice, PortiaCpoSwitch)
    assert PortiaNpoSimx.__bases__ == (SimxDevice, PortiaNpoSwitch)
    assert PortiaNpoTASimx.__bases__ == (SimxDevice, PortiaNpoSwitch)


@pytest.mark.parametrize("model", ["N7300_LD_TA", "N7400_LD_TA"])
def test_ta_models_are_simx_only(model):
    from ngts.nvos_tools.Devices.DeviceFactory import DeviceFactory

    matching_keys = [key for key in DeviceFactory.device_type_dict if key.startswith(model)]
    assert matching_keys == [f"{model}_simx - Portia"]


@pytest.mark.parametrize("switch_type", [PORTIA_NVL72_HW_KEY, PORTIA_NVL72_SIMX_KEY])
def test_nvl72_non_expandable_port_model(switch_type):
    from ngts.nvos_tools.Devices.DeviceFactory import DeviceFactory

    switch = DeviceFactory.create_device(switch_type)
    assert len(switch.nvl_access_ports_list) == NVL72_INTERNAL_PORTS
    assert switch.nvl_trunk_ports_list == []
    assert not is_cpo_capable(switch)


def test_nvl72_has_two_internal_fnm_ports_per_asic():
    from ngts.nvos_tools.Devices.DeviceFactory import DeviceFactory

    switch = DeviceFactory.create_device(PORTIA_NVL72_HW_KEY)
    assert switch.nvl_internal_fnm_ports == [
        f"fnma{asic}p{port}" for asic in range(4) for port in range(1, 3)
    ]


@pytest.mark.parametrize(
    ("switch_type", "asic_amount"),
    [
        (PORTIA_CPO_HW_KEY, 8),
        (PORTIA_CPO_SIMX_KEY, 8),
        (PORTIA_CPO_TA_SIMX_KEY, 2),
    ],
)
def test_cpo_port_and_optics_model(switch_type, asic_amount):
    from ngts.nvos_tools.Devices.DeviceFactory import DeviceFactory

    switch = DeviceFactory.create_device(switch_type)
    assert isinstance(switch, CpoCapable)
    assert isinstance(switch.cpo, CpoTopology)
    assert switch.cpo.cpo_count == asic_amount
    assert switch.cpo_list == switch.cpo.cpo_names()
    assert switch.laser_source_list == switch.cpo.els_names()
    assert switch.oe_list == switch.cpo.oe_names()
    # els_list is the Gen1 (Taipan) discriminator - test_ib_show_interface and the
    # els_fiber_tuning conftest select on it, so Gen2 must never define it.
    assert not hasattr(switch, "els_list")
    assert len(switch.nvl_access_ports_list) == 72 * asic_amount
    assert len(switch.nvl_trunk_ports_list) == 64 * asic_amount
    assert switch.nvl_trunk_ports_list[-1] == f"sw{8 * asic_amount}p1s8"
    assert switch.nvl_trunk_port_speed == "200G"
    assert switch.nvl_internal_fnm_ports == [
        f"fnma{asic}p{port}" for asic in range(asic_amount) for port in range(1, 3)
    ]


@pytest.mark.parametrize(
    ("switch_type", "asic_amount"),
    [
        (PORTIA_NPO_HW_KEY, 8),
        (PORTIA_NPO_SIMX_KEY, 8),
        (PORTIA_NPO_TA_SIMX_KEY, 2),
    ],
)
def test_npo_port_model_is_not_cpo_capable(switch_type, asic_amount):
    from ngts.nvos_tools.Devices.DeviceFactory import DeviceFactory

    switch = DeviceFactory.create_device(switch_type)
    assert not is_cpo_capable(switch)
    assert len(switch.nvl_access_ports_list) == 72 * asic_amount
    assert len(switch.nvl_trunk_ports_list) == 64 * asic_amount
    assert switch.nvl_trunk_ports_list[-1] == f"sw{8 * asic_amount}p1s8"
    assert switch.nvl_trunk_port_speed == "200G"
    assert switch.nvl_internal_fnm_ports == [
        f"fnma{asic}p{port}" for asic in range(asic_amount) for port in range(1, 3)
    ]


@pytest.mark.parametrize(
    ("switch_type", "asic_amount"),
    [
        (PORTIA_NVL72_HW_KEY, 4),
        (PORTIA_NVL72_SIMX_KEY, 4),
        (PORTIA_CPO_HW_KEY, 8),
        (PORTIA_CPO_SIMX_KEY, 8),
        (PORTIA_CPO_TA_SIMX_KEY, 2),
        (PORTIA_NPO_HW_KEY, 8),
        (PORTIA_NPO_SIMX_KEY, 8),
        (PORTIA_NPO_TA_SIMX_KEY, 2),
    ],
)
def test_sensor_inventory_scales_with_asic_amount(switch_type, asic_amount):
    from ngts.nvos_tools.Devices.DeviceFactory import DeviceFactory

    switch = DeviceFactory.create_device(switch_type)
    expected_asics = [f"ASIC{asic}" for asic in range(1, asic_amount + 1)]
    assert [sensor for sensor in switch.temperature_sensors if sensor.startswith("ASIC")] == expected_asics
    for asic in range(1, asic_amount + 1):
        assert any(f"-ASIC{asic}-VDD-Out-1" in sensor for sensor in switch.voltage_sensors)
    assert not any(f"-ASIC{asic_amount + 1}-" in sensor for sensor in switch.voltage_sensors)
