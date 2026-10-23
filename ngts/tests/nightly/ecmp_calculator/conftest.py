import pytest
import logging
import os
import re

from ngts.config_templates.lag_lacp_config_template import LagLacpConfigTemplate
from ngts.config_templates.vlan_config_template import VlanConfigTemplate
from ngts.config_templates.ip_config_template import IpConfigTemplate
from ngts.config_templates.interfaces_config_template import InterfaceConfigTemplate
from ngts.config_templates.route_config_template import RouteConfigTemplate
from ngts.config_templates.sub_interface_config_template import SubIntConfigTemplate
from ngts.config_templates.vrf_config_template import VrfConfigTemplate
from devts.infra.tools.validations.traffic_validations.ping.ping_runner import PingChecker
from ngts.tests.nightly.ecmp_calculator.constants import V4_CONFIG, V6_CONFIG, DEST_ROUTE_V4, DEST_ROUTE_V6
from retry.api import retry_call
from ngts.tests.nightly.ecmp_calculator.ecmp_calculator_helper import copy_packet_json_to_syncd, ECMP_CALCULATOR_PATH
from ngts.constants.constants import PLATFORM_800G_SUPPORT_LIST

logger = logging.getLogger()


def generate_arp(players, interface, sender, dst_ip):
    validation = {'sender': sender, 'args': {'interface': interface, 'count': 3, 'dst': dst_ip}}
    ping = PingChecker(players, validation)
    logger.info('Sending 3 ping packets to {} from interface {}'.format(dst_ip, interface))
    retry_call(ping.run_validation, fargs=[], tries=15, delay=2, logger=logger)


@pytest.fixture(scope='package', autouse=True)
def skipping_ecmp_calculator_test(engines):
    ecmp_calculator_not_exist_pattern = r".*ls: cannot access \'\/usr\/bin\/ecmp_calc.py\': No such file or directory.*"
    res = engines.dut.run_cmd("docker exec syncd bash -c 'ls /usr/bin/ecmp_calc.py'")
    if re.match(ecmp_calculator_not_exist_pattern, res):
        pytest.skip("The ECMP calculator feature is missing, skipping the test case")


@pytest.fixture(scope='class')
def pre_configure_for_interface_default_vrf(request, engines, topology_obj, interfaces, cli_objects, players):
    """
    Pytest fixture which are doing configuration for ecmp interface default vrf case
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    :param interfaces: interfaces object fixture
    :param cli_objects: cli_objects object fixture
    :param players: players object fixture
    """
    # IP config which will be used in test
    ip_config_dict = {
        'dut': [{'iface': interfaces.dut_ha_1, 'ips': [(V4_CONFIG['dut_ha_1'], '24')]},
                {'iface': interfaces.dut_ha_2, 'ips': [(V4_CONFIG['dut_ha_2'], '24')]},
                {'iface': interfaces.dut_hb_1, 'ips': [(V4_CONFIG['dut_hb_1'], '24')]},
                {'iface': interfaces.dut_hb_2, 'ips': [(V4_CONFIG['dut_hb_2'], '24')]},
                ],
        'ha': [{'iface': interfaces.ha_dut_1, 'ips': [(V4_CONFIG['ha_dut_1'], '24')]},
               {'iface': interfaces.ha_dut_2, 'ips': [(V4_CONFIG['ha_dut_2'], '24')]}],
        'hb': [{'iface': interfaces.hb_dut_1, 'ips': [(V4_CONFIG['hb_dut_1'], '24')]},
               {'iface': interfaces.hb_dut_2, 'ips': [(V4_CONFIG['hb_dut_2'], '24')]}]

    }

    static_route_config_dict = {
        'dut': [{'dst': DEST_ROUTE_V4["prefix"], 'dst_mask': DEST_ROUTE_V4["mask"],
                 'via': [V4_CONFIG['ha_dut_2'], V4_CONFIG['hb_dut_1'], V4_CONFIG['hb_dut_2']]}]
    }

    ping_info_list = [{"host": "ha", "src_intf": interfaces.ha_dut_1, "dst_intf": 'dut_ha_1'},
                      {"host": "ha", "src_intf": interfaces.ha_dut_2, "dst_intf": 'dut_ha_2'},
                      {"host": "hb", "src_intf": interfaces.hb_dut_1, "dst_intf": 'dut_hb_1'},
                      {"host": "hb", "src_intf": interfaces.hb_dut_2, "dst_intf": 'dut_hb_2'}]

    IpConfigTemplate.configuration(topology_obj, ip_config_dict, request)
    RouteConfigTemplate.configuration(topology_obj, static_route_config_dict, request)

    # Generate arp table
    gen_arp_table_via_ping(players, ping_info_list, only_ping_v4=True)


@pytest.fixture(scope='class', autouse=False)
def pre_configure_for_interface_vrf(request, engines, topology_obj, interfaces, cli_objects, players):
    """
    Pytest fixture which are doing configuration for ecmp interface default vrf case
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    :param interfaces: interfaces object fixture
    :param cli_objects: cli_objects object fixture
    """
    # vrf config
    vrf_config_dict = {
        'dut': [{'vrf': 'Vrf_ecmp', 'vrf_interfaces': [interfaces.dut_ha_1,
                                                       interfaces.dut_ha_2,
                                                       interfaces.dut_hb_1,
                                                       interfaces.dut_hb_2]}]}

    # IP config which will be used in test
    ip_config_dict = {
        'dut': [{'iface': interfaces.dut_ha_1, 'ips': [(V4_CONFIG['dut_ha_1'], '24'), (V6_CONFIG['dut_ha_1'], '64')]},
                {'iface': interfaces.dut_ha_2, 'ips': [(V4_CONFIG['dut_ha_2'], '24'), (V6_CONFIG['dut_ha_2'], '64')]},
                {'iface': interfaces.dut_hb_1, 'ips': [(V4_CONFIG['dut_hb_1'], '24'), (V6_CONFIG['dut_hb_1'], '64')]},
                {'iface': interfaces.dut_hb_2, 'ips': [(V4_CONFIG['dut_hb_2'], '24'), (V6_CONFIG['dut_hb_2'], '64')]},
                ],
        'ha': [{'iface': interfaces.ha_dut_1, 'ips': [(V4_CONFIG['ha_dut_1'], '24'), (V6_CONFIG['ha_dut_1'], '64')]},
               {'iface': interfaces.ha_dut_2, 'ips': [(V4_CONFIG['ha_dut_2'], '24'), (V6_CONFIG['ha_dut_2'], '64')]}],
        'hb': [{'iface': interfaces.hb_dut_1, 'ips': [(V4_CONFIG['hb_dut_1'], '24'), (V6_CONFIG['hb_dut_1'], '64')]},
               {'iface': interfaces.hb_dut_2, 'ips': [(V4_CONFIG['hb_dut_2'], '24'), (V6_CONFIG['hb_dut_2'], '64')]}]

    }

    ping_info_list = [{"host": "ha", "src_intf": interfaces.ha_dut_1, "dst_intf": 'dut_ha_1'},
                      {"host": "ha", "src_intf": interfaces.ha_dut_2, "dst_intf": 'dut_ha_2'},
                      {"host": "hb", "src_intf": interfaces.hb_dut_1, "dst_intf": 'dut_hb_1'},
                      {"host": "hb", "src_intf": interfaces.hb_dut_2, "dst_intf": 'dut_hb_2'}]

    # Static route config
    static_route_config_dict = get_interface_test_static_route_config(vrf="Vrf_ecmp")

    VrfConfigTemplate.configuration(topology_obj, vrf_config_dict, request)
    IpConfigTemplate.configuration(topology_obj, ip_config_dict, request)
    RouteConfigTemplate.configuration(topology_obj, static_route_config_dict, request)

    # generate arp by pinging from host
    gen_arp_table_via_ping(players, ping_info_list)


@pytest.fixture(scope='class', autouse=False)
def pre_configure_for_interface_vlan_vrf(request, engines, topology_obj, interfaces, cli_objects, players):
    """
    Pytest fixture which are doing configuration for ecmp interface vrf case
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    :param interfaces: interfaces object fixture
    :param cli_objects: cli_objects object fixture
    :param players: players object fixture
    """
    cli_obj = topology_obj.players['dut']['cli']

    # vrf config
    vrf_config_dict = {
        'dut': [{'vrf': 'Vrf_ecmp', 'vrf_interfaces': ["Vlan100", "Vlan200", "Vlan300"]}]}

    # IP config which will be used in test
    ip_config_dict = {
        'dut': [{'iface': "Vlan100", 'ips': [(V4_CONFIG['dut_ha_1'], '24'), (V6_CONFIG['dut_ha_1'], '64')]},
                {'iface': "Vlan200", 'ips': [(V4_CONFIG['dut_ha_2'], '24'), (V6_CONFIG['dut_ha_2'], '64')]},
                {'iface': "Vlan300", 'ips': [(V4_CONFIG['dut_hb_1'], '24'), (V6_CONFIG['dut_hb_1'], '64')]}
                ],
        'ha': [{'iface': interfaces.ha_dut_1, 'ips': [(V4_CONFIG['ha_dut_1'], '24'), (V6_CONFIG['ha_dut_1'], '64')]},
               {'iface': interfaces.ha_dut_2, 'ips': [(V4_CONFIG['ha_dut_2'], '24'), (V6_CONFIG['ha_dut_2'], '64')]}],
        'hb': [{'iface': interfaces.hb_dut_1, 'ips': [(V4_CONFIG['hb_dut_1'], '24'), (V6_CONFIG['hb_dut_1'], '64')]},
               {'iface': interfaces.hb_dut_2,
                'ips': [(V4_CONFIG['vlan_300_dut_hb_2'], '24'), (V6_CONFIG['vlan_300_dut_hb_2'], '64')]}]

    }

    # VLAN config which will be used in test
    vlan_config_dict = {
        'dut': [{'vlan_id': 100, 'vlan_members': [{interfaces.dut_ha_1: 'access'}]},
                {'vlan_id': 200, 'vlan_members': [{interfaces.dut_ha_2: 'access'}]},
                {'vlan_id': 300, 'vlan_members': [{interfaces.dut_hb_1: 'access'}, {interfaces.dut_hb_2: 'access'}]},
                ],
    }

    # Static route config
    static_route_config_dict = {
        'dut': [{'dst': DEST_ROUTE_V4["prefix"], 'dst_mask': DEST_ROUTE_V4["mask"],
                 'via': [V4_CONFIG['ha_dut_2'], V4_CONFIG['hb_dut_1'], V4_CONFIG['vlan_300_dut_hb_2']],
                 'vrf': "Vrf_ecmp"},
                {'dst': DEST_ROUTE_V6["prefix"], 'dst_mask': DEST_ROUTE_V6["mask"],
                 'via': [V6_CONFIG['ha_dut_2'], V6_CONFIG['hb_dut_1'], V6_CONFIG['vlan_300_dut_hb_2']],
                 'vrf': "Vrf_ecmp"}
                ]
    }

    ping_info_list = [{"host": "ha", "src_intf": interfaces.ha_dut_1, "dst_intf": 'dut_ha_1'},
                      {"host": "ha", "src_intf": interfaces.ha_dut_2, "dst_intf": 'dut_ha_2'},
                      {"host": "hb", "src_intf": interfaces.hb_dut_1, "dst_intf": 'dut_hb_1'},
                      {"host": "hb", "src_intf": interfaces.hb_dut_2, "dst_intf": 'dut_hb_2',
                       "ping_ip_v4": V4_CONFIG['dut_hb_1'], "ping_ip_v6": V6_CONFIG['dut_hb_1']}]

    VlanConfigTemplate.configuration(topology_obj, vlan_config_dict, request)
    VrfConfigTemplate.configuration(topology_obj, vrf_config_dict, request)
    IpConfigTemplate.configuration(topology_obj, ip_config_dict, request)
    RouteConfigTemplate.configuration(topology_obj, static_route_config_dict, request)

    # Enable arp_ignore on hb to prevent ARP cross-replies between its two interfaces
    # in the same Vlan300 subnet. Without this, both hb interfaces respond to each other's
    # ARP requests (due to Linux default arp_ignore=0), causing random neighbor MAC swaps
    # on the DUT that break ECMP egress port verification.
    changed = _set_arp_ignore(engines.hb, 1, interfaces.hb_dut_1, interfaces.hb_dut_2)
    if changed:
        def restore_arp_ignore():
            for intf, orig_val in changed.items():
                _set_arp_ignore(engines.hb, orig_val, intf)
        request.addfinalizer(restore_arp_ignore)

    # generate arp by pinging from host
    gen_arp_table_via_ping(players, ping_info_list)
    logger.info("Collecting arp and mac table")
    engines.dut.run_cmd("show arp")
    engines.dut.run_cmd("show mac")


@pytest.fixture(scope='class', autouse=False)
def pre_configure_for_sub_interface(request, engines, topology_obj, interfaces, cli_objects, players):
    """
    Pytest fixture which are doing configuration for ecmp sub interface vrf case
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    :param interfaces: interfaces object fixture
    :param cli_objects: cli_objects object fixture
    :param players: players object fixture
    """
    # IP config which will be used in test
    ip_config_dict = {
        'dut': [{'iface': f"{interfaces.dut_ha_1}.100",
                 'ips': [(V4_CONFIG['dut_ha_1'], '24'), (V6_CONFIG['dut_ha_1'], '64')]},
                {'iface': f"{interfaces.dut_ha_2}.200",
                 'ips': [(V4_CONFIG['dut_ha_2'], '24'), (V6_CONFIG['dut_ha_2'], '64')]},
                {'iface': f"{interfaces.dut_hb_1}.300",
                 'ips': [(V4_CONFIG['dut_hb_1'], '24'), (V6_CONFIG['dut_hb_1'], '64')]},
                {'iface': f"{interfaces.dut_hb_2}.400",
                 'ips': [(V4_CONFIG['dut_hb_2'], '24'), (V6_CONFIG['dut_hb_2'], '64')]},
                ],
        'ha': [{'iface': f"{interfaces.ha_dut_1}.100",
                'ips': [(V4_CONFIG['ha_dut_1'], '24'), (V6_CONFIG['ha_dut_1'], '64')]},
               {'iface': f"{interfaces.ha_dut_2}.200",
                'ips': [(V4_CONFIG['ha_dut_2'], '24'), (V6_CONFIG['ha_dut_2'], '64')]}],
        'hb': [{'iface': f"{interfaces.hb_dut_1}.300",
                'ips': [(V4_CONFIG['hb_dut_1'], '24'), (V6_CONFIG['hb_dut_1'], '64')]},
               {'iface': f"{interfaces.hb_dut_2}.400",
                'ips': [(V4_CONFIG['hb_dut_2'], '24'), (V6_CONFIG['hb_dut_2'], '64')]}]

    }

    # VLAN config which will be used in test
    vlan_config_dict = {
        'dut': [{'vlan_id': 100, 'vlan_members': []},
                {'vlan_id': 200, 'vlan_members': []},
                {'vlan_id': 300, 'vlan_members': []},
                {'vlan_id': 400, 'vlan_members': []},
                ],
    }

    # IP config which will be used in test
    sub_int_config_dict = {
        'dut': [{'iface': f"{interfaces.dut_ha_1}.100", "vlan_id": ""},
                {'iface': f"{interfaces.dut_ha_2}.200", "vlan_id": ""},
                {'iface': f"{interfaces.dut_hb_1}.300", "vlan_id": ""},
                {'iface': f"{interfaces.dut_hb_2}.400", "vlan_id": ""},
                ],
        'ha': [{'iface': f"{interfaces.ha_dut_1}", "vlan_id": "100"},
               {'iface': f"{interfaces.ha_dut_2}", "vlan_id": "200"},
               ],
        'hb': [{'iface': f"{interfaces.hb_dut_1}", "vlan_id": "300"},
               {'iface': f"{interfaces.hb_dut_2}", "vlan_id": "400"}]

    }

    ping_info_list = [{"host": "ha", "src_intf": f"{interfaces.ha_dut_1}.100", "dst_intf": 'dut_ha_1'},
                      {"host": "ha", "src_intf": f"{interfaces.ha_dut_2}.200", "dst_intf": 'dut_ha_2'},
                      {"host": "hb", "src_intf": f"{interfaces.hb_dut_1}.300", "dst_intf": 'dut_hb_1'},
                      {"host": "hb", "src_intf": f"{interfaces.hb_dut_2}.400", "dst_intf": 'dut_hb_2'}]

    # Static route config
    static_route_config_dict = get_interface_test_static_route_config()

    VlanConfigTemplate.configuration(topology_obj, vlan_config_dict, request)
    SubIntConfigTemplate.configuration(topology_obj, sub_int_config_dict, request)
    IpConfigTemplate.configuration(topology_obj, ip_config_dict, request)
    RouteConfigTemplate.configuration(topology_obj, static_route_config_dict, request)

    # generate arp by pinging from host
    gen_arp_table_via_ping(players, ping_info_list)


@pytest.fixture(scope='module', autouse=False)
def adapt_speed_for_lag(request, engines, topology_obj, interfaces, cli_objects, platform_params):
    """
    Pytest fixture which is doing configuration for lag
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    :param interfaces: interfaces object fixture
    :param cli_objects: cli_objects object fixture
    :param platform_params: platform_params object fixture
    """
    # VLAN config which will be used in test
    cli_obj = topology_obj.players['dut']['cli']
    dut_original_interfaces_speeds = cli_obj.interface.get_interfaces_speed(
        [interfaces.dut_ha_2, interfaces.dut_hb_1, interfaces.dut_hb_2])
    new_speed = '800G' if platform_params.filtered_platform in PLATFORM_800G_SUPPORT_LIST else '10G'
    interfaces_config_dict = {
        'dut': [{'iface': interfaces.dut_ha_2, 'speed': new_speed,
                 'original_speed': dut_original_interfaces_speeds.get(interfaces.dut_ha_2, new_speed)},
                {'iface': interfaces.dut_hb_1, 'speed': new_speed,
                 'original_speed': dut_original_interfaces_speeds.get(interfaces.dut_hb_1, new_speed)},
                {'iface': interfaces.dut_hb_2, 'speed': new_speed,
                 'original_speed': dut_original_interfaces_speeds.get(interfaces.dut_hb_2, new_speed)}
                ]
    }
    InterfaceConfigTemplate.configuration(topology_obj, interfaces_config_dict, request)


@pytest.fixture(scope='class', autouse=False)
def pre_configure_for_lag(request, engines, topology_obj, interfaces, cli_objects, players, adapt_speed_for_lag):
    """
    Pytest fixture which is doing configuration for ecmp lag case
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    :param interfaces: interfaces object fixture
    :param cli_objects: cli_objects object fixture
    :param players: players object fixture
    :param adapt_speed_for_lag: adapt_speed_for_lag fixture
    """
    # LAG/LACP config which will be used in test
    lag_lacp_config_dict = {
        'dut': [{'type': 'lacp', 'name': 'PortChannel1', 'members': [interfaces.dut_ha_2]},
                {'type': 'lacp', 'name': 'PortChannel2', 'members': [interfaces.dut_hb_1, interfaces.dut_hb_2]}],
        'ha': [{'type': 'lacp', 'name': 'bond1', 'members': [interfaces.ha_dut_2]}],
        'hb': [{'type': 'lacp', 'name': 'bond2', 'members': [interfaces.hb_dut_1, interfaces.hb_dut_2]}]
    }

    # IP config which will be used in test
    ip_config_dict = {
        'dut': [{'iface': interfaces.dut_ha_1, 'ips': [(V4_CONFIG['dut_ha_1'], '24'), (V6_CONFIG['dut_ha_1'], '64')]},
                {'iface': 'PortChannel1', 'ips': [(V4_CONFIG['dut_ha_2'], '24'), (V6_CONFIG['dut_ha_2'], '64')]},
                {'iface': 'PortChannel2', 'ips': [(V4_CONFIG['dut_hb_1'], '24'), (V6_CONFIG['dut_hb_1'], '64')]},
                ],
        'ha': [{'iface': interfaces.ha_dut_1, 'ips': [(V4_CONFIG['ha_dut_1'], '24'), (V6_CONFIG['ha_dut_1'], '64')]},
               {'iface': "bond1", 'ips': [(V4_CONFIG['ha_dut_2'], '24'), (V6_CONFIG['ha_dut_2'], '64')]}],
        'hb': [{'iface': "bond2", 'ips': [(V4_CONFIG['hb_dut_1'], '24'), (V6_CONFIG['hb_dut_1'], '64')]}]
    }

    ping_info_list = [{"host": "ha", "src_intf": interfaces.ha_dut_1, "dst_intf": 'dut_ha_1'},
                      {"host": "ha", "src_intf": "bond1", "dst_intf": 'dut_ha_2'},
                      {"host": "hb", "src_intf": "bond2", "dst_intf": 'dut_hb_1'}]

    # Static route config
    static_route_config_dict = get_lag_test_static_route_config()

    LagLacpConfigTemplate.configuration(topology_obj, lag_lacp_config_dict, request)
    IpConfigTemplate.configuration(topology_obj, ip_config_dict, request)
    RouteConfigTemplate.configuration(topology_obj, static_route_config_dict, request)

    # generate arp by pinging from host
    gen_arp_table_via_ping(players, ping_info_list)


@pytest.fixture(scope='class', autouse=False)
def pre_configure_for_vlan_lag(request, engines, topology_obj, interfaces, cli_objects, players, adapt_speed_for_lag):
    """
    Pytest fixture which is doing configuration for lag in vlan case
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    :param interfaces: interfaces object fixture
    :param cli_objects: cli_objects object fixture
    :param players: players object fixture
    :param adapt_speed_for_lag: adapt_speed_for_lag fixture
    """
    # LAG/LACP config which will be used in test
    lag_lacp_config_dict = {
        'dut': [{'type': 'lacp', 'name': 'PortChannel1', 'members': [interfaces.dut_hb_1, interfaces.dut_hb_2]}],
        'hb': [{'type': 'lacp', 'name': 'bond1', 'members': [interfaces.hb_dut_1, interfaces.hb_dut_2]}]
    }

    # VLAN config which will be used in test
    vlan_config_dict = {
        'dut': [{'vlan_id': 100, 'vlan_members': [{interfaces.dut_ha_1: 'access'}]},
                {'vlan_id': 200, 'vlan_members': [{interfaces.dut_ha_2: 'access'}, {"PortChannel1": 'access'}]}],
    }

    # IP config which will be used in test
    ip_config_dict = {
        'dut': [{'iface': "Vlan100", 'ips': [(V4_CONFIG['dut_ha_1'], '24'), (V6_CONFIG['dut_ha_1'], '64')]},
                {'iface': 'Vlan200', 'ips': [(V4_CONFIG['dut_ha_2'], '24'), (V6_CONFIG['dut_ha_2'], '64')]},
                ],
        'ha': [{'iface': interfaces.ha_dut_1, 'ips': [(V4_CONFIG['ha_dut_1'], '24'), (V6_CONFIG['ha_dut_1'], '64')]},
               {'iface': interfaces.ha_dut_2, 'ips': [(V4_CONFIG['ha_dut_2'], '24'), (V6_CONFIG['ha_dut_2'], '64')]}],
        'hb': [{'iface': "bond1", 'ips': [(V4_CONFIG['vlan_200_bond1'], '24'), (V6_CONFIG['vlan_200_bond1'], '64')]}]
    }

    # Static route config
    static_route_config_dict = {
        'dut': [{'dst': DEST_ROUTE_V4["prefix"], 'dst_mask': DEST_ROUTE_V4["mask"],
                 'via': [V4_CONFIG['ha_dut_2'], V4_CONFIG['vlan_200_bond1']]},
                {'dst': DEST_ROUTE_V6["prefix"], 'dst_mask': DEST_ROUTE_V6["mask"],
                 'via': [V6_CONFIG['ha_dut_2'], V6_CONFIG['vlan_200_bond1']]}
                ]
    }

    ping_info_list = [{"host": "ha", "src_intf": interfaces.ha_dut_1, "dst_intf": 'dut_ha_1'},
                      {"host": "ha", "src_intf": interfaces.ha_dut_2, "dst_intf": 'dut_ha_2'},
                      {"host": "hb", "src_intf": "bond1", "dst_intf": 'dut_ha_2'}]

    LagLacpConfigTemplate.configuration(topology_obj, lag_lacp_config_dict, request)
    VlanConfigTemplate.configuration(topology_obj, vlan_config_dict, request)
    IpConfigTemplate.configuration(topology_obj, ip_config_dict, request)
    RouteConfigTemplate.configuration(topology_obj, static_route_config_dict, request)

    # generate arp by pinging from host
    gen_arp_table_via_ping(players, ping_info_list)


@pytest.fixture(scope='class', autouse=False)
def pre_configure_for_sub_interface_lag(request, engines, topology_obj, interfaces, cli_objects, players, adapt_speed_for_lag):
    """
    Pytest fixture which is doing configuration for lag
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    :param interfaces: interfaces object fixture
    :param cli_objects: cli_objects object fixture
    :param players: players object fixture
    :param adapt_speed_for_lag: adapt_speed_for_lag fixture
    """
    # VLAN config which will be used in test
    vlan_config_dict = {
        'dut': [
            {'vlan_id': 200, 'vlan_members': []},
            {'vlan_id': 300, 'vlan_members': []}
        ],
    }

    # LAG/LACP config which will be used in test
    lag_lacp_config_dict = {
        'dut': [{'type': 'lacp', 'name': 'PortChannel1', 'members': [interfaces.dut_ha_2]},
                {'type': 'lacp', 'name': 'PortChannel2', 'members': [interfaces.dut_hb_1, interfaces.dut_hb_2]}],
        'ha': [{'type': 'lacp', 'name': 'bond1', 'members': [interfaces.ha_dut_2]}],
        'hb': [{'type': 'lacp', 'name': 'bond2', 'members': [interfaces.hb_dut_1, interfaces.hb_dut_2]}]
    }

    # IP config which will be used in test
    ip_config_dict = {
        'dut': [{'iface': interfaces.dut_ha_1, 'ips': [(V4_CONFIG['dut_ha_1'], '24'), (V6_CONFIG['dut_ha_1'], '64')]},
                {'iface': 'Po1.200', 'ips': [(V4_CONFIG['dut_ha_2'], '24'), (V6_CONFIG['dut_ha_2'], '64')]},
                {'iface': 'Po2.300', 'ips': [(V4_CONFIG['dut_hb_1'], '24'), (V6_CONFIG['dut_hb_1'], '64')]},
                ],
        'ha': [{'iface': interfaces.ha_dut_1, 'ips': [(V4_CONFIG['ha_dut_1'], '24'), (V6_CONFIG['ha_dut_1'], '64')]},
               {'iface': "bond1.200", 'ips': [(V4_CONFIG['ha_dut_2'], '24'), (V6_CONFIG['ha_dut_2'], '64')]}],
        'hb': [{'iface': "bond2.300", 'ips': [(V4_CONFIG['hb_dut_1'], '24'), (V6_CONFIG['hb_dut_1'], '64')]}]
    }

    # IP config which will be used in test
    sub_int_config_dict = {
        'dut': [{'iface': "Po1.200", "vlan_id": "200"},
                {'iface': "Po2.300", "vlan_id": "300"},
                ],
        'ha': [{'iface': "bond1", "vlan_id": "200"}],
        'hb': [{'iface': "bond2", "vlan_id": "300"}]

    }

    ping_info_list = [{"host": "ha", "src_intf": interfaces.ha_dut_1, "dst_intf": 'dut_ha_1'},
                      {"host": "ha", "src_intf": "bond1.200", "dst_intf": 'dut_ha_2'},
                      {"host": "hb", "src_intf": "bond2.300", "dst_intf": 'dut_hb_1'}]

    # Static route config
    static_route_config_dict = get_lag_test_static_route_config()

    VlanConfigTemplate.configuration(topology_obj, vlan_config_dict, request)
    LagLacpConfigTemplate.configuration(topology_obj, lag_lacp_config_dict, request)
    SubIntConfigTemplate.configuration(topology_obj, sub_int_config_dict, request)
    IpConfigTemplate.configuration(topology_obj, ip_config_dict, request)
    RouteConfigTemplate.configuration(topology_obj, static_route_config_dict, request)

    # generate arp by pinging from host
    gen_arp_table_via_ping(players, ping_info_list)


@pytest.fixture(scope='class')
def copy_negative_json_to_syncd(engines):
    """
    Pytest fixture which are copying files in data folder to syncd docker
    :param engines: engines object fixture
    """
    data_path = os.path.join(ECMP_CALCULATOR_PATH, "data")
    for file_name in os.listdir(data_path):
        copy_packet_json_to_syncd(engines.dut, file_name, data_path)


def get_interface_test_static_route_config(vrf=None):
    static_route_config_dict = {
        'dut': [{'dst': DEST_ROUTE_V4["prefix"], 'dst_mask': DEST_ROUTE_V4["mask"],
                 'via': [V4_CONFIG['ha_dut_2'], V4_CONFIG['hb_dut_1'], V4_CONFIG['hb_dut_2']], 'vrf': vrf},
                {'dst': DEST_ROUTE_V6["prefix"], 'dst_mask': DEST_ROUTE_V6["mask"],
                 'via': [V6_CONFIG['ha_dut_2'], V6_CONFIG['hb_dut_1'], V6_CONFIG['hb_dut_2']], 'vrf': vrf}
                ]
    }
    logger.info(f"Interface test static route_config:{static_route_config_dict}")
    return static_route_config_dict


def get_lag_test_static_route_config():
    static_route_config_dict = {
        'dut': [{'dst': DEST_ROUTE_V4["prefix"], 'dst_mask': DEST_ROUTE_V4["mask"],
                 'via': [V4_CONFIG['ha_dut_2'], V4_CONFIG['hb_dut_1']]},
                {'dst': DEST_ROUTE_V6["prefix"], 'dst_mask': DEST_ROUTE_V6["mask"],
                 'via': [V6_CONFIG['ha_dut_2'], V6_CONFIG['hb_dut_1']]}
                ]
    }
    logger.info(f"Lag test static route_config:{static_route_config_dict}")
    return static_route_config_dict


def gen_arp_table_via_ping(players, ping_info_list, only_ping_v4=False):
    def ping_ports(ip_config):
        for ping_info in ping_info_list:
            if "ping_ip_v4" in ping_info:
                ping_ip = ping_info["ping_ip_v4"]
            elif "ping_ip_v6" in ping_info:
                ping_ip = ping_info["ping_ip_v6"]
            else:
                ping_ip = ip_config[ping_info["dst_intf"]]
            generate_arp(players, ping_info["src_intf"], ping_info["host"], ping_ip)

    ping_ports(V4_CONFIG)
    if not only_ping_v4:
        ping_ports(V6_CONFIG)


def _set_arp_ignore(host_engine, value, *interfaces):
    """Set arp_ignore on host interfaces and return a dict of original values.

    When multiple interfaces on the same host share a VLAN/subnet, Linux default
    arp_ignore=0 causes each interface to reply to ARP requests for IPs on any
    other interface, using its own MAC. This leads to random neighbor MAC swaps
    on the DUT. Setting arp_ignore=1 restricts each interface to only reply for
    IPs configured on itself.

    Returns {intf: original_value} for interfaces that were changed, or empty
    dict if all interfaces already had the requested value.
    """
    changed = {}
    for intf in interfaces:
        original = host_engine.run_cmd(f"sysctl -n net.ipv4.conf.{intf}.arp_ignore").strip()
        if original == str(value):
            logger.info(f"arp_ignore for {intf} already set to {value}, skipping")
            continue
        host_engine.run_cmd(f"sysctl -w net.ipv4.conf.{intf}.arp_ignore={value}")
        actual = host_engine.run_cmd(f"sysctl -n net.ipv4.conf.{intf}.arp_ignore").strip()
        logger.info(f"arp_ignore for {intf}: {original} -> {actual}")
        changed[intf] = original
    return changed
