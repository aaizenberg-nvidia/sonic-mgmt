"""NV unset CLI coverage baseline and helpers (SSIM ``Test_Otel_Mgmt_Vrf_Insecure_CLI_Coverage``)."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_constants.constants_nvos import ConfState, TelemetryConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port, PortRequirements
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_telemetry import list_swp_interfaces
from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import (
    apply_otel_mgmt_vrf_no_tls_telemetry_config,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.lab_topology import (
    discover_swp_interfaces_on_dut,
)

logger = logging.getLogger(__name__)


def get_cumulus_version_tuple(dut) -> Tuple[int, int]:
    """Parse ``product-release`` from ``nv show system version``."""
    parsed = System().version.parse_show(dut_engine=dut)
    if not isinstance(parsed, dict):
        return 0, 0
    release = parsed.get("product-release") or parsed.get("version") or ""
    match = re.match(r"(\d+)\.(\d+)", str(release))
    if match:
        return int(match.group(1)), int(match.group(2))
    return 0, 0


def require_cumulus_at_least(
    dut, min_version: str, max_version: Optional[str] = None
) -> None:
    """Skip when DUT Cumulus version is outside ``[min_version, max_version]``.

    ``max_version`` is optional; omit it for a lower-bound-only gate (SSIM
    ``check_version``). No-op on non-Cumulus platforms: NVOS reports no Cumulus
    ``product-release`` so the version parses to ``(0, 0)`` and the gate doesn't apply.
    """
    current = get_cumulus_version_tuple(dut)
    if current == (0, 0):
        return
    minimum = tuple(int(part) for part in min_version.split(".")[:2])
    if current < minimum:
        pytest.skip(
            f"Cumulus {current[0]}.{current[1]} < required {min_version} for this unset test"
        )
    if max_version is not None:
        maximum = tuple(int(part) for part in max_version.split(".")[:2])
        if current > maximum:
            pytest.skip(
                f"Cumulus {current[0]}.{current[1]} > {max_version} for this unset test"
            )


def require_cumulus_release_family(dut, release_family: str) -> None:
    """Skip unless the DUT Cumulus release family equals ``release_family``.

    Gating is done at ``(major, minor)`` granularity only — the patch component is not
    considered (:func:`get_cumulus_version_tuple` parses only major/minor). ``release_family``
    should be a ``"major.minor"`` string (e.g. ``"5.13"``); any extra components are
    ignored, so ``"5.13.0"`` is treated identically to ``"5.13"``.

    Mirrors SSIM ``VersionCheckType.CUMULUS: ['==5.13.0']`` gating for tests that only
    apply to a single release, but note this deliberately does NOT distinguish patch
    builds (5.13.0 vs 5.13.1) — it gates on the whole 5.13 family.

    No-op on non-Cumulus platforms: NVOS reports no Cumulus ``product-release`` so the
    version parses to ``(0, 0)`` and the gate doesn't apply.
    """
    current = get_cumulus_version_tuple(dut)
    if current == (0, 0):
        return
    required = tuple(int(part) for part in release_family.split(".")[:2])
    if current != required:
        pytest.skip(
            f"Cumulus {current[0]}.{current[1]} not in required "
            f"{required[0]}.{required[1]} release family for this test"
        )


def require_cumulus_equal(dut, version: str) -> None:
    """Skip when DUT Cumulus version is not exactly ``version`` (SSIM ``==``).

    No-op on non-Cumulus platforms (same as :func:`require_cumulus_at_least`).
    """
    current = get_cumulus_version_tuple(dut)
    if current == (0, 0):
        return
    required = tuple(int(part) for part in version.split(".")[:2])
    if current != required:
        pytest.skip(
            f"Cumulus {current[0]}.{current[1]} != required {version} for this unset test"
        )


def _nv_apply(dut) -> None:
    dut.run_cmd("nv config apply -y", validate=True)


def _system_telemetry_node(path: str) -> BaseComponent:
    return BaseComponent(System().telemetry, path=path)


def _iface_telemetry_node(iface: str, path: str) -> BaseComponent:
    return BaseComponent(Interface(None, port_name=iface), path=f'/telemetry{path}')


def _telemetry_applied_show(dut, path: str) -> Dict[str, Any]:
    parsed = _system_telemetry_node(path).parse_show(
        rev=ConfState.APPLIED, dut_engine=dut
    )
    return parsed if isinstance(parsed, dict) else {}


def _iface_telemetry_applied_show(dut, iface: str, path: str) -> Dict[str, Any]:
    parsed = _iface_telemetry_node(iface, path).parse_show(
        rev=ConfState.APPLIED, dut_engine=dut
    )
    return parsed if isinstance(parsed, dict) else {}


def _interface_label_pairs() -> List[Tuple[str, str]]:
    labels = CumulusOtelConst.INTF_LABELS
    return [(labels[i], labels[i + 1]) for i in range(0, len(labels), 2)]


def _nvos_ib_interfaces_for_unset(dut) -> List[str]:
    """Return NVOS IB front-panel ports using the NVOS port inventory API."""
    reqs = PortRequirements()
    reqs.set_port_type(IbInterfaceConsts.IB_PORT_TYPE)
    return sorted(port.name for port in Port.get_list_of_ports(dut, reqs))


def _configure_interface_telemetry_cli_baseline(dut, *, is_nvos: bool = False) -> None:
    """SSIM ``OtelMgmtVrfNoTLSConfig.configure_topo`` spine ``swp`` loop (lines 62-128).

    Per interface: full histogram (PG/TC 0-7, rx-byte + crc) then labels l1-l10.
    NVOS has no per-interface histogram, so it configures labels only.
    """
    ifaces = swp_interfaces_for_unset(dut, is_nvos=is_nvos)
    label_pairs = _interface_label_pairs()
    with allure.step(
        f"Configure interface {'labels' if is_nvos else 'histogram + labels'} "
        f"on {len(ifaces)} front-panel port(s) (SSIM spine swp loop)"
    ):
        for iface in ifaces:
            if not is_nvos:
                for pg in range(8):
                    dut.run_cmd(
                        f"nv set interface {iface} telemetry histogram "
                        f"ingress-buffer priority-group {pg}",
                        validate=True,
                    )
                for tc in range(8):
                    dut.run_cmd(
                        f"nv set interface {iface} telemetry histogram "
                        f"egress-buffer traffic-class {tc}",
                        validate=True,
                    )
                    dut.run_cmd(
                        f"nv set interface {iface} telemetry histogram "
                        f"latency traffic-class {tc}",
                        validate=True,
                    )
                for counter_type in ("rx-byte", "crc"):
                    dut.run_cmd(
                        f"nv set interface {iface} telemetry histogram "
                        f"counter counter-type {counter_type}",
                        validate=True,
                    )
            for label_id, description in label_pairs:
                dut.run_cmd(
                    f"nv set interface {iface} telemetry label {label_id} "
                    f'description "{description}"',
                    validate=True,
                )
        _nv_apply(dut)


def _set_device_labels(dut) -> None:
    """SSIM ``OtelMgmtVrfNoTLSConfig`` system telemetry device labels (lines 339-359)."""
    with allure.step("Configure system telemetry device labels (CLI coverage)"):
        for label_id, description in CumulusOtelConst.DEVICE_LABELS:
            dut.run_cmd(
                f"nv set system telemetry label {label_id} "
                f'description "{description}"',
                validate=True,
            )
        _nv_apply(dut)


def cleanup_otel_cli_coverage_labels(dut) -> None:
    """Remove telemetry labels created by the unset CLI coverage baseline."""
    label_ids = [label_id for label_id, _ in _interface_label_pairs()]
    with allure.step("Clean interface telemetry labels configured by CLI coverage"):
        try:
            ifaces = discover_swp_interfaces_on_dut(dut)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unable to discover interfaces for CLI label cleanup: %s", exc)
            ifaces = []
        for iface in ifaces:
            for label_id in label_ids:
                dut.run_cmd(
                    f"nv unset interface {iface} telemetry label {label_id}",
                    validate=False,
                )

    with allure.step("Clean system telemetry labels configured by CLI coverage"):
        for label_id, _ in CumulusOtelConst.DEVICE_LABELS:
            dut.run_cmd(
                f"nv unset system telemetry label {label_id}",
                validate=False,
            )
        _nv_apply(dut)


def _restore_otlp_mgmt_insecure_destinations(
    dut,
    collector_ips: Iterable[str],
    *,
    export_vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT,
    is_nvos: bool = False,
) -> None:
    """Re-establish OTLP export after per-test ``clear_config`` (SSIM ``post_run_hook`` parity).

    NGTS ``clear_config`` resets the DUT to factory YAML between tests, which drops
    ``grpc insecure`` and collector destinations from ``otel_suite_mgmt``. Without this
    step, baseline restore fails ``nv config apply`` with missing-certificate errors.

    Also used by coexistence insecure prep (pass ``export_vrf`` / ``is_nvos`` for NVOS).
    """
    from ngts.tests_nvos.system.telemetry.otel.helpers import (
        configure_switch_otlp_grpc_dual_destination,
    )

    ips = list(collector_ips)
    if not ips:
        pytest.fail("CLI coverage baseline requires at least the primary collector IP, got none")
    with allure.step(
        "Restore insecure OTLP destinations (%d, export_vrf=%s)" % (len(ips), export_vrf)
    ):
        configure_switch_otlp_grpc_dual_destination(
            dut,
            primary_ip=ips[0],
            secondary_ip=ips[1] if len(ips) > 1 else None,
            export_vrf=export_vrf,
            insecure=True,
            is_nvos=is_nvos,
        )


def restore_otlp_destination_stats_group_binding(
    dut,
    collector_ips: Iterable[str],
    stats_group_id: str,
    *,
    export_vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT,
    is_nvos: bool = False,
) -> None:
    """Recreate the OTLP destinations and bind ``stats_group_id`` on each of them.

    SSIM test13a unsets the entire ``system telemetry export otlp grpc`` subtree, which
    removes the destinations created once by the module-scoped baseline. A later test that
    unbinds a destination must therefore recreate it first, or NVUE rejects the unset with
    "The requested item does not exist". Note that ``stats_group_id`` itself survives
    test13a -- only the destinations are lost.
    """
    from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import (
        _bind_stats_group_on_otlp_destinations,
    )

    ips = list(collector_ips)
    with allure.step(
        f"Restore OTLP destinations and stats-group {stats_group_id} binding ({len(ips)})"
    ):
        _restore_otlp_mgmt_insecure_destinations(
            dut, ips, export_vrf=export_vrf, is_nvos=is_nvos
        )
        _bind_stats_group_on_otlp_destinations(System(), dut, ips, stats_group_id)
        apply_unset_changes(dut)


def apply_otel_cli_coverage_baseline(
    dut,
    collector_ips: Iterable[str],
    *,
    suite=None,
) -> None:
    """``OtelMgmtVrfNoTLSConfig`` baseline for unset CLI tests (mlx lab, no BGP/leaves).

    SSIM ``configure_topo`` order (Cumulus):
    1. Per-interface histogram + interface labels on every spine ``swp`` (lines 62-128).
    2. System telemetry tree + device labels (lines 237-359).
    3. OTLP destinations + ``grpc insecure`` (``configure_topo_post_boot`` / post_run).
    4. Stats-group + root telemetry + ``asic-monitor`` via
       :func:`apply_otel_mgmt_vrf_no_tls_telemetry_config` (interface histogram skipped).

    On NVOS (``suite.is_nvos``) the histogram and mgmt-VRF steps don't apply: only
    labels are configured, then all telemetry families are enabled over the default
    export VRF via :func:`enable_nvos_telemetry_families`.
    """
    is_nvos = bool(suite and suite.is_nvos)
    _configure_interface_telemetry_cli_baseline(dut, is_nvos=is_nvos)
    _set_device_labels(dut)
    if is_nvos:
        from ngts.tests_nvos.system.telemetry.otel.helpers import (
            enable_nvos_telemetry_families,
        )

        enable_nvos_telemetry_families(
            dut,
            suite.primary_ip,
            TelemetryConsts.ALL_STATS_SUBTREES,
            export_vrf=suite.export_vrf,
            collector_ips=tuple(collector_ips),
        )
        return
    _restore_otlp_mgmt_insecure_destinations(dut, collector_ips)
    apply_otel_mgmt_vrf_no_tls_telemetry_config(
        dut,
        collector_ips=collector_ips,
        enable_interface_histogram=False,
    )


def unset_interface_histogram_ingress_pg(dut, iface: str, pg: str) -> None:
    _iface_telemetry_node(iface, '/histogram/ingress-buffer/priority-group').unset(
        str(pg), dut_engine=dut
    )


def unset_interface_histogram_egress_tc(dut, iface: str, tc: str) -> None:
    _iface_telemetry_node(iface, '/histogram/egress-buffer/traffic-class').unset(
        str(tc), dut_engine=dut
    )


def unset_interface_histogram_latency_tc(dut, iface: str, tc: str) -> None:
    _iface_telemetry_node(iface, '/histogram/latency/traffic-class').unset(
        str(tc), dut_engine=dut
    )


def unset_interface_histogram_counter(dut, iface: str, counter_type: str = "rx-byte") -> None:
    _iface_telemetry_node(iface, '/histogram/counter/counter-type').unset(
        counter_type, dut_engine=dut
    )


def unset_interface_label(dut, iface: str, label_id: str, *, key: str = "") -> None:
    BaseComponent(
        Interface(None, port_name=iface), path=f'/telemetry/label/{label_id}'
    ).unset(key, dut_engine=dut)


def get_interface_telemetry_labels_applied(dut, iface: str) -> Dict[str, Any]:
    return _iface_telemetry_applied_show(dut, iface, '/label')


def unset_system_telemetry_interface_stats_egress_tc(dut, tc: str) -> None:
    _system_telemetry_node('/interface-stats/egress-buffer/traffic-class').unset(
        str(tc), dut_engine=dut
    )


def unset_system_telemetry_interface_stats_ingress_pg(dut, pg: str) -> None:
    _system_telemetry_node('/interface-stats/ingress-buffer/priority-group').unset(
        str(pg), dut_engine=dut
    )


def unset_system_telemetry_interface_stats_switch_priority(dut, sp: str) -> None:
    """``nv unset system telemetry interface-stats switch-priority <sp>``.

    SSIM branches on ``cumulus-5.17.0`` only to pick the API keyword name
    (``if_int_sp_id`` vs ``if_int_pg_id``); the emitted NVUE CLI is identical, so
    NGTS issues the same unset regardless of version.
    """
    _system_telemetry_node('/interface-stats/switch-priority').unset(
        str(sp), dut_engine=dut
    )


def unset_system_telemetry_interface_stats_sample_interval(dut) -> None:
    _system_telemetry_node('/interface-stats/sample-interval').unset(
        '', dut_engine=dut
    )


def get_system_telemetry_interface_stats_sample_interval_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/interface-stats')


def unset_system_telemetry_control_plane_stats_sample_interval(dut) -> None:
    _system_telemetry_node('/control-plane-stats/sample-interval').unset(
        '', dut_engine=dut
    )


def get_system_telemetry_control_plane_stats_sample_interval_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/control-plane-stats')


def unset_system_telemetry_buffer_stats(dut) -> None:
    """Unset the complete root ``system telemetry buffer-stats`` subtree."""
    _system_telemetry_node('/buffer-stats').unset('', dut_engine=dut)


def get_system_telemetry_buffer_stats_applied(dut) -> Dict[str, Any]:
    """Return applied root ``system telemetry buffer-stats`` configuration."""
    return _telemetry_applied_show(dut, '/buffer-stats')


def unset_system_telemetry_software_stats(dut) -> None:
    """Unset the complete root ``system telemetry software-stats`` subtree."""
    _system_telemetry_node('/software-stats').unset('', dut_engine=dut)


def get_system_telemetry_software_stats_applied(dut) -> Dict[str, Any]:
    """Return applied root ``system telemetry software-stats`` configuration."""
    return _telemetry_applied_show(dut, '/software-stats')


def unset_system_telemetry_export_otlp_grpc(dut) -> None:
    System().telemetry.export.otlp.grpc.unset('', dut_engine=dut)


def get_system_telemetry_export_otlp_grpc_applied(dut) -> Dict[str, Any]:
    parsed = System().telemetry.export.otlp.grpc.parse_show(
        rev=ConfState.APPLIED, dut_engine=dut
    )
    return parsed if isinstance(parsed, dict) else {}


def unset_system_telemetry_export_otlp(dut, *, key: str = "", value: str = "") -> None:
    op_param = f"{key} {value}".strip() if key else ""
    System().telemetry.export.otlp.unset(op_param, dut_engine=dut)


def get_system_telemetry_export_otlp_applied(dut) -> Dict[str, Any]:
    parsed = System().telemetry.export.otlp.parse_show(
        rev=ConfState.APPLIED, dut_engine=dut
    )
    return parsed if isinstance(parsed, dict) else {}


def unset_system_otlp_destination_port(dut, destination_id: str) -> None:
    BaseComponent(
        System().telemetry.export.otlp.grpc.destination,
        path=f'/{destination_id}',
    ).unset('port', dut_engine=dut)


def unset_system_otlp_destination_stats_group(dut, destination_id: str) -> None:
    BaseComponent(
        System().telemetry.export.otlp.grpc.destination,
        path=f'/{destination_id}',
    ).unset('stats-group', dut_engine=dut)


def unset_system_telemetry_stats_group(dut, stats_group_id: str) -> None:
    BaseComponent(System().telemetry.stats_group, path=f'/{stats_group_id}').unset(
        dut_engine=dut
    )


def get_system_otlp_destination_applied(dut, destination_id: str) -> Dict[str, Any]:
    parsed = BaseComponent(
        System().telemetry.export.otlp.grpc.destination,
        path=f'/{destination_id}',
    ).parse_show(rev=ConfState.APPLIED, dut_engine=dut)
    return parsed if isinstance(parsed, dict) else {}


def get_system_telemetry_stats_groups_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/stats-group')


def _stats_group_telemetry_node(stats_group_id: str, path: str) -> BaseComponent:
    return BaseComponent(
        System().telemetry.stats_group,
        path=f'/{stats_group_id}{path}',
    )


def _stats_group_telemetry_applied_show(
    dut, stats_group_id: str, path: str
) -> Dict[str, Any]:
    parsed = _stats_group_telemetry_node(stats_group_id, path).parse_show(
        rev=ConfState.APPLIED, dut_engine=dut
    )
    return parsed if isinstance(parsed, dict) else {}


def unset_system_telemetry_stats_group_router_export(
    dut, stats_group_id: str
) -> None:
    """Unset the complete stats-group ``router export`` subtree."""
    _stats_group_telemetry_node(stats_group_id, "/router/export").unset(
        "", dut_engine=dut
    )


def get_system_telemetry_stats_group_router_export_applied(
    dut, stats_group_id: str
) -> Dict[str, Any]:
    """Return applied stats-group ``router export`` configuration."""
    return _stats_group_telemetry_applied_show(
        dut, stats_group_id, "/router/export"
    )


def unset_system_telemetry_stats_group_router(
    dut, stats_group_id: str
) -> None:
    """Unset the stats-group router sample interval."""
    _stats_group_telemetry_node(stats_group_id, "/router").unset(dut_engine=dut
                                                                 )


def get_system_telemetry_stats_group_router_applied(
    dut, stats_group_id: str
) -> Dict[str, Any]:
    """Return applied stats-group router configuration."""
    return _stats_group_telemetry_applied_show(dut, stats_group_id, "/router")


def unset_system_telemetry_stats_group_buffer_stats_export(
    dut, stats_group_id: str
) -> None:
    """Unset the complete stats-group ``buffer-stats export`` subtree."""
    _stats_group_telemetry_node(stats_group_id, "/buffer-stats/export").unset(
        "", dut_engine=dut
    )


def get_system_telemetry_stats_group_buffer_stats_export_applied(
    dut, stats_group_id: str
) -> Dict[str, Any]:
    """Return applied stats-group ``buffer-stats export`` configuration."""
    return _stats_group_telemetry_applied_show(
        dut, stats_group_id, "/buffer-stats/export"
    )


def unset_system_telemetry_stats_group_platform_stats_class(
    dut,
    stats_group_id: str,
    platform_class: str = "",
    *,
    key: str = "",
    value: str = "",
) -> None:
    """SSIM test15c: ``nv unset system telemetry stats-group <id> platform-stats class``."""
    path = "/platform-stats/class"
    if platform_class:
        path = f"{path}/{platform_class}"
    op_param = f"{key} {value}".strip() if key else ""
    _stats_group_telemetry_node(stats_group_id, path).unset(op_param, dut_engine=dut)


def unset_system_telemetry_stats_group_platform_stats_export_sample_interval(
    dut, stats_group_id: str
) -> None:
    """SSIM test15c: unset stats-group platform-stats export sample-interval."""
    _stats_group_telemetry_node(stats_group_id, "/platform-stats/export").unset(
        TelemetryConsts.SAMPLE_INTERVAL, dut_engine=dut
    )


def get_system_telemetry_stats_group_platform_stats_classes_applied(
    dut, stats_group_id: str
) -> Dict[str, Any]:
    return _stats_group_telemetry_applied_show(
        dut, stats_group_id, "/platform-stats/class"
    )


def get_system_telemetry_stats_group_platform_stats_export_applied(
    dut, stats_group_id: str
) -> Dict[str, Any]:
    return _stats_group_telemetry_applied_show(
        dut, stats_group_id, "/platform-stats/export"
    )


def _stats_group_platform_stats_class_default() -> Dict[str, Any]:
    return {
        TelemetryConsts.SAMPLE_INTERVAL: TelemetryConsts.Defaults.PLATFORM_EXPORT_SAMPLE_INTERVAL,
        TelemetryConsts.STATE: TelemetryConsts.Defaults.PLATFORM_CLASS_STATE,
    }


def expected_stats_group_platform_stats_classes_after_unset_pre114() -> Dict[str, Dict[str, Any]]:
    """Defaults after unset on CL 5.14–5.17 (NVUE 1.13 and older)."""
    default = _stats_group_platform_stats_class_default()
    return {
        platform_class: dict(default)
        for platform_class in CumulusOtelConst.STATS_GROUP_PLATFORM_CLASSES
    }


def expected_stats_group_platform_stats_classes_after_unset_test15b() -> Dict[str, Dict[str, Any]]:
    """Expected platform-stats classes after unset for test15b (CL 5.12–5.13)."""
    default = _stats_group_platform_stats_class_default()
    return {
        platform_class: dict(default)
        for platform_class in CumulusOtelConst.TEST15B_STATS_GROUP_PLATFORM_CLASSES
    }


def expected_stats_group_platform_stats_classes_after_unset_v114(
    is_nvos: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Defaults after unset on CL 5.18+ (NVUE 1.14+ includes ``asic-resource``).

    NVOS restores its own class set (:data:`TelemetryConsts.PLATFORM_CLASSES`, which adds
    ``asic-power``/``health-info`` and has no ``file-system`` or ``asic-resource``) and
    carries no per-class ``sample-interval`` -- that lives under ``platform-stats export``.
    """
    if is_nvos:
        return {
            platform_class: {
                TelemetryConsts.STATE: TelemetryConsts.Defaults.PLATFORM_CLASS_STATE
            }
            for platform_class in TelemetryConsts.PLATFORM_CLASSES
        }
    expected = expected_stats_group_platform_stats_classes_after_unset_pre114()
    expected[CumulusOtelConst.PLATFORM_CLASS_ASIC_RESOURCE] = {
        TelemetryConsts.SAMPLE_INTERVAL: (
            CumulusOtelConst.STATS_GROUP_PLATFORM_CLASS_ASIC_RESOURCE_SAMPLE_INTERVAL
        ),
        TelemetryConsts.STATE: TelemetryConsts.Defaults.PLATFORM_CLASS_STATE,
    }
    return expected


def expected_stats_group_platform_stats_classes_default_applied() -> Dict[str, Dict[str, Any]]:
    """Backward-compatible alias for pre-5.18 expected platform-stats classes."""
    return expected_stats_group_platform_stats_classes_after_unset_pre114()


def validate_unset_stats_group_platform_stats(
    dut,
    stats_group_id: str,
    expected_platform_stats_class: Dict[str, Dict[str, Any]],
) -> None:
    """Shared SSIM test15b/15c unset + validation for stats-group platform-stats."""
    unset_system_telemetry_stats_group_platform_stats_class(dut, stats_group_id)
    apply_unset_changes(dut)
    out = get_system_telemetry_stats_group_platform_stats_classes_applied(
        dut, stats_group_id
    )
    assert out == expected_platform_stats_class, (
        "nv unset system telemetry stats-group platform-stats class failed: "
        f"actual={out!r} expected={expected_platform_stats_class!r}"
    )
    unset_system_telemetry_stats_group_platform_stats_export_sample_interval(
        dut, stats_group_id
    )
    apply_unset_changes(dut)
    out = get_system_telemetry_stats_group_platform_stats_export_applied(
        dut, stats_group_id
    )
    assert out.get(TelemetryConsts.SAMPLE_INTERVAL) == (
        TelemetryConsts.Defaults.PLATFORM_EXPORT_SAMPLE_INTERVAL
    ), (
        "nv unset system telemetry stats-group platform-stats export "
        f"sample-interval failed: actual={out.get(TelemetryConsts.SAMPLE_INTERVAL)!r} "
        f"expected={TelemetryConsts.Defaults.PLATFORM_EXPORT_SAMPLE_INTERVAL!r} "
        f"full out={out!r}"
    )


def get_system_interface_stats_egress_tc_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(
        dut, '/interface-stats/egress-buffer/traffic-class'
    )


def unset_system_telemetry_interface_stats_export(
    dut, *, key: str = "", value: str = ""
) -> None:
    """SSIM test06a: ``nv unset system telemetry interface-stats export [state]``."""
    op_param = f"{key} {value}".strip() if key else ""
    _system_telemetry_node('/interface-stats/export').unset(op_param, dut_engine=dut)


def get_system_telemetry_interface_stats_export_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/interface-stats/export')


def unset_system_telemetry_control_plane_stats_export(
    dut, *, key: str = "", value: str = ""
) -> None:
    """SSIM test06b: ``nv unset system telemetry control-plane-stats export [state]``."""
    op_param = f"{key} {value}".strip() if key else ""
    _system_telemetry_node('/control-plane-stats/export').unset(op_param, dut_engine=dut)


def get_system_telemetry_control_plane_stats_export_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/control-plane-stats/export')


def unset_system_telemetry_buffer_stats_export(
    dut, *, key: str = "", value: str = ""
) -> None:
    """SSIM test06c: ``nv unset system telemetry buffer-stats export [state]``."""
    op_param = f"{key} {value}".strip() if key else ""
    _system_telemetry_node('/buffer-stats/export').unset(op_param, dut_engine=dut)


def get_system_telemetry_buffer_stats_export_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/buffer-stats/export')


def unset_system_telemetry_buffer_stats(dut, *, key: str = "", value: str = "") -> None:
    """SSIM test06c: ``nv unset system telemetry buffer-stats [sample-interval]``."""
    op_param = f"{key} {value}".strip() if key else ""
    _system_telemetry_node('/buffer-stats').unset(op_param, dut_engine=dut)


def get_system_telemetry_buffer_stats_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/buffer-stats')


def unset_system_telemetry_label(dut, label_id: str, *, key: str = "") -> None:
    BaseComponent(System().telemetry.label, path=f'/{label_id}').unset(
        key, dut_engine=dut
    )


def get_system_telemetry_labels_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/label')


def unset_system_interface_stats_class_phy(dut, *, key: str = "", value: str = "") -> None:
    op_param = f"{key} {value}".strip() if key else ""
    System().telemetry.interface_stats.cls.phy.unset(op_param, dut_engine=dut)


def get_system_interface_stats_class_phy_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/interface-stats/class/phy')


def _unset_system_telemetry_stats_export(
    dut, family: str, *, key: str = "", value: str = ""
) -> None:
    """``nv unset system telemetry <family> export [<key> <value>]``."""
    op_param = f"{key} {value}".strip() if key else ""
    _system_telemetry_node(f'/{family}/export').unset(op_param, dut_engine=dut)


def _get_system_telemetry_stats_export_applied(dut, family: str) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, f'/{family}/export')


def unset_system_telemetry_interface_stats_export(
    dut, *, key: str = "", value: str = ""
) -> None:
    _unset_system_telemetry_stats_export(dut, 'interface-stats', key=key, value=value)


def get_system_telemetry_interface_stats_export_applied(dut) -> Dict[str, Any]:
    return _get_system_telemetry_stats_export_applied(dut, 'interface-stats')


def unset_system_telemetry_platform_stats_export(
    dut, *, key: str = "", value: str = ""
) -> None:
    _unset_system_telemetry_stats_export(dut, 'platform-stats', key=key, value=value)


def get_system_telemetry_platform_stats_export_applied(dut) -> Dict[str, Any]:
    return _get_system_telemetry_stats_export_applied(dut, 'platform-stats')


def unset_system_telemetry_control_plane_stats_export(
    dut, *, key: str = "", value: str = ""
) -> None:
    _unset_system_telemetry_stats_export(dut, 'control-plane-stats', key=key, value=value)


def get_system_telemetry_control_plane_stats_export_applied(dut) -> Dict[str, Any]:
    return _get_system_telemetry_stats_export_applied(dut, 'control-plane-stats')


def unset_system_telemetry_buffer_stats_export(
    dut, *, key: str = "", value: str = ""
) -> None:
    _unset_system_telemetry_stats_export(dut, 'buffer-stats', key=key, value=value)


def get_system_telemetry_buffer_stats_export_applied(dut) -> Dict[str, Any]:
    return _get_system_telemetry_stats_export_applied(dut, 'buffer-stats')


def _unset_system_telemetry_stats(dut, family: str, *, key: str = "", value: str = "") -> None:
    """``nv unset system telemetry <family> [<key> <value>]`` (whole-subtree unset)."""
    op_param = f"{key} {value}".strip() if key else ""
    _system_telemetry_node(f'/{family}').unset(op_param, dut_engine=dut)


def _get_system_telemetry_stats_applied(dut, family: str) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, f'/{family}')


def unset_system_telemetry_interface_stats(dut, *, key: str = "", value: str = "") -> None:
    _unset_system_telemetry_stats(dut, 'interface-stats', key=key, value=value)


def get_system_telemetry_interface_stats_applied(dut) -> Dict[str, Any]:
    return _get_system_telemetry_stats_applied(dut, 'interface-stats')


def unset_system_telemetry_control_plane_stats(dut, *, key: str = "", value: str = "") -> None:
    _unset_system_telemetry_stats(dut, 'control-plane-stats', key=key, value=value)


def get_system_telemetry_control_plane_stats_applied(dut) -> Dict[str, Any]:
    return _get_system_telemetry_stats_applied(dut, 'control-plane-stats')


def unset_system_telemetry_platform_stats(dut, *, key: str = "", value: str = "") -> None:
    _unset_system_telemetry_stats(dut, 'platform-stats', key=key, value=value)


def get_system_telemetry_platform_stats_applied(dut) -> Dict[str, Any]:
    return _get_system_telemetry_stats_applied(dut, 'platform-stats')


def set_system_telemetry_histogram(dut, key: str, value: str) -> None:
    """``nv set system telemetry histogram <key> <value>`` (e.g. ``temporality cumulative``)."""
    _system_telemetry_node('/histogram').set(key, value, dut_engine=dut)


def unset_system_telemetry_histogram(dut, *, key: str = "") -> None:
    """``nv unset system telemetry histogram [<key>]``."""
    _system_telemetry_node('/histogram').unset(key, dut_engine=dut)


def get_system_telemetry_histogram_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/histogram')


def unset_system_telemetry_histogram_export(dut, *, key: str = "") -> None:
    """``nv unset system telemetry histogram export [<key>]``."""
    _system_telemetry_node('/histogram/export').unset(key, dut_engine=dut)


def get_system_telemetry_histogram_export_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/histogram/export')


def unset_system_telemetry_software_stats_systemd(dut, *, key: str = "") -> None:
    """``nv unset system telemetry software-stats systemd [<key>]``.

    ``key`` empty unsets the whole ``systemd`` subtree; otherwise unsets a single leaf
    (e.g. ``active-profile`` / ``process-level`` / ``sample-interval``).
    """
    _system_telemetry_node('/software-stats/systemd').unset(key, dut_engine=dut)


def get_system_telemetry_software_stats_systemd_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/software-stats/systemd')


def unset_system_telemetry_software_stats_systemd_export(dut, *, key: str = "") -> None:
    """``nv unset system telemetry software-stats systemd export [<key>]``."""
    _system_telemetry_node('/software-stats/systemd/export').unset(key, dut_engine=dut)


def get_system_telemetry_software_stats_systemd_export_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/software-stats/systemd/export')


def unset_system_telemetry_software_stats_systemd_unit_profile(
    dut, *, unit_profile_id: str = ""
) -> None:
    """``nv unset system telemetry software-stats systemd unit-profile [<unit-profile-id>]``.

    ``unit_profile_id`` empty unsets the whole ``unit-profile`` subtree; otherwise unsets a
    single profile (e.g. ``custom``).
    """
    _system_telemetry_node('/software-stats/systemd/unit-profile').unset(
        unit_profile_id, dut_engine=dut
    )


def get_system_telemetry_software_stats_systemd_unit_profile_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/software-stats/systemd/unit-profile')


def unset_system_telemetry_software_stats_systemd_unit_profile_unit(
    dut, *, unit_profile_id: str = "custom"
) -> None:
    """``nv unset system telemetry software-stats systemd unit-profile <unit-profile-id> unit``."""
    _system_telemetry_node(
        f'/software-stats/systemd/unit-profile/{unit_profile_id}'
    ).unset('unit', dut_engine=dut)


def unset_system_platform_stats_class(
    dut, platform_class: str, *, key: str = "", value: str = ""
) -> None:
    system = System()
    node = system.telemetry.platform_stats.cls.categories.get(platform_class)
    if node is None:
        node = BaseComponent(system.telemetry.platform_stats.cls, path=f'/{platform_class}')
    op_param = f"{key} {value}".strip() if key else ""
    node.unset(op_param, dut_engine=dut)


def platform_stats_classes_for_unset(is_nvos: bool) -> Tuple[str, ...]:
    """SSIM test05 platform-stats classes to unset, per platform.

    ``platform-info`` (test05a) and ``transceiver-info`` (transceiver test) are
    handled elsewhere and excluded here.
    """
    skip = {
        TelemetryConsts.PLATFORM_CLASS_PLATFORM_INFO,
        TelemetryConsts.PLATFORM_CLASS_TRANSCEIVER_INFO,
    }
    source = (
        TelemetryConsts.PLATFORM_CLASSES
        if is_nvos
        else CumulusOtelConst.ROOT_PLATFORM_CLASSES
    )
    return tuple(c for c in source if c not in skip)


def get_system_platform_stats_class_applied(dut, platform_class: str) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, f'/platform-stats/class/{platform_class}')


def unset_system_platform_stats_class_platform_info(
    dut, *, key: str = "", value: str = ""
) -> None:
    """SSIM test05a: ``nv unset system telemetry platform-stats class platform-info``."""
    unset_system_platform_stats_class(
        dut, TelemetryConsts.PLATFORM_CLASS_PLATFORM_INFO, key=key, value=value
    )


def get_system_platform_stats_class_platform_info_applied(dut) -> Dict[str, Any]:
    return get_system_platform_stats_class_applied(
        dut, TelemetryConsts.PLATFORM_CLASS_PLATFORM_INFO
    )


def unset_system_platform_stats_class_transceiver_info(
    dut, *, key: str = "", value: str = ""
) -> None:
    """SSIM test18a/18b: ``nv unset system telemetry platform-stats class transceiver-info``."""
    unset_system_platform_stats_class(
        dut, TelemetryConsts.PLATFORM_CLASS_TRANSCEIVER_INFO, key=key, value=value
    )


def get_system_platform_stats_class_transceiver_info_applied(dut) -> Dict[str, Any]:
    return get_system_platform_stats_class_applied(
        dut, TelemetryConsts.PLATFORM_CLASS_TRANSCEIVER_INFO
    )


def unset_system_telemetry_adaptive_routing_stats(dut, *, key: str = "") -> None:
    """SSIM test20a/20b: ``nv unset system telemetry adaptive-routing-stats [key]``."""
    _system_telemetry_node('/adaptive-routing-stats').unset(key, dut_engine=dut)


def unset_system_telemetry_adaptive_routing_stats_export(dut) -> None:
    """SSIM test20b: ``nv unset system telemetry adaptive-routing-stats export``."""
    _system_telemetry_node('/adaptive-routing-stats/export').unset("", dut_engine=dut)


def get_system_telemetry_adaptive_routing_stats_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/adaptive-routing-stats')


def apply_unset_changes(dut) -> None:
    with allure.step("Apply NVUE after unset operations"):
        _nv_apply(dut)


def swp_interfaces_for_unset(dut, *, is_nvos: Optional[bool] = None) -> List[str]:
    ifaces: List[str] = []
    if is_nvos is None:
        is_nvos = get_cumulus_version_tuple(dut) == (0, 0)
    if is_nvos:
        ifaces = _nvos_ib_interfaces_for_unset(dut)
    else:
        try:
            ifaces = list_swp_interfaces(dut)
        except Exception:  # noqa: BLE001
            ifaces = []
        if not ifaces:
            ifaces = list(discover_swp_interfaces_on_dut(dut))
    if not ifaces:
        pytest.fail(
            "No front-panel interfaces found for unset coverage "
            "(expected Cumulus 'swp*' or NVOS IB ports from Port.get_list_of_ports()); "
            "check 'nv show interface' output and port type discovery"
        )
    return ifaces
