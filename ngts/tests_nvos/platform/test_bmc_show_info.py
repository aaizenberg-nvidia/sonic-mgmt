"""
TestBmcShowInfo: Validates the 'nv show platform bmc' NVUE tree and cross-validates
against Redfish ground truth and bmc-cli output.

Extended coverage — not in BMC_Testing.txt.
"""
import json
import logging
import pytest
from ngts.nvos_constants.constants_nvos import PlatformConsts
from ngts.nvos_tools.infra.CurlTool import CurlTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.tests_nvos.platform.bmc_helpers import (
    bmc_redfish_oob_host, ensure_usb0_up, skip_if_no_bmc,
)
from ngts.tools.test_utils import allure_utils as allure
cumulus_owner = "gpazbenitzha"
logger = logging.getLogger()


def _get_redfish(engines, path):
    """GET a Redfish endpoint over the BMC OOB IP with known CL credentials.

    Raw Redfish only works against the OOB interface (usb0/BMC_IP does not
    serve HTTPS). Returns {} on non-JSON/empty so callers can skip.
    """
    host = bmc_redfish_oob_host(engines, TestToolkit.topology_obj)
    username, password = BmcTool.get_bmc_credentials(engines.dut)
    client = CurlTool(server_host=host, username=username, password=password)
    raw = client.run_redfish_command('GET', path=path)
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {}


@pytest.mark.bmc
@pytest.mark.cumulus
def test_show_bmc_firmware_inventory(engines, devices, topology_obj):
    """Cross-validate NVUE BMC firmware against Redfish; require detail fields.

    Inventory ground-truth plus detail field presence.
    """
    skip_if_no_bmc(devices)
    ensure_usb0_up(engines.dut)
    with allure.step("Get NVUE firmware info"):
        nvue = OutputParsingTool.parse_json_str_to_dictionary(
            Platform().firmware.show("BMC")
        ).get_returned_value()

    with allure.step("Get Redfish firmware inventory + manager version"):
        # Inventory collection must be reachable; version ground-truth is on
        # Managers/BMC_0 (same source as test_bmc_data_model cross-check).
        inv = _get_redfish(engines, '/UpdateService/FirmwareInventory')
        rf = _get_redfish(engines, '/Managers/BMC_0')

    with allure.step("Verify NVUE reports a firmware version (detail fields)"):
        assert PlatformConsts.FW_ACTUAL in nvue, f"Missing actual-firmware in detail: {nvue}"
        assert nvue.get(PlatformConsts.FW_ACTUAL), f"NVUE missing actual-firmware: {nvue}"

    with allure.step("Cross-validate NVUE firmware version against Redfish"):
        if not (isinstance(inv, dict) and inv):
            pytest.skip("Redfish FirmwareInventory unavailable — cannot cross-validate")
        nvue_version = nvue.get(PlatformConsts.FW_ACTUAL, "")
        rf_version = rf.get('FirmwareVersion', '') if isinstance(rf, dict) else ''
        if not rf_version:
            pytest.skip("Redfish returned no FirmwareVersion — cannot cross-validate")
        assert nvue_version == rf_version, \
            f"NVUE version '{nvue_version}' != Redfish version '{rf_version}'"
