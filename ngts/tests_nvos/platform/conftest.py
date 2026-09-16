import pytest
import logging
from typing import Iterator

from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.platform.UsbFrontPanel import UsbFrontPanel
from ngts.nvos_tools.Devices.IbDevice import RosalindSwitch
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_constants.constants_nvos import ActionParamConsts, ApiType, PlatformConsts, SystemConsts, \
    CumulusConsts, UsbFrontPanelConsts
from ngts.nvos_tools.infra.SudoScope import sudo_scope_if
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active


@pytest.fixture(scope='module')
def skip_for_fanless_setup(devices):
    """
    If setup has no fans, JulietNonScaleoutSwitchGB300, skip test.
    """
    if len(devices.dut.fan_list) == 0:
        pytest.skip("Skipping all tests in this module because setup has no fans")


logger = logging.getLogger()


def skip_unsupported_dpc(device) -> None:
    if not isinstance(device, RosalindSwitch):
        pytest.skip("DPC voltage update is only supported on Rosalind devices")
    if is_bug_active(5223078):
        pytest.skip(f"DPC tests skipped while redmine issue 5160583 is open")


@pytest.fixture(scope='function')
def clear_asic_files():
    yield
    platform = Platform()
    with allure.step('delete fetched firmware asic image files'):
        files = platform.firmware.asic.files.get_files()
        platform.firmware.asic.files.delete_files(files_to_delete=files).verify_result()


def _is_min_psu_present_in_output(output):
    """
    Return True only if output looks like a valid MIN_PSU line from VPD data.
    Rejects empty output, stderr messages, and any non-VPD text.
    """
    if not output or not isinstance(output, str):
        return False
    return CumulusConsts.MIN_PSU_VPD_PATTERN.search(output.strip()) is not None


def collect_min_psu_from_vpd_data(engines, devices):
    """
    Read MIN_PSU from /var/run/hw-management/eeprom/vpd_data.
    :param engines: Engines object
    :param devices: Devices fixture (used for sudo_scope_if on Ethernet)
    :return: True if a valid MIN_PSU line was found, False otherwise (missing file,
             permission denied, or no MIN_PSU line). Does not rely on raw output truthiness.
    """
    cmd = "cat /var/run/hw-management/eeprom/vpd_data | grep MIN_PSU"
    with allure.step("Collect MIN_PSU from /var/run/hw-management/eeprom/vpd_data"):
        with sudo_scope_if(condition=devices.dut.is_eth()):
            output = engines.dut.run_cmd(cmd)
    return _is_min_psu_present_in_output(output)


@pytest.fixture(scope='function')
def min_psu_not_available_eth(devices, engines):
    """
    Fixture to check if MIN_PSU is not available on Ethernet devices.
    :param devices: Devices fixture
    :param engines: Engines fixture
    :return: True if device is Ethernet and MIN_PSU is not available (missing or invalid VPD).
    """
    return devices.dut.is_eth() and not collect_min_psu_from_vpd_data(engines, devices)


@pytest.fixture(scope='function')
def usb_front_panel() -> Iterator[UsbFrontPanel]:
    """
    Return the DUT's '/platform/usb-front-panel' component, skipping if it is not supported.

    The feature is exposed through the NVUE CLI only, so the API is pinned for the whole test.
    Support is detected from the CLI instead of a hardcoded platform list. Only the errors of an
    unknown resource mean "unsupported" - any other failure fails the test instead of skipping it.
    """
    with TestToolkit.force_api(ApiType.NVUE):
        component = Platform().usb_front_panel
        result = component.show(if_returned_value=False).ignore_result()
        if not result.result:
            logger.info(f'Show of the front-panel USB failed: {result.info}')
            if not any(error in result.info for error in UsbFrontPanelConsts.UNSUPPORTED_ERRORS):
                result.verify_result()
            pytest.skip(f'The DUT does not support the front-panel USB feature: {result.info}')
        yield component


@pytest.fixture(scope='function')
def usb_front_panel_original_state(usb_front_panel) -> Iterator[str]:
    """
    Return the front-panel USB state the DUT started with, and restore it after the test.

    Whether a reboot is needed is not decided from 'state', the field NVBugs 6337141 misreports
    while a change is staged. The original state is requested with 'skip-reboot' first - free, and
    it also cancels anything a failed test left staged - and 'reboot-required' then decides. The
    reboot is explicit, so a request the CLI treats as a no-op cannot leave the teardown waiting.
    """
    original_state = usb_front_panel.get_state()
    yield original_state

    with allure.step(f'Restore the front-panel USB to "{original_state}"'):
        usb_front_panel.action_change_state(original_state, force=True, skip_reboot=True).verify_result()
        if usb_front_panel.parse_show()[UsbFrontPanelConsts.REBOOT_REQUIRED] == ActionParamConsts.YES:
            with allure.step(f'Reboot to apply the restored state "{original_state}"'):
                System().reboot.action_reboot(params=ActionParamConsts.FORCE).verify_result()
        usb_front_panel.verify_state(original_state)


@pytest.fixture(scope='function')
def usb_front_panel_storage_attached(usb_front_panel, usb_front_panel_original_state) -> None:
    """
    Ensure a front-panel USB storage device is attached and reachable, or skip the test.

    The functional checks are meaningful only with a real USB stick in the front-panel port.
    NVOS does not auto-mount USB; reachability is a USB disk in lsblk.
    """
    if usb_front_panel_original_state != ActionParamConsts.ENABLED:
        usb_front_panel.action_change_state(ActionParamConsts.ENABLED, force=True).verify_result()
        usb_front_panel.verify_state(ActionParamConsts.ENABLED)
    if not usb_front_panel.get_usb_storage_disks():
        pytest.skip('No USB disk enumerated (lsblk TRAN=usb TYPE=disk) - '
                    'the functional checks require a USB stick in the front-panel port')
