"""Lightweight helpers for building the extra kwargs required by
get_topology_by_setup_name for Air setups.
"""
from ngts.scripts.air_bringup.topologies.ports_to_ports_description_mapping import (
    hwskus_ports_to_ports_description_mapping,
)


def get_air_topology_kwargs(setup_name):
    """Build the extra kwargs required by get_topology_by_setup_name for Air setups.

    The per-hwsku port-to-port description mapping is hosted in sonic-mgmt and passed
    to the devts Air API via custom_air_port_to_port_description_mapping. The AIR
    constants stay in devts, so they are not passed here.

    For non-Air setups this returns an empty dict, so it can safely be splatted into
    any get_topology_by_setup_name(...) call:
        get_topology_by_setup_name(setup_name, slow_cli, **get_air_topology_kwargs(setup_name))
    """
    if "air" not in setup_name:
        return {}
    return {
        "custom_air_port_to_port_description_mapping": hwskus_ports_to_ports_description_mapping,
    }
