#!/usr/bin/python
from ansible.module_utils.basic import *
import traceback
import re
import json
import sys
import socket
import time
import os
socket.setdefaulttimeout(60)

if sys.version_info[0] == 3:
    # Python 3
    from urllib.request import urlopen
    unicode = bytes
else:
    # Python 2
    from urllib import urlopen

'''
---
module: minigraph_facts
author: Petro Pikh (petrop@nvidia.com)
short_description: Module(emulates) which replaces original file ansible/library/minigraph_facts.py
description: Instead of retrieve minigraph facts for a device from minigraph.xml - this module creating stub with
    data which are required by tests.
    As module argument we got hostname - using this hostname we find setup_name then we read config_db.json
    for specific setup and create dictionary with data(example below)

Example of data which returned by this module:
"ansible_facts": {
    "minigraph_portchannels": {},
    "minigraph_port_indices": {},
    "minigraph_vlans": {},
    "inventory_hostname": "r-leopard-32",
    "minigraph_ports": {"Ethernet8": {"alias": "etp3", "name": "Ethernet8"},
    "Ethernet0": {"alias": "etp1", "name": "Ethernet0"}, "Ethernet4": {"alias": "etp2", "name": "Ethernet4"},
    "Ethernet108": {"alias": "etp28", "name": "Ethernet108"}, "Ethernet100": {"alias": "etp26", "name": "Ethernet100"},
    "Ethernet104": {"alias": "etp27", "name": "Ethernet104"}, "Ethernet68": {"alias": "etp18", "name": "Ethernet68"},
    "Ethernet96": {"alias": "etp25", "name": "Ethernet96"}, "Ethernet124": {"alias": "etp32", "name": "Ethernet124"},
    "Ethernet92": {"alias": "etp24", "name": "Ethernet92"}, "Ethernet120": {"alias": "etp31", "name": "Ethernet120"},
    "Ethernet52": {"alias": "etp14", "name": "Ethernet52"}, "Ethernet56": {"alias": "etp15", "name": "Ethernet56"},
    "Ethernet76": {"alias": "etp20", "name": "Ethernet76"}, "Ethernet72": {"alias": "etp19", "name": "Ethernet72"},
    "Ethernet64": {"alias": "etp17", "name": "Ethernet64"}, "Ethernet32": {"alias": "etp9", "name": "Ethernet32"},
    "Ethernet16": {"alias": "etp5", "name": "Ethernet16"}, "Ethernet36": {"alias": "etp10", "name": "Ethernet36"},
    "Ethernet12": {"alias": "etp4", "name": "Ethernet12"}, "Ethernet88": {"alias": "etp23", "name": "Ethernet88"},
    "Ethernet116": {"alias": "etp30", "name": "Ethernet116"}, "Ethernet80": {"alias": "etp21", "name": "Ethernet80"},
    "Ethernet112": {"alias": "etp29", "name": "Ethernet112"}, "Ethernet84": {"alias": "etp22", "name": "Ethernet84"},
    "Ethernet48": {"alias": "etp13", "name": "Ethernet48"}, "Ethernet44": {"alias": "etp12", "name": "Ethernet44"},
    "Ethernet40": {"alias": "etp11", "name": "Ethernet40"}, "Ethernet28": {"alias": "etp8", "name": "Ethernet28"},
    "Ethernet60": {"alias": "etp16", "name": "Ethernet60"}, "Ethernet20": {"alias": "etp6", "name": "Ethernet20"},
    "Ethernet24": {"alias": "etp7", "name": "Ethernet24"}},
    "minigraph_devices": {},
    "minigraph_vlan_interfaces": [],
    "minigraph_hwsku": "ACS-MSN4700",
    "minigraph_neighbors": {}
}

'''

http_topo_base_path = "http://nbu-mtr-nfs.nvidia.com/auto/sw_regression/system/SONIC/MARS/conf/topo/"


def str_hook(obj):
    return {k.encode("utf-8") if isinstance(k, unicode) else k:
            v.encode("utf-8") if isinstance(v, unicode) else v
            for k, v in obj}


def read_config_db_json(json_file_path):
    with open(json_file_path, 'r') as f:
        config_db_json = json.load(f)
    return config_db_json


def get_config_db_json_from_hostname(hostname, logs):
    """
    Method returns config_db.json data as dictionary for specific host
    We looking for setup name in shared folder and then read config_db.json for specific host.
    If setup name does not include hostname in name - then we will try to find hostname in CI setups folder
    """
    if 'ptf-any' in hostname:
        hostname = hostname.strip('ptf-any')
    if socket.gethostname() == "dut" or socket.gethostname() == hostname:
        tmp_config_db = "/etc/sonic/config_db.json"
    else:
        tmp_config_db = '/tmp/config_db.json'
        logs.append('{} Copy config_db.json file from {}'.format(time.ctime(), hostname))
        sw_user = os.getenv("SONIC_SWITCH_USER")
        sw_password = os.getenv("SONIC_SWITCH_PASSWORD")
        scp_cmd = "sshpass -p '{sw_password}' scp -o 'StrictHostKeyChecking no' -o 'UserKnownHostsFile=/dev/null'" \
                  " -r {sw_user}@{hostname}:" \
                  "/etc/sonic/config_db.json {tmp_config_db}".format(sw_password=sw_password,
                                                                     sw_user=sw_user,
                                                                     hostname=hostname,
                                                                     tmp_config_db=tmp_config_db)
        os.system(scp_cmd)

    logs.append('Read config_db.json file')
    config_db_json = read_config_db_json(tmp_config_db)

    return config_db_json


def get_dut_ports(config_db_json, logs):
    ports = {}
    logs.append('{} Getting dut ports'.format(time.ctime()))
    for iface in config_db_json["PORT"]:
        if config_db_json["PORT"][iface].get('role', None) == 'Dpc' or \
                config_db_json["PORT"][iface].get('admin_status') == 'down':
            continue
        ports[iface] = {"alias": config_db_json["PORT"][iface]["alias"], "name": iface}
    logs.append('{} Getting dut ports finished'.format(time.ctime()))
    return ports


def generate_minigraph_facts(hostname):
    minigraph_facts = dict()
    logs = []
    config_db_json = get_config_db_json_from_hostname(hostname, logs)

    minigraph_facts["inventory_hostname"] = config_db_json["DEVICE_METADATA"]["localhost"]["hostname"]
    minigraph_facts["minigraph_hwsku"] = config_db_json["DEVICE_METADATA"]["localhost"]["hwsku"]
    minigraph_facts["minigraph_ports"] = get_dut_ports(config_db_json, logs)
    minigraph_facts["minigraph_port_indices"] = {}
    minigraph_facts["minigraph_vlans"] = {}
    minigraph_facts["minigraph_portchannels"] = {}
    minigraph_facts["minigraph_devices"] = {}
    minigraph_facts["minigraph_neighbors"] = {}
    minigraph_facts["minigraph_device_metadata"] = {"device_type": config_db_json["DEVICE_METADATA"]["localhost"]["type"]}
    minigraph_facts["minigraph_mgmt_interface"] = {"gwaddr": config_db_json["MGMT_INTERFACE"][list(config_db_json["MGMT_INTERFACE"].keys())[0]]["gwaddr"]}
    minigraph_facts['logs'] = logs
    return minigraph_facts


def main():
    module = AnsibleModule(
        argument_spec=dict(
            host=dict(required=True),
            filename=dict(),
            namespace=dict(required=False, default=None)
        ),
        supports_check_mode=True
    )

    m_args = module.params

    try:
        results = generate_minigraph_facts(m_args['host'])
        module.exit_json(ansible_facts=results)
    except Exception as e:
        tb = traceback.format_exc()
        module.fail_json(msg=str(e) + "\n" + tb)


if __name__ == "__main__":
    main()
