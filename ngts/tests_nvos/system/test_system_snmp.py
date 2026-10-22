import logging
import re
import pytest

from retry import retry

from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.tools.test_utils import allure_utils as allure
import random
import string
import time
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.HostMethods import HostMethods
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_constants.constants_nvos import ImageConsts, SystemConsts, NvosConst, DatabaseConst
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import InterfaceConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.ResultObj import ResultObj


@pytest.mark.system
def test_snmp_default_values_fields(engines, devices, test_name):
    """
    Test flow:
        1. Check snmp output default values
        2. Enable snmp
        3. Check default values after enable
        4. Change location/contact, verify changes
        5. Unset
    """
    system = System(None)
    with allure.step('Show snmp and verify default values'):
        system_snmp_output = OutputParsingTool.parse_json_str_to_dictionary(system.snmp_server.show())\
            .get_returned_value()

        with allure.step("Verify default values"):
            ValidationTool.validate_fields_values_in_output(SystemConsts.SNMP_OUTPUT_FIELDS,
                                                            SystemConsts.SNMP_DEFAULT_VALUES,
                                                            system_snmp_output).verify_result()
            logging.info("All expected values were found")

    with allure.step("Enable snmp"):
        def enable_snmp():
            HostMethods.start_snmp_server(
                engine=engines.dut,
                state=NvosConst.ENABLED,
                readonly_community='qwerty12',
                listening_address='all',
            )
            HostMethods.wait_for_snmp_is_running(system)
            return ResultObj(True)

        result_obj, duration = OperationTime.save_duration(
            "enable snmp",
            '',
            test_name,
            enable_snmp,
        )
        OperationTime.verify_operation_time(duration, "enable snmp", devices).verify_result()

    with allure.step('Verify fields and values after snmp enabled'):
        listening_address_output = OutputParsingTool.parse_json_str_to_dictionary(
            system.snmp_server.listening_address.show()).get_returned_value()
        ValidationTool.compare_values(listening_address_output, SystemConsts.SNMP_ENABLED_DEFAULT_LISTENING_ADDRESS).verify_result()
        read_only_community_output = OutputParsingTool.parse_json_str_to_dictionary(
            system.snmp_server.readonly_community.show()).get_returned_value()
        assert 'qwerty12' not in read_only_community_output, 'snmp community not encrypted'
        read_only_community_output = OutputParsingTool.parse_json_str_to_dictionary(
            system.snmp_server.readonly_community.show('qwerty12')).get_returned_value()
        assert 'qwerty12' not in read_only_community_output, 'snmp community not encrypted'
        system_snmp_output = OutputParsingTool.parse_json_str_to_dictionary(system.snmp_server.show())\
            .get_returned_value()
        ValidationTool.validate_fields_values_in_output([SystemConsts.SNMP_REFRESH_INTERVAL,
                                                         SystemConsts.SNMP_STATE,
                                                         SystemConsts.SNMP_LISTENING_ADDRESS],
                                                        [SystemConsts.SNMP_DEFAULT_REFRESH_INTERVAL,
                                                         SystemConsts.SNMP_ENABLED_STATE,
                                                         SystemConsts.SNMP_ENABLED_DEFAULT_LISTENING_ADDRESS],
                                                        system_snmp_output).verify_result()
        logging.info("All expected fields were found")

    with allure.step("Change snmp contact/location"):
        system.snmp_server.set(SystemConsts.SNMP_SYSTEM_CONTACT, 'a1').verify_result()
        system.snmp_server.set(SystemConsts.SNMP_SYSTEM_LOCATION, 'b2').verify_result()
        NvueGeneralCli.apply_config(engines.dut)
        logging.info("Snmp enabled successfully")

        with allure.step('Verify fields and values after contact/location/set'):
            system_snmp_output = OutputParsingTool.parse_json_str_to_dictionary(system.snmp_server.show())\
                .get_returned_value()
            ValidationTool.validate_fields_values_in_output([SystemConsts.SNMP_SYSTEM_CONTACT,
                                                             SystemConsts.SNMP_SYSTEM_LOCATION],
                                                            ['a1', 'b2'],
                                                            system_snmp_output).verify_result()

    with allure.step("Change snmp contact/location to max string length"):
        length = 255
        letters = string.ascii_letters + string.digits
        max_length_string = ''.join(random.choice(letters) for _ in range(length))
        system.snmp_server.set(SystemConsts.SNMP_SYSTEM_CONTACT, max_length_string).verify_result()
        system.snmp_server.set(SystemConsts.SNMP_SYSTEM_LOCATION, max_length_string).verify_result()
        NvueGeneralCli.apply_config(engines.dut)
        logging.info("Snmp enabled successfully")

        with allure.step('Verify fields and values after contact/location/name/set'):
            system_snmp_output = OutputParsingTool.parse_json_str_to_dictionary(system.snmp_server.show())\
                .get_returned_value()
            ValidationTool.validate_fields_values_in_output([SystemConsts.SNMP_SYSTEM_CONTACT,
                                                             SystemConsts.SNMP_SYSTEM_LOCATION],
                                                            [max_length_string, max_length_string],
                                                            system_snmp_output).verify_result()

    with allure.step("Unset snmp"):
        system.snmp_server.listening_address.unset().verify_result()
        system.snmp_server.readonly_community.unset().verify_result()
        system.snmp_server.unset('state').verify_result()
        system.snmp_server.unset('system-contact').verify_result()
        system.snmp_server.unset('system-location').verify_result()
        system.snmp_server.unset('auto-refresh-interval').verify_result()
        NvueGeneralCli.apply_config(engines.dut)
        HostMethods.wait_for_snmp_is_running(system, SystemConsts.SNMP_DEFAULT_STATE)

        with allure.step('Verify values changed to default after unset'):
            system_snmp_output = OutputParsingTool.parse_json_str_to_dictionary(system.snmp_server.show())\
                .get_returned_value()
            ValidationTool.validate_fields_values_in_output(SystemConsts.SNMP_OUTPUT_FIELDS,
                                                            SystemConsts.SNMP_DEFAULT_VALUES,
                                                            system_snmp_output).verify_result()


@pytest.mark.system
def test_system_snmp_negative(engines, players, topology_obj):
    """
    Test flow:
        1. Check enable snmp without listening address
        2. Check enable snmp with ip 0.0.0.0/127.0.0.1 instead of all or localhost
        3. Check enable snmp with invalid value for listening address and port
        4. Check enable snmp with already used udp port and check error in the log
        5. Negative testing for contact/location/name
        6. Check snmpget with wrong port and community
        7. Unset
    """
    skip_if_engines_does_not_exist_in_setup([NvosConst.HOST_HA], engines)
    system = System(None)
    host_engine = engines.ha
    with allure.step("Check apply without listening address"):
        system.snmp_server.set('state', 'enabled').verify_result()
        system.snmp_server.set('readonly-community', 'qwerty12').verify_result()
        NvueGeneralCli.apply_config(engines.dut, validate_apply_message='Must configure at least 1 listening address')

    with allure.step("Check apply without listening address"):
        system.snmp_server.set('listening-address', '0.0.0.0').verify_result()
        NvueGeneralCli.apply_config(engines.dut,
                                    validate_apply_message="Listening-address 0.0.0.0 should be specified by the keyword 'all'")
        system.snmp_server.unset('listening-address').verify_result()
        system.snmp_server.set('listening-address', '127.0.0.1').verify_result()
        NvueGeneralCli.apply_config(engines.dut,
                                    validate_apply_message="Listening-address 127.0.0.1 should be specified by the keyword 'localhost'")
        system.snmp_server.unset('listening-address').verify_result()

    with allure.step("Negative testing for refresh interval"):
        system.snmp_server.set('auto-refresh-interval', 'a1').verify_result(False)

    with allure.step("Verify snmp not working with already used port"):
        logging.info("Rotate logs")
        system.log.rotate_logs()
        system.snmp_server.set('listening-address', 'all port 123').verify_result()
        NvueGeneralCli.apply_config(engines.dut, option='--assume-no', validate_apply_message="'snmpd' is not running")

        with allure.step('Verify snmp not running with booked port for ntp'):
            system_snmp_output = OutputParsingTool.parse_json_str_to_dictionary(system.snmp_server.show()) \
                .get_returned_value()
            ValidationTool.validate_fields_values_in_output([SystemConsts.SNMP_STATE], [SystemConsts.SNMP_DEFAULT_STATE],
                                                            system_snmp_output).verify_result()

    with allure.step("Configure snmp listening address all"):
        system.snmp_server.unset('listening-address').verify_result()
        system.snmp_server.set('listening-address', 'all').verify_result()
        NvueGeneralCli.apply_config(engines.dut)

    with allure.step("Negative testing for contact/location"):
        length = 256
        letters = string.ascii_letters + string.digits
        max_length_string = ''.join(random.choice(letters) for _ in range(length))
        system.snmp_server.set(SystemConsts.SNMP_SYSTEM_CONTACT, max_length_string).verify_result(False)
        system.snmp_server.set(SystemConsts.SNMP_SYSTEM_LOCATION, max_length_string).verify_result(False)

    with allure.step("Snmpget with wrong port and community"):
        ip_address = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific'][
            'ip_address']
        host_output = HostMethods.host_snmp_get(host_engine, ip_address, port=':162')
        assert 'Timeout' in host_output, 'snmp get with wrong port returned output'
        host_output = HostMethods.host_snmp_get(host_engine, ip_address, community='a1')
        assert 'Timeout' in host_output, 'snmp get with wrong community returned output'

    with allure.step("SNMP unset"):
        system.snmp_server.unset(apply=True).verify_result()


def test_system_snmp_functional(engines, topology_obj):
    """
    Test flow:
        1. Enable snmp
        2. Check system name with snmp get
        3. Check listening address ipv4, ipv6, all, allv6
        4. Enable auto refresh
        5. Set description on eth0 interface
        6. Check with snmpwalk description before autorefresh
        7. Check with snmpwalk description after autorefresh
    """
    skip_if_engines_does_not_exist_in_setup([NvosConst.HOST_HA], engines)
    system = System(None)
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    host_engine = engines.ha
    noga_query_data = topology_obj.players['dut']['attributes'].noga_query_data['attributes']
    ip_address = noga_query_data['Specific']['ip_address']
    dhcp_hostname = HostMethods.get_dhcp_hostname(topology_obj)
    with allure.step("Enable snmp server"):
        HostMethods.start_snmp_server(engine=engines.dut, state=NvosConst.ENABLED, readonly_community='qwerty12',
                                      listening_address=ip_address)
        HostMethods.wait_for_snmp_is_running(system)

        with allure.step("Check snmpget with listening eth0 ip address"):
            host_output = HostMethods.host_snmp_get(host_engine, ip_address)
            HostMethods.assert_snmp_system_name_matches_dhcp(host_output, dhcp_hostname)

    with allure.step("Enable snmp"):
        with allure.step('Get ipv6 address'):
            ipv6_address = mgmt_port.interface.ipv6.get_primary_ip_address()

        with allure.step('Set ipv6 listening address'):
            system.snmp_server.set('listening-address', ipv6_address, apply=True).verify_result()

        with allure.step("Check snmpget with listening eth0 ip_v6 address"):
            host_output = HostMethods.host_snmp_get(host_engine, ipv6_address)
            HostMethods.assert_snmp_system_name_matches_dhcp(host_output, dhcp_hostname)

        with allure.step("Configure listening address ipv6 all and do snmpget"):
            system.snmp_server.listening_address.unset(ipv6_address)
            system.snmp_server.set('listening-address', 'all-v6', apply=True).verify_result()
            host_output = HostMethods.host_snmp_get(host_engine, ipv6_address)
            HostMethods.assert_snmp_system_name_matches_dhcp(host_output, dhcp_hostname)

    with allure.step("Configure auto-refresh interval and do snmpget"):
        system.snmp_server.set('auto-refresh-interval', '8', apply=True).verify_result()

    with allure.step('Set possible description on mgmt port'):
        mgmt_port.interface.set(op_param_name='description', op_param_value='nvosdescription',
                                apply=True).verify_result()
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
            mgmt_port.interface.show()).get_returned_value()

        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=InterfaceConsts.DESCRIPTION,
                                                          expected_value='nvosdescription')

        with allure.step("Snmpwalk after autorefresh"):
            _wait_snmpwalk_nvos_description(host_engine, ip_address)

    with allure.step("SNMP unset"):
        system.snmp_server.unset(apply=True).verify_result()
        mgmt_port.interface.unset(op_param="description", apply=True).verify_result()


def test_system_snmp_redis_crash(engines, topology_obj):
    """
    Test flow:
        1. Enable snmp
        2. Change with redis cli snmp community value
        3. Check in show command changes
        4. Check that we can do snmpget with new community
        5. Unset
    """
    skip_if_engines_does_not_exist_in_setup([NvosConst.HOST_HA], engines)
    system = System(None)
    host_engine = engines.ha
    with allure.step("Enable snmp"):
        HostMethods.start_snmp_server(engine=engines.dut, state=NvosConst.ENABLED, readonly_community='qwerty12',
                                      listening_address='all')
        HostMethods.wait_for_snmp_is_running(system)

    with allure.step("Rewrite value for snmp community with redis db"):
        with allure.step('Write value to snmp community via redis cli'):
            redis_cli_output = Tools.DatabaseTool.sonic_db_cli_hset(engine=engines.dut, asic="",
                                                                    db_name=DatabaseConst.CONFIG_DB_NAME,
                                                                    db_config="SNMP_COMMUNITY|qwerty12",
                                                                    param="TYPE", value="aa")
            assert redis_cli_output != 0, "Redis command failed"

        with allure.step("Snmpget after rewrite type of community"):
            noga_query_data = topology_obj.players['dut']['attributes'].noga_query_data['attributes']
            ip_address = noga_query_data['Specific']['ip_address']
            dhcp_hostname = HostMethods.get_dhcp_hostname(topology_obj)
            host_output = HostMethods.host_snmp_get(host_engine, ip_address)
            HostMethods.assert_snmp_system_name_matches_dhcp(host_output, dhcp_hostname)

    with allure.step("SNMP unset"):
        system.snmp_server.unset(apply=True).verify_result()


def test_system_snmp_load_test(engines, topology_obj):
    """
    Test flow:
        1. Configure snmp
        2. Stress switch with snmpwalk command
        3. Check snmp work after snmpwalk
    """
    skip_if_engines_does_not_exist_in_setup([NvosConst.HOST_HA, NvosConst.HOST_HB], engines)
    system = System(None)
    host_a_engine = engines.ha
    host_b_engine = engines.hb
    with allure.step("Enable snmp"):
        HostMethods.start_snmp_server(engine=engines.dut, state=NvosConst.ENABLED, readonly_community='qwerty12',
                                      listening_address='all')
        HostMethods.wait_for_snmp_is_running(system)

    with allure.step("Enable snmp"):
        ip_address = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific'][
            'ip_address']
        for _ in range(10):
            HostMethods.host_snmp_walk(host_a_engine, ip_address)
            HostMethods.host_snmp_walk(host_b_engine, ip_address)

    with allure.step('Verify snmp and system work fine after stress with snpwalk'):
        system_snmp_output = OutputParsingTool.parse_json_str_to_dictionary(system.snmp_server.show())\
            .get_returned_value()
        ValidationTool.validate_fields_values_in_output([SystemConsts.SNMP_STATE], [SystemConsts.SNMP_ENABLED_STATE],
                                                        system_snmp_output).verify_result()
        logging.info("All expected fields were found")

    with allure.step("SNMP unset"):
        system.snmp_server.unset(apply=True).verify_result()


@pytest.mark.system
def test_system_snmp_mismatch_system_image(engines):
    """
    Test flow:
        1. Enable snmp
        2. Get nvos version via snmpget
        3. Get nvos version via nv show system image
        4. Compare
    """
    snmp_descr_nvos_pattern = r'NVOS Software Version:\s*(\w+(-|.)[\d.-]+)'
    sysDescr_OID = '1.3.6.1.2.1.1.1.0'
    system = System()
    ip_address = engines.dut.ip

    with allure.step("Enable SNMP"):
        HostMethods.start_snmp_server(engine=engines.dut, state=NvosConst.ENABLED, readonly_community='qwerty12',
                                      listening_address=ip_address)
        HostMethods.wait_for_snmp_is_running(system)

    with allure.step("Get Version Via SNMP sysDescr"):
        snmpget_output: str = HostMethods.host_snmp_get(engines.sonic_mgmt, ip_address, get_param=sysDescr_OID)
        match = re.search(snmp_descr_nvos_pattern, snmpget_output)
        assert match is not None, 'NVOS Software Version not in SNMP sysDescr'
        snmp_nvos_version = match.group(1)

    with allure.step("Get Version Via NVUe System Image"):
        system_image_output = OutputParsingTool.parse_json_str_to_dictionary(system.version.show()).get_returned_value()
        system_image_nvos_version = system_image_output["image"][ImageConsts.BUILD_ID]
        assert system_image_nvos_version, "system_image_nvos_version is None - parsing failed."

    with allure.step("Compare Version Outputs"):
        assert snmp_nvos_version == system_image_nvos_version, f"Version outputs mismatch! snmp returned: {snmp_nvos_version} NVUe returned: {system_image_nvos_version}"

    with allure.step("SNMP unset"):
        system.snmp_server.unset(apply=True).verify_result()


def skip_if_engines_does_not_exist_in_setup(required_engines_list, engines):
    not_existed_engines = []
    for engine_name in required_engines_list:
        if engine_name not in engines:
            not_existed_engines.append(engine_name)
    if not_existed_engines:
        pytest.skip("Skip this test cause don't have the required engines {}".format(not_existed_engines))


@retry(Exception, tries=3, delay=3)
def _wait_snmpwalk_nvos_description(host_engine, ip_address):
    host_output = HostMethods.host_snmp_walk(host_engine, ip_address, mib='1.3.6.1.2.1.31.1.1.1.18',
                                             param='| grep nvosdescription')
    assert 'nvosdescription' in host_output, 'snmp get with wrong port returned output'
