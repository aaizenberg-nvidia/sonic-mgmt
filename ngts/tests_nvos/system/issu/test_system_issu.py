import concurrent.futures
import logging
import os
import pytest
import random
import shlex
import subprocess
import time

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.nvos_constants.constants_nvos import (ActionConsts, ApiType, DatabaseConst, HealthConsts, IbConsts, IpConsts,
                                                IssuConsts, NvosConst, PlatformConsts, SystemConsts)
from ngts.tests_nvos.constants import FW_COMPONENT_SSD
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.opensm.OpenSmTool import OpenSmTool
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.DatabaseTool import DatabaseTool
from ngts.nvos_tools.infra.DutUtilsTool import ping_device
from ngts.nvos_tools.infra.FWComponentsTool import FWComponentsTool
from ngts.nvos_tools.infra.SSDTool import SSDTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RegressionConfigurations import Configurations, RegressionConfigurations
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.InterfaceConfigurationTool import InterfaceConfigurationTool
from ngts.nvos_tools.infra.NvosGitTool import NvosGitTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.nvos_constants.constants_nvos import ImageConsts
from ngts.scripts.sonic_deploy.nvos_only_methods import NvosInstallationSteps
from ngts.tests_nvos.system import helpers as system_helpers
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_config_utils import clear_conf
from retry import retry
from ngts.tests_nvos.platform.firmware_telemetry_helpers import (
    assert_gnmi_firmware_version_matches_nvue,
)
from ngts.tests_nvos.system.reboot_telemetry_helpers import gnmi_client_for_dut

logger = logging.getLogger()


def _nvos_version_key(version_path: str) -> tuple:
    """Convert an NVOS image path (or version string) to a comparable tuple.

    e.g. '25.02.7002-003' -> (25, 2, 7002, 3); '25.02.5035' -> (25, 2, 5035, 0).
    """
    if version_path.endswith('.bin') or '/' in version_path:
        version_str, _ = NvosGitTool.parse_version_from_path(version_path)
    else:
        version_str = version_path
    main, _, suffix = version_str.partition('-')
    parts = [int(p) for p in main.split('.')]
    parts.append(int(suffix) if suffix else 0)
    return tuple(parts)


@pytest.mark.system
@pytest.mark.issu
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_system_issu_positive_basic_flow(engines, devices, issu_version, target_version, test_api):
    """
    Validates basic image install with issu

    Test flow:
    1. Fetch and install issu image (without ISSU)
    2. Fetch and install target image with ISSU skip-sm flag
    3. Show ISSU time
    4. Verify show ISSU status
    5. Verify image version
    6. Verify default ACL JSON and control-plane / loopback ACL bindings preserved after ISSU
    """
    dut_engine = engines.dut
    dut_device = devices.dut
    player = engines.sonic_mgmt
    system = System()
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)
    acl_persist_mangle_state = None
    # In case of manual run add the following lines with the relevant paths
    # issu_version = '/auto/sw_system_release/nos/nvos/25.02.4014/amd64/prod/nvos-amd64-25.02.4014.bin'
    # target_version = '/auto/sw_system_release/nos/nvos/25.02.5014/amd64/prod/nvos-amd64-25.02.5014.bin'

    target_version = player.run_cmd(f'ls {target_version}')
    target_version_num = target_version.split('/')[-1].replace('amd64-', '').replace('.bin', '')

    with (allure.step('Verify image versions')):
        system_version = OutputParsingTool.parse_json_str_to_dictionary(
            system.version.show()).get_returned_value()['image']
        if ImageConsts.BUILD_ID in system_version:
            system_version = system_version[ImageConsts.BUILD_ID]
        if system_version == target_version_num:
            target_fw = OutputParsingTool.parse_json_str_to_dictionary(
                Platform().firmware.show(dut_engine=dut_engine)).get_returned_value()['ASIC']['actual-firmware']
        else:
            target_fw = ''

    with allure.step("Downgrade system image to issu"):
        install_system_image_and_start_opensm(engines, dut_device, system, issu_version, False)

    with allure.step("Prepare system target image for install"):
        target_filename, recovery_engine, scp_host_creds = prepare_image_for_install(
            player, dut_engine, dut_device, target_version)

    with allure.step("Add ACL/control-plane configs for persistence checks (before baseline capture)"):
        acl_persist_mangle_state = system_helpers.add_acl_new_configs_for_persistence_checks(
            dut_engine=dut_engine)

    with allure.step("Save default ACL rules baseline (before ISSU)"):
        json_acl_rules = system_helpers.extract_acl_rules(dut_engine=dut_engine)
        logger.info("ACL baseline captured for %d default ACL object(s)", len(json_acl_rules))

    with allure.step("Save control-plane ACL bindings baseline (nv show system control-plane acl)"):
        cp_acl_bindings = system_helpers.extract_control_plane_acl_bindings(
            dut_engine=dut_engine, mangle_state=acl_persist_mangle_state)
        logger.info("Control-plane ACL bindings captured: %s", cp_acl_bindings)

    issu_start = time.time()
    logger.info(f"ISSU start time: {issu_start}")

    with allure.step("Perform install image with ISSU skip-sm flag"):
        system.image.files.file_name[target_filename].action(
            ActionConsts.INSTALL, flags=IssuConsts.ISSU_SKIP_SM, send_user_confirmation='y', reboot_params=True,
            timeout=IssuConsts.ISSU_READ_TIMEOUT). \
            verify_result(should_succeed=True)

    issu_end = time.time()
    logger.info(f"ISSU end time: {issu_end}")
    issu_diff = issu_end - issu_start
    logger.info(f"ISSU diff time: {issu_diff}")

    with allure.step('Verify show ISSU status'):
        issu_status = OutputParsingTool.parse_json_str_to_dictionary(
            system.image.show()).get_returned_value()[IssuConsts.ISSU_STATUS]
        assert issu_status == IssuConsts.IssuStatus.NO_ISSU.value, \
            f"ISSU status is {issu_status}, instead of: {IssuConsts.IssuStatus.NO_ISSU.value}"

    with (allure.step('Verify image version')):
        system_version = OutputParsingTool.parse_json_str_to_dictionary(
            system.version.show()).get_returned_value()['image']
        if ImageConsts.BUILD_ID in system_version:
            system_version = system_version[ImageConsts.BUILD_ID]
        assert system_version == target_version_num, (f'system image is: {system_version}, '
                                                      f'instead of {target_version_num}')

    if target_fw:
        with (allure.step('Verify fw versions')):
            fw_version = OutputParsingTool.parse_json_str_to_dictionary(
                Platform().firmware.show(dut_engine=dut_engine)).get_returned_value()['ASIC']['actual-firmware']
            assert fw_version == target_fw, f'FW version is: {fw_version}, instead of {target_fw}'
            for asic_name in getattr(dut_device, "asic_numbers", ["ASIC1"]):
                assert_gnmi_firmware_version_matches_nvue(gnmi_client, asic_name, target_fw)

    with allure.step('Verify default ACL and control-plane ACL preserved after ISSU'):
        with allure.independent_step('Verify default ACL JSON preserved after ISSU (nv show acl)'):
            system_helpers.verify_acl_rules_preserved(
                json_acl_rules, mangle_state=acl_persist_mangle_state, dut_engine=dut_engine)
        with allure.independent_step(
            'Verify control-plane ACL bindings + loopback defaults on interface lo after ISSU'
        ):
            system_helpers.verify_control_plane_acl_bindings(cp_acl_bindings, dut_engine=dut_engine)

    with allure.step('Clear ACL persistence mangle state (if any)'):
        system_helpers.clear_acl_configs(acl_persist_mangle_state, dut_engine=dut_engine)


@pytest.mark.system
@pytest.mark.issu
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
@pytest.mark.parametrize('issu_base', IssuConsts.IssuBase.ALL_TYPES)
def test_system_issu_positive_flow_with_traffic(engines, devices, pytestconfig, issu_version,
                                                target_version, issu_base, test_api, test_name):
    """
    Validates basic image install with issu

    Test flow:
    1. Fetch and install issu image (without ISSU)
    2. Fetch and install target image with ISSU flag
    3. Show ISSU time
    4. Verify show ISSU status
    5. Verify image version
    6. Post-ISSU: verify default ACL JSON and control-plane / loopback ACL bindings (baseline from pre-ISSU)
    """
    dut_engine = engines.dut
    dut_device = devices.dut
    player = engines.sonic_mgmt
    system = System()

    # In case of manual run add the following lines with the relevant paths
    # issu_base_version = '/auto/sw_system_release/nos/nvos/25.02.4014/amd64/dev/nvos-amd64-25.02.4014.bin'
    # target_version = '/auto/sw_system_release/nos/nvos/25.02.5014/amd64/dev/nvos-amd64-25.02.5014.bin'

    with allure.step(f'Start test ISSU with traffic for {issu_base} image'):
        if issu_base == IssuConsts.IssuBase.LAST_GA:
            issu_base_version = issu_version
        else:  # issu_base == IssuConsts.IssuBase.LAST_FW
            if pytestconfig.issu_last_fw_path == issu_version:
                pytest.skip("ISSU last FW image is also the last GA image, therefor test is skipped")
            issu_base_version = pytestconfig.issu_last_fw_path

    target_version = player.run_cmd(f'ls {target_version}')
    target_version_num = target_version.split('/')[-1].replace('amd64-', '').replace('.bin', '')

    with allure.step('Verify image versions'):
        system_version = OutputParsingTool.parse_json_str_to_dictionary(
            system.version.show()).get_returned_value()['image']
        if ImageConsts.BUILD_ID in system_version:
            system_version = system_version[ImageConsts.BUILD_ID]
        if system_version == target_version_num:
            fw_version = OutputParsingTool.parse_json_str_to_dictionary(
                Platform().firmware.show(dut_engine=dut_engine)).get_returned_value()['ASIC']['actual-firmware']
        else:
            fw_version = ''

    with allure.step("Downgrade system image to the issu_base_version"):
        install_system_image_and_start_opensm(engines, dut_device, system, issu_base_version)

    with allure.step("Prepare system target image for install"):
        target_filename, recovery_engine, scp_host_creds = prepare_image_for_install(
            player, dut_engine, dut_device, target_version)

    with allure.step('Pre issu installation steps'):
        (traffic_start_time, interface_dict, ip_list, mtu_info, ssd_should_auto_update,
         json_acl_rules, cp_acl_bindings, acl_persist_mangle_state) = pre_issu_installation_steps(
            engines, devices, target_version, scp_host_creds, issu_base_version=issu_base_version)

    issu_start = time.time()
    logger.info(f"ISSU start time: {issu_start}")

    with allure.step("Perform install image with ISSU flag"):
        system.image.files.file_name[target_filename].action(
            ActionConsts.INSTALL, flags=IssuConsts.ISSU, send_user_confirmation='y', reboot_params=True,
            timeout=IssuConsts.ISSU_READ_TIMEOUT). \
            verify_result(should_succeed=True)

    issu_end = time.time()
    logger.info(f"ISSU end time: {issu_end}")
    issu_diff = issu_end - issu_start
    logger.info(f"ISSU diff time: {issu_diff}")

    with allure.step('Post issu installation steps'):
        post_issu_installation_steps(engines, devices, target_version, fw_version,
                                     interface_dict, traffic_start_time, ip_list, test_name,
                                     mtu_info=mtu_info, ssd_should_auto_update=ssd_should_auto_update,
                                     json_acl_rules=json_acl_rules, cp_acl_bindings=cp_acl_bindings,
                                     acl_persist_mangle_state=acl_persist_mangle_state)


# @pytest.mark.system
# @pytest.mark.issu
# @pytest.mark.parametrize('test_api', [ApiType.NVUE])
# def test_system_issu_positive_flow(engines, devices, issu_version, target_version, test_api, test_name):
#     """
#     Validate:
#     - Upgrade is successfully done (system boots up into new version of OS and FW)
#     - Management services restored: GNMI, SNMP, Rsyslog, Remote AAA, NTP, etc. (high CPU stress)
#     - All configuration saved (System, Interface, Platform…)
#     - No traffic loss during the ISSU process (high-traffic load)
#     - Counters increased as expected. Resumed after ISSU even if the user cleaned them before
#     - Rest/SSH connectivity cut-off during the process
#     - Rest/SSH should recover
#     - Measure both mgmt ports downtime (up to 1 min)
#     - No errors
#     - Can run show command during ISSU + see ISSU status changed (no_issu, in_progress, done)
#     - Events table
#     - Robustness – ISSU from version X -> Y -> Z
#
#     Test flow:
#     1. Change and apply configuration for several resources (system, interface, platform)
#     2. Run management services
#     3. Clear counters
#     4. Clear system log (rotate)
#     5. Show system ISSU
#     6. Start Ping mgmt. port 0 and mgmt. port 1
#     7. Start sending data packets from Host A to Host B
#     8. Install system image with ISSU
#     9. Show system ISSU (from another session)
#     10. Wait for the ISSU to finish...
#     11. Stop Ping mgmt. port 0 and mgmt. port 1 and analyze both logs
#     12. Stop sending data packets from Host A to Host B and analyze log
#     13. Show system ISSU
#     14. Validate versions
#     15. Check port 1 and port 2 counters
#     16. Validate configuration
#     17. Validate management services
#     18. Validate system log
#     19. Validate event table
#     20. Run another upgrade with ISSU to newer image
#     """
#     TestToolkit.tested_api = random_api
#     dut_engine = engines.dut
#     dut_device = devices.dut
#     player = engines.sonic_mgmt
#     system = System()
#
#     # In case of manual run add the following lines with the relevant paths
#     # issu_version = '/auto/sw_system_release/nos/nvos/25.02.4014/amd64/dev/nvos-amd64-25.02.4014.bin'
#     # target_version = '/auto/sw_system_release/nos/nvos/25.02.5004-005/amd64/dev/nvos-amd64-25.02.5004-005.bin'
#
#     target_version = player.run_cmd(f'ls {target_version}')
#     target_version_num = target_version.split('/')[-1].replace('amd64-', '').replace('.bin', '')
#
#     with (allure.step('Verify image versions')):
#         system_version = OutputParsingTool.parse_json_str_to_dictionary(
#             system.version.show()).get_returned_value()['image']
#         if ImageConsts.BUILD_ID in system_version:
#             system_version = system_version[ImageConsts.BUILD_ID]
#         if system_version == target_version_num:
#             fw_version = OutputParsingTool.parse_json_str_to_dictionary(
#                 Platform().firmware.show(dut_engine=dut_engine)).get_returned_value()['ASIC']['actual-firmware']
#         else:
#             fw_version = ''
#
#     with allure.step("Prepare system issu image for install"):
#         issu_filename, recovery_engine, scp_host_creds = prepare_image_for_install(
#             player, dut_engine, dut_device, issu_version)
#
#     with allure.step("Install issu version image (without ISSU)"):
#         system.image.files.file_name[issu_filename].action_file_install_with_reboot(
#             force=False, engine=dut_engine, device=dut_device, recovery_engine=recovery_engine,
#             should_succeed=True, press_y=True).verify_result(should_succeed=True)
#
#     with allure.step("Save configuration"):
#         TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)
#
#     with allure.step(f"Reduce ISSU timeout to {IssuConsts.REDUCED_TIMEOUT} seconds"):
#         reduce_issu_timeout(engines.dut, IssuConsts.REDUCED_TIMEOUT)
#
#     with allure.step("Prepare system target image for install"):
#         target_filename, recovery_engine, scp_host_creds = prepare_image_for_install(
#             player, dut_engine, dut_device, target_version)
#
#     with allure.step('Pre issu installation steps'):
#         traffic_start_time, interface_dict, ip_list = pre_issu_installation_steps(
#             engines, devices, target_version, scp_host_creds)
#
#     with allure.step("Running on 2 sessions in parallel:"):
#         with allure.step(f'Create another session'):
#             connection = ConnectionTool.create_ssh_conn(engines.dut.ip, engines.dut.username,
#                                                         engines.dut.password).get_returned_value()
#
#         with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
#             with allure.step("Perform install image with iSSU flag"):
#                 executor.submit(run_install_system_image_issu, connection, dut_device,
#                                 recovery_engine, target_filename, IssuConsts.ISSU, True)
#             issu_start = time.time()
#             logger.info(f"ISSU start time: {issu_start}")
#
#             # install image process will start immediately, and only when finish - update issu status to
#             # "in-progress" and start requesting openSM response.
#             # with allure.step(f'Verify ISSU status is: {IssuConsts.IssuStatus.IN_PROGRESS.value}'):
#             #     wait_for_image_status_update(system, IssuConsts.IssuStatus.IN_PROGRESS.value)
#
#             # verify openSM status in updated to "yes" in all asics
#             with allure.step("Wait for opensm status update to 'yes' in all asics"):
#                 wait_for_opensm_status_update(dut_engine, dut_device, IssuConsts.OPENSM_RESPONSE_YES)
#
#     # reach here only when install ISSU action is done
#     # with allure.step('Wait until switch is up'):
#     #     dut_engine.disconnect()  # force engines.dut to reconnect
#     #     # after upgrade flow switch has new default password
#     #     dut_engine.password = dut_device.get_default_password_by_version(target_version)
#
#     with allure.step('Post issu installation steps'):
#         post_issu_installation_steps(engines, devices, target_version, fw_version,
#                                      interface_dict, traffic_start_time, ip_list, test_name)


@pytest.mark.system
@pytest.mark.issu
def test_system_issu_prevention_cases(engines, devices, downgrade_version, issu_version, target_version, random_api):
    """
    Validate:
    - No permission to upgrade system from SM
    - No response to upgrade request from SM (reaching timeout)
    - ISSU cannot be started with reboot no flag, and block the ISSU
    - Unsaved configuration blocks the ISSU
    - Perform ISSU with no SM in the cluster using the no-sm flag succeeds
    - Reason for failed ISSU is written to the logs (no permission from SM, no response from SM, FW is not ISSU-able,
        fail to install new FW, etc.)
    - Events table

    Note: the "downgrade blocks ISSU" sub-case lives in test_system_issu_downgrade_prevention
    so that it can be skipped at the pytest level when the configured downgrade_version is
    not strictly lower than issu_version.

    Test flow:
    1. Simulate no permission from the SM by changing DB value
    2. Close the SM, change TO value to 1 min and run ISSU
    3. Perform ISSU with "no reboot" flag
    4. Change configuration without saving and run ISSU
    5. Perform ISSU with an invalid flag
    6. Validate system log
    7. Validate event table
    8. Perform ISSU with no-sm flag
    """
    dut_engine = engines.dut
    dut_device = devices.dut
    player = engines.sonic_mgmt
    system = System()

    # In case of manual run add the following lines with the relevant paths
    # downgrade_version = '/auto/sw_system_release/nos/nvos/25.02.4014/amd64/dev/nvos-amd64-25.02.4014.bin'
    # issu_version = '/auto/sw_system_release/nos/nvos/25.02.5002/amd64/dev/nvos-amd64-25.02.5002.bin'
    # target_version = '/auto/sw_system_release/nos/nvos/25.02.5008/amd64/dev/nvos-amd64-25.02.5008.bin'

    target_version = player.run_cmd(f'ls {target_version}')

    with allure.step("Downgrade system image to issu"):
        install_system_image_and_start_opensm(engines, dut_device, system, issu_version)

    with allure.step(f"Reduce ISSU timeout to {IssuConsts.REDUCED_TIMEOUT} seconds"):
        reduce_issu_timeout(engines.dut, IssuConsts.REDUCED_TIMEOUT)

    with allure.step("Prepare system target image for install"):
        target_filename, recovery_engine, scp_host_creds = prepare_image_for_install(
            player, dut_engine, dut_device, target_version)

    with allure.step("Stop OpenSM"):
        OpenSmTool.stop_open_sm(engines).verify_result()

    with allure.step('Clear system log (rotate)'):
        system.log.rotate_logs()

    # with allure.step("Simulate no permission from the SM by changing DB value"):
    #     with allure.step("Clear OpenSM response value in DB"):
    #         set_opensm_response_status(dut_engine, dut_device, IssuConsts.OPENSM_RESPONSE_CLEAR)
    #
    #     with allure.step("Running on 2 sessions in parallel:"):
    #         with allure.step(f'Create another session'):
    #             connection = ConnectionTool.create_ssh_conn(engines.dut.ip, engines.dut.username,
    #                                                         engines.dut.password).get_returned_value()
    #
    #         with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
    #             with allure.step("Perform install image with iSSU"):
    #                 executor.submit(run_install_system_image_issu, connection, dut_device,
    #                                 recovery_engine, target_filename, IssuConsts.ISSU, False)
    #
    #             with allure.step("Wait for opensm status update in all asics"):
    #                 # in positive flow, the request to OpenSM should be sent after upgrading OS (~60 secs) and
    #                 # upgrading FW (~100 secs), therefore we'll wait up to 4 minutes for changing OpenSM status value in
    #                 # DB to "requesting". we'll check the status every 20 secs for case there was an error and other
    #                 # status was written to the DB, in order to avoid unnecessary waiting.
    #                 opensm_response = wait_for_opensm_status_update(dut_engine, dut_device)
    #
    #             if opensm_response == IssuConsts.OPENSM_RESPONSE_REQUESTING:
    #                 with allure.step("Set OpenSM response to 'No'"):
    #                     set_opensm_response_status(dut_engine, dut_device, IssuConsts.OPENSM_RESPONSE_NO)
    #
    # with allure.step('Verify show ISSU status'):
    #     issu_status = OutputParsingTool.parse_json_str_to_dictionary(
    #         system.image.show()).get_returned_value()[IssuConsts.ISSU_STATUS]
    #     assert issu_status == IssuConsts.IssuStatus.FAILED.value, \
    #         f"ISSU status is {issu_status}, instead of: {IssuConsts.IssuStatus.FAILED.value}"

    with allure.step("Check ISSU openSM no response timeout"):
        with allure.step("Clear OpenSM response value in DB"):
            set_opensm_response_status(dut_engine, dut_device, IssuConsts.OPENSM_RESPONSE_CLEAR)

        with allure.step("Perform install image with ISSU"):
            output = system.image.files.file_name[target_filename].action(
                ActionConsts.INSTALL, flags=IssuConsts.ISSU, send_user_confirmation='y',
                timeout=IssuConsts.ISSU_READ_TIMEOUT).\
                verify_result(should_succeed=False)

        assert IssuConsts.ERROR_OPENSM_REACH_TIMEOUT in output, \
            f'error message: {IssuConsts.ERROR_OPENSM_REACH_TIMEOUT} is missing in output: {output}'

    with allure.step("Start OpenSM"):
        OpenSmTool.start_open_sm(engines).verify_result()

    with allure.step("Perform ISSU with “no reboot” flag"):
        with allure.step("Perform install image with ISSU with 'reboot no' flag"):
            output = system.image.files.file_name[target_filename].action(
                ActionConsts.INSTALL, flags=IssuConsts.ISSU,
                additional_params=IssuConsts.ISSU_NO_REBOOT_PARAMS,
                send_user_confirmation='y',
                timeout=IssuConsts.ISSU_READ_TIMEOUT).\
                verify_result(should_succeed=False)

        assert IssuConsts.ERROR_SYSTEM_MUST_BE_REBOOTED in output, \
            f'error message: {IssuConsts.ERROR_SYSTEM_MUST_BE_REBOOTED} is missing in output: {output}'

    with allure.step("Change configuration without saving and run ISSU"):
        with allure.step("Apply system message pre-login configuration change without saving"):
            system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value='"TESTING_001"',
                               apply=True, dut_engine=dut_engine).verify_result()

        with allure.step("Perform install image with ISSU"):
            output = system.image.files.file_name[target_filename].action(
                ActionConsts.INSTALL, flags=IssuConsts.ISSU, send_user_confirmation='y',
                timeout=IssuConsts.ISSU_READ_TIMEOUT).\
                verify_result(should_succeed=False)

        assert IssuConsts.ERROR_CONFIG_MUST_BE_SAVED in output, \
            f'error message: {IssuConsts.ERROR_CONFIG_MUST_BE_SAVED} is missing in output: {output}'

        with allure.step('Unset system message, apply and save config'):
            system.message.unset(op_param="", apply=True, dut_engine=engines.dut).verify_result()
            TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

    with allure.step("Perform ISSU with an invalid flag"):
        output = system.image.files.file_name[target_filename].action(
            ActionConsts.INSTALL, flags=IssuConsts.ISSU_INVALID_FLAG).verify_result(should_succeed=False)

        assert IssuConsts.ERROR_INVALID_PARAM in output, \
            f'error message: {IssuConsts.ERROR_INVALID_PARAM} is missing in output: {output}'

    with allure.step("Validate system log"):
        system.log.verify_expected_logs(IssuConsts.LOG_MSG_LIST, engine=dut_engine, only_latest_log=True)

    # with allure.step("Validate event table"):
    #     # TODO update...
    #     system.log.verify_expected_logs(IssuConsts.LOG_MSG_LIST, engine=dut_engine)


# @pytest.mark.system
# @pytest.mark.issu
# @pytest.mark.parametrize('test_api', [ApiType.NVUE])
# def test_power_cycle_during_issu_process(topology_obj, engines, devices, issu_version, target_version, test_api):
#     """
#     Validate:
#     - Power cycle during ISSU (system will go up correctly)
#
#     Test flow:
#     1. Install system image with ISSU
#     2. Perform power cycle during ISSU, at randomly stage:
#         - Before upgrade OS: no upgrades when system is up
#         - During upgrade OS: no upgrades when system is up
#         - After upgrade OS: run FW upgrade when system is up
#         - During upgrade FW: complete FW upgrade when system is up
#     """
#
#     dut_engine = engines.dut
#     dut_device = devices.dut
#     player = engines.sonic_mgmt
#     system = System()
#
#     with allure.step("Downgrade system image to issu"):
#         install_system_image_and_start_opensm(engines, dut_device, system, issu_version)
#
#     with allure.step("Prepare system target image for install"):
#         target_filename, recovery_engine, scp_host_creds = prepare_image_for_install(
#             player, dut_engine, dut_device, target_version)
#
#     with allure.step("Running on 2 sessions in parallel:"):
#         with allure.step(f'Create another session'):
#             connection = ConnectionTool.create_ssh_conn(engines.dut.ip, engines.dut.username,
#                                                         engines.dut.password).get_returned_value()
#
#         with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
#             with allure.step("Perform install image with iSSU no-sm flag"):
#                 executor.submit(run_install_system_image_issu, connection, dut_device,
#                                 recovery_engine, target_filename, IssuConsts.ISSU_SKIP_SM, False)
#
#             # install image process will start immediately (openSM request is skipped),
#             # therefor the status should be updated to "in progress" immediately.
#             time.sleep(15)
#
#             # with allure.step(f'Verify ISSU status is: {IssuConsts.IssuStatus.IN_PROGRESS.value}'):
#             #     issu_status = OutputParsingTool.parse_json_str_to_dictionary(
#             #         system.image.show()).get_returned_value()[IssuConsts.ISSU_STATUS]
#             #     assert issu_status == IssuConsts.IssuStatus.IN_PROGRESS.value, \
#             #         f"ISSU status is {issu_status}, instead of: {IssuConsts.IssuStatus.IN_PROGRESS.value}"
#
#             # # verify openSM status in updated to "yes" in all asics
#             # with allure.step("Wait for opensm status update to 'yes' in all asics"):
#             #     wait_for_opensm_status_update(dut_engine, dut_device, IssuConsts.OPENSM_RESPONSE_YES)
#
#             with allure.step('Execute power cycle'):
#                 remote_reboot_dut(topology_obj)
#
#     with (allure.step('Verify image versions')):
#         system_version = OutputParsingTool.parse_json_str_to_dictionary(
#             system.version.show()).get_returned_value()['image']
#         if ImageConsts.BUILD_ID in system_version:
#             system_version = system_version[ImageConsts.BUILD_ID]
#         issu_version_num = issu_version.split('/')[-1].replace('amd64-', '').replace('.bin', '')
#         assert system_version == issu_version_num, (f'system image is: {system_version}, '
#                                                     f'instead of {issu_version_num}')
#     # if res:
#     #     with allure.step('Waiting for switch to bring-up after reload'):
#     #         check_port_status_till_alive(should_be_alive=True, destination_host=engines.dut.ip,
#     #                                      destination_port=engines.dut.ssh_port)
#     #         res = ping_device(engines.dut.ip)
#     #
#     # assert res, f"dut {engines.dut.ip} is unreachable"
#
#
# @pytest.mark.system
# @pytest.mark.issu
# @pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
# def test_stuck_asic_during_issu_process(engines, devices, test_api):
#     """
#     Validate:
#     - Failure during upgrade (stuck ASIC FW-dump-me style)
#     - Show system ISSU (failed)
#     - Validate tech support (Fatal state)
#
#     Test flow:
#     1. Install system image with ISSU
#     2. Perform stuck ASIC during FW upgrade
#     3. Show system ISSU (from another session)
#     4. Validate tech-support after warm reboot (reaching fatal state)
#     5. Validate FW version
#     6. Validate event table
#     """
#     TestToolkit.tested_api = random_api
#     dut_engine = engines.dut
#     dut_device = devices.dut
#     player = engines.sonic_mgmt
#     system = System()
#
#     # TODO: to be removed
#     target_version = "/auto/sw_system_release/nos/nvos/25.02.1930-025/amd64/dev/nvos-amd64-25.02.1930-025.bin"
#
#     with allure.step("Prepare system target image for install"):
#         target_filename, recovery_engine, scp_host_creds = prepare_image_for_install(
#             player, dut_engine, dut_device, target_version)
#
#     ibv_num = str(random.randint(0, dut_device.asic_amount - 1) if hasattr(dut_device, 'asic_amount') else '')
#     # just multi asic systems have asic_amount attribute
#     syncd_ibv = "syncd-ibv0{}".format(ibv_num)
#     sdk_dump_folder = "/var/log/mellanox/sdk-dumps{}/".format('_dev{}'.format(ibv_num) if ibv_num else '')
#
#     with allure.step('Upload sdk fw crush file to switch'):
#         player_engine = engines['sonic_mgmt']
#         player_engine.upload_file_using_scp(dest_username=dut_device.default_username,
#                                             dest_password=dut_device.default_password,
#                                             dest_folder=NvosConst.DESTINATION_FW_SCRIPT_PATH,
#                                             dest_ip=engines.dut.ip,
#                                             local_file_path=NvosConst.FW_DUMP_ME_SCRIPT_PATH)
#
#     with allure.step('Copy sxd api crash fw file to the {} container'.format(syncd_ibv)):
#         engines.dut.run_cmd('sudo docker cp /var/tmp/sxd_api_crash_fw.py {}:/tmp/'.format(syncd_ibv))
#         cmd_output = engines.dut.run_cmd('echo $?')
#         assert '0' in cmd_output, "Docker copy finished with error"
#
#     with allure.step('Delete all sdk dumps before fw crash script'):
#         engines.dut.run_cmd('sudo rm -Rf {}sai-dfw*'.format(sdk_dump_folder))
#
#     with allure.step("Rotate logs"):
#         system.log.rotate_logs()
#
#     try:
#         with allure.step("Running on 2 sessions in parallel:"):
#             with allure.step(f'Create another session'):
#                 connection = ConnectionTool.create_ssh_conn(engines.dut.ip, engines.dut.username,
#                                                             engines.dut.password).get_returned_value()
#
#             with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
#                 with allure.step("Perform install image with iSSU no-sm flag"):
#                     executor.submit(run_install_system_image_issu, connection, dut_device,
#                                     recovery_engine, target_filename, IssuConsts.ISSU_SKIP_SM, False)
#
#                 # install image process will start immediately, even it still requesting openSM response,
#                 # therefor we wait few seconds and status should be updated to "in progress".
#                 time.sleep(5)
#
#                 with allure.step(f'Verify ISSU status is: {IssuConsts.IssuStatus.IN_PROGRESS.value}'):
#                     issu_status = OutputParsingTool.parse_json_str_to_dictionary(
#                         system.image.show()).get_returned_value()[IssuConsts.ISSU_STATUS]
#                     assert issu_status == IssuConsts.IssuStatus.IN_PROGRESS.value, \
#                         f"ISSU status is {issu_status}, instead of: {IssuConsts.IssuStatus.IN_PROGRESS.value}"
#
#                 # verify openSM status in updated to "yes" in all asics
#                 with allure.step("Wait for opensm status update to 'yes' in all asics"):
#                     wait_for_opensm_status_update(dut_engine, dut_device, IssuConsts.OPENSM_RESPONSE_YES)
#
#                 with allure.step('Exec sxd api crash fw from {} docker'.format(syncd_ibv)):
#                     cmd_output = engines.dut.run_cmd(
#                         'docker exec -i {} bash -c "python /tmp/sxd_api_crash_fw.py --device_id 1"'.format(syncd_ibv))
#                     assert "trigger_stack_overflow" in cmd_output, "SXD API CRASH script failed"
#
#                 with allure.step("Run nv show system log command follow to view system logs"):
#                     logging.info("Run nv show system log command follow to view system logs")
#                     show_output = system.log.file.show_log(exit_cmd='q', expected_str=' ')
#
#                 with allure.step('Verify updated SDK message in the logs as expected'):
#                     logging.info('Verify updated SDK message in the logs as expected')
#                     ValidationTool.verify_expected_output(show_output, 'FW test event').verify_result()
#
#                 timeout_in_seconds = 30
#                 with allure.step(f'Validate if sdk_fw_dump created after {timeout_in_seconds} sec timeout'):
#                     time.sleep(timeout_in_seconds)
#                     cmd_output = engines.dut.run_cmd('ls {}'.format(sdk_dump_folder))
#                     assert 'sai-dfw' in cmd_output, "Sdk dump not created"
#                     sdk_dump = cmd_output.split()[0]
#
#                 with allure.step('Validate upload sdkdump to sonic-mgmt'):
#                     logging.info('Validate upload sdkdump to sonic-mgmt')
#                     player_engine = engines['sonic_mgmt']
#                     player_engine.run_cmd(
#                         'sshpass -p {0} scp -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking='
#                         'no {1}@{2}:{3}{4} {5}'.format(engines.dut.password, engines.dut.username, engines.dut.ip,
#                                                        sdk_dump_folder, sdk_dump, NvosConst.MARS_RESULTS_FOLDER))
#                     cmd_output = player_engine.run_cmd('ls {0} | grep {1}'.format(NvosConst.MARS_RESULTS_FOLDER,
#                                                                                   sdk_dump))
#                     assert sdk_dump in cmd_output, 'sdk dump not in results folder'
#
#                     logging.info('Delete dump file in Mars directory')
#                     player_engine.run_cmd('rm -f {0}{1}'.format(NvosConst.MARS_RESULTS_FOLDER, sdk_dump))
#
#         # reach here only when install ISSU action is done
#         with allure.step('Wait until switch is up'):
#             dut_engine.disconnect()  # force engines.dut to reconnect
#             # after upgrade flow switch has new default password
#             dut_engine.password = dut_device.get_default_password_by_version(target_version)
#
#     finally:
#         with allure.step('Reboot system'):
#             logging.info("Reboot system")
#             system.reboot.action_reboot(params='force').verify_result()
#
#
# @pytest.mark.system
# @pytest.mark.issu
# @pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
# def test_link_down_during_issu(engines, devices, test_api, target_version=''):
#     """
#     Validate:
#     - Change link speed on connected host
#     - Link down during ISSU - host HCA went down (rest of the system behaves normally)
#
#     Test flow:
#     1. Install system image with ISSU
#     2. Set link down on Host A during ISSU
#     3. Wait for the ISSU to finish...
#     4. Validate show interface swA1p1 link
#     """
#     # TODO: to be removed
#     target_version = "/auto/sw_system_release/nos/nvos/25.02.1930-025/amd64/dev/nvos-amd64-25.02.1930-025.bin"
#
#     dut_engine = engines.dut
#     engines_ha = engines.ha
#     dut_device = devices.dut
#     player = engines.sonic_mgmt
#     system = System()
#
#     with allure.step(f"Verify port state is: {IpConsts.PORT_STATE_UP}"):
#         port = Port(Configurations.ndr_ports[dut_engine.ip][0])
#         port_state = port.interface.link.state.show()
#         assert port_state == IpConsts.PORT_STATE_UP, f'port state is {port_state} instead of {IpConsts.PORT_STATE_UP}'
#
#     # prepare system image for install
#     target_filename, recovery_engine, scp_host_creds = prepare_image_for_install(player, dut_engine,
#                                                                                  dut_device, target_version)
#
#     with allure.step("Running on 2 sessions in parallel:"):
#         with allure.step(f'Create another session'):
#             connection = ConnectionTool.create_ssh_conn(engines.dut.ip, engines.dut.username,
#                                                         engines.dut.password).get_returned_value()
#
#         with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
#             with allure.step("Perform install image with iSSU no-sm flag"):
#                 executor.submit(run_install_system_image_issu, connection, dut_device,
#                                 recovery_engine, target_filename, IssuConsts.ISSU_SKIP_SM, False)
#
#             # install image process will start immediately, even it still requesting openSM response, therefor we wait
#             # few seconds and status should be updated to "in progress".
#             time.sleep(5)
#
#             with allure.step(f'Verify ISSU status is: {IssuConsts.IssuStatus.IN_PROGRESS.value}'):
#                 issu_status = OutputParsingTool.parse_json_str_to_dictionary(
#                     system.image.show()).get_returned_value()[IssuConsts.ISSU_STATUS]
#                 assert issu_status == IssuConsts.IssuStatus.IN_PROGRESS.value, \
#                     f"ISSU status is {issu_status}, instead of: {IssuConsts.IssuStatus.IN_PROGRESS.value}"
#
#             with allure.step(f'Set host link interface to: {IpConsts.PORT_STATE_DOWN}'):
#                 interface = engines_ha.run_cmd(IbConsts.IB_DEV_2_NET_DEV).split()[-2]
#                 engines_ha.run_cmd(IpConsts.IP_LINK_SET_INTERFACE.format(interface, IpConsts.PORT_STATE_DOWN))
#
#     # reach here only when install ISSU action is done
#     with allure.step('Wait until switch is up'):
#         dut_engine.disconnect()  # force engines.dut to reconnect
#         # after upgrade flow switch has new default password
#         dut_engine.password = dut_device.get_default_password_by_version(target_version)
#
#     with (allure.step(f"Verify port state is: {IpConsts.PORT_STATE_DOWN}")):
#         port = Port(Configurations.ndr_ports[dut_engine.ip][0])
#         port_state = port.interface.link.state.show()
#         assert port_state == IpConsts.PORT_STATE_DOWN, \
#             f'port state is {port_state} instead of {IpConsts.PORT_STATE_DOWN}'


# -------------------------------------------------------


@pytest.mark.system
@pytest.mark.issu
def test_system_issu_downgrade_prevention(engines, devices, downgrade_version, target_version, original_version, random_api):
    """
    Validate that ISSU into an image strictly older than the running image is blocked
    with ERROR_DOWNGRADE_NOT_ALLOWED.

    Precondition: the DUT is already running target_version. In the regression matrix
    this test is scheduled right after test_system_issu_positive_flow_with_traffic,
    which leaves the DUT at target_version after a successful upgrade. The test does
    not install any image on its own.

    Skipped when:
    - the configured downgrade_version is not strictly lower than target_version
      (the downgrade guard cannot be exercised in that case), or
    - the DUT is not actually running target_version (the upstream positive flow
      did not run, did not finish, or the orchestration order was changed).

    Test flow:
    1. Skip-gate: pytest.skip if downgrade_version >= target_version.
    2. Verify the DUT is currently running target_version; pytest.skip otherwise.
    3. Prepare the (older) downgrade image for install.
    4. Attempt ISSU into the downgrade image; expect failure.
    5. Assert ERROR_DOWNGRADE_NOT_ALLOWED in the output.
    """
    if _nvos_version_key(downgrade_version) >= _nvos_version_key(target_version):
        pytest.skip(
            f"downgrade_version ({downgrade_version}) is not lower than "
            f"target_version ({target_version}); cannot exercise downgrade guard"
        )

    dut_engine = engines.dut
    dut_device = devices.dut
    player = engines.sonic_mgmt
    system = System()

    with allure.step("Verify DUT is running target_version (precondition)"):
        target_version_num = target_version.split('/')[-1].replace('amd64-', '').replace('.bin', '')
        if original_version != target_version_num:
            pytest.skip(
                f"DUT is running {original_version}, expected {target_version_num} "
                f"(precondition: test_system_issu_positive_flow_with_traffic should "
                f"run first to leave the DUT at target_version)"
            )

    with allure.step("Prepare downgrade image for install"):
        base_filename, recovery_engine, scp_host_creds = prepare_image_for_install(
            player, dut_engine, dut_device, downgrade_version)

    with allure.step('Clear system log (rotate)'):
        system.log.rotate_logs()

    with allure.step("Attempt ISSU into downgrade image (expect rejection)"):
        output = system.image.files.file_name[base_filename].action(
            ActionConsts.INSTALL, flags=IssuConsts.ISSU, send_user_confirmation='y',
            timeout=IssuConsts.ISSU_READ_TIMEOUT).\
            verify_result(should_succeed=False)

    with allure.step("Verify ERROR_DOWNGRADE_NOT_ALLOWED in output"):
        assert IssuConsts.ERROR_DOWNGRADE_NOT_ALLOWED in output, \
            f'error message: {IssuConsts.ERROR_DOWNGRADE_NOT_ALLOWED} is missing in output: {output}'

    with allure.step("Validate downgrade-not-supported log in syslog"):
        system.log.verify_expected_logs([IssuConsts.LOG_MSG_DOWNGRADE_NOT_SUPPORTED],
                                        engine=dut_engine, only_latest_log=True)


# -------------------------------------------------------


def pre_issu_installation_steps(engines, devices, target_version, scp_host_creds, issu_base_version=None):
    """
    - Change and apply configuration for several resources
    - Run management services
    - Clear counters
    - Clear system log (rotate)
    - Show system ISSU
    - Start Ping mgmt. port 0 and mgmt. port 1
    - Start sending data packets from Host A to Host B
    - Save default ACL + control-plane ACL baselines for post-ISSU verification

    :param engines:
    :param devices:
    :param target_version:
    :param scp_host_creds:
    :param issu_base_version: image currently on the DUT pre-ISSU. Used to pick the config file
        that matches the running image's NVUE schema. Falls back to ``target_version`` only when
        the caller did not supply it (legacy callers).
    :return: ``traffic_start_time``, ``interface_output``, ``ip_list``, ``mtu_info``,
        ``ssd_should_auto_update``, ``json_acl_rules``, ``cp_acl_bindings``, ``acl_persist_mangle_state``.
    """
    system = System()
    interface = Interface(parent_obj=None)
    dut_engine = engines.dut
    engines_ha = engines.ha
    engines_hb = engines.hb
    player = engines.sonic_mgmt
    dut_device = devices.dut

    with (allure.step('Validate health status')):
        health_output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
        assert health_output[HealthConsts.STATUS] == HealthConsts.OK, \
            f"System health status is {health_output[HealthConsts.STATUS]}, instead of: {HealthConsts.OK}"

    config_lookup_version = issu_base_version if issu_base_version else target_version
    with allure.step(f'Get config file matching running image ({config_lookup_version})'):
        config_file_path, config_filename = dut_device.get_test_config_file_by_version(config_lookup_version)

    # In case of manual run add the following line with updated user/password
    # scp_host_creds = '{username}:{password}@fit70'

    with allure.step('Apply and save pre-defined configuration'):
        NvosInstallationSteps.fetch_apply_save_config(config_filename, config_file_path, dut_engine,
                                                      scp_host_creds, system)

    with allure.step('Configure MTU on a random port'):
        mtu_info = InterfaceConfigurationTool.change_mtu_on_random_port(devices)

    # with allure.step('Run management services'):
    #     with allure.step("Enable snmp"):
    #         HostMethods.start_snmp_server(engine=engines.dut, state=NvosConst.ENABLED,
    #                                       readonly_community=IssuConsts.SNMP_READ_ONLY_COMMUNITY,
    #                                       listening_address=NvosConst.ALL)
    #         HostMethods.wait_for_snmp_is_running(system)
    #     with allure.step("Enable ntp"):
    #         system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.ENABLED.value,
    #                        apply=True).verify_result()

    # TODO: start gnmi, rsyslog, and AAA processes.

    with allure.step('Clear ports counters'):
        Port().interface.action_clear_counter_for_all_interfaces(dut_engine).verify_result()

    with allure.step('Clear system log (rotate)'):
        system.log.rotate_logs()

    with allure.step('Verify show ISSU status'):
        issu_status = OutputParsingTool.parse_json_str_to_dictionary(
            system.image.show()).get_returned_value()[IssuConsts.ISSU_STATUS]
        assert issu_status == IssuConsts.IssuStatus.NO_ISSU.value, \
            f"ISSU status is {issu_status}, instead of: {IssuConsts.IssuStatus.NO_ISSU.value}"

    ssd_should_auto_update = False
    with allure.step('Downgrade SSD firmware to test auto-upgrade during ISSU'):
        _, _, latest_version_name = FWComponentsTool.get_fw_component_version_latest(FW_COMPONENT_SSD)
        if latest_version_name is not None:
            ssd_component = Platform().firmware.ssd
            # Get latest version and verify currently on latest
            FWComponentsTool.verify_platform_component_version(ssd_component, latest_version_name)

            # Downgrade SSD firmware to previous version
            _, previous_filename, previous_version_name = FWComponentsTool.get_fw_component_version_previous(FW_COMPONENT_SSD)
            SSDTool.downgrade_ssd_firmware(engines, ssd_component, previous_filename, previous_version_name)
            ssd_should_auto_update = True
        else:
            logger.info('SSD downgrade skipped: non supported SSD model')

    with allure.step('Start pinging system mgmt ports'):
        ip_list = []
        for mgmt_port in devices.dut.mgmt_ports:
            ip_list.append(Port(mgmt_port).get_port_ip_addresses(dut_engine))
        Tools.TrafficGeneratorTool.start_ping_multiple_ips(player, ip_list)

    with allure.step('start send traffic from Host A to Host B'):
        traffic_start_time = Tools.TrafficGeneratorTool.start_traffic_between_2_hosts(
            engines_ha, engines_hb, IssuConsts.TRAFFIC_DURATION, IssuConsts.SERVER_OUTPUT, IssuConsts.CLIENT_OUTPUT)

        # For testing via ibping - mask the previous line and unmask the following
        # Tools.TrafficGeneratorTool.start_ibping_between_2_hosts(engines_ha, engines_hb,
        #                                                         IssuConsts.SERVER_OUTPUT, IssuConsts.CLIENT_OUTPUT)
        # traffic_start_time = time.time()

    with allure.step('Run show interface and save its output'):
        interface_output = Tools.OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
            interface.show()).get_returned_value()

    # TODO: add ipoib test (random)

    with allure.step("Add ACL/control-plane configs for persistence checks (before baseline capture)"):
        acl_persist_mangle_state = system_helpers.add_acl_new_configs_for_persistence_checks(dut_engine=dut_engine)

    with allure.step("Save default ACL rules baseline (before ISSU)"):
        json_acl_rules = system_helpers.extract_acl_rules(dut_engine=dut_engine)
        logger.info("ACL baseline captured for %d default ACL object(s)", len(json_acl_rules))

    with allure.step("Save control-plane ACL bindings baseline (nv show system control-plane acl)"):
        cp_acl_bindings = system_helpers.extract_control_plane_acl_bindings(
            dut_engine=dut_engine, mangle_state=acl_persist_mangle_state)
        logger.info("Control-plane ACL bindings captured: %s", cp_acl_bindings)

    return (traffic_start_time, interface_output, ip_list, mtu_info, ssd_should_auto_update,
            json_acl_rules, cp_acl_bindings, acl_persist_mangle_state)


def post_issu_installation_steps(engines, devices, target_version, fw_expected,
                                 interface_dict, traffic_start_time, ip_list, test_name='',
                                 mtu_info=None, ssd_should_auto_update=False,
                                 json_acl_rules=None, cp_acl_bindings=None,
                                 acl_persist_mangle_state=None):
    """
    - Stop Ping mgmt. port 0 and mgmt. port 1 and analyze both logs
    - Stop sending data packets from Host A to Host B and analyze log
    - Show system ISSU
    - Validate versions
    - Check port 1 and port 2 counters
    - Validate configuration
    - Optionally verify default ACL JSON and control-plane / loopback ACL bindings (when baselines passed)
    - Validate management services
    - Validate system log
    - Validate event table

    :param engines:
    :param devices:
    :param target_version:
    :param fw_expected:
    :param interface_dict:
    :param traffic_start_time:
    :param ip_list:
    :param json_acl_rules: baseline from ``extract_acl_rules`` before ISSU (optional)
    :param cp_acl_bindings: baseline from ``extract_control_plane_acl_bindings`` before ISSU (optional)
    :param acl_persist_mangle_state: state from ``add_acl_new_configs_for_persistence_checks`` (optional)
    :return:
    """
    system = System()
    interface = Interface(parent_obj=None)
    dut_engine = engines.dut
    engines_ha = engines.ha
    engines_hb = engines.hb
    player = engines.sonic_mgmt
    dut_device = devices.dut

    with allure.step('Verify show ISSU post install status'):
        try:
            with allure.independent_step('Stop pinging system mgmt ports and verify results'):
                ping_outputs = Tools.TrafficGeneratorTool.stop_ping_multiple_ips(player, ip_list)
                for ping_output in ping_outputs:
                    ping_output_split = ping_output.split('packets')
                    packets_lost = int(ping_output_split[0].split()[-1]) - int(ping_output_split[1].split()[-1])

                    OperationTime.save_manual_operation_duration_to_db('ISSU CPU downtime', packets_lost, test_name)

                    assert packets_lost < devices.dut.expected_operation_durations['ISSU CPU max downtime'], (f'Too many packets were lost: {packets_lost}, '
                                                                                                              f'means cpu was down for ~{packets_lost} seconds')

            with allure.independent_step('Verify traffic results from Host A to Host B'):
                num_of_iterations = Tools.TrafficGeneratorTool.stop_traffic_between_2_hosts(
                    engines_ha, engines_hb, traffic_start_time, IssuConsts.TRAFFIC_TIMEOUT,
                    IssuConsts.SERVER_OUTPUT, IssuConsts.CLIENT_OUTPUT)

                # For testing via ibping - mask the previous line and unmask the following
                # num_of_packets = Tools.TrafficGeneratorTool.stop_ibping_between_2_hosts(engines_ha, engines_hb,
                #                                                                         IssuConsts.SERVER_OUTPUT,
                #                                                                         IssuConsts.CLIENT_OUTPUT)

            with allure.independent_step('Verify show ISSU status'):
                issu_status = OutputParsingTool.parse_json_str_to_dictionary(
                    system.image.show()).get_returned_value()[IssuConsts.ISSU_STATUS]
                assert issu_status == IssuConsts.IssuStatus.NO_ISSU.value, \
                    f"ISSU status is {issu_status}, instead of: {IssuConsts.IssuStatus.NO_ISSU.value}"

            with (allure.independent_step('Verify image versions')):
                system_version = OutputParsingTool.parse_json_str_to_dictionary(
                    system.version.show()).get_returned_value()['image']
                if ImageConsts.BUILD_ID in system_version:
                    system_version = system_version[ImageConsts.BUILD_ID]
                target_version_num = target_version.split('/')[-1].replace('amd64-', '').replace('.bin', '')
                assert system_version == target_version_num, (f'system image is: {system_version}, '
                                                              f'instead of {target_version_num}')

            if fw_expected:
                with (allure.step('Verify fw versions')):
                    fw_version = OutputParsingTool.parse_json_str_to_dictionary(
                        Platform().firmware.show(dut_engine=dut_engine)).get_returned_value()['ASIC']['actual-firmware']
                    assert fw_version == fw_expected, f'FW version is: {fw_version}, instead of {fw_expected}'

            _, _, latest_version_name = FWComponentsTool.get_fw_component_version_latest(FW_COMPONENT_SSD)
            if latest_version_name is not None:
                with allure.independent_step('Verify SSD was auto-upgraded to latest during ISSU'):
                    FWComponentsTool.verify_platform_component_version(Platform().firmware.ssd, latest_version_name)
            else:
                logger.info('SSD auto-upgrade verification skipped: non supported SSD model')

            # with allure.step('Validate ports counters'):
            #     for port in Configurations.ndr_ports[dut_engine.ip]:
            #         counters = OutputParsingTool.parse_json_str_to_dictionary(
            #             Port(port).interface.link.stats.show(dut_engine=dut_engine)).get_returned_value()
            #         assert counters[IbInterfaceConsts.LINK_STATS_IN_PKTS] > num_of_packets, \
            #             f"counters in packets is: {counters[IbInterfaceConsts.LINK_STATS_IN_PKTS]}, \
            #             while number of packets sent is: {num_of_packets}"
            #         assert counters[IbInterfaceConsts.LINK_STATS_OUT_PKTS] > num_of_packets, \
            #             f"counters in packets is: {counters[IbInterfaceConsts.LINK_STATS_OUT_PKTS]}, \
            #             while number of packets sent is: {num_of_packets}"

            with allure.independent_step('Get config file and path for target version'):
                config_file_path, config_filename = dut_device.get_test_config_file_by_version(target_version)

            with allure.independent_step('Verify configuration after upgrade'):
                NvosInstallationSteps.verify_config_after_upgrade(config_file_path, dut_engine)

            with allure.independent_step('Verify interfaces status'):
                interface_output = Tools.OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
                    interface.show()).get_returned_value()
                ValidationTool.compare_dictionaries(interface_output, interface_dict).verify_result()

            with allure.independent_step('Verify MTU preserved after ISSU and cleanup'):
                InterfaceConfigurationTool.verify_and_cleanup_mtu(mtu_info)

            if json_acl_rules is not None:
                with allure.independent_step('Verify default ACL JSON preserved after ISSU (nv show acl)'):
                    system_helpers.verify_acl_rules_preserved(
                        json_acl_rules, mangle_state=acl_persist_mangle_state, dut_engine=dut_engine)
            if cp_acl_bindings is not None:
                with allure.independent_step(
                    'Verify control-plane ACL bindings + loopback defaults on interface lo after ISSU'
                ):
                    system_helpers.verify_control_plane_acl_bindings(cp_acl_bindings, dut_engine=dut_engine)

            # with allure.step('Validate management services'):
            #     with allure.step("Verify ntp state"):
            #         ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            #         assert ntp_show[NtpConsts.STATE] == NtpConsts.State.ENABLED.value, \
            #             f"Ntp state should be {NtpConsts.State.ENABLED.value}"
            #
            #     with allure.step('Verify snmp status'):
            #         system_snmp_output = OutputParsingTool.parse_json_str_to_dictionary(system.snmp_server.show())\
            #             .get_returned_value()
            #         ValidationTool.validate_fields_values_in_output([SystemConsts.SNMP_STATE],
            #                                                         [SystemConsts.SNMP_ENABLED_STATE],
            #                                                         system_snmp_output).verify_result()
                # TODO: verify gnmi, rsyslog, and AAA processes.

            time.sleep(10)

            with allure.independent_step('Validate health status'):
                health_output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
                assert health_output[HealthConsts.STATUS] == HealthConsts.OK, \
                    f"System health status is {health_output[HealthConsts.STATUS]}, instead of: {HealthConsts.OK}"

            # with allure.step('Validate system log'):
            # TODO: complete (check with Elias)

            # with allure.step('Validate event table'):
            # TODO: complete (check with Elias)

        finally:
            # with allure.step('Clear tested configuration for the tests'):
            #     clear_conf(dut_engine)

            with allure.independent_step('Clear ACL persistence mangle state (if any)'):
                system_helpers.clear_acl_configs(acl_persist_mangle_state, dut_engine=dut_engine)

            with allure.independent_step('Clear fetched files for the tests'):
                # system = System()
                dut_engine.disconnect()  # force engines.dut to reconnect

                # with allure.step('Delete fetched image file'):
                #     system.image.files.delete_all_existing_files(engine=dut_engine)
                # with allure.step('Delete config files'):
                #     system.config.files.delete_all_existing_files(engine=dut_engine)
                # with allure.step('Uninstall older version'):
                #     system.image.action_uninstall(params="force", engine=dut_engine, verify_res=False)


def prepare_image_for_install(player, dut_engine, dut_device, image_version):
    system = System()

    with allure.step("Uninstall system image on the other partition"):
        system.image.action_uninstall(params="force", engine=dut_engine, verify_res=False)

    with allure.step("Prepare system image for install"):
        scp_host_creds = f'{player.username}:{player.password}@{IpTool.format_ip_for_uri(player)}'
        if image_version.startswith('http'):
            image_version = f'/auto/{image_version.split("/auto/")[1]}'
        image_filename = image_version.split('/')[-1]

        with allure.step(f"Fetch system image: {image_version}"):
            image_scp_url = f'scp://{scp_host_creds}{image_version}'
            system.image.action_fetch(image_scp_url, base_url='', engine=dut_engine)

        with allure.step('Get recovery engine, use new default password for recovery after upgrade'):
            recovery_engine = LinuxSshEngine(dut_engine.ip, dut_engine.username,
                                             dut_device.get_default_password_by_version(image_version))

    return image_filename, recovery_engine, scp_host_creds


def install_system_image_and_start_opensm(engines, dut_device, system, image_version, start_sm=True):
    player = engines.sonic_mgmt
    dut_engine = engines.dut

    with (allure.step('Verify image version, and recover to target version if needed')):
        expected_version = image_version.split('/')[-1].replace('amd64-', '').replace('.bin', '')
        system_version = OutputParsingTool.parse_json_str_to_dictionary(
            system.version.show()).get_returned_value()['image']
        if ImageConsts.BUILD_ID in system_version:
            system_version = system_version[ImageConsts.BUILD_ID]
        if system_version == expected_version:
            logger.info(f'image version {system_version} is already installed')
        else:
            with allure.step("Prepare system issu image for install"):
                issu_filename, recovery_engine, scp_host_creds = prepare_image_for_install(
                    player, dut_engine, dut_device, image_version)

            with allure.step(f"Install (without ISSU) nvos image: {image_version}"):
                system.image.files.file_name[issu_filename].action(
                    ActionConsts.INSTALL, send_user_confirmation='y', reboot_params=True).\
                    verify_result(should_succeed=True)

    with allure.step("Configure ports to legacy (ndr)"):
        RegressionConfigurations.configure_ports_to_legacy(engine=engines.dut, apply=True, throw_exception=False)

    with allure.step("Save configuration"):
        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

    if start_sm:
        with allure.step("Verify opensm is running (start opensm if not"):
            OpenSmTool.start_open_sm(engines, multiplanar=dut_device.multi_planar).verify_result()


def remote_reboot_dut(topology_obj):
    with allure.step("Remote reboot DUT"):
        cmd = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific']['remote_reboot']
        cmd.replace('/auto', '/.autodirect')
        logging.info(f"Running cmd: {cmd}")
        p = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            p.communicate(timeout=60)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            logger.debug('Process is not responding. Sending SIGKILL.')
            p.kill()
            std_out, std_err = p.communicate()
            rc = p.returncode
            std_out = str(std_out.decode('utf-8') or '')
            std_err = str(std_err.decode('utf-8') or '')
            logging.info(f"std_out = {std_out}, std_err = {std_err}")
        return rc == 0


@retry(AssertionError, tries=12, delay=20)
def wait_for_opensm_status_update(dut_engine, dut_device, requested_status=''):
    opensm_response = IssuConsts.OPENSM_RESPONSE_CLEAR
    opensm_responses = []
    for asic_num in range(dut_device.asic_amount):
        opensm_response = DatabaseTool.sonic_db_cli_hget(
            dut_engine, f'asic{asic_num}', db_name=DatabaseConst.STATE_DB_NAME,
            db_config=IssuConsts.DB_REQUEST_ISSU, param=IssuConsts.DB_STATUS)
        opensm_responses.append(opensm_response)

    if requested_status:  # check if all asics statuses were updated to the requested status
        assert (len(set(opensm_responses)) == 1 and opensm_response == requested_status), \
            f'OpenSm request status "{requested_status}" was not updated for all asics: {opensm_responses}'
    else:  # check if all asics statuses were updated to any status uniformly (not empty)
        assert (len(set(opensm_responses)) == 1 and opensm_response != IssuConsts.OPENSM_RESPONSE_CLEAR), \
            f'OpenSm request status was not updated or equal for all asics: {opensm_responses}'

    return opensm_response


@retry(AssertionError, tries=12, delay=20)
def wait_for_image_status_update(system, status):
    with allure.step(f'Verify ISSU status is: {status}'):
        issu_status = OutputParsingTool.parse_json_str_to_dictionary(
            system.image.show()).get_returned_value()[IssuConsts.ISSU_STATUS]
        assert issu_status == status, f"ISSU status is {issu_status}, instead of: {status}"


def set_opensm_response_status(dut_engine, dut_device, value):
    for asic_num in range(dut_device.asic_amount):
        DatabaseTool.sonic_db_cli_hset(
            dut_engine, f'asic{asic_num}', db_name=DatabaseConst.STATE_DB_NAME,
            db_config=IssuConsts.DB_REQUEST_ISSU, param=IssuConsts.DB_STATUS, value=value)


def run_install_system_image_issu(dut_engine, dut_device, recovery_engine, image_filename, param_value, should_succeed):
    system = System()

    with allure.step("Perform Install image with ISSU"):
        # output = system.image.files.file_name[image_filename].action_file_install_with_reboot(
        #     force=True, engine=dut_engine, device=dut_device, recovery_engine=recovery_engine,
        #     param_value=param_value, should_succeed=should_succeed, press_y=True).verify_result(
        #     should_succeed=should_succeed)
        output = system.image.files.file_name[image_filename].action(
            ActionConsts.INSTALL, flags=param_value, send_user_confirmation='y', engine=dut_engine, device=dut_device,
            reboot_params=True, timeout=IssuConsts.ISSU_READ_TIMEOUT).verify_result(should_succeed=should_succeed)

    return output


def reduce_issu_timeout(engine, timeout_in_sec):
    with allure.step(f"Reduce ISSU timeout to {timeout_in_sec} seconds"):
        engine.run_cmd(f"sudo sed -i 's/^SM_APPROVAL_TIMEOUT=[0-9]\\+$/SM_APPROVAL_TIMEOUT={timeout_in_sec}/g' "
                       "/usr/local/bin/warm-reboot")
        engine.run_cmd(f"sudo sed -i 's/^SM_APPROVAL_TIMEOUT=[0-9]\\+$/SM_APPROVAL_TIMEOUT={timeout_in_sec}/g' "
                       "/usr/local/bin/fast-reboot")
