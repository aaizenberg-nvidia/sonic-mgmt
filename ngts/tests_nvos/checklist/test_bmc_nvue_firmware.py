"""
P0 NVUE BMC Firmware tests (TC-BMC-005 through TC-BMC-009).

Covers the end-to-end firmware fetch/install cycle, skip-reboot staging,
stress, and the negative fetch/install cases over both update transports.
"""
import logging
import time

import pytest

from ngts.nvos_constants.constants_nvos import PlatformConsts
from ngts.nvos_tools.infra.BmcCliTool import BmcCliTool
from ngts.nvos_tools.infra.BmcTool import assert_usb0_bmc_link_addrs
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.platform.bmc_helpers import (
    bmc_image_version_name,
    ensure_bmc_image_staged,
    ensure_usb0_up,
    get_bmc_firmware,
    get_fw_info,
    nos_uptime_seconds,
    recover_bmc_after_failed_install,
    skip_if_no_bmc,
    update_bmc_firmware_via_bmc_cli,
    update_bmc_firmware_via_nvue,
    update_bmc_firmware_via_redfish,
    validate_bmc_healthy,
    wait_for_bmc_ready,
    wait_for_erot_copy_idle,
)
from ngts.tools.test_utils import allure_utils as allure

cumulus_owner = "gpazbenitzha"
logger = logging.getLogger()

FW_INSTALL_MAX_MINUTES = 30
STRESS_CYCLES = 3

# BMC firmware update transports exercised by the fetch/install test. The same
# test runs once over each: 'bmc_cli' (bmc-cli firmware --file), 'nvue'
# (nv action fetch/install platform firmware BMC) and 'redfish' (multipart POST
# to UpdateService). The CLI transports reach that same endpoint underneath, so
# 'redfish' is what keeps a break in the API itself from hiding behind them.
BMC_FW_INTERFACES = ["bmc_cli", "nvue", "redfish"]

# The stress case runs 3 upgrade/downgrade cycles per transport (~2.5h each), so
# it stays on the two CLI transports. A third would buy no new stress surface --
# all three drive the same UpdateService burn.
BMC_FW_STRESS_INTERFACES = ["bmc_cli", "nvue"]


def _skip_if_no_bmc_image_pair(devices):
    """Skip a burn when this platform has no latest/previous image pair.

    SN6800 and SN6810 are still in bring-up. Their device classes leave
    ``bmc_older_version_path`` unset so these cases cannot fall through to
    the SN6600 catalog.
    """
    if getattr(devices.dut, 'bmc_older_version_path', None) is None:
        pytest.skip("No BMC firmware image pair is defined for this platform")


def _fetch_and_install(engines, topology_obj, test_name, version="latest",
                       interface="bmc_cli"):
    """Update BMC firmware to a version and return (version_name, duration_seconds).

    The same end-to-end update runs over three interchangeable transports on
    CL/SPC6, selected by ``interface`` so the caller can parametrize them:

      - ``interface="bmc_cli"``: ``bmc-cli firmware --file <image>`` then
        ``bmc-cli reset bmc-reset``.
      - ``interface="nvue"``: ``nv action fetch`` (file:// from /var/tmp) then
        ``nv action install platform firmware BMC files <image>``.
      - ``interface="redfish"``: multipart POST to ``UpdateService``, then poll
        the returned Task, then the same activating BMC reset.

    All three consume a ``.fwpkg`` from the FW versions JSON, uploaded to the
    switch by ``ensure_bmc_image_staged`` so the test does not depend on a manual
    pre-stage.
    """
    image_path = ensure_bmc_image_staged(engines, version)
    version_name = bmc_image_version_name(image_path)

    start = time.time()
    if interface == "nvue":
        with allure.step(f"Update BMC firmware to {version_name} via NVUE ({image_path})"):
            update_bmc_firmware_via_nvue(engines, image_path, topology_obj, test_name=test_name)
    elif interface == "redfish":
        with allure.step(f"Update BMC firmware to {version_name} via Redfish ({image_path})"):
            update_bmc_firmware_via_redfish(engines, image_path, topology_obj)
    else:
        with allure.step(f"Update BMC firmware to {version_name} via bmc-cli ({image_path})"):
            update_bmc_firmware_via_bmc_cli(engines, image_path)
    duration = time.time() - start

    with allure.step("Re-establish BMC connectivity after update"):
        ensure_usb0_up(engines.dut, strict=False)
        wait_for_bmc_ready(engines, topology_obj)

    with allure.step(f"Verify BMC firmware version is now {version_name}"):
        fw_after = get_bmc_firmware(engines)
        actual = fw_after.get(PlatformConsts.FW_ACTUAL, '')
        logger.info(f"BMC firmware after update: {actual} (expected: {version_name})")
        # Empty actual must not pass: ``'' in version_name`` is True in Python.
        assert actual, (
            f"BMC firmware version empty after update (expected '{version_name}'); "
            f"fw_after={fw_after}")
        assert version_name in actual or actual in version_name, \
            f"Firmware version mismatch: got '{actual}', expected '{version_name}'"

    return version_name, duration


@pytest.mark.timeout(60 * MINUTE, func_only=True)
@pytest.mark.bmc
@pytest.mark.checklist
@pytest.mark.firmware
@pytest.mark.disable_loganalyzer
@pytest.mark.cumulus
@pytest.mark.parametrize("interface", BMC_FW_INTERFACES)
def test_nvue_firmware_fetch_and_install(engines, devices, topology_obj, test_name, interface):
    """TC-BMC-005: End-to-end firmware upgrade/downgrade cycle with timing assertion.

    Runs once per transport (``bmc_cli``, ``nvue`` and ``redfish``) so the
    bmc-cli, ``nv action fetch/install`` and raw Redfish paths are all validated
    by the same case.

    Steps:
        1. Record current BMC firmware version.
        2. Fetch and install the latest version, verify the version changed.
        3. Assert the installation took less than 30 minutes.
        4. Restore: fetch and install the previous version.
    """
    skip_if_no_bmc(devices, engines)
    _skip_if_no_bmc_image_pair(devices)

    with allure.step("Record current BMC firmware version"):
        original_fw = get_bmc_firmware(engines)
        original_version = original_fw.get(PlatformConsts.FW_ACTUAL)
        logger.info(f"Current BMC firmware: {original_version}")

    try:
        with allure.step(f"Upgrade: fetch and install latest version via {interface}"):
            new_version, install_duration = _fetch_and_install(
                engines, topology_obj, test_name, version="latest", interface=interface)
            logger.info(f"Upgraded BMC firmware to {new_version}")

        with allure.step(f"Assert install time < {FW_INSTALL_MAX_MINUTES} minutes"):
            duration_minutes = install_duration / 60
            logger.info(f"Install duration: {duration_minutes:.1f} minutes")
            assert duration_minutes < FW_INSTALL_MAX_MINUTES, \
                f"Install took {duration_minutes:.1f} min, threshold is {FW_INSTALL_MAX_MINUTES} min"

        validate_bmc_healthy(engines)
        # A firmware install reboots the BMC, so usb0 re-enumerates: check the
        # addressing survived it (#5124733). Deliberately only on the upgrade path
        # and not on the restore below, where the surrounding except-and-recover
        # would swallow the failure and still report the case as passed.
        assert_usb0_bmc_link_addrs(engines.dut)

    finally:
        with allure.step(f"Restore: fetch and install previous version via {interface}"):
            try:
                _fetch_and_install(
                    engines, topology_obj, test_name, version="previous", interface=interface)
                validate_bmc_healthy(engines)
            except Exception:
                logger.error("Failed to restore firmware -- recovering device")
                recover_bmc_after_failed_install(engines, topology_obj)


@pytest.mark.timeout(60 * MINUTE, func_only=True)
@pytest.mark.bmc
@pytest.mark.firmware
@pytest.mark.disable_loganalyzer
@pytest.mark.cumulus
def test_nvue_firmware_install_skip_reboot(engines, devices, topology_obj, test_name):
    """TC-BMC-006 / SN5: Verify skip-reboot stages firmware without activating it.

    Steps:
        1. Record BMC firmware version and NOS uptime.
        2. Stage the latest image and install with ``skip-reboot``.
        3. Assert the NOS did not reboot (uptime still climbing).
        4. Activate via ``bmc-cli reset bmc-reset`` and wait for the BMC.
        5. Assert the BMC firmware version is now the staged target.
    """
    skip_if_no_bmc(devices, engines)
    _skip_if_no_bmc_image_pair(devices)

    with allure.step("Record current firmware version and NOS uptime"):
        fw_before = get_bmc_firmware(engines)
        version_before = fw_before[PlatformConsts.FW_ACTUAL]
        uptime_before = nos_uptime_seconds(engines.dut)
        assert uptime_before is not None, "Could not read NOS /proc/uptime"
        logger.info(f"Version before: {version_before}, uptime: {uptime_before}s")

    # Prefer a version that differs from the currently-active one so the
    # post-activation assert is meaningful. If we are already on catalog
    # 'latest', stage 'previous' instead (a downgrade with skip-reboot is
    # still valid). Compare against the catalog version_name -- never a
    # hard-coded build number that goes stale when the catalog moves.
    latest_name = get_fw_info(engines.dut, "latest")[2]
    target_version = (
        "previous" if version_before and latest_name in str(version_before)
        else "latest"
    )
    image_path = ensure_bmc_image_staged(engines, target_version)
    version_name = bmc_image_version_name(image_path)

    try:
        with allure.step(f"Install {version_name} with skip-reboot via NVUE"):
            update_bmc_firmware_via_nvue(engines, image_path, topology_obj,
                                         test_name=test_name, skip_reboot=True)

        with allure.step("Verify NOS did NOT reboot (SSH alive, uptime intact)"):
            uptime_after = nos_uptime_seconds(engines.dut)
            assert uptime_after is not None, "Could not read NOS /proc/uptime after install"
            logger.info(f"Uptime after skip-reboot install: {uptime_after}s")
            assert uptime_after >= uptime_before, \
                f"NOS appears to have rebooted: uptime {uptime_before}s -> {uptime_after}s"

        with allure.step("Activate pending firmware via a BMC reset"):
            # skip-reboot only STAGES the image. Activation requires a BMC reset,
            # not a NOS reboot (nv action reboot system leaves staged BMC FW inactive).
            BmcCliTool(engines.dut).reset_bmc()
            wait_for_bmc_ready(engines, topology_obj)
            wait_for_erot_copy_idle(engines)

        with allure.step("Verify FW version reflects the install after BMC reset"):
            fw_after = get_bmc_firmware(engines)
            version_after = fw_after.get(PlatformConsts.FW_ACTUAL, '')
            logger.info(f"Version before: {version_before}, staged/target: {version_name}, "
                        f"after BMC reset: {version_after}")
            # Empty version_after must not pass: ``'' in version_name`` is True.
            assert version_after, (
                f"BMC firmware version empty after BMC reset "
                f"(expected '{version_name}'); fw_after={fw_after}")
            assert version_name in version_after or version_after in version_name, \
                (f"Staged firmware did not activate after BMC reset: "
                 f"actual '{version_after}', expected '{version_name}'")
            validate_bmc_healthy(engines)

    except Exception:
        with allure.step("skip-reboot test failed -- recovering device"):
            recover_bmc_after_failed_install(engines, topology_obj)
        raise


@pytest.mark.bmc
@pytest.mark.firmware
@pytest.mark.stress
@pytest.mark.slow
@pytest.mark.disable_loganalyzer
@pytest.mark.cumulus
@pytest.mark.parametrize("interface", BMC_FW_STRESS_INTERFACES)
def test_nvue_firmware_install_stress(engines, devices, topology_obj, test_name, interface):
    """TC-BMC-009 / SN8: 3 consecutive upgrade/downgrade cycles for stability.

    Marked ``@slow`` -- 3 cycles × upgrade+downgrade × 2 transports is ~12 full
    BMC burns and is not part of the fast suite. Deliberately parametrized over
    ``BMC_FW_STRESS_INTERFACES`` rather than every transport the fetch/install
    case covers, to keep that cost from growing with each one added.
    """
    skip_if_no_bmc(devices, engines)
    _skip_if_no_bmc_image_pair(devices)

    try:
        for cycle in range(1, STRESS_CYCLES + 1):
            with allure.step(f"Cycle {cycle}/{STRESS_CYCLES}: upgrade to latest via {interface}"):
                _fetch_and_install(engines, topology_obj, test_name, version="latest",
                                   interface=interface)
                validate_bmc_healthy(engines)

            with allure.step(f"Cycle {cycle}/{STRESS_CYCLES}: downgrade to previous via {interface}"):
                _fetch_and_install(engines, topology_obj, test_name, version="previous",
                                   interface=interface)
                validate_bmc_healthy(engines)

            logger.info(f"Stress cycle {cycle}/{STRESS_CYCLES} completed successfully")

        with allure.step("Final health check after all cycles"):
            validate_bmc_healthy(engines)

    except Exception:
        with allure.step("Stress test failed -- recovering device"):
            recover_bmc_after_failed_install(engines, topology_obj)
        raise


@pytest.mark.bmc
@pytest.mark.firmware
@pytest.mark.cumulus
def test_nvue_firmware_wrong_image_blocked(engines, devices, topology_obj):
    """TC-BMC-007: Verify corrupted firmware image is rejected cleanly.

    Steps:
        1. Create a dummy file on the DUT and copy it into the NVUE firmware dir.
        2. Attempt install -- verify error.
        3. Verify BMC is still healthy.
        4. Clean up.
    """
    skip_if_no_bmc(devices, engines)
    bad_fw_name = 'bad_fw_test.bin'
    bad_fw_tmp_path = f'/tmp/{bad_fw_name}'
    platform_component = Platform().firmware.bmc

    try:
        with allure.step("Create dummy firmware file and place in firmware directory"):
            engines.dut.run_cmd(
                f'dd if=/dev/urandom of={bad_fw_tmp_path} bs=4096 count=1',
                validate=False)
            fw_dir = '/host/cumulus/firmware/bmc'
            engines.dut.run_cmd(f'sudo mkdir -p {fw_dir}', validate=False)
            engines.dut.run_cmd(
                f'sudo cp {bad_fw_tmp_path} {fw_dir}/{bad_fw_name}',
                validate=False)

        with allure.step("Attempt to install bad firmware -- expect failure"):
            result = platform_component.files.file_name[
                bad_fw_name].action_file_install()
            if isinstance(result, ResultObj):
                result.ignore_result()
                result_str = str(result)
                # Product rejection from Aug-6 green run (bmc-cli / NVUE install path):
                # "Invalid FW Package header" — do not match generic "firmware".
                assert "invalid fw package header" in result_str.lower(), \
                    f"Expected product rejection 'Invalid FW Package header', got: {result}"
                logger.info(f"Install correctly rejected: {result_str[:300]}")
            else:
                pytest.fail(f"Expected ResultObj with error, got: {result}")

        with allure.step("Verify BMC is still healthy after bad install attempt"):
            validate_bmc_healthy(engines)

    finally:
        with allure.step("Cleanup: remove dummy files"):
            engines.dut.run_cmd(f'rm -f {bad_fw_tmp_path}', validate=False)
            engines.dut.run_cmd(
                f'sudo rm -f /host/cumulus/firmware/bmc/{bad_fw_name}',
                validate=False)


@pytest.mark.bmc
@pytest.mark.firmware
@pytest.mark.cumulus
def test_nvue_firmware_fetch_unreachable_url(engines, devices, topology_obj):
    """TC-BMC-008: Verify fetch from unreachable URL fails with clear error.

    Steps:
        1. Attempt fetch from unreachable URL -- expect failure.
        2. Verify error message indicates timeout/unreachable.
        3. Verify BMC is still healthy.
    """
    skip_if_no_bmc(devices, engines)
    unreachable_url = 'http://192.168.255.255/nonexistent.bin'
    platform_component = Platform().firmware.bmc

    with allure.step(f"Attempt to fetch from unreachable URL: {unreachable_url}"):
        try:
            result = platform_component.action_fetch(unreachable_url, base_url='')
            if isinstance(result, ResultObj):
                result.ignore_result()
                if not result.result:
                    logger.info(
                        f"Fetch correctly returned failure ResultObj: {str(result)[:300]}")
                else:
                    pytest.fail(
                        "Fetch from unreachable URL should have failed but succeeded")
            else:
                pytest.fail(
                    "Fetch from unreachable URL should have failed but succeeded")
        except Exception as e:
            error_msg = str(e).lower()
            logger.info(f"Fetch correctly failed with: {error_msg[:500]}")
            error_indicators = ['error', 'fail', 'timeout', 'unreachable',
                                'refused', 'could not', 'unable']
            assert any(ind in error_msg for ind in error_indicators), \
                f"Error message doesn't indicate fetch failure: {e}"

    with allure.step("Verify BMC is still healthy after failed fetch"):
        # A failed HTTP fetch does not touch the BMC, but usb0 (the CPU<->BMC
        # link) may still be recovering from an earlier test's BMC reset. Wait
        # for usb0/BMC to be reachable before asserting health, otherwise
        # 'nv show platform inventory BMC' transiently returns state=None.
        ensure_usb0_up(engines.dut, strict=False)
        wait_for_bmc_ready(engines, topology_obj)
        validate_bmc_healthy(engines)
