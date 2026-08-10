import allure
import json
import logging
import os
import re
import ipaddress
import time
import random
from retry.api import retry_call
import pytest

from devts.infra.tools.validations.traffic_validations.ping.ping_runner import PingChecker
from devts.infra.tools.validations.traffic_validations.scapy.scapy_runner import ScapyChecker
from ngts.config_templates.lag_lacp_config_template import LagLacpConfigTemplate
from ngts.config_templates.ip_config_template import IpConfigTemplate
from ngts.config_templates.vlan_config_template import VlanConfigTemplate
from ngts.config_templates.interfaces_config_template import InterfaceConfigTemplate
from ngts.cli_util.verify_cli_show_cmd import verify_show_cmd
from ngts.conftest import cleanup_last_config_in_stack
from ngts.helpers.reboot_reload_helper import get_supported_reboot_reload_types_list
from ngts.helpers.interface_helpers import speed_string_to_int_in_mb
from ngts.helpers.sonic_branch_helper import is_sanitizer_image
from ngts.helpers import json_file_helper
from ngts.constants.constants import SonicConst
from ngts.helpers.interface_helpers import get_service_port

logger = logging.getLogger()
PORTCHANNEL_NAME = 'PortChannel1111'
BASE_PKT = 'Ether(dst="{}")/IP(src="50.0.0.2",dst="50.0.0.3")/{}()/Raw()'
SDK_LAG_QUERY_SCRIPT = 'get_sdk_lag_count.py'
SDK_LAG_QUERY_TIMEOUT_SEC = 120
SDK_LAG_QUERY_INTERVAL_SEC = 10
CHIP_LAGS_LIM = {
    'SPC': 64,
    'SPC2': 110,    # TODO SDK support 128, but currently 128 doesn't work
    'SPC3': 110,     # TODO SDK support 128, but currently 128 doesn't work
    'SPC4': 110,     # TODO SDK support 128, but currently 128 doesn't work
    'SPC5': 110,     # TODO SDK support 128, but currently 128 doesn't work
    'SPC6': 235       # TODO SDK support 512, but currently 236 doesn't work
}
CHIP_LAG_MEMBERS_LIM = {
    'SPC': 32,
    'SPC2': 64,
    'SPC3': 64,
    'SPC4': 64,
    'SPC5': 64,
    'SPC6': 128
}


@pytest.mark.reboot_reload
@allure.title('LAG_LACP core functionality and reboot')
def test_core_functionality_with_reboot(topology_obj, cli_objects, traffic_type, interfaces, engines, cleanup_list,
                                        platform_params, is_simx, lag_config_order, l3_route_config):
    """
    This test case will check the base functionality of LAG/LACP feature.
    Config base configuration as in the picture below.
    Validate port channel and links state.
    Disable port 1 on the host. Validate port channel was affected.
    Disable port 2 and enable port 1 on the host. Validate port channel was affected.
    Enable port 2 on the host. Validate port channel was affected.
    Reboot switch and validate.
    :param topology_obj: topology object
    :param cli_objects: cli_objects object
    :param traffic_type: the type of the traffic
    :param interfaces: interfaces fixture
    :param engines: engines fixture
    :param cleanup_list: list with functions to cleanup
    :param lag_config_order: LAG configuration order (add_lag_first or add_member_first)
    :return: raise assertion error on unexpected behavior

                                                dut
                                       ------------------------------
                     ha                |    Vlan50 50.0.0.1/24      |                                  hb
              ------------------       |                            |dut_hb1 in lag               ------------------
              |                |       |dut_ha1 vlan50 trunk        |----------------------------|                |
              |     ha_dut1.50 |-------|                            |             hb_dut1 in bond|  bond0.50      |
              |     50.0.0.2/24|       |                            |             hb_dut2 in bond|  50.0.0.3/24   |
              |                |       |            PortChannel1111 |----------------------------|                |
              |     ha_dut2    |-------|              vlan 50 trunk |dut_hb2 in lag              ------------------
              |     51.0.0.2/24|       |dut_ha2 51.0.0.1/24         |
              ------------------       ------------------------------
              L2: ha_dut1.50 -> Vlan50 -> PortChannel1111 -> bond0.50
              L3: ha_dut2 -> dut_ha2 -> route -> Vlan50 -> PortChannel1111 -> bond0.50
    """
    dut_cli = topology_obj.players['dut']['cli']
    logger.info("Executing test with order: {}".format(lag_config_order))

    cleanup_list.append((cli_objects.hb.interface.enable_interface, (interfaces.dut_hb_1,)))
    cleanup_list.append((cli_objects.hb.interface.enable_interface, (interfaces.dut_hb_2,)))
    cleanup_list.append((cli_objects.hb.interface.enable_interface, ('bond0',)))

    # LAG/LACP config which will be used in this test
    lag_lacp_config_dict = {
        'dut': [{'type': 'lacp', 'name': PORTCHANNEL_NAME, 'members': [interfaces.dut_hb_1, interfaces.dut_hb_2]}]
    }

    # VLAN config which will be used in this test
    vlan_config_dict = {'vlan_id': 50,
                        'vlan_member': PORTCHANNEL_NAME
                        }

    if lag_config_order == "add_lag_first":
        dut_cli.lag.create_lag_interface(PORTCHANNEL_NAME)
        dut_cli.vlan.add_port_to_vlan(vlan_config_dict['vlan_member'], vlan_config_dict['vlan_id'])
        for member in [interfaces.dut_hb_1, interfaces.dut_hb_2]:
            dut_cli.lag.add_port_to_port_channel(member, PORTCHANNEL_NAME)
        cleanup_list.append((LagLacpConfigTemplate.cleanup, (topology_obj, lag_lacp_config_dict)))
        cleanup_list.append((dut_cli.vlan.del_port_from_vlan,
                             (vlan_config_dict['vlan_member'], vlan_config_dict['vlan_id'])))
    else:
        add_lag_conf(topology_obj, lag_lacp_config_dict, cleanup_list)
        add_vlan_conf(engines.dut, dut_cli, vlan_config_dict, cleanup_list)

    try:
        with allure.step('Validate the PortChannel status'):
            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  'Up',
                                                  [(interfaces.dut_hb_1, 'S'), (interfaces.dut_hb_2, 'S')],
                                                  tries=3)

        with allure.step('Validate the base functionality of LAG - traffic'):
            # PING below need to prevent issue when packet not forwarded to host from switch
            validation_ping = {'sender': 'ha', 'args': {'count': 3, 'dst': '50.0.0.1'}}
            PingChecker(topology_obj.players, validation_ping).run_validation()
            traffic_validation(topology_obj, traffic_type)
            # PING for L3 to trigger ARP on dut_ha2 interface
            l3_validation_ping = {'sender': 'ha', 'args': {'count': 3, 'dst': l3_route_config['dut_l3_ip'],
                                                           'src_ip': l3_route_config['ha_l3_ip']}}
            PingChecker(topology_obj.players, l3_validation_ping).run_validation()
            l3_traffic_validation(topology_obj, l3_route_config)

        with allure.step('STEP1: Disable interface 1 on host, traffic should pass via interface 2'):
            cli_objects.hb.interface.disable_interface(interfaces.hb_dut_1)
            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  'Up',
                                                  [(interfaces.dut_hb_1, 'D'), (interfaces.dut_hb_2, 'S')])
            traffic_validation(topology_obj, traffic_type)
            l3_traffic_validation(topology_obj, l3_route_config)

        with allure.step('STEP2: Enable interface 1 and disable interface 2 on host,'
                         ' traffic should pass via interface 1'):
            cli_objects.hb.interface.enable_interface(interfaces.hb_dut_1)
            cli_objects.hb.interface.disable_interface(interfaces.hb_dut_2)
            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  'Up',
                                                  [(interfaces.dut_hb_1, 'S'), (interfaces.dut_hb_2, 'D')])
            traffic_validation(topology_obj, traffic_type)
            l3_traffic_validation(topology_obj, l3_route_config)

        with allure.step('STEP3: Enable both interfaces on host'):
            cli_objects.hb.interface.enable_interface(interfaces.hb_dut_2)
            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  'Up',
                                                  [(interfaces.dut_hb_1, 'S'), (interfaces.dut_hb_2, 'S')])
            traffic_validation(topology_obj, traffic_type)
            l3_traffic_validation(topology_obj, l3_route_config)

        with allure.step('STEP4: Reboot dut'):
            dut_cli.general.save_configuration()
            reboot_types = get_supported_reboot_reload_types_list(platform=platform_params.platform)
            reboot_type = random.choice(reboot_types)
            if is_simx:
                reboot_type = 'reboot'
            dut_cli.general.reboot_reload_flow(r_type=reboot_type, topology_obj=topology_obj, platform_params=platform_params)

        with allure.step('STEP5: Validate port channel status and send traffic'):
            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  'Up',
                                                  [(interfaces.dut_hb_1, 'S'), (interfaces.dut_hb_2, 'S')])
            traffic_validation(topology_obj, traffic_type)
            l3_traffic_validation(topology_obj, l3_route_config)

        with allure.step('STEP6: Validate fallback parameter (default - false)'):
            cli_objects.hb.interface.disable_interface('bond0')
            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  'Dw',
                                                  [(interfaces.dut_hb_1, 'D'), (interfaces.dut_hb_2, 'D')])
            cli_objects.hb.interface.enable_interface('bond0')

        with allure.step('STEP7: Validate configuration of LAG with fallback parameter "true"'):
            cleanup_last_config_in_stack(cleanup_list)  # pop vlan cleanup from stack
            cleanup_last_config_in_stack(cleanup_list)  # remove LAG
            lag_lacp_config_dict = {
                'dut': [{'type': 'lacp', 'name': PORTCHANNEL_NAME,
                         'members': [interfaces.dut_hb_1, interfaces.dut_hb_2], 'params': '--fallback enable'}]
            }
            add_lag_conf(topology_obj, lag_lacp_config_dict, cleanup_list)
            add_vlan_conf(engines.dut, dut_cli, vlan_config_dict, cleanup_list)
            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  'Up',
                                                  [(interfaces.dut_hb_1, 'S'), (interfaces.dut_hb_2, 'S')])
            traffic_validation(topology_obj, traffic_type)
            l3_traffic_validation(topology_obj, l3_route_config)

        with allure.step('STEP8: Validate functionality of LAG with fallback parameter "true"'):
            config_bond_type_lag(topology_obj, interfaces, cleanup_list)
            logger.info('Wait 120 seconds for LACP timeout')
            time.sleep(120)
            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  'Up',
                                                  [(interfaces.dut_hb_1, 'S'), (interfaces.dut_hb_2, 'S')])
            traffic_validation(topology_obj, traffic_type)
            l3_traffic_validation(topology_obj, l3_route_config)

    except BaseException as err:
        raise AssertionError(err)


@pytest.mark.reboot_reload
@allure.title('Test port cannot be added to LAG')
def test_port_cannot_be_added_to_lag(topology_obj, traffic_type, interfaces, engines, cleanup_list,
                                     lag_lacp_config_with_two_bonds):
    """
    This test case will check the interop of the port channel.
    Check 'ip', 'speed', 'other_lag', 'vlan' dependencies.
    Config dependency on a port. Trying to add the port to port channel.
    Validate expected error message.
    :param topology_obj: topology object
    :param traffic_type: the type of the traffic
    :param interfaces: interfaces fixture
    :param engines: engines fixture
    :param cleanup_list: list with functions to cleanup
    :return: raise assertion error on unexpected behavior
    """
    try:
        dut_cli = topology_obj.players['dut']['cli']

        # LAG/LACP config which will be used in this test
        lag_lacp_config_dict = {
            'dut': [{'type': 'lacp', 'name': PORTCHANNEL_NAME, 'members': [interfaces.dut_hb_1]}]
        }
        add_lag_conf(topology_obj, lag_lacp_config_dict, cleanup_list)

        # Add VLAN config
        vlan_config_dict = {'vlan_id': 50,
                            'vlan_member': PORTCHANNEL_NAME
                            }
        add_vlan_conf(engines.dut, dut_cli, vlan_config_dict, cleanup_list)

        dependency_list = ['ip', 'speed', 'other_lag', 'vlan']

        for dependency in dependency_list:
            check_dependency(topology_obj, dependency, cleanup_list)

        with allure.step('Validate the PortChannel status'):
            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  'Up',
                                                  [(interfaces.dut_hb_1, 'S')],
                                                  tries=3)

        with allure.step('Validate the traffic via the LAG'):
            # PING below need to prevent issue when packet not forwarded to host from switch
            validation_ping = {'sender': 'ha', 'args': {'count': 3, 'dst': '50.0.0.1'}}
            PingChecker(topology_obj.players, validation_ping).run_validation()
            traffic_validation(topology_obj, traffic_type)

    except BaseException as err:
        raise AssertionError(err)


@pytest.mark.reboot_reload
@allure.title('LAG min-links Test')
def test_lag_min_links(topology_obj, traffic_type, interfaces, engines, cleanup_list):
    """
    This test case will check the functionality of 'min-links' parameter.
    Checks that port channel in down state, until he have num of members < min-links parameter.
    :param topology_obj: topology object
    :param traffic_type: the type of the traffic
    :param interfaces: interfaces fixture
    :param engines: engines fixture
    :param cleanup_list: list with functions to cleanup
    :return: raise assertion error on unexpected behavior
    """
    try:
        dut_cli = topology_obj.players['dut']['cli']

        with allure.step('STEP1: Create PortChannel with min-links 1 and no members'):
            lag_config_dict = {
                'dut': [{'type': 'lacp', 'name': PORTCHANNEL_NAME, 'members': [], 'params': '--min-links 1'}]
            }
            add_lag_conf(topology_obj, lag_config_dict, cleanup_list)

        with allure.step('Validate the PortChannel status is Down'):
            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  'Dw',
                                                  [],
                                                  tries=5)
        cleanup_last_config_in_stack(cleanup_list)

        with allure.step('STEP2: Create PortChannel with min-links 1 and 1 member'):
            lag_config_dict = {
                'dut': [{'type': 'lacp',
                         'name': PORTCHANNEL_NAME,
                         'members': [interfaces.dut_hb_1],
                         'params': '--min-links 1'}]
            }
            add_lag_conf(topology_obj, lag_config_dict, cleanup_list)

        with allure.step('Validate the PortChannel status is Up, port status is Selected'):
            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  'Up',
                                                  [(interfaces.dut_hb_1, 'S')],
                                                  tries=5)
        cleanup_last_config_in_stack(cleanup_list)

        with allure.step('STEP3: Create PortChannel with min-links 2 and 1 member'):
            lag_config_dict = {
                'dut': [{'type': 'lacp',
                         'name': PORTCHANNEL_NAME,
                         'members': [interfaces.dut_hb_1],
                         'params': '--min-links 2'}]
            }
            add_lag_conf(topology_obj, lag_config_dict, cleanup_list)

        with allure.step('Validate the PortChannel status is Down, port status is Selected'):
            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  'Dw',
                                                  [(interfaces.dut_hb_1, 'S')],
                                                  tries=5)

        with allure.step('STEP4: Add second member to PortChannel with min-links 2'):
            lag_config_dict = {
                'dut': [{'type': 'lacp', 'name': PORTCHANNEL_NAME, 'members': [interfaces.dut_hb_2]}]
            }
            add_lag_conf(topology_obj, lag_config_dict, cleanup_list)

        with allure.step('Validate the PortChannel status is Up, both members status is Selected'):
            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  'Up',
                                                  [(interfaces.dut_hb_1, 'S'), (interfaces.dut_hb_2, 'S')],
                                                  tries=5)

        vlan_config_dict = {'vlan_id': 50,
                            'vlan_member': PORTCHANNEL_NAME
                            }
        add_vlan_conf(engines.dut, dut_cli, vlan_config_dict, cleanup_list)

        with allure.step('Validate the traffic via a LAG'):
            validation_ping = {'sender': 'ha', 'args': {'count': 3, 'dst': '50.0.0.1'}}
            PingChecker(topology_obj.players, validation_ping).run_validation()
            traffic_validation(topology_obj, traffic_type)
    except BaseException as err:
        raise AssertionError(err)


def exclude_service_ports(interfaces_info, service_ports):
    return {iface: info for iface, info in interfaces_info.items() if iface not in service_ports}


def exclude_dpu_interfaces(interfaces_info, platform):
    if '4280' in platform:
        dpu_interfaces = ['Ethernet224', 'Ethernet232', 'Ethernet240', 'Ethernet248']
        return {iface: info for iface, info in interfaces_info.items() if iface not in dpu_interfaces}
    return interfaces_info


@pytest.mark.reboot_reload
@allure.title('LAG members scale Test')
def test_lag_members_scale(topology_obj, interfaces, engines, cleanup_list, platform_params, tested_asic_index):
    """
    This test case will check the configuration of 1 port channel with max number of members.
    :param topology_obj: topology object
    :param interfaces: interfaces fixture
    :param engines: engines fixture
    :param cleanup_list: list with functions to cleanup
    :return: raise assertion error on unexpected behavior
    """
    try:
        dut_cli = topology_obj.players['dut']['cli']
        del_port_from_vlan(engines.dut, dut_cli, interfaces.dut_ha_1, '50', cleanup_list)

        chip_type = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific']['chip_type']
        max_lag_members = CHIP_LAG_MEMBERS_LIM[chip_type]
        all_ifaces_info = dut_cli.interface.parse_interfaces_status(asic_num=tested_asic_index)
        service_ports = get_service_port(platform_params.platform)
        all_ifaces_info = exclude_service_ports(all_ifaces_info, service_ports)
        all_ifaces_info = exclude_dpu_interfaces(all_ifaces_info, platform_params.platform)
        # Use the largest group of ports with a compatible hardware type.
        interfaces_types_dict = get_interfaces_by_type_dict(all_ifaces_info)
        test_ifaces_type = get_ifaces_type_which_has_bigger_ifaces_list(interfaces_types_dict)
        all_interfaces_with_same_type = interfaces_types_dict[test_ifaces_type]
        member_interfaces = random.sample(all_interfaces_with_same_type, min(max_lag_members,
                                                                             len(all_interfaces_with_same_type)))
        lb_member_interfaces = get_lag_lb_members(interfaces, member_interfaces)
        # Preserve ports intentionally left admin-down after setup bring-up identified link issues.
        admin_up_lb_member_interfaces = [
            interface for interface in lb_member_interfaces if all_ifaces_info[interface]['Admin'] == 'up'
        ]

        with allure.step('Set same speed to all LAG members'):
            dut_orig_ifaces_speeds = dut_cli.interface.get_interfaces_speed(all_interfaces_with_same_type)
            # Get minimal supported speed
            min_speed = min([speed_string_to_int_in_mb(speed) for speed in dut_orig_ifaces_speeds.values()])
            # If member speeds differ, set the selected members to the minimum speed for their interface type.
            members_speed = {}
            [members_speed.update({iface: dut_orig_ifaces_speeds[iface]}) for iface in member_interfaces]
            if not all(x == list(members_speed.values())[0] for x in list(members_speed.values())):
                interfaces_config_list = []
                for interface in member_interfaces:
                    interfaces_config_list.append({'iface': interface,
                                                   'speed': min_speed,
                                                   'original_speed': dut_orig_ifaces_speeds.get(interface, min_speed)})
                interfaces_config_dict = {
                    'dut': interfaces_config_list
                }
                add_interface_conf(topology_obj, interfaces_config_dict, cleanup_list)
                configured_speeds = dut_cli.interface.get_interfaces_speed(member_interfaces)
                unexpected_speeds = {
                    interface: speed for interface, speed in configured_speeds.items()
                    if speed_string_to_int_in_mb(speed) != min_speed
                }
                assert not unexpected_speeds, \
                    'Failed to set all LAG members to speed {}: {}'.format(min_speed, unexpected_speeds)

        with allure.step('Disable all member loopback interfaces'):
            dut_cli.interface.disable_interfaces(admin_up_lb_member_interfaces)
            cleanup_list.append((dut_cli.interface.enable_interfaces, (admin_up_lb_member_interfaces,)))

        with allure.step('Create PortChannel with all ports as members'):
            lag_config_dict = {
                'dut': [{'type': 'lacp', 'name': PORTCHANNEL_NAME, 'members': member_interfaces}]
            }
            add_lag_conf(topology_obj, lag_config_dict, cleanup_list)

        with allure.step('Check that all interfaces in Up state'):
            retry_call(dut_cli.interface.check_ports_status,
                       fargs=[list(set(member_interfaces).difference(lb_member_interfaces))],
                       tries=20, delay=15,
                       logger=logger)

        with allure.step('Validate members status in PortChannel'):
            expected_ports_status_list = []
            lag_status = 'Dw'
            for interface in member_interfaces:
                if interface in [interfaces.dut_hb_1, interfaces.dut_hb_2]:
                    expected_ports_status_list.append((interface, 'S'))
                    lag_status = 'Up'
                else:
                    expected_ports_status_list.append((interface, 'D'))

            verify_port_channel_status_with_retry(dut_cli,
                                                  engines.dut,
                                                  PORTCHANNEL_NAME,
                                                  lag_status,
                                                  expected_ports_status_list,
                                                  tries=10)
        with allure.step('Validate dockers status'):
            dut_cli.general.verify_dockers_are_up(platform_params=platform_params)

    except BaseException as err:
        raise AssertionError(err)


@pytest.mark.reboot_reload
@allure.title('LAGs scale Test')
def test_lags_scale(topology_obj, engines, cleanup_list, platform_params):
    """
    This test case will check the configuration of maximum number of port channels with ipv4&ipv6 addresses.
    :param topology_obj: topology object
    :param engines: engines fixture
    :param cleanup_list: list with functions to cleanup
    :return: raise assertion error on unexpected behavior
    """
    # Skip the case on ASAN image
    if is_sanitizer_image(topology_obj):
        pytest.skip("Skip performance and scalability tests on ASAN image")

    try:
        dut_cli = topology_obj.players['dut']['cli']

        wait_for_sdk_lag_count(engines.dut, expected_count=0)
        cleanup_list.append((wait_for_sdk_lag_count, (engines.dut, 0)))

        chip_type = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific']['chip_type']
        number_of_lags = CHIP_LAGS_LIM[chip_type]

        lag_config_dict, lag_expected_info, ip_config_dict, ip_expected_info \
            = get_lags_scale_configuration(number_of_lags)

        with allure.step('Create max number of PortChannels'):
            add_lag_conf(topology_obj, lag_config_dict, cleanup_list)

        with allure.step('Validation port channels were created'):
            retry_call(
                verify_port_channels_status,
                fargs=[dut_cli, engines.dut, lag_expected_info],
                tries=10,
                delay=5,
                logger=logger,
            )

        with allure.step('Validation of bug 2435816 - add and verify IPs on all PortChannels'):
            add_ip_conf(topology_obj, ip_config_dict, cleanup_list)
            verify_port_channels_ip(dut_cli, engines.dut, ip_expected_info)

        with allure.step('Reloading the DUT config using cmd: "config reload -y"'):
            dut_cli.general.save_configuration()
            dut_cli.general.reboot_reload_flow(r_type=SonicConst.CONFIG_RELOAD_CMD, topology_obj=topology_obj, platform_params=platform_params)

        with allure.step('Validation for bug 2435254 - validate lags and ips'
                         ' are configured properly after config reload'):
            retry_call(
                verify_port_channels_status,
                fargs=[dut_cli, engines.dut, lag_expected_info],
                tries=10,
                delay=5,
                logger=logger,
            )
            retry_call(
                verify_port_channels_ip,
                fargs=[dut_cli, engines.dut, ip_expected_info],
                tries=3,
                delay=10,
                logger=logger,
            )
    except BaseException as err:
        raise AssertionError(err)


def prepare_sdk_lag_query(dut_engine):
    script_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), SDK_LAG_QUERY_SCRIPT)
    dut_engine.copy_file(
        source_file=script_path,
        file_system='/tmp',
        dest_file=SDK_LAG_QUERY_SCRIPT,
        overwrite_file=True,
    )
    dut_engine.run_cmd(
        'docker cp /tmp/{script} syncd:/{script}'.format(
            script=SDK_LAG_QUERY_SCRIPT,
        ),
        validate=True,
    )


def get_sdk_lag_state(dut_engine):
    output = dut_engine.run_cmd(
        'docker exec syncd python3 /{script}'.format(
            script=SDK_LAG_QUERY_SCRIPT,
        ),
        validate=True,
    )
    for line in reversed(str(output).splitlines()):
        try:
            state = json.loads(line.strip())
        except ValueError:
            continue

        if not isinstance(state, dict):
            continue
        lag_ids = state.get('lag_ids')
        if isinstance(lag_ids, list):
            state['count'] = len(lag_ids)
            return state

    raise RuntimeError("Invalid SDK LAG query output: {}".format(output))


def verify_sdk_lag_count(dut_engine, expected_count):
    sdk_state = get_sdk_lag_state(dut_engine)
    if sdk_state['count'] != expected_count:
        raise AssertionError(
            "SDK LAG IDs have not been released: expected={}, actual={}, IDs={}".format(
                expected_count,
                sdk_state['count'],
                sdk_state['lag_ids'],
            )
        )
    return sdk_state


def wait_for_sdk_lag_count(dut_engine, expected_count):
    logger.info(
        "Waiting for SDK LAG count on syncd to reach %s (timeout=%ss, interval=%ss)",
        expected_count,
        SDK_LAG_QUERY_TIMEOUT_SEC,
        SDK_LAG_QUERY_INTERVAL_SEC,
    )
    prepare_sdk_lag_query(dut_engine)
    tries = SDK_LAG_QUERY_TIMEOUT_SEC // SDK_LAG_QUERY_INTERVAL_SEC + 1
    sdk_state = retry_call(
        verify_sdk_lag_count,
        fargs=[dut_engine, expected_count],
        exceptions=(AssertionError, RuntimeError),
        tries=tries,
        delay=SDK_LAG_QUERY_INTERVAL_SEC,
        logger=logger,
    )
    logger.info(
        "SDK LAG count on syncd reached %s, IDs=%s",
        sdk_state['count'],
        sdk_state['lag_ids'],
    )
    return sdk_state


def get_lag_lb_members(interfaces, member_interfaces):
    """
    :param interfaces: host-dut/dut-host ports dict
    :param member_interfaces: a list of lag member ports
    :return: a list of lag members which are connected as loopback
    """
    dut_host_ifaces = [interfaces.dut_ha_1, interfaces.dut_ha_2, interfaces.dut_hb_1, interfaces.dut_hb_2]
    lb_member_interfaces = list(set(member_interfaces).difference(dut_host_ifaces))
    return lb_member_interfaces


def get_lags_scale_configuration(number_of_lags):
    """
    Create configuration info for large number of lags with ip addresses and expected result
    :param number_of_lags: number of lags
    :return: lag_config_dict - lag configuration dictionary
             lag_expected_info - lag expected status information
             ip_config_dict - ip configuration dictionary
             ip_expected_info - ip expected status information
    """
    base_lag_name = 'PortChannel'
    base_lag_index = '1'
    lag_config_list = []
    lag_expected_info = []
    base_ipv4 = ipaddress.IPv4Address('100.0.0.1')
    base_ipv6 = ipaddress.IPv6Address('2000:2001::1')
    ip_config_list = []
    ip_expected_info = {'ipv4': [], 'ipv6': []}
    logger.info('Generate PortChannels configuration lists')
    for index in range(number_of_lags):
        lag_name = '{}{}'.format(base_lag_name, str(int(base_lag_index) + index))
        lag_config_list.append({'type': 'lacp', 'name': lag_name, 'members': []})
        lag_expected_info.append((r'{PORTCHANNEL}.*{PORTCHANNEL_STATUS}.*(N/A|.*)'
                                  .format(PORTCHANNEL=lag_name,
                                          PORTCHANNEL_STATUS='Dw'), True))
        lag_ipv4_address = base_ipv4 + index * 256    # increase the 10.0.X.1
        lag_ipv6_address = base_ipv6 + index * 65536  # increase the 2000:2001::X:1
        ip_config_list.append({'iface': lag_name, 'ips': [(lag_ipv4_address, '24'), (lag_ipv6_address, '112')]})
        ip_expected_info['ipv4'].append((r'{PORTCHANNEL}\s+{IP}/24'
                                         .format(PORTCHANNEL=lag_name, IP=lag_ipv4_address), True))
        ip_expected_info['ipv6'].append((r'{PORTCHANNEL}\s+{IP}/112'
                                         .format(PORTCHANNEL=lag_name, IP=lag_ipv6_address), True))

    lag_config_dict = {
        'dut': lag_config_list
    }
    ip_config_dict = {
        'dut': ip_config_list
    }
    logger.debug('lag_config_dict: {} \nlag_expected_info: {}'.format(lag_config_dict, lag_expected_info))
    return lag_config_dict, lag_expected_info, ip_config_dict, ip_expected_info


def check_dependency(topology_obj, dependency, cleanup_list):
    """
    Verify port channel dependencies
    :param topology_obj: topology object
    :param dependency: type of dependency
    :param cleanup_list: list with functions to cleanup
    :return: None, raise error in case of unexpected result
    """
    with allure.step('Validate the {} dependency'.format(dependency)):
        dut_engine = topology_obj.players['dut']['engine']
        dut_cli = topology_obj.players['dut']['cli']
        dut_hb_2 = topology_obj.ports['dut-hb-2']
        config_dependency = eval('config_{}_dependency(topology_obj, cleanup_list)'.format(dependency))
        if dependency == 'speed':
            dut_hb_1 = topology_obj.ports['dut-hb-1']
            portchannel_member_speeds = dut_cli.interface.get_interfaces_speed([dut_hb_1])
            if config_dependency['dut'][0]['speed'] == portchannel_member_speeds[dut_hb_1]:
                logger.info("skip speed dependency validation as the only possible speed is the same as the portchannel member speed")
        else:
            err_msg = eval('get_{}_dependency_err_msg(dut_hb_2)'.format(dependency))
            verify_add_member_to_lag_failed_with_err(dut_engine, dut_cli, dut_hb_2, err_msg)
        cleanup_last_config_in_stack(cleanup_list)


def config_ip_dependency(topology_obj, cleanup_list):
    """
    Add ip configurations to verify the ip dependency
    :param topology_obj: topology object
    :param cleanup_list: list with functions to cleanup
    """
    dut_hb_2 = topology_obj.ports['dut-hb-2']
    ip_config_dict = {
        'dut': [{'iface': dut_hb_2, 'ips': [('50.0.0.10', '24')]}]
    }
    add_ip_conf(topology_obj, ip_config_dict, cleanup_list)


def add_ip_conf(topology_obj, ip_config_dict, cleanup_list):
    """
    Add ip configurations
    :param topology_obj: topology object
    :param ip_config_dict: vlan configuration to add
    :param cleanup_list: list with functions to cleanup
    """
    IpConfigTemplate.configuration(topology_obj, ip_config_dict)
    cleanup_list.append((IpConfigTemplate.cleanup, (topology_obj, ip_config_dict,)))


def get_ip_dependency_err_msg(interface):
    """
    Get expected error message of adding the port to port channel
    :param interface: interface name
    :return: expected error message
    """
    err_msg = '{} has ip address configured'.format(interface)
    return err_msg


def config_speed_dependency(topology_obj, cleanup_list):
    """
    Add speed configurations to verify the speed dependency
    :param topology_obj: topology object
    :param cleanup_list: list with functions to cleanup
    """
    dut_hb_2 = topology_obj.ports['dut-hb-2']
    cli_obj = topology_obj.players['dut']['cli']
    dut_engine = topology_obj.players['dut']['engine']
    dut_original_interfaces_speeds = cli_obj.interface.get_interfaces_speed([dut_hb_2])
    # Read supported port speeds from platform.json file
    platform_json_info = json_file_helper.get_platform_json(dut_engine, cli_obj, fail_if_does_not_exist=False)
    iface_supported_speeds = cli_obj.general.parse_platform_json(topology_obj, platform_json_info)[dut_hb_2]
    # Get unique values and find minimum configured speed
    unique_speeds = set(item for s in iface_supported_speeds.values() if s for item in s)
    min_supported_speed = min(unique_speeds, key=lambda x: int(x[:-1]))
    interfaces_config_dict = {
        'dut': [{'iface': dut_hb_2, 'speed': min_supported_speed,
                 'original_speed': dut_original_interfaces_speeds[dut_hb_2]}]
    }
    add_interface_conf(topology_obj, interfaces_config_dict, cleanup_list)
    return interfaces_config_dict


def add_interface_conf(topology_obj, interfaces_config_dict, cleanup_list):
    """
    Add interface configurations
    :param topology_obj: topology object
    :param interfaces_config_dict: vlan configuration to add
    :param cleanup_list: list with functions to cleanup
    """
    InterfaceConfigTemplate.configuration(topology_obj, interfaces_config_dict)
    cleanup_list.append((InterfaceConfigTemplate.cleanup, (topology_obj, interfaces_config_dict,)))


def get_speed_dependency_err_msg(interface):
    """
    Get expected error message of adding the port to port channel
    :param interface: interface name
    :return: expected error message
    """
    err_msg = 'Port speed of {} is different than the other members of the portchannel {}'. \
        format(interface, PORTCHANNEL_NAME)
    return err_msg


def config_other_lag_dependency(topology_obj, cleanup_list):
    """
    Add lag configurations to verify the lag dependency
    :param topology_obj: topology object
    :param cleanup_list: list with functions to cleanup
    """
    dut_hb_2 = topology_obj.ports['dut-hb-2']
    lag_config_dict_second_lag = {
        'dut': [{'type': 'lacp', 'name': 'PortChannel2222', 'members': [dut_hb_2]}]
    }
    add_lag_conf(topology_obj, lag_config_dict_second_lag, cleanup_list)


def add_lag_conf(topology_obj, lag_config_dict, cleanup_list):
    """
    Add lag configurations
    :param topology_obj: topology object
    :param lag_config_dict: vlan configuration to add
    :param cleanup_list: list with functions to cleanup
    """
    LagLacpConfigTemplate.configuration(topology_obj, lag_config_dict)
    cleanup_list.append((LagLacpConfigTemplate.cleanup, (topology_obj, lag_config_dict,)))


def remove_lag_conf(topology_obj, lag_config_dict, cleanup_list):
    """
    remove lag configurations
    :param topology_obj: topology object
    :param lag_config_dict: vlan configuration to remove
    :param cleanup_list: list with functions to cleanup
    """
    LagLacpConfigTemplate.cleanup(topology_obj, lag_config_dict)
    cleanup_list.append((LagLacpConfigTemplate.configuration, (topology_obj, lag_config_dict,)))


def get_other_lag_dependency_err_msg(interface):
    """
    Get expected error message of adding the port to port channel
    :param interface: interface name
    :return: expected error message
    """
    err_msg = '{} Interface is already member of {}'.format(interface, 'PortChannel2222')
    return err_msg


def config_vlan_dependency(topology_obj, cleanup_list):
    """
    Add vlan configurations to verify the vlan dependency
    :param topology_obj: topology object
    :param cleanup_list: list with functions to cleanup
    """
    dut_hb_2 = topology_obj.ports['dut-hb-2']
    engine = topology_obj.players['dut']['engine']
    dut_cli = topology_obj.players['dut']['cli']
    vlan_config_dict = {'vlan_id': 50,
                        'vlan_member': dut_hb_2
                        }
    add_vlan_conf(engine, dut_cli, vlan_config_dict, cleanup_list)


def get_vlan_dependency_err_msg(interface):
    """
    Get expected error message of adding the port to port channel
    :param interface: interface name
    :return: expected error message
    """
    err_msg = '{} Interface configured as VLAN_MEMBER under vlan : Vlan50'.format(interface)
    return err_msg


def add_vlan_conf(engine, cli_obj, vlan_config_dict, cleanup_list):
    """
    Add vlan configurations
    :param engine: engine of dut
    :param cli_obj: dut cli object
    :param vlan_config_dict: vlan configuration to add
    :param cleanup_list: list with functions to cleanup
    """
    cli_obj.vlan.add_port_to_vlan(vlan_config_dict['vlan_member'], vlan_config_dict['vlan_id'])
    cleanup_list.append((cli_obj.vlan.del_port_from_vlan,
                         (vlan_config_dict['vlan_member'], vlan_config_dict['vlan_id'])))


def del_port_from_vlan(dut_engine, cli_obj, port, vlan, cleanup_list):
    """
    Delete port from vlan
    :param dut_engine: dut engine
    :param cli_obj: dut cli object
    :param port: port name
    :param cleanup_list: list with functions to cleanup
    """
    cli_obj.vlan.del_port_from_vlan(port, vlan)
    cleanup_list.append((cli_obj.vlan.add_port_to_vlan, (port, vlan, 'trunk',)))


def verify_add_member_to_lag_failed_with_err(dut_engine, cli_object, member_port, err_msg):
    """
    Verify negative adding member to port channel
    :param dut_engine: dut engine
    :param cli_object: dut cli object
    :param member_port: name of member port to be added
    :param err_msg: expected error message
    :return: None, raise error in case of unexpected result
    """
    with allure.step('Verify lag dependency, adding member failed as expected with error message: {}'.format(err_msg)):
        output = cli_object.lag.add_port_to_port_channel(member_port, PORTCHANNEL_NAME)
        if not re.search(err_msg, output, re.IGNORECASE):
            output = cli_object.lag.delete_port_from_port_channel(member_port, PORTCHANNEL_NAME)
            raise AssertionError("Expected to failed on adding member to LAG "
                                 "with error msg '{}' but output {}".
                                 format(err_msg, output))


def get_pkt_to_send(traffic_type, cli_obj, dst_iface):
    """
    Create scapy packet for validation
    :param traffic_type: the type of the traffic
    :param cli_obj: device cli_obj
    :param dst_iface: destination interface name
    :return: scapy packet
    """
    dst_mac = cli_obj.mac.get_mac_address_for_interface(dst_iface)
    return BASE_PKT.format(dst_mac, traffic_type)


def verify_port_channel_status_with_retry(cli_object, dut_engine, lag_name, lag_status,
                                          expected_ports_status_list, tries=8, delay=15):
    """
    Verify the PortChannels from "show interfaces portchannel" output, accordingly to handed statuses with retry
    :param cli_object: dut cli object
    :param dut_engine: dut engine
    :param lag_name: port channel name
    :param lag_status: port channel status
    :param expected_ports_status_list: list of tuples - (member port name, status)
    :param tries: number of attempts
    :param delay: delay time between attempts
    """
    retry_call(cli_object.lag.verify_port_channel_status,
               fargs=[lag_name, lag_status, expected_ports_status_list],
               tries=tries,
               delay=delay,
               logger=logger)


def verify_port_channels_status(cli_object, dut_engine, expected_lag_info):
    """
    Verify the PortChannels from "show interfaces portchannel" output
    :param cli_object: dut cli object
    :param dut_engine: dut engine
    :param expected_lag_info: expected port channels information
    :return: None, raise error in case of unexpected result
    """
    port_channel_info = cli_object.lag.show_interfaces_port_channel()
    verify_show_cmd(port_channel_info, expected_lag_info)


def verify_port_channels_ip(cli_object, dut_engine, expected_ip_info):
    """
    Verify the PortChannels ips
    :param cli_object: dut cli object
    :param dut_engine: dut engine
    :param expected_ip_info: expected ip information
    :return: None, raise error in case of unexpected result
    """
    verify_port_channels_ipv4_addresses(cli_object, dut_engine, expected_ip_info['ipv4'])
    verify_port_channels_ipv6_addresses(cli_object, dut_engine, expected_ip_info['ipv6'])


def verify_port_channels_ipv4_addresses(cli_object, dut_engine, expected_ip_info):
    """
    Verify the PortChannels ips from "show ip interfaces" output
    :param cli_object: dut cli object
    :param dut_engine: dut engine
    :param expected_ip_info: expected ip information
    :return: None, raise error in case of unexpected result
    """
    ip_info = cli_object.ip.show_ip_interfaces()
    verify_show_cmd(ip_info, expected_ip_info)


def verify_port_channels_ipv6_addresses(cli_object, dut_engine, expected_ip_info):
    """
    Verify the PortChannels ips from "show ipv6 interfaces" output
    :param cli_object: dut cli object
    :param dut_engine: dut engine
    :param expected_ip_info: expected ip information
    :return: None, raise error in case of unexpected result
    """
    ip_info = cli_object.ip.show_ipv6_interfaces()
    verify_show_cmd(ip_info, expected_ip_info)


def traffic_validation(topology_obj, traffic_type):
    """
    Validate the handed traffic type on the setup (L2 traffic)
    :param topology_obj: topology object
    :param traffic_type: the type of the traffic (TCP/UDP)
    :return: None, raise error in case of unexpected result
    """
    tcpdump_filter = 'dst 50.0.0.3 and {}'.format(traffic_type.lower())
    hadut1 = topology_obj.ports['ha-dut-1']
    hb_cli_obj = topology_obj.players['hb']['cli']
    pkt = get_pkt_to_send(traffic_type, hb_cli_obj, 'bond0')
    validation = {'sender': 'ha', 'send_args': {'interface': hadut1 + '.50',
                                                'packets': pkt, 'count': 100},
                  'receivers':
                      [
                          {'receiver': 'hb',
                           'receive_args': {'interface': 'bond0.50',
                                            'filter': tcpdump_filter, 'count': 100}}
    ]
    }
    ScapyChecker(topology_obj.players, validation).run_validation()


def l3_traffic_validation(topology_obj, l3_route_config):
    """
    Validate L3 routed traffic through LAG.
    Traffic path: ha (51.0.0.2) -> DUT (51.0.0.1) routes to -> hb (50.0.0.3) via LAG
    """
    dut_cli = topology_obj.players['dut']['cli']
    ha_l3_iface = l3_route_config['ha_l3_iface']
    dut_l3_iface = l3_route_config['dut_l3_iface']
    ha_l3_ip = l3_route_config['ha_l3_ip']

    dut_mac = dut_cli.mac.get_mac_address_for_interface(dut_l3_iface)
    l3_pkt = 'Ether(dst="{}")/IP(src="{}",dst="50.0.0.3")/TCP()/Raw()'.format(dut_mac, ha_l3_ip)
    validation = {
        'sender': 'ha',
        'send_args': {'interface': ha_l3_iface, 'packets': l3_pkt, 'count': 100},
        'receivers': [
            {'receiver': 'hb',
             'receive_args': {'interface': 'bond0.50',
                              'filter': 'dst 50.0.0.3 and tcp', 'count': 100}}
        ]
    }
    ScapyChecker(topology_obj.players, validation).run_validation()


def config_bond_type_lag(topology_obj, interfaces, cleanup_list):
    """
    Config bond to type lag, add to cleanup restore functions
    :param topology_obj: topology object
    :param interfaces: interfaces obj
    :param cleanup_list: list with functions to cleanup
    :return: None, raise error in case of unexpected result
    """
    lag_lacp_config_dict = {
        'hb': [{'type': 'lacp', 'name': 'bond0', 'members': [interfaces.hb_dut_1, interfaces.hb_dut_2]}]
    }

    vlan_config_dict = {
        'hb': [{'vlan_id': 50, 'vlan_members': [{'bond0': None}]}]
    }

    ip_config_dict = {
        'hb': [{'iface': 'bond0.50', 'ips': [('50.0.0.3', '24')]}]
    }

    lag_lacp_config_dict_type_lag = {
        'hb': [{'type': 'lag', 'name': 'bond0', 'members': [interfaces.hb_dut_1, interfaces.hb_dut_2]}]
    }

    # add to cleanup stack original IP and Vlan config
    cleanup_list.append((IpConfigTemplate.configuration, (topology_obj, ip_config_dict,)))
    cleanup_list.append((VlanConfigTemplate.configuration, (topology_obj, vlan_config_dict,)))

    # remove origin lag config and add to stack the adding
    remove_lag_conf(topology_obj, lag_lacp_config_dict, cleanup_list)

    # add temp config and add to stack the cleanup
    add_lag_conf(topology_obj, lag_lacp_config_dict_type_lag, cleanup_list)

    # add original IP and Vlan configs
    VlanConfigTemplate.configuration(topology_obj, vlan_config_dict)
    IpConfigTemplate.configuration(topology_obj, ip_config_dict)


def get_interfaces_by_type_dict(interfaces_info):
    """
    Group CPO and RJ45 interfaces separately from other pluggable interfaces.
    :param interfaces_info: parsed output from "show interfaces status" command
    :return: dictionary
    """
    rg45_type = 'RJ45'
    qsfp_type = 'QSFP'
    cpo_type = 'CPO'
    interfaces_types_dict = {rg45_type: [], qsfp_type: [], cpo_type: []}

    for interface in interfaces_info:
        interface_type = interfaces_info[interface]['Type']
        if interface_type == rg45_type:
            interfaces_types_dict[rg45_type].append(interface)
        elif interface_type == cpo_type:
            interfaces_types_dict[cpo_type].append(interface)
        else:
            interfaces_types_dict[qsfp_type].append(interface)

    return interfaces_types_dict


def get_ifaces_type_which_has_bigger_ifaces_list(interfaces_types_dict):
    """
    Get type of interface which has bigger number of interfaces
    :param interfaces_types_dict: dict of interface types and their interfaces
    :return: str, interface type which have bigger list of interfaces
    """
    num_of_ifaces = 0
    ifaces_type = None

    for iface_type in interfaces_types_dict:
        if len(interfaces_types_dict[iface_type]) > num_of_ifaces:
            num_of_ifaces = len(interfaces_types_dict[iface_type])
            ifaces_type = iface_type

    return ifaces_type
