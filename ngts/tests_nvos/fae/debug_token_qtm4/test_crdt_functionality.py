"""
CRDT (Debug Image Token) Functionality Test

Test Plan Section 6.2: CRDT Complete Flow - IPN Systems
"""
import logging
import pytest
from retry.api import retry_call

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import PlatformConsts, HealthConsts, RebootConsts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.system.reboot_telemetry_helpers import (
    RebootReasonCategory,
    assert_nvue_gnmi_counters_match,
    gnmi_client_for_dut,
    take_reboot_telemetry_snapshot,
    verify_reboot_telemetry_after_reboot,
)

from ngts.tests_nvos.fae.debug_token_qtm4.helpers import (
    CRDTTokenManager,
    DebugTokenFileHelper,
    TokenVerifier,
)
from ngts.tests_nvos.fae.debug_token_qtm4.token_signing import CRDTTokenSigner
from ngts.tests_nvos.platform.firmware_telemetry_helpers import (
    assert_gnmi_firmware_version_matches_nvue,
)

logger = logging.getLogger(__name__)


def _assert_health_ok(nv_command) -> None:
    """Assert system health status is OK."""
    nv_command.system.validate_health_status(HealthConsts.OK)


@pytest.mark.timeout(30 * MINUTE)
@pytest.mark.fae
@pytest.mark.debug_token
@pytest.mark.crdt
@pytest.mark.dut_reboot
@pytest.mark.functionality
def test_crdt_complete_flow(engines, devices, nv_command, test_name, random_api,
                            topology_obj, serial_log_analyzers, skip_if_prod_asics,
                            ensure_debug_firmware):
    """
    Test Plan Section 6.2: CRDT Complete Flow with Firmware

    Comprehensive CRDT token lifecycle with firmware operations:
    - Verify initial state (token disabled) and save original firmware version
    - Fetch debug BIN firmware for token generation
    - Generate first token info from BIN file
    - Sign first token on switch
    - Install first CRDT token
    - Verify token status enabled for all ASICs
    - Uninstall token and verify token is disabled
    - Try to reinstall same token - expect failure
    - Generate and sign second token
    - Install second CRDT token
    - Verify token status enabled for all ASICs
    - Fetch and install MFA firmware
    - Verify token is still applied and firmware version changed
    - Try to uninstall token while debug FW in use - expect failure
    - Reboot with default firmware
    - Verify token persistence and original firmware version restored
    Cleanup: Factory reset to ensure clean state

    Test Scope: Token signing, uninstall flow, firmware version verification

    Note: Uses ensure_debug_firmware fixture to generate debug firmware if needed.
    """
    serial_analyzer, = serial_log_analyzers.values()
    manager = CRDTTokenManager()
    system = System()
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)

    # Get firmware info directly from fixture (consistent bin/mfa versions)
    fw_info = ensure_debug_firmware
    debug_fw_bin = fw_info['bin_filename']
    expected_debug_fw_version = fw_info['version_name']

    logger.info(f"Using debug firmware: {debug_fw_bin} (version: {expected_debug_fw_version})")
    try:
        with allure.step('Verify initial state and save original firmware version'):
            original_firmware_version = DebugTokenFileHelper.get_asic_firmware_version(nv_command)
            logger.info(f"Original firmware version: {original_firmware_version}")

            token_status = manager.get_token_status()
            assert not token_status.is_enabled, "Token should be disabled initially"
            retry_call(_assert_health_ok, [nv_command], exceptions=AssertionError, tries=6, delay=5)

        with allure.step('Fetch debug BIN firmware for token generation'):
            manager.fetch_debug_fw(debug_fw_filename=debug_fw_bin, bin_path=fw_info['bin_path']).verify_result()

        with allure.step('Generate first CRDT token info from BIN file'):
            first_token_info_file = "crdt_first_token.xml"
            manager.generate_token_info(first_token_info_file, test_name, fw_signed_filename=debug_fw_bin)
            manager.verify_files_output(expected_files=[first_token_info_file])

        with allure.step('Sign first token on switch'):
            signer = CRDTTokenSigner(engines)
            first_signed_token_path, first_signed_token_name = signer.sign_on_switch(first_token_info_file)

        with allure.step('Install first signed CRDT token'):
            manager.install_token(first_signed_token_name).verify_result()

        with allure.step('Verify first token status ENABLED for all ASICs'):
            TokenVerifier.verify_token_enabled(manager, expected_enabled=True)

        with allure.step('Uninstall token and verify token is disabled'):
            manager.uninstall_token().verify_result()
            TokenVerifier.verify_token_enabled(manager, expected_enabled=False)

        with allure.step('Try to reinstall same token - expect failure'):
            # After uninstall, the same token should not be applicable
            manager.install_token(first_signed_token_name).verify_result(False)
            TokenVerifier.verify_token_enabled(manager, expected_enabled=False)

        with allure.step('Generate and sign second token'):
            second_token_info_file = "crdt_second_token.xml"
            manager.generate_token_info(second_token_info_file, test_name, fw_signed_filename=debug_fw_bin)
            second_signed_token_path, second_signed_token_name = signer.sign_on_switch(second_token_info_file)

        with allure.step('Install second signed CRDT token'):
            manager.install_token(second_signed_token_name).verify_result()

        with allure.step('Verify second token status ENABLED for all ASICs'):
            TokenVerifier.verify_token_enabled(manager, expected_enabled=True)

        with allure.step('Fetch and install MFA firmware'):
            manager.fetch_and_install_mfa_fw(
                nv_command, engines,
                mfa_filename=fw_info['mfa_filename'],
                mfa_path=fw_info['mfa_path']
            ).verify_result()

        with allure.step('Verify token and firmware version after MFA install'):
            TokenVerifier.verify_token_enabled(manager, expected_enabled=True)

            # Verify firmware version changed to debug version
            if expected_debug_fw_version:
                DebugTokenFileHelper.verify_firmware_version(nv_command, expected_debug_fw_version, 'after MFA install')
                for asic_name in getattr(devices.dut, "asic_numbers", ["ASIC1"]):
                    assert_gnmi_firmware_version_matches_nvue(
                        gnmi_client, asic_name, expected_debug_fw_version
                    )

            retry_call(_assert_health_ok, [nv_command], exceptions=AssertionError, tries=6, delay=5)

        with allure.step('Try to uninstall token while debug FW in use - expect failure'):
            # Token cannot be uninstalled while debug firmware is actively running
            manager.uninstall_token().verify_result(False)
            # Token should still be enabled after failed uninstall attempt
            TokenVerifier.verify_token_enabled(manager, expected_enabled=True)

        with allure.step('Reboot with default firmware'):
            nv_command.platform.firmware.asic.set(
                PlatformConsts.FW_SOURCE,
                PlatformConsts.FW_SOURCE_DEFAULT,
                apply=True
            )
            NvueGeneralCli.save_config(engines.dut)

            res_obj, duration = OperationTime.save_duration(
                'reboot with default FW', '', test_name,
                nv_command.system.reboot.action_reboot,
                system_is_ready_timeout=PlatformConsts.TIMEOUT_AFTER_FW_INSTALL
            )
            retry_call(_assert_health_ok, [nv_command], exceptions=AssertionError, tries=6, delay=5)

        with allure.step('Verify token persistence and original firmware version restored'):
            TokenVerifier.verify_token_enabled(manager, expected_enabled=True)

            # Verify firmware version is back to original
            DebugTokenFileHelper.verify_firmware_version(nv_command, original_firmware_version, 'after reboot with default FW')
            for asic_name in getattr(devices.dut, "asic_numbers", ["ASIC1"]):
                assert_gnmi_firmware_version_matches_nvue(
                    gnmi_client, asic_name, original_firmware_version
                )

        with allure.step('Verify operation time for reboot with default FW'):
            OperationTime.verify_operation_time(duration, 'reboot with default FW').verify_result()

    finally:
        # Cleanup: Factory reset to ensure clean state
        with allure.step('Cleanup: Factory reset'):
            gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)
            expected_reason, expected_user = devices.dut.reboot_reason_dict[RebootConsts.FACTORY_RESET]

            with allure.step("NVUE and gNMI reboot counters must match before factory reset"):
                telemetry_before_reset = take_reboot_telemetry_snapshot(system, gnmi_client)
                assert_nvue_gnmi_counters_match(telemetry_before_reset)

            with serial_analyzer.stage('Reset-factory'):
                result_obj = system.factory_default.action_reset(
                    operation=devices.dut.reset_factory,
                    param="",
                    topology_obj=topology_obj,
                    test_name=test_name
                )
                result_obj.verify_result()

            TokenVerifier.verify_token_enabled(manager, expected_enabled=False)

            # Verify firmware version is back to original after factory reset
            DebugTokenFileHelper.verify_firmware_version(nv_command, original_firmware_version, 'after factory reset')

            ValidationTool.validate_reboot_reason_and_user(system, expected_reason, expected_user)

            with allure.step("Verify NVUE and gNMI reboot telemetry after factory reset"):
                verify_reboot_telemetry_after_reboot(
                    snapshot_before=telemetry_before_reset,
                    system=system,
                    gnmi_client=gnmi_client,
                    expected_category=RebootReasonCategory.USER_INITIATED,
                    expected_details=expected_reason,
                    expected_user=expected_user,
                )
