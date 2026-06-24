import json
import logging
from typing import Dict

from retry import retry

from infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_constants.constants_nvos import DatabaseConst
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.SecureBootTool import SecureBootTool
from ngts.nvos_tools.infra.CurlTool import CurlTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.nvos_tools.infra.TpmTool import TpmTool
from ngts.tests_nvos.constants import PRODUCTION, DEVELOPMENT
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import ADMIN
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import BMC_USER_BACKUP_PASSWORD, ROOT
from ngts.nvos_constants.constants_nvos import OpenApiReqType
from ngts.nvos_constants.constants_nvos import FansConsts, PlatformConsts, SystemConsts
from ngts.nvos_tools.infra.NvCommand import NvCommand

logger = logging.getLogger()


class BmcTool:
    BMC_LOCAL_IP = "10.0.1.1"
    BASE_REDFISH_URL = "/redfish/v1/"
    BASE_URL = "https://" + BMC_LOCAL_IP + BASE_REDFISH_URL
    USER_NAME = ADMIN
    PLATFORM_COMPONENTS_DICT = dict()
    FW_VERSIONS_JSON_FILE = None

    @classmethod
    def set_fw_versions_json_file(cls, path):
        cls.FW_VERSIONS_JSON_FILE = path

    @staticmethod
    def _get_bmc_password(engine: LinuxSshEngine):
        return TpmTool(engine).get_bmc_admin_password_from_tpm()

    @staticmethod
    def reset(engine: LinuxSshEngine):
        password = BmcTool._get_bmc_password(engine)
        cmd = (f"""curl -k -u {BmcTool.USER_NAME}:{password} -H "Content-Type: application/json" -X POST """ +
               """-d '{"ResetType": "GracefulRestart"}' """ +
               f"""{BmcTool.BASE_URL}Managers/BMC_0/Actions/Manager.Reset""")
        response = engine.run_cmd(cmd)
        if "The request completed successfully" not in response:
            raise Exception("Shutdown command failed with the following response:\n" + response)

    @staticmethod
    def fetch_and_install_platform_component(platform_component, path, name, filename, topology_obj, test_name, skip_version_check=False) -> ResultObj:
        with allure.step(f'Fetch {name} image from: {path}'):
            platform_component.action_fetch(path).verify_result()

        with allure.step(f'installing image {name}'):
            return BmcTool.install_fw_image(platform_component, test_name, filename, topology_obj, name, skip_version_check)

    @staticmethod
    def fetch_and_install_platform_component_without_reboot(platform_component, path, name, filename, test_name, skip_version_check=False) -> ResultObj:
        with allure.step(f'Fetch {name} image from: {path}'):
            platform_component.action_fetch(path).verify_result()

        with allure.step(f'installing image {name}'):
            return BmcTool.install_fw_image_without_reboot(platform_component, test_name, filename, skip_version_check)

    @staticmethod
    def verify_platform_component_version(platform_component, expected_version: str):
        with allure.step(f'Making sure component is now on version {expected_version}'):
            platform_component_output = OutputParsingTool.parse_json_str_to_dictionary(
                platform_component.show()).verify_result()
            output_version = platform_component_output[PlatformConsts.FW_ACTUAL]
            logger.info(f"Found version: {output_version}")

            assert output_version == expected_version, \
                f"firmware is {output_version}, expected {expected_version} after the install"

    @staticmethod
    def verify_cpld_versions(image_details: Dict[str, Dict[str, str]]):
        firmware_shown = OutputParsingTool.parse_json_str_to_dictionary(Platform().firmware.show()).get_returned_value()
        with allure.step('validate cpld firmware versions'):
            for cpld_number, expected_version in image_details.items():
                with allure.independent_step(f"Checking {cpld_number}"):
                    actual_firmware = firmware_shown[cpld_number][PlatformConsts.FW_ACTUAL]
                    assert actual_firmware == expected_version, f"{cpld_number} version mismatch: Expected '{expected_version}', Got '{actual_firmware}'"

    @staticmethod
    def compare_bmc_version_issu_module(engines, expected_version: str):
        with allure.step(f'Making sure component is now on version {expected_version}'):
            bmc_output = Tools.DatabaseTool.sonic_db_cli_hgetall(engine=engines.dut, asic="",
                                                                 db_name=DatabaseConst.STATE_DB_NAME,
                                                                 table_name='\"SYSTEM_COMPONENTS|BMC\"')

            assert bmc_output == expected_version, \
                f"firmware is {bmc_output}, expected {expected_version} after the install"

    @staticmethod
    def _get_fw_component_version_info(component_name, version):
        fw_path = BmcTool.FW_VERSIONS_JSON_FILE
        if not fw_path:
            logger.info("FW_VERSIONS_JSON_FILE is not set")
            return None, None, None
        with allure.step(f'Read platform components info from json {fw_path}'):
            if not BmcTool.PLATFORM_COMPONENTS_DICT:
                with open(fw_path, 'r') as file:
                    BmcTool.PLATFORM_COMPONENTS_DICT = json.load(file)
            platform_components_dict = BmcTool.PLATFORM_COMPONENTS_DICT
            provisioning = DEVELOPMENT if SecureBootTool.is_dev_system(TestToolkit.get_engine()) else PRODUCTION
            if component_name not in platform_components_dict[provisioning].keys():
                logger.info(f"Component {component_name} not found in json")
                return None, None, None
            component_image_info = platform_components_dict[provisioning][component_name][version]
            path = component_image_info['path']
            # Derive filename from path if not explicitly provided (e.g., for CPLD components)
            filename = component_image_info.get('filename') or path.rsplit('/', 1)[-1]
            return path, filename, component_image_info['version_name']

    @staticmethod
    def get_fw_component_version_dict(component_name, version):
        device = TestToolkit.get_device()
        fw_path = BmcTool.FW_VERSIONS_JSON_FILE or device.fw_versions_json_file_path
        with allure.step(f'Read platform components info from json {fw_path}'):
            if not BmcTool.PLATFORM_COMPONENTS_DICT:
                with open(fw_path, 'r') as file:
                    BmcTool.PLATFORM_COMPONENTS_DICT = json.load(file)
            platform_components_dict = BmcTool.PLATFORM_COMPONENTS_DICT
            provisioning = DEVELOPMENT if SecureBootTool.is_dev_system(TestToolkit.get_engine()) else PRODUCTION
            component_image_info = platform_components_dict[provisioning][component_name][version]
            return component_image_info

    @staticmethod
    def is_automatic_background_copy_enabled(engine, erot_name: str) -> bool:
        """
        Check if AutomaticBackgroundCopyEnabled is true for the given EROT component.

        Uses Redfish API to query the EROT chassis and check the Oem.Nvidia.AutomaticBackgroundCopyEnabled field.

        :param engine: Engine object to execute commands
        :param erot_name: EROT name (e.g., 'EROT-BMC', 'EROT-CPU')
        :return: True if automatic background copy is enabled, False otherwise
        """
        # Convert EROT name to Redfish chassis name (e.g., 'EROT-BMC' -> 'MGX_ERoT_BMC_0')
        # The format is: EROT-XXX -> MGX_ERoT_XXX_0
        erot_suffix = erot_name.replace('EROT-', '')
        redfish_chassis_name = f"MGX_ERoT_{erot_suffix}_0"
        chassis_path = f"/Chassis/{redfish_chassis_name}"

        with allure.step(f"Checking if AutomaticBackgroundCopyEnabled is true for {erot_name}"):
            password = BmcTool._get_bmc_password(engine)
            curl_tool = CurlTool(server_host=BmcTool.BMC_LOCAL_IP, username=BmcTool.USER_NAME, password=password)
            response = curl_tool.run_redfish_command(rest_op='GET', path=chassis_path, dut_engine=engine)

            try:
                response_dict = json.loads(response)
                auto_bg_copy_enabled = response_dict.get("Oem", {}).get("Nvidia", {}).get("AutomaticBackgroundCopyEnabled", True)
                logger.info(f"AutomaticBackgroundCopyEnabled for {erot_name}: {auto_bg_copy_enabled}")
                return auto_bg_copy_enabled
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to parse Redfish response for {erot_name}: {e}. Assuming background copy is enabled.")
                return True

    @staticmethod
    @retry(AssertionError, tries=14, delay=30)
    def verify_background_copy_completed(platform, erot_name):
        with allure.step(f"Verifying {erot_name} completed background copy"):
            firmware_shown: Dict[str, str] = OutputParsingTool.parse_json_str_to_dictionary(
                platform.firmware.erot_id[erot_name].show()).get_returned_value()
            background_copy_status = firmware_shown[PlatformConsts.FW_BACKGROUND_COPY_STATUS]
            assert background_copy_status.lower() == "Completed".lower(), "Background copy status is not completed"

    @staticmethod
    def verify_background_copy_completed_if_enabled(engine, platform, erot_name):
        """
        Verify background copy is completed only if AutomaticBackgroundCopyEnabled is true.

        If automatic background copy is disabled, this method skips the verification and logs a message.

        :param engine: Engine object to execute commands for Redfish API calls
        :param platform: Platform object for NVUE show commands
        :param erot_name: EROT name (e.g., 'EROT-BMC', 'EROT-CPU')
        """
        if not BmcTool.is_automatic_background_copy_enabled(engine, erot_name):
            with allure.step(f"Skipping background copy verification - AutomaticBackgroundCopyEnabled is false for {erot_name}"):
                logger.info(f"AutomaticBackgroundCopyEnabled is false for {erot_name}, skipping background copy verification")
                return

        BmcTool.verify_background_copy_completed(platform, erot_name)

    @staticmethod
    @retry(AssertionError, tries=3, delay=15)
    def wait_for_cpu_to_detect_bmc(dut_engine: LinuxSshEngine, nv_command: NvCommand):
        bmc_inventory: dict = OutputParsingTool.parse_json_str_to_dictionary(
            nv_command.platform.inventory.show("BMC")).get_returned_value()
        assert bmc_inventory.get(SystemConsts.STATE) == FansConsts.STATE_OK, "BMC is not detected by CPU"

    @staticmethod
    def get_fw_component_version_latest(component_name):
        return BmcTool._get_fw_component_version_info(component_name, "latest")

    @staticmethod
    def get_fw_component_version_previous(component_name):
        return BmcTool._get_fw_component_version_info(component_name, "previous")

    @staticmethod
    def get_bmc_ip_addresses(engines, topology_obj) -> Dict[str, str]:
        """
        get ipv4 using noga and ipv6 using curl -k -u <user>>:<password> https://<bmc_ip>/redfish/v1/Managers/BMC_0/EthernetInterfaces/eth0
        :return: {"IPv4": ... , "IPv6": ...}
        """
        ip_addresses = {}
        with allure.step("Get bmc ipv4 from noga"):
            bmc_ipv4_address = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific'][
                'bmc_ip']
            assert bmc_ipv4_address, "Could you please check the BMC IP box in NOGA? It appears to be empty"
            logger.info(f"the bmc IPv4 is {bmc_ipv4_address}")
            ip_addresses["IPv4"] = bmc_ipv4_address

        with allure.step("Sending a curl request to get the IPv6 address from the BMC"):
            curl_request = f'curl -s -k -u {BmcTool.USER_NAME}:{BmcTool._get_bmc_password(engines.dut)} https://{bmc_ipv4_address}/redfish/v1/Managers/BMC_0/EthernetInterfaces/eth0 | python3 -m json.tool'
            eth0_details = OutputParsingTool.parse_json_str_to_dictionary(
                engines.dut.run_cmd(curl_request)).verify_result()
            ipv6_data = eth0_details["IPv6Addresses"]
            slaac_address = next(
                (address['Address'] for address in ipv6_data if (address['AddressOrigin'] == 'SLAAC') or (address['AddressOrigin'] == 'DHCPv6')),
                None  # If no SLAAC address is found, return None
            )
            ip_addresses["IPv6"] = slaac_address
            return ip_addresses

    @staticmethod
    def install_fw_image(platform_component, test_name, filename, topology_obj, name, skip_version_check=False) -> ResultObj:
        component_name = platform_component.get_resource_basename().lower()
        res_obj, duration = OperationTime.save_duration(f'{component_name} install with reboot', '',
                                                        test_name,
                                                        platform_component.files.file_name[
                                                            filename].action_file_install_with_reboot,
                                                        topology_obj=topology_obj, skip_version_check=skip_version_check)
        return res_obj

    @staticmethod
    def install_fw_image_without_reboot(platform_component, test_name, filename, skip_version_check=False) -> ResultObj:
        component_name = platform_component.get_resource_basename().lower()
        res_obj, duration = OperationTime.save_duration(f'{component_name} install without reboot', '',
                                                        test_name,
                                                        platform_component.files.file_name[filename].action_file_install,
                                                        force=False, skip_version_check=skip_version_check)
        return res_obj

    @staticmethod
    def _build_curl_base(method, bmc_ip_address, component_path):
        """Build base curl command with authentication and URL."""
        if IpTool.is_address_ipv6(bmc_ip_address):
            bmc_ip_address = f'[{bmc_ip_address}]'
        # Quote credentials so shell does not interpret '#' or other special chars (e.g. over SSH)
        creds = f"{ROOT}:{BMC_USER_BACKUP_PASSWORD}"
        if "'" in creds:
            creds_quoted = "'" + creds.replace("'", "'\"'\"'") + "'"
        else:
            creds_quoted = "'" + creds + "'"
        return (
            f"curl -s -k -u {creds_quoted} "
            f"https://{bmc_ip_address}{BmcTool.BASE_REDFISH_URL}{component_path} -X {method}"
        )

    @staticmethod
    def send_get_request(engine, bmc_ip_address, component_path) -> ResultObj:
        """
        Send a GET request to BMC Redfish API.

        :param engine: Engine object to execute commands
        :param bmc_ip_address: BMC IP address
        :param component_path: Redfish API endpoint path
        :return: ResultObj with response or error
        """

        with allure.step(f"Send GET request to {bmc_ip_address}"):
            curl_get_cmd = BmcTool._build_curl_base(OpenApiReqType.GET, bmc_ip_address, component_path)
            curl_get_output = engine.run_cmd(curl_get_cmd + ' --fail && echo')

            if not curl_get_output:
                return ResultObj(False, "Received empty response from BMC")

            return ResultObj(True, "", curl_get_output)

    @staticmethod
    def send_patch_request(engine, bmc_ip_address, component_path, new_values, expected_value="") -> ResultObj:
        """
        Send a PATCH request to BMC Redfish API.

        :param engine: Engine object to execute commands
        :param bmc_ip_address: BMC IP address
        :param component_path: Redfish API endpoint path
        :param new_values: Dictionary of values to update
        :param expected_value: Expected value to verify in response
        :return: ResultObj with response or error
        """
        with allure.step(f"Send PATCH request to {bmc_ip_address}"):

            json_data = json.dumps(new_values)
            curl_path_cmd = (
                f"{BmcTool._build_curl_base(OpenApiReqType.PATCH, bmc_ip_address, component_path)} "
                f"-H 'Content-Type: application/json' -d '{json_data}' --fail 2>&1; echo \"CURL_EXIT=$?\""
            )
            curl_patch_output = engine.run_cmd(curl_path_cmd, validate=False)

            if "CURL_EXIT=0" not in curl_patch_output:
                return ResultObj(
                    False,
                    f"PATCH failed (exit code or curl error). Output: {curl_patch_output}"
                )

            if expected_value and expected_value not in curl_patch_output:
                return ResultObj(
                    False,
                    f"Expected value '{expected_value}' not found in response: {curl_patch_output}"
                )

            response_output = curl_patch_output.split("CURL_EXIT=")[0].rstrip()
            return ResultObj(True, "", response_output)
