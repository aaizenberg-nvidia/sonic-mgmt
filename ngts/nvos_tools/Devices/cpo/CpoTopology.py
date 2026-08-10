import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol, TypeGuard, runtime_checkable

from ngts.nvos_tools.infra.ResultObj import ResultObj

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CpoTopology:
    """Gen2 (Portia / QM5 / NVL7) CPO vModule topology.

    Single source of truth for the *expected* number of CPOs, optical engines,
    laser-sources, lasers and channels on a CPO-capable device, and for their
    deterministic names and relationships. One CPO == one vModule == one ASIC.

    This is intentionally decoupled from the Gen1 (Taipan) CpoCapability model:
    here CPO / OE / ELS are first-class objects, not entries in transceiver_list,
    and the layout is generated from counts instead of hand-authored mapping dicts.

    Immutable (`frozen=True`) and `slots=True`: instances cannot be mutated after
    creation and cannot grow stray attributes (typos raise AttributeError), which
    also lowers per-instance memory and speeds attribute access.
    """

    cpo_count: int
    oe_per_cpo: int = 4
    els_per_cpo: int = 1
    lasers_per_els: int = 16
    lanes_per_oe: int = 16
    channels_per_cpo: int = 64

    # Name prefixes. ClassVar => shared class constants, NOT dataclass fields, so
    # they are excluded from __init__, from equality and from __slots__.
    CPO_PREFIX: ClassVar[str] = "cpo"
    OE_PREFIX: ClassVar[str] = "oe"
    ELS_PREFIX: ClassVar[str] = "els"
    LASER_PREFIX: ClassVar[str] = "laser-"
    CHANNEL_PREFIX: ClassVar[str] = "channel-"

    def __post_init__(self) -> None:
        for count_field in (
            "cpo_count",
            "oe_per_cpo",
            "els_per_cpo",
            "lasers_per_els",
            "lanes_per_oe",
            "channels_per_cpo",
        ):
            value = getattr(self, count_field)
            if value <= 0:
                raise ValueError(f"{count_field} must be positive, got {value}")
        expected_channels = self.oe_per_cpo * self.lanes_per_oe
        if expected_channels != self.channels_per_cpo:
            logger.warning(
                "CpoTopology: channels_per_cpo=%s != oe_per_cpo*lanes_per_oe=%s",
                self.channels_per_cpo,
                expected_channels,
            )
        # derived, so it needs its own check: floor division would otherwise
        # yield a silent 0 that only surfaces much later in laser_sibling_ports
        if self.lanes_per_laser <= 0:
            raise ValueError(
                f"channels_per_cpo={self.channels_per_cpo} cannot feed "
                f"els_per_cpo*lasers_per_els={self.els_per_cpo * self.lasers_per_els} lasers"
            )

    # ------------------------------------------------------------------ counts
    @property
    def oe_count(self) -> int:
        return self.cpo_count * self.oe_per_cpo

    @property
    def els_count(self) -> int:
        return self.cpo_count * self.els_per_cpo

    # ------------------------------------------------------------------- names
    def cpo_names(self) -> list[str]:
        return [f"{self.CPO_PREFIX}{i}" for i in range(1, self.cpo_count + 1)]

    def els_names(self) -> list[str]:
        return [f"{self.ELS_PREFIX}{i}" for i in range(1, self.els_count + 1)]

    def oe_names(self) -> list[str]:
        """OE names are global: oe1..oe(cpo_count*oe_per_cpo) across the device."""
        return [f"{self.OE_PREFIX}{i}" for i in range(1, self.oe_count + 1)]

    def laser_names(self) -> list[str]:
        """Laser names within a single ELS (identical for every ELS)."""
        return [f"{self.LASER_PREFIX}{i}" for i in range(1, self.lasers_per_els + 1)]

    def channel_names(self) -> list[str]:
        """Channel names within a single CPO (identical for every CPO)."""
        return [
            f"{self.CHANNEL_PREFIX}{i}" for i in range(1, self.channels_per_cpo + 1)
        ]

    # ----------------------------------------------------------- relationships
    def oes_for_cpo(self, cpo: str | int) -> list[str]:
        idx = self._index(cpo, self.CPO_PREFIX, self.cpo_count)
        start = (idx - 1) * self.oe_per_cpo + 1
        return [f"{self.OE_PREFIX}{i}" for i in range(start, start + self.oe_per_cpo)]

    def els_for_cpo(self, cpo: str | int) -> list[str]:
        idx = self._index(cpo, self.CPO_PREFIX, self.cpo_count)
        start = (idx - 1) * self.els_per_cpo + 1
        return [f"{self.ELS_PREFIX}{i}" for i in range(start, start + self.els_per_cpo)]

    def channels_for_cpo(self, cpo: str | int) -> list[str]:
        """Channel names of a CPO (identical for every CPO; the arg is validated)."""
        self._index(cpo, self.CPO_PREFIX, self.cpo_count)
        return self.channel_names()

    def lasers_for_els(self, els: str | int) -> list[str]:
        """Laser names of an ELS (identical for every ELS; the arg is validated)."""
        self._index(els, self.ELS_PREFIX, self.els_count)
        return self.laser_names()

    def subcomponents_for_cpo(self, cpo: str | int) -> list[str]:
        """Expected subcomponent references of a CPO component (ELSs + OEs).

        In the platform model OE/ELS are top-level components which the CPO
        references (gNMI: components/component[name=cpoN]/subcomponents/
        subcomponent[name=...] leafrefs; CLI: the `laser-sources` field and the
        `optical-engines` container) - they are not contained children.
        """
        return self.els_for_cpo(cpo) + self.oes_for_cpo(cpo)

    def cpo_for_els(self, els: str | int) -> str:
        idx = self._index(els, self.ELS_PREFIX, self.els_count)
        return f"{self.CPO_PREFIX}{(idx - 1) // self.els_per_cpo + 1}"

    def _index(self, name: str | int, prefix: str, count: int) -> int:
        """Parse a 1-based instance index out of a name/int and range-check it."""
        if isinstance(name, int):
            idx = name
        else:
            try:
                idx = int(str(name).lower().removeprefix(prefix))
            except ValueError:
                raise ValueError(f"invalid {prefix!r} identifier {name!r}") from None
        if not 1 <= idx <= count:
            raise ValueError(
                f"{prefix!r} index {idx} out of range 1..{count} (from {name!r})"
            )
        return idx

    # ------------------------------------------------------------- validation
    def assert_consistent(
        self,
        *,
        cpo_to_oes: dict[str, list[str]] | None = None,
        cpo_to_els: dict[str, list[str]] | None = None,
        cpo_to_channels: dict[str, list[str]] | None = None,
        cpo_to_ports: dict[str, list[str]] | None = None,
        port_to_cpo: dict[str, str] | None = None,
    ) -> ResultObj:
        """Structural self-consistency check for what a DUT actually reports.

        Each CPO's reported OEs / ELSs / channels must equal the topology's
        expected names for that CPO (oes_for_cpo / els_for_cpo /
        channels_for_cpo) - ownership swaps, duplicates and phantom names are
        all errors, not just wrong counts. Ports have no expected map (physical
        cabling is not modeled), so the port check verifies the two reported
        directions agree with each other instead.
        Every argument is optional - only the ones supplied are checked - except
        that the port cross-check needs cpo_to_ports AND port_to_cpo together.
        """
        if (cpo_to_ports is None) != (port_to_cpo is None):
            raise ValueError(
                "cpo_to_ports and port_to_cpo must be supplied together "
                "(the port check is a two-way cross-reference)"
            )
        errors: list[str] = []
        expected_cpos = set(self.cpo_names())

        if cpo_to_oes is not None:
            self._check_cpo_keys(cpo_to_oes, expected_cpos, "cpo_to_oes", errors)
            self._check_membership(cpo_to_oes, self.oes_for_cpo, "OE", errors)

        if cpo_to_els is not None:
            self._check_cpo_keys(cpo_to_els, expected_cpos, "cpo_to_els", errors)
            self._check_membership(cpo_to_els, self.els_for_cpo, "ELS", errors)

        if cpo_to_channels is not None:
            self._check_cpo_keys(
                cpo_to_channels, expected_cpos, "cpo_to_channels", errors
            )
            self._check_membership(
                cpo_to_channels, self.channels_for_cpo, "channel", errors
            )

        if cpo_to_ports is not None and port_to_cpo is not None:
            # unlike the maps above, port maps may cover a subset of CPOs (e.g.
            # built from a few interfaces), so keys are checked for membership
            # in the expected CPO set rather than for full equality
            for cpo in set(cpo_to_ports) | set(port_to_cpo.values()):
                if cpo not in expected_cpos:
                    errors.append(f"port maps reference unknown CPO {cpo!r}")
            for cpo, ports in cpo_to_ports.items():
                for port in ports:
                    if port_to_cpo.get(port) != cpo:
                        errors.append(
                            f"port {port} listed under {cpo} but port_to_cpo says {port_to_cpo.get(port)!r}"
                        )
            for port, cpo in port_to_cpo.items():
                if port not in cpo_to_ports.get(cpo, []):
                    errors.append(
                        f"port {port} maps to {cpo} but is not in its ports"
                    )

        if errors:
            return ResultObj(False, "CPO topology inconsistent:\n" + "\n".join(errors))
        return ResultObj(True, "CPO topology is self-consistent")

    @staticmethod
    def _check_cpo_keys(
        mapping: dict[str, list[str]],
        expected_cpos: set[str],
        label: str,
        errors: list[str],
    ) -> None:
        actual = set(mapping)
        if actual != expected_cpos:
            errors.append(
                f"{label}: expected CPOs {sorted(expected_cpos)}, got {sorted(actual)}"
            )

    def _check_membership(
        self,
        mapping: dict[str, list[str]],
        expected_for: Callable[[str], list[str]],
        item_label: str,
        errors: list[str],
    ) -> None:
        """Each known CPO's reported items must equal its expected name set.

        Unknown CPO keys are skipped here - _check_cpo_keys already reported
        them (and expected_for would raise on them). The extra len() comparison
        catches duplicates that set equality alone would hide.
        """
        expected_cpos = set(self.cpo_names())
        for cpo, items in mapping.items():
            if cpo not in expected_cpos:
                continue
            expected = expected_for(cpo)
            if len(items) != len(expected) or set(items) != set(expected):
                errors.append(
                    f"{cpo}: expected {item_label}s {expected}, got {list(items)}"
                )

    @property
    def lanes_per_laser(self) -> int:
        """Optical lanes (channels) fed by one ELS laser."""
        return self.channels_per_cpo // (self.els_per_cpo * self.lasers_per_els)


@runtime_checkable
class CpoCapable(Protocol):
    """Structural (duck-typed) marker for devices exposing a Gen2 CPO topology.

    A device satisfies this Protocol iff it has the attributes set by
    PortiaCpoCapability: the ``cpo`` topology plus the flat name projections.
    Being ``@runtime_checkable`` means it works with
    ``isinstance(device, CpoCapable)`` at runtime AND gives static type checkers
    a precise type to narrow to - a self-documenting replacement for
    ``hasattr(device, 'cpo')`` scattered through the code/tests.

    Note: a runtime ``isinstance`` check verifies the *presence* of these
    attributes, not their types (that is the static half).
    """

    cpo: CpoTopology
    cpo_list: list[str]
    laser_source_list: list[str]
    oe_list: list[str]


def is_cpo_capable(device: object) -> TypeGuard[CpoCapable]:
    """Return True if ``device`` exposes a Gen2 CPO topology (``device.cpo``).

    Declared as a TypeGuard, so inside an ``if is_cpo_capable(device):`` branch
    static type checkers treat ``device`` as CpoCapable (``device.cpo`` etc.
    resolve without casts).
    """
    return isinstance(device, CpoCapable)
