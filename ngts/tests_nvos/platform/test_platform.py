import logging

import pytest

from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool


cumulus_owner = "hiept"


logger = logging.getLogger(__name__)


@pytest.mark.platform
@pytest.mark.cumulus
@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
@pytest.mark.nvos_ci
@pytest.mark.air
def test_show_platform(engines, random_api, devices, nv_command, update_platform_expected_values):
    """
    Validates the output of nv show platform.
    The OpenAPI test checks the JSON output while the NVUE test checks the auto output.
    Test flow:
        1. nv show platform (json output for OpenAPI test, auto output for NVUE test)
        2. Parse output to dict
        3. Validate all keys (field names) exist and there are no extra keys
        4. Validate all values are correct
    """
    output = OutputParsingTool.parse_show_output_to_dict(nv_command.platform.show()).get_returned_value()
    ValidationTool.validate_output_of_show(output, TestToolkit.get_device().show_platform_output).verify_result()
