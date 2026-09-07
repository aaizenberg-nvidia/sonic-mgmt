import logging
import random
import time

import pytest
from ngts.nvos_tools.Devices.BaseDevice import BaseSwitch
from ngts.nvos_tools.Devices.IbDevice import JulietSwitch, RosalindSwitch
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.StressResourcesTool import StressResourcesTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_constants.constants_nvos import PlatformConsts, IbConsts, ApiType, OutputFormat, SystemConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.ib.Ib import Ib
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports, refresh_switch_ports
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.infra.ValidationTool import ExpectedString
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active

logger = logging.getLogger()

# NOTE: Local constants deprecated - use devices.dut.expected_cluster_apps instead
# These are kept temporarily for backward compatibility
NMX_CONTROLLER = 'nmx-controller'
NMX_TELEMETRY = 'nmx-telemetry'
INITIAL_EXPECTED_APPS = [NMX_CONTROLLER, NMX_TELEMETRY]

START_APP_WHILE_CLUSTER_DISABLED_ERR_MSG = 'Output was expected to contain:\nApp has been successfully started\nBut the output is:\nAction executing ...\nError: Action failed with the following issue:\n  cluster is not enabled'
STOP_APP_WHILE_CLUSTER_DISABLED_ERR_MSG = 'Output was expected to contain:\nAction succeeded\nBut the output is:\nAction executing ...\nError: Action failed with the following issue:\n  cluster is not enabled'
CLUSTER_IS_NOT_ENABLED_MESSAGE = 'cluster is not enabled'
INVALID_SHOW_EXPECTED_OUTPUT_NVUE = 'Error: The requested item does not exist.'
INVALID_SHOW_EXPECTED_OUTPUT_OPENAPI = 'Error: Request failed. Details: The requested item does not exist.'
INVALID_SHOW_EXPECTED_OUTPUT = {'NVUE': INVALID_SHOW_EXPECTED_OUTPUT_NVUE, 'OpenApi': INVALID_SHOW_EXPECTED_OUTPUT_OPENAPI}


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.simx
@pytest.mark.nvl_ci
@pytest.mark.timeout(30 * MINUTE, func_only=True)
def test_cluster_app_start_stop(engines, devices, random_api, has_loopbox, standalone_system, setup_name):
    # Skip test for Juliet systems (but NOT Rosalind) while bug SW #4859121 is active
    # Bug: [Juliet] - nmx-controller having "nvbridge" capability - which should not be supported over juliet systems
    # Note: Rosalind systems should run this test
    if isinstance(devices.dut, JulietSwitch) and not isinstance(devices.dut, RosalindSwitch) and is_bug_active(4859121):
        pytest.skip("Skipping test for Juliet systems (non-Rosalind) while bug SW #4859121 is active: "
                    "nmx-controller has 'nvbridge' capability which is not supported on Juliet")

    TestToolkit.tested_api = random_api
    output_format = OutputFormat.json

    def verify_apps_attributes(output):
        app_names = list(output.keys())

        with allure.step("Verify initial existing apps"):
            assert set(app_names) == set(devices.dut.expected_cluster_apps), f"Expected apps:{devices.dut.expected_cluster_apps} Actual apps:{app_names}"

        with allure.step("Verify 'nv show cluster apps' output"):
            if is_bug_active(4207869) and standalone_system:
                cluster_app_nmx_controller = devices.dut.cluster_app_nmx_controller.copy()
                cluster_app_nmx_controller['status'] = ExpectedString(regex=".*")
                cluster_app_nmx_controller['reason'] = ExpectedString(regex=".*")
                ValidationTool.validate_output_of_show(output[NMX_CONTROLLER],
                                                       cluster_app_nmx_controller).verify_result()
            else:
                ClusterTools.wait_for_app_healthy(cluster, NMX_CONTROLLER,
                                                  devices.dut.cluster_app[NMX_CONTROLLER], engine=engines.dut)
                ClusterTools.wait_for_app_healthy(cluster, NMX_TELEMETRY,
                                                  devices.dut.cluster_app[NMX_TELEMETRY], engine=engines.dut)

    with allure.step("Create Cluster object"):
        interface_wa_called = False

        cluster = Cluster()
    try:
        logger.info("Setting cluster state to enabled")
        ClusterTools.start_cluster(cluster, setup_name, output_format, engine=engines.dut, devices=devices)

        with allure.step("Running 'nv show cluster apps' command and parsing output"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.apps.show(output_format=output_format),
                output_format=output_format).get_returned_value()
            verify_apps_attributes(output)

        with allure.step("Running 'nv show cluster apps <app-name>' command and parsing output"):
            for app in devices.dut.expected_cluster_apps:
                output = OutputParsingTool.parse_show_output_to_dict(
                    cluster.apps.app_name[app].show(output_format=OutputFormat.json),
                    output_format=OutputFormat.json).get_returned_value()
                if app == NMX_CONTROLLER:
                    cluster_app_nmx_controller = devices.dut.cluster_app[app].copy()
                    if is_bug_active(4207869) and standalone_system:
                        cluster_app_nmx_controller['status'] = ExpectedString(regex=".*")
                        cluster_app_nmx_controller['reason'] = ExpectedString(regex=".*")
                    ValidationTool.validate_output_of_show(output, cluster_app_nmx_controller).verify_result()
                else:
                    ClusterTools.wait_for_app_healthy(cluster, app,
                                                      devices.dut.cluster_app[app], engine=engines.dut)

        TestToolkit.tested_api = 'NVUE'
        with allure.step("Running 'nv show cluster apps installed' command and verifying output"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.apps.installed.show(output_format=output_format),
                output_format=output_format).get_returned_value()
            for app in devices.dut.expected_cluster_apps:
                ValidationTool.validate_output_of_show(output[app], devices.dut.cluster_app_installed[app]).verify_result()

        with allure.step("Running 'nv show cluster apps running' command and verifying output"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.apps.running.show(output_format=OutputFormat.json),
                output_format=OutputFormat.json).get_returned_value()
            for app in devices.dut.expected_cluster_apps:
                if app == NMX_CONTROLLER and is_bug_active(4207869) and standalone_system:
                    continue
                app_status = output[app]['status']
                assert app_status == 'ok', f"App {app} status is {app_status} instead of 'ok'"
            logger.info("Make sure there are no extra Unexpected apps")
            assert len(devices.dut.expected_cluster_apps) == len(output), f"Expected apps {devices.dut.expected_cluster_apps}, actual apps: {output}"

        # TestToolkit.tested_api = random_api

        ClusterTools.stop_start_app(cluster, engines.dut, devices, has_loopbox, setup_name, standalone_system)

    finally:
        pass


@disabled_access_ports
@pytest.mark.timeout(60 * MINUTE, func_only=True)
@pytest.mark.nmx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_stress_cluster_app_start_stop(engines, devices, test_api, test_name, has_loopbox, standalone_system, setup_name):
    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json

    with allure.step("Create Cluster object"):
        interface_wa_called = False
        cluster = Cluster()
    try:
        with allure.step("Stress testing start/stop apps"):
            operation = 'start stop cluster app'
            if has_loopbox:
                operation = 'start stop cluster app with loopbox'
            ClusterTools.start_cluster(cluster, setup_name, output_format, engine=engines.dut, devices=devices)
            for i in range(4):
                logger.info(f"Starting iteration {i}")
                result_obj, duration = OperationTime.save_duration(operation, '', test_name, ClusterTools.stop_start_app, cluster, engines.dut, devices, has_loopbox, setup_name, standalone_system)
                OperationTime.verify_operation_time(duration, operation, devices).verify_result()

    finally:
        pass


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(35 * MINUTE, func_only=True)
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_cluster_app_start_stop_under_stressed_resources(engines, devices, test_api, test_name, has_loopbox, standalone_system, setup_name):
    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json

    with allure.step("Create Cluster object"):
        interface_wa_called = False
        cluster = Cluster()
        installed_packages = []
    try:
        with allure.step("Test cluster with stressed CPU and Memory utilization"):
            # This will run in background &
            installed_packages = StressResourcesTool.stress_cpu_and_memory(engines, devices.dut.core_count, timeout='600s')
            timeout = 600  # for example, 600 seconds

            # Get the current time
            start_time = time.time()

            # Loop until the timeout is reached
            operation = 'start stop cluster app stressed resources'
            while time.time() - start_time < timeout:
                if has_loopbox:
                    operation = 'start stop cluster app stressed resources with loopbox'
                ClusterTools.start_cluster(cluster, setup_name, output_format, engine=engines.dut, devices=devices)
                result_obj, duration = OperationTime.save_duration(operation, '', test_name, ClusterTools.stop_start_app, cluster, engines.dut, devices, has_loopbox, setup_name, standalone_system)
                OperationTime.verify_operation_time(duration, operation, devices).verify_result()

    finally:
        pass


@pytest.mark.nmx
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_cluster_app_start_stop_disabled_cluster(engines, devices, test_api):
    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json

    with allure.step("Create Cluster object"):
        cluster = Cluster()

    with allure.step("Running 'nv show cluster apps' command and parsing output"):
        output = OutputParsingTool.parse_show_output_to_dict(
            cluster.apps.show(output_format=output_format),
            output_format=output_format).get_returned_value()
        assert output == {}, f"Expected to get empty output, but instead received {output}"

    for app in devices.dut.expected_cluster_apps:
        with allure.step(f"Running 'nv show cluster apps {app}' command and parsing output"):
            output = cluster.apps.app_name[app].show(output_format=OutputFormat.json, should_succeed=False)
            assert INVALID_SHOW_EXPECTED_OUTPUT[test_api] in output, f"Expected {INVALID_SHOW_EXPECTED_OUTPUT[test_api]}, but instead received {output} "

    TestToolkit.tested_api = 'NVUE'

    with allure.step("Running 'nv show cluster apps installed' command and verifying output"):
        output = OutputParsingTool.parse_show_output_to_dict(
            cluster.apps.installed.show(output_format=output_format),
            output_format=output_format).get_returned_value()
        assert output == {}, f"Expected to get empty output, but instead received {output}"

    with allure.step("Running 'nv show cluster apps running' command and verifying output"):
        output = OutputParsingTool.parse_show_output_to_dict(
            cluster.apps.running.show(output_format=output_format),
            output_format=output_format).get_returned_value()
        assert output == {}, f"Expected to get empty output, but instead received {output}"

    TestToolkit.tested_api = test_api
    with allure.step("Start/Stop apps"):
        for app in devices.dut.expected_cluster_apps:
            with allure.step(f"Start app {app} and validate action fails"):
                output = cluster.apps.app_name[app].action_start_cluster_app().get_returned_value(False)
                assert CLUSTER_IS_NOT_ENABLED_MESSAGE in output, f"Expected output to contain {CLUSTER_IS_NOT_ENABLED_MESSAGE}, actual output {output}"
            with allure.step(f"Stop app {app} and validate action fails"):
                output = cluster.apps.app_name[app].action_stop_cluster_app().get_returned_value(False)
                assert CLUSTER_IS_NOT_ENABLED_MESSAGE in output, f"Expected output to contain {CLUSTER_IS_NOT_ENABLED_MESSAGE}, actual output {output}"
