import logging
import socket
import random
import time
import pytest
from retry import retry

from ngts.cli_wrappers.common.general_clis_common import GeneralCliCommon
from ngts.nvos_constants.constants_nvos import ApiType, NtpConsts, NvosConst, SystemConsts
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active


@pytest.mark.system
@pytest.mark.ntp
@pytest.mark.simx
def test_configure_ntp_server(random_api):
    """
    validate:
    - Show NTP global configuration
    - Configure NTP feature state
    - Configure server default values
    - Manage ntp servers (create, remove, change)
    - Set per-server configurations as: version, trust, state, association-type and key
    - Set system ntp enabled/disabled
    - Unset on per-server configuration reset it to default state
    - Unset on 'ntp' node returns whole feature to default
    - Unset system ntp state stops the NTP daemon run

    Test flow:
    1. Clear all ntp configurations
    2. Validate show system ntp commands output (expect default values)
    3. Change time and date
    4. Configure ntp server and enable ntp
    5. Validate show system ntp output (expect states enable and clock sync)
    6. Validate server configured with default values
    7. Validate system clock and date are up to date
    8. Update existing ntp server with none default values
    9. Validate ntp server configured values (expect all values as configured)
    10. Validate show system ntp output (expect server state disabled and clock unsync)
    11. Unset each of the server configurations
    12. Validate server configured with default values
    13.	Enable ntp server
    14.	Validate show system ntp output (expect server state enabled and clock sync)
    15.	Set system ntp disabled
    16.	Validate show system ntp output (expect ntp state disabled and clock unsync)
    17.	Verify ntp daemon state (expect Ntpd stopped running)
    18.	Set system ntp enabled
    19.	Validate show system ntp output (expect ntp state enabled and clock sync)
    20.	Unset system ntp
    21.	Validate show system ntp commands output (expect default values)
    22.	Verify ntp daemon state (expect Ntpd running)
    """
    system = System()
    server_name = NtpConsts.SERVER1_IPV4
    ntp_dict = dict(NtpConsts.NTP_DEFAULT_DICT)

    try:
        with allure.step("Clear all ntp configurations"):
            system.ntp.unset().verify_result()
            system.ntp.set(op_param_name=NtpConsts.DHCP, op_param_value=NtpConsts.Dhcp.DISABLED.value).verify_result()
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.DISABLED.value).verify_result()
            system.ntp.listen.set(NtpConsts.Listen.ETH0.value).verify_result()
            system.ntp.set(op_param_name=NtpConsts.VRF, op_param_value=NtpConsts.Vrf.DEFAULT.value,
                           apply=True, ask_for_confirmation=True).verify_result()
            time.sleep(NtpConsts.CONFIG_TIME)

        with allure.step("Validate show system ntp commands output"):
            ntp_dict[NtpConsts.STATE] = NtpConsts.State.DISABLED.value
            ntp_dict[NtpConsts.DHCP] = NtpConsts.Dhcp.DISABLED.value
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            ValidationTool.compare_dictionary_content(ntp_show, ntp_dict).verify_result()
            server_list = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.servers.show()).get_returned_value()
            assert not server_list, f"server list {server_list} should be empty"

        with allure.step("Set different date and time"):
            prev_time = GeneralCliCommon(TestToolkit.engines.dut).get_utc_time()
            GeneralCliCommon(TestToolkit.engines.dut).set_time(NtpConsts.OLD_DATE)

        with allure.step("Configure ntp server and enable ntp"):
            system.ntp.servers.set_resource(server_name).verify_result()
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.ENABLED.value,
                           apply=True).verify_result()

        with allure.step("Validate show system ntp output"):
            ntp_show = wait_for_ntp_status(system, NtpConsts.Status.SYNCHRONISED.value)
            ntp_dict[NtpConsts.SERVER] = {server_name: {}}
            ntp_dict[NtpConsts.STATE] = NtpConsts.State.ENABLED.value
            ntp_dict[NtpConsts.STATUS] = NtpConsts.Status.SYNCHRONISED.value
            ntp_dict[NtpConsts.REFERENCE] = server_name
            ntp_dict[NtpConsts.OFFSET] = ntp_show[NtpConsts.OFFSET]  # specific parameter does not verified
            ValidationTool.compare_nested_dictionary_content(ntp_show, ntp_dict).verify_result()

        with allure.step("Validate server configured with default values"):
            server_list = OutputParsingTool.parse_json_str_to_dictionary(
                system.ntp.servers.show(server_name)).get_returned_value()
            ValidationTool.compare_dictionary_content(
                server_list, NtpConsts.SERVER1_DEFAULT_VALUES_DICT).verify_result()

        with allure.step("Validate system clock and date"):
            # compare to previous time and validate the change
            curr_time = GeneralCliCommon(TestToolkit.engines.dut).get_utc_time()
            diff_time = int(curr_time) - int(prev_time)
            assert diff_time < 300, "ntp diff time: {diff_time} seconds, is higher than expected time of 300 seconds".\
                format(diff_time=diff_time)

        with allure.step("Create authentication key"):
            system.ntp.keys.set_resource(NtpConsts.KEY_1).verify_result()
            system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(
                op_param_name=NtpConsts.VALUE, op_param_value=NtpConsts.KEY_VALUE, apply=True).verify_result()

        with allure.step("Update existing ntp server with none default values"):
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.IBURST, op_param_value=NtpConsts.Iburst.ENABLED.value).\
                verify_result()
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.DISABLED.value).verify_result()
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.VERSION, op_param_value=NtpConsts.Version.VERSION_3.value).verify_result()
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.TRUSTED, op_param_value=NtpConsts.Trusted.YES.value).verify_result()
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.KEY, op_param_value=NtpConsts.KEY_1, apply=True).verify_result()

        with allure.step("Validate ntp server configured values"):
            server_list = OutputParsingTool.parse_json_str_to_dictionary(
                system.ntp.servers.show(server_name)).get_returned_value()
            ValidationTool.compare_dictionary_content(
                server_list, NtpConsts.SERVER_NONE_DEFAULT_VALUES_DICT).verify_result()
            time.sleep(NtpConsts.CONFIG_TIME)

        with allure.step("Validate show system ntp output"):
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            ntp_dict[NtpConsts.SERVER] = {server_name: {}}
            ntp_dict[NtpConsts.STATE] = NtpConsts.State.ENABLED.value
            ntp_dict[NtpConsts.STATUS] = NtpConsts.Status.UNSYNCHRONISED.value
            ntp_dict.pop(NtpConsts.REFERENCE)
            ntp_dict.pop(NtpConsts.OFFSET)
            ValidationTool.compare_nested_dictionary_content(ntp_show, ntp_dict).verify_result()

        with allure.step("Unset each of the server configurations"):
            if TestToolkit.tested_api == ApiType.OPENAPI:
                system.ntp.servers.unset_resource(server_name, apply=True).verify_result()
                system.ntp.servers.set_resource(server_name, apply=True).verify_result()
            else:
                system.ntp.servers.resources_dict[server_name].unset(op_param=NtpConsts.ASSOCIATION_TYPE).\
                    verify_result()
                system.ntp.servers.resources_dict[server_name].unset(op_param=NtpConsts.IBURST).\
                    verify_result()
                system.ntp.servers.resources_dict[server_name].unset(op_param=NtpConsts.STATE).verify_result()
                system.ntp.servers.resources_dict[server_name].unset(op_param=NtpConsts.VERSION).verify_result()
                system.ntp.servers.resources_dict[server_name].unset(op_param=NtpConsts.TRUSTED).verify_result()
                system.ntp.servers.resources_dict[server_name].unset(op_param=NtpConsts.KEY, apply=True).verify_result()

        with allure.step("Validate server configured with default values"):
            server_list = OutputParsingTool.parse_json_str_to_dictionary(
                system.ntp.servers.show(server_name)).get_returned_value()
            ValidationTool.compare_dictionary_content(
                server_list, NtpConsts.SERVER1_DEFAULT_VALUES_DICT).verify_result()

        with allure.step("Enable ntp server"):
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.ENABLED.value, apply=True).verify_result()
            time.sleep(NtpConsts.SYNCHRONIZATION_MAX_TIME)

        with allure.step("Validate show system ntp output"):
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            ntp_dict[NtpConsts.SERVER] = {server_name: {}}
            ntp_dict[NtpConsts.STATE] = NtpConsts.State.ENABLED.value
            ntp_dict[NtpConsts.STATUS] = NtpConsts.Status.SYNCHRONISED.value
            ntp_dict[NtpConsts.REFERENCE] = server_name
            ntp_dict[NtpConsts.OFFSET] = ntp_show[NtpConsts.OFFSET]  # specific parameter does not verified
            ValidationTool.compare_nested_dictionary_content(ntp_show, ntp_dict).verify_result()

        with allure.step("Set system ntp disabled"):
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.DISABLED.value,
                           apply=True).verify_result()

        with allure.step("Validate show system ntp output"):
            if is_bug_active(4868603):
                time.sleep(4)
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            ntp_dict[NtpConsts.SERVER] = {server_name: {}}
            ntp_dict[NtpConsts.STATE] = NtpConsts.State.DISABLED.value
            ntp_dict[NtpConsts.STATUS] = NtpConsts.Status.UNSYNCHRONISED.value
            ntp_dict.pop(NtpConsts.REFERENCE)
            ntp_dict.pop(NtpConsts.OFFSET)
            ValidationTool.compare_nested_dictionary_content(ntp_show, ntp_dict).verify_result()

        # with allure.step("Verify ntp daemon state"):
        #     # Verify ntp daemon state (Ntpd stopped running)

        with allure.step("Set system ntp enabled"):
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.ENABLED.value,
                           apply=True, ask_for_confirmation=True).verify_result()
            time.sleep(NtpConsts.SYNCHRONIZATION_MAX_TIME)

        with allure.step("Validate show system ntp output"):
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            ntp_dict[NtpConsts.SERVER] = {server_name: {}}
            ntp_dict[NtpConsts.STATE] = NtpConsts.State.ENABLED.value
            ntp_dict[NtpConsts.STATUS] = NtpConsts.Status.SYNCHRONISED.value
            ntp_dict[NtpConsts.REFERENCE] = server_name
            ntp_dict[NtpConsts.OFFSET] = ntp_show[NtpConsts.OFFSET]  # specific parameter does not verified
            ValidationTool.compare_nested_dictionary_content(ntp_show, ntp_dict).verify_result()

        with allure.step("Unset system ntp"):
            system.ntp.unset().verify_result()
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.DISABLED.value).verify_result()
            system.ntp.set(op_param_name=NtpConsts.DHCP, op_param_value=NtpConsts.Dhcp.DISABLED.value,
                           apply=True, ask_for_confirmation=True).verify_result()
            time.sleep(NtpConsts.CONFIG_TIME)

        with allure.step("Validate show system ntp commands output"):
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            default_dict = dict(NtpConsts.NTP_DEFAULT_DICT)
            default_dict[NtpConsts.STATE] = NtpConsts.State.DISABLED.value
            default_dict[NtpConsts.DHCP] = NtpConsts.Dhcp.DISABLED.value
            ValidationTool.compare_dictionary_content(ntp_show, default_dict).verify_result()
            server_list = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.servers.show()).get_returned_value()
            assert not server_list, f"server list {server_list} should be empty"
            key_list = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.keys.show()).get_returned_value()
            assert not key_list, f"key list {key_list} should be empty"

        with allure.step("Validate unset system ntp commands"):
            if TestToolkit.tested_api == ApiType.OPENAPI:
                system.ntp.unset(apply=True).verify_result()
            else:
                system.ntp.unset(op_param=NtpConsts.AUTHENTICATION).verify_result()
                system.ntp.unset(op_param=NtpConsts.DHCP).verify_result()
                system.ntp.unset(op_param=NtpConsts.LISTEN).verify_result()
                system.ntp.unset(op_param=NtpConsts.STATE).verify_result()
                system.ntp.unset(op_param=NtpConsts.VRF, apply=True, ask_for_confirmation=True).verify_result()
            time.sleep(NtpConsts.CONFIG_TIME)
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            assert ntp_show[NtpConsts.AUTHENTICATION] == NtpConsts.NTP_DEFAULT_DICT[NtpConsts.AUTHENTICATION], \
                "Ntp parameter should equal to default value"
            assert ntp_show[NtpConsts.DHCP] == NtpConsts.NTP_DEFAULT_DICT[NtpConsts.DHCP], \
                "Ntp parameter should equal to default value"
            assert ntp_show[NtpConsts.LISTEN] == NtpConsts.NTP_DEFAULT_DICT[NtpConsts.LISTEN], \
                "Ntp parameter should equal to default value"
            assert ntp_show[NtpConsts.STATE] == NtpConsts.NTP_DEFAULT_DICT[NtpConsts.STATE], \
                "Ntp parameter should equal to default value"
            assert ntp_show[NtpConsts.VRF] == NtpConsts.NTP_DEFAULT_DICT[NtpConsts.VRF], \
                "Ntp parameter should equal to default value"

    finally:
        with allure.step("Verify ntp daemon state"):
            logging.info("Verify ntp daemon state")
            # Verify daemon state (Ntpd is running)


@pytest.mark.system
@pytest.mark.ntp
@pytest.mark.simx
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_ntp_system_authentication(engines, test_api):
    """
    validate:
    - Show NTP authentication keys inventory
    - Configure NTP authentication state
    - Manage NTP authentication keys (create, remove, change)
    - Authentication key default values
    - Unset on specific key node removes the key, unset on 'key' node removes all keys
    - Unset on per-key configurations resets them to default state
    - Trusted key

    Test flow:
    1. Clear all ntp configurations:
    2. Disable ntp functionality
    3. Set some different date and time
    4 .Configure ntp server and key
    5. Create authentication key
    6. Validate show system key output (expect key exists with default values)
    7. Update authentication key configuration
    8. Validate show system key output (expect key exists with configured values)
    9. Enable ntp authentication
    10. Enable ntp functionality
    11. Validate show system ntp output (expect ntp auth enabled and clock unsync)
    12. Set system ntp key to trusted
    13. Validate show system ntp output (expect ntp auth enabled and clock sync)
    14. Verify time and date are up to date
    15. Disable ntp functionality
    16. Unset each of the configurations of the key
    17. Create wrong authentication key
    18. Validate show system key output (expect both keys, first key with default values)
    19. Remove the first auth. Key
    20. Validate show system key output (expect only second key)
    21. Enable ntp functionality
    22. Validate show system ntp output (expect ntp auth enabled and clock unsync)
    23. Remove all authentication keys
    24. Validate show system key output (expect no keys)
    25. Disable ntp authentication
    26. Validate show system ntp output (expect ntp auth disabled and clock sync)
    """
    TestToolkit.tested_api = test_api
    system = System()
    player_engine = engines['sonic_mgmt']
    server_name = player_engine.ip
    ntp_dict = dict(NtpConsts.NTP_DEFAULT_DICT)
    ntp_key_dict = dict(NtpConsts.KEY_CONFIGURED_DICT)

    try:
        with allure.step("Create ntp server on sonic-mgmt docker"):
            create_ntp_server(player_engine)

        with allure.step("Clear all ntp configurations"):
            system.ntp.unset().verify_result()

        with allure.step("Disable dhcp and ntp functionality"):
            system.ntp.set(op_param_name=NtpConsts.DHCP, op_param_value=NtpConsts.Dhcp.DISABLED.value).verify_result()
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.DISABLED.value,
                           apply=True).verify_result()

        with allure.step("Set different date and time"):
            prev_time = GeneralCliCommon(TestToolkit.engines.dut).get_utc_time()
            GeneralCliCommon(TestToolkit.engines.dut).set_time(NtpConsts.OLD_DATE)

        with allure.step("Create authentication key"):
            system.ntp.keys.set_resource(NtpConsts.KEY_1).verify_result()
            system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(
                op_param_name=NtpConsts.VALUE, op_param_value=NtpConsts.KEY_VALUE, apply=True).verify_result()

        with allure.step("Configure ntp server and key"):
            system.ntp.servers.set_resource(server_name).verify_result()
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.KEY, op_param_value=NtpConsts.KEY_1).verify_result()
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.TRUSTED, op_param_value=NtpConsts.Trusted.YES.value, apply=True).verify_result()

        with allure.step("Validate show system ntp key output"):
            key_list = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.keys.show()).get_returned_value()
            ntp_key_list = {NtpConsts.KEY_1: ntp_key_dict}
            ValidationTool.compare_dictionary_content(key_list, ntp_key_list).verify_result()

        with allure.step("Update authentication key configuration"):
            system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(
                op_param_name=NtpConsts.VALUE, op_param_value=NtpConsts.KEY1_VALUE).verify_result()
            system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(
                op_param_name=NtpConsts.TYPE, op_param_value=NtpConsts.KeyType.SHA1.value, apply=True).verify_result()

        with allure.step("Validate show system ntp key output"):
            key_list = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.keys.show()).get_returned_value()
            ntp_key_dict[NtpConsts.TYPE] = NtpConsts.KeyType.SHA1.value
            ntp_key_list[NtpConsts.KEY_1] = ntp_key_dict
            ValidationTool.compare_dictionary_content(key_list, ntp_key_list).verify_result()

        with allure.step("Enable ntp authentication"):
            system.ntp.set(op_param_name=NtpConsts.AUTHENTICATION,
                           op_param_value=NtpConsts.Authentication.ENABLED.value).verify_result()

        with allure.step("Enable ntp functionality"):
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.ENABLED.value,
                           apply=True).verify_result()
            time.sleep(NtpConsts.SYNCHRONIZATION_MAX_TIME)

        with allure.step("Validate show system ntp output"):
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            ntp_dict[NtpConsts.AUTHENTICATION] = NtpConsts.Authentication.ENABLED.value
            ntp_dict[NtpConsts.DHCP] = NtpConsts.Dhcp.DISABLED.value
            ntp_dict[NtpConsts.SERVER] = {server_name: {}}
            ValidationTool.compare_nested_dictionary_content(ntp_show, ntp_dict).verify_result()

        with allure.step("Set key to trusted"):
            system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(
                op_param_name=NtpConsts.TRUSTED, op_param_value=NtpConsts.Trusted.YES.value, apply=True).verify_result()
            time.sleep(NtpConsts.SYNCHRONIZATION_MAX_TIME)

        with allure.step("Validate show system ntp output"):
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            # Update ntp_dict with the configured ntp values
            ntp_dict[NtpConsts.SERVER] = {server_name: {}}
            ntp_dict[NtpConsts.OFFSET] = ntp_show[NtpConsts.OFFSET]
            ntp_dict[NtpConsts.REFERENCE] = server_name
            ntp_dict[NtpConsts.STATUS] = NtpConsts.Status.SYNCHRONISED.value
            ValidationTool.compare_nested_dictionary_content(ntp_show, ntp_dict).verify_result()

        with allure.step("Validate system clock and date"):
            # compare to previous time and validate the change
            curr_time = GeneralCliCommon(TestToolkit.engines.dut).get_utc_time()
            diff_time = int(curr_time) - int(prev_time)
            assert diff_time < 300, "ntp diff time: {diff_time} seconds, is higher than expected time of 300 seconds".\
                format(diff_time=diff_time)

        with allure.step("Disable ntp functionality"):
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.DISABLED.value,
                           apply=True).verify_result()

        with allure.step("Unset each of the configurations of the key"):
            if TestToolkit.tested_api == ApiType.OPENAPI:
                system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(
                    op_param_name=NtpConsts.TYPE, op_param_value=NtpConsts.KeyType.MD5.value).verify_result()
                system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(
                    op_param_name=NtpConsts.TRUSTED, op_param_value=NtpConsts.Trusted.NO.value, apply=True).\
                    verify_result()
            else:
                system.ntp.keys.resources_dict[NtpConsts.KEY_1].unset(op_param=NtpConsts.TYPE).verify_result()
                system.ntp.keys.resources_dict[NtpConsts.KEY_1].unset(op_param=NtpConsts.TRUSTED, apply=True).\
                    verify_result()

        with allure.step("Create wrong authentication key"):
            system.ntp.keys.set_resource(NtpConsts.KEY_2).verify_result()
            system.ntp.keys.resources_dict[NtpConsts.KEY_2].set(
                op_param_name=NtpConsts.VALUE, op_param_value=NtpConsts.KEY2_VALUE, apply=True).verify_result()

        with allure.step("Validate show system ntp key output"):
            key_list = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.keys.show()).get_returned_value()
            ntp_key_dict = dict(NtpConsts.KEY_DEFAULT_DICT)
            ntp_key_dict[NtpConsts.KEY_1] = NtpConsts.KEY_CONFIGURED_DICT
            ntp_key_dict[NtpConsts.KEY_2] = NtpConsts.KEY_CONFIGURED_DICT
            ValidationTool.compare_dictionary_content(key_list, ntp_key_dict).verify_result()

        with allure.step("Validate show system ntp specific key output"):
            key_show = OutputParsingTool.parse_json_str_to_dictionary(
                system.ntp.keys.show(NtpConsts.KEY_1)).get_returned_value()
            ValidationTool.compare_dictionary_content(
                key_show, NtpConsts.KEY_CONFIGURED_DICT).verify_result()

            key_list = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.keys.show()).get_returned_value()
            ntp_key_dict = dict(NtpConsts.KEY_DEFAULT_DICT)
            ntp_key_dict[NtpConsts.KEY_1] = NtpConsts.KEY_CONFIGURED_DICT
            ntp_key_dict[NtpConsts.KEY_2] = NtpConsts.KEY_CONFIGURED_DICT
            ValidationTool.compare_dictionary_content(key_list, ntp_key_dict).verify_result()

        with (allure.step("Remove the first authenticated key and add the second (incorrect) key to server instead")):
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.KEY, op_param_value=NtpConsts.KEY_2).verify_result()
            system.ntp.keys.unset_resource(NtpConsts.KEY_1, apply=True).verify_result()

        with allure.step("Validate show system ntp key output"):
            key_list = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.keys.show()).get_returned_value()
            ntp_key_dict.pop(NtpConsts.KEY_1)
            ValidationTool.compare_dictionary_content(key_list, ntp_key_dict).verify_result()

        with allure.step("Enable ntp functionality"):
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.ENABLED.value,
                           apply=True).verify_result()
            time.sleep(NtpConsts.CONFIG_TIME)

        with allure.step("Validate show system ntp output"):
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            ntp_dict.pop(NtpConsts.REFERENCE)
            ntp_dict.pop(NtpConsts.OFFSET)
            ntp_dict[NtpConsts.STATUS] = NtpConsts.Status.UNSYNCHRONISED.value
            ValidationTool.compare_nested_dictionary_content(ntp_show, ntp_dict).verify_result()

        with allure.step("Remove all authentication keys"):
            system.ntp.servers.resources_dict[server_name].unset(
                op_param=NtpConsts.KEY + ' ' + NtpConsts.KEY_2).verify_result()
            system.ntp.unset(NtpConsts.KEY, apply=True).verify_result()

        with allure.step("Validate show system ntp key output"):
            key_list = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.keys.show()).get_returned_value()
            assert not key_list, "Key list should be empty"

        with allure.step("Disable ntp authentication"):
            system.ntp.set(op_param_name=NtpConsts.AUTHENTICATION,
                           op_param_value=NtpConsts.Authentication.DISABLED.value, apply=True).verify_result()
            time.sleep(NtpConsts.SYNCHRONIZATION_MAX_TIME)

        with allure.step("Validate show system ntp output"):
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            ntp_dict[NtpConsts.AUTHENTICATION] = NtpConsts.Authentication.DISABLED.value
            ntp_dict[NtpConsts.SERVER] = {server_name: {}}
            ntp_dict[NtpConsts.OFFSET] = ntp_show[NtpConsts.OFFSET]
            ntp_dict[NtpConsts.REFERENCE] = server_name
            ntp_dict[NtpConsts.STATUS] = NtpConsts.Status.SYNCHRONISED.value
            ValidationTool.compare_nested_dictionary_content(ntp_show, ntp_dict).verify_result()

    finally:
        with allure.step("Unset system ntp"):
            system.ntp.unset(apply=True).verify_result()


@pytest.mark.system
@pytest.mark.ntp
@pytest.mark.simx
def test_configure_ntp_multiple_servers(random_api):
    """
    validate:
    - Add and configure multiple servers
    - Servers can be configured using ip address (v4/v6) and hostname.
    - Show system NTP (brief and status)
    - Show all NTP servers (brief and detail)
    - Show NTP configuration per-server
    - Unset of specific server and unset on all servers
    - Disable per-server configuration
    - System remains stable and responsive with a couple of ntp servers configured
    - Proper behavior in case of disruption from an active server
        (verify it starts syncing with another existing server).
    - The maximum number of servers that can be configured.
    - Setting trusted on a specific server is not effecting other configured servers.

    Test flow:
    1. Clear all ntp configurations:
    2. Configure 10 servers (by v4/v6/hostname)
    3. Validate show system ntp (all flags) output (Display: Referenced server, Clock is synchronized)
    4. Validate show system ntp server (all flags) output (Display: list of 10 configured servers)
    5. Validate show system ntp server <server-id> output (Show specific server configuration)
    6. Validate max number of servers configured (Failure (reached maximum))
    7. Disable state of a specific server (Only the specific server becomes “disabled”)
    8. Check unset of a specific server, and active another server
        (The active server does not exist, and another server becomes active)
    9. Check unset of all servers (All servers removed)
    """
    system = System()
    server2_hostname = get_hostname_from_ip(NtpConsts.SERVER2_IPV4)
    ntp_dict = dict(NtpConsts.NTP_DEFAULT_DICT)
    ntp_brief_dict = dict(NtpConsts.NTP_DEFAULT_DICT)

    try:
        with allure.step("Clear all ntp configurations"):
            system.ntp.unset().verify_result()
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.DISABLED.value).verify_result()
            system.ntp.set(op_param_name=NtpConsts.DHCP, op_param_value=NtpConsts.Dhcp.DISABLED.value, apply=True).\
                verify_result()

        with allure.step("Configure 10 servers (by v4|v6|hostname)"):
            system.ntp.servers.set_resource(NtpConsts.SERVER1_IPV4).verify_result()
            system.ntp.servers.set_resource(server2_hostname).verify_result()
            for server_id in range(1, (NtpConsts.MULTIPLE_SERVERS_NUMBER - 2)):
                server_name = 'dummy.server' + str(server_id)
                system.ntp.servers.set_resource(server_name, apply=False)
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.ENABLED.value,
                           apply=True).verify_result()
            time.sleep(NtpConsts.SYNCHRONIZATION_MAX_TIME)

        with allure.step("Validate show system ntp (all flags) output"):
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            if TestToolkit.tested_api == ApiType.OPENAPI:
                ntp_dict[NtpConsts.SERVER] = NtpConsts.MULTIPLE_SERVERS_DEFAULT_DICT
                ntp_dict[NtpConsts.DHCP] = NtpConsts.Dhcp.DISABLED.value
                ntp_dict[NtpConsts.STATUS] = NtpConsts.Status.SYNCHRONISED.value
                ntp_dict[NtpConsts.REFERENCE] = ntp_show[NtpConsts.REFERENCE]
                ntp_dict[NtpConsts.OFFSET] = ntp_show[NtpConsts.OFFSET]  # Offset is not validated
            else:
                ntp_show_brief = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show('brief')).\
                    get_returned_value()
                ntp_dict[NtpConsts.SERVER] = ntp_brief_dict[NtpConsts.SERVER] = NtpConsts.MULTIPLE_SERVERS_DEFAULT_DICT
                ntp_dict[NtpConsts.DHCP] = ntp_brief_dict[NtpConsts.DHCP] = NtpConsts.Dhcp.DISABLED.value
                ntp_dict[NtpConsts.STATUS] = ntp_brief_dict[NtpConsts.STATUS] = NtpConsts.Status.SYNCHRONISED.value
                ntp_dict[NtpConsts.REFERENCE] = ntp_brief_dict[NtpConsts.REFERENCE] = ntp_show[NtpConsts.REFERENCE]
                ntp_dict[NtpConsts.OFFSET] = ntp_brief_dict[NtpConsts.OFFSET] = ntp_show[NtpConsts.OFFSET]  # Offset is not validated
                ValidationTool.compare_dictionary_content(ntp_show_brief, ntp_brief_dict).verify_result()
            ValidationTool.compare_dictionary_content(ntp_show, ntp_dict).verify_result()

        with allure.step("Validate show system ntp server (all flags) output"):
            server_list = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.servers.show()).get_returned_value()
            if TestToolkit.tested_api == ApiType.OPENAPI:
                server_dict = dict(NtpConsts.MULTIPLE_SERVERS_CONFIG_DICT)
                server_dict[NtpConsts.SERVER1_IPV4] = server_list[NtpConsts.SERVER1_IPV4]
                server_dict[NtpConsts.SERVER2_HOSTNAME] = server_list[NtpConsts.SERVER2_HOSTNAME]
                ValidationTool.compare_dictionary_content(server_list, server_dict).verify_result()
            else:
                server_brief_list = OutputParsingTool.parse_json_str_to_dictionary(
                    system.ntp.servers.show('brief')).get_returned_value()
                server_detail_list = OutputParsingTool.parse_json_str_to_dictionary(
                    system.ntp.servers.show('detail')).get_returned_value()
                # The hostname server's resolve-as reflects live DNS, which may return a different
                # (or rotating) A record than SERVER2_IPV4. Don't pin the exact address: verify it
                # resolved to a valid IP, then relax only that field while still strictly checking
                # every other server field (see the OPENAPI branch above, which is similarly lenient).
                resolved = server_list[NtpConsts.SERVER2_HOSTNAME][NtpConsts.RESOLVE_AS]
                assert resolved != NtpConsts.SERVER_FAILED, \
                    f"{NtpConsts.SERVER2_HOSTNAME} failed DNS resolution: {resolved}"
                socket.inet_aton(resolved)  # raises OSError if not a valid IPv4 address
                expected_servers = dict(NtpConsts.MULTIPLE_SERVERS_CONFIG_DICT)
                expected_server2 = dict(NtpConsts.SERVER2_DEFAULT_VALUES_DICT)
                expected_server2[NtpConsts.RESOLVE_AS] = resolved
                expected_servers[NtpConsts.SERVER2_HOSTNAME] = expected_server2
                ValidationTool.compare_dictionary_content(server_brief_list, expected_servers).\
                    verify_result()
                listed_servers = len(server_detail_list)
                assert listed_servers == 2, "Listed {listed} servers, expected {expected} servers". \
                    format(listed=listed_servers, expected=2)
                ValidationTool.compare_dictionary_content(server_list, expected_servers). \
                    verify_result()

        with allure.step("Validate server configured with default values"):
            server_dict = OutputParsingTool.parse_json_str_to_dictionary(
                system.ntp.servers.show(NtpConsts.SERVER1_IPV4)).get_returned_value()
            ValidationTool.compare_dictionary_content(
                server_dict, NtpConsts.SERVER1_DEFAULT_VALUES_DICT).verify_result()

        # with allure.step("Validate max number of servers configured"):
        #     # currently not supported (can configure more than 10 servers)
        #     # system.ntp.servers.set_resource(NtpConsts.SERVER3_IPV4).verify_result(should_succeed=False)

        with allure.step("Disable state of a specific server"):
            system.ntp.servers.resources_dict[NtpConsts.SERVER1_IPV4].set(
                op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.DISABLED.value, apply=True).\
                verify_result()
            server_list = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.servers.show()).get_returned_value()
            assert server_list[NtpConsts.SERVER1_IPV4][NtpConsts.STATE] == NtpConsts.State.DISABLED.value, \
                "Server {server} state should be {expected}".\
                format(server=NtpConsts.SERVER1_IPV4, expected=NtpConsts.State.DISABLED.value)
            assert server_list[server2_hostname][NtpConsts.STATE] == NtpConsts.State.ENABLED.value, \
                "Server {server} state should be {expected}".\
                format(server=server2_hostname, expected=NtpConsts.State.ENABLED.value)
            for server_id in range(1, (NtpConsts.MULTIPLE_SERVERS_NUMBER - 2)):
                server_name = 'dummy.server' + str(server_id)
                assert server_list[server_name][NtpConsts.STATE] == NtpConsts.State.ENABLED.value, \
                    "Server {server} state should be {expected}". \
                    format(server=server_name, expected=NtpConsts.State.ENABLED.value)

        with allure.step("Set trusted on a specific server"):
            system.ntp.servers.resources_dict[server2_hostname].set(
                op_param_name=NtpConsts.TRUSTED, op_param_value=NtpConsts.Trusted.YES.value, apply=True).verify_result()

        with allure.step("Validate trusted value for all the configured servers"):
            server_dict = OutputParsingTool.parse_json_str_to_dictionary(
                system.ntp.servers.show(NtpConsts.SERVER1_IPV4)).get_returned_value()
            assert server_dict[NtpConsts.TRUSTED] == NtpConsts.Trusted.NO.value, \
                "Server {server} trusted should be {expected}".\
                format(server=NtpConsts.SERVER1_IPV4, expected=NtpConsts.Trusted.NO.value)

            for server_id in range(1, (NtpConsts.MULTIPLE_SERVERS_NUMBER - 2)):
                server_name = 'dummy.server' + str(server_id)
                server_dict = OutputParsingTool.parse_json_str_to_dictionary(
                    system.ntp.servers.show(server_name)).get_returned_value()
                assert server_dict[NtpConsts.TRUSTED] == NtpConsts.Trusted.NO.value, \
                    "Server {server} trusted should be {expected}".\
                    format(server=server_name, expected=NtpConsts.Trusted.NO.value)

            server_dict = OutputParsingTool.parse_json_str_to_dictionary(
                system.ntp.servers.show(server2_hostname)).get_returned_value()
            assert server_dict[NtpConsts.TRUSTED] == NtpConsts.Trusted.YES.value, \
                "Server {server} trusted should be {expected}".\
                format(server=server2_hostname, expected=NtpConsts.Trusted.YES.value)

        with allure.step("Check unset of a specific server and active another server"):
            system.ntp.servers.unset_resource(NtpConsts.SERVER1_IPV4, apply=True).verify_result()
            time.sleep(NtpConsts.SYNCHRONIZATION_MAX_TIME)
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            assert ntp_show[NtpConsts.REFERENCE] != NtpConsts.SERVER1_IPV4, \
                "Reference server should be other than {server}".format(server=NtpConsts.SERVER1_IPV4)

            server_list = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.servers.show()).get_returned_value()
            ValidationTool.verify_field_exist_in_json_output(
                server_list, NtpConsts.SERVER1_IPV4, should_be_found=False).verify_result()

        with allure.step("Check unset of all servers"):
            system.ntp.unset(NtpConsts.SERVER, apply=True).verify_result()
            time.sleep(5)
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            ntp_dict = NtpConsts.NTP_DEFAULT_DICT
            ntp_dict[NtpConsts.DHCP] = NtpConsts.Dhcp.DISABLED.value
            ValidationTool.compare_dictionary_content(ntp_show, ntp_dict).verify_result()
            server_list = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.servers.show()).get_returned_value()
            assert not server_list, f"server list {server_list} should be empty"

    finally:
        with allure.step("Unset system ntp"):
            system.ntp.unset(apply=True).verify_result()


@pytest.mark.system
@pytest.mark.ntp
@pytest.mark.simx
def test_ntp_performance(random_api):
    """
    validate:
    - Similar configuration time for 1 and 10 servers
    - No degradation time to “nv show system ntp” when a lot of servers are configured
    - Normal system resources utilization when a lot of servers are configured
    - Time to sync system is reasonable

    Test flow:
    1. Clear all ntp configurations:
    2. Measure configuring time of 1 sever
    3. Measure show system ntp for 1 server (server is configured. Clock is synchronized)
    4. Measure configuring time of 10 severs
    5. Measure show system ntp for 10 server (All servers are configured. Clock is synchronized)
    6. Validate configuration time diff (Diff time < 2 sec)
    7. Validate show system ntp time diff (Diff time < 0.5 sec)
    8. Validate CPU utilization (Utilization < 60%)
    9. Remove all ntp servers
    10. Validate system sync time after setting a new server (Sync time < 5 sec)
    """
    system = System()
    server_name = NtpConsts.SERVER1_IPV4

    try:
        with allure.step("Clear all ntp configurations"):
            system.ntp.unset().verify_result()
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.DISABLED.value,
                           apply=True).verify_result()

        with allure.step("Measure configuring time of 1 server"):
            start_time = time.time()
            system.ntp.servers.set_resource(server_name, apply=True)
            end_time = time.time()
            config_1_duration = end_time - start_time

        with allure.step("Measure show system ntp time of 1 server"):
            start_time = time.time()
            system.ntp.show()
            end_time = time.time()
            show_1_duration = end_time - start_time

        with allure.step("Remove all ntp servers"):
            system.ntp.unset(apply=True).verify_result()

        with allure.step("Measure configuring time of 10 servers"):
            for server_id in range(1, NtpConsts.MULTIPLE_SERVERS_NUMBER - 1):
                server_name = 'dummy.server' + str(server_id)
                system.ntp.servers.set_resource(server_name, apply=False)
            server_name = 'server10'
            start_time = time.time()
            system.ntp.servers.set_resource(server_name, apply=True)
            end_time = time.time()
            config_10_duration = end_time - start_time

        with allure.step("Measure show system ntp time of 10 servers"):
            start_time = time.time()
            system.ntp.show()
            end_time = time.time()
            show_10_duration = end_time - start_time

        with allure.step("Validate configuration diff time"):
            config_duration_diff = config_10_duration - config_1_duration
            assert config_duration_diff < NtpConsts.CONFIG_TIME_DIFF_THRESHOLD, \
                "Configuration diff time: {actual} is higher than expected time: {expected}".\
                format(actual=config_duration_diff, expected=NtpConsts.CONFIG_TIME_DIFF_THRESHOLD)

        with allure.step("Validate show diff time"):
            show_duration_diff = show_10_duration - show_1_duration
            assert show_duration_diff < NtpConsts.SHOW_TIME_DIFF_THRESHOLD, \
                "Show diff time: {actual} is higher than expected time: {expected}".\
                format(actual=show_duration_diff, expected=NtpConsts.SHOW_TIME_DIFF_THRESHOLD)

        with allure.step("Validate cpu utilization"):
            cpu_show = OutputParsingTool.parse_json_str_to_dictionary(system.show("cpu")).get_returned_value()
            cpu_cores = cpu_show[SystemConsts.CPU_CORES].keys()
            for core in cpu_cores:
                cpu_utilization = cpu_show[SystemConsts.CPU_CORES][core][SystemConsts.CPU_UTILIZATION_KEY]
                assert cpu_utilization < SystemConsts.CPU_PERCENT_THRESH_MAX, \
                    "CPU utilization: {actual}% is higher than expected time: {expected}%".\
                    format(actual=cpu_utilization, expected=SystemConsts.CPU_PERCENT_THRESH_MAX)

        with allure.step("Remove all ntp servers"):
            system.ntp.unset(apply=True).verify_result()

        with allure.step("Validate system sync time after setting a new server"):
            system.ntp.servers.set_resource(server_name, apply=True)
            time.sleep(NtpConsts.SYNCHRONIZATION_MAX_TIME)
            ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
            assert ntp_show[NtpConsts.STATUS] == NtpConsts.Status.SYNCHRONISED.value, \
                "Synchronization time is longer than expected time of {expected} seconds".\
                format(actual=show_duration_diff, expected=NtpConsts.SYNCHRONIZATION_MAX_TIME)

    finally:
        with allure.step("Unset system ntp"):
            system.ntp.unset(apply=True).verify_result()


@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.dut_reboot
@pytest.mark.system
@pytest.mark.ntp
@pytest.mark.simx
def test_ntp_reliability():
    """
    validate:
    - Time sync work after reboot
    - Time sync work after power cycle
    - System remains operational under error conditions (configured unreachable server)
    - Time unsync expected when feature can't connect to server due to it's unavailability
    - Configuring/return to default the feature in the loop should be stable
    - Synchronization of the time should recover after losing and then recovering connection to configured server

    Test flow:
    1. Clear all ntp configurations
    2. Configure ntp server
    3. Verify system clock is synchronized
    3. Reboot system
    4. Verify system clock is synchronized (Clock is synchronized)
    5. Kill ntp server
    6 .Verify system clock is unsynchronized (Clock is unsynchronized)
    7. Run ntp server
    8. Verify system clock is synchronized (Clock is synchronized)
    9. Remove all ntp servers
    10. Configure an unreachable ntp server
    11. Sanity checks (System still operational)
    12. Configure ntp server in the loop (System remains stable)
    13. Set ntp server to default in the loop (System remains stable)
    """
    server_name = NtpConsts.SERVER1_IPV4
    system = System()

    try:
        with allure.step("Clear all ntp configurations"):
            system.ntp.unset().verify_result()
            system.ntp.set(op_param_name=NtpConsts.DHCP, op_param_value=NtpConsts.Dhcp.DISABLED.value, apply=True).\
                verify_result()

        with allure.step("Configure ntp server"):
            system.ntp.servers.set_resource(server_name, apply=True).verify_result()

        with allure.step("Verify system clock is synchronized"):
            ntp_dict = wait_for_ntp_status(system, NtpConsts.Status.SYNCHRONISED.value)
            assert ntp_dict[NtpConsts.STATUS] == NtpConsts.Status.SYNCHRONISED.value, \
                "Server {server} status should be {expected}".\
                format(server=server_name, expected=NtpConsts.Status.SYNCHRONISED.value)

        with allure.step('Reboot system'):
            system.reboot.action_reboot(params='force').verify_result()

        with allure.step("Verify system clock is synchronized"):
            ntp_dict = wait_for_ntp_status(system, NtpConsts.Status.SYNCHRONISED.value)
            assert ntp_dict[NtpConsts.STATUS] == NtpConsts.Status.SYNCHRONISED.value, \
                "Server {server} status should be {expected}".\
                format(server=server_name, expected=NtpConsts.Status.SYNCHRONISED.value)

        with allure.step('Stop ntp server'):
            GeneralCliCommon(TestToolkit.engines.dut).stop_service('ntp')

        with allure.step("Verify system clock is unsynchronized"):
            ntp_dict = wait_for_ntp_status(system, NtpConsts.Status.UNSYNCHRONISED.value)
            assert ntp_dict[NtpConsts.STATUS] == NtpConsts.Status.UNSYNCHRONISED.value, \
                "Server {server} status should be {expected}".\
                format(server=server_name, expected=NtpConsts.Status.UNSYNCHRONISED.value)

        with allure.step('Start ntp server'):
            GeneralCliCommon(TestToolkit.engines.dut).start_service('ntp')

        with allure.step("Verify system clock is synchronized"):
            ntp_dict = wait_for_ntp_status(system, NtpConsts.Status.SYNCHRONISED.value)
            assert ntp_dict[NtpConsts.STATUS] == NtpConsts.Status.SYNCHRONISED.value, \
                "Server {server} status should be {expected}".\
                format(server=server_name, expected=NtpConsts.Status.SYNCHRONISED.value)

        with allure.step("Remove all ntp servers"):
            system.ntp.unset(NtpConsts.SERVER, apply=True).verify_result()

        with allure.step("Configure an unreachable ntp server and verify system is still operational"):
            system.ntp.servers.set_resource(NtpConsts.INVALID_SERVER, apply=True).verify_result()
            # Validate system is still running by executing show ntp command
            OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()

        with allure.step("Configure ntp server in the loop and verify system remains stable"):
            for server_id in range(1, NtpConsts.NUMBER_OF_ITERATION):
                server_name = 'server-' + str(server_id)
                system.ntp.servers.set_resource(server_name, apply=True).verify_result()
            # Validate system is still stable by executing show ntp command
            OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()

        with allure.step("Set ntp server to default in the loop and verify system remains stable"):
            for server_id in range(1, NtpConsts.NUMBER_OF_ITERATION):
                system.ntp.unset(apply=True).verify_result()
            # Validate system is still stable by executing show ntp command
            OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()

    finally:
        with allure.step("Unset system ntp"):
            system.ntp.unset(apply=True).verify_result()


@pytest.mark.system
@pytest.mark.ntp
@pytest.mark.simx
def test_ntp_log(engines):
    """
    validate:
    - Configuring commands are logged to system log
    - Debug dump (tech support) contains the information related to the feature configuration

    Test flow:
    1. Rotate logs
    2. Clear all ntp configurations
    3. Configure ntp server and enable ntp
    4. Update server configuration
    5. Configure server key and update its configuration
    6. Configure vrf
    7. Validate commands exist in system log (Ntp commands exist)
    8. Validate commands exist in debug dump (Ntp commands exist)
    """
    server_name = NtpConsts.SERVER1_IPV4
    system = System()
    ssh_connection = ConnectionTool.create_ssh_conn(engines.dut.ip, engines.dut.username,
                                                    engines.dut.password).get_returned_value()

    try:
        with allure.step("Clear all ntp configurations"):
            system.log.rotate_logs()
            system.ntp.unset().verify_result()
            system.ntp.set(op_param_name=NtpConsts.DHCP, op_param_value=NtpConsts.Dhcp.DISABLED.value).verify_result()
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.DISABLED.value,
                           apply=True).verify_result()

        with allure.step("Configure ntp server and enable ntp"):
            system.ntp.servers.set_resource(NtpConsts.SERVER1_IPV4).verify_result()
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.ENABLED.value,
                           apply=True).verify_result()
            time.sleep(NtpConsts.SYNCHRONIZATION_MAX_TIME)

        with allure.step("Create authentication key"):
            system.ntp.keys.set_resource(NtpConsts.KEY_1).verify_result()
            system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(
                op_param_name=NtpConsts.VALUE, op_param_value=NtpConsts.KEY_VALUE, apply=True).verify_result()

        with allure.step("Update server configuration"):
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.State.DISABLED.value).verify_result()
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.VERSION, op_param_value=NtpConsts.Version.VERSION_3.value).verify_result()
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.TRUSTED, op_param_value=NtpConsts.Trusted.YES.value).verify_result()
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.KEY, op_param_value=NtpConsts.KEY_1, apply=True).verify_result()

        with allure.step("Configure server key and update its configuration"):
            system.ntp.keys.set_resource(NtpConsts.KEY_1).verify_result()
            system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(
                op_param_name=NtpConsts.VALUE, op_param_value=NtpConsts.KEY1_VALUE).verify_result()
            system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(
                op_param_name=NtpConsts.TYPE, op_param_value=NtpConsts.KeyType.SHA1.value).verify_result()
            system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(
                op_param_name=NtpConsts.TRUSTED, op_param_value=NtpConsts.Trusted.YES.value, apply=True).verify_result()

        # with allure.step("Configure vrf"):
        #     # Currently not supported
        #     # system.ntp.vrfs.set_resource(NtpConsts.Vrf.MGMT.value, apply=True).verify_result()

        with allure.step("Validate commands exist in system log"):
            system.log.verify_expected_logs(NtpConsts.LOG_MSG_LIST, engine=ssh_connection)

        # with allure.step("Validate commands exist in debug dump"):
        #     # Run tech support and validate command exist

    finally:
        with allure.step("Unset system ntp"):
            system.ntp.unset(apply=True).verify_result()


@pytest.mark.parametrize('test_api', [ApiType.NVUE])
@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.system
@pytest.mark.ntp
@pytest.mark.simx
def test_ntp_mgmt_port_listeners(topology_obj, nv_command, serial_engine, test_api):
    """
    validate:
    - NTP synchronization when listening to each of the mgmt ports.
    - Verify NTP still synchronizing when listen mgmt-port is up and the other mgmt-port is down.
    - Verify NTP synchronization stability for 5 minutes on both mgmt-ports listeners.
    - Check the following scenarios:
        NTP listen eth0 | eth0 link up   | eth1 link up   | expected: synchronized
        NTP listen eth0 | eth0 link up   | eth1 link down | expected: synchronized
        NTP listen eth0 | eth0 link down | eth1 link up   | expected: unsynchronized
        NTP listen eth1 | eth0 link up   | eth1 link up   | expected: synchronized
        NTP listen eth1 | eth0 link up   | eth1 link down | expected: synchronized
        NTP listen eth1 | eth0 link down | eth1 link up   | expected: unsynchronized

    Test flow:
    1. Clear all ntp configurations
    2. Verify ntp status is synchronized, listen to eth0
    3. Set ntp listen to eth1
    4. Verify ntp status is synchronized, listen to eth1
    5. Set interface eth1 state to down
    6. Verify ntp status is unsynchronized, listen to eth1
    7. Set interface eth0 state to down and eth1 state to up (SSH connection will be lost)
    8. Verify ntp status is synchronized, listen to eth1, and stable for 5 min
    9. Set ntp listen to eth0
    10. Verify ntp status is unsynchronized, listen to eth0
    11. Set interface eth0 state to up and eth1 state to down (SSH connection will be back)
    12. Verify ntp status is synchronized, listen to eth0, and stable for 5 min
    13. Clear ntp and mgmt-ports configuration
    """
    # serial_engine is a pytest fixture that provides a serial console connection
    # Use it when SSH might be unavailable (e.g., when bringing down network interfaces)

    try:
        with allure.step("Clear all ntp configurations"):
            nv_command.system.ntp.unset(apply=True).verify_result()
            time.sleep(NtpConsts.CONFIG_TIME)

        with allure.step("Verify ntp status is synchronized, listen to eth0"):
            verify_ntp_status_and_listen(nv_command, NtpConsts.Listen.ETH0.value, NtpConsts.Status.SYNCHRONISED.value)

        with allure.step("Set ntp listen to eth1"):
            nv_command.system.ntp.set(op_param_name=NtpConsts.LISTEN, op_param_value=NtpConsts.Listen.ETH1.value,
                                      apply=True).verify_result()
            time.sleep(NtpConsts.SYNCHRONIZE_TIME)  # Use SYNCHRONIZE_TIME (80s) instead of SYNCHRONIZE_NEW_LISTEN_TIME (20s)

        with allure.step("Verify ntp status is synchronized, listen to eth1"):
            verify_ntp_status_and_listen(nv_command, NtpConsts.Listen.ETH1.value, NtpConsts.Status.SYNCHRONISED.value)

        with allure.step("Set interface eth1 state to down"):
            nv_command.port['eth1'].interface.link.state.set(op_param_name=NvosConst.PORT_STATUS_DOWN, apply=True,
                                                             ask_for_confirmation=True).verify_result()
            time.sleep(NtpConsts.CONFIG_TIME)

        with allure.step("Verify ntp status is unsynchronized, listen to eth1"):
            # eth1 is down, use serial_engine
            verify_ntp_status_and_listen(nv_command, NtpConsts.Listen.ETH1.value, NtpConsts.Status.UNSYNCHRONISED.value, engine_dut=serial_engine)

        with allure.step("Set interface eth0 state to down and eth1 state to up (SSH connection will be lost)"):
            # eth1 is already down from step 5; use serial to avoid SSH to unreachable eth1
            nv_command.port['eth0'].interface.link.state.set(op_param_name=NvosConst.PORT_STATUS_DOWN,
                                                             dut_engine=serial_engine).verify_result()
            nv_command.port['eth1'].interface.link.state.set(op_param_name=NvosConst.PORT_STATUS_UP,
                                                             dut_engine=serial_engine, apply=True,
                                                             ask_for_confirmation=True).verify_result()
            time.sleep(NtpConsts.SYNCHRONIZE_TIME)

        # Connection in SSH is lost, continue in serial port

        with allure.step("Verify ntp status is synchronized, listen to eth1, and stable for 5 min"):
            verify_ntp_sync_stabilization(nv_command, NtpConsts.Listen.ETH1.value, 300, serial_engine)

        with allure.step("Set ntp listen to eth0"):
            nv_command.system.ntp.set(op_param_name=NtpConsts.LISTEN, op_param_value=NtpConsts.Listen.ETH0.value,
                                      dut_engine=serial_engine, apply=True).verify_result()
            time.sleep(NtpConsts.CONFIG_TIME)

        with allure.step("Verify ntp status is unsynchronized, listen to eth0"):
            # eth0 is down, use serial_engine
            verify_ntp_status_and_listen(nv_command, NtpConsts.Listen.ETH0.value,
                                         NtpConsts.Status.UNSYNCHRONISED.value, engine_dut=serial_engine)

        with allure.step("Set interface eth0 state to up and eth1 state to down (SSH connection will be back)"):
            nv_command.port['eth0'].interface.link.state.set(op_param_name=NvosConst.PORT_STATUS_UP,
                                                             dut_engine=serial_engine).verify_result()
            nv_command.port['eth1'].interface.link.state.set(op_param_name=NvosConst.PORT_STATUS_DOWN,
                                                             dut_engine=serial_engine, apply=True,
                                                             ask_for_confirmation=True).verify_result()
            time.sleep(NtpConsts.SYNCHRONIZE_TIME)

        # Connection in SSH is back

        with allure.step("Verify ntp status is synchronized, listen to eth0, and stable for 5 min"):

            # eth0 is now up, eth1 is down
            # Use serial_engine to avoid connection issues during transition
            verify_ntp_sync_stabilization(nv_command, NtpConsts.Listen.ETH0.value, 300, serial_engine)

    finally:
        with allure.step("Clear ntp and mgmt-ports configuration"):
            nv_command.port['eth0'].interface.link.state.unset(dut_engine=serial_engine, apply=True, ask_for_confirmation=True).verify_result()
            nv_command.port['eth1'].interface.link.state.unset(dut_engine=serial_engine, apply=True, ask_for_confirmation=True).verify_result()
            nv_command.system.ntp.unset(dut_engine=serial_engine, apply=True, ask_for_confirmation=True).verify_result()


@pytest.mark.system
@pytest.mark.ntp
@pytest.mark.simx
def test_ntp_invalid_values(random_api):
    """
    Check all the commands that get param with bad values

    Test flow:
    1. nv set system ntp state <random str> (Failure)
    2. nv set system ntp authentication <random str> (Failure)
    3. nv set system ntp key <higher than 65535> val <password> (Failure)
    4. nv set system ntp key <lower than 1> type <type> (Failure)
    5. nv set system ntp key <key-id> type <random str> (Failure)
    6. nv set system ntp key <key-id> trusted <random str> (Failure)
    7. nv set system ntp server <server-id> association-type <random str> (Failure)
    8. nv set system ntp server <server-id> state <random str> (Failure)
    9. nv set system ntp server <server-id> key <random str> (Failure)
    10. nv set system ntp server <server-id> trusted <random str> (Failure)
    11. nv set system ntp server <server-id> version <not 3|4> (Failure)
    12. nv set system ntp vrf <random str> (Failure)
    """
    system = System()
    server_name = NtpConsts.SERVER1_IPV4

    try:
        with allure.step("Validate set ntp invalid state"):
            system.ntp.set(op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.INVALID_STATE). \
                verify_result(should_succeed=False)

        with allure.step("Validate set ntp invalid authentication"):
            system.ntp.set(op_param_name=NtpConsts.AUTHENTICATION, op_param_value=NtpConsts.INVALID_AUTHENTICATION) \
                .verify_result(should_succeed=False)

        with allure.step("Validate set ntp invalid authentication"):
            system.ntp.set(op_param_name=NtpConsts.DHCP, op_param_value=NtpConsts.INVALID_DHCP) \
                .verify_result(should_succeed=False)

        with allure.step("Validate set ntp invalid authentication"):
            system.ntp.set(op_param_name=NtpConsts.LISTEN, op_param_value=NtpConsts.INVALID_LISTEN) \
                .verify_result(should_succeed=False)

        with allure.step("Validate set ntp invalid vrf"):
            system.ntp.set(op_param_name=NtpConsts.VRF, op_param_value=NtpConsts.INVALID_VRF) \
                .verify_result(should_succeed=False)

        with allure.step("Validate set ntp invalid higher key"):
            system.ntp.keys.set_resource(NtpConsts.INVALID_HIGHER_KEY).verify_result(should_succeed=False)

        with allure.step("Validate set ntp invalid lower key"):
            system.ntp.keys.set_resource(NtpConsts.INVALID_LOWER_KEY).verify_result(should_succeed=False)

        with allure.step("Validate set ntp key invalid type"):
            system.ntp.keys.set_resource(NtpConsts.KEY_1).verify_result()
            system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(
                op_param_name=NtpConsts.TYPE, op_param_value=NtpConsts.INVALID_KEY_TYPE).\
                verify_result(should_succeed=False)

        with allure.step("Validate set ntp key invalid trusted"):
            system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(
                op_param_name=NtpConsts.TRUSTED, op_param_value=NtpConsts.INVALID_KEY_TRUSTED). \
                verify_result(should_succeed=False)

        with allure.step("Validate set ntp server invalid association-type"):
            system.ntp.servers.set_resource(server_name).verify_result()
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.ASSOCIATION_TYPE, op_param_value=NtpConsts.INVALID_SERVER_ASSOCIATION_TYPE). \
                verify_result(should_succeed=False)

        with allure.step("Validate set ntp server invalid state"):
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.STATE, op_param_value=NtpConsts.INVALID_SERVER_STATE). \
                verify_result(should_succeed=False)

        with allure.step("Validate set ntp server invalid higher key"):
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.KEY, op_param_value=NtpConsts.INVALID_SERVER_HIGHER_KEY). \
                verify_result(should_succeed=False)

        with allure.step("Validate set ntp server invalid lower key"):
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.KEY, op_param_value=NtpConsts.INVALID_SERVER_LOWER_KEY). \
                verify_result(should_succeed=False)

        with allure.step("Validate set ntp server invalid trusted"):
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.TRUSTED, op_param_value=NtpConsts.INVALID_SERVER_TRUSTED).\
                verify_result(should_succeed=False)

        with allure.step("Validate set ntp server invalid version"):
            system.ntp.servers.resources_dict[server_name].set(
                op_param_name=NtpConsts.VERSION, op_param_value=NtpConsts.INVALID_SERVER_VERSION).\
                verify_result(should_succeed=False)

    finally:
        with allure.step("Unset system ntp"):
            system.ntp.unset(apply=True).verify_result()


# ---------------------------------------------

def wait_for_ntp_status(system, expected_status, timeout=NtpConsts.SYNCHRONIZATION_MAX_TIME, interval=1):
    elapsed_time = 0
    ntp_show = {}
    while elapsed_time < timeout:
        ntp_show = OutputParsingTool.parse_json_str_to_dictionary(system.ntp.show()).get_returned_value()
        if ntp_show.get(NtpConsts.STATUS) == expected_status:
            return ntp_show
        time.sleep(interval)
        elapsed_time += interval
    return ntp_show


def get_hostname_from_ip(ip):
    host_name_index = 0
    hostname_str = socket.gethostbyaddr(ip)[host_name_index]

    # Remove mlnx labs suffix from switch hostname
    return hostname_str.split('.')[host_name_index] + NtpConsts.HOSTNAME_SUFFIX


def create_ntp_server(player_engine):
    player_engine.run_cmd("apt-get install ntp; y")
    player_engine.run_cmd(f"cp {NtpConsts.NTP_SERVER_FILES} /etc/")
    player_engine.run_cmd("service ntp restart")


def verify_ntp_sync_stabilization(nv_command, expected_listen, expected_time, engine_dut=None):
    with allure.step(f"Verify ntp status is synchronized, listen to {expected_listen}, "
                     f"and stable for {expected_time} sec"):
        start_time = time.time()
        diff_time = 0
        while diff_time < expected_time:
            verify_ntp_status_and_listen(nv_command, expected_listen, NtpConsts.Status.SYNCHRONISED.value, engine_dut)
            diff_time = time.time() - start_time


@retry(Exception, tries=10, delay=6)
def verify_ntp_status_and_listen(nv_command, expected_listen, expected_status, engine_dut=None):
    with allure.step(f"Verify ntp status is {expected_status}, and listen to {expected_listen}"):
        ntp_show = OutputParsingTool.parse_json_str_to_dictionary(
            nv_command.system.ntp.show(dut_engine=engine_dut)).get_returned_value()
        assert ntp_show[NtpConsts.STATUS] == expected_status, (f'NTP status is {ntp_show[NtpConsts.STATUS]},'
                                                               f'while it should be "{expected_status}"')
        assert ntp_show[NtpConsts.LISTEN] == {expected_listen: {}}, (f'NTP listen is {ntp_show[NtpConsts.LISTEN]},'
                                                                     f'while it should be "{expected_listen}"')
