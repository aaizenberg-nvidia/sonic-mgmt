import logging
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.topology_tools.topology_setup_utils import get_topology_by_setup_name, create_player_entry
from ngts.cli_wrappers.linux.linux_cli import LinuxCli
from ngts.constants.constants import PlayersAliases, BmcDeployConstants
from ngts.common.util import get_specified_installed_dpu_indexes
from ngts.tools.topology_tools.air_topology_kwargs import get_air_topology_kwargs

logger = logging.getLogger()


def need_dpu_player(dut_alias, noga_name):
    return dut_alias.startswith("dut") and (not dut_alias.endswith("_serial")) and "bobcat" in noga_name


def get_dpu_player_key(dut_alias, device_alias):
    # for back compatibility,
    # one if the dut alias is not 'dut', the dpus are prefixed with the dut alias
    # for the main dut, the dpus are named as dpu0, dpu1, dpu2, dpu3
    # for the other duts, the dpus are prefixed with the dut alias
    # as dut-b-dpu0, dut-b-dpu1, dut-b-dpu2, dut-b-dpu3
    if dut_alias == "dut":
        return device_alias
    return f'{dut_alias}-{device_alias}'


def get_topology_by_setup_name_and_aliases(
    setup_name, slow_cli, override_type=False
):

    topology = get_topology_by_setup_name(
        setup_name, slow_cli, override_type, **get_air_topology_kwargs(setup_name))
    topology = update_dut_alias(topology)
    for dut_alias, dut_info in list(topology.players.items()):
        dut_name = dut_info["attributes"].noga_query_data["attributes"][
            "Common"
        ]["Name"]
        if need_dpu_player(dut_alias, dut_name):
            add_dpu_player(
                topology, slow_cli, override_type, dut_alias, dut_name
            )
    add_bmc_player(topology)
    return topology


def add_bmc_player(topology):
    """Attach a synthetic 'bmc' player when Noga exposes a BMC IP on the DUT.

    The BMC IP comes from Noga's "BMC IP" field on the switch resource
    (`players['dut'].attributes.noga_query_data['attributes']['Specific']['bmc_ip']`).
    The BMC is not a Noga player on its own; we attach a player with `engine` and `cli` only
    (no `attributes`/`noga_query_data`). `update_topology_with_cli_class` must skip 'bmc'.
    """
    try:
        bmc_ip = topology.players['dut']['attributes'].noga_query_data['attributes']['Specific'].get('bmc_ip')
    except Exception as err:
        logger.warning(f"Failed to read bmc_ip from Noga, skip BMC player attach: {err}")
        return
    if not bmc_ip:
        return
    bmc_engine = LinuxSshEngine(bmc_ip,
                                BmcDeployConstants.BMC_SONIC_OS_USERNAME,
                                BmcDeployConstants.BMC_SONIC_OS_PASSWORD)
    topology.players['bmc'] = {
        'engine': bmc_engine,
        'cli': LinuxCli(bmc_engine),
    }
    logger.info(f"Attached BMC player at {bmc_ip}")


def update_dut_alias(topology):
    if 'dut' not in topology.players.keys():
        # For the lower tor in dual-tor or smartswitch-ha topology,
        # or other setups with two duts,
        # the key of dut name got from noga is 'dut-b'
        # If the topology only has key 'dut-b' without key 'dut', means it is deployed as a normal setup
        # Change the 'dut-b' to 'dut' in this case
        if 'dut-b' in topology.players.keys():
            topology.players['dut'] = topology.players.pop('dut-b')
            topology.players['dut_serial'] = topology.players.pop('dut-b_serial')
            topology.players['dut']['attributes'].noga_query_data['attributes']['Common']['Description'] = 'dut'
            topology.players['fanout'] = topology.players.pop('fanout-b')
            topology.players['fanout_serial'] = topology.players.pop('fanout-b_serial')
            topology.players['fanout']['attributes'].noga_query_data['attributes']['Common']['Description'] = 'fanout'
        for alias in PlayersAliases.Aliases_list:
            if alias in topology.players.keys():
                topology.players['dut'] = topology.players[alias]
                del topology.players[alias]
    return topology


def add_dpu_player(topology, slow_cli, override_type, dut_alias, dut_name):
    dpu_player_entry = {'DESCRIPTION': 'dpu0',
                        'SSH_PORT': 5021,
                        'XML_RPC_PORT': 9999,
                        'TYPE_TITLE': "Switch",
                        'TYPE': '11',
                        'IP': topology.players[dut_alias]['engine'].ip,
                        }
    dpu_indexes = get_specified_installed_dpu_indexes(dut_alias, dut_name)
    base_dpu_ssh_nat_port = 5021

    for dpu_index in dpu_indexes:
        # for back compatibility,
        # one if the dut alias is not 'dut', the dpus are prefixed with the dut alias
        # for the main dut, the dpus are named as dpu0, dpu1, dpu2, dpu3
        # for the other duts, the dpus are prefixed with the dut alias
        # as dut-b-dpu0, dut-b-dpu1, dut-b-dpu2, dut-b-dpu3
        dpu_host_name = get_dpu_player_key(dut_alias, f"dpu{dpu_index}")
        dpu_player_entry['DESCRIPTION'] = dpu_host_name
        dpu_player_entry['SSH_PORT'] = base_dpu_ssh_nat_port + dpu_index
        logger.info(f"create dpu{dpu_index} player for {dut_name} (dut_alias: {dut_alias})")
        topology.players.update(create_player_entry(dpu_player_entry, slow_cli, override_type))
        if dpu_host_name in topology.players:
            topology.players[dpu_host_name]['attributes'].noga_query_data['attributes']['Common']['Name'] += f"-dpu-{dpu_index}"
            topology.players[dpu_host_name]['attributes'].noga_query_data['attributes']['Common']['Description'] = dpu_host_name
