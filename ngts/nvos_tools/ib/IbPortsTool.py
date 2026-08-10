"""
Live IB port topology discovery.

Answers "which ports are cabled to what" by running ibdiagnet on the DUT and
parsing its output, instead of reading the static connectivity json under
/auto/sw_system_project/NVOS_INFRA/verification_files/connectivity_files/.

That json is a snapshot regenerated only when someone runs
test_and_update_connectivity_json.py - some setup files are months old - so its
link-state fields cannot be trusted to decide whether a link is usable right
now. Discovery here reflects the fabric at the moment the test runs.

Typical use, via the session-scoped `ib_ports` fixture:

    def test_something(ib_ports):
        port_a, port_b = ib_ports.loop_ports()

The parsing helpers below mirror the ones in
ngts/tests_nvos/general/test_and_update_connectivity_json.py, which is left
untouched on purpose. The two copies are therefore independent: if the ibdiagnet
output format changes, both have to be updated, and a fix applied to only one of
them will make live discovery and the generated json disagree.
"""
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from ngts.nvos_constants.constants_nvos import ApiType, IbConsts
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


AGG_LABEL_RE = re.compile(r'^\s*APort\s+(?P<aport>\d+)\s+Aggregated label:\s+(?P<label>\S+)\s*$')
PLANE_LINE_RE = re.compile(
    r'^\s*(?P<plane>\S+):\s+(?P<state>\S+)\s+PortGuid:\s+(?P<port_guid>\S+)\s+'
    r'NodeGuid:\s+(?P<node_guid>\S+)\s+Port:\s+(?P<port_num>\d+),'
)
SYSTEM_LINE_RE = re.compile(r'^\s*SW\s+SystemGUID:\s*(?P<guid>\S+)\s+Description:\s*(?P<desc>.+?)\s*$')

KEY_LOOPBACK = "loopback"
KEY_CONNECTED_TO = "connected_to"
KEY_NEIGHBOR_DESCRIPTION = "neighbor_description"
KEY_PHYSICAL_STATE = "physical_state"
KEY_STATE = "state"

PHYSICAL_STATE_LINK_UP = "LINK UP"
# Aggregated labels of host-facing links carry the HCA marker, e.g.
# "ibB134D0F0HCA0p1s3". Switch-facing labels do not.
HCA_LABEL_MARKER = "HCA"


@dataclass
class IbPortsInfo:
    """Result of a live IB topology discovery, with port-selection helpers."""

    description: str = ""
    system_guid: str = ""
    ports: Dict[str, Any] = field(default_factory=dict)

    def all_loop_ports(self, require_link_up: bool = True) -> List[Tuple[str, str]]:
        """
        Every loopback pair as [(port, peer), ...] - two ports of this switch cabled
        to each other.

        A cabled link shows up once per direction, so pairs are de-duplicated: the
        caller gets one entry per physical link. Sorted for determinism, so repeated
        calls select the same pair and a fixture and the test it serves agree on
        which link they configure.
        """
        pairs = []
        seen = set()
        for port_name in sorted(self.ports):
            port_data = self.ports[port_name]
            if not port_data.get(KEY_LOOPBACK):
                continue
            peer_name = port_data.get(KEY_CONNECTED_TO)
            if not peer_name or peer_name not in self.ports:
                logger.warning(f"loopback port '{port_name}' has no resolvable peer, skipping")
                continue
            link = frozenset((port_name, peer_name))
            if link in seen:
                continue
            seen.add(link)
            if require_link_up and not self._both_up(port_name, peer_name):
                logger.info(f"skipping loopback pair {port_name} <-> {peer_name}: not physically up")
                continue
            pairs.append((port_name, peer_name))
        return pairs

    def loop_ports(self, index: int = 0, require_link_up: bool = True) -> Tuple[str, str]:
        """
        One loopback pair as (port_a, port_b).

        Raises AssertionError when the setup has no usable loopback pair - callers
        that should skip instead are expected to check all_loop_ports() themselves.
        """
        pairs = self.all_loop_ports(require_link_up=require_link_up)
        assert pairs, (f"no {'up ' if require_link_up else ''}loopback (switch-to-switch) port pair "
                       f"found on '{self.description}'")
        assert index < len(pairs), f"loopback pair index {index} out of range, found {len(pairs)} pair(s)"
        return pairs[index]

    def switch_connected_ports(self, require_link_up: bool = True) -> List[str]:
        """Ports cabled to a different switch (not loopback, not host-facing)."""
        return sorted(
            name for name, data in self.ports.items()
            if data.get(KEY_CONNECTED_TO) and
            not data.get(KEY_LOOPBACK) and
            HCA_LABEL_MARKER not in str(data.get(KEY_CONNECTED_TO)) and
            (not require_link_up or self._is_up(name))
        )

    def host_connected_ports(self, require_link_up: bool = True) -> List[str]:
        """Ports cabled to a host HCA."""
        return sorted(
            name for name, data in self.ports.items()
            if HCA_LABEL_MARKER in str(data.get(KEY_CONNECTED_TO) or "") and
            (not require_link_up or self._is_up(name))
        )

    def _is_up(self, port_name: str) -> bool:
        return self.ports.get(port_name, {}).get(KEY_PHYSICAL_STATE) == PHYSICAL_STATE_LINK_UP

    def _both_up(self, *port_names: str) -> bool:
        return all(self._is_up(name) for name in port_names)


class IbPortsTool:
    """
    Runs ibdiagnet on the DUT and turns its output into an :class:`IbPortsInfo`.

    Discovery is cached per DUT engine: ibdiagnet takes about a minute, and the
    cabling does not change mid-session. Pass force_refresh=True to re-run, e.g.
    after a reboot or a cabling change.
    """

    _cache: Dict[str, IbPortsInfo] = {}

    @classmethod
    def discover(cls, engine, force_refresh: bool = False) -> IbPortsInfo:
        cache_key = getattr(engine, 'ip', str(engine))
        if not force_refresh and cache_key in cls._cache:
            logger.info(f"IbPortsTool: returning cached topology for {cache_key}")
            return cls._cache[cache_key]

        with allure.step('Discover IB port topology with ibdiagnet'):
            # Imported here to avoid a module-level import cycle: Ib pulls in the
            # component tree, which imports tools from this package.
            from ngts.nvos_tools.ib.Ib import Ib
            from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit

            archive = IbConsts.IBDIAGNET_ZIPPED_FOLDER_PATH + '/' + IbConsts.IBDIAGNET_FILE_NAME
            # Unique absolute path: a bare relative name would collide with the copy
            # the connectivity-json generator leaves in the home directory, and a
            # stale one must never be mistaken for this run's output. The suffix is
            # random rather than the pid, which is the *local* process and can repeat
            # across two containers driving the same DUT.
            parsed_path = f'/tmp/ibdiagnet2_output_{uuid.uuid4().hex[:8]}.txt'

            # ibdiagnet is only wired for NVUE, so pin the api regardless of the
            # random_api / test_api parametrization the caller happens to be under.
            # `engine` is passed explicitly: without it the action runs on whichever
            # DUT is active, and the read below would parse another device's archive.
            with TestToolkit.force_api(ApiType.NVUE):
                Ib(None).ibdiagnet.action_run(
                    command=IbConsts.IBDIAGNET_COMMAND,
                    option=IbConsts.IBDIAGNET_CABLE_INFO,
                    expected_str=IbConsts.IBDIAGNET_EXPECTED_MESSAGE,
                    engine=engine,
                )

            try:
                engine.run_cmd(f'rm -f {parsed_path}', print_output=False)
                engine.run_cmd(f'zcat {archive} > {parsed_path}', print_output=False)
                assert 'OK' in engine.run_cmd(f'test -s {parsed_path} && echo OK'), (
                    f"ibdiagnet produced no readable output on {cache_key}: "
                    f"'{archive}' missing or empty")
                # print_output=False: the dump is ~50k lines / ~3MB, and echoing it
                # would bury the start of every session that uses this fixture in the
                # log and the Allure step.
                raw_output = engine.run_cmd(f'cat {parsed_path}', print_output=False)
                assert raw_output, f"ibdiagnet produced no output on {cache_key}"
            finally:
                engine.run_cmd(f'rm -f {parsed_path}', print_output=False)

            info = build_ports_info(raw_output.splitlines())
            cls._assert_topology_belongs_to(info, engine, cache_key)
            logger.info(f"discovered '{info.description}': {len(info.ports)} ports, "
                        f"{len(info.all_loop_ports())} up loopback pair(s), "
                        f"{len(info.host_connected_ports())} host port(s)")
            cls._cache[cache_key] = info
            return info

    @staticmethod
    def _assert_topology_belongs_to(info: 'IbPortsInfo', engine, cache_key: str):
        """
        Fail fast if the parsed topology is not this DUT's.

        Guards against reading an archive that some other flow generated on a
        different device: every port name returned would then be wrong, and silently
        so. Compared loosely because the ibdiagnet description and the hostname do not
        always agree on the '-mgmt' suffix (croc reports 'croc-94' while its Black
        Mamba counterpart reports 'mamba-248-mgmt2').
        """
        hostname = engine.run_cmd('hostname').strip()
        if not hostname or not info.description:
            logger.warning(f"cannot confirm ibdiagnet topology belongs to {cache_key}: "
                           f"hostname='{hostname}', description='{info.description}'")
            return
        short_hostname = hostname.split('.')[0].replace('-mgmt2', '').replace('-mgmt', '')
        short_description = info.description.split('.')[0].replace('-mgmt2', '').replace('-mgmt', '')
        assert short_hostname == short_description, (
            f"ibdiagnet output on {cache_key} describes '{info.description}' but the DUT is "
            f"'{hostname}' - refusing to return another device's topology")


# ---- Parsing ----------------------------------------------------------------
# Mirrors of the parsers in test_and_update_connectivity_json.py, which is left
# untouched on purpose. Keep the two in sync by hand when the ibdiagnet output
# format changes.


def parse_aggregated_ports(lines: List[str]) -> Dict[str, Any]:
    """
    Parse the 'APort <N> Aggregated label: <agg>' section with its per-plane lines
    into {label: {"aport": int, "planes": {...}}}.
    """
    ports: Dict[str, Any] = {}
    current_label = ""

    for line in lines:
        agg_match = AGG_LABEL_RE.match(line)
        if agg_match:
            current_label = agg_match.group("label")
            ports.setdefault(current_label, {"aport": 0, "planes": {}})
            ports[current_label]["aport"] = int(agg_match.group("aport"))
            continue

        if not current_label:
            continue

        plane_match = PLANE_LINE_RE.match(line)
        if plane_match:
            ports[current_label]["planes"][plane_match.group("plane")] = {
                "state": plane_match.group("state"),
                "port_guid": plane_match.group("port_guid"),
                "node_guid": plane_match.group("node_guid"),
                "port": int(plane_match.group("port_num")),
            }

    return ports


def extract_neighbor_device(desc_quoted: str) -> str:
    """
    Pull the device name out of a quoted neighbor description.

    "MF0;mtvr-croc-19-mgmt2:Q3200_RA/U1" -> "mtvr-croc-19-mgmt2"
    """
    desc = desc_quoted.strip()
    if desc.startswith('"') and desc.endswith('"') and len(desc) >= 2:
        desc = desc[1:-1]
    if ';' in desc:
        desc = desc.split(';', 1)[1]
    if ':' in desc:
        desc = desc.split(':', 1)[0]
    return desc.strip()


def parse_system_and_connections(lines: List[str]) -> Tuple[Dict[str, Any], int]:
    """
    Parse a 'SW SystemGUID: <guid> Description: <desc>' block and the connection
    table that follows it. Returns (system_info, index where parsing stopped).
    """
    system_info: Dict[str, Any] = {"system_guid": "", "description": "", "connections": {}}
    i = 0
    n = len(lines)

    while i < n:
        m = SYSTEM_LINE_RE.match(lines[i])
        if m:
            system_info["system_guid"] = m.group("guid")
            system_info["description"] = m.group("desc")
            i += 1
            break
        i += 1

    while i < n and not lines[i].lstrip().startswith("Label"):
        i += 1
    if i < n and lines[i].lstrip().startswith("Label"):
        i += 1

    while i < n:
        line = lines[i].rstrip("\n")
        if not line.strip() or SYSTEM_LINE_RE.match(line):
            break
        # Pull the trailing quoted neighbor description off first, so the colons
        # inside it do not confuse the field split below.
        desc_match = re.search(r':\s*"([^"]*)"\s*$', line)
        neighbor_desc_clean = ""
        pre = line
        if desc_match:
            neighbor_desc_clean = extract_neighbor_device(desc_match.group(1))
            pre = line[:desc_match.start()]
        parts = [p.strip() for p in pre.split(':')]
        if len(parts) >= 13:
            system_info["connections"][parts[0]] = {
                KEY_STATE: parts[3],
                KEY_PHYSICAL_STATE: parts[4],
                "neighbor_guid": parts[10],
                KEY_CONNECTED_TO: parts[11],
                KEY_NEIGHBOR_DESCRIPTION: neighbor_desc_clean,
            }
        i += 1

    return system_info, i


def build_topology(lines: List[str]) -> Dict[str, Any]:
    """
    Build the connectivity structure {system_guid, description, ports} from
    ibdiagnet output lines. This is the shape written to the connectivity json.
    """
    agg_ports = parse_aggregated_ports(lines)

    systems: List[Dict[str, Any]] = []
    idx = 0
    n = len(lines)
    while idx < n:
        sys_info, next_idx = parse_system_and_connections(lines[idx:])
        if not sys_info.get("system_guid"):
            # No further 'SW SystemGUID' block: the helper already scanned to the end,
            # so advancing one line at a time from here would re-sweep the whole tail
            # of the file per line.
            break
        systems.append(sys_info)
        idx += max(next_idx, 1)

    chosen_system: Dict[str, Any] = {}
    for s in systems:
        if any(label in agg_ports for label in s.get("connections", {})):
            chosen_system = s
            break
    if not chosen_system and systems:
        chosen_system = systems[0]

    description = chosen_system.get("description", "")
    connections = chosen_system.get("connections", {})

    ports_out: Dict[str, Any] = {}
    for label, data in agg_ports.items():
        entry = {"aport": data.get("aport"), "planes": data.get("planes", {})}
        conn = connections.get(label)
        if conn:
            neighbor_device = conn.get(KEY_NEIGHBOR_DESCRIPTION, "")
            entry.update({
                KEY_CONNECTED_TO: conn.get(KEY_CONNECTED_TO),
                "neighbor_guid": conn.get("neighbor_guid"),
                KEY_NEIGHBOR_DESCRIPTION: neighbor_device,
                KEY_STATE: conn.get(KEY_STATE),
                KEY_PHYSICAL_STATE: conn.get(KEY_PHYSICAL_STATE),
                # A loopback is a link whose far end is this same switch.
                KEY_LOOPBACK: bool(description) and neighbor_device == description,
            })
        else:
            entry[KEY_LOOPBACK] = False
        ports_out[label] = entry

    return {
        "system_guid": chosen_system.get("system_guid", ""),
        "description": description,
        "ports": ports_out,
    }


def build_ports_info(lines: List[str]) -> IbPortsInfo:
    """Parse ibdiagnet output lines straight into an :class:`IbPortsInfo`."""
    topology = build_topology(lines)
    return IbPortsInfo(
        description=topology.get("description", ""),
        system_guid=topology.get("system_guid", ""),
        ports=topology.get("ports", {}),
    )
