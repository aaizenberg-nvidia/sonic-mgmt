"""
Thin wrapper over existing ``devices.dut`` fields for the zombie-link test.

No new DUT state; no class-name branching. Each property picks the first
already-populated field for the role, with a derived fallback computed from
other existing DUT fields:

- ``ports_under_test`` — first non-empty of:
  1. ``dut.nvl_access_ports_list`` (NVL5 / NVL6 access ports).
  2. Traffic ports derived from ``dut.interface_list`` by subtracting
     ``network_ports``, ``mgmt_ports`` and ``fnm_external_port_list`` — yields
     ``sw<N>p<M>`` (Black Mamba), ``sw<N>p1`` (Taipan), ``sw<A|B><N>p<M>``
     (Crocodile), without any platform-specific shape hardcoded here.

- ``fnm_ports_under_test`` — first non-empty of:
  1. ``dut.nvl_internal_fnm_ports`` (NVL5 / NVL6 internal FNM list).
  2. ``dut.fnm_external_port_list`` (external FNM, e.g. ``['fnm1']``). On
     multi-ASIC platforms the internal-FNM (``fnma<N>p<M>``) apply path is not
     consistently wired up — ``nv config apply`` rejects with "Interface does
     not exist" — so we test the external FNM port instead.

Both properties return ``None`` when nothing is populated; the test
pytest.skips that case cleanly. Adding a new platform = populate one of
these existing DUT fields; this file stays untouched.

``build_port_range`` reads the platform's NVUE range-grammar separator
from ``dut.port_range_group_separator`` (see ``IbSwitch._init_constants``):
Crocodile requires ``','`` between switch-prefix groups (``swA…,swB…``);
every other IB platform emits a single group so the default ``''`` works.
"""

import re
from collections import defaultdict
from typing import List, Optional


_PORT_SEG_RE = re.compile(r'([a-zA-Z]+)(\d+)')


def _first_non_empty(*candidates):
    """Return the first non-empty iterable as a list, or ``None``."""
    for c in candidates:
        if c:
            return list(c)
    return None


def _traffic_ports_from_interface_list(dut) -> List[str]:
    """Derive the traffic-port subset from ``dut.interface_list``.

    Strips mgmt/network and external-FNM entries — leaves only the
    switching ports (``sw*p*`` family, regardless of platform-specific shape).
    """
    iface_list = getattr(dut, 'interface_list', None) or []
    exclude = set()
    for attr in ('network_ports', 'mgmt_ports', 'fnm_external_port_list'):
        exclude.update(getattr(dut, attr, None) or [])
    return [p for p in iface_list if p not in exclude]


class ZombieLinkManager:
    """Reads existing DUT port fields, with fallback per role."""

    def __init__(self, devices):
        self.devices = devices
        self.dut = devices.dut

    @property
    def ports_under_test(self) -> Optional[List[str]]:
        """Ports for the range-configuration test. ``None`` skips."""
        return _first_non_empty(
            getattr(self.dut, 'nvl_access_ports_list', None),
            _traffic_ports_from_interface_list(self.dut),
        )

    @property
    def fnm_ports_under_test(self) -> Optional[List[str]]:
        """FNM ports (internal or external) for the FNM test. ``None`` skips."""
        return _first_non_empty(
            getattr(self.dut, 'nvl_internal_fnm_ports', None),
            getattr(self.dut, 'fnm_external_port_list', None),
        )

    def build_port_range(self, ports: List[str]) -> str:
        """Compress a port list into a NVUE-friendly range string, per port group.

        Partitions ports by their leading prefix (e.g. ``swA`` vs ``swB`` on
        Crocodile) so each group emits its own ``<prefix>{mn}-{mx}`` chain
        independently. Within a group, segments keep the order they appear in
        the port name. Single-value numeric segments are collapsed to just the
        number (so ``['sw1p1', ..., 'sw144p1']`` yields ``sw1-144p1``, not
        ``sw1-144p1-1`` which NVUE rejects).

        Groups are joined by ``dut.port_range_group_separator`` — that
        attribute encodes the platform's NVUE grammar (``','`` on Crocodile,
        ``''`` everywhere else).

        Examples:
        - ``['acp1', ..., 'acp72']``                       -> ``acp1-72``
        - ``['sw1p1', ..., 'sw144p1']``                    -> ``sw1-144p1``
        - ``['sw1p1', 'sw1p2', ..., 'sw72p2']``            -> ``sw1-72p1-2``
        - ``['swA1p1', ..., 'swB18p2']`` (Crocodile)       -> ``swA1-18p1-2,swB1-18p1-2``
        """
        group_segments = defaultdict(lambda: defaultdict(set))
        group_order = {}
        for port in ports:
            tokens = _PORT_SEG_RE.findall(port)
            if not tokens:
                continue
            group = tokens[0][0]
            for prefix, num in tokens:
                group_segments[group][prefix].add(int(num))
            group_order.setdefault(group, [pref for pref, _ in tokens])

        group_strs = []
        for group, prefixes in group_segments.items():
            chunks = []
            for prefix in group_order[group]:
                numbers = prefixes[prefix]
                if not numbers:
                    continue
                mn, mx = min(numbers), max(numbers)
                chunks.append(f"{prefix}{mn}-{mx}" if mn != mx else f"{prefix}{mn}")
            if chunks:
                group_strs.append(''.join(chunks))
        separator = getattr(self.dut, 'port_range_group_separator', '')
        return separator.join(group_strs)


def get_zombie_link_manager(devices) -> ZombieLinkManager:
    return ZombieLinkManager(devices)
