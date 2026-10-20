import logging
import pytest
import random

from ngts.nvos_constants.constants_nvos import ApiType, LinkDetectionConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.mark.interfaces
@pytest.mark.parametrize('fec_mode', LinkDetectionConsts.FEC_MODES)
def test_set_interface_link_fec_mode(engines, fec_mode):
    """
    Configure ib0 interface link fec and verify the configuration applied successfully
    Steps:
    1. Select a random active port
    2. Verify fec mode is default by running “nv show interface <intf>”
    3. Verify fec mode is default by running “nv show interface <intf> link”
    4. Set port link fec mode by running "nv set interface <intf> fec <fec-mode>
    5. Verify the configuration applied by running “nv show interface <intf>”
    6. Verify the configuration applied by running “nv show interface <intf> link”
    7. Unset port link fec mode by running "nv unset interface <intf> fec
    8. Verify fec mode is default by running “nv show interface <intf>”
    9. Verify fec mode is default by running “nv show interface <intf> link”
    """

    TestToolkit.tested_api = ApiType.NVUE

    with allure.step("Get a random active port"):
        selected_port = Tools.RandomizationTool.select_random_port().get_returned_value()

    TestToolkit.update_tested_ports([selected_port])

    with allure.step("Verify fec mode is set to default ({})".format(LinkDetectionConsts.FEC_MODE_DEFAULT)):
        link_output = selected_port.interface.link.show(output_format="auto")
        ValidationTool.verify_fec_config_in_auto_output(link_output, LinkDetectionConsts.FEC_MODE_DEFAULT)

    with allure.step("Set the fec mode to {} for the selected port {}".format(fec_mode, selected_port.name)):
        selected_port.interface.link.set(op_param_name=LinkDetectionConsts.FEC_MODE, op_param_value=fec_mode,
                                         apply=True, ask_for_confirmation=True).verify_result()

    with allure.step("Verify fec mode is set to {}".format(fec_mode)):
        link_output = selected_port.interface.link.show(output_format="auto")
        ValidationTool.verify_fec_config_in_auto_output(link_output, fec_mode)

    with allure.step("Unset the fec mode for the selected port {}".format(selected_port.name)):
        selected_port.interface.link.unset(op_param=LinkDetectionConsts.FEC_MODE, apply=True, ask_for_confirmation=True)\
            .verify_result()

    with allure.step("Verify fec mode is default"):
        link_output = selected_port.interface.link.show(output_format="auto")
        ValidationTool.verify_fec_config_in_auto_output(link_output, LinkDetectionConsts.FEC_MODE_DEFAULT)


@pytest.mark.interfaces
def test_set_interface_link_fec_mode_invalid(engines):
    """
    Try to configure interface link fec mode to invalid mode and verify the configuration is not applied
    Steps:
    1. Select a random active port
    2. Verify fec mode is default by running “nv show interface <intf> link”
    3. Try to set port link fec mode by running "nv set interface <intf> fec fec_dummy
    4. Verify the command fails
    5. Verify fec mode is default by running “nv show interface <intf> link”
    """

    TestToolkit.tested_api = ApiType.NVUE
    fec_mode = "fec_dummy"

    with allure.step("Get a random active port"):
        selected_port = Tools.RandomizationTool.select_random_port().get_returned_value()

    TestToolkit.update_tested_ports([selected_port])

    with allure.step("Verify fec mode is default"):
        link_output = selected_port.interface.link.show(output_format="auto")
        ValidationTool.verify_fec_config_in_auto_output(link_output, LinkDetectionConsts.FEC_MODE_DEFAULT)

    with allure.step("Set the fec mode to {} for the selected port {}".format(fec_mode, selected_port.name)):
        selected_port.interface.link.set(op_param_name=LinkDetectionConsts.FEC_MODE, op_param_value=fec_mode,
                                         apply=True, ask_for_confirmation=True).verify_result(should_succeed=False)

    with allure.step("Verify fec mode remains default"):
        link_output = selected_port.interface.link.show(output_format="auto")
        ValidationTool.verify_fec_config_in_auto_output(link_output, LinkDetectionConsts.FEC_MODE_DEFAULT)
