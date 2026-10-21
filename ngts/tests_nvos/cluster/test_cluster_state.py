import logging
import time

import pytest

from ngts.nvos_constants.constants_nvos import SystemConsts, OutputFormat, ApiType, NvosConst
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.StressResourcesTool import StressResourcesTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.simx
@pytest.mark.nvl_ci
@pytest.mark.timeout(30 * MINUTE, func_only=True)
def test_cluster_state(engines, devices, random_api, has_loopbox, standalone_system, setup_name):
    TestToolkit.tested_api = random_api
    output_format = OutputFormat.json

    try:
        with allure.step("Create Cluster object"):
            cluster = Cluster()

        with allure.step("Running 'nv show cluster' command and parsing output"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=output_format),
                output_format=output_format).get_returned_value()
            with allure.step("Validate initial state is disabled"):
                assert output[SystemConsts.STATE] == NvosConst.DISABLED, f"initial state is , " \
                    f"{output[SystemConsts.STATE]}, Expected to be: " \
                    f"{NvosConst.DISABLED}"
                # TBD - once bug fixed:
                # [NVOS - Design] Bug SW #3982533: [Functional] [NVL5 - NMX] | nmxc-conn field shows NONE value instead of empty string | Assignee: Oren Reiss | Status: Assigned
                # assert output['nmxc-conn'] == NvosConst.DISABLED, f"nmxc-conn state is {output['nmxc-conn']} " \
                #                                                   f"instead of disabled"
                assert ClusterConsts.NMXC_CONN in output, f"{ClusterConsts.NMXC_CONN} was not found in {output}"
                assert output[ClusterConsts.NMXC_CONN] == 'down', f"{ClusterConsts.NMXC_CONN} state was expected to be down but instead it was {output[ClusterConsts.NMXC_CONN]}"

        for state in [NvosConst.ENABLED, NvosConst.DISABLED]:
            with allure.step(f"Running 'nv set cluster state {state}' and validating state changed"):
                cluster.set(op_param_name="state", op_param_value=state, apply=True).verify_result()
                if state == NvosConst.DISABLED:
                    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled', nmx_c_expected_state='down')
                else:
                    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state='up')
                output = OutputParsingTool.parse_show_output_to_dict(
                    cluster.show(output_format=output_format),
                    output_format=output_format).get_returned_value()

                with allure.step(f"Validate state is {state}"):
                    assert output[SystemConsts.STATE] == state, f"initial state is , " \
                        f"{output[SystemConsts.STATE]}, Expected to be: " \
                        f"{state}"
                    assert ClusterConsts.NMXC_CONN in output, f"{ClusterConsts.NMXC_CONN} was not found in {output}"
                    expected_nmxc_state = ClusterConsts.NMXC_CONN_STATE_PER_CLUSTER_STATE[output[SystemConsts.STATE]]
                    assert output[ClusterConsts.NMXC_CONN] == expected_nmxc_state, f"{ClusterConsts.NMXC_CONN} state was expected to be {expected_nmxc_state} but instead it was {output[ClusterConsts.NMXC_CONN]}"
                    # TBD - once bug fixed:
                    # [NVOS - Design] Bug SW #3982533: [Functional] [NVL5 - NMX] | nmxc-conn field shows NONE value instead of empty string | Assignee: Oren Reiss | Status: Assigned
                    # assert output['nmxc-conn'] == state, f"nmxc-conn state is {output['nmxc-conn']} " \
                    #                                                   f"instead of {state}"

        with allure.step("Apply a non defined state"):
            output = cluster.set(op_param_name="state", op_param_value=ClusterConsts.UNDEFINED_STATE).get_returned_value(should_succeed=False)
            output = output.split('\n')[-1]
            assert ClusterConsts.UNDEFINED_STATE_DICT[random_api] in output, f"Expected error message {ClusterConsts.UNDEFINED_STATE_DICT[random_api]}, " \
                f"actual message received {output}"

        with allure.step("Running 'nv set cluster state enabled' and validating state changed"):
            cluster.set(op_param_name="state", op_param_value=NvosConst.ENABLED, apply=True)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state='up')
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=output_format),
                output_format=output_format).get_returned_value()

            with allure.step("Validate state is enabled"):
                assert output[SystemConsts.STATE] == NvosConst.ENABLED, f"state is , " \
                    f"{output[SystemConsts.STATE]}, Expected to be: " \
                    f"{state}"
                assert ClusterConsts.NMXC_CONN in output, f"{ClusterConsts.NMXC_CONN} was not found in {output}"
                expected_nmxc_state = ClusterConsts.NMXC_CONN_STATE_PER_CLUSTER_STATE[output[SystemConsts.STATE]]
                assert output[ClusterConsts.NMXC_CONN] == expected_nmxc_state, f"{ClusterConsts.NMXC_CONN} state was expected to be {expected_nmxc_state} but instead it was {output[ClusterConsts.NMXC_CONN]}"

            with allure.step("Running 'nv unset cluster' and validate state is back to disabled"):
                cluster.unset(apply=True).verify_result()
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled', nmx_c_expected_state='down')
                output = OutputParsingTool.parse_show_output_to_dict(
                    cluster.show(output_format=output_format),
                    output_format=output_format).get_returned_value()

                with allure.step("Validate state is disabled after running unset command"):
                    assert output[SystemConsts.STATE] == NvosConst.DISABLED, f"State is , " \
                        f"{output[SystemConsts.STATE]}, Expected to be: " \
                        f"{state}"
                    assert ClusterConsts.NMXC_CONN in output, f"{ClusterConsts.NMXC_CONN} was not found in {output}"
                    expected_nmxc_state = ClusterConsts.NMXC_CONN_STATE_PER_CLUSTER_STATE[output[SystemConsts.STATE]]
                    assert output[ClusterConsts.NMXC_CONN] == expected_nmxc_state, f"{ClusterConsts.NMXC_CONN} state was expected to be {expected_nmxc_state} but instead it was {output[ClusterConsts.NMXC_CONN]}"

    finally:
        pass


@disabled_access_ports
@pytest.mark.timeout(50 * MINUTE, func_only=True)
@pytest.mark.nmx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
@pytest.mark.simx
def test_stress_cluster_state(engines, devices, test_api, test_name, has_loopbox, standalone_system, setup_name):
    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json

    with allure.step("Create Cluster object"):
        cluster = Cluster()

    try:
        with allure.step("Stress testing cluster state"):
            for i in range(10):
                logger.info(f"Starting iteration {i}")
                result_obj, duration = OperationTime.save_duration('start stop cluster', '', test_name, ClusterTools.start_stop_cluster, cluster, setup_name, output_format)
                OperationTime.verify_operation_time(duration, 'start stop cluster', devices).verify_result()

    finally:
        pass


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.simx
def test_cluster_state_with_stressed_resources(engines, devices, random_api, test_name, has_loopbox, standalone_system, setup_name):
    TestToolkit.tested_api = random_api
    output_format = OutputFormat.json

    with allure.step("Create Cluster object"):
        cluster = Cluster()
        installed_packages = []
    try:
        with allure.step("Test cluster with stressed CPU and Memory utilization"):
            # This will run in background &
            installed_packages = StressResourcesTool.stress_cpu_and_memory(engines, devices.dut.core_count)
            timeout = 300  # for example, 300 seconds

            # Get the current time
            start_time = time.time()

            # Loop until the timeout is reached
            while time.time() - start_time < timeout:
                result_obj, duration = OperationTime.save_duration('start stop cluster stressed resources', '', test_name, ClusterTools.start_stop_cluster, cluster, setup_name, output_format)
                OperationTime.verify_operation_time(duration, 'start stop cluster stressed resources', devices).verify_result()
    finally:
        if installed_packages:
            StressResourcesTool.delete_packages(engines, installed_packages)
