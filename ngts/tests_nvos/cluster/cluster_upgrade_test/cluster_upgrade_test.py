import logging
import random
import time
import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import SystemConsts, OutputFormat, ApiType, NvosConst, ImageConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_tools.system.System import System
from ngts.scripts.sonic_deploy.nvos_only_methods import NvosInstallationSteps
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.nvos_tools.infra.IpTool import IpTool

logger = logging.getLogger()


@pytest.mark.nmx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
@pytest.mark.timeout(55 * MINUTE, func_only=True)
def test_upgrade_with_nmx_enabled(test_api, devices, topology_obj, setup_name, engines, dut_engines, has_loopbox, standalone_system,
                                  base_version_realpath, target_version_realpath, handle_la_marker_in_manufacture):
    '''
    Test will install a base version (Taken from regression).
    On base version perform the following:
        1. Enabled cluster.
        2. Configure log level.
        3. fetch config files.
        4. install config files.
        5. Perform upgrade.
        6. Verify cluster still enabled, log level still changed, and fetched/installed files are still here.
    Cleanup:
        1. Includes disabling the cluster.
        2. Deleting fetched files.
        3. Installing initial control plane files.
        4. Restore log level.
    '''
    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json
    try:
        interface_wa_called = False
        target_image_installed = False
        cli_obj = NvueGeneralCli(engines.dut, devices.dut)

        NvueGeneralCli(engines.dut, devices.dut).install_image_via_onie(topology_obj, base_version_realpath)
        TestToolkit.engines.dut.disconnect()
        with allure.step("Create Cluster object"):
            cluster = Cluster()
            system = System()
            sdn = Sdn()
            all_config_files_paths = {}
            initial_config_contents = {}
            initial_configs_paths_to_restore = {}
            log_levels = {}
            uploaded_files = []
            initial_configuration_restored = False

        with allure.step("Running 'nv show cluster' command and parsing output"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=output_format),
                output_format=output_format).get_returned_value()
            with allure.step("Validate initial state is disabled"):
                assert output[SystemConsts.STATE] == NvosConst.DISABLED, f"initial state is , " \
                    f"{output[SystemConsts.STATE]}, Expected to be: " \
                    f"{NvosConst.DISABLED}"

        with allure.step("Enable cluster and perform configurations"):
            ClusterTools.start_cluster(cluster, setup_name, output_format, verify_nmx_c=False, engine=engines.dut, devices=devices)  # remove verify=False once base version for regression is different than 1638.

            interfaces_wa = ClusterTools().wa_to_get_active_interface_for_loopbox_systems(cluster, sdn, devices, engines.dut, dut_engines, has_loopbox, setup_name, standalone_system)
            next(interfaces_wa)
            interface_wa_called = True

            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, app=ClusterConsts.NMX_CONTROLLER,
                                                             standalone_system=standalone_system, engine=engines.dut)

            with allure.step("Choose random log level, and set cluster app log level to"):
                for app in devices.dut.expected_cluster_apps:
                    log_level = random.choice(ClusterConsts.ClusterAppsLogLevelsList)
                    cluster.apps.app_name[app].loglevel.action_update_cluster_log_level(level=log_level)
                    log_levels[app] = log_level

            # Get config files paths for all apps that exist on this device type
            config_files_paths = ClusterTools.get_all_apps_config_files_paths(sdn, devices, engine=engines.dut)

            # Initialize dicts with ONLY file types that exist on this device
            path_to_config = {config_type: '' for config_type in config_files_paths.keys()}
            config_file_name = {config_type: '' for config_type in config_files_paths.keys()}

            for file_type, file_path in config_files_paths.items():
                initial_config_contents[file_type] = engines.dut.run_cmd("sudo cat {}".format(file_path))

            with allure.step('Upload initial configurations'):
                # Only iterate over file types that actually exist on this device
                chassis_sn = ClusterTools.get_chassis_sn()
                for file_type in config_files_paths.keys():
                    app = ClusterConsts.MAP_CONFIG_FILE_TYPE_TO_APP[file_type]
                    sdn.config.apps.app_name[app].type.file_type[file_type].files.file_name[
                        config_files_paths[file_type].split('/')[-1]].action_upload(
                        ImageConsts.SCP_PATH + ClusterConsts.INITIAL_CONFIGURATIONS_PATH).verify_result()
                    initial_configs_paths_to_restore[file_type] = ClusterConsts.INITIAL_CONFIGURATIONS_PATH + '/' + config_files_paths[file_type].split('/')[-1]
                    logger.info(f"Uploading files: {initial_configs_paths_to_restore[file_type]}")
                    uploaded_files.append(ClusterConsts.INITIAL_CONFIGURATIONS_PATH + '/' + config_files_paths[file_type].split('/')[-1])

                    # Create a dummy config file.
                    file_name = 'dummy_' + (initial_configs_paths_to_restore[file_type]).split('/')[-1]
                    dummy_file_path = ClusterConsts.INITIAL_CONFIGURATIONS_PATH + '/' + file_name
                    engines.sonic_mgmt.run_cmd("sudo cp {} {}".format(initial_configs_paths_to_restore[file_type], dummy_file_path))
                    uploaded_files.append(ClusterConsts.INITIAL_CONFIGURATIONS_PATH + '/' + file_name)
                    edit_cmd = ClusterConsts.CONFIG_FILES_CHANGE[file_type].format(
                        file_path=dummy_file_path, chassis_sn=chassis_sn)
                    engines.sonic_mgmt.run_cmd(edit_cmd)
                    path_to_config[file_type] = dummy_file_path
                    config_file_name[file_type] = file_name

            with allure.step("Install config file"):
                non_preserved_configs = []
                # Only iterate over file types that actually exist on this device
                for file_type in config_files_paths.keys():
                    app = ClusterConsts.MAP_CONFIG_FILE_TYPE_TO_APP[file_type]
                    sdn.config.apps.app_name[app].type.file_type[file_type].action_fetch_sdn(ImageConsts.SCP_PATH + path_to_config[file_type])
                    sdn.config.apps.app_name[app].type.file_type[file_type].files.file_name[config_file_name[file_type]].action_install(reboot_params=False, force=False)
                    output = sdn.config.apps.app_name[app].type.file_type[file_type].action_generate_sdn()
                    installed_file = ClusterTools.get_generated_file_name(output.returned_value, 'config')
                    output = OutputParsingTool.parse_show_output_to_dict(sdn.config.apps.app_name[app].type.file_type[file_type].files.show(output_format=output_format),
                                                                         output_format=output_format).get_returned_value()
                    all_config_files_paths[file_type] = [item['path'] for item in output.values()]
                    current_installed_config_path = output[installed_file]['path']
                    current_config_content = engines.dut.run_cmd("sudo cat {}".format(current_installed_config_path))
                    expected_config_content = engines.sonic_mgmt.run_cmd("sudo cat {}".format(path_to_config[file_type]))
                    if file_type == 'chassis_mapping' and is_bug_active(4222718):
                        continue
                    assert set(current_config_content.split('\n')) == set(expected_config_content.split('\n')), f"Config file was not loaded properly. Expected content {expected_config_content}, Actual content: {current_config_content}"
                    if ClusterTools.config_edit_expected_to_change(file_type, initial_config_contents[file_type]):
                        if set(current_config_content.split('\n')) == set((initial_config_contents[file_type]).split('\n')):
                            non_preserved_configs.append(f"Configuration was restored to initial state and not saved during upgrade. init: {initial_config_contents[file_type]}, \ncurrent{current_config_content}")
                assert not non_preserved_configs, "\n\n".join(non_preserved_configs)

        if not standalone_system:
            with allure.step("Creating Empty partition, then adding a GPU to it with no-reroute option"):
                logger.info("After upgrade, empty partition should persist, but GPU added to it with no-reroute should be deleted")
                uuid, location, _, partition_to_remove_from = ClusterTools.create_empty_partition_and_add_gpu(sdn, 'no-reroute', engine=engines.dut)
        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

        with allure.step("Before upgrade, verify apps are running"):
            ClusterTools.verify_apps_running(engines.dut, devices, cluster, 'ok', output_format,
                                             standalone_system, has_loopbox)

        with allure.step("Performing upgrade:"):
            bin_filename = target_version_realpath.split('/')[-1]
            system = System()
            sonic_mgmt_engine = topology_obj.players['sonic-mgmt']['engine']
            scp_host_creds = f'{sonic_mgmt_engine.username}:{sonic_mgmt_engine.password}@{IpTool.format_ip_for_uri(sonic_mgmt_engine)}'
            NvosInstallationSteps.upgrade_to_target_version(bin_filename, cli_obj.engine, cli_obj.device, scp_host_creds,
                                                            system,
                                                            target_version_realpath, topology_obj)

            with allure.step('disconnect dut engine'):
                TestToolkit.engines.dut.disconnect()  # if install succeeded, need to replace dut engine
            target_image_installed = True

        with allure.step("Install sm_config after upgrade"):
            sm_result = ClusterTools.edit_sm_config(sdn, engines.dut, devices, standalone_system, has_loopbox)
            if sm_result:
                # Apply device-specific workaround after SM config is installed (e.g., Rosalind Bug 4910763)
                if hasattr(devices.dut, 'wa_restart_nv_bridge_after_sm_config'):
                    devices.dut.wa_restart_nv_bridge_after_sm_config(cluster, engines.dut)

        with allure.step("Running 'nv show cluster' command and parsing output"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=output_format),
                output_format=output_format).get_returned_value()
            with allure.step("Validate enabled state is preserved after upgrade"):
                assert output[SystemConsts.STATE] == NvosConst.ENABLED, f"initial state after upgrade is , " \
                    f"{output[SystemConsts.STATE]}, Expected to be: " \
                    f"{NvosConst.ENABLED}"

        ClusterTools.reboot_compute_nodes_gpus(setup_name)

        with allure.step("Validate apps are still running"):
            ClusterTools.verify_apps_running(engines.dut, devices, cluster, 'ok', output_format, standalone_system,
                                             has_loopbox, retries=6)
        with allure.step("Check log level"):
            for app in devices.dut.expected_cluster_apps:
                ClusterTools.verify_log_level(log_levels[app], app, output_format, cluster, engine=engines.dut)

        with allure.step("Make sure config is saved"):
            # Only iterate over file types that were actually configured
            for file_type in initial_config_contents.keys():
                app = ClusterConsts.MAP_CONFIG_FILE_TYPE_TO_APP[file_type]
                output = sdn.config.apps.app_name[app].type.file_type[file_type].action_generate_sdn()
                installed_file = ClusterTools.get_generated_file_name(output.returned_value, 'config')
                output = OutputParsingTool.parse_show_output_to_dict(sdn.config.apps.app_name[app].type.file_type[file_type].files.show(output_format=output_format),
                                                                     output_format=output_format).get_returned_value()
                all_config_files_paths[file_type] = [item['path'] for item in output.values()]
                current_installed_config_path = output[installed_file]['path']
                current_config_content = engines.dut.run_cmd("sudo cat {}".format(current_installed_config_path))
                expected_config_content = engines.sonic_mgmt.run_cmd("sudo cat {}".format(path_to_config[file_type]))
                # only meaningful when the guarded edit actually injected the marker line
                if not ClusterTools.config_edit_expected_to_change(file_type, initial_config_contents[file_type]):
                    continue
                if file_type == 'chassis_mapping' and is_bug_active(4222718):
                    continue
                assert ClusterConsts.EXPECTED_LINE_TO_BE_PRESERVED_AFTER_UPGRADE[file_type] in current_config_content, \
                    f"Config file was not loaded properly. Expected content {expected_config_content}, Actual content: " \
                    f"{current_config_content}. \n " \
                    f"{ClusterConsts.EXPECTED_LINE_TO_BE_PRESERVED_AFTER_UPGRADE[file_type]} was not preserved"

        if not standalone_system:
            with allure.step("checking partitions are preserved after upgrade"):
                output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                     output_format=output_format).get_returned_value()
                assert ClusterConsts.EMPTY_PARTITION_ID in output.keys(), f'Partition {ClusterConsts.EMPTY_PARTITION_ID} was deleted, while its expected to be kept'
                output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.partition_id[ClusterConsts.EMPTY_PARTITION_ID].show(output_format=output_format),
                                                                     output_format=output_format).get_returned_value()
                uuids, locations = ClusterTools.uuid_location_in_partition(sdn, partition_to_remove_from, engine=engines.dut)
                assert uuid not in uuids, f"uuid {uuid} was not deleted from {partition_to_remove_from} although it was removed with no-reroute, See current uuids: {uuids}"
                assert location not in locations, f"uuid {uuid} was not deleted from {partition_to_remove_from} although it was removed with no-reroute. See current locations: {locations}"

    finally:
        if not standalone_system:
            with allure.step("Running sdn factory reset"):
                ClusterTools.reset_sdn_factory_default_and_wait_for_restart(sdn, cluster, engine=engines.dut)

        if not target_image_installed:
            NvueGeneralCli(engines.dut, devices.dut).install_image_via_onie(topology_obj, target_version_realpath)
            TestToolkit.engines.dut.disconnect()
        else:
            with allure.step("Install initial configurations"):
                # Only iterate over file types that were actually saved
                skipped_empty_restores = False
                for file_type in initial_configs_paths_to_restore.keys():
                    if not initial_config_contents.get(file_type, '').strip():
                        # nmx-c rejects installing empty config files (bug 6447101) - the empty
                        # initial state is restored by sdn factory reset instead
                        logger.info(f"Skipping restore of {file_type}: initial file was empty")
                        skipped_empty_restores = True
                        continue
                    app = ClusterConsts.MAP_CONFIG_FILE_TYPE_TO_APP[file_type]
                    sdn.config.apps.app_name[app].type.file_type[file_type].action_fetch_sdn(initial_configs_paths_to_restore[file_type])
                    conf_file_name = initial_configs_paths_to_restore[file_type].split('/')[-1]
                    sdn.config.apps.app_name[app].type.file_type[file_type].files.file_name[conf_file_name].action_install(reboot_params=False, force=False)

            with allure.step("Delete state/config Files"):
                # Only iterate over file types that were actually generated
                for file_type in all_config_files_paths.keys():
                    if all_config_files_paths[file_type]:
                        for file in all_config_files_paths[file_type]:
                            app = ClusterConsts.MAP_CONFIG_FILE_TYPE_TO_APP[file_type]
                            file = file.split('/')[-1]
                            sdn.config.apps.app_name[app].type.file_type[file_type].files.file_name[
                                file].action_delete().verify_result()
                            engines.sonic_mgmt.run_cmd(f"sudo rm -rf {initial_configs_paths_to_restore[file_type]}")
                for file_path in uploaded_files:
                    engines.sonic_mgmt.run_cmd(f"sudo rm -f {file_path}")

            if standalone_system and skipped_empty_restores:
                # on standalone the factory reset at the top of the teardown did not run, and the
                # empty initial files could not be reinstalled - restore them via sdn factory reset
                with allure.step("Running sdn factory reset to restore empty initial config files"):
                    ClusterTools.reset_sdn_factory_default_and_wait_for_restart(sdn, cluster, engine=engines.dut)

            with allure.step("Restore log level"):
                for app in devices.dut.expected_cluster_apps:
                    if app in log_levels:
                        cluster.apps.app_name[app].loglevel.action_restore_cluster()

            if interface_wa_called:
                try:
                    next(interfaces_wa)
                except StopIteration:
                    pass  # Or handle it if necessary

            else:
                cluster.unset(apply=True)
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled', nmx_c_expected_state='down', engine=engines.dut)


def get_next_partition_id(partition_id):
    return ImageConsts.PARTITION2_IMG if partition_id == ImageConsts.PARTITION1_IMG else ImageConsts.PARTITION1_IMG
