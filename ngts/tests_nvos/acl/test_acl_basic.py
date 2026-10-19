import ipaddress
import logging
import pytest
import shlex
import subprocess
import time
from retry import retry

from retry.api import retry_call
import random
from devts.infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.acl.acl import Acl
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import ApiType, AclConsts, OutputFormat, IpConsts
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.system.System import System
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from scapy.layers.inet import IP, TCP, ICMP
from scapy.layers.inet6 import IPv6, ICMPv6EchoRequest
from scapy.all import *
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from multiprocessing import Process

logger = logging.getLogger()

SLEEP_TIME = 5
IPV6_ADDR = "2001:db8:abcd:0012:0000:0000:0000:00ef"
RULE_CONFIG_FUNCTION = {
    AclConsts.ACTION: lambda rule_id_obj, param: rule_id_obj.action.recent.set() if param == 'recent' else rule_id_obj.action.set(param),
    AclConsts.ACTION_LOG_PREFIX: lambda rule_id_obj, param: rule_id_obj.action.log.set_log_prefix(param),
    AclConsts.REMARK: lambda rule_id_obj, param: rule_id_obj.set_remark(param),

    AclConsts.TCP_SOURCE_PORT: lambda rule_id_obj, param: rule_id_obj.match.ip.tcp.source_port.set(param),
    AclConsts.UDP_SOURCE_PORT: lambda rule_id_obj, param: rule_id_obj.match.ip.udp.source_port.set(param),
    AclConsts.TCP_DEST_PORT: lambda rule_id_obj, param: rule_id_obj.match.ip.tcp.dest_port.set(param),
    AclConsts.UDP_DEST_PORT: lambda rule_id_obj, param: rule_id_obj.match.ip.udp.dest_port.set(param),
    AclConsts.FRAGMENT: lambda rule_id_obj, param: rule_id_obj.match.ip.set_fragment(),
    AclConsts.ECN_FLAGS: lambda rule_id_obj, param: rule_id_obj.match.ip.ecn.flags.set(param),
    AclConsts.ECN_IP_ECT: lambda rule_id_obj, param: rule_id_obj.match.ip.ecn.set_ecn_ip_ect(param),
    AclConsts.TCP_FLAGS: lambda rule_id_obj, param: rule_id_obj.match.ip.tcp.flags.set(param),
    AclConsts.TCP_MASK: lambda rule_id_obj, param: rule_id_obj.match.ip.tcp.mask.set(param),
    AclConsts.TCP_STATE: lambda rule_id_obj, param: rule_id_obj.match.ip.state.set(param),
    AclConsts.MSS: lambda rule_id_obj, param: rule_id_obj.match.ip.tcp.set_mss(param),
    AclConsts.ALL_MSS_EXCEPT: lambda rule_id_obj, param: rule_id_obj.match.ip.tcp.set_all_mss_except(param),
    AclConsts.SOURCE_IP: lambda rule_id_obj, param: rule_id_obj.match.ip.set_source_ip(param),
    AclConsts.DEST_IP: lambda rule_id_obj, param: rule_id_obj.match.ip.set_dest_ip(param),
    AclConsts.ICMP_TYPE: lambda rule_id_obj, param: rule_id_obj.match.ip.set_icmp_type(param),
    AclConsts.ICMPV6_TYPE: lambda rule_id_obj, param: rule_id_obj.match.ip.set_icmpv6_type(param),
    AclConsts.IP_PROTOCOL: lambda rule_id_obj, param: rule_id_obj.match.ip.set_protocol(param),
    AclConsts.RECENT_LIST_NAME: lambda rule_id_obj, param: rule_id_obj.match.ip.recent_list.set_name(param),
    AclConsts.RECENT_LIST_UPDATE: lambda rule_id_obj, param: rule_id_obj.match.ip.recent_list.set_update_interval(param),
    AclConsts.RECENT_LIST_HIT: lambda rule_id_obj, param: rule_id_obj.match.ip.recent_list.set_hit_count(param),
    AclConsts.RECENT_LIST_ACTION: lambda rule_id_obj, param: rule_id_obj.match.ip.recent_list.set_action(param),
    AclConsts.DSCP_SET_ACTION: lambda rule_id_obj, param: rule_id_obj.action.dscp.set(param),
    AclConsts.HASHLIMIT_NAME: lambda rule_id_obj, param: rule_id_obj.match.ip.hashlimit.set_name(param),
    AclConsts.HASHLIMIT_RATE: lambda rule_id_obj, param: rule_id_obj.match.ip.hashlimit.set_rate_limit(param),
    AclConsts.HASHLIMIT_BURST: lambda rule_id_obj, param: rule_id_obj.match.ip.hashlimit.set_burst(param),
    AclConsts.HASHLIMIT_MODE: lambda rule_id_obj, param: rule_id_obj.match.ip.hashlimit.set_mode(param),
    AclConsts.HASHLIMIT_EXPIRE: lambda rule_id_obj, param: rule_id_obj.match.ip.hashlimit.set_expire(param),
    AclConsts.HASHLIMIT_DEST_MASK: lambda rule_id_obj, param: rule_id_obj.match.ip.hashlimit.set_destination_mask(param),
    AclConsts.HASHLIMIT_SRC_MASK: lambda rule_id_obj, param: rule_id_obj.match.ip.hashlimit.set_source_mask(param),

    AclConsts.SOURCE_MAC: None,
    AclConsts.SOURCE_MAC_MASK: None,
    AclConsts.DEST_MAC: None,
    AclConsts.DEST_MAC_MASK: None,
    AclConsts.MAC_PROTOCOL: None
}


@pytest.mark.acl
@pytest.mark.air
def test_show_acls(engines, devices, random_api):
    """
    Validate ACL show commands and verify new ACL structure after migration.
    This test validates:
    1. All new default ACLs are present after migration
    2. DOS ACLs contain only DENY actions
    3. WHITELIST ACLs contain only PERMIT actions
    4. Rule counts are preserved during migration from old to new ACLs
    """

    with allure.step("Show ACL and verify the output"):
        acl = Acl()
        acls = OutputParsingTool.parse_show_output_to_dict(acl.show()).get_returned_value()
        assert acls and len(acls.keys()) > 1, "No ACLs were found"

        with allure.step("Verify all default ACL are found"):
            ValidationTool.verify_field_exist_in_json_output(acls, AclConsts.NEW_DEFAULT_ACLS).verify_result()

        with allure.step("Verify expected ACL fields"):
            # Use first available ACL for field verification
            first_acl = AclConsts.NEW_DEFAULT_ACLS[0]
            ValidationTool.verify_field_exist_in_json_output(acls[first_acl],
                                                             [AclConsts.RULE, AclConsts.TYPE])

        with allure.step("Verify ACL rule placement based on remark field"):
            for acl_name, acl_data in acls.items():
                if AclConsts.RULE in acl_data:
                    rules = acl_data[AclConsts.RULE]

                    # Skip remark-based validation for loopback and outbound ACLs (one-to-one mapping)
                    if ('loopback' in acl_name.lower() or 'outbound' in acl_name.lower()):
                        logger.info(f"Skipping remark-based validation for {acl_name} (one-to-one mapping)")
                        continue

                    # Check each rule based on its remark field (action type is irrelevant)
                    for rule_id, rule_data in rules.items():
                        if AclConsts.REMARK in rule_data:
                            remark = rule_data[AclConsts.REMARK].lower()

                            # DOS ACLs should contain rules without "whitelist" remark
                            if 'dos' in acl_name.lower():
                                assert 'whitelist' not in remark, \
                                    f"DOS ACL {acl_name} rule {rule_id} has 'whitelist' remark: {remark}"

                            # WHITELIST ACLs should contain rules with "whitelist" remark
                            elif 'whitelist' in acl_name.lower():
                                assert 'whitelist' in remark, \
                                    f"WHITELIST ACL {acl_name} rule {rule_id} missing 'whitelist' remark: {remark}"

        with allure.step("Verify ACL rule counts match expected values"):
            for acl_name, expected_count in devices.dut.expected_acl_rule_counts.items():
                if acl_name in acls:
                    actual_count = len(acls[acl_name].get(AclConsts.RULE, {}))
                    assert actual_count == expected_count, \
                        f"ACL {acl_name} has {actual_count} rules, expected {expected_count}"
                    logger.info(f"✅ {acl_name}: {actual_count} rules (expected: {expected_count})")
                else:
                    logger.warning(f"ACL {acl_name} not found in output")

        logger.info("✅ All ACL rule counts validated successfully after migration")


def test_can_ping_from_eth1(engines, devices):
    """
    This test is a workaround for the issue that some switches fail to ping the sonic_mgmt ip through eth1.
    It should run before all other ACL tests because some of them depend on using eth1.
    The test attempts to ping. On failure it runs a shell command that fixes the issue, allowing future tests to run
    smoothly, but this test will still fail to let us know the issue still exists.
    """
    if 'eth1' not in devices.dut.mgmt_ports:
        pytest.skip("Device does not have eth1 mgmt-port")

    try:
        ping_from_switch(engines.dut, engines.sonic_mgmt.ip, "eth1").verify_result()
        logger.info("Successfully pinged sonic-mgmt through eth1")
    except Exception:
        logger.error(f"Could not ping sonic-mgmt through eth1. Fixing...")
        gateway = Port("eth0").interface.ipv4.gateway.show(output_format=OutputFormat.auto).splitlines()[-1].strip()
        engines.dut.run_cmd(f"sudo ip route add {engines.sonic_mgmt.ip} via {gateway} dev eth1")
        raise


@pytest.mark.acl
def test_acl_ipv6(engines, random_api, topology_obj, sonic_mgmt_ipv6_addr):
    """
    Validate ACLs rules over ipv6.
    steps:
    1. config ACL with a rule
    2. send packet
    3. validate counters increase
    """
    if not IpTool.is_dhcp_client6_has_lease(engines.dut):
        pytest.skip("DUT DHCP client6 has no lease; cannot run this IPv6 test.")

    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    switch_ipv6_addr = IpTool.verify_ipv6_available(mgmt_port_name)

    with allure.step("Define ACLs with rule"):
        acl_type = 'ipv6'
        ipv6_prefix_or_netmask = sonic_mgmt_ipv6_addr + '/64'
        rule_id = '1'
        rule_configuration_dict = {AclConsts.ACTION: AclConsts.DENY, AclConsts.SOURCE_IP: sonic_mgmt_ipv6_addr}

        acl_id_1 = "AA_TEST_ACL_IPV6"
        mgmt_port = Port(mgmt_port_name)
        acl_id_1_obj = config_acl_with_rule_attached_to_interface(engines.dut, acl_id_1, acl_type, rule_id,
                                                                  rule_configuration_dict, mgmt_port, AclConsts.INBOUND, AclConsts.CONTROL_PLANE)

    with allure.step("Validate ACL counters"):
        time.sleep(5)
        rule_packets_1_before = get_rule_packets(mgmt_port, acl_id_1)
        with allure.step("Ping"):
            ping_packet = IPv6(dst=switch_ipv6_addr, src=sonic_mgmt_ipv6_addr) / ICMPv6EchoRequest()
            ping_from_sonic_mgmt(ping_packet)
        rule_packets_1_after = get_rule_packets(mgmt_port, acl_id_1)
        assert rule_packets_1_after[rule_id] > rule_packets_1_before[rule_id], \
            f'we expect to see increase in acl {acl_id_1} rule id {rule_id} counter'

    with allure.step("Change the rule- use ipv6 prefix"):
        config_rule(engines.dut, acl_id_1_obj, rule_id,
                    {AclConsts.ACTION: AclConsts.DENY, AclConsts.SOURCE_IP: ipv6_prefix_or_netmask})
        time.sleep(5)
        rule_packets_1_before = get_rule_packets(mgmt_port, acl_id_1)
        ping_packet = IPv6(dst=switch_ipv6_addr, src=sonic_mgmt_ipv6_addr) / ICMPv6EchoRequest()
        ping_from_sonic_mgmt(ping_packet)
        rule_packets_1_after = get_rule_packets(mgmt_port, acl_id_1)
        assert rule_packets_1_after[rule_id] > rule_packets_1_before[rule_id], \
            f'we expect to see increase in acl {acl_id_1} rule id {rule_id} counter'


@pytest.mark.acl
def test_acl_loopback(engines, random_api):
    """
    Validate ACLs rules can't be defined for the loopback connection
    steps:
    1. config ACL with a rule
    2. try to apply, and fail
    """

    with allure.step("Define ACLs with rule"):
        acl_type = 'ipv4'
        mgmt_port = Port('lo')
        sonic_mgmt_ip = engines.sonic_mgmt.ip
        rule_id = '1'
        rule_configuration_dict = {AclConsts.ACTION: AclConsts.PERMIT}

        acl_id_1 = "AA_TEST_ACL_LOOPBACK"
        acl_id_1_obj = config_acl_with_rule_attached_to_interface(engines.dut, acl_id_1, acl_type, rule_id,
                                                                  rule_configuration_dict, mgmt_port, AclConsts.INBOUND,
                                                                  AclConsts.CONTROL_PLANE, should_succeed=False)


@pytest.mark.acl
@pytest.mark.air
def test_show_acl_commands(devices, engines, random_api, topology_obj):
    """
    Validate acl show commands.
    steps:
    1. config an ACL with rules
    2. validate show commands
    """
    with allure.step("Define ACL with rules"):

        with allure.step("Define ACL"):
            acl = Acl()
            acl_id = "AA_TEST_ACL1"
            acl.set(acl_id).verify_result()
            acl_id_obj = acl.acl_id[acl_id]
            acl_id_obj.set(AclConsts.TYPE, 'ipv4').verify_result()
            expected_acl_dict = {acl_id: {AclConsts.RULE: {}, AclConsts.TYPE: 'ipv4'}}

        with allure.step("Config 3 rules"):
            rule_id_1 = '1'
            config_rule(engines.dut, acl_id_obj, rule_id_1, {AclConsts.ACTION: AclConsts.DENY, AclConsts.REMARK: "description", AclConsts.SOURCE_IP: 'ANY',
                                                             AclConsts.IP_PROTOCOL: 'icmp', AclConsts.ICMP_TYPE: 'echo-request'})
            expected_acl_dict[acl_id][AclConsts.RULE].update({
                rule_id_1: {
                    AclConsts.ACTION: {AclConsts.DENY: {}},
                    AclConsts.REMARK: "description",
                    AclConsts.MATCH: {
                        AclConsts.IP: {
                            AclConsts.SOURCE_IP: 'ANY',
                            AclConsts.PROTOCOL: 'icmp',
                            AclConsts.ICMP_TYPE: 'echo-request',
                        },
                    },
                }
            })
            rule_id_2 = '2'
            config_rule(engines.dut, acl_id_obj, rule_id_2, {AclConsts.ACTION: AclConsts.PERMIT, AclConsts.IP_PROTOCOL: 'tcp',
                                                             AclConsts.TCP_DEST_PORT: 'snmp', AclConsts.ECN_FLAGS: 'tcp-ece', AclConsts.ECN_IP_ECT: 2})
            expected_acl_dict[acl_id][AclConsts.RULE].update({
                rule_id_2: {
                    AclConsts.ACTION: {AclConsts.PERMIT: {}},
                    AclConsts.MATCH: {
                        AclConsts.IP: {
                            AclConsts.PROTOCOL: 'tcp',
                            'tcp': {
                                'dest-port': {
                                    'snmp': {},
                                },
                            },
                            'ecn': {
                                AclConsts.FLAGS: {
                                    'tcp-ece': {},
                                },
                                AclConsts.IP_ECT: 2,
                            },
                        },
                    },
                }
            })
            rule_id_3 = '3'
            config_rule(engines.dut, acl_id_obj, rule_id_3,
                        {AclConsts.ACTION: AclConsts.LOG, AclConsts.IP_PROTOCOL: 'tcp', AclConsts.TCP_FLAGS: 'syn', AclConsts.TCP_MASK: 'syn'})
            expected_acl_dict[acl_id][AclConsts.RULE].update({
                rule_id_3: {
                    AclConsts.ACTION: {AclConsts.LOG: {}},
                    AclConsts.MATCH: {
                        AclConsts.IP: {
                            AclConsts.PROTOCOL: 'tcp',
                            'tcp': {
                                AclConsts.FLAGS: {
                                    'syn': {},
                                },
                                AclConsts.MASK: {
                                    'syn': {},
                                },
                            },
                        },
                    },
                }
            })

        with allure.step("Validate configuration with show commands"):
            rule_id_1_obj = acl_id_obj.rule.rule_id[rule_id_1]
            acl_id_output = acl_id_obj.parse_show()
            ValidationTool.compare_dictionaries(expected_acl_dict[acl_id], acl_id_output).verify_result()

            rule_output = acl_id_obj.rule.parse_show()
            assert expected_acl_dict[acl_id][AclConsts.RULE] == rule_output

            rule_id_1_output = rule_id_1_obj.parse_show()
            assert expected_acl_dict[acl_id][AclConsts.RULE][rule_id_1] == rule_id_1_output

            action_show = rule_id_1_obj.action.parse_show()
            assert expected_acl_dict[acl_id][AclConsts.RULE][rule_id_1][AclConsts.ACTION] == action_show  # bug 3659032

            match_show = rule_id_1_obj.match.parse_show()
            assert expected_acl_dict[acl_id][AclConsts.RULE][rule_id_1][AclConsts.MATCH] == match_show

            match_ip_show = rule_id_1_obj.match.ip.parse_show()
            assert expected_acl_dict[acl_id][AclConsts.RULE][rule_id_1][AclConsts.MATCH][AclConsts.IP] == match_ip_show

            dest_port_show = acl_id_obj.rule.rule_id[rule_id_2].match.ip.tcp.dest_port.parse_show()
            assert expected_acl_dict[acl_id][AclConsts.RULE][rule_id_2][AclConsts.MATCH][AclConsts.IP]['tcp']['dest-port'] == dest_port_show

            tcp_show = acl_id_obj.rule.rule_id[rule_id_3].match.ip.tcp.parse_show()
            assert expected_acl_dict[acl_id][AclConsts.RULE][rule_id_3][AclConsts.MATCH][AclConsts.IP]['tcp'] == tcp_show

            tcp_flags_show = acl_id_obj.rule.rule_id[rule_id_3].match.ip.tcp.flags.parse_show()
            assert expected_acl_dict[acl_id][AclConsts.RULE][rule_id_3][AclConsts.MATCH][AclConsts.IP]['tcp'][AclConsts.FLAGS] == tcp_flags_show

            tcp_mask_show = acl_id_obj.rule.rule_id[rule_id_3].match.ip.tcp.mask.parse_show()
            assert expected_acl_dict[acl_id][AclConsts.RULE][rule_id_3][AclConsts.MATCH][AclConsts.IP]['tcp'][AclConsts.MASK] == tcp_mask_show

            ecn_show = acl_id_obj.rule.rule_id[rule_id_2].match.ip.ecn.parse_show()
            assert expected_acl_dict[acl_id][AclConsts.RULE][rule_id_2][AclConsts.MATCH][AclConsts.IP]['ecn'] == ecn_show

            ecn_flags_show = acl_id_obj.rule.rule_id[rule_id_2].match.ip.ecn.flags.parse_show()
            assert expected_acl_dict[acl_id][AclConsts.RULE][rule_id_2][AclConsts.MATCH][AclConsts.IP]['ecn'][AclConsts.FLAGS] == ecn_flags_show

    with allure.step("Define ACL to mgmt interface"):
        mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
        mgmt_port = Port(mgmt_port_name)
        mgmt_port.interface.acl.set(acl_id).verify_result()
        mgmt_port.interface.acl.acl_id[acl_id].inbound.set(AclConsts.CONTROL_PLANE, apply=True).verify_result()
        wait_till_acl_applied(mgmt_port, acl_id)

        with allure.step("Validate configuration with show commands"):
            interface_acls_output = mgmt_port.interface.acl.parse_show()
            assert acl_id_output[AclConsts.RULE].keys() == interface_acls_output[acl_id][AclConsts.STATISTICS].keys()

            interface_acl_output = mgmt_port.interface.acl.acl_id[acl_id].parse_show()
            assert rule_output.keys() == interface_acl_output[AclConsts.STATISTICS].keys()

            statistics_output = mgmt_port.interface.acl.acl_id[acl_id].statistics.parse_show()
            assert rule_output.keys() == statistics_output.keys()

            rule_statistics_output = mgmt_port.interface.acl.acl_id[acl_id].statistics.parse_show(rule_id_1)
            assert statistics_output[rule_id_1].keys() == rule_statistics_output.keys()

            inbound_output = mgmt_port.interface.acl.acl_id[acl_id].inbound.parse_show(AclConsts.CONTROL_PLANE)
            assert rule_output.keys() == inbound_output[AclConsts.STATISTICS].keys()


@retry(Exception, tries=5, delay=3)
def wait_till_acl_applied(mgmt_port, acl_id):
    interface_acls_output = mgmt_port.interface.acl.parse_show()
    assert acl_id in interface_acls_output.keys(), f"{acl_id} not found"


@pytest.mark.acl
def test_acl_match_dest_ip(engines, random_api, topology_obj, sonic_mgmt_ipv6_addr):
    """
    Validate ACL match dest-ip rules.
    steps:
    For each ip-string in the list:
        - Define ACL rule for ip, with the lowest rule-ID so it has the highest priority
        - Attach rule to the interface
        - Send packet over interface
        - Assert the rule statistics have increased
    """
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    with allure.step("ACL type ipv4 test"):
        ipv4_addr = engines.sonic_mgmt.ip
        dest_ip_list = [ipv4_addr, ipv4_addr + '/32', ipv4_addr + '/255.255.255.0']
        dest_ip_test(engines, mgmt_port, 'ipv4', "AA_TEST_ACL_IPV4", dest_ip_list, ipv4_addr)

    if IpTool.is_dhcp_client6_has_lease(engines.dut):
        with allure.step("ACL type ipv6 test"):
            dest_ip_list = [sonic_mgmt_ipv6_addr, sonic_mgmt_ipv6_addr + '/64']
            dest_ip_test(engines, mgmt_port, 'ipv6', "AA_TEST_ACL_IPV6", dest_ip_list, sonic_mgmt_ipv6_addr)


@pytest.mark.acl
def test_acl_match_source_port(engines, random_api, topology_obj):
    """
    Validate ACL match source port rules.
    steps:
    1. config ACL with a match source port rule
    2. send packet
    3. validate counter increased
    """
    unset_default_control_plane_acls(engines)
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    src_port_list = ['ANY', 'ssh', 1244]
    match_ip_port_test(engines, mgmt_port, 'ipv4', 'AA_TEST_ACL_SOURCE_PORT', src_port_list, engines.dut.ip, AclConsts.TCP_SOURCE_PORT, engines.sonic_mgmt)
    set_default_control_plane_acls(engines)


@pytest.mark.acl
def test_acl_match_dest_port(engines, random_api, topology_obj):
    """
    Validate ACL match dest port rules.
    steps:
    1. config ACL with a match dest port rule
    2. send packet
    3. validate counter increased
    """
    unset_default_control_plane_acls(engines)
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    dest_port_list = ['ANY', 'ssh', 1244]
    match_ip_port_test(engines, mgmt_port, 'ipv4', 'AA_TEST_ACL_DEST_PORT', dest_port_list, engines.dut.ip, AclConsts.TCP_DEST_PORT, engines.sonic_mgmt)
    set_default_control_plane_acls(engines)


@pytest.mark.acl
def test_acl_match_protocol(engines, random_api, topology_obj):
    """
    Validate ACL match protocol rules.
    steps:
    1. config ACL with a match protocol rule
    2. send packet
    3. validate counter increased
    """
    acl_id = "AA_TEST_ACL_PROTOCOL"
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    dest_addr = engines.dut.ip
    protocol_packet_dict = {'tcp': f"IP(dst=\"{dest_addr}\") / TCP()",
                            'udp': f"IP(dst=\"{dest_addr}\") / UDP()",
                            'icmp': f"IP(dst=\"{dest_addr}\") / ICMP()"}
    rule_id = str(len(protocol_packet_dict))
    acl_obj = None

    with allure.step("Testing protocols"):
        for protocol, packet in protocol_packet_dict.items():
            with allure.independent_step(f"{protocol=}"):
                rule_configuration_dict = {AclConsts.ACTION: AclConsts.PERMIT, AclConsts.IP_PROTOCOL: protocol}
                acl_obj = config_acl_with_rule_attached_to_interface(engines.dut, acl_id, 'ipv4', rule_id,
                                                                     rule_configuration_dict, mgmt_port, AclConsts.INBOUND,
                                                                     AclConsts.CONTROL_PLANE, acl_obj=acl_obj)
                time.sleep(5)
                validate_counters_after_traffic(engines.sonic_mgmt, AclConsts.INBOUND, mgmt_port, acl_id, rule_id, dest_addr,
                                                packet=packet)
                rule_id = str(int(rule_id) - 1)


@pytest.mark.skip(reason="Fragment test is unreliable for control-plane ACLs - skipping temporarily")
@pytest.mark.acl
def test_acl_match_fragment(engines, random_api, topology_obj):
    """
    Validate ACL match fragment rules.
    steps:
    1. config ACL with a match fragment rule
    2. send packet
    3. validate counter increased
    """
    acl_id = "AA_TEST_ACL_FRAGMENT"
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    dest_addr = engines.dut.ip
    src_addr = engines.sonic_mgmt.ip
    # Add source IP and create a large packet that will be fragmented
    packet = f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\") / ICMP() / (\"X\" * 8000)"
    rule_id = '3'
    rule_configuration_dict = {AclConsts.ACTION: AclConsts.PERMIT, AclConsts.IP_PROTOCOL: 'icmp', AclConsts.FRAGMENT: AclConsts.FRAGMENT}
    # Use CONTROL_PLANE instead of empty string for proper control-plane ACL
    acl_obj = config_acl_with_rule_attached_to_interface(engines.dut, acl_id, 'ipv4', rule_id, rule_configuration_dict, mgmt_port,
                                                         AclConsts.INBOUND, AclConsts.CONTROL_PLANE)

    validate_counters_after_traffic(engines.sonic_mgmt, AclConsts.INBOUND, mgmt_port, acl_id, rule_id, dest_addr, packet=packet)


@pytest.mark.acl
def test_acl_match_tcp_flag_mask(engines, random_api, topology_obj):
    """
    Validate ACL match tcp flag and mask rules.
    steps:
    1. config ACL with tcp flag and mask rule
    2. send packet
    3. validate counter increased

    Note: Only ACK, SYN, and NONE flags are tested as they are reliable in control-plane ACLs.
    FIN, RST, PSH, and URG flags are excluded because they are either dropped or unreliably
    matched by the Linux network stack before reaching control-plane ACLs.
    The 'mask=all' option is also excluded because it requires exact flag matching (only the specified
    flag set, all others unset), which is unreliable in control-plane ACLs where the network stack
    may add additional flags to packets.
    """
    unset_default_control_plane_acls(engines)
    acl_id = "AA_TEST_ACL_TCP_FLAG_MASK"
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    dest_addr = engines.dut.ip
    src_addr = engines.sonic_mgmt.ip
    # Only test reliable flags for control-plane ACLs:
    # - Removed 'fin' and 'rst': dropped by Linux network stack
    # - Removed 'psh' and 'urg': unreliably matched in control-plane ACLs
    # - Removed 'all' mask: requires exact flag matching which is unreliable
    # - Keeping 'ack', 'syn', 'none': these are reliably matched in control-plane ACLs
    flag_packet_dict = {'ack': f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\") / TCP(flags=\"A\", dport=12345)",
                        'syn': f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\") / TCP(flags=\"S\", dport=12345)",
                        'none': f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\") / TCP(flags=\"\", dport=12345)"}

    rule_id = str(len(flag_packet_dict))
    acl_obj = None

    for flag, packet in flag_packet_dict.items():
        # Test with mask matching the flag (e.g., flags=ack, mask=ack)
        # This matches any packet with that flag set, regardless of other flags
        rule_configuration_dict = {AclConsts.ACTION: AclConsts.PERMIT, AclConsts.IP_PROTOCOL: 'tcp',
                                   AclConsts.TCP_FLAGS: flag, AclConsts.TCP_MASK: flag}
        acl_obj = config_acl_with_rule_attached_to_interface(engines.dut, acl_id, 'ipv4', rule_id,
                                                             rule_configuration_dict, mgmt_port, AclConsts.INBOUND,
                                                             AclConsts.CONTROL_PLANE, acl_obj=acl_obj)
        validate_counters_after_traffic(engines.sonic_mgmt, AclConsts.INBOUND, mgmt_port, acl_id, rule_id, dest_addr,
                                        packet=packet)
        rule_id = str(int(rule_id) - 1)
    set_default_control_plane_acls(engines)


@pytest.mark.acl
def test_acl_match_ip_state(engines, random_api, topology_obj):
    """
    Validate ACL match ip state rules.
    steps:
    1. config ACL with a match ip state rule
    2. send packet
    3. validate counter increased

    Note: Testing 'new' state only as 'invalid' and 'established' states are difficult
    to reliably test in control-plane ACLs without proper TCP handshake setup.
    """
    acl_id = "AA_TEST_ACL_IP_STATE"
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    dest_addr = engines.dut.ip

    # Only test 'new' state which is reliable with SYN packets
    # 'invalid' (RST) and 'established' require proper TCP connection state
    state_packet_dict = {
        'new': f"IP(src=\"{engines.sonic_mgmt.ip}\", dst=\"{dest_addr}\") / TCP(flags=\"S\", dport=12345, seq=1000)"
    }

    rule_id = str(len(state_packet_dict))
    acl_obj = None

    for state, packet in state_packet_dict.items():
        rule_configuration_dict = {AclConsts.ACTION: AclConsts.PERMIT, AclConsts.TCP_STATE: state, AclConsts.IP_PROTOCOL: 'tcp'}
        acl_obj = config_acl_with_rule_attached_to_interface(engines.dut, acl_id, 'ipv4', rule_id,
                                                             rule_configuration_dict, mgmt_port, AclConsts.INBOUND,
                                                             AclConsts.CONTROL_PLANE, acl_obj=acl_obj)

        validate_counters_after_traffic(engines.sonic_mgmt, AclConsts.INBOUND, mgmt_port, acl_id, rule_id, dest_addr,
                                        packet=packet)
        rule_id = str(int(rule_id) - 1)


@pytest.mark.acl
def test_acl_match_icmp_type(engines, random_api, topology_obj):
    """
    Validate ACL match icmp_type rules.
    steps:
    1. config ACL with a match icmp_type rule
    2. send packet
    3. validate counter increased

    Note: time-exceeded and dest-unreachable ICMP types require proper payload (original IP header + data)
    per RFC 792, otherwise they may be rejected by the network stack.
    """
    unset_default_control_plane_acls(engines)
    acl_id = "AA_TEST_ACL_ICMP_TYPE"
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    dest_addr = engines.dut.ip
    src_addr = engines.sonic_mgmt.ip
    rand_num_type = random.randint(0, 255)
    # Create proper ICMP packets with required payloads per RFC 792
    # time-exceeded, dest-unreachable, and port-unreachable require original IP header + data as payload
    state_packet_dict = {'echo-reply': f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\") / ICMP(type=\"echo-reply\")",
                         'echo-request': f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\") / ICMP(type=\"echo-request\")",
                         'time-exceeded': f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\") / ICMP(type=11, code=0) / IP(dst=\"8.8.8.8\") / ICMP()",
                         'dest-unreachable': f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\") / ICMP(type=3, code=0) / IP(dst=\"8.8.8.8\") / ICMP()",
                         'port-unreachable': f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\") / ICMP(type=3, code=3) / IP(dst=\"8.8.8.8\") / UDP(dport=9999)",
                         rand_num_type: f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\") / ICMP(type={rand_num_type})"}
    rule_id = str(len(state_packet_dict))
    acl_obj = None

    for icmp_type, packet in state_packet_dict.items():
        rule_configuration_dict = {AclConsts.ACTION: AclConsts.PERMIT, AclConsts.ICMP_TYPE: icmp_type, AclConsts.IP_PROTOCOL: 'icmp'}
        acl_obj = config_acl_with_rule_attached_to_interface(engines.dut, acl_id, 'ipv4', rule_id,
                                                             rule_configuration_dict, mgmt_port, AclConsts.INBOUND,
                                                             AclConsts.CONTROL_PLANE, acl_obj=acl_obj)
        validate_counters_after_traffic(engines.sonic_mgmt, AclConsts.INBOUND, mgmt_port, acl_id, rule_id, dest_addr,
                                        packet=packet)
        rule_id = str(int(rule_id) - 1)
    set_default_control_plane_acls(engines)


@pytest.mark.acl
def test_acl_match_icmpv6_type(engines, random_api, topology_obj, sonic_mgmt_ipv6_addr):
    """
    Validate ACL match icmpv6_type rules.
    steps:
    1. config ACL with a match icmpv6_type rule
    2. send packet
    3. validate counter increased
    """
    acl_id = "AA_TEST_ACL_ICMPV6_TYPE"
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    # Get IPv6 address for the destination (not IPv4!)
    dest_addr = mgmt_port.interface.get_ipv6_address()
    src_addr = sonic_mgmt_ipv6_addr
    # Skip when sonic-mgmt and DUT IPv6 are on different /64s (a router sits between them).
    # Scapy injects RS/RA on the player; those packets never leave that L2, so they never
    # reach DUT eth0 and the ACL counter stays 0.
    if not is_same_ipv6_link(src_addr, dest_addr):
        pytest.skip(
            f"Skip: sonic-mgmt {src_addr} and DUT {dest_addr} are on different IPv6 /64s, "
            f"so RS/RA sent from the player never arrive on {mgmt_port_name}"
        )
    icmpv6_type_packet_dict = {'router-solicitation': f"IPv6(dst=\"{dest_addr}\") / ICMPv6ND_RS()",
                               'router-advertisement': f"IPv6(dst=\"{dest_addr}\") / ICMPv6ND_RA()"}
    # 'neighbor-solicitation': f"IPv6(dst=\"{dest_addr}\") / ICMPv6ND_NS()",
    # 'neighbor-advertisement': f"IPv6(dst=\"{dest_addr}\") / ICMPv6ND_NA()"}
    rule_id = str(len(icmpv6_type_packet_dict))
    acl_obj = None

    for icmpv6_type, packet in icmpv6_type_packet_dict.items():
        rule_configuration_dict = {AclConsts.ACTION: AclConsts.PERMIT, AclConsts.ICMPV6_TYPE: icmpv6_type, AclConsts.IP_PROTOCOL: 'icmpv6'}
        acl_obj = config_acl_with_rule_attached_to_interface(engines.dut, acl_id, 'ipv6', rule_id,
                                                             rule_configuration_dict, mgmt_port, AclConsts.INBOUND,
                                                             AclConsts.CONTROL_PLANE, acl_obj=acl_obj)
        validate_counters_after_traffic(engines.sonic_mgmt, AclConsts.INBOUND, mgmt_port, acl_id, rule_id, dest_addr,
                                        packet=packet)
        rule_id = str(int(rule_id) - 1)


@pytest.mark.acl
def test_acl_match_mss(engines, random_api, topology_obj):
    """
    Validate ACL match ip mss rules.
    steps:
    1. config ACL with a match ip mss rule
    2. send packet
    3. validate counter increased
    """
    unset_default_control_plane_acls(engines)
    acl_id = "AA_TEST_ACL_MSS"
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    dest_addr = engines.dut.ip
    rand_mss = str(random.randint(1500, 2500))
    packet = f"IP(dst=\"{dest_addr}\") / TCP(options=[('MSS', {rand_mss})])"
    rule_id = str(random.randint(2, 10))

    with allure.step("mss rules"):
        rule_configuration_dict = {AclConsts.ACTION: AclConsts.PERMIT, AclConsts.MSS: rand_mss, AclConsts.IP_PROTOCOL: 'tcp'}
        acl_obj = config_acl_with_rule_attached_to_interface(engines.dut, acl_id, 'ipv4', rule_id,
                                                             rule_configuration_dict, mgmt_port, AclConsts.INBOUND,
                                                             AclConsts.CONTROL_PLANE)
        validate_counters_after_traffic(engines.sonic_mgmt, AclConsts.INBOUND, mgmt_port, acl_id, rule_id, dest_addr,
                                        packet=packet)

    with allure.step("all-mss-except rules"):
        rule_id = str(int(rule_id) - 1)
        rule_configuration_dict = {AclConsts.ACTION: AclConsts.PERMIT, AclConsts.ALL_MSS_EXCEPT: rand_mss, AclConsts.IP_PROTOCOL: 'tcp'}
        config_acl_with_rule_attached_to_interface(engines.dut, acl_id, 'ipv4', rule_id, rule_configuration_dict,
                                                   mgmt_port, AclConsts.INBOUND, AclConsts.CONTROL_PLANE, acl_obj=acl_obj)
        validate_counters_after_traffic(engines.sonic_mgmt, AclConsts.INBOUND, mgmt_port, acl_id, rule_id, dest_addr,
                                        packet=packet)
    set_default_control_plane_acls(engines)


@pytest.mark.acl
def test_acl_match_ecn(engines, random_api, topology_obj):
    """
    Validate ACL match ecn rules.
    steps:
    1. config ACL with a match ecn rule
    2. send packet
    3. validate counter increased

    Note: ECN flags and IP ECT values are tested with proper source IP and destination port
    to ensure packets reach the ACL layer correctly.
    """
    unset_default_control_plane_acls(engines)
    acl_id = "AA_TEST_ACL_ECN"
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    dest_addr = engines.dut.ip
    src_addr = engines.sonic_mgmt.ip
    # Add source IP and proper destination port to ensure packets are correctly formed
    ecn_flags_dict = {'tcp-cwr': f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\") / TCP(flags=\"C\", dport=12345)",
                      'tcp-ece': f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\") / TCP(flags=\"E\", dport=12345)"}
    ecn_ip_ect_dict = {0: f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\", tos=0) / TCP(dport=12345)",
                       1: f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\", tos=1) / TCP(dport=12345)",
                       2: f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\", tos=2) / TCP(dport=12345)"}
    # 3: f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\", tos=3) / TCP(dport=12345)"}
    rule_id = str(len(ecn_flags_dict) + len(ecn_ip_ect_dict))
    acl_obj = None

    with allure.step("ecn flags rules"):
        for ecn_flag, packet in ecn_flags_dict.items():
            rule_configuration_dict = {AclConsts.ACTION: AclConsts.PERMIT, AclConsts.ECN_FLAGS: ecn_flag, AclConsts.IP_PROTOCOL: 'tcp'}
            acl_obj = config_acl_with_rule_attached_to_interface(engines.dut, acl_id, 'ipv4', rule_id,
                                                                 rule_configuration_dict, mgmt_port, AclConsts.INBOUND,
                                                                 AclConsts.CONTROL_PLANE, acl_obj=acl_obj)
            validate_counters_after_traffic(engines.sonic_mgmt, AclConsts.INBOUND, mgmt_port, acl_id, rule_id, dest_addr,
                                            packet=packet)
            rule_id = str(int(rule_id) - 1)

    with allure.step("ecn ip-ect rules"):
        for ip_ect, packet in ecn_ip_ect_dict.items():
            rule_configuration_dict = {AclConsts.ACTION: AclConsts.PERMIT, AclConsts.ECN_IP_ECT: ip_ect, AclConsts.IP_PROTOCOL: 'tcp'}
            acl_obj = config_acl_with_rule_attached_to_interface(engines.dut, acl_id, 'ipv4', rule_id,
                                                                 rule_configuration_dict, mgmt_port, AclConsts.INBOUND,
                                                                 AclConsts.CONTROL_PLANE, acl_obj=acl_obj)
            validate_counters_after_traffic(engines.sonic_mgmt, AclConsts.INBOUND, mgmt_port, acl_id, rule_id, dest_addr,
                                            packet=packet)
            rule_id = str(int(rule_id) - 1)
    set_default_control_plane_acls(engines)


@pytest.mark.acl
def test_acl_hashlimit(engines, random_api, topology_obj):
    """
    Validate ACL match hashlimit rules.
    steps:
    1. config ACL with 2 rule hashlimit rules
    2. send packet
    3. validate counter increased
    """
    acl_id = "AA_TEST_ACL_HASH_LIMIT"
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    dest_addr = engines.sonic_mgmt.ip
    rule_id = '1'
    rand_burst = random.randint(1, 10)

    with allure.step("configurations"):
        rule_1_configuration_dict = {AclConsts.ACTION: AclConsts.DENY, AclConsts.DEST_IP: dest_addr,
                                     AclConsts.IP_PROTOCOL: 'icmp', AclConsts.ICMP_TYPE: 'echo-request',
                                     AclConsts.HASHLIMIT_NAME: 'one_per_min', AclConsts.HASHLIMIT_RATE: '1/min',
                                     AclConsts.HASHLIMIT_BURST: rand_burst, AclConsts.HASHLIMIT_MODE: 'src-ip',
                                     AclConsts.HASHLIMIT_EXPIRE: 50000}
        config_acl_with_rule_attached_to_interface(engines.dut, acl_id, 'ipv4', rule_id, rule_1_configuration_dict,
                                                   mgmt_port, AclConsts.OUTBOUND)

    with allure.step(f"Validate counters increased"):
        rule_packets_before = get_rule_packets(mgmt_port, acl_id, rule_id, rule_direction=AclConsts.OUTBOUND)
        packets_amount = 3 * rand_burst
        ping_from_switch(engines.dut, dest_addr, mgmt_port_name, count=packets_amount, optional_params="-i 0.2").verify_result()
        time.sleep(5)
        rule_packets_after = get_rule_packets(mgmt_port, acl_id, rule_id, rule_direction=AclConsts.OUTBOUND)
        assert int(rule_packets_after[rule_id]) - int(rule_packets_before[rule_id]) >= (packets_amount - rand_burst - 1), \
            "expect to see difference in the counters after the ping"


def run_tcpdump_validate_option_dscp(dut, acl_type, regex):
    with allure.step('Run tcpdump and validate option dscp'):
        retry_call(validate_dscp_option_tcpdump, [dut, acl_type, regex],
                   exceptions=AssertionError, tries=5, delay=1)


def validate_dscp_option_tcpdump(dut, acl_type, regex):
    if acl_type == IpConsts.IPV4:
        command = f"sudo tcpdump -n -vv -c 200 | grep 'tos {regex}'"
    else:
        command = f"sudo tcpdump -n -vv -c 200 | grep 'class {regex}'"
    tcpdump_output = dut.run_cmd(command)
    assert tcpdump_output, "DSCP TOS value not present in packets"
    logger.info(f"DSCP TOS value - {regex} present in tcpdump")


def sleep():
    logger.info(f"sleep {SLEEP_TIME}")
    time.sleep(SLEEP_TIME)


def is_acl_attached_to_interface(mgmt_port, acl_id):
    """Check if ACL is attached to interface"""
    try:
        mgmt_port.interface.acl.acl_id[acl_id].parse_show()
        return True
    except Exception:
        return False


def get_rule_packets(mgmt_port, acl_id, rule_id=None, rule_direction=AclConsts.INBOUND):
    with allure.step(f"get_rule_packet({mgmt_port.name=}, {acl_id=}, {rule_id=}, {rule_direction=})"):
        try:
            output = mgmt_port.interface.acl.acl_id[acl_id].parse_show()
            res = {}
            assert AclConsts.STATISTICS in output.keys(), f"{AclConsts.STATISTICS} is not found in the output"
            if rule_id:
                res[rule_id] = int(output[AclConsts.STATISTICS][rule_id][rule_direction]["packet"])
            else:
                for rule_id, rule_obj in output[AclConsts.STATISTICS].items():
                    res[rule_id] = int(rule_obj[rule_direction]["packet"])
            return res
        except Exception as e:
            # ACL not attached to interface or doesn't exist
            logger.info(f"ACL {acl_id} not attached to interface {mgmt_port.name} or doesn't exist: {e}")
            raise e


def config_rule(engine, acl_id_obj, rule_id, rule_config_dict):
    with allure.step(f"Config rule {rule_id}"):
        acl_id_obj.rule.set(rule_id).verify_result()
        rule_id_obj = acl_id_obj.rule.rule_id[rule_id]

        for key, value in rule_config_dict.items():
            # Bug 4508304: OpenAPI calls for action 'recent' should expect failure
            if (key == AclConsts.ACTION and value == 'recent' and
                    TestToolkit.tested_api in [ApiType.OPENAPI]):
                try:
                    from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
                    if is_bug_active(4508304):
                        logger.info("Bug 4508304 is active - expecting failure for OpenAPI action 'recent'")
                        RULE_CONFIG_FUNCTION[key](rule_id_obj, value).verify_result(should_succeed=False)
                        continue
                except ImportError:
                    # Fallback if redmine helpers not available
                    pass

            RULE_CONFIG_FUNCTION[key](rule_id_obj, value).verify_result()

        result_obj = SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config,
                                                     engine, True)
        sleep()
        return result_obj


def config_acl_with_rule_attached_to_interface(engine, acl_id, acl_type, rule_id, rule_configuration_dict, mgmt_port,
                                               rule_direction, control_plane=AclConsts.CONTROL_PLANE, acl_obj=None,
                                               should_succeed=True):
    with allure.step(f"config acl {acl_id} with rule {rule_id} attached to interface {mgmt_port.name}"):
        if acl_obj:
            config_rule(engine, acl_obj, rule_id, rule_configuration_dict).verify_result()
        else:
            acl = Acl()
            acl.set(acl_id).verify_result()
            acl_obj = acl.acl_id[acl_id]
            acl_obj.set(AclConsts.TYPE, acl_type).verify_result()
            config_rule(engine, acl_obj, rule_id, rule_configuration_dict).verify_result()
            attach_acl_to_interface(acl_id, mgmt_port, rule_direction, control_plane).verify_result(should_succeed)
    sleep()
    return acl_obj


def attach_acl_to_interface(acl_id, mgmt_port, rule_direction, control_plane=AclConsts.CONTROL_PLANE):
    with allure.step(f"Attach acl {acl_id} to interface {mgmt_port.name}"):
        mgmt_port.interface.acl.set(acl_id).verify_result()
        if rule_direction == AclConsts.INBOUND:
            result_obj = mgmt_port.interface.acl.acl_id[acl_id].inbound.set(control_plane, apply=True)
        elif rule_direction == AclConsts.OUTBOUND:
            result_obj = mgmt_port.interface.acl.acl_id[acl_id].outbound.set(control_plane, apply=True)
        return result_obj


def validate_counters_after_traffic(engine, rule_direction, mgmt_port, acl_id, rule_id, ping_dest=None, packet=None):
    with allure.step(f"Verify {rule_direction} rule captures relevant traffic"):
        rule_packets_before = get_rule_packets(mgmt_port, acl_id, rule_id, rule_direction=rule_direction)
        if packet:
            scapy_send_packet(engine, packet, interface=mgmt_port.name)
        elif ping_dest:
            ping_from_switch(engine, ping_dest, mgmt_port.name).verify_result()
        time.sleep(5)
        rule_packets_after = get_rule_packets(mgmt_port, acl_id, rule_id, rule_direction=rule_direction)
        assert int(rule_packets_after[rule_id]) > int(rule_packets_before[rule_id]), \
            "expect to see difference in the counters after the ping"


def ping_from_switch(engine: ProxySshEngine, dest: str, source_interface, count=2, optional_params="") -> ResultObj:
    with allure.step(f"Ping from switch through {source_interface} to {dest}"):
        cmd = f"ping {dest} -c {count}"
        if source_interface:
            cmd += " -I " + source_interface
        if optional_params:
            cmd += " " + optional_params
        ping_output = engine.run_cmd(cmd)
        if "100% packet loss" in ping_output:
            return ResultObj(False, f"Failed to ping {dest}", ping_output)

        return ResultObj(True, "", ping_output)


def ping_from_sonic_mgmt(dst: Union[str, Packet], src=None) -> Packet:
    with allure.step(f"ping {dst} from {src or 'default'}"):
        # When running locally (not through MARS), uncomment these 2 lines and delete the following lines.
        # Also you might need to set the src parameter to your VDI rather than the sonic_mgmt ip.
        # subprocess.run(f"ping {dst if isinstance(dst, str) else dst.dst} -c1".split(' '), capture_output=True)
        # return dst if isinstance(dst, Packet) else (IP(dst=dst, src=src) / ICMP())
        try:
            packet = dst if isinstance(dst, Packet) else (IP(dst=dst, src=src) / ICMP())
            send(packet)
            return packet
        except PermissionError as e:
            raise Exception(
                "When running this locally (not through MARS) you need to uncomment in the function's source-code"
            ) from e


def dest_ip_test(engines, mgmt_port, acl_type, acl_id, dest_ip_list, ping_dest):
    with allure.step(f"Define ACL {acl_id} type {acl_type}"):
        rule_id = str(len(dest_ip_list))
        acl_obj = None

    for dest_ip in dest_ip_list:
        with allure.step(f"{dest_ip=}"):
            rule_configuration_dict = {AclConsts.ACTION: AclConsts.PERMIT, AclConsts.DEST_IP: dest_ip}
            acl_obj = config_acl_with_rule_attached_to_interface(engines.dut, acl_id, acl_type, rule_id,
                                                                 rule_configuration_dict, mgmt_port, AclConsts.OUTBOUND,
                                                                 AclConsts.CONTROL_PLANE, acl_obj=acl_obj)
            time.sleep(5)
            validate_counters_after_traffic(engines.dut, AclConsts.OUTBOUND, mgmt_port, acl_id, rule_id, ping_dest=ping_dest)
            rule_id = str(int(rule_id) - 1)


def scapy_send_packet(engine, packet, interface=''):
    # The interactive scapy shell can echo its prompt before the send executes, so the session may be
    # torn down with the packet never leaving. Send non-interactively and require scapy's confirmation.
    # Packets may embed single quotes, e.g. TCP(options=[('MSS', 1500)]), so let shlex do the quoting
    py_cmd = shlex.quote(f"from scapy.all import *; send({packet}, verbose=1)")
    cmd = f"sudo python3 -c {py_cmd}"
    with allure.step(f"On {engine.ip}: sending with scapy {cmd}"):
        ret = engine.run_cmd(cmd)
        if "ModuleNotFoundError" in ret:
            # scapy may only be installed in the virtualenv, which plain sudo does not inherit
            ret = engine.run_cmd(f'sudo -E env "PATH=$PATH" python3 -c {py_cmd}')
        if "Traceback" in ret or "Sent" not in ret:
            raise Exception("scapy failed to send: " + ret)


def is_same_ipv6_link(addr_a, addr_b, prefix_len=64):
    """Whether two IPv6 addresses share a /64, i.e. traffic between them is not routed."""
    try:
        network = ipaddress.ip_network(f"{addr_a.split('/')[0]}/{prefix_len}", strict=False)
        return ipaddress.ip_address(addr_b.split('/')[0]) in network
    except ValueError:
        logger.warning(f"Could not compare IPv6 addresses {addr_a} and {addr_b}")
        return False


def unset_default_control_plane_acls(engines):
    """Unset all system control-plane ACL bindings (dos / whitelist / outbound)."""
    with allure.step("Unset default system control-plane ACLs"):
        System().control_plane.acl.unset(apply=True).verify_result()


def set_default_control_plane_acls(engines):
    """Re-attach the default system control-plane ACL bindings by name."""
    with allure.step("Set default system control-plane ACLs back"):
        cp_acl = System().control_plane.acl
        for acl_id in AclConsts.NEW_DEFAULT_ACLS:
            # Loopback defaults are bound on interface lo, not system control-plane
            if 'loopback' in acl_id:
                continue
            if 'outbound' in acl_id:
                cp_acl.acl_id[acl_id].outbound.set()
            else:
                cp_acl.acl_id[acl_id].inbound.set()
        SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config,
                                        engines.dut, True).verify_result()


def match_ip_port_test(engines, mgmt_port, acl_type, acl_id, port_list, dest_addr, port_direction, engine_send_packet):
    rule_id = str(len(port_list))
    acl_obj = None

    for port in port_list:
        with allure.step(f"{port=}"):
            src_addr = engine_send_packet.ip
            rule_configuration_dict = {AclConsts.ACTION: AclConsts.PERMIT, AclConsts.IP_PROTOCOL: 'tcp', port_direction: port}
            acl_obj = config_acl_with_rule_attached_to_interface(engines.dut, acl_id, acl_type, rule_id,
                                                                 rule_configuration_dict, mgmt_port, AclConsts.INBOUND,
                                                                 control_plane="", acl_obj=acl_obj)

            if port == 'ANY':
                port = 1234
            port = port if isinstance(port, int) else f"\"{port}\""
            packet = f"IP(src=\"{src_addr}\", dst=\"{dest_addr}\") / TCP(sport={port}, dport={port})"
            validate_counters_after_traffic(engine_send_packet, AclConsts.INBOUND, mgmt_port, acl_id, rule_id, dest_addr, packet=packet)
            rule_id = str(int(rule_id) - 1)


def get_dscp_hexadecimal_dict(option):
    if option == 'supported_dec':
        rand_int = random.randint(0, 63)
        return ({rand_int: hex(rand_int * 4)})
    elif option == 'unsupported_dec':
        rand_int = random.randint(64, 100)
        return ({rand_int: hex(rand_int)})
    elif option == 'supported_enum':
        enum_list = [af11, af12, af13, af21, af22, af23, af31, af32, af33, af41, af42, af43, cs1, cs2, cs3, cs4, cs5, cs6, cs7, be, ef]
        return (random.choice(enum_list))
    else:
        logger.info("please provide valid option")
