from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import NvosConst, PlatformConsts, HealthConsts, ImageConsts, RebootConsts
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.Devices.IbDevice import NvLinkSwitch
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.nvos_tools.infra.NvosGitTool import NvosGitTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.helpers import redmine_helpers
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.system.reboot_telemetry_helpers import (
    RebootReasonCategory,
    assert_nvue_gnmi_counters_match,
    gnmi_client_for_dut,
    take_reboot_telemetry_snapshot,
    verify_reboot_telemetry_after_reboot,
)
from ngts.tests_nvos.platform.firmware_telemetry_helpers import (
    assert_gnmi_firmware_version_matches_nvue,
    canonicalize_fw_version,
    expand_nvue_key_to_gnmi_components,
)

logger = logging.getLogger()

# Map device asic_type (e.g., 'Quantum3') to FW pattern name (e.g., 'QTM3')
ASIC_TYPE_TO_FW_NAME = {
    'Quantum2': 'QTM2',
    'Quantum3': 'QTM3',
    'Quantum4': 'QTM4',
    'QTM2': 'QTM2',
    'QTM3': 'QTM3',
    'QTM4': 'QTM4',
}


def get_previous_fw_file(target_version: str, chip_type: str, component_name: str = 'asic') -> tuple[str, str, str]:
    """
    Get previous FW file path using NvosGitTool with BmcTool fallback.

    Args:
        target_version: Target image version path
        chip_type: Chip type (e.g., 'Quantum3', 'QTM3')
        component_name: Component name for BmcTool fallback (default: 'asic')

    Returns:
        Tuple of (fw_file_path, filename, version_name)
    """
    fw_file, filename, version_name = None, None, None

    # Map device asic_type to FW pattern name
    fw_chip_type = ASIC_TYPE_TO_FW_NAME.get(chip_type, chip_type)
    logger.info(f"Chip type: {chip_type} -> FW chip type: {fw_chip_type}")

    with allure.step("Get previous FW from image versions (NvosGitTool approach)"):
        try:
            git_tool = NvosGitTool()
            version, image_type = git_tool.parse_version_from_path(target_version)
            logger.info(f"Parsed version: {version}, image_type: {image_type}")

            prev_version, fw_version, _ = git_tool.find_previous_fw_version(
                target_version, asic_type=fw_chip_type
            )
            logger.info(f"Previous version: {prev_version}, FW version: {fw_version}")

            fw_file = NvosGitTool.build_fw_file_path(fw_chip_type, fw_version, image_type)
            filename = Path(fw_file).name
            version_name = fw_version
            logger.info(f"Built FW path: {fw_file}")
        except Exception as e:
            logger.warning(f"NvosGitTool approach failed: {e}")

    # Fallback to JSON-based approach if the new method fails
    if not fw_file:
        with allure.step("Fallback: Get FW from BmcTool JSON-based approach"):
            logger.warning("Could not find previous FW from image versions, falling back to JSON-based approach")
            fw_file, filename, version_name = BmcTool.get_fw_component_version_latest(component_name)
            logger.info(f"BmcTool fallback - FW file: {fw_file}, version: {version_name}")

    logger.info(f"Using FW file: {fw_file}, filename: {filename}, version: {version_name}")
    return fw_file, filename, version_name


# XXX: remove after the bug is closed: https://redmine.mellanox.com/issues/4221742
@pytest.fixture(scope='module', autouse=True)
def _update_install_threshold(devices):
    if isinstance(devices.dut, NvLinkSwitch) and redmine_helpers.is_bug_active(4221742):
        devices.dut.expected_operation_durations['reboot with default FW installation'] *= 2
        devices.dut.expected_operation_durations['reboot with new user FW'] *= 2


@pytest.mark.checklist
@pytest.mark.platform
@pytest.mark.timeout(30 * MINUTE, func_only=True)
def test_install_platform_firmware(engines, devices, test_name, topology_obj, nv_command, clear_asic_files, verify_no_kernel_errors, show_platform_initial_state, target_version):
    """
    Install platform firmware test

    Test flow:
    1. Install platform firmware and reboot
    2. Verify the firmware is updated successfully to new version
    3. Change fw-source to default and reboot
    4. Verify the firmware is updated successfully to embedded version
    """
    test_image_name = "test_fw_asic.mfa"
    fw_has_changed = False

    # Get chip type from device (e.g., 'QTM3', 'QTM4')
    chip_type = getattr(devices.dut, 'asic_type', 'QTM3')
    logger.info(f"Detected chip type: {chip_type}")

    # Get previous FW file using helper function
    fw_file, filename, version_name = get_previous_fw_file(target_version, chip_type)
    expected_reason, expected_user = devices.dut.reboot_reason_dict[RebootConsts.INSTALL_FW]
    gnmi_client = gnmi_client_for_dut(engines.dut, devices.dut)

    with allure.step("Check actual firmware value"):
        asic_dictionary = get_asic_dict(nv_command.platform)
        first_asic_name = next(iter(asic_dictionary))
        actual_firmware = asic_dictionary[first_asic_name]["actual-firmware"]
        logging.info("Original actual firmware - " + actual_firmware)
        nv_command.system.validate_health_status(HealthConsts.OK)

    try:
        with allure.step("Install system firmware file - " + fw_file):
            with allure.step("fetch firmware file to switch"):
                # Use fit70 SCP path for mswg release FW files, sonic_mgmt for other paths
                if NvosGitTool.FW_RELEASE_PATH in fw_file:
                    scp_path = ImageConsts.SCP_PATH
                    logger.info(f"Using fit70 SCP path for mswg FW: {scp_path}")
                else:
                    player_engine = engines['sonic_mgmt']
                    scp_path = f'scp://{player_engine.username}:{player_engine.password}@{player_engine.ip}'
                    logger.info(f"Using sonic_mgmt SCP path: {scp_path}")
                nv_command.platform.firmware.asic.action_fetch(fw_file, base_url=scp_path).verify_result()
                fetched_image_file = nv_command.platform.firmware.asic.files.file_name[filename]
                fetched_image_file.action_rename(test_image_name, rewrite_file_name=False).verify_result()

            with allure.step("Install firmware and verify"):
                nv_command.platform.firmware.asic.set(PlatformConsts.FW_SOURCE, PlatformConsts.FW_SOURCE_CUSTOM, apply=True)
                NvueGeneralCli.save_config(engines.dut)
                with allure.step("NVUE and gNMI reboot counters must match before FW install reboot"):
                    telemetry_before_fw_install = take_reboot_telemetry_snapshot(nv_command.system, gnmi_client)
                    assert_nvue_gnmi_counters_match(telemetry_before_fw_install)
                _, install_time = install_new_image_fw(nv_command.platform, test_name, test_image_name, devices)
            with allure.step('Verify the firmware installed successfully'):
                verify_firmware_with_platform_cmd(nv_command.platform, version_name)
                for gnmi_component in expand_nvue_key_to_gnmi_components(PlatformConsts.FW_ASIC, devices.dut):
                    assert_gnmi_firmware_version_matches_nvue(
                        gnmi_client, gnmi_component, version_name
                    )
                if redmine_helpers.is_bug_active(4844323):
                    ValidationTool.retry_until_valid(
                        lambda: nv_command.system.validate_health_status(HealthConsts.OK),
                        tries=5, delay=10,
                        description="Waiting for health status OK (workaround for #4844323)")
                else:
                    nv_command.system.validate_health_status(HealthConsts.OK)
                fw_has_changed = True
                ValidationTool.validate_reboot_reason_and_user(nv_command.system, expected_reason, expected_user)
                with allure.step("Verify NVUE and gNMI reboot telemetry after FW install reboot"):
                    verify_reboot_telemetry_after_reboot(
                        snapshot_before=telemetry_before_fw_install,
                        system=nv_command.system,
                        gnmi_client=gnmi_client,
                        expected_category=RebootReasonCategory.USER_INITIATED,
                        expected_details=expected_reason,
                        expected_user=expected_user,
                    )
                install_time.verify_result()
    finally:
        with allure.step("cleanup steps"):
            with allure.step("Install original system firmware file"):
                nv_command.platform.firmware.asic.set(PlatformConsts.FW_SOURCE, PlatformConsts.FW_SOURCE_DEFAULT, apply=True)
                NvueGeneralCli.save_config(engines.dut)

            with allure.step("NVUE and gNMI reboot counters must match before default FW reboot"):
                telemetry_before_default_fw = take_reboot_telemetry_snapshot(nv_command.system, gnmi_client)
                assert_nvue_gnmi_counters_match(telemetry_before_default_fw)

            install_default_image_fw(nv_command.system, test_name, fw_has_changed, devices)

        with allure.step('Verify the firmware installed successfully'):
            verify_firmware_with_platform_cmd(nv_command.platform, actual_firmware)
            for gnmi_component in expand_nvue_key_to_gnmi_components(PlatformConsts.FW_ASIC, devices.dut):
                assert_gnmi_firmware_version_matches_nvue(
                    gnmi_client, gnmi_component, actual_firmware
                )
            validate_all_asics_have_same_info(nv_command.platform)
            if redmine_helpers.is_bug_active(4844323):
                ValidationTool.retry_until_valid(
                    lambda: nv_command.system.validate_health_status(HealthConsts.OK),
                    tries=5, delay=10,
                    description="Waiting for health status OK (workaround for #4844323)")
            else:
                nv_command.system.validate_health_status(HealthConsts.OK)
            ValidationTool.validate_reboot_reason_and_user(nv_command.system, expected_reason, expected_user)
            with allure.step("Verify NVUE and gNMI reboot telemetry after default FW reboot"):
                verify_reboot_telemetry_after_reboot(
                    snapshot_before=telemetry_before_default_fw,
                    system=nv_command.system,
                    gnmi_client=gnmi_client,
                    expected_category=RebootReasonCategory.USER_INITIATED,
                    expected_details=expected_reason,
                    expected_user=expected_user,
                )


def get_version_and_file_name(device) -> tuple[str, str]:
    return getattr(device.asic_version, 'version'), getattr(device.asic_version, 'filename')


def get_asic_dict(platform):
    show_output = OutputParsingTool.parse_json_str_to_dictionary(platform.firmware.show()).get_returned_value()
    asic_dictionary = {k: v for k, v in show_output.items() if PlatformConsts.FW_ASIC in k and 'EROT' not in k}
    assert asic_dictionary, "asic list is empty"
    return asic_dictionary


def install_new_image_fw(platform, test_name, fw_file_name, devices):
    with allure.step('new fw image installation'):
        res_obj, duration = OperationTime.save_duration('reboot with new user FW', '', test_name,
                                                        platform.firmware.asic.files.file_name[fw_file_name].action_file_install_with_reboot)
    # Timing verdict is returned, not asserted here, so a slow install cannot skip the post-reboot checks.
    return res_obj, OperationTime.verify_operation_time(duration, 'install user FW', devices)


def install_default_image_fw(system, test_name, fw_has_changed, devices):
    with allure.step('Rebooting the dut after image installation'):
        logging.info("Rebooting dut")
        if fw_has_changed:
            res_obj, duration = OperationTime.save_duration('reboot with default FW installation', '', test_name,
                                                            system.reboot.action_reboot, system_is_ready_timeout=PlatformConsts.TIMEOUT_AFTER_FW_INSTALL)
            res = res_obj
            with allure.step('Verify operation time'):
                OperationTime.verify_operation_time(duration, 'reboot with default FW installation', devices).verify_result()
        else:
            res = system.reboot.action_reboot()

        return res


def get_original_fw_path(engines, original_fw):
    fw_dir = "/auto/sw_system_project/MLNX_OS_INFRA/mlnx_os2/sx_mlnx_fw/"
    orig_fw_file = engines[NvosConst.SONIC_MGMT].run_cmd(f"ls {fw_dir}| grep {original_fw}")
    fw_path = fw_dir + orig_fw_file
    logger.info(f"original fw path is: {fw_path}")
    return fw_path


def verify_field_value_in_output_for_each_asic(output_dictionary, field, value):
    ValidationTool.verify_field_value_in_output(output_dictionary, field, value).verify_result()


def validate_all_asics_have_same_info(platform):
    show_output = get_asic_dict(platform)
    output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
    assert output_dictionary, "asic list is empty"

    if len(output_dictionary) > 1:
        with allure.step("Validate all the ASICs have the same info"):
            logging.info("Validate all the ASICs have the same info")
            asic_info = next(iter(output_dictionary.values()))
            for asic in output_dictionary.keys():
                assert asic_info == output_dictionary[asic], "ASICs are different"


def _assert_actual_firmware_matches(output_dictionary, expected_fw):
    # actual-firmware can come back with either dots or underscores depending on
    # source (build metadata vs filename). Compare normalized forms.
    assert expected_fw, f"expected FW version is empty/None: {expected_fw!r}"
    assert "actual-firmware" in output_dictionary, (
        f"'actual-firmware' field missing from output: keys={list(output_dictionary.keys())}"
    )
    reported = output_dictionary["actual-firmware"]
    with allure.step(f"Verify the value of actual-firmware is equal to '{expected_fw}' as expected"):
        assert canonicalize_fw_version(reported) == canonicalize_fw_version(expected_fw), (
            f"actual-firmware mismatch: reported={reported!r}, expected={expected_fw!r}"
        )


def verify_firmware_with_platform_cmd(platform, actual_fw):
    output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(platform.firmware.asic.show()).get_returned_value()
    _assert_actual_firmware_matches(output_dictionary, actual_fw)
    asic_dictionary = get_asic_dict(platform)
    for asic in asic_dictionary:
        _assert_actual_firmware_matches(asic_dictionary[asic], actual_fw)
