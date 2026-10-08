import logging
from datetime import datetime
from time import sleep
import pytest

from ngts.nvos_constants.constants_nvos import SystemConsts, PlatformConsts, FansConsts, HealthConsts
from ngts.nvos_tools.infra.BmcTool import (BmcTool, assert_usb0_bmc_link_addrs, read_usb0_ifindex,
                                           wait_for_usb0_bmc_link)
from ngts.nvos_tools.infra.CurlTool import CurlTool
from ngts.nvos_tools.infra.DeviceLogTool import grep_log_lines_after_datetime
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool, wait_for_specific_regex_in_logs
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.switch_recovery import recover_dut_with_remote_reboot
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.nvos_tools.Devices.EthDevice import EthSwitch
from ngts.nvos_tools.Devices.IbDevice import JulietNonScaleoutSwitchGB300
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import BmcUsers

logger = logging.getLogger()


BMC_ON_LOG_LINE = r"BMC status changed from .* to Ok"
LOG_DELAY = 150
CPU_MAX_UTILIZATION = 40.0
BMC_LOG_LINES_MAX = 30


@pytest.mark.dut_reboot
@pytest.mark.disable_loganalyzer
def test_recover_from_bmc_reset(engines, devices, topology_obj, loganalyzer):
    # LA ignores some BMC errors during this test, pending on FR 4210208
    if is_bug_active(4359149) and isinstance(devices.dut, JulietNonScaleoutSwitchGB300):
        pytest.skip("Skipping test because we have a bug in bmc reset factory for gb300.")
    system = System()
    platform = Platform()
    engine = engines.dut
    is_cl = isinstance(TestToolkit.get_device(), EthSwitch)

    with allure.step("Add health.err to loganalyzer ignore list"):
        for log_analyzer_instance in (loganalyzer or {}).values():
            log_analyzer_instance.ignore_regex.append('health.err')

    with allure.step("Assert BMC status is ok before starting test"):
        initial_bmc_status = OutputParsingTool.parse_json_str_to_dictionary(
            platform.inventory.show('BMC')).get_returned_value()
        assert initial_bmc_status[PlatformConsts.INV_STATE] == FansConsts.STATE_OK

    with allure.step("Get health issues before starting test"):
        initial_health_issues = OutputParsingTool.parse_json_str_to_dictionary(
            system.health.show()).get_returned_value()[HealthConsts.ISSUES].keys()

    with allure.step("Capture initial overall health status (gates strict post-reset validation)"):
        # Skipping it when baseline isn't OK avoids false failures from pre-existing,
        # non-BMC-related issues on the DUT.
        initial_system_output = OutputParsingTool.parse_json_str_to_dictionary(
            system.show()).get_returned_value()
        initial_health_status = initial_system_output[SystemConsts.HEALTH][HealthConsts.STATUS]
        logger.info(f"Initial overall health status: {initial_health_status!r}")

    with allure.step("Reset BMC"):
        start_time = datetime.now()
        # Read before the reset: the usb0 re-enumeration is what CL waits on below.
        usb0_ifindex_before = read_usb0_ifindex(engine) if is_cl else None
        BmcTool.reset(engine)

    try:
        with allure.step("Wait for the BMC to come back"):
            if is_cl:
                # Cumulus does not emit BMC_ON_LOG_LINE; wait on the usb0 link instead.
                wait_for_usb0_bmc_link(engine, timeout_sec=LOG_DELAY,
                                       previous_ifindex=usb0_ifindex_before)
            else:
                wait_for_specific_regex_in_logs(engine, BMC_ON_LOG_LINE, LOG_DELAY)
            logger.info("waiting 2:30 minutes for system to recover...")
            sleep(150)

        # pre-check loop until health reports ready (same signal the proxy requires for normal run_cmd).
        with allure.step("Wait for NVUE system-ready (nv show system) on SSH after BMC reset"):
            DutUtilsTool.wait_for_nv_show_system_ready_on_ssh(
                engine, timeout_sec=devices.dut.timeout_system_is_ready, poll_sec=10
            )

        with allure.step("Validating BMC reachability"):
            try:
                BmcTool.get_bmc_ip_addresses(engines, topology_obj)
            except Exception as e:
                raise Exception("Unable to reach BMC") from e

        # The CL wait above accepts the bare address; re-check with the prefix so a
        # mask regression after re-enumeration is caught too (#5124733).
        assert_usb0_bmc_link_addrs(engine)

        with allure.step("Assert BMC status is ok"):
            final_bmc_status = OutputParsingTool.parse_json_str_to_dictionary(
                platform.inventory.show('BMC')).get_returned_value()
            assert final_bmc_status == initial_bmc_status

        with allure.step("Check general system status"):
            with allure.independent_step("Assert logs aren't flooded with BMC error messages"):
                bmc_log_lines = grep_log_lines_after_datetime(engine, 'bmc', start_time)
                assert len(bmc_log_lines) < BMC_LOG_LINES_MAX, (
                    f'BMC reset causes log flooding: logs contain {len(bmc_log_lines)} lines regarding BMC, more '
                    f'than the threshold of {BMC_LOG_LINES_MAX}:\n\n' +
                    '\n'.join(bmc_log_lines)
                )

            with allure.independent_step("Assert health ok"):
                final_health_issues = OutputParsingTool.parse_json_str_to_dictionary(
                    system.health.show()).get_returned_value()[HealthConsts.ISSUES].keys()
                assert set(final_health_issues) <= set(initial_health_issues)

            with allure.independent_step("Verify system health status and status-led after BMC reset"):
                if initial_health_status == HealthConsts.OK:
                    expected_led = HealthConsts.LED_OK_STATUS if devices.dut.is_eth() else None
                    system.validate_health_status(
                        HealthConsts.OK, expected_led=expected_led)
                else:
                    logger.info(
                        f"Skipping strict health validation: initial status was "
                        f"{initial_health_status!r}, not {HealthConsts.OK!r}. "
                        f"Pre-existing issues unrelated to BMC reset are tolerated."
                    )

            with allure.independent_step("Assert show-firmware command is not broken"):
                firmware_output = platform.firmware.show("BMC")
                assert "actual-firmware" in firmware_output
                # test_show_platform_firmware(engines, devices, test_api, output_format)

            with allure.independent_step("Assert CPU usage"):
                cpu_output = OutputParsingTool.parse_json_str_to_dictionary(system.show("cpu")).get_returned_value()
                cpu_utilization = cpu_output[SystemConsts.CPU_TOTAL_UTILIZATION_KEY]
                assert cpu_utilization <= CPU_MAX_UTILIZATION

    except Exception:
        with allure.step("Failed to recover from BMC reset. Fixing device by remote-reboot."):
            recover_dut_with_remote_reboot(topology_obj, engines)
        raise
    finally:
        # CL has no `nv action reset platform bmc-password` ("'bmc-password' is not
        # one of ['transceiver']"), and BMC_INTERNAL_IP is the IB path, not usb0.
        # The failure is recorded as a ResultObj, so try/except is not enough to
        # keep it out of the verify_result_objects teardown — skip the block.
        if is_cl:
            logger.info("Skipping NVUE BMC password restore: not supported on CL")
        else:
            with allure.step("Restore BMC root password to known state"):
                try:
                    platform.bmc_password.action_reset()
                    client = CurlTool(server_host=PlatformConsts.BMC_INTERNAL_IP,
                                      username=BmcUsers.root.username,
                                      password=BmcUsers.root.default_password)
                    client.change_root_password(password=BmcUsers.root.default_password)
                except Exception as e:
                    logger.warning(f"Failed to restore BMC root password: {e}")
