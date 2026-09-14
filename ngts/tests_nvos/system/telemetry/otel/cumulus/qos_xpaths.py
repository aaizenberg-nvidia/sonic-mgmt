"""OpenConfig QoS gNMI xpath generators for Cumulus interface/QoS telemetry."""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple


QOS_UNSUPPORTED_XPATH_SUBSTRINGS: Sequence[str] = (
    "shared-buffer",
    "headroom-buffer",
)

# Spectrum-6 does not expose output queues 12–15.
SPECTRUM6_UNSUPPORTED_QUEUES: Sequence[int] = (12, 13, 14, 15)


def is_spectrum6_asic(asic_model: Optional[str]) -> bool:
    """True when *asic_model* identifies a Spectrum-6 ASIC."""
    if not asic_model:
        return False
    normalized = str(asic_model).strip().lower().replace(" ", "")
    return normalized in ("spectrum-6", "spectrum6", "spc6")


def qos_output_queue_ranges(
    *,
    asic_model: Optional[str] = None,
) -> Tuple[List[int], List[int]]:
    """Return ``(queues, max_queue_len_queues)`` for the DUT ASIC."""
    queues = list(range(16))
    max_queue_len_queues = list(range(17))
    if is_spectrum6_asic(asic_model):
        unsupported = set(SPECTRUM6_UNSUPPORTED_QUEUES)
        queues = [queue for queue in queues if queue not in unsupported]
        max_queue_len_queues = [
            queue for queue in max_queue_len_queues if queue not in unsupported
        ]
    return queues, max_queue_len_queues


def qos_unsupported_xpath_substrings(
    asic_model: Optional[str] = None,
) -> Tuple[str, ...]:
    """Return XPath fragments ignored for the specified ASIC."""
    substrings = list(QOS_UNSUPPORTED_XPATH_SUBSTRINGS)
    if is_spectrum6_asic(asic_model):
        substrings.extend(
            f"queue[name={queue}]" for queue in SPECTRUM6_UNSUPPORTED_QUEUES
        )
    return tuple(substrings)


def filter_unsupported_qos_xpaths(
    xpaths: Iterable[str],
    *,
    asic_model: Optional[str] = None,
) -> List[str]:
    """Drop shared/headroom-buffer paths and Spectrum-6 queues 12–15."""
    unsupported_substrings = qos_unsupported_xpath_substrings(asic_model)
    filtered: List[str] = []
    for xpath in xpaths:
        text = str(xpath)
        lowered = text.lower()
        if any(unsupported in lowered for unsupported in unsupported_substrings):
            continue
        filtered.append(text)
    return filtered


# Output-queue leaf counters (queues 0–15).
QOS_OUTPUT_QUEUE_STATE_LEAVES: Sequence[str] = (
    "transmit-pkts",
    "transmit-octets",
    "ecn-marked-pkts",
    "no-buffer-uc-dropped-pkts",
    "wred-dropped-pkts",
    "instant-queue-len",
    "transmit-unicast-pkts",
)

# Switch-priority counters / durations (priorities 0–7).
QOS_SWITCH_PRIORITY_LEAVES: Sequence[str] = (
    "counters/in-pause-pkts",
    "counters/out-pause-pkts",
    "counters/in-discards",
    "in-pause-duration",
    "out-pause-duration",
)

# Input priority-group counters (pg-id 0–7).
QOS_PRIORITY_GROUP_LEAVES: Sequence[str] = (
    "counters/in-pkts",
    "counters/in-octets",
)


def qos_interface_base(interface_id: str) -> str:
    """Root QoS interface xpath, e.g. ``/qos/interfaces/interface[interface-id=swp1]``."""
    return f"/qos/interfaces/interface[interface-id={interface_id}]"


def generate_qos_container_xpaths(interface_id: str) -> List[str]:
    """Supported parent container paths for one interface."""
    base = qos_interface_base(interface_id)
    return filter_unsupported_qos_xpaths(
        [
            base,
            f"{base}/output/queues",
            f"{base}/input",
            f"{base}/input/headroom-buffer",
        ]
    )


def generate_qos_output_queue_xpaths(
    interface_id: str,
    *,
    queues: Iterable[int] = range(16),
    max_queue_len_queues: Iterable[int] = range(17),
    leaves: Sequence[str] = QOS_OUTPUT_QUEUE_STATE_LEAVES,
) -> List[str]:
    """Output queue state leaves.

    Default: common leaves for queues ``0–15``; ``max-queue-len`` for ``0–16``.
    """
    base = qos_interface_base(interface_id)
    paths: List[str] = []
    for queue in queues:
        queue_base = f"{base}/output/queues/queue[name={queue}]/state"
        for leaf in leaves:
            paths.append(f"{queue_base}/{leaf}")
    for queue in max_queue_len_queues:
        paths.append(
            f"{base}/output/queues/queue[name={queue}]/state/max-queue-len"
        )
    return paths


def generate_qos_switch_priority_xpaths(
    interface_id: str,
    *,
    priorities: Iterable[int] = range(8),
    leaves: Sequence[str] = QOS_SWITCH_PRIORITY_LEAVES,
) -> List[str]:
    """Switch-priority paths for priorities ``0–7`` by default."""
    base = qos_interface_base(interface_id)
    paths: List[str] = []
    for priority in priorities:
        sp_base = f"{base}/switch-priority[priority={priority}]/state"
        for leaf in leaves:
            paths.append(f"{sp_base}/{leaf}")
    return paths


def generate_qos_priority_group_xpaths(
    interface_id: str,
    *,
    priority_groups: Iterable[int] = range(8),
    leaves: Sequence[str] = QOS_PRIORITY_GROUP_LEAVES,
) -> List[str]:
    """Input priority-group paths for ``pg-id`` ``0–7`` by default."""
    base = qos_interface_base(interface_id)
    paths: List[str] = []
    for pg_id in priority_groups:
        pg_base = f"{base}/input/priority-group[pg-id={pg_id}]/state"
        for leaf in leaves:
            paths.append(f"{pg_base}/{leaf}")
    return paths


def generate_qos_xpaths(
    interface_id: str,
    *,
    include_containers: bool = True,
    include_output_queues: bool = True,
    include_switch_priority: bool = True,
    include_priority_groups: bool = True,
    queues: Optional[Iterable[int]] = None,
    max_queue_len_queues: Optional[Iterable[int]] = None,
    priorities: Optional[Iterable[int]] = None,
    priority_groups: Optional[Iterable[int]] = None,
    asic_model: Optional[str] = None,
) -> List[str]:
    """Generate the full QoS xpath set for ``interface_id`` (e.g. ``swp1``).

    On Spectrum-6, output queues ``12–15`` are omitted unless *queues* /
    *max_queue_len_queues* are supplied explicitly.
    """
    default_queues, default_max_queue_len_queues = qos_output_queue_ranges(
        asic_model=asic_model,
    )
    paths: List[str] = []
    if include_containers:
        paths.extend(generate_qos_container_xpaths(interface_id))
    if include_output_queues:
        paths.extend(
            generate_qos_output_queue_xpaths(
                interface_id,
                queues=queues if queues is not None else default_queues,
                max_queue_len_queues=(
                    max_queue_len_queues
                    if max_queue_len_queues is not None
                    else default_max_queue_len_queues
                ),
            )
        )
    if include_switch_priority:
        paths.extend(
            generate_qos_switch_priority_xpaths(
                interface_id,
                priorities=priorities if priorities is not None else range(8),
            )
        )
    if include_priority_groups:
        paths.extend(
            generate_qos_priority_group_xpaths(
                interface_id,
                priority_groups=(
                    priority_groups if priority_groups is not None else range(8)
                ),
            )
        )
    return filter_unsupported_qos_xpaths(paths, asic_model=asic_model)
