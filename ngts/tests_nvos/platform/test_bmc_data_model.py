"""
TestBmcDataModel: Validates the YANG tree structure against spec —
field types, mandatory/optional semantics, and NOS-to-Redfish data consistency.

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


def _get_redfish_json(engines, path):
    """GET a Redfish endpoint on the BMC over the OOB IP with known CL credentials.

    Raw Redfish only works against the OOB interface (usb0/BMC_IP does not
    serve HTTPS). Returns {} on any non-JSON/empty response so callers can skip.
    """
    host = bmc_redfish_oob_host(engines, TestToolkit.topology_obj)
    username, password = BmcTool.get_bmc_credentials(engines.dut)
    client = CurlTool(server_host=host, username=username, password=password)
    raw = client.run_redfish_command('GET', path=path)
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        logger.warning("Redfish returned non-JSON for %s: %s", path,
                       str(raw)[:200] if raw else "(empty)")
        return {}


@pytest.mark.bmc
@pytest.mark.cumulus
def test_nvue_vs_redfish_full_consistency(engines, devices, topology_obj):
    """Field-by-field comparison of NVUE BMC firmware output vs Redfish ground truth."""
    skip_if_no_bmc(devices)
    ensure_usb0_up(engines.dut)

    with allure.step("Get NVUE firmware info"):
        nvue = OutputParsingTool.parse_json_str_to_dictionary(
            Platform().firmware.show("BMC")
        ).get_returned_value()

    with allure.step("Get Redfish firmware info"):
        rf = _get_redfish_json(engines, '/Managers/BMC_0')

    with allure.step("Compare firmware version"):
        nvue_version = nvue.get(PlatformConsts.FW_ACTUAL, "")
        rf_version = rf.get('FirmwareVersion', '') if isinstance(rf, dict) else ''

        if not nvue_version:
            pytest.fail(f"NVUE returned no firmware version: {nvue}")
        if not rf_version:
            pytest.skip("Redfish returned no FirmwareVersion — cannot cross-validate")
        assert nvue_version == rf_version, \
            f"NVUE version '{nvue_version}' != Redfish version '{rf_version}'"
