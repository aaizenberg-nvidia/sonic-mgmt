# Builtin libs
import subprocess as sp
import pytest
import logging
import allure
import sys
import re

from ngts.constants.constants import MarsConstants

logger = logging.getLogger("PreTestCheck")


@pytest.fixture(scope='function')
def current_topo(request):
    """
    Method for get current_topo from pytest arguments
    :param request: pytest builtin
    :return: current_topo, i.e. t1-lag
    """
    return request.config.getoption('--current_topo')


@pytest.fixture(scope='function')
def expected_topo(request):
    """
    Method for get expected_topo from pytest arguments
    :param request: pytest builtin
    :return: expected_topo, i.e. ptf32
    """
    return request.config.getoption('--expected_topo')


def get_sonic_hwsku(duthost):
    return duthost.run_cmd('redis-cli -n 4 hget "DEVICE_METADATA|localhost" hwsku').strip().replace('"', '')


def get_dev_neighbor(duthost):
    reg_neighbor = r'.*"(?P<neighbor>.*)".*'
    neighbors_res = duthost.run_cmd("redis-cli -n 4 keys DEVICE_NEIGHBOR*").strip("\n").split("\n")
    neighbor_list = []

    for neighbor in neighbors_res:
        res = re.search(reg_neighbor, neighbor)
        if res:
            neighbor_list.append(res.groupdict()["neighbor"])

    logger.info(f"neighbors is :{neighbor_list}")
    return neighbor_list


def get_neighbor_name(duthost, neighbor):
    reg_neighbor = r'.*"(?P<neighbor_name>.*)".*'
    neighbor_name_res = duthost.run_cmd('redis-cli -n 4 hget \"{}\" name'.format(neighbor)).strip("\n")
    res = re.search(reg_neighbor, neighbor_name_res)
    neighbor_name = ''
    if res:
        neighbor_name = res.groupdict()["neighbor_name"]

    logger.info(f"neighbors name is :{neighbor_name}")
    return neighbor_name


def mock_t1_topo_on_ptf32(duthost):
    """
    Hack the DEVICE_NEIGHBOR_METADATA table for the ptf32 topo on "Mellanox-SN4600C-C64" to simulate the
    dual tor t1 topo
    :param duthost: dut host engine
    """
    logger.info("mock t1 topo on ptf32")
    neighbors = get_dev_neighbor(duthost)
    vm_hwsku = "Arista-VM"

    add_neighbor_metadata_cmd_pattern = 'redis-cli -n 4 hset "DEVICE_NEIGHBOR_METADATA|{}" hwsku {} mgmt_addr {}  type {}'

    mgmt_addr_patern = "10.75.207.{}"
    for index, neighbor in enumerate(neighbors):
        neighbor_name = get_neighbor_name(duthost, neighbor)
        mgmt_addr = mgmt_addr_patern.format(10 + index)
        if neighbor_name.endswith("T0"):
            router_type = "ToRRouter"
        else:
            router_type = "SpineRouter"
        add_neighbor_metadata_cmd = add_neighbor_metadata_cmd_pattern.format(neighbor_name, vm_hwsku, mgmt_addr, router_type)
        duthost.run_cmd(add_neighbor_metadata_cmd)
    enable_qos_config_and_reload_config(duthost)


def mock_t0_topo_on_ptf32(duthost):
    """
    Hack the DEVICE_NEIGHBOR_METADATA table for the ptf32 topo on "Mellanox-SN2700-D48C8" to simulate the
    dual tor t0 topo
    :param duthost: dut host engine
    """
    logger.info("mock t0 topo on ptf32")
    neighbors = get_dev_neighbor(duthost)
    for index, neighbor in enumerate(neighbors):
        neighbor_name = get_neighbor_name(duthost, neighbor)
        mgmt_addr = "10.75.207.{}".format(index + 1)
        if neighbor_name.endswith("T0"):
            new_neighbor_name = neighbor_name.replace("T0", "T1")
            router_type = "LeafRouter"
            add_neighbor_metadata_cmd = 'redis-cli -n 4 hset "DEVICE_NEIGHBOR_METADATA|{}" ' \
                                        'hwsku Arista-VM mgmt_addr {} type {}'.format(new_neighbor_name, mgmt_addr, router_type)
            logger.info("Add DEVICE_NEIGHBOR_METADATA with cmd:{}".format(add_neighbor_metadata_cmd))
            duthost.run_cmd(add_neighbor_metadata_cmd)
            update_neighbor_name_cmd = 'redis-cli -n 4 hset "{}" name {}'.format(neighbor, new_neighbor_name)
        elif neighbor_name.endswith("T2"):
            update_neighbor_name_cmd = 'redis-cli -n 4 hset "{}" name Servers{} port eth0'.format(neighbor, index)
        logger.info("Update DEVICE_NEIGHBOR name with cmd:{}".format(update_neighbor_name_cmd))
        duthost.run_cmd(update_neighbor_name_cmd)
    logger.info("Set type to ToRRouter and subtype to DualToR in DEVICE_METADATA|localhost")
    duthost.run_cmd('redis-cli -n 4 hset "DEVICE_METADATA|localhost" type ToRRouter subtype DualToR')

    enable_qos_config_and_reload_config(duthost)


def enable_qos_config_and_reload_config(duthost):
    enable_qos_remap_table = 'redis-cli -n 4 hset "SYSTEM_DEFAULTS|tunnel_qos_remap" status enabled'
    duthost.run_cmd(enable_qos_remap_table)
    duthost.run_cmd("sudo config qos reload --no-dynamic-buffer")
    duthost.run_cmd("sudo config save -y")
    duthost.run_cmd("sudo config reload -y -f")
    duthost.run_cmd("sleep 180")


def run_testbed_cli_script(cmd, ansible_path):
    logger.info("Running CMD: {}".format(cmd))
    pipe = sp.Popen(f"cd {ansible_path} && {cmd}", shell=True, stdout=sp.PIPE, stderr=sp.PIPE)
    res = pipe.communicate()
    for line in res[0].decode(encoding='utf-8').split('\n'):
        logger.info(line)
    assert not pipe.returncode, "stderr = {0}".format(res[1])


def test_rpc_check_and_set_topology(topology_obj, engines, cli_objects, current_topo, expected_topo):
    ansible_path = "/root/mars/workspace/sonic-mgmt/ansible"
    dut_name = cli_objects.dut.chassis.get_hostname()
    if current_topo == expected_topo:
        pytest.skip(f"current topo is expected topo:{expected_topo}")

    sonic_branch = topology_obj.players['dut']['branch']
    logger.info('SONiC branch is: {}'.format(sonic_branch))
    ptf_tag = MarsConstants.BRANCH_PTF_MAPPING.get(sonic_branch, MarsConstants.DEFAULT_PTF_TAG)

    with allure.step("Remove topo {}".format(current_topo)):
        cmd = "./testbed-cli.sh -k ceos remove-topo {SWITCH}-{TOPO} vault".format(SWITCH=dut_name, TOPO=current_topo)
        run_testbed_cli_script(cmd, ansible_path)

    with allure.step("Add topology {}".format(expected_topo)):
        cmd = "./testbed-cli.sh -k ceos add-topo {SWITCH}-{TOPO} vault " \
              "-e ptf_imagetag={PTF_TAG}".format(SWITCH=dut_name, TOPO=expected_topo, PTF_TAG=ptf_tag)
        run_testbed_cli_script(cmd, ansible_path)

    with allure.step("Deploy minigraph"):
        cmd = "./testbed-cli.sh deploy-mg {SWITCH}-{TOPO} lab vault".format(SWITCH=dut_name, TOPO=expected_topo)
        run_testbed_cli_script(cmd, ansible_path)

    with allure.step("Check if FW control feature enabled, if yes - disable auto neg on ports supporting FW control"):
        if cli_objects.dut.im.is_im_enabled():
            port_supporting_im = cli_objects.dut.im.get_ports_supporting_im(
                cli_objects.dut.im.dut_ports_number_dict(topology_obj, is_community=True))
            if port_supporting_im:
                aoc_cables = cli_objects.dut.im.sw_controlled_aoc_cables(port_supporting_im)
                if aoc_cables:
                    with allure.step('Disable autoneg on AOC ports if SW controlled'):
                        cli_objects.dut.im.disable_autoneg_on_ports_supporting_im(aoc_cables)
                copper_cables = port_supporting_im.get('passive_copper_cables')
                if copper_cables:
                    with allure.step('Enable autoneg on copper cable ports if SW controlled'):
                        cli_objects.dut.im.enable_autoneg_on_passive_copper(copper_cables)

    with allure.step('Apply DNS servers configuration'):
        cli_objects.dut.ip.apply_dns_servers_into_resolv_conf()

    ptp_topos = ["ptp-256", "ptp-130", "ptp-126"]
    if expected_topo not in ptp_topos:
        with allure.step("Post upgrade checks"):
            cmd = "ansible-playbook -i inventory --limit {SWITCH} post_upgrade_check.yml " \
                  "-e topo={TOPO} -b -vvv ".format(SWITCH=dut_name, TOPO=expected_topo)
            run_testbed_cli_script(cmd, ansible_path)

    engines.dut.disconnect()
    hwsku = get_sonic_hwsku(engines.dut)
    if expected_topo == "ptf32" and hwsku in ["Mellanox-SN4600C-C64"]:
        mock_t1_topo_on_ptf32(engines.dut)
    elif expected_topo == "ptf32" and hwsku in ["Mellanox-SN2700-D48C8", "Mellanox-SN4700-O8V48"]:
        mock_t0_topo_on_ptf32(engines.dut)
