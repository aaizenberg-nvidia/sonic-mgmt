import pytest
import allure
import logging

from retry.api import retry_call
from ngts.config_templates.vlan_config_template import VlanConfigTemplate
from ngts.config_templates.ip_config_template import IpConfigTemplate
from ngts.config_templates.dhcp_relay_config_template import DhcpRelayConfigTemplate
from devts.infra.tools.validations.traffic_validations.scapy.scapy_runner import ScapyChecker
from devts.infra.tools.validations.traffic_validations.ping.ping_runner import PingChecker
from ngts.helpers.general_helper import is_smartswitch_platform

logger = logging.getLogger()
num_of_dhcp_servers = 10
dhcp_client_vlan = 100
dhcp_servers_first_vlan = 101


@pytest.fixture(scope='function')
def skip_dhcp_relay_v4_test(topology_obj):
    if is_smartswitch_platform(topology_obj):
        pytest.skip(
            "Smartswitch not support the case. \
             Because by default, dhcp relay v4 is enabled on smartswitch and is mainly for DPU.\
             So when configuring dhcp relay in smartswitch, \
             it will return error: Cannot change dhcp_relay configuration when dhcp_server feature is enabled.")


@pytest.fixture(scope='module', autouse=True)
def configure_dhcp_scale(topology_obj, cli_objects, interfaces, engines):
    skip_v4 = is_smartswitch_platform(topology_obj)

    dutha1 = topology_obj.ports['dut-ha-1']
    duthb1 = topology_obj.ports['dut-hb-1']
    hbdut1 = topology_obj.ports['hb-dut-1']

    with allure.step('Check that links in UP state'.format()):
        ports_list = [interfaces.dut_ha_1, interfaces.dut_ha_2, interfaces.dut_hb_1, interfaces.dut_hb_2]
        retry_call(cli_objects.dut.interface.check_ports_status, fargs=[ports_list], tries=20, delay=10,
                   logger=logger)

    vlan_config_dict = {
        'dut': [{'vlan_id': dhcp_client_vlan, 'vlan_members': [{dutha1: 'access'}]}],
        'hb': []
    }

    ip_config_dict = {
        'dut': [{'iface': 'Vlan100', 'ips': [('10.0.0.1', '24'), ('2000::1', '64')]}],
        'hb': []
    }

    dhcp_relay_config_dict = {
        'dut': [{'vlan_id': dhcp_client_vlan, 'dhcp_servers': []}]
    }
    # Generate 10 DHCP relays
    for item in range(dhcp_servers_first_vlan, dhcp_servers_first_vlan + num_of_dhcp_servers):
        vlan_config_dict['dut'].append({'vlan_id': item, 'vlan_members': [{duthb1: 'trunk'}]})
        vlan_config_dict['hb'].append({'vlan_id': item, 'vlan_members': [{hbdut1: None}]})
        ip_config_dict['dut'].append({'iface': 'Vlan{}'.format(item), 'ips': [('10.{}.0.1'.format(item), '24'),
                                                                              ('2{}::1'.format(item), '64')]})
        ip_config_dict['hb'].append({'iface': '{}.{}'.format(hbdut1, item), 'ips': [('10.{}.0.2'.format(item), '24'),
                                                                                    ('2{}::2'.format(item), '64')]})
        if not skip_v4:
            dhcp_relay_config_dict['dut'][0]['dhcp_servers'].append('10.{}.0.2'.format(item))
        dhcp_relay_config_dict['dut'][0]['dhcp_servers'].append('2{}::2'.format(item))

    logger.info('Starting DHCP relay scale configuration')
    VlanConfigTemplate.configuration(topology_obj, vlan_config_dict)
    IpConfigTemplate.configuration(topology_obj, ip_config_dict)
    DhcpRelayConfigTemplate.configuration(topology_obj, dhcp_relay_config_dict)
    logger.info('DHCP relay scale configuration completed')

    yield

    logger.info('Starting DHCP relay scale configuration cleanup')
    DhcpRelayConfigTemplate.cleanup(topology_obj, dhcp_relay_config_dict)
    IpConfigTemplate.cleanup(topology_obj, ip_config_dict)
    VlanConfigTemplate.cleanup(topology_obj, vlan_config_dict)
    cli_objects.dut.general.save_configuration()
    logger.info('DHCP relay scale configuration completed')


@pytest.fixture(autouse=True, scope='module')
def update_arp_for_peers_DHCP_servers(topology_obj, interfaces):
    for item in range(dhcp_servers_first_vlan, dhcp_servers_first_vlan + num_of_dhcp_servers):
        validation_create_arp_ipv4 = {'sender': 'hb', 'args': {'interface': '{}.{}'.format(interfaces.hb_dut_1, item),
                                                               'count': 3, 'dst': '10.{}.0.1'.format(item)}}
        ping_checker = PingChecker(topology_obj.players, validation_create_arp_ipv4)
        retry_call(ping_checker.run_validation, fargs=[], tries=3, delay=10, logger=logger)

        validation_create_neigh_ipv6 = {'sender': 'hb', 'args': {'interface': '{}.{}'.format(interfaces.hb_dut_1, item),
                                                                 'count': 3, 'dst': '2{}::1'.format(item)}}
        ping6_checker = PingChecker(topology_obj.players, validation_create_neigh_ipv6)
        retry_call(ping6_checker.run_validation, fargs=[], tries=3, delay=10, logger=logger)


@allure.title('Test DHCP Relay scale')
def test_dhcp_relay_few_dhcp_servers(topology_obj, interfaces, skip_dhcp_relay_v4_test):
    """
    This test will check DHCP Relay functionality in case when few DHCP servers configured
    We send DHCP request from client and expected to see it on all DHCP servers which configured in relay settings
    :param topology_obj: topology object fixture
    :param interfaces: interfaces fixture
    :return: raise assertion error in case when test failed
    """
    src_mac = '64:42:a1:17:e6:35'
    chaddr = bytes.fromhex(src_mac.replace(':', ''))
    pkt = 'Ether(src="{}",dst="ff:ff:ff:ff:ff:ff")/IP(src="0.0.0.0",dst="255.255.255.255")/' \
          'UDP(sport=68,dport=67)/BOOTP(chaddr="{}",xid=RandInt())/DHCP()'.format(src_mac, chaddr)

    try:
        with allure.step('Validate that DHCP packet(request) forwarded to all DHCP servers'):
            validation = {'sender': 'ha', 'send_args': {'interface': interfaces.ha_dut_1, 'packets': pkt, 'count': 1},
                          'receivers':
                              [
                {'receiver': 'hb',
                 'receive_args': {'interface': interfaces.hb_dut_1, 'filter': 'port 67',
                                  'count': num_of_dhcp_servers}}
            ]
            }
            scapy_checker = ScapyChecker(topology_obj.players, validation)
            retry_call(scapy_checker.run_validation, fargs=[], tries=3, delay=10, logger=logger)

    except BaseException as err:
        raise AssertionError(err)


@allure.title('Test DHCP6 Relay scale')
def test_dhcp6_relay_few_dhcp_servers(topology_obj, interfaces):
    """
    This test will check DHCP6 Relay functionality in case when few DHCP6 servers configured
    We send DHCP6 request from client and expected to see it on all DHCP6 servers which configured in relay settings
    :param topology_obj: topology object fixture
    :param interfaces: interfaces fixture
    :return: raise assertion error in case when test failed
    """

    # It will send packet with IPv6 src ip - link local IPv6 address(by default)
    pkt = 'Ether(dst="33:33:00:01:00:02")/' \
          'IPv6(dst="ff02::1:2")/' \
          'UDP(sport=546, dport=547)/' \
          'DHCP6_Solicit(trid=12345)/' \
          'DHCP6OptElapsedTime()/' \
          'DHCP6OptOptReq()'

    try:
        with allure.step('Validate that DHCP packet(request) forwarded to all DHCP servers'):
            validation = {'sender': 'ha', 'send_args': {'interface': interfaces.ha_dut_1, 'packets': pkt, 'count': 1},
                          'receivers':
                              [
                {'receiver': 'hb',
                 'receive_args': {'interface': interfaces.hb_dut_1, 'filter': 'port 547',
                                  'count': num_of_dhcp_servers}}
            ]
            }
            scapy_checker = ScapyChecker(topology_obj.players, validation)
            retry_call(scapy_checker.run_validation, fargs=[], tries=3, delay=10, logger=logger)

    except BaseException as err:
        raise AssertionError(err)
