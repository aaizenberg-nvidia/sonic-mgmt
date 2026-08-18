import pytest

from ngts.nvos_constants.constants_nvos import ChassisLocationConsts
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RegressionConfigurations import Configurations
from ngts.nvos_tools.platform.Platform import Platform


@pytest.fixture(scope="module")
def chassis_info():
    """Get chassis serial number and slot number from platform."""
    platform = Platform()
    chassis_location = OutputParsingTool.parse_show_output_to_dict(
        platform.chassis_location.show()).get_returned_value()
    return {
        'slot-number': chassis_location.get(ChassisLocationConsts.SLOT_NUM),
        'chassis-sn': chassis_location.get(ChassisLocationConsts.CHAS_SN),
    }


@pytest.fixture(scope="module")
def tray_topology_config(setup_name):
    """
    Per-setup tray model from RegressionConfigurations.tray_topology.

    Missing setup_name: pytest.skip (not a failure). Tests that need a lab model
    must list the setup there before running.
    """
    tray_info = Configurations.tray_topology.get(setup_name)
    if not tray_info:
        pytest.skip(f"No tray topology defined for setup '{setup_name}'")
    return tray_info
