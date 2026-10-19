import re
import logging
from tests.common.platform.interface_utils import get_port_map
import functools
from collections import defaultdict

DICT_WRITABLE_BYTE_FOR_PAGE_0 = {
    "cmis":  33,
    "sff8472": 110,
    "sff8636": 86}

MGMT_TYPE_BY_LONG_TYPE = {
    # SFF‑8472 (SFP family)
    "Module/connector soldered to motherboard": "sff8472",
    "SFP/SFP+/SFP28":                           "sff8472",

    # SFF‑8636 (QSFP family)
    "QSFP":                                     "sff8636",
    "QSFP+ or later with SFF-8636 or SFF-8436": "sff8636",
    "QSFP28 or later":                          "sff8636",

    # CMIS (QSFP-DD / OSFP / SFP-DD / DSFP / QSFP+ CMIS)
    "QSFP-DD Double Density 8X Pluggable Transceiver":   "cmis",
    "OSFP 8X Pluggable Transceiver":                     "cmis",
    "SFP-DD Double Density 2X Pluggable Transceiver":    "cmis",
    "DSFP Dual Small Form Factor Pluggable Transceiver": "cmis",
    "QSFP+ or later with CMIS":                          "cmis",
}


def parse_output(output_lines):
    """
    @summary: For parsing command output. The output lines should have format 'key value'.
    @param output_lines: Command output lines
    @return: Returns result in a dictionary
    """
    res = {}
    for line in output_lines:
        fields = line.split()
        if len(fields) < 2:
            continue
        res[fields[0]] = line.replace(fields[0], '').strip()
    return res


def parse_eeprom(output_lines):
    """
    @summary: Parse the SFP eeprom information from command output
    @param output_lines: Command output lines
    @return: Returns result in a dictionary
    """
    res = {}
    for line in output_lines:
        if re.match(r"^Ethernet\d+: .*", line):
            fields = line.split(":")
            res[fields[0]] = fields[1].strip()
    return res


def parse_eeprom_hexdump(data):
    # Define a regular expression to capture all required data
    regex = re.compile(
        r"EEPROM hexdump for port (\S+)\n"  # Capture port name
        r"(?:\s+)?"  # Match and skip intermediate lines
        r"((?:Lower|Upper) page \S+|\S+ dump)\n"  # Capture full page type string
        r"((?:\s+[0-9a-fA-F]{8}(?: [0-9a-fA-F]{2}){8} (?: [0-9a-fA-F]{2}){8} .*\n)+)"  # Capture hex data block
    )
    # Dictionary to store parsed results
    parsed_data = {}

    # Find all matches in the data
    matches = regex.findall(data)
    for port, page_type, hex_data in matches:
        if port not in parsed_data:
            parsed_data[port] = {}

        # Parse hex data block into individual hex values
        hex_lines = hex_data.splitlines()
        hex_values = [
            value
            for line in hex_lines
            for value in line[9:56].split()  # Extract hex bytes from columns 9-56
        ]

        parsed_data[port][page_type] = hex_values

    return parsed_data


def get_dev_conn(duthost, conn_graph_facts, asic_index):
    if duthost.facts.get("asic_type") == "vs":
        dev_conn = conn_graph_facts.get("device_conn", {}).get(duthost.hostname, {})
    else:
        dev_conn = conn_graph_facts["device_conn"][duthost.hostname]

    # Get the interface pertaining to that asic
    portmap = get_port_map(duthost, asic_index)
    logging.info("Got portmap {}".format(portmap))

    if asic_index is not None:
        # Check if the interfaces of this AISC is present in conn_graph_facts
        dev_conn = {k: v for k, v in list(portmap.items()) if k in dev_conn}
        logging.info("ASIC {} interface_list {}".format(asic_index, dev_conn))

    return portmap, dev_conn


def validate_transceiver_lpmode(sfp_lpmode, port):
    lpmode = sfp_lpmode.get(port)
    if lpmode is None:
        logging.error(f"Interface {port} does not present in the show command")
        return False

    if lpmode not in ["Off", "On"]:
        logging.error("Invalid low-power mode {} for port {}".format(lpmode, port))
        return False

    return True


def write_eeprom_by_page_and_byte(
        duthost, port, sfp_type, data, page, offset, is_verify=False, module_ignore_errors=False):
    cmd_write_sfp_eeprom = f"sfputil write-eeprom -p {port} -n {page} -o {offset} -d {data}"

    if sfp_type == "sff8472":
        cmd_write_sfp_eeprom = f"{cmd_write_sfp_eeprom} --wire-addr a0h"

    if is_verify:
        cmd_write_sfp_eeprom = f"{cmd_write_sfp_eeprom} --verify"

    return duthost.shell(cmd_write_sfp_eeprom, module_ignore_errors=module_ignore_errors)['stdout']


def get_sfp_type_per_interface(duthost,interfaces, interfaces_skip_list):
    sfp_type_sc_port_dict = {}
    sfp_type_all_interfaces = get_sfp_mgmt_iface_type(duthost, interfaces)
    for intf in interfaces:
        if intf not in interfaces_skip_list[duthost.hostname]:
            sfp_type = sfp_type_all_interfaces[intf]
            assert sfp_type,  f"Failed to get sfp type {sfp_type} for port {intf}"
            sfp_type_sc_port_dict.update({intf: sfp_type})
    return sfp_type_sc_port_dict


def parse_eeprom_data(output_text, cmd_type = "READ_EEPROM"):
    sections = output_text.split(f"=== {cmd_type}")
    eeprom_data = {}
    for section in sections[1:]:
        split_section = section.split('===')
        port = split_section[0].strip()
        data = split_section[1].strip()
        eeprom_data[port] = data
    return eeprom_data


def read_write_eeprom_by_page_and_byte_to_interfaes_list(duthost, eeprom_cmd_per_interface, ports_list, cmd_type):
    ports_str = " ".join(ports_list)
    cmd_read_write_eeprom = f"""for port in {ports_str}; do echo "=== {cmd_type} $port ==="; {eeprom_cmd_per_interface}; echo; done"""
    result = duthost.shell(cmd_read_write_eeprom)
    read_write_eeprom_data = parse_eeprom_data(result['stdout'], cmd_type)
    return read_write_eeprom_data


def group_interfaces_by_sfp_type(sfp_type_per_interface, interfaces):
    ports_list_per_sfp_type = defaultdict(list)
    for intf in interfaces:
        sfp_type = sfp_type_per_interface[intf]
        ports_list_per_sfp_type[sfp_type].append(intf)
    return ports_list_per_sfp_type


def read_write_eeprom_by_page_and_byte_to_interfaes_list_by_sfp_type(duthost,cmd_type, sfp_type_per_interface, interfaces_list, page=0, offset_per_sfp_type=0,size=None, data=None, is_verify=False, no_format=True):
    parsed_cmd_type = "read" if cmd_type == "READ_EEPROM" else "write"
    ports_list_per_sfp_type = group_interfaces_by_sfp_type(sfp_type_per_interface, interfaces_list)
    read_write_eeprom_data_all_types = {}
    for sfp_type, ports_list in ports_list_per_sfp_type.items():
        cmd_suffix = ""
        if sfp_type == "sff8472":
            cmd_suffix = f" --wire-addr a0h"
        if is_verify and cmd_type == "WRITE_EEPROM":
            cmd_suffix = f"{cmd_suffix} --verify"
        if no_format and cmd_type == "READ_EEPROM":
            cmd_suffix = f"{cmd_suffix} --no-format"
        offset = offset_per_sfp_type[sfp_type] if type(offset_per_sfp_type) == dict else offset_per_sfp_type
        data_param = f" -d {data}" if data is not None else ""
        size_param = f" -s {size}" if size is not None else ""
        cmd_read_write_eeprom_per_interface = f"sfputil {parsed_cmd_type}-eeprom -p $port{data_param} -o {offset} -n {page}{size_param}{cmd_suffix}"
        read_write_eeprom_data = read_write_eeprom_by_page_and_byte_to_interfaes_list(duthost, cmd_read_write_eeprom_per_interface,  ports_list, cmd_type)
        read_write_eeprom_data_all_types.update(read_write_eeprom_data)
    return read_write_eeprom_data_all_types


def parse_sfp_type_output(lines, cmd_type="SFP_TYPE"):
    result = {}
    current_port = None
    for line in lines:
        line = line.strip()
        if line.startswith(f"=== {cmd_type} "):
            # Line is a header like "=== SFP_TYPE Ethernet104 ==="
            parts = line.split()
            if len(parts) >= 3:
                current_port = parts[2]  # "Ethernet104"
        elif current_port and line:
            result[current_port] = line
            current_port = None  # Reset for next block
    logging.info(f"result: {result}")
    return result


def get_sfp_module_type(duthost, dev_conn):
    ports_str = " ".join(dev_conn)
    cmd_type = "SFP_TYPE"
    sfp_type_cmd = 'redis-cli -n 6 --raw hget "TRANSCEIVER_INFO|$port" type'

    cmd = f"""for port in {ports_str}; do echo "=== {cmd_type} $port ==="; {sfp_type_cmd}; echo; done"""
    result = duthost.shell(cmd)
    return parse_sfp_type_output(result["stdout_lines"], cmd_type)


def get_sfp_mgmt_iface_type(duthost, interfaces):
    sfp_mgmt_iface_type = get_sfp_module_type(duthost, interfaces)

    for intf, module_type in sfp_mgmt_iface_type.items():
        mgmt_type = MGMT_TYPE_BY_LONG_TYPE.get(module_type, "unknown")
        sfp_mgmt_iface_type[intf] = mgmt_type
        logging.info(f"{intf}: module_type= {module_type} → type={mgmt_type}")

    logging.info(f"sfp_mgmt_iface_type: {sfp_mgmt_iface_type}")
    return sfp_mgmt_iface_type
