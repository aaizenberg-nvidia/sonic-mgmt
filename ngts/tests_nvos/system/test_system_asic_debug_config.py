import pytest
import logging
import random
import retry
import re
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_constants.constants_nvos import SystemConsts, ApiType
from ngts.nvos_tools.infra.RegisterTool import RegisterTool
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import ActionConsts, HealthConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit


logger = logging.getLogger(__name__)


@pytest.mark.asic_debug_config
@pytest.mark.system
def test_asic_debug_config_positive(engines, devices, nv_command):
    """
    Test flow:
        1. Check default values for asic-debug-config output
        2. Download positive asic-debug-config and verify output
        3. Set next asic-debug-config yaml and verify output
        4. Perform reboot and verify asic-debug-config applied
        5. Unset asic-debug-config, reboot system, check config not applied
        6. Cleanup
    """
    system = nv_command.system
    try:
        with allure.step("Check default values for asic-debug-config output"):
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.asic_debug_config.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.CURRENT_ASIC_DEBUG_CONFIG, SystemConsts.NA).verify_result()
            ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.NEXT_ASIC_DEBUG_CONFIG, SystemConsts.NA).verify_result()

        with allure.step('Download positive asic-debug-config and verify output'):
            _download_asic_debug_config(system, SystemConsts.PASS_ASIC_DEBUG_CONFIG)
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.asic_debug_config.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.CURRENT_ASIC_DEBUG_CONFIG, SystemConsts.NA).verify_result()
            ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.NEXT_ASIC_DEBUG_CONFIG, SystemConsts.NA).verify_result()
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.asic_debug_config.show('files')).get_returned_value()
            ValidationTool.verify_expected_output(output_dictionary, SystemConsts.PASS_ASIC_DEBUG_CONFIG).verify_result()

        with allure.step("Set next asic-debug-config yaml and verify output"):
            system.asic_debug_config.set(SystemConsts.NEXT_ASIC_DEBUG_CONFIG, SystemConsts.PASS_ASIC_DEBUG_CONFIG)
            NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation=True)
            NvueGeneralCli.save_config(engines.dut)

            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.asic_debug_config.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.CURRENT_ASIC_DEBUG_CONFIG, SystemConsts.NA).verify_result()
            ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.NEXT_ASIC_DEBUG_CONFIG, SystemConsts.PASS_ASIC_DEBUG_CONFIG).verify_result()

        with allure.step("Try to delete config file, when it already set"):
            system.asic_debug_config.action(ActionConsts.DELETE,
                                            additional_params={'files': f'{SystemConsts.PASS_ASIC_DEBUG_CONFIG}'}).verify_result(should_succeed=False)

        with allure.step("Perform reboot and verify asic-debug-config applied"):
            with allure.step("Perform system reboot"):
                system.reboot.action_reboot(params='force').verify_result()

            with allure.step("Verify asic-debug-config success after reboot"):
                output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.asic_debug_config.show()).get_returned_value()
                ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.STATUS_ASIC_DEBUG_CONFIG, SystemConsts.SUCCESS_STATUS_ASIC_DEBUG_CONFIG).verify_result()
                ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.CURRENT_ASIC_DEBUG_CONFIG, SystemConsts.PASS_ASIC_DEBUG_CONFIG).verify_result()
                ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.NEXT_ASIC_DEBUG_CONFIG, SystemConsts.PASS_ASIC_DEBUG_CONFIG).verify_result()

            with allure.step("Check system logs"):
                show_output = system.log.file.show_log(param="| grep asic-debug-config")
                ValidationTool.verify_expected_output(show_output, 'asic-debug-config').verify_result()

            with allure.step("Validate asic-debug-config log file"):
                _validate_log_file(engines, string_to_validate='asic_debug_config_init')

    finally:
        with allure.step("Unset asic-debug-config, reboot system, check config not applied"):
            with allure.step("Unset asic debug config"):
                system.asic_debug_config.unset()
                NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation=True)
                NvueGeneralCli.save_config(engines.dut)

            with allure.step("Perform system reboot"):
                system.reboot.action_reboot(params='force').verify_result()

            with allure.step("Check config not applied after reboot"):
                output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.asic_debug_config.show()).get_returned_value()
                ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.CURRENT_ASIC_DEBUG_CONFIG, SystemConsts.NA).verify_result()
                ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.NEXT_ASIC_DEBUG_CONFIG, SystemConsts.NA).verify_result()

        with allure.step("Cleanup for asic debug config"):
            system.asic_debug_config.action(ActionConsts.DELETE,
                                            additional_params={'files': f'{SystemConsts.PASS_ASIC_DEBUG_CONFIG}'})

            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.asic_debug_config.show('files')).get_returned_value()
            assert SystemConsts.PASS_ASIC_DEBUG_CONFIG not in output_dictionary, 'config file not deleted'


@pytest.mark.asic_debug_config
@pytest.mark.system
def test_asic_debug_config_positive_fae(engines, devices, nv_command):
    """
    Test flow:
        1. Check default values
        2. Download and fae run passing config file
        3. Check values after action run fae command
        4. Cleanup
    """
    system = nv_command.system
    fae_system = nv_command.fae.system

    with allure.step("Download config file"):
        _download_asic_debug_config(system, SystemConsts.PASS_ASIC_DEBUG_CONFIG)

    with allure.step("Run nv action run fae asic-debug config"):
        fae_system.asic_debug_config.action(ActionConsts.RUN, additional_params={'files': f'{SystemConsts.PASS_ASIC_DEBUG_CONFIG}'})

    with allure.step("Check values after action run fae command"):
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
            system.asic_debug_config.show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.STATUS_ASIC_DEBUG_CONFIG,
                                                    SystemConsts.SUCCESS_STATUS_ASIC_DEBUG_CONFIG).verify_result()
        ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.CURRENT_ASIC_DEBUG_CONFIG,
                                                    SystemConsts.PASS_ASIC_DEBUG_CONFIG).verify_result()

    with allure.step("Cleanup for asic debug config"):
        system.asic_debug_config.action(ActionConsts.DELETE,
                                        additional_params={'files': f'{SystemConsts.PASS_ASIC_DEBUG_CONFIG}'})

        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
            system.asic_debug_config.show('files')).get_returned_value()
        assert SystemConsts.PASS_ASIC_DEBUG_CONFIG not in output_dictionary, 'config file not deleted'

    with allure.step("Perform system reboot"):
        system.reboot.action_reboot(params='force').verify_result()


@pytest.mark.asic_debug_config
@pytest.mark.system
def test_asic_debug_config_negative(engines, devices, nv_command):
    """
    Test flow:
        1. Download not yaml config, should fail
        2. Download yaml config, without values inside
        3. Download yaml config with wrong register
        4. Download yaml config with wrong version
        5. Check logs
        6. Check health status is OK
        7. Unset asic-debug-config, reboot system, check config not applied
    """
    system = nv_command.system
    fae_system = nv_command.fae.system
    try:
        with allure.step("Download negative not a yaml config file"):
            _download_asic_debug_config(system, SystemConsts.NOT_YAML_ASIC_DEBUG_CONFIG,
                                        expected_output='no asic-debug-config section found in script', should_succeed=False)

        with allure.step("Download yaml config file without any fields, values inside"):
            _download_asic_debug_config(system, SystemConsts.YAML_WITHOUT_FIELDS,
                                        expected_output='no asic-debug-config section found in script', should_succeed=False)

        with allure.step("Download yaml config file with wrong register"):
            _download_asic_debug_config(system, SystemConsts.FAIL_ASIC_DEBUG_CONFIG_WRONG_REGISTER)

        with allure.step("Run asic-debug-config with wrong register"):
            fae_system.asic_debug_config.action(ActionConsts.RUN,
                                                additional_params={'files': f'{SystemConsts.FAIL_ASIC_DEBUG_CONFIG_WRONG_REGISTER}'})

        with allure.step("Verify asic-debug-config failed"):
            _wait_asic_debug_config_status_changed(system, SystemConsts.STATUS_ASIC_DEBUG_CONFIG, SystemConsts.FAILED_STATUS_ASIC_DEBUG_CONFIG)

        with allure.step("Cleanup for asic debug config"):
            system.asic_debug_config.action(ActionConsts.DELETE,
                                            additional_params={'files': f'{SystemConsts.FAIL_ASIC_DEBUG_CONFIG_WRONG_REGISTER}'})

            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.asic_debug_config.show('files')).get_returned_value()
            assert SystemConsts.FAIL_ASIC_DEBUG_CONFIG_WRONG_REGISTER not in output_dictionary, 'config file not deleted'

        with allure.step("Download yaml config file without any fields, values inside"):
            _download_asic_debug_config(system, SystemConsts.FAIL_ASIC_DEBUG_CONFIG_WRONG_VERSION,
                                        expected_output='is not compatible with script version', should_succeed=False)

        with allure.step("Check logs"):
            show_output = system.log.file.show_log(param="| grep asic-debug-config")
            ValidationTool.verify_expected_output(show_output, SystemConsts.ASIC_DEBUG_CONFIG_ERROR).verify_result()

        with allure.step("Check system status is OK"):
            system.validate_health_status(HealthConsts.OK)
    finally:
        # 'current' and 'status' keep describing the failed run until reboot, so the following tests
        # must not inherit them
        _unset_asic_debug_config_and_reboot(system, engines)


@pytest.mark.asic_debug_config
@pytest.mark.system
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_asic_debug_config_pgcb(engines, devices, nv_command, test_api):
    """
    Test flow:
        1. Check default values for asic-debug-config output
        2. Download positive asic-debug-config and verify output
        3. Set next asic-debug-config yaml and verify output
        4. Perform reboot and verify asic-debug-config applied
        5. Unset asic-debug-config, reboot system, check config not applied
        6. Cleanup
    """
    system = nv_command.system
    mst_path = "/dev/mst/"
    mst_devices = engines.dut.run_cmd(f"ls {mst_path} | grep -i pciconf").splitlines()

    with allure.step('Download positive asic-debug-config and verify output'):
        _download_asic_debug_config(system, SystemConsts.PGCB_PASS_CONFIG)
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.asic_debug_config.show()).get_returned_value()
        ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(), SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.values(), output_dictionary).verify_result()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.asic_debug_config.files.show()).get_returned_value()
        ValidationTool.verify_expected_output(output_dictionary, SystemConsts.PGCB_PASS_CONFIG).verify_result()

    with allure.step("Set next asic-debug-config yaml and verify output"):
        system.asic_debug_config.set(SystemConsts.NEXT_ASIC_DEBUG_CONFIG, SystemConsts.PGCB_PASS_CONFIG)
        TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config(engines.dut)
        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.asic_debug_config.show()).get_returned_value()
        ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(), [SystemConsts.NA, SystemConsts.PGCB_PASS_CONFIG], output_dictionary).verify_result()

    with allure.step("Try to delete config file, when it already set"):
        system.asic_debug_config.files.file_name[SystemConsts.PGCB_PASS_CONFIG].action_delete().verify_result(should_succeed=False)

    with allure.step("Perform reboot and verify asic-debug-config applied"):
        with allure.step("Perform system reboot"):
            system.reboot.action_reboot(params='force').verify_result()

        with allure.step("Verify asic-debug-config success after reboot"):
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.asic_debug_config.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.STATUS_ASIC_DEBUG_CONFIG, SystemConsts.SUCCESS_STATUS_ASIC_DEBUG_CONFIG).verify_result()
            ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(), [SystemConsts.PGCB_PASS_CONFIG, SystemConsts.PGCB_PASS_CONFIG], output_dictionary)

        with allure.step("Run PTER register access command"):
            output = RegisterTool.get_mst_register_value(engines.dut, mst_path + mst_devices[1], "PGCB",
                                                         '-i cfg_buffer_num=0x0',
                                                         'buffer')
            assert 'buffer_size=0x24' not in output, 'PGCB register was not changed after reboot'

    with allure.step("Unset asic-debug-config, reboot system, check config not applied"):
        with allure.step("Unset asic debug config"):
            system.asic_debug_config.unset()
            TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config(engines.dut)
            TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

        with allure.step("Perform system reboot"):
            system.reboot.action_reboot(params='force').verify_result()

        with allure.step("Check config not applied after reboot"):
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.asic_debug_config.show()).get_returned_value()
            ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(),
                                                            SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.values(),
                                                            output_dictionary)

    with allure.step("Cleanup for asic debug config"):
        system.asic_debug_config.files.file_name[SystemConsts.PGCB_PASS_CONFIG].action_delete()
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
            system.asic_debug_config.files.show()).get_returned_value()
        assert SystemConsts.PGCB_PASS_CONFIG not in output_dictionary, 'config file not deleted'


@pytest.mark.asic_debug_config
@pytest.mark.system
def test_asic_debug_config_pgbc(engines, devices, nv_command, standalone_system):
    """
        Test flow:
            1. Download positive asic-debug-config for 20 prms and verify output
            2. Download positive asic-debug-config for 32 prms and verify output
            3. Download positive asic-debug-config for 2k buffer and verify output
            4. Unset asic-debug-config, reboot system, check config not applied
            5. Cleanup
        """
    if standalone_system:
        pytest.skip(f"Skipping test - Standalone system, need to run on system with gpu.")
    system = nv_command.system
    mst_path = "/dev/mst/"
    mst_devices = engines.dut.run_cmd(f"ls {mst_path} | grep -i pciconf").splitlines()
    try:
        with allure.step('Download positive asic-debug-config and verify output'):
            _download_asic_debug_config(system, SystemConsts.ASIC_DEBUG_CONFIG_20_PRMS)
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.asic_debug_config.show()).get_returned_value()
            ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(),
                                                            SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.values(),
                                                            output_dictionary)
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.asic_debug_config.files.show()).get_returned_value()
            ValidationTool.verify_expected_output(output_dictionary, SystemConsts.ASIC_DEBUG_CONFIG_20_PRMS).verify_result()

        with allure.step("Set next asic-debug-config yaml and verify output"):
            system.asic_debug_config.set(SystemConsts.NEXT_ASIC_DEBUG_CONFIG, SystemConsts.ASIC_DEBUG_CONFIG_20_PRMS)
            TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config(engines.dut)
            TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.asic_debug_config.show()).get_returned_value()
            ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(),
                                                            [SystemConsts.NA, SystemConsts.ASIC_DEBUG_CONFIG_20_PRMS],
                                                            output_dictionary)

        with allure.step("Perform reboot and verify asic-debug-config applied"):
            with allure.step("Perform system reboot"):
                system.reboot.action_reboot(params='force').verify_result()

            with allure.step("Verify asic-debug-config success after reboot"):
                output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                    system.asic_debug_config.show()).get_returned_value()
                ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.STATUS_ASIC_DEBUG_CONFIG,
                                                            SystemConsts.SUCCESS_STATUS_ASIC_DEBUG_CONFIG).verify_result()
                ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(),
                                                                [SystemConsts.ASIC_DEBUG_CONFIG_20_PRMS,
                                                                 SystemConsts.ASIC_DEBUG_CONFIG_20_PRMS], output_dictionary)

            with allure.step("Run PTER register access command"):
                output = RegisterTool.get_mst_register_value(engines.dut, mst_path + mst_devices[1], "PGRRS",
                                                             '-i local_port=0x2D')
                assert validate_return_status(output, "return_status_19")

        with allure.step('Download positive asic-debug-config and verify output'):
            _download_asic_debug_config(system, SystemConsts.ASIC_DEBUG_CONFIG_2K_BUFFER)
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.asic_debug_config.show()).get_returned_value()
            ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(),
                                                            SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.values(),
                                                            output_dictionary)
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.asic_debug_config.files.show()).get_returned_value()
            ValidationTool.verify_expected_output(output_dictionary, SystemConsts.ASIC_DEBUG_CONFIG_2K_BUFFER).verify_result()

        with allure.step("Set next asic-debug-config yaml and verify output"):
            system.asic_debug_config.set(SystemConsts.NEXT_ASIC_DEBUG_CONFIG, SystemConsts.ASIC_DEBUG_CONFIG_2K_BUFFER)
            TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config(engines.dut)
            TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.asic_debug_config.show()).get_returned_value()
            ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(),
                                                            [SystemConsts.NA, SystemConsts.ASIC_DEBUG_CONFIG_2K_BUFFER],
                                                            output_dictionary)

        with allure.step("Perform reboot and verify asic-debug-config applied"):
            with allure.step("Perform system reboot"):
                system.reboot.action_reboot(params='force').verify_result()

            with allure.step("Verify asic-debug-config success after reboot"):
                output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                    system.asic_debug_config.show()).get_returned_value()
                ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.STATUS_ASIC_DEBUG_CONFIG,
                                                            SystemConsts.SUCCESS_STATUS_ASIC_DEBUG_CONFIG).verify_result()
                ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(),
                                                                [SystemConsts.ASIC_DEBUG_CONFIG_2K_BUFFER,
                                                                 SystemConsts.ASIC_DEBUG_CONFIG_2K_BUFFER], output_dictionary)

            with allure.step("Run PTER register access command"):
                output = RegisterTool.get_mst_register_value(engines.dut, mst_path + mst_devices[1], "PGRRS",
                                                             '-i local_port=0x2D')
                assert validate_return_status(output, "return_status_27")

        with allure.step('Download positive asic-debug-config and verify output'):
            _download_asic_debug_config(system, SystemConsts.ASIC_DEBUG_CONFIG_32_PRMS)
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.asic_debug_config.show()).get_returned_value()
            ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(),
                                                            SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.values(),
                                                            output_dictionary)
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.asic_debug_config.files.show()).get_returned_value()
            ValidationTool.verify_expected_output(output_dictionary, SystemConsts.ASIC_DEBUG_CONFIG_32_PRMS).verify_result()

        with allure.step("Set next asic-debug-config yaml and verify output"):
            system.asic_debug_config.set(SystemConsts.NEXT_ASIC_DEBUG_CONFIG, SystemConsts.ASIC_DEBUG_CONFIG_32_PRMS)
            TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config(engines.dut)
            TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.asic_debug_config.show()).get_returned_value()
            ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(),
                                                            [SystemConsts.NA, SystemConsts.ASIC_DEBUG_CONFIG_32_PRMS],
                                                            output_dictionary)

        with allure.step("Perform reboot and verify asic-debug-config applied"):
            with allure.step("Perform system reboot"):
                system.reboot.action_reboot(params='force').verify_result()

            with allure.step("Verify asic-debug-config success after reboot"):
                output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                    system.asic_debug_config.show()).get_returned_value()
                ValidationTool.verify_field_value_in_output(output_dictionary, SystemConsts.STATUS_ASIC_DEBUG_CONFIG,
                                                            SystemConsts.SUCCESS_STATUS_ASIC_DEBUG_CONFIG).verify_result()
                ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(),
                                                                [SystemConsts.ASIC_DEBUG_CONFIG_32_PRMS,
                                                                 SystemConsts.ASIC_DEBUG_CONFIG_32_PRMS], output_dictionary)

            with allure.step("Run PTER register access command"):
                output = RegisterTool.get_mst_register_value(engines.dut, mst_path + mst_devices[1], "PGRRS",
                                                             '-i local_port=0x2D')
                assert validate_return_status(output, "return_status_31")
    finally:
        with allure.step("Unset asic-debug-config, reboot system, check config not applied"):
            with allure.step("Unset asic debug config"):
                system.asic_debug_config.unset()
                TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config(engines.dut)
                TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

            with allure.step("Perform system reboot"):
                system.reboot.action_reboot(params='force').verify_result()

            with allure.step("Check config not applied after reboot"):
                output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.asic_debug_config.show()).get_returned_value()
                ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(),
                                                                SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.values(),
                                                                output_dictionary)

        with allure.step("Cleanup for asic debug config"):
            system.asic_debug_config.files.file_name[SystemConsts.ASIC_DEBUG_CONFIG_20_PRMS].action_delete()
            system.asic_debug_config.files.file_name[SystemConsts.ASIC_DEBUG_CONFIG_32_PRMS].action_delete()
            system.asic_debug_config.files.file_name[SystemConsts.ASIC_DEBUG_CONFIG_2K_BUFFER].action_delete()
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.asic_debug_config.files.show()).get_returned_value()
            assert SystemConsts.ASIC_DEBUG_CONFIG_20_PRMS not in output_dictionary, 'config file not deleted'
            assert SystemConsts.ASIC_DEBUG_CONFIG_32_PRMS not in output_dictionary, 'config file not deleted'
            assert SystemConsts.ASIC_DEBUG_CONFIG_2K_BUFFER not in output_dictionary, 'config file not deleted'


@pytest.mark.asic_debug_config
@pytest.mark.system
def test_asic_debug_config_pgrss(engines, devices, nv_command):
    """
    Test flow:
        1. Clear system events
        2. Check default values for asic-debug-config output
        3. Download and run pgrss simulate trap script success
        4. Download and run pgrss simulate trap script nldf-fail
        5. Download and run pgrss simulate trap script prm-set-fail
        6. Clear system events
    """
    system = nv_command.system

    with allure.step('Clear system events to remove older  events'):
        system.events.action(ActionConsts.CLEAR)

    with allure.step("Check default values for asic-debug-config output"):
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(system.asic_debug_config.show()).get_returned_value()
        ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(), SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.values(), output_dictionary)

    with allure.step('Download and run pgrss simulate trap script success'):
        _download_run_simulate_pgrss_trap(engines.dut, argument=SystemConsts.SUCCESS_STATUS_DEBUG_CONFIG, download=True, run=True)
        output = OutputParsingTool.parse_json_str_to_dictionary(system.events.show()).get_returned_value()
        assert any(
            isinstance(v, dict) and 'NLDF succeeded on Port acp112' in v.get('text', '')
            for v in output.values()
        ), f"NLDF succeeded on Port acp112 not found in output: {output}"
        # TBD We download asic_debug_config with hardcoded values, that's why we have it hardcoded. In future when we will have rosalind with GPU, we will make automation to generate this configs, check test_asic_debug_config_pgbc test.

    with allure.step('Download and run pgrss simulate trap script nldf-fail'):
        _download_run_simulate_pgrss_trap(engines.dut, argument='nldf-fail', download=False, run=True)
        output = OutputParsingTool.parse_json_str_to_dictionary(system.events.show()).get_returned_value()
        assert any(
            isinstance(v, dict) and 'NLDF Failed on Port acp112' in v.get('text', '')
            for v in output.values()
        ), f"NLDF Failed on Port acp122 not found in output: {output}"

    with allure.step('Download and run pgrss simulate trap script prm-set-fail'):
        _download_run_simulate_pgrss_trap(engines.dut, argument='prm-set-fail', download=False, run=True)
        output = OutputParsingTool.parse_json_str_to_dictionary(system.events.show()).get_returned_value()
        assert any(
            isinstance(v, dict) and 'Unexpected GPU EMAD status on Port acp112' in v.get('text', '')
            for v in output.values()
        ), f"Unexpected GPU EMAD status on Port acp112 not found in output: {output}"

    with allure.step('Clear system events to remove NLDF events'):
        system.events.action(ActionConsts.CLEAR)


def _download_run_simulate_pgrss_trap(engine, argument='', script=SystemConsts.SIMULATE_PGRSS_TRAP, server=SystemConsts.NBU_NFS_SERVER, path=SystemConsts.VERIFICATION_ASIC_DEBUG_PATH, download=False, run=False):
    if download:
        engine.run_cmd(f"sudo curl -o /tmp/{script} {server}{path}/{script}")
        engine.run_cmd(f'sudo chmod 655 /tmp/{script}')
    if run:
        engine.run_cmd(f"sudo /tmp/{script} {argument}")


def _download_asic_debug_config(system, yaml='', expected_output='', should_succeed=True):
    path = f'{SystemConsts.VERIFICATION_ASIC_DEBUG_PATH}/{yaml}'
    system.asic_debug_config.action_fetch(path).verify_result(should_succeed=should_succeed, expected_value=expected_output)


def _unset_asic_debug_config_and_reboot(system, engines):
    with allure.step("Unset asic-debug-config, reboot system, check config not applied"):
        with allure.step("Unset asic debug config"):
            system.asic_debug_config.unset()
            TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config(engines.dut)
            TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

        with allure.step("Perform system reboot"):
            system.reboot.action_reboot(params='force').verify_result()

        with allure.step("Check config not applied after reboot"):
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                system.asic_debug_config.show()).get_returned_value()
            ValidationTool.validate_fields_values_in_output(SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.keys(),
                                                            SystemConsts.DEFAUL_ASIC_DEBUG_CONFIG_DEFAULT_VALUES.values(),
                                                            output_dictionary).verify_result()


def _validate_log_file(engines, string_to_validate=''):
    output = engines.dut.run_cmd(f'cat {SystemConsts.ASIC_DEBUG_CONFIG_LOG_FILE} | grep "{string_to_validate}"')
    assert string_to_validate in output, 'String not in asic-debug-config log'


def validate_return_status(output: str, field_name: str, expected: str = "0x00000000") -> bool:
    """
    :param output: output of mlxreg command
    :param field_name: prm return status "return_status_19"
    :param expected: expected value (default = 0x00000000)
    :return: True / False
    """
    pattern = rf"^{re.escape(field_name)}\s*\|\s*(0x[0-9a-fA-F]+)"

    for line in output.splitlines():
        match = re.match(pattern, line.strip())
        if match:
            actual = match.group(1)
            return actual == expected

    return False


@retry.retry(Exception, tries=2, delay=1)
def _wait_asic_debug_config_status_changed(system, status='', status_value=''):
    with allure.step("Run show show asic debug config"):
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
            system.asic_debug_config.show()).get_returned_value()

    with allure.step("Verify asic debug config status changed"):
        ValidationTool.verify_field_value_in_output(output_dictionary, status, status_value).verify_result()
