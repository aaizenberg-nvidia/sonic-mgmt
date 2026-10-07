"""NV unset CLI coverage baseline and helpers (SSIM ``Test_Otel_Mgmt_Vrf_Insecure_CLI_Coverage``)."""

from __future__ import annotations

import json
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
    apply_staged_candidate,
    nvue_quoted,
    stage_interface_histogram_member,
    stage_interface_telemetry_label,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.lab_topology import (
    discover_swp_interfaces_on_dut,
)

logger = logging.getLogger(__name__)

# Applied revision captured after the module baseline. Per-test restore
# reapplies this id instead of reissuing the interface and telemetry sets.
_golden_revision_id: Optional[str] = None


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


def require_cumulus_at_most(dut, max_version: str) -> None:
    """Skip when DUT Cumulus version is above ``max_version``."""
    current = get_cumulus_version_tuple(dut)
    if current == (0, 0):
        return
    maximum = tuple(int(part) for part in max_version.split(".")[:2])
    if current > maximum:
        pytest.skip(
            f"Cumulus {current[0]}.{current[1]} > supported {max_version} for this unset test"
        )


def require_cumulus_exactly(dut, version: str) -> None:
    """Skip when DUT Cumulus major.minor does not match ``version`` (SSIM ``==`` gate).

    No-op on non-Cumulus platforms (version parses to ``(0, 0)``).
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
    apply_staged_candidate(dut)


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


def _stage_nv_commands(dut, commands: List[str]) -> None:
    """Stage ``nv`` lines without scraping each exit status.

    ``validate=True`` makes the engine run ``echo "$?"`` and scrape the first digits
    out of the reply. On mlx DUT shells every command can carry ``/tmp/cen<N>:
    Permission denied`` bash noise (see ``cleanup_stale_nvue_censor_files``), so that
    scrape can return the digits of the tempfile name instead of the real status.
    ``nv config apply`` rejects the candidate if any staged line was invalid.
    """
    for cmd in commands:
        dut.run_cmd(cmd, validate=False, print_output=False)


def discard_pending_nvue_revision(dut) -> None:
    """Drop a candidate revision left by an unset that was not applied."""
    dut.run_cmd("nv config detach", validate=False, print_output=False)


def _load_nv_json(raw: str) -> Any:
    starts = [index for index in (raw.find("{"), raw.find("[")) if index != -1]
    if not starts:
        return None
    start = min(starts)
    closer = "}" if raw[start] == "{" else "]"
    end = raw.rfind(closer)
    if end <= start:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None


def _revision_state(row: Dict[str, Any]) -> str:
    return str(row.get("state") or row.get("status") or "").lower()


def _find_applied_revision(payload: Any) -> Optional[str]:
    """Return the numeric id of the applied revision from ``nv config revision -o json``."""
    if isinstance(payload, list):
        for item in payload:
            found = _find_applied_revision(item)
            if found:
                return found
        return None
    if not isinstance(payload, dict):
        return None
    revision_rows = {
        str(key): value
        for key, value in payload.items()
        if str(key).isdigit() and isinstance(value, dict)
    }
    if revision_rows:
        applied = [
            key for key, value in revision_rows.items() if _revision_state(value) == "applied"
        ]
        if len(applied) == 1:
            return applied[0]
        # More than one row marked applied: the newest id is the live config.
        return max(applied, key=int) if applied else None
    state = _revision_state(payload)
    rev = payload.get("rev-id", payload.get("rev_id", payload.get("id")))
    if state == "applied" and rev is not None and str(rev).isdigit():
        return str(rev)
    for value in payload.values():
        if isinstance(value, (dict, list)):
            found = _find_applied_revision(value)
            if found:
                return found
    return None


def _applied_revision_id(dut) -> str:
    raw = dut.run_cmd("nv config revision -o json", validate=False, print_output=False)
    found = _find_applied_revision(_load_nv_json(str(raw)))
    if found:
        return found
    match = re.search(r"^\s*(\d+)\s+applied\b", str(raw), re.MULTILINE | re.IGNORECASE)
    if match:
        return match.group(1)
    pytest.fail(f"Could not read the applied NVUE revision: {str(raw)[:500]}")


def capture_otel_cli_coverage_golden(dut) -> str:
    """Remember the applied revision that holds the module baseline."""
    global _golden_revision_id
    _golden_revision_id = _applied_revision_id(dut)
    logger.info("Unset CLI coverage golden revision is %s", _golden_revision_id)
    return _golden_revision_id


def restore_otel_cli_coverage_golden(dut, *, missing_ok: bool = False) -> None:
    """Roll applied config back to the module baseline with one ``nv config apply``.

    Replaying interface labels, histogram, device labels, OTLP destinations, and
    the telemetry tree after every test is what made this module take about an hour.
    """
    if not _golden_revision_id:
        if missing_ok:
            return
        pytest.fail(
            "Unset CLI golden revision was not captured; "
            "otel_cli_coverage_baseline did not run"
        )
    discard_pending_nvue_revision(dut)
    current = _applied_revision_id(dut)
    if current == _golden_revision_id:
        return
    with allure.step(
        f"Restore unset CLI golden revision {_golden_revision_id} (applied {current})"
    ):
        from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli

        NvueGeneralCli.apply_config(
            dut,
            option="-y",
            rev_id=_golden_revision_id,
            verify_execution=True,
        )
    # The rollback is a new applied revision with the same contents. Track it so
    # later rollbacks do not depend on a revision NVUE may prune.
    capture_otel_cli_coverage_golden(dut)


def _configure_interface_telemetry_cli_baseline(dut, *, is_nvos: bool = False) -> None:
    """SSIM ``OtelMgmtVrfNoTLSConfig.configure_topo`` spine ``swp`` loop (lines 62-128).

    Per interface: full histogram (PG/TC 0-7, rx-byte + crc) then labels l1-l10.
    NVOS has no per-interface histogram, so it configures labels only.
    """
    from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
        cleanup_stale_nvue_censor_files,
    )

    cleanup_stale_nvue_censor_files(dut)
    ifaces = swp_interfaces_for_unset(dut, is_nvos=is_nvos)
    label_pairs = _interface_label_pairs()
    commands: List[str] = []
    for iface in ifaces:
        if not is_nvos:
            for pg in range(8):
                commands.append(
                    f"nv set interface {iface} telemetry histogram "
                    f"ingress-buffer priority-group {pg}"
                )
            for tc in range(8):
                commands.append(
                    f"nv set interface {iface} telemetry histogram "
                    f"egress-buffer traffic-class {tc}"
                )
                commands.append(
                    f"nv set interface {iface} telemetry histogram "
                    f"latency traffic-class {tc}"
                )
            for counter_type in ("rx-byte", "crc"):
                commands.append(
                    f"nv set interface {iface} telemetry histogram "
                    f"counter counter-type {counter_type}"
                )
        for label_id, description in label_pairs:
            commands.append(
                f"nv set interface {iface} telemetry label {label_id} "
                f'description "{description}"'
            )
    with allure.step(
        f"Configure interface {'labels' if is_nvos else 'histogram + labels'} "
        f"on {len(ifaces)} front-panel port(s) ({len(commands)} commands, once per module)"
    ):
        for iface in ifaces:
            if not is_nvos:
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
        _nv_apply(dut)


def _set_device_labels(dut) -> None:
    """SSIM ``OtelMgmtVrfNoTLSConfig`` system telemetry device labels (lines 339-359)."""
    with allure.step("Configure system telemetry device labels (CLI coverage)"):
        for label_id, description in CumulusOtelConst.DEVICE_LABELS:
            BaseComponent(System().telemetry, path=f"/label/{label_id}").set(
                TelemetryConsts.DESCRIPTION,
                nvue_quoted(description),
                dut_engine=dut,
            ).verify_result()
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
                BaseComponent(
                    Interface(None, port_name=iface),
                    path=f"/telemetry/label/{label_id}",
                ).unset(dut_engine=dut).ignore_result()

    with allure.step("Clean system telemetry labels configured by CLI coverage"):
        for label_id, _ in CumulusOtelConst.DEVICE_LABELS:
            BaseComponent(System().telemetry, path=f"/label/{label_id}").unset(
                dut_engine=dut
            ).ignore_result()
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
    include_interface_baseline: bool = True,
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

    Call this once per module, then :func:`capture_otel_cli_coverage_golden`.
    Per-test undo is :func:`restore_otel_cli_coverage_golden` (one ``nv config apply``
    of that revision). Replaying step 1 after every test is 36 ``nv set`` calls per
    front-panel port and is what made the module take about an hour.
    ``include_interface_baseline=False`` skips step 1 when a caller only needs the
    system tree.
    """
    is_nvos = bool(suite and suite.is_nvos)
    if include_interface_baseline:
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


def get_interface_histogram_ingress_pg_applied(dut, iface: str) -> Dict[str, Any]:
    return _iface_telemetry_applied_show(
        dut, iface, '/histogram/ingress-buffer/priority-group'
    )


def get_interface_histogram_egress_tc_applied(dut, iface: str) -> Dict[str, Any]:
    return _iface_telemetry_applied_show(
        dut, iface, '/histogram/egress-buffer/traffic-class'
    )


def get_interface_histogram_latency_tc_applied(dut, iface: str) -> Dict[str, Any]:
    return _iface_telemetry_applied_show(dut, iface, '/histogram/latency/traffic-class')


def get_interface_histogram_counter_applied(dut, iface: str) -> Dict[str, Any]:
    return _iface_telemetry_applied_show(dut, iface, '/histogram/counter/counter-type')


def applied_ids(applied_show: Dict[str, Any]) -> set:
    """List-keyed node ids as strings (``priority-group``, ``traffic-class``, ...)."""
    return {str(node_id) for node_id in applied_show}


def expected_interface_labels_after_unset(unset_label_ids: Iterable[str]) -> Dict[str, Any]:
    """Applied ``interface <iface> telemetry label`` tree once ``unset_label_ids`` are gone.

    Derived from the same ``CumulusOtelConst.INTF_LABELS`` the baseline configures.
    """
    removed = set(unset_label_ids)
    return {
        label_id: {"description": description}
        for label_id, description in _interface_label_pairs()
        if label_id not in removed
    }


def expected_system_labels_after_unset(unset_label_ids: Iterable[str]) -> Dict[str, Any]:
    """Applied ``system telemetry label`` tree once ``unset_label_ids`` are gone.

    Derived from the same ``CumulusOtelConst.DEVICE_LABELS`` the baseline configures.
    """
    removed = set(unset_label_ids)
    return {
        label_id: {"description": description}
        for label_id, description in CumulusOtelConst.DEVICE_LABELS
        if label_id not in removed
    }


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
    """``nv unset system telemetry interface-stats sample-interval``.

    Same OpenAPI rule as interface-stats: the property name is the unset argument.
    """
    _system_telemetry_node('/interface-stats').unset(
        TelemetryConsts.SAMPLE_INTERVAL, dut_engine=dut
    )


def get_system_telemetry_interface_stats_sample_interval_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/interface-stats')


def unset_system_telemetry_control_plane_stats_sample_interval(dut) -> None:
    """``nv unset system telemetry control-plane-stats sample-interval``.

    Same OpenAPI rule as interface-stats: the property name is the unset argument.
    """
    _system_telemetry_node('/control-plane-stats').unset(
        TelemetryConsts.SAMPLE_INTERVAL, dut_engine=dut
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


def get_system_interface_stats_ingress_pg_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(
        dut, '/interface-stats/ingress-buffer/priority-group'
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


def unset_system_telemetry_lldp(dut, *, key: str = "", value: str = "") -> None:
    """``nv unset system telemetry lldp [<key> <value>]``."""
    _unset_system_telemetry_stats(dut, 'lldp', key=key, value=value)


def get_system_telemetry_lldp_applied(dut) -> Dict[str, Any]:
    return _get_system_telemetry_stats_applied(dut, 'lldp')


def unset_system_telemetry_lldp_export(dut, *, key: str = "", value: str = "") -> None:
    """``nv unset system telemetry lldp export [<key> <value>]``."""
    _unset_system_telemetry_stats_export(dut, 'lldp', key=key, value=value)


def get_system_telemetry_lldp_export_applied(dut) -> Dict[str, Any]:
    return _get_system_telemetry_stats_export_applied(dut, 'lldp')


def unset_system_telemetry_adaptive_routing_stats(
    dut, *, key: str = "", value: str = ""
) -> None:
    """``nv unset system telemetry adaptive-routing-stats [<key> <value>]``."""
    _unset_system_telemetry_stats(dut, 'adaptive-routing-stats', key=key, value=value)


def get_system_telemetry_adaptive_routing_stats_applied(dut) -> Dict[str, Any]:
    return _get_system_telemetry_stats_applied(dut, 'adaptive-routing-stats')


def unset_system_telemetry_adaptive_routing_stats_export(
    dut, *, key: str = "", value: str = ""
) -> None:
    """``nv unset system telemetry adaptive-routing-stats export [<key> <value>]``."""
    _unset_system_telemetry_stats_export(
        dut, 'adaptive-routing-stats', key=key, value=value
    )


def get_system_telemetry_adaptive_routing_stats_export_applied(dut) -> Dict[str, Any]:
    return _get_system_telemetry_stats_export_applied(dut, 'adaptive-routing-stats')


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


def unset_system_platform_stats_export(dut, *, key: str = "") -> None:
    _system_telemetry_node('/platform-stats/export').unset(key, dut_engine=dut)


def get_system_platform_stats_export_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/platform-stats/export')


def unset_system_platform_stats_classes(dut) -> None:
    _system_telemetry_node('/platform-stats/class').unset(dut_engine=dut)


def get_system_platform_stats_classes_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/platform-stats/class')


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


def unset_system_interface_stats_ingress_buffer(dut) -> None:
    _system_telemetry_node('/interface-stats/ingress-buffer').unset(dut_engine=dut)


def get_system_interface_stats_ingress_buffer_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/interface-stats/ingress-buffer')


def unset_system_interface_stats_egress_buffer(dut) -> None:
    _system_telemetry_node('/interface-stats/egress-buffer').unset(dut_engine=dut)


def get_system_interface_stats_egress_buffer_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/interface-stats/egress-buffer')


def unset_system_interface_stats_switch_priority(dut) -> None:
    _system_telemetry_node('/interface-stats/switch-priority').unset(dut_engine=dut)


def get_system_interface_stats_switch_priority_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/interface-stats/switch-priority')


def unset_system_interface_stats_classes(dut) -> None:
    _system_telemetry_node('/interface-stats/class').unset(dut_engine=dut)


def get_system_interface_stats_classes_applied(dut) -> Dict[str, Any]:
    return _telemetry_applied_show(dut, '/interface-stats/class')


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
