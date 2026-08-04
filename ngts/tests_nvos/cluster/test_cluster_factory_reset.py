import logging
import random
import re
import time

import pytest

from ngts.nvos_constants.constants_nvos import ApiType, OutputFormat, SystemConsts, ClusterAppsLogLevels, NvosConst, \
    ImageConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports
from ngts.tests_nvos.cluster.dscp_marking_tools import DscpValueSelector
from ngts.tests_nvos.cluster.test_cluster_dscp_marking import _get_random_dscp_not_default
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.system.factory_reset.helpers import add_verification_data, \
    verify_the_setup_is_functional, get_current_time, KEEP_ONLY_FILES
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.nvos_tools.Devices.IbDevice import RosalindSwitch
from ngts.tests_nvos.cluster.tray_admin_state.helpers import is_rtf_compute_tray_scenario, delete_sdn_partitions_before_compute_tray_test

logger = logging.getLogger()


@disabled_access_ports
@pytest.mark.timeout(35 * MINUTE, func_only=True)
@pytest.mark.nmx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_cluster_default_factory_reset(engines, dut_engines, devices, test_api, has_loopbox, standalone_system, setup_name, chassis_info, tray_topology_config):

    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json

    with allure.step("Create Cluster object"):
        cluster = Cluster()
        current_time = get_current_time(engines)
        system = System()
        sdn = Sdn()
        all_state_files_paths = {}
        all_config_files_paths = {}
        initial_config_contents = {}
        sdn_files_deleted = False
        interface_wa_called = False
        uploaded_files = []
    try:
        with allure.step("Add data before reset factory"):
            username = add_verification_data(engines.dut, system)
        _, uploaded_files = reset_factory_pre_steps(engines, devices, test_api, cluster, current_time, system, sdn, all_state_files_paths, all_config_files_paths, output_format, initial_config_contents, setup_name, standalone_system)

        if not standalone_system:
            initial_partition_output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                                   output_format=output_format).get_returned_value()
            with allure.step("Create Empty partition"):
                ClusterTools.create_empty_partition(sdn, {}, engine=engines.dut)

        with allure.step("Configure custom DSCP before factory reset"):
            dscp_value, dscp_numeric = _get_random_dscp_not_default()
            logger.info(f"Setting DSCP to {dscp_value} (numeric: {dscp_numeric}) before factory reset")
            cluster.set(op_param_name=ClusterConsts.DSCP_MARKING_FIELD,
                        op_param_value=dscp_value, apply=True)

        tray_state_set = False
        if not standalone_system and isinstance(devices.dut, RosalindSwitch):
            with allure.step("Set tray maintenance-state to non-default before factory reset"):
                trays_output = OutputParsingTool.parse_show_output_to_dict(sdn.trays.show()).get_returned_value()
                tray_ids = list(trays_output.keys())
                if tray_ids:
                    selected_tray = tray_ids[0]
                    local_switch_slot = str(chassis_info.get('slot-number'))
                    partition_deletion_flag = is_rtf_compute_tray_scenario(selected_tray, local_switch_slot, tray_topology_config)
                    if partition_deletion_flag:
                        delete_sdn_partitions_before_compute_tray_test(sdn)
                    tray_non_default = random.choice(['down', 'diag'])
                    sdn.trays.action_update_maintenance_state(
                        tray_id=selected_tray, maintenance_state=tray_non_default).verify_result()
                    tray_state_set = True
                    logger.info(f"Set tray {selected_tray} to '{tray_non_default}' before factory reset")

        with allure.step("Run reset factory without params"):
            execute_reset_factory(engines, system, devices.dut.reset_factory, "", current_time)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled', nmx_c_expected_state='down', engine=engines.dut)

        with allure.step("Verify cluster in correct state"):
            verify_cluster_state_resetted(cluster)
            for app in devices.dut.expected_cluster_apps:
                ClusterTools.verify_app_is_down(engines.dut, app, devices)
            # Only check file types that were actually generated
            for file_type in all_config_files_paths.keys():
                verify_all_files_are_deleted(engines, all_config_files_paths[file_type])
            state_files = ClusterTools.get_filtered_state_files(devices, standalone_system)
            for file_type in state_files:
                if file_type in all_state_files_paths:
                    verify_all_files_are_deleted(engines, all_state_files_paths[file_type])
            ClusterTools.start_cluster(cluster, setup_name, OutputFormat.json, engine=engines.dut, devices=devices)
            ClusterTools.verify_sdn_config_files_deleted(sdn, devices, engine=engines.dut)
            ClusterTools.verify_sdn_state_files_deleted(sdn, standalone_system, devices, engine=engines.dut)
            sdn_files_deleted = True
            rotate_logs(system)
            logger.info("Sleeping for 30 seconds to gather nmx log messages")
            time.sleep(30)
            for app in devices.dut.expected_cluster_apps:
                ClusterTools.verify_log_level(ClusterAppsLogLevels.NOTICE, app, output_format, cluster, engine=engines.dut)

            interfaces_wa = ClusterTools().wa_to_get_active_interface_for_loopbox_systems(cluster, sdn, devices, engines.dut, dut_engines, has_loopbox, setup_name, standalone_system)
            next(interfaces_wa)
            interface_wa_called = True

            verify_config_files_content_not_changed(sdn, initial_config_contents, engines, devices)
            if not (is_bug_active(4207869) and standalone_system):
                ClusterTools.wait_for_app_healthy(cluster, ClusterConsts.NMX_CONTROLLER, engine=engines.dut)
            ClusterTools.wait_for_app_healthy(cluster, ClusterConsts.NMX_TELEMETRY, engine=engines.dut)
            verify_apps_in_expected_state(cluster, 'ok', has_loopbox, standalone_system, devices)

            if not standalone_system:
                output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                     output_format=output_format).get_returned_value()
                assert initial_partition_output == output, f'Partition was not restored to initial. initial: {initial_partition_output}\n current: {output}'

        with allure.step(f"Verify DSCP reset to default ({ClusterConsts.DSCP_DEFAULT_VALUE}) after factory reset"):
            show_output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=output_format), output_format=output_format
            ).get_returned_value()
            actual_dscp = show_output.get(ClusterConsts.DSCP_MARKING_FIELD)
            actual_numeric = DscpValueSelector.get_numeric_value(actual_dscp)
            assert actual_numeric == ClusterConsts.DSCP_DEFAULT_VALUE, \
                f"DSCP should be default ({ClusterConsts.DSCP_DEFAULT_VALUE}) after factory reset, " \
                f"got {actual_dscp} ({actual_numeric})"

        if tray_state_set:
            with allure.step(f"Verify tray {selected_tray} maintenance-state reset to default after factory reset"):
                tray_output = OutputParsingTool.parse_show_output_to_dict(
                    sdn.trays.tray[selected_tray].show()).get_returned_value()
                actual_tray_state = tray_output.get(ClusterConsts.MAINTENANCE_STATE)
                assert actual_tray_state == 'up', (
                    f"Tray {selected_tray}: expected 'up' after factory reset, got '{actual_tray_state}'")

    finally:
        if interface_wa_called:
            try:
                next(interfaces_wa)
            except StopIteration:
                pass  # Or handle it if necessary
        for file_path in uploaded_files:
            engines.sonic_mgmt.run_cmd(f"sudo rm -f {file_path}")
        cluster.unset(apply=True)
        ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled', nmx_c_expected_state='down', engine=engines.dut)

        with allure.step("Verify the setup is functional"):
            verify_the_setup_is_functional(system, engines, dut=devices.dut)

        if not sdn_files_deleted:
            ClusterTools.start_cluster(cluster, setup_name, OutputFormat.json, engine=engines.dut, devices=devices)
            delete_all_sdn_fetched_generated_files(engines, sdn, all_config_files_paths, all_state_files_paths, standalone_system)


@disabled_access_ports
@pytest.mark.timeout(35 * MINUTE, func_only=True)
@pytest.mark.nmx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_cluster_factory_reset_keep_basic(engines, dut_engines, devices, test_api, test_name, has_loopbox, setup_name,
                                          standalone_system, handle_la_marker_in_manufacture):
    # SAME AS DEFAULT.
    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json

    with allure.step("Create Cluster object"):
        cluster = Cluster()
        current_time = get_current_time(engines)
        system = System()
        sdn = Sdn()
        all_state_files_paths = {}
        all_config_files_paths = {}
        initial_config_contents = {}
        sdn_files_deleted = False
        interface_wa_called = False
        uploaded_files = []
    try:
        with allure.step("Add data before reset factory"):
            username = add_verification_data(engines.dut, system)
        _, uploaded_files = reset_factory_pre_steps(engines, devices, test_api, cluster, current_time, system, sdn, all_state_files_paths, all_config_files_paths, output_format, initial_config_contents, setup_name, standalone_system)

        if not standalone_system:
            initial_partition_output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                                   output_format=output_format).get_returned_value()
            with allure.step("Create Empty partition"):
                ClusterTools.create_empty_partition(sdn, {}, engine=engines.dut)

        with allure.step("Run reset factory keep basic param"):
            execute_reset_factory(engines, system, devices.dut.reset_factory, "keep basic", current_time)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled', nmx_c_expected_state='down', engine=engines.dut)

        with allure.step("Verify cluster in correct state"):
            verify_cluster_state_resetted(cluster)
            for app in devices.dut.expected_cluster_apps:
                ClusterTools.verify_app_is_down(engines.dut, app, devices)
            # Only check file types that were actually generated
            for file_type in all_config_files_paths.keys():
                verify_all_files_are_deleted(engines, all_config_files_paths[file_type])
            state_files = ClusterTools.get_filtered_state_files(devices, standalone_system)
            for file_type in state_files:
                if file_type in all_state_files_paths:
                    verify_all_files_are_deleted(engines, all_state_files_paths[file_type])
            ClusterTools.start_cluster(cluster, setup_name, OutputFormat.json, engine=engines.dut, devices=devices)
            ClusterTools.verify_sdn_config_files_deleted(sdn, devices, engine=engines.dut)
            ClusterTools.verify_sdn_state_files_deleted(sdn, standalone_system, devices, engine=engines.dut)
            sdn_files_deleted = True
            rotate_logs(system)
            logger.info("Sleeping for 30 seconds to gather nmx log messages")
            time.sleep(30)
            for app in devices.dut.expected_cluster_apps:
                ClusterTools.verify_log_level(ClusterAppsLogLevels.NOTICE, app, output_format, cluster, engine=engines.dut)

            interfaces_wa = ClusterTools().wa_to_get_active_interface_for_loopbox_systems(cluster, sdn, devices, engines.dut, dut_engines, has_loopbox, setup_name, standalone_system)
            next(interfaces_wa)
            interface_wa_called = True
            verify_config_files_content_not_changed(sdn, initial_config_contents, engines, devices)
            if not (is_bug_active(4207869) and standalone_system):
                ClusterTools.wait_for_app_healthy(cluster, ClusterConsts.NMX_CONTROLLER, engine=engines.dut)
            ClusterTools.wait_for_app_healthy(cluster, ClusterConsts.NMX_TELEMETRY, engine=engines.dut)
            verify_apps_in_expected_state(cluster, 'ok', has_loopbox, standalone_system, devices)

            if not standalone_system:
                output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                     output_format=output_format).get_returned_value()
                assert initial_partition_output == output, f'Partition was not restored to initial. initial: {initial_partition_output}\n current: {output}'

    finally:
        if interface_wa_called:
            try:
                next(interfaces_wa)
            except StopIteration:
                pass  # Or handle it if necessary
        for file_path in uploaded_files:
            engines.sonic_mgmt.run_cmd(f"sudo rm -f {file_path}")

        with allure.step("Verify the setup is functional"):
            verify_the_setup_is_functional(system, engines, dut=devices.dut)

        if not sdn_files_deleted:
            ClusterTools.start_cluster(cluster, setup_name, OutputFormat.json, engine=engines.dut, devices=devices)
            delete_all_sdn_fetched_generated_files(engines, sdn, all_config_files_paths, all_state_files_paths, standalone_system)


@disabled_access_ports
@pytest.mark.timeout(35 * MINUTE, func_only=True)
@pytest.mark.nmx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_cluster_factory_keep_only_files(engines, dut_engines, devices, test_api, test_name, has_loopbox, setup_name, standalone_system):
    # SAME
    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json

    with allure.step("Create Cluster object"):
        cluster = Cluster()
        current_time = get_current_time(engines)
        system = System()
        sdn = Sdn()
        all_state_files_paths = {}
        all_config_files_paths = {}
        initial_config_contents = {}
        sdn_files_deleted = False
        interface_wa_called = False
        uploaded_files = []
    try:
        with allure.step("Add data before reset factory"):
            username = add_verification_data(engines.dut, system)
        _, uploaded_files = reset_factory_pre_steps(engines, devices, test_api, cluster, current_time, system, sdn, all_state_files_paths, all_config_files_paths, output_format, initial_config_contents, setup_name, standalone_system)

        if not standalone_system:
            initial_partition_output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                                   output_format=output_format).get_returned_value()
            with allure.step("Create Empty partition"):
                ClusterTools.create_empty_partition(sdn, {}, engine=engines.dut)

        with allure.step("Run reset factory with keep only-files param"):
            execute_reset_factory(engines, system, devices.dut.reset_factory, KEEP_ONLY_FILES, current_time)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled', nmx_c_expected_state='down', engine=engines.dut)

        with allure.step("Verify cluster in correct state"):
            verify_cluster_state_resetted(cluster)
            for app in devices.dut.expected_cluster_apps:
                ClusterTools.verify_app_is_down(engines.dut, app, devices)
            # Only check file types that were actually generated
            for file_type in all_config_files_paths.keys():
                verify_all_files_are_deleted(engines, all_config_files_paths[file_type])
            state_files = ClusterTools.get_filtered_state_files(devices, standalone_system)
            for file_type in state_files:
                if file_type in all_state_files_paths:
                    verify_all_files_are_deleted(engines, all_state_files_paths[file_type])
            ClusterTools.start_cluster(cluster, setup_name, OutputFormat.json, engine=engines.dut, devices=devices)
            ClusterTools.verify_sdn_config_files_deleted(sdn, devices, engine=engines.dut)
            ClusterTools.verify_sdn_state_files_deleted(sdn, standalone_system, devices, engine=engines.dut)
            sdn_files_deleted = True
            rotate_logs(system)
            logger.info("Sleeping for 30 seconds to gather nmx log messages")
            time.sleep(30)
            for app in devices.dut.expected_cluster_apps:
                ClusterTools.verify_log_level(ClusterAppsLogLevels.NOTICE, app, output_format, cluster, engine=engines.dut)

            interfaces_wa = ClusterTools().wa_to_get_active_interface_for_loopbox_systems(cluster, sdn, devices, engines.dut, dut_engines, has_loopbox, setup_name, standalone_system)
            next(interfaces_wa)
            interface_wa_called = True
            verify_config_files_content_not_changed(sdn, initial_config_contents, engines, devices)
            if not (is_bug_active(4207869) and standalone_system):
                ClusterTools.wait_for_app_healthy(cluster, ClusterConsts.NMX_CONTROLLER, engine=engines.dut)
            ClusterTools.wait_for_app_healthy(cluster, ClusterConsts.NMX_TELEMETRY, engine=engines.dut)
            verify_apps_in_expected_state(cluster, 'ok', has_loopbox, standalone_system, devices)

            if not standalone_system:
                output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                     output_format=output_format).get_returned_value()
                assert initial_partition_output == output, f'Partition was not restored to initial. initial: {initial_partition_output}\n current: {output}'

    finally:
        if interface_wa_called:
            try:
                next(interfaces_wa)
            except StopIteration:
                pass  # Or handle it if necessary
        for file_path in uploaded_files:
            engines.sonic_mgmt.run_cmd(f"sudo rm -f {file_path}")

        with allure.step("Verify the setup is functional"):
            verify_the_setup_is_functional(system, engines, dut=devices.dut)

        if not sdn_files_deleted:
            ClusterTools.start_cluster(cluster, setup_name, OutputFormat.json, engine=engines.dut, devices=devices)
            delete_all_sdn_fetched_generated_files(engines, sdn, all_config_files_paths, all_state_files_paths, standalone_system)


@disabled_access_ports
@pytest.mark.timeout(50 * MINUTE, func_only=True)
@pytest.mark.nmx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_cluster_factory_reset_keep_all_config(engines, dut_engines, devices, test_api, test_name, has_loopbox, setup_name,
                                               standalone_system):
    # Only fetched and generated files will be cleaned.
    # SAME
    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json

    with allure.step("Create Cluster object"):
        cluster = Cluster()
        current_time = get_current_time(engines)
        system = System()
        sdn = Sdn()
        all_state_files_paths = {}
        all_config_files_paths = {}
        log_level = ''
        initial_config_contents = {}
        sdn_files_deleted = False
        interface_wa_called = False
        uploaded_files = []
    try:
        with allure.step("Add data before reset factory"):
            username = add_verification_data(engines.dut, system)
        log_level, uploaded_files = reset_factory_pre_steps(engines, devices, test_api, cluster, current_time, system, sdn, all_state_files_paths, all_config_files_paths, output_format, initial_config_contents, setup_name, standalone_system)

        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

        if not standalone_system:
            with allure.step("Create Empty partition"):
                ClusterTools.create_empty_partition(sdn, {}, engine=engines.dut)

        with allure.step("Run reset factory with keep all-config param"):
            execute_reset_factory(engines, system, devices.dut.reset_factory, "keep all-config", current_time)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state='up', engine=engines.dut)

        with allure.step("Verify cluster in correct state"):
            cluster_state = ClusterTools.check_cluster_state(cluster, output_format)
            assert cluster_state == NvosConst.ENABLED, f"Expected cluster state {NvosConst.ENABLED}, Actual {cluster_state}"
            for app in devices.dut.expected_cluster_apps:
                ClusterTools.verify_app_is_up(engines.dut, app)  # Verify apps are running
            # Only check file types that were actually generated
            for file_type in all_config_files_paths.keys():
                verify_all_files_are_deleted(engines, all_config_files_paths[file_type])
            state_files = ClusterTools.get_filtered_state_files(devices, standalone_system)
            for file_type in state_files:
                if file_type in all_state_files_paths:
                    verify_all_files_are_deleted(engines, all_state_files_paths[file_type])
            ClusterTools.verify_sdn_config_files_deleted(sdn, devices, engine=engines.dut)
            ClusterTools.verify_sdn_state_files_deleted(sdn, standalone_system, devices, engine=engines.dut)
            sdn_files_deleted = True
            rotate_logs(system)
            time.sleep(30)
            for app in devices.dut.expected_cluster_apps:
                ClusterTools.verify_log_level(log_level, app, output_format, cluster, engine=engines.dut)
            # Not expecting content to change.
            for file_type in initial_config_contents.keys():
                # transform only where the guarded edit actually modified the file - on files that
                # already carried the injected section the edit no-ops and initial stays as-is
                if not ClusterTools.config_edit_expected_to_change(file_type, initial_config_contents[file_type]):
                    continue
                transformation_fn = ClusterConsts.CONFIG_FILES_CONTENT_CHANGE.get(file_type, lambda x: x)
                initial_config_contents[file_type] = transformation_fn(initial_config_contents[file_type])

            interfaces_wa = ClusterTools().wa_to_get_active_interface_for_loopbox_systems(cluster, sdn, devices, engines.dut, dut_engines, has_loopbox, setup_name, standalone_system)
            next(interfaces_wa)
            interface_wa_called = True
            verify_config_files_content_not_changed(sdn, initial_config_contents, engines, devices)
            if not (is_bug_active(4207869) and standalone_system):
                ClusterTools.wait_for_app_healthy(cluster, ClusterConsts.NMX_CONTROLLER, engine=engines.dut)
            ClusterTools.wait_for_app_healthy(cluster, ClusterConsts.NMX_TELEMETRY, engine=engines.dut)
            verify_apps_in_expected_state(cluster, 'ok', has_loopbox, standalone_system, devices)  # Apps should be running

            if not standalone_system:
                output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                     output_format=output_format).get_returned_value()
                assert ClusterConsts.EMPTY_PARTITION_ID in output.keys(), f'Partition {ClusterConsts.EMPTY_PARTITION_ID} was deleted, while its expected to be kept'
    finally:
        if interface_wa_called:
            try:
                next(interfaces_wa)
            except StopIteration:
                pass  # Or handle it if necessary
        if not standalone_system:
            with allure.step("Running sdn factory reset"):
                ClusterTools.reset_sdn_factory_default_and_wait_for_restart(sdn, cluster, engine=engines.dut)

        for file_path in uploaded_files:
            engines.sonic_mgmt.run_cmd(f"sudo rm -f {file_path}")
        for app in devices.dut.expected_cluster_apps:
            cluster.apps.app_name[app].loglevel.action_restore_cluster(engine=engines.dut)

        with allure.step("Verify the setup is functional"):
            verify_the_setup_is_functional(system, engines, dut=devices.dut)

        if not sdn_files_deleted:
            ClusterTools.start_cluster(cluster, setup_name, OutputFormat.json, engine=engines.dut, devices=devices)
            delete_all_sdn_fetched_generated_files(engines, sdn, all_config_files_paths, all_state_files_paths, standalone_system)

        with allure.step("Run reset factory to get back to default configuration"):
            execute_reset_factory(engines, system, devices.dut.reset_factory, "", current_time)


def execute_reset_factory(engines, system, operation, flag, current_time):
    logging.info("Current time: " + str(current_time))
    system.factory_default.action_reset(engine=engines.dut, operation=operation, param=flag).verify_result()


def verify_cluster_state_resetted(cluster):
    with allure.step("Running 'nv show cluster' command and parsing output"):
        output = OutputParsingTool.parse_show_output_to_dict(
            cluster.show(output_format=OutputFormat.json),
            output_format=OutputFormat.json).get_returned_value()
        with allure.step("Validate initial state is disabled"):
            assert output[SystemConsts.STATE] == NvosConst.DISABLED, f"initial state is , " \
                f"{output[SystemConsts.STATE]}, Expected to be: " \
                f"{NvosConst.DISABLED}"


def verify_apps_in_expected_state(cluster, status, has_loopbox, standalone, devices):
    with allure.step("Running 'nv show cluster apps running' command and verifying output"):
        output = OutputParsingTool.parse_show_output_to_dict(
            cluster.apps.running.show(output_format=OutputFormat.json),
            output_format=OutputFormat.json).get_returned_value()
        for app in devices.dut.expected_cluster_apps:
            if standalone and app == ClusterConsts.NMX_CONTROLLER:
                continue
            app_status = output[app]['status']
            assert app_status == status, f"App {app} status is {app_status} instead of {status}"


def get_generated_file_name(output, file_type):
    # Regular expression to match the file name
    match = re.search(rf'App {file_type} file (\S+) is successfully generated', output)
    file_name = None
    # Extract the file name if found
    assert match, f"File was not generated successfully"
    if match:
        file_name = match.group(1)
        logger.info(f"Extracted file name: {file_name}")
    return file_name


def verify_all_files_are_deleted(engines, files_list):
    for file_path in files_list:
        output = engines.dut.run_cmd(f"ls {file_path}")
        assert "No such file or directory" in output, "File was found, not expected to be found"


def reset_factory_pre_steps(engines, devices, test_api, cluster, current_time, system, sdn, all_state_files_paths, all_config_files_paths, output_format, initial_config_contents, setup_name, standalone_system):

    logger.info("Setting cluster state to enabled")
    ClusterTools.start_cluster(cluster, setup_name, output_format, engine=engines.dut, devices=devices)
    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, app=ClusterConsts.NMX_CONTROLLER,
                                                     standalone_system=standalone_system, engine=engines.dut)

    # Get config files paths for all apps that exist on this device type
    config_files_paths = ClusterTools.get_all_apps_config_files_paths(sdn, devices, engine=engines.dut)

    with allure.step("Choose random log level, and set cluster app log level to and start app"):
        log_level = random.choice(ClusterConsts.ClusterAppsLogLevelsList)
        for app in devices.dut.expected_cluster_apps:
            cluster.apps.app_name[app].loglevel.action_update_cluster_log_level(level=log_level)

    for file_type, file_path in config_files_paths.items():
        initial_config_contents[file_type] = engines.dut.run_cmd("sudo cat {}".format(file_path))

    initial_configs_paths_to_restore = {}
    path_to_config = {}
    config_file_name = {}
    uploaded_files = []

    # Only iterate over file types that actually exist on this device
    chassis_sn = ClusterTools.get_chassis_sn()
    for file_type in config_files_paths.keys():
        app = ClusterConsts.MAP_CONFIG_FILE_TYPE_TO_APP[file_type]
        sdn.config.apps.app_name[app].type.file_type[file_type].files.file_name[
            config_files_paths[file_type].split('/')[-1]].action_upload(
            ImageConsts.SCP_PATH + ClusterConsts.INITIAL_CONFIGURATIONS_PATH).verify_result()
        initial_configs_paths_to_restore[file_type] = ClusterConsts.INITIAL_CONFIGURATIONS_PATH + '/' + config_files_paths[file_type].split('/')[-1]
        uploaded_files.append(ClusterConsts.INITIAL_CONFIGURATIONS_PATH + '/' + config_files_paths[file_type].split('/')[-1])
        logger.info(f"Uploading files: {initial_configs_paths_to_restore[file_type]}")

        file_name = 'dummy_' + (initial_configs_paths_to_restore[file_type]).split('/')[-1]
        dummy_file_path = ClusterConsts.INITIAL_CONFIGURATIONS_PATH + '/' + file_name
        engines.sonic_mgmt.run_cmd("sudo cp {} {}".format(initial_configs_paths_to_restore[file_type], dummy_file_path))
        uploaded_files.append(ClusterConsts.INITIAL_CONFIGURATIONS_PATH + '/' + file_name)
        with allure.step("Change initial content"):
            edit_cmd = ClusterConsts.CONFIG_FILES_CHANGE[file_type].format(
                file_path=dummy_file_path, chassis_sn=chassis_sn)
            engines.sonic_mgmt.run_cmd(edit_cmd)
        path_to_config[file_type] = dummy_file_path
        config_file_name[file_type] = file_name
    with allure.step("Fetch & Generate config files"):
        # Only iterate over file types that actually exist on this device
        for file_type in config_files_paths.keys():
            app = ClusterConsts.MAP_CONFIG_FILE_TYPE_TO_APP[file_type]
            sdn.config.apps.app_name[app].type.file_type[file_type].action_fetch_sdn(ImageConsts.SCP_PATH + path_to_config[file_type])
            output = sdn.config.apps.app_name[app].type.file_type[file_type].action_generate_sdn()

    with allure.step("Generate state files"):
        # Get state files for device's apps
        state_file_types = []
        for app in devices.dut.expected_cluster_apps:
            app_state_files = devices.dut.cluster_state_files_by_app.get(app, [])
            state_file_types.extend(app_state_files)

        for file_type in state_file_types:
            if standalone_system and file_type == 'topology':
                continue
            app = ClusterConsts.MAP_STATE_FILE_TYPE_TO_APP[file_type]
            output = sdn.state.apps.app_name[app].type.file_type[file_type].action_generate_sdn()
            output = OutputParsingTool.parse_show_output_to_dict(sdn.state.apps.app_name[app].type.file_type[file_type].files.show(output_format=output_format),
                                                                 output_format=output_format).get_returned_value()
            all_state_files_paths[file_type] = [item['path'] for item in output.values()]

    with allure.step("Install config file"):
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

            current_config_set = set(line.strip() for line in current_config_content.strip().split('\n') if line.strip())
            expected_config_set = set(line.strip() for line in expected_config_content.strip().split('\n') if line.strip())
            if file_type == 'chassis_mapping' and is_bug_active(4222718):
                continue
            assert current_config_set == expected_config_set, f"Configuration mismatch:\nCurrent: {current_config_set}\nExpected: {expected_config_set}"

    return log_level, uploaded_files


def rotate_logs(system):
    with allure.step("Rotate logs"):
        logging.info("Rotate logs")
        system.log.rotate_logs()


def _filter_config_lines(content, file_type):
    # drop empty/whitespace-only lines so trailing-newline artifacts (cat vs netmiko prompt
    # stripping vs the CONFIG_FILES_CONTENT_CHANGE transformations) never fail the comparison
    lines = {line.strip() for line in content.split('\n') if line.strip()}
    if file_type == 'fm_config':
        lines = {line for line in lines if not line.startswith('LOG_LEVEL=') and
                 not line.startswith('GFM_WAIT_TIMEOUT_SEC=')}
    return lines


def verify_config_files_content_not_changed(sdn, initial_config_contents, engines, devices):
    errors_list = []
    current_config_files_content = {}
    config_files_paths = ClusterTools.get_all_apps_config_files_paths(sdn, devices, engine=engines.dut)

    for file_type, file_path in config_files_paths.items():
        current_config_files_content[file_type] = engines.dut.run_cmd("sudo cat {}".format(file_path))
    assert len(current_config_files_content) == len(initial_config_contents), 'Missing configs'
    for file_type, current_file_content in current_config_files_content.items():
        if file_type in ('chassis_mapping', 'sm_config'):
            continue
        init_file_content = initial_config_contents.get(file_type)
        if _filter_config_lines(current_file_content, file_type) != _filter_config_lines(init_file_content, file_type):
            errors_list.append(f"Configuration mismatch in file {file_type}:\nInitial: {init_file_content}\nCurrent: {current_file_content}")
    assert not errors_list, "\n\n".join(errors_list)


def delete_all_sdn_fetched_generated_files(engines, sdn, all_config_files_paths, all_state_files_paths, standalone_system):
    with allure.step("Delete state/config Files"):
        # Only iterate over file types that were actually generated
        for file_type in all_config_files_paths.keys():
            if all_config_files_paths.get(file_type):
                for file in all_config_files_paths[file_type]:
                    app = ClusterConsts.MAP_CONFIG_FILE_TYPE_TO_APP[file_type]
                    file = file.split('/')[-1]
                    try:
                        sdn.config.apps.app_name[app].type.file_type[file_type].files.file_name[file].action_delete().verify_result()
                    except Exception as e:
                        logger.info("File Already Deleted")
        # Only iterate over state file types that were actually generated
        for file_type in all_state_files_paths.keys():
            if standalone_system and file_type == 'topology':
                continue
            if all_state_files_paths.get(file_type):
                for file in all_state_files_paths[file_type]:
                    app = ClusterConsts.MAP_STATE_FILE_TYPE_TO_APP[file_type]
                    file = file.split('/')[-1]
                    try:
                        sdn.state.apps.app_name[app].type.file_type[file_type].files.file_name[file].action_delete().verify_result()
                    except Exception as e:
                        logger.info("File Already Deleted")
