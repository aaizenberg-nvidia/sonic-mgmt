import argparse
import os
import logging
import shutil
import sys
import json
import pandas as pd
import numpy as np

from devts.infra.tools.general_constants.air_constants import NvidiaAirConstants
from devts.infra.tools.topology_tools.topology_setup_utils import get_topology_by_setup_name
from ngts.tools.topology_tools.air_topology_kwargs import get_air_topology_kwargs
from ngts.constants.constants import SerialConsts

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
ch = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)
logger.propagate = False


class ConfFiles:
    def __init__(self, sonic_mgmt):
        self.testbed_yaml = "{sonic_mgmt}/ansible/testbed.yaml".format(sonic_mgmt=sonic_mgmt)
        self.lab = "{sonic_mgmt}/ansible/lab".format(sonic_mgmt=sonic_mgmt)
        self.inventory = "{sonic_mgmt}/ansible/inventory".format(sonic_mgmt=sonic_mgmt)
        self.minigraph_facts = "{sonic_mgmt}/ansible/library/minigraph_facts.py".format(sonic_mgmt=sonic_mgmt)
        self.sonic_nvidia_common_devices = "{sonic_mgmt}/ansible/files/sonic_nvidia_common_devices.csv".format(sonic_mgmt=sonic_mgmt)

    def __getattribute__(self, name):
        return object.__getattribute__(self, name)


class TestbedYAML:
    """
    @summary: Class which adds entry to the 'testbed.yaml' file
    """
    def __init__(self, yaml_file):
        self.testbed_yaml = yaml_file

    def add_entry(self, dut_name):
        """
        Write the testbed entry to the testbed.yaml file
        """
        line = (
            f"- conf-name: {dut_name}-ptf-any\n"
            f"  group-name: vm-t1\n"
            f"  topo: ptf-any\n"
            f"  ptf_image_name: docker-ptf-mlnx\n"
            f"  ptf: ptf-dummy\n"
            f"  ptf_ip: 1.1.1.1/16\n"
            f"  ptf_ipv6:\n"
            f"  server: server_54\n"
            f"  vm_base: VM0000\n"
            f"  dut:\n"
            f"    - {dut_name}\n"
            f"  inv_name: lab\n"
            f"  comment: NvidiaAir testbed"
        )
        with open(self.testbed_yaml, "w") as testbed_file:
            testbed_file.write(line)


class Inventory:
    """
    @summary: Class to add entry to the 'inventory' file
    """
    def __init__(self, inventory):
        self.inventory_path = inventory

    def add_entry(self, dut_name, ansible_host, ansible_port, hwsku, topology_type):
        """
        Write inventory entries to the inventory file
        Entry example:
        air_2700_1-ptf-any ansible_host=147.75.47.205 ansible_port=18696
        air_2700_1 ansible_host=147.75.47.205 ansible_port=18696
        """
        serial = SerialConsts.PLATFORM_SERIAL_NUM_MAP.get(topology_type, None)
        model = SerialConsts.PLATFORM_MODEL_MAP.get(topology_type, None)
        host_entry_ptf_any = f"{dut_name}-ptf-any ansible_host={ansible_host} ansible_port={ansible_port} sonic_hwsku={hwsku}"
        host_entry = f"{dut_name} ansible_host={ansible_host} ansible_port={ansible_port} sonic_hwsku={hwsku}"
        if serial:
            host_entry += f" serial={serial}"
        if model:
            host_entry += f" model={model}"

        with open(self.inventory_path, "w") as inv_file:
            inv_file.write(
                f"[sonic_latest]\n"
                f"{host_entry_ptf_any}\n"
                f"{host_entry}\n"
                f"\n"
                f"[sonic:children]\n"
                f"sonic_latest\n"
                f"\n"
                f"[lab]\n"
                f"{dut_name}-ptf-any\n"
                f"{dut_name}\n"
            )


class Lab:
    """
    @summary: Class to add entry to the 'lab' file.
    """
    def __init__(self, lab_path):
        self.lab_path = lab_path

    def add_entry(self, dut_name, ansible_host, ansible_port, hwsku):
        """
        Write lab entry to the lab file
        Entry example:
        air_2700_1      ansible_host=10.210.25.107 ansible_port=12345 sonic_version=v2
        """
        with open(self.lab_path, "w") as lab_file:
            lab_file.write(
                f"[sonic_latest]\n"
                f"{dut_name}      ansible_host={ansible_host} ansible_port={ansible_port} "
                f"sonic_version=v2 sonic_hwsku={hwsku}\n"
                f"\n"
                f"[sonic:children]\n"
                f"sonic_latest\n"
                f"\n"
                f"[lab:children]\n"
                f"sonic\n"
                f"fanout\n"
                f"\n"
                f"[lab:vars]\n"
                f"mgmt_subnet_mask_length=\"24\"\n"
                f"\n"
            )


class SonicNvidiaCommonDevices:
    CSV_COLUMNS = ["Hostname", "ManagementIp", "HwSku", "Type", "Protocol", "Os"]

    def __init__(self, sonic_nvidia_common_devices_path):
        self.sonic_nvidia_common_devices_path = sonic_nvidia_common_devices_path

    def add_entries(self, host_names, management_ip, hwsku):
        rows = [{
            "Hostname": host_name,
            "ManagementIp": management_ip,
            "HwSku": hwsku,
            "Type": "DevSonic",
            "Protocol": np.nan,
            "Os": "sonic"
        } for host_name in host_names]
        pd.DataFrame(rows, columns=self.CSV_COLUMNS).to_csv(self.sonic_nvidia_common_devices_path, index=False)

class MinigraphFacts:
    def __init__(self, mgmt_minigraph_path):
        self.mgmt_minigraph_path = mgmt_minigraph_path
        self.stub_mgmt_minigraph_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "minigraph_facts.py")

    def write_minigraph_facts(self):
        shutil.copyfile(self.stub_mgmt_minigraph_path, self.mgmt_minigraph_path)


def replace_conn_graph_facts(sonic_mgmt_path):
    mgmt_conn_graph_facts_path = '{}/ansible/library/conn_graph_facts.py'.format(sonic_mgmt_path)
    mgmt_conn_graph_facts_community_path = '{}/ansible/module_utils/conn_graph_facts_community.py'.format(
        sonic_mgmt_path)
    stub_mgmt_conn_graph_facts_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'conn_graph_facts.py')

    if not os.path.exists(mgmt_conn_graph_facts_community_path):
        # Copy ansible/library/conn_graph_facts.py to ansible/module_utils/conn_graph_facts_community.py
        logger.info('Copying: {} to {}'.format(mgmt_conn_graph_facts_path, mgmt_conn_graph_facts_community_path))
        shutil.copyfile(mgmt_conn_graph_facts_path, mgmt_conn_graph_facts_community_path)

        # Update ansible/module_utils/conn_graph_facts_community.py
        logger.info('Updating: {}'.format(mgmt_conn_graph_facts_community_path))
        os.system(f"sed -i 's/module.exit_json/return dict/g' {mgmt_conn_graph_facts_community_path}")

        # Replace ansible/library/conn_graph_facts.py with sonic-tool/sonic_ngts/scripts/conn_graph_facts.py
        logger.info('Replacing: {} by {}'.format(mgmt_conn_graph_facts_path, stub_mgmt_conn_graph_facts_path))
        shutil.copyfile(stub_mgmt_conn_graph_facts_path, mgmt_conn_graph_facts_path)


def replace_ptfadapter_init_py(sonic_mgmt_path):
    stub_ptfadapater_init_py_path = '{}/sonic-tool/sonic_ngts/scripts/ptfadapter/__init__.py'.format(sonic_mgmt_path)
    mgmt_ptfadapater_init_py_community_path = '{}/tests/common/plugins/ptfadapter/__init__.py'.format(
        sonic_mgmt_path)

    logger.info('Copying: {} to {}'.format(stub_ptfadapater_init_py_path, mgmt_ptfadapater_init_py_community_path))
    shutil.copyfile(stub_ptfadapater_init_py_path, mgmt_ptfadapater_init_py_community_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dut", help="DUT name", type=str, required=True)
    parser.add_argument("--mgmt_repo", help="Path to the sonic-mgmt repo", type=str, required=True)
    parser.add_argument("--setup_name", help="Setup name", type=str, required=False)
    parser.add_argument("--force_air_external_ips", dest="force_air_external_ips", action="store_true",
                        help="If passed, the script will force the use of external IPs instead of configured by simulation.")

    args = parser.parse_args()

    if args.force_air_external_ips:
        os.environ[NvidiaAirConstants.EXTERNAL_CONNECTION_MODE_ENV_VAR] = 'yes'

    dut_name = args.dut
    setup_name = args.setup_name
    mgmt_repo = args.mgmt_repo

    conf_files = ConfFiles(mgmt_repo)
    testbed_yaml = TestbedYAML(conf_files.testbed_yaml)

    lab = Lab(lab_path=conf_files.lab)
    inv = Inventory(conf_files.inventory)
    mg_facts = MinigraphFacts(conf_files.minigraph_facts)
    sonic_nvidia_common_devices = SonicNvidiaCommonDevices(conf_files.sonic_nvidia_common_devices)

    # Update minigraph_facts.py
    mg_facts.write_minigraph_facts()
    logger.info('minigraph_facts.py replaced by stub file')

    # Replace "ansible/library/conn_graph_facts.py" with "sonic-tool/sonic_ngts/scripts/conn_graph_facts.py"
    logger.info('Replace "ansible/library/conn_graph_facts.py" with "sonic-tool/sonic_ngts/scripts/conn_graph_facts.py"')
    replace_conn_graph_facts(mgmt_repo)

    # This step is for running dash case on CI
    logger.info('Replace "tests/common/plugins/ptfadapter/__init__.py" '
                'with "sonic-tool/sonic_ngts/scripts/ptfadapter/__init__.py"')
    replace_ptfadapter_init_py(mgmt_repo)

    if 'air' in setup_name:
        topology = get_topology_by_setup_name(
            setup_name=setup_name, slow_cli=False, **get_air_topology_kwargs(setup_name))
        ansible_host = topology.players['dut']['engine'].ip
        ansible_port = topology.players['dut']['engine'].ssh_port
        devdescription = json.loads(topology.players['dut']['attributes'].noga_query_data['attributes']['Specific']['devdescription'])
        hwsku = devdescription['hwsku']
        topology_type = devdescription['platform']
        os.system(f"echo '{ansible_host} {setup_name}' >> /etc/hosts")
        files = [inv, lab, testbed_yaml, sonic_nvidia_common_devices]
        for f in files:
            if isinstance(f, TestbedYAML):
                f.add_entry(dut_name=dut_name)
            elif isinstance(f, SonicNvidiaCommonDevices):
                f.add_entries(host_names=[ansible_host, dut_name], management_ip=ansible_host, hwsku=hwsku)
            elif isinstance(f, Inventory):
                f.add_entry(dut_name=dut_name, ansible_host=ansible_host, ansible_port=ansible_port, hwsku=hwsku, topology_type=topology_type)
            else:
                f.add_entry(dut_name=dut_name, ansible_host=ansible_host, ansible_port=ansible_port, hwsku=hwsku)
            logger.info(f"Entry for '{dut_name}' DUT added to {f.__class__.__name__} file.")