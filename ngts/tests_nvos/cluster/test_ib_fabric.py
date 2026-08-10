import json
import logging
import re
import time

import pytest

from ngts.ngts_types import DevicesT, EnginesT
from ngts.nvos_tools.Devices.IbDevice import PortiaSwitch
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.nvos_tools.infra.IbnetdiscoverTool import IbnetdiscoverTool
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()

# ── smpquery nd constants ─────────────────────────────────────────────────────

SMPQUERY_ND_CMD = 'sudo smpquery -t 5000 nd -D {dr_path}'
SMPQUERY_RETRIES = 5
SMPQUERY_RETRY_DELAY_S = 5
NV_SHOW_FAE_INTERFACE_JSON = 'nv show fae interface -o json'
NV_SHOW_FAE_INTERFACE_PORT_JSON = 'nv show fae interface {port} -o json'

# Node Description format: "Node Description:..MF0;<host>:<dev>/U<n>"
RE_NODE_DESCRIPTION = re.compile(r'Node Description:\s*\.+(.+)$', re.MULTILINE)
RE_MF0_DESC = re.compile(r'MF0;[^:]+:\S+/U\d+')


# ── helpers ───────────────────────────────────────────────────────────────────

def _is_fae_interface_up(port_data):
    """Return True when FAE interface JSON reports link state up."""
    link_data = port_data.get('link', {})
    link_state = link_data.get(IbInterfaceConsts.LINK_STATE)

    if isinstance(link_state, dict):
        return NvosConsts.LINK_STATE_UP in link_state
    if isinstance(link_state, str):
        return link_state.lower() == NvosConsts.LINK_STATE_UP

    return (
        link_data.get(IbInterfaceConsts.LINK_LOGICAL_PORT_STATE) ==
        IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE and
        link_data.get(IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE) ==
        IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP
    )


def _get_fnm_interfaces(engines, devices):
    """Return sorted list of FNM port names from 'nv show fae interface -o json'."""
    output = engines.dut.run_cmd(NV_SHOW_FAE_INTERFACE_JSON)
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Could not parse JSON from 'nv show fae interface': %s", output)
        return []

    fnm_ports = []
    is_portia_simx = isinstance(devices.dut, PortiaSwitch) and devices.dut.is_simx
    for port_name, port_data in data.items():
        if isinstance(port_data, dict):
            port_type = port_data.get('type', '')
            if port_type == 'fnm' or (not port_type and port_name.startswith('fnm')):
                if is_portia_simx and not _is_fae_interface_up(port_data):
                    logger.info("Skipping down Portia SIMX FNM port %s: %s", port_name, port_data.get('link', {}))
                    continue
                fnm_ports.append(port_name)
    return sorted(fnm_ports)


def _get_ib_port(engines, port_name):
    """Return the ib-port integer for *port_name*, or None on failure."""
    output = engines.dut.run_cmd(
        NV_SHOW_FAE_INTERFACE_PORT_JSON.format(port=port_name)
    )
    try:
        value = json.loads(output).get('ib-port')
        if value is not None:
            return int(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    logger.warning("Could not extract ib-port for %s from: %s", port_name, output)
    return None


def _smpquery_nd(engines, dr_path_str):
    """Run 'sudo smpquery nd -D <dr_path>' and return raw output, retrying on timeout."""
    for attempt in range(SMPQUERY_RETRIES):
        output = engines.dut.run_cmd(SMPQUERY_ND_CMD.format(dr_path=dr_path_str))
        if 'Connection timed out' not in output:
            return output
        if attempt < SMPQUERY_RETRIES - 1:
            logger.warning(
                "smpquery timed out for DR path %s, retrying (%s/%s)",
                dr_path_str,
                attempt + 1,
                SMPQUERY_RETRIES - 1,
            )
            time.sleep(SMPQUERY_RETRY_DELAY_S)
    return output


def _parse_node_desc(output):
    """Extract the Node Description string from smpquery nd output."""
    m = RE_NODE_DESCRIPTION.search(output)
    return m.group(1).strip() if m else None


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.air
@pytest.mark.simx
@pytest.mark.air_ci
@pytest.mark.air_sanity
@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_ibnetdiscover(engines, devices, setup_name):
    """
    Verify 'sudo ibnetdiscover' output via IbnetdiscoverTool:
      1. At least one switch is discovered.
      2. Every switch has a non-empty guid and a valid order (> 0).
      3. Every switch has at least one port connection.
      4. Port connections are bidirectionally consistent across all switches.
    """
    with allure.step("Run ibnetdiscover and parse topology"):
        switches = IbnetdiscoverTool.run_ibnetdiscover(engines)
        logger.info("Discovered %s switch(es)", len(switches))

    with allure.step("Verify at least one switch was discovered"):
        assert switches, "IbnetdiscoverTool returned no switches"

    with allure.step("Verify each switch has a valid guid and order"):
        for sw in switches:
            assert sw['switch_guid'], \
                f"Switch at order {sw['order']} has empty switch_guid"
            assert sw['order'] > 0, \
                f"Switch {sw['switch_guid']} has invalid order {sw['order']}"

    with allure.step("Verify each switch has at least one port connection"):
        for sw in switches:
            assert sw['ports'], \
                f"Switch {sw['switch_guid']} (U{sw['order']}) has no port connections"

    with allure.step("Verify port connections are bidirectionally consistent"):
        guid_map = {sw['switch_guid']: sw for sw in switches}
        # Build (guid, port_num) -> (remote_guid, remote_port_num)
        connection_map = {
            (sw['switch_guid'], p['port_num']): (p['remote_switch_guid'], p['remote_port_num'])
            for sw in switches
            for p in sw['ports']
        }

        inconsistencies = []
        for (guid, port_num), (remote_guid, remote_port_num) in connection_map.items():
            if remote_guid not in guid_map:
                continue  # external / host node — not in fabric output
            reverse = connection_map.get((remote_guid, remote_port_num))
            if reverse is None:
                inconsistencies.append(
                    f"Switch {guid} port {port_num} -> {remote_guid} port {remote_port_num}: "
                    f"reverse entry missing"
                )
            elif reverse != (guid, port_num):
                inconsistencies.append(
                    f"Switch {guid} port {port_num} -> {remote_guid} port {remote_port_num}: "
                    f"reverse points to {reverse} instead of ({guid}, {port_num})"
                )

        assert not inconsistencies, \
            "Bidirectional connection inconsistencies:\n" + "\n".join(inconsistencies)

    with allure.step("Log topology summary"):
        for sw in switches:
            logger.info("  U%s guid=%s connections=%s", sw['order'], sw['switch_guid'], len(sw['ports']))


@pytest.mark.air
@pytest.mark.simx
@pytest.mark.air_ci
@pytest.mark.air_sanity
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_smpquery_nd_per_fnm_port(engines: EnginesT, devices: DevicesT):
    """
    For each FNM interface on the switch:
      1. Retrieve its ib-port number via 'nv show fae interface <port> -o json'.
      2. Use ibnetdiscover to know the expected number of switches in the ring.
      3. Traverse the ring by building DR paths 0,P / 0,P,P / ... up to num_switches hops.
      4. Each hop must return a valid Node Description matching MF0;<host>:<dev>/U<n>.
      5. All hops before the ring closes must reach distinct switches.
    """
    with allure.step("Discover switches via ibnetdiscover"):
        switches = IbnetdiscoverTool.run_ibnetdiscover(engines)
        num_switches = len(switches)
        logger.info("Discovered %s switch(es)", num_switches)
        assert num_switches > 0, "No switches discovered via ibnetdiscover"

    with allure.step("Discover FNM interfaces via 'nv show fae interface'"):
        fnm_ports = _get_fnm_interfaces(engines, devices)
        logger.info("Found FNM ports: %s", fnm_ports)
        assert fnm_ports, "No FNM interfaces found in 'nv show fae interface' output"

    for port_name in fnm_ports:
        with allure.step(f"FNM port: {port_name}"):

            with allure.step(f"Get ib-port number for {port_name}"):
                ib_port = _get_ib_port(engines, port_name)
                assert ib_port is not None, \
                    f"Could not determine ib-port for interface {port_name}"
                logger.info("  %s -> ib-port %s", port_name, ib_port)

            visited = []

            with allure.step(f"Traverse ring via DR hops using port {ib_port} ({num_switches} switches expected)"):
                for hops in range(1, num_switches + 1):
                    dr_path = ','.join(['0'] + [str(ib_port)] * hops)

                    with allure.step(f"smpquery nd -D {dr_path}"):
                        output = _smpquery_nd(engines, dr_path)
                        logger.info("    [%s] -> %s", dr_path, output.strip())

                        assert 'Connection timed out' not in output, \
                            f"smpquery timed out for DR path {dr_path}:\n{output}"
                        assert 'failed' not in output.lower(), \
                            f"smpquery failed for DR path {dr_path}:\n{output}"

                        desc = _parse_node_desc(output)
                        assert desc is not None, \
                            f"No Node Description in output for DR path {dr_path}:\n{output}"
                        assert RE_MF0_DESC.search(desc), \
                            f"Node Description '{desc}' does not match " \
                            f"expected format 'MF0;<host>:<dev>/U<n>' (DR path {dr_path})"

                        # Ring closed: back to the first hop's switch
                        if visited and desc == visited[0]:
                            logger.info("  Ring closed after %s hops (back to '%s')", hops, desc)
                            break

                        visited.append(desc)

            with allure.step("Verify all intermediate hops reached distinct switches"):
                assert len(visited) == len(set(visited)), \
                    f"Duplicate Node Descriptions before ring closed: {visited}"

            with allure.step(f"Verify all {num_switches} switches (U1-U{num_switches}) are present"):
                u_numbers = set()
                for desc in visited:
                    m = re.search(r'/U(\d+)$', desc)
                    if m:
                        u_numbers.add(int(m.group(1)))
                expected = set(range(1, num_switches + 1))
                assert u_numbers == expected, \
                    f"Expected switches {expected}, found {u_numbers} in ring: {visited}"

            logger.info("  %s: ring = %s", port_name, " -> ".join(visited))
