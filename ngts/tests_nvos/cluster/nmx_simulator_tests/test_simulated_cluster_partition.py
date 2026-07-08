import logging
import random
import pytest
import time

from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_constants.constants_nvos import ApiType, OutputFormat
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.cluster.test_cluster_partition import (
    remove_gpu_from_partition_and_add_to_new_partition,
    remove_gpu_from_partition_and_add_to_existing_partition,
    choose_new_partition_id
)
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.cluster.nmx_simulator_tests.constants import (
    PARTITION_UPDATE_WAIT_TIME,
    PARTITION_OPERATION_WAIT_TIME,
    PARTITION_UPDATE_WAIT_MESSAGE,
    PARTITION_OPERATION_WAIT_MESSAGE,
    INVALID_MCAST_LIMIT,
    MAX_MCAST_LIMIT,
    INVALID_MCAST_NVUE_ERROR,
    INVALID_MCAST_OPENAPI_ERROR
)

logger = logging.getLogger()


def wait_for_partition_update():
    """
    Helper function to wait for partition updates to complete.
    Uses the configured wait time from constants.
    """
    logger.info(PARTITION_UPDATE_WAIT_MESSAGE.format(PARTITION_UPDATE_WAIT_TIME))
    time.sleep(PARTITION_UPDATE_WAIT_TIME)


@pytest.mark.nvl_ci
@pytest.mark.nmx
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_simulated_cluster_partition(engines, devices, test_api):

    output_format = OutputFormat.json

    with allure.step("Create Cluster object"):
        cluster = Cluster()
        sdn = Sdn()
        system = System()
        used_partition_ids = []
        partition_ids = []
        expected_number_of_gpus = devices.dut.nmx_simulation_gpu_count
        default_partition_id = -1
        default_partition_type = None
        gpus_removed_from_default = []
        initial_partition_output = None
        partitions_mapping = {}  # key: partition_id, value: list of tuples, each index is (uuid, location)

    with allure.step("Show All Partitions - at the beginning its just the default partition"):
        if is_bug_active(4998867):
            for _ in range(3):
                initial_partition_output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                                       output_format=output_format).get_returned_value()
                if initial_partition_output:
                    break
                logger.info("Failed to get initial partition output, sleeping for 5 seconds...")
                time.sleep(5)
        else:
            initial_partition_output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                                   output_format=output_format).get_returned_value()
        assert initial_partition_output, "Failed to get initial partition output"

        partition_ids = list(initial_partition_output.keys())
        default_partition_id = partition_ids[0]
        default_partition_type = initial_partition_output[default_partition_id]['partition-type']

        showed_number_of_gpus = int(initial_partition_output[default_partition_id]['num-gpus'])
        assert showed_number_of_gpus == expected_number_of_gpus, f'Expected number of gpus {expected_number_of_gpus}, showed number of gpus: {showed_number_of_gpus}'
        # Add assert to check the values - num of gpus, health, resiliency etc...
    with allure.step("Show partition per partition id"):
        for partition_id in partition_ids:
            output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.partition_id[partition_id].show(output_format=output_format),
                                                                 output_format=output_format).get_returned_value()
            list_of_tuples = ClusterTools.get_partition_uuid_location_map(output)
            partitions_mapping[partition_id] = list_of_tuples

    with allure.step("Create empty partition"):
        ClusterTools.create_empty_partition(sdn, partitions_mapping, engine=engines.dut)

    with allure.step("Delete empty partition"):
        ClusterTools.delete_empty_partition(sdn, partitions_mapping, engine=engines.dut)

    with allure.step("Create empty partition"):
        ClusterTools.create_empty_partition(sdn, partitions_mapping, engine=engines.dut)

    with allure.step("Remove first GPU and add to new partition"):
        partition_name1 = ClusterConsts.CREATED_PARTITION_NAME + '1'
        new_partition_id1, partition_type_1 = remove_gpu_from_partition_and_add_to_new_partition(sdn, default_partition_id, partitions_mapping, used_partition_ids, default_partition_type, partition_name1)

    with allure.step("Remove second GPU and add to new partition"):
        partition_name2 = ClusterConsts.CREATED_PARTITION_NAME + '2'
        new_partition_id2, partition_type_2 = remove_gpu_from_partition_and_add_to_new_partition(sdn, default_partition_id, partitions_mapping, used_partition_ids, default_partition_type, partition_name2)

    # At this point we have 4 partitions. default, empty, 1, 1

    remove_gpu_from_partition_and_add_to_existing_partition(sdn, new_partition_id2, new_partition_id1, partitions_mapping, used_partition_ids, partition_type_2, partition_name1, partition_type_1)


@pytest.mark.nmx
@pytest.mark.disable_loganalyzer
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_simulated_cluster_partition_bad_flow(engines, devices, test_api, has_loopbox, standalone_system, setup_name):

    output_format = OutputFormat.json
    with allure.step("Create Cluster object"):
        cluster = Cluster()
        sdn = Sdn()
        system = System()
        used_partition_ids = []
        partition_ids = []
        expected_number_of_gpus = devices.dut.nmx_simulation_gpu_count
        default_partition_id = -1
        default_partition_type = None
        gpus_removed_from_default = []
        initial_partition_output = None
        partitions_mapping = {}  # key: partition_id, value: list of tuples, each index is (uuid, location)

    with allure.step("Show All Partitions - at the beginning its just the default partition"):
        initial_partition_output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                               output_format=output_format).get_returned_value()
        partition_ids = list(initial_partition_output.keys())
        default_partition_id = partition_ids[0]
        default_partition_type = initial_partition_output[default_partition_id]['partition-type']

    with allure.step("Show partition per partition id"):
        for partition_id in partition_ids:
            output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.partition_id[partition_id].show(output_format=output_format),
                                                                 output_format=output_format).get_returned_value()
            list_of_tuples = ClusterTools.get_partition_uuid_location_map(output)
            partitions_mapping[partition_id] = list_of_tuples

    gpus_in_partition = partitions_mapping[default_partition_id]
    (uuid, location) = random.choice(gpus_in_partition)
    with allure.step("Add GPU to a second partition"):
        resiliency_mode = random.choice(ClusterConsts.RESILIENCY_MODES)
        mcast_limit = random.randrange(ClusterConsts.MIN_MCAST, MAX_MCAST_LIMIT + 1, 4)
        no_reroute = random.choice(['', 'no-reroute'])
        part_id = choose_new_partition_id(used_partition_ids)
        partition_type = random.choice(ClusterConsts.PARTITION_TYPES)
        if partition_type == 'location_based':
            output = sdn.partition.partition_id[part_id].action_create_partition_id(name=ClusterConsts.CREATED_PARTITION_NAME + '1', resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, location=location).verify_result(should_succeed=False)
        else:
            output = sdn.partition.partition_id[part_id].action_create_partition_id(name=ClusterConsts.CREATED_PARTITION_NAME + '1', resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, uuid=int(uuid)).verify_result(should_succeed=False)
        wait_for_partition_update()
        err_msg = f"failed to create partition {part_id}"
        assert err_msg in output, f"Expected message to include {err_msg}, instead\n {output}"

    with allure.step("Remove GPU from default partition - Twice"):
        no_reroute = random.choice(['', 'no-reroute'])
        if default_partition_type == 'location_based':
            sdn.partition.partition_id[default_partition_id].location.location_id[location].action_restore_partition(reroute_param=no_reroute).verify_result()
            wait_for_partition_update()
            output = sdn.partition.partition_id[default_partition_id].location.location_id[location].action_restore_partition(reroute_param=no_reroute).verify_result(should_succeed=False)
            err_msg = f"failed to restore partition {default_partition_id} location {location}"
        else:
            sdn.partition.partition_id[default_partition_id].uuid.uuid_value[uuid].action_restore_partition(reroute_param=no_reroute).verify_result()
            wait_for_partition_update()
            output = sdn.partition.partition_id[default_partition_id].uuid.uuid_value[uuid].action_restore_partition(reroute_param=no_reroute).verify_result(should_succeed=False)
            err_msg = f"failed to restore partition {default_partition_id} uuid {uuid}"
        partitions_mapping[default_partition_id].remove((uuid, location))
        assert err_msg in output, f"Expected message to include {err_msg}, instead\n {output}"

    logger.info(PARTITION_OPERATION_WAIT_MESSAGE.format(PARTITION_OPERATION_WAIT_TIME))
    time.sleep(PARTITION_OPERATION_WAIT_TIME)

    with allure.step("ADD GPU To partition - Twice"):
        no_reroute = random.choice(['', 'no-reroute'])
        if default_partition_type == 'location_based':
            sdn.partition.partition_id[default_partition_id].location.location_id[location].action_update_partition(reroute_param=no_reroute).verify_result()
            wait_for_partition_update()
            output = sdn.partition.partition_id[default_partition_id].location.location_id[location].action_update_partition(reroute_param=no_reroute).verify_result(should_succeed=False)
            err_msg = f"failed to update partition {default_partition_id} location {location}"
        else:
            sdn.partition.partition_id[default_partition_id].uuid.uuid_value[uuid].action_update_partition(reroute_param=no_reroute).verify_result()
            wait_for_partition_update()
            output = sdn.partition.partition_id[default_partition_id].uuid.uuid_value[uuid].action_update_partition(reroute_param=no_reroute).verify_result(should_succeed=False)
            err_msg = f"failed to update partition {default_partition_id} uuid {uuid}"
        partitions_mapping[default_partition_id].append((uuid, location))
        assert err_msg in output, f"Expected message to include {err_msg}, instead\n {output}"

    with allure.step("Run partition commands with invalid parameters and make sure apps are still running"):
        gpus_in_partition = partitions_mapping[default_partition_id]
        (uuid, location) = random.choice(gpus_in_partition)
        with allure.step("Add GPU with wrong resiliency_mode"):
            resiliency_mode = random.choice(ClusterConsts.RESILIENCY_MODES) + '1'  # Invalid.
            mcast_limit = random.randrange(ClusterConsts.MIN_MCAST, MAX_MCAST_LIMIT + 1, 4)
            no_reroute = random.choice(['', 'no-reroute'])
            part_id = choose_new_partition_id(used_partition_ids)
            partition_type = random.choice(ClusterConsts.PARTITION_TYPES)
            if partition_type == 'location_based':
                output = sdn.partition.partition_id[part_id].action_create_partition_id(name=ClusterConsts.CREATED_PARTITION_NAME + '1', resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, location=location).verify_result(should_succeed=False)
            else:
                output = sdn.partition.partition_id[part_id].action_create_partition_id(name=ClusterConsts.CREATED_PARTITION_NAME + '1', resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, uuid=int(uuid)).verify_result(should_succeed=False)

            wait_for_partition_update()
            TestToolkit.tested_api = ApiType.NVUE
            ClusterTools.verify_apps_running(engines.dut, devices, cluster, 'ok', output_format, standalone_system, has_loopbox)
            TestToolkit.tested_api = test_api

        with allure.step("Add GPU with wrong mcast_limit"):
            resiliency_mode = random.choice(ClusterConsts.RESILIENCY_MODES)
            mcast_limit = random.randrange(ClusterConsts.MIN_MCAST, MAX_MCAST_LIMIT + 1, 4) + INVALID_MCAST_LIMIT  # Invalid
            no_reroute = random.choice(['', 'no-reroute'])
            part_id = choose_new_partition_id(used_partition_ids)
            partition_type = random.choice(ClusterConsts.PARTITION_TYPES)
            if partition_type == 'location_based':
                output = sdn.partition.partition_id[part_id].action_create_partition_id(name=ClusterConsts.CREATED_PARTITION_NAME + '11', resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, location=location).verify_result(should_succeed=False)
            else:
                output = sdn.partition.partition_id[part_id].action_create_partition_id(name=ClusterConsts.CREATED_PARTITION_NAME + '11', resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, uuid=int(uuid)).verify_result(should_succeed=False)

            wait_for_partition_update()

            if is_bug_active(4563791):
                err_msg = INVALID_MCAST_NVUE_ERROR if test_api == ApiType.NVUE else INVALID_MCAST_OPENAPI_ERROR.format(mcast_limit)
            else:
                err_msg = INVALID_MCAST_NVUE_ERROR
            assert err_msg in output, f"Expected message to include {err_msg}, instead\n {output}"
            TestToolkit.tested_api = ApiType.NVUE
            ClusterTools.verify_apps_running(engines.dut, devices, cluster, 'ok', output_format, standalone_system, has_loopbox)
