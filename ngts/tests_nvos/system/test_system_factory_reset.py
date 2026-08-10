import pytest
import logging

from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.RegressionConfigurations import RegressionConfigurations
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.system.factory_reset.helpers import *
from ngts.tests_nvos.system.factory_reset.helpers import verify_cleanup_done, verify_the_setup_is_functional
from ngts.tests_nvos.system.factory_reset.post_steps import factory_reset_no_params_post_steps
from ngts.tests_nvos.system.factory_reset.pre_steps import (factory_reset_no_params_pre_steps,
                                                            factory_reset_keep_basic_pre_steps,
                                                            factory_reset_general_pre_steps)
from ngts.tests_nvos.system.test_system_profile_change import update_timezone
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_constants.constants_nvos import SystemConsts, NvosConst, RebootConsts, HealthConsts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.reboot_telemetry_helpers import (
    RebootReasonCategory,
    assert_nvue_gnmi_counters_match,
    gnmi_client_for_dut,
    take_reboot_telemetry_snapshot,
    verify_reboot_telemetry_after_reboot,
)


cumulus_owner = "lharo"


logger = logging.getLogger()


@pytest.mark.timeout(50 * MINUTE)
@pytest.mark.system
@pytest.mark.checklist
@pytest.mark.reset_factory
def test_reset_factory_without_params(engines, devices, topology_obj, platform_params, random_api, has_loopbox,
                                      setup_name, standalone_system, test_name, serial_log_analyzers, verify_no_kernel_errors, ib_router):
    """
    Validate reset factory without params cleanup done as expected

        Test flow:
            1.  Set description to ib/nvl ports:
                 - set, apply and save configuration
                 - set and apply
                 - set
            2.  Validate ports description
            3.  Add system contact information
            4.  Add system location information
            5.  Add data
            6.  Run reset factory without params
            7.  After system is up again, verify the cleanup done successfully
            8.  Validate description is deleted
            9.  Validate system contact information is deleted
            10. Validate system location information is deleted
            11. Verify the setup is functional:
                11.1.	Run several show commands
                11.2.    Run set command & apply
    """
    serial_analyzer = list(serial_log_analyzers.values())[0]
    system = System()
    cluster = Cluster()
    expected_reason, expected_user = devices.dut.reboot_reason_dict[RebootConsts.FACTORY_RESET]
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)

    with allure.step('pre factory reset steps'):
        apply_and_save_port, current_time, just_apply_port, health_status, machine_type, not_apply_port, \
            username, init_cluster_status = factory_reset_no_params_pre_steps(engines, platform_params, system, devices,
                                                                              has_loopbox, setup_name, standalone_system)

    with allure.step("NVUE and gNMI reboot counters must match before factory reset"):
        telemetry_before = take_reboot_telemetry_snapshot(system, gnmi_client)
        assert_nvue_gnmi_counters_match(telemetry_before)

    with allure.step("Run reset factory without params"):
        with serial_analyzer.stage('Reset-factory'):
            duration = execute_reset_factory(engines, system, devices.dut.reset_factory, "", current_time, test_name=test_name)

    ValidationTool.validate_reboot_reason_and_user(system, expected_reason, expected_user)

    with allure.step("Verify NVUE and gNMI reboot telemetry after factory reset"):
        verify_reboot_telemetry_after_reboot(
            snapshot_before=telemetry_before,
            system=system,
            gnmi_client=gnmi_client,
            expected_category=RebootReasonCategory.USER_INITIATED,
            expected_details=expected_reason,
            expected_user=expected_user,
            reset_factory=True,
        )

    with allure.step('post factory reset steps'):
        factory_reset_no_params_post_steps(apply_and_save_port, engines, just_apply_port, health_status,
                                           machine_type, not_apply_port, system, init_cluster_status, has_loopbox,
                                           devices, setup_name, standalone_system)
        RegressionConfigurations.configure_ports_to_legacy(engine=engines.dut, apply=True, throw_exception=True)

    with allure.step("Verify the cleanup done successfully"):
        verify_cleanup_done(engines.dut, current_time, system, username, ib_router=ib_router)

    with allure.step("Verify the setup is functional"):
        verify_the_setup_is_functional(system, engines)

    with allure.step("Verify operation time"):
        OperationTime.verify_operation_time(duration, devices.dut.reset_factory, devices).verify_result()

    with allure.step('Check if NVL Switch'):
        if devices.dut.switch_type == NvosConst.NVL_SWITCH_TYPE:
            cluster.unset(apply=True)


@pytest.mark.timeout(25 * MINUTE, func_only=True)
@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.checklist
@pytest.mark.reset_factory
def test_reset_factory_keep_basic(engines, devices, random_api, test_name, serial_log_analyzers, ib_router):
    """
    Validate reset factory with keep basic param cleanup done as expected

        Test flow:
            1. set description to eth0 port:
                - set, apply and save configuration
                - set and apply
                - set
            2. Validate ports description
            3. Add data
            4. Run reset factory with keep basic param
            5. After system is up again, verify the cleanup done successfully
            6. Verify the setup is functional:
                6.1.	Run several show commands
                6.2.    Run set command & apply
    """
    serial_analyzer = list(serial_log_analyzers.values())[0]
    with allure.step('Create System object'):
        system = System()

    with allure.step('pre factory reset steps'):
        current_time, username, health_status, mgmt_port, \
            output_dictionary_mgmt_show, dns_server_id = factory_reset_keep_basic_pre_steps(engines, system)

    with allure.step("Run reset factory with keep basic param"):
        with serial_analyzer.stage('Reset-factory'):
            duration = execute_reset_factory(engines, system, devices.dut.reset_factory, KEEP_BASIC, current_time, test_name=test_name)

    update_timezone(system)

    with allure.step("Validate health status and report"):
        validate_health_status_report(system, health_status)

    with allure.step("Verify the cleanup done successfully"):
        verify_cleanup_done(engines.dut, current_time, system, username, param=KEEP_BASIC, ib_router=ib_router,
                            dns_server_id=dns_server_id)

        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary_mgmt_show,
                                                          field_name=NvosConst.DESCRIPTION,
                                                          expected_value='nvosdescription')

        mgmt_port.interface.unset(NvosConst.DESCRIPTION, apply=True, ask_for_confirmation=True).verify_result()

    with allure.step("Verify the setup is functional"):
        verify_the_setup_is_functional(system, engines)

    with allure.step("Verify operation time"):
        OperationTime.verify_operation_time(duration, devices.dut.reset_factory, devices).verify_result()


@pytest.mark.timeout(25 * MINUTE)
@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.checklist
@pytest.mark.reset_factory
def test_reset_factory_keep_all_config(engines, devices, random_api, test_name, serial_log_analyzers, verify_no_kernel_errors, ib_router):
    """
    Validate reset factory with keep all config param cleanup done as expected

        Test flow:
            1. set description to ib/nvl ports:
                - set, apply and save configuration
                - set and apply
                - set
            2. Validate ports description
            3. Add data
            4. Run reset factory with keep all config params
            5. After system is up again, verify the cleanup done successfully
            6. Verify the setup is functional:
                6.1.	Run several show commands
                6.2.    Run set command & apply
    """
    serial_analyzer = list(serial_log_analyzers.values())[0]
    with allure.step('Create System object'):
        system = System()

    with allure.step('pre factory reset steps'):
        health_status, current_time, apply_and_save_port, description, just_apply_port, \
            not_apply_port, username = factory_reset_general_pre_steps(engines, devices, system)

    with allure.step("Run reset factory with keep all-config param"):
        with serial_analyzer.stage('Reset-factory'):
            duration = execute_reset_factory(engines, system, devices.dut.reset_factory, KEEP_ALL_CONFIG, current_time, test_name=test_name)

    update_timezone(system)

    with allure.step('Validate ports description after reset factory'):
        logger.info("Validate ports description after reset factory")
        validate_port_description(engines.dut, apply_and_save_port, description)
        validate_port_description(engines.dut, just_apply_port, "")
        validate_port_description(engines.dut, not_apply_port, "")

    with allure.step("Validate health status and report"):
        validate_health_status_report(system, health_status)

    with allure.step("Verify the cleanup done successfully"):
        verify_cleanup_done(engines.dut, current_time, system, username, param=KEEP_ALL_CONFIG, ib_router=ib_router)

    with allure.step("Verify the setup is functional"):
        verify_the_setup_is_functional(system, engines)

    with allure.step("Verify operation time"):
        OperationTime.verify_operation_time(duration, devices.dut.reset_factory, devices).verify_result()


@pytest.mark.timeout(30 * MINUTE)
@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.checklist
@pytest.mark.reset_factory
def test_reset_factory_keep_only_files(engines, devices, random_api, test_name, serial_log_analyzers, ib_router):
    """
    Validate reset factory with keep only files param cleanup done as expected

        Test flow:
            1. set description to ib/nvl ports:
                - set, apply and save configuration
                - set and apply
                - set
            2. Validate ports description
            3. Add data
            4. Run reset factory with keep only files param
            5. After system is up again, verify the cleanup done successfully
            6. Verify the setup is functional:
                6.1.	Run several show commands
                6.2.    Run set command & apply
    """
    serial_analyzer = list(serial_log_analyzers.values())[0]
    with allure.step('Create System object'):
        system = System()

    with allure.step('pre factory reset steps'):
        health_status, current_time, apply_and_save_port, description, just_apply_port, \
            not_apply_port, username = factory_reset_general_pre_steps(engines, devices, system)

    with allure.step("Run reset factory keep only-files"):
        with serial_analyzer.stage('Reset-factory'):
            duration = execute_reset_factory(engines, system, devices.dut.reset_factory, KEEP_ONLY_FILES, current_time, test_name=test_name)

    update_timezone(system)

    with allure.step("Validate health status and report"):
        validate_health_status_report(system, health_status)

    with allure.step("Verify the cleanup done successfully"):
        verify_cleanup_done(engines.dut, current_time, system, username, param=KEEP_ONLY_FILES, ib_router=ib_router)

    with allure.step("Verify the setup is functional"):
        verify_the_setup_is_functional(system, engines)

    with allure.step("Verify operation time"):
        OperationTime.verify_operation_time(duration, devices.dut.reset_factory, devices).verify_result()


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.checklist
def test_error_flow_reset_factory_with_params(random_api, engines, devices, topology_obj):
    """
    This test is a temporary test - will be changed for GA
    :return:
    """
    with allure.step("Run reset factory with params - expect failure"):
        logging.info("Run reset factory with params - expect failure")
        output = engines.dut.run_cmd("nv action reset system factory-default only-config")
        assert "Invalid parameter" in output, "Reset factory with param should fail"


def execute_reset_factory(engines, system, operation, flag, current_time, topology_obj=None, test_name=''):
    logging.info("Current time: " + str(current_time))
    topology_obj = topology_obj or (TestToolkit.topology_obj if TestToolkit else None)
    result_obj = system.factory_default.action_reset(operation=operation, param=flag, topology_obj=topology_obj, test_name=test_name)
    result_obj.verify_result()
    return result_obj.duration


def get_last_status_line(system):
    with allure.step('Validate health status is OK'):
        logger.info("Validate health status is OK")
        try:
            system.validate_health_status(HealthConsts.OK)
            return system.health.history.retry_get_health_history_file_summary_line()
        except BaseException:
            return ""
