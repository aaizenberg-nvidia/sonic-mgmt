import re
import logging
from functools import cache

from devts.infra.tools.general_constants.air_constants import NvidiaAirConstants
from devts.infra.tools.nvidia_air_tools.air import get_air_api_object, get_project_from_simulation_name
from devts.infra.tools.nvidia_air_tools.air_metadata import SimulationMetadata, SimulationMetadataHandler

logger = logging.getLogger()


@cache
def is_air_external_ips(setup_name):
    """
    True when an AIR setup is reached through external (port-forwarded) IPs - the rule devts uses to build the
    topology: forced by FORCE_AIR_EXTERNAL_IPS, or the simulation's sonic-mgmt is not internal.
    Cached: the answer holds for the whole session, so the AIR API is queried at most once per setup.
    """
    if 'air' not in setup_name:
        return False
    if NvidiaAirConstants.FORCE_AIR_EXTERNAL_IPS:
        return True
    air = get_air_api_object(get_project_from_simulation_name(setup_name))
    simulation = next(air.simulations.list(name=setup_name))
    return not SimulationMetadataHandler(simulation).get_field(SimulationMetadata.SONIC_MGMT_INTERNAL, False)


def get_internal_ip_for_oob_server(oob_mgmt_server):
    try:
        cmd = 'sudo cat /etc/dhcp/dhcpd.hosts'
        logger.info(f"Getting internal IP for OOB server: {cmd}")
        output = oob_mgmt_server.run_cmd(cmd)

        match = re.search(r'option\s+routers\s+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', output)
        if match:
            logger.info(f"Internal IP for OOB server: {match.group(1)}")
            return match.group(1)
        else:
            logger.error("Internal IP for OOB server not found")
            return oob_mgmt_server.ip
    except Exception as e:
        logger.error(f"Error getting internal IP for OOB server: {e}")
        return oob_mgmt_server.ip
