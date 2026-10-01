import random
import re
import pytest
import logging

from retry.api import retry_call
from ngts.constants.constants import InterfacesTypeConstants, FecConstants
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from tests.common.plugins.allure_wrapper import allure_step_wrapper as allure
from ngts.helpers.interface_helpers import get_alias_number
from ngts.helpers.general_helper import is_smartswitch_platform

logger = logging.getLogger()


@pytest.fixture(scope='session')
def disable_ssh_client_alive_interval(topology_obj):
    """
    Pytest fixture which are disabling ClientAliveInterval(set it to 0), for prevent SSH session disconnection
    after 15 min without activity. After changed sshd config, we do service ssh restart and reconnect ssh engine
    :param topology_obj: topology object fixture
    """
    engine = topology_obj.players['dut']['engine']
    engine.run_cmd('sudo sed -i "s/ClientAliveInterval 900/ClientAliveInterval 0/g" /etc/ssh/sshd_config')
    engine.run_cmd('sudo service ssh restart')
    engine.disconnect()
    engine.get_engine()

    yield

    engine.run_cmd('sudo sed -i "s/ClientAliveInterval 0/ClientAliveInterval 900/g" /etc/ssh/sshd_config')
    engine.run_cmd('sudo service ssh restart')


@pytest.fixture()
def cleanup_list():
    """
    Fixture to execute cleanup after a test has run
    :return: None
    """
    cleanup_list = []

    yield cleanup_list

    logger.info("------------------test teardown------------------")
    cleanup(cleanup_list)


def cleanup(cleanup_list):
    """
    execute all the functions in the cleanup list
    :return: None
    """
    for cleanup_item in cleanup_list:
        if len(cleanup_item) == 3:  # if **kwargs available
            func, args, kwargs = cleanup_item
            func(*args, **kwargs)
        else:
            func, args = cleanup_item
            func(*args)


def save_configuration_and_reboot(topology_obj, dut_engine, cli_object, ports, cleanup_list, reboot_type):
    """
    save configuration and reboot
    :param dut_engine: ssh connection to dut
    :param cli_object: cli object of dut
    :param ports: list of interfaces to validate after reboot
    :param cleanup_list: a list of functions to be cleaned after the test
    :param reboot_type: i.e reboot/warm-reboot/fast-reboot
    :return: none
    """
    with allure.step('Save configuration and reboot with type: {}'.format(reboot_type)):
        save_configuration(dut_engine, cli_object, cleanup_list)
        logger.info("Reload switch with reboot type: {}".format(reboot_type))
        cli_object.general.reboot_reload_flow(r_type=reboot_type, topology_obj=topology_obj, ports_list=ports)


def save_configuration(dut_engine, cli_object, cleanup_list):
    """
    save configuration
    :param dut_engine: ssh connection to dut
    :param cli_object: cli object of dut
    :param cleanup_list: a list of functions to be cleaned after the test
    :return: none
    """
    logger.info("saving configuration")
    cleanup_list.append((dut_engine.run_cmd, ('sudo config save -y',)))
    cli_object.general.save_configuration()


def compare_actual_and_expected(key, expected_val, actual_val):
    """
    :param key: a string describing what is being compared, "speed"/"status", etc...
    :param expected_val: the expected value
    :param actual_val: the actual value
    :return: raise assertion error in case values does not match
    """
    assert str(expected_val) == str(actual_val), \
        "Compared {} result failed: actual - {}, expected - {}".format(key, actual_val, expected_val)


def convert_speed_format_to_m_speed(split_mode_supported_speeds):
    """
    Convert speed value from platform.json to {speed_value}M - which is used by the test
    :param split_mode_supported_speeds: dictionary, result of parsing platform.json file
    :return: split_mode_supported_speeds with updated speeds in M format, i.e,
    {'Ethernet0': {1: ('100M', '1000M', '10M'),2: (), 4: ()},...}
    """
    split_mode = 1
    for iface, split_speeds_info in split_mode_supported_speeds.items():
        for speed in split_speeds_info[split_mode]:
            if re.match(r"\d+$", speed):
                split_mode_supported_speeds[iface][split_mode].remove(speed)
                split_mode_supported_speeds[iface][split_mode].add(f"{speed}M")


def reboot_reload_random(topology_obj, dut_engine, cli_object, ports, cleanup_list, simx=False):
    """
    Do reload/or reboot by any given way (reboot, fast-reboot, warm-reboot) on dut
    :param topology_obj: topology_obj fixture
    :param dut_engine: a ssh connection to dut
    :param cli_object: a cli object of dut
    :param ports: a ports list on dut to validate after reboot
    :param cleanup_list: a list of cleanup functions that should be called in the end of the test
    :return: raise assertion error in case reload/reboot failed
    """
    supported_reboot_modes = ['reload', 'warm-reboot', 'fast-reboot', 'reboot']
    if is_smartswitch_platform(topology_obj):
        supported_reboot_modes = ['reload', 'reboot']
    if simx:
        supported_reboot_modes = ['reload', 'reboot', 'fast-reboot']

    mode = random.choice(supported_reboot_modes)
    with allure.step('Preforming {} on dut:'.format(mode)):
        logger.info('Saving Configuration and preforming {} on dut:'.format(mode))
        if mode == 'reload':
            save_configuration(dut_engine, cli_object, cleanup_list)
            cli_object.general.reload_flow(ports_list=ports)
        else:
            save_configuration_and_reboot(topology_obj, dut_engine, cli_object, ports, cleanup_list, reboot_type=mode)


@pytest.fixture()
def skip_if_active_optical_cable(mlxcables_info):
    """
    Fixture that skips test execution in case setup has Optical Module
    """
    if re.search(r"Optical\s+(Module|Cable)", mlxcables_info, re.IGNORECASE):
        pytest.skip("This test is not supported because setup has Optical Module")


@pytest.fixture(scope='session')
def mlxcables_info(engines):
    """
    Fixture that init cables and retrieve mlxcables command output
    """
    engines.dut.run_cmd('sudo mst cable add')
    cables_info = engines.dut.run_cmd('sudo mlxcables -q')
    return cables_info


@pytest.fixture()
def skip_if_rj45_cable(cable_compliance_info):
    """
    Fixture that skips test execution in case setup has RJ45 Cable
    """
    if re.search(r"SFP EEPROM is not applicable for RJ45 port", cable_compliance_info, re.IGNORECASE):
        pytest.skip("This test is not supported because setup has RJ45 Cable")


@pytest.fixture(scope='session')
def cable_compliance_info(topology_obj, platform_params, engines, is_simx):
    if not is_simx:
        cables_output = retry_call(check_cable_compliance_info_updated_for_all_port,
                                   fargs=[topology_obj, engines], tries=12, delay=10, logger=logger)
    else:
        cables_output = "No cables info on simx setups"
    return cables_output


def check_cable_compliance_info_updated_for_all_port(topology_obj, engines):
    ports = topology_obj.players_all_ports['dut']
    logger.info("Verify cable compliance info is updated for all ports")
    compliance_info = engines.dut.run_cmd("show interfaces transceiver eeprom")
    for port in ports:
        if re.search("{}: SFP EEPROM is not applicable for RJ45 port".format(port), compliance_info):
            continue
        if not re.search("{}: SFP EEPROM detected".format(port), compliance_info):
            raise AssertionError("Cable Information for port {} is not Loaded by"
                                 " \"show interfaces transceiver eeprom\" cmd".format(port))
    return compliance_info


@pytest.fixture(scope='session')
def dut_ports_default_speeds_configuration(topology_obj, engines, cli_objects):
    ports = topology_obj.players_all_ports['dut']
    return cli_objects.dut.interface.get_interfaces_speed(interfaces_list=ports)


@pytest.fixture(scope='session')
def dut_ports_interconnects(topology_obj):
    """
    :return: a dictionary with all the Noga connectivity for dut ports, i.e.
    {
    'Ethernet4': 'Ethernet8'
    }
    """
    dut_ports_interconnects_dict = {}
    for port_noga_alias, neighbor_port_noga_alias in topology_obj.ports_interconnects.items():
        alias_prefix = port_noga_alias.split('-')[0]
        if alias_prefix == 'dut':
            port = topology_obj.ports[port_noga_alias]
            neighbor_port = topology_obj.ports[neighbor_port_noga_alias]
            dut_ports_interconnects_dict.update({port: neighbor_port})
    return dut_ports_interconnects_dict


@pytest.fixture(scope='session')
def interfaces_status_dict(engines, cli_objects):
    """
    Get and parse show interfaces status output
    :param engines: engines fixture
    :param cli_objects:  cli objects fixture
    :return: dictionary with parsed output
    """
    interfaces_status_dict = cli_objects.dut.interface.parse_interfaces_status()
    return interfaces_status_dict


@pytest.fixture(scope='session')
def physical_interfaces_types_dict(interfaces_status_dict):
    """
    Get physical interfaces type dictionary
    :param interfaces_status_dict: dictionary with parsed output of "show interfaces status"
    :return: dictionary, example: {"Ethernet0": "RJ45", "Ethernet1": "RJ45", ...}
    """
    interfaces_types_dict = {}
    for port, port_status in interfaces_status_dict.items():
        interfaces_types_dict[port] = port_status['Type']
    return interfaces_types_dict


@pytest.fixture(scope='session')
def rj45_ports_list(physical_interfaces_types_dict):
    """
    Get rj45 ports list
    :param physical_interfaces_types_dict: dictionary, example: {"Ethernet0": "RJ45", "Ethernet1": "RJ45", ...}
    :return: list with rj45 ports, example: ['Ethernet0', 'Ethernet1', 'Ethernet2'...]
    """
    rj_45_ports_list = []
    for port in physical_interfaces_types_dict:
        if physical_interfaces_types_dict[port] == InterfacesTypeConstants.RJ45:
            rj_45_ports_list.append(port)

    return rj_45_ports_list


@pytest.fixture(scope='session')
def sfp_ports_list(physical_interfaces_types_dict):
    """
    Get SFP ports list
    :param physical_interfaces_types_dict: dictionary, example: {"Ethernet0": "RJ45", "Ethernet1": "QSFP", ...}
    :return: list with SFP ports, example: ['Ethernet1', 'Ethernet2'...]
    """
    spf_ifaces_list = []
    for port in physical_interfaces_types_dict:
        if 'SFP' in physical_interfaces_types_dict[port]:
            spf_ifaces_list.append(port)

    return spf_ifaces_list


@pytest.fixture(autouse=False, scope='session')
def fec_modes_speed_support(chip_type, platform_params):
    if chip_type == "SPC":
        return FecConstants.FEC_MODES_SPC_SPEED_SUPPORT
    elif chip_type == "SPC2":
        return FecConstants.FEC_MODES_SPC2_SPEED_SUPPORT[platform_params.filtered_platform.upper()]
    elif chip_type == "SPC3":
        return FecConstants.FEC_MODES_SPC3_SPEED_SUPPORT[platform_params.filtered_platform.upper()]
    elif chip_type == "SPC4":
        return FecConstants.FEC_MODES_SPC4_SPEED_SUPPORT[platform_params.filtered_platform.upper()]
    elif chip_type in ("SPC5", "SPC6"):
        return FecConstants.FEC_MODES_SPEED_SUPPORT_NOT_AVAILABLE
    else:
        raise AssertionError("Chip type {} is unrecognized".format(chip_type))


@pytest.fixture(autouse=False, scope='session')
def dut_ports_number_dict(topology_obj, engines, cli_objects):
    dut_ports_number_dict = {}
    ports = topology_obj.players_all_ports['dut']
    ports_aliases_dict = cli_objects.dut.interface.parse_ports_aliases_on_sonic()
    for port in ports:
        dut_ports_number_dict[port] = get_alias_number(ports_aliases_dict[port])
    return dut_ports_number_dict


@pytest.fixture(scope='session')
def is_sw_control_feature_enabled(cli_objects):
    return cli_objects.dut.im.is_ms_hwsku() and cli_objects.dut.im.is_im_enabled()


@pytest.fixture(scope='session')
def sw_control_ports(cli_objects, is_sw_control_feature_enabled, dut_ports_number_dict):
    if is_sw_control_feature_enabled:
        return cli_objects.dut.im.get_ports_supporting_im(dut_ports_number_dict)


@pytest.fixture(scope='module')
def disable_rsyslog_ratelimit(engines, request):
    dut_engine = engines.dut
    container_name = request.param
    cmd_comment_interval = rf'docker exec -i {container_name} sed -e "s/\$SystemLogRateLimitInterval/\#\$SystemLogRateLimitInterval/g" -i /etc/rsyslog.conf'
    cmd_comment_burst = rf'docker exec -i {container_name} sed -e "s/\$SystemLogRateLimitBurst/\#\$SystemLogRateLimitBurst/g" -i /etc/rsyslog.conf'

    cmd_restart_rsyslogd = f'docker exec -i {container_name} supervisorctl restart rsyslogd'

    cmd_uncomment_interval = rf'docker exec -i {container_name} sed -e "s/\#\$SystemLogRateLimitInterval/\$SystemLogRateLimitInterval/g" -i /etc/rsyslog.conf '
    cmd_uncomment_burst = rf'docker exec -i {container_name} sed -e "s/\#\$SystemLogRateLimitBurst/\$SystemLogRateLimitBurst/g" -i /etc/rsyslog.conf'

    with allure.step(f'Disabling rate limit for rsyslogd in {container_name} docker'):
        dut_engine.run_cmd(cmd_comment_interval)
        dut_engine.run_cmd(cmd_comment_burst)
        dut_engine.run_cmd(cmd_restart_rsyslogd)
    yield
    with allure.step(f'Enabling rate limit for rsyslogd in {container_name} docker'):
        dut_engine.run_cmd(cmd_uncomment_interval)
        dut_engine.run_cmd(cmd_uncomment_burst)
        dut_engine.run_cmd(cmd_restart_rsyslogd)
