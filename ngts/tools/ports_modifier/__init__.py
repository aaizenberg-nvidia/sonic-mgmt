import pytest
import json
import os
import logging

from ngts.tools.topology_tools.topology_by_setup import get_topology_by_setup_name_and_aliases
from ngts.cli_wrappers.sonic.sonic_cli import SonicCli
from ngts.constants.constants import InfraConst, PlatformTypesConstants
from ngts.helpers.config_db_utils import save_config_db_json
from ngts.tools.infra import get_platform_info
from ngts.conftest import chip_type, platform_params
logger = logging.getLogger()

REBOOT_TEST_NAME = 'test_push_gate_reboot_policer'
ACL_SCALE_TEST_NAME = 'test_acl_config_scale'
CONFIG_DB_DUT_PATH = '/etc/sonic/config_db.json'
CONFIG_DB_DUT_TEMP_PATH = '/tmp/config_db.json'
CONFIG_DB_COPY_NAME = 'config_db_copy.json'
CONFIG_DB_COPY_PATH = os.path.join('/tmp', CONFIG_DB_COPY_NAME)
PRE_RUNNING_CONFIG_PATH = '/tmp/pre_running_config.json'
MAX_PORTS_TEST_LIST = [REBOOT_TEST_NAME, ACL_SCALE_TEST_NAME]
INDEPENDENT_MODULE_PLATFORMS = [PlatformTypesConstants.PLATFORM_MOOSE, PlatformTypesConstants.PLATFORM_GAUR,
                                PlatformTypesConstants.PLATFORM_LEOPARD, PlatformTypesConstants.BISON_PLATFORMS,
                                PlatformTypesConstants.PLATFORM_LEOPARD_DC]


def pytest_addoption(parser):

    parser.addoption('--ports_number', action='store', default=None,
                     help='Argument related to reboot test, based on it we can specify number of ports which '
                          'should be used in reboot tests')


def pytest_collection_modifyitems(session, config, items):
    """
    Max supported ports per platform on canonical setup(4 ports connected to hosts)
    msn2700  - 60
    msn3420  - 93
    msn3700  - 116
    msn4410  - 116
    msn4600  - 124
    msn4600c - 124
    msn4700  - 116
    sn5400   - 128
    sn5600   - 128
    sn5610   - 244
    sn5640   - 512
    """
    if len(items) == 1 and items[0].name in MAX_PORTS_TEST_LIST and config.option.ports_number:
        minimum_ports_number = 4
        max_ports_num_per_platform = {PlatformTypesConstants.PLATFORM_PANTHER: 60,
                                      PlatformTypesConstants.PLATFORM_PANTHER_A1: 60,
                                      PlatformTypesConstants.PLATFORM_LIONFISH: 93,
                                      PlatformTypesConstants.PLATFORM_ANACONDA: 116,
                                      PlatformTypesConstants.PLATFORM_ANACONDA_C: 116,
                                      PlatformTypesConstants.PLATFORM_TIGON: 124,
                                      PlatformTypesConstants.PLATFORM_LEOPARD: 116,
                                      PlatformTypesConstants.PLATFORM_LEOPARD_DC: 116,
                                      PlatformTypesConstants.PLATFORM_MOOSE: 244,
                                      PlatformTypesConstants.PLATFORM_GAUR: 244,
                                      PlatformTypesConstants.PLATFORM_CHAMELEON_SINGLE_LD: 504,
                                      }

        setup_name = session.config.option.setup_name
        topology = get_topology_by_setup_name_and_aliases(session.config.option.setup_name, slow_cli=False)
        dut_engine = topology.players['dut']['engine']
        cli_object = SonicCli(topology)
        platform = get_platform_info(topology)['platform']

        # save pre running config for config_check
        dut_engine.run_cmd(f"sonic-cfggen -d --print-data > {PRE_RUNNING_CONFIG_PATH}")

        # Save available config_db.json from DUT to sonic-mgmt docker /tmp folder
        logger.info(f'Copy original config_db.json from DUT to sonic-mgmt /tmp folder')
        dut_engine.copy_file(source_file=f"{PRE_RUNNING_CONFIG_PATH}",
                             dest_file=CONFIG_DB_COPY_PATH, file_system='/tmp/', direction='get')

        if platform in PlatformTypesConstants.BISON_PLATFORMS:
            logger.info("Bison is already configured to max ports")
            return

        if items[0].name == ACL_SCALE_TEST_NAME:
            if '_simx' in platform:
                # enable simx
                platform = platform.replace('_simx', '')
            elif platform in INDEPENDENT_MODULE_PLATFORMS and cli_object.im.is_im_enabled():
                # disable IM
                dut_engine.run_cmd('sudo /usr/bin/cmis_host_mgmt.py --disable')

        elif cli_object.im.is_im_enabled():
            skip = pytest.mark.skip(reason=f'SW control feature enabled at {platform} and port breakout '
                                    f'is not supported')
            add_marker(items, skip)
            return
        if platform not in max_ports_num_per_platform.keys():
            skip = pytest.mark.skip(reason=f'{platform} platform does not support split to maximum ports')
            add_marker(items, skip)
            return

        platform_max_ports_num = max_ports_num_per_platform[platform]
        if config.option.ports_number == "max":
            expected_ports_num = platform_max_ports_num
        else:
            expected_ports_num = int(config.option.ports_number)

        if expected_ports_num < minimum_ports_number:
            skip = pytest.mark.skip(reason=f'Expected number of ports: {expected_ports_num}, '
                                    f'but it must be >= {minimum_ports_number}')
            add_marker(items, skip)
            return
        if expected_ports_num > platform_max_ports_num:
            skip = pytest.mark.skip(reason=f'Platform: {platform} expected number of ports: {expected_ports_num}, '
                                    f'but it must be <= {platform_max_ports_num}')
            add_marker(items, skip)
            return
        logger.info(f'Setup will be configured with {expected_ports_num} ports')
        # Get config from shared location
        shared_path = f'{InfraConst.MARS_TOPO_FOLDER_PATH}{setup_name}'
        sonic_ver = cli_object.general.get_image_sonic_version()
        config_db_file_name = f'{sonic_ver}_config_db.json'
        orig_config_db_shared_path = os.path.join(shared_path, config_db_file_name)

        original_config_db = get_original_config_db(orig_config_db_shared_path, CONFIG_DB_COPY_PATH)
        existing_ports_num = len(original_config_db['PORT'])

        if expected_ports_num != existing_ports_num:
            dut_to_host_ports_list = [port for alias, port in topology.ports.items() if alias.startswith('dut-h')]
            modified_config = generate_config_db(original_config_db, dut_engine, expected_ports_num, platform,
                                                 dut_to_host_ports_list, topology)
            save_config_db_json(dut_engine, modified_config)
            # Reload QoS to clear QoS configuration and generate new QoS configuration,
            # because post are splitted to max ports, port configuration change
            # some left qos buffer config will does not match the new ports config
            # eg. For ACS-MSN2700, Port Ethernet25 will not exist in new config_db.json, but it still has qos buffer config in config_db.json
            cli_object.qos.reload_qos()
            cli_object.general.save_configuration()
            cli_object.general.reload_configuration(force=True)

            # Mark that the original configuration needs to be restored after the test
            logger.info('The max port configuration has loaded')
            cli_object.ip.apply_dns_servers_into_resolv_conf()


def add_marker(items, skip):
    # Helper method for adding skip markers
    for item in items:
        item.add_marker(skip)
        return


def reload_config(session, platform_params, chip_type, dut_cli_object):
    # Reload the original configuration
    topology = get_topology_by_setup_name_and_aliases(session.config.option.setup_name, slow_cli=False)
    dut_engine = topology.players['dut']['engine']
    cli_object = SonicCli(topology)
    logger.info(f'Copy original config_db.json file from sonic-mgmt /tmp folder to  DUT /tmp/ folder')
    dut_engine.copy_file(source_file=CONFIG_DB_COPY_PATH,
                         dest_file=CONFIG_DB_DUT_TEMP_PATH, file_system='/tmp/',
                         overwrite_file=True, verify_file=False)
    logger.info(f'Copy db file from DUT /tmp folder to DUT {CONFIG_DB_DUT_PATH}')
    dut_engine.run_cmd(f'sudo cp {CONFIG_DB_DUT_TEMP_PATH} {CONFIG_DB_DUT_PATH}')

    # enable independent module in case it's disabled
    platform = get_platform_info(topology)['platform']
    if platform in INDEPENDENT_MODULE_PLATFORMS and not cli_object.im.is_im_enabled():
        cli_object.im.cleanup_sai_profile_flag(platform_params)
        cli_object.im.enable_im_in_sai()
        cli_object.im.upload_cmis_files(platform_params, chip_type)
        cli_object.im.enable_cmis_mgr_in_pmon_file(platform_params)

    # reload config for original config_db.json and IM configuration
    dut_cli_object.general.reload_flow(ports_list=None, topology_obj=topology, reload_force=True)


def get_original_config_db(shared_config_db_path, dut_config_db_copy_path):
    if os.path.exists(shared_config_db_path):
        logger.info(f'Loading config_db from shared location: {shared_config_db_path}')
        return read_config_db_from_file(shared_config_db_path)

    logger.info(
        f'Shared config_db not found at {shared_config_db_path}, '
        f'using pre-test DUT config from {dut_config_db_copy_path}'
    )
    if not os.path.exists(dut_config_db_copy_path):
        raise FileNotFoundError(
            f'config_db not found at shared path ({shared_config_db_path}) '
            f'or local copy ({dut_config_db_copy_path})'
        )
    return read_config_db_from_file(dut_config_db_copy_path)


def read_config_db_from_file(config_db_path):
    with open(config_db_path) as conf_obj:
        config_db = json.load(conf_obj)
    return config_db


def get_dut_physical_ports_config(engine, platform):
    platform_ini_name = f'{platform}.ini'
    port_ini_local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'port_configs', platform_ini_name)
    engine.copy_file(source_file=port_ini_local_path, dest_file=platform_ini_name,
                     file_system='/tmp', overwrite_file=True, verify_file=False)

    port_ini_dut_path = f'/tmp/{platform_ini_name}'
    cmd = f'sonic-cfggen -k none -p {port_ini_dut_path} --print-data'
    res = json.loads(engine.run_cmd(cmd))
    physical_dut_ports = res['PORT']

    return physical_dut_ports


def modify_lanes_per_platform(platform, port_lanes, split_x2=False):
    lanes_4_spit_x2_x2_lanes = False
    lanes_8_spit_x2_x4_lanes = False
    lanes_8_spit_x4_x2_lanes = False

    four_lanes_x2_split_platforms = [PlatformTypesConstants.PLATFORM_PANTHER,
                                     PlatformTypesConstants.PLATFORM_PANTHER_A1,
                                     PlatformTypesConstants.PLATFORM_TIGON]
    lanes_4_spit_x2_x2_lanes = platform in four_lanes_x2_split_platforms

    eight_lanes_x4_split_platforms = [PlatformTypesConstants.PLATFORM_LEOPARD, PlatformTypesConstants.PLATFORM_MOOSE,
                                      PlatformTypesConstants.PLATFORM_GAUR]

    if platform in [PlatformTypesConstants.PLATFORM_MOOSE] and split_x2:
        lanes_8_spit_x2_x4_lanes = True
    else:
        lanes_8_spit_x4_x2_lanes = platform in eight_lanes_x4_split_platforms

    if len(port_lanes) == 4 and lanes_4_spit_x2_x2_lanes:  # 4 lanes, can be split into x2 with 2 lanes each port
        port_lanes = [','.join(port_lanes[0:2]), ','.join(port_lanes[2:4])]
    if lanes_8_spit_x4_x2_lanes:  # 8 lanes, can be split into x4 with 2 lanes each port
        port_lanes = [','.join(port_lanes[0:2]), ','.join(port_lanes[2:4]),
                      ','.join(port_lanes[4:6]), ','.join(port_lanes[6:8])]
    if len(port_lanes) == 8 and lanes_8_spit_x2_x4_lanes:  # 8 lanes, can be split into x2 with 4 lanes each port
        port_lanes = [','.join(port_lanes[0:4]), ','.join(port_lanes[4:8])]

    return port_lanes


def generate_config_db(config_db, engine, expected_num_of_ports, platform, dut_host_ports, topology):
    # Get DUT physical ports
    physical_dut_ports = get_dut_physical_ports_config(engine, platform)

    # Remove breakout related data
    config_db.pop('BREAKOUT_CFG')
    port_speed = '25000'
    if platform == PlatformTypesConstants.PLATFORM_MOOSE:
        # Remove service port from list of ports which will be split
        physical_dut_ports.pop('Ethernet512')
        port_speed = "100000"
    if platform == PlatformTypesConstants.PLATFORM_LEOPARD:
        port_speed = "100000"
    nonsplitable_ports = get_nonsplitable_ports(platform, topology, physical_dut_ports)
    target_ports = {}
    # Add ports connected from DUT to hosts
    if platform == PlatformTypesConstants.PLATFORM_CHAMELEON_SINGLE_LD:
        port_speed = "200000"
        # Remove the port connected to server, which is not connected to host A and B
        physical_dut_ports.pop("Ethernet4")
        # Remove service ports
        physical_dut_ports.pop("Ethernet512")
        physical_dut_ports.pop("Ethernet513")

    for port in dut_host_ports:
        target_ports[port] = config_db['PORT'][port]
        if port in physical_dut_ports:
            physical_dut_ports.pop(port)

    aliases_list = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']
    added_ports_counter = len(target_ports)
    # Add loopback split ports
    port_index_list = [config_db['PORT'][port]['index'] for port in config_db['PORT']]
    for port in physical_dut_ports:
        port_data = physical_dut_ports[port]
        port_index = port_data['index']
        if port_index not in port_index_list:
            continue
        port_alias = port_data['alias']
        port_speed = port_speed
        port_mtu = '9100'
        port_lanes = port_data['lanes'].split(',')
        if port in nonsplitable_ports:
            if len(port_lanes) == 1:
                port_lanes = port_lanes
            else:
                port_lanes = [','.join(port_lanes)]
        else:
            if (platform in [PlatformTypesConstants.PLATFORM_MOOSE, PlatformTypesConstants.PLATFORM_GAUR] and
                    expected_num_of_ports - added_ports_counter <= 4):
                port_lanes = modify_lanes_per_platform(platform, port_lanes, split_x2=True)
            else:
                port_lanes = modify_lanes_per_platform(platform, port_lanes)

        for lane in port_lanes:
            if expected_num_of_ports != added_ports_counter:

                lane_index = port_lanes.index(lane)
                first_port_lane = lane.split(',')[0]
                # 4600c has specific lanes(not one by one) config: "Ethernet0 0,1,2,3", "Ethernet4 8,9,10,11"
                if platform == PlatformTypesConstants.PLATFORM_TIGON:
                    first_port_lane = int(first_port_lane) // 2

                port_name = f'Ethernet{first_port_lane}'
                alias_symbol = aliases_list[lane_index]
                new_port_alias = f'{port_alias}{alias_symbol}'

                # In case when port with single lane - do not change port alias, use original
                if len(port_lanes) == 1:
                    new_port_alias = port_alias

                port_data = {'index': port_index, 'lanes': lane, 'mtu': port_mtu, 'alias': new_port_alias,
                             'admin_status': 'up', 'speed': port_speed}

                target_ports[port_name] = port_data
                added_ports_counter += 1

    config_db['PORT'] = target_ports

    return config_db


def get_nonsplitable_ports(platform, topology, physical_dut_ports):
    nonsplitable_ports = []
    if platform == PlatformTypesConstants.PLATFORM_LIONFISH:
        for noga_alias, peer_noga_alias in topology.ports_interconnects.items():
            if noga_alias.startswith("dut") and peer_noga_alias.startswith("dut"):
                port = topology.ports[noga_alias]
                peer_port = topology.ports[peer_noga_alias]

                if port in nonsplitable_ports or peer_port in nonsplitable_ports:
                    continue
                if port not in physical_dut_ports or peer_port not in physical_dut_ports:
                    continue

                port_lanes = physical_dut_ports[port]['lanes'].split(',')
                peer_port_lanes = physical_dut_ports[peer_port]['lanes'].split(',')

                if len(port_lanes) != len(peer_port_lanes):
                    nonsplitable_ports.append(port)
                    nonsplitable_ports.append(peer_port)

    return nonsplitable_ports
