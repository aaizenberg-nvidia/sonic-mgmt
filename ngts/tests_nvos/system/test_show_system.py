import logging
import random
import time

import pytest
from retry import retry

from ngts.helpers.memory_helper import MemoryValidatorFactory, build_memory_stats
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_constants.constants_nvos import SystemConsts, CumulusConsts
from ngts.nvos_tools.Devices.BaseDevice import BaseDevice
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.system.test_system_factory_reset import execute_reset_factory
from ngts.tests_nvos.system.factory_reset.helpers import verify_the_setup_is_functional

from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.reboot_telemetry_helpers import REBOOT_REASON_SHOW_EXEMPTED_ERR_MSGS


cumulus_owner = "yleibovichwe"


logger = logging.getLogger()


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
@pytest.mark.air
@pytest.mark.air_ci
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_system(test_api, engines, devices, topology_obj, nv_command, test_name):
    """
    Run show system message command and verify the required message
        Test flow:
            1. run show system message
            2. validate all fields have values
            3. set hostname to "Jaguar-NVOS"
            5. run show system message
            6. verify hostname appending value is "Jaguar-NVOS"
            7. run nv config apply
            8. verify hostname changed to "Jaguar-NVOS"
            9. run unset system hostname
            10. run nv config apply
            11. verify hostname changed to ""nvos"
    """
    TestToolkit.tested_api = test_api
    dut_device: BaseDevice = devices.dut

    with allure.step('Run show system command and verify that each field has a value'):
        system_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.show()).get_returned_value()

    with allure.step('validate expected fields exist in output'):

        keys_to_remove = [SystemConsts.LOCATION, SystemConsts.CONTACT]  # keys pruned from output
        for key in keys_to_remove:
            system_output.pop(key, None)

        ValidationTool.verify_all_fields_value_exist_in_output_dictionary(
            system_output, nv_command.system.get_expected_fields(devices.dut, 'system')).verify_result()

    with allure.step('get default hostname value'):
        output = OutputParsingTool.parse_json_str_to_dictionary(Interface(None, dut_device.cur_mgmt_port_name).show()).get_returned_value()

        dhcp_enabled = 'state' in output and output['state'] == "enabled"
        if dhcp_enabled:
            noga_query_data = topology_obj.players['dut']['attributes'].noga_query_data['attributes']
            dhcp_hostname = noga_query_data['Specific']['dhcp_hostname'] or noga_query_data['Common']['Name']
            if dhcp_hostname:
                assert system_output[SystemConsts.HOSTNAME] in [dhcp_hostname, f'{dhcp_hostname}-mgmt2'], f'unexpected "{SystemConsts.HOSTNAME}" value.\nexpected: {[dhcp_hostname, f"{dhcp_hostname}-mgmt2"]}\nactual: {system_output[SystemConsts.HOSTNAME]}'
            default_hostname = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.show()).get_returned_value()[SystemConsts.HOSTNAME]
        else:
            default_hostname = dut_device.system_default_value_dict[SystemConsts.HOSTNAME]

    with allure.step('set system hostname command and verify that hostname is updated'):
        with allure.step('set new hostname'):
            new_hostname_value = "NOS-NVOS"
            res_obj, duration = OperationTime.save_duration('set hostname', '', test_name, nv_command.system.set,
                                                            SystemConsts.HOSTNAME, new_hostname_value,
                                                            apply=True, ask_for_confirmation=True)
            res_obj.verify_result()
        with allure.step('verify duration'):
            OperationTime.verify_operation_time(duration, 'set hostname', devices).verify_result()
        with allure.step('verify change in show'):
            def _check_hostname():
                output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.show()).get_returned_value()
                ValidationTool.verify_field_value_in_output(output, SystemConsts.HOSTNAME, new_hostname_value).verify_result()
            ValidationTool.retry_until_valid(_check_hostname, tries=5, delay=3,
                                             description=f"wait for hostname to become '{new_hostname_value}'")

    with allure.step('Run unset system hostname command and verify that hostname is updated'):
        with allure.step('unset hostname'):
            nv_command.system.unset(SystemConsts.HOSTNAME, apply=True, ask_for_confirmation=True).verify_result()
            if dhcp_enabled:
                logging.info("Wait till the management interface will be reloaded to get a hostname from DHCP")
                time.sleep(30)
        with allure.step('verify hostname is back to default'):
            system_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.show()).get_returned_value()
            time.sleep(3)
            valid_hostnames = [default_hostname, f'{default_hostname}-mgmt2']
            assert system_output[SystemConsts.HOSTNAME] in valid_hostnames, (
                f'unexpected "{SystemConsts.HOSTNAME}" value.\nexpected one of: {valid_hostnames}\nactual: {system_output[SystemConsts.HOSTNAME]}'
            )


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
@pytest.mark.air
@pytest.mark.air_sanity
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_system_message(test_api, engines, devices, nv_command):
    """
    Run show/set/unset system message command and verify the required message
        Test flow:
            1. run show system message
            2. validate all fields have values
            3. set pre-login message[run cmd + apply conf]
            5. run show system message
            6. verify pre-login changed to "NVOS-TESTING"
            7. set post-login message[run cmd + apply conf]
            8. run show system message
            9. verify post-login changed to "NVOS-TESTING"
            10. unset post-login message[run cmd + apply conf]
            11. run show system message
            12. verify post-login changed to default value
            10. unset pre-login message[run cmd + apply conf]
            11. run show system message
            12. verify pre-login changed to default value
    """
    TestToolkit.tested_api = test_api

    new_pre_login_msg = "Testing PRE LOGIN MESSAGE"
    new_post_login_msg = "Testing POST LOGIN MESSAGE"

    with allure.step('Run set system message pre/post-login command and verify that pre/post-login are updated'):
        message_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.message.show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(message_output, SystemConsts.PRE_LOGIN_MESSAGE,
                                                    devices.dut.pre_login_message).verify_result()
        TestToolkit.tested_api = ApiType.NVUE
        message_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.message.show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(message_output, SystemConsts.POST_LOGIN_MESSAGE,
                                                    devices.dut.post_login_message).verify_result()
        TestToolkit.tested_api = test_api

        nv_command.system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value=f'"{new_pre_login_msg}"',
                                      apply=True, dut_engine=engines.dut).verify_result()
        nv_command.system.message.set(op_param_name=SystemConsts.POST_LOGIN_MESSAGE, op_param_value=f'"{new_post_login_msg}"',
                                      apply=True, dut_engine=engines.dut).verify_result()
        time.sleep(3)
        message_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.message.show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(message_output, SystemConsts.PRE_LOGIN_MESSAGE,
                                                    new_pre_login_msg).verify_result()
        ValidationTool.verify_field_value_in_output(message_output, SystemConsts.POST_LOGIN_MESSAGE,
                                                    new_post_login_msg).verify_result()

    with allure.step('Run unset system message pre-login command and verify that pre-login is updated'):
        nv_command.system.message.unset(op_param=SystemConsts.PRE_LOGIN_MESSAGE, apply=True).verify_result()
        time.sleep(3)
        message_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.message.show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(message_output, SystemConsts.PRE_LOGIN_MESSAGE,
                                                    devices.dut.pre_login_message).verify_result()
        logging.info("Verify the post-login was not affected")
        TestToolkit.tested_api = ApiType.NVUE
        message_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.message.show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(message_output, SystemConsts.POST_LOGIN_MESSAGE,
                                                    new_post_login_msg).verify_result()
        TestToolkit.tested_api = test_api

    with allure.step('Run unset system message post-login command and verify that pre-login is updated'):
        nv_command.system.message.unset(op_param=SystemConsts.POST_LOGIN_MESSAGE, apply=True).verify_result()
        time.sleep(3)
        TestToolkit.tested_api = ApiType.NVUE
        message_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.message.show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(message_output, SystemConsts.POST_LOGIN_MESSAGE,
                                                    devices.dut.post_login_message).verify_result()
        TestToolkit.tested_api = test_api


@pytest.mark.system
@pytest.mark.cumulus
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_show_system_reboot(test_api, engines, devices, nv_command):
    """
    Run show system reboot command and verify the reboot history and reason values
        Test flow:
            1. run show system reboot
            2. validate all fields have values
            3. reboot the switch
            5. run show system reboot
            6. validate all fields have the new values
    """
    TestToolkit.tested_api = test_api

    with allure.step('Run show system reboot command and verify that each field has a value'):
        reboot_output = OutputParsingTool.parse_json_str_to_dictionary(
            nv_command.system.reboot.show(exempted_err_msgs=REBOOT_REASON_SHOW_EXEMPTED_ERR_MSGS)
        ).get_returned_value()
        assert reboot_output['reason'], "reason field is missing"


@pytest.mark.cumulus
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.air
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_show_system_memory(test_api, engines, devices, nv_command):
    """
    Run show system memory and verify there is a correlation between the different values,
    and the values are in appropriate range.
        Test flow:
            1. run show system memory
            2. verify both keys (Physical and Swap) are exist
            3. validate total value = (free + used) values and greater than 0
            4. validate Utilization percentages are not reaching 60% for both Physical and Swap types (physical > 0)
            5. validate utilization value = (used / total) * 100, for both Physical and Swap types
    """
    TestToolkit.tested_api = test_api

    with allure.step('Run show system memory command and verify that each field has a value'):
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.show("memory")).get_returned_value()
        assert set(output_dictionary.keys()) == {
            SystemConsts.MEMORY_PHYSICAL_KEY,
            SystemConsts.MEMORY_SWAP_KEY,
        }, "Unexpected memory keys"

        physical, swap = build_memory_stats(output_dictionary)

    with allure.step('Validate memory statistics'):
        validator = MemoryValidatorFactory.get_validator(devices)
        validator.validate(physical, swap)

    with allure.step('Validate utilization thresholds'):
        for name, mem in [("Physical", physical), ("Swap", swap)]:
            assert SystemConsts.MEMORY_PERCENT_THRESH_MIN <= mem.utilization < \
                SystemConsts.MEMORY_PERCENT_THRESH_MAX, \
                f"{name} utilization out of range"

            if mem.total > 0:
                mem.validate_utilization(name)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.air
@pytest.mark.cumulus
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_show_system_cpu(test_api, engines, devices, nv_command):
    """
    Run show system memory and verify there is a correlation between the different values,
    and the values are in appropriate range.
        Test flow:
            1. run show system memory
            2. verify 3 keys (core-count, model and utilization) are exist
            3. verify switch CPU core-count matches the switch type
            4. validate Utilization percentages are not reaching 30%
    """
    TestToolkit.tested_api = test_api

    with allure.step('Run show system cpu command and verify that each field has a value'):
        output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.show("cpu")).get_returned_value()

        for key, value in output_dictionary.items():
            assert value is not None and value != "", f"Field '{key}' is empty or None"

        expected_keys = {
            SystemConsts.CPU_CORE_COUNT_KEY,
            SystemConsts.CPU_CORES,
            SystemConsts.CPU_LOAD_AVERAGE_KEY,
            SystemConsts.CPU_MODEL_KEY,
            SystemConsts.CPU_TOTAL_UTILIZATION_KEY,
        }
        assert set(output_dictionary.keys()) == expected_keys, (
            f"Unexpected keys: {output_dictionary.keys()}"
        )
        with allure.step('Verify core-count'):
            verify_core_count(devices, output_dictionary)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
@pytest.mark.air
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_system_contact_set(test_api, engines, nv_command):
    """
    Run show system message command and verify the required message
        Test flow:
            1. Run set system contact
            2. Run system show
            3. Validate system contact is as set in step 1
            4. Run unset system contact
            5. Run system show
            6. Validate system contact is not present in system show
    """
    TestToolkit.tested_api = test_api

    try:
        help_system_contact_location(engines, nv_command.system, SystemConsts.CONTACT)

    finally:
        clear_system_contact_and_location(nv_command.system)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
@pytest.mark.air
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_system_location_set(test_api, engines, nv_command):
    """
    Run show system message command and verify the required message
        Test flow:
            1. Run set system location
            2. Run system show
            3. Validate system location is as set in step 1
            4. Run unset system location
            5. Run system show
            6. Validate system location is not present in system show
    """
    TestToolkit.tested_api = test_api

    try:
        help_system_contact_location(engines, nv_command.system, SystemConsts.LOCATION)

    finally:
        clear_system_contact_and_location(nv_command.system)


@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
def test_factory_reset_for_system_contact_location(engines, nv_command, devices):
    """
    Run factory reset system command and verify the system contact and location fields are removed from system show
        Test flow:
            1. Run 'nv set system contact <args>>'
            2. Run 'nv set system location <args>>'
            4. Run 'nv show system' and verify system contact and location are set
            5. Run system factory reset
            6. Run 'nv show system' and verify systems contact and location fields are removed
    """
    test_name = "test_factory_reset_for_system_contact_location"
    system = System()

    try:
        with allure.step('Run set system contact command and apply config'):
            nv_command.system.set(op_param_name=SystemConsts.CONTACT, op_param_value="contact_info", apply=True,
                                  dut_engine=engines.dut).verify_result()

        with allure.step('Verify system contact is set'):
            system_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(system_output, SystemConsts.CONTACT, "contact_info").\
                verify_result()

        with allure.step('Run set system location command and apply config'):
            nv_command.system.set(op_param_name=SystemConsts.LOCATION, op_param_value="location_info", apply=True,
                                  dut_engine=engines.dut).verify_result()

        with allure.step('Verify system location is set'):
            system_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(system_output, SystemConsts.LOCATION, "location_info").\
                verify_result()
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        with allure.step("Run reset factory with keep basic param"):
            execute_reset_factory(engines, system, devices.dut.reset_factory, "keep basic", current_time, test_name=test_name)

        with allure.step("Verify the setup is functional"):
            verify_the_setup_is_functional(system, engines)

        with allure.step('Validate system contact is back to default'):
            system_output = OutputParsingTool.parse_json_str_to_dictionary(nv_command.system.show()).get_returned_value()
            assert system_output[SystemConsts.CONTACT] == devices.dut.system_default_value_dict[SystemConsts.CONTACT], "System contact in system show is {} instead of default value".\
                format(devices.dut.system_default_value_dict[SystemConsts.CONTACT])

        with allure.step('Validate system location is back to default'):
            assert system_output[SystemConsts.LOCATION] == devices.dut.system_default_value_dict[SystemConsts.LOCATION], "System location in system show is {} instead of" \
                "default value".format(devices.dut.system_default_value_dict[SystemConsts.LOCATION])

    finally:
        clear_system_contact_and_location(nv_command.system)


def clear_system_contact_and_location(system):
    device = TestToolkit.get_device()
    with allure.step('Unset the system contact'):
        system.unset(SystemConsts.CONTACT, apply=True).verify_result()

    with allure.step('Unset the system location'):
        system.unset(SystemConsts.LOCATION, apply=True).verify_result()

    with allure.step('Validate system contact is back to default'):
        system_output = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
        assert system_output[SystemConsts.CONTACT] == device.system_default_value_dict[SystemConsts.CONTACT], "System contact in system show is {} instead of default value". \
            format(device.system_default_value_dict[SystemConsts.CONTACT])

    with allure.step('Validate system location is back to default'):
        system_output = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
        assert system_output[SystemConsts.LOCATION] == device.system_default_value_dict[SystemConsts.LOCATION], "System location in system show is {} instead of default value". \
            format(device.system_default_value_dict[SystemConsts.LOCATION])


def help_system_contact_location(engines, system, field_name):
    device = TestToolkit.get_device()
    with allure.step('Set system {} command and verify that contact is updated'.format(field_name)):
        with allure.step('Set new system {}'.format(field_name)):
            field_info = field_name + "info"
            system.set(op_param_name=field_name, op_param_value=field_info, apply=True,
                       dut_engine=engines.dut).verify_result()

        with allure.step('Run show system and validate system {}'.format(field_name)):
            system_output = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(system_output, field_name, field_info).\
                verify_result()

    with allure.step('Unset system {} command and verify that {} field is default'.format(field_name, field_name)):
        with allure.step('Unset the system {}'.format(field_name)):
            system.unset(field_name, apply=True).verify_result()

        with allure.step('Validate system {} is back to default'.format(field_name)):
            system_output = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
            assert system_output[field_name] == device.system_default_value_dict[field_name], "System {} in system show is {} instead of default value".\
                format(field_name, device.system_default_value_dict[field_name])


def verify_core_count(devices, output_dictionary):
    """
    Verify core-count and model
    """
    if devices.dut.is_eth():
        # Verify core-count and model
        assert 0 < output_dictionary[SystemConsts.CPU_CORE_COUNT_KEY] == devices.dut.core_count, \
            "CPU core-count must be greater than 0 and match expected device core-count"
        assert output_dictionary[SystemConsts.CPU_MODEL_KEY], "CPU model must be non-empty"

        # Verify total-utilization
        assert SystemConsts.CPU_PERCENT_THRESH_MIN < output_dictionary[SystemConsts.CPU_TOTAL_UTILIZATION_KEY] < SystemConsts.CPU_PERCENT_THRESH_MAX, \
            "CPU total-utilization is out of range"

        # Verify load-average fields
        load_avg = output_dictionary[SystemConsts.CPU_LOAD_AVERAGE_KEY]
        assert 0 <= load_avg["one-minute"] and 0 <= load_avg["five-minute"] and 0 <= load_avg["fifteen-minute"], \
            "load-average values must be non-negative"

        # Verify cores count and utilization
        cores = output_dictionary[SystemConsts.CPU_CORES]
        assert len(cores) == output_dictionary[SystemConsts.CPU_CORE_COUNT_KEY], \
            "Number of cores must match core-count"
        for cpu_name, cpu_data in cores.items():
            assert 0 <= cpu_data["utilization"] <= 100, \
                f"Core {cpu_name} utilization must be between 0 and 100"
    else:
        assert output_dictionary[SystemConsts.CPU_CORE_COUNT_KEY] == devices.dut.core_count, \
            "Unexpected switch core-count"
        utilization = output_dictionary[SystemConsts.CPU_TOTAL_UTILIZATION_KEY]
        assert SystemConsts.CPU_PERCENT_THRESH_MIN < utilization < SystemConsts.CPU_PERCENT_THRESH_MAX, \
            "utilization percentage is out of range"
