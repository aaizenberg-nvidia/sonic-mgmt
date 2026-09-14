"""Helpers for the NVOS OTEL telemetry test suite.

The module is split into two helper types, separated by the banners below:

1. **Suite plumbing** — bring-up, DUT telemetry configuration, collector artifact
   parsing, and metric-name comparison. Test files call these directly. For telemetry
   master enable, always use :func:`stage_telemetry_master_state` and
   :func:`apply_telemetry_configuration` with ``is_nvos`` (never raw
   ``telemetry.set(state)`` / ``export.otlp.set`` in new code).
2. **Expected-metrics derivation** — fetch ``metrics-classes.yaml`` from the DUT
   and gate the expected metric-name set by DUT capabilities. Only
   :func:`expected_metric_names_from_dut` is consumed by tests; everything else
   in that section is internal.
"""

import json
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

import pytest
import yaml

import ngts.tools.test_utils.allure_utils as allure
from devts.infra.tools.linux_tools.linux_tools import scp_file

from ngts.nvos_constants.constants_nvos import TelemetryConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.telemetry.otel.constants import OtelCapability, OtelCollectorConst, OtelCollectorLabel
from ngts.tests_nvos.system.telemetry.otel.otel_collector import OtelCollector
from ngts.tests_nvos.system.telemetry.otel.metrics_parser import MetricTimestamps, metric_timestamps_from_docs

logger = logging.getLogger(__name__)

_ENABLED = TelemetryConsts.State.ENABLED.value
_DISABLED = TelemetryConsts.State.DISABLED.value


# =============================================================================
# Suite plumbing — bring-up, DUT telemetry configuration, collector artifact
# parsing, and metric-name comparison. Test files call these directly.
# =============================================================================


@dataclass(frozen=True)
class OtelSuiteContext:
    """Returned by :func:`setup_otel_suite` and yielded by the suite fixture.

    ``is_nvos``, ``export_vrf`` and ``telemetry_unit`` carry the platform choices so
    tests and helpers read them from the suite instead of branching on platform.
    """

    primary: OtelCollector
    primary_ip: str
    secondary: Optional[OtelCollector] = None
    secondary_ip: Optional[str] = None
    is_nvos: bool = False
    export_vrf: str = TelemetryConsts.Defaults.EXPORT_VRF
    telemetry_unit: Optional[str] = None

    @property
    def collector_ips(self) -> Tuple[str, ...]:
        """OTLP destination IPs configured on the DUT for this suite.

        Single-collector suites yield ``(primary_ip,)``; pass this instead of a
        literal ``(primary_ip, secondary_ip)`` tuple so call sites never stage a
        ``None`` destination on a setup with no second collector host.
        """
        if self.secondary_ip is None:
            return (self.primary_ip,)
        return (self.primary_ip, self.secondary_ip)


@dataclass(frozen=True)
class EnabledClasses:
    """Per-family enabled telemetry classes for a single export scope.

    ``families`` maps a telemetry family (``interface-stats`` etc.) to the set of
    enabled class tokens. A class token is the base sentinel
    :data:`OtelCollectorConst.TELEMETRY_BASE_CLASS` (the always-on non-phy /
    non-categorized metrics), :data:`TelemetryConsts.PHY`, or a platform-stats
    class name (``cpu``, ``transceiver-info``, …).
    """

    families: Dict[str, frozenset]


@dataclass(frozen=True)
class TelemetryEnablement:
    """Test-declared model of what telemetry was enabled, per export scope.

    * ``root`` — classes enabled at the telemetry root.
    * ``stats_groups`` — classes enabled under each ``stats-group <id>``.
    * ``destination_binding`` — OTLP destination IP -> bound ``stats-group`` id, or
      ``None`` when the destination exports the root config.

    The expected metric set for a destination is derived from
    :meth:`effective_for_destination` (one stats-group per destination; a bound
    stats-group overrides root, and an absent/empty binding falls back to root).
    """

    root: EnabledClasses
    stats_groups: Dict[str, EnabledClasses]
    destination_binding: Dict[str, Optional[str]]

    def effective_for_destination(self, destination_ip: str) -> EnabledClasses:
        stats_group_id = self.destination_binding.get(destination_ip)
        if stats_group_id:
            scope = self.stats_groups.get(stats_group_id)
            if scope is not None and scope.families:
                return scope
        return self.root


def _enabled_classes_for_root_families(families: Iterable[str]) -> EnabledClasses:
    """Build :class:`EnabledClasses` for the NVOS root enable (see ``_enable_family_classes``)."""
    base = OtelCollectorConst.TELEMETRY_BASE_CLASS
    phy = TelemetryConsts.PHY
    out: Dict[str, frozenset] = {}
    for family in families:
        if family in (TelemetryConsts.INTERFACE_STATS, TelemetryConsts.PEER_PORT_STATS):
            out[family] = frozenset({base, phy})
        elif family == TelemetryConsts.PLATFORM_STATS:
            out[family] = frozenset(TelemetryConsts.PLATFORM_CLASSES)
        elif family == TelemetryConsts.IB_ROUTER_STATS:
            out[family] = frozenset({base})
    return EnabledClasses(families=out)


def _enabled_classes_for_nvos_stats_group() -> EnabledClasses:
    """Classes enabled by ``_stage_nvos_test01_stats_group`` (interface + platform, phy on).

    Platform sub-classes are default-on: enabling ``platform-stats`` exports every
    platform sub-class regardless of which subset the stats-group explicitly turns
    on. The test only enables a subset (``TEST01_STATS_GROUP_PLATFORM_CLASSES``) but
    the DUT still exports the rest (disk/memory/health-info/…), so the expected set
    must mirror the full default-on class set. A class is only excluded when it is
    explicitly disabled (no test does so today).
    """
    base = OtelCollectorConst.TELEMETRY_BASE_CLASS
    phy = TelemetryConsts.PHY
    return EnabledClasses(
        families={
            TelemetryConsts.INTERFACE_STATS: frozenset({base, phy}),
            TelemetryConsts.PLATFORM_STATS: frozenset(TelemetryConsts.PLATFORM_CLASSES),
        }
    )


def build_nvos_enablement(
    families: Iterable[str],
    *,
    collector_ips: Optional[Iterable[str]] = None,
    stats_group_id: Optional[str] = None,
    use_stats_group: bool = False,
) -> TelemetryEnablement:
    """Construct the test-declared :class:`TelemetryEnablement` for an NVOS config.

    Mirrors what :func:`enable_nvos_telemetry_families` /
    :func:`enable_nvos_families_export` apply on the DUT, so the strict-compare
    expected set is derived from exactly what the test enabled.
    """
    enabled = set(families)
    root = _enabled_classes_for_root_families(enabled)
    ips = tuple(collector_ips or ())
    stats_groups: Dict[str, EnabledClasses] = {}
    if use_stats_group and ips:
        from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst

        sg_id = stats_group_id or CumulusOtelConst.TEST01_STATS_GROUP_ID
        stats_groups[sg_id] = _enabled_classes_for_nvos_stats_group()
        destination_binding: Dict[str, Optional[str]] = {ip: sg_id for ip in ips}
    else:
        destination_binding = {ip: None for ip in ips}
    return TelemetryEnablement(
        root=root,
        stats_groups=stats_groups,
        destination_binding=destination_binding,
    )


def is_nvos_dut(devices) -> bool:
    """NVOS vs Cumulus/SONiC. Cumulus/SONiC report an eth switch type."""
    return not devices.dut.is_eth()


def nvtelemetry_unit(is_nvos: bool, vrf: str) -> str:
    """NVOS has a single ``nv-telemetry.service``; Cumulus uses a per-VRF instance."""
    return "nv-telemetry.service" if is_nvos else f"nv-telemetry@{vrf}"


def _cumulus_version_tuple(dut) -> Tuple[int, int]:
    """(major, minor) Cumulus release from ``nv show system version``; ``(0, 0)`` on NVOS/unknown."""
    try:
        parsed = System().version.parse_show(dut_engine=dut)
    except Exception as exc:  # noqa: BLE001
        logger.warning("nv show system version parse failed (%s); assuming state schema", exc)
        return (0, 0)
    if not isinstance(parsed, dict):
        return (0, 0)
    release = parsed.get("product-release") or parsed.get("version") or ""
    match = re.match(r"(\d+)\.(\d+)", str(release))
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def cumulus_telemetry_uses_enable_schema(dut) -> bool:
    """True when the DUT toggles the telemetry master feature via ``enable on/off``.

    Pre-5.15 Cumulus uses ``nv set system telemetry enable on|off``; 5.15+ switched to
    ``nv set system telemetry state enabled|disabled`` (this is the only telemetry knob
    that changed schema across 5.15 — every subtree ``export … state`` uses
    ``state enabled/disabled`` on both, per SSIM ``OM_CHANGED_BEFORE_5_15``).

    Applies only to Cumulus (``is_eth_dut``): NVOS always uses the ``state`` schema.
    NVOS / DUTs with no parsed version also report ``(0, 0)`` and fall back to the
    ``state`` schema.
    """
    version = _cumulus_version_tuple(dut)
    if version == (0, 0):
        return False
    return version < (5, 15)


def stage_telemetry_master_state(
    system: System,
    dut,
    *,
    is_nvos: bool,
) -> None:
    """Stage the platform-specific telemetry master enable (no ``nv config apply``).

    Call before other ``nv set system telemetry …`` knobs in a multi-set block.

    * NVOS: ``nv set system telemetry export otlp state enabled``
    * Cumulus 5.15+: ``nv set system telemetry state enabled``
    * Cumulus <5.15: ``nv set system telemetry enable on`` (pre-5.15 schema)
    """
    if is_nvos:
        system.telemetry.export.otlp.set(
            TelemetryConsts.STATE, _ENABLED, dut_engine=dut
        ).verify_result()
    else:
        key, value = (
            (TelemetryConsts.ENABLE, TelemetryConsts.ENABLE_ON)
            if cumulus_telemetry_uses_enable_schema(dut)
            else (TelemetryConsts.STATE, _ENABLED)
        )
        system.telemetry.set(key, value, dut_engine=dut).verify_result()


def apply_telemetry_configuration(
    system: System,
    dut,
    *,
    is_nvos: bool,
    ask_for_confirmation: bool = True,
) -> None:
    """Commit staged telemetry NVUE with OTLP export enabled.

    Always runs ``nv set system telemetry export otlp state enabled`` with
    ``nv config apply``. On Cumulus, :func:`stage_telemetry_master_state` must
    already have staged ``system telemetry state enabled`` in the same candidate.
    """
    system.telemetry.export.otlp.set(
        TelemetryConsts.STATE,
        _ENABLED,
        apply=True,
        ask_for_confirmation=ask_for_confirmation,
        dut_engine=dut,
    ).verify_result()


def enable_telemetry_master_state(
    system: System,
    dut,
    *,
    is_nvos: bool,
    apply: bool = False,
    ask_for_confirmation: bool = False,
) -> None:
    """Convenience wrapper around :func:`stage_telemetry_master_state` / :func:`apply_telemetry_configuration`."""
    if apply:
        apply_telemetry_configuration(
            system,
            dut,
            is_nvos=is_nvos,
            ask_for_confirmation=ask_for_confirmation,
        )
    else:
        stage_telemetry_master_state(system, dut, is_nvos=is_nvos)


# --- NVUE typed-API plumbing -------------------------------------------------


def _telemetry_stats_parent(system: System, stats_group_id: Optional[str] = None):
    """Telemetry root or a single ``stats-group/<id>`` instance (created on demand)."""
    if not stats_group_id:
        return system.telemetry
    if stats_group_id not in system.telemetry.stats_group.resources_dict:
        system.telemetry.stats_group.set_resource(stats_group_id).verify_result()
    return system.telemetry.stats_group.resources_dict[stats_group_id]


def _stats_node(system: System, group_name: str, *, stats_group_id: Optional[str] = None):
    """Map a stats subtree name to the typed node under telemetry root or a stats-group."""
    parent = _telemetry_stats_parent(system, stats_group_id)
    return {
        TelemetryConsts.INTERFACE_STATS: parent.interface_stats,
        TelemetryConsts.PEER_PORT_STATS: parent.peer_port_stats,
        TelemetryConsts.IB_ROUTER_STATS: parent.ib_router_stats,
        TelemetryConsts.PLATFORM_STATS: parent.platform_stats,
    }[group_name]


def _set_stats_sample_interval(
    system: System,
    dut,
    group: str,
    seconds: int,
    *,
    stats_group_id: Optional[str] = None,
) -> None:
    """`platform-stats` exposes `sample-interval` under `export`; other groups have it at root."""
    node = _stats_node(system, group, stats_group_id=stats_group_id)
    target = node.export if group == TelemetryConsts.PLATFORM_STATS else node
    target.set(TelemetryConsts.SAMPLE_INTERVAL, seconds, dut_engine=dut).verify_result()


def _apply_otlp_grpc_base(
    system: System,
    dut,
    collector_ip: str,
    port: int,
    *,
    insecure: bool = True,
) -> None:
    """Enable OTLP, configure the first gRPC destination + port + security mode. Does NOT apply.

    ``insecure=True`` (default) writes the ``insecure`` flag, matching today's OTel test posture.
    ``insecure=False`` configures TLS: destination ``certificate``, gRPC ``insecure`` disabled,
    and optional gRPC-level ``certificate`` (default VRF secured topology).
    """
    otlp = system.telemetry.export.otlp
    grpc = otlp.grpc
    otlp.set(TelemetryConsts.STATE, _ENABLED, dut_engine=dut).verify_result()
    grpc.destination.set_resource(collector_ip).verify_result()
    grpc.destination.resources_dict[collector_ip].set(
        TelemetryConsts.PORT, port, dut_engine=dut).verify_result()
    grpc.set(TelemetryConsts.PORT, port, dut_engine=dut).verify_result()
    if insecure:
        grpc.set(TelemetryConsts.INSECURE, _ENABLED, dut_engine=dut).verify_result()
    else:
        cert_name = OtelCollectorConst.OTEL_TLS_CA_NAME
        grpc.destination.resources_dict[collector_ip].set(
            TelemetryConsts.CERTIFICATE, cert_name, dut_engine=dut
        ).verify_result()
        grpc.set(
            TelemetryConsts.INSECURE,
            TelemetryConsts.Defaults.GRPC_INSECURE,
            dut_engine=dut,
        ).verify_result()


def _apply_otlp_grpc_secure_default_vrf(
    system: System,
    dut,
    collector_ip: str,
    port: int,
) -> None:
    """TLS OTLP for default VRF (grpc-level + destination certificate)."""
    _apply_otlp_grpc_base(system, dut, collector_ip, port, insecure=False)
    system.telemetry.export.otlp.grpc.set(
        TelemetryConsts.CERTIFICATE,
        OtelCollectorConst.OTEL_TLS_CA_NAME,
        dut_engine=dut,
    ).verify_result()


# --- DUT telemetry configuration ---------------------------------------------


def configure_switch_otlp_grpc_dual_destination(
    dut,
    primary_ip: str,
    secondary_ip: Optional[str] = None,
    port: int = OtelCollectorConst.OTLP_GRPC_PORT,
    interface_sample_interval_sec: int = OtelCollectorConst.INTERFACE_STATS_SAMPLE_INTERVAL_SEC,
    insecure: bool = True,
    export_vrf: Optional[str] = None,
    *,
    is_nvos: bool = False,
) -> None:
    """Enable OTLP, set the interface-stats sample interval, and add the OTLP destination(s).

    Configures the primary destination and, when ``secondary_ip`` is given, a
    secondary one too (``secondary_ip=None`` -> single-destination bring-up).

    ``export_vrf`` sets ``system telemetry export vrf`` (e.g. ``mgmt`` on Cumulus when collectors
    are reached via the management network). When omitted the DUT keeps its current export VRF
    (factory default is often ``default``, which may not reach sonic-mgmt).
    """
    system = System()
    grpc = system.telemetry.export.otlp.grpc
    iface = system.telemetry.interface_stats

    destinations = f"{primary_ip}, {secondary_ip}:{port}" if secondary_ip else f"{primary_ip}:{port}"
    step_label = f"Configure OTLP destination(s) {destinations}"
    if export_vrf:
        step_label += f", export vrf={export_vrf}"
    with allure.step(step_label):
        if export_vrf:
            system.telemetry.export.set(
                TelemetryConsts.VRF, export_vrf, dut_engine=dut
            ).verify_result()
        if not is_nvos:
            stage_telemetry_master_state(system, dut, is_nvos=False)
        _apply_otlp_grpc_base(system, dut, primary_ip, port, insecure=insecure)

        if secondary_ip:
            grpc.destination.set_resource(secondary_ip).verify_result()
            grpc.destination.resources_dict[secondary_ip].set(
                TelemetryConsts.PORT, port, dut_engine=dut).verify_result()

        iface.export.set(TelemetryConsts.STATE, _ENABLED, dut_engine=dut).verify_result()
        _set_stats_sample_interval(system, dut, TelemetryConsts.INTERFACE_STATS, interface_sample_interval_sec)

        apply_telemetry_configuration(system, dut, is_nvos=is_nvos)


def configure_switch_otlp_grpc_secured_destination(
    dut,
    collector_ip: str,
    port: int = OtelCollectorConst.OTLP_GRPC_PORT,
    *,
    export_vrf: Optional[str] = None,
    default_vrf_grpc_certificate: bool = True,
    is_nvos: bool = False,
) -> None:
    """Enable TLS OTLP gRPC export to a single collector destination."""
    system = System()
    step_label = f"Configure OTLP secured destination {collector_ip}:{port}"
    if export_vrf:
        step_label += f", export vrf={export_vrf}"
    with allure.step(step_label):
        if export_vrf:
            system.telemetry.export.set(
                TelemetryConsts.VRF, export_vrf, dut_engine=dut
            ).verify_result()
        if not is_nvos:
            stage_telemetry_master_state(system, dut, is_nvos=False)
        if default_vrf_grpc_certificate:
            _apply_otlp_grpc_secure_default_vrf(system, dut, collector_ip, port)
        else:
            _apply_otlp_grpc_base(system, dut, collector_ip, port, insecure=False)
        apply_telemetry_configuration(system, dut, is_nvos=is_nvos)


def cleanup_switch_otlp_grpc(dut) -> None:
    """Reset the telemetry subtree to factory defaults (single blanket ``nv unset``)."""
    system = System()
    with allure.step("Reset DUT telemetry subtree to factory defaults"):
        system.telemetry.unset(
            apply=True, ask_for_confirmation=True, dut_engine=dut).verify_result()


_DEFAULT_SAMPLE_INTERVALS: Dict[str, int] = {
    TelemetryConsts.INTERFACE_STATS: OtelCollectorConst.INTERFACE_STATS_SAMPLE_INTERVAL_SEC,
    TelemetryConsts.PEER_PORT_STATS: OtelCollectorConst.PEER_PORT_STATS_SAMPLE_INTERVAL_SEC,
    TelemetryConsts.IB_ROUTER_STATS: OtelCollectorConst.IB_ROUTER_STATS_SAMPLE_INTERVAL_SEC,
    TelemetryConsts.PLATFORM_STATS: OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC,
}


def _enable_family_classes(
    system: System, dut, family: str, *, stats_group_id: Optional[str] = None
) -> None:
    """Enable per-class metrics under a telemetry family (NVOS platform class list)."""
    parent = _telemetry_stats_parent(system, stats_group_id)
    if family == TelemetryConsts.INTERFACE_STATS:
        parent.interface_stats.cls.phy.set(
            TelemetryConsts.STATE, _ENABLED, dut_engine=dut
        ).verify_result()
    elif family == TelemetryConsts.PEER_PORT_STATS:
        parent.peer_port_stats.cls.phy.set(
            TelemetryConsts.STATE, _ENABLED, dut_engine=dut
        ).verify_result()
    elif family == TelemetryConsts.PLATFORM_STATS:
        for cat in TelemetryConsts.PLATFORM_CLASSES:
            categories = parent.platform_stats.cls.categories
            if cat in categories:
                result = categories[cat].set(
                    TelemetryConsts.STATE, _ENABLED, dut_engine=dut
                )
            else:
                result = BaseComponent(parent.platform_stats.cls, path="/" + cat).set(
                    TelemetryConsts.STATE, _ENABLED, dut_engine=dut
                )
            if result.result:
                result.verify_result()
            else:
                result.ignore_result()
                logger.warning(
                    "Skipping platform-stats class %s (NVUE rejected set)",
                    cat,
                )


def _configure_stats_families(
    system: System,
    dut,
    enabled: Set[str],
    intervals: Dict[str, int],
    *,
    stats_group_id: Optional[str] = None,
    family_universe: Optional[Iterable[str]] = None,
) -> None:
    """Enable/disable export + sample intervals (+ class knobs) at telemetry root."""
    families = family_universe or TelemetryConsts.ALL_STATS_SUBTREES
    for family in families:
        state = _ENABLED if family in enabled else _DISABLED
        _stats_node(system, family, stats_group_id=stats_group_id).export.set(
            TelemetryConsts.STATE, state, dut_engine=dut
        ).verify_result()
        if family in enabled:
            _set_stats_sample_interval(
                system, dut, family, intervals[family], stats_group_id=stats_group_id
            )
            _enable_family_classes(system, dut, family, stats_group_id=stats_group_id)


def _stage_nvos_test01_stats_group(
    system: System,
    dut,
    collector_ips: Iterable[str],
    *,
    stats_group_id: str,
) -> None:
    """Bind ``stats_group_id`` on OTLP destinations and stage test01 stats-group knobs.

    Mirrors Cumulus test01: interface-stats + platform-stats under ``stats-group sg_01``.
    Call before :func:`apply_telemetry_configuration` on the same ``System`` instance.
    """
    from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
    from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import (
        _bind_stats_group_on_otlp_destinations,
        configure_stats_group_interface_platform,
    )

    sg_intervals = {
        **_DEFAULT_SAMPLE_INTERVALS,
        **CumulusOtelConst.TEST01_STATS_GROUP_SAMPLE_INTERVALS,
    }
    with allure.step(
        f"Stage NVOS stats-group {stats_group_id} (interface-stats + platform-stats, "
        f"destinations={list(collector_ips)})"
    ):
        _bind_stats_group_on_otlp_destinations(
            system, dut, collector_ips, stats_group_id
        )
        configure_stats_group_interface_platform(
            system, dut, stats_group_id, sg_intervals,
            enable_phy_under_stats_group=True,
        )


def enable_nvos_telemetry_families(
    dut,
    collector_ip: str,
    families: Iterable[str],
    *,
    port: int = OtelCollectorConst.OTLP_GRPC_PORT,
    sample_intervals: Optional[Dict[str, int]] = None,
    insecure: bool = True,
    export_vrf: Optional[str] = None,
    collector_ips: Optional[Iterable[str]] = None,
    stats_group_id: Optional[str] = None,
    use_stats_group: bool = True,
) -> TelemetryEnablement:
    """Enable OTLP + the given telemetry families on the DUT (others explicitly disabled).

    ``families`` is any subset of :data:`TelemetryConsts.ALL_STATS_SUBTREES` —
    pass one family for "X only" tests, all four for full coverage, or any combo
    in between. For each enabled family the per-class metrics are turned on too.

    ``sample_intervals`` is an optional ``{family: seconds}`` overrides map; any
    family not present falls back to the matching
    ``OtelCollectorConst.<FAMILY>_SAMPLE_INTERVAL_SEC`` default.

    ``insecure`` is forwarded to :func:`_apply_otlp_grpc_base`; leave at the
    default for today's tests, set ``False`` once cert/mTLS support lands.

    ``export_vrf`` sets ``system telemetry export vrf`` (e.g. ``mgmt`` for legacy
    test_01 parity). When omitted the DUT keeps its current export VRF.

    ``collector_ips`` lists the OTLP gRPC destinations to configure (defaults to
    ``(collector_ip,)``). Pass both primary and secondary IPs from
    :class:`OtelSuiteContext` for dual-dest insecure suites. When ``use_stats_group``
    is True (default) ``stats-group sg_01`` is also bound on each destination and the
    test01 stats-group export tree is staged; set ``use_stats_group=False`` to export
    via root telemetry only (no stats-group).

    Cumulus lab test01 uses :mod:`ngts.tests_nvos.system.telemetry.otel.cumulus.helpers`.
    """
    enabled = set(families)
    unknown = enabled - set(TelemetryConsts.ALL_STATS_SUBTREES)
    assert not unknown, f"unknown telemetry families: {sorted(unknown)}"
    intervals = {**_DEFAULT_SAMPLE_INTERVALS, **(sample_intervals or {})}

    system = System()
    families_label = ", ".join(sorted(enabled)) or "(none)"
    step_parts = [f"Enable OTLP + telemetry families: {families_label}"]
    if export_vrf:
        step_parts.append(f"export vrf={export_vrf}")
    with allure.step(", ".join(step_parts)):
        if export_vrf:
            system.telemetry.export.set(
                TelemetryConsts.VRF, export_vrf, dut_engine=dut
            ).verify_result()
        _apply_otlp_grpc_base(
            system,
            dut,
            collector_ip,
            port,
            insecure=insecure,
        )
        otlp_dest_ips = tuple(
            collector_ips if collector_ips is not None else (collector_ip,)
        )
        grpc = system.telemetry.export.otlp.grpc
        for extra_ip in otlp_dest_ips[1:]:
            grpc.destination.set_resource(extra_ip).verify_result()
            grpc.destination.resources_dict[extra_ip].set(
                TelemetryConsts.PORT, port, dut_engine=dut
            ).verify_result()
        _configure_stats_families(
            system,
            dut,
            enabled,
            intervals,
            stats_group_id=None,
            family_universe=TelemetryConsts.ALL_STATS_SUBTREES,
        )
        sg_id_used: Optional[str] = None
        if use_stats_group:
            from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst

            sg_id_used = stats_group_id or CumulusOtelConst.TEST01_STATS_GROUP_ID
            _stage_nvos_test01_stats_group(
                system, dut, otlp_dest_ips, stats_group_id=sg_id_used
            )
        apply_telemetry_configuration(system, dut, is_nvos=True)

    return build_nvos_enablement(
        enabled,
        collector_ips=otlp_dest_ips,
        stats_group_id=sg_id_used,
        use_stats_group=use_stats_group,
    )


def enable_nvos_families_export(
    dut,
    families: Iterable[str],
    *,
    sample_intervals: Optional[Dict[str, int]] = None,
    collector_ips: Optional[Iterable[str]] = None,
    stats_group_id: Optional[str] = None,
) -> TelemetryEnablement:
    """Enable export + per-class knobs for ``families`` on an already-configured OTLP base.

    Unlike :func:`enable_nvos_telemetry_families` this does NOT (re)apply the OTLP
    gRPC destination/security base, so it is safe to layer on top of a secured
    (TLS) destination that a suite already configured. By default only **root**
    family subtrees are staged (Cumulus/NVOS secured test01 parity).

    Pass ``collector_ips`` to also bind ``stats-group sg_01`` and stage interface +
    platform under the stats-group (insecure test01 / dual-dest flows only — not
    used on secured test01).
    """
    enabled = set(families)
    unknown = enabled - set(TelemetryConsts.ALL_STATS_SUBTREES)
    assert not unknown, f"unknown telemetry families: {sorted(unknown)}"
    intervals = {**_DEFAULT_SAMPLE_INTERVALS, **(sample_intervals or {})}

    system = System()
    families_label = ", ".join(sorted(enabled)) or "(none)"
    with allure.step(f"Enable NVOS telemetry families (no base): {families_label}"):
        _configure_stats_families(
            system,
            dut,
            enabled,
            intervals,
            stats_group_id=None,
            family_universe=TelemetryConsts.ALL_STATS_SUBTREES,
        )
        sg_id_used: Optional[str] = None
        if collector_ips:
            from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst

            sg_id_used = stats_group_id or CumulusOtelConst.TEST01_STATS_GROUP_ID
            _stage_nvos_test01_stats_group(
                system, dut, collector_ips, stats_group_id=sg_id_used
            )
        apply_telemetry_configuration(system, dut, is_nvos=True)

    return build_nvos_enablement(
        enabled,
        collector_ips=collector_ips,
        stats_group_id=sg_id_used,
        use_stats_group=bool(collector_ips),
    )


# --- Suite orchestration -----------------------------------------------------


def _probe_dut_otlp_export(dut, *, export_vrf: str, is_nvos: bool, collector_ip: str, tag: str) -> str:
    """Dump DUT telemetry/export health while waiting for collector artifacts."""
    unit = shlex.quote(nvtelemetry_unit(is_nvos, export_vrf))
    reach = "'echo >/dev/tcp/%s/%d'" % (collector_ip, OtelCollectorConst.OTLP_GRPC_PORT)
    probes = {
        'telemetry_export': 'nv show system telemetry export 2>&1',
        'nvtelemetry': 'systemctl status %s --no-pager -n 25 2>&1' % (unit,),
        'reach_default': 'timeout 3 bash -lc %s 2>&1 || true' % (reach,),
        'reach_vrf': 'timeout 3 ip vrf exec %s bash -lc %s 2>&1 || true' % (export_vrf, reach),
    }
    lines = ['=== DUT_OTLP_PROBE [%s] vrf=%s unit=%s ===' % (tag, export_vrf, unit)]
    with allure.step('Probe DUT OTLP export (%s)' % (tag,)):
        for name, cmd in probes.items():
            out = (dut.run_cmd(cmd, validate=False, print_output=False) or '').strip()[-1500:]
            lines.append('--- %s ---\n%s' % (name, out))
    report = '\n'.join(lines)
    logger.info('DUT_OTLP_PROBE [%s]:\n%s', tag, report)
    allure.attach('dut-otlp-probe-%s' % (tag,), report)
    return report


def setup_otel_suite(
    engines,
    *,
    insecure: bool = True,
    interface_sample_interval_sec: int = OtelCollectorConst.INTERFACE_STATS_SAMPLE_INTERVAL_SEC,
    install_if_missing: bool = True,
    export_vrf: Optional[str] = None,
    is_nvos: bool = False,
    use_secondary: bool = False,
) -> OtelSuiteContext:
    """
    Bring the OTEL suite up end-to-end (idempotent; safe to re-run after a previous broken run).

    Steps (each its own Allure step):

    1. Pre-clean DUT NVUE OTLP destinations + family knobs (best-effort).
    2. Construct primary (engines.sonic_mgmt) + (when ``use_secondary``) secondary
       (engines.ha) collector clients.
    3. Truncate the export JSON of every configured collector.
    4. Ensure primary collector running on sonic-mgmt (install_if_missing).
    5. When ``use_secondary``, ensure secondary collector running on HA host (install_if_missing).
    6. Configure NVUE OTLP on the DUT with the destination(s) + insecure + interface-stats baseline.
       Optional ``export_vrf`` (e.g. ``mgmt``) for management-network collector reachability.
    7. Verify every configured collector receives non-empty artifacts within one collection window.

    Single-collector by default: most setups have no ``ha`` player, and only tests
    marked ``multiple_otlp_collectors`` read a second collector. Set
    ``use_secondary=True`` for those (see the ``otel_suite_dual`` fixture); the suite
    then skips when the topology has no ``ha`` player.

    Fails the suite if ``engines.sonic_mgmt`` is missing.

    Caller must call :func:`teardown_otel_suite` (with the returned context) at module exit.
    """
    if not hasattr(engines, "sonic_mgmt"):
        pytest.fail("OTEL suite requires a 'sonic-mgmt' player in the topology (engines.sonic_mgmt missing).")
    if use_secondary and not hasattr(engines, "ha"):
        pytest.skip("needs a second collector host ('ha' player) for two OTLP destinations")

    dut = engines.dut

    primary = OtelCollector.build_collector(engines, OtelCollectorLabel.PRIMARY)
    primary_ip = primary.ip
    secondary = (
        OtelCollector.build_collector(engines, OtelCollectorLabel.SECONDARY)
        if use_secondary
        else None
    )
    secondary_ip = secondary.ip if secondary else None

    from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
        cleanup_stale_nvue_censor_files,
    )

    cleanup_stale_nvue_censor_files(dut)

    with allure.step("Pre-clean NVUE OTLP state on DUT"):
        cleanup_switch_otlp_grpc(dut)

    collectors = [primary] + ([secondary] if secondary else [])

    with allure.step("Truncate collector export artifacts"):
        for collector in collectors:
            collector.truncate_artifact()

    with allure.step(f"Ensure primary collector running ({primary_ip})"):
        primary.ensure_running(install_if_missing=install_if_missing)

    if secondary:
        with allure.step(f"Ensure secondary collector running ({secondary_ip})"):
            secondary.ensure_running(install_if_missing=install_if_missing)

    with allure.step("Configure NVUE OTLP destination(s) on DUT"):
        configure_switch_otlp_grpc_dual_destination(
            dut,
            primary_ip=primary_ip,
            secondary_ip=secondary_ip,
            interface_sample_interval_sec=interface_sample_interval_sec,
            insecure=insecure,
            export_vrf=export_vrf,
            is_nvos=is_nvos,
        )

    with allure.step("Verify collectors receive non-empty artifacts"):
        wait_sec = OtelCollectorConst.collection_window_sec(interface_sample_interval_sec)
        poll_timeout = OtelCollectorConst.artifact_poll_timeout_sec(
            interface_sample_interval_sec
        )
        vrf = export_vrf or TelemetryConsts.Defaults.EXPORT_VRF
        with allure.step(f"Sleep {wait_sec}s for first OTLP batch"):
            time.sleep(wait_sec)
        for collector in collectors:
            with allure.step(
                f"{collector.label.value} artifact non-empty (poll timeout={poll_timeout}s)"
            ):
                try:
                    collector.wait_for_artifact(timeout_sec=poll_timeout)
                except Exception:
                    _probe_dut_otlp_export(
                        dut, export_vrf=vrf, is_nvos=is_nvos, collector_ip=collector.ip,
                        tag=collector.label.value,
                    )
                    raise
        with allure.step("Truncate artifacts before tests start"):
            for collector in collectors:
                collector.truncate_artifact()

    return OtelSuiteContext(
        primary=primary,
        secondary=secondary,
        primary_ip=primary_ip,
        secondary_ip=secondary_ip,
        is_nvos=is_nvos,
        export_vrf=export_vrf or TelemetryConsts.Defaults.EXPORT_VRF,
        telemetry_unit=nvtelemetry_unit(
            is_nvos, export_vrf or TelemetryConsts.Defaults.EXPORT_VRF
        ),
    )


def setup_otel_suite_secured(
    engines,
    *,
    export_vrf: Optional[str] = None,
    install_if_missing: bool = True,
    default_vrf_grpc_certificate: bool = True,
    enable_routing: bool = True,
    enable_interface_histogram: bool = False,
    is_nvos: bool = False,
    use_secondary: bool = False,
    nvos_families: Optional[Iterable[str]] = None,
) -> OtelSuiteContext:
    """Bring up TLS OTLP suite (primary collector only; SSIM ``Otel*WithTLSConfig`` parity).

    Generates TLS material locally on the pytest runner (SSIM ``gen_tls_certs_mgmt``),
    uploads server cert/key to sonic-mgmt (SSIM ``copy_server_cert_and_key``), imports CA
    on the DUT (SSIM ``copy_ca_cert``), configures the collector with a TLS gRPC receiver,
    and applies secured NVUE telemetry via
    :func:`ngts.tests_nvos.system.telemetry.otel.cumulus.helpers.apply_otel_secured_telemetry_config`.

    ``nvos_families`` (NVOS only) selects which metric families are enabled to open the
    TLS session; defaults to :data:`TelemetryConsts.ALL_STATS_SUBTREES`. Callers that only
    need the session established (e.g. the syslog secured suite) can pass a trimmed,
    universally-supported subset to avoid unmapped-metric subscription storms.
    """
    from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import (
        apply_otel_secured_telemetry_config,
    )
    from ngts.tests_nvos.system.telemetry.otel.cumulus.telemetry_health import (
        assert_otlp_session_established,
    )
    from ngts.tests_nvos.system.telemetry.otel.cumulus.tls import (
        configure_collector_tls,
        generate_tls_certs_locally,
        install_ca_on_dut,
        upload_server_certs_to_collector,
    )

    if not hasattr(engines, "sonic_mgmt"):
        pytest.fail("OTEL suite requires engines.sonic_mgmt.")
    if use_secondary and not hasattr(engines, "ha"):
        pytest.skip("needs a second collector host ('ha' player) for two OTLP destinations")

    dut = engines.dut
    primary = OtelCollector.build_collector(engines, OtelCollectorLabel.PRIMARY)
    secondary = (
        OtelCollector.build_collector(engines, OtelCollectorLabel.SECONDARY)
        if use_secondary
        else None
    )
    primary_ip = primary.ip
    secondary_ip = secondary.ip if secondary else None

    from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
        cleanup_stale_nvue_censor_files,
        settle_dut_ssh_after_module_transition,
    )

    cleanup_stale_nvue_censor_files(dut)

    with allure.step("Pre-clean NVUE OTLP state on DUT"):
        cleanup_switch_otlp_grpc(dut)
        settle_dut_ssh_after_module_transition(dut)

    with allure.step("Truncate primary collector export artifact"):
        primary.truncate_artifact()
        if secondary:
            secondary.truncate_artifact()

    material = generate_tls_certs_locally(san_ip=primary_ip)
    upload_server_certs_to_collector(primary, material)
    install_ca_on_dut(dut, material)

    with allure.step(f"Ensure primary collector running with TLS ({primary_ip})"):
        configure_collector_tls(primary, material, install_if_missing=install_if_missing)

    with allure.step("Configure NVUE secured OTLP on DUT"):
        configure_switch_otlp_grpc_secured_destination(
            dut,
            primary_ip,
            export_vrf=export_vrf,
            default_vrf_grpc_certificate=default_vrf_grpc_certificate,
            is_nvos=is_nvos,
        )
        vrf = export_vrf or TelemetryConsts.Defaults.EXPORT_VRF
        if is_nvos:
            # Match Cumulus secured test01: root families only (_enable_secured_root_telemetry
            # has no stats-group). Do not pass collector_ips here.
            families = (
                tuple(nvos_families)
                if nvos_families is not None
                else TelemetryConsts.ALL_STATS_SUBTREES
            )
            enable_nvos_families_export(dut, families)
        else:
            apply_otel_secured_telemetry_config(
                dut,
                export_vrf=vrf,
                enable_routing=enable_routing,
                enable_interface_histogram=enable_interface_histogram,
            )

    with allure.step("Verify OTLP TLS session established"):
        assert_otlp_session_established(primary)

    return OtelSuiteContext(
        primary=primary,
        secondary=secondary,
        primary_ip=primary_ip,
        secondary_ip=secondary_ip,
        is_nvos=is_nvos,
        export_vrf=vrf,
        telemetry_unit=nvtelemetry_unit(is_nvos, vrf),
    )


def teardown_otel_suite(engines, suite: OtelSuiteContext) -> None:
    """Cleanup: stop DUT from sending metrics, stop both collectors, truncate both artifacts.

    Order matters:

    1. Reset DUT NVUE telemetry first (blanket ``nv unset``) so the DUT stops
       exporting OTLP traffic before we kill the listeners.
    2. Stop both collector processes (primary on sonic-mgmt, secondary on HA).
    3. Truncate both collectors' file-exporter artifacts.

    Every step always runs to completion; failures are collected and re-raised
    at the end so the suite fails loudly while still leaving the environment
    in the cleanest possible state.
    """
    failures: List[str] = []

    with allure.step("Cleanup DUT NVUE OTLP state"):
        try:
            cleanup_switch_otlp_grpc(engines.dut)
        except Exception as exc:  # noqa: BLE001
            logger.error("OTEL teardown: NVUE cleanup raised: %s", exc)
            failures.append(f"NVUE cleanup: {exc}")

    active_collectors = [c for c in (suite.primary, suite.secondary) if c is not None]

    with allure.step("Stop collector processes"):
        for collector in active_collectors:
            try:
                collector.stop()
            except Exception as exc:  # noqa: BLE001
                logger.error("OTEL teardown: %s stop raised: %s", collector.label.value, exc)
                failures.append(f"stop {collector.label.value}: {exc}")

    with allure.step("Truncate collector artifacts"):
        for collector in active_collectors:
            try:
                collector.truncate_artifact()
            except Exception as exc:  # noqa: BLE001
                logger.error("OTEL teardown: truncate %s raised: %s", collector.label.value, exc)
                failures.append(f"truncate {collector.label.value}: {exc}")

    if failures:
        pytest.fail(
            "OTEL teardown completed with failures:\n  - " + "\n  - ".join(failures)
        )


# --- Collector artifact parsing & metric-name comparison ---------------------


def _read_otel_collector_documents(otel_json_path: str) -> List[Dict[str, Any]]:
    """Read 1+ OTEL JSON documents from a single file (handles concatenated / line-delimited JSON)."""
    file_size = os.path.getsize(otel_json_path) if os.path.exists(otel_json_path) else -1
    assert file_size > 0, (
        f"OTEL collector artifact is empty or missing: {otel_json_path} (size={file_size}). "
        "This usually means telemetry was not emitted/received yet."
    )
    with open(otel_json_path, "r", encoding="utf-8") as handle:
        content = handle.read()
    content = content.lstrip("\ufeff")

    decoder = json.JSONDecoder()
    docs: List[Dict[str, Any]] = []
    index = 0
    length = len(content)
    while index < length:
        while index < length and content[index].isspace():
            index += 1
        if index >= length:
            break
        if content[index] not in "{[":
            next_object = min(
                [pos for pos in (content.find("{", index), content.find("[", index)) if pos != -1],
                default=-1,
            )
            if next_object == -1:
                break
            index = next_object
            continue
        try:
            doc, next_index = decoder.raw_decode(content, index)
        except json.JSONDecodeError:
            next_object = min(
                [pos for pos in (content.find("{", index + 1), content.find("[", index + 1)) if pos != -1],
                default=-1,
            )
            if next_object == -1:
                break
            index = next_object
            continue
        if isinstance(doc, dict):
            docs.append(doc)
        elif isinstance(doc, list):
            docs.extend([item for item in doc if isinstance(item, dict)])
        index = next_index

    assert docs, (
        f"Failed to parse any OTEL JSON documents from artifact: {otel_json_path}. "
        f"First 120 chars={content[:120]!r}"
    )
    return docs


def _metric_names_from_docs(docs: Iterable[Dict[str, Any]]) -> Set[str]:
    """Walk OTEL documents and return the unique set of metric names."""
    names: Set[str] = set()
    for doc in docs:
        for resource in doc.get("resourceMetrics", []) or []:
            for scope in resource.get("scopeMetrics", []) or []:
                for metric in scope.get("metrics", []) or []:
                    name = metric.get("name")
                    if name:
                        names.add(name)
    return names


def load_otel_metric_names(otel_json_path: str) -> Set[str]:
    """Extract the unique set of OTEL metric names from a collector file exporter artifact."""
    return _metric_names_from_docs(_read_otel_collector_documents(otel_json_path))


def collect_metric_names_window(
    collector: OtelCollector,
    local_dir: str,
    *,
    label: str,
    max_sample_interval_sec: int = OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC,
    wait_sec: Optional[int] = None,
) -> Set[str]:
    """
    Truncate the collector's export, wait one collection window, fetch the artifact, parse names.

    The wait defaults to :func:`OtelCollectorConst.collection_window_sec` — at minimum
    :data:`OtelCollectorConst.OTEL_COLLECTION_MIN_WAIT_SEC` (70s) so platform-stats (60s
    sample interval) always emits at least one point. Pass ``wait_sec`` explicitly for
    Cumulus collection window (e.g. test01's 177s wait). Returns the de-duplicated set of metric names
    seen in the artifact.
    """
    if wait_sec is None:
        wait_sec = OtelCollectorConst.collection_window_sec(max_sample_interval_sec)
    poll_timeout = wait_sec + OtelCollectorConst.ARTIFACT_TIMEOUT_SEC
    file_name = f"otel-out-{label}.json"

    with allure.step(f"Collect metric names ({label})"):
        collector.truncate_artifact()
        with allure.step(f"Sleep {wait_sec}s for collection window"):
            time.sleep(wait_sec)
        local_json = collector.fetch_artifact(
            local_dir, file_name=file_name, timeout_sec=poll_timeout
        )

    return load_otel_metric_names(local_json)


def collect_otlp_metrics_window(
    collector: OtelCollector,
    local_dir: str,
    *,
    label: str,
    max_sample_interval_sec: int = OtelCollectorConst.PLATFORM_STATS_SAMPLE_INTERVAL_SEC,
    wait_sec: Optional[int] = None,
) -> Tuple[Set[str], List[Dict[str, Any]], MetricTimestamps]:
    """Collect OTLP artifact; return metric names, parsed docs, and ``metrics_timestamps``."""
    if wait_sec is None:
        wait_sec = OtelCollectorConst.collection_window_sec(max_sample_interval_sec)
    poll_timeout = wait_sec + OtelCollectorConst.ARTIFACT_TIMEOUT_SEC
    file_name = f"otel-out-{label}.json"

    with allure.step(f"Collect OTLP metrics ({label})"):
        collector.truncate_artifact()
        with allure.step(f"Sleep {wait_sec}s for collection window"):
            time.sleep(wait_sec)
        local_json = collector.fetch_artifact(
            local_dir, file_name=file_name, timeout_sec=poll_timeout
        )

    docs = _read_otel_collector_documents(local_json)
    names = _metric_names_from_docs(docs)
    timestamps = metric_timestamps_from_docs(docs)
    return names, docs, timestamps


def _is_prometheus_sidecar_metric(metric_name: str) -> bool:
    """True for node_exporter / scrape artifacts co-exported on OTLP (not NVUE catalog)."""
    if metric_name in OtelCollectorConst.OTEL_PROMETHEUS_SIDECAR_EXACT_NAMES:
        return True
    return any(
        metric_name.startswith(prefix)
        for prefix in OtelCollectorConst.OTEL_PROMETHEUS_SIDECAR_NAME_PREFIXES
    )


def _metric_name_matches_group_prefix(metric_name: str, group_prefix: str) -> bool:
    """Return True when ``metric_name`` belongs to the NVUE telemetry family ``group_prefix``.

    metrics-classes / NVUE group names (``interface-stats``, ``platform-stats``, …) map to
    OTLP ``nvswitch_*`` prefixes; each family prefix also matches the metrics of its
    ``-phy`` subgroup.
    """
    if group_prefix == TelemetryConsts.INTERFACE_STATS:
        return metric_name.startswith("nvswitch_interface_")
    if group_prefix == TelemetryConsts.PLATFORM_STATS:
        return metric_name.startswith("nvswitch_platform_")
    if group_prefix == TelemetryConsts.PEER_PORT_STATS:
        return metric_name.startswith("nvswitch_peer_port_")
    if group_prefix == TelemetryConsts.IB_ROUTER_STATS:
        return metric_name.startswith("nvswitch_ib_router_")
    if group_prefix == "buffer-stats":
        return (
            "nvswitch_interface_shared_buffer" in metric_name or
            "nvswitch_interface_headroom" in metric_name
        )
    if group_prefix == "control-plane-stats":
        return metric_name.startswith("nvswitch_control_plane_")
    if group_prefix == "histogram":
        return metric_name.startswith("nvswitch_histogram_")
    if group_prefix == "lldp":
        return metric_name.startswith("nvswitch_lldp_")
    if group_prefix == "software-stats":
        return _is_prometheus_sidecar_metric(metric_name)
    slug = group_prefix.replace("-", "_")
    return metric_name.startswith(f"nvswitch_{slug}_")


def _log_inventory_metric_names(
    names: List[str],
    section_title: str,
    *,
    max_names: Optional[int] = None,
) -> None:
    """Emit one INFO line per metric; optional cap only when ``max_names`` is set."""
    logger.info("  --- %s (%d) ---", section_title, len(names))
    display = names if max_names is None else names[:max_names]
    for name in display:
        logger.info("    %s", name)
    if max_names is not None and len(names) > max_names:
        logger.info("    ... +%d more", len(names) - max_names)


def log_otel_metric_inventory(
    actual: Set[str],
    *,
    collector_label: str,
    group_name_prefixes: Optional[Tuple[str, ...]] = None,
    expected: Optional[Set[str]] = None,
    extra_ignore: Optional[Set[str]] = None,
    max_names_per_section: Optional[int] = None,
) -> Dict[str, Union[int, List[str], Dict[str, List[str]]]]:
    """Log which metrics were collected vs expected; return a summary for assertions/debug.

    Categories logged:
    - **by_prefix**: actual names matching each ``group_name_prefixes`` entry (NVUE telemetry).
    - **prometheus_sidecar**: ``node_*``, ``scrape_*``, etc. (allowed when YAML is absent).
    - **other_unmatched**: collected but not telemetry catalog and not prometheus sidecar.
    - **missing** / **unexpected_vs_expected**: only when ``expected`` is provided.

    By default every name in each section is logged. Pass ``max_names_per_section`` to restore
    truncated output (e.g. 30 for prefix groups, 25 for prometheus).
    """
    ignore: Set[str] = set(extra_ignore) if extra_ignore else set()
    actual_filtered = set(actual) - ignore
    expected_filtered = (set(expected) - ignore) if expected is not None else None

    by_prefix: Dict[str, List[str]] = {}
    if group_name_prefixes:
        for prefix in group_name_prefixes:
            by_prefix[prefix] = sorted(
                name
                for name in actual_filtered
                if _metric_name_matches_group_prefix(name, prefix)
            )

    telemetry_matched: Set[str] = set()
    for names in by_prefix.values():
        telemetry_matched.update(names)

    prometheus_sidecar = sorted(
        name for name in actual_filtered if _is_prometheus_sidecar_metric(name)
    )
    other_unmatched = sorted(
        name
        for name in actual_filtered
        if name not in telemetry_matched and not _is_prometheus_sidecar_metric(name)
    )
    missing: List[str] = []
    unexpected_vs_expected: List[str] = []
    if expected_filtered is not None:
        missing = sorted(expected_filtered - actual_filtered)
        unexpected_vs_expected = sorted(actual_filtered - expected_filtered)

    cap_note = (
        f" (capped at {max_names_per_section} per section)"
        if max_names_per_section is not None
        else " (full list)"
    )
    full_inventory_text = "\n".join(sorted(actual_filtered))

    with allure.step(f"OTEL metric inventory ({collector_label})"):
        logger.info("=" * 72)
        logger.info("OTEL METRIC INVENTORY — %s", collector_label)
        logger.info("  collected (raw):           %d", len(actual))
        logger.info("  after exclude/ignore:      %d (ignore=%d)", len(actual_filtered), len(ignore))
        logger.info("  NVUE telemetry (matched): %d", len(telemetry_matched))
        logger.info("  prometheus sidecar:        %d", len(prometheus_sidecar))
        logger.info("  other (unclassified):      %d", len(other_unmatched))
        logger.info("  detail logging%s", cap_note)
        if expected_filtered is not None:
            logger.info("  expected catalog size:     %d", len(expected_filtered))
            logger.info("  missing from export:     %d", len(missing))
            logger.info("  unexpected vs expected:  %d", len(unexpected_vs_expected))

        for prefix, names in by_prefix.items():
            _log_inventory_metric_names(
                names,
                f"AVAILABLE by prefix '{prefix}'",
                max_names=max_names_per_section,
            )

        if other_unmatched:
            _log_inventory_metric_names(
                other_unmatched,
                "NOT AVAILABLE / unclassified",
                max_names=max_names_per_section,
            )

        if prometheus_sidecar:
            _log_inventory_metric_names(
                prometheus_sidecar,
                "PROMETHEUS SIDECAR (allowed)",
                max_names=max_names_per_section,
            )

        if missing:
            _log_inventory_metric_names(
                missing,
                "EXPECTED but NOT collected",
                max_names=max_names_per_section,
            )

        if unexpected_vs_expected:
            _log_inventory_metric_names(
                unexpected_vs_expected,
                "COLLECTED but NOT in expected set",
                max_names=max_names_per_section,
            )
        logger.info("=" * 72)
        try:
            allure.attach(
                f"otel-metric-inventory-{collector_label}",
                full_inventory_text,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("OTEL: Allure attach of metric inventory failed: %s", exc)

    return {
        "actual_count": len(actual_filtered),
        "telemetry_matched_count": len(telemetry_matched),
        "prometheus_sidecar_count": len(prometheus_sidecar),
        "other_unmatched_count": len(other_unmatched),
        "by_prefix": by_prefix,
        "prometheus_sidecar": prometheus_sidecar,
        "other_unmatched": other_unmatched,
        "missing": missing,
        "unexpected_vs_expected": unexpected_vs_expected,
    }


def assert_metric_names_strict(
    expected: Set[str],
    actual: Set[str],
    *,
    collector_label: str,
    extra_ignore: Optional[Set[str]] = None,
    group_name_prefixes: Optional[Tuple[str, ...]] = None,
) -> None:
    """Strict equality of expected vs actual metric names. Subtracts ``extra_ignore`` from both sides if given."""
    ignore: Set[str] = set(extra_ignore) if extra_ignore else set()

    expected_filtered = set(expected) - ignore
    actual_filtered = set(actual) - ignore

    missing = expected_filtered - actual_filtered
    unexpected = actual_filtered - expected_filtered

    log_otel_metric_inventory(
        actual,
        collector_label=collector_label,
        group_name_prefixes=group_name_prefixes,
        expected=expected,
        extra_ignore=ignore,
    )

    with allure.step(
        f"Strict metric-name equality on {collector_label} "
        f"(expected={len(expected_filtered)}, actual={len(actual_filtered)})"
    ):
        if missing or unexpected:
            msg_lines = [
                f"OTEL metric-name strict-equality FAILED on {collector_label}.",
                f"  expected (filtered): {len(expected_filtered)}",
                f"  actual   (filtered): {len(actual_filtered)}",
                f"  ignore_list_size:    {len(ignore)}",
                f"  --- missing ({len(missing)}) ---",
            ]
            msg_lines.extend(sorted(missing) or ["  (none)"])
            msg_lines.append(f"  --- unexpected ({len(unexpected)}) ---")
            msg_lines.extend(sorted(unexpected) or ["  (none)"])
            msg_lines.append(
                "Resolutions: (a) pass extra_ignore to skip a name temporarily, "
                "(b) add to OTLP_METRIC_NAME_PREFIXES_GATED_BY_CAPABILITY (and probe in dut_capabilities), "
                "(c) fix the metric pipeline, (d) accept the new metric in metrics-classes.yaml."
            )
            pytest.fail("\n".join(msg_lines))


def assert_nvos_metric_names_for_enablement(
    engines,
    devices,
    cur_dir: str,
    *,
    actual_metric_names: Iterable[str],
    enablement: "TelemetryEnablement",
    destination_ip: str,
    is_ib_router: bool,
    collector_label: str,
) -> None:
    """Strict metric-name equality from metrics-classes-full.yaml + declared enablement.

    Shared by dual-platform collection and coexistence NVOS validators: callers
    supply already-collected metric names (and a pre-built enablement).
    """
    expected = expected_metric_names_for_enablement(
        engines,
        cur_dir,
        enablement,
        destination_ip=destination_ip,
        file_name="metrics-classes-full.yaml",
        is_ib_router=is_ib_router,
        devices=devices,
    )
    assert expected, "No expected metric names after NVOS enablement gating"
    assert_metric_names_strict(
        expected,
        set(actual_metric_names),
        collector_label=collector_label,
        group_name_prefixes=OtelCollectorConst.METRICS_CLASSES_FULL_EXPORT_GROUP_PREFIXES,
    )


# =============================================================================
# Expected-metrics derivation — fetch metrics-classes.yaml from the DUT and gate
# the expected metric-name set by DUT capabilities. Only
# expected_metric_names_from_dut is consumed by tests; everything below is
# internal to this section.
# =============================================================================


# --- metrics-classes.yaml fetch + parse --------------------------------------


def _resolve_metrics_classes_remote_path(dut) -> Optional[str]:
    """Locate ``metrics-classes.yaml`` on the DUT (sudo-readable paths)."""
    for path in OtelCollectorConst.METRICS_CLASSES_CANDIDATE_PATHS:
        out = dut.run_cmd(
            f"sudo test -f {shlex.quote(path)} && echo {shlex.quote(path)}",
            validate=False,
            print_output=False,
        ).strip()
        if out == path:
            logger.info("Found metrics-classes.yaml at %s", path)
            return path
    find_cmd = (
        "sudo find /etc /usr /opt /var -maxdepth 8 "
        "-name 'metrics-classes.yaml' -type f 2>/dev/null | head -1"
    )
    found = dut.run_cmd(find_cmd, validate=False, print_output=False).strip()
    if found:
        line = found.splitlines()[0].strip()
        logger.info("Found metrics-classes.yaml via find: %s", line)
        return line
    return None


def fetch_metrics_classes_yaml_from_dut(
    engines,
    local_output_dir: str,
    file_name: str = "metrics-classes.yaml",
    remote_path: Optional[str] = None,
) -> str:
    """SCP the DUT's ``metrics-classes.yaml`` locally (via a world-readable /tmp staging copy)."""
    os.makedirs(local_output_dir, exist_ok=True)
    local_path = os.path.join(local_output_dir, file_name)
    dut = engines.dut
    source = remote_path or _resolve_metrics_classes_remote_path(dut)
    if not source:
        pytest.fail(
            "metrics-classes.yaml not found on DUT "
            f"(tried {OtelCollectorConst.METRICS_CLASSES_CANDIDATE_PATHS} and find). "
            "NVUE telemetry export can still work; install nv-umf-manager metrics-classes "
            "or use assert_metric_names_for_stats_group_export when YAML is absent."
        )
    staged = OtelCollectorConst.METRICS_CLASSES_STAGED_ON_DUT
    with allure.step(f"Fetch metrics-classes.yaml from DUT ({source})"):
        dut.run_cmd(
            f"sudo cp {shlex.quote(source)} {shlex.quote(staged)} && "
            f"sudo chmod 644 {shlex.quote(staged)}",
            validate=True,
        )
        try:
            scp_file(dut, staged, local_path, download_from_remote=True)
        finally:
            dut.run_cmd(f"rm -f {shlex.quote(staged)}", validate=False)
    return local_path


def try_fetch_metrics_classes_yaml_from_dut(
    engines,
    local_output_dir: str,
    file_name: str = "metrics-classes.yaml",
) -> Optional[str]:
    """Like :func:`fetch_metrics_classes_yaml_from_dut` but returns ``None`` if the file is absent."""
    if not _resolve_metrics_classes_remote_path(engines.dut):
        logger.warning(
            "metrics-classes.yaml not found on DUT; skipping YAML-based expected set"
        )
        return None
    return fetch_metrics_classes_yaml_from_dut(engines, local_output_dir, file_name=file_name)


def assert_metric_names_for_stats_group_export(
    actual: Set[str],
    *,
    collector_label: str,
    group_name_prefixes: Tuple[str, ...],
    extra_ignore: Optional[Set[str]] = None,
) -> None:
    """Validate NVUE telemetry metrics per prefix were exported (no strict expected set).

    Checks for ``nvswitch_*`` names matching ``group_name_prefixes``. Prometheus
    ``node_*`` / ``scrape_*`` sidecar metrics are logged but not treated as failures.
    """
    ignore: Set[str] = set(extra_ignore) if extra_ignore else set()
    summary = log_otel_metric_inventory(
        actual,
        collector_label=collector_label,
        group_name_prefixes=group_name_prefixes,
        extra_ignore=ignore,
    )

    with allure.step(
        f"Validate stats-group metric export on {collector_label} "
        f"(prefixes={group_name_prefixes})"
    ):
        assert actual, f"{collector_label}: no OTLP metrics collected"
        if summary["telemetry_matched_count"] == 0:
            pytest.fail(
                f"{collector_label}: no NVUE telemetry metrics (nvswitch_*) matched "
                f"prefixes {group_name_prefixes}; see OTEL METRIC INVENTORY log above."
            )
        other_unmatched = summary["other_unmatched"]
        if other_unmatched:
            pytest.fail(
                f"{collector_label}: {len(other_unmatched)} metric(s) outside telemetry "
                f"prefixes {group_name_prefixes} and not prometheus sidecar "
                f"(first 20): {other_unmatched[:20]}" +
                (
                    f" ... (+{len(other_unmatched) - 20} more)"
                    if len(other_unmatched) > 20
                    else ""
                ) +
                "; see OTEL METRIC INVENTORY log above."
            )
        logger.info(
            "%s: export OK — telemetry=%d, prometheus_sidecar=%d (prefixes=%s)",
            collector_label,
            summary["telemetry_matched_count"],
            summary["prometheus_sidecar_count"],
            group_name_prefixes,
        )


def _parse_metrics_classes_entry(raw: str) -> str:
    line = raw.strip()
    if line.startswith("- "):
        line = line[2:].strip()
    bracket = line.find("[")
    if bracket != -1:
        line = line[:bracket].strip()
    return line.strip()


def load_otlp_metric_names_from_metrics_classes_yaml(
    yaml_path: str,
    *,
    group_name_prefixes: Tuple[str, ...],
) -> Set[str]:
    """Return expected OTLP metric base names from groups whose name matches a prefix."""
    with allure.step(f"Parse expected metric names from {os.path.basename(yaml_path)}"):
        with open(yaml_path, "r", encoding="utf-8") as handle:
            doc = yaml.safe_load(handle) or {}
    names: Set[str] = set()
    for group_name, entries in doc.items():
        if not isinstance(group_name, str) or not group_name.strip():
            continue
        if not any(group_name.startswith(pfx) for pfx in group_name_prefixes):
            continue
        if not isinstance(entries, list):
            continue
        for item in entries:
            if isinstance(item, str):
                parsed = _parse_metrics_classes_entry(item)
                if parsed:
                    names.add(parsed)
    return names


def _metrics_classes_group_names(yaml_path: str) -> Set[str]:
    """Return the set of top-level group names present in ``metrics-classes.yaml``."""
    with open(yaml_path, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    return {g for g in doc if isinstance(g, str) and g.strip()}


def load_otlp_metric_names_for_groups(
    yaml_path: str,
    *,
    exact_group_names: Optional[Set[str]] = None,
    group_name_prefixes: Optional[Tuple[str, ...]] = None,
) -> Tuple[Set[str], Set[str]]:
    """Return (metric names, matched group names) for the requested groups.

    A group matches when its name is in ``exact_group_names`` OR starts with one
    of ``group_name_prefixes``. Returning the matched group names lets callers
    detect requested groups that are absent from the YAML (forward-compat).
    """
    exact = set(exact_group_names or set())
    prefixes = tuple(group_name_prefixes or ())
    with allure.step(f"Parse expected metric names for groups from {os.path.basename(yaml_path)}"):
        with open(yaml_path, "r", encoding="utf-8") as handle:
            doc = yaml.safe_load(handle) or {}
    names: Set[str] = set()
    matched_groups: Set[str] = set()
    for group_name, entries in doc.items():
        if not isinstance(group_name, str) or not group_name.strip():
            continue
        if group_name not in exact and not any(
            group_name.startswith(pfx) for pfx in prefixes
        ):
            continue
        matched_groups.add(group_name)
        if not isinstance(entries, list):
            continue
        for item in entries:
            if isinstance(item, str):
                parsed = _parse_metrics_classes_entry(item)
                if parsed:
                    names.add(parsed)
    return names, matched_groups


# --- Expected set derivation + capability gating -----------------------------


def expected_metric_names_from_dut(
    engines,
    local_output_dir: str,
    group_name_prefixes: Tuple[str, ...],
    file_name: str = "metrics-classes.yaml",
    *,
    yaml_path: Optional[str] = None,
    is_ib_router: bool = False,
    devices=None,
) -> Set[str]:
    """
    Fetch ``metrics-classes.yaml`` from the DUT, return the expected OTLP metric-name set for the
    given group prefixes, with **capability gating applied** (names absent because the DUT lacks
    the relevant capability are subtracted from expected).

    Required test inputs:

    - ``devices``: the standard session-scope ``devices`` pytest fixture; used to read
      ``devices.dut.switch_type`` so we can decide IB-vs-NVL (peer-port-stats only on NVL).
    - ``is_ib_router``: from the existing session-scope ``is_ib_router`` fixture (CLI flag).

    Other capabilities (connected-transceivers presence, ib-router-profile state)
    are probed automatically.
    """
    if yaml_path is None:
        yaml_path = fetch_metrics_classes_yaml_from_dut(
            engines, local_output_dir, file_name=file_name
        )
    expected = load_otlp_metric_names_from_metrics_classes_yaml(
        yaml_path, group_name_prefixes=group_name_prefixes
    )

    capabilities = dut_capabilities(
        is_ib_router=is_ib_router, devices=devices, dut=engines.dut
    )
    expected_after_gating, gating_summary = _apply_capability_gating(expected, capabilities)

    with allure.step(
        f"Expected metric names: {len(expected_after_gating)} after gating "
        f"(raw={len(expected)}, removed={len(expected) - len(expected_after_gating)})"
    ):
        try:
            allure.attach(
                f"capability gating summary ({', '.join(group_name_prefixes)})",
                gating_summary,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("OTEL: Allure attach of capability summary failed: %s", exc)
    return expected_after_gating


def _group_names_for_enabled_classes(effective: "EnabledClasses") -> Tuple[Set[str], Set[str]]:
    """Resolve enabled (family, class) pairs to exact YAML group names.

    Returns ``(group_names, unmapped_tokens)`` where ``unmapped_tokens`` collects
    ``"family/class"`` pairs that have no entry in the class->group mapping (logged
    by the caller for forward-compat).
    """
    group_names: Set[str] = set()
    unmapped: Set[str] = set()
    for family, classes in effective.families.items():
        for cls in classes:
            if family == TelemetryConsts.PLATFORM_STATS:
                if cls == OtelCollectorConst.TELEMETRY_BASE_CLASS:
                    # platform metrics are fully categorized; no base group.
                    continue
                group_names.add(
                    OtelCollectorConst.METRICS_CLASSES_PLATFORM_GROUP_FMT.format(cls=cls)
                )
                continue
            mapped = OtelCollectorConst.METRICS_CLASSES_GROUP_BY_FAMILY_CLASS.get(
                (family, cls)
            )
            if mapped:
                group_names.update(mapped)
            else:
                unmapped.add(f"{family}/{cls}")
    return group_names, unmapped


def expected_metric_names_for_enablement(
    engines,
    local_output_dir: str,
    enablement: "TelemetryEnablement",
    *,
    destination_ip: str,
    file_name: str = "metrics-classes.yaml",
    yaml_path: Optional[str] = None,
    is_ib_router: bool = False,
    devices=None,
) -> Set[str]:
    """Expected OTLP metric names for ``destination_ip`` from the test-declared enablement.

    Resolves the destination's effective enabled classes (a bound stats-group
    overrides root; an absent/empty binding falls back to root), maps those
    classes to exact ``metrics-classes.yaml`` group names, then applies the same
    hardware capability gating as :func:`expected_metric_names_from_dut`.
    """
    if yaml_path is None:
        yaml_path = fetch_metrics_classes_yaml_from_dut(
            engines, local_output_dir, file_name=file_name
        )

    effective = enablement.effective_for_destination(destination_ip)
    requested_groups, unmapped = _group_names_for_enabled_classes(effective)

    expected_raw, matched_groups = load_otlp_metric_names_for_groups(
        yaml_path, exact_group_names=requested_groups
    )
    missing_groups = requested_groups - matched_groups

    capabilities = dut_capabilities(
        is_ib_router=is_ib_router, devices=devices, dut=engines.dut
    )
    expected_after_gating, gating_summary = _apply_capability_gating(
        expected_raw, capabilities
    )

    with allure.step(
        f"Expected metric names for destination {destination_ip}: "
        f"{len(expected_after_gating)} after gating "
        f"(groups={len(matched_groups)}, raw={len(expected_raw)})"
    ):
        logger.info(
            "OTEL expected-for-enablement (dest=%s): groups_requested=%d "
            "groups_matched=%d names_raw=%d names_after_gating=%d",
            destination_ip,
            len(requested_groups),
            len(matched_groups),
            len(expected_raw),
            len(expected_after_gating),
        )
        if missing_groups:
            logger.warning(
                "OTEL expected-for-enablement: requested groups absent from YAML: %s "
                "(available groups: %s)",
                sorted(missing_groups),
                sorted(_metrics_classes_group_names(yaml_path)),
            )
        if unmapped:
            logger.warning(
                "OTEL expected-for-enablement: enabled classes with no group mapping: %s",
                sorted(unmapped),
            )
        try:
            allure.attach(
                f"enablement expected groups (dest {destination_ip})",
                "requested groups:\n  " + "\n  ".join(sorted(requested_groups) or ["(none)"]) +
                "\n\nmatched groups:\n  " + "\n  ".join(sorted(matched_groups) or ["(none)"]) +
                "\n\n" + gating_summary,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("OTEL: Allure attach of enablement summary failed: %s", exc)
    return expected_after_gating


def _apply_capability_gating(
    expected: Set[str],
    capabilities: Set[str],
) -> Tuple[Set[str], str]:
    """Drop expected names whose prefix is gated by a capability the DUT lacks."""
    out = set(expected)
    summary_lines: List[str] = [f"capabilities present: {sorted(capabilities) or '(none)'}"]

    for capability, prefixes in OtelCollectorConst.OTLP_METRIC_NAME_PREFIXES_GATED_BY_CAPABILITY.items():
        if capability in capabilities:
            summary_lines.append(f"  [PRESENT] {capability}: prefixes {list(prefixes)} kept in expected")
            continue
        removed = {name for name in out if any(name.startswith(p) for p in prefixes)}
        if capability == OtelCapability.HAS_IB_ROUTER_PROFILE:
            exempt = set(OtelCollectorConst.OTLP_IB_ROUTER_METRICS_ALWAYS_PRESENT)
            kept = removed & exempt
            removed -= exempt
            if kept:
                summary_lines.append(
                    f"  [EXEMPT]  {capability}: kept {sorted(kept)} despite absent profile"
                )
        out -= removed
        summary_lines.append(
            f"  [ABSENT]  {capability}: prefixes {list(prefixes)} → {len(removed)} names subtracted from expected"
        )

    for capability, names in OtelCollectorConst.OTLP_METRIC_NAMES_GATED_BY_CAPABILITY.items():
        gated = set(names)
        if capability in capabilities:
            summary_lines.append(
                f"  [PRESENT] {capability}: exact names {sorted(gated)} kept in expected"
            )
            continue
        removed = out & gated
        out -= removed
        summary_lines.append(
            f"  [ABSENT]  {capability}: exact names {sorted(gated)} → "
            f"{len(removed)} names subtracted from expected"
        )

    return out, "\n".join(summary_lines)


# --- DUT capability probes ---------------------------------------------------


def dut_capabilities(
    *,
    is_ib_router: bool = False,
    devices,
    dut=None,
) -> Set[str]:
    """Probe the DUT and return the set of present capabilities (best-effort; absent on probe error)."""
    present: Set[str] = set()
    dut_engine = dut or getattr(devices, "dut", None)

    if is_ib_router or _dut_has_ib_router_profile_enabled(dut_engine):
        present.add(OtelCapability.HAS_IB_ROUTER_PROFILE)

    # peer-port-stats and selected platform metrics are NVLink-only; IB skips them.
    if not devices.dut.is_ib():
        present.add(OtelCapability.SUPPORTS_PEER_PORT_STATS)
        present.add(OtelCapability.SUPPORTS_NVLINK_PLATFORM_METRICS)

    if _dut_has_connected_transceivers(dut_engine):
        present.add(OtelCapability.HAS_CONNECTED_TRANSCEIVERS)

    logger.info("OTEL capabilities: %s (is_ib_router=%s)", sorted(present), is_ib_router)
    return present


def _nvue_probe_failed(output: str) -> bool:
    return not output.strip() or "Error:" in output or "is not one of" in output


def _dut_has_ib_router_profile_enabled(dut=None) -> bool:
    """True iff ib-routing is enabled; False when ``system profile`` is absent (typical Cumulus)."""
    if dut is not None:
        out = dut.run_cmd(
            "nv show system profile ib-routing -o json 2>&1",
            validate=False,
            print_output=False,
        )
        if _nvue_probe_failed(out):
            logger.debug("IB router profile probe unavailable: %s", out[:240])
            return False
        try:
            doc = json.loads(out)
            if isinstance(doc, dict):
                value = doc.get("ib-routing") or doc.get("ib_routing")
                if value is not None:
                    return str(value).strip().lower() == _ENABLED
        except json.JSONDecodeError:
            pass
        return "enabled" in out.lower()

    profile_show = System().profile.show()
    if not profile_show.result:
        profile_show.ignore_result()
        return False
    profile_show.ignore_result()
    system_profile_output = (
        OutputParsingTool.parse_show_output_to_dict(profile_show).returned_value or {}
    )
    return str(system_profile_output.get("ib-routing") or "").strip().lower() == _ENABLED


def _dut_has_connected_transceivers(dut=None) -> bool:
    """True iff ``nv show platform transceiver`` returns a non-empty payload."""
    if dut is not None:
        out = dut.run_cmd(
            "nv show platform transceiver -o json 2>&1",
            validate=False,
            print_output=False,
        )
        if _nvue_probe_failed(out):
            return False
        try:
            return bool(json.loads(out))
        except json.JSONDecodeError:
            return False

    tx_show = Platform().transceiver.show()
    if not tx_show.result:
        tx_show.ignore_result()
        return False
    tx_show.ignore_result()
    return bool(OutputParsingTool.parse_show_output_to_dict(tx_show).returned_value)
