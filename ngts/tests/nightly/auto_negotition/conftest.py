import logging
import pytest
import os
import re
import json

from ngts.constants.constants import InterfacesTypeConstants, PlatformTypesConstants, SonicConst, CableComplianceConst
from ngts.tests.conftest import get_dut_loopbacks, get_dut_host_loopbacks
from ngts.helpers.interface_helpers import get_lb_mutual_speed, speed_string_to_int_in_mb
from ngts.cli_util.cli_parsers import parse_show_interfaces_transceiver_eeprom

logger = logging.getLogger()

NO_LOOPBACKS_SKIP_REASON = 'The DUT has no appropriate loopbacks to run the test with.'


@pytest.fixture(scope='module', autouse=True)
def auto_neg_configuration(topology_obj, setup_name, engines, cli_objects, platform_params, is_air):
    """
    Pytest fixture which will clean all fec configuration leftover from the dut

    """
    cli_objects.dut.engine.run_cmd("sudo cp /etc/sonic/config_db.json /etc/sonic/config_db.json.bak")

    yield

    logger.info('Starting Auto Neg configuration cleanup')
    cli_objects.dut.engine.run_cmd("sudo cp /etc/sonic/config_db.json.bak /etc/sonic/config_db.json")
    cli_objects.dut.general.reload_flow(topology_obj=topology_obj, reload_force=True)

    logger.info('Auto Neg cleanup completed')


@pytest.fixture(scope='session')
def available_lb_dict(topology_obj, split_mode_supported_speeds, ports_spec_compliance):
    """
    Same selection as tested_lb_dict, but does not skip when empty so CLI-only tests can still run.
    :param topology_obj: topology object fixture
    :param ports_spec_compliance: ports_spec_compliance fixture
    :return: a dictionary of loopback list for each split mode on the dut
    {1: [('Ethernet52', 'Ethernet56')],
    2: [('Ethernet12', 'Ethernet16')],
    4: [('Ethernet20', 'Ethernet24')]}
    """
    tested_lb_dict = {1: []
                      }
    update_split_2_if_possible(topology_obj, tested_lb_dict, ports_spec_compliance)
    update_split_3_if_possible(topology_obj, split_mode_supported_speeds, tested_lb_dict, ports_spec_compliance)
    update_split_4_if_possible(topology_obj, split_mode_supported_speeds, tested_lb_dict, ports_spec_compliance)
    update_split_8_if_possible(topology_obj, split_mode_supported_speeds, tested_lb_dict, ports_spec_compliance)
    split_mode = 1
    dut_lbs = list(filter(lambda lb: is_auto_neg_supported_lb(lb, ports_spec_compliance),
                          get_dut_loopbacks(topology_obj)))
    if dut_lbs:
        tested_lb_dict[split_mode].append(get_dut_lb_with_max_capability(dut_lbs, split_mode_supported_speeds))
    return tested_lb_dict


@pytest.fixture(scope='session')
def tested_lb_dict(available_lb_dict):
    """
    :param available_lb_dict: available_lb_dict fixture
    :return: a dictionary of loopback list for each split mode on the dut
    """
    if not any(available_lb_dict.values()):
        pytest.skip(NO_LOOPBACKS_SKIP_REASON)
    logger.info("Tests will run on the following ports :\n{}".format(available_lb_dict))
    return available_lb_dict


def get_dut_lb_with_max_capability(dut_lbs, split_mode_supported_speeds):
    return max(dut_lbs,
               key=lambda lb: speed_string_to_int_in_mb(max(get_lb_mutual_speed(lb, 1, split_mode_supported_speeds),
                                                            key=speed_string_to_int_in_mb)))


def get_split_lb_ports(topology_obj, split_mode):
    """
    Resolve the loopback pair of the first sub-ports of a split loopback.
    Setups name split loopback aliases either without a loopback index (dut-lb-splt2-p1-1) or
    with one (dut-lb1-splt2-p1-1), so both conventions are looked up, index free first.
    :param topology_obj: topology object fixture
    :param split_mode: the port split mode i.e, 2/3/4/8
    :return: a tuple of the two loopback ports, or None when the setup has no such loopback
    """
    alias_pattern = re.compile(r"^dut-lb(?P<index>\d*)-splt{}-p1-1$".format(split_mode))
    matched_aliases = [alias for alias in topology_obj.ports if alias_pattern.match(alias)]
    for alias in sorted(matched_aliases,
                        key=lambda alias: int(alias_pattern.match(alias).group('index') or 0)):
        peer_alias = alias.replace('-p1-1', '-p2-1')
        port, peer_port = topology_obj.ports.get(alias), topology_obj.ports.get(peer_alias)
        if port and peer_port:
            return port, peer_port
    return None


@pytest.fixture(scope='session')
def even_split_lb_dict(topology_obj):
    """
    Even-split loopbacks including optics (no auto-neg filter). Split-3 is not included.
    :param topology_obj: topology object fixture
    :return: a dictionary of even-split loopbacks for split modes 1/2/4/8
    """
    lbs = {1: []}
    try:
        lbs[1] = [lb for lb in get_dut_loopbacks(topology_obj) if all(lb)]
    except KeyError:
        logger.warning("Incomplete split-1 loopback aliases in topology")
    for split_mode in (SonicConst.PORT_SPLIT_NUM_2, SonicConst.PORT_SPLIT_NUM_4, SonicConst.PORT_SPLIT_NUM_8):
        split_lb = get_split_lb_ports(topology_obj, split_mode)
        if split_lb:
            lbs[split_mode] = [split_lb]
    return lbs


@pytest.fixture(scope='session')
def negative_cli_port(topology_obj, interfaces, even_split_lb_dict, ports_spec_compliance):
    """
    DUT port for negative CLI checks. Prefer a loopback port that does not support auto-neg, so that
    setups whose loopbacks are all uneven split are covered too, and default to the dut host port.
    :param topology_obj: topology object fixture
    :param interfaces: interfaces fixture
    :param even_split_lb_dict: even_split_lb_dict fixture
    :param ports_spec_compliance: ports_spec_compliance fixture
    :return: port name
    """
    uneven_split_lb = get_split_lb_ports(topology_obj, SonicConst.PORT_SPLIT_NUM_3)
    lb_lists = [even_split_lb_dict.get(split_mode, [])
                for split_mode in (SonicConst.PORT_SPLIT_NUM_2, SonicConst.PORT_SPLIT_NUM_4,
                                   SonicConst.PORT_SPLIT_NUM_8, SonicConst.PORT_SPLIT_NUM_1)]
    lb_lists.append([uneven_split_lb] if uneven_split_lb else [])
    lb_ports = (port for lb_list in lb_lists for lb in lb_list for port in lb
                if port in ports_spec_compliance)
    for port in lb_ports:
        if not is_auto_neg_supported_port(port, ports_spec_compliance, used_in_auto_neg_tests=False):
            logger.info("Negative CLI checks will use port {}".format(port))
            return port
    return interfaces.dut_ha_1


@pytest.fixture(scope='session')
def ports_lanes_dict(interfaces_status_dict, interfaces, cli_objects):
    ports_lanes_dict = {}
    for port, port_status in interfaces_status_dict.items():
        lanes = port_status['Lanes'].split(sep=",")
        ports_lanes_dict[port] = len(lanes)
    ports_lanes_dict[interfaces.ha_dut_1] = 4
    return ports_lanes_dict


def update_split_3_if_possible(topology_obj, split_mode_supported_speeds, tested_lb_dict, ports_spec_compliance):
    """
    :param topology_obj: topology object fixture
    :param split_mode_supported_speeds: a dictionary with available speed options for each split mode on all setup ports
    :param tested_lb_dict: a dictionary of loopback list for each split mode on the dut
    :param ports_spec_compliance: ports_spec_compliance fixture
    :return: Update loopback with split 3 (uneven split) configuration only in cases where there are mutual speeds
    available, e.g. on platforms such as Mellanox-SN6600_LD-P64O128C2
    """
    split_3 = SonicConst.PORT_SPLIT_NUM_3
    split_3_lb = get_split_lb_ports(topology_obj, split_3)
    if split_3_lb:
        if not all(split_mode_supported_speeds.get(port, {}).get(split_3) for port in split_3_lb):
            logger.info("Skipping split-3 lb %s: mode 3 is not supported on these ports/platform", split_3_lb)
            return
        mutual_speeds = get_lb_mutual_speed(split_3_lb, split_3, split_mode_supported_speeds)
        if len(mutual_speeds) > 1 and is_auto_neg_supported_lb(split_3_lb, ports_spec_compliance):
            tested_lb_dict.update({split_3: [split_3_lb]})


def update_split_4_if_possible(topology_obj, split_mode_supported_speeds, tested_lb_dict, ports_spec_compliance):
    """
    :param topology_obj: topology object fixture
    :param split_mode_supported_speeds: a dictionary with available speed options for each split mode on all setup ports
    :param tested_lb_dict: a dictionary of loopback list for each split mode on the dut
    :param ports_spec_compliance: ports_spec_compliance fixture
    :return: Update loopback with split 4 configuration only in cases were there are mutual speeds available,
    for example, parsing of platform.json file for panther will not return speeds option for port with split 4,
    because this breakout mode is not supported on panther
    """
    if topology_obj.ports.get('dut-lb-splt4-p1-1') and topology_obj.ports.get('dut-lb-splt4-p2-1'):
        split_4_lb = (topology_obj.ports['dut-lb-splt4-p1-1'], topology_obj.ports['dut-lb-splt4-p2-1'])
        mutual_speeds = get_lb_mutual_speed(split_4_lb, 4, split_mode_supported_speeds)
        if mutual_speeds and is_auto_neg_supported_lb(split_4_lb, ports_spec_compliance):
            tested_lb_dict.update({4: [split_4_lb]})


def update_split_2_if_possible(topology_obj, tested_lb_dict, ports_spec_compliance):
    """
    :param topology_obj: topology object fixture
    :param tested_lb_dict: a dictionary of loopback list for each split mode on the dut
    :param ports_spec_compliance: ports_spec_compliance fixture
    :return: Update loopback with split 2 configuration only in cases were there is such a loopback in setup
    (some simx setups don't have split ports as part of the configuration)
    """
    if topology_obj.ports.get('dut-lb-splt2-p1-1') and topology_obj.ports.get('dut-lb-splt2-p2-1'):
        split_2_lb = (topology_obj.ports['dut-lb-splt2-p1-1'], topology_obj.ports['dut-lb-splt2-p2-1'])
        if is_auto_neg_supported_lb(split_2_lb, ports_spec_compliance):
            tested_lb_dict.update({2: [split_2_lb]})


def update_split_8_if_possible(topology_obj, split_mode_supported_speeds, tested_lb_dict, ports_spec_compliance):
    """
    :param topology_obj: topology object fixture
    :param split_mode_supported_speeds: a dictionary with available speed options for each split mode on all setup ports
    :param tested_lb_dict: a dictionary of loopback list for each split mode on the dut
    :param ports_spec_compliance: ports_spec_compliance fixture
    :return: Update loopback with split 8 configuration only in cases were there are mutual speeds available,
    for example, parsing of platform.json file for panther will not return speeds option for port with split 8,
    because this breakout mode is not supported on panther
    """
    if topology_obj.ports.get('dut-lb-splt8-p1-1') and topology_obj.ports.get('dut-lb-splt8-p2-1'):
        split_8_lb = (topology_obj.ports['dut-lb-splt8-p1-1'], topology_obj.ports['dut-lb-splt8-p2-1'])
        mutual_speeds = get_lb_mutual_speed(split_8_lb, 8, split_mode_supported_speeds)
        if mutual_speeds and is_auto_neg_supported_lb(split_8_lb, ports_spec_compliance):
            tested_lb_dict.update({8: [split_8_lb]})


@pytest.fixture(scope='session')
def tested_dut_host_lb_dict(topology_obj, interfaces, split_mode_supported_speeds, ports_spec_compliance):
    """
    :param topology_obj: topology object fixture
    :param ports_spec_compliance - ports_spec_compliance fixture
    :return: a dictionary of loopback of dut - host ports connectivity
    {1: [('Ethernet64', 'enp66s0f0')]}
    """
    tested_dut_host_lb_dict = dict()
    for dut_host_lb in get_dut_host_loopbacks(interfaces):
        if is_auto_neg_supported_port(dut_host_lb[0], ports_spec_compliance):
            tested_dut_host_lb_dict[1] = [dut_host_lb]
            break
    if not tested_dut_host_lb_dict:
        pytest.skip("Skipping test as there are no auto-neg supporting dut_host loopbacks")
    logger.info("Test will run on the following ports:\n{}".format(tested_dut_host_lb_dict))
    return tested_dut_host_lb_dict


@pytest.fixture(scope='session')
def interfaces_types_dict(engines, platform_params, chip_type):
    """

    """
    platform = platform_params.filtered_platform
    logger.info("platform is: {}".format(platform))
    if chip_type == "SPC":
        if platform.upper() == PlatformTypesConstants.FILTERED_PLATFORM_PANTHER:
            if is_aoc_cable(engines):
                supported_speed = InterfacesTypeConstants.INTERFACE_TYPE_SUPPORTED_SPEEDS_SPC.get(
                    platform.upper()).get("PANTHER_AOC")
            else:
                supported_speed = InterfacesTypeConstants.INTERFACE_TYPE_SUPPORTED_SPEEDS_SPC['default']
        else:
            supported_speed = InterfacesTypeConstants.INTERFACE_TYPE_SUPPORTED_SPEEDS_SPC.get(
                platform.upper())
        if not supported_speed:
            supported_speed = InterfacesTypeConstants.INTERFACE_TYPE_SUPPORTED_SPEEDS_SPC['default']

    elif chip_type == "SPC2":
        supported_speed = InterfacesTypeConstants.INTERFACE_TYPE_SUPPORTED_SPEEDS_SPC2[platform.upper()]
    elif chip_type == "SPC3":
        supported_speed = InterfacesTypeConstants.INTERFACE_TYPE_SUPPORTED_SPEEDS_SPC3[platform.upper()]
    elif chip_type in ("SPC4", "SPC5", "SPC6"):
        supported_speed = {}
    else:
        raise AssertionError("Chip type {} is unrecognized".format(chip_type))
    return supported_speed


@pytest.fixture(scope='session')
def interfaces_types_port_dict(engines, cli_objects, platform_params, chip_type, ports_aliases_dict, interfaces,
                               interfaces_types_dict):
    """
    get the supported interface type with the sdk api
    example of the return value of the get_supported_intf_type.py:
    {'1': {'CR': ['1G', '10G', '25G', '50G'], 'CR2': ['50G', '100G'], 'CR4': ['40G', '100G', '200G']},
     '2': {'CR': ['1G', '10G', '25G', '50G'], 'CR2': ['50G', '100G'], 'CR4': ['40G', '100G', '200G']},
     '3': {'CR': ['1G', '10G', '25G', '50G'], 'CR2': ['50G', '100G'], 'CR4': ['40G', '100G', '200G']},
     '4': {'CR': ['1G', '10G', '25G', '50G'], 'CR2': ['50G', '100G']},
     '5': {'CR': ['1G', '10G', '25G', '50G'], 'CR2': ['50G', '100G']},
     '6': {'CR': ['1G', '10G', '25G', '50G']},
     '7': {'CR': ['1G', '10G', '25G', '50G']},
     '8': {'CR': ['1G', '10G', '25G', '50G'], 'CR2': ['50G', '100G'], 'CR4': ['40G', '100G', '200G']},
     ...
     }
    """
    platform = platform_params.filtered_platform
    supported_speed = {}
    # For SPC1, the sdk dose not support to get port rate cap .
    if chip_type == "SPC":
        if platform.upper() == PlatformTypesConstants.FILTERED_PLATFORM_PANTHER:
            if is_aoc_cable(engines):
                port_supported_speed = InterfacesTypeConstants.INTERFACE_TYPE_SUPPORTED_SPEEDS_SPC.get(
                    platform.upper()).get("PANTHER_AOC")
            else:
                port_supported_speed = InterfacesTypeConstants.INTERFACE_TYPE_SUPPORTED_SPEEDS_SPC['default']
        else:
            port_supported_speed = InterfacesTypeConstants.INTERFACE_TYPE_SUPPORTED_SPEEDS_SPC.get(
                platform.upper())
        if not port_supported_speed:
            port_supported_speed = InterfacesTypeConstants.INTERFACE_TYPE_SUPPORTED_SPEEDS_SPC['default']
        for intf, alias in ports_aliases_dict.items():
            supported_speed[intf] = port_supported_speed
    else:
        base_dir = os.path.dirname(os.path.realpath(__file__))
        get_port_cap_file_name = "get_port_supported_intf_type.py"
        get_port_cap_file = os.path.join(base_dir, f"{get_port_cap_file_name}")
        engines.dut.copy_file(source_file=get_port_cap_file,
                              file_system='/tmp',
                              dest_file=get_port_cap_file_name,
                              overwrite_file=True, )
        cmd_copy_file_to_syncd = f"docker cp /tmp/{get_port_cap_file_name} syncd:/"
        engines.dut.run_cmd(cmd_copy_file_to_syncd)

        port_cap = engines.dut.run_cmd(f'docker exec syncd bash -c "python3 /{get_port_cap_file_name}"')
        port_cap = json.loads(port_cap.replace("\'", "\""))

        for intf, alias in ports_aliases_dict.items():
            regex_pattern = r"etp(\d+)\w*"
            label_port = re.match(regex_pattern, alias).group(1)
            supported_intf_type = port_cap[label_port]
            supported_speed[intf] = {}
            for intf_type, speeds in supported_intf_type.items():
                if not intf_type[2:]:
                    lane_num = 1
                else:
                    lane_num = int(intf_type[2:])
                supported_speed[intf][lane_num] = {intf_type: speeds}
    supported_speed[interfaces.ha_dut_1] = supported_speed[interfaces.dut_ha_1]
    return supported_speed


@pytest.fixture(autouse=True, scope='session')
def ports_aliases_dict(engines, cli_objects):
    """
    :param engines: setup engines fixture
    :param cli_objects: cli objects fixture
    :return: a dictionary of the port and it's sonic alias on the dut
    i.e, {'Ethernet0': 'etp1', ..
          'Ethernet60': 'etp16'}
    """
    return cli_objects.dut.interface.parse_ports_aliases_on_sonic()


def get_interface_cable_width(type_string, expected_speed=None):
    """
    :param type_string: a interface type string 'CR'
    :param expected_speed: if the expected speed is 400G the width of the cable will be 8,
                           although the cable type will be CR4 - this is a sonic limitation
    :return: int width  value, i.e, '1'
    more examples,
    'CR' -> 1
    'CR2' -> 2
    'CR4' -> 4
    """
    width = 1
    match = re.search(r"\w+(\d+)", type_string)
    if match:
        width = int(match.group(1))
    return width


def get_matched_types(lane_number, speed_list, types_dict):
    """
    :param lane_number: the port number of lanes i.e, 1,2,4 etc
    :param speed_list: set of speeds, {'40G', '25G', '50G', '10G'}
    :param types_dict: a dictionary of port supported types based on
    the port number of lanes
    i.e,
    {
        SonicConst.PORT_LANE_NUM_1: {'CR': ['1G', '10G', '25G']},
        SonicConst.PORT_LANE_NUM_2: {'CR2': ['50G']},
        SonicConst.PORT_LANE_NUM_4: {'CR4': ['40G', '100G']},
    }
    :return: a set of types that match speeds in the list, i.e, {'CR', 'CR4'}
    """
    matched_types = set()
    lane_option = list(filter(lambda lane_option: lane_option <= lane_number, [1, 2, 4, 8]))
    for speed in speed_list:
        for lane in lane_option:
            if lane in types_dict:
                for interface_type, supported_speeds in types_dict[lane].items():
                    if speed in supported_speeds:
                        matched_types.add(interface_type)
    return matched_types


def convert_speeds_to_mb_format(speeds_list):
    """
    :param speeds_list: a list of speeds,  ['40G', '10G', '50G']
    :return: return a string of speeds configuration in string list format, i.e, '40000,10000,50000'
    """
    speeds_in_mb_format = list(map(lambda speed: str(speed_string_to_int_in_mb(speed)), speeds_list))
    return ",".join(speeds_in_mb_format)


@pytest.fixture(autouse=False)
def ignore_main_loganalyzer(cli_objects, loganalyzer):
    """
    :param cli_objects: cli objects fixture
    :param loganalyzer: loganalyzer fixture
    :return: None
    """
    dut_hostname = cli_objects.dut.chassis.get_hostname()
    if loganalyzer:
        loganalyzer[dut_hostname].add_start_ignore_mark()
    yield
    if loganalyzer:
        loganalyzer[dut_hostname].add_end_ignore_mark()


def get_all_advertised_speeds_sorted_string(speeds_list, physical_interface_type=None):
    """
    :param speeds_list: a list of speeds ['10000', '50000']
    :param physical_interface_type: physical interface type
    :return: return a string of sorted speeds configuration in M/G format, i.e, "10G,50G"
    """
    speeds_list = sorted(speeds_list, key=lambda speed_str: int(speed_str))
    if physical_interface_type == 'RJ45':
        speeds_in_str_format = list(map(lambda speed: "{}M".format(int(speed)), speeds_list))
    else:
        speeds_in_str_format = list(map(lambda speed: "{}G".format(int(int(speed) / 1000)), speeds_list))
    return ",".join(speeds_in_str_format)


def is_aoc_cable(engines):
    extended_spc = engines.dut.run_cmd(
        "show interfaces transceiver eeprom Ethernet0 | grep \"Extended Specification Compliance\"")
    if re.search("AOC", extended_spc):
        return True
    return False


@pytest.fixture(scope='session')
def ports_spec_compliance(topology_obj, engines, cable_compliance_info, is_simx):
    """
    The function parses the information of the command show interfaces transceiver eeprom to a dictionary that contains
    the compliance eeprom data for each port.
    :param topology_obj: topology_obj fixture
    :param engines: engines fixture
    :param cable_compliance_info: cable_compliance_info fixture
    :param is_simx: is_simx fixture
    :return: A dictionary mapping each port to a tuple of 2 values. The first value is the specification compliance
    type (extended or not) and the second value is the actual specification compliance value.
    It should be noted that eeprom_info_per_port contains the entire eeprom output of a port,
    so if it is needed for future usage, the function can be adjusted to return it as well.
    Example for entry in the dictionary - {"Ethernet8": ("Specification compliance", "passive_copper_media_interface")}
    """
    # If the platform is simx, we do not have port compliance and cannot parse it
    # Currently, auto-neg tests do not run on simx setups but when using this fixture in fec, we need to know if it's
    # simx
    if is_simx:
        return None
    eeprom_info_per_port = parse_show_interfaces_transceiver_eeprom(cable_compliance_info)
    ports_compliance = dict()
    for port_name, port_info in eeprom_info_per_port.items():
        # RJ45 cables shouldn't be skipped on all auto-neg cases despite not having eeprom data
        if port_info['Status'] == CableComplianceConst.UNDETECTED_RJ45_EEPROM_MSG:
            ports_compliance[port_name] = CableComplianceConst.DEFAULT_CABLE_COMPLIANCE_TUPLE
        else:
            ports_compliance[port_name] = parse_port_compliance(port_info)
    return ports_compliance


def parse_port_compliance(port_info):
    """
    The function parses the port compliance information from the output of "show interfaces transceiver eeprom" command
    :param port_info:  The port information, as it appears in the output of "show interfaces transceiver eeprom"
    :return: A tuple of 2 values. The first value is the specification compliance
    type (extended or not) and the second value is the actual specification compliance value.
    Example for return value - ("Specification compliance", "passive_copper_media_interface")
    """
    port_compliance = get_port_compliance(port_info)
    if isinstance(port_compliance, str):
        port_compliance_type = CableComplianceConst.SPEC_COMPLIANCE_PREFIX
    else:  # This means the compliance is a dict
        port_compliance_type, port_compliance = parse_port_compliance_dict(port_compliance)
    return port_compliance_type, port_compliance


def get_port_compliance(port_info):
    """
    The function returns the port compliance.
    :param port_info: The port information, as it appears in the output of "show interfaces transceiver eeprom"
    :return: The port compliance, which can either be a single value or a dictionary.
    """
    if CableComplianceConst.SPEC_COMPLIANCE_PREFIX in port_info.keys():
        port_compliance = port_info[CableComplianceConst.SPEC_COMPLIANCE_PREFIX]
    else:
        # In the case the port compliance is a dictionary, the function parse_show_interfaces_transceiver_eeprom parses
        # it with the ':'
        port_compliance = port_info[f'{CableComplianceConst.SPEC_COMPLIANCE_PREFIX}:']
    return port_compliance


def parse_port_compliance_dict(port_compliance_dict):
    """
    The function parses the port compliance in the case it is a dictionary.
    :param port_compliance_dict: The port compliance dictionary, as it appears in "show interfaces transceiver eeprom"
    :return: A tuple of 2 values, containing the port compliance type and port compliance value. Supported types can be
    seen in the keys of CableComplianceConst.SUPPORTED_SPECIFICATION_COMPLIANCE
    """
    port_compliance_type = None
    port_compliance = None
    if CableComplianceConst.EXTENDED_SPEC_COMPLIANCE_PREFIX in port_compliance_dict.keys():
        port_compliance_type = CableComplianceConst.EXTENDED_SPEC_COMPLIANCE_PREFIX
        port_compliance = port_compliance_dict[port_compliance_type]
    elif CableComplianceConst.SFP_COMPLIANCE in port_compliance_dict.keys():
        port_compliance_type = CableComplianceConst.SFP_COMPLIANCE
        port_compliance = port_compliance_dict[port_compliance_type]
    return port_compliance_type, port_compliance


def is_auto_neg_supported_port(port, compliance_info_per_port, used_in_auto_neg_tests=True):
    """
    The function returns True if the port supports auto-neg and false otherwise.
    :param port: port to check
    :param compliance_info_per_port: output of compliance_info_per_port fixture
    :param used_in_auto_neg_tests: a boolean stating if the function is called from the auto-neg test (and thus port is skipped)
    :return: True if the port supports auto-neg and False otherwise
    """
    port_supports_auto_neg = False
    port_spec_type, port_spec_value = compliance_info_per_port[port]

    # eeprom contains empty values for Specification compliance, test will not run on the port
    if port_spec_value == "Unknown":
        return port_supports_auto_neg

    matched_supported_regex = any(re.search(regex_pattern, port_spec_value) for regex_pattern in
                                  CableComplianceConst.SUPPORTED_SPECIFICATION_COMPLIANCE[port_spec_type])
    if matched_supported_regex:
        logger.debug(f"Supported port is - {port}. {port_spec_type} : {port_spec_value}\n")
        port_supports_auto_neg = True
    if not port_supports_auto_neg:
        matched_unsupported_regex = any(re.search(regex_pattern, port_spec_value) for regex_pattern in
                                        CableComplianceConst.UNSUPPORTED_SPECIFICATION_COMPLIANCE[port_spec_type])
        if matched_unsupported_regex:
            if used_in_auto_neg_tests:
                logger.warning(f"Tests will not run on port {port} because of unsupported cable type - "
                               f"{port_spec_type} : {port_spec_value}\n")
        else:
            logger.warning(f"Tests will not run on port {port} because of unknown cable type - "
                           f"{port_spec_type} : {port_spec_value}\n")
            return port_supports_auto_neg
    return port_supports_auto_neg


def is_auto_neg_supported_lb(lb, compliance_info_per_port):
    """
    The function returns True if the both ports of the lb support auto-neg and false otherwise.
    :param lb: lb to check
    :param compliance_info_per_port: output of compliance_info_per_port fixture
    :return: True if the lb supports auto-neg and False otherwise
    """
    return all(is_auto_neg_supported_port(port, compliance_info_per_port) for port in lb)
