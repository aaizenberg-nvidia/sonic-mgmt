import pytest
import logging

from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.Devices.IbDevice import RosalindSwitch
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_constants.constants_nvos import PlatformConsts, SystemConsts, CumulusConsts
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
