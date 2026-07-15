import logging
import time
import re

import pexpect

from devts.infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine
from ngts.nvos_tools.infra.OnieTool import OnieTool
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


class PxeTool:
    # ---- Pre-boot & PXE Entry ----
    PBA = 'PBA'                              # prompt indicating drive lock or pre-boot authentication
    PXE_START_REGEX = 'American Megatrends'  # banner text indicating PXE environment is available
    SUCCESSFUL_DOWNLOAD = 'NBP file downloaded successfully'  # network bootstrap program fetched

    # ---- Key Sequences & Navigation ----
    ESC_PLUS_8 = "\x1b8"    # ESC then '8' to enter PXE menu
    ENTER = "\015"          # Carriage return / Enter key
    ARROW_UP_CHAR = "\x1b[A"    # ANSI up‑arrow key
    ARROW_DOWN_CHAR = "\x1b[B"  # ANSI down‑arrow key

    # ---- Timing ----
    PEXPECT_TIMEOUT = 2      # default timeout (seconds) for pexpect-based reads
    KEY_STROKE_SLEEP = 0.5   # pause (seconds) between keystrokes

    # ---- Steps for ONIE ----
    OPN_STEPS = 1
    IPN_STEPS = 5

    # ---- Authentication Prompts ----
    PASSWORD_PROMPT = "Enter Password"      # PXE password prompt
    INVALID_PASSWORD_PROMPT = "Invalid Password"  # PXE invalid password response
    DEFAULT_PASSWORD = 'admin'              # default PXE password

    # ---- Menu Titles & Prefixes ----
    PXE_FIRST_TITLE = "localboot"            # first menu entry label to confirm PXE menu

    # ---- Error Messages ----
    SECURE_BOOT_VIOLATION = "Secure Boot Violation"  # secure boot blocking PXE

    @classmethod
    def enter_pxe(cls, serial_engine, after_sed_erase=False):
        """
        Enter the PXE boot menu on the device.

        This method waits for the pre-boot authentication (PBA) prompt,
        then the PXE banner, sends the ESC+8 sequence, handles secure-boot
        or password prompts, and finally verifies the PXE menu is displayed.

        :param serial_engine: the serial engine connected to the DUT
        :
        :raises Exception: on secure boot violation or password failure
        """
        if not after_sed_erase:
            logger.info("Waiting for pre-boot authentication (PBA) prompt...")
            serial_engine.run_cmd('', [cls.PBA], timeout=240, send_without_enter=True)

            time.sleep(cls.KEY_STROKE_SLEEP)
            logger.info("Waiting for PXE start banner...")
            serial_engine.run_cmd('', [cls.PXE_START_REGEX], timeout=240, send_without_enter=True)

            time.sleep(cls.KEY_STROKE_SLEEP)
            logger.info("Sending ESC+8 to enter PXE menu...")
            _, idx = serial_engine.run_cmd(cls.ESC_PLUS_8,
                                           [cls.SUCCESSFUL_DOWNLOAD, cls.PASSWORD_PROMPT, cls.SECURE_BOOT_VIOLATION],
                                           timeout=240,
                                           send_without_enter=True)
            time.sleep(cls.KEY_STROKE_SLEEP)
            # Handle secure boot violation
            if idx == 2:
                logger.info("Secure Boot Violation detected; PXE boot is blocked.")
                # consume ENTER to clear the prompt
                serial_engine.run_cmd(cls.ENTER, '.*', timeout=5, send_without_enter=True)
                raise Exception("Secure Boot Violation: enable PXE boot on the system.")
            # Handle password prompt
            if idx == 1:
                logger.info(f"Password prompt detected; sending default password '{cls.DEFAULT_PASSWORD}'")
                serial_engine.run_cmd(cls.DEFAULT_PASSWORD, '.*', timeout=10, send_without_enter=True)
                time.sleep(cls.KEY_STROKE_SLEEP)
                out2, resp = serial_engine.run_cmd(
                    cls.ENTER,
                    [cls.INVALID_PASSWORD_PROMPT, cls.SUCCESSFUL_DOWNLOAD, cls.SECURE_BOOT_VIOLATION],
                    timeout=15,
                    send_without_enter=True
                )
                if resp == 0:
                    raise Exception("Invalid PXE password; could not enter PXE menu.")
                elif resp == 2:
                    logger.info("Secure Boot Violation detected after password; PXE boot is blocked.")
                    # consume ENTER to clear the prompt
                    serial_engine.run_cmd(cls.ENTER, '.*', timeout=5, send_without_enter=True)
                    raise Exception("Secure Boot Violation: enable PXE boot on the system.")
                logger.info("Entered PXE menu with password.")
            else:
                logger.info("Entered PXE menu without password.")

        time.sleep(cls.KEY_STROKE_SLEEP)
        logger.info("Waiting to see PXE menu options")
        serial_engine.run_cmd('', [cls.PXE_FIRST_TITLE], timeout=240, send_without_enter=True)

    @classmethod
    def get_pxe_menu_step_count(cls, topology_obj):
        """
        Returns number of arrow-down steps in PXE menu based on device type.
        IPN = 6 steps down, OPN = 1 step down

        @param topology_obj: topology object containing players and attributes
        @return: int - number of steps
        """
        return cls.OPN_STEPS if OnieTool.is_opn(topology_obj) else cls.IPN_STEPS

    @classmethod
    def pxe_find_and_select(cls, serial_engine, name, max_selections=40):
        """
        Scroll through the PXE menu and select the given entry.

        :param serial_engine: the serial engine connected to the DUT
        :param name: the exact menu entry text to find
        :param max_selections: maximum number of arrow-down presses before giving up
        :raises Exception: if the named entry is not found
        """
        with allure.step(f"Navigate PXE menu to find '{name}'"):
            for attempt in range(max_selections):
                logger.info(f"Attempt {attempt + 1}: sending ARROW_DOWN_CHAR")
                try:
                    out, res_index = serial_engine.run_cmd(cls.ARROW_DOWN_CHAR, name, 0.05, True)
                    logger.info(f"Found '{name}' in PXE menu")
                    # Press ENTER to select
                    serial_engine.run_cmd(cls.ENTER, '.*', timeout=cls.PEXPECT_TIMEOUT, send_without_enter=True)
                    return
                except pexpect.exceptions.TIMEOUT:
                    continue

        # If we exit the loop, we never found the entry
        logger.error(f"Failed to find PXE menu entry '{name}' after {max_selections} attempts.")
        raise Exception(f"PXE menu entry '{name}' not found.")

    @classmethod
    def pxe_select_by_steps(cls, serial_engine, step_count):
        """
        Select a PXE menu entry by moving down a fixed number of steps.

        :param serial_engine: the serial engine connected to the DUT
        :param step_count: number of arrow down key presses
        """
        with allure.step(f"Navigate PXE menu by {step_count} step(s)"):
            for i in range(step_count):
                logger.info(f"Step {i + 1}/{step_count}: Sending ARROW_DOWN_CHAR")
                serial_engine.run_cmd(cls.ARROW_DOWN_CHAR, '.*', timeout=0.05, send_without_enter=True)
                time.sleep(cls.KEY_STROKE_SLEEP)

            logger.info("Sending ENTER to select menu item")
            serial_engine.run_cmd(cls.ENTER, '.*', timeout=cls.PEXPECT_TIMEOUT, send_without_enter=True)

    @classmethod
    def wait_for_grub_menu(cls, serial_engine):
        """
        Wait for the ONIE installer GRUB menu to appear after PXE boot.

        :param serial_engine: the serial engine connected to the DUT
        :raises pexpect.TIMEOUT: if the GRUB menu does not appear in time
        """
        logger.info(f"Waiting for prompt to see 'ONIE: Install OS'")
        serial_engine.run_cmd('', ['ONIE: Install OS'], timeout=240, send_without_enter=True)

    @classmethod
    def wait_for_onie_success_msg(cls, serial_engine) -> str:
        """
        Wait for the ONIE installer GRUB menu to appear after PXE boot and return the ONIE version.

        :param serial_engine: the serial engine connected to the DUT
        :return: The ONIE version string
        :raises pexpect.TIMEOUT: if the GRUB menu does not appear in time
        """
        logger.info(f"Waiting for prompt to see 'ONIE: Success'")
        output, _ = serial_engine.run_cmd('', ['ONIE: Success'], timeout=240, send_without_enter=True)

        # Extract ONIE version using regex
        version_match = re.search(r'ONIE: Version\s+:\s+([^\n]+)', output)
        if version_match:
            onie_version = version_match.group(1).strip()
            logger.info(f"Found ONIE version: {onie_version}")
            return onie_version
        else:
            logger.warning("Could not find ONIE version in output")
            return None
