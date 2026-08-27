"""
P0 NVUE BMC Power-Cycle and Reboot Tests (TC-BMC-011 through TC-BMC-013, TC-BMC-017).
Validates system reboot modes that go through BMC when present, and verifies
that a CPU-only reboot leaves the BMC undisturbed.

Reboot modes (via BMC when present):
    - ``nv action reboot system mode power-cycle`` (TC-BMC-011)
    - ``nv action reboot system mode cold`` (TC-BMC-012)
    - ``nv action reboot system mode immediate`` (TC-BMC-013)

CPU-only reboot:
    - ``nv action reboot system force`` (TC-BMC-017)
"""
import logging
import time

import pytest

from ngts.nvos_constants.constants_nvos import FansConsts
from ngts.nvos_tools.infra.BmcTool import assert_usb0_bmc_link_addrs
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.platform.bmc_helpers import (
    bmc_host_console_capture,
    bmc_recover_nos,
    console_target,
    ignore_cl_reboot_result,
    nos_uptime_seconds,
    skip_if_no_bmc,
    snapshot_bmc_state_safe,
    wait_for_nos_reboot,
)
from ngts.tools.test_utils import allure_utils as allure

cumulus_owner = "gpazbenitzha"
logger = logging.getLogger()

# Wall-clock gate from reboot trigger through wait_for_nos_reboot return.
# Includes harness overhead (NOS_REBOOT_SETTLE_SECONDS=60 plus reconnect/usb0),
# not pure platform RTO. 420s matches the former v1 budget and leaves headroom
# above observed power-cycle ~274s on green.
POWER_CYCLE_RECOVERY_MAX_SECONDS = 420

# Text the BMC must see on the host console across a reboot. 'GRUB' cannot
# predate the reset (the bootloader only runs on boot) and 'Reached target'
# shows systemd came back up. Kernel printk is not usable here: the CL cmdline
# carries 'quiet'.
HOST_BOOT_MARKERS = ('GRUB', 'Reached target')


def _reboot_and_verify(engines, devices, topology_obj, mode):
    """Shared logic for TC-BMC-011/012/013: reboot with mode, recover, verify.

    Args:
        mode: Reboot mode -- 'power-cycle', 'cold', or 'immediate'.
    Steps:
        1. Snapshot BMC state before.
        2. Trigger ``nv action reboot system mode <mode>``.
        3. Wait for recovery, assert time < threshold.
        4. Verify BMC state 'ok'.
        5. Compare BMC state before/after.
    """
    with allure.step("Snapshot BMC state before reboot"):
        state_before = snapshot_bmc_state_safe(engines)
        logger.info(f"BMC before: state={state_before['state']}, "
                    f"fw={state_before['firmware_version']}")
        assert state_before['state'] == FansConsts.STATE_OK, \
            f"BMC not healthy before reboot: {state_before['state']}"

    with allure.step(f"Trigger: nv action reboot system mode {mode}"):
        uptime_before = nos_uptime_seconds(engines.dut)
        start_time = time.time()
        # NOTE: the valid CL syntax is 'mode <mode>' (NOT 'force mode <mode>' —
        # 'mode' after 'force' is rejected with "Invalid parameter: 'mode'").
        # send_user_confirmation='y' answers the [y/N] prompt;
        # should_wait_till_system_ready=False skips the serial-console wait path.
        result = System().reboot.action_reboot(params=f'mode {mode}',
                                               send_user_confirmation='y',
                                               should_wait_till_system_ready=False)
        ignore_cl_reboot_result(result)
        # Robust reboot wait + uptime-drop proof (port-22 CLOSED detection is
        # unreliable on fast CL reboots).
        wait_for_nos_reboot(engines, uptime_before=uptime_before)
        recovery_time = time.time() - start_time

    with allure.step(
            "Assert recovery time < {}s".format(POWER_CYCLE_RECOVERY_MAX_SECONDS)):
        logger.info(
            "Recovery time (includes harness settle/reconnect): %.1fs"
            % recovery_time)
        assert recovery_time < POWER_CYCLE_RECOVERY_MAX_SECONDS, (
            "Recovery took %.1fs (includes harness settle/reconnect), "
            "threshold is %ss"
            % (recovery_time, POWER_CYCLE_RECOVERY_MAX_SECONDS)
        )

    with allure.step("Verify BMC state is 'ok' after reboot"):
        state_after = snapshot_bmc_state_safe(engines)
        assert state_after['state'] == FansConsts.STATE_OK, \
            f"BMC state after reboot: '{state_after['state']}'"

    with allure.step("Compare BMC state before/after reboot"):
        assert state_before['firmware_version'] == state_after['firmware_version'], \
            (f"FW version changed: '{state_before['firmware_version']}' -> "
             f"'{state_after['firmware_version']}'")
        assert state_before['state'] == state_after['state'], \
            (f"BMC state changed: '{state_before['state']}' -> "
             f"'{state_after['state']}'")


@pytest.mark.bmc
@pytest.mark.platform
@pytest.mark.power_cycle
@pytest.mark.disable_loganalyzer
@pytest.mark.skip_clear_config
@pytest.mark.cumulus
def test_nvue_reboot_mode_power_cycle(engines, devices, topology_obj):
    """TC-BMC-011: Verify ``nv action reboot system mode power-cycle``.

    When BMC is present, this power-cycles via BMC (both CPU and BMC reboot).
    """
    skip_if_no_bmc(devices, engines)
    try:
        _reboot_and_verify(engines, devices, topology_obj, mode='power-cycle')
    except Exception:
        bmc_recover_nos(engines, topology_obj)
        raise


@pytest.mark.bmc
@pytest.mark.platform
@pytest.mark.power_cycle
@pytest.mark.disable_loganalyzer
@pytest.mark.skip_clear_config
@pytest.mark.cumulus
def test_nvue_reboot_mode_cold(engines, devices, topology_obj):
    """TC-BMC-012: Verify ``nv action reboot system mode cold``."""
    skip_if_no_bmc(devices, engines)
    try:
        _reboot_and_verify(engines, devices, topology_obj, mode='cold')
    except Exception:
        bmc_recover_nos(engines, topology_obj)
        raise


@pytest.mark.bmc
@pytest.mark.platform
@pytest.mark.power_cycle
@pytest.mark.disable_loganalyzer
@pytest.mark.skip_clear_config
@pytest.mark.cumulus
def test_nvue_reboot_mode_immediate(engines, devices, topology_obj):
    """TC-BMC-013: Verify ``nv action reboot system mode immediate``."""
    skip_if_no_bmc(devices, engines)
    try:
        _reboot_and_verify(engines, devices, topology_obj, mode='immediate')
    except Exception:
        bmc_recover_nos(engines, topology_obj)
        raise


@pytest.mark.bmc
@pytest.mark.platform
@pytest.mark.reboot
@pytest.mark.disable_loganalyzer
@pytest.mark.skip_clear_config
@pytest.mark.cumulus
def test_nvue_cpu_only_reboot_bmc_healthy(engines, devices, topology_obj):
    """TC-BMC-017: Verify CPU-only reboot leaves BMC undisturbed on standby power.

    ``nv action reboot system force`` restarts the NOS and CPU but the BMC
    remains running on its independent standby power domain.

    Steps:
        1. Verify BMC healthy, record FW version.
        2. Trigger ``nv action reboot system force``.
        3. Wait for NOS recovery.
        4. Verify BMC state 'ok' immediately after recovery.
        5. Verify BMC FW version unchanged (BMC was never rebooted).
        6. Verify the BMC's console session saw the host reboot and come back.
    """
    skip_if_no_bmc(devices, engines)

    with allure.step("Verify BMC healthy and record state before reboot"):
        state_before = snapshot_bmc_state_safe(engines)
        assert state_before['state'] == FansConsts.STATE_OK, \
            f"BMC not healthy before reboot: {state_before['state']}"
        logger.info(f"BMC before reboot: state={state_before['state']}, "
                    f"fw={state_before['firmware_version']}")

    try:
        with bmc_host_console_capture(engines, topology_obj) as host_console:
            with allure.step("Trigger CPU-only reboot: nv action reboot system force"):
                uptime_before = nos_uptime_seconds(engines.dut)
                # 'nv action reboot system force' is valid on CL (no [y/N] prompt);
                # should_wait_till_system_ready=False skips the serial-console wait.
                result = System().reboot.action_reboot(
                    params='force', should_wait_till_system_ready=False)
                ignore_cl_reboot_result(result)
                wait_for_nos_reboot(engines, uptime_before=uptime_before)

            with allure.step("Verify BMC state 'ok' immediately after NOS recovery"):
                state_after = snapshot_bmc_state_safe(engines)
                assert state_after['state'] == FansConsts.STATE_OK, \
                    f"BMC state after CPU reboot: '{state_after['state']}'"
                # The CPU reboot re-creates usb0 on the host side while the BMC keeps
                # running on standby power, so the addressing has to come back (#5124733).
                assert_usb0_bmc_link_addrs(engines.dut)

            with allure.step(
                    "Verify BMC firmware version unchanged (BMC was not rebooted)"):
                assert state_before['firmware_version'] == state_after['firmware_version'], \
                    (f"BMC FW version changed across CPU reboot: "
                     f"'{state_before['firmware_version']}' -> "
                     f"'{state_after['firmware_version']}'")
                logger.info("BMC stayed healthy across CPU-only reboot (standby power)")

            with allure.step("Verify the BMC console shows the host rebooting"):
                captured = host_console()
                logger.info(f"Captured {len(captured)} bytes of host console via the BMC")
                missing = [marker for marker in HOST_BOOT_MARKERS
                           if marker not in captured]
                assert not missing, \
                    (f"BMC console session did not show the host booting — missing "
                     f"{missing} in {len(captured)} captured bytes")

            with allure.step("Log the serial-console mux target after the CPU reboot"):
                # Not asserted: on mlx-6600-04 a CPU-only reboot left uart_sel at raw
                # 3, which bmc-cli reports as 'unknown', while mlx-6600-09 kept 'bmc'.
                # Logged until we can tell a transient read from a real mux change.
                logger.info(f"Console mux target after CPU reboot: "
                            f"{console_target(engines.dut)}")
    except Exception:
        bmc_recover_nos(engines, topology_obj)
        raise
