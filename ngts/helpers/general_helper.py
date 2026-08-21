import datetime as dt
import re

from perscache import Cache

from devts.infra.tools.topology_tools.topology_setup_utils import get_all_setups_per_group
from ngts.cli_wrappers.nvue.cumulus.cumulus_general_cli import CumulusGeneralCli
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.cli_wrappers.sonic.sonic_cli import SonicCli
from ngts.cli_wrappers.dvs.dvs_cli import DvsCli
from ngts.constants.constants import SonicConst, ResultUploaderConst
from ngts.constants.performance_constants import PerfConsts
from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.Devices.DeviceFactory import DeviceFactory

cache = Cache()


@cache(ttl=dt.timedelta(hours=36))
def get_all_setups():
    all_setups_platforms = get_all_setups_platform()
    all_setups = list(all_setups_platforms.keys())
    return all_setups


@cache(ttl=dt.timedelta(hours=36))
def get_all_setups_platform():
    canonical_setups_platforms = get_all_setups_per_group(SonicConst.SONIC_CANONICAL_NOGA_GROUP)
    filter_canonical_setups(canonical_setups_platforms)
    community_setups_platforms = get_all_setups_per_group(SonicConst.SONIC_COMMUNITY_NOGA_GROUP)
    dpu_setups_platforms = get_all_setups_per_group(SonicConst.SONIC_DPU_NOGA_GROUP)
    canonical_setups_platforms.update(community_setups_platforms)
    canonical_setups_platforms.update(dpu_setups_platforms)
    all_setups_platforms = canonical_setups_platforms
    return all_setups_platforms


def filter_canonical_setups(canonical_setups_platforms):
    keys_to_remove = []
    for setup_name, platform in canonical_setups_platforms.items():
        # Keep sonic_slm-* (e.g. sonic_slm-110); regex below only matches sonic_<codename>_
        if setup_name in ResultUploaderConst.NON_STANDARD_NAMED_SETUPS or setup_name.startswith("sonic_slm-"):
            continue
        match = re.search(r"sonic_(\w+)_", setup_name)
        if setup_name.startswith("CI") or not match:
            keys_to_remove.append(setup_name)
        else:
            switch_platform_name = match.group(1)
            if switch_platform_name == "simx":
                keys_to_remove.append(setup_name)
    for key in keys_to_remove:
        canonical_setups_platforms.pop(key)


def get_cli_obj(topology_obj, cli_type, switch_type, engine, host, dut_alias):
    if cli_type == NvosConst.NVUE_CLI:
        device_name = topology_obj.players[host]['attributes'].noga_query_data['attributes']['Specific']. \
            get('switch_type', '')
        device = DeviceFactory.create_device(device_name)
        if switch_type == NvosConst.CUMULUS_SWITCH:
            cli_obj = CumulusGeneralCli(engine, device)
        else:
            cli_obj = NvueGeneralCli(engine, device)
    elif cli_type == PerfConsts.DVS_CLI_TYPE:
        cli_obj = DvsCli(topology_obj, dut_alias=dut_alias).general
    else:
        cli_obj = SonicCli(topology_obj, dut_alias=dut_alias).general

    return cli_obj


def extract_host_details_from_topo_obj(topology_obj, host):
    dut_name = topology_obj.players[host]['attributes'].noga_query_data['attributes']['Common']['Name']
    dut_alias = topology_obj.players[host]['attributes'].noga_query_data['attributes']['Common']['Description']
    cli_type = topology_obj[0][host]['attributes'].noga_query_data['attributes']['Topology Conn.']['CLI_TYPE']
    switch_type = topology_obj.players[host]['attributes'].noga_query_data['attributes']['Specific'].get('TYPE', '')
    dut_ip = topology_obj.players[host]['attributes'].noga_query_data['attributes']['Specific'].get('ip address', '')
    engine = topology_obj.players[host]['engine']
    return cli_type, dut_alias, dut_ip, dut_name, engine, switch_type


def get_dut_cli_objs_from_topo_obj(topology_obj):
    dut_cli_objs = dict()
    for host in topology_obj.players:
        if host.startswith('dut') and not host.endswith('_serial') and "dpu" not in host:
            # only pick the SSH dut aliases, not the serial or DPUs
            cli_type, dut_alias, dut_ip, dut_name, engine, switch_type = extract_host_details_from_topo_obj(topology_obj, host)
            dut_cli_objs[host] = get_cli_obj(topology_obj, cli_type, switch_type, engine, host, dut_alias)
    return dut_cli_objs


def get_pytest_test_name(request):
    return request.node.nodeid.split("::")[-1]


def is_smartswitch_platform(topology_obj):
    dut_name = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Common']['Name']
    return 'bobcat' in dut_name


def is_bmc_testbed(testbed):
    """Detect BMC test flow by the testbed name.

    BMC testbeds are named `<dut>-bmc-dual-mgmt` in ansible/testbed.yaml
    """
    testbed = testbed or ''
    return testbed.endswith('-bmc-dual-mgmt')
