"""
SN11 — BMC reachability (one collect-all case).

ICMP ping usb0 IPv4, bmc-cli ping, and one usb0 Redfish GET.
The GET uses internal_redfish_get (scheme/port flags). Default HTTP :443,
no curl -k. HTTPS opt-in: --bmc-internal-redfish-scheme=https.
Do not dual-probe. Do not use CurlTool (OOB/SN49).
"""
import logging

import pytest

from ngts.nvos_tools.infra.BmcCliTool import BMC_IP, BmcCliTool
from ngts.nvos_tools.infra.BmcTool import assert_usb0_bmc_link_addrs
from ngts.tests_nvos.platform.bmc_helpers import (
    BMC_INTERNAL_REDFISH_ETH0_PATH,
    get_bmc_internal_redfish_settings,
    internal_redfish_get,
    skip_if_no_bmc,
)
from ngts.tools.test_utils import allure_utils as allure

cumulus_owner = "gpazbenitzha"
logger = logging.getLogger()


def _assert_no_login_failure(output, action):
    low = (output or "").lower()
    assert "login failed" not in low, (
        f"bmc-cli {action} login failed: {output[:300]}"
    )


@pytest.mark.bmc
@pytest.mark.platform
@pytest.mark.skip_clear_config
@pytest.mark.cumulus
def test_bmc_reachability(engines, devices, topology_obj):
    """TC-BMC-011: BMC reachable from NOS over usb0 (ICMP + bmc-cli + Redfish)."""
    skip_if_no_bmc(devices, engines)

    # Steady-state address check first (#5124733): the ping below only proves the
    # BMC answers, so without this a wrong host address or mask would surface as a
    # bare ping failure.
    assert_usb0_bmc_link_addrs(engines.dut)

    with allure.step(f"ICMP ping BMC usb0 IPv4 {BMC_IP}"):
        ping_out = engines.dut.run_cmd(
            f"ping -I usb0 -c 3 -W 2 {BMC_IP}", validate=False)
        assert "100% packet loss" not in ping_out, (
            f"ICMP ping {BMC_IP} via usb0 failed: {ping_out[:400]}"
        )
        assert "bytes from" in ping_out, (
            f"ICMP ping {BMC_IP} via usb0 had no replies: {ping_out[:400]}"
        )

    with allure.step("bmc-cli ping"):
        ping_cli = BmcCliTool(engines.dut).ping(timeout=15)
        _assert_no_login_failure(ping_cli, "ping")
        assert "reachable" in ping_cli.lower(), (
            f"bmc-cli ping did not report reachable: {ping_cli[:300]}"
        )

    scheme, port = get_bmc_internal_redfish_settings()
    with allure.step(
            f"usb0 Redfish GET EthernetInterfaces/eth0 "
            f"({scheme}://{BMC_IP}:{port})"):
        body = internal_redfish_get(
            engines.dut, path=BMC_INTERNAL_REDFISH_ETH0_PATH)
        logger.info("Redfish eth0 keys: %s", list(body.keys())[:12])
        assert "@odata.id" in body or "Id" in body, (
            f"Redfish eth0 body missing @odata.id/Id: {list(body.keys())}"
        )
        # Resource identity: eth0, not the service root.
        odata = str(body.get("@odata.id") or body.get("odata") or "")
        ident = str(body.get("Id", ""))
        assert "eth0" in odata.lower() or ident.lower() == "eth0", (
            f"Expected EthernetInterfaces/eth0, got odata={odata!r} Id={ident!r}"
        )
        logger.info(
            "SN11 usb0 GET used %s://%s:%s (HTTPS opt-in: "
            "--bmc-internal-redfish-scheme=https)",
            scheme, BMC_IP, port)
