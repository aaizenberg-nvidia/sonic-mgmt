import logging

import pytest

from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.Fae import Fae
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_constants.constants_nvos import StoDebug
from ngts.tools.test_utils.nvos_general_utils import loganalyzer_ignore
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from retry.api import retry_call


logger = logging.getLogger(__name__)


def _simulate_sto_event(engines):
    engines.dut.run_cmd("sudo mcra /dev/mst/mt54008_pciconf0 0x3ffffffc 0x80000000 && sudo mcra /dev/mst/mt54008_pciconf1 0x3ffffffc 0x80000000 && sudo mcra /dev/mst/mt54008_pciconf2 0x3ffffffc 0x80000000 && sudo mcra /dev/mst/mt54008_pciconf3 0x3ffffffc 0x80000000")
    engines.dut.run_cmd("sudo mcra /dev/mst/mt54008_pciconf0 0x29112bc.23:1 1 && sudo mcra /dev/mst/mt54008_pciconf1 0x29112bc.23:1 1 && sudo mcra /dev/mst/mt54008_pciconf2 0x29112bc.23:1 1 && sudo mcra /dev/mst/mt54008_pciconf3 0x29112bc.23:1 1")


def _verify_debug_sto_files_created(engines):
    dump_folder = engines.dut.run_cmd(f"ls -la {StoDebug.DEBUG_STO_DUMP_FOLDER}")
    assert StoDebug.STO_DEBUG_DUMPS in dump_folder, 'STO dumps not created'


@pytest.mark.dut_reboot
@pytest.mark.fae
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_fae_system_sto_event_default(engines, devices, nv_command, random_api, test_name):
    """
    Test flow:
        1. Verify default values for nv show fae system sto-debug command
        2. Simulate STO event
        3. Verify STO logs
        4. Verify STO dumps created
        5. Generate tech support and verify STO dumps inside
    """
    fae = Fae()
    try:
        with allure.step('Run show command and verify default values'):
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(fae.system.sto_event.show()).get_returned_value()

            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=StoDebug.STATE,
                                                              expected_value=StoDebug.ENABLED)

        with allure.step('Simulate STO debug event'):
            nv_command.system.log.rotate_logs()
            # TBD simulate STO event will be changed to stress tool
            with loganalyzer_ignore():  # supposed to be able to ignore LA here because sto injection
                _simulate_sto_event(engines)

        with allure.step('Verify STO event logs'):
            TestToolkit.tested_api = 'NVUE'
            show_output = nv_command.system.log.file.show_log(param=f" | grep '{StoDebug.STO_EVENT_LOG}'")
            ValidationTool.verify_expected_output(show_output, StoDebug.STO_EVENT_LOG).verify_result()

        with allure.step('Simulate STO debug files'):
            retry_call(lambda: _verify_debug_sto_files_created(engines), tries=10, delay=2)

        with allure.step('Verify STO files added to dump'):
            tech_support_file, duration = nv_command.system.techsupport.action_generate(test_name=test_name)
            tech_support_dir = tech_support_file.replace('.tar.gz', '')
            nv_command.system.techsupport.extract_techsupport_files(engines.dut)

            # Get all files in dump folder
            dump_files_list = nv_command.system.techsupport.get_techsupport_files_list(engines.dut, '')
            assert any(f.startswith(StoDebug.STO_DEBUG_DUMPS) for f in dump_files_list), \
                f'{StoDebug.STO_DEBUG_DUMPS} not found in tech-support'

    finally:
        with allure.step('Reboot system after injection STO event'):
            nv_command.system.reboot.action_reboot(params='force').verify_result()


@pytest.mark.fae
def test_fae_system_sto_event_disabled(engines, devices, nv_command, random_api):
    """
    Test flow:
        1. Verify default values for nv show fae system sto-debug command
        2. Set nv set fae system sto-debug state disabled and verify state changed
        3. Simulate STO event
        4. Verify STO logs not exist
        5. Unset nv unset fae system sto-debug state and verify state changed
    """
    fae = Fae()
    try:
        with allure.step('Run show command and verify default values'):
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                fae.system.sto_event.show()).get_returned_value()

            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=StoDebug.STATE,
                                                              expected_value=StoDebug.ENABLED)

        with allure.step('Run show command and verify default values'):
            fae.system.sto_event.set(op_param_name=StoDebug.STATE, op_param_value=StoDebug.DISABLED, apply=True).verify_result()
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                fae.system.sto_event.show()).get_returned_value()

            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=StoDebug.STATE,
                                                              expected_value=StoDebug.DISABLED)

        with allure.step('Simulate STO debug event'):
            nv_command.system.log.rotate_logs()
            # TBD simulate STO event will be changed to stress tool
            _simulate_sto_event(engines)

        with allure.step('Verify STO event logs'):
            show_output = nv_command.system.log.file.show()
            ValidationTool.verify_expected_output(show_output, StoDebug.STO_EVENT_LOG, should_be_found=False).verify_result()

    finally:
        fae.system.sto_event.unset(op_param=StoDebug.STATE, apply=True).verify_result()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
            fae.system.sto_event.show()).get_returned_value()
        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=StoDebug.STATE,
                                                          expected_value=StoDebug.ENABLED)
