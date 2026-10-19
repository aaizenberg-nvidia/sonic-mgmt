"""Cumulus NVUE telemetry overlay and test01/test05 validation helpers."""

import logging
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import ApiType, ConfState, TelemetryConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.lab_topology import (
    histogram_interfaces_on_dut,
    resolve_cumulus_lab_interfaces_on_dut,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.catalog import flatten_supported_metrics
from ngts.tests_nvos.system.telemetry.otel.metrics_parser import MetricTimestamps
from ngts.tests_nvos.system.telemetry.otel.helpers import (
    _DEFAULT_SAMPLE_INTERVALS,
    _apply_otlp_grpc_base,
    _metric_name_matches_group_prefix,
    _set_stats_sample_interval,
    _stats_node,
    _telemetry_stats_parent,
    _is_prometheus_sidecar_metric,
    apply_telemetry_configuration,
    stage_telemetry_master_state,
)

logger = logging.getLogger(__name__)
_ENABLED = TelemetryConsts.State.ENABLED.value
_DISABLED = TelemetryConsts.State.DISABLED.value


def _stats_group_subtree(
    system: System, stats_group_id: str, path: str
) -> BaseComponent:
    """``BaseComponent`` under ``telemetry stats-group <id>``."""
    return BaseComponent(_telemetry_stats_parent(system, stats_group_id), path=path)


def _enable_family_classes(
    system: System,
    dut,
    family: str,
    *,
    stats_group_id: Optional[str] = None,
    platform_classes: Optional[Iterable[str]] = None,
    platform_class_intervals: Optional[Dict[str, int]] = None,
    enable_phy_under_stats_group: bool = False,
) -> None:
    """Enable the class-level (per-subclass) metrics under a single telemetry family.

    Per-family knobs:
    - ``interface-stats`` / ``peer-port-stats``: single ``phy`` class at the telemetry root.
    - ``platform-stats``: platform class categories (root vs stats-group lists differ).
    - ``ib-router-stats``: no class-level subtree (no-op).

    Under ``stats-group <id>``, Cumulus NVUE exposes only ``class debounce`` for
    interface/peer-port stats (not ``phy``), so by default the ``phy`` class is
    skipped under a stats-group. NVOS NVUE does expose ``class phy`` under a
    stats-group, so NVOS callers pass ``enable_phy_under_stats_group=True`` to
    enable it. Platform classes must match
    :data:`CumulusOtelConst.STATS_GROUP_PLATFORM_CLASSES` (no health-info /
    asic-power; includes file-system).
    """
    if (
        stats_group_id and
        not enable_phy_under_stats_group and
        family in (
            TelemetryConsts.INTERFACE_STATS,
            TelemetryConsts.PEER_PORT_STATS,
        )
    ):
        logger.info(
            "Skipping class phy for %s under stats-group %s "
            "(NVUE class subtree is debounce-only)",
            family,
            stats_group_id,
        )
        return
    parent = _telemetry_stats_parent(system, stats_group_id)
    if family == TelemetryConsts.INTERFACE_STATS:
        parent.interface_stats.cls.phy.set(
            TelemetryConsts.STATE, _ENABLED, dut_engine=dut).verify_result()
    elif family == TelemetryConsts.PEER_PORT_STATS:
        parent.peer_port_stats.cls.phy.set(
            TelemetryConsts.STATE, _ENABLED, dut_engine=dut).verify_result()
    elif family == TelemetryConsts.PLATFORM_STATS:
        classes = platform_classes or CumulusOtelConst.ROOT_PLATFORM_CLASSES
        for cat in classes:
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
                continue
            if platform_class_intervals and cat in platform_class_intervals:
                interval = platform_class_intervals[cat]
                if cat in categories:
                    interval_result = categories[cat].set(
                        TelemetryConsts.SAMPLE_INTERVAL, interval, dut_engine=dut
                    )
                else:
                    interval_result = BaseComponent(
                        parent.platform_stats.cls, path="/" + cat
                    ).set(TelemetryConsts.SAMPLE_INTERVAL, interval, dut_engine=dut)
                if interval_result.result:
                    interval_result.verify_result()
                else:
                    interval_result.ignore_result()
                    logger.warning(
                        "Skipping platform-stats class %s sample-interval=%s",
                        cat,
                        interval,
                    )


def _configure_stats_families(
    system: System,
    dut,
    enabled: Set[str],
    intervals: Dict[str, int],
    *,
    stats_group_id: Optional[str] = None,
    family_universe: Optional[Iterable[str]] = None,
    enable_phy_under_stats_group: bool = False,
) -> None:
    """Enable/disable export + sample intervals (+ class knobs) at root or under a stats-group."""
    if family_universe is not None:
        families = family_universe
    elif stats_group_id:
        families = CumulusOtelConst.STATS_GROUP_SUPPORTED_FAMILIES
    else:
        # Cumulus root NVUE has interface/platform only (no peer-port / ib-router).
        families = CumulusOtelConst.ROOT_STATS_FAMILIES
    for family in families:
        state = _ENABLED if family in enabled else _DISABLED
        _stats_node(system, family, stats_group_id=stats_group_id).export.set(
            TelemetryConsts.STATE, state, dut_engine=dut
        ).verify_result()
        if family in enabled:
            _set_stats_sample_interval(
                system, dut, family, intervals[family], stats_group_id=stats_group_id
            )
            _enable_family_classes(
                system,
                dut,
                family,
                stats_group_id=stats_group_id,
                enable_phy_under_stats_group=enable_phy_under_stats_group,
            )


def _telemetry_subtree(system: System, path: str) -> BaseComponent:
    """``BaseComponent`` under ``system telemetry`` for NVUE paths not modeled in ``Telemetry.py``."""
    return BaseComponent(system.telemetry, path=path)


def apply_staged_candidate(dut, *, verify_execution: bool = True) -> None:
    """Commit the staged candidate on the API the suite is running.

    ``nv config apply`` always commits the NVUE candidate. Sets that went through
    OpenAPI live in a different revision, so a raw apply would commit the wrong
    one (or none). ``TestToolkit.GeneralApi`` dispatches to NVUE or OpenAPI.
    """
    TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config(
        dut, ask_for_confirmation=True, verify_execution=verify_execution
    )


def nvue_auto_confirm() -> str:
    """``-y`` for NVUE commands that prompt. OpenAPI has no confirm flag."""
    return "-y" if TestToolkit.tested_api == ApiType.NVUE else ""


def nvue_quoted(value: str) -> str:
    """Double-quote a multi-word value for NVUE. OpenAPI ``set`` strips the quotes."""
    text = str(value)
    if any(char.isspace() for char in text):
        return f'"{text}"'
    return text


def cli_token_for_api(value: str) -> str:
    """Shell-quote ``value`` on NVUE. OpenAPI receives the raw token."""
    import shlex

    if TestToolkit.tested_api == ApiType.NVUE:
        return shlex.quote(value)
    return value


def stage_telemetry_value(system: System, dut, path: str, name: str, value) -> None:
    """Stage one ``nv set system telemetry <path> <name> <value>`` without applying."""
    _telemetry_subtree(system, path).set(name, value, dut_engine=dut).verify_result()


def stage_interface_histogram_member(dut, iface: str, kind: str, leaf: str, member) -> None:
    """Stage ``nv set interface <iface> telemetry histogram <kind> <leaf> <member>``."""
    BaseComponent(
        Interface(None, port_name=iface),
        path=f"/telemetry/histogram/{kind}/{leaf}",
    ).set(str(member), "", dut_engine=dut).verify_result()


def stage_interface_telemetry_label(dut, iface: str, label_id: str, description: str) -> None:
    """Stage ``nv set interface <iface> telemetry label <id> description <text>``."""
    BaseComponent(
        Interface(None, port_name=iface),
        path=f"/telemetry/label/{label_id}",
    ).set(
        TelemetryConsts.DESCRIPTION, nvue_quoted(description), dut_engine=dut
    ).verify_result()


def _enable_simple_telemetry_export(
    system: System,
    dut,
    subtree: str,
    sample_interval_sec: int,
    *,
    required: bool = True,
) -> bool:
    """Enable ``<subtree>/export state`` and ``<subtree> sample-interval`` (root telemetry tree).

    Returns True when both NVUE sets succeed. When ``required`` is false, logs and skips
  unsupported subtrees (e.g. ``adaptive-routing-stats`` missing from root on some Cumulus builds).
    """
    with allure.step(f"Enable root {subtree} export (interval={sample_interval_sec}s)"):
        interval_result = _telemetry_subtree(system, f"/{subtree}").set(
            TelemetryConsts.SAMPLE_INTERVAL, sample_interval_sec, dut_engine=dut
        )
        export_result = _telemetry_subtree(system, f"/{subtree}/export").set(
            TelemetryConsts.STATE, _ENABLED, dut_engine=dut
        )
        if interval_result.result and export_result.result:
            return True
        if not required:
            interval_result.ignore_result()
            export_result.ignore_result()
            logger.warning(
                "Skipping unsupported root telemetry subtree %s (NVUE rejected set)",
                subtree,
            )
            return False
        interval_result.verify_result()
        export_result.verify_result()
    return True


def _try_disable_telemetry_export(
    dut,
    path: str,
    *,
    context: str,
    node_for_path,
) -> None:
    """Disable export state on a telemetry path when the NVUE subtree exists (best-effort)."""
    result = node_for_path(path).set(
        TelemetryConsts.STATE, _DISABLED, dut_engine=dut
    )
    if result.result:
        result.verify_result()
        return
    result.ignore_result()
    logger.info("Skipping disable %s (not in NVUE model under %s)", path, context)


def _disable_router_telemetry_exports(system: System, dut) -> None:
    """Keep router/BGP/RIB telemetry off (no routing topology in Cumulus lab)."""
    with allure.step("Disable root router telemetry (no BGP/rib export)"):
        for path in ("/router/export", "/router/bgp/export", "/router/rib/export"):
            _try_disable_telemetry_export(
                dut,
                path,
                context="telemetry root",
                node_for_path=lambda p: _telemetry_subtree(system, p),
            )


def _enable_router_telemetry_exports(system: System, dut) -> None:
    """Enable router/BGP/RIB telemetry (``Otel*WithTLSConfig`` secured topology)."""
    with allure.step("Enable root router telemetry export"):
        for path in ("/router/export", "/router/bgp/export", "/router/rib/export"):
            _telemetry_subtree(system, path).set(
                TelemetryConsts.STATE, _ENABLED, dut_engine=dut
            ).verify_result()


def _enable_secured_root_telemetry(
    system: System,
    dut,
    intervals: Dict[str, int],
    *,
    enable_routing: bool = True,
) -> None:
    """Root telemetry for secured OTLP configs (no stats-group)."""
    routing_note = "routing enabled" if enable_routing else "routing disabled (lab)"
    with allure.step(f"Enable secured OTLP root telemetry ({routing_note})"):
        stage_telemetry_master_state(system, dut, is_nvos=False)
        if enable_routing:
            _enable_router_telemetry_exports(system, dut)
        else:
            _disable_router_telemetry_exports(system, dut)
        for subtree in ("lldp", "buffer-stats", "control-plane-stats"):
            _enable_simple_telemetry_export(
                system, dut, subtree, intervals[subtree], required=True
            )
        _enable_simple_telemetry_export(
            system,
            dut,
            "adaptive-routing-stats",
            intervals["adaptive-routing-stats"],
            required=False,
        )
        _enable_histogram_root(system, dut)
        _enable_software_stats_systemd_root(
            system, dut,
            intervals[CumulusOtelConst.SOFTWARE_STATS_SYSTEMD_INTERVAL_KEY]
        )
        enabled_stats = {
            TelemetryConsts.INTERFACE_STATS,
            TelemetryConsts.PLATFORM_STATS,
        }
        _configure_stats_families(
            system,
            dut,
            enabled_stats,
            intervals,
            stats_group_id=None,
            family_universe=CumulusOtelConst.ROOT_STATS_FAMILIES,
        )
        _enable_family_classes(
            system,
            dut,
            TelemetryConsts.INTERFACE_STATS,
            stats_group_id=None,
        )
        _enable_family_classes(
            system,
            dut,
            TelemetryConsts.PLATFORM_STATS,
            stats_group_id=None,
            platform_classes=CumulusOtelConst.ROOT_PLATFORM_CLASSES,
        )
        _enable_interface_stats_root_extras(system, dut)


def apply_otel_secured_telemetry_config(
    dut,
    *,
    export_vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_DEFAULT,
    enable_routing: bool = True,
    enable_interface_histogram: bool = False,
) -> None:
    """Apply secured OTLP telemetry (``OtelDefaultVrfWithTLSConfig`` / ``OtelMgmtVrfWithTLSConfig``).

    Expects TLS OTLP destination already configured on the DUT. Enables histogram,
    platform, interface, buffer, control-plane, software-stats, and lldp. Router/bgp/rib
    exports are optional (disabled on standalone Cumulus lab mgmt VRF tests).
    Per-interface histogram (SSIM spine ``swp`` knobs) is optional for mlx lab.
    """
    intervals = {
        **_DEFAULT_SAMPLE_INTERVALS,
        **CumulusOtelConst.SECURED_ROOT_SAMPLE_INTERVALS,
    }
    system = System()
    with allure.step(f"Apply secured OTLP telemetry (export vrf={export_vrf})"):
        system.telemetry.export.set(
            TelemetryConsts.VRF, export_vrf, dut_engine=dut
        ).verify_result()
        _enable_secured_root_telemetry(
            system, dut, intervals, enable_routing=enable_routing
        )
        if enable_interface_histogram:
            from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
                get_dut_hostname,
            )

            enable_cumulus_lab_interface_histogram(dut, get_dut_hostname(dut))
        # system.telemetry.export.otlp.set(
        #     TelemetryConsts.STATE,
        #     _ENABLED,
        #     apply=True,
        #     ask_for_confirmation=True,
        #     dut_engine=dut,
        # ).verify_result()
        apply_telemetry_configuration(system, dut, is_nvos=False)
        if enable_interface_histogram:
            from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
                ensure_asic_monitor_running,
            )

            # After NVUE apply (same order as mgmt VRF insecure test01).
            ensure_asic_monitor_running(dut, export_vrf)


def _disable_stats_group_router_exports(
    system: System, dut, stats_group_id: str
) -> None:
    """Disable stats-group router export only (NVUE has ``router export``, not ``router/bgp``)."""
    with allure.step(f"Disable stats-group {stats_group_id} router export"):
        _try_disable_telemetry_export(
            dut,
            "/router/export",
            context=f"stats-group {stats_group_id}",
            node_for_path=lambda p: _stats_group_subtree(system, stats_group_id, p),
        )


def _enable_interface_stats_root_extras(system: System, dut) -> None:
    """Root interface-stats: ingress PG, egress TC, switch-priority (not under stats-group)."""
    with allure.step("Enable root interface-stats ingress/egress/switch-priority"):
        pg_parent = _telemetry_subtree(
            system, "/interface-stats/ingress-buffer/priority-group"
        )
        for pg in range(8):
            pg_parent.set(str(pg), "", dut_engine=dut).verify_result()
        tc_parent = _telemetry_subtree(
            system, "/interface-stats/egress-buffer/traffic-class"
        )
        for tc in range(8):
            tc_parent.set(str(tc), "", dut_engine=dut).verify_result()
        sp_parent = _telemetry_subtree(system, "/interface-stats/switch-priority")
        for sp in ("0", "1", "2", "3", "7"):
            sp_parent.set(sp, "", dut_engine=dut).verify_result()


def _enable_histogram_root(system: System, dut) -> None:
    """Root histogram export, buffer sizes, latency/counter (SSIM ``OtelMgmtVrfNoTLSConfig``)."""
    with allure.step("Enable root histogram export"):
        _telemetry_subtree(system, "/histogram/export").set(
            TelemetryConsts.STATE, _ENABLED, dut_engine=dut
        ).verify_result()
        # SSIM enables latency + counter subtrees (empty cue nodes); best-effort on NVUE.
        for subtree in ("latency", "counter"):
            result = _telemetry_subtree(system, f"/histogram/{subtree}").set(
                "", "", dut_engine=dut
            )
            if not result.result:
                result.ignore_result()
                logger.info(
                    "Skipping root histogram/%s (NVUE rejected set)", subtree
                )
        for subtree, size in CumulusOtelConst.TEST01_HISTOGRAM_BUFFER_SIZES.items():
            result = _telemetry_subtree(system, f"/histogram/{subtree}").set(
                "histogram-size", size, dut_engine=dut
            )
            if result.result:
                result.verify_result()
            else:
                result.ignore_result()
                logger.warning(
                    "Skipping histogram %s histogram-size=%s (NVUE rejected set)",
                    subtree,
                    size,
                )


def _enable_stats_group_simple_export(
    system: System,
    dut,
    stats_group_id: str,
    subtree: str,
    sample_interval_sec: int,
) -> None:
    """Enable a simple-export subtree under ``stats-group <id>`` (e.g. ``sg_01``)."""
    with allure.step(
        f"Enable stats-group {stats_group_id} {subtree} export "
        f"(interval={sample_interval_sec}s)"
    ):
        interval_result = _stats_group_subtree(
            system, stats_group_id, f"/{subtree}"
        ).set(TelemetryConsts.SAMPLE_INTERVAL, sample_interval_sec, dut_engine=dut)
        export_result = _stats_group_subtree(
            system, stats_group_id, f"/{subtree}/export"
        ).set(TelemetryConsts.STATE, _ENABLED, dut_engine=dut)
        if interval_result.result and export_result.result:
            return
        interval_result.ignore_result()
        export_result.ignore_result()
        logger.warning(
            "Skipping stats-group %s subtree %s (NVUE rejected set)",
            stats_group_id,
            subtree,
        )


def _try_nvue_set(dut, node, key: str, value, *, context: str, required: bool = False) -> bool:
    """Apply one NVUE ``set``; skip quietly unless ``required``."""
    result = node.set(key, value, dut_engine=dut)
    if result.result:
        result.verify_result()
        return True
    result.ignore_result()
    if required:
        pytest.fail(f"Required NVUE set {key}={value!r} failed under {context}")
    logger.info("Skipping %s=%r under %s (NVUE rejected set)", key, value, context)
    return False


def _enable_stats_group_software_stats_systemd(
    system: System, dut, stats_group_id: str, sample_interval_sec: int
) -> None:
    """``sg_01`` software-stats/systemd — export + interval; unit profile is root-only on many builds."""
    profile = "custom"
    ctx = f"stats-group {stats_group_id} software-stats/systemd"
    with allure.step(f"Enable {ctx}"):
        _try_nvue_set(
            dut,
            _stats_group_subtree(system, stats_group_id, "/software-stats/systemd/export"),
            TelemetryConsts.STATE,
            _ENABLED,
            context=ctx,
            required=True,
        )
        systemd = _stats_group_subtree(system, stats_group_id, "/software-stats/systemd")
        _try_nvue_set(
            dut,
            systemd,
            TelemetryConsts.SAMPLE_INTERVAL,
            sample_interval_sec,
            context=ctx,
        )
        if not _try_nvue_set(dut, systemd, "process-level", _ENABLED, context=ctx):
            return
        unit_parent = _stats_group_subtree(
            system,
            stats_group_id,
            f"/software-stats/systemd/unit-profile/{profile}/unit",
        )
        for unit in CumulusOtelConst.TEST01_SYSTEMD_UNIT_NAMES:
            if not _try_nvue_set(dut, unit_parent, unit, "", context=ctx):
                return
        _try_nvue_set(dut, systemd, "active-profile", profile, context=ctx)


def _enable_software_stats_systemd_root(
    system: System,
    dut,
    sample_interval_sec: int,
    *,
    unit_names: Optional[Iterable[str]] = None,
) -> None:
    """Root software-stats/systemd custom unit profile."""
    profile = "custom"
    units = tuple(unit_names or CumulusOtelConst.TEST01_SYSTEMD_UNIT_NAMES)
    with allure.step("Enable root software-stats systemd export"):
        systemd = _telemetry_subtree(system, "/software-stats/systemd")
        _telemetry_subtree(system, "/software-stats/systemd/export").set(
            TelemetryConsts.STATE, _ENABLED, dut_engine=dut
        ).verify_result()
        systemd.set("process-level", _ENABLED, dut_engine=dut).verify_result()
        systemd.set(
            TelemetryConsts.SAMPLE_INTERVAL, sample_interval_sec, dut_engine=dut
        ).verify_result()
        unit_parent = _telemetry_subtree(
            system, f"/software-stats/systemd/unit-profile/{profile}/unit"
        )
        for unit in units:
            unit_parent.set(unit, "", dut_engine=dut).verify_result()
        systemd.set("active-profile", profile, dut_engine=dut).verify_result()


def configure_stats_group_interface_platform(
    system: System,
    dut,
    stats_group_id: str,
    intervals: Dict[str, int],
    *,
    enable_phy_under_stats_group: bool = False,
) -> None:
    """Enable interface-stats + platform-stats under a stats-group.

    Shared baseline for NVOS OTLP tests and any stats-group export that only
    needs the two native families. Cumulus test01 layers additional subtrees
    (lldp, buffer, control-plane, software-stats/systemd, …) via
    :func:`_configure_test01_stats_group`; NVOS has none of those under
    stats-group and must use this narrower tree instead.

    ``enable_phy_under_stats_group`` enables the interface/peer-port ``class phy``
    knob under the stats-group (NVOS-only; Cumulus NVUE is debounce-only there).
    """
    _telemetry_stats_parent(system, stats_group_id)
    _disable_stats_group_router_exports(system, dut, stats_group_id)
    enabled = set(CumulusOtelConst.STATS_GROUP_SUPPORTED_FAMILIES)
    _configure_stats_families(
        system,
        dut,
        enabled,
        intervals,
        stats_group_id=stats_group_id,
        family_universe=enabled,
        enable_phy_under_stats_group=enable_phy_under_stats_group,
    )
    for family in enabled:
        if family == TelemetryConsts.PLATFORM_STATS:
            _enable_family_classes(
                system,
                dut,
                family,
                stats_group_id=stats_group_id,
                platform_classes=CumulusOtelConst.TEST01_STATS_GROUP_PLATFORM_CLASSES,
                platform_class_intervals=CumulusOtelConst.TEST01_STATS_GROUP_PLATFORM_CLASS_INTERVALS,
            )
        else:
            _enable_family_classes(
                system,
                dut,
                family,
                stats_group_id=stats_group_id,
                enable_phy_under_stats_group=enable_phy_under_stats_group,
            )


def _configure_test01_stats_group(
    system: System,
    dut,
    stats_group_id: str,
    intervals: Dict[str, int],
) -> None:
    """``stats-group sg_01`` telemetry from ``mgmt VRF insecure telemetry`` (router/BGP omitted)."""
    _telemetry_stats_parent(system, stats_group_id)
    _disable_stats_group_router_exports(system, dut, stats_group_id)
    for subtree in CumulusOtelConst.TEST01_STATS_GROUP_SIMPLE_EXPORTS:
        _enable_stats_group_simple_export(
            system, dut, stats_group_id, subtree, intervals[subtree]
        )
    _enable_stats_group_software_stats_systemd(
        system, dut, stats_group_id,
        intervals[CumulusOtelConst.SOFTWARE_STATS_SYSTEMD_INTERVAL_KEY]
    )
    enabled = {
        TelemetryConsts.INTERFACE_STATS,
        TelemetryConsts.PLATFORM_STATS,
    }
    _configure_stats_families(
        system,
        dut,
        enabled,
        intervals,
        stats_group_id=stats_group_id,
        family_universe=enabled,
    )
    for family in enabled:
        if family == TelemetryConsts.PLATFORM_STATS:
            _enable_family_classes(
                system,
                dut,
                family,
                stats_group_id=stats_group_id,
                platform_classes=CumulusOtelConst.TEST01_STATS_GROUP_PLATFORM_CLASSES,
                platform_class_intervals=CumulusOtelConst.TEST01_STATS_GROUP_PLATFORM_CLASS_INTERVALS,
            )
        else:
            # Stats-group interface-stats ``class`` only exposes ``debounce`` on this NVUE
            # (not ``phy``); root enables ``phy`` in ``_enable_test01_root_telemetry``.
            _enable_family_classes(system, dut, family, stats_group_id=stats_group_id)


def _enable_test01_root_telemetry(
    system: System,
    dut,
    intervals: Dict[str, int],
    *,
    systemd_unit_names: Optional[Iterable[str]] = None,
) -> None:
    """Root telemetry from ``mgmt VRF insecure telemetry`` (router/BGP disabled)."""
    with allure.step("Enable mgmt VRF insecure telemetry root telemetry (no router/BGP)"):
        stage_telemetry_master_state(system, dut, is_nvos=False)
        _disable_router_telemetry_exports(system, dut)
        for subtree in CumulusOtelConst.TEST01_ROOT_SIMPLE_EXPORTS:
            _enable_simple_telemetry_export(
                system, dut, subtree, intervals[subtree], required=True
            )
        for subtree in CumulusOtelConst.TEST01_ROOT_OPTIONAL_SIMPLE_EXPORTS:
            if subtree not in intervals:
                continue
            _enable_simple_telemetry_export(
                system, dut, subtree, intervals[subtree], required=False
            )
        _enable_histogram_root(system, dut)
        _enable_software_stats_systemd_root(
            system,
            dut,
            intervals[CumulusOtelConst.SOFTWARE_STATS_SYSTEMD_INTERVAL_KEY],
            unit_names=systemd_unit_names,
        )
        enabled_stats = set(CumulusOtelConst.TEST01_ROOT_STATS_FAMILIES)
        _configure_stats_families(
            system,
            dut,
            enabled_stats,
            intervals,
            stats_group_id=None,
            family_universe=CumulusOtelConst.ROOT_STATS_FAMILIES,
        )
        _enable_family_classes(
            system,
            dut,
            TelemetryConsts.PLATFORM_STATS,
            stats_group_id=None,
            platform_classes=CumulusOtelConst.ROOT_PLATFORM_CLASSES,
            platform_class_intervals=CumulusOtelConst.TEST01_ROOT_PLATFORM_CLASS_INTERVALS,
        )
        _enable_interface_stats_root_extras(system, dut)


def _bind_stats_group_on_otlp_destinations(
    system: System,
    dut,
    collector_ips: Iterable[str],
    stats_group_id: str,
) -> None:
    """Attach ``stats_group_id`` to existing OTLP gRPC destinations (does not create OTLP)."""
    grpc = system.telemetry.export.otlp.grpc
    for collector_ip in collector_ips:
        with allure.step(f"Bind stats-group {stats_group_id} on destination {collector_ip}"):
            if collector_ip not in grpc.destination.resources_dict:
                grpc.destination.set_resource(collector_ip).verify_result()
            grpc.destination.resources_dict[collector_ip].set(
                TelemetryConsts.STATS_GROUP, stats_group_id, dut_engine=dut
            ).verify_result()


def _ensure_otlp_grpc_insecure(system: System, dut) -> None:
    """Re-assert ``otlp grpc insecure enabled`` before enabling the telemetry master state.

    The function-scoped ``clear_config`` fixture resets the DUT to the default YAML after
    every test, dropping the ``insecure`` flag that the module-scoped ``otel_suite_mgmt``
    set up. A later CLI-show apply then fails with ``No certificate found for
    destination(s) ... set the insecure flag to enabled`` whenever the test is not the
    first in the module. Mirrors :func:`apply_otel_mgmt_vrf_no_tls_telemetry_config`.
    """
    with allure.step("Re-assert OTLP gRPC insecure flag (pre master-state apply)"):
        system.telemetry.export.otlp.grpc.set(
            TelemetryConsts.INSECURE, _ENABLED, dut_engine=dut
        ).verify_result()


def _interface_label_pairs() -> List[Tuple[str, str]]:
    labels = CumulusOtelConst.INTF_LABELS
    return [(labels[i], labels[i + 1]) for i in range(0, len(labels), 2)]


def enable_cumulus_lab_interface_histogram(dut, hostname: str) -> None:
    """Enable per-interface histogram on mlx lab swp ports (SSIM spine ``swp`` setup).

    ``histogram-export-service`` / ``asic-monitor@mgmt`` need **interface-level**
    ``nv set interface <swp> telemetry histogram ...``, not only root
    ``nv set system telemetry histogram export state enabled``.

    SSIM ``OtelMgmtVrfNoTLSConfig`` also applies ``mgmtVrfNoTls-l*`` interface labels
    on each spine ``swp``; OTEL attr keys for test02/test03 include those labels.
    """
    ifaces = histogram_interfaces_on_dut(dut, hostname)
    label_pairs = _interface_label_pairs()
    with allure.step(f"Enable interface histogram on {', '.join(ifaces)}"):
        for iface in ifaces:
            for pg in range(8):
                stage_interface_histogram_member(
                    dut, iface, "ingress-buffer", "priority-group", pg
                )
            for tc in range(8):
                stage_interface_histogram_member(
                    dut, iface, "egress-buffer", "traffic-class", tc
                )
                stage_interface_histogram_member(
                    dut, iface, "latency", "traffic-class", tc
                )
            for counter_type in ("rx-byte", "crc"):
                stage_interface_histogram_member(
                    dut, iface, "counter", "counter-type", counter_type
                )
            for label_id, description in label_pairs:
                stage_interface_telemetry_label(dut, iface, label_id, description)


def apply_otel_mgmt_vrf_no_tls_telemetry_config(
    dut,
    collector_ips: Iterable[str],
    *,
    stats_group_id: str = CumulusOtelConst.TEST01_STATS_GROUP_ID,
    enable_interface_histogram: bool = True,
) -> None:
    """Apply mgmt VRF insecure OTLP telemetry (no routing/BGP/leaf topology).

    Expects OTLP export (mgmt VRF, dual destinations, insecure) from :func:`setup_otel_suite`.
    Configures ``stats-group sg_01`` and root telemetry for Cumulus lab tests;
    router/BGP/RIB exports are left disabled.

    Set ``enable_interface_histogram=False`` when the caller already configured
    per-interface histogram (SSIM ``OtelMgmtVrfNoTLSConfig`` spine ``swp`` loop).
    """
    root_intervals = {
        **_DEFAULT_SAMPLE_INTERVALS,
        **CumulusOtelConst.TEST01_ROOT_SAMPLE_INTERVALS,
    }
    sg_intervals = {
        **_DEFAULT_SAMPLE_INTERVALS,
        **CumulusOtelConst.TEST01_STATS_GROUP_SAMPLE_INTERVALS,
    }
    system = System()
    with allure.step(
        f"Apply mgmt VRF insecure telemetry telemetry (stats-group={stats_group_id})"
    ):
        _bind_stats_group_on_otlp_destinations(
            system, dut, collector_ips, stats_group_id
        )
        _configure_test01_stats_group(system, dut, stats_group_id, sg_intervals)
        _enable_test01_root_telemetry(
            system,
            dut,
            root_intervals,
            systemd_unit_names=CumulusOtelConst.TEST01_SYSTEMD_UNIT_NAMES_MGMT_VRF,
        )
        if enable_interface_histogram:
            from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
                get_dut_hostname,
            )

            enable_cumulus_lab_interface_histogram(dut, get_dut_hostname(dut))
        system.telemetry.export.otlp.grpc.set(
            TelemetryConsts.INSECURE, _ENABLED, dut_engine=dut
        ).verify_result()
        # system.telemetry.export.otlp.set(
        #     TelemetryConsts.STATE,
        #     _ENABLED,
        #     apply=True,
        #     ask_for_confirmation=True,
        #     dut_engine=dut,
        # ).verify_result()
        apply_telemetry_configuration(system, dut, is_nvos=False)
        from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
            ensure_asic_monitor_running,
        )

        ensure_asic_monitor_running(dut, CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT)


def apply_transceiver_info_show_telemetry_config(
    dut,
    collector_ips: Iterable[str],
    *,
    stats_group_id: str = CumulusOtelConst.TEST01_STATS_GROUP_ID,
    is_nvos: bool = False,
) -> None:
    """Root + stats-group ``transceiver-info`` knobs for SSIM ``otel_show`` (NVOS + Cumulus).

    Expects OTLP dual-destination export from :func:`setup_otel_suite`. Binds
    ``stats_group_id`` on each gRPC destination and applies the same platform-stats
    class intervals Cumulus test01 uses for ``transceiver-info`` (62s) at root and
    under the stats-group.
    """
    sg_intervals = {
        **_DEFAULT_SAMPLE_INTERVALS,
        **CumulusOtelConst.TEST01_STATS_GROUP_SAMPLE_INTERVALS,
    }
    root_intervals = {
        **_DEFAULT_SAMPLE_INTERVALS,
        **CumulusOtelConst.TEST01_ROOT_SAMPLE_INTERVALS,
    }
    enabled_sg = {
        TelemetryConsts.INTERFACE_STATS,
        TelemetryConsts.PLATFORM_STATS,
    }
    system = System()
    with allure.step(
        f"Configure transceiver-info show telemetry (stats-group={stats_group_id})"
    ):
        _bind_stats_group_on_otlp_destinations(
            system, dut, collector_ips, stats_group_id
        )
        _telemetry_stats_parent(system, stats_group_id)
        _disable_stats_group_router_exports(system, dut, stats_group_id)
        _configure_stats_families(
            system,
            dut,
            enabled_sg,
            sg_intervals,
            stats_group_id=stats_group_id,
            family_universe=enabled_sg,
        )
        _enable_family_classes(
            system,
            dut,
            TelemetryConsts.PLATFORM_STATS,
            stats_group_id=stats_group_id,
            platform_classes=CumulusOtelConst.TEST01_STATS_GROUP_PLATFORM_CLASSES,
            platform_class_intervals=CumulusOtelConst.TEST01_STATS_GROUP_PLATFORM_CLASS_INTERVALS,
        )
        if not is_nvos:
            stage_telemetry_master_state(system, dut, is_nvos=False)
        _disable_router_telemetry_exports(system, dut)
        _configure_stats_families(
            system,
            dut,
            {TelemetryConsts.PLATFORM_STATS},
            root_intervals,
            stats_group_id=None,
            family_universe={TelemetryConsts.PLATFORM_STATS},
        )
        _enable_family_classes(
            system,
            dut,
            TelemetryConsts.PLATFORM_STATS,
            stats_group_id=None,
            platform_classes=CumulusOtelConst.ROOT_PLATFORM_CLASSES,
            platform_class_intervals=CumulusOtelConst.TEST01_ROOT_PLATFORM_CLASS_INTERVALS,
        )
        if not is_nvos:
            _ensure_otlp_grpc_insecure(system, dut)
        apply_telemetry_configuration(system, dut, is_nvos=is_nvos)


def dut_root_on_nvme_storage(dut) -> bool:
    """True when root filesystem is on NVMe (ATA disk metrics not exported)."""
    root_fs = dut.run_cmd(
        "findmnt -n -o SOURCE / 2>/dev/null || df / | awk 'NR==2{print $1}'",
        validate=False,
        print_output=False,
    ).strip()
    return "nvme" in root_fs.lower()


def secured_exclude_metrics(dut, *, no_routing: bool = False) -> List[str]:
    """Cumulus secured-collection metric exclusions (mgmt VRF TLS / coexistence smoke)."""
    exclude = list(CumulusOtelConst.SECURED_EXCLUDE_METRICS)
    if no_routing:
        exclude.extend(CumulusOtelConst.SECURED_NO_ROUTING_EXCLUDE_METRICS)
        exclude.extend(CumulusOtelConst.SECURED_MGMT_CP_TRAP_GROUP_EXCLUDE_METRICS)
        exclude.extend(CumulusOtelConst.SECURED_MGMT_LAB_TOPOLOGY_EXCLUDE_METRICS)
        from ngts.tests_nvos.system.telemetry.otel.cumulus.metric_catalog import (
            supported_metrics_parity as metrics_parity,
        )

        exclude.extend(metrics_parity.ALLOWED_EXTRAS)
    if dut_root_on_nvme_storage(dut):
        exclude.extend(CumulusOtelConst.TEST01_EXCLUDE_METRICS_NVME)
    return exclude


def test01_expected_metric_names_from_catalog(
    catalog: Dict[str, List[str]],
    *,
    exclude_metrics: Optional[Iterable[str]] = None,
) -> Set[str]:
    """Metric names from the catalog that test01 NVUE config can export (by group prefix)."""
    ignore = set(exclude_metrics or ())
    expected: Set[str] = set()
    for name in flatten_supported_metrics(catalog, skip_buckets=()):
        if name in ignore:
            continue
        if _is_prometheus_sidecar_metric(name):
            expected.add(name)
            continue
        if any(
            _metric_name_matches_group_prefix(name, prefix)
            for prefix in CumulusOtelConst.TEST01_METRICS_GROUP_PREFIXES
        ):
            expected.add(name)
    return expected


def validate_test01_collected_metrics(
    metrics_timestamps: MetricTimestamps,
    *,
    catalog_flat: Set[str],
    exclude_metrics: Optional[List[str]] = None,
    group_prefixes: Optional[Tuple[str, ...]] = None,
) -> None:
    """test01: export must be catalogued; core telemetry families must appear."""
    prefixes = group_prefixes or CumulusOtelConst.TEST01_METRICS_GROUP_PREFIXES
    exclude_set = set(exclude_metrics or ())
    collected = {name for name in metrics_timestamps if name not in exclude_set}
    if not collected:
        pytest.fail("No metrics_timestamps data collected.")

    unknown = sorted(name for name in collected if name not in catalog_flat)
    if unknown:
        pytest.fail(
            f"test01: {len(unknown)} collected metrics are not in supported_metrics "
            f"catalog: {unknown[:40]}{' ...' if len(unknown) > 40 else ''}"
        )

    required_families = (
        TelemetryConsts.INTERFACE_STATS,
        TelemetryConsts.PLATFORM_STATS,
    )
    for prefix in required_families:
        matched = [
            name for name in collected if _metric_name_matches_group_prefix(name, prefix)
        ]
        if not matched:
            pytest.fail(f"test01: no collected metrics for required family {prefix}")

    prometheus = [name for name in collected if _is_prometheus_sidecar_metric(name)]
    if not prometheus:
        pytest.fail("test01: no prometheus sidecar metrics (node_*/scrape_*) in export")

    for prefix in prefixes:
        if prefix in required_families:
            continue
        matched = [
            name for name in collected if _metric_name_matches_group_prefix(name, prefix)
        ]
        if not matched:
            logger.warning(
                "test01: no collected metrics matching family prefix %s (optional on Cumulus)",
                prefix,
            )

    logger.info(
        "test01 validation passed: collected=%d (catalogued, core families + prometheus present)",
        len(collected),
    )


def _parse_platform_show(show_output: str) -> Dict[str, Any]:
    parsed = OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()
    return parsed if isinstance(parsed, dict) else {}


def get_plat_env_temp_stats_with_cli(dut) -> Dict[str, Any]:
    """Platform environment temperature snapshot for test05 validation."""
    with allure.step("CLI: platform environment temperature"):
        logger.info("Getting environment temperature stats via Platform().environment.temperature")
        platform = Platform()
        raw = platform.environment.temperature.show(dut_engine=dut)
        return _parse_platform_show(raw)


def get_plat_env_fan_stats_with_cli(dut) -> Dict[str, Any]:
    """Platform environment fan snapshot for test05 validation."""
    with allure.step("CLI: platform environment fan"):
        logger.info("Getting environment fan stats via Platform().environment.fan")
        platform = Platform()
        raw = platform.environment.fan.show(dut_engine=dut)
        return _parse_platform_show(raw)


def get_plat_env_psu_stats_with_cli(dut) -> Dict[str, Any]:
    """Platform environment PSU snapshot for test05 validation."""
    with allure.step("CLI: platform environment psu"):
        logger.info("Getting environment psu stats via Platform().environment.psu")
        platform = Platform()
        raw = platform.environment.psu.show(dut_engine=dut)
        return _parse_platform_show(raw)


def collect_cli_platform_environment_stats(dut) -> Dict[str, Any]:
    """CLI payloads stored under telemetry cache key ``cli`` for test05."""
    return {
        "plat_env_temp": get_plat_env_temp_stats_with_cli(dut),
        "plat_env_fan": get_plat_env_fan_stats_with_cli(dut),
        "plat_env_psu": get_plat_env_psu_stats_with_cli(dut),
    }


def _parse_telemetry_show(raw: str) -> Dict[str, Any]:
    parsed = OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()
    return parsed if isinstance(parsed, dict) else {}


def show_platform_stats_class_transceiver_info(
    dut,
    *,
    stats_group_id: Optional[str] = None,
) -> Dict[str, Any]:
    """``nv show … platform-stats class transceiver-info --applied``."""
    system = System()
    transceiver_cat = TelemetryConsts.PLATFORM_CLASS_TRANSCEIVER_INFO
    if stats_group_id:
        sg = system.telemetry.stats_group.resources_dict.get(stats_group_id)
        if sg is None:
            system.telemetry.stats_group.set_resource(stats_group_id).verify_result()
            sg = system.telemetry.stats_group.resources_dict[stats_group_id]
        node = sg.platform_stats.cls.categories[transceiver_cat]
        ctx = f"stats-group {stats_group_id}"
    else:
        node = system.telemetry.platform_stats.cls.categories[transceiver_cat]
        ctx = "telemetry root"
    with allure.step(f"nv show {ctx} platform-stats class transceiver-info --applied"):
        raw = node.show(rev=ConfState.APPLIED, dut_engine=dut)
        return _parse_telemetry_show(raw)


def validate_transceiver_info_applied(
    output: Dict[str, Any],
    *,
    context: str,
    expected_interval: int = CumulusOtelConst.TEST01_STATS_GROUP_PLATFORM_CLASS_INTERVALS[
        TelemetryConsts.PLATFORM_CLASS_TRANSCEIVER_INFO
    ],
) -> None:
    """Assert transceiver-info platform-stats class matches SSIM ``otel_show`` expectations."""
    assert output.get("state") == _ENABLED, (
        f"{context}: state expected enabled, got {output!r}"
    )
    assert output.get("sample-interval") == expected_interval, (
        f"{context}: sample-interval expected {expected_interval}, got {output!r}"
    )
    allure.attach(context, str(output))
    logger.info("%s: %s", context, output)


def apply_simple_export_show_telemetry_config(
    dut,
    collector_ips: Iterable[str],
    *,
    subtree: str,
    root_sample_interval: int,
    stats_group_sample_interval: int,
    stats_group_id: str = CumulusOtelConst.TEST01_STATS_GROUP_ID,
    is_nvos: bool = False,
) -> None:
    """Root + stats-group simple-export knobs for SSIM ``otel_show`` show tests.

    Used for the single-``export``/``sample-interval`` families (e.g. ``lldp``,
    ``adaptive-routing-stats``). Expects OTLP dual-destination export from
    :func:`setup_otel_suite`. Binds ``stats_group_id`` on each gRPC destination, then
    enables ``<subtree> export state`` + ``<subtree> sample-interval`` at the telemetry
    root (``root_sample_interval``) and under ``stats-group <id>``
    (``stats_group_sample_interval``) so the ``--applied`` show output can be validated
    (config -> show parity).
    """
    system = System()
    with allure.step(
        f"Configure {subtree} show telemetry (stats-group={stats_group_id})"
    ):
        _bind_stats_group_on_otlp_destinations(
            system, dut, collector_ips, stats_group_id
        )
        _disable_stats_group_router_exports(system, dut, stats_group_id)
        _enable_stats_group_simple_export(
            system, dut, stats_group_id, subtree, stats_group_sample_interval
        )
        if not is_nvos:
            stage_telemetry_master_state(system, dut, is_nvos=False)
        _disable_router_telemetry_exports(system, dut)
        _enable_simple_telemetry_export(
            system, dut, subtree, root_sample_interval, required=True
        )
        if not is_nvos:
            _ensure_otlp_grpc_insecure(system, dut)
        apply_telemetry_configuration(system, dut, is_nvos=is_nvos)


def show_system_telemetry_simple_export(
    dut,
    subtree: str,
    *,
    export_node: bool = False,
    stats_group_id: Optional[str] = None,
) -> Dict[str, Any]:
    """``nv show system telemetry [stats-group <id>] <subtree>[ export] --applied``."""
    system = System()
    path = f"/{subtree}/export" if export_node else f"/{subtree}"
    if stats_group_id:
        node = _stats_group_subtree(system, stats_group_id, path)
        ctx = f"stats-group {stats_group_id} {subtree}"
    else:
        node = _telemetry_subtree(system, path)
        ctx = f"telemetry root {subtree}"
    if export_node:
        ctx += " export"
    with allure.step(f"nv show {ctx} --applied"):
        raw = node.show(rev=ConfState.APPLIED, dut_engine=dut)
        return _parse_telemetry_show(raw)


def validate_simple_export_applied(
    output: Dict[str, Any],
    *,
    context: str,
    expected_interval: Optional[int] = None,
    export_node: bool = False,
) -> None:
    """Assert a simple-export subtree show matches SSIM ``otel_show`` expectations.

    ``export_node`` validates the ``<subtree> export`` leaf (``state`` only); otherwise
    the ``<subtree>`` node (``export.state`` + ``sample-interval``).
    """
    if export_node:
        assert output.get("state") == _ENABLED, (
            f"{context}: state expected enabled, got {output!r}"
        )
    else:
        assert output.get("export", {}).get("state") == _ENABLED, (
            f"{context}: export state expected enabled, got {output!r}"
        )
        assert output.get("sample-interval") == expected_interval, (
            f"{context}: sample-interval expected {expected_interval}, got {output!r}"
        )
    allure.attach(context, str(output))
    logger.info("%s: %s", context, output)


def apply_software_stats_show_telemetry_config(
    dut,
    collector_ips: Iterable[str],
    *,
    stats_group_id: str = CumulusOtelConst.TEST01_STATS_GROUP_ID,
    is_nvos: bool = False,
) -> None:
    """Root + stats-group ``software-stats/systemd`` knobs for SSIM ``software_stats`` show.

    Expects OTLP dual-destination export from :func:`setup_otel_suite`. Binds
    ``stats_group_id`` on each gRPC destination, then enables the ``custom`` systemd
    unit profile (``process-level``/``export`` enabled, ``sample-interval`` 60) at the
    telemetry root and under ``stats-group <id>`` so the ``--applied`` show output can
    be validated (config → show parity).
    """
    root_intervals = {
        **_DEFAULT_SAMPLE_INTERVALS,
        **CumulusOtelConst.TEST01_ROOT_SAMPLE_INTERVALS,
    }
    sg_intervals = {
        **_DEFAULT_SAMPLE_INTERVALS,
        **CumulusOtelConst.TEST01_STATS_GROUP_SAMPLE_INTERVALS,
    }
    system = System()
    with allure.step(
        f"Configure software-stats show telemetry (stats-group={stats_group_id})"
    ):
        _bind_stats_group_on_otlp_destinations(
            system, dut, collector_ips, stats_group_id
        )
        _enable_stats_group_software_stats_systemd(
            system, dut, stats_group_id,
            sg_intervals[CumulusOtelConst.SOFTWARE_STATS_SYSTEMD_INTERVAL_KEY]
        )
        stage_telemetry_master_state(system, dut, is_nvos=is_nvos)
        _enable_software_stats_systemd_root(
            system, dut,
            root_intervals[CumulusOtelConst.SOFTWARE_STATS_SYSTEMD_INTERVAL_KEY]
        )
        if not is_nvos:
            _ensure_otlp_grpc_insecure(system, dut)
        apply_telemetry_configuration(system, dut, is_nvos=is_nvos)


def show_system_telemetry_software_stats(dut) -> Dict[str, Any]:
    """``nv show system telemetry software-stats --applied`` (telemetry root)."""
    system = System()
    with allure.step("nv show system telemetry software-stats --applied"):
        software_stats = system.telemetry.software_stats.show(
            rev=ConfState.APPLIED, dut_engine=dut
        )
        return _parse_software_stats_show(software_stats)


def _parse_software_stats_show(show_output: str) -> Dict[str, Any]:
    return OutputParsingTool.parse_json_str_to_dictionary(show_output).get_returned_value()


def validate_software_stats_applied(
    dut,
    *,
    stats_group_id: str = CumulusOtelConst.TEST01_STATS_GROUP_ID,
    expected_units: Optional[Iterable[str]] = None,
    expected_sample_interval: Optional[int] = None,
) -> None:
    """Assert applied ``software-stats`` show output matches the configured systemd profile.

    Mirrors SSIM ``test_nv_show_system_telemetry_software_stats_validation``: validates
    root ``software-stats`` (+ ``systemd``/``export``/``unit-profile`` subtrees) and the
    same subtree under ``stats-group <id>``. The ``custom`` unit profile is checked
    exactly against the configured units; the built-in ``default`` profile is only
    checked for presence (its unit list is platform/build specific).
    """
    expected_units = sorted(expected_units or CumulusOtelConst.TEST01_SYSTEMD_UNIT_NAMES)
    if expected_sample_interval is None:
        expected_sample_interval = CumulusOtelConst.TEST01_ROOT_SAMPLE_INTERVALS[
            CumulusOtelConst.SOFTWARE_STATS_SYSTEMD_INTERVAL_KEY
        ]
    systemd_keys = list(CumulusOtelConst.SOFTWARE_STATS_SYSTEMD_KEYS)
    expected_profiles = sorted(
        [CumulusOtelConst.UNIT_PROFILE_CUSTOM, CumulusOtelConst.UNIT_PROFILE_DEFAULT]
    )
    system = System()

    with allure.step("Validate root software-stats show output"):
        software_stats = show_system_telemetry_software_stats(dut)
        systemd = software_stats[CumulusOtelConst.SYSTEMD]
        assert systemd[CumulusOtelConst.ACTIVE_PROFILE] == \
            CumulusOtelConst.UNIT_PROFILE_CUSTOM, (
            f"software-stats active-profile expected custom, "
            f"got {systemd[CumulusOtelConst.ACTIVE_PROFILE]}"
        )
        assert systemd[TelemetryConsts.EXPORT][TelemetryConsts.STATE] == _ENABLED, (
            f"software-stats systemd export state expected enabled, "
            f"got {systemd[TelemetryConsts.EXPORT][TelemetryConsts.STATE]}"
        )
        assert systemd[CumulusOtelConst.PROCESS_LEVEL] == _ENABLED, (
            f"software-stats process-level expected enabled, "
            f"got {systemd[CumulusOtelConst.PROCESS_LEVEL]}"
        )
        assert systemd[TelemetryConsts.SAMPLE_INTERVAL] == expected_sample_interval, (
            f"software-stats sample-interval expected {expected_sample_interval}, "
            f"got {systemd[TelemetryConsts.SAMPLE_INTERVAL]}"
        )
        custom_units = (
            systemd[CumulusOtelConst.UNIT_PROFILE]
            [CumulusOtelConst.UNIT_PROFILE_CUSTOM][CumulusOtelConst.UNIT]
        )
        assert sorted(custom_units.keys()) == expected_units, (
            f"software-stats custom unit-profile units expected {expected_units}, "
            f"got {sorted(custom_units.keys())}"
        )
        logger.info("nv show system telemetry software-stats: %s", software_stats)
        allure.attach(
            "nv show system telemetry software-stats", str(software_stats)
        )

    with allure.step("Validate software-stats systemd show output"):
        systemd_show = system.telemetry.software_stats.systemd.show(
            rev=ConfState.APPLIED, dut_engine=dut
        )
        systemd = _parse_software_stats_show(systemd_show)
        assert systemd[CumulusOtelConst.ACTIVE_PROFILE] == \
            CumulusOtelConst.UNIT_PROFILE_CUSTOM, (
            f"software-stats active-profile expected custom, "
            f"got {systemd[CumulusOtelConst.ACTIVE_PROFILE]}"
        )
        logger.info("nv show system telemetry software-stats systemd: %s", systemd)
        allure.attach(
            "nv show system telemetry software-stats systemd", str(systemd)
        )

    with allure.step("Validate software-stats systemd export show output"):
        export_show = system.telemetry.software_stats.systemd.export.show(
            rev=ConfState.APPLIED, dut_engine=dut
        )
        export_out = _parse_software_stats_show(export_show)
        assert export_out[TelemetryConsts.STATE] == _ENABLED, (
            f"software-stats systemd export state expected enabled, "
            f"got {export_out[TelemetryConsts.STATE]}"
        )
        logger.info(
            "nv show system telemetry software-stats systemd export: %s", export_out
        )
        allure.attach(
            "nv show system telemetry software-stats systemd export", str(export_out)
        )

    with allure.step("Validate software-stats systemd unit-profile show output"):
        unit_profile_show = system.telemetry.software_stats.systemd.unit_profile.show(
            rev=ConfState.APPLIED, dut_engine=dut
        )
        custom_out = _parse_software_stats_show(unit_profile_show)
        assert sorted(custom_out.keys()) == expected_profiles, (
            f"software-stats systemd unit-profile keys expected {expected_profiles}, "
            f"got {custom_out.keys()}"
        )
        custom_units = custom_out[CumulusOtelConst.UNIT_PROFILE_CUSTOM]
        assert sorted(custom_units[CumulusOtelConst.UNIT].keys()) == expected_units, (
            f"software-stats systemd unit-profile custom unit expected "
            f"{expected_units}, got {custom_units[CumulusOtelConst.UNIT]}"
        )
        logger.info(
            "nv show system telemetry software-stats systemd unit-profile: %s",
            custom_out,
        )
        allure.attach(
            "nv show system telemetry software-stats systemd unit-profile",
            str(custom_out),
        )

    with allure.step("Validate software-stats systemd unit-profile custom show output"):
        custom_profile = system.telemetry.software_stats.systemd.unit_profile \
            ._create_resource(CumulusOtelConst.UNIT_PROFILE_CUSTOM)
        unit_profile = custom_profile.show(rev=ConfState.APPLIED, dut_engine=dut)
        unit_profile_out = _parse_software_stats_show(unit_profile)
        assert sorted(unit_profile_out.keys()) == ['unit'], (
            f"software-stats systemd unit-profile keys expected ['unit'], "
            f"got {sorted(unit_profile_out.keys())}"
        )
        logger.info(
            "nv show system telemetry software-stats systemd unit-profile custom: %s",
            unit_profile_out,
        )
        allure.attach(
            "nv show system telemetry software-stats systemd unit-profile custom",
            str(unit_profile_out),
        )

        unit_out = custom_profile.unit.show(rev=ConfState.APPLIED, dut_engine=dut)
        unit_out = _parse_software_stats_show(unit_out)
        assert sorted(unit_out) == expected_units, (
            f"software-stats systemd unit-profile unit keys expected "
            f"{expected_units}, got {sorted(unit_out.keys())}"
        )
        logger.info(
            "nv show system telemetry software-stats systemd unit-profile "
            "custom unit: %s",
            unit_out,
        )
        allure.attach(
            "nv show system telemetry software-stats systemd unit-profile custom unit",
            str(unit_out),
        )

    with allure.step(
        f"Validate stats-group {stats_group_id} software-stats show output"
    ):
        sg = system.telemetry.stats_group.resources_dict.get(stats_group_id)
        if sg is None:
            system.telemetry.stats_group.set_resource(stats_group_id).verify_result()
            sg = system.telemetry.stats_group.resources_dict[stats_group_id]

        sg_software_stats = sg.software_stats
        sg_out = _parse_software_stats_show(
            sg_software_stats.show(rev=ConfState.APPLIED, dut_engine=dut)
        )
        assert sorted(sg_out.keys()) == [CumulusOtelConst.SYSTEMD], (
            f"stats-group {stats_group_id} software-stats systemd keys expected "
            f"['systemd'], got {sorted(sg_out.keys())}"
        )
        logger.info(
            "stats-group %s software-stats: %s", stats_group_id, sg_out
        )
        allure.attach(
            f"stats-group {stats_group_id} software-stats", str(sg_out)
        )

        sg_out_systemd = _parse_software_stats_show(
            sg_software_stats.systemd.show(rev=ConfState.APPLIED, dut_engine=dut)
        )
        assert sorted(sg_out_systemd.keys()) == sorted(systemd_keys), (
            f"stats-group {stats_group_id} software-stats systemd keys expected "
            f"{systemd_keys}, got {sorted(sg_out_systemd.keys())}"
        )
        allure.attach(
            f"stats-group {stats_group_id} software-stats systemd",
            str(sg_out_systemd),
        )

        sg_export_out = _parse_software_stats_show(
            sg_software_stats.systemd.export.show(
                rev=ConfState.APPLIED, dut_engine=dut
            )
        )
        assert sg_export_out[TelemetryConsts.STATE] == _ENABLED, (
            f"stats-group {stats_group_id} software-stats systemd export state "
            f"expected enabled, got {sg_export_out[TelemetryConsts.STATE]}"
        )
        allure.attach(
            f"stats-group {stats_group_id} software-stats systemd export",
            str(sg_export_out),
        )
        logger.info(
            "software-stats show validation passed (stats-group=%s)", stats_group_id
        )
