"""NOS-agnostic port exclusion / inclusion selection for performance tests.

This module centralizes the logic for running a performance test on a subset of the
switch. It supports two mutually-exclusive user inputs:

- ``exclude_ports`` — run on the whole switch **except** these ports.
- ``include_ports`` — run **only** on these ports (exclude everything else).

Both inputs are collapsed into a single internal notion of "selected-out" ports so the
rest of the codebase stays uniform. The class is intentionally free of any NOS-specific
switch calls: callers pass in the already-discovered DUT ``left``/``right`` lists and the
resolver returns the balanced per-side sets to remove.

Backward compatibility: when neither input is supplied the object is *inactive*
(``is_active()`` is ``False``) and every method returns its input unchanged, so existing
tests behave byte-for-byte as before.

See ``docs/PORT_EXCLUSION_INCLUSION_PLAN.md`` for the full design.
"""
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Set, Tuple

import yaml

from devts.infra.tools.exceptions.test_issue import TestIssue

logger = logging.getLogger()

SWP = "swp"
SDK_HEX = "sdk_hex"
ETHERNET = "ethernet"
SUPPORTED_PORT_STYLES = (SWP, SDK_HEX, ETHERNET)

EXCLUDE_KEY = "exclude_ports"
INCLUDE_KEY = "include_ports"

# Default YAML config path: ``ngts/performance_tests/port_selection_config.yaml``.
# This module lives at ``ngts/helpers/performance/`` so we climb two levels to ``ngts``.
_NGTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PORT_SELECTION_CONFIG = os.path.join(_NGTS_DIR, "performance_tests",
                                             "port_selection_config.yaml")

# Session-wide port-selection options captured from the pytest command line at
# ``pytest_configure`` time. Stored at module scope (not on a fixture) so they are available
# even when the config is applied by an earlier, higher-scoped fixture (e.g. a class-scoped
# ``basic_setup_configuration``) that would otherwise run before a function-scoped fixture.
_CLI_PORT_SELECTION_OPTIONS = {
    "setup_name": None,
    "exclude_enabled": False,
    "include_enabled": False,
    "config_path": None,
}


def set_cli_port_selection_options(setup_name=None, exclude_enabled=False,
                                   include_enabled=False, config_path=None):
    """Store the session port-selection options (called from ``pytest_configure``)."""
    _CLI_PORT_SELECTION_OPTIONS["setup_name"] = setup_name
    _CLI_PORT_SELECTION_OPTIONS["exclude_enabled"] = bool(exclude_enabled)
    _CLI_PORT_SELECTION_OPTIONS["include_enabled"] = bool(include_enabled)
    _CLI_PORT_SELECTION_OPTIONS["config_path"] = config_path


def get_cli_port_selection_options():
    """Return a copy of the stored session port-selection options."""
    return dict(_CLI_PORT_SELECTION_OPTIONS)


# Set True the first time an *active* selection is built, so a session-end check can warn if
# a mode was requested on the CLI but never actually took effect (e.g. the test path did not
# apply configuration, or the options were not plumbed).
_PORT_SELECTION_ACTIVATED = False


def mark_port_selection_activated():
    """Record that an active port selection was built at least once this session."""
    global _PORT_SELECTION_ACTIVATED
    _PORT_SELECTION_ACTIVATED = True


def port_selection_was_activated():
    """Return True if an active port selection was built at least once this session."""
    return _PORT_SELECTION_ACTIVATED


# The DUT's cascade-excluded port names, published once the DUT resolves
# get_right_left_ports_dict. Backed by BOTH a module global (fast, same-process) AND a file:
# the file survives the module-global reset that apply_test_configuration does at its start and
# the object-identity gap between the ``players`` dict and ``topology_obj``, so the TGs can read
# it reliably at traffic time (by name) to scope traffic and skip readiness waits — robust
# whether an excluded DUT port ends up link-up-but-not-routed or fully down.
_RESOLVED_EXCLUDED_DUT_PORTS = set()
_EXCLUDED_DUT_PORTS_FILE = os.path.join(tempfile.gettempdir(), "perf_excluded_dut_ports.json")
_PORT_SELECTION_DEBUG_FILE = os.path.join(tempfile.gettempdir(), "perf_port_selection_debug.json")


def _write_names_file(path, names):
    try:
        with open(path, "w") as f:
            json.dump(sorted(names), f)
    except OSError as e:
        logger.warning(f"Could not persist excluded-ports file {path}: {e}")


def _read_names_file(path):
    try:
        with open(path) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def set_resolved_excluded_dut_ports(names):
    """Publish the DUT's cascade-excluded port names (DUT only).

    Writes the file only for a non-empty set, so the apply_test_configuration reset
    (``set_resolved_excluded_dut_ports(set())``) clears the in-memory global for a fresh
    scenario without wiping the file that later phases (run_traffic) rely on.
    """
    global _RESOLVED_EXCLUDED_DUT_PORTS
    _RESOLVED_EXCLUDED_DUT_PORTS = set(names or [])
    if names:
        _write_names_file(_EXCLUDED_DUT_PORTS_FILE, names)


def get_resolved_excluded_dut_ports():
    """Return the DUT's cascade-excluded port names (global, falling back to the file)."""
    return set(_RESOLVED_EXCLUDED_DUT_PORTS) or _read_names_file(_EXCLUDED_DUT_PORTS_FILE)


def record_port_selection_debug(tag, payload):
    """Append a best-effort breadcrumb to a persistent debug file for post-mortem.

    Not cleared at teardown, so a completed run leaves a trail of exactly what port selection
    decided (mode, excluded names, per-TG mloop before/after, validator config). Never raises.
    """
    entry = {"tag": tag, "payload": payload}
    try:
        existing = []
        if os.path.exists(_PORT_SELECTION_DEBUG_FILE):
            with open(_PORT_SELECTION_DEBUG_FILE) as f:
                existing = json.load(f)
        existing.append(entry)
        with open(_PORT_SELECTION_DEBUG_FILE, "w") as f:
            json.dump(existing, f, indent=2, default=str)
    except (OSError, ValueError, TypeError) as e:
        logger.warning(f"Could not write port-selection debug breadcrumb: {e}")


def clear_resolved_excluded_ports():
    """Clear all published exclusion state (globals + files). Called at teardown/restore.

    The persistent debug breadcrumb file is intentionally left in place for post-mortem.
    """
    global _RESOLVED_EXCLUDED_DUT_PORTS
    _RESOLVED_EXCLUDED_DUT_PORTS = set()
    for path in (_EXCLUDED_DUT_PORTS_FILE,):
        try:
            os.remove(path)
        except OSError:
            pass


_SWP_SPLIT_RE = re.compile(r"^(swp\d+)s(\d+)$")
_SWP_SPLIT_NUM_RE = re.compile(r"^swp(\d+)s(\d+)$")
_SWP_PARENT_RE = re.compile(r"^swp(\d+)$")
_ETHERNET_RE = re.compile(r"^Ethernet(\d+)$")


def _normalize_tokens(ports):
    """Return a clean list of non-empty, stripped string tokens.

    Accepts ``None``, a comma/space separated string, or an iterable of names and always
    returns a list of individual port tokens.

    Args:
        ports: ``None``, a delimited string, or an iterable of port names.

    Returns:
        list[str]: Cleaned, order-preserving list of tokens (may be empty).
    """
    if ports is None:
        return []
    if isinstance(ports, str):
        raw = re.split(r"[,\s]+", ports)
    else:
        raw = []
        for item in ports:
            raw.extend(re.split(r"[,\s]+", str(item)))
    return [tok.strip() for tok in raw if tok and tok.strip()]


def _parent_name(port, port_style):
    """Return the parent identifier of ``port`` for the given ``port_style``.

    For ``swp`` a split child (``swp26s0``) collapses to its parent (``swp26``); all other
    styles have no naming-based parent so the port is its own parent.

    Args:
        port: Port identifier.
        port_style: One of :data:`SUPPORTED_PORT_STYLES`.

    Returns:
        str: The parent identifier.
    """
    name = str(port)
    if port_style == SWP:
        match = _SWP_SPLIT_RE.match(name)
        return match.group(1) if match else name
    return name


def _sort_key(port_style):
    """Return a sort-key callable that orders ports numerically for the given style."""

    def swp_key(port):
        name = str(port)
        match = _SWP_SPLIT_NUM_RE.match(name)
        if match:
            return (int(match.group(1)), int(match.group(2)))
        match = _SWP_PARENT_RE.match(name)
        if match:
            return (int(match.group(1)), -1)
        return (10 ** 9, 0)

    def ethernet_key(port):
        match = _ETHERNET_RE.match(str(port))
        return (int(match.group(1)),) if match else (10 ** 9,)

    def sdk_hex_key(port):
        try:
            return (int(str(port), 16),)
        except ValueError:
            try:
                return (int(str(port)),)
            except ValueError:
                return (10 ** 12,)

    return {SWP: swp_key, ETHERNET: ethernet_key, SDK_HEX: sdk_hex_key}[port_style]


@dataclass
class PortSelection:
    """Resolves user exclude/include input into "selected-out" decisions.

    Only one of ``include`` / ``exclude`` may be set. When neither is set the selection is
    inactive and behaves as a no-op (backward compatibility).

    Attributes:
        include: Ports to keep (everything else is excluded). String or iterable.
        exclude: Ports to drop. String or iterable.
        port_style: Identifier style for matching/sorting; one of
            :data:`SUPPORTED_PORT_STYLES` (default ``swp``).
    """

    include: Optional[object] = None
    exclude: Optional[object] = None
    port_style: str = SWP
    _include_set: Set[str] = field(default_factory=set, init=False, repr=False)
    _exclude_set: Set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self):
        if self.port_style not in SUPPORTED_PORT_STYLES:
            raise TestIssue(msg=f"Unsupported port_style {self.port_style!r}; "
                            f"expected one of {SUPPORTED_PORT_STYLES}")
        self._include_set = set(_normalize_tokens(self.include))
        self._exclude_set = set(_normalize_tokens(self.exclude))
        if self._include_set and self._exclude_set:
            raise TestIssue(msg="include_ports and exclude_ports are mutually exclusive; "
                                f"got include={sorted(self._include_set)} "
                                f"exclude={sorted(self._exclude_set)}")

    @property
    def mode(self):
        """Return ``'include'``, ``'exclude'`` or ``'inactive'``."""
        if self._include_set:
            return "include"
        if self._exclude_set:
            return "exclude"
        return "inactive"

    def is_active(self):
        """True when a non-empty include or exclude selection was supplied."""
        return self.mode != "inactive"

    def _matches(self, port, selector_set):
        """True if ``port`` matches ``selector_set`` by exact name or parent name."""
        name = str(port)
        return name in selector_set or _parent_name(name, self.port_style) in selector_set

    def is_selected_out(self, port):
        """Return True if ``port`` should be removed from the run.

        - Inactive selection: always ``False`` (backward compatible).
        - Exclude mode: ``True`` when the port matches the exclude set (exact or parent).
        - Include mode: ``True`` when the port does **not** match the include set.
        """
        if not self.is_active():
            return False
        if self.mode == "exclude":
            return self._matches(port, self._exclude_set)
        return not self._matches(port, self._include_set)

    def filter_selected_out(self, ports):
        """Return ``ports`` with selected-out entries removed (order preserved).

        When inactive, returns a list copy of the input unchanged.
        """
        return [p for p in ports if not self.is_selected_out(p)]

    def sorted_ports(self, ports):
        """Return ``ports`` sorted numerically according to ``port_style``."""
        return sorted(ports, key=_sort_key(self.port_style))


def resolve_symmetric_cascade(left_ports: Sequence, right_ports: Sequence,
                              selection: PortSelection) -> Tuple[Set[str], Set[str]]:
    """Resolve the balanced per-side sets of DUT ports to remove.

    Enforces the two balance invariants required by the spine performance topology:

    - **DUT left/right balance:** the returned left and right removal sets have equal size,
      so ``len(left_ports) == len(right_ports)`` still holds after removal.
    - **Symmetric counterpart:** a port removed on one side is mirrored to the other side by
      **sorted index** (the same positional pairing used by the mloop VLAN assignment).

    Reconciliation of the two user-approved policies:

    - **Single-sided selection auto-mirrors.** If the user selects out ports on only one
      side (the common ``--perf-exclude-ports swp26`` case), the same-index counterpart on
      the other side is added automatically.
    - **Two-sided explicit selection must be balanced.** If the user selects out ports on
      **both** sides, the per-side counts must already be equal, otherwise this raises
      ``TestIssue`` (fail fast — never auto-trim).
    - **Include mode must keep equal counts** per side, otherwise ``TestIssue`` (fail fast).

    Args:
        left_ports: DUT ports facing the left TG (any identifier style matching
            ``selection.port_style``).
        right_ports: DUT ports facing the right TG.
        selection: The resolved :class:`PortSelection`.

    Returns:
        tuple(set, set): ``(excluded_left, excluded_right)`` — the ports to remove on each
        side. Both empty when the selection is inactive.

    Raises:
        TestIssue: On any imbalance that cannot be resolved without auto-trimming, or when
            left/right base lists differ in length so a symmetric index has no counterpart.
    """
    if not selection.is_active():
        return set(), set()

    sorted_left = selection.sorted_ports(left_ports)
    sorted_right = selection.sorted_ports(right_ports)

    if len(sorted_left) != len(sorted_right):
        raise TestIssue(msg="Cannot apply symmetric port cascade: DUT left/right counts "
                            f"differ ({len(sorted_left)} left vs {len(sorted_right)} right). "
                            "Symmetric-index pairing requires equal base counts.")

    left_out = [p for p in sorted_left if selection.is_selected_out(p)]
    right_out = [p for p in sorted_right if selection.is_selected_out(p)]

    if selection.mode == "include":
        kept_left = len(sorted_left) - len(left_out)
        kept_right = len(sorted_right) - len(right_out)
        if kept_left != kept_right:
            raise TestIssue(msg="include_ports is unbalanced: it would keep "
                                f"{kept_left} left port(s) but {kept_right} right port(s). "
                                "Provide an include list that keeps equal counts per side "
                                "(no auto-trim is performed).")
        excluded_left, excluded_right = set(left_out), set(right_out)
    else:
        excluded_left, excluded_right = _resolve_exclude_cascade(sorted_left, sorted_right,
                                                                 left_out, right_out)

    logger.info(f"Port selection ({selection.mode}) resolved cascade: "
                f"left={sorted(excluded_left)} right={sorted(excluded_right)}")
    return excluded_left, excluded_right


def _resolve_exclude_cascade(sorted_left: List, sorted_right: List,
                             left_out: List, right_out: List) -> Tuple[Set[str], Set[str]]:
    """Apply the exclude-mode cascade rules (single-sided mirror / two-sided balance)."""
    if left_out and right_out:
        if len(left_out) != len(right_out):
            raise TestIssue(msg="exclude_ports selects an unbalanced set: "
                                f"{len(left_out)} left port(s) ({left_out}) vs "
                                f"{len(right_out)} right port(s) ({right_out}). "
                                "When excluding ports on both sides the counts must match "
                                "(no auto-trim is performed).")
        return set(left_out), set(right_out)

    if left_out:
        indices = [sorted_left.index(p) for p in left_out]
        return set(left_out), {sorted_right[i] for i in indices}

    if right_out:
        indices = [sorted_right.index(p) for p in right_out]
        return {sorted_left[i] for i in indices}, set(right_out)

    return set(), set()


def _load_ports_from_config(config_path: str, setup_name: str, scenario: str, mode: str) -> List[str]:
    """Read the port list for ``mode`` from the YAML port-selection config file.

    The file is keyed by ``setup_name`` then ``scenario``; each scenario block holds
    ``exclude_ports`` and ``include_ports`` lists, for example::

        nv_performance_slm-254:
          spcx_ra:
            exclude_ports: [swp26]
            include_ports: []

    Args:
        config_path: Path to the YAML file.
        setup_name: Setup key (from ``--setup_name``).
        scenario: Scenario key (e.g. ``spcx_ra``).
        mode: ``'exclude'`` or ``'include'`` — selects which list to read.

    Returns:
        list[str]: The configured ports (non-empty).

    Raises:
        TestIssue: If the file is missing/unreadable, the setup/scenario block is absent, or
            the selected list is empty (the mode was enabled but nothing was configured).
    """
    if not os.path.exists(config_path):
        raise TestIssue(msg=f"Port-selection is enabled but the config file was not found at "
                        f"{config_path!r}. Create it or pass --perf-ports-config=<path>.")
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise TestIssue(msg=f"Failed to parse port-selection config {config_path!r}: {e}") from e

    setup_cfg = data.get(setup_name)
    if not isinstance(setup_cfg, dict):
        raise TestIssue(msg=f"Port-selection config {config_path!r} has no entry for setup "
                        f"{setup_name!r}. Available setups: {sorted(data.keys())}.")
    scenario_cfg = setup_cfg.get(scenario)
    if not isinstance(scenario_cfg, dict):
        raise TestIssue(msg=f"Port-selection config {config_path!r} has no entry for scenario "
                        f"{scenario!r} under setup {setup_name!r}. "
                        f"Available scenarios: {sorted(setup_cfg.keys())}.")

    key = EXCLUDE_KEY if mode == "exclude" else INCLUDE_KEY
    ports = _normalize_tokens(scenario_cfg.get(key))
    if not ports:
        raise TestIssue(msg=f"Port {mode} was enabled but {key!r} is empty/missing for "
                        f"{setup_name!r}/{scenario!r} in {config_path!r}.")
    return ports


def build_port_selection(setup_name: str, scenario: str, exclude_enabled: bool,
                         include_enabled: bool, config_path: Optional[str] = None,
                         port_style: str = SWP) -> PortSelection:
    """Build a :class:`PortSelection` from the enable flags and the YAML config file.

    Behavior:

    - Neither flag set → **inactive** selection (backward compatible no-op); the config file
      is not even read.
    - Both flags set → ``TestIssue`` (mutually exclusive).
    - One flag set → read the matching list (``exclude_ports`` / ``include_ports``) for
      ``setup_name``/``scenario`` from the config file and build the selection.

    Args:
        setup_name: Value of ``--setup_name``.
        scenario: Scenario name (e.g. ``spcx_ra``).
        exclude_enabled: Value of ``--perf-exclude-ports``.
        include_enabled: Value of ``--perf-include-ports``.
        config_path: Optional override; defaults to
            :data:`DEFAULT_PORT_SELECTION_CONFIG`.
        port_style: Identifier style for this NOS (``swp`` / ``sdk_hex`` / ``ethernet``).

    Returns:
        PortSelection: Active selection when a flag is set, otherwise inactive.

    Raises:
        TestIssue: On mutual-exclusion violation or any config-loading error.
    """
    if exclude_enabled and include_enabled:
        raise TestIssue(msg="--perf-exclude-ports and --perf-include-ports are mutually "
                            "exclusive; enable only one.")
    if not (exclude_enabled or include_enabled):
        return PortSelection(port_style=port_style)

    path = config_path or DEFAULT_PORT_SELECTION_CONFIG
    mode = "exclude" if exclude_enabled else "include"
    ports = _load_ports_from_config(path, setup_name, scenario, mode)
    logger.info(f"Port-selection ({mode}) for {setup_name}/{scenario} from {path}: {ports}")
    mark_port_selection_activated()
    if mode == "exclude":
        return PortSelection(exclude=ports, port_style=port_style)
    return PortSelection(include=ports, port_style=port_style)
