import pytest
import logging
import pexpect
import random

from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.cluster.cluster_tools import disabled_access_ports
from ngts.tests_nvos.cluster.dscp_marking_tools import DscpValueSelector
from ngts.tests_nvos.cluster.test_cluster_dscp_marking import _get_random_dscp_not_default
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_tools.Devices.IbDevice import RosalindSwitch
from retry.api import retry_call
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.cluster.tray_admin_state.helpers import is_rtf_compute_tray_scenario, delete_sdn_partitions_before_compute_tray_test
from ngts.tests_nvos.cluster.tray_admin_state.conftest import *

logger = logging.getLogger()


@disabled_access_ports
@pytest.mark.system
@pytest.mark.nvos_build
@pytest.mark.nmx
@pytest.mark.timeout(35 * MINUTE, func_only=True)
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_reboot_command(engines, devices, test_name, test_api, has_loopbox, standalone_system, setup_name, chassis_info, tray_topology_config):
    """
    Test flow:
        1. Enable cluster
        2. Configure custom DSCP, apply and save
        3. Verify LIDs > 0 and links Active prior to reboot
        4. Run nv action reboot system
        5. After reboot, verify cluster is still enabled
        6. Verify LIDs > 0 and links Active after reboot
        7. Verify DSCP persisted after reboot
        8. Cleanup - disable cluster
    """
    system = System(None)
    cluster = Cluster()
    sdn = Sdn()
    output_format = OutputFormat.json
    TestToolkit.tested_api = test_api
    try:
        logger.info("Setting cluster state to enabled")
        ClusterTools.start_cluster(cluster, setup_name, output_format, engine=engines.dut, devices=devices)

        with allure.step("Configure custom DSCP before reboot"):
            dscp_value, dscp_numeric = _get_random_dscp_not_default()
            logger.info(f"Setting DSCP to {dscp_value} (numeric: {dscp_numeric}) before reboot")
            cluster.set(op_param_name=ClusterConsts.DSCP_MARKING_FIELD,
                        op_param_value=dscp_value, apply=True)

        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

        if not (is_bug_active(4207869) and standalone_system):
            ClusterTools.wait_for_app_healthy(cluster, ClusterConsts.NMX_CONTROLLER, engine=engines.dut)

        if has_loopbox or not standalone_system:
            with allure.step("Verify LIDs bigger than 0 prior to reboot"):
                ClusterTools.verify_lid_value(devices, engine=engines.dut)
            with allure.step("Verify links Active prior to reboot"):
                ClusterTools.verify_interface_up(devices, has_loopbox, setup_name, engine=engines.dut)

        if not standalone_system:
            with allure.step("Creating Empty partition, then adding a GPU to it with no-reroute option"):
                logger.info("After reboot, empty partition should persist, but GPU added to it with no-reroute should be deleted")
                uuid, location, _, partition_to_remove_from = ClusterTools.create_empty_partition_and_add_gpu(sdn, 'no-reroute', engine=engines.dut)

        tray_state_set = False
        if not standalone_system and isinstance(devices.dut, RosalindSwitch):
            with allure.step("Set tray maintenance-state to non-default before reboot"):
                trays_output = OutputParsingTool.parse_show_output_to_dict(sdn.trays.show()).get_returned_value()
                tray_ids = list(trays_output.keys())
                if tray_ids:
                    selected_tray = tray_ids[0]
                    local_switch_slot = str(chassis_info.get('slot-number'))
                    partition_deletion_flag = is_rtf_compute_tray_scenario(selected_tray, local_switch_slot, tray_topology_config)
                    if partition_deletion_flag:
                        delete_sdn_partitions_before_compute_tray_test(sdn, excluded_partitions=[ClusterConsts.EMPTY_PARTITION_ID])
                    tray_non_default = random.choice(['down', 'diag'])
                    sdn.trays.action_update_maintenance_state(
                        tray_id=selected_tray, maintenance_state=tray_non_default).verify_result()
                    tray_state_set = True
                    logger.info(f"Set tray {selected_tray} to '{tray_non_default}' before reboot")

        with allure.step('Run nv action reboot system'):
            result_obj, duration = OperationTime.save_duration('reboot', '', test_name, system.reboot.action_reboot)

        with allure.step("Check Cluster status and cluster apps after reboot"):
            ClusterTools.validate_cluster_enabled(cluster, engine=engines.dut)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled',
                                                             nmx_c_expected_state='up', engine=engines.dut)
            # ClusterTools.verify_apps_running(engines.dut, devices, cluster, 'ok', output_format, standalone_system, has_loopbox)
            retry_call(ClusterTools.verify_apps_running,
                       [engines.dut, devices, cluster, 'ok', output_format, standalone_system, has_loopbox],
                       exceptions=AssertionError, tries=6, delay=5)

        if has_loopbox or not standalone_system:
            with allure.step("Verify LIDs bigger than 0 after reboot"):
                ClusterTools.verify_lid_value(devices, engine=engines.dut)
            with allure.step("Verify links Active after reboot"):
                ClusterTools.verify_interface_up(devices, has_loopbox, setup_name, engine=engines.dut)

        cluster_reboot_extra_time = 60
        # Reboot time when cluster is enabled is longer than when cluster is disabled, so we add extra time to the threshold
        reboot_threshold = devices.dut.expected_operation_durations.get(devices.dut.reboot_type)
        if reboot_threshold is not None:
            reboot_threshold += cluster_reboot_extra_time
        OperationTime.verify_operation_time(duration, devices.dut.reboot_type, devices,
                                            threshold=reboot_threshold).verify_result()

        with allure.step("Verify DSCP persisted after reboot"):
            show_output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=output_format), output_format=output_format
            ).get_returned_value()
            actual_dscp = show_output.get(ClusterConsts.DSCP_MARKING_FIELD)
            actual_numeric = DscpValueSelector.get_numeric_value(actual_dscp)
            assert actual_numeric == dscp_numeric, \
                f"DSCP should persist ({dscp_value}/{dscp_numeric}) after reboot, got {actual_dscp}"

        if tray_state_set:
            with allure.step(f"Verify tray {selected_tray} maintenance-state persisted after reboot"):
                tray_output = OutputParsingTool.parse_show_output_to_dict(
                    sdn.trays.tray[selected_tray].show()).get_returned_value()
                actual_tray_state = tray_output.get(ClusterConsts.MAINTENANCE_STATE)
                assert actual_tray_state == tray_non_default, (
                    f"Tray {selected_tray}: expected '{tray_non_default}' after reboot, got '{actual_tray_state}'")

            with allure.step(f"Restore tray {selected_tray} to default"):
                sdn.trays.action_restore_maintenance_state(tray_id=selected_tray).verify_result()

        if not standalone_system:
            output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                 output_format=output_format).get_returned_value()
            assert ClusterConsts.EMPTY_PARTITION_ID in output.keys(), f'Partition {ClusterConsts.EMPTY_PARTITION_ID} was deleted, while its expected to be kept'
            assert ClusterConsts.DEFAULT_PARTITION_ID not in output.keys(), f'Partition {ClusterConsts.DEFAULT_PARTITION_ID} was not deleted, while its expected to be deleted'
            output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.partition_id[ClusterConsts.EMPTY_PARTITION_ID].show(output_format=output_format),
                                                                 output_format=output_format).get_returned_value()
            uuids, locations = ClusterTools.uuid_location_in_partition(sdn, partition_to_remove_from)
            assert uuid not in uuids, f"uuid {uuid} was not deleted from {partition_to_remove_from} although it was removed with no-reroute, See current uuids: {uuids}"
            assert location not in locations, f"uuid {uuid} was not deleted from {partition_to_remove_from} although it was removed with no-reroute. See current locations: {locations}"

    finally:
        if not standalone_system:
            with allure.step("Running sdn factory reset"):
                ClusterTools.reset_sdn_factory_default_and_wait_for_restart(sdn, cluster, engine=engines.dut)
        cluster.unset(apply=True, dut_engine=engines.dut)
        ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled', nmx_c_expected_state='down', engine=engines.dut)
        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)
