import logging
import pytest
import time
import random

from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_constants.constants_nvos import ApiType, OutputFormat, NvosConst
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.system.factory_reset.helpers import get_current_time
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active

logger = logging.getLogger()


@pytest.mark.nmx
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_cluster_sdn_factory_reset_nmx_down(engines, devices, test_api, has_loopbox):

    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json
    config_files_deleted = False
    cluster = Cluster()

    try:
        with allure.step("Disable cluster"):
            cluster.unset(apply=True).verify_result()
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled',
                                                             nmx_c_expected_state='down', engine=engines.dut)

        with allure.step("Run sdn reset factory while cluster is disabled"):
            sdn = Sdn()
            output = sdn.factory_default.action_reset(param='force').verify_result(should_succeed=False)

            assert ClusterConsts.RESET_FACTORY_CLUSTER_DISABLED[test_api] in output, f'Expected {ClusterConsts.RESET_FACTORY_CLUSTER_DISABLED[test_api]} Got {output}'

        with allure.step("Run sdn reset factory while nmx-conn is disabled (Enable cluster, and then reset factory before nmx-conn is up"):
            cluster.set(op_param_name="state", op_param_value=NvosConst.ENABLED, apply=True)
            cluster.show(output_format=output_format)
            output = sdn.factory_default.action_reset(param='force').verify_result(should_succeed=False)
            assert ClusterConsts.RESET_FACTORY_NMX_CONN_DISABLED[test_api] in output, f'Expected {ClusterConsts.RESET_FACTORY_NMX_CONN_DISABLED[test_api]} Got {output}'

    finally:
        cluster.unset(apply=True).verify_result()
        ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled',
                                                         nmx_c_expected_state='down', engine=engines.dut)


@pytest.mark.nmx
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
@pytest.mark.timeout(25 * MINUTE, func_only=True)
def test_sdn_reset_factory(engines, devices, test_api, has_loopbox, test_name, setup_name,
                           standalone_system, handle_la_marker_in_manufacture):
    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json
    with allure.step("Create Cluster object"):
        cluster = Cluster()
        system = System()
        sdn = Sdn()
        initial_config_contents = {}
        initial_configs_paths_to_restore = {}
        initial_configuration_restored = False
    try:
        logger.info("Setting cluster state to enabled")
        ClusterTools.start_cluster(cluster, setup_name, output_format, engine=engines.dut, devices=devices)
        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

        skip_chassis_mapping = is_bug_active(4207869) and standalone_system
        _wait_until_fm_config_has_gfm_value(engines, sdn)
        config_files_paths = get_current_config_files_paths(sdn, devices, skip_chassis_mapping)
        if not skip_chassis_mapping:
            with allure.step("Wait for chassis_mapping to be populated by nmx-c before baseline snapshot"):
                config_files_paths[ClusterConsts.NMX_CONTROLLER_CONFIG_CHASSIS_MAPPING] = _wait_until_chassis_mapping_file_has_content(engines, sdn)

        for file_type, file_path in config_files_paths.items():
            initial_config_contents[file_type] = engines.dut.run_cmd("sudo cat {}".format(file_path))

        with allure.step("Change initial content"):
            chassis_sn = ClusterTools.get_chassis_sn()
            for file_type in config_files_paths.keys():
                edit_cmd = ClusterConsts.CONFIG_FILES_CHANGE[file_type].format(
                    file_path=config_files_paths[file_type], chassis_sn=chassis_sn)
                engines.dut.run_cmd(edit_cmd)

        with allure.step("Install modified configurations"):
            for file_type in config_files_paths.keys():
                app = ClusterConsts.MAP_CONFIG_FILE_TYPE_TO_APP[file_type]
                file_name = config_files_paths[file_type].split('/')[-1]
                sdn.config.apps.app_name[app].type.file_type[file_type].files.file_name[file_name].action_file_install(force=False)

        with allure.step("Running sdn factory reset"):
            ClusterTools.reset_sdn_factory_default_and_wait_for_restart(sdn, cluster, engine=engines.dut)

        _wait_until_fm_config_has_gfm_value(engines, sdn)
        verify_current_config_equals_given_config(sdn, engines, devices, initial_config_contents, output_format,
                                                  skip_chassis_mapping)

        with allure.step("Reboot and verify configuration not changed"):
            result_obj, duration = OperationTime.save_duration('reboot', '', test_name, system.reboot.action_reboot)
            verify_current_config_equals_given_config(sdn, engines, devices, initial_config_contents, output_format,
                                                      skip_chassis_mapping)

    finally:
        current_time = get_current_time(engines)
        duration = execute_reset_factory(engines, system, devices.dut.reset_factory, "", current_time)

        with allure.step("Verify operation time"):
            OperationTime.verify_operation_time(duration, devices.dut.reset_factory, devices).verify_result()


def verify_current_config_equals_given_config(sdn, engines, devices, initial_config_contents, output_format,
                                              skip_chassis_mapping):
    """
    Verify current config equals given config for all apps on this device.
    Uses device-specific app configuration to handle different device types.
    """
    errors_list = []
    with allure.step("Verify config files content restored to initial"):
        for app in devices.dut.expected_cluster_apps:
            app_config_files = devices.dut.cluster_config_files_by_app.get(app, [])
            for file_type in app_config_files:
                if file_type == ClusterConsts.NMX_CONTROLLER_CONFIG_CHASSIS_MAPPING and skip_chassis_mapping:
                    continue
                output = sdn.config.apps.app_name[app].type.file_type[file_type].action_generate_sdn()
                installed_file = ClusterTools.get_generated_file_name(output.returned_value, 'config')
                output = OutputParsingTool.parse_show_output_to_dict(sdn.config.apps.app_name[app].type.file_type[file_type].files.show(output_format=output_format),
                                                                     output_format=output_format).get_returned_value()
                current_installed_config_path = output[installed_file]['path']
                current_config_content = engines.dut.run_cmd("sudo cat {}".format(current_installed_config_path))
                sdn.config.apps.app_name[app].type.file_type[file_type].files.file_name[installed_file].action_delete().verify_result()
                if file_type == 'chassis_mapping' and is_bug_active(4222718):
                    continue
                initial_config_set = set(line.strip() for line in initial_config_contents[file_type].strip().split('\n') if line.strip())
                current_config_set = set(line.strip() for line in current_config_content.strip().split('\n') if line.strip())
                if initial_config_set != current_config_set:
                    errors_list.append(f"Configuration mismatch in file {file_type}:\nInitial: {initial_config_set}\nCurrent: {current_config_set}")
        assert not errors_list, "\n\n".join(errors_list)


def execute_reset_factory(engines, system, operation, flag, current_time):
    logging.info("Current time: " + str(current_time))
    result_obj = system.factory_default.action_reset(operation=operation, param=flag)
    result_obj.verify_result()
    return result_obj.duration


def get_current_config_files_paths(sdn, devices, skip_chassis_mapping):
    """
    Get current config file paths for all apps on this device.
    Uses device-specific app configuration to handle different device types.
    """
    files_dict = {}
    with allure.step("Fetch & Generate config files"):
        for app in devices.dut.expected_cluster_apps:
            app_config_files = devices.dut.cluster_config_files_by_app.get(app, [])
            for file_type in app_config_files:
                if file_type == ClusterConsts.NMX_CONTROLLER_CONFIG_CHASSIS_MAPPING and skip_chassis_mapping:
                    continue
                output = sdn.config.apps.app_name[app].type.file_type[file_type].action_generate_sdn()
                installed_file = ClusterTools.get_generated_file_name(output.returned_value, 'config')
                output = OutputParsingTool.parse_show_output_to_dict(sdn.config.apps.app_name[app].type.file_type[file_type].files.show(output_format=OutputFormat.json),
                                                                     output_format=OutputFormat.json).get_returned_value()
                current_installed_config_path = output[installed_file]['path']
                files_dict[file_type] = current_installed_config_path
    return files_dict


def _wait_until_fm_config_has_gfm_value(engines, sdn):
    """
    Wait until a generated fm_config carries a value for GFM_WAIT_TIMEOUT_SEC. Observed: for ~10s after
    an sdn/system factory reset brings nmx-controller back, `generate` still renders the package template
    (key empty) before GFM's value appears, and no NVUE state exposes that transition. NVBug 6348727
    (Not-a-bug) covers the value itself, not this window.
    """
    fm_config = sdn.config.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[ClusterConsts.NMX_CONTROLLER_CONFIG_FM]
    with allure.step("Wait for GFM_WAIT_TIMEOUT_SEC to be populated in generated fm_config"):
        for _ in range(12):
            generate_output = fm_config.action_generate_sdn()
            generated_file = ClusterTools.get_generated_file_name(generate_output.returned_value, 'config')
            output = OutputParsingTool.parse_show_output_to_dict(fm_config.files.show(output_format=OutputFormat.json),
                                                                 output_format=OutputFormat.json).get_returned_value()
            content = engines.dut.run_cmd(f"sudo cat {output[generated_file]['path']}")
            if any(line.strip().startswith('GFM_WAIT_TIMEOUT_SEC=') and line.strip() != 'GFM_WAIT_TIMEOUT_SEC='
                   for line in content.split('\n')):
                return
            logger.info("GFM_WAIT_TIMEOUT_SEC has no value yet, sleeping for 5 seconds")
            time.sleep(5)
        assert False, "fm_config GFM_WAIT_TIMEOUT_SEC did not get a value within 60s after nmx-controller came back"


def _wait_until_chassis_mapping_file_has_content(engines, sdn):
    """Wait until chassis_mapping file is updated"""
    for _ in range(20):
        generate_output = sdn.config.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[ClusterConsts.NMX_CONTROLLER_CONFIG_CHASSIS_MAPPING].action_generate_sdn()
        installed_file = ClusterTools.get_generated_file_name(generate_output.returned_value, 'config')
        output = OutputParsingTool.parse_show_output_to_dict(sdn.config.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[ClusterConsts.NMX_CONTROLLER_CONFIG_CHASSIS_MAPPING].files.show(output_format=OutputFormat.json),
                                                             output_format=OutputFormat.json).get_returned_value()
        current_installed_config_path = output[installed_file]['path']
        content = engines.dut.run_cmd(f"sudo cat {current_installed_config_path}")
        lines = [line.strip() for line in content.strip().split("\n") if line.strip()]
        if lines:
            return current_installed_config_path
        logger.info("Sleeping for 5 seconds")
        time.sleep(5)
    assert False, "Chassis mapping file did not have content"
