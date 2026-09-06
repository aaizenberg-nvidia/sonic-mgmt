import json
import logging
import pytest
import random
import time

from ngts.nvos_tools.infra.FWComponentsTool import FWComponentsTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tests_nvos.checklist.test_firmware_install_same_version import select_random_component, \
    install_same_firmware_version
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_constants.constants_nvos import PlatformConsts, OperationTimeConsts
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.tests_nvos.general.security.bmc.constants import BootPolicy
from ngts.nvos_tools.infra.ValidationTool import ValidationTool

logger = logging.getLogger()
BOOT_POLICY_REDFISH_API_ENDPOINT_PATH = "Systems/System_0/"


@pytest.mark.platform
@pytest.mark.bmc
@pytest.mark.timeout(MINUTE, func_only=True)
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_show_set_platform_boot_policy(engines, devices, topology_obj, output_format, test_api):
    """
    in this case we validate the platform boot policy settings by comparing nv command output and BMC GET request.

    Test Flow:
        - get BMC IP Addresses
        - Validate Default Values
        - Modify Boot Policy - new timeout
        - Validate values
        - Cleanup: resets the boot policy settings on the BMC to their default values after testing.

    :return:
    """
    TestToolkit.tested_api = test_api
    with allure.step("Create Platform object"):
        platform = Platform()

    with allure.step("get BMC ip address"):
        ip_addresses = BmcTool.get_bmc_ip_addresses(engines, topology_obj)
        assert ip_addresses["IPv6"], "BMC IPv6 address is not found"
        get_ip_addresses = random.choice(list(ip_addresses.keys()))
        patch_ip_addresses = next(ip_add for ip_add in ip_addresses if ip_add != get_ip_addresses)

    try:
        with allure.step("Testing GET and PATCH"):
            with allure.independent_step("Run show command and send curl GET request to BMC and validate both have default "
                                         "values"):
                expected_output = {
                    BootPolicy.NVOS_CPU_POWER_POLICY: BootPolicy.NVOS_CPU_POWER_ALWAYS_ON,
                    BootPolicy.NVOS_CPU_POWER_TIMEOUT: 0
                }
                with allure.step("Test default values of nv show platform boot-policy"):
                    validate_show_and_bmc_outputs(engine=engines.dut, platform=platform, expected_output=expected_output,
                                                  bmc_ip_address=ip_addresses[get_ip_addresses], output_format=output_format)

            with allure.independent_step("Run different curl PATCH requests to BMC and validate GET and show command will "
                                         "have same expected values"):
                sec_delay = 300
                expected_output[BootPolicy.NVOS_CPU_POWER_TIMEOUT] = sec_delay

                with allure.step("Test nv show platform boot-policy after sending curl request to change the power policy"):
                    with allure.step("change power policy using BMC"):
                        new_values = {BootPolicy.POWER_ON_DELAY_SECONDS: sec_delay}
                        BmcTool.send_patch_request(engine=engines['sonic_mgmt'], bmc_ip_address=ip_addresses[patch_ip_addresses], component_path=BOOT_POLICY_REDFISH_API_ENDPOINT_PATH, new_values=new_values).verify_result()

                    validate_show_and_bmc_outputs(engine=engines.dut, platform=platform,
                                                  expected_output=expected_output,
                                                  bmc_ip_address=ip_addresses[patch_ip_addresses], output_format=output_format)
    finally:
        default_values = {BootPolicy.POWER_ON_DELAY_SECONDS: 0}
        BmcTool.send_patch_request(engine=engines['sonic_mgmt'], bmc_ip_address=ip_addresses[patch_ip_addresses],
                                   component_path=BOOT_POLICY_REDFISH_API_ENDPOINT_PATH,
                                   new_values=default_values).verify_result()


@pytest.mark.dut_reboot
@pytest.mark.platform
@pytest.mark.bmc
@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_system_boot_policy_after_firmware_upgrade(engines, devices, topology_obj, test_api, output_format, test_name):
    """
    This method tests the firmware upgrade process and ensures the switch is available after the upgrade

    Test Flow:
        - Set BMC Power Restore Policy to Always Off
        - Select Random Component for Testing
        - Install Same Firmware Version
        - Set BMC Power Restore Policy to Always On

    :return:
    """
    TestToolkit.tested_api = test_api

    with allure.step("Test default values of nv show platform boot-policy"):
        platform = Platform()
        show_output = OutputParsingTool.parse_show_output_to_dict(platform.boot_policy.show(output_format=output_format), output_format=output_format, field_name_dict=PlatformConsts.FW_FIELD_NAME_DICT).get_returned_value()
        ValidationTool.verify_field_value_in_output(output_dictionary=show_output, field_name=BootPolicy.NVOS_CPU_POWER_POLICY, expected_value=BootPolicy.NVOS_CPU_POWER_ALWAYS_ON).verify_result()

    try:
        with allure.step("Select a random component to test"):
            component = select_random_component(devices)
            platform_component = getattr(Platform().firmware, component)
        send_bmc_request(engines['sonic_mgmt'], topology_obj, new_values={BootPolicy.POWER_RESTORE_POLICY: BootPolicy.ALWAYS_OFF})
        with allure.step("Select a random component to test switch is available after upgrade"):
            with allure.step("Install same fw version while using 'skip-version-check' 'force' options"):
                path, filename, version_name = FWComponentsTool.get_fw_component_version_latest(component)
                BmcTool.fetch_and_install_platform_component(
                    platform_component=platform_component,
                    path=path, name=version_name, topology_obj=topology_obj,
                    filename=filename,
                    test_name=test_name,
                    skip_version_check=True).verify_result()

    finally:
        send_bmc_request(engines['sonic_mgmt'], topology_obj, new_values={BootPolicy.POWER_RESTORE_POLICY: BootPolicy.ALWAYS_ON})
        with allure.step(f"Delete fetched fw image files"):
            files = platform_component.files.get_files()
            platform_component.files.delete_files(files_to_delete=files)


def send_bmc_request(engine, topology_obj, new_values):
    """
    This function sends a PATCH request to the BMC to update specific configuration values
    :return:
    """
    with allure.step(f"Send BMC request with {new_values} as new values"):
        ip_addresses = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific']['bmc_ip']
        BmcTool.send_patch_request(engine=engine, bmc_ip_address=ip_addresses,
                                   component_path=BOOT_POLICY_REDFISH_API_ENDPOINT_PATH,
                                   new_values=new_values).verify_result()


def validate_show_and_bmc_outputs(engine, platform, expected_output, bmc_ip_address, output_format):
    """
        This function checks the consistency between the output of a system cmd and the values retrieved from the BMC.

    steps:
        - Retrieve Power Policy from BMC
        - Run System Show Command
        - Compare Values

    :return:
    """

    with allure.step(f"Validate the show output and BMC values both are: {expected_output}"):
        with allure.step("Get power policy from BMC"):
            curl_get_output = OutputParsingTool.parse_json_str_to_dictionary(BmcTool.send_get_request(engine, bmc_ip_address, BOOT_POLICY_REDFISH_API_ENDPOINT_PATH).verify_result()).verify_result()

        with allure.step("run show command"):
            show_output = OutputParsingTool.parse_show_output_to_dict(platform.boot_policy.show(output_format=output_format), output_format=output_format, field_name_dict=PlatformConsts.FW_FIELD_NAME_DICT).get_returned_value()

        with allure.step("compare both values"):
            power_delay = BootPolicy.POWER_ON_DELAY_SECONDS
            power_policy = BootPolicy.POWER_RESTORE_POLICY
            value_map = {
                BootPolicy.NVOS_CPU_POWER_ALWAYS_OFF: BootPolicy.ALWAYS_OFF,
                BootPolicy.NVOS_CPU_POWER_ALWAYS_ON: BootPolicy.ALWAYS_ON
            }
            expected_state = value_map.get(show_output[BootPolicy.NVOS_CPU_POWER_POLICY])
            actual_state = curl_get_output[power_policy]

            with allure.independent_step("validate state"):
                assert expected_state and actual_state == expected_state, f"Mismatch: show_output is {show_output[BootPolicy.NVOS_CPU_POWER_POLICY]}, curl output is {actual_state}"

            with allure.independent_step("validate timeout"):
                assert curl_get_output[power_delay] == int(show_output[BootPolicy.NVOS_CPU_POWER_TIMEOUT]), f"Mismatch: show_output is {show_output[BootPolicy.NVOS_CPU_POWER_TIMEOUT]}, curl output is {curl_get_output[power_delay]}"
