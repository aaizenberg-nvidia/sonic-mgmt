import allure
import pytest
import logging

from retry.api import retry_call
from ngts.cli_wrappers.linux.linux_arp_cache_cli import LinuxARPCache
from ngts.config_templates.vlan_config_template import VlanConfigTemplate
from ngts.config_templates.ip_config_template import IpConfigTemplate

logger = logging.getLogger()


@pytest.fixture(scope='module')
def env(duthosts, topology_obj, platform_params):
    """ Fixture which contains DUT - engine and CLI objects """

    class Collector:
        pass

    Collector.duthost = duthosts[0]
    Collector.dut_engine = topology_obj.players['dut']['engine']
    Collector.sonic_cli = topology_obj.players['dut']['cli']
    Collector.vlan_iface_69 = 'Vlan69'
    Collector.vlan_iface_40 = 'Vlan40'
    Collector.dut_ha_2 = topology_obj.ports["dut-ha-2"]
    Collector.ip_neigh_69 = '69.0.0.5'
    Collector.ip_neigh_40 = '40.0.0.5'
    Collector.dst_ip = '2.2.2.0'
    Collector.mask = 24
    Collector.platform_params = platform_params
    Collector.MAX_CRM_UPDATE_TIME = 15
    Collector.APPLY_CFG_MAX_UPDATE_TIME = 30
    if 'simx' in platform_params.platform:
        Collector.MAX_CRM_UPDATE_TIME = 60
        Collector.APPLY_CFG_MAX_UPDATE_TIME = 90
    yield Collector


@pytest.fixture(scope='module', autouse=True)
def pre_configure_for_crm(request, engines, topology_obj, interfaces):
    """
    Pytest fixture which are doing configuration for crm tests
    :param engines: request object fixture
    :param engines: engines object fixture
    :param topology_obj: topology object fixture
    :param interfaces: interfaces object fixture
    """
    # VLAN config which will be used in test
    vlan_config_dict = {
        'dut': [{'vlan_id': 40, 'vlan_members': [{interfaces.dut_ha_1: 'trunk'}, {interfaces.dut_ha_2: 'trunk'}]},
                {'vlan_id': 69, 'vlan_members': [{interfaces.dut_hb_2: 'trunk'}]},
                ],
        'ha': [{'vlan_id': 40, 'vlan_members': [{interfaces.ha_dut_1: None}]},
               ],
        'hb': [{'vlan_id': 69, 'vlan_members': [{interfaces.hb_dut_2: None}]},
               ]
    }

    # IP config which will be used in test
    ip_config_dict = {
        'dut': [{'iface': 'Vlan40', 'ips': [('40.0.0.1', '24')]},
                {'iface': 'Vlan69', 'ips': [('69.0.0.1', '24')]},
                ],
        'ha': [{'iface': "{}.40".format(interfaces.ha_dut_1), 'ips': [('40.0.0.3', '24')]}],
        'hb': [{'iface': "{}.69".format(interfaces.hb_dut_2), 'ips': [('69.0.0.3', '24')]}]
    }
    VlanConfigTemplate.configuration(topology_obj, vlan_config_dict, request)
    IpConfigTemplate.configuration(topology_obj, ip_config_dict, request)
    yield


@pytest.fixture(scope='module', autouse=True)
def set_polling_interval(env):
    """ Set CRM polling interval to 1 second """
    wait_time = 2
    polling_1_sec = 1
    original_poll_interval = env.sonic_cli.crm.get_polling_interval()

    with allure.step('Set CRM polling interval to {}'.format(polling_1_sec)):
        env.sonic_cli.crm.set_polling_interval(polling_1_sec)
    retry_call(ensure_polling_configured, fargs=[polling_1_sec, env.sonic_cli, env.dut_engine], tries=5, delay=1,
               logger=None)

    yield

    with allure.step('Restore CRM polling interval to {}'.format(original_poll_interval)):
        env.sonic_cli.crm.set_polling_interval(original_poll_interval)
    retry_call(ensure_polling_configured, fargs=[original_poll_interval, env.sonic_cli, env.dut_engine], tries=5,
               delay=1, logger=None)


@pytest.fixture
def cleanup(request):
    """
    Fixture executes cleanup commands on DUT after each of test case if such were provided
    """
    params_list = []
    yield params_list
    if "rep_call" in dir(request.node):
        if params_list:
            with allure.step('Execute test cleanup commands'):
                for item in params_list:
                    item[0](*item[1:])


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """ This fixture call is required for 'cleanup' fixture """
    # execute all other hooks to obtain the report object
    outcome = yield
    rep = outcome.get_result()

    setattr(item, "rep_" + rep.when, rep)


def ensure_polling_configured(expected_interval, sonic_cli, dut_engine):
    """
    Function checks that crm polling interval was configured
    """
    assert (sonic_cli.crm.get_polling_interval() == expected_interval), "CRM polling interval was not updated"


@pytest.fixture(scope='module')
def map_res_to_thr():
    """
    Contains mapping of CRM resource names to its threshold names
    """
    res_to_thr = {
        'ipv4_route': 'ipv4 route',
        'ipv6_route': 'ipv6 route',
        'ipv4_nexthop': 'ipv4 nexthop',
        'ipv6_nexthop': 'ipv6 nexthop',
        'ipv4_neighbor': 'ipv4 neighbor',
        'ipv6_neighbor': 'ipv6 neighbor',
        'nexthop_group_member': 'nexthop group member',
        'nexthop_group': 'nexthop group object',
        'acl_entry': 'acl group entry',
        'acl_counter': 'acl group counter',
        'fdb_entry': 'fdb'
    }
    return res_to_thr


@pytest.fixture(scope='module', autouse=True)
def increase_arp_cache(env):
    """
    Increase default Linux ARP cache config, this will prevent dynamical ARP and neighbor clearing.
    This is needed to keep hardware resources counter unchanged dynamically during tests execution.

    gc_thresh1 The minimum number of entries to keep in the ARP cache. The garbage collector will
        not run if there are fewer than this number of entries in the cache. Defaults to 128.
    gc_thresh2 The soft maximum number of entries to keep in the ARP cache. The garbage collector
        will allow the number of entries to exceed this for 5 seconds before collection will be performed.
        Defaults to 512.
    gc_thresh3 The hard maximum number of entries to keep in the ARP cache. The garbage collector
        will always run if there are more than this number of entries in the cache. Defaults to 1024.
    gc_interval The amount of time after which to clear outdated ARP entries.

    Example of commands to be executed:
    sysctl -w net.ipv4.neigh.default.gc_interval=600
    sysctl -w net.ipv6.neigh.default.gc_interval=600
    sysctl -w net.ipv4.route.gc_interval=600
    sysctl -w net.ipv6.route.gc_interval=600

    sysctl -w net.ipv4.neigh.default.gc_thresh3=8192
    sysctl -w net.ipv4.neigh.default.gc_thresh2=8192
    sysctl -w net.ipv4.neigh.default.gc_thresh1=8192

    sysctl -w net.ipv6.neigh.default.gc_thresh1=8192
    sysctl -w net.ipv6.neigh.default.gc_thresh2=8192
    sysctl -w net.ipv6.neigh.default.gc_thresh3=8192
    """

    gc_thresh = {4: {1: None, 2: None, 3: None}, 6: {1: None, 2: None, 3: None}}
    route_gc_interval = {4: None, 6: None}
    neigh_gc_interval = {4: None, 6: None}

    test_gc_interval = 600
    test_gc_thresh = 30000
    # Collect Linux gc_interval and gc_thresh1 values
    for ip_ver in [4, 6]:
        for thresh_id in range(1, 4):
            # Store default
            gc_thresh[ip_ver][thresh_id] = LinuxARPCache(engine=env.dut_engine).get_gc_thresh(ip_ver, thresh_id)
            # Configure threshold that will not reach by test case
            LinuxARPCache(engine=env.dut_engine).set_gc_thresh(ip_ver, thresh_id, test_gc_thresh)
        # Store default net.ipv[4|6].route.gc_interval
        route_gc_interval[ip_ver] = LinuxARPCache(engine=env.dut_engine).get_route_gc_interval(ip_ver)
        # Increase 'route.gc_interval' interval to 600 seconds
        LinuxARPCache(engine=env.dut_engine).set_route_gc_interval(ip_ver, route_gc_interval[ip_ver])
        # Store default net.ipv[4|6].neigh.default.gc_interval
        neigh_gc_interval[ip_ver] = LinuxARPCache(engine=env.dut_engine).get_neigh_gc_interval(ip_ver)
        # Increase 'neigh.default.gc_interval' interval to 600 seconds
        LinuxARPCache(engine=env.dut_engine).set_neigh_gc_interval(ip_ver, neigh_gc_interval[ip_ver])

    yield

    # Restore default Linux gc_interval and gc_thresh values
    for ip_ver in [4, 6]:
        for thresh_id in range(1, 4):
            LinuxARPCache(engine=env.dut_engine).set_gc_thresh(ip_ver, thresh_id, gc_thresh[ip_ver][thresh_id])
        LinuxARPCache(engine=env.dut_engine).set_route_gc_interval(ip_ver, route_gc_interval[ip_ver])
        LinuxARPCache(engine=env.dut_engine).set_neigh_gc_interval(ip_ver, neigh_gc_interval[ip_ver])


@pytest.fixture
def thresholds_cleanup(env, map_res_to_thr, cleanup):
    crm_thresholds = env.sonic_cli.crm.parse_thresholds_table()
    cmd = 'crm config thresholds {res} type {th_type}; crm config thresholds {res} low {low}; crm config thresholds {res} high {high}'
    yield
    # Restore CRM thresholds configuration
    for crm_res, cli_res in map_res_to_thr.items():
        th_type = crm_thresholds[crm_res]['Threshold Type']
        low = crm_thresholds[crm_res]['Low Threshold']
        high = crm_thresholds[crm_res]['High Threshold']
        env.dut_engine.run_cmd(cmd.format(res=cli_res, th_type=th_type, low=low, high=high))


@pytest.fixture(scope='module', autouse=True)
def stop_arp_update(env):
    cmd_template = 'docker exec -i swss supervisorctl {action} arp_update'
    with allure.step('Stop arp_update in SONIC'):
        env.dut_engine.run_cmd(cmd_template.format(action='stop'))
    yield
    with allure.step('Start arp_update in SONIC'):
        env.dut_engine.run_cmd(cmd_template.format(action='start'))
