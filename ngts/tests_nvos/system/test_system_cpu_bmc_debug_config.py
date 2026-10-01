import os
import pytest
import logging
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_constants.constants_nvos import SystemConsts, NvosConst
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import ActionConsts, HealthConsts
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active


logger = logging.getLogger(__name__)


@pytest.mark.dut_reboot
@pytest.mark.cpu_debug_config
@pytest.mark.system
def test_system_cpu_debug_config(engines, devices, nv_command):
    """
    Test flow:
        1. Check default values for cpu-debug-config output
        2. Download positive cpu-debug-config and verify output
        3. Set next cpu-debug-config yaml and verify output
        4. Perform reboot and verify cpu-debug-config applied
        5. Unset cpu-debug-config, reboot system, check config not applied
        6. Cleanup
    """
    system = nv_command.system
    cpu = nv_command.system.cpu_debug_config

    try:
        with allure.step("Check default values for cpu-debug-config output"):
            _verify_debug_config_state(cpu, current=SystemConsts.NA, next=SystemConsts.NA)

        with allure.step('Download positive cpu-debug-config and verify output'):
            _download_debug_config(cpu, SystemConsts.VERIFICATION_CPU_DEBUG_PATH, SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG)
            _verify_debug_config_state(cpu, current=SystemConsts.NA, next=SystemConsts.NA)
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.cpu_debug_config.show('files')).get_returned_value()
            ValidationTool.verify_expected_output(output_dictionary, SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG).verify_result()

        with allure.step("Set next cpu-debug-config yaml and verify output"):
            system.cpu_debug_config.set(SystemConsts.NEXT_DEBUG_CONFIG, SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG, apply=True, ask_for_confirmation=True)
            NvueGeneralCli.save_config(engines.dut)
            _verify_debug_config_state(cpu, current=SystemConsts.NA, next=SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG)

        with allure.step("Try to delete config file, when it already set"):
            system.cpu_debug_config.action(ActionConsts.DELETE,
                                           additional_params={'files': f'{SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG}'}).verify_result(should_succeed=False)

        with allure.step("Perform reboot and verify cpu-debug-config applied"):
            with allure.step("Perform system reboot"):
                system.reboot.action_reboot(params='force').verify_result()

            with allure.step("Verify cpu-debug-config success after reboot"):
                _verify_debug_config_state(cpu, current=SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG, next=SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG, status=SystemConsts.SUCCESS_STATUS_DEBUG_CONFIG)

            with allure.step("Verify folder created after cpu-debug-config pass"):
                _check_folder_created(engines, SystemConsts.CPU_CONFIG_CREATED_FOLDER)

            with allure.step("Check system logs"):
                show_output = system.log.file.show_log(param=SystemConsts.CPU_DEBUG_LOG_GREP)
                ValidationTool.verify_expected_output(show_output, 'cpu-debug-config').verify_result()

        with allure.step("Unset cpu-debug-config, reboot system, check config not applied"):
            with allure.step("Unset cpu debug config"):
                system.cpu_debug_config.unset(apply=True, ask_for_confirmation=True)
                NvueGeneralCli.save_config(engines.dut)

            with allure.step("Check cpu-debug-config after unset"):
                _verify_debug_config_state(cpu, current=SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG, next=SystemConsts.NA)

            with allure.step("Perform system reboot"):
                system.reboot.action_reboot(params='force').verify_result()

            with allure.step("Check config not applied after reboot"):
                _verify_debug_config_state(cpu, current=SystemConsts.NA, next=SystemConsts.NA)

    finally:
        with allure.step("Cleanup for cpu debug config"):
            system.cpu_debug_config.action(ActionConsts.DELETE,
                                           additional_params={'files': f'{SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG}'})

            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.cpu_debug_config.show('files')).get_returned_value()
            assert SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG not in output_dictionary, 'config file not deleted'


@pytest.mark.dut_reboot
@pytest.mark.bmc_debug_config
@pytest.mark.system
def test_bmc_debug_config(engines, devices, nv_command):
    """
    Test flow:
        1. Check default values for bmc-debug-config output
        2. Download positive bmc-debug-config and verify output
        3. Set next bmc-debug-config yaml and verify output
        4. Perform reboot and verify bmc-debug-config applied
        5. Unset bmc-debug-config, reboot system, check config not applied
        6. Cleanup
    """
    system = nv_command.system
    bmc = nv_command.system.bmc_debug_config

    with allure.step("Check default values for bmc-debug-config output"):
        _verify_debug_config_state(bmc, current=SystemConsts.NA, next=SystemConsts.NA)

    with allure.step('Download positive bmc-debug-config and verify output'):
        _download_debug_config(bmc, SystemConsts.VERIFICATION_CPU_DEBUG_PATH,
                               SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG)
        _verify_debug_config_state(bmc, current=SystemConsts.NA, next=SystemConsts.NA)
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
            system.bmc_debug_config.show('files')).get_returned_value()
        ValidationTool.verify_expected_output(output_dictionary,
                                              SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG).verify_result()

    with allure.step("Set next bmc-debug-config yaml and verify output"):
        system.bmc_debug_config.set(SystemConsts.NEXT_DEBUG_CONFIG, SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG, apply=True, ask_for_confirmation=True)
        NvueGeneralCli.save_config(engines.dut)

        _verify_debug_config_state(bmc, current=SystemConsts.NA, next=SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG)

    with allure.step("Try to delete config file, when it already set"):
        system.bmc_debug_config.action(ActionConsts.DELETE,
                                       additional_params={
                                           'files': f'{SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG}'}).verify_result(
            should_succeed=False)

    with allure.step("Perform power cycle and verify bmc-debug-config applied"):
        with allure.step("Perform power cycle"):
            system.action(ActionConsts.POWER_CYCLE, flags='force', reboot_params=True,
                          expected_output='System will power cycle in a few seconds')

        with allure.step("Verify bmc-debug-config success after power cycle"):
            _verify_debug_config_state(bmc, current=SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG, next=SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG,
                                       status=SystemConsts.SUCCESS_STATUS_DEBUG_CONFIG)

    with allure.step("Unset bmc-debug-config, power cycle, check config not applied"):
        with allure.step("Unset bmc debug config"):
            system.bmc_debug_config.unset(apply=True, ask_for_confirmation=True)
            NvueGeneralCli.save_config(engines.dut)

        with allure.step("Check bmc-debug-config after unset"):
            _verify_debug_config_state(bmc, current=SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG, next=SystemConsts.NA)

        with allure.step("Perform power cycle"):
            system.action(ActionConsts.POWER_CYCLE, flags='force', reboot_params=True,
                          expected_output='System will power cycle in a few seconds')

        with allure.step("Check config not applied after power cycle"):
            _verify_debug_config_state(bmc, current=SystemConsts.NA, next=SystemConsts.NA)

    with allure.step("Cleanup for bmc debug config"):
        system.bmc_debug_config.action(ActionConsts.DELETE,
                                       additional_params={'files': f'{SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG}'})

        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
            system.bmc_debug_config.show('files')).get_returned_value()
        assert SystemConsts.PASS_FOLDER_CPU_DEBUG_CONFIG not in output_dictionary, 'config file not deleted'


@pytest.mark.dut_reboot
@pytest.mark.cpu_debug_config
@pytest.mark.system
def test_cpu_bmc_debug_config_negative(engines, devices, nv_command):
    """
    Test flow:
        1. Download not yaml config, should fail
        2. Download yaml config, without values inside
        3. Download yaml config with wrong register
        4. Download yaml config with wrong version
        5. Check logs
        6. Check health status is OK
    """
    system = nv_command.system
    cpu = nv_command.system.cpu_debug_config
    bmc = nv_command.system.bmc_debug_config

    with allure.step("Download negative not a yaml config file"):
        _download_debug_config(cpu, SystemConsts.NOT_SHELL_DEBUG_CONFIG,
                               expected_output=SystemConsts.ERROR_NOT_SHELL_SCRIPT, should_succeed=False)

    with allure.step("Download yaml config file without any fields, values inside"):
        _download_debug_config(bmc, SystemConsts.NOT_SHELL_DEBUG_CONFIG,
                               expected_output=SystemConsts.ERROR_NOT_SHELL_SCRIPT, should_succeed=False)

    with allure.step("Download cpu negative config file"):
        _download_debug_config(cpu, SystemConsts.VERIFICATION_CPU_DEBUG_PATH,
                               SystemConsts.FAIL_DEBUG_CONFIG)

    with allure.step("Download bmc negative config file"):
        _download_debug_config(bmc, SystemConsts.VERIFICATION_BMC_DEBUG_PATH,
                               SystemConsts.FAIL_DEBUG_CONFIG)

    with allure.step("Set not exist file and verify output"):
        cpu.set(SystemConsts.NEXT_DEBUG_CONFIG, SystemConsts.NOT_EXIST_DEBUG_CONFIG, apply=True, ask_for_confirmation=True).verify_result(False)
        bmc.set(SystemConsts.NEXT_DEBUG_CONFIG, SystemConsts.NOT_EXIST_DEBUG_CONFIG, apply=True, ask_for_confirmation=True).verify_result(False)
        NvueGeneralCli.detach_config(engines.dut)

    with allure.step("Set next debug-config file to negative"):
        cpu.set(SystemConsts.NEXT_DEBUG_CONFIG, SystemConsts.FAIL_DEBUG_CONFIG, apply=True, ask_for_confirmation=True)
        bmc.set(SystemConsts.NEXT_DEBUG_CONFIG, SystemConsts.FAIL_DEBUG_CONFIG, apply=True, ask_for_confirmation=True)
        NvueGeneralCli.save_config(engines.dut)

    with allure.step("Perform power cycle"):
        system.action(ActionConsts.POWER_CYCLE, flags='force', reboot_params=True,
                      expected_output='System will power cycle in a few seconds')

    with allure.step("Check cpu config applied after power cycle"):
        _verify_debug_config_state(cpu, current=SystemConsts.FAIL_DEBUG_CONFIG, next=SystemConsts.FAIL_DEBUG_CONFIG, status=SystemConsts.FAILED_STATUS_DEBUG_CONFIG)
        _verify_debug_config_state(bmc, current=SystemConsts.FAIL_DEBUG_CONFIG, next=SystemConsts.FAIL_DEBUG_CONFIG, status=SystemConsts.FAILED_STATUS_DEBUG_CONFIG)

    with allure.step("Check system status is OK"):
        system.validate_health_status(HealthConsts.OK)

    with allure.step("Unset cpu and bmc config files"):
        cpu.unset(apply=True, ask_for_confirmation=True)
        bmc.unset(apply=True, ask_for_confirmation=True)
        NvueGeneralCli.save_config(engines.dut)

    with allure.step("Perform power cycle"):
        system.action(ActionConsts.POWER_CYCLE, flags='force', reboot_params=True,
                      expected_output='System will power cycle in a few seconds')

    with allure.step("Check default values for bmc-debug-config output after cleanup"):
        _verify_debug_config_state(bmc, current=SystemConsts.NA, next=SystemConsts.NA)

    with allure.step("Check default values for cpu-debug-config output after cleanup"):
        _verify_debug_config_state(cpu, current=SystemConsts.NA, next=SystemConsts.NA)


def _download_debug_config(debug_config, folder='', script='', expected_output='', should_succeed=True):
    path = os.path.join(folder, script)
    debug_config.action_fetch(path).verify_result(should_succeed=should_succeed, expected_value=expected_output)


def _check_folder_created(engine, folder_path):
    output = engine.dut.run_cmd(f'ls -la {folder_path}')
    assert output, f"No output from ls command for {folder_path}"
    assert 'No such file or directory' not in output


def _verify_debug_config_state(debug_config, current=SystemConsts.NA, next=SystemConsts.NA, status=None):
    output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(debug_config.show()).get_returned_value()
    ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.CURRENT_DEBUG_CONFIG, current).verify_result()
    ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.NEXT_DEBUG_CONFIG, next).verify_result()
    if status:
        ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.STATUS_DEBUG_CONFIG, status).verify_result()
