import pytest
import logging
import allure
import os
import copy
import random

from ngts.config_templates.lag_lacp_config_template import LagLacpConfigTemplate
from ngts.config_templates.ip_config_template import IpConfigTemplate
from ngts.config_templates.interfaces_config_template import InterfaceConfigTemplate
from devts.infra.tools.validations.traffic_validations.ping.ping_runner import PingChecker
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.tests.nightly.rocev2_acl_counter.constants import V4_CONFIG, V6_CONFIG, ROCEV2_ACL_BASIC_TEST_DATA, TEST_COMBINATION
from ngts.helpers.rocev2_acl_counter_helper import copy_apply_rocev2_acl_config, remove_rocev2_acl_rule_and_talbe, \
    BTH_OPCODE_NAK_TYPE_AMP, is_support_rocev2_acl_counter_feature
from retry.api import retry_call
from jinja2 import Template
from ngts.constants.constants import PLATFORM_200G_SUPPORT_LIST, PLATFORM_800G_SUPPORT_LIST

ROCEV2_ACL_COUNTER_PATH = os.path.dirname(os.path.abspath(__file__))
ROCEV2_ACL_COUNTER_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template")

logger = logging.getLogger()
DUT_PORTCHANNEL_NAME = "PortChannel1"


def generate_arp(players, interface, sender, dst_ip):
    validation = {'sender': sender, 'args': {'interface': interface, 'count': 3, 'dst': dst_ip}}
    ping = PingChecker(players, validation)
    logger.info('Sending 3 ping packets to {} from interface {}'.format(dst_ip, interface))
    retry_call(ping.run_validation, fargs=[], tries=3, delay=5, logger=logger)


@pytest.fixture(scope='package', autouse=True)
def skipping_rocev2_acl_counter_tests(cli_objects, is_simx, sonic_branch):
    if not is_support_rocev2_acl_counter_feature(cli_objects, is_simx, sonic_branch):
        pytest.skip("The rocev2 acl counter feature is missing, skipping the test case")


@pytest.fixture(scope='class', autouse=False)
def toggle_tested_port(interfaces, cli_objects, players):
    """
    Pytest fixture which is to toggle tested ports
    :param interfaces: interfaces object fixture
    :param cli_objects: cli_objects object fixture
    :param players: players object fixture
    """
    tested_ports = [interfaces.dut_ha_1, DUT_PORTCHANNEL_NAME]
    with allure.step(f"Shutdown tested port:{tested_ports}"):
        cli_objects.dut.interface.disable_interfaces(tested_ports)

        with allure.step("Verify ports: {tested_ports} are down"):
            cli_objects.dut.interface.check_link_state(ifaces=tested_ports, expected_status='down')

    with allure.step(f"Start tested port:{tested_ports}"):
        cli_objects.dut.interface.enable_interfaces(tested_ports)

        with allure.step("Verify ports: {tested_ports} are up"):
            cli_objects.dut.interface.check_link_state(ifaces=tested_ports, expected_status='up')

    with allure.step(f"After toggle, generate arp "):
        gen_arp_table_via_ping(players, interfaces)

    yield
    ports_status = cli_objects.dut.interface.parse_interfaces_status()
    if ports_status[interfaces.dut_ha_1]['Oper'] == 'down' or ports_status[DUT_PORTCHANNEL_NAME]['Oper'] == 'down':
        with allure.step(f"Recover tested port:{tested_ports}"):
            cli_objects.dut.interface.enable_interfaces(tested_ports)


@pytest.fixture(scope='module', autouse=True)
def rocev2_acl_rule_list(interfaces, platform_params):
    """
    Pytest fixture which is to generate acl rocev2 config
    :param interfaces: interfaces object fixture
    """
    yield gen_acl_rule_list(interfaces, platform_params)


@pytest.fixture(scope='module', autouse=False)
def apply_rocev2_acl_config(topology_obj, interfaces, engines, rocev2_acl_rule_list):
    """
    Pytest fixture which is to apply acl rocev2 config on dut
    :param topology_obj: topology_obj object fixture
    :param interfaces: interfaces object fixture
    :param engines: engines object fixture
    :param engines: rocev2_acl_rule_list object fixture
    """
    acl_rocev2_config_filename = "rocev2_acl_config.json"
    gen_acl_config_json_file(interfaces, rocev2_acl_rule_list, acl_rocev2_config_filename)
    copy_apply_rocev2_acl_config(engines.dut, acl_rocev2_config_filename, ROCEV2_ACL_COUNTER_TEMPLATE_PATH)

    yield
    acl_type_list = ["CUSTOM_L3", "CUSTOM_L3_E"]
    remove_rocev2_acl_rule_and_talbe(topology_obj, ["ROCE_ACL_INGRESS", "ROCE_ACL_EGRESS"], acl_type_list)


@pytest.fixture(scope='module', autouse=False)
def adapt_speed_for_lag(engines, topology_obj, interfaces, cli_objects, platform_params):
    """
    Pytest fixture which is doing configuration for lag
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    :param interfaces: interfaces object fixture
    :param cli_objects: cli_objects object fixture
    """
    # VLAN config which will be used in test
    cli_obj = topology_obj.players['dut']['cli']
    dut_original_interfaces_speeds = cli_obj.interface.get_interfaces_speed(
        [interfaces.dut_ha_2, interfaces.dut_hb_1, interfaces.dut_hb_2])
    if platform_params.filtered_platform in PLATFORM_800G_SUPPORT_LIST:
        new_speed = "800G"
    elif platform_params.filtered_platform in PLATFORM_200G_SUPPORT_LIST:
        new_speed = "200G"
    else:
        new_speed = "10G"
    interfaces_config_dict = {
        'dut': [{'iface': interfaces.dut_ha_2, 'speed': new_speed,
                 'original_speed': dut_original_interfaces_speeds.get(interfaces.dut_ha_2, new_speed)}
                ]
    }
    InterfaceConfigTemplate.configuration(topology_obj, interfaces_config_dict)

    yield

    InterfaceConfigTemplate.cleanup(topology_obj, interfaces_config_dict)


@pytest.fixture(scope='module', autouse=True)
def pre_configure(request, engines, topology_obj, interfaces, cli_objects, players, adapt_speed_for_lag, platform_params):
    """
    Pytest fixture which is doing basic configuration for rocev2 test
    :param request: request object fixture
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    :param interfaces: interfaces object fixture
    :param cli_objects: cli_objects object fixture
    :param players: players object fixture
    :param adapt_speed_for_lag: adapt_speed_for_lag fixture
    """
    # LAG/LACP config which will be used in test
    lag_lacp_config_dict = {
        'dut': [{'type': 'lacp', 'name': DUT_PORTCHANNEL_NAME, 'members': [interfaces.dut_ha_2]}],
        'ha': [{'type': 'lacp', 'name': 'bond1', 'members': [interfaces.ha_dut_2]}]
    }

    # IP config which will be used in test
    ip_config_dict = {
        'dut': [{'iface': interfaces.dut_ha_1, 'ips': [(V4_CONFIG['dut_ha_1'], '24'), (V6_CONFIG['dut_ha_1'], '64')]},
                {'iface': DUT_PORTCHANNEL_NAME, 'ips': [(V4_CONFIG['dut_ha_2'], '24'), (V6_CONFIG['dut_ha_2'], '64')]},
                {'iface': interfaces.dut_hb_1, 'ips': [(V4_CONFIG['dut_hb_1'], '24'), (V6_CONFIG['dut_hb_1'], '64')]},
                ],
        'ha': [{'iface': interfaces.ha_dut_1, 'ips': [(V4_CONFIG['ha_dut_1'], '24'), (V6_CONFIG['ha_dut_1'], '64')]},
               {'iface': "bond1", 'ips': [(V4_CONFIG['ha_dut_2'], '24'), (V6_CONFIG['ha_dut_2'], '64')]}],
        'hb': [{'iface': interfaces.hb_dut_1, 'ips': [(V4_CONFIG['hb_dut_1'], '24'), (V6_CONFIG['hb_dut_1'], '64')]}]
    }

    LagLacpConfigTemplate.configuration(topology_obj, lag_lacp_config_dict)
    IpConfigTemplate.configuration(topology_obj, ip_config_dict)
    cmd = f"sudo config mirror_session span add port0 {interfaces.dut_hb_2} {interfaces.dut_ha_1},{DUT_PORTCHANNEL_NAME}"
    if platform_params.filtered_platform in PLATFORM_200G_SUPPORT_LIST:
        cmd += " rx"
    engines.dut.run_cmd(cmd)

    def recover_config():
        engines.dut.run_cmd(f"sudo config mirror_session remove port0 ")
        IpConfigTemplate.cleanup(topology_obj, ip_config_dict)
        LagLacpConfigTemplate.cleanup(topology_obj, lag_lacp_config_dict)

    request.addfinalizer(recover_config)
    # generate arp by pinging from host
    gen_arp_table_via_ping(players, interfaces)


def gen_arp_table_via_ping(players, interfaces):
    ping_info_list = [{"host": "ha", "src_intf": interfaces.ha_dut_1, "dst_intf": 'dut_ha_1'},
                      {"host": "ha", "src_intf": "bond1", "dst_intf": 'dut_ha_2'},
                      {"host": "hb", "src_intf": interfaces.hb_dut_1, "dst_intf": 'dut_hb_1'}]

    def ping_ports(ip_config):
        for ping_info in ping_info_list:
            generate_arp(players, ping_info["src_intf"], ping_info["host"], ip_config[ping_info["dst_intf"]])

    ping_ports(V4_CONFIG)
    ping_ports(V6_CONFIG)


def gen_acl_rule_list(interfaces, platform_params):
    """
    This method is to generate acl rule list
    :param interfaces: interfaces object fixture
    """

    acl_rule_list = []
    acl_rule_counter = 0
    for index, test_scenario in enumerate(TEST_COMBINATION):
        ipv4_base = "{}0.0".format(index + 1)
        ipv6_base = "{}0".format(index + 1)
        priority_base = "{}0".format(index + 1)
        if test_scenario['scenario'] == "bth_aeth_together_random":
            tested_bth_opcode = random.choice(list(BTH_OPCODE_NAK_TYPE_AMP.keys()))
            logger.info(f"Selected bth_opcode is{tested_bth_opcode}")

        for acl_rule in ROCEV2_ACL_BASIC_TEST_DATA:
            acl_rule_new = copy.copy(acl_rule)
            acl_rule_new.update(test_scenario)
            if test_scenario['scenario'] == "bth_aeth_together_random":
                acl_rule_new['bth_opcode'] = tested_bth_opcode
            acl_rule_counter += 1
            acl_rule_new["name"] = f'{acl_rule_new["name"]}_{acl_rule_counter}'

            acl_rule_new["priority"] = acl_rule_new["priority"].replace("10", priority_base, 1)
            if acl_rule_new["src_type"] == "SRC_IP":
                acl_rule_new["src_ip"] = acl_rule_new["src_ip"].replace("10.0", ipv4_base, 1)
            else:
                acl_rule_new["src_ip"] = acl_rule_new["src_ip"].replace("10", ipv6_base, 1)
            if not acl_rule_new["action"]:
                acl_rule_new["action"] = f"REDIRECT:{interfaces.dut_hb_2}"
            acl_rule_list.append(acl_rule_new)

    return acl_rule_list


def gen_acl_config_json_file(interfaces, acl_rule_list, acl_config_json_file_name):
    """
    This method is to generate acl config json file according to the j2 template
    :param interfaces: interfaces object fixture
    :param acl_rule_list: acl rule list
    :param acl_config_json_file_name: acl config json file name
    """
    logger.info(f"Generate alc rule config json file with template file ")
    with open(os.path.join(ROCEV2_ACL_COUNTER_TEMPLATE_PATH, "rocev2_acl_config.j2")) as template_file:
        t = Template(template_file.read())

    content = t.render(acl_rule_list=acl_rule_list, acl_rule_len=len(acl_rule_list), physic_port=interfaces.dut_ha_1, lag_port=DUT_PORTCHANNEL_NAME)
    logger.debug(f"acl rocev2 config content is {content}")

    logger.info(f"Save  acl rocev2 config json to {acl_config_json_file_name}")
    with open(os.path.join(ROCEV2_ACL_COUNTER_TEMPLATE_PATH, acl_config_json_file_name), "w", encoding='utf-8') as f:
        f.write(content)
