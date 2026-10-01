"""
Domain object for the front-panel USB disable/enable feature (FR #4294636).

The port is controlled by UEFI/BIOS, so a state change takes effect only after the next reboot -
which the CLI performs as part of the action unless 'skip-reboot' is used.

Covered CLI:
    nv show platform usb-front-panel
    nv action <enable|disable> platform usb-front-panel [force] [skip-reboot]
"""
import logging

from ngts.nvos_constants.constants_nvos import ActionParamConsts, SystemConsts, UsbFrontPanelConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.DutUtilsTool import RebootParams
from ngts.nvos_tools.infra.LsblkCmdBuilder import LsblkCmdBuilder
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.helpers.general_helpers import run_ssh_cmd_with_rc
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


class UsbFrontPanel(BaseComponent):
    """Represents the '/platform/usb-front-panel' resource."""

    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/usb-front-panel')

    def get_state(self) -> str:
        """
        Return the 'state' field, i.e. the state currently in effect.

        :return: ActionParamConsts.ENABLED or ActionParamConsts.DISABLED.
        :raises AssertionError: When the show output has no 'state' field.
        """
        show_output = self.parse_show()
        assert SystemConsts.STATE in show_output, \
            f'The front-panel USB show output has no "{SystemConsts.STATE}" field: {show_output}'
        state = show_output[SystemConsts.STATE]
        logger.info(f'Reported front-panel USB state: {state}')
        return state

    def verify_state(self, state: str, pending_state: str = None, reboot_required: str = None) -> None:
        """
        Assert the whole show output, rejecting missing, unexpected and empty fields.

        The defaults express the settled case: nothing staged, no reboot required. Passing a
        different 'pending_state' expresses a change staged for the next boot.

        :param state: Expected state in effect.
        :param pending_state: Expected state staged for the next boot. Defaults to 'state'.
        :param reboot_required: Expected 'reboot-required'. Defaults to whether a change is staged.
        """
        pending_state = pending_state or state
        expected = {SystemConsts.STATE: state,
                    UsbFrontPanelConsts.PENDING_STATE: pending_state,
                    UsbFrontPanelConsts.REBOOT_REQUIRED: reboot_required or (
                        ActionParamConsts.NO if pending_state == state
                        else ActionParamConsts.YES)}
        with allure.step(f'Verify the front-panel USB reports {expected}'):
            ValidationTool.validate_output_of_show(self.parse_show(), expected).verify_result()

    def action_change_state(self, state: str, force: bool = False, skip_reboot: bool = False) -> ResultObj:
        """
        Run 'nv action <enable|disable> platform usb-front-panel [force] [skip-reboot]', waiting
        for the reboot the action triggers unless 'skip-reboot' is used.

        :param state: Requested state, ActionParamConsts.ENABLED or DISABLED.
        :param force: Add the 'force' flag, which suppresses the interactive confirmation.
        :param skip_reboot: Add the 'skip-reboot' flag - no reboot, state applies on the next boot.
        :return: ResultObj of the action, not yet verified.
        """
        assert state in UsbFrontPanelConsts.ACTION_PER_STATE, \
            f'Unsupported state "{state}". Supported: {list(UsbFrontPanelConsts.ACTION_PER_STATE)}'
        flags = [flag for flag, requested in ((ActionParamConsts.FORCE, force),
                                              (ActionParamConsts.SKIP_REBOOT, skip_reboot)) if requested]
        with allure.step(f'Change the front-panel USB state to "{state}" (flags={flags})'):
            return self.action(
                UsbFrontPanelConsts.ACTION_PER_STATE[state], flags=flags,
                reboot_params=None if skip_reboot else RebootParams(topology_obj=TestToolkit.topology_obj),
                send_user_confirmation=None if force else UsbFrontPanelConsts.CONFIRMATION,
                # Without a reboot the return code is validated, so matching the response would
                # only over-constrain. Across a reboot the response is all there is to judge by.
                expected_output='' if skip_reboot else UsbFrontPanelConsts.ACTION_REBOOT_EXPECTED_OUTPUT,
                timeout=UsbFrontPanelConsts.ACTION_TIMEOUT)

    def get_usb_storage_disks(self) -> list:
        """
        Return USB disk names from lsblk (TRAN=usb, TYPE=disk). Empty if none are enumerated.

        Raises if lsblk is missing or its JSON cannot be parsed, so a broken check is never
        reported as a correctly blocked port.
        """
        lsblk_cmd = (LsblkCmdBuilder().nodeps().json()
                     .output_columns(UsbFrontPanelConsts.LSBLK_COLUMNS).build())
        with allure.step('List USB disks (lsblk TRAN=usb TYPE=disk)'):
            output, exit_code = run_ssh_cmd_with_rc(TestToolkit.get_engine(), lsblk_cmd)
            assert exit_code != UsbFrontPanelConsts.COMMAND_NOT_FOUND_RC, \
                f'Cannot check USB storage: lsblk is missing. Output: {output}'
            assert exit_code == 0, \
                f'Cannot check USB storage: lsblk exit {exit_code}. Output: {output}'
            payload = OutputParsingTool.parse_json_str_to_dictionary(output).get_returned_value()
            devices = payload.get(UsbFrontPanelConsts.LSBLK_BLOCKDEVICES)
            assert isinstance(devices, list), \
                f'lsblk JSON has no {UsbFrontPanelConsts.LSBLK_BLOCKDEVICES} list: {payload}'
            name_key, tran_key, type_key = (
                column.lower() for column in UsbFrontPanelConsts.LSBLK_COLUMNS.split(','))
            disks = []
            for device in devices:
                if (device.get(tran_key) == UsbFrontPanelConsts.LSBLK_TRAN_USB and
                        device.get(type_key) == UsbFrontPanelConsts.LSBLK_TYPE_DISK):
                    name = device.get(name_key)
                    assert name, f'lsblk USB disk entry has no name: {device}'
                    disks.append(name)
            logger.info(f'Enumerated USB disks: {disks}')
            return disks
