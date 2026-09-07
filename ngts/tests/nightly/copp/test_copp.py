import allure
import logging
import pytest
import json
import time
import os
import re
import random
import copy
import codecs

from retry.api import retry_call

from abc import abstractmethod
from devts.infra.tools.validations.traffic_validations.scapy.scapy_runner import ScapyChecker
from ngts.common.checkers import verify_deviation, verify_deviation_for_simx
from .conftest import pause_lldp_before_copp_test, pause_lldp_after_reboot


logger = logging.getLogger()

# config file constants
CONFIG_DB_COPP_CONFIG_NAME = 'copp_cfg.json'
UPDATED_FILE_PATH = '/tmp/' + CONFIG_DB_COPP_CONFIG_NAME
CONFIG_DB_COPP_CONFIG_REMOTE = '/etc/sonic/' + CONFIG_DB_COPP_CONFIG_NAME
COPP_TRAP = 'COPP_TRAP'
COPP_GROUP = 'COPP_GROUP'
DEFAULT_TRAP_GROUP = 'queue4_group2'
TRAP_GROUP = 'trap_group'
TRAP_IDS = 'trap_ids'
SHORT_INTERVAL = 'short'
LONG_INTERVAL = 'long'
GROUPS_WITH_MINIMAL_CONFIG = ['queue4_group1', 'queue4_group3']
LEGAL_TRAP_ID_TO_ADD = 'l3_mtu_error'
COPP_ALWAYS_ENABLED = 'always_enabled'
COPP_ALWAYS_ENABLED_TRUE = 'true'

RATE_TRAFFIC_MULTIPLIER = 4
BURST_TRAFFIC_MULTIPLIER = 30
RATE_TRAFFIC_DURATION = 10
BURST_TRAFFIC_DURATION = 0.06
SIMX_DEFAULT_CIR = 0
SIMX_SENT_RATE_VALUE = 25

PROTOCOLS_IN_COPP_TRAP = ['bgp', 'lacp', 'arp', 'lldp', 'dhcp_relay', 'ip2me']
PROTOCOLS_WIRH_FEW_TRAP_IDS = ['bgp', 'arp', 'dhcp_relay']


# list of tested protocols
PROTOCOLS_LIST = ["ARP", "IP2ME", "SNMP", "SSH", "LLDP", "LACP", "BGP", "DHCP"]
protocol_for_reboot_flow = random.choice(PROTOCOLS_LIST)
logger.info(f"Random protocol for testing reboot flow is: {protocol_for_reboot_flow}\n")
PROTOCOLS_REBOOT_LIST = []
for protocol in PROTOCOLS_LIST:
    if protocol == protocol_for_reboot_flow:
        PROTOCOLS_REBOOT_LIST.append(f"{protocol}_Reboot")
    else:
        PROTOCOLS_REBOOT_LIST.append(protocol)


@pytest.mark.reboot_reload
@pytest.mark.disable_loganalyzer
@allure.title('CoPP Policer test case')
@pytest.mark.parametrize("protocol", PROTOCOLS_REBOOT_LIST)
def test_copp_policer(topology_obj, protocol, platform_params,
                      sonic_version, is_simx, is_trap_counters_supported, cli_objects, pause_lldp_before_copp_test):
    """
    Run CoPP Policer test case, which will check that the policer enforces the rate limit for protocols.
    The test flow:
    1. set interval value for flow counters trap
    2. validate BURST traffic with default limit values
    2.1 update copp_cfg.json file with tested limits (CBS and CIR)
    2.2 create validation (traffic according to tested protocol)
    2.3 send traffic
    2.4 validate results
    2.4.1 check the CPU counters increased as expected (used ifconfig)
    2.4.2 check the flow counters, compared to ifconfig counters. Must be changed according to interval
    3. validate RATE traffic with default limit values(same sub-steps as 2, but with other CBS, CIR and expected values)
    4. validate BURST OR RATE traffic with user limit values BEFORE reboot
    5. validate BURST AND RATE traffic with user limit values AFTER reboot
    6. validate BURST traffic with default limit values
    7. validate RATE traffic with default limit values
    8. validate interop
    8.1 remove trap_id from trap_ids of protocol in copp_cfg.json file
    8.2 check in flow counters show that the trap was removed
    8.3 add trap_id to trap_ids of protocol in copp_cfg.json file
    8.4 check in flow counters show that the trap was added
    8.5 restore trap_ids of protocol in copp_cfg.json file
    8.6 check in flow counters show that the traps was restored to default
    :param topology_obj: topology object fixture
    :param protocol: tested protocol name
    :param platform_params: platform parameters
    :param sonic_version: sonic version
    :return: None, raise error in case of unexpected result
    """
    try:
        # CIR (committed information rate) - bandwidth limit set by the policer
        # CBS (committed burst size) - largest burst of packets allowed by the policer
        reboot = "Reboot" in protocol
        protocol = protocol.replace("_Reboot", "")
        platform = platform_params.filtered_platform
        tested_protocol_obj = eval(protocol + 'Test' + '(topology_obj, sonic_version, is_simx, platform)')
        tested_protocol_obj.is_trap_counters_supported = is_trap_counters_supported
        if is_simx:
            tested_protocol_obj.copp_simx_test_runner(reboot)
        else:
            tested_protocol_obj.copp_test_runner(reboot)

    except Exception as err:
        raise AssertionError(err)

# -------------------------------------------------------------------------------


class CoppBase:
    """
    Base CoPP class
    """

    def __init__(self, topology_obj, sonic_version, is_simx, platform):
        self.topology = topology_obj
        self.sonic_version = sonic_version
        self.is_simx = is_simx
        self.chip_type = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific']['chip_type']
        self.sender = 'ha'
        self.host_iface = topology_obj.ports['ha-dut-1']
        self.dut_iface = topology_obj.ports['dut-ha-1']
        self.dut_engine = topology_obj.players['dut']['engine']
        self.host_engine = topology_obj.players['ha']['engine']
        self.dut_cli_object = topology_obj.players['dut']['cli']
        self.host_cli_object = topology_obj.players['ha']['cli']
        self.src_mac = self.host_cli_object.mac.get_mac_address_for_interface(topology_obj.ports['ha-dut-1'])
        self.dst_mac = self.dut_cli_object.mac.get_mac_address_for_interface(topology_obj.ports['dut-ha-1'])
        self.validation = None
        self.pre_validation = None
        self.traffic_duration = None
        self.pre_rx_counts = self.dut_cli_object.ifconfig.get_interface_ifconfig_details(self.dut_iface).rx_packets
        self.tested_protocol = self.get_tested_protocol_name()
        self.post_rx_counts = None
        self.default_cir = None
        self.default_simx_cir = 1
        self.default_cbs = None
        self.low_limit = 150
        self.user_limit = None
        self.trap_name = None
        self.is_trap_counters_supported = None
        self.short_interval = 1001
        self.long_interval = 30000
        self.interval_type = None
        self.init_trap_names = None
        self.removed_trap_ids = None
        self.flowcnt_deviation = 0.05 if self.is_simx else 0.01
        self.canonical_threshold = 0.25
        self.simx_threshold = 10
        self.used_threshold = self.simx_threshold if self.is_simx else self.canonical_threshold
        self.platform = platform

# -------------------------------------------------------------------------------

    def copp_test_runner(self, reboot_flow):
        """
        Test runner, defines general logic of the test case.
        Note - To validate burst specific traffic type, need to set low rate value in this traffic type.
        :param reboot_flow: True if protocol should run with reboot flow, else False
        :return: None, raise error in case of unexpected result
        """
        # check default burst and rate value
        if self.is_trap_counters_supported:
            self.init_trap_names = list(self.dut_cli_object.flowcnt.parse_trap_stats().keys())
            with allure.step('Set short trap interval'):
                self.set_counters_short_trap_interval()
        with allure.step('Check functionality of default burst limit'):
            self.run_validation_flow(self.default_cbs, self.low_limit, 'burst')
        with allure.step('Check functionality of default rate limit'):
            self.run_validation_flow(self.default_cbs, self.default_cir)

        # check non default burst and rate limit value with reboot
        if self.is_trap_counters_supported:
            with allure.step('Set long trap interval'):
                self.set_counters_long_trap_interval()
        if reboot_flow:
            self.run_validation_flow_with_reboot()
        else:
            with allure.step('Check functionality of configured burst limit'):
                self.run_validation_flow(self.user_limit, self.low_limit, 'burst')
            with allure.step('Check functionality of configured rate limit'):
                self.run_validation_flow(self.default_cbs, self.user_limit)

        # check restored default burst and rate value
        if self.is_trap_counters_supported:
            with allure.step('Set short trap interval'):
                self.set_counters_short_trap_interval()
        with allure.step('Check functionality of restored to default burst limit'):
            self.run_validation_flow(self.default_cbs, self.low_limit, 'burst')
        with allure.step('Check functionality of restored to default rate limit'):
            self.run_validation_flow(self.default_cbs, self.default_cir)

        if self.is_trap_counters_supported:
            with allure.step('Check interop between CoPP and flow counters'):
                self.run_interop_flow()

# -------------------------------------------------------------------------------

    def copp_simx_test_runner(self, reboot_flow):
        """
        Test runner for simx platform.
        Note - To validate pps traffic type only, as burst traffic is not supported on simx.
        :param reboot_flow: True if protocol should run with reboot flow, else False
        :return: None, raise error in case of unexpected result
        """
        # check default rate value
        if self.is_trap_counters_supported:
            self.init_trap_names = list(self.dut_cli_object.flowcnt.parse_trap_stats().keys())
            with allure.step('Set short trap interval'):
                self.set_counters_short_trap_interval()
        with allure.step('Check functionality of default rate limit'):
            self.run_validation_flow(self.default_cbs, self.default_simx_cir)

        # check non default rate limit value with reboot
        if self.is_trap_counters_supported:
            with allure.step('Set long trap interval'):
                self.set_counters_long_trap_interval()
        if reboot_flow:
            self.run_validation_flow_with_reboot_for_simx()
            with allure.step('Check functionality of default rate limit after reboot flow'):
                self.run_validation_flow(self.default_cbs, self.default_simx_cir)

        if self.is_trap_counters_supported:
            with allure.step('Check interop between CoPP and flow counters'):
                self.run_interop_flow()

# -------------------------------------------------------------------------------

    def run_validation_flow_with_reboot(self):
        """
        Runs validation flow logic with reboot.
        To save time and do not reboot for each traffic type,
        will be randomized primary validation, which will be checked specific traffic type before and after reboot,
        and secondary validation,which will be checked specific traffic type only after reboot
        :return: None, raise error in case of unexpected result
        """
        traffic_types = ['rate', 'burst']
        random.shuffle(traffic_types)
        primary_traffic_type, secondary_traffic_type = traffic_types
        if primary_traffic_type == 'rate':
            with allure.step('Check functionality of non default rate limit before reboot'):
                self.run_validation_flow(self.default_cbs, self.user_limit, 'rate')
            primary_validation_flow = "self.run_validation_flow(self.default_cbs, self.user_limit, 'rate', False)"
            secondary_validation_flow = "self.run_validation_flow(self.user_limit, self.low_limit, 'burst')"
        else:
            with allure.step('Check functionality of non default burst limit before reboot'):
                self.run_validation_flow(self.user_limit, self.low_limit, 'burst')
            primary_validation_flow = "self.run_validation_flow(self.user_limit, self.low_limit, 'burst', False)"
            secondary_validation_flow = "self.run_validation_flow(self.default_cbs, self.user_limit, 'rate')"

        logger.info('Reboot Switch')
        self.dut_cli_object.general.save_configuration()
        self.dut_cli_object.general.reboot_reload_flow(topology_obj=self.topology)
        pause_lldp_after_reboot(self.tested_protocol, self.platform, self.dut_cli_object)
        self.pre_rx_counts = self.dut_cli_object.ifconfig. \
            get_interface_ifconfig_details(self.dut_iface).rx_packets

        if self.is_trap_counters_supported:
            # restart the timer
            self.set_counters_short_trap_interval()
            time.sleep(self.long_interval / 1000)     # milliseconds to seconds
            self.set_counters_long_trap_interval()

        with allure.step(f'Check functionality of non default {primary_traffic_type} limit value after reboot'):
            eval(primary_validation_flow)
        with allure.step(f'Check functionality of non default {secondary_traffic_type} limit value after reboot'):
            eval(secondary_validation_flow)

# -------------------------------------------------------------------------------

    def run_validation_flow_with_reboot_for_simx(self):
        """
        Runs validation flow logic with reboot.
        To save time and do not reboot for each traffic type,
        will be randomized primary validation, which will be checked specific traffic type before and after reboot,
        and secondary validation,which will be checked specific traffic type only after reboot
        :return: None, raise error in case of unexpected result
        """
        logger.info('Reboot Switch')
        self.dut_cli_object.general.save_configuration()
        self.dut_cli_object.general.reboot_reload_flow(topology_obj=self.topology)
        self.pre_rx_counts = self.dut_cli_object.ifconfig. \
            get_interface_ifconfig_details(self.dut_iface).rx_packets

# -------------------------------------------------------------------------------

    def run_validation_flow(self, cbs_value, cir_value, traffic_type='rate', update_configs_request=True):
        """
        Runs validation flow logic
        :param cbs_value: burst limit value
        :param cir_value: rate limit value
        :param traffic_type: type of the traffic - rate/burst
        :param update_configs_request: the flag to update limit values in the config file
        :return: None, raise error in case of unexpected result
        """
        if update_configs_request:
            self.config_copp_json_file(cir_value=cir_value, cbs_value=cbs_value)

        retry_call(
            self.validate_traffic,
            fargs=[cbs_value, cir_value, traffic_type],
            tries=6,
            delay=4,
            logger=logger,
        )

# -------------------------------------------------------------------------------

    def run_interop_flow(self):
        """
        Runs logic of validation for interoperability between CoPP and Flow Counters Trap
        :return: None, raise error in case of unexpected result
        """
        protocol = self.get_tested_protocol_name()
        if protocol in PROTOCOLS_IN_COPP_TRAP:
            if protocol in PROTOCOLS_WIRH_FEW_TRAP_IDS:
                self.config_copp_json_file(trap_action='remove')
                self.verify_interop_trap_stats(list(set(self.init_trap_names) - set(self.removed_trap_ids)))

            self.config_copp_json_file(trap_action='add')
            self.verify_interop_trap_stats(self.init_trap_names + ['l3_mtu_error'])

            self.config_copp_json_file(trap_action='restore')
            self.verify_interop_trap_stats(self.init_trap_names)

# -------------------------------------------------------------------------------

    def validate_traffic(self, cbs_value, cir_value, traffic_type):
        if traffic_type == 'rate':
            logger.info('Tested traffic type is   RATE')
            self.create_rate_validation(cir_value)
            pps = cir_value
        else:
            logger.info('Tested traffic type is   BURST')
            self.create_burst_validation(cbs_value)
            pps = cbs_value
        if self.is_trap_counters_supported:
            self.dut_cli_object.flowcnt.clear_trap_counters()
        self.send_traffic()
        self.validate_results(pps)

# -------------------------------------------------------------------------------

    def get_tested_protocol_name(self):
        """
        Getting the name of tested protocol, based on class name
        :return: protocol name (Example: arp or snmp etc.)
        """
        protocol = type(self).__name__.replace('Test', '').lower()
        # TODO SNMP trapped as ip2me. Mellanox should add support for SNMP trap
        if protocol == 'snmp':
            protocol = 'ip2me'

        if protocol == 'dhcp':
            protocol = 'dhcp_relay'

        return protocol

# -------------------------------------------------------------------------------

    def create_burst_validation(self, cbs_value):
        """
        Creating burst validation, based on given CBS value
        :param cbs_value: CBS value
        """
        self.create_validation(pps=cbs_value * BURST_TRAFFIC_MULTIPLIER,
                               times=int(cbs_value * BURST_TRAFFIC_MULTIPLIER * BURST_TRAFFIC_DURATION))
        self.create_pre_validation()

# -------------------------------------------------------------------------------

    def create_rate_validation(self, cir_value):
        """
        Creating rate validation, based on given CIR value
        :param cir_value: CIR value
        """
        if cir_value == SIMX_DEFAULT_CIR:
            cir_value += SIMX_SENT_RATE_VALUE
        self.create_validation(pps=cir_value * RATE_TRAFFIC_MULTIPLIER,
                               times=cir_value * RATE_TRAFFIC_MULTIPLIER * RATE_TRAFFIC_DURATION)
        self.create_pre_validation()

# -------------------------------------------------------------------------------

    @abstractmethod
    def create_validation(self, pps, times):
        """
        This method is abstractmethod and should be implemented in child classes
        """
        pass

# -------------------------------------------------------------------------------

    def create_pre_validation(self):
        """
        Creating pre validation with 1 packet to send. Based on main validation
        """
        self.pre_validation = copy.deepcopy(self.validation)
        self.pre_validation['send_args']['loop'] = 1

# -------------------------------------------------------------------------------

    def send_traffic(self):
        """
        Sending Scapy traffic.
        Pre validation - to be sure main all validation will be received
        Validation - main traffic sends
        """
        logger.info('validation: {}'.format(str(self.validation)))
        with allure.step('Send pre traffic of 1 packet'):
            ScapyChecker(self.topology.players, self.pre_validation).run_validation()
            time.sleep(1)
        with allure.step('Send traffic'):
            start_time = time.time()
            ScapyChecker(self.topology.players, self.validation).run_validation()
            self.traffic_duration = time.time() - start_time
            time.sleep(1)

# -------------------------------------------------------------------------------

    def validate_results(self, expected_pps):
        """
        Verifying the result of received traffic
        :param expected_pps: expected packet rate
        :return: None, raise error in case of unexpected result
        """
        with allure.step('Validate results'):
            rx_ifconfig_count, rx_ifconfig_pps = self.get_ifconfig_results()
            self.verify_pps_deviation(rx_ifconfig_pps, expected_pps)

            if self.is_trap_counters_supported:
                self.validate_flowcnt_results(rx_ifconfig_count)

# -------------------------------------------------------------------------------

    def verify_pps_deviation(self, rx_ifconfig_pps, expected_pps):
        if self.is_simx:
            with allure.step(f"Verify that received ifconfig pps({rx_ifconfig_pps}) "
                             f"is lass than: {expected_pps} + {self.simx_threshold}"):
                verify_deviation_for_simx(rx_ifconfig_pps, self.simx_threshold)
        else:
            # We use +- 25% threshold due to not possible to be more precise
            with allure.step(f"Verify that received ifconfig pps({rx_ifconfig_pps}) "
                             f"is in allowed rate: {expected_pps} +-{self.used_threshold}%"):
                verify_deviation(rx_ifconfig_pps, expected_pps, self.used_threshold)

# -------------------------------------------------------------------------------

    def validate_flowcnt_results(self, rx_ifconfig_count):
        """
        Verifying the flow counters result of received traffic
        :param rx_ifconfig_count: received counter from ifconfig
        :return: None, raise error in case of unexpected result
        """
        rx_trap_count = self.get_flowcnt_trap_results()

        if self.interval_type == LONG_INTERVAL:
            try:
                with allure.step(f"Verify rx counters from ifconfig and "
                                 f"flow counters trap in allowed deviation +-{self.flowcnt_deviation * 100}%"):
                    verify_deviation(rx_trap_count, rx_ifconfig_count, self.flowcnt_deviation)
                    raise Exception("The flow counters updated before long interval time elapsed")
            except AssertionError as err:
                logger.info('As expected, the flowcnt not updated before interval time,'
                            ' wait this time {}ms and check again '.format(self.long_interval))
                time.sleep(int(self.long_interval) / 1000)  # interval in msec
                rx_trap_count = self.get_flowcnt_trap_results()
                with allure.step(f"Verify rx counters from ifconfig and "
                                 f"flow counters trap in allowed deviation +-{self.flowcnt_deviation * 100}%"):
                    verify_deviation(rx_trap_count, rx_ifconfig_count, self.flowcnt_deviation)
        else:
            with allure.step(f"Verify rx counters from ifconfig and "
                             f"flow counters trap in allowed deviation +-{self.flowcnt_deviation * 100}%"):
                verify_deviation(rx_trap_count, rx_ifconfig_count, self.flowcnt_deviation)

# -------------------------------------------------------------------------------

    def get_ifconfig_results(self):
        """
        Get results from ifconfig output.
        :return: received counter, calculated PPS
        """
        self.post_rx_counts = self.dut_cli_object.ifconfig.get_interface_ifconfig_details(self.dut_iface).rx_packets
        rx_count = int(self.post_rx_counts) - int(self.pre_rx_counts)
        self.pre_rx_counts = self.post_rx_counts

        logger.info('The traffic duration is {:10.4f} '.format(self.traffic_duration))
        self.traffic_duration = correct_traffic_duration_for_calculations(self.traffic_duration)
        rx_pps = int(rx_count / self.traffic_duration)
        logger.info('The ifconfig RX counters increased by {} '.format(rx_count))
        logger.info('The calculated ifconfig pps is {} '.format(rx_pps))
        return rx_count, rx_pps

# -------------------------------------------------------------------------------

    def get_flowcnt_trap_results(self):
        """
        Get received counter of trap
        :return: received counter
        """
        trap_stats = self.dut_cli_object.flowcnt.parse_trap_stats()
        rx_count = trap_stats[self.trap_name]['Packets'].replace(',', '')  # convert from 1,200 format
        logger.info('The flowcnt trap counter is {} '.format(rx_count))
        return rx_count

# -------------------------------------------------------------------------------

    def config_copp_json_file(self, cir_value=None, cbs_value=None, trap_action='add'):
        """
        Configuration of database via copp_cfg.json file with given CIR and CBS values or trap action
        :param cir_value: value of CIR
        :param cbs_value: value of CBS
        :param trap_action: action for trap_ids value
        :return:
        """
        # copy file from switch to local system ( will be copied to current location ".")
        self.copy_remote_file(CONFIG_DB_COPP_CONFIG_REMOTE, CONFIG_DB_COPP_CONFIG_NAME, '/', 'get')

        # update the limits in json file
        if cir_value is not None and cbs_value is not None:
            logger.info('Load new CoPP configuration with CIR: {} and CBS: {}'.format(cir_value, cbs_value))
            update_limits_copp_json_file(self.get_tested_protocol_name(), cir_value, cbs_value, self.trap_name)
        else:
            logger.info('Load new CoPP configurations for protocol {} with action {} '
                        .format(self.get_tested_protocol_name(), trap_action))
            self.removed_trap_ids = update_protocol_trap_ids_copp_json_file(self.get_tested_protocol_name(),
                                                                            trap_action)

        # copy file back to switch
        self.copy_remote_file(CONFIG_DB_COPP_CONFIG_NAME, CONFIG_DB_COPP_CONFIG_NAME, '/tmp')

        # remove local file
        os.remove(CONFIG_DB_COPP_CONFIG_NAME)

        # apply updated config file
        self.dut_cli_object.general.load_configuration(UPDATED_FILE_PATH)

# -------------------------------------------------------------------------------

    def copy_remote_file(self, src, dst, file_system, direction='put'):
        """
        Copying the file TO / FROM tested switch
        :param src: path to the source file
        :param dst: destination file name
        :param file_system: location of destination file
        :param direction: the direction of the copy
        :return: None, raise error in case of unexpected result
        """
        self.dut_engine.copy_file(source_file=src,
                                  dest_file=dst,
                                  file_system=file_system,
                                  direction=direction,
                                  overwrite_file=True,
                                  verify_file=False)

# -------------------------------------------------------------------------------

    def set_counters_short_trap_interval(self):
        self.change_flowcnt_trap_interval(self.short_interval)
        self.interval_type = SHORT_INTERVAL

# -------------------------------------------------------------------------------

    def set_counters_long_trap_interval(self):
        self.change_flowcnt_trap_interval(self.long_interval)
        self.interval_type = LONG_INTERVAL

# -------------------------------------------------------------------------------

    def change_flowcnt_trap_interval(self, interval):
        """
        Set and validate trap interval
        :param interval: interval value
        :return: None, raise error in case of unexpected result
        """
        self.dut_cli_object.counterpoll.set_trap_interval(interval)
        self.verify_flowcnt_trap_interval(interval)

# -------------------------------------------------------------------------------

    def verify_flowcnt_trap_interval(self, interval, status='enable'):
        """
        Validate the flow counters trap with interval and status
        :param interval: expected interval value
        :param status: expected status value
        :return: None, raise error in case of unexpected result
        """
        parsed_output = self.dut_cli_object.counterpoll.parse_counterpoll_show()
        assert str(interval) == parsed_output['FLOW_CNT_TRAP_STAT']['Interval (in ms)']
        assert status == parsed_output['FLOW_CNT_TRAP_STAT']['Status']

# -------------------------------------------------------------------------------

    def verify_interop_trap_stats(self, expected_trap_names):
        """
        Validate the traps are as expected
        :param expected_trap_names: expected traps list
        :return: None, raise error in case of unexpected result
        """
        trap_stats = self.dut_cli_object.flowcnt.parse_trap_stats()
        delta = list(set(trap_stats.keys()) ^ set(expected_trap_names))
        if delta:
            raise Exception('The list of TRAP NAMEs: {} \nis not as expected: {}'
                            .format(trap_stats.keys(), expected_trap_names))

# -------------------------------------------------------------------------------


class ARPTest(CoppBase):
    """
    ARP class/test extends the basic CoPP class with specific validation for ARP protocol
    """

    def __init__(self, topology_obj, sonic_version, is_simx, platform):
        CoppBase.__init__(self, topology_obj, sonic_version, is_simx, platform)
        self.default_cir = 600
        self.default_cbs = 600
        self.user_limit = 1000
        self.dst_mac = 'ff:ff:ff:ff:ff:ff'

# -------------------------------------------------------------------------------

    def create_validation(self, pps, times):
        """
        Creating Scapy validation for ARP protocol. One legal packet from list.
        :param pps: packets rate value
        :param times: packets number
        """
        packet_index = 0
        trap_name_index = 1
        arp_dict = {'ARP Request': ['ARP(op=1, psrc="192.168.1.1", pdst="192.168.2.2")', 'arp_req'],
                    'ARP Reply': ['ARP(op=2, psrc="192.168.1.1", pdst="192.168.2.2")', 'arp_resp'],
                    'Neighbor Solicitation': ['IPv6(src="2001:db8:5::5",dst="ff02::1")/ICMPv6ND_NS()',
                                              'neigh_discovery'],
                    'Neighbor Advertisement': ['IPv6(src="2001:db8:5::5",dst="ff02::1")/ICMPv6ND_NA()',
                                               'neigh_discovery']}
        chosen_packet = random.choice(list(arp_dict.keys()))
        self.trap_name = arp_dict[chosen_packet][trap_name_index]

        with allure.step('ARP - Create "{}" validation'.format(chosen_packet)):
            arp_pkt = 'Ether(src="{}", dst="{}")/' + arp_dict[chosen_packet][packet_index]
            self.validation = {'sender': self.sender,
                               'send_args': {'interface': self.host_iface,
                                             'send_method': 'sendpfast',
                                             'packets': arp_pkt.format(self.src_mac, self.dst_mac),
                                             'pps': pps,
                                             'loop': times,
                                             'timeout': 20}
                               }

# -------------------------------------------------------------------------------


class SNMPTest(CoppBase):
    """
    SNMP class/test extends the basic CoPP class with specific validation for SNMP protocol
    """

    def __init__(self, topology_obj, sonic_version, is_simx, platform):
        CoppBase.__init__(self, topology_obj, sonic_version, is_simx, platform)
        # TODO trapped as ip2me. Mellanox should add support for SNMP trap. update values accordingly
        self.default_cir = 6000
        self.default_cbs = 1000
        self.user_limit = 600
        self.trap_name = 'ip2me'
        logger.info("The tested protocol SNMP have too big default value for burst, "
                    "can't be tested on canonical systems. "
                    "Will be tested the value {} instead"
                    .format(self.default_cbs))

# -------------------------------------------------------------------------------

    def create_validation(self, pps, times):
        """
        Creating Scapy validation for SNMP protocol. One legal packet from list.
        :param pps: packets rate value
        :param times: packets number
        """
        snmp_dict = {'SNMP get': 'SNMP(community="public",'
                                 'PDU=SNMPget(varbindlist=[SNMPvarbind(oid=ASN1_OID("1.3.6.1.2.1.1.1.0"))]))',
                     'SNMP set': 'SNMP(community="private",'
                                 'PDU=SNMPset(varbindlist='
                                 '[SNMPvarbind(oid=ASN1_OID("1.3.6.1.4.1.9.2.1.55.192.168.2.100"),'
                                 'value="192.168.2.150.config")]))'
                     }
        chosen_packet = random.choice(list(snmp_dict.keys()))

        with allure.step('SNMP - Create "{}" validation'.format(chosen_packet)):
            snmp_pkt = 'Ether(src="{}", dst="{}")/IP(dst="192.168.1.1")/UDP(sport=161)/' + snmp_dict[chosen_packet]
            self.validation = {'sender': self.sender,
                               'send_args': {'interface': self.host_iface,
                                             'send_method': 'sendpfast',
                                             'packets': snmp_pkt.format(self.src_mac, self.dst_mac),
                                             'pps': pps,
                                             'loop': times,
                                             'timeout': 20}
                               }

# -------------------------------------------------------------------------------


class IP2METest(CoppBase):
    """
    IP2ME class/test extends the basic CoPP class with specific validation for IP2ME packets type
    """

    def __init__(self, topology_obj, sonic_version, is_simx, platform):
        CoppBase.__init__(self, topology_obj, sonic_version, is_simx, platform)
        self.default_cir = 6000
        self.default_cbs = 1000
        self.user_limit = 600
        self.trap_name = 'ip2me'
        logger.info("The tested protocol IP2ME have too big default value for burst, "
                    "can't be tested on canonical systems. "
                    "Will be tested the value {} instead"
                    .format(self.default_cbs))

# -------------------------------------------------------------------------------

    def create_validation(self, pps, times):
        """
        Creating Scapy validation for IP2ME packets. Simple IP packet.
        :param pps: packets rate value
        :param times: packets number
        """
        with allure.step('IP2ME - Create validation (with simple IP packet and right destination ip)'):
            ip2me_pkt = 'Ether(src="{}", dst="{}")/IP(dst="192.168.1.1")'
            self.validation = {'sender': self.sender,
                               'send_args': {'interface': self.host_iface,
                                             'send_method': 'sendpfast',
                                             'packets': ip2me_pkt.format(self.src_mac, self.dst_mac),
                                             'pps': pps,
                                             'loop': times,
                                             'timeout': 20}
                               }

# -------------------------------------------------------------------------------


class SSHTest(CoppBase):
    """
    SSH class/test extends the basic CoPP class with specific validation for SSH packet type
    """

    def __init__(self, topology_obj, sonic_version, is_simx, platform):
        CoppBase.__init__(self, topology_obj, sonic_version, is_simx, platform)
        self.default_cir = 600
        self.default_cbs = 600
        self.user_limit = 1000
        self.trap_name = 'ssh'

# -------------------------------------------------------------------------------

    def create_validation(self, pps, times):
        """
        Creating Scapy validation for SSH packets. Simple TCP packet with specific source and destination ports.
        :param pps: packets rate value
        :param times: packets number
        """
        with allure.step('SSH - Create validation (with simple TCP packet and destination port 22)'):
            ssh_pkt = 'Ether(dst="{}")/IP(dst="192.168.1.1", src="192.168.1.2")/TCP(dport=22, sport=22)'
            self.validation = {'sender': self.sender,
                               'send_args': {'interface': self.host_iface,
                                             'send_method': 'sendpfast',
                                             'packets': ssh_pkt.format(self.dst_mac),
                                             'pps': pps,
                                             'loop': times,
                                             'timeout': 20}
                               }

# -------------------------------------------------------------------------------


class LLDPTest(CoppBase):
    """
    LLDP class/test extends the basic CoPP class with specific validation for LLDP packet type
    """

    def __init__(self, topology_obj, sonic_version, is_simx, platform):
        CoppBase.__init__(self, topology_obj, sonic_version, is_simx, platform)
        self.default_cir = 600
        self.default_cbs = 600
        self.user_limit = 1000
        # TODO: need to update self.default_simx_cir - was not possible before because #2682609
        # noise from origin lldp traffic. if disable LLDP, the traffic will not be moved to cpu/counters
        self.flowcnt_deviation = 0.15
        self.trap_name = 'lldp'

# -------------------------------------------------------------------------------

    def create_validation(self, pps, times):
        """
        Creating Scapy validation for LLDP packets. Simple ETH packet with specific destination mac and type.
        :param pps: packets rate value
        :param times: packets number
        """
        with allure.step('LLDP - Create validation (simple ETH packet with fixed d_mac and type)'):
            lldp_pkt = 'Ether(dst="01:80:c2:00:00:0e", src="{}", type=0x88cc)'
            self.validation = {'sender': self.sender,
                               'send_args': {'interface': self.host_iface,
                                             'send_method': 'sendpfast',
                                             'packets': lldp_pkt.format(self.src_mac),
                                             'pps': pps,
                                             'loop': times,
                                             'timeout': 20}
                               }

# -------------------------------------------------------------------------------


class LACPTest(CoppBase):
    """
    LACP class/test extends the basic CoPP class with specific validation for LACP packet type
    """

    def __init__(self, topology_obj, sonic_version, is_simx, platform):
        CoppBase.__init__(self, topology_obj, sonic_version, is_simx, platform)
        self.default_cir = 600
        self.default_cbs = 600
        self.user_limit = 1000
        self.trap_name = 'lacp'

# -------------------------------------------------------------------------------

    def create_validation(self, pps, times):
        """
        Creating Scapy validation for LACP packets. Simple ETH packet with specific destination mac and type.
        :param pps: packets rate value
        :param times: packets number
        """
        with allure.step('LACP - Create validation (simple ETH packet with fixed dst_mac and type)'):
            lacp_pkt = 'Ether(dst="01:80:c2:00:00:02", src="{}", type=0x8809)/(chr(0x01)*50)'
            self.validation = {'sender': self.sender,
                               'send_args': {'interface': self.host_iface,
                                             'send_method': 'sendpfast',
                                             'packets': lacp_pkt.format(self.src_mac),
                                             'pps': pps,
                                             'loop': times,
                                             'timeout': 20}
                               }

# -------------------------------------------------------------------------------


class BGPTest(CoppBase):
    """
    BGP class/test extends the basic CoPP class with specific validation for BGP packet type
    """

    def __init__(self, topology_obj, sonic_version, is_simx, platform):
        CoppBase.__init__(self, topology_obj, sonic_version, is_simx, platform)
        self.default_cir = 600
        self.default_cbs = 600
        self.user_limit = 1000

# -------------------------------------------------------------------------------

    def create_validation(self, pps, times):
        """
        Creating Scapy validation for BGP packets. Simple TCP packet with specific TCP destination port.
        :param pps: packets rate value
        :param times: packets number
        """
        packet_index = 0
        trap_name_index = 1
        bgp_dict = {
            'bgp': ['IP(dst="192.168.1.1")', 'bgp'],
            'bgpv6': ['IPv6(dst="2001:db8:5::1")', 'bgpv6']
        }
        chosen_packet = random.choice(list(bgp_dict.keys()))
        self.trap_name = bgp_dict[chosen_packet][trap_name_index]
        with allure.step('LLDP - Create validation (simple TCP packet with fixed TCP dst port)'):
            bgp_pkt = 'Ether(dst="{}")/' + bgp_dict[chosen_packet][packet_index] + '/TCP(dport=179)'
            self.validation = {'sender': self.sender,
                               'send_args': {'interface': self.host_iface,
                                             'send_method': 'sendpfast',
                                             'packets': bgp_pkt.format(self.dst_mac),
                                             'pps': pps,
                                             'loop': times,
                                             'timeout': 20}
                               }

# -------------------------------------------------------------------------------


class DHCPTest(CoppBase):
    """
    DHCP class/test extends the basic CoPP class with specific validation for DHCP packet type
    """

    def __init__(self, topology_obj, sonic_version, is_simx, platform):
        CoppBase.__init__(self, topology_obj, sonic_version, is_simx, platform)
        self.default_cir = 600
        self.default_cbs = 600
        self.user_limit = 1000

# -------------------------------------------------------------------------------

    def create_validation(self, pps, times):
        """
        Creating Scapy validation for DHCP packets. UDP packet with bootp and dhcp layers.
        :param pps: packets rate value
        :param times: packets number
        """
        packet_index = 0
        trap_name_index = 1

        decode_hex = codecs.getdecoder("hex_codec")
        localmacraw = decode_hex(self.src_mac.replace(':', ''))[0]

        dhcp_dict = {
            'dhcpv4_pkt': ['Ether(dst="ff:ff:ff:ff:ff:ff", type=0x800)/IP(dst="255.255.255.255", src="0.0.0.0")/'
                           'UDP(dport=67, sport=68)/BOOTP(chaddr={}, ciaddr="0.0.0.0")/'
                           'DHCP(options=[("message-type", "discover"),"end"])'.format(localmacraw), 'dhcp'],
            'dhcpv6_pkt': ['Ether(dst="{}")/IPv6(src="2001:db8:5::2", dst="2001:db8:5::1")/'
                           'UDP(sport=547, dport=546)/DHCP6_Request(trid=0)'.format(self.dst_mac), 'dhcpv6']
        }

        chosen_packet = random.choice(list(dhcp_dict.keys()))
        self.trap_name = dhcp_dict[chosen_packet][trap_name_index]

        with allure.step('DHCP - Create validation (with DHCP discover packet)'):
            self.validation = {'sender': self.sender,
                               'send_args': {'interface': self.host_iface,
                                             'send_method': 'sendpfast',
                                             'packets': dhcp_dict[chosen_packet][packet_index],
                                             'pps': pps,
                                             'loop': times,
                                             'timeout': 20}
                               }


def update_limits_copp_json_file(protocol, cir_value, cbs_value, trap_ids):
    """
    This function updates the local copp.json configuration file with given CIR and CBS value for given protocol
    :param protocol: protocol name
    :param cir_value: value of CIR
    :param cbs_value: value of CBS
    :param trap_ids: traps_ids value of the protocol.
                    Used if  the protocol doesn't exist in default copp_cfg.json file.
    :return:
    """
    with open(CONFIG_DB_COPP_CONFIG_NAME) as copp_json_file:
        copp_json_file_dic = json.load(copp_json_file)
    trap_group = get_trap_group(protocol, copp_json_file_dic, trap_ids)
    if trap_group in GROUPS_WITH_MINIMAL_CONFIG:
        update_group_params(copp_json_file_dic, trap_group)
    update_limit_values(copp_json_file_dic, trap_group, cir_value, cbs_value)
    os.remove(CONFIG_DB_COPP_CONFIG_NAME)
    with open(CONFIG_DB_COPP_CONFIG_NAME, 'w') as copp_json_file:
        json.dump(copp_json_file_dic, copp_json_file, indent=4)

# -------------------------------------------------------------------------------


def update_protocol_trap_ids_copp_json_file(protocol, action):
    """
    This function updates the local copp.json configuration file with given CIR and CBS value for given protocol
    :param protocol: protocol name
    :param action: update action
    :return: removed trap ids for given protocol(can be empty list)
    """
    with open(CONFIG_DB_COPP_CONFIG_NAME) as copp_json_file:
        copp_json_file_dic = json.load(copp_json_file)
    removed_trap_ids = update_trap_id_values(copp_json_file_dic, protocol, action)
    logger.info(f"Removed trap ids for {protocol} are: {removed_trap_ids}")
    os.remove(CONFIG_DB_COPP_CONFIG_NAME)
    with open(CONFIG_DB_COPP_CONFIG_NAME, 'w') as copp_json_file:
        json.dump(copp_json_file_dic, copp_json_file, indent=4)
    return removed_trap_ids

# -------------------------------------------------------------------------------


def get_trap_group(protocol, copp_dict, trap_ids=''):
    """
    Getting the trap group by give protocol name.
    If this protocol not into the config dictionary(copp_cfg.json) file,
        add new key-value tuple and the trap_group will be default.
    :param protocol: protocol name
    :param copp_dict: config dictionary
    :param trap_ids: trap_ids of the protocol
    :return: trap_group
    For example the part of config dictionary:
        "COPP_TRAP": {
            "bgp": {
                "trap_ids": "bgp,bgpv6",
                "trap_group": "queue4_group1"
            },
            "arp": {
                "trap_ids": "arp_req,arp_resp,neigh_discovery",
                "trap_group": "queue4_group2"
            }
        }
    """
    if protocol in copp_dict[COPP_TRAP]:
        logger.info('The protocol {} exist under COPP_TRAP dictionary in copp_cfg.json file'.format(protocol))
        return copp_dict[COPP_TRAP][protocol][TRAP_GROUP]
    else:
        add_new_protocol_to_config(protocol, copp_dict, trap_ids)
    return DEFAULT_TRAP_GROUP

# -------------------------------------------------------------------------------


def add_new_protocol_to_config(protocol, copp_dict, trap_ids):
    """
    Add new protocol to config dictionary
    :param protocol: protocol name
    :param copp_dict: config dictionary
    :param trap_ids: traps ids
    """
    copp_dict[COPP_TRAP].update({protocol: {TRAP_IDS: trap_ids,
                                            TRAP_GROUP: DEFAULT_TRAP_GROUP,
                                            COPP_ALWAYS_ENABLED: COPP_ALWAYS_ENABLED_TRUE}})

# -------------------------------------------------------------------------------


def update_limit_values(copp_dict, trap_group, cir_value, cbs_value):
    """
    Update the CIR and CBS values for given trap group.
    :param copp_dict: config dictionary
    :param trap_group: trap group
    :param cir_value: value of CIR
    :param cbs_value: value of CBS
    :return:
    """
    if 'cir' in copp_dict[COPP_GROUP][trap_group]:
        copp_dict[COPP_GROUP][trap_group]['cir'] = cir_value
    else:
        copp_dict[COPP_GROUP][trap_group].update({'cir': cir_value})

    if 'cbs' in copp_dict[COPP_GROUP][trap_group]:
        copp_dict[COPP_GROUP][trap_group]['cbs'] = cbs_value
    else:
        copp_dict[COPP_GROUP][trap_group].update({'cbs': cbs_value})

# -------------------------------------------------------------------------------


def update_group_params(copp_dict, trap_group):
    """
    Updates the group with required parameters to use non default values
    :param copp_dict: config dictionary
    :param trap_group: trap group
    :return:
    """
    # there is no point in CIR and CBS values, it will change later
    copp_dict[COPP_GROUP][trap_group].update({"meter_type": "packets",
                                              "mode": "sr_tcm",
                                              "cir": "400",
                                              "cbs": "400",
                                              "red_action": "drop"})

# -------------------------------------------------------------------------------


def update_trap_id_values(copp_dict, protocol, action):
    """
    Update the CIR and CBS values for given trap group.
    :param copp_dict: config dictionary
    :param protocol: protocol to change
    :param action: update action
    :return:
    """
    removed_trap_ids = []
    default_trap_ids = copp_dict[COPP_TRAP][protocol]['trap_ids']
    logger.info(f"Default trap ids for {protocol} are: {default_trap_ids}")

    if action == 'add':
        new_trap_ids = default_trap_ids + ',' + LEGAL_TRAP_ID_TO_ADD
    elif action == 'remove':
        current_trap_ids_list = default_trap_ids.split(',')
        new_trap_ids = current_trap_ids_list.pop(random.randrange(len(current_trap_ids_list)))
        removed_trap_ids = current_trap_ids_list  # after pop
        logger.info(f"Removed trap ids for {protocol} are: {removed_trap_ids}")
    else:
        new_trap_ids = default_trap_ids  # restore to default

    copp_dict[COPP_TRAP][protocol]['trap_ids'] = new_trap_ids
    logger.info(f"Updated trap ids for {protocol} are: {new_trap_ids}")
    return removed_trap_ids

# -------------------------------------------------------------------------------


def correct_traffic_duration_for_calculations(current_traffic_duration):
    """
    This function correct the traffic duration time from some network/scapy delays.
    :param current_traffic_duration: current traffic duration time
    :return: traffic duration time after correction
    """
    if current_traffic_duration >= 10:
        return rate_traffic_duration_time_correction(current_traffic_duration)
    else:
        return burst_traffic_duration_time_correction()

# -------------------------------------------------------------------------------


def rate_traffic_duration_time_correction(current_traffic_duration):
    """
    The rate traffic time is 10 seconds. So the traffic duration time can't be bigger then 11 seconds
    :param current_traffic_duration: current traffic duration time
    :return: traffic duration time after correction
    """
    max_rate_traffic_duration = 11
    return min(current_traffic_duration, max_rate_traffic_duration)

# -------------------------------------------------------------------------------


def burst_traffic_duration_time_correction():
    """
    The burst traffic time is <0.1 second. So for calculation burst traffic will be returned value 1
    :return: traffic duration time after correction
    """
    return 1
