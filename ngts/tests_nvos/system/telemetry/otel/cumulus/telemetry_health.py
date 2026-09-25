"""Cumulus ``nv show system telemetry health`` helpers (SSIM telemetry_health port)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.telemetry.otel.constants import OtelCollectorConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
    restart_asic_monitor,
    restart_nvtelemetry,
    start_nvtelemetry,
    stop_nvtelemetry,
)
from ngts.tests_nvos.system.telemetry.otel.otel_collector import OtelCollector

logger = logging.getLogger(__name__)

_HEALTH_POLL_INTERVAL_SEC = 3
_CONNECTIVITY_STATUS_PASS = "Pass"
_CONNECTIVITY_STATUS_FAIL = "Fail"


def show_telemetry_health(dut) -> Dict[str, Any]:
    """``nv show system telemetry health -o json``."""
    system = System()
    health_node = BaseComponent(system.telemetry, path="/health")
    with allure.step("nv show system telemetry health"):
        raw = health_node.show(dut_engine=dut)
        parsed = OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()
        return parsed if isinstance(parsed, dict) else {}


def get_export_destinations_from_health(dut) -> List[str]:
    """Export destination keys from telemetry health (for connectivity checks)."""
    health = show_telemetry_health(dut)
    export_status = health.get("export-destination-status") or {}
    return list(export_status.keys())


def wait_until_service_status(
    dut,
    *,
    service: str,
    status: str = "active",
    max_attempts: int = 20,
) -> None:
    """Poll ``service-status`` until *service* reaches *status*."""
    for attempt in range(1, max_attempts + 1):
        health = show_telemetry_health(dut)
        current = (health.get("service-status") or {}).get(service)
        if current == status:
            logger.info("service %s on DUT is %s (attempt %d)", service, status, attempt)
            return
        if attempt < max_attempts:
            logger.debug(
                "service %s status %r != %r (attempt %d/%d); waiting",
                service,
                current,
                status,
                attempt,
                max_attempts,
            )
            time.sleep(_HEALTH_POLL_INTERVAL_SEC)
    pytest.fail(
        f"service {service!r} never reached status {status!r} after {max_attempts} attempts"
    )


def wait_until_connectivity_status(
    dut,
    *,
    server: str,
    status: str = _CONNECTIVITY_STATUS_PASS,
    max_attempts: int = 40,
) -> None:
    """Poll export-destination connectivity for *server*."""
    for attempt in range(1, max_attempts + 1):
        health = show_telemetry_health(dut)
        export_status = health.get("export-destination-status") or {}
        server_status = export_status.get(server) or {}
        current = server_status.get("connectivity")
        if current == status:
            logger.info(
                "export destination %s connectivity=%s (attempt %d)",
                server,
                status,
                attempt,
            )
            return
        if attempt < max_attempts:
            logger.debug(
                "destination %s connectivity %r != %r (attempt %d/%d); waiting",
                server,
                current,
                status,
                attempt,
                max_attempts,
            )
            time.sleep(_HEALTH_POLL_INTERVAL_SEC)
    pytest.fail(
        f"export destination {server!r} connectivity never reached {status!r} "
        f"after {max_attempts} attempts"
    )


def verify_counter_flow(dut, server: str) -> None:
    """Assert export counter increments over ~10s (SSIM ``verify_counter_flow``)."""
    health = show_telemetry_health(dut)
    export_status = health.get("export-destination-status") or {}
    before = int(export_status[server]["counter"])
    for _ in range(10):
        time.sleep(1)
        health = show_telemetry_health(dut)
        after = int(health["export-destination-status"][server]["counter"])
        if after > before:
            logger.info(
                "export counter flowing for %s: before=%d after=%d",
                server,
                before,
                after,
            )
            return
    pytest.fail(
        f"export counter for {server!r} did not increment (before={before})"
    )


def verify_otel_health_services(
    dut,
    *,
    max_attempts: int = 20,
    services: Optional[Iterable[str]] = None,
) -> None:
    """Wait until configured OTEL health services report *active*."""
    health = show_telemetry_health(dut)
    available = set((health.get("service-status") or {}).keys())
    candidates = list(services or CumulusOtelConst.OTEL_HEALTH_SERVICES)
    to_check = [svc for svc in candidates if svc in available]
    if not to_check:
        pytest.fail(
            f"none of the expected OTEL health services found in telemetry health: "
            f"candidates={candidates}, available={sorted(available)}"
        )
    skipped = sorted(set(candidates) - set(to_check))
    if skipped:
        logger.info(
            "Skipping OTEL health services not reported on this build: %s",
            skipped,
        )
    with allure.step(f"Verify OTEL health services active ({len(to_check)})"):
        for service in to_check:
            wait_until_service_status(
                dut,
                service=service,
                status="active",
                max_attempts=max_attempts,
            )


def verify_export_destinations_health(
    dut,
    *,
    max_connectivity_attempts: int = 40,
) -> None:
    """Connectivity Pass + counter flow for each non-unix export destination."""
    servers = [
        s for s in get_export_destinations_from_health(dut) if not str(s).startswith("unix://")
    ]
    assert servers, "no gRPC/HTTP export destinations in telemetry health"
    with allure.step(f"Verify export destination health ({len(servers)} server(s))"):
        for server in servers:
            wait_until_connectivity_status(
                dut,
                server=server,
                max_attempts=max_connectivity_attempts,
            )
            verify_counter_flow(dut, server)


def assert_otlp_session_established(
    collector: OtelCollector,
    *,
    port: int = OtelCollectorConst.OTLP_GRPC_PORT,
    max_attempts: int = 8,
    retry_interval_sec: float = 2.0,
) -> None:
    """Collector host has ESTABLISHED TCP session to OTLP gRPC port (SSIM ``is_otlp_session_established``)."""
    cmd = "netstat -ptan | grep ESTABLISHED | grep otelcol"
    with allure.step(f"Verify OTLP TCP session established on port {port}"):
        for attempt in range(1, max_attempts + 1):
            output = collector.engine.run_cmd(
                f"sudo {cmd}", validate=False, print_output=False
            )
            if str(port) in output:
                logger.info("OTLP session established on port %s (attempt %d)", port, attempt)
                return
            if attempt < max_attempts:
                time.sleep(retry_interval_sec * attempt)
        pytest.fail(
            f"OTLP TCP session not established on port {port} after {max_attempts} attempts"
        )


def assert_otlp_session_established_or_restart_client(
    collector: OtelCollector,
    dut,
    vrf: str,
    *,
    telemetry_unit: Optional[str] = None,
    is_nvos: bool = False,
) -> None:
    """Poll for the collector session, restarting the DUT client once if needed.

    A large secured ``nv config apply`` does not always bounce ``nv-telemetry``
    before the first poll. The client then keeps the pre-TLS export and never
    finishes the handshake, so the collector shows no ESTABLISHED session.
    """
    try:
        assert_otlp_session_established(collector)
        return
    except BaseException as exc:
        if exc.__class__.__name__ != "Failed":
            raise
        logger.warning(
            "OTLP session not established; restarting telemetry client and retrying"
        )
    restart_nvtelemetry(dut, vrf, unit=telemetry_unit)
    if not is_nvos:
        restart_asic_monitor(dut, vrf)
    assert_otlp_session_established(collector, max_attempts=12)


def ensure_mgmt_vrf_secured_otlp_session(dut, collector, *, collector_ip: str) -> None:
    """Re-apply secured mgmt VRF OTLP and verify collector TCP session.

    NGTS ``clear_config`` between tests can drop export state while the module-scoped
    ``otel_suite_mgmt_secured`` fixture still holds the TLS collector — call this before
    secured health test02 (SSIM keeps config across class tests).

    Restores the collector receiver too: a module that also uses ``otel_suite_mgmt``
    re-points the shared collector host at the plaintext config, so the DUT would
    otherwise handshake TLS against a plaintext listener.
    """
    from ngts.tests_nvos.system.telemetry.otel.cumulus.helpers import (
        apply_otel_secured_telemetry_config,
    )
    from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
        restart_nvtelemetry,
    )
    from ngts.tests_nvos.system.telemetry.otel.helpers import (
        configure_switch_otlp_grpc_secured_destination,
    )

    vrf = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT
    with allure.step("Ensure collector still serves the secured (TLS) OTLP receiver"):
        collector.ensure_config_applied()
    with allure.step("Ensure mgmt VRF secured OTLP export is configured"):
        configure_switch_otlp_grpc_secured_destination(
            dut,
            collector_ip,
            export_vrf=vrf,
            default_vrf_grpc_certificate=False,
            is_nvos=False,
        )
        apply_otel_secured_telemetry_config(
            dut,
            export_vrf=vrf,
            enable_routing=False,
            enable_interface_histogram=True,
        )
        restart_nvtelemetry(dut, vrf)
    assert_otlp_session_established(collector)


def cleanup_otlp_export_session(
    dut,
    collector: OtelCollector,
    vrf: str = CumulusOtelConst.TELEMETRY_EXPORT_VRF_MGMT,
    *,
    telemetry_unit: Optional[str] = None,
    is_nvos: bool = False,
) -> None:
    """Stop DUT + collector, truncate export, restart (SSIM ``cleanup_server_export_file``).

    On Cumulus, also restarts ``asic-monitor@<vrf>`` and waits so
    ``histogram-export-service`` can report active. NVOS runs a single
    ``nv-telemetry.service`` (``telemetry_unit``) and has no asic-monitor.
    """
    with allure.step("Cleanup and restart OTLP export session"):
        stop_nvtelemetry(dut, vrf, unit=telemetry_unit)
        collector.stop()
        collector.truncate_artifact()
        collector.ensure_running(install_if_missing=False)
        start_nvtelemetry(dut, vrf, unit=telemetry_unit)
        if not is_nvos:
            restart_asic_monitor(dut, vrf)
        settle = CumulusOtelConst.OTEL_HEALTH_POST_CLEANUP_SETTLE_SEC
        logger.info(
            "Waiting %ss for OTLP export and histogram-export-service to stabilize",
            settle,
        )
        time.sleep(settle)


def stop_otlp_related_systemd_units(
    dut,
    *,
    units: Tuple[Tuple[str, Optional[str]], ...] = CumulusOtelConst.OTEL_SYSTEMD_STOP_UNITS,
    max_attempts: int = 40,
) -> None:
    """Stop OTLP-related systemd units; optionally wait for health service inactive."""
    with allure.step(f"Stop {len(units)} OTLP-related systemd unit(s)"):
        for unit, health_service in units:
            dut.run_cmd(f"sudo systemctl stop {unit}", validate=False)
            if health_service:
                wait_until_service_status(
                    dut,
                    service=health_service,
                    status="inactive",
                    max_attempts=max_attempts,
                )
            logger.info("stopped systemd unit %s", unit)


def start_otlp_related_systemd_units(
    dut,
    *,
    units: Tuple[str, ...] = CumulusOtelConst.OTEL_SYSTEMD_START_UNITS,
    settle_sec: int = CumulusOtelConst.OTEL_SERVICES_RESTART_SETTLE_SEC,
) -> None:
    """Start OTLP-related systemd units and wait for stabilization."""
    with allure.step(f"Start {len(units)} OTLP-related systemd unit(s)"):
        for unit in units:
            dut.run_cmd(f"sudo systemctl start {unit}", validate=False)
            logger.info("started systemd unit %s", unit)
        logger.info("Waiting %ss for OTLP services to stabilize", settle_sec)
        time.sleep(settle_sec)


def verify_drop_counter(dut, server: str) -> None:
    """Assert export drop-counter is zero (SSIM ``verify_drop_counter``)."""
    health = show_telemetry_health(dut)
    drops = int((health.get("export-destination-status") or {})[server]["drop-counter"])
    if drops != 0:
        pytest.fail(
            f"export destination {server!r} has drop-counter={drops}, expected 0"
        )
    logger.info("export destination %s drop-counter=0", server)


def verify_otlp_client_active(
    dut,
    vrf: str,
    *,
    is_nvos: bool = False,
    telemetry_unit: Optional[str] = None,
) -> None:
    """DUT OTLP client services are running (SSIM ``is_otlp_client_active``).

    NVOS runs a single ``nv-telemetry.service`` and has no prometheus sidecars or
    per-VRF ``asic-monitor`` histogram export, so only that unit is checked there.
    """
    from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
        _asic_monitor_active,
        _parse_systemctl_is_active,
    )

    if is_nvos:
        units = (telemetry_unit or "nv-telemetry.service",)
    else:
        units = (
            f"nv-telemetry@{vrf}",
            "prometheus-sdk-stats",
            "prometheus-node-exporter",
        )
    with allure.step(f"Verify OTLP client services active (vrf={vrf}, is_nvos={is_nvos})"):
        for unit in units:
            state = _parse_systemctl_is_active(
                dut.run_cmd(
                    f"systemctl is-active {unit} 2>&1",
                    validate=False,
                    print_output=False,
                )
            )
            if state != "active":
                status = dut.run_cmd(
                    f"systemctl status {unit} --no-pager 2>&1",
                    validate=False,
                    print_output=False,
                )
                pytest.fail(
                    f"OTLP client service {unit!r} is not active (vrf={vrf}, "
                    f"is-active={state!r}):\n{status}"
                )
            logger.info("service %s is active", unit)

        if is_nvos:
            # NVOS has no asic-monitor / histogram-export-service.
            return

        if not _asic_monitor_active(dut, vrf):
            unit = f"asic-monitor@{vrf}"
            status = dut.run_cmd(
                f"systemctl status {unit} --no-pager 2>&1",
                validate=False,
                print_output=False,
            )
            pytest.fail(
                f"OTLP histogram export not active ({unit!r} / histogram-export-service) "
                f"(vrf={vrf}):\n{status}"
            )
        logger.info("asic-monitor / histogram-export-service active (vrf=%s)", vrf)


def verify_otelcol_server_active(collector: OtelCollector) -> None:
    """Collector ``otelcol`` process is active."""
    with allure.step("Verify OTEL collector server is active"):
        if not collector.is_server_active():
            pytest.fail(f"otelcol not active on collector {collector.ip}")


def verify_export_destinations_connectivity(
    dut,
    *,
    status: str = _CONNECTIVITY_STATUS_PASS,
    max_connectivity_attempts: int = 40,
    check_counter_flow: bool = False,
    check_drop_counter: bool = False,
) -> None:
    """Poll connectivity for each export destination; optional counter/drop checks."""
    servers = [
        s for s in get_export_destinations_from_health(dut) if not str(s).startswith("unix://")
    ]
    assert servers, "no gRPC/HTTP export destinations in telemetry health"
    with allure.step(
        f"Verify export destination connectivity={status!r} ({len(servers)} server(s))"
    ):
        for server in servers:
            wait_until_connectivity_status(
                dut,
                server=server,
                status=status,
                max_attempts=max_connectivity_attempts,
            )
            if status == _CONNECTIVITY_STATUS_PASS:
                if check_drop_counter:
                    verify_drop_counter(dut, server)
                if check_counter_flow:
                    verify_counter_flow(dut, server)


def restart_otlp_collector_and_verify_health(
    dut,
    collector: OtelCollector,
    *,
    verify_counter_flow_after: bool = True,
) -> None:
    """Stop collector → connectivity Fail → start collector → session + Pass (+ counter flow)."""
    servers = [
        s for s in get_export_destinations_from_health(dut) if not str(s).startswith("unix://")
    ]
    assert servers, "no export destinations before collector restart test"

    with allure.step("Stop OTLP collector"):
        collector.stop()

    with allure.step("Verify export connectivity Fail while collector is down"):
        verify_export_destinations_connectivity(
            dut, status=_CONNECTIVITY_STATUS_FAIL, max_connectivity_attempts=40
        )

    with allure.step("Start OTLP collector and verify session"):
        collector.ensure_running(install_if_missing=False)
        assert_otlp_session_established(collector)

    verify_otel_health_services(dut)
    verify_export_destinations_connectivity(
        dut,
        status=_CONNECTIVITY_STATUS_PASS,
        check_counter_flow=verify_counter_flow_after,
    )
