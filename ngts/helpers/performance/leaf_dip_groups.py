"""
Grouped incremental-DIP plan for the DVS leaf scenario.

The spine traffic generator's unconnected (generator) ports are split into groups, and each group
sweeps its own slice of the leaf DIP range: on every pass through the MAC loopback the generator adds 1
to the DIP, and rewrites the slice's last DIP (limit) back to its first DIP (offset). Slices never
overlap, so at any moment the groups send to different DIPs.

Every port owns the same share of the range, and a group's slice is its ports' shares back to back, so
every DIP is sent at the same rate whatever the group sizes. With staggered starts, each port's first
packet carries the first DIP of its own share; the ports of a group then keep that distance from each
other, because they all advance one DIP per pass and wrap at the same limit.

A port holds several resident packets, and each one starts at its own DIP, spread evenly over the
port's share. Resident packets that shared a DIP would advance in lockstep and reach the same DUT
egress port back to back, so the port would send bursts instead of single packets.

This module only plans. The traffic generator installs the slices (MultiDipsGroupedTG in perf_sys_sdk),
the traffic JSON sets each port's first DIP, and the DUT spreads the DIPs over its right ports with a
seeded permutation (SPCXRALeafDipGroupsDUT).
"""
import ipaddress
import logging
from dataclasses import dataclass
from typing import Dict, List, Tuple

from devts.infra.tools.exceptions.test_issue import TestIssue

logger = logging.getLogger()

IPV4_FAMILY = "ipv4"
IPV6_FAMILY = "ipv6"
IPV6_VERSION = 6
# The generator adds 1 to the low 16 bits of the DIP only, so a slice must not cross a 16-bit boundary.
ALU_WINDOW_SIZE = 1 << 16

# conf_args keys rendered into the DVS dut.jinja. Their presence (with two-sided AR off) selects the
# leaf DIP groups DUT class.
LEAF_DIP_BASE_CONF_KEY = "leaf_dip_base"
LEAF_DIP_COUNT_CONF_KEY = "leaf_dip_count"
LEAF_DIP_SEED_CONF_KEY = "leaf_dip_seed"
LEAF_DIP_CONF_KEYS = (LEAF_DIP_BASE_CONF_KEY, LEAF_DIP_COUNT_CONF_KEY, LEAF_DIP_SEED_CONF_KEY)


@dataclass(frozen=True)
class LeafDipGroupsRequest:
    """Inputs of the leaf DIP groups plan."""
    family: str  # Address family of the run, IPV4_FAMILY or IPV6_FAMILY
    tg_ports: Tuple[int, ...]  # Spine TG generator ports, in the order the TG reports them
    egress_port_count: int  # DUT right ports the DIPs are spread over
    dip_base: str  # First DIP of the leaf range
    dip_count: int  # Number of DIPs in the leaf range
    group_count: int  # Number of generator port groups
    is_staggered: bool = True  # Start each port at its own share instead of at the group offset
    reserved_dips: Tuple[str, ...] = ()  # DIPs the range must not contain (e.g. the reverse direction)
    packets_per_port: int = 1  # Resident packets per generator port, each starting at its own DIP


@dataclass(frozen=True)
class LeafDipPortPlan:
    """Plan of one generator port."""
    tg_port: int  # Spine TG generator port
    start_dip: str  # DIP carried by the port's first packet
    dip_share: int  # DIPs of the group slice this port accounts for
    packet_dips: Tuple[str, ...]  # First DIP of every resident packet, spread over the port's share


@dataclass(frozen=True)
class LeafDipGroupPlan:
    """Plan of one generator port group."""
    index: int  # Group index, 0-based
    offset_dip: str  # First DIP of the group slice, where the limit rule resets to
    limit_dip: str  # Last DIP of the group slice, matched by the limit rule
    port_plans: Tuple[LeafDipPortPlan, ...]  # Ports of the group, in TG order

    @property
    def dip_count(self) -> int:
        """
        Returns:
            int: Number of DIPs in the group slice.
        """
        return int(ipaddress.ip_address(self.limit_dip)) - int(ipaddress.ip_address(self.offset_dip)) + 1


@dataclass(frozen=True)
class LeafDipPlan:
    """The full leaf DIP groups plan."""
    request: LeafDipGroupsRequest  # Inputs the plan was built from
    group_plans: Tuple[LeafDipGroupPlan, ...]  # Groups, in DIP order

    def port_plans(self) -> List[LeafDipPortPlan]:
        """
        Returns:
            list: Every port plan, group by group.
        """
        return [port_plan for group_plan in self.group_plans for port_plan in group_plan.port_plans]

    def dip_range_summary(self) -> str:
        """
        Returns:
            str: The leaf DIP range the groups cover, as "first - last (count DIPs)".
        """
        return f"{self.group_plans[0].offset_dip} - {self.group_plans[-1].limit_dip} ({self.request.dip_count} DIPs)"

    def to_tg_plan_json(self) -> Dict:
        """
        Builds the plan file MultiDipsGroupedTG installs on the spine traffic generator.

        Returns:
            dict: {"groups": [{"index", "ports", "ranges": {family: {"offset", "limit"}}}]}.
        """
        return {"groups": [
            {"index": group_plan.index,
             "ports": [port_plan.tg_port for port_plan in group_plan.port_plans],
             "ranges": {self.request.family: {"offset": group_plan.offset_dip, "limit": group_plan.limit_dip}}}
            for group_plan in self.group_plans]}

    def to_dut_conf_args(self, seed: str) -> Dict:
        """
        Builds the conf_args keys the DVS dut.jinja renders for the leaf DIP groups DUT class.

        Args:
            seed (str): Base seed of the DIP to right port permutation.

        Returns:
            dict: The leaf DIP conf_args keys.
        """
        return {LEAF_DIP_BASE_CONF_KEY: self.request.dip_base,
                LEAF_DIP_COUNT_CONF_KEY: self.request.dip_count,
                LEAF_DIP_SEED_CONF_KEY: seed}

    def to_table(self) -> str:
        """
        Renders the plan as a text table, one row per generator port.

        Returns:
            str: Plan summary followed by the per-port table.
        """
        request = self.request
        rows = [f"family={request.family}, DIPs={request.dip_count} from {request.dip_base}, "
                f"groups={len(self.group_plans)}, generator ports={len(request.tg_ports)}, "
                f"right ports={request.egress_port_count}, staggered={request.is_staggered}, "
                f"packets per port={request.packets_per_port}",
                "group | offset | limit | slice size | tg port | share | packet DIPs (first..last)"]
        for group_plan in self.group_plans:
            for port_plan in group_plan.port_plans:
                rows.append(f"{group_plan.index} | {group_plan.offset_dip} | {group_plan.limit_dip} | "
                            f"{group_plan.dip_count} | {hex(port_plan.tg_port)} | {port_plan.dip_share} | "
                            f"{port_plan.packet_dips[0]}..{port_plan.packet_dips[-1]}")
        return "\n".join(rows)


@dataclass(frozen=True)
class GroupResetSummary:
    """Limit resets of one group and family, as counted on the spine TG during the run."""
    index: int  # Group index
    family: str  # Address family of the counter
    port_count: int  # Generator ports in the group
    slice_size: int  # DIPs in the group slice, 0 when the plan ranges were not recorded
    resets: int  # Limit rule hits; -1 when the counter could not be read
    relative_port_rate: float  # Per-port packet rate against the fastest group, 0.0 when not able to compute


def summarize_reset_counts(resets_json: Dict) -> List[GroupResetSummary]:
    """
    Turns the reset counts saved by RemoveMultiDipsGroupedTG into per-group port rates.

    Every resident packet resets once per pass over its group slice, so over the run a group resets
    (ports x per-port packet rate x time) / slice size times. resets x slice size / ports is therefore
    proportional to the group's average per-port rate, and dividing by the fastest group makes groups
    directly comparable: 1.0 is the fastest group, 0.9 a group whose ports sent 10% fewer packets.

    Args:
        resets_json (dict): {"groups": [{"index", "ports", "families": {family: {"offset", "limit",
            "resets"}}}]}, as written by RemoveMultiDipsGroupedTG.

    Returns:
        list: One summary per group and family, in group order.
    """
    summaries = []
    for group in resets_json["groups"]:
        for family, family_resets in group["families"].items():
            slice_size = 0
            if family_resets.get("offset") and family_resets.get("limit"):
                slice_size = (int(ipaddress.ip_address(family_resets["limit"])) -
                              int(ipaddress.ip_address(family_resets["offset"])) + 1)
            resets = family_resets.get("resets")
            summaries.append(GroupResetSummary(index=group["index"], family=family, port_count=len(group["ports"]),
                                               slice_size=slice_size, resets=-1 if resets is None else resets,
                                               relative_port_rate=0.0))
    rate_indexes = [summary.resets * summary.slice_size / summary.port_count
                    if summary.resets >= 0 and summary.slice_size else 0.0 for summary in summaries]
    fastest = max(rate_indexes, default=0.0)
    if not fastest:
        return summaries
    return [GroupResetSummary(index=summary.index, family=summary.family, port_count=summary.port_count,
                              slice_size=summary.slice_size, resets=summary.resets,
                              relative_port_rate=rate_index / fastest)
            for summary, rate_index in zip(summaries, rate_indexes)]


def format_reset_summary(summaries: List[GroupResetSummary], resets_json: Dict) -> str:
    """
    Renders the per-group reset summary as a text table.

    Args:
        summaries (list): Output of summarize_reset_counts.
        resets_json (dict): Raw reset counts, for the port list of every group.

    Returns:
        str: Table, one row per group and family.
    """
    ports_by_group = {group["index"]: group["ports"] for group in resets_json["groups"]}
    rows = ["group | family | ports | slice size | limit resets | relative port rate | generator ports"]
    for summary in summaries:
        ports = ports_by_group.get(summary.index, [])
        port_range = f"{hex(ports[0])}..{hex(ports[-1])}" if ports else "-"
        rows.append(f"{summary.index} | {summary.family} | {summary.port_count} | {summary.slice_size} | "
                    f"{summary.resets} | {summary.relative_port_rate:.3f} | {port_range}")
    return "\n".join(rows)


def split_evenly(total: int, part_count: int) -> List[int]:
    """
    Splits total into part_count integers that differ by at most 1, larger parts first.

    Args:
        total (int): Value to split.
        part_count (int): Number of parts.

    Returns:
        list: part_count integers summing to total.
    """
    base_size, remainder = divmod(total, part_count)
    return [base_size + 1] * remainder + [base_size] * (part_count - remainder)


def build_leaf_dip_plan(request: LeafDipGroupsRequest) -> LeafDipPlan:
    """
    Builds and self-checks the leaf DIP groups plan.

    Ports are split into group_count groups of sizes that differ by at most 1, taken in TG order. The
    range is split into one share per port, shares differing by at most 1, and each group's slice is its
    ports' shares back to back, so slices are contiguous, disjoint and cover the range exactly once.
    Resident packet j of a port starts at the port's start + j * share // packets_per_port.

    Args:
        request (LeafDipGroupsRequest): Plan inputs.

    Returns:
        LeafDipPlan: The plan.

    Raises:
        TestIssue: If the request cannot be planned, or the built plan fails its own checks.
    """
    _validate_request(request)
    dip_shares = split_evenly(request.dip_count, len(request.tg_ports))
    ports_per_group = split_evenly(len(request.tg_ports), request.group_count)
    base_value = int(ipaddress.ip_address(request.dip_base))
    group_plans = []
    port_index = 0
    slice_start = base_value
    for group_index, group_port_count in enumerate(ports_per_group):
        port_plans = []
        share_start = slice_start
        for _ in range(group_port_count):
            dip_share = dip_shares[port_index]
            start_value = share_start if request.is_staggered else slice_start
            packet_dips = tuple(str(ipaddress.ip_address(start_value + packet_index * dip_share //
                                                         request.packets_per_port))
                                for packet_index in range(request.packets_per_port))
            port_plans.append(LeafDipPortPlan(tg_port=request.tg_ports[port_index],
                                              start_dip=str(ipaddress.ip_address(start_value)),
                                              dip_share=dip_share, packet_dips=packet_dips))
            share_start += dip_share
            port_index += 1
        group_plans.append(LeafDipGroupPlan(index=group_index,
                                            offset_dip=str(ipaddress.ip_address(slice_start)),
                                            limit_dip=str(ipaddress.ip_address(share_start - 1)),
                                            port_plans=tuple(port_plans)))
        slice_start = share_start
    plan = LeafDipPlan(request=request, group_plans=tuple(group_plans))
    _verify_plan(plan)
    logger.info(f"Leaf DIP groups plan: {request.dip_count} {request.family} DIPs from {request.dip_base} "
                f"over {len(group_plans)} groups of {sorted(set(ports_per_group))} ports, "
                f"slice sizes {sorted({group_plan.dip_count for group_plan in group_plans})}, "
                f"{request.packets_per_port} resident packets per port")
    smallest_share = min(dip_shares)
    if request.packets_per_port > smallest_share:
        logger.warning(f"Leaf DIP resident packets outnumber the DIPs of a port share | expected "
                       f"<= {smallest_share} packets per port for distinct DIPs, got {request.packets_per_port}: "
                       f"some packets of a port share a DIP and reach the same egress port back to back")
    return plan


def _validate_request(request: LeafDipGroupsRequest) -> None:
    """
    Checks that a request can be planned.

    Args:
        request (LeafDipGroupsRequest): Plan inputs.

    Raises:
        TestIssue: On the first violated constraint, with the expected and received values.
    """
    port_count = len(request.tg_ports)
    if request.family not in (IPV4_FAMILY, IPV6_FAMILY):
        raise TestIssue(f"Unknown leaf DIP family | expected {IPV4_FAMILY} or {IPV6_FAMILY}, got {request.family}")
    if port_count == 0 or len(set(request.tg_ports)) != port_count:
        raise TestIssue(f"Leaf DIP generator ports must be non-empty and unique | got {request.tg_ports}")
    if not 1 <= request.group_count <= port_count:
        raise TestIssue(f"Leaf DIP group count out of range | expected 1..{port_count} (generator ports), "
                        f"got {request.group_count}")
    if request.packets_per_port < 1:
        raise TestIssue(f"Leaf DIP packets per port must be positive | expected >= 1, "
                        f"got {request.packets_per_port}")
    minimum_dip_count = max(port_count, request.egress_port_count)
    if request.dip_count < minimum_dip_count:
        raise TestIssue(f"Leaf DIP count too small | expected >= {minimum_dip_count} (one per generator port "
                        f"and per right port), got {request.dip_count}")
    base_address = ipaddress.ip_address(request.dip_base)
    if (base_address.version == IPV6_VERSION) != (request.family == IPV6_FAMILY):
        raise TestIssue(f"Leaf DIP base does not match the family | family={request.family}, "
                        f"base={request.dip_base}")
    window_offset = int(base_address) % ALU_WINDOW_SIZE
    if window_offset + request.dip_count > ALU_WINDOW_SIZE:
        raise TestIssue(f"Leaf DIP range crosses a 16-bit boundary, which the generator +1 cannot carry | "
                        f"expected count <= {ALU_WINDOW_SIZE - window_offset} from base {request.dip_base}, "
                        f"got {request.dip_count}")
    base_value = int(base_address)
    for reserved_dip in request.reserved_dips:
        reserved_value = int(ipaddress.ip_address(reserved_dip))
        if base_value <= reserved_value < base_value + request.dip_count:
            raise TestIssue(f"Leaf DIP range contains a reserved DIP | range {request.dip_base} + "
                            f"{request.dip_count} must exclude {reserved_dip}")


def _verify_plan(plan: LeafDipPlan) -> None:
    """
    Checks the built plan: slices contiguous and disjoint covering the range once, every port once, and
    every first DIP inside its own group slice.

    Args:
        plan (LeafDipPlan): Plan to check.

    Raises:
        TestIssue: On the first violated property.
    """
    request = plan.request
    expected_start = int(ipaddress.ip_address(request.dip_base))
    for group_plan in plan.group_plans:
        offset_value = int(ipaddress.ip_address(group_plan.offset_dip))
        limit_value = int(ipaddress.ip_address(group_plan.limit_dip))
        if offset_value != expected_start or limit_value < offset_value:
            raise TestIssue(f"Leaf DIP group {group_plan.index} slice is not contiguous | expected offset "
                            f"{ipaddress.ip_address(expected_start)}, "
                            f"got {group_plan.offset_dip}..{group_plan.limit_dip}")
        for port_plan in group_plan.port_plans:
            start_value = int(ipaddress.ip_address(port_plan.start_dip))
            if not offset_value <= start_value <= limit_value:
                raise TestIssue(f"Leaf DIP port {hex(port_plan.tg_port)} starts outside its group slice | "
                                f"expected {group_plan.offset_dip}..{group_plan.limit_dip}, got {port_plan.start_dip}")
            _verify_packet_dips(group_plan, port_plan, request.packets_per_port)
        expected_start = limit_value + 1
    covered_count = expected_start - int(ipaddress.ip_address(request.dip_base))
    if covered_count != request.dip_count:
        raise TestIssue(f"Leaf DIP slices do not cover the range | expected {request.dip_count} DIPs, "
                        f"got {covered_count}")
    planned_ports = [port_plan.tg_port for port_plan in plan.port_plans()]
    if sorted(planned_ports) != sorted(request.tg_ports):
        raise TestIssue(f"Leaf DIP plan does not use every generator port exactly once | expected "
                        f"{sorted(request.tg_ports)}, got {sorted(planned_ports)}")


def _verify_packet_dips(group_plan: LeafDipGroupPlan, port_plan: LeafDipPortPlan, packets_per_port: int) -> None:
    """
    Checks that a port has one first DIP per resident packet, all inside its group slice, and all distinct
    whenever its share has room for that.

    Args:
        group_plan (LeafDipGroupPlan): Group of the port.
        port_plan (LeafDipPortPlan): Port to check.
        packets_per_port (int): Resident packets per port.

    Raises:
        TestIssue: On the first violated property.
    """
    packet_values = [int(ipaddress.ip_address(packet_dip)) for packet_dip in port_plan.packet_dips]
    offset_value = int(ipaddress.ip_address(group_plan.offset_dip))
    limit_value = int(ipaddress.ip_address(group_plan.limit_dip))
    if len(packet_values) != packets_per_port:
        raise TestIssue(f"Leaf DIP port {hex(port_plan.tg_port)} has the wrong number of packet DIPs | "
                        f"expected {packets_per_port}, got {len(packet_values)}")
    if not all(offset_value <= packet_value <= limit_value for packet_value in packet_values):
        raise TestIssue(f"Leaf DIP port {hex(port_plan.tg_port)} has packet DIPs outside its group slice | "
                        f"expected {group_plan.offset_dip}..{group_plan.limit_dip}, got {port_plan.packet_dips}")
    expected_distinct = min(packets_per_port, port_plan.dip_share)
    if len(set(packet_values)) != expected_distinct:
        raise TestIssue(f"Leaf DIP port {hex(port_plan.tg_port)} packet DIPs collide | expected "
                        f"{expected_distinct} distinct DIPs, got {len(set(packet_values))}")
