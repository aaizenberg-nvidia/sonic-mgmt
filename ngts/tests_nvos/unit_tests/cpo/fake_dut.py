"""Fake DUT for dry-running the real CPO show tests offline (test_cpo_dryrun.py).

The fake switch state is a single nested "show tree": `nv show <segments...>`
resolves by walking nested nodes keyed by CLI path segments, and the matched
output is returned as CLI JSON. Any path that is not in the tree fails the
NVUE way (error text that SendCommandTool flags), so negative flows like
`nv show interface <acp-port> cpo` need no special-casing.

Adding a future show test to the dry run:
  1. If it shows a CLI area the tree does not serve yet, graft that area's
     outputs onto the tree in build_show_tree() with _insert() calls (reuse or
     add generators in sample_outputs.py).
  2. Register the test function in DRY_RUN_TESTS in test_cpo_dryrun.py. If it
     takes a fixture the runner does not fake yet, the runner fails with an
     explicit message telling you to add it.
"""

import copy
import json

from ngts.nvos_constants.constants_nvos import Cpov2Consts, HealthConsts
from ngts.tests_nvos.unit_tests.cpo import sample_outputs as samples

_HEALTHY_INSTANCE = {"state": "HEALTHY", "last-unhealthy": "", "unhealthy-count": "0"}


class FakeDutEngine:
    """LinuxSshEngine stand-in that answers `nv show <path>` from a show tree."""

    ip = "192.0.2.1"
    ssh_port = 22
    open_api_port = 443
    username = "admin"
    password = "admin"

    def __init__(self, show_tree: dict):
        self._show_tree = show_tree
        self.commands: list[str] = []
        self.engine = self

    def run_cmd(self, cmd: str, **kwargs) -> str:
        self.commands.append(cmd)
        tokens = cmd.split(" --", 1)[0].split()
        if tokens[:2] != ["nv", "show"]:
            raise NotImplementedError(f"FakeDutEngine only serves 'nv show' commands, got: {cmd}")
        output = _resolve(self._show_tree, tokens[2:])
        if output is None:
            return "Error: The requested item does not exist."
        return json.dumps(output)


def _new_node() -> dict:
    return {"output": None, "children": {}}


def _insert(tree: dict, path: str, output: dict) -> None:
    node = tree
    for segment in path.split():
        node = node["children"].setdefault(segment, _new_node())
    node["output"] = output


def _resolve(tree: dict, segments: list[str]) -> dict | None:
    node = tree
    for segment in segments:
        node = node["children"].get(segment)
        if node is None:
            return None
    return node["output"]


def _partition(items: list[str], buckets: list[str]) -> dict[str, list[str]]:
    assert len(items) % len(buckets) == 0, f"cannot partition {len(items)} items into {len(buckets)} equal buckets"
    size = len(items) // len(buckets)
    return {bucket: items[i * size:(i + 1) * size] for i, bucket in enumerate(buckets)}


def _interface_cpo_detail(cpo: str, cpo_detail: dict, oe: str, channels: list[str]) -> dict:
    """`nv show interface <port> cpo`: parent header as-is + the port's oe/channel slice."""
    header = {
        field: value for field, value in cpo_detail.items() if field not in (Cpov2Consts.OE, Cpov2Consts.CHANNEL)
    }
    return copy.deepcopy({
        Cpov2Consts.PARENT: cpo,
        **header,
        Cpov2Consts.OE: {oe: cpo_detail[Cpov2Consts.OE][oe]},
        Cpov2Consts.CHANNEL: {ch: cpo_detail[Cpov2Consts.CHANNEL][ch] for ch in channels},
    })


def _insert_with_drilldowns(tree: dict, path: str, detail: dict, subtrees: tuple[str, ...]) -> None:
    """Insert an instance detail plus its per-subtree and per-entry drill-down paths."""
    _insert(tree, path, detail)
    for subtree in subtrees:
        _insert(tree, f"{path} {subtree}", detail[subtree])
        for name, entry in detail[subtree].items():
            _insert(tree, f"{path} {subtree} {name}", entry)


def build_show_tree(device) -> dict:
    """Generate the full fake show state from a CPO-capable device object.

    Ports are partitioned into equal contiguous slices per CPO; within a CPO,
    port i maps to one OE (even blocks) and one channel (channel i). The exact
    identity is arbitrary - the show tests only check slice/subset contracts.
    """
    topology = device.cpo
    ports_by_cpo = _partition(list(device.nvl_trunk_ports_list), topology.cpo_names())
    tree = _new_node()

    _insert(tree, "platform cpo",
            {cpo: samples.make_cpo_summary(cpo, ports=ports_by_cpo[cpo]) for cpo in topology.cpo_names()})
    for cpo in topology.cpo_names():
        detail = samples.make_cpo_detail(cpo, ports=ports_by_cpo[cpo])
        _insert_with_drilldowns(tree, f"platform cpo {cpo}", detail, (Cpov2Consts.OE, Cpov2Consts.CHANNEL))

        oes = topology.oes_for_cpo(cpo)
        channels = topology.channels_for_cpo(cpo)
        ports = ports_by_cpo[cpo]
        for i, port in enumerate(ports):
            oe = oes[i * len(oes) // len(ports)]
            port_detail = _interface_cpo_detail(cpo, detail, oe, [channels[i]])
            _insert_with_drilldowns(tree, f"interface {port} cpo", port_detail,
                                    (Cpov2Consts.OE, Cpov2Consts.CHANNEL))

    _insert(tree, "platform laser-source", samples.SHOW_PLATFORM_LASER_SOURCE)
    for els in topology.els_names():
        _insert_with_drilldowns(tree, f"platform laser-source {els}", samples.make_laser_source_detail(els),
                                (Cpov2Consts.LASER,))

    _insert(tree, "interface",
            {port: {} for port in device.nvl_access_ports_list + device.nvl_trunk_ports_list})
    _insert(tree, "platform transceiver",
            {name: {"identifier": "QSFP-DD"} for name in device.nvl_fnm_ports})

    health = copy.deepcopy(samples.SHOW_SYSTEM_HEALTH_COMPONENT_CPO)
    health[HealthConsts.Component.Transceiver] = {
        HealthConsts.Component.INSTANCE: {name: dict(_HEALTHY_INSTANCE) for name in device.nvl_fnm_ports}
    }
    _insert(tree, "system health component", health)
    return tree
