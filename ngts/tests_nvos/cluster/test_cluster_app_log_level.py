import logging
import random
import time

import pytest

from ngts.nvos_constants.constants_nvos import OutputFormat, ApiType
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.StressResourcesTool import StressResourcesTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active

logger = logging.getLogger()


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.simx
@pytest.mark.timeout(30 * MINUTE, func_only=True)
def test_cluster_app_log_level(engines, devices, random_api, has_loopbox, standalone_system, setup_name):
    output_format = OutputFormat.json

    try:
        with allure.step("Create Cluster object"):
            cluster = Cluster()
            system = System()
            logger.info("Setting cluster state to enabled")
            ClusterTools.start_cluster(cluster, setup_name, output_format, devices=devices)
        with allure.step("Validate initial log level"):
            for app in devices.dut.expected_cluster_apps:
                ClusterTools.verify_log_level(ClusterConsts.DEFAULT_LOG_LEVEL, app, output_format, cluster)

        if not is_bug_active(4266900):
            TestToolkit.tested_api = 'NVUE'
            with allure.step("Set log level to undefined log level"):
                for app in devices.dut.expected_cluster_apps:
                    output = cluster.apps.app_name[app].loglevel.action_update_cluster_log_level(level='undefined').get_returned_value(should_succeed=False)
                    assert ClusterConsts.UNDEFINED_LOG_LEVEL in output, f"Expected {ClusterConsts.UNDEFINED_LOG_LEVEL}, Actual: {output}"
                    ClusterTools.verify_log_level(ClusterConsts.DEFAULT_LOG_LEVEL, app, output_format, cluster)

        with allure.step("Choose random log level, and set cluster app log level to"):
            log_level = random.choice(ClusterConsts.ClusterAppsLogLevelsList)
            for app in devices.dut.expected_cluster_apps:
                cluster.apps.app_name[app].loglevel.action_update_cluster_log_level(level=log_level)
                ClusterTools.verify_log_level(log_level, app, output_format, cluster)

    finally:
        TestToolkit.tested_api = 'NVUE'
        for app in devices.dut.expected_cluster_apps:
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.apps.running.show(output_format=OutputFormat.json),
                output_format=OutputFormat.json).get_returned_value()
            app_status = output[app]['status']
            if app_status != 'ok':
                ClusterTools.stop_app(cluster, app)
                ClusterTools.start_app(cluster, app, has_loopbox, standalone_system)
                time.sleep(5)
            cluster.apps.app_name[app].loglevel.action_restore_cluster()
            ClusterTools.verify_log_level(ClusterConsts.DEFAULT_LOG_LEVEL, app, output_format, cluster)


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
@pytest.mark.simx
def test_cluster_app_log_level_under_stress(engines, devices, test_api, test_name, has_loopbox, standalone_system, setup_name):
    TestToolkit.tested_api = test_api
    output_format = OutputFormat.json

    try:
        with allure.step("Create Cluster object"):
            cluster = Cluster()
            system = System()
            logger.info("Setting cluster state to enabled")
            installed_packages = []
            ClusterTools.start_cluster(cluster, setup_name, output_format, devices=devices)

            installed_packages = StressResourcesTool.stress_cpu_and_memory(engines, devices.dut.core_count)
            timeout = 300  # for example, 300 seconds

            # Get the current time
            start_time = time.time()

        while time.time() - start_time < timeout:
            with allure.step("Choose random log level, and set cluster app log level to"):
                log_level = random.choice(ClusterConsts.ClusterAppsLogLevelsList)
                for app in devices.dut.expected_cluster_apps:
                    result_obj, duration = OperationTime.save_duration('cluster update log level', '', test_name, cluster.apps.app_name[app].loglevel.action_update_cluster_log_level, level=log_level)
                    OperationTime.verify_operation_time(duration, 'cluster update log level', devices).verify_result()
                    ClusterTools.verify_log_level(log_level, app, output_format, cluster)
    finally:
        if installed_packages:
            StressResourcesTool.delete_packages(engines, installed_packages)
        for app in devices.dut.expected_cluster_apps:
            with allure.step("Make sure apps are still running"):
                output = OutputParsingTool.parse_show_output_to_dict(
                    cluster.apps.running.show(output_format=OutputFormat.json),
                    output_format=OutputFormat.json).get_returned_value()
                app_status = output[app]['status']
                with allure.step("Start apps that were stopped"):
                    if app_status != 'ok':
                        ClusterTools.stop_app(cluster, app)
                        ClusterTools.start_app(cluster, app, has_loopbox, standalone_system)
            with allure.step("Restore log level"):
                cluster.apps.app_name[app].loglevel.action_restore_cluster()
                ClusterTools.verify_log_level(ClusterConsts.DEFAULT_LOG_LEVEL, app, output_format, cluster)
