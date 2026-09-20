import pytest

from ngts.ngts_types import DevicesT
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.tests_nvos.helpers.platform import platform_uphy_helpers as uphy_helpers


@pytest.mark.fae
@pytest.mark.platform
def test_platform_uphy_version(random_api: ApiType, devices: DevicesT):
    uphy_helpers.verify_uphy_cmd(uphy_helpers.fae_uphy_cmd, devices.dut.asic_numbers)
