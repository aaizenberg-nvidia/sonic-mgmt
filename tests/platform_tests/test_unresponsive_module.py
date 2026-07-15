import logging
import re

import pytest

from tests.common.reboot import reboot
from tests.common.plugins.allure_wrapper import allure_step_wrapper as allure
from tests.transceiver.utils.cli_parser_helper import parse_eeprom
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from tests.common.platform.daemon_utils import check_pmon_daemon_status
from tests.common.platform.processes_utils import check_critical_processes
from tests.common.platform.interface_utils import get_dut_interfaces_status, get_physical_index_to_ports_map

logger = logging.getLogger(__name__)

# this firmware is from "http://nbu-mtr-nfs.nvidia.com//auto/fwshared_release/linkx/mlnx_linkx_module_aoc_fw/44_Wolverine2_arcus2/099_90_release/rel-44_099_90001/signed/dev/fw_44_099_90001_dev_signed_WOLVERINE2-ARCUS2_DK.bin"
DEBUG_FW_DIR = "/auto/sw_regression/system/SONIC/MARS/debug_fw"
DEBUG_FW_MAPPING = {
    "980-9IAU9-00XM0M": "fw_44_099_90001_dev_signed_WOLVERINE2-ARCUS2_DK.bin",
    "980-9IAU9-00XM0H": "fw_44_099_90001_dev_signed_WOLVERINE2-ARCUS2_DK.bin",
}
DEBUG_FW_ON_DUT = "/tmp/debug.bin"
CMD_SFP_EEPROM = "show interfaces transceiver info"
# ──────────────────────────────────────────────────────────────────────
# unresponsive module verification, will not sync to community
# ──────────────────────────────────────────────────────────────────────

pytestmark = [
    pytest.mark.disable_loganalyzer
]


def copy_debug_firmware_to_dut(duthost, module_info):
    fw_name = DEBUG_FW_MAPPING[module_info["vendor_pn"]]
    fw_path = f"{DEBUG_FW_DIR}/{fw_name}"
    result = duthost.copy(src=fw_path, dest=DEBUG_FW_ON_DUT)
    pytest_assert(
        not result.get("failed", False),
        f"Copy debug FW failed for port {module_info['port']}: {result.get('msg', 'unknown error')}"
    )


def get_module_active_firmware_version(duthost, module_port):
    result = duthost.command(f"sudo sfputil show fwversion {module_port}")
    match = re.search(r"Active Firmware\s*:\s*(.+)", result.get("stdout"))
    pytest_assert(
        match,
        f"Active Firmware not found in 'sfputil show fwversion' output for {module_port}. "
        f"stderr: {result.get('stderr', '').strip()}"
    )
    return match.group(1).strip()


def upgrade_module_firmware(duthost, module_port):
    active_fw = get_module_active_firmware_version(duthost, module_port)
    duthost.command(f"sudo sfputil firmware download {module_port} {DEBUG_FW_ON_DUT}")
    # this command is used to switch between the active and inactive firmware
    duthost.command(f"sudo sfputil firmware run {module_port}")
    # commit is necessary so that the firmware will not switch back
    duthost.command(f"sudo sfputil firmware commit {module_port}")

    def _fw_changed():
        return get_module_active_firmware_version(duthost, module_port) != active_fw

    pytest_assert(
        wait_until(20, 2, 0, _fw_changed),
        f"Active FW should change after upgrade, still {active_fw}"
    )
    logger.info(f"Active FW: before upgrade: {active_fw}, after upgrade: {get_module_active_firmware_version(duthost, module_port)}")


def set_module_unresponsive_state(duthost, module_port):
    # be careful, this will make the module unresponsive, need a reboot to restore
    # refer to https://nvidia.atlassian.net/wiki/spaces/SW/pages/3034252977/Pluggable+FW+I2C+debug+Features+list+and+User+interface
    duthost.command(f"sudo sfputil write-eeprom -p {module_port} -d 01 -o 83 -n 0x00")


def verify_module_unresponsive(duthost, module_port):
    def _unresponsive():
        result = duthost.command(f"sudo sfputil show fwversion {module_port}", module_ignore_errors=True)
        return "eeprom not detected" in result.get('stdout').lower()

    pytest_assert(
        wait_until(5, 1, 0, _unresponsive),
        f"Module {module_port} did not become unresponsive"
    )
    logger.info("Module %s is unresponsive as expected", module_port)


def get_related_split_ports(duthost, module_port):
    """Return all logical ports sharing the same physical module as module_port."""
    for ports in get_physical_index_to_ports_map(duthost).values():
        if module_port in ports:
            return ports
    raise ValueError(f"{module_port} not found in physical index to ports map")


def verify_system_health(duthost, interfaces, module_port):
    # Related split ports (e.g. Ethernet0-7) go down together when the module is unresponsive.
    skip_ports = set(get_related_split_ports(duthost, module_port))
    ports_to_check = [port for port in interfaces if port not in skip_ports]
    logger.info(f"Excluding related split ports from health check: {skip_ports}")

    check_critical_processes(duthost, watch_secs=120)

    def _ports_up(duthost, ports):
        status = get_dut_interfaces_status(duthost)
        for port in ports:
            port_status = status.get(port, {})
            if port_status.get("admin") == "down":
                continue
            if port_status.get("oper") == "down":
                return False
        return True

    pytest_assert(
        wait_until(60, 5, 0, _ports_up, duthost, ports_to_check),
        f"Some interfaces are down after excluding ports {skip_ports}"
    )
    pytest_assert(check_pmon_daemon_status(duthost), "Not all PMON daemons are running")


@pytest.fixture
def module_info(duthost):
    """Select a module that has debug firmware mapping."""
    result = duthost.command(CMD_SFP_EEPROM)
    stdout_lines = result.get("stdout_lines", [])
    pytest_assert(stdout_lines, "CLI returned empty output")

    for port, attrs in parse_eeprom(stdout_lines).items():
        vendor_pn = attrs.get("Vendor PN", "")
        if vendor_pn in DEBUG_FW_MAPPING and attrs.get("Active Firmware"):
            yield {"port": port, "vendor_pn": vendor_pn}
            return

    pytest.skip("No target module with debug firmware mapping found")


@pytest.fixture
def restore_module_and_dut(duthost, localhost, module_info):
    """Restore module FW version and DUT state after test."""
    port = module_info["port"]
    active_fw = get_module_active_firmware_version(duthost, port)
    logger.info("Module FW version before test: %s, port: %s", active_fw, port)
    yield
    with allure.step("Restore module fw version and DUT state"):
        current_fw = get_module_active_firmware_version(duthost, port)
        logger.info("Module FW version after test: %s, port: %s", current_fw, port)
        # reboot the DUT so that the fw version can be switched back
        reboot(duthost, localhost, reboot_type="cold")
        if current_fw != active_fw:
            duthost.command(f"sudo sfputil firmware run {port}")
            duthost.command(f"sudo sfputil firmware commit {port}")
            pytest_assert(
                get_module_active_firmware_version(duthost, port) == active_fw,
                "Module FW version should be the same after restore"
            )
        # reset the module to make it oper up
        duthost.command(f"sudo sfputil reset {port}")


def test_unresponsive_module(duthost, module_info, restore_module_and_dut, conn_graph_facts):
    """
    Verify that when a module is unresponsive, the system still works as expected, and no crash detected.
    """
    port = module_info["port"]
    logger.info("Module info: %s", module_info)

    with allure.step("Install debug FW and inject unresponsive state"):
        copy_debug_firmware_to_dut(duthost, module_info)
        upgrade_module_firmware(duthost, port)
        set_module_unresponsive_state(duthost, port)

    with allure.step("Verify module is unresponsive"):
        verify_module_unresponsive(duthost, port)

    with allure.step("Check the system is still working"):
        interfaces = conn_graph_facts.get("device_conn", {}).get(duthost.hostname, {})
        verify_system_health(duthost, interfaces, port)
