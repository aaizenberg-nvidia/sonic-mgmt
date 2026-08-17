import time
import pytest
import allure
import logging
from devts.infra.tools.validations.traffic_validations.iperf.iperf_runner import IperfChecker
from devts.infra.tools.validations.traffic_validations.scapy.scapy_runner import ScapyChecker
from retry.api import retry_call
from ngts.config_templates.wjh_buffer_config_template import WjhBufferConfigTemplate
from ngts.cli_util.cli_parsers import generic_sonic_output_parser
from devts.infra.tools.validations.traffic_validations.ping.ping_runner import PingChecker
from ngts.common.checkers import is_feature_ready
from ngts.constants.constants import SonicConst, WJHConsts
from ngts.tests.push_build_tests.general.wjh import utils
from ngts.tests.push_build_tests.general.wjh.utils import (wjh_is_channel_enabled, wjh_config_channel_state,
                                                           get_buffer_profile_trimming_status, configure_trimming_action,
                                                           discover_trimming_enabled_profiles)
# Import from tests/common directory
from scapy.all import Ether, Dot1Q, IP, IPv6, Raw, TCP
from ngts.tests.push_build_tests.general.conftest import check_qos_counter_status
from tests.common.mellanox_data import LOSSY_ONLY_HWSKUS
from tests.common.utilities import wait_until
pytest.CHANNEL_CONF = None
logger = logging.getLogger()

drop_reason_dict = {"tail_drop": "Tail drop - Monitor network congestion",
                    "buffer_congestion": "Port TC Congestion Threshold Crossed - Monitor network congestion",
                    "buffer_latency": "Packet Latency Threshold Crossed - Monitor network congestion"}
l2_drop_reason_dict = {"multicast_src_mac": "Source MAC is multicast - Bad packet was received from peer",
                       "src_mac_equals_dst_mac": "Source MAC equals destination MAC - Bad packet was received from peer",
                       "dst_mac_is_reserved": "Destination MAC is reserved (DMAC=01-80-C2-00-00-0x) - Bad packet was "
                                              "received from the peer"}
l3_drop_reason_dict = {
    "ipv6_multicast_ffx0": "IPv6 destination in multicast scope FFx0:/16 - Expected behavior - packet is not routable",
    "ipv6_multicast_ffx1": "IPv6 destination in multicast scope FFx1:/16 - Expected behavior - packet is not routable",
    "ipv4_dst_ip_local_network": "IPv4 destination IP is local network (destination=0.0.0.0/8) - Bad packet was "
                                 "received from the peer",
    "multicast_mac_mismatch": "Multicast MAC mismatch - Bad packet was received from the peer",
    "ip_dst_loopback": "Destination IP is loopback address - Bad packet was received from the peer",
    "limited_broadcast_src_ip": "IPv4 source IP is limited broadcast - Bad packet was received from the peer",
    "non_ip_packet": "Non IP packet - Destination MAC is the router, packet is not routable",
    "packet_size_larger_than_mtu": "Packet size is larger than router interface MTU - Validate the router interface "
                                   "MTU configuration"}
acl_drop_reason_dict = {"ingress_router_acl": "Ingress port ACL - Validate ACL configuration"}
table_parser_info = {
    WJHConsts.RAW_TABLE:
        {'headers_ofset': 0,
         'header_len': 2,
         'len_ofset': 2,
         'data_ofset_from_start': 3,
         'column_ofset': 1,
         'output_key': '#'
         },
    WJHConsts.RAW_ACL_TABLE:
        {'headers_ofset': 1,
         'header_len': 1,
         'len_ofset': 2,
         'data_ofset_from_start': 3,
         'column_ofset': 1,
         'output_key': '#'
         },
    WJHConsts.AGG_TABLE:
        {'headers_ofset': 2,
         'header_len': 1,
         'len_ofset': 3,
         'data_ofset_from_start': 4,
         'column_ofset': 1,
         'output_key': '#'
         },
    WJHConsts.AGG_ACL_TABLE:
        {'headers_ofset': 1,
         'header_len': 1,
         'len_ofset': 2,
         'data_ofset_from_start': 3,
         'column_ofset': 1,
         'output_key': '#'
         }
}


@pytest.fixture(scope='module', autouse=True)
def disable_sflow(cli_objects):
    """
    Disable sFlow and persist the state for the WJH tests, then restore it.

    Persisting the disabled state prevents a config reload during the WJH
    tests from enabling sFlow again.
    """
    sflow_was_enabled = cli_objects.dut.sflow.is_sflow_enabled()
    if not sflow_was_enabled:
        logger.info("sFlow is not enabled; no WJH-specific change is required.")

    try:
        if sflow_was_enabled:
            with allure.step('Disable sFlow and save the configuration before WJH tests'):
                logger.info('Disable sFlow and save the configuration before WJH tests')
                cli_objects.dut.sflow.disable_sflow()
                cli_objects.dut.general.save_configuration()
                assert not cli_objects.dut.sflow.is_sflow_enabled(), \
                    "sFlow should be down after disable + config save before WJH tests"
        yield
    finally:
        if sflow_was_enabled:
            try:
                with allure.step('Restore sFlow and save the configuration after WJH tests'):
                    assert not cli_objects.dut.sflow.is_sflow_enabled(), \
                        "sFlow should still be down before restore after WJH tests"
                    cli_objects.dut.sflow.enable_sflow()
                    cli_objects.dut.general.save_configuration()
                    assert cli_objects.dut.sflow.is_sflow_enabled(), \
                        "sFlow should be up after enable + config save after WJH tests"
            except Exception as err:
                logger.error(f"Failed to restore sFlow after WJH tests: {err}")


@pytest.fixture(scope='module', autouse=True)
def disable_doroce(cli_objects):
    """
    Disable doroce before test in case when doroce enabled and enable back after test
    :param cli_objects: cli_objects fixture
    """
    is_doroce_enabled = cli_objects.dut.doroce.is_doroce_configuration_enabled()
    if is_doroce_enabled:
        cli_objects.dut.doroce.disable_doroce()
    yield
    if is_doroce_enabled:
        cli_objects.dut.doroce.config_doroce_lossless_double_ipool()


@pytest.fixture(scope='module', autouse=True)
def check_global_configuration(engines, check_feature_enabled):
    """
    An autouse fixture to check the global configurations of WJH.
    :param engines: engines fixture
    :param check_feature_enabled: check_feature_enabled fixture
    """
    global_config = engines.dut.run_cmd('show what-just-happened configuration global')
    wjh_global = generic_sonic_output_parser(global_config)[0]
    try:
        with allure.step('Validating debug mode in WJH'):
            if wjh_global.get('Mode') != 'debug':
                pytest.fail("Debug mode is not enabled. Skipping test.")
    except Exception as e:
        pytest.fail("Could not fetch global configuration information.")


@pytest.fixture(scope='module', autouse=True)
def get_channel_configuration(engines, check_feature_enabled):
    """
    An autouse fixture to check the channel configurations of WJH.
    :param engines: engines fixture
    :param check_feature_enabled: check_feature_enabled fixture
    """
    channels_config = engines.dut.run_cmd('show what-just-happened configuration channels')
    pytest.CHANNEL_CONF = generic_sonic_output_parser(channels_config, output_key="Channel")
    logger.info(f"pytest.CHANNEL_CONF: {pytest.CHANNEL_CONF}")


@pytest.fixture(scope='module')
def check_feature_enabled(cli_objects):
    """
    An autouse fixture to check if WJH fixture is enabled
    :param cli_objects: cli_objects fixture
    """
    with allure.step('Validating WJH feature is installed and enabled on the DUT'):
        status, msg = is_feature_ready(cli_objects, feature_name='what-just-happened',
                                       docker_name='what-just-happened')
        if not status:
            pytest.skip(f"{msg} Skipping the test.")
    with allure.step('Validating WJH docker is UP'):
        cli_objects.dut.general.verify_dockers_are_up(dockers_list=['what-just-happened'])


def check_if_channel_enabled(cli_object, engines, channel, channel_type):
    if channel == "buffer" and cli_object.general.is_spc1():
        pytest.skip("buffer channel is not supported in SPC1.")
    if not wjh_is_channel_enabled(cli_object, channel):
        pytest.fail("{} channel is not confiugred on WJH.".format(channel))
    if channel_type not in pytest.CHANNEL_CONF[channel]['Type']:
        logger.info(f"pytest.CHANNEL_CONF: {pytest.CHANNEL_CONF}")
        pytest.fail("{} {} channel type is not confiugred on WJH.".format(channel, channel_type))


def get_parsed_table(dut, cmd, table_type):
    output = dut.run_cmd(cmd)
    parser = table_parser_info[table_type]
    table = generic_sonic_output_parser(output, headers_ofset=parser['headers_ofset'],
                                        len_ofset=parser['len_ofset'],
                                        data_ofset_from_start=parser['data_ofset_from_start'],
                                        column_ofset=parser['column_ofset'],
                                        output_key=parser['output_key'],
                                        header_line_number=parser['header_len'])
    return table


@pytest.fixture(scope='class', autouse=True)
def disable_trimming(topology_obj, cli_objects, interfaces, engines):
    """
    This function is used to disable trimming for all buffer profiles before the test and enable it after the test.
    Only operates on SPC4 and above platforms.
    """
    trimming_enabled_profiles = []

    # Check if platform is SPC4 or above - trimming is only supported on SPC4+
    if cli_objects.dut.general.is_spc4_or_above():
        logger.info('WJH trimming pre test')
        trimming_enabled_profiles = cli_objects.dut.trimming.discover_trimming_enabled_profiles()
        logger.info(f"Found trimming enabled profiles: {trimming_enabled_profiles}")
        for profile_name in trimming_enabled_profiles:
            current_status = cli_objects.dut.trimming.get_buffer_profile_trimming_status(profile_name)
            logger.info(f"Profile {profile_name} current status: {current_status}")
            logger.info(f"Disabling trimming for profile: {profile_name}")
            cli_objects.dut.trimming.configure_trimming_action(profile_name, "off")
    yield

    if cli_objects.dut.general.is_spc4_or_above() and trimming_enabled_profiles:
        logger.info('WJH trimming post test')
        for profile_name in trimming_enabled_profiles:
            current_status = cli_objects.dut.trimming.get_buffer_profile_trimming_status(profile_name)
            logger.info(f"Profile {profile_name} current status: {current_status}")
            logger.info(f"Enable trimming for profile: {profile_name}")
            cli_objects.dut.trimming.configure_trimming_action(profile_name, "on")


@pytest.fixture(scope='class', autouse=True)
def enable_channel_buffer(cli_objects, interfaces, engines):
    """
    This fixture is used to enable the buffer channel before the test and disable it after the test.
    """
    logger.info(f"checking if platform is SPC1 since buffer channel is not supported in SPC1")
    if cli_objects.dut.general.is_spc1():
        pytest.skip("buffer channel is not supported in SPC1.")
    else:
        try:
            with allure.step('WJH buffer channel pre test'):
                initial_state_buffer_enabled = cli_objects.dut.wjh.is_channel_enabled("buffer")
                logger.info(f"Initial state enabled: {initial_state_buffer_enabled}")
                if not initial_state_buffer_enabled:
                    logger.info("Enabling buffer channel")
                    cli_objects.dut.wjh.config_wjh_channel_state("buffer", "enabled")
                    if not cli_objects.dut.wjh.is_channel_enabled("buffer"):
                        raise AssertionError("wjh_buffer_channel_management_fixture: Buffer channel is not enabled")
                else:
                    logger.info("Buffer channel already enabled")
            logger.info("Setup complete, yielding to tests")
        except Exception as e:
            logger.error(f"Setup failed with exception: {e}")
            raise
        yield

        try:
            with allure.step('WJH buffer channel post test'):
                if not initial_state_buffer_enabled:
                    logger.info("Restoring to disabled state")
                    cli_objects.dut.wjh.config_wjh_channel_state("buffer", "disabled")
                    if cli_objects.dut.wjh.is_channel_enabled("buffer"):
                        raise AssertionError("Buffer channel is still enabled")
        except Exception as e:
            logger.error(f"Cleanup failed with exception: {e}")


@pytest.fixture(scope='class')
def wjh_buffer_configuration(topology_obj, cli_objects, interfaces, engines):
    """
    Pytest fixture which is doing configuration fot WJH Buffer test case
    :param topology_obj: topology object fixture
    :param cli_objects: cli_objects fixture
    :param interfaces: interfaces fixture
    """
    with allure.step('Check that links are in UP state'):
        ports_list = [interfaces.dut_ha_1, interfaces.dut_ha_2, interfaces.dut_hb_1, interfaces.dut_hb_2]
        retry_call(cli_objects.dut.interface.check_ports_status, fargs=[ports_list], tries=10, delay=10,
                   logger=logger)
    with allure.step(f"Configuring port {interfaces.dut_ha_2}, pg 0, congestion threshold = 10%, "
                     f"latency threshold = 100ns"):
        thresholds_config_dict = {
            'dut': [{'iface': interfaces.dut_ha_2, 'queue_type': 'queue', 'threshold': 10},
                    {'iface': interfaces.dut_ha_2, 'queue_type': 'latency', 'threshold': 100}
                    ]
        }
    with allure.step(f"Config the shaper of the port {interfaces.dut_ha_2}"):
        port_scheduler = "port_scheduler"
        cli_objects.dut.interface.config_port_scheduler(port_scheduler, SonicConst.MIN_SHAPER_RATE_BPS)
        cli_objects.dut.interface.config_port_qos_map(interfaces.dut_ha_2, port_scheduler)
    logger.info('Starting WJH Buffer configuration')
    cli_objects.dut.interface.disable_interfaces([interfaces.dut_ha_2, interfaces.dut_hb_2])
    WjhBufferConfigTemplate.configuration(topology_obj, thresholds_config_dict)
    cli_objects.dut.interface.enable_interfaces([interfaces.dut_ha_2, interfaces.dut_hb_2])
    cli_objects.dut.interface.check_link_state([interfaces.dut_ha_2, interfaces.dut_hb_2])
    logger.info('WJH Buffer configuration completed')
    with allure.step('Check qos counter is ready'):
        check_qos_counter_status(engines)
    yield
    with allure.step("delete configured qos map and port scheduler"):
        cli_objects.dut.interface.del_port_qos_map(interfaces.dut_ha_2, port_scheduler)
        cli_objects.dut.interface.del_port_scheduler(port_scheduler)
    WjhBufferConfigTemplate.cleanup(topology_obj, thresholds_config_dict)
    logger.info('WJH Buffer cleanup completed')


def flush_wjh_cmd(engine):
    output = engine.run_cmd("show what-just-happened poll")
    if "Timeout waiting for daemon" in output:
        raise Exception("WJH daemon timeout")
    return output


@pytest.fixture(scope="function", autouse=True)
def flush_wjh_table(engines):
    logger.info(
        "\n\nFlushing WJH Table before running the test case to avoid background noise from dropped packets\n\n")
    retry_call(flush_wjh_cmd, fargs=[engines.dut], tries=5, delay=5, logger=logger)
    yield


def check_if_entry_exists(table, interface, dst_ip, src_ip, proto, drop_reason, dst_mac, src_mac):
    """
    A function that checks if an entry with variables exists in the recieved table
    If found, the entry is returned as well
    :param table: a table made of dictionary
    :param interface: the interface name
    :param dst_ip: dst ip
    :param src_ip: src ip
    :param proto: protocol
    :param drop_reason: drop reason
    :param dst_mac: dst mac
    :param src_mac: src mac
    """
    result = {'result': False, 'entry': None}
    for key in table:
        entry = table[key]
        # If entry is a list, it means that the message is longer than one line,
        # but all rest of info is in the first entry
        if isinstance(entry, list):
            entry = entry[0]
        format_wjh_entry_data(entry)
        entry_src_ip = utils.parse_ip_address_from_packet(entry['Src IP:Port'])
        entry_dst_ip = utils.parse_ip_address_from_packet(entry['Dst IP:Port'])
        logger.info(f"\nExpected Entry is with:\n"
                    f"interface = {interface}, src_ip = {src_ip}, dst_ip = {dst_ip}, proto = {proto}, "
                    f"drop_reason = {drop_reason}, dst_mac = {dst_mac}, src_mac = {src_mac}")
        logger.info(f"\nActual Entry is:\nFull Table Entry: \n {entry}\nParsed Fields: \n"
                    f"interface = {entry['sPort']}, src_ip = {entry_src_ip}, dst_ip = {entry_dst_ip}, "
                    f"proto = {entry['IP Proto']}, drop_reason = {entry['Drop reason - Recommended action']}, "
                    f"dst_mac = {entry['dMAC']}, src_mac = {entry['sMAC']}")
        logger.info(f"\nExpected Entry Vs Actual Entry Comparison:\n"
                    f"sPort equal = {entry['sPort'] == interface}\n"
                    f"src_ip equal = {entry_src_ip == src_ip}\n"
                    f"dst_ip equal = {entry_dst_ip == dst_ip}\n"
                    f"protocol equal = {entry['IP Proto'] == proto}\n"
                    f"dst_mac equal = {entry['dMAC'] == dst_mac}\n"
                    f"src_mac equal = {entry['sMAC'] == src_mac}\n"
                    f"drop_reason equal = {entry['Drop reason - Recommended action'] in drop_reason}\n")
        if (entry['sPort'] == interface and
                entry_src_ip == src_ip and
                entry_dst_ip == dst_ip and
                entry['IP Proto'] == proto and
                entry['dMAC'] == dst_mac and
                entry['sMAC'] == src_mac and
                entry['Drop reason - Recommended action'] in drop_reason):
            result['result'] = True
            result['entry'] = entry
            break
    return result


def format_wjh_entry_data(entry):
    """
    Some column data of the entry may take more than one line, for this case, the entry data will be a list,
    need to convert it to string
    :param entry: the wjh entry data, in dict format
    :return: None
        for example:
            entry before format:
            {'#': '4',
            'Timestamp': '22/01/19 07:22:15.525',
            'sPort': 'Ethernet248', 'dPort': 'N/A', 'VLAN': 'N/A',
            'sMAC': '98:03:9b:9b:3b:22', 'dMAC': '33:33:00:00:00:16',
            'EthType': 'IPv6', 'Src IP:Port': ['fe80::9a03:9bff:fe9b:', '3b22'],
            'Dst IP:Port': 'ff02::16', 'IP Proto': 'ip', 'Drop Group': 'L2', 'Severity': 'Warn',
            'Drop reason - Recommended action': ['Multicast egress port list is empty - Validate',
                                                'why IGMP join or multicast router port does not', 'exist']}
            entry after format:
            {'#': '4',
            'Timestamp': '22/01/19 07:22:15.525',
            'sPort': 'Ethernet248', 'dPort': 'N/A', 'VLAN': 'N/A',
            'sMAC': '98:03:9b:9b:3b:22', 'dMAC': '33:33:00:00:00:16',
            'EthType': 'IPv6', 'Src IP:Port': 'fe80::9a03:9bff:fe9b:3b22'],
            'Dst IP:Port': 'ff02::16', 'IP Proto': 'ip', 'Drop Group': 'L2', 'Severity': 'Warn',
            'Drop reason - Recommended action': 'Multicast egress port list is empty - Validate why IGMP
                                                join or multicast router port does not exist'}
    """
    for column_header in ['sPort', 'Src IP:Port', 'Dst IP:Port', 'dMAC', 'sMAC',
                          'Drop reason - Recommended action']:
        if isinstance(entry[column_header], list):
            if column_header == 'Drop reason - Recommended action':
                entry[column_header] = " ".join(entry[column_header])
            else:
                entry[column_header] = "".join(entry[column_header])


def validate_wjh_table(engines, cmd, table_type, interface, dst_ip, src_ip, proto, drop_reason, dst_mac, src_mac):
    """
    A function that checks the WJH table
    :param engines: engines fixture
    :param cmd: command to execute on DUT
    :param table_type: table type
    :param interface: an interface name
    :param dst_ip: dst ip
    :param src_ip: src ip
    :param proto: protocol
    :param drop_reason: drop reason
    :param dst_mac: dst mac
    :param src_mac: src mac
    """
    table = get_parsed_table(engines.dut, cmd, table_type)
    result = check_if_entry_exists(table, interface, dst_ip,
                                   src_ip, proto, drop_reason, dst_mac, src_mac)
    if not result['result']:
        raise Exception(f"Could not find drop in WJH {table_type} table.\nThe table is:\n{table}")


def validate_wjh_acl_buffer_table(engines, cmd, table_types, interface, dst_ip, src_ip, proto, drop_reason_message,
                                  dst_mac, src_mac, drop_reason, table_separator, cli_object):
    """
    A function that checks the WJH buffer/acl tables (raw/agg + second page)
    :param engines: engines fixture
    :param cmd: command to execute on DUT
    :param table_types: table types
    :param interface: an interface name
    :param dst_ip: dst ip
    :param src_ip: src ip
    :param proto: protocol
    :param drop_reason_message: drop reason message
    :param dst_mac: dst mac
    :param src_mac: src mac
    :param drop_reason: drop reason
    :param table_separator: name of second table in WJH output of buffer/acl, used to parse the tables.
    :param cli_object: cli_object
    """
    output = engines.dut.run_cmd(cmd)
    split_tables = output.split(table_separator)
    parsed_tables = []
    for table_type, table in zip(table_types, split_tables):
        parser = table_parser_info[table_type]
        parsed_table = generic_sonic_output_parser(table, headers_ofset=parser['headers_ofset'],
                                                   len_ofset=parser['len_ofset'],
                                                   data_ofset_from_start=parser['data_ofset_from_start'],
                                                   column_ofset=parser['column_ofset'],
                                                   output_key=parser['output_key'],
                                                   header_line_number=parser['header_len'])
        parsed_tables.append(parsed_table)
    result = check_if_entry_exists(parsed_tables[0], interface, dst_ip,
                                   src_ip, proto, drop_reason_message, dst_mac, src_mac)
    if not result['result']:
        raise Exception(f"Could not find drop in WJH {table_types[0]} table.\n The table is: \n {parsed_tables[0]}")
    # If the call is from test_buffer, drop reason will be one of these, else, it will be None and this clause will
    # be skipped
    if drop_reason in ['buffer_congestion', 'buffer_latency']:
        check_buffer_info_table(parsed_tables[1], result['entry'], drop_reason, table_types[0],
                                cli_object, is_dynamic_buffer_configured(engines))


def is_dynamic_buffer_configured(engines):
    get_buffer_mode_cmd = 'redis-cli -n 4 hget "DEVICE_METADATA|localhost" buffer_model'
    buffer_mode = engines.dut.run_cmd(get_buffer_mode_cmd)
    return buffer_mode.strip('"') == "dynamic"


def check_buffer_info_table(table, entry, drop_reason, table_type, cli_object, is_dynamic_buffer=False):
    """
    A function that checks the WJH buffer info table
    :param table: buffer info table
    :param entry: entry which found on raw/agg table
    :param drop_reason: drop reason
    :param table_type: table type (raw/agg)
    :param is_dynamic_buffer: whether dynamic or static buffer mode is configured
    """
    logger.info(f'Validating buffer table. Table type is:{table_type}, '
                f'is dynamic buffer configured:{is_dynamic_buffer}')
    index = entry['#']
    entry_found = False
    tc_id = "N/A"
    tc_usage = "N/A"
    latency = "N/A"
    tc_watermark = "N/A"
    latency_watermark = "N/A"
    latency_exceed_substring = "Latency"
    tc_watermark_exceed_substring = "TC Watermark >"
    occupancy_exceed_substring = "Occupancy >"
    # Lossy-only platforms use TC 0, others use TC 1 based on DSCP->TC->QUEUE mapping
    lossy_only_hwsku = cli_object.chassis.get_platform_hwsku() in LOSSY_ONLY_HWSKUS
    expected_tc_id = '0' if lossy_only_hwsku else '1'

    for key in table:
        entry = table[key]
        # If entry is a list, it means that the message is longer then one line,
        # but all rest of info is in the first entry
        if isinstance(entry, list):
            entry = entry[0]
        if (entry['#'] == index):
            tc_id = entry['TC ID']
            tc_usage = entry['TC Usage [KB]']
            latency = entry['Latency [nanoseconds]']
            tc_watermark = entry['TC Watermark [KB]']
            latency_watermark = entry['Latency Watermark [nanoseconds]']
            entry_found = True
            break
    if not entry_found:
        pytest.fail("Buffer info table does not contain the entry found on raw/agg table.")
    if (table_type == WJHConsts.RAW_TABLE):
        if drop_reason == 'buffer_congestion':
            if (tc_id == expected_tc_id and (
                    (occupancy_exceed_substring in tc_usage) or (tc_usage != "N/A" and int(tc_usage) > 0)) and
                    latency == "N/A" and tc_watermark == "N/A" and latency_watermark == "N/A"):
                return
        elif drop_reason == 'buffer_latency':
            if (tc_id == expected_tc_id and (
                    (occupancy_exceed_substring in tc_usage) or (tc_usage != "N/A" and int(tc_usage) > 0)) and
                    ((latency_exceed_substring in latency) or (latency != "N/A" and int(latency) > 0)) and
                    tc_watermark == "N/A" and latency_watermark == "N/A"):
                return
    elif (table_type == WJHConsts.AGG_TABLE):
        if drop_reason == 'buffer_congestion':
            if (tc_id == expected_tc_id and tc_usage == "N/A" and latency == "N/A" and
                    ((tc_watermark_exceed_substring in tc_watermark) or (
                        tc_watermark != "N/A" and int(tc_watermark))) > 0 and
                    latency_watermark == "N/A"):
                return
        elif (drop_reason == 'buffer_latency'):
            if (tc_id == expected_tc_id and tc_usage == "N/A" and latency == "N/A" and
                    ((tc_watermark_exceed_substring in tc_watermark) or (
                        tc_watermark != "N/A" and int(tc_watermark))) > 0 and
                    latency_watermark != "N/A" and
                    ((latency_exceed_substring in latency_watermark) or int(latency_watermark) > 0)):
                return
    raise Exception(f"Buffer info table is wrong, tc_id = {tc_id}, tc_usage = {tc_usage}, latency = {latency}, "
                    f"tc_watermark = {tc_watermark}, latency_watermark = {latency_watermark}")


def do_raw_test(engines, cli_object, channel, channel_type, interface, dst_ip, src_ip, proto, drop_reason, dst_mac,
                src_mac, command):
    """
    A function that checks the WJH feature with raw channel type
    :param engines: engines fixture
    :param cli_object: cli_object
    :param interface: the interface name
    :param dst_ip: dst ip
    :param src_ip: src ip
    :param proto: protocol
    :param drop_reason: drop reason
    :param dst_mac: dst mac
    :param src_mac: src mac
    :param command: raw command
    """
    check_if_channel_enabled(cli_object, engines, channel, channel_type)
    retry_call(validate_wjh_table,
               fargs=[engines, command, WJHConsts.RAW_TABLE, interface, dst_ip, src_ip, proto, drop_reason,
                      dst_mac, src_mac],
               tries=3, delay=3, logger=logger)


def do_acl_buffer_raw_test(engines, cli_object, channel, channel_types, interface, dst_ip, src_ip, proto,
                           drop_reason_message, dst_mac, src_mac, command, table_separator, drop_reason=None):
    """
    A function that checks the WJH feature with raw channel type
    :param engines: engines fixture
    :param cli_object: cli_object
    :param channel: channel
    :param channel_types: channel types
    :param interface: the interface name
    :param dst_ip: dst ip
    :param src_ip: src ip
    :param proto: protocol
    :param drop_reason_message: drop reason message
    :param dst_mac: dst mac
    :param src_mac: src mac
    :param command: raw command
    :param drop_reason: drop reason
    :param table_separator: table separator that will be used in validate_wjh_acl_buffer_table to split the two tables
    """
    check_if_channel_enabled(cli_object, engines, channel, channel_types[0])
    retry_call(validate_wjh_acl_buffer_table, fargs=[engines, command, channel_types, interface, dst_ip, src_ip, proto,
                                                     drop_reason_message, dst_mac, src_mac,
                                                     drop_reason, table_separator, cli_object],
               tries=3, delay=3, logger=logger)


def do_agg_test(engines, cli_object, channel, channel_type, interface, dst_ip, src_ip, proto, drop_reason, dst_mac,
                src_mac, command):
    """
    A function that checks the WJH feature with aggregated channel type
    :param engines: engines fixture
    :param cli_object: cli_object
    :param interface: the interface name
    :param dst_ip: dst ip
    :param src_ip: src ip
    :param proto: protocol
    :param drop_reason: drop reason
    :param dst_mac: dst mac
    :param src_mac: src mac
    :param command: aggregate command
    """
    check_if_channel_enabled(cli_object, engines, channel, channel_type)
    retry_call(validate_wjh_table,
               fargs=[engines, command, WJHConsts.AGG_TABLE, interface, dst_ip, src_ip, proto, drop_reason,
                      dst_mac, src_mac],
               tries=3, delay=3, logger=logger)


def do_acl_buffer_agg_test(engines, cli_object, channel, channel_types, interface, dst_ip, src_ip, proto,
                           drop_reason_message, dst_mac, src_mac, command, table_separator, drop_reason=None):
    """
    A function that checks the WJH feature with aggregated channel type
    :param engines: engines fixture
    :param cli_object: cli_object
    :param channel: channel
    :param channel_types: channel types
    :param interface: the interface name
    :param dst_ip: dst ip
    :param src_ip: src ip
    :param proto: protocol
    :param drop_reason_message: drop reason message
    :param dst_mac: dst mac
    :param src_mac: src mac
    :param command: raw command
    :param drop_reason: drop reason
    :param table_separator: table separator that will be used in validate_wjh_acl_buffer_table to split the two tables
    """
    check_if_channel_enabled(cli_object, engines, channel, channel_types[0])
    retry_call(validate_wjh_acl_buffer_table, fargs=[engines, command, channel_types, interface, dst_ip, src_ip, proto,
                                                     drop_reason_message, dst_mac, src_mac,
                                                     drop_reason, table_separator, cli_object],
               tries=3, delay=3, logger=logger)


def scapy_packet_validation(interfaces, pkt, topology_obj, count):
    vlan_interface = f"{interfaces.ha_dut_2}.40"
    validation = {'sender': 'ha', 'send_args': {'interface': vlan_interface, 'packets': pkt, 'count': count}}
    ScapyChecker(topology_obj.players, validation).run_validation()


def generate_wjh_poll_cmd(channel, aggregate=False):
    """
    The function generates the wjh poll command according to the given channel
    :param channel: A WJH channel, i.e. forwarding
    :param aggregate: whether to include the '-- aggregate' flag in the command
    :return: Returns a full WJH polling cmd, i.e. show what-just-happened poll layer-1 --aggregate
    """
    aggregate_flag = WJHConsts.AGGREGATE_FLAG if aggregate else ''
    return ' '.join([WJHConsts.WJH_POLL_CMD_PREFIX, channel, aggregate_flag])


@pytest.mark.wjh
@pytest.mark.build
@pytest.mark.physical_coverage
@pytest.mark.push_gate
@pytest.mark.usefixtures("wjh_buffer_configuration")
class TestBuffer:
    @pytest.mark.parametrize("drop_reason", drop_reason_dict.keys())
    @allure.title('WJH Buffer test case')
    def test_buffer(self, drop_reason, engines, topology_obj, players, interfaces,
                    ha_dut_2_mac, hb_dut_2_mac, sonic_branch):
        """
        This test will configure the DUT and hosts to generate buffer drops
        """
        validation = {
            'server': 'ha',
            'client': 'hb',
            'client_args': {
                'server_address': '40.0.0.2',
                'duration': '30',
                'bandwidth': '20G',
                'protocol': 'UDP',
                'length': '65507',
                'window': '415k'
            },
            'expect': [
                {
                    'parameter': 'loss_packets',
                    'operator': '>=',
                    'type': 'int',
                    'value': '0'
                }
            ]
        }
        ping_validation = {'sender': 'hb', 'args': {'count': 3, 'dst': '40.0.0.2'}}
        ping_checker = PingChecker(players, ping_validation)
        try:
            retry_call(ping_checker.run_validation, fargs=[], tries=14, delay=10, logger=logger)
            with allure.step('Sending iPerf traffic'):
                logger.info('Sending iPerf traffic')
                IperfChecker(players, validation).run_validation()
            ha_ip = '40.0.0.2'
            hb_ip = '40.0.0.3'
            drop_reason_message = drop_reason_dict[drop_reason]
            cli_object = topology_obj.players['dut']['cli']
            with allure.step('Validating WJH raw table output'):
                do_acl_buffer_raw_test(engines=engines, cli_object=cli_object, channel=WJHConsts.BUFFER,
                                       channel_types=[WJHConsts.RAW_TABLE, WJHConsts.RAW_ACL_TABLE],
                                       interface=interfaces.dut_hb_2, dst_ip=ha_ip,
                                       src_ip=hb_ip, proto=WJHConsts.UDP_PROTO, drop_reason_message=drop_reason_message,
                                       dst_mac=ha_dut_2_mac, src_mac=hb_dut_2_mac,
                                       command=generate_wjh_poll_cmd(WJHConsts.BUFFER),
                                       drop_reason=drop_reason, table_separator=WJHConsts.BUFFER_TABLE_SEPARATOR)
            with allure.step('Sending iPerf traffic'):
                logger.info('Sending iPerf traffic')
                IperfChecker(players, validation).run_validation()
            with allure.step('Validating WJH aggregated table output'):
                # The ip protocol cannot be parsed when the packet is fragmented.
                # It will be displayed as "ip" in the table.
                # As Extend WJH linux channel support with current buffer capabilities via WJH lib feature
                # It will be displayed as "udp" in the pull buffer aggregate table in master and 202311 branch
                agg_proto = WJHConsts.IP_PROTO if sonic_branch in ['202211', '202305'] else WJHConsts.UDP_PROTO
                do_acl_buffer_agg_test(engines=engines, cli_object=cli_object, channel=WJHConsts.BUFFER,
                                       channel_types=[WJHConsts.AGG_TABLE, WJHConsts.AGG_ACL_TABLE],
                                       interface=interfaces.dut_hb_2, dst_ip=ha_ip,
                                       src_ip=hb_ip, proto=agg_proto, drop_reason_message=drop_reason_message,
                                       dst_mac=ha_dut_2_mac, src_mac=hb_dut_2_mac,
                                       command=generate_wjh_poll_cmd(WJHConsts.BUFFER, aggregate=True),
                                       drop_reason=drop_reason, table_separator=WJHConsts.BUFFER_TABLE_SEPARATOR)
        except Exception as e:
            pytest.fail(f"Could not finish the test due to exception: \n{e}.\nAborting!.")


def is_ports_in_state(cli_objects, ports_list, state='up'):
    cli_objects.dut.interface.check_ports_status(ports_list, state)
    return True


@pytest.mark.wjh
@pytest.mark.build
@allure.title('WJH L1 Raw test case')
def test_l1_raw_drop(engines, cli_objects):
    port = cli_objects.dut.interface.get_active_phy_port()
    if not port:
        pytest.skip("Could not find port in active state. Skipping the test.")
    try:
        with allure.step('Shutting down {} interface'.format(port)):
            cli_objects.dut.interface.disable_interface(port)
            wait_until(30, 1, 0, is_ports_in_state, cli_objects, [port], 'down')

        drop_reason_message = 'Generic L1 event - Check layer 1 aggregated information'
        na = WJHConsts.NA
        with allure.step('Validating WJH raw table output'):
            do_raw_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.LAYER_1,
                        channel_type=WJHConsts.RAW_CHANNEL,
                        interface=port, dst_ip=na, src_ip=na, proto=na, drop_reason=drop_reason_message,
                        dst_mac=na, src_mac=na, command=generate_wjh_poll_cmd(WJHConsts.LAYER_1))
    except Exception as e:
        pytest.fail(f"Could not finish the test due to exception: \n{str(e)}.\nAborting!.")
    finally:
        cli_objects.dut.interface.enable_interface(port)
        wait_until(30, 1, 0, is_ports_in_state, cli_objects, [port], 'up')


@pytest.mark.wjh
@pytest.mark.build
@allure.title('WJH L1 Aggregated test case')
def test_l1_agg_drop(engines, cli_objects):
    check_if_channel_enabled(cli_objects.dut, engines, WJHConsts.LAYER_1, WJHConsts.AGG_CHANNEL)
    port = cli_objects.dut.interface.get_active_phy_port()
    if not port:
        pytest.skip("Could not find port in active state. Skipping the test.")
    with allure.step('Shutting down {} interface'.format(port)):
        cli_objects.dut.interface.disable_interface(port)
    with allure.step('Checking for port to be down'):
        cli_objects.dut.interface.check_link_state([port], 'down')
    drop_reason_message = 'Port admin down - Validate port configuration'
    try:
        with allure.step('Validating WJH L1 Aggregated table output with down port'):
            table = get_parsed_table(engines.dut, generate_wjh_poll_cmd(WJHConsts.LAYER_1, aggregate=True),
                                     WJHConsts.AGG_TABLE)
            verify_l1_agg_drop_exists(table, port, 'Down', drop_reason_message)
        with allure.step('Starting up {} interface'.format(port)):
            cli_objects.dut.interface.enable_interface(port)
            retry_call(cli_objects.dut.interface.check_ports_status, fargs=[[port]], tries=10, delay=5,
                       logger=logger)
            time.sleep(3)
        with allure.step('Validating WJH L1 Aggregated table output with up port'):
            table = get_parsed_table(engines.dut, generate_wjh_poll_cmd(WJHConsts.LAYER_1, aggregate=True),
                                     WJHConsts.AGG_TABLE)
            verify_l1_agg_drop_exists(table, port, 'Up', drop_reason_message)
    except Exception as e:
        pytest.fail(f"Could not finish the test due to exception: \n{str(e)}.\nAborting!.")
    finally:
        with allure.step('Starting up {} interface'.format(port)):
            cli_objects.dut.interface.enable_interface(port)


def verify_l1_agg_drop_exists(table, port, state, drop_reason_message):
    entry_exists = False
    for entry in table:
        if (table[entry]['State'] == state and
                table[entry]['Port'] == port and
                table[entry]['Down Reason - Recommended Action'] and
                int(table[entry]['State Change']) > 0):
            entry_exists = True
            break
    if not entry_exists:
        pytest.fail("Could not find L1 drop on WJH aggregated table.")
    return entry


@pytest.mark.wjh
@pytest.mark.build
@pytest.mark.push_gate
@allure.title('WJH L2 test case')
def test_l2_src_mac_equals_dst_mac(engines, cli_objects, topology_obj, interfaces, hb_dut_2_mac):
    src_ip = WJHConsts.TESTED_SRC_IP
    dst_ip = WJHConsts.TESTED_DST_IP
    pkt = WJHConsts.TCP_IPV4_PACKET_FORMAT.format(SRC_MAC=hb_dut_2_mac, DST_MAC=hb_dut_2_mac, SRC_IP=src_ip, DST_IP=dst_ip)
    drop_reason_message = l2_drop_reason_dict["src_mac_equals_dst_mac"]
    try:
        with allure.step('Sending packet with src mac = dst mac'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.RAW_PACKET_COUNT)
        with allure.step('Validating WJH L2 raw table output'):
            do_raw_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.RAW_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=hb_dut_2_mac, src_mac=hb_dut_2_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING))
        with allure.step(f'Sending {WJHConsts.AGG_PACKET_COUNT} packets with src mac = dst mac'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.AGG_PACKET_COUNT)
        with allure.step('Validating WJH L2 aggregated table output'):
            do_agg_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.AGG_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=hb_dut_2_mac, src_mac=hb_dut_2_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING, aggregate=True))
    except Exception as e:
        pytest.fail(f"Could not finish the test due to exception: \n{str(e)}.\nAborting!.")


@pytest.mark.wjh
@pytest.mark.build
@pytest.mark.push_gate
@allure.title('WJH L3 test case')
def test_l3_dst_ip_is_loopback(engines, cli_objects, topology_obj, interfaces):
    src_mac = WJHConsts.TESTED_SRC_MAC
    broadcast_mac = 'ff:ff:ff:ff:ff:ff'
    loopback_ip = '127.0.0.1'
    src_ip = WJHConsts.TESTED_SRC_IP
    pkt = WJHConsts.TCP_IPV4_PACKET_FORMAT.format(SRC_MAC=src_mac, DST_MAC=broadcast_mac, SRC_IP=src_ip, DST_IP=loopback_ip)
    drop_reason_message = l3_drop_reason_dict["ip_dst_loopback"]
    try:
        with allure.step('Sending loopback dst ip packet'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.RAW_PACKET_COUNT)
        with allure.step('Validating WJH L3 raw table output'):
            do_raw_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.RAW_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=loopback_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=broadcast_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING))
        with allure.step(f'Sending {WJHConsts.AGG_PACKET_COUNT} loopback dst ip packets'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.AGG_PACKET_COUNT)
        with allure.step('Validating WJH L3 aggregated table output'):
            do_agg_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.AGG_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=loopback_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=broadcast_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING, aggregate=True))
    except Exception as e:
        pytest.fail(f"Could not finish the test due to exception: \n{str(e)}.\nAborting!.")


@pytest.mark.wjh
@pytest.mark.build
@allure.title('WJH L2 test case')
def test_l2_src_mac_is_multicast(engines, cli_objects, topology_obj, interfaces):
    src_mac = '01:00:5e:01:02:04'
    dst_mac = cli_objects.dut.mac.get_mac_address_for_interface(interfaces.dut_hb_2)
    src_ip = WJHConsts.TESTED_SRC_IP
    dst_ip = WJHConsts.TESTED_DST_IP
    pkt = WJHConsts.TCP_IPV4_PACKET_FORMAT.format(SRC_MAC=src_mac, DST_MAC=dst_mac, SRC_IP=src_ip, DST_IP=dst_ip)
    drop_reason_message = l2_drop_reason_dict["multicast_src_mac"]
    try:
        with allure.step('Sending multicast src mac packet'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.RAW_PACKET_COUNT)
        with allure.step('Validating WJH L2 raw table output'):
            do_raw_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.RAW_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING))
        with allure.step(f'Sending {WJHConsts.AGG_PACKET_COUNT} multicast src mac packets'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.AGG_PACKET_COUNT)
        with allure.step('Validating WJH L2 aggregated table output'):
            do_agg_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.AGG_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING, aggregate=True))
    except Exception as e:
        pytest.fail(f"Could not finish the test due to exception: \n{str(e)}.\nAborting!.")


@pytest.mark.wjh
@pytest.mark.build
@allure.title('WJH L3 test case')
def test_l3_ipv6_dst_multicast_scope_ffx0(engines, cli_objects, topology_obj, interfaces):
    src_mac = WJHConsts.TESTED_SRC_MAC
    dst_mac = cli_objects.dut.mac.get_mac_address_for_interface(interfaces.dut_hb_2)
    dst_ip = "ff00::42:1"
    src_ip = "2001:db8::1"
    pkt = WJHConsts.TCP_IPV6_PACKET_FORMAT.format(SRC_MAC=src_mac, DST_MAC=dst_mac, SRC_IP=src_ip, DST_IP=dst_ip)
    drop_reason_message = l3_drop_reason_dict["ipv6_multicast_ffx0"]
    try:
        with allure.step('Sending ffx0 multicast dst ip packet'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.RAW_PACKET_COUNT)
        with allure.step('Validating WJH L3 raw table output'):
            do_raw_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.RAW_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING))
        with allure.step(f'Sending {WJHConsts.AGG_PACKET_COUNT} ffx0 multicast dst ip packets'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.AGG_PACKET_COUNT)
        with allure.step('Validating WJH L3 aggregated table output'):
            do_agg_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.AGG_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING, aggregate=True))
    except Exception as e:
        pytest.fail(f"Could not finish the test due to exception: \n{str(e)}.\nAborting!.")


@pytest.mark.wjh
@pytest.mark.build
@allure.title('WJH L3 test case')
def test_l3_ipv6_dst_multicast_scope_ffx1(engines, cli_objects, topology_obj, interfaces):
    src_mac = WJHConsts.TESTED_SRC_MAC
    dst_mac = cli_objects.dut.mac.get_mac_address_for_interface(interfaces.dut_hb_2)
    dst_ip = "ff01::42:1"
    src_ip = "2001:db8::2"
    pkt = WJHConsts.TCP_IPV6_PACKET_FORMAT.format(SRC_MAC=src_mac, DST_MAC=dst_mac, SRC_IP=src_ip, DST_IP=dst_ip)
    drop_reason_message = l3_drop_reason_dict["ipv6_multicast_ffx1"]
    try:
        with allure.step('Sending ffx1 multicast dst ip packet'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.RAW_PACKET_COUNT)
        with allure.step('Validating WJH L3 raw table output'):
            do_raw_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.RAW_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING))
        with allure.step(f'Sending {WJHConsts.AGG_PACKET_COUNT} ffx1 multicast dst ip packets'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.AGG_PACKET_COUNT)
        with allure.step('Validating WJH L3 aggregated table output'):
            do_agg_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.AGG_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING, aggregate=True))
    except Exception as e:
        pytest.fail(f"Could not finish the test due to exception: \n{str(e)}.\nAborting!.")


@pytest.mark.wjh
@pytest.mark.build
@allure.title('WJH L3 test case')
def test_l3_multicast_mac_mismatch(engines, cli_objects, topology_obj, interfaces):
    src_mac = WJHConsts.TESTED_SRC_MAC
    dst_mac = cli_objects.dut.mac.get_mac_address_for_interface(interfaces.dut_hb_2)
    src_ip = WJHConsts.TESTED_SRC_IP
    dst_ip = '224.0.0.12'
    pkt = WJHConsts.TCP_IPV4_PACKET_FORMAT.format(SRC_MAC=src_mac, DST_MAC=dst_mac, SRC_IP=src_ip, DST_IP=dst_ip)
    drop_reason_message = l3_drop_reason_dict["multicast_mac_mismatch"]
    try:
        with allure.step('Sending multicast mac mismatch packet'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.RAW_PACKET_COUNT)
        with allure.step('Validating WJH L3 raw table output'):
            do_raw_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.RAW_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING))
        with allure.step(f'Sending {WJHConsts.AGG_PACKET_COUNT} multicast mac mismatch packets'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.AGG_PACKET_COUNT)
        with allure.step('Validating WJH L3 aggregated table output'):
            do_agg_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.AGG_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING, aggregate=True))
    except Exception as e:
        pytest.fail(f"Could not finish the test due to exception: \n{str(e)}.\nAborting!.")


@pytest.mark.wjh
@pytest.mark.build
@allure.title('WJH L3 test case')
def test_l3_ipv4_limited_broadcast_src_ip(engines, cli_objects, topology_obj, interfaces):
    src_mac = WJHConsts.TESTED_SRC_MAC
    dst_mac = cli_objects.dut.mac.get_mac_address_for_interface(interfaces.dut_hb_2)
    src_ip = '255.255.255.255'
    dst_ip = WJHConsts.TESTED_DST_IP
    pkt = WJHConsts.TCP_IPV4_PACKET_FORMAT.format(SRC_MAC=src_mac, DST_MAC=dst_mac, SRC_IP=src_ip, DST_IP=dst_ip)
    drop_reason_message = l3_drop_reason_dict["limited_broadcast_src_ip"]
    try:
        with allure.step('Sending limited broadcast src ip packet'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.RAW_PACKET_COUNT)
        with allure.step('Validating WJH L3 raw table output'):
            do_raw_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.RAW_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING))
        with allure.step(f'Sending {WJHConsts.AGG_PACKET_COUNT} limited broadcast src ip packets'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.AGG_PACKET_COUNT)
        with allure.step('Validating WJH L3 aggregated table output'):
            do_agg_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.AGG_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING, aggregate=True))
    except Exception as e:
        pytest.fail(f"Could not finish the test due to exception: \n{str(e)}.\nAborting!.")


@pytest.mark.wjh
@pytest.mark.build
@allure.title('WJH L3 test case')
def test_l3_ipv4_dst_local_network(engines, cli_objects, topology_obj, interfaces):
    src_mac = WJHConsts.TESTED_SRC_MAC
    dst_mac = cli_objects.dut.mac.get_mac_address_for_interface(interfaces.dut_hb_2)
    src_ip = WJHConsts.TESTED_SRC_IP
    dst_ip = '0.0.0.2'
    pkt = WJHConsts.TCP_IPV4_PACKET_FORMAT.format(SRC_MAC=src_mac, DST_MAC=dst_mac, SRC_IP=src_ip, DST_IP=dst_ip)
    drop_reason_message = l3_drop_reason_dict["ipv4_dst_ip_local_network"]
    try:
        with allure.step('Sending ipv4 ip dst local network packet'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.RAW_PACKET_COUNT)
        with allure.step('Validating WJH L3 raw table output'):
            do_raw_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.RAW_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING))
        with allure.step(f'Sending {WJHConsts.AGG_PACKET_COUNT} ipv4 ip dst local network packets'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.AGG_PACKET_COUNT)
        with allure.step('Validating WJH L3 aggregated table output'):
            do_agg_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.AGG_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING, aggregate=True))
    except Exception as e:
        pytest.fail(f"Could not finish the test due to exception: \n{str(e)}.\nAborting!.")


@pytest.mark.wjh
@pytest.mark.build
@allure.title('WJH L2 test case')
def test_l2_dst_mac_is_reserved(engines, cli_objects, topology_obj, interfaces):
    src_mac = WJHConsts.TESTED_SRC_MAC
    dst_mac = '01:80:c2:00:00:01'
    src_ip = WJHConsts.TESTED_SRC_IP
    dst_ip = WJHConsts.TESTED_DST_IP
    pkt = WJHConsts.TCP_IPV4_PACKET_FORMAT.format(SRC_MAC=src_mac, DST_MAC=dst_mac, SRC_IP=src_ip, DST_IP=dst_ip)
    drop_reason_message = l2_drop_reason_dict["dst_mac_is_reserved"]
    try:
        with allure.step('Sending reserved dst mac packet'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.RAW_PACKET_COUNT)
        with allure.step('Validating WJH L2 raw table output'):
            do_raw_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.RAW_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING))
        with allure.step(f'Sending {WJHConsts.AGG_PACKET_COUNT} reserved dst mac packets'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.AGG_PACKET_COUNT)
        with allure.step('Validating WJH L2 aggregated table output'):
            do_agg_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.AGG_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING, aggregate=True))
    except Exception as e:
        pytest.fail(f"Could not finish the test due to exception: \n{str(e)}.\nAborting!.")


@pytest.mark.wjh
@pytest.mark.build
@allure.title('WJH L3 test case')
def test_l3_non_ip_packet(engines, cli_objects, topology_obj, interfaces):
    src_mac = WJHConsts.TESTED_SRC_MAC
    dst_mac = cli_objects.dut.mac.get_mac_address_for_interface(interfaces.dut_hb_2)
    src_ip = WJHConsts.NA
    dst_ip = WJHConsts.NA
    proto = WJHConsts.NA
    pkt = f'Ether(src="{src_mac}", dst="{dst_mac}")'
    drop_reason_message = l3_drop_reason_dict["non_ip_packet"]
    try:
        with allure.step('Sending non ip packet'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.RAW_PACKET_COUNT)
        with allure.step('Validating WJH L3 raw table output'):
            do_raw_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.RAW_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=proto,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING))
        with allure.step(f'Sending {WJHConsts.AGG_PACKET_COUNT} non ip packets'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.AGG_PACKET_COUNT)
        with allure.step('Validating WJH L3 aggregated table output'):
            do_agg_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.FORWARDING,
                        channel_type=WJHConsts.AGG_CHANNEL,
                        interface=interfaces.dut_ha_2, dst_ip=dst_ip, src_ip=src_ip, proto=proto,
                        drop_reason=drop_reason_message, dst_mac=dst_mac, src_mac=src_mac,
                        command=generate_wjh_poll_cmd(WJHConsts.FORWARDING, aggregate=True))
    except Exception as e:
        pytest.fail(f"Could not finish the test due to exception: \n{str(e)}.\nAborting!.")


@pytest.mark.wjh
@allure.title('WJH ACL test case')
def test_acl_ingress_router(engines, cli_objects, topology_obj, interfaces):
    src_mac = WJHConsts.TESTED_SRC_MAC
    dst_mac = cli_objects.dut.mac.get_mac_address_for_interface(interfaces.dut_hb_2)
    src_ip = utils.get_drop_src_ip_from_ingress_acl_table(topology_obj.players['dut']['cli'])
    dst_ip = WJHConsts.TESTED_DST_IP
    pkt = WJHConsts.TCP_IPV4_PACKET_FORMAT.format(SRC_MAC=src_mac, DST_MAC=dst_mac, SRC_IP=src_ip, DST_IP=dst_ip)
    drop_reason_message = acl_drop_reason_dict["ingress_router_acl"]
    try:
        with allure.step('Sending a packet when acl is configured to drop it'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.RAW_PACKET_COUNT)
        with allure.step('Validating WJH ACL raw table output'):
            do_acl_buffer_raw_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.ACL,
                                   channel_types=[WJHConsts.RAW_CHANNEL, WJHConsts.RAW_ACL_TABLE],
                                   interface=interfaces.dut_ha_2,
                                   dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                                   drop_reason_message=drop_reason_message,
                                   dst_mac=dst_mac, src_mac=src_mac, command=generate_wjh_poll_cmd(WJHConsts.ACL),
                                   table_separator=WJHConsts.ACL_TABLE_SEPARATOR)
        with allure.step(f'Sending {WJHConsts.AGG_PACKET_COUNT} packets when acl is configured to drop them'):
            scapy_packet_validation(interfaces, pkt, topology_obj, WJHConsts.AGG_PACKET_COUNT)
        with allure.step('Validating WJH ACL aggregated table output'):
            do_acl_buffer_agg_test(engines=engines, cli_object=cli_objects.dut, channel=WJHConsts.ACL,
                                   channel_types=[WJHConsts.AGG_TABLE, WJHConsts.AGG_ACL_TABLE],
                                   interface=interfaces.dut_ha_2,
                                   dst_ip=dst_ip, src_ip=src_ip, proto=WJHConsts.TCP_PROTO,
                                   drop_reason_message=drop_reason_message,
                                   dst_mac=dst_mac, src_mac=src_mac,
                                   command=generate_wjh_poll_cmd(WJHConsts.ACL, aggregate=True),
                                   table_separator=WJHConsts.ACL_TABLE_SEPARATOR)
    except Exception as e:
        pytest.fail(f"Could not finish the test due to exception: \n{str(e)}.\nAborting!.")
