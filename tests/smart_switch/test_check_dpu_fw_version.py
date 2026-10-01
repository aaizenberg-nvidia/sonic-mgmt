import logging
import pytest
import re

from tests.common.plugins.allure_wrapper import allure_step_wrapper as allure
from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.sonic_image_readme import resolve_sonic_image_readme_path

pytestmark = [
    pytest.mark.topology('smartswitch'),
    pytest.mark.skip_check_dut_health
]

logger = logging.getLogger(__name__)


def test_check_dpu_fw_version(dpuhosts, localhost):
    with allure.step("Get the expected fw version"):
        sonic_version_output = dpuhosts[0].shell("show ver | grep '^SONiC Software Version:'")['stdout']
        # SONiC.master_RC.43-878aeef52_Internal
        sonic_image_name = sonic_version_output.split(":", 1)[1].strip()
        pytest_assert(sonic_image_name.startswith("SONiC."), f'SONiC image name must start with "SONiC." Got: {sonic_version_output}')

        # SONiC.master_RC.43-878aeef52_Internal -> master_RC.43-878aeef52_Internal
        sonic_image_directory = sonic_image_name[6:]
        # master_RC.43-878aeef52_Internal -> 43-878aeef52_Internal
        sonic_image_branch, sonic_image_version = sonic_image_directory.split(".", 1)
        readme_path = resolve_sonic_image_readme_path(sonic_image_directory)
        logger.debug(f"Sonic image branch: {sonic_image_branch}, version: {sonic_image_version}")
        logger.debug(f"Resolved README path: {readme_path}")
        if not readme_path:
            # Classic CI images version as 0 after the branch name and have no NFS README
            if sonic_image_version.startswith("0-"):
                pytest.skip("README is not available in the NFS for the CI runs")
            else:
                pytest.fail(
                    f"README /auto/sw_system_release/sonic/{sonic_image_directory}/dev/README is not found"
                )

        fw_version_output = localhost.shell(f"grep FW_VERSION_DPU '{readme_path}'")['stdout']
        fw_version_pattern = r'\d{2}\.\d{4}'
        expected_fw_version = re.search(fw_version_pattern, fw_version_output).group()
    with allure.step("Check fw versions of all the DPUs"):
        for dpuhost in dpuhosts:
            fw_version_output = dpuhost.shell('sudo mlxfwmanager | grep FW')['stdout']
            fw_version = re.search(fw_version_pattern, fw_version_output).group()
            pytest_assert(fw_version == expected_fw_version,
                          f"The fw version of {dpuhost.hostname}: {fw_version} is not "
                          f"as expected: {expected_fw_version}")
