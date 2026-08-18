import logging
import random
import pytest
import time
import re

from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_constants.constants_nvos import ApiType, OutputFormat
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.nvos_tools.infra.RegressionConfigurations import Configurations
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.constants import MINUTE

logger = logging.getLogger()


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(30 * MINUTE, func_only=True)
def test_cluster_partition(engines, dut_engines, devices, random_api, has_loopbox, setup_name, standalone_system, is_simx):
    if standalone_system:
        pytest.skip("Skipping test - supported only for non standalone systems.")

    output_format = OutputFormat.json
    interface_wa_called = False
    with allure.step("Create Cluster object"):
        cluster = Cluster()
        sdn = Sdn()
        system = System()
        used_partition_ids = []
        partition_ids = []
        expected_number_of_gpus = Configurations.oberon_num_of_gpus[setup_name]
        default_partition_id = -1
        default_partition_type = None
        gpus_removed_from_default = []
        initial_partition_output = None
        partitions_mapping = {}  # key: partition_id, value: list of tuples, each index is (uuid, location)
        zero_uuid_gpus = set()
    try:
        with allure.step("Enable cluster"):
            ClusterTools().start_cluster(cluster, setup_name, output_format, engine=engines.dut, devices=devices)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, app=ClusterConsts.NMX_CONTROLLER,
                                                             standalone_system=standalone_system, engine=engines.dut)

        with allure.step("Show All Partitions - at the beginning its just the default partition"):
            time.sleep(2)
            if is_bug_active(4998867):
                for _ in range(3):
                    initial_partition_output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                                           output_format=output_format).get_returned_value()
                    if initial_partition_output:
                        break
                    logger.info("Trying after 2 seconds...")
                    time.sleep(3)

            partition_ids = list(initial_partition_output.keys())
            default_partition_id = partition_ids[0]
            default_partition_type = initial_partition_output[default_partition_id]['partition-type']

            showed_number_of_gpus = initial_partition_output[default_partition_id]['num-gpus']
            if not is_bug_active(4210584):
                assert showed_number_of_gpus == expected_number_of_gpus, f'Expected number of gpus {expected_number_of_gpus}, showed number of gpus: {showed_number_of_gpus}'
            # Add assert to check the values - num of gpus, health, resiliency etc...
        with allure.step("Show partition per partition id"):
            for partition_id in partition_ids:
                output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.partition_id[partition_id].show(output_format=output_format),
                                                                     output_format=output_format).get_returned_value()
                list_of_tuples = ClusterTools.get_partition_uuid_location_map(output)
                partitions_mapping[partition_id] = list_of_tuples

        ClusterTools.collect_zero_uuid_gpus(partitions_mapping, zero_uuid_gpus, sdn=sdn, engine=engines.dut, setup_name=setup_name)

        ClusterTools.create_empty_partition(sdn, partitions_mapping, engine=engines.dut)

        ClusterTools.delete_empty_partition(sdn, partitions_mapping, engine=engines.dut)

        ClusterTools.create_empty_partition(sdn, partitions_mapping, engine=engines.dut)

        with allure.step("Remove first GPU and add to new partition"):
            partition_name1 = ClusterConsts.CREATED_PARTITION_NAME + '1'
            new_partition_id1, partition_type_1 = remove_gpu_from_partition_and_add_to_new_partition(sdn, default_partition_id, partitions_mapping, used_partition_ids, default_partition_type, partition_name1, zero_uuid_gpus=zero_uuid_gpus)

        with allure.step("Remove second GPU and add to new partition"):
            partition_name2 = ClusterConsts.CREATED_PARTITION_NAME + '2'
            new_partition_id2, partition_type_2 = remove_gpu_from_partition_and_add_to_new_partition(sdn, default_partition_id, partitions_mapping, used_partition_ids, default_partition_type, partition_name2, zero_uuid_gpus=zero_uuid_gpus)

        # At this point we have 4 partitions. default, empty, 1, 1

        remove_gpu_from_partition_and_add_to_existing_partition(sdn, new_partition_id2, new_partition_id1, partitions_mapping, used_partition_ids, partition_type_2, partition_name1, partition_type_1, zero_uuid_gpus=zero_uuid_gpus)

        # TODO - Once we have a way to test the reroute option, cover the gaps. (need to run nv action update sdn partition <partition_id> reroute)
        # And also, need to run with no-reroute randomization.
        with allure.step("Running sdn factory reset"):
            ClusterTools.reset_sdn_factory_default_and_wait_for_restart(sdn, cluster, engine=engines.dut)

        ClusterTools().stop_cluster(cluster, engine=engines.dut)
        ClusterTools().start_cluster(cluster, setup_name, engine=engines.dut, devices=devices)
        TestToolkit.tested_api = ApiType.NVUE
        ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, app=ClusterConsts.NMX_CONTROLLER,
                                                         standalone_system=standalone_system, engine=engines.dut)
        interfaces_wa = ClusterTools().wa_to_get_active_interface_for_loopbox_systems(cluster, sdn, devices, engines.dut, dut_engines, has_loopbox, setup_name, standalone_system, is_simx)
        next(interfaces_wa)
        interface_wa_called = True
        with allure.step("Checking if partition is restored to original"):
            output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                 output_format=output_format).get_returned_value()
            assert initial_partition_output == output, f"Initial partition was {initial_partition_output}, but current partition is {output}"

        with allure.step("Verify no GPUs have uuid=0"):
            ClusterTools.assert_no_zero_uuid_gpus(zero_uuid_gpus)

    finally:
        TestToolkit.tested_api = random_api
        with allure.step("Running sdn factory reset"):
            ClusterTools.reset_sdn_factory_default_and_wait_for_restart(sdn, cluster, engine=engines.dut)

        if interface_wa_called:
            try:
                next(interfaces_wa)
            except StopIteration:
                pass  # Or handle it if necessary
        cluster.unset(apply=True)
        ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled', nmx_c_expected_state='down', engine=engines.dut)


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(30 * MINUTE, func_only=True)
def test_cluster_partition_bad_flow(engines, dut_engines, devices, random_api, has_loopbox, standalone_system, setup_name, is_simx):

    if standalone_system:
        pytest.skip("Skipping test - supported only for non standalone systems.")

    output_format = OutputFormat.json
    interface_wa_called = False
    with allure.step("Create Cluster object"):
        cluster = Cluster()
        sdn = Sdn()
        system = System()
        used_partition_ids = []
        partition_ids = []
        expected_number_of_gpus = Configurations.oberon_num_of_gpus[setup_name]
        default_partition_id = -1
        default_partition_type = None
        gpus_removed_from_default = []
        initial_partition_output = None
        partitions_mapping = {}  # key: partition_id, value: list of tuples, each index is (uuid, location)
        zero_uuid_gpus = set()
    try:
        with allure.step("Enable cluster"):
            ClusterTools().start_cluster(cluster, setup_name, output_format, engine=engines.dut, devices=devices)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, app=ClusterConsts.NMX_CONTROLLER,
                                                             standalone_system=standalone_system, engine=engines.dut)

        with allure.step("Show All Partitions - at the beginning its just the default partition"):
            time.sleep(2)
            if is_bug_active(4998867):
                for _ in range(3):
                    initial_partition_output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                                           output_format=output_format).get_returned_value()
                    if initial_partition_output:
                        break
                    logger.info("Trying after 2 seconds...")
                    time.sleep(3)

            partition_ids = list(initial_partition_output.keys())
            default_partition_id = partition_ids[0]
            default_partition_type = initial_partition_output[default_partition_id]['partition-type']

        with allure.step("Show partition per partition id"):
            for partition_id in partition_ids:
                output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.partition_id[partition_id].show(output_format=output_format),
                                                                     output_format=output_format).get_returned_value()
                list_of_tuples = ClusterTools.get_partition_uuid_location_map(output)
                partitions_mapping[partition_id] = list_of_tuples

        ClusterTools.collect_zero_uuid_gpus(partitions_mapping, zero_uuid_gpus, sdn=sdn, engine=engines.dut, setup_name=setup_name)

        gpus_in_partition = partitions_mapping[default_partition_id]
        (uuid, location) = ClusterTools.choose_valid_gpu(gpus_in_partition, zero_uuid_gpus)
        with allure.step("Add GPU to a second partition"):
            resiliency_mode = random.choice(ClusterConsts.RESILIENCY_MODES)
            mcast_limit = random.randrange(ClusterConsts.MIN_MCAST, ClusterConsts.MAX_MCAST + 1, 4)
            no_reroute = random.choice(['', 'no-reroute'])
            part_id = choose_new_partition_id(used_partition_ids)
            partition_type = random.choice(ClusterConsts.PARTITION_TYPES)
            if partition_type == 'location_based':
                output = sdn.partition.partition_id[part_id].action_create_partition_id(name=ClusterConsts.CREATED_PARTITION_NAME + '1', resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, location=location).verify_result(should_succeed=False)
            else:
                output = sdn.partition.partition_id[part_id].action_create_partition_id(name=ClusterConsts.CREATED_PARTITION_NAME + '1', resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, uuid=int(uuid)).verify_result(should_succeed=False)
            logger.info("Wait for 2 seconds until partitions are updated")
            time.sleep(2)
            err_msg = f"failed to create partition {part_id}"
            assert err_msg in output, f"Expected message to include {err_msg}, instead\n {output}"

        with allure.step("Remove GPU from default partition - Twice"):
            no_reroute = random.choice(['', 'no-reroute'])
            if default_partition_type == 'location_based':
                sdn.partition.partition_id[default_partition_id].location.location_id[location].action_restore_partition(reroute_param=no_reroute).verify_result()
                logger.info("Wait for 2 seconds until partitions are updated")
                time.sleep(2)
                output = sdn.partition.partition_id[default_partition_id].location.location_id[location].action_restore_partition(reroute_param=no_reroute).verify_result(should_succeed=False)
                err_msg = f"failed to restore partition {default_partition_id} location {location}"
            else:
                sdn.partition.partition_id[default_partition_id].uuid.uuid_value[uuid].action_restore_partition(reroute_param=no_reroute).verify_result()
                logger.info("Wait for 2 seconds until partitions are updated")
                time.sleep(2)
                output = sdn.partition.partition_id[default_partition_id].uuid.uuid_value[uuid].action_restore_partition(reroute_param=no_reroute).verify_result(should_succeed=False)
                err_msg = f"failed to restore partition {default_partition_id} uuid {uuid}"
            partitions_mapping[default_partition_id].remove((uuid, location))
            assert err_msg in output, f"Expected message to include {err_msg}, instead\n {output}"

        logger.info("Sleeping for 10 seconds")
        time.sleep(10)

        with allure.step("ADD GPU To partition - Twice"):
            no_reroute = random.choice(['', 'no-reroute'])
            if default_partition_type == 'location_based':
                sdn.partition.partition_id[default_partition_id].location.location_id[location].action_update_partition(reroute_param=no_reroute).verify_result()
                logger.info("Wait for 2 seconds until partitions are updated")
                time.sleep(2)
                output = sdn.partition.partition_id[default_partition_id].location.location_id[location].action_update_partition(reroute_param=no_reroute).verify_result(should_succeed=False)
                err_msg = f"failed to update partition {default_partition_id} location {location}"
            else:
                sdn.partition.partition_id[default_partition_id].uuid.uuid_value[uuid].action_update_partition(reroute_param=no_reroute).verify_result()
                logger.info("Wait for 2 seconds until partitions are updated")
                time.sleep(2)
                output = sdn.partition.partition_id[default_partition_id].uuid.uuid_value[uuid].action_update_partition(reroute_param=no_reroute).verify_result(should_succeed=False)
                err_msg = f"failed to update partition {default_partition_id} uuid {uuid}"
            partitions_mapping[default_partition_id].append((uuid, location))
            assert err_msg in output, f"Expected message to include {err_msg}, instead\n {output}"

        with allure.step("Run partition commands with invalid parameters and make sure apps are still running"):
            gpus_in_partition = partitions_mapping[default_partition_id]
            (uuid, location) = ClusterTools.choose_valid_gpu(gpus_in_partition, zero_uuid_gpus)
            with allure.step("Add GPU with wrong resiliency_mode"):
                resiliency_mode = random.choice(ClusterConsts.RESILIENCY_MODES) + '1'  # Invalid.
                mcast_limit = random.randrange(ClusterConsts.MIN_MCAST, ClusterConsts.MAX_MCAST + 1, 4)
                no_reroute = random.choice(['', 'no-reroute'])
                part_id = choose_new_partition_id(used_partition_ids)
                partition_type = random.choice(ClusterConsts.PARTITION_TYPES)
                if partition_type == 'location_based':
                    output = sdn.partition.partition_id[part_id].action_create_partition_id(name=ClusterConsts.CREATED_PARTITION_NAME + '1', resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, location=location).verify_result(should_succeed=False)
                else:
                    output = sdn.partition.partition_id[part_id].action_create_partition_id(name=ClusterConsts.CREATED_PARTITION_NAME + '1', resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, uuid=int(uuid)).verify_result(should_succeed=False)

                logger.info("Wait for 2 seconds until partitions are updated")
                time.sleep(2)

                if is_bug_active(4497969) and random_api == 'NVUE':
                    err_msg = "Error: Invalid Command"
                    assert err_msg in output, f"Expected message to include {err_msg}, instead\n {output}"
                else:
                    expected_err_regex = rf"'{resiliency_mode}' is not one of \[(.*?)\]"
                    msg = f"Expected message to include {expected_err_regex}, instead\n {output}"
                    match = re.search(expected_err_regex, output)
                    assert match, msg
                    extracted_resiliency_modes = set([item.strip().strip("'") for item in match.group(1).split(',')])
                    assert extracted_resiliency_modes == set(ClusterConsts.RESILIENCY_MODES), msg

                TestToolkit.tested_api = ApiType.NVUE
                ClusterTools.verify_apps_running(engines.dut, devices, cluster, 'ok', output_format, standalone_system, has_loopbox, is_simx)

            with allure.step("Add GPU with wrong mcast_limit"):
                resiliency_mode = random.choice(ClusterConsts.RESILIENCY_MODES)
                mcast_limit = random.randrange(ClusterConsts.MIN_MCAST, ClusterConsts.MAX_MCAST + 1, 4) + 1025  # Invalid
                no_reroute = random.choice(['', 'no-reroute'])
                part_id = choose_new_partition_id(used_partition_ids)
                partition_type = random.choice(ClusterConsts.PARTITION_TYPES)
                if partition_type == 'location_based':
                    output = sdn.partition.partition_id[part_id].action_create_partition_id(name=ClusterConsts.CREATED_PARTITION_NAME + '11', resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, location=location).verify_result(should_succeed=False)
                else:
                    output = sdn.partition.partition_id[part_id].action_create_partition_id(name=ClusterConsts.CREATED_PARTITION_NAME + '11', resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, uuid=int(uuid)).verify_result(should_succeed=False)

                logger.info("Wait for 2 seconds until partitions are updated")
                time.sleep(2)

                err_msg = "Valid range for mcast-limit is 0 - 1024"
                assert err_msg in output, f"Expected message to include {err_msg}, instead\n {output}"
                TestToolkit.tested_api = ApiType.NVUE
                ClusterTools.verify_apps_running(engines.dut, devices, cluster, 'ok', output_format, standalone_system, has_loopbox, is_simx)

        with allure.step("Running sdn factory reset"):
            ClusterTools.reset_sdn_factory_default_and_wait_for_restart(sdn, cluster, engine=engines.dut)

        ClusterTools().stop_cluster(cluster, engine=engines.dut)
        ClusterTools().start_cluster(cluster, setup_name, engine=engines.dut, devices=devices)
        TestToolkit.tested_api = ApiType.NVUE
        ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, app=ClusterConsts.NMX_CONTROLLER,
                                                         standalone_system=standalone_system, engine=engines.dut)
        interfaces_wa = ClusterTools().wa_to_get_active_interface_for_loopbox_systems(cluster, sdn, devices, engines.dut, dut_engines, has_loopbox, setup_name, standalone_system, is_simx)
        next(interfaces_wa)
        interface_wa_called = True
        with allure.step("Checking if partition is restored to original"):
            output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                                 output_format=output_format).get_returned_value()
            assert initial_partition_output == output, f"Initial partition was {initial_partition_output}, but current partition is {output}"

        with allure.step("Verify no GPUs have uuid=0"):
            ClusterTools.assert_no_zero_uuid_gpus(zero_uuid_gpus)

    finally:
        TestToolkit.tested_api = random_api
        with allure.step("Running sdn factory reset"):
            ClusterTools.reset_sdn_factory_default_and_wait_for_restart(sdn, cluster, engine=engines.dut)

        if interface_wa_called:
            try:
                next(interfaces_wa)
            except StopIteration:
                pass  # Or handle it if necessary
        cluster.unset(apply=True)
        ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled', nmx_c_expected_state='down', engine=engines.dut)


def choose_new_partition_id(used_partition_ids):
    available_partitions = set(range(1, 32766 + 1)) - set(used_partition_ids)
    return random.choice(list(available_partitions))


def remove_gpu_from_partition_and_add_to_existing_partition(sdn, original_partition_id, target_partition_id, partitions_mapping, used_partition_ids, original_partition_type, target_partition_name, target_partition_type, output_format=OutputFormat.json, zero_uuid_gpus=None):
    # Remove GPU from default partition, and add it to a newly created one - randomize adding it by uuid or location.
    gpus_in_partition = partitions_mapping[original_partition_id]
    (uuid, location) = ClusterTools.choose_valid_gpu(gpus_in_partition, zero_uuid_gpus)

    remove_gpu_from_partition(sdn, original_partition_id, location, uuid, partitions_mapping, original_partition_type)
    no_reroute = random.choice(['', 'no-reroute'])
    with allure.step("Add Removed GPU to an existing partition"):
        if target_partition_type == 'location_based':
            sdn.partition.partition_id[target_partition_id].location.location_id[location].action_update_partition(reroute_param=no_reroute).verify_result()
        else:
            sdn.partition.partition_id[target_partition_id].uuid.uuid_value[uuid].action_update_partition(reroute_param=no_reroute).verify_result()
        partitions_mapping[target_partition_id].append((uuid, location))

        logger.info("Wait for 2 seconds until partitions are updated")
        time.sleep(2)

    with allure.step("Checking newly updated partition"):
        output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.partition_id[target_partition_id].show(output_format=output_format),
                                                             output_format=output_format).get_returned_value()

        gpus_in_partition = partitions_mapping[target_partition_id]
        uuids_dict, locations_dict = build_uuid_location_dicts(partitions_mapping, target_partition_id)
        number_of_gpus = len(partitions_mapping[target_partition_id])
        # TODO - location/uuid as sets. When do not have guarantee on order.
        if not is_bug_active(4209873):
            expected_output = {'health': 'healthy', 'locations': {}, 'mcast-limit': '', 'name': target_partition_name, 'num-gpus': number_of_gpus, 'partition-type': target_partition_type, 'resiliency-mode': ''}
            ClusterTools.validate_partition_content(output, expected_output)
        assert location in output['locations'].keys(), f"location {location} was not added to the partition {target_partition_id}"
        assert uuid == output['locations'][location]['uuid'], f"location{location} was expected to contain uuid {uuid}, but instead it has: {output['locations'][location]['uuid']}"


def build_uuid_location_dicts(partitions_mapping, original_partition_id):
    gpus_in_partition = partitions_mapping[original_partition_id]
    locations = {location: {} for _, location in gpus_in_partition}
    uuids = {uuid: {} for uuid, _ in gpus_in_partition}
    return uuids, locations


def remove_gpu_from_partition(sdn, original_partition_id, location, uuid, partitions_mapping, original_partition_type, output_format=OutputFormat.json):
    # At this point we only have default partition.
    no_reroute = random.choice(['', 'no-reroute'])
    with allure.step(f"Remove GPU from partition {original_partition_id}"):
        if original_partition_type == 'location_based':
            sdn.partition.partition_id[original_partition_id].location.location_id[location].action_restore_partition(reroute_param=no_reroute).verify_result()
        else:
            sdn.partition.partition_id[original_partition_id].uuid.uuid_value[uuid].action_restore_partition(reroute_param=no_reroute).verify_result()

        logger.info("Wait for 2 seconds until partitions are updated")
        time.sleep(2)

    if is_bug_active(4285786):
        time.sleep(15)

    partitions_mapping[original_partition_id].remove((uuid, location))
    output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.partition_id[original_partition_id].show(output_format=output_format),
                                                         output_format=output_format).get_returned_value()

    original_partition_mapping_list = ClusterTools.get_partition_uuid_location_map(output)  # Contains tuples of uuid/location
    assert (uuid, location) not in original_partition_mapping_list, f"{uuid} {location} should not be part of the partition {original_partition_id}, after it was removed. but its part of it: {original_partition_mapping_list}"


def remove_gpu_from_partition_and_add_to_new_partition(sdn, original_partition_id, partitions_mapping, used_partition_ids, original_partition_type, partition_name, output_format=OutputFormat.json, zero_uuid_gpus=None):
    # Remove GPU from default partition, and add it to a newly created one - randomize adding it by uuid or location.
    partition_type = random.choice(ClusterConsts.PARTITION_TYPES)
    gpus_in_partition = partitions_mapping[original_partition_id]
    (uuid, location) = ClusterTools.choose_valid_gpu(gpus_in_partition, zero_uuid_gpus)

    remove_gpu_from_partition(sdn, original_partition_id, location, uuid, partitions_mapping, original_partition_type)

    with allure.step("Add Removed GPU to a new partition"):
        resiliency_mode = random.choice(ClusterConsts.RESILIENCY_MODES)
        mcast_limit = random.randrange(ClusterConsts.MIN_MCAST, ClusterConsts.MAX_MCAST + 1, 4)
        new_partition = choose_new_partition_id(used_partition_ids)
        used_partition_ids.append(new_partition)
        if partition_type == 'location_based':
            sdn.partition.partition_id[new_partition].action_create_partition_id(name=partition_name, resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, location=location).verify_result()
        else:
            sdn.partition.partition_id[new_partition].action_create_partition_id(name=partition_name, resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, uuid=int(uuid)).verify_result()

        logger.info("Wait for 2 seconds until partitions are updated")
        time.sleep(2)

    with allure.step("Checking newly created partition"):
        output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format),
                                                             output_format=output_format).get_returned_value()
        new_partition = str(new_partition)
        assert new_partition in list(output.keys()), f'Partition {new_partition} was not created'
        output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.partition_id[new_partition].show(output_format=output_format),
                                                             output_format=output_format).get_returned_value()
        partitions_mapping[new_partition] = [(uuid, location)]
        uuids_dict, locations_dict = build_uuid_location_dicts(partitions_mapping, new_partition)
        locations = {location: {'uuid': uuid}}
        if not is_bug_active(4190587):
            expected_output = {'health': 'healthy', 'locations': locations, 'mcast-limit': mcast_limit, 'name': partition_name, 'num-gpus': 1, 'partition-type': partition_type, 'resiliency-mode': resiliency_mode}
            ClusterTools.validate_partition_content(output, expected_output)

    return new_partition, partition_type
