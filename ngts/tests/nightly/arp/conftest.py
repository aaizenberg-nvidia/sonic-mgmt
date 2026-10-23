import pytest

from ngts.config_templates.lag_lacp_config_template import LagLacpConfigTemplate
from ngts.config_templates.vlan_config_template import VlanConfigTemplate
from ngts.config_templates.ip_config_template import IpConfigTemplate
from ngts.helpers.arp_helper import INTERFACE_TYPE_LIST, \
    clear_dynamic_arp_table_and_check_the_specified_arp_entry_deleted
from ngts.config_templates.interfaces_config_template import InterfaceConfigTemplate
from ngts.config_templates.route_config_template import RouteConfigTemplate
from ngts.constants.constants import PLATFORM_200G_SUPPORT_LIST, PLATFORM_800G_SUPPORT_LIST


@pytest.fixture(scope='module', autouse=True)
def pre_configure_for_arp(engines, topology_obj, interfaces, platform_params):
    """
    Pytest fixture which are doing configuration for test case based for arp test
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    :param interfaces: topology object fixture
    :param platform_params: platform_params object fixture
    """
    cli_obj = topology_obj.players['dut']['cli']
    dut_original_interfaces_speeds = cli_obj.interface.get_interfaces_speed([interfaces.dut_hb_2])
    if platform_params.filtered_platform in PLATFORM_800G_SUPPORT_LIST:
        new_speed_hb = '800G'
    elif platform_params.filtered_platform in PLATFORM_200G_SUPPORT_LIST:
        new_speed_hb = '200G'
    else:
        new_speed_hb = '10G'
    interfaces_config_dict = {
        'dut': [{'iface': interfaces.dut_hb_2, 'speed': new_speed_hb,
                 'original_speed': dut_original_interfaces_speeds.get(interfaces.dut_hb_2, new_speed_hb)}
                ]
    }
    # LAG/LACP config which will be used in test
    lag_lacp_config_dict = {
        'dut': [{'type': 'lacp', 'name': 'PortChannel0002', 'members': [interfaces.dut_hb_2]}],
        'hb': [{'type': 'lacp', 'name': 'bond0', 'members': [interfaces.hb_dut_2]}]
    }

    # VLAN config which will be used in test
    vlan_config_dict = {
        'dut': [{'vlan_id': 40, 'vlan_members': [{interfaces.dut_ha_2: 'access'},
                                                 {interfaces.dut_hb_1: 'access'}]},
                ],
        'ha': [{'vlan_id': 40, 'vlan_members': [{interfaces.ha_dut_2: None}]},
               ],
        'hb': [{'vlan_id': 40, 'vlan_members': [{interfaces.hb_dut_1: None}]},
               ]
    }

    # IP config which will be used in test
    ip_config_dict = {
        'dut': [{'iface': 'Vlan40', 'ips': [('40.0.0.1', '24')]},
                {'iface': interfaces.dut_ha_1, 'ips': [('30.0.0.1', '24')]},
                {'iface': 'PortChannel0002', 'ips': [('50.0.0.1', '24')]}
                ],
        'ha': [{'iface': interfaces.ha_dut_1, 'ips': [('30.0.0.2', '24')]}],
        'hb': [{'iface': interfaces.hb_dut_1, 'ips': [('40.0.0.10', '24')]}]
    }
    static_route_config_dict = {
        'ha': [{'dst': '40.0.0.0', 'dst_mask': 24, 'via': ['30.0.0.1']}],
        'hb': [{'dst': '30.0.0.0', 'dst_mask': 24, 'via': ['40.0.0.1']}]
    }
    InterfaceConfigTemplate.configuration(topology_obj, interfaces_config_dict)
    LagLacpConfigTemplate.configuration(topology_obj, lag_lacp_config_dict)
    VlanConfigTemplate.configuration(topology_obj, vlan_config_dict)
    IpConfigTemplate.configuration(topology_obj, ip_config_dict)
    RouteConfigTemplate.configuration(topology_obj, static_route_config_dict)

    yield
    RouteConfigTemplate.cleanup(topology_obj, static_route_config_dict)
    IpConfigTemplate.cleanup(topology_obj, ip_config_dict)
    VlanConfigTemplate.cleanup(topology_obj, vlan_config_dict)
    LagLacpConfigTemplate.cleanup(topology_obj, lag_lacp_config_dict)
    InterfaceConfigTemplate.cleanup(topology_obj, interfaces_config_dict)


@pytest.fixture(scope='module', autouse=True)
def pre_test_interface_data(topology_obj, interfaces):
    """
    Pytest fixture which are doing configuration for test case based for arp test
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    :param interfaces: topology object fixture
    """
    test_interface_data = {}
    for interface_type in INTERFACE_TYPE_LIST:
        test_interface_data[interface_type] = gen_test_interface_data(topology_obj, interfaces, interface_type)
    yield test_interface_data


@pytest.fixture(scope='function', autouse=True)
def pre_clear_arp(cli_objects):
    """
    Pytest fixture which is to clear the static arp and  dynamic arp before test
    :param cli_objects: cli_objects fixture
    """
    ip_list = ["40.0.0.2", "30.0.0.2"]
    cli_objects.dut.ip.del_static_neigh()
    for ip in ip_list:
        clear_dynamic_arp_table_and_check_the_specified_arp_entry_deleted(cli_objects.dut, ip)


@pytest.fixture(scope='module')
def interface_data_ping(cli_objects, interfaces):
    """
    Pytest fixture which are doing configuration for test case based for arp test
    :param cli_objects: cli_objects fixture
    :param interfaces: topology object fixture
    """
    test_interface_data = {}
    test_interface_data["src_interface"] = interfaces.ha_dut_1
    test_interface_data["src_ip"] = "30.0.0.2"
    test_interface_data["src_mac"] = cli_objects.ha.mac.get_mac_address_for_interface(interfaces.ha_dut_1)
    test_interface_data["src_dut_iface"] = interfaces.dut_ha_1
    test_interface_data["src_dut_vlan_id"] = "-"
    test_interface_data["src_host_alias"] = "ha"
    test_interface_data["dst_ip"] = "40.0.0.10"
    test_interface_data["dst_mac"] = cli_objects.hb.mac.get_mac_address_for_interface(interfaces.hb_dut_1)
    test_interface_data["dst_dut_iface"] = interfaces.dut_hb_1
    test_interface_data["dst_dut_vlan_id"] = "40"
    yield test_interface_data


def gen_test_interface_data(topology_obj, interfaces, interface_type):
    """
    Pytest fixture which is to prepare the interface test data
    :param topology_obj: topology_obj object fixture
    :param interfaces: interfaces object fixture
    :param interface_type:  interface type such as "ethernet", "vlan", "portchannel"
    """
    test_interface_data = {}
    dut_cli_obj = topology_obj.players['dut']['cli']
    ha_cli_obj = topology_obj.players['ha']['cli']
    hb_cli_obj = topology_obj.players['hb']['cli']
    if interface_type == "ethernet":
        test_interface_data["host_ip"] = "30.0.0.2"
        test_interface_data["dut_ip"] = "30.0.0.1"
        test_interface_data["host_interface"] = interfaces.ha_dut_1
        test_interface_data["dut_interface"] = interfaces.dut_ha_1
        test_interface_data["host_mac"] = ha_cli_obj.mac.get_mac_address_for_interface(interfaces.ha_dut_1)
        test_interface_data["dut_mac"] = dut_cli_obj.mac.get_mac_address_for_interface(interfaces.dut_ha_1)
        test_interface_data["dut_vlan_id"] = "-"
        test_interface_data["host_alias"] = "ha"
    elif interface_type == "vlan":
        test_interface_data["host_ip"] = "40.0.0.2"
        test_interface_data["dut_ip"] = "40.0.0.1"
        test_interface_data["host_interface"] = interfaces.ha_dut_2
        test_interface_data["dut_interface"] = interfaces.dut_ha_2
        test_interface_data["host_mac"] = ha_cli_obj.mac.get_mac_address_for_interface(interfaces.ha_dut_2)
        test_interface_data["dut_mac"] = dut_cli_obj.mac.get_mac_address_for_interface(interfaces.dut_ha_2)
        test_interface_data["dut_vlan_id"] = "40"
        test_interface_data["host_alias"] = "ha"
    elif interface_type == "portchannel":
        test_interface_data["host_ip"] = "50.0.0.2"
        test_interface_data["dut_ip"] = "50.0.0.1"
        test_interface_data["host_interface"] = "bond0"
        test_interface_data["dut_interface"] = "PortChannel0002"
        test_interface_data["host_mac"] = hb_cli_obj.mac.get_mac_address_for_interface("bond0")
        test_interface_data["dut_mac"] = dut_cli_obj.mac.get_mac_address_for_interface(interfaces.dut_hb_2)
        test_interface_data["dut_vlan_id"] = "-"
        test_interface_data["host_alias"] = "hb"
    return test_interface_data
