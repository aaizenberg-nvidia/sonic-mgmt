import os
import sys
import logging
import pytest

# this code is necessary for the imports below to work
path = os.path.abspath(__file__)
sonic_mgmt_path = path.split('/ngts/')[0]
sys.path.append(sonic_mgmt_path)
sys.path.append(os.path.join(sonic_mgmt_path, "sonic-tool", "mars", "scripts"))

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.general_constants.constants import DefaultConnectionValues
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.OnieTool import OnieTool
from ngts.nvos_tools.infra.PxeTool import PxeTool
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.general.ONIE.constants import OnieConsts
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool

logger = logging.getLogger()


@pytest.mark.no_cli_coverage_run
@pytest.mark.skip_clear_config
def test_install_onie_via_pxe_boot(topology_obj, engines, devices):
    """
    @summary: Verify that the device can install ONIE via PXE boot menu.

    This test verifies the PXE-based ONIE installation process by:
      1. Determining the number of PXE menu steps to reach the correct ONIE entry
         based on the device type (OPN = 1 step, IPN = 3 steps).
      2. Rebooting the device remotely via NVUE.
      3. Entering the PXE boot menu via serial console.
      4. Navigating to the correct ONIE installer entry using a fixed number of arrow down presses.
      5. Waiting for successful installation msg to appear
      6. Waiting for the ONIE GRUB menu to appear - end of the test.

    @param topology_obj: Testbed topology object containing player/device mappings and metadata.
    @param engines:      Dictionary containing CLI and serial engine handles.
    @param devices:      Dictionary containing DUT device objects.
    @raises AssertionError: If any critical step fails during the installation process.
    """
    TestToolkit.tested_api = ApiType.NVUE
    nvue_cli_obj = NvueGeneralCli(engine=engines.dut, device=devices.dut)

    with allure.step("Get PXE menu step count based on device OPN/IPN type"):
        step_count = PxeTool.get_pxe_menu_step_count(topology_obj)

    with allure.step('Execute remote reboot'):
        nvue_cli_obj.remote_reboot_nvue(topology_obj)

    try:
        serial_engine = ConnectionTool.create_serial_engine(topology_obj, enter_serial_context=True)

        with allure.step("Enter PXE boot menu"):
            PxeTool.enter_pxe(serial_engine)
            logger.info("Successfully entered PXE menu")

        with allure.step(f"Select ONIE version entry by stepping {step_count} times"):
            PxeTool.pxe_select_by_steps(serial_engine, step_count)

        with allure.step("Wait for successful installation msg"):
            onie_version = PxeTool.wait_for_onie_success_msg(serial_engine)

        with allure.step(f"Wait for grub menu after installation of ONIE version: {onie_version}"):
            PxeTool.wait_for_grub_menu(serial_engine)

    except Exception as err:
        logger.info("ONIE install via PXE failed on error and will now remote reboot machine:\n{}".format(err))
        nvue_cli_obj.remote_reboot_nvue(topology_obj)
        engines.dut.disconnect()
        raise AssertionError(err)
