"""
Front-panel USB disable/enable tests (FR #4294636).

The port is controlled by UEFI/BIOS, so a state change takes effect only on the next boot - the
CLI reboots the DUT itself unless 'skip-reboot' is used. Fixtures live in the area conftest.

CLI under test:
    nv show platform usb-front-panel
    nv action <enable|disable> platform usb-front-panel [force] [skip-reboot]
"""
import pytest

from ngts.nvos_constants.constants_nvos import ActionParamConsts, UsbFrontPanelConsts
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure

# Worst-case duration of a single reboot, including the wait until the system is ready again.
REBOOT_TIMEOUT = 15 * MINUTE
# Headroom for all the CLI actions and show commands of a test, excluding the reboots.
CLI_TIMEOUT = 5 * MINUTE
# 'func_only' is deliberately not used, so the budgets also bound the teardown restore and its
# possible reboot - an unbounded reboot is what turns a failed test into a stuck session.
THREE_REBOOT_TEST_TIMEOUT = CLI_TIMEOUT + 3 * REBOOT_TIMEOUT


@pytest.mark.platform
@pytest.mark.security
@pytest.mark.timeout(THREE_REBOOT_TEST_TIMEOUT)
@pytest.mark.bug(nvbugs=6337141,
                 note='skip-reboot reports the change takes effect after reboot, but nv show '
                      'immediately reports the new state')
def test_usb_front_panel_state_machine(usb_front_panel, usb_front_panel_original_state):
    """
    Verify the front-panel USB state machine end to end.

    The scenarios share reboots: the reboot that activates the staged change also proves the first
    toggle direction was applied. The return leg omits 'skip-reboot' to exercise the action's own
    reboot. How a staged change is reported before it takes effect is checked last, so the open
    reporting bug cannot abort the functional steps - the check itself costs no reboot, since the
    teardown cancels the staged change in place.

    Test flow:
        1. Re-apply the current state and verify nothing changed (idempotency).
        2. Stage the opposite state with 'skip-reboot'.
        3. Reboot and verify the staged state is in effect.
        4. Change back with a plain action, letting it reboot, and verify the original state.
        5. Stage the opposite state again and verify it is reported as pending, not in effect.
    """
    original_state = usb_front_panel_original_state
    opposite_state = UsbFrontPanelConsts.OPPOSITE_STATE[original_state]

    with allure.step(f'Re-apply the current state "{original_state}" and verify it is unchanged'):
        usb_front_panel.action_change_state(original_state, force=True, skip_reboot=True).verify_result()
        usb_front_panel.verify_state(original_state)

    with allure.step(f'Stage "{opposite_state}" with "skip-reboot"'):
        usb_front_panel.action_change_state(opposite_state, force=True, skip_reboot=True).verify_result()

    with allure.step(f'Reboot and verify the staged state "{opposite_state}" is in effect'):
        System().reboot.action_reboot(params=ActionParamConsts.FORCE).verify_result()
        usb_front_panel.verify_state(opposite_state)

    with allure.step(f'Change back to "{original_state}", letting the action reboot the DUT'):
        usb_front_panel.action_change_state(original_state, force=True).verify_result()
        usb_front_panel.verify_state(original_state)

    with allure.step(f'Stage "{opposite_state}" again and verify it is pending, not in effect'):
        usb_front_panel.action_change_state(opposite_state, force=True, skip_reboot=True).verify_result()
        usb_front_panel.verify_state(original_state, pending_state=opposite_state)


@pytest.mark.platform
@pytest.mark.security
@pytest.mark.timeout(THREE_REBOOT_TEST_TIMEOUT)
@pytest.mark.usefixtures("usb_front_panel_storage_attached")
@pytest.mark.bug(nvbugs=6338258,
                 note='state shows disabled, but a USB disk stays enumerated')
def test_usb_front_panel_disabled_blocks_usb_storage(usb_front_panel):
    """
    Verify that disabling the front-panel USB drops an attached USB disk from lsblk.

    Skipped unless a USB stick is attached. NVOS does not auto-mount USB.

    Test flow:
        1. (fixture) Enable the port and confirm a USB disk is enumerated, else skip.
        2. Disable the port, wait for the reboot, and verify the reported state.
        3. Verify no USB disk is enumerated.
    """
    with allure.step('Disable the front-panel USB'):
        usb_front_panel.action_change_state(ActionParamConsts.DISABLED, force=True).verify_result()
        usb_front_panel.verify_state(ActionParamConsts.DISABLED)

    with allure.step('Verify no USB disk is enumerated'):
        disks = usb_front_panel.get_usb_storage_disks()
        assert not disks, \
            f'The front-panel USB is disabled but USB disk(s) {disks} are still enumerated'
