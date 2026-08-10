import pytest
import logging
import scapy
import re
import random
from tests.drop_packets.drop_packets import *
from ptf.testutils import verify_no_packet_any, simple_ip_only_packet, simple_ipv4ip_packet, simple_tcp_packet, simple_vxlan_packet
from tests.common.helpers.assertions import pytest_assert
from collections import OrderedDict
from tests.common.platform.device_utils import fanout_switch_port_lookup
from tests.common.platform.processes_utils import wait_critical_processes
from tests.common.utilities import run_until, wait, wait_until
from tests.vxlan.test_vxlan_decap import prepare_ptf, generate_vxlan_config_files
import json
from jinja2 import Template
from netaddr import IPAddress
from tests.vxlan.vnet_constants import DUT_VXLAN_PORT_JSON
from tests.vxlan.vnet_utils import render_template_to_host
import time

SUCCESS_CODE = 0
SHOW_L1_AGGREGATE = "show what-just-happened poll layer-1 --aggregate"
VTEP2_IP = "8.8.8.8"
VNI_BASE = 336
logger = logging.getLogger(__name__)
pytest.CHANNEL_CONF = None

protocols = {
    '0x6'   : 'tcp',
    '0x11'  : 'udp',
    '0x2'   : 'igmp',
    '0x4'   : 'ipencap',
    '0x1'   : 'icmp'
}


def parse_table(table_lines):
    entries = []
    headers = []
    header_lines_num = 2

    # check separators index
    for sep_index in range(len(table_lines)):
        if table_lines[sep_index][0] == '-':
            break

    separators = re.split(r'\s{2,}', table_lines[sep_index])[0].split()  # separators between headers and content
    headers_line = table_lines[0]
    start = 0
    # separate headers by table separators
    for sep in separators:
        curr_len = len(sep)
        headers.append(headers_line[start:start+curr_len].strip())
        start += curr_len + 1
    # check if headers appears in next line as well (only for Drop Group header - raw)
    if table_lines[1].strip() == "Group":
        headers[11] = headers[11] + " Group"
        header_lines_num = 3
    output_lines = table_lines[header_lines_num:]  # Skip the header lines in output

    for line in output_lines:
        # if the previous line was too long and has splitted to 2 lines
        if line[0] == " ":
            start_index = len(line) - len(line.lstrip()) + 1
            sep_len = 0
            for j in range(len(separators)):
                sep = separators[j]
                sep_len += len(sep)
                if start_index <= sep_len:
                    break
            # j is the index in the entry
            start_index -= 1
            if (entries[-1][headers[j]].endswith(']') or entries[-1][headers[j]].endswith(':')):
                space = ''
                first_space_index = line[start_index:].index(' ')
                entries[-1][headers[j]] = entries[-1][headers[j]] + space + line[start_index:start_index + first_space_index].strip()
            else:
                space = ' '
                entries[-1][headers[j]] = entries[-1][headers[j]] + space + line.strip()
            continue

        entry = {}
        data = []
        start = 0
        for sep in separators:
            curr_len = len(sep)
            data.append(line[start:start + curr_len].strip())
            start += curr_len + 1

        for i in range(len(data)):
            entry[headers[i]] = data[i]
        entries.append(entry)
    return entries


def parse_wjh_table(table):
    table_lines = table.splitlines()
    if not table_lines:
        return [table_lines]
    if "" in table_lines:
        second_table_index = table_lines.index("")
        first_table = table_lines[:second_table_index]
        second_table = table_lines[second_table_index + 2:]
        second_entries = parse_table(second_table)
    else:
        first_table = table_lines
        second_entries = []

    first_entries = parse_table(first_table)
    entries = [first_entries, second_entries]
    return entries

def check_for_daemon_error(stderr_lines):
    if not stderr_lines:
        return

    for err in stderr_lines:
        if ('failed to connect to daemon' in err or 'Timeout waiting for daemon to send data' in err):
            pytest.fail("{} error has appeared.\n\
            Please check if SDK version in WJH and Syncd containers is the same.".format(err))


def get_raw_tables_output(duthost, command="show what-just-happened"):
    stdout = duthost.command(command)
    check_for_daemon_error(stdout['stderr_lines'])
    if stdout['rc'] != SUCCESS_CODE:
        raise Exception(stdout['stdout'] + stdout['stderr'])
    table_output = parse_wjh_table(stdout['stdout'])
    return table_output


def get_agg_tables_output(duthost, command="show what-just-happened poll --aggregate"):
    stdout = duthost.command(command)
    check_for_daemon_error(stdout['stderr_lines'])
    if stdout['rc'] != SUCCESS_CODE:
        raise Exception(stdout['stdout'] + stdout['stderr'])
    splitted_table = stdout['stdout'].splitlines()[3:]
    table = "\n".join(splitted_table)
    table_output = parse_wjh_table(table)
    return table_output


def norm(pkt_mac):
    return pkt_mac.decode() if isinstance(pkt_mac, bytes) else pkt_mac


def check_if_entry_exists(table, pkt):
    entries = []
    entry_found = False
    ip_key = 'IP'
    proto_key = 'proto'
    if ip_key not in pkt:
        ip_key = 'IPv6'
        proto_key = 'nh'
    for entry in table:
        src_ip_port = entry['Src IP:Port'].rsplit(':', 1)
        dst_ip_port = entry['Dst IP:Port'].rsplit(':', 1)
        if (norm(pkt.dst).lower() == entry['dMAC'].lower() and
            norm(pkt.src).lower() == entry['sMAC'].lower()):

                if src_ip_port[0] != 'N/A':
                    if ip_key in pkt:
                        if isinstance(pkt[ip_key].dst, scapy.base_classes.Net):
                            pkt[ip_key].dst = '0.0.0.0'
                        if (pkt[ip_key].src.lower() != src_ip_port[0].replace('[', '').replace(']', '').lower() or
                            pkt[ip_key].dst.lower() != dst_ip_port[0].replace('[', '').replace(']', '').lower()):
                                continue
                        if proto_key == 'proto':
                            if (protocols[hex(pkt[ip_key].proto)] == entry['IP Proto']):
                                entries.append(entry)
                                break
                        else:
                            if (protocols[hex(pkt[ip_key].nh)] == entry['IP Proto']):
                                entries.append(entry)
                                break

                    if ('TCP' in pkt and len(src_ip_port) > 1 and len(dst_ip_port) > 1):
                        if (str(pkt['TCP'].sport) != src_ip_port[1] or
                            str(pkt['TCP'].dport) != dst_ip_port[1]):
                                continue

                entries.append(entry)

    return entries


def verify_drop_on_wjh_rule_table(pkt_entry, rules_table, drop_information):
    INGRESS_RULE_PATTERN = r'SIP: 20.0.0.0/255.255.255.0.*ETHERTYPE: 0x800/0xffff.*COUNTER_ID ='
    EGRESS_RULE_PATTERN = r'DIP: 192.168.144.0/255.255.255.0.*ETHERTYPE: 0x800/0xffff.*COUNTER_ID ='

    if drop_information == "OUTDATAACL":
        rule_pattern = EGRESS_RULE_PATTERN
    else:
        rule_pattern = INGRESS_RULE_PATTERN

    for rule_entry in rules_table:
        if re.match(rule_pattern, rule_entry['Rule']):
            if rule_entry['#'] == pkt_entry['#']:
                return True
    return False


def verify_drop_on_wjh_raw_table(duthost, pkt, discard_group, drop_information=None):
    logger.info(f"The discard group is {discard_group}, the drop information is {drop_information}")
    if discard_group in ["ACL"]:
        tables = get_raw_tables_output(duthost, command="show what-just-happened poll {}".format(discard_group.lower()))
    else:
        tables = get_raw_tables_output(duthost)
    entries = check_if_entry_exists(tables[0], pkt)

    for entry in entries:
        if discard_group == entry['Drop Group']:
            if discard_group == 'ACL':
                return verify_drop_on_wjh_rule_table(entry, tables[1], drop_information)
            return True
        else:
            logger.info(f"The discard group is {discard_group}, the drop information is {drop_information}, the entry "
                        f"is {entry}")
    return False


def check_wjh_drop_counts_in_aggregate_table(duthost, discard_group, pkt, num_packets, entry_totals, acl_on_rule_table, drop_information):
    if discard_group in ["ACL"]:
        tables = get_agg_tables_output(duthost, command="show what-just-happened poll {} --aggregate".format(
            discard_group.lower()))
    else:
        tables = get_agg_tables_output(duthost)

    for entry in check_if_entry_exists(tables[0], pkt):
        key = (entry.get('sPort', ''), entry.get('Drop reason - Recommended action', ''))
        sum_of_previous_drop_counts = entry_totals.get(key, 0)
        current_drop_count = int(entry.get('Count', 0))
        entry_totals[key] = sum_of_previous_drop_counts + current_drop_count

        if entry_totals[key] == num_packets:
            logger.info(f"WJH verified: {key[0]} cumulative={entry_totals[key]}, expected={num_packets}")
            if discard_group == 'ACL':
                  acl_on_rule_table['value'] = verify_drop_on_wjh_rule_table(entry, tables[1], drop_information)
            return  True

    if entry_totals:
        for key, total in entry_totals.items():
            logger.info(f"WJH count not match: {key[0]} cumulative={total}, expected={num_packets}")
    return False


def verify_drop_on_agg_wjh_table(duthost, pkt, num_packets, discard_group, drop_information=None,
                                 timeout=120, interval=1):
    """
    Verify packet drops on WJH aggregate table with cumulative counting.
    WJH rate-limits burst sampling, so we poll multiple times and sum counts per entry.
    """
    logger.info(f"Verifying WJH agg: discard_group={discard_group}, expected={num_packets}")
    entry_totals = {}
    acl_on_rule_table = {'value': None}

    wjh_drop_count_result = wait_until(timeout, interval, 0, check_wjh_drop_counts_in_aggregate_table, duthost, discard_group,
                  pkt, num_packets, entry_totals, acl_on_rule_table, drop_information)

    return acl_on_rule_table['value'] if acl_on_rule_table['value'] else wjh_drop_count_result

def do_raw_test(discard_group, pkt, ptfadapter, duthost, ports_info, sniff_ports, tx_dut_ports=None,
                comparable_pkt=None, skip_counter_check=False, drop_information=None, ip_ver='ipv4'):
    # send packet
    send_packets(pkt, ptfadapter, ports_info["ptf_tx_port_id"])
    # verify packet is dropped
    exp_pkt = expected_packet_mask(pkt, ip_ver=ip_ver)
    verify_no_packet_any(ptfadapter, exp_pkt, ports=sniff_ports)

    # Some test cases will not increase the drop counter consistently on certain platforms
    if skip_counter_check:
        logger.info("Skipping counter check")
        return None

    # verify wjh table
    if comparable_pkt:
        pkt = comparable_pkt
    if not verify_drop_on_wjh_raw_table(duthost, pkt, discard_group, drop_information):
        pytest.fail("Could not find drop on WJH table. packet: {}".format(pkt.command()))


def do_agg_test(discard_group, pkt, ptfadapter, duthost, ports_info, sniff_ports, tx_dut_ports=None, comparable_pkt=None,
                skip_counter_check=False, drop_information=None, ip_ver='ipv4'):
    num_packets = random.randint(2,100)
    # start: debug info , will remove it after debug
    logger.info(f"num_packets: {num_packets}, ports_info: {ports_info}")
    duthost.shell("sudo portstat -c")
    logger.info(f"Check portstat before sending packets")
    duthost.shell(f"sudo portstat -i {ports_info['dut_iface']}")
    duthost.shell(f"sudo portstat ")
    # end: debug info , will remove it after debug

    send_packets(pkt, ptfadapter, ports_info["ptf_tx_port_id"], num_packets=num_packets)

    # verify packet is dropped
    exp_pkt = expected_packet_mask(pkt, ip_ver=ip_ver)
    verify_no_packet_any(ptfadapter, exp_pkt, ports=sniff_ports, timeout=0.3)

    time.sleep(2)

    # start: debug info , will remove it after debug
    logger.info(f"Check portstat after sending packets")
    duthost.shell(f"sudo portstat -i {ports_info['dut_iface']}")
    duthost.shell(f"sudo portstat ")
    # end: debug info , will remove it after debug

    # Some test cases will not increase the drop counter consistently on certain platforms
    if skip_counter_check:
        logger.info("Skipping counter check")
        return None

    # verify wjh table
    if comparable_pkt:
        pkt = comparable_pkt
    if not verify_drop_on_agg_wjh_table(duthost, pkt, num_packets, discard_group, drop_information):
        # start: debug info , will remove it after debug
        logger.info(f"Check portstat after failed")
        duthost.shell(f"sudo portstat -i {ports_info['dut_iface']}")
        duthost.shell(f"sudo portstat ")
        # end: debug info , will remove it after debug
        pytest.fail("Could not find drop on aggregation WJH table. packet: {}".format(pkt.command()))


@pytest.fixture(scope='module')
def do_test(duthosts, rand_one_dut_hostname):
    duthost = duthosts[rand_one_dut_hostname]

    def do_wjh_test(discard_group, pkt, ptfadapter, ports_info, sniff_ports, tx_dut_ports=None, comparable_pkt=None,
                    skip_counter_check=False, drop_information=None, ip_ver='ipv4'):
        try:
            if (pytest.CHANNEL_CONF['forwarding']['type'].find('raw') != -1):
                do_raw_test(discard_group, pkt, ptfadapter, duthost, ports_info, sniff_ports, tx_dut_ports,
                            comparable_pkt, skip_counter_check=skip_counter_check,
                            drop_information=drop_information, ip_ver=ip_ver)
        finally:
            if (pytest.CHANNEL_CONF['forwarding']['type'].find('aggregate') != -1):
                # a temporary check. there is a problem with IP header absent in aggregate
                # TODO: remove this check when issue is fixed
                if 'IP' in pkt or 'IPv6' in pkt:
                    do_agg_test(discard_group, pkt, ptfadapter, duthost, ports_info, sniff_ports, tx_dut_ports,
                                comparable_pkt, skip_counter_check=skip_counter_check,
                                drop_information=drop_information, ip_ver=ip_ver)

    return do_wjh_test


def verify_l1_raw_drop_exists(table, port):
    entry_exists = False
    for entry in table:
        if (entry['Drop Group'] == 'L1' and
            entry['sPort'] == port and
            entry['Severity'] == 'Warn' and
            entry['Drop reason - Recommended action'] == 'Generic L1 event - Check layer 1 aggregated information'):
                entry_exists = True
                break

    return entry_exists


@pytest.fixture(scope='module', autouse=True)
def check_global_configuration(duthost):
    config_facts  = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
    wjh_global = config_facts.get('WJH', {})

    try:
        wjh_global = wjh_global['global']
        if wjh_global['mode'] != 'debug':
            pytest.skip("Debug mode is not enabled. Skipping test.")
    except Exception as e:
        pytest.fail("Could not fetch global configuration information.")


@pytest.fixture(scope='module', autouse=True)
def get_channel_configuration(duthost):
    config_facts  = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
    pytest.CHANNEL_CONF = config_facts.get('WJH_CHANNEL', {})


@pytest.fixture(scope='module', autouse=True)
def check_feature_enabled(duthost):
    features = duthost.feature_facts()['ansible_facts']['feature_facts']
    if 'what-just-happened' not in features or features['what-just-happened'] != 'enabled':
        pytest.skip("what-just-happened feature is not available. Skipping the test.")


@pytest.fixture(scope='module')
def vxlan_config(duthosts, rand_one_dut_hostname, ptfhost, tbinfo):
    duthost = duthosts[rand_one_dut_hostname]

    logger.info("Gather minigraph facts")
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)

    logger.info("Copying vxlan_switch.json")
    render_template_to_host("vxlan_switch.j2", duthost, DUT_VXLAN_PORT_JSON)
    duthost.shell("docker cp {} swss:/vxlan.switch.json".format(DUT_VXLAN_PORT_JSON))
    duthost.shell("docker exec swss sh -c \"swssconfig /vxlan.switch.json\"")
    time.sleep(3)

    logger.info("Prepare PTF")
    prepare_ptf(ptfhost, mg_facts, duthost)

    logger.info("Generate VxLAN config files")
    generate_vxlan_config_files(duthost, mg_facts)

    setup_info = {
        "mg_facts": mg_facts
    }

    yield setup_info

    logger.info("Stop arp_responder on PTF")
    ptfhost.shell("supervisorctl stop arp_responder")

    logger.info("Always try to remove any possible VxLAN tunnel and map configuration")
    for vlan in mg_facts["minigraph_vlans"]:
            duthost.shell('docker exec -i database redis-cli -n 4 -c DEL "VXLAN_TUNNEL_MAP|tunnelVxlan|map%s"' % vlan)
    duthost.shell('docker exec -i database redis-cli -n 4 -c DEL "VXLAN_TUNNEL|tunnelVxlan"')


@pytest.fixture(scope='module')
def vxlan_status(vxlan_config, duthosts, rand_one_dut_hostname):
    duthost = duthosts[rand_one_dut_hostname]
    #clear FDB and arp cache on DUT
    duthost.shell('sonic-clear arp; fdbclear')
    duthost.shell("sonic-cfggen -j /tmp/vxlan_db.tunnel.json --write-to-db")
    duthost.shell("sonic-cfggen -j /tmp/vxlan_db.maps.json --write-to-db")


# NOTE: this test case is available only for setups with SDK ver 4.4.2522 and above
def test_fid_miss(request, do_test, ptfadapter, duthost, setup, pkt_fields, ports_info, tbinfo):
    if tbinfo['topo']['type'] != 't0':
        pytest.skip("fid_miss test case is only available for t0 topology")
    request.getfixturevalue('vxlan_config')
    request.getfixturevalue('vxlan_status')
    router_mac = ports_info['dst_mac']
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    switch_loopback_ip = mg_facts['minigraph_lo_interfaces'][0]['addr']

    inner_packet = simple_tcp_packet(
                        eth_dst=ports_info['src_mac'],
                        eth_src=router_mac,
                        ip_dst=pkt_fields['ipv4_dst'],
                        ip_src=pkt_fields['ipv4_src']
                        )

    packet = simple_vxlan_packet(
                        eth_dst=router_mac,
                        eth_src=ports_info['src_mac'],
                        ip_src=VTEP2_IP,
                        ip_dst=switch_loopback_ip,
                        vxlan_vni=6, # vni in mapping will be 1336 so we will receive the drop
                        inner_frame=inner_packet
                        )
    do_test('L2', packet, ptfadapter, ports_info, setup['neighbor_sniff_ports'])


def test_tunnel_ip_in_ip(do_test, ptfadapter, duthost, setup, pkt_fields, ports_info):
    dst_ip = pkt_fields['ipv4_dst']

    # gather facts
    dscp_range = list(range(0, 33))
    ttl_range = list(range(2, 65))
    router_mac = ports_info['dst_mac']
    src_mac = ports_info['src_mac']
    dscp_in_idx = 0
    dscp_out_idx = len(dscp_range) // 2
    ttl_in_idx = 0
    ttl_out_idx = len(ttl_range) // 2

    dscp_in = dscp_range[dscp_in_idx]
    tos_in = dscp_in << 2
    dscp_out = dscp_range[dscp_out_idx]
    tos_out = dscp_out << 2

    ecn_in = 0
    ecn_out = 2
    ttl_in = ttl_range[ttl_in_idx]
    ttl_in |= ecn_in
    ttl_out = ttl_range[ttl_out_idx]
    ttl_out |= ecn_out

    inner_src_ip = '1.1.1.1'

    inner_packet = simple_ip_only_packet(
                ip_dst=dst_ip,
                ip_src=inner_src_ip,
                ip_ttl=ttl_in,
                ip_tos=tos_in
    )

    pkt = simple_ipv4ip_packet(
        eth_dst=router_mac,
        eth_src=src_mac,
        ip_src='0.0.0.0',
        ip_dst=dst_ip,
        ip_tos=tos_out,
        ip_ttl=ttl_out,
        inner_frame=inner_packet
    )

    do_test("L3", pkt, ptfadapter, ports_info, setup['neighbor_sniff_ports'])


def check_if_l1_enabled(type):
    if "layer-1" not in pytest.CHANNEL_CONF:
        pytest.skip("layer-1 channel is not confiugred on WJH.")
    if pytest.CHANNEL_CONF['layer-1']['type'].find(type) == -1:
        pytest.skip("layer-1 {} channel type is not confiugred on WJH.".format(type))


def get_active_port(duthost):
    intf_facts = duthost.interface_facts()['ansible_facts']['ansible_interface_facts']

    for port in intf_facts.keys():
        if intf_facts[port]['active'] and port.startswith('Ethernet'):
            return port

    pytest.skip("Could not find port in active state. Skipping the test.")


def get_inactive_phy_port(duthost, fanouthosts):
    intf_facts = duthost.interface_facts()['ansible_facts']['ansible_interface_facts']

    for port in sorted(intf_facts.keys()):
        if not port.startswith('Ethernet') or intf_facts[port]['active']:
            continue

        fanout, fanout_port = fanout_switch_port_lookup(fanouthosts, duthost.hostname, port)
        if not fanout or not fanout_port:
            continue

        return port

    pytest.skip("Could not find port in Down state. Skipping the test.")


def check_if_port_is_active(duthost, port):
    intf_facts = duthost.interface_facts()['ansible_facts']['ansible_interface_facts']
    if intf_facts[port]['active']:
        return True
    return False


def test_l1_raw_drop(duthost):
    check_if_l1_enabled('raw')

    port = get_active_port(duthost)
    # shut down one of the ports
    duthost.command("config interface shutdown {}".format(port))

    try:
        table = get_raw_tables_output(duthost, "show what-just-happened poll layer-1")[0]
        if not wait_until(6, 1, 0, verify_l1_raw_drop_exists, table, port):
            pytest.fail("Could not find L1 drop on WJH table.")
    finally:
        duthost.command("config interface startup {}".format(port))
        if not wait_until(60, 5, 0, check_if_port_is_active, duthost, port):
            pytest.fail("Could not start up {} port.\nAborting.".format(port))


def verify_l1_agg_drop_exists(duthost, cmd, port, state):
    entry_exists = False
    table = get_agg_tables_output(duthost, command=cmd)[0]
    for entry in table:
        if (entry['State'] == state and
            entry['Port'] == port and
            int(entry['State Change']) > 0):
            entry_exists = True
            break

    return entry if entry_exists else entry_exists

def test_l1_agg_port_up(duthost, fanouthosts):
    check_if_l1_enabled('aggregate')

    port = get_inactive_phy_port(duthost, fanouthosts)

    duthost.command("config interface startup {}".format(port))
    try:
        if not wait_until(120, 5, 0, check_if_port_is_active, duthost, port):
            pytest.fail("Could not start up {} port.\nAborting.".format(port))
        entry = run_until(2, 1, 5, ('Port', port), verify_l1_agg_drop_exists, duthost, SHOW_L1_AGGREGATE, port, 'Up')
        if entry['Down Reason - Recommended Action'] != 'N/A':
            pytest.fail("Could not find L1 drop on WJH aggregated table.")
    finally:
        duthost.command("config interface shutdown {}".format(port))


def test_l1_agg_port_down(duthost):
    check_if_l1_enabled('aggregate')
    port = get_active_port(duthost)

    duthost.command("config interface shutdown {}".format(port))
    try:
        entry = run_until(2, 1, 5, ('Port', port), verify_l1_agg_drop_exists, duthost, SHOW_L1_AGGREGATE, port, "Down")
        if (entry['Down Reason - Recommended Action'] != 'Port admin down - Validate port configuration' and
            entry['Down Reason - Recommended Action'] != 'N/A'):
            pytest.fail("Could not find L1 drop on WJH aggregated table.")
    finally:
        duthost.command("config interface startup {}".format(port))
        if not wait_until(60, 5, 0, check_if_port_is_active, duthost, port):
            pytest.fail("Could not start up {} port.\nAborting.".format(port))


def test_l1_agg_fanout_port_down(duthost, fanouthosts):
    check_if_l1_enabled('aggregate')
    port = get_active_port(duthost)

    fanout, fanout_port = fanout_switch_port_lookup(fanouthosts, duthost.hostname, port)
    fanout.shutdown(fanout_port)
    wait(15, 'Wait for fanout port to shutdown')
    try:
        entry = verify_l1_agg_drop_exists(duthost, SHOW_L1_AGGREGATE, port, 'Down')
        if (entry['Down Reason - Recommended Action'] != 'Other reason -' and
            entry['Down Reason - Recommended Action'] != 'Autoneg - No partner detected' and
            entry['Down Reason - Recommended Action'] != 'Autoneg - No partner detected during force mode' and
            entry['Down Reason - Recommended Action'] != 'Auto-negotiation failure - Set port speed manually, disable auto-negotiation'):
            pytest.fail("Could not find L1 drop on WJH aggregated table.")
    finally:
        fanout.no_shutdown(fanout_port)


def test_wjh_starts_after_config_reload(duthost):
    stdout = duthost.command('config reload -y', module_ignore_errors=True)
    if stdout['rc'] != SUCCESS_CODE:
        raise Exception(stdout['stdout'] + stdout['stderr'])
    wait_critical_processes(duthost)
    stdout = duthost.shell('docker ps | grep "what-just-happened"', module_ignore_errors=True)
    if stdout['rc'] != SUCCESS_CODE:
        raise Exception(stdout['stdout'] + stdout['stderr'])
    stdout = stdout['stdout']
    # if what-just-happened container is not up - fail
    if stdout == "":
        pytest.fail("what-just-happened container did not start up after config reload\nAborting!")
