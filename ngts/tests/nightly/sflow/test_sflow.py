import pytest
import logging
import random
from tests.common.plugins.allure_wrapper import allure_step_wrapper as allure
from ngts.config_templates.ip_config_template import IpConfigTemplate
from ngts.tests.conftest import get_dut_loopbacks
from ngts.cli_wrappers.common.mac_clis_common import MacCliCommon
from ngts.constants.constants import SflowConsts
from ngts.helpers.sflow_helper import verify_sflow_configuration, verify_sflow_sample_agent_id, \
    kill_sflowtool_process, remove_tmp_sample_file, verify_sflow_sample_polling_interval, verify_flow_sample_received, \
    verify_sflow_interface_configuration, get_agent_id_from_hsflowd, wait_until_hsflowd_ready
from tests.common.utilities import wait_until

logger = logging.getLogger()
allure.logger = logger

POLLING_INTERVAL_LIST = [SflowConsts.POLLING_INTERVAL_1, SflowConsts.POLLING_INTERVAL_0, SflowConsts.POLLING_INTERVAL_2]
SAMPLE_RATE_LIST = [SflowConsts.SAMPLE_RATE_2, SflowConsts.SAMPLE_RATE_3]
COLLECTOR_WARNING_CONTENT = "Only 2 collectors can be configured, please delete one"


@pytest.fixture(scope='module', autouse=False)
def pick_random_loopback(topology_obj, engines):
    """
    This method is used to pick a random loopback that is configured with highest speed available on the switch.
    :param topology_obj: topology_obj fixture
    :param engines: engines fixture
    :return: tuple of selected loopback ports.
    """
    loopback_list = get_dut_loopbacks(topology_obj, split=True)
    random.shuffle(loopback_list)
    for loopback in loopback_list:
        first_iface = loopback[0]
        dut_engine = engines.dut
        if is_max_speed(dut_engine, first_iface):
            return loopback


def is_max_speed(dut_engine, loopback_iface):
    """
    This method is used to check if the speed of the interface is the highest on the switch.
    :param dut_engine: engines fixture
    :param loopback_iface: selected loopback to be tested
    :return: True if the speed configured is the maximum on the switch
    """
    output = dut_engine.run_cmd("show int st | awk 'NR>1{{print $3,$1}}' | sort -nr | awk 'NR==1{{max=$1}} $1==max{{print $2}}' | grep {}"
                                .format(loopback_iface))
    #  filters the interfaces that has the highest speed on the switch and checks if the selected interface is included in the output
    return bool(output)


@pytest.fixture(scope='module', autouse=True)
def basic_l3_connectivity_configuration(topology_obj, interfaces, engines, pick_random_loopback):
    """
    Pytest fixture used to configure basic layer 3 connectivity configuration
    :param topology_obj: topology object fixture
    :param interfaces:  interfaces fixture
    :param engines: engines fixture
    :param pick_random_loopback: pick_random_loopback fixture
    """
    ip_config_dict = {
        'dut': [{'iface': interfaces.dut_ha_1, 'ips': [(SflowConsts.DUT_HA_1_IP, '24')]},
                {'iface': interfaces.dut_ha_2, 'ips': [(SflowConsts.DUT_HA_2_IP, '24')]},
                {'iface': interfaces.dut_hb_1, 'ips': [(SflowConsts.DUT_HB_1_IP, '24')]},
                {'iface': interfaces.dut_hb_2, 'ips': [(SflowConsts.DUT_HB_2_IP, '24')]},
                {'iface': interfaces.dut_hb_2, 'ips': [(SflowConsts.DUT_HB_2_IP_V6, '64')]},
                {'iface': SflowConsts.LOOPBACK_0, 'ips': [(SflowConsts.LOOPBACK_0_IP, '128')]},
                {'iface': pick_random_loopback[1], 'ips': [(SflowConsts.LOOPBACK_DEST_PORT_IP, '24')]}
                ],
        'ha': [{'iface': interfaces.ha_dut_1, 'ips': [(SflowConsts.HA_DUT_1_IP, '24')]},
               {'iface': interfaces.ha_dut_2, 'ips': [(SflowConsts.HA_DUT_2_IP, '24')]}
               ],
        'hb': [{'iface': interfaces.hb_dut_1, 'ips': [(SflowConsts.HB_DUT_1_IP, '24')]},
               {'iface': interfaces.hb_dut_2, 'ips': [(SflowConsts.HB_DUT_2_IP, '24')]},
               {'iface': interfaces.hb_dut_2, 'ips': [(SflowConsts.HB_DUT_2_IP_V6, '64')]}
               ]
    }

    IpConfigTemplate.configuration(topology_obj, ip_config_dict)
    logger.info('basic layer 3 connectivity configuration completed')
    yield

    IpConfigTemplate.cleanup(topology_obj, ip_config_dict)
    logger.info('basic layer 3 connectivity configuration cleanup completed')


@pytest.fixture(scope='module', autouse=True)
def enable_sflow_feature(engines, cli_objects):
    """
    Pytest fixture used to configure basic sflow configuration
    :param engines: engines fixture
    :param cli_objects: cli_objects fixture
    """
    cli_obj = cli_objects.dut
    with allure.step(f"Start feature {SflowConsts.SFLOW_FEATURE_NAME}"):
        cli_obj.sflow.enable_sflow_feature()

    yield

    with allure.step(f"Stop feature {SflowConsts.SFLOW_FEATURE_NAME}"):
        cli_obj.sflow.disable_sflow_feature()
    with allure.step("Remove sflowtool sample files"):
        remove_tmp_sample_file(engines)
    with allure.step("Delete sflow entries from redis db"):
        for table in SflowConsts.SFLOW_REDIS_GENERAL_TABLES:
            cli_obj.general.delete_redis_table(table)


@pytest.fixture(scope='function', autouse=True)
def basic_sflow_configuration_for_function(engines, cli_objects, interfaces):
    """
    Pytest fixture used to configure basic sflow configuration for test function
    :param engines: engines fixture
    :param cli_objects: cli_objects fixture
    :param interfaces: interfaces fixture
    """
    cli_obj = cli_objects.dut
    with allure.step(f"Enable {SflowConsts.SFLOW_FEATURE_NAME} globally"):
        cli_obj.sflow.enable_sflow()
    with allure.step(f"Configure sflow polling interval to {SflowConsts.POLLING_INTERVAL_1}"):
        cli_obj.sflow.config_sflow_polling_interval(SflowConsts.POLLING_INTERVAL_1)
    with allure.step(f"Add collector {SflowConsts.COLLECTOR_0} with udp port {SflowConsts.DEFAULT_UDP}"):
        cli_obj.sflow.add_collector(SflowConsts.COLLECTOR_0, SflowConsts.COLLECTOR_0_IP)
    with allure.step(f"Add collector {SflowConsts.COLLECTOR_1} with udp port {SflowConsts.UDP_1}"):
        cli_obj.sflow.add_collector(SflowConsts.COLLECTOR_1, SflowConsts.COLLECTOR_1_IP, SflowConsts.UDP_1)
    with allure.step("Disable all sflow interface"):
        cli_obj.sflow.disable_all_sflow_interface()
    with allure.step(f"Enable sflow interface {interfaces.dut_ha_1}"):
        cli_obj.sflow.enable_sflow_interface(interfaces.dut_ha_1)
    with allure.step(f"Enable sflow interface {interfaces.dut_ha_2}"):
        cli_obj.sflow.enable_sflow_interface(interfaces.dut_ha_2)
    with allure.step("Wait for hsflowd to be ready after sflow interface enabled"):
        wait_until_hsflowd_ready(engines, [SflowConsts.COLLECTOR_0_IP, SflowConsts.COLLECTOR_1_IP])
    yield

    with allure.step(f"Delete collector {SflowConsts.COLLECTOR_0}"):
        cli_obj.sflow.del_collector(SflowConsts.COLLECTOR_0)
    with allure.step(f"Delete collector {SflowConsts.COLLECTOR_1}"):
        cli_obj.sflow.del_collector(SflowConsts.COLLECTOR_1)
    with allure.step("Delete agent id"):
        cli_obj.sflow.del_agent_id()
    with allure.step(f"Disable sflow interface {interfaces.dut_ha_1}"):
        cli_obj.sflow.disable_sflow_interface(interfaces.dut_ha_1)
    with allure.step(f"Disable sflow interface {interfaces.dut_ha_2}"):
        cli_obj.sflow.disable_sflow_interface(interfaces.dut_ha_2)
    with allure.step(f"Disable {SflowConsts.SFLOW_FEATURE_NAME} globally"):
        cli_obj.sflow.disable_sflow()
    with allure.step("Kill all sflowtool process"):
        kill_sflowtool_process(engines)


def test_sflow_agent_id(engines, cli_objects):
    """
    Add loopback ip as the agent id and check the samples are received with intended agent-id.
    Remove agent-ip and check whether samples are received with previously configured agent ip.
    Add eth0 ip as the agent ip and check the samples are received with intended agent-id.
    :param engines: engines fixture
    :param cli_objects: cli_objects fixture
    """
    def _hsflowd_synced_after_agent_delete(engines, previous_agent_ip=SflowConsts.LOOPBACK_0_IP):
        """Return True when hsflowd has synced with DB."""
        logger.info(f"Checking if hsflowd has synced with DB after agent delete. Previous agent IP: {previous_agent_ip}")
        agent = get_agent_id_from_hsflowd(engines)
        if agent == previous_agent_ip:
            return False
        return True
    try:
        cli_obj = cli_objects.dut
        with allure.step('Configure the Loopback interface as agent'):
            cli_obj.sflow.add_agent_id(SflowConsts.LOOPBACK_0)
        with allure.step('Validate agent id configuration'):
            verify_sflow_configuration(cli_obj, status=SflowConsts.SFLOW_UP, agent_id=SflowConsts.LOOPBACK_0)
        with allure.step('Validate that agent value in sflow sample is the IP address of Loopback interface'):
            random_collector = random.choice(SflowConsts.COLLECTOR_LIST)
            verify_sflow_sample_agent_id(engines, random_collector, SflowConsts.LOOPBACK_0_IP)

        with allure.step('Delete sflow agent'):
            cli_obj.sflow.del_agent_id()
        with allure.step('Validate agent id configured to default'):
            verify_sflow_configuration(cli_obj, status=SflowConsts.SFLOW_UP, agent_id=SflowConsts.AGENT_ID_DEFAULT)
        with allure.step('Validate that agent value in sflow sample is previously configured agent IP'):
            random_collector = random.choice(SflowConsts.COLLECTOR_LIST)
            assert wait_until(10, 1, 0, _hsflowd_synced_after_agent_delete, engines), \
                "hsflowd is not synced after agent deleted"
            selected_agent_ip = get_agent_id_from_hsflowd(engines)
            verify_sflow_sample_agent_id(engines, random_collector, selected_agent_ip)

        with allure.step('Configure eth0 as agent id'):
            cli_obj.sflow.add_agent_id(SflowConsts.MGMT_INTF)
            mgmt_ip_mask = cli_obj.ip.get_interface_ips(SflowConsts.MGMT_INTF)
            mgmt_ip = mgmt_ip_mask[0]['ip']
        with allure.step('Validate agent id configured to mgmt interface'):
            verify_sflow_configuration(cli_obj, status=SflowConsts.SFLOW_UP, agent_id=SflowConsts.MGMT_INTF)
        with allure.step('Validate that agent value in sflow sample is the mgmt interface ip'):
            random_collector = random.choice(SflowConsts.COLLECTOR_LIST)
            verify_sflow_sample_agent_id(engines, random_collector, mgmt_ip)
    except Exception as err:
        raise AssertionError(err)


def test_sflow_polling_interval(engines, cli_objects, topology_obj, interfaces):
    """
    Test sflow polling with different polling interval and check whether the test interface sends counter samples for every polling interval
    Disable polling and check the DUT does not send counter samples
    :param engines: engines fixture
    :param cli_objects: cli_objects fixture
    :param topology_obj: topology_obj fixture
    :param interfaces: interfaces fixture
    """
    try:
        cli_obj = cli_objects.dut

        for polling_interval in POLLING_INTERVAL_LIST:
            with allure.step(f"Configure sflow polling interval to {polling_interval}"):
                cli_obj.sflow.config_sflow_polling_interval(polling_interval)
            with allure.step("Wait for hsflowd to apply new polling interval"):
                wait_until_hsflowd_ready(engines, [SflowConsts.COLLECTOR_0_IP, SflowConsts.COLLECTOR_1_IP])
            with allure.step(f"Validate sflow polling interval is configured to {polling_interval}"):
                verify_sflow_configuration(cli_obj, status=SflowConsts.SFLOW_UP, polling_interval=polling_interval)
            if polling_interval == 0:
                allure.step("Validate that counter samples could not be received")
            else:
                allure.step(f"Validate that counter samples could be received every {SflowConsts.POLLING_INTERVAL_2} seconds")
            random_collector = random.choice(SflowConsts.COLLECTOR_LIST)
            verify_sflow_sample_polling_interval(engines, topology_obj, random_collector, polling_interval)
    except Exception as err:
        raise AssertionError(err)


def test_sflow_interface(engines, cli_objects, interfaces, topology_obj, ha_dut_1_mac, dut_ha_1_mac):
    """
    Enable/disable sflow interfaces and check the samples are received only from the intended interface
    Test interfaces with different sampling rate
    :param engines: engines fixture
    :param cli_objects: cli_objects fixture
    :param interfaces: interfaces fixture
    :param topology_obj: topology_obj fixture
    :param ha_dut_1_mac: ha_dut_1_mac fixture
    :param dut_ha_1_mac: dut_ha_1_mac fixture
    """
    try:
        cli_obj = cli_objects.dut
        with allure.step(f"Disable sflow interface {interfaces.dut_ha_1} and {interfaces.dut_ha_2}"):
            cli_obj.sflow.disable_sflow_interface(interfaces.dut_ha_1)
            cli_obj.sflow.disable_sflow_interface(interfaces.dut_ha_2)
        with allure.step(f"Configure sflow interface {interfaces.dut_ha_1} and {interfaces.dut_ha_2} sample rate to {SflowConsts.SAMPLE_RATE_1}"):
            cli_obj.sflow.config_sflow_interface_sample_rate(interfaces.dut_ha_1, SflowConsts.SAMPLE_RATE_1)
            cli_obj.sflow.config_sflow_interface_sample_rate(interfaces.dut_ha_2, SflowConsts.SAMPLE_RATE_1)
        with allure.step(f"Validate that sflow interface {interfaces.dut_ha_1} and {interfaces.dut_ha_2} are in down status"):
            verify_sflow_interface_configuration(cli_obj, interfaces.dut_ha_1, SflowConsts.SFLOW_DOWN, SflowConsts.SAMPLE_RATE_1)
            verify_sflow_interface_configuration(cli_obj, interfaces.dut_ha_2, SflowConsts.SFLOW_DOWN, SflowConsts.SAMPLE_RATE_1)
        with allure.step("Validate that flow samples could not be received"):
            verify_flow_sample_received(engines, interfaces, topology_obj, SflowConsts.COLLECTOR_0, SflowConsts.SAMPLE_RATE_1, ha_dut_1_mac, dut_ha_1_mac, sample_exist=False)

        with allure.step(f"Enable sflow interface {interfaces.dut_ha_1} and {interfaces.dut_ha_2}"):
            cli_obj.sflow.enable_sflow_interface(interfaces.dut_ha_1)
            cli_obj.sflow.enable_sflow_interface(interfaces.dut_ha_2)
        with allure.step(f"Validate that sflow interface {interfaces.dut_ha_1} and {interfaces.dut_ha_2} are in up status"):
            verify_sflow_interface_configuration(cli_obj, interfaces.dut_ha_1, SflowConsts.SFLOW_UP, SflowConsts.SAMPLE_RATE_1)
            verify_sflow_interface_configuration(cli_obj, interfaces.dut_ha_2, SflowConsts.SFLOW_UP, SflowConsts.SAMPLE_RATE_1)
        with allure.step(f"Send traffic and validate {SflowConsts.COLLECTOR_0} could receive flow sample with sample rate {SflowConsts.SAMPLE_RATE_1}"):
            verify_flow_sample_received(engines, interfaces, topology_obj, SflowConsts.COLLECTOR_0, SflowConsts.SAMPLE_RATE_1, ha_dut_1_mac, dut_ha_1_mac)

        for sample_rate in SAMPLE_RATE_LIST:
            with allure.step(f"Configure sflow interface {interfaces.dut_ha_1} and {interfaces.dut_ha_2} sample rate to {sample_rate}"):
                cli_obj.sflow.config_sflow_interface_sample_rate(interfaces.dut_ha_1, sample_rate)
                cli_obj.sflow.config_sflow_interface_sample_rate(interfaces.dut_ha_2, sample_rate)
            with allure.step(f"Validate that sample rate of sflow interface {interfaces.dut_ha_1} and {interfaces.dut_ha_2} is {sample_rate}"):
                verify_sflow_interface_configuration(cli_obj, interfaces.dut_ha_1, SflowConsts.SFLOW_UP, sample_rate)
                verify_sflow_interface_configuration(cli_obj, interfaces.dut_ha_2, SflowConsts.SFLOW_UP, sample_rate)
            with allure.step(f"Send traffic and validate {SflowConsts.COLLECTOR_0} could receive flow sample with sample rate {sample_rate}"):
                verify_flow_sample_received(engines, interfaces, topology_obj, SflowConsts.COLLECTOR_0, sample_rate, ha_dut_1_mac, dut_ha_1_mac)
    except Exception as err:
        raise AssertionError(err)


def test_sflow_collector(engines, cli_objects, interfaces, topology_obj, ha_dut_1_mac, dut_ha_1_mac, pick_random_loopback):
    """
    Test sflow with 2 collectors, adding or removing collector and verify samples
    Collector IP address support both IPv4 and IPv6
    :param engines: engines fixture
    :param cli_objects: cli_objects fixture
    :param interfaces: interfaces fixture
    :param topology_obj: topology_obj fixture
    :param ha_dut_1_mac: ha_dut_1_mac fixture
    :param dut_ha_1_mac: dut_ha_1_mac fixture
    :param pick_random_loopback: pick_random_loopback fixture
    """
    try:
        cli_obj = cli_objects.dut
        with allure.step(f"Remove sflow collector {SflowConsts.COLLECTOR_1}"):
            cli_obj.sflow.del_collector(SflowConsts.COLLECTOR_1)
        with allure.step("Disable counter sample polling"):
            cli_obj.sflow.config_sflow_polling_interval(SflowConsts.POLLING_INTERVAL_0)
        with allure.step(f"Configure sample rate of sflow interface {interfaces.dut_ha_1}, {interfaces.dut_ha_2},"
                         f" {pick_random_loopback[0]}, {pick_random_loopback[1]} to {SflowConsts.SAMPLE_RATE_1}"):
            cli_obj.sflow.config_sflow_interface_sample_rate(interfaces.dut_ha_1, SflowConsts.SAMPLE_RATE_1)
            cli_obj.sflow.config_sflow_interface_sample_rate(interfaces.dut_ha_2, SflowConsts.SAMPLE_RATE_1)
            cli_obj.sflow.config_sflow_interface_sample_rate(pick_random_loopback[0], SflowConsts.SAMPLE_RATE_1)
            cli_obj.sflow.config_sflow_interface_sample_rate(pick_random_loopback[1], SflowConsts.SAMPLE_RATE_1)
        with allure.step(f"Send traffic and validate {SflowConsts.COLLECTOR_0} could receive flow sample with sample rate {SflowConsts.SAMPLE_RATE_1}"):
            verify_flow_sample_received(engines, interfaces, topology_obj, SflowConsts.COLLECTOR_0, SflowConsts.SAMPLE_RATE_1, ha_dut_1_mac, dut_ha_1_mac)

        with allure.step(f"Remove sflow collector {SflowConsts.COLLECTOR_0}"):
            cli_obj.sflow.del_collector(SflowConsts.COLLECTOR_0)
        with allure.step(f"Validate that {SflowConsts.COLLECTOR_0} is removed"):
            verify_sflow_configuration(cli_obj, status=SflowConsts.SFLOW_UP, collector=[])
        with allure.step(f"Validate that flow samples could not be received"):
            verify_flow_sample_received(engines, interfaces, topology_obj, SflowConsts.COLLECTOR_0, SflowConsts.SAMPLE_RATE_1, ha_dut_1_mac, dut_ha_1_mac, sample_exist=False)

        with allure.step(f"Add one sflow collector {SflowConsts.COLLECTOR_0}"):
            cli_obj.sflow.add_collector(SflowConsts.COLLECTOR_0, SflowConsts.COLLECTOR_0_IP)
        with allure.step(f"Validate sflow collector {SflowConsts.COLLECTOR_0} is configured"):
            verify_sflow_configuration(cli_obj, status=SflowConsts.SFLOW_UP, collector=[SflowConsts.COLLECTOR_0])
        with allure.step(f"Send traffic and validate that {SflowConsts.COLLECTOR_0} could receive flow sample with sample rate {SflowConsts.SAMPLE_RATE_1}"):
            verify_flow_sample_received(engines, interfaces, topology_obj, SflowConsts.COLLECTOR_0, SflowConsts.SAMPLE_RATE_1, ha_dut_1_mac, dut_ha_1_mac)

        with allure.step(f"Add second sflow collector {SflowConsts.COLLECTOR_1} with IPv6 address {SflowConsts.COLLECTOR_1_IP_V6} and UDP port {SflowConsts.UDP_1}"):
            cli_obj.sflow.add_collector(SflowConsts.COLLECTOR_1, SflowConsts.COLLECTOR_1_IP_V6, SflowConsts.UDP_1)
        with allure.step(f"Validate sflow collector {SflowConsts.COLLECTOR_0} and {SflowConsts.COLLECTOR_1} are configured"):
            verify_sflow_configuration(cli_obj, status=SflowConsts.SFLOW_UP, collector=[SflowConsts.COLLECTOR_0, SflowConsts.COLLECTOR_1])
        with allure.step(f"Send traffic and validate that {SflowConsts.COLLECTOR_0} could receive flow sample with with sample rate {SflowConsts.SAMPLE_RATE_1}"):
            verify_flow_sample_received(engines, interfaces, topology_obj, SflowConsts.COLLECTOR_0, SflowConsts.SAMPLE_RATE_1, ha_dut_1_mac, dut_ha_1_mac)
        with allure.step(f"Send traffic through loopback ports and validate that {SflowConsts.COLLECTOR_1} could receive flow sample with with sample rate {SflowConsts.SAMPLE_RATE_1}"):
            dest_loopback_mac = dut_ha_1_mac  # the mac address of the loopback is the same as any data port on the switch
            verify_flow_sample_received(engines, interfaces, topology_obj, SflowConsts.COLLECTOR_1, SflowConsts.SAMPLE_RATE_1, SflowConsts.DUMMY_SOURCE_LOOPBACK_MAC, dest_loopback_mac,
                                        loopback=True, src_iface_name=pick_random_loopback[0])
        with allure.step(f"Configure the third collector, system only support 2 collectors, and the third collector configuration is supposed to be failed"):
            result = cli_obj.sflow.add_collector(SflowConsts.COLLECTOR_F, SflowConsts.COLLECTOR[SflowConsts.COLLECTOR_F]['ip'], validate=False)
            assert result == COLLECTOR_WARNING_CONTENT, f"System could configure the third collector {SflowConsts.COLLECTOR_F}"
    except Exception as err:
        raise AssertionError(err)
