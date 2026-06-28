import logging
import time

import pytest

from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.acl.test_acl_basic import test_show_acls
from ngts.tests_nvos.checklist.test_checklist_ipv6 import test_checklist_ipv6
from ngts.tests_nvos.general.security.radius.constants import RadiusConsts
from ngts.tests_nvos.general.security.tacacs.constants import TacacsConsts
from ngts.tests_nvos.general.security.test_aaa_ldap.constants import LdapConsts
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
@pytest.mark.nvos_ci
def test_ci_sanity_openapi(engines, topology_obj, devices):
    with allure.step("Test OpenApi"):
        TestToolkit.tested_api = ApiType.OPENAPI
        platform = Platform()
        platform.show()


@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
@pytest.mark.nvos_ci
def test_ci_sanity_acl(engines, topology_obj, devices):
    TestToolkit.tested_api = ApiType.NVUE

    with allure.step("Test ACL"):
        test_show_acls(engines, devices, ApiType.NVUE)


@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
def test_ci_sanity_ipv6(engines, topology_obj, devices):
    with allure.step("Test IPV6"):
        test_checklist_ipv6(engines, topology_obj)


@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
@pytest.mark.nvos_ci
def test_ci_sanity_set(engines, topology_obj, devices):
    with allure.step("Test 'set' command"):
        system = System()
        new_pre_login_msg = "test_msg"
        system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value=f'"{new_pre_login_msg}"',
                           apply=True, dut_engine=engines.dut).verify_result()
        time.sleep(3)
        message_output = OutputParsingTool.parse_json_str_to_dictionary(system.message.show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(message_output, SystemConsts.PRE_LOGIN_MESSAGE,
                                                    new_pre_login_msg).verify_result()
        system.message.unset(op_param=SystemConsts.PRE_LOGIN_MESSAGE, apply=True)


@pytest.mark.simx
@pytest.mark.nvos_ci
def test_ci_sanity_fetch(engines, topology_obj, devices):
    with allure.step("Test action fetch"):
        if not getattr(devices.dut, 'nvl_trunk_ports_list', None):
            pytest.skip("No trunk ports available")

        platform = Platform()
        new_fw_file = "sec_issu_46_120_10011_dev_signed.bin"
        fw_path = f"{SystemConsts.GENERAL_TRANSCEIVER_FIRMWARE_FILES}/{new_fw_file}"
        player_engine = engines['sonic_mgmt']
        scp_path = 'scp://{}:{}@{}'.format(player_engine.username, player_engine.password, player_engine.ip)
        platform.firmware.transceiver.action_fetch(fw_path, base_url=scp_path).verify_result()
        with allure.step("Run the show command and verify that all expected files are correct"):
            platform.firmware.transceiver.files.verify_show_files_output(expected_files=[new_fw_file])


@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
@pytest.mark.nvos_ci
def test_ci_sanity_ldap(engines, topology_obj, devices):
    with allure.step("Check LDAP output"):
        system = System()
        output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.ldap.show()).get_returned_value()
        expected_field = LdapConsts.LDAP_SHOW_FIELDS
        ValidationTool.verify_field_exist_in_json_output(json_output=output,
                                                         keys_to_search_for=expected_field).verify_result()


@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
@pytest.mark.nvos_ci
def test_ci_sanity_tacacs(engines, topology_obj, devices):
    with allure.step("Check TACACS output"):
        system = System()
        output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.tacacs.show()).get_returned_value()
        expected_field = TacacsConsts.TACACS_FIELDS
        ValidationTool.verify_field_exist_in_json_output(json_output=output,
                                                         keys_to_search_for=expected_field).verify_result()


@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
@pytest.mark.nvos_ci
def test_ci_sanity_radius(engines, topology_obj, devices):
    with allure.step("Check RADIUS output"):
        system = System()
        output = OutputParsingTool.parse_json_str_to_dictionary(system.aaa.radius.show()).get_returned_value()
        expected_field = RadiusConsts.RADIUS_FIELDS
        ValidationTool.verify_field_exist_in_json_output(json_output=output,
                                                         keys_to_search_for=expected_field).verify_result()


#   @pytest.mark.simx
#   @pytest.mark.nvos_chipsim_ci
#   @pytest.mark.nvos_ci
#   def test_ci_sanity_health(engines, topology_obj, devices):
    #   with allure.step("Test Health"):
        #   system = System()
        #   health_output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
        #   ValidationTool.validate_all_values_exists_in_list([HealthConsts.STATUS, HealthConsts.STATUS_LED],
        #                                                     health_output.keys()).verify_result()
        #   system.validate_health_status(HealthConsts.OK)


@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
@pytest.mark.nvos_ci
def test_ci_sanity_snmp(engines, topology_obj, devices):
    with allure.step("Show SNMP"):
        system = System()
        system_snmp_output = OutputParsingTool.parse_json_str_to_dictionary(system.snmp_server.show()) \
            .get_returned_value()

        with allure.step("Verify default values"):
            ValidationTool.validate_fields_values_in_output(SystemConsts.SNMP_OUTPUT_FIELDS,
                                                            SystemConsts.SNMP_DEFAULT_VALUES,
                                                            system_snmp_output).verify_result()
            logging.info("All expected values were found")
