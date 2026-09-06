import random

import pytest

from ngts.tests_nvos.checklist.erot_fw_upgrade.BaseFWUpgradeTest import BaseFWUpgradeTest
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.constants import MINUTE, FW_COMPONENT_EROT


@pytest.mark.timeout(25 * MINUTE, func_only=True)
@pytest.mark.erot
@pytest.mark.dut_reboot
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_erot_upgrade_all(engines, devices, topology_obj, test_api, test_name, show_platform_initial_state):
    """
    Test 'nv {show | fetch | install | delete} platform firmware EROT
    Bad BMC erot fw - hardware limitation, therefore removing 'ERoT_BMC_0' from install verification
    This function tests the process of fetching, installing, and verifying firmware images on a switch,
    The test includes steps for fetching previous and current firmware images, installing them,
    and verifying the installation on all relevant firmware components.


    Steps:
    1. Fetch the previous firmware image and verify its presence.
    2. Set the firmware source to custom.
    3. Install the previous firmware image and recover the device with a remote reboot.
    4. Verify the installation on all firmware components except 'ERoT_BMC_0'.
    5. Fetch the current firmware image and verify its presence.
    6. Install the current firmware image and recover the device with a remote reboot.
    7. Verify the installation on all firmware components except 'ERoT_BMC_0'.
    8. Finally, reset the firmware source to default and delete the firmware image files.

    """
    with allure.step('Create Test and system objects'):
        platform = Platform()
        test = BaseFWUpgradeTest(firmware_component=platform.firmware.erot_id[FW_COMPONENT_EROT.upper()])

    with allure.step(f"Fetch, install and assert prev & curr versions (through {test_api}) for {FW_COMPONENT_EROT}"):
        test.test(engines=engines, switch=devices.dut, topology_obj=topology_obj, test_api=test_api, test_name=test_name)


@pytest.mark.timeout(25 * MINUTE, func_only=True)
@pytest.mark.erot
@pytest.mark.dut_reboot
@pytest.mark.parametrize('test_api', random.sample(ApiType.ALL_TYPES, 1))
def test_erot_upgrade_fae(engines, devices, topology_obj, test_api, test_name, clear_erot_files, show_platform_initial_state):
    """
    Test 'nv {show | fetch | install | delete} fae platform firmware <EROT-Component>
    Bad BMC erot fw - hardware limitation, therefore removing 'ERoT_BMC_0' from install verification
    This function tests the process of fetching, installing, and verifying firmware images on a switch,
    The test includes steps for fetching previous and current firmware images, installing them,
    and verifying the installation on all relevant firmware components.


    Steps:
    1. Fetch the previous firmware image and verify its presence.
    2. Set the firmware source to custom.
    3. Install the previous firmware image and recover the device with a remote reboot.
    4. Verify the installation on all firmware components except 'ERoT_BMC_0'.
    5. Fetch the current firmware image and verify its presence.
    6. Install the current firmware image and recover the device with a remote reboot.
    7. Verify the installation on all firmware components except 'ERoT_BMC_0'.
    8. Finally, reset the firmware source to default and delete the firmware image files.

    """
    with allure.step('Create Test and system objects'):
        fae = Fae()
        erots_list = devices.dut.constants.erots
        erot_name = random.choice(erots_list)
        test = BaseFWUpgradeTest(firmware_component=fae.platform.firmware.erot_id[erot_name])

    with allure.step(f"Fetch, install and assert prev & curr versions (through {test_api})"):
        test.test_fae(engines=engines, switch=devices.dut, topology_obj=topology_obj, test_api=test_api)
