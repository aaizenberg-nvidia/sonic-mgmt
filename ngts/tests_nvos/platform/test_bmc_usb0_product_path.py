"""Assert CL product path brings up usb0 for BMC without test-side configuration."""
import logging

import pytest

from ngts.nvos_tools.Devices.EthDevice import EthSwitch
from ngts.nvos_tools.infra.BmcTool import assert_usb0_bmc_link_addrs
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.platform.bmc_helpers import skip_if_no_bmc

cumulus_owner = "gpazbenitzha"
logger = logging.getLogger()


@pytest.mark.bmc
@pytest.mark.cumulus
def test_usb0_product_path_without_helper(engines, devices):
    """usb0 must have the product CPU address and reach BMC without ensure_usb0_up / nv config.

    Product path: /etc/network/interfaces.d/bmc-usb.intf + hw-management udev ifup.
    This test must not call ensure_usb0_up or touch NVUE usb0 config;
    assert_usb0_bmc_link_addrs only reads state, it never configures usb0.
    """
    # Pass devices only — skip_if_no_bmc(engines=...) would call ensure_usb0_up.
    skip_if_no_bmc(devices)
    if not isinstance(TestToolkit.get_device(), EthSwitch):
        pytest.skip("usb0 product-path assert is CL-only")

    assert_usb0_bmc_link_addrs(engines.dut)
